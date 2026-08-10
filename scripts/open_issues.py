# -*- coding: utf-8 -*-
"""要確認案件の恒久台帳（open_issues.json）操作ツール。

「要手動確認がメール1通に埋もれて放置される」問題の構造対策（2026-06-30設計 wkc8g7yhw）。
自動タスク（new-machine/auto-add/verify/quality-review）がエスカレーションを add で積み、
verify の毎朝メールに digest（未解決一覧・経過日数付き）を再掲し続ける。
解決したら close する（人間または対応したセッションのClaude）。
無人タスクのcloseは原則禁止。例外は2つのみ＝
①verify STEP 2.8のホワイトリスト型quality修正（audit/validate機械確認済み・数値転記を含む案件は対象外）
②verify STEP 2.9の自動裏取り（verify_claims.py exit 0＝出典の機械検証合格が根拠の場合のみ）。

★このスクリプトは機種データを一切触らない（台帳ファイルの読み書きのみ）★

使い方:
  python scripts/open_issues.py add --source verify --slug hokuto --kind external_value \
      --title "狙い目760G疑義" --detail "複数サイトは550G/650Gの報告あり・要裏取り"
      → 同一(slug+kind+title)が既にopenなら重複登録せず last_seen だけ更新
  python scripts/open_issues.py list            # open案件を一覧表示
  python scripts/open_issues.py list --all      # closed含め全件
  python scripts/open_issues.py digest          # メール転記用ブロックを出力（open 0件なら空出力・exit 0）
  python scripts/open_issues.py close --id 3 --reason "5サイト裏取りの上150Gに統一(コミットabc123)"

kind の目安:
  external_value    外部数値の疑義（無人修正禁止カテゴリ・裏取り待ち）
  structural        構造判断（重複統合・新規作成可否など）
  quality           品質指摘（quality-review C評価など）
  environment       環境問題（python3スタブ等）
  other             その他

保存先: C:/Users/imao_/Documents/uchidokoro/open_issues.json（--fileで上書き可・テスト用）
"""
import argparse
import datetime
import json
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DEFAULT_FILE = Path("C:/Users/imao_/Documents/uchidokoro/open_issues.json")

# ---------------------------------------------------------------- 自由文の受け取り
# ★なぜファイル渡しにするか（2026-08-09）★
#   2026-08-08、無人タスクが台帳に
#     「`python scripts/codex_reported.py` を実行する必要がある」
#   と**書こうとしただけ**で、その部分が本当に実行された。
#   バッククォートは文章としては飾りでも、シェルには
#   「ここを実行して結果を差し込め」という命令だから。
#   手順書は「ツールの出力をそのまま転記」「外部サイトの機種名を渡す」形なので、
#   同じことがいつでも起き得る。
#   ★文章はファイルに書き、コマンドにはパスだけを渡す★＝中身は読まれるだけで
#   実行されない。無人タスクが動いている間は、直接指定を受け付けない。

LOCK_PATH = Path("C:/Users/imao_/Documents/uchidokoro/task.lock")
LOCK_STALE_MIN = 30           # task_lock.py と同じ（これを超えたら残骸とみなす）
MAX_TEXT_BYTES = 64 * 1024

# ★文章ファイルはここから下だけ★（2026-08-09・依頼127 A-2 P1）
#   どこのファイルでも読めると、うっかり認証情報のファイルを指したときに
#   台帳やメールへその中身が写る。置き場を決めておけば起こらない。
TEXT_ROOTS = (
    Path("C:/Users/imao_/Documents/uchidokoro/ops"),
    Path("C:/Users/imao_/Desktop/個人用/うちどころ/_design"),
)


def _running_task() -> str:
    """無人タスクが動いている最中ならタスク名を返す（動いていなければ空）。"""
    try:
        d = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except Exception:                        # noqa: BLE001
        return ""
    ts = d.get("heartbeat") or d.get("started_at")
    try:
        t = datetime.datetime.fromisoformat(str(ts).replace("Z", ""))
    except Exception:                        # noqa: BLE001
        return ""
    if (datetime.datetime.now() - t).total_seconds() / 60.0 > LOCK_STALE_MIN:
        return ""                            # 残骸のロックは「動いていない」扱い
    return str(d.get("task") or "")


def _read_text_arg(inline: str, path: str, label: str,
                   allow_newline: bool = True) -> str:
    """自由文を受け取る。★直接指定とファイル指定は同時に使えない★"""
    if inline and path:
        raise SystemExit(f"★{label} は直接指定とファイル指定を同時に使えません★")
    if path:
        p = Path(path)
        if p.is_symlink() or not p.is_file():
            raise SystemExit(f"★{label}: 通常のファイルではありません: {path}★")
        real = p.resolve()

        def _inside(child: Path, root: Path) -> bool:
            # ★フォルダの区切りで見る★（2026-08-09・依頼127→128で修正）
            #   文字の前方一致で見ていたため、隣の `ops-secret` まで
            #   許可されていた（実際に読めることを確認した）。
            try:
                child.relative_to(root)
                return True
            except ValueError:
                return False

        if not any(_inside(real, r.resolve()) for r in TEXT_ROOTS):
            raise SystemExit(
                f"★{label}: この置き場のファイルは使えません: {real}★ "
                + "／".join(str(r) for r in TEXT_ROOTS) + " の下に置いてください"
                "（うっかり認証情報のファイルを指しても台帳に写らないため）")
        size = real.stat().st_size          # ★読む前に大きさを見る★
        if size > MAX_TEXT_BYTES:
            raise SystemExit(
                f"★{label}: 大きすぎます（{size}バイト・上限{MAX_TEXT_BYTES}）★")
        raw = real.read_bytes()
        try:
            text = raw.decode("utf-8")       # ★strict＝壊れた文字は受け取らない★
        except UnicodeDecodeError as e:
            raise SystemExit(f"★{label}: UTF-8として読めません（{e}）★")
        # ★Windowsの改行（CRLF）で書かれたファイルも受け取る★
        #   以前はCRを制御文字として弾いていた。メモ帳等で書くと必ずCRLFになる。
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    else:
        text = inline or ""
        # ★シェルを通らない呼び出しだけは直接指定を許す★（2026-08-09）
        #   add_machine_run.py などは subprocess の引数配列で呼ぶので、
        #   文章の中の記号が実行されることはない（危ないのはシェル文字列だけ）。
        #   この印は「うっかり古い書き方に戻らないため」のものであって、
        #   安全の境界ではない（境界は PreToolUse の shell_guard.py）。
        if text and os.environ.get("UCHIDOKORO_ARGV_CALL") != "1" \
                and _running_task():
            raise SystemExit(
                f"★{label} は無人タスクの実行中は直接指定できません"
                f"（{_running_task()} が実行中）★ "
                f"文章をファイルに書いて --{label}-file でパスを渡してください"
                "（コマンドに文章を書くと、中の記号がシェルに実行されます）")
    bad = [c for c in text if c in "\x00" or (ord(c) < 32 and c not in "\n\t")]
    if bad:
        raise SystemExit(f"★{label}: 使えない制御文字が入っています★")
    if not allow_newline and ("\n" in text or "\r" in text):
        raise SystemExit(f"★{label}: 改行は入れられません★")
    return text.strip()


# ★どれだけ危ないか★（2026-07-30・Codex「これだけはやれ」⑧）
#   C評価が52件たまっていたが、「全部止める」も「全部出し続ける」も雑すぎる。
#   **公開を止めるべきものだけ**を機械が判別できるように段階を付ける。
#
#   CRITICAL … 機械の客観的な事実が誤っている疑い。★公開を止める★
#               別機種・別型式の混入／天井・恩恵・設定段階・機種タイプの誤り／
#               CZ間とAT間、実G と 液晶G、G と pt の取り違え。
#   MATERIAL … 当サイトの目安どうしが食い違っている等。読者は混乱するが、
#               機械について誤ったことを述べてはいない。公開は続けて順に直す。
#   QUALITY  … 文体・冗長・読みやすさ。公開に影響しない。
SEVERITIES = ("CRITICAL", "MATERIAL", "QUALITY")


def severity_of(issue: dict) -> str:
    """案件の危険度。★未設定は MATERIAL 扱いにしない★

    未設定＝まだ人が仕分けていない、という意味なので、
    公開を止める側（CRITICAL）に倒す（fail-closed）。
    仕分けが終わっていないものを黙って公開に通さない。
    """
    sev = issue.get("severity")
    return sev if sev in SEVERITIES else "CRITICAL"


def blocking_slugs(path=None) -> dict:
    """★公開を止めるべき機種★ {slug: [理由, ...]}（未解決の CRITICAL だけ）"""
    data = _load(Path(path) if path else DEFAULT_FILE)
    out: dict = {}
    for it in data.get("issues") or []:
        if it.get("status") != "open":
            continue
        if severity_of(it) != "CRITICAL":
            continue
        slug = it.get("slug")
        if not slug or slug in ("site", "env", "_site", "-"):
            continue        # サイト全体の課題は機種の公開停止にしない
        out.setdefault(slug, []).append(f"#{it['id']} {it.get('title','')}")
    return out


def _load(path):
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"next_id": 1, "issues": []}


def _save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _today():
    return datetime.date.today().isoformat()


def _days_open(issue):
    try:
        first = datetime.date.fromisoformat(issue["first_seen"])
        return (datetime.date.today() - first).days
    except Exception:
        return -1


def selftest() -> int:
    """★自由文の受け取りかたの回帰テスト★（2026-08-09・依頼126〜128）

    ここを緩めると「文章を書いただけでコマンドが実行される」経路が戻る。
    """
    import tempfile

    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    def stops(name, fn):
        try:
            fn()
            t(name, False)
        except SystemExit:
            t(name, True)

    global LOCK_PATH
    keep = LOCK_PATH
    tmp = Path(tempfile.mkdtemp())
    ops = TEXT_ROOTS[0]
    ops.mkdir(parents=True, exist_ok=True)
    try:
        LOCK_PATH = tmp / "task.lock"
        LOCK_PATH.write_text(json.dumps(
            {"task": "uchidokoro-add-machine",
             "heartbeat": datetime.datetime.now().isoformat()}), encoding="utf-8")
        t("　無人タスクが動いていると分かる", _running_task())
        stops("★★無人タスク実行中はシェルからの直接指定を断る★★",
              lambda: _read_text_arg("直接書いた文章", "", "detail"))

        os.environ["UCHIDOKORO_ARGV_CALL"] = "1"
        t("　実行器（引数配列）からの直接指定は通す",
          _read_text_arg("実行器からの文章", "", "detail") == "実行器からの文章")
        del os.environ["UCHIDOKORO_ARGV_CALL"]

        mark = chr(96) + "記号" + chr(96) + " と " + chr(36) + "(canary)"
        good = ops / "_selftest_detail.txt"
        good.write_text(mark, encoding="utf-8", newline="\n")
        t("★★ファイル渡しは無人でも通り、記号はそのまま残る★★",
          _read_text_arg("", str(good), "detail") == mark)

        crlf = ops / "_selftest_crlf.txt"
        crlf.write_bytes("1行目\r\n2行目".encode("utf-8"))
        t("　Windowsの改行（CRLF）でも受け取れる",
          _read_text_arg("", str(crlf), "detail") == "1行目\n2行目")

        # ★隣のフォルダを許さない★（依頼128で実際に読めてしまった）
        sib = Path(str(ops) + "-secret")
        sib.mkdir(parents=True, exist_ok=True)
        himitsu = sib / "_selftest.txt"
        himitsu.write_text("許可していない置き場", encoding="utf-8")
        stops("★★許可した置き場の『隣』は読まない（ops-secret 等）★★",
              lambda: _read_text_arg("", str(himitsu), "detail"))

        outside = tmp / "outside.txt"
        outside.write_text("よその文章", encoding="utf-8")
        stops("　決めた置き場の外は読まない",
              lambda: _read_text_arg("", str(outside), "detail"))
        stops("　直接指定とファイル指定の同時使用を断る",
              lambda: _read_text_arg("a", str(good), "detail"))

        bad = ops / "_selftest_bad.bin"
        bad.write_bytes(b"\xff\xfe not utf8")
        stops("　UTF-8として読めないファイルを断る",
              lambda: _read_text_arg("", str(bad), "detail"))

        nl = ops / "_selftest_nl.txt"
        nl.write_text("1行目\n2行目", encoding="utf-8", newline="\n")
        stops("　題に改行は入れられない",
              lambda: _read_text_arg("", str(nl), "title", allow_newline=False))

        big = ops / "_selftest_big.txt"
        big.write_text("あ" * 40000, encoding="utf-8", newline="\n")
        stops("　大きすぎるファイルを断る",
              lambda: _read_text_arg("", str(big), "detail"))

        LOCK_PATH.write_text(json.dumps(
            {"task": "x", "heartbeat":
             (datetime.datetime.now() - datetime.timedelta(hours=2)).isoformat()}),
            encoding="utf-8")
        t("　残骸のロックは実行中とみなさない", _running_task() == "")

        # ★コードから台帳へ登録する道が生きているか★（2026-08-10・台帳#300）
        #   2026-08-09に --title-file を足したとき、コード側は Namespace を
        #   手で組んでいたので **2か所とも黙って壊れた**（安全網が黙って死んだ）。
        #   CLIに引数を足しても、この道が壊れないことをここで固定する。
        store = tmp / "issues.json"
        n = add_issue(store, source="grow-machine", slug="s1",
                      kind="external_value", title="値が再現できません",
                      detail="出典が消えました", severity="MATERIAL",
                      reason_code="GROW_VALUE_LOST")
        got = json.loads(store.read_text(encoding="utf-8"))
        t("★★コードから台帳へ登録できる★★"
          "（黙って止まり続けないための安全網そのもの）",
          n == 0 and len(got["issues"]) == 1
          and got["issues"][0]["title"] == "値が再現できません")

        # ★重ならないだけでなく、最終確認日と追記まで見る★（依頼141の指摘2）
        old = json.loads(store.read_text(encoding="utf-8"))
        old["issues"][0]["last_seen"] = "2020-01-01"
        store.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
        add_issue(store, source="grow-machine", slug="s1",
                  kind="external_value", title="値が再現できません",
                  detail="別の出典も消えました", severity="MATERIAL")
        one = json.loads(store.read_text(encoding="utf-8"))["issues"]
        t("　同じ題は重ねず、最終確認日が動いて詳細が追記される",
          len(one) == 1 and one[0]["last_seen"] == _today()
          and "別の出典も消えました" in one[0]["detail"]
          and "追記]" in one[0]["detail"])
        add_issue(store, source="grow-machine", slug="s1",
                  kind="external_value", title="値が再現できません",
                  detail="別の出典も消えました", severity="MATERIAL")
        two = json.loads(store.read_text(encoding="utf-8"))["issues"]
        t("　同じ詳細は二度追記しない",
          two[0]["detail"].count("別の出典も消えました") == 1)

        # ★★実際に使っている2本を、本当に呼んで確かめる★★（依頼141の指摘1）
        #   文字列があるかを見るだけでは、綴り違い・呼ばれない分岐・
        #   引数の組み立て崩れを通してしまう＝今回の事故そのものを防げない。
        #   grow_machine は例外を握ってログだけにするので、
        #   「落ちないこと」ではなく**台帳に載ったこと**を見る。
        real = tmp / "real.json"
        # ★呼ぶ側が読んでいる実体を差し替える★
        #   このファイルを直接動かすと自分は `__main__` になり、
        #   grow_machine が読む `open_issues` は**別の実体**になる。
        #   自分の globals を書き換えても届かないので、名前で取り直す。
        import open_issues as _oi_mod
        keep_default = _oi_mod.DEFAULT_FILE
        try:
            _oi_mod.DEFAULT_FILE = real
            import grow_legacy as _gl
            import grow_machine as _gm
            _gm.ledger_once("s9", "s9: 値が再現できません", "詳細です",
                            "MATERIAL")
            _gl._to_ledger("s8", ["材料が集まりません"], transient=True)
            _gl._to_ledger("s7", ["形が違います"], transient=False)
            rows = {r["slug"]: r for r in
                    json.loads(real.read_text(encoding="utf-8"))["issues"]}
            limit = _gl._TRANSIENT_LIMIT
        finally:
            _oi_mod.DEFAULT_FILE = keep_default
        t("★★実際に使っている2本を呼んで、本当に台帳へ載ることを確かめる★★"
          "（文字列を探すだけでは、今回の事故そのものを見逃す）",
          len(rows) == 3 and set(rows) == {"s9", "s8", "s7"})
        # ★載ったかだけでなく、中身が正しいかまで見る★（依頼142の指摘1）
        #   分類や危険度を取り違えたまま登録されると、人の見る順番が狂う。
        t("　値が再現できない側は、分類・危険度・理由コード・詳細まで正しい",
          rows.get("s9", {}).get("source") == "grow-machine"
          and rows["s9"]["kind"] == "external_value"
          and rows["s9"]["severity"] == "MATERIAL"
          and rows["s9"]["reason_code"] == "GROW_VALUE_LOST"
          and rows["s9"]["title"] == "s9: 値が再現できません"
          and rows["s9"]["detail"] == "詳細です")
        t("　材料が集まらない側（何度も続いた）も、題と分類が正しい",
          rows.get("s8", {}).get("source") == "update-machine"
          and rows["s8"]["kind"] == "external_value"
          and rows["s8"]["severity"] == "QUALITY"
          and rows["s8"]["reason_code"] == "GROW_LEGACY_TRANSIENT"
          and rows["s8"]["title"] == (
              "s8: 旧方式の先行記事の材料を%d回続けて集められません" % limit)
          and "材料が集まりません" in rows["s8"]["detail"])
        t("★★人の判断が要る側は、危険度も分類も別になる★★"
          "（同じ関数の2つの分岐を取り違えると、緊急のものが埋もれる）",
          rows.get("s7", {}).get("kind") == "structural"
          and rows["s7"]["severity"] == "MATERIAL"
          and rows["s7"]["reason_code"] == "GROW_LEGACY_HALT"
          and "人の判断が要ります" in rows["s7"]["title"])

        for f in (good, crlf, bad, nl, big, himitsu, store, real):
            try:
                f.unlink()
            except Exception:              # noqa: BLE001
                pass
        try:
            sib.rmdir()
        except Exception:                  # noqa: BLE001
            pass
    finally:
        LOCK_PATH = keep

    ng = sum(1 for _, o in results if not o)
    print()
    print("%d/%d 合格" % (len(results) - ng, len(results)))
    return 1 if ng else 0


def add_issue(path, *, source, slug, kind, title, severity, detail="",
              reason_code=None):
    """★コードから台帳へ登録する入口★（CLIの引数の形に左右されない）

    ★なぜ分けたか★（2026-08-10・台帳#300）
      以前はコードからも `cmd_add(Namespace(...))` を組み立てて呼んでいた。
      そのため**CLIに引数を1つ足すたびにコード側が黙って壊れる**。
      実際 2026-08-09 に `--title-file` を足したところ、
      grow_machine と grow_legacy の台帳登録が2か所とも落ちていた
      ＝「黙って止まり続けないための安全網」自体が、黙って死んでいた。
      CLIの都合（ファイル渡し・既定値）は cmd_add に閉じ込める。
    """
    title = str(title or "").strip()
    if not title:
        raise ValueError("title が空です")
    data = _load(path)
    for it in data["issues"]:
        if it["status"] == "open" and it["slug"] == slug and \
           it["kind"] == kind and it["title"] == title:
            it["last_seen"] = _today()
            if detail and detail not in (it.get("detail") or ""):
                it["detail"] = (it.get("detail") or "") + f"\n[{_today()}追記] {detail}"
            _save(path, data)
            print(f"既存案件 #{it['id']} の last_seen を更新（重複登録なし・経過{_days_open(it)}日）")
            return 0
    issue = {
        "id": data["next_id"],
        "status": "open",
        "source": source,
        "slug": slug,
        "kind": kind,
        "title": title,
        "detail": detail or "",
        "first_seen": _today(),
        "last_seen": _today(),
        "severity": severity,
        "reason_code": reason_code or None,
        "resolution": None,
        "resolved_date": None,
    }
    data["issues"].append(issue)
    data["next_id"] += 1
    _save(path, data)
    print(f"新規案件 #{issue['id']} を登録: [{kind}] {slug}: {title}")
    return 0


def cmd_add(path, args):
    """CLIから台帳へ登録する（ファイル渡しなどCLI固有の都合はここに閉じる）。"""
    # ★引数が足りなければ早く大きく失敗させる★（2026-08-10・依頼141）
    #   getattr で既定値を補うと、CLI専用と決めた境界を破った呼び出しを
    #   部分的に延命し、**今回と同じ壊れ方をまた隠す**（次に必須項目が
    #   増えたときに同じ事故が起きる）。コードからは add_issue() を使う。
    title = _read_text_arg(args.title, args.title_file, "title",
                           allow_newline=False)
    detail = _read_text_arg(args.detail, args.detail_file, "detail")
    if not title:
        raise SystemExit("★--title または --title-file が要ります★")
    return add_issue(path, source=args.source, slug=args.slug, kind=args.kind,
                     title=title, detail=detail, severity=args.severity,
                     reason_code=args.reason_code)


def cmd_list(path, args):
    data = _load(path)
    items = data["issues"] if args.all else [i for i in data["issues"] if i["status"] == "open"]
    if not items:
        print("open案件なし" if not args.all else "案件なし")
        return 0
    for it in items:
        mark = "🔓" if it["status"] == "open" else "✅"
        days = f"・経過{_days_open(it)}日" if it["status"] == "open" else f"・解決{it.get('resolved_date')}"
        print(f"{mark} #{it['id']} [{it['kind']}] {it['slug']}: {it['title']}（{it['source']}・初出{it['first_seen']}{days}）")
        if it.get("detail"):
            for line in str(it["detail"]).splitlines():
                print(f"      {line}")
        if it["status"] != "open" and it.get("resolution"):
            print(f"      → 解決: {it['resolution']}")
    return 0


def cmd_digest(path, args):
    data = _load(path)
    items = [i for i in data["issues"] if i["status"] == "open"]
    if not items:
        return 0  # 空出力＝メールに何も足さない
    items.sort(key=_days_open, reverse=True)
    print("━━━ 未解決の要確認案件（解決するまで毎朝再掲されます） ━━━")
    for it in items:
        days = _days_open(it)
        urgency = "🔴" if days >= 7 else ("🟠" if days >= 3 else "🟡")
        print(f"{urgency} #{it['id']} [{it['kind']}] {it['slug']}: {it['title']}（経過{days}日・初出{it['first_seen']}・発見元{it['source']}）")
        if it.get("detail"):
            first_line = str(it["detail"]).splitlines()[0]
            print(f"    {first_line}")
    print(f"（計{len(items)}件。対応後は python scripts/open_issues.py close --id N --reason \"...\" でクローズ）")
    print("（対応方法: このメールをClaude Codeのセッションに貼り付けて「対応して」と伝えるだけでOK。裏取り→修正→closeまで処理されます）")
    return 0


def cmd_close(path, args):
    args.reason = _read_text_arg(args.reason, args.reason_file, "reason")
    if not args.reason:
        raise SystemExit("★--reason または --reason-file が要ります★")
    data = _load(path)
    for it in data["issues"]:
        if it["id"] == args.id:
            if it["status"] != "open":
                print(f"#{args.id} は既にclosed（{it.get('resolved_date')}）")
                return 0
            it["status"] = "closed"
            it["resolution"] = args.reason
            it["resolved_date"] = _today()
            _save(path, data)
            print(f"案件 #{args.id} をクローズ: {args.reason}")
            return 0
    print(f"⚠ 案件 #{args.id} が見つかりません")
    return 1


def cmd_severity(path, args):
    data = _load(path)
    for it in data["issues"]:
        if it["id"] == args.id:
            old = it.get("severity") or "(未設定)"
            it["severity"] = args.level
            it["reason_code"] = args.reason_code
            _save(path, data)
            print(f"#{args.id}: {old} → {args.level} ({args.reason_code})")
            return 0
    print(f"案件 #{args.id} が見つかりません")
    return 1


def cmd_blocking(path, args):
    """公開を止めるべき機種の一覧（ビルドが読む形）。"""
    b = blocking_slugs(path)
    print(json.dumps(b, ensure_ascii=False, indent=1))
    print(f"# 公開を止めるべき機種: {len(b)}", file=sys.stderr)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="", help="台帳ファイルパス（既定: Documents/uchidokoro/open_issues.json）")
    ap.add_argument("--selftest", action="store_true",
                    help="自由文の受け取りかたの回帰テスト")
    if "--selftest" in sys.argv:
        return selftest()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add")
    p.add_argument("--source", required=True, help="発見元タスク（verify/new-machine/auto-add/quality-review/manual）")
    p.add_argument("--slug", required=True, help="対象機種slug（機種以外は site/env 等）")
    p.add_argument("--kind", required=True, choices=["external_value", "structural", "quality", "environment", "other"])
    # ★自由文はファイル渡しを使う★（無人タスクでは直接指定を拒否・2026-08-09）
    p.add_argument("--title", default="")
    p.add_argument("--title-file", dest="title_file", default="",
                   help="一行要約を書いたファイル（無人タスクはこちら）")
    p.add_argument("--detail", default="")
    p.add_argument("--detail-file", dest="detail_file", default="",
                   help="判断材料を書いたファイル（無人タスクはこちら）")
    p.add_argument("--severity", choices=SEVERITIES, default="CRITICAL",
                   help="どれだけ危ないか（既定は CRITICAL＝仕分け前は止める側に倒す）")
    p.add_argument("--reason-code", dest="reason_code", default="",
                   help="機械可読な理由コード（例: WRONG_CEILING / MEYASU_MISMATCH）")

    p = sub.add_parser("severity")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--level", required=True, choices=SEVERITIES)
    p.add_argument("--reason-code", dest="reason_code", required=True)

    p = sub.add_parser("blocking")

    p = sub.add_parser("list")
    p.add_argument("--all", action="store_true")

    sub.add_parser("digest")

    p = sub.add_parser("close")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--reason", default="")
    p.add_argument("--reason-file", dest="reason_file", default="",
                   help="クローズ理由を書いたファイル（無人タスクはこちら）")

    args = ap.parse_args()
    path = Path(args.file) if args.file else DEFAULT_FILE
    fn = {"add": cmd_add, "list": cmd_list, "digest": cmd_digest, "close": cmd_close,
          "severity": cmd_severity, "blocking": cmd_blocking}[args.cmd]
    sys.exit(fn(path, args))


if __name__ == "__main__":
    raise SystemExit(main())

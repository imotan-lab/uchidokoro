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

保存先: （書類フォルダ）/uchidokoro/open_issues.json（--fileで上書き可・テスト用）
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

import os as _os_lp                 # noqa: E402
import sys as _sys_lp               # noqa: E402
_sys_lp.path.insert(0, _os_lp.path.dirname(_os_lp.path.abspath(__file__)))
import local_paths as _lp           # noqa: E402
DEFAULT_FILE = Path(_lp.doc("open_issues.json"))

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

LOCK_PATH = Path(_lp.doc("task.lock"))
LOCK_STALE_MIN = 30           # task_lock.py と同じ（これを超えたら残骸とみなす）
MAX_TEXT_BYTES = 64 * 1024

# ★文章ファイルはここから下だけ★（2026-08-09・依頼127 A-2 P1）
#   どこのファイルでも読めると、うっかり認証情報のファイルを指したときに
#   台帳やメールへその中身が写る。置き場を決めておけば起こらない。
TEXT_ROOTS = (
    Path(_lp.doc("ops")),
    Path(_lp.DESIGN),
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
    ops = TEXT_ROOTS[0]
    # ★自分が作ったものだけ片づける★（元からあった置き場は消さない）
    #   ★「前に見たら無かった」ではなく「自分が作れた」で決める★（依頼146）
    #     見てから作るまでの間に別の実行が作ることがあるので、
    #     exists() の結果を所有の根拠にしない。
    ops_created = False
    # ★後片付けは finally で必ず通すので、先に名前を用意しておく★
    #   （途中で落ちた回に、まだ作っていない名前を触って別の失敗にしない）
    tmp = None
    good = crlf = bad = nl = big = himitsu = sib = None
    stuck = []                 # ★消せなかったもの（黙って残さない）★
    try:
        # ★一時フォルダを作るのも try の中★（2026-08-11・依頼145）
        #   外で作ると、この直後の mkdir が失敗したときに finally へ入らず、
        #   フォルダが残ったままになる。
        tmp = Path(tempfile.mkdtemp())
        try:
            ops.mkdir(parents=True)        # ★作れたときだけ自分のもの★
            ops_created = True
        except FileExistsError:
            pass
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
        #   ★名前は固定にしない★（2026-08-11・依頼146）
        #     固定名 `ops-secret` を丸ごと消す形にしてしまい、
        #     **人が置いた同名フォルダがあれば中身ごと消える**ところだった。
        #     頭が `ops-` で始まる使い捨ての名前にすれば、
        #     「隣を読まない」の検査はそのままで、自分の作ったものだけ消せる。
        sib = Path(tempfile.mkdtemp(prefix=ops.name + "-", dir=ops.parent))
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
            # ★先に「行の数」で見る★（依頼143の指摘2）
            #   いきなり slug をキーにすると、同じ機種の行が二重に増えても
            #   上書きされて気づけない（重複が台帳に溜まる回帰を見逃す）。
            raw = json.loads(real.read_text(encoding="utf-8"))["issues"]
            rows = {r["slug"]: r for r in raw}
            limit = _gl._TRANSIENT_LIMIT
        finally:
            _oi_mod.DEFAULT_FILE = keep_default
        t("★★実際に使っている2本を呼んで、本当に台帳へ載ることを確かめる★★"
          "（文字列を探すだけでは、今回の事故そのものを見逃す）",
          len(raw) == 3 and set(rows) == {"s9", "s8", "s7"})
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
          and "人の判断が要ります" in rows["s7"]["title"]
          and rows["s7"]["detail"]
          == "grow_legacy.py --next が止まりました: 形が違います")
        # ★★聞くことの種類を増やしても輪から外れない★★（2026-08-14・依頼190）
        _q = tempfile.NamedTemporaryFile(
            suffix=".json", delete=False, mode="w", encoding="utf-8")
        _q.write(json.dumps({"next_id": 4, "issues": [
            {"id": 1, "status": "open", "reason_code": "ASK_2AI",
             "first_seen": "2026-08-01"},
            {"id": 2, "status": "open", "reason_code": "ASK_2AI_MAKER",
             "first_seen": "2026-08-02"},
            {"id": 3, "status": "open", "reason_code": "GROW_VALUE_LOST",
             "first_seen": "2026-08-03"},
        ]}, ensure_ascii=False))
        _q.close()
        try:
            _ids = [i["id"] for i in open_questions(Path(_q.name))]
            t("★★メーカー表記の質問も同じ晩の輪に入る★★（依頼190）"
              "／理由コードを完全一致で見ていたので、種類を増やすと"
              "**黙って自動の輪から外れる**ところだった",
              _ids == [1, 2])
            t("　（対照）関係ない理由コードは拾わない", 3 not in _ids)
        finally:
            os.unlink(_q.name)
    finally:
        LOCK_PATH = keep
        # ★後片付けは必ず通る場所へ★（依頼143の指摘3）
        #   途中で落ちた回ほど一時ファイルが残るので、finally に置く。
        # ★決めた置き場（ops）と、その隣に作ったものを消す★
        for f in (good, crlf, bad, nl, big, himitsu):
            if f is None:
                continue
            try:
                f.unlink()
            except FileNotFoundError:
                pass
            except Exception as e:         # noqa: BLE001
                stuck.append("%s（%s）" % (f.name, type(e).__name__))
        for d in (sib, tmp):
            # ★一時フォルダは丸ごと消す★（2026-08-11・依頼144）
            #   個々に並べると**足し忘れたものが黙って残る**ので、
            #   この中に作ったものはまとめて回収する。
            if d is None:
                continue
            try:
                import shutil
                shutil.rmtree(d)
            except FileNotFoundError:
                pass
            except Exception as e:         # noqa: BLE001
                stuck.append("%s（%s）" % (d.name, type(e).__name__))
        if ops_created:
            # 自分が作った置き場だけ戻す（元からあれば触らない）
            try:
                ops.rmdir()
            except FileNotFoundError:
                pass
            except OSError as e:
                # ★ここの失敗も黙って通さない★（依頼146）
                stuck.append("%s（%s）" % (ops.name, type(e).__name__))
        # ★消せなかったことを黙って通さない★（2026-08-11・依頼145）
        #   握りつぶしていたので、権限や掴まれで残っても合格に見えていた。
        t("　後片付けが実際にできた（残ったもの: %s）"
          % ("なし" if not stuck else "、".join(stuck)), not stuck)

    ng = sum(1 for _, o in results if not o)
    print()
    print("%d/%d 合格" % (len(results) - ng, len(results)))
    return 1 if ng else 0


def add_argv(*, source, slug, kind, title, severity, detail="",
             reason_code=None, python=None, script=None) -> list:
    """★別プロセスから台帳へ登録するときの引数列を作る唯一の場所★

    （2026-08-21・台帳#312）

    ★なぜ要るのか★
      コード側が「--source」「--slug」…とオプション名を**自分で並べて**
      別プロセスを起動している箇所が3つあった
      （add_machine_run / codex_audit / machine_sources）。
      ★CLIのオプション名や必須項目を変えると、3つとも黙って失敗しうる★。
      #300 と同じ型（オプション名への依存がコードの各所に散る）。

      ★オプション名を書く場所をここ1か所にする★＝
      CLIを変えたらここだけ直せばよい。

    ★シェルを通さない★＝引数の配列をそのまま subprocess へ渡す前提。
      （自由文をシェル文字列に入れない、という運用の線・鉄則1c）

    使い方:
        subprocess.run(open_issues.add_argv(source=..., slug=..., ...),
                       cwd=BASE, capture_output=True)
    """
    import sys as _sys
    py = python or _sys.executable
    sc = script or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "open_issues.py")
    if not str(title or "").strip():
        raise ValueError("title が空です")
    argv = [py, sc, "add",
            "--source", str(source), "--slug", str(slug),
            "--kind", str(kind), "--severity", str(severity),
            "--title", str(title)]
    if detail:
        argv += ["--detail", str(detail)]
    if reason_code:
        argv += ["--reason-code", str(reason_code)]
    return argv


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


def open_questions(path) -> list:
    """★まだ答えが出ていない「2AIに聞くこと」★（2026-08-12）

    人を中継役にしないため、翌日のタスクがここから1件拾って答える。
    古いものから返す（放置を作らない）。
    """
    data = _load(path)
    return sorted(
        (i for i in data["issues"]
         # ★ASK_2AI で始まるものはすべて拾う★（2026-08-14・依頼190）
         #   メーカー表記の質問（ASK_2AI_MAKER）は聞き方が違うだけで、
         #   ★同じ晩に片づける★点は同じ。ここを完全一致にしていると、
         #   新しい種類を足したときに**黙って自動の輪から外れる**。
         if i.get("status") == "open"
         and str(i.get("reason_code") or "").startswith("ASK_2AI")
         # ★人へ渡し終えたものだけ外す★（2026-08-12・依頼164のP1）
         #   回数だけで外すと、メールに失敗した質問が
         #   **自動の輪からも通知からも同時に消える**。
         #   知らせ終えた（notified_at がある）ものだけを外す。
         and not i.get("notified_at")),
        key=lambda i: (str(i.get("first_seen") or ""), i.get("id") or 0))


# ★何回やり直したら人に知らせるか★（2026-08-12・運営者決定）
ASK_MAX_ATTEMPTS = 3


def cmd_attempt(path, args):
    """★1回やり直したことを記録する★（決まらなかったときだけ呼ぶ）

    3回目で「人に知らせる番」と表示する。
    ★数えるのは道具の側★＝手順書に回数を書くと、いつか合わなくなる。
    """
    data = _load(path)
    hit = next((i for i in data["issues"] if i.get("id") == args.id), None)
    if hit is None:
        print(f"★#{args.id} は台帳にありません★")
        return 1
    if hit.get("status") != "open":
        print(f"#{args.id} はすでに解決済みです（やり直しは要りません）")
        return 0
    hit["attempts"] = int(hit.get("attempts") or 0) + 1
    hit["last_seen"] = _today()
    if args.note:
        notes = hit.setdefault("attempt_notes", [])
        notes.append(f"{_today()}: {args.note}")
        del notes[:-ASK_MAX_ATTEMPTS]      # 直近ぶんだけ残す
    n = hit["attempts"]
    if n >= ASK_MAX_ATTEMPTS:
        hit["needs_notify"] = True        # ★送るまで残す印★
    _save(path, data)
    print(f"#{args.id} やり直し {n} 回目 / 上限 {ASK_MAX_ATTEMPTS}")
    if n >= ASK_MAX_ATTEMPTS:
        # ★ここではメールを送らない★（送るのはタスク側。台帳は台帳の仕事だけ）
        print(f"★NOTIFY_HUMAN★ {ASK_MAX_ATTEMPTS}回やって決まりませんでした。"
              "人に知らせて、送れたら notified --id で印を付けてください")
        return 0
    print("まだ自分でやり直します（材料を変えて次の回へ）")
    return 0


def cmd_notified(path, args):
    """★メールを送れたときだけ呼ぶ★（2026-08-12・依頼164のP1）

    送信の成否を確かめずに自動の輪から外すと、
    送れなかった質問がどこからも見えなくなる。
    """
    data = _load(path)
    hit = next((i for i in data["issues"] if i.get("id") == args.id), None)
    if hit is None:
        print(f"★#{args.id} は台帳にありません★")
        return 1
    hit["notified_at"] = _today()
    hit.pop("needs_notify", None)
    _save(path, data)
    print(f"#{args.id} 人へ知らせ済みにしました（自動では拾いません）")
    return 0


def cmd_notifications(path, args):
    """★まだ知らせていない質問★（メール送信に失敗しても消えないための一覧）"""
    data = _load(path)
    items = [i for i in data["issues"]
             if i.get("status") == "open" and i.get("needs_notify")]
    if not items:
        print("知らせるべき質問はありません")
        return 0
    for it in items:
        print(f"#{it['id']} [{it['slug']}] {it['title']}")
        for line in str(it.get("detail") or "").splitlines():
            print(f"      {line}")
        for note in (it.get("attempt_notes") or []):
            print(f"      ・試したこと: {note}")
    return 0


def cmd_questions(path, args):
    """未回答の質問を1件だけ出す（無ければ何も出さない＝タスクは次へ進む）。"""
    items = open_questions(path)
    if not items:
        print("2AIに聞くことはありません")
        return 0
    for it in items[:max(1, int(getattr(args, "limit", 1) or 1))]:
        print(f"#{it['id']} [{it['slug']}] {it['title']}")
        for line in str(it.get("detail") or "").splitlines():
            print(f"      {line}")
        for note in (it.get("attempt_notes") or []):
            print(f"      ・試したこと: {note}")
        print(f"      （初出 {it.get('first_seen')}・経過{_days_open(it)}日"
              f"・やり直し{int(it.get('attempts') or 0)}回"
              f"／上限{ASK_MAX_ATTEMPTS}回）")
    return 0


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

    # ★未回答の「2AIに聞くこと」を取り出す★（2026-08-12・人を中継役にしない）
    p = sub.add_parser("questions", help="まだ答えが出ていない2AIへの質問")
    p.add_argument("--limit", type=int, default=1)

    # ★やり直した回数を数える★（3回目で人に知らせる）
    p = sub.add_parser("attempt", help="決まらなかった質問のやり直し回数を+1する")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--note", default="", help="何を試したか（短く）")

    # ★送れたときだけ印を付ける★（送信の成否を確かめずに輪から外さない）
    sub.add_parser("notifications", help="まだ知らせていない質問")
    p = sub.add_parser("notified", help="メールを送れた質問に印を付ける")
    p.add_argument("--id", type=int, required=True)

    sub.add_parser("digest")

    p = sub.add_parser("close")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--reason", default="")
    p.add_argument("--reason-file", dest="reason_file", default="",
                   help="クローズ理由を書いたファイル（無人タスクはこちら）")

    args = ap.parse_args()
    path = Path(args.file) if args.file else DEFAULT_FILE
    fn = {"add": cmd_add, "list": cmd_list, "digest": cmd_digest, "close": cmd_close,
          "severity": cmd_severity, "blocking": cmd_blocking,
          "questions": cmd_questions, "attempt": cmd_attempt,
          "notified": cmd_notified,
          "notifications": cmd_notifications}[args.cmd]
    sys.exit(fn(path, args))


if __name__ == "__main__":
    raise SystemExit(main())

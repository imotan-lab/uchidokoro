# -*- coding: utf-8 -*-
"""要確認案件の恒久台帳（open_issues.json）操作ツール。

「要手動確認がメール1通に埋もれて放置される」問題の構造対策（2026-06-30設計）。

★★閉じてよいのは、機械が検査をやり直して合格したときだけ★★（2026-08-30）
  入口は scripts/ledger_sweep.py --close。
  2AIが記事を読んで「この検査が全部通れば直っている」と決め、
  機械がその検査を**全部**やり直して、通ったときだけ閉じる。
  ★AIの宣言では閉じない／人を待たせもしない★
  ★どちらでも確かめられない案件は開いたままにする★

★★古い決まりを消した★★（2026-08-30・鉄則0b）＝ここには
  「無人タスクのcloseは原則禁止。例外は verify STEP 2.8 / 2.9 の2つだけ」と
  書いてあったが、★そこに挙がっていた4タスク（new-machine / auto-add /
  verify / quality-review）はすべて削除済み★で、例外の指し先が存在しない。
  しかもこの決まりは「直した報告だけメールして人が閉じる」形＝
  ★人を中継役にする★もので、運営者の決定と正面から食い違っていた。

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
import hashlib
import json
import os
import re
import subprocess
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

    def _stops_ret(fn) -> bool:
        """止まったか（例外でも、0以外を返しても「止まった」）。"""
        try:
            return fn() != 0
        except SystemExit:
            return True

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
            # ★★本番と同じ順で積んでから呼ぶ★★（2026-09-08）
            #   `ledger_once` は控えを読み直すので、
            #   行き詰まりを実際に積まないと積まれない
            #   （＝「3回の判断を通した」の証明になる作りに変わった）。
            # ★★行き詰まりの控えも一時ファイルへ向ける★★
            #   （2026-09-08・Codexの指摘3）
            #   ★直す前は本番の grow_check.json を書き換えていた★＝
            #   無人タスクと同時に走ると、丸ごと読んで書き戻す作りなので
            #   本番の更新が消える。★試験は本番の状態を触らない★。
            _keep_state = _gm.STATE_PATH
            try:
                _gm.STATE_PATH = str(tmp / "grow_check.json")
                for _d in range(1, _gm.STUCK_ASK_LIMIT + 1):
                    _gm.grow_result("s9", False, "理由",
                                    today=f"2026-08-{_d:02d}")
                _gm.ledger_once("s9", "s9: 値が再現できません", "詳細です",
                                "MATERIAL")
            finally:
                _gm.STATE_PATH = _keep_state
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

    # ------------------------------------------------ 閉じる入口を1本にした守り
    # ★★ここが緩むと、確かめずに閉じる道が戻る★★（2026-09-17）
    #   実測＝閉じた414件のうち、機械が確かめて閉じたのは29件だけだった。
    #   ★形の検査は純粋な関数にしてある★＝gitの状態に左右されないので、
    #   手元でもCIでも同じに動く（検査そのものをやり直す部分は
    #   `recheck.closeable` が持っていて、そちらは木が綺麗なことを求める）。
    _row = {"id": 7, "slug": "dmm_5086", "status": "open"}
    _good = {"schema": RECEIPT_SCHEMA, "issue_id": 7, "slug": "dmm_5086",
             "conditions": [{"condition": {"check": "x"},
                             "observation_digest": "abc"}]}
    t("★★形の整った受領証は通る★★（断るだけの守りは、いつか全部断る）",
      _receipt_problems(_good, 7, _row) == "")
    t("　★受領証が無い（None）なら断る★",
      bool(_receipt_problems(None, 7, _row)))
    t("★★版が違う受領証は断る★★（形を変えたのに古い受領証で閉じられる）",
      bool(_receipt_problems(dict(_good, schema="なにか別の版"), 7, _row)))
    t("★★別の案件の受領証では閉じない★★"
      "（1件ぶん確かめて、別の案件を閉じられてしまう）",
      bool(_receipt_problems(dict(_good, issue_id=8), 7, _row)))
    t("★★別の機種の受領証では閉じない★★"
      "（他機種の控えで通してしまう）",
      bool(_receipt_problems(dict(_good, slug="hokuto"), 7, _row)))
    t("★★検査が1件も書かれていない受領証では閉じない★★（空で閉じない）",
      bool(_receipt_problems(dict(_good, conditions=[]), 7, _row)))
    t("★★確かめた時の指紋が無い受領証では閉じない★★"
      "（台帳の外にある控えは、コミットの照合では覆えない）",
      bool(_receipt_problems(
          dict(_good, conditions=[{"condition": {"check": "x"}}]), 7, _row)))

    # ★★案件と検査を構造で結び付ける★★（2026-09-17・Codexの指摘）
    #   ★直す前★＝閉じる側は番号・機種・状態しか見ていなかったので、
    #   ★その案件と何の関係もない検査でも、通りさえすれば閉じられた★。
    # ★本物の証拠つきで組み立てる★（閉じる側が証拠まで見るため）
    _headsha = subprocess.run(["git", "rev-parse", "HEAD"],
                              cwd=str(Path(__file__).resolve().parent.parent),
                              capture_output=True, text=True).stdout.strip()

    def _cond(check="confirmed_value_recorded", version=1, **a):
        return {"check": check, "version": version, "args": dict(a),
                "result_when_set": "FAIL", "failed_at_commit": _headsha,
                "failed_digest": "e" * 64}

    def _rcpt(check="confirmed_value_recorded", version=1, **a):
        return {"condition": _cond(check, version, **a),
                "observation_digest": "d"}

    def _seal(row):
        """★封（これで全部を覆ったという2AIの宣言）を付けた写しを返す★"""
        r = dict(row)
        sl = str(r.get("slug") or "")
        r["conditions_sealed"] = {
            "at": "2026-09-17", "by": ["claude", "codex"],
            "why": "この条件で案件の全部を覆いました（試験）",
            "issue_digest": issue_digest(r),
            "condition_keys": sorted(
                json.dumps(_cond_key(c, sl), ensure_ascii=False)
                for c in row_conditions(r))}
        return r

    _bound = _seal(dict(_row, resolution_conditions=[
        _cond(field="gameplay#normal_cz")]))
    _hit = [_rcpt(slug="dmm_5086", field="gameplay#normal_cz")]
    t("★★条件が1件も登録されていない案件は閉じない★★"
      "（登録が無ければ素通りだった＝台帳の全件がそうだった）",
      bool(_condition_binds_row(_row, _hit)))
    t("★★登録した条件と同じ検査・同じ引数なら通る★★",
      _condition_binds_row(_bound, _hit) == "")
    t("★★検査の名前が違えば閉じない★★",
      bool(_condition_binds_row(_bound, [_rcpt("text_gone")])))
    t("★★同じ検査でも、別の項目を見ていたら閉じない★★"
      "（値は記録済みだが、案件の中身は別、という取り違え）",
      bool(_condition_binds_row(
          _bound, [_rcpt(slug="dmm_5086", field="別の項目")])))
    # ★★登録した版と違う版で確かめた受領証では閉じない★★
    #   （2026-09-17・Codexの指摘②）＝直す前は検査名と引数しか見ていなかった。
    t("★★検査の版が上がったのに、登録し直さずに閉じない★★"
      "（「中身を読み直して登録し直す」が守られなくても止まらなかった）",
      bool(_condition_binds_row(
          _seal(dict(_row, resolution_conditions=[
              _cond(version=2, field="gameplay#normal_cz")])), _hit)))
    t("★★機種は案件の行から固定する★★"
      "（受領証の自己申告だと、別の機種の控えで通せる）",
      bool(_condition_binds_row(
          _bound, [_rcpt(slug="hokuto", field="gameplay#normal_cz")])))
    # ★★条件の側が機種を名乗っていても、案件の行が勝つ★★
    _sneak = _seal(dict(_row, resolution_conditions=[
        _cond(slug="hokuto", field="gameplay#normal_cz")]))
    t("★★登録した条件が別の機種を名乗っていても、案件の機種で照合する★★",
      _condition_binds_row(
          _sneak, [_rcpt(slug="dmm_5086", field="gameplay#normal_cz")]) == ""
      and bool(_condition_binds_row(
          _sneak, [_rcpt(slug="hokuto", field="gameplay#normal_cz")])))
    # ★★登録した条件は「全部」通す★★（2026-09-17・Codexの指摘①・再現済み）
    #   ★片方だけ登録して片方の検査だけ渡せば閉じられた★
    #   ＝1つの案件に問題が2つ書いてあるとき（#284の型）に取りこぼす。
    _multi = _seal(dict(_row, resolution_conditions=[
        _cond(field="gameplay#normal_cz"),
        _cond("text_gone", 1, text="消えるべき文"),
    ]))
    t("★★登録した条件が2件あるとき、1件だけでは閉じない★★"
      "（問題が2つある案件を、片方の検査だけで閉じられた）",
      bool(_condition_binds_row(_multi, _hit)))
    t("　★2件そろえば通る★",
      _condition_binds_row(_multi, _hit + [
          _rcpt("text_gone", 1, slug="dmm_5086", text="消えるべき文")]) == "")

    # ★★「これで案件の全部を覆った」という封★★（2026-09-17・Codexの指摘）
    #   ★条件が1件あるだけでは、案件に書かれた問題を全部登録したことにならない★
    #   ＝「型式名／ヤメ時」の案件に型式名だけ登録し、型式名だけ直せば閉じられた。
    #   ★いくつ問題があるかは文章を読んで決めること＝機械にはやらせない★。
    #   機械は「2AIが覆ったと言ったか」「そのあと動いていないか」だけを見る。
    _nosealed = dict(_row, resolution_conditions=[
        _cond(field="gameplay#normal_cz")])
    t("★★封が無ければ閉じない★★"
      "（条件を1件だけ登録して、片方の問題を直さずに閉じられた）",
      bool(_condition_binds_row(_nosealed, _hit)))
    t("　★封があれば通る★", _condition_binds_row(_seal(_nosealed), _hit) == "")
    _moved = _seal(_nosealed)
    _moved["detail"] = "封をしたあとで書き換えた詳細"
    t("★★封をしたあと案件の本文が書き換わったら、封を無効にする★★"
      "（覆っているかどうかが分からなくなるため）",
      bool(_condition_binds_row(_moved, _hit)))
    _added = _seal(_nosealed)
    _added["resolution_conditions"] = list(_added["resolution_conditions"]) + [
        _cond("text_gone", 1, text="あとから足した")]
    t("★★封をしたあと条件を足したら、封を無効にする★★"
      "（封の時点と違う顔ぶれで閉じられる）"
      "／★足した条件を受領証が確かめていても断る★"
      "（隣の守りに助けられていないこと・罠④）",
      bool(_condition_binds_row(_added, _hit + [
          _rcpt("text_gone", 1, slug="dmm_5086", text="あとから足した")])))
    t("★★封はあるが条件が空なら閉じない★★"
      "（条件が無いことの検査を消しても、封の検査は素通りする＝罠④）",
      bool(_condition_binds_row(_seal(dict(_row,
                                           resolution_conditions=[])), _hit)))

    # ★★「2AIが決めた」を、1AIでは名乗れない★★（2026-09-17・Codexの指摘）
    #   ★直す前は「2つあればよい」だった★ので `--by claude,claude` が通り、
    #   ★1AIだけで2AIの宣言を作れた★。
    t("★★同じ判断者を2つ並べても通さない★★（--by claude,claude）",
      bool(judges_problem(["claude", "claude"])))
    t("　★大文字小文字の違いでは別人にならない★",
      bool(judges_problem(["Claude", "CLAUDE"])))
    t("　★claude と codex の両方なら通る★",
      judges_problem(["Claude", "Codex"]) == "")
    t("　★契約に無い名前は通さない★", bool(judges_problem(["claude", "gpt"])))
    _badby = _seal(_nosealed)
    _badby["conditions_sealed"]["by"] = ["claude", "claude"]
    t("★★閉じる側でも封の判断者を見る★★"
      "（指紋だけを持つ辞書でも封として通っていた）",
      bool(_condition_binds_row(_badby, _hit)))
    _nowhy = _seal(_nosealed)
    _nowhy["conditions_sealed"]["why"] = "短い"
    t("　★封に理由が書かれていなければ通さない★",
      bool(_condition_binds_row(_nowhy, _hit)))
    t("　★封の中身が辞書ですらなければ通さない★",
      bool(seal_problem(dict(_row, conditions_sealed="ただの文字列"))))

    # ★★「確かに落ちていた」証拠を、閉じるときにも見る★★
    #   ★直す前は登録のときに記録するだけだった★ので、
    #   証拠を持たない古い条件に封を付ければ、そのまま閉じられた。
    t("★★落ちていた記録が無い条件では閉じない★★",
      bool(evidence_problem({"check": "x"})))
    t("　★落ちていたコミットが無ければ通さない★",
      bool(evidence_problem({"result_when_set": "FAIL",
                             "failed_digest": "e" * 64})))
    t("　★落ちていた指紋が無ければ通さない★",
      bool(evidence_problem({"result_when_set": "FAIL",
                            "failed_at_commit": _headsha})))
    t("　★そろっていれば通る★",
      evidence_problem({"result_when_set": "FAIL",
                        "failed_at_commit": _headsha,
                        "failed_digest": "e" * 64}) == "")
    _old = _seal(dict(_row, resolution_conditions=[
        {"check": "confirmed_value_recorded", "version": 1,
         "args": {"field": "gameplay#normal_cz"}}]))
    t("★★証拠を持たない古い条件は、封を付けても閉じられない★★",
      bool(_condition_binds_row(_old, _hit)))
    # ★★狙った1件だけが効く形で見る★★（罠④＝隣の守りに助けられない）
    #   コミットは正しい歴史の中にあるが、落ちていた記録だけが無い形。
    _noresult = _seal(dict(_row, resolution_conditions=[
        {"check": "confirmed_value_recorded", "version": 1,
         "args": {"field": "gameplay#normal_cz"},
         "failed_at_commit": _headsha, "failed_digest": "e" * 64}]))
    t("　★落ちていた記録だけが無くても閉じない★"
      "（コミットの祖先の検査に助けられていないこと）",
      bool(_condition_binds_row(_noresult, _hit)))
    _other = _seal(dict(_row, resolution_conditions=[
        _cond(field="gameplay#normal_cz")]))
    _other["resolution_conditions"][0]["failed_at_commit"] = "0" * 40
    t("★★別の枝で落としたコミットは証拠にしない★★"
      "（いまの歴史の中に無いものを持ってこられる）",
      bool(_condition_binds_row(_other, _hit)))
    t("　★いまの歴史にあるコミットなら通る★", is_ancestor(_headsha) is True)
    t("　★40桁でないものは通さない★", is_ancestor("abc") is False)
    # ★★案内は「そのまま打てる形」であること★★（2026-09-17・Codexの指摘）
    #   ★私の試験は「共有元が各出口に入っているか」しか見ていなかった★ので、
    #   ★共有元そのものが間違っていても全部通った★
    #   （実際、最後の1つが `ledger_sweep.py --slug …` で
    #     `python scripts/` が抜けており、そのまま打つと動かなかった）。
    #   ★形を機械で固定する★＝道具の名前を書いたら、必ず
    #   「python scripts/<実在するファイル>」の形で書く。
    _SCR = Path(__file__).resolve().parent
    for _txt, _nm in ((REPAIR_STEPS, "直し方"), (SEAL_AGAIN, "封のやり直し")):
        _cmds = re.findall(r"\S+\.py", _txt)
        _bad = [c for c in _cmds
                if not c.startswith("scripts/")
                or not (_SCR / c.split("/", 1)[1]).is_file()
                or ("python " + c) not in _txt]
        t(f"★★{_nm}の案内は、そのまま打てる形で書く★★"
          f"（打てない案内は、無人タスクをその場で止める）"
          + (f"／★打てない: {_bad}★" if _bad else ""),
          bool(_cmds) and not _bad)
        t(f"　★{_nm}の案内は python から始まる★",
          all(x.startswith("python scripts/")
              for x in re.findall(r"python \S+\.py", _txt))
          and _txt.count(".py") == _txt.count("python scripts/"))

    # ★★壊れた条件を黙って捨てない★★（2026-09-17・Codexの補足）
    #   ★直す前は辞書でない要素を落として読んでいた★ので、
    #   壊れた要素が1つ混ざっていても残りだけで閉じられた（fail-open）。
    t("★★条件の一覧に壊れた要素が混ざっていたら閉じない★★",
      bool(conditions_broken({"resolution_conditions": [
          {"check": "x"}, "壊れた要素"]})))
    t("　★一覧ですらなければ閉じない★",
      bool(conditions_broken({"resolution_conditions": "ただの文字列"})))
    t("　★そろっていれば通る★",
      conditions_broken({"resolution_conditions": [{"check": "x"}]}) == "")
    t("　★登録が無い案件は、ここでは何も言わない★"
      "（「1件も登録されていません」の側が言う）",
      conditions_broken({}) == "")
    _brk = _seal(_nosealed)
    _brk["resolution_conditions"] = list(_brk["resolution_conditions"]) \
        + ["壊れた要素"]
    t("★★閉じる側でも、壊れた条件の一覧を断る★★",
      bool(_condition_binds_row(_brk, _hit)))

    # -------------------------------------- 登録の関門（いま落ちていること）
    # ★★案件の説明文そのものを条件にできた★★（2026-09-17・Codexの指摘・再現済み）
    #   案件の詳細が「ヤメ時の説明が読みづらい」なら、その文を逐語にできる。
    #   ★その文はもともと記事に無いので、text_gone が必ず通る★
    #   ＝ヤメ時を1文字も直さずに閉じられた。
    # ★登録の時点で落ちていることを求めれば、この形はここで落ちる★
    _dreg = tempfile.mkdtemp(prefix="open_issues_cond_test_")
    _keep_roots3 = TEXT_ROOTS
    try:
        globals()["TEXT_ROOTS"] = (Path(_dreg),)
        _cwhy = Path(_dreg) / "why.txt"
        _cwhy.write_text("この逐語が消えていれば直っています\n",
                         encoding="utf-8")
        _cled = Path(_dreg) / "open_issues.json"

        def _fresh():
            _cled.write_text(json.dumps({"next_id": 2, "issues": [
                {"id": 1, "slug": "zz_no_such_machine", "kind": "quality",
                 "source": "manual", "status": "open", "title": "試験用",
                 "detail": "試験用", "severity": "CRITICAL",
                 "first_seen": "2026-09-01", "last_seen": "2026-09-01"}]},
                ensure_ascii=False), encoding="utf-8")

        class _C:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        def _reg(**kw):
            a = dict(id=1, check="text_gone", arg=["text=どこにも無い文XYZ"],
                     why="", why_file=str(_cwhy), by="claude,codex")
            a.update(kw)
            return cmd_condition(_cled, _C(**a))

        def _n():
            return len(json.loads(_cled.read_text(encoding="utf-8"))
                       ["issues"][0].get("resolution_conditions") or [])

        # ★木の状態を作って両方向を見る★（罠④＝隣の守りに助けられない）
        #   手元は汚れている／CIと写しは綺麗、で通る道が変わるので、
        #   どちらの道も必ず試す。
        import contextlib as _ctx4
        import io as _io4
        import recheck as _rcT

        def _reg_says(clean, **kw):
            _keep_c = _rcT.repo_clean
            _b = _io4.StringIO()
            try:
                _rcT.repo_clean = lambda: clean
                with _ctx4.redirect_stdout(_b):
                    _r = _reg(**kw)
            finally:
                _rcT.repo_clean = _keep_c
            return _r, _b.getvalue()

        _fresh()
        _rA, _mA = _reg_says(False)
        t("★★未コミットのままでは条件を登録できない★★"
          "（一時の書き換えで検査を落として登録し、戻せば何も直さずに閉じられた）",
          _rA != 0 and _n() == 0 and "未コミットの変更があります" in _mA)
        _fresh()
        _rB, _mB = _reg_says(True)
        t("★★いま落ちていない検査は条件として登録できない★★"
          "（案件の説明文そのものを逐語にすると、記事に無いので必ず通る）",
          _rB != 0 and _n() == 0 and "いま落ちていません" in _mB)
        _fresh()
        _rC, _mC = _reg_says(True, arg=["text="])
        t("　★判定できないもの（空の逐語）も登録できない★"
          "（PASSでなければよい、にすると通ってしまう）",
          _rC != 0 and _n() == 0 and "いま落ちていません" in _mC)

        # ★★条件を足したら、前の封を外す★★（そろっていないのに閉じられる）
        _reg_out = []          # ★直前の呼び出しが何を言ったか★

        def _reg_ok(**kw):
            """★検査を落ちた扱いにして、書き込む道だけを通す★"""
            _keep_c, _keep_r = _rcT.repo_clean, _rcT.run
            _b = _io4.StringIO()
            try:
                _rcT.repo_clean = lambda: True
                _rcT.run = lambda c, a: {"result": _rcT.FAIL, "detail": "試験",
                                         "observation_digest": "d" * 64}
                with _ctx4.redirect_stdout(_b):
                    return _reg(**kw)
            finally:
                _rcT.repo_clean, _rcT.run = _keep_c, _keep_r
                _reg_out[:] = [_b.getvalue()]

        _fresh()
        _reg_ok()
        _row2 = json.loads(_cled.read_text(encoding="utf-8"))["issues"][0]
        t("　★登録できた条件には、落ちていたコミットが残る★",
          bool((_row2.get("resolution_conditions") or [{}])[0]
               .get("failed_at_commit")))
        _row2["conditions_sealed"] = {"at": "2026-09-17"}
        _cled.write_text(json.dumps({"next_id": 2, "issues": [_row2]},
                                    ensure_ascii=False), encoding="utf-8")
        _reg_ok(arg=["text=もう1つ別の逐語"])
        _row3 = json.loads(_cled.read_text(encoding="utf-8"))["issues"][0]
        t("★★封をしたあと条件を足したら、封そのものを外す★★"
          "（そろっていないのに閉じられる状態へ戻る）",
          _row3.get("conditions_sealed") is None
          and len(_row3.get("resolution_conditions") or []) == 2)

        # ★★証拠を持たない古い条件は、登録し直せる★★
        #   ★直す前は「同じ条件です」で終わっていた★ので、
        #   ★汚れた木で登録した条件を、道具からは直せなかった★。
        _fresh()
        _r4 = json.loads(_cled.read_text(encoding="utf-8"))["issues"][0]
        _r4["resolution_conditions"] = [
            {"check": "text_gone", "version": 1,
             "args": {"text": "どこにも無い文XYZ"}}]      # ★証拠なし★
        _r4["conditions_sealed"] = {"at": "2026-09-17"}
        _cled.write_text(json.dumps({"next_id": 2, "issues": [_r4]},
                                    ensure_ascii=False), encoding="utf-8")
        _reg_ok()
        _r5 = json.loads(_cled.read_text(encoding="utf-8"))["issues"][0]
        _got5 = (_r5.get("resolution_conditions") or [{}])[0]
        t("★★証拠を持たない古い条件は、いまの証拠で上書きできる★★"
          "（できないと、汚れた木で登録した条件を道具から直せない）",
          len(_r5.get("resolution_conditions") or []) == 1
          and evidence_problem(_got5) == ""
          and _r5.get("conditions_sealed") is None)

        # ★★形はそろっているが、いまの歴史に無いコミットの条件★★
        #   （2026-09-17・Codexの2回目の指摘＝rebase や別の枝のあと）
        #   ★閉じる側は断り、登録し直す側は「もう登録されています」で更新しない★
        #   ＝人が台帳を手で直すまで**永久に閉じられなかった**。
        _fresh()
        _r6 = json.loads(_cled.read_text(encoding="utf-8"))["issues"][0]
        _r6["resolution_conditions"] = [
            {"check": "text_gone", "version": 1,
             "args": {"text": "どこにも無い文XYZ"},
             "result_when_set": "FAIL",
             "failed_at_commit": "0" * 40,      # ★形は正しいが歴史に無い★
             "failed_digest": "f" * 64}]
        _r6["conditions_sealed"] = {"at": "2026-09-17"}
        _cled.write_text(json.dumps({"next_id": 2, "issues": [_r6]},
                                    ensure_ascii=False), encoding="utf-8")
        _reg_ok()
        _r7 = json.loads(_cled.read_text(encoding="utf-8"))["issues"][0]
        _got7 = (_r7.get("resolution_conditions") or [{}])[0]
        t("★★いまの歴史に無いコミットの条件も、登録し直せる★★"
          "（できないと、閉じるときは断られ、直すこともできず永久に詰まる）",
          len(_r7.get("resolution_conditions") or []) == 1
          and str(_got7.get("failed_at_commit") or "") != "0" * 40
          and is_ancestor(str(_got7.get("failed_at_commit") or ""))
          and _r7.get("conditions_sealed") is None)

        # ★★詰まった案件を、道具だけで直して閉じられるところまで見る★★
        #   （2026-09-17・Codexの指摘＝断るだけで直す道が無かった）
        #   ★ここが往復の試験★＝「拒否できる」だけでは足りない。
        def _reset(**kw):
            a = dict(id=1, why="", why_file=str(_cwhy), by="claude,codex")
            a.update(kw)
            _b = _io4.StringIO()
            with _ctx4.redirect_stdout(_b):
                return cmd_condition_reset(_cled, _C(**a))

        for _name, _broken in (
                ("壊れた要素が混ざった一覧", ["ただの文字列"]),
                ("名簿から消えた検査", [{"check": "もう無い検査XYZ",
                                        "version": 1, "args": {},
                                        "result_when_set": "FAIL",
                                        "failed_at_commit": _headsha,
                                        "failed_digest": "f" * 64}]),
                ("版が上がった条件", [{"check": "text_gone", "version": 99,
                                      "args": {"text": "どこにも無い文XYZ"},
                                      "result_when_set": "FAIL",
                                      "failed_at_commit": _headsha,
                                      "failed_digest": "f" * 64}]),
        ):
            _fresh()
            _rw = json.loads(_cled.read_text(encoding="utf-8"))["issues"][0]
            _rw["resolution_conditions"] = _broken
            _rw["conditions_sealed"] = {
                "at": "2026-09-17", "by": ["claude", "codex"],
                "why": "この条件で案件の全部を覆いました（試験）",
                "issue_digest": issue_digest(_rw), "condition_keys": []}
            _cled.write_text(json.dumps({"next_id": 2, "issues": [_rw]},
                                        ensure_ascii=False), encoding="utf-8")
            _stuck = bool(_condition_binds_row(
                json.loads(_cled.read_text(encoding="utf-8"))["issues"][0],
                _hit))
            # ★白紙に戻す前に、登録の道具がどう振る舞うかを見る★
            #   ①落ちない（落ちると、白紙に戻す前の段階で手が止まる）
            #   ②★壊れた一覧のままでは登録しない★（残ると結局閉じられない）
            #   ③★そのとき直し方まで言う★（言わないと同じ輪に戻る）
            try:
                _rc0 = _reg_ok()
                _blew0 = ""
            except Exception as e:                           # noqa: BLE001
                _rc0, _blew0 = 0, f"{type(e).__name__}: {e}"
            _m0 = (_reg_out or [""])[0]
            _n0 = len(json.loads(_cled.read_text(encoding="utf-8"))
                      ["issues"][0].get("resolution_conditions") or [])
            if _name == "壊れた要素が混ざった一覧":
                t("★★壊れた一覧のままでは登録しない★★"
                  "（登録できても壊れた要素が残り、閉じる側は結局ずっと断る）"
                  + (f"／★落ちた: {_blew0[:60]}★" if _blew0 else ""),
                  not _blew0 and _rc0 != 0 and _n0 == 1
                  and REPAIR_STEPS in _m0)
            else:
                t(f"　★{_name}のままでも、登録の道具は落ちない★"
                  + (f"／★落ちた: {_blew0[:60]}★" if _blew0 else ""),
                  not _blew0)
            # ★封を戻してから白紙にする★＝登録し直すと封は外れるので、
            #   そのままだと「白紙にすると封が消える」を試験できない（罠④）
            _pre = json.loads(_cled.read_text(encoding="utf-8"))["issues"][0]
            _pre["conditions_sealed"] = {"at": "2026-09-17"}
            _cled.write_text(json.dumps({"next_id": 2, "issues": [_pre]},
                                        ensure_ascii=False), encoding="utf-8")
            _reset()
            # ★白紙に戻した直後を見る★（あとで見ると、登録し直す側が
            #   封を外すので、白紙側の守りを壊しても気づけない＝罠④）
            _mid = json.loads(_cled.read_text(encoding="utf-8"))["issues"][0]
            _rec = (_mid.get("condition_resets") or [{}])[0]
            t(f"　★{_name}を白紙に戻すと、条件も封も消える★",
              _mid.get("resolution_conditions") is None
              and _mid.get("conditions_sealed") is None)
            t(f"　★{_name}の中身を控えに残す★（何を消したか追えなくなる）",
              isinstance(_rec.get("dropped"), list)
              and all(x in _rec["dropped"] for x in _broken)
              and judges_problem(_rec.get("by")) == "")
            # ★登録し直す道具が落ちたら、それは「直せる」ではない★
            try:
                _reg_ok()
                _blew = ""
            except Exception as e:                           # noqa: BLE001
                _blew = f"{type(e).__name__}: {e}"
            _after = json.loads(_cled.read_text(encoding="utf-8"))["issues"][0]
            _conds = _after.get("resolution_conditions") or []
            t(f"★★{_name}でも、道具だけで直して登録し直せる★★"
              "（直す道が無いと、人がJSONを触るまで永久に詰まる）"
              + (f"／★落ちた: {_blew[:60]}★" if _blew else ""),
              _stuck and not _blew and len(_conds) == 1
              and evidence_problem(_conds[0]) == ""
              and _after.get("conditions_sealed") is None)
        _fresh()
        t("　★判断者が2つ無ければ登録できない★",
          _stops_ret(lambda: _reg(by="claude")) and _n() == 0)
        _fresh()
        t("　★観測どまりの検査は登録できない★",
          _stops_ret(lambda: _reg(check="strategy_vs_checker", arg=[]))
          and _n() == 0)
    finally:
        globals()["TEXT_ROOTS"] = _keep_roots3
        shutil.rmtree(_dreg, ignore_errors=True)

    # ★★機種を取らない検査にも、条件を登録できる★★（2026-09-17・自分で見つけた）
    #   ★直す前は機種を必ず足していた★ので、
    #   ★機種に紐づかない案件（site / env）は条件を登録すらできなかった★
    #   ＝実測で開いている281件のうち111件（4割）がそこに当たる。
    t("★★機種を取らない検査に、機種を渡さない★★"
      "（渡すと「知らない引数です」で、機種に紐づかない案件が永久に閉じられない）",
      pin_slug("guard_proven", {"mutation_why": "x"}, "site")
      == {"mutation_why": "x"})
    t("　★機種を取る検査には、行の機種を入れる★",
      pin_slug("text_gone", {"text": "x"}, "hokuto")
      == {"text": "x", "slug": "hokuto"})
    t("　★機種を取らない検査に機種が混ざっていたら落とす★"
      "（登録の側から知らない引数を持ち込ませない）",
      pin_slug("guard_proven", {"mutation_why": "x", "slug": "site"}, "site")
      == {"mutation_why": "x"})

    # -------------------------------------- 本物の入口を通す（罠③＝直接呼びだけにしない）
    # ★★関数だけを試すと、呼び出し行を消したときに緑のまま★★
    #   ここでは `cmd_close` / `cmd_attempt` を本当に呼ぶ。
    #   ★本番の台帳は触らない★（罠㉗＝対照実験で本物の案件を1件消した）
    _d2 = tempfile.mkdtemp(prefix="open_issues_close_test_")
    _keep_lock2 = LOCK_PATH
    _keep_roots2 = TEXT_ROOTS
    try:
        # ★理由の文は一時の置き場から読ませる★
        #   （無人の印を立てる試験なので、直接指定は機械が断る＝正しい）
        globals()["TEXT_ROOTS"] = (Path(_d2),)
        _why2 = Path(_d2) / "why.txt"
        _why2.write_text("機械では確かめられないので人の判断で閉じます\n",
                         encoding="utf-8")
        _led = Path(_d2) / "open_issues.json"
        _led.write_text(json.dumps({"next_id": 2, "issues": [
            {"id": 1, "slug": "zz_test", "kind": "external_value",
             "source": "manual", "status": "open", "title": "試験用",
             "detail": "試験用", "severity": "CRITICAL",
             "first_seen": "2026-09-01", "last_seen": "2026-09-01"}]},
            ensure_ascii=False), encoding="utf-8")

        class _A:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        def _st():
            return json.loads(_led.read_text(encoding="utf-8"))["issues"][0]

        _rc0 = cmd_close(_led, _A(id=1, reason="試験のため", reason_file="",
                                  receipt="", owner_decision=False))
        t("★★受領証が無ければ閉じない★★"
          "（確かめずに閉じる裏口＝閉じた414件のうち385件が通っていた道）",
          _rc0 != 0 and _st()["status"] == "open")
        # ★★形の関門を「呼んでいる」ことまで見る★★（罠③）
        #   ★関数だけを直接試すと、呼び出し行を消しても緑のまま★。
        #   ここは読める受領証を渡すので、読み込みの失敗には助けられない。
        _bad_rp = Path(_d2) / "bad_receipt.json"
        _bad_rp.write_text(json.dumps(
            {"schema": RECEIPT_SCHEMA, "issue_id": 999, "slug": "zz_test",
             "conditions": [{"condition": {"check": "text_gone"},
                             "observation_digest": "d"}]},
            ensure_ascii=False), encoding="utf-8")
        #   ★断った「理由の文」まで見る★（罠㉚）＝
        #   奥にも守りがあるので「閉じなかった」だけでは、
        #   この関門を通ったのかどうかが分からない。
        import contextlib as _ctx2
        import io as _io2
        _buf2 = _io2.StringIO()
        with _ctx2.redirect_stdout(_buf2):
            _rc0b = cmd_close(_led, _A(id=1, reason="試験のため",
                                       reason_file="",
                                       receipt=str(_bad_rp),
                                       owner_decision=False))
        _msg2 = _buf2.getvalue()
        t("　★読めるが別の案件の受領証は、形の関門が名指しで断る★"
          "（形の関門を呼んでいることの証明）",
          _rc0b != 0 and _st()["status"] == "open" and "#999" in _msg2)

        # ★無人タスクが動いている最中は、運営者判断の道も使えない★
        globals()["LOCK_PATH"] = Path(_d2) / "task.lock"
        LOCK_PATH.write_text(json.dumps(
            {"task": "zz-task",
             "heartbeat": datetime.datetime.now().isoformat()},
            ensure_ascii=False), encoding="utf-8")
        _rc1 = cmd_close(_led, _A(id=1, reason="",
                                  reason_file=str(_why2), receipt="",
                                  owner_decision=True))
        t("★★無人タスクの最中は、運営者判断の道でも閉じない★★"
          "（自動で回る道に紛れると、機械が確かめた件数が嘘になる）",
          _rc1 != 0 and _st()["status"] == "open")
        LOCK_PATH.unlink()
        _rc2 = cmd_close(_led, _A(id=1, reason="",
                                  reason_file=str(_why2), receipt="",
                                  owner_decision=True))
        t("　★無人が動いていなければ、運営者判断で閉じられる★"
          "（断るだけの守りは、いつか全部断る）",
          _rc2 == 0 and _st()["status"] == "closed")
        t("　★人の手で閉じたことが別の印で残る★"
          "（残らないと、自動化が進んだのか後退したのかが分からない）",
          _st().get("closed_by") == "owner")

        # ------------------------------------------ 回数の数え方
        _led.write_text(json.dumps({"next_id": 2, "issues": [
            {"id": 1, "slug": "zz_test", "kind": "external_value",
             "source": "manual", "status": "open", "title": "試験用",
             "detail": "試験用", "severity": "CRITICAL",
             "first_seen": "2026-09-01", "last_seen": "2026-09-01"}]},
            ensure_ascii=False), encoding="utf-8")
        cmd_attempt(_led, _A(id=1, note="", round_id="r1",
                             outcome="unresolved"))
        cmd_attempt(_led, _A(id=1, note="", round_id="r1",
                             outcome="unresolved"))
        t("★★同じ回は二度数えない★★"
          "（タスクが落ちてやり直しただけで3回に達し、人へ回っていた）",
          int(_st().get("attempts") or 0) == 1)
        cmd_attempt(_led, _A(id=1, note="", round_id="r2", outcome="error"))
        t("★★仕組みの都合で動かせなかった回は数えない★★"
          "（利用制限・時間切れ・ロックだけで人へ回る）",
          int(_st().get("attempts") or 0) == 1
          and int(_st().get("errors") or 0) == 1)
        cmd_attempt(_led, _A(id=1, note="", round_id="r3",
                             outcome="unresolved"))
        t("　★別の回はちゃんと数える★（数えないほうへ倒れていない）",
          int(_st().get("attempts") or 0) == 2)
    finally:
        globals()["LOCK_PATH"] = _keep_lock2
        globals()["TEXT_ROOTS"] = _keep_roots2
        shutil.rmtree(_d2, ignore_errors=True)

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


def run_add(*, source, slug, kind, title, severity, detail="",
            reason_code=None, timeout=60, script=None):
    """★別プロセスで台帳へ登録する唯一の関数★（引数列を作って実行まで行う）

    （2026-08-21・Codexの再指摘）

    ★なぜ「引数列を返すだけ」では足りなかったか★
      add_argv だけだと、呼ぶ側が subprocess を書く。
      ★字面の監査（項目48）では、変数に入れる書き方や単引用符を拾えない★
      とCodexに指摘され、実際そのとおりだった。
      ＝「オプション名を並べない」を**監査で見張る**のではなく、
        **実行ごと1か所に閉じ込める**ほうが確実。

    ★同じプロセスで良いなら add_issue() を直接呼ぶこと★
      別プロセスが要るのは「呼び出し元が落ちても登録は残したい」
      「環境を分けたい」場合だけ。

    戻り値: (成功したか, 出力の抜粋)
    """
    import subprocess as _sp
    argv = add_argv(source=source, slug=slug, kind=kind, title=title,
                    severity=severity, detail=detail,
                    reason_code=reason_code, script=script)
    try:
        r = _sp.run(argv, cwd=os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))),
            capture_output=True, timeout=timeout,
            # ★引数配列＝シェルを通らないので直接指定してよい★（台帳#295）
            env=dict(os.environ, UCHIDOKORO_ARGV_CALL="1"))
    except Exception as e:            # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    out = (r.stdout or b"").decode("utf-8", "replace") \
        + (r.stderr or b"").decode("utf-8", "replace")
    return r.returncode == 0, out[:300]


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

    ★★同じ回を二度数えない★★（2026-09-17・Codexの指摘）
      ★直す前★＝呼ぶたびに増えていたので、
      ★タスクが途中で落ちて同じ晩にやり直しただけで3回に達した★。
      ＝まだ材料を変えて試してもいないのに「人に知らせる番」になる。
      いまは回に名前（--round）を付け、同じ名前は1回しか数えない。

    ★★決まらなかったのか、動かせなかったのかを分ける★★（同）
      `--outcome error`（利用制限・時間切れ・ロック・読めない）は**数えない**。
      仕組みの都合で人へ回してしまうのは、`repair_journal` で既に
      「1回に数えない」と決めた線と同じ。
    """
    data = _load(path)
    hit = next((i for i in data["issues"] if i.get("id") == args.id), None)
    if hit is None:
        print(f"★#{args.id} は台帳にありません★")
        return 1
    if hit.get("status") != "open":
        print(f"#{args.id} はすでに解決済みです（やり直しは要りません）")
        return 0
    outcome = str(getattr(args, "outcome", "") or "unresolved")
    if outcome not in ("unresolved", "error"):
        raise SystemExit("--outcome は unresolved か error です")
    hit["last_seen"] = _today()
    if outcome == "error":
        # ★数えないが、黙って消さない★（何回つまずいたかは残す）
        hit["errors"] = int(hit.get("errors") or 0) + 1
        _save(path, data)
        print(f"#{args.id} 仕組みの都合で動かせませんでした"
              f"（回数に数えません／通算 {hit['errors']} 回）")
        return 0
    rid = str(getattr(args, "round_id", "") or "").strip()
    if not rid:
        raise SystemExit("★--round（この回の名前）が要ります★"
                         "＝同じ回を二度数えないため")
    done = hit.setdefault("attempt_rounds", [])
    if rid in done:
        print(f"#{args.id} この回（{rid}）はもう数えてあります"
              f"（いま {int(hit.get('attempts') or 0)} 回目）")
        _save(path, data)
        return 0
    done.append(rid)
    del done[:-ASK_MAX_ATTEMPTS * 2]
    hit["attempts"] = int(hit.get("attempts") or 0) + 1
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
    print(f"（計{len(items)}件。★閉じるのは機械が確かめたときだけ★＝"
          "python scripts/ledger_sweep.py --slug <機種> --close N "
          "--check <検査名> …）")
    print("（対応方法: このメールをClaude Codeのセッションに貼り付けて「対応して」と伝えるだけでOK。裏取り→修正→closeまで処理されます）")
    return 0


# ---------------------------------------------------------------- 閉じるときの受領証
# ★★閉じる入口を本当に1本にする★★（2026-09-17・Codexの指摘）
#   ★直す前★＝この close は理由の文章だけで閉じていた。
#   ＝機械が検査をやり直す仕組み（ledger_sweep）を**素通りできた**。
#   しかも毎朝のメールが、その素通りする呼び方を案内していた
#   ＝★裏口のほうが広く知られていた★。
#   実測（2026-09-17）＝閉じた414件のうち、機械が確かめて閉じたのは29件だけ。
#
# ★★受領証は「信じる」ものではない★★＝中身は
#   「どの案件を・どの検査で確かめたか」という**申告**でしかない。
#   ★この close は、その検査を自分でもう一度やり直す★（recheck.closeable）。
#   ＝でたらめな受領証を書いても、検査が通らなければ閉じない。
#   ★これは認可ではない★（同じ権限なら誰でもファイルを書ける）。
#   止めているのは「確かめずに閉じること」であって、悪意ではない。
RECEIPT_SCHEMA = "ledger-close-receipt/v1"


def _receipt_problems(rec, issue_id: int, row: dict) -> str:
    """★受領証が、この案件のものとして形を満たしているか★ → 問題の文（無ければ空）

    ★中身の正しさは見ない★＝検査をやり直すのは呼び出し側。
    ここは「どの案件の話か」がずれていないかだけを見る。
    """
    if not isinstance(rec, dict):
        return "受領証がJSONの辞書ではありません"
    if str(rec.get("schema") or "") != RECEIPT_SCHEMA:
        return (f"受領証の版が違います（{rec.get('schema')!r} / "
                f"いま {RECEIPT_SCHEMA}）")
    if rec.get("issue_id") != issue_id:
        return (f"受領証は #{rec.get('issue_id')} のものです"
                f"（閉じようとしているのは #{issue_id}）")
    if str(rec.get("slug") or "") != str(row.get("slug") or ""):
        return (f"受領証の機種は {rec.get('slug')!r} で、"
                f"案件の {row.get('slug')!r} と違います")
    conds = rec.get("conditions")
    if not isinstance(conds, list) or not conds:
        return "受領証に、やり直す検査が1件も書かれていません"
    for c in conds:
        if not isinstance(c, dict) or not isinstance(c.get("condition"), dict):
            return "受領証の検査の書き方が壊れています"
        if not str(c.get("observation_digest") or ""):
            return "受領証に、確かめた時の指紋がありません"
    return ""


# ★★判断者の契約は、確定値の控えと同じものを読む★★（罠③＝2か所に書かない）
#   ★直す前は「2つあればよい」だった★ので `--by claude,claude` が通り、
#   ★1AIだけで「2AIが決めた」ことにできた★（2026-09-17・Codexの指摘）。
def judges_problem(by) -> str:
    """★判断者が claude と codex の両方そろっているか★ → 問題の文（無ければ空）"""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import confirmed_values as _cv
        need = set(_cv.REQUIRED_JUDGES)
    except Exception:                                        # noqa: BLE001
        return "判断者の契約を読めません"
    got = {str(x).strip().casefold() for x in (by or []) if str(x).strip()}
    if got != need:
        return (f"判断者は {'/'.join(sorted(need))} の2つが要ります"
                f"（いまは {','.join(sorted(got)) or 'なし'}）")
    return ""


def evidence_problem(cond: dict) -> str:
    """★その条件が「確かに落ちていた」証拠を持っているか★ → 問題の文

    ★★閉じるときにも見る★★（2026-09-17・Codexの指摘）＝
      ★直す前は登録のときに記録するだけで、閉じる側は見ていなかった★。
      ＝証拠を持たない古い条件に封を付ければ、そのまま閉じられた。
    """
    if str(cond.get("result_when_set") or "") != "FAIL":
        return "登録したときに落ちていた記録がありません"
    if not re.fullmatch(r"[0-9a-f]{40}", str(cond.get("failed_at_commit")
                                             or "")):
        return "落ちていたときのコミットが記録されていません"
    if not re.fullmatch(r"[0-9a-f]{64}", str(cond.get("failed_digest") or "")):
        return "落ちていたときの指紋が記録されていません"
    return ""


def is_ancestor(sha: str, tip: str = "HEAD") -> bool:
    """★そのコミットが、いまの先端の祖先か★（★読めなければ False★）

    ★なぜ要るか★＝「落ちていた状態から直った」と言うには、
      落ちていたコミットが**いまの歴史の中にある**ことが要る。
      別の枝で落としたものを持ってきても証拠にならない。
    """
    if not re.fullmatch(r"[0-9a-f]{40}", str(sha or "")):
        return False
    try:
        r = subprocess.run(["git", "merge-base", "--is-ancestor", sha, tip],
                           cwd=str(Path(__file__).resolve().parent.parent),
                           capture_output=True, text=True, timeout=30)
    except Exception:                                        # noqa: BLE001
        return False
    return r.returncode == 0


def seal_problem(row: dict) -> str:
    """★封そのものが、2AIの宣言として形を満たしているか★ → 問題の文

    ★閉じる側でも見る★（2026-09-17・Codexの指摘）＝
      ★直す前は中身を見ていなかった★ので、指紋だけを持つ辞書でも封として通った。
    """
    seal = row.get("conditions_sealed")
    if not isinstance(seal, dict):
        return ("この案件には「これで全部を覆った」という封がありません。"
                "python scripts/open_issues.py seal --id <番号> "
                "--why-file <理由> --by claude,codex で封をしてください")
    ng = judges_problem(seal.get("by"))
    if ng:
        return "封の" + ng + "。" + SEAL_AGAIN
    if len(str(seal.get("why") or "").strip()) < 10:
        return ("封に、なぜ全部を覆ったと言えるのかが書かれていません。"
                + SEAL_AGAIN)
    return ""


def row_conditions(row: dict) -> list:
    """★その案件に登録されている「閉じる条件」を全部返す★（2026-09-17）

    ★★1件だけでは足りない★★（Codexの指摘・再現済み）＝
      1つの案件に問題が2つ書いてあることがある（実例 #284＝
      「狙い目の逆転」と「句点後の半角スペース」）。
      ★条件を1件しか持てないと、片方だけ登録して片方を直さずに閉じられる★。
      実際に、型式名とヤメ時の2つを書いた案件を、
      型式名の検査だけで閉じられることを確かめた。
    ★全部そろって初めて閉じられる★（足りない検査を呼び出し側が省けない）。
    """
    got = row.get("resolution_conditions")
    return [c for c in got if isinstance(c, dict)] if isinstance(got, list) \
        else []


# ★★詰まったときの直し方は、1か所に書いて全部の出口で使う★★
#   （2026-09-17・Codexの指摘・罠⓸＋罠③）
#   ★直す前★＝白紙に戻す道具は作ったのに、
#   ★詰まりを知らせる文は「登録し直してください」のままだった★。
#   無人タスクは案内どおりに動くので、★同じ輪に戻るだけ★だった
#   （版が上がった条件は、登録し直しても古いほうが残る）。
#   ★★案内は「そのまま打てる形」で書く★★（2026-09-17・Codexの指摘）
#   ★直す前は最後の1つを `ledger_sweep.py --slug …` と書いていた★
#   ＝そのまま打つと**動かない**（`python scripts/` が抜けている）。
#   ★私の試験は「共有元が各出口に入っているか」しか見ていなかった★ので、
#   ★共有元そのものが間違っていても全部通った★（下で形を固定した）。
REPAIR_STEPS = (
    "★直し方★＝①python scripts/open_issues.py condition-reset "
    "--id <番号> --why-file <理由> --by claude,codex"
    " ②python scripts/open_issues.py condition --id <番号> "
    "--check <検査名> --why-file <理由> --by claude,codex"
    " ③python scripts/open_issues.py seal --id <番号> "
    "--why-file <理由> --by claude,codex"
    " ④python scripts/ledger_sweep.py --slug <機種> --close <番号>"
)

# ★封がずれただけのときは、封をし直すだけでよい★（白紙に戻す必要がない）
SEAL_AGAIN = ("★直し方★＝python scripts/open_issues.py seal --id <番号> "
              "--why-file <理由> --by claude,codex")


def conditions_broken(row: dict) -> str:
    """★条件の入れ物そのものが壊れていないか★ → 問題の文（無ければ空）

    ★★黙って捨てない★★（2026-09-17・Codexの補足）＝
      `row_conditions` は辞書でない要素を落として読むので、
      ★壊れた要素が1つ混ざっていても、残りだけで閉じられた★。
      ＝壊れているのに「そろっている」と読む形（fail-open）。
    """
    got = row.get("resolution_conditions")
    if got is None:
        return ""
    if not isinstance(got, list):
        return ("閉じる条件の入れ物が壊れています（一覧ではありません）。"
                + REPAIR_STEPS)
    bad = [i for i, c in enumerate(got) if not isinstance(c, dict)]
    if bad:
        return (f"閉じる条件の {len(bad)} 件が壊れています"
                f"（{bad[:3]} 番目）。" + REPAIR_STEPS)
    return ""


def pin_slug(check: str, args: dict, slug: str) -> dict:
    """★機種を案件の行から固定する★（ただし機種を取る検査だけ）（2026-09-17）

    ★★なぜ「だけ」か★★＝機種を取らない検査がある。
      `guard_proven`（機械の中身を直したことを、守りを1行壊して証明する）は
      壊し方の名前しか取らない。
      ★直す前は機種を必ず足していた★ので、
      ★機種に紐づかない案件（site / env）は条件を登録すらできなかった★
      ＝実測で開いている281件のうち111件（4割）がそこに当たる。
      それらは機械の中身の話なので、`guard_proven` が唯一の道だった。
    ★同じ規則を2か所に書かない★＝台帳側も台帳を閉じる側もこれを通す。
    """
    a = dict(args or {})
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import recheck as _rc
    except Exception:                                        # noqa: BLE001
        return a
    meta = _rc.CHECKS.get(check) or {}
    if "slug" in (meta.get("args_spec") or {}):
        a["slug"] = slug
    else:
        a.pop("slug", None)          # ★取らない検査には渡さない★
    return a


def _cond_key(cond: dict, slug: str = "") -> tuple:
    """★条件を見比べるための鍵★

    ★`slug` を渡すと、その機種で固定する★＝**登録した条件の側だけ**に使う。
    ★受領証の側は、そのまま見る★（2026-09-17）＝
      あちらは「実際に何を動かしたか」なので、こちらで書き換えてしまうと
      ★別の機種で動かした結果を、この案件の証拠として受け取ってしまう★。
    """
    a = dict(cond.get("args") or {})
    if slug:
        a = pin_slug(str(cond.get("check") or ""), a, slug)
    return (str(cond.get("check") or ""), cond.get("version"),
            json.dumps(a, ensure_ascii=False, sort_keys=True))


def _condition_binds_row(row: dict, conds: list) -> str:
    """★案件に登録した条件を**全部**通したか★ → 問題の文（無ければ空）

    （2026-09-17・Codexの指摘＝案件と検査引数を構造的に結合する）
    ★何が起きていたか★＝閉じる側は番号・機種・状態しか見ていなかったので、
    ★その案件と何の関係もない検査でも、通りさえすれば閉じられた★。
    実例＝ある機種の値は先月そろっていたが、案件の中身は
    「表記ゆれを記録できない**仕組み**」だった
    ＝値が記録されている検査は通るが、★案件は直っていない★。

    ★機種は案件の行から取る★（受領証に書かせない）＝
    自己申告にすると、別の機種の控えで通してしまう。
    ★★条件が1件も無ければ閉じない★★＝
      「登録があれば照合する」だけにしていたので、
      ★登録の無い案件は素通りだった★（台帳の全件がそうだった）。
    """
    slug = str(row.get("slug") or "")
    ng = conditions_broken(row)          # ★壊れた要素を黙って捨てない★
    if ng:
        return ng
    want = row_conditions(row)
    if not want:
        return ("この案件には、閉じる条件が1件も登録されていません。"
                "python scripts/open_issues.py condition --id <番号> "
                "--check <検査名> … で先に登録してください")
    # ★★「これで案件の全部を覆った」という封があること★★
    #   （2026-09-17・Codexの指摘＝条件が1件あるだけでは、
    #     案件に書かれた問題を全部登録したことにならない）
    ng = seal_problem(row)
    if ng:
        return ng
    # ★★条件が「確かに落ちていた」証拠を持っているか★★（閉じるときにも見る）
    for w in want:
        ng = evidence_problem(w)
        if ng:
            return (f"登録した条件（{w.get('check')}）は{ng}。"
                    + REPAIR_STEPS)
        if not is_ancestor(str(w.get("failed_at_commit") or "")):
            return (f"登録した条件（{w.get('check')}）が落ちていたコミットは、"
                    "いまの歴史の中にありません"
                    "（別の枝で落としたものは証拠になりません）。"
                    + REPAIR_STEPS)
    seal = row.get("conditions_sealed")
    if str(seal.get("issue_digest") or "") != issue_digest(row):
        return ("封をしたあとに案件の本文が書き換わっています"
                "（覆っているか分からないので、封をし直してください）。"
                + SEAL_AGAIN)
    keys = sorted(json.dumps(_cond_key(c, slug), ensure_ascii=False)
                  for c in want)
    if list(seal.get("condition_keys") or []) != keys:
        return ("封をしたときの条件と、いまの条件が違います"
                "（封をし直してください）。" + SEAL_AGAIN)
    have = {_cond_key(c.get("condition") or {}) for c in conds
            if isinstance(c, dict)}          # ★受領証はそのまま見る★
    for w in want:
        if _cond_key(w, slug) not in have:
            return (f"登録した条件（{w.get('check')} / 版 {w.get('version')} "
                    f"/ {w.get('args')}）を、受領証が確かめていません"
                    "（登録した条件は全部そろって初めて閉じられます）")
    return ""


def cmd_close(path, args):
    """★閉じる★＝受領証の検査を**この場でやり直して**から書き換える。

    ★運営者の判断で閉じる道も残す★（--owner-decision）＝
    機械が確かめられない案件は実在する（例＝誤って閉じた案件の復元）。
    ★ただし別の印で残す★（closed_by）＝
    「機械が確かめた件数」を数えられなくなるのを防ぐ。
    """
    args.reason = _read_text_arg(args.reason, args.reason_file, "reason")
    if not args.reason:
        raise SystemExit("★--reason または --reason-file が要ります★")
    data = _load(path)
    row = next((i for i in data["issues"] if i["id"] == args.id), None)
    if row is None:
        print(f"⚠ 案件 #{args.id} が見つかりません")
        return 1
    if row["status"] != "open":
        print(f"#{args.id} は既にclosed（{row.get('resolved_date')}）")
        return 0

    closed_by = "machine"
    if getattr(args, "owner_decision", False):
        # ★★運営者が「機械では確かめられない」と判断したときだけ★★
        #   ★これは塞いだ裏口を開け直す形なので、条件を厳しくする★
        #   （2026-09-17・これはCodexの指摘ではなく、こちらで見つけた穴）
        #   ①★無人タスクが動いている間は使えない★＝
        #     自動で回る道に紛れ込むと、「機械が確かめて閉じた」件数が
        #     嘘になり、★仕組みが壊れても誰も気づかない★。
        #     （この形は自由文の受け取りで既に使っている守り）
        #   ②★別の印で残す★＝あとから「人の手が何件要ったか」を数える。
        #     数えられないと、自動化が進んだのか後退したのかが分からない。
        who = _running_task()
        if who:
            print(f"★閉じません★ いま無人タスク（{who}）が動いています。"
                  "運営者の判断で閉じる道は、無人の最中には使えません")
            return 1
        closed_by = "owner"
        print("★運営者の判断で閉じます★（機械の検査は通していません）")
    else:
        rp = str(getattr(args, "receipt", "") or "")
        if not rp:
            print("★閉じません★ 受領証（--receipt）がありません。"
                  "閉じる入口は python scripts/ledger_sweep.py "
                  "--slug <機種> --close <番号> …です")
            return 1
        try:
            with open(rp, encoding="utf-8") as f:
                rec = json.load(f)
        except Exception as e:                               # noqa: BLE001
            print(f"★閉じません★ 受領証を読めません: {rp}（{e}）")
            return 1
        ng = _receipt_problems(rec, args.id, row)
        if ng:
            print(f"★閉じません★ {ng}")
            return 1
        conds = rec["conditions"]
        ng = _condition_binds_row(row, conds)
        if ng:
            print(f"★閉じません★ {ng}")
            return 1
        # ★★申告を信じず、その場でやり直す★★
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import recheck as _rc
        except Exception as e:                               # noqa: BLE001
            print(f"★閉じません★ 検査の道具を読めません: {e}")
            return 1
        for c in conds:
            cond = dict(c["condition"])
            ok, why, got = _rc.closeable(cond)
            name = str(cond.get("check") or "?")
            if not ok:
                print(f"★閉じません★ {name} をやり直したら通りませんでした: "
                      f"{why}")
                return 1
            # ★確かめた時と同じものを見ているか★＝
            #   台帳の外にある控え（確定値・壊し方）はコミットで覆えないので、
            #   指紋を見比べないと「昨日の合格」で閉じられる。
            now_d = str((got or {}).get("observation_digest") or "")
            if now_d != str(c.get("observation_digest") or ""):
                print(f"★閉じません★ {name} で見たものが、"
                      "受領証を書いた時と変わっています")
                return 1
            print(f"  ○ {name} をやり直しました")

    row["status"] = "closed"
    row["resolution"] = args.reason
    row["resolved_date"] = _today()
    row["closed_by"] = closed_by
    _save(path, data)
    print(f"案件 #{args.id} をクローズ（{closed_by}）: {args.reason}")
    return 0


def cmd_condition_reset(path, args):
    """★登録した条件を、まとめて白紙に戻す★（2026-09-17・Codexの指摘）

    ★★なぜ要るか★★＝閉じる側は「使えない条件」を正しく断るのに、
      ★断られたあと、道具で直す道が無かった★。
        ・壊れた要素が混ざった一覧 … 読み飛ばしても残る限り断られる
        ・検査の版が上がった条件 … 登録し直しても別物として増えるだけ
        ・名簿から消えた検査／観測どまりへ変わった検査 … 登録し直せない
        ・いまの歴史に無いコミット … （これは登録し直せるようにした）
      ＝★どれも人が台帳のJSONを手で直すまで永久に閉じられなかった★。
      運営者の目的（手を使わずに閉じる）を正面から壊す。

    ★消したものは捨てない★＝`condition_resets` に写しを残す
      （何を白紙にしたかが分からなくなると、あとから追えない）。
    ★封も外す★（覆っている条件が無くなるため）。
    """
    why = _read_text_arg(args.why, args.why_file, "why")
    if len(why) < 10:
        raise SystemExit("★なぜ白紙に戻すのかを10字以上で★")
    by = [s.strip() for s in str(args.by or "").split(",") if s.strip()]
    _ngby = judges_problem(by)
    if _ngby:
        raise SystemExit("★" + _ngby + "★")
    data = _load(path)
    row = next((i for i in data["issues"] if i["id"] == args.id), None)
    if row is None:
        print(f"⚠ 案件 #{args.id} が見つかりません")
        return 1
    if row["status"] != "open":
        print(f"#{args.id} は既にclosed（白紙には戻しません）")
        return 1
    old = row.get("resolution_conditions")
    if old is None and not row.get("conditions_sealed"):
        print(f"#{args.id} には登録した条件がありません")
        return 0
    box = row.setdefault("condition_resets", [])
    box.append({"at": _today(), "by": by, "why": why,
                "dropped": json.loads(json.dumps(old, ensure_ascii=False,
                                                 default=str))})
    del box[:-10]                       # ★直近だけ残す★（台帳を太らせない）
    row.pop("resolution_conditions", None)
    row.pop("conditions_sealed", None)
    _save(path, data)
    n = len(old) if isinstance(old, list) else 0
    print(f"#{args.id} の閉じる条件を白紙に戻しました（{n} 件・封も外しました）")
    print("★このあと condition で登録し直し、seal で封をしてください★")
    return 0


def cmd_condition(path, args):
    """★案件に「これが通れば直っている」という条件を登録する★（2026-09-17）

    ★機種は書かせない★＝案件の行から取る（`_condition_binds_row` と同じ考え）。
    ★決めるのは2AI★＝機械は、名簿にある閉じられる検査かどうかと、
      **いまはまだ通らないこと**だけを見る。

    ★★いま通ってしまう検査は登録できない★★（2026-09-17・Codexの指摘）
      ★何が起きていたか★＝案件の説明文そのものを逐語にできた。
      例＝案件の詳細が「ヤメ時の説明が読みづらい」なら、
      `説明が読みづらい` を消えた逐語として渡せる。
      ★その文字は**もともと記事に無い**ので必ず「消えている」と出る★
      ＝ヤメ時は1文字も直っていないのに閉じられた（実際に再現した）。
      ★「案件の本文にある」と「記事から消すべき逐語だった」は別物★。
      → 登録の時点で通ってしまう検査は、あとで通っても
        ★何かが直った証拠にならない★ので受け取らない。
      ★すでに直っている案件は登録できない★＝機械には確かめようがないので、
        運営者の判断で閉じる道（`close --owner-decision`）へ回す。
    """
    why = _read_text_arg(args.why, args.why_file, "why")
    if len(why) < 10:
        raise SystemExit("★なぜその検査で直ったと言えるかを10字以上で★")
    by = [s.strip() for s in str(args.by or "").split(",") if s.strip()]
    _ngby = judges_problem(by)
    if _ngby:
        raise SystemExit("★" + _ngby + "★")
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import recheck as _rc
    except Exception as e:                                   # noqa: BLE001
        raise SystemExit(f"検査の名簿を読めません: {e}")
    meta = _rc.CHECKS.get(args.check)
    if meta is None:
        raise SystemExit(f"★知らない検査です★: {args.check}")
    if not meta.get("closeable"):
        raise SystemExit(f"★観測どまりの検査です★: {args.check}")
    cargs = {}
    for kv in (args.arg or []):
        if "=" not in kv:
            raise SystemExit(f"--arg は 名前=値 の形で書きます: {kv!r}")
        k, v = kv.split("=", 1)
        cargs[k.strip()] = v
    cargs.pop("slug", None)              # ★機種は行から固定する★
    data = _load(path)
    row = next((i for i in data["issues"] if i["id"] == args.id), None)
    if row is None:
        print(f"⚠ 案件 #{args.id} が見つかりません")
        return 1
    if row["status"] != "open":
        print(f"#{args.id} は既にclosed（条件は登録しません）")
        return 1

    # ★★いま動かして、まだ通らないことを確かめる★★
    run_args = pin_slug(args.check, cargs, str(row.get("slug") or ""))
    ng = _rc.validate_args(args.check, run_args)
    if ng:
        print(f"★登録しません★ 引数が足りません: {ng}")
        return 1
    # ★★落ちていた姿を、コミットで固定する★★（2026-09-17・Codexの指摘）
    #   ★直す前は未コミットのままでも登録できた★（自分で踏んだ）＝
    #   一時の書き換えで検査を落として登録し、それを戻せば
    #   ★元のコミットで通るので、何も直さずに閉じられた★。
    #   ＝「確かに壊れていた状態から直った」という証拠にならない。
    _head0 = _rc.head_commit()
    if not _rc.repo_clean():
        print("★登録しません★ 未コミットの変更があります"
              "（いま落ちている、と言い切れないため）")
        return 1
    got = _rc.run(args.check, run_args)
    if _rc.head_commit() != _head0 or not _rc.repo_clean():
        print("★登録しません★ 確かめている間にリポジトリが動きました")
        return 1
    # ★★「いま落ちている」ことを求める★★（PASS でないだけでは足りない）
    #   ★直す前は「PASS でなければよい」にしていた★ので、
    #   ★空の逐語（判定できない＝NOT_APPLICABLE）が登録できた★（自分で踏んだ）。
    #   判定できない・動かせないものは、あとで通っても何の証拠にもならない。
    if got.get("result") != _rc.FAIL:
        print(f"★登録しません★ {args.check} は**いま落ちていません**"
              f"（{got.get('result')}／{str(got.get('detail'))[:110]}）")
        print("  ＝あとで通っても「直った」証拠になりません。"
              "その問題を実際に見つける検査と引数を選ぶか、"
              "機械では確かめられない案件として "
              "close --owner-decision で閉じてください")
        return 1

    # ★★壊れた一覧のままでは登録しない★★（2026-09-17・Codexの指摘）
    #   ★直す前★＝一覧でないときは直し方を言わずに断り、
    #   壊れた要素が混ざっているときは★そのまま「登録しました」と出していた★
    #   （残った壊れた要素のせいで、閉じる側は結局ずっと断る）。
    _ngbox = conditions_broken(row)
    if _ngbox:
        print("★登録しません★ " + _ngbox)
        return 1
    box = row.setdefault("resolution_conditions", [])
    new = {"check": args.check, "version": meta["version"], "args": cargs,
           "set_at": _today(), "set_by": by, "why": why,
           "result_when_set": got.get("result"),
           "failed_at_commit": _head0,
           "failed_digest": got.get("observation_digest")}
    slug = str(row.get("slug") or "")
    # ★ここに来る時点で、一覧に壊れた要素は無い★
    #   （conditions_broken が手前で断っている＝同じ規則を2か所に書かない）
    _same = [i for i, c in enumerate(box)
             if _cond_key(c, slug) == _cond_key(new, slug)]
    if _same:
        # ★★使えない古い条件は、登録し直せる★★（2026-09-17・Codexの指摘）
        #   ★直す前は「同じ条件です」で終わっていた★ので、
        #   ★汚れた木で登録した条件を、道具からは直せなかった★。
        # ★★閉じる側と同じ物差しで見る★★（同・2回目の指摘）＝
        #   ★形だけを見ていた★ので、別の枝や rebase 前の40桁も
        #   「証拠あり」として扱い、
        #   ①閉じるときは「いまの歴史の中にありません」で断られ
        #   ②登録し直そうとすると「もう登録されています」で更新されず
        #   ＝★人が台帳を手で直すまで永久に閉じられなかった★。
        _old = box[_same[0]]
        if (not evidence_problem(_old)
                and is_ancestor(str(_old.get("failed_at_commit") or ""))):
            print(f"#{args.id} には同じ条件がもう登録されています")
            return 0
        box[_same[0]] = new
        row.pop("conditions_sealed", None)
        _save(path, data)
        print(f"#{args.id} の条件を、いまの証拠で登録し直しました"
              f"（落ちていたコミット {_head0[:12]}）")
        return 0
    box.append(new)
    # ★条件を足したら、前の「全部そろった」宣言は無効にする★
    #   （そろっていないのに閉じられる状態に戻さないため）
    row.pop("conditions_sealed", None)
    _save(path, data)
    print(f"#{args.id} に閉じる条件を登録しました: {args.check} {cargs}"
          f"（いまは {got.get('result')}／計 {len(box)} 件）")
    print("★このあと『これで案件の全部を覆った』と封をしてください★"
          "＝ python scripts/open_issues.py seal --id "
          f"{args.id} --why-file <理由> --by claude,codex")
    return 0


def issue_digest(row: dict) -> str:
    """★案件の本文の指紋★（題＋詳細）。封をしたあと書き換わったら気づくため。"""
    src = str(row.get("title") or "") + "\n" + str(row.get("detail") or "")
    return hashlib.sha256(src.encode("utf-8")).hexdigest()


def cmd_seal(path, args):
    """★「この条件で案件の全部を覆った」と2AIが封をする★（2026-09-17）

    ★★なぜ機械に数えさせないか★★（Codexは案件を機械可読な複数の要件へ
      分けることを勧めたが、そこは採らなかった）＝
      「この案件には問題がいくつ書いてあるか」は**文章を読んで決めること**で、
      機械にやらせると例外リストと場合分けが際限なく増える
      （運営者から繰り返し止められている型）。
    ★機械にできること★＝
      ①2AIが「全部覆った」と明言したことを記録する
      ②そのときの**案件の本文の指紋**を残す
      ③閉じるとき、指紋が変わっていたら封を無効にする
      ④封をしたあと条件を足したら、封を外す
    ＝「そろっているか」は2AIの判断、「そのあと動いていないか」は機械の担当。
    """
    why = _read_text_arg(args.why, args.why_file, "why")
    if len(why) < 10:
        raise SystemExit("★なぜこれで案件の全部を覆ったのかを10字以上で★")
    by = [s.strip() for s in str(args.by or "").split(",") if s.strip()]
    _ngby = judges_problem(by)
    if _ngby:
        raise SystemExit("★" + _ngby + "★")
    data = _load(path)
    row = next((i for i in data["issues"] if i["id"] == args.id), None)
    if row is None:
        print(f"⚠ 案件 #{args.id} が見つかりません")
        return 1
    if row["status"] != "open":
        print(f"#{args.id} は既にclosed（封はしません）")
        return 1
    conds = row_conditions(row)
    if not conds:
        print("★封をしません★ 閉じる条件が1件も登録されていません")
        return 1
    slug = str(row.get("slug") or "")
    row["conditions_sealed"] = {
        "at": _today(), "by": by, "why": why,
        "issue_digest": issue_digest(row),
        "condition_keys": sorted(json.dumps(_cond_key(c, slug),
                                            ensure_ascii=False)
                                 for c in conds),
    }
    _save(path, data)
    print(f"#{args.id} に封をしました（条件 {len(conds)} 件）"
          "＝これで案件の全部を覆ったという2AIの宣言です")
    return 0


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
    p.add_argument("--round", dest="round_id", default="",
                   help="★この回の名前★（同じ名前は二度数えない。"
                        "タスクが落ちてやり直しただけで3回に達するのを防ぐ）")
    p.add_argument("--outcome", default="unresolved",
                   choices=["unresolved", "error"],
                   help="unresolved=決まらなかった（数える）／"
                        "error=仕組みの都合で動かせなかった（数えない）")

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
    p.add_argument("--receipt", default="",
                   help="★ledger_sweep が出した受領証★"
                        "（中の検査をここでやり直してから閉じる）")
    p.add_argument("--owner-decision", dest="owner_decision",
                   action="store_true",
                   help="★機械では確かめられない案件を、運営者の判断で閉じる★"
                        "（無人タスクの最中は使えない・別の印で残る）")

    p = sub.add_parser("condition",
                       help="案件に「これが通れば直っている」条件を登録する")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--check", required=True, help="閉じられる検査の名前")
    p.add_argument("--arg", action="append", default=[],
                   help="検査に渡す引数（名前=値・複数可。★機種は書かない★）")
    p.add_argument("--why", default="")
    p.add_argument("--why-file", dest="why_file", default="",
                   help="なぜその検査で直ったと言えるか（無人タスクはこちら）")
    p.add_argument("--by", default="", help="判断者（例: claude,codex）")

    p = sub.add_parser("condition-reset",
                       help="登録した条件を白紙に戻す（封も外す）")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--why", default="")
    p.add_argument("--why-file", dest="why_file", default="",
                   help="なぜ白紙に戻すのか（無人タスクはこちら）")
    p.add_argument("--by", default="", help="判断者（例: claude,codex）")

    p = sub.add_parser("seal",
                       help="「この条件で案件の全部を覆った」と2AIが封をする")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--why", default="")
    p.add_argument("--why-file", dest="why_file", default="",
                   help="なぜこれで案件の全部を覆ったか（無人タスクはこちら）")
    p.add_argument("--by", default="", help="判断者（例: claude,codex）")

    args = ap.parse_args()
    path = Path(args.file) if args.file else DEFAULT_FILE
    fn = {"add": cmd_add, "list": cmd_list, "digest": cmd_digest, "close": cmd_close,
          "severity": cmd_severity, "blocking": cmd_blocking,
          "questions": cmd_questions, "attempt": cmd_attempt,
          "notified": cmd_notified, "condition": cmd_condition,
          "seal": cmd_seal, "condition-reset": cmd_condition_reset,
          "notifications": cmd_notifications}[args.cmd]
    sys.exit(fn(path, args))


if __name__ == "__main__":
    raise SystemExit(main())

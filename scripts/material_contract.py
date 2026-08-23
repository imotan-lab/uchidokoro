"""material_contract.py — ★どの出典を材料に採るかを決める側の承認★

★何のためか★（2026-08-17・台帳#389／Codex依頼230〜232の設計）
  いまの承認の仕組み（template-approval.json）は**公開物を作る側**が対象です。
  ひな型・CSS・共通JS・公開HTMLを書くスクリプトを1行変えると全機種に効くので、
  指紋を固定して「過不足なく一致した時だけ作れる」ようにしてあります。

  ところが★どの出典を材料に採るかを決める側★は、どのレビューにも載りません。
    maker-catalogs.json      … MATCH / RELATED の分かれ目を決める
    directory-catalogs.json  … 根拠にしてよいURLの形・発行者
    automation-policy.json   … どこへ通信してよいか（規約の承認）
    source-registry.json     … 独立した票の数え方
  これらは**記事の中身を直接左右する**のに、黙って変えられました。

★なぜ template-approval に足さないか★（Codexの判断・こちらも同意）
  あちらは「公開物の契約」です。意味の違うものを混ぜると、どちらの契約も
  ぼやけます。**別の集合**にして、それぞれの目的を保ちます。

★この仕組みが見るもの★
  ①集合が**過不足なく**一致すること（外して回避できないように）
  ②各ファイルの指紋が承認済みと一致すること
  ③★各スクリプトが取り込んでいる手元のモジュールの顔ぶれ★が変わっていないこと
    （2026-08-17・Codexの指摘＝「新しい依存を足したのに承認集合へ
      足していない場合を落とす試験も必要」）
    ＝顔ぶれが変わったら止まる。そのとき人が「集合に足す」か
      「その依存はここでは持たない」かを決めます。

使い方:
    python scripts/material_contract.py --check      # 確かめる（既定）
    python scripts/material_contract.py --approve    # 今の中身で承認し直す
    python scripts/material_contract.py --selftest
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

SCHEMA = "material-contract-approval/v1"
STORE = os.path.join("assets", "data", "material-contract-approval.json")

# ★材料の採否を決める固定集合★（Codex依頼232の一覧＋こちらの追加1件）
#   ★ここから外して回避できないように、過不足なく一致を求めます★
CONTRACT_INPUTS = frozenset({
    # 状態を作る側（名鑑のメーカー欄がどの社か・そのページが本人か）
    "scripts/model_code_lookup.py",
    # 控えを確かめる側（この名鑑ページを材料に使ってよいか）
    "scripts/maker_identity_cache.py",
    # 採否を使う側・公開への接続
    "scripts/add_machine_run.py",
    # ★★DMM側の取得器★★（2026-08-21・台帳#425）
    #   ★なぜ材料の契約に入れるのか★
    #     採否を決める最後の関門（maker_identity_cache）は、控えを作るときに
    #     `dmm_machine.fetch()` の戻り値を**そのまま信用して**
    #     機種名の照合と導入日（YYYY-MM-DD でなければ控えを拒否）を決めている
    #     （1496行で実際に import している）。
    #     ＝★「この名鑑ページを材料に使ってよいか」の判断が、
    #        dmm_machine の読み取り結果に乗っている★。
    #     ここが集合の外だと、書き換えても承認をやり直さずに通ってしまう。
    #   ★関門だけを守っても、その関門が信用している側が野放しなら意味がない★
    "scripts/dmm_machine.py",
    "scripts/dmm_calendar.py",
    "scripts/dmm_discover.py",
    "scripts/slug_binding.py",
    # 独立した票の数え方
    "scripts/source_lineage.py",
    "assets/data/source-registry.json",
    # ★★その票数で採ってよいかを決める側★★（2026-08-23・運営者決定）
    #   「新台公開1週間前でもDMMしかない状態なら、DMMのだけを正として
    #     記事にしていい」を実装した場所。
    #   ★source_lineage が「何票あるか」／ここが「その票数で採ってよいか」★
    #   ＝採否そのものを決めるので、材料の契約から外せない。
    #   ★ここが集合の外だと、条件を1行ゆるめるだけで
    #     承認をやり直さずに1出典の内容を公開できてしまう★
    "scripts/adoption_basis.py",
    # ★★人と2AIが確かめた出典の控え★★（2026-08-23・Codexの敵対的レビューP0）
    #   ★なぜ採否に効くのか★＝「DMM単独だ」と名乗ってよいかを、
    #   ここに別の発行者の出典が無いかで判断する。
    #   索引は1ページしか読めない名鑑があるので、★記事があるのに索引に
    #   出ない★ことが実際にある（台帳#468で実測）。控えを見ないと
    #   「DMMしかない」と誤判定して、食い違いを見逃す経路が成立していた。
    "scripts/machine_sources.py",
    # 通信してよい先（規約の承認）
    "scripts/new_machine_watch.py",
    "scripts/automation_policy.py",
    "assets/data/automation-policy.json",
    # ★規約で禁じた先の境界★（こちらの追加。Codexの一覧には無いが、
    #   automation_policy より上位で必ず止める役なので同じ集合に置く）
    "scripts/blocked_hosts.py",
    # 投稿欄・AI欄を落とす役（落とし損ねると読者の書き込みが材料になる）
    "scripts/user_area.py",
    # ★★材料の採否・値を左右する直接の依存★★（2026-08-21・台帳#420）
    #   ★契約が「直接依存に閉じていなかった」★＝
    #   契約に入っている側が中で呼んでいるのに、呼ばれる側は指紋の対象外だった。
    #   ＝呼ばれる側を書き換えれば、承認をやり直さずに採否を変えられた。
    #   ★入れる基準＝材料の採否・値そのものを左右するか★
    #     directory_index  … どのURLを候補にするかを決める
    #     lineage_check    … 転載を見抜いて票と材料から外す（独立2出典の土台）
    #     html_tables      … 値の表を「区画」として読む共通部品
    #     confirmed_values … 2AIで確定した値を材料へ足す
    #     page_probe       … 出典が変わったかを見て、取り直すかを決める
    "scripts/directory_index.py",
    "scripts/lineage_check.py",
    "scripts/html_tables.py",
    "scripts/confirmed_values.py",
    "scripts/page_probe.py",
    # ★取ってきた本文を1個のデータとして持ち回る器★（2026-08-17・台帳#393）
    #   「確かめた本文」と「あとで読む本文」を同じ物にする土台なので、
    #   材料の採否そのものと同じ集合に置く。
    "scripts/fetched_page.py",
    # ★材料の値を読む側★（2026-08-17・Codex依頼234の厚みの指摘）
    #   4つとも共通の関所（material_page_identity_ok）を通しているが、
    #   その1行を将来消しても #389 の指紋検査だけでは通ってしまうため、
    #   同じ契約の中に置いて「変えたら承認し直す」を強制する。
    "scripts/spec_lookup.py",
    "scripts/ceiling_lookup.py",
    "scripts/cz_lookup.py",
    "scripts/at_spec_lookup.py",
    # メーカー・名鑑の設定
    "assets/data/maker-catalogs.json",
    "assets/data/directory-catalogs.json",
})

# ★取り込んでいてもよい「土台」★＝どのスクリプトも使う共通の部品。
#   ここに載っているものは、顔ぶれの記録から外します（毎回変わって邪魔なので）。
#   ★土台そのものを変える話は、この仕組みの外です★
BASELINE_MODULES = frozenset({
    "safe_json", "local_paths", "ci_safe", "open_issues", "task_lock",
    "backup_guard", "log", "send_notify",
})


class ContractError(Exception):
    """材料の採否の契約に関する異常（★迷ったら止める★）。"""


def _sha(path: str) -> str:
    """★改行をそろえてから指紋を取る★（2026-08-21・台帳#430と同じ型）

    ★直す前に何が起きたか★
      この会社PCは `core.autocrlf=true` なので、
      **git の中は LF・チェックアウトすると CRLF** になる。
      ところがここは読んだ中身をそのまま数えていたので、
      ★同じ内容のファイルでも「どうチェックアウトしたか」で指紋が変わった★。

      実際、CIと同じ条件を再現するために綺麗なクローンを作ったら
      automation-policy.json が「承認済みの内容と違います」で落ちた。
      中身は1文字も違わないのに、である。

      ★公開物の契約（build_pages_artifact.template_sha）は最初から
        そろえていた★ので、materialだけが取り残されていた。

    ★同じ規則を2か所に書かない★＝あちらと同じ「CRLF→LF」にそろえる。
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        data = f.read()
    # ★中身がプログラムでない（画像など）ものは入っていない集合だが、
    #   万一入っても壊さないように、NULを含むものはそのまま数える★
    if b"\x00" not in data[:8000]:
        data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    h.update(data)
    return h.hexdigest()


def local_imports(path: str, base: str = BASE) -> list:
    """★そのスクリプトが取り込んでいる「手元のモジュール」の顔ぶれ★

    ★字面で探さない★（2026-08-17）＝`import` の文字を正規表現で拾うと、
      文字列の中・コメント・関数の中の書き方の違いで数が揺れます。
      ここは ast（構文として読む）で数えます。監査43で同じ失敗をしています。
    """
    if not path.endswith(".py"):
        return []
    with open(os.path.join(base, path), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    scripts_dir = os.path.join(base, "scripts")
    got = set()
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module.split(".")[0]]
        for n in names:
            if n in BASELINE_MODULES:
                continue
            if os.path.isfile(os.path.join(scripts_dir, n + ".py")):
                got.add(n)
    return sorted(got)


def snapshot(base: str = BASE, deps_why: str = "") -> dict:
    """いまの中身から承認の内容を作る。"""
    files, imports = {}, {}
    for name in sorted(CONTRACT_INPUTS):
        p = os.path.join(base, name)
        if not os.path.isfile(p):
            raise ContractError(f"承認対象のファイルがありません: {name}")
        files[name] = _sha(p)
        if name.endswith(".py"):
            imports[name] = local_imports(name, base)
    out = {"schema_version": SCHEMA,
           "_why": ("★どの出典を材料に採るかを決める側の指紋★（台帳#389）。"
                    "ここと実ファイルが一致する時だけ公開物を作れる。"
                    "中身を直したらこのファイルも更新すること"
                    "（python scripts/material_contract.py --approve）＝"
                    "レビューに載せるための仕組み。"),
           "files": files, "imports": imports}
    if deps_why:
        out["_deps_changed_why"] = deps_why
    return out


def check(base: str = BASE) -> dict:
    """★承認済みの内容と一致するか★（違えば例外）"""
    p = os.path.join(base, STORE)
    if not os.path.isfile(p):
        raise ContractError(
            f"承認の控えがありません: {STORE}"
            "／★python scripts/material_contract.py --approve で作ります★")
    try:
        with open(p, encoding="utf-8") as f:
            want = json.load(f)
    except Exception as e:                 # noqa: BLE001
        raise ContractError(f"承認の控えを読めません: {e}")
    if want.get("schema_version") != SCHEMA:
        raise ContractError(
            f"承認の控えの版が違います（想定 {SCHEMA}／"
            f"実際 {want.get('schema_version')!r}）")
    files = want.get("files")
    if not isinstance(files, dict):
        raise ContractError("承認の控えに files（辞書）がありません")
    # ★①集合が過不足なく一致すること★（外して回避できないように）
    if set(files) != set(CONTRACT_INPUTS):
        missing = sorted(set(CONTRACT_INPUTS) - set(files))
        extra = sorted(set(files) - set(CONTRACT_INPUTS))
        raise ContractError(
            f"承認一覧が固定集合と一致しません（不足: {missing} / 余分: {extra}）")
    now = snapshot(base)
    # ★②取り込んでいる手元のモジュールの顔ぶれ★を先に見る
    #   ＝新しい依存を足したのに集合へ入れ忘れた、を落とす（Codexの指摘）
    #   ★指紋より先に見る理由★＝.py を1文字直すと指紋も必ず変わるので、
    #     指紋を先に見ると「依存が増えた」という**具体的な話が埋もれる**。
    _deps_changed(want, now)
    # ★③指紋が一致すること★
    for name in sorted(CONTRACT_INPUTS):
        if files[name] != now["files"][name]:
            raise ContractError(
                f"{name} が承認済みの内容と違います"
                f"（承認: {str(files[name])[:12]}…／実際: "
                f"{now['files'][name][:12]}…）。意図した変更なら "
                "python scripts/material_contract.py --approve を実行して"
                "一緒にコミットすること")
    return now


def _deps_changed(want: dict, now: dict) -> list:
    """依存の顔ぶれの違いを返す（違えば例外・同じなら空）。"""
    got_imp = want.get("imports")
    if not isinstance(got_imp, dict):
        raise ContractError("承認の控えに imports（辞書）がありません")
    diffs = []
    known = set((want.get("files") or {}))
    for name, mods in sorted(now["imports"].items()):
        old = got_imp.get(name)
        if old is None:
            # ★集合に新しく入れたファイルは「顔ぶれが変わった」ではない★
            #   （2026-08-17）＝新入りは、集合が増えたことが承認の差分に出る。
            #   ★既存のファイルなのに依存の記録が無いときは止める★（fail-closed）
            if name not in known:
                continue
            raise ContractError(f"承認の控えに {name} の依存がありません")
        if list(old) != list(mods):
            diffs.append((name, sorted(set(mods) - set(old)),
                          sorted(set(old) - set(mods))))
    if diffs:
        msg = "／".join(f"{n}（増えた: {a} / 減った: {g}）" for n, a, g in diffs)
        raise ContractError(
            f"取り込むモジュールの顔ぶれが変わりました: {msg}"
            "／★増えたものを材料の採否の集合に入れるか、ここでは持たないかを"
            "決めてください。決めたら理由をつけて承認します＝"
            "python scripts/material_contract.py --approve "
            "--deps-why-file <理由を書いたファイル>★")
    return diffs


def write(base: str = BASE, deps_why: str = "") -> dict:
    """承認し直す。

    ★依存の顔ぶれが変わっているときは、理由なしでは書き換えない★
      （2026-08-17・自分で作った穴を塞ぐ）
      指紋だけ見る形にしていたら、`--approve` が**依存の記録も黙って
      上書き**するので、Codexの求めた「新しい依存を足したら落ちる」が
      成り立っていなかった（自己試験で発覚）。
    """
    p = os.path.join(base, STORE)
    if os.path.isfile(p) and not deps_why:
        try:
            with open(p, encoding="utf-8") as f:
                old = json.load(f)
        except Exception:                  # noqa: BLE001
            old = None
        if isinstance(old, dict) and isinstance(old.get("imports"), dict):
            _deps_changed(old, snapshot(base))   # 違えばここで例外
    got = snapshot(base, deps_why)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(got, ensure_ascii=False, indent=1) + "\n")
    return got


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    import shutil
    import tempfile
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅ " if cond else "❌ ") + name)

    with tempfile.TemporaryDirectory() as td:
        # 本物の並びを写して、そこで壊してみる
        for name in sorted(CONTRACT_INPUTS):
            dst = os.path.join(td, name)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(BASE, name), dst)
        write(td)
        t("★★いまの中身なら通る★★", bool(check(td)))

        # ①集合から外して回避できないこと
        p = os.path.join(td, STORE)
        d = json.load(open(p, encoding="utf-8"))
        one = sorted(CONTRACT_INPUTS)[0]
        d["files"].pop(one)
        json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False)
        try:
            check(td)
            t("★★承認一覧から外して回避できない★★", False)
        except ContractError as e:
            t("★★承認一覧から外して回避できない★★", "不足" in str(e))

        # ②中身を変えたら止まること
        write(td)
        tgt = os.path.join(td, "assets/data/maker-catalogs.json")
        with open(tgt, "a", encoding="utf-8") as f:
            f.write("\n")
        try:
            check(td)
            t("★★設定を黙って変えたら止まる★★", False)
        except ContractError as e:
            t("★★設定を黙って変えたら止まる★★", "承認済みの内容と違います" in str(e))

        # ③新しい依存を足したら止まること
        shutil.copy2(os.path.join(BASE, "assets/data/maker-catalogs.json"), tgt)
        write(td)
        # ★写しの中にも「手元のモジュール」を1つ置く★
        #   置かないと、そもそも手元のものだと認識されず試験にならない
        open(os.path.join(td, "scripts/よそのしくみ.py"), "w",
             encoding="utf-8").write("X = 1\n")
        py = os.path.join(td, "scripts/user_area.py")
        src = open(py, encoding="utf-8").read()
        open(py, "w", encoding="utf-8").write("import よそのしくみ\n" + src)
        try:
            check(td)
            t("★★★新しい依存を足したのに集合へ入れ忘れたら止まる★★★"
              "（Codex依頼232の指摘）", False)
        except ContractError as e:
            t("★★★新しい依存を足したのに集合へ入れ忘れたら止まる★★★"
              "（Codex依頼232の指摘）",
              "顔ぶれが変わりました" in str(e) and "よそのしくみ" in str(e))
        # ★★承認し直すだけでは黙って通せないこと★★
        #   （自分で作った穴＝指紋だけ見ていたら --approve が依存の記録も
        #     上書きして、上の関門が骨抜きになっていた）
        try:
            write(td)
            t("★★★理由なしの承認で、依存の変化を黙って通せない★★★", False)
        except ContractError as e:
            t("★★★理由なしの承認で、依存の変化を黙って通せない★★★",
              "顔ぶれが変わりました" in str(e))
        t("　（対照）理由をつければ承認できる",
          bool(write(td, deps_why="試験のため")) and bool(check(td)))
        t("　理由は控えに残る（あとから何を許したか分かる）",
          json.load(open(os.path.join(td, STORE), encoding="utf-8"))
          .get("_deps_changed_why") == "試験のため")

        # 承認の控えが無い／版が違うときは止まる（fail-closed）
        os.remove(os.path.join(td, STORE))
        try:
            check(td)
            t("　承認の控えが無ければ止まる（fail-closed）", False)
        except ContractError as e:
            t("　承認の控えが無ければ止まる（fail-closed）", "ありません" in str(e))

    # ★字面ではなく構文で数えていること★（監査43と同じ失敗を繰り返さない）
    t("★依存は ast で数える（文字列やコメントの中を拾わない）★",
      "maker_identity_cache" in local_imports("scripts/add_machine_run.py")
      and "material_contract" not in local_imports("scripts/user_area.py"))
    t("　土台の部品は顔ぶれに数えない（毎回変わって邪魔なので）",
      all(m not in local_imports("scripts/add_machine_run.py")
          for m in ("safe_json", "local_paths")))

    ng = sum(1 for _, o in results if not o)
    print()
    print("%d/%d 合格" % (len(results) - ng, len(results)))
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="材料の採否を決める側の承認")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--approve", action="store_true")
    # ★自由文はファイルで渡す★（コマンドに書いた記号がシェルに実行されるため）
    ap.add_argument("--deps-why-file", dest="deps_why_file", default="",
                    help="依存の顔ぶれを変えた理由を書いたファイル")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.approve:
        why = ""
        if a.deps_why_file:
            try:
                import open_issues as _oi
                why = _oi._read_text_arg("", a.deps_why_file, "deps-why")
            except SystemExit as e:
                print(str(e))
                return 2
        try:
            got = write(deps_why=why)
        except ContractError as e:
            print("★" + str(e) + "★")
            return 1
        print(f"承認し直しました: {STORE}（{len(got['files'])}件）")
        return 0
    try:
        check()
    except ContractError as e:
        print("★" + str(e) + "★")
        return 1
    print(f"材料の採否の契約: 一致しています（{len(CONTRACT_INPUTS)}件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

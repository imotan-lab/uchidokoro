#!/usr/bin/env python3
"""build_pages_artifact.py — 全機種に一斉に効く入力の「承認」を扱う。

★いまの役目は承認の仕組みだけ★（2026-10-01）
  かつてはここが「公開物を空のフォルダから組み立てる」役（Phase 1 の公開ゲート
  claim-gate が有効なときだけ動く build）も持っていたが、ゲートは一度も
  有効にされないまま運営者の判断で撤去した。組み立て・成果物の監査・外部依存の
  洗い出し・CSS検査・published-slugs の扱いは、そのとき一緒に外した。

★承認とは★
  ひな型・固定ページ・CSS・共通JS・SW・画像・公開物や記事データを書くスクリプト群など、
  「1か所直すと全機種に一斉に効く入力」を APPROVED_INPUTS に列挙し、
  その指紋を assets/data/template-approval.json に記録する。
  変えたら --approve で記録し直して一緒にコミットする（＝全機種に効く変更を
  必ずレビューに載せる）。照合は「過不足なく一致」（一覧から外して回避させない）。

使い方:
    python scripts/build_pages_artifact.py --approve    # 承認一覧を今の中身で作り直す
    python scripts/build_pages_artifact.py --check      # 承認と実ファイルが一致するか見る（書かない）
    python scripts/build_pages_artifact.py --selftest
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]

APPROVAL_SCHEMA = "template-approval/v2"

# ★全機種に一斉に効く入力★（1箇所直すと全ページに載るもの）
#   ここは「過不足なく一致」を要求する。承認一覧から外して回避できないようにするため。
APPROVED_INPUTS = frozenset({
    # ページのひな型
    "machine.html",
    "index.html",
    # 手書きの固定ページ（そのまま公開されるので中身の検査が効かない）
    #   （Codex 17巡目 (a)-2：ここに嘘を書けば監査を通って公開されていた）
    "404.html",
    "about.html",
    "contact.html",
    "privacy.html",
    "guide-haena.html",
    "guide-pochipochi.html",
    "guide-rate.html",
    "guide-reset.html",
    "guide-yamedoki.html",
    "manifest.json",
    # 全ページで読み込まれる見た目と動き
    "assets/css/practical.css",
    "meta-auto.js",
    "service-worker.js",
    # 公開設定（そのまま配信される）
    "CNAME",
    "ads.txt",
    "robots.txt",
    "favicon.ico",
    "googleafe441235e57f84f.html",
    # 画像（★中に文字を描けば表示できるので全部★・Codex 18巡目 (a)-2）
    "assets/img/logo.png",
    "assets/img/ogp.png",
    "assets/img/favicon-16.png",
    "assets/img/favicon-32.png",
    "assets/img/apple-touch-icon.png",
    "assets/img/icon-192.png",
    "assets/img/icon-512.png",
    # 公開物を作るコード（ここを直せば何でも書ける）
    #   ★判定を構成する側も入れる★（Codex 17巡目 (a)-1）
    "scripts/build_pages_artifact.py",
    "scripts/build_machine_pages.py",
    "scripts/build_hub_pages.py",
    "scripts/build_ledger.py",
    # ★★毎朝すべての機種の記事データを書き換える側★★（2026-08-21・台帳#438）
    #   grow_machine / grow_legacy は machine-details/{slug}.json と
    #   machines.json を直接書く。＝★ここを直せば何でも書ける★
    #   （このかたまりの見出しがそのまま当てはまる）。
    #   ★材料の契約ではない★＝あちらは「材料の採否を決める側」で、
    #   採否を決めているのは add_machine_run / maker_identity_cache。
    #   grow_* は決まった採否を1機種に当てはめて記事へ書く側。
    #   ★実際に穴が溜まっていた★＝2026-08-20の依頼239で
    #   「grow_machine が slug を名乗っていない」「grow_legacy がメーカーを
    #   渡していない」が見つかった。関所が見ていない場所だった。
    "scripts/grow_machine.py",
    "scripts/grow_legacy.py",
    # ★★2AIが決めた直しを記事データへ書く側★★（2026-08-21）
    #   ★同じ理由で入れる★＝どれも machine-details/{slug}.json を直接書く。
    #   しかも slug を指定しなければ**全機種を一度に**書き換える
    #   （fix_plain_style / strip_model_code は run(slug=None) で全機種）。
    #   ＝「ここを直せば何でも書ける」がそのまま当てはまる。
    #   ★入れ忘れていた★＝2026-08-21に作った当日は承認の集合に入れておらず、
    #   grow_machine / grow_legacy と同じ役なのに関所が見ていなかった。
    "scripts/decide_now.py",
    "scripts/apply_prose_dedup.py",
    "scripts/fix_plain_style.py",
    "scripts/strip_model_code.py",
    # ★2026-09-01に足した★（Codexの指摘）＝記事データを直接書けるのに
    #   承認の集合へ入れ忘れていた（decide_now / apply_prose_dedup と同じ役）。
    #   実際に47機種の記事を書き換えている。
    "scripts/tableize_spec.py",
    # ★★狙い目の文を全機種ぶん作る側★★（2026-09-11）
    #   machines.json の strategy / strategyByRate と、
    #   machine-details の狙い目の箱を**全機種まとめて**書き換える。
    #   ＝「ここを直せば何でも書ける」がそのまま当てはまる。
    #   ★材料の契約ではない★＝出典の採否は決めない
    #   （チェッカーが持っている値を読んで文にするだけ）。
    #   ★承認はコードの話であって、生成物が最新かは別★
    #   （そちらは push 前の関所の点検が見る）。
    "scripts/target_display.py",
    # ★★カウンターの注記を直す側★★（2026-09-12）
    #   machines.json の checker の注記を**全機種まとめて**書き換える。
    #   注記は読者の画面に出る（カウンターのすぐ下）ので、
    #   target_display と同じ理由で承認に載せる。
    #   ★材料の契約ではない★＝出典の採否は決めない。
    "scripts/note_text.py",
    # ★★狙い目の線（チェッカーの good / caution）を書く側★★（2026-09-18）
    #   machines.json の checker を書き換える＝★読者の画面に直接出る★
    #   （狙い目チェッカーの判定と、そこから作る一覧の文）。
    #   ★材料の契約ではない★＝出典の採否は決めない（2AIが決めた線を書くだけ）。
    "scripts/checker_verdict.py",
    # ★★新台を記事にして公開するまでの一式★★（2026-08-21・台帳#420）
    #   ★契約が「直接依存に閉じていなかった」★＝
    #   契約に入っている側が中で呼んでいるのに、呼ばれる側は指紋の対象外だった。
    #   ＝呼ばれる側を書き換えれば、承認をやり直さずに公開物を変えられた。
    #   ★どちらの契約にも入っていなかった6本★＝
    #     build_new_article  … 材料から記事データを組み立てる
    #     page_decision      … 区分と判定書（検索に載せるかを決める唯一の場所）
    #     publish_new_machine… 新台1機種を公開する専用経路
    #     prepush_gate       … push してよいかを決める最後の関所
    #     check_duplicate    … 二重登録を止める
    #     pending_machines   … まだ記事にできていない新台の控え
    "scripts/build_new_article.py",
    "scripts/page_decision.py",
    "scripts/publish_new_machine.py",
    "scripts/prepush_gate.py",
    "scripts/check_duplicate.py",
    "scripts/pending_machines.py",
    "scripts/gates.py",
    "scripts/audit_public.py",
    "scripts/claim_reconcile.py",
    "scripts/claim_c5.py",
    "scripts/claim_inventory.py",
    "scripts/claim_ledger.py",
    "scripts/claim_identity.py",
    "scripts/claim_evidence.py",
    "scripts/preview_site.py",
    "scripts/ci_safe.py",
    "scripts/safe_json.py",
    # ★claim_inventory が実行時にimportする（公開判定に入る）★（Codex 19巡目 (a)-2）
    "scripts/extract_setting_rates.py",
    # ★extract_setting_rates が直接読む（確率の出どころ）★
    "setting.html",
    # ハブの手書き散文
    "scripts/hub_prose.json",
    # ★公開判断の土台になる設定★（Codex 20巡目 (a)-6）
    #   ledger はリスク表現を ALLOW にできる。registry は「独立2票」の数え方を、
    #   allowlist は自動採用してよい型を決める。
    "assets/data/ledger.json",
    "assets/data/source-registry.json",
    "assets/data/claim-allowlist.json",
})


class BuildError(RuntimeError):
    pass


def redact_value(text) -> str:
    """診断に載せる値（CIでは原文を出さない）。"""
    sys.path.insert(0, str(BASE / "scripts"))
    from ci_safe import redact as _r
    return _r(text)


def _no_duplicate_keys(pairs):
    """★JSONの同名キー重複を黙って通さない★（Codex 13巡目 (b)-3）
      Python既定は last-wins なので、同じキーを2回書けば
      前の値が消える＝照合を欺ける。重複はその場で失敗させる。
    """
    seen = {}
    for k, v in pairs:
        if k in seen:
            raise BuildError(f"duplicate JSON key: {redact_value(k)}")
        seen[k] = v
    return seen


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"),
                          object_pairs_hook=_no_duplicate_keys)
    except BuildError:
        raise
    except Exception as exc:
        raise BuildError(f"cannot read JSON: {path}: {exc}") from exc


def read_json_dict(path: Path) -> dict:
    """辞書であることまで確かめて読む（`.get()` で未処理例外にしない）。"""
    data = read_json(path)
    if not isinstance(data, dict):
        raise BuildError(f"expected a JSON object: {path}")
    return data


def template_sha(path: Path) -> str:
    """ひな型の指紋（改行差を吸収してから取る）。"""
    return hashlib.sha256(
        path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")).hexdigest()


def check_template_approved(work: Path) -> dict:
    """★全機種に一斉に効く入力が、承認済みのものと一致するか★

    （2026-07-30・Codex 15巡目 (a)-1 / 16巡目 (a)-1・(a)-2）
      ひな型に固定文を1行足すと全機種のページに載る。作り直して比べる検査は
      **同じひな型を使う**ので一致してしまう＝共通原因の故障。
      さらに、ひな型だけ固定しても **CSS・共通JS・Service Worker・生成器のコード**
      から同じことができる（`body::before{content:"…"}` など）。
      そこで「全機種に一斉に効く入力」を固定集合として列挙し、
      **過不足なく一致すること**を要求する（承認一覧から外して回避できないように）。
    """
    approval = read_json_dict(work / "assets/data/template-approval.json")
    if approval.get("schema_version") != APPROVAL_SCHEMA:
        raise BuildError(
            f"template-approval.json の schema_version が想定と違います"
            f"（想定 {APPROVAL_SCHEMA}／実際 {approval.get('schema_version')!r}）")
    want = approval.get("templates")
    if not isinstance(want, dict):
        raise BuildError("template-approval.json に templates（辞書）がありません")
    names = set(want)
    if names != set(APPROVED_INPUTS):
        missing = sorted(set(APPROVED_INPUTS) - names)
        extra = sorted(names - set(APPROVED_INPUTS))
        raise BuildError(
            f"承認一覧が固定集合と一致しません（不足: {missing} / 余分: {extra}）")
    got = {}
    for name in sorted(APPROVED_INPUTS):
        expected = want[name]
        if ".." in name or name.startswith("/") or ":" in name:
            raise BuildError(f"承認対象の書き方が不正です: {name}")
        path = work / name
        if not path.is_file():
            raise BuildError(f"承認対象のファイルがありません: {name}")
        actual = template_sha(path)
        if not isinstance(expected, str) or actual != expected:
            raise BuildError(
                f"{name} が承認済みの内容と違います（承認: {expected}／実際: {actual}）。"
                f"意図した変更なら assets/data/template-approval.json を更新すること"
                f"（python scripts/build_pages_artifact.py --approve）")
        got[name] = actual
    return got


def write_approval(base: Path = BASE) -> dict:
    """承認一覧を今の中身で作り直す（★変更をレビューに載せるための道具★）。"""
    data = {
        "schema_version": APPROVAL_SCHEMA,
        "note": ("全機種に一斉に効く入力の指紋。ここと実ファイルが一致することを照合する。"
                 "中身を直したらこのファイルも更新すること（レビュー必須）。"),
        "templates": {name: template_sha(base / name) for name in sorted(APPROVED_INPUTS)},
    }
    path = base / "assets/data/template-approval.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8", newline="\n")
    return data["templates"]


# ---------------------------------------------------------------- selftest
def selftest() -> int:
    import traceback
    cases: list[tuple[str, callable]] = []

    def case(name, fn):
        cases.append((name, fn))

    # --- 承認の仕組み ---
    case("承認を作り直した直後は一致して通る",
         lambda root: bool(check_template_approved(_approval_fixture(root))))
    case("(a)-1 ひな型に固定文を足したら止める（共通原因の故障）",
         lambda root: _template_change_stopped(root))
    case("(a)-1 承認一覧から対象を外す／余分を足す／版を変えると止める",
         lambda root: _approval_scope_enforced(root))
    case("承認対象のファイルが消えたら止める",
         lambda root: _approved_file_missing_stopped(root))
    case("改行の違い（CRLF/LF）では指紋が変わらない",
         lambda root: _sha_ignores_newlines(root))
    case("(b)-3 辞書でない承認ファイルは例外にせず止める",
         lambda root: _raises(lambda: read_json_dict(_write(root / "g.json", "[]"))))
    case("(b)-3 JSONの同名キー重複を通さない",
         lambda root: _raises(lambda: read_json(_write(
             root / "d.json", '{"templates": {}, "templates": {}}'))))
    case("壊れたJSONは例外にせず止める",
         lambda root: _raises(lambda: read_json(_write(root / "b.json", "{"))))
    # --- CIで原文を出さない（ci_safe / safe_json） ---
    case("(a)-6 数字だけの偽添字も伏せる",
         lambda root: _fake_index_number_redacted())
    case("(a)-2 キー名を添字に見せかけても伏せる",
         lambda root: _fake_index_redacted())
    case("(a)-5 未知のJSONキーはCIで伏せる",
         lambda root: _unknown_key_redacted())
    case("(a)-4 指紋は実行ごとに変わる（総当たりで当てられない）",
         lambda root: _fingerprint_is_keyed())

    ok = 0
    for name, fn in cases:
        with tempfile.TemporaryDirectory(prefix="approval-selftest-") as td:
            root = Path(td)
            try:
                result = fn(root)
            except Exception:
                print(f"  ✗ {name}: 例外")
                traceback.print_exc()
                continue
        if result is True:
            ok += 1
        else:
            print(f"  ✗ {name}")
    print(f"{ok}/{len(cases)} 合格")
    return 0 if ok == len(cases) else 1


def _approval_fixture(root: Path) -> Path:
    """承認対象を全部そろえた作業コピーを作る。"""
    work = root / "repo"
    for name in sorted(APPROVED_INPUTS):
        dst = work / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes((BASE / name).read_bytes())
    (work / "assets/data").mkdir(parents=True, exist_ok=True)
    write_approval(work)
    return work


def _template_change_stopped(root: Path) -> bool:
    """ひな型に固定文を1行足したら、承認の照合で止まること。"""
    work = _approval_fixture(root)
    if not check_template_approved(work):          # まずは一致して通ること
        return False
    tpl = work / "machine.html"
    tpl.write_text(tpl.read_text(encoding="utf-8").replace(
        "</body>", "<p>未公開機の天井は999Gです</p></body>"), encoding="utf-8", newline="\n")
    return _raises(lambda: check_template_approved(work))


def _approval_scope_enforced(root: Path) -> bool:
    """承認一覧から対象を外す／余分を足す／schemaを変えると止まること。"""
    work = _approval_fixture(root)
    path = work / "assets/data/template-approval.json"
    full = json.loads(path.read_text(encoding="utf-8"))

    def with_templates(templates, schema=APPROVAL_SCHEMA):
        path.write_text(json.dumps({"schema_version": schema, "templates": templates},
                                   ensure_ascii=False), encoding="utf-8")
        return _raises(lambda: check_template_approved(work))

    dropped = {k: v for k, v in full["templates"].items() if k != "machine.html"}
    extra = {**full["templates"], "zzz-not-approved.html": "x"}
    return (with_templates(dropped)
            and with_templates(extra)
            and with_templates(full["templates"], schema="template-approval/v1")
            and with_templates({}))


def _approved_file_missing_stopped(root: Path) -> bool:
    """承認済みのファイルが実在しなくなったら止まること。"""
    work = _approval_fixture(root)
    (work / "machine.html").unlink()
    return _raises(lambda: check_template_approved(work))


def _sha_ignores_newlines(root: Path) -> bool:
    a = _write(root / "a.txt", "x")
    a.write_bytes(b"1\r\n2\r\n")
    b = root / "b.txt"
    b.write_bytes(b"1\n2\n")
    c = root / "c.txt"
    c.write_bytes(b"1\n3\n")
    return template_sha(a) == template_sha(b) != template_sha(c)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _with_ci(fn):
    was = os.environ.get("CI")
    os.environ["CI"] = "true"
    try:
        return fn()
    finally:
        if was is None:
            os.environ.pop("CI", None)
        else:
            os.environ["CI"] = was


def _fake_index_number_redacted() -> bool:
    sys.path.insert(0, str(BASE / "scripts"))
    import safe_json as _sj
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "x.json"
        f.write_text(json.dumps({"DRAFT[314159]": "a" + chr(8) + "b"}), encoding="utf-8")

        def go():
            try:
                _sj.read_json(f)
                return False
            except _sj.SafeJsonError as e:
                return "314159" not in str(e)
        return _with_ci(go)


def _fake_index_redacted() -> bool:
    sys.path.insert(0, str(BASE / "scripts"))
    import ci_safe
    out = _with_ci(lambda: ci_safe.safe_path("$.title[DRAFT_SECRET_X9]"))
    return "DRAFT_SECRET_X9" not in out


def _unknown_key_redacted() -> bool:
    sys.path.insert(0, str(BASE / "scripts"))
    import ci_safe
    out = _with_ci(lambda: ci_safe.safe_path("$.UNPUBLISHED_X9.paras[0]"))
    return "UNPUBLISHED_X9" not in out and "paras[0]" in out


def _fingerprint_is_keyed() -> bool:
    """指紋が「素のハッシュ」でないこと（候補を総当たりされない）。"""
    sys.path.insert(0, str(BASE / "scripts"))
    import ci_safe
    plain = hashlib.sha256(b"memo").hexdigest()[:12]
    # 同じ実行の中では同じ文字列が同じ指紋になること（突き合わせ用）
    same = ci_safe.fingerprint("memo") == ci_safe.fingerprint("memo")
    return ci_safe.fingerprint("memo") != plain and same


def _raises(fn) -> bool:
    try:
        fn()
    except BuildError:
        return True
    return False


USAGE = ("使い方: build_pages_artifact.py --approve | --check | --selftest\n"
         "  （公開物の組み立ては 2026-10-01 に撤去しました）")


def main() -> int:
    args = sys.argv[1:]
    if "--selftest" in args:
        return selftest()
    if "--approve" in args:
        got = write_approval()
        print("承認一覧を更新しました（差分をレビューに載せること）:")
        for name, h in sorted(got.items()):
            print(f"  {name}  {h[:16]}…")
        return 0
    if "--check" in args:
        got = check_template_approved(BASE)
        print(f"承認一覧と一致しています（{len(got)} 件）")
        return 0
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:      # ★どんな壊れ方でも診断にする★（閉鎖条件5）
        print(f"ERROR: 想定外の失敗 {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)

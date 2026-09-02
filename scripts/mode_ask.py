# -*- coding: utf-8 -*-
"""★2AIに「モード・ゾーンがあるか」を聞くための材料を用意する★

（2026-09-02・台帳#523の②・5段目）

★この道具がやること★
  1. その機種のページを**全部**集める（なな徹なら本体＋下位、最大50本）
  2. 見える文字を1つのファイルに書き出す（2AIが読む用）
  3. 証拠の集合の指紋を残す
  4. 質問文を出す

★やらないこと★＝**読み取らない・判定しない**。
  「モードがあるか」を決めるのは2AI（運営者の決まり）。

★★本文のファイルは一時的なもの★★（CLAUDE.mdの決まり・台帳#378）
  このリポジトリは公開で、なな徹の規約にも複製の条項がある。
  ★リポジトリの中に書かない★／★使い終わったら消す★（`--clean`）。

★★「無い」と書くには2サイト★★（2026-09-02・運営者の判断）
  「ある」は引用できるので1サイトでよいが、
  ★「無い」は引用できない★ので、1サイトの書き落としを見抜けない。
  ＝集める先も2サイトにする。

使い方:
    python scripts/mode_ask.py --slug hokuto            # 集めて質問を出す
    python scripts/mode_ask.py --slug hokuto --clean    # 本文の写しを消す
    python scripts/mode_ask.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                            # noqa: BLE001
    pass

import local_paths as _lp                                    # noqa: E402
import page_corpus as _pc                                    # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
CATALOGS = os.path.join(BASE, "assets", "data", "directory-catalogs.json")
# ★本文の写しはリポジトリの外★（公開されるところに他サイトの本文を置かない）
WORK = _lp.doc("mode_ask")


# ★区切りの印★（★本文の行と紛れない形にする★）
MARK = "===== うちどころ・出典 ====="


def dump_pages(pages: dict) -> str:
    """★2AIが読む形に書き出す★（★往復して元に戻ること★）

    ★通し確認で踏んだ★＝以前は「\n\n で連結」していたので、
    読み直すと本文の末尾に改行が余り、★指紋が合わなくなった★。
    """
    out = []
    for u, t in sorted((pages or {}).items()):
        out.append(f"{MARK}\n{u}\n{MARK}\n{t}")
    return "\n".join(out)


def load_pages(raw: str) -> dict:
    """★書き出したものを元に戻す★（`dump_pages` の逆）。"""
    pages = {}
    lines = str(raw or "").split("\n")
    i = 0
    while i < len(lines):
        if lines[i] == MARK and i + 2 < len(lines) and lines[i + 2] == MARK:
            url = lines[i + 1]
            j = i + 3
            body = []
            while j < len(lines):
                if (lines[j] == MARK and j + 2 < len(lines)
                        and lines[j + 2] == MARK):
                    break
                body.append(lines[j])
                j += 1
            pages[url] = "\n".join(body)
            i = j
            continue
        i += 1
    return pages


def machine_name(slug: str) -> str:
    """slug から機種名を引く（★自分で決めない★＝一覧に無ければ空）。"""
    import safe_json as _sj
    for m in _sj.read_rows(MACHINES):
        if str(m.get("slug")) == slug:
            return str(m.get("name") or "")
    return ""


def catalog_of(url: str) -> dict:
    """そのURLがどの名鑑のものか（★名鑑の決まりを持ってくる★）。"""
    with open(CATALOGS, encoding="utf-8") as f:
        dirs = json.load(f)["directories"]
    import re
    for _id, cat in dirs.items():
        pat = str(cat.get("machine_page_pattern") or "")
        if pat and re.match(pat, str(url or "")):
            return cat
    return {}


def gather(slug: str, fetch, text_of, find_urls) -> dict:
    """★その機種の証拠の集合を、複数サイトぶん組み立てる★

    返り: {"pages": {...}, "manifest": {...}, "roots": [...], "why": ...}

    ★1サイトでも「読めなかった」ら、その機種は読めていない★
      （欠けた材料で2AIに判断させない）。
    ★そもそも無いページ（404）は、そのサイトを外すだけ★
      （Codexの助言「404は否定票にしない」）。
    """
    name = machine_name(slug)
    if not name:
        return {"pages": {}, "manifest": _pc.manifest({}, False),
                "roots": [], "why": f"機種一覧に {slug} がありません"}
    urls = [u for u in find_urls(name) if catalog_of(u)]
    if not urls:
        return {"pages": {}, "manifest": _pc.manifest({}, False),
                "roots": [], "why": "名鑑の機種ページが見つかりません"}

    pages, gone, roots, bad = {}, [], [], []
    for u in urls:
        got = _pc.collect(catalog_of(u), u, fetch, text_of)
        if got["why"]:
            # ★このサイトは読めていない★＝根拠に数えない（外すだけ）
            bad.append(f"{u} … {got['why']}")
            continue
        pages.update(got["pages"])
        gone += list(got["manifest"].get("gone") or [])
        roots.append(u)
    if not roots:
        return {"pages": {}, "manifest": _pc.manifest({}, False),
                "roots": [], "why": "どのサイトも読めませんでした: "
                                    + " / ".join(bad)[:200]}
    return {"pages": pages, "manifest": _pc.manifest(pages, True, gone),
            "roots": roots, "why": "", "skipped": bad}


QUESTION = """★この機種に「モード」や「ゾーン」があるかを判断してください★

★読むもの★＝{path}
  （{n} ページ・{chars} 字。★全部読んでから答えてください★）

★決めること★（モードとゾーンを**別々に**）
  HAS            … 狙い方が変わるモード／ゾーンがある
  NONE_CONFIRMED … 全部読んだが、そういうものは無い
  （★決められないときは答えないでください★＝欄ごと出しません）

★★演出だけのモードは HAS にしない★★
  実例＝スマスロ サンダーVの「2種類の演出モード」は、
  演出が変わるだけで**天井や狙い目には関係しません**。
  ★読者が知りたいのは「いま打つべきか」★なので、
  狙い方が変わらないものは「無い」側に入れてください。

★HAS のときは引用が要ります★（その逐語が本文に在ることを機械が確かめます）
★NONE_CONFIRMED は引用できません★（存在しないものは引用できないため）
  ＝代わりに★独立した2つの出典★を機械が求めます（いま {sources} 件）

★答えの書き方★＝決定ファイルを作ってください:
{decision}
"""

DECISION_SAMPLE = """{
  "slug": "<機種>",
  "kind": "mode",                       // mode か zone
  "machine_url": "<証拠の集合の本体URL>",
  "manifest": "<この実行が出した manifest.json のパス>",
  "decisions": [
    {"judge": "claude", "state": "HAS", "why": "<15字以上の理由>"},
    {"judge": "codex",  "state": "HAS", "why": "<15字以上の理由>"}
  ],
  "quotes": [{"url": "<ページURL>", "quote": "<本文にある逐語>"}],
  "table": {"headers": ["モード", "見分け方", "次に期待できるところ"],
            "rows": [["天国", "…", "…"]]}
}"""


def run(slug: str) -> int:
    import fetched_page as _fp
    import html_check as _hc
    import new_machine_watch as _nmw
    import directory_index as _di
    import source_lineage as _sl

    def _find(name):
        return _di.found_urls(_di.find(name))

    os.makedirs(WORK, exist_ok=True)
    with _nmw.fetching("claim_material"):
        got = gather(slug, lambda u: _fp.fetch(u, "claim_material"),
                     lambda pg: _hc.visible_text(pg.cleaned_html), _find)
    if got["why"]:
        print("★集められませんでした★ " + got["why"])
        return 1

    body = dump_pages(got["pages"])
    p = os.path.join(WORK, f"{slug}_corpus.txt")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    mp = os.path.join(WORK, f"{slug}_manifest.json")
    with open(mp, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"manifest": got["manifest"], "roots": got["roots"]},
                  f, ensure_ascii=False, indent=2)

    for x in (got.get("skipped") or []):
        print("（読めないので外しました）" + x[:160])
    print(QUESTION.format(
        path=p, n=len(got["pages"]), chars=len(body),
        sources=_sl.independent(_pc.publishers(got["manifest"]["urls"])),
        decision=DECISION_SAMPLE))
    print(f"★本文の写しは使い終わったら消してください★"
          f"（python scripts/mode_ask.py --slug {slug} --clean）")
    return 0


def clean(slug: str) -> int:
    n = 0
    for suffix in ("_corpus.txt", "_manifest.json"):
        p = os.path.join(WORK, slug + suffix)
        if os.path.isfile(p):
            os.remove(p)
            n += 1
    print(f"消しました: {n} 件")
    return 0


def selftest() -> int:
    ok, cases = 0, []

    def t(name, cond):
        nonlocal ok
        cases.append(name)
        if cond:
            ok += 1
        print(("✅" if cond else "❌") + " " + name)

    NANA = "https://nana-press.com/kaiseki/machine/644/"
    CHON = "https://chonborista.com/slot/sammy-slot/12345/"

    class _P:
        def __init__(self, html):
            self.cleaned_html = html

    HTML = f'<a href="{NANA}18017/">A</a>'

    def _fetch(u):
        return _P(HTML if u == NANA else "")

    def _text(pg):
        return "本文" + pg.cleaned_html[:12]

    t("★機種一覧に無ければ集めない★",
      gather("zzz_no_such_machine", _fetch, _text, lambda n: [])["why"] != "")

    # ★名鑑の決まりが引けることを、本物の設定で確かめる★
    t("　なな徹の機種ページを名鑑と結び付けられる",
      bool(catalog_of(NANA).get("sub_page_pattern")))
    t("　ちょんぼりすたの機種ページも結び付けられる",
      bool(catalog_of(CHON).get("machine_page_pattern")))
    t("　関係ないURLはどの名鑑にも結び付かない",
      catalog_of("https://example.com/x") == {})

    t("　質問文に「演出だけのモードは HAS にしない」が入っている",
      "演出" in QUESTION)
    t("★質問文が「決められないときは答えない」と言っている★",
      "決められないときは答えない" in QUESTION)
    t("　決定ファイルの見本に判断者が2人ある",
      DECISION_SAMPLE.count('"judge"') == 2)

    # ★★往復して元に戻ること★★（2026-09-02・通し確認が踏んだ）
    #   ★1文字でもずれると、指紋が合わず何も控えられない★
    for name, sample in (
            ("ふつうの本文", {"https://a/": "あいう\nえお", "https://b/": "かき"}),
            ("末尾が改行", {"https://a/": "あいう\n"}),
            ("空行を含む", {"https://a/": "あ\n\n\nい"}),
            ("本文が空", {"https://a/": ""}),
            ("区切りに似た行を含む",
             {"https://a/": "=====\n=== うちどころ ===\n本文"}),
            ("1ページだけ", {"https://a/": "ひとつ"}),
    ):
        t(f"★書いて読み直すと元に戻る★（{name}）",
          load_pages(dump_pages(sample)) == sample)

    print(f"\n{ok}/{len(cases)} 合格")
    return 0 if ok == len(cases) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="モード・ゾーンを2AIに聞く材料を用意する")
    ap.add_argument("--slug")
    ap.add_argument("--clean", action="store_true",
                    help="本文の写しを消す（★使い終わったら必ず★）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        raise SystemExit(selftest())
    if not a.slug:
        print("★--slug が要ります★")
        raise SystemExit(1)
    raise SystemExit(clean(a.slug) if a.clean else run(a.slug))

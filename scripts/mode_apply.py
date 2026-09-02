# -*- coding: utf-8 -*-
"""★控えた判断を、実際の記事に書き込む★（2026-09-02・台帳#523の②・7段目）

★Codexのレビュー36の指摘★＝
「`mode_corpus` は読み口しかなく、材料へ入れる生産側がありません」
＝★仕組みは全部あるのに、記事に届く道が無かった★。

★この道具がやること★
  1. いまの証拠の集合を作り直す（★本体1ページだけ★＝軽い確認）
  2. 控えが使えるか確かめる（指紋が違えば使わない）
  3. 記事の節を作って、正しい位置へ入れる／消す

★やらないこと★＝判断しない。文章を書かない。
  出す文言は `build_new_article` が持つ決まり文句だけ。

★書き込むのは1機種だけ★＝`--slug` が要る（罠㉛）。
★既定は書かない★＝`--apply` を付けたときだけ書く。

使い方:
    python scripts/mode_apply.py --slug monkeyv           # 何が起きるか見る
    python scripts/mode_apply.py --slug monkeyv --apply   # 実際に書く
    python scripts/mode_apply.py --selftest
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

import build_new_article as _ba                              # noqa: E402
import mode_verdict as _mv                                   # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DETAILS = os.path.join(BASE, "assets", "data", "machine-details")


def plan(detail: dict, box) -> dict:
    """★記事をどう直すか★を決める（書き込まない）。

    box … `build_new_article.mode_section` が返した節、または None

    返り: {"action": …, "sections": …, "why": …}
      keep   … 変えない
      insert … 入れる
      update … 差し替える
      remove … 消す（★控えが使えなくなった＝欄ごと出さない★）
    """
    if not isinstance(detail, dict) or not isinstance(
            detail.get("sections"), list):
        return {"action": "", "sections": None, "why": "記事の形が違います"}
    secs = list(detail["sections"])
    at = next((i for i, s in enumerate(secs)
               if isinstance(s, dict) and s.get("title") == _ba.MODE_TITLE),
              -1)
    if box is None:
        if at < 0:
            return {"action": "keep", "sections": secs, "why": "控えがありません"}
        # ★控えが使えなくなったら消す★＝古い判断を残さない
        return {"action": "remove", "sections": secs[:at] + secs[at + 1:],
                "why": "控えが使えなくなりました（欄ごと出しません）"}
    if at >= 0:
        if secs[at] == box:
            return {"action": "keep", "sections": secs, "why": "変わりません"}
        secs[at] = box
        return {"action": "update", "sections": secs, "why": "中身が変わりました"}
    # ★入れる位置★＝「基本スペック」の次（無ければ決まった位置）
    titles = [s.get("title") if isinstance(s, dict) else "" for s in secs]
    pos = next((p for ti, p in _ba.OPTIONAL_SECTIONS
                if ti == _ba.MODE_TITLE), 2)
    if "基本スペック" in titles:
        pos = titles.index("基本スペック") + 1
    return {"action": "insert", "sections": secs[:pos] + [box] + secs[pos:],
            "why": "控えができました"}


def corpus_now(slug: str):
    """★いまの証拠の集合★（本体1ページだけで軽く確かめる）。

    返り: (集合, 理由)  … 使えなければ集合は None
    """
    import page_corpus as _pc
    import fetched_page as _fp
    import html_check as _hc
    import new_machine_watch as _nmw
    import mode_ask as _ma

    mp = os.path.join(_ma.WORK, f"{slug}_manifest.json")
    if not os.path.isfile(mp):
        return None, "証拠の記録がありません（先に mode_ask を流してください）"
    with open(mp, encoding="utf-8") as f:
        man = json.load(f)
    saved = man.get("manifest")
    if not isinstance(saved, dict):
        return None, "証拠の記録の形が違います"
    if man.get("slug") != slug:
        return None, f"証拠の記録は別の機種のものです（{man.get('slug')}）"

    with _nmw.fetching("claim_material"):
        for root in (man.get("roots") or []):
            cat = _ma.catalog_of(root)
            got, why = _pc.quick_check(
                cat, root, lambda u: _fp.fetch(u, "claim_material"),
                lambda pg: _hc.visible_text(pg.cleaned_html), saved)
            if got != "SAME":
                return None, f"{root} … {why or got}"
    return saved, ""


def run(slug: str, apply: bool) -> int:
    p = os.path.join(DETAILS, f"{slug}.json")
    if not os.path.isfile(p):
        print(f"★記事データがありません★（{p}）")
        return 1
    corpus, why = corpus_now(slug)
    if corpus is None:
        print(f"★控えを使えません★ {why}")
        box = None
    else:
        box = _ba.mode_box_for(slug, corpus)
    with open(p, encoding="utf-8") as f:
        detail = json.load(f)
    got = plan(detail, box)
    if not got["action"]:
        print("★" + got["why"] + "★")
        return 1
    print(f"{slug}: {got['action']}（{got['why']}）")
    if got["action"] == "keep":
        return 0
    if not apply:
        print("★書いていません★（実際に書くなら --apply）")
        return 0
    out = dict(detail)
    out["sections"] = got["sections"]
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, p)
    # ★書いたあと読み直して確かめる★
    with open(p, encoding="utf-8") as f:
        again = json.load(f)
    if again["sections"] != got["sections"]:
        print("★書いた内容と読み直した内容が違います★")
        return 1
    print("書きました。★ページの作り直しを忘れないこと★"
          f"（build_machine_pages.py --legacy --slug {slug}）")
    return 0


def selftest() -> int:
    ok, cases = 0, []

    def t(name, cond):
        nonlocal ok
        cases.append(name)
        if cond:
            ok += 1
        print(("✅" if cond else "❌") + " " + name)

    BOX = {"title": _ba.MODE_TITLE, "body": ["ありません。"]}
    D = {"sections": [{"title": "天井・恩恵"}, {"title": "基本スペック"},
                      {"title": "当サイトの狙い目"}]}

    g = plan(D, BOX)
    t("★控えができたら、基本スペックの次に入れる★",
      g["action"] == "insert"
      and [s["title"] for s in g["sections"]][:3]
      == ["天井・恩恵", "基本スペック", _ba.MODE_TITLE])

    D2 = {"sections": D["sections"][:2] + [BOX] + D["sections"][2:]}
    t("　同じ中身なら変えない", plan(D2, BOX)["action"] == "keep")

    NEW = {"title": _ba.MODE_TITLE, "body": ["別の中身。"]}
    g2 = plan(D2, NEW)
    t("　中身が変わったら差し替える",
      g2["action"] == "update" and g2["sections"][2] == NEW)

    g3 = plan(D2, None)
    t("★★控えが使えなくなったら、節を消す★★"
      "／★これが無いと、古い「ありません」が残り続ける★",
      g3["action"] == "remove"
      and _ba.MODE_TITLE not in [s.get("title") for s in g3["sections"]])

    t("　もともと無くて控えも無ければ、何もしない",
      plan(D, None)["action"] == "keep")

    D3 = {"sections": [{"title": "天井・恩恵"}, {"title": "当サイトの狙い目"}]}
    g4 = plan(D3, BOX)
    t("　基本スペックが無い記事でも、決まった位置に入れる",
      g4["action"] == "insert"
      and [s["title"] for s in g4["sections"]][2] == _ba.MODE_TITLE)

    t("　記事の形が違えば断る", plan({"sections": "x"}, BOX)["action"] == "")

    print(f"\n{ok}/{len(cases)} 合格")
    return 0 if ok == len(cases) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="控えた判断を記事に書き込む（★1機種ずつ★）")
    ap.add_argument("--slug")
    ap.add_argument("--apply", action="store_true",
                    help="実際に書く（★既定は書かない★）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        raise SystemExit(selftest())
    if not a.slug:
        print("★--slug が要ります★（★どこへ書くかを必ず言わせる★・罠㉛）")
        raise SystemExit(1)
    raise SystemExit(run(a.slug, a.apply))

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


def corpus_now(slug: str, quick=None, fetch=None, text_of=None):
    """★いまの証拠の集合★（★サイトごとに、本体1ページだけで確かめる★）

    返り: (状態, 集合, 理由)
      "VALID"      … 控えを反映してよい
      "STALE"      … 証拠が変わった＝★節を消す★
      "UNREADABLE" … ★いま確認できないだけ＝何も書かない★

    ★★「確認できない」と「控えが無効」を分ける★★
      （2026-09-02・Codexのレビュー37の重大②）
      ★直す前は両方 None にして節を消していた★＝
      `--clean` は記録も消すので、★普段の手順で必ず節が消えた★。
      一時的な503でも同じだった。
    """
    import page_corpus as _pc
    import mode_ask as _ma

    mp = os.path.join(_ma.WORK, f"{slug}_manifest.json")
    if not os.path.isfile(mp):
        # ★記録が無いのは「確認できない」★（控えが無効になったのではない）
        return "UNREADABLE", None, "証拠の記録がありません（先に mode_ask を）"
    try:
        with open(mp, encoding="utf-8") as f:
            man = json.load(f)
    except Exception as e:                                   # noqa: BLE001
        return "UNREADABLE", None, f"証拠の記録を読めません（{type(e).__name__}）"
    if not isinstance(man, dict):
        return "UNREADABLE", None, "証拠の記録の形が違います"
    saved = man.get("manifest")
    if not isinstance(saved, dict):
        return "UNREADABLE", None, "証拠の記録の形が違います"
    if man.get("slug") != slug:
        return "UNREADABLE", None, \
            f"証拠の記録は別の機種のものです（{man.get('slug')}）"

    roots = man.get("roots")
    per = man.get("per_root")
    # ★★本体URLが無ければ断る★★（Codexのレビュー37の重大③）
    #   ★直す前は0回のループを抜けて、1ページも確認せず古い控えを採用した★
    if not isinstance(roots, list) or not roots:
        return "UNREADABLE", None, "証拠の記録に本体URLがありません"
    if not isinstance(per, dict) or set(per) != {str(x) for x in roots}:
        return "UNREADABLE", None, \
            "証拠の記録に、サイトごとの記録がそろっていません"

    if quick is None:
        import fetched_page as _fp
        import html_check as _hc
        import new_machine_watch as _nmw
        fetch = fetch or (lambda u: _fp.fetch(u, "claim_material"))
        text_of = text_of or (lambda pg: _hc.visible_text(pg.cleaned_html))
        quick = _pc.quick_check
        ctx = _nmw.fetching("claim_material")
    else:
        import contextlib as _cl
        ctx = _cl.nullcontext()

    with ctx:
        for root in roots:
            # ★★サイトごとの記録と比べる★★（重大①）
            #   ★合体した記録と比べると必ず「変わった」になる★
            got, why = quick(_ma.catalog_of(root), root, fetch, text_of,
                             per[str(root)])
            if got == "CHANGED":
                return "STALE", None, f"{root} … {why}"
            if got != "SAME":
                # ★いま読めないだけ★＝何も書かない
                return "UNREADABLE", None, f"{root} … {why or got}"
    return "VALID", saved, ""


def run(slug: str, apply: bool) -> int:
    p = os.path.join(DETAILS, f"{slug}.json")
    if not os.path.isfile(p):
        print(f"★記事データがありません★（{p}）")
        return 1
    state, corpus, why = corpus_now(slug)
    if state == "UNREADABLE":
        # ★★いま確認できないだけ★★＝★記事を1文字も触らない★
        #   （2026-09-02・Codexのレビュー37の重大②）
        print(f"★いま確認できません★ {why}")
        print("★記事は触っていません★")
        return 1
    box = None if state == "STALE" else _ba.mode_box_for(slug, corpus)
    if state == "STALE":
        print(f"★証拠が変わりました★ {why}")
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

    # ★★ここから corpus_now★★（2026-09-02・Codexのレビュー37）
    #   ★直す前は plan() しか試しておらず、直した3件を1つも見ていなかった★
    import json as _js
    import tempfile as _tf
    import mode_ask as _ma
    import page_corpus as _pc

    _keep_work = _ma.WORK
    _ma.WORK = _tf.mkdtemp(prefix="mapply_")
    NANA = "https://nana-press.com/kaiseki/machine/644/"
    CHON = "https://chonborista.com/slot/sammy-slot/12345/"

    def _write(man):
        with open(os.path.join(_ma.WORK, "zzz_manifest.json"), "w",
                  encoding="utf-8", newline="\n") as f:
            _js.dump(man, f, ensure_ascii=False)

    _pn = _pc.manifest({NANA: "あ"}, True)
    _pc2 = _pc.manifest({CHON: "い"}, True)
    _all = _pc.manifest({NANA: "あ", CHON: "い"}, True)
    _good = {"manifest": _all, "roots": [NANA, CHON], "slug": "zzz",
             "per_root": {NANA: _pn, CHON: _pc2}}

    def _quick(cat, root, fetch, text_of, saved):
        # ★渡された記録が、そのサイトのものかを見る★
        urls = set(saved.get("urls") or [])
        return ("SAME", "") if urls == {_pc._norm(root)} else \
            ("CHANGED", f"顔ぶれが違います（{sorted(urls)}）")

    try:
        _write(_good)
        st, _c, _w = corpus_now("zzz", quick=_quick, fetch=lambda u: None,
                                text_of=lambda p2: "")
        t("★★サイトごとの記録と比べる（合体した記録では必ず"
          "「変わった」になる）★★", st == "VALID")

        # ★合体した記録を渡していた頃の姿★（対照実験）
        _bad = dict(_good)
        _bad["per_root"] = {NANA: _all, CHON: _all}
        _write(_bad)
        t("　合体した記録だと「変わった」になる（直す前の姿）",
          corpus_now("zzz", quick=_quick, fetch=lambda u: None,
                     text_of=lambda p2: "")[0] == "STALE")

        # ★★記録が無いのは「確認できない」★★（節を消さない）
        _write(_good)
        os.remove(os.path.join(_ma.WORK, "zzz_manifest.json"))
        t("★★記録が無いのは「確認できない」★★"
          "／★直す前は節が消えた（--clean は記録も消すので普段の手順で起きた）★",
          corpus_now("zzz", quick=_quick, fetch=lambda u: None,
                     text_of=lambda p2: "")[0] == "UNREADABLE")

        # ★★一時的に読めないのも「確認できない」★★
        _write(_good)

        def _busy(cat, root, fetch, text_of, saved):
            return "UNREADABLE", "HTTP 503"

        t("★一時的に読めないのも「確認できない」★（節を消さない）",
          corpus_now("zzz", quick=_busy, fetch=lambda u: None,
                     text_of=lambda p2: "")[0] == "UNREADABLE")

        # ★★本体URLが空★★（1ページも確認せず古い控えを採るのを防ぐ）
        _write({"manifest": _all, "roots": [], "slug": "zzz", "per_root": {}})
        t("★★本体URLが空なら断る★★"
          "／★直す前は1ページも確認せず、古い控えを記事へ反映できた★",
          corpus_now("zzz", quick=_quick, fetch=lambda u: None,
                     text_of=lambda p2: "")[0] == "UNREADABLE")

        _write({"manifest": _all, "roots": [NANA, CHON], "slug": "zzz",
                "per_root": {NANA: _pn}})
        t("　サイトごとの記録がそろっていなければ断る",
          corpus_now("zzz", quick=_quick, fetch=lambda u: None,
                     text_of=lambda p2: "")[0] == "UNREADABLE")

        _write({"manifest": _all, "roots": [NANA], "slug": "別の機種",
                "per_root": {NANA: _pn}})
        t("　記録が別の機種のものなら断る",
          corpus_now("zzz", quick=_quick, fetch=lambda u: None,
                     text_of=lambda p2: "")[0] == "UNREADABLE")

        # ★証拠が変わったときだけ STALE★
        _write(_good)

        def _changed(cat, root, fetch, text_of, saved):
            return "CHANGED", "下位ページが増えました"

        t("　証拠が変わったときは STALE（節を消す）",
          corpus_now("zzz", quick=_changed, fetch=lambda u: None,
                     text_of=lambda p2: "")[0] == "STALE")

        # ★★後片づけ（--clean）のあとでも記事へ反映できる★★
        #   （2026-09-02・Codexのレビュー38）
        #   ★直す前は記録まで消していた★ので、
        #   「誤って節を消す」は直った代わりに
        #   ★記事へ永久に届かなくなった★（節を入れる・更新する・消すが全部不可）。
        #   ★私の試験はそこを見ていなかった★＝
        #   「記録が無ければ止まる」までしか試していなかった。
        _write(_good)
        with open(os.path.join(_ma.WORK, "zzz_corpus.txt"), "w",
                  encoding="utf-8") as _f:
            _f.write("本文の写し")
        _ma.clean("zzz")
        t("★後片づけで本文の写しは消える★",
          not os.path.isfile(os.path.join(_ma.WORK, "zzz_corpus.txt")))
        t("★★後片づけのあとでも記事へ反映できる★★"
          "／★記録まで消すと、節を入れる・更新する・消すが全部できなくなる★",
          corpus_now("zzz", quick=_quick, fetch=lambda u: None,
                     text_of=lambda p2: "")[0] == "VALID")
        _ma.purge("zzz")
        t("　記録ごと消す操作は別にある（普段は使わない）",
          corpus_now("zzz", quick=_quick, fetch=lambda u: None,
                     text_of=lambda p2: "")[0] == "UNREADABLE")
    finally:
        import shutil as _sh
        _sh.rmtree(_ma.WORK, ignore_errors=True)
        _ma.WORK = _keep_work

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

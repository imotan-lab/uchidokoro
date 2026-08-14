"""at_spec_lookup.py — ATの仕様（純増・1セットのG数）を「モードごと」に採る。

★条件（どのモードか）を必ず持つ★
  同じページに「通常AT 純増約2.8枚」と「上位AT 純増約5.0枚」が並ぶ。
  モードを落とした純増は、それ自体が誤情報になる
  （`collection-rules.json` の conditions_required に登録済み）。

★2サイトで形が違う（実データで確認）★
  P-WORLD       … 文章「AT『夢娘ライブ』は1セット100G継続、純増約2.8枚のセット数管理型。」
  ちょんぼりすた … 表「継続G数→1セット100G」「純増→約2.8枚/G」＋見出しがモード名

★採用の条件★
  独立2出典で **モード名・純増・1セットのG数がすべて一致** したときだけ採用。

使い方:
    python scripts/at_spec_lookup.py --name "Lすーぱぁびん娘" --url <URL1> --url <URL2>
    python scripts/at_spec_lookup.py --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import html_tables as _ht            # noqa: E402
import model_code_lookup as _mc       # noqa: E402
import new_machine_watch as _w        # noqa: E402
import user_area as _ua              # noqa: E402
import spec_lookup as _sl             # noqa: E402

# ★モードの区別★ これが違えば別の事実
#   「上位」が付くかどうかで別物として扱う（AT と 上位AT は別）。
UPPER_WORDS = ("上位AT", "上位")


def _norm(s) -> str:
    return unicodedata.normalize("NFKC", " ".join(str(s or "").split()))


def mode_of(text: str) -> str:
    """その記述がどのモードの話か。★分からなければ空を返す（採らない）★"""
    t = _norm(text)
    if any(w in t for w in UPPER_WORDS):
        return "UPPER_AT"
    if "AT" in t:
        return "MAIN_AT"
    return ""


# 文章から採る形（★許可した言い回しだけ★）
_SENT = re.compile(
    r"(?P<mode>[^。]{0,24}?AT[^。]{0,20}?)は1セット\s*(?P<games>\d{2,4})\s*G継続[、,]\s*"
    r"純増約\s*(?P<net>\d+(?:\.\d+)?)\s*枚")
# 表から採る形（見出しの並び）
_TBL_GAMES = ("継続G数",)
_TBL_NET = ("純増",)


def from_sentences(text: str) -> list:
    out = []
    for m in _SENT.finditer(_norm(text)):
        mode = mode_of(m.group("mode"))
        if not mode:
            continue
        out.append({"mode": mode, "games": int(m.group("games")),
                    "net": float(m.group("net")), "raw": m.group(0)[:110]})
    return out


def from_tables(html: str) -> list:
    """表を1区画ずつ読む。★モード名はその表の直前の見出しからだけ取る★

    ★2026-07-31・CZ側と同じ穴を自分で再現して作り直した★
      行の列にしてから見出しをさかのぼると、間に別の表が挟まったとき
      **通常ATの純増を上位ATの値として採って**しまう（実際に再現）。
      表ごとに切り出せば、値も見出しも同じ区画の中だけで決まる。
    """
    out = []
    for tb in _ht.tables(html):
        if tb.get("has_span"):
            continue          # ★多段見出し（rowspan/colspan）は列がずれる＝不採用★
        mode = mode_of(tb["title"])
        if not mode:
            continue          # ★どのモードの表か分からなければ採らない★
        mg = re.match(r"^1セット\s*(\d{2,4})\s*G$",
                      _norm(_ht.value_of(tb["pairs"], _TBL_GAMES)))
        mn = re.match(r"^約?\s*(\d+(?:\.\d+)?)\s*枚(?:/G)?$",
                      _norm(_ht.value_of(tb["pairs"], _TBL_NET)))
        if not (mg and mn):
            continue
        out.append({"mode": mode, "games": int(mg.group(1)), "net": float(mn.group(1)),
                    "raw": f"{tb['title'][:20]} / 1セット{mg.group(1)}G / 純増{mn.group(1)}枚"})
    return out


def read_page(url: str, official_name: str) -> dict:
    out = {"url": url, "host": url.split("/")[2].lower().removeprefix("www."),
           "ok": False, "reason": "", "specs": []}
    try:
        html = _w._get(url)
        # ★取ってきた直後に、投稿欄・AI欄を箱ごと落とす★（2026-08-14・台帳#345）
        #   ここを通さないと、**表を生のHTMLから読む処理**に読者の書き込みが入る。
        #   落としきれないときは例外＝そのページは使わない（fail-closed）。
        html = _ua.clean_html(html, url)
    except Exception as e:
        out["reason"] = f"取得できません: {e}"
        return out
    # ★材料の照合も厳格側で★（2026-08-02・Codex55回目。緩い側だと
    #   「機種名 新台 BLACK」のような未知の版名が装飾語の後ろで通り、
    #   別バージョンの値を2媒体一致で採用できた）
    ok, why = _mc.page_is_machine(html, official_name,
                                  strict_all_tail=True)
    if not ok:
        out["reason"] = why
        return out
    text = _w._visible_text(html)
    seen, got = set(), []
    for c in from_sentences(text) + from_tables(html):
        key = (c["mode"], c["games"], c["net"])
        if key in seen:
            continue
        seen.add(key)
        got.append(c)
    out["specs"], out["ok"], out["reason"] = got, True, "OK"
    return out


def compare(pages: list) -> dict:
    """★モード・純増・G数がすべて一致したものだけ採る★"""
    votes: dict = {}
    for p in pages:
        if not p.get("ok"):
            continue
        lin = _sl.vote_lineage(p["host"])
        if not lin:      # ★登録されていないサイトは票に数えない★
            continue
        for c in p["specs"]:
            k = json.dumps({x: c[x] for x in ("mode", "games", "net")}, sort_keys=True)
            votes.setdefault(k, {"sample": c, "sources": set()})
            votes[k]["sources"].add(lin)
    adopted, need_third = [], []
    by_mode: dict = {}
    for k, v in votes.items():
        by_mode.setdefault(v["sample"]["mode"], []).append(v)
    for mode, items in by_mode.items():
        # ★票の数は source_lineage が決める★（2026-08-14・依頼192のP1）
        agreed = [v for v in items if _sl._indep(v["sources"]) >= 2]
        # ★反対票が1票でもあれば採らない★（2026-08-02・Codex56回目）
        if len(agreed) == 1 and len(items) == 1:
            c = dict(agreed[0]["sample"])
            c["sources"] = sorted(agreed[0]["sources"])
            adopted.append(c)
        else:
            need_third.append({
                "mode": mode,
                "why": ("出典が食い違っています" if len(items) > 1
                        else "1つの出典にしかありません"),
                "candidates": [{"games": v["sample"]["games"], "net": v["sample"]["net"],
                                "sources": sorted(v["sources"])} for v in items]})
    return {"adopted": adopted, "need_third": need_third}


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []
    nl = chr(10)

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    S = ("AT「夢娘ライブ」は1セット100G継続、純増約2.8枚のセット数管理型。"
         "上位AT「クライMAXライブ」は1セット100G継続、純増約5.0枚のセット数管理型。")
    got = from_sentences(S)
    main = next((x for x in got if x["mode"] == "MAIN_AT"), None)
    up = next((x for x in got if x["mode"] == "UPPER_AT"), None)
    t("★★同じページの通常ATと上位ATを別々に採る★★（混ぜたら誤情報）",
      main and up and main["net"] == 2.8 and up["net"] == 5.0)
    t("　1セットのG数も一緒に採る", main["games"] == 100 and up["games"] == 100)
    t("★どのモードか分からない記述は採らない★",
      from_sentences("これは1セット100G継続、純増約2.8枚です。") == [])

    H = ('<h3>AT「夢娘ライブ」</h3><table>'
         "<tr><th>タイプ</th><td>セット数管理</td></tr>"
         "<tr><th>継続G数</th><td>1セット100G</td></tr>"
         "<tr><th>純増</th><td>約2.8枚/G</td></tr></table>")
    tb = from_tables(H)
    t("★★表からも同じ形で採れる（見出しからモードを取る）★★",
      tb and tb[0]["mode"] == "MAIN_AT" and tb[0]["net"] == 2.8)
    t("★★見出しにモードが無ければ採らない★★",
      from_tables("<h3>なにかの表</h3><table>"
                  "<tr><th>継続G数</th><td>1セット100G</td></tr>"
                  "<tr><th>純増</th><td>約2.8枚/G</td></tr></table>") == [])
    t("　純増が取れなければ採らない",
      from_tables('<h3>AT「x」</h3><table>'
                  "<tr><th>継続G数</th><td>1セット100G</td></tr>"
                  "<tr><th>備考</th><td>なし</td></tr></table>") == [])
    t("　上位ATは上位として採る",
      from_tables('<h3>上位AT「クライMAXライブ」</h3><table>'
                  "<tr><th>継続G数</th><td>1セット100G</td></tr>"
                  "<tr><th>純増</th><td>約5.0枚/G</td></tr></table>"
                  )[0]["mode"] == "UPPER_AT")
    t("★★別の表の値を、上のATの見出しで採らない★★（実際に再現した）",
      [(c["mode"], c["net"]) for c in from_tables(
          '<h3>上位AT「クライMAXライブ」</h3><table>'
          "<tr><th>継続G数</th><td>1セット100G</td></tr>"
          "<tr><th>純増</th><td>約5.0枚/G</td></tr></table>"
          "<h3>ボーナス</h3><table>"
          "<tr><th>継続G数</th><td>1セット100G</td></tr>"
          "<tr><th>純増</th><td>約2.8枚/G</td></tr></table>")]
      == [("UPPER_AT", 5.0)])

    _VIS = ('<h3>AT「本物」</h3><table><tr><th>継続G数</th><td>1セット100G</td></tr>'
            "<tr><th>純増</th><td>約2.8枚/G</td></tr></table>")
    _HID = ('<div hidden><h3>上位AT「旧仕様」</h3><table>'
            "<tr><th>継続G数</th><td>1セット100G</td></tr>"
            "<tr><th>純増</th><td>約9.9枚/G</td></tr></table></div>")
    t("★★非表示の表の値を採らない★★（読者に見えない旧値が票になれた・Codex63回目）",
      [(c["mode"], c["net"]) for c in from_tables(_VIS + _HID)]
      == [("MAIN_AT", 2.8)]
      and from_tables(_HID) == [])

    A = {"url": "https://www.p-world.co.jp/x", "host": "p-world.co.jp", "ok": True,
         "specs": [{"mode": "MAIN_AT", "games": 100, "net": 2.8, "raw": ""}]}
    B = {"url": "https://chonborista.com/y", "host": "chonborista.com", "ok": True,
         "specs": [{"mode": "MAIN_AT", "games": 100, "net": 2.8, "raw": ""}]}
    t("★2出典一致なら採用★", len(compare([A, B])["adopted"]) == 1)
    C = {**B, "specs": [{**B["specs"][0], "net": 3.0}]}
    t("★値が違えば採らない★", not compare([A, C])["adopted"])
    D = {**B, "specs": [{**B["specs"][0], "mode": "UPPER_AT"}]}
    t("★★モードが違えば別物として扱う（同じ値でも合算しない）★★",
      not compare([A, D])["adopted"] and len(compare([A, D])["need_third"]) == 2)
    t("　同じ運営元の2ページを2票と数えない",
      not compare([A, {**B, "host": "p-world.co.jp"}])["adopted"])
    E = {"url": "https://p-town.dmm.com/z", "host": "p-town.dmm.com", "ok": True,
         "specs": [{"mode": "MAIN_AT", "games": 100, "net": 3.0, "raw": ""}]}
    t("★★2票一致でも反対票が1票あれば採らない★★（Codex56回目）",
      not compare([A, B, E])["adopted"])

    ng = [n for n, ok in results if not ok]
    print(f"{nl}{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--name")
    ap.add_argument("--url", action="append")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not (args.name and args.url):
        ap.print_help()
        return 0
    pages = [read_page(u, args.name) for u in args.url]
    for p in pages:
        print(f"{p['host']:20} {p['reason']:20} AT仕様 {len(p['specs'])} 件")
        for c in p["specs"]:
            jp = "通常AT" if c["mode"] == "MAIN_AT" else "上位AT"
            print(f"     {jp}: 1セット{c['games']}G / 純増約{c['net']}枚")
    print(chr(10) + json.dumps(compare(pages), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(f"★想定外の失敗 {type(e).__name__}: {e}★")
        raise SystemExit(1)

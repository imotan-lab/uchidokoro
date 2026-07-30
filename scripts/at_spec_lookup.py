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

import model_code_lookup as _mc       # noqa: E402
import new_machine_watch as _w        # noqa: E402
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


def from_table(lines: list) -> list:
    """表から採る。★モード名は見出しをさかのぼって探す★

    「継続G数 → 1セット100G」「純増 → 約2.8枚/G」が並ぶが、
    どのモードの表かは**上の見出し**にしか書かれていない。
    さかのぼって見つからなければ採らない（モード不明の値は載せない）。
    """
    out = []
    for i, line in enumerate(lines):
        if line not in _TBL_GAMES or i + 1 >= len(lines):
            continue
        mg = re.match(r"^1セット\s*(\d{2,4})\s*G$", _norm(lines[i + 1]))
        if not mg:
            continue
        net = None
        for j in range(i + 2, min(i + 6, len(lines))):
            if lines[j] in _TBL_NET and j + 1 < len(lines):
                mn = re.match(r"^約?\s*(\d+(?:\.\d+)?)\s*枚(?:/G)?$", _norm(lines[j + 1]))
                if mn:
                    net = float(mn.group(1))
                break
        if net is None:
            continue          # ★純増が取れなければ採らない★
        mode = ""
        for k in range(i - 1, max(i - 12, -1), -1):   # 見出しをさかのぼる
            mode = mode_of(lines[k])
            if mode:
                break
        if not mode:
            continue          # ★どのモードか分からなければ採らない★
        out.append({"mode": mode, "games": int(mg.group(1)), "net": net,
                    "raw": f"{lines[i+1]} / 純増{net}枚"})
    return out


def read_page(url: str, official_name: str) -> dict:
    out = {"url": url, "host": url.split("/")[2].lower().removeprefix("www."),
           "ok": False, "reason": "", "specs": []}
    try:
        html = _w._get(url)
    except Exception as e:
        out["reason"] = f"取得できません: {e}"
        return out
    ok, why = _mc.page_is_machine(html, official_name)
    if not ok:
        out["reason"] = why
        return out
    text = _w._visible_text(html)
    lines = [x.strip() for x in text.splitlines()]
    seen, got = set(), []
    for c in from_sentences(text) + from_table(lines):
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
        lin = _sl._lineage(p["host"])
        for c in p["specs"]:
            k = json.dumps({x: c[x] for x in ("mode", "games", "net")}, sort_keys=True)
            votes.setdefault(k, {"sample": c, "sources": set()})
            votes[k]["sources"].add(lin)
    adopted, need_third = [], []
    by_mode: dict = {}
    for k, v in votes.items():
        by_mode.setdefault(v["sample"]["mode"], []).append(v)
    for mode, items in by_mode.items():
        agreed = [v for v in items if len(v["sources"]) >= 2]
        if len(agreed) == 1:
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

    L = ["AT「夢娘ライブ」", "タイプ", "セット数管理", "継続G数", "1セット100G",
         "純増", "約2.8枚/G"]
    tb = from_table(L)
    t("★★表からも同じ形で採れる（見出しからモードを取る）★★",
      tb and tb[0]["mode"] == "MAIN_AT" and tb[0]["net"] == 2.8)
    t("★★見出しにモードが無ければ採らない★★",
      from_table(["なにかの表", "継続G数", "1セット100G", "純増", "約2.8枚/G"]) == [])
    t("　純増が取れなければ採らない",
      from_table(["AT「x」", "継続G数", "1セット100G", "備考", "なし"]) == [])
    LU = ["上位AT「クライMAXライブ」", "継続G数", "1セット100G", "純増", "約5.0枚/G"]
    t("　上位ATは上位として採る", from_table(LU)[0]["mode"] == "UPPER_AT")

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

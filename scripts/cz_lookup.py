"""cz_lookup.py — CZ（チャンスゾーン）を名前ごとに採る。

★CZ名で束ねる★
  1機種に複数のCZがあり、継続G数も期待度も違う。
  「CZの期待度は約40%」だけでは、どのCZの話か分からず誤情報になる。
  → **CZ名・継続G数・期待度** がそろって初めて1つの事実として採る。

★2サイトで形が違う（実データで確認）★
  P-WORLD       … 文章「すぱ娘チャレンジは4G+α継続、成功期待度は約40%。」
  ちょんぼりすた … 表「CZ「すぱ娘チャレンジ」→ 継続G数 4G+α → 期待度 約40%」

★期待度の書き方が違うものは一致にしない★
  「約50%」と「50%以上」は意味の幅が違う（collection-rules に登録済み）。
  そのまま比べるので、違えば一致しない＝採らない。

使い方:
    python scripts/cz_lookup.py --name "Lすーぱぁびん娘" --url <URL1> --url <URL2>
    python scripts/cz_lookup.py --selftest
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

# CZ名として認める形（★短い固有名だけ★・文を拾わない）
_CZ_NAME_OK = re.compile(r"^[ぁ-んァ-ヶ一-龥A-Za-z0-9ー・]{2,20}$")
# 継続G数（「4G+α」「7G」「5G or 10G」）
_GAMES_OK = re.compile(r"^(\d{1,3})\s*G(\+\s*α)?$")
# 期待度（★書き方をそのまま持つ★＝「約50%」と「50%以上」を混ぜない）
_RATE_OK = re.compile(r"^(約\s*)?\d{1,3}(\.\d)?\s*%(以上|超)?$")

_SENT = re.compile(
    r"(?P<name>[ぁ-んァ-ヶ一-龥A-Za-z0-9ー・]{2,20}?)は"
    r"(?P<games>\d{1,3}\s*G(?:\+\s*α)?)継続[、,]\s*"
    r"(?:成功)?期待度は(?P<rate>約?\s*\d{1,3}(?:\.\d)?\s*%(?:以上|超)?)")

_TBL_GAMES = ("継続G数",)
_TBL_RATE = ("期待度", "成功期待度")


def _norm(s) -> str:
    return unicodedata.normalize("NFKC", " ".join(str(s or "").split()))


def clean_name(text: str) -> str:
    """『CZ「すぱ娘チャレンジ」』→『すぱ娘チャレンジ』。

    ★上位かどうかは名前に残す★（上位CZと通常CZは別物）
    """
    t = _norm(text)
    upper = "上位" in t
    m = re.search(r"「([^」]{2,24})」", t)
    core = m.group(1) if m else t
    core = re.sub(r"^(上位)?(AT-)?CZ\s*", "", core).strip()
    if not _CZ_NAME_OK.match(core):
        return ""
    return ("上位" + core) if upper and not core.startswith("上位") else core


def from_sentences(text: str) -> list:
    out = []
    for m in _SENT.finditer(_norm(text)):
        name = clean_name(m.group("name"))
        if not name:
            continue
        out.append({"name": name, "games": _norm(m.group("games")),
                    "rate": _norm(m.group("rate")), "raw": m.group(0)[:110]})
    return out


def from_table(lines: list) -> list:
    """表から採る。★CZ名は上の見出しにしかないのでさかのぼる★"""
    out = []
    for i, line in enumerate(lines):
        if line not in _TBL_GAMES or i + 1 >= len(lines):
            continue
        games = _norm(lines[i + 1])
        if not _GAMES_OK.match(games):
            continue
        rate = None
        for j in range(i + 2, min(i + 6, len(lines))):
            if lines[j] in _TBL_RATE and j + 1 < len(lines):
                cand = _norm(lines[j + 1])
                if _RATE_OK.match(cand):
                    rate = cand
                break
        if not rate:
            continue          # ★期待度が取れなければ採らない★
        name = ""
        for k in range(i - 1, max(i - 8, -1), -1):
            if "CZ" in lines[k] or "チャレンジ" in lines[k]:
                name = clean_name(lines[k])
                if name:
                    break
        if not name:
            continue          # ★どのCZか分からなければ採らない★
        out.append({"name": name, "games": games, "rate": rate,
                    "raw": f"{name} / {games} / {rate}"})
    return out


def read_page(url: str, official_name: str) -> dict:
    out = {"url": url, "host": url.split("/")[2].lower().removeprefix("www."),
           "ok": False, "reason": "", "czs": []}
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
        if c["name"] in seen:
            continue
        seen.add(c["name"])
        got.append(c)
    out["czs"] = got
    looks = "チャレンジ" in text or "CZ" in text
    if looks and not got:
        out["ok"], out["reason"] = False, "CZの記述はあるが採れませんでした（要確認）"
        return out
    out["ok"] = True
    out["reason"] = "OK" if got else "CZの記述がありません"
    return out


def compare(pages: list) -> dict:
    """★CZ名ごとに、継続G数と期待度が一致したものだけ採る★"""
    votes: dict = {}
    for p in pages:
        if not p.get("ok"):
            continue
        lin = _sl._lineage(p["host"])
        for c in p["czs"]:
            k = json.dumps({x: c[x] for x in ("name", "games", "rate")},
                           ensure_ascii=False, sort_keys=True)
            votes.setdefault(k, {"sample": c, "sources": set()})
            votes[k]["sources"].add(lin)
    adopted, need_third = [], []
    by_name: dict = {}
    for v in votes.values():
        by_name.setdefault(v["sample"]["name"], []).append(v)
    for nm, items in by_name.items():
        agreed = [v for v in items if len(v["sources"]) >= 2]
        if len(agreed) == 1:
            c = dict(agreed[0]["sample"])
            c["sources"] = sorted(agreed[0]["sources"])
            adopted.append(c)
        else:
            need_third.append({
                "name": nm,
                "why": ("出典が食い違っています" if len(items) > 1
                        else "1つの出典にしかありません"),
                "candidates": [{"games": v["sample"]["games"],
                                "rate": v["sample"]["rate"],
                                "sources": sorted(v["sources"])} for v in items]})
    return {"adopted": sorted(adopted, key=lambda x: x["name"]),
            "need_third": need_third}


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []
    nl = chr(10)

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    t("★CZ名から飾りを外す★",
      clean_name('CZ「すぱ娘チャレンジ」') == "すぱ娘チャレンジ")
    t("★★上位かどうかは名前に残す★★（上位CZと通常CZは別物）",
      clean_name('上位AT-CZ「クライMAXライブチャレンジ」') == "上位クライMAXライブチャレンジ")
    t("　文が入っていたら名前にしない", clean_name("CZは3種類あって、それぞれ性能が違います") == "")

    S = ("すぱ娘チャレンジは4G+α継続、成功期待度は約40%。"
         "しす娘チャレンジは7G継続、成功期待度は約50%。"
         "びん娘チャレンジは10G継続、成功期待度は約66%。")
    got = from_sentences(S)
    t("★★文章から3つのCZを別々に採る★★", len(got) == 3)
    t("　名前・G数・期待度をそろえて持つ",
      got[0]["name"] == "すぱ娘チャレンジ" and got[0]["games"] == "4G+α"
      and got[0]["rate"] == "約40%")

    L = ['CZ「すぱ娘チャレンジ」', "タイプ", "ST", "継続G数", "4G+α", "期待度", "約40%"]
    tb = from_table(L)
    t("★★表からも同じ形で採れる★★",
      tb and tb[0]["name"] == "すぱ娘チャレンジ" and tb[0]["rate"] == "約40%")
    t("★★見出しにCZ名が無ければ採らない★★",
      from_table(["なにかの表", "継続G数", "4G+α", "期待度", "約40%"]) == [])
    t("　期待度が取れなければ採らない",
      from_table(['CZ「x」', "継続G数", "4G+α", "備考", "なし"]) == [])

    A = {"url": "https://www.p-world.co.jp/x", "host": "p-world.co.jp", "ok": True,
         "czs": [{"name": "すぱ娘チャレンジ", "games": "4G+α", "rate": "約40%", "raw": ""}]}
    B = {"url": "https://chonborista.com/y", "host": "chonborista.com", "ok": True,
         "czs": [{"name": "すぱ娘チャレンジ", "games": "4G+α", "rate": "約40%", "raw": ""}]}
    t("★2出典一致なら採用★", len(compare([A, B])["adopted"]) == 1)
    C = {**B, "czs": [{**B["czs"][0], "rate": "50%以上"}]}
    t("★★『約50%』と『50%以上』を一致にしない★★（意味の幅が違う）",
      not compare([A, C])["adopted"])
    D = {**B, "czs": [{**B["czs"][0], "name": "上位クライMAXライブチャレンジ"}]}
    t("　名前が違えば別のCZとして扱う", not compare([A, D])["adopted"])
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
        print(f"{p['host']:20} {p['reason']:22} CZ {len(p['czs'])} 件")
        for c in p["czs"]:
            print(f"     {c['name']}: {c['games']} / 期待度 {c['rate']}")
    r = compare(pages)
    print(chr(10) + json.dumps(r, ensure_ascii=False, indent=1))
    return 0 if r["adopted"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(f"★想定外の失敗 {type(e).__name__}: {e}★")
        raise SystemExit(1)

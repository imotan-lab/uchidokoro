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
import html_tables as _ht            # noqa: E402
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


def is_cz_title(text: str) -> bool:
    """その見出しはCZの見出しか。★CZだと分かる語が要る★"""
    t = _norm(text)
    return ("CZ" in t or "チャレンジ" in t or "CHALLENGE" in t or "チャンス" in t)


def from_sentences(text: str) -> list:
    out = []
    for m in _SENT.finditer(_norm(text)):
        name = clean_name(m.group("name"))
        if not name:
            continue
        out.append({"name": name, "games": _norm(m.group("games")),
                    "rate": _norm(m.group("rate")), "raw": m.group(0)[:110]})
    return out


def from_tables(html: str) -> list:
    """表を1区画ずつ読む。★名前はその表の直前の見出しからだけ取る★

    ★2026-07-31・Codex指摘4を自分で再現して作り直した★
      以前は本文を平らな行にしてから見出しをさかのぼっていたため、
      間に別の表が挟まると**別のCZの名前で値を採って**いた。
      表ごとに切り出せば、値も名前も同じ区画の中だけで決まる。
    """
    out = []
    for tb in _ht.tables(html):
        # ★見出しがCZだと分かる時だけ採る★
        #   これが無いと「ボーナス」「なにかの表」まで CZ名 になってしまう
        #   （clean_name は短い語なら何でも通すため）。
        if not is_cz_title(tb["title"]):
            continue
        name = clean_name(tb["title"])
        if not name:
            continue          # ★名前がひも付いていない表は使わない★
        games = _norm(_ht.value_of(tb["pairs"], _TBL_GAMES))
        rate = _norm(_ht.value_of(tb["pairs"], _TBL_RATE))
        if not (_GAMES_OK.match(games) and _RATE_OK.match(rate)):
            continue          # ★継続G数と期待度がそろわなければ採らない★
        out.append({"name": name, "games": games, "rate": rate,
                    "raw": f"{name} / {games} / {rate}"})
    return out


# ★採り漏れの検知は、採れる形より広く取る★（2026-07-31・Codex指摘2を再現）
#   かぎかっこ付きしか数えていなかったため、「Bチャレンジは5G or 10G継続…」のような
#   **採れなかった記述**が採り漏れとして数えられず、「全部採れた」と扱われていた。
#   なお「上位」はかぎかっこの外に書かれる（例: 上位CZ「クライMAXライブCHALLENGE」）。
_CZ_WORDS = "(?:チャレンジ|CHALLENGE|チャンス)"
_MENTION_Q = re.compile("(.{0,8})「([^」]{2,24}" + _CZ_WORDS + ")」")
_MENTION_BARE = re.compile("([ぁ-んァ-ヶ一-龥A-Za-z0-9ー・]{1,24}" + _CZ_WORDS + ")")


def mentioned_names(text: str) -> set:
    """本文に出てくるCZらしい名前。★採り漏れがあるかを測るためだけに使う★

    ★広めに拾う★ 拾いすぎると「採り切れていない」と判定して**載せない**側に倒れる。
    取りこぼすと、採れなかったCZに気づかないまま一部だけ載せることになる。
    """
    t = _norm(text)
    out = set()
    for before, raw in _MENTION_Q.findall(t):
        nm = clean_name(before + "「" + raw + "」")
        if nm:
            out.add(nm)
    for raw in _MENTION_BARE.findall(t):
        nm = clean_name(raw)
        if nm and not any(nm in x or x in nm for x in out):
            out.add(nm)
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
    cands = from_sentences(text) + from_tables(html)
    # ★同じページの中で同じ名前に別の値が出たら、そのページは使わない★
    #   （2026-07-31・Codex指摘3を再現）以前は先に見つけた方だけ残し、
    #   食い違いを黙って捨てていた。捨てた方が正しい可能性がある。
    by_name: dict = {}
    for c in cands:
        by_name.setdefault(c["name"], []).append(c)
    conflict = sorted(n for n, v in by_name.items()
                      if len({(x["games"], x["rate"]) for x in v}) > 1)
    if conflict:
        out["reason"] = ("同じページの中でCZの値が食い違っています（"
                         + "・".join(conflict[:3]) + "）")
        return out
    got = [v[0] for v in by_name.values()]
    out["czs"] = got
    # ★一部だけ採れた状態で使わない★（実際に再現した）
    #   P-WORLDには6つのCZ名があるのに3つしか採れず、それでも「OK」を返していた。
    missing = sorted(mentioned_names(text) - set(by_name))
    if missing:
        out["reason"] = "CZを採り切れていません（" + "・".join(missing[:4]) + "）"
        out["czs"] = []
        return out
    out["ok"] = True
    out["reason"] = "OK" if got else "CZの記述がありません"
    return out


def compare(pages: list) -> dict:
    """★CZ名ごとに、継続G数と期待度が一致したものだけ採る★"""
    votes: dict = {}
    usable = 0
    for p in pages:
        if not p.get("ok"):
            continue
        usable += 1
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
    # ★CZは一式で出す★（2026-07-31・Codex指摘1を再現）
    #   1つでも食い違いが残ったまま残りを載せると、
    #   読者は載っているものが全種類だと読む。
    if need_third:
        adopted = []
    return {"adopted": sorted(adopted, key=lambda x: x["name"]),
            "need_third": need_third,
            # ★使えるページが2つ無いなら「そろっている」とは言わない★
            "complete": bool(not need_third and usable >= 2)}


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

    H = ('<h3><span>CZ「すぱ娘チャレンジ」</span></h3><table><tbody>'
         '<tr><th>タイプ</th><td>ST</td></tr>'
         '<tr><th>継続G数</th><td>4G＋α</td></tr>'
         '<tr><th>期待度</th><td>約40%</td></tr></tbody></table>')
    tb = from_tables(H)
    t("★★表からも同じ形で採れる★★",
      tb and tb[0]["name"] == "すぱ娘チャレンジ" and tb[0]["rate"] == "約40%")
    t("★★表に名前がひも付いていなければ採らない★★",
      from_tables("<h3>なにかの表</h3><table><tr><th>継続G数</th><td>4G</td></tr>"
                  "<tr><th>期待度</th><td>約40%</td></tr></table>") == [])
    t("　期待度が取れなければ採らない",
      from_tables('<h3>CZ「x娘チャレンジ」</h3><table>'
                  "<tr><th>継続G数</th><td>4G</td></tr>"
                  "<tr><th>備考</th><td>なし</td></tr></table>") == [])

    # ★Codex指摘4：間に別の表の見出しが挟まっても名前を横取りしない★
    HH = ('<h3>CZ「Aチャレンジ」</h3><table>'
          "<tr><th>継続G数</th><td>4G</td></tr>"
          "<tr><th>期待度</th><td>約40%</td></tr></table>"
          "<h3>ボーナス</h3><table>"
          "<tr><th>継続G数</th><td>7G</td></tr>"
          "<tr><th>期待度</th><td>約50%</td></tr></table>")
    t("★★別の表の値を、上のCZの名前で採らない★★（実際に再現した）",
      [(c["name"], c["games"]) for c in from_tables(HH)] == [("Aチャレンジ", "4G")])

    t("★本文に出てくるCZ名を数えられる★",
      "すぱ娘チャレンジ" in mentioned_names("「すぱ娘チャレンジ」があります。"))
    t("★★かぎかっこが無い記述も採り漏れとして数える★★（Codex指摘2・再現した）",
      "Bチャレンジ" in mentioned_names(
          "Aチャレンジは4G継続、期待度は約40%。Bチャレンジは5G or 10G継続、期待度は約50%。"))

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

    # ★Codex指摘1：1つでも食い違えばCZは一式で出さない★
    A2 = {**A, "czs": [A["czs"][0],
                       {"name": "Bチャレンジ", "games": "7G", "rate": "約50%", "raw": ""}]}
    B2 = {**B, "czs": [B["czs"][0],
                       {"name": "Bチャレンジ", "games": "7G", "rate": "約60%", "raw": ""}]}
    _r = compare([A2, B2])
    t("★★1つでも食い違えば、一致した分も載せない★★"
      "（一部だけ載せると全種類だと読まれる・再現した）",
      _r["adopted"] == [] and _r["complete"] is False
      and [n["name"] for n in _r["need_third"]] == ["Bチャレンジ"])
    t("　全部そろえば complete になる", compare([A, B])["complete"] is True)
    t("★使えるページが無いのに『そろっている』と言わない★",
      compare([{**A, "ok": False, "czs": []},
               {**B, "ok": False, "czs": []}])["complete"] is False)

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

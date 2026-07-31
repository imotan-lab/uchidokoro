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

_TAIL = (r"(?P<games>[0-9]{1,3}[ ]*G(?:\+[ ]*α)?)継続[、,][ ]*"
         r"(?:平均)?(?:成功)?期待度は(?P<rate>約?[ ]*[0-9]{1,3}(?:\.[0-9])?[ ]*%(?:以上|超)?)")
# かぎかっこ無し（例: すぱ娘チャレンジは4G+α継続、成功期待度は約40%）
_SENT = re.compile("(?P<name>[ぁ-んァ-ヶ一-龥A-Za-z0-9ー・]{2,20}?)は" + _TAIL)
# ★かぎかっこ付き★（例: 上位CZ「クライMAXライブCHALLENGE」は10G継続、平均成功期待度は50%以上）
#   2026-07-31: この形を読めておらず、上位CZが片方の出典からしか採れていなかった。
_SENT_Q = re.compile("(?P<name>(?:[^。]{0,8})「[^」]{2,24}」)は" + _TAIL)

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


def norm_name(name: str) -> str:
    """照合用にそろえる。★英語表記とカタカナ表記の差だけを吸収する★

    ★2026-07-31・Codexと相談し、公式ページで裏を取ってから決めた★
      P-WORLD「クライMAXライブCHALLENGE」／ちょんぼりすた「クライMAXライブチャレンジ」。
      メーカー公式（BELLCO）に上位AT「クライMAXライブ」の記載があり、
      固有部分は公式で確認できた。役割（上位ATへのCZ）も継続G数（10G）も一致。
      **そろえるのはこの1語だけ**で、残りは完全一致を求める。
    """
    t = _norm(name).lower()
    return t.replace("challenge", "チャレンジ")


def is_cz_title(text: str) -> bool:
    """その見出しはCZの見出しか。★CZだと分かる語が要る★"""
    t = _norm(text)
    return ("CZ" in t or "チャレンジ" in t or "CHALLENGE" in t or "チャンス" in t)


def from_sentences(text: str) -> list:
    out, seen = [], set()
    t = _norm(text)
    for rx in (_SENT_Q, _SENT):
        for m in rx.finditer(t):
            name = clean_name(m.group("name"))
            if not name or norm_name(name) in seen:
                continue
            seen.add(norm_name(name))
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
    by_name: dict = {}
    for c in cands:
        by_name.setdefault(norm_name(c["name"]), []).append(c)
    conflict = sorted(v[0]["name"] for v in by_name.values()
                      if len({(x["games"], x["rate"]) for x in v}) > 1)
    if conflict:
        out["reason"] = ("同じページの中でCZの値が食い違っています（"
                         + "・".join(conflict[:3]) + "）")
        return out
    out["czs"] = [v[0] for v in by_name.values()]
    # ★採り漏れは「警告」にとどめる★（2026-07-31・Codexと相談して案Dへ）
    #   語尾だけで拾った語（前兆ステージ・文中の普通名詞）でページごと捨てると、
    #   **2出典で確認できたCZまで失う**。完全な一覧だと言わないことで安全側を保つ。
    out["unresolved"] = sorted(
        mentioned_names(text) - {c["name"] for c in out["czs"]})
    out["ok"] = True
    out["reason"] = "OK" if out["czs"] else "CZの記述がありません"
    return out


def compare(pages: list) -> dict:
    """★CZごとに、項目ごとに採る★（2026-07-31・Codexと相談した案D）

    以前は「継続G数と期待度が両方一致」しないと丸ごと捨てていた。
    それだと期待度の書き方が違うだけで、**存在も継続G数も失う**。
    存在・継続G数・期待度をそれぞれ独立に2出典一致で採る。

    ★総数や「全種類」は決して言わない★
      どの出典も「これで全部」とは書いていないため、一覧の完全性は判定できない。
    """
    per: dict = {}
    unresolved = set()
    for p in pages:
        if not p.get("ok"):
            continue
        lin = _sl._lineage(p["host"])
        unresolved |= set(p.get("unresolved") or [])
        for c in p["czs"]:
            nk = norm_name(c["name"])
            e = per.setdefault(nk, {"names": {}, "sources": set(),
                                    "games": {}, "rate": {}})
            e["sources"].add(lin)
            e["names"].setdefault(c["name"], set()).add(lin)
            e["games"].setdefault(c["games"], set()).add(lin)
            e["rate"].setdefault(c["rate"], set()).add(lin)

    def _pick(d):
        """2出典以上で一致した値だけ返す（割れていたら採らない）。"""
        ok = [(v, srcs) for v, srcs in d.items() if len(srcs) >= 2]
        return ok[0] if len(ok) == 1 else (None, set())

    adopted, need_third = [], []
    for nk, e in sorted(per.items()):
        if len(e["sources"]) < 2:
            need_third.append({"name": sorted(e["names"])[0],
                               "why": "1つの出典にしかありません",
                               "candidates": [{"sources": sorted(e["sources"])}]})
            continue
        games, gs = _pick(e["games"])
        rate, rs = _pick(e["rate"])
        # 表記が割れたときは、票の多い書き方を出す（同数なら並べ替えて決める）
        display = sorted(e["names"], key=lambda n: (-len(e["names"][n]), n))[0]
        adopted.append({"name": display, "games": games, "rate": rate,
                        "sources": sorted(e["sources"]),
                        "games_disputed": games is None and len(e["games"]) > 1,
                        "rate_disputed": rate is None and len(e["rate"]) > 1})
    return {"adopted": adopted, "need_third": need_third,
            # ★CZらしいのに採れなかった語★（載せない判断には使わない・報告用）
            "unresolved": sorted(unresolved)}


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

    mk = lambda h, czs: {"url": "https://" + h + "/x", "host": h, "ok": True,
                         "czs": czs, "unresolved": []}
    one = lambda n, g, r: {"name": n, "games": g, "rate": r, "raw": ""}

    A = mk("p-world.co.jp", [one("すぱ娘チャレンジ", "4G+α", "約40%")])
    B = mk("chonborista.com", [one("すぱ娘チャレンジ", "4G+α", "約40%")])
    r = compare([A, B])
    t("★2出典一致なら採用★",
      len(r["adopted"]) == 1 and r["adopted"][0]["games"] == "4G+α")
    t("　同じ運営元の2ページを2票と数えない",
      not compare([A, mk("p-world.co.jp", B["czs"])])["adopted"])

    # ★Codexと相談した案D：項目ごとに採る★
    C = mk("chonborista.com", [one("すぱ娘チャレンジ", "4G+α", "50%以上")])
    r2 = compare([A, C])
    t("★★『約40%』と『50%以上』が違っても、CZの存在と継続G数は残す★★"
      "（丸ごと捨てると分かっている事実まで失う）",
      len(r2["adopted"]) == 1 and r2["adopted"][0]["games"] == "4G+α"
      and r2["adopted"][0]["rate"] is None
      and r2["adopted"][0]["rate_disputed"] is True)

    # ★英語表記とカタカナ表記の差だけをそろえる★
    D1 = mk("p-world.co.jp", [one("上位クライMAXライブCHALLENGE", "10G", "約50%")])
    D2 = mk("chonborista.com", [one("上位クライMAXライブチャレンジ", "10G", "約50%")])
    t("★★CHALLENGE と チャレンジ を同じCZとして扱う★★（公式で固有部分を確認済み）",
      len(compare([D1, D2])["adopted"]) == 1)
    t("　固有部分が違えば別のCZのまま",
      not compare([D1, mk("chonborista.com",
                          [one("上位ゆめ娘チャレンジ", "10G", "約50%")])])["adopted"])

    t("　1つの出典にしか無いCZは採らない",
      compare([A, mk("chonborista.com", [])])["adopted"] == []
      and compare([A, mk("chonborista.com", [])])["need_third"])
    t("★採り切れなかった語は報告に残す（載せない判断には使わない）★",
      compare([{**A, "unresolved": ["ユニゾンチャレンジ"]}, B])["unresolved"]
      == ["ユニゾンチャレンジ"])

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

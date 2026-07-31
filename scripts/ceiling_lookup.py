"""ceiling_lookup.py — 天井を「一式」で採る（値だけ先に載せない）。

★なぜ一式か（2026-07-31・Codexの助言／自分でも妥当と判断）★
  天井は数字だけでは使えない。同じ「1200G」でも、
  何を数えた1200Gなのか（通常時／AT間／液晶G数）で別の事実になる。
  実際、東京喰種で CZ間600G（液晶G数）と AT間1200G（メニュー画面）を
  取り違える事故が起きている。

  そこで **値・数える対象・恩恵** がそろって初めて1つの事実として採る。
  1つでも欠けたら**採らない**（値だけ先に載せない）。

★2つのサイトで書き方が違う（実データで確認）★
  P-WORLD       … 文章。「通常時を最大1200G消化するとゲーム数天井に到達し、ATに当選する。」
  ちょんぼりすた … 表。「天井G数 → 1200G」「恩恵 → AT当選」
  どちらの形からも同じ形に落とす。

★採用の条件★
  独立2出典で **値・種類・恩恵がすべて一致** したときだけ採用。
  1つでも違えば採らず、「第三の出典が要る」として返す。

使い方:
    python scripts/ceiling_lookup.py --name "Lすーぱぁびん娘" \\
        --url https://www.p-world.co.jp/machine/database/10496 \\
        --url https://chonborista.com/slot/belko-slot/260918/
    python scripts/ceiling_lookup.py --selftest
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

# 天井の種類。★数える対象が違えば別の事実★
KINDS = {
    "GAME": {"jp": "ゲーム数天井", "unit": "G"},
    "CYCLE": {"jp": "周期天井", "unit": "周期"},
    "POINT": {"jp": "ポイント天井", "unit": "pt"},
}

# 文章から採る形（★許可した言い回しだけ★・禁止語を並べる方式は必ず抜ける）
_SENT_GAME = re.compile(
    r"(?P<counted>[^。、]{0,14}?)を?最大\s*(?P<amount>\d{2,5})\s*G\s*消化すると"
    r"[^。]{0,20}?天井に到達し[、,]?\s*(?P<benefit>[^。]{1,24}?)に当選")
_SENT_CYCLE = re.compile(
    r"(?P<counted>[^。、]{0,20}?)が?最大\s*(?P<amount>\d{1,3})\s*周期に到達すると"
    r"[^。]{0,16}?天井となり[、,]?\s*(?P<benefit>[^。]{1,24}?)に当選")
# 表から採る形（見出しと値が交互に並ぶ）
_TABLE_LABELS = {"GAME": ("天井G数", "天井ゲーム数"), "CYCLE": ("天井周期", "周期天井")}
_BENEFIT_LABELS = ("恩恵", "天井恩恵")

# 恩恵として認める形（★文にせず短い語だけ★）
_BENEFIT_OK = re.compile(r"^[ぁ-んァ-ヶ一-龥A-Za-z0-9「」・＋+/ 　]{2,24}$")


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", " ".join(str(s or "").split()))


# 恩恵の言い方をそろえる。★意味を変えない範囲だけ★
#   「AT」と「AT当選」は同じことを言っている（片方は動詞が省かれているだけ）。
#   ★「約50%」と「50%以上」のような意味の幅が違うものは揃えない★
#   （それは spec_lookup の phrasing_not_equal が担当）。
# 「当選」は動詞が省かれているだけなので落としてよい。
#   ★「濃厚」「確定」は落とさない★（確からしさが変わる・Codex指摘、実際に再現した）
_BENEFIT_PLAIN = ("に当選", "当選", "に突入", "突入")
# 確からしさ。★これが違えば別の事実として扱う★
_CERTAINTY = (("濃厚", "LIKELY"), ("確定", "GUARANTEED"))


def split_benefit(text: str):
    """恩恵を「何が起きるか」と「どのくらい確かか」に分ける。

    ★『CZ当選濃厚』と『CZ当選』を同じにしない★
      濃厚は確定ではない。以前は両方 `CZ` にしていたため、
      **確からしさの違う出典どうしを一致とみなしていた**（実際に再現）。
    """
    t = _norm(text).strip("。、,. ")
    cert = "PLAIN"
    for word, name in _CERTAINTY:
        if word in t:
            cert = name
            t = t.replace(word, "")
            break
    for suf in _BENEFIT_PLAIN:
        if t.endswith(suf) and len(t) > len(suf):
            t = t[: -len(suf)]
            break
    return t.strip("　 "), cert


def normalize_benefit(text: str) -> str:
    """後方互換：恩恵の中身だけを返す（確からしさは split_benefit で取る）。"""
    return split_benefit(text)[0]


def normalize_counted(text):
    """数える対象をそろえる（見出しの飾りを落とす）。"""
    if not text:
        return None
    t = _norm(text)
    for mark in ("▼", "■", "●", "◆"):
        if mark in t:
            t = t.rsplit(mark, 1)[-1]
    # 「ゲーム数天井 通常時」のように見出しが残ることがあるので、天井の語を落とす
    t = re.sub(r"^(ゲーム数天井|周期天井|ポイント天井)\s*", "", t.strip())
    return t.strip() or None


def from_sentences(text: str) -> list:
    """文章から天井を採る（P-WORLD の形）。"""
    out = []
    for rx, kind in ((_SENT_GAME, "GAME"), (_SENT_CYCLE, "CYCLE")):
        for m in rx.finditer(_norm(text)):
            benefit = m.group("benefit").strip()
            counted = m.group("counted").strip() or None
            if not _BENEFIT_OK.match(benefit):
                continue
            out.append({"kind": kind, "amount": int(m.group("amount")),
                        "unit": KINDS[kind]["unit"],
                        "counted": normalize_counted(counted),
                        "benefit": split_benefit(benefit)[0],
                        "certainty": split_benefit(benefit)[1],
                        "raw": m.group(0)[:120]})
    return out


def from_table(lines: list) -> list:
    """表から天井を採る（ちょんぼりすたの形）。

    ★恩恵は「天井の見出しの近く」にあるものだけを使う★
      ページのどこかにある「恩恵」を拾うと、別項目の恩恵が混ざる。
    """
    out = []
    for i, line in enumerate(lines):
        for kind, labels in _TABLE_LABELS.items():
            if line not in labels or i + 1 >= len(lines):
                continue
            m = re.match(r"^(\d{1,5})\s*" + KINDS[kind]["unit"] + r"$",
                         _norm(lines[i + 1]))
            if not m:
                continue
            benefit = None
            for j in range(i + 2, min(i + 8, len(lines))):
                if lines[j] in _BENEFIT_LABELS and j + 1 < len(lines):
                    cand = _norm(lines[j + 1])
                    if _BENEFIT_OK.match(cand):
                        benefit = cand
                    break
            if not benefit:
                continue        # ★恩恵が取れなければ採らない（値だけ載せない）★
            out.append({"kind": kind, "amount": int(m.group(1)),
                        "unit": KINDS[kind]["unit"], "counted": None,
                        "benefit": split_benefit(benefit)[0],
                        "certainty": split_benefit(benefit)[1],
                        "raw": f"{line}={lines[i+1]} / 恩恵={benefit}"})
    return out


def read_page(url: str, official_name: str) -> dict:
    """1ページから天井の一式を採る。★機種が違えば何も採らない★"""
    out = {"url": url, "host": url.split("/")[2].lower().removeprefix("www."),
           "ok": False, "reason": "", "ceilings": []}
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
        key = (c["kind"], c["amount"])
        if key in seen:
            continue
        seen.add(key)
        got.append(c)
    out["ceilings"] = got
    # ★天井の話がありそうなのに1つも採れないなら OK と言わない★（Codex指摘・再現済み）
    #   採れなかったことを「天井が無い」と読まれると、
    #   別の出典だけで採用してしまう。
    looks = any(w in text for w in ("天井", "ゲーム数天井", "周期天井"))
    if looks and not got:
        out["ok"], out["reason"] = False, "天井の記述はあるが採れませんでした（要確認）"
        return out
    out["ok"] = True
    out["reason"] = "OK" if got else "天井の記述がありません"
    return out


def _key(c: dict) -> str:
    """一致を見るための鍵。★恩恵と「何を数えるか」まで含める★

    ★2026-07-31・実際に再現した値漏れ★
      以前は `counted`（何を数えるか）を鍵に入れていなかったため、
      **「通常時1200G」と「AT間1200G」が同じ天井として採用された**。
      この2つはまったく別物で、AT間天井を通常時天井として出すと
      読者は打てない台を打つことになる。
    """
    return json.dumps({k: (c.get(k) or "") for k in ("kind", "amount", "unit",
                                                     "counted", "benefit",
                                                     "certainty")},
                      ensure_ascii=False, sort_keys=True)


def _base_key(c: dict) -> str:
    """「何を数えるか」だけを外した鍵（片方が書いていない場合の突き合わせ用）。"""
    return json.dumps({k: (c.get(k) or "") for k in ("kind", "amount", "unit",
                                                     "benefit", "certainty")},
                      ensure_ascii=False, sort_keys=True)


def _merge_unqualified(votes: dict) -> dict:
    """片方が「何を数えるか」を書いていないだけなら、書いてある方に寄せる。

    ★条件を書いてある方を必ず残す★
      「通常時1200G」と「1200G（条件の記載なし）」は食い違いではない。
      ただし**条件なしの側を採用すると条件が消える**ので、
      条件つきの方にまとめる（消える方向には倒さない）。
      条件つきが2種類ある（通常時とAT間）ときはまとめない＝食い違いとして残す。
    """
    groups: dict = {}
    for k, v in votes.items():
        groups.setdefault(_base_key(v["sample"]), []).append((k, v))
    out: dict = {}
    for _, items in groups.items():
        qualified = [(k, v) for k, v in items if v["sample"].get("counted")]
        plain = [(k, v) for k, v in items if not v["sample"].get("counted")]
        if len(qualified) == 1 and plain:
            k, v = qualified[0]
            for _, pv in plain:
                v["sources"] |= pv["sources"]
            out[k] = v
        else:
            for k, v in items:
                out[k] = v
    return out


def compare(pages: list) -> dict:
    """★値・種類・恩恵がすべて一致したものだけ採る★"""
    votes: dict = {}
    for p in pages:
        if not p.get("ok"):
            continue
        lin = _sl._lineage(p["host"])
        for c in p["ceilings"]:
            votes.setdefault(_key(c), {"sample": c, "sources": set()})
            votes[_key(c)]["sources"].add(lin)
    votes = _merge_unqualified(votes)
    adopted, need_third = [], []
    # 同じ種類で値が割れていないかも見る（1200Gと1500Gが両方2票、はありえない）
    by_kind: dict = {}
    for k, v in votes.items():
        by_kind.setdefault(v["sample"]["kind"], []).append((k, v))
    for kind, items in by_kind.items():
        agreed = [(k, v) for k, v in items if len(v["sources"]) >= 2]
        if len(agreed) == 1:
            c = dict(agreed[0][1]["sample"])
            c["sources"] = sorted(agreed[0][1]["sources"])
            adopted.append(c)
        else:
            need_third.append({
                "kind": kind, "jp": KINDS[kind]["jp"],
                "why": ("出典が食い違っています" if len(agreed) != 1 and len(items) > 1
                        else "1つの出典にしかありません"),
                "candidates": [{"amount": v["sample"]["amount"],
                                "counted": v["sample"].get("counted"),
                                "benefit": v["sample"]["benefit"],
                                "sources": sorted(v["sources"])} for _, v in items]})
    return {"adopted": adopted, "need_third": need_third}


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []
    nl = chr(10)

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    S = ("天井は周期天井とゲーム数天井の2種類が存在する。"
         "各キャラの娘ポイントが最大10周期に到達すると周期天井となり、CZに当選する。"
         "通常時を最大1200G消化するとゲーム数天井に到達し、ATに当選する。")
    got = from_sentences(S)
    g = next((x for x in got if x["kind"] == "GAME"), None)
    c = next((x for x in got if x["kind"] == "CYCLE"), None)
    t("★★文章から 値・数える対象・恩恵 を一式で採る★★",
      g and g["amount"] == 1200 and g["counted"] == "通常時" and g["benefit"] == "AT")
    t("　周期天井も採れる", c and c["amount"] == 10 and c["benefit"] == "CZ")
    t("★恩恵が文になっていたら採らない（短い語だけ）★",
      from_sentences("通常時を最大1200G消化するとゲーム数天井に到達し、"
                     "状況によっては上位ATに直行する場合もあるとされているものに当選") == [])

    L = ["天井詳細", "天井G数", "1200G", "恩恵", "AT当選", "解析"]
    tb = from_table(L)
    t("★★表からも同じ形で採れる★★",
      tb and tb[0]["amount"] == 1200 and tb[0]["benefit"] == "AT")
    t("★★『AT』と『AT当選』を同じ恩恵として扱う★★（実データの差）",
      normalize_benefit("AT当選") == normalize_benefit("AT") == "AT"
      and normalize_benefit("CZ当選濃厚") == "CZ")
    t("　意味の違う語まで揃えない（『AT』と『上位AT』は別）",
      normalize_benefit("上位AT当選") != normalize_benefit("AT当選"))
    t("　見出しの飾りを落として数える対象にする",
      normalize_counted("▼ゲーム数天井 通常時") == "通常時")
    t("★★恩恵が無ければ採らない（値だけ載せない）★★",
      from_table(["天井G数", "1200G", "解析", "通常時の抽選"]) == [])
    t("　単位が合わなければ採らない",
      from_table(["天井G数", "1200pt", "恩恵", "AT当選"]) == [])

    A = {"url": "https://www.p-world.co.jp/x", "host": "p-world.co.jp", "ok": True,
         "ceilings": [{"kind": "GAME", "amount": 1200, "unit": "G",
                       "counted": "通常時", "benefit": "AT", "raw": ""}]}
    B = {"url": "https://chonborista.com/y", "host": "chonborista.com", "ok": True,
         "ceilings": [{"kind": "GAME", "amount": 1200, "unit": "G",
                       "counted": None, "benefit": "AT", "raw": ""}]}
    r = compare([A, B])
    t("★2出典で値も恩恵も一致すれば採用★",
      len(r["adopted"]) == 1 and r["adopted"][0]["amount"] == 1200)
    C = {**B, "ceilings": [{**B["ceilings"][0], "benefit": "CZ"}]}
    r2 = compare([A, C])
    t("★★値が同じでも恩恵が違えば採らない★★（値だけ合っても採用しない）",
      not r2["adopted"] and r2["need_third"])
    D = {**B, "ceilings": [{**B["ceilings"][0], "amount": 1500}]}
    r3 = compare([A, D])
    t("　値が違えば採らない", not r3["adopted"])
    t("　1出典だけなら採らない", not compare([A])["adopted"])
    E = {**B, "host": "p-world.co.jp"}
    t("★同じ運営元の2ページを2票と数えない★", not compare([A, E])["adopted"])
    t("　機種が違うページの内容は混ぜない",
      not compare([{**A, "ok": False}, B])["adopted"])


    mk = lambda h, cnt: {"url": "https://" + h + "/x", "host": h, "ok": True,
                         "ceilings": [{"kind": "GAME", "amount": 1200, "unit": "G",
                                       "counted": cnt, "benefit": "AT",
                                       "certainty": "PLAIN", "raw": ""}]}
    t("★★『通常時1200G』と『AT間1200G』を同じ天井にしない★★（実際に起きた値漏れ）",
      not compare([mk("p-world.co.jp", "通常時"), mk("chonborista.com", "AT間")])["adopted"])
    _r = compare([mk("chonborista.com", None), mk("p-world.co.jp", "通常時")])
    t("★★片方が条件を書いていないだけなら、条件つきの方を残す★★（条件を消さない）",
      len(_r["adopted"]) == 1 and _r["adopted"][0]["counted"] == "通常時")
    t("　その場合も2出典ぶんの票として数える",
      len(_r["adopted"][0]["sources"]) == 2)
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
        print(f"{p['host']:20} {p['reason']:22} 天井 {len(p['ceilings'])} 件")
        for c in p["ceilings"]:
            print(f"     {KINDS[c['kind']]['jp']}: {c['amount']}{c['unit']}"
                  f" / 恩恵={c['benefit']} / 数える対象={c['counted']}")
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

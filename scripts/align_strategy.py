# -*- coding: utf-8 -*-
"""一覧の狙い目（strategy）を、読者が既定で見る値にそろえる。

★★運営者の判断（2026-08-30）★★
トップページ・早見表に出る「一覧の狙い目」と、記事とチェッカーが見せる値が
食い違っていた（実例＝東京喰種は一覧が AT間450G・記事とチェッカーは 350G）。
選択肢を出したところ、運営者は
★「一覧の数字を、記事と同じ既定表示にそろえる」★を選んだ。

★★新しい数字は作らない★★
　一覧に出す値は**チェッカーが持っている値そのもの**を指すだけ。
　（`page_decision.derived_payout_range` と同じ考え方＝表に載っている値の端を指す）

★★ラベルを読まない★★（意味の判断を機械にやらせない・2026-08-27の指示）
　一覧の数値は、もともとチェッカーの「交換率を選ばないときの値」から作られている。
　＝★いまの数値と一致する枠を探せば、対応が取れる★。
　「CZ間」「G数」「BIG間」といった**呼び方を読む必要がない**。
　★実測：11機種のうち、呼び方がチェッカーと違うものが3つあった★
　（「G数」↔「通常」／「BIG間」↔「BIG後」／「1スルー以上」↔「1スルー」）。
　呼び方を読む作りにしていたら、この3つは例外表を足すことになっていた。

★★決まらなければ、その機種は丸ごと触らない★★（fail-closed）
　・いまの数値と一致する枠が無い
　・一致する枠が複数あって、既定の値が割れている
　→ どちらも「機械には決められない」ので 2AI へ回す。

使い方:
  python scripts/align_strategy.py --all              # 下見（全機種）
  python scripts/align_strategy.py --slug tokyo_ghoul # 下見（1機種）
  python scripts/align_strategy.py --all --apply      # 書く
  python scripts/align_strategy.py --selftest
"""
from __future__ import annotations
import argparse
import io
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_S = os.path.join(BASE, "scripts")
for _p in (BASE, _S):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import safe_json as _sj                                  # noqa: E402

MACHINES = os.path.join(BASE, "assets", "data", "machines.json")

# ★G が付いた数値だけ★（pt・周期・スルー回数は狙い目のG数ではない）
GNUM = re.compile(r"(\d{1,4})\s*G")


def mode_conf(ck: dict, key):
    """★モード設定は checker 直下と checker.modeData 配下の2系統ある★

    （CLAUDE.md「必ず共通アクセサ経由で読む」＝集計漏れ事故があった）
    """
    if isinstance(ck.get(key), dict):
        return ck[key]
    md = ck.get("modeData")
    if isinstance(md, dict) and isinstance(md.get(key), dict):
        return md[key]
    return None


def default_rate(ck: dict) -> str:
    """★読者が最初に見る交換率★（machine.html と同じ選び方）

    `checker.defaultRate || rates[0].key`
    """
    rates = ck.get("exchangeRates")
    if not isinstance(rates, list) or not rates:
        return ""
    return str(ck.get("defaultRate") or rates[0].get("key") or "")


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _base_value(conf: dict):
    """★交換率を選ばないときの値★（一覧の数値はここから作られている）"""
    if not isinstance(conf, dict):
        return None
    return _num(conf.get("target", conf.get("good")))


def _rate_value(conf: dict, rk: str):
    """★その交換率で読者が見る値★"""
    if not isinstance(conf, dict):
        return None
    br = conf.get("byRate")
    if isinstance(br, dict) and isinstance(br.get(rk), dict):
        c = br[rk]
        return _num(c.get("target", c.get("good")))
    return _base_value(conf)


def slots(ck: dict) -> list:
    """狙い目の枠を全部並べる

    → [{"mode": モードの呼び名, "name": 枠の呼び名,
        "base": 交換率なしの値, "rate": 既定の値}]
    ★mode を持たせる理由★＝区切りが名乗っているモードの中だけで探すため。
      全部の枠から値だけで探すと、★そろえた後の数値が別のモードの枠に
      引き寄せられて壊れる★（2026-08-30に実測3機種）。
    """
    rk = default_rate(ck)
    out = []
    for md in (ck.get("modes") or []):
        if not isinstance(md, dict):
            continue
        key = md.get("key")
        conf = mode_conf(ck, key)
        if not isinstance(conf, dict):
            continue
        label = str(md.get("label") or key or "")
        b, r = _base_value(conf), _rate_value(conf, rk)
        if b is not None and r is not None:
            out.append({"mode": label, "name": label, "base": b, "rate": r})
        suru = conf.get("suru")
        if isinstance(suru, list):
            for s in suru:
                if not isinstance(s, dict):
                    continue
                b2, r2 = _base_value(s), _rate_value(s, rk)
                if b2 is not None and r2 is not None:
                    out.append({"mode": label,
                                "name": f"{label}{s.get('count')}スルー",
                                "base": b2, "rate": r2})
    return out


# ★★サイトが交換率を指すときの言い方★★
#   exchangeRates の呼び名に加えて、記事・一覧で使ってきた2語。
#   （CLAUDE.md の例＝「等価600G〜 / 5.6枚650G〜 / 現金700G〜」）
#   ★値を決めるための名簿ではない★＝
#     「この一覧は交換率ごとに書き分けているか」を振り分けるだけ。
#   ★実測（2026-08-30）★＝どの機種にも「等価」という交換率は無い
#     （呼び名は 5.6枚 / 6.0枚 / 6.5枚 / 7.0枚 だけ）。
#     つまり一覧の「等価」は**行き場のない呼び名**で、
#     ★数値だけ既定の値に替えると、呼び名と中身が食い違う★。
#     8機種で実際にそうなるところだった。→ まるごと2AIへ回す。
SITE_RATE_WORDS = ("等価", "現金")


def rate_words(ck: dict, strat: str) -> list:
    """一覧が交換率を名乗っているか（名乗っていれば、その呼び名）"""
    words = [str(r.get("label") or "")
             for r in (ck.get("exchangeRates") or [])
             if isinstance(r, dict)]
    words = [w for w in words if w] + list(SITE_RATE_WORDS)
    return [w for w in words if w in strat]


def _segment(strat: str, at: int) -> str:
    """★その数値が入っている区切り★（「/」で区切られた一片）

    ★区切りで見る理由★＝一覧は「CZ間250G〜 / AT間450G〜」のように
    モードごとに区切って書いてある。全体から探すと、
    ★別のモードの枠に引き寄せられる★（2026-08-30に実測3機種）。
    """
    s = strat.rfind("/", 0, at) + 1
    e = strat.find("/", at)
    return strat[s:(e if e >= 0 else len(strat))]


def plan(machine: dict) -> dict:
    """1機種ぶんの書き換え案。★決まらなければ触らない★"""
    slug = str(machine.get("slug") or "")
    out = {"slug": slug, "before": "", "after": "", "why": "", "moves": []}
    ck = machine.get("checker")
    strat = str(machine.get("strategy") or "")
    out["before"] = strat
    if not isinstance(ck, dict) or not strat:
        out["why"] = "チェッカーか一覧の狙い目がありません"
        return out
    if not default_rate(ck):
        out["why"] = "交換率の一覧がありません"
        return out
    sl = slots(ck)
    if not sl:
        out["why"] = "狙い目の枠が読めません"
        return out

    named = rate_words(ck, strat)
    if named:
        out["why"] = ("一覧が交換率ごとに書き分けています"
                      f"（{'/'.join(named)}）。"
                      "どの数値がどの交換率かは書き方の問題なので"
                      "2AIが読んでください")
        return out

    hits = list(GNUM.finditer(strat))
    if not hits:
        out["why"] = "一覧にG数がありません"
        return out

    pieces, last = [], 0
    for m in hits:
        cur = int(m.group(1))
        seg = _segment(strat, m.start(1))
        here = [s for s in sl if s["mode"] and s["mode"] in seg]
        if not here:
            out["why"] = (f"「{seg.strip()[:24]}」がどのモードの話か"
                          "分かりません（2AIが読んでください）")
            out["moves"] = []
            return out
        # ①もう既定の値になっている＝触らない（★2回目に壊さない★）
        if cur in {s["rate"] for s in here}:
            pieces.append(strat[last:m.start(1)])
            pieces.append(str(cur))
            last = m.end(1)
            continue
        # ②そのモードの中で「交換率なしの値」が一致する枠を探す
        cand = [s for s in here if s["base"] == cur]
        if not cand:
            out["why"] = (f"{cur}G と一致する枠がこのモードにありません"
                          "（2AIが読んでください）")
            out["moves"] = []
            return out
        vals = {s["rate"] for s in cand}
        if len(vals) != 1:
            out["why"] = (f"{cur}G に当たる枠が複数あり、既定の値が割れています"
                          f"（{sorted(vals)}・2AIが読んでください）")
            out["moves"] = []
            return out
        new = int(list(vals)[0])
        pieces.append(strat[last:m.start(1)])
        pieces.append(str(new))
        last = m.end(1)
        out["moves"].append({"from": cur, "to": new, "slot": cand[0]["name"]})
    pieces.append(strat[last:])
    after = "".join(pieces)

    # ★数字以外は1文字も変えていないこと★
    if GNUM.sub("#G", after) != GNUM.sub("#G", strat):
        out["why"] = "★数字以外が変わりました★（安全のため書きません）"
        out["moves"] = []
        return out
    out["after"] = after
    out["why"] = ("そろっています" if after == strat
                  else f"{len(out['moves'])} 箇所をそろえます")
    return out


def _load():
    data = _sj.read_json(MACHINES, expect=(dict, list))
    rows = data if isinstance(data, list) else (data.get("machines") or [])
    return data, rows


def main() -> int:
    ap = argparse.ArgumentParser(
        description="一覧の狙い目を、読者が既定で見る値にそろえる")
    ap.add_argument("--slug", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.slug and not a.all:
        print("--slug か --all が要ります")
        return 1

    data, rows = _load()
    targets = [m for m in rows
               if a.all or str(m.get("slug") or "") == a.slug]
    if not targets:
        print(f"{a.slug} が machines.json にありません")
        return 1

    changed, skipped = [], []
    for m in targets:
        p = plan(m)
        if p["moves"]:
            changed.append((m, p))
        elif p["after"] != p["before"] or "2AI" in p["why"]:
            skipped.append(p)

    print(f"そろえる機種: {len(changed)} ／ 2AIへ回す機種: {len(skipped)}")
    for m, p in changed:
        print(f"\n■ {p['slug']}")
        print(f"   before: {p['before']}")
        print(f"   after : {p['after']}")
        for mv in p["moves"]:
            print(f"     {mv['from']}G → {mv['to']}G  ［{mv['slot']}］")
    for p in skipped:
        print(f"\n△ {p['slug']}: {p['why']}")
        print(f"   一覧: {p['before']}")

    if not a.apply:
        print("\n★下見です★（--apply で書きます）")
        return 0
    if not changed:
        print("\n書くものはありません")
        return 0

    for m, p in changed:
        m["strategy"] = p["after"]
    raw = json.dumps(data, ensure_ascii=False, indent=1)
    io.open(MACHINES, "w", encoding="utf-8", newline="\n").write(raw + "\n")
    print(f"\n★書きました★ {len(changed)} 機種")
    print("★このあと build_hub_pages.py と build_machine_pages.py を回すこと★")
    return 0


def selftest() -> int:
    ng = []
    ran = [0]

    def t(name, cond):
        ran[0] += 1
        print(("✅ " if cond else "❌ ") + name)
        if not cond:
            ng.append(name)

    ck = {"exchangeRates": [{"key": "eq56", "label": "5.6枚"},
                            {"key": "r55", "label": "6.0枚"}],
          "defaultRate": "eq56",
          "modes": [{"key": "cz", "label": "CZ間"},
                    {"key": "at", "label": "AT間", "hasSuru": True}],
          "cz": {"good": 250, "byRate": {"eq56": {"target": 300},
                                         "r55": {"target": 350}}},
          "at": {"suru": [{"count": 0, "good": 450,
                           "byRate": {"eq56": {"target": 350}}}]}}

    p = plan({"slug": "x", "checker": ck,
              "strategy": "CZ間250G〜 / AT間450G〜"})
    t("★★既定の交換率の値にそろえる★★",
      p["after"] == "CZ間300G〜 / AT間350G〜")
    t("★★もう既定の値になっている数値は触らない★★"
      "（＝2回目に走らせても壊さない）",
      plan({"slug": "x", "checker": ck,
            "strategy": "CZ間300G〜 / AT間350G〜"})["moves"] == [])
    t("★★数字以外は1文字も変えない★★",
      p["after"].replace("300", "250").replace("350", "450")
      == "CZ間250G〜 / AT間450G〜")

    q = plan({"slug": "x", "checker": ck, "strategy": "CZ間999G〜"})
    t("★★一致する枠が無ければ触らない★★（2AIへ回す）",
      q["moves"] == [] and "2AI" in q["why"])

    # ★同じモードの中で、同じ「交換率なしの値」が別の既定値を持つとき★
    ck2 = json.loads(json.dumps(ck))
    ck2["at"]["suru"].append({"count": 1, "good": 450,
                              "byRate": {"eq56": {"target": 200}}})
    r = plan({"slug": "x", "checker": ck2, "strategy": "AT間450G〜"})
    t("★★当たる枠が複数あって既定の値が割れたら触らない★★",
      r["moves"] == [] and "2AI" in r["why"])
    t("★★モードの呼び名が区切りに出ていなければ触らない★★"
      "（＝値だけで探すと、別のモードの枠に引き寄せられる）",
      plan({"slug": "x", "checker": ck,
            "strategy": "なんとか450G〜"})["moves"] == [])

    ck3 = json.loads(json.dumps(ck))
    ck3["at"]["suru"][0]["byRate"]["eq56"]["target"] = 250
    ck3["at"]["suru"][0]["good"] = 250
    ck3["cz"]["byRate"]["eq56"]["target"] = 250
    s = plan({"slug": "x", "checker": ck3, "strategy": "CZ間250G〜"})
    t("　★枠が複数でも既定の値が同じなら通る★", s["after"] == "CZ間250G〜")

    t("★★G が付いていない数値は触らない★★（周期・スルー回数）",
      plan({"slug": "x", "checker": ck,
            "strategy": "CZ間250G〜 / 5周期目〜 / 2スルー〜"})["after"]
      == "CZ間300G〜 / 5周期目〜 / 2スルー〜")
    t("★★一覧が交換率を名乗っていたら触らない★★"
      "（呼び名と中身が食い違うため・実測8機種）",
      plan({"slug": "x", "checker": ck,
            "strategy": "等価250G〜"})["moves"] == []
      and plan({"slug": "x", "checker": ck,
                "strategy": "5.6枚250G〜"})["moves"] == [])
    t("　★呼び名を名乗っていなければ通る★",
      plan({"slug": "x", "checker": ck,
            "strategy": "CZ間250G〜"})["after"] == "CZ間300G〜")
    t("★★交換率の一覧が無ければ触らない★★",
      plan({"slug": "x", "checker": {"modes": []},
            "strategy": "CZ間250G〜"})["moves"] == [])

    # ★モード設定が modeData 配下にある系統でも読む★
    ck4 = {"exchangeRates": [{"key": "eq56", "label": "5.6枚"}],
           "defaultRate": "eq56",
           "modes": [{"key": "cz", "label": "CZ間"}],
           "modeData": {"cz": {"good": 250,
                               "byRate": {"eq56": {"target": 300}}}}}
    t("★★modeData 配下のモードも読む★★（片方だけ読むと集計漏れ）",
      plan({"slug": "x", "checker": ck4,
            "strategy": "CZ間250G〜"})["after"] == "CZ間300G〜")

    # --- ★本物のデータで動く★ -------------------------------------------
    #   ★直した瞬間に落ちる試験を書かない★（2026-08-30に踏んだ罠⑲）＝
    #     「東京喰種の一覧が 300/350 になること」と書いたら、
    #     そろえた瞬間にこの試験だけ赤くなり、
    #     壊し方の道具が4件とも「壊す前から赤い」になった。
    #   → 本物のチェッカーから**ずれた一覧を組み立てて**渡す。
    _data, rows = _load()
    real_ok, real_why = False, "試せる機種がありません"
    for m in rows:
        ck = m.get("checker")
        if not isinstance(ck, dict):
            continue
        sl = slots(ck)
        # ★モードの呼び名がそのまま使える枠だけを選ぶ★
        #   （区切りにモードの呼び名が出ていないと、道具は触らない）
        moved = [s for s in sl if s["base"] != s["rate"] and s["name"]]
        # 同じモードの枠が2つ以上あると、作った一覧が曖昧になるので外す
        seen = {}
        for s in moved:
            seen[s["mode"]] = seen.get(s["mode"], 0) + 1
        moved = [s for s in moved if seen[s["mode"]] == 1]
        if len(moved) < 2:
            continue
        if len({s["base"] for s in moved}) != len(moved):
            continue
        made = " / ".join(f"{s['name']}{s['base']}G〜" for s in moved)
        want = " / ".join(f"{s['name']}{s['rate']}G〜" for s in moved)
        p2 = plan({"slug": m.get("slug"), "checker": ck, "strategy": made})
        real_ok = (p2["after"] == want)
        real_why = f"{m.get('slug')}: {made} → {p2['after']}（期待 {want}）"
        break
    t("★★本物のチェッカーで動く★★ " + real_why[:80], real_ok)

    # ★そろえたあとは、もう動かない★（同じ処理を2度かけても変わらない）
    still = [m.get("slug") for m in rows if plan(m)["moves"]]
    t("★★いまのデータには、そろえ残しが無い★★"
      f"（残り: {still[:3]}）", not still)

    # ★★2回かけても値が動かないこと★★（2026-08-30に踏んだ欠陥そのもの）
    #   直す前は「そろえた後の数値が別のモードの枠に一致」して、
    #   2回目で違う値へ引き寄せられた（実測3機種）。
    twice_ng = []
    for m in rows:
        p1 = plan(m)
        if not p1["after"]:
            continue
        p2 = plan({"slug": p1["slug"], "checker": m.get("checker"),
                   "strategy": p1["after"]})
        if p2["moves"]:
            twice_ng.append(f"{p1['slug']}: {p1['after']} → {p2['after']}")
    t("★★2回かけても値が動かない★★" + ("" if not twice_ng
                                       else f"（{twice_ng[:2]}）"),
      not twice_ng)

    print(f"\n{ran[0] - len(ng)}/{ran[0]} " + ("合格" if not ng else "不合格"))
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


if __name__ == "__main__":
    raise SystemExit(main())

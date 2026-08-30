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

★★区切りごとに、その区切りが名乗っているモードの中だけで見る★★
　一覧は「CZ間250G〜 / AT間450G〜」のようにモードごとに区切って書いてある。
　その区切りにモードの呼び名が出ていて、かつ
　そのモードの枠の「交換率を選ばないときの値」と一致するときだけ、
　既定の値へそろえる。

★★はじめ「値だけで探す」作りにしたら、2回目で値が壊れた★★
　（2026-08-30・実測3機種）
　★そろえた後の数値が、別のモードの枠の値に一致して引き寄せられた★。

    東京喰種  CZ間:        交換率なし=250 / 既定=300
              AT間2スルー: 交換率なし=300 / 既定=250   ←★交差している★

　＝そろえて 300 にした途端、300 は「AT間2スルーの交換率なしの値」に見え、
　　次の実行で 250 へ戻された。
　★「いまの値を手がかりに、いまの値を書き換える」道具は、
　　2回かけて動かないことを試験にする★（鉄則5e）。

★★決まらなければ、その機種は丸ごと触らない★★（fail-closed）
　・もう既定の値になっている（＝そろっている。2回目に壊さない）
　・区切りにモードの呼び名が出ていない
　・そのモードの中に、いまの数値と一致する枠が無い
　・一致する枠が複数あって、既定の値が割れている
　・一覧が交換率ごとに書き分けている
　→ どれも「機械には決められない」ので 2AI へ回す。
　★実測：断るのは111機種★（呼び名がモード名と違うものを含む）。

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
#   ★前後に数字が続いていないこと★（2026-08-30・Codexの指摘4）＝
#   境目が無いと「12345G」の後ろ4桁だけに食いつく。
#   （いまは「数字以外を変えていないか」の網が受け止めて断っていたが、
#     ★断る理由が意味不明になる★ので、切り出しの側を正しくする）
GNUM = re.compile(r"(?<!\d)(\d{1,4})(?!\d)\s*G")


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
    """★整数だけ受け取る★（2026-08-30・Codexの指摘4）

    小数を許すと `int()` で黙って切り捨てる（350.5 → 350G）。
    G数は整数で持っている前提なので、小数は**読めない値**として扱い、
    その機種は丸ごと2AIへ回す。
    """
    return v if type(v) is int else None


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
            out.append({"mode": label, "key": str(key or label),
                        "name": label, "base": b, "rate": r})
        suru = conf.get("suru")
        if isinstance(suru, list):
            for s in suru:
                if not isinstance(s, dict):
                    continue
                b2, r2 = _base_value(s), _rate_value(s, rk)
                if b2 is not None and r2 is not None:
                    out.append({"mode": label, "key": str(key or label),
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
        # ★★区切りから決まるモードは1つだけ★★（2026-08-30・Codexの指摘2）
        #   直す前は当たったモードを**全部混ぜて**いたので、
        #   「CZまたはAT当選250G〜」のような区切りで
        #   ★別のモードの枠から書き換えられた★（実際に再現した）。
        keys = {s["key"] for s in here}
        if len(keys) != 1:
            out["why"] = (f"「{seg.strip()[:24]}」がどのモードの話か"
                          + ("決まりません" if keys else "分かりません")
                          + "（2AIが読んでください）")
            out["moves"] = []
            return out
        # ★★「もう揃っている」と「まだずれている」が両方成り立つなら触らない★★
        #   （2026-08-30・Codexの指摘1）＝同じモードの中でも値は交差しうる。
        #     0スルー: 交換率なし=100 / 既定=200
        #     1スルー: 交換率なし=200 / 既定=300
        #   一覧の 200 は「1スルーのずれ」でも「0スルーの揃い」でも読める。
        #   ★直す前は「揃っている」を先に見ていたので、古い数値が残った★。
        cand = [s for s in here if s["base"] == cur]
        aligned = cur in {s["rate"] for s in here}
        if cand and aligned:
            out["why"] = (f"{cur}G は「もう揃っている」とも「まだずれている」とも"
                          "読めます（2AIが読んでください）")
            out["moves"] = []
            return out
        if aligned:
            pieces.append(strat[last:m.start(1)])
            pieces.append(str(cur))
            last = m.end(1)
            continue
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


def _digest() -> str:
    """★機種データの中身の指紋★（読んでから書くまでに変わっていないか）"""
    import hashlib
    with io.open(MACHINES, "rb") as f:
        return hashlib.sha256(f.read().replace(b"\r\n", b"\n")).hexdigest()


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

    if a.slug and a.all:
        # ★同時に渡すと --all が勝って全機種を書いていた★（Codexの指摘3）
        print("--slug と --all は同時に使えません")
        return 1

    before = _digest()
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

    # ★★読んでから書くまでに、誰かが機種データを変えていないか★★
    #   （2026-08-30・Codexの指摘3）＝変わっていたら、
    #   こちらの全文保存でその変更を消してしまう。
    if _digest() != before:
        print("\n★書きません★ 読んでいる間に機種データが変わりました"
              "（もう一度やり直してください）")
        return 1

    for m, p in changed:
        m["strategy"] = p["after"]

    # ★★途中で止まっても壊れないように書く★★（同・指摘3）
    #   直す前は本体を直接開いて上書きしていたので、
    #   書込み中に止まると**途中までのJSON**が残った。
    body = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
    tmp = MACHINES + ".align.tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, MACHINES)
    print(f"\n★書きました★ {len(changed)} 機種")
    if skipped:
        print(f"★2AIへ回す機種が {len(skipped)} 件あります★"
              "（機械では決められないもの・上に理由を出しました）")

    # ★★ページを作り直すまで「成功」と言わない★★（同・指摘6）
    #   データだけ書いてハブが古いままだと、
    #   その晩の新台公開が丸ごと止まる（4ページの一致を求めるため）。
    try:
        import publish_new_machine as _pnm
        ng = _pnm.check_hubs_untouched()
    except Exception as e:                                   # noqa: BLE001
        print(f"★ハブ4ページを確かめられません: {type(e).__name__}: {e}★")
        return 1
    if ng:
        print("\n★まだ終わっていません★ 一覧・ランキングが古いままです:")
        for x in ng[:4]:
            print("  - " + str(x)[:110])
        print("  python scripts/build_hub_pages.py --legacy")
        print("  python scripts/build_machine_pages.py --legacy --slug <slug>")
        return 3
    print("★一覧・ランキングは、いまのデータと一致しています★")
    print("★機種ページは build_machine_pages.py --legacy --slug で作り直すこと★")
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
    #   ★「何も書かない」ではなく「そのまま通る」ことを見る★＝
    #     moves が空なだけなら、別の守りが断っただけでも合格になる（罠④）。
    _a = plan({"slug": "x", "checker": ck,
               "strategy": "CZ間300G〜 / AT間350G〜"})
    t("★★もう既定の値になっている数値は、そのまま通る★★"
      "（＝2回目に走らせても壊さない）",
      _a["moves"] == [] and _a["after"] == "CZ間300G〜 / AT間350G〜")
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

    # ★同じモードに枠が2つあっても、既定の値が同じなら通る★
    ck3 = json.loads(json.dumps(ck))
    ck3["at"]["suru"].append({"count": 1, "good": 450,
                              "byRate": {"eq56": {"target": 350}}})
    s = plan({"slug": "x", "checker": ck3, "strategy": "AT間450G〜"})
    t("　★枠が複数でも既定の値が同じなら通る★", s["after"] == "AT間350G〜")

    # ★★同じモードの中で値が交差していたら触らない★★
    #   （2026-08-30・Codexの指摘1。★実際に再現してから直した★）
    #     0スルー: 交換率なし=100 / 既定=200
    #     1スルー: 交換率なし=200 / 既定=300
    #   一覧の 200 は「1スルーのずれ」とも「0スルーの揃い」とも読める。
    ck5 = {"exchangeRates": [{"key": "eq56", "label": "5.6枚"}],
           "defaultRate": "eq56",
           "modes": [{"key": "at", "label": "AT間", "hasSuru": True}],
           "at": {"suru": [
               {"count": 0, "good": 100, "byRate": {"eq56": {"target": 200}}},
               {"count": 1, "good": 200,
                "byRate": {"eq56": {"target": 300}}}]}}
    c = plan({"slug": "x", "checker": ck5, "strategy": "AT間200G〜"})
    t("★★「もう揃っている」とも「まだずれている」とも読めたら触らない★★"
      "（＝古い数値をそのまま残す穴）",
      c["moves"] == [] and "とも" in c["why"])

    # ★★区切りから決まるモードが1つでなければ触らない★★（同・指摘2）
    ck6 = {"exchangeRates": [{"key": "eq56", "label": "5.6枚"}],
           "defaultRate": "eq56",
           "modes": [{"key": "cz", "label": "CZ"}, {"key": "at", "label": "AT"}],
           "cz": {"good": 250, "byRate": {"eq56": {"target": 300}}},
           "at": {"good": 900, "byRate": {"eq56": {"target": 111}}}}
    d = plan({"slug": "x", "checker": ck6,
              "strategy": "CZまたはAT当選250G〜"})
    t("★★モード名が2つ出ている区切りは触らない★★"
      "（＝別のモードの枠から書き換えられた穴）", d["moves"] == [])

    # ★★小数は読めない値として扱う★★（同・指摘4）
    ck7 = {"exchangeRates": [{"key": "eq56", "label": "5.6枚"}],
           "defaultRate": "eq56",
           "modes": [{"key": "cz", "label": "CZ間"}],
           "cz": {"good": 250, "byRate": {"eq56": {"target": 350.5}}}}
    t("★★小数を黙って切り捨てない★★（350.5 → 350G にしない）",
      plan({"slug": "x", "checker": ck7,
            "strategy": "CZ間250G〜"})["moves"] == [])

    # ★★数字の途中に食いつかない★★（同・指摘4）
    ck8 = {"exchangeRates": [{"key": "eq56", "label": "5.6枚"}],
           "defaultRate": "eq56",
           "modes": [{"key": "cz", "label": "CZ間"}],
           "cz": {"good": 2345, "byRate": {"eq56": {"target": 999}}}}
    #   ★理由まで見る★＝真偽だけだと「数字以外を変えていないか」の網が
    #     先に受け止めてしまい、境目の守りを一度も通らない（罠④）。
    _b = plan({"slug": "x", "checker": ck8, "strategy": "CZ間12345G〜"})
    t("★★12345G の後ろ4桁に食いつかない★★"
      f"（理由: {_b['why'][:28]}）",
      _b["moves"] == [] and "G数がありません" in _b["why"])

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
    t("★★機械が決められるそろえ残しは無い★★"
      "（★2AIへ回す機種は別にある＝これは「全部正しい」の意味ではない★）"
      f"（残り: {still[:3]}）", not still)

    # ★★2回かけても値が動かないこと★★（2026-08-30に踏んだ欠陥そのもの）
    #   直す前は「そろえた後の数値が別のモードの枠に一致」して、
    #   2回目で違う値へ引き寄せられた（実測3機種）。
    #   ★いまのデータはもう揃っているので、そのまま回しても何も試せない★
    #     （Codexの指摘5）→ 全機種について、枠の「交換率なしの値」から
    #     ★ずれた一覧を組み立てて★、1回目の結果を2回目へ渡す。
    twice_ng = []
    for m in rows:
        ck9 = m.get("checker")
        if not isinstance(ck9, dict):
            continue
        sl9 = slots(ck9)
        seen9 = {}
        for s in sl9:
            seen9[s["mode"]] = seen9.get(s["mode"], 0) + 1
        use = [s for s in sl9 if seen9[s["mode"]] == 1 and s["name"]]
        if not use:
            continue
        made = " / ".join(f"{s['name']}{s['base']}G〜" for s in use)
        p1 = plan({"slug": m.get("slug"), "checker": ck9, "strategy": made})
        if not p1["after"]:
            continue
        p2 = plan({"slug": m.get("slug"), "checker": ck9,
                   "strategy": p1["after"]})
        if p2["moves"]:
            twice_ng.append(f"{m.get('slug')}: {p1['after']} → {p2['after']}")
    t("★★2回かけても値が動かない★★" + ("" if not twice_ng
                                       else f"（{twice_ng[:2]}）"),
      not twice_ng)

    print(f"\n{ran[0] - len(ng)}/{ran[0]} " + ("合格" if not ng else "不合格"))
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""grow_legacy.py — 旧方式の先行記事に、裏取りできた材料を書き足す。

★何のための道具か（2026-08-06）★
  8月3日に導入された7機種の記事が「当サイトでは未確認です」のまま残っている。
  名鑑の索引を直した結果、型式名・機械割・天井などが**2出典一致で採れる**ように
  なったので、その分だけを記事へ入れる。

★やること／やらないこと★
  やる   : 未確認の箱を、2出典で一致した事実に差し替える
  やらない: すでに書いてある文を書き換える／消す（★足すだけ★）
           値を作る（材料に無いものは書かない）

使い方:
    python scripts/grow_legacy.py                 # 対象と差分を見る
    python scripts/grow_legacy.py --slug xxx      # 1機種だけ
    python scripts/grow_legacy.py --slug xxx --apply
    python scripts/grow_legacy.py --selftest
"""
from __future__ import annotations

import argparse
import json
import re
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import page_decision as _pd              # noqa: E402
import safe_json as _sj                  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DETAILS = os.path.join(BASE, "assets", "data", "machine-details")
PENDING = "当サイトでは未確認です。確認でき次第、この欄に掲載します。"

# 天井の種類ごとの書き方（★材料に無い言葉を足さない★）
_KIND_JP = {"GAME": "ゲーム数天井", "CYCLE": "周期天井",
            "POINT": "ポイント天井", "THROUGH": "スルー天井"}


def targets(slug: str | None = None) -> list:
    ms = _sj.read_json(os.path.join(BASE, "assets", "data", "machines.json"),
                       expect=(dict, list))
    ms = ms["machines"] if isinstance(ms, dict) else ms
    out = []
    for m in ms:
        if slug and m.get("slug") != slug:
            continue
        try:
            if _pd.machine_class(m) == "LEGACY_PREVIEW":
                out.append(m)
        except Exception:                 # noqa: BLE001
            continue
    return out


def ceiling_lines(material: dict) -> list:
    """天井の材料から本文の行を作る（★採用されたものだけ★）。"""
    out = []
    for c in ((material.get("ceilings") or {}).get("adopted") or []):
        jp = _KIND_JP.get(c.get("kind"))
        if not jp:
            continue
        counted = c.get("counted") or ""
        amount, unit = c.get("amount"), c.get("unit")
        if amount is None or not unit:
            continue
        what = f"（{counted}）" if counted else ""
        line = f"**{jp}**：{amount}{unit}{what}"
        ben = str(c.get("benefit") or "").strip()
        if ben:
            line += f" → {ben}"
        out.append(line + "（出典2件で一致）")
    return out


def spec_lines(material: dict) -> list:
    """基本スペックの材料から本文の行を作る。"""
    ad = material.get("adopted") or {}
    out = []
    if ad.get("model_code"):
        out.append(f"**型式名**：{ad['model_code']['value']}（出典2件で一致）")
    rng = ad.get("payout_range")
    if rng:
        v = rng["value"]
        out.append(f"**機械割**：{v['low']}%〜{v['high']}%（出典2件で一致）")
    g50 = ad.get("games_per_50")
    if g50:
        out.append(f"**50枚あたりのゲーム数**：約{g50['value']['games']:g}G"
                   "（出典2件で一致）")
    return out


# ★「まだ分かりません」を表す言い方★
_UNKNOWN_MARK = ("判明していない", "判明していません", "判明しておらず",
                 "公開されていません", "解析判明後", "判明次第", "未解析",
                 "揃っていません", "確認できていません", "分かっていません",
                 "不明です")
# 足した事実 → その事実を指す言葉（本文中の呼び方）
_ITEM_WORDS = {"天井": ("天井",), "機械割": ("機械割", "出玉率"),
               "型式名": ("型式名",), "ゲーム数": ("50枚",)}
_ENUM = re.compile(r"^\*\*(?P<items>[^*]+)\*\*：解析判明次第追記します。?$")


def _knows(text: str, kinds: set) -> bool:
    """その文が『いま分かった事実』を「まだ分からない」と言っているか。"""
    if not any(w in text for w in _UNKNOWN_MARK):
        return False
    return any(w in text for k in kinds for w in _ITEM_WORDS.get(k, ()))


def resolve_contradictions(after: dict, kinds: set) -> tuple:
    """★足した事実と食い違う文を落とす★（2026-08-06）

    天井が分かったのに「天井が判明していないので狙い目を出せません」が
    残っていると、同じページの中で矛盾する。**足した事実に限って**
    その打ち消し文を落とす（値は1つも書かない）。
    """
    removed = []
    if not kinds:
        return after, removed
    for sec in after.get("sections") or []:
        if not isinstance(sec.get("body"), list):
            continue
        out = []
        for b in sec["body"]:
            t = str(b)
            m = _ENUM.match(t.strip())
            if m:                          # 「A・B・C：解析判明次第追記します」
                keep = [x for x in m.group("items").split("・")
                        if not any(w in x for k in kinds
                                   for w in _ITEM_WORDS.get(k, ()))]
                new = (f"**{'・'.join(keep)}**：解析判明次第追記します。"
                       if keep else "")
            else:
                sents = [s for s in re.split(r"(?<=。)", t) if s.strip()]
                new = "".join(s for s in sents if not _knows(s, kinds))
            if new.strip() == t.strip():
                out.append(b)
                continue
            removed.append(t)
            if new.strip():
                out.append(new.strip())
        if not out:                        # ★節を空にしない★
            out = [PENDING]
        sec["body"] = out
    return after, removed


def plan(machine: dict, material: dict) -> dict:
    """記事に足す内容を決める（★書き込まない★）。"""
    slug = machine["slug"]
    p = os.path.join(DETAILS, f"{slug}.json")
    detail = _sj.read_json(p, expect=dict)
    after = json.loads(json.dumps(detail))
    added = []
    have = json.dumps(detail, ensure_ascii=False)

    def _put(title: str, lines: list):
        if not lines:
            return
        sec = next((s for s in after.get("sections") or []
                    if str(s.get("title")) == title), None)
        if sec is None or not isinstance(sec.get("body"), list):
            return
        new = [x for x in lines if x not in have]      # ★同じ内容は足さない★
        if not new:
            return
        # ★未確認の断りは、中身が入ったら外す★（両方並べない）
        body = [b for b in sec["body"] if str(b).strip() != PENDING]
        sec["body"] = new + body
        added.extend(f"{title}: {x}" for x in new)

    cl = ceiling_lines(material)
    sp = spec_lines(material)
    _put("天井・恩恵", cl)
    _put("基本スペック", sp)
    # ★どの事実が入ったか★（打ち消し文を落とす範囲をこれに限る）
    kinds = {"天井"} if cl else set()
    for line in sp:
        for k in ("型式名", "機械割", "ゲーム数"):
            if k in line:
                kinds.add(k)
    after, removed = resolve_contradictions(after, kinds)
    return {"slug": slug, "detail": after, "added": added,
            "before": detail, "removed": removed}


def check(before: dict, after: dict, removed=()) -> list:
    """★足すだけ★になっているか確かめる（消してよいのは removed だけ）。"""
    ng = []
    allow = {str(x) for x in (removed or [])}
    b_secs = {str(s.get("title")): list(s.get("body") or [])
              for s in (before.get("sections") or [])}
    for s in after.get("sections") or []:
        t = str(s.get("title"))
        old = [x for x in b_secs.get(t, [])
               if str(x).strip() != PENDING and str(x) not in allow]
        new = list(s.get("body") or [])
        missing = [x for x in old if x not in new]
        if missing:
            ng.append(f"{t}: 前からあった文が消えます（{str(missing[0])[:36]}…）")
    if len(after.get("sections") or []) != len(before.get("sections") or []):
        ng.append("節の数が変わります")
    return ng


def run(slug: str, apply_it: bool, gather=None) -> dict:
    ms = targets(slug)
    if not ms:
        return {"slug": slug, "problems": ["対象ではありません（旧方式の先行記事のみ）"]}
    m = ms[0]
    if gather is None:
        import add_machine_run as _amr
        gather = _amr.gather
    got = gather(m["name"])
    mat = got.get("material") or {}
    if not mat:
        return {"slug": slug, "problems": ["材料を集められません: "
                                           + " / ".join(got.get("problems") or [])[:160]]}
    pl = plan(m, mat)
    ng = check(pl["before"], pl["detail"], pl["removed"])
    if ng:
        return {"slug": slug, "problems": ng}
    res = {"slug": slug, "added": pl["added"], "removed": pl["removed"],
           "wrote": False, "problems": []}
    if apply_it and (pl["added"] or pl["removed"]):
        with open(os.path.join(DETAILS, f"{slug}.json"), "w",
                  encoding="utf-8") as f:
            json.dump(pl["detail"], f, ensure_ascii=False, indent=1)
            f.write("\n")
        res["wrote"] = True
    return res


# ------------------------------------------------------------------ selftest

def selftest() -> int:
    ok, ran = True, [0]

    def t(name, cond):
        nonlocal ok
        ran[0] += 1
        print(("✅ " if cond else "❌ ") + name)
        ok = ok and bool(cond)

    MAT = {"adopted": {"model_code": {"value": "L機/1"},
                       "payout_range": {"value": {"low": 97.0, "high": 110.0}}},
           "ceilings": {"adopted": [
               {"kind": "THROUGH", "amount": 6, "unit": "スルー",
                "counted": "CZ", "benefit": ""}]}}
    t("★★採用された材料だけを行にする★★",
      ceiling_lines(MAT) == ["**スルー天井**：6スルー（CZ）（出典2件で一致）"]
      and len(spec_lines(MAT)) == 2)
    t("　採用されていない天井は書かない",
      ceiling_lines({"ceilings": {"adopted": [], "need_third": [{"x": 1}]}}) == [])
    t("★★値が欠けた材料からは書かない★★",
      ceiling_lines({"ceilings": {"adopted": [
          {"kind": "GAME", "amount": None, "unit": "G"}]}}) == [])
    before = {"sections": [
        {"title": "天井・恩恵", "body": [PENDING]},
        {"title": "基本スペック", "body": ["**メーカー**：A社"]}]}
    after = json.loads(json.dumps(before))
    after["sections"][0]["body"] = ["**スルー天井**：6スルー"]
    after["sections"][1]["body"] = ["**型式名**：L機/1", "**メーカー**：A社"]
    t("　足すだけなら通る", check(before, after) == [])
    bad = json.loads(json.dumps(after))
    bad["sections"][1]["body"] = ["**型式名**：L機/1"]
    t("★★前からあった文が消える書き方は止める★★",
      any("消えます" in x for x in check(before, bad)))
    bad2 = json.loads(json.dumps(after))
    bad2["sections"].pop()
    t("★★節が減る書き方は止める★★", any("節の数" in x for x in check(before, bad2)))

    # --- 足した事実と食い違う文を落とす -------------------------------
    d = {"sections": [{"title": "狙い目の根拠", "body": [
        "天井の管理方式が判明していない段階では狙い目ラインを示せません。"
        "それまでは慎重に判断してください。",
        "**機械割・コイン単価・天井ゲーム数・設定段階**：解析判明次第追記します。"]}]}
    got, rm = resolve_contradictions(json.loads(json.dumps(d)), {"天井"})
    t("★★天井が分かったら『天井は不明』の文だけ落とす★★（後ろの文は残す）",
      got["sections"][0]["body"][0] == "それまでは慎重に判断してください。")
    t("★★『解析判明次第』の並びは、分かった項目だけ抜く★★",
      got["sections"][0]["body"][1]
      == "**機械割・コイン単価・設定段階**：解析判明次第追記します。")
    got2, _ = resolve_contradictions(json.loads(json.dumps(d)), set())
    t("★★何も足していない時は1文字も消さない★★",
      got2["sections"][0]["body"] == d["sections"][0]["body"])
    keep = {"sections": [{"title": "立ち回りのコツ", "body": [
        "天井は1200Gです。", "純増は約2.5枚です。"]}]}
    got3, rm3 = resolve_contradictions(json.loads(json.dumps(keep)), {"天井"})
    t("★★値が書いてある文は消さない★★（打ち消し語が無いため）",
      got3["sections"][0]["body"] == keep["sections"][0]["body"] and not rm3)
    empt = {"sections": [{"title": "天井・恩恵", "body": [
        "天井は判明していません。"]}]}
    got4, _ = resolve_contradictions(json.loads(json.dumps(empt)), {"天井"})
    t("　全部消えたら未確認の断りを残す（節を空にしない）",
      got4["sections"][0]["body"] == [PENDING])
    b5 = {"sections": [{"title": "狙い目の根拠",
                        "body": ["天井が判明していません。", "残す文。"]}]}
    a5, rm5 = resolve_contradictions(json.loads(json.dumps(b5)), {"天井"})
    t("★★消してよいのは、落とすと決めた文だけ★★",
      check(b5, a5, rm5) == [] and any("消えます" in x for x in check(b5, a5)))
    print(f"\n{ran[0]}/{ran[0]} 合格" if ok else "\n不合格あり")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="旧方式の先行記事に材料を足す")
    ap.add_argument("--slug")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    ms = targets(a.slug)
    if not a.slug:
        print("対象:", " ".join(m["slug"] for m in ms))
        return 0
    r = run(a.slug, a.apply)
    for p in r.get("problems") or []:
        print("  -", p)
    for x in r.get("added") or []:
        print("  ＋", x)
    if r.get("wrote"):
        print("書きました")
    elif not (r.get("problems")):
        print("（下見です。--apply で書きます）" if r.get("added") else "足すものがありません")
    return 1 if r.get("problems") else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except _sj.SafeJsonError as e:
        print(f"★入力データが読めません: {e}★")
        raise SystemExit(1)

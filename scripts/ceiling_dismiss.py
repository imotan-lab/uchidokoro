# -*- coding: utf-8 -*-
"""★機械が読んだ天井の候補を、2AIが「出典に無い読み違い」と決めたときに控える★（2026-10-29）

使い方:
  python scripts/ceiling_dismiss.py --apply <決定ファイル>   … 控える
  python scripts/ceiling_dismiss.py --list [--slug X]         … 控えを見る
  python scripts/ceiling_dismiss.py --selftest

★なぜ要るか★＝機械が天井を読めたのに恩恵が読めないと、育成と新台の材料集めは
「恩恵を確かめて記録してください」と2AIへ問う（add_machine_run.missing_benefit_questions）。
2AIが出典を読んで「その天井は出典に無い（読み違い）」と決めても、★答えを控える口が無く★、
同じ問いが毎朝出続けていた（10/27・パリピ孔明の「9スルー天井」）。

★決定ファイル★（Writeツールで書く）
  {"slug": "prskkm", "field": "ceiling#through-9-354de2", "urls": ["<問いの「読んだ先」のURL>"],
   "by": ["claude", "codex"], "why": "<15字以上・出典のどこを読んで無いと決めたか>"}
  ・urls は問いの「読んだ先」をそのまま（★読んだ先が変われば控えは効かず、問いが戻る★）
  ・field は問いに書いてある項目名をそのまま（★項目名は読んだ中身の指紋つき＝読み方が変われば別の問いになる★）
  ・by は claude と codex の組、Codexが利用制限なら claude-agent-a と claude-agent-b の組

★控えると★＝その項目名の問いは出なくなる。★記事には何も書かない★（天井を消す道具ではない）。
置き場＝Documents/uchidokoro/ceiling_dismissed.json（リポジトリの外）
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import local_paths as _lp                                   # noqa: E402

STORE = _lp.doc("ceiling_dismissed.json")
MIN_WHY = 15
SLUG_RE = re.compile(r"^[a-z0-9_]{1,40}$")
FIELD_RE = re.compile(r"^(?:ceiling|reset)(?:#[A-Za-z0-9_\-]{1,60})?$")


def _read() -> dict:
    """★無いは空・壊れていたら例外★（黙って0件と読まない）"""
    if not os.path.isfile(STORE):
        return {}
    with open(STORE, encoding="utf-8") as f:
        got = json.load(f)
    if not isinstance(got, dict) or not isinstance(got.get("machines", {}), dict):
        raise ValueError("ceiling_dismissed.json の形が違います")
    return got


def dismissed(slug: str) -> dict:
    """★その機種で、2AIが読み違いと決めた候補★＝{項目名: 読んだページのURLの集合}（読めなければ例外）

    ★読むたびに書くときと同じ条件を確かめる★（判断者の組・理由・URL）＝満たさない控えは無かったことにする
    （JSONとして読める壊れ方〈null・{}・1人・1文字〉で問いが消えないように・Codex review efdd1f8f の2）
    """
    rows = (_read().get("machines") or {}).get(slug) or {}
    out = {}
    if not isinstance(rows, dict):
        return out
    for fld, rec in rows.items():
        if not isinstance(rec, dict):
            continue
        if decision_problems({"slug": slug, "field": fld, **{k: rec.get(k) for k in ("by", "why", "urls")}}):
            continue
        out[fld] = set(rec.get("urls") or [])
    return out


def dismissed_fields(slug: str) -> set:
    """★項目名だけ★（一覧用）"""
    return set(dismissed(slug))


def decision_problems(dec) -> list:
    import confirmed_values as _cv
    if not isinstance(dec, dict):
        return ["決定ファイルが辞書ではありません"]
    ng = []
    if not SLUG_RE.match(str(dec.get("slug") or "")):
        ng.append("slug が英小文字・数字・下線ではありません")
    if not FIELD_RE.match(str(dec.get("field") or "")):
        ng.append("field が天井の項目名（ceiling… / reset…）ではありません")
    if _cv.judges_pair(dec.get("by")) is None or not _cv.judges_exact(dec.get("by")):
        ng.append("判断者は claude と codex（または claude-agent-a と claude-agent-b）の組です")
    if len(str(dec.get("why") or "").strip()) < MIN_WHY:
        ng.append(f"理由が{MIN_WHY}字より短いです")
    # ★★どのページで読んだ候補かを控える★★（Codex review efdd1f8f の1）＝問いの「読んだ先」をそのまま。
    #   ★読んだ先が変われば（新しい出典に載った等）、控えは効かず問いが戻る★
    urls = dec.get("urls")
    if (not isinstance(urls, list) or not urls
            or not all(isinstance(u, str) and u.startswith("http") for u in urls)):
        ng.append("urls（問いに書いてある「読んだ先」のURL）が要ります")
    return ng


def record(dec: dict) -> int:
    ng = decision_problems(dec)
    if ng:
        print("★控えません★")
        for x in ng:
            print("  ・" + x)
        return 1
    data = _read() or {"schema_version": "ceiling-dismissed/v1", "machines": {}}
    rows = data.setdefault("machines", {}).setdefault(dec["slug"], {})
    rows[dec["field"]] = {"by": sorted(dec["by"]), "why": str(dec["why"]).strip()[:500],
                          "urls": sorted(set(dec["urls"])),
                          "at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M")}
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=1) + "\n")
    os.replace(tmp, STORE)
    print(f"控えました: {dec['slug']} {dec['field']}（この項目名の問いは出なくなります）")
    return 0


def selftest() -> int:
    import tempfile
    global STORE
    res = []

    def t(name, cond):
        res.append(bool(cond))
        print(("✅" if cond else "❌") + " " + name)

    keep = STORE
    try:
        STORE = os.path.join(tempfile.mkdtemp(prefix="cdis_"), "ceiling_dismissed.json")
        ok = {"slug": "zzz_a", "field": "ceiling#through-9-354de2", "by": ["claude", "codex"],
              "why": "出典2社の天井の節に9スルーの記述が無い（試験）", "urls": ["https://a.invalid/1"]}
        t("★控える前は何も無い★", dismissed_fields("zzz_a") == set())
        t("★決定を受け取って控える★", record(ok) == 0 and dismissed_fields("zzz_a") == {ok["field"]})
        t("★判断者が1人なら断る★", decision_problems({**ok, "by": ["claude"]}) != [])
        t("★判断者の組を混ぜたら断る★", decision_problems({**ok, "by": ["claude", "claude-agent-b"]}) != [])
        t("★代役の組は受け取る★", decision_problems({**ok, "by": ["claude-agent-a", "claude-agent-b"]}) == [])
        t("★理由が短ければ断る★", decision_problems({**ok, "why": "無い"}) != [])
        t("★天井以外の項目名は断る★", decision_problems({**ok, "field": "payout_rate"}) != [])
        t("★読んだ先のURLが無ければ断る★", decision_problems({**ok, "urls": []}) != [])
        t("★控えには読んだ先が残る★", dismissed("zzz_a") == {ok["field"]: {"https://a.invalid/1"}})
        with open(STORE, "w", encoding="utf-8") as f:
            json.dump({"machines": {"zzz_a": {"ceiling#x": None, "ceiling#y": {},
                                              "ceiling#z": {"by": ["claude"], "why": "短", "urls": ["https://a"]}}}}, f)
        t("★★読める形で壊れた控え（null・空・1人・短い理由）は無かったことにする★★（Codex review efdd1f8f の2）",
          dismissed("zzz_a") == {})
        with open(STORE, "w", encoding="utf-8") as f:
            f.write("{壊れた")
        try:
            dismissed_fields("zzz_a")
            t("★控えが壊れていたら例外★（黙って0件と読まない）", False)
        except ValueError:
            t("★控えが壊れていたら例外★（黙って0件と読まない）", True)
    finally:
        STORE = keep
    print(f"\n{sum(res)}/{len(res)} 合格")
    return 0 if all(res) else 1


def main() -> int:
    for _st in (sys.stdout, sys.stderr):
        try:
            _st.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description="2AIが読み違いと決めた天井の候補を控える")
    ap.add_argument("--apply", default="")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--slug", default="")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.apply:
        with open(a.apply, encoding="utf-8") as f:
            return record(json.load(f))
    if a.list:
        for s, rows in sorted((_read().get("machines") or {}).items()):
            if a.slug and s != a.slug:
                continue
            for k, v in rows.items():
                print(f"{s} {k} {'・'.join(v.get('by') or [])} {v.get('at')} {v.get('why')}")
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())

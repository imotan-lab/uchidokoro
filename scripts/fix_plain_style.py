#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""★常体の文末を「です・ます」へそろえる★（CLAUDE.md の文体ルール）

★決まりごと★
  「機種記事は**です・ます調で統一**する（だ・である は禁止）」

★この道具がやること★
  ★文末の言い切りだけ★を、決めてある対のとおりに置き換える。

★この道具がやらないこと★
  ・文の中身を変えない（数値・条件・言い回しは触らない）
  ・新しい文を作らない
  ・箇条書きの項目・体言止め・見出しには触らない

★触る条件（全部そろったときだけ）★
  ①その文が「。」で終わっている（＝箇条書きの項目ではない）
  ②12字以上（＝短い項目名を拾わない）
  ③行頭が「**」「・」「-」でない（＝見出しや箇条書きではない）
  ④文末が、下の表にある形とちょうど一致する

★なぜ表で持つのか★
  「〜する」で終わる語尾を機械で活用させると、機種名（「七つの魔剣が支配する」）
  まで書き換えてしまう。★実際、字面で探したら誤検知した★。
  だから**書き換えてよい形を1つずつ数え上げる**。
  表に無い形は触らない（安全側）。

使い方:
  python scripts/fix_plain_style.py              （全機種・見るだけ）
  python scripts/fix_plain_style.py --slug tenken
  python scripts/fix_plain_style.py --apply
  python scripts/fix_plain_style.py --selftest
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import safe_json as _sj      # noqa: E402
import page_decision as _pd  # noqa: E402

DETAILS = os.path.join(BASE, "assets", "data", "machine-details")

# ★★書き換えてよい文末の対★★（★1つずつ数え上げる★）
#   左が常体の終わり方、右が「です・ます」の終わり方。
#   ★意味を変えない対だけ★＝丁寧さだけが変わる。
ENDINGS = (
    ("がある。", "があります。"),
    ("される。", "されます。"),
    ("られる。", "られます。"),
    ("できる。", "できます。"),
    ("広がる。", "広がります。"),
    ("変わる。", "変わります。"),
    ("増やせる。", "増やせます。"),
    ("狙える。", "狙えます。"),
    ("ヤメる。", "ヤメましょう。"),
    ("立ち回る。", "立ち回りましょう。"),
    ("覚えておく。", "覚えておきましょう。"),
    ("必要だ。", "必要です。"),
    ("重要だ。", "重要です。"),
    ("有効だ。", "有効です。"),
    ("である。", "です。"),
)
# ★左右が同じ対は入れない★（何も変わらない書き換えを作らないため）
ENDINGS = tuple((a, b) for a, b in ENDINGS if a != b)

_SKIP_HEAD = ("**", "・", "-", "＊", "※")


def rewrite_sentence(sent: str):
    """1文を書き換えた結果を返す（変えないなら None）。"""
    s = str(sent or "")
    t = s.strip()
    if not t.endswith("。"):
        return None                     # ①句点で終わっていない
    if len(t) < 12:
        # ②短すぎる
        # ★2026-08-21に20→12へ下げた★＝「朝一から複数の狙い目が広がる。」(14字)
        #   「スルー数はAT終了でリセットされる。」(17字) を取りこぼしていた。
        #   ★下げても安全★＝下の「表に載っている終わり方」で既に強く絞れている
        #   （表に無い形は触らないので、短い項目名を巻き込まない）。
        return None
    if t.startswith(_SKIP_HEAD):
        return None                     # ③見出し・箇条書き
    for a, b in ENDINGS:
        if t.endswith(a):
            return s.replace(a, b) if s.rstrip().endswith(a) else None
    return None


def plan_for(detail: dict) -> list:
    """書き換える場所を返す（★書かない★）。"""
    out = []
    for si, sec in enumerate(detail.get("sections") or []):
        if sec.get("type") == "settei":
            continue
        for bi, line in enumerate(sec.get("body") or []):
            if not isinstance(line, str):
                continue
            parts = re.split(r"(?<=。)", line)
            new_parts, hit = [], False
            for p in parts:
                r = rewrite_sentence(p)
                if r is not None:
                    new_parts.append(r)
                    hit = True
                else:
                    new_parts.append(p)
            if hit:
                out.append((si, bi, line, "".join(new_parts),
                            str(sec.get("title") or "")))
    return out


def run(slug: str | None = None, apply_it: bool = False) -> dict:
    rows = _sj.read_rows(os.path.join(BASE, "assets", "data", "machines.json"))
    result = {"changed": [], "n": 0}
    for m in rows:
        sl = m.get("slug")
        if slug and sl != slug:
            continue
        # ★旧形式の公開記事だけ★（新台経路は別の作り方）
        if _pd.machine_class(m) != "LEGACY_COMPLETE":
            continue
        p = os.path.join(DETAILS, sl + ".json")
        if not os.path.isfile(p):
            continue
        d = _sj.read_json(p, expect=dict)
        plan = plan_for(d)
        if not plan:
            continue
        result["n"] += len(plan)
        result["changed"].append({"slug": sl, "items": plan})
        if apply_it:
            for si, bi, _old, new, _t in plan:
                d["sections"][si]["body"][bi] = new
            tmp = p + ".tmp"
            with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
                f.write("\n")
            os.replace(tmp, p)
    return result


def _selftest() -> int:
    ng = []

    def t(name, cond):
        print(("✅ " if cond else "❌ ") + name)
        if not cond:
            ng.append(name)

    t("★文末だけを丁寧にする★",
      rewrite_sentence("高設定域では通常時のモード移行率が優遇されているとの見方がある。")
      == "高設定域では通常時のモード移行率が優遇されているとの見方があります。")
    t("　受け身の言い切りも直す",
      rewrite_sentence("ボーナス間天井は通常ボーナスが当選した台に適用される。")
      == "ボーナス間天井は通常ボーナスが当選した台に適用されます。")
    t("★★句点で終わっていない行は触らない★★（箇条書きの項目）",
      rewrite_sentence("虚構連モードスルー時はゲーム数を引き継ぐ") is None)
    t("★★見出しの行は触らない★★",
      rewrite_sentence("**リセット**：天井が大幅短縮され0G〜から狙える。") is None)
    t("★★機種名を書き換えない★★（「支配する」で終わる実例がある）",
      rewrite_sentence("**機種名**：スマスロ 七つの魔剣が支配する") is None)
    t("　短すぎる文は触らない（7字）", rewrite_sentence("狙い目が広がる。") is None)
    t("　14字なら直す（下限を下げた分）",
      rewrite_sentence("朝一から複数の狙い目が広がる。")
      == "朝一から複数の狙い目が広がります。")
    t("★★表に無い形は触らない（安全側）★★",
      rewrite_sentence("この機種は朝から打つと勝ちやすいという噂もあるらしい。")
      is None)
    t("　もう丁寧な文は触らない",
      rewrite_sentence("高設定域ではモード移行率が優遇されている可能性があります。")
      is None)
    t("　1行に2文あっても、当てはまる文だけ直す",
      plan_for({"sections": [{"title": "x", "body": [
          "天井は999Gです。AT間天井とボーナス間天井は独立してカウントされる。"]}]})[0][3]
      == "天井は999Gです。AT間天井とボーナス間天井は独立してカウントされます。")
    t("　設定示唆の表は触らない",
      plan_for({"sections": [{"title": "x", "type": "settei", "body": [
          "高設定域では通常時のモード移行率が優遇されているとの見方がある。"]}]}) == [])

    print()
    print(f"{11 - len(ng)}/11 " + ("合格" if not ng else "不合格"))
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--slug")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()

    r = run(a.slug, a.apply)
    print(("★書きました★" if a.apply else "★見るだけ（--apply で書きます）★")
          + f" {len(r['changed'])} 機種 / {r['n']} 行")
    for c in r["changed"]:
        print(f"  {c['slug']}")
        for _si, _bi, old, new, title in c["items"]:
            # ★変わったところだけを見せる★
            i = 0
            while i < min(len(old), len(new)) and old[i] == new[i]:
                i += 1
            print(f"      [{title}] …{old[max(0, i - 26):][:46]}")
            print(f"      → …{new[max(0, i - 26):][:46]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

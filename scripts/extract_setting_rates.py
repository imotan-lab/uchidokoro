#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""extract_setting_rates.py — setting.html の設定別確率を取り出して在庫に載せる

★なぜ要るか（Codex 10巡目 (a)-5）★
  小役カウンター「ポチポチくん」の設定別確率は `setting.html` の
  `MACHINE_CONFIGS` に**HTMLへ直書き**されている。
  claim の在庫にも C5 にも公開ゲートにも到達していないので、
  ここに誤った確率を書けば、検査を一度も通らずに公開される。

★何をするか★
  `MACHINE_CONFIGS` の `rates`（機種→項目→設定→確率）を機械的に取り出し、
  claim_inventory と同じ形の「検証が要る枠」に変換する。
  ★HTMLを書き換えることはしない（読むだけ）★

使い方:
    python scripts/extract_setting_rates.py --selftest
    python scripts/extract_setting_rates.py --list          # 機種ごとの件数
    python scripts/extract_setting_rates.py --slug hokuto   # 中身を見る
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

SETTING_HTML = os.path.join(BASE, "setting.html")

# rates: { 機種: { 項目: { 設定: 値 } } } を素直に読むための正規表現
_MACHINE_BLOCK = re.compile(
    r"^\s{2}([A-Za-z0-9_]+)\s*:\s*\{", re.M)
_RATES_BLOCK = re.compile(r"rates\s*:\s*\{")
# 1/259.0 / 259 / 0.0039 のいずれか
_NUM = r"(?:1\s*/\s*)?[0-9]+(?:\.[0-9]+)?"
_RATE_ENTRY = re.compile(
    r"([A-Za-z0-9_]+)\s*:\s*\{([^{}]*)\}")
_SETTING_VAL = re.compile(r"([1-6])\s*:\s*(" + _NUM + r")")


def _find_block(text: str, start: int) -> tuple[str, int]:
    """`{` の位置から対応する `}` までを返す（入れ子を数える）。"""
    depth, i = 0, start
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1], i + 1
        i += 1
    return "", len(text)


def extract_rates(html: str) -> dict:
    """{機種: {項目: {設定: 値}}} を返す。読めない箇所は黙って捨てない。"""
    out: dict = {}
    m = re.search(r"const MACHINE_CONFIGS\s*=\s*\{", html)
    if not m:
        return out
    body, _ = _find_block(html, m.end() - 1)
    for mm in _MACHINE_BLOCK.finditer(body):
        slug = mm.group(1)
        block, _ = _find_block(body, mm.end() - 1)
        rm = _RATES_BLOCK.search(block)
        if not rm:
            continue
        rates_block, _ = _find_block(block, rm.end() - 1)
        per_field: dict = {}
        for fm in _RATE_ENTRY.finditer(rates_block):
            field, inner = fm.group(1), fm.group(2)
            vals = {s: v.replace(" ", "") for s, v in _SETTING_VAL.findall(inner)}
            if vals:
                per_field[field] = vals
        if per_field:
            out[slug] = per_field
    return out


def _sha(s: str) -> str:
    import hashlib
    return hashlib.sha256(str(s).encode("utf-8")).hexdigest()


def _norm(raw: str):
    """「1/259.0」を在庫と同じ形に。読めなければ None（＝公開できない）。"""
    m = re.fullmatch(r"\s*1\s*/\s*([0-9]+(?:\.[0-9]+)?)\s*", str(raw or ""))
    if not m:
        return None
    return {"amount": float(m.group(1)), "unit": "1/x", "plus_alpha": False}


def as_slots(slug: str, rates: dict) -> list:
    """在庫の枠と同じ形に変換する（設定ごとに1枠・束で縛る）。"""
    slots = []
    for field, per_setting in sorted(rates.items()):
        group = f"{slug}:setting_rates:{field}"
        for setting, raw in sorted(per_setting.items()):
            slots.append({
                "slot_id": f"{slug}:setting_rate.{field}:setting={setting}",
                "field_key": f"setting_rate.{field}",
                "conditions": {"mode": "ANY", "scope": "NONE",
                               "counter_basis": "NONE", "setting": setting},
                "atomic_group_id": group,
                "expected_value_kind": "PROBABILITY",
                "expected_unit": "1/x",
                "expected_operator": "EXACT",
                "expected_setting": setting,
                "render_unit_id": "PROBABILITY_ONE_OVER_X",
                "source_pointer": f"/setting.html/MACHINE_CONFIGS/{slug}/rates/{field}/{setting}",
                "current_text": raw,
                "source_text_sha256": _sha(f"{slug}|{field}|{setting}|{raw}"),
                # 「1/259.0」の形なら値として読める（それ以外は None＝公開できない）
                "current_value": _norm(raw),
                "value_issue": None if _norm(raw) else "NOT_PROBABILITY_FORM",
                # ★意味の検証器がまだ無い型★＝公開ゲートで必ず止まる
                "allowlisted_type": False,
                "verify_state": "UNVERIFIED",
            })
    return slots


def load_all() -> dict:
    if not os.path.isfile(SETTING_HTML):
        return {}
    return extract_rates(open(SETTING_HTML, encoding="utf-8").read())


# ---------------------------------------------------------------- selftest

_SAMPLE = """
const MACHINE_CONFIGS = {
  hokuto: {
    note: 'テスト',
    fields: [ { id: 'bb', label: 'BB回数' } ],
    rates: {
      bb: { 1: 1/259.0, 2: 1/255.0, 6: 1/234.9 },
      cherry: { 1: 1/94.0, 6: 1/89.0 }
    }
  },
  sf5: {
    rates: { bb: { 1: 1/300.0, 6: 1/250.0 } }
  }
};
"""


def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    got = extract_rates(_SAMPLE)
    t("機種ごとに確率を取り出せる", set(got) == {"hokuto", "sf5"})
    t("項目ごと・設定ごとに分かれている",
      got["hokuto"]["bb"] == {"1": "1/259.0", "2": "1/255.0", "6": "1/234.9"})
    t("★項目をまたいで混ざらない", got["hokuto"]["cherry"]["6"] == "1/89.0")

    slots = as_slots("hokuto", got["hokuto"])
    t("在庫の枠に変換できる（設定ごとに1枠）", len(slots) == 5)
    t("★同じ項目の設定は同じ束に入る（1つ欠けたら行ごと止める）",
      len({s["atomic_group_id"] for s in slots if s["field_key"] == "setting_rate.bb"}) == 1)
    t("★★意味の検証器が無い型なので自動採用の対象にしない★★",
      all(s["allowlisted_type"] is False for s in slots))
    t("　枠に設定が入っている", slots[0]["conditions"]["setting"] in ("1", "2", "6"))

    real = load_all()
    t("実データ（setting.html）からも取り出せる", len(real) > 0)
    t("　実データの枠も作れる",
      len(as_slots(next(iter(real)), real[next(iter(real))])) > 0)

    ng = [n for n, c in results if not c]
    print(f"\n{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--slug")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    data = load_all()
    if args.slug:
        r = data.get(args.slug)
        if not r:
            print("その機種の確率はありません:", args.slug)
            return 1
        print(json.dumps(as_slots(args.slug, r), ensure_ascii=False, indent=1))
        return 0
    if args.list:
        total = 0
        for slug in sorted(data):
            n = len(as_slots(slug, data[slug]))
            total += n
            print(f"  {slug:<24} {n:>3} 枠")
        print(f"\nポチポチくんの確率: {len(data)} 機種 / {total} 枠"
              f"（すべて未検証・意味の検証器なし＝公開ゲートで止まる）")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

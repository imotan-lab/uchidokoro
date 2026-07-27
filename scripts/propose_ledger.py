#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""propose_ledger.py — 分類台帳の下書きを機械的に作る（提案のみ・適用しない）

build_ledger.py が出した作業リスト(_design/ledger_todo.json)に対し、
**構造的に安全と言い切れるものだけ** ALLOW を提案する。残りは判断保留（null）。

★方針（安全側）★
  - 提案するのは「スペックの事実（機械割/純増/獲得枚数など）＋数値」型だけ。
  - 少しでも計算・価値・行動の含みがあれば提案しない（平均獲得枚数・天井到達時・期待・狙い目 等）。
  - 提案は提案。**適用はしない**（verdict を書き込んだ台帳を別ファイルに出すだけ）。
  - gates.py の絶対禁止に触れるものは、そもそもここに来ない（先にDROPされている）。

使い方:
    python scripts/propose_ledger.py                       # 提案の集計だけ
    python scripts/propose_ledger.py --out _design/ledger_draft.json
    python scripts/propose_ledger.py --list 20             # 提案内容の実例
"""
from __future__ import annotations

import argparse
import json
import os
import re

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 「スペックの事実」として扱ってよいラベル（完全一致・括弧内の設定番号等は許容）
_SPEC_LABEL = re.compile(
    r"^(?:機械割|出玉率|純増|AT純増|ART純増|BIG純増|ボーナス純増|BB純増|RB純増|"
    r"ジャングルボーナス純増|純増\(BT\)|コイン持ち|コイン単価|払い出し率|"
    r"BIG獲得枚数|REG獲得枚数|BB獲得枚数|RB獲得枚数|ボーナス獲得枚数)"
    r"(?:\([^)]*\))?$")

# 値として許す形（数値＋単位。文章や動詞が混ざるものは対象外）
_SPEC_VALUE = re.compile(
    r"^(?:約|およそ)?[0-9０-９][0-9０-９.,\-〜~/／ ]*"
    r"(?:%|％|枚|枚/G|枚/g|G|pt|円|倍)?"
    r"(?:\s*[〜~\-]\s*(?:約)?[0-9０-９][0-9０-９.,]*(?:%|％|枚|枚/G|G|pt|円|倍)?)?"
    r"(?:\s*\([^)]*\))?$")

# これらを含むものは絶対に提案しない（計算値・価値判断・行動示唆の疑い）
# ★平均・想定・換算も除外★（括弧内に「平均約500枚」のような推定値が紛れていた実例あり。
#   独立検査で検出。スペックの事実と、そこから導いた推定値を混ぜない）
_NEVER = re.compile(
    r"期待|収支|平均|想定|換算|逆算|天井到達|時給|プラス|マイナス|得する|お得|勝|狙|"
    r"旨味|リターン|回収|優秀|おすすめ|推奨|有利|不利|効率|投資|コスト|損")


# 「基本スペック」欄の本文行のうち、"項目:値" の定型だけを対象にする
_SPEC_PREFIX = "基本スペック / "
_SPEC_KIND = (r"(?:AT|ART|BB|RB|BIG|REG|上位AT|通常AT|ボーナス|ジャングルボーナス)?"
              r"(?:純増|獲得枚数)")
_SPEC_BODY_SIMPLE = re.compile(
    r"^" + _SPEC_KIND + r"[:：]\s*(?:最大|約|およそ)?[0-9０-９][0-9０-９.,]*"
    r"\s*(?:枚/G|枚/g|枚|%|％)?(?:\([^)]*\))?$")
# 機械割の設定別列挙（設1:97.5% / 設6:114.9% など）
_SPEC_BODY_KAIWARI = re.compile(
    r"^機械割(?:\(設定?[0-9]\))?[:：]\s*"
    r"(?:設定?[0-9][:：]\s*[0-9]+(?:\.[0-9]+)?[%％]\s*(?:/|／)?\s*)+"
    r"(?:\([^)]*\))?$|^機械割(?:\(設定?[0-9]\))?[:：]\s*[0-9]+(?:\.[0-9]+)?[%％]$")


def propose(item: dict) -> tuple[str | None, str]:
    """(verdict, 理由) を返す。verdict=None は判断保留。"""
    text = item.get("text", "")
    path = re.sub(r"\[\d+\]", "[]", item.get("path", ""))

    # 「基本スペック」セクションの定型スペック行
    if path == "sections[].body[]" and text.startswith(_SPEC_PREFIX):
        body = text[len(_SPEC_PREFIX):].strip()
        if _NEVER.search(body):
            return None, "計算値・価値判断の疑いがあるため保留"
        if _SPEC_BODY_SIMPLE.match(body) or _SPEC_BODY_KAIWARI.match(body):
            return "ALLOW", "スペックの事実（断定ではない。出典検証はPhase 2の別軸）"
        return None, "基本スペック欄だが定型でない"

    if path not in ("factTable[]", "summaryBoxes[]"):
        return None, "表・要約以外は自動提案しない"
    if _NEVER.search(text):
        return None, "計算値・価値判断の疑いがあるため保留"
    parts = text.split(" / ")
    if len(parts) != 2:
        return None, "ラベルと値の2要素でない"
    label, value = parts[0].strip(), parts[1].strip()
    if not _SPEC_LABEL.match(label):
        return None, "スペックのラベルとして未登録"
    if not _SPEC_VALUE.match(value):
        return None, "値が数値＋単位の形でない"
    return "ALLOW", "スペックの事実（断定ではない。出典検証はPhase 2の別軸）"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--todo", default=os.path.join(BASE, "_design", "ledger_todo.json"))
    ap.add_argument("--out")
    ap.add_argument("--list", type=int, default=0)
    args = ap.parse_args()

    items = json.load(open(args.todo, encoding="utf-8"))
    proposed, held = [], []
    for it in items:
        v, why = propose(it)
        rec = {**it, "verdict": v, "reason": why}
        (proposed if v else held).append(rec)

    n_occ = sum(x["count"] for x in proposed)
    print("=" * 66)
    print(f"作業リスト {len(items)} 件 → 自動提案 ALLOW {len(proposed)} 件（延べ {n_occ} 箇所）"
          f" / 判断保留 {len(held)} 件")
    print(f"提案後の残作業: {len(held)} 件（延べ {sum(x['count'] for x in held)} 箇所）")
    print("=" * 66)

    if args.list:
        print("\n■ 自動提案 ALLOW の実例")
        for x in proposed[:args.list]:
            print(f"  {x['count']:>3}箇所  {x['text'][:70]}")
        print("\n■ 判断保留の実例（人／第二AIが決める）")
        for x in held[:args.list]:
            print(f"  {x['count']:>3}箇所 [{x['reason']}] {x['text'][:70]}")

    if args.out:
        # 台帳形式（gates.py が読む形）＋ 監査用の元情報を併記
        ledger = {x["atom_id"]: {"verdict": x["verdict"], "note": x["reason"],
                                 "text": x["text"], "count": x["count"]}
                  for x in proposed}
        json.dump(ledger, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"\n下書き台帳を書き出しました: {args.out}（{len(ledger)}件・★未適用★）")
        held_path = args.out.replace(".json", "_held.json")
        json.dump(held, open(held_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"判断保留リスト: {held_path}（{len(held)}件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

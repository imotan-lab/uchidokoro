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
    r"BIG獲得枚数|REG獲得枚数|BB獲得枚数|RB獲得枚数|ボーナス獲得枚数|"
    r"ボーナス合算確率|初当たり確率|BIG確率|REG確率|AT初当たり確率|設定段階|設定)"
    r"(?:\([^)]*\))?$")

# 確率（1/179.6）・コイン持ち（約32G/50枚）・設定別の列挙（設1:97.9%〜設6:114.9%）・
# 複数区分の純増（通常AT約3.2枚/G / 上位AT約8.0枚/G）など、実データの定型を追加で許可する。
_SPEC_VALUE_EXTRA = re.compile(
    r"^(?:"
    r"1\s*/\s*[0-9][0-9.,]*"                                   # 確率
    r"|(?:約|およそ)?[0-9][0-9.]*\s*G\s*/\s*[0-9]+枚(?:\([^)]*\))?"   # コイン持ち
    r"|[0-9]段階(?:設定)?(?:\([^)]*\))?"                          # 設定段階
    r"|(?:設定?[0-9]\s*[:：]?\s*[0-9][0-9.]*[%％]\s*[〜～]?\s*)+(?:\([^)]*\))?"  # 設定別列挙
    r"|(?:[ぁ-んァ-ヶ一-龥A-Za-z0-9()（）]{1,16}\s*[:：]?\s*(?:約)?[0-9][0-9.]*\s*枚/G\s*)+"  # 区分別純増
    r")$")

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
# 設定段階の宣言（6段階 / 6段階(1〜6) 等）。欠番のある列挙は gates が別途DROPする
_SET_STAGE = re.compile(r"^設定(?:段階)?[:：]\s*[0-9]段階(?:設定)?(?:\(設定?[0-9]\s*[〜～]\s*[0-9]\))?$")
# 「項目:数値+単位」の定型（コイン持ち・獲得枚数・純増 等）
_SPEC_KV = re.compile(
    r"^[ぁ-んァ-ヶ一-龥A-Za-z0-9()（）・]{2,20}[:：]\s*(?:最大|約|およそ)?"
    r"[0-9][0-9.,]*\s*(?:枚/G|枚|%|％|G|G/50枚|pt|円|段階)"
    r"(?:\s*\([^)]*\))?$")
# 機械割の設定別列挙（設1:97.5% / 設6:114.9% など）
_SPEC_BODY_KAIWARI = re.compile(
    r"^機械割(?:\(設定?[0-9]\))?[:：]\s*"
    r"(?:設定?[0-9][:：]\s*[0-9]+(?:\.[0-9]+)?[%％]\s*(?:/|／)?\s*)+"
    r"(?:\([^)]*\))?$|^機械割(?:\(設定?[0-9]\))?[:：]\s*[0-9]+(?:\.[0-9]+)?[%％]$")


# 統一セクション見出し（CLAUDE.md の IDEAL_ORDER）。見出しそのものは事実主張ではない。
_FIXED_TITLES = {
    "天井・恩恵", "基本スペック", "期待値の目安", "朝一・リセット情報", "設定示唆まとめ",
    "狙い目の根拠", "ヤメ時の判断", "立ち回りのコツ", "噂・未確定情報",
    "設定判別のポイント", "設定狙いのポイント", "ゲーム性", "このページの役割",
}

# SEOタイトルの定型（機種名＋固定の後置き）。機種名部分は自由だが後置きが定型。
_SEO_TITLE = re.compile(
    r"^.{1,40}?\s(?:狙い目・天井・期待値まとめ|狙い目・設定差まとめ|設定判別|狙い目)$")

# 天井まわりのラベル（「◯◯天井」「天井」）。値はG数・pt・周期・+α の事実表記だけ許す。
_TENJO_LABEL = re.compile(r"^(?:[ぁ-んァ-ヶ一-龥A-Za-z0-9]{0,10}天井|天井)(?:\([^)]*\))?$")
_TENJO_VALUE = re.compile(
    r"^(?:[^。]{0,80})$")   # 文末が無い（＝文章でない）ことだけを条件にし、語の中身は _NEVER で弾く
_TENJO_VALUE_SHAPE = re.compile(
    r"^[^。]*[0-9][^。]*(?:G|pt|周期|回|枚|%|％)[^。]*$")


def _tenjo_ok(label: str, value: str) -> bool:
    """天井系ラベル＋数値主体の値。動詞や評価語は _NEVER 側で既に除外されている。"""
    if not _TENJO_LABEL.match(label):
        return False
    if not (_TENJO_VALUE.match(value) and _TENJO_VALUE_SHAPE.match(value)):
        return False
    # 「〜です」「〜ます」「〜しましょう」等の文章は対象外（表の値ではない）
    return not re.search(r"(?:です|ます|ましょう|ください|でしょう|と思)", value)


# 価値判断・行動示唆の語（仕様の記載でもこれが混ざれば保留に落とす）
_VALUE_JUDGE = re.compile(
    r"期待|収支|時給|得する|お得|勝|狙|旨味|リターン|回収|優秀|おすすめ|推奨|"
    r"有利|不利|効率|投資|損得")

# 小役確率の列挙（「名前:1/○○」を / で連ねた形。評価語を含まない）
_KOYAKU_LIST = re.compile(
    r"^[^:：]{1,24}[:：]\s*(?:[ぁ-んァ-ヶ一-龥A-Za-z0-9()（）・]{1,16}\s*[:：]\s*"
    r"1\s*/\s*[0-9][0-9.,]*\s*(?:/|／)?\s*)+$")

# 仕様の記載（コンプリート機能など）。数値を含む機構の説明で、評価も推奨も含まない。
_SPEC_MECHANISM = re.compile(
    r"^(?:コンプリート機能|有利区間|規定[^。]{0,10})[^。]{0,80}$")


# ラベルとして許さない語（行動をすすめる・評価する・収支を語る）
_LABEL_NG = re.compile(
    r"期待|収支|プラス|マイナス|利益|損|時給|回収|投資|勝|儲|"
    r"狙え|狙う|打て|打つ|拾|着席|ヤメ時|やめ時|推奨|おすすめ|必ず|絶対|確実|"
    r"有利|不利|優秀|甘い|辛い|効率|得")


def propose(item: dict) -> tuple[str | None, str]:
    """(verdict, 理由) を返す。verdict=None は判断保留。"""
    text = item.get("text", "")
    path = re.sub(r"\[\d+\]", "[]", item.get("path", ""))

    # 「基本スペック」セクションの定型スペック行
    if path == "sections[].body[]" and text.startswith(_SPEC_PREFIX):
        body = text[len(_SPEC_PREFIX):].strip()
        # ★仕様の記載は先に判定★（「差枚最大マイナスを基点に」等、_NEVER の語を
        #   含むが計算値でも価値判断でもないものを取りこぼさないため）
        if _SPEC_MECHANISM.match(body) and not _VALUE_JUDGE.search(body):
            return "ALLOW", "仕様の事実（機構の記載）"
        if _NEVER.search(body):
            return None, "計算値・価値判断の疑いがあるため保留"
        if _SPEC_BODY_SIMPLE.match(body) or _SPEC_BODY_KAIWARI.match(body):
            return "ALLOW", "スペックの事実（断定ではない。出典検証はPhase 2の別軸）"
        # 設定段階の宣言（6段階など）は事実の記載。★欠番のある列挙は gates が既にDROPする★
        if _SET_STAGE.match(body):
            return "ALLOW", "設定段階の記載（欠番の暗示は gates 側で別途DROP）"
        # コイン持ち・獲得枚数など「項目:数値+単位」の定型
        if _SPEC_KV.match(body):
            return "ALLOW", "スペックの事実（項目:数値の定型）"
        # 「天井:AT間1400G(設定変更後1000G)」のような天井の事実
        if re.search(r"[:：]", body):
            lab, val = re.split(r"[:：]", body, maxsplit=1)
            if _tenjo_ok(lab.strip(), val.strip()):
                return "ALLOW", "天井の事実（ラベル＋数値表記）"
            # 小役確率の列挙（リプレイ:1/8.2 / スイカ:1/99.9 …）
            if _KOYAKU_LIST.match(body):
                return "ALLOW", "小役確率の列挙（スペックの事実）"
        return None, "基本スペック欄だが定型でない"

    # ★数字を持たない短いラベル★（表の見出し・案内の項目名など）
    #   数値主張が無く、行動をすすめる語も評価語も含まないものだけ。
    #   例:「設定示唆まとめ / AT終了画面」「設定判別 / ポチポチくんへ移動」
    if (not re.search(r"[0-9０-９]", text) and len(text) <= 40
            and not _LABEL_NG.search(text) and not re.search(r"[。！？!?]", text)):
        return "ALLOW", "数値を含まない短いラベル（表の見出し・項目名）"

    # 統一セクション見出し（title 単体の原子）
    if path == "sections[].title":
        return (("ALLOW", "統一セクション見出し（事実主張を含まない定型ラベル）")
                if text in _FIXED_TITLES else (None, "統一見出しリストに無い"))

    # SEOタイトルの定型（検索結果に出る見出し。機種名＋固定の後置き）
    if path == "seo.title":
        if _NEVER.search(text.replace("期待値まとめ", "").replace("狙い目", "")):
            return None, "定型外の語を含む"
        return (("ALLOW", "SEOタイトルの定型（機種名＋固定の後置き）")
                if _SEO_TITLE.match(text) else (None, "SEOタイトルの定型でない"))

    if path not in ("factTable[]", "summaryBoxes[]"):
        return None, "表・要約以外は自動提案しない"
    if _NEVER.search(text):
        return None, "計算値・価値判断の疑いがあるため保留"
    parts = text.split(" / ")
    if len(parts) < 2:
        return None, "ラベルと値の2要素でない"
    # 「純増 / 通常AT約3.2枚/G / 上位AT約8.0枚/G」のように値側が / を含む定型も許す
    label, value = parts[0].strip(), " / ".join(parts[1:]).strip()
    if _tenjo_ok(label, value):
        return "ALLOW", "天井の事実（ラベル＋数値表記）"
    if not _SPEC_LABEL.match(label):
        return None, "スペックのラベルとして未登録"
    if not (_SPEC_VALUE.match(value) or _SPEC_VALUE_EXTRA.match(value)):
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

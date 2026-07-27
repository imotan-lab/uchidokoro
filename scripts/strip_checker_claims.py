#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""strip_checker_claims.py — 「計算できない断定」を文ごと削る（チェッカー注意書き／記事本文）

★何をするか★
  machines.json の checker.*.note に含まれる、当サイトでは計算できない断定
  （「580G〜でプラス域」「0Gからプラス期待値」など）を、**その文だけ**削除する。
  数値そのもの（天井G数・閾値）は触らない。書き換えもしない（新しい文を作らない）。

★なぜ文ごと削るのか★
  以前、語の置換で対応したところ「150G〜が目安の目安です」のような壊れた日本語が
  生まれた。置換ではなく削除にすれば、残る文は元の原稿のままなので壊れない。

★安全策★
  - 追加も書き換えもしない（削除のみ）。差分は必ず「文が消えた」だけになる。
  - 削除後に残りが空になる場合は書き換えない（意味を失うため人が判断する）。
  - 既定は dry-run。--apply で初めて書き込む。
  - 禁止語の一覧は gates.py の ABSOLUTE_DENY を単一情報源として読む。

使い方:
    python scripts/strip_checker_claims.py            # 変更内容の確認（書き込まない）
    python scripts/strip_checker_claims.py --apply    # 書き込む
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import gates  # noqa: E402

MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
DETAILS = os.path.join(BASE, "assets", "data", "machine-details")


# ★削ったあとに残ってはいけない語★
#   禁止語そのものではないが、同じ主張（＝当サイトでは計算できない収支の断定）を
#   別の言い方で述べているもの。これが残るなら「半分だけ直った」状態になるので、
#   自動では触らず人／第二AIの判断に回す。
_RESIDUAL = re.compile(r"プラス|期待値|利益|確実|儲|回収|得する|時給|収支")

skipped: list = []


def _strip(text: str) -> str | None:
    """禁止語を含む文だけ削った結果を返す。変更不要 / 残りが空なら None。"""
    if not isinstance(text, str) or not any(d in text for d in gates.ABSOLUTE_DENY):
        return None
    sents = [x for x in re.split(r"(?<=。)", text) if x.strip()]
    kept = [s for s in sents if not any(d in s for d in gates.ABSOLUTE_DENY)]
    if len(kept) == len(sents):
        # 文末「。」が無く1文として切れない場合は手を出さない（人が判断する）
        return None
    rest = "".join(kept).strip()
    if not rest:
        return None
    if _RESIDUAL.search(rest):
        # 同じ主張が別の言い方で残っている＝文の削除では直りきらない
        skipped.append((text, rest))
        return None
    return rest


def _walk(node, path: str, edits: list) -> None:
    """checker の中の note だけを対象に歩く（数値・構造には触れない）。"""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "note" and isinstance(v, str):
                new = _strip(v)
                if new is not None and new != v:
                    edits.append((f"{path}.note", v, new, node))
            else:
                _walk(v, f"{path}.{k}", edits)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk(v, f"{path}[{i}]", edits)


# ★記事本文で、削った残りが前の文を受けている形を避ける★
#   例:「…170Gから期待値プラスに入ります。350Gまでの投資が抑えられるため…」の
#   前半を削ると、残りが受け手のない文になり読み手が迷う。誤情報ではないが
#   自動では触らず、人／第二AIが書き直す対象にする。
_BACKREF = re.compile(r"^(?:その|これ|それ|同様|また|そのため|したがって|ただし|なお|[0-9０-９])")

detail_skipped: list = []


def _strip_prose(text: str) -> str | None:
    """記事本文用。checker の note より条件を1つ厳しくする（先頭文を削って
    残りが前文を受けている形は自動で触らない）。"""
    if not isinstance(text, str) or not any(d in text for d in gates.ABSOLUTE_DENY):
        return None
    sents = [x for x in re.split(r"(?<=。)", text) if x.strip()]
    kept = [s for s in sents if not any(d in s for d in gates.ABSOLUTE_DENY)]
    if len(kept) == len(sents):
        detail_skipped.append((text, "文として切り出せない（丸ごと削除が要る）"))
        return None
    rest = "".join(kept).strip()
    if not rest:
        detail_skipped.append((text, "全部が断定（項目ごと削除が要る）"))
        return None
    if _RESIDUAL.search(rest):
        detail_skipped.append((text, f"別の言い方が残る → {rest}"))
        return None
    if sents and any(d in sents[0] for d in gates.ABSOLUTE_DENY) and _BACKREF.match(rest):
        detail_skipped.append((text, f"残りが前の文を受けている → {rest}"))
        return None
    return rest


def _walk_detail(node, path: str, edits: list, parent=None, key=None) -> None:
    """記事データの文字列を歩く（構造・数値には触れない）。"""
    if isinstance(node, str):
        new = _strip_prose(node)
        if new is not None and new != node:
            edits.append((path, node, new, (parent, key)))
    elif isinstance(node, dict):
        for k, v in node.items():
            _walk_detail(v, f"{path}.{k}", edits, node, k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk_detail(v, f"{path}[{i}]", edits, node, i)


def _drop_whole_items(data, apply: bool, slug: str) -> list:
    """★項目そのものが断定になっている箇条書きを、項目ごと外す★

    「0スルー：170G〜が期待値プラスのライン」のように、文を削ると何も残らない
    ものは、その項目を出さないのが正しい（半端に残すと意味が変わる）。

    安全策:
      - 対象は sections[].body[] の要素だけ（構造は変えない）
      - 削った結果その節の本文が空になるなら**触らない**（空セクションを作らない）
      - 追加も書き換えもしない
    """
    removed = []
    for si, sec in enumerate(data.get("sections") or []):
        body = sec.get("body")
        if not isinstance(body, list) or len(body) < 2:
            continue
        keep, drop = [], []
        for el in body:
            if isinstance(el, str) and any(d in el for d in gates.ABSOLUTE_DENY):
                # 文を削って何か残るなら、そちらは _strip_prose の担当
                sents = [x for x in re.split(r"(?<=。)", el) if x.strip()]
                rest = "".join(x for x in sents
                               if not any(d in x for d in gates.ABSOLUTE_DENY)).strip()
                if not rest:
                    drop.append(el)
                    continue
            keep.append(el)
        if drop and keep:                     # 全部消える節は触らない
            removed.extend((f"{slug} sections[{si}].body", x) for x in drop)
            if apply:
                sec["body"] = keep
    return removed


def _run_details(apply: bool) -> int:
    """記事データ（machine-details/*.json）に同じ処理を適用する。"""
    total = 0
    for fn in sorted(os.listdir(DETAILS)):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(DETAILS, fn)
        data = json.load(open(path, encoding="utf-8"))
        edits: list = []
        _walk_detail(data, "root", edits)
        whole = _drop_whole_items(data, apply, fn[:-5])
        for where, txt in whole:
            print("■ %s 項目ごと削除" % where)
            print("   %s" % txt)
        if not edits and not whole:
            continue
        if whole and apply:
            json.dump(data, open(path, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n")
        for p, old, new, _ in edits:
            print(f"■ {fn} {p}\n   前: {old}\n   後: {new}")
        total += len(edits)
        if apply:
            for _, _, new, (parent, key) in edits:
                parent[key] = new
            json.dump(data, open(path, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n")
    print("=" * 70)
    print(f"記事本文の対象 {total} 件（削除のみ）")
    if detail_skipped:
        print(f"\n▲ 自動では触らなかったもの: {len(detail_skipped)} 件"
              f"（人／第二AIが書き直す）")
        for old, why in detail_skipped:
            print(f"   - [{why}]\n     {old[:120]}")
    if apply:
        print(f"✅ {total} 件を書き込みました")
    else:
        print("（確認のみ。書き込むには --apply）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--details", action="store_true",
                    help="machines.json ではなく記事データを対象にする")
    args = ap.parse_args()

    if args.details:
        return _run_details(args.apply)

    machines = json.load(open(MACHINES, encoding="utf-8"))
    edits: list = []
    for m in machines:
        if isinstance(m.get("checker"), dict):
            _walk(m["checker"], f"{m['slug']}.checker", edits)

    print("=" * 70)
    for path, old, new, _ in edits:
        print(f"■ {path}")
        print(f"   前: {old}")
        print(f"   後: {new}")
    print("=" * 70)
    print(f"対象 {len(edits)} 件（削除のみ・数値は不変）")
    if skipped:
        print(f"\n▲ 文の削除では直りきらないため手を付けなかったもの: {len(skipped)} 件")
        print("   （同じ主張が別の言い方で残るため。人／第二AIが書き直す）")
        for old, rest in skipped:
            print(f"   - {old}")
            print(f"     削っても残る: {rest}")

    # 削除だけであることを機械的に確かめる（新しい語が入っていないこと）
    for path, old, new, _ in edits:
        if new not in old.replace("", ""):
            # 連結で作られた文字列なので部分列であることを確認する
            oi = 0
            for ch in new:
                oi = old.find(ch, oi)
                if oi < 0:
                    print(f"❌ {path}: 削除以外の変更が混ざっている")
                    return 1
                oi += 1

    if not args.apply:
        print("（確認のみ。書き込むには --apply）")
        return 0

    for _, _, new, node in edits:
        node["note"] = new
    json.dump(machines, open(MACHINES, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    with open(MACHINES, "a", encoding="utf-8") as f:
        f.write("\n")
    print(f"✅ {len(edits)} 件を書き込みました: {MACHINES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

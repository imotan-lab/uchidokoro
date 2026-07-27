#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""build_ledger.py — 分類台帳づくりの作業リストを作る（ローカル専用・読み取り専用）

gates.py は「未分類のリスク表現があれば公開しない」。その未分類を人（と第二AI）が
ALLOW / DROP に仕分けするための作業リストを、authoring データから直接組み立てる。

★位置づけ★
  - これは**ローカルの編集支援ツール**。公開経路（publish_view / audit_view）とは別物。
  - audit_view が原文を返さないのは「ビルド診断に原文を混ぜない」ため。こちらは手元で
    原稿そのものを読む作業なので原文を扱う。公開物には一切関与しない。
  - 一切ファイルを書き換えない（--out を指定したときだけ作業リストを書き出す）。

使い方:
    python scripts/build_ledger.py                    # 集計だけ表示
    python scripts/build_ledger.py --list 40          # 未分類の実例を40件表示
    python scripts/build_ledger.py --out _design/ledger_todo.json   # 作業リストを書き出す
    python scripts/build_ledger.py --ledger _design/ledger.json     # 既存台帳を読んで残りを集計
"""
from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter, defaultdict

import gates

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "assets", "data")


def provisional(m: dict) -> dict:
    """gates_dryrun と同じ暫定移行案（machines.json は書き換えない）。"""
    sim = dict(m)
    sim["lifecycle"] = "VERIFIED_PREVIEW" if m.get("status") == "preview" else "LEGACY_SEARCH"
    c = m.get("checker") or {}
    modes = {}
    for k, v in c.items():
        if isinstance(v, dict) and k not in ("modeData", "byRate"):
            modes[k] = "DISABLED" if "_disabled" in v else "UNVERIFIED"
    md = c.get("modeData")
    if isinstance(md, dict):
        for k, v in md.items():
            if isinstance(v, dict):
                modes.setdefault(k, "DISABLED" if "_disabled" in v else "UNVERIFIED")
    sim["checker_modes"] = modes
    return sim


class _Collector(gates._Ctx):
    """射影と同じ経路を通しつつ、未分類原子の**原文**を手元に集める（ローカル専用）。"""

    def __init__(self, profile, ledger, slug):
        super().__init__(profile, ledger)
        self.slug = slug
        self.items: list[dict] = []

    def atom(self, parts, path):
        text = gates.normalize_atom(parts if isinstance(parts, (list, tuple)) else [parts])
        verdict = gates.classify_atom(parts, self.ledger, self.profile)
        if verdict == gates.UNCLASSIFIED:
            self.items.append({
                "atom_id": gates.atom_id(text, self.profile),
                "slug": self.slug, "path": path, "profile": self.profile, "text": text,
            })
        return super().atom(parts, path)


# 仕分けの目安（機械的な下ごしらえ。最終判断は人／第二AI）
_SPEC_ONLY = re.compile(
    r"^(?:[^。]*?(?:純増|機械割|出玉率|コイン持ち|払い出し|枚/G|枚|％|%|G|pt|円)[^。]*)$")
_HAS_ASSERTION = re.compile(
    r"(?:勝て|勝率|得|有利|旨味|リターン|回収|儲|плюс|おすすめ|狙え|打てる|拾える|優秀)")


def triage(text: str) -> str:
    """作業を効率化するための粗い仕分け（提案であって決定ではない）。"""
    if _HAS_ASSERTION.search(text):
        return "要判断（価値・行動の示唆を含む）"
    if re.search(r"[0-9]", text) and _SPEC_ONLY.match(text):
        return "スペック寄り（ALLOW候補）"
    return "要判断"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", type=int, default=0, help="未分類の実例をN件表示")
    ap.add_argument("--out", help="作業リストの書き出し先JSON")
    ap.add_argument("--ledger", help="既存の分類台帳JSON（読み込むと残件だけ集計）")
    args = ap.parse_args()

    ledger = {}
    if args.ledger and os.path.isfile(args.ledger):
        ledger = json.load(open(args.ledger, encoding="utf-8"))

    machines = json.load(open(os.path.join(DATA, "machines.json"), encoding="utf-8"))
    all_items: list[dict] = []
    by_slug = Counter()

    for m in machines:
        sim = provisional(m)
        g = gates.compute_gates(sim)
        if not g["public"]:
            continue
        dp = os.path.join(DATA, "machine-details", f"{m['slug']}.json")
        detail = json.load(open(dp, encoding="utf-8")) if os.path.isfile(dp) else {}
        ctx = _Collector(g["profile"], ledger, m["slug"])
        gates._project_machine(sim, g, ctx)
        gates._project_detail(detail, g, ctx)
        all_items.extend(ctx.items)
        if ctx.items:
            by_slug[m["slug"]] = len(ctx.items)

    # 同一原子は1回だけ仕分ければよい
    uniq: dict[str, dict] = {}
    for it in all_items:
        u = uniq.setdefault(it["atom_id"], {**it, "count": 0, "slugs": set()})
        u["count"] += 1
        u["slugs"].add(it["slug"])

    kinds = Counter(triage(u["text"]) for u in uniq.values())
    fields = Counter(re.sub(r"\[\d+\]", "[]", u["path"]).split(".")[0] for u in uniq.values())

    print("=" * 66)
    print(f"未分類の原子: 延べ {len(all_items)} 箇所 / ユニーク {len(uniq)} 件"
          f" / 影響機種 {len(by_slug)}")
    print("-" * 66)
    print("■ 粗い仕分け（作業量の見積り用・最終判断は人/第二AI）")
    for k, v in kinds.most_common():
        print(f"   {k:<28} {v:>5} 件")
    print("-" * 66)
    print("■ どこに出ている文か")
    for k, v in fields.most_common(10):
        print(f"   {k:<20} {v:>5} 件")
    print("-" * 66)
    print("■ 未分類が多い機種 上位10")
    for s, n in by_slug.most_common(10):
        print(f"   {s:<24} {n:>4} 箇所")
    print("=" * 66)

    if args.list:
        print(f"\n■ 未分類の実例（出現回数の多い順・{args.list}件）")
        for u in sorted(uniq.values(), key=lambda x: -x["count"])[:args.list]:
            print(f"\n  [{triage(u['text'])}] {u['count']}箇所 例:{sorted(u['slugs'])[0]} {u['path']}")
            print(f"    {u['text'][:150]}")

    if args.out:
        out = [{"atom_id": u["atom_id"], "profile": u["profile"], "text": u["text"],
                "count": u["count"], "slugs": sorted(u["slugs"])[:5], "path": u["path"],
                "triage": triage(u["text"]), "verdict": None}
               for u in sorted(uniq.values(), key=lambda x: -x["count"])]
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        json.dump(out, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"\n作業リストを書き出しました: {args.out}（{len(out)}件・verdict を ALLOW/DROP で埋める）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""crosscheck_gates.py — ゲートと独立監査の突き合わせ（毎回同じコマンドで再実行できる停止ゲート）

★何を確かめるか★
  gates.py が「公開してよい」と判断した内容を、gates.py を使わない独立監査（audit_public.py）
  にかけ、食い違いがゼロであることを確かめる。共通原因故障（両者が同じ勘違いをする）の検出。

★最悪運用を想定する★
  分類台帳が未完成のため、未分類の原子を**すべて ALLOW と仮定**して射影する。
  これは「人が仕分けで全部OKにしてしまった場合」に相当し、最も危険な運用を模擬している。
  見出しが通ると配下が新たに検査対象になるため、増えなくなるまで繰り返す（不動点）。

★陰性対照★
  --negative-control を付けると、実データに危険な文を注入して
  「監査器がちゃんと鳴るか」を確認する。何も検出しないこと自体が異常でないかを確かめるため。

実行:
    python scripts/crosscheck_gates.py                 # 突き合わせ（違反があれば非0終了）
    python scripts/crosscheck_gates.py --negative-control
終了コード: 0=合格 / 1=違反あり・陰性対照失敗
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gates                     # noqa: E402
import audit_public              # noqa: E402
import build_ledger as bl        # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "assets", "data")
# 見出しが通ると配下が新たに現れるため、実データでは十数巡かかる機種がある
# （tokyo_ghoul は13巡で収束）。余裕を持たせつつ、無限ループは検出できるようにする。
MAX_ROUNDS = 40


def _all_allow_ledger(sim: dict, detail: dict, g: dict, slug: str) -> dict:
    """未分類をすべて ALLOW と仮定した台帳（不動点まで反復）。"""
    ledger: dict = {}
    for _ in range(MAX_ROUNDS):
        ctx = bl._Collector(g["profile"], ledger, slug)
        gates._project_machine(sim, g, ctx)
        gates._project_detail(detail, g, ctx)
        new = {it["atom_id"]: {"verdict": "ALLOW"} for it in ctx.items
               if it["atom_id"] not in ledger}
        if not new:
            return ledger
        ledger.update(new)
    raise RuntimeError(f"{slug}: 台帳が収束しない（見出しの依存が深すぎる）")


def run() -> int:
    machines = json.load(open(os.path.join(DATA, "machines.json"), encoding="utf-8"))
    published = blocked = 0
    problems: list[str] = []
    seen_slugs: set = set()
    expected_public = sum(1 for m in machines
                          if gates.compute_gates(bl.provisional(m))["public"])

    for m in machines:
        sim = bl.provisional(m)
        g = gates.compute_gates(sim)
        if not g["public"]:
            continue
        dp = os.path.join(DATA, "machine-details", f"{m['slug']}.json")
        detail = json.load(open(dp, encoding="utf-8")) if os.path.isfile(dp) else {}

        ledger = _all_allow_ledger(sim, detail, g, m["slug"])
        try:
            view = gates.publish_view(sim, detail, ledger)
        except gates.GateError as e:
            blocked += 1
            problems.append(f"{m['slug']}: 公開が止まった（{e}）")
            continue
        published += 1
        # slug重複も停止条件に含める（同じslugが2件あると上書き事故になる）
        problems.extend(audit_public.audit_machine(view["machine"], seen_slugs))
        # LEGACY（記事を出す状態）なのに記事が空なら、記事欠落として止める
        if view["gates"]["profile"] != "preview_basic" and not view["detail"]:
            problems.append(f"{m['slug']}: 記事を出す状態なのに公開記事が空")
        problems.extend(audit_public.audit_detail(
            m["slug"], view["detail"],
            has_disclaimer=isinstance(view["machine"].get("disclaimer"), str)
            and view["machine"]["disclaimer"] == audit_public.EXPECTED_DISCLAIMER))

    # 件数予算（黙って機種が消える事故を止める）
    if published != expected_public:
        problems.append(f"公開機種数が想定と違う: {published} != {expected_public}")

    print(f"公開できた機種: {published} / 止まった機種: {blocked}（想定 {expected_public}）")
    print(f"独立監査の違反: {len(problems)} 件")
    for p in problems[:30]:
        print("  ✗", p)
    return 1 if problems else 0


def negative_control() -> int:
    """危険な文を注入して、監査器が確実に鳴ることを確かめる。"""
    cases = [
        ("機種データの計算断定",
         lambda: audit_public.audit_machine(
             {"slug": "x", "name": "t", "strategy": "580G〜から期待収支がプラスになります",
              "disclaimer": audit_public.EXPECTED_DISCLAIMER})),
        ("記事の分割断定",
         lambda: audit_public.audit_detail(
             "x", {"sections": [{"title": "期待値が", "body": ["プラス"]}]}, True)),
        ("設定の非存在断定",
         lambda: audit_public.audit_machine(
             {"slug": "x", "name": "t", "info": "設定3は非搭載",
              "disclaimer": audit_public.EXPECTED_DISCLAIMER})),
        ("秘密つきURL",
         lambda: audit_public.audit_machine(
             {"slug": "x", "name": "t", "sources": [{"url": "https://a.example/x?token=S"}],
              "disclaimer": audit_public.EXPECTED_DISCLAIMER})),
        ("目安ラベル無しの数値",
         lambda: audit_public.audit_machine({"slug": "x", "name": "t", "limit": 999})),
    ]
    ng = []
    for name, fn in cases:
        found = fn()
        print(("✅" if found else "❌") + f" {name}: {len(found)} 件検出")
        if not found:
            ng.append(name)
    print(f"\n陰性対照 {len(cases) - len(ng)}/{len(cases)} 合格")
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--negative-control", action="store_true")
    args = ap.parse_args()
    if args.negative_control:
        return negative_control()
    return run()


if __name__ == "__main__":
    sys.exit(main())

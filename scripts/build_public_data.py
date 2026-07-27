#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""build_public_data.py — 公開物（machines.public.json ほか）を作る

★何をするか★
  authoring データ（machines.json / machine-details/*.json）を publish_view に通し、
  **公開してよいと判定されたものだけ**を公開物として書き出す。

★安全策★
  - 1機種でも構造エラー・未分類・公開できない表現があれば、その機種は公開物に入らない。
  - 書き出したあと、gates を使わない独立監査（audit_public）で必ず検査する。
    1件でも違反があれば**書き出したファイルを消して非0終了**する（中途半端な公開物を残さない）。
  - 既定は dry-run。--apply で初めて書き込む。
  - 出力先は authoring と別ディレクトリ（assets/data/public/）。

使い方:
    python scripts/build_public_data.py                 # 何が公開されるかを確認
    python scripts/build_public_data.py --apply         # 公開物を書き出す
    python scripts/build_public_data.py --verify        # 既存の公開物を検査するだけ
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import audit_public  # noqa: E402
import build_ledger  # noqa: E402
import gates  # noqa: E402

DATA = os.path.join(BASE, "assets", "data")
OUT_DIR = os.path.join(DATA, "public")
OUT_MACHINES = os.path.join(OUT_DIR, "machines.public.json")
OUT_DETAILS = os.path.join(OUT_DIR, "machine-details")
# ★台帳はバージョン管理される場所に置く★（_design は gitignore 対象で、
#   失うと「どの表現を確認済みか」が全部消える。中身は sha256 と ALLOW/DROP だけで、
#   原文は持たないので公開されても害はない）
LEDGER = os.path.join(DATA, "ledger.json")


def _load_ledger() -> dict:
    """分類台帳。未作成なら空（＝未分類が残る機種は公開されない）。"""
    if os.path.isfile(LEDGER):
        return json.load(open(LEDGER, encoding="utf-8"))
    return {}


def build() -> tuple[list, dict, list]:
    """(公開する機種の配列, slug->公開記事, 止まった理由の一覧) を返す。"""
    ledger = _load_ledger()
    machines = json.load(open(os.path.join(DATA, "machines.json"), encoding="utf-8"))
    pub_machines: list = []
    pub_details: dict = {}
    blocked: list = []

    for m in machines:
        # ★暫定移行の状態は build_ledger.provisional が単一情報源★
        sim = build_ledger.provisional(m)
        dp = os.path.join(DATA, "machine-details", f"{m['slug']}.json")
        detail = json.load(open(dp, encoding="utf-8")) if os.path.isfile(dp) else {}
        try:
            view = gates.publish_view(sim, detail, ledger)
        except gates.GateError as e:
            blocked.append({"slug": m["slug"], "reason": str(e)})
            continue
        if not view["gates"]["public"] or not view["machine"]:
            blocked.append({"slug": m["slug"], "reason": "公開ゲートが開いていない"})
            continue
        pub_machines.append(view["machine"])
        if view["detail"]:
            pub_details[m["slug"]] = view["detail"]
    return pub_machines, pub_details, blocked


def audit(pub_machines: list, pub_details: dict) -> list:
    """書き出す前後で、gates を使わない独立監査に掛ける。"""
    problems: list = []
    seen: set = set()
    for pm in pub_machines:
        problems.extend(audit_public.audit_machine(pm, seen))
        dr = pm.get("display_requirements") or {}
        problems.extend(audit_public.audit_detail(
            pm.get("slug", "?"), pub_details.get(pm.get("slug"), {}),
            has_disclaimer=(pm.get("disclaimer") == audit_public.EXPECTED_DISCLAIMER),
            surfaces=dr.get("surfaces")))
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="公開物を書き出す")
    ap.add_argument("--verify", action="store_true", help="既存の公開物を検査するだけ")
    args = ap.parse_args()

    if args.verify:
        if not os.path.isfile(OUT_MACHINES):
            print("公開物がありません:", OUT_MACHINES)
            return 1
        pm = json.load(open(OUT_MACHINES, encoding="utf-8"))
        pd_ = {}
        if os.path.isdir(OUT_DETAILS):
            for fn in os.listdir(OUT_DETAILS):
                if fn.endswith(".json"):
                    pd_[fn[:-5]] = json.load(
                        open(os.path.join(OUT_DETAILS, fn), encoding="utf-8"))
        problems = audit(pm, pd_)
        print(f"公開物: {len(pm)} 機種 / 記事 {len(pd_)} 件 / 違反 {len(problems)} 件")
        for x in problems[:10]:
            print("  ✗", x)
        return 1 if problems else 0

    pub_machines, pub_details, blocked = build()
    print("=" * 66)
    print(f"公開する機種: {len(pub_machines)} / 止まった機種: {len(blocked)}")
    print(f"公開する記事: {len(pub_details)}")
    print("-" * 66)

    problems = audit(pub_machines, pub_details)
    print(f"独立監査の違反: {len(problems)} 件")
    for x in problems[:10]:
        print("  ✗", x)
    if problems:
        print("★違反があるので公開物は作りません★")
        return 1

    if not args.apply:
        print("（確認のみ。書き出すには --apply）")
        print("=" * 66)
        return 0

    # ★書き出しは一時ディレクトリへ作ってから差し替える（中途半端な状態を作らない）★
    tmp = OUT_DIR + ".tmp"
    if os.path.isdir(tmp):
        shutil.rmtree(tmp)
    os.makedirs(os.path.join(tmp, "machine-details"), exist_ok=True)
    json.dump(pub_machines, open(os.path.join(tmp, "machines.public.json"), "w",
                                 encoding="utf-8"), ensure_ascii=False, indent=1)
    for slug, d in pub_details.items():
        json.dump(d, open(os.path.join(tmp, "machine-details", f"{slug}.json"), "w",
                          encoding="utf-8"), ensure_ascii=False, indent=1)

    # 公開物の指紋（配線後にデプロイされた物と突き合わせるため）
    h = hashlib.sha256(
        json.dumps(pub_machines, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    json.dump({"machines": len(pub_machines), "details": len(pub_details),
               "blocked": len(blocked), "hash": h},
              open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    if os.path.isdir(OUT_DIR):
        shutil.rmtree(OUT_DIR)
    os.rename(tmp, OUT_DIR)
    print(f"✅ 公開物を書き出しました: {OUT_DIR}（指紋 {h}）")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

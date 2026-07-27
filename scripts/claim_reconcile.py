#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""claim_reconcile.py — 記事・在庫・台帳の三者照合

★なぜ要るか（Codex 指摘4）★
  claim_ledger.py は台帳「単体」の検証しかしていなかった。そのため：
    - 在庫(inventory)に無い slot_id の claim も通る
    - 台帳が**空でも**通る（＝何も調べていないのに合格に見える）
    - atomic_group_id は保存されるだけで**検証されていない**
  つまり「見落とし防止」も「1つ欠けたら行ごと止める」も実現していなかった。

★三者を突き合わせる★
    いまの記事・機種データ の指紋
            ↕
    在庫（検証が要る枠の一覧）＋その入力指紋
            ↕
    台帳（claim と検証状態）

  どれかがズレたら止める。「調べていない枠が残ったまま公開」を機構で防ぐ。

使い方:
    python scripts/claim_reconcile.py --selftest
    python scripts/claim_reconcile.py --slug tokyo_ghoul
"""
from __future__ import annotations

import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import claim_inventory as ci  # noqa: E402
import claim_ledger as cl  # noqa: E402

DATA = os.path.join(BASE, "assets", "data")

# 枠が「終端した」と言える状態（＝結論が出ている）
TERMINAL_STATES = ("VERIFIED", "CONFLICT", "REVIEW", "REVIEW_MANUAL",
                   "STALE", "NOT_FOUND")


def reconcile(slug: str, machine: dict, detail: dict,
              inventory: dict, ledger: dict) -> list:
    """三者を突き合わせ、違反の一覧を返す（空なら整合）。"""
    problems: list = []

    # --- ① 在庫が今の記事から作られたものか（古い在庫で合格させない）
    now = ci.build_inventory(slug, machine, detail)
    for k in ("machine_record_sha256", "detail_json_sha256"):
        if inventory.get("input_hashes", {}).get(k) != now["input_hashes"][k]:
            problems.append(
                f"在庫が古い（{k} が今の記事と一致しない）。記事を変えたら在庫を作り直すこと")
    if inventory.get("slug") != slug:
        problems.append("在庫の機種が違う")

    # --- ② 未分類が残っていたら公開できない
    n_unc = len(inventory.get("unclassified_atoms") or [])
    if n_unc:
        problems.append(f"型に落ちていない事実が {n_unc} 件ある（全部片付くまで公開しない）")

    inv_slots = {s["slot_id"]: s for s in inventory.get("slots") or []}
    claims = ledger.get("claims") or []
    by_slot: dict = {}
    for c in claims:
        by_slot.setdefault(c.get("slot_id"), []).append(c)

    # --- ③ 台帳の claim が在庫に無い枠を指していないか
    for sid in by_slot:
        if sid not in inv_slots:
            problems.append(f"台帳に、在庫に無い枠の claim がある: {sid}")

    # --- ④ 在庫の全枠が終端しているか（★見落とし防止の本体★）
    for sid, slot in inv_slots.items():
        cs = by_slot.get(sid) or []
        if not cs:
            problems.append(
                f"調べていない枠が残っている: {slot['field_key']} ({sid})")
            continue
        if len(cs) > 1:
            problems.append(f"同じ枠に claim が複数ある: {sid}")
            continue
        st = cs[0].get("verify_state")
        if st not in TERMINAL_STATES:
            problems.append(
                f"枠の結論が出ていない: {slot['field_key']} = {st}")

    # --- ⑤ 設定1〜6などの束（atomic group）は全員そろって初めて有効
    groups: dict = {}
    for sid, slot in inv_slots.items():
        gid = slot.get("atomic_group_id")
        if gid:
            groups.setdefault(gid, []).append(sid)
    for gid, sids in groups.items():
        states = []
        for sid in sids:
            cs = by_slot.get(sid) or []
            states.append(cs[0].get("verify_state") if cs else None)
        ok = [s for s in states if s == "VERIFIED"]
        if ok and len(ok) != len(states):
            problems.append(
                f"★束の一部だけが検証済み★ {gid}: {len(ok)}/{len(states)}。"
                f"1つでも欠けたら行ごと出さない")

    # --- ⑥ 台帳そのものの検証（スキーマ・票の数え方・TTL）
    if claims:
        try:
            cl.validate_ledger(ledger, f"{slug}.ledger")
        except cl.LedgerError as e:
            problems.append(f"台帳の検証に失敗: {e}")

    return problems


def publishable(slug: str, machine: dict, detail: dict,
                inventory: dict, ledger: dict) -> tuple[bool, list]:
    """この機種を公開してよいか（三者整合＋全枠が VERIFIED）。"""
    problems = reconcile(slug, machine, detail, inventory, ledger)
    if problems:
        return False, problems
    inv_slots = inventory.get("slots") or []
    by_slot = {c.get("slot_id"): c for c in (ledger.get("claims") or [])}
    not_verified = [s["field_key"] for s in inv_slots
                    if by_slot.get(s["slot_id"], {}).get("verify_state") != "VERIFIED"]
    if not_verified:
        return False, [f"検証済みでない枠がある: {sorted(set(not_verified))}"]
    return True, []


# ---------------------------------------------------------------- selftest

def _inv(slots, unclassified=None, hashes=None):
    return {"schema_version": ci.SCHEMA_VERSION, "slug": "x",
            "input_hashes": hashes or {"machine_record_sha256": "m",
                                       "detail_json_sha256": "d"},
            "slots": slots, "unclassified_atoms": unclassified or []}


def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    m, d = {"slug": "x"}, {"factTable": [["AT間天井", "1200G+α"]]}
    real = ci.build_inventory("x", m, d)
    sid = real["slots"][0]["slot_id"]

    def claim(state="VERIFIED", slot=sid, cid=None):
        c = cl._mk_claim()
        c["slot_id"] = slot
        c["verify_state"] = state
        if cid:
            c["claim_id"] = cid
        return c

    def ledger(claims):
        """台帳スキーマとして正しい入れ物に claim を入れる。"""
        return cl._mk_ledger(claims)

    ok, why = publishable("x", m, d, real, ledger([claim()]))
    t("全枠が検証済みなら公開できる", ok)

    # ★台帳が空なら「調べていない」として止まる（以前は素通りしていた）★
    ok2, why2 = publishable("x", m, d, real, ledger([]))
    t("★台帳が空なら止まる（何も調べていないのに合格にしない）",
      not ok2 and any("調べていない枠" in w for w in why2))

    t("★在庫に無い枠の claim があれば止まる",
      any("在庫に無い枠" in w for w in
          reconcile("x", m, d, real, ledger([claim(slot="x:nazo:000000000000")]))))

    t("★結論が出ていない枠があれば止まる",
      any("結論が出ていない" in w for w in
          reconcile("x", m, d, real, ledger([claim("UNVERIFIED")]))))

    t("★在庫が古ければ止まる（記事を変えたら作り直す）",
      any("在庫が古い" in w for w in
          reconcile("x", m, {"factTable": [["AT間天井", "1300G+α"]]}, real,
                    ledger([claim()]))))

    t("★型に落ちていない事実が残っていれば止まる",
      any("型に落ちていない" in w for w in
          reconcile("x", m, d, {**real, "unclassified_atoms": [{"label": "謎"}]},
                    ledger([claim()]))))

    # ★束（設定1〜6）は全員そろって初めて有効★
    g_slots = [{"slot_id": f"x:kikaiwari.setting:{i:012d}",
                "field_key": "kikaiwari.setting",
                "atomic_group_id": "x:kikaiwari:t"} for i in range(3)]
    g_inv = _inv(g_slots, hashes=real["input_hashes"])
    part = [claim("VERIFIED", g_slots[0]["slot_id"], "x:ceiling.normal.at:001"),
            claim("REVIEW", g_slots[1]["slot_id"], "x:ceiling.normal.at:002"),
            claim("REVIEW", g_slots[2]["slot_id"], "x:ceiling.normal.at:003")]
    t("★束の一部だけ検証済みなら止まる（設定1だけ確認して出さない）",
      any("束の一部だけ" in w for w in
          reconcile("x", m, d, g_inv, ledger(part))))
    allv = [claim("VERIFIED", s["slot_id"], f"x:ceiling.normal.at:{i+1:03d}")
            for i, s in enumerate(g_slots)]
    t("　束が全員検証済みなら束の指摘は出ない",
      not any("束の一部だけ" in w for w in
              reconcile("x", m, d, g_inv, ledger(allv))))

    t("★同じ枠に claim が複数あれば止まる",
      any("複数ある" in w for w in
          reconcile("x", m, d, real, ledger([claim(), claim(cid="x:ceiling.normal.at:002")]))))

    ng = [n for n, ok_ in results if not ok_]
    print(f"\n{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--slug")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if args.slug:
        m, d = ci.load_machine(args.slug)
        if m is None:
            print("機種が見つからない:", args.slug)
            return 1
        inv = ci.build_inventory(args.slug, m, d)
        lp = os.path.join(DATA, "claim-ledgers", f"{args.slug}.json")
        led = (json.load(open(lp, encoding="utf-8")) if os.path.isfile(lp)
               else {"schema_version": cl.SCHEMA_VERSION,
                     "machine_ref": {"slug": args.slug,
                                     "machine_variant_key": args.slug,
                                     "catalog_record_sha256": "0" * 64,
                                     "identity_state": "UNVERIFIED"},
                     "claims": []})
        ok, why = publishable(args.slug, m, d, inv, led)
        print(f"公開可否: {'○' if ok else '×'}")
        for w in why[:12]:
            print("  -", w)
        if len(why) > 12:
            print(f"  … 他 {len(why) - 12} 件")
        return 0 if ok else 1
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

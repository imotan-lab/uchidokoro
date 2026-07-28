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

    # --- ① ★渡された在庫を信用しない★（Codex 指摘1）
    #   入力ハッシュだけ合わせて slots と unclassified_atoms を空にすれば
    #   「枠ゼロ＝全部済み」に見せられた。**その場で作り直したものを正とする**。
    now = ci.build_inventory(slug, machine, detail)
    if cl.canonical_sha256(inventory) != cl.canonical_sha256(now):
        problems.append(
            "在庫が今の記事から作り直したものと一致しない"
            "（記事を変えたか、在庫が書き換えられている）")
    inventory = now                        # 以降は必ず作り直した在庫で判定する
    if now.get("slug") != slug:
        problems.append("在庫の機種が違う")

    # --- ② 未分類が残っていたら公開できない
    n_unc = len(inventory.get("unclassified_atoms") or [])
    if n_unc:
        problems.append(f"型に落ちていない事実が {n_unc} 件ある（全部片付くまで公開しない）")
    # ★型が未実装の事実（設定示唆の表など）も、残っていれば公開しない★
    #   素通りさせると「未分類ゼロ」が網羅の証明にならない（Codex 指摘）
    n_uns = len(inventory.get("unsupported_facts") or [])
    if n_uns:
        problems.append(f"型が未実装の事実が {n_uns} 件ある（設定示唆の表など）")

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
        c = cs[0]
        st = c.get("verify_state")
        if st not in TERMINAL_STATES:
            problems.append(
                f"枠の結論が出ていない: {slot['field_key']} = {st}")
        # ★枠と claim が同じものを指しているか（slot_id 一致だけでは足りない）★
        if c.get("field_key") != slot["field_key"]:
            problems.append(
                f"枠と claim の項目が違う: 枠={slot['field_key']} / "
                f"claim={c.get('field_key')}")
        v, cond = c.get("value") or {}, c.get("conditions") or {}
        if v.get("kind") != slot.get("expected_value_kind"):
            problems.append(
                f"{slot['field_key']}: 値の種類が枠と違う "
                f"（枠={slot.get('expected_value_kind')} / claim={v.get('kind')}）")
        if v.get("unit") != slot.get("expected_unit"):
            problems.append(
                f"{slot['field_key']}: 単位が枠と違う "
                f"（枠={slot.get('expected_unit')} / claim={v.get('unit')}）")
        for key in ("mode", "scope"):
            if cond.get(key) != slot["conditions"].get(key):
                problems.append(
                    f"{slot['field_key']}: {key} が枠と違う "
                    f"（枠={slot['conditions'].get(key)} / claim={cond.get(key)}）")
        if c.get("atomic_group_id") != slot.get("atomic_group_id"):
            problems.append(
                f"{slot['field_key']}: 束(atomic_group)の指定が枠と違う")
        # ★★記事に載っている値と claim の値が一致するか★★（Codex 3回目 重大1）
        #   これが無いと、記事を1300Gに変えても1200Gの古い claim で通ってしまう。
        cur = slot.get("current_value")
        if cur is None:
            problems.append(
                f"{slot['field_key']}: 記事の値を1つに特定できない"
                f"（{str(slot.get('current_text'))[:40]}）")
        else:
            amt = v.get("amount")
            if not isinstance(amt, (int, float)) or isinstance(amt, bool):
                problems.append(f"{slot['field_key']}: claim に数値が無い")
            elif abs(float(amt) - cur["amount"]) > 1e-9:
                problems.append(
                    f"★記事と台帳の値が違う★ {slot['field_key']}: "
                    f"記事={cur['amount']:g} / 台帳={amt:g}"
                    f"（記事を直したら調べ直すこと）")
            if bool(v.get("plus_alpha")) != cur["plus_alpha"]:
                problems.append(
                    f"{slot['field_key']}: +α の有無が記事と台帳で違う")

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

    # --- ⑥ 台帳そのものの検証（★空でも必ず行う★・Codex 指摘1）
    try:
        # ★出典レジストリと必ず照合する（票の水増しを止める）★
        cl.validate_ledger(ledger, f"{slug}.ledger", cl.load_registry())
    except cl.LedgerError as e:
        problems.append(f"台帳の検証に失敗: {e}")
    # 台帳が別機種のものでないか
    mref = (ledger.get("machine_ref") or {})
    if mref.get("slug") != slug:
        problems.append(f"台帳の機種が違う: {mref.get('slug')} != {slug}")
    if cl.canonical_sha256(machine) != mref.get("catalog_record_sha256"):
        problems.append("台帳が参照している機種データが今のものと違う")

    return problems


def publish_gate(slug: str) -> tuple[bool, list]:
    """★本番の公開判定はこれを使う★（Codex 3回目 重大2）

    machine / detail / 在庫 / 台帳を**すべてゲート自身が信頼できる場所から読む**。
    呼び出し側から渡させると、空の記事＋空の在庫＋空の台帳という
    「形式上は正しいが中身が無い」組み合わせで公開可にできてしまう。
    """
    machine, detail = ci.load_machine(slug)
    if machine is None:
        return False, [f"機種データが無い: {slug}"]
    if not detail:
        return False, [f"記事データが無い: {slug}（空の記事を公開しない）"]
    lp = os.path.join(DATA, "claim-ledgers", f"{slug}.json")
    if not os.path.isfile(lp):
        return False, [f"台帳が無い: {slug}（何も調べていない）"]
    ledger = json.load(open(lp, encoding="utf-8"))
    inventory = ci.build_inventory(slug, machine, detail)
    if not inventory.get("slots"):
        # ★枠が1つも作れない記事を「全部済み」と誤認しない★
        return False, [f"検証すべき枠を1つも抽出できない: {slug}"
                       f"（記事の書き方が想定外か、事実が載っていない）"]
    return _publishable(slug, machine, detail, inventory, ledger)


def _publishable(slug: str, machine: dict, detail: dict,
                 inventory: dict, ledger: dict) -> tuple[bool, list]:
    """三者整合＋全枠が VERIFIED。★検査用の内部関数★

    本番の判定は publish_gate() を使うこと（入力を外から渡させない）。
    """
    problems = reconcile(slug, machine, detail, inventory, ledger)
    if problems:
        return False, problems
    # ★在庫は必ずその場で作り直したものを使う（渡された在庫を信用しない）★
    inv_slots = ci.build_inventory(slug, machine, detail).get("slots") or []
    by_slot = {c.get("slot_id"): c for c in (ledger.get("claims") or [])}

    not_verified = [s["field_key"] for s in inv_slots
                    if by_slot.get(s["slot_id"], {}).get("verify_state") != "VERIFIED"]
    if not_verified:
        return False, [f"検証済みでない枠がある: {sorted(set(not_verified))}"]

    # ★★許可リストを実際に通す★★（Codex 指摘：関数はあるが呼ばれていなかった）
    #   型が許可リストに載っていて、かつ検証済み・事実・数え方確定・期限内であること。
    not_adoptable = []
    for s_ in inv_slots:
        c = by_slot.get(s_["slot_id"])
        if not c or not cl.auto_adoptable(c):
            not_adoptable.append(s_["field_key"])
    if not_adoptable:
        return False, [
            f"自動採用の条件を満たさない枠がある: {sorted(set(not_adoptable))}"
            f"（許可リストに無い型／期限切れ／数え方未確定 など）"]
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

    # ★実データと同じ作り方で在庫を作る（在庫は必ずその場で作り直される）★
    m = {"slug": "x", "name": "テスト機"}
    d = {"factTable": [["AT間天井", "1200G+α"]]}
    inv = ci.build_inventory("x", m, d)
    slot = inv["slots"][0]

    def claim(state="VERIFIED", cid="x:ceiling.normal.at:001", **over):
        """在庫の枠に**ぴったり合う** claim を作る。"""
        c = cl._mk_claim()
        c["claim_id"] = cid
        c["slot_id"] = slot["slot_id"]
        c["field_key"] = slot["field_key"]
        c["verify_state"] = state
        c["atomic_group_id"] = slot.get("atomic_group_id")
        c["value"] = {**c["value"], "kind": slot["expected_value_kind"],
                      "unit": slot["expected_unit"],
                      "raw": "1200", "amount": 1200, "plus_alpha": True}
        c["conditions"] = {**c["conditions"], **slot["conditions"],
                           "counter_basis": "MENU_GAME"}
        c.update(over)
        return c

    def ledger(claims, machine=None):
        led = cl._mk_ledger(claims)
        led["machine_ref"]["catalog_record_sha256"] = cl.canonical_sha256(machine or m)
        return led

    ok, why = _publishable("x", m, d, inv, ledger([claim()]))
    t("枠にぴったり合う検証済みclaimがあれば公開できる", ok or print(why))

    t("★台帳が空なら止まる（何も調べていないのに合格にしない）",
      any("調べていない枠" in w for w in
          reconcile("x", m, d, inv, ledger([]))))

    # ★★在庫を空に差し替えても素通りしない（Codex 指摘1）★★
    empty = {**inv, "slots": [], "unclassified_atoms": []}
    t("★★在庫を空に書き換えても止まる（渡された在庫を信用しない）★★",
      any("作り直したもの" in w for w in reconcile("x", m, d, empty, ledger([]))))
    ok_e, _ = _publishable("x", m, d, empty, ledger([]))
    t("　その場合 publishable も False", not ok_e)

    t("★在庫が古ければ止まる（記事を変えたら作り直す）",
      any("作り直したもの" in w for w in
          reconcile("x", m, {"factTable": [["AT間天井", "1300G+α"]]}, inv,
                    ledger([claim()]))))

    t("★在庫に無い枠の claim があれば止まる",
      any("在庫に無い枠" in w for w in
          reconcile("x", m, d, inv, ledger([claim(slot_id="x:nazo:000000000000")]))))

    t("★結論が出ていない枠があれば止まる",
      any("結論が出ていない" in w for w in
          reconcile("x", m, d, inv, ledger([claim("UNVERIFIED")]))))

    # ★★枠と claim が別物なら止まる（Codex 指摘2）★★
    t("★★項目が違う claim で枠を埋められない★★",
      any("項目が違う" in w for w in
          reconcile("x", m, d, inv, ledger([claim(field_key="kikaiwari.setting")]))))
    t("★★単位が違う claim で枠を埋められない（ptでGの枠を埋めない）★★",
      any("単位が枠と違う" in w for w in
          reconcile("x", m, d, inv,
                    ledger([claim(value={**claim()["value"], "unit": "pt"})]))))
    t("★★scope が違う claim で枠を埋められない（CZ間でAT間の枠を埋めない）★★",
      any("scope が枠と違う" in w for w in
          reconcile("x", m, d, inv,
                    ledger([claim(conditions={**claim()["conditions"],
                                              "scope": "CZ_GAP"})]))))

    t("★台帳が別機種のものなら止まる",
      any("台帳の機種が違う" in w for w in
          reconcile("x", m, d, inv,
                    ledger([claim()], machine=m) | {"machine_ref": {
                        "slug": "y", "machine_variant_key": "y",
                        "catalog_record_sha256": cl.canonical_sha256(m),
                        "identity_state": "VERIFIED"}})))

    t("★台帳が参照する機種データが今のものと違えば止まる",
      any("機種データが今のものと違う" in w for w in
          reconcile("x", m, d, inv, ledger([claim()], machine={"slug": "x", "old": 1}))))

    t("★同じ枠に claim が複数あれば止まる",
      any("複数ある" in w for w in
          reconcile("x", m, d, inv,
                    ledger([claim(), claim(cid="x:ceiling.normal.at:002")]))))

    # ★束（設定1〜6）は全員そろって初めて有効★
    d2 = {"sections": [{"title": "設定示唆まとめ", "type": "settei",
                        "tables": [{"label": "ボーナス確率",
                                    "headers": ["設定", "BIG", "REG"],
                                    "rows": [["設定1", "1/259.0", "1/354.2"],
                                             ["設定6", "1/234.9", "1/234.9"]]}]}]}
    inv2 = ci.build_inventory("x", m, d2)
    gslots = [s2 for s2 in inv2["slots"] if s2.get("atomic_group_id")]
    t("設定別の表から束つきの枠ができる", len(gslots) >= 2)

    def gclaim(s2, state, i):
        c = cl._mk_claim()
        c["claim_id"] = f"x:{s2['field_key']}:{i:03d}"
        c["slot_id"] = s2["slot_id"]
        c["field_key"] = s2["field_key"]
        c["verify_state"] = state
        c["atomic_group_id"] = s2["atomic_group_id"]
        raw = s2.get("current_text") or "1/259.0"
        import re as _re
        _n = _re.findall(r"\d+(?:\.\d+)?", str(raw))
        c["value"] = {"kind": s2["expected_value_kind"], "raw": str(raw),
                      "unit": s2["expected_unit"], "operator": "EXACT",
                      "amount": float(_n[-1]) if _n else 0.0}
        c["conditions"] = {**c["conditions"], **s2["conditions"],
                           "counter_basis": "NONE"}
        return c

    part = [gclaim(s2, "VERIFIED" if k == 0 else "REVIEW", k + 1)
            for k, s2 in enumerate(gslots)]
    t("★★束の一部だけ検証済みなら止まる（設定1だけ確認して出さない）★★",
      any("束の一部だけ" in w for w in reconcile("x", m, d2, inv2, ledger(part))))

    # ★★記事を直したのに古い claim が残っていたら止まる★★（Codex 3回目 重大1）
    d_new = {"factTable": [["AT間天井", "1300G+α"]]}
    inv_new = ci.build_inventory("x", m, d_new)      # 在庫は正しく作り直した
    old_claim = claim()                              # claim は 1200G のまま
    old_claim["slot_id"] = inv_new["slots"][0]["slot_id"]
    ok_old, why_old = _publishable("x", m, d_new, inv_new, ledger([old_claim]))
    t("★★記事を1300Gに直したのに1200Gの古いclaimでは公開できない★★",
      not ok_old and any("記事と台帳の値が違う" in w for w in why_old))

    # ★★許可リストが本当に効いているか（Codex：関数はあるが呼ばれていなかった）★★
    d3 = {"factTable": [["CZ間天井", "600G+α"]]}      # CZ間は許可リストに無い型
    inv3 = ci.build_inventory("x", m, d3)
    s3 = inv3["slots"][0]

    def cz_claim(state="VERIFIED"):
        c = cl._mk_claim()
        c["claim_id"] = "x:ceiling.normal.cz:001"
        c["slot_id"] = s3["slot_id"]
        c["field_key"] = s3["field_key"]
        c["verify_state"] = state
        c["atomic_group_id"] = s3.get("atomic_group_id")
        c["value"] = {"kind": s3["expected_value_kind"], "raw": "600",
                      "amount": 600, "unit": s3["expected_unit"],
                      "operator": "MAX", "plus_alpha": True}
        c["conditions"] = {**c["conditions"], **s3["conditions"],
                           "counter_basis": "LCD_GAME"}
        return c

    ok3, why3 = _publishable("x", m, d3, inv3, ledger([cz_claim()]))
    t("★★許可リストに無い型（CZ間天井）は検証済みでも公開できない★★",
      not ok3 and any("自動採用の条件" in w for w in why3))

    # 期限切れの claim は publishable を通らない
    exp = claim()
    exp["verified_at"] = "2020-01-01T00:00:00Z"
    exp["expires_at"] = "2020-06-01T00:00:00Z"
    ok4, _ = _publishable("x", m, d, inv, ledger([exp]))
    t("★期限切れの claim では公開できない", not ok4)

    # ★★空の記事＋空の在庫＋空の台帳で公開可にならないこと★★（Codex 3回目 重大2）
    empty_inv = ci.build_inventory("x", m, {})
    ok_z, why_z = _publishable("x", m, {}, empty_inv, ledger([]))
    t("　（内部関数では空入力が通ってしまう＝これが指摘された穴）", ok_z)
    t("★★本番ゲート publish_gate は実在しない機種を通さない★★",
      not publish_gate("__not_exist__")[0])
    # 実データの機種で、台帳が無ければ止まることを確かめる
    ok_t, why_t = publish_gate("tokyo_ghoul")
    t("★★台帳が無い機種は「何も調べていない」として止まる★★",
      not ok_t and any("台帳が無い" in w for w in why_t))

    ng = [n for n, ok_ in results if not ok_]
    print("")
    print(f"{len(results) - len(ng)}/{len(results)} 合格")
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
        ok, why = publish_gate(args.slug)
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

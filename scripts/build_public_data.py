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
import claim_reconcile  # noqa: E402
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


# ★出典の裏取りゲート（claim）を公開ビルドに効かせるかどうか★
CLAIM_GATE = os.path.join(DATA, "claim-gate.json")


CLAIM_GATE_SCHEMA = "claim-gate/v1"


class GateConfigError(Exception):
    pass


def claim_gate_enabled() -> bool:
    """出典の裏取りゲートを公開判定に含めるか。既定は無効（Phase 1 移行中）。

    ★無効のあいだ、claim の仕組みは「検査コマンド」であって「停止ゲート」ではない★
      （Codex 1巡目 (a)-1）。この事実が見えないと、実装しただけで守られている
      と誤解する。build は無効時に必ず警告と影響件数を出す。

    ★★設定ファイルが欠けている・壊れている・型が違うのは「無効」ではなくエラー★★
      （Codex 2巡目 (a)-5）。例外を握りつぶして False にすると、
      ファイルを1文字壊すだけでゲートを外せてしまう。
    """
    if not os.path.isfile(CLAIM_GATE):
        raise GateConfigError(f"設定ファイルがありません: {CLAIM_GATE}")
    try:
        cfg = json.load(open(CLAIM_GATE, encoding="utf-8"))
    except Exception as e:
        raise GateConfigError(f"設定ファイルが壊れています: {CLAIM_GATE}: {e}")
    if not isinstance(cfg, dict):
        raise GateConfigError(f"設定ファイルの形式が違います: {CLAIM_GATE}")
    if cfg.get("schema_version") != CLAIM_GATE_SCHEMA:
        raise GateConfigError(
            f"schema_version が {CLAIM_GATE_SCHEMA} でない: "
            f"{cfg.get('schema_version')!r}")
    en = cfg.get("enabled")
    if not isinstance(en, bool):
        raise GateConfigError(f"enabled は true / false で書く（received={en!r}）")
    return en


def build(claim_gate: bool | None = None) -> tuple[list, dict, list]:
    """(公開する機種の配列, slug->公開記事, 止まった理由の一覧) を返す。"""
    if claim_gate is None:
        claim_gate = claim_gate_enabled()
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
        # ★出典の裏取りゲート（有効時のみ公開を止める）★
        if claim_gate:
            ok_c, why_c = claim_reconcile.publish_gate(m["slug"])
            if not ok_c:
                # ★理由は全部残す★（Codex 2巡目 (b)-1）
                blocked.append({"slug": m["slug"],
                                "reason": "出典の裏取りが済んでいない",
                                "details": why_c})
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


def selftest() -> int:
    """★停止ゲートの設定ファイルが fail-closed か★（Codex 2巡目 (a)-5）"""
    import tempfile
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    def raises(body: str | None):
        global CLAIM_GATE
        keep = CLAIM_GATE
        try:
            if body is None:
                CLAIM_GATE = os.path.join(tempfile.gettempdir(), "__no_such__.json")
            else:
                fp = os.path.join(tempfile.gettempdir(), "claim-gate-test.json")
                open(fp, "w", encoding="utf-8").write(body)
                CLAIM_GATE = fp
            try:
                claim_gate_enabled()
                return False
            except GateConfigError:
                return True
        finally:
            CLAIM_GATE = keep

    t("★設定ファイルが無ければエラー（無効扱いにしない）", raises(None))
    t("★★JSONが壊れていればエラー★★",
      raises('{"schema_version":"claim-gate/v1","enabled": fals}'))
    t("★★enabled が true/false でなければエラー★★",
      raises('{"schema_version":"claim-gate/v1","enabled":[]}'))
    t("★schema_version が違えばエラー",
      raises('{"schema_version":"x","enabled":false}'))
    t("正しい設定は読める（現在は無効）", claim_gate_enabled() is False)

    ng = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--apply", action="store_true", help="公開物を書き出す")
    ap.add_argument("--verify", action="store_true", help="既存の公開物を検査するだけ")
    ap.add_argument("--claim-gate", action="store_true",
                    help="出典の裏取りゲートを今回だけ有効にして影響を見る")
    args = ap.parse_args()
    if args.selftest:
        return selftest()

    try:
        gate_on = args.claim_gate or claim_gate_enabled()
    except GateConfigError as e:
        # ★停止ゲートの設定が読めないときは、ビルド自体を止める★
        print(f"★出典の裏取りゲートの設定が読めません: {e}")
        return 1

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
        # ★★既存の公開物にも裏取りゲートを掛け直す★★（Codex 2巡目 (a)-5）
        #   無効のまま作った公開物が、有効化後に --verify で「合格」に
        #   見えてしまうのを防ぐ。
        if gate_on:
            for x in pm:
                ok_c, why_c = claim_reconcile.publish_gate(x.get("slug"))
                if not ok_c:
                    problems.extend(
                        f"{x.get('slug')}: 出典の裏取りが済んでいない: {w}"
                        for w in why_c)
        print(f"公開物: {len(pm)} 機種 / 記事 {len(pd_)} 件 / 違反 {len(problems)} 件")
        print(f"出典の裏取りゲート: {'★有効★' if gate_on else '☆無効☆'}")
        for x in problems:
            print("  ✗", x)
        return 1 if problems else 0

    cg = gate_on
    pub_machines, pub_details, blocked = build(cg)
    print("=" * 66)
    print(f"公開する機種: {len(pub_machines)} / 止まった機種: {len(blocked)}")
    print(f"公開する記事: {len(pub_details)}")
    print("-" * 66)
    # ★★繋がっていないことを黙らせない★★（Codex 2回目 (a)-1）
    if cg:
        print("出典の裏取りゲート: ★有効★（裏取りできていない機種は公開しません）")
    else:
        print("出典の裏取りゲート: ☆無効☆ "
              "＝ claim の仕組みは検査コマンドであって、まだ公開を止めていません")
        would = [pm["slug"] for pm in pub_machines
                 if not claim_reconcile.publish_gate(pm["slug"])[0]]
        print(f"  有効にした場合に止まる機種: {len(would)} / {len(pub_machines)}")
        print(f"  有効化は {os.path.relpath(CLAIM_GATE, BASE)} の enabled を true に")
    # ★止まった理由を件数で終わらせない★（Codex 2巡目 (b)-1）
    for b in blocked:
        print(f"  ✗ {b['slug']}: {b['reason']}")
        for dline in (b.get("details") or []):
            for ln in str(dline).split("\n"):
                print(f"      {ln}")
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

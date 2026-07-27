#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""claim_ledger.py — 数値の正本（typed claim ledger）の定義と検証

★何のためにあるか★
  記事に載せる**客観的な数値**（天井・機械割・純増など）の正本。
  「どの機種の・どの状態の・何を数えた値か」まで型で持ち、
  逐語引用つきの出典と検証状態を必ず添える。

  これまでの `assets/data/facts/{slug}.json` は、Codexのレビューで
  「検証系・ビルダー・ゲートのどこからも参照されない第三の正本」と指摘された。
  逐語引用もURLも検証状態も無く、公開の根拠にできない。
  そこで facts は判断メモ（UNVERIFIED_LEGACY）へ降格し、正本をこちらに移す。

★Phase 1 の範囲（2026-07-28）★
  スキーマの定義と検証だけ。**記事生成にもビルドにも一切使わない**。
  Codexの段階表:
    Phase 1 schema/inventory のみ … 手動実行・SHADOW・run artifact保存のみ許可
                                     ledgerを記事生成に使う/machines変更/push は禁止

使い方:
    python scripts/claim_ledger.py --selftest
    python scripts/claim_ledger.py --validate assets/data/claim-ledgers/xxx.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "assets", "data")
LEDGER_DIR = os.path.join(DATA, "claim-ledgers")
INVENTORY_DIR = os.path.join(DATA, "claim-inventory")
ALLOWLIST = os.path.join(DATA, "claim-allowlist.json")
SOURCE_REGISTRY = os.path.join(DATA, "source-registry.json")

SCHEMA_VERSION = "claim-ledger/v1"

# ---------------------------------------------------------------- 語彙
# ★どれも default deny★ 未知の値は必ず止める（黙って通さない）
VALUE_KINDS = ("INTEGER", "DECIMAL", "PROBABILITY", "PERCENT", "TEXT", "BOOLEAN")
OPERATORS = ("MAX", "MIN", "EXACT", "APPROX", "RANGE")
MODES = ("NORMAL", "RESET", "ANY")
SCOPES = ("NONE", "AT_GAP", "CZ_GAP", "BONUS_GAP", "ST_GAP", "BIG_AFTER", "REG_AFTER")
# 何を数えた値か。★これが違えば同じ数字でも別物★（東京喰種の600G/1200Gの教訓）
COUNTER_BASIS = ("LCD_GAME", "MENU_GAME", "REAL_GAME", "DATA_COUNTER", "POINT",
                 "CYCLE", "THROUGH", "COIN", "NONE",
                 # ★未確定★ ラベルから推測せず、出典の逐語引用で確定させる。
                 #   UNKNOWN のままでは VERIFIED にできない（下の検証で止める）。
                 "UNKNOWN")
VERIFY_STATES = ("UNVERIFIED", "VERIFIED", "CONFLICT", "REVIEW", "REVIEW_MANUAL",
                 "STALE", "NOT_FOUND", "UNVERIFIED_LEGACY")
CLAIM_KINDS = ("FACT", "JUDGMENT")
INDEPENDENCE = ("KNOWN_INDEPENDENT", "SAME_OWNER", "LINEAGE_COPY", "UNKNOWN")
VOTE_DISPOSITION = ("COUNTED", "NOT_COUNTED_SAME_OWNER", "NOT_COUNTED_LINEAGE",
                    "NOT_COUNTED_UNKNOWN", "NOT_COUNTED_FAILED")
CHECK_IDS = ("C0", "C1", "C2", "C3", "C4", "C5")
VERDICTS = ("PASS", "FAIL", "SKIP")

_ID_RE = re.compile(r"^[a-z0-9_]+:[a-z0-9_.]+:[0-9]{3}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class LedgerError(Exception):
    """スキーマ違反。★必ず止める（黙って直さない）★"""


def canonical_sha256(obj) -> str:
    """並び順に依存しない指紋。ledger/inventory の突き合わせに使う。"""
    s = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _req(d: dict, key: str, where: str):
    if key not in d:
        raise LedgerError(f"{where}: 必須フィールド {key} が無い")
    return d[key]


def _enum(v, allowed, where: str, key: str):
    if v not in allowed:
        raise LedgerError(f"{where}.{key}: 未知の値 {v!r}（許可: {list(allowed)}）")
    return v


def _validate_source(src: dict, where: str) -> None:
    for k in ("source_id", "requested_url", "final_url", "quote", "quote_sha256",
              "fetched_at", "trust_snapshot", "verification"):
        _req(src, k, where)
    # ★逐語引用は必須★（要約は根拠にならない）
    if not isinstance(src["quote"], str) or not src["quote"].strip():
        raise LedgerError(f"{where}.quote: 逐語引用が空（要約は根拠にできない）")
    if not _SHA_RE.match(str(src["quote_sha256"])):
        raise LedgerError(f"{where}.quote_sha256: sha256の形式でない")
    if canonical_sha256(src["quote"]) != src["quote_sha256"]:
        # 引用が後から書き換えられていないことを、指紋で確かめる
        raise LedgerError(f"{where}.quote_sha256: 引用と指紋が一致しない")
    for k in ("requested_url", "final_url"):
        if not str(src[k]).startswith("https://"):
            raise LedgerError(f"{where}.{k}: https のURLでない")
    if not _TS_RE.match(str(src["fetched_at"])):
        raise LedgerError(f"{where}.fetched_at: UTCのISO形式でない")

    ts = src["trust_snapshot"]
    for k in ("publisher_id", "ownership_group_id", "content_lineage_id",
              "independence_state", "vote_key", "registry_version"):
        _req(ts, k, f"{where}.trust_snapshot")
    _enum(ts["independence_state"], INDEPENDENCE, f"{where}.trust_snapshot",
          "independence_state")

    ver = src["verification"]
    for k in ("verdict", "code", "checked_at", "verifier_version",
              "vote_disposition", "checks"):
        _req(ver, k, f"{where}.verification")
    _enum(ver["verdict"], VERDICTS, f"{where}.verification", "verdict")
    _enum(ver["vote_disposition"], VOTE_DISPOSITION, f"{where}.verification",
          "vote_disposition")
    for cid in CHECK_IDS:
        if cid not in ver["checks"]:
            raise LedgerError(f"{where}.verification.checks: {cid} が無い")
        _enum(ver["checks"][cid].get("verdict"), VERDICTS,
              f"{where}.verification.checks.{cid}", "verdict")

    # ★独立性が不明なら票に数えない★（数だけ揃えて誤情報を通さない）
    if (ts["independence_state"] == "UNKNOWN"
            and ver["vote_disposition"] == "COUNTED"):
        raise LedgerError(f"{where}: 独立性が UNKNOWN なのに票に数えている")


def validate_claim(c: dict, where: str) -> None:
    for k in ("claim_id", "slot_id", "claim_kind", "field_key", "value",
              "conditions", "verify_state", "sources"):
        _req(c, k, where)
    if not _ID_RE.match(str(c["claim_id"])):
        raise LedgerError(f"{where}.claim_id: 形式が違う（slug:field:001）")
    _enum(c["claim_kind"], CLAIM_KINDS, where, "claim_kind")
    _enum(c["verify_state"], VERIFY_STATES, where, "verify_state")

    v = c["value"]
    for k in ("kind", "raw", "unit", "operator"):
        _req(v, k, f"{where}.value")
    _enum(v["kind"], VALUE_KINDS, f"{where}.value", "kind")
    _enum(v["operator"], OPERATORS, f"{where}.value", "operator")

    cond = c["conditions"]
    for k in ("mode", "scope", "counter_basis"):
        _req(cond, k, f"{where}.conditions")
    _enum(cond["mode"], MODES, f"{where}.conditions", "mode")
    _enum(cond["scope"], SCOPES, f"{where}.conditions", "scope")
    _enum(cond["counter_basis"], COUNTER_BASIS, f"{where}.conditions", "counter_basis")

    if not isinstance(c["sources"], list):
        raise LedgerError(f"{where}.sources: 配列でない")
    for i, s in enumerate(c["sources"]):
        _validate_source(s, f"{where}.sources[{i}]")

    # ★VERIFIED を名乗るなら、票に数えた出典が2つ以上必要★
    #   （AIの自己申告では VERIFIED にできない。検証器が付ける状態）
    if c["verify_state"] == "VERIFIED":
        # ★数え方が未確定のまま VERIFIED にはできない★
        #   （同じ数字でも数えた対象が違えば別物。実データに反例あり：
        #     gundam_uc2 は AT間が液晶、sao2 は CZ間が実ゲーム数）
        if cond["counter_basis"] == "UNKNOWN":
            raise LedgerError(f"{where}: 数え方(counter_basis)が未確定のまま VERIFIED にできない")
        counted = [s for s in c["sources"]
                   if s["verification"]["vote_disposition"] == "COUNTED"]
        keys = {s["trust_snapshot"]["vote_key"] for s in counted}
        if len(keys) < 2:
            raise LedgerError(
                f"{where}: VERIFIED だが独立した票が {len(keys)} 件しかない（2件必要）")
        if not c.get("verified_at") or not _TS_RE.match(str(c["verified_at"])):
            raise LedgerError(f"{where}.verified_at: VERIFIED なのに検証日時が無い")
        if not c.get("expires_at") or not _TS_RE.match(str(c.get("expires_at", ""))):
            # ★TTLが無いと、古い記録を公開から落とせない★
            raise LedgerError(f"{where}.expires_at: VERIFIED なのに期限が無い")


def validate_ledger(led: dict, path: str = "ledger") -> list:
    """スキーマ検証。違反があれば LedgerError。戻り値は claim_id の一覧。"""
    if led.get("schema_version") != SCHEMA_VERSION:
        raise LedgerError(f"{path}: schema_version が {SCHEMA_VERSION} でない")
    mr = _req(led, "machine_ref", path)
    for k in ("slug", "machine_variant_key", "catalog_record_sha256", "identity_state"):
        _req(mr, k, f"{path}.machine_ref")
    if not _SHA_RE.match(str(mr["catalog_record_sha256"])):
        raise LedgerError(f"{path}.machine_ref.catalog_record_sha256: sha256でない")

    claims = _req(led, "claims", path)
    if not isinstance(claims, list):
        raise LedgerError(f"{path}.claims: 配列でない")

    seen_ids, seen_slots = set(), set()
    for i, c in enumerate(claims):
        w = f"{path}.claims[{i}]"
        validate_claim(c, w)
        if c["claim_id"] in seen_ids:
            raise LedgerError(f"{w}: claim_id が重複している {c['claim_id']}")
        seen_ids.add(c["claim_id"])
        # ★同じ枠に2つの値を置かない★（どちらを表示するか決まらない）
        if c["slot_id"] in seen_slots:
            raise LedgerError(f"{w}: 同じ slot_id に複数のclaimがある {c['slot_id']}")
        seen_slots.add(c["slot_id"])
        if not str(c["claim_id"]).startswith(mr["slug"] + ":"):
            raise LedgerError(f"{w}: claim_id の機種が machine_ref と違う")
    return sorted(seen_ids)


def load_allowlist() -> dict:
    """自動採用してよい claim 型。★未知キーは default deny★"""
    if not os.path.isfile(ALLOWLIST):
        return {"default_action": "DENY", "auto_adopt": []}
    return json.load(open(ALLOWLIST, encoding="utf-8"))


def auto_adoptable(claim: dict, allow: dict | None = None) -> bool:
    """この claim を自動で採用してよいか（許可リストに載っている型だけ）。"""
    allow = allow or load_allowlist()
    if allow.get("default_action") != "DENY":
        raise LedgerError("許可リストの default_action は DENY でなければならない")
    for rule in allow.get("auto_adopt", []):
        if (claim["field_key"] == rule["field_key"]
                and claim["value"]["kind"] == rule["value_kind"]
                and claim["value"]["unit"] == rule["unit"]
                and claim["value"]["operator"] in rule["operators"]
                and claim["conditions"]["mode"] in rule["modes"]
                and claim["conditions"]["scope"] in rule["scopes"]):
            return True
    return False


# ---------------------------------------------------------------- selftest

def _mk_source(quote: str, pub: str, counted: bool = True,
               independence: str = "KNOWN_INDEPENDENT") -> dict:
    return {
        "source_id": f"src-{pub}",
        "requested_url": f"https://{pub}.example/x",
        "final_url": f"https://{pub}.example/x",
        "quote": quote,
        "quote_sha256": canonical_sha256(quote),
        "fetched_at": "2026-07-28T03:10:00Z",
        "trust_snapshot": {
            "publisher_id": pub, "ownership_group_id": f"own-{pub}",
            "content_lineage_id": f"lin-{pub}", "independence_state": independence,
            "vote_key": f"publisher:{pub}", "registry_version": "source-registry/1.0.0",
        },
        "verification": {
            "verdict": "PASS", "code": "OK", "checked_at": "2026-07-28T03:15:00Z",
            "verifier_version": "consensus_verify/2.0.0",
            "vote_disposition": "COUNTED" if counted else "NOT_COUNTED_UNKNOWN",
            "checks": {c: {"verdict": "PASS", "code": "OK"} for c in CHECK_IDS},
        },
    }


def _mk_claim(**over) -> dict:
    c = {
        "claim_id": "x:ceiling.normal.at:001",
        "slot_id": "x:ceiling.normal.at:mode=NORMAL;scope=AT_GAP",
        "claim_kind": "FACT",
        "field_key": "ceiling.normal.at",
        "value": {"kind": "INTEGER", "raw": "1200", "amount": 1200,
                  "unit": "G", "operator": "MAX", "plus_alpha": True},
        "conditions": {"mode": "NORMAL", "scope": "AT_GAP",
                       "counter_basis": "MENU_GAME", "setting": None,
                       "phase": None, "through_count": None, "exchange_rate": None},
        "atomic_group_id": None,
        "verify_state": "VERIFIED",
        "verified_at": "2026-07-28T03:20:00Z",
        "expires_at": "2026-10-26T03:20:00Z",
        "sources": [_mk_source("AT間天井は最大1200G+αです。", "a"),
                    _mk_source("AT間1200G+αで天井に到達します。", "b")],
    }
    c.update(over)
    return c


def _mk_ledger(claims) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "machine_ref": {"slug": "x", "machine_variant_key": "x:2026",
                        "catalog_record_sha256": "0" * 64, "identity_state": "VERIFIED"},
        "claims": claims,
    }


def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    def raises(fn):
        try:
            fn()
        except LedgerError:
            return True
        return False

    t("正常なledgerは通る", validate_ledger(_mk_ledger([_mk_claim()])))

    # --- 逐語引用まわり
    bad = _mk_claim()
    bad["sources"][0]["quote"] = "  "
    t("★逐語引用が空なら止める（要約は根拠にできない）",
      raises(lambda: validate_ledger(_mk_ledger([bad]))))
    bad2 = _mk_claim()
    bad2["sources"][0]["quote"] = "後から書き換えた文"
    t("★引用と指紋が一致しなければ止める（後からの書き換えを検出）",
      raises(lambda: validate_ledger(_mk_ledger([bad2]))))

    # --- VERIFIED の条件
    one = _mk_claim(sources=[_mk_source("AT間天井は最大1200G+αです。", "a")])
    t("★VERIFIED なのに独立した票が1件なら止める",
      raises(lambda: validate_ledger(_mk_ledger([one]))))
    same = _mk_claim(sources=[_mk_source("q1", "a"), _mk_source("q2", "a")])
    t("★同じ発行者の2件は2票にならない",
      raises(lambda: validate_ledger(_mk_ledger([same]))))
    unk = _mk_claim()
    unk["sources"][1]["trust_snapshot"]["independence_state"] = "UNKNOWN"
    t("★独立性がUNKNOWNなのにCOUNTEDなら止める",
      raises(lambda: validate_ledger(_mk_ledger([unk]))))
    nottl = _mk_claim()
    del nottl["expires_at"]
    t("★TTLが無ければVERIFIEDにできない（古い記録を落とせなくなる）",
      raises(lambda: validate_ledger(_mk_ledger([nottl]))))

    # --- 型・語彙
    t("★未知の counter_basis は止める（数え方が決まらない）",
      raises(lambda: validate_ledger(_mk_ledger(
          [_mk_claim(conditions={**_mk_claim()["conditions"],
                                 "counter_basis": "MYSTERY"})]))))
    t("★未知の scope は止める",
      raises(lambda: validate_ledger(_mk_ledger(
          [_mk_claim(conditions={**_mk_claim()["conditions"], "scope": "NAZO"})]))))
    t("★未知の verify_state は止める",
      raises(lambda: validate_ledger(_mk_ledger([_mk_claim(verify_state="OK")]))))

    # --- 重複
    dup = [_mk_claim(), _mk_claim()]
    t("★claim_id の重複を止める", raises(lambda: validate_ledger(_mk_ledger(dup))))
    dup2 = [_mk_claim(), _mk_claim(claim_id="x:ceiling.normal.at:002")]
    t("★同じ slot に2つのclaimを置けない（どちらを表示するか決まらない）",
      raises(lambda: validate_ledger(_mk_ledger(dup2))))

    # --- 許可リスト
    allow = {"schema_version": "claim-allowlist/v1", "default_action": "DENY",
             "auto_adopt": [{"field_key": "ceiling.normal.at", "value_kind": "INTEGER",
                             "unit": "G", "operators": ["MAX"], "modes": ["NORMAL"],
                             "scopes": ["AT_GAP"]}]}
    t("許可リストに載っている型は自動採用できる", auto_adoptable(_mk_claim(), allow))
    t("★載っていない型は自動採用しない（default deny）",
      not auto_adoptable(_mk_claim(field_key="kikaiwari.setting"), allow))
    t("★unitが違えば自動採用しない（ptをGとして扱わない）",
      not auto_adoptable(
          _mk_claim(value={**_mk_claim()["value"], "unit": "pt"}), allow))
    t("★scopeが違えば自動採用しない（CZ間をAT間として扱わない）",
      not auto_adoptable(
          _mk_claim(conditions={**_mk_claim()["conditions"], "scope": "CZ_GAP"}),
          allow))
    t("★default_action が DENY でない許可リストは受け付けない",
      raises(lambda: auto_adoptable(_mk_claim(), {"default_action": "ALLOW"})))

    ng = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--validate", help="ledger JSON を検証する")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if args.validate:
        led = json.load(open(args.validate, encoding="utf-8"))
        ids = validate_ledger(led, os.path.basename(args.validate))
        print(f"✅ 検証OK: {len(ids)} claims")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

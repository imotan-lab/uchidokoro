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
IDENTITY_STATES = ("VERIFIED", "UNVERIFIED", "AMBIGUOUS")
# 証拠として受け取ってよい単位（claim_c5 と同じ定義）
EVIDENCE_UNIT_TYPES = ("TABLE_ROW", "TABLE_CELL", "LIST_ITEM", "PARAGRAPH",
                       "HEADING", "DEFINITION_ITEM")
# ★設定ごとの事実★ ここに載る項目は conditions.setting が必須
SETTING_REQUIRED_FIELDS = ("kikaiwari.setting", "koyaku.setting", "bonus.setting")
# 設定ごとに載ることも、機種共通で載ることもある項目（設定があってもよい）
SETTING_ALLOWED_FIELDS = SETTING_REQUIRED_FIELDS + (
    "prob.big", "prob.reg", "prob.grape", "prob.first_hit", "prob.bonus_total",
    "payout.big", "payout.reg", "base_game", "coin_persistence",
    "coin_unit_price", "net_increase.phase")
SETTING_VALUES = ("1", "2", "3", "4", "5", "6")
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


def _now_utc():
    """現在時刻（UTC・naive）。期限切れ判定に使う。"""
    import datetime as _dt
    return _dt.datetime.utcnow()


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


def _validate_source(src: dict, where: str, registry: dict | None = None) -> None:
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
              "vote_disposition", "checks",
              # ★★証拠は台帳の外に置き、指紋だけを持つ★★（Codex 8巡目・閉鎖条件③）
              #   台帳に証拠そのものを書かせると、claim を書く側が
              #   「別ページのURLに、対象機種の見出しと表の行」を書くだけで通る。
              "evidence_ref"):
        _req(ver, k, f"{where}.verification")
    if not _SHA_RE.match(str(ver["evidence_ref"])):
        raise LedgerError(
            f"{where}.verification.evidence_ref: 証拠の指紋(sha256)でない")
    # ★台帳に証拠の中身を書かせない★（書けてしまうと外部保管の意味が無くなる）
    for banned in ("identity_evidence", "machine_variant_key_matched"):
        if banned in ver:
            raise LedgerError(
                f"{where}.verification.{banned}: 台帳に証拠の中身を書かない"
                f"（`assets/data/claim-evidence/` に置き evidence_ref で参照する）")
    # ★引用は証拠単位の中の逐語であること★は、証拠を引ける公開ゲート側で検査する
    #   （台帳だけでは証拠本文を持たないため、ここでは形式のみを見る）
    _enum(ver["verdict"], VERDICTS, f"{where}.verification", "verdict")
    _enum(ver["vote_disposition"], VOTE_DISPOSITION, f"{where}.verification",
          "vote_disposition")
    for cid in CHECK_IDS:
        if cid not in ver["checks"]:
            raise LedgerError(f"{where}.verification.checks: {cid} が無い")
        _enum(ver["checks"][cid].get("verdict"), VERDICTS,
              f"{where}.verification.checks.{cid}", "verdict")

    # ★★票に数えてよい条件（Codex 指摘3）★★
    #   これが無いと、FAIL の出典・C5未通過・同一運営のコピー記事でも
    #   vote_key を別々に付ければ2票になり、VERIFIED を名乗れてしまった。
    if ver["vote_disposition"] == "COUNTED":
        if ver["verdict"] != "PASS":
            raise LedgerError(
                f"{where}: verdict={ver['verdict']} なのに票に数えている")
        bad = [cid for cid in CHECK_IDS
               if ver["checks"][cid].get("verdict") != "PASS"]
        if bad:
            raise LedgerError(
                f"{where}: {bad} が PASS でないのに票に数えている")
        if ts["independence_state"] != "KNOWN_INDEPENDENT":
            raise LedgerError(
                f"{where}: 独立性が {ts['independence_state']} なのに票に数えている")
        # vote_key は検証器が registry から作る。発行者IDと無関係な値を許さない
        if ts["vote_key"] != f"publisher:{ts['publisher_id']}":
            raise LedgerError(
                f"{where}: vote_key が発行者IDから作られていない"
                f"（任意の値で票を水増しできてしまう）")
        # ★★出典レジストリと照合する★★（Codex 3回目 重大4）
        #   台帳の申告ではなく、**最終URLのホスト**から発行者を引き直す。
        #   レジストリに無いホストは票に数えない（default deny）。
        if registry is not None:
            pub = resolve_publisher(src["final_url"], registry)
            if pub is None:
                raise LedgerError(
                    f"{where}: 出典レジストリに無いホスト（票に数えない）: "
                    f"{src['final_url']}")
            if pub["publisher_id"] != ts["publisher_id"]:
                raise LedgerError(
                    f"{where}: 発行者の申告がURLと一致しない"
                    f"（申告={ts['publisher_id']} / URL={pub['publisher_id']}）")
            for k in ("ownership_group_id", "content_lineage_id"):
                if pub.get(k) != ts.get(k):
                    raise LedgerError(
                        f"{where}: {k} の申告がレジストリと違う"
                        f"（申告={ts.get(k)} / 正={pub.get(k)}）")


def validate_claim(c: dict, where: str, registry: dict | None = None) -> None:
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
    # ★★raw と amount は同じ解釈から導く★★（Codex 指摘6）
    #   以前は別々に検査していたので raw="999%" / amount=100 のような
    #   食い違いが通っていた（表示と判定が別の数字になる）。
    raw = str(v.get("raw", ""))
    if v["kind"] in ("INTEGER", "DECIMAL", "PERCENT"):
        nums = re.findall(r"\d+(?:\.\d+)?", raw)
        if not nums:
            raise LedgerError(f"{where}.value.raw: {v['kind']} なのに数字が無い: {raw!r}")
        amt = v.get("amount")
        if not isinstance(amt, (int, float)) or isinstance(amt, bool):
            raise LedgerError(f"{where}.value.amount: 数値が無い（型だけ宣言している）")
        if abs(float(nums[0]) - float(amt)) > 1e-9:
            raise LedgerError(
                f"{where}: raw={raw!r} と amount={amt} が一致しない"
                f"（表示と判定が別の数字になる）")
        if v["kind"] == "PERCENT" and not (0 < float(amt) < 300):
            raise LedgerError(f"{where}.value.amount: 機械割として異常な値 {amt}")
        if v["kind"] == "INTEGER" and float(amt) != int(amt):
            raise LedgerError(f"{where}.value.amount: INTEGER なのに小数 {amt}")
    if v["kind"] == "PROBABILITY":
        if not re.match(r"^\s*1\s*/\s*\d+(?:\.\d+)?\s*$", raw):
            raise LedgerError(f"{where}.value.raw: 確率の形（1/x）でない: {raw!r}")
    # ★★+α は天井（最大N）でだけ許す★★（Codex 3巡目 (a)-3）
    #   機械割や確率に +α を付けると、「ちょうどN」の出典で
    #   「N以上」の記述を裏付けた扱いになってしまう。
    if v.get("plus_alpha"):
        if v["operator"] != "MAX" or not str(c["field_key"]).startswith("ceiling."):
            raise LedgerError(
                f"{where}.value.plus_alpha: +α は天井（MAX）でだけ使える"
                f"（field={c['field_key']} / operator={v['operator']}）")
    # ★判断（JUDGMENT）は VERIFIED にできない★（事実ではないため）
    if c.get("claim_kind") == "JUDGMENT" and c.get("verify_state") == "VERIFIED":
        raise LedgerError(f"{where}: 編集判断(JUDGMENT)を VERIFIED にはできない")
    # claim_id の field 部と field_key の食い違いを止める
    if str(c["claim_id"]).split(":")[1] != c["field_key"]:
        raise LedgerError(f"{where}.claim_id: field_key と一致しない")

    cond = c["conditions"]
    for k in ("mode", "scope", "counter_basis"):
        _req(cond, k, f"{where}.conditions")
    _enum(cond["mode"], MODES, f"{where}.conditions", "mode")
    _enum(cond["scope"], SCOPES, f"{where}.conditions", "scope")
    _enum(cond["counter_basis"], COUNTER_BASIS, f"{where}.conditions", "counter_basis")

    # ★★設定ごとの値は「どの設定か」を必ず持つ★★（Codex 3回目 手順1）
    #   機械割や小役確率は設定ごとに別の事実。設定が無い claim を許すと
    #   「設定6の値を設定1の欄に出す」型の取り違えを機械的に止められない。
    st = cond.get("setting")
    if c["field_key"] in SETTING_REQUIRED_FIELDS and st is None:
        raise LedgerError(
            f"{where}.conditions.setting: {c['field_key']} は設定ごとの値なので"
            f"設定番号が必要")
    if st is not None:
        if c["field_key"] not in SETTING_ALLOWED_FIELDS:
            raise LedgerError(
                f"{where}.conditions.setting: {c['field_key']} は設定ごとの値ではない"
                f"（設定を付けると別項目と取り違える）")
        if not isinstance(st, str):
            # 1 と "1" が混ざると突き合わせで別物になる
            raise LedgerError(f"{where}.conditions.setting: 文字列で書く（\"1\" 等）")
        if st not in SETTING_VALUES:
            raise LedgerError(
                f"{where}.conditions.setting: 設定は {SETTING_VALUES} のいずれか"
                f"（received={st!r}）")

    if not isinstance(c["sources"], list):
        raise LedgerError(f"{where}.sources: 配列でない")
    for i, s in enumerate(c["sources"]):
        _validate_source(s, f"{where}.sources[{i}]", registry)

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
        # ★同一運営・同一転載系列は1票★（vote_key を別にしても数えない）
        owners = {s["trust_snapshot"]["ownership_group_id"] for s in counted}
        lineages = {s["trust_snapshot"]["content_lineage_id"] for s in counted}
        if len(counted) > len(owners):
            raise LedgerError(f"{where}: 同じ運営元の出典を複数票に数えている")
        if len(counted) > len(lineages):
            raise LedgerError(f"{where}: 同じ転載系列の出典を複数票に数えている")
        keys = {s["trust_snapshot"]["vote_key"] for s in counted}
        if len(keys) < 2:
            raise LedgerError(
                f"{where}: VERIFIED だが独立した票が {len(keys)} 件しかない（2件必要）")
        if not c.get("verified_at") or not _TS_RE.match(str(c["verified_at"])):
            raise LedgerError(f"{where}.verified_at: VERIFIED なのに検証日時が無い")
        if not c.get("expires_at") or not _TS_RE.match(str(c.get("expires_at", ""))):
            # ★TTLが無いと、古い記録を公開から落とせない★
            raise LedgerError(f"{where}.expires_at: VERIFIED なのに期限が無い")
        # ★★期限は実日時で判定する★★（Codex 指摘6）
        #   以前は年の差だけを見ていたので 2026-01-01→2027-12-31（約2年）が通り、
        #   さらに「今すでに期限切れ」でも VERIFIED のままだった。
        import datetime as _dt
        va = _dt.datetime.strptime(c["verified_at"], "%Y-%m-%dT%H:%M:%SZ")
        ea = _dt.datetime.strptime(c["expires_at"], "%Y-%m-%dT%H:%M:%SZ")
        if ea <= va:
            raise LedgerError(f"{where}.expires_at: 検証日時より前か同じ")
        if (ea - va).days > 365:
            raise LedgerError(
                f"{where}.expires_at: 期限が長すぎる（{(ea - va).days}日・365日以内にする）")
        if ea <= _now_utc():
            raise LedgerError(
                f"{where}.expires_at: すでに期限切れ（STALE にして調べ直すこと）")


def validate_ledger(led: dict, path: str = "ledger",
                    registry: dict | None = None) -> list:
    """スキーマ検証。違反があれば LedgerError。戻り値は claim_id の一覧。"""
    if led.get("schema_version") != SCHEMA_VERSION:
        raise LedgerError(f"{path}: schema_version が {SCHEMA_VERSION} でない")
    mr = _req(led, "machine_ref", path)
    for k in ("slug", "machine_variant_key", "catalog_record_sha256", "identity_state"):
        _req(mr, k, f"{path}.machine_ref")
    if not _SHA_RE.match(str(mr["catalog_record_sha256"])):
        raise LedgerError(f"{path}.machine_ref.catalog_record_sha256: sha256でない")
    # ★★機種の型番は、その機種の slug から作る★★（Codex 2巡目 (a)-3）
    #   自由文字列を許すと「別機種の出典を、別機種だと正しく申告したまま」
    #   対象機種の claim に使えてしまう（申告どうしが一致するだけで通る）。
    vk = str(mr["machine_variant_key"])
    if not re.match(r"^[a-z0-9_]+:[A-Za-z0-9_.\-]+$", vk):
        raise LedgerError(
            f"{path}.machine_ref.machine_variant_key: 形式が違う（slug:型番）: {vk}")
    if vk.split(":")[0] != mr["slug"]:
        raise LedgerError(
            f"{path}.machine_ref.machine_variant_key: 機種が slug と違う"
            f"（{vk} / slug={mr['slug']}）")
    _enum(mr["identity_state"], IDENTITY_STATES, f"{path}.machine_ref",
          "identity_state")
    # 検証済み claim が1件でもあるなら、機種の同定も済んでいなければならない
    if (mr["identity_state"] != "VERIFIED"
            and any(c.get("verify_state") == "VERIFIED"
                    for c in (led.get("claims") or []))):
        raise LedgerError(
            f"{path}.machine_ref.identity_state: 機種の同定が済んでいないのに"
            f"検証済みの claim がある（{mr['identity_state']}）")

    claims = _req(led, "claims", path)
    if not isinstance(claims, list):
        raise LedgerError(f"{path}.claims: 配列でない")

    seen_ids, seen_slots = set(), set()
    for i, c in enumerate(claims):
        w = f"{path}.claims[{i}]"
        validate_claim(c, w, registry)
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


def load_registry() -> dict:
    """出典レジストリ。★ここに無いホストは票に数えない（default deny）★"""
    if not os.path.isfile(SOURCE_REGISTRY):
        return {"publishers": {}}
    return json.load(open(SOURCE_REGISTRY, encoding="utf-8"))


def resolve_publisher(url: str, registry: dict | None = None):
    """URL のホストから発行者を引く。★台帳の申告ではなくURLから決める★

    台帳の trust_snapshot を権威にすると、発行者IDを自由に書けるので
    票の水増しが止められない（Codex 3回目 重大4）。
    """
    registry = registry if registry is not None else load_registry()
    m = re.match(r"^https://([^/]+)/?", str(url or ""))
    if not m:
        return None
    host = m.group(1).lower()
    for pid, pub in (registry.get("publishers") or {}).items():
        if host in [h.lower() for h in pub.get("canonical_hosts", [])]:
            if pub.get("status") != "ACTIVE":
                return None
            return {"publisher_id": pid, **pub}
    return None


def load_allowlist() -> dict:
    """自動採用してよい claim 型。★未知キーは default deny★"""
    if not os.path.isfile(ALLOWLIST):
        return {"default_action": "DENY", "auto_adopt": []}
    return json.load(open(ALLOWLIST, encoding="utf-8"))


def allowlisted_type_candidate(claim: dict, allow: dict | None = None) -> bool:
    """★これは「型として許可リストに載っているか」だけを見る★

    ★自動採用の可否ではない★（Codex 指摘2）。名前を auto_adoptable にしていたため、
    「これが True なら公開してよい」と誤解する作りになっていた。実際にはこの関数は
    field/kind/unit/operator/mode/scope しか見ておらず、次を**見ていない**:
      counter_basis / verify_state / C0〜C5の結果 / claim_kind / setting / TTL /
      値が本当に数値として解釈できるか
    自動採用してよいかは `auto_adoptable()` が最終判定する。
    """
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

def auto_adoptable(claim: dict, allow: dict | None = None) -> bool:
    """★自動採用してよいかの最終判定★（型の許可リスト＋実際の検証状態）

    型が許可されているだけでは足りない。検証器が VERIFIED を付け、
    数え方が確定し、期限内で、事実(FACT)であることまで満たして初めて自動採用できる。
    """
    if not allowlisted_type_candidate(claim, allow):
        return False
    if claim.get("verify_state") != "VERIFIED":
        return False
    if claim.get("claim_kind") != "FACT":
        return False
    if claim["conditions"].get("counter_basis") == "UNKNOWN":
        return False
    # 検証済みの体裁が整っていること（validate_claim と同じ条件を通す）
    try:
        validate_claim(claim, "auto_adoptable")
    except LedgerError:
        return False
    return True


# テストで使う実在の発行者（レジストリに載っているもの）
_TEST_PUBS = {"a": ("chonborista", "chonborista.com"),
              "b": ("1geki", "1geki.jp")}


def _use_test_evidence_dir():
    """★検査は本番の証拠置き場を汚さない★（一時ディレクトリへ向ける）"""
    import tempfile
    import claim_evidence as ce
    if not getattr(ce, "_TEST_DIR", None):
        ce._TEST_DIR = tempfile.mkdtemp()
        ce.EVIDENCE_DIR = ce._TEST_DIR
    return ce


def _test_evidence_ref(host: str, quote: str, variant: str) -> str:
    """検査用：証拠置き場に1件書いてその指紋を返す（実物と同じ経路）。"""
    ce = _use_test_evidence_dir()
    return ce.write_evidence({
        "schema_version": ce.SCHEMA_VERSION,
        "fetch": {"requested_url": f"https://{host}/x",
                  "final_url": f"https://{host}/x",
                  "fetched_at": "2026-07-28T09:00:00Z", "http_status": 200,
                  "response_sha256": "a" * 64},
        "page": {"title": "スマスロテスト機 天井・機械割・設定判別",
                 "body_sha256": "b" * 64},
        "evidence_unit": {"unit_type": "TABLE_ROW",
                          "dom_path": "table[1]/tbody/tr[2]",
                          "text": "スマスロテスト機｜" + quote},
        "machine_identity": {"manufacturer_id": "test-maker",
                             "regulatory_model_code": "TEST-001",
                             "release_date": "2026-01-01"},
        "fetcher_version": "selftest/1"})


def _mk_source(quote: str, pub: str, counted: bool = True,
               independence: str = "KNOWN_INDEPENDENT",
               variant: str = "x:2026") -> dict:
    pid, host = _TEST_PUBS.get(pub, (pub, f"{pub}.example"))
    return {
        "source_id": f"src-{pid}",
        "requested_url": f"https://{host}/x",
        "final_url": f"https://{host}/x",
        "quote": quote,
        "quote_sha256": canonical_sha256(quote),
        "fetched_at": "2026-07-28T03:10:00Z",
        "trust_snapshot": {
            "publisher_id": pid, "ownership_group_id": f"own-{pid}",
            "content_lineage_id": f"lin-{pid}", "independence_state": independence,
            "vote_key": f"publisher:{pid}", "registry_version": "source-registry/1.0.0",
        },
        "verification": {
            "verdict": "PASS", "code": "OK", "checked_at": "2026-07-28T03:15:00Z",
            "verifier_version": "consensus_verify/2.0.0",
            "vote_disposition": "COUNTED" if counted else "NOT_COUNTED_UNKNOWN",
            # ★証拠は台帳の外（claim-evidence/）に置き、指紋だけを持つ★
            "evidence_ref": _test_evidence_ref(host, quote, variant),
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

    # --- ★Codex 指摘3: 票の水増し（これが最も危ない穴だった）★
    fail = _mk_claim()
    fail["sources"][0]["verification"]["verdict"] = "FAIL"
    t("★verdict=FAIL なのに票に数えたら止める",
      raises(lambda: validate_ledger(_mk_ledger([fail]))))
    c5 = _mk_claim()
    c5["sources"][0]["verification"]["checks"]["C5"]["verdict"] = "SKIP"
    t("★C5が未通過(SKIP)なのに票に数えたら止める",
      raises(lambda: validate_ledger(_mk_ledger([c5]))))
    # ★同一運営・同一転載系列は1票★（レジストリ照合を通さない検証で確かめる）
    owner = _mk_claim()
    for src in owner["sources"]:
        src["trust_snapshot"]["ownership_group_id"] = "own-same"
    t("★同じ運営元の2件を2票に数えたら止める（vote_keyを別にしても）",
      raises(lambda: validate_ledger(_mk_ledger([owner]))))
    lin = _mk_claim()
    for src in lin["sources"]:
        src["trust_snapshot"]["content_lineage_id"] = "lin-same"
    t("★同じ転載系列の2件を2票に数えたら止める",
      raises(lambda: validate_ledger(_mk_ledger([lin]))))
    t("　レジストリ照合を通す場合は、申告の食い違い自体が先に止まる",
      raises(lambda: validate_ledger(_mk_ledger([owner]), "t", load_registry())))
    vk = _mk_claim()
    vk["sources"][1]["trust_snapshot"]["vote_key"] = "publisher:偽装"
    t("★vote_key が発行者IDから作られていなければ止める",
      raises(lambda: validate_ledger(_mk_ledger([vk]))))
    same = _mk_claim()
    same["sources"][1]["trust_snapshot"]["independence_state"] = "SAME_OWNER"
    t("★独立性が SAME_OWNER なのに票に数えたら止める",
      raises(lambda: validate_ledger(_mk_ledger([same]))))

    # --- ★Codex 指摘5: 型だけ宣言して中身が伴わない値★
    t("★kind=INTEGER なのに raw='非搭載' は止める",
      raises(lambda: validate_ledger(_mk_ledger([_mk_claim(
          value={"kind": "INTEGER", "raw": "非搭載", "amount": 0,
                 "unit": "G", "operator": "MAX"})]))))
    t("★機械割として異常な値(999%)は止める",
      raises(lambda: validate_ledger(_mk_ledger([_mk_claim(
          claim_id="x:kikaiwari.setting:001", field_key="kikaiwari.setting",
          value={"kind": "PERCENT", "raw": "999%", "amount": 999,
                 "unit": "%", "operator": "EXACT"})]))))
    t("★確率なのに 1/x の形でなければ止める",
      raises(lambda: validate_ledger(_mk_ledger([_mk_claim(
          claim_id="x:prob.big:001", field_key="prob.big",
          value={"kind": "PROBABILITY", "raw": "259", "unit": "1/x",
                 "operator": "EXACT"})]))))
    t("★編集判断(JUDGMENT)は VERIFIED にできない",
      raises(lambda: validate_ledger(_mk_ledger([_mk_claim(claim_kind="JUDGMENT")]))))
    t("★claim_id の field 部と field_key の食い違いを止める",
      raises(lambda: validate_ledger(_mk_ledger([_mk_claim(field_key="prob.big")]))))
    t("★期限が検証日時より前なら止める",
      raises(lambda: validate_ledger(_mk_ledger([_mk_claim(
          expires_at="2026-07-27T03:20:00Z")]))))
    t("★★すでに期限切れなら止める（古い記録を検証済みのまま使わない）★★",
      raises(lambda: validate_ledger(_mk_ledger([_mk_claim(
          verified_at="2020-01-01T00:00:00Z", expires_at="2020-06-01T00:00:00Z")]))))
    t("★★raw と amount が食い違えば止める（表示と判定が別の数字になる）★★",
      raises(lambda: validate_ledger(_mk_ledger([_mk_claim(
          value={"kind": "INTEGER", "raw": "1200", "amount": 800,
                 "unit": "G", "operator": "MAX"})]))))
    t("★機械割 raw='999%' / amount=100 のような食い違いも止める",
      raises(lambda: validate_ledger(_mk_ledger([_mk_claim(
          claim_id="x:kikaiwari.setting:001", field_key="kikaiwari.setting",
          value={"kind": "PERCENT", "raw": "999%", "amount": 100,
                 "unit": "%", "operator": "EXACT"})]))))
    t("★確率 raw='/' のような壊れた形も止める",
      raises(lambda: validate_ledger(_mk_ledger([_mk_claim(
          claim_id="x:prob.big:001", field_key="prob.big",
          value={"kind": "PROBABILITY", "raw": "/", "unit": "1/x",
                 "operator": "EXACT"})]))))
    t("★期限が約2年（365日超）なら止める",
      raises(lambda: validate_ledger(_mk_ledger([_mk_claim(
          verified_at="2026-07-28T00:00:00Z",
          expires_at="2028-07-01T00:00:00Z")]))))
    t("★期限が長すぎる（1年超）なら止める",
      raises(lambda: validate_ledger(_mk_ledger([_mk_claim(
          expires_at="2028-07-28T03:20:00Z")]))))
    t("★数え方が未確定(UNKNOWN)のまま VERIFIED にできない",
      raises(lambda: validate_ledger(_mk_ledger([_mk_claim(
          conditions={**_mk_claim()["conditions"], "counter_basis": "UNKNOWN"})]))))

    # --- ★出典レジストリとの照合（Codex 3回目 重大4）★
    reg = load_registry()
    t("実在する発行者はURLから引ける",
      (resolve_publisher("https://chonborista.com/slot/x/1/", reg) or {})
      .get("publisher_id") == "chonborista")
    t("★★レジストリに無いホストは引けない（票に数えない）★★",
      resolve_publisher("https://nazo-site.example/x", reg) is None)
    t("★除外したサイト（スロベース）は引けない",
      resolve_publisher("https://slobase.jp/machines/x", reg) is None)

    def reg_claim(host_b=None, pub_b=None):
        """既定は実在の2社。host_b/pub_b を渡すと2つ目を差し替える。"""
        c = _mk_claim()
        if host_b:
            src = c["sources"][1]
            src["requested_url"] = src["final_url"] = f"https://{host_b}/x"
            src["trust_snapshot"].update(
                {"publisher_id": pub_b, "ownership_group_id": f"own-{pub_b}",
                 "content_lineage_id": f"lin-{pub_b}",
                 "vote_key": f"publisher:{pub_b}"})
        return c

    t("レジストリに載る2社の出典なら通る",
      validate_ledger(_mk_ledger([reg_claim()]), "t", reg))
    t("★★レジストリに無いホストの出典は止める★★",
      raises(lambda: validate_ledger(
          _mk_ledger([reg_claim(host_b="nazo.example", pub_b="nazo")]), "t", reg)))
    t("★★発行者の申告がURLと違えば止める（なりすまし）★★",
      raises(lambda: validate_ledger(
          _mk_ledger([reg_claim(host_b="1geki.jp", pub_b="chonborista")]), "t", reg)))
    fake = reg_claim()
    fake["sources"][1]["trust_snapshot"]["ownership_group_id"] = "own-nazo"
    t("★運営元の申告がレジストリと違えば止める",
      raises(lambda: validate_ledger(_mk_ledger([fake]), "t", reg)))

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
    t("許可リストに載っている型は候補になる",
      allowlisted_type_candidate(_mk_claim(), allow))
    t("★載っていない型は候補にならない（default deny）",
      not allowlisted_type_candidate(_mk_claim(field_key="kikaiwari.setting"), allow))
    t("★unitが違えば候補にならない（ptをGとして扱わない）",
      not allowlisted_type_candidate(
          _mk_claim(value={**_mk_claim()["value"], "unit": "pt"}), allow))
    t("★scopeが違えば候補にならない（CZ間をAT間として扱わない）",
      not allowlisted_type_candidate(
          _mk_claim(conditions={**_mk_claim()["conditions"], "scope": "CZ_GAP"}),
          allow))
    t("★default_action が DENY でない許可リストは受け付けない",
      raises(lambda: allowlisted_type_candidate(_mk_claim(), {"default_action": "ALLOW"})))
    # ★★型が許可されているだけでは自動採用しない★★
    t("検証済み・数え方確定・事実 なら自動採用できる", auto_adoptable(_mk_claim(), allow))
    t("★UNVERIFIED は型が合っていても自動採用しない",
      not auto_adoptable(_mk_claim(verify_state="UNVERIFIED"), allow))
    t("★REVIEW も自動採用しない", not auto_adoptable(_mk_claim(verify_state="REVIEW"), allow))
    t("★数え方が未確定なら自動採用しない",
      not auto_adoptable(_mk_claim(
          conditions={**_mk_claim()["conditions"], "counter_basis": "UNKNOWN"}), allow))
    t("★編集判断(JUDGMENT)は自動採用しない",
      not auto_adoptable(_mk_claim(claim_kind="JUDGMENT"), allow))

    # -------- ★証拠は台帳の外（Codex 8巡目・閉鎖条件③）★
    def _with_src(**over):
        c = _mk_claim()
        s = dict(c["sources"][0])
        s["verification"] = {**s["verification"], **over}
        c["sources"] = [s, c["sources"][1]]
        return c

    t("★★証拠の指紋（evidence_ref）が無い台帳は通らない★★",
      raises(lambda: validate_claim(
          _with_src(evidence_ref=None), "w")))
    t("★指紋の形でなければ通らない",
      raises(lambda: validate_claim(_with_src(evidence_ref="not-a-sha"), "w")))
    t("★★台帳に証拠の中身を書いたら止める（外部保管の意味が無くなる）★★",
      raises(lambda: validate_claim(
          _with_src(identity_evidence={"page_title": "偽"}), "w")))
    t("　旧形式の machine_variant_key_matched も書かせない",
      raises(lambda: validate_claim(
          _with_src(machine_variant_key_matched="x:1"), "w")))

    # -------- 設定ごとの値は「どの設定か」を必ず持つ（Codex 3回目 手順1）
    def _kw(**over):
        c = _mk_claim(
            claim_id="x:kikaiwari.setting:001",
            slot_id="x:kikaiwari.setting:setting=1",
            field_key="kikaiwari.setting",
            value={"kind": "PERCENT", "raw": "97.2%", "amount": 97.2,
                   "unit": "%", "operator": "EXACT"},
            conditions={"mode": "ANY", "scope": "NONE", "counter_basis": "NONE",
                        "setting": "1", "phase": None, "through_count": None,
                        "exchange_rate": None},
            sources=[_mk_source("設定1の機械割は97.2%です。", "a"),
                     _mk_source("機械割は設定1:97.2%", "b")])
        c.update(over)
        return c

    t("設定を持つ機械割 claim は通る", validate_claim(_kw(), "w") is None)
    t("★★設定が無い機械割 claim は作れない★★",
      raises(lambda: validate_claim(
          _kw(conditions={**_kw()["conditions"], "setting": None}), "w")))
    t("★設定が範囲外なら止める（設定7）",
      raises(lambda: validate_claim(
          _kw(conditions={**_kw()["conditions"], "setting": "7"}), "w")))
    t("★設定を数値で書いたら止める（\"1\" と 1 が混ざると別物になる）",
      raises(lambda: validate_claim(
          _kw(conditions={**_kw()["conditions"], "setting": 1}), "w")))
    t("★設定ごとでない項目に設定を付けたら止める（天井に設定は無い）",
      raises(lambda: validate_claim(
          _mk_claim(conditions={**_mk_claim()["conditions"], "setting": "6"}), "w")))

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
        # ★★公開ゲートと同じ条件で検証する★★（Codex (b)-4）
        #   レジストリを渡さないと、未登録ホストの台帳でも
        #   単体検証だけ「OK」と表示され、誤解を招いていた。
        ids = validate_ledger(led, os.path.basename(args.validate), load_registry())
        print(f"✅ 検証OK: {len(ids)} claims（出典レジストリ照合あり）")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

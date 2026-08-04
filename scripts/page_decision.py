# -*- coding: utf-8 -*-
"""page_decision.py — 新台経路の「判定書」（PageDecision v1）。

★なぜ要るか（2026-08-04・Codex71〜72回目の設計）★
  「先行記事／完成記事」という読者向けの二分をやめ、
  検索に載せるか・何を表示するかを**データから導出**する。
  ただし各画面が独自に欠損を判定するとかえって複雑になるため、
  判定はこのモジュール1箇所に集約し、結果（判定書）だけを
  HTML・sitemap・監査・X投稿が読む。

★対象は新台経路だけ★
  既存113件＋旧preview7件は従来の status 契約のまま（凍結）。
  区分は machine_class() が唯一の判定箇所。

★fail-closed★
  設定の欠落・破損・未知値・policyとstatusの同居は、黙って安全側に
  倒すのではなく DecisionError で止める（「破損が解析待ちの顔をして
  公開される」のを防ぐ。71回目の設計）。

正本契約: _design/page_decision_contract.md

使い方:
    python scripts/page_decision.py --selftest
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import unicodedata
from datetime import date

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import safe_json as _sj                # noqa: E402

SCHEMA = "page-decision/v1"
POLICY_SCHEMA = "indexing-policy/v1"
POLICY_PATH = os.path.join(BASE, "assets", "data", "indexing-policy.json")
POLICY_MODES = ("normal", "force_noindex_new_auto")

# ★topicの宇宙は固定★（省略された topic は pending。二値にしない設計の入口）
TOPICS = ("gameplay", "cz", "ceiling", "spec", "setting", "strategy", "reset")

# claim ID → カテゴリ（★同一claimの水増し・複数カテゴリ加算を許さない★）
_SPEC_CLAIMS = ("model_code", "payout_range", "games_per_50",
                "at_prob", "payout_rate")

# 品質ライン（契約 §5）
MIN_CLAIMS = 3
MIN_CATEGORIES = 2


class DecisionError(RuntimeError):
    pass


# ---------------------------------------------------------------- policy

def load_policy() -> dict:
    """緊急overrideを読む。★欠落・破損・未知はビルド停止★

    自動で全noindexへ倒さない（Codex71回目。倒すと「設定事故」が
    「検索からの全滅」に化ける。止まれば人が気づける）。
    """
    if not os.path.isfile(POLICY_PATH):
        raise DecisionError(f"indexing-policy が見つかりません: {POLICY_PATH}")
    got = _sj.read_json(POLICY_PATH, expect=dict)
    if got.get("schema_version") != POLICY_SCHEMA:
        raise DecisionError(
            f"indexing-policy の形が違います: {got.get('schema_version')!r}")
    mode = got.get("mode")
    if mode not in POLICY_MODES:
        raise DecisionError(f"indexing-policy の mode が不明です: {mode!r}")
    return got


# ---------------------------------------------------------------- claims

def _norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKC", str(s or "")).lower()
    return "".join(s.split())


def claims_from_material(material: dict) -> list:
    """材料から一意claim IDの一覧を作る（契約 §4）。

    ★機種名・メーカー・登場時期は数えない★（本人性に使う情報であって
    「中身の濃さ」ではない。Codex70回目）。
    ★setで重複排除★＝同じclaimを何度足しても点数は変わらない。
    """
    got = set()
    adopted = (material or {}).get("adopted") or {}
    for key in _SPEC_CLAIMS:
        if adopted.get(key):
            got.add(key)
    for c in ((material or {}).get("ceilings") or {}).get("adopted") or []:
        got.add(f"ceiling:{c.get('kind')}:{c.get('counted') or ''}")
    for c in ((material or {}).get("at_specs") or {}).get("adopted") or []:
        got.add(f"at:{c.get('mode')}")
    for c in ((material or {}).get("czs") or {}).get("adopted") or []:
        got.add(f"cz:{_norm_name(c.get('name'))}")
    return sorted(got)


def _category(claim: str) -> str:
    if claim in _SPEC_CLAIMS:
        return "spec"
    if claim.startswith("ceiling:"):
        return "ceiling"
    if claim.startswith("at:"):
        return "gameflow"
    if claim.startswith("cz:"):
        return "cz"
    raise DecisionError(f"不明なclaim IDです: {claim!r}")


def topics_from_claims(claims: list) -> tuple:
    """(confirmed, pending) を返す。宇宙は TOPICS 固定・省略=pending。"""
    confirmed = set()
    for c in claims:
        if c in ("model_code", "payout_range", "games_per_50"):
            confirmed.add("spec")
        elif c in ("at_prob", "payout_rate"):
            confirmed.add("setting")
        elif c.startswith("ceiling:"):
            confirmed.add("ceiling")
        elif c.startswith("at:"):
            confirmed.add("gameplay")
        elif c.startswith("cz:"):
            confirmed.add("cz")
    pending = [t for t in TOPICS if t not in confirmed]
    return sorted(confirmed), pending


# ---------------------------------------------------------------- decide

def decide(material: dict, policy: dict | None = None,
           decided_at: str = "") -> dict:
    """材料から判定書を作る（純関数・材料以外の外部状態は policy だけ）。

    ★並べ替え・重複追加で結果が変わらない★（claimsを正規化してから判定）。
    """
    policy = policy if policy is not None else load_policy()
    mode = policy.get("mode")
    if mode not in POLICY_MODES:
        raise DecisionError(f"policy mode が不明です: {mode!r}")
    claims = claims_from_material(material)
    confirmed, pending = topics_from_claims(claims)
    cats = sorted({_category(c) for c in claims})
    reasons = []
    if len(claims) < MIN_CLAIMS:
        reasons.append("CLAIMS_LT_3")
    if len(cats) < MIN_CATEGORIES:
        reasons.append("CATEGORIES_LT_2")
    if not any(c.startswith(("at:", "cz:")) for c in claims):
        reasons.append("NO_UNIQUE_GAMEPLAY")
    indexable = not reasons
    if mode == "force_noindex_new_auto":
        # ★通常判定には介入せず、最終段で強制するだけ★（理由も残す）
        indexable = False
        reasons = reasons + ["POLICY_FORCE_NOINDEX"]
    digest_src = json.dumps(
        {"schema": SCHEMA, "claims": claims, "mode": mode},
        ensure_ascii=False, sort_keys=True)
    return {
        "schema_version": SCHEMA,
        "indexable": indexable,
        "confirmed_topics": confirmed,
        "pending_topics": pending,
        "reason_codes": reasons,
        "claims": claims,
        "policy_mode": mode,
        "decided_at": decided_at or date.today().isoformat(),
        "input_digest": "sha256:" + hashlib.sha256(
            digest_src.encode("utf-8")).hexdigest(),
    }


# ---------------------------------------------------------------- class

def machine_class(machine: dict) -> str:
    """machines.json の1件を4区分に分ける唯一の判定箇所（契約 §1）。"""
    policy = machine.get("publication_policy")
    status = machine.get("status")
    if policy is None:
        if status in (None, "complete"):
            return "LEGACY_COMPLETE"
        if status == "preview":
            return "LEGACY_PREVIEW"
        raise DecisionError(
            f"不明な status です: {status!r} (slug={machine.get('slug')})")
    if policy != SCHEMA:
        raise DecisionError(
            f"不明な publication_policy です: {policy!r} "
            f"(slug={machine.get('slug')})")
    if status is not None:
        raise DecisionError(
            f"publication_policy と status は同居できません "
            f"(slug={machine.get('slug')})")
    pd = machine.get("page_decision")
    if not isinstance(pd, dict) or pd.get("schema_version") != SCHEMA \
            or not isinstance(pd.get("indexable"), bool):
        raise DecisionError(
            f"page_decision が欠落または壊れています "
            f"(slug={machine.get('slug')})")
    return "AUTO_INDEXABLE" if pd["indexable"] else "AUTO_PENDING"


def is_auto(machine: dict) -> bool:
    return machine.get("publication_policy") == SCHEMA


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    ok_all = True
    ran = [0]

    def t(name, cond):
        nonlocal ok_all
        ran[0] += 1
        ok_all = ok_all and bool(cond)
        print(("✅" if cond else "❌") + " " + name)

    NORMAL = {"schema_version": POLICY_SCHEMA, "mode": "normal", "reason": ""}
    FORCE = {"schema_version": POLICY_SCHEMA,
             "mode": "force_noindex_new_auto", "reason": "試験"}
    # 材料: claim3件・カテゴリ2種・ゲーム性あり = 合格ライン丁度
    MAT_OK = {"adopted": {"model_code": {"value": "L試験A1"},
                          "payout_range": {"value": {"low": 97, "high": 110}}},
              "at_specs": {"adopted": [{"mode": "MAIN_AT",
                                        "games": 30, "net": 2.8}]}}
    d = decide(MAT_OK, NORMAL, "2026-08-04")
    t("★claim3件・2カテゴリ・固有ゲーム性あり → indexable★",
      d["indexable"] and d["reason_codes"] == []
      and d["claims"] == ["at:MAIN_AT", "model_code", "payout_range"])
    t("　topicsが導出される（spec+gameplay確定・残りpending）",
      d["confirmed_topics"] == ["gameplay", "spec"]
      and "ceiling" in d["pending_topics"]
      and "strategy" in d["pending_topics"])
    # claimを1件削る → 不合格＋理由コード
    MAT_2 = {"adopted": {"model_code": {"value": "L試験A1"}},
             "at_specs": {"adopted": [{"mode": "MAIN_AT",
                                       "games": 30, "net": 2.8}]}}
    d2 = decide(MAT_2, NORMAL, "2026-08-04")
    t("★claim1件減 → indexable=false＋理由コード★",
      not d2["indexable"] and "CLAIMS_LT_3" in d2["reason_codes"])
    # ゲーム性なし（spec3件だけ）→ 不合格
    MAT_SPEC = {"adopted": {"model_code": {"value": "x"},
                            "payout_range": {"value": 1},
                            "games_per_50": {"value": 1}}}
    d3 = decide(MAT_SPEC, NORMAL, "2026-08-04")
    t("★spec3件だけ（カテゴリ1種・ゲーム性なし）→ 不合格★",
      not d3["indexable"] and "CATEGORIES_LT_2" in d3["reason_codes"]
      and "NO_UNIQUE_GAMEPLAY" in d3["reason_codes"])
    # 並べ替え・重複で不変
    MAT_DUP = {"adopted": dict(MAT_OK["adopted"]),
               "at_specs": {"adopted": [
                   {"mode": "MAIN_AT", "games": 30, "net": 2.8},
                   {"mode": "MAIN_AT", "games": 30, "net": 2.8}]}}
    d4 = decide(MAT_DUP, NORMAL, "2026-08-04")
    t("★同一claimの重複追加で点数・digestが変わらない★",
      d4["claims"] == d["claims"] and d4["input_digest"] == d["input_digest"])
    # 緊急override
    d5 = decide(MAT_OK, FORCE, "2026-08-04")
    t("★override（force_noindex_new_auto）→ 品質合格でも indexable=false★",
      not d5["indexable"] and "POLICY_FORCE_NOINDEX" in d5["reason_codes"])
    # 壊れたpolicy
    try:
        decide(MAT_OK, {"schema_version": POLICY_SCHEMA, "mode": "zzz"})
        t("★不明なpolicy modeは止まる★", False)
    except DecisionError:
        t("★不明なpolicy modeは止まる★", True)
    # machine_class の行列
    m_auto = {"slug": "a", "publication_policy": SCHEMA, "page_decision": d}
    m_pend = {"slug": "b", "publication_policy": SCHEMA, "page_decision": d2}
    t("★machine_class: AUTO_INDEXABLE / AUTO_PENDING★",
      machine_class(m_auto) == "AUTO_INDEXABLE"
      and machine_class(m_pend) == "AUTO_PENDING")
    t("　LEGACY_COMPLETE（status無し）/ LEGACY_PREVIEW",
      machine_class({"slug": "c"}) == "LEGACY_COMPLETE"
      and machine_class({"slug": "d", "status": "preview"})
      == "LEGACY_PREVIEW")
    for bad, label in (
            ({"slug": "e", "publication_policy": SCHEMA,
              "status": "preview", "page_decision": d},
             "★policyとstatusの同居は止まる★"),
            ({"slug": "f", "publication_policy": "other/v9",
              "page_decision": d}, "★未知のpolicyは止まる★"),
            ({"slug": "g", "publication_policy": SCHEMA},
             "★page_decision欠落は止まる★"),
            ({"slug": "h", "status": "zzz"}, "★未知のstatusは止まる★")):
        try:
            machine_class(bad)
            t(label, False)
        except DecisionError:
            t(label, True)
    # 実ファイルのpolicyが読める（形式検査）
    try:
        p = load_policy()
        t("★実物の indexing-policy.json が読める（mode=normal想定）★",
          p.get("mode") in POLICY_MODES)
    except DecisionError as e:
        t(f"★実物の indexing-policy.json が読める★（{e}）", False)
    print(f"{ran[0]}/{ran[0]} 合格" if ok_all else "不合格あり")
    return 0 if ok_all else 1


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="新台経路の判定書")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    sys.exit(selftest() if a.selftest else 0)

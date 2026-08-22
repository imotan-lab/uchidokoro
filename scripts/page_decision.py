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
import re
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
_SPEC_CLAIMS = ("payout_range", "games_per_50", "at_prob", "payout_rate")

# ★もう新しくは作らない claim★（2026-08-23・台帳#461）
#   ★型式名を外した理由★＝**記事には書かない決まり**（決定事項表／監査47が
#   記事データと公開HTMLの両方から消す）。読者が一度も見ない値で
#   「検索に載せてよい濃さ」（MIN_CLAIMS=3）を測っていた。
#   ★機種名・メーカー・登場時期を数えないのとまったく同じ理由★
#   （下の説明文＝Codex70回目。型式名も「本人性に使う情報」で、
#     置き場も identity.regulatory_model_code）。
#   ★消さずに残す理由★＝すでに model_code を claim に持つ機種が6件ある。
#   `_category()` は知らない claim を例外で弾くので、**消すとその6件が
#   読めなくなって止まる**（fail-closed が裏目に出る）。
#   ★実測（2026-08-23）★＝判定書つき10機種は全部すでに indexable=False。
#   外して検索から落ちるページは**0件**。
RETIRED_CLAIMS = ("model_code",)

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


# ★claim IDに使ってよい値★（2026-08-04・Codex74回目の指摘3。
#   接頭辞だけ見ていたので `at:`（モード空）や `ceiling:None:` が
#   「固有ゲーム性1件」として数えられ、中身の無い機種が index できた）
AT_MODES = ("MAIN_AT", "UPPER_AT")
CEILING_KINDS = ("GAME", "CYCLE", "POINT")
_CZ_NAME_OK = re.compile(r"^[^\s]{1,60}$")


def _bad_value(v) -> bool:
    """空・None・文字列の 'None'/'none' を値として認めない。"""
    s = "" if v is None else str(v).strip()
    return (not s) or s.lower() in ("none", "null", "nan", "-")


def _from_2ai(v) -> bool:
    """2AIが確定させた値か（機械の裏取りをまだ通っていない）。"""
    return isinstance(v, dict) and v.get("_from") == "confirmed_values"


def _claims(material: dict, *, count_confirmed: bool) -> list:
    """材料から一意claim IDの一覧を作る（契約 §4）。

    ★機種名・メーカー・登場時期は数えない★（本人性に使う情報であって
    「中身の濃さ」ではない。Codex70回目）。★型式名も同じ★＝RETIRED_CLAIMS。
    ★setで重複排除★＝同じclaimを何度足しても点数は変わらない。
    ★欠けた値からclaimを作らない★（Codex74回目。作れば「中身がある」と
      数えてしまう。材料が壊れているなら止める＝fail-closed）

    ★★2つの意味を分けた（2026-08-23・台帳#461）★★
      count_confirmed=False … 「検索に載せてよい濃さ」（今までどおり）
      count_confirmed=True  … 「今夜そのことを知っているか」（消失の判定用）
      ★分けた理由★＝同じ一覧を `grow_machine.claims_grew` が
      「事実が消えたか」の判定に使っていた。濃さの一覧は2AIの確定値を
      **意図的に外す**ので、★2AIで確定させるほど「消えた」と判定された★
      （実測: 喰霊-零-Re は6件確定させた晩に「3件消えた」で止まった）。
      ★「知っていること全部」ではない★（Codexの指摘）＝
      gameplay・reset などは同じ体系のIDを持たない。**回帰検査用の射影**。
    """
    got = set()
    adopted = (material or {}).get("adopted") or {}
    for key in _SPEC_CLAIMS:
        v = adopted.get(key)
        # ★2AIが確定した値は「検索に載せてよい濃さ」に数えない★
        #   （2026-08-09・依頼130 P1-3）
        #   記事に載せる材料としては使うが、claim の裏取り（verify_claims）を
        #   まだ通っていない。数えると、機械が確かめていない値で
        #   検索に載る判定が出てしまう。★載せるのは裏取り後★
        if v and (count_confirmed or not _from_2ai(v)):
            got.add(key)
    for c in ((material or {}).get("ceilings") or {}).get("adopted") or []:
        if _from_2ai(c) and not count_confirmed:
            continue
        kind = (c or {}).get("kind")
        if kind not in CEILING_KINDS:
            raise DecisionError(f"天井の種類が不明です: {kind!r}")
        if _bad_value(c.get("amount")):
            raise DecisionError(f"天井の値がありません: {c!r}")
        counted = "" if _bad_value(c.get("counted")) else str(c["counted"]).strip()
        got.add(f"ceiling:{kind}:{counted}")
    for c in ((material or {}).get("at_specs") or {}).get("adopted") or []:
        if _from_2ai(c) and not count_confirmed:
            continue    # ★裏取り前の値は濃さに数えない★（依頼130 P1-3）
        mode = (c or {}).get("mode")
        if mode not in AT_MODES:
            raise DecisionError(f"ATのモードが不明です: {mode!r}")
        # ★どれか1つでも中身があればclaimにする★（2026-08-09）
        #   以前は「1セットG数」と「純増」の両方を必須にしていたが、
        #   継続率しか公表されていない機種が実在する（パリピ孔明）。
        #   両方必須だと、確かに分かっている継続率まで捨てることになる。
        if all(_bad_value(c.get(k)) for k in ("games", "net", "loop_rate")):
            raise DecisionError(f"ATの値がありません: {c!r}")
        got.add(f"at:{mode}")
    for c in ((material or {}).get("czs") or {}).get("adopted") or []:
        if _from_2ai(c) and not count_confirmed:
            continue    # ★裏取り前の値は濃さに数えない★（依頼130 P1-3）
        nm = _norm_name((c or {}).get("name"))
        if _bad_value(nm):
            raise DecisionError(f"CZの名前がありません: {c!r}")
        got.add(f"cz:{nm}")
    return sorted(got)


def index_claims_from_material(material: dict) -> list:
    """★検索に載せてよい濃さ★（品質ライン MIN_CLAIMS/MIN_CATEGORIES 用）。

    2AIが確定させただけの値は数えない（機械の裏取り前）。
    """
    return _claims(material, count_confirmed=False)


def regression_claims_from_material(material: dict) -> list:
    """★今夜そのことを知っているか★（消失の判定＝回帰検査だけに使う）。

    ★公開の判定には使わない★＝ここに入っても検索には載らない。
    ★「知っていること全部」ではない★（Codexの指摘）＝
      gameplay・reset・checker_ceiling などは同じ体系のIDを持たない。
      あくまで claim 体系で表せる範囲の**射影**。
    """
    return _claims(material, count_confirmed=True)


def claims_from_material(material: dict) -> list:
    """★旧名★＝「検索に載せてよい濃さ」。呼び出し側を壊さないために残す。"""
    return index_claims_from_material(material)


def _category(claim: str) -> str:
    """claim ID の**形まで**確かめてカテゴリを返す（不正は例外）。"""
    # ★もう作らないものも「読める」ままにする★（2026-08-23・台帳#461）
    #   保存済みの判定書に model_code を持つ機種が6件ある。
    if claim in _SPEC_CLAIMS or claim in RETIRED_CLAIMS:
        return "spec"
    if claim.startswith("ceiling:"):
        parts = claim.split(":")
        if len(parts) != 3 or parts[1] not in CEILING_KINDS \
                or (parts[2] and _bad_value(parts[2])):
            raise DecisionError(f"天井のclaim IDが不正です: {claim!r}")
        return "ceiling"
    if claim.startswith("at:"):
        if claim[3:] not in AT_MODES:
            raise DecisionError(f"ATのclaim IDが不正です: {claim!r}")
        return "gameflow"
    if claim.startswith("cz:"):
        nm = claim[3:]
        if _bad_value(nm) or not _CZ_NAME_OK.match(nm):
            raise DecisionError(f"CZのclaim IDが不正です: {claim!r}")
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

REASON_CODES = ("CLAIMS_LT_3", "CATEGORIES_LT_2", "NO_UNIQUE_GAMEPLAY",
                "POLICY_FORCE_NOINDEX")
_DECISION_KEYS = {"schema_version", "indexable", "confirmed_topics",
                  "pending_topics", "reason_codes", "claims", "policy_mode",
                  "decided_at", "input_digest"}


def decide_from_claims(claims: list, mode: str, decided_at: str = "") -> dict:
    """claim一覧と policy mode から判定書を組み立てる（唯一の計算箇所）。

    ★並べ替え・重複追加で結果が変わらない★（claimsを正規化してから判定）。
    """
    if mode not in POLICY_MODES:
        raise DecisionError(f"policy mode が不明です: {mode!r}")
    claims = sorted(set(claims))
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


def decide(material: dict, policy: dict | None = None,
           decided_at: str = "") -> dict:
    """材料から判定書を作る（純関数・材料以外の外部状態は policy だけ）。"""
    policy = policy if policy is not None else load_policy()
    return decide_from_claims(claims_from_material(material),
                              policy.get("mode"), decided_at)


def validate_decision(pd: dict) -> None:
    """★保存された判定書を、claims から計算し直して丸ごと突き合わせる★

    （2026-08-04・Codex73回目の指摘3。以前は「辞書・schema一致・indexableがbool」
    しか見ておらず、**claims も理由も無い判定書で index できた**。
    台帳を信用せず毎回計算し直す、という当サイトの原則にも反していた）
    合わないものは例外＝fail-closed（黙って安全側に倒さない）。
    """
    if not isinstance(pd, dict):
        raise DecisionError("判定書が辞書ではありません")
    missing = sorted(_DECISION_KEYS - set(pd))
    extra = sorted(set(pd) - _DECISION_KEYS)
    if missing or extra:
        raise DecisionError(f"判定書の項目が違います（欠け={missing} 余分={extra}）")
    if pd["schema_version"] != SCHEMA:
        raise DecisionError(f"判定書の schema が違います: {pd['schema_version']!r}")
    if not isinstance(pd["indexable"], bool):
        raise DecisionError("判定書の indexable が真偽値ではありません")
    if not isinstance(pd["claims"], list) \
            or not all(isinstance(c, str) and c for c in pd["claims"]):
        raise DecisionError("判定書の claims が文字列の配列ではありません")
    if not isinstance(pd["decided_at"], str):
        raise DecisionError("判定書の decided_at が文字ではありません")
    try:
        date.fromisoformat(pd["decided_at"])   # ★実在する日か★（Codex74回目）
    except ValueError:
        raise DecisionError(f"判定書の decided_at が実在する日付ではありません: "
                            f"{pd['decided_at']!r}")
    for c in pd["claims"]:
        _category(c)                       # 不明なclaim IDはここで例外
    want = decide_from_claims(pd["claims"], pd["policy_mode"], pd["decided_at"])
    for k in sorted(_DECISION_KEYS):
        if pd[k] != want[k]:
            raise DecisionError(
                f"判定書の {k} が claims から計算し直した値と違います "
                f"（保存={pd[k]!r} / 計算={want[k]!r}）")


# ---------------------------------------------------------------- class

def machine_class(machine: dict, policy: dict | None = None) -> str:
    """machines.json の1件を4区分に分ける唯一の判定箇所（契約 §1）。

    ★いまの緊急overrideを毎回かける★（2026-08-04・Codex73回目の指摘1。
    以前は公開時に焼いた indexable をそのまま信じていたので、
    **公開済みの機種にスイッチが効かなかった**）。
    """
    policy = policy if policy is not None else load_policy()
    pol_mode = policy.get("mode")
    if pol_mode not in POLICY_MODES:
        raise DecisionError(f"policy mode が不明です: {pol_mode!r}")
    pub = machine.get("publication_policy")
    status = machine.get("status")
    if pub is None:
        if status in (None, "complete"):
            return "LEGACY_COMPLETE"
        if status == "preview":
            return "LEGACY_PREVIEW"
        raise DecisionError(
            f"不明な status です: {status!r} (slug={machine.get('slug')})")
    if pub != SCHEMA:
        raise DecisionError(
            f"不明な publication_policy です: {pub!r} "
            f"(slug={machine.get('slug')})")
    if status is not None:
        raise DecisionError(
            f"publication_policy と status は同居できません "
            f"(slug={machine.get('slug')})")
    try:
        validate_decision(machine.get("page_decision"))
    except DecisionError as e:
        raise DecisionError(f"{machine.get('slug')}: {e}")
    pd = machine["page_decision"]
    # ★保存値ではなく「いまのpolicyで計算し直した結果」を使う★
    now = decide_from_claims(pd["claims"], pol_mode, pd["decided_at"])
    return "AUTO_INDEXABLE" if now["indexable"] else "AUTO_PENDING"


def stale_decisions(machines: list, policy: dict | None = None) -> list:
    """保存された判定書と、いまのpolicyでの判定が食い違う機種を返す。

    緊急overrideを切り替えた直後は、ページ・sitemap が古い判定のまま。
    ★監査がこれを検知し、`apply_indexing_policy.py` で成果物をそろえる★
    """
    policy = policy if policy is not None else load_policy()
    out = []
    for m in machines:
        if not is_auto(m):
            continue
        pd = m.get("page_decision") or {}
        try:
            validate_decision(pd)
        except DecisionError:
            out.append(m.get("slug"))
            continue
        if pd["policy_mode"] != policy["mode"]:
            out.append(m.get("slug"))
    return out


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

    def _raises(fn):
        try:
            fn()
            return False
        except DecisionError:
            return True

    NORMAL = {"schema_version": POLICY_SCHEMA, "mode": "normal", "reason": ""}
    FORCE = {"schema_version": POLICY_SCHEMA,
             "mode": "force_noindex_new_auto", "reason": "試験"}
    # 材料: claim3件・カテゴリ2種・ゲーム性あり = 合格ライン丁度
    MAT_OK = {"adopted": {"games_per_50": {"value": {"games": 36.1}},
                          "payout_range": {"value": {"low": 97, "high": 110}}},
              "at_specs": {"adopted": [{"mode": "MAIN_AT",
                                        "games": 30, "net": 2.8}]}}
    d = decide(MAT_OK, NORMAL, "2026-08-04")
    t("★claim3件・2カテゴリ・固有ゲーム性あり → indexable★",
      d["indexable"] and d["reason_codes"] == []
      and d["claims"] == ["at:MAIN_AT", "games_per_50", "payout_range"])
    t("　topicsが導出される（spec+gameplay確定・残りpending）",
      d["confirmed_topics"] == ["gameplay", "spec"]
      and "ceiling" in d["pending_topics"]
      and "strategy" in d["pending_topics"])
    # claimを1件削る → 不合格＋理由コード
    MAT_2 = {"adopted": {"payout_range": {"value": {"low": 97, "high": 110}}},
             "at_specs": {"adopted": [{"mode": "MAIN_AT",
                                       "games": 30, "net": 2.8}]}}
    d2 = decide(MAT_2, NORMAL, "2026-08-04")
    t("★claim1件減 → indexable=false＋理由コード★",
      not d2["indexable"] and "CLAIMS_LT_3" in d2["reason_codes"])
    # ゲーム性なし（spec3件だけ）→ 不合格
    MAT_SPEC = {"adopted": {"at_prob": {"value": 1},
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
    # ★判定書の丸ごと検証★（Codex73回目の指摘3）
    t("★★claims だけの判定書は通さない（項目の欠けを検知）★★",
      _raises(lambda: validate_decision(
          {"schema_version": SCHEMA, "indexable": True})))
    t("★★中身の無い判定書で index できない★★"
      "（claims無しの indexable=true が通っていた）",
      _raises(lambda: machine_class(
          {"slug": "z", "publication_policy": SCHEMA,
           "page_decision": {"schema_version": SCHEMA, "indexable": True}})))
    t("★★indexable を手で書き換えたら止まる（claimsから計算し直す）★★",
      _raises(lambda: validate_decision({**d, "indexable": False})))
    t("★★理由コードを消したら止まる★★",
      _raises(lambda: validate_decision({**d2, "reason_codes": []})))
    t("★★claims を足して digest を直さなければ止まる★★",
      _raises(lambda: validate_decision({**d, "claims": d["claims"] + ["cz:x"]})))
    t("　余分な項目があれば止まる",
      _raises(lambda: validate_decision({**d, "extra": 1})))
    t("　正しい判定書は通る", validate_decision(d) is None)
    # ★中身の無いclaim IDで index できない★（Codex74回目の指摘3）
    t("★★空のATモード（at:）は固有ゲーム性として数えない★★",
      _raises(lambda: decide_from_claims(
          ["at:", "model_code", "payout_range"], "normal", "2026-08-04")))
    t("★★天井の種類が不明（ceiling:None:）は通さない★★",
      _raises(lambda: decide_from_claims(
          ["ceiling:None:", "model_code", "payout_range"], "normal",
          "2026-08-04")))
    t("★★名前の無いCZ（cz:）は通さない★★",
      _raises(lambda: decide_from_claims(
          ["cz:", "model_code", "payout_range"], "normal", "2026-08-04")))
    t("★★材料側でも欠けた値からclaimを作らない★★",
      _raises(lambda: claims_from_material(
          {"at_specs": {"adopted": [{"mode": None, "games": 30, "net": 2.8}]}}))
      and _raises(lambda: claims_from_material(
          {"at_specs": {"adopted": [{"mode": "MAIN_AT", "games": None,
                                     "net": None}]}}))
      and _raises(lambda: claims_from_material(
          {"czs": {"adopted": [{"name": ""}]}}))
      and _raises(lambda: claims_from_material(
          {"ceilings": {"adopted": [{"kind": None, "amount": 800}]}})))
    t("　正しい材料からは今までどおりclaimが出る",
      claims_from_material(
          {"ceilings": {"adopted": [{"kind": "GAME", "amount": 800,
                                     "counted": "通常時"}]},
           "czs": {"adopted": [{"name": "喰霊チャンス"}]}})
      == ["ceiling:GAME:通常時", "cz:喰霊チャンス"])
    t("★実在しない日付（2026-99-99）の判定書は通さない★",
      _raises(lambda: validate_decision({**d, "decided_at": "2026-99-99"})))
    # ★公開済みの機種にも緊急overrideが効く★（Codex73回目の指摘1）
    t("★★override中は、公開時にindexableで焼かれた機種もnoindex側になる★★",
      machine_class(m_auto, FORCE) == "AUTO_PENDING"
      and machine_class(m_auto, NORMAL) == "AUTO_INDEXABLE")
    t("★★policyを切り替えたら、成果物が古い機種を一覧できる★★",
      stale_decisions([m_auto, {"slug": "x"}], FORCE) == ["a"]
      and stale_decisions([m_auto], NORMAL) == [])
    # ★★型式名を濃さから外した（2026-08-23・台帳#461）★★
    MAT_CODE = {"adopted": {"model_code": {"value": "L試験A1"},
                            "payout_range": {"value": {"low": 97, "high": 110}}},
                "at_specs": {"adopted": [{"mode": "MAIN_AT",
                                          "games": 30, "net": 2.8}]}}
    t("★★新しい材料の型式名は「濃さ」に数えない★★",
      index_claims_from_material(MAT_CODE) == ["at:MAIN_AT", "payout_range"])
    t("★★型式名だけでは品質ラインに届かない（claim2件）★★",
      not decide(MAT_CODE, NORMAL, "2026-08-04")["indexable"])
    # ★保存済みの判定書は今までどおり読める★（6機種が model_code を持つ）
    t("★★昔の判定書の型式名は今までどおり読める★★",
      _category("model_code") == "spec"
      and decide_from_claims(["model_code", "payout_range", "at:MAIN_AT"],
                             "normal", "2026-08-04")["indexable"])
    # ★★2AIの確定値は「濃さ」には数えず「知っている」には数える★★
    CV = {"_from": "confirmed_values"}
    MAT_CV = {"adopted": {"games_per_50": {**CV, "value": {"games": 36.1}}},
              "ceilings": {"adopted": [{**CV, "kind": "GAME", "amount": 999,
                                        "counted": "通常時"}]},
              "at_specs": {"adopted": [{**CV, "mode": "MAIN_AT", "net": 1.0}]},
              "czs": {"adopted": [{**CV, "name": "解放の刻"}]}}
    t("★★2AIの確定値は検索の濃さに数えない（今までどおり）★★",
      index_claims_from_material(MAT_CV) == [])
    t("★★2AIの確定値も「知っている」には数える（消失の判定用）★★",
      regression_claims_from_material(MAT_CV)
      == ["at:MAIN_AT", "ceiling:GAME:通常時", "cz:解放の刻", "games_per_50"])
    t("　機械が裏取りした値は、どちらの数え方でも同じ",
      index_claims_from_material(MAT_OK)
      == regression_claims_from_material(MAT_OK))
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

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
# ★★判定書 v2★★（2026-08-25・Codexの27回目の設計助言）
#   ★なぜ v2 を作るか★＝判定書は項目の**完全一致**を要求するので、
#   v1 に項目を足すと**既にある11機種が「壊れている」扱い**になる。
#   v1 は読めるまま残し、v2 へは明示的に移す。
SCHEMA_V2 = "page-decision/v2"
SCHEMAS = (SCHEMA, SCHEMA_V2)

# ★★機種の型★★（掲載判定の線を選ぶためだけに使う。claim には数えない）
#   AT_CZ   … AT または CZ を持つ機種（いままでの前提）
#   BONUS   … 完全告知などのボーナスタイプ（AT も CZ も無い）
#   UNKNOWN … まだ決まっていない
#   ★UNKNOWN を AT_CZ に倒さない★（Codexの助言）＝
#   倒すと、原因がまた `NO_UNIQUE_GAMEPLAY` に隠れて見えなくなる。
MACHINE_PROFILES = ("AT_CZ", "BONUS", "UNKNOWN")

# ★★天井の状態★★（★型から推論してはいけない★・Codexの助言）
#   実例＝X-300 は概要が「完全告知のボーナスタイプ」だが、
#   天井欄は「調査中」。＝型が分かっても天井の有無は分からない。
#   PRESENT … 天井がある   NONE … 天井が無い   UNKNOWN … 分からない
CEILING_STATES = ("PRESENT", "NONE", "UNKNOWN")
POLICY_SCHEMA = "indexing-policy/v1"
POLICY_PATH = os.path.join(BASE, "assets", "data", "indexing-policy.json")
POLICY_MODES = ("normal", "force_noindex_new_auto")

# ★topicの宇宙は固定★（省略された topic は pending。二値にしない設計の入口）
TOPICS = ("gameplay", "cz", "ceiling", "spec", "setting", "strategy", "reset")

# claim ID → カテゴリ（★同一claimの水増し・複数カテゴリ加算を許さない★）
_SPEC_CLAIMS = ("payout_range", "games_per_50", "at_prob", "payout_rate")
# ★★ボーナスタイプの「その機種らしさ」★★（2026-08-25・Codexの27回目）
#   ★at_prob を流用しない★＝あちらは「AT初当たり確率」専用で、しかも
#   payout_rate などと同じ `spec` カテゴリ。流用すると
#   **「種類が2つ以上」の条件が満たせないまま**になる。
#   ★表全体で1件★＝設定の数やBIG/REGの列ごとに水増ししない。
_BONUS_CLAIMS = ("bonus_prob",)

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
# ★★claim が入る箱の名簿（正本）★★（2026-08-24・Codexの3回目の指摘4）
#   ★増やしたら3か所に効く★＝
#     ①ここ ②`_claims` の各ループ ③`adoption_basis` の通し試験の表
#   ★書き忘れを機械が見つける★＝
#     ・②は page_decision の自己試験（ループの中に箱の名前が出るか）
#     ・③は adoption_basis の自己試験（表の箱がこの名簿と一致するか）
#   ★なぜ要るか★＝天井とスペックだけ本物の抽出器を通し、
#   AT と CZ を手作りの材料のまま残した（1日に7回「片方だけ直した」）。
#   1つずつ書いていると、必ずどれかを書き忘れる。
CLAIM_BOXES = ("adopted", "ceilings", "at_specs", "czs")

# ★★読者に出る箱の名簿（正本）★★（2026-08-24・Codexの4回目の指摘）
#   ★CLAIM_BOXES とは別物★＝あちらは「検索の濃さを数える箱」、
#   こちらは「記事に文章として出る箱」。
#   ★分けた理由★＝gameplays / resets は検索の濃さには数えないが、
#   **読者には出る**。CLAIM_BOXES だけを見ていたので、
#   この2つが根拠の関所を素通りしていた（Codexが実際に見つけた）。
#   ★増やしたら `build_new_article._BASIS_REQUIRED` にも足す★
#   （足し忘れは build_new_article の自己試験が落とす）。
#   ★★2026-08-24：定義しただけで誰も読んでいなかった★★（Codexの5回目）
#   ★私は「名簿を1か所にした」と報告したが、関所は別の手書き表を読んでいた★
#   ＝「名簿を作っても実処理がその名簿を読んでいない」という、
#     まさに直したかった型を、直したつもりの場所で作っていた。
#   → 箱ごとに必要な根拠の鍵まで持たせ、関所がここから組み立てる。
READER_BOXES = {
    "adopted": ("basis",),            # 基本スペック（払い出し・50枚あたり…）
    "ceilings": ("basis",),           # 天井
    "at_specs": ("basis",),           # AT
    "czs": ("basis", "games_basis", "rate_basis"),     # CZ
    "gameplays": ("basis",),          # ゲームの流れ
    "resets": ("basis",),             # 朝一・リセット
}

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


def _bad_value_deep(v) -> bool:
    """値そのものが空か（組の中まで見る）。★spec系の検査に使う★"""
    if isinstance(v, dict):
        return (not v) or any(_bad_value_deep(x) for x in v.values())
    if isinstance(v, (list, tuple)):
        return (not v) or any(_bad_value_deep(x) for x in v)
    return _bad_value(v)


def _from_2ai(v) -> bool:
    """2AIが確定させた値か（機械の裏取りをまだ通っていない）。"""
    return isinstance(v, dict) and v.get("_from") == "confirmed_values"


def _single_source(v) -> bool:
    """★DMM単独で採った値か★（2026-08-23・運営者決定の例外）

    ★検索の濃さには数えない★＝記事には載せるが、
    「検索に載せてよい濃さ」の点にはしない（2AIの確定値と同じ二層化）。

    ★★件数に期待して安全だと思わないこと★★（Codexの指摘で気づいた）
      私は「DMM単独なら claim 2件以下だから、どうせ noindex になる」と
      考えていたが、**運営者決定は「DMMページにあるものは全部採用」**なので、
      DMMだけで 機械割・天井・AT・CZ が採れると
      claim 5件・カテゴリ4種・固有ゲーム性ありになり
      ★AUTO_INDEXABLE になり得る★。だから明示的に外す。
    """
    return (isinstance(v, dict)
            and str(v.get("basis") or "") == "DMM_SINGLE_NEAR_RELEASE")


def _skip_for_index(v, count_confirmed: bool) -> bool:
    """検索の濃さに数えないもの（★回帰検査では数える★）。

    ★★白名簿にした（2026-08-23・Codexの敵対的レビューP0）★★
      ★直す前は黒名簿だった★＝「DMM単独のときだけ外す」。
      これだと ★根拠を保存し忘れた経路が「普通のclaim」として数えられる★。

      実際そうなっていた＝`spec_lookup` と `ceiling_lookup` は
      採用値に basis を保存しておらず、**DMM単独の機械割・コイン持ち・天井が
      検索の濃さに数えられていた**（＝1出典だけの内容が検索に出る経路）。
      ★私は「除外を入れた」と報告していたが、実際には破れていた★。

      ★白名簿なら、保存し忘れは「数えない」側に倒れる★＝
      安全側で落ちるので、同じ抜け方が二度と起きない。
    """
    if count_confirmed:
        return False                      # 回帰検査＝知っているかを見る側
    # ★数えてよいのは「独立2出典で採った」と明示されたものだけ★
    return not (isinstance(v, dict)
                and str(v.get("basis") or "") == "INDEPENDENT_MULTI")


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
        # ★★spec系も「検査が先」★★（2026-08-23・Codexの再レビューP1）
        #   ★天井・AT・CZだけ直して、ここを忘れていた★＝
        #   根拠なしの壊れたspec材料が黙って読み飛ばされていた。
        if v is not None and not isinstance(v, dict):
            raise DecisionError(f"{key} の形が違います: {v!r}")
        if isinstance(v, dict) and "value" in v and _bad_value_deep(v["value"]):
            raise DecisionError(f"{key} の値がありません: {v!r}")
        # ★2AIが確定した値は「検索に載せてよい濃さ」に数えない★
        #   （2026-08-09・依頼130 P1-3）
        #   記事に載せる材料としては使うが、claim の裏取り（verify_claims）を
        #   まだ通っていない。数えると、機械が確かめていない値で
        #   検索に載る判定が出てしまう。★載せるのは裏取り後★
        if v and not _skip_for_index(v, count_confirmed):
            got.add(key)
    for c in ((material or {}).get("ceilings") or {}).get("adopted") or []:
        # ★★壊れていないかを先に見る★★（2026-08-23）
        #   ★白名簿を検査より前に置くと、根拠の無い壊れた材料が
        #     黙って読み飛ばされ、fail-closed が壊れる★（入れた直後に踏んだ）
        kind = (c or {}).get("kind")
        if kind not in CEILING_KINDS:
            raise DecisionError(f"天井の種類が不明です: {kind!r}")
        if _bad_value(c.get("amount")):
            raise DecisionError(f"天井の値がありません: {c!r}")
        if _skip_for_index(c, count_confirmed):
            continue                 # ★数えないだけ（検査は済ませた）★
        counted = "" if _bad_value(c.get("counted")) else str(c["counted"]).strip()
        got.add(f"ceiling:{kind}:{counted}")
    for c in ((material or {}).get("at_specs") or {}).get("adopted") or []:
        mode = (c or {}).get("mode")   # ★検査が先★（上と同じ理由）
        if mode not in AT_MODES:
            raise DecisionError(f"ATのモードが不明です: {mode!r}")
        # ★どれか1つでも中身があればclaimにする★（2026-08-09）
        #   以前は「1セットG数」と「純増」の両方を必須にしていたが、
        #   継続率しか公表されていない機種が実在する（パリピ孔明）。
        #   両方必須だと、確かに分かっている継続率まで捨てることになる。
        if all(_bad_value(c.get(k)) for k in ("games", "net", "loop_rate")):
            raise DecisionError(f"ATの値がありません: {c!r}")
        if _skip_for_index(c, count_confirmed):
            continue                 # ★数えないだけ（検査は済ませた）★
        got.add(f"at:{mode}")
    for c in ((material or {}).get("czs") or {}).get("adopted") or []:
        nm = _norm_name((c or {}).get("name"))   # ★検査が先★
        if _bad_value(nm):
            raise DecisionError(f"CZの名前がありません: {c!r}")
        if _skip_for_index(c, count_confirmed):
            continue                 # ★数えないだけ（検査は済ませた）★
        got.add(f"cz:{nm}")
    # ★ボーナスタイプの「その機種らしさ」★（2026-08-25）
    got.update(_bonus_claim(material, count_confirmed))
    return sorted(got)


def _bonus_claim(material: dict, count_confirmed: bool) -> list:
    """★設定別のボーナス確率が採れていれば claim を1件だけ立てる★

    ★表全体で1件★（2026-08-25・Codexの助言）＝
    設定の数やBIG/REGの列ごとに水増ししない。
    """
    v = (material.get("adopted") or {}).get("bonus_prob")
    if not v:
        return []
    if _skip_for_index(v, count_confirmed):
        return []
    val = v.get("value")
    if not isinstance(val, dict) or not val:
        return []
    return ["bonus_prob"]


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
    if claim in _BONUS_CLAIMS:
        return "bonusflow"                 # ★spec とは別の種類★
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
                "POLICY_FORCE_NOINDEX",
                # ★v2で足した★（2026-08-25）
                "NO_BONUS_PROB",           # ボーナスタイプなのに確率が無い
                "MACHINE_PROFILE_UNKNOWN")  # 型が決まっていない
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


_DECISION_KEYS_V2 = _DECISION_KEYS | {"machine_profile", "ceiling_state"}


def decide_from_claims_v2(claims: list, mode: str, profile: str,
                          ceiling_state: str = "UNKNOWN",
                          decided_at: str = "") -> dict:
    """★判定書 v2★＝機種の型ごとに品質ラインを変える。

    ★★なぜ要るか（2026-08-25・Codexの27回目）★★
      v1 は3つ目の条件で **at:/cz: を必ず要求**していた。
      ノーマル機（完全告知のボーナスタイプ）には AT も CZ も**存在しない**ので、
      ★材料が全部揃っても永久に検索へ載せられない★。
      実例＝マイジャグラーV は載っているのに、
      新台経路で作った マイジャグラーVI は永久に載らない。

    ★型と天井の有無は別々★（Codexの助言）＝
      「ボーナスタイプ」と分かっても、天井が無いとは限らない。
      実例＝X-300 は概要が「完全告知のボーナスタイプ」だが天井欄は「調査中」。

    ★型そのものは claim に数えない★＝判定の線を選ぶためだけに使う。
    """
    if mode not in POLICY_MODES:
        raise DecisionError(f"policy mode が不明です: {mode!r}")
    if profile not in MACHINE_PROFILES:
        raise DecisionError(f"機種の型が不明です: {profile!r}")
    if ceiling_state not in CEILING_STATES:
        raise DecisionError(f"天井の状態が不明です: {ceiling_state!r}")
    claims = sorted(set(claims))
    confirmed, pending = topics_from_claims(claims)
    cats = sorted({_category(c) for c in claims})
    reasons = []
    if len(claims) < MIN_CLAIMS:
        reasons.append("CLAIMS_LT_3")
    if len(cats) < MIN_CATEGORIES:
        reasons.append("CATEGORIES_LT_2")
    # ★★型ごとに「その機種らしさ」の求め方を変える★★
    if profile == "AT_CZ":
        if not any(c.startswith(("at:", "cz:")) for c in claims):
            reasons.append("NO_UNIQUE_GAMEPLAY")
    elif profile == "BONUS":
        if not any(c in _BONUS_CLAIMS for c in claims):
            reasons.append("NO_BONUS_PROB")
    else:                                  # UNKNOWN
        # ★黙って AT の線に倒さない★＝原因が隠れるため（Codexの助言）
        reasons.append("MACHINE_PROFILE_UNKNOWN")
    indexable = not reasons
    if mode == "force_noindex_new_auto":
        indexable = False
        reasons = reasons + ["POLICY_FORCE_NOINDEX"]
    digest_src = json.dumps(
        {"schema": SCHEMA_V2, "claims": claims, "mode": mode,
         "profile": profile, "ceiling_state": ceiling_state},
        ensure_ascii=False, sort_keys=True)
    return {
        "schema_version": SCHEMA_V2,
        "indexable": indexable,
        "machine_profile": profile,
        "ceiling_state": ceiling_state,
        "confirmed_topics": confirmed,
        "pending_topics": pending,
        "reason_codes": reasons,
        "claims": claims,
        "policy_mode": mode,
        "decided_at": decided_at or date.today().isoformat(),
        "input_digest": "sha256:" + hashlib.sha256(
            digest_src.encode("utf-8")).hexdigest(),
    }


def profile_from_material(material: dict) -> str:
    """★材料から機種の型を取る★（2AIが確定させたものだけ）。

    ★機械が本文を読んで決めない★＝意味の判断は2AIの仕事。
    決まっていなければ UNKNOWN（＝掲載不可・理由も残る）。
    """
    v = ((material.get("adopted") or {}).get("machine_profile") or {})
    got = (v.get("value") or {}).get("profile")
    return got if got in MACHINE_PROFILES else "UNKNOWN"


def ceiling_state_from_material(material: dict) -> str:
    """★材料から天井の有無を取る★（★型から推論しない★）。

    ★なぜ分けるか（2026-08-25・Codexの27回目）★
      X-300 は概要が「完全告知のボーナスタイプ」でも、
      天井欄は「調査中」＝**型が分かっても天井の有無は分からない**。
    ★天井のclaimが実際にあるなら PRESENT★（そちらが強い証拠）。
    """
    if (material.get("ceilings") or {}).get("adopted"):
        return "PRESENT"
    v = ((material.get("adopted") or {}).get("ceiling_state") or {})
    got = (v.get("value") or {}).get("state")
    return got if got in CEILING_STATES else "UNKNOWN"


def decide_v2(material: dict, policy: dict | None = None,
              decided_at: str = "") -> dict:
    """★材料から v2 の判定書を作る★（新台経路が使う）。"""
    policy = policy if policy is not None else load_policy()
    return decide_from_claims_v2(
        index_claims_from_material(material), policy["mode"],
        profile_from_material(material),
        ceiling_state_from_material(material), decided_at)


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
    # ★★v1 と v2 を併読する★★（2026-08-25・Codexの27回目）
    #   ★v1 に項目を足すと、既にある11機種が「壊れている」扱いになる★ので、
    #   版ごとに求める項目を分ける。v1 の機種は今までどおり読める。
    ver = pd.get("schema_version")
    if ver not in SCHEMAS:
        raise DecisionError(f"判定書の schema が違います: {ver!r}")
    keys = _DECISION_KEYS_V2 if ver == SCHEMA_V2 else _DECISION_KEYS
    missing = sorted(keys - set(pd))
    extra = sorted(set(pd) - keys)
    if missing or extra:
        raise DecisionError(f"判定書の項目が違います（欠け={missing} 余分={extra}）")
    if ver == SCHEMA_V2:
        if pd["machine_profile"] not in MACHINE_PROFILES:
            raise DecisionError(
                f"判定書の機種の型が不明です: {pd['machine_profile']!r}")
        if pd["ceiling_state"] not in CEILING_STATES:
            raise DecisionError(
                f"判定書の天井の状態が不明です: {pd['ceiling_state']!r}")
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
    if ver == SCHEMA_V2:
        want = decide_from_claims_v2(
            pd["claims"], pd["policy_mode"], pd["machine_profile"],
            pd["ceiling_state"], pd["decided_at"])
    else:
        want = decide_from_claims(pd["claims"], pd["policy_mode"],
                                  pd["decided_at"])
    for k in sorted(keys):
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
    # ★v1 と v2 の両方を認める★（2026-08-25。v1 の機種は今までどおり）
    if pub not in SCHEMAS:
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
    # ★★版に合わせて計算し直す★★（2026-08-25）
    #   ★ここを v1 のままにすると、v2 の機種は永久に AUTO_PENDING★
    #   ＝直したはずの欠陥が、最後の一行で元に戻る。
    if pd.get("schema_version") == SCHEMA_V2:
        now = decide_from_claims_v2(pd["claims"], pol_mode,
                                    pd["machine_profile"],
                                    pd["ceiling_state"], pd["decided_at"])
    else:
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

    # ★白名簿に合わせて、試験の材料も実物と同じく根拠を持たせる★
    #   （2026-08-23）実物の抽出器は必ず basis を入れる。
    #   ★根拠の無い材料は「数えない」側に落ちる★のが新しい決まり。
    IM = {"basis": "INDEPENDENT_MULTI"}
    NORMAL = {"schema_version": POLICY_SCHEMA, "mode": "normal", "reason": ""}
    FORCE = {"schema_version": POLICY_SCHEMA,
             "mode": "force_noindex_new_auto", "reason": "試験"}
    # 材料: claim3件・カテゴリ2種・ゲーム性あり = 合格ライン丁度
    MAT_OK = {"adopted": {"games_per_50": {**IM, "value": {"games": 36.1}},
                          "payout_range": {**IM,
                                           "value": {"low": 97, "high": 110}}},
              "at_specs": {"adopted": [{**IM, "mode": "MAIN_AT",
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
    MAT_2 = {"adopted": {"payout_range": {**IM,
                                          "value": {"low": 97, "high": 110}}},
             "at_specs": {"adopted": [{**IM, "mode": "MAIN_AT",
                                       "games": 30, "net": 2.8}]}}
    d2 = decide(MAT_2, NORMAL, "2026-08-04")
    t("★claim1件減 → indexable=false＋理由コード★",
      not d2["indexable"] and "CLAIMS_LT_3" in d2["reason_codes"])
    # ゲーム性なし（spec3件だけ）→ 不合格
    MAT_SPEC = {"adopted": {"at_prob": {**IM, "value": 1},
                            "payout_range": {**IM, "value": 1},
                            "games_per_50": {**IM, "value": 1}}}
    d3 = decide(MAT_SPEC, NORMAL, "2026-08-04")
    t("★spec3件だけ（カテゴリ1種・ゲーム性なし）→ 不合格★",
      not d3["indexable"] and "CATEGORIES_LT_2" in d3["reason_codes"]
      and "NO_UNIQUE_GAMEPLAY" in d3["reason_codes"])
    # 並べ替え・重複で不変
    MAT_DUP = {"adopted": dict(MAT_OK["adopted"]),
               "at_specs": {"adopted": [
                   {**IM, "mode": "MAIN_AT", "games": 30, "net": 2.8},
                   {**IM, "mode": "MAIN_AT", "games": 30, "net": 2.8}]}}
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
          {"ceilings": {"adopted": [{**IM, "kind": "GAME", "amount": 800,
                                     "counted": "通常時"}]},
           "czs": {"adopted": [{**IM, "name": "喰霊チャンス"}]}})
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
    MAT_CODE = {"adopted": {"model_code": {**IM, "value": "L試験A1"},
                            "payout_range": {**IM,
                                             "value": {"low": 97, "high": 110}}},
                "at_specs": {"adopted": [{**IM, "mode": "MAIN_AT",
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
    # ★★DMM単独で採った値も検索の濃さに数えない★★（2026-08-23・運営者決定）
    #   ★私が間違えた点★＝「DMM単独なら claim 2件以下だから、どうせ
    #   noindex になる」と考えたが、運営者決定は「DMMページにあるものは
    #   全部採用」なので、DMMだけで5claim・4カテゴリになり得る。
    SS = {"basis": "DMM_SINGLE_NEAR_RELEASE"}
    MAT_SS = {"adopted": {"payout_range": {**SS, "value": {"low": 97,
                                                           "high": 110}},
                          "games_per_50": {**SS, "value": {"games": 36.1}}},
              "ceilings": {"adopted": [{**SS, "kind": "GAME", "amount": 999,
                                        "counted": "通常時"}]},
              "at_specs": {"adopted": [{**SS, "mode": "MAIN_AT", "net": 1.0}]},
              "czs": {"adopted": [{**SS, "name": "解放の刻"}]}}
    t("★★DMM単独の値は検索の濃さに数えない★★"
      "／★これが無いと1出典だけの内容が検索に出る★",
      index_claims_from_material(MAT_SS) == [])
    # ★★根拠を保存し忘れた値も数えない★★（2026-08-23・Codexの敵対的レビューP0）
    #   ★実際に起きていた★＝spec_lookup と ceiling_lookup が basis を
    #   保存しておらず、DMM単独の機械割・コイン持ち・天井が
    #   **普通のclaimとして数えられていた**（＝1出典の内容が検索に出る経路）。
    #   ★黒名簿（DMM単独だけ外す）では、保存し忘れが素通りする★ので白名簿にした。
    NOB = {"adopted": {"payout_range": {"value": {"low": 97, "high": 110}},
                       "games_per_50": {"value": {"games": 36.1}}},
           "ceilings": {"adopted": [{"kind": "GAME", "amount": 999,
                                     "counted": "通常時"}]},
           "at_specs": {"adopted": [{"mode": "MAIN_AT", "net": 1.0}]}}
    t("★★根拠が付いていない値は、検索の濃さに数えない★★"
      "／保存し忘れを「普通のclaim」として通さない",
      index_claims_from_material(NOB) == [])
    t("★★それでも壊れた材料は今までどおり例外で止まる★★"
      "／★数えないことと、検査を飛ばすことは別★"
      "（白名簿を検査より前に置いて、入れた直後にここを壊した）",
      _raises(lambda: index_claims_from_material(
          {"ceilings": {"adopted": [{"kind": None, "amount": 800}]}}))
      and _raises(lambda: index_claims_from_material(
          {"at_specs": {"adopted": [{"mode": "MAIN_AT", "games": None,
                                     "net": None}]}}))
      and _raises(lambda: index_claims_from_material(
          {"czs": {"adopted": [{"name": ""}]}})))
    # ★★spec系も壊れていれば例外で止まる★★（2026-08-23）
    #   ★ミューテーション試験に名指しされた★＝天井・AT・CZだけ見ていて、
    #   spec系の検査を消しても誰も気づかなかった。
    t("★★spec系の壊れた材料も例外で止まる★★"
      "／★数えないことと、検査を飛ばすことは別★",
      _raises(lambda: index_claims_from_material(
          {"adopted": {"payout_range": {**IM, "value": {"low": None,
                                                        "high": 110}}}}))
      and _raises(lambda: index_claims_from_material(
          {"adopted": {"games_per_50": {**IM, "value": {}}}}))
      and _raises(lambda: index_claims_from_material(
          {"adopted": {"payout_rate": "文字列は形が違う"}})))
    # ★★名簿の箱が、本当に読まれているか★★（2026-08-24・Codexの3回目の指摘4）
    #   ★名簿を置いただけでは、実物とずれる★＝
    #   `CLAIM_BOXES` に足したのに `_claims` のループに足し忘れると、
    #   その家族の値は**検査もされず、数えられもしない**まま公開される。
    #   ★字面で確かめる★＝ループの中に箱の名前が出ているか。
    import inspect as _insp
    _src_claims = _insp.getsource(_claims)
    t("★★名簿の箱は全部 _claims が読んでいる★★"
      "（名簿に足してループに足し忘れると、その家族は素通りする）",
      all(f'"{b}"' in _src_claims or f"'{b}'" in _src_claims
          for b in CLAIM_BOXES))
    t("　（対照）実在しない箱を名簿に入れたら気づく",
      not all(f'"{b}"' in _src_claims
              for b in tuple(CLAIM_BOXES) + ("zzz_no_such_box",)))
    t("★★対照：外していなければ5claim・4カテゴリで検索に載ってしまう★★"
      "／件数に期待して安全だと思わない",
      len(regression_claims_from_material(MAT_SS)) == 5)
    t("★★DMM単独だけの機種は indexable にならない★★",
      not decide(MAT_SS, NORMAL, "2026-08-23")["indexable"])
    t("　DMM単独の値も「知っている」には数える（消失の判定用）",
      regression_claims_from_material(MAT_SS)
      == ["at:MAIN_AT", "ceiling:GAME:通常時", "cz:解放の刻",
          "games_per_50", "payout_range"])
    # 実ファイルのpolicyが読める（形式検査）
    try:
        p = load_policy()
        t("★実物の indexing-policy.json が読める（mode=normal想定）★",
          p.get("mode") in POLICY_MODES)
    except DecisionError as e:
        t(f"★実物の indexing-policy.json が読める★（{e}）", False)
    # ★★★判定書v2：ノーマル機の救済★★★（2026-08-26・Codexの27回目）
    #   ★直す前は at:/cz: が必須★＝ジャグラー等は材料が全部揃っても
    #   **原理的に永久に検索へ載せられなかった**（マイジャグラーV は
    #   載っているのに、新台経路の VI は永久に載らない、が実際に起きていた）。
    _c3 = ["payout_range", "games_per_50", "bonus_prob"]
    _at3 = ["payout_range", "games_per_50", "at:MAIN_AT"]
    t("★★★ノーマル機は、ボーナス確率があれば載せられる★★★"
      "／★これが無いと、ジャグラー等の新台は永久に載らない★",
      decide_from_claims_v2(_c3, "normal", "BONUS")["indexable"] is True)
    t("★★ボーナスタイプに at:/cz: を求めない★★"
      "／★求めると、存在しないものを要求することになる★",
      "NO_UNIQUE_GAMEPLAY"
      not in decide_from_claims_v2(_c3, "normal", "BONUS")["reason_codes"])
    t("　ボーナス確率が無ければ載せない（線は緩めない）",
      "NO_BONUS_PROB"
      in decide_from_claims_v2(_at3, "normal", "BONUS")["reason_codes"])
    t("★★AT機の線は今までどおり★★（at:/cz: が要る）",
      decide_from_claims_v2(_at3, "normal", "AT_CZ")["indexable"] is True
      and "NO_UNIQUE_GAMEPLAY"
      in decide_from_claims_v2(_c3, "normal", "AT_CZ")["reason_codes"])
    t("★★型が不明なら載せない（ATの線に黙って倒さない）★★"
      "／★倒すと、原因が NO_UNIQUE_GAMEPLAY に隠れて見えなくなる★",
      decide_from_claims_v2(_c3, "normal", "UNKNOWN")["indexable"] is False
      and decide_from_claims_v2(_c3, "normal", "UNKNOWN")["reason_codes"]
      == ["MACHINE_PROFILE_UNKNOWN"])
    t("　ボーナス確率は spec とは別の種類（種類2つの条件を満たせる）",
      _category("bonus_prob") != _category("payout_range"))
    # ★★区分は版に合わせて計算し直す★★
    #   ★ここが v1 のままだと、v2 の機種は永久に AUTO_PENDING★
    #   ＝直したはずの欠陥が、最後の一行で元に戻る。
    _d_v2 = decide_from_claims_v2(_c3, "normal", "BONUS", "NONE", "2026-08-26")
    _m_v2 = {"slug": "zzz_v2", "name": "試験", "publication_policy": SCHEMA_V2,
             "page_decision": _d_v2}
    t("★★v2 の機種が、条件を満たせば AUTO_INDEXABLE になる★★"
      "／★区分を v1 の式で計算すると、永久に AUTO_PENDING のまま★",
      machine_class(_m_v2, {"mode": "normal"}) == "AUTO_INDEXABLE")
    # ★★天井の有無は、型から推論しない★★
    #   実例＝X-300 は概要が「完全告知のボーナスタイプ」でも天井欄は「調査中」。
    t("★★型が BONUS でも、天井の有無は分からないまま★★"
      "／★推論すると、出典に無いことを断定することになる★",
      ceiling_state_from_material(
          {"adopted": {"machine_profile": {"value": {"profile": "BONUS"}}}})
      == "UNKNOWN")
    t("　天井のclaimが実際にあれば PRESENT",
      ceiling_state_from_material(
          {"ceilings": {"adopted": [{"kind": "GAME", "amount": 999}]}})
      == "PRESENT")
    t("　2AIが「無い」と確定させたときだけ NONE",
      ceiling_state_from_material(
          {"adopted": {"ceiling_state": {"value": {"state": "NONE"}}}})
      == "NONE")
    # ★v1 は無傷★
    t("　v1 の判定書は今までどおり読める（併読）",
      decide_from_claims(_at3, "normal", "2026-08-26")["indexable"] is True)

    print(f"{ran[0]}/{ran[0]} 合格" if ok_all else "不合格あり")
    return 0 if ok_all else 1


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="新台経路の判定書")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    sys.exit(selftest() if a.selftest else 0)

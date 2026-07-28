#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""claim_c5.py — 引用文から値を導き出して claim と突き合わせる（意味の検証）

★C5とは何か★
  C0〜C4 は「URLが実在する」「引用がページにある」「値が引用の中にある」までを見る。
  しかしそれだけでは、**引用の中の数字が本当にその項目の値か**が分からない。
  例：「設定1の機械割は97.2%、設定6は106.5%」という引用に対し、
      「設定6の機械割=97.2%」という claim も、値も引用も一致してしまう。

  C5は**引用文を解析して (機種・設定・項目・値・単位) を導き**、
  claim と**完全一致したときだけ PASS** にする。

★設計上の約束（Codex 3回目）★
  - C5の結果を台帳から受け取らない。**必ずここで計算し直す**
  - 最初の数字だけを取らない。曖昧な文字列は FAIL
  - 導出結果が claim と完全一致した場合だけ PASS

使い方:
    python scripts/claim_c5.py --selftest
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from decimal import Decimal, InvalidOperation

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 「設定N の 機械割 は X%」という組を、文中から**すべて**拾う
_SETTING = r"(?:設定|設)\s*([1-6])"
_PCT = r"([0-9]{1,3}(?:\.[0-9]+)?)\s*[%％]"
_KIKAIWARI_WORD = r"(?:機械割|出玉率)"

# 設定と値をつなぐ言い回し。★任意の文字は挟ませない★
#   （挟ませると「設定1…（別の話）…106.5%」を組と誤読する）
# ★★組の中に「機械割／出玉率」を必須にする★★（Codex (a)-3）
#   これが任意だと「設定1の勝率は97.2%。設定6の機械割は106.5%」から
#   勝率97.2%を設定1の機械割として導いてしまう。
_WORD = r"(?:機械割|出玉率)"
_CONNECT_LABELED = r"(?:\s*の?\s*" + _WORD + r"\s*[:：はが＝=→]?\s*)"
_CONNECT_BARE = r"(?:\s*[:：＝=]\s*)"

# 文章形式：組ごとに「機械割」語が入っているものだけ
_PAIR_LABELED = re.compile(_SETTING + _CONNECT_LABELED + _PCT)
# 列挙形式：「機械割は 設定1:97.2% / 設定6:106.5%」のように
#   表頭で1回だけ宣言し、あとは 設定:値 が並ぶ書き方（区切り記号は必須）
_PAIR_BARE = re.compile(_SETTING + _CONNECT_BARE + _PCT)
# 値のあとに括弧で設定を書く形（「106.5%（設定6）」）
_PAIR_PAREN = re.compile(_PCT + r"\s*[（(]\s*" + _SETTING + r"\s*[）)]")

# 列挙形式で「組と宣言語と区切り」以外に残ってよい文字
_ENUM_RESIDUE = re.compile(r"[\s/／、，,・|｜。:：はが＝=]+")

# 値を1つに確定できない書き方（範囲・比較・条件つき・否定）
_AMBIGUOUS = re.compile(
    # 範囲（97.2%〜106.5% / 97.2%-99.9% / 97.2%–99.9%）
    r"[〜~～\-–—－]\s*[0-9]|[0-9]\s*[〜~～\-–—－]|"
    # 比較・概算（97.2%未満 / 以上 / 超 / 約97.2% / 前後）
    r"未満|以下|以上|超え?|前後|程度|およそ|約\s*[0-9]|最大|最低|平均|"
    # 否定・訂正（97.2%ではなく99.9%）
    r"ではなく|で(?:は)?ない|誤り|訂正|"
    r"完全攻略|技術介入|フル攻略|"             # 条件つきの別値
    r"実戦値|実測|サンプル")                   # 解析値でない


def parse_percent(raw: str):
    """'97.2%' を Decimal に。全体が一致しなければ None（先頭だけ取らない）。"""
    m = re.fullmatch(r"\s*([0-9]{1,3}(?:\.[0-9]+)?)\s*[%％]\s*", str(raw or ""))
    if not m:
        return None
    try:
        return Decimal(m.group(1))
    except InvalidOperation:
        return None


def derive_kikaiwari(quote: str) -> dict:
    """引用文から {設定番号: 機械割} を導く。曖昧なら空を返す。

    ★導出できないことと、値が無いことを区別する★
      条件つきの別値（完全攻略時など）や範囲が混ざる文は、
      どの数字がその設定の機械割か決まらないので**何も返さない**。
    """
    q = str(quote or "")
    if not re.search(_KIKAIWARI_WORD, q):
        return {}                      # 機械割の話をしていない
    if _AMBIGUOUS.search(q):
        return {}                      # 値を1つに確定できない書き方

    def _collect(pairs):
        found: dict = {}
        for setting, pct in pairs:
            try:
                val = Decimal(pct)
            except InvalidOperation:
                return None
            if setting in found and found[setting] != val:
                return None            # 同じ設定に別の値＝曖昧
            found[setting] = val
        return found

    # ① 文章形式：組ごとに「機械割」語が入っているもの
    labeled = [(m.group(1), m.group(2)) for m in _PAIR_LABELED.finditer(q)]
    paren = [(m.group(2), m.group(1)) for m in _PAIR_PAREN.finditer(q)]
    if labeled or paren:
        return _collect(labeled + paren) or {}

    # ② 列挙形式：「機械割は 設1:97.2% / 設6:106.5%」
    #    ★組・宣言語・区切り以外の文字が残るなら、別の話が混ざっている★
    bare = [(m.group(1), m.group(2)) for m in _PAIR_BARE.finditer(q)]
    if not bare:
        return {}
    rest = _PAIR_BARE.sub("", q)
    rest = re.sub(_WORD, "", rest)
    rest = re.sub(r"^\s*[*＊]*\s*", "", rest)
    if _ENUM_RESIDUE.sub("", rest):
        return {}                      # 「勝率」など別項目の語が残っている
    return _collect(bare) or {}


def check_c5_kikaiwari(claim: dict, quote: str) -> dict:
    """機械割 claim を引用文と突き合わせる。戻り値は {verdict, code}。"""
    cond = claim.get("conditions") or {}
    val = claim.get("value") or {}
    setting = str(cond.get("setting") or "").strip()

    if claim.get("field_key") != "kikaiwari.setting":
        return {"verdict": "SKIP", "code": "NOT_KIKAIWARI"}
    if setting not in ("1", "2", "3", "4", "5", "6"):
        # ★設定が決まらない機械割 claim は作らせない★
        return {"verdict": "FAIL", "code": "SETTING_MISSING"}
    if val.get("unit") not in ("%", "％"):
        return {"verdict": "FAIL", "code": "UNIT_NOT_PERCENT"}
    if val.get("operator") != "EXACT":
        return {"verdict": "FAIL", "code": "OPERATOR_NOT_EXACT"}

    want = parse_percent(val.get("raw"))
    if want is None:
        return {"verdict": "FAIL", "code": "RAW_NOT_PERCENT"}
    amt = val.get("amount")
    if amt is None or Decimal(str(amt)) != want:
        return {"verdict": "FAIL", "code": "RAW_AMOUNT_MISMATCH"}

    derived = derive_kikaiwari(quote)
    if not derived:
        return {"verdict": "FAIL", "code": "NOT_DERIVABLE_FROM_QUOTE"}
    if setting not in derived:
        # ★引用に「その設定の」機械割が無い★（別の設定の値を流用させない）
        return {"verdict": "FAIL", "code": "SETTING_NOT_IN_QUOTE"}
    if derived[setting] != want:
        return {"verdict": "FAIL", "code": "VALUE_MISMATCH"}
    return {"verdict": "PASS", "code": "OK"}


# ------------------------------------------------- 公開ゲートでの再計算

# 意味の検証ができる項目だけを載せる。★ここに無い項目は自動採用しない★
_SEMANTIC_CHECKERS = {
    "kikaiwari.setting": check_c5_kikaiwari,
}


def semantic_artifact(claim: dict, machine_variant_key: str,
                      registry: dict | None = None) -> dict:
    """claim を出典の引用から検証し直した「検証結果の控え」を作る。

    ★台帳が書いている C5 の結果は読まない★（Codex 3回目 重大4）
      台帳は AI が書き込めるので、そこに PASS と書くだけで通ってしまう。
      公開の可否は**この関数がその場で計算した結果**だけで決める。

    ★台帳の申告は「減点」にしか使わない★
      C0〜C4（URL実在・引用一致など）は引用文だけでは再計算できないので、
      台帳が PASS と言っていることを**必要条件**として使う。
      台帳が FAIL と言っていれば当然数えない。PASS と言っていても、
      ここで C5 を計算し直して不合格なら数えない。
    """
    from claim_ledger import resolve_publisher, load_registry, CHECK_IDS

    registry = registry if registry is not None else load_registry()
    field = claim.get("field_key")
    checker = _SEMANTIC_CHECKERS.get(field)
    art = {"field_key": field, "machine_variant_key": machine_variant_key,
           "verifier_version": VERIFIER_VERSION, "sources": [],
           "counted_votes": 0, "verified": False}
    if checker is None:
        art["reason"] = "NO_SEMANTIC_CHECKER"     # 意味の検証器が無い項目
        return art

    counted, owners, lineages = [], set(), set()
    for i, src in enumerate(claim.get("sources") or []):
        row = {"index": i, "final_url": src.get("final_url")}
        ver = (src.get("verification") or {})
        checks = (ver.get("checks") or {})

        # 1) 台帳が自分で FAIL と言っているものは、その時点で数えない
        #    ★★C5だけでなく、出典全体の判定と票の扱いも見る★★（Codex (a)-2）
        #      これが無いと、台帳が「票に数えない(FAIL)」とした出典を
        #      C5の再計算だけで票に復活させられてしまう。
        declared_bad = [cid for cid in CHECK_IDS if cid != "C5"
                        and (checks.get(cid) or {}).get("verdict") != "PASS"]
        if ver.get("verdict") != "PASS":
            declared_bad.append("verdict")
        if ver.get("vote_disposition") != "COUNTED":
            declared_bad.append("vote_disposition")
        # 2) 発行者を**URLから**引き直す（台帳の申告は使わない）
        pub = resolve_publisher(src.get("final_url"), registry)
        # 3) 機種の型番までの一致（★同名の別バージョンを混ぜない★）
        variant_ok = ver.get("machine_variant_key_matched") == machine_variant_key
        # 4) 引用から値を導き直す
        c5 = checker(claim, src.get("quote"))
        row.update({"c5": c5, "declared_bad": declared_bad,
                    "publisher_id": (pub or {}).get("publisher_id"),
                    "variant_matched": variant_ok})

        if c5["verdict"] != "PASS":
            row["disposition"] = "NOT_COUNTED_C5"
        elif declared_bad:
            row["disposition"] = "NOT_COUNTED_DECLARED_FAIL"
        elif pub is None:
            row["disposition"] = "NOT_COUNTED_UNKNOWN_PUBLISHER"
        elif not variant_ok:
            row["disposition"] = "NOT_COUNTED_VARIANT_MISMATCH"
        elif pub["ownership_group_id"] in owners:
            row["disposition"] = "NOT_COUNTED_SAME_OWNER"
        elif pub["content_lineage_id"] in lineages:
            row["disposition"] = "NOT_COUNTED_LINEAGE"
        else:
            row["disposition"] = "COUNTED"
            owners.add(pub["ownership_group_id"])
            lineages.add(pub["content_lineage_id"])
            counted.append(pub["publisher_id"])
        art["sources"].append(row)

    art["counted_publishers"] = sorted(counted)
    art["counted_votes"] = len(counted)
    # ★独立2出典で同じ値が導けたときだけ VERIFIED★
    art["verified"] = len(counted) >= 2
    if not art["verified"]:
        art["reason"] = "NOT_ENOUGH_INDEPENDENT_VOTES"
    art["artifact_sha256"] = _artifact_sha(art)
    return art


VERIFIER_VERSION = "claim_c5/1"


def _artifact_sha(art: dict) -> str:
    import hashlib
    import json as _json
    body = {k: v for k, v in art.items() if k != "artifact_sha256"}
    return hashlib.sha256(
        _json.dumps(body, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":")).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- selftest

def _claim(setting="1", raw="97.2%", amount=97.2, operator="EXACT", unit="%"):
    return {"field_key": "kikaiwari.setting",
            "conditions": {"setting": setting},
            "value": {"kind": "PERCENT", "raw": raw, "amount": amount,
                      "unit": unit, "operator": operator}}


def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    Q = "機械割は設定1:97.2% / 設定2:98.2% / 設定6:106.5%"

    t("引用と一致する機械割は PASS",
      check_c5_kikaiwari(_claim("1", "97.2%", 97.2), Q)["verdict"] == "PASS")
    t("　設定6も同じ引用から取れる",
      check_c5_kikaiwari(_claim("6", "106.5%", 106.5), Q)["verdict"] == "PASS")

    # ★★Codex が必須と挙げた反例★★
    t("★★設定と値の入れ替えを止める（設定6に設定1の値）★★",
      check_c5_kikaiwari(_claim("6", "97.2%", 97.2), Q)["code"] == "VALUE_MISMATCH")
    t("★★引用に無い設定の値を作れない（設定3は引用に無い）★★",
      check_c5_kikaiwari(_claim("3", "99.4%", 99.4), Q)["code"] == "SETTING_NOT_IN_QUOTE")
    t("★設定が欠けた claim は作れない",
      check_c5_kikaiwari(_claim("", "97.2%", 97.2), Q)["code"] == "SETTING_MISSING")
    t("★★範囲値の引用からは導出しない（97.2%〜106.5%）★★",
      check_c5_kikaiwari(_claim("1", "97.2%", 97.2),
                         "機械割97.2%〜106.5%")["code"] == "NOT_DERIVABLE_FROM_QUOTE")
    t("★★完全攻略など条件つきの別値が混ざる引用からは導出しない★★",
      check_c5_kikaiwari(_claim("1", "98.4%", 98.4),
                         "設定1の機械割は98.4%（完全攻略103.0%）"
                         )["code"] == "NOT_DERIVABLE_FROM_QUOTE")
    t("★実戦値の引用は解析値として使わない",
      check_c5_kikaiwari(_claim("6", "106.5%", 106.5),
                         "設定6の機械割は106.5%（実戦値）"
                         )["code"] == "NOT_DERIVABLE_FROM_QUOTE")
    t("★機械割の話をしていない引用からは導出しない",
      check_c5_kikaiwari(_claim("1", "97.2%", 97.2),
                         "設定1のボーナス出現率は97.2%です"
                         )["code"] == "NOT_DERIVABLE_FROM_QUOTE")
    t("★同じ設定に別の値が併記される引用は曖昧として止める",
      check_c5_kikaiwari(_claim("1", "97.2%", 97.2),
                         "機械割 設定1:97.2% ところにより 設定1:99.9%"
                         )["code"] == "NOT_DERIVABLE_FROM_QUOTE")
    t("★raw と amount が食い違えば止める",
      check_c5_kikaiwari(_claim("1", "97.2%", 106.5), Q)["code"] == "RAW_AMOUNT_MISMATCH")
    t("★単位が % でなければ止める",
      check_c5_kikaiwari(_claim("1", "97.2%", 97.2, unit="G"), Q)["code"]
      == "UNIT_NOT_PERCENT")
    t("★operator が EXACT でなければ止める（機械割に MAX は無い）",
      check_c5_kikaiwari(_claim("1", "97.2%", 97.2, operator="MAX"), Q)["code"]
      == "OPERATOR_NOT_EXACT")
    t("★raw が % の形でなければ止める（先頭の数字だけ取らない）",
      check_c5_kikaiwari(_claim("1", "97.2", 97.2), Q)["code"] == "RAW_NOT_PERCENT")
    t("★機械割以外の claim には使わない（SKIP）",
      check_c5_kikaiwari({**_claim(), "field_key": "ceiling.normal"}, Q)["verdict"]
      == "SKIP")

    # 実データに近い書き方でも導出できること
    real = ("**機械割**：設1：97.2% / 設2：98.2% / 設3：99.4% / "
            "設4：101.6% / 設5：103.8% / 設6：106.5%")
    d = derive_kikaiwari(real)
    t("実データの書き方（設1：97.2% / …）から6設定ぶん導出できる",
      len(d) == 6 and d["6"] == Decimal("106.5"))

    # ------------------------------------------------ 公開ゲートの再計算
    VK = "gogo_juggler3:SS-01"

    def _src(url, quote, c5_declared="PASS", others="PASS", vk=VK,
             verdict="PASS", disp="COUNTED"):
        checks = {c: {"verdict": others} for c in ("C0", "C1", "C2", "C3", "C4")}
        checks["C5"] = {"verdict": c5_declared}
        return {"final_url": url, "quote": quote,
                "verification": {"checks": checks, "verdict": verdict,
                                 "vote_disposition": disp,
                                 "machine_variant_key_matched": vk}}

    def _c(sources, setting="1", raw="97.2%", amount=97.2):
        return {**_claim(setting, raw, amount), "sources": sources}

    Q1 = "機械割は設定1:97.2% / 設定6:106.5%"
    Q2 = "設定1の機械割は97.2%です"
    ok2 = _c([_src("https://chonborista.com/a", Q1),
              _src("https://nana-press.com/b", Q2)])

    a = semantic_artifact(ok2, VK)
    t("独立2出典で同じ値が導ければ VERIFIED", a["verified"] and a["counted_votes"] == 2)
    t("　検証結果の控えに指紋がつく", len(a.get("artifact_sha256", "")) == 64)

    t("★★台帳がC5をPASSと書いていても、引用が合わなければ数えない★★",
      not semantic_artifact(
          _c([_src("https://chonborista.com/a", "設定1の機械割は99.9%"),
              _src("https://nana-press.com/b", Q2)]), VK)["verified"])
    t("★1出典しか導けなければ VERIFIED にしない",
      not semantic_artifact(_c([_src("https://chonborista.com/a", Q1)]),
                            VK)["verified"])
    t("★レジストリに無いホストは票に数えない",
      not semantic_artifact(
          _c([_src("https://chonborista.com/a", Q1),
              _src("https://example.com/b", Q2)]), VK)["verified"])
    t("★★機種の型番が一致しない出典は数えない（同名の別バージョン混入）★★",
      not semantic_artifact(
          _c([_src("https://chonborista.com/a", Q1),
              _src("https://nana-press.com/b", Q2, vk="gogo_juggler3:SS-02")]),
          VK)["verified"])
    t("★機種の型番の記録が無い出典も数えない（既定で不合格）",
      [r["disposition"] for r in semantic_artifact(
          _c([_src("https://chonborista.com/a", Q1),
              {**_src("https://nana-press.com/b", Q2),
               "verification": {"verdict": "PASS", "vote_disposition": "COUNTED",
                                "checks": {c: {"verdict": "PASS"}
                                           for c in ("C0", "C1", "C2", "C3", "C4", "C5")}}}
              ]), VK)["sources"]][1] == "NOT_COUNTED_VARIANT_MISMATCH")
    # ★★Codex 2回目 (a)-2：台帳が票に数えないとした出典を復活させない★★
    t("★★台帳が FAIL とした出典を、C5が合っても票に復活させない★★",
      not semantic_artifact(
          _c([_src("https://chonborista.com/a", Q1, verdict="FAIL",
                   disp="NOT_COUNTED_UNKNOWN"),
              _src("https://nana-press.com/b", Q2, verdict="FAIL",
                   disp="NOT_COUNTED_UNKNOWN")]), VK)["verified"])
    t("　票に数えないと書かれた出典も同様",
      not semantic_artifact(
          _c([_src("https://chonborista.com/a", Q1, disp="NOT_COUNTED_SAME_OWNER"),
              _src("https://nana-press.com/b", Q2)]), VK)["verified"])
    # ★★Codex 2回目 (a)-3：別項目の百分率を機械割として導出しない★★
    t("★★別項目の％を機械割として導出しない（勝率97.2%／機械割106.5%）★★",
      check_c5_kikaiwari(_claim("1", "97.2%", 97.2),
                         "設定1の勝率は97.2%。設定6の機械割は106.5%"
                         )["code"] == "SETTING_NOT_IN_QUOTE")
    t("　同じ引用でも、機械割と明記された設定6は導ける",
      check_c5_kikaiwari(_claim("6", "106.5%", 106.5),
                         "設定1の勝率は97.2%。設定6の機械割は106.5%"
                         )["verdict"] == "PASS")
    t("★列挙形式でも、組以外の語が混ざれば導出しない",
      check_c5_kikaiwari(_claim("1", "97.2%", 97.2),
                         "機械割 設定1:97.2% 勝率 設定2:98.2%"
                         )["code"] == "NOT_DERIVABLE_FROM_QUOTE")
    # ★★Codex 2回目 (a)-4：範囲・比較・否定をEXACTとして通さない★★
    for bad_q in ("設定1の機械割は97.2%-99.9%", "設定1の機械割は97.2%～99.9%",
                  "設定1の機械割は97.2%未満", "設定1の機械割は97.2%以上",
                  "設定1の機械割は97.2%ではなく99.9%", "設定1の機械割は約97.2%"):
        t(f"★★比較・範囲・否定を EXACT にしない：{bad_q}★★",
          check_c5_kikaiwari(_claim("1", "97.2%", 97.2), bad_q)["code"]
          == "NOT_DERIVABLE_FROM_QUOTE")

    t("★台帳が C2 を FAIL と書いていればその出典は数えない",
      not semantic_artifact(
          _c([_src("https://chonborista.com/a", Q1),
              _src("https://nana-press.com/b", Q2, others="FAIL")]),
          VK)["verified"])
    t("★★同じ運営元の別ホストは1票（www有無で水増しできない）★★",
      not semantic_artifact(
          _c([_src("https://slopachi-quest.com/a", Q1),
              _src("https://www.slopachi-quest.com/b", Q2)]), VK)["verified"])
    t("★意味の検証器が無い項目は VERIFIED にしない（既定拒否）",
      semantic_artifact({**ok2, "field_key": "ceiling.normal"}, VK)["reason"]
      == "NO_SEMANTIC_CHECKER")
    t("★引用の内容が同じでも、出典が0件なら VERIFIED にしない",
      not semantic_artifact(_c([]), VK)["verified"])

    ng = [n for n, ok in results if not ok]
    print("")
    print(f"{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

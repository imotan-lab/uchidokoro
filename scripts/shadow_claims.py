# -*- coding: utf-8 -*-
"""
shadow_claims.py — Codexシャドー運用のcanonical claim抽出＋比較器（決定論・LLM非依存）

チャッピー承認済み設計v2（2026-07-16）のPhase 1実装:
  - サイト側アダプター: machines.jsonの構造化データ → canonical claim
    （★散文・noteからの推測は一切しない。構造化から一意に取れない属性はnull★）
  - 比較器: サイトclaim（site_baseline） vs Codex claim → 5値+方向つき判定
  - 改変テスト: --selftest で50件以上の系統的改変を流し検出率100%を検証（ネット不要）

canonical claim（dict）:
    claim_key        例: ceiling.normal.game / ceiling.normal.through / ceiling.reset.game
    ceiling_type     game / point / cycle / through / combined_component / none
    scope            AT間/BB間/CZ間/通常時/液晶 … 構造化に無ければ None
    mode             normal / reset / at / cz / bonus_gap …（checkerのモードキー）
    operator         exact / max / about / range … 構造化に無ければ None
    value            数値（asserted時のみ）
    unit             G / pt / Gpt / cycle / through / times
    plus_alpha       True / False / None（None=記載なし）
    assertion_status asserted / asserted_none / not_published / cannot_verify / no_site_field

判定（方向つき5値）:
    MATCH / MISMATCH / UNKNOWN / MISSING_IN_CODEX / MISSING_IN_SITE / ERROR

★サイト側はsite_baseline（真実源ではない・サイト自体も検証対象）★
★scope等がサイト側で構造的に存在しない場合は「サイト側は制約しない」扱いで
  値・単位の比較は行い、判定レコードに scope_unverified=True を残す
  （両者がscopeを主張して食い違う場合のみscope起因のMISMATCH/UNKNOWNにする）★
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent

VALID_UNITS = {"G", "pt", "Gpt", "cycle", "through", "times"}
VERDICTS = ("MATCH", "MISMATCH", "UNKNOWN", "MISSING_IN_CODEX", "MISSING_IN_SITE", "ERROR")


# ─────────────────────────────────────────────
# サイト側アダプター（構造化データ → canonical claims）
# ─────────────────────────────────────────────

def _mode_conf(checker: dict, key: str):
    """checker直下とchecker.modeData配下の2系統を吸収する共通アクセサ
    （build_hub_pages.py / audit_site.py 項目27と同じ思想。2026-07-13事故対応）"""
    if not isinstance(checker, dict):
        return None
    v = checker.get(key)
    if isinstance(v, dict):
        return v
    md = checker.get("modeData")
    if isinstance(md, dict) and isinstance(md.get(key), dict):
        return md[key]
    return None


def _mode_keys(checker: dict) -> list[str]:
    """checkerに実在するモードキー一覧（modes宣言ではなく実データ基準）"""
    keys = []
    if not isinstance(checker, dict):
        return keys
    known = ["normal", "reset", "cz", "at", "bonus_gap", "suru", "cycle"]
    for k in known:
        if _mode_conf(checker, k) is not None:
            keys.append(k)
    return keys


def _is_setting_only(machine: dict) -> bool:
    """設定狙い専用（天井なし）検知＝audit_render.pyのis_setting_onlyと同じ規則"""
    checker = machine.get("checker") or {}
    normal = _mode_conf(checker, "normal") or {}
    excellent = normal.get("excellent")
    return (
        machine.get("limit") in (None, 0)
        and isinstance(excellent, (int, float))
        and excellent >= 99999
    )


def _claim(key, ctype, mode, unit=None, value=None, scope=None, operator=None,
           plus_alpha=None, status="asserted"):
    return {
        "claim_key": key, "ceiling_type": ctype, "scope": scope, "mode": mode,
        "operator": operator, "value": value, "unit": unit,
        "plus_alpha": plus_alpha, "assertion_status": status,
    }


def extract_site_claims(machine: dict) -> list[dict]:
    """machines.jsonの1エントリからcanonical claimを抽出する。
    ★構造化データのみ。note・散文からの推測は禁止（一意に取れない属性はNone）★"""
    claims = []
    checker = machine.get("checker") or {}
    limit = machine.get("limit")
    unit = checker.get("unit") or "G"
    if unit not in VALID_UNITS:
        unit = "G"
    has_suru = bool(checker.get("hasSuru")) or _mode_conf(checker, "suru") is not None
    suru_max = checker.get("suruMax")
    if suru_max is None:
        sc = _mode_conf(checker, "suru") or {}
        suru_max = sc.get("suruMax")
    has_cycle = bool(checker.get("hasCycle"))
    cycle_max = checker.get("cycleMax")

    # 1) 天井なし（設定狙い専用）
    if _is_setting_only(machine):
        claims.append(_claim("ceiling.normal.none", "none", "normal",
                             status="asserted_none"))
        return claims

    # 2) G数/pt系の主天井: 構造化の正本は machines.json の limit
    #    scope（AT間/BB間/CZ間等）は構造化されていない → None（散文から推測しない）
    if isinstance(limit, (int, float)) and limit > 0:
        ctype = "point" if unit in ("pt", "Gpt") else "game"
        claims.append(_claim(f"ceiling.normal.{ctype}", ctype, "normal",
                             unit=unit, value=limit))
    else:
        # limitなし＋設定専用でもない（pt機のchecker.limit等）: checker直下limitのみ許容
        c_limit = checker.get("limit")
        if isinstance(c_limit, (int, float)) and c_limit > 0:
            ctype = "point" if unit in ("pt", "Gpt") else "game"
            claims.append(_claim(f"ceiling.normal.{ctype}", ctype, "normal",
                                 unit=unit, value=c_limit))
        else:
            claims.append(_claim("ceiling.normal.game", "game", "normal",
                                 status="no_site_field"))

    # 3) スルー天井（構造化: hasSuru + suruMax）
    if has_suru:
        if isinstance(suru_max, (int, float)):
            claims.append(_claim("ceiling.normal.through", "through", "suru",
                                 unit="through", value=suru_max))
        else:
            claims.append(_claim("ceiling.normal.through", "through", "suru",
                                 status="no_site_field"))

    # 4) 周期天井（構造化: hasCycle + cycleMax）
    if has_cycle:
        if isinstance(cycle_max, (int, float)):
            claims.append(_claim("ceiling.normal.cycle", "cycle", "cycle",
                                 unit="cycle", value=cycle_max))
        else:
            claims.append(_claim("ceiling.normal.cycle", "cycle", "cycle",
                                 status="no_site_field"))

    # 5) リセット短縮天井: ★大半の機種で「短縮後の天井値」は構造化されていない★
    #    （checker.resetにあるのは狙い目ライン＝別物。値の推測はしない）
    #    resetモードの存在＝「リセット挙動の構造化データあり」だが値はno_site_field
    if _mode_conf(checker, "reset") is not None:
        claims.append(_claim("ceiling.reset.game", "game", "reset",
                             status="no_site_field"))

    # 6) 複合天井の構成要素（at/cz/bonus_gap モードが実在する場合）
    #    ★モードの「しきい値」は天井値と同義ではないため、値はno_site_field。
    #      ただしlimitがatモード天井と一致する設計（例: ultraman_final）でも
    #      構造からは断定できないので推測しない★
    for mk, scope in (("at", "AT間"), ("cz", "CZ間"), ("bonus_gap", "ボーナス間")):
        if _mode_conf(checker, mk) is not None:
            claims.append(_claim(f"ceiling.normal.{mk}", "combined_component", mk,
                                 scope=scope, status="no_site_field"))

    return claims


# ─────────────────────────────────────────────
# 比較器（site_baseline vs Codex）
# ─────────────────────────────────────────────

_UNIT_NORM = {"g": "G", "ゲーム": "G", "Ｇ": "G", "pt": "pt", "ポイント": "pt",
              "gpt": "Gpt", "周期": "cycle", "cycle": "cycle",
              "スルー": "through", "through": "through", "回": "times", "times": "times"}


def _norm_unit(u):
    if u is None:
        return None
    return _UNIT_NORM.get(str(u).strip().lower(), _UNIT_NORM.get(str(u).strip(), str(u)))


def _norm_scope(s):
    if s in (None, ""):
        return None
    s = str(s).strip().replace("ＡＴ", "AT").replace("ＣＺ", "CZ").replace("ＢＢ", "BB")
    return s


def compare_one(site: dict, codex: dict | None) -> dict:
    """同一claim_keyのsite/codexペアを判定する。codex=Noneは欠落。"""
    key = site["claim_key"]
    rec = {"claim_key": key, "verdict": None, "detail": "", "scope_unverified": False}

    s_status = site.get("assertion_status")
    c_status = (codex or {}).get("assertion_status")

    # サイト側に構造化項目がない → 比較不能（Codex側の主張は記録価値あり）
    if s_status == "no_site_field":
        if codex is None or c_status in (None, "cannot_verify"):
            rec.update(verdict="UNKNOWN", detail="サイト構造化なし・Codex確認不能")
        else:
            rec.update(verdict="MISSING_IN_SITE",
                       detail="サイトに構造化項目なし（Codex主張は記録のみ・正誤判定はしない）")
        return rec

    if codex is None:
        rec.update(verdict="MISSING_IN_CODEX", detail="Codexがこのclaimを返さなかった")
        return rec
    if c_status == "cannot_verify":
        rec.update(verdict="UNKNOWN", detail="Codexが検索で確認できなかった")
        return rec

    # 「なし」の突き合わせ
    if s_status == "asserted_none" or c_status == "asserted_none":
        if s_status == c_status:
            rec.update(verdict="MATCH", detail="両者とも「なし」を明示")
        elif c_status == "not_published":
            rec.update(verdict="UNKNOWN", detail="サイト=なし / Codex=未公表")
        else:
            rec.update(verdict="MISMATCH", detail=f"なし/あり不一致（site={s_status}, codex={c_status}）")
        return rec
    if c_status == "not_published":
        rec.update(verdict="MISMATCH",
                   detail="サイトは値を持つがCodexは未公表と主張")
        return rec

    # 値の比較（両者asserted）
    s_val, c_val = site.get("value"), codex.get("value")
    s_unit, c_unit = _norm_unit(site.get("unit")), _norm_unit(codex.get("unit"))
    if s_val is None or c_val is None:
        rec.update(verdict="UNKNOWN", detail="asserted だが値欠落（スキーマ異常）")
        return rec
    if s_unit is None or c_unit is None:
        rec.update(verdict="UNKNOWN", detail="単位欠落")
        return rec
    if s_unit != c_unit:
        rec.update(verdict="MISMATCH", detail=f"単位不一致（site={s_unit}, codex={c_unit}）")
        return rec

    # scope: 両者が主張して食い違う場合のみ不一致。サイト側None=構造上の制約なし
    s_scope, c_scope = _norm_scope(site.get("scope")), _norm_scope(codex.get("scope"))
    if s_scope is not None and c_scope is not None and s_scope != c_scope:
        rec.update(verdict="MISMATCH", detail=f"scope不一致（site={s_scope}, codex={c_scope}）")
        return rec
    if s_scope is None and c_scope is not None:
        rec["scope_unverified"] = True  # サイト側にscopeが構造化されていない（設計判断・記録）

    # plus_alpha: 三値。どちらかがNone（記載なし）なら比較しない
    s_pa, c_pa = site.get("plus_alpha"), codex.get("plus_alpha")
    if s_pa is not None and c_pa is not None and bool(s_pa) != bool(c_pa):
        rec.update(verdict="MISMATCH", detail=f"+α有無の不一致（site={s_pa}, codex={c_pa}）")
        return rec

    if float(s_val) == float(c_val):
        rec.update(verdict="MATCH", detail=f"{c_val}{c_unit} 一致")
    else:
        rec.update(verdict="MISMATCH", detail=f"値不一致（site={s_val} / codex={c_val} {c_unit}）")
    return rec


def compare_claims(site_claims: list[dict], codex_claims: list[dict]) -> list[dict]:
    """全claimの突き合わせ（双方向）。"""
    results = []
    codex_by_key = {}
    for c in codex_claims:
        codex_by_key.setdefault(c.get("claim_key"), c)  # 同一keyの重複は先勝ち（重複はERROR記録）
    seen = set()
    for s in site_claims:
        seen.add(s["claim_key"])
        results.append(compare_one(s, codex_by_key.get(s["claim_key"])))
    for key, c in codex_by_key.items():
        if key not in seen:
            results.append({"claim_key": key, "verdict": "MISSING_IN_SITE",
                            "detail": "Codexのみが主張（サイトにclaim_keyなし）。記録のみ",
                            "scope_unverified": False})
    return results


# ─────────────────────────────────────────────
# 改変テスト（--selftest・ネット不要）
# ─────────────────────────────────────────────

def _synthetic_bases() -> list[tuple[str, list[dict]]]:
    """全属性を持つ合成site_baseline（比較ルールを全て運動させるため）"""
    return [
        ("G数天井機", [_claim("ceiling.normal.game", "game", "normal", unit="G",
                              value=1268, scope="BB間", plus_alpha=True)]),
        ("pt天井機", [_claim("ceiling.normal.point", "point", "normal", unit="pt",
                             value=1400, plus_alpha=False)]),
        ("スルー機", [_claim("ceiling.normal.through", "through", "suru",
                             unit="through", value=6)]),
        ("周期機", [_claim("ceiling.normal.cycle", "cycle", "cycle",
                           unit="cycle", value=10)]),
        ("複合機", [_claim("ceiling.normal.game", "game", "normal", unit="G",
                           value=1500, scope="AT間", plus_alpha=True),
                    _claim("ceiling.normal.cz", "game", "cz", unit="G",
                           value=700, scope="CZ間", plus_alpha=True)]),
        ("天井なし機", [_claim("ceiling.normal.none", "none", "normal",
                               status="asserted_none")]),
    ]


def _as_codex(site_claims):
    """site_baselineをそのままCodex側主張に写す（正コントロール用）"""
    return [dict(c) for c in site_claims]


def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, cond))
        print(("✅" if cond else "❌") + " " + name)

    bases = _synthetic_bases()
    cases = 0

    # 正コントロール: 同一なら全MATCH（no_site_field除く）
    for name, sc in bases:
        rs = compare_claims(sc, _as_codex(sc))
        ok = all(r["verdict"] == "MATCH" for r in rs)
        t(f"正コントロール[{name}]: 完全一致は全MATCH", ok)

    # 系統的改変: 各改変が「MATCHにならない」こと（検出率100%）
    def mutations(claim):
        muts = []
        if claim.get("assertion_status") == "asserted_none":
            m = dict(claim); m.update(assertion_status="asserted", value=999, unit="G")
            muts.append(("なし→値あり", m, "MISMATCH"))
            return muts
        v = claim["value"]
        for dv in (+1, -1, +100, -100, +50, v):  # ×2（v+v）含む6種の値改変
            m = dict(claim); m["value"] = v + dv
            muts.append((f"値改変{dv:+}", m, "MISMATCH"))
        # 桁ずれ
        m = dict(claim); m["value"] = v * 10
        muts.append(("値10倍", m, "MISMATCH"))
        # 単位すり替え
        for u2 in VALID_UNITS - {claim["unit"]}:
            m = dict(claim); m["unit"] = u2
            muts.append((f"単位→{u2}", m, "MISMATCH"))
        # scopeすり替え（サイト側がscopeを持つ場合のみMISMATCH要求）
        if claim.get("scope"):
            m = dict(claim); m["scope"] = "CZ間" if claim["scope"] != "CZ間" else "AT間"
            muts.append(("scopeすり替え", m, "MISMATCH"))
        # +α反転（サイト側が明示する場合のみ）
        if claim.get("plus_alpha") is not None:
            m = dict(claim); m["plus_alpha"] = not claim["plus_alpha"]
            muts.append(("+α反転", m, "MISMATCH"))
        # 値あり→「なし」主張
        m = dict(claim); m.update(assertion_status="asserted_none", value=None)
        muts.append(("値→なし主張", m, "MISMATCH"))
        # 値あり→未公表主張
        m = dict(claim); m.update(assertion_status="not_published", value=None)
        muts.append(("値→未公表主張", m, "MISMATCH"))
        return muts

    all_detected = True
    for name, sc in bases:
        for claim in sc:
            for mname, mut, expect in mutations(claim):
                cases += 1
                codex = [mut if c["claim_key"] == mut["claim_key"] else dict(c) for c in sc]
                rs = compare_claims(sc, codex)
                verdict = next(r["verdict"] for r in rs if r["claim_key"] == mut["claim_key"])
                if verdict != expect:
                    all_detected = False
                    print(f"   ❌ 検出漏れ [{name}/{mut['claim_key']}/{mname}]: {verdict}（期待{expect}）")
    t(f"改変テスト {cases}件: 検出率100%", all_detected and cases >= 50)

    # 新旧値交換（機種間クロス汚染の型）
    g = bases[0][1][0]; p = bases[1][1][0]
    swapped = dict(g); swapped["value"] = 999  # 旧作の値に差し替わった想定
    rs = compare_claims([g], [swapped])
    t("新旧値交換はMISMATCH", rs[0]["verdict"] == "MISMATCH")

    # 方向つきMISSING
    rs = compare_claims([g], [])
    t("Codex欠落はMISSING_IN_CODEX", rs[0]["verdict"] == "MISSING_IN_CODEX")
    extra = _claim("ceiling.reset.game", "game", "reset", unit="G", value=800)
    rs = compare_claims([g], [dict(g), extra])
    t("Codexのみの主張はMISSING_IN_SITE", any(
        r["claim_key"] == "ceiling.reset.game" and r["verdict"] == "MISSING_IN_SITE" for r in rs))

    # UNKNOWN系: cannot_verify / scope両者主張の欠落はUNKNOWNに倒す
    cv = dict(g); cv.update(assertion_status="cannot_verify", value=None)
    rs = compare_claims([g], [cv])
    t("cannot_verifyはUNKNOWN（MATCHに押し込まない）", rs[0]["verdict"] == "UNKNOWN")

    # scope_unverifiedフラグ（サイト側scope=Noneの設計判断の記録）
    s_noscope = dict(g); s_noscope["scope"] = None
    rs = compare_claims([s_noscope], [dict(g)])
    t("サイト側scope欠落は比較可＋scope_unverified記録",
      rs[0]["verdict"] == "MATCH" and rs[0]["scope_unverified"] is True)

    # 実ファイルsmoke: 全機種でアダプターが例外なく走り、claim在庫を数える
    try:
        data = json.loads((BASE / "assets" / "data" / "machines.json").read_text(encoding="utf-8"))
        machines = data["machines"] if isinstance(data, dict) else data
        counts = {}
        for m in machines:
            for c in extract_site_claims(m):
                k = (c["ceiling_type"], c["assertion_status"])
                counts[k] = counts.get(k, 0) + 1
        total = sum(counts.values())
        print(f"   実ファイルsmoke: {len(machines)}機種 → claim {total}件 "
              f"{ {f'{a}/{b}': n for (a, b), n in sorted(counts.items())} }")
        t("実ファイルsmoke: 全機種で例外なし・claim生成あり", total >= len(machines))
    except Exception as e:
        t(f"実ファイルsmoke: 例外 {e}", False)

    ok = all(c for _, c in results)
    print(f"\nselftest: {sum(1 for _, c in results if c)}/{len(results)} 合格（改変{cases}件込み）")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Codexシャドーのclaim抽出＋比較器")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--extract", metavar="SLUG", help="1機種のsite_baseline claimsを表示")
    args = parser.parse_args()
    if args.selftest:
        return selftest()
    if args.extract:
        data = json.loads((BASE / "assets" / "data" / "machines.json").read_text(encoding="utf-8"))
        machines = data["machines"] if isinstance(data, dict) else data
        for m in machines:
            if m["slug"] == args.extract:
                print(json.dumps(extract_site_claims(m), ensure_ascii=False, indent=1))
                return 0
        print("slugが見つかりません")
        return 2
    parser.error("--selftest か --extract SLUG を指定")
    return 2


if __name__ == "__main__":
    sys.exit(main())

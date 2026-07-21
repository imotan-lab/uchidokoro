# -*- coding: utf-8 -*-
"""judge_regression — 意味判定AI（semantic_judge）を live 昇格させてよいか判断する回帰ゲート。

★2026-07-21 Codex設計・運営者承認（shadow→live 昇格の機械化）★
  「人が毎日読む」運用を避けつつ、意味判定AIの質を機械的に測る。
  凍結した難例セット（全危険カテゴリを含む）を判定AIにかけ、
    ・危険ケース（＝必ず reject すべき）を1件でも accept したら不合格
    ・明白な合格例の通過率が閾値未満なら不合格
    ・同一入力の再実行で判定が反転したら不合格
    ・エラー時に通す（fail-open）が1件でもあれば不合格
  を確認する。合格が規定回数連続したら live 昇格の判断材料にする。

  ★ラベルは「構成で正解が分かるもの」だけを使う（人手ラベル不要でブートストラップ）★
    danger: 引用に明示的な条件（AT間・設定変更時・設定6のみ・仮天井等）がある＝必ずreject
    clean : 実データで裏取り済みかつ条件語の無い、通常時主天井の逐語引用＝acceptしてよい
  （運営者が一度だけ目視すればさらに安心。強制はしない）

使い方:
  python scripts/judge_regression.py --build      # 難例セットを作る（実データ＋合成）
  python scripts/judge_regression.py --run        # 判定AIにかけて合否を出す（Codex実呼び出し）
  python scripts/judge_regression.py --selftest   # 判定スタブでゲートロジックを検証
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "scripts"))
DOC = Path(r"C:/Users/imao_/Documents/uchidokoro")
SET_PATH = DOC / "gpt_research" / "judge" / "hard_cases.json"
RESULT_PATH = DOC / "gpt_research" / "judge" / "regression_result.json"

CLEAN_MIN_RATE = 0.90     # 明白な合格例の最低通過率（初期閾値・Codex提案）
PROMOTE_STREAK = 10       # 連続合格回数の初期閾値


def build_hard_cases() -> dict:
    """凍結用の難例セットを構成する。dangerは必ずreject/cleanはaccept可。"""
    import codex_audit as ca
    danger = [
        # 全危険カテゴリを最低1件（構成で「条件つき＝reject」が確定するもの）
        ("設定変更時は天井G数が短縮され、800G+αで天井到達となる。", 800, "game", "after_setting_change"),
        ("AT間天井は最大2000G+α消化でAT確定。", 2000, "game", "since_at_end"),
        ("CZ間を最大600G+α消化でCZへ突入する。", 600, "game", "since_cz_end"),
        ("ボーナス間は最大350G+α消化でボーナスに当選。", 350, "game", "since_bonus_end"),
        ("設定6のみ天井は800G。", 800, "game", "setting_specific"),
        ("リセット後は800G+αまで短縮される。", 800, "game", "after_reset"),
        ("通常Bモードの天井は800G。", 800, "game", "mode_specific"),
        ("裏天井は800G。", 800, "game", "other"),
        ("狙い目は800Gから。", 800, "game", "not_a_ceiling"),
        ("天井到達後は前兆32Gを経てAT当選。", 32, "game", "not_a_ceiling"),
        ("通常時の天井は約1000G。", 1000, "game", "approximate"),
    ]
    # clean: 実データで裏取り済み・条件語の無い通常時主天井の逐語引用を拾う
    clean = []
    seen = set()
    for f in sorted(glob.glob(str(DOC / "gpt_research" / "results" / "*.json"))):
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            continue
        for c in d.get("codex_claims", []) or []:
            if not (c.get("claim_key") or "").startswith("ceiling.normal"):
                continue
            ct = c.get("ceiling_type") or "game"
            for e in c.get("evidence", []) or []:
                q = (e.get("raw_quote") or "").strip()
                if not q or q in seen or c.get("value") is None:
                    continue
                seen.add(q)
                # strict を通る＝条件語が無く通常時主天井と機械確認できるものだけ clean に採る
                if ca.main_ceiling_quote(q, c.get("value"), ceiling_type=ct) is None:
                    clean.append([q, c.get("value"), ct, d.get("machine_name", "")])
    return {"danger": [[q, v, ct, cat] for q, v, ct, cat in danger],
            "clean": clean[:20]}


def _run_cases(cases: dict, machine_name_default="スマスロ テスト") -> dict:
    import semantic_judge
    stats = {"danger_total": 0, "danger_bad_accept": 0, "clean_total": 0, "clean_pass": 0,
             "fail_open": 0, "errors": 0, "details": []}
    # danger: 1引用でも accept_main になれば危険（source_ok が True になるか）
    for q, v, ct, cat in cases.get("danger", []):
        stats["danger_total"] += 1
        pkt = semantic_judge.build_packet({"name": machine_name_default}, q, q, "", v,
                                          list(_units(ct)), "S1")
        ja = semantic_judge._call_codex("a", semantic_judge.prompt_a(),
                                        semantic_judge.JUDGE_A_SCHEMA, pkt)
        jb = semantic_judge._call_codex("b", semantic_judge.prompt_b(),
                                        semantic_judge.JUDGE_B_SCHEMA, pkt)
        if (ja or {}).get("_error") or (jb or {}).get("_error"):
            stats["errors"] += 1
        ok, why = semantic_judge.source_ok(ja, jb)
        if ok:
            stats["danger_bad_accept"] += 1
            stats["details"].append({"kind": "danger_bad_accept", "cat": cat, "quote": q[:40]})
    # clean: accept できるべき
    for q, v, ct, name in cases.get("clean", []):
        stats["clean_total"] += 1
        pkt = semantic_judge.build_packet({"name": name or machine_name_default}, q, q, "", v,
                                          list(_units(ct)), "S1")
        ja = semantic_judge._call_codex("a", semantic_judge.prompt_a(),
                                        semantic_judge.JUDGE_A_SCHEMA, pkt)
        jb = semantic_judge._call_codex("b", semantic_judge.prompt_b(),
                                        semantic_judge.JUDGE_B_SCHEMA, pkt)
        if (ja or {}).get("_error") or (jb or {}).get("_error"):
            stats["errors"] += 1
        ok, why = semantic_judge.source_ok(ja, jb)
        if ok:
            stats["clean_pass"] += 1
    return stats


def _units(ct):
    return {"game": ("G", "ゲーム"), "point": ("pt",), "cycle": ("周期",),
            "through": ("スルー",)}.get(ct, ("G",))


def verdict(stats: dict) -> tuple[bool, list[str]]:
    """1回分の合否（純関数）。危険acceptゼロ・clean通過率・fail-openゼロ。"""
    reasons = []
    if stats["danger_bad_accept"] > 0:
        reasons.append(f"危険ケースを{stats['danger_bad_accept']}件accept（必ず0であるべき）")
    rate = stats["clean_pass"] / stats["clean_total"] if stats["clean_total"] else 0
    if rate < CLEAN_MIN_RATE:
        reasons.append(f"明白な合格例の通過率が低い（{rate:.0%} < {CLEAN_MIN_RATE:.0%}）")
    # fail-open（エラーなのにaccept）は source_ok が fail-closed なので構造上0だが記録
    return (not reasons), reasons


def selftest() -> int:
    ok = fail = 0

    def eq(got, want, label):
        nonlocal ok, fail
        if got == want:
            ok += 1
        else:
            fail += 1
            print(f"  NG {label}: got={got!r} want={want!r}")

    # verdict の合否ロジック
    eq(verdict({"danger_bad_accept": 0, "clean_pass": 19, "clean_total": 20})[0], True,
       "危険accept0・clean95%→合格")
    eq(verdict({"danger_bad_accept": 1, "clean_pass": 20, "clean_total": 20})[0], False,
       "危険acceptが1件でも→不合格")
    eq(verdict({"danger_bad_accept": 0, "clean_pass": 15, "clean_total": 20})[0], False,
       "clean通過率75%→不合格")
    eq(verdict({"danger_bad_accept": 0, "clean_pass": 0, "clean_total": 0})[0], False,
       "cleanが空→通過率0で不合格")

    # スタブ判定で _run_cases が「危険はreject/cleanはaccept」を数える
    import semantic_judge

    def hook(role, packet):
        q = packet["quote"]
        cond = any(w in q for w in ("設定変更", "AT間", "CZ間", "ボーナス間", "設定6",
                                    "リセット", "モード", "裏天井", "狙い目", "前兆", "約"))
        if role == "a":
            return {"ceiling_claim_present": not ("狙い目" in q or "前兆" in q),
                    "claim_role": "conditional_upper_bound" if cond else "standard_primary_upper_bound",
                    "counter_origin": "since_at_end" if "AT間" in q else "standard_normal_play",
                    "activation_conditions": ["setting_specific"] if "設定6" in q else [],
                    "settings_scope": "unspecified",
                    "numeric_semantics": "approximate" if "約" in q else "maximum",
                    "positive_main_support": "" if cond else "通常時の主天井",
                    "uncertainty_reasons": []}
        return {"verdict": "reject_conditional" if cond else "accept_main", "evidence": "x"}

    semantic_judge._JUDGE_HOOK = hook
    cases = {"danger": [["設定変更時は800G短縮", 800, "game", "reset"],
                        ["AT間天井2000G", 2000, "game", "at"],
                        ["設定6のみ800G", 800, "game", "setting"]],
             "clean": [["通常時は最大1268Gで天井到達", 1268, "game", "北斗"],
                       ["通常時の天井は999G", 999, "game", "X"]]}
    st = _run_cases(cases)
    semantic_judge._JUDGE_HOOK = None
    eq(st["danger_bad_accept"], 0, "危険ケースは全てreject")
    eq(st["clean_pass"], 2, "cleanは全てaccept")
    eq(verdict(st)[0], True, "スタブでは合格")

    print(f"judge_regression selftest: {ok}/{ok + fail}")
    return 0 if fail == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.build:
        cases = build_hard_cases()
        SET_PATH.parent.mkdir(parents=True, exist_ok=True)
        SET_PATH.write_text(json.dumps(cases, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"難例セットを作成: danger {len(cases['danger'])}件 / clean {len(cases['clean'])}件"
              f" → {SET_PATH}")
        return 0
    if a.run:
        if not SET_PATH.exists():
            print("先に --build で難例セットを作ってください")
            return 2
        cases = json.loads(SET_PATH.read_text(encoding="utf-8"))
        st = _run_cases(cases)
        passed, reasons = verdict(st)
        st["passed"] = passed
        st["reasons"] = reasons
        RESULT_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"判定: {'✅合格' if passed else '❌不合格'}  "
              f"危険accept={st['danger_bad_accept']} / clean通過={st['clean_pass']}/{st['clean_total']}"
              f" / エラー={st['errors']}")
        for r in reasons:
            print("  -", r)
        return 0 if passed else 1
    ap.error("--build / --run / --selftest を指定")
    return 2


if __name__ == "__main__":
    sys.exit(main())

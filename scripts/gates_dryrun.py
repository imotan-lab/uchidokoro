#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""gates_dryrun.py — 現行データに gates.py を当てたらどうなるかを試算する（読み取り専用・一切書き込まない）。

移行前に「何がどれだけ閉じるか」「分類台帳の作業量はどれくらいか」を数字で把握するための道具。
lifecycle はまだ machines.json に無いので、暫定割当（complete→LEGACY_SEARCH / preview→VERIFIED_PREVIEW）
を**メモリ上だけ**で行う。ファイルは書き換えない。

実行: python scripts/gates_dryrun.py
"""
import json
import os
from collections import Counter

import gates

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "assets", "data")


def provisional_lifecycle(m: dict) -> str:
    """移行の暫定案（Phase 1）。現状 index されている＝LEGACY_SEARCH（"検証済み"ではない）。"""
    return "VERIFIED_PREVIEW" if m.get("status") == "preview" else "LEGACY_SEARCH"


def provisional_checker_modes(m: dict) -> dict:
    """現行データからの暫定 checker_modes。
    ★重要★ 現行 modes 配列にある＝「動く」であって「数値が裏取り済み」ではない（Codex指摘）。
    よって **UNVERIFIED**（=非表示）を既定にし、Phase 0で停止した mode は DISABLED と明示する。
    VERIFIED へ上げるのは裏取り後（Phase 2）。
    """
    c = m.get("checker") or {}
    out = {}
    for k, v in c.items():
        if isinstance(v, dict) and k not in ("modeData", "byRate"):
            out[k] = "DISABLED" if "_disabled" in v else "UNVERIFIED"
    md = c.get("modeData")
    if isinstance(md, dict):
        for k, v in md.items():
            if isinstance(v, dict):
                out.setdefault(k, "DISABLED" if "_disabled" in v else "UNVERIFIED")
    return out


# 識別子は置き換えない（置き換えるとmode照合が壊れ、検査自体が誤検知する）
_IDENTIFIER_KEYS = ("key", "defaultRate", "unit")


def _neutralize(node):
    """散文だけを無害な文字列に置き換える（スキーマ検査用・キー構造と識別子は保つ）。"""
    if isinstance(node, str):
        return "安全なテキスト"
    if isinstance(node, list):
        return [_neutralize(x) for x in node]
    if isinstance(node, dict):
        return {k: (v if (k in _IDENTIFIER_KEYS and isinstance(v, str)) else _neutralize(v))
                for k, v in node.items()}
    return node


def schema_coverage(machines: list) -> None:
    """許可スキーマの取りこぼし検査。

    ★なぜ要るか★ 射影は許可リスト方式なので、実データにあるのにスキーマへ書き忘れた
    フィールドは「黙って消える」。過去に exchangeRates/defaultRate/target を書き忘れ、
    51機種の交換率切替が壊れる寸前だった。全modeをVERIFIEDと仮定して射影し、
    元データにあるキーが残るかを毎回突き合わせる。
    """
    missing_top, missing_mode = set(), set()
    for m in machines:
        c = m.get("checker") or {}
        if not isinstance(c, dict):
            continue
        # 全modeを VERIFIED と仮定（スキーマの網羅性だけを見る）。
        # 本文の分類状況に左右されないよう、checker の射影だけを直接呼ぶ。
        modes = {k for k, v in c.items() if isinstance(v, dict) and k not in ("modeData", "byRate")}
        md = c.get("modeData")
        if isinstance(md, dict):
            modes |= {k for k, v in md.items() if isinstance(v, dict)}
        # ★文章の分類による削除と、スキーマの書き忘れを混同しないため、
        #   全ての文字列を無害な文字列に置き換えてから射影する（残るのはスキーマ由来の欠落だけ）。
        ctx = gates._Ctx("legacy_safe", None)
        out = gates._project_checker(_neutralize(c), sorted(modes), ctx) or {}
        for k, v in c.items():
            if k in ("modeData",) or k in modes:
                continue
            if k not in out:
                missing_top.add(k)
        for mk in modes:
            conf = c.get(mk) if isinstance(c.get(mk), dict) else (md or {}).get(mk, {})
            got = out.get(mk) or {}
            for k in conf:
                if k == "_disabled":      # 意図的に落とす（Phase 0の停止マーカー）
                    continue
                if k not in got:
                    missing_mode.add(k)

    print("■ 許可スキーマの取りこぼし検査（実データにあるのに公開射影で消えるキー）")
    print(f"   checker直下: {sorted(missing_top) or 'なし'}")
    print(f"   mode配下   : {sorted(missing_mode) or 'なし'}")
    if missing_top or missing_mode:
        print("   ⚠ 上記は配線すると機能が壊れる可能性がある。gates.py の許可スキーマに追加するか、"
              "意図的に落とすなら理由をコメントすること。")
    print("-" * 64)


def main() -> int:
    machines = json.load(open(os.path.join(DATA, "machines.json"), encoding="utf-8"))

    gate_counts = Counter()
    hub_counts = Counter()
    unclassified_total = 0
    unclassified_machines = 0
    dropped_total = 0
    unique_ids = set()
    err_count = 0

    for m in machines:
        sim = dict(m)
        sim["lifecycle"] = provisional_lifecycle(m)
        sim["checker_modes"] = provisional_checker_modes(m)

        errs = gates.validate_machine(sim)
        err_count += len(errs)

        g = gates.compute_gates(sim)
        for key in ("public", "index", "ads", "checker", "affiliate"):
            if g[key]:
                gate_counts[key] += 1
        hub_counts[g["hub"]] += 1

        dp = os.path.join(DATA, "machine-details", f"{m['slug']}.json")
        detail = json.load(open(dp, encoding="utf-8")) if os.path.isfile(dp) else {}

        # ★原文を持ち出さない診断API（audit_view）を使う★
        a = gates.audit_view(sim, detail, ledger=None)
        u, d = a["unclassified"], a["dropped"]
        dropped_total += len(d)
        if u:
            unclassified_machines += 1
            unclassified_total += len(u)
            unique_ids.update(x["atom_id"] for x in u if x.get("atom_id"))

    print("=" * 64)
    print(f"機種数: {len(machines)}    スキーマ検証エラー: {err_count} 件")
    print("-" * 64)
    schema_coverage(machines)
    print("■ ゲートが開く機種数（暫定移行案の場合）")
    for key in ("public", "index", "ads", "checker", "affiliate"):
        print(f"   {key:<10} : {gate_counts[key]:>3} / {len(machines)}")
    print(f"   hub        : " + " / ".join(f"{k}={v}" for k, v in sorted(hub_counts.items())))
    print("-" * 64)
    print("■ 分類台帳の作業量（未分類のまま公開しようとすると全部止まる）")
    print(f"   未分類の文字列   : {unclassified_total} 箇所")
    print(f"   ユニーク文（重複除く）: {len(unique_ids)} 文")
    print(f"   影響機種         : {unclassified_machines} / {len(machines)}")
    print(f"   自動DROP（絶対禁止・preview禁止話題等）: {dropped_total} 箇所")
    print("=" * 64)
    print("※ このスクリプトは一切ファイルを書き換えていません。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

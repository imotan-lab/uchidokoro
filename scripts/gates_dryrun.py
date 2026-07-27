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
import build_ledger as bl

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "assets", "data")


def provisional_lifecycle(m: dict) -> str:
    """暫定 lifecycle（単一情報源＝build_ledger.provisional に委譲）。"""
    return bl.provisional(m)["lifecycle"]


def provisional_checker_modes(m: dict) -> dict:
    """暫定 checker_modes（単一情報源＝build_ledger.provisional に委譲）。

    ★以前ここに独自実装があり、突き合わせ側と状態がズレて
      checkerが一度も検証されない穴になっていた（Codex 15巡目 #1）。★
    """
    return bl.provisional(m)["checker_modes"]


# 識別子・日付・slug は置き換えない
# （置き換えると照合や形式検査が壊れ、検査自体が誤検知する）
# label も保持する（無害化で全ラベルが同一文字列になると「重複」検査に誤って引っかかるため）
_IDENTIFIER_KEYS = ("key", "defaultRate", "unit", "slug", "release_date", "confirmed_at",
                    "lifecycle", "name", "type", "label")


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


def schema_coverage(machines: list) -> bool:
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
        # ★_disabled のmodeは「意図的に公開しない」ので検査対象から外す★
        #   （含めると gates 側が正しく拒否した結果を「取りこぼし」と誤報告してしまう）
        modes = {k for k, v in c.items()
                 if isinstance(v, dict) and k not in ("modeData", "byRate")
                 and "_disabled" not in v}
        md = c.get("modeData")
        if isinstance(md, dict):
            modes |= {k for k, v in md.items() if isinstance(v, dict) and "_disabled" not in v}
        # ★文章の分類による削除と、スキーマの書き忘れを混同しないため、
        #   全ての文字列を無害な文字列に置き換えてから射影する（残るのはスキーマ由来の欠落だけ）。
        if not modes:
            continue                      # 表示できるmodeが無い機種は検査対象外
        ctx = gates._Ctx("legacy_safe", None)
        out = gates._project_checker(_neutralize(c), sorted(modes), ctx, m)
        if ctx.errors:                    # ★continueの前に構造エラーを回収する★
            for e in ctx.errors:
                missing_top.add(f"(構造エラー) {e['reason']}")
        if out is None:
            # checkerごと出ない（天井なし機種＝判定の主軸が無い等）＝取りこぼしではない
            continue
        out.pop("_live_modes", None)
        # ★意図的に公開しないフィールド（UIが参照しない）は取りこぼしではない★
        INTENTIONAL = {"ok", "ng", "hasSuru", "hasCycle", "suruMax"}
        for k, v in c.items():
            if k in ("modeData",) or k in modes or k in INTENTIONAL:
                continue
            if isinstance(v, dict) and "_disabled" in v:
                continue                  # Phase 0で意図的に停止したmode
            if k not in out:
                missing_top.add(k)
        def _cmp(src, dst, where):
            """入れ子まで再帰的に突き合わせる（byRate.*・suru/cycleの各行・宣言の子まで）。"""
            if isinstance(src, dict):
                for k2, v2 in src.items():
                    if k2 == "_disabled":
                        continue
                    if not isinstance(dst, dict) or k2 not in dst:
                        missing_mode.add(f"{where}.{k2}" if where else k2)
                        continue
                    _cmp(v2, dst[k2], f"{where}.{k2}" if where else k2)
            elif isinstance(src, list) and isinstance(dst, list):
                # ★位置ではなくキーの有無で比較（要素が正当に落ちると位置がずれる）★
                dst_keysets = [set(v2.keys()) for v2 in dst if isinstance(v2, dict)]
                for v2 in src:
                    if not isinstance(v2, dict):
                        continue
                    want = {k3 for k3, v3 in v2.items()
                            if k3 != "_disabled" and v3 is not None and v3 != [] and v3 != {}}
                    if dst_keysets and not any(want <= ks for ks in dst_keysets):
                        best = max(dst_keysets, key=lambda ks: len(want & ks))
                        for k3 in want - best:
                            missing_mode.add(f"{where}[].{k3}")

        for mk in modes:
            if mk not in out:
                continue                  # そのmodeが出ない（内容・構造上の理由）
            conf = c.get(mk) if isinstance(c.get(mk), dict) else (md or {}).get(mk, {})
            _cmp(conf, out[mk], "")
        # modes宣言・交換率の子フィールドも突き合わせる（checkerが出た機種だけ）
        for top in ("modes", "exchangeRates") if out else ():
            if not isinstance(c.get(top), list):
                continue
            if top == "modes":
                # ★key で対応付けて比較（出力に無いmodeは内容除去で落ちたもの）★
                got = {x.get("key"): x for x in (out.get("modes") or []) if isinstance(x, dict)}
                for x in c["modes"]:
                    if not isinstance(x, dict) or x.get("key") not in got:
                        continue
                    for k3, v3 in x.items():
                        if v3 is not None and k3 not in got[x["key"]]:
                            missing_mode.add("modes[]." + k3)
            else:
                _cmp(c[top], out.get(top) or [], top)
        if ctx.errors:                    # 検査中に構造エラーが出たら黙って通さない
            for e in ctx.errors:
                missing_top.add(f"(構造エラー) {e['reason']}")

    # ★checker以外（機種一般フィールド・記事）も取りこぼしを検査する★
    #   許可リストから誤って外した時に「取りこぼし0」と表示してしまうのを防ぐ。
    PUBLIC_MACHINE = {"slug", "name", "manufacturer", "info", "strategy", "strategyByRate",
                      "aliases", "limit", "tenjo_display", "release_date", "confirmed_at",
                      "original", "checker", "sources", "seo"}
    PUBLIC_DETAIL = {"lead", "summaryBoxes", "factTable", "sections"}
    missing_other: set = set()
    for m in machines:
        sim = dict(m)
        sim["lifecycle"] = "LEGACY_SEARCH"
        sim["checker_modes"] = {}
        g = gates.compute_gates(sim)
        ctx = gates._Ctx("legacy_safe", None)
        pm = gates._project_machine(_neutralize(sim), g, ctx)
        # ★入れ子まで再帰的に突き合わせる（seo.description・sources[].title 等の欠落も拾う）★
        def _deep(src, dst, where):
            if src is None:
                return                    # null は「無し」の明示
            if isinstance(src, dict):
                if not isinstance(dst, dict):
                    missing_other.add(where); return
                for k2, v2 in src.items():
                    if k2 in dst:
                        _deep(v2, dst[k2], f"{where}.{k2}")
                    elif v2 is not None and v2 != [] and v2 != {}:
                        missing_other.add(f"{where}.{k2}")
            elif isinstance(src, list):
                if not isinstance(dst, list):
                    missing_other.add(where); return
                # ★位置ではなく「項目の有無」で比較する★
                #   射影で一部の要素が正当に落ちると位置がずれ、誤検知になるため。
                #   ここで見たいのは「スキーマから項目を書き忘れていないか」だけ。
                if not dst:
                    return
                # ★要素ごとに「その要素が持つ項目が、対応する出力要素にもあるか」を見る★
                #   配列全体で和集合を取ると、settei型だけが持つ tables/rows などが
                #   他の要素にも必要と誤解される（誤検知）。
                #   対応付けは type や識別子ではなく「同じ形の要素があるか」で緩く見る。
                dst_keysets = [set(v2.keys()) for v2 in dst if isinstance(v2, dict)]
                for v2 in src:
                    if not isinstance(v2, dict):
                        continue
                    # 内容除去で丸ごと落ちた要素は「取りこぼし」ではない
                    if not any(isinstance(d2, dict) and d2.get("title") == v2.get("title")
                               for d2 in dst) and "title" in v2:
                        continue
                    want = {k3 for k3, v3 in v2.items()
                            if v3 is not None and v3 != [] and v3 != {}}
                    if not any(want <= ks for ks in dst_keysets):
                        # どの出力要素もこの項目集合を満たさない＝射影で落ちた項目がある
                        best = max((len(want & ks) for ks in dst_keysets), default=0)
                        if best < len(want):
                            for k3 in want - (max(dst_keysets, key=lambda ks: len(want & ks))
                                              if dst_keysets else set()):
                                missing_other.add(f"{where}[].{k3}")
        for k in m:
            if k in ("checker",) or k not in PUBLIC_MACHINE:
                continue
            if m[k] is None:
                continue
            if k not in pm:
                missing_other.add(f"machine.{k}")
            else:
                _deep(m[k], pm[k], f"machine.{k}")
        dp = os.path.join(DATA, "machine-details", f"{m['slug']}.json")
        if os.path.isfile(dp):
            det = json.load(open(dp, encoding="utf-8"))
            pd_ = gates._project_detail(_neutralize(det), g, gates._Ctx("legacy_safe", None))
            for k in det:
                if k not in PUBLIC_DETAIL:
                    continue
                if k not in pd_:
                    missing_other.add(f"detail.{k}")
                else:
                    _deep(det[k], pd_[k], f"detail.{k}")
    print("■ 許可スキーマの取りこぼし検査（実データにあるのに公開射影で消えるキー）")
    print(f"   checker直下: {sorted(missing_top) or 'なし'}")
    print(f"   mode配下   : {sorted(missing_mode) or 'なし'}")
    print(f"   機種・記事 : {sorted(missing_other) or 'なし'}")
    if missing_other:
        missing_top |= missing_other
    if missing_top or missing_mode:
        print("   ⚠ 上記は配線すると機能が壊れる可能性がある。gates.py の許可スキーマに追加するか、"
              "意図的に落とすなら理由をコメントすること。")
    print("-" * 64)
    return not (missing_top or missing_mode)   # ★異常は終了コードへ反映させる★


def main() -> int:
    machines = json.load(open(os.path.join(DATA, "machines.json"), encoding="utf-8"))

    gate_counts = Counter()
    hub_counts = Counter()
    unclassified_total = 0
    unclassified_machines = 0
    dropped_total = 0
    unique_ids = set()
    err_count = 0
    struct_errors: list[dict] = []

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
        # ★構造エラーを必ず集計する（見落とすと「異常なし」と誤報告してしまう）★
        for e in a["errors"]:
            # 検証エラーは文字列、射影の構造エラーは辞書で来る（両方を受ける）
            struct_errors.append({"slug": m["slug"], **e} if isinstance(e, dict)
                                 else {"slug": m["slug"], "path": "-", "reason": str(e)})
        if u:
            unclassified_machines += 1
            unclassified_total += len(u)
            unique_ids.update(x["atom_id"] for x in u if x.get("atom_id"))

    print("=" * 64)
    print(f"機種数: {len(machines)}    スキーマ検証エラー: {err_count} 件")
    print("-" * 64)
    coverage_ok = schema_coverage(machines)
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
    print("-" * 64)
    from collections import Counter as _C
    print("■ 射影時の構造エラー（配線前に必ず0にする）")
    if struct_errors:
        for reason, n in _C(e["reason"] for e in struct_errors).most_common(8):
            print(f"   {n:>4} 件  {reason}")
        print(f"   影響機種: {len({e['slug'] for e in struct_errors})}")
    else:
        print("   なし")
    print("=" * 64)
    print("※ このスクリプトは一切ファイルを書き換えていません。")
    # ★異常があれば非0終了（CI/preflightに繋げられるように）★
    #   取りこぼし検査(schema_coverage)の異常も終了コードに含める
    return 1 if (err_count or struct_errors or not coverage_ok) else 0


if __name__ == "__main__":
    raise SystemExit(main())

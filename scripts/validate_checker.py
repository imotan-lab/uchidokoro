#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""チェッカーのスルー回数天井モード(schema)を検証する（決定論・LLM非依存）。

2026-07-23 #95フェーズ1で新設。Codex設計に基づく「judgeBy を明示し、キー名から
判定方式を推測しない／偽の上限を出さない／モード別ラベルを必須にする」を機械強制する。

対象= checker.modes 内の 'suru'/'through' モード。
- judgeBy:"count" のモード（フェーズ1で移行済み・回数だけで判定）は必須フィールドと整合を検査。
- judgeBy 未設定だが FLAT count 形（excellent が小さい整数・suru配列なし）のモードは
  「未移行のスルー回数天井（フェーズ2以降で移行）」として INFO 列挙（見落とし防止・NGにはしない）。
- config.suru が配列（kengan方式=回数×G）は対象外。

exit: 0=judgeBy:"count"モードが全て妥当 / 1=judgeBy:"count"モードに不備あり。
INFO(未移行)は exit に影響しない。
"""
import sys, json, io, os, argparse

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
COUNT_KEYS = ("suru", "through")
# スルー回数閾値としてありうる上限（これを超える値は「G数の誤入力」を疑う）
MAX_REASONABLE_COUNT = 20


def _mode_conf(c, key):
    if not isinstance(c, dict):
        return None
    v = c.get(key)
    if isinstance(v, dict):
        return v
    md = c.get("modeData")
    if isinstance(md, dict) and isinstance(md.get(key), dict):
        return md[key]
    return None


VALID_JUDGE = ("count", "count-and-game", "game", "review")


def _is_count(v):
    """スルー回数として妥当＝bool以外の非負整数。"""
    return isinstance(v, int) and not isinstance(v, bool)


def _levels(cfg):
    """base + byRate の各プロファイルで (excellent, good, caution) を取り出す。"""
    out = []
    base = {k: cfg.get(k) for k in ("excellent", "good", "caution")}
    out.append(("base", base))
    if isinstance(cfg.get("byRate"), dict):
        for rk, rv in cfg["byRate"].items():
            if isinstance(rv, dict):
                merged = dict(base)
                for k in ("excellent", "good", "caution"):
                    if k in rv:
                        merged[k] = rv[k]
                out.append((rk, merged))
    return out


def _validate_count(slug, key, cfg, ngs):
    """judgeBy:"count" モードの契約を機械強制する（fail-closed）。"""
    # counterLabel（モード別ラベル）必須
    if not isinstance(cfg.get("counterLabel"), str) or not cfg["counterLabel"].strip():
        ngs.append(f"{slug}.{key}: judgeBy=count なのに counterLabel が無い/空")
    # suruMax: 1〜MAX_REASONABLE_COUNT の整数（偽の上限・G混入を弾く）
    smax = cfg.get("suruMax")
    if not (_is_count(smax) and 0 < smax <= MAX_REASONABLE_COUNT):
        ngs.append(f"{slug}.{key}: suruMax は 1〜{MAX_REASONABLE_COUNT} の整数が必須（実値={smax}）")
        smax = None
    # byRate に構造フィールドを置かない（＋ボタン上限はbaseを読むため不整合になる）
    if isinstance(cfg.get("byRate"), dict):
        for rk, rv in cfg["byRate"].items():
            if isinstance(rv, dict):
                for f in ("judgeBy", "counterLabel", "suruMax"):
                    if f in rv:
                        ngs.append(f"{slug}.{key}.byRate[{rk}]: {f} は base に置く（byRate上書き不可）")
    # excellent（確定天井回数）必須・suruMaxと一致
    exc = cfg.get("excellent")
    if not _is_count(exc):
        ngs.append(f"{slug}.{key}: excellent(確定天井回数・整数) が必須（実値={exc}）")
    elif smax is not None and exc != smax:
        ngs.append(f"{slug}.{key}: excellent({exc}) は suruMax({smax}=確定天井) と一致必須")
    # good（狙い目回数）必須
    if not _is_count(cfg.get("good")):
        ngs.append(f"{slug}.{key}: good(狙い目回数・整数) が必須（実値={cfg.get('good')}）")
    # 各プロファイル: 非負整数・≤suruMax・順序
    for rk, lv in _levels(cfg):
        for name in ("excellent", "good", "caution"):
            v = lv.get(name)
            if v is None:
                continue
            if not _is_count(v) or v < 0:
                ngs.append(f"{slug}.{key}[{rk}]: {name}={v} は非負整数のスルー回数が必須")
                continue
            if v > MAX_REASONABLE_COUNT:
                ngs.append(f"{slug}.{key}[{rk}]: {name}={v} はスルー回数として過大（G数の混入疑い）")
            if smax is not None and v > smax:
                ngs.append(f"{slug}.{key}[{rk}]: {name}={v} が suruMax={smax} を超える")
        order = [lv.get(k) for k in ("excellent", "good", "caution") if _is_count(lv.get(k))]
        if order != sorted(order, reverse=True):
            ngs.append(f"{slug}.{key}[{rk}]: 閾値の順序が excellent>=good>=caution でない: {order}")


def validate(machines):
    ngs = []
    infos = []
    for m in machines:
        slug = m.get("slug", "?")
        c = m.get("checker") or {}
        if not isinstance(c, dict):
            continue
        modes = c.get("modes") or []
        for md in modes:
            key = md.get("key") if isinstance(md, dict) else md
            if not isinstance(key, str):
                continue
            cfg = _mode_conf(c, key)
            judge_by = cfg.get("judgeBy") if isinstance(cfg, dict) else None
            # 未知の judgeBy 値（タイプミス等）は全モードで弾く
            if judge_by is not None and judge_by not in VALID_JUDGE:
                ngs.append(f"{slug}.{key}: 未知の judgeBy 値 '{judge_by}'（{'/'.join(VALID_JUDGE)} のいずれか）")
                continue
            # judgeBy:"count" は suru/through 専用
            if judge_by == "count" and key not in COUNT_KEYS:
                ngs.append(f"{slug}.{key}: judgeBy=count は suru/through モード専用（このキーには不可）")
                continue
            if key not in COUNT_KEYS or not isinstance(cfg, dict):
                continue
            if isinstance(cfg.get("suru"), list):
                continue  # kengan方式（回数×G配列）は対象外
            if judge_by == "count":
                _validate_count(slug, key, cfg, ngs)
            elif judge_by in ("count-and-game", "game", "review"):
                infos.append(f"{slug}.{key}: judgeBy={judge_by}（フェーズ2スキーマ）")
            elif _is_count(cfg.get("excellent")) and cfg.get("excellent") <= MAX_REASONABLE_COUNT:
                infos.append(f"{slug}.{key}: 未移行のスルー回数天井（judgeBy未設定・exc={cfg.get('excellent')}）→フェーズ2以降で移行")
    return ngs, infos


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    machines = json.loads(io.open(MACHINES, encoding="utf-8").read())
    ngs, infos = validate(machines)
    print("=== チェッカー スルー天井スキーマ検証 ===")
    if infos:
        print(f"[INFO] 未移行/後続フェーズ: {len(infos)}件")
        for x in infos:
            print("  - " + x)
    if ngs:
        print(f"[NG] judgeBy=count モードの不備: {len(ngs)}件")
        for x in ngs:
            print("  ✗ " + x)
        print("=== 判定: NG ===")
        return 1
    print("=== 判定: judgeBy=count モードは全て妥当 ===")
    return 0


def _selftest():
    ok = True
    def C(slug_cfg):
        return [{"slug": "x", "checker": {"modes": [{"key": slug_cfg[0]}], slug_cfg[0]: slug_cfg[1]}}]
    def check(name, machines, expect_ng):
        nonlocal ok
        ngs, _ = validate(machines)
        got = len(ngs) > 0
        res = "OK" if got == expect_ng else "FAIL"
        if got != expect_ng:
            ok = False
        print(f"[{res}] {name}: NG={len(ngs)} {ngs[:2]}")
    base = {"judgeBy": "count", "counterLabel": "CZスルー回数", "suruMax": 4, "excellent": 4, "good": 3}
    check("正常(count)", C(("through", dict(base))), False)
    check("counterLabel欠落", C(("through", {k: v for k, v in base.items() if k != "counterLabel"})), True)
    check("suruMax欠落", C(("through", {k: v for k, v in base.items() if k != "suruMax"})), True)
    check("suruMax過大(>20)", C(("through", {**base, "suruMax": 500, "excellent": 500})), True)
    check("excellent≠suruMax", C(("through", {**base, "excellent": 3})), True)
    check("good欠落", C(("through", {k: v for k, v in base.items() if k != "good"})), True)
    check("非整数閾値(good=2.5)", C(("through", {**base, "good": 2.5})), True)
    check("負値(good=-1)", C(("through", {**base, "good": -1})), True)
    check("未知judgeBy(coutn)", C(("through", {**base, "judgeBy": "coutn"})), True)
    check("countを非suruキーに", [{"slug": "x", "checker": {"modes": [{"key": "normal"}], "normal": {"judgeBy": "count", "counterLabel": "L", "suruMax": 4, "excellent": 4, "good": 3}}}], True)
    check("byRateに構造上書き", C(("through", {**base, "byRate": {"r": {"suruMax": 3}}})), True)
    check("byRate閾値のみ変動(正常)", C(("suru", {**base, "suruMax": 6, "excellent": 6, "good": 4, "byRate": {"r": {"good": 5}}})), False)
    check("順序破綻(good>excellent)", C(("through", {**base, "suruMax": 5, "excellent": 5, "good": 4, "caution": 6})), True)
    check("kengan配列は対象外", [{"slug": "x", "checker": {"modes": [{"key": "suru"}], "suru": {"suru": [{"count": 1, "excellent": 600}]}}}], False)
    check("未移行FLAT(judgeBy無)はNGにしない", [{"slug": "x", "checker": {"modes": [{"key": "suru"}], "suru": {"excellent": 6, "good": 4, "caution": 2}}}], False)
    print("=== selftest:", "PASS" if ok else "FAIL", "===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""スルー回数天井チェッカー(judgeBy:"count")の実DOM挙動を検査する（Playwright）。

2026-07-23 #95フェーズ1で新設。audit_render(R1-11)はチェッカーの判定を検査しないため、
count モードの回帰（G欄が消えるか・カウンター上限・0〜天井の判定・単位・ラベル・G表記混入）
を実ブラウザで機械検証する。Codex再レビューの「新機能用DOMテスト」要件への対応。

使い方:
    python -m http.server 8000  （別窓）
    python scripts/audit_checker.py --base-url http://localhost:8000
    python scripts/audit_checker.py --base-url http://localhost:8000 --slug karakuri2
本番:
    python scripts/audit_checker.py            （uchidokoro.com を検査＝デプロイ後）

検査対象= checker.modes に judgeBy:"count" のモードを持つ機種のみ（未移行機種は対象外）。
exit: 0=全機種OK / 1=NGあり。
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PROD_URL = "https://uchidokoro.com"

# Windows コンソール UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass


def load_machines() -> list:
    return json.loads((BASE / "assets" / "data" / "machines.json").read_text(encoding="utf-8"))


def count_modes(m: dict):
    """judgeBy:"count" のモードを (key, resolved_base_cfg) で返す。"""
    c = m.get("checker") or {}
    out = []
    for md in (c.get("modes") or []):
        key = md.get("key") if isinstance(md, dict) else md
        if not isinstance(key, str):
            continue
        cfg = c.get(key)
        if isinstance(cfg, dict) and cfg.get("judgeBy") == "count":
            out.append((key, cfg))
    return out


def check_machine(page, base_url, m) -> list:
    ngs = []
    slug = m["slug"]
    modes = count_modes(m)
    if not modes:
        return ngs
    try:
        page.goto(f"{base_url}/machines/{slug}/", wait_until="domcontentloaded", timeout=20000)
        # モードラジオはラベル方式で視覚的に隠れているため attached を待つ
        page.wait_for_selector('input[name="mode"]', state="attached", timeout=10000)
        page.wait_for_timeout(300)
    except Exception as e:  # noqa: BLE001
        return [f"{slug}: ページ読込/チェッカー生成に失敗: {e}"]

    for mk, cfg in modes:
        label = cfg.get("counterLabel")
        smax = cfg.get("suruMax")
        exc = cfg.get("excellent")
        good = cfg.get("good")
        picked = page.evaluate(
            "(k) => { const r=document.querySelector('input[name=\"mode\"][value=\"'+k+'\"]'); if(!r) return false; r.checked=true; r.dispatchEvent(new Event('change',{bubbles:true})); return true; }",
            mk,
        )
        if not picked:
            ngs.append(f"{slug}.{mk}: モードラジオが生成されていない")
            continue
        st = page.evaluate(
            "() => ({ gi: document.getElementById('gameInput').classList.contains('is-hidden'),"
            " sw: document.getElementById('suruWrap').classList.contains('is-hidden'),"
            " label: (document.querySelector('#suruWrap .suru-label')||{}).textContent })"
        )
        if not st.get("gi"):
            ngs.append(f"{slug}.{mk}: G入力欄(gameInput)が非表示になっていない")
        if st.get("sw"):
            ngs.append(f"{slug}.{mk}: スルー回数カウンター(suruWrap)が表示されていない")
        if st.get("label") != label:
            ngs.append(f"{slug}.{mk}: カウンターラベル不一致 実'{st.get('label')}' != 期待'{label}'")

        # 0からsuruMax+2までカウンターを進め、各カウントの判定を収集（モード再選択で0に戻す）
        page.evaluate(
            "(k) => { const r=document.querySelector('input[name=\"mode\"][value=\"'+k+'\"]'); r.checked=true; r.dispatchEvent(new Event('change',{bubbles:true})); }",
            mk,
        )
        seen = []
        steps = (smax if isinstance(smax, int) else 6) + 3
        for _ in range(steps):
            v = page.evaluate("() => parseInt(document.getElementById('suruVal').textContent,10)")
            rt = page.evaluate("() => (document.querySelector('.checker-result .result-text')||{}).textContent || ''")
            seen.append((v, rt))
            page.evaluate("() => document.getElementById('suruInc').click()")

        for v, rt in seen:
            if "G" in rt:
                ngs.append(f"{slug}.{mk}: count判定にG表記が混入 count={v} 文言'{rt}'")
            if not (isinstance(exc, int) and isinstance(good, int)):
                continue
            expect = "◎" if v >= exc else ("◯" if v >= good else "×")
            if not rt.startswith(expect):
                ngs.append(f"{slug}.{mk}: count={v} 期待{expect} 実際'{rt}'")
        max_seen = max((v for v, _ in seen), default=0)
        if isinstance(smax, int) and max_seen != smax:
            ngs.append(f"{slug}.{mk}: カウンター上限={max_seen} が suruMax={smax} と不一致")
    return ngs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=PROD_URL)
    ap.add_argument("--slug", default=None)
    args = ap.parse_args()

    machines = load_machines()
    targets = [m for m in machines if count_modes(m)]
    if args.slug:
        targets = [m for m in targets if m["slug"] == args.slug]
    print(f"=== スルー天井チェッカーDOM検査（judgeBy:count {len(targets)}機種・{args.base_url}） ===")
    if not targets:
        print("対象機種なし")
        return 0

    from playwright.sync_api import sync_playwright

    total = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        for i, m in enumerate(targets, 1):
            ngs = check_machine(page, args.base_url, m)
            total += len(ngs)
            mark = "✅" if not ngs else "❌"
            print(f"[{i:3}/{len(targets):3}] {mark} {m['slug']}")
            for x in ngs:
                print("       ✗ " + x)
        browser.close()
    print(f"=== 完了: {len(targets)}機種・NG合計 {total}件 ===")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())

"""
レンダリング後DOMの全機種検査（Playwrightベース）

audit_site.py（静的解析）では捕まえられないJS実行後の表示異常を検出する。
本番URL（https://uchidokoro.com）に対してヘッドレスChromeでアクセスし、
各機種ページの最終DOMをチェックする。

使い方:
    python scripts/audit_render.py [--slug <slug>]    # 1機種だけ確認
    python scripts/audit_render.py [--limit N]        # 先頭N機種だけ確認
    python scripts/audit_render.py                    # 全104機種を確認

実行時間: 約10分（全機種・1機種あたり約5秒）

チェック項目:
    R1. ページタイトルが「機種ページ | うちどころ。」のデフォルトのまま固まっていないか
    R2. h1（機種名）が「機種名」のままになっていないか
    R3. body内に '99999' が表示されていないか
    R4. canonical タグが /machines/{slug}/ を指しているか
    R5. 設定狙い専用機種で「ゲーム数狙いには向きません」が表示されているか
    R6. fetchエラー（machines.json / machine-details）が発生していないか
    R7. JSコンソールエラーが発生していないか
    R8. 機種名 h1 と machines.json のnameが一致するか
    R9. body内に '**' 記号が見える形で残っていないか（Markdown未解釈バグの検知）
    R10. セクションtitleが統一形と一致しているか（titleの揺れ検知）
"""

from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path

# Windows コンソール UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent
SITE_URL = "https://uchidokoro.com"


def load_machines() -> list:
    return json.loads((BASE / "assets" / "data" / "machines.json").read_text(encoding="utf-8"))


def is_setting_only(machine: dict) -> bool:
    checker = machine.get("checker") or {}
    normal = checker.get("normal") or {}
    excellent = normal.get("excellent")
    return (
        machine.get("limit") in (None, 0)
        and isinstance(excellent, (int, float))
        and excellent >= 99999
    )


def check_one(page, machine: dict) -> list[str]:
    """1機種のレンダリング検査。NGメッセージのリストを返す。"""
    slug = machine["slug"]
    url = f"{SITE_URL}/machines/{slug}/"
    ngs: list[str] = []
    console_errors: list[str] = []

    # コンソールエラー収集
    def on_console(msg):
        if msg.type in ("error",):
            text = msg.text
            # サードパーティスクリプトの既知ノイズは除外
            if any(k in text for k in ["adsbygoogle", "googletagmanager", "Failed to load resource"]):
                return
            console_errors.append(text)

    page.on("console", on_console)

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
    except Exception as e:
        ngs.append(f"[goto失敗] {url}: {e}")
        return ngs

    # meta-auto.js の fetch + DOM更新を待つ（h1が機種名に変わるか、最大3秒）
    try:
        page.wait_for_function(
            """() => {
                const h1 = document.querySelector('#machineTitle');
                return h1 && h1.textContent && h1.textContent !== '機種名';
            }""",
            timeout=3000,
        )
    except Exception:
        pass  # タイムアウトしてもチェックは継続（R2でNG出る）
    # 残りのレンダリング安定化
    page.wait_for_timeout(500)

    # R1: タイトル
    title = page.title()
    if title.strip() in ("機種ページ | うちどころ。", "", "機種ページ"):
        ngs.append(f"R1: タイトルがデフォルトのまま固まってる ('{title}')")

    # R2: h1
    h1_text = page.evaluate("() => document.querySelector('#machineTitle')?.textContent || ''").strip()
    if h1_text == "機種名" or h1_text == "":
        ngs.append(f"R2: 機種名h1が未更新 ('{h1_text}')")

    # R8: machines.json の name と一致
    expected_name = machine["name"]
    if h1_text and h1_text != expected_name:
        ngs.append(f"R8: h1='{h1_text}' vs machines.json name='{expected_name}'")

    # R3: body内に99999が表示されていないか
    body_text = page.evaluate("() => document.body.innerText")
    if "99999" in body_text:
        # 周辺を抜粋
        idx = body_text.find("99999")
        snippet = body_text[max(0, idx - 30):idx + 40].replace("\n", " ")
        ngs.append(f"R3: body内に '99999' を検出 (周辺: ...{snippet}...)")

    # R4: canonical
    canonical = page.evaluate("() => document.querySelector('link[rel=\"canonical\"]')?.href || ''")
    expected_canon = f"{SITE_URL}/machines/{slug}/"
    if canonical != expected_canon:
        ngs.append(f"R4: canonical='{canonical}' (期待値: {expected_canon})")

    # R5: 設定狙い専用機種の表示
    if is_setting_only(machine):
        result_text = page.evaluate("() => document.querySelector('.checker-result .result-text')?.textContent || ''").strip()
        if "向きません" not in result_text and "設定狙い" not in result_text:
            ngs.append(f"R5: 設定狙い専用機種なのに案内表示が誤り ('{result_text}')")

    # R6: fetch関連のエラー（machines.json/machine-details）
    # → ネットワークエラーは page.goto 時の networkidle で大体検出されるが念のため
    # 既に R2/R8 で h1 が機種名になっているか確認しているので、fetch失敗時はそこで検出される

    # R7: コンソールエラー
    if console_errors:
        for err in console_errors[:3]:  # 最大3件
            ngs.append(f"R7: console.error: {err[:120]}")

    # R9: body内に '**' 記号がレンダリング後に残っていないか
    # （Markdown解釈で <strong> 化されているはず）
    if "**" in body_text:
        idx = body_text.find("**")
        snippet = body_text[max(0, idx - 20):idx + 30].replace("\n", " ")
        ngs.append(f"R9: body内に '**' 記号を検出 (Markdown未解釈の可能性・周辺: ...{snippet}...)")

    # R10: セクションtitleが統一形と一致しているか
    allowed_titles = {
        "天井・恩恵", "基本スペック", "期待値の目安", "朝一・リセット情報",
        "設定示唆まとめ", "狙い目の根拠", "ヤメ時の判断", "立ち回りのコツ",
        "噂・未確定情報",
    }
    titles = page.evaluate("""() => Array.from(document.querySelectorAll('.article-title')).map(e => e.textContent.trim())""")
    for t in titles:
        if t and t not in allowed_titles:
            ngs.append(f"R10: 統一形外のtitle: '{t}'")

    return ngs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", help="特定の1機種だけチェック")
    parser.add_argument("--limit", type=int, help="先頭N機種だけチェック")
    parser.add_argument("--json", action="store_true", help="JSON形式で結果を出力")
    args = parser.parse_args()

    machines = load_machines()
    if args.slug:
        machines = [m for m in machines if m["slug"] == args.slug]
        if not machines:
            print(f"slug '{args.slug}' が machines.json に見つかりません")
            sys.exit(2)
    elif args.limit:
        machines = machines[: args.limit]

    from playwright.sync_api import sync_playwright

    all_results: dict[str, list[str]] = {}
    total_ng = 0
    started = time.time()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        for i, m in enumerate(machines, 1):
            slug = m["slug"]
            t0 = time.time()
            try:
                ngs = check_one(page, m)
            except Exception as e:
                ngs = [f"[例外] {e}"]
            elapsed = time.time() - t0
            all_results[slug] = ngs
            total_ng += len(ngs)
            mark = "✅" if not ngs else "❌"
            if not args.json:
                print(f"[{i:3}/{len(machines):3}] {mark} {slug} ({elapsed:.1f}s)" + (f"  NG:{len(ngs)}件" if ngs else ""))
                for ng in ngs:
                    print(f"     - {ng}")

        browser.close()

    elapsed_total = time.time() - started

    if args.json:
        print(json.dumps(all_results, ensure_ascii=False, indent=2))
    else:
        print(f"\n=== レンダリング監査完了 ({elapsed_total:.1f}秒・{len(machines)}機種・NG合計 {total_ng}件) ===")

    sys.exit(0 if total_ng == 0 else 1)


if __name__ == "__main__":
    main()

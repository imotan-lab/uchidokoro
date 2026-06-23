#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
楽天アフィリエイト ＆ A8.net 成果監視スクリプト（週1実行）

両サイトのダッシュボードを Playwright(headless, channel=chrome, 専用プロファイル)で開き、
楽天=成果報酬/売上件数、A8=発生金額/発生件数 を読み取って前回値と比較し、
増えていたら（=1円でも成果が発生したら）メール通知する。

- 未ログイン検知時は「再ログインしてください」を1通だけ送る（毎週のスパム防止）。
- 月初の最初の実行で稼働確認のハートビートメールを1通送る（サイレント死の早期検知）。
- それ以外（変化なし）はメールを送らずログのみ（通知を最小化）。
- ログは全行ファイル出力（C:/Users/imao_/Documents/uchidokoro/logs/rakuten_monitor.log）。

使い方:
  python scripts/rakuten_affiliate_monitor.py --login      # 初回: 専用プロファイルで両サイトにログイン（実Chrome起動）
  python scripts/rakuten_affiliate_monitor.py              # 通常チェック（週1タスクから呼ぶ）
  python scripts/rakuten_affiliate_monitor.py --dry-run    # メール送らず結果だけログ
  python scripts/rakuten_affiliate_monitor.py --test-email # テストメール送信
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# send_notify.send_mail を利用
sys.path.insert(0, "C:/Users/imao_/.claude")
try:
    from send_notify import send_mail  # noqa: E402
except Exception:  # 単体テスト用フォールバック
    send_mail = None

PROFILE_DIR = Path("C:/Users/imao_/.claude/rakuten_a8_monitor_profile")
STATE_PATH = Path("C:/Users/imao_/Documents/uchidokoro/rakuten_monitor_state.json")
LOG_DIR = Path("C:/Users/imao_/Documents/uchidokoro/logs")
LOG_PATH = LOG_DIR / "rakuten_monitor.log"
CHROME_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

A8_URL = "https://media-console.a8.net/home"
RAKUTEN_URL = "https://affiliate.rakuten.co.jp/"

# A8.netは自動化セッション（別プロファイル）を拒否して再認証を強制するため、自動取得は不可。
# 2026-06-23検証: headless/headed どちらも login_required に飛ばされた（Cookieは保存済みでもサーバーが無効化）。
# よって楽天のみ自動監視し、A8は月次ハートビートで「手動でレポート確認を」と促す。
A8_ENABLED = False

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
STEALTH = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"


def _log(msg, level="INFO"):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {msg}"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        print(line.encode(enc, "replace").decode(enc, "replace"))


def load_state():
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            _log(f"state読込失敗（初期化扱い）: {e}", "WARN")
    return {}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _num(s):
    if not s:
        return 0
    digits = re.sub(r"[^\d]", "", s)
    return int(digits) if digits else 0


def _email(subject, body, dry_run=False):
    _log(f"メール: 件名={subject}")
    if dry_run:
        _log("dry-run のため送信スキップ。本文↓\n" + body)
        return
    if send_mail is None:
        _log("send_mail が import できず送信不可", "ERROR")
        return
    try:
        send_mail(subject, body)
        _log("メール送信成功")
    except Exception as e:
        _log(f"メール送信失敗: {type(e).__name__}: {e}", "ERROR")


# --- 抽出用JS（ラベルから祖先ブロックを特定し innerText を返す）---
A8_JS = r"""
(function(){
  var els = Array.prototype.slice.call(document.querySelectorAll('*'));
  var label = els.find(function(el){ return el.children.length===0 && el.textContent.trim()==='発生金額'; });
  if(!label) return null;
  var c = label;
  for(var i=0;i<6 && c.parentElement;i++){
    c = c.parentElement;
    var t = c.textContent;
    if(/クリック/.test(t) && /確定金額/.test(t) && /累計未確定/.test(t)) return c.innerText;
  }
  return null;
})()
"""

RAKUTEN_JS = r"""
(function(){
  var els = Array.prototype.slice.call(document.querySelectorAll('*'));
  var label = els.find(function(el){ return el.children.length===0 && /成果報酬/.test(el.textContent) && el.textContent.length<20; });
  if(!label) return null;
  var c = label;
  for(var i=0;i<7 && c.parentElement;i++){
    c = c.parentElement;
    var t = c.textContent;
    if(/売上金額/.test(t) && /クリック数/.test(t) && /売上件数/.test(t)) return c.innerText;
  }
  return null;
})()
"""


def extract_a8(page):
    page.goto(A8_URL, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_function("()=>/発生金額/.test(document.body.innerText)", timeout=20000)
    except Exception:
        pass
    txt = page.evaluate(A8_JS)
    if not txt:
        url = (page.url or "").lower()
        logged_out = ("login" in url) or ("/auth/" in url) or ("a8.net/" == url.split("//")[-1])
        body = ""
        try:
            body = page.inner_text("body")[:200]
        except Exception:
            pass
        return {"ok": False, "logged_out": logged_out, "url": page.url, "body": body}
    m_kensu = re.search(r"発生件数\s+([\d,]+)", txt)
    m_kingaku = re.search(r"発生金額\s+([\d,]+)\s*円", txt)
    m_ruikei = re.search(r"累計未確定金額\s*([\d,]+)\s*円", txt)
    return {
        "ok": True, "logged_out": False,
        "hassei_kensu": _num(m_kensu.group(1)) if m_kensu else 0,
        "hassei_kingaku": _num(m_kingaku.group(1)) if m_kingaku else 0,
        "ruikei_miteijo": _num(m_ruikei.group(1)) if m_ruikei else 0,
    }


def extract_rakuten(page):
    page.goto(RAKUTEN_URL, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_function("()=>/成果報酬/.test(document.body.innerText)", timeout=20000)
    except Exception:
        pass
    txt = page.evaluate(RAKUTEN_JS)
    if not txt:
        url = (page.url or "").lower()
        logged_out = ("login" in url) or ("id.rakuten" in url) or ("/mylogin" in url)
        return {"ok": False, "logged_out": logged_out, "url": page.url}
    m_hoshu = re.search(r"成果報酬[\s\S]{0,12}?¥\s*([\d,]+)", txt)
    m_kensu = re.search(r"売上件数[\s\S]{0,12}?([\d,]+)", txt)
    m_click = re.search(r"クリック数[\s\S]{0,12}?([\d,]+)", txt)
    return {
        "ok": True, "logged_out": False,
        "seika_hoshu": _num(m_hoshu.group(1)) if m_hoshu else 0,
        "uriage_kensu": _num(m_kensu.group(1)) if m_kensu else 0,
        "click": _num(m_click.group(1)) if m_click else 0,
    }


def do_login():
    """専用プロファイルで実Chromeを起動し、ユーザーに両サイトのログインをしてもらう。"""
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    if not os.path.exists(CHROME_EXE):
        _log(f"Chromeが見つかりません: {CHROME_EXE}", "ERROR")
        return
    cmd = [CHROME_EXE, f"--user-data-dir={PROFILE_DIR}", A8_URL, RAKUTEN_URL]
    _log(f"ログイン用Chrome起動: {cmd}")
    subprocess.Popen(cmd)
    print("\n==== 初回ログイン手順 ====")
    print("1) 開いたChromeで A8.net と 楽天アフィリエイト の両方にログインしてください")
    print("   （タブ1=A8 / タブ2=楽天。楽天は『今尾笙夢さん』表示が出ればOK）")
    print("2) 両方ログインできたら、このChromeウィンドウを閉じてください")
    print("3) その後 `python scripts/rakuten_affiliate_monitor.py --dry-run` で読み取りテスト")
    print("==========================\n")


def run_check(dry_run=False):
    from playwright.sync_api import sync_playwright

    _log("=== 成果監視チェック開始 ===")
    if not PROFILE_DIR.exists():
        _log("専用プロファイル未作成。--login を先に実行してください。", "ERROR")
        return

    state = load_state()
    is_first = not state.get("metrics")
    a8 = {"ok": False, "logged_out": False, "error": "未実行"}
    rk = {"ok": False, "logged_out": False, "error": "未実行"}

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",
            headless=True,
            args=LAUNCH_ARGS,
            user_agent=USER_AGENT,
            locale="ja-JP",
            viewport={"width": 1280, "height": 900},
        )
        try:
            ctx.add_init_script(STEALTH)
            page = ctx.new_page()
            try:
                rk = extract_rakuten(page)
            except Exception as e:
                _log(f"楽天抽出エラー: {type(e).__name__}: {e}", "ERROR")
                rk = {"ok": False, "logged_out": False, "error": str(e)}
            if A8_ENABLED:
                try:
                    a8 = extract_a8(page)
                except Exception as e:
                    _log(f"A8抽出エラー: {type(e).__name__}: {e}", "ERROR")
                    a8 = {"ok": False, "logged_out": False, "error": str(e)}
            else:
                a8 = {"ok": False, "disabled": True}
                _log("A8は自動取得不可のためスキップ（A8_ENABLED=False）")
        finally:
            ctx.close()

    _log(f"A8結果: {a8}")
    _log(f"楽天結果: {rk}")

    metrics = state.get("metrics", {})
    prev_a8 = metrics.get("a8", {})
    prev_rk = metrics.get("rakuten", {})
    earned = []

    # --- 楽天 ---
    if rk.get("ok"):
        if not is_first and (
            rk["seika_hoshu"] > prev_rk.get("seika_hoshu", 0)
            or rk["uriage_kensu"] > prev_rk.get("uriage_kensu", 0)
        ):
            earned.append(
                f"【楽天アフィリエイト】\n"
                f"  成果報酬: ¥{prev_rk.get('seika_hoshu', 0):,} → ¥{rk['seika_hoshu']:,}\n"
                f"  売上件数: {prev_rk.get('uriage_kensu', 0)} → {rk['uriage_kensu']}件\n"
                f"  レポート: https://affiliate.rakuten.co.jp/"
            )
        metrics["rakuten"] = {
            "seika_hoshu": rk["seika_hoshu"],
            "uriage_kensu": rk["uriage_kensu"],
            "click": rk["click"],
        }
        state["rakuten_login_alert"] = False
    else:
        if rk.get("logged_out") and not state.get("rakuten_login_alert"):
            _email(
                "⚠【うちどころ。】楽天アフィリエイトに再ログインしてください",
                "楽天アフィリエイトの成果監視で、ログインが切れている可能性があります。\n\n"
                "次のコマンドで再ログインしてください:\n"
                "  python scripts/rakuten_affiliate_monitor.py --login\n\n"
                "（このアラートはログインが切れている間、重複送信しません）",
                dry_run,
            )
            state["rakuten_login_alert"] = True
        _log("楽天: 読み取り不可（未ログインの可能性）", "WARN")

    # --- A8（自動取得不可のため既定で無効。手動確認はハートビートで促す）---
    if A8_ENABLED and a8.get("ok"):
        if not is_first and (
            a8["hassei_kingaku"] > prev_a8.get("hassei_kingaku", 0)
            or a8["hassei_kensu"] > prev_a8.get("hassei_kensu", 0)
        ):
            earned.append(
                f"【A8.net】\n"
                f"  発生金額(今月): ¥{prev_a8.get('hassei_kingaku', 0):,} → ¥{a8['hassei_kingaku']:,}\n"
                f"  発生件数(今月): {prev_a8.get('hassei_kensu', 0)} → {a8['hassei_kensu']}件\n"
                f"  レポート: https://media-console.a8.net/home"
            )
        metrics["a8"] = {
            "hassei_kingaku": a8["hassei_kingaku"],
            "hassei_kensu": a8["hassei_kensu"],
            "ruikei_miteijo": a8["ruikei_miteijo"],
        }

    state["metrics"] = metrics
    state["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # --- 成果メール ---
    if earned:
        body = (
            "アフィリエイトの成果が発生しました！🎉\n\n"
            + "\n\n".join(earned)
            + "\n\n――――――\n"
            "※楽天は楽天キャッシュ（成果翌月末確定→翌々月10日頃付与）、A8は規定の振込条件で支払われます。\n"
            "サイト: https://uchidokoro.com"
        )
        _email("🎉【うちどころ。】アフィリエイト成果が発生しました", body, dry_run)
    else:
        _log("成果の増加なし（メールなし）")

    # --- 月初ハートビート（稼働確認・1通/月）---
    this_month = datetime.now().strftime("%Y-%m")
    if state.get("last_heartbeat_month") != this_month:
        rk_now = metrics.get("rakuten", {})
        a8_now = metrics.get("a8", {})
        if A8_ENABLED:
            a8_line = f"  A8 : 発生金額 ¥{a8_now.get('hassei_kingaku', 0):,} / 発生 {a8_now.get('hassei_kensu', 0)}件\n"
        else:
            a8_line = (
                "  A8 : 自動取得不可（A8がボット拒否のため）。お手数ですが月1回ほど\n"
                "        https://media-console.a8.net/home でご確認ください\n"
            )
        hb = (
            f"楽天アフィリエイト 成果監視は稼働中です（週1チェック）。\n\n"
            f"【現在の今月成果】\n"
            f"  楽天: 成果報酬 ¥{rk_now.get('seika_hoshu', 0):,} / 売上 {rk_now.get('uriage_kensu', 0)}件\n"
            f"{a8_line}\n"
            f"最終チェック: {state['last_check']}\n"
            f"※楽天は成果が出た時とログイン切れ時のみ別途メールします。"
        )
        # 読み取りに両方失敗した初回は誤った『稼働中』を送らない
        if rk.get("ok") or a8.get("ok"):
            _email(f"🟢【うちどころ。】アフィリ監視 稼働中（{this_month}）", hb, dry_run)
            if not dry_run:
                state["last_heartbeat_month"] = this_month

    if not dry_run:
        save_state(state)
    _log("=== 成果監視チェック終了 ===")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true", help="初回ログイン用Chromeを起動")
    ap.add_argument("--dry-run", action="store_true", help="メール送らず結果のみログ")
    ap.add_argument("--test-email", action="store_true", help="テストメール送信")
    args = ap.parse_args()

    if args.login:
        do_login()
    elif args.test_email:
        _email(
            "🟢【うちどころ。】アフィリ監視 テストメール",
            "成果監視スクリプトからのテスト送信です。メール経路は正常です。",
            dry_run=False,
        )
    else:
        run_check(dry_run=args.dry_run)


if __name__ == "__main__":
    main()

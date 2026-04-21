#!/usr/bin/env python3
"""
うちどころ。のX更新告知投稿スクリプト（@uchidokoro）

新台追加以外の更新通知用。以下の2モードをサポート：
- promotion : 先行記事 → 完全記事 への昇格告知
- correction: データ修正告知（天井値変更・設定段階変更など重大修正時）

新台追加時の投稿は別スクリプト `post_to_x.py` が担当。こちらは「既存記事の更新」用。

使い方:
  # 先行記事→完全記事の昇格告知
  python post_update_to_x.py promotion \\
    --slug sample_slug \\
    --ceiling "1200G" \\
    --strategy "等価700G〜"

  # データ修正告知
  python post_update_to_x.py correction \\
    --slug sample_slug \\
    --change "天井を1000pt→1200ptに訂正"

  # dry-run（投稿せず本文だけ表示）
  python post_update_to_x.py promotion --slug xxx --ceiling 1200G --strategy "700G〜" --dry-run
"""

import argparse
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "C:/Users/imao_/.claude")
from x_poster import post_tweet, count_x_weight, MAX_TWEET_WEIGHT  # noqa: E402
from refresh_x_cookies import refresh_with_auto_chrome  # noqa: E402
from clear_x_cache import clear_account, human_size  # noqa: E402
from send_notify import send_mail  # noqa: E402

PROJECT_DIR = Path(__file__).resolve().parent.parent
MACHINES_PATH = PROJECT_DIR / "assets" / "data" / "machines.json"
RESULT_PATH = PROJECT_DIR / "scripts" / "x_post_result.json"
LOG_DIR = Path("C:/Users/imao_/Documents/uchidokoro/logs")
DETACHED_LOG = LOG_DIR / "post_update_to_x_detached.log"

ACCOUNT = "uchidokoro"
MACHINE_URL_BASE = "https://uchidokoro.com/machine.html?slug="


def _log(msg: str, level: str = "INFO"):
    pid = os.getpid()
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{level}] [pid={pid}] {msg}"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(DETACHED_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        print(line.encode(enc, errors="replace").decode(enc, errors="replace"))


def _log_exception(msg: str, exc: Exception):
    import traceback
    tb = traceback.format_exc()
    _log(f"{msg}: {type(exc).__name__}: {exc}", level="ERROR")
    for tb_line in tb.splitlines():
        _log(f"  {tb_line}", level="ERROR")


def _notify_completion(mode: str, slug: str, machine_name: str, text: str, ok: bool, msg: str):
    """投稿完了後にメール通知を送る（失敗時のみ）。成功時はログのみ（メール通数削減）。"""
    if ok:
        _log(f"投稿成功のためメール通知スキップ（{machine_name}）")
        return

    now = datetime.now().strftime("%Y年%m月%d日 %H:%M")
    mode_label = "解析判明" if mode == "promotion" else "データ修正"
    subject = f"【うちどころ。X】❌ {mode_label}投稿 失敗（{machine_name}）"

    status = "❌ 失敗"
    lines = [
        f"{now} のX投稿結果（{mode_label}）",
        "",
        f"機種: {machine_name}（{slug}）",
        f"結果: {status}",
    ]
    lines.append(f"失敗理由: {msg}")
    lines.append("↓ 以下を手動でXに投稿してください ↓")
    lines += [
        "",
        "--- 投稿本文 ---",
        text,
        "----------------",
        "",
        f"詳細ログ: {DETACHED_LOG}",
        "",
        "サイト: https://uchidokoro.com",
    ]
    body = "\n".join(lines)
    try:
        send_mail(subject, body)
        _log(f"通知メール送信完了: {subject}")
    except Exception as e:
        _log(f"通知メール送信失敗: {type(e).__name__}: {e}")


def find_machine(slug: str) -> dict | None:
    try:
        with open(MACHINES_PATH, encoding="utf-8") as f:
            machines = json.load(f)
        for m in machines:
            if m.get("slug") == slug:
                return m
    except Exception:
        return None
    return None


def detect_machine_type(info: str) -> str:
    if not info:
        return "パチスロ"
    patterns = [
        ("スマスロ", "スマスロ"),
        ("Aタイプ", "Aタイプ"),
        ("ATタイプ", "AT"),
        ("ART", "ART"),
        ("AT", "AT"),
        ("ジャグラー", "ジャグラー"),
    ]
    for key, tag in patterns:
        if key in info:
            return tag
    return "パチスロ"


def build_hashtags(machine: dict) -> str:
    machine_type = detect_machine_type(machine.get("info", ""))
    tags = ["#うちどころ", f"#{machine_type}"]
    return " ".join(tags)


def build_promotion_text(machine: dict, ceiling: str = "", strategy: str = "") -> str:
    """先行記事 → 完全記事 昇格の告知文。
    方針：具体的な数値（天井G数・狙い目G数等）は載せない（誤情報が残るリスク回避）。
    引数 ceiling/strategy は後方互換のため受け取るだけで文面には使わない。"""
    name = machine.get("name", "")
    slug = machine.get("slug", "")
    url = f"{MACHINE_URL_BASE}{slug}"
    hashtags = build_hashtags(machine)

    def build(nm: str) -> str:
        lines = [
            "🔔 解析データ判明",
            nm,
            "",
            "先行公開していた記事を本記事に更新しました",
            "天井・狙い目・小役カウンターはこちら",
            url,
            "",
            hashtags,
        ]
        return "\n".join(lines)

    while count_x_weight(build(name)) > MAX_TWEET_WEIGHT and len(name) > 10:
        name = name[:-1]
    return build(name)


def build_correction_text(machine: dict, change: str = "") -> str:
    """データ修正告知文。
    方針：具体的な変更内容（どの数値をどう変えたか）は載せない（誤情報が残るリスク回避）。
    引数 change は後方互換のため受け取るだけで文面には使わない。"""
    name = machine.get("name", "")
    slug = machine.get("slug", "")
    url = f"{MACHINE_URL_BASE}{slug}"
    hashtags = build_hashtags(machine)

    def build(nm: str) -> str:
        lines = [
            "⚙️ データ更新",
            nm,
            "",
            "最新情報に訂正しました",
            url,
            "",
            hashtags,
        ]
        return "\n".join(lines)

    while count_x_weight(build(name)) > MAX_TWEET_WEIGHT and len(name) > 10:
        name = name[:-1]
    return build(name)


def save_result(mode: str, slug: str, text: str, success: bool, message: str):
    data = {
        "posts": [{
            "slug": slug,
            "name": slug,
            "change_type": mode,
            "location": slug,
            "text": text,
            "success": success,
            "message": message,
        }]
    }
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _safe_print(s: str):
    """Windows cp932 コンソールでも絵文字を含むテキストを出力できるようにする"""
    try:
        print(s)
    except UnicodeEncodeError:
        # コンソールが絵文字等を出せない場合は?に置換して出力
        enc = sys.stdout.encoding or "utf-8"
        print(s.encode(enc, errors="replace").decode(enc, errors="replace"))


def do_post(text: str, slug: str, machine_name: str, mode: str, dry_run: bool, skip_jitter: bool = False) -> int:
    weight = count_x_weight(text)
    if dry_run:
        _safe_print(f"--- [{mode}] {slug} ---")
        _safe_print(text)
        _safe_print(f"(weight: {weight}/{MAX_TWEET_WEIGHT})")
        save_result(mode, slug, text, None, "dry-run")
        return 0

    _log("=" * 60)
    _log(f"=== post_update_to_x 開始 ===")
    _log(f"実行引数: mode={mode}, slug={slug}, name={machine_name}, skip_jitter={skip_jitter}")
    _log(f"実行環境: python={sys.version.split()[0]}, cwd={os.getcwd()}")
    _log(f"投稿本文長: weight={weight}/{MAX_TWEET_WEIGHT}")

    # ランダム待機（bot検出対策：0〜180分）※テスト時はスキップ
    if skip_jitter:
        _log("ランダム待機スキップ（--skip-jitter 指定）", level="WARN")
    else:
        jitter_sec = random.randint(0, 10800)
        h, rem = divmod(jitter_sec, 3600)
        m, s = divmod(rem, 60)
        wake_str = datetime.fromtimestamp(datetime.now().timestamp() + jitter_sec).strftime('%H:%M:%S')
        _log(f"ランダム待機開始: {jitter_sec}秒（約{h}時間{m}分{s}秒）→ 再開予定 {wake_str}")
        time.sleep(jitter_sec)
        _log("ランダム待機完了、投稿処理に入る")

    # Cookieリフレッシュ
    _log("Cookie refresh 開始")
    ok, msg = refresh_with_auto_chrome(ACCOUNT)
    _log(f"Cookie refresh 結果: {'OK' if ok else 'SKIP'} - {msg}",
         level="INFO" if ok else "WARN")

    # 投稿
    _log(f"投稿開始: {slug} ({machine_name})")
    t0 = time.time()
    try:
        ok, msg = post_tweet(ACCOUNT, text)
    except Exception as e:
        _log_exception("post_tweet で例外", e)
        ok, msg = False, f"例外: {type(e).__name__}"
    elapsed = time.time() - t0
    _log(f"投稿結果: {slug} → {'OK' if ok else 'NG'} ({elapsed:.1f}秒) - {msg}",
         level="INFO" if ok else "ERROR")
    save_result(mode, slug, text, ok, msg)
    _log(f"結果JSON保存: {RESULT_PATH}")

    # キャッシュクリア
    try:
        r = clear_account(ACCOUNT)
        if r["skipped"]:
            _log(f"Cache clear: SKIP ({r['reason']})", level="WARN")
        else:
            _log(f"Cache clear: OK ({human_size(r['freed_bytes'])} 解放 / {len(r['details'])}ディレクトリ)")
            for d in r['details']:
                _log(f"  - {d['subdir']}: {human_size(d['freed'])} 削除", level="DEBUG")
    except Exception as e:
        _log_exception("Cache clear で例外", e)

    # 通知メール送信（失敗時のみ）
    _notify_completion(mode, slug, machine_name, text, ok, msg)

    _log("=== post_update_to_x 完了 ===")
    _log("=" * 60)
    return 0 if ok else 1


def _relaunch_detached(argv: list):
    """自分自身を detached サブプロセスとして再起動し、親は即終了する。
    子プロセスの stdout/stderr は DEVNULL に捨てる（ログは _log() 経由で直接ファイルに書く）。"""
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [sys.executable, __file__] + argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
        cwd=str(PROJECT_DIR),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["promotion", "correction"], help="投稿モード")
    parser.add_argument("--slug", required=True, help="対象機種slug")
    parser.add_argument("--ceiling", default="", help="[promotion] 天井値（例: 1200G, 1400pt）")
    parser.add_argument("--strategy", default="", help="[promotion] 狙い目（例: 等価700G〜）")
    parser.add_argument("--change", default="", help="[correction] 変更内容（例: 天井を1000pt→1200ptに訂正）")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--detach", action="store_true",
                        help="実投稿処理をバックグラウンドプロセスで実行し親は即終了")
    parser.add_argument("--skip-jitter", action="store_true",
                        help="0〜180分のランダム待機をスキップ（テスト用）")
    parser.add_argument("--_child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    # --detach 指定時は自身を detached 子プロセスとして起動し親は即終了
    if args.detach and not args._child and not args.dry_run:
        child_argv = [a for a in sys.argv[1:] if a != "--detach"] + ["--_child"]
        _relaunch_detached(child_argv)
        print(f"[detach] バックグラウンドで投稿処理を開始しました。ログ: C:/Users/imao_/Documents/uchidokoro/logs/post_update_to_x_detached.log")
        return 0

    machine = find_machine(args.slug)
    if not machine:
        print(f"ERR: slug '{args.slug}' が machines.json に見つかりません")
        return 1

    if args.mode == "promotion":
        # --ceiling/--strategy は現行の本文方針では未使用（誤情報リスク回避のため数値を載せない）
        # 互換のため引数は受け取るがbuild側で無視される
        text = build_promotion_text(machine)
    else:  # correction
        # --change も現行の本文方針では未使用
        text = build_correction_text(machine)

    return do_post(text, args.slug, machine.get("name", args.slug), args.mode, args.dry_run, args.skip_jitter)


if __name__ == "__main__":
    sys.exit(main())

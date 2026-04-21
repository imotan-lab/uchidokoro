#!/usr/bin/env python3
"""
新台追加のX自動投稿スクリプト（@uchidokoro）

assets/data/machines.json と scripts/machines_prev.json を比較し、
新規追加されたエントリ（新台）のみXに投稿する。
更新・削除は投稿しない（新台タスクの導入時のみ告知）。

投稿結果は scripts/x_post_result.json に保存。
タスクはそれをメール通知に渡す。

使い方:
  python post_to_x.py                                         # 実投稿（導入日なし）
  python post_to_x.py --dry-run                               # 投稿せず文面のみ出力
  python post_to_x.py --dates '{"animal_dotch":"4/22（火）"}'  # 導入日を指定
  python post_to_x.py --dry-run --dates '{"slug":"M/D（曜）"}'

導入日は slug → 日付文字列 のJSON辞書で渡す。
指定のないslugは「導入日: ...」行を省略して投稿する。

新台タスク側からは以下のように呼ぶ:
  python scripts/post_to_x.py --dates '{"animal_dotch":"4/22（火）"}'
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

# x_poster.py, refresh_x_cookies.py を import パスに追加
sys.path.insert(0, "C:/Users/imao_/.claude")
from datetime import datetime  # noqa: E402
from x_poster import post_tweet, count_x_weight, MAX_TWEET_WEIGHT  # noqa: E402
from refresh_x_cookies import refresh_with_auto_chrome  # noqa: E402
from clear_x_cache import clear_account, human_size  # noqa: E402
from send_notify import send_mail  # noqa: E402

PROJECT_DIR = Path(__file__).resolve().parent.parent
MACHINES_PATH = PROJECT_DIR / "assets" / "data" / "machines.json"
PREV_PATH = PROJECT_DIR / "scripts" / "machines_prev.json"
RESULT_PATH = PROJECT_DIR / "scripts" / "x_post_result.json"
LOG_DIR = Path("C:/Users/imao_/Documents/uchidokoro/logs")
DETACHED_LOG = LOG_DIR / "post_to_x_detached.log"

ACCOUNT = "uchidokoro"
MACHINE_URL_BASE = "https://uchidokoro.com/machine.html?slug="


def _log(msg: str):
    """詳細ログをdetached logに時刻付きで追記＋標準出力にも出す。"""
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
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


def _notify_completion(posts: list):
    """投稿完了後にメール通知を送る（失敗または一部失敗時のみ）。
    全件成功時はメールせずログに書くだけ（メール通数削減）。"""
    if not posts:
        return
    succ = [p for p in posts if p.get("success")]
    fail = [p for p in posts if p.get("success") is False]
    total = len(posts)
    now = datetime.now().strftime("%Y年%m月%d日 %H:%M")

    if not fail:
        # 全件成功時はメール送らない（ログのみ）
        _log(f"全件成功のためメール通知スキップ（成功{len(succ)}/{total}件）")
        return

    if not succ:
        subject = f"【うちどころ。X】❌ 新台投稿 {total}件 全て失敗"
    else:
        subject = f"【うちどころ。X】⚠ 新台投稿 成功{len(succ)}/失敗{len(fail)}件"

    lines = [f"{now} のX投稿結果（新台追加）", "",
             f"成功: {len(succ)}件 / 失敗: {len(fail)}件"]
    for p in posts:
        status = "✅ 成功" if p.get("success") else ("❌ 失敗" if p.get("success") is False else "・未投稿")
        lines += ["", f"【{p.get('slug','')}】{p.get('name','')}  {status}"]
        if p.get("success") is False:
            lines.append(f"失敗理由: {p.get('message','')}")
            lines.append("↓ 以下を手動でXに投稿してください ↓")
        lines.append("--- 投稿本文 ---")
        lines.append(p.get("text", ""))
        lines.append("----------------")
    lines += ["", f"詳細ログ: {DETACHED_LOG}", "", "サイト: https://uchidokoro.com"]
    body = "\n".join(lines)
    try:
        send_mail(subject, body)
        _log(f"通知メール送信完了: {subject}")
    except Exception as e:
        _log(f"通知メール送信失敗: {type(e).__name__}: {e}")


def detect_machine_type(info: str) -> str:
    """info文字列から機種タイプ（ハッシュタグ用）を抽出する。
    例: 'スマスロAT' → 'スマスロ', 'AT' → 'AT', '6号機Aタイプ' → 'Aタイプ' """
    if not info:
        return "パチスロ"
    # 優先度順にマッチ
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


def build_hashtags(entry: dict) -> str:
    """新台エントリからハッシュタグ文字列を生成。"""
    info = entry.get("info", "")
    machine_type = detect_machine_type(info)
    tags = ["#うちどころ", f"#{machine_type}", "#パチスロ新台"]
    return " ".join(tags)


def load_json(path: Path, default):
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def diff_added(current: list, prev: list) -> list:
    """slugベースで「前回になく今回あるエントリ」を抽出。更新・削除は無視。"""
    prev_slugs = {e.get("slug") for e in prev}
    return [e for e in current if e.get("slug") and e.get("slug") not in prev_slugs]


def build_post_text(entry: dict, release_date: str | None) -> str:
    """新台エントリから投稿本文を生成。
    status: "preview" の場合は解析待ち向けの文面に切り替える。
    """
    name = entry.get("name", "")
    slug = entry.get("slug", "")
    status = entry.get("status", "complete")
    url = f"{MACHINE_URL_BASE}{slug}"
    hashtags = build_hashtags(entry)
    is_preview = status == "preview"

    def build(nm: str) -> str:
        if is_preview:
            # 先行記事モード：解析前の早期告知
            lines = ["【新台情報・先行記事】", nm]
            if release_date:
                lines.append(f"導入予定: {release_date}")
            lines += ["", "機種概要を先行公開（解析データは判明次第更新）", url, "", hashtags]
        else:
            # 完全記事モード：通常の新台追加告知
            lines = ["【新台追加】", nm]
            if release_date:
                lines.append(f"導入日: {release_date}")
            lines += ["", "狙い目・天井・小役カウンターはこちら", url, "", hashtags]
        return "\n".join(lines)

    # 機種名が長すぎる場合のみ切り詰める
    while count_x_weight(build(name)) > MAX_TWEET_WEIGHT and len(name) > 10:
        name = name[:-1]
    if count_x_weight(build(name)) > MAX_TWEET_WEIGHT:
        name = name.rstrip("・、,") + "…"

    return build(name)


def parse_dates(s: str | None) -> dict:
    if not s:
        return {}
    try:
        d = json.loads(s)
        return d if isinstance(d, dict) else {}
    except Exception as e:
        print(f"警告: --dates のJSONパース失敗: {e}")
        return {}


def _relaunch_detached(argv: list):
    """自分自身を detached サブプロセスとして再起動し、親は即終了する。
    Windows の DETACHED_PROCESS を使うので、呼び出し元のタスクは待たずに完了できる。
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
    parser.add_argument("--dry-run", action="store_true", help="投稿せず文面だけ出力")
    parser.add_argument("--dates", type=str, default="",
                        help='slug→導入日のJSON辞書（例: \'{"slug1":"4/22（火）"}\'）')
    parser.add_argument("--detach", action="store_true",
                        help="実投稿処理をバックグラウンドプロセスで実行し親は即終了（タスクがブロックされるのを防ぐ）")
    parser.add_argument("--_child", action="store_true", help=argparse.SUPPRESS)  # 内部利用
    args = parser.parse_args()

    # --detach 指定時は自身を detached 子プロセスとして起動し親は即終了
    if args.detach and not args._child and not args.dry_run:
        child_argv = [a for a in sys.argv[1:] if a != "--detach"] + ["--_child"]
        _relaunch_detached(child_argv)
        print(f"[detach] バックグラウンドで投稿処理を開始しました。ログ: C:/Users/imao_/Documents/uchidokoro/logs/post_to_x_detached.log")
        return 0

    _log(f"=== post_to_x 開始 === args: dry_run={args.dry_run}, dates={args.dates}, child={args._child}")

    current = load_json(MACHINES_PATH, [])
    prev = load_json(PREV_PATH, None)
    dates = parse_dates(args.dates)

    # 初回実行: prev がなければ現在値をコピーして終了（全件を新規扱いしない）
    if prev is None:
        save_json(PREV_PATH, current)
        save_json(RESULT_PATH, {"initialized": True, "posts": []})
        _log("初回実行: machines_prev.json を初期化しました（投稿なし）")
        return 0

    added = diff_added(current, prev)
    _log(f"差分検出: 新規 {len(added)} 件 {[e.get('slug') for e in added]}")

    if not added:
        save_json(RESULT_PATH, {"posts": []})
        _log("投稿対象なし（新規追加された機種なし）→ 終了")
        save_json(PREV_PATH, current)
        return 0

    # 投稿時刻のランダム化（bot検出対策：0〜180分）
    if not args.dry_run:
        jitter_sec = random.randint(0, 10800)
        h, rem = divmod(jitter_sec, 3600)
        m, s = divmod(rem, 60)
        _log(f"ランダム待機開始: {jitter_sec}秒（約{h}時間{m}分{s}秒）")
        time.sleep(jitter_sec)
        _log("ランダム待機完了、投稿処理に入る")

    # 投稿前にCookieをリフレッシュ（専用Chromeを一時起動→取得→終了、失敗しても続行）
    if not args.dry_run:
        ok, msg = refresh_with_auto_chrome(ACCOUNT)
        _log(f"Cookie refresh: {'OK' if ok else 'SKIP'} - {msg}")

    posts = []
    for i, entry in enumerate(added):
        if i > 0 and not args.dry_run:
            gap = random.randint(30, 120)
            _log(f"投稿間待機: {gap}秒")
            time.sleep(gap)

        slug = entry.get("slug", "")
        release_date = dates.get(slug)
        text = build_post_text(entry, release_date)

        if args.dry_run:
            posts.append({
                "slug": slug,
                "name": entry.get("name", ""),
                "change_type": "追加",
                "location": entry.get("name", ""),
                "text": text,
                "success": None,
                "message": "dry-run",
            })
            print(f"--- [追加] {slug} ---")
            print(text)
            print(f"(weight: {count_x_weight(text)}/{MAX_TWEET_WEIGHT})")
            print()
            continue

        _log(f"[投稿開始] {slug} ({entry.get('name','')})")
        ok, msg = post_tweet(ACCOUNT, text)
        posts.append({
            "slug": slug,
            "name": entry.get("name", ""),
            "change_type": "追加",
            "location": entry.get("name", ""),
            "text": text,
            "success": ok,
            "message": msg,
        })
        _log(f"[投稿結果] {slug}: {'OK' if ok else 'NG'} - {msg}")

    save_json(RESULT_PATH, {"posts": posts})
    _log(f"結果JSON保存: {RESULT_PATH}")

    # 実投稿モードなら prev を更新（次回以降の差分基準にする）
    if not args.dry_run:
        save_json(PREV_PATH, current)
        _log("machines_prev.json を更新")

        try:
            r = clear_account(ACCOUNT)
            if r["skipped"]:
                _log(f"Cache clear: SKIP ({r['reason']})")
            else:
                _log(f"Cache clear: OK ({human_size(r['freed_bytes'])} 解放)")
        except Exception as e:
            _log(f"Cache clear: ERR ({type(e).__name__}: {e})")

        # 投稿完了通知メール（detachedバックグラウンドでも親タスクとは別途送信）
        _notify_completion(posts)

    _log("=== post_to_x 完了 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())

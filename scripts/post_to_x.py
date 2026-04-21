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
from x_poster import post_tweet, count_x_weight, MAX_TWEET_WEIGHT  # noqa: E402
from refresh_x_cookies import refresh_with_auto_chrome  # noqa: E402
from clear_x_cache import clear_account, human_size  # noqa: E402

PROJECT_DIR = Path(__file__).resolve().parent.parent
MACHINES_PATH = PROJECT_DIR / "assets" / "data" / "machines.json"
PREV_PATH = PROJECT_DIR / "scripts" / "machines_prev.json"
RESULT_PATH = PROJECT_DIR / "scripts" / "x_post_result.json"

ACCOUNT = "uchidokoro"
MACHINE_URL_BASE = "https://uchidokoro.com/machine.html?slug="


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
    Windows の DETACHED_PROCESS を使うので、呼び出し元のタスクは待たずに完了できる。"""
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    # 標準入出力を切り離してログファイルに書く
    log_dir = Path("C:/Users/imao_/Documents/uchidokoro/logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "post_to_x_detached.log"
    with open(log_path, "ab") as logf:
        subprocess.Popen(
            [sys.executable, __file__] + argv,
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=logf,
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

    current = load_json(MACHINES_PATH, [])
    prev = load_json(PREV_PATH, None)
    dates = parse_dates(args.dates)

    # 初回実行: prev がなければ現在値をコピーして終了（全件を新規扱いしない）
    if prev is None:
        save_json(PREV_PATH, current)
        save_json(RESULT_PATH, {"initialized": True, "posts": []})
        print("初回実行: machines_prev.json を初期化しました（投稿なし）")
        return 0

    added = diff_added(current, prev)

    if not added:
        save_json(RESULT_PATH, {"posts": []})
        print("投稿対象なし（新規追加された機種なし）")
        # prev を最新で上書き（削除や更新があった場合も追従）
        save_json(PREV_PATH, current)
        return 0

    # 投稿時刻のランダム化（bot検出対策：0〜180分）
    # タスクは23:30固定だが、実投稿時刻を0〜3時間後まで広くずらす
    # （23:30〜翌2:30の広い範囲にバラけるので深夜一斉投稿パターンを回避）
    if not args.dry_run:
        jitter_sec = random.randint(0, 10800)
        h, rem = divmod(jitter_sec, 3600)
        m, s = divmod(rem, 60)
        print(f"Posting jitter: {jitter_sec}秒待機（約{h}時間{m}分{s}秒）")
        time.sleep(jitter_sec)

    # 投稿前にCookieをリフレッシュ（専用Chromeを一時起動→取得→終了、失敗しても続行）
    if not args.dry_run:
        ok, msg = refresh_with_auto_chrome(ACCOUNT)
        print(f"Cookie refresh: {'OK' if ok else 'SKIP'} - {msg}")

    posts = []
    for i, entry in enumerate(added):
        # 2件目以降は投稿間に30〜120秒のランダム待機（連投パターン回避）
        if i > 0 and not args.dry_run:
            gap = random.randint(30, 120)
            print(f"Inter-post jitter: {gap}秒待機")
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
        print(f"[追加] {slug}: {'OK' if ok else 'NG'} - {msg}")

    save_json(RESULT_PATH, {"posts": posts})

    # 実投稿モードなら prev を更新（次回以降の差分基準にする）
    if not args.dry_run:
        save_json(PREV_PATH, current)

        # 投稿でChromeを起動してキャッシュが増えたのでクリア（ログイン情報は残る）
        # Chromeが動いているケース（想定外）は clear_account 側でスキップされる
        try:
            r = clear_account(ACCOUNT)
            if r["skipped"]:
                print(f"Cache clear: SKIP ({r['reason']})")
            else:
                print(f"Cache clear: OK ({human_size(r['freed_bytes'])} 解放)")
        except Exception as e:
            print(f"Cache clear: ERR ({type(e).__name__}: {e})")

    return 0


if __name__ == "__main__":
    sys.exit(main())

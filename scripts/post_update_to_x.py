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
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, "C:/Users/imao_/.claude")
from x_poster import post_tweet, count_x_weight, MAX_TWEET_WEIGHT  # noqa: E402
from refresh_x_cookies import refresh_with_auto_chrome  # noqa: E402
from clear_x_cache import clear_account, human_size  # noqa: E402

PROJECT_DIR = Path(__file__).resolve().parent.parent
MACHINES_PATH = PROJECT_DIR / "assets" / "data" / "machines.json"
RESULT_PATH = PROJECT_DIR / "scripts" / "x_post_result.json"

ACCOUNT = "uchidokoro"
MACHINE_URL_BASE = "https://uchidokoro.com/machine.html?slug="


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


def do_post(text: str, slug: str, mode: str, dry_run: bool) -> int:
    weight = count_x_weight(text)
    if dry_run:
        _safe_print(f"--- [{mode}] {slug} ---")
        _safe_print(text)
        _safe_print(f"(weight: {weight}/{MAX_TWEET_WEIGHT})")
        save_result(mode, slug, text, None, "dry-run")
        return 0

    # ランダム待機（bot検出対策）
    jitter_sec = random.randint(0, 1800)
    print(f"Posting jitter: {jitter_sec}秒待機")
    time.sleep(jitter_sec)

    # Cookieリフレッシュ
    ok, msg = refresh_with_auto_chrome(ACCOUNT)
    print(f"Cookie refresh: {'OK' if ok else 'SKIP'} - {msg}")

    # 投稿
    ok, msg = post_tweet(ACCOUNT, text)
    print(f"[{mode}] {slug}: {'OK' if ok else 'NG'} - {msg}")
    save_result(mode, slug, text, ok, msg)

    # キャッシュクリア
    try:
        r = clear_account(ACCOUNT)
        if r["skipped"]:
            print(f"Cache clear: SKIP ({r['reason']})")
        else:
            print(f"Cache clear: OK ({human_size(r['freed_bytes'])} 解放)")
    except Exception as e:
        print(f"Cache clear: ERR ({type(e).__name__}: {e})")

    return 0 if ok else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["promotion", "correction"], help="投稿モード")
    parser.add_argument("--slug", required=True, help="対象機種slug")
    parser.add_argument("--ceiling", default="", help="[promotion] 天井値（例: 1200G, 1400pt）")
    parser.add_argument("--strategy", default="", help="[promotion] 狙い目（例: 等価700G〜）")
    parser.add_argument("--change", default="", help="[correction] 変更内容（例: 天井を1000pt→1200ptに訂正）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

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

    return do_post(text, args.slug, args.mode, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())

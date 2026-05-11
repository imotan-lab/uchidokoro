"""
machine-details/{slug}.json の sections title 揺れを統一する一括正規化スクリプト。

統一ルール:
  「ヤメ時」「やめどき」「リセット・ヤメ時」 → 「ヤメ時の判断」
  「立ち回りメモ」 → 「立ち回りのコツ」
  「朝一 リセット情報」「リセット狙い」「リセット恩恵」 → 「朝一・リセット情報」
  「判明しているスペック」 → 「基本スペック」
  「狙い目・ヤメ時」 → 「狙い目の根拠」（既に「狙い目の根拠」が別にある機種は重複を統合）
  「狙い目の目安」「狙い目」 → 「狙い目の根拠」（同上）

統合ルール: 統一後のtitleが既に同じ機種に存在する場合は body を結合（重複行は重複削除）

使い方:
    python scripts/normalize_sections.py            # 全機種に適用
    python scripts/normalize_sections.py --dry-run  # 変更内容だけ表示
"""

from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent

# 正規化マッピング
TITLE_RENAME = {
    "ヤメ時": "ヤメ時の判断",
    "やめどき": "ヤメ時の判断",
    "リセット・ヤメ時": "ヤメ時の判断",
    "立ち回りメモ": "立ち回りのコツ",
    "朝一 リセット情報": "朝一・リセット情報",
    "リセット狙い": "朝一・リセット情報",
    "リセット恩恵": "朝一・リセット情報",
    "判明しているスペック": "基本スペック",
    "狙い目・ヤメ時": "狙い目の根拠",
    "狙い目の目安": "狙い目の根拠",
    "狙い目": "狙い目の根拠",
}


def merge_body(existing, incoming):
    """既存sectionのbodyに incoming(別section) を結合する"""
    e = existing.get("body")
    i = incoming.get("body")

    def to_list(x):
        if x is None:
            return []
        if isinstance(x, list):
            return [str(s) for s in x]
        return [str(x)]

    merged = to_list(e) + to_list(i)
    # 重複削除（順序保持）
    seen = set()
    deduped = []
    for line in merged:
        if line not in seen:
            seen.add(line)
            deduped.append(line)
    existing["body"] = deduped
    return existing


def normalize(detail: dict) -> tuple[dict, list[str]]:
    """1機種のsectionsを正規化。変更内容のログを返す。"""
    sections = detail.get("sections", [])
    log = []
    new_sections = []
    # 統一後titleでグルーピング
    by_title: dict[str, dict] = {}
    order: list[str] = []
    for sec in sections:
        old_title = sec.get("title", "")
        new_title = TITLE_RENAME.get(old_title, old_title)
        if new_title != old_title:
            log.append(f"  rename '{old_title}' → '{new_title}'")
            sec["title"] = new_title
        # 既に同じtitleがあれば結合
        if new_title in by_title:
            log.append(f"  merge body into '{new_title}'（重複セクションを統合）")
            merge_body(by_title[new_title], sec)
        else:
            by_title[new_title] = sec
            order.append(new_title)

    # 順序通りに再構築
    for t in order:
        new_sections.append(by_title[t])

    detail["sections"] = new_sections
    return detail, log


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    detail_dir = BASE / "assets" / "data" / "machine-details"
    files = sorted(detail_dir.glob("*.json"))
    total_changed = 0
    total_renamed = 0
    total_merged = 0
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        new_data, log = normalize(data)
        if not log:
            continue
        total_changed += 1
        renamed = sum(1 for l in log if "rename" in l)
        merged = sum(1 for l in log if "merge" in l)
        total_renamed += renamed
        total_merged += merged
        print(f"\n{f.stem}: rename={renamed} merge={merged}")
        for l in log:
            print(l)
        if not args.dry_run:
            f.write_text(
                json.dumps(new_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    suffix = "（DRY-RUN）" if args.dry_run else ""
    print(f"\n=== 完了{suffix} 機種数:{total_changed} / rename:{total_renamed} / merge:{total_merged} ===")


if __name__ == "__main__":
    main()

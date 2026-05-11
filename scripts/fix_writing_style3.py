"""残った常体文の最終ピンポイント修正"""
import json
from pathlib import Path
BASE = Path(__file__).resolve().parent.parent
DETAIL = BASE / "assets" / "data" / "machine-details"

FIXES = [
    ("hokuto", "立ち回りが向いている。", "立ち回りが向いています。"),
    ("hokuto", "100G前後から候補にする。", "100G前後から候補にしましょう。"),
    ("hokuto", "目押し必須。", "目押しが必須です。"),
    ("funky_juggler2", "ヤメ時の概念はない。", "ヤメ時の概念はありません。"),
]

import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

by_slug = {}
for slug, old, new in FIXES:
    by_slug.setdefault(slug, []).append((old, new))

for slug, pairs in by_slug.items():
    p = DETAIL / f"{slug}.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    changed = 0
    for s in d.get("sections", []):
        body = s.get("body")
        if not isinstance(body, list):
            continue
        for i, item in enumerate(body):
            if not isinstance(item, str):
                continue
            new_item = item
            for old, new in pairs:
                if old in new_item:
                    new_item = new_item.replace(old, new)
                    changed += 1
            body[i] = new_item
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{slug}: {changed}箇所書き換え")

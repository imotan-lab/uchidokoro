"""
machine.html を元に、各 machines/{slug}/index.html を実コンテンツとして生成する。

これにより /machines/{slug}/ がリダイレクトページではなく、machine.html と同等の実コンテンツを持つページになる。
Google から「クロール済み・インデックス未登録」と判定される問題（中身が空のリダイレクト判定）を解消する。

変換ルール:
1. <head> 直後に <base href="/"> を追加（相対リソースをルート基準に）
2. <link rel="canonical"> を /machines/{slug}/ に書き換え（無ければ追加）
3. machine.html のJSはURLパスから slug を自動取得するので、その他は変更不要

使い方:
    python scripts/build_machine_pages.py
"""

from __future__ import annotations
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent


def main():
    # 機種一覧
    machines = json.loads((BASE / "assets" / "data" / "machines.json").read_text(encoding="utf-8"))
    slugs = [m["slug"] for m in machines]

    # machine.html を読み込み
    template = (BASE / "machine.html").read_text(encoding="utf-8")

    # <base href="/"> を <head> 直後に挿入
    if '<base ' not in template:
        template = re.sub(r"(<head[^>]*>)", r'\1\n<base href="/">', template, count=1)

    generated = 0
    for slug in slugs:
        canonical_url = f"https://uchidokoro.com/machines/{slug}/"
        # canonical タグを置換 or 追加
        html = template
        if 'rel="canonical"' in html:
            html = re.sub(
                r'<link\s+rel="canonical"[^>]*>',
                f'<link rel="canonical" href="{canonical_url}">',
                html,
                count=1,
            )
        else:
            # </head> の直前に追加
            html = html.replace("</head>", f'<link rel="canonical" href="{canonical_url}">\n</head>', 1)

        # 出力先ディレクトリを作成
        out_dir = BASE / "machines" / slug
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "index.html").write_text(html, encoding="utf-8", newline="\n")
        generated += 1

    print(f"生成完了: {generated} 機種 / machines/{{slug}}/index.html")


if __name__ == "__main__":
    main()

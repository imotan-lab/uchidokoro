"""build_preview_site.py — 公開されない「確認用の写し」をまっさらから作る。

★これは公開物ではない★
  出典の裏取りが済んでいない内容も、そのまま出す。だから
  ・出力先は .preview-site/ 固定（Git管理外）
  ・全ページ noindex,nofollow ＋ 赤いバナー ＋ 目印 PREVIEW_BUILD
  ・robots.txt は全面 Disallow
  ・本番の artifact は PREVIEW_BUILD を見つけたら失敗する（二重の歯止め）

使い方:
    python scripts/build_preview_site.py
    python -m http.server 8000 -d .preview-site      # 確認用サーバ
    → http://localhost:8000/machines/hokuto/ など
"""

from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "scripts"))

import preview_site as pv          # noqa: E402
import build_machine_pages as bmp  # noqa: E402
import build_hub_pages as bhp      # noqa: E402


def main() -> int:
    print("=" * 66)
    print("確認用の写しを作ります（公開されません）")
    print("=" * 66)

    pv.clean()
    pv.ensure_scaffold()

    rc = bmp.main(preview=True) or 0
    if rc:
        print("★機種ページの写しで失敗しました★")
        return rc

    rc = bhp.main(preview=True) or 0
    if rc:
        print("★ハブ4ページの写しで失敗しました★")
        return rc

    # 念のため：写しの中身が本当に「公開されない印」を持っているか確かめる
    bad = []
    for f in sorted(pv.PREVIEW_DIR.rglob("*.html")):
        text = f.read_text(encoding="utf-8")
        rel = f.relative_to(pv.PREVIEW_DIR).as_posix()
        if pv.MARKER not in text:
            bad.append(f"{rel}: 目印 {pv.MARKER} が無い")
        if 'content="noindex,nofollow"' not in text:
            bad.append(f"{rel}: noindex,nofollow が無い")
        if pv.BANNER_HTML not in text:
            bad.append(f"{rel}: 確認用バナーが無い")
    robots = (pv.PREVIEW_DIR / "robots.txt").read_text(encoding="utf-8")
    if "Disallow: /" not in robots:
        bad.append("robots.txt が全面Disallowになっていない")

    if bad:
        print(f"★写しの印が付いていない箇所が {len(bad)} 件あります★")
        for b in bad[:20]:
            print(f"  ✗ {b}")
        return 1

    count = len(list(pv.PREVIEW_DIR.rglob("*.html")))
    print("=" * 66)
    print(f"完了: {count} ページを {pv.PREVIEW_DIR.name}/ に写しました（公開されません）")
    print("  確認: python -m http.server 8000 -d .preview-site")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

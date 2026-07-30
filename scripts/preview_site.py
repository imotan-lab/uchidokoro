"""preview_site.py — 編集中の内容を「公開されない場所」で確かめるための土台。

★なぜ必要か（2026-07-30・移行手順2）★
  本番のHTML生成は出典の裏取りゲートを通らないと動かない（いまは公開できる機種が0）。
  それだけだと「記事を直して表示を確かめる」手段が消える。そこで、
  ゲートを通らない内容は **公開されない場所（.preview-site/）にだけ** 書き出す。

★この土台が守ること★
  1. 出力先は .preview-site/ の中だけ。外へ書こうとしたら例外で止める（assert_inside）
  2. 全ページに noindex,nofollow ／ 目印 PREVIEW_BUILD ／ 見て分かるバナー
  3. robots.txt は全面 Disallow
  4. .gitignore 対象なのでコミットされない。本番 artifact 側でも明示的に拒否する
     （build_pages_artifact.py が PREVIEW_BUILD を見つけたら失敗する）

★使い方★
  python scripts/build_preview_site.py        # まっさらから作り直す
  python -m http.server 8000 -d .preview-site # 確認用サーバ（document root を写しにする）
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PREVIEW_DIR = BASE / ".preview-site"

# ★artifact側の検査がこの文字列を探す★（写しが本番へ紛れ込んだら失敗させるため）
MARKER = "PREVIEW_BUILD"

PREVIEW_CSS = """/* 確認用の写しだけに読み込まれる。本番には存在しない。 */
.preview-banner{position:sticky;top:0;z-index:9999;display:block;
  padding:8px 12px;background:#7a1f1f;color:#fff;font-size:13px;
  line-height:1.5;text-align:center;letter-spacing:.02em}
.preview-banner b{color:#ffd76a}
"""

BANNER_HTML = (
    '<div class="preview-banner">'
    "<b>確認用の写しです。</b>"
    "ここに出ている数値は出典の確認が済んでいません。公開されていません。"
    "</div>"
)

ROBOTS_TXT = "User-agent: *\nDisallow: /\n"

# 写しに入れないもの（公開設定・検索エンジン向けの実ファイル）
SCAFFOLD_SKIP_HTML = {"googleafe441235e57f84f.html"}
SCAFFOLD_ROOT_EXTRA = ("meta-auto.js", "manifest.json", "favicon.ico")


class PreviewError(RuntimeError):
    pass


def assert_inside(path: Path, root: Path = PREVIEW_DIR) -> Path:
    """path が root の中であることを確かめる。外なら書かせない（fail-closed）。"""
    rp = Path(path).resolve()
    rr = Path(root).resolve()
    if rp != rr and rr not in rp.parents:
        raise PreviewError(f"写しの外へ書こうとしました: {rp}")
    return rp


def clean() -> None:
    """写しをまっさらにする。★.preview-site 以外は絶対に消さない★"""
    target = PREVIEW_DIR.resolve()
    if target.name != ".preview-site" or target.parent != BASE.resolve():
        raise PreviewError(f"想定外の場所を消そうとしました: {target}")
    if target.exists():
        shutil.rmtree(target)


def mark(html: str) -> str:
    """1ページ分のHTMLに noindex・目印・バナー・写し用CSSを入れる（何度通しても同じ結果）。"""
    if MARKER in html:
        return html

    # 1) 既存の robots meta を消してから noindex,nofollow を入れ直す
    html = re.sub(r'<meta\s+name="robots"[^>]*>\s*', "", html, flags=re.IGNORECASE)
    head_extra = (
        f"<!-- {MARKER} -->\n"
        '<meta name="robots" content="noindex,nofollow">\n'
        '<link rel="stylesheet" href="/preview.css">\n'
    )
    if "</head>" in html:
        html = html.replace("</head>", head_extra + "</head>", 1)
    else:
        html = head_extra + html

    # 2) 本文の先頭にバナー
    m = re.search(r"<body[^>]*>", html, flags=re.IGNORECASE)
    if m:
        html = html[: m.end()] + "\n" + BANNER_HTML + html[m.end():]
    else:
        html = BANNER_HTML + html

    # 3) Service Worker を写しでは動かさない
    #    （本番用SWが写しの内容をキャッシュして、後で本番に混ざるのを避ける）
    html = html.replace('navigator.serviceWorker.register("/service-worker.js")',
                        'Promise.reject(new Error("preview: service worker disabled"))')
    return html


def write_html(rel_path: str, html: str) -> Path:
    """写しの中へHTMLを1枚書く（必ず mark を通す）。"""
    out = assert_inside(PREVIEW_DIR / rel_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(mark(html), encoding="utf-8", newline="\n")
    return out


def ensure_scaffold() -> None:
    """写しの土台（CSS・画像・データ・素のページ・robots.txt）を用意する。

    何度呼んでも壊れない（消さずに上書きする）。まっさらにしたい時は clean() を先に呼ぶ。
    ★ここでコピーするのは authoring のデータ★＝裏取り前の内容。だから写しにしか置かない。
    """
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)

    (PREVIEW_DIR / "robots.txt").write_text(ROBOTS_TXT, encoding="utf-8", newline="\n")
    (PREVIEW_DIR / "preview.css").write_text(PREVIEW_CSS, encoding="utf-8", newline="\n")
    (PREVIEW_DIR / MARKER).write_text(
        "この中身は確認用の写しです。公開しません。\n", encoding="utf-8", newline="\n")

    for sub in ("css", "img", "data"):
        src = BASE / "assets" / sub
        if src.is_dir():
            dst = assert_inside(PREVIEW_DIR / "assets" / sub)
            shutil.copytree(src, dst, dirs_exist_ok=True)

    for name in SCAFFOLD_ROOT_EXTRA:
        src = BASE / name
        if src.is_file():
            shutil.copy2(src, assert_inside(PREVIEW_DIR / name))

    for src in sorted(BASE.glob("*.html")):
        if src.name in SCAFFOLD_SKIP_HTML:
            continue
        write_html(src.name, src.read_text(encoding="utf-8"))

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
  python scripts/build_preview_site.py
  python -m http.server 8000 --bind 127.0.0.1 -d .preview-site

  ★`--bind 127.0.0.1` を必ず付ける★（2026-07-30・Codex 13巡目 (a)-3）
    Python の http.server は既定で全ネットワーク面に開くので、付けないと
    **同じLANの他の端末から裏取り前の内容を丸ごと読めてしまう**。
    noindex も robots も「検索避け」であって鍵ではない。
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
.preview-copy-bar{position:sticky;top:0;z-index:9999;display:block;
  padding:8px 12px;background:#7a1f1f;color:#fff;font-size:13px;
  line-height:1.5;text-align:center;letter-spacing:.02em}
.preview-copy-bar b{color:#ffd76a}
"""

BANNER_HTML = (
    '<div class="preview-copy-bar">'
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


HEAD_BLOCK = (
    f"<!-- {MARKER} -->\n"
    '<meta name="robots" content="noindex,nofollow">\n'
    '<link rel="stylesheet" href="/preview.css">\n'
)

# 写しを開いた時に、前に登録された Service Worker を解除する（Codex 14巡目 (a)-7）
#   登録文を消しても、同じ origin に残っている古いSWは動き続け、
#   本番の内容と写しの内容が混ざる。写し側から能動的に解除しにいく。
UNREGISTER_SW = (
    "<script>"
    "if('serviceWorker' in navigator){"
    "navigator.serviceWorker.getRegistrations()"
    ".then(function(rs){rs.forEach(function(r){r.unregister();});}).catch(function(){});"
    "if(window.caches&&caches.keys){"
    "caches.keys().then(function(ks){ks.forEach(function(k){caches.delete(k);});})"
    ".catch(function(){});}}"
    "</script>"
)

# ★何度通しても増えないように、前回入れた印だけを取り除く★（Codex 14巡目 (b)-4）
#   以前は `<!-- PREVIEW_BUILD -->` から次の preview.css までを
#   ワイルドカードで消していたので、**元のHTMLを巻き込んで削れた**。
#   いまは自分が入れた文字列と完全一致した時だけ剥がす。
_SW_REGISTER_RE = re.compile(
    r"navigator\s*\.\s*serviceWorker\s*\.\s*register\s*\([^)]*\)", re.IGNORECASE)


BODY_BLOCK = "\n" + BANNER_HTML + UNREGISTER_SW


def strip_html_comments(html: str) -> str:
    """HTMLコメントを外す。**印がコメント内にあるだけで合格にしないため**。"""
    return re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)


def mark(html: str) -> str:
    """1ページ分に noindex・目印・バナー・写し用CSSを入れる（何度通しても同じ結果）。

    ★「MARKER が含まれていたら何もしない」方式をやめた★（Codex 13巡目 (b)-2）
      元のHTMLのコメント欄に文字列を置くだけで、metaもバナーも無いページが
      「印つき」として通ってしまっていた。**毎回いったん剥がして、必ず入れ直す**。
    """
    # 0) 前回「自分が入れた文字列」だけを剥がす（元のHTMLは巻き込まない）
    for block in (HEAD_BLOCK, BODY_BLOCK):
        html = html.replace(block, "")

    # 1) 既存の robots meta を消してから noindex,nofollow を入れ直す
    html = re.sub(r'<meta\s+name="robots"[^>]*>\s*', "", html, flags=re.IGNORECASE)
    if "</head>" in html:
        html = html.replace("</head>", HEAD_BLOCK + "</head>", 1)
    else:
        html = HEAD_BLOCK + html

    # 2) 本文の先頭にバナー＋前のService Workerの解除
    m = re.search(r"<body[^>]*>", html, flags=re.IGNORECASE)
    if m:
        html = html[: m.end()] + BODY_BLOCK + html[m.end():]
    else:
        html = BODY_BLOCK + html

    # 3) Service Worker を写しでは動かさない
    #    ★引用符の種類を問わず消す★（Codex 13巡目 (b)-1）
    #      ダブルクォート決め打ちだったので setting.html（シングルクォート）が素通りしていた。
    html = _SW_REGISTER_RE.sub(
        'Promise.reject(new Error("preview: service worker disabled"))', html)
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

    for sub in ("css", "img"):
        src = BASE / "assets" / sub
        if src.is_dir():
            dst = assert_inside(PREVIEW_DIR / "assets" / sub)
            shutil.copytree(src, dst, dirs_exist_ok=True)

    # ★assets/data は丸ごと写さない★（2026-07-30・Codex 13巡目 (a)-3）
    #   丸ごとだと台帳・出典レジストリ・ゲート設定まで写しに置かれ、
    #   サーバを開いた瞬間に内部情報ごと読めてしまう。表示に要る2つだけにする。
    data_dst = assert_inside(PREVIEW_DIR / "assets" / "data")
    data_dst.mkdir(parents=True, exist_ok=True)
    src_machines = BASE / "assets" / "data" / "machines.json"
    if src_machines.is_file():
        shutil.copy2(src_machines, data_dst / "machines.json")
    src_details = BASE / "assets" / "data" / "machine-details"
    if src_details.is_dir():
        shutil.copytree(src_details, data_dst / "machine-details", dirs_exist_ok=True)

    for name in SCAFFOLD_ROOT_EXTRA:
        src = BASE / name
        if src.is_file():
            shutil.copy2(src, assert_inside(PREVIEW_DIR / name))

    for src in sorted(BASE.glob("*.html")):
        if src.name in SCAFFOLD_SKIP_HTML:
            continue
        write_html(src.name, src.read_text(encoding="utf-8"))

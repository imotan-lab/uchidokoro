#!/usr/bin/env python3
"""build_pages_artifact.py — 公開する物を「空のフォルダ」から組み立てる。

★なぜ空から作るのか（移行手順3・Codex 11〜12巡目）★
  いまはリポジトリの中身がそのまま公開されている。つまり
  「置いてあるファイルは全部公開される」ので、うっかり置いた編集用データも公開される。
  そこで公開の入口を一本にし、**必要な物だけを明示的に入れる**方式へ移す。
  入れ忘れは表示崩れですぐ気づくが、入れてはいけない物の混入は気づけない。
  だから「既定は入れない」にする。

★入れてはいけない物★
  編集用の machines.json / machine-details ／ 台帳・証拠・レジストリ・許可リスト・
  ゲート設定 ／ 汎用の machine.html ／ 裏取り前の setting.html ／ 確認用の写し

★出来上がりの照合★
  公開名簿 ＝ /machines/ の実フォルダ ＝ 一覧ページの機種 ＝ sitemap ＝ ハブに出る機種
  この5つが完全に同じ集合でなければ失敗させる。

使い方:
    python scripts/build_pages_artifact.py          # _site/ を作る
    python scripts/build_pages_artifact.py --selftest
"""

from __future__ import annotations

import hashlib
import html as html_mod
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
OUT = BASE / "_site"
NEXT = BASE / "_site.next"

# 写しの目印（preview_site.MARKER と同じ。写しが混ざったら失敗させる）
PREVIEW_MARKER = "PREVIEW_BUILD"
PREVIEW_DIRNAME = ".preview-site"

ROOT_FILES = (
    "404.html",
    "CNAME",
    "about.html",
    "ads.txt",
    "contact.html",
    "guide-haena.html",
    "guide-pochipochi.html",
    "guide-rate.html",
    "guide-reset.html",
    "guide-yamedoki.html",
    "index.html",
    "manifest.json",
    "meta-auto.js",
    "privacy.html",
    "robots.txt",
    "service-worker.js",
)

GENERATED_HUBS = (
    "guide-ichiran.html",
    "guide-reset-ranking.html",
    "guide-suru-tenjo.html",
    "guide-tenjo-ranking.html",
)

# sitemap に載せる固定ページ（setting.html は準備中の差し替えなので載せない）
SITEMAP_STATIC = (
    "/",
    "/about.html",
    "/contact.html",
    "/privacy.html",
    "/guide-haena.html",
    "/guide-ichiran.html",
    "/guide-pochipochi.html",
    "/guide-rate.html",
    "/guide-reset.html",
    "/guide-reset-ranking.html",
    "/guide-suru-tenjo.html",
    "/guide-tenjo-ranking.html",
    "/guide-yamedoki.html",
)

# artifact に入っていたら失敗させるパス（編集用データ・内部情報・汎用ページ）
FORBIDDEN_PATHS = (
    "machine.html",
    PREVIEW_DIRNAME,
    "scripts",
    "_design",
    "assets/data/public",
    "assets/data/claim-gate.json",
    "assets/data/claim-allowlist.json",
    "assets/data/claim-evidence",
    "assets/data/ledger.json",
    "assets/data/source-registry.json",
    "assets/data/facts",
)

MACHINE_HREF = re.compile(r"""(?:https?://[^/"']+)?/machines/([^/"'?#]+)/""")
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
TAG = re.compile(r"<[^>]+>")

# 写しの目印を探す対象（HTMLだけ見ていると .js / .svg / .htm を見落とす）
MARKER_SCAN_SUFFIXES = {".html", ".htm", ".js", ".json", ".svg", ".css", ".xml", ".txt"}

APPROVAL_SCHEMA = "template-approval/v2"

# ★全機種に一斉に効く入力★（1箇所直すと全ページに載るもの）
#   ここは「過不足なく一致」を要求する。承認一覧から外して回避できないようにするため。
APPROVED_INPUTS = frozenset({
    # ページのひな型
    "machine.html",
    "index.html",
    # 手書きの固定ページ（そのまま公開されるので中身の検査が効かない）
    #   （Codex 17巡目 (a)-2：ここに嘘を書けば監査を通って公開されていた）
    "404.html",
    "about.html",
    "contact.html",
    "privacy.html",
    "guide-haena.html",
    "guide-pochipochi.html",
    "guide-rate.html",
    "guide-reset.html",
    "guide-yamedoki.html",
    "manifest.json",
    # 全ページで読み込まれる見た目と動き
    "assets/css/practical.css",
    "meta-auto.js",
    "service-worker.js",
    # 全ページに出る画像（画像の中に文字を描けば全機種に表示できる）
    "assets/img/logo.png",
    "assets/img/ogp.png",
    # 公開物を作るコード（ここを直せば何でも書ける）
    #   ★判定を構成する側も入れる★（Codex 17巡目 (a)-1）
    "scripts/build_pages_artifact.py",
    "scripts/build_machine_pages.py",
    "scripts/build_hub_pages.py",
    "scripts/build_public_data.py",
    "scripts/build_ledger.py",
    "scripts/gates.py",
    "scripts/audit_public.py",
    "scripts/claim_reconcile.py",
    "scripts/claim_c5.py",
    "scripts/claim_inventory.py",
    "scripts/claim_ledger.py",
    "scripts/claim_identity.py",
    "scripts/preview_site.py",
    # ハブの手書き散文
    "scripts/hub_prose.json",
})

# ルート直下に置く固定名の資産（★名前パターンでのコピーはやめた★・Codex 17巡目 (a)-2）
#   `google*.html` のような書き方だと、新しく `google-news.html` を作るだけで
#   検査を通らない公開URLを増やせてしまう。
ROOT_ASSETS = (
    "favicon.ico",
    "googleafe441235e57f84f.html",   # Search Console 所有権確認（削除禁止）
)

IGNORED_DIR_NAMES = {".git", ".github", PREVIEW_DIRNAME, "_site", "_site.next",
                     "__pycache__", "_design", ".claude",
                     "node_modules", ".venv", "venv", ".mypy_cache", ".pytest_cache"}
SOURCE_IGNORE = shutil.ignore_patterns(*sorted(IGNORED_DIR_NAMES), "*.pyc")


class BuildError(RuntimeError):
    pass


def run(work: Path, *args: str) -> None:
    cp = subprocess.run([sys.executable, *args], cwd=work, text=True, check=False)
    if cp.returncode:
        raise BuildError(f"command failed ({cp.returncode}): {' '.join(args)}")


def _no_duplicate_keys(pairs):
    """★JSONの同名キー重複を黙って通さない★（Codex 13巡目 (b)-3）
      Python既定は last-wins なので、`"slugs"` を2回書けば
      前の値が消える＝監査を欺ける。重複はその場で失敗させる。
    """
    seen = {}
    for k, v in pairs:
        if k in seen:
            raise BuildError(f"duplicate JSON key: {k}")
        seen[k] = v
    return seen


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"),
                          object_pairs_hook=_no_duplicate_keys)
    except BuildError:
        raise
    except Exception as exc:
        raise BuildError(f"cannot read JSON: {path}: {exc}") from exc


def read_json_dict(path: Path) -> dict:
    """辞書であることまで確かめて読む（`.get()` で未処理例外にしない）。"""
    data = read_json(path)
    if not isinstance(data, dict):
        raise BuildError(f"expected a JSON object: {path}")
    return data


def reject_symlinks(root: Path, ignore: set[str] = frozenset()) -> None:
    """★リンクの類を全面的に拒否する★（Codex 13巡目 (a)-2 / 15巡目 (a)-4）

    許可ディレクトリ（assets/img など）の中に
    `authoring-machines.json -> ../data/machines.json` のようなリンクを置くと、
    コピーが実体を追って**禁止データがそのまま公開される**。名前で拒否しても意味がない。
    ★ハードリンクも同じ★。しかも一時領域へコピーすると通常ファイルになって
    後から見分けられないので、**コピーする前の入力**で拒否する。
    """
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            entries = list(cur.iterdir())
        except OSError as exc:      # ★権限エラーも整理した警告にする★（Codex 16巡目 (b)-5）
            raise BuildError(f"ビルド入力を読めません: {cur}: {exc}") from exc
        for entry in entries:
            if entry.name in ignore:
                continue
            rel = entry.relative_to(root).as_posix()
            try:
                is_link = entry.is_symlink() or (os.name == "nt" and entry.is_junction())
                is_dir = entry.is_dir()
                nlink = 1 if is_dir else entry.stat().st_nlink
            except OSError as exc:
                raise BuildError(f"ビルド入力を調べられません: {rel}: {exc}") from exc
            if is_link:
                raise BuildError(
                    f"symlink/junction is not allowed in the build input: {rel}")
            if is_dir:
                stack.append(entry)
            elif nlink > 1:
                raise BuildError(f"hard link is not allowed in the build input: {rel}")


def machine_rows(payload) -> list[dict]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and isinstance(payload.get("machines"), list):
        rows = payload["machines"]
    else:
        raise BuildError("public machines JSON has an unsupported shape")
    if not all(isinstance(row, dict) for row in rows):
        raise BuildError("public machines contains a non-object row")
    return rows


def safe_clear(path: Path) -> None:
    """★_site / _site.next 以外は絶対に消さない★"""
    resolved = path.resolve()
    if resolved not in {OUT.resolve(), NEXT.resolve()}:
        raise BuildError(f"refusing to remove unexpected path: {resolved}")
    if path.exists():
        shutil.rmtree(path)


# 改行を LF に揃えて入れる拡張子（Windowsで作ってもCIで作っても同じ中身にするため）
TEXT_SUFFIXES = {".html", ".css", ".js", ".json", ".txt", ".xml", ".svg", ".webmanifest"}
TEXT_NAMES = {"CNAME"}

# assets/css・assets/img に置いてよい拡張子（これ以外があればビルドを止める）
# ★.svg は入れない★（Codex 14巡目 (a)-4）SVGはスクリプトを書ける＝能動コンテンツ。
#   いま使っているのは .png だけなので、必要になるまで許可しない。
ASSET_SUFFIXES = {".css", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".avif"}

# ★中身が名乗りどおりか確かめる★（同）`ledger.json` を `logo.png` にしても通さない。
IMAGE_MAGIC = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".webp": (b"RIFF",),
    ".ico": (b"\x00\x00\x01\x00", b"\x00\x00\x02\x00"),
    ".avif": (b"\x00\x00\x00",),
}


def is_text(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES or path.name in TEXT_NAMES


def copy_file(source: Path, target: Path) -> None:
    """1ファイルをコピーする。

    ★テキストは改行をLFへ揃える★（Codex 13巡目の観点）
      このPCは `core.autocrlf=true` なので作業ツリーの改行はCRLF、
      CIのLinuxではLFになる。そのまま入れると**同じコミットでも成果物の指紋が環境で変わる**。
      「2回作って同じ」を環境をまたいでも成り立たせるため、入れる時に揃える。
      画像などは1バイトも触らない。
    """
    if source.is_symlink():
        raise BuildError(f"refusing to copy a symlink: {source}")
    if not source.is_file():
        raise BuildError(f"required file is missing: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if is_text(source):
        data = source.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        target.write_bytes(data)
    else:
        shutil.copy2(source, target)


def copy_asset_dir(source: Path, target: Path) -> None:
    """assets/css・assets/img を、置いてよい拡張子だけコピーする。

    ★ディレクトリ単位で許可しない★（Codex 13巡目 (a)-2）
      丸ごと許可だと、そこに置かれた authoring-machines.json のようなファイルまで
      公開されてしまう。「入ってよい形」を列挙し、それ以外は失敗させる。
    """
    if not source.exists():
        return
    reject_symlinks(source)
    for src in sorted(p for p in source.rglob("*") if p.is_file()):
        rel = src.relative_to(source).as_posix()
        suffix = src.suffix.lower()
        if suffix not in ASSET_SUFFIXES:
            raise BuildError(f"unexpected file type under {source.name}/: {rel}")
        magic = IMAGE_MAGIC.get(suffix)
        if magic and not src.read_bytes()[:8].startswith(magic):
            raise BuildError(f"{rel}: 中身が {suffix} ではありません（拡張子の偽装）")
        copy_file(src, target / rel)


def copy_tree(source: Path, target: Path) -> None:
    if not source.is_dir():
        raise BuildError(f"required directory is missing: {source}")
    for src in sorted(p for p in source.rglob("*") if p.is_file()):
        copy_file(src, target / src.relative_to(source))
    if not target.exists():
        raise BuildError(f"required directory is empty: {source}")


def host_origin(work: Path) -> str:
    cname = (work / "CNAME").read_text(encoding="utf-8").strip()
    if not cname or "/" in cname:
        raise BuildError("CNAME is empty or malformed")
    return f"https://{cname}"


SETTING_PLACEHOLDER = """<!doctype html>
<html lang="ja"><head>
<base href="/">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>小役カウンター ポチポチくん | うちどころ。</title>
<link rel="stylesheet" href="assets/css/practical.css">
</head><body>
<main class="wrap">
<h1>準備中です</h1>
<p>小役カウンターの確率は出典の確認が済んでいないため、いまは公開していません。
確認ができ次第あらためて掲載します。</p>
<p><a href="index.html">トップページへ戻る</a></p>
</main>
</body></html>
"""


def write_setting_placeholder(stage: Path) -> None:
    (stage / "setting.html").write_text(SETTING_PLACEHOLDER, encoding="utf-8", newline="\n")


def write_sitemap(stage: Path, origin: str, slugs) -> None:
    """検索エンジンに知らせるURL一覧（★noindexの機種は入れない★）。"""
    locations = [origin + p for p in SITEMAP_STATIC]
    locations.extend(f"{origin}/machines/{slug}/" for slug in sorted(slugs))
    body = "\n".join(f"  <url><loc>{u}</loc></url>" for u in locations)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
           f"{body}\n</urlset>\n")
    (stage / "sitemap.xml").write_text(xml, encoding="utf-8", newline="\n")


def machine_links(text: str) -> list[str]:
    """本文から /machines/{slug}/ のリンクを取り出す。

    ★コメントを外してから見る★（Codex 13巡目 (b)-3）
      コメント内のリンクで「機種が載っている」ことにできてしまっていた。
    ★%エンコードは復号してから照合★
      `/%6dachines/zzz/` のような書き方で監査の目を逃れられないようにする。
    """
    body = HTML_COMMENT.sub("", text)
    decoded = urllib.parse.unquote(body)
    return MACHINE_HREF.findall(decoded)


def href_slugs(path: Path) -> set[str]:
    if not path.is_file():
        raise BuildError(f"required page is missing: {path.name}")
    links = machine_links(path.read_text(encoding="utf-8"))
    # ★同じ機種への重複リンクは黙って消さない★（同 (b)-3）
    dup = sorted({s for s in links if links.count(s) > 1})
    if dup:
        raise BuildError(f"{path.name}: duplicated machine links: {dup}")
    return set(links)


def template_sha(path: Path) -> str:
    """ひな型の指紋（改行差を吸収してから取る）。"""
    return hashlib.sha256(
        path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")).hexdigest()


def check_template_approved(work: Path) -> dict:
    """★全機種に一斉に効く入力が、承認済みのものと一致するか★

    （2026-07-30・Codex 15巡目 (a)-1 / 16巡目 (a)-1・(a)-2）
      ひな型に固定文を1行足すと全機種のページに載る。作り直して比べる検査は
      **同じひな型を使う**ので一致してしまう＝共通原因の故障。
      さらに、ひな型だけ固定しても **CSS・共通JS・Service Worker・生成器のコード**
      から同じことができる（`body::before{content:"…"}` など）。
      そこで「全機種に一斉に効く入力」を固定集合として列挙し、
      **過不足なく一致すること**を要求する（承認一覧から外して回避できないように）。
    """
    approval = read_json_dict(work / "assets/data/template-approval.json")
    if approval.get("schema_version") != APPROVAL_SCHEMA:
        raise BuildError(
            f"template-approval.json の schema_version が想定と違います"
            f"（想定 {APPROVAL_SCHEMA}／実際 {approval.get('schema_version')!r}）")
    want = approval.get("templates")
    if not isinstance(want, dict):
        raise BuildError("template-approval.json に templates（辞書）がありません")
    names = set(want)
    if names != set(APPROVED_INPUTS):
        missing = sorted(set(APPROVED_INPUTS) - names)
        extra = sorted(names - set(APPROVED_INPUTS))
        raise BuildError(
            f"承認一覧が固定集合と一致しません（不足: {missing} / 余分: {extra}）")
    got = {}
    for name in sorted(APPROVED_INPUTS):
        expected = want[name]
        if ".." in name or name.startswith("/") or ":" in name:
            raise BuildError(f"承認対象の書き方が不正です: {name}")
        path = work / name
        if not path.is_file():
            raise BuildError(f"承認対象のファイルがありません: {name}")
        actual = template_sha(path)
        if not isinstance(expected, str) or actual != expected:
            raise BuildError(
                f"{name} が承認済みの内容と違います（承認: {expected}／実際: {actual}）。"
                f"意図した変更なら assets/data/template-approval.json を更新すること"
                f"（python scripts/build_pages_artifact.py --approve）")
        got[name] = actual
    return got


def write_approval(base: Path = BASE) -> dict:
    """承認一覧を今の中身で作り直す（★変更をレビューに載せるための道具★）。"""
    data = {
        "schema_version": APPROVAL_SCHEMA,
        "note": ("全機種に一斉に効く入力の指紋。ここと実ファイルが一致する時だけ公開物を作れる。"
                 "中身を直したらこのファイルも更新すること（レビュー必須）。"),
        "templates": {name: template_sha(base / name) for name in sorted(APPROVED_INPUTS)},
    }
    path = base / "assets/data/template-approval.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8", newline="\n")
    return data["templates"]


def pages_match_data(stage: Path, template_path: Path, slugs: list[str]) -> None:
    """★出荷するページが「出荷するデータから作り直した物」と1バイトも違わないか★

    （2026-07-30・Codex 14巡目 (a)-1）
      前の方式（公開データを1本の文字列につないで部分一致を見る）は照合になっていなかった。
      文字を並べ替える・要素を分ける・検査対象のidを消す・属性に書く、で素通りできた。
      そこで **出荷するJSONだけを入れてページを作り直し、完全一致を要求する**。
      これで「ページに出ている物はすべて出荷データから決まる」が保証される
      （データ自体が正しいかはゲートと独立監査の担当）。
    """
    sys.path.insert(0, str(BASE / "scripts"))
    import build_machine_pages as bmp

    template = bmp.prepare_template(template_path.read_text(encoding="utf-8"))
    reasons = bmp.extract_pochipochi_reasons(template)
    machines = {row.get("slug"): row
                for row in machine_rows(read_json(stage / "assets/data/machines.json"))}
    diffs: list[str] = []
    for slug in slugs:
        page = (stage / "machines" / slug / "index.html").read_text(encoding="utf-8")
        detail_path = stage / "assets/data/machine-details" / f"{slug}.json"
        detail = read_json(detail_path) if detail_path.is_file() else None
        try:
            expected = bmp.render_page(template, machines[slug], detail, reasons,
                                       pochipochi_public=False)
        except Exception as exc:
            diffs.append(f"{slug}: 作り直せません（{type(exc).__name__}: {exc}）")
            continue
        if expected.replace("\r\n", "\n") != page.replace("\r\n", "\n"):
            diffs.append(f"{slug}: 出荷データから作り直した内容と一致しません")
    if diffs:
        # ★診断は打ち切らない★（Codex 14巡目 (b)-4）
        raise BuildError("pages do not match the shipped data:\n  " + "\n  ".join(diffs))


def normalise_text(s: str) -> str:
    """HTMLから取り出した文字列を、記事データと比べられる形に均す。

    ★エスケープを先に外してからタグを外す★
      逆順だと `&lt;br&gt;` が「文字としての <br>」として残り、
      記事データ側の `<br>`（タグとして除去済み）と食い違って
      **正しいページを誤って弾く**（実データ120機種中114機種で誤検知した）。
    """
    s = html_mod.unescape(s)
    s = TAG.sub(" ", s)
    s = s.replace("**", "")
    return re.sub(r"\s+", "", s)


def audit(stage: Path, expected: set[str]) -> None:
    """出来上がった物が「同じ機種集合だけ」でできているか確かめる。"""
    reject_symlinks(stage)

    manifest = read_json_dict(stage / "assets/data/published-slugs.json")
    if manifest.get("claim_gate_enabled") is not True:
        raise BuildError("artifact manifest is not fail-closed")
    if set(manifest.get("slugs") or []) != expected:
        raise BuildError("published manifest differs from approved slugs")

    machines = machine_rows(read_json(stage / "assets/data/machines.json"))
    if {row.get("slug") for row in machines} != expected:
        raise BuildError("public machines differs from approved slugs")

    details = {p.stem for p in (stage / "assets/data/machine-details").glob("*.json")}
    if details != expected:
        raise BuildError("public detail files differ from approved slugs")

    machines_dir = stage / "machines"
    if not machines_dir.is_dir():
        raise BuildError("machines directory is missing")
    directories = {p.name for p in machines_dir.iterdir()
                   if p.is_dir() and (p / "index.html").is_file()}
    stray = {p.name for p in machines_dir.iterdir()} - directories
    if stray:
        raise BuildError(f"unexpected entries under /machines/: {sorted(stray)}")
    if directories != expected:
        raise BuildError("machine page directories differ from approved slugs")

    # ★先行記事（noindex）は sitemap にも一覧にも載せない★（Codex 16巡目 (b)-1）
    #   機種ページには noindex を付けるのに sitemap には載せる、という食い違いがあった。
    preview = {row.get("slug") for row in machines if row.get("status") == "preview"}
    indexable = expected - preview

    sitemap_slugs = set(machine_links((stage / "sitemap.xml").read_text(encoding="utf-8")))
    if sitemap_slugs != indexable:
        raise BuildError(
            "sitemap が検索登録してよい機種の集合と違います"
            f"（余分: {sorted(sitemap_slugs - indexable)} / 不足: {sorted(indexable - sitemap_slugs)}）")

    # ページ単位の検査（本文の一致は pages_match_data が完全一致で見る）
    by_slug = {row.get("slug"): row for row in machines}
    sys.path.insert(0, str(BASE / "scripts"))
    import build_machine_pages as _bmp
    for slug in sorted(expected):
        page = (stage / "machines" / slug / "index.html").read_text(encoding="utf-8")
        # ★コメントを外してから見る／head内に1つだけ★（Codex 14巡目 (b)-3）
        head = HTML_COMMENT.sub("", page).split("</head>", 1)[0]
        bases = re.findall(r'<base\s+href\s*=\s*["\']([^"\']*)["\']', head, re.IGNORECASE)
        if bases != ["/"]:
            raise BuildError(
                f'machines/{slug}/index.html: <base href="/"> が head に1つだけ必要（実際: {bases}）')
        # ★「目安です」が必要な面すべてに、必要な回数だけ出ているか★（同 (a)-8）
        machine = by_slug.get(slug) or {}
        text, _sf = _bmp.disclaimer_of(machine)
        if text:
            want = len(_bmp.disclaimer_anchors(machine))
            visible = HTML_COMMENT.sub("", page)
            got = visible.count(f'<p class="site-disclaimer">{_bmp.esc(text)}</p>')
            if got != want:
                raise BuildError(
                    f"machines/{slug}/index.html: 「{text}」の併記が {want} 箇所必要ですが "
                    f"{got} 箇所です")
            # ★HTML側で隠していないか★（同 (a)-3）
            for attr in ("hidden", 'aria-hidden="true"', "style="):
                if re.search(r'<p class="site-disclaimer"[^>]*' + re.escape(attr), visible):
                    raise BuildError(
                        f"machines/{slug}/index.html: 併記が {attr} で隠されています")

    # ハブ（早見表を含む）は先行記事も載せる。分類の断定は yome() 側で避ける。
    # ★sitemap だけ「検索登録してよい機種」に限る★（Codex 17巡目 (b)-1）
    hub_union: set[str] = set()
    for name in GENERATED_HUBS:
        current = href_slugs(stage / name)
        if not current <= expected:
            raise BuildError(
                f"{name} に承認していない機種があります: {sorted(current - expected)}")
        hub_union |= current
    ichiran = href_slugs(stage / "guide-ichiran.html")
    if ichiran != expected:
        raise BuildError(
            "早見表が公開機種の集合と違います"
            f"（余分: {sorted(ichiran - expected)} / 不足: {sorted(expected - ichiran)}）")
    if hub_union != expected:
        raise BuildError(
            f"ハブ全体の機種集合が違います（不足: {sorted(expected - hub_union)}）")

    for rel in FORBIDDEN_PATHS:
        if (stage / rel).exists():
            raise BuildError(f"forbidden authoring path in artifact: {rel}")

    for css in sorted((stage / "assets/css").rglob("*.css")):
        for problem in css_problems(css.read_text(encoding="utf-8")):
            raise BuildError(f"{css.name}: {problem}")

    # ★HTMLだけでなくJS・SVG・JSONも見る／拡張子の大文字小文字も問わない★（同 (b)-3）
    for path in stage.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in MARKER_SCAN_SUFFIXES:
            continue
        rel = path.relative_to(stage).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise BuildError(f"text file is not valid UTF-8: {rel}")
        if PREVIEW_MARKER in text:
            raise BuildError(f"preview marker found: {rel}")
        if "assets/data/public/" in text:
            raise BuildError(f"internal public path referenced: {rel}")
        # 汎用URL（?slug= で別機種を出す旧経路）を artifact に残さない
        # （コメント内の記述は経路にならないので外してから見る）
        if "machine.html?slug=" in urllib.parse.unquote(HTML_COMMENT.sub("", text)):
            raise BuildError(f"generic machine URL referenced: {rel}")

    for path in stage.rglob("*"):
        if PREVIEW_DIRNAME in path.relative_to(stage).parts:
            raise BuildError(f"preview output inside artifact: {path}")


def git_dirty() -> bool:
    """作業ツリーがコミットと食い違っているか。"""
    cp = subprocess.run(["git", "status", "--porcelain"], cwd=BASE,
                        text=True, capture_output=True, check=False)
    if cp.returncode:
        return True
    return bool(cp.stdout.strip())


HIDE_DECLS = ("display:none", "visibility:hidden", "visibility:collapse", "opacity:0",
              "font-size:0", "content-visibility:hidden", "transform:scale(0)",
              "clip-path:inset(100%)", "max-height:0", "height:0", "width:0",
              # ★見えなくする手は他にもある★（Codex 17巡目 (a)-4）
              "color:transparent", "filter:opacity(0)", "-webkit-text-fill-color:transparent",
              "text-indent:-9999px", "clip:rect(0,0,0,0)", "font-size:0px")
CSS_ESCAPE = re.compile(r"\\([0-9a-fA-F]{1,6})\s?")

# ★記録済みの外部読み込み（Codex 16巡目 (a)-2 の指摘・未解消）★
#   見出し用の欧文フォントを Google Fonts から読んでいる。外部応答は指紋の外なので、
#   本来は**自前配信にするか、フォント自体をやめる**のが正しい。
#   フォントファイルの取得は運営者の判断が要るので、いまは「例外として記録」して
#   それ以外の外部読み込みを禁止する状態にしてある。
CSS_EXTERNAL_ALLOW = (
    "https://fonts.googleapis.com/css2?family=orbitron:wght@700;900&display=swap",
)


def css_rules(text: str):
    """CSSを「セレクタ」と「宣言」に分解する（入れ子にも対応した簡易パーサ）。

    ★正規表現での切り出しをやめた★（Codex 17巡目 (a)-4）
      `.x { display:none; & span { … } }` のような入れ子があると、
      外側の宣言を取り出せずに見逃していた。括弧を数えて自前で分ける。
    戻り値: (親セレクタを連ねたもの, 宣言文字列) の並び。
    """
    out = []
    stack: list[str] = []
    buf = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "{":
            stack.append(buf.strip())
            buf = ""
        elif ch == "}":
            if buf.strip():
                out.append((" ".join(stack), buf.strip()))
            buf = ""
            if stack:
                stack.pop()
        elif ch == ";" and stack:
            out.append((" ".join(stack), buf.strip()))
            buf = ""
        else:
            buf += ch
        i += 1
    if buf.strip() and stack:
        out.append((" ".join(stack), buf.strip()))
    return out


def _targets_disclaimer(selector: str) -> bool:
    """その規則が「併記そのもの」を対象にしているか。

    `:not(.site-disclaimer)` は対象外、`.site-disclaimer::before` は飾りなので対象外。
    （Codex 17巡目 (a)-4 の誤検知指摘）
    """
    sel = selector.replace(" ", "")
    if "site-disclaimer" not in sel:
        return False
    for part in re.split(r"[,]", sel):
        if "site-disclaimer" not in part:
            continue
        if re.search(r":not\([^)]*site-disclaimer", part):
            continue
        if re.search(r"site-disclaimer[^,]*::(before|after|marker|placeholder)", part):
            continue
        return True
    return False


def css_problems(text: str) -> list[str]:
    """CSSに「見せない仕掛け」「外部読み込み」「文字を生やす指定」が無いか。

    （Codex 15巡目 (a)-3 / 16巡目 (a)-2・(a)-3 / 17巡目 (a)-4）
      ・`.site-disclaimer` を隠されたら併記の意味が無い
      ・`content:` は**CSSから文章を生やせる**＝HTML検査の外で誤情報を出せる
      ・外部の読み込みは実行時の中身が指紋の外
    """
    problems: list[str] = []
    body = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    # CSSエスケープ（\64 → d、\i → i）を戻す
    body = CSS_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), body)
    body = re.sub(r"\\(.)", r"\1", body)

    # 外部読み込み（url(...) と @import "..." の両方）
    externals = [m.group(1).strip("\"' ") for m in re.finditer(r"url\(([^)]*)\)", body)]
    externals += [m.group(1) for m in re.finditer(r'@import\s+["\']([^"\']+)["\']', body)]
    for url in externals:
        if not re.match(r"^(?:https?:)?//", url.strip()):
            continue
        if url.strip().lower() in CSS_EXTERNAL_ALLOW:
            continue        # ★記録済みの例外（定義のコメント参照）★
        problems.append(f"承認していない外部URLを読み込んでいます « {url[:70]} »")

    rules = css_rules(body)
    # 変数（--名前）の値を集めておき、var(--名前) は中身に置き換えてから判定する。
    # ★同じCSSの中で定義された変数なら、実際の値で判断できる★（Codex 17巡目 (a)-4 の誤検知対策）
    variables = {}
    for _sel, decl in rules:
        m = re.match(r"\s*(--[\w-]+)\s*:\s*(.+)$", decl, re.DOTALL)
        if m:
            variables[m.group(1)] = m.group(2).strip()

    def resolve(value: str) -> str:
        for _ in range(3):      # 入れ子の var() も数段だけ辿る
            def sub(m):
                name = m.group(1).strip()
                return variables.get(name, m.group(0))
            new = re.sub(r"var\(\s*(--[\w-]+)\s*(?:,[^)]*)?\)", sub, value)
            if new == value:
                break
            value = new
        return value

    for selector, decl in rules:
        resolved = resolve(decl)
        flat = re.sub(r"\s+", "", resolved).lower()
        if _targets_disclaimer(selector):
            for hidden in HIDE_DECLS:
                if hidden in flat:
                    problems.append(f"併記（.site-disclaimer）を隠しています « {hidden} »")
            if re.search(r"(display|visibility|opacity|font-size|color|filter):var\(", flat):
                problems.append(
                    f"併記の表示指定に、中身の分からない変数を使っています « {decl[:40]} »")
        # ★CSSから「文章」を生やせないようにする★
        #   箇条書きの記号（"— " など）は文字も数字も含まないので通す。
        if flat.startswith("content:"):
            value = decl.split(":", 1)[1].strip()
            if re.search(r"\b(var|attr|counter|counters)\s*\(", value):
                problems.append(f"content に変数・属性を使っています « {selector[:30]} »")
            for cm in re.finditer(r'"([^"]*)"|\'([^\']*)\'', value):
                literal = cm.group(1) if cm.group(1) is not None else cm.group(2)
                if re.search(r"[0-9A-Za-z぀-ヿ一-鿿]", literal or ""):
                    problems.append(
                        f"CSSから文字を生やしています « {selector[:30]} → {literal[:30]} »")
    return problems


def write_artifact_manifest(stage: Path, template_hashes: dict | None = None) -> None:
    in_ci = bool(os.environ.get("GITHUB_SHA"))
    source_sha = os.environ.get("GITHUB_SHA", "")
    if not source_sha:
        cp = subprocess.run(["git", "rev-parse", "HEAD"], cwd=BASE,
                            text=True, capture_output=True, check=True)
        source_sha = cp.stdout.strip()

    # ★「このコミットから作った」と言えるのは作業ツリーが綺麗な時だけ★
    #   （Codex 13巡目 (b)-5）中身は手元の変更なのに source_commit は HEAD、
    #   という食い違いを黙って記録しない。CIでは失敗させ、手元では印を残す。
    dirty = git_dirty()
    if dirty and in_ci:
        raise BuildError("working tree is dirty in CI; artifact would not match the commit")

    files: dict[str, str] = {}
    for path in sorted(p for p in stage.rglob("*") if p.is_file()):
        rel = path.relative_to(stage).as_posix()
        if rel == "artifact-manifest.json":
            continue
        files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()

    canonical = json.dumps(files, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    payload = {
        "schema_version": 1,
        "source_commit": source_sha,
        "source_dirty": dirty,
        # ★成果物に入らないが出来上がりを決めるもの★（Codex 15巡目 (a)-1）
        #   ひな型は artifact に入れないので、成果物だけを見ても再現できない。
        #   何から作ったかを残す。
        "template_sha256": template_hashes or {},
        "content_sha256": hashlib.sha256(canonical).hexdigest(),
        "files": files,
    }
    (stage / "artifact-manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")


def build() -> int:
    # ★辞書でない設定は診断つきで止める★（Codex 14巡目 (b)-2）
    gate = read_json_dict(BASE / "assets/data/claim-gate.json")
    if gate.get("enabled") is not True:
        raise BuildError("claim gate is not enabled")

    safe_clear(NEXT)

    # ★★入力の時点でリンクを拒否する★★（Codex 14巡目 (a)-4）
    #   copytree はリンク先を実体としてコピーするので、
    #   コピーした後に調べても「ただのファイル」になっていて見つけられない。
    reject_symlinks(BASE, ignore=IGNORED_DIR_NAMES)

    with tempfile.TemporaryDirectory(prefix="uchidokoro-source-") as tmp:
        work = Path(tmp) / "repo"
        shutil.copytree(BASE, work, ignore=SOURCE_IGNORE)
        if (work / PREVIEW_DIRNAME).exists():
            raise BuildError("preview output leaked into the build workspace")

        template_hashes = check_template_approved(work)

        run(work, "scripts/build_public_data.py", "--apply")
        run(work, "scripts/build_machine_pages.py")
        run(work, "scripts/build_hub_pages.py")

        public_root = work / "assets/data/public"
        public_machines = public_root / "machines.public.json"
        public_details = public_root / "machine-details"

        rows = machine_rows(read_json(public_machines))
        slugs = sorted(row["slug"] for row in rows
                       if isinstance(row.get("slug"), str) and row["slug"])
        if len(slugs) != len(rows) or len(set(slugs)) != len(slugs):
            raise BuildError("public machines has a missing or duplicate slug")
        if not slugs:
            raise BuildError("zero publishable machines")

        NEXT.mkdir(parents=True)

        for name in (*ROOT_FILES, *GENERATED_HUBS):
            copy_file(work / name, NEXT / name)

        for name in ROOT_ASSETS:
            copy_file(work / name, NEXT / name)

        # ★ディレクトリ単位で許可しない★（Codex 13巡目 (a)-2）
        #   assets/img を丸ごと入れる方式だと、そこに置かれた
        #   authoring-machines.json のようなファイルまで公開されてしまう。
        #   置いてよい拡張子を列挙し、それ以外は失敗させる。
        for dirname in ("css", "img"):
            copy_asset_dir(work / "assets" / dirname, NEXT / "assets" / dirname)

        copy_file(public_machines, NEXT / "assets/data/machines.json")
        copy_tree(public_details, NEXT / "assets/data/machine-details")

        for slug in slugs:
            copy_file(work / "machines" / slug / "index.html",
                      NEXT / "machines" / slug / "index.html")

        published_path = NEXT / "assets/data/published-slugs.json"
        published_path.parent.mkdir(parents=True, exist_ok=True)
        published_path.write_text(json.dumps(
            {"schema_version": "published-slugs/v1",
             "claim_gate_enabled": True,
             "slugs": slugs}, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8", newline="\n")

        write_setting_placeholder(NEXT)
        preview_slugs = {row.get("slug") for row in rows if row.get("status") == "preview"}
        if preview_slugs:
            print(f"先行記事（noindex）{len(preview_slugs)} 機種は sitemap と一覧に載せません: "
                  f"{sorted(preview_slugs)}")
        write_sitemap(NEXT, host_origin(work), set(slugs) - preview_slugs)
        audit(NEXT, set(slugs))
        # ★出荷データから作り直して1バイトも違わないことを確かめる★（Codex 14巡目 (a)-1）
        pages_match_data(NEXT, work / "machine.html", slugs)
        # ★ひな型の指紋も記録する★（成果物だけでは再現できない依存を残す）
        write_artifact_manifest(NEXT, template_hashes)

    safe_clear(OUT)
    NEXT.replace(OUT)
    print(f"artifact: {OUT}（{len(slugs)} 機種）")
    return 0


# ---------------------------------------------------------------- selftest
PNG_MAGIC = b"\x89PNG\r\n\x1a\nrest"

PAGE_TPL = ('<html><head><base href="/"></head><body>'
            '<h1 id="machineTitle" class="page-title">{name}</h1>'
            '<p id="heroSub" class="hero-sub">{lead}</p>'
            "</body></html>")


def _stage_ok(root: Path, slugs=("aaa", "bbb")) -> Path:
    """audit() を通る最小の成果物を作る（検査の反例を固定するための土台）。"""
    s = set(slugs)
    (root / "assets/data/machine-details").mkdir(parents=True, exist_ok=True)
    (root / "assets/data/published-slugs.json").write_text(json.dumps(
        {"schema_version": "published-slugs/v1", "claim_gate_enabled": True,
         "slugs": sorted(s)}), encoding="utf-8")
    (root / "assets/data/machines.json").write_text(json.dumps(
        [{"slug": x, "name": f"機種{x}"} for x in sorted(s)]), encoding="utf-8")
    for x in s:
        (root / f"assets/data/machine-details/{x}.json").write_text(
            json.dumps({"lead": f"{x}の説明"}, ensure_ascii=False), encoding="utf-8")
        d = root / "machines" / x
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.html").write_text(
            PAGE_TPL.format(name=f"機種{x}", lead=f"{x}の説明"), encoding="utf-8")
    links = "".join(f'<a href="/machines/{x}/">x</a>' for x in sorted(s))
    for name in GENERATED_HUBS:
        (root / name).write_text(f"<html><body>{links}</body></html>", encoding="utf-8")
    write_sitemap(root, "https://uchidokoro.com", sorted(s))
    return root


def selftest() -> int:
    import traceback
    cases: list[tuple[str, callable, bool]] = []

    def case(name, fn, should_pass=False):
        cases.append((name, fn, should_pass))

    def denies(mutate):
        def run_case(root):
            _stage_ok(root)
            mutate(root)
            try:
                audit(root, {"aaa", "bbb"})
            except BuildError:
                return True
            return False
        return run_case

    case("正常な成果物は通る", lambda root: (_stage_ok(root),
         audit(root, {"aaa", "bbb"}) or True)[1], True)

    case("名簿がゲート無効なら止める", denies(
        lambda r: (r / "assets/data/published-slugs.json").write_text(json.dumps(
            {"claim_gate_enabled": False, "slugs": ["aaa", "bbb"]}), encoding="utf-8")))
    case("名簿に余分な機種があれば止める", denies(
        lambda r: (r / "assets/data/published-slugs.json").write_text(json.dumps(
            {"claim_gate_enabled": True, "slugs": ["aaa", "bbb", "ccc"]}), encoding="utf-8")))
    case("公開machinesがずれたら止める", denies(
        lambda r: (r / "assets/data/machines.json").write_text(
            json.dumps([{"slug": "aaa"}]), encoding="utf-8")))
    case("記事ファイルが余ったら止める", denies(
        lambda r: (r / "assets/data/machine-details/ccc.json").write_text("{}", encoding="utf-8")))
    case("機種フォルダが足りなければ止める", denies(
        lambda r: shutil.rmtree(r / "machines/bbb")))
    case("machines配下にファイルが紛れたら止める", denies(
        lambda r: (r / "machines/stray.html").write_text("x", encoding="utf-8")))
    case("sitemapがずれたら止める", denies(
        lambda r: write_sitemap(r, "https://uchidokoro.com", ["aaa"])))
    case("(b)-1 先行記事はsitemapと一覧から外す（載っていたら止める）",
         lambda root: _preview_excluded(root), True)
    case("ハブに未承認の機種が出たら止める", denies(
        lambda r: (r / "guide-tenjo-ranking.html").write_text(
            '<a href="/machines/zzz/">x</a>', encoding="utf-8")))
    case("一覧ページに機種が足りなければ止める", denies(
        lambda r: (r / "guide-ichiran.html").write_text(
            '<a href="/machines/aaa/">x</a>', encoding="utf-8")))
    case("ハブが1枚欠けたら止める", denies(
        lambda r: (r / "guide-suru-tenjo.html").unlink()))
    case("編集用machine.htmlが入ったら止める", denies(
        lambda r: (r / "machine.html").write_text("<html></html>", encoding="utf-8")))
    case("台帳が入ったら止める", denies(
        lambda r: (r / "assets/data/ledger.json").write_text("{}", encoding="utf-8")))
    case("ゲート設定が入ったら止める", denies(
        lambda r: (r / "assets/data/claim-gate.json").write_text("{}", encoding="utf-8")))
    case("証拠フォルダが入ったら止める", denies(
        lambda r: (r / "assets/data/claim-evidence").mkdir(parents=True)))
    case("出典レジストリが入ったら止める", denies(
        lambda r: (r / "assets/data/source-registry.json").write_text("{}", encoding="utf-8")))
    case("scriptsが入ったら止める", denies(
        lambda r: (r / "scripts").mkdir()))
    case("写しの目印があれば止める", denies(
        lambda r: (r / "machines/aaa/index.html").write_text(
            f"<html><!-- {PREVIEW_MARKER} --></html>", encoding="utf-8")))
    case("写しフォルダが混ざったら止める", denies(
        lambda r: (r / PREVIEW_DIRNAME).mkdir()))
    case("内部の公開パスを参照したら止める", denies(
        lambda r: (r / "machines/aaa/index.html").write_text(
            '<html><script>fetch("assets/data/public/machines.public.json")</script></html>',
            encoding="utf-8")))
    case("旧形式の汎用URLが残ったら止める", denies(
        lambda r: (r / "machines/aaa/index.html").write_text(
            '<html><a href="machine.html?slug=bbb">x</a></html>', encoding="utf-8")))
    case("名簿が壊れたJSONなら止める", denies(
        lambda r: (r / "assets/data/published-slugs.json").write_text("{", encoding="utf-8")))
    case("公開machinesが配列でも辞書でもなければ止める", denies(
        lambda r: (r / "assets/data/machines.json").write_text("42", encoding="utf-8")))
    case("公開machinesに非オブジェクトが混ざれば止める", denies(
        lambda r: (r / "assets/data/machines.json").write_text(
            json.dumps([{"slug": "aaa"}, "bbb"]), encoding="utf-8")))
    # --- Codex 13・14巡目の反例をここに固定する ---
    case("(a)-1 出荷データから作り直した内容と一致すれば通る",
         lambda root: _rebuild_check(root, None), True)
    case("(a)-1 本文に1文を混ぜたら止める",
         lambda root: _rebuild_check(
             root, lambda h: h.replace("</body>", "<p>未公開機の天井は999G</p></body>")), True)
    case("(a)-1 数字を並べ替えて分割しても止める",
         lambda root: _rebuild_check(
             root, lambda h: h.replace(
                 '<p id="heroSub" class="hero-sub">説明1234G</p>',
                 '<p id="heroSub" class="hero-sub">説明<span>4</span>'
                 '<span>3</span><span>2</span><span>1</span>G</p>')), True)
    case("(a)-1 検査対象のidを変えても止める",
         lambda root: _rebuild_check(
             root, lambda h: h.replace('id="machineTitle"', 'id="machineTitleX"')), True)
    case("(a)-1 属性に文字を仕込んでも止める",
         lambda root: _rebuild_check(
             root, lambda h: h.replace("<body", '<body data-x="天井999G"', 1)), True)
    # --- Codex 15巡目の反例 ---
    case("(a)-1 ひな型に固定文を足したら止める（共通原因の故障）",
         lambda root: _template_change_stopped(root), True)
    case("(a)-1 差し込み先が消えたら黙って通さない",
         lambda root: _missing_anchor_stopped(), True)
    case("(a)-1 承認一覧から対象を外す／余分を足すと止める",
         lambda root: _approval_scope_enforced(root), True)
    case("(a)-2 CSSから文字を生やす指定を止める",
         lambda root: bool(css_problems('body::before{content:"未公開機は999G"}')), True)
    case("(a)-2 承認外の外部読み込みを止める",
         lambda root: bool(css_problems("@import url('https://evil.example/x.css');")), True)
    case("(a)-3 @media の中で隠しても止める",
         lambda root: bool(css_problems(
             "@media(min-width:0){.site-disclaimer{display:none!important}}")), True)
    case("(a)-3 変数経由で隠しても止める",
         lambda root: bool(css_problems(
             ":root{--h:none}.site-disclaimer{display:var(--h)}")), True)
    case("(a)-3 CSSエスケープで名前を隠しても止める",
         lambda root: bool(css_problems(r".site-\64 isclaimer{display:none}")), True)
    case("(a)-3 ふつうの箇条書き記号では止めない",
         lambda root: not css_problems('.x li::before{content:"— "}'), True)
    case("(a)-3 いまのCSSは問題なしと判定される",
         lambda root: not css_problems(
             (BASE / "assets/css/practical.css").read_text(encoding="utf-8")), True)
    case("(a)-3 併記を隠すCSSがあれば止める", denies(
        lambda r: (r / "assets/css/practical.css").parent.mkdir(parents=True, exist_ok=True)
        or (r / "assets/css/practical.css").write_text(
            ".site-disclaimer{display:none}", encoding="utf-8")))
    case("(b)-6 base href が無ければ止める", denies(
        lambda r: (r / "machines/aaa/index.html").write_text(
            '<html><head></head><body><h1 id="machineTitle" class="page-title">機種aaa</h1>'
            '<p id="heroSub" class="hero-sub">aaaの説明</p></body></html>', encoding="utf-8")))
    case("(b)-4 必須の併記が画面に無ければ止める", denies(
        lambda r: (r / "assets/data/machines.json").write_text(json.dumps(
            [{"slug": "aaa", "name": "機種aaa",
              "display_requirements": {"disclaimer": "当サイトの目安です"}},
             {"slug": "bbb", "name": "機種bbb"}], ensure_ascii=False), encoding="utf-8")))
    case("(b)-3 コメント内のリンクでは機種が載ったことにならない", denies(
        lambda r: (r / "guide-ichiran.html").write_text(
            '<a href="/machines/aaa/">x</a><!-- <a href="/machines/bbb/">x</a> -->',
            encoding="utf-8")))
    case("(b)-3 %エンコードした未承認リンクも見つける", denies(
        lambda r: (r / "guide-tenjo-ranking.html").write_text(
            '<a href="/machines/aaa/">x</a><a href="/machines/bbb/">x</a>'
            '<a href="/%6Dachines/zzz/">x</a>', encoding="utf-8")))
    case("(b)-3 同じ機種への重複リンクを見逃さない", denies(
        lambda r: (r / "guide-ichiran.html").write_text(
            '<a href="/machines/aaa/">x</a><a href="/machines/aaa/">x</a>'
            '<a href="/machines/bbb/">x</a>', encoding="utf-8")))
    case("(b)-3 JSONの同名キー重複を通さない", denies(
        lambda r: (r / "assets/data/published-slugs.json").write_text(
            '{"claim_gate_enabled": true, "slugs": ["aaa","bbb"], "slugs": ["aaa","bbb"]}',
            encoding="utf-8")))
    case("(b)-3 大文字拡張子・JSの写し目印も見つける", denies(
        lambda r: (r / "assets/x.JS").parent.mkdir(parents=True, exist_ok=True)
        or (r / "assets/x.JS").write_text(f"// {PREVIEW_MARKER}", encoding="utf-8")))
    case("(b)-3 辞書でない claim-gate.json は例外にせず止める",
         lambda root: _raises(lambda: read_json_dict(_write(root / "g.json", "[]"))))
    case("(a)-2 成果物にsymlinkがあれば止める", denies(_add_symlink))
    case("(a)-2 許可ディレクトリ内のsymlinkはコピーしない",
         lambda root: _symlink_rejected(root), True)
    case("(a)-2 assets配下の想定外ファイルは止める（画像は通る）",
         lambda root: _asset_suffix_rejected(root), True)
    case("(a)-4 拡張子の偽装（中身がJSONのpng）を止める",
         lambda root: _magic_spoof_rejected(root), True)
    case("(a)-4 SVGは許可しない（能動コンテンツ）",
         lambda root: _svg_rejected(), True)

    case("_site以外を消そうとしたら止める",
         lambda root: _raises(lambda: safe_clear(root / "machines")))
    case("読めないCNAMEは止める",
         lambda root: _raises(lambda: host_origin(_cname(root, " "))))
    case("パス付きCNAMEは止める",
         lambda root: _raises(lambda: host_origin(_cname(root, "example.com/x"))))
    case("成果物の指紋は2回とも同じ",
         lambda root: _hash_twice(root), True)
    case("CRLFで入れてもLFに揃う（指紋が環境で変わらない）",
         lambda root: _crlf_normalised(root), True)
    case("画像は1バイトも変えずに入る",
         lambda root: _binary_untouched(root), True)

    ok = 0
    for name, fn, _ in cases:
        with tempfile.TemporaryDirectory(prefix="artifact-selftest-") as td:
            root = Path(td)
            try:
                result = fn(root)
            except Exception:
                print(f"  ✗ {name}: 例外")
                traceback.print_exc()
                continue
        if result is True:
            ok += 1
        else:
            print(f"  ✗ {name}")
    print(f"{ok}/{len(cases)} 合格")
    return 0 if ok == len(cases) else 1


def _rebuild_check(root: Path, tamper) -> bool:
    """実物の machine.html を使って「作り直して一致するか」を確かめる。

    tamper=None なら一致して通ること、tamper があれば必ず止まることを期待する。
    """
    sys.path.insert(0, str(BASE / "scripts"))
    import build_machine_pages as bmp

    machine = {"slug": "aaa", "name": "機種aaa"}
    detail = {"lead": "説明1234G"}
    (root / "assets/data/machine-details").mkdir(parents=True, exist_ok=True)
    (root / "assets/data/machines.json").write_text(
        json.dumps([machine], ensure_ascii=False), encoding="utf-8")
    (root / "assets/data/machine-details/aaa.json").write_text(
        json.dumps(detail, ensure_ascii=False), encoding="utf-8")

    template_path = BASE / "machine.html"
    template = bmp.prepare_template(template_path.read_text(encoding="utf-8"))
    reasons = bmp.extract_pochipochi_reasons(template)
    page = bmp.render_page(template, machine, detail, reasons, pochipochi_public=False)
    if tamper:
        page = tamper(page)
    out = root / "machines/aaa/index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8", newline="\n")

    try:
        pages_match_data(root, template_path, ["aaa"])
        stopped = False
    except BuildError:
        stopped = True
    return stopped if tamper else not stopped


def _preview_excluded(root: Path) -> bool:
    """先行記事は sitemap から外す（一覧には載る）。載せたら止まること。"""
    _stage_ok(root)
    # bbb を先行記事にする（ハブには両方載る／sitemap は aaa だけ）
    (root / "assets/data/machines.json").write_text(json.dumps(
        [{"slug": "aaa", "name": "機種aaa"},
         {"slug": "bbb", "name": "機種bbb", "status": "preview"}],
        ensure_ascii=False), encoding="utf-8")
    write_sitemap(root, "https://uchidokoro.com", ["aaa"])
    try:
        audit(root, {"aaa", "bbb"})          # sitemap から外した状態なら通る
    except BuildError:
        return False
    write_sitemap(root, "https://uchidokoro.com", ["aaa", "bbb"])   # 載せたら止まる
    return _raises(lambda: audit(root, {"aaa", "bbb"}))


def _approval_fixture(root: Path) -> Path:
    """承認対象を全部そろえた作業コピーを作る。"""
    work = root / "repo"
    for name in sorted(APPROVED_INPUTS):
        dst = work / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(BASE / name, dst)
    (work / "assets/data").mkdir(parents=True, exist_ok=True)
    write_approval(work)
    return work


def _template_change_stopped(root: Path) -> bool:
    """ひな型に固定文を1行足したら、承認の照合で止まること。"""
    work = _approval_fixture(root)
    if not check_template_approved(work):          # まずは一致して通ること
        return False
    tpl = work / "machine.html"
    tpl.write_text(tpl.read_text(encoding="utf-8").replace(
        "</body>", "<p>未公開機の天井は999Gです</p></body>"), encoding="utf-8", newline="\n")
    return _raises(lambda: check_template_approved(work))


def _approval_scope_enforced(root: Path) -> bool:
    """承認一覧から対象を外す／余分を足す／schemaを変えると止まること。"""
    work = _approval_fixture(root)
    path = work / "assets/data/template-approval.json"
    full = json.loads(path.read_text(encoding="utf-8"))

    def with_templates(templates, schema=APPROVAL_SCHEMA):
        path.write_text(json.dumps({"schema_version": schema, "templates": templates},
                                   ensure_ascii=False), encoding="utf-8")
        return _raises(lambda: check_template_approved(work))

    dropped = {k: v for k, v in full["templates"].items() if k != "machine.html"}
    extra = {**full["templates"], "about.html": "x"}
    return (with_templates(dropped)
            and with_templates(extra)
            and with_templates(full["templates"], schema="template-approval/v1")
            and with_templates({}))


def _missing_anchor_stopped() -> bool:
    """差し込み先が消えたひな型で、黙って通さず止まること。"""
    sys.path.insert(0, str(BASE / "scripts"))
    import build_machine_pages as bmp
    broken = bmp.prepare_template(
        (BASE / "machine.html").read_text(encoding="utf-8")
    ).replace('<h1 id="machineTitle" class="page-title">機種名</h1>',
              '<h1 id="machineTitle" class="page-title">天井は999Gです</h1>')
    try:
        bmp.render_page(broken, {"slug": "aaa", "name": "機種aaa"}, {"lead": "説明"},
                        {}, pochipochi_public=False)
        return False
    except bmp.TemplateError:
        return True


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _add_symlink(root: Path) -> None:
    """成果物の中にsymlinkを1つ作る（作れない環境では代わりに禁止パスを置く）。"""
    target = root / "assets/data/machines.json"
    link = root / "assets/img/authoring.json"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        # 権限が無い環境では symlink を作れないので、同じ検査対象の別条件で代替する
        (root / "machine.html").write_text("<html></html>", encoding="utf-8")


def _asset_suffix_rejected(root: Path) -> bool:
    """assets 配下に許可外のファイルがあれば実際に止まること。"""
    src = root / "img"
    src.mkdir(parents=True)
    (src / "logo.png").write_bytes(PNG_MAGIC)
    (src / "authoring-machines.json").write_text("{}", encoding="utf-8")
    if not _raises(lambda: copy_asset_dir(src, root / "out")):
        return False
    (src / "authoring-machines.json").unlink()
    copy_asset_dir(src, root / "out2")
    return (root / "out2/logo.png").is_file()


def _symlink_rejected(root: Path) -> bool:
    """許可ディレクトリ内のsymlinkを実際に拒否できること（作れない環境ではスキップ扱い）。"""
    src = root / "img"
    src.mkdir(parents=True)
    (src / "logo.png").write_bytes(PNG_MAGIC)
    secret = root / "secret.json"
    secret.write_text("{}", encoding="utf-8")
    # リンク単体で拒否できることを先に確認（作れない環境では省略）
    try:
        (src / "leak.png").symlink_to(secret)
    except (OSError, NotImplementedError):
        print("  （このPCではsymlinkを作れないため実地テストは省略）")
        return True
    if not _raises(lambda: reject_symlinks(src)):
        return False
    if not _raises(lambda: copy_asset_dir(src, root / "out")):
        return False
    # リンクを外せば通ることも確かめる（別の理由で止まっていないか）
    (src / "leak.png").unlink()
    copy_asset_dir(src, root / "out2")
    return (root / "out2/logo.png").is_file()


def _magic_spoof_rejected(root: Path) -> bool:
    """拡張子を偽装したファイル（中身がJSONの logo.png）を弾けること。"""
    src = root / "img"
    src.mkdir(parents=True)
    (src / "logo.png").write_text('{"secret": 1}', encoding="utf-8")
    return _raises(lambda: copy_asset_dir(src, root / "out"))


def _svg_rejected() -> bool:
    """SVGは能動コンテンツを書けるので許可しない。"""
    return ".svg" not in ASSET_SUFFIXES


def _raises(fn) -> bool:
    try:
        fn()
    except BuildError:
        return True
    return False


def _cname(root: Path, text: str) -> Path:
    (root / "CNAME").write_text(text, encoding="utf-8")
    return root


def _crlf_normalised(root: Path) -> bool:
    src = root / "src"
    src.mkdir()
    (src / "a.html").write_bytes(b"<p>1</p>\r\n<p>2</p>\r\n")
    (src / "CNAME").write_bytes(b"uchidokoro.com\r\n")
    copy_file(src / "a.html", root / "out/a.html")
    copy_file(src / "CNAME", root / "out/CNAME")
    return (b"\r" not in (root / "out/a.html").read_bytes()
            and b"\r" not in (root / "out/CNAME").read_bytes()
            and host_origin(root / "out") == "https://uchidokoro.com")


def _binary_untouched(root: Path) -> bool:
    src = root / "src"
    src.mkdir()
    blob = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x0D, 0x0A])
    (src / "favicon.ico").write_bytes(blob)
    copy_file(src / "favicon.ico", root / "out/favicon.ico")
    return (root / "out/favicon.ico").read_bytes() == blob


def _hash_twice(root: Path) -> bool:
    _stage_ok(root)
    write_artifact_manifest(root)
    first = read_json(root / "artifact-manifest.json")["content_sha256"]
    (root / "artifact-manifest.json").unlink()
    write_artifact_manifest(root)
    second = read_json(root / "artifact-manifest.json")["content_sha256"]
    return first == second


def main() -> int:
    if "--selftest" in sys.argv[1:]:
        return selftest()
    if "--approve" in sys.argv[1:]:
        got = write_approval()
        print("承認一覧を更新しました（差分をレビューに載せること）:")
        for name, h in sorted(got.items()):
            print(f"  {name}  {h[:16]}…")
        return 0
    return build()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)

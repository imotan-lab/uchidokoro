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
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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

SOURCE_IGNORE = shutil.ignore_patterns(
    ".git", ".github", PREVIEW_DIRNAME, "_site", "_site.next",
    "__pycache__", "*.pyc", "_design", ".claude",
)


class BuildError(RuntimeError):
    pass


def run(work: Path, *args: str) -> None:
    cp = subprocess.run([sys.executable, *args], cwd=work, text=True, check=False)
    if cp.returncode:
        raise BuildError(f"command failed ({cp.returncode}): {' '.join(args)}")


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise BuildError(f"cannot read JSON: {path}: {exc}") from exc


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


def copy_file(source: Path, target: Path) -> None:
    if not source.is_file():
        raise BuildError(f"required file is missing: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def copy_tree(source: Path, target: Path) -> None:
    if not source.is_dir():
        raise BuildError(f"required directory is missing: {source}")
    shutil.copytree(source, target, dirs_exist_ok=True)


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


def write_sitemap(stage: Path, origin: str, slugs: list[str]) -> None:
    locations = [origin + p for p in SITEMAP_STATIC]
    locations.extend(f"{origin}/machines/{slug}/" for slug in slugs)
    body = "\n".join(f"  <url><loc>{u}</loc></url>" for u in locations)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
           f"{body}\n</urlset>\n")
    (stage / "sitemap.xml").write_text(xml, encoding="utf-8", newline="\n")


def href_slugs(path: Path) -> set[str]:
    if not path.is_file():
        raise BuildError(f"required page is missing: {path.name}")
    return set(MACHINE_HREF.findall(path.read_text(encoding="utf-8")))


def audit(stage: Path, expected: set[str]) -> None:
    """出来上がった物が「同じ機種集合だけ」でできているか確かめる。"""
    manifest = read_json(stage / "assets/data/published-slugs.json")
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

    sitemap_slugs = set(MACHINE_HREF.findall(
        (stage / "sitemap.xml").read_text(encoding="utf-8")))
    if sitemap_slugs != expected:
        raise BuildError("sitemap differs from approved slugs")

    hub_union: set[str] = set()
    for name in GENERATED_HUBS:
        current = href_slugs(stage / name)
        if not current <= expected:
            raise BuildError(f"{name} contains a non-approved slug")
        hub_union |= current
    if href_slugs(stage / "guide-ichiran.html") != expected:
        raise BuildError("guide-ichiran differs from approved slugs")
    if hub_union != expected:
        raise BuildError("hub union differs from approved slugs")

    for rel in FORBIDDEN_PATHS:
        if (stage / rel).exists():
            raise BuildError(f"forbidden authoring path in artifact: {rel}")

    for html in stage.rglob("*.html"):
        text = html.read_text(encoding="utf-8")
        rel = html.relative_to(stage).as_posix()
        if PREVIEW_MARKER in text:
            raise BuildError(f"preview marker found: {rel}")
        if "assets/data/public/" in text:
            raise BuildError(f"internal public path referenced: {rel}")
        # 汎用URL（?slug= で別機種を出す旧経路）を artifact に残さない
        if "machine.html?slug=" in text:
            raise BuildError(f"generic machine URL referenced: {rel}")

    for path in stage.rglob("*"):
        if PREVIEW_DIRNAME in path.relative_to(stage).parts:
            raise BuildError(f"preview output inside artifact: {path}")


def write_artifact_manifest(stage: Path) -> None:
    source_sha = os.environ.get("GITHUB_SHA", "")
    if not source_sha:
        cp = subprocess.run(["git", "rev-parse", "HEAD"], cwd=BASE,
                            text=True, capture_output=True, check=True)
        source_sha = cp.stdout.strip()

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
        "content_sha256": hashlib.sha256(canonical).hexdigest(),
        "files": files,
    }
    (stage / "artifact-manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")


def build() -> int:
    gate = read_json(BASE / "assets/data/claim-gate.json")
    if gate.get("enabled") is not True:
        raise BuildError("claim gate is not enabled")

    safe_clear(NEXT)

    with tempfile.TemporaryDirectory(prefix="uchidokoro-source-") as tmp:
        work = Path(tmp) / "repo"
        shutil.copytree(BASE, work, ignore=SOURCE_IGNORE)
        if (work / PREVIEW_DIRNAME).exists():
            raise BuildError("preview output leaked into the build workspace")

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

        for pattern in ("favicon.*", "apple-touch-icon*", "google*.html"):
            for source in sorted(work.glob(pattern)):
                if source.is_file():
                    copy_file(source, NEXT / source.name)

        for dirname in ("css", "img"):
            source = work / "assets" / dirname
            if source.exists():
                copy_tree(source, NEXT / "assets" / dirname)

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
        write_sitemap(NEXT, host_origin(work), slugs)
        audit(NEXT, set(slugs))
        write_artifact_manifest(NEXT)

    safe_clear(OUT)
    NEXT.replace(OUT)
    print(f"artifact: {OUT}（{len(slugs)} 機種）")
    return 0


# ---------------------------------------------------------------- selftest
def _stage_ok(root: Path, slugs=("aaa", "bbb")) -> Path:
    """audit() を通る最小の成果物を作る（検査の反例を固定するための土台）。"""
    s = set(slugs)
    (root / "assets/data/machine-details").mkdir(parents=True, exist_ok=True)
    (root / "assets/data/published-slugs.json").write_text(json.dumps(
        {"schema_version": "published-slugs/v1", "claim_gate_enabled": True,
         "slugs": sorted(s)}), encoding="utf-8")
    (root / "assets/data/machines.json").write_text(json.dumps(
        [{"slug": x} for x in sorted(s)]), encoding="utf-8")
    for x in s:
        (root / f"assets/data/machine-details/{x}.json").write_text("{}", encoding="utf-8")
        d = root / "machines" / x
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.html").write_text("<html><body>ok</body></html>", encoding="utf-8")
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
    case("_site以外を消そうとしたら止める",
         lambda root: _raises(lambda: safe_clear(root / "machines")))
    case("読めないCNAMEは止める",
         lambda root: _raises(lambda: host_origin(_cname(root, " "))))
    case("パス付きCNAMEは止める",
         lambda root: _raises(lambda: host_origin(_cname(root, "example.com/x"))))
    case("成果物の指紋は2回とも同じ",
         lambda root: _hash_twice(root), True)

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


def _raises(fn) -> bool:
    try:
        fn()
    except BuildError:
        return True
    return False


def _cname(root: Path, text: str) -> Path:
    (root / "CNAME").write_text(text, encoding="utf-8")
    return root


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
    return build()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)

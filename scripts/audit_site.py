"""
サイト構造整合性チェックスクリプト
verifyタスク（毎日5:05）から呼ばれる。8項目をチェックしてNG項目を標準出力に出す。

NGがあれば exit code 1。メール通知や自動修正は呼び出し側のSKILL.mdで判定。

使い方:
    python scripts/audit_site.py [--json]

オプション:
    --json: JSON形式で出力（人間可読がデフォルト）

チェック項目:
    1. machine.html にインラインstyle（style="..."）が無いか
    2. サイト内コードに /uchidokoro/ サブパス残骸が無いか
    3. machines.json の info 表記ゆれ（疑/擬・スペース有無）
    4. canonical / og:url / sitemap の3点整合性
    5. service-worker.js の STATIC_CACHE が全て実在
    6. machines.json と machines/{slug}/index.html / machine-details/{slug}.json の整合性
    7. sitemap.xml の機種URL件数と machines.json 件数の一致＋重複検知
    8. README.md の機種数記載と実数の一致

AdSense審査向け（コンテンツ品質）:
    9. machine-details の本文文字数（先行記事除いて1500字以上）
    10. 必須法的ページ（about/privacy/contact）の本文文字量（500字以上）
    11. メタディスクリプションが全HTMLにあり50〜160字
    12. 全HTMLの <img> に alt属性
    13. HTML内の内部リンクが実在ファイルを指しているか
"""

from __future__ import annotations
import json
import os
import re
import sys
from pathlib import Path

# Windows のcp932 ターミナルでも絵文字を出せるようにUTF-8で出力
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent


def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def check_1_inline_style(machines: list) -> list[str]:
    """machine.html にインラインstyle（style="..."）が無いか"""
    ngs = []
    p = BASE / "machine.html"
    text = load_text(p)
    matches = re.findall(r'style="[^"]*"', text)
    if matches:
        ngs.append(f"machine.htmlにインラインstyle {len(matches)}箇所: {matches[:3]}")
    return ngs


def check_2_old_subpath(machines: list) -> list[str]:
    """サイト内コードに /uchidokoro/ サブパス残骸が無いか
    （404.html の救済処理・scripts内のドキュメントパスは除外）
    """
    ngs = []
    allowed_files = {"404.html", "scripts/post_to_x.py", "scripts/post_update_to_x.py", "scripts/audit_site.py"}
    targets = list(BASE.glob("*.html")) + list(BASE.glob("assets/**/*.css")) + list(BASE.glob("assets/**/*.js"))
    targets += [BASE / "service-worker.js", BASE / "meta-auto.js", BASE / "manifest.json"]
    for f in targets:
        if not f.is_file():
            continue
        rel = f.relative_to(BASE).as_posix()
        if rel in allowed_files:
            continue
        text = load_text(f)
        if "/uchidokoro/" in text:
            ngs.append(f"{rel} に /uchidokoro/ サブパス残骸あり")
    return ngs


def check_3_info_notation(machines: list) -> list[str]:
    """machines.json の info 表記ゆれ"""
    ngs = []
    for m in machines:
        info = m.get("info", "")
        if "擬似" in info:
            ngs.append(f"{m['slug']}: infoに『擬似』使用 → 『疑似』に統一すべき (現在: '{info}')")
        if "スマスロ ノーマル" in info:
            ngs.append(f"{m['slug']}: infoに『スマスロ ノーマル』(スペース有) → 『スマスロノーマル』に統一すべき (現在: '{info}')")
    return ngs


def check_4_canonical(machines: list) -> list[str]:
    """canonical / og:url / sitemap の3点整合性
    meta-auto.js の canonical / og:url が /machines/{slug}/ を指しているか
    sitemap.xml の機種URLが /machines/{slug}/ 形式で揃っているか
    """
    ngs = []
    meta = load_text(BASE / "meta-auto.js")
    if "machine.html?slug=" in meta and "canonical.href" in meta:
        # canonical が機種ページURLを指しているか確認
        m = re.search(r"canonical\.href\s*=\s*`([^`]+)`", meta)
        if m and "machines/${slug}/" not in m.group(1):
            ngs.append(f"meta-auto.js: canonical が /machines/{{slug}}/ 形式でない → '{m.group(1)}'")
    m_og = re.search(r"og:url'\s*,\s*`([^`]+)`", meta)
    if m_og and "machines/${slug}/" not in m_og.group(1):
        ngs.append(f"meta-auto.js: og:url が /machines/{{slug}}/ 形式でない → '{m_og.group(1)}'")
    # sitemap.xml の機種URL形式
    sm = load_text(BASE / "sitemap.xml")
    bad_machine_urls = re.findall(r"<loc>https://uchidokoro\.com/machine\.html\?[^<]+</loc>", sm)
    if bad_machine_urls:
        ngs.append(f"sitemap.xml に machine.html?slug= 形式のURL {len(bad_machine_urls)}件 → /machines/{{slug}}/ に統一すべき")
    return ngs


def check_5_sw_cache(machines: list) -> list[str]:
    """service-worker.js の STATIC_CACHE が全て実在するか"""
    ngs = []
    sw = load_text(BASE / "service-worker.js")
    m = re.search(r"const\s+STATIC_CACHE\s*=\s*\[(.*?)\]", sw, re.S)
    if not m:
        ngs.append("service-worker.js に STATIC_CACHE が見つからない")
        return ngs
    paths = re.findall(r"'([^']+)'", m.group(1))
    for p in paths:
        if p == "/":
            target = BASE / "index.html"
        else:
            target = BASE / p.lstrip("/")
        if not target.is_file():
            ngs.append(f"SW STATIC_CACHE 内の {p} が存在しない")
    return ngs


def check_6_machine_files(machines: list) -> list[str]:
    """machines.json と machines/{slug}/index.html / machine-details/{slug}.json の整合性"""
    ngs = []
    slugs = [m["slug"] for m in machines]
    for slug in slugs:
        if not (BASE / "machines" / slug / "index.html").is_file():
            ngs.append(f"machines/{slug}/index.html がない")
        if not (BASE / "assets" / "data" / "machine-details" / f"{slug}.json").is_file():
            ngs.append(f"machine-details/{slug}.json がない")
    # 逆: machinesディレクトリにあるが machines.json にない
    machines_dir = BASE / "machines"
    if machines_dir.is_dir():
        for d in machines_dir.iterdir():
            if d.is_dir() and d.name not in slugs:
                ngs.append(f"machines/{d.name}/ がmachines.jsonに無い（孤児ディレクトリ）")
    # 逆: machine-detailsにあるが machines.json にない
    detail_dir = BASE / "assets" / "data" / "machine-details"
    if detail_dir.is_dir():
        for f in detail_dir.glob("*.json"):
            if f.stem not in slugs:
                ngs.append(f"machine-details/{f.name} がmachines.jsonに無い（孤児ファイル）")
    return ngs


def check_7_sitemap_count(machines: list) -> list[str]:
    """sitemap.xml の機種URL件数と machines.json 件数の一致＋重複検知"""
    ngs = []
    sm = load_text(BASE / "sitemap.xml")
    sitemap_machine_slugs_list = re.findall(r"/machines/([^/]+)/", sm)
    sitemap_machine_slugs = set(sitemap_machine_slugs_list)
    machine_slugs = set(m["slug"] for m in machines)
    missing_in_sitemap = sorted(machine_slugs - sitemap_machine_slugs)
    extra_in_sitemap = sorted(sitemap_machine_slugs - machine_slugs)
    if missing_in_sitemap:
        ngs.append(f"sitemap.xml に未登録の機種 {len(missing_in_sitemap)}件: {missing_in_sitemap[:5]}")
    if extra_in_sitemap:
        ngs.append(f"sitemap.xml に余分な機種URL {len(extra_in_sitemap)}件: {extra_in_sitemap[:5]}")
    # 機種URL重複
    dups = sorted(set(s for s in sitemap_machine_slugs_list if sitemap_machine_slugs_list.count(s) > 1))
    if dups:
        ngs.append(f"sitemap.xml 内で機種URL重複 {len(dups)}件: {dups[:5]}")
    # 全URL重複（setting.html や guide系も含む）
    all_locs = re.findall(r"<loc>([^<]+)</loc>", sm)
    loc_dups = sorted(set(u for u in all_locs if all_locs.count(u) > 1))
    if loc_dups:
        ngs.append(f"sitemap.xml 内でURL重複 {len(loc_dups)}件: {loc_dups[:5]}")
    return ngs


def check_8_readme_count(machines: list) -> list[str]:
    """README.md の機種数記載と実数の一致"""
    ngs = []
    actual = len(machines)
    text = load_text(BASE / "README.md")
    nums = [int(n) for n in re.findall(r"(?<![\d])(\d{2,3})機種", text)]
    if not nums:
        ngs.append("README.md に『XX機種』記載が見つからない")
        return ngs
    # 全機種数として書くべき値（最も多く記載されてる値が実数と一致するはず）
    inconsistent = [n for n in nums if n != actual and n != 36 and n < 50]
    # 36 はポチポチくん対応数なので除外、50未満はカテゴリ別件数の可能性で許容
    big_inconsistent = [n for n in nums if n != actual and n >= 50]
    if big_inconsistent:
        ngs.append(f"README.md の機種数記載が実数{actual}と不一致: {sorted(set(big_inconsistent))}")
    return ngs


def _section_text(section: dict) -> str:
    """sectionから本文相当のテキストを抽出（body/items/rows等）"""
    parts = []
    body = section.get("body")
    if body:
        if isinstance(body, list):
            parts.extend(str(x) for x in body)
        elif isinstance(body, str):
            parts.append(body)
    for it in section.get("items", []) or []:
        if isinstance(it, str):
            parts.append(it)
        elif isinstance(it, dict):
            parts.append(str(it.get("text", "") or it.get("body", "") or ""))
    for row in section.get("rows", []) or []:
        if isinstance(row, list):
            parts.extend(str(x) for x in row)
        elif isinstance(row, dict):
            parts.extend(str(v) for v in row.values())
    return " ".join(parts)


def check_9_article_length(machines: list) -> list[str]:
    """machine-details の本文文字数（先行記事除き1500字以上）"""
    ngs = []
    detail_dir = BASE / "assets" / "data" / "machine-details"
    for m in machines:
        if m.get("status") == "preview":
            continue
        slug = m["slug"]
        p = detail_dir / f"{slug}.json"
        try:
            d = load_json(p)
        except Exception:
            continue
        total = sum(len(_section_text(s)) for s in d.get("sections", []))
        # lead もカウント
        total += len(d.get("lead", "") or "")
        if total < 1500:
            ngs.append(f"{slug}: 本文{total}字 (1500字未満)")
    return ngs


def check_10_legal_pages(machines: list) -> list[str]:
    """必須法的ページの本文文字量（500字以上）"""
    ngs = []
    for fname in ["about.html", "privacy.html", "contact.html"]:
        p = BASE / fname
        if not p.is_file():
            ngs.append(f"{fname}: ファイルが存在しない")
            continue
        text = load_text(p)
        # <main>...</main> の中身の文字数を計測
        m = re.search(r"<main[^>]*>(.*?)</main>", text, re.S)
        body = m.group(1) if m else text
        # タグ除去
        plain = re.sub(r"<[^>]+>", "", body)
        plain = re.sub(r"\s+", "", plain)
        if len(plain) < 500:
            ngs.append(f"{fname}: 本文{len(plain)}字 (500字未満)")
    return ngs


def check_11_meta_description(machines: list) -> list[str]:
    """全HTMLにmeta descriptionがあり50〜160字"""
    ngs = []
    # 除外：404ページ・Google Search Console認証ファイル・redirectページ
    skip_files = {"404.html"}
    targets = []
    for p in BASE.glob("*.html"):
        if p.name in skip_files:
            continue
        # Search Console所有権確認ファイル（googleXXXX.html）は除外
        if p.name.startswith("google") and p.name.endswith(".html"):
            continue
        targets.append(p)
    for p in targets:
        text = load_text(p)
        m = re.search(r'<meta\s+name="description"\s+content="([^"]*)"', text)
        if not m:
            ngs.append(f"{p.name}: meta description なし")
            continue
        desc = m.group(1)
        # machine.html / setting.html はmeta-auto.jsで動的生成されるのでテンプレ値はOK
        if p.name in ("machine.html", "setting.html"):
            continue
        if len(desc) < 50:
            ngs.append(f"{p.name}: meta description {len(desc)}字 (50字未満)")
        elif len(desc) > 160:
            ngs.append(f"{p.name}: meta description {len(desc)}字 (160字超)")
    return ngs


def check_12_img_alt(machines: list) -> list[str]:
    """全HTMLの<img>にalt属性"""
    ngs = []
    targets = list(BASE.glob("*.html"))
    for p in targets:
        text = load_text(p)
        # img タグを抽出
        for m in re.finditer(r"<img\b([^>]*)>", text):
            attrs = m.group(1)
            if not re.search(r"\balt\s*=", attrs):
                # 行番号
                line = text[: m.start()].count("\n") + 1
                ngs.append(f"{p.name}:{line}: <img> に alt属性なし")
    return ngs


def check_13_internal_links(machines: list) -> list[str]:
    """HTML内の内部リンクが実在ファイルを指しているか"""
    ngs = []
    targets = list(BASE.glob("*.html"))
    seen = set()
    for p in targets:
        text = load_text(p)
        for m in re.finditer(r'(?:href|src)="([^"#?]+?)(?:[?#][^"]*)?"', text):
            url = m.group(1)
            # 外部URL・データURL・テンプレ変数・ハッシュは除外
            if url.startswith(("http://", "https://", "//", "data:", "mailto:", "tel:", "javascript:", "${")):
                continue
            if url == "" or url == "/":
                continue
            key = (p.name, url)
            if key in seen:
                continue
            seen.add(key)
            # 絶対パス /xxx は BASE 起点
            if url.startswith("/"):
                target = BASE / url.lstrip("/")
            else:
                target = (p.parent / url).resolve()
            # ディレクトリ参照（末尾/）はindex.htmlを想定
            if str(target).endswith(("/", "\\")) or target.is_dir():
                target = Path(str(target).rstrip("/\\")) / "index.html"
            if not target.exists():
                ngs.append(f"{p.name}: 内部リンク切れ → '{url}'")
    return ngs


CHECKS = [
    ("1_インラインstyle", check_1_inline_style),
    ("2_サブパス残骸", check_2_old_subpath),
    ("3_info表記ゆれ", check_3_info_notation),
    ("4_canonical整合性", check_4_canonical),
    ("5_SWキャッシュ実在", check_5_sw_cache),
    ("6_機種ファイル整合", check_6_machine_files),
    ("7_sitemap件数", check_7_sitemap_count),
    ("8_README機種数", check_8_readme_count),
    ("9_記事文字数", check_9_article_length),
    ("10_法的ページ文字量", check_10_legal_pages),
    ("11_metaディスクリプション", check_11_meta_description),
    ("12_img_alt属性", check_12_img_alt),
    ("13_内部リンク切れ", check_13_internal_links),
]


def main():
    machines = load_json(BASE / "assets" / "data" / "machines.json")
    out_json = "--json" in sys.argv
    results = {}
    total_ng = 0
    for name, fn in CHECKS:
        try:
            ngs = fn(machines)
        except Exception as e:
            ngs = [f"チェック実行エラー: {e}"]
        results[name] = ngs
        total_ng += len(ngs)

    if out_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print(f"=== サイト構造整合性チェック（NG合計: {total_ng}件）===")
        for name, ngs in results.items():
            mark = "✅" if not ngs else "❌"
            print(f"\n{mark} {name}: {len(ngs)}件")
            for ng in ngs:
                print(f"   - {ng}")
    sys.exit(0 if total_ng == 0 else 1)


if __name__ == "__main__":
    main()

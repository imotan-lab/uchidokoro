"""
サイト構造整合性チェックスクリプト
verifyタスク（毎日5:05）から呼ばれる。24項目をチェックしてNG項目を標準出力に出す。

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

バグの種・予防:
    14. JSコード内の機種slugハードコード検知（machine.html/setting.html等）
    15. レンダリング前HTML内の `99999` 文字列検知（JS実行前の異常値）
    16. machine-details の文体混在検知（です・ます調と だ・である調の混在）
    17. 他サイト名の露出検知（スロパチクエスト/ちょんぼりすた/ナナプレス/DMM/ぱちタウン/スロラボ）
    18. サブディレクトリ配下のHTMLに <base href="/"> が入っているか（パス解決事故予防）
    19. machine-details の lead 内 Markdown 残留
    20. 旧URL（machine.html?slug=）形式の内部リンク残留
    21. プリレンダ検証（machines/{slug}/index.html に本文が焼き込まれているか）
    22. 機種重複検知（名前正規化での衝突＝同一機種の別名義二重登録）

SEO・運用:
    23. CLAUDE.md肥大検知（50KB超＋履歴退避ルールの生存確認）
    24. 機種ページ noindex 整合（preview=noindex / complete=index の恒久ポリシー）
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
sys.path.insert(0, str(BASE / "scripts"))
from ci_safe import redact as _redact   # noqa: E402  ★CIでは原文を出さない★
import safe_json as _sj                 # noqa: E402  ★壊れた入力は診断で止める★
import page_decision as _pd            # noqa: E402  ★区分の唯一の判定箇所★

# ★ビルドの出力は監査の対象外★（2026-07-30）
#   .preview-site/ は公開されない写し（全ページ noindex・robots全面Disallow）、
#   _site/ は保護CIが空から組み立てる成果物。どちらもGit管理外なので、
#   ここを本番と同じ物差しで測ると「直す必要のないNG」が出て判断を誤らせる。
#   ★写し自身の検査は build_preview_site.py が、成果物の検査は
#     build_pages_artifact.py の audit() が別に行う★
BUILD_DIRS = {".preview-site", "_site", "_site.next"}


def is_build_output(path: Path) -> bool:
    """ビルド出力（写し・成果物）配下のパスか。"""
    try:
        rel = path.resolve().relative_to(BASE.resolve())
    except ValueError:
        return False
    return bool(set(rel.parts) & BUILD_DIRS)


def site_html_files() -> list[Path]:
    """サイト本体のHTML（ビルド出力を除いた全 *.html）。"""
    return [p for p in BASE.glob("**/*.html") if not is_build_output(p)]


def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_json(path: Path):
    """★壊れた入力は診断で止める★（Codex 閉鎖条件5）"""
    return _sj.read_json(path)


def check_1_inline_style(machines: list) -> list[str]:
    """全HTMLファイルにインラインstyle（style="..."）が無いか

    CLAUDE.mdルール：機種ページに限らず、全HTMLファイルで `style="..."` 直書きを禁止。
    全スタイルは practical.css に集約する。
    machines/{slug}/index.html は machine.html のコピーなので親で検知すれば十分。
    """
    ngs = []
    targets = list(BASE.glob("*.html"))
    for p in targets:
        # googleafe... は Search Console 認証ファイルなので除外
        if p.name.startswith("google") and p.name.endswith(".html"):
            continue
        text = load_text(p)
        # ★どの行かを出す（伏せ字にしても直せるように）★（Codex 19巡目 (b)-3）
        hits = [(i + 1, m.group(0))
                for i, line in enumerate(text.splitlines())
                for m in re.finditer(r'style="[^"]*"', line)]
        if hits:
            where_ = ", ".join(f"{ln}行目{_redact(v)}" for ln, v in hits[:5])
            more = f" ほか{len(hits) - 5}箇所" if len(hits) > 5 else ""
            ngs.append(f"{p.name}: インラインstyle {len(hits)}箇所: {where_}{more}")
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
            ngs.append(f"{m['slug']}: infoに『擬似』使用 → 『疑似』に統一すべき (現在: {_redact(info)})")
        if "スマスロ ノーマル" in info:
            ngs.append(f"{m['slug']}: infoに『スマスロ ノーマル』(スペース有) → 『スマスロノーマル』に統一すべき (現在: {_redact(info)})")
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
    # 重複解消でリダイレクト化した旧slug（machines.jsonからは削除済みだが /machines/{slug}/ に
    # mhrise等への client-side リダイレクトを残しているため孤児扱いしない）
    REDIRECT_SLUGS = {"monhun_rise", "okidoki_duo_encore"}  # → mhrise / okidoki_encore に統合(2026-06-29)
    # 逆: machinesディレクトリにあるが machines.json にない
    machines_dir = BASE / "machines"
    if machines_dir.is_dir():
        for d in machines_dir.iterdir():
            if d.is_dir() and d.name not in slugs and d.name not in REDIRECT_SLUGS:
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
    # ★区分は page_decision.machine_class が唯一の判定箇所★（2026-08-04・Codex71〜72回目）
    #   index対象 = LEGACY_COMPLETE ∪ AUTO_INDEXABLE ／ noindex対象 = LEGACY_PREVIEW ∪ AUTO_PENDING
    complete_slugs = set(m["slug"] for m in machines
                         if _pd.machine_class(m) in ("LEGACY_COMPLETE",
                                                     "AUTO_INDEXABLE"))
    preview_slugs = set(m["slug"] for m in machines
                        if _pd.machine_class(m) in ("LEGACY_PREVIEW",
                                                    "AUTO_PENDING"))
    machine_slugs = complete_slugs | preview_slugs
    missing_in_sitemap = sorted(complete_slugs - sitemap_machine_slugs)
    extra_in_sitemap = sorted(sitemap_machine_slugs - machine_slugs)
    # noindex対象のsitemap掲載はnoindexと信号矛盾になるため禁止
    preview_in_sitemap = sorted(preview_slugs & sitemap_machine_slugs)
    if preview_in_sitemap:
        ngs.append(f"sitemap.xml にnoindex対象（preview/AUTO_PENDING）の機種が掲載（noindexと信号矛盾）: {preview_in_sitemap[:5]}")
    if missing_in_sitemap:
        ngs.append(f"sitemap.xml に未登録のcomplete機種 {len(missing_in_sitemap)}件: {missing_in_sitemap[:5]}")
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
    # ポチポチくんのクエリURL再混入検知（2026-07-09 SEO整理: ツールは検索対象外・setting.html本体のみ掲載）
    if "setting.html?slug=" in sm:
        ngs.append("sitemap.xml に setting.html?slug= のクエリURLが再混入（ツールページは検索対象外＝本体のみ掲載。2026-07-09整理）")
    return ngs


# ★当サイト全体の掲載数を表す言い回し★（2026-07-31・全体件数の表示をやめた）
#   「36機種（ポチポチくん対応）」のような別用途の件数は誤検知しないよう、
#   「全体を指す語＋数＋機種」の形だけを見る。
_TOTAL_COUNT_PAT = re.compile(
    r"(全|全部で|掲載|対象機種数[:：]?\s*)(<[^>]+>)?\s*\d{2,3}\s*(</[^>]+>)?\s*機種")


def check_8_readme_count(machines: list) -> list[str]:
    """★サイト全体の機種数を書いていないか★（2026-07-31 方針転換）

    以前は「READMEの数が実数と合っているか」を見ていた。
    しかし全体件数は、増減のたびに README・運営者情報・早見表の散文を
    そろえる必要があり、実際に何度もずれた（読者にとっての価値も薄い）。
    そこで**全体件数は表示しない**方針にしたので、
    検査も「合っているか」から「**書いていないか**」に変える。

    条件つきの件数（天井が浅い機種は何件、など）は意味があるので触らない。
    """
    ngs = []
    for rel in ("README.md", "about.html", "guide-ichiran.html"):
        text = load_text(BASE / rel)
        for m in _TOTAL_COUNT_PAT.finditer(text):
            ngs.append(f"{rel}: サイト全体の機種数が書かれています"
                       f"（{m.group(0)[:30]!r}）。全体件数は表示しない方針です")
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
        if _pd.machine_class(m) != "LEGACY_COMPLETE":
            # ★preview と新台経路(AUTO_*)は対象外★（網羅性を保証しない設計・Codex72回目）
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
        # machine.html はmeta-auto.js、setting.html は自前JSで動的更新されるためテンプレ値はOK
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
            # JSテンプレートリテラル（href="/machines/${x.slug}/" 等）はJS生成リンクなので静的検証対象外
            if "${" in url:
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
                ngs.append(f"{p.name}: 内部リンク切れ → {_redact(url)}")
    return ngs


def check_14_slug_hardcode(machines: list) -> list[str]:
    """JSコード内の機種slug文字列ハードコード検知（バグの種を予防）

    例: machine.html の `slug === "sf5"` のような特定機種だけ動く分岐は危険信号。
    新規機種追加時に修正漏れする原因になる。
    意図的な「設定差なし機種リスト」等のslug配列は除外（noSettingDiff等）。
    """
    ngs = []
    targets = [BASE / "machine.html", BASE / "setting.html", BASE / "index.html", BASE / "meta-auto.js"]
    slugs = set(m["slug"] for m in machines)
    # 許可される文脈（リスト・配列形式での列挙は意図的なので除外）
    for f in targets:
        if not f.is_file():
            continue
        text = load_text(f)
        for ln, line in enumerate(text.splitlines(), 1):
            # slug === "xxx" / slug == "xxx" / slug.includes("xxx") の検知
            for m in re.finditer(r'slug\s*===?\s*["\']([a-z_0-9]+)["\']', line):
                slug = m.group(1)
                if slug in slugs:
                    ngs.append(f"{f.name}:{ln}: ハードコード `slug === \"{slug}\"`（条件分岐は危険）")
    return ngs


def check_15_render_99999(machines: list) -> list[str]:
    """レンダリング前HTMLに `99999` 数値が残留していないか

    machines.json の checker.normal.excellent: 99999 等は data なのでOK。
    machine.html や machines/{slug}/index.html の表示テキストに 99999 がそのまま
    出ているのはバグの兆候（JSの判定漏れで表示されてしまう）。
    """
    ngs = []
    targets = [BASE / "machine.html"] + list(BASE.glob("machines/*/index.html"))
    for f in targets:
        text = load_text(f)
        # JSコード内・JSON-LD内・data-* 属性内の 99999 は許容
        # 表示テキスト相当の場所（<body>内・<title>・meta description content）に 99999 があれば検出
        # 簡略化: <body>...</body> の中、かつタグ属性外（タグの中身テキスト）に 99999 があれば検出
        body_match = re.search(r"<body[^>]*>(.*?)</body>", text, re.S)
        if not body_match:
            continue
        body = body_match.group(1)
        # スクリプト除去
        body_no_script = re.sub(r"<script[^>]*>.*?</script>", "", body, flags=re.S)
        # 「99999」が見えるかどうか
        if "99999" in body_no_script:
            rel = f.relative_to(BASE).as_posix()
            ngs.append(f"{rel}: 表示テキスト中に '99999' を検出（チェッカー閾値が漏れて表示されている可能性）")
    return ngs


def check_16_writing_style(machines: list) -> list[str]:
    """machine-details の文体混在検知（です・ます と だ・である の混在）

    機種記事は「です・ます」調で統一する。1機種内で常体（だ・である調）の文が
    1文以上あれば NG。文体ルールはプロジェクトCLAUDE.md「セクションtitle・文体の統一ルール」参照。
    """
    import re as _re
    ngs = []
    detail_dir = BASE / "assets" / "data" / "machine-details"

    def _is_plain(sent: str) -> bool:
        s = sent.rstrip("。、,!?").strip()
        if not s:
            return False
        last = s[-5:]
        if _re.search(r"(?:です|ます|でしょう|ません|でした|ました|ください|でしょ)$", last):
            return False
        return bool(_re.search(r"(?:だ|である|した|する|った|ない|だが|だろう|だろ|なる|させる|られる|られた)$", last))

    for m in machines:
        if _pd.machine_class(m) != "LEGACY_COMPLETE":
            # ★preview と新台経路(AUTO_*)は対象外★（網羅性を保証しない設計・Codex72回目）
            continue
        slug = m["slug"]
        p = detail_dir / f"{slug}.json"
        if not p.is_file():
            continue
        try:
            d = load_json(p)
        except Exception:
            continue
        plain_sentences = []
        for s in d.get("sections", []):
            if s.get("type") == "settei":
                continue
            body = s.get("body")
            text = " ".join(body) if isinstance(body, list) else (body if isinstance(body, str) else "")
            if not text or len(text) < 30:
                continue
            sents = [x for x in _re.split(r"(?<=。)", text) if x.strip()]
            for sent in sents:
                if _is_plain(sent):
                    plain_sentences.append((s.get("title", ""), sent.strip()))
        if plain_sentences:
            for title, sent in plain_sentences[:2]:  # 機種ごとに最大2件
                ngs.append(f"{slug}: 常体文混在 [{_redact(title)}] {_redact(sent)}")
    return ngs


def check_17_external_site_names(machines: list) -> list[str]:
    """他サイト名の露出検知

    記事本文・公開HTMLに「スロパチクエスト」「ちょんぼりすた」「ナナプレス」「DMM」
    「ぱちタウン」「スロラボ」などの他サイト名が出てないかチェック。
    競合サイト誘導・著作権リスク回避のため、本文に出さないルール。

    自動タスクのSKILL.md・スクリプト・CLAUDE.md内（読まれない領域）は対象外。
    audit対象は assets/data/machine-details/*.json と *.html のみ。
    """
    import re as _re
    ngs = []
    sites = [
        # 競合解析サイト
        "スロパチクエスト", "ちょんぼりすた", "ナナプレス", "DMM", "ぱちタウン", "スロラボ",
        # 削除されたアフィリエイトサービス（もしもアフィリエイト・パチスロでは利用不可）
        "もしもアフィリエイト", "moshimo.com", "af.moshimo", "i.moshimo",
    ]
    # 業界用語と区別：「DMM」は「DMM ぱちタウン」サイト名のみ検出（他用途は無いと仮定）
    detail_dir = BASE / "assets" / "data" / "machine-details"
    for jf in sorted(detail_dir.glob("*.json")):
        text = load_text(jf)
        for s in sites:
            c = text.count(s)
            if c:
                ngs.append(f"machine-details/{jf.name}: '{s}' × {c}件 露出")
    # machines.json も対象（checker.note / strategy / seo.title 等）
    mj = BASE / "assets" / "data" / "machines.json"
    if mj.is_file():
        text = load_text(mj)
        for s in sites:
            c = text.count(s)
            if c:
                ngs.append(f"machines.json: '{s}' × {c}件 露出")
    # HTMLファイル（machines/{slug}/index.html は machine.html のコピーなので除外）
    for hf in BASE.glob("*.html"):
        # 404.html の旧サブパスリダイレクト処理は除外
        if hf.name == "404.html":
            continue
        text = load_text(hf)
        for s in sites:
            c = text.count(s)
            if c:
                ngs.append(f"{hf.name}: '{s}' × {c}件 露出")
    return ngs


def check_18_subdir_base_href(machines: list) -> list[str]:
    """サブディレクトリ配下のHTMLに <base href="/"> が入っているか

    サブディレクトリ（machines/{slug}/ 等）配下のHTMLは `<base href="/">` が無いと、
    相対パス（href="index.html" / src="assets/img/logo.png" 等）がそのディレクトリ
    起点で解決されてしまい、ロゴ・ナビ・footer内リンクが全て404になる事故が起きる。

    対象：BASE直下を除いたすべての *.html。
    例外：<head>タグを持たない単純なリダイレクトスクリプト（checker.html 等の1行JS）。
    """
    ngs = []
    for f in site_html_files():
        # ルート直下は対象外（<base>無しでも相対パスが正しく解決されるため）
        if f.parent == BASE:
            continue
        text = load_text(f)
        # <head>タグが無いHTMLは単純なリダイレクト等として対象外（checker.html等）
        if "<head" not in text.lower():
            continue
        if not re.search(r'<base\s+href\s*=\s*["\']/["\']', text, re.IGNORECASE):
            rel = f.relative_to(BASE).as_posix()
            ngs.append(f"{rel}: <base href=\"/\"> が無い（サブディレクトリ配下のHTMLには必須）")
    return ngs


def check_19_lead_markdown(machines: list) -> list[str]:
    """machine-details の lead に Markdown記法（**強調**）が残っていないか

    lead は machine.html の heroSub に textContent で描画されるため、sections と違って
    Markdown（**強調** → <strong>）が解釈されず `**` がそのまま画面に出る。
    新台追加・昇格時に lead へ `**` を書くと表示崩れになるので静的に検知する。
    （レンダリング後監査 audit_render.py R9 でも捕捉できるが、こちらは数秒で判明する）
    """
    ngs = []
    detail_dir = BASE / "assets" / "data" / "machine-details"
    for jf in sorted(detail_dir.glob("*.json")):
        try:
            d = load_json(jf)
        except Exception:
            continue
        lead = d.get("lead") or ""
        if "**" in lead:
            ngs.append(f"machine-details/{jf.name}: lead に '**'（Markdown未解釈で表示される）")
    return ngs


def check_20_old_url_links(machines: list) -> list[str]:
    """HTML内に旧URL形式 machine.html?slug= の内部リンクが残っていないか

    トップ(index.html)等から旧URLへリンクすると、canonicalで正規化はされても
    内部リンク評価が分散しインデックス促進が弱まる。正規URL /machines/{slug}/ に統一する。
    sitemap.xml は check_4 が担当。404.html の旧パス救済は別物なので対象外。
    """
    ngs = []
    targets = list(BASE.glob("*.html")) + list(BASE.glob("machines/*/index.html"))
    for f in targets:
        if not f.is_file() or f.name == "404.html":
            continue
        text = load_text(f)
        c = text.count("machine.html?slug=")
        if c:
            rel = f.relative_to(BASE).as_posix()
            ngs.append(f"{rel}: 旧URLリンク machine.html?slug= が{c}箇所 → /machines/{{slug}}/ に統一")
    return ngs


def check_21_prerender(machines: list) -> list[str]:
    """機種ページが静的HTMLにプリレンダ済みか（空シェル再発防止）

    build_machine_pages.py 未実行や旧ビルドだと title/h1/本文が JS待ちの空シェルに戻り、
    「クロール済み・インデックス未登録」やAdSense「中身なし」を招く。静的HTMLに
    機種名h1・本文が焼かれているかを検査する。
    """
    ngs = []
    for m in machines:
        slug = m["slug"]
        p = BASE / "machines" / slug / "index.html"
        if not p.is_file():
            continue  # check_6 が担当
        text = load_text(p)
        if "<title>機種ページ | うちどころ。</title>" in text:
            ngs.append(f"machines/{slug}/index.html: title が空シェルのまま（build_machine_pages.py 要実行）")
        if '>機種名</h1>' in text:
            ngs.append(f"machines/{slug}/index.html: h1 が『機種名』プレースホルダのまま（要プリレンダ）")
        # articleSections が空（先行記事除く・本文があるはず）
        if ('<div id="articleSections"></div>' in text
                and _pd.machine_class(m) == "LEGACY_COMPLETE"):
            ngs.append(f"machines/{slug}/index.html: 本文(articleSections)が空シェルのまま（要プリレンダ）")
    return ngs


def check_22_duplicate_machines(machines: list) -> list[str]:
    """同一機種が複数slugで二重登録されていないか（名前正規化での衝突検知）。
    monhun_rise/mhrise・okidoki系のような重複コンテンツ(AdSense上の重複ページ)を防ぐ。
    プレフィックス(スマスロ/L等)と記号を除いた正規化名が一致する複数slugを『重複の疑い』として報告。
    別シリーズの別機種(北斗2種・炎炎1/2等)は正規化名が異なるため誤検知しない。"""
    import unicodedata
    prefix = re.compile(r"^(スマスロ|スマパチ|パチスロ|ぱちすろ|L|Ｌ|P|Ｐ|新|新台)\s*")

    def norm(name: str) -> str:
        s = unicodedata.normalize("NFKC", name or "")
        prev = None
        while prev != s:
            prev = s
            s = prefix.sub("", s).strip()
        s = re.sub(r"[\s　・/／!！?？()（）\-—~〜【】\[\]、。,.'\"]+", "", s)
        return s.lower()

    by_norm: dict[str, list] = {}
    for m in machines:
        by_norm.setdefault(norm(m["name"]), []).append(m["slug"])
    ngs = []
    for n, slugs in by_norm.items():
        if len(slugs) > 1:
            ngs.append(f"同一機種が複数slugで二重登録の疑い: {sorted(slugs)}（正規化名='{n}'）→統合＋リダイレクト要")
    return ngs


def check_23_claude_md_size(machines: list) -> list[str]:
    """CLAUDE.md（毎セッション読み込まれるルールファイル）の肥大化検知。
    50KB超でNG＝対話セッションで圧縮する合図（履歴・完了施策の詳細をCLAUDE_history.mdへ退避。
    手順は2026-07-09の実施例＝退避→別エージェントで欠損検証。★無人タスクはCLAUDE.mdを書き換えない★）。
    2026-07-09に76KB→42KBへ圧縮した際の再発防止（日次履歴行の本体追記が肥大の主因だった）。
    あわせて履歴退避ルールの生存確認（CLAUDE_history.mdへの参照が消えていないか）も行う。"""
    ngs = []
    path = BASE / "CLAUDE.md"
    if not path.is_file():
        return ngs  # 家PC等でファイルが無い環境では検査しない
    size = path.stat().st_size
    if size > 50 * 1024:
        ngs.append(
            f"CLAUDE.mdが{size/1024:.1f}KB（閾値50KB超）→対話セッションで圧縮する"
            "（履歴をCLAUDE_history.mdへ退避→欠損検証。無人タスクは書き換え禁止）")
    text = path.read_text(encoding="utf-8", errors="replace")
    if "CLAUDE_history.md" not in text:
        ngs.append("CLAUDE.mdからCLAUDE_history.mdへの参照が消えている（履歴退避ルールの喪失疑い）→「修正履歴について」セクションを復元する")
    return ngs


def check_24_robots_noindex(machines: list) -> list[str]:
    """機種ページの noindex 整合（2026-07-09 恒久ポリシー: preview=noindex / complete=index）。
    complete ページへの noindex 残留は検索から消える重大事故なので毎日検知する。"""
    ngs = []
    for m in machines:
        slug = m["slug"]
        page = BASE / "machines" / slug / "index.html"
        if not page.is_file():
            continue  # 実在チェックは check_6 の担当
        text = load_text(page)
        has_noindex = bool(re.search(r"<meta[^>]*name=[\"']robots[\"'][^>]*content=[\"'][^\"']*noindex", text, re.I))
        # ★区分は machine_class（preview/AUTO_PENDING=noindex・complete/AUTO_INDEXABLE=index）★
        want_noindex = _pd.machine_class(m) in ("LEGACY_PREVIEW", "AUTO_PENDING")
        if want_noindex and not has_noindex:
            ngs.append(f"{slug}: noindex対象（{_pd.machine_class(m)}）なのに noindex が無い")
        if (not want_noindex) and has_noindex:
            ngs.append(f"{slug}: index対象（{_pd.machine_class(m)}）なのに noindex が残留（検索から消える事故）")
    return ngs


def check_25_section_body_type(machines: list) -> list[str]:
    """machine-details の sections[].body が配列か（文字列だと生成側が1文字ずつ<p>化する不具合。
    2026-07-10に biohazard_re3/takt_opus/super_rio_ace2 で実発生・本番で数千段落に崩壊した）。"""
    ngs = []
    for m in machines:
        p = BASE / "assets" / "data" / "machine-details" / f"{m['slug']}.json"
        if not p.is_file():
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for i, s in enumerate(d.get("sections", [])):
            b = s.get("body")
            if isinstance(b, str):
                ngs.append(f"{m['slug']}: sections[{i}]({_redact(s.get('title'))}) の body が文字列→配列であるべき（1文字ずつ段落化する不具合）")
    return ngs


def check_26_empty_paragraph(machines: list) -> list[str]:
    """machine-details の body に空・空白のみの段落が無いか（空<p></p>として焼き込まれる。
    2026-07-12に lupin_daikokaisha で実発生・外部レビューで指摘された）。lead空も検知。"""
    ngs = []
    for m in machines:
        p = BASE / "assets" / "data" / "machine-details" / f"{m['slug']}.json"
        if not p.is_file():
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not str(d.get("lead", "x")).strip():
            ngs.append(f"{m['slug']}: lead が空")
        for i, s in enumerate(d.get("sections", [])):
            b = s.get("body")
            if not isinstance(b, list):
                continue  # 型不正は項目25の担当
            for j, t in enumerate(b):
                if not isinstance(t, str) or not t.strip():
                    ngs.append(f"{m['slug']}: sections[{i}]({_redact(s.get('title'))}) body[{j}] が空段落")
    return ngs


def check_29_control_chars(machines: list) -> list[str]:
    """スクリプト・HTML・CSS に制御文字が紛れ込んでいないか

    ★2026-07-30に実害のあるバグを発見★
      `gates.py` の正規表現で `\b` がバックスペース文字(0x08)に化けており、
      **onclick= などの危ない属性を1つも検知できていなかった**。
      見た目では気づけないので機械で検査する（タブ・改行だけ許す）。
    """
    ngs = []
    # ★Gitが追跡している全ファイルを対象にする★（Codex 23巡目 (b)）
    #   直下のHTMLとCSSだけでは machines/**/*.html・assets/**/*.js・workflow が漏れる。
    import subprocess
    try:
        listed = subprocess.run(["git", "ls-files"], cwd=BASE, text=True,
                                capture_output=True, check=True).stdout.splitlines()
    except Exception as e:
        return [f"追跡ファイルの一覧を取れません（{type(e).__name__}）: {e}"]
    exts = {".py", ".html", ".htm", ".js", ".css", ".json", ".yml", ".yaml", ".txt", ".xml", ".md"}
    for rel in listed:
        rel = rel.strip()
        if not rel:
            continue
        f = BASE / rel
        if is_build_output(f) or not f.is_file() or f.suffix.lower() not in exts:
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except Exception as e:
            # ★読めないファイルを黙って飛ばさない★（同）
            ngs.append(f"{rel}: 読めません（{type(e).__name__}）")
            continue
        # ★splitlines は 0x0B/0x0C などを行区切りとして消してしまう★（同）
        #   文字列全体を1文字ずつ見て、位置は改行の数から出す。
        line = 1
        for ch in text:
            if ch == chr(10):
                line += 1
            elif ord(ch) < 32 and ch != chr(9):
                ngs.append(f"{rel}:{line} に制御文字 {hex(ord(ch))}")
    return ngs


def check_27_hub_counts(machines: list) -> list[str]:
    """ハブ/ランキング4ページ内の件数表記が machines.json の実数と一致するか
    （散文の手書き件数がデータ更新に追従せずズレた事故の再発検知・2026-07-12外部レビュー指摘）。
    build_hub_pages.py と同じロジックで A/C/D/ALL を再計算し、HTML中の件数数字と突合する。"""
    ngs = []

    # build_hub_pages.py の mode_conf/base_caution/dataset_A/C/D と同一ロジック（ズレると誤検知するため変更時は両方直す）
    def _mode_key(x):
        return x.get("key") if isinstance(x, dict) else x

    def _mode_conf(c, key):
        # checker直下とchecker.modeData配下の両形式に対応（modeData形式3機種の漏れ事故対策・2026-07-13）
        if not isinstance(c, dict):
            return None
        v = c.get(key)
        if isinstance(v, dict):
            return v
        md = c.get("modeData")
        if isinstance(md, dict) and isinstance(md.get(key), dict):
            return md[key]
        return None

    rows = []
    for m in machines:
        c = m.get("checker") or {}
        if not isinstance(c, dict):
            c = {}
        modes = [_mode_key(x) for x in (c.get("modes") or [])]
        # ★構造カバレッジ検出: modes宣言に対応する設定がどこにも無い機種＝集計・表示から黙って漏れる★
        for x in (c.get("modes") or []):
            k = _mode_key(x)
            if isinstance(x, dict) and len(x) > 2:
                continue  # モード定義がエントリ内に直書きされている形式は対象外
            if isinstance(k, str) and _mode_conf(c, k) is None:
                ngs.append(f"{m['slug']}: modes宣言 '{k}' に対応する設定がchecker直下にもmodeDataにも無い（集計から漏れる・データ形式の確認要）")
        # 基準モード: normal優先→modes宣言順のreset系以外→直下キー走査
        ncau = None
        v = _mode_conf(c, "normal")
        if isinstance(v, dict) and isinstance(v.get("caution"), (int, float)):
            ncau = v["caution"]
        if not isinstance(ncau, (int, float)):
            for k in modes:
                if not isinstance(k, str) or "reset" in k.lower():
                    continue
                v = _mode_conf(c, k)
                if isinstance(v, dict) and isinstance(v.get("caution"), (int, float)):
                    ncau = v["caution"]
                    break
        if not isinstance(ncau, (int, float)):
            for k, v in c.items():
                if k in ("reset", "modeData") or "reset" in str(k).lower() or not isinstance(v, dict):
                    continue
                cv = v.get("caution")
                if isinstance(cv, (int, float)):
                    ncau = cv
                    break
        _lim = m.get("limit")
        if isinstance(_lim, dict):  # mode別limit(2026-07-23〜)はnormalを代表値に（build_hub_pagesと同一ロジック）
            _lim = _lim["normal"] if _lim.get("normal") is not None else next(iter(_lim.values()), None)
        rows.append(dict(
            unit=c.get("unit"),
            limit=_lim,
            # ★status を入れ忘れていて preview 除外が効いていなかった★（Codex 17巡目 (b)-1）
            status=m.get("status", "complete"),
            # ★新台経路(AUTO_*)はランキング母集団から明示除外★（2026-08-04・Codex72回目）
            mclass=_pd.machine_class(m),
            has_suru=bool(c.get("hasSuru") or "suru" in modes or "through" in modes),
            has_cycle=bool(c.get("hasCycle") or "cycle" in modes),
            ncau=ncau,
            rcau=(_mode_conf(c, "reset") or {}).get("caution"),
        ))
    # ★先行記事も早見表には載る（build_hub_pages と揃える）★（Codex 17巡目 (b)-1）
    #   分類の断定は yome() 側で避ける。sitemap にだけ載せない。
    ALL = rows
    # ★A/C/D は先行記事を除く★（Codex 18巡目 (b)-3）
    #   本番のハブは公開射影（preview は数値を落とす）を読むので、
    #   authoring の数値で分類すると「件数が合わない」という誤警告になる。
    rows = [r for r in rows
            if r.get("status") != "preview"
            and r.get("mclass") == "LEGACY_COMPLETE"]
    A = [r for r in rows
         if r["unit"] == "G" and isinstance(r["limit"], (int, float))
         and not r["has_cycle"] and r["limit"] < 1000
         and isinstance(r["ncau"], (int, float))]
    C = [r for r in rows
         if isinstance(r["rcau"], (int, float)) and isinstance(r["ncau"], (int, float))
         and r["ncau"] - r["rcau"] > 0]
    D = [r for r in rows if r["has_suru"]]
    # ★全機種一覧は件数ではなく「載っている機種の集合」で見る★
    #   （2026-07-31・全体件数の表示をやめたので、数字では確かめられない。
    #     そもそも件数が合っていても、中身が欠けていれば意味がない）
    want = {m.get("slug") for m in machines if m.get("slug")}
    ich = BASE / "guide-ichiran.html"
    if ich.is_file():
        html_i = ich.read_text(encoding="utf-8")
        got = set(re.findall(r'href="/machines/([a-z0-9_]+)/"', html_i))
        missing = sorted(want - got)
        extra = sorted(got - want)
        if missing:
            ngs.append(f"guide-ichiran.html: 一覧に無い機種 {missing[:5]}"
                       f"（全{len(missing)}件）→ 早見表を作り直してください")
        if extra:
            ngs.append(f"guide-ichiran.html: 機種データに無い行 {extra[:5]}"
                       f"（全{len(extra)}件）")
        dup = [x for x in set(got)
               if html_i.count(f'href="/machines/{x}/"') > 1]
        if dup:
            ngs.append(f"guide-ichiran.html: 同じ機種の行が重複 {sorted(dup)[:5]}")

    expected = {
        "guide-tenjo-ranking.html": len(A),
        "guide-reset-ranking.html": len(C),
        "guide-suru-tenjo.html": len(D),
    }
    for file, exp in expected.items():
        p = BASE / file
        if not p.is_file():
            ngs.append(f"{file}: ファイルが存在しない")
            continue
        html_src = p.read_text(encoding="utf-8")
        # 自動生成の件数（list-count span）と散文中の「全N機種」「全部でN機種」を全て抽出して突合
        nums = set(int(x) for x in re.findall(r'list-count">(\d+)<', html_src))
        nums |= set(int(x) for x in re.findall(r"全(?:部で)?(?:<strong>)?(\d+)(?:</strong>)?機種", html_src))
        bad = sorted(n for n in nums if n != exp)
        if bad:
            ngs.append(f"{file}: 件数表記 {bad} が実数 {exp} と不一致（build_hub_pages.py 再実行かhub_prose.jsonの手書き数字残り）")
    return ngs


def check_28_settei_table_shape(machines: list) -> list[str]:
    """settei表の headers 数と各行のセル数が一致しているか（データレベル）。
    2026-07-13外部レビューで「4列見出しなのに2セルしか描画されない」レンダラーバグが発覚。
    レンダラーは全セル出力に修正済みだが、データ側の列数不揃いも表示崩れになるため毎日検知する。"""
    ngs = []
    for m in machines:
        p = BASE / "assets" / "data" / "machine-details" / f"{m['slug']}.json"
        if not p.is_file():
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for si, s in enumerate(d.get("sections", [])):
            if s.get("type") != "settei":
                continue
            for ti, tbl in enumerate(s.get("tables") or []):
                headers = tbl.get("headers") or []
                for ri, row in enumerate(tbl.get("rows") or []):
                    cells = row if isinstance(row, list) else [row]
                    if headers and len(cells) != len(headers):
                        ngs.append(
                            f"{m['slug']}: sections[{si}].tables[{ti}]({_redact(tbl.get('label'))}) 行{ri}が"
                            f"{len(cells)}セル（見出しは{len(headers)}列）"
                        )
    return ngs


def check_30_surface_conflicts(machines: list) -> list[str]:
    """同じ事実が1つの記事の中で違う値になっていないか（自己矛盾）

    ★なぜ要るか（2026-07-30）★
      東京喰種は「CZ間天井の恩恵」が、天井・恩恵セクションでは
      「CZまたはAT当選」、基本スペックでは「CZ確定」と書かれていた。
      **どちらかが必ず誤り**なのに、人が読み比べるまで誰も気づけなかった。

      同じ日に、8機種で「入力欄の上限」がそのまま
      「（天井N）」として画面に出ていたことも分かった
      （railgun2 は記事999G+αに対し画面1050G）。
      どちらも機械で気づける形なので、ここで毎回見る。

    ★+α の有無も食い違いとして挙げる★（2026-07-30・Codex指摘4で訂正）
      当初は「1200G」と「1200G+α」を書き方の違いとして通していたが、
      **意味が違う**（ちょうど1200 と 1200以上）。しかも
      公開判定（claim_reconcile）は正規化した値ぜんぶで比べるので止まる。
      監査だけ通すと「監査は緑なのに公開は止まる」という食い違いになり、
      ゲート無効中に頼れる唯一の検査が見逃しになる。判定を揃える。

    ★文章の食い違いはここでは挙げない★（2026-07-30）
      恩恵などの文章は「ST確定」と「ST『カバネリラッシュ』当選」のように、
      **同じ意味を別の言い方で書いただけ**の例が大半（実データで24件中の多数）。
      同義かどうかは機械では判定できない。ここで落とすと、
      本題である数値の食い違いが言い換えの山に埋もれる。
      文章の食い違いは公開判定（claim_reconcile）が止める。
      いま公開中の記事の分は要確認台帳へ載せて、更新タスクが順に処理する。
    """
    import sys as _s
    _s.path.insert(0, str(BASE / "scripts"))
    import claim_inventory as _ci

    ngs = []
    for m in machines:
        slug = m.get("slug")
        dp = BASE / "assets" / "data" / "machine-details" / f"{slug}.json"
        if not dp.is_file():
            continue
        try:
            detail = load_json(dp)
        except Exception:
            continue        # 読めない記事は別項目の担当
        try:
            inv = _ci.build_inventory(slug, m, detail)
        except Exception as e:
            ngs.append(f"{slug}: 在庫を作れず矛盾を検査できません（{type(e).__name__}）")
            continue
        for c in inv.get("surface_conflicts") or []:
            kinds = {v.get("kind") for v in
                     (sf["current_value"] for sf in c["surfaces"])
                     if isinstance(v, dict)}
            if kinds == {"TEXT"}:
                continue        # 文章の食い違いは意味判定が要る（上の説明）
            where = " / ".join(
                f"{sf['source_pointer']}={_redact(str(sf['current_text']))[:24]}"
                for sf in c["surfaces"])
            ngs.append(f"{slug}: {c['field_key']} が記事内で食い違い → {where}")
    return ngs


def check_31_codex_report(machines: list) -> list[str]:
    """スクリプトを変えたのにCodexへ報告していないまま溜まっていないか

    ★なぜ機械に見張らせるか（2026-07-31）★
      「作ったらCodexへ報告する」を記憶・CLAUDE.md・手順書の3か所に書いたが、
      **3回とも守れなかった**（作業に没頭すると飛ぶ）。運営者からも
      「確認怠りすぎ、忘れないように」と指摘された。
      文章で覚えるのをやめて、**commit前に必ず走らせるこの監査**に載せる。

    ★数え方★
      `Documents/uchidokoro/last_codex_report.json` に「最後に報告した時点の
      コミット」を記録する。それ以降に scripts/ を触ったコミットが
      たまっていたら NG にする。

    ★報告したら記録する★
      python scripts/codex_reported.py     （このコミットまで報告済み、と記録）
    """
    import subprocess
    STATE = r"C:/Users/imao_/Documents/uchidokoro/last_codex_report.json"
    LIMIT = 3
    try:
        last = load_json(STATE).get("commit") if os.path.isfile(STATE) else None
    except Exception:
        last = None
    if not last:
        return []          # 記録が無い間は何も言わない（初回の邪魔をしない）
    try:
        # ★文字コードを必ず指定する★（2026-07-31）
        #   指定しないと Windows の既定（cp932）で読もうとし、
        #   日本語のコミットメッセージで例外になる。
        #   すると下の except に落ちて **この検査が黙って無効化される**。
        #   実際に一度そうなっていた（入れたのに効いていなかった）。
        r = subprocess.run(["git", "log", "--oneline", f"{last}..HEAD", "--",
                            "scripts/"], cwd=BASE, capture_output=True)
        if r.returncode != 0:
            return []      # 記録のコミットが無い等。監査を落とさない
        text = r.stdout.decode("utf-8", "replace")
        lines = [x for x in text.splitlines() if x.strip()]
    except Exception as e:
        # ★黙って通さない★（検査が動かないこと自体を知らせる）
        return [f"Codexへの未報告を数えられません（{type(e).__name__}）: {e}"]
    if len(lines) < LIMIT:
        return []
    head = " / ".join(x[:56] for x in lines[:4])
    return [f"Codexへ未報告のスクリプト変更が {len(lines)} 件たまっています: {head}"
            f" → 実コードを見せて報告し、`python scripts/codex_reported.py` を実行してください"]


def check_32_dangling_machine_page(machines: list) -> list[str]:
    """★一覧に出るのにページが無い機種★（2026-07-31・新台追加タスクで判明）

    `index.html` は machines.json の全機種に `/machines/{slug}/` へリンクを張る。
    そのため machines.json に足しただけでページを作らないと、
    **本番に404リンクができる**。新台追加が `--apply` でデータだけ書ける以上、
    ここで機械的に止める。
    """
    ng = []
    for m in machines:
        slug = m.get("slug")
        if not slug:
            continue
        if not (BASE / "machines" / slug / "index.html").is_file():
            ng.append(f"{slug}: 一覧に載っているのに machines/{slug}/index.html がありません"
                      f"（トップページから404になります）")
    return ng


def check_33_publish_in_progress(machines: list) -> list[str]:
    """★途中で終わった公開が残っていないか★（2026-07-31・Codex9回目）

    電源断で止まると、ページも一覧もそろってしまい、
    ほかの検査では「中断された処理」と「正常な新台」を区別できない。
    公開処理は書き始める前に目印を作り、全部終わってから消す。
    目印が残っていれば、まだ途中。
    """
    p = BASE / ".publish-in-progress.json"
    if not p.is_file():
        return []
    try:
        got = _sj.read_json(str(p), expect=dict)
        who = f"{got.get('slug')} / {got.get('started_at')}"
    except Exception:                     # noqa: BLE001
        who = "（目印が壊れています）"
    return [f"公開が途中で終わっています（{who}）。中身を確かめて直すか元に戻し、"
            f".publish-in-progress.json を消してください"]


def check_34_indexing_policy_applied(machines: list) -> list[str]:
    """★緊急overrideの切り替えが、成果物へ反映されているか★

    （2026-08-04・Codex73回目の指摘1）
    indexing-policy.json を切り替えても、公開済みの静的HTMLとsitemapは
    そのままなので、「スイッチを入れたつもりで何も起きていない」状態になる。
    判定書に焼かれた policy_mode といまの設定を突き合わせて検知する。
    """
    stale = _pd.stale_decisions(machines)
    if not stale:
        return []
    return [f"緊急overrideの切り替えが未反映の機種 {len(stale)}件: {stale[:5]}"
            "（python scripts/apply_indexing_policy.py --apply で反映）"]


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
    ("14_slugハードコード", check_14_slug_hardcode),
    ("15_99999残留", check_15_render_99999),
    ("16_文体混在", check_16_writing_style),
    ("17_他サイト名露出", check_17_external_site_names),
    ("18_サブディレクトリbase_href", check_18_subdir_base_href),
    ("19_lead内Markdown残留", check_19_lead_markdown),
    ("20_旧URLリンク残留", check_20_old_url_links),
    ("21_プリレンダ検証", check_21_prerender),
    ("22_機種重複検知", check_22_duplicate_machines),
    ("23_CLAUDE_md肥大検知", check_23_claude_md_size),
    ("24_noindex整合", check_24_robots_noindex),
    ("25_body型", check_25_section_body_type),
    ("26_空段落", check_26_empty_paragraph),
    ("27_ハブ件数整合", check_27_hub_counts),
    ("28_settei表の列数整合", check_28_settei_table_shape),
    ("29_制御文字混入", check_29_control_chars),
    ("30_記事内の自己矛盾", check_30_surface_conflicts),
    ("31_Codexへの未報告", check_31_codex_report),
    ("32_ページ欠けの機種", check_32_dangling_machine_page),
    ("33_公開が途中で終わっている", check_33_publish_in_progress),
    ("34_検索方針の反映漏れ", check_34_indexing_policy_applied),
]


def main():
    try:
        machines = load_json(BASE / "assets" / "data" / "machines.json")
    except _sj.SafeJsonError as e:
        print(f"★機種データが読めません: {e}★")
        sys.exit(1)
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

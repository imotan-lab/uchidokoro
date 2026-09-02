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
import local_paths as _lp             # noqa: E402  ★置き場は1か所で決める★

# ★ビルドの出力は監査の対象外★（2026-07-30）
#   .preview-site/ は公開されない写し（全ページ noindex・robots全面Disallow）、
#   _site/ は保護CIが空から組み立てる成果物。どちらもGit管理外なので、
#   ここを本番と同じ物差しで測ると「直す必要のないNG」が出て判断を誤らせる。
#   ★写し自身の検査は build_preview_site.py が、成果物の検査は
#     build_pages_artifact.py の audit() が別に行う★
BUILD_DIRS = {".preview-site", "_site", "_site.next"}
# ★試験用に保存した他所のページも対象外★（2026-08-16）
#   tests/fixtures/ には**他サイトの実ページをそのまま保存**してある。
#   これは「ネットに出ずに試験を回す」ための材料で、公開もしないし
#   当サイトのひな型でもないので、当サイトの物差し（base href・
#   インラインstyle等）で測ると直しようのないNGが出る。
#   ★中身は絶対に書き換えない★＝書き換えたら試験が実物と食い違う。
FIXTURE_DIRS = {"tests"}


# ★他サイト名の名簿（監査17と recheck.competitor_names_gone が共有する正本）★
#   ★同じ規則を2か所に書かない★（2026-08-21）＝
#   自動修理が「他サイト名を消した」と言うとき、閉じてよいかを判定する検査は
#   監査とまったく同じ名簿で見ないといけない。
def strip_allowed_basis(t: str) -> str:
    """★根拠の名乗りだけを外す★（他サイト名の検査の前に通す）

    ★★2026-08-24・Codexの16回目★★
      運営者が決めた「DMM単独確認」の名乗りは、
      **読者に根拠を正しく伝えるために意図して書いている**もの。
      ここを他サイト名として弾くと、
      ★その例外を使った記事は必ず監査で巻き戻され、一度も公開できない★
      （実際そうなっていた。記事づくりと監査を別々に試験していて見えなかった）。
    ★正本は build_new_article 側の文言★＝そちらを変えたらここも自動で合う。
    ★外から呼べる形にしてある★＝記事づくりの試験がここを通して確かめる。
    """
    import build_new_article as _ba_names
    out = str(t or "")
    for _ok in list(_ba_names.BASIS_SUFFIX.values()) + [
            _ba_names.SINGLE_SOURCE_NOTE]:
        if _ok:
            out = out.replace(_ok, "")
    return out


COMPETITOR_NAMES = (
    # 競合解析サイト
    "スロパチクエスト", "ちょんぼりすた", "ナナプレス", "DMM", "ぱちタウン", "スロラボ",
    # ★2026-08-22 追加★（更新タスクが台帳#457で見つけた漏れ）
    #   ★見つかり方★＝akudama の記事に「（スロベース・複数解析サイトで確認）」と
    #   出ているのに、監査17も recheck の competitor_names_gone も PASS した。
    #   両方が同じ名簿を読むので、★名簿に無い名前は二重に素通りする★。
    "スロベース",
    #   P-WORLD は2026-08-16に出典として使うのをやめたが、記事には残っていた
    #   （world_dai_star「P-WORLDでは「紫」を「ピンク」…」＝同日に直した）。
    "P-WORLD", "PWORLD", "P-world",
    # 削除されたアフィリエイトサービス（もしもアフィリエイト・パチスロでは利用不可）
    "もしもアフィリエイト", "moshimo.com", "af.moshimo", "i.moshimo",
)

# ★★ここに入れてはいけない名前★★（2026-08-22・実データで数えて決めた）
#   「一撃」は出典サイトの名前でもあるが、★普通の言葉でもある★。
#   実測＝14機種で使われていて、6件を読んだところ全部が普通の用法だった
#   （「一撃性が魅力」「一撃2000枚クラス」「一撃出玉を左右します」）。
#   ＝名簿に入れると**全部が誤検知**になる。
#   ★機械の名簿は、意味を読まなくても判る名前だけにする★。
#   紛らわしい名前は、更新タスクの STEP 1-Q（2AIが記事を読む）が見る。
#   「からくりサーカス」も同じ理由で入れない（★機種名★）。
_NOT_COMPETITOR_NAMES_WHY = "一撃・からくりサーカス＝普通の語／機種名なので入れない"
# ★★曖昧な表示名と、一意な別表記は分ける★★（2026-08-24・Codexの16回目）
#   「一撃」を外す判断は正しいが、★一意な別表記まで外す理由はない★。
#   ドメインや、ほかの意味を持たない呼び方は入れる。
_COMPETITOR_ALIASES = ("1geki.jp", "1geki", "なな徹", "やんちゃプレス",
                       "ハズセ", "HAZUSE", "slopachi-quest",
                       "chonborista", "nana-press", "yancha-press")


def is_build_output(path: Path) -> bool:
    """ビルド出力（写し・成果物）・試験用の保存ページ配下のパスか。"""
    try:
        rel = path.resolve().relative_to(BASE.resolve())
    except ValueError:
        return False
    return bool(set(rel.parts) & (BUILD_DIRS | FIXTURE_DIRS))


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
#   ★JavaScriptで後から入れる形も見る★（2026-08-12・運営者から再度の指摘）
#     トップページのボタンが `全${totalCount}機種を見る` を後から差し込んでいた。
#     ファイルの文字だけを見ると、数字が入っていないので気づけない。
_TOTAL_COUNT_PAT = re.compile(
    r"(全|全部で|掲載|対象機種数[:：]?\s*)(<[^>]+>)?\s*"
    r"(\d{2,3}|\$\{[^}]+\}|\{\{[^}]+\}\})\s*(</[^>]+>)?\s*機種")


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
    # ★説明書も見る★（2026-08-12・運営者から再度の指摘）
    #   公開ページは守れていたのに、CLAUDE.md 自身が
    #   「機種数は書かない」と書きながら「全120機種」と書いていた。
    #   ルールを書いた場所こそ、静かに戻りやすい。
    for rel in ("README.md", "about.html", "guide-ichiran.html", "CLAUDE.md",
                "index.html"):
        # ★手元にしか無いファイルは、無くても止めない★（2026-08-12）
        #   CLAUDE.md は Git 管理外なので CI には存在しない。
        #   読めないだけで**サイトの配信が落ちた**（実際に3回の失敗メール）。
        #   ★見張りたいのは「書いてあること」であって「在ること」ではない★
        p = BASE / rel
        if not p.is_file():
            continue
        text = load_text(p)
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
    """本文が短い記事を知らせる（★止めない＝お知らせだけ★）

    ★「1500字以上」ルールは2026-07-24に廃止した★（字数のための加筆をしない）。
    ところが監査だけ残っていて、**廃止したはずのルールが公開を止めていた**
    （2026-08-06、裏取りできた事実だけで書いた6機種が引っかかった）。
    決めたことをコードに反映する。短いこと自体は知りたいので、
    NGではなく「お知らせ」として出す。
    """
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
            ngs.append(f"{slug}: 本文{total}字（短め・★止めません★）")
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
        """★見つけ方は fix_plain_style の表と同じにする★
        （2026-08-21・台帳#233・#122）

        ★直す前に取りこぼしていた形★＝
          「〜がある。」「〜活用できる。」「〜広がる。」「〜変わる。」など。
          品質レビューが9機種で見つけていたのに、この検査は0件だった
          ＝★人が読んで見つかるものを、機械が見落としていた★。

        ★同じ規則を2か所に書かない★＝
          直す側（fix_plain_style.ENDINGS）が持っている「常体の終わり方」を
          そのまま使う。直す側に形を足せば、この検査も一緒に増える。
        """
        s = sent.rstrip("。、,!?").strip()
        if not s:
            return False
        last = s[-5:]
        if _re.search(r"(?:です|ます|でしょう|ません|でした|ました|ください|でしょ)$", last):
            return False
        if _re.search(r"(?:だ|である|した|する|った|ない|だが|だろう|だろ|なる|させる|られる|られた)$", last):
            return True
        # ★直す側の表に載っている終わり方も常体として数える★
        try:
            import fix_plain_style as _fps
            return any(s.endswith(a.rstrip("。")) for a, _b in _fps.ENDINGS)
        except Exception:            # noqa: BLE001
            return False

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
        # ★★表の注記も見る★★（2026-08-21・台帳#332）
        #   ★直す前は body だけだった★ので、設定示唆まとめの
        #   `tables[].note` に常体が残っていた
        #   （実例＝hokuto「…高設定否定にならない。」）。
        #   ★直す側（fix_plain_style）と同じ場所を見る★
        try:
            import fix_plain_style as _fps2
            for _w, _old, _new, _title in _fps2.plan_for(d):
                if _w[0] in ("table_note", "sec_note"):
                    plain_sentences.append((_title, _old[:80]))
        except Exception:                 # noqa: BLE001
            pass
        for s in d.get("sections", []):
            if s.get("type") == "settei":
                continue
            body = s.get("body")
            text = " ".join(body) if isinstance(body, list) else (body if isinstance(body, str) else "")
            if not text or len(text) < 30:
                continue
            sents = [x for x in _re.split(r"(?<=。)", text) if x.strip()]
            for sent in sents:
                # ★★見出しの行は文ではない★★（2026-08-21）
                #   「**機種名**：スマスロ転生したらスライムだった件」を
                #   ★常体だと言っていた★＝機種名の末尾を語尾と読んでいた。
                #   ★直す側（fix_plain_style）と同じ条件で外す★＝
                #   見出し・箇条書きの行頭、句点で終わらない行。
                _t = sent.strip()
                if _t.startswith(("**", "・", "-", "＊", "※")):
                    continue
                if not _t.endswith("。"):
                    continue
                if _is_plain(sent):
                    plain_sentences.append((s.get("title", ""), _t))
        if plain_sentences:
            for title, sent in plain_sentences[:2]:  # 機種ごとに最大2件
                ngs.append(f"{slug}: 常体文混在 [{_redact(title)}] {_redact(sent)}")
    return ngs


# ★見ないでよいのは「機械が同定に使う跡」だけ★（2026-08-16・依頼213の指摘）
#   ★identity 全部を外すのは行き過ぎ★＝そこに読者向けの文字列
#   （announced_name・型式名）を混ぜれば検査を逃げられてしまう。
#   ★machines.json は読者が取得できる★（Pagesはリポジトリ全体を配信する。
#   `/assets/data/*.json` は200＝publish-pages.yml に明記）。
#   描画されないだけで「読者から見えない」わけではないので、
#   外すのは**URL・証跡・出典ドメイン・結び付け方**に限る。
_EVIDENCE_KEYS = {
    "official_product_url", "identity_binding", "identity_evidence_ref",
    "identity_evidence", "_model_code_sources", "_observed_model_code_sources",
    "_legacy_evidence_ref", "_legacy_official_product_url",
}


def _strip_identity(text: str) -> str:
    """★同定に使う跡（URL・証跡・出典ドメイン）だけを外した本文★

    ★文字列を切り貼りしない★＝JSONとして読み直し、決めた項目だけ落としてから
    もう一度文字にする。正規表現で括弧を数えると、入れ子や記号で崩れる。
    読めなければ**そのまま全部見る**（見落とすより厳しいほうに倒す）。

    ★`announced_name` と型式名は外さない★＝読者が読める文字列なので、
    ここに他サイト名が入れば今までどおりNGにする。
    """
    import json as _json
    try:
        d = _json.loads(text)
    except Exception:                     # noqa: BLE001
        return text
    ms = d["machines"] if isinstance(d, dict) else d
    if not isinstance(ms, list):
        return text
    for m in ms:
        ident = m.get("identity") if isinstance(m, dict) else None
        if isinstance(ident, dict):
            for k in list(ident):
                if k in _EVIDENCE_KEYS:
                    ident.pop(k, None)
    return _json.dumps(d, ensure_ascii=False)


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
    # ★名簿は下の COMPETITOR_NAMES が正本★（recheck の competitor_names_gone も同じ物を読む）
    sites = list(COMPETITOR_NAMES) + list(_COMPETITOR_ALIASES)
    # 業界用語と区別：「DMM」は「DMM ぱちタウン」サイト名のみ検出（他用途は無いと仮定）
    detail_dir = BASE / "assets" / "data" / "machine-details"
    # ★★根拠の名乗りだけは別扱い★★（2026-08-24・Codexの16回目）
    #   ★運営者が決めた「DMM単独確認」の名乗り★は、
    #   **読者に根拠を正しく伝えるために意図して書いている**もの。
    #   ここを他サイト名として弾くと、
    #   ★その例外を使った記事は必ず監査で巻き戻され、一度も公開できない★
    #   （実際そうなっていた。記事づくりと監査を別々に試験していて見えなかった）。
    #   ★正本は build_new_article 側の文言★＝そちらを変えたらここも自動で合う。
    for jf in sorted(detail_dir.glob("*.json")):
        text = strip_allowed_basis(load_text(jf))
        for s in sites:
            c = text.count(s)
            if c:
                ngs.append(f"machine-details/{jf.name}: '{s}' × {c}件 露出")
    # machines.json も対象（checker.note / strategy / seo.title 等）
    mj = BASE / "assets" / "data" / "machines.json"
    if mj.is_file():
        # ★同定の控え（identity）は読者に出ない★（2026-08-16・台帳#376）
        #   ここには機種ページのURLが入る。規約でDMMへ移した結果、
        #   同定用のURLに "DMM" が含まれるようになったが、
        #   **読者向けページには一切出ない**（描画側は identity を読まない。
        #   実データで machines/{slug}/index.html に0件であることを確認）。
        #   ★見るのを狭めるのはここだけ★＝本文・見出し・checker の注記は
        #   今までどおり全部見る。
        text = _strip_identity(load_text(mj))
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
        import html_check as _hc18
        # ★★字面ではなく構造で見る★★（2026-08-31・実際に事故を起こした）
        #   直す前は正規表現だったので、★JSのコメントの中の文字列でも合格★した。
        #   実際、コメントに書いた文字列を生成器が実タグと誤認して、
        #   120ページから base が消えたのに、この監査は気づかなかった。
        _bp18 = _hc18.base_problem(text)
        if _bp18:
            rel = f.relative_to(BASE).as_posix()
            ngs.append(f"{rel}: {_bp18}"
                       "（サブディレクトリ配下のHTMLには必須）")
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
    ★報告したら記録する★（2026-08-09に領収書方式へ変更・依頼126）
      python scripts/codex_reported.py --receipt <領収書のパス>
      領収書は codex_review.sh がCodexを実際に呼んで成功したときにだけ発行する。
      以前は「引数なしで実行すれば印が付く」形だったため、台帳に書いた文章の
      バッククォートをシェルが実行して**報告していないのに印が付いた**。
    """
    import subprocess
    STATE = _lp.doc("last_codex_report.json")
    # ★1件でも知らせる★（2026-08-09。3件まで黙っていた）
    #   「3件たまってから」だと、2件までは何も出ないので気づけない。
    #   実際この日、コードを直しては報告せずに進み、運営者から
    #   「言われなくてもやって。記憶しているはず」と指摘された。
    #   覚え直すのではなく、**最初の1件から見えるようにする**。
    LIMIT = 1
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
            # ★★黙って通さない★★（2026-08-21・台帳#295の③）
            #   直す前は空を返していたので、
            #   ★記録が壊れているときに「未報告0件」に見えた★
            #   （記録のコミットが消えた・rebaseで別物になった等）。
            #   ＝見張りが効かなくなったことに誰も気づけない。
            _err = (r.stderr or b"").decode("utf-8", "replace").strip()
            return [f"Codexへの未報告を数えられません"
                    f"（記録のコミット {str(last)[:12]} を git が見つけられません）"
                    f": {_err[:120]}"
                    " → python scripts/codex_receipt.py list で領収書を確かめ、"
                    "python scripts/codex_reported.py --receipt <領収書> で記録し直す"]
        text = r.stdout.decode("utf-8", "replace")
        lines = [x for x in text.splitlines() if x.strip()]
    except Exception as e:
        # ★黙って通さない★（検査が動かないこと自体を知らせる）
        return [f"Codexへの未報告を数えられません（{type(e).__name__}）: {e}"]
    if len(lines) < LIMIT:
        return []
    head = " / ".join(x[:56] for x in lines[:4])
    return [f"Codexへ未報告のスクリプト変更が {len(lines)} 件たまっています: {head}"
            f" → 実コードを見せて報告し、"
            f"python scripts/codex_reported.py --receipt <領収書> を実行してください"
            f"（領収書の一覧は python scripts/codex_receipt.py list）"]


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
    # ★判定書だけでなく、実際のページのnoindexとsitemapまで突き合わせる★
    #   （2026-08-04・Codex74回目の指摘1。部分的に反映されたまま落ちた状態を
    #     「反映済み」と誤認しないため）
    try:
        import apply_indexing_policy as _ap
        got = _ap.plan()
    except Exception as e:                # noqa: BLE001
        return [f"検索方針の反映状況を確かめられません: {e}"]
    if not got["changes"]:
        return []
    detail = ", ".join(f"{c['slug']}（{'/'.join(c['why'])}）"
                       for c in got["changes"][:5])
    return [f"検索方針が成果物へ反映されていない機種 {len(got['changes'])}件: {detail}"
            "（python scripts/apply_indexing_policy.py --apply で反映）"]


def check_36_duplicate_facts(machines: list) -> list[str]:
    """★同じ節に同じ事実が2行あるか★（2026-08-07・台帳#262）

    「設定変更後：天井が650G+αに短縮」と「設定変更時：天井が650G+αに短縮」の
    ように、**同じ事実を指す見出しの言い換え**で同じ行が二重に入っていた。
    実データで4機種に出ていて、読者にそのまま見えていた。

    ★書き手ごとに直すのではなく、全機種を機械で見る★
      重複を作るのは grow_legacy だけとは限らない（手で足すこともある）。
      入口を1つずつ塞ぐより、出来上がったものを毎回数えるほうが確実。
    """
    try:
        import grow_legacy as _gl
    except Exception as e:                # noqa: BLE001
        return [f"重複の検査ができません: {e}"]
    ngs = []
    for m in machines:
        slug = m.get("slug")
        p = BASE / "assets" / "data" / "machine-details" / f"{slug}.json"
        if not p.exists():
            continue
        try:
            det = load_json(p)
        except Exception as e:            # noqa: BLE001
            ngs.append(f"{slug}: 記事データが読めません（{e}）")
            continue
        for sec in det.get("sections") or []:
            seen = {}
            for b in sec.get("body") or []:
                mt = _gl._LABELED.match(str(b).strip())
                if not mt:
                    continue
                key = _gl._canon_label(mt.group("label"))
                if key in seen:
                    ngs.append(
                        f"{slug} /「{sec.get('title')}」に同じ見出しの行が2つ: "
                        f"{seen[key][:40]} ／ {str(b)[:40]}")
                else:
                    seen[key] = str(b)
    return ngs


def check_35_risky_atoms(machines: list) -> list[str]:
    """★公開記事に残る「危ない表現」が増えていないか★（2026-08-05）

    Phase 0 で落とすと決めた表現（利益・行動の断定／設定段階の非存在断定）が
    既存記事に残っている。全部直すには人の手が要るので、
    **いま残っている数を基準として持ち、増えたら止める**。
    ＝直すのは人、増やさないのは機械。

    ★なぜ「0件」にしないのか★（2026-08-05・Codex101回目）
      段落単位で安全に消せるものが実データで0件だった（危ない文が
      他の事実と同居している／設定段階は消すと情報が減る）。
      0件を条件にすると、直せないまま監査が毎日赤くなり誰も見なくなる。
      基準値を置いて「減る方向にしか動かせない」ようにする。
    """
    base_p = BASE / "assets" / "data" / "risky-baseline.json"
    try:
        import risky_atoms as _ra
        rows = _ra.plan()
    except Exception as e:                # noqa: BLE001
        return [f"危ない表現を数えられません: {e}"]
    now = len(rows)
    try:
        base = load_json(base_p)
        limit = int(base["max_atoms"])
    except Exception as e:                # noqa: BLE001
        return [f"危ない表現の基準値を読めません（{base_p.name}）: {e}"]
    if now > limit:
        slugs = sorted({r["slug"] for r in rows})[:6]
        return [f"危ない表現が増えています: {now}箇所（基準 {limit}箇所）"
                f" 例: {', '.join(slugs)}"
                "（python scripts/risky_atoms.py で場所を確認）"]
    if now < limit:
        return [f"危ない表現が {limit} → {now} 箇所に減りました。"
                "assets/data/risky-baseline.json の max_atoms を下げてください"
                "（減った分を基準に戻さないため）"]
    return []


def check_55_plain_style(machines: list) -> list[str]:
    """★記事の文体（です・ます）から外れた文が増えていないか★
    （2026-08-31・運営者の指示）

    ★運営者の言葉★
      > 文体は統一したいね　今後も。
      > タスクが走るたびに表記変わるのは避けたい

    ★★負の検査をやめた★★＝
    いままで文体を見ていたのは `recheck.plain_style_gone` の
    「常体の文末**19通り**」という名簿で、名簿に無い言い方は素通りした
    （対照実験で「…となる。」が通ることを確認済み）。
    ここは**正の検査**（`style_check.py`）＝
    「です・ます で終わっていること」を求め、外れたものを全部挙げる。

    ★0件を条件にしない★＝実測1189件あり、直すのは毎朝のタスクの仕事。
    項目35（危ない表現）と同じく**基準値を置いて、増える方向を止める**。
    ＝★直すのは2AI、増やさないのは機械★。
    """
    sys.path.insert(0, str(BASE / "scripts"))
    try:
        import style_check as _sc
        rows = _sc.scan_all()
    except Exception as e:                # noqa: BLE001
        return [f"文体を数えられません: {type(e).__name__}: {e}"]
    base_p = BASE / "assets" / "data" / "style-baseline.json"
    try:
        base = load_json(base_p)
    except Exception as e:                # noqa: BLE001
        return [f"文体の基準値を読めません（{base_p.name}）: {e}"]
    # ★★件数ではなく集合で比べる★★（2026-08-31・Codexの6回目の指摘）
    #   ★件数だけだと入れ替えを許す★＝古い違反を1件直して
    #   別の文に1件入れると、同じ数のまま通る。
    #   ＝運営者の要望「走るたびに表記が変わるのを避けたい」を満たせない。
    d = _sc.compare(rows, base)
    if d["new"]:
        ex = [f"{r['slug']}／{r['sentence'][-24:]}" for r in d["new"][:3]]
        return [f"文体の新しい違反が {len(d['new'])} 件あります"
                f"（いま {d['now']}件／基準 {d['base']}件）"
                f" 例: {' / '.join(ex)}"
                "（python scripts/style_check.py --slug <機種> で場所を確認）"]
    if d["gone"]:
        return [f"文体の違反が {d['base']} → {d['now']} 件に減りました。"
                "python scripts/style_check.py --update-baseline "
                "で基準値を書き直してください（減った分を基準に戻さないため）"]
    return []


# ★タスクの契約に必ず要る鍵★（2026-09-01・Codexのレビュー30の指摘2）
#   ★1つでも欠けたら、その分の監査が黙って消える★
_CONTRACT_KEYS = ("live", "stopped", "skills")


def _skill_contract(base: str):
    """★どのタスクが動いていて、どれを止めたか★を外の設定から読む。

    ★公開されるこのファイルにタスク名を書かない★（2026-08-13・依頼177）
      手順書をリポジトリへ置かない理由（内部構成が読まれる）と同じ。
      置き場に `tasks-contract.json` を置き、そこから読む。

    ★★返すのは (契約, 問題) の組★★（2026-09-01・Codexの指摘3）
      ★直す前は「無い／壊れている／辞書でない」を全部 {} にまとめていた★ので、
      呼び手の `if not conf: return` が★契約が消えても壊れても黙って通した★
      ＝見張りが静かに消える。
      ★呼び手はこの置き場が在ることを既に確かめている★（別PCは手前で返る）ので、
      ここから先の「無い」は事故。
      ★正しい契約で中身が0件なのは正常★（問題は空文字で返す）。
    """
    import json
    p = os.path.join(base, "tasks-contract.json")
    if not os.path.isfile(p):
        return {}, ("★タスクの契約がありません★"
                    "（置き場はあるのに契約が無い＝見張りが効きません）")
    try:
        with open(p, encoding="utf-8") as f:
            got = json.load(f)
    except Exception as e:                # noqa: BLE001
        return {}, f"★タスクの契約を読めません★（{type(e).__name__}）"
    if not isinstance(got, dict):
        return {}, ("★タスクの契約の形が違います★"
                    "（いちばん外側が辞書ではありません）")
    # ★★3つの鍵がそろっていること★★（2026-09-01・Codexのレビュー30の指摘2）
    #   ★直す前は「いちばん外側が辞書か」しか見ていなかった★ので、
    #   `skills` の鍵を1つ消すだけで**スキルの監査が丸ごと黙った**
    #   （呼び手が `conf.get("skills") or ()` で空として扱うため）。
    #   ★空の正しい契約は `{}` ではなく
    #     `{"live": [], "stopped": [], "skills": []}`★
    missing = [k for k in _CONTRACT_KEYS if k not in got]
    if missing:
        return {}, f"★タスクの契約に鍵がありません★（{missing}）"
    # ★前後の空白も許さない★（2026-09-01・Codexのレビュー31の指摘4）
    #   ★`" old-task "` は形としては通るが、照合は空白ごと探す★ので
    #   `old-task を実行` を捕まえられない＝黙って見張りが外れる。
    bad = [k for k in _CONTRACT_KEYS
           if not isinstance(got[k], list)
           or any(not isinstance(x, str) or not x.strip() or x != x.strip()
                  for x in got[k])]
    if bad:
        return {}, (f"★タスクの契約の中身の形が違います★（{bad} は"
                    "前後に空白のない、中身のある文字列の並びで）")
    # ★知らない鍵も止める★（同・閉じた契約にする）
    #   ★`skils` のような書き間違いが黙って無視される★＝
    #   その分の監査が消えるのに、誰にも分からない。
    #   `_` で始まるものは覚え書き（`_why`）なので許す。
    extra = [k for k in got if k not in _CONTRACT_KEYS and not k.startswith("_")]
    if extra:
        return {}, (f"★タスクの契約に知らない鍵があります★（{extra}）"
                    "＝書き間違いだと、その分の監査が黙って消えます")
    return got, ""


# ★コマンドの形をした行の先頭語★（2026-09-01）
#   ★意味は見ない★＝何をするコマンドかは判定しない。
#   「その行はコマンドか」という**形**だけで決める。
_CMD_HEADS = ("python", "python3", "py", "bash", "sh")


def _doc_paths(text: str) -> list:
    """★手順書が実際に叩いているファイル★を返す（2026-09-01）。

    ★構造で決める★＝コマンドの形をした行の中の、`.py` / `.sh` で終わる語。
    ★道筋を含む語だけ★＝`codex_review.sh` のような裸の名前は、
      どこにあるか分からないので見ない（ありもしない場所を探して誤検知する）。
    ★直す前は `python scripts/*.py` の形だけ★だったので、
      スキルの中心である `codex_review.sh`（絶対パス）が対象外だった。
    """
    import re as _re
    out = []
    for line in text.splitlines():
        # ★候補は「行そのもの」と「バッククォートで囲んだ範囲」★
        #   （2026-09-01・対照実験が捕まえた）＝
        #   手順書では「実行は `python scripts/x.py` です。」のように
        #   文の途中へ埋め込むことが多く、行の前後を外すだけでは届かない。
        #   ★これも構造★＝囲みは Markdown のコードであって、意味ではない。
        cands = [line.strip().strip("`")]
        cands += _re.findall(r"`([^`]+)`", line)
        for cand in cands:
            bare = cand.strip().lstrip("$").strip()
            head = bare.split(" ", 1)[0] if bare else ""
            if head not in _CMD_HEADS:
                continue
            for tok in _re.findall(r'[^\s"\']+\.(?:py|sh)', bare):
                tok = tok.strip('"\'')
                if "/" in tok or "\\" in tok:
                    out.append(tok)
    # ★同じ道筋は1度だけ★＝行そのものと囲みの両方に当たると二重になる
    return list(dict.fromkeys(out))


def _doc_file_exists(rel: str) -> bool:
    """その道筋にファイルが在るか（絶対でもリポジトリ相対でも見る）。"""
    import re as _re
    p = str(rel or "").replace("\\", "/")
    if _re.match(r"^[A-Za-z]:/", p) or p.startswith("/"):
        return os.path.isfile(p)
    return os.path.isfile(os.path.join(BASE, p.lstrip("./")))


def skill_doc_problems(name: str, text: str, stopped, exists) -> list:
    """★対話セッション用の手順書（スキル）の食い違い★だけを返す。

    読み書きしない（2026-09-01）＝
      exists(rel) … そのスクリプトが在るか

    ★見るのは2つ★
      ①実在しないスクリプトを叩けと書いていないか
      ②止めた（消した）タスクを実行しろと書いていないか

    ★Codexの呼び方は見ない★＝`codex_with_lock.sh` 経由は
      **無人タスクだけ**の決まり（ロックを失うと黙って死ぬため）。
      対話セッションはロックを持たないので、素で呼ぶのが正しい。
    """
    out = []
    for rel in _doc_paths(text):
        if not exists(rel):
            out.append(f"{name}: 手順書が無いスクリプトを指しています → {rel}")
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for st in stopped:
            if st in line and "を実行" in line:
                out.append(
                    f"{name}: 止めたタスク {st} を実行するよう書いています")
    return out


# ★スキルの手順書で「止めなければいけない」形★（2026-09-01）
_SKILL_MUST_CATCH = {
    "実在しないスクリプトを叩けと書いている":
        "```bash\npython scripts/no_such_tool_xyz.py --selftest\n```\n",
    "止めたタスクを実行しろと書いている":
        "uchidokoro-fact-check を実行すること。\n",
    "文の途中に混ざっていても見つける":
        "まず uchidokoro-fact-check を実行してから次へ進む。\n",
    # ★2026-09-01・Codexの指摘4★＝スキルの中心は .sh（絶対パス）なのに
    #   `python scripts/*.py` の形しか見ていなかった。
    "実在しない .sh を絶対パスで叩く":
        'bash "D:/no_such_dir_xyz/codex_review.sh" a b\n',
    "バッククォートで囲んだコマンドの中":
        "実行は `python scripts/no_such_tool_xyz.py --selftest` です。\n",
    "先頭が $ のコマンド":
        "$ python scripts/no_such_tool_xyz.py\n",
}
# ★止めてはいけない形★
_SKILL_MUST_PASS = {
    "実在するスクリプト": "```bash\npython scripts/audit_site.py\n```\n",
    # ★道筋を含まない裸の名前は見ない★（どこにあるか分からないため）
    "裸のファイル名（道筋なし）": "bash codex_review.sh ask.md out.txt\n",
    # ★コマンドの形をしていない行は見ない★（文章の中の名前）
    "文章の中でファイル名を挙げるだけ":
        "むかしは no_such_tool_xyz.py を使っていた。\n",
    "止めたタスクの名前を「実行」以外で挙げるだけ":
        "uchidokoro-fact-check は2026-08-22に削除した。\n",
    "コメント行の中":
        "# uchidokoro-fact-check を実行すること（昔の書き方）\n",
    "★Codexを素で呼ぶのは対話セッションでは正しい★":
        "bash codex_review.sh ask.md out.txt 900 3 high\n",
}


def _check_37_wiring() -> list:
    """★live とスキルの「配線」を実経路で試す★（2026-09-01・Codexの指摘2）

    ★共通関数の単体試験だけでは足りない★＝
      `check_37_skill_vs_code()` の中の**呼び出し行**を消しても、
      共通関数を直接試す試験は緑のまま（罠③）。

    ★一時の置き場を作って本物の経路を通す★（実物の手順書には触らない）。
    """
    import json as _js
    import shutil as _sh
    import tempfile as _tf

    bad = []
    # ★★自分の一時フォルダの「中」に置き場を作る★★（2026-09-02・Codexの指摘）
    #   ★直す前は一時領域の直下に `skills` を作って消していた★＝
    #   共通の名前なので、同名のものがあれば巻き込む（しかも復元できない）。
    #   本体は `base` の**隣**を見るので、`base` を自分の中に作れば
    #   隣も自分の中になる。
    _root39 = _tf.mkdtemp(prefix="wiring_")
    d = os.path.join(_root39, "scheduled-tasks")
    os.makedirs(d, exist_ok=True)
    try:
        # ── live 側 ──
        os.makedirs(os.path.join(d, "test-live"), exist_ok=True)
        with open(os.path.join(d, "test-live", "SKILL.md"), "w",
                  encoding="utf-8") as f:
            f.write("test-stopped を実行すること。\n"
                    "```bash\npython scripts/no_such_tool_xyz.py\n```\n")
        with open(os.path.join(d, "tasks-contract.json"), "w",
                  encoding="utf-8") as f:
            _js.dump({"live": ["test-live"], "stopped": ["test-stopped"],
                      "skills": []}, f)
        # ★スキル側も同じ置き場の隣に作る★（実装は base の親から組み立てる）
        sk = os.path.join(os.path.dirname(d), "skills", "test-skill")
        os.makedirs(sk, exist_ok=True)
        with open(os.path.join(sk, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("```bash\npython scripts/no_such_tool_xyz.py\n```\n")

        keep = os.environ.get("UCHIDOKORO_TASKS_DIR")
        os.environ["UCHIDOKORO_TASKS_DIR"] = d
        try:
            got = check_37_skill_vs_code([], required=True)
            if not any("test-live" in x and "test-stopped" in x for x in got):
                bad.append("live 側の手順書が検査されていません"
                           f"（返り: {got[:3]}）")
            if not any("test-live" in x and "no_such_tool_xyz" in x
                       for x in got):
                bad.append("live 側の実在検査が働いていません")
            # ── スキル側 ──
            with open(os.path.join(d, "tasks-contract.json"), "w",
                      encoding="utf-8") as f:
                _js.dump({"live": [], "stopped": [],
                          "skills": ["test-skill"]}, f)
            got2 = check_37_skill_vs_code([], required=True)
            if not any("test-skill" in x and "no_such_tool_xyz" in x
                       for x in got2):
                bad.append("スキル側の手順書が検査されていません"
                           f"（返り: {got2[:3]}）")
            # ── 契約に載っているのに手順書が無い ──
            with open(os.path.join(d, "tasks-contract.json"), "w",
                      encoding="utf-8") as f:
                _js.dump({"live": ["no-such-task"], "stopped": [],
                          "skills": ["no-such-skill"]}, f)
            got3 = check_37_skill_vs_code([], required=True)
            if not any("no-such-task" in x for x in got3):
                bad.append("live の手順書が消えても気づきません")
            if not any("no-such-skill" in x for x in got3):
                bad.append("スキルの手順書が消えても気づきません")
        finally:
            if keep is None:
                os.environ.pop("UCHIDOKORO_TASKS_DIR", None)
            else:
                os.environ["UCHIDOKORO_TASKS_DIR"] = keep
    finally:
        # ★消すのは自分が作った一時フォルダだけ★
        _sh.rmtree(_root39, ignore_errors=True)
    return bad


def _no_dir_ok(fake_base: str, required: bool) -> bool:
    """★置き場が無いときの振る舞いだけを試す★（2026-09-01）

    返り値 True＝止めない ／ False＝止める
    ★環境変数で置き場を差し替えて、本物の置き場には触らない★
    """
    keep = os.environ.get("UCHIDOKORO_TASKS_DIR")
    os.environ["UCHIDOKORO_TASKS_DIR"] = fake_base
    try:
        return check_37_skill_vs_code([], required=required) == []
    finally:
        if keep is None:
            os.environ.pop("UCHIDOKORO_TASKS_DIR", None)
        else:
            os.environ["UCHIDOKORO_TASKS_DIR"] = keep


def _check_37_selftest() -> list:
    """★見張り37（スキルの手順書）自身の対照実験★＝失敗した項目を返す。

    ★52・53・54と同じ形にしてある★＝
      `selftest()` と監査本体の両方が、**同じ関数**を呼ぶ。
    ★2026-09-01：はじめ監査本体にだけ置いて、`--selftest` に足し忘れた★
      （壊し方の道具が「守られていません」と正しく報告した。
        同じ足し忘れを 2026-08-26 に項目54でもやっている）。
    """
    stopped = ("uchidokoro-fact-check",)

    def exists(rel):
        return rel == "scripts/audit_site.py"

    bad = []
    for name, text in _SKILL_MUST_CATCH.items():
        if not skill_doc_problems("x", text, stopped, exists):
            bad.append(f"止められない: {name}")
    for name, text in _SKILL_MUST_PASS.items():
        if skill_doc_problems("x", text, stopped, exists):
            bad.append(f"止めてはいけないのに止めた: {name}")
    return bad


def selftest_37() -> int:
    """スキルの検査が、見逃すと言われた形を全部捕まえるか（表示つき）。"""
    bad = _check_37_selftest()
    n = len(_SKILL_MUST_CATCH) + len(_SKILL_MUST_PASS)
    for b in bad:
        print("  ★NG " + b)
    print(f"{n - len(bad)}/{n} 合格")
    return 1 if bad else 0


def check_37_skill_vs_code(machines: list, required: bool = False) -> list[str]:
    """★手順書が、いま無いものを指していないか★（2026-08-13新設）

    ★なぜ要るか★（実際に起きたこと）
      2026-08-12に新台の探し方をP-WORLD一本へ切り替えたのに、
      手順書（SKILL.md）は「メーカー公式9社を見張る」のままだった。
      手順書はリポジトリの外（Claudeの自動タスクが読む場所）にあるので、
      コードを直しても一緒にレビューされず、**AIが古い前提で判断する**。

    ★手順書そのものはリポジトリに入れない★（2026-08-13・検討して却下）
      中にWindowsのユーザー名が186回、認証情報の置き場所が書かれている。
      このリポジトリは公開なので、入れると構成が読まれる。
      代わりに「食い違いだけ」をここで見る。

    見るもの（機械で確かめられることだけ）:
      ①手順書が叩けと書いているスクリプトが実在するか
      ②止めたタスクを「実行する」と書いていないか
    """
    import re
    # ★このファイルは公開される★（2026-08-13・依頼177のP1）
    #   手順書をリポジトリに置かない理由（ユーザー名・内部構成が読まれる）と
    #   同じ理屈で、**その置き場をここに書いてもいけない**。
    #   置き場は環境変数か、ホームからの相対で組み立てる。
    #   タスク名も外の設定から読む（無ければ検査そのものを行わない）。
    base = os.environ.get("UCHIDOKORO_TASKS_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude", "scheduled-tasks")
    if not os.path.isdir(base):
        # ★★別PCと運用PCを区別する★★（2026-09-01・Codexのレビュー30の指摘3）
        #   ふだんは「手順書が無いだけ」で止めない（別PC・CI）。
        #   ★ローカルの関所からは required=True で呼ぶ★＝
        #   運用PCで置き場ごと消えたときに黙らないため。
        if required:
            return ["★タスクの手順書の置き場がありません★"
                    "（この機械では在るはずです＝見張りが効いていません）"]
        return []
    ng = []
    # ★★見張り自身が働いているかを、毎回いっしょに確かめる★★（2026-09-01）
    #   ★項目51・52・53と同じ理由★＝別コマンドにすると走らせ忘れる。
    #   ★契約より前に置く★＝契約が無いPCでも、見張りの試験だけは通す。
    import io as _io37, contextlib as _cl37
    _b37 = _io37.StringIO()
    with _cl37.redirect_stdout(_b37):
        _sn37 = selftest_37()
    if _sn37:
        ng.append("★この見張り自身の試験が落ちています★: "
                  + " / ".join(x.strip() for x in _b37.getvalue().split(chr(10))
                               if x.strip().startswith("★NG")))
    conf, _conf_ng = _skill_contract(base)
    if _conf_ng:
        # ★黙って通さない★（2026-09-01・Codexの指摘3）＝
        #   契約が消えた・壊れた状態は、見張りが効いていない状態そのもの。
        ng.append(_conf_ng)
        return ng
    stopped = tuple(conf.get("stopped") or ())
    live = tuple(conf.get("live") or ())
    # ★★対話セッション用の手順書（スキル）も見る★★（2026-09-01）
    #   ★スケジュールタスクとは置き場も決まりも違う★ので別に数える。
    #   ・置き場はリポジトリの**外**（無人タスクの手順書の隣）
    #     ★中に置くと clone や写しで必ず落ちる★（2026-09-01に実際にそうなった）
    #   ・★Codexの呼び方の検査は当てない★＝無人タスクだけの決まり
    #     （ロックを失うと黙って死ぬ）。対話セッションはロックを持たない。
    # ★置き場は無人タスクの手順書の隣★（2026-09-01）
    #   ★リポジトリの中に置いてはいけない★＝
    #   clone や障害注入の写しには `.claude/` が無い（gitignore済み）ので、
    #   ★監査が写しの中を見ると必ず落ちる★（実際に ci_repro が赤くなった）。
    #   ここは `base`（= ~/.claude/scheduled-tasks）から組み立てるので、
    #   公開されるこのファイルに置き場を書かない決まりも保たれる。
    _skills_dir = os.path.join(os.path.dirname(base), "skills")
    for skill in tuple(conf.get("skills") or ()):
        f = os.path.join(_skills_dir, skill, "SKILL.md")
        if not os.path.isfile(f):
            ng.append(f"スキル {skill}: 契約に載っているのに手順書がありません")
            continue
        try:
            with open(f, encoding="utf-8") as _fh:
                text = _fh.read()
        except Exception as e:                    # noqa: BLE001
            ng.append(f"スキル {skill}: 手順書を読めません（{e}）")
            continue
        ng.extend(skill_doc_problems(
            f"スキル {skill}", text, stopped, _doc_file_exists))
    for task in live:
        f = os.path.join(base, task, "SKILL.md")
        if not os.path.isfile(f):
            # ★契約に載っているのに手順書が無い★（依頼177のP1）
            #   以前は黙って飛ばしていたので、消えても気づけなかった。
            ng.append(f"{task}: 動かすことになっている手順書がありません")
            continue
        try:
            with open(f, encoding="utf-8") as _fh:
                text = _fh.read()
        except Exception as e:                    # noqa: BLE001
            ng.append(f"{task}: 手順書を読めません（{e}）")
            continue
        # ★★スキルと同じ判定関数を通す★★（2026-09-01・Codexの指摘4）
        #   ★直す前は別実装だった★ので、対照実験がこちらを一度も試さず、
        #   live 側の判定を壊しても緑のままだった（罠③）。
        ng.extend(skill_doc_problems(task, text, stopped, _doc_file_exists))
        # ★★Codexを素で呼んでいないか★★（2026-08-21・Codexの設計レビュー）
        #   ★実際に起きていたこと★＝更新タスクの手順書は、上のほうで
        #   「必ず codex_with_lock.sh 経由」と決めておきながら、
        #   STEP 4 だけ codex_review.sh を**直接**呼んでいた（しかも待ち60分）。
        #   ロックは最後のheartbeatから30分で他のタスクに奪われるので、
        #     相談中にロックを失う → CTXを見失う → ★黙って終わる★
        #   ＝起動はしたのに何もせず死ぬ（lastRunAt では検知できない型）。
        #   ★2回同じ食い違いを作らないよう、機械に見張らせる★
        #
        #   ★見方は単純にする★＝「bash で始まる行」だけを呼び出しとみなす。
        #   説明の文章の中で名前を挙げるのは構わない（それは呼び出しではない）。
        for i, line in enumerate(text.splitlines(), 1):
            bare = line.lstrip()
            if not bare.startswith("bash "):
                continue
            if "codex_review.sh" in bare and "codex_with_lock" not in bare:
                ng.append(
                    f"{task}: {i}行目でCodexを素で呼んでいます"
                    "（codex_with_lock.sh 経由にしてください）")
    return sorted(set(ng))


# ★票の数え方を扱ってよいモジュール★（2026-08-14・依頼194のCodexの助言）
#   ここに挙げたものの中では、独立票の数え方は source_lineage に一本化する。
VOTE_MODULES = ("spec_lookup.py", "ceiling_lookup.py", "at_spec_lookup.py",
                "cz_lookup.py", "model_code_lookup.py",
                # ★2026-08-17・依頼228★ メーカー照合の控えも独立票を数える
                #   ようになった（材料に使うには独立2名鑑）。ここに載せないと、
                #   将来 len(集合) に戻したとき監査39が見逃す。
                "maker_identity_cache.py")
# 票のかたまりを入れている入れ物によく使う名前
_VOTE_WORDS = ("sources", "srcs", "lins", "lineage", "hosts", "votes")
# 票を作っている呼び出し（この結果を入れた変数は「票の入れ物」）
_VOTE_MAKERS = ("vote_key", "vote_lineage", "_indep", "independent",
                "merge_joint")


def _vote_names_in(fn, src, seed=None) -> set:
    """★その関数の中で「票の入れ物」になっている名前★（2026-08-14・台帳#349）

    ★関数ごとに集める★＝ファイル全体で集めると、別の関数の同じ名前まで
      巻き込んで誤検知になる（Codexの指摘）。

    たどるのは4つ:
      ①`for … in votes.items()` の取り出し先
      ②`keys = vote_key(...)` のような代入
      ③`alias = keys` のような**別名**（何度でも伝わるまで繰り返す）
      ④`def f(keys)` の引数で、同じファイルの呼び出し側が票を渡している場合
    """
    import ast
    # ★引数から分かっている票も種にする★（2026-08-14・依頼202）
    #   `def enough(keys): alias = keys; len(alias) >= 2` を追えるように。
    names = set(seed or ())
    own = _own_nodes(fn)
    while True:                            # ★増えなくなるまで（固定点）★
        before = len(names)
        for node in own:
            it = getattr(node, "iter", None)
            if it is not None and isinstance(node, (ast.For, ast.comprehension)):
                if _looks_vote(it, src, names):
                    names |= _names_of(node.target)
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                val = getattr(node, "value", None)
                if val is None:
                    continue
                if _looks_vote(val, src, names):
                    tg = (node.targets if isinstance(node, ast.Assign)
                          else [node.target])
                    for t in tg:
                        names |= _names_of(t)
        if len(names) == before:
            return names


def _names_of(target) -> set:
    import ast
    return {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}


def _looks_vote(node, src, names) -> bool:
    """その式は票から来ているか（言葉・作り手・すでに分かっている名前）。"""
    import ast
    seg = ast.get_source_segment(src, node) or ""
    if any(w in seg for w in _VOTE_WORDS + _VOTE_MAKERS):
        return True
    base = node
    while isinstance(base, (ast.Subscript, ast.Attribute)):
        base = base.value
    return isinstance(base, ast.Name) and base.id in names


def _own_nodes(scope):
    """★その関数**だけ**のノード★（入れ子の関数の中には入らない）

    （2026-08-14・依頼201のP3）`ast.walk` は入れ子の関数まで歩くので、
    外側と内側で同じ名前を使うと由来が混ざる。
    `lambda` は式の一部なので中まで見る（`key=lambda n: -len(...)`）。
    """
    import ast
    # ★はじめの並びからも関数を外す★（2026-08-14・依頼204）
    #   子だけを外していたので、**モジュール直下に書いた関数の中身**が
    #   まるごとモジュールの走査に入り、全関数の名前が混ざっていた
    #   （＝別の関数の票が流れ込み、無関係な検査を誤って止める）。
    out = []
    stack = [n for n in (getattr(scope, "body", []) or [])
             if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    while stack:
        n = stack.pop()
        out.append(n)
        for ch in ast.iter_child_nodes(n):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue                   # ★中の関数は自分の名前で見る★
            stack.append(ch)
    return out


def _defs_in(scope) -> dict:
    """その入れ物**の直下**で定義されている関数（名前 → ノード）。

    ★同じ名前を2回定義したら、あとに書いたほうが勝つ★（依頼204）
      実行時と同じにしないと、実際に使われる定義を見逃す。
    """
    import ast
    out = {}
    for n in getattr(scope, "body", []) or []:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[n.name] = n                # 後勝ち（本文の順に見る）
    return out


def _shadowed(name: str, scope) -> bool:
    """その名前が、引数や代入で**関数以外**に束ねられているか（依頼204）。

    `def g(votes, enough): return enough(...)` の `enough` は
    モジュールの関数ではなく引数。ここを見ないと、無関係な関数に
    票の印が付いて**自動運用を誤って止める**。
    """
    import ast
    a = getattr(scope, "args", None)
    if a is not None:
        for x in (list(getattr(a, "posonlyargs", []) or []) + list(a.args)
                  + list(a.kwonlyargs)
                  + [y for y in (a.vararg, a.kwarg) if y]):
            if x.arg == name:
                return True
    for n in _own_nodes(scope):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return True
        if isinstance(n, (ast.AnnAssign, ast.AugAssign)):
            t = n.target
            if isinstance(t, ast.Name) and t.id == name:
                return True
    return False


def _resolve(name: str, owner, chain: dict, tree):
    """★呼び出し先は「いちばん近いところ」の定義を選ぶ★（依頼203）

    同じ名前の関数が別々の入れ物にあると、名前で引いただけでは
    無関係なほうにまで票の印が付く（誤って止める）。
    呼び出し位置から外側へ順にたどり、最初に見つかった定義を使う。
    """
    here = owner
    while here is not None:
        got = _defs_in(here).get(name)
        if got is not None:
            return got
        if here is not tree and _shadowed(name, here):
            return None                    # ★引数や変数で隠れている★
        here = chain.get(id(here))
    return _defs_in(tree).get(name)


def _scope_chain(tree) -> dict:
    """関数 → その関数を囲んでいる関数（外側が無ければ None）。"""
    import ast

    chain = {}

    def _walk(scope):
        for fn in _defs_in(scope).values():
            chain[id(fn)] = None if scope is tree else scope
            _walk(fn)

    _walk(tree)
    return chain


def _vote_params(tree, src) -> dict:
    """★票を渡されている引数★（関数のノード → 引数名の集合）

    `def enough(keys): len(keys) >= 2` は、それだけ見ると票の話か分からない。
    同じファイルの呼び出し側が `enough(independent(votes))` としていれば票である。

    ★何段でも伝える★（2026-08-14・依頼202〜203）
      `g → r1 → … → enough` と何段挟まっても届くよう、
      **増えなくなるまで**繰り返す（回数で打ち切らない）。
      名前も引数も有限で、集合は増える一方なので必ず止まる。
    ★キーワードで渡した場合も見る★（`enough(keys=keys)`）
    ★同じ名前の関数は「いちばん近いほう」を選ぶ★（別の入れ物のものを巻き込まない）
    """
    import ast
    funcs = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    chain = _scope_chain(tree)
    out = {}                               # id(関数) -> 引数名の集合
    while True:                            # ★増えなくなるまで（固定点）★
        before = sum(len(v) for v in out.values())
        base = {id(f): _vote_names_in(f, src, out.get(id(f))) for f in funcs}
        top = _vote_names_in(tree, src)
        for owner in funcs + [tree]:
            # ★関数の集合が空でも、モジュールの名前で埋めない★（依頼203）
            #   埋めると、別の関数で見つけた名前がここへ流れ込み、
            #   無関係な `len(keys) >= 2` を誤って止める。
            here = top if owner is tree else base.get(id(owner), set())
            for node in _own_nodes(owner):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)):
                    continue
                fn = _resolve(node.func.id, owner, chain, tree)
                if fn is None:
                    continue
                pos = [a.arg for a in
                       list(getattr(fn.args, "posonlyargs", []) or [])
                       + list(fn.args.args)]
                kwn = {a.arg for a in
                       list(fn.args.args) + list(fn.args.kwonlyargs)}
                for i, a in enumerate(node.args):
                    if i < len(pos) and _looks_vote(a, src, here):
                        out.setdefault(id(fn), set()).add(pos[i])
                for k in node.keywords:
                    if k.arg and k.arg in kwn \
                            and _looks_vote(k.value, src, here):
                        out.setdefault(id(fn), set()).add(k.arg)
        if sum(len(v) for v in out.values()) == before:
            return out


def _raw_vote_counts(src: str, fname: str) -> list:
    """★票を自前で数えている場所★を探す。

    ★なぜ要るか★（2026-08-14）
      共同制作の組をまとめる処理を入れたとき、**採用地点を1つ通し忘れた**。
      さらに直した翌日、cz_lookup にもう2か所残っていた（Codexが発見）。
      数える場所が散らばると、必ずまた繋ぎ忘れる。

    ★形と由来の両方を要る条件にする★（2026-08-14・台帳#349）
      形だけで見ると、文字列の長さ検査や試験の件数比較まで拾って
      **18件の誤検知**が出た（実測）。名前だけで見ると書き方の違いを見逃す。

      形 … len(x) と 2 の比較（左右どちらでも・等号でも）／
           -len(x)／key=len と reverse=True／x.sort(key=len, reverse=True)
      由来 … その x が票から来ているか（関数ごとにたどる）
    """
    import ast
    out = []
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [f"{fname}: 読めません（{e}）"]
    # ★自己試験の中は見ない★（試験は件数の確認を普通にする）
    for _fn in list(ast.walk(tree)):
        if isinstance(_fn, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and "selftest" in _fn.name:
            _fn.body = []
    params = _vote_params(tree, src)

    def _seg(node) -> str:
        return (ast.get_source_segment(src, node) or "")[:60]

    def _scan(scope, names):
        def _about(node) -> bool:
            return _looks_vote(node, src, names)

        def _is_len(node) -> bool:
            return (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "len" and bool(node.args)
                    and _about(node.args[0]))

        def _is_two(node) -> bool:
            return isinstance(node, ast.Constant) and node.value == 2

        for node in _own_nodes(scope):
            if isinstance(node, ast.Compare):
                sides = [node.left] + list(node.comparators)
                hit = any((_is_len(a) and _is_two(b))
                          or (_is_two(a) and _is_len(b))
                          for a, b in zip(sides, sides[1:]))
                if hit and all(isinstance(o, (ast.GtE, ast.Lt, ast.Gt, ast.LtE,
                                              ast.Eq, ast.NotEq))
                               for o in node.ops):
                    out.append(f"{fname}:{node.lineno} " + _seg(node))
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) \
                    and _is_len(node.operand):
                out.append(f"{fname}:{node.lineno} 並び替えの鍵に生の件数: "
                           + _seg(node))
            if isinstance(node, ast.Call):
                kw = {k.arg: k.value for k in node.keywords if k.arg}
                rev, key = kw.get("reverse"), kw.get("key")
                if not (isinstance(rev, ast.Constant) and rev.value is True
                        and key is not None
                        and "len" in (ast.get_source_segment(src, key) or "")):
                    continue
                # sorted(票, key=len, reverse=True) と 票.sort(key=len, …)
                tgt = None
                if node.args:
                    tgt = node.args[0]
                elif isinstance(node.func, ast.Attribute) \
                        and node.func.attr == "sort":
                    tgt = node.func.value
                if tgt is not None and _about(tgt):
                    out.append(f"{fname}:{node.lineno} 並び替えの鍵に生の件数: "
                               + _seg(node))

    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        _p = set(params.get(id(fn)) or ())
        _scan(fn, _vote_names_in(fn, src, _p) | _p)
    # 関数の外（モジュール直下）も見る
    top = ast.Module(body=[b for b in tree.body
                           if not isinstance(b, (ast.FunctionDef,
                                                 ast.AsyncFunctionDef))],
                     type_ignores=[])
    _scan(top, _vote_names_in(top, src))
    return sorted(set(out))


def _check_39_selftest() -> list:
    """★見張り39（票の数え方）自身の対照実験★＝失敗した項目を返す。

    ★2026-09-01：`selftest()` へ繋いだ★（Codexの指摘5・罠㉞）＝
      それまでは `check_39` の中でだけ動いていた。
      ★壊し方の道具は対象を `--selftest` で呼ぶ★ので、
      監査39を黙らせる壊し方を足しても**一度も捕まえられなかった**。
    """
    bad = []
    for _name, _code in _WATCHDOG_MUST_FIND.items():
        if not _raw_vote_counts(_code, "（見張りの試験）"):
            bad.append(f"見つけられない: {_name}")
    for _name, _code in _WATCHDOG_MUST_PASS.items():
        if _raw_vote_counts(_code, "（見張りの試験）"):
            bad.append(f"誤って止める: {_name}")
    return bad


def check_39_vote_counting(machines: list) -> list[str]:
    """★独立した票の数え方が、正本を通っているか★（2026-08-14）

    ★これは記事の正しさに直結する★＝「独立した2つの出典が一致したら採用」の
      2 を各所で自前に数えると、同じ会社の別サイトや共同制作の組を
      2票と数えてしまい、土台が崩れる。

    ★この検査自体が効いているかは selftest で見る★
      （直す前の2つの書き方を実際に見つけられることを確かめる）
    """
    ngs = []
    for fn in VOTE_MODULES:
        path = BASE / "scripts" / fn
        if not path.is_file():
            ngs.append(f"{fn}: ありません（VOTE_MODULES を直してください）")
            continue
        for hit in _raw_vote_counts(path.read_text(encoding="utf-8"), fn):
            ngs.append(hit + "／★source_lineage.independent() を通してください★")
    # ★見張りが壊れていないか、その場で確かめる★（直す前の姿を入れて試す）
    # ★直す前の書き方と、同じ意味の別の書き方★（2026-08-14・台帳#349）
    #   ここに並べた7つを全部見つけられなければ、見張りは働いていない。
    # ★見張り自身が働いているか、1つずつ確かめる★（2026-08-14・依頼200のP3）
    #   件数だけを見ると、1つを二重に数えて1つを見逃しても合格してしまう。
    #   ★見つけるべき形★と★見つけてはいけない形★の両方を並べる。
    for b in _check_39_selftest():
        ngs.append("★票の数え方の見張り★ " + b)
    return ngs


def check_41_automation_policy(machines: list) -> list[str]:
    """★自動で通信してよい先の名簿が、他の設定と食い違っていないか★

    （2026-08-16・台帳#376／Codex依頼214の助言）
    ★JSONを置くだけでは関所にならない★＝巡回先の設定・黒い名簿と
    毎回突き合わせる。ここが赤いまま自動タスクを動かすと、
    今回と同じ「規約を読まないまま毎晩アクセスする」形に戻る。
    """
    import automation_policy as _ap
    try:
        return list(_ap.disagreements())
    except Exception as e:                # noqa: BLE001
        return [f"通信の名簿を確かめられません: {str(e)[:150]}"]


def check_42_fetch_purpose(machines: list) -> list[str]:
    """★通信の入口が「用途を名乗る」囲みの中にあるか★（2026-08-17・依頼225）

    ★なぜ機械で見張るのか★
      2026-08-16 に「取りに行く前に用途を名乗る」を必須にしたとき、
      **呼び出し側を全部は直していなかった**。そのため
      collect_evidence / machine_sources / confirmed_values ほか計11箇所が
      「名乗っていません」で必ず落ちる状態になり、
      ★更新タスクは出典0件・新台タスクは確定値を書き戻せない★まま
      1日気づかれなかった。自己試験は全部通っていた（偽物の取得へ
      差し替えて試験するので、関所を踏まない）。

    ★同じ型の事故★＝関所を厳しくして、通る側の更新が漏れる。
      文章の約束では守られないので、ここで数える。

    見方は素朴でよい＝`_get(` を呼んでいる行より上に、
    同じか浅いインデントの `fetching(` の囲みがあるか。
    ★見つからなければ「疑い」として挙げる★（判断は人がする）。
    """
    import re as _re
    ngs: list[str] = []
    sdir = BASE / "scripts"
    # ★除外条件を作り込まない★（2026-08-17）
    #   最初の版は「試験の差し替えを外す」つもりで `= _nw._get` を除外に入れ、
    #   **捕まえたい `page = _nw._get(url)` まで一緒に外して**しまった
    #   （対照実験で発覚）。呼び出しかどうかは丸括弧の有無で決まるので、
    #   `_get(` と書いてある行だけを見れば足りる。差し替え（`_w._get = 偽物`）は
    #   丸括弧が続かないので、そもそもここに来ない。
    call = _re.compile(
        r"(?<![A-Za-z0-9_])(?:_w|_nw|_nwp|new_machine_watch)\._get\s*\(")
    for path in sorted(sdir.glob("*.py")):
        if path.name in ("new_machine_watch.py",):
            continue                      # 取得口そのもの
        lines = path.read_text(encoding="utf-8").split("\n")
        for i, line in enumerate(lines):
            # 説明文（#から後ろ）はコードではないので見ない
            if not call.search(line.split("#")[0]):
                continue
            indent = len(line) - len(line.lstrip())
            # 上へさかのぼって、囲んでいる `with ... fetching(` を探す
            ok = False
            for j in range(i - 1, max(-1, i - 40), -1):
                prev = lines[j]
                if not prev.strip():
                    continue
                pind = len(prev) - len(prev.lstrip())
                if pind >= indent:
                    continue
                if "fetching(" in prev:
                    ok = True
                    break
                if _re.match(r"\s*(def|class)\s", prev):
                    break                 # 関数の頭まで来たら囲みは無い
                indent = pind
            if not ok:
                ngs.append(
                    f"{path.name}:{i + 1} 用途を名乗らずに取りに行っています"
                    "（with new_machine_watch.fetching(\"用途\"): で囲む）")
    return ngs


def check_44_empty_settei_box(machines: list) -> list[str]:
    """★設定示唆まとめの箱が、中身なしで読者に出ていないか★（2026-08-21・台帳#150）

    ★なぜ★ `machine.html` は `type:"settei"` の箱を描くとき、
    見出しとバッジ凡例（弱/中/強/確）を**先に必ず出してから**表を並べる。
    中身が無いと「見出しと凡例だけ」が読者に残る。
    台帳#150 は「43/113機種がこの状態」と記録し、あわせて
    **「audit_site.py の全項目が検知できない」**とも書いていた。ここで塞ぐ。

    ★行が1つあることと、中身が描けることは別★（2026-08-21・Codex依頼245）
      `rows: [{"trigger":"","hint":""}]` は行1つだが、画面には空のセルが2つ出るだけ。
      数えるのは「実際に文字が出るセルを持つ行」。

    判定は `scripts/recheck.py` に任せる（★同じ規則を2か所に書かない★）。
    """
    sys.path.insert(0, str(BASE / "scripts"))
    try:
        import recheck as _rc
    except Exception as e:                                   # noqa: BLE001
        return [f"再検査の道具を読み込めません: {type(e).__name__}: {e}"]

    out = []
    for m in machines:
        slug = m.get("slug")
        if not slug:
            continue
        r = _rc.run("settei_filled", {"slug": slug})
        if r["result"] == _rc.FAIL:
            out.append(f"{slug}: {r['detail']}")
        elif r["result"] == _rc.ERROR:
            out.append(f"{slug}: 検査が失敗しました（{r['detail']}）")
    return out


# ★★監査51そのものの試験★★（2026-08-22・Codexの指摘を全部当てる）
#   ★なぜ要るか★＝最初の版は正規表現で書いたので `ng = sum(...)` を見逃し、
#   ★自分で作った見張りが、自分の3件目（maker_identity_cache）を見逃した★。
#   ＝**その見張りは「赤なのに緑」を1件、実際に通していた**（実証済み）。
#   Codexが「この形は見逃す」と挙げたものを、ここで全部当てる。
# ★止めなければならない形★
_TALLY_MUST_CATCH = {
    "sum(...)": "def selftest():\n    results = []\n    def t(n, c): results.append((n, c))\n    t('a', True)\n    ng = sum(1 for _, o in results if not o)\n    t('b', True)\n",
    "別の変数名": "def selftest():\n    results = []\n    def t(n, c): results.append((n, c))\n    t('a', True)\n    bad = [n for n, o in results if not o]\n    t('b', True)\n",
    "all(...)": "def selftest():\n    results = []\n    def t(n, c): results.append((n, c))\n    t('a', True)\n    ok_all = all(o for _, o in results)\n    t('b', True)\n",
    "複数行の式": "def selftest():\n    results = []\n    def t(n, c): results.append((n, c))\n    t('a', True)\n    ng = [n for n, o in results\n          if not o]\n    t('b', True)\n",
    "表示なし・英語": "def selftest():\n    results = []\n    def t(n, c): results.append((n, c))\n    t('a', True)\n    ng = len([1 for _, o in results if not o])\n    t('b', True)\n",
}

# ★止めてはいけない形★（行き過ぎの検知）
_TALLY_MUST_PASS = {
    "正しい形（数えるのは最後）": "def selftest():\n    results = []\n    def t(n, c): results.append((n, c))\n    t('a', True)\n    t('b', True)\n    ng = [n for n, o in results if not o]\n",
    "selftest でない関数": "def helper():\n    results = []\n    def t(n, c): results.append((n, c))\n    ng = [n for n, o in results if not o]\n    t('b', True)\n",
    "試験が1件も無い": "def selftest():\n    results = []\n    ng = [n for n, o in results if not o]\n",
}


def selftest_51() -> int:
    """監査51が、見逃すと言われた形を全部捕まえるか。"""
    bad = []
    for name, src in _TALLY_MUST_CATCH.items():
        got = bool(selftest_tally_gaps(src))
        print(("  OK   " if got else "  ★NG ") + f"止める: {name}")
        if not got:
            bad.append(name)
    for name, src in _TALLY_MUST_PASS.items():
        got = bool(selftest_tally_gaps(src))
        print(("  OK   " if not got else "  ★NG ") + f"止めない: {name}")
        if got:
            bad.append(name)
    n = len(_TALLY_MUST_CATCH) + len(_TALLY_MUST_PASS)
    print(f"{n - len(bad)}/{n} 合格")
    return 1 if bad else 0


_IN_52 = False


def check_52_test_residue(machines: list) -> list:
    """★試験用の偽の機種が残っていないか★（2026-08-24新設・自分で踏んだ）

    ★何が起きたか★
      障害注入の試験は**本番のファイルへ実際に書いてから元へ戻す**。
      その試験を強制終了したら巻き戻しが走らず、
      「再開確認機ZZZ」という偽の機種の記事データとページが残った。

    ★なぜ怖いか★
      残ったままだと、夜の公開の関所が
      「許していないファイルが混ざっている」と見なして
      **その晩の公開を丸ごと止める**。
      しかも**エラーではないので誰にも届かない**
      ＝2026-08-22に直した「静かに0件が続く」とまったく同じ型。

    ★掃除そのものは試験の側でやる★（始めと終わりの2回）。
      ここは**別の道で残った時に気づくため**の見張り。
    """
    import glob as _g
    ng = ["★見張り52自身が働いていません★: " + x
          for x in (_check_52_selftest() if not _IN_52 else [])]
    for pat, what in ((os.path.join(BASE, "machines", "zzz_*"), "ページ"),
                      (os.path.join(BASE, "assets", "data", "machine-details",
                                    "zzz_*.json"), "記事データ")):
        for x in _g.glob(pat):
            ng.append(f"試験用の残骸（{what}）が残っています: "
                      f"{os.path.relpath(x, BASE)}"
                      "／★このままだと夜の公開が丸ごと止まります★"
                      "／掃除＝python -c "
                      "\"import sys;sys.path.insert(0,'scripts');"
                      "import publish_new_machine as p;"
                      "p.purge_test_residue()\"")
    for m in machines:
        if str(m.get("slug") or "").startswith("zzz_"):
            ng.append(f"機種一覧に試験用の機種が残っています: {m.get('slug')}")
    # ★早見表にも入り込む★（2026-08-24・Codexの3回目の指摘3）
    #   ★機種一覧だけ見ても足りない★＝公開の関所は
    #   早見表の変更も「許していないファイル」として数える。
    for rel in ("guide-tenjo-ranking.html", "guide-reset-ranking.html",
                "guide-suru-tenjo.html", "guide-ichiran.html"):
        f = os.path.join(BASE, rel)
        if not os.path.isfile(f):
            continue
        if "zzz_" in open(f, encoding="utf-8", errors="replace").read():
            ng.append(f"早見表に試験用の機種が残っています: {rel}")
    # ★公開途中の目印★（残ると以後の新台追加が永久に止まる）
    for rel in (".publish-in-progress.json", ".push-pending.json"):
        f = os.path.join(BASE, rel)
        if not os.path.isfile(f):
            continue
        try:
            import json as _j
            slug = str((_j.load(open(f, encoding="utf-8")) or {}).get("slug")
                       or "")
        except Exception:                                    # noqa: BLE001
            continue
        if slug.startswith("zzz_"):
            ng.append(f"試験用の公開途中の目印が残っています: {rel}"
                      f"（{slug}）／★以後の新台追加が止まります★")
    return ng


def _check_52_selftest() -> list:
    """★見張り52が本当に働くかを、毎回いっしょに確かめる★（対照実験）

    ★なぜコードに置くか★（2026-08-24・Codexの3回目の指摘）
      手で1回やった対照実験は、コードのどこにも残らない。
      ★残らない確認は、次の変更で静かに壊れる★。
      項目51と同じやり方で、監査を回すたびに一緒に試す。

    ★本物のファイルは作らない★＝一時ディレクトリを BASE に見せかけて試す。
    """
    import tempfile as _tf
    global BASE
    bad = []
    real = BASE
    global _IN_52
    d = _tf.mkdtemp(prefix="audit52_")
    try:
        BASE = d
        _IN_52 = True
        if check_52_test_residue([]) != []:
            bad.append("何も無いのに鳴った")
        os.makedirs(os.path.join(d, "machines", "zzz_probe"))
        if not check_52_test_residue([]):
            bad.append("★偽の機種を置いても黙っていた★")
        if not check_52_test_residue([{"slug": "zzz_probe"}]):
            bad.append("★一覧の偽の機種を見つけられない★")
        with open(os.path.join(d, ".publish-in-progress.json"), "w",
                  encoding="utf-8") as fh:
            fh.write('{"slug": "zzz_probe"}')
        if not [x for x in check_52_test_residue([]) if "目印" in x]:
            bad.append("★試験用の目印を見つけられない★")
        with open(os.path.join(d, ".publish-in-progress.json"), "w",
                  encoding="utf-8") as fh:
            fh.write('{"slug": "hokuto"}')
        if [x for x in check_52_test_residue([]) if "目印" in x]:
            bad.append("★本物の公開途中まで残骸扱いした★")
    finally:
        BASE = real
        _IN_52 = False
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)
    return bad

_IN_53 = False


def _check_53_selftest() -> list:
    """★見張り53が本当に働くかを、毎回いっしょに確かめる★（対照実験）

    （2026-08-24・Codexの12回目＝「記録があるだけで永久に緑」を防ぐ）
    ★本物の名簿は触らない★＝作り物の名簿を渡して判定だけ見る。
    """
    import datetime as _dt
    bad = []
    yday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
    ok_ua = {"_no_user_area_why": "確かめた", "_checked_at": "2026-08-24",
             "_checked_by": "試験", "_recheck_by": "2999-01-01"}
    for name, ua, want in (
            ("決まりごとも記録も無い", {}, True),
            ("記録はあるが期限切れ", {**ok_ua, "_recheck_by": yday}, True),
            ("記録はあるが確かめた人が無い",
             {**ok_ua, "_checked_by": ""}, True),
            ("期限の形が違う", {**ok_ua, "_recheck_by": "2026/13/45"}, True),
            ("確かめた日の形が違う", {**ok_ua, "_checked_at": "きのう"}, True),
            ("そろっている", ok_ua, False),
            ("★掃除が理解できない決まりごと★",
             {"drop": [{"selector": "x"}]}, True),
            # ★必須箱はあるが、決まりごとの形が違う★
            #   （この形でないと、必須箱の検査に助けられて
            #     「id/classを見る」を外しても気づけない）
            ("★形だけ違う（必須箱はある）★",
             {"drop": [{"selector": "x"}],
              "require_before": [{"id": "x"}]}, True),
            ("★必須箱が無い★（相手が名前を変えたら素通り）",
             {"drop": [{"id": "comments"}]}, True),
            ("箱ごと落とせる（必須箱つき）",
             {"drop": [{"id": "comments"}],
              "require_before": [{"id": "comments"}]}, False)):
        got = bool(_judge_53_user_area(ua))
        if got != want:
            bad.append(f"★{name}★ → {'鳴った' if got else '黙った'}")
    return bad


def _judge_53_user_area(ua: dict) -> list:
    """1つの出典について、投稿欄の備えが足りているか（★判定はここだけ★）"""
    import datetime as _dt
    ng = []
    drops = [r for r in (ua.get("drop") or []) if isinstance(r, dict)]
    if ua.get("drop"):
        # ★★中身まで見る★★（2026-08-24・Codexの13回目）
        #   ★truthy なら即合格にしていた★ので、
        #   掃除の処理が理解できない形でも監査だけ緑になれた。
        #   掃除側が見るのは id と class（`user_area._match`）。
        if not drops:
            return ["投稿欄の決まりごとの形が違います（辞書の配列で書きます）"]
        bad = [r for r in drops if not (r.get("id") or r.get("class"))]
        if bad:
            return [f"投稿欄の決まりごとに id も class もありません: {bad[:2]}"
                    "／★掃除の処理が理解できない形です★"]
        # ★★落とせたことを確かめる備えがあるか★★（2026-08-24・Codexの14回目）
        #   ★id や class があるだけでは足りない★＝相手が名前を変えたら
        #   **0箱削除でも通ってしまう**。
        #   ★必須箱（require_before）★があれば、消える前に居たことを確かめられる。
        if not [r for r in (ua.get("require_before") or [])
                if isinstance(r, dict)]:
            return ["投稿欄の決まりごとに必須箱（require_before）がありません"
                    "／★相手が名前を変えると、0箱削除でも通ります★"]
        return ng                          # 箱ごと落とせる
    why = str(ua.get("_no_user_area_why") or "").strip()
    if not why:
        return ["投稿欄の決まりごとも「投稿欄が無いことを確かめた記録」も"
                "ありません／★読者の書き込みが根拠になり得ます★"]
    for k in ("_checked_at", "_checked_by", "_recheck_by"):
        if not str(ua.get(k) or "").strip():
            ng.append(f"投稿欄を確かめた記録に {k} がありません")
    # ★確かめた日も日付として読めること★（2026-08-24・Codexの13回目）
    #   ★空でないことしか見ていなかった★ので、でたらめな日付でも通った。
    ca = str(ua.get("_checked_at") or "").strip()
    if ca:
        try:
            _dt.date.fromisoformat(ca)
        except Exception:                                    # noqa: BLE001
            ng.append(f"確かめた日の形が違います（{ca!r}）")
    rb = str(ua.get("_recheck_by") or "").strip()
    if rb:
        try:
            due = _dt.date.fromisoformat(rb)
        except Exception:                                    # noqa: BLE001
            ng.append(f"再確認の期限の形が違います（{rb!r}）")
        else:
            if due < _dt.date.today():
                ng.append(f"投稿欄の確認が期限切れです（{rb}）"
                          "／★実ページを見て確かめ直してください★")
    return ng

def check_53_source_user_area(machines: list) -> list:
    """★出典に使う先の投稿欄★（2026-08-24新設・Codexの11回目）

    ★何が危ないか★＝投稿欄の決まりごとを登録していないサイトは、
    読者の書き込みが本文と一緒に読める。
    ★同じ誤った値が2サイトの投稿欄にあり、2AIがそれを逐語引用に選ぶと★、
    独立2出典として記録できてしまう。

    ★見るのは「通信してよい先」だけ★＝名簿に載っていないサイトは
    そもそも取りに行けないので出典にできない（実測で確認）。

    ★どちらかが要る★
      ・投稿欄の決まりごとが登録されている（箱ごと落とせる）
      ・「投稿欄が無いことを確かめた」記録がある（`_no_user_area_why`）
    ★「いま無かった」は永久の保証ではない★ので、記録には確かめた日を書く。
    """
    import json as _j
    global _IN_53
    ng = ["★見張り53自身が働いていません★: " + x
          for x in (_check_53_selftest() if not _IN_53 else [])]
    _IN_53 = True
    try:
        pol = _j.load(open(os.path.join(BASE, "assets", "data",
                                        "automation-policy.json"),
                           encoding="utf-8"))
        cat = _j.load(open(os.path.join(BASE, "assets", "data",
                                        "directory-catalogs.json"),
                           encoding="utf-8"))
    except Exception as e:                                   # noqa: BLE001
        _IN_53 = False
        return ng + [f"名簿を読めません: {str(e)[:60]}"]
    finally:
        _IN_53 = False
    hosts = pol.get("hosts") or {}
    # ★名鑑のURLの形からホスト名を引く★（名鑑は host を直接持っていない）
    import urllib.parse as _up
    ua_by_host = {}
    for d in (cat.get("directories") or {}).values():
        if not isinstance(d, dict):
            continue
        got = set()
        for x in (d.get("surfaces") or []):
            h = _up.urlsplit(str((x or {}).get("url") or "")).hostname or ""
            if h:
                got.add(h.lower())
        for h in got:
            ua_by_host[h] = d.get("user_area") or {}
    for host, v in hosts.items():
        if not isinstance(v, dict):
            continue
        if v.get("status") != "APPROVED":
            continue
        # ★用途に「材料を読む」が入っている先だけ★（発見や同定だけなら関係ない）
        if "claim_material" not in (v.get("purpose") or []):
            continue
        # ★www の有無はそろえて引く★（名簿は別名も持つが、名鑑は素のホスト）
        _h = str(host).lower()
        ua = (ua_by_host.get(_h)
              or ua_by_host.get(_h[4:] if _h.startswith("www.") else "www." + _h)
              or {})
        # ★★記録があるだけでは通さない★★（2026-08-24・Codexの12回目）
        #   ★「いま無い」は永久の保証ではない★＝
        #   サイト側が普通の改修で投稿欄を足した時点で、経路が開く。
        ng += [f"{host}: {x}" for x in _judge_53_user_area(ua)]
    return ng

def check_51_selftest_tally(machines: list) -> list:
    # ★見張り自身が働いているかを、毎回いっしょに確かめる★（2026-08-22）
    #   ★理由★＝最初の版は正規表現で書いたので sum(...) を見逃し、
    #   ★見張りが「赤なのに緑」を1件、実際に通していた★。
    #   見張りの試験を別コマンドにすると、走らせ忘れて同じことが起きる。
    """★試験の数え方が早すぎないか★（2026-08-22新設・自分で2度踏んだ）

    ★何が起きたか★＝失敗を数える行が、試験より**手前**にあると、
    あとの試験が❌でも

        84/84 合格   （終了コード 0）

    と出る。＝★試験が落ちても緑に見える★＝いちばん危ない壊れ方。

    ★実際に2本で起きていた★
      pending_machines … 4件が数えられていなかった
      gates            … 6件
      maker_identity_cache … 11件（★台帳#454の直しを守る試験そのもの★。
        わざと壊すと❌が6件出るのに「84/84 合格」で通った＝実証済み）

    ★★正規表現をやめてASTで見る★★（2026-08-22・Codexの指摘）
      ★最初の版は正規表現で書いたので、`ng = sum(...)` の形を見逃した★
      （変数名・内包表記かどうか・「合格」の字の有無に依存していた）。
      ＝**自分で作った見張りが、自分の3件目を見逃した**。
      CLAUDE.md の監査43と同じ結論＝★字面でなく構文で数える★。

    ★見るもの★＝selftest らしき関数ごとに
      ①`results`（試験の記録）を**まとめて読む**式が現れる位置
      ②そのあとに `t(...)` などの**試験の呼び出し**が残っていないか
    変数名も表示の文言も見ない。
    """
    # ★★見張り自身が働いているかを、毎回いっしょに確かめる★★（2026-08-22）
    #   ★理由★＝最初の版は正規表現で書いたので sum(...) を見逃し、
    #   ★見張りが「赤なのに緑」を1件、実際に通していた★。
    #   見張りの試験を別コマンドにすると走らせ忘れるので、ここで必ず通す。
    ng = []
    import io as _io_51, contextlib as _cl_51
    _buf = _io_51.StringIO()
    with _cl_51.redirect_stdout(_buf):
        _self_ng = selftest_51()
    if _self_ng:
        ng.append("★この見張り自身の試験が落ちています★: "
                  + " / ".join(x.strip() for x in _buf.getvalue().split(chr(10))
                                if x.strip().startswith("★NG")))
    for f in sorted((BASE / "scripts").glob("*.py")):
        for fn_name, line, after in selftest_tally_gaps(load_text(f)):
            ng.append(f"{f.name}: {fn_name} が {line}行目で試験の記録を"
                      f"まとめて読んだあと、試験が {after} 件あります"
                      f"（★その分は数えられず、落ちても合格と出ます★）")
    return ng


def selftest_tally_gaps(src: str) -> list:
    """★数え上げより後ろに残っている試験を返す★（文字列からも試せる形）

    返り値: [(関数名, 数え上げの行, 残っている試験の数), …]
    """
    import ast as _ast
    try:
        tree = _ast.parse(src)
    except SyntaxError:
        return []
    out = []
    for fn in _ast.walk(tree):
        if not isinstance(fn, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        if "selftest" not in fn.name:
            continue
        tally = []
        for st in _ast.walk(fn):
            if not isinstance(st, _ast.Assign):
                continue
            # ★results をまとめて読む代入★（＝数え上げ）
            #   `results.append(...)` のような「足す側」は代入ではないので入らない
            if any(isinstance(n, _ast.Name) and n.id == "results"
                   for n in _ast.walk(st.value)):
                tally.append(st.lineno)
        if not tally:
            continue
        first = min(tally)
        after = 0
        for st in _ast.walk(fn):
            if not isinstance(st, _ast.Call):
                continue
            if getattr(st, "lineno", 0) <= first:
                continue
            nm = getattr(st.func, "id", "") or getattr(st.func, "attr", "")
            if nm == "t":
                after += 1
        if after:
            out.append((fn.name, first, after))
    return out


def check_50_contract_closure(machines: list) -> list[str]:
    """契約が「取り込んでいる相手」まで閉じているか

    ★なぜ見張るか（2026-08-21・台帳#420）★
      承認の関所は「この57枚の書類に判子を押した」と数えているが、
      ★その書類が中で呼んでいる別のファイルは数に入っていなかった★。
      ＝呼ばれる側を書き換えれば、承認をやり直さずに中身を変えられた。

      実測（2026-08-21）＝どちらの契約にも入っていない直接依存が **6本**
      （build_new_article / page_decision / publish_new_machine /
        prepush_gate / check_duplicate / pending_machines）。
      いずれも新台を記事にして公開するまでの一式で、
      ★書き換えれば公開物が変わる★ものだった。

    ★見るのは「どちらの契約にも入っていない」ものだけ★
      材料の契約か公開物の契約のどちらかに入っていればよい。
      ＝役割の違うものを無理に片方へ寄せない。

    直し方＝どちらかの集合へ足して、その契約を承認し直す
      材料の採否を決める側     → scripts/material_contract.py --approve
      公開物を作る側           → scripts/build_pages_artifact.py --approve
    """
    ng = []
    try:
        import json as _j
        sys.path.insert(0, os.path.join(BASE, "scripts"))
        import build_pages_artifact as _bpa
        ap = os.path.join(BASE, "assets", "data",
                          "material-contract-approval.json")
        with open(ap, encoding="utf-8") as f:
            a = _j.load(f)
        mat = set(a.get("files") or {})
        pub = set(_bpa.APPROVED_INPUTS)
        deps = set()
        for _k, v in (a.get("imports") or {}).items():
            for x in (v or []):
                deps.add("scripts/" + str(x) + ".py")
        for d in sorted(deps):
            if d in mat or d in pub:
                continue
            if not os.path.isfile(os.path.join(BASE, d)):
                continue          # 手元に無いものは対象外
            ng.append(
                f"{d}: 契約に入っている側が取り込んでいるのに、"
                "どちらの契約にも入っていません（台帳#420）")
    except Exception as e:            # noqa: BLE001
        ng.append(f"契約の閉じ方を調べられません（{type(e).__name__}: {e}）")
    return ng


def check_49_equivalence_label(machines: list) -> list[str]:
    """「等価＝5.6枚」と書いていないか（サイト内ガイドと矛盾する）

    ★なぜ見張るか（2026-08-21・台帳#219）★
      サイト内ガイド `guide-rate.html` の定義は
        等価（5.0枚）… 借りた時と同じ価値で換金できる最も有利な条件
        5.6枚      … 当サイトが基準としている交換率
      ＝★「等価」と「5.6枚」は別のもの★。

      ところが記事側に「等価（5.6枚等価）」「5.6枚等価」という書き方が
      4機種8箇所あった（biohazard / bofuri / neoplanet / tensura）。
      ★ガイドを読んだ人が記事を読むと、話が食い違う★。

    ★数値の話ではない★＝呼び方だけの問題なので、
      見つけたら「5.6枚」へ言い換える（数値は触らない）。
    """
    import glob
    ng = []
    det = os.path.join(BASE, "assets", "data", "machine-details")
    for path in sorted(glob.glob(os.path.join(det, "*.json"))):
        slug = os.path.basename(path)[:-5]
        try:
            with open(path, encoding="utf-8") as f:
                txt = f.read()
        except OSError:
            continue
        n = txt.count("5.6枚等価")
        if n:
            ng.append(
                f"{slug}: 「5.6枚等価」という書き方が {n} 箇所あります"
                "（ガイドの定義は 等価=5.0枚 / 5.6枚 は別物）。"
                "「5.6枚」へ言い換えてください")
    return ng


def check_48_ledger_argv(machines: list) -> list[str]:
    """台帳CLIのオプション名を、あちこちで並べていないか

    ★なぜ見張るか（2026-08-21・台帳#312）★
      コード側が「--source」「--slug」…と**自分で並べて**台帳CLIを
      別プロセスで起動している箇所が3つあった。
      ★CLIの引数を増減させると、3つとも黙って失敗しうる★
      （台帳#300とまったく同じ型＝オプション名への依存が各所に散る）。

    ★並べてよいのは2か所だけ★
      ・open_issues.add_argv … 引数列を作る唯一の場所
      ・open_issues の argparse 定義 … CLIそのもの

    ★★これは主防御ではない（補助の見張り）★★（2026-08-21・Codexの再指摘）
      字面で探しているので、次のような書き方は拾えない:
        ・'add' や '--source' のように単引用符で書く
        ・cmd = ["add"] のあとから extend() する
        ・オプション名を定数や変数に入れる
        ・400字より離れたところで組み立てる
      ★本当の守りは「書きようがない形にする」こと★＝
        ・同じプロセスでよければ `open_issues.add_issue()`
        ・別プロセスが要るなら `open_issues.run_add()`
          （引数列を作るのも起動するのも、その中だけ）
      3系統（add_machine_run / codex_audit / machine_sources）は
      2026-08-21に run_add() へ寄せた。
      ここは「うっかり戻した」を早めに見つけるための網。
    """
    import glob
    ng = []
    for path in sorted(glob.glob(os.path.join(BASE, "scripts", "*.py"))):
        rel = "scripts/" + os.path.basename(path)
        try:
            with open(path, encoding="utf-8") as f:
                src = f.read()
        except OSError:
            continue
        if rel == "scripts/open_issues.py":
            continue          # ★ここが唯一の置き場（add_argv と argparse 定義）★
        if rel == "scripts/audit_site.py":
            continue          # この検査自身
        # ★★「台帳CLIを起動している並び」だけを見る★★
        #   ★自分のCLIを定義しているだけの add_argument("--source"…) は別物★
        #   （confirmed_values を誤って挙げたので絞った）。
        #   台帳へ登録する呼び出しは、必ず "add" のあとに
        #   --source と --slug と --kind が並ぶ。
        for m in re.finditer(r'"add"\s*,', src):
            seg = src[m.start():m.start() + 400]
            if '"--source"' in seg and '"--slug"' in seg and '"--kind"' in seg:
                ng.append(
                    f"{rel}: 台帳CLIのオプション名を自分で並べています"
                    "（open_issues.add_argv を使ってください・台帳#312）")
                break
    return ng


def check_47_model_code_in_html(machines: list) -> list[str]:
    """公開ページに型式名が焼き込まれたまま残っていないか

    ★なぜ見張るか（2026-08-21・台帳#434）★
      CLAUDE.md の決定＝「★型式名は記事には書かない。取り違えを防ぐ
      同定にだけ使う★」。ところが garei_zero_re は、記事データからは
      消したのに**公開HTMLには残り続けていた**。
      ＝★記事データを直しても、読者に見えるページには届かない★
      （HTMLは生成物なので、描き直さないと変わらない）。

    ★見るのは「記事データに無いのにHTMLにある」ときだけ★
      記事データにも残っているなら、それは別の話（データ側の違反）で、
      そちらは公開前の検査が見ている。ここは**届いていない**ことを見る。

    ★記事データそのものも見る★（2026-08-21に追加）
      2026-08-21に全機種を調べたら、★9機種の記事に型式名が出ていた★
      （tonsuki / toaru_index2 / sf6 / jashinchan / super_binmusume /
        world_dai_star / yajikita_mairu / galfy / yabachiba）。
      HTMLだけ見ていると、記事データに書いた時点では気づけない。

    直し方:
      記事データ  python scripts/strip_model_code.py --apply
      新台経路    python scripts/build_machine_pages.py --rebuild-auto <slug>
      旧形式      python scripts/build_machine_pages.py --legacy --slug <slug>
    """
    ng = []
    # ★記事データに型式名の見出しが残っていないか★
    try:
        import strip_model_code as _smc
        for m in machines:
            slug = m.get("slug")
            if not slug:
                continue
            dp = os.path.join(BASE, "assets", "data",
                              "machine-details", f"{slug}.json")
            if not os.path.isfile(dp):
                continue
            try:
                d = load_json(dp)
            except Exception:
                continue
            plan = _smc.plan_for(d)
            n = len(plan["drop_body"]) + len(plan["drop_fact"])
            if n:
                ng.append(
                    f"{slug}: 記事データに型式名の行が {n} 件あります"
                    "（python scripts/strip_model_code.py --apply で消せます）")
            for _si, _bi, line in plan["mixed"]:
                ng.append(
                    f"{slug}: 型式名が他の情報と同じ行にあります: {line[:50]!r}"
                    "（行ごと消すと他の情報も消えるので、人が決めてください）")
    except Exception as e:            # noqa: BLE001
        ng.append(f"記事データの型式名を調べられません（{type(e).__name__}: {e}）")

    for m in machines:
        slug = m.get("slug")
        if not slug:
            continue
        ident = m.get("identity") or {}
        codes = [str(ident.get("regulatory_model_code") or ""),
                 str(ident.get("observed_model_code") or "")]
        codes = [c for c in codes if len(c) >= 4]
        if not codes:
            continue
        page = os.path.join(BASE, "machines", slug, "index.html")
        if not os.path.isfile(page):
            continue
        try:
            with open(page, encoding="utf-8") as f:
                html = f.read()
        except OSError:
            continue
        detail_path = os.path.join(BASE, "assets", "data",
                                   "machine-details", f"{slug}.json")
        detail_text = ""
        if os.path.isfile(detail_path):
            try:
                with open(detail_path, encoding="utf-8") as f:
                    detail_text = f.read()
            except OSError:
                pass
        for c in codes:
            if c in html and c not in detail_text:
                ng.append(
                    f"{slug}: 公開ページに型式名 {c!r} が残っています"
                    "（記事データからは消えているので、ページを描き直してください）")
                break
    return ng


def check_46_pochipochi_reachable(machines: list) -> list[str]:
    """★ポチポチくんの案内が出るのに、飛び先が準備中になっていないか★
    （2026-08-21・台帳#252）

    ★なぜ★ 記事ページに「小役カウンター ポチポチくん →」が**有効なリンク**として
    出るのに、飛んだ先が「準備中」になる＝★読者が空振りする★。
    ★新台が増えるたびに増える構造★＝新しく足した機種はどのリストにも入らない。
    実際、2026-08-07に15件だったものが2026-08-21には24件になっていた。

    判定は `scripts/recheck.py` に任せる（★同じ規則を2か所に書かない★）。
    """
    sys.path.insert(0, str(BASE / "scripts"))
    try:
        import recheck as _rc
    except Exception as e:                                   # noqa: BLE001
        return [f"再検査の道具を読み込めません: {type(e).__name__}: {e}"]

    out = []
    for m in machines:
        slug = m.get("slug")
        if not slug:
            continue
        r = _rc.run("pochipochi_reachable", {"slug": slug})
        if r["result"] == _rc.FAIL:
            out.append(f"{slug}: {r['detail']}")
        elif r["result"] == _rc.ERROR:
            out.append(f"{slug}: 検査が失敗しました（{r['detail']}）")
    return out


def check_45_rumor_declared_empty(machines: list) -> list[str]:
    """★噂の箱に「噂はありません」と書いたまま出していないか★（2026-08-21・台帳#334）

    ★なぜ★ 運営者の決定（2026-08-12・CLAUDE.md）＝
    「rumor は中身ができてから出す。空の箱は『あるのに載せていない』と読める」。
    実際には56機種が、黄色い枠と見出しを描いたうえで
    「現時点で目立った噂・未確定情報はありません」と書いて公開していた。

    ★ここで見るのはサイト自身が書いた定型文だけ★＝他所の日本語を読み解くのではなく、
    **うちの生成物が自分で「無い」と宣言している**ことを見つける。
    中身が有るか無いかの判断はしない（それは2AIの仕事）。

    判定は `scripts/recheck.py` に任せる（★同じ規則を2か所に書かない★）。
    """
    sys.path.insert(0, str(BASE / "scripts"))
    try:
        import recheck as _rc
    except Exception as e:                                   # noqa: BLE001
        return [f"再検査の道具を読み込めません: {type(e).__name__}: {e}"]

    out = []
    for m in machines:
        slug = m.get("slug")
        if not slug:
            continue
        r = _rc.run("rumor_not_declared_empty", {"slug": slug})
        if r["result"] == _rc.FAIL:
            out.append(f"{slug}: {r['detail']}")
        elif r["result"] == _rc.ERROR:
            out.append(f"{slug}: 検査が失敗しました（{r['detail']}）")
    return out


def check_43_undefined_names(machines: list) -> list[str]:
    """★消したはずの名前を呼び続けていないか★（2026-08-17・依頼225）

    ★なぜ★ 台帳#377 で `_ensure_list` の**定義だけ**を消して
    呼び出しが残り、grow_machine が毎朝 NameError で落ちていた。
    実行するまで分からない一方、名前を探すだけなら実行せずに分かる。
    ★自己試験では見つからなかった★（その経路を踏む試験が無かった）。

    決まりごと＝**そのファイルのどこにも束ねられていない名前を呼んでいたら挙げる**。

    ★正規表現では数えない★（2026-08-17・最初に書いた版は7件すべて誤検知だった＝
      `from X import _num` / タプル代入 / lambda の既定引数 / 文字列の中の字面）。
      ここは字面ではなく**構文**の話なので、Python自身に読ませる。
      束ね方（import・代入・for・with as・except as・引数・内包表記）を
      一つずつ場合分けするのではなく、`ast` が Store と呼ぶものを全部拾う。
    """
    import ast as _ast
    import builtins as _bi
    ngs: list[str] = []
    for path in sorted((BASE / "scripts").glob("*.py")):
        src = path.read_text(encoding="utf-8")
        try:
            tree = _ast.parse(src, filename=path.name)
        except SyntaxError as e:
            ngs.append(f"{path.name}:{e.lineno} 文として読めません（{e.msg}）")
            continue
        bound = set(dir(_bi))
        for n in _ast.walk(tree):
            if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef,
                              _ast.ClassDef)):
                bound.add(n.name)
            elif isinstance(n, _ast.Name) and isinstance(n.ctx, _ast.Store):
                bound.add(n.id)
            elif isinstance(n, _ast.alias):
                bound.add((n.asname or n.name).split(".")[0])
            elif isinstance(n, _ast.ExceptHandler) and n.name:
                bound.add(n.name)
            elif isinstance(n, _ast.arg):
                bound.add(n.arg)
            elif isinstance(n, (_ast.Global, _ast.Nonlocal)):
                bound.update(n.names)
        for n in _ast.walk(tree):
            if (isinstance(n, _ast.Call) and isinstance(n.func, _ast.Name)
                    and n.func.id not in bound):
                ngs.append(f"{path.name}:{n.lineno} "
                           f"定義の無い {n.func.id}() を呼んでいます")
    return ngs


def check_40_slug_binding(machines: list) -> list[str]:
    """★slugと機種ページURLの対応★（2026-08-16・台帳#376／Codex依頼212）

    ★なぜ監査するのか★
      「slugは機種ページのURLから作る」という決まりが、
      **別機種の記事を別機種のURLで公開する事故**を止めている。
      規約でP-WORLDからDMMへ移した公開済み7機種だけは、読者のリンクを
      切らないため `pw_*` のまま公開し続ける。その例外を
      **増やせない対応表**（scripts/slug_binding.py）に閉じ込めてあるので、
      表が壊れていないか・全機種がその二択に収まっているかを毎回見る。
    """
    import publish_new_machine as _pn
    import slug_binding as _sb
    ngs = list(_sb.audit_against_site())
    for m in machines:
        ident = m.get("identity") or {}
        # ★identity に知らない項目が混ざっていないか★（2026-08-16・依頼213）
        #   公開の関所（_IDENTITY_KEYS）は新台の経路しか通らないので、
        #   **既にある機種の identity は誰も見ていなかった**。
        #   ここで全機種を毎回見る＝移行や手直しで増えた項目に気づける。
        extra = sorted(set(ident) - _pn._IDENTITY_KEYS)
        if extra:
            ngs.append(f"{m.get('slug')}: identity に許可されていない項目が"
                       f"あります: {extra[:5]}"
                       "／★publish_new_machine._IDENTITY_KEYS に足すか、"
                       "書かないようにしてください★")
        url = str(ident.get("official_product_url") or "").strip()
        if not url:
            continue                     # identity未登録の機種は他の項目が見る
        ok, why = _sb.check(m.get("slug", ""), url)
        if not ok:
            ngs.append(f"{m.get('slug')}: {why}")
    return ngs


# ★見張りが必ず見つけるべき形★（直す前の実物＋同じ意味の別の書き方）
#   2026-08-14・依頼194〜200。★実際に本番コードにあった形から作った★
_WATCHDOG_MUST_FIND = {
    "出典の数で決める": "def f(per):\n"
    "    for nk, e in per.items():\n"
    "        if len(e['sources']) < 2:\n"
    "            pass\n",
    "多数決（マイナスの件数）": "def f(e):\n"
    "    return sorted(e['names'], key=lambda n: (-len(e['sources'][n]), n))\n",
    "内包表記から取り出した票": "def f(votes):\n"
    "    return [(fp, s) for fp, s in votes.items() if len(s) >= 2]\n",
    "ホストの数で決める": "def f(codes):\n"
    "    for code, hosts in codes.items():\n"
    "        if len(hosts) >= 2:\n"
    "            pass\n",
    "左右が逆": "def f(pubs):\n"
    "    keys = {vote_key(p) for p in pubs}\n"
    "    return 2 <= len(keys)\n",
    "等号": "def f(pubs):\n"
    "    keys = {vote_key(p) for p in pubs}\n"
    "    return len(keys) == 2\n",
    "並び替えの鍵": "def f(pubs):\n"
    "    keys = {vote_key(p) for p in pubs}\n"
    "    return sorted(keys, key=len, reverse=True)\n",
    "別名に入れ替えた": "def f(votes):\n"
    "    keys = independent(votes)\n"
    "    alias = keys\n"
    "    return len(alias) >= 2\n",
    "別の関数の引数として渡した": "def enough(keys):\n"
    "    return len(keys) >= 2\n"
    "def g(votes):\n"
    "    return enough(independent(votes))\n",
    "別の関数の戻り値から受けた": "def make_keys(votes):\n"
    "    return independent(votes)\n"
    "def g(votes):\n"
    "    keys = make_keys(votes)\n"
    "    return len(keys) >= 2\n",
    "その場で並べ替えた": "def f(votes):\n"
    "    keys = independent(votes)\n"
    "    keys.sort(key=len, reverse=True)\n",
    # ★2026-08-14・依頼201でCodexが挙げた見逃し例★
    "別名を挟んで引数に渡した": "def enough(keys):\n"
    "    return len(keys) >= 2\n"
    "def g(votes):\n"
    "    keys = independent(votes)\n"
    "    return enough(keys)\n",
    "何段も挟んで渡した": "def enough(keys):\n"
    "    return len(keys) >= 2\n"
    "def relay(x):\n"
    "    return enough(x)\n"
    "def g(votes):\n"
    "    keys = independent(votes)\n"
    "    return relay(keys)\n",
    "キーワードで渡した": "def enough(keys):\n"
    "    return len(keys) >= 2\n"
    "def g(votes):\n"
    "    return enough(keys=independent(votes))\n",
    "引数を別名に入れ替えた": "def enough(keys):\n"
    "    alias = keys\n"
    "    return len(alias) >= 2\n"
    "def g(votes):\n"
    "    return enough(independent(votes))\n",
    "7段挟んで渡した": "def enough(keys):\n"
    "    return len(keys) >= 2\n"
    "def r1(x):\n    return enough(x)\n"
    "def r2(x):\n    return r1(x)\n"
    "def r3(x):\n    return r2(x)\n"
    "def r4(x):\n    return r3(x)\n"
    "def r5(x):\n    return r4(x)\n"
    "def r6(x):\n    return r5(x)\n"
    "def g(votes):\n"
    "    keys = independent(votes)\n"
    "    return r6(keys)\n",
    "別名を5回つないだ": "def f(votes):\n"
    "    a1 = independent(votes)\n"
    "    a2 = a1\n    a3 = a2\n    a4 = a3\n    a5 = a4\n"
    "    return len(a5) >= 2\n",
    "同じ名前を2回定義（あとが本物）": "def enough(text):\n"
    "    return text\n"
    "def enough(keys):\n"
    "    return len(keys) >= 2\n"
    "def g(votes):\n"
    "    return enough(independent(votes))\n",
    "入れ子の関数の外側": "def outer(votes):\n"
    "    keys = independent(votes)\n"
    "    def inner(rows):\n"
    "        return rows\n"
    "    return len(keys) >= 2\n",
}

# ★見張りが止めてはいけない形★（行き過ぎの検知）
_WATCHDOG_MUST_PASS = {
    "文字列の長さ": "def f(c):\n    return len(c) >= 2\n",
    "自己試験の中の件数確認": "def selftest():\n"
    "    return len(r['adopted'][0]['sources']) == 2\n",
    "別の関数の同じ名前": "def a(votes):\n"
    "    keys = independent(votes)\n"
    "    return keys\n"
    "def b(keys):\n"
    "    return len(keys) >= 2\n",
    "別の関数から名前が流れ込まない": "def a2(votes):\n"
    "    keys = independent(votes)\n"
    "    return keys\n"
    "def b2(keys):\n"
    "    return len(keys) >= 2\n",
    "同じ名前の入れ子の関数": "def outer(votes):\n"
    "    def helper(keys):\n"
    "        return independent(keys)\n"
    "    return helper(independent(votes))\n"
    "def other():\n"
    "    def helper(text):\n"
    "        return len(text) >= 2\n"
    "    return helper\n",
    "asyncの自己試験": "async def selftest_vote():\n"
    "    return len(sources) == 2\n",
    # ★2026-08-14・依頼204でCodexが挙げた3つ★
    "別の関数を経由しても名前が流れ込まない": "def enough(items):\n"
    "    return len(items) >= 2\n"
    "def a3(votes):\n"
    "    keys = independent(votes)\n"
    "    return keys\n"
    "def b3(keys):\n"
    "    return enough(keys)\n",
    "引数で同じ名前が隠れている": "def enough(keys):\n"
    "    return len(keys) >= 2\n"
    "def g(votes, enough):\n"
    "    return enough(independent(votes))\n",
    # ★入れ子の関数の中の同じ名前は、外の票と混ぜない★（依頼201のP3）
    "入れ子の関数の中の同じ名前": "def outer(votes):\n"
    "    keys = independent(votes)\n"
    "    return keys\n"
    "def other():\n"
    "    def inner(keys):\n"
    "        return len(keys) >= 2\n"
    "    return inner\n",
}


def check_38_home_path_leak(machines: list) -> list[str]:
    """★公開されるファイルに、このパソコンのログイン名が出ていないか★

    ★なぜ要るか★（2026-08-14・運営者の指示）
      このリポジトリは公開されている。にもかかわらず、スクリプトに
      利用者フォルダの絶対パスを直書きしていたため、運営者の本名
      （Windowsのログイン名）とフォルダ構成が誰にでも読める状態だった
      （25ファイル・52か所）。置き場は local_paths.py に集めた。

    ★うっかり書き戻したらここで止まる★
      置き場が要るときは local_paths（_lp.doc / _lp.claude / _lp.DOCS …）を使う。
    """
    import subprocess
    me = os.path.basename(os.path.expanduser("~"))
    if not me:
        return []
    try:
        files = subprocess.run(["git", "ls-files"], cwd=BASE,
                               capture_output=True, encoding="utf-8",
                               errors="replace").stdout.split()
    except Exception as e:                # noqa: BLE001
        return [f"ファイル一覧を取れません: {e}"]
    ng = []
    for rel in files:
        p = os.path.join(BASE, rel)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                body = f.read()
        except Exception:                 # noqa: BLE001
            continue
        n = body.count(me)
        if n:
            ng.append(f"{rel}: ログイン名が{n}か所（local_paths を使ってください）")
        # ★置き場を使うと書いたのに、読み込んでいないファイルを見つける★
        #   （2026-08-14）docstringの中に読み込む行が入ってしまい、
        #   **構文は通るのに実行時に落ちる**という壊れ方が実際に2件あった。
        if rel.endswith(".py") and "_lp." in body                 and not rel.endswith("local_paths.py"):
            import ast
            try:
                _tree = ast.parse(body)
            except SyntaxError:
                ng.append(f"{rel}: 構文が壊れています")
                continue
            if not any(isinstance(_n, ast.Import)
                       and any(_a.name == "local_paths" for _a in _n.names)
                       for _n in ast.walk(_tree)):
                ng.append(f"{rel}: local_paths を使っているのに"
                          f"読み込んでいません（説明文の中に入っていませんか）")
    return sorted(ng)


# ─────────────────────────────────────────────────────────────
# ★★54_どこから採ったかの言い回し★★（2026-08-26・運営者の指示＋Codex29回目）
# ─────────────────────────────────────────────────────────────
#   ★運営者の指示★＝「いちいちほかサイトから引っ張ってきてるって
#   分かるように書かなくていい」「ほかサイトのコピーと思われたくない」。
#
#   ★★監査17（サイト名の名簿）ではこれを見つけられない★★＝
#   名前を伏せた「出典2件で一致」は**名簿に1件も当たらない**。
#   実際、記事に148か所・ひな型・meta説明・固定ページに残っていて、
#   運営者が自分で記事を読んで気づいた。＝別の型なので別の見張りが要る。
#
#   ★見るのは読者に届くものだけ★＝
#     ・HTMLは**見える文字**と meta／OGP／JSON-LD（コメント・scriptは見ない）
#       ＝2026-08-26に生のまま数えたら、131ページのJSコメント
#         「JSソース内のURL」が全部引っかかった。
#     ・記事データ（machine-details）と machines.json は全文
#       （鍵は英字なので当たらない・identity は読者に出ないので外す）
#     ・X投稿の定型文は**文字列だけ**を ast で見る（コメントを拾わない）
#
#   ★例外はファイル単位にしない★＝その一文だけを名指しする
#     （ファイルごと外すと、同じファイルに新しく入ったものを見逃す）
_SOURCE_WORDS = (
    "出典", "解析サイト", "情報源", "解析元", "掲載元", "引用元",
    "他サイト", "別サイト",
    # ★2026-08-26・Codex30回目★＝この見張りを作った翌日に、
    #   ★実在する4文を見逃していた★ことが分かって足した語。
    "出所", "情報元", "参照元", "データ元", "外部サイト",
    "他社サイト", "元サイト",
)
# ★数え方の言い回し★（サイト名も「出典」も使わずに、よそから採ったと分かる形）
_SOURCE_PATTERNS = (
    # ★数の単位と、続く言い方を広く見る★（2026-08-26・Codex30回目）
    #   ★直す前は「3サイト以上で確認」を拾えなかった★
    #   （`\d+サイトで一致` しか見ていなかった＝「以上」「確認」が抜ける）。
    #   ★全角の数字も見る★
    r"[0-9０-９]+\s*(?:件|出典|サイト|媒体|社|記事|票)\s*(?:以上)?\s*"
    r"(?:で|に)?\s*(?:一致|確認|照合|掲載)",
    # ★「複数の◯◯を〜」の形★（数を書かずに同じことを言える）
    r"複数の(?:サイト|媒体|記事|解析情報|解析データ|公開情報|情報)"
    r".{0,12}(?:一致|確認|照合|参考|もとに|基づ)",
    # ★「解析情報をもとに」の形★（語だけ禁止すると「解析情報待ち」まで拾う）
    r"(?:解析情報|解析データ|公開情報)\s*(?:を|に)?\s*"
    r"(?:もとに|基に|参考に|基づ|照合)",
    r"公開されている.{0,14}をもとに",
)
# ★この一文だけは通す★（うちの根拠の話ではないもの）
_SOURCE_ALLOWED_SENTENCES = (
    # 受け付けない問い合わせの例（contact.html）
    "解析情報の有償販売・他社の有料情報源からの転載依頼",
    "他サイトへの誘導・スパムと判断される内容",
    # ★Google AdSense の定型の説明（privacy.html）★
    #   うちの根拠の話ではない。★文言を変えると説明として不正確になる★ので
    #   消さずに名指しで通す。
    "ユーザーの過去の当サイトや他サイトへのアクセス情報に基づいた広告を配信する",
    # ★運営者情報の「どうやって正確さを担保しているか」★（2026-08-26・運営者の判断
    #   「言い換えて残す」）＝AdSense審査での透明性のために**意図して残した一文**。
    #   ★どこから採ったかは言っていない★（サイトの種別に触れていない）。
    "原則として複数の情報を突き合わせて確認しています",
)


def _judge_54_wording(text: str) -> list:
    """★判定はここだけ★（対照実験がこの関数を直接たたく）"""
    import re as _re
    t = str(text or "")
    for ok in _SOURCE_ALLOWED_SENTENCES:
        t = t.replace(ok, "")
    hits = []
    for w in _SOURCE_WORDS:
        c = t.count(w)
        if c:
            hits.append(f"'{w}' × {c}件")
    for p in _SOURCE_PATTERNS:
        m = _re.findall(p, t)
        if m:
            hits.append(f"'{m[0]}' のような書き方 × {len(m)}件")
    return hits


def _check_54_selftest() -> list:
    """★見張り54が本当に働くかを、毎回いっしょに確かめる★（対照実験）

    ★本物のファイルは触らない★＝判定の関数に文字列を渡すだけ。
    （2026-08-24に、本番のファイルへ書いてから戻す作りで
      偽の機種を残す事故を起こしたので、この形にする）
    """
    bad = []
    for name, text, want in (
            ("出典という語", "天井は999Gです（出典2件で一致）。", True),
            ("解析サイト", "解析サイトの数値を基準にしています。", True),
            ("情報源", "最新の情報は各情報源をご確認ください。", True),
            ("件数の言い回し", "設定別の数値（2件で一致）。", True),
            ("出典の数え方", "3出典で確認しました。", True),
            ("公開されている〜をもとに",
             "掲載情報は公開されている解析データや実戦値をもとに作成しています。",
             True),
            ("★受け付けない問い合わせの一文は通す★",
             "<li>他サイトへの誘導・スパムと判断される内容</li>", False),
            ("★有料情報源からの転載依頼も通す★",
             "<li>解析情報の有償販売・他社の有料情報源からの転載依頼</li>", False),
            ("★通してよい一文の中に別の違反があれば鳴る★",
             "<li>他サイトへの誘導・スパムと判断される内容</li>"
             "<p>出典は2件です</p>", True),
            ("★AdSenseの定型文は通す★",
             "ユーザーの過去の当サイトや他サイトへのアクセス情報に基づいた"
             "広告を配信することがあります。", False),
            # ★★実際に見逃していた4文★★（2026-08-26・Codex30回目）
            #   ★列挙した言い方しか試さないと、語彙の不足に気づけない★
            ("★実在①（トップ）★", "掲載情報は複数の解析情報を毎日照合し、"
             "新台の追加やデータの検証を行っています。", True),
            ("★実在②（交換率ガイド）★", "複数の解析情報を参考にした"
             "当サイトの目安であり、", True),
            ("★実在③（お問い合わせ）★", "最新の解析情報に基づいて"
             "速やかに修正いたします。", True),
            ("★実在④（記事データ）★", "トロフィー色は3サイト以上で確認。", True),
            ("　全角の数字でも拾う", "トロフィー色は３サイト以上で確認。", True),
            ("　『解析情報待ち』は拾わない（普通の言い方）",
             "詳細な出現率は解析情報待ちです。", False),
            ("　『解析中』も拾わない", "ゾーン実戦値は解析中です。", False),
            ("★運営者情報の一文は通す（運営者が残すと決めた）★",
             "原則として複数の情報を突き合わせて確認しています。", False),
            ("　その一文に別の違反が続けば鳴る",
             "原則として複数の情報を突き合わせて確認しています。出典は2件です。",
             True),
            ("ふつうの記事本文", "天井は999Gで、恩恵はATです。", False),
            ("いまの名乗り（確認1件のみ）", "天井は999G（確認1件のみ）です。", False)):
        got = bool(_judge_54_wording(text))
        if got != want:
            bad.append(f"★{name}★ → {'鳴った' if got else '黙った'}")
    return bad


_IN_54 = [False]


def _visible_and_meta(html: str) -> str:
    """読者に届く文字だけ（見える本文 ＋ meta/OGP/JSON-LD）。

    ★コメントと script は入れない★＝そこに書いてあっても読者は読まない。
    ★ただし JSON-LD（application/ld+json）は検索結果に出るので入れる★
    """
    import json as _json
    import re as _re
    import html_check as _hc54
    try:
        vis = _hc54.visible_text(html)
    except Exception:                                    # noqa: BLE001
        vis = html
    # ★引用符の種類に頼らない★（2026-08-26・Codex30回目）
    #   OGP は property= なので name だけ見ると落ちる。中身だけ全部拾う。
    try:
        metas = list(_hc54.parse(html).meta_contents)
    except Exception:                                # noqa: BLE001
        metas = _re.findall(
            r"""<meta[^>]*?content=(?:"([^"]*)"|'([^']*)')""", html)
        metas = [a or b for a, b in metas]
    lds = []
    for m in _re.finditer(
            r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>',
            html, _re.S):
        try:
            lds.append(_json.dumps(_json.loads(m.group(1)), ensure_ascii=False))
        except Exception:                                # noqa: BLE001
            lds.append(m.group(1))
    return " ".join([vis] + metas + lds)


def _py_string_literals(path: Path) -> str:
    """.py の中の**文字列だけ**（コメント・変数名は拾わない）"""
    import ast as _ast
    try:
        tree = _ast.parse(load_text(path))
    except Exception:                                    # noqa: BLE001
        return ""
    out = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
    return " ".join(out)


def _scan_54(base) -> list:
    """★読者に届くものを走査する（本体も対照実験もここを通る）★

    ★切り出した理由★（2026-08-26・Codex31回目のP1）
      対照実験が `_judge_54_wording()` に文を渡すだけだったので、
      ★OGPの読み取りや checker.html の走査を外しても緑のまま★だった。
      ＝罠⑤（関数だけ試して、読み取り経路を試していない）。
    """
    ng = []
    # ① 記事データ
    for jf in sorted((base / "assets" / "data" / "machine-details")
                     .glob("*.json")):
        for h in _judge_54_wording(load_text(jf)):
            ng.append(f"machine-details/{jf.name}: {h}")
    # ② 機種一覧（同定の控えは読者に出ないので外す）
    mj = base / "assets" / "data" / "machines.json"
    if mj.is_file():
        for h in _judge_54_wording(_strip_identity(load_text(mj))):
            ng.append(f"machines.json: {h}")
    # ③ 固定ページ・ひな型（見える文字＋meta＋JSON-LD）
    for hf in sorted(base.glob("*.html")):
        if hf.name == "404.html":
            continue
        for h in _judge_54_wording(_visible_and_meta(load_text(hf))):
            ng.append(f"{hf.name}: {h}")
    # ④ 公開ページ（★checker.html も読者が見る★・2026-08-26・Codex30回目）
    for hf in sorted((base / "machines").glob("*/*.html")):
        for h in _judge_54_wording(_visible_and_meta(load_text(hf))):
            ng.append(f"machines/{hf.parent.name}/{hf.name}: {h}")
    # ⑤ X投稿の定型文（文字列だけ）
    for py in ("post_to_x.py", "post_update_to_x.py"):
        p = base / "scripts" / py
        if p.is_file():
            for h in _judge_54_wording(_py_string_literals(p)):
                ng.append(f"{py}（投稿文）: {h}")
    return ng


def _check_54_scan_selftest() -> list:
    """★読み取り経路そのものの対照実験★（一時ディレクトリに実物を置く）"""
    import shutil as _sh54
    import tempfile as _tf54
    bad = []
    d = _tf54.mkdtemp(prefix="uchi_a54_")
    try:
        root = Path(d)
        (root / "machines" / "zzz_a54").mkdir(parents=True)
        # ⓐ OGP（property=）の中身
        (root / "zzz_ogp.html").write_text(
            '<html><head><meta property="og:description" '
            'content="出典は2件です"></head><body>本文</body></html>',
            encoding="utf-8")
        # ⓑ ポチポチくんのページ（index.html ではない）
        (root / "machines" / "zzz_a54" / "checker.html").write_text(
            "<html><body><p>解析サイトの数値です</p></body></html>",
            encoding="utf-8")
        # ⓒ 何も無いページ（鳴ってはいけない）
        (root / "machines" / "zzz_a54" / "index.html").write_text(
            "<html><body><p>天井は999Gです</p></body></html>",
            encoding="utf-8")
        got = _scan_54(root)
        if not any("zzz_ogp.html" in x for x in got):
            bad.append("★OGP（property=）の中身を見ていません★")
        if not any("checker.html" in x for x in got):
            bad.append("★ポチポチくんのページを見ていません★")
        if any("zzz_a54/index.html" in x for x in got):
            bad.append("★問題の無いページで鳴っています★")
        # ⓓ コメントの中は鳴らない（読者に届かない）
        (root / "zzz_cmt.html").write_text(
            "<html><body><!-- 出典は2件です --><p>天井は999G</p></body></html>",
            encoding="utf-8")
        if any("zzz_cmt.html" in x for x in _scan_54(root)):
            bad.append("★コメントの中で鳴っています（読者に届きません）★")
    finally:
        _sh54.rmtree(d, ignore_errors=True)
    return bad


def check_54_source_wording(machines: list) -> list:
    """どこから採ったかを読者に見せていないか（★サイト名とは別の型★）"""
    if _IN_54[0]:
        return []
    _IN_54[0] = True
    try:
        ng = [f"★見張り54そのものが働いていません★: {x}"
              for x in _check_54_selftest() + _check_54_scan_selftest()]
        return ng + _scan_54(BASE)
    finally:
        _IN_54[0] = False



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
    ("35_危ない表現の残り", check_35_risky_atoms),
    ("36_同じ事実の重複行", check_36_duplicate_facts),
    ("37_手順書と実装の食い違い", check_37_skill_vs_code),
    ("38_ログイン名の露出", check_38_home_path_leak),
    ("39_票の数え方", check_39_vote_counting),
    ("40_slugと機種ページURLの対応", check_40_slug_binding),
    ("41_自動で通信してよい先", check_41_automation_policy),
    ("42_通信の用途の名乗り", check_42_fetch_purpose),
    ("43_定義の無い内部関数の呼び出し", check_43_undefined_names),
    ("44_中身なしの設定示唆の箱", check_44_empty_settei_box),
    ("45_中身なしの噂の箱", check_45_rumor_declared_empty),
    ("46_ポチポチくんの案内と飛び先", check_46_pochipochi_reachable),
    ("47_公開ページに残った型式名", check_47_model_code_in_html),
    ("48_台帳CLIの引数の並べ場所", check_48_ledger_argv),
    ("49_等価の呼び方", check_49_equivalence_label),
    ("50_契約が依存まで閉じているか", check_50_contract_closure),
    ("51_試験の数え方が早すぎないか", check_51_selftest_tally),
    ("52_試験用の残骸", check_52_test_residue),
    ("53_出典の投稿欄", check_53_source_user_area),
    ("54_どこから採ったかの言い回し", check_54_source_wording),
    ("55_文体（です・ます）の残り", check_55_plain_style),
]


# ★お知らせだけの項目★（見たいが、公開は止めない）
#   ★決めたことをコードに反映する★（廃止したルール・記事の正しさと
#     無関係なことで、読者に届くものを止めない）
#   9_記事文字数 : 「1500字以上」ルールは2026-07-24に廃止済み。
#   23_CLAUDE_md肥大: ★説明書の大きさ★。読者には何の関係もない。
#   31_Codexへの未報告: ★私の作業の進み方★。読者には何の関係もない。
#
# ★2026-08-12・実際に起きたこと★
#   「9・23・31は公開を止めない」と決定事項表に書いていたのに、
#   ここには 9 しか入っていなかった。そのため publish-pages が
#   **「Codexへの報告が済んでいない」だけでサイトの配信ごと落ち**、
#   運営者に失敗メールが届いた。決めた場所と動く場所がずれていた。
# ★41_自動で通信してよい先は、ここに入れない（＝止める）★
#   （2026-08-16）巡回する4社の規約を全部読んで名簿に載せたので0件になった。
#   記録＝`_design/tos_review_2026-08-16.md`。
#   ★ここが赤いまま進めると、規約を確かめていない先へ毎晩アクセスする★
#   （今回の事故そのもの）。巡回先を足したいなら、先に規約を読む。
# ★55_文体：2026-09-03に「止めない」へ移した（運営者の判断「いいよ」）★
#   ★運営者の言葉★
#     > 私の理想は至極単純／2つのAIを使って記事を作る／これだけ。
#     > だから、それもAIで更新の度に統一すれば良くない？
#   ★何が間違っていたか★＝文体は**意味の判断**なのに、
#   1189件の違反を一覧にして「増えたら止める」という★名簿の型★で守っていた。
#   ＝機械が「この言い方は常体だ」と決めていたのと同じこと。
#   ★実害★＝機械が作った書き出しが決まりから外れていたため、
#   導入日が細かくなるたびに★育成が丸ごと取り消され★、
#   新台13機種が何日も検索に載らないままだった（台帳#552）。
#   ★いまの形★＝機械は候補を挙げるだけ／そろえるのは2AI（更新タスク STEP 1-Q の1番）。
#   ★報告は続ける★＝増減はℹ️で出るので、放置には気づける。
# ★16_文体混在も同じ日に「止めない」へ移した★（2026-09-03）
#   ★見つけた経緯★＝55の対照実験で、**もう1つ別の文体の関所が止めていた**。
#   16の見つけ方は
#     (?:だ|である|した|する|った|ない|だが|だろう|なる|させる|られる|られた)$
#   ＋ fix_plain_style.ENDINGS の表＝★機械が「常体だ」と決めている★。
#   しかも★0件許容★（1文でもNG）なので、公開済み113機種のどれかに
#   常体が1文入るだけで**サイトの配信ごと落ちる**。
#   ★55だけ外して16を残すと、同じ事故がもう一度起きる★ので一緒に移した。
INFO_ONLY = {"9_記事文字数", "23_CLAUDE_md肥大検知", "31_Codexへの未報告",
             "55_文体（です・ます）の残り", "16_文体混在"}


def selftest() -> int:
    """★見張り自身が働いているかを確かめる★（2026-08-24新設）

    ★なぜ要るか★＝見張りを足しても、その見張りが空振りしていることがある
    （実際、監査53は名簿の鍵の名前を取り違えて**1件も見つけない**状態だった）。
    ★ここは判定だけを試す★＝本物の名簿もサイトも触らない。
    """
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅ " if cond else "❌ ") + name)

    for x in _check_52_selftest():
        t("★見張り52★ " + x, False)
    t("★★見張り52（試験用の残骸）が働いている★★", not _check_52_selftest())
    for x in _check_53_selftest():
        t("★見張り53★ " + x, False)
    t("★★見張り53（出典の投稿欄）が働いている★★", not _check_53_selftest())
    # ★★2026-08-26：54をここに足し忘れていた★★
    #   壊し方の道具が見つけた＝「見張り54を黙らせる」を試しても試験は緑だった。
    #   ＝53には入れてあったのに、同じことを翌週にやった。
    for x in _check_54_selftest():
        t("★見張り54★ " + x, False)
    t("★★見張り54（どこから採ったかの言い回し）が働いている★★",
      not _check_54_selftest())
    for x in _check_54_scan_selftest():
        t("★見張り54の読み取り★ " + x, False)
    t("★★見張り54が、読者に届くものを実際に読めている★★"
      "／★文を直接渡す試験だけでは、読み取りを外しても緑になる★",
      not _check_54_scan_selftest())
    # ★★契約の状態を見分けられるか★★（2026-09-01・Codexの指摘3）
    #   ★直す前は「無い／壊れている／辞書でない」を全部まとめていた★ので、
    #   契約が消えても壊れても監査37が黙って通った＝見張りが静かに消える。
    #   ★一時の置き場で試す★（本番の契約は触らない・罠㉗）。
    import json as _js37, tempfile as _tf37
    _d37 = _tf37.mkdtemp(prefix="contract_")
    _p37 = os.path.join(_d37, "tasks-contract.json")
    t("★契約が無ければ止める★（置き場はあるのに契約が無い）",
      _skill_contract(_d37)[1] != "")
    with open(_p37, "w", encoding="utf-8") as _f37:
        _f37.write("{壊れた")
    t("★契約が読めなければ止める★", _skill_contract(_d37)[1] != "")
    with open(_p37, "w", encoding="utf-8") as _f37:
        _f37.write("[]")
    t("★契約の形が違えば止める★（いちばん外側が辞書でない）",
      _skill_contract(_d37)[1] != "")
    with open(_p37, "w", encoding="utf-8") as _f37:
        _js37.dump({"live": [], "stopped": [], "skills": []}, _f37)
    t("　正しい契約なら通す（中身が0件でも正常）",
      _skill_contract(_d37) == ({"live": [], "stopped": [], "skills": []}, ""))
    with open(_p37, "w", encoding="utf-8") as _f37:
        _js37.dump({"live": ["a"], "stopped": [], "skills": []}, _f37)
    t("　中身のある契約もそのまま返す",
      _skill_contract(_d37)[0].get("live") == ["a"]
      and _skill_contract(_d37)[1] == "")
    # ★★鍵が欠けたら止める★★（2026-09-01・Codexのレビュー30の指摘2）
    #   ★直す前は外側が辞書かしか見ていなかった★ので、
    #   `skills` の鍵を1つ消すだけでスキルの監査が丸ごと黙った。
    for _k37 in _CONTRACT_KEYS:
        _c37 = {"live": [], "stopped": [], "skills": []}
        del _c37[_k37]
        with open(_p37, "w", encoding="utf-8") as _f37:
            _js37.dump(_c37, _f37)
        t(f"★契約の鍵が1つ欠けたら止める★（{_k37}）",
          _skill_contract(_d37)[1] != "")
    with open(_p37, "w", encoding="utf-8") as _f37:
        _js37.dump({"live": "a", "stopped": [], "skills": []}, _f37)
    t("★契約の値が並びでなければ止める★", _skill_contract(_d37)[1] != "")
    with open(_p37, "w", encoding="utf-8") as _f37:
        _js37.dump({"live": [1], "stopped": [], "skills": []}, _f37)
    t("★契約の中身が文字列でなければ止める★", _skill_contract(_d37)[1] != "")
    with open(_p37, "w", encoding="utf-8") as _f37:
        _js37.dump({"live": ["  "], "stopped": [], "skills": []}, _f37)
    t("★契約の中身が空白だけでも止める★", _skill_contract(_d37)[1] != "")
    with open(_p37, "w", encoding="utf-8") as _f37:
        _f37.write("{}")
    t("★空の辞書は「正しい契約」ではない★（鍵がそろっていない）",
      _skill_contract(_d37)[1] != "")
    # ★★前後の空白と、知らない鍵★★（2026-09-01・Codexのレビュー31の指摘4）
    with open(_p37, "w", encoding="utf-8") as _f37:
        _js37.dump({"live": [], "stopped": [" old-task "], "skills": []}, _f37)
    t("★契約の中身に前後の空白があれば止める★"
      "／★通すと、照合が空白ごと探して見張りが黙って外れる★",
      _skill_contract(_d37)[1] != "")
    with open(_p37, "w", encoding="utf-8") as _f37:
        _js37.dump({"live": [], "stopped": [], "skills": [], "skils": []},
                   _f37)
    t("★契約に知らない鍵があれば止める★（書き間違いが黙って無視される）",
      _skill_contract(_d37)[1] != "")
    with open(_p37, "w", encoding="utf-8") as _f37:
        _js37.dump({"_why": "覚え書き", "live": [], "stopped": [],
                    "skills": []}, _f37)
    t("　覚え書き（_ で始まる鍵）は許す",
      _skill_contract(_d37)[1] == "")
    # ★★置き場ごと消えたときに黙らない★★（Codexのレビュー30の指摘3）
    import tempfile as _tf38
    # ★親も消す★（2026-09-02・Codexの指摘）＝空フォルダが残っていた
    _gone_root = _tf38.mkdtemp(prefix="gone_")
    _gone37 = os.path.join(_gone_root, "no_such_dir")
    t("　置き場が無いときは、ふだんは止めない（別PC・CI）",
      check_37_skill_vs_code([], required=False) is not None
      and _no_dir_ok(_gone37, False))
    t("★置き場が無いのを必須モードでは止める★（運用PCで消えたら黙らない）",
      _no_dir_ok(_gone37, True) is False)
    import shutil as _sh38
    _sh38.rmtree(_gone_root, ignore_errors=True)
    import shutil as _sh37
    _sh37.rmtree(_d37, ignore_errors=True)

    # ★★2026-09-01：39をここに足す★★（Codexの指摘5）
    #   ★票の数え方は「独立2出典」の土台★＝読者に届く情報側の穴。
    _w39 = _check_39_selftest()
    for x in _w39:
        t("★見張り39★ " + x, False)
    t("★★見張り39（票の数え方）が働いている★★", not _w39)
    # ★★2026-09-01：37をここに足す★★
    #   ★足し忘れると、監査本体にだけ対照実験があっても
    #     `--selftest` からは一度も動かない★（54で同じことをした）。
    # ★★配線の経路試験★★（2026-09-01・Codexのレビュー31の指摘2）
    #   ★共通関数の単体試験だけでは、呼び出し行を消しても緑★（罠③）。
    # ★1度だけ呼ぶ★（2026-09-02・Codexの指摘）＝
    #   一時フォルダを作って消す処理なので、2回呼ぶと副作用も2回。
    _w37 = _check_37_wiring()
    for x in _w37:
        t("★見張り37の配線★ " + x, False)
    t("★★見張り37が、live とスキルの両方に本当に繋がっている★★", not _w37)
    _s37 = _check_37_selftest()
    for x in _s37:
        t("★見張り37★ " + x, False)
    t("★★見張り37（スキルの手順書が古くなっていないか）が働いている★★",
      not _s37)
    t("★★試験の数え方の見張りが働いている★★（項目51）",
      not check_51_selftest_tally([]))
    ng = [n for n, ok in results if not ok]
    print(f"{chr(10)}{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng[:5])
    return 1 if ng else 0

def main():
    # ★★スキル・手順書の監査だけを回す入口★★
    #   （2026-09-01・Codexのレビュー30の指摘3）
    #   ★ローカルの関所は記事の変更が無くてもこれを流す★＝
    #   サイト監査は重いので全部は流せないが、ここは一瞬で終わる。
    #   `--required` を付けると「置き場が無い」も赤になる（運用PC用）。
    if "--skill-audit" in sys.argv:
        _req = "--required" in sys.argv
        _ngs = check_37_skill_vs_code([], required=_req)
        for _x in _ngs:
            print("❌ " + _x)
        if not _ngs:
            print("手順書の監査: 問題ありません")
        sys.exit(1 if _ngs else 0)
    # ★見張り自身の対照実験だけを回す★（2026-08-24）
    if "--selftest" in sys.argv:
        # ★戻り値ではなく終了コードで返す★（2026-08-24・自分で踏んだ）
        #   この main の戻り値はどこにも使われていないので、
        #   ★赤でも終了コード0で終わって「合格」に見えていた★。
        sys.exit(selftest())
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
        if name not in INFO_ONLY:         # ★お知らせだけの項目は数えない★
            total_ng += len(ngs)

    if out_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print(f"=== サイト構造整合性チェック（NG合計: {total_ng}件）===")
        for name, ngs in results.items():
            mark = ("ℹ️" if name in INFO_ONLY and ngs
                    else ("✅" if not ngs else "❌"))
            print(f"\n{mark} {name}: {len(ngs)}件")
            for ng in ngs:
                print(f"   - {ng}")
    sys.exit(0 if total_ng == 0 else 1)


if __name__ == "__main__":
    main()

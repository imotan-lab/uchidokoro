# -*- coding: utf-8 -*-
"""
ハブ/ランキング記事ページ一括生成スクリプト

machines.json（データ）と scripts/hub_prose.json（散文）から、以下4ページを生成する：
    guide-tenjo-ranking.html  天井が浅い機種ランキング   （表 A: G数天井 昇順・1000G未満）
    guide-reset-ranking.html  朝一リセット狙いランキング   （表 C: 狙い目短縮幅 降順 TOP30）
    guide-suru-tenjo.html     スルー天井の機種一覧と狙い方 （表 D: スルー天井 全件）
    guide-ichiran.html        全機種 狙い目・天井 早見表   （表 ALL: 全機種 稼働率順）

★表データは machines.json から毎回機械生成するため、新台が追加されると再実行で自動的に最新化される。
machine-details/machines.json を更新した後・本スクリプトを更新した後は必ず再実行すること。
verify（5:05）/ auto-add（0:00）タスクからも呼ばれる想定。

使い方:
    python scripts/build_hub_pages.py

注意:
    - 生成HTMLはルート直下なので <base href="/"> は不要（audit_site.py 項目18の対象外）。
    - インラインstyle禁止（項目1）：装飾は practical.css の .rank-list / .spec-list 等を使う。
    - 他サイト名禁止（項目17）・旧URL machine.html?slug= 禁止（項目20）：本スクリプトは出さない。
    - meta description は 50〜160字（項目11）：hub_prose.json 側で担保。
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent
MACHINES = BASE / "assets" / "data" / "machines.json"
PROSE = BASE / "scripts" / "hub_prose.json"

SITE = "https://uchidokoro.com"

# ガイド/ハブの全ページ（関連リンク生成に使う・label は短め）
PAGES = [
    ("guide-tenjo-ranking.html", "天井が浅い機種ランキング"),
    ("guide-reset-ranking.html", "朝一リセット狙いランキング"),
    ("guide-suru-tenjo.html", "スルー天井の一覧と狙い方"),
    ("guide-ichiran.html", "全機種 狙い目・天井 早見表"),
    ("guide-haena.html", "初心者向けハイエナ講座"),
    ("guide-rate.html", "交換率と期待値の考え方"),
    ("guide-pochipochi.html", "ポチポチくんの使い方"),
]


def esc(s) -> str:
    """HTMLエスケープ"""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def md(s) -> str:
    """エスケープ後に **強調** を <strong> に変換（散文用）"""
    out = esc(s)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    return out


def mode_key(x):
    return x.get("key") if isinstance(x, dict) else x


def ck(m, mode, key):
    c = m.get("checker") or {}
    if not isinstance(c, dict):
        return None
    sub = c.get(mode) or {}
    return sub.get(key) if isinstance(sub, dict) else None


def mode_conf(c, key):
    """モード設定の共通アクセサ。checker直下（checker.normal等）と
    checker.modeData配下（新形式・sao/bandori/hanma_baki等）の両方を探す
    （modeData形式の3機種が全集計から漏れていた事故の修正・2026-07-13）。"""
    if not isinstance(c, dict):
        return None
    v = c.get(key)
    if isinstance(v, dict):
        return v
    md = c.get("modeData")
    if isinstance(md, dict) and isinstance(md.get(key), dict):
        return md[key]
    return None


def base_caution(m):
    """リセット比較の基準となる通常時系モードのcaution値。
    normalを優先し、無ければmodes宣言順にreset系以外のモード（cz等）を使う
    （基準モードがnormalでない機種＝東京喰種/攻殻/ヴヴヴ2/ダンベル/バキが
    リセットランキングから漏れていた事故の修正・2026-07-13）。"""
    c = m.get("checker") or {}
    if not isinstance(c, dict):
        return None
    v = mode_conf(c, "normal")
    if isinstance(v, dict) and isinstance(v.get("caution"), (int, float)):
        return v["caution"]
    for x in (c.get("modes") or []):
        k = mode_key(x)
        if not isinstance(k, str) or "reset" in k.lower():
            continue
        v = mode_conf(c, k)
        if isinstance(v, dict) and isinstance(v.get("caution"), (int, float)):
            return v["caution"]
    for k, v in c.items():
        if k in ("reset", "modeData") or "reset" in str(k).lower() or not isinstance(v, dict):
            continue
        cv = v.get("caution")
        if isinstance(cv, (int, float)):
            return cv
    return None


def _scalar_limit(lim):
    """mode別limit(dict)なら normal（無ければ最初の値）を、スカラーならそのまま返す。
    2026-07-23 enen2 等でリセット天井を分けるため limit がモード別objectになり得る。"""
    if isinstance(lim, dict):
        return lim["normal"] if lim.get("normal") is not None else next(iter(lim.values()), None)
    return lim


def load_rows():
    machines = json.loads(MACHINES.read_text(encoding="utf-8"))
    rows = []
    for m in machines:
        c = m.get("checker") or {}
        if not isinstance(c, dict):
            c = {}
        modes = [mode_key(x) for x in (c.get("modes") or [])]
        rows.append(
            dict(
                slug=m["slug"],
                name=m["name"],
                info=m.get("info", ""),
                strategy=m.get("strategy", ""),
                limit=_scalar_limit(m.get("limit")),
                tenjo_display=m.get("tenjo_display"),
                status=m.get("status", "complete"),
                unit=c.get("unit"),
                # スルー天井はモードキー'suru'に加え'through'表記の機種がある
                # （バジ天膳/からくり/まどマギフォルテ/沖ドキDUOアンコールが漏れていた・2026-07-13修正）
                has_suru=bool(c.get("hasSuru") or "suru" in modes or "through" in modes),
                has_cycle=bool(c.get("hasCycle") or "cycle" in modes),
                ncau=base_caution(m),
                rcau=(mode_conf(c, "reset") or {}).get("caution"),
            )
        )
    return rows


def yome(r) -> str:
    s = (r.get("strategy") or "").strip()
    return s if s else "設定狙い向け（ゲーム数狙い非対応）"


def tenjo_disp(r) -> str:
    # machines.json に tenjo_display があれば優先（液晶/実など複数条件天井の一覧表記用）
    td = r.get("tenjo_display")
    if td:
        return td
    lim = r.get("limit")
    if not isinstance(lim, (int, float)):
        return "—"
    unit = r.get("unit") or "G"
    return f"{lim}{unit}"


# ---- データセット算出（analyze と同一ロジック） ----

def dataset_A(rows):
    # G数でカウントする天井（基準モードのcautionがG数で構造化されている機種）が対象。
    # スルー天井を併せ持つ機種も、G数天井があればランキングに含める
    # （旧実装はスルー併用機を一律除外しており、番長4/mhrise等が漏れていた・2026-07-13修正）。
    # 周期天井の機種と、G数天井の構造化データが無い機種（スルー専用チェッカー等）は対象外。
    a = [
        r for r in rows
        if r["unit"] == "G" and isinstance(r["limit"], (int, float))
        and not r["has_cycle"] and r["limit"] < 1000
        and isinstance(r["ncau"], (int, float))
    ]
    a.sort(key=lambda r: (r["limit"], r["ncau"]))
    return a


def dataset_C(rows):
    c = []
    for r in rows:
        if isinstance(r["rcau"], (int, float)) and isinstance(r["ncau"], (int, float)) and r["ncau"] - r["rcau"] > 0:
            c.append(dict(diff=r["ncau"] - r["rcau"], **r))
    c.sort(key=lambda r: -r["diff"])
    return c


def dataset_D(rows):
    return [r for r in rows if r["has_suru"]]


# ---- 散文ブロック → HTML ----

def render_blocks(blocks):
    html = []
    for b in blocks:
        html.append('    <article class="article-block">')
        html.append(f'      <h2 class="block-label">▶ {md(b["label"])}</h2>')
        for i, para in enumerate(b.get("paras", [])):
            cls = "hint-text" if i == 0 else "hint-text spacing-sm"
            html.append(f'      <p class="{cls}">{md(para)}</p>')
        html.append("    </article>")
    return "\n".join(html)


def render_rank_list(items, meta_fn):
    html = ['      <ol class="rank-list">']
    for i, r in enumerate(items, 1):
        href = f"/machines/{r['slug']}/"
        html.append('        <li class="rank-item">')
        html.append(f'          <span class="rank-num">{i}</span>')
        html.append('          <span class="rank-body">')
        html.append(f'            <a class="rank-name" href="{href}">{esc(r["name"])}</a>')
        html.append(f'            <span class="rank-meta">{meta_fn(r)}</span>')
        html.append("          </span>")
        html.append("        </li>")
    html.append("      </ol>")
    return "\n".join(html)


def render_spec_list(items, meta_fn):
    html = ['      <ul class="spec-list">']
    for r in items:
        href = f"/machines/{r['slug']}/"
        html.append('        <li class="spec-item">')
        html.append(f'          <a class="spec-name" href="{href}">{esc(r["name"])}</a>')
        html.append(f'          <span class="spec-meta">{meta_fn(r)}</span>')
        html.append("        </li>")
    html.append("      </ul>")
    return "\n".join(html)


def related_html(self_file):
    items = []
    for fn, label in PAGES:
        if fn == self_file:
            continue
        items.append(f'      <a class="related-item" href="{fn}">{esc(label)}</a>')
    items.append('      <a class="related-item" href="index.html">トップページ（機種検索）</a>')
    return '    <div class="related-list">\n' + "\n".join(items) + "\n    </div>"


HEAD_TPL = """<!DOCTYPE html>
<html lang="ja">
<head>
<!-- Google tag (gtag.js) -->
<script async src="https://www.googletagmanager.com/gtag/js?id=G-MSXLEMX2VJ"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){{dataLayer.push(arguments);}}
  gtag('js', new Date());
  gtag('config', 'G-MSXLEMX2VJ');
</script>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<meta name="description" content="{desc}">
<link rel="canonical" href="{site}/{file}">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{ogdesc}">
<meta property="og:type" content="article">
<meta property="og:url" content="{site}/{file}">
<meta property="og:image" content="{site}/assets/img/ogp.png">
<meta property="og:site_name" content="うちどころ。">
<meta name="twitter:card" content="summary_large_image">
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="icon" type="image/png" href="/assets/img/favicon-32.png" sizes="32x32">
<link rel="icon" type="image/png" href="/assets/img/favicon-16.png" sizes="16x16">
<link rel="apple-touch-icon" href="/assets/img/apple-touch-icon.png">
<link rel="stylesheet" href="assets/css/practical.css">
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#07090c">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="うちどころ。">
<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-2097489177716087" crossorigin="anonymous"></script>
</head>
<body>
<header class="site-header">
  <div class="header-inner">
    <a class="brand" href="index.html"><img src="assets/img/logo.png" alt="うちどころ。"></a>
    <nav class="header-nav">
      <a href="index.html">トップ</a>
      <a href="about.html">このサイトについて</a>
      <a href="contact.html">お問い合わせ</a>
      <a href="privacy.html">プライバシーポリシー</a>
      <a href="https://x.com/uchidokoro" target="_blank" rel="noopener" class="header-x">𝕏</a>
    </nav>
  </div>
</header>
<main class="site-main">
  <section class="article-hero article-hero--compact">
    <p class="eyebrow">{eyebrow}</p>
    <h1 class="page-title">{h1}</h1>
    <p class="hero-sub">{hero_sub}</p>
  </section>
  <section class="article-section-wrap">
"""

FOOT_TPL = """  </section>
</main>
<footer>
  <div class="site-footer-inner">
    <div class="footer-links">
      <a href="about.html">このサイトについて</a>
      <a href="guide-haena.html">ハイエナ講座</a>
      <a href="guide-rate.html">交換率と期待値</a>
      <a href="guide-pochipochi.html">ポチポチくんの使い方</a>
      <a href="contact.html">お問い合わせ</a>
      <a href="privacy.html">プライバシーポリシー</a>
      <a href="https://x.com/uchidokoro" target="_blank" rel="noopener">X (@uchidokoro)</a>
    </div>
    <p class="footer-copy">&copy; 2026 うちどころ。</p>
  </div>
</footer>
<script>
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/service-worker.js")
      .catch(err => console.warn("SW登録失敗:", err));
  });
}
</script>
</body>
</html>
"""


def build_page(file, prose, data_html):
    head = HEAD_TPL.format(
        title=esc(prose["title"]),
        desc=esc(prose["meta_description"]),
        ogdesc=esc(prose.get("og_description") or prose["meta_description"]),
        site=SITE,
        file=file,
        eyebrow=esc(prose["eyebrow"]),
        h1=esc(prose["h1"]),
        hero_sub=esc(prose["hero_sub"]),
    )
    parts = [head]
    # 導入
    parts.append(render_blocks(prose.get("intro_blocks", [])))
    # 表（caption + data + note）
    table_block = ['    <article class="article-block">']
    table_block.append(f'      <p class="hint-text">{md(prose["table_caption"])}</p>')
    table_block.append(data_html["list"])
    if data_html.get("note"):
        table_block.append(f'      <p class="list-note">{data_html["note"]}</p>')
    table_block.append("    </article>")
    parts.append("\n".join(table_block))
    # 解説
    parts.append(render_blocks(prose.get("outro_blocks", [])))
    # 関連
    parts.append('    <article class="article-block">')
    parts.append('      <h2 class="block-label">▶ 関連ガイド・ランキング</h2>')
    parts.append(related_html(file))
    parts.append("    </article>")
    parts.append(FOOT_TPL)
    return "\n".join(parts)


def main():
    rows = load_rows()
    prose_all = json.loads(PROSE.read_text(encoding="utf-8"))

    A = dataset_A(rows)
    C = dataset_C(rows)
    D = dataset_D(rows)
    ALL = rows  # machines.json 順（稼働率順）

    # 散文内の件数はプレースホルダで持ち、生成時に実数を埋める
    # （手書き数字がデータ更新に追従せずズレる事故の恒久対策・2026-07-12）
    counts = {
        "{COUNT_A}": str(len(A)),
        "{COUNT_C}": str(len(C)),
        "{COUNT_D}": str(len(D)),
        "{COUNT_ALL}": str(len(ALL)),
    }

    def fill(obj):
        if isinstance(obj, str):
            for k, v in counts.items():
                obj = obj.replace(k, v)
            return obj
        if isinstance(obj, list):
            return [fill(x) for x in obj]
        if isinstance(obj, dict):
            return {k: fill(v) for k, v in obj.items()}
        return obj

    prose_all = fill(prose_all)

    # --- tenjo ---
    tenjo_list = render_rank_list(
        A, lambda r: f'天井 <strong>{tenjo_disp(r)}</strong> ／ 狙い目 {esc(yome(r))}'
    )
    tenjo_note = (
        "※同じ天井ゲーム数の機種は、狙い目ゲーム数が浅い順に掲載しています。"
        f"G数でカウントする天井が1000G未満の機種は全<span class=\"list-count\">{len(A)}</span>機種です"
        "（周期天井の機種と、G数天井のチェッカーデータが無い機種は集計対象外です）。"
    )

    # --- reset ---
    C_top = C[:30]
    reset_list = render_rank_list(
        C_top,
        lambda r: f'通常 <strong>{r["ncau"]}G〜</strong> → リセット後 <strong>{r["rcau"]}G〜</strong>（短縮 {r["diff"]}G）',
    )
    reset_note = (
        "※短縮幅（通常時の狙い目ライン − リセット後の狙い目ライン）が大きい順。比較の基準は各機種の主要カウンターモード（通常時またはCZ間）です。"
        f"チェッカーのデータで短縮を確認できる機種は全<span class=\"list-count\">{len(C)}</span>機種で、上位{len(C_top)}機種を掲載しています。"
    )

    # --- suru ---
    suru_list = render_spec_list(D, lambda r: esc(yome(r)))
    suru_note = (
        f"チェッカー対応データのあるスルー天井機種は全<span class=\"list-count\">{len(D)}</span>機種です。"
        "「N回目で確定」という表記は（N−1）スルーの状態を指す点に注意してください。"
    )

    # --- ichiran ---
    ichiran_list = render_spec_list(
        ALL,
        lambda r: f'{esc(r["info"])}｜天井 <strong>{tenjo_disp(r)}</strong>｜狙い目 {esc(yome(r))}',
    )
    ichiran_note = f"全<span class=\"list-count\">{len(ALL)}</span>機種（稼働率順）。機種名をタップすると各詳細ページへ移動します。"

    pages = {
        "guide-tenjo-ranking.html": (prose_all["tenjo"], {"list": tenjo_list, "note": tenjo_note}),
        "guide-reset-ranking.html": (prose_all["reset"], {"list": reset_list, "note": reset_note}),
        "guide-suru-tenjo.html": (prose_all["suru"], {"list": suru_list, "note": suru_note}),
        "guide-ichiran.html": (prose_all["ichiran"], {"list": ichiran_list, "note": ichiran_note}),
    }

    for file, (prose, data_html) in pages.items():
        html = build_page(file, prose, data_html)
        (BASE / file).write_text(html, encoding="utf-8")
        # 簡易検証：meta description 長さ
        dlen = len(prose["meta_description"])
        warn = "" if 50 <= dlen <= 160 else f"  ⚠ meta desc {dlen}字（50〜160推奨）"
        print(f"  生成: {file}  ({dlen}字 desc){warn}")

    print(f"\n完了: 4ページ生成。 A(天井浅い)={len(A)} / C(リセット恩恵)={len(C)} / D(スルー)={len(D)} / ALL={len(ALL)}")


if __name__ == "__main__":
    main()

"""
machine.html を元に、各 machines/{slug}/index.html を「中身が静的HTMLに焼き込まれた」実コンテンツページとして生成する。

【プリレンダリングの目的（2026-06 収益化/SEO対応）】
従来は machine.html を丸ごとコピーするだけで、本文・title・h1 は JS が machines.json /
machine-details を fetch して後から描画する「空シェル」だった。若いサイトは Google が JS を
後回しにするため「クロール済み・インデックス未登録」が多発し、AdSense にも「中身の無いページ」
に見えていた。本スクリプトはビルド時に下記を静的HTMLへ直接書き出し、クローラが JS 実行を待たずに
本文を読めるようにする（チェッカー等の動的UIは従来通り JS のまま）。

書き込む要素:
1. <base href="/"> を <head> 直後に挿入
2. <title> / <meta name="description"> を機種別に生成（meta-auto.js と同じロジック）
3. <link rel="canonical"> を /machines/{slug}/ に
4. <h1 id="machineTitle"> に機種名
5. <p id="heroSub"> に lead
6. <div id="articleSections"> に各セクション（machine.html の JS と同じ構造）
7. <tbody id="infoTableBody"> に factTable

machine.html 側の JS は articleSections / infoTableBody を innerHTML="" でクリアしてから再描画する
ため、プリレンダHTMLと二重描画にはならない（最終表示はJS版が権威）。

使い方:
    python scripts/build_machine_pages.py
"""

from __future__ import annotations
import html
import json
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import page_decision as _pd  # noqa: E402  ★区分の唯一の判定箇所★

BASE = Path(__file__).resolve().parent.parent

# settei バッジのクラス対応（machine.html の badgeClass と一致させる）
BADGE_CLASS = {"hint": "settei-hint", "weak": "settei-weak", "mid": "settei-mid",
               "strong": "settei-strong", "ok": "settei-ok"}


def esc(s) -> str:
    """テキストをHTMLエスケープ（& < > とダブルクォート）。"""
    return html.escape("" if s is None else str(s), quote=True)


def md(text) -> str:
    """簡易Markdown：エスケープ後に **xxx** → <strong>xxx</strong>（machine.html の md() 相当）。"""
    if not isinstance(text, str):
        return esc(text)
    out = esc(text)
    # esc後でも ** は不変なので強調変換できる
    return re.sub(r"\*\*([^*]+?)\*\*", r"<strong>\1</strong>", out)


def jp_date(date_str: str) -> str:
    if not date_str:
        return ""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", str(date_str))
    if not m:
        return ""
    return f"{int(m.group(2))}月{int(m.group(3))}日"


def extract_pochipochi_reasons(template: str) -> dict:
    """machine.html の pochipochiStatus ロジックから「ポチポチくん非対応」の
    slug → 理由 を抽出する（noSettingDiff / noAnalysis）。machine.html を単一情報源とし、
    SEO文言（title/description）とプリレンダHTMLのリンク無効化をここに同期させる（誤情報防止）。
    preview 機種は status で別途判定するためここには含めない。"""
    reasons = {}
    for var in ("noSettingDiff", "noAnalysis"):
        m = re.search(r"const\s+" + var + r"\s*=\s*\[(.*?)\]", template, re.S)
        if not m:
            continue
        # 理由文字列も machine.html から読む（build側ハードコードだと二重管理でズレる）
        rm = re.search(var + r"\.includes\(slug\)\)\s*return\s*\{[^}]*reason:\s*\"([^\"]+)\"", template)
        reason = rm.group(1) if rm else "非対応"
        for slug in re.findall(r"'([^']+)'", m.group(1)):
            reasons.setdefault(slug, reason)
    return reasons


def build_title_desc(machine: dict, pochipochi_available: bool = True) -> tuple[str, str]:
    """meta-auto.js と同じ title / description を生成。
    pochipochi_available=False の機種はSEO文言に『ポチポチくん対応』を入れない
    （非対応機種で対応と宣伝する誤情報を防ぐ）。"""
    name = machine.get("name", "")
    strategy = machine.get("strategy", "") or ""
    info = machine.get("info", "") or ""
    cls = _pd.machine_class(machine)
    is_preview = cls == "LEGACY_PREVIEW"
    release_jp = jp_date(machine.get("release_date", ""))

    if cls in ("AUTO_INDEXABLE", "AUTO_PENDING"):
        # ★新台経路: 実在する内容だけを名乗る★（2026-08-04・Codex70〜72回目）
        #   天井・狙い目は載っていないので名乗らない。
        #   時間で嘘になる語（導入予定・導入前・先行）も使わない。
        title = (machine.get("seo") or {}).get("title") \
            or f"{name} スペック・基本情報"
        desc = (f"{name}のスペック・基本情報。出典で確認が取れた項目のみ"
                "掲載しています。未掲載の項目は確認でき次第更新します。"
                + (f"登場時期は{release_jp}（公式確認）。" if release_jp else ""))
        return title, desc
    if is_preview:
        # ★時間で嘘になる語（導入予定・導入前）と「天井・狙い目」の名乗りをやめた★
        #   （2026-08-04・Codex70回目。8/3導入後も「導入予定」のmeta説明が残っていた）
        title = f"{name} スペック・基本情報｜解析判明次第更新"
        desc = (f"{name}のスペック・基本情報。出典で確認が取れた項目のみ掲載し、"
                "解析データが判明次第、随時更新します。"
                + (f"登場時期は{release_jp}（公式確認）。" if release_jp else ""))
    elif pochipochi_available:
        title = f"{name} 天井・狙い目・やめどき｜小役カウンター ポチポチくん対応"
        if strategy:
            desc = f"{name}の天井・狙い目・やめどき・設定差を徹底解説。{strategy}。小役カウンター ポチポチくんで設定判別も可能。期待値重視の立ち回りガイド。"
        else:
            desc = f"{name}の天井・狙い目・やめどき・設定差を徹底解説。小役カウンター ポチポチくんで設定判別も可能。{info}の立ち回りを期待値重視でサポート。"
    else:
        title = f"{name} 天井・狙い目・やめどき｜期待値・立ち回りガイド"
        if strategy:
            desc = f"{name}の天井・狙い目・やめどき・設定差を徹底解説。{strategy}。期待値重視の立ち回りをサポートします。"
        else:
            desc = f"{name}の天井・狙い目・やめどき・設定差を徹底解説。{info}の立ち回りを期待値重視でサポートします。"
    return title, desc


# ★未確認の箱に付ける目印★（2026-08-04・Codex77回目の指摘1）
#   ページ全体で文字列を数える形だと、別の場所に同じ文言を置くだけで
#   数がそろってしまう。**どの項目が未確認か**を構造で示す。
PENDING_ATTR = "data-pending-section"


def section_attrs(section: dict) -> str:
    """★全部の箱に目印を付ける★（2026-08-04・Codex78回目の指摘1）

    ページ側の**欠落・重複・順番・中身**を確かめられるようにするため、
    未確認の箱だけでなく全セクションに `data-section` を付ける。
    """
    from build_new_article import is_pending_body as _pending
    title = section.get("title", "")
    out = f' data-section="{esc(title)}"'
    # ★見分けるのは build_new_article の1か所★（2026-08-12・依頼160のP2-7）
    if _pending(section.get("body")):
        out += f' {PENDING_ATTR}="{esc(title)}"'
    return out


def render_section(section: dict) -> str:
    """1セクションを machine.html の JS と同じ構造の静的HTMLに。"""
    title = section.get("title", "")
    stype = section.get("type")
    body = section.get("body") or []
    if isinstance(body, str):  # 文字列を1文字ずつ<p>化する不具合の防御（2026-07-10）
        body = [x.strip() for x in body.splitlines() if x.strip()] or [body]
    body = [t for t in body if isinstance(t, str) and t.strip()]  # 空段落<p></p>の防御（2026-07-12）

    if stype == "rumor":
        paras = "".join(f'<p class="rumor-body">{md(t)}</p>' for t in body)
        inner = (f'<h3 class="article-title">{esc(title)}</h3>'
                 f'<div class="rumor-box"><p class="rumor-label">⚠ 噂・未確定情報</p>{paras}</div>')
        return f'<div class="article-item"{section_attrs(section)}>{inner}</div>'

    if stype == "settei":
        tables = section.get("tables")
        legend = ('<div class="settei-legend">'
                  '<span class="settei-legend-item"><span class="settei-legend-badge settei-weak">弱</span>弱示唆</span>'
                  '<span class="settei-legend-item"><span class="settei-legend-badge settei-mid">中</span>中示唆</span>'
                  '<span class="settei-legend-item"><span class="settei-legend-badge settei-strong">強</span>強示唆</span>'
                  '<span class="settei-legend-item"><span class="settei-legend-badge settei-ok">確</span>高設定確定/有力</span>'
                  '</div>')
        h = f'<h3 class="article-title">{esc(title)}</h3>{legend}'
        wide = " settei-table--wide" if (tables and any(t.get("wide") for t in tables)) else ""
        if tables:
            for tbl in tables:
                h += f'<p class="settei-sub-label">{esc(tbl.get("label",""))}</p>'
                headers = "".join(f"<th>{esc(hh)}</th>" for hh in tbl.get("headers", []))
                h += f'<table class="settei-table{wide}"><tr>{headers}</tr>'
                for row in tbl.get("rows", []):
                    # 行の全セルを出力（旧実装は2セル固定で、4列表のREG・合算列が消えていた・2026-07-13修正）
                    cells = row if isinstance(row, list) else [row]
                    tds = []
                    for c in cells:
                        if isinstance(c, dict):
                            tds.append(f'<td><span class="settei-badge {BADGE_CLASS.get(c.get("badge",""), "")}">{esc(c.get("text",""))}</span></td>')
                        else:
                            tds.append(f"<td>{esc(c)}</td>")
                    h += "<tr>" + "".join(tds) + "</tr>"
                h += "</table>"
                if tbl.get("note"):
                    h += f'<p class="settei-note">{esc(tbl["note"])}</p>'
        elif section.get("rows"):
            h += '<table class="settei-table"><tr><th>要素</th><th>示唆</th></tr>'
            for row in section["rows"]:
                if isinstance(row, list):
                    c0, c1 = (row + ["", ""])[:2]
                else:
                    c0, c1 = row.get("trigger", ""), row.get("hint", "")
                if isinstance(c1, dict):
                    badge = f'<span class="settei-badge {BADGE_CLASS.get(c1.get("badge",""), "")}">{esc(c1.get("text",""))}</span>'
                else:
                    badge = esc(c1)
                h += f"<tr><td>{esc(c0)}</td><td>{badge}</td></tr>"
            h += "</table>"
        return f'<div class="article-item"{section_attrs(section)}>{h}</div>'

    # default
    paras = "".join(f'<p class="article-body">{md(t)}</p>' for t in body)
    return (f'<div class="article-item"{section_attrs(section)}>'
            f'<h3 class="article-title">{esc(title)}</h3>{paras}</div>')


def build_jsonld(machine: dict, canonical_url: str, title: str, desc: str) -> str:
    """Article + BreadcrumbList のJSON-LDを静的HTMLへ焼き込む（meta-auto.jsは既存があればスキップする）。"""
    name = machine.get("name", "")
    article = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": title,
        "description": desc,
        "image": "https://uchidokoro.com/assets/img/ogp.png",
    }
    # 日付は出力しない（2026-07-12・チャッピーレビュー反映）：
    # release_date=機種の導入日であり「記事の公開日」ではない。正確な記事公開日を持たないため
    # datePublishedは省略（schema.org/Google仕様上いずれも任意）。導入日は本文・factTable側の情報として扱う。
    # dateModifiedも毎日の全機種再ビルドで日付が動き信頼性が下がるため出力しない
    article.update({
            "author": {"@type": "Organization", "name": "うちどころ。", "url": "https://uchidokoro.com"},
            "publisher": {"@type": "Organization", "name": "うちどころ。", "url": "https://uchidokoro.com",
                          "logo": {"@type": "ImageObject", "url": "https://uchidokoro.com/assets/img/ogp.png"}},
            "mainEntityOfPage": {"@type": "WebPage", "@id": canonical_url},
    })
    ld = [
        article,
        {
            "@context": "https://schema.org",
            "@type": "BreadcrumbList",
            "itemListElement": [
                {"@type": "ListItem", "position": 1, "name": "うちどころ。", "item": "https://uchidokoro.com/"},
                {"@type": "ListItem", "position": 2, "name": name, "item": canonical_url},
            ],
        },
    ]
    payload = json.dumps(ld, ensure_ascii=False).replace("</", "<\\/")
    return f'<script type="application/ld+json">{payload}</script>'


def claim_gate_state():
    """出典の裏取りゲートが有効か。★設定が読めなければ止める★

    ★★ここが最大の抜け道だった★★（Codex 9巡目 (a)-6）
      公開物の生成（build_public_data.py）にはゲートを付けたのに、
      **実際に読者が見るHTMLを作るのはこのスクリプト**で、
      authoring の machines.json / machine-details を直接読んでいた。
      つまり誤った数値を書いて本スクリプトを回せば、
      公開ゲートを一度も通らずに静的HTMLへ入っていた。
    """
    import sys as _sys
    _sys.path.insert(0, str(BASE / "scripts"))
    import build_public_data as bpd
    return bpd.claim_gate_enabled()


PLACEHOLDER_HTML = """<!doctype html>
<html lang="ja"><head>
<base href="/">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>準備中 | うちどころ。</title>
<link rel="stylesheet" href="assets/css/practical.css">
</head><body>
<main class="wrap">
<h1>準備中です</h1>
<p>このページの数値は出典の確認が済んでいないため、いまは公開していません。
確認ができ次第あらためて掲載します。</p>
<p><a href="index.html">トップページへ戻る</a></p>
</main>
</body></html>
"""


class TemplateError(RuntimeError):
    """テンプレートと生成器の食い違い（差し込み先が無い／複数ある）。"""


def replace_once(text: str, needle: str, repl: str) -> str:
    """★差し込み先がちょうど1つあることを確かめてから置き換える★

    （2026-07-30・Codex 15巡目 (a)-1）
      これまでは `replace(..., 1)` だったので、テンプレートの目印が
      無くなっていても**黙って何もせず**、テンプレートの固定文がそのまま
      全機種のページに残った。生成器と検査器が同じテンプレートを読むため、
      作り直して比べても一致してしまう（共通原因の故障）。
      差し込み先が0個でも2個以上でも止める。
    """
    n = text.count(needle)
    if n != 1:
        raise TemplateError(
            f"テンプレートの差し込み先が {n} 箇所です（1箇所であるべき）: {needle[:60]}")
    return text.replace(needle, repl, 1)


def prepare_template(template: str) -> str:
    """テンプレートを機種ページ用に整える（1回だけ行う前処理）。"""
    if "<base " not in template:
        template = re.sub(r"(<head[^>]*>)", r'\1\n<base href="/">', template, count=1)
    # テンプレ由来の robots meta（machine.html自体のnoindex）を除去。
    # complete機種はnoindex無し(index)、preview機種は下で noindex,follow を再付与する
    return re.sub(r'<meta name="robots"[^>]*>(<!--.*?-->)?\n?', "", template)


def render_page(template: str, machine: dict, detail: dict | None,
                pochipochi_reasons: dict, pochipochi_public: bool = True) -> str:
    """1機種ぶんのHTMLを作る。★入力（machine / detail）だけで出力が決まる★

    （2026-07-30・Codex 14巡目 (a)-1）
      成果物の検査は「この関数に公開データを入れ直した結果と1バイトも違わないか」で行う。
      そのため、この関数は外部の状態を読まない（引数だけで決まる）必要がある。
    """
    slug = machine["slug"]
    html_out = template
    canonical_url = f"https://uchidokoro.com/machines/{slug}/"

    # canonical（★個数を数えてから置き換える★・Codex 16巡目 (a)-5）
    canon_pat = re.compile(r'<link\s+rel="canonical"[^>]*>')
    n_canon = len(canon_pat.findall(html_out))
    if n_canon > 1:
        raise TemplateError(f"canonical が {n_canon} 箇所あります（1箇所であるべき）")
    if n_canon == 1:
        html_out = canon_pat.sub(f'<link rel="canonical" href="{canonical_url}">', html_out, count=1)
    else:
        html_out = replace_once(html_out, "</head>", f'<link rel="canonical" href="{canonical_url}">\n</head>')

    # title / description（meta-auto.js 同等・ポチポチくん対応表記は非対応機種で外す）
    # ★公開版では「ポチポチくん対応」と名乗らない★（Codex 14巡目 (a)-3）
    #   成果物の setting.html は準備中ページに差し替えているので、
    #   対応と書けばSEO文言もリンクも実態と食い違う。
    if not pochipochi_public:
        pp_available, pp_reason = False, "準備中"
    elif _pd.machine_class(machine) != "LEGACY_COMPLETE":
        # preview と新台経路(AUTO_*)は設定判別データが無い＝対応と名乗らない
        pp_available, pp_reason = False, "解析データ判明後に対応"
    elif slug in pochipochi_reasons:
        pp_available, pp_reason = False, pochipochi_reasons[slug]
    else:
        pp_available, pp_reason = True, ""
    title, desc = build_title_desc(machine, pp_available)
    html_out = replace_once(html_out, "<title>機種ページ | うちどころ。</title>",
                                f"<title>{esc(title)}</title>")
    html_out = replace_once(html_out, 
        '<meta name="description" content="機種ごとの狙い目記事ページです。結論と要点をスマホ向けに表示します。">',
        f'<meta name="description" content="{esc(desc)}">')

    # OGP（SNSシェア用・meta-auto.js も後で更新するが静的にも焼く）
    html_out = replace_once(html_out, 
        '<meta property="og:title" content="機種ページ | うちどころ。">',
        f'<meta property="og:title" content="{esc(title)}">')
    html_out = replace_once(html_out, 
        '<meta property="og:description" content="機種ごとの狙い目記事ページです。結論と要点をスマホ向けに表示します。">',
        f'<meta property="og:description" content="{esc(desc)}">')
    html_out = replace_once(html_out, 
        '<meta property="og:url" content="https://uchidokoro.com/machine.html">',
        f'<meta property="og:url" content="{canonical_url}">')

    # Twitter Card（meta-auto.js はプリレンダ済みで上書きしないため静的に焼く）
    html_out = replace_once(html_out, 
        '<meta name="twitter:card" content="summary_large_image">',
        '<meta name="twitter:card" content="summary_large_image">\n'
        f'<meta name="twitter:title" content="{esc(title)}">\n'
        f'<meta name="twitter:description" content="{esc(desc)}">\n'
        '<meta name="twitter:site" content="@uchidokoro">\n'
        '<meta name="twitter:image" content="https://uchidokoro.com/assets/img/ogp.png">')

    # ポチポチくん導線：非対応機種は初期HTML段階でリンクを無効化して焼く
    # （JS実行前・JS無効・クローラーに「対応機能あり」と誤認させない。inline styleは使わずclassで）
    if not pp_available:
        for anchor_id, cls in (("settingHeroLink", "btn-settei btn-settei--wide"),
                               ("settingToolLink", "btn-show-all btn-show-all--center")):
            pat = re.compile(r'<a id="' + anchor_id + r'"[^>]*>小役カウンター ポチポチくん →</a>')
            n = len(pat.findall(html_out))
            if n != 1:      # ★0件を黙って通さない★（Codex 16巡目 (a)-5）
                raise TemplateError(
                    f"ポチポチくん導線 {anchor_id} が {n} 箇所です（1箇所であるべき）")
            html_out = pat.sub(
                f'<a id="{anchor_id}" class="{cls} is-disabled" aria-disabled="true" '
                f'title="{esc(pp_reason)}">小役カウンター ポチポチくん（{esc(pp_reason)}）</a>',
                html_out, count=1)

    # h1 機種名
    html_out = replace_once(html_out, 
        '<h1 id="machineTitle" class="page-title">機種名</h1>',
        f'<h1 id="machineTitle" class="page-title">{esc(machine["name"])}</h1>')

    # ★「当サイトの目安です」の併記を、必要な表示面すべてに出す★
    #   （Codex 13巡目 (b)-4 / 14巡目 (a)-8）
    #   以前は hero の1箇所だけで、`surfaces` を読んでいなかった。
    html_out = insert_disclaimer(html_out, machine)

    # JSON-LD（Article + BreadcrumbList）を静的に焼き込み
    html_out = replace_once(html_out, 
        "</head>", build_jsonld(machine, canonical_url, title, desc) + "\n</head>")

    # noindex の付与は区分で決める（2026-08-04・Codex71〜72回目）:
    #   LEGACY_PREVIEW / AUTO_PENDING = noindex,follow ／
    #   LEGACY_COMPLETE / AUTO_INDEXABLE = 付けない。
    #   ★緊急overrideはここで読まない★（render_page は「引数だけで出力が決まる」
    #     契約。override は判定書を作る側＝page_decision.decide が適用済み）
    if _pd.machine_class(machine) in ("LEGACY_PREVIEW", "AUTO_PENDING"):
        html_out = replace_once(html_out,
            "</head>", '<meta name="robots" content="noindex,follow">\n</head>')
    # ★2026-07-24: AdSenseローダーの注入を全機種で停止（Phase 0・止血）★
    #   承認ゲート（ads = public && index && page_review approved && content_hash一致）の
    #   実装後、承認済みページだけで再開する。無条件注入に戻さないこと。

    # ★本文が空でも差し込み先の個数は確かめる★（Codex 16巡目 (a)-5）
    #   先行記事（記事が空）のときに早期returnしていたので、
    #   本文まわりの目印が消されていても気づけなかった。
    for anchor in ('<p id="heroSub" class="hero-sub"></p>',
                   '<div id="articleSections"></div>',
                   '<tbody id="infoTableBody"></tbody>',
                   '<table id="summaryGrid" class="summary-grid"></table>'):
        n = html_out.count(anchor)
        if n != 1:
            raise TemplateError(f"本文の差し込み先が {n} 箇所です（1箇所であるべき）: {anchor}")

    if not isinstance(detail, dict) or not detail:
        return html_out

    lead = detail.get("lead", "") or ""
    if lead:
        html_out = replace_once(html_out, 
            '<p id="heroSub" class="hero-sub"></p>',
            f'<p id="heroSub" class="hero-sub">{esc(lead)}</p>')
    sections = detail.get("sections") or []
    if sections:
        sections_html = "".join(render_section(s) for s in sections)
        html_out = replace_once(html_out, 
            '<div id="articleSections"></div>',
            f'<div id="articleSections">{sections_html}</div>')
    fact = detail.get("factTable") or [["機種名", machine["name"]]]
    rows_html = "".join(f"<tr><th>{esc(r[0])}</th><td>{esc(r[1])}</td></tr>"
                        for r in fact if len(r) >= 2)
    html_out = replace_once(html_out, 
        '<tbody id="infoTableBody"></tbody>',
        f'<tbody id="infoTableBody">{rows_html}</tbody>')
    # summaryBoxes をプリレンダ（machine.html の renderSummaryGrid と同じ2列組み）
    summary_boxes = detail.get("summaryBoxes") or [
        {"label": "天井", "value": machine.get("strategy") or "-"},
        {"label": "ヤメ時", "value": "-"},
    ]
    srows = ""
    for i in range(0, len(summary_boxes), 2):
        a = summary_boxes[i]
        cell_a = (f'<span class="s-label">{esc(a.get("label",""))}</span>'
                  f'<span class="s-value">{esc(a.get("value",""))}</span>')
        if i + 1 < len(summary_boxes):
            b = summary_boxes[i + 1]
            cell_b = (f'<span class="s-label">{esc(b.get("label",""))}</span>'
                      f'<span class="s-value">{esc(b.get("value",""))}</span>')
            srows += f"<tr><td>{cell_a}</td><td>{cell_b}</td></tr>"
        else:
            srows += f"<tr><td>{cell_a}</td><td></td></tr>"
    html_out = replace_once(html_out, 
        '<table id="summaryGrid" class="summary-grid"></table>',
        f'<table id="summaryGrid" class="summary-grid">{srows}</table>')
    return html_out


# 「目安です」を出す位置（公開データの surfaces → 差し込む目印）
# ★実際のテンプレートに存在する文字列を使うこと★（Codex 15巡目 (b)-2）
#   `<section id="checkerCard"` は machine.html に存在せず、
#   checker 面が必要な最初の機種で必ず止まる状態だった。
DISCLAIMER_ANCHORS = {
    "hero": '<p id="heroSub"',
    "checker": '<div class="checker-card">',
    "detail.sections": '<div id="articleSections">',
}
# surfaces の名前 → 実際に置く場所（machine 側の面はすべて hero にまとめる）
SURFACE_TO_ANCHOR = {
    "checker": "checker",
    "detail.sections": "detail.sections",
}


def disclaimer_of(machine: dict) -> tuple[str | None, list]:
    req = machine.get("display_requirements")
    text = req.get("disclaimer") if isinstance(req, dict) else None
    surfaces = req.get("surfaces") if isinstance(req, dict) else None
    if not isinstance(text, str) or not text.strip():
        d = machine.get("disclaimer")
        text = d if isinstance(d, str) and d.strip() else None
    return text, (surfaces if isinstance(surfaces, list) else [])


def disclaimer_anchors(machine: dict) -> list:
    """この機種で「目安です」を置くべき場所（重複なし・順序固定）。"""
    text, surfaces = disclaimer_of(machine)
    if not text:
        return []
    spots = ["hero"]      # 機種の要約は必ず hero に付ける
    for s in surfaces:
        a = SURFACE_TO_ANCHOR.get(s)
        if a and a not in spots:
            spots.append(a)
    return spots


def insert_disclaimer(html_out: str, machine: dict) -> str:
    text, _ = disclaimer_of(machine)
    if not text:
        return html_out
    block = f'<p class="site-disclaimer">{esc(text)}</p>\n'
    for spot in disclaimer_anchors(machine):
        anchor = DISCLAIMER_ANCHORS[spot]
        if anchor not in html_out:
            # ★置けない面があったら黙って諦めない★（Codex 14巡目 (a)-8）
            raise RuntimeError(
                f"{machine.get('slug','?')}: 「{text}」を {spot} に置けません"
                f"（テンプレートに {anchor} が無い）")
        html_out = replace_once(html_out, anchor, block + anchor)
    return html_out


def render_all(source_root: Path) -> tuple[dict, list, list]:
    """公開用の機種ページを**描くだけ**（書き込みはしない）。

    ★条件7の設計★（2026-07-30・Codex 23巡目）
      公開HTMLを書けるのは build_pages_artifact.py だけにするため、
      ここは `{相対パス: HTML}` を返す純粋な描画関数にする。

    source_root : 公開データ（assets/data/public/）を含む作業ツリー
    戻り値      : (作ったページ, 準備中に置き換える機種, 作れなかった機種)
    """
    sys.path.insert(0, str(BASE / "scripts"))
    import safe_json as _sj

    pub_dir = source_root / "assets" / "data" / "public"
    pub_file = pub_dir / "machines.public.json"
    pub_details = pub_dir / "machine-details"
    if not pub_file.is_file() or not pub_details.is_dir():
        raise RuntimeError(f"公開データがありません: {pub_file}")

    machines_all = _sj.read_rows(source_root / "assets" / "data" / "machines.json")
    pub_rows = _sj.read_rows(pub_file)
    public_by_slug = {r["slug"]: r for r in pub_rows
                      if isinstance(r.get("slug"), str) and r["slug"]}

    template = prepare_template((source_root / "machine.html").read_text(encoding="utf-8"))
    reasons = extract_pochipochi_reasons(template)

    pages: dict = {}
    blocked: list = []
    broken: list = []
    # ★行番号は enumerate で数える★（Codex 26巡目 (b)-2）
    #   len(pages)+len(blocked) は二重計上で、先頭行が「0番目」になっていた。
    for row_no, m in enumerate(machines_all, start=1):
        slug = m.get("slug")
        if not isinstance(slug, str) or not slug:
            # ★行の中身は出さない★（同 (a)-4）未公開の見出しをキーに入れられる。
            broken.append(f"{row_no}行目: slug の型が不正")
            continue
        if slug not in public_by_slug:
            blocked.append(slug)
            pages[f"machines/{slug}/index.html"] = PLACEHOLDER_HTML
            continue
        # ★1件壊れても残りを列挙する★（同 (b)-3）
        #   読み込みと描画を同じ try に入れ、その機種だけ失敗させる。
        try:
            detail = _sj.read_json(pub_details / f"{slug}.json",
                                   expect=dict, allow_missing=True, default=None)
            pages[f"machines/{slug}/index.html"] = render_page(
                template, public_by_slug[slug], detail, reasons, pochipochi_public=False)
        except Exception as e:
            broken.append(f"{row_no}行目 {slug}: {type(e).__name__}")
    return pages, blocked, broken


# ★旧形式ページの目印★（この文言が全ページに入っていることを機械的に確かめる）
#   machine.html のテンプレートに直接書いてある。ここと食い違ったら生成を止める。
LEGACY_NOTE = ("数値は各種解析情報をもとにまとめた当サイトの整理です。"
               "出典の照合は順次進めています。")


def _rebuild_auto(slug: str) -> int:
    """★新台経路のページを1枚だけ描き直す★（2026-08-21・台帳#434）

    ★なぜ要るのか★
      `--legacy` は「公開中の危険な記述を消す手段が無いのは本末転倒」
      という理由で作られた。★新台経路には、その手段が無いままだった★。
      実際 garei_zero_re は、記事データから型式名を消したのに
      公開HTMLには残り続けていた（CLAUDE.md「型式名は記事に書かない」に反する）。

    ★抜け道にしないための線★
      ①**1機種ずつしか描き直せない**（一括再生成にならない）
        ＝もとの拒否が守っていたのは「翌朝の一括再生成が noindex を剥がす」こと。
      ②描くのは `publish_new_machine.render()`
        ＝★そのページを最初に作ったのと同じ関数★（別の描き方を持ち込まない）
      ③書く前に `publish_new_machine.check_page()` を通す
      ④★区分が動いていたら書かない★
        ＝いまのページの noindex の有無と、判定書の区分が食い違ったら断る。
        （2026-08-21・Codexの再指摘。★直す前は「いまが新台経路か」しか
          見ておらず、判定書だけ変えれば noindex を外せた★）
      ⑤★noindex が消えていたら書かない★
      ⑥★記事データ・機種データ・値の検査も通す★
        （check_detail / check_machine / check_only_allowed_values /
          check_page に detail を渡す）
        ＝★直す前は check_page しか通していなかった★ので、
          記事データを変えてから実行すればそのままHTMLへ届けられた。
      ⑦★公開ロックを通す★＝夜の公開処理と同時に書かない
      ⑧公開データ（assets/data/public/）は読まない
        ＝裏取り済みとして公開する経路には決してならない
    """
    sys.path.insert(0, str(BASE / "scripts"))
    import publish_new_machine as _pub
    import safe_json as _sj3

    rows = _sj3.read_rows(BASE / "assets" / "data" / "machines.json")
    hit = [m for m in rows if m.get("slug") == slug]
    if len(hit) != 1:
        print(f"★{slug} が machines.json に {len(hit)} 件です（1件でないと触りません）★")
        return 1
    machine = hit[0]
    cls = _pd.machine_class(machine)
    if cls not in ("AUTO_INDEXABLE", "AUTO_PENDING"):
        print(f"★{slug} は新台経路の機種ではありません（{cls}）。"
              "旧形式なら --legacy を使ってください★")
        return 1

    dpath = BASE / "assets" / "data" / "machine-details" / f"{slug}.json"
    if not dpath.is_file():
        print(f"★{slug} の記事データがありません★")
        return 1
    detail = _sj3.read_json(dpath, expect=dict)

    out = BASE / "machines" / slug / "index.html"
    if not out.is_file():
        print(f"★{slug} の公開ページがありません（新しく作る経路ではありません）★")
        return 1
    before = out.read_text(encoding="utf-8")

    # ★★いまのページと同じ区分でしか描き直さない★★
    #   （2026-08-21・Codexの再指摘）
    #   ★直す前の穴★＝「いまが新台経路か」しか見ていなかったので、
    #     ①既存HTMLは AUTO_PENDING（noindex）
    #     ②machines.json の判定書が AUTO_INDEXABLE に変わる
    #     ③--rebuild-auto を実行
    #   とすると★noindex を外したHTMLを書けた★＝
    #   区分を上げる正しい経路（公開判定）を迂回できた。
    #   ★この経路は「誤りを消す」ためのもの★なので、
    #   区分を動かすのは仕事ではない。動いていたら断る。
    was_noindex = ("noindex" in before)
    want_noindex = (cls == "AUTO_PENDING")
    if was_noindex != want_noindex:
        print(f"★{slug} は区分が動いています"
              f"（いまのページ: {'noindex あり' if was_noindex else 'noindex なし'}"
              f" ／ 判定書: {cls}）。"
              "この経路では区分を変えません★")
        print("  区分を変えるのは公開判定の仕事です"
              "（apply_indexing_policy / 新台の公開経路）")
        return 1

    html = _pub.render(slug, machine, detail)

    # ★書く前に確かめる★
    if want_noindex and "noindex" not in html:
        print("★描き直したページに noindex がありません。書きません★")
        return 1
    if not want_noindex and "noindex" in html:
        print("★検索に載せる区分なのに noindex が入っています。書きません★")
        return 1
    # ★★記事データと機種データも検査する★★（2026-08-21・Codexの再指摘）
    #   ★直す前は check_page しか通していなかった★＝
    #   記事データを変えてから実行すれば、その変更をそのままHTMLへ届けられた。
    problems = []
    problems += _pub.check_detail(slug, detail)
    problems += _pub.check_machine(slug, machine)
    problems += _pub.check_only_allowed_values(slug, machine, detail, html)
    problems += _pub.check_page(slug, html, expect_noindex=want_noindex,
                                detail=detail)
    if problems:
        print("★描き直したページが検査を通りません。書きません★")
        for x in problems[:5]:
            print("  ✗ " + str(x)[:120])
        return 1

    if html == before:
        print(f"{slug}: 変わりません（描き直す必要がありませんでした）")
        return 0

    # ★公開ロックを通す★（2026-08-21・Codexの再指摘）
    #   夜の公開処理と同時に走ると、同じページを2つの処理が書きうる。
    with _pub._OnlyOne():
        tmp = out.with_suffix(".html.tmp")
        tmp.write_text(html, encoding="utf-8", newline="")
        os.replace(tmp, out)
    print(f"{slug}: 描き直しました（{len(before)} → {len(html)} 字・区分 {cls}）")
    return 0


def _build_legacy(only_slug: str | None = None) -> int:
    """★いま公開中の旧形式ページを作り直す★（2026-07-30・Codex「これだけはやれ」③）

    ここは `LEGACY_UNVERIFIED` だけを作る経路。裏取り済みとして公開する道には
    決してならないよう、次の3つで縛る（詳細は main() の docstring）。
      1. 裏取りゲートが**有効なら実行しない**
      2. 公開データ（assets/data/public/）を一切読まない
      3. 全ページに旧形式の目印が入っていることを確かめ、
         1枚でも欠けたら**1枚も書かずに**終わる
    """
    import safe_json as _sj2
    import preview_site as _pv

    # 1) ゲートが有効なら、旧形式の作り直しは筋違い（正しい経路を使う）
    try:
        if claim_gate_state():
            print("★裏取りゲートが有効なので、旧形式の作り直しはできません★")
            print("  公開物は build_pages_artifact.py が組み立てます。")
            return 1
    except Exception as e:
        # ★設定が読めないときは書かない★（fail-closed）
        print(f"★裏取りゲートの設定が読めません: {e} → 何も書きません")
        return 1

    machines = _sj2.read_rows(BASE / "assets" / "data" / "machines.json")
    # ★新台経路（page-decision/v1）の機種を旧statusロジックで再生成しない★
    #   （2026-08-04・Codex72回目。ここで除外しないと、翌朝の一括再生成が
    #     AUTO_PENDING の noindex を剥がし、AUTO_INDEXABLE に旧タイトルを焼く）
    # ★区分は machine_class で判定する★（壊れた判定書は例外で止まる＝
    #   「AUTOだから除外」で成功扱いにしない・Codex73回目の指摘6）
    auto_slugs = [m["slug"] for m in machines
                  if _pd.machine_class(m) in ("AUTO_INDEXABLE", "AUTO_PENDING")]
    if only_slug and only_slug in auto_slugs:
        print(f"★{only_slug} は新台経路（page-decision/v1）の機種です。"
              "この経路（--legacy）では作り直せません★")
        return 1
    if auto_slugs:
        print(f"新台経路の機種 {len(auto_slugs)} 件はこの経路では触りません: "
              + ", ".join(auto_slugs[:5])
              + (" ほか" if len(auto_slugs) > 5 else ""))
        machines = [m for m in machines if m["slug"] not in set(auto_slugs)]
    # ★1機種だけ直せるようにする★（2026-07-30・Codex指摘6）
    #   全機種を書き直す作りだったので、
    #     ①更新タスクの「1日1機種」が実際には全機種だった
    #     ②新台タスクが machines.json に行を足した翌朝、そのページはまだ無いので
    #       「未公開ページを作ろうとした」と**全件が中止**していた
    #   ＝2本に分けたことで互いを止め合う経路になっていた。
    if only_slug:
        machines = [m for m in machines if m.get("slug") == only_slug]
        if not machines:
            print(f"★machines.json に {only_slug} がありません★")
            return 1
        if not (BASE / "machines" / only_slug / "index.html").is_file():
            print(f"★{only_slug} はまだ公開していません（ここでは作れません）★")
            return 1
    template = prepare_template((BASE / "machine.html").read_text(encoding="utf-8"))

    # 2) authoring の記事だけを読む（公開データは触らない）
    detail_dir = BASE / "assets" / "data" / "machine-details"
    pochipochi_reasons = extract_pochipochi_reasons(template)

    pages: dict = {}
    broken: list = []
    for machine in machines:
        slug = machine["slug"]
        detail = None
        dp = detail_dir / f"{slug}.json"
        if dp.is_file():
            try:
                detail = _sj2.read_json(dp, expect=dict)
            except Exception as e:
                print(f"★記事データが読めません: {slug}: {type(e).__name__}: {e}")
                broken.append(slug)
                continue
        try:
            pages[f"machines/{slug}/index.html"] = render_page(
                template, machine, detail, pochipochi_reasons, True)
        except Exception as e:
            print(f"★ページを作れません: {slug}: {type(e).__name__}: {e}")
            broken.append(slug)

    if broken:
        print(f"★{len(broken)} 機種が作れませんでした → 1枚も書きません: {broken}")
        return 1
    if not pages:
        print("★1機種も作れませんでした★")
        return 1

    # 3) 旧形式の目印が全ページにあること（無ければ1枚も書かない）
    missing = [rel for rel, h in pages.items() if LEGACY_NOTE not in h]
    if missing:
        print(f"★旧形式の目印が {len(missing)} ページに入っていません → 1枚も書きません")
        for rel in missing[:5]:
            print(f"  ✗ {rel}")
        print("  machine.html の文言と LEGACY_NOTE を一致させてください:")
        print("  " + LEGACY_NOTE)
        return 1

    # 目印が「本当に表示される場所」にあることも確かめる（コメント内だけは不可）
    hidden = [rel for rel, h in pages.items()
              if LEGACY_NOTE not in _pv.strip_html_comments(h)]
    if hidden:
        print(f"★目印がコメント内にしかないページが {len(hidden)} 枚 → 1枚も書きません")
        return 1

    # 4) ★すでに公開しているページだけを対象にする★（Codex指摘1・2026-07-30）
    #   目印があることは「未照合と書いてある」ことしか保証しない。
    #   新しい slug を machines.json に足せば、この経路で**新規ページも作れた**。
    #   新台の公開は裏取りを通った経路（build_pages_artifact）の仕事なので、
    #   ここは「いま公開しているページを直す」ことだけに限る。
    existing = {rel for rel in pages if (BASE / rel).is_file()}
    new_pages = sorted(set(pages) - existing)
    if new_pages:
        print(f"★まだ公開していないページを {len(new_pages)} 件作ろうとしました → 中止")
        for rel in new_pages[:5]:
            print(f"  ✗ {rel}")
        print("  新しい機種の公開は裏取りを通る経路の仕事です（ここでは作れません）")
        return 1

    # 5) ★いったん別の場所に書いて、全部そろってから置き換える★
    #   途中で失敗したときに「半分だけ新しいページ」が残らないようにする。
    tmp = BASE / "_legacy.next"
    if tmp.exists():
        shutil.rmtree(tmp)
    try:
        for rel, h in pages.items():
            out = tmp / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(h, encoding="utf-8", newline=chr(10))
        # 書いた物をもう一度読み直して、目印が本当に入っているか確かめる
        for rel in pages:
            got = (tmp / rel).read_text(encoding="utf-8")
            if LEGACY_NOTE not in _pv.strip_html_comments(got):
                print(f"★書き出した物に目印がありません: {rel} → 1枚も置き換えません")
                return 1
        for rel in pages:
            out = BASE / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(tmp / rel, out)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)
    print(f"旧形式ページを作り直しました: {len(pages)} 機種")
    print("  （全ページに旧形式の目印つき・裏取り済みとしては公開していません）")
    return 0


def main(preview: bool = False, legacy: bool = False,
         legacy_slug: str | None = None):
    """preview=True のときは .preview-site/ にだけ書く（公開されない写し）。

    ★2026-07-30・移行手順2で --allow-ungated を廃止した★
      以前は「裏取りゲートが無効でも、承知のうえなら本番のHTMLを上書きしてよい」
      という抜け道があった。フラグ1つで公開物が書けてしまうので廃止し、
      裏取り前の内容は **公開されない写し（.preview-site/）にしか出せない** ようにした。

    ★legacy=True＝旧形式ページの作り直し★（2026-07-30・Codex「これだけはやれ」③）
      --allow-ungated を廃止した結果、**いま公開中の旧形式ページを直す手段が
      無くなった**（記事本文はHTMLに焼き込まれていて、JSONを直しても届かない）。
      東京喰種の矛盾のような、公開中の危険な記述を消せないのは本末転倒。

      ただし --allow-ungated の復活ではない。向きが逆で、抜け道にならない:
        - `--allow-ungated`：ゲートが**無効でも**公開物を書けた（ゲート回避）
        - `--legacy`       ：ゲートが**有効になったら使えない**（旧形式専用）
      さらに
        - 公開データ（assets/data/public/）は一切読まない
          ＝裏取り済みとして公開する経路には決してならない
        - 全ページに旧形式の目印（LEGACY_NOTE）が入っていることを確認し、
          1枚でも欠けたら**何も書かずに止める**（fail-closed）
    """
    sys.path.insert(0, str(BASE / "scripts"))
    import preview_site as _pv

    if legacy and preview:
        # ★--preview --legacy で本番パスへ書けていた★（Codex指摘1・2026-07-30）
        #   legacy を先に見ていたので、preview を付けても公開パスに書いていた。
        print("★--preview と --legacy は同時に使えません★")
        print("  写しを見るなら --preview だけ、公開ページを直すなら --legacy だけ。")
        return 1
    if legacy:
        return _build_legacy(legacy_slug)

    # ★公開物を書けるのは build_pages_artifact.py だけ★
    #   （2026-07-30・Codex 23巡目 条件7の設計）
    #   ここは「描くだけ」にし、公開用の書き込み口を持たない。
    #   写し（.preview-site/）への書き出しだけを残す。
    if not preview:
        print("★公開用のHTMLはここからは作れません★")
        print("  公開物は build_pages_artifact.py が組み立てます（render_all を呼びます）。")
        print("  裏取り前の内容を見たいだけなら --preview を付けてください。")
        return 1
    out_root = _pv.PREVIEW_DIR
    # 機種の一覧（＝ページを持ちうるslugの全体）は authoring から取る。
    # 公開が止まった機種にも「準備中」ページを置き換えるために必要。
    import safe_json as _sj2
    machines = _sj2.read_rows(BASE / "assets" / "data" / "machines.json")
    template = (BASE / "machine.html").read_text(encoding="utf-8")

    # ★★裏取りゲートが有効なら、通らない機種のHTMLは作らない★★
    try:
        gate_on = claim_gate_state()
    except Exception as e:
        # 写しは公開しないので、設定が読めなくても確認だけはできる
        if not preview:
            print(f"★出典の裏取りゲートの設定が読めません: {e}")
            return 1
        print(f"（写し）出典の裏取りゲートの設定が読めません: {e} — 全機種を写します")
        gate_on = False
    blocked_by_claim = {}
    detail_dir_override = None
    if preview:
        # 写しは「裏取り前の内容を見るため」のものなので、止めずに全機種を出す。
        # 代わりに全ページへ noindex・バナー・目印が入り、robots.txt は全面Disallow。
        _pv.ensure_scaffold()
        print(f"☆写しを作ります（公開されません）: {out_root.name}/ ☆")
    elif gate_on:
        import claim_reconcile as cr
        for m in machines:
            try:
                ok, why = cr.publish_gate(m["slug"])
            except Exception as e:
                ok, why = False, [f"検査が例外で失敗: {e}"]
            if not ok:
                blocked_by_claim[m["slug"]] = why
        # ★★公開用HTMLは「安全化を通した公開データ」からしか作らない★★
        #   （2026-07-30・Codex 13巡目 (a)-1）
        #   以前は authoring の machines.json / machine-details を直接読んでHTMLに焼いていた。
        #   ゲートで機種を止めても、**通った機種のページの中身は素通り**していた
        #   （例：射影では消えるはずの説明文が静的HTMLには残る）。
        #   公開データ（assets/data/public/）が無ければ作らない＝fail-closed。
        pub_dir = BASE / "assets" / "data" / "public"
        pub_file = pub_dir / "machines.public.json"
        pub_details = pub_dir / "machine-details"
        if not pub_file.is_file() or not pub_details.is_dir():
            print("★公開データがありません（先に build_public_data.py --apply を実行）★")
            print(f"  期待した場所: {pub_file}")
            return 1
        try:
            pub_rows = json.loads(pub_file.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"★公開データが読めません: {e}")
            return 1
        if not isinstance(pub_rows, list) or not all(isinstance(r, dict) for r in pub_rows):
            print("★公開データの形が想定と違います（機種の配列ではない）★")
            return 1
        public_by_slug = {r.get("slug"): r for r in pub_rows if isinstance(r.get("slug"), str)}
        # 公開データに無い機種も「準備中」に置き換える（古いページを残さない）
        for m in machines:
            if m["slug"] not in public_by_slug:
                blocked_by_claim.setdefault(m["slug"], ["公開データに含まれていない"])
        machines = [public_by_slug.get(m["slug"], m) for m in machines]
        detail_dir_override = pub_details
        print(f"出典の裏取りゲート: ★有効★ → {len(blocked_by_claim)} 機種は"
              f"noindexの準備中ページに置き換えます")
        # ★理由を捨てない★（Codex 10巡目 (b)-1）
        for slug, why in blocked_by_claim.items():
            for ln in (why or []):      # ★全理由を出す★（Codex 11巡目 (b)-1）
                print(f"  ✗ {slug}: {ln}")
    else:
        # ★★ゲート無効のまま既存HTMLを置き換えない★★（Codex 10巡目 (a)-1）
        #   「警告して書き込む」は条件7（enabled=falseなら既存成果物を置換しない）に反する。
        print("★出典の裏取りゲートが無効なので公開用のHTMLは作りません★")
        print("  assets/data/claim-gate.json の enabled を true にするか、")
        print("  裏取り前の内容を確かめたいなら --preview を付けてください")
        print("  （.preview-site/ にだけ書き出します。公開されません）")
        return 1

    template = prepare_template(template)
    # ポチポチくん非対応slug→理由（machine.htmlのpochipochiStatusと同期）
    pochipochi_reasons = extract_pochipochi_reasons(template)
    # ★公開版では setting.html を準備中に差し替えるので「対応」と名乗らない★
    pochipochi_public = bool(preview)

    # 公開時は公開データの記事、写しのときだけ authoring の記事
    detail_dir = detail_dir_override or (BASE / "assets" / "data" / "machine-details")
    broken_details: list = []
    generated_slugs: list = []
    generated = 0
    prerendered = 0
    for machine in machines:
        slug = machine["slug"]
        if slug in blocked_by_claim:
            # ★★「作らない」だけでは古い誤情報が残り続ける★★（Codex 10巡目 (a)-2）
            # ★ここは写し専用★（公開物は build_pages_artifact.py が書く・条件7）
            _pv.write_html(f"machines/{slug}/index.html", PLACEHOLDER_HTML)
            continue

        detail = None
        dp = detail_dir / f"{slug}.json"
        if dp.is_file():
            try:
                detail = _sj2.read_json(dp, expect=dict)
            except Exception as e:
                # ★★読めない記事を「本文なし」で公開しない★★（Codex 11巡目 (b)-4）
                print(f"★記事データが読めません: {slug} ({dp.name}) "
                      f"{type(e).__name__}: {e}")
                broken_details.append(slug)
                continue
            # ★空の記事（先行記事）は「本文を焼いた」に数えない★（Codex 16巡目 (b)-4）
            if isinstance(detail, dict) and detail:
                prerendered += 1

        try:
            html_out = render_page(template, machine, detail,
                                   pochipochi_reasons, pochipochi_public)
        except Exception as e:
            print(f"★ページを作れません: {slug}: {type(e).__name__}: {e}")
            broken_details.append(slug)
            continue

        # ★ここは写し専用★（公開物は build_pages_artifact.py が書く・条件7）
        _pv.write_html(f"machines/{slug}/index.html", html_out)
        generated += 1
        generated_slugs.append(slug)

    # ★★公開してよい機種の名簿を書き出す★★（Codex 11巡目 (a)-1/(a)-2）
    #   ブラウザ側（machine.html / setting.html / index.html）はこれを見て、
    #   名簿に無い機種は「準備中」と表示する＝汎用URLで中身を見せない。
    print(f"生成完了: {generated} 機種 / machines/{{slug}}/index.html（うち本文プリレンダ {prerendered} 機種）")
    # ★★1機種も作れなければ成功にしない★★（Codex 11巡目 (b)-5）
    if generated == 0:
        print("★1機種も生成できませんでした（全機種が止まっています）★")
        return 1
    # ★★失敗した回では名簿を更新しない★★（Codex 12巡目 (a)-4）
    #   以前は「ゲートで止まらなかった集合」から名簿を作り、
    #   記事の読み込みに失敗した機種も名簿に残したまま非0終了していた。
    #   ＝古いページ＋その機種を許可する名簿、という食い違いが残る。
    if broken_details:
        print(f"★記事データが読めない機種があります: {broken_details}★")
        print("　この回は公開名簿を更新しません（古いページと食い違うため）")
        return 1

    # ★名簿は「実際に生成できた機種」から作る★
    manifest = out_root / "assets" / "data" / "published-slugs.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    if preview:
        _pv.assert_inside(manifest)
    manifest.write_text(json.dumps(
        {"schema_version": "published-slugs/v1",
         "claim_gate_enabled": bool(gate_on),
         "slugs": sorted(generated_slugs)}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    print(f"公開名簿を書き出し: {len(generated_slugs)} 機種 → assets/data/published-slugs.json")


if __name__ == "__main__":
    # ★終了コードを落とさない★（Codex 10巡目 (b)-2）
    #   main() が 1 を返しても、プロセスは 0 で終わっていた＝呼び出し側が失敗に気づけない
    import argparse as _ap
    _p = _ap.ArgumentParser()
    _p.add_argument("--preview", action="store_true",
                    help="公開されない写し（.preview-site/）にだけ書き出す")
    _p.add_argument("--slug", default=None,
                    help="--legacy と併用。その1機種だけ作り直す（既定は全機種）")
    _p.add_argument("--legacy", action="store_true",
                    help="いま公開中の旧形式ページを作り直す"
                         "（裏取りゲートが有効なら実行しない・公開データは読まない）")
    _p.add_argument("--rebuild-auto", default=None, metavar="SLUG",
                    help="新台経路のページを1枚だけ描き直す"
                         "（公開中の誤りを消すため・区分とnoindexを確かめてから書く）")
    _a = _p.parse_args()
    # ★どんな壊れた入力でも traceback にしない★（Codex 閉鎖条件5・27巡目）
    import sys as _s9
    _s9.path.insert(0, str(BASE / "scripts"))
    import safe_json as _sj9
    try:
        if _a.rebuild_auto:
            if _a.preview or _a.legacy or _a.slug:
                print("★--rebuild-auto は他の指定と一緒には使えません★")
                raise SystemExit(1)
            raise SystemExit(_rebuild_auto(_a.rebuild_auto) or 0)
        raise SystemExit(main(_a.preview, _a.legacy, _a.slug) or 0)
    except SystemExit:
        raise
    except _sj9.SafeJsonError as _e:
        print(f"★入力データが読めません: {_e}★")
        print("  作業を中止しました（直してから再実行してください）")
        raise SystemExit(1)
    except Exception as _e:
        print(f"★想定外の失敗 {type(_e).__name__}: {_e}★")
        raise SystemExit(1)

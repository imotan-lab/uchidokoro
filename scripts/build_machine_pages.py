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
import re
import sys
from pathlib import Path

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
    is_preview = machine.get("status") == "preview"
    release_jp = jp_date(machine.get("release_date", ""))

    if is_preview:
        if release_jp:
            title = f"【先行】{name} {release_jp}導入｜天井・狙い目予想・解析判明次第更新"
            desc = f"{release_jp}導入予定の{name}の機種概要を先行公開。天井・狙い目・設定差などの解析データが判明次第、随時更新します。導入前から最新情報をチェック。"
        else:
            title = f"【先行】{name} 天井・狙い目予想｜解析判明次第更新"
            desc = f"{name}の機種概要を先行公開。天井・狙い目・設定差などの解析データが判明次第、随時更新します。導入前から最新情報をチェック。"
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
        return f'<div class="article-item">{inner}</div>'

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
        return f'<div class="article-item">{h}</div>'

    # default
    paras = "".join(f'<p class="article-body">{md(t)}</p>' for t in body)
    return f'<div class="article-item"><h3 class="article-title">{esc(title)}</h3>{paras}</div>'


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


def main(preview: bool = False):
    """preview=True のときは .preview-site/ にだけ書く（公開されない写し）。

    ★2026-07-30・移行手順2で --allow-ungated を廃止した★
      以前は「裏取りゲートが無効でも、承知のうえなら本番のHTMLを上書きしてよい」
      という抜け道があった。フラグ1つで公開物が書けてしまうので廃止し、
      裏取り前の内容は **公開されない写し（.preview-site/）にしか出せない** ようにした。
    """
    sys.path.insert(0, str(BASE / "scripts"))
    import preview_site as _pv

    out_root = _pv.PREVIEW_DIR if preview else BASE
    machines = json.loads((BASE / "assets" / "data" / "machines.json").read_text(encoding="utf-8"))
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

    # <base href="/"> を <head> 直後に挿入
    if "<base " not in template:
        template = re.sub(r"(<head[^>]*>)", r'\1\n<base href="/">', template, count=1)

    # テンプレ由来の robots meta（machine.html自体のnoindex）を除去。
    # complete機種はnoindex無し(index)、preview機種は下で noindex,follow を再付与する
    template = re.sub(r'<meta name="robots"[^>]*>(<!--.*?-->)?\n?', "", template)

    # ポチポチくん非対応slug→理由（machine.htmlのpochipochiStatusと同期）
    pochipochi_reasons = extract_pochipochi_reasons(template)

    detail_dir = BASE / "assets" / "data" / "machine-details"
    broken_details: list = []
    generated_slugs: list = []
    generated = 0
    prerendered = 0
    for machine in machines:
        slug = machine["slug"]
        if slug in blocked_by_claim:
            # ★★「作らない」だけでは古い誤情報が残り続ける★★（Codex 10巡目 (a)-2）
            #   既存の index.html を noindex の準備中ページに置き換える。
            out = out_root / "machines" / slug / "index.html"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(PLACEHOLDER_HTML, encoding="utf-8")
            continue
        html_out = template
        canonical_url = f"https://uchidokoro.com/machines/{slug}/"

        # canonical
        if 'rel="canonical"' in html_out:
            html_out = re.sub(r'<link\s+rel="canonical"[^>]*>',
                              f'<link rel="canonical" href="{canonical_url}">', html_out, count=1)
        else:
            html_out = html_out.replace("</head>", f'<link rel="canonical" href="{canonical_url}">\n</head>', 1)

        # title / description（meta-auto.js 同等・ポチポチくん対応表記は非対応機種で外す）
        if machine.get("status") == "preview":
            pp_available, pp_reason = False, "解析データ判明後に対応"
        elif slug in pochipochi_reasons:
            pp_available, pp_reason = False, pochipochi_reasons[slug]
        else:
            pp_available, pp_reason = True, ""
        title, desc = build_title_desc(machine, pp_available)
        html_out = html_out.replace("<title>機種ページ | うちどころ。</title>",
                                    f"<title>{esc(title)}</title>", 1)
        html_out = html_out.replace(
            '<meta name="description" content="機種ごとの狙い目記事ページです。結論と要点をスマホ向けに表示します。">',
            f'<meta name="description" content="{esc(desc)}">', 1)

        # OGP（SNSシェア用・meta-auto.js も後で更新するが静的にも焼く）
        html_out = html_out.replace(
            '<meta property="og:title" content="機種ページ | うちどころ。">',
            f'<meta property="og:title" content="{esc(title)}">', 1)
        html_out = html_out.replace(
            '<meta property="og:description" content="機種ごとの狙い目記事ページです。結論と要点をスマホ向けに表示します。">',
            f'<meta property="og:description" content="{esc(desc)}">', 1)
        html_out = html_out.replace(
            '<meta property="og:url" content="https://uchidokoro.com/machine.html">',
            f'<meta property="og:url" content="{canonical_url}">', 1)

        # Twitter Card（meta-auto.js はプリレンダ済みで上書きしないため静的に焼く）
        html_out = html_out.replace(
            '<meta name="twitter:card" content="summary_large_image">',
            '<meta name="twitter:card" content="summary_large_image">\n'
            f'<meta name="twitter:title" content="{esc(title)}">\n'
            f'<meta name="twitter:description" content="{esc(desc)}">\n'
            '<meta name="twitter:site" content="@uchidokoro">\n'
            '<meta name="twitter:image" content="https://uchidokoro.com/assets/img/ogp.png">', 1)

        # ポチポチくん導線：非対応機種は初期HTML段階でリンクを無効化して焼く
        # （JS実行前・JS無効・クローラーに「対応機能あり」と誤認させない。inline styleは使わずclassで）
        if not pp_available:
            for anchor_id, cls in (("settingHeroLink", "btn-settei btn-settei--wide"),
                                   ("settingToolLink", "btn-show-all btn-show-all--center")):
                html_out = re.sub(
                    r'<a id="' + anchor_id + r'"[^>]*>小役カウンター ポチポチくん →</a>',
                    f'<a id="{anchor_id}" class="{cls} is-disabled" aria-disabled="true" '
                    f'title="{esc(pp_reason)}">小役カウンター ポチポチくん（{esc(pp_reason)}）</a>',
                    html_out, count=1)

        # h1 機種名
        html_out = html_out.replace(
            '<h1 id="machineTitle" class="page-title">機種名</h1>',
            f'<h1 id="machineTitle" class="page-title">{esc(machine["name"])}</h1>', 1)

        # JSON-LD（Article + BreadcrumbList）を静的に焼き込み
        html_out = html_out.replace(
            "</head>", build_jsonld(machine, canonical_url, title, desc) + "\n</head>", 1)

        # 先行記事（preview）は完全記事へ昇格するまで noindex（恒久ポリシー・審査中だけの措置ではない）
        # 昇格時は auto-add が本スクリプトを再実行するため自動で index に戻る
        if machine.get("status") == "preview":
            html_out = html_out.replace(
                "</head>", '<meta name="robots" content="noindex,follow">\n</head>', 1)
        # ★2026-07-24: AdSenseローダーの注入を全機種で停止（Phase 0・止血）★
        #   理由: Google Publisher Policy は「人のレビュー/キュレーションが無い自動生成
        #   コンテンツ」への広告掲載を認めていない。現状は status=complete というだけで
        #   index・広告・チェッカーが一括で開く fail-open 設計であり、記事の検証状態を
        #   まったく見ていない。承認ゲート（ads = public && index && page_review approved
        #   && content_hash一致 …）の実装後、承認済みページだけで再開する。
        #   ここを戻すときは必ずゲート参照にすること（無条件注入に戻さない）。

        # 本文（lead / sections / factTable）をプリレンダ
        dp = detail_dir / f"{slug}.json"
        if dp.is_file():
            try:
                detail = json.loads(dp.read_text(encoding="utf-8"))
            except Exception as e:
                # ★★読めない記事を「本文なし」で公開しない★★（Codex 11巡目 (b)-4）
                #   握り潰すと、中身が欠けたページを正常生成として扱ってしまう。
                print(f"★記事データが読めません: {slug} ({dp.name}) "
                      f"{type(e).__name__}: {e}")
                broken_details.append(slug)
                continue
            lead = detail.get("lead", "") or ""
            if lead:
                html_out = html_out.replace(
                    '<p id="heroSub" class="hero-sub"></p>',
                    f'<p id="heroSub" class="hero-sub">{esc(lead)}</p>', 1)
            sections = detail.get("sections") or []
            if sections:
                sections_html = "".join(render_section(s) for s in sections)
                html_out = html_out.replace(
                    '<div id="articleSections"></div>',
                    f'<div id="articleSections">{sections_html}</div>', 1)
            fact = detail.get("factTable") or [["機種名", machine["name"]]]
            rows_html = "".join(f"<tr><th>{esc(r[0])}</th><td>{esc(r[1])}</td></tr>"
                                for r in fact if len(r) >= 2)
            html_out = html_out.replace(
                '<tbody id="infoTableBody"></tbody>',
                f'<tbody id="infoTableBody">{rows_html}</tbody>', 1)
            # summaryBoxes をプリレンダ（JS未実行・データ取得失敗時も要約欄が出るように）
            # machine.html の renderSummaryGrid と同じ2列組み。strategyByRate上書きは
            # 実行時JSが再描画するため、静的には既定のsummaryBoxesを焼く。
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
            html_out = html_out.replace(
                '<table id="summaryGrid" class="summary-grid"></table>',
                f'<table id="summaryGrid" class="summary-grid">{srows}</table>', 1)
            prerendered += 1

        if preview:
            # 写しは必ず noindex・バナー・目印つきで書く（外へ書けない仕組みも通す）
            _pv.write_html(f"machines/{slug}/index.html", html_out)
        else:
            out_dir = out_root / "machines" / slug
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "index.html").write_text(html_out, encoding="utf-8", newline="\n")
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
    _a = _p.parse_args()
    raise SystemExit(main(_a.preview) or 0)

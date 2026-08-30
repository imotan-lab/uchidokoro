"""
レンダリング後DOMの全機種検査（Playwrightベース）

audit_site.py（静的解析）では捕まえられないJS実行後の表示異常を検出する。
本番URL（https://uchidokoro.com）に対してヘッドレスChromeでアクセスし、
各機種ページの最終DOMをチェックする。

使い方:
    python scripts/audit_render.py [--slug <slug>]         # 1機種だけ確認
    python scripts/audit_render.py [--limit N]             # 先頭N機種だけ確認
    python scripts/audit_render.py [--base-url <URL>]      # 検査先を差し替え（ローカル検査用。例: http://localhost:8000）
    python scripts/audit_render.py                         # 全機種を確認（機種数はmachines.jsonから動的）

実行時間: 約2分（全機種・1機種あたり約1秒）

ローカル検査（push前の事前DOMゲート・2026-07-16追加）:
    リポジトリルートで `python -m http.server 8000` を起動してから
    `python scripts/audit_render.py --base-url http://localhost:8000 --slug <新slug>`
    ※R4のcanonical期待値は --base-url に関係なく常に本番URL（プリレンダで焼き込まれる値のため）

チェック項目:
    R1. ページタイトルが「機種ページ | うちどころ。」のデフォルトのまま固まっていないか
    R2. h1（機種名）が「機種名」のままになっていないか
    R3. body内に '99999' が表示されていないか
    R4. canonical タグが本番URLの /machines/{slug}/ を指しているか
    R5. 設定狙い専用機種で「ゲーム数狙いには向きません」が表示されているか
    R6. fetchエラー（machines.json / machine-details）が発生していないか
    R7. JSコンソールエラーが発生していないか
    R8. 機種名 h1 と machines.json のnameが一致するか
    R9. body内に '**' 記号が見える形で残っていないか（Markdown未解釈バグの検知）
    R10. セクションtitleが統一形と一致しているか（titleの揺れ検知）
    R11. ヘッダー行型テーブルのth数と各行td数の一致（settei表2セル固定バグの再発検知）
    R12. チェッカーと早見表のmode選択が双方向で同期するか（食い違った画面の再発検知）
    R12b. 早見表に無いmode（スルー・周期）選択中に、前のmodeの表が残らないか
"""

from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path

# Windows コンソール UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent
PROD_URL = "https://uchidokoro.com"
SITE_URL = PROD_URL  # --base-url で上書き可（ローカル検査用）。R4のcanonical期待値は常にPROD_URL


def load_machines() -> list:
    return json.loads((BASE / "assets" / "data" / "machines.json").read_text(encoding="utf-8"))


def is_setting_only(machine: dict) -> bool:
    checker = machine.get("checker") or {}
    normal = checker.get("normal") or {}
    excellent = normal.get("excellent")
    return (
        machine.get("limit") in (None, 0)
        and isinstance(excellent, (int, float))
        and excellent >= 99999
    )


sys.path.insert(0, str(BASE / "scripts"))
from build_new_article import PENDING_TEXT   # noqa: E402  ★未確認の文言（正本）★
import html_check as _hc                     # noqa: E402
import page_decision as _pd                  # noqa: E402


def _load_detail(slug: str):
    """記事データ。★無い・壊れている場合は None（合格にしない）★

    （2026-08-04・Codex81回目の指摘3。空dictに変換していたので、
      読み込み失敗が「節が無いページ」として素通りしていた）
    """
    p_ = BASE / "assets" / "data" / "machine-details" / f"{slug}.json"
    try:
        got = json.loads(p_.read_text(encoding="utf-8"))
    except Exception:                     # noqa: BLE001
        return None
    return got if isinstance(got, dict) else None


def _sig_of(html: str) -> list:
    """描き直したHTMLのタグの並び（属性は見ない）。

    ★tbody は数えない★（2026-08-04・Codex81回目の指摘1。
      ブラウザは table の中に tbody を自動で足すので、
      文字列から数えた並びとは必ずズレる）
    """
    import re as _re
    return [t for t in _re.findall(r"<([a-z0-9]+)", html) if t != "tbody"]


# ★最終DOMから箱の情報を取り出すJS★（統合試験からも同じものを使う）
BOX_JS = r"""() => {
        // ★隠し方は display だけではない★（2026-08-04・Codex81〜82回目）
        //   自分・祖先・**子孫**のどれで隠されても「見えていない」と扱う。
        //   clip-path は「何も切り取らない指定」を隠しと数えない（誤検知防止）。
        const noopClip = (v) => !v || v === 'none'
            || /^inset\(\s*0(px|%)?(\s+0(px|%)?){0,3}\s*\)$/.test(v);
        const hiddenSelf = (n) => {
            const st = getComputedStyle(n);
            return st.display === 'none' || st.visibility === 'hidden'
                || parseFloat(st.opacity || '1') < 0.05
                || !noopClip(st.clipPath)
                || (st.clip && st.clip !== 'auto');
        };
        const visible = (el) => {
            for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
                if (hiddenSelf(n)) return false;
            }
            const r = el.getBoundingClientRect();
            return el.offsetParent !== null && r.width >= 1 && r.height >= 1;
        };
        // ★見える文字は自分でたどって集める★
        //   innerText は opacity:0 / clip-path の文字を残すので、
        //   子を透明にするだけで検査を通せた（Codex82回目の指摘1）。
        const shownText = (node) => {
            let out = '';
            node.childNodes.forEach(c => {
                if (c.nodeType === 3) { out += c.nodeValue; return; }
                if (c.nodeType !== 1) return;
                if (['script', 'style', 'template'].includes(
                        c.tagName.toLowerCase())) return;
                if (c.hasAttribute('hidden')
                    || c.getAttribute('aria-hidden') === 'true') return;
                if (hiddenSelf(c)) return;
                out += shownText(c);
            });
            return out;
        };
        const out = [];
        document.querySelectorAll('#articleSections [data-section]').forEach(el => {
            const head = el.querySelector(':scope > h3.article-title');
            const sig = [el.tagName.toLowerCase()].concat(
                Array.from(el.querySelectorAll('*')).map(x => x.tagName.toLowerCase()));
            out.push({
                title: el.getAttribute('data-section'),
                pending: el.getAttribute('data-pending-section'),
                tag: el.tagName.toLowerCase(),
                has_cls: el.classList.contains('article-item'),
                shown: visible(el),
                heading: head ? (head.textContent || '') : null,
                sig: sig,
                text: shownText(el).replace(/\s+/g, ''),
            });
        });
        return out;
    }"""


def judge_boxes(boxes: list, detail) -> list[str]:
    """最終DOMの箱が契約どおりか（★ブラウザ無しで試験できる★）。

    ★この関数に来るのは新台経路の機種だけ★（呼ぶ側で絞る）。
    したがって**記事データが無い・空・契約と違う**のは、
    対象外ではなく**不合格**として扱う（Codex81回目の指摘3）。
    """
    import build_machine_pages as _bmp
    import build_new_article as _ba
    # ★並びは build_new_article に聞く★（2026-08-12・依頼160のP0-5）
    want = _ba.expected_titles(detail)
    if not isinstance(detail, dict):
        return ["R13: 記事データを読めません（新台経路のページなのに中身が無い）"]
    # ★箱だけの骨組み（本文が空）も止める★（Codex82回目の指摘2）
    bad = _ba.article_contract_problems(detail)
    if bad:
        return ["R13: " + x for x in bad]
    secs = [x for x in (detail.get("sections") or []) if isinstance(x, dict)]
    got = [b.get("title") for b in boxes]
    if got != want:
        return [f"R13: 最終DOMの箱がデータと違います（{got} / {want} のはず）"]
    ngs = []
    for sec, b in zip(secs, boxes):
        title = sec.get("title")
        if not b.get("shown"):
            ngs.append(f"R13: 箱が読者に見えていません: {title}")
        if b.get("tag") != "div" or not b.get("has_cls"):
            ngs.append(f"R13: 箱の作りが違います: {title} <{b.get('tag')}>")
        if (b.get("heading") or "").strip() != title:
            ngs.append(f"R13: 箱の見出しが違います: {title}")
        rendered = _bmp.render_section(sec)
        if [x for x in (b.get("sig") or []) if x != "tbody"] != _sig_of(rendered):
            ngs.append(f"R13: 箱の中の作りが違います（表や段落が壊れています）: {title}")
        want_text = "".join(_hc.visible_text("<body>" + rendered + "</body>").split())
        if (b.get("text") or "") != want_text:
            ngs.append(f"R13: 箱の中身がデータと違います: {title}")
        # ★新旧どちらの文言でも未確認★（2026-08-12・依頼160のP2-7）
        if _ba.is_pending_body(sec.get("body")) and b.get("pending") != title:
            ngs.append(f"R13: 未確認の箱に目印がありません: {title}")
        if not _ba.is_pending_body(sec.get("body")) and b.get("pending"):
            ngs.append(f"R13: 中身がある箱に未確認の目印が付いています: {title}")
    return ngs


# ★最終DOMから目次表を取り出すJS★（2026-08-31・台帳#523）
#   ★見え方の判定は BOX_JS と同じ厳しさにする★＝display だけを見ると
#   opacity:0 / visibility:hidden / clip-path で消したものを見逃す。
TOC_JS = r"""() => {
        const noopClip = (v) => !v || v === 'none'
            || /^inset\(\s*0(px|%)?(\s+0(px|%)?){0,3}\s*\)$/.test(v);
        const hiddenSelf = (n) => {
            const st = getComputedStyle(n);
            return st.display === 'none' || st.visibility === 'hidden'
                || parseFloat(st.opacity || '1') < 0.05
                || !noopClip(st.clipPath)
                || (st.clip && st.clip !== 'auto');
        };
        const visible = (el) => {
            for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
                if (hiddenSelf(n)) return false;
            }
            const r = el.getBoundingClientRect();
            return el.offsetParent !== null && r.width >= 1 && r.height >= 1;
        };
        const block = document.getElementById('tocBlock');
        const items = Array.from(document.querySelectorAll('#tocList .toc-item'));
        return {
            block_shown: block ? visible(block) : false,
            items: items.map(a => {
                const href = a.getAttribute('href') || '';
                const id = href.slice(0, 1) === '#' ? href.slice(1) : '';
                const t = id ? document.getElementById(id) : null;
                return {label: (a.textContent || '').trim(), id: id,
                        exists: !!t, shown: t ? visible(t) : false};
            }),
        };
    }"""


def expected_toc(machine: dict, detail, pochi_reasons: dict) -> list:
    """★目次に並ぶはずのラベル★（ページのDOMを一切見ずに決める）

    （2026-08-31・Codexの指摘P2。★罠⑦＝期待値を検査対象そのものから作る★）
      直す前の通し確認は、節の期待値を `#articleSections` から取っていたので、
      **目次と本文が同じように壊れたら合格**した
      （JSが1節落とせば、目次からも消えて一致してしまう）。

    ここで見るもの:
      節         … `machine-details/{slug}.json` の sections[].title
      道具の有無 … 機種の区分（新台経路・preview は道具が隠れる）
      ポチポチくん … ひな型の名簿（`extract_pochipochi_reasons`）
    """
    cls = _pd.machine_class(machine)          # ★壊れた判定書は例外で止まる★
    hide_tools = cls in ("AUTO_INDEXABLE", "AUTO_PENDING", "LEGACY_PREVIEW")
    out = []
    if not hide_tools:
        out += ["狙い目チェッカー", "狙い目早見表"]
    out.append("基本情報")
    for sec in ((detail or {}).get("sections") or []):
        if not isinstance(sec, dict):
            continue
        title = str(sec.get("title") or "").strip()
        if title:
            out.append(title)
    if not hide_tools and machine.get("slug") not in pochi_reasons:
        out.append("ポチポチくん")
    return out


def _raises(fn) -> bool:
    """★例外で止まることを試験する小道具★（黙って通していないか）。"""
    try:
        fn()
    except Exception:                     # noqa: BLE001
        return True
    return False


def judge_toc(toc: dict, want: list) -> list[str]:
    """目次表が契約どおりか（★ブラウザ無しで試験できる純関数★）。"""
    ngs: list[str] = []
    if not isinstance(toc, dict):
        return ["R14: 目次を読み取れません"]
    items = toc.get("items") or []
    got = [x.get("label") for x in items]
    if len(want) < 2:
        # ★1つ以下なら目次にならないので出さない★（ひな型の決まりと対）
        if toc.get("block_shown"):
            ngs.append(f"R14: 目次に出すものが{len(want)}件なのに目次が出ています")
        return ngs
    if not toc.get("block_shown"):
        return [f"R14: 目次が読者に見えていません（{want} が出るはず）"]
    if got != want:
        return [f"R14: 目次の中身が違います（{got} / {want} のはず）"]
    for it in items:
        if not it.get("exists"):
            ngs.append(f"R14: 飛び先がありません: {it.get('label')}"
                       f" -> #{it.get('id')}")
        elif not it.get("shown"):
            ngs.append(f"R14: 飛び先が読者に見えていません: {it.get('label')}")
    return ngs


def check_one(page, machine: dict) -> list[str]:
    """1機種のレンダリング検査。NGメッセージのリストを返す。"""
    slug = machine["slug"]
    detail = _load_detail(slug)
    url = f"{SITE_URL}/machines/{slug}/"
    ngs: list[str] = []
    console_errors: list[str] = []

    # コンソールエラー収集
    def on_console(msg):
        if msg.type in ("error",):
            text = msg.text
            # サードパーティスクリプトの既知ノイズは除外
            if any(k in text for k in ["adsbygoogle", "googletagmanager", "Failed to load resource"]):
                return
            console_errors.append(text)

    page.on("console", on_console)

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
    except Exception as e:
        ngs.append(f"[goto失敗] {url}: {e}")
        return ngs

    # meta-auto.js の fetch + DOM更新を待つ（h1が機種名に変わるか、最大3秒）
    try:
        page.wait_for_function(
            """() => {
                const h1 = document.querySelector('#machineTitle');
                return h1 && h1.textContent && h1.textContent !== '機種名';
            }""",
            timeout=3000,
        )
    except Exception:
        pass  # タイムアウトしてもチェックは継続（R2でNG出る）
    # 残りのレンダリング安定化
    page.wait_for_timeout(500)

    # R1: タイトル
    title = page.title()
    if title.strip() in ("機種ページ | うちどころ。", "", "機種ページ"):
        ngs.append(f"R1: タイトルがデフォルトのまま固まってる ('{title}')")

    # R2: h1
    h1_text = page.evaluate("() => document.querySelector('#machineTitle')?.textContent || ''").strip()
    if h1_text == "機種名" or h1_text == "":
        ngs.append(f"R2: 機種名h1が未更新 ('{h1_text}')")

    # R8: machines.json の name と一致
    expected_name = machine["name"]
    if h1_text and h1_text != expected_name:
        ngs.append(f"R8: h1='{h1_text}' vs machines.json name='{expected_name}'")

    # R3: body内に99999が表示されていないか
    body_text = page.evaluate("() => document.body.innerText")
    if "99999" in body_text:
        # 周辺を抜粋
        idx = body_text.find("99999")
        snippet = body_text[max(0, idx - 30):idx + 40].replace("\n", " ")
        ngs.append(f"R3: body内に '99999' を検出 (周辺: ...{snippet}...)")

    # R4: canonical（期待値はローカル検査でも常に本番URL＝プリレンダで焼き込まれる値）
    canonical = page.evaluate("() => document.querySelector('link[rel=\"canonical\"]')?.href || ''")
    expected_canon = f"{PROD_URL}/machines/{slug}/"
    if canonical != expected_canon:
        ngs.append(f"R4: canonical='{canonical}' (期待値: {expected_canon})")

    # R5: 設定狙い専用機種の表示
    if is_setting_only(machine):
        result_text = page.evaluate("() => document.querySelector('.checker-result .result-text')?.textContent || ''").strip()
        if "向きません" not in result_text and "設定狙い" not in result_text:
            ngs.append(f"R5: 設定狙い専用機種なのに案内表示が誤り ('{result_text}')")

    # R6: fetch関連のエラー（machines.json/machine-details）
    # → ネットワークエラーは page.goto 時の networkidle で大体検出されるが念のため
    # 既に R2/R8 で h1 が機種名になっているか確認しているので、fetch失敗時はそこで検出される

    # R7: コンソールエラー
    if console_errors:
        for err in console_errors[:3]:  # 最大3件
            ngs.append(f"R7: console.error: {err[:120]}")

    # R9: body内に '**' 記号がレンダリング後に残っていないか
    # （Markdown解釈で <strong> 化されているはず）
    if "**" in body_text:
        idx = body_text.find("**")
        snippet = body_text[max(0, idx - 20):idx + 30].replace("\n", " ")
        ngs.append(f"R9: body内に '**' 記号を検出 (Markdown未解釈の可能性・周辺: ...{snippet}...)")

    # R10: 既に統一されているはずの旧titleが残っていないか（揺れ検知）
    # normalize_sections.py の TITLE_RENAME マッピングと同期
    deprecated_titles = {
        "ヤメ時", "やめどき", "リセット・ヤメ時",
        "立ち回りメモ",
        "朝一 リセット情報", "リセット狙い", "リセット恩恵",
        "判明しているスペック",
        "狙い目・ヤメ時", "狙い目の目安", "狙い目",
    }
    titles = page.evaluate("""() => Array.from(document.querySelectorAll('.article-title')).map(e => e.textContent.trim())""")
    for t in titles:
        if t in deprecated_titles:
            ngs.append(f"R10: 統一前の旧title残留: '{t}' → 正規化スクリプト実行が必要")

    # R11: 全tableで見出しth数と各行のtd数が一致しているか
    # （2026-07-13外部レビュー: settei表レンダラーが2セル固定で4列表のREG・合算列が消えていた事故の再発検知）
    bad_tables = page.evaluate("""() => {
        const out = [];
        document.querySelectorAll('table').forEach((tbl, ti) => {
            const firstTr = tbl.querySelector('tr');
            if (!firstTr) return;
            const headTh = firstTr.querySelectorAll('th').length;
            // 対象は「先頭行が全thのヘッダー行型」テーブルのみ（info-table等の行見出し型は対象外）
            if (headTh < 2 || firstTr.querySelectorAll('td').length) return;
            Array.from(tbl.querySelectorAll('tr')).forEach((tr, ri) => {
                if (ri === 0 || tr.querySelectorAll('th').length) return;
                const tds = tr.querySelectorAll('td').length;
                if (tds && tds !== headTh) out.push(`table${ti} 行${ri}: th${headTh}列に対しtd${tds}セル`);
            });
        });
        return out.slice(0, 3);
    }""")
    for b in bad_tables:
        ngs.append(f"R11: 表の列数不整合: {b}")

    # R13: 記事の箱が、読者の見る最終DOMでも契約どおりか
    #   （2026-08-04・Codex79〜80回目。ページはJSで箱を作り直すので、
    #     静的HTMLの契約が最終DOMまで保たれているかを別に確かめる）
    #   ★判定は judge_boxes()（純関数）に置く★＝ブラウザ無しで試験できる
    boxes = page.evaluate(BOX_JS)
    # ★契約は新台経路（page-decision/v1）だけ★（既存120機種は従来の作り）
    try:
        _is_auto = _pd.machine_class(machine) in ("AUTO_INDEXABLE",
                                                  "AUTO_PENDING")
    except Exception as e:                # noqa: BLE001
        ngs.append(f"R13: 機種の区分を判定できません: {e}")
        _is_auto = False
    if _is_auto:
        ngs += judge_boxes(boxes, detail)

    # R14: 目次表が、独立に決めた期待値と一致するか（2026-08-31・台帳#523）
    #   ★期待値はページのDOMから作らない★（Codexの指摘P2・罠⑦）
    try:
        import build_machine_pages as _bmp14
        with open(BASE / "machine.html", encoding="utf-8") as _f14:
            _pochi14 = _bmp14.extract_pochipochi_reasons(_f14.read())
        _want14 = expected_toc(machine, detail, _pochi14)
    except Exception as e:                # noqa: BLE001
        ngs.append(f"R14: 目次の期待値を作れません: {type(e).__name__}: {e}")
    else:
        ngs += judge_toc(page.evaluate(TOC_JS), _want14)


    # R12: チェッカーと早見表のmode選択が同期しているか
    #   （2026-07-27 Codex閉鎖確認 #2: 別々に持っていたため
    #     「チェッカーはリセット・早見表は通常」という食い違った画面になっていた）
    modes = page.eval_on_selector_all(
        'input[name="mode"]', "els=>els.map(e=>e.value)")
    ev_modes = page.eval_on_selector_all(
        'input[name="evMode"]', "els=>els.map(e=>e.value)")
    common = [m for m in modes if m in ev_modes]
    if len(common) >= 2:
        try:
            page.click(f'label[for="mode_{common[1]}"]')
            page.wait_for_timeout(120)
            now = page.eval_on_selector('input[name="evMode"]:checked', "e=>e.value")
            if now != common[1]:
                ngs.append(f"R12: チェッカーで {common[1]} を選んでも早見表が {now} のまま")
            page.click(f'label[for="evmode_{common[0]}"]')
            page.wait_for_timeout(120)
            now2 = page.eval_on_selector('input[name="mode"]:checked', "e=>e.value")
            if now2 != common[0]:
                ngs.append(f"R12: 早見表で {common[0]} を選んでもチェッカーが {now2} のまま")
        except Exception as e:                       # 操作できない形なら検査自体を失敗にする
            ngs.append(f"R12: mode同期の検査に失敗: {type(e).__name__}")

    # R12b: 早見表に無いmode（スルー・周期）を選んだら、古いmodeの表が残らないこと
    #   （2026-07-27 Codex閉鎖確認2回目: 同期の例外経路が抜けていた）
    only_checker = [m for m in modes if m not in ev_modes]
    if only_checker and ev_modes:
        try:
            page.click(f'label[for="mode_{only_checker[0]}"]')
            page.wait_for_timeout(120)
            shown = page.evaluate(
                """() => {const b=document.getElementById('evTableBlock');
                   if(!b) return false;
                   const st=getComputedStyle(b);
                   return st.display!=='none' && st.visibility!=='hidden';}""")
            if shown:
                ngs.append(f"R12b: {only_checker[0]} を選んでも早見表が出たまま"
                           f"（前のmodeの表が残る）")
            # 通常modeへ戻したら復帰すること
            page.click(f'label[for="mode_{ev_modes[0]}"]')
            page.wait_for_timeout(120)
            back = page.evaluate(
                """() => {const b=document.getElementById('evTableBlock');
                   if(!b) return false;
                   const st=getComputedStyle(b);
                   return st.display!=='none' && st.visibility!=='hidden';}""")
            if not back:
                ngs.append(f"R12b: {ev_modes[0]} へ戻しても早見表が復帰しない")
            now3 = page.eval_on_selector('input[name="evMode"]:checked', "e=>e.value")                 if len(ev_modes) > 1 else ev_modes[0]
            if now3 != ev_modes[0]:
                ngs.append(f"R12b: 戻したあとの早見表modeが {now3}（期待 {ev_modes[0]}）")
        except Exception as e:
            ngs.append(f"R12b: 検査に失敗: {type(e).__name__}")

    return ngs


def selftest_dom() -> int:
    """★実ブラウザでの統合試験★（2026-08-04・Codex81回目の指摘5）

    手で組み立てた値ではなく、**本物のブラウザにHTMLを読ませて**
    抽出JSの結果を確かめる。tbodyの自動挿入・子の非表示・祖先の非表示。
    """
    import build_machine_pages as _bmp
    import build_new_article as _ba
    from playwright.sync_api import sync_playwright
    ok_all, ran = True, [0]

    def t(name, cond):
        nonlocal ok_all
        ran[0] += 1
        ok_all = ok_all and bool(cond)
        print(("✅" if cond else "❌") + " " + name)

    _IM = {"basis": "INDEPENDENT_MULTI"}     # ★本番の材料は必ず根拠を持つ★
    mat = {"adopted": {"model_code": {"value": "L1"},
                       "payout_range": {**_IM,
                                        "value": {"low": 97, "high": 110}},
                       "payout_rate": {**_IM,
                                       "value": {"1": "97%", "6": "110%"}}},
           "at_specs": {"adopted": [{**_IM, "mode": "MAIN_AT", "games": 30,
                                     "net": 2.8}]}}
    det = _ba.build_detail("zzz", "試験機", "2026-09", mat)
    inner = "".join(_bmp.render_section(x) for x in det["sections"])

    def page_of(body_extra="", wrap_style=""):
        return ("<html><head><meta charset='utf-8'></head><body>"
                f'<div id="articleSections"{wrap_style}>{inner}</div>'
                + body_extra + "</body></html>")

    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        pg = b.new_page()
        try:
            pg.set_content(page_of())
            good = pg.evaluate(BOX_JS)
            t("★★本物のブラウザで、正しいページなら通る★★"
              "（表の tbody 自動挿入で誤検知しない・Codex81回目の指摘1）",
              judge_boxes(good, det) == [])
            t("　ブラウザは実際に tbody を足している（誤検知の元）",
              any("tbody" in (x.get("sig") or []) for x in good))
            # 子（本文）だけ隠す
            pg.set_content(page_of().replace('<p class="article-body">',
                                             '<p class="article-body" hidden>'))
            hid = pg.evaluate(BOX_JS)
            t("★★本文だけ隠したら止める★★（Codex81回目の指摘2）",
              any("中身がデータと違います" in x for x in judge_boxes(hid, det)))
            # 祖先を透明にする
            pg.set_content(page_of(wrap_style=' style="opacity:0"'))
            anc = pg.evaluate(BOX_JS)
            t("★★祖先を透明にしたら止める★★（箱だけ見ていると気づけない）",
              any("見えていません" in x for x in judge_boxes(anc, det)))
            # 祖先を切り抜く
            pg.set_content(page_of(wrap_style=' style="clip-path:inset(100%)"'))
            clp = pg.evaluate(BOX_JS)
            t("　祖先を clip-path で切り抜いても止める",
              any("見えていません" in x for x in judge_boxes(clp, det)))
            # ★子だけを透明にする★（2026-08-04・Codex82回目の指摘1）
            pg.set_content(page_of(body_extra=(
                "<style>#articleSections [data-section] > *"
                "{opacity:0}</style>")))
            ch1 = pg.evaluate(BOX_JS)
            t("★★子を opacity:0 で消したら止める★★（innerTextでは残っていた）",
              any("中身がデータと違います" in x for x in judge_boxes(ch1, det)))
            pg.set_content(page_of(body_extra=(
                "<style>#articleSections [data-section] > *"
                "{clip-path:inset(100%)}</style>")))
            ch2 = pg.evaluate(BOX_JS)
            t("★★子を clip-path で切り抜いても止める★★",
              any("中身がデータと違います" in x for x in judge_boxes(ch2, det)))
            # ★何も切り取らない clip-path で誤検知しない★（Codex82回目・厚み）
            pg.set_content(page_of(wrap_style=' style="clip-path:inset(0)"'))
            ok2 = pg.evaluate(BOX_JS)
            t("　clip-path:inset(0)（何も隠さない指定）では止めない",
              judge_boxes(ok2, det) == [])
            # ★骨組みだけの記事データは止める★（Codex82回目の指摘2）
            import build_new_article as _ba2
            skel = {"slug": "zzz",
                    "sections": [{"title": x, "body": []}
                                 for x in _ba2.SECTION_ORDER]
                    + [{"title": _ba2.RUMOR_SECTION["title"], "body": []}]}
            pg.set_content(page_of())
            t("★★タイトルだけ残して本文が空の記事データは止める★★",
              any("中身がありません" in x for x in
                  judge_boxes(pg.evaluate(BOX_JS), skel)))
        finally:
            b.close()
    print(f"{ran[0]}/{ran[0]} 合格" if ok_all else "不合格あり")
    return 0 if ok_all else 1


def selftest() -> int:
    """★R13の判定を、ブラウザ無しで確かめる★（2026-08-04・Codex80回目の指摘5）

    ブラウザから取る値（boxes）を手で組み立てて、判定関数だけを試す。
    """
    import build_machine_pages as _bmp
    import build_new_article as _ba
    ok_all, ran = True, [0]

    def t(name, cond):
        nonlocal ok_all
        ran[0] += 1
        ok_all = ok_all and bool(cond)
        print(("✅" if cond else "❌") + " " + name)

    _IM = {"basis": "INDEPENDENT_MULTI"}     # ★本番の材料は必ず根拠を持つ★
    mat = {"adopted": {"model_code": {"value": "L1"},
                       "payout_range": {**_IM,
                                        "value": {"low": 97, "high": 110}},
                       "payout_rate": {**_IM,
                                       "value": {"1": "97%", "6": "110%"}}},
           "at_specs": {"adopted": [{**_IM, "mode": "MAIN_AT", "games": 30,
                                     "net": 2.8}]}}
    det = _ba.build_detail("zzz", "試験機", "2026-09", mat)

    def box_of(sec):
        rendered = _bmp.render_section(sec)
        return {"title": sec["title"],
                "pending": (sec["title"]
                            if _ba.is_pending_body(sec.get("body")) else None),
                "tag": "div", "has_cls": True, "shown": True,
                "heading": sec["title"], "sig": _sig_of(rendered),
                "text": "".join(_hc.visible_text(
                    "<body>" + rendered + "</body>").split())}
    good = [box_of(x) for x in det["sections"]]
    t("★正しい最終DOMなら通る★", judge_boxes(good, det) == [])
    t("★★箱が1つも無ければ止める★★"
      "（JSが作らない不具合が素通りしていた・Codex80回目の指摘1）",
      any("箱がデータと違います" in x for x in judge_boxes([], det)))
    t("　順番が違えば止める",
      any("箱がデータと違います" in x for x in
          judge_boxes(list(reversed(good)), det)))
    t("★★opacity等で見えなくしていたら止める★★",
      any("見えていません" in x for x in
          judge_boxes([{**good[0], "shown": False}] + good[1:], det)))
    t("★★クラスが article-item-broken のような別物なら止める★★"
      "（部分一致で通っていた・Codex80回目の指摘4）",
      any("箱の作りが違います" in x for x in
          judge_boxes([{**good[0], "has_cls": False}] + good[1:], det)))
    t("　見出しが直下の h3.article-title でなければ止める",
      any("見出しが違います" in x for x in
          judge_boxes([{**good[0], "heading": None}] + good[1:], det)))
    t("★★表を段落に潰したら止める★★（作りの並びで見る・指摘2）",
      any("表や段落が壊れています" in x for x in judge_boxes(
          [{**b, "sig": [x.replace("table", "p").replace("tr", "p")
                         .replace("th", "p").replace("td", "p")
                         for x in b["sig"]]} if b["title"] == "設定示唆まとめ"
           else b for b in good], det)))
    t("★★本文を消したら止める★★",
      any("中身がデータと違います" in x for x in judge_boxes(
          [{**b, "text": b["title"]} if b["title"] == "ゲーム性" else b
           for b in good], det)))
    t("　未確認の箱の目印が無ければ止める",
      any("目印がありません" in x for x in judge_boxes(
          [{**b, "pending": None} if b["pending"] else b for b in good], det)))
    # ---- R14: 目次表（2026-08-31・台帳#523）
    #   ★材料は手で組み立てる★（本番のデータに貼り付けない・罠㉙）
    _legacy = {"slug": "zzz_legacy", "name": "試験機"}
    _dec14 = _pd.decide_from_claims([], "normal")
    _auto = {"slug": "zzz_auto", "name": "試験機2",
             # ★版は判定書が名乗るものに合わせる★（食い違いは別の検査の担当）
             "publication_policy": _dec14["schema_version"],
             "page_decision": _dec14}
    _prev = {"slug": "zzz_prev", "name": "試験機3", "status": "preview"}
    _det14 = {"sections": [{"title": "天井・恩恵", "body": ["x"]},
                           {"title": "基本スペック", "body": ["x"]}]}
    _want_legacy = expected_toc(_legacy, _det14, {})
    t("★旧形式は道具も節も並ぶ★",
      _want_legacy == ["狙い目チェッカー", "狙い目早見表", "基本情報",
                       "天井・恩恵", "基本スペック", "ポチポチくん"])
    t("★ポチポチくんが使えない機種は出さない★",
      "ポチポチくん" not in expected_toc(_legacy, _det14,
                                        {"zzz_legacy": "設定差なし"}))
    t("★★新台経路は道具を1つも出さない★★"
      "（ひな型がチェッカー・早見表を隠し、ポチポチくんも使えないため）",
      expected_toc(_auto, _det14, {})
      == ["基本情報", "天井・恩恵", "基本スペック"])
    t("　preview も同じ（道具が隠れている）",
      expected_toc(_prev, _det14, {})
      == ["基本情報", "天井・恩恵", "基本スペック"])
    t("★★判定書が壊れていたら黙って旧形式にせず止める★★",
      _raises(lambda: expected_toc(
          {**_auto, "page_decision": {"schema_version": "こわれ"}},
          _det14, {})))

    def _toc_of(labels, shown=True, exists=True, target_shown=True):
        return {"block_shown": shown,
                "items": [{"label": x, "id": "i%d" % i,
                           "exists": exists, "shown": target_shown}
                          for i, x in enumerate(labels)]}
    t("★期待どおりなら通る★", judge_toc(_toc_of(_want_legacy),
                                        _want_legacy) == [])
    t("★★節が1つ落ちたら止める★★（目次と本文が同じように壊れても気づく）",
      any("目次の中身が違います" in x for x in
          judge_toc(_toc_of([x for x in _want_legacy if x != "基本スペック"]),
                    _want_legacy)))
    t("★★新台なのにチェッカーが目次に出ていたら止める★★",
      any("目次の中身が違います" in x for x in
          judge_toc(_toc_of(["狙い目チェッカー"] + _want_legacy),
                    _want_legacy)))
    t("★★飛び先が無ければ止める★★",
      any("飛び先がありません" in x for x in
          judge_toc(_toc_of(_want_legacy, exists=False), _want_legacy)))
    t("★★飛び先が見えていなければ止める★★",
      any("飛び先が読者に見えていません" in x for x in
          judge_toc(_toc_of(_want_legacy, target_shown=False), _want_legacy)))
    t("★★目次そのものが見えていなければ止める★★（opacity:0 等）",
      any("見えていません" in x for x in
          judge_toc(_toc_of(_want_legacy, shown=False), _want_legacy)))
    t("　並ぶものが1つなら目次を出さないのが正しい",
      judge_toc({"block_shown": False, "items": []}, ["基本情報"]) == [])
    t("　並ぶものが1つなのに目次が出ていたら止める",
      any("なのに目次が出ています" in x for x in
          judge_toc({"block_shown": True, "items": []}, ["基本情報"])))

    t("　中身がある箱に未確認の目印が付いていたら止める",
      any("未確認の目印が付いています" in x for x in judge_boxes(
          [{**b, "pending": b["title"]} if not b["pending"] else b
           for b in good], det)))
    t("★★新台なのに記事データが空なら不合格★★"
      "（対象外にしていた＝fail-open・Codex81回目の指摘3）",
      any("契約と違います" in x for x in
          judge_boxes([], {"slug": "zzz", "sections": []})))
    t("★★タイトルだけで本文が空の骨組みも不合格★★（Codex82回目の指摘2）",
      any("中身がありません" in x for x in judge_boxes([], {
          "slug": "zzz",
          "sections": [{"title": x, "body": []}
                       for x in _ba.SECTION_ORDER]
          + [{"title": _ba.RUMOR_SECTION["title"], "body": []}]})))
    t("★★記事データを読めない（None）場合も不合格★★",
      any("読めません" in x for x in judge_boxes([], None)))
    t("　ブラウザが足す tbody は作りの違いに数えない",
      judge_boxes([{**b, "sig": (b["sig"][:1] + ["tbody"] + b["sig"][1:])}
                   for b in good], det) == [])
    print(f"{ran[0]}/{ran[0]} 合格" if ok_all else "不合格あり")
    return 0 if ok_all else 1


def main():
    global SITE_URL
    parser = argparse.ArgumentParser()
    parser.add_argument("--selftest", action="store_true",
                        help="R13の判定をブラウザ無しで試す")
    parser.add_argument("--selftest-dom", action="store_true",
                        help="R13の抽出を実ブラウザで試す")
    parser.add_argument("--slug", help="特定の1機種だけチェック")
    parser.add_argument("--limit", type=int, help="先頭N機種だけチェック")
    parser.add_argument("--json", action="store_true", help="JSON形式で結果を出力")
    parser.add_argument("--base-url", help="検査対象のベースURL（省略時は本番。ローカル検査は http://localhost:8000 等）")
    args = parser.parse_args()
    if args.selftest:
        return selftest()
    if args.selftest_dom:
        return selftest_dom()

    if args.base_url:
        SITE_URL = args.base_url.rstrip("/")

    machines = load_machines()
    if args.slug:
        machines = [m for m in machines if m["slug"] == args.slug]
        if not machines:
            print(f"slug '{args.slug}' が machines.json に見つかりません")
            sys.exit(2)
    elif args.limit:
        machines = machines[: args.limit]

    from playwright.sync_api import sync_playwright

    all_results: dict[str, list[str]] = {}
    total_ng = 0
    started = time.time()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        for i, m in enumerate(machines, 1):
            slug = m["slug"]
            t0 = time.time()
            try:
                ngs = check_one(page, m)
            except Exception as e:
                ngs = [f"[例外] {e}"]
            elapsed = time.time() - t0
            all_results[slug] = ngs
            total_ng += len(ngs)
            mark = "✅" if not ngs else "❌"
            if not args.json:
                print(f"[{i:3}/{len(machines):3}] {mark} {slug} ({elapsed:.1f}s)" + (f"  NG:{len(ngs)}件" if ngs else ""))
                for ng in ngs:
                    print(f"     - {ng}")

        browser.close()

    elapsed_total = time.time() - started

    if args.json:
        print(json.dumps(all_results, ensure_ascii=False, indent=2))
    else:
        print(f"\n=== レンダリング監査完了 ({elapsed_total:.1f}秒・{len(machines)}機種・NG合計 {total_ng}件) ===")

    sys.exit(0 if total_ng == 0 else 1)


if __name__ == "__main__":
    raise SystemExit(main() or 0)

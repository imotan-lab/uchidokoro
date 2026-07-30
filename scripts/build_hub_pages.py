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
import html as html_mod
import json
import os
import re
import sys
import unicodedata
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


# 「数＋単位」の出現を1つずつ取り出す
# ★NFKC正規化のあとに掛ける★（全角・丸数字などを取りこぼさない）
# ★「ゲーム」も単位★（Codex 15巡目 (a)-2：「天井は200ゲームです」が素通りしていた）
NUMERAL_OCCURRENCE = re.compile(
    r"[0-9一二三四五六七八九十百千万〇零]+\s*"
    r"(?:G|g|Ｇ|ゲーム|pt|ポイント|P|回|周期|スルー|枚|円|%|パーセント|割|倍|分|時間|日|台|"
    r"機種|セット|連|スロット|ベル|レア役|ゲーム数)")

# インライン要素は取り除いて文字をつなぐ（「勝<strong>て</strong>る」を「勝てる」に戻す）
# ★語を分断できる要素は全部ここに入れる★（Codex 16巡目 (a)-4）
_INLINE_TAG = re.compile(
    r"</?(?:strong|b|em|i|span|small|sup|sub|u|mark|a|br|wbr|ruby|rt|rp|rb|abbr|code|"
    r"kbd|var|cite|q|data|time|bdi|bdo|ins|del|s|big|tt|font|label|output|dfn|samp|"
    r"nobr|acronym)\b[^>]*>", re.IGNORECASE)
_ANY_TAG = re.compile(r"<[^>]+>")
# 属性に入った文章（meta description・title・alt など）も検査対象にする
# ★シングルクォートと、文章が入りうる属性を全部見る★（同 (a)-4）
_ATTR_TEXT = re.compile(
    r"(?:content|title|alt|aria-[\w-]+|placeholder|value|label|summary|data-[\w-]+)"
    r"""\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>"']+))""", re.IGNORECASE)
# data: URL は中身が読めないので、そもそも使わない（見つけたら止める）
_DATA_URL = re.compile(r"""["'(]\s*data:[^"')\s]+""", re.IGNORECASE)
# 見た目は同じでも検査を回避できる不可視文字（ゼロ幅・方向制御・不可視区切り）
_INVISIBLE = re.compile(
    "[​-‏‪-‮⁠-⁤⁪-⁯﻿­᠎]")
# 「全<span class="list-count">49</span>機種です」の 49 はデータから毎回数える件数
_COUNT_SPAN = re.compile(r'<span class="list-count">.*?</span>', re.DOTALL)
_RUBY_ANNOTATION = re.compile(r"<(rt|rp)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)


def visible_text(html: str) -> str:
    """表示される文章（＋文章が入る属性）を、比べられる形にして返す。

    ★タグを空白に置き換えるだけでは足りない★（Codex 15巡目 (a)-2）
      「勝<strong>て</strong>る」が「勝 て る」になって禁止語を回避できた。
      インライン要素は詰めてつなぎ、ブロック要素だけ区切る。
      metaの中身はタグごと消えていたので、属性の文章を別に拾う。
    """
    # ★属性はクォート無しと data-* も全部見る★（Codex 17巡目 (a)-5）
    attrs = " ".join(a or b or c for a, b, c in _ATTR_TEXT.findall(html))
    # ★集計の件数はここでは消さない★（Codex 24巡目 (a)-2）
    #   CSSクラスを「検査免除の札」にすると、生成器の回帰で
    #   <span class="list-count">必ず勝てる</span> のような文も素通りしてしまう。
    #   代わりに、許される集計値（数）だけを呼び出し側から渡して照合する。
    body = html
    # ふりがな（rt/rp）は本文の間に挟まるので、中身ごと落としてから詰める
    # （「勝<rt>か</rt>てる」で語を分断できてしまうため・Codex 16巡目 (a)-4）
    body = _RUBY_ANNOTATION.sub("", body)
    body = _INLINE_TAG.sub("", body)          # インラインは詰める
    body = _ANY_TAG.sub("\n", body)           # ブロックは区切る
    text = html_mod.unescape(body + "\n" + html_mod.unescape(attrs))
    text = unicodedata.normalize("NFKC", text)
    # ★不可視文字は取り除いてから判定する★（Codex 16巡目 (a)-4）
    #   「必ず勝<ゼロ幅>てる」「200<ゼロ幅>G」は画面では同じに見えるのに素通りしていた。
    return _INVISIBLE.sub("", text)

# ★生成器自身のコードに書いた固定の数値表現★（集計の説明で使う言葉。機種の数値ではない）
#   ここに無い数値が散文に出たら止める（＝手書きの数値は裏取りが要る）。
# ★集計の定義そのもの（機種の数値ではない）★
#   ページには <span class="list-count"> で出すので、検査からは自動で外れる。
#   ここに文字列を並べる「例外リスト」は持たない（Codex 閉鎖条件1）。
SHALLOW_TENJO_LIMIT = 1000
HUB_FIXED_NUMERALS: tuple = ()


def _counts_allowed(a: int, c: int, c_top: int, d: int, all_: int) -> dict:
    """出してよい「集計の数」を★描く前に・ページ別に・出現回数つきで★決める。

    （Codex 25巡目 (a)-2 / 26巡目）
      生成後HTMLから拾うと、生成器が件数を間違えても検査器が許してしまう。
      さらに「49機種」を別の意味の場所で使い回せないよう、
      ページごとに「何が何回まで出てよいか」を持つ。
    """
    from collections import Counter
    return {
        "guide-tenjo-ranking.html": Counter({f"{SHALLOW_TENJO_LIMIT}G": 1, f"{a}機種": 1}),
        # ★同じ数になる場合は2回分として数える★（Codex 26巡目 (b)-1）
        #   Counter({"30機種":1, "30機種":1}) は1件に潰れてしまう。
        "guide-reset-ranking.html": Counter([f"{c}機種", f"{c_top}機種"]),
        "guide-suru-tenjo.html": Counter({f"{d}機種": 1}),
        "guide-ichiran.html": Counter({f"{all_}機種": 1}),
    }


def hub_content_problems(built: dict, data_html: dict, *deny_pats,
                         prose_all: dict | None = None,
                         allowed_counts: dict | None = None) -> list:
    """出来上がったハブ4ページのうち、**データ由来でない部分**を検査する。

    ★機種一覧そのものは検査しない★（Codex 14巡目 (b)-1）
      一覧は公開データをそのまま並べた部分なので、ここの数値は裏取り済み。
      以前は一覧も含めて「単位つき数値は全部未検証」と見ていたため、
      生成器自身が出す「1000G未満」でも必ず止まり、原因も分からなかった。
      検査すべきは **手書きの散文（hub_prose.json）と生成器のコードに書いた文言**。
    ★数値の無い断定も止める★（同 (a)-5）
      「必ず勝てる」のように単位つき数値を含まない文は素通りしていた。
    """
    bad = []
    for f, html in built.items():
        rest = html
        # 機種一覧（公開データをそのまま並べた部分）だけを検査から外す。
        # ★ちょうど1回だけ除去する★（Codex 15巡目 (a)-2）
        #   replace は一致箇所を全部消すので、生成器の回帰で一覧が2回出ると
        #   両方が検査対象外になってしまう。
        part = (data_html.get(f) or {}).get("list")
        if part:
            n = rest.count(part)
            if n != 1:
                bad.append(f"{f}: 機種一覧の描画が {n} 箇所あります（1箇所であるべき）")
                continue
            rest = rest.replace(part, "\n", 1)
        # ★data: URL は中身を読めない＝検査できない★（Codex 17巡目 (a)-5）
        #   画像に文字を描いて埋め込む等ができるので、そもそも使わせない。
        for hit in _DATA_URL.finditer(rest):
            bad.append(f"{f}: data: URL は使えません {_redact(hit.group(0)[1:])}")
        text = visible_text(rest)
        # ★語のかたまりではなく「数＋単位」の出現ごとに見る★
        #   日本語は空白で区切れないので、語単位だと文まるごとが1語になり
        #   許可リストが作れない。出現そのものを取り出して照合する。
        # ★ページ別・出現回数つきで消費する★（Codex 26巡目）
        budget = dict((allowed_counts or {}).get(f, {}))
        for occ in NUMERAL_OCCURRENCE.finditer(text):
            token = occ.group(0)
            if token in HUB_FIXED_NUMERALS:
                continue
            if budget.get(token, 0) > 0:
                budget[token] -= 1
                continue
            bad.append(f"{f}: 裏取りしていない数値 {_redact(token)}"
                       f" 指紋{_fp(token)} … {_around(text, occ.start())}"
                       f"{_prose_key_of(token, prose_all)}")
        # ★数値の無い断定・損得の話も止める★（Codex 14巡目 (a)-5）
        #   散文には台帳が無いので、ゲートの禁止語・要判断語に当たったら公開しない。
        for label, pat in deny_pats:
            for hit in pat.finditer(text):
                bad.append(f"{f}: {label} {_redact(hit.group(0))}"
                           f" 指紋{_fp(hit.group(0))} … {_around(text, hit.start())}"
                           f"{_prose_key_of(hit.group(0), prose_all)}")
    return sorted(bad)


sys.path.insert(0, str(BASE / "scripts"))
from ci_safe import (in_ci as _in_ci, redact as _redact,  # noqa: E402
                     fingerprint as _fp, safe_path as _safe_path)


def _prose_key_of(hit: str, prose_all: dict | None) -> str:
    """当たった語が、手書き散文（hub_prose.json）のどのキーに含まれるかを探す。

    ★原文を出さずに「どこを直せばいいか」を伝えるため★（Codex 18巡目 (b)-1）
      「457文字目」だけでは直せない。JSONのパスを添える。
    """
    if not prose_all:
        return ""

    # ★表示文字列と同じ形（NFKC・不可視文字なし）に均してから探す★
    #   （Codex 19巡目 (b)-1：原文が「２００Ｇ」だと見つけられず誤診していた）
    target = _INVISIBLE.sub("", unicodedata.normalize("NFKC", hit or ""))
    found: list = []

    def walk(node, path):
        if isinstance(node, str):
            norm = _INVISIBLE.sub("", unicodedata.normalize("NFKC", node))
            if target and target in norm:
                found.append(path)
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(prose_all, "$")
    if not found:
        return " / 散文には無い（生成器のコード側）"
    # ★キー名に原稿を書けるので、CIではパスも伏せる★
    shown = " ".join(_safe_path(x) for x in found[:3])
    more = f" ほか{len(found) - 3}件" if len(found) > 3 else ""
    return f" / 散文の場所 {shown}{more}"


def _around(text: str, pos: int, width: int = 24) -> str:
    """どこで引っかかったか分かるように前後を少し出す（Codex 15巡目 (b)-3）。

    ★CI（GitHub Actions）では原文を出さない★（Codex 17巡目 (a)-6）
      Actionsのログは公開されるので、未公開の原稿がそこに出てしまう。
      CIでは位置だけ、手元では前後の文脈を出す。
    """
    if _in_ci():
        return f"（{pos}文字目付近・原文はCIログに出しません）"
    s = text[max(0, pos - width):pos + width].replace("\n", " ").strip()
    return f"«…{s}…»"


def load_rows(source: "Path | None" = None):
    """一覧のもとになる機種データを読む。

    ★公開時は必ず公開データ（assets/data/public/machines.public.json）から★
      （2026-07-30・Codex 13巡目 (a)-1）
      authoring を直接読むと、ランキングやスペック欄に射影で消えるはずの値が出る。
    """
    import sys as _s2
    _s2.path.insert(0, str(BASE / "scripts"))
    import safe_json as _sj
    machines = _sj.read_rows(source or MACHINES)
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
    # ★先行記事は strategy があっても分類を断定しない★（Codex 18巡目・二重防御）
    #   公開射影が strategy を落とすので通常は来ないが、生成器側でも守る。
    if r.get("status") == "preview":
        return "解析待ち（先行記事）"
    s = (r.get("strategy") or "").strip()
    if s:
        return s
    # ★先行記事（解析待ち）に分類を断定しない★（Codex 15巡目 (b)-1 / 17巡目 (b)-1）
    #   狙い目が空なだけで「設定狙い向け」と書くと、まだ分からないことを断定してしまう。
    #   一覧からは外さず（早見表は全機種の表なので）、書き方だけを正しくする。
    if r.get("status") == "preview":
        return "解析待ち（先行記事）"
    # ★分類を断定しない★（Codex 25巡目 (a)-1）
    #   狙い目が空なだけで「設定狙い向け」と書くのは、公開データで裏取りしていない分類。
    return "狙い目情報なし"


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
        and not r["has_cycle"] and r["limit"] < SHALLOW_TENJO_LIMIT
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
<!-- AdSenseローダーは2026-07-24に全停止（Phase 0）。承認ゲート実装後に承認済みページのみ再開 -->
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


def _meta_len_of(file: str, prose_all: dict) -> str:
    """そのページの meta description（長さの確認用）。"""
    key = {"guide-tenjo-ranking.html": "tenjo", "guide-reset-ranking.html": "reset",
           "guide-suru-tenjo.html": "suru", "guide-ichiran.html": "ichiran"}.get(file, "")
    return (prose_all.get(key) or {}).get("meta_description", "")


def _build_pages(rows: list, prose_all: dict) -> tuple:
    """機種データと散文から、ハブ4ページのHTMLを組み立てる（書き込みはしない）。"""
    # ランキング系は先行記事を除く（未確定の数値で順位を付けない）
    ranked = [r for r in rows if r.get("status") != "preview"]
    A = dataset_A(ranked)
    C = dataset_C(ranked)
    D = dataset_D(ranked)
    ALL = rows  # 早見表は全機種（先行記事は「解析待ち」表記）

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
        f"G数でカウントする天井が<span class=\"list-count\">{SHALLOW_TENJO_LIMIT}</span>G"
        f"未満の機種は全<span class=\"list-count\">{len(A)}</span>機種です"
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

    # ★★検査は「生成後の最終HTML」に対して行う★★（Codex 12巡目 (a)-5）
    #   入力（hub_prose.json）だけを見ていたので、生成器のコード内に書いた
    #   「1000G未満」「スルーN回」などの数値は検査を素通りしていた。
    built = {f: build_page(f, prose, data_html)
             for f, (prose, data_html) in pages.items()}
    return (built, {f: d for f, (_p, d) in pages.items()},
            _counts_allowed(len(A), len(C), len(C_top), len(D), len(ALL)))


def render_all(source_root: "Path") -> dict:
    """公開用のハブ4ページを**描くだけ**（書き込みはしない）。

    ★条件7の設計★（2026-07-30・Codex 23巡目）
    """
    import sys as _s
    _s.path.insert(0, str(BASE / "scripts"))
    import gates as _g
    import safe_json as _sj

    pub = source_root / "assets" / "data" / "public" / "machines.public.json"
    if not pub.is_file():
        raise RuntimeError(f"公開データがありません: {pub}")
    rows = load_rows(pub)
    prose_all = _sj.read_json(source_root / "scripts" / "hub_prose.json", expect=dict)
    built, data_html, allowed = _build_pages(rows, prose_all)
    bad = hub_content_problems(built, data_html,
                               ("公開できない表現", _g.ABSOLUTE_DENY_PAT),
                               ("要人手確認の語（損得・設定の話）", _g.RISK_PAT),
                               prose_all=prose_all,
                               allowed_counts=allowed)
    if bad:
        raise RuntimeError("ハブに出せない内容があります:\n  " + "\n  ".join(bad))
    return built


def main(preview: bool = False):
    """preview=True のときは .preview-site/ にだけ書く（公開されない写し）。

    ★2026-07-30・移行手順2で --allow-ungated を廃止した★（理由は build_machine_pages.py 参照）
    """
    # ★★ハブ4ページもゲート外だった★★（Codex 10巡目 (a)-4）
    #   tenjo_display / strategy / checker閾値 をそのままランキングHTMLへ出すので、
    #   誤った値を書いて本スクリプトを回せば公開ゲートを通らず公開される。
    import sys as _sys
    _sys.path.insert(0, str(BASE / "scripts"))
    import build_public_data as _bpd
    import preview_site as _pv

    # ★公開物はリポジトリ直下に書かない★（2026-07-30・Codex 22巡目 条件7）
    #   ここが直接 machines/{slug}/index.html を書けると、
    #   「公開物の書込み経路は artifact 1本」と言えない（ブランチ直配信が生きている間は特に）。
    #   公開用の書き出しは build_pages_artifact.py が --out で置き場所を渡す時だけ許す。
    # ★公開物を書けるのは build_pages_artifact.py だけ★（Codex 23巡目 条件7）
    if not preview:
        print("★公開用のハブ4ページはここからは作れません★")
        print("  公開物は build_pages_artifact.py が組み立てます。")
        print("  裏取り前の内容を見たいだけなら --preview を付けてください。")
        return 1
    out_root = _pv.PREVIEW_DIR
    try:
        gate_on = _bpd.claim_gate_enabled()
    except Exception as e:
        if not preview:
            print(f"★出典の裏取りゲートの設定が読めません: {e}")
            return 1
        print(f"（写し）出典の裏取りゲートの設定が読めません: {e} — 全機種を写します")
        gate_on = False
    if preview:
        # 写しは裏取り前の内容を見るためのもの。止めずに全機種を出す。
        rows = load_rows()
        _pv.ensure_scaffold()
        print(f"☆写しを作ります（公開されません）: {out_root.name}/ ☆")
        gate_on = False
    elif gate_on:
        # ★一覧も公開データから作る★（Codex 13巡目 (a)-1）
        pub_file = BASE / "assets" / "data" / "public" / "machines.public.json"
        if not pub_file.is_file():
            print("★公開データがありません（先に build_public_data.py --apply を実行）★")
            print(f"  期待した場所: {pub_file}")
            return 1
        try:
            rows = load_rows(pub_file)
        except Exception as e:
            print(f"★公開データが読めません: {e}")
            return 1
        import claim_reconcile as _cr
        blocked = []
        for r in rows:
            try:
                ok, why = _cr.publish_gate(r.get("slug"))
            except Exception as e:
                ok, why = False, [f"検査が例外で失敗: {e}"]
            if not ok:
                blocked.append((r.get("slug"), why))
        if blocked:
            print(f"出典の裏取りゲート: ★有効★ → {len(blocked)} 機種を一覧から外します")
            for s_, why in blocked:
                for ln in (why or []):   # ★全理由を出す★（Codex 11巡目 (b)-1）
                    print(f"  ✗ {s_}: {ln}")
            ng = {s for s, _ in blocked}
            rows = [r for r in rows if r.get("slug") not in ng]
        # ★★空の一覧を成功として書き出さない★★（Codex 11巡目 (b)-5）
        if not rows:
            print("★公開できる機種が1件も無いのでハブ4ページは作りません★")
            return 1
    else:
        print("★出典の裏取りゲートが無効なので公開用のハブ4ページは作りません★")
        print("  裏取り前の内容を確かめたいなら --preview を付けてください")
        print("  （.preview-site/ にだけ書き出します。公開されません）")
        return 1
    import safe_json as _sj3
    prose_all = _sj3.read_json(PROSE, expect=dict)
    # ★★固定文に埋まった数値もゲートの外だった★★（Codex 11巡目 (a)-3）
    #   一覧から機種を外しても「うみねこ2は200G」等の記述は本文に残る。
    #   ゲート有効時は、単位つきの数値を含む固定文を出さない。
    #   ★検査器は1本にした★（Codex 16巡目 (a)-4）
    #     入力（hub_prose.json）と生成後HTMLで別々の検出器を持っていたため、
    #     単位の集合がズレて「どちらかだけ通る」状態になっていた。
    #     いまは生成後HTMLに対する hub_content_problems() 1本で見る（散文も含まれる）。

    # ★先行記事（解析待ち）は一覧に残すが、分類は断定しない★（Codex 17巡目 (b)-1）
    #   早見表は「全機種の表」なので外すと件数が合わなくなる。
    #   代わりに yome() が「解析待ち（先行記事）」を返す。sitemap には載せない。
    previews = [r for r in rows if r.get("status") == "preview"]
    if previews:
        print(f"先行記事（解析待ち）{len(previews)} 機種は「解析待ち」と表記します: "
              f"{[r['slug'] for r in previews]}")

    built, data_html_map, allowed = _build_pages(rows, prose_all)
    if gate_on:
        import gates as _g
        # ★数値は「公開データに載っている値」だけ許す★（Codex 14巡目 (b)-1）
        #   以前は単位つき数値をすべて未検証扱いにしていたため、
        #   生成器自身が出す「1000G未満」で必ず止まり、
        #   しかも警告文が原因を正しく表していなかった。
        bad = hub_content_problems(built, {f: d for f, (_p, d) in pages.items()},
                                   ("公開できない表現", _g.ABSOLUTE_DENY_PAT),
                                   ("要人手確認の語（損得・設定の話）", _g.RISK_PAT),
                                   prose_all=prose_all,
                                   allowed_counts=allowed)
        if bad:
            print(f"★生成後のHTMLに出せない内容が {len(bad)} 箇所あります★")
            for b in bad:      # ★打ち切らない★（Codex 14巡目 (b)-4）
                print(f"  ✗ {b}")
            print("  裏取り／文言修正が済むまでハブ4ページは書き出しません")
            return 1

    for file, html in built.items():
        if preview:
            _pv.write_html(file, html)   # noindex・バナー・目印つきで写しへ
        else:
            # 改行はLF固定（Windowsで作ってもCIで作っても同じ中身にする）
            # ★書き先は out_root★（Codex 23巡目 (a)-2：ここが BASE のままで、
            #   --out を付けてもリポジトリ直下の4ページを上書きできていた）
            out_root.mkdir(parents=True, exist_ok=True)
            (out_root / file).write_text(html, encoding="utf-8", newline="\n")
        dlen = len(_meta_len_of(file, prose_all))
        warn = "" if 50 <= dlen <= 160 else f"  ⚠ meta desc {dlen}字（50〜160推奨）"
        print(f"  生成: {file}  ({dlen}字 desc){warn}")

    print(f"\n完了: {len(built)} ページ生成（写し）。")


def selftest() -> int:
    """ハブ4ページの内容検査の反例を固定する（Codex 14巡目 (a)-5 / (b)-1）。"""
    import sys as _s
    _s.path.insert(0, str(BASE / "scripts"))
    import gates as _g
    deny = (("公開できない表現", _g.ABSOLUTE_DENY_PAT),
            ("要人手確認の語（損得・設定の話）", _g.RISK_PAT))

    ok = 0
    cases = []

    def t(name, cond):
        nonlocal ok
        cases.append(name)
        if cond:
            ok += 1
        print(("✅" if cond else "❌") + " " + name)

    listing = '<ol><li><a href="/machines/x/">機種x</a> 天井 <strong>777G</strong></li></ol>'
    page = f"<html><body><p>説明</p>{listing}</body></html>"
    t("一覧の中の数値は止めない（公開データ由来）",
      hub_content_problems({"a.html": page}, {"a.html": {"list": listing}}, *deny) == [])
    t("一覧を外さなければ検知する（検査が効いていることの確認）",
      hub_content_problems({"a.html": page}, {}, *deny) != [])

    prose = "<html><body><p>この機種は天井200Gです</p></body></html>"
    t("散文に手書きした数値は止める",
      any("裏取りしていない数値" in x
          for x in hub_content_problems({"a.html": prose}, {}, *deny)))

    # ★集計の定義の数値も「データから出す」形にした★（Codex 閉鎖条件1）
    #   例外リストを持たず、<span class="list-count"> に入れることで検査から外れる。
    fixed = ('<html><body><p>G数でカウントする天井が'
             '<span class="list-count">1000</span>G未満の機種は全'
             '<span class="list-count">49</span>機種です</p></body></html>')
    t("集計の定義（件数・閾値）は、値を渡した時だけ通す",
      hub_content_problems({"a.html": fixed}, {}, *deny,
                           allowed_counts={"a.html": {"1000G": 1, "49機種": 1}}) == [])
    twice_same = ('<html><body><p>全<span class="list-count">49</span>機種です。'
                  'もう一度<span class="list-count">49</span>機種。</p></body></html>')
    t("★同じ集計値を2回使ったら止める（回数まで見る）",
      hub_content_problems({"a.html": twice_same}, {}, *deny,
                           allowed_counts={"a.html": {"49機種": 1}}) != [])
    t("★同じ件数が2スロットある場合も2回まで許す（Counterで潰れない）",
      hub_content_problems(
          {"guide-reset-ranking.html":
           '<p>全<span class="list-count">30</span>機種のうち'
           '<span class="list-count">30</span>機種を掲載</p>'},
          {}, *deny,
          allowed_counts=_counts_allowed(0, 30, 30, 0, 0)) == [])

    t("★別ページの許可は流用できない",
      hub_content_problems({"b.html": fixed}, {}, *deny,
                           allowed_counts={"a.html": {"1000G": 1, "49機種": 1}}) != [])

    t("★渡していない集計値は止める（クラス名は免除札にしない）",
      hub_content_problems({"a.html": fixed}, {}, *deny) != [])
    t("★例外リストは空である（固定数値の抜け道を持たない）", HUB_FIXED_NUMERALS == ())

    claim = "<html><body><p>この機種は必ず勝てるため最優先です</p></body></html>"
    t("数値の無い断定も止める",
      hub_content_problems({"a.html": claim}, {}, *deny) != [])

    zenkaku = "<html><body><p>天井は２００Ｇです</p></body></html>"
    t("全角の数値も見つける",
      hub_content_problems({"a.html": zenkaku}, {}, *deny) != [])

    # --- Codex 15巡目 (a)-2 の反例 ---
    split = "<html><body><p>この方法なら勝<strong>て</strong>るため安心です</p></body></html>"
    t("タグで分断した禁止語も見つける",
      hub_content_problems({"a.html": split}, {}, *deny) != [])

    meta = '<html><head><meta name="description" content="必ず勝てるので安心です"></head><body></body></html>'
    t("meta属性の中の断定も見つける",
      hub_content_problems({"a.html": meta}, {}, *deny) != [])

    game = "<html><body><p>天井は200ゲームです</p></body></html>"
    t("「ゲーム」単位の数値も見つける",
      any("裏取りしていない数値" in x
          for x in hub_content_problems({"a.html": game}, {}, *deny)))

    twice = f"<html><body>{listing}<p>説明</p>{listing}</body></html>"
    t("一覧が2回出たら止める（片方だけ除去して素通りさせない）",
      any("1箇所であるべき" in x
          for x in hub_content_problems({"a.html": twice}, {"a.html": {"list": listing}},
                                        *deny)))

    ctx = hub_content_problems({"a.html": "<html><body><p>天井は200Gです</p></body></html>"},
                               {}, *deny)
    t("どこで引っかかったか位置が出る", ctx and "…" in ctx[0])
    # ★CIでは原文をログに出さない★（Codex 17巡目 (a)-6）
    _was = os.environ.get("CI")
    os.environ["CI"] = "true"
    try:
        ci = hub_content_problems({"a.html": "<html><body><p>天井は200Gです</p></body></html>"},
                                  {}, *deny)
        t("CIでは原文を出さない",
          ci and "200G" not in ci[0] and "伏せ字" in ci[0])
    finally:
        if _was is None:
            os.environ.pop("CI", None)
        else:
            os.environ["CI"] = _was

    # --- Codex 16巡目 (a)-4 の反例 ---
    z = "​"
    t("ゼロ幅文字で分断した禁止語も見つける",
      hub_content_problems({"a.html": f"<p>必ず勝{z}てる</p>"}, {}, *deny) != [])
    t("ゼロ幅文字で分断した数値も見つける",
      hub_content_problems({"a.html": f"<p>天井は200{z}Gです</p>"}, {}, *deny) != [])
    t("パーセント表記も単位として見る",
      hub_content_problems({"a.html": "<p>勝率80パーセント</p>"}, {}, *deny) != [])
    t("ふりがなで分断した禁止語も見つける",
      hub_content_problems({"a.html": "<p><ruby>勝<rt>か</rt></ruby>てる</p>"}, {}, *deny) != [])
    # --- Codex 24巡目 (a)-2 の反例 ---
    t("〇G（漢数字のゼロ）も数値として見る",
      hub_content_problems({"a.html": "<p>天井は〇Gです</p>"}, {}, *deny) != [])
    t("小文字の g も単位として見る",
      hub_content_problems({"a.html": "<p>天井は200gです</p>"}, {}, *deny) != [])
    t("免除札（list-count）に断定を入れても止める",
      hub_content_problems(
          {"a.html": '<p><span class="list-count">必ず勝てる</span></p>'}, {}, *deny) != [])

    t("シングルクォート属性の中も見る",
      hub_content_problems({"a.html": "<input placeholder='必ず勝てる'>"}, {}, *deny) != [])

    print(f"\n{ok}/{len(cases)} 合格")
    return 0 if ok == len(cases) else 1


if __name__ == "__main__":
    import argparse as _ap
    _p = _ap.ArgumentParser()
    _p.add_argument("--preview", action="store_true",
                    help="公開されない写し（.preview-site/）にだけ書き出す")
    _p.add_argument("--selftest", action="store_true")
    _a = _p.parse_args()
    if _a.selftest:
        raise SystemExit(selftest())
    # ★どんな壊れた入力でも traceback にしない★（Codex 閉鎖条件5・27巡目）
    import sys as _s9
    _s9.path.insert(0, str(BASE / "scripts"))
    import safe_json as _sj9
    try:
        raise SystemExit(main(_a.preview) or 0)
    except SystemExit:
        raise
    except _sj9.SafeJsonError as _e:
        print(f"★入力データが読めません: {_e}★")
        print("  作業を中止しました（直してから再実行してください）")
        raise SystemExit(1)
    except Exception as _e:
        print(f"★想定外の失敗 {type(_e).__name__}: {_e}★")
        raise SystemExit(1)

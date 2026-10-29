# -*- coding: utf-8 -*-
"""
ハブ/ランキング記事ページ一括生成スクリプト

machines.json（データ）と scripts/hub_prose.json（散文）から、以下4ページを生成する：
    guide-tenjo-ranking.html  天井が浅い機種ランキング   （表 A: G数天井 昇順・1000G未満）
    guide-reset-ranking.html  朝一リセット狙いランキング   （表 C: 狙い目短縮幅 降順 TOP30）
    guide-suru-tenjo.html     スルー天井の機種一覧と狙い方 （表 D: スルー天井 全件）
    guide-ichiran.html        全機種 狙い目・天井 早見表   （表 ALL: 全機種）

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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import page_decision as _pd  # noqa: E402  ★区分の唯一の判定箇所★
import target_display as _td  # noqa: E402  ★狙い目の線の読み方の唯一の場所★

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


def mode_line(c, conf):
    """★その欄で読者が見る狙い目の線★（2026-10-25）

    ★記事の狙い目と同じ数字を出す★＝`target_display.effective_good` を
    既定の交換率（読者が最初に見る交換率）で読む。
    ★直す前は `caution`（様子見の入口＝手前の線）を「狙い目」として出していた★＝
    カバネリはランキングで430G、記事とチェッカーは5.6枚でST間590G。
    読者に「430って何？」と言われて分かった（全機種が手前の低い数字で出ていた）。
    ★画面から外した欄（`_disabled`）は使わない★（読者が選べない）。
    """
    if not isinstance(conf, dict) or _td.is_off(conf):
        return None
    g = _td.effective_good(conf, _td.default_rate(c))
    # ★「狙い目なし」の番兵（設定狙いの99999）は数値として出さない★（2026-10-25・Codex review247）
    if g is None or g >= _td.NO_TARGET:
        return None
    return g


def base_pick(m):
    """リセット比較の基準となる通常時系モード（鍵, 狙い目の線）。
    normalを優先し、無ければmodes宣言順にreset系以外のモード（cz等）を使う
    （基準モードがnormalでない機種＝東京喰種/攻殻/ヴヴヴ2/ダンベル/バキが
    リセットランキングから漏れていた事故の修正・2026-07-13）。"""
    c = m.get("checker") or {}
    if not isinstance(c, dict):
        return None, None
    v = mode_line(c, mode_conf(c, "normal"))
    if v is not None:
        return "normal", v
    for x in (c.get("modes") or []):
        k = mode_key(x)
        if not isinstance(k, str) or "reset" in k.lower():
            continue
        v = mode_line(c, mode_conf(c, k))
        if v is not None:
            return k, v
    for k, v in c.items():
        if k in ("reset", "modeData") or "reset" in str(k).lower() or not isinstance(v, dict):
            continue
        cv = mode_line(c, v)
        if cv is not None:
            return k, cv
    return None, None


def base_line(m):
    return base_pick(m)[1]


def mode_label(c, key, fallback):
    """★記事の狙い目の文と同じ欄の呼び名★（target_display.parts と同じ＝modes の label）

    ★なぜ★＝カバネリは記事で「ST間590G〜」なのに、ランキングだけ「通常」と出ていた。
    """
    for x in (c.get("modes") or []) if isinstance(c, dict) else []:
        if isinstance(x, dict) and x.get("key") == key and x.get("label"):
            return str(x["label"])
    return fallback
    v = mode_line(c, mode_conf(c, "normal"))
    if v is not None:
        return v
    for x in (c.get("modes") or []):
        k = mode_key(x)
        if not isinstance(k, str) or "reset" in k.lower():
            continue
        v = mode_line(c, mode_conf(c, k))
        if v is not None:
            return v
    for k, v in c.items():
        if k in ("reset", "modeData") or "reset" in str(k).lower() or not isinstance(v, dict):
            continue
        cv = mode_line(c, v)
        if cv is not None:
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
                # ★天井ランキングの並びと対象は tenjo_games で決める★（2026-10-25・Codex review246）＝
                #   limit はチェッカーの入力の上限で、天井とは限らなかった（番長4 700／記事699G+α、
                #   マギアレコード 633／記事950pt）。鍵が在って null なら G数で数えない天井＝対象外。
                rank_limit=(m["tenjo_games"] if "tenjo_games" in m
                            else _scalar_limit(m.get("limit"))),
                tenjo_display=m.get("tenjo_display"),
                status=m.get("status", "complete"),
                # ★新台経路(AUTO_*)の区分★（2026-08-04・Codex72回目。
                #   status欠落を complete に化けさせない）
                mclass=_pd.machine_class(m),
                unit=c.get("unit"),
                # スルー天井はモードキー'suru'に加え'through'表記の機種がある
                # （バジ天膳/からくり/まどマギフォルテ/沖ドキDUOアンコールが漏れていた・2026-07-13修正）
                has_suru=bool(c.get("hasSuru") or "suru" in modes or "through" in modes),
                has_cycle=bool(c.get("hasCycle") or "cycle" in modes),
                # ★G数天井を tenjo_games で決めた機種は、周期の天井も持っていても対象★（2026-10-25・Codex review251）
                #   ＝モンキーターンV（795G＋6周期）・ヴァルヴレイヴ2（CZ間999G＋周期）が漏れていた。
                games_set=("tenjo_games" in m),
                nline=base_line(m),
                nlabel=mode_label(c, base_pick(m)[0], "通常"),
                rlabel=mode_label(c, "reset", "リセット後"),
                # ★行ごとに交換率を出す★（2026-10-25・Codex review246）＝北斗は5.6枚・バンドリは等価の線で、
                #   表だけでは交換条件の違う数字を並べていると分からなかった。
                ratelabel=(_td.rate_label(c, _td.default_rate(c)) or _td.NORATE_PREFIX),
                rline=mode_line(c, mode_conf(c, "reset")),
            )
        )
    return rows


def yome(r) -> str:
    # ★先行記事は strategy があっても分類を断定しない★（Codex 18巡目・二重防御）
    #   公開射影が strategy を落とすので通常は来ないが、生成器側でも守る。
    if r.get("status") == "preview":
        return "解析待ち"
    # ★新台経路は「機種ページで確認」★（2026-10-25・2AIの全点検で一致）＝記事には天井・狙い目が
    #   載っているのに「解析待ち」と出ていた。新台の狙い目は指紋がそろったときだけ記事に出る
    #   （target_projection）ので、表では値を写さず記事へ案内する。
    if r.get("mclass", "LEGACY_COMPLETE") != "LEGACY_COMPLETE":
        return AUTO_ROW_TEXT
    s = (r.get("strategy") or "").strip()
    if s:
        return s
    # ★先行記事（解析待ち）に分類を断定しない★（Codex 15巡目 (b)-1 / 17巡目 (b)-1）
    #   狙い目が空なだけで「設定狙い向け」と書くと、まだ分からないことを断定してしまう。
    #   一覧からは外さず（早見表は全機種の表なので）、書き方だけを正しくする。
    if r.get("status") == "preview"             or r.get("mclass", "LEGACY_COMPLETE") != "LEGACY_COMPLETE":
        return "解析待ち"
    # ★分類を断定しない★（Codex 25巡目 (a)-1）
    #   狙い目が空なだけで「設定狙い向け」と書くのは、公開データで裏取りしていない分類。
    return "狙い目情報なし"


AUTO_ROW_TEXT = "機種ページで確認"


def tenjo_disp(r) -> str:
    # machines.json に tenjo_display があれば優先（液晶/実など複数条件天井の一覧表記用）
    td = r.get("tenjo_display")
    if td:
        return td
    if r.get("mclass", "LEGACY_COMPLETE") != "LEGACY_COMPLETE":
        return AUTO_ROW_TEXT
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
        if r["unit"] == "G" and isinstance(r["rank_limit"], (int, float))
        and (r.get("games_set") or not r["has_cycle"]) and r["rank_limit"] < SHALLOW_TENJO_LIMIT
        and isinstance(r["nline"], (int, float))
    ]
    a.sort(key=lambda r: (r["rank_limit"], r["nline"]))
    return a


def dataset_C(rows):
    c = []
    for r in rows:
        # ★ゲーム数で数える機種だけ★（2026-10-25）＝ゴジラ（pt）が「570G」と出ていた。
        #   ptとGは比べられないので並べない（天井ランキングと同じ扱い）。
        if r["unit"] != "G":
            continue
        if isinstance(r["rline"], (int, float)) and isinstance(r["nline"], (int, float)) and r["nline"] - r["rline"] > 0:
            c.append(dict(diff=r["nline"] - r["rline"], **r))
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
<meta name="theme-color" content="#f6f7fa">
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
    # ★新台経路(AUTO_*)も明示除外★（checker無しの自然除外に頼らない・Codex72回目）
    ranked = [r for r in rows if r.get("status") != "preview"
              and r.get("mclass", "LEGACY_COMPLETE") == "LEGACY_COMPLETE"]
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
        A, lambda r: f'天井 <strong>{esc(tenjo_disp(r))}</strong> ／ 狙い目 {esc(yome(r))}'
    )
    tenjo_note = (
        "※天井の欄はG数の天井です。スルー回数・周期・ポイントなど、ほかの天井は各機種ページで確認してください。"
        "同じ天井ゲーム数の機種は、狙い目ゲーム数が浅い順に掲載しています。"
        # ★list-count は「機種数」の印★（2026-07-31）
        #   ここに付けると監査が 1000 を機種数と読み違える（実際に起きた）。
        #   ゲーム数はただの数字として出す。
        f"G数でカウントする天井が{SHALLOW_TENJO_LIMIT}G"
        f"未満の機種は全<span class=\"list-count\">{len(A)}</span>機種です"
        "（G数で数えない天井だけの機種と、チェッカーに狙い目が無い機種は集計対象外です）。"
    )

    # --- reset ---
    C_top = C[:30]
    reset_list = render_rank_list(
        C_top,
        # ★差（短縮幅）は書かない★（2026-10-25・運営者「リセット後の狙い目ゲーム数があるのに要る？」）
        #   並び順の説明は表の下の注記だけ。
        lambda r: f'{esc(r["ratelabel"])} ／ {esc(r["nlabel"])} <strong>{r["nline"]}G〜</strong> → {esc(r["rlabel"])} <strong>{r["rline"]}G〜</strong>',
    )
    reset_note = (
        "※リセット後に浅くから狙える幅（通常時の狙い目 − リセット後の狙い目）が大きい順。比較の基準は各機種の主要カウンターモード（通常時・CZ間・ST間など）です。"
        "狙い目は各機種の狙い目チェッカーの線で、交換率を選べる機種は最初に表示される交換率の線です。"
        f"チェッカーのデータで短縮を確認できる機種は全<span class=\"list-count\">{len(C)}</span>機種で、上位{len(C_top)}機種を掲載しています。"
    )

    # --- suru ---
    suru_list = render_spec_list(D, lambda r: esc(yome(r)))
    suru_note = (
        f"チェッカーでスルー回数を入れて判定できる機種は全<span class=\"list-count\">{len(D)}</span>機種です。"
        "「N回目で確定」という表記は（N−1）スルーの状態を指す点に注意してください。"
    )

    # --- ichiran ---
    ichiran_list = render_spec_list(
        ALL,
        lambda r: f'{esc(r["info"])}｜天井 <strong>{esc(tenjo_disp(r))}</strong>｜狙い目 {esc(yome(r))}',
    )
    # ★全体の機種数は出さない★（2026-07-31・運営者の判断／Codex同意）
    #   増減のたびに README・運営者情報・散文の数を合わせる必要があり、
    #   実際に何度もずれた。読者にとっての価値も薄い。
    #   条件つきの件数（天井が浅い機種は何件、など）は意味があるので残す。
    # ★並びの説明は書かない★（2026-08-29）＝トップページは人気の高い順に毎週入れ替わるが、
    #   このページは公開ゲートが開くまで作り直せないので並びが揃わない。
    #   ★言えないことを言わない★（元は「稼働率順」と書いていた）。
    ichiran_note = ("機種名をタップすると各詳細ページへ移動します。"
                    "天井の欄はG数の天井です。スルー回数・周期・ポイントなど、ほかの天井は各機種ページで確認してください。")

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


# ★★トップページの「掲載機種の一覧」★★（2026-10-29）
#   ★なぜ★＝トップの生のHTMLに記事へのリンクが0本だった（一覧はJSで後から描く）。
#   Googleは最初のHTMLのリンクから先に辿るので、全機種への普通のリンクを最初から置く。
#   ★作り直すのは --legacy だけ★（毎週月曜の並べ替え・対話・朝タスクの作り直し）＝
#   --check と夜の新台公開は見ない（夜が index.html を書かずに済むように・Codex review index_links_1 の案B）。
#   ★新台がこの一覧に載るのは次の --legacy まで遅れる★（JSの一覧と早見表には即日載る）。
#   ★壊れた・消えた・死にリンクは監査59が止める／載っていない機種は監査60が知らせるだけ★
LINKS_PAGE = "index.html"
LINKS_BEGIN = "<!-- MACHINE-LINKS-BEGIN -->"
LINKS_END = "<!-- MACHINE-LINKS-END -->"
_LINK_RE = re.compile(r'<a class="all-machines-item" href="machines/([a-z0-9_]+)/">')
_ANY_A_RE = re.compile(r"<a(?=[\s>/])", re.I)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def machine_links_html(rows: list) -> str:
    """★機種一覧の並び（machines.json の順）のまま、全機種へのリンクを作る★"""
    items = []
    for m in rows:
        if not isinstance(m, dict) or not re.fullmatch(r"[a-z0-9_]+", str(m.get("slug") or "")):
            raise ValueError(f"機種の slug が読めません: {m!r}"[:200])
        if not str(m.get("name") or "").strip():
            raise ValueError(f"機種名が空です: {m.get('slug')}")
        items.append(f'      <a class="all-machines-item" href="machines/{m["slug"]}/">{esc(m["name"])}</a>')
    if not items:
        raise ValueError("機種が1件もありません")
    return "\n".join(['    <div class="all-machines">'] + items + ["    </div>"])


def _links_span(html: str):
    if html.count(LINKS_BEGIN) != 1 or html.count(LINKS_END) != 1:
        raise ValueError(f"{LINKS_PAGE} の機種一覧の印が1組ではありません")
    a = html.index(LINKS_BEGIN) + len(LINKS_BEGIN)
    b = html.index(LINKS_END)
    if b < a:
        raise ValueError(f"{LINKS_PAGE} の機種一覧の印の順が逆です")
    return a, b


def splice_machine_links(html: str, rows: list) -> str:
    """★印のあいだだけを作り直す★（印が1組でなければ例外＝書かない）"""
    a, b = _links_span(html)
    return html[:a] + "\n" + machine_links_html(rows) + "\n    " + html[b:]


def machine_links_problems(html: str, rows: list, base=None) -> list:
    """★トップの機種一覧が壊れていないか★（監査59・夜の公開でも走るので「最新か」は見ない）

    ①印が1組・正しい順 ②リンクが1本以上 ③どれも機種一覧にある機種で、記事ページの実体がある
    ④同じ機種が2回出ない ⑤印のあいだに、決まった形のリンク以外の <a> が無い
    """
    try:
        a, b = _links_span(html)
    except ValueError as e:
        return [str(e)]
    # ★コメントの中はリンクではない★（印の内側を丸ごとコメントにしても通っていた・Codex review 772ffbb3 の4）
    body = _COMMENT_RE.sub("", html[a:b])
    slugs = _LINK_RE.findall(body)
    ng = []
    if not slugs:
        ng.append("トップの機種一覧にリンクが1本もありません")
    # ★大文字・改行つきの <a も数える★（ブラウザはどれもリンクとして読む）
    if len(_ANY_A_RE.findall(body)) != len(slugs):
        ng.append("トップの機種一覧に、決まった形でないリンクがあります")
    known = {str(m.get("slug")) for m in rows if isinstance(m, dict)}
    root = base if base is not None else BASE
    seen = set()
    for s in slugs:
        if s in seen:
            ng.append(f"トップの機種一覧に {s} が2回あります")
        seen.add(s)
        if s not in known:
            ng.append(f"トップの機種一覧の {s} は機種一覧にありません（消えた機種への死にリンク）")
        elif not (root / "machines" / s / "index.html").is_file():
            ng.append(f"トップの機種一覧の {s} の記事ページがありません（死にリンク）")
    return ng


def machine_links_missing(html: str, rows: list) -> list:
    """★まだトップの一覧に載っていない機種★（監査60・知らせるだけ）"""
    try:
        a, b = _links_span(html)
    except ValueError:
        return []
    have = set(_LINK_RE.findall(_COMMENT_RE.sub("", html[a:b])))
    return [str(m.get("slug")) for m in rows if isinstance(m, dict) and str(m.get("slug")) not in have]


def _build_legacy() -> int:
    """★いま公開中のハブ4ページを作り直す★（2026-08-30）

    ★4ページ全部を描けたときだけ書く★（1枚でも欠けたら1枚も書かない）

    ★中身は publish_new_machine.build_hubs() と同じ関数から作る★＝
      あちらは「いまのデータから作った内容と repo が一致するか」を見張るので、
      ★別の作り方をすると、その晩の新台公開が丸ごと止まる★。
    """
    import sys as _sys
    _sys.path.insert(0, str(BASE / "scripts"))
    import safe_json as _sj2

    try:
        rows = load_rows()
        prose = _sj2.read_json(PROSE, expect=dict)
        built, _data_html, _allowed = _build_pages(rows, prose)
        marked = build_marked_pages()      # ★手書きページの印も、全部読めてから書く★
        # ★トップの機種一覧は --legacy でだけ作り直す★（--check と夜の新台公開は見ない・2026-10-29）
        marked[LINKS_PAGE] = splice_machine_links(marked[LINKS_PAGE], _sj2.read_rows(MACHINES))
    except Exception as e:                                   # noqa: BLE001
        print(f"★描けませんでした: {type(e).__name__}: {e} → 何も書きません")
        return 1

    if len(built) != 4:
        print(f"★4ページそろっていません（{sorted(built)}）→ 何も書きません★")
        return 1

    n = 0
    for rel, html in list(built.items()) + list(marked.items()):
        p = BASE / rel
        if p.is_file() and p.read_text(encoding="utf-8") == html:
            continue
        p.write_text(html, encoding="utf-8", newline="\n")
        print(f"  書きました: {rel}")
        n += 1
    print(f"★ハブ4ページと印のあるページを作り直しました★（変わったのは {n} ページ）")
    return 0


# ★★手書きのページに埋める「データの印」★★（2026-10-25）
#   ★なぜ★＝ガイドに手書きした機種の狙い目が記事と食い違っていた
#   （北斗「等価550／5.6枚現金680」、ToLOVEる「リセット後70G〜」など）。
#   ★狙い目の数字は手で書かず、印を置いてここで埋める★：
#     <span data-dv="target:hokuto:eq56:normal">570G〜</span>  … その交換率・その欄の狙い目
#     <span data-dv="ratelabel:hokuto:eq56">5.6枚</span>         … 交換率の呼び名
#     <span data-dv="modelabel:hokuto:reset">リセット</span>      … 欄の呼び名
#     交換率を持たない機種は交換率に「-」を書く（target:slug:-:normal）。
#   ★4ページの生成（_build_pages）には混ぜない★＝新台公開が4ページの集合を要求している（Codex review247）。
#   ★読めない印は例外で止める★（無い機種・選べない交換率・無い欄・画面から外した欄・狙い目なし）。
MARK_PAGES = ("index.html", "guide-haena.html", "guide-rate.html", "guide-reset.html",
              "guide-yamedoki.html", "guide-pochipochi.html")
_MARK_RE = re.compile(r'(<span data-dv="([^"]+)">)(.*?)(</span>)', re.S)


def _mark_text(key: str, by_slug: dict) -> str:
    parts = key.split(":")
    kind = parts[0]
    if kind not in ("target", "ratelabel", "modelabel"):
        raise ValueError(f"知らない印です: {key}")
    if len(parts) < 2 or parts[1] not in by_slug:
        raise ValueError(f"機種がありません: {key}")
    c = by_slug[parts[1]].get("checker") or {}
    rates = {str(r.get("key")): str(r.get("label") or "")
             for r in (c.get("exchangeRates") or []) if isinstance(r, dict)}
    modes = {mode_key(x): (x.get("label") if isinstance(x, dict) else None)
             for x in (c.get("modes") or [])}

    def _rate(rk):
        if rk == "-":
            if rates:
                raise ValueError(f"交換率を選べる機種です（交換率を書いてください）: {key}")
            return ""
        if rk not in rates:
            raise ValueError(f"その交換率は選べません: {key}")
        return rk

    if kind == "ratelabel":
        if len(parts) != 3:
            raise ValueError(f"印の形が違います: {key}")
        return rates[_rate(parts[2])] if parts[2] != "-" else ""
    if kind == "modelabel":
        if len(parts) != 3 or parts[2] not in modes:
            raise ValueError(f"その欄はありません: {key}")
        return str(modes[parts[2]] or parts[2])
    if len(parts) != 4:
        raise ValueError(f"印の形が違います: {key}")
    rk = _rate(parts[2])
    if parts[3] not in modes:
        raise ValueError(f"その欄はありません: {key}")
    conf = mode_conf(c, parts[3])
    if not isinstance(conf, dict) or _td.is_off(conf):
        raise ValueError(f"画面に出ていない欄です: {key}")
    g = _td.effective_good(conf, rk)
    if g is None or g >= _td.NO_TARGET:
        raise ValueError(f"狙い目がありません: {key}")
    return f"{g}{c.get('unit') or 'G'}〜"


def fill_marks(html: str, by_slug: dict) -> str:
    """★印の中身をデータから作り直す★（読めない印があれば例外）"""
    return _MARK_RE.sub(lambda m: m.group(1) + esc(_mark_text(m.group(2), by_slug)) + m.group(4), html)


def check_set(built: dict, marked: dict) -> dict:
    """★--check が比べるページ＝4ページ＋印のあるページ★（片方だけにしない）"""
    return dict(built, **marked)


def build_marked_pages() -> dict:
    """{置き場: 印を埋めた中身}（全部そろってから返す＝1つでも読めなければ例外）"""
    import safe_json as _sjm
    ms = _sjm.read_rows(MACHINES)
    by_slug = {m.get("slug"): m for m in ms if isinstance(m, dict)}
    out = {}
    for rel in MARK_PAGES:
        p = BASE / rel
        out[rel] = fill_marks(p.read_text(encoding="utf-8"), by_slug)
    return out


# ★このスクリプトが作り直す4ページ★（2026-09-01）
#   ★件数ではなく名前で持つ★＝「1件欠落＋別名1件」を件数だけでは見抜けない。
LEGACY_PAGES = ("guide-tenjo-ranking.html", "guide-reset-ranking.html",
                "guide-suru-tenjo.html", "guide-ichiran.html")


def stale_pages(built: dict, read) -> list:
    """★どのページが古いか★だけを返す（読み書きしない・2026-09-01）。

    built = {置き場: いまのデータから作った中身}
    read(置き場) = repo にある中身（無ければ None）

    ★無いページも「古い」に数える★＝
      「作り直したら生まれる」＝いまの repo には無い、なので直す必要がある。
      ★ここを「無ければ飛ばす」にすると、ページが消えた事故が素通りする★。
    """
    out = []
    for rel in sorted(built):
        got = read(rel)
        if got is None:
            out.append(f"{rel}（ありません）")
        elif got != built[rel]:
            out.append(rel)
    return out


def _check_legacy() -> int:
    """★書かずに★、いまの早見表が古くなっていないかだけを見る（2026-09-01）。

    ★`--legacy` と同じ材料・同じ関数で作って、repo の中身と比べるだけ★
      ＝「作り直したら変わる」なら古い。
    ★1ページでも描けなければ「分からない」ではなく止める★（fail-closed）＝
      描けない状態で push すると、その晩の新台公開が丸ごと止まる。
    """
    import sys as _sys
    _sys.path.insert(0, str(BASE / "scripts"))
    import safe_json as _sj2

    try:
        rows = load_rows()
        prose = _sj2.read_json(PROSE, expect=dict)
        built, _d, _a = _build_pages(rows, prose)
        marked = build_marked_pages()
    except Exception as e:                                   # noqa: BLE001
        print(f"★早見表を描けません: {type(e).__name__}: {e}★")
        return 1

    # ★★件数ではなく、期待する4ファイルの集合で見る★★
    #   （2026-09-01・Codexの助言）＝件数だけだと
    #   「1件欠落＋別名1件」でも4件になり、そのまま通る。
    if set(built) != set(LEGACY_PAGES):
        print(f"★作れたページが想定と違います★ 期待={sorted(LEGACY_PAGES)} "
              f"／ 実際={sorted(built)}")
        return 1

    def _read(rel):
        p = BASE / rel
        return p.read_text(encoding="utf-8") if p.is_file() else None

    stale = stale_pages(check_set(built, marked), _read)
    if stale:
        print("★早見表が古いままです★ → " + " / ".join(stale))
        print("  直し方: python scripts/build_hub_pages.py --legacy")
        return 1
    print("早見表4ページと印のあるページ: いまのデータと一致しています")
    return 0


def main(preview: bool = False, legacy: bool = False):
    """preview=True のときは .preview-site/ にだけ書く（公開されない写し）。

    ★2026-07-30・移行手順2で --allow-ungated を廃止した★（理由は build_machine_pages.py 参照）
    """
    import sys as _sys
    _sys.path.insert(0, str(BASE / "scripts"))
    import preview_site as _pv

    # ★書き出せるのは2通りだけ★（2026-10-01・検査スイッチの撤去で公開物の組み立て経路は無い）
    #   --legacy … いま公開中のハブ4ページを作り直す
    #   --preview … 公開されない写し（.preview-site/）に書く
    if legacy:
        return _build_legacy()
    if not preview:
        print("★--legacy（いま公開中のページを作り直す）か --preview（公開されない写し）を付けてください★")
        return 1
    out_root = _pv.PREVIEW_DIR
    rows = load_rows()
    _pv.ensure_scaffold()
    print(f"☆写しを作ります（公開されません）: {out_root.name}/ ☆")
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

    # --- ★早見表が古いままかを見る（2026-09-01新設）★ ---
    #   ★実際に起きたこと★＝並べ替えたのに作り直さず、CIが3回赤くなった。
    #   実測で、既存の自己試験3本はこれを1本も見ていなかった。
    _built = {"guide-a.html": "AAA", "guide-b.html": "BBB"}
    t("早見表：そろっていれば何も言わない",
      stale_pages(_built, lambda r: _built[r]) == [])
    t("早見表：中身が違えば古いと言う",
      stale_pages(_built, lambda r: "AAA" if r == "guide-a.html" else "ZZZ")
      == ["guide-b.html"])
    t("★早見表：ページが無いのも古いに数える★",
      stale_pages(_built, lambda r: None if r == "guide-a.html" else "BBB")
      == ["guide-a.html（ありません）"])
    t("早見表：2ページとも違えば2件とも挙げる",
      stale_pages(_built, lambda r: "ZZZ") == ["guide-a.html", "guide-b.html"])
    t("★早見表：空文字と「無い」を混同しない★",
      stale_pages({"guide-a.html": ""}, lambda r: None)
      == ["guide-a.html（ありません）"])
    t("早見表：中身が空同士なら古くない",
      stale_pages({"guide-a.html": ""}, lambda r: "") == [])

    # ★★ランキングの数字は記事の狙い目と同じ線★★（2026-10-25）＝
    #   直す前は caution（様子見の入口）を「狙い目」として出していた（カバネリ430G）。
    _ck = {"unit": "G", "exchangeRates": [{"key": "eq56", "label": "5.6枚"},
                                          {"key": "rate50", "label": "5.0枚"}],
           "defaultRate": "eq56",
           "normal": {"caution": 430, "good": 580,
                      "byRate": {"eq56": {"good": 590}, "rate50": {"good": 670}}},
           "reset": {"caution": 0, "good": 100}}
    t("★ランキング：様子見の入口ではなく狙い目の線を出す★",
      base_line({"checker": _ck}) == 590)
    t("★ランキング：リセット後も狙い目の線を出す★",
      mode_line(_ck, mode_conf(_ck, "reset")) == 100)
    _ck2 = {"unit": "G", "normal": {"caution": 500, "good": 630}}
    t("ランキング：交換率の無い機種は直下の狙い目",
      base_line({"checker": _ck2}) == 630)
    _ck3 = {"unit": "G", "normal": {"caution": 500, "good": 630, "_disabled": True},
            "cz": {"caution": 300, "good": 400}}
    t("★ランキング：画面から外した欄は使わない★",
      base_line({"checker": _ck3}) == 400)
    _ck4 = dict(_ck, modes=[{"key": "normal", "label": "ST間"}, {"key": "reset", "label": "リセット"}])
    t("★ランキング：欄の呼び名は記事と同じ（ST間）★",
      mode_label(_ck4, base_pick({"checker": _ck4})[0], "通常") == "ST間")
    _rows_u = [dict(unit="pt", nline=570, rline=150), dict(unit="G", nline=590, rline=100)]
    t("★リセットランキング：ゲーム数で数える機種だけ並べる★",
      [r["unit"] for r in dataset_C(_rows_u)] == ["G"])
    _bs = {"h": {"checker": dict(_ck, modes=[{"key": "normal", "label": "通常"},
                                             {"key": "reset", "label": "リセット"}])},
           "n": {"checker": {"unit": "G", "modes": [{"key": "normal", "label": "通常"}],
                             "normal": {"good": 630}}},
           "s": {"checker": {"unit": "G", "modes": [{"key": "normal", "label": "設定狙い"}],
                             "normal": {"good": 99999}}}}
    t("★印：その交換率・その欄の狙い目を埋める★",
      fill_marks('<span data-dv="target:h:rate50:normal">x</span>', _bs)
      == '<span data-dv="target:h:rate50:normal">670G〜</span>')
    t("印：交換率と欄の呼び名を埋める",
      fill_marks('<span data-dv="ratelabel:h:eq56">x</span><span data-dv="modelabel:h:reset">y</span>', _bs)
      == '<span data-dv="ratelabel:h:eq56">5.6枚</span><span data-dv="modelabel:h:reset">リセット</span>')
    t("印：交換率の無い機種は「-」",
      fill_marks('<span data-dv="target:n:-:normal">x</span>', _bs)
      == '<span data-dv="target:n:-:normal">630G〜</span>')

    def _raises(h):
        try:
            fill_marks(h, _bs)
            return False
        except ValueError:
            return True
    t("★印：選べない交換率は止める（共通の値へ黙って戻さない）★",
      _raises('<span data-dv="target:h:equal:normal">x</span>'))
    t("★印：狙い目なし（99999）を数字として出さない★",
      _raises('<span data-dv="target:s:-:normal">x</span>'))
    t("印：無い欄・無い機種・知らない印は止める",
      _raises('<span data-dv="target:h:eq56:cz">x</span>')
      and _raises('<span data-dv="target:zz:eq56:normal">x</span>')
      and _raises('<span data-dv="price:h">x</span>'))
    t("★新台経路の行は「機種ページで確認」（解析待ちと言わない）★",
      yome({"mclass": "AUTO_INDEXABLE", "strategy": ""}) == AUTO_ROW_TEXT
      and tenjo_disp({"mclass": "AUTO_PENDING", "limit": None}) == AUTO_ROW_TEXT)
    _ra = [dict(slug="m", unit="G", rank_limit=795, has_cycle=True, games_set=True, nline=420),
           dict(slug="c", unit="G", rank_limit=700, has_cycle=True, games_set=False, nline=300)]
    t("★天井ランキング：G数天井を決めた機種は周期の天井があっても入れる★",
      [r["slug"] for r in dataset_A(_ra)] == ["m"])
    t("天井の書き方は tenjo_display が優先", tenjo_disp({"tenjo_display": "CZ間600G+α", "limit": 1200}) == "CZ間600G+α")
    t("★点検：印のあるページも比べる★",
      check_set({"guide-a.html": "A"}, {"index.html": "B"}) == {"guide-a.html": "A", "index.html": "B"})
    t("★ランキング：狙い目なし（99999）を数字として出さない★",
      mode_line(_bs["s"]["checker"], _bs["s"]["checker"]["normal"]) is None)
    t("★ランキング：行ごとに交換率の呼び名を出す★",
      _td.rate_label(_ck, _td.default_rate(_ck)) == "5.6枚"
      and (_td.rate_label(_ck2, _td.default_rate(_ck2)) or _td.NORATE_PREFIX) == "チェッカー基準")
    t("ランキング：呼び名が無ければ既定の言い方",
      mode_label(_ck, "reset", "リセット後") == "リセット後")

    # ★★トップの機種一覧★★（2026-10-29）＝印のあいだだけを作り直し、壊れ方は監査59が止める
    def _splice_raises(h, rows):
        try:
            splice_machine_links(h, rows)
            return False
        except ValueError:
            return True
    import tempfile as _tf
    _root = Path(_tf.mkdtemp(prefix="links_"))
    for _s in ("aa", "bb"):
        (_root / "machines" / _s).mkdir(parents=True)
        (_root / "machines" / _s / "index.html").write_text("x", encoding="utf-8")
    _rows = [{"slug": "aa", "name": "機種<A>"}, {"slug": "bb", "name": "機種B"}]
    _pg = f"<main>{LINKS_BEGIN}{LINKS_END}</main><footer>f</footer>"
    _one = splice_machine_links(_pg, _rows)
    t("★印のあいだに全機種へのリンクを置き、名前は文字として出す★",
      _LINK_RE.findall(_one) == ["aa", "bb"] and "機種&lt;A&gt;" in _one
      and _one.startswith("<main>") and _one.endswith("</main><footer>f</footer>"))
    t("★2回作り直しても同じ★（毎回ずれない）", splice_machine_links(_one, _rows) == _one)
    t("★できた一覧は監査59を通る★", machine_links_problems(_one, _rows, _root) == [])
    t("★印が無ければ監査59が止める★",
      any("1組" in x for x in machine_links_problems("<main></main>", _rows, _root)))
    t("★中身が空なら監査59が止める★",
      any("1本も" in x for x in machine_links_problems(_pg, _rows, _root)))
    t("★消えた機種への死にリンクを止める★",
      any("死にリンク" in x for x in machine_links_problems(_one, _rows[:1], _root)))
    (_root / "machines" / "bb" / "index.html").unlink()
    t("★記事ページの実体が無ければ止める★",
      any("記事ページがありません" in x for x in machine_links_problems(_one, _rows, _root)))
    t("★決まった形でないリンクを混ぜたら止める★",
      any("決まった形" in x for x in machine_links_problems(
          _one.replace(LINKS_END, '<a href="https://example.invalid/">x</a>' + LINKS_END), _rows, _root)))
    t("★新台がまだ載っていないだけでは止めない★（夜の公開でも監査が走る）／知らせる側には出る",
      machine_links_problems(splice_machine_links(_pg, _rows[:1]), _rows, _root) == []
      and machine_links_missing(splice_machine_links(_pg, _rows[:1]), _rows) == ["bb"])
    t("★印の内側を丸ごとコメントにしたら止める★（コメントの中はリンクではない）",
      any("1本も" in x for x in machine_links_problems(
          _one.replace(LINKS_BEGIN, LINKS_BEGIN + "<!--").replace(LINKS_END, "-->" + LINKS_END), _rows, _root)))
    t("★大文字や改行つきの <a も決まった形でないリンクとして止める★",
      any("決まった形" in x for x in machine_links_problems(
          _one.replace(LINKS_END, '<A href="x">x</A>' + LINKS_END), _rows, _root))
      and any("決まった形" in x for x in machine_links_problems(
          _one.replace(LINKS_END, '<a\nhref="x">x</a>' + LINKS_END), _rows, _root)))
    t("★印が2組あれば作り直さない★（どこへ書くか決められない）",
      _splice_raises(_pg + _pg, _rows))

    print(f"\n{ok}/{len(cases)} 合格")
    return 0 if ok == len(cases) else 1


if __name__ == "__main__":
    import argparse as _ap
    _p = _ap.ArgumentParser()
    _p.add_argument("--preview", action="store_true",
                    help="公開されない写し（.preview-site/）にだけ書き出す")
    _p.add_argument("--legacy", action="store_true",
                    help="いま公開中のハブ4ページを作り直す")
    _p.add_argument("--check", action="store_true",
                    help="★書かずに★早見表が古くなっていないかだけを見る"
                         "（push前の関所が使う）")
    _p.add_argument("--selftest", action="store_true")
    _a = _p.parse_args()
    if _a.check and (_a.preview or _a.legacy):
        print("★--check は単独で使ってください★")
        raise SystemExit(1)
    if _a.check:
        raise SystemExit(_check_legacy())
    if _a.preview and _a.legacy:
        print("★--preview と --legacy は同時に使えません★")
        raise SystemExit(1)
    if _a.selftest:
        raise SystemExit(selftest())
    # ★どんな壊れた入力でも traceback にしない★（Codex 閉鎖条件5・27巡目）
    import sys as _s9
    _s9.path.insert(0, str(BASE / "scripts"))
    import safe_json as _sj9
    try:
        raise SystemExit(main(_a.preview, _a.legacy) or 0)
    except SystemExit:
        raise
    except _sj9.SafeJsonError as _e:
        print(f"★入力データが読めません: {_e}★")
        print("  作業を中止しました（直してから再実行してください）")
        raise SystemExit(1)
    except Exception as _e:
        print(f"★想定外の失敗 {type(_e).__name__}: {_e}★")
        raise SystemExit(1)

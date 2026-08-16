"""directory_index.py — 名鑑の一覧から「機種名 → 個別ページURL」の索引を作る。

★なぜ要るか（2026-07-31）★
  名鑑のURLを人が手で探していたので、自動タスクが回らなかった。
  メーカー公式から取った正式名称で、この索引を引いて個別ページを見つける。

★検索エンジンは使わない★
  順位や要約に引きずられる。実際、検索結果から「ビンゴライブ」という
  **実在しない機種名**を拾って空振りした。名鑑の一覧を直接読む。

★入口は1つでは足りない（実データで確認）★
  ちょんぼりすたは、全機種一覧に `Lすーぱぁびん娘` が無く、
  メーカー別一覧（/slot/belko-slot/）にはある。
  P-WORLD は /database/machine.html と導入カレンダーの両方にある。
  だから **1つの名鑑につき複数の入口を和集合で見る**。

★「見つからない」と「まだ載っていない」を分ける★
  一覧が壊れて0件になったのを「新台なし」と読むのが一番こわい。
  返す状態を分ける:

    FOUND                個別ページを1つに決められた
    HEALTHY_NO_MATCH     一覧は正常に読めたが、その時点では載っていない
    AMBIGUOUS_CANDIDATES 候補が複数あって決められない → ★止める★
    CATALOG_UNHEALTHY    一覧を正常に読めていない → ★新台なしと扱わない★

使い方:
    python scripts/directory_index.py --name "Lすーぱぁびん娘"
    python scripts/directory_index.py --check chonborista
    python scripts/directory_index.py --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import claim_identity as _ci          # noqa: E402
import new_machine_watch as _w        # noqa: E402
import safe_json as _sj               # noqa: E402

CATALOGS = os.path.join(BASE, "assets", "data", "directory-catalogs.json")

# 一覧のリンク文字が「2026年5月22日 Lすーぱぁびん娘 スロット 新台 天井 …」の形なので、
# 先頭の日付と、後ろに続く記事タイトルの飾りを落として機種名だけにする。
# ★「2026年9月7日導入 」「未定 」で始まる形もある★（2026-08-08・なな徹の索引）
#   落とさないと芯が「導入lパチスロ彼女お借りします」になって一致しない。
#   ★「未定」は後ろに区切りがあるときだけ落とす★（2026-08-09・依頼127）
#     区切りを見ていなかったので「未定義…」のような実在の題でも
#     先頭2文字を削っていた（機種名が変わって引き当てられなくなる）。
_DATE_HEAD = re.compile(
    r"^\s*(?:20\d\d年\s*\d{1,2}月\s*\d{1,2}日\s*(?:導入|登場)?"
    r"|(?:導入日未定|未定)(?=\s|$))\s*")
_TITLE_TAIL = ("設定判別", "解析まとめ", "終了画面", "徹底解説", "打ち方", "狙い目",
               "やめどき", "ヤメ時", "ゾーン", "スペック", "期待値", "情報",
               "設置店", "掲示板", "初打ち", "機械割", "スロット", "パチスロ",
               "スマスロ", "新台", "天井", "解析", "まとめ", "攻略", "示唆",
               "評価", "考察",
               # ★実データで足りなかった飾り語★（2026-08-07・台帳#264）
               #   末尾から剥がすので、**最後に来る語**が無いと1つも剥がせない。
               #   例「ゴーゴージャグラー3新台設定判別機械割6号機」は
               #   「6号機」が無いために丸ごと芯になっていた。
               "6号機", "5号機", "設定差", "最速", "解説", "レビュー",
               "動画", "導入", "実践", "実戦", "打法", "全解析", "設定",
               # DMMのカードの飾り（2026-08-03・実データ）
               "導入開始日", "導入予定日", "導入日", "掲載準備中", "準備中",
               "掲載", "予定")
# 飾り語どうしをつなぐ記号・助詞（これだけが残るなら「飾りだけの塊」とみなす）
_TAIL_JOINERS = set("・、/｜|()（） 　をとのー:：!！?？")
# 日付・曜日の文字（DMMのカードの「導入開始日:2026年09月07日（月）予定」用）
_DATE_CHARS = set("0123456789年月日火水木金土")

_MAKER_WORDS = None


def _maker_word_cores() -> set:
    """★名簿にあるメーカーの言い方すべての芯★（2026-08-03・DMM実データ）

    DMMのカードは機種名の後ろにメーカー名（オリンピア等）を書く。
    名簿の name / id / directory_names を飾りとして落とせるようにする。
    """
    global _MAKER_WORDS
    if _MAKER_WORDS is None:
        out = set()
        try:
            got = _sj.read_json(os.path.join(BASE, "assets", "data",
                                             "maker-catalogs.json"),
                                expect=dict)
            for mid, c in (got.get("catalogs") or {}).items():
                if not isinstance(c, dict):
                    continue
                for w in [c.get("name") or "", mid] + list(
                        c.get("directory_names") or []):
                    core = _ci.normalize_core(str(w))
                    if core:
                        out.add(core)
        except Exception:                 # noqa: BLE001
            pass
        _MAKER_WORDS = out
    return _MAKER_WORDS


def _decor_only(token: str) -> bool:
    """その塊（空白なし）が記事タイトルの飾りだけでできているか。

    ★DMMのカードの形も飾りと数える★（2026-08-03・実データで確認）
      「オリンピア」（名簿のメーカー名）・「機械割:」・「掲載準備中」・
      「導入開始日:2026年09月07日（月）予定」。これらが落ちないと
      芯が伸びて、実在の新台（青ブタ）がDMMで照合不能だった。
    """
    if not token:
        return False
    if _ci.normalize_core(token) in _maker_word_cores():
        return True                       # メーカー名の塊
    t = token
    for w in sorted(_TITLE_TAIL, key=len, reverse=True):
        t = t.replace(w, " ")
    return all(ch in _TAIL_JOINERS or ch == " " or ch in _DATE_CHARS
               for ch in t)

STATES = ("FOUND", "HEALTHY_NO_MATCH", "AMBIGUOUS_CANDIDATES", "CATALOG_UNHEALTHY")


def anchor_core(text: str, aggressive: bool = False) -> str:
    """一覧のリンク文字から「機種名の芯」を作る。

    ★aggressive★＝飾りを1つしか剥がせなくても剥がした形を返す。
      索引には「剥がさない芯」と「剥がした芯」を**両方**入れるので、
      剥がし過ぎても取りこぼしにはならない（別名が1つ増えるだけ）。

    ★記事タイトルの飾りを落とす★
      落とさないと芯が「すーぱぁびん娘新台天井設定判別…」まで伸びて、
      正式名称の芯と一致しなくなる（実データで確認）。

    ★飾りは「右端から・語境界を保って」剥がす★（2026-08-02・Codex51回目）
      最初に現れた飾り語で切ると、機種名の中の「パチスロ」「スロット」で
      名前ごと消える。実在の「Lパチスロうる星やつら」は芯が空になり、
      「Lパチスロ からくりサーカス2」はちょんぼりすたの実一覧で
      既に取りこぼしていた（両方とも実ページで確認）。
    """
    t = _DATE_HEAD.sub("", " ".join(str(text or "").split()))
    # ｜は語の区切りとして扱う（「機種名｜天井・設定判別…」の形が実在）
    toks = [x for x in re.split(r"[ ｜|]+", t) if x]
    # 右端から「飾りだけの塊」を落とす。機種名に届いたら止まる
    while toks and _decor_only(toks[-1]):
        toks.pop()
    t = " ".join(toks)
    # ベタ付きの飾り（…びん娘新台天井）は、末尾に飾り語が2語以上
    # 連続する時だけ剥がす。1語では剥がさない（「アニマルスロット」等、
    # 機種名の一部かもしれないため）
    stripped, n = t, 0
    while True:
        hit = next((w for w in sorted(_TITLE_TAIL, key=len, reverse=True)
                    if stripped.endswith(w) and len(stripped) > len(w)), None)
        if not hit:
            break
        stripped = stripped[:-len(hit)].rstrip("".join(_TAIL_JOINERS))
        n += 1
    # ★aggressive=True なら1つ剥がせただけでも使う★（別の索引キーとして足す用）
    if stripped and (n >= 2 or (aggressive and n >= 1)):
        t = stripped
    return _ci.normalize_core(t)


def build_index(html: str, base_url: str, link_pattern: str,
                title_class: str = "", title_tag: str = "") -> dict:
    """1つの入口から {機種名の芯: [(URL, 元の文字), ...]} を作る。

    ★リンクはHTML解析で読む★（2026-08-02・Codex52回目）
      href=\"...\" の正規表現だと単一引用符のリンクだけを黙って見落とし、
      既存リンクが十分あるページでは面が健全に見えたまま
      新台だけが HEALTHY_NO_MATCH で欠落する。
      解析できなければ0件＝最低件数の警報側に倒れる。
    """
    idx: dict = {}
    rx = re.compile(link_pattern)
    # ★題の場所は「クラス」か「タグ」のどちらか一方で指す★（2026-08-14・依頼188）
    #   両方あると、どちらを見ればよいか決まらない＝設定の誤りとして止める。
    if title_class and title_tag:
        return {"_PROBLEM_": [("", "名簿の設定が誤っています："
                                   "title_class と title_tag は"
                                   "どちらか一方だけにしてください")]}
    _where = title_class or title_tag
    # ★題の場所が決まっている名鑑は、そこだけを読む★（2026-08-06・台帳#189）
    pairs = (_w.visible_anchor_titles(html, title_class, title_tag) if _where
             else _w._visible_anchor_pairs(html))
    # ★題を取れなかったリンクを黙って捨てない★（2026-08-06・Codex123回目）
    #   新しいカードだけ作りが変わって題を取れなくても、既存カードが
    #   最低件数を満たせば「面は正常」に見え、**新台だけ消える**（#189の再発）。
    if _where:
        all_links = [(h, t) for h, t in (_w._visible_anchor_pairs(html) or [])
                     if rx.search(h)]
        got = {h for h, _ in (pairs or []) if rx.search(h)}
        missing = [h for h, _ in all_links if h not in got]
        if missing:
            idx["_PROBLEM_"] = [(
                "", f"題を読めない機種リンクが {len(missing)} 件あります"
                    f"（例: {missing[0]}）。名鑑の作りが変わった可能性があります")]
    for href, text in (pairs or []):
        if not rx.search(href):
            continue
        core = anchor_core(text)
        if not core:
            continue
        url = urllib.parse.urljoin(base_url, href).split("#")[0].split("?")[0]
        # ★飾りを剥がした芯も一緒に索引へ入れる★（2026-08-07・台帳#264）
        #   剥がせた数が1つだけの時は今まで使っていなかったので、
        #   「マギアレコード最速解析まとめ」のような題が引けなかった。
        #   ★元の芯は必ず残す★＝剥がし過ぎても取りこぼしにならない。
        for c in dict.fromkeys([core, anchor_core(text, aggressive=True)]):
            if not c:
                continue
            idx.setdefault(c, [])
            if url not in [u for u, _ in idx[c]]:
                idx[c].append((url, " ".join(text.split())[:60]))
    return idx


# ★ページ送りの「次へ」を追いかける★（2026-08-07・台帳#264）
#   一覧が50件ずつしか出ない名鑑があり、1ページ目だけ見ていたため
#   古い機種が丸ごと索引に入っていなかった。
#   ★上限を必ず置く★＝作りが変わって輪になっても止まる。
NEXT_PAGE_MAX = 40
_NEXT_WORDS = ("次へ", "次の", "次ページ", "次 ", ">>", "»")


def _same_listing(cand: str, base: str) -> bool:
    """そのリンクが「同じ一覧の別ページ」か。★別の一覧へ渡り歩かない★"""
    a, b = urllib.parse.urlsplit(cand), urllib.parse.urlsplit(base)
    if a.hostname != b.hostname:
        return False
    root = b.path.rstrip("/")
    return a.path.rstrip("/") == root or a.path.startswith(root + "/")


def more_pages(html: str, base_url: str) -> list:
    """一覧の「別のページ」へのリンクを集める。

    ★「次へ」だけを追わない★（2026-08-07・実データ）
      DMMの送りは「次へ」が**最終ページ**（75頁目）を指していて、
      そこだけ読むと2〜74頁が丸ごと抜ける。数字のページ送り
      （1 2 3 … ）をたどれば、この取り違えが起きない。
    """
    out = []
    for href, text in _w._visible_anchor_pairs(html):
        t = " ".join(str(text or "").split())
        if not t:
            continue
        num = t.isdigit() and len(t) <= 4
        nxt = any(t.startswith(w) or t == w.strip() for w in _NEXT_WORDS)
        if not (num or nxt):
            continue
        u = urllib.parse.urljoin(base_url, href).split("#")[0]
        if u not in out and _same_listing(u, base_url):
            out.append(u)
    return out


def _surface_pages(url: str, max_pages: int, first_html: str = ""):
    """1つの入口の**2ページ目以降**を順に返す（★同じURLは二度読まない★）。

    1ページ目は呼び出し側が持っている前提。ここで落ちても
    1ページ目を捨てないようにするための切り分け。
    """
    seen, queue = {url}, list(more_pages(first_html, url))
    while queue and len(seen) < max(1, max_pages):
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        html = _w._get(cur)
        yield cur, html
        for u in more_pages(html, url):
            if u not in seen and u not in queue:
                queue.append(u)


def scan_directory(dir_id: str, conf: dict) -> dict:
    """1つの名鑑の全入口を見て、索引と健全性を返す。"""
    out = {"directory": dir_id, "name": conf.get("name"), "index": {},
           "surfaces_ok": 0, "surfaces_total": 0, "problems": []}
    least = int(conf.get("min_expected") or 1)
    # ★何ページまで追うか★（名鑑ごと。0/未設定なら1ページだけ＝従来どおり）
    pages = int(conf.get("max_pages") or (NEXT_PAGE_MAX
                                          if conf.get("follow_next_page")
                                          else 1))
    for sf in conf.get("surfaces") or []:
        out["surfaces_total"] += 1
        try:
            html = _w._get(sf["url"])
        except Exception as e:
            out["problems"].append(f"{sf['url']}: 取得できません（{e}）")
            continue
        # ★2ページ目以降が読めなくても1ページ目を捨てない★（2026-08-07）
        #   ちょんぼりすたの番号送りは実際には404を返す飾りだった。
        #   ここで例外を通していたため、入口ごと落ちて索引が565→224に減った。
        extra = []
        if pages > 1:
            gen = _surface_pages(sf["url"], pages, first_html=html)
            while True:
                try:
                    got = next(gen)
                except StopIteration:
                    break
                except Exception as e:      # noqa: BLE001
                    out["problems"].append(f"{sf['url']}: 2頁目以降（{e}）")
                    break
                extra.append(got)
        idx = build_index(html, sf["url"], conf["link_pattern"],
                          title_class=str(conf.get("title_class") or ""),
                          title_tag=str(conf.get("title_tag") or ""))
        # ★題を読めないカードがあれば、その面は使わない★（#189の再発防止）
        if "_PROBLEM_" in idx:
            out["problems"].append(f"{sf['url']}: {idx['_PROBLEM_'][0][1]}")
            continue
        if len(idx) < least:
            # ★ここが黙って0件になる事故を止める砦★
            out["problems"].append(
                f"{sf['url']}: {len(idx)} 件しか取れません（最低 {least} 件のはず）")
            continue
        out["surfaces_ok"] += 1
        # ★2ページ目以降も同じ入口として足す★（1ページ目の健全さは上で見た）
        for page_url, page_html in extra:
            more = build_index(page_html, page_url, conf["link_pattern"],
                               title_class=str(conf.get("title_class") or ""),
                               title_tag=str(conf.get("title_tag") or ""))
            if "_PROBLEM_" in more:
                out["problems"].append(
                    f"{page_url}: {more['_PROBLEM_'][0][1]}")
                continue
            for core, items in more.items():
                idx.setdefault(core, [])
                for it in items:
                    if it[0] not in [u for u, _ in idx[core]]:
                        idx[core].append(it)
        for core, items in idx.items():          # ★入口どうしは和集合★
            cur = out["index"].setdefault(core, [])
            for it in items:
                if it[0] not in [u for u, _ in cur]:
                    cur.append(it)
    return out


# ★「機種名＋宣伝文句」の見出しを引き当てる★（2026-08-07・台帳#264）
#   ちょんぼりすたの一覧は「マイジャグラーVスペック設定判別ぶどう」のように
#   機種名の**後ろ**に宣伝文句が連なる。飾りを末尾から剥がす作りでは
#   語を1つ足すたびに別の形が現れて追いつかない（実データで確認）。
#   ★探す機種名が分かっているときは、前から当てたほうが確実★
#     ①見出しが機種名でちょうど始まる ②残りが飾りの語だけでできている
#   この2つが揃ったときだけ同じ機種とみなす。
#   ★「マイジャグラーV」で「マイジャグラーVI」を引かない★
#     残りが "i" になり、飾りの語ではないので外れる。
#   ★「ストリートファイターV」で「ストリートファイターV挑戦者の道」も引かない★
_DECOR_WORDS = _TITLE_TAIL + (
    "確率", "ボーナス", "データ", "パターン", "プレミアム", "コメント",
    "みんなの", "評価", "解析", "設定差", "設定判別", "設定", "打ち方",
    "ぶどう", "ベル", "小役", "負け", "勝ち", "実戦", "実践", "期待値",
    "フリーズ", "恩恵", "モード", "示唆", "早見表", "画面", "終了",
    "打法", "立ち回り", "スペック", "天井", "狙い目", "やめ時", "やめどき",
    "解説", "まとめ", "一覧", "動画", "感想", "results", "の",
    "出玉率", "出玉", "差枚", "判別", "推測", "抽選", "確定", "期待",
)
_MAX_DECOR_STEPS = 24


def remainder_is_decor(rest: str) -> bool:
    """機種名の後ろに残った文字が「飾りの語だけ」でできているか。"""
    s = str(rest or "")
    for _ in range(_MAX_DECOR_STEPS):
        if not s:
            return True
        hit = next((w for w in sorted(_DECOR_WORDS, key=len, reverse=True)
                    if s.startswith(_ci.normalize_core(w))
                    and _ci.normalize_core(w)), None)
        if not hit:
            return False
        s = s[len(_ci.normalize_core(hit)):]
    return False


def lookup_hits(index: dict, core: str) -> list:
    """索引から、この機種名にあたる項目を集める。"""
    hits = list(index.get(core) or [])
    if hits:
        return hits
    # ★世代表記の同値化★（公式「…2」↔名鑑「…II」）
    ck = _ci.canon_num_tail(core)
    for k, v in index.items():
        if k != core and _ci.canon_num_tail(k) == ck:
            hits += v
    if hits or not core:
        return hits
    for k, v in index.items():           # ★機種名＋飾り の見出し★
        if k != core and k.startswith(core) and remainder_is_decor(k[len(core):]):
            hits += v
    return hits


def find(*a, **k):
    """★何のために取りに行くかを名乗ってから中身を動かす★

    （2026-08-16・依頼219の指摘1）
    前は共有の値へ直接入れていたので、**抜けたあとも残って**いた。
    残ると、そのあとの「名乗っていない取得」が材料として通ってしまい、
    関所の意味が崩れる。★囲みにして必ず元へ戻す★
    """
    import new_machine_watch as _nwp
    with _nwp.fetching("claim_material"):
        return _find(*a, **k)


def _find(official_name: str, catalogs: dict | None = None) -> dict:
    """正式名称から、各名鑑の個別ページURLを探す。"""
    cats = catalogs or _sj.read_json(CATALOGS, expect=dict)["directories"]
    core = _ci.normalize_core(official_name)
    out = {"official_name": official_name, "core": core, "results": {}}
    if not core:
        out["results"]["_"] = {"state": "CATALOG_UNHEALTHY",
                               "why": "正式名称から芯を作れません"}
        return out
    for dir_id, conf in cats.items():
        if conf.get("status") != "ACTIVE":
            continue
        r = scan_directory(dir_id, conf)
        hits = lookup_hits(r["index"], core)
        if r["surfaces_ok"] == 0:
            state, why = "CATALOG_UNHEALTHY", " / ".join(r["problems"])
        elif len(hits) == 1:
            state, why = "FOUND", ""
        elif len(hits) > 1:
            # ★順位や「新しい方」で選ばない★（Codexの指摘・自分でも妥当と判断）
            state, why = "AMBIGUOUS_CANDIDATES", f"候補が {len(hits)} 件あります"
        else:
            state, why = "HEALTHY_NO_MATCH", "正常に読めた一覧に載っていません"
        out["results"][dir_id] = {
            "state": state, "why": why,
            "url": hits[0][0] if state == "FOUND" else None,
            "candidates": [u for u, _ in hits],
            "surfaces": f"{r['surfaces_ok']}/{r['surfaces_total']}",
            "index_size": len(r["index"]),
            "problems": r["problems"],
        }
    return out


def found_urls(result: dict) -> list:
    return [v["url"] for v in result["results"].values()
            if v["state"] == "FOUND" and v["url"]]


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []
    nl = chr(10)

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    # ★宣伝文句がくっついたままの芯を索引に入れていた★（2026-08-07・台帳#264）
    #   末尾から剥がす作りなので、**最後の語**が飾り一覧に無いと1つも剥がせない。
    #   実データで多数の機種が引けなくなっていた。
    t("★★末尾の宣伝文句を剥がして機種名にする★★（台帳#264の実データ）",
      anchor_core("ゴーゴージャグラー3新台設定判別機械割6号機")
      == _ci.normalize_core("ゴーゴージャグラー3")
      and anchor_core("ネオアイムジャグラーex新台スペック打ち方設定差")
      == _ci.normalize_core("ネオアイムジャグラーEX")
      and anchor_core("マギアレコード最速解析まとめ")
      == _ci.normalize_core("マギアレコード"))
    t("　機種名の一部は剥がさない（別機種と混ざらない）",
      anchor_core("北斗の拳転生の章2") == _ci.normalize_core("北斗の拳転生の章2")
      and anchor_core("北斗の拳転生の章2", aggressive=True)
      != _ci.normalize_core("北斗の拳"))

    _pre = {_ci.normalize_core(k): [("u%d" % i, k)] for i, k in enumerate(
        ["マイジャグラーVスペック設定判別ぶどう", "マイジャグラーVI",
         "ストリートファイターV挑戦者の道", "北斗の拳転生の章2"])}

    def _hit(name):
        return [x[1] for x in lookup_hits(_pre, _ci.normalize_core(name))]

    # ★2ページ目が404でも1ページ目を捨てない★（2026-08-07・実データで発生）
    #   ちょんぼりすたの番号送りは404を返す飾りで、これを通していたため
    #   入口ごと落ちて索引が565→224件に減った。
    _pages_conf = {"surfaces": [{"url": "https://ex.test/list/"}],
                   "link_pattern": r"/m/\d+", "max_pages": 6,
                   "min_expected": 1}
    _keep_get = _w._get

    def _fake_get(u, timeout=20):
        if u == "https://ex.test/list/":
            return ('<a href="/m/1">Lためし機</a>'
                    '<a href="/list/page/2/">2</a>')
        raise RuntimeError("HTTP 404")

    _w._get = _fake_get
    try:
        _r = scan_directory("ex", _pages_conf)
    finally:
        _w._get = _keep_get
    t("★★2ページ目が読めなくても1ページ目は使う★★（実データで565→224に減った）",
      _r["surfaces_ok"] == 1 and len(_r["index"]) == 1 and _r["problems"])

    t("★★機種名＋宣伝文句の見出しを引き当てる★★（台帳#264）",
      _hit("マイジャグラーV") == ["マイジャグラーVスペック設定判別ぶどう"])
    t("★★世代違いを引き当てない★★（V で VI を拾わない）",
      _hit("マイジャグラーVI") == ["マイジャグラーVI"])
    t("★★続編・副題は飾りではない★★（V で『V挑戦者の道』を拾わない）",
      _hit("ストリートファイターV") == [] and _hit("北斗の拳") == [])

    t("★★一覧の記事タイトルから機種名だけを取り出す★★（実データの形）",
      anchor_core("2026年5月22日 Lすーぱぁびん娘 スロット 新台 天井 設定判別 やめどき 解析まとめ")
      == _ci.normalize_core("Lすーぱぁびん娘"))
    t("　日付が無くても取れる",
      anchor_core("Lすーぱぁびん娘(スマスロ) パチスロ新台")
      == _ci.normalize_core("Lすーぱぁびん娘"))
    t("　飾りしか無ければ空を返す", anchor_core("スロット 新台 天井") == "")
    t("★★機種名の中の「パチスロ」で名前ごと消えない★★（実在・Codex51回目）",
      anchor_core("Lパチスロうる星やつら") == _ci.normalize_core("Lパチスロうる星やつら")
      and anchor_core("2026年7月20日 Lパチスロ からくりサーカス2｜天井 スペック 設定判別 解析 評価")
      == _ci.normalize_core("Lパチスロ からくりサーカス2"))
    t("　機種名の中の「スロット」でも切らない（Lアニマルスロット ドッチ）",
      anchor_core("Lアニマルスロット ドッチ 新台 天井 設定判別")
      == _ci.normalize_core("Lアニマルスロット ドッチ"))
    t("　｜区切りの飾り列も落とす",
      anchor_core("Lタクトオーパス デスティニー｜天井・設定判別・終了画面・ヤメ時を徹底解説")
      == _ci.normalize_core("Lタクトオーパス デスティニー"))
    t("　ベタ付きの飾りは2語以上の時だけ剥がす",
      anchor_core("Lすーぱぁびん娘新台天井設定判別") == _ci.normalize_core("Lすーぱぁびん娘")
      and anchor_core("Lアニマルスロット") == _ci.normalize_core("Lアニマルスロット"))
    t("★★DMMのカードの形（メーカー名・機械割・導入開始日つき）を読める★★"
      "（実在の青ブタがDMMで照合不能＝名鑑1票のまま12回止まっていた・2026-08-03実データ）",
      anchor_core("L青春ブタ野郎はバニーガール先輩の夢を見ない オリンピア "
                  "機械割: 掲載準備中 導入開始日:2026年09月07日（月）予定")
      == _ci.normalize_core("L青春ブタ野郎はバニーガール先輩の夢を見ない"))
    t("　メーカー名だけ・日付だけの塊は飾り扱い（機種名は消えない）",
      anchor_core("オリンピア 機械割: 導入開始日:2026年09月07日") == ""
      and anchor_core("Lアニマルスロット ドッチ オリンピア")
      == _ci.normalize_core("Lアニマルスロット ドッチ"))

    HTML = ('<a href="/slot/belko-slot/260918/">2026年5月22日 Lすーぱぁびん娘 スロット 新台</a>'
            '<a href="/slot/belko-slot/111111/">Lスーパービンゴネオ スロット 解析</a>'
            '<a href="/about/">会社案内</a>'
            '<a href="/slot/belko-slot/260918/?utm=1">Lすーぱぁびん娘 まとめ</a>')
    # ★★2026-08-14・依頼188（題をタグで指す）★★
    #   一撃は機種名を <h4> に入れており、クラス名が無い。
    _TAG_HTML = (
        '<a href="/slot/l_x/"><div><h4>L試験機</h4>'
        '<p class="maker_item">サミー</p>'
        '<p class="last_updated">導入開始日：2026年04月06日</p></div></a>')
    _tag_idx = build_index(_TAG_HTML, "https://x.test/", r"/slot/[a-z0-9_]+/",
                           title_tag="h4")
    t("★★題をタグで指せる★★（クラス名が無い名鑑・依頼188）",
      _ci.normalize_core("L試験機") in _tag_idx)
    # ★実物と同じ形で確かめる★（メーカーの英字＋読み＋タイプ＋更新日）
    #   これが混ざると anchor_core では剥がしきれない（実際に一致しなかった）。
    _REAL_HTML = (
        '<a href="/slot/l_x/"><div><h4>L試験機</h4>'
        '<p class="maker_item">CROSSALPHA（クロスアルファ）</p>'
        '<p class="type_item"><span class="type">ATタイプ</span>'
        '<span class="type">天井</span></p>'
        '<p class="last_updated">導入開始日：2026年04月06日'
        '最終更新日：2026年04月25日</p></div></a>')
    t("★★タグで指さないと、メーカーや更新日まで芯に混ざる★★（実物の形）",
      _ci.normalize_core("L試験機") not in build_index(
          _REAL_HTML, "https://x.test/", r"/slot/[a-z0-9_]+/"))
    t("　タグで指せば、実物の形でも機種名だけを読める",
      _ci.normalize_core("L試験機") in build_index(
          _REAL_HTML, "https://x.test/", r"/slot/[a-z0-9_]+/", title_tag="h4"))
    t("★★クラスとタグの両方を指定したら、設定の誤りとして止める★★",
      "_PROBLEM_" in build_index(_TAG_HTML, "https://x.test/",
                                 r"/slot/[a-z0-9_]+/",
                                 title_class="x", title_tag="h4"))
    t("　題を読めないリンクがあれば、タグ指定でも知らせる",
      "_PROBLEM_" in build_index(
          _TAG_HTML + '<a href="/slot/l_y/"><span>題なし</span></a>',
          "https://x.test/", r"/slot/[a-z0-9_]+/", title_tag="h4"))
    idx = build_index(HTML, "https://d.example/slot/", r"/slot/[a-z\-]+/\d+")
    key = _ci.normalize_core("Lすーぱぁびん娘")
    t("★機種名から個別ページURLを引ける★",
      [u for u, _ in idx.get(key, [])] == ["https://d.example/slot/belko-slot/260918/"])
    t("　機種ページでないリンクは索引に入れない",
      all("about" not in u for v in idx.values() for u, _ in v))
    t("　同じURLは1件にまとめる（?や#が付いていても）",
      len(idx.get(key, [])) == 1)
    t("　別機種は別の項目になる",
      _ci.normalize_core("Lスーパービンゴネオ") in idx)
    idx_q = build_index(
        "<a href=\"/slot/belko-slot/1/\">L既存機 スロット 新台</a>"
        "<a href='/slot/belko-slot/2/'>L新台機 スロット 新台</a>",
        "https://d.example/slot/", r"/slot/[a-z\-]+/\d+")
    t("★★単一引用符のリンクも見落とさない★★"
      "（新台だけ欠落しても面が健全に見えた・Codex52回目）",
      _ci.normalize_core("L新台機") in idx_q
      and _ci.normalize_core("L既存機") in idx_q)
    t("　非表示のリンクは索引に入れない",
      _ci.normalize_core("L隠し機") not in build_index(
          '<div hidden><a href="/slot/belko-slot/3/">L隠し機 スロット</a></div>',
          "https://d.example/slot/", r"/slot/[a-z\-]+/\d+"))

    CONF = {"name": "t", "link_pattern": r"/slot/[a-z\-]+/\d+", "min_expected": 2,
            "status": "ACTIVE",
            "surfaces": [{"url": "https://d.example/slot/"},
                         {"url": "https://d.example/slot/belko-slot/"}]}
    real = _w._get
    try:
        _w._get = lambda u, timeout=20: HTML                       # noqa: E731
        r = find("Lすーぱぁびん娘", {"d": CONF})
        t("★★2つの入口を和集合で見る★★", r["results"]["d"]["surfaces"] == "2/2")
        t("　見つかれば FOUND と URL を返す",
          r["results"]["d"]["state"] == "FOUND"
          and r["results"]["d"]["url"].endswith("/260918/"))
        r2 = find("Lまったく別の機種", {"d": CONF})
        t("★★載っていないだけなら HEALTHY_NO_MATCH★★（異常と混ぜない）",
          r2["results"]["d"]["state"] == "HEALTHY_NO_MATCH")

        _w._get = lambda u, timeout=20: '<a href="/x">y</a>'       # noqa: E731
        r3 = find("Lすーぱぁびん娘", {"d": CONF})
        t("★★一覧が読めないときは『載っていない』と言わない★★",
          r3["results"]["d"]["state"] == "CATALOG_UNHEALTHY")
        t("　その理由が残る", bool(r3["results"]["d"]["problems"]))

        DUP = ('<a href="/slot/a/1/">Lすーぱぁびん娘 スロット</a>'
               '<a href="/slot/b/2/">Lすーぱぁびん娘 スロット</a>'
               '<a href="/slot/c/3/">Lべつの機種 スロット</a>')
        _w._get = lambda u, timeout=20: DUP                        # noqa: E731
        r4 = find("Lすーぱぁびん娘", {"d": CONF})
        t("★★候補が複数なら決めずに止める★★（順位や新しい方で選ばない）",
          r4["results"]["d"]["state"] == "AMBIGUOUS_CANDIDATES"
          and len(r4["results"]["d"]["candidates"]) == 2)
        t("　止めたときは URL を返さない", r4["results"]["d"]["url"] is None)
    finally:
        _w._get = real

    t("　状態の名前が想定どおり",
      set(STATES) == {"FOUND", "HEALTHY_NO_MATCH", "AMBIGUOUS_CANDIDATES",
                      "CATALOG_UNHEALTHY"})

    ng = [n for n, ok in results if not ok]
    print(f"{nl}{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--name", help="メーカー公式の正式名称")
    ap.add_argument("--check", help="1つの名鑑の索引だけ作って健全性を見る")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    cats = _sj.read_json(CATALOGS, expect=dict)["directories"]
    if args.check:
        conf = cats.get(args.check)
        if not conf:
            print(f"★{args.check} は directory-catalogs.json にありません★")
            return 1
        r = scan_directory(args.check, conf)
        print(f"{r['name']}: 入口 {r['surfaces_ok']}/{r['surfaces_total']} / "
              f"索引 {len(r['index'])} 件")
        for p in r["problems"]:
            print("  ✗ " + p)
        return 1 if r["surfaces_ok"] == 0 else 0
    if args.name:
        r = find(args.name)
        for did, v in r["results"].items():
            print(f"{did:14} {v['state']:22} 入口{v['surfaces']} 索引{v['index_size']:>4}件"
                  f"  {v['url'] or v['why']}")
        urls = found_urls(r)
        print(f"{chr(10)}見つかった個別ページ: {len(urls)} 件")
        for u in urls:
            print("  " + u)
        return 0 if urls else 1
    ap.print_help()
    return 0


# ★中身を見に来たら元の関数を返す★（2026-08-16・依頼219）
#   囲みにしたので find は薄い包みになった。試験や監査が中身を読むとき、
#   包みだけ見えると**守りが消えたように見える**（実際に試験が落ちた）。
find.__wrapped__ = _find


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except _sj.SafeJsonError as e:
        print(f"★入力データが読めません: {e}★")
        raise SystemExit(1)
    except Exception as e:
        print(f"★想定外の失敗 {type(e).__name__}: {e}★")
        raise SystemExit(1)

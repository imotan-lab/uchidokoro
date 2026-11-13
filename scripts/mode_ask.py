# -*- coding: utf-8 -*-
"""★2AIに「モード・ゾーンがあるか」を聞くための材料を用意する★

（2026-09-02・台帳#523の②・5段目）

★この道具がやること★
  1. その機種のページを**全部**集める（なな徹なら本体＋下位、最大50本）
  2. 見える文字を1つのファイルに書き出す（2AIが読む用）
  3. 証拠の集合の指紋を残す
  4. 質問文を出す

★やらないこと★＝**読み取らない・判定しない**。
  「モードがあるか」を決めるのは2AI（運営者の決まり）。

★★本文のファイルは一時的なもの★★（CLAUDE.mdの決まり・台帳#378）
  このリポジトリは公開で、なな徹の規約にも複製の条項がある。
  ★リポジトリの中に書かない★／★使い終わったら消す★（`--clean`）。

★★「無い」と書くには2サイト★★（2026-09-02・運営者の判断）
  「ある」は引用できるので1サイトでよいが、
  ★「無い」は引用できない★ので、1サイトの書き落としを見抜けない。
  ＝集める先も2サイトにする。

使い方:
    python scripts/mode_ask.py --slug hokuto            # 集めて質問を出す
    python scripts/mode_ask.py --slug hokuto --clean    # ★本文の写しだけ★消す
    python scripts/mode_ask.py --slug hokuto --purge    # 記録ごと消す（普段は使わない）
    python scripts/mode_ask.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                            # noqa: BLE001
    pass

import local_paths as _lp                                    # noqa: E402
import page_corpus as _pc                                    # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
CATALOGS = os.path.join(BASE, "assets", "data", "directory-catalogs.json")
# ★本文の写しはリポジトリの外★（公開されるところに他サイトの本文を置かない）
WORK = _lp.doc("mode_ask")


# ★区切りの印★（★本文の行と紛れない形にする★）
MARK = "===== うちどころ・出典 ====="
# ★★本文の行には必ず頭に印を付ける★★（2026-09-02・Codexのレビュー36）
#   ★直す前は、本文に区切りそのものが出てくると、その先が
#     別のページの本文として読まれた★（実際に試験で再現した）。
#   ＝引用の照合が別ページを見ることになる。
#   ★逃がすのではなく、全部の行に印を付ける★＝
#   「どの行が本文か」が形だけで決まり、本文の中身に左右されない。
BODY_MARK = "| "


def dump_pages(pages: dict) -> str:
    """★2AIが読む形に書き出す★（★往復して元に戻ること★）

    ★通し確認で踏んだ★＝以前は「改行2つで連結」していたので、
    読み直すと本文の末尾に改行が余り、★指紋が合わなくなった★。
    ★本文の行には頭に印を付ける★＝本文に区切りが出てきても化けない。
    """
    out = []
    for u, t in sorted((pages or {}).items()):
        out.append(f"{MARK}\n{BODY_MARK}{u}\n{MARK}")
        for line in str(t).split("\n"):
            out.append(BODY_MARK + line)
    return "\n".join(out)


def load_pages(raw: str) -> dict:
    """★書き出したものを元に戻す★（`dump_pages` の逆）。"""
    pages = {}
    lines = str(raw or "").split("\n")
    i, url, body = 0, None, []
    while i < len(lines):
        if (lines[i] == MARK and i + 2 < len(lines)
                and lines[i + 2] == MARK
                and lines[i + 1].startswith(BODY_MARK)):
            if url is not None:
                pages[url] = "\n".join(body)
            url, body = lines[i + 1][len(BODY_MARK):], []
            i += 3
            continue
        if url is not None and lines[i].startswith(BODY_MARK):
            body.append(lines[i][len(BODY_MARK):])
        i += 1
    if url is not None:
        pages[url] = "\n".join(body)
    return pages


def machine_name(slug: str) -> str:
    """slug から機種名を引く（★自分で決めない★＝一覧に無ければ空）。"""
    import safe_json as _sj
    for m in _sj.read_rows(MACHINES):
        if str(m.get("slug")) == slug:
            return str(m.get("name") or "")
    return ""


def catalog_of(url: str) -> dict:
    """そのURLがどの名鑑のものか（★名鑑の決まりを持ってくる★）。"""
    with open(CATALOGS, encoding="utf-8") as f:
        dirs = json.load(f)["directories"]
    import re
    for _id, cat in dirs.items():
        pat = str(cat.get("machine_page_pattern") or "")
        if pat and re.match(pat, str(url or "")):
            return cat
    return {}


def find_roots(directory_urls, control_recs) -> list:
    """★証拠の集合の起点（名鑑の機種ページ）を決める★（2026-11-14）

    ①名鑑の一覧で決まれば、その結果だけを使う（今までどおり）。
    ②決まらないとき（候補が前作と続編で割れる等）だけ、2AIで控えた出典
      （machine_sources の控え）のうち、稼働中（ACTIVE）の名鑑の機種ページの形をしたものを使う。
    ★control_recs は呼べるもの（関数）でもよい★＝①で決まったときは呼ばない
      （一覧で決まったのに、控えが読めないだけで止まらないように・Codexの指摘3）。
    ★隔離された控え（別のページに変わった疑い）と、取得を止めた名鑑は使わない★（Codexの指摘2）。
    ★使う直前に同じページのままかを確かめるのは呼ぶ側★（control_pick・Codexの指摘1）。
    ★なぜ★＝①だけだと、2AIで出典を控えても「名鑑の機種ページが見つかりません」で
      何も集めず、モードの欄を作る道が無かった（炎炎ノ消防隊・2026-11-14）。
    """
    got = [u for u in (directory_urls or []) if catalog_of(u)]
    if got:
        return got
    import machine_sources as _ms
    recs = control_recs() if callable(control_recs) else control_recs
    out = []
    for r in recs or []:
        if not isinstance(r, dict):
            continue
        if _ms.quarantined(r):
            continue
        u = str(r.get("url") or "")
        if u and active_catalog(u) and u not in out:
            out.append(u)
    return out


def active_catalog(url: str) -> bool:
    """★取りに行ってよい名鑑の機種ページか★（status が ACTIVE のものだけ）。"""
    return str(catalog_of(url).get("status") or "") == "ACTIVE"


def control_pick(slug: str, recs, recheck, ok_state: str):
    """★控えの出典を、使う直前に確かめる★（2026-11-14・Codexの指摘1）

    返り: (使ってよい控えの行, {URL: (確かめたときの本文, 到達先)})
    ★確かめたときに取った本文を、そのまま材料に使う★（取り直して別の本文を使わない）。
    ★同じページのままと確かめられなかった控えは使わない★（読めない・変わった疑い）。
    """
    keep, bodies = [], {}
    for r in recs or []:
        if not isinstance(r, dict):
            continue
        u = str(r.get("url") or "")
        if not u or not active_catalog(u):
            continue
        got = recheck(slug, r) or {}
        if got.get("state") == ok_state and got.get("html"):
            keep.append(r)
            bodies[u] = (got.get("html"), str(got.get("final_url") or u))
        else:
            print(f"  控えの出典を使いません（{u}）: {got.get('state')} {str(got.get('why') or '')[:80]}")
    return keep, bodies


def roots_for(slug: str, name: str, directory_urls, controls) -> list:
    """★run が起点を決める道（配線）★＝一覧の結果 → 決まらなければ控え。試験もこの関数を通す。"""
    return find_roots(directory_urls(name), controls)


def gather(slug: str, fetch, text_of, find_urls) -> dict:
    """★その機種の証拠の集合を、複数サイトぶん組み立てる★

    返り: {"pages": {...}, "manifest": {...}, "roots": [...], "why": ...}

    ★1サイトでも「読めなかった」ら、その機種は読めていない★
      （欠けた材料で2AIに判断させない）。
    ★そもそも無いページ（404）は、そのサイトを外すだけ★
      （Codexの助言「404は否定票にしない」）。
    """
    name = machine_name(slug)
    if not name:
        return {"pages": {}, "manifest": _pc.manifest({}, False),
                "roots": [], "why": f"機種一覧に {slug} がありません"}
    urls = [u for u in find_urls(name) if catalog_of(u)]
    if not urls:
        return {"pages": {}, "manifest": _pc.manifest({}, False),
                "roots": [], "why": "名鑑の機種ページが見つかりません"}

    pages, gone, roots, per_root = {}, [], [], {}
    for u in urls:
        got = _pc.collect(catalog_of(u), u, fetch, text_of)
        if got["why"]:
            # ★★1サイトでも読めなければ、その機種は読めていない★★
            #   （2026-09-02・Codexのレビュー36の重大①）
            #   ★直す前は、読めないサイトを外して続行していた★＝
            #   A・Bが読めれば、★Cにモードがあっても「ありません」と
            #   書けた★（説明と実装が逆だった）。
            #   ★「無い」は引用できない★ので、読み落としを防ぐ唯一の手が
            #   「全部読んだ」であり、そこが崩れると根拠が無くなる。
            return {"pages": {}, "manifest": _pc.manifest({}, False),
                    "roots": [],
                    "why": f"読めないサイトがあります（{u} … {got['why']}）"}
        pages.update(got["pages"])
        gone += list(got["manifest"].get("gone") or [])
        roots.append(u)
        # ★★サイトごとの記録も残す★★（2026-09-02・Codexのレビュー37の重大①）
        #   ★合体した記録には全サイトのURLが入っている★ので、
        #   1サイトぶんの軽い確認に渡すと★必ず「変わった」になる★。
        per_root[u] = got["manifest"]
    if not roots:
        return {"pages": {}, "manifest": _pc.manifest({}, False),
                "roots": [], "why": "読めたサイトがありません"}
    return {"pages": pages, "manifest": _pc.manifest(pages, True, gone),
            "roots": roots, "per_root": per_root, "why": ""}


QUESTION = """★この機種に「モード」や「ゾーン」があるかを判断してください★

★読むもの★＝{path}
  （{n} ページ・{chars} 字。★全部読んでから答えてください★）

★決めること★（モードとゾーンを**別々に**）
  HAS            … 狙い方が変わるモード／ゾーンがある
  NONE_CONFIRMED … 全部読んだが、そういうものは無い
  （★決められないときは答えないでください★＝欄ごと出しません）

★★「モード」の定義★★（2026-10-29・定義の違いで2AIが割れたため。モンハンサンブレイクで実際に割れた）
  ★入れる★＝通常時に内部で滞在する状態で、天井の深さ・当選の期待・次の当たりまでの道のりが変わるもの。
    かつ出典が「見分け方（示唆・履歴・挙動）」か「狙い方・ヤメ時への影響」のどちらかを書いているもの。
    例＝通常A〜D・天国・天国準備・引き戻し・設定変更後の特殊モード。
    ★名前が「高確」「シナリオ」でも、この2つを満たせば入れる★。
  ★入れない★＝演出だけのもの／名前が挙がるだけで見分け方も影響も書かれていないもの／
    ボーナス・AT中だけの状態（通常時の狙い方と関係しない）。
★★「ゾーン」の定義★★
  ★入れる★＝特定のゲーム数（pt・周期数・スルー回数でもよい）で当選が期待できる区間で、
    出典がその位置を数字で書いているもの。
  ★入れない★＝天井そのもの（天井のG数）。
  ★1つの出典が「目立ったゾーンは無い」と書いていても、別の出典が数字つきで書いていれば HAS★。
★★割れたら1回だけ答え直す★★＝相手の理由と引用を見せ合い、上の定義に照らしてもう一度答える。
  それでも割れたら答えない（欄は出さない）。

★★演出だけのモードは HAS にしない★★
  実例＝スマスロ サンダーVの「2種類の演出モード」は、
  演出が変わるだけで**天井や狙い目には関係しません**。
  ★読者が知りたいのは「いま打つべきか」★なので、
  狙い方が変わらないものは「無い」側に入れてください。

★HAS のときは引用が要ります★（その逐語が本文に在ることを機械が確かめます）
★NONE_CONFIRMED は引用できません★（存在しないものは引用できないため）
  ＝代わりに★独立した2つの出典★を機械が求めます（いま {sources} 件）

★答えの書き方★＝決定ファイルを作ってください:
{decision}
"""

DECISION_SAMPLE = """{
  "slug": "<機種>",
  "kind": "mode",                       // mode か zone
  "machine_url": "<証拠の集合の本体URL>",
  "manifest": "<この実行が出した manifest.json のパス>",
  "decisions": [
    {"judge": "claude", "state": "HAS", "why": "<15字以上の理由>"},
    {"judge": "codex",  "state": "HAS", "why": "<15字以上の理由>"}
  ],
  "quotes": [{"url": "<ページURL>", "quote": "<本文にある逐語>"}],
  "table": {"headers": ["モード", "見分け方", "次に期待できるところ"],
            "rows": [["天国", "…", "…"]]}
}"""


def run(slug: str) -> int:
    import fetched_page as _fp
    import html_check as _hc
    import new_machine_watch as _nmw
    import directory_index as _di
    import source_lineage as _sl

    import machine_sources as _ms

    bodies = {}

    def _controls():
        # ★一覧で決まらなかったときだけ呼ばれる★＝控えを読み、使う直前に同じページか確かめる
        keep, got_bodies = control_pick(slug, _ms.urls_for(slug), _ms.recheck, _ms.CHECK_OK)
        bodies.update(got_bodies)
        return keep

    def _get(u):
        # ★確かめたときの本文をそのまま使う★（取り直して別の本文を使わない）
        if u in bodies:
            html, fin = bodies[u]
            _nmw.LAST_FINAL_URL["url"] = fin
            return html
        return _nmw._get(u)

    def _find(name):
        return roots_for(slug, name, lambda n: _di.found_urls(_di.find(n)), _controls)

    os.makedirs(WORK, exist_ok=True)
    with _nmw.fetching("claim_material"):
        got = gather(slug, lambda u: _fp.fetch(u, "claim_material", get=_get),
                     lambda pg: _hc.visible_text(pg.cleaned_html), _find)
    if got["why"]:
        print("★集められませんでした★ " + got["why"])
        return 1

    body = dump_pages(got["pages"])
    p = os.path.join(WORK, f"{slug}_corpus.txt")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    mp = os.path.join(WORK, f"{slug}_manifest.json")
    with open(mp, "w", encoding="utf-8", newline="\n") as f:
        # ★★どの機種の証拠束かを残す★★（Codexのレビュー36の重大②）
        #   ★直す前は slug を残していなかった★ので、
        #   機種Aの証拠束を、決定ファイルの slug だけ機種Bにして控えられた。
        json.dump({"manifest": got["manifest"], "roots": got["roots"],
                   "per_root": got.get("per_root") or {},
                   "slug": slug, "name": machine_name(slug)},
                  f, ensure_ascii=False, indent=2)

    print(QUESTION.format(
        path=p, n=len(got["pages"]), chars=len(body),
        sources=_sl.independent(_pc.publishers(got["manifest"]["urls"])),
        decision=DECISION_SAMPLE))
    print(f"★本文の写しは使い終わったら消してください★"
          f"（python scripts/mode_ask.py --slug {slug} --clean）")
    return 0


def clean(slug: str) -> int:
    """★本文の写しだけを消す★（2026-09-02・Codexのレビュー38）

    ★記録（URLと指紋）は残す★＝
      ★直す前は記録まで消していた★ので、そのあと記事へ反映しようとすると
      「確認できない」で止まり、★記事に永久に届かなくなった★
      （節を入れる・更新する・消す、が全部できない）。

    ★残しても心配ない★＝記録に本文は入っていない（URLと指紋だけ）。
      台帳#378（複製の条項）で問題になるのは★本文の写し★のほう。
    """
    p = os.path.join(WORK, f"{slug}_corpus.txt")
    if os.path.isfile(p):
        os.remove(p)
        print("本文の写しを消しました（★記録は残します★）")
    else:
        print("本文の写しはありません")
    return 0


def purge(slug: str) -> int:
    """★記録ごと全部消す★（★普段は使わない★）

    ★これを使うと、その機種は記事へ反映できなくなる★
      （次に `mode_ask` を流し直すまで）。
    """
    n = 0
    for suffix in ("_corpus.txt", "_manifest.json"):
        p = os.path.join(WORK, slug + suffix)
        if os.path.isfile(p):
            os.remove(p)
            n += 1
    print(f"★記録ごと消しました★: {n} 件"
          "（次に mode_ask を流すまで、記事へ反映できません）")
    return 0


def selftest() -> int:
    ok, cases = 0, []

    def t(name, cond):
        nonlocal ok
        cases.append(name)
        if cond:
            ok += 1
        print(("✅" if cond else "❌") + " " + name)

    NANA = "https://nana-press.com/kaiseki/machine/644/"
    CHON = "https://chonborista.com/slot/sammy-slot/12345/"

    class _P:
        def __init__(self, html):
            self.cleaned_html = html

    HTML = f'<a href="{NANA}18017/">A</a>'

    def _fetch(u):
        return _P(HTML if u == NANA else "")

    def _text(pg):
        return "本文" + pg.cleaned_html[:12]

    t("★機種一覧に無ければ集めない★",
      gather("zzz_no_such_machine", _fetch, _text, lambda n: [])["why"] != "")

    # ★★名鑑の一覧で機種ページが決まらないときは、2AIで控えた出典を起点にする★★（2026-11-14）
    #   ★直す前は★、名鑑の一覧の候補が前作と続編で割れる（AMBIGUOUS_CANDIDATES）と
    #   「名鑑の機種ページが見つかりません」で何も集めず、2AIで出典を控えてもモードの欄を作れなかった（炎炎ノ消防隊）。
    _C1 = "https://nana-press.com/kaiseki/machine/738/"
    _C2 = "https://pachiseven.jp/machines/6927"
    _recs = [{"url": _C1}, {"url": _C2},
             {"url": "https://nana-press.com/kaiseki/machine/526/",
              "last_check": {"state": "CHANGED"}},
             {"url": "https://example.invalid/not-a-catalog/1"}, "壊れた行"]
    t("★一覧で決まれば一覧の結果を使う（控えは使わない）★",
      find_roots(["https://nana-press.com/kaiseki/machine/644/"], _recs)
      == ["https://nana-press.com/kaiseki/machine/644/"])
    t("★一覧で決まらなければ2AIの控えを使う★",
      find_roots([], _recs) == [_C1, _C2])
    t("　隔離された控え・名鑑の機種ページでないURL・壊れた行は使わない",
      _C1 in find_roots([], _recs)
      and "https://nana-press.com/kaiseki/machine/526/" not in find_roots([], _recs)
      and "https://example.invalid/not-a-catalog/1" not in find_roots([], _recs))
    t("　控えも無ければ空（今までどおり集めない）", find_roots([], []) == [])

    # ★Codexの指摘3★＝一覧で決まったときは控えを読まない（読めないだけで止まらない）
    def _boom():
        raise RuntimeError("控えが壊れている")
    try:
        _r3 = find_roots(["https://nana-press.com/kaiseki/machine/644/"], _boom)
    except Exception:                                   # noqa: BLE001
        _r3 = None
    t("★一覧で決まったときは控えを読まない（控えが壊れていても止まらない）★",
      _r3 == ["https://nana-press.com/kaiseki/machine/644/"])
    t("　一覧で決まらないときだけ控え（関数）を呼ぶ",
      find_roots([], lambda: [{"url": _C1}]) == [_C1])
    # ★Codexの指摘2★＝取得を止めた名鑑（相手に拒否された等）の控えは起点にしない
    t("★取得を止めた名鑑の控えは起点にしない★",
      find_roots([], [{"url": "https://chonborista.com/slot/sankyo-slot/252081/"}, {"url": _C2}]) == [_C2])
    # ★Codexの指摘1★＝控えは使う直前に同じページか確かめ、確かめた本文をそのまま使う
    def _rc(slug, r):
        if r["url"] == _C1:
            return {"state": "OK", "html": "<html>確かめた本文</html>", "final_url": _C1}
        return {"state": "CHANGED", "why": "題が変わりました"}
    _keep, _bodies = control_pick("zzz_x", [{"url": _C1}, {"url": _C2},
                                            {"url": "https://chonborista.com/slot/sankyo-slot/252081/"}],
                                  _rc, "OK")
    t("★確かめて同じページのままの控えだけ使う★",
      [r["url"] for r in _keep] == [_C1])
    t("　確かめたときの本文を持ち帰る（取り直さない）",
      _bodies == {_C1: ("<html>確かめた本文</html>", _C1)})
    _called = []
    t("　取得を止めた名鑑の控えは確かめにも行かない",
      control_pick("zzz_x", [{"url": "https://chonborista.com/slot/sankyo-slot/252081/"}],
                   lambda s, r: _called.append(r) or {"state": "OK", "html": "x"}, "OK")[0] == []
      and _called == [])
    # ★runの配線★＝run は roots_for → control_pick（machine_sources の recheck）を通る
    import sys as _sys
    import types as _types
    _seen = {}
    _fake_di = _types.SimpleNamespace(find=lambda n: {}, found_urls=lambda r: [])
    _fake_ms = _types.SimpleNamespace(
        urls_for=lambda s: [{"url": _C1}],
        recheck=lambda s, r: {"state": "OK", "html": "<p>x</p>", "final_url": _C1},
        CHECK_OK="OK", quarantined=lambda r: False)
    _saved = {k: _sys.modules.get(k) for k in ("directory_index", "machine_sources")}
    _real_gather = globals()["gather"]

    def _spy_gather(slug, fetch, text_of, find_urls):
        _seen["roots"] = find_urls("x")
        return {"pages": {}, "manifest": {}, "roots": [], "why": "試験で止める"}
    try:
        _sys.modules["directory_index"] = _fake_di
        _sys.modules["machine_sources"] = _fake_ms
        globals()["gather"] = _spy_gather
        import io as _io
        _out = _io.StringIO()
        import contextlib as _cl
        with _cl.redirect_stdout(_out):
            run("zzz_wiring")
    finally:
        globals()["gather"] = _real_gather
        for _k, _v in _saved.items():
            if _v is None:
                _sys.modules.pop(_k, None)
            else:
                _sys.modules[_k] = _v
    t("★run は一覧で決まらないとき、確かめた控えを起点にする（配線）★",
      _seen.get("roots") == [_C1])

    # ★★1サイトでも読めなければ、その機種は読めていない★★
    #   （2026-09-02・Codexのレビュー36の重大①）
    #   ★直す前は読めないサイトを外して続行していた★＝
    #   A・Bが読めれば、Cにモードがあっても「ありません」と書けた。
    _NANA = "https://nana-press.com/kaiseki/machine/644/"
    _CHON = "https://chonborista.com/slot/sammy-slot/12345/"

    def _both(name):
        return [_NANA, _CHON]

    def _ok(u):
        return _P("")

    def _one_dead(u):
        if u == _CHON:
            raise RuntimeError("HTTP 503")
        return _P("")

    _real = machine_name

    def _fake_name(slug):
        return "スマスロモンキーターンV" if slug == "zzz_two" else _real(slug)

    globals()["machine_name"] = _fake_name
    try:
        t("　2サイトとも読めれば集まる",
          gather("zzz_two", _ok, _text, _both)["manifest"]["complete"] is True)
        _bad = gather("zzz_two", _one_dead, _text, _both)
        t("★★読めないサイトが1つでもあれば、その機種は読めていない★★"
          "／★これが無いと、読めなかったサイトにモードがあっても"
          "「ありません」と書ける★",
          _bad["manifest"]["complete"] is False and "読めない" in _bad["why"])
    finally:
        globals()["machine_name"] = _real

    # ★名鑑の決まりが引けることを、本物の設定で確かめる★
    t("　なな徹の機種ページを名鑑と結び付けられる",
      bool(catalog_of(NANA).get("sub_page_pattern")))
    t("　ちょんぼりすたの機種ページも結び付けられる",
      bool(catalog_of(CHON).get("machine_page_pattern")))
    t("　関係ないURLはどの名鑑にも結び付かない",
      catalog_of("https://example.com/x") == {})

    t("　質問文に「演出だけのモードは HAS にしない」が入っている",
      "演出" in QUESTION)
    t("　質問文に「モード」「ゾーン」の定義と、割れたら1回だけ答え直す決まりが入っている（2026-10-29）",
      "「モード」の定義" in QUESTION and "「ゾーン」の定義" in QUESTION and "割れたら1回だけ" in QUESTION)
    t("★質問文が「決められないときは答えない」と言っている★",
      "決められないときは答えない" in QUESTION)
    t("　決定ファイルの見本に判断者が2人ある",
      DECISION_SAMPLE.count('"judge"') == 2)

    # ★★往復して元に戻ること★★（2026-09-02・通し確認が踏んだ）
    #   ★1文字でもずれると、指紋が合わず何も控えられない★
    for name, sample in (
            ("ふつうの本文", {"https://a/": "あいう\nえお", "https://b/": "かき"}),
            ("末尾が改行", {"https://a/": "あいう\n"}),
            ("空行を含む", {"https://a/": "あ\n\n\nい"}),
            ("本文が空", {"https://a/": ""}),
            ("区切りに似た行を含む",
             {"https://a/": "=====\n=== うちどころ ===\n本文"}),
            # ★★本文に区切りそのものが出てくる場合★★
            #   （2026-09-02・Codexのレビュー36の重箱2）
            #   ★起きにくいが、起きたら本文が別のページに化ける★
            ("本文に区切りそのものが出てくる",
             {"https://a/": MARK + "\nhttps://b/\n" + MARK + "\nだまし"}),
            ("1ページだけ", {"https://a/": "ひとつ"}),
    ):
        t(f"★書いて読み直すと元に戻る★（{name}）",
          load_pages(dump_pages(sample)) == sample)

    print(f"\n{ok}/{len(cases)} 合格")
    return 0 if ok == len(cases) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="モード・ゾーンを2AIに聞く材料を用意する")
    ap.add_argument("--slug")
    ap.add_argument("--clean", action="store_true",
                    help="★本文の写しだけ★を消す（使い終わったら必ず）"
                         "／記録は残すので、あとから記事へ反映できる")
    ap.add_argument("--purge", action="store_true",
                    help="★記録ごと全部消す★（普段は使わない）"
                         "／次に mode_ask を流すまで記事へ反映できません")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        raise SystemExit(selftest())
    if not a.slug:
        print("★--slug が要ります★")
        raise SystemExit(1)
    if a.clean and a.purge:
        # ★同時には書けない★（2026-09-02・Codexのレビュー39の重箱3）
        #   ★どちらのつもりか分からないまま記録を消さない★
        print("★--clean と --purge は同時に使えません★")
        raise SystemExit(1)
    if a.purge:
        raise SystemExit(purge(a.slug))
    raise SystemExit(clean(a.slug) if a.clean else run(a.slug))

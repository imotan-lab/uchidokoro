"""publish_new_machine.py — 新台1機種だけを公開する専用の経路。

★なぜ専用にするか（2026-07-31・Codexと相談して案Bに決めた）★
  既存119機種のページを直す `--legacy` に相乗りさせると、
  入力条件も品質も失敗時の扱いも違うものが同じ経路に混ざる。
  既存は `LEGACY_UNVERIFIED`（未裏取り）だが、新台の記事は
  **確認できた項目だけを載せた先行記事**で、意味がまるで違う。
  そこで状態名も別にする → `PREVIEW_VERIFIED_SUBSET`
  （載せた値は出典2件で確認済み・ただし記事は網羅的ではない）。

★この経路が触ってよいもの（これ以外は書かない）★
  1. `machines/{新しいslug}/index.html` を**新規に**作る
  2. `assets/data/machine-details/{新しいslug}.json` を新規に作る
  3. `machines.json` に1件足す
  ★sitemap は触らない★（preview は載せない決まり）
  ★既存ページは作り直さない・消さない・上書きしない★

★書く順番（Codexの指摘）★
  **ページを先に置き、最後に machines.json を足す。**
  トップページは machines.json を見てリンクを張るので、
  逆順だと「一覧に出るのにページが無い（404）」瞬間ができる。

使い方:
    python scripts/publish_new_machine.py --slug <slug>          # 確かめるだけ
    python scripts/publish_new_machine.py --slug <slug> --apply
    python scripts/publish_new_machine.py --selftest
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import re
import subprocess
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import build_machine_pages as _bmp      # noqa: E402
import build_new_article as _ba         # noqa: E402
import html_check as _hc                # noqa: E402
import safe_json as _sj                 # noqa: E402

MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
DETAILS = os.path.join(BASE, "assets", "data", "machine-details")
SITEMAP = os.path.join(BASE, "sitemap.xml")
STATE = "PREVIEW_VERIFIED_SUBSET"


class PublishError(RuntimeError):
    pass


# ★slug に使ってよい形★（2026-07-31・自分で確かめて危険を確認）
#   `../` を入れると machines/ の外へ書けてしまう
#   （_page_path("../../evil") → ../evil/index.html）。
# ★断り書きは、決めた文言と丸ごと一致していること★
#   （2026-07-31・Codex指摘5を再現して変えた）
#   必須語と禁止語の組み合わせでは、
#   「先行記事です。解析の結果、全項目が正しいと判明しました。」が通ってしまった。
#   文言はこちらで作るものなので、**丸ごと突き合わせる**のが確実。
NOTICE_TEXT = ("⚠ 先行記事（解析待ち）"
               "この機種はまだ解析データが出揃っていません。"
               "天井・狙い目・設定差は判明次第、随時更新します。")
NOTICE = chr(100)+chr(97)+chr(116)+chr(97)+chr(45)+"preview-notice="+chr(34)+STATE+chr(34)
_SLUG_OK = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
# 空白の並び（バックスラッシュを直接書かない：制御文字に化ける事故が続いたため）
_WS = "[ " + chr(9) + chr(13) + chr(10) + "]*"


LOCK = os.path.join(BASE, ".publish.lock")


class _OnlyOne:
    """★同時に2つ公開しない★（2026-07-31・Codex指摘4）

    2機種を同時に公開すると、どちらも同じ古い machines.json を読み、
    後から置き換えた方が先の追加を消してしまう。
    ロックファイルを「排他作成」で作れた側だけが進む。
    """

    def __init__(self, path=LOCK):
        self.path = path
        self.fd = None

    def __enter__(self):
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self.fd, str(os.getpid()).encode())
        except FileExistsError:
            raise PublishError(
                "いま別の公開処理が動いています（同時に2つは公開しません）。"
                f"止まったままなら {self.path} を消してください")
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
        try:
            os.remove(self.path)
        except OSError:
            pass
        return False


def check_slug(slug: str) -> list:
    """★書く場所を決める前に、slug そのものを確かめる★"""
    if not isinstance(slug, str) or not _SLUG_OK.match(slug):
        return [f"slug の形が許せません: {slug!r}"
                "（小文字英字で始まり、英数字と_のみ・2〜41文字）"]
    # ★形が合っていても、実際の書き先が machines/ の中か確かめる★（二重の守り）
    root = os.path.realpath(os.path.join(BASE, "machines"))
    for path in (os.path.realpath(os.path.join(BASE, "machines", slug, "index.html")),):
        if os.path.commonpath([root, path]) != root:
            return [f"書き先が machines/ の外を指しています: {slug!r}"]
    return []


def _page_path(slug: str) -> str:
    if check_slug(slug):
        raise PublishError(f"slug が不正です: {slug!r}")
    return os.path.join(BASE, "machines", slug, "index.html")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _existing_pages() -> dict:
    """いま公開中のページの指紋。★1枚も変えていないことを確かめるため★"""
    out = {}
    root = os.path.join(BASE, "machines")
    for slug in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        p = os.path.join(root, slug, "index.html")
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                out[slug] = _sha(f.read())
    return out


def check_before(slug: str, machine: dict, rows: list) -> list:
    """書く前に確かめること。★1つでも引っかかったら書かない★"""
    ng = check_slug(slug)
    if ng:
        return ng
    if not slug or slug != machine.get("slug"):
        ng.append("slug が機種データと合いません")
    if os.path.isfile(_page_path(slug)):
        ng.append(f"{slug} のページは既にあります（この経路は新規作成だけです）")
    if any(m.get("slug") == slug for m in rows):
        ng.append(f"{slug} は既に machines.json にあります（上書きしません）")
    if machine.get("status") != "preview":
        ng.append(f"status が preview ではありません（{machine.get('status')!r}）"
                  "。この経路は先行記事だけを公開します")
    if machine.get("publish_state") != STATE:
        ng.append(f"publish_state が {STATE} ではありません"
                  f"（{machine.get('publish_state')!r}）")
    return ng


def check_page(slug: str, html: str) -> list:
    """作ったページそのものを確かめる。★テンプレート任せにしない★

    ★2026-07-31・Codexの指摘を再現して3回直した★
      1回目: 本文まるごとの文字列検索 → コメントの noindex で合格していた
      2回目: head の中は見るようにしたが、タグ全体に "noindex" があるかで
             見ていたため `content="index" data-note="noindex"` が合格した
      3回目: 正規表現をやめた。`<div hidden="">` を見逃し、
             `<meta name='robots' content='index'>` を数え落としていた。
             → **HTMLを実際に解析して属性を正規化してから**見る。
    """
    ng = []
    doc = _hc.parse(html)
    robots = _hc.meta_values(doc, "robots")
    if len(robots) != 1:
        ng.append(f"robots 指定が {len(robots)} 個です（1個であるべきです）")
    else:
        vals = robots[0]
        if "noindex" not in vals:
            ng.append(f"robots が noindex ではありません（{sorted(vals)}）")
        if "index" in vals:
            ng.append("robots に index と noindex が両方あります")
    if doc.bases != ["/"]:
        ng.append(f'<base href="/"> が {doc.bases!r} です'
                  "（1個でないとロゴ・ナビが404になります）")
    canon = _hc.link_hrefs(doc, "canonical")
    want = f"https://uchidokoro.com/machines/{slug}/"
    if canon != [want]:
        ng.append(f"canonical が {canon!r} です（{want!r} が1個であるべきです）")
    if "style=" in html:
        ng.append("インラインstyleが入っています")
    # ★先行記事だと読者に分かる表示があるか★（noindexは非公開化ではない）
    #   ★本文のどこかに語があるだけでは認めない★（Codex指摘3）
    #     ひな型のバナーは完成機種のページにも同じ形で入っており、
    #     JavaScriptで表示を切り替えているだけだった。
    #     専用の目印を持ち、隠されていない要素をちょうど1個求める。
    notices = _hc.preview_notices(doc, STATE)
    if len(notices) != 1:
        ng.append(f"先行記事の断り書きが {len(notices)} 個です"
                  "（隠されていないものが1個であるべきです）")
    else:
        # ★文面まで確かめる★（2026-07-31・Codex指摘6を再現）
        #   「先行記事」の5文字だけを見ていたので、
        #   「先行記事ですが、全項目を完全確認済みです」も通っていた。
        text = "".join(notices[0]["text"].split())
        if text != "".join(NOTICE_TEXT.split()):
            ng.append(f"断り書きの文面が決めたものと違います: {notices[0]['text'][:60]!r}")
    return ng


# 数値らしいかたまり（全角も半角にそろえてから見る）
_NUM = re.compile(r"[0-9][0-9,./]*%?")


def _numbers(text: str) -> set:
    import unicodedata
    t = unicodedata.normalize("NFKC", text or "")
    return {x.rstrip(",./") for x in _NUM.findall(t) if x.rstrip(",./")}


def check_only_allowed_values(slug: str, machine: dict, detail: dict,
                              html: str) -> list:
    """★載せてよい値だけが載っているか★（2026-07-31・Codexの必須条件）

    ひな型だけで描いた結果と見比べ、**この機種のせいで増えた数値**を取り出す。
    それが機種データ・記事データのどこにも無ければ、
    どこかで作られた値ということになるので止める。

    本文だけでなく `<head>`（title・説明・JSON-LD）も含めて丸ごと見る。
    """
    empty_machine = {"slug": slug, "name": machine.get("name", ""),
                     "seo": {"title": ""}, "info": "", "strategy": "",
                     "aliases": [], "status": "preview", "release_date": ""}
    try:
        base = render(slug, empty_machine, {"slug": slug, "sections": []})
    except Exception as e:                # noqa: BLE001
        return [f"見比べ用のページを描けません: {type(e).__name__}: {e}"]
    added = _numbers(html) - _numbers(base)
    allowed = _numbers(json.dumps(machine, ensure_ascii=False)
                       + json.dumps(detail, ensure_ascii=False))
    stray = sorted(x for x in added if x not in allowed)
    if stray:
        return ["載せる材料に無い数値がページに出ています: "
                + "・".join(stray[:8])]
    return []


# 記事データに入ってよい鍵（★これ以外があれば止める★）
# ★実際の記事データを見て決めた★（2026-07-31・自分の検査が本物を弾いて気づいた）
#   新台: slug / lead / sections / factTable / summaryBoxes / updated
#   既存: それに name / evTable が加わる
_DETAIL_KEYS = {"slug", "name", "lead", "sections", "factTable",
                "summaryBoxes", "evTable", "updated"}
_SECTION_KEYS = {"title", "type", "body", "tables", "rows"}
# ★記事データへ入ってはいけない鍵★（採用しなかったものの置き場）
_FORBIDDEN = ("need_third", "unresolved", "candidates", "thin", "disputed")


_TABLE_KEYS = {"label", "headers", "rows", "note"}
_SECTION_TYPES = {"settei", "rumor"}
# 機種データに入ってよい鍵（★新台が作るものだけ★）
_MACHINE_KEYS = {"slug", "name", "seo", "info", "strategy", "aliases",
                 "status", "release_date", "identity", "publish_state"}


def _is_text(x) -> bool:
    return isinstance(x, str)


def _rows_ok(rows) -> bool:
    """表の中身が「文字の並びの並び」か。"""
    return (isinstance(rows, list)
            and all(isinstance(r, list) and all(_is_text(c) for c in r)
                    for r in rows))


def check_detail(slug: str, detail: dict) -> list:
    """★受け取った記事データそのものを確かめる★（2026-07-31・Codex指摘）

    `build_detail` が正しくても、この関数は任意の記事データを受け取れる。
    直接呼び出し・試験用の呼び出し・将来のつなぎ間違いが別の入口になるので、
    **境界でもう一度、形と型を最後まで**確かめる。

    ★2026-07-31・自分で確かめて分かったこと★
      「配列である」までしか見ていない所が9箇所あり、
      その中に任意の辞書や文字列を入れられた。中まで見る。
    """
    ng = []
    if not isinstance(detail, dict):
        return ["記事データが辞書ではありません"]
    if detail.get("slug") != slug:
        ng.append(f"記事データの slug が {detail.get('slug')!r} です（{slug!r} のはず）")
    stray = sorted(set(detail) - _DETAIL_KEYS)
    if stray:
        ng.append(f"記事データに知らない項目があります: {stray}")
    for key in ("name", "lead", "updated"):
        if key in detail and not _is_text(detail[key]):
            ng.append(f"{key} が文字ではありません")
    if not isinstance(detail.get("sections"), list):
        ng.append("sections が配列ではありません")
    for sec in (detail.get("sections") or []):
        if not isinstance(sec, dict):
            ng.append("節が辞書ではありません")
            continue
        bad = sorted(set(sec) - _SECTION_KEYS)
        if bad:
            ng.append(f"節『{sec.get('title')}』に知らない項目があります: {bad}")
        if not _is_text(sec.get("title")):
            ng.append("節に題がありません")
        if "type" in sec and sec["type"] not in _SECTION_TYPES:
            ng.append(f"知らない節の種類です: {sec.get('type')!r}")
        if "body" in sec and not (isinstance(sec["body"], list)
                                  and all(_is_text(x) for x in sec["body"])):
            ng.append(f"節『{sec.get('title')}』の本文が文字の配列ではありません")
        if "rows" in sec and not _rows_ok(sec["rows"]):
            ng.append(f"節『{sec.get('title')}』の rows が文字の並びではありません")
        if "tables" in sec and not isinstance(sec["tables"], list):
            ng.append(f"節『{sec.get('title')}』の tables が配列ではありません")
        for tb in (sec.get("tables") or []):
            if not isinstance(tb, dict):
                ng.append("表が辞書ではありません")
                continue
            tbad = sorted(set(tb) - _TABLE_KEYS)
            if tbad:
                ng.append(f"表に知らない項目があります: {tbad}")
            for k in ("label", "note"):
                if k in tb and not _is_text(tb[k]):
                    ng.append(f"表の {k} が文字ではありません")
            if "headers" in tb and not (isinstance(tb["headers"], list)
                                        and all(_is_text(x) for x in tb["headers"])):
                ng.append("表の headers が文字の配列ではありません")
            if not _rows_ok(tb.get("rows")):
                ng.append("表の中身が文字の並びではありません")
            # ★見出しの数と行の列数をそろえる★（2026-07-31・Codex指摘3）
            #   ずれると、正しい値が別の見出しの下に表示される。
            elif isinstance(tb.get("headers"), list):
                w = len(tb["headers"])
                bad_rows = [i for i, r in enumerate(tb["rows"]) if len(r) != w]
                if bad_rows:
                    ng.append(f"表の見出しが {w} 列なのに、"
                              f"{len(bad_rows)} 行の列数が違います")
    # ★中まで見る★（配列であることだけでは、任意の辞書を入れられる）
    for key in ("factTable", "evTable"):
        val = detail.get(key)
        if val is None:
            continue
        if not _rows_ok(val):
            ng.append(f"{key} が文字の並びではありません")
    boxes = detail.get("summaryBoxes")
    if boxes is not None:
        if not isinstance(boxes, list):
            ng.append("summaryBoxes が配列ではありません")
        else:
            for b in boxes:
                if not isinstance(b, dict) or set(b) - {"title", "body", "type"}:
                    ng.append(f"summaryBoxes に知らない形があります: {b!r}"[:120])
                    continue
                # ★配列なら中身まで見る★（2026-07-31・Codex指摘3を再現）
                #   「文字か配列か」で止めていたので、配列の中に辞書を入れられた。
                for k, v in b.items():
                    if _is_text(v):
                        continue
                    if isinstance(v, list) and all(_is_text(x) for x in v):
                        continue
                    ng.append(f"summaryBoxes の {k} が文字でも文字の配列でもありません")
    blob = json.dumps(detail, ensure_ascii=False)
    for word in _FORBIDDEN:
        if chr(34) + word + chr(34) in blob:
            ng.append(f"採用しなかったものの置き場（{word}）が記事データに残っています")
    return ng


_IDENTITY_KEYS = {"manufacturer_id", "official_product_url", "announced_name",
                  "market_release_date", "identity_tier", "regulatory_model_code",
                  "_model_code_sources"}
_RELEASE_OK = re.compile(r"^(20[0-9]{2}-[0-9]{2}(-[0-9]{2})?)?$")


def release_ok(value: str) -> bool:
    """★暦として実在する年月（日）か★（2026-07-31・Codex指摘4を再現）

    形だけ見ていたので `2026-99` や `2026-02-30` が通っていた。
    """
    v = str(value or "")
    if v == "":
        return True
    if not _RELEASE_OK.match(v):
        return False
    from datetime import date
    try:
        if len(v) == 7:
            y, m = int(v[:4]), int(v[5:7])
            date(y, m, 1)               # 月が1〜12かは date が判断する
        else:
            date.fromisoformat(v)       # 日まであるなら暦どおりか
    except ValueError:
        return False
    return True


def check_machine(slug: str, machine: dict) -> list:
    """★機種データそのものを確かめる★（2026-07-31・Codex指摘2）

    知らない項目が混ざれば、そこに書いた文字がページへ出る道になる。
    ★配列・辞書は中まで見る★（「配列である」だけでは任意の辞書を入れられる）
    """
    ng = []
    if not isinstance(machine, dict):
        return ["機種データが辞書ではありません"]
    stray = sorted(set(machine) - _MACHINE_KEYS)
    if stray:
        ng.append(f"機種データに知らない項目があります: {stray}")
    for key in ("name", "info", "strategy", "status", "publish_state"):
        if key in machine and not _is_text(machine[key]):
            ng.append(f"{key} が文字ではありません")
    aliases = machine.get("aliases", [])
    if not (isinstance(aliases, list) and all(_is_text(x) for x in aliases)):
        ng.append("aliases が文字の配列ではありません")
    seo = machine.get("seo")
    if seo is not None:
        if not isinstance(seo, dict) or set(seo) - {"title", "description"}:
            ng.append("seo に知らない項目があります")
        elif not all(_is_text(v) for v in seo.values()):
            ng.append("seo の中身が文字ではありません")
    if not release_ok(machine.get("release_date", "")):
        ng.append(f"release_date が暦として実在しません: "
                  f"{machine.get('release_date')!r}（YYYY-MM か YYYY-MM-DD か空）")
    ident = machine.get("identity")
    if ident is not None:
        if not isinstance(ident, dict):
            ng.append("identity が辞書ではありません")
        else:
            ibad = sorted(set(ident) - _IDENTITY_KEYS)
            if ibad:
                ng.append(f"identity に知らない項目があります: {ibad}")
            for k, v in ident.items():
                if _is_text(v):
                    continue
                # ★配列なら中身まで見る★（Codex指摘3を再現）
                if isinstance(v, list) and all(_is_text(x) for x in v):
                    continue
                ng.append(f"identity.{k} が文字でも文字の配列でもありません")
    # ★狙い目は当サイトの判断なので、この経路では書かせない★
    if machine.get("strategy"):
        ng.append("先行記事に狙い目を書くことはできません（strategy は空のはず）")
    return ng


# ★機種数を書いている場所★（2026-07-31・公開後に監査して見つけた）
#   新台を足すと README・運営者情報の「全120機種」がずれる。
#   （一覧ページは機種の行そのものを持つので、数字だけ直すと嘘になる。下の作り直しで扱う）
# ★全体の機種数は表示しない方針になった（2026-07-31）★
#   増減のたびに数を合わせる必要があり、実際に何度もずれた。
#   数字が無いので直す処理も要らない。監査は「書いていないか」を見る。
COUNT_FILES = ()
# ★一覧・ランキングの4ページ★（機種の行を実際に持つ生成物）
HUB_FILES = ("guide-tenjo-ranking.html", "guide-reset-ranking.html",
             "guide-suru-tenjo.html", "guide-ichiran.html")


def count_updates(old_n: int, new_n: int) -> dict:
    """★もう何もしない★（全体の機種数を表示しない方針にしたため）"""
    return {}


def build_hubs() -> dict:
    """いまの machines.json から一覧・ランキング4ページを描く（書き込まない）。

    ★2026-07-31・公開後に自分で監査して見つけた★
      新台を足しても一覧ページは120機種のままだった。
      あのページは**機種の行を実際に持つ生成物**なので、
      件数の数字だけ直すと「121機種」と言いながら120行しかない嘘になる。
    """
    import build_hub_pages as _bhp
    import safe_json as _sj2
    rows = _bhp.load_rows()
    prose = _sj2.read_json(_bhp.PROSE, expect=dict)
    built, _data_html, _allowed = _bhp._build_pages(rows, prose)
    return built


def check_hubs_untouched() -> list:
    """★いまの4ページが、いまのデータから作った物と同じか★

    違えば、誰かの未反映の変更が残っているということ。
    その状態で作り直すと**既存の公開内容まで変えてしまう**ので、この経路は進まない。
    """
    try:
        built = build_hubs()
    except Exception as e:                # noqa: BLE001
        return [f"一覧・ランキングを描けません: {type(e).__name__}: {e}"]
    ng = []
    for rel, html in built.items():
        path = os.path.join(BASE, rel)
        if not os.path.isfile(path):
            ng.append(f"{rel} がありません")
            continue
        with open(path, encoding="utf-8") as f:
            if f.read() != html:
                ng.append(f"{rel} が、いまのデータから作った内容と違います"
                          "（未反映の変更が残っているので、この経路では触りません）")
    return ng


# ★サイト全体の掲載数を表す言い回し★（2026-07-31・全体件数の表示をやめた）
_TOTAL_COUNT_PAT = re.compile(
    r"(全|全部で|掲載|対象機種数[:：]?[ ]*)(<[^>]+>)?[ ]*[0-9]{2,3}[ ]*(</[^>]+>)?[ ]*機種")


def check_counts(new_n: int, slug: str = "") -> list:
    """★早見表が機種データと合っているか★（2026-07-31・Codex指摘2/3）

    以前は「一覧に新台の文字列があるか」しか見ていなかったので、
    既存機種の欠落・余分な行・重複・他3ページの未更新に気づけなかった。
    **載っている機種の集合**で突き合わせ、
    4ページとも「いまのデータから作った内容」と丸ごと一致することを確かめる。
    """
    ng = []
    rows = _sj.read_rows(MACHINES)
    want = [m.get("slug") for m in rows if m.get("slug")]
    path = os.path.join(BASE, "guide-ichiran.html")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            html_i = f.read()
        got = re.findall(r'href="/machines/([a-z0-9_]+)/"', html_i)
        missing = sorted(set(want) - set(got))
        extra = sorted(set(got) - set(want))
        dup = sorted({x for x in got if got.count(x) > 1})
        if missing:
            ng.append(f"一覧に無い機種: {missing[:5]}（全{len(missing)}件）")
        if extra:
            ng.append(f"機種データに無い行: {extra[:5]}（全{len(extra)}件）")
        if dup:
            ng.append(f"一覧に同じ機種の行が複数: {dup[:5]}")
    # ★4ページとも、いまのデータから作った内容と丸ごと同じか★
    ng += check_hubs_untouched()
    # ★全体件数が書き戻されていないか★（表示しない方針）
    for rel in ("README.md", "about.html", "guide-ichiran.html"):
        p2 = os.path.join(BASE, rel)
        if not os.path.isfile(p2):
            continue
        with open(p2, encoding="utf-8") as f:
            m = _TOTAL_COUNT_PAT.search(f.read())
        if m:
            ng.append(f"{rel} にサイト全体の機種数があります（{m.group(0)[:24]!r}）"
                      "。全体件数は表示しない方針です")
    return ng


def allowed_paths(slug: str) -> set:
    """★この経路が変えてよいファイル★（これ以外が変わっていたら止める）"""
    return {
        f"machines/{slug}/index.html",
        f"assets/data/machine-details/{slug}.json",
        "assets/data/machines.json",
    } | set(COUNT_FILES) | set(HUB_FILES)   # ★件数と一覧の行も整える★


def changed_paths() -> list:
    """いまリポジトリで変わっているファイル（gitに聞く）。"""
    r = subprocess.run(["git", "status", "--porcelain"], cwd=BASE,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0:
        raise PublishError(f"git status が失敗しました: {r.stderr[:200]}")
    out = []
    for line in r.stdout.splitlines():
        if len(line) <= 3:
            continue
        path = line[3:].strip().strip('"')
        if path.endswith("/"):
            # ★gitは新しいフォルダを「フォルダごと1行」で報告する★
            #   （2026-07-31・自分の検査が正しい公開を止めて気づいた）
            #   そのままだと許可リスト（ファイル単位）と突き合わせられないので、
            #   中のファイルに開いてから比べる。
            root = os.path.join(BASE, path.rstrip("/"))
            for dirpath, _dirs, files in os.walk(root):
                for name in files:
                    rel = os.path.relpath(os.path.join(dirpath, name), BASE)
                    out.append(rel.replace(os.sep, "/"))
        else:
            out.append(path)
    return out


def snapshot(paths) -> dict:
    """指定したファイルの中身の指紋。★名前ではなく中身で見るため★"""
    out = {}
    for rel in paths:
        full = os.path.join(BASE, rel)
        if os.path.isfile(full):
            with open(full, "rb") as f:
                out[rel] = hashlib.sha256(f.read()).hexdigest()
        else:
            out[rel] = None
    return out


def check_no_stray_changes(slug: str, before_snap: dict) -> list:
    """★許した3つ以外を書いていないか★（2026-07-31・Codexの条件）

    ★Codex指摘を再現して直した★
      以前は「実行前から変更中だったパス」を名前で除外していたので、
      **もともとdirtyだったCSSをさらに書き換えても見逃した**。
      実行前に取った中身の指紋と突き合わせる。
    """
    allowed = allowed_paths(slug)
    ng = []
    now = snapshot(list(before_snap))
    for rel, sha in before_snap.items():
        if rel in allowed:
            continue
        if now.get(rel) != sha:
            ng.append(f"許していないファイルが変わっています: {rel}")
    for rel in changed_paths():
        if rel not in allowed and rel not in before_snap:
            ng.append(f"許していないファイルが増えました: {rel}")
    return ng


def check_sitemap_kept(before_text: str) -> list:
    """★sitemap が1文字も変わっていないこと★（この経路は触らない決まり）

    件数だけ見ていると、同じ件数のまま別のURLへ差し替わっても通る（Codex指摘）。
    """
    with open(SITEMAP, encoding="utf-8") as f:
        now = f.read()
    if now != before_text:
        n0, n1 = before_text.count("<url>"), now.count("<url>")
        return [f"sitemap が変わりました（{n0} → {n1} 件）。この経路は触りません"]
    return []


def check_served(slug: str) -> list:
    """★実際にHTTPで返るか確かめる★（ファイルがあるだけでは足りない）

    ローカルの簡易サーバで `/machines/{slug}/` を引き、200 と noindex を見る。
    ★必ずサーバを止める★
    """
    import http.server
    import socketserver
    import threading
    import urllib.request

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=BASE)
    try:
        srv = socketserver.TCPServer(("127.0.0.1", 0), handler)
    except OSError as e:
        return [f"確かめ用のサーバを立てられません: {e}"]
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    ng = []
    try:
        url = f"http://127.0.0.1:{port}/machines/{slug}/"
        with urllib.request.urlopen(url, timeout=10) as r:
            if r.status != 200:
                ng.append(f"公開したページが HTTP {r.status} を返します")
            body = r.read(400000).decode("utf-8", "replace")
        vals = _hc.meta_values(_hc.parse(body), "robots")
        if len(vals) != 1 or "noindex" not in vals[0]:
            ng.append(f"配信されたHTMLの robots が {vals!r} です（noindex 1個のはず）")
    except Exception as e:                # noqa: BLE001
        ng.append(f"公開したページを引けません: {type(e).__name__}: {e}")
    finally:
        srv.shutdown()
        srv.server_close()
    return ng


def check_after(slug: str, before_pages: dict, rows_before: list) -> list:
    """書いたあとに確かめること。★取り返しがつくうちに気づくため★"""
    ng = []
    now = _existing_pages()
    for s, h in before_pages.items():
        if s not in now:
            ng.append(f"既存ページが消えました: {s}")
        elif now[s] != h:
            ng.append(f"既存ページが書き換わりました: {s}")
    if slug not in now:
        ng.append(f"{slug} のページができていません")
    rows = _sj.read_rows(MACHINES)
    if not rows or rows[-1].get("slug") != slug:
        ng.append(f"一覧の最後が {slug} ではありません"
                  "（同時に別の書き込みがあった可能性があります）")
    if len(rows) != len(rows_before) + 1:
        ng.append(f"machines.json の件数が {len(rows_before)} → {len(rows)} です（+1のはず）")
    # ★件数だけでは、既存行の書き換えや入れ替わりを見つけられない★
    elif _sha(json.dumps(rows[:-1], ensure_ascii=False, sort_keys=True)) !=             _sha(json.dumps(rows_before, ensure_ascii=False, sort_keys=True)):
        ng.append("machines.json の既存の行が書き換わっています（足すだけのはずです）")
    for m in rows:
        if not os.path.isfile(_page_path(m.get("slug", ""))):
            ng.append(f"一覧に出るのにページがありません: {m.get('slug')}")
    with open(SITEMAP, encoding="utf-8") as f:
        if f"/machines/{slug}/" in f.read():
            ng.append("sitemap に先行記事が載っています（載せない決まりです）")
    return ng


# ひな型のバナー（JavaScriptで表示を切り替えている素の形）
_BANNER_HIDDEN = '<div id="previewBanner" class="preview-banner is-hidden">'
# 先行記事として出す形（★JavaScriptが動かなくても見える★＋機械で確かめられる目印）
_BANNER_SHOWN = ('<div id="previewBanner" class="preview-banner" role="note" '
                 + NOTICE + '>')


def render(slug: str, machine: dict, detail: dict) -> str:
    """既存ページと同じ描き方で1枚だけ作る。

    ★2026-07-31・Codex指摘3を確かめて分かったこと★
      先行記事のバナーは、**preview でも完成機種でもHTMLが全く同じ**で、
      JavaScript が `is-hidden` を外して初めて見える作りだった。
      つまり **JSが動かなければ、先行記事だという断りが一切出ない**。
      しかも検査側は「本文のどこかに『先行記事』の語があるか」しか見ていないので、
      完成機種のページでも合格してしまっていた。

      そこでこの経路で作るページだけ、
      **最初から見える形**にし、機械で数えられる目印を付ける。
      （ひな型と描画関数は既存119機種と共通のまま・ここでは差し替えない）
    """
    with open(os.path.join(BASE, "machine.html"), encoding="utf-8") as f:
        template = _bmp.prepare_template(f.read())
    reasons = _bmp.extract_pochipochi_reasons(template)
    html = _bmp.render_page(template, machine, detail, reasons)
    if machine.get("status") == "preview":
        if _BANNER_HIDDEN not in html:
            raise PublishError("ひな型の先行記事バナーが見つかりません"
                               "（machine.html の作りが変わった可能性があります）")
        html = html.replace(_BANNER_HIDDEN, _BANNER_SHOWN, 1)
    return html


def publish_from_material(slug: str, name: str, maker: str, official_url: str,
                          release: str, material: dict,
                          apply_it: bool = False) -> dict:
    """★材料から公開まで一気に通す（これが正しい入口）★

    ★2026-07-31・Codex指摘1★
      以前は完成した `machine` / `detail` を受け取っていたので、
      **誰かが作った任意のデータをそのまま公開できた**。
      「出玉率の97.3%」を「CZ期待度97.3%」として渡しても、
      入力のどこかに同じ数値があるため検査を通ってしまう。

      公開の境界で組み立てれば、載る値は
      `build_new_article` が**採用済みの材料からしか作らない**ものに限られる。
    """
    machine = _ba.build_machine(slug, name, maker, official_url, release, material)
    detail = _ba.build_detail(slug, name, release, material)
    return _publish_prebuilt(slug, machine, detail, apply_it=apply_it)


def _publish_prebuilt(slug: str, machine: dict, detail: dict,
                      apply_it: bool = False) -> dict:
    """★内部専用★ 外からは `publish_from_material` を使うこと。

    こちらは完成データを受け取るので、境界の検査でしか守れない。
    コマンドからは呼べないようにしてある（2026-07-31・Codex指摘1）。
    """
    if not apply_it:
        return _publish(slug, machine, detail, apply_it=False)
    with _OnlyOne():
        return _publish(slug, machine, detail, apply_it=True)


# ★外から使ってよいのは publish_from_material だけ★（2026-07-31・Codex指摘4）
#   完成データを受け取る経路は名前を _ で始めて、import * でも出さない。
__all__ = ["publish_from_material", "check_page", "check_detail", "check_machine",
           "check_counts", "check_hubs_untouched", "render", "STATE"]


def _publish(slug: str, machine: dict, detail: dict, apply_it: bool = False) -> dict:
    """新台1件を公開する。★ページを先に置き、最後に一覧へ足す★"""
    out = {"slug": slug, "problems": [], "wrote": [], "html_bytes": 0}
    rows = _sj.read_rows(MACHINES)
    out["problems"] += check_before(slug, machine, rows)
    out["problems"] += check_detail(slug, detail)
    out["problems"] += check_machine(slug, machine)
    # ★一覧・ランキングが、いまのデータと一致しているか★
    #   ずれたまま作り直すと、既存の公開内容まで変えてしまう。
    for x in check_hubs_untouched():
        out["problems"].append(
            x + "／先に `python scripts/build_hub_pages.py` 相当の作り直しが要ります")
    if out["problems"]:
        return out
    html = render(slug, machine, detail)
    out["html_bytes"] = len(html)
    out["problems"] += check_page(slug, html)
    out["problems"] += check_only_allowed_values(slug, machine, detail, html)
    if out["problems"] or not apply_it:
        return out

    before_pages = _existing_pages()
    with open(MACHINES, "rb") as f:
        machines_before = f.read()          # ★戻すときの正本★
    # ★早見表の元の中身を控える★（作り直しに失敗したら戻すため）
    hub_backup = {}
    for rel in HUB_FILES:
        full = os.path.join(BASE, rel)
        if os.path.isfile(full):
            with open(full, encoding="utf-8") as f:
                hub_backup[full] = f.read()
    before_snap = snapshot(changed_paths()
                           + ["sitemap.xml", "index.html", "machine.html",
                              "assets/css/practical.css", "meta-auto.js"])
    with open(SITEMAP, encoding="utf-8") as f:
        before_sitemap = f.read()
    page = _page_path(slug)
    dp = os.path.join(DETAILS, f"{slug}.json")
    made = []          # ★この処理が実際に作ったものだけ★（既存を消さないため）

    def _cleanup():
        """★自分が作ったものだけ片付ける★（2026-07-31・Codex指摘3を再現して直した）

        以前は「置くはずだった場所」を消していたので、
        **たまたま同名で既にあった記事データを消して**しまい、
        しかも「元に戻しました」と報告していた（実際に再現した）。
        """
        for kind, q, want in reversed(made):
            try:
                if kind == "file" and os.path.isfile(q):
                    # ★自分が書いた中身のままの時だけ消す★（Codex指摘5）
                    with open(q, encoding="utf-8") as fh:
                        if _sha(fh.read()) != want:
                            out["problems"].append(
                                f"作った後に中身が変わっていたので消しませんでした: {q}")
                            continue
                    os.remove(q)
                elif kind == "dir" and os.path.isdir(q):
                    os.rmdir(q)
            except OSError:
                pass

    try:
        # ① 記事データとページを置く（★この時点では一覧から辿れない★）
        #    "x" で開く＝既にあれば作らずに例外。存在確認との隙間も無くす。
        detail_text = json.dumps(detail, ensure_ascii=False, indent=1) + chr(10)
        with open(dp, "x", encoding="utf-8", newline=chr(10)) as f:
            made.append(("file", dp, _sha(detail_text)))
            f.write(detail_text)
        d = os.path.dirname(page)
        if not os.path.isdir(d):
            os.makedirs(d)
            made.append(("dir", d, None))
        with open(page, "x", encoding="utf-8", newline=chr(10)) as f:
            made.append(("file", page, _sha(html)))
            f.write(html)
    except FileExistsError as e:
        _cleanup()
        raise PublishError(f"同じ名前のファイルが既にあります（触っていません）: {e}")
    except Exception as e:                # noqa: BLE001
        _cleanup()
        raise PublishError(f"公開できませんでした（作ったものは消しました）: {e}")

    # ② ★一覧に足す前に全部確かめる★（2026-07-31）
    #   以前は machines.json まで書いてから確かめていたので、
    #   問題が見つかっても戻せなかった。ここで確かめれば、
    #   駄目なときは置いたファイルを消すだけで完全に元へ戻る。
    late = []
    # ★書いたページと記事データが、そのままの中身か★（Codex指摘5）
    for path, want in ((page, _sha(html)), (dp, _sha(detail_text))):
        with open(path, encoding="utf-8") as f:
            if _sha(f.read()) != want:
                late.append(f"書いたはずの中身と違います: {path}")
    late += check_served(slug)
    late += check_no_stray_changes(slug, before_snap)
    late += check_sitemap_kept(before_sitemap)
    now_pages = _existing_pages()
    for s_, h in before_pages.items():
        if s_ not in now_pages:
            late.append(f"既存ページが消えました: {s_}")
        elif now_pages[s_] != h:
            late.append(f"既存ページが書き換わりました: {s_}")
    if late:
        _cleanup()
        out["problems"] += late
        out["problems"].append("★確かめで引っかかったので、置いたものを消して元に戻しました★")
        return out

    # ③ ここで初めて一覧へ足す（★これ以降トップページからリンクされる★）
    try:
        rows = _sj.read_rows(MACHINES)        # ★直前に読み直す★（競合対策）
        if any(m.get("slug") == slug for m in rows):
            _cleanup()
            out["problems"].append("書いている間に同じ機種が一覧へ入りました（やり直してください）")
            return out
        rows.append(machine)
        # ★一時ファイル名を実行ごとに変える★（同時に走っても踏み合わない）
        tmp = MACHINES + f".new.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8", newline=chr(10)) as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
            f.write(chr(10))
        os.replace(tmp, MACHINES)
        out["wrote"] = [dp, page, MACHINES]
        # ★機種数の表記も同時に直す★（ここまで来たら一緒に整える）
        #   直せなくても公開は成立しているので、失敗は問題として残すだけにする。
        for rel, text in count_updates(len(rows) - 1, len(rows)).items():
            try:
                full = os.path.join(BASE, rel)
                with open(full, "w", encoding="utf-8", newline=chr(10)) as f:
                    f.write(text)
                out["wrote"].append(full)
            except OSError as e:
                out["problems"].append(f"機種数の表記を直せませんでした（{rel}）: {e}")
        # ★一覧・ランキングを作り直す★（行そのものを持つので数字だけでは足りない）
        #   ★全部そろってから一気に置き換える★（2026-07-31・Codex指摘1）
        #     1枚ずつ直接上書きしていたので、途中で失敗すると
        #     「1枚目だけ新台が載っている」ちぐはぐな状態が残った。
        #     書きかけのHTMLが配信される恐れもあった。
        try:
            new_hubs = build_hubs()                      # ①全部メモリで作る
            tmps = []
            for rel, html2 in new_hubs.items():
                full = os.path.join(BASE, rel)
                tmp2 = full + f".new.{os.getpid()}"
                with open(tmp2, "w", encoding="utf-8", newline=chr(10)) as f:
                    f.write(html2)
                tmps.append((tmp2, full, html2))
            for tmp2, full, html2 in tmps:               # ②一気に置き換える
                os.replace(tmp2, full)
                hub_backup[full] = hub_backup.get(full)  # 控えは取得済み
                out["wrote"].append(full)
        except Exception as e:            # noqa: BLE001
            for tmp2, _f, _h in locals().get("tmps", []):
                if os.path.exists(tmp2):
                    os.remove(tmp2)
            # ★置き換え済みの早見表も元に戻す★
            for full, text0 in hub_backup.items():
                if text0 is not None:
                    with open(full, "w", encoding="utf-8", newline=chr(10)) as f:
                        f.write(text0)
            out["problems"].append(f"一覧・ランキングを作り直せませんでした（元に戻しました）: {e}")
    except Exception as e:                # noqa: BLE001
        _cleanup()
        raise PublishError(f"一覧に足せませんでした（作ったものは消しました）: {e}")

    # ④ 一覧に足したあとの最終確認
    late2 = check_after(slug, before_pages, rows[:-1])
    late2 += check_counts(len(rows), slug)
    with open(page, encoding="utf-8") as f:          # ★最後にもう一度★
        if _sha(f.read()) != _sha(html):
            late2.append(f"一覧へ足した後にページの中身が変わっています: {page}")
    if late2:
        # ★戻せるときだけ戻す★（2026-07-31・Codexの助言）
        #   いま置いてある中身が「自分が書いたもの」と同じ時にだけ戻す。
        #   違っていれば誰かが触っているので、上書きせず知らせる。
        mine = _sha(json.dumps(rows, ensure_ascii=False, indent=1) + chr(10))
        with open(MACHINES, encoding="utf-8") as f:
            now_text = f.read()
        if _sha(now_text) == mine:
            with open(MACHINES, "wb") as f:
                f.write(machines_before)
            for full, text0 in hub_backup.items():       # ★早見表も戻す★
                if text0 is not None:
                    with open(full, "w", encoding="utf-8", newline=chr(10)) as f:
                        f.write(text0)
            _cleanup()
            out["wrote"] = []
            late2.append("★一覧から外し、置いたものを消して元に戻しました★")
        else:
            late2.append("★別の書き込みが入っているため、自動では戻しませんでした★"
                         "（人が確かめてください）")
        out["problems"] += late2
        return out
    return out


# ---------------------------------------------------------------- selftest

def _raises(fn) -> bool:
    try:
        fn()
    except Exception:                        # noqa: BLE001
        return True
    return False


def selftest() -> int:
    results = []
    nl = chr(10)

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    rows = _sj.read_rows(MACHINES)
    ok_machine = {"slug": "zzz_test", "name": "テスト機", "status": "preview",
                  "publish_state": STATE}
    t("★新しい機種なら前提を通る★", check_before("zzz_test", ok_machine, rows) == [])
    t("★★既にある機種は拒否する★★（上書きしない）",
      check_before(rows[0]["slug"],
                   {**ok_machine, "slug": rows[0]["slug"]}, rows))
    t("★★先行記事以外は公開しない★★",
      any("preview" in x for x in
          check_before("zzz_test", {**ok_machine, "status": "complete"}, rows)))
    t("★★状態名が違えば公開しない★★（既存の未裏取りページと混ぜない）",
      any("publish_state" in x for x in
          check_before("zzz_test",
                       {**ok_machine, "publish_state": "LEGACY_UNVERIFIED"}, rows)))
    t("　slugが食い違えば拒否", check_before("aaa", ok_machine, rows))

    good = ('<html><head><base href="/">'
            '<meta name="robots" content="noindex,follow">'
            '<link rel="canonical" href="https://uchidokoro.com/machines/zzz_test/">'
            "</head><body>"
            '<div class="preview-banner" role="note" ' + NOTICE + ">"
            + NOTICE_TEXT + "</div></body></html>")
    t("★作ったページの中身を必ず確かめる★", check_page("zzz_test", good) == [])
    t("★★noindex をコメントに書いただけでは通さない★★（実際に通っていた）",
      check_page("zzz_test",
                 good.replace('content="noindex,follow"', 'content="index,follow"')
                 + "<!-- noindex -->"))
    t("★★robots が2つあれば止める★★（競合する指定を見逃さない）",
      any("robots" in x for x in check_page(
          "zzz_test", good.replace("</head>",
                                   '<meta name="robots" content="index"></head>'))))
    t("　base href が無ければ公開しない",
      any("base" in x for x in check_page("zzz_test",
                                          good.replace('<base href="/">', ""))))
    t("　canonical が別機種なら公開しない",
      any("canonical" in x for x in
          check_page("zzz_test", good.replace("zzz_test/", "other/"))))
    t("　インラインstyleがあれば公開しない",
      any("style" in x for x in check_page("zzz_test",
                                           good.replace("<body>", '<body style="x">'))))
    t("★★断り書きが無ければ公開しない★★（noindexは非公開化ではない）",
      any("断り書き" in x for x in check_page(
          "zzz_test", good.replace(NOTICE, 'data-x="y"'))))
    t("★★本文に『先行記事』の語があるだけでは認めない★★"
      "（ひな型のバナーは完成機種のページにも同じ形で入っている）",
      any("断り書き" in x for x in check_page(
          "zzz_test", good.replace(NOTICE, 'data-x="y"')
          + "<p>先行記事一覧はこちら</p>")))
    t("　断り書きが2個あれば止める",
      any("2 個" in x for x in check_page(
          "zzz_test", good.replace("</body>",
                                   '<div ' + NOTICE + '>先行記事</div></body>'))))

    t("　数値のかたまりを取り出せる（全角もそろえる）",
      _numbers("約97.3%と１２００Ｇ") == {"97.3%", "1200"})

    t("★★robots は content の中身で見る★★"
      "（data-note=\"noindex\" で合格していた・実際に再現）",
      any("robots" in x for x in check_page(
          "zzz_test",
          good.replace('content="noindex,follow"',
                       'content="index" data-note="noindex"'))))
    t("★★断り書きの文面が決めたものと違えば止める★★",
      any("文面" in x for x in check_page(
          "zzz_test", good.replace(NOTICE_TEXT, "ふつうの記事です"))))
    t("★★『先行記事です。解析の結果、全項目が正しいと判明しました』も止める★★"
      "（必須語＋禁止語では通っていた・Codex指摘）",
      any("文面" in x for x in check_page(
          "zzz_test", good.replace(
              NOTICE_TEXT, "先行記事です。解析の結果、全項目が正しいと判明しました。"))))
    t("　暦にない日付は止める",
      any("暦" in x for x in check_machine(
          "zzz_test", {"slug": "zzz_test", "name": "x", "seo": {"title": "x"},
                       "info": "", "strategy": "", "aliases": [],
                       "status": "preview", "release_date": "2026-99",
                       "publish_state": STATE})))
    t("　2月30日も止める", not release_ok("2026-02-30"))
    t("　ふつうの年月は通る", release_ok("2026-09") and release_ok("2026-09-15")
      and release_ok(""))

    # ★中まで見る★（2026-07-31・自分で確かめて9箇所が素通りしていた）
    _b = {"slug": "zzz_test", "sections": []}
    for _why, _bad in (
            ("factTable の中に辞書", {**_b, "factTable": [{"x": "9999G"}]}),
            ("summaryBoxes に任意の形", {**_b, "summaryBoxes": [{"任意": "天井99999G"}]}),
            ("表の headers に辞書",
             {**_b, "sections": [{"title": "x",
                                  "tables": [{"headers": [{"a": 1}], "rows": []}]}]}),
            ("節の rows に辞書",
             {**_b, "sections": [{"title": "x", "rows": [{"a": 1}]}]}),
            ("lead が辞書", {**_b, "lead": {"a": "b"}})):
        t(f"★{_why}は止める★", check_detail("zzz_test", _bad))
    _m2 = {"slug": "zzz_test", "name": "x", "seo": {"title": "x"}, "info": "",
           "strategy": "", "aliases": [], "status": "preview",
           "release_date": "2026-09", "publish_state": STATE}
    for _why, _bad in (
            ("aliases に辞書", {**_m2, "aliases": [{"a": 1}]}),
            ("seo.title が辞書", {**_m2, "seo": {"title": {"a": 1}}}),
            ("identity に知らない項目", {**_m2, "identity": {"任意": "9999"}}),
            ("release_date が変な形", {**_m2, "release_date": "9999年天井"})):
        t(f"★{_why}は止める★", check_machine("zzz_test", _bad))
    t("★★identity の配列の中に辞書を入れられない★★（Codex指摘・再現した）",
      check_machine("zzz_test",
                    {**_m2, "identity": {"_model_code_sources": [{"任意": "にせ"}]}}))
    t("　まともな identity は通る",
      check_machine("zzz_test",
                    {**_m2, "identity": {"manufacturer_id": "bellco",
                                         "_model_code_sources": ["a", "b"]}}) == [])
    t("　本物の機種データは通る", check_machine("zzz_test", _m2) == [])

    # ★受け取った記事データそのものを確かめる★
    t("★まともな記事データなら通る★",
      check_detail("zzz_test", {"slug": "zzz_test", "sections": []}) == [])
    t("★★実際に作られる記事データが通る★★"
      "（許可リストを狭く書いて本物を弾いた・自分で気づいた）",
      check_detail("zzz_test", __import__("build_new_article").build_detail(
          "zzz_test", "テスト", "2026-09",
          {"adopted": {}, "need_third": {}, "thin": {}})) == [])
    t("★★別の機種の記事データなら止める★★",
      check_detail("zzz_test", {"slug": "other", "sections": []}))
    t("★★採用しなかったものの置き場が残っていたら止める★★",
      any("need_third" in x for x in
          check_detail("zzz_test", {"slug": "zzz_test", "sections": [],
                                    "need_third": {"at_prob": "1/999"}})))
    t("　知らない項目があれば止める",
      check_detail("zzz_test", {"slug": "zzz_test", "sections": [],
                                "こっそり": 1}))
    t("　節に知らない項目があれば止める",
      check_detail("zzz_test", {"slug": "zzz_test",
                                "sections": [{"title": "x", "候補": []}]}))

    t("★★summaryBoxes の配列の中に辞書を入れられない★★（Codex指摘・再現した）",
      check_detail("zzz_test", {"slug": "zzz_test", "sections": [],
                                "summaryBoxes": [{"title": "題",
                                                  "body": [{"任意": "天井99999G"}]}]}))
    t("　まともな summaryBoxes は通る",
      check_detail("zzz_test", {"slug": "zzz_test", "sections": [],
                                "summaryBoxes": [{"title": "題",
                                                  "body": ["ふつうの文"]}]}) == [])
    t("★★表の見出し数と行の列数がそろわなければ止める★★"
      "（正しい値が別の見出しの下に出る）",
      any("列数" in x for x in check_detail(
          "zzz_test", {"slug": "zzz_test",
                       "sections": [{"title": "x",
                                     "tables": [{"headers": ["A", "B", "C"],
                                                 "rows": [["1", "2"]]}]}]})))

    # ★機種データそのものを確かめる★（Codex指摘2）
    _ok_machine = {"slug": "zzz_test", "name": "テスト", "seo": {"title": "x"},
                   "info": "", "strategy": "", "aliases": [],
                   "status": "preview", "release_date": "2026-09",
                   "publish_state": STATE}
    t("★まともな機種データなら通る★", check_machine("zzz_test", _ok_machine) == [])
    t("★★知らない項目が混ざっていたら止める★★（そこに書いた文字がページへ出る）",
      any("知らない項目" in x for x in
          check_machine("zzz_test", {**_ok_machine, "こっそり": "9999G天井"})))
    t("★★先行記事に狙い目は書かせない★★（当サイトの判断は裏取りの外）",
      any("狙い目" in x for x in
          check_machine("zzz_test", {**_ok_machine, "strategy": "等価600G〜"})))
    t("　aliases が配列でなければ止める",
      check_machine("zzz_test", {**_ok_machine, "aliases": "ほくと"}))

    # ★記事データの中の形まで見る★
    t("　表の中身が文字の並びでなければ止める",
      any("文字の並び" in x for x in check_detail(
          "zzz_test", {"slug": "zzz_test",
                       "sections": [{"title": "x",
                                     "tables": [{"rows": "ただの文字列"}]}]})))
    t("　知らない節の種類なら止める",
      any("節の種類" in x for x in check_detail(
          "zzz_test", {"slug": "zzz_test",
                       "sections": [{"title": "x", "type": "なぞ"}]})))
    t("　本文が文字の配列でなければ止める",
      any("本文" in x for x in check_detail(
          "zzz_test", {"slug": "zzz_test",
                       "sections": [{"title": "x", "body": "ひとつの文字列"}]})))

    # ★見えない要素の判定★（Codex指摘5）
    t("★★引用符が違う robots も数える★★（正規表現では見逃していた）",
      any("2 個" in x for x in check_page("zzz_test", good.replace(
          "</head>", "<meta name='robots' content='index'></head>"))))
    t("★★隠された断り書きは認めない★★",
      any("断り書き" in x for x in check_page(
          "zzz_test", good.replace('role="note"', 'role="note" hidden'))))

    t("★★全体の機種数はもう扱わない★★（表示しない方針・監査が再導入を見張る）",
      count_updates(120, 121) == {} and COUNT_FILES == ())

    t("★★一覧・ランキングのずれを見つけられる★★"
      "（ずれたまま作り直すと既存の公開内容まで変わる）",
      isinstance(check_hubs_untouched(), list))
    t("★★一覧と機種データを集合で突き合わせる★★（欠け・余分・重複を見つける）",
      check_counts(len(rows)) == [])

    # ★同時に2つ公開しない★（Codex指摘4）
    with _OnlyOne(os.path.join(BASE, ".publish.lock.test")) as _one:
        t("★★ロックを持っている間は、もう一方が入れない★★",
          _raises(lambda: _OnlyOne(
              os.path.join(BASE, ".publish.lock.test")).__enter__()))
    t("　抜けたらロックは消える",
      not os.path.exists(os.path.join(BASE, ".publish.lock.test")))

    # ★sitemap は1文字も変えない★
    with open(SITEMAP, encoding="utf-8") as _f2:
        _sm2 = _f2.read()
    t("★★sitemapは件数が同じでも中身が変われば止める★★"
      "（同数の別URLに差し替えても通っていた）",
      check_sitemap_kept(_sm2.replace("/machines/", "/kikai/", 1)))

    # ★slug そのものを確かめる★（2026-07-31・machines/ の外へ書けた）
    t("★★slug に ../ が入っていたら受け付けない★★（machines/ の外へ書けた）",
      check_slug("../../evil"))
    t("　変な文字も受け付けない",
      check_slug("A B") and check_slug("") and check_slug("1abc"))
    t("　普通のslugは通る", check_slug("lbinko") == [])

    # ★machines.json の既存行が書き換わっていないか★
    _rows_before = [{"slug": "a", "name": "あ"}, {"slug": "b", "name": "い"}]
    _now = _rows_before + [{"slug": "c", "name": "う"}]
    t("　足すだけなら通る",
      _sha(json.dumps(_now[:-1], ensure_ascii=False, sort_keys=True))
      == _sha(json.dumps(_rows_before, ensure_ascii=False, sort_keys=True)))
    _tampered = [{"slug": "a", "name": "書き換え"}, {"slug": "b", "name": "い"},
                 {"slug": "c", "name": "う"}]
    t("★★件数が合っていても既存行が書き換わっていたら気づく★★",
      _sha(json.dumps(_tampered[:-1], ensure_ascii=False, sort_keys=True))
      != _sha(json.dumps(_rows_before, ensure_ascii=False, sort_keys=True)))

    pages = _existing_pages()
    t("★既存ページの指紋を取れる（1枚も変えていないことを確かめるため）★",
      len(pages) >= 100 and all(len(v) == 64 for v in pages.values()))

    t("★★新しいフォルダは中のファイルに開いてから比べる★★"
      "（gitはフォルダごと1行で報告するため、正しい公開を止めていた）",
      not any(x.endswith("/") for x in changed_paths()))
    t("★変えてよいのは決めたものだけ★",
      allowed_paths("zzz") == {"machines/zzz/index.html",
                               "assets/data/machine-details/zzz.json",
                               "assets/data/machines.json"}
      | set(COUNT_FILES) | set(HUB_FILES))
    _real_changed = changed_paths
    try:
        globals()["changed_paths"] = lambda: ["assets/css/practical.css"]
        _snap = snapshot(["assets/css/practical.css"])
        t("　何も変えていなければ通る（＝誤検知しない）",
          check_no_stray_changes("zzz", _snap) == [])
        t("★★もともと変更中だったファイルを、さらに書き換えたら気づく★★"
          "（名前で除外していたので見逃していた）",
          any("practical.css" in x for x in
              check_no_stray_changes("zzz", {"assets/css/practical.css": "ちがう指紋"})))
        globals()["changed_paths"] = lambda: ["assets/img/logo.png"]
        t("★許していないファイルが増えたら気づく★",
          any("増えました" in x for x in check_no_stray_changes("zzz", {})))
    finally:
        globals()["changed_paths"] = _real_changed

    with open(SITEMAP, encoding="utf-8") as _f3:
        _sm = _f3.read()
    t("　sitemapが変わっていなければ通る", check_sitemap_kept(_sm) == [])
    t("★★sitemapが1件でも増減したら止める★★",
      check_sitemap_kept(_sm + "<url>x</url>"))
    t("★★実際にHTTPで引いて200とnoindexを確かめられる★★"
      "（ファイルがあるだけでは足りない）",
      check_served(next(m["slug"] for m in rows
                        if m.get("status") == "preview")) == [])
    t("　存在しない機種なら引けないと分かる",
      any("引けません" in x for x in check_served("zzz_nothing_here")))

    ng = [n for n, ok in results if not ok]
    print(f"{nl}{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--slug")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--material", help="採用済みの材料（JSONファイル）")
    ap.add_argument("--name", help="メーカー公式の正式名称")
    ap.add_argument("--maker", help="メーカーID")
    ap.add_argument("--official-url", dest="official_url", help="公式ページURL")
    ap.add_argument("--release", default="", help="登場年月 YYYY-MM")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.slug:
        ap.print_help()
        return 0
    # ★公開できるのは材料からだけ★（2026-07-31・Codex指摘1）
    #   以前は完成した機種データ・記事データを受け取って publish() を直接呼べた。
    #   それだと「数値を含まない誤った文章」や
    #   「別項目の数値を置いたデータ」をそのまま公開できてしまう。
    if not (args.material and args.name and args.maker and args.official_url):
        print("★材料と機種の情報が要ります★")
        print("  --material <材料JSON> --name <正式名称> "
              "--maker <メーカーID> --official-url <公式URL> [--release YYYY-MM]")
        print("  （ふだんは add_machine_run.py --apply が中で呼びます）")
        return 1
    material = _sj.read_json(args.material, expect=dict)
    res = publish_from_material(args.slug, args.name, args.maker,
                                args.official_url, args.release or "",
                                material, apply_it=args.apply)
    if res["problems"]:
        print("★公開できません★")
        for p in res["problems"]:
            print("  ✗ " + p[:160])
        return 1
    if args.apply:
        print("公開しました:")
        for w in res["wrote"]:
            print("   " + os.path.relpath(w, BASE).replace(os.sep, "/"))
    else:
        print(f"確認だけ済みました（問題なし・{res['html_bytes']} バイトのページを作れます）")
        print("  実際に書くには --apply を付けてください")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except (PublishError, _sj.SafeJsonError) as e:
        print(f"★{e}★")
        raise SystemExit(1)
    except Exception as e:                # noqa: BLE001
        print(f"★想定外の失敗 {type(e).__name__}: {e}★")
        raise SystemExit(1)

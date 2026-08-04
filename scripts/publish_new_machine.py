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
import page_decision as _pdz            # noqa: E402  ★区分の唯一の判定箇所★
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
# ★時間で嘘になる語（先行・導入前）を使わない★（2026-08-04・Codex70〜72回目）
NOTICE_TEXT = ("⚠ このページは出典で確認が取れた項目のみ掲載しています。"
               "未掲載の項目は確認でき次第更新します。")
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


# ★作業中の目印★（2026-07-31・Codex9回目・実際に再現した）
#   全部書き終えた直後に電源が落ちると、
#   ページも一覧もそろっているため「中断された処理」と
#   「正常に完成した新台」を区別できなかった。
#   書き始める前にこの目印を作り、全部終わってから消す。
#   目印が残っていれば、次の実行も push も止める。
IN_PROGRESS = os.path.join(BASE, ".publish-in-progress.json")


def mark_start(slug: str, machine: dict, backup: dict) -> None:
    """★書き始める前に目印を残す★（電源が落ちても残る）

    ★戻すのに必要な情報も一緒に残す★（2026-07-31・Codex10回目）
      目印だけ消して再実行すると、中途半端な状態のまま
      「正常」と見なして公開できてしまう。
      どのファイルを何に戻せばよいかを、目印の中に書いておく。
    """
    from datetime import datetime
    data = {
        "slug": slug, "name": machine.get("name", ""),
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "pid": os.getpid(),
        # ★戻し方★ 変える前の中身の指紋
        "restore": {os.path.relpath(k, BASE).replace(os.sep, "/"): _sha(v)
                    for k, v in backup.items() if v is not None},
        # ★作るものの指紋★（消してよいか判断する。人が直していたら消さない）
        # ★これから作るもの★（作る前に残す）
        #   2026-07-31・Codex13回目: 作ってから指紋を書く形だと、
        #   その隙間で落ちたとき「作ったのに目印に無い」残骸ができる。
        "planned": ([f"machines/{slug}/index.html",
                     f"assets/data/machine-details/{slug}.json",
                     f"machines.json#{slug}"]
                    # ★index対象は sitemap にも1行足す★（2026-08-04・Codex72回目）
                    + ([f"sitemap.xml#{slug}"]
                       if _pdz.is_auto(machine)
                       and _pdz.machine_class(machine) == "AUTO_INDEXABLE"
                       else [])),
        "created": {},
        "_why": "この目印がある間は、公開が途中で終わっています。"
                "★目印だけ消してはいけません★ "
                "scripts/publish_new_machine.py --recover で元に戻してください。",
    }
    # ★排他作成★（同時に2つ始まらない）
    tmp = f"{IN_PROGRESS}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline=chr(10)) as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=1) + chr(10))
        f.flush()
        os.fsync(f.fileno())
    try:
        os.link(tmp, IN_PROGRESS)     # 既にあれば失敗する（＝排他）
    except FileExistsError:
        os.remove(tmp)
        raise PublishError("いま別の公開処理が動いているか、前回が途中で終わっています")
    except (OSError, AttributeError):
        # リンクが使えない環境では、存在を確かめてから置く
        if os.path.exists(IN_PROGRESS):
            os.remove(tmp)
            raise PublishError("いま別の公開処理が動いているか、前回が途中で終わっています")
        os.replace(tmp, IN_PROGRESS)
        return
    os.remove(tmp)


def mark_created(created: dict) -> None:
    """★作ったものの指紋を目印に足す★（復旧のとき、消してよいか判断する）"""
    try:
        got = _sj.read_json(IN_PROGRESS, expect=dict)
    except Exception:                     # noqa: BLE001
        return
    got["created"] = {**(got.get("created") or {}), **created}
    write_atomic(IN_PROGRESS, json.dumps(got, ensure_ascii=False, indent=1) + chr(10))


def mark_done() -> None:
    """★全部終わってから消す★（ここまで来て初めて「終わった」）"""
    if os.path.exists(IN_PROGRESS):
        os.remove(IN_PROGRESS)


def unfinished() -> dict:
    """途中で終わった公開が残っていないか。★残っていれば中身を返す★"""
    if not os.path.exists(IN_PROGRESS):
        return {}
    try:
        return _sj.read_json(IN_PROGRESS, expect=dict)
    except Exception:                     # noqa: BLE001
        return {"slug": "(読めません)", "_why": "目印が壊れています"}


def write_atomic(path: str, text: str, new_only: bool = False) -> None:
    """★一時ファイルに完成させてから置き換える★（2026-07-31・Codex指摘2/3）

    最終名へ直接書いていたため、次の2つが起きた。
      ・書き込みの途中で失敗すると、**書きかけのファイル**が最終名に残る
        （この処理が作ったのに指紋が違うので、片付けの対象からも外れていた）
      ・復元も直接書いていたので、失敗すると**元は正常だった早見表が空になる**

    new_only=True は「新しく作る時だけ」。既にあれば作らない。
    """
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8", newline=chr(10)) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())      # ★中身が確実に書けてから置き換える★
        if new_only and os.path.exists(path):
            raise FileExistsError(path)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


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
    try:
        cls = _pdz.machine_class(machine)
        if cls not in ("AUTO_INDEXABLE", "AUTO_PENDING"):
            ng.append(f"新台経路の区分ではありません（{cls}）"
                      "。この経路は判定書つきの新台だけを公開します")
    except _pdz.DecisionError as e:
        ng.append(f"判定書が壊れています: {e}")
    if machine.get("publish_state") != STATE:
        ng.append(f"publish_state が {STATE} ではありません"
                  f"（{machine.get('publish_state')!r}）")
    return ng


def check_page(slug: str, html: str, expect_noindex: bool = True) -> list:
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
    if expect_noindex:
        if len(robots) != 1:
            ng.append(f"robots 指定が {len(robots)} 個です（1個であるべきです）")
        else:
            vals = robots[0]
            if "noindex" not in vals:
                ng.append(f"robots が noindex ではありません（{sorted(vals)}）")
            if "index" in vals:
                ng.append("robots に index と noindex が両方あります")
    else:
        # ★index対象（AUTO_INDEXABLE）: robots meta が1個も無いこと★
        #   逆に付いていたら止める（逆方向もfail-closed・Codex72回目）
        if len(robots) != 0:
            ng.append(f"index対象なのに robots 指定が {len(robots)} 個あります"
                      f"（{[sorted(v) for v in robots]!r}）")
    if doc.bases != ["/"]:
        ng.append(f'<base href="/"> が {doc.bases!r} です'
                  "（1個でないとロゴ・ナビが404になります）")
    canon = _hc.link_hrefs(doc, "canonical")
    want = f"https://uchidokoro.com/machines/{slug}/"
    if canon != [want]:
        ng.append(f"canonical が {canon!r} です（{want!r} が1個であるべきです）")
    if "style=" in html:
        ng.append("インラインstyleが入っています")
    # ★時間で嘘になる語の禁止★（2026-08-04・Codex70〜72回目の鮮度ゲート。
    #   導入日を過ぎた瞬間に記事が古くなる語は、日付を問わず最初から書かない）
    for w in _ba.STALE_WORDS:
        if w in html:
            ng.append(f"時間で嘘になる語がページに入っています: {w}")
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
                     "aliases": [], "release_date": ""}
    if _pdz.is_auto(machine):
        # バナー有無を実物とそろえる（新契約の最小の判定書つき）
        empty_machine["publication_policy"] = _pdz.SCHEMA
        empty_machine["page_decision"] = {
            "schema_version": _pdz.SCHEMA, "indexable": False}
    else:
        empty_machine["status"] = "preview"
    try:
        base = render(slug, empty_machine, {"slug": slug, "sections": []})
    except Exception as e:                # noqa: BLE001
        return [f"見比べ用のページを描けません: {type(e).__name__}: {e}"]
    added = _numbers(html) - _numbers(base)
    # ★判定書（decided_at・digest）の数字を「載せてよい数値」に混ぜない★
    #   （2026-08-04・Codex72回目の分析。掲載値の由来ではないため）
    m_for_allowed = {k: v for k, v in machine.items()
                     if k not in ("page_decision", "publication_policy")}
    allowed = _numbers(json.dumps(m_for_allowed, ensure_ascii=False)
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
                 "status", "release_date", "identity", "publish_state",
                 # ★新台経路の判定書★（2026-08-04・Codex71〜72回目）
                 "publication_policy", "page_decision"}


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


def run_site_audit(ignore_in_progress: bool = False) -> list:
    """サイト全体の監査を回す。★公開の前後の二段構えにするため★

    （2026-07-31・Codexの助言）
      公開してから監査するだけだと、見つけたときには既に世に出ている。
      置き換える前にも同じ監査を通し、**駄目なら公開しない**。

    ★ignore_in_progress★（2026-07-31・実際に動かして見つけた）
      監査の項目33は「公開中の目印があるか」を見る。
      公開の最終確認は**自分がその目印を持っている最中**に回るので、
      そのままだと必ず引っかかり、**書けた記事を毎回取り消していた**。
      目印を正しく持っている側だけが、この項目を外してよい。
      ★push の関所では絶対に外さない★（そこは残骸を止める場所）。
    """
    r = subprocess.run([sys.executable, os.path.join(BASE, "scripts", "audit_site.py"),
                        "--json"],
                       cwd=BASE, capture_output=True, text=True,
                       encoding="utf-8", errors="replace",
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    # ★監査そのものが壊れて終わった場合を「合格」にしない★（2026-08-01〜02・Codex23〜24回目）
    #   1回目の直しは「❌の行が無い非0は異常」だったが、
    #   ❌を1行出した**あとに**落ちると素通りする穴が残っていた（Codex24回目）。
    #   JSONで受ければ「完全に出力し終えたか」を機械で判定できる：
    #   途中で落ちた出力はJSONとして読めないか、項目が欠ける。
    try:
        got = json.loads(r.stdout or "")
        if not isinstance(got, dict) or not got:
            raise ValueError("形が違います")
        # ★項目1〜33がそろっているか★（欠け＝監査が途中で終わっている）
        nums = {k.split("_", 1)[0] for k in got}
        missing = {str(i) for i in range(1, 34)} - nums
        if missing:
            raise ValueError(f"項目が欠けています: {sorted(missing, key=int)[:5]}")
    except (ValueError, json.JSONDecodeError) as e:
        return ["サイト監査が異常終了しました（監査できていません）: "
                + str(e)[:100] + " / "
                + ((r.stderr or r.stdout or "").strip()[:150] or "出力なし")]
    out = []
    for key, items in got.items():
        if not items:
            continue
        # ★Codexへの報告漏れは公開の可否と関係ない★（開発の作法の話）
        if key.startswith("31_"):
            continue
        if ignore_in_progress and key.startswith("33_"):
            continue
        out.append(f"サイト監査: {key}: {len(items)}件 " + str(items[0])[:120])
    return out


def allowed_paths(slug: str, with_sitemap: bool = False) -> set:
    """★この経路が変えてよいファイル★（これ以外が変わっていたら止める）

    with_sitemap は AUTO_INDEXABLE の公開だけ True（無条件に許すと、
    AUTO_PENDING で誤って sitemap を書いた事故を検知できない・Codex72回目）。
    """
    got = {
        f"machines/{slug}/index.html",
        f"assets/data/machine-details/{slug}.json",
        "assets/data/machines.json",
    } | set(COUNT_FILES) | set(HUB_FILES)   # ★件数と一覧の行も整える★
    if with_sitemap:
        got.add("sitemap.xml")
    return got


def changed_paths() -> list:
    """いまリポジトリで変わっているファイル（gitに聞く）。"""
    # ★-z で読む★（2026-07-31・Codexの助言）
    #   ふつうの porcelain は、空白や日本語を含むパスを引用符で囲み、
    #   rename を「旧 -> 新」の1行で出す。素朴に切ると読み違える。
    r = subprocess.run(["git", "status", "--porcelain", "-z"], cwd=BASE,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0:
        raise PublishError(f"git status が失敗しました: {r.stderr[:200]}")
    out = []
    for line in r.stdout.split(chr(0)):
        if len(line) <= 3:
            continue
        path = line[3:].strip()
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


def check_no_stray_changes(slug: str, before_snap: dict,
                           with_sitemap: bool = False) -> list:
    """★許した3つ以外を書いていないか★（2026-07-31・Codexの条件）

    ★Codex指摘を再現して直した★
      以前は「実行前から変更中だったパス」を名前で除外していたので、
      **もともとdirtyだったCSSをさらに書き換えても見逃した**。
      実行前に取った中身の指紋と突き合わせる。
    """
    ng = []
    allowed = allowed_paths(slug, with_sitemap=with_sitemap)
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


SITE_ORIGIN = "https://uchidokoro.com"


def _sitemap_locs(text: str) -> list:
    import re as _re
    return _re.findall(r"<loc>([^<]+)</loc>", text)


def sitemap_line(slug: str) -> str:
    """追加する1行（1行形式・生成器 write_sitemap と同じ側に合わせる）。"""
    return f"  <url><loc>{SITE_ORIGIN}/machines/{slug}/</loc></url>"


def add_to_sitemap(before_text: str, slug: str) -> str:
    """★1行形式で </urlset> の直前に1件だけ足す★（復旧は同じ1行の完全一致除去）"""
    line = sitemap_line(slug)
    if line in before_text:
        raise PublishError(f"sitemap に {slug} の行が既にあります")
    marker = "</urlset>"
    if before_text.count(marker) != 1:
        raise PublishError("sitemap の形が想定と違います（</urlset> が1個でない）")
    return before_text.replace(marker, line + chr(10) + marker)


def remove_from_sitemap(text: str, slug: str) -> str:
    """add_to_sitemap が足した1行だけを外す（無ければそのまま返す）。"""
    line = sitemap_line(slug)
    return text.replace(line + chr(10), "", 1)


def check_sitemap_added(before_text: str, slug: str) -> list:
    """★期待した1件だけ増えたこと★（バイト一致でなく<loc>集合で見る・Codex72回目）"""
    with open(SITEMAP, encoding="utf-8") as f:
        now = f.read()
    b, n = _sitemap_locs(before_text), _sitemap_locs(now)
    want = f"{SITE_ORIGIN}/machines/{slug}/"
    ng = []
    if n.count(want) != 1:
        ng.append(f"sitemap に {slug} のURLが {n.count(want)} 件あります（1件のはず）")
    import collections as _c
    if _c.Counter([x for x in n if x != want]) != _c.Counter(b):
        ng.append("sitemap で追加した1件以外のURLが増減・変更されています")
    return ng


def check_served(slug: str, expect_noindex: bool = True) -> list:
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
        if expect_noindex:
            if len(vals) != 1 or "noindex" not in vals[0]:
                ng.append(f"配信されたHTMLの robots が {vals!r} です（noindex 1個のはず）")
        else:
            if len(vals) != 0:
                ng.append(f"index対象なのに配信HTMLに robots が {vals!r} あります")
    except Exception as e:                # noqa: BLE001
        ng.append(f"公開したページを引けません: {type(e).__name__}: {e}")
    finally:
        srv.shutdown()
        srv.server_close()
    return ng


def check_after(slug: str, before_pages: dict, rows_before: list,
                expect_in_sitemap: bool = False) -> list:
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
        sm_now = f.read()
    if expect_in_sitemap:
        if f"<loc>{SITE_ORIGIN}/machines/{slug}/</loc>" not in sm_now:
            ng.append("index対象なのに sitemap に載っていません")
    else:
        if f"/machines/{slug}/" in sm_now:
            ng.append("sitemap に noindex対象の機種が載っています（載せない決まりです）")
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
    # ★通知divは AUTO_INDEXABLE にも付く★（indexableと独立・2026-08-04・Codex72回目）
    if machine.get("status") == "preview" or _pdz.is_auto(machine):
        if _BANNER_HIDDEN not in html:
            raise PublishError("ひな型の断り書きバナーが見つかりません"
                               "（machine.html の作りが変わった可能性があります）")
        html = html.replace(_BANNER_HIDDEN, _BANNER_SHOWN, 1)
    return html


def publish_from_material(slug: str, name: str, maker: str, official_url: str,
                          release: str, material: dict,
                          apply_it: bool = False, before_write=None,
                          on_written=None) -> dict:
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
    return _publish_prebuilt(slug, machine, detail, apply_it=apply_it,
                             before_write=before_write, on_written=on_written)


def _publish_prebuilt(slug: str, machine: dict, detail: dict,
                      apply_it: bool = False, before_write=None,
                      on_written=None) -> dict:
    """★内部専用★ 外からは `publish_from_material` を使うこと。

    こちらは完成データを受け取るので、境界の検査でしか守れない。
    コマンドからは呼べないようにしてある（2026-07-31・Codex指摘1）。
    """
    if not apply_it:
        return _publish(slug, machine, detail, apply_it=False)
    with _OnlyOne():
        return _publish(slug, machine, detail, apply_it=True,
                        before_write=before_write, on_written=on_written)


# ★外から使ってよいのは publish_from_material だけ★（2026-07-31・Codex指摘4）
#   完成データを受け取る経路は名前を _ で始めて、import * でも出さない。
__all__ = ["publish_from_material", "check_page", "check_detail", "check_machine",
           "check_counts", "check_hubs_untouched", "render", "STATE"]


def _publish(slug: str, machine: dict, detail: dict, apply_it: bool = False,
             before_write=None, on_written=None) -> dict:
    """新台1件を公開する。★ページを先に置き、最後に一覧へ足す★"""
    out = {"slug": slug, "problems": [], "wrote": [], "html_bytes": 0}
    rows = _sj.read_rows(MACHINES)
    out["problems"] += check_before(slug, machine, rows)
    # ★区分（index対象かどうか）はここで一度だけ決めて全検査に配る★
    try:
        indexable = _pdz.machine_class(machine) == "AUTO_INDEXABLE"
    except _pdz.DecisionError:
        indexable = False      # check_before が既に問題として積んでいる
    out["problems"] += check_detail(slug, detail)
    out["problems"] += check_machine(slug, machine)
    # ★書き始める前にサイトが健全か確かめる★（2026-07-31・Codexの助言・二段構え）
    #   壊れた状態から公開すると、後で「どこまでが自分のせいか」分からなくなる。
    #   ★ページを置いた直後は設計上わざと不整合（一覧にまだ足していない）なので、
    #     その途中では監査しない★
    # ★前回の公開が途中で終わっていないか★（2026-07-31・Codex9回目）
    #   電源断だと、ページも一覧もそろってしまい、監査では区別できない。
    left = unfinished()
    if left:
        out["problems"].append(
            f"★前回の公開が途中で終わっています（{left.get('slug')} / "
            f"{left.get('started_at')}）★ "
            "★目印だけ消してはいけません★"
            "（中途半端な状態のまま『正常』として公開できてしまいます）。"
            "`python scripts/publish_new_machine.py --recover` で元に戻してください")
        return out
    out["problems"] += run_site_audit()
    # ★一覧・ランキングが、いまのデータと一致しているか★
    #   ずれたまま作り直すと、既存の公開内容まで変えてしまう。
    for x in check_hubs_untouched():
        out["problems"].append(
            x + "／先に `python scripts/build_hub_pages.py` 相当の作り直しが要ります")
    if out["problems"]:
        return out
    html = render(slug, machine, detail)
    out["html_bytes"] = len(html.encode("utf-8"))   # ★文字数ではなくバイト数★
    out["problems"] += check_page(slug, html, expect_noindex=not indexable)
    out["problems"] += check_only_allowed_values(slug, machine, detail, html)
    if out["problems"] or not apply_it:
        return out

    # ★ここが最初の書き込みの直前★（2026-07-31・Codex20回目）
    #   「1日1機種」の枠は、前の検査を全部通ってから使う。
    #   手前で使うと、途中公開・監査・早見表のずれで断られたときにも
    #   その日の枠が消えて、**別の正しい機種を公開できなくなる**。
    if before_write and not before_write():
        out["problems"].append("今日の担当ではありません（1日1機種）")
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
    # ★目印は、書き始める前に・戻し方つきで★
    backup_for_mark = {**hub_backup, MACHINES: machines_before.decode("utf-8")}
    if indexable:
        backup_for_mark[SITEMAP] = before_sitemap
    mark_start(slug, machine, backup_for_mark)
    machines_replaced = {}   # 一覧を置き換えたか（戻すため・置き換える前に立てる）
    sitemap_replaced = {}    # sitemap を置き換えたか（同上）

    def _cleanup():
        """★自分が作ったものだけ片付ける★（2026-07-31・Codex指摘3を再現して直した）

        以前は「置くはずだった場所」を消していたので、
        **たまたま同名で既にあった記事データを消して**しまい、
        しかも「元に戻しました」と報告していた（実際に再現した）。

        ★片付け切れた時だけ「途中」の目印を消す★（2026-08-03・Codex57回目）
          先に目印を消すと、消せなかった残骸があるのに復旧の手がかりだけ
          失われる。残った時は目印を保持し、残ったパスを問題として残す。
        """
        left = []
        for kind, q, want in reversed(made):
            try:
                if kind == "file" and os.path.isfile(q):
                    # ★自分が書いた中身のままの時だけ消す★（Codex指摘5）
                    with open(q, encoding="utf-8") as fh:
                        if _sha(fh.read()) != want:
                            out["problems"].append(
                                f"作った後に中身が変わっていたので消しませんでした: {q}")
                            left.append(q)
                            continue
                    os.remove(q)
                elif kind == "dir" and os.path.isdir(q):
                    os.rmdir(q)
            except OSError as e:
                out["problems"].append(f"片付けに失敗しました: {q}（{e}）")
                left.append(q)
        if left:
            out["problems"].append(
                "★片付け切れていないため『途中』の目印は残します"
                "（--recover で確かめてください）★")
            return False
        mark_done()                    # 片付け切れて初めて「途中」ではない
        return True

    try:
        # ① 記事データとページを置く（★この時点では一覧から辿れない★）
        #    "x" で開く＝既にあれば作らずに例外。存在確認との隙間も無くす。
        detail_text = json.dumps(detail, ensure_ascii=False, indent=1) + chr(10)
        # ★やる前に登録する★（2026-07-31・Codex指摘を再現して直した）
        #   以前は「置き換えが済んでから登録」だったので、
        #   os.replace が成功した直後に Ctrl+C が入ると、
        #   **できあがったファイルが片付けの対象にならず残った**（実際に再現）。
        #   write_atomic は一時ファイルを完成させてから置き換えるので、
        #   最終名に「書きかけ」は現れない。だから先に登録して安全。
        if os.path.exists(dp):
            raise FileExistsError(dp)
        made.append(("file", dp, _sha(detail_text)))
        write_atomic(dp, detail_text, new_only=True)
        d = os.path.dirname(page)
        if not os.path.isdir(d):
            made.append(("dir", d, None))
            os.makedirs(d)
        if os.path.exists(page):
            raise FileExistsError(page)
        made.append(("file", page, _sha(html)))
        write_atomic(page, html, new_only=True)
        # ★何を作ったかを目印にも残す★（復旧が「自分の作った物か」を見分ける）
        mark_created({f"machines/{slug}/index.html": _sha(html),
                      f"assets/data/machine-details/{slug}.json":
                          _sha(detail_text)})
    except FileExistsError as e:
        _cleanup()
        raise PublishError(f"同じ名前のファイルが既にあります（触っていません）: {e}")
    except BaseException as e:            # noqa: BLE001
        # ★Ctrl+C や強制終了でも巻き戻す★（2026-07-31・Codex指摘1）
        #   KeyboardInterrupt は Exception ではないので、
        #   以前は途中の状態を残したまま抜けていた。
        _cleanup()
        if isinstance(e, KeyboardInterrupt):
            raise
        raise PublishError(f"公開できませんでした（作ったものは消しました）: {e}")

    # ② ★一覧に足す前に全部確かめる★（2026-07-31）
    #   以前は machines.json まで書いてから確かめていたので、
    #   問題が見つかっても戻せなかった。ここで確かめれば、
    #   駄目なときは置いたファイルを消すだけで完全に元へ戻る。
    late = []
    # ★確かめている最中に例外が出ても片付ける★
    #   （2026-07-31・Codexが勧めた障害注入テストで見つけた）
    #   確認の関数が投げると、そのまま外へ抜けてページと記事データが残っていた。
    try:
        # ★書いたページと記事データが、そのままの中身か★（Codex指摘5）
        for path, want in ((page, _sha(html)), (dp, _sha(detail_text))):
            with open(path, encoding="utf-8") as f:
                if _sha(f.read()) != want:
                    late.append(f"書いたはずの中身と違います: {path}")
        late += check_served(slug, expect_noindex=not indexable)
        late += check_no_stray_changes(slug, before_snap,
                                       with_sitemap=indexable)
        # ★この時点では sitemap はまだ書いていない＝不変のはず★
        late += check_sitemap_kept(before_sitemap)
        now_pages = _existing_pages()
        for s_, h in before_pages.items():
            if s_ not in now_pages:
                late.append(f"既存ページが消えました: {s_}")
            elif now_pages[s_] != h:
                late.append(f"既存ページが書き換わりました: {s_}")
    except BaseException as e:            # noqa: BLE001
        _cleanup()
        if isinstance(e, KeyboardInterrupt):
            raise
        raise PublishError(f"確かめの最中に失敗しました（作ったものは消しました）: {e}")
    if late:
        # ★目印は _cleanup が「片付け切れた時だけ」消す★（2026-08-03・
        #   Codex60回目。ここで無条件に mark_done すると、ページ削除だけ
        #   失敗した時に残骸があるのに復旧の目印が消えた）
        if _cleanup():
            out["problems"] += late
            out["problems"].append(
                "★確かめで引っかかったので、置いたものを消して元に戻しました★")
        else:
            out["problems"] += late
        return out

    # ③ ここで初めて一覧へ足す（★これ以降トップページからリンクされる★）
    try:
        rows = _sj.read_rows(MACHINES)        # ★直前に読み直す★（競合対策）
        if any(m.get("slug") == slug for m in rows):
            _cleanup()
            out["problems"].append("書いている間に同じ機種が一覧へ入りました（やり直してください）")
            return out
        rows.append(machine)
        # ★一覧を置き換える前に「戻し方」を登録する★
        #   （2026-07-31・Codex指摘を再現：置き換え直後に中断すると戻らなかった）
        machines_replaced["yes"] = True
        write_atomic(MACHINES, json.dumps(rows, ensure_ascii=False, indent=1) + chr(10))
        # ★足した行の指紋も残す★（2026-07-31・Codex12回目）
        #   ページと同じで、人が直した行を巻き添えで消さないため。
        mark_created({f"machines.json#{slug}":
                      _sha(json.dumps(machine, ensure_ascii=False, sort_keys=True))})
        out["wrote"] = [dp, page, MACHINES]
        # ★index対象は sitemap にも1行足す★（2026-08-04・Codex72回目。
        #   1行形式・</urlset> 直前・復旧は同じ1行の完全一致除去）
        if indexable:
            sitemap_replaced["yes"] = True
            write_atomic(SITEMAP, add_to_sitemap(before_sitemap, slug))
            mark_created({f"sitemap.xml#{slug}": _sha(sitemap_line(slug))})
            out["wrote"].append(SITEMAP)
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
        tmps, swapped = [], []
        try:
            new_hubs = build_hubs()                      # ①全部メモリで作る
            # ★4ページそろっているか★（生成器が減らしても気づける・Codex指摘4）
            if set(new_hubs) != set(HUB_FILES):
                raise PublishError(
                    f"早見表が {sorted(new_hubs)} しか作られませんでした"
                    f"（{sorted(HUB_FILES)} のはず）")
            for rel, html2 in new_hubs.items():
                full = os.path.join(BASE, rel)
                tmp2 = full + f".new.{os.getpid()}"
                with open(tmp2, "w", encoding="utf-8", newline=chr(10)) as f:
                    f.write(html2)
                    f.flush()
                    os.fsync(f.fileno())
                tmps.append((tmp2, full))
            for tmp2, full in tmps:                      # ②一気に置き換える
                swapped.append(full)                     # ★やる前に登録★
                os.replace(tmp2, full)
                out["wrote"].append(full)
        except BaseException as e:        # noqa: BLE001  ★Ctrl+Cでも戻す★
            for tmp2, _f in tmps:
                if os.path.exists(tmp2):
                    os.remove(tmp2)
            # ★戻す途中で失敗しても、残りを戻し続ける★（Codexの助言）
            failed = []
            for full in swapped:
                text0 = hub_backup.get(full)
                if text0 is None:
                    continue
                try:
                    write_atomic(full, text0)
                except Exception as e2:       # noqa: BLE001
                    failed.append(f"{os.path.basename(full)}: {e2}")
            out["problems"].append(f"一覧・ランキングを作り直せませんでした（元に戻しました）: {e}")
            if failed:
                out["problems"].append(
                    "★戻せなかったファイルがあります（人が確かめてください）: "
                    + " / ".join(failed) + "★")
            if isinstance(e, KeyboardInterrupt):
                raise
    except BaseException as e:            # noqa: BLE001  ★Ctrl+Cでも戻す★
        if machines_replaced.get("yes"):
            try:
                write_atomic(MACHINES, machines_before.decode("utf-8"))
            except Exception:             # noqa: BLE001
                out["problems"].append("★一覧を戻せませんでした（人が確かめてください）★")
        if sitemap_replaced.get("yes"):
            try:
                write_atomic(SITEMAP, before_sitemap)
            except Exception:             # noqa: BLE001
                out["problems"].append("★sitemapを戻せませんでした（人が確かめてください）★")
        _cleanup()
        if isinstance(e, KeyboardInterrupt):
            raise
        raise PublishError(f"一覧に足せませんでした（作ったものは消しました）: {e}")

    # ④ 一覧に足したあとの最終確認
    late2 = check_after(slug, before_pages, rows[:-1],
                        expect_in_sitemap=indexable)
    late2 += (check_sitemap_added(before_sitemap, slug) if indexable
              else check_sitemap_kept(before_sitemap))
    # ★終わったあとにもう一度★
    #   ここは自分が「公開中」の目印を持っている最中なので、項目33だけ外す。
    #   （外さないと、書けた記事を毎回自分で取り消していた・実機で判明）
    late2 += run_site_audit(ignore_in_progress=True)
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
            write_atomic(MACHINES, machines_before.decode("utf-8"))
            if sitemap_replaced.get("yes"):
                # ★自分が書いた1行のままの時だけ戻す★（他人の変更を消さない）
                with open(SITEMAP, encoding="utf-8") as f:
                    sm_now2 = f.read()
                if _sha(sm_now2) == _sha(add_to_sitemap(before_sitemap, slug)):
                    write_atomic(SITEMAP, before_sitemap)
                else:
                    late2.append("★sitemapに別の変更が入っているため自動では"
                                 "戻しませんでした（人が確かめてください）★")
            for full, text0 in hub_backup.items():       # ★早見表も戻す★
                if text0 is not None:
                    write_atomic(full, text0)
            # ★目印は _cleanup が「片付け切れた時だけ」消す★（Codex60回目）
            if _cleanup():
                out["wrote"] = []
                late2.append("★一覧から外し、置いたものを消して元に戻しました★")
            else:
                out["wrote"] = []
        else:
            late2.append("★別の書き込みが入っているため、自動では戻しませんでした★"
                         "（人が確かめてください）")
        out["problems"] += late2
        return out
    # ★「途中」の目印を消す前に、次の担当へ引き継ぐ★（2026-07-31・Codex22回目）
    #   ここで消してから呼び出し元が push待ちの目印を作っていたので、
    #   その間に止まると「公開ファイルはあるが目印はどこにも無い」状態になった。
    #   翌日は何も復旧できず、機種は『既に登録』と判定されて待ち行列から消え、
    #   残った変更が後続のpushも塞いでいた。
    if on_written:
        try:
            on_written(slug)
        except Exception as e:            # noqa: BLE001
            out["problems"].append(f"引き継ぎに失敗しました（pushしないでください）: {e}")
            return out
    mark_done()                        # ★ここまで来て初めて「終わった」★
    return out


def _find_stale_held(full: str, want: str):
    """★前回の復旧が残した退避物（*.recover.<旧PID>）を探す★（Codex62回目）

    返すもの: (指紋が一致した退避物のパス or None, 指紋が合わない退避物のリスト)
    一致しない退避物は「人が確かめる」対象（触らない）。
    """
    import glob as _glob
    hit, bad = None, []
    for o in sorted(_glob.glob(full + ".recover.*")):
        if not os.path.isfile(o):
            continue
        try:
            with open(o, encoding="utf-8") as f:
                if _sha(f.read()) == want:
                    if hit is None:
                        hit = o
                    else:
                        bad.append(o)     # 一致が2つ＝想定外。人へ
                    continue
        except OSError:
            pass
        bad.append(o)
    return hit, bad


RECOVER_LOCK = os.path.join(BASE, ".recover.lock")


def recover(apply_it: bool = False) -> dict:
    """★復旧も同時に2つ走らせない★（2026-07-31・Codex13回目）

    目印の存在は「新しい公開」を止めるが、
    同じ目印を読んで動く復旧処理どうしは止めない。
    2つが同時に「指紋が一致した」と判断して消しに行ける。
    """
    if not apply_it:
        return _recover(apply_it=False)
    # ★公開と復旧は同じロックでも排他する★（2026-08-03・Codex61回目）
    #   公開が mark_start() の直後（ファイル作成前）の隙間に復旧が走ると、
    #   「created が空＝何も作られていない」と判断して目印を消し、
    #   進行中の公開が目印なしになる。公開のロックを先に取れば、
    #   公開中の復旧・復旧中の公開はどちらも待たされて成立しない。
    with _OnlyOne():
        with _OnlyOne(RECOVER_LOCK):
            return _recover(apply_it=True)


def _recover(apply_it: bool = False) -> dict:
    """★途中で終わった公開を、処理前の状態に戻す★（2026-07-31・Codex10〜11回目）

    ★これは「厳密に元へ戻す」ではなく「今回の追加を打ち消す」処理★
      早見表は生成物なので、いまのデータから作り直せば整合します。
      ただし「処理前とバイト単位で同じ」ではありません（生成器が変われば変わる）。

    ★人が直したものは消さない★（Codex11回目）
      作ったときの指紋と違えば、誰かが手を入れたということなので、
      消さずに知らせて止まります。

    ★何度走らせても平気★ 既に片付いているものは「済んでいる」と扱います。
    """
    left = unfinished()
    out = {"slug": left.get("slug"), "problems": [], "restored": [],
           "todo": [], "kept": []}
    if not left:
        out["problems"].append("途中で終わった公開はありません")
        return out
    slug = left.get("slug") or ""
    created = left.get("created") or {}
    if not slug or not _SLUG_OK.match(slug):
        out["problems"].append(
            f"目印から機種名を読めません（{slug!r}）。手で確かめてください")
        return out
    planned = left.get("planned") or []
    if not created and planned:
        # ★作る前に落ちた場合★（目印に指紋が無い）
        #   planned に載っているものが実際にあるなら、
        #   それは「作ったが指紋を書く前に落ちた」もの。中身は分からないので人へ。
        stuck = [rel for rel in planned
                 if not rel.startswith("machines.json#")
                 and os.path.isfile(os.path.join(BASE, rel))]
        if stuck:
            out["problems"].append(
                "★作ったものの指紋が残る前に止まっています。"
                "中身が正しいか人が確かめてください★")
            for rel in stuck:
                out["problems"].append(f"  確かめる: {rel}")
            return out
        # 何も作られていないなら、目印を消すだけで元通り
        out["todo"].append("何も作られていないので、目印を消すだけです")
        if apply_it:
            mark_done()
            out["restored"].append("（目印を消しました）")
            _clear_stale_push_marker(slug, out)
        return out
    if not created:
        # ★目印が壊れていても、作られうる物は決まっている★
        #   指紋が無いので「自分が作った物か」は判断できない。
        #   その場合は消さずに、何を確かめるべきかだけ知らせる。
        out["problems"].append(
            "目印に『作ったものの指紋』がありません（壊れているか、"
            "作る前に止まった可能性）。下のファイルを人が確かめてください")
        for rel in (f"machines/{slug}/index.html",
                    f"assets/data/machine-details/{slug}.json"):
            if os.path.isfile(os.path.join(BASE, rel)):
                out["problems"].append(f"  確かめる: {rel}")
        rows0 = _sj.read_rows(MACHINES)
        if any(m.get("slug") == slug for m in rows0):
            out["problems"].append(f"  確かめる: 一覧に {slug} が入っています")
        return out

    # ★目印に書かれたパスをそのまま信用しない★（2026-07-31・Codex12回目）
    #   目印が書き換えられていたら、関係ないファイルを消しに行ける。
    allowed_created = {f"machines/{slug}/index.html",
                       f"assets/data/machine-details/{slug}.json",
                       f"machines.json#{slug}",
                       # ★index対象の公開は sitemap の1行も作る★（Codex72回目）
                       f"sitemap.xml#{slug}"}
    stray = sorted(set(created) - allowed_created)
    if stray:
        out["problems"].append(
            f"★目印に知らないファイルが入っています: {stray[:3]}。"
            "触らずに止めました。人が確かめてください★")
        return out

    # ★消す前に、一覧の行も先に確かめる★（2026-08-03・Codex57回目）
    #   ページ・詳細を消した後に一覧の行の食い違いで止まると、
    #   「記事は消えたのに一覧に行だけ残る」中途半端な状態を自分で作る。
    #   全部を確かめてから、初めて消し始める（全か無か）。
    rows_pre = _sj.read_rows(MACHINES)
    hit_pre = [i for i, m in enumerate(rows_pre) if m.get("slug") == slug]
    if len(hit_pre) > 1:
        out["problems"].append(
            f"★一覧に {slug} が {len(hit_pre)} 件あります。何も消さずに"
            "止めました。手で確かめてください★")
        return out
    if hit_pre:
        want_row_pre = (created or {}).get(f"machines.json#{slug}")
        now_row_pre = _sha(json.dumps(rows_pre[hit_pre[0]],
                                      ensure_ascii=False, sort_keys=True))
        if want_row_pre and now_row_pre != want_row_pre:
            out["kept"].append(f"machines.json#{slug}")
            out["problems"].append(
                f"★一覧の {slug} の行が、足したときと中身が違います"
                "（誰かが直した可能性）。ページ・詳細も含め何も消さずに"
                "止めました。人が確かめてください★")
            return out

    # ① 作ったものを消す（★自分が作った中身のままの時だけ★）
    #   ★確かめてから消すまでの隙間をなくす★（2026-07-31・Codex13回目）
    #     「読む→一致→消す」の間に人が直すと、その編集ごと消える。
    #     先に別名へ動かしてしまえば、以降の編集は別のファイルに向かうので、
    #     動かしたものを確かめて消せば取り違えない。
    # ★全部を確保・検証してから、初めて消す（全か無か）★（2026-08-03・
    #   Codex58回目。1件ずつ検証・削除すると、2件目の指紋違いで止まった時
    #   1件目だけが消えており、404や欠損記事を自分で作っていた）
    held_map, grab_fail = [], False
    for rel, want in created.items():
        if rel.startswith(("machines.json#", "sitemap.xml#")):
            continue                      # 一覧の行は上と②・sitemapの行は②bで扱う
        full = os.path.join(BASE, rel)
        # ★前回の復旧が残した退避物（旧PID名）を再接続する★
        #   （2026-08-03・Codex62回目。巻き戻しの復元に失敗すると
        #     *.recover.<旧PID> のまま残り、元パスが無いため以後の復旧が
        #     何度走っても見つけられず、新台公開が恒久停止した。
        #     指紋が一致した退避物だけを自分の held_map に引き取る）
        _old_hit, _old_bad = _find_stale_held(full, want)
        if _old_bad:
            out["problems"].append(
                f"★{rel} の退避物が残っていますが、作った時の指紋と合いません: "
                f"{_old_bad[0]}。触らずに止めました。人が確かめてください★")
            grab_fail = True
            break
        if not os.path.isfile(full):
            if _old_hit:
                out["todo"].append(f"消す: {rel}（前回の退避物を引き取り）")
                if apply_it:
                    held_map.append((rel, full, _old_hit, want))
            continue                      # 既に片付いている（何度走らせても平気）
        if _old_hit:
            # 元パスも退避物もある＝退避物は自分の複製（指紋一致）なので消してよい
            out["todo"].append(f"消す: {_old_hit}（自分の複製）")
            if apply_it:
                try:
                    os.remove(_old_hit)
                except OSError as e:      # noqa: BLE001
                    out["problems"].append(f"複製を消せませんでした: {e}")
                    grab_fail = True
                    break
        out["todo"].append(f"消す: {rel}")
        if not apply_it:
            continue
        held = f"{full}.recover.{os.getpid()}"
        try:
            os.replace(full, held)        # ★先に確保する（原子的）★
        except OSError as e:
            out["problems"].append(f"{rel} を確保できませんでした: {e}")
            grab_fail = True
            break
        held_map.append((rel, full, held, want))
    if apply_it:
        bad = []
        if not grab_fail:
            for rel, full, held, want in held_map:
                with open(held, encoding="utf-8") as f:
                    if _sha(f.read()) != want:
                        bad.append(rel)
    def _undo_held() -> bool:
        """退避したファイルを全部原位置へ戻す（何も消さなかったことにする）。

        ★成否を返す★（2026-08-03・Codex62回目）。戻せなかった退避物は
        旧PID名のまま残るが、次の復旧が指紋一致で引き取る（上の再接続）。
        """
        ok_all = True
        for rel_, full_, held_, _w2 in held_map:
            try:
                os.replace(held_, full_)
            except OSError as e:          # noqa: BLE001
                ok_all = False
                out["problems"].append(
                    f"★{rel_} を戻せませんでした（{held_} に退避したまま・"
                    f"次の --recover --apply が引き取ります）: {e}★")
        return ok_all

    if apply_it:
        if grab_fail or bad:
            _undo_held()
            for rel in bad:
                out["kept"].append(rel)
                out["problems"].append(
                    f"★{rel} は作ったときと中身が違います（誰かが直した可能性）。"
                    "何も消さずに止めました。人が確かめてください★")
            return out
        # ★退避物はまだ消さない★（2026-08-03・Codex59回目）
        #   一覧の行・早見表・監査まで成功した最後に消す。
        #   途中の失敗では _undo_held() で全部戻せるようにしておく
        #   （消してしまうと、目印に本文が無いので自動では戻せない）。
        for rel, full, held, _w_ in held_map:
            out["restored"].append(rel)
    if out["kept"]:
        return out                        # ★1つでも判断がつかなければ進まない★

    # ② 一覧から今回の1件だけを外す（★同じslugの行だけ・1件だけ★）
    rows = _sj.read_rows(MACHINES)
    with open(MACHINES, encoding="utf-8") as f:
        machines_text_before = f.read()   # ★失敗したら戻すための正本★
    hit = [i for i, m in enumerate(rows) if m.get("slug") == slug]
    if len(hit) > 1:
        _undo_held()
        out["problems"].append(
            f"★一覧に {slug} が {len(hit)} 件あります。何も消さずに戻しました。"
            "手で確かめてください★")
        return out
    if hit:
        # ★行の中身が作ったときと同じ時だけ外す★（2026-07-31・Codex12回目）
        #   同名が1件かどうかだけ見ていたので、
        #   あとから人が足した別名や狙い目ごと消していた（実際に再現）。
        want_row = (created or {}).get(f"machines.json#{slug}")
        now_row = _sha(json.dumps(rows[hit[0]], ensure_ascii=False, sort_keys=True))
        if want_row and now_row != want_row:
            _undo_held()
            out["kept"].append(f"machines.json#{slug}")
            out["problems"].append(
                f"★一覧の {slug} の行が、足したときと中身が違います"
                "（誰かが直した可能性）。何も消さずに戻しました。"
                "人が確かめてください★")
            return out
        out["todo"].append(f"一覧から外す: {slug}")
        if apply_it:
            del rows[hit[0]]
            write_atomic(MACHINES, json.dumps(rows, ensure_ascii=False,
                                              indent=1) + chr(10))
            out["restored"].append("assets/data/machines.json")

    # ②b sitemap から今回の1行だけを外す（★足した行そのもの・1行だけ★）
    with open(SITEMAP, encoding="utf-8") as f:
        sitemap_text_before = f.read()    # ★失敗したら戻すための正本★
    sm_replaced = {}
    smap_key = f"sitemap.xml#{slug}"
    if smap_key in (created or {}):
        line = sitemap_line(slug)
        if _sha(line) != created[smap_key]:
            _undo_held()
            out["kept"].append(smap_key)
            out["problems"].append(
                "★sitemap の行の指紋が、足したときの記録と合いません。"
                "何も消さずに戻しました。人が確かめてください★")
            return out
        if line in sitemap_text_before:
            out["todo"].append(f"sitemap から外す: {slug}")
            if apply_it:
                sm_replaced["yes"] = True
                write_atomic(SITEMAP,
                             remove_from_sitemap(sitemap_text_before, slug))
                out["restored"].append("sitemap.xml")
        # 無ければ既に片付いている（何度走らせても平気）

    def _undo_all():
        """一覧の行・sitemap・退避物を元へ戻し、早見表も元データで作り直す。

        ★退避物を最初に戻す★（2026-08-03・Codex61回目）
          一覧の書き戻しが先だと、そこで失敗した時に退避物が
          退避名のまま残り、次の復旧が見つけられなかった。
        """
        _undo_held()
        write_atomic(MACHINES, machines_text_before)
        if sm_replaced.get("yes"):
            write_atomic(SITEMAP, sitemap_text_before)
        try:
            for rel_, html_ in build_hubs().items():
                full_ = os.path.join(BASE, rel_)
                with open(full_, encoding="utf-8") as f_:
                    if f_.read() != html_:
                        write_atomic(full_, html_)
        except Exception as e:            # noqa: BLE001
            out["problems"].append(f"★早見表を元に戻せませんでした: {e}★")

    # ③ 早見表を、いまのデータから作り直す
    if apply_it:
        try:
            for rel, html in build_hubs().items():
                full = os.path.join(BASE, rel)
                with open(full, encoding="utf-8") as f:
                    same = (f.read() == html)
                if not same:
                    write_atomic(full, html)
                    out["restored"].append(rel)
        except Exception as e:            # noqa: BLE001
            _undo_all()
            out["problems"].append(
                f"★早見表の作り直しに失敗したため、全部元に戻しました: {e}★")
            return out
    else:
        out["todo"].append("早見表4ページを作り直す")

    if apply_it:
        # ★戻し終わったか確かめてから目印を消す★
        #   監査の項目33は「目印がある＝途中」を見るので、
        #   消す前に回すと自分の目印を自分で見つけて永久に詰まる。
        ng = [x for x in run_site_audit() if "33_" not in x]
        if ng:
            _undo_all()
            out["problems"] += ng
            out["problems"].append(
                "★戻したあとの監査に落ちたため、全部元に戻しました。"
                "目印は消しません★")
            return out
        # ★一覧・早見表・監査まで成功して、初めて退避物を消す★
        #   （2026-08-03・Codex59回目。先に消すと、後段の失敗で
        #     「先に消したファイルだけ自動復元できない」中途半端が残った）
        _del_fail = []
        for rel, full, held, _w_ in held_map:
            try:
                os.remove(held)
            except OSError:               # noqa: BLE001
                # ★消せなかった退避物は元パスへ戻す★（2026-08-03・Codex60回目）
                #   退避名（*.recover.<pid>）のまま残すと、目印に場所が
                #   書かれていないため、次の --recover では見つからず
                #   （元パスが無い＝held_mapが空）、目印だけ消えて
                #   未追跡ファイルが後続のpushを全部止めた。
                #   元パスへ戻せば、次の --recover が普通にやり直せる。
                try:
                    os.replace(held, full)
                    _del_fail.append(f"{rel}（元パスへ戻しました）")
                except OSError as e2:     # noqa: BLE001
                    _del_fail.append(f"{held}（戻せず退避名のまま: {e2}）")
        if _del_fail:
            out["problems"].append(
                "★退避物を消せませんでした（目印は残します・もう一度 "
                "--recover --apply でやり直せます）: "
                + " / ".join(_del_fail)[:200] + "★")
            return out
        d = os.path.join(BASE, "machines", slug)
        if os.path.isdir(d) and not os.listdir(d):
            os.rmdir(d)
        # ★退避物が1つでも残っている間は目印を消さない★（2026-08-03・
        #   Codex61回目。目印が消えると次の復旧が退避物を見つけられず、
        #   未追跡ファイルが後続のpushを止め続ける）
        import glob as _glob
        _strays = []
        for rel in allowed_created:
            if rel.startswith("machines.json#"):
                continue
            _strays += [x for x in _glob.glob(
                os.path.join(BASE, rel) + ".recover.*") if os.path.isfile(x)]
        if _strays:
            out["problems"].append(
                "★退避物が残っているため目印は消しません: "
                + " / ".join(sorted(set(_strays))[:3])[:200] + "★")
            return out
        mark_done()                       # ★最後の操作★（Codex11回目の助言）
        out["restored"].append("（目印を消しました）")
        _clear_stale_push_marker(slug, out)
    else:
        d = os.path.join(BASE, "machines", slug)
        if os.path.isdir(d):
            out["todo"].append(f"消す: machines/{slug}/（空になった時）")
        out["todo"].append("同じ機種のコミット前push待ちの目印があれば消す")
    return out


def _clear_stale_push_marker(slug: str, out: dict) -> None:
    """★戻した公開のpush待ちの目印も片付ける★（2026-08-02・Codex56回目）

    公開部は「途中」の目印を消す**前**にpush待ちの目印を作る（引き継ぎの
    隙間を無くすため・Codex22回目）。その間に止まると両方が残り、
    復旧で「途中」を戻しても push待ちだけが残った。翌晩からは
    変更なしのツリーをコミットしようとして毎晩失敗し、自動経路が
    恒久停止する（Codex56回目の指摘・コードで確認）。
    ★消すのは「同じ機種・コミット前（WRITTEN・sha無し）」の目印だけ★。
    コミット済み（sha入り）や別機種の目印は push側の仕事なので触らない。
    """
    p = os.path.join(BASE, ".push-pending.json")
    if not os.path.isfile(p):
        return
    try:
        got = _sj.read_json(p, expect=dict)
    except Exception:                     # noqa: BLE001
        out["problems"].append(
            "★push待ちの目印が壊れています。人が確かめてください★")
        return
    stage = got.get("stage") or ("COMMITTED" if got.get("sha") else "WRITTEN")
    if got.get("slug") == slug and stage == "WRITTEN" and not got.get("sha"):
        os.remove(p)
        out["restored"].append("（push待ちの目印も消しました＝コミット前だったため）")
    elif got.get("slug") == slug:
        out["problems"].append(
            f"★{slug} のpush待ちの目印がコミット済みの形で残っています。"
            "push側の再開処理に任せます（消していません）★")


# ---------------------------------------------------------------- selftest

def _raises(fn) -> bool:
    try:
        fn()
    except Exception:                        # noqa: BLE001
        return True
    return False


def selftest() -> int:
    import inspect
    import inspect
    import tempfile as _tf
    results = []
    nl = chr(10)

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    rows = _sj.read_rows(MACHINES)
    _pd_ok = {"schema_version": _pdz.SCHEMA, "indexable": False}
    ok_machine = {"slug": "zzz_test", "name": "テスト機",
                  "publication_policy": _pdz.SCHEMA, "page_decision": _pd_ok,
                  "publish_state": STATE}
    t("★新しい機種なら前提を通る★", check_before("zzz_test", ok_machine, rows) == [])
    t("★★既にある機種は拒否する★★（上書きしない）",
      check_before(rows[0]["slug"],
                   {**ok_machine, "slug": rows[0]["slug"]}, rows))
    t("★★判定書と旧statusの同居は公開しない★★（fail-closed・Codex71回目）",
      any("判定書" in x or "区分" in x for x in
          check_before("zzz_test", {**ok_machine, "status": "preview"}, rows)))
    t("★★未知のpolicyは公開しない★★",
      any("判定書" in x or "区分" in x for x in
          check_before("zzz_test",
                       {**ok_machine, "publication_policy": "other/v9"}, rows)))
    t("★★判定書の欠落は公開しない★★",
      any("判定書" in x or "区分" in x for x in
          check_before("zzz_test",
                       {k: v for k, v in ok_machine.items()
                        if k != "page_decision"}, rows)))
    t("★★旧status契約（LEGACY_PREVIEW）はこの経路で公開しない★★",
      any("区分" in x for x in
          check_before("zzz_test", {"slug": "zzz_test", "name": "テスト機",
                                    "status": "preview",
                                    "publish_state": STATE}, rows)))
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
    # ★index対象（AUTO_INDEXABLE）: robots meta が無いことを要求★（Codex72回目）
    good_indexable = good.replace(
        '<meta name="robots" content="noindex,follow">', "")
    t("★★index対象は robots 無しで通る★★",
      check_page("zzz_test", good_indexable, expect_noindex=False) == [])
    t("★★index対象に noindex が付いていたら止める★★（逆方向もfail-closed）",
      any("robots" in x for x in
          check_page("zzz_test", good, expect_noindex=False)))
    t("★★時間で嘘になる語（導入予定等）が入っていたら止める★★"
      "（鮮度ゲート・Codex70回目）",
      any("時間で嘘になる語" in x for x in
          check_page("zzz_test",
                     good.replace("</body>", "<p>2026年9月導入予定</p></body>"))))
    # ★sitemap の追加・除去（1行形式・1件だけ）★
    _sm0 = ('<?xml version="1.0" encoding="UTF-8"?>' + chr(10)
            + '<urlset>' + chr(10)
            + '  <url><loc>https://uchidokoro.com/machines/aaa/</loc></url>'
            + chr(10) + '</urlset>' + chr(10))
    _sm1 = add_to_sitemap(_sm0, "zzz_new")
    t("★★sitemapへ1行だけ足せる（</urlset>直前・1行形式）★★",
      _sitemap_locs(_sm1) == ["https://uchidokoro.com/machines/aaa/",
                              "https://uchidokoro.com/machines/zzz_new/"])
    def _pub_raises(fn):
        try:
            fn()
            return False
        except PublishError:
            return True
    t("　同じ行の二重追加は止める",
      _pub_raises(lambda: add_to_sitemap(_sm1, "zzz_new")))
    t("★★除去は自分が足した1行の完全一致だけ★★（元に戻る）",
      remove_from_sitemap(_sm1, "zzz_new") == _sm0)
    _real_smp = globals().get("SITEMAP")
    try:
        import tempfile as _tf65
        _smd = _tf65.mkdtemp(prefix="uchi_sm_")
        globals()["SITEMAP"] = os.path.join(_smd, "sitemap.xml")
        with open(SITEMAP, "w", encoding="utf-8") as _f65:
            _f65.write(_sm1)
        t("★★check_sitemap_added: 正しい1件追加は通る★★",
          check_sitemap_added(_sm0, "zzz_new") == [])
        with open(SITEMAP, "w", encoding="utf-8") as _f65:
            _f65.write(add_to_sitemap(_sm1, "zzz_two"))
        t("　2件増えていたら止める",
          check_sitemap_added(_sm0, "zzz_new"))
        with open(SITEMAP, "w", encoding="utf-8") as _f65:
            _f65.write(_sm1.replace("machines/aaa", "machines/bbb"))
        t("　1件追加＋別URLの書き換えは止める",
          check_sitemap_added(_sm0, "zzz_new"))
    finally:
        globals()["SITEMAP"] = _real_smp
        __import__("shutil").rmtree(_smd, ignore_errors=True)
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
                   "publication_policy": _pdz.SCHEMA,
                   "page_decision": {"schema_version": _pdz.SCHEMA,
                                     "indexable": False},
                   "release_date": "2026-09",
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

    # ★形だけの試験をやめる★（2026-07-31・Codex指摘：常に合格していた）
    t("★いまは早見表がデータと一致している★", check_hubs_untouched() == [])
    _real_build = build_hubs
    try:
        globals()["build_hubs"] = lambda: {"guide-ichiran.html": "ちがう中身"}
        t("★★早見表がずれていたら見つける★★",
          any("違います" in x for x in check_hubs_untouched()))
        globals()["build_hubs"] = lambda: {"guide-ichiran.html": "x"}
        t("　4ページそろっていなければ気づける（生成器が減らした場合）",
          set(build_hubs()) != set(HUB_FILES))
    finally:
        globals()["build_hubs"] = _real_build
    import tempfile as _tf3
    _d3 = _tf3.mkdtemp(prefix="uchi_atomic_")
    try:
        _p3 = os.path.join(_d3, "a.txt")
        write_atomic(_p3, "ほんぶん")
        t("★一時ファイルに完成させてから置き換える★",
          open(_p3, encoding="utf-8").read() == "ほんぶん"
          and not [x for x in os.listdir(_d3) if ".tmp." in x])
        t("★★新しく作る時に既にあれば作らない★★",
          _raises(lambda: write_atomic(_p3, "うわがき", new_only=True))
          and open(_p3, encoding="utf-8").read() == "ほんぶん")
        t("　書きかけの一時ファイルを残さない",
          len(os.listdir(_d3)) == 1)
    finally:
        __import__("shutil").rmtree(_d3, ignore_errors=True)

    t("★いまは途中で終わった公開が残っていない★", unfinished() == {})
    _real_marker = IN_PROGRESS
    _md = _tf.mkdtemp(prefix="uchi_mark_")
    try:
        globals()["IN_PROGRESS"] = os.path.join(_md, "mark.json")
        t("★★目印を作れば「途中」と分かる★★"
          "（電源断ではページも一覧もそろってしまい、監査では区別できない）",
          (mark_start("zzz_mark", {"name": "試験"},
                      {os.path.join(BASE, "README.md"): "元の中身"})
           or unfinished().get("slug")) == "zzz_mark")
        t("★★戻し方を目印に持っている★★（目印だけ消すと中途半端なまま公開できる）",
          unfinished().get("restore"))
        t("★★同じ目印を二重に作れない★★（同時に2つ始まらない）",
          _raises(lambda: mark_start("zzz_two", {"name": "試験2"}, {})))
        mark_done()
        t("　消せば「途中」ではなくなる", unfinished() == {})
    finally:
        globals()["IN_PROGRESS"] = _real_marker
        __import__("shutil").rmtree(_md, ignore_errors=True)
    t("★★人が直したページは消さない★★（作ったときの指紋と違えば止まる・Codex11回目）",
      "created" in inspect.getsource(_recover)
      and "誰かが直した可能性" in inspect.getsource(_recover))
    t("★★復旧も同時に2つ走らせない★★（双方が指紋一致と判断して消しに行ける）",
      "RECOVER_LOCK" in inspect.getsource(recover))
    t("★★確かめてから消すまでの隙間をなくす★★"
      "（読む→一致→消すの間に人が直すと、その編集ごと消える）",
      "os.replace(full, held)" in inspect.getsource(_recover)
      and "os.replace(held_, full_)" in inspect.getsource(_recover))
    t("★★作る前に『これから作る』を目印へ残す★★"
      "（作ってから書く形だと、その隙間で落ちた残骸を特定できない）",
      "planned" in inspect.getsource(mark_start))
    t("★★一覧の行も、足したときと同じ時だけ外す★★"
      "（人が足した別名ごと消していた・実際に再現）",
      "足したときと中身が違います" in inspect.getsource(_recover))
    t("★★目印に書かれたパスをそのまま信用しない★★（書き換えられたら別のファイルを消せる）",
      "知らないファイルが入っています" in inspect.getsource(_recover))
    t("★★一覧から外すのは同じslugが1件のときだけ★★（複数あれば人へ）",
      "len(hit) > 1" in inspect.getsource(_recover))
    t("　目印が壊れていたら消さずに人へ知らせる",
      "作ったものの指紋』がありません" in inspect.getsource(_recover))
    t("★★消す前に一覧の行も先に確かめる（全か無か）★★"
      "（ページを消した後に行の食い違いで止まると中途半端が残る・Codex57回目）",
      "rows_pre" in inspect.getsource(_recover)
      and "何も消さずに" in inspect.getsource(_recover))
    t("★★ファイルの削除も全部を確保・検証してから（全か無か）★★"
      "（2件目の指紋違いで1件目だけ消え404を自作できた・Codex58回目）",
      "held_map" in inspect.getsource(_recover)
      and "_undo_held" in inspect.getsource(_recover))
    t("★★退避物を消すのは一覧・早見表・監査の成功後だけ★★"
      "（先に消すと後段の失敗で自動復元できない・Codex59回目）",
      "_undo_all" in inspect.getsource(_recover)
      and "初めて退避物を消す" in inspect.getsource(_recover)
      and "machines_text_before" in inspect.getsource(_recover))
    t("★★片付けに失敗したら目印を消さない（呼び出し側も）★★"
      "（_cleanup後の無条件mark_doneで残骸があるのに目印が消えた・Codex60回目）",
      "if _cleanup():" in inspect.getsource(_publish)
      and "return False" in inspect.getsource(_publish))
    t("★★消せなかった退避物は元パスへ戻す★★"
      "（退避名のままだと次のrecoverが見つけられず回収不能・Codex60回目）",
      "元パスへ戻しました" in inspect.getsource(_recover))
    t("★★公開と復旧は同じロックで排他★★"
      "（mark_startとファイル作成の隙間に復旧が目印を消せた・Codex61回目）",
      "with _OnlyOne():" in inspect.getsource(recover))
    # ★★Codex62回目：旧PIDの退避物の再接続★★
    _fs_dir = __import__("tempfile").mkdtemp(prefix="uchi_stale_")
    try:
        _fp = os.path.join(_fs_dir, "index.html")
        _want62 = _sha("中身A")
        with open(_fp + ".recover.100", "w", encoding="utf-8") as f:
            f.write("中身A")
        _hit, _bad = _find_stale_held(_fp, _want62)
        t("★★前回の退避物（旧PID名）を指紋一致で見つける★★"
          "（見失うと復旧が恒久に完走できなかった・Codex62回目）",
          _hit == _fp + ".recover.100" and _bad == [])
        with open(_fp + ".recover.200", "w", encoding="utf-8") as f:
            f.write("別の中身")
        _hit2, _bad2 = _find_stale_held(_fp, _want62)
        t("　指紋が合わない退避物は「人が確かめる」側に分ける",
          _hit2 == _fp + ".recover.100"
          and _bad2 == [_fp + ".recover.200"])
        t("　復旧の入口が退避物を引き取る配線",
          "_find_stale_held(full, want)" in inspect.getsource(_recover)
          and "前回の退避物を引き取り" in inspect.getsource(_recover))
    finally:
        __import__("shutil").rmtree(_fs_dir, ignore_errors=True)
    t("★★退避物が残っている間は目印を消さない＋巻き戻しは退避物から★★"
      "（Codex61回目）",
      "退避物が残っているため目印は消しません" in inspect.getsource(_recover)
      and inspect.getsource(_recover).index("_undo_held()")
      < inspect.getsource(_recover).index("write_atomic(MACHINES, machines_text_before)"))
    t("★★外部の材料JSONからは公開（--apply）できない★★"
      "（出典の再検証を通らない値を記事化できた・Codex58回目）",
      "外部の材料JSONからの公開" in inspect.getsource(main)
      and "apply_it=False" in inspect.getsource(main))
    t("★★失敗時の片付けは、片付け切れた時だけ目印を消す★★"
      "（残骸があるのに復旧の手がかりだけ失われた・Codex57回目）",
      "片付け切れて初めて" in inspect.getsource(_publish)
      and "片付け切れていないため" in inspect.getsource(_publish))
    # ★★Codex56回目：復旧はpush待ちの目印も片付ける★★
    _pp = os.path.join(BASE, ".push-pending.json")
    if os.path.isfile(_pp):
        # 本物のpush待ちがある時は触らない（試験は挙動の代わりに配線だけ見る）
        t("★★復旧がコミット前のpush待ちの目印も消す★★（配線のみ確認・Codex56回目）",
          "_clear_stale_push_marker" in inspect.getsource(_recover))
    else:
        try:
            import json as _js56
            write_atomic(_pp, _js56.dumps(
                {"slug": "zzz_test56", "stage": "WRITTEN", "sha": ""}))
            _o1 = {"problems": [], "restored": [], "todo": []}
            _clear_stale_push_marker("zzz_test56", _o1)
            _gone = not os.path.isfile(_pp)
            write_atomic(_pp, _js56.dumps(
                {"slug": "zzz_test56", "stage": "COMMITTED", "sha": "abc123"}))
            _o2 = {"problems": [], "restored": [], "todo": []}
            _clear_stale_push_marker("zzz_test56", _o2)
            _kept = os.path.isfile(_pp)
            t("★★復旧がコミット前（WRITTEN）のpush待ちの目印を消す★★"
              "（残ると毎晩の空コミット失敗で自動経路が恒久停止・Codex56回目）",
              _gone and _o1["restored"])
            t("　コミット済み（sha入り）の目印は消さない（push側の再開に任せる）",
              _kept and _o2["problems"])
        finally:
            try:
                os.remove(_pp)
            except OSError:
                pass
    t("★★公開の前にもサイト監査を通せる★★（後から気づいても世に出ている）",
      run_site_audit() == [])
    # ★★実機で見つけた壊れ方★★（2026-07-31・レビューでは出なかった）
    #   公開の最終確認は、自分が「公開中」の目印を持っている最中に回る。
    #   項目33（公開が途中で終わっている）を外していなかったので、
    #   **書けた記事を毎回自分で取り消していた**＝1機種も公開できなかった。
    t("★★最終確認は、自分が置いた目印を理由に取り消さない★★"
      "（1機種も公開できなくなっていた・実機で判明）",
      "ignore_in_progress=True" in inspect.getsource(_publish))
    # ★監査は別のプロセスで動く★ 本物の目印でないと再現できない。
    #   （モジュールの中で差し替えても、監査は本物のファイルを見る）
    if unfinished():
        t("　いま公開が途中なので、目印の試験は飛ばします", True)
    else:
        try:
            mark_start("zzz_audit33", {"name": "試験"}, {})
            _strict = run_site_audit()
            _loose = run_site_audit(ignore_in_progress=True)
        finally:
            mark_done()
        t("★★目印があるとき、外さなければちゃんと引っかかる★★"
          "（push の関所はここで残骸を止める）",
          any("33_" in x for x in _strict))
        t("★★外したときだけ、それを理由に止めない★★"
          "（公開の最終確認は目印を持っている最中に回る）",
          not any("33_" in x for x in _loose))
    # ★監査そのものが壊れたら「合格」にしない★（2026-08-01・Codex23回目を再現して直した）
    #   起動失敗・構文エラーは❌を出さずに非0で終わり、以前は空リスト＝合格だった。
    _real_run = subprocess.run

    def _crash_run(cmd, **k):
        if any("audit_site.py" in str(c) for c in cmd):
            class _R:
                returncode = 1
                stdout = "Traceback: ImportError"
                stderr = "boom"
            return _R()
        return _real_run(cmd, **k)

    def _fake_audit(stdout_text):
        def _fk(cmd, **k):
            if any("audit_site.py" in str(c) for c in cmd):
                class _R:
                    returncode = 1
                    stdout = stdout_text
                    stderr = "boom"
                return _R()
            return _real_run(cmd, **k)
        subprocess.run = _fk
        try:
            return run_site_audit()
        finally:
            subprocess.run = _real_run

    _full = {f"{i}_試験": [] for i in range(1, 34)}
    t("★★監査が異常終了したら合格にしない★★"
      "（構文エラー等はJSONを出さずに終わり、素通りしていた・Codex23回目）",
      any("異常終了" in x for x in _fake_audit("Traceback: ImportError")))
    _cut = json.dumps({**_full, "31_Codexへの未報告": ["x"]},
                      ensure_ascii=False)[:80]
    t("★★途中まで出力して落ちた監査も合格にしない★★"
      "（❌を1行出した後に落ちると素通りしていた・Codex24回目）",
      any("異常終了" in x for x in _fake_audit(_cut)))
    _lack = {k: v for k, v in _full.items() if not k.startswith("32_")}
    t("　項目が欠けたJSONも合格にしない（途中終了の別の形）",
      any("異常終了" in x for x in _fake_audit(json.dumps(_lack, ensure_ascii=False))))
    t("　除外対象（Codex未報告）だけの非0は、いままでどおり通す",
      _fake_audit(json.dumps({**_full, "31_Codexへの未報告": ["x"]},
                             ensure_ascii=False)) == [])
    t("　普通のNGはちゃんと出る",
      any("22_" in x for x in _fake_audit(
          json.dumps({**_full, "22_機種重複検知": ["だぶり"]}, ensure_ascii=False))))
    t("★★同じ入力なら毎回同じ物ができる★★（2回目に差分が出ない・Codexの助言）",
      build_hubs() == build_hubs())
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

    # ★★書き込みのどこで失敗しても、中途半端な状態を残さない★★
    #   （2026-07-31・Codexが最も勧めた「障害注入」）
    #   各書き込み地点をわざと失敗させ、
    #   毎回「完全に元のまま」に戻ることを確かめる。
    import shutil as _sh
    import tempfile as _tf4
    _dir4 = _tf4.mkdtemp(prefix="uchi_fault_")
    # ★試験が追跡ファイルの中身（改行コードを含む）を変えないようにする★
    #   巻き戻しは中身を戻すが、書き直す以上どうしても改行が揃ってしまう。
    #   元のバイト列を控え、試験の最後にそのまま書き戻す。
    _bytes4 = {}
    for _rel4 in list(HUB_FILES) + ["assets/data/machines.json"]:
        _f4 = os.path.join(BASE, _rel4)
        with open(_f4, "rb") as _fh4:
            _bytes4[_f4] = _fh4.read()
    # ★障害注入は本番の目印を触らない★（試験の残骸で監査が赤くなるため）
    _real_ip = IN_PROGRESS
    globals()["IN_PROGRESS"] = os.path.join(_dir4, "in_progress.json")
    _real = {"write_atomic": write_atomic, "build_hubs": build_hubs,
             "check_served": check_served, "run_site_audit": run_site_audit,
             "check_after": check_after, "MACHINES": MACHINES}
    try:
        def _snapshot():
            """公開に関わるファイルの指紋（元のままか確かめる用）。"""
            out = {}
            for rel in list(HUB_FILES) + ["assets/data/machines.json"]:
                full = os.path.join(BASE, rel)
                with open(full, encoding="utf-8") as f:
                    # ★改行コードの違いは「戻っていない」と数えない★
                    #   巻き戻しは書き直すので改行がそろう。中身が同じなら戻っている。
                    out[rel] = _sha(f.read().replace(chr(13) + chr(10), chr(10)))
            return out

        _slug4 = "zzz_fault_test"
        _mat4 = {"adopted": {}, "need_third": {}, "thin": {}}
        _before4 = _snapshot()

        def _try_with(name, breaker):
            globals()[name] = breaker
            try:
                publish_from_material(_slug4, "障害注入確認機", "bellco",
                                      f"https://m.example/products/slot/{_slug4}/",
                                      "2026-09", _mat4, apply_it=True)
            except BaseException:                       # noqa: BLE001
                pass
            finally:
                globals()[name] = _real[name]
            ok = (_snapshot() == _before4
                  and not os.path.isdir(os.path.join(BASE, "machines", _slug4))
                  and not os.path.isfile(os.path.join(
                      BASE, "assets", "data", "machine-details", f"{_slug4}.json")))
            # 後片付け（失敗しても残っていたら消す）
            _sh.rmtree(os.path.join(BASE, "machines", _slug4), ignore_errors=True)
            dp4 = os.path.join(BASE, "assets", "data", "machine-details",
                               f"{_slug4}.json")
            if os.path.isfile(dp4):
                os.remove(dp4)
            return ok

        def _boom(*_a, **_k):
            raise RuntimeError("わざと失敗させました")

        def _interrupt(*_a, **_k):
            raise KeyboardInterrupt()

        t("★★早見表を作る所で失敗しても、完全に元のまま★★",
          _try_with("build_hubs", _boom))
        t("★★配信の確認で失敗しても、完全に元のまま★★",
          _try_with("check_served", _boom))
        t("★★最後の監査で失敗しても、完全に元のまま★★",
          _try_with("run_site_audit", lambda: ["わざとNG"]))
        t("★★最終確認で失敗しても、完全に元のまま★★",
          _try_with("check_after", lambda *a, **k: ["わざとNG"]))
        t("★★Ctrl+C（中断）でも、完全に元のまま★★",
          _try_with("build_hubs", _interrupt))
        # ★置き換えた直後に中断される狭い窓★（Codex指摘・実際に再現した）
        _real_replace = os.replace
        for _nth in (1, 2, 3, 4, 5):
            _cnt = {"i": 0}

            def _replace_then_stop(src, dst, _n=_nth, _c=_cnt):
                _real_replace(src, dst)
                _c["i"] += 1
                if _c["i"] == _n:
                    raise KeyboardInterrupt()

            os.replace = _replace_then_stop
            try:
                _ok = _try_with("check_served", _real["check_served"])
            finally:
                os.replace = _real_replace
            t(f"★★{_nth}回目の置き換え直後に中断されても元のまま★★",
              _ok)
        # ★復旧が途中で落ちても、もう一度走らせて完走できるか★
        #   （2026-07-31・Codex12回目「各中断点から再開できるか」）
        #   「ページだけ消えた」状態を作ってから復旧を走らせる。
        _s5 = "zzz_resume_test"
        globals()["check_after"] = lambda *a, **k: ["わざとNG"]
        try:
            publish_from_material(_s5, "再開確認機ZZZ", "bellco",
                                  f"https://m.example/products/slot/{_s5}/",
                                  "2026-09", {"adopted": {}, "need_third": {},
                                              "thin": {}}, apply_it=True)
        except BaseException:                # noqa: BLE001
            pass
        finally:
            globals()["check_after"] = _real["check_after"]
        _pg5 = os.path.join(BASE, "machines", _s5, "index.html")
        _made5 = os.path.isfile(_pg5)
        if _made5:
            os.remove(_pg5)                  # ★復旧の途中で落ちた状態★
        _r5 = recover(apply_it=True)
        _left5 = (os.path.isdir(os.path.join(BASE, "machines", _s5))
                  or os.path.isfile(os.path.join(
                      BASE, "assets", "data", "machine-details", f"{_s5}.json"))
                  or any(m.get("slug") == _s5 for m in _sj.read_rows(MACHINES)))
        t("★★復旧が途中で落ちても、もう一度走らせれば完走する★★",
          (not _made5) or (not _left5 and not _r5["problems"]))
        _sh.rmtree(os.path.join(BASE, "machines", _s5), ignore_errors=True)
        if os.path.exists(IN_PROGRESS):
            os.remove(IN_PROGRESS)
        t("　中途半端な一時ファイルを残さない",
          not [x for x in os.listdir(BASE) if ".tmp." in x or ".new." in x])
    finally:
        for k, v in _real.items():
            globals()[k] = v
        globals()["IN_PROGRESS"] = _real_ip
        for _f4, _b4 in _bytes4.items():        # ★元のバイト列に戻す★
            with open(_f4, "rb") as _fh4:
                if _fh4.read() != _b4:
                    with open(_f4, "wb") as _fh5:
                        _fh5.write(_b4)
        _sh.rmtree(_dir4, ignore_errors=True)

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
    ap.add_argument("--recover", action="store_true",
                    help="途中で終わった公開を処理前へ戻す")
    ap.add_argument("--material", help="採用済みの材料（JSONファイル）")
    ap.add_argument("--name", help="メーカー公式の正式名称")
    ap.add_argument("--maker", help="メーカーID")
    ap.add_argument("--official-url", dest="official_url", help="公式ページURL")
    ap.add_argument("--release", default="", help="登場年月 YYYY-MM")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if args.recover:
        r = recover(apply_it=args.apply)
        for x in r["todo"]:
            print("  " + x)
        for x in r["restored"]:
            print("  戻しました: " + x)
        for x in r["problems"]:
            print("  ✗ " + x[:160])
        if not args.apply and not r["problems"]:
            print("★確認だけです。実際に戻すには --recover --apply★")
        return 1 if r["problems"] else 0
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
    # ★外部の材料JSONからは公開（--apply）できない★（2026-08-03・Codex58回目）
    #   ファイルの中身は「2出典で確認済み」の再検証を通らないので、
    #   誤ったJSONや手打ちの値をそのまま「出典2件で一致」として
    #   記事化できてしまう。下見（--applyなし）だけ許し、
    #   公開は材料収集から検証込みで行う add_machine_run.py --apply 経由に限る。
    if args.apply:
        print("★外部の材料JSONからの公開（--apply）はできません★")
        print("  出典の再検証を通らない値を記事化できてしまうため、"
              "このコマンドは下見（--applyなし）専用です。")
        print("  公開は python scripts/add_machine_run.py --apply を使ってください。")
        return 1
    material = _sj.read_json(args.material, expect=dict)
    res = publish_from_material(args.slug, args.name, args.maker,
                                args.official_url, args.release or "",
                                material, apply_it=False)
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

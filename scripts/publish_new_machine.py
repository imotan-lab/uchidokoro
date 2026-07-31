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
_SLUG_OK = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
# 空白の並び（バックスラッシュを直接書かない：制御文字に化ける事故が続いたため）
_WS = "[ " + chr(9) + chr(13) + chr(10) + "]*"


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


def _head(html: str) -> str:
    """<head> の中だけを取り出す。★コメントは外す★"""
    m = re.search("(?is)<head[^>]*>(.*?)</head" + _WS + ">", html or "")
    body = m.group(1) if m else ""
    return re.sub("(?s)<!--.*?-->", " ", body)


def _visible_body(html: str) -> str:
    """読者に見える本文だけ。★コメント・script・非表示は外す★

    ★2026-07-31・Codex指摘を再現して作った★
      以前は本文まるごとの文字列検索だったので、
      `<!-- 先行記事 -->` と書いてあるだけで合格していた。
    """
    m = re.search("(?is)<body[^>]*>(.*?)</body" + _WS + ">", html or "")
    body = m.group(1) if m else (html or "")
    body = re.sub("(?s)<!--.*?-->", " ", body)
    for tag in ("script", "style", "template", "noscript"):
        body = re.sub("(?is)<" + tag + "[^>]*>.*?</" + tag + _WS + ">", " ", body)
    # 隠されている要素は「表示されている」と見なさない
    #   hidden 属性 / aria-hidden="true" / display:none / visibility:hidden
    for pat in ("[ ]hidden[ >]", 'aria-hidden="true"',
                "display[ ]*:[ ]*none", "visibility[ ]*:[ ]*hidden"):
        body = re.sub("(?is)<([a-z]+)[^>]*" + pat + "[^>]*>.*?</" + chr(92) + "1"
                      + _WS + ">", " ", body)
    return re.sub("(?s)<[^>]+>", " ", body)


def _meta_content(tag: str) -> set:
    """metaタグの content= の中身を、区切りでほどいて返す。"""
    m = re.search('(?is)content="([^"]*)"', tag or "")
    if not m:
        return set()
    return {x.strip().lower() for x in re.split("[,; ]+", m.group(1)) if x.strip()}


def check_page(slug: str, html: str) -> list:
    """作ったページそのものを確かめる。★テンプレート任せにしない★

    ★2026-07-31・Codexの指摘を再現して2回直した★
      1回目: 本文まるごとの文字列検索だったので、
             HTMLコメントに noindex と書いてあるだけで合格していた。
      2回目: head の中は見るようにしたが、タグ全体に "noindex" が
             含まれるかで見ていたため、
             `<meta name="robots" content="index" data-note="noindex">`
             が合格していた（実際に再現）。content の中身で見る。
    """
    ng = []
    head = _head(html)
    robots = re.findall('(?is)<meta[^>]+name="robots"[^>]*>', head)
    if len(robots) != 1:
        ng.append(f"head の robots 指定が {len(robots)} 個です（1個であるべきです）")
    else:
        vals = _meta_content(robots[0])
        if "noindex" not in vals:
            ng.append(f"robots が noindex ではありません（{sorted(vals)}）")
        if "index" in vals:
            ng.append("robots に index と noindex が両方あります")
    bases = re.findall('(?is)<base[^>]+href="/"[^>]*>', head)
    if len(bases) != 1:
        ng.append(f'head の <base href="/"> が {len(bases)} 個です'
                  "（1個でないとロゴ・ナビが404になります）")
    canon = re.findall('(?is)<link[^>]+rel="canonical"[^>]+href="([^"]+)"', head)
    want = f"https://uchidokoro.com/machines/{slug}/"
    if canon != [want]:
        ng.append(f"canonical が {canon!r} です（{want!r} が1個であるべきです）")
    if "style=" in html:
        ng.append("インラインstyleが入っています")
    # ★先行記事だと読者に分かる表示があるか★（noindexは非公開化ではない）
    if "先行記事" not in _visible_body(html):
        ng.append("先行記事であることが読者に見える形で書かれていません")
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


def check_detail(slug: str, detail: dict) -> list:
    """★受け取った記事データそのものを確かめる★（2026-07-31・Codex指摘）

    `build_detail` が正しくても、この関数は任意の記事データを受け取れる。
    直接呼び出し・試験用の呼び出し・将来のつなぎ間違いが別の入口になるので、
    **境界でもう一度、形と型まで確かめる**。
    """
    ng = []
    if not isinstance(detail, dict):
        return ["記事データが辞書ではありません"]
    if detail.get("slug") != slug:
        ng.append(f"記事データの slug が {detail.get('slug')!r} です（{slug!r} のはず）")
    stray = sorted(set(detail) - _DETAIL_KEYS)
    if stray:
        ng.append(f"記事データに知らない項目があります: {stray}")
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
        for tb in (sec.get("tables") or []):
            if not isinstance(tb, dict):
                ng.append("表が辞書ではありません")
                continue
            tbad = sorted(set(tb) - _TABLE_KEYS)
            if tbad:
                ng.append(f"表に知らない項目があります: {tbad}")
            rows = tb.get("rows")
            if not (isinstance(rows, list)
                    and all(isinstance(r, list) and all(_is_text(c) for c in r)
                            for r in rows)):
                ng.append("表の中身が文字の並びではありません")
    for key in ("factTable", "summaryBoxes", "evTable"):
        val = detail.get(key)
        if val is None:
            continue
        if not isinstance(val, list):
            ng.append(f"{key} が配列ではありません")
    blob = json.dumps(detail, ensure_ascii=False)
    for word in _FORBIDDEN:
        if chr(34) + word + chr(34) in blob:
            ng.append(f"採用しなかったものの置き場（{word}）が記事データに残っています")
    return ng


def check_machine(slug: str, machine: dict) -> list:
    """★機種データそのものを確かめる★（2026-07-31・Codex指摘2）

    以前は slug と status と publish_state しか見ていなかった。
    知らない項目が混ざれば、そこに書いた文字がページへ出る道になる。
    """
    ng = []
    if not isinstance(machine, dict):
        return ["機種データが辞書ではありません"]
    stray = sorted(set(machine) - _MACHINE_KEYS)
    if stray:
        ng.append(f"機種データに知らない項目があります: {stray}")
    for key in ("name", "info", "strategy"):
        if key in machine and not _is_text(machine[key]):
            ng.append(f"{key} が文字ではありません")
    if not isinstance(machine.get("aliases", []), list):
        ng.append("aliases が配列ではありません")
    seo = machine.get("seo")
    if seo is not None and not (isinstance(seo, dict)
                                and set(seo) <= {"title", "description"}):
        ng.append("seo に知らない項目があります")
    ident = machine.get("identity")
    if ident is not None and not isinstance(ident, dict):
        ng.append("identity が辞書ではありません")
    # ★狙い目は当サイトの判断なので、この経路では書かせない★
    if machine.get("strategy"):
        ng.append("先行記事に狙い目を書くことはできません（strategy は空のはず）")
    return ng


def allowed_paths(slug: str) -> set:
    """★この経路が変えてよいファイル★（これ以外が変わっていたら止める）"""
    return {
        f"machines/{slug}/index.html",
        f"assets/data/machine-details/{slug}.json",
        "assets/data/machines.json",
    }


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
        if "noindex" not in body:
            ng.append("配信されたHTMLに noindex がありません")
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


def render(slug: str, machine: dict, detail: dict) -> str:
    """既存ページと同じ描き方で1枚だけ作る。"""
    with open(os.path.join(BASE, "machine.html"), encoding="utf-8") as f:
        template = _bmp.prepare_template(f.read())
    reasons = _bmp.extract_pochipochi_reasons(template)
    return _bmp.render_page(template, machine, detail, reasons)


def publish(slug: str, machine: dict, detail: dict, apply_it: bool = False) -> dict:
    """新台1件を公開する。★ページを先に置き、最後に一覧へ足す★"""
    out = {"slug": slug, "problems": [], "wrote": [], "html_bytes": 0}
    rows = _sj.read_rows(MACHINES)
    out["problems"] += check_before(slug, machine, rows)
    out["problems"] += check_detail(slug, detail)
    out["problems"] += check_machine(slug, machine)
    if out["problems"]:
        return out
    html = render(slug, machine, detail)
    out["html_bytes"] = len(html)
    out["problems"] += check_page(slug, html)
    out["problems"] += check_only_allowed_values(slug, machine, detail, html)
    if out["problems"] or not apply_it:
        return out

    before_pages = _existing_pages()
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
        for kind, q in reversed(made):
            try:
                if kind == "file" and os.path.isfile(q):
                    os.remove(q)
                elif kind == "dir" and os.path.isdir(q):
                    os.rmdir(q)
            except OSError:
                pass

    try:
        # ① 記事データとページを置く（★この時点では一覧から辿れない★）
        #    "x" で開く＝既にあれば作らずに例外。存在確認との隙間も無くす。
        with open(dp, "x", encoding="utf-8", newline=chr(10)) as f:
            made.append(("file", dp))
            json.dump(detail, f, ensure_ascii=False, indent=1)
            f.write(chr(10))
        d = os.path.dirname(page)
        if not os.path.isdir(d):
            os.makedirs(d)
            made.append(("dir", d))
        with open(page, "x", encoding="utf-8", newline=chr(10)) as f:
            made.append(("file", page))
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
    except Exception as e:                # noqa: BLE001
        _cleanup()
        raise PublishError(f"一覧に足せませんでした（作ったものは消しました）: {e}")

    # ④ 一覧に足したあとの最終確認（ここで出たら人が直す＝台帳へ）
    out["problems"] += check_after(slug, before_pages, rows[:-1])
    return out


# ---------------------------------------------------------------- selftest

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
            "</head><body>⚠ 先行記事（解析待ち）</body></html>")
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
    t("★★先行記事だと読者に分かる表示が無ければ公開しない★★"
      "（noindexは非公開化ではない）",
      any("先行記事" in x for x in
          check_page("zzz_test", good.replace("⚠ 先行記事（解析待ち）", "ふつうの記事"))))

    t("　数値のかたまりを取り出せる（全角もそろえる）",
      _numbers("約97.3%と１２００Ｇ") == {"97.3%", "1200"})

    t("★★robots は content の中身で見る★★"
      "（data-note=\"noindex\" で合格していた・実際に再現）",
      any("robots" in x for x in check_page(
          "zzz_test",
          good.replace('content="noindex,follow"',
                       'content="index" data-note="noindex"'))))
    t("★★先行記事の表示はコメントでは認めない★★（読者に見えないため）",
      any("先行記事" in x for x in check_page(
          "zzz_test", good.replace("⚠ 先行記事（解析待ち）",
                                   "<!-- 先行記事 -->ふつうの記事"))))
    t("　scriptの中に書いてあるだけでも認めない",
      any("先行記事" in x for x in check_page(
          "zzz_test", good.replace("⚠ 先行記事（解析待ち）",
                                   "<script>var x='先行記事';</script>本文"))))

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
    for _hide in ('aria-hidden="true"', "hidden ",
                  'class="x" style="display:none"'):
        t(f"　{_hide[:18]} で隠された文字は「見える」と扱わない",
          "先行記事" not in _visible_body(
              "<body><div " + _hide + ">先行記事</div>ふつうの本文</body>"))

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
    t("★変えてよいのは3つだけ★",
      allowed_paths("zzz") == {"machines/zzz/index.html",
                               "assets/data/machine-details/zzz.json",
                               "assets/data/machines.json"})
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
    ap.add_argument("--machine", help="machines.json に足す1件（JSONファイル）")
    ap.add_argument("--detail", help="記事データ（JSONファイル）")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.slug:
        ap.print_help()
        return 0
    # ★実際に公開する経路を持たせる★（2026-07-31・Codex指摘）
    #   以前はここが「説明を表示して終わり」だったので、
    #   `--apply` を付けても何も起きなかった。
    #   **何もしないのに成功したように見える**のが一番こわい。
    if not (args.machine and args.detail):
        print("★機種データと記事データのファイルが要ります★")
        print("  先に add_machine_run.py が作ったものを、")
        print("  --machine <machine.json> --detail <detail.json> で渡してください。")
        print("  （ふだんは add_machine_run.py --apply が中で呼びます）")
        return 1
    machine = _sj.read_json(args.machine, expect=dict)
    detail = _sj.read_json(args.detail, expect=dict)
    res = publish(args.slug, machine, detail, apply_it=args.apply)
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

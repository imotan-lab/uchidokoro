"""★★大手サイトにあって、この記事に無い項目を足す唯一の場所★★（2026-11-10・運営者の指示）

運営者の言葉：
  > 無い項目ももしも見つかったら付け足すんだよ？
  > 大手サイトと比較して無いものはこっちのサイトにも書きたい　遜色ないようにするのが目的

★なぜ要るか★＝既存記事を直す道具（decide_now）は「直す・消す」しかできず、確定値の数字を差し替えるのが限界だった。
大手のページにある項目（例：有利区間・打ち方の要点・ボーナス確率の表・上位ATの入り方）が記事に無くても、足す道が無かった。

★決めるのは2AI★＝どの項目が足りないか・何を書くか・どの数字がどの言葉に付くか。
★機械が見るのは形と「数字を作っていないか」だけ★：
  ①判断者の組（claude と codex／代役の2人）・理由15字以上
  ②記事の指紋（source_sha256）が判断したときと同じ
  ③足す文・セルに、タグ・見えない字・サイト名・「どこから採ったか」の言い回しが無い（derived_sections.text_problems と同じ検査）
  ④★足す数字は、名指しした確定値の記録（confirmed_values・独立2出典）か、いまの記事と機種行に同じ単位で在るものだけ★
  ⑤足す欄の名前が記事に無い（あれば add_rows / add_line）・決まった欄の名前でも在る位置へ並べる
  ⑥表の欄（type=table）は本文を持たない・行の列数は見出しと同じ
  ⑦★新台経路の機種は、欄を足す（add_section）だけ★＝毎朝の育成が材料から記事を作り直すので、
    足した欄は育成が持ち越す（grow_machine.carry_added_sections・この控えに載った欄の名前だけ）

決定ファイル（Writeツールで書く）：
  {"slug": "...", "by": ["claude","codex"], "why": "...", "source_sha256": "<--gather で出た指紋>",
   "compared": [{"url": "...", "missing": ["比べて足りなかった項目", ...]}],
   "additions": [
     {"op": "add_section", "title": "有利区間", "body": ["…です。"], "fields": ["confirmed の項目名", ...]},
     {"op": "add_section", "title": "ボーナス確率", "table": {"headers": [...], "rows": [[...]], "note": ""}, "fields": [...]},
     {"op": "add_rows", "section": "基本スペック", "table": 0, "rows": [[...]], "fields": [...]},
     {"op": "add_line", "section": "ゲーム性", "text": "…です。", "fields": [...]}
   ]}

使い方：
  python scripts/add_items.py --gather <slug>                 いまの記事の欄・指紋・確定値の項目名を出す（比べる材料）
  python scripts/add_items.py --apply <決定ファイル> [--dry-run]
  python scripts/add_items.py --selftest
控え＝Documents/uchidokoro/add_items_basis.json（足した欄・行・判断者・理由・比べたページ）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import io
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import local_paths as _lp            # noqa: E402

BASE = os.path.dirname(HERE)
DETAILS = os.path.join(BASE, "assets", "data", "machine-details")
MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
BASIS = os.path.join(_lp.DOCS, "add_items_basis.json")
# ★足した欄の名前の一覧はリポジトリの中★＝新台の記事の契約（build_new_article.expected_titles）と育成の持ち越しが読む
ADDED = os.path.join(BASE, "assets", "data", "added-sections.json")
WHY_MIN = 15
OPS = ("add_section", "add_rows", "add_line")
TITLE_MAX = 20
TEXT_MAX = 400
CELL_MAX = 80
_SLUG = re.compile(r"^[a-z0-9_]{1,60}$")
RUMOR_TITLE = "噂・未確定情報"
_IN_SELFTEST = False


def _log(msg: str) -> None:
    if _IN_SELFTEST:
        return
    try:
        os.makedirs(_lp.LOGS, exist_ok=True)
        p = os.path.join(_lp.LOGS, "add_items_%s.log" % _dt.date.today().isoformat())
        with io.open(p, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (_dt.datetime.now().strftime("%H:%M:%S"), msg))
    except OSError:
        pass


def article_sha(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8").replace(b"\r\n", b"\n")).hexdigest()


def _judges_ok(by) -> bool:
    import confirmed_values as _cv
    try:
        return _cv.judges_pair(list(by or [])) is not None
    except Exception:                                            # noqa: BLE001
        return False


def _text_ng(t, where: str, lo: int, hi: int) -> list:
    import derived_sections as _ds
    if not isinstance(t, str) or not lo <= len(t.strip()) <= hi:
        return [f"{where} は{lo}〜{hi}字で書きます: {str(t)[:40]!r}"]
    out = []
    for p in _ds.text_problems(t):
        if "字で書きます" in p:              # ★長さは段落の決まりではなく、ここの決まりで見る★
            continue
        out.append(f"{where}: {p}")
    return out


def _numbers_ok(texts: list, pool: tuple, rec_toks=frozenset()) -> list:
    """★足す数字は、いまの記事・機種行に同じ単位で在るか、名指しした確定値の照合済みの値に在るものだけ★"""
    import decide_now as _dn
    pairs, toks = pool
    bad = []
    for t in texts:
        for nu in _dn.numbers_with_unit(t):
            if nu in pairs or (nu[1] == "" and nu[0] in toks) or nu[0] in rec_toks or _canon(nu[0]) in rec_toks:
                continue
            bad.append(f"{nu[0]}{nu[1]}")
    return sorted(set(bad))


def record_strings(field: str, value) -> list:
    """★確定値の記録のうち、出典の逐語と照合した表示値だけ★（2026-11-10・Codexの指摘2回）

    ★自分で値を拾わない★＝確定値の道具が「引用と照合すべき表示値」として返すもの（confirmed_values.check_shape）
    をそのまま使う（覚え書き・照合しない鍵・範囲の組み立てで、照合していない数字が入るのを防ぐ）。
    形が壊れていれば例外（呼ぶ側が「記録がありません」として断る）。
    """
    import confirmed_values as _cv
    return [str(x) for x in _cv.check_shape(field, value)]


def _record_pool(slug: str, fields: list, confirmed: dict) -> tuple:
    """名指しした確定値の記録の、照合した表示値の中の数（単位は問わない＝どの言葉に付くかは2AIが決める）"""
    import decide_now as _dn
    toks, missing = set(), []
    for f in fields:
        rec = confirmed.get(f)
        if not isinstance(rec, dict) or "value" not in rec:
            missing.append(f)
            continue
        try:
            strings = record_strings(f, rec["value"])
        except Exception:                                        # noqa: BLE001
            missing.append(f)
            continue
        for st in strings:
            for tok, _unit in _dn.numbers_with_unit(st):
                toks.add(tok)
                toks.add(_canon(tok))          # ★31.0 と 31 は同じ数★（Codexの指摘）
        # ★設定ごとの項目だけ、設定の番号（1〜6）も使える★（「設定1は…」と書くため・Codexの指摘＝ほかの項目の数字の鍵は使わない）
        import confirmed_values as _cv
        import spec_lookup as _sp
        if str((_sp.FIELDS.get(_cv.base_field(f)) or {}).get("kind") or "").startswith("per_setting")                 and isinstance(rec["value"], dict):
            toks |= {str(k) for k in rec["value"] if str(k) in ("1", "2", "3", "4", "5", "6")}
    return toks, missing


def _canon(tok: str) -> str:
    """数の書き方をそろえる（31.0 → 31・31.50 → 31.5）。数でなければそのまま"""
    try:
        v = float(tok)
    except ValueError:
        return tok
    return str(int(v)) if v.is_integer() else ("%s" % v)


def _place(secs: list, title: str) -> int:
    """★足す欄の位置★＝決まった並び（normalize_sections.IDEAL_ORDER）にあればそこ、無ければ噂の欄の前（無ければ末尾）"""
    import normalize_sections as _ns
    order = list(_ns.IDEAL_ORDER)
    titles = [s.get("title") if isinstance(s, dict) else "" for s in secs]
    if title in order:
        rank = order.index(title)
        for i, ti in enumerate(titles):
            if ti in order and order.index(ti) > rank:
                return i
        return len(secs)
    if RUMOR_TITLE in titles:
        return titles.index(RUMOR_TITLE)
    return len(secs)


def plan(detail: dict, row: dict, dec: dict, confirmed: dict) -> dict:
    """★書く前に全部確かめる★（1つでも外れたら何も書かない）。戻り値の sections が書いたあとの姿"""
    import page_decision as _pd
    import derived_sections as _ds
    p = []
    if not _judges_ok(dec.get("by")):
        p.append("判断者の組がそろっていません（claude と codex、または代役の2人）")
    if len(str(dec.get("why") or "").strip()) < WHY_MIN:
        p.append(f"理由（why）は{WHY_MIN}字以上で書きます")
    adds = dec.get("additions")
    if not isinstance(adds, list) or not adds:
        return {"problems": p + ["additions は1件以上の並びで書きます"]}
    auto = _pd.is_auto(row or {})
    secs = json.loads(json.dumps(detail.get("sections") or []))
    base_pool = _ds.number_pool(detail, row or {})
    done = []
    for i, a in enumerate(adds):
        w = f"additions[{i}]"
        if not isinstance(a, dict) or a.get("op") not in OPS:
            p.append(f"{w}: op は {'/'.join(OPS)} のどれかです")
            continue
        if auto and a["op"] != "add_section":
            p.append(f"{w}: 新台経路の機種は欄を足す（add_section）だけです（育成が記事を作り直すため）")
            continue
        fields = a.get("fields") or []
        if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
            p.append(f"{w}: fields は確定値の項目名の並びです")
            continue
        rec_toks, miss = _record_pool(row.get("slug") or detail.get("slug") or "", fields, confirmed)
        if miss:
            p.append(f"{w}: 確定値の記録がありません・形が違います: {'・'.join(miss)}（先に confirmed_values.py --record）")
        pool = base_pool
        titles = [s.get("title") for s in secs if isinstance(s, dict)]
        texts = []
        if a["op"] == "add_section":
            title = a.get("title")
            p += _text_ng(title, f"{w} 欄の名前", 2, TITLE_MAX)
            texts.append(title if isinstance(title, str) else "")   # ★見出しの数字も作らない★（Codexの指摘）
            if title in titles:
                p.append(f"{w}: 「{title}」はもう記事にあります（add_rows か add_line で足す）")
            if title == RUMOR_TITLE or (isinstance(title, str) and title.startswith("噂")):
                p.append(f"{w}: 噂の欄は rumor_verdict で書きます")
            has_body, has_tbl = bool(a.get("body")), bool(a.get("table"))
            if has_body == has_tbl:
                p.append(f"{w}: 欄は本文（body）か表（table）のどちらか一方で書きます")
                continue
            if has_body:
                body = a["body"]
                if not isinstance(body, list) or not 1 <= len(body) <= 6:
                    p.append(f"{w}: body は1〜6段落の並びです")
                    continue
                for j, b in enumerate(body):
                    p += _text_ng(b, f"{w} body[{j}]", 10, TEXT_MAX)
                texts += [b for b in body if isinstance(b, str)]
                new = {"title": title, "body": list(body)}
            else:
                tb = a["table"]
                tp, tt = _table_ng(tb, w)
                p += tp
                texts += tt
                new = {"title": title, "type": "table",
                       "tables": [{k: tb[k] for k in ("headers", "rows", "note", "label") if k in tb}]}
            if not p:
                secs.insert(_place(secs, title), new)
                done.append(f"欄「{title}」")
        else:
            sec = next((s for s in secs if isinstance(s, dict) and s.get("title") == a.get("section")), None)
            if sec is None:
                p.append(f"{w}: 欄「{a.get('section')}」が記事にありません")
                continue
            if a["op"] == "add_line":
                if sec.get("type") in ("table", "settei", "rumor"):
                    p.append(f"{w}: 表・設定示唆・噂の欄に本文は足せません（表は add_rows）")
                    continue
                t = a.get("text")
                p += _text_ng(t, f"{w} text", 10, TEXT_MAX)
                if isinstance(t, str) and t in (sec.get("body") or []):
                    p.append(f"{w}: 同じ文がもうあります")
                texts.append(t if isinstance(t, str) else "")
                if not p:
                    sec.setdefault("body", []).append(t)
                    done.append(f"「{sec['title']}」に1文")
            else:
                ti = a.get("table", 0)
                tbls = sec.get("tables") or []
                if not isinstance(ti, int) or isinstance(ti, bool) or not 0 <= ti < len(tbls):
                    p.append(f"{w}: 欄「{sec.get('title')}」に表 {ti} がありません")
                    continue
                tbl = tbls[ti]
                rows = a.get("rows")
                hdr = tbl.get("headers") or []
                if not isinstance(rows, list) or not rows:
                    p.append(f"{w}: rows は1行以上です")
                    continue
                for r_i, r in enumerate(rows):
                    if not isinstance(r, list) or len(r) != len(hdr):
                        p.append(f"{w}: rows[{r_i}] の列数が見出し（{len(hdr)}列）と違います")
                        continue
                    for c_i, c in enumerate(r):
                        if not isinstance(c, str):
                            p.append(f"{w}: rows[{r_i}][{c_i}] は文字で書きます（設定示唆の札は足せません）")
                            continue
                        p += _text_ng(c, f"{w} rows[{r_i}][{c_i}]", 1, CELL_MAX)
                        texts.append(c)
                    if r in (tbl.get("rows") or []):
                        p.append(f"{w}: rows[{r_i}] と同じ行がもうあります")
                if not p:
                    tbl.setdefault("rows", []).extend(rows)
                    done.append(f"「{sec['title']}」の表に{len(rows)}行")
        made = _numbers_ok(texts, pool, rec_toks)
        if made:
            p.append(f"{w}: 確定値にも記事にも無い数字があります: {'・'.join(made)}"
                     "（独立2出典で確かめて confirmed_values.py --record し、fields に名指しする）")
    if p:
        return {"problems": p}
    return {"problems": [], "sections": secs, "done": done}


def _table_ng(tb, w: str) -> tuple:
    p, texts = [], []
    if not isinstance(tb, dict):
        return [f"{w}: table は辞書です"], []
    extra = set(tb) - {"headers", "rows", "note", "label"}
    if extra:
        p.append(f"{w}: table に知らない鍵があります: {sorted(extra)}")
    hdr, rows = tb.get("headers"), tb.get("rows")
    if not isinstance(hdr, list) or not 2 <= len(hdr) <= 8:
        return p + [f"{w}: headers は2〜8列です"], []
    for h in hdr:
        p += _text_ng(h, f"{w} 見出し", 1, 30)
        texts.append(h if isinstance(h, str) else "")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 40:
        return p + [f"{w}: rows は1〜40行です"], texts
    for r_i, r in enumerate(rows):
        if not isinstance(r, list) or len(r) != len(hdr):
            p.append(f"{w}: rows[{r_i}] の列数が見出しと違います")
            continue
        for c_i, c in enumerate(r):
            p += _text_ng(c, f"{w} rows[{r_i}][{c_i}]", 1, CELL_MAX)
            texts.append(c if isinstance(c, str) else "")
    for k in ("note", "label"):
        if tb.get(k):
            p += _text_ng(tb[k], f"{w} {k}", 2, TEXT_MAX)
            texts.append(tb[k])
    return p, texts


# ---------------------------------------------------------------- 書く
def _read_json_or(path: str, default, expect):
    if not os.path.exists(path):
        return default
    import safe_json as _sj
    return _sj.read_json(path, expect=expect)


def _write_atomic(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def _indent_of(raw: str) -> int:
    lines = raw.split("\n")
    if len(lines) > 1:
        m = re.match(r"^( +)\S", lines[1])
        if m:
            return len(m.group(1))
    return 1


def rebuild_page(slug: str, auto: bool) -> bool:
    if auto:
        import build_machine_pages as _b
        return (_b._rebuild_auto(slug) or 0) == 0
    r = subprocess.run([sys.executable, os.path.join(HERE, "build_machine_pages.py"), "--legacy", "--slug", slug],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode == 0


def _row(slug: str) -> dict:
    import safe_json as _sj
    return next((m for m in _sj.read_rows(MACHINES) if m.get("slug") == slug), {}) or {}


def apply_decision(path: str, apply_it: bool) -> dict:
    import safe_json as _sj
    import confirmed_values as _cv
    import page_decision as _pd
    try:
        dec = _sj.read_json(path, expect=dict)
    except Exception as e:                                       # noqa: BLE001
        return {"problems": [f"決定ファイルを読めません: {type(e).__name__}: {e}"]}
    slug = dec.get("slug")
    if not isinstance(slug, str) or not _SLUG.match(slug):
        return {"problems": ["slug は英小文字・数字・下線だけです"]}
    p = os.path.join(DETAILS, slug + ".json")
    if not os.path.isfile(p):
        return {"slug": slug, "problems": ["記事データがありません"]}
    row = _row(slug)
    if not row:
        return {"slug": slug, "problems": ["機種一覧にありません"]}
    raw = io.open(p, encoding="utf-8", newline="").read()
    now_sha = article_sha(raw)
    if dec.get("source_sha256") != now_sha:
        return {"slug": slug, "problems": [
            "判断したときの記事の指紋（source_sha256）が無いか、いまの記事と違います"
            f"（いま {now_sha[:12]}…）。--gather で読み直して決め直してください"]}
    try:
        confirmed = _cv.for_slug(slug)
    except Exception as e:                                       # noqa: BLE001
        return {"slug": slug, "problems": [f"確定値の控えを読めないので書きません: {type(e).__name__}: {e}"]}
    detail = json.loads(raw)
    got = plan(detail, row, dec, confirmed)
    got["slug"] = slug
    if got["problems"]:
        _log(f"{slug}: 断りました: " + " ／ ".join(got["problems"])[:600])
        return got
    if not apply_it:
        return got
    try:
        basis = _read_json_or(BASIS, [], list)
    except Exception as e:                                       # noqa: BLE001
        got["problems"] = [f"控えを読めないので書きません: {type(e).__name__}: {e}"]
        return got
    after = dict(detail)
    after["sections"] = got["sections"]
    new_raw = json.dumps(after, ensure_ascii=False, indent=_indent_of(raw))
    if raw.endswith("\n"):
        new_raw += "\n"
    if "\r\n" in raw:
        new_raw = new_raw.replace("\r\n", "\n").replace("\n", "\r\n")
    basis.append({"at": _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "slug": slug,
                  "by": dec["by"], "why": dec["why"], "compared": dec.get("compared") or [],
                  "additions": dec["additions"], "old_sha256": now_sha, "new_sha256": article_sha(new_raw)})
    _write_atomic(BASIS, json.dumps(basis, ensure_ascii=False, indent=1) + "\n")
    new_titles = [a["title"] for a in dec["additions"] if a.get("op") == "add_section"]
    added_raw = io.open(ADDED, encoding="utf-8", newline="").read() if os.path.isfile(ADDED) else None
    if new_titles:
        added = json.loads(added_raw) if added_raw else {}
        added[slug] = sorted(set(added.get(slug, [])) | set(new_titles))
        _write_atomic(ADDED, json.dumps(dict(sorted(added.items())), ensure_ascii=False, indent=1) + "\n")
    _write_atomic(p, new_raw)
    try:
        ok = rebuild_page(slug, _pd.is_auto(row))
    except Exception as e:                                       # noqa: BLE001
        ok = False
        got.setdefault("notes", []).append(f"作り直しで例外: {type(e).__name__}: {e}")
    if not ok:
        if io.open(p, encoding="utf-8", newline="").read() == new_raw:
            _write_atomic(p, raw)
            if new_titles:
                if added_raw is None:
                    os.remove(ADDED)
                else:
                    _write_atomic(ADDED, added_raw)
            try:
                rebuild_page(slug, _pd.is_auto(row))
            except Exception:                                    # noqa: BLE001
                pass
        got["problems"] = ["ページを作り直せなかったので、記事を元に戻しました"]
        _log(f"{slug}: {got['problems'][0]}")
        return got
    got["written"] = True
    _log(f"{slug}: 足しました: {'・'.join(got['done'])}（判断者 {','.join(dec['by'])}）")
    return got


def added_titles(slug: str, basis=None) -> set:
    """★この道具で足した欄の名前★（新台の育成が持ち越すのはこれだけ）"""
    basis = _read_json_or(BASIS, [], list) if basis is None else basis
    out = set()
    for b in basis:
        if isinstance(b, dict) and b.get("slug") == slug:
            for a in b.get("additions") or []:
                if isinstance(a, dict) and a.get("op") == "add_section" and isinstance(a.get("title"), str):
                    out.add(a["title"])
    return out


def gather(slug: str) -> dict:
    import confirmed_values as _cv
    p = os.path.join(DETAILS, slug + ".json")
    raw = io.open(p, encoding="utf-8", newline="").read()
    d = json.loads(raw)
    try:
        fields = sorted(_cv.for_slug(slug))
    except Exception as e:                                       # noqa: BLE001
        fields = [f"（読めません: {type(e).__name__}）"]
    return {"slug": slug, "source_sha256": article_sha(raw),
            "sections": [s.get("title") for s in d.get("sections") or [] if isinstance(s, dict)],
            "confirmed_fields": fields,
            "how_to_decide": ("大手サイト（なな徹・スロパチクエスト・パチ7・DMMぱちタウン）のこの機種のページを2AIがそれぞれ読み、"
                              "そちらにあってこの記事に無い項目を挙げる。足す数字は独立2出典で確かめて "
                              "confirmed_values.py --record してから fields に名指しする。文は自分の言葉で、です・ます。"
                              "サイト名・どこから採ったかは書かない。打ち方をすすめる言い方はしない")}


# ---------------------------------------------------------------- 自己試験
def selftest() -> int:
    global _IN_SELFTEST
    _IN_SELFTEST = True
    import tempfile
    ok = [0, 0]

    def t(name, cond):
        ok[0 if cond else 1] += 1
        print(("✅ " if cond else "❌ ") + name)

    row = {"slug": "zzz_g", "name": "試験機G", "checker": {"normal": {"good": 600, "ceiling": 999}}}
    detail = {"slug": "zzz_g", "sections": [
        {"title": "天井・恩恵", "body": ["通常時の天井は999Gです。"]},
        {"title": "基本スペック", "type": "table", "tables": [{"headers": ["項目", "内容"], "rows": [["純増", "約3.0枚"]]}]},
        {"title": "ゲーム性", "body": ["AT中心のゲーム性です。"]},
        {"title": "噂・未確定情報", "type": "rumor", "body": ["噂です。"]}]}
    conf = {"ceiling#yuuri": {"value": {"kind": "GAME", "amount": "3000", "unit": "G", "benefit": "有利区間の終わり",
                                        "_memo": "採用しない別条件の9999G"}},
            "ceiling#at": {"value": {"kind": "GAME", "amount": "777", "unit": "G", "benefit": "AT 5000枚",
                                     "low": "9999", "high": "10000"}},
            "payout_rate": {"value": {"1": "97.7%", "6": "114.9%"}},
            "games_per_50": {"value": {"games": 31.0, "note": "9999G"}}}
    conf["ceiling#six"] = {"value": {"kind": "GAME", "amount": "777", "unit": "G", "benefit": "AT", "6": "9999G"}}
    base = {"slug": "zzz_g", "by": ["claude", "codex"], "why": "大手2サイトと比べて有利区間の欄が無かった"}

    def dec(*adds):
        return dict(base, additions=list(adds))
    r = plan(detail, row, dec({"op": "add_section", "title": "有利区間",
                               "body": ["有利区間は最大3000Gまでです。"], "fields": ["ceiling#yuuri"]}), conf)
    t("★★大手にあって記事に無い欄を、確定値の数字で足す（噂の欄の前に並ぶ）★★",
      not r["problems"] and [s["title"] for s in r["sections"]][-2:] == ["有利区間", "噂・未確定情報"])
    r = plan(detail, row, dec({"op": "add_section", "title": "有利区間",
                               "body": ["有利区間は最大3500Gまでです。"], "fields": ["ceiling#yuuri"]}), conf)
    t("★★確定値にも記事にも無い数字は断る（数字を作らない）★★", any("無い数字" in x for x in r["problems"]))
    r = plan(detail, row, dec({"op": "add_section", "title": "有利区間",
                               "body": ["有利区間は最大3000Gまでです。"]}), conf)
    t("　確定値を名指ししなければ、その数字は使えない", any("無い数字" in x for x in r["problems"]))
    r = plan(detail, row, dec({"op": "add_section", "title": "有利区間",
                               "body": ["有利区間は最大9999Gまでです。"], "fields": ["ceiling#yuuri"]}), conf)
    t("★★確定値の覚え書き（_ で始まる鍵）の数字は使えない★★（Codexの指摘）", any("無い数字" in x for x in r["problems"]))
    r = plan(detail, row, dec({"op": "add_section", "title": "AT間の天井",
                               "body": ["AT間の天井は777Gです。"], "fields": ["ceiling#at"]}), conf)
    t("★★数と単位が別の鍵の確定値（amount＋unit）から 777G を使える★★（Codexの指摘）", not r["problems"])
    r = plan(detail, row, dec({"op": "add_section", "title": "AT間の天井",
                               "body": ["AT間の天井は9999Gです。"], "fields": ["ceiling#at"]}), conf)
    t("★★照合しない鍵（範囲 low・high）の数字は、決まった形の項目では使えない★★（Codexの再指摘）",
      any("無い数字" in x or "形が違います" in x for x in r["problems"]))
    r = plan(detail, row, dec({"op": "add_section", "title": "50枚あたり",
                               "body": ["50枚あたり9999Gです。"], "fields": ["games_per_50"]}), conf)
    t("　spec の項目でも、照合しない鍵（note）の数字は使えない（Codexの再指摘）",
      any("無い数字" in x or "形が違います" in x for x in r["problems"]))
    r = plan(detail, row, dec({"op": "add_section", "title": "回転数の目安",
                               "body": ["コイン持ちは約31Gです。"], "fields": ["games_per_50"]}), conf)
    t("　照合した値（31.0）は 31G と書いても使える（数の書き方をそろえる・Codexの指摘）", not r["problems"])
    r = plan(detail, row, dec({"op": "add_section", "title": "継続の目安",
                               "body": ["継続ゲーム数は6Gです。"], "fields": ["ceiling#six"]}), conf)
    t("★設定別でない項目では、数字の鍵（\"6\"）を設定の番号として使わない★（Codexの指摘）",
      any("無い数字" in x or "形が違います" in x for x in r["problems"]))
    r = plan(detail, row, dec({"op": "add_section", "title": "AT間の天井",
                               "body": ["天井到達でAT 5000枚です。"], "fields": ["ceiling#at"]}), conf)
    t("　引用と照合していない鍵（benefit）の数字は使えない", any("無い数字" in x for x in r["problems"]))
    r = plan(detail, row, dec({"op": "add_section", "title": "機械割",
                               "body": ["設定1は97.7%、設定6は114.9%です。"], "fields": ["payout_rate"]}), conf)
    t("　設定ごとの値（機械割）も照合した値として使える", not r["problems"])
    r = plan(detail, row, dec({"op": "add_section", "title": "天井9999G", "body": ["見出しに数字を書く文です。"]}), conf)
    t("★★欄の見出しの数字も作らない★★（Codexの指摘）", any("無い数字" in x for x in r["problems"]))
    r = plan(detail, row, dec({"op": "add_section", "title": "天井の補足",
                               "body": ["天井の999Gは通常時のゲーム数です。"]}), conf)
    t("★記事にもう在る数字（999G）は、確定値を名指ししなくても使える★", not r["problems"])
    r = plan(detail, row, dec({"op": "add_section", "title": "ゲーム性", "body": ["重ねて書く文です。"]}), conf)
    t("　記事に在る欄の名前では足さない（add_rows か add_line）", any("もう記事にあります" in x for x in r["problems"]))
    r = plan(detail, row, dec({"op": "add_section", "title": "有利区間",
                               "body": ["スロパチクエストでは3000Gです。"], "fields": ["ceiling#yuuri"]}), conf)
    t("　サイト名は書けない", any("サイト名" in x for x in r["problems"]))
    r = plan(detail, row, dec({"op": "add_rows", "section": "基本スペック", "table": 0,
                               "rows": [["有利区間", "最大3000G"]], "fields": ["ceiling#yuuri"]}), conf)
    t("★★既にある表に行を足す★★", not r["problems"]
      and r["sections"][1]["tables"][0]["rows"][-1] == ["有利区間", "最大3000G"])
    r = plan(detail, row, dec({"op": "add_rows", "section": "基本スペック", "table": 0,
                               "rows": [["有利区間"]], "fields": ["ceiling#yuuri"]}), conf)
    t("　行の列数が見出しと違えば断る", any("列数" in x for x in r["problems"]))
    r = plan(detail, row, dec({"op": "add_line", "section": "ゲーム性",
                               "text": "有利区間は最大3000Gまでです。", "fields": ["ceiling#yuuri"]}), conf)
    t("★★既にある欄に1文足す★★", not r["problems"] and r["sections"][2]["body"][-1].startswith("有利区間"))
    r = plan(detail, row, dec({"op": "add_line", "section": "噂・未確定情報", "text": "噂の文を足します。"}), conf)
    t("　噂の欄には足せない（rumor_verdict）", bool(r["problems"]))
    r = plan(detail, dict(row, publication_policy="page-decision/v2"),
             dec({"op": "add_line", "section": "ゲーム性", "text": "AT中心で純増は約3.0枚です。"}), conf)
    t("★新台経路の機種は欄を足すだけ（行・文は育成が作り直すので断る）★", any("新台経路" in x for x in r["problems"]))
    r = plan(detail, row, dict(base, by=["claude"], additions=[
        {"op": "add_section", "title": "有利区間", "body": ["有利区間は最大3000Gまでです。"], "fields": ["ceiling#yuuri"]}]), conf)
    t("　判断者が1人なら断る", any("判断者" in x for x in r["problems"]))
    r = plan(detail, row, dec({"op": "add_section", "title": "ボーナス確率",
                               "table": {"headers": ["設定", "合算"], "rows": [["1", "1/150"]]}}), conf)
    t("　表の欄も数字を作らない（1/150 は確定値にも記事にも無い）", any("無い数字" in x for x in r["problems"]))
    t("　記録の無い確定値を名指ししたら断る",
      any("確定値の記録がありません" in x for x in plan(detail, row, dec(
          {"op": "add_section", "title": "有利区間", "body": ["有利区間は最大3000Gまでです。"], "fields": ["nai"]}),
          conf)["problems"]))
    # ★書く：指紋・作り直しに失敗したら戻す・控えに残す★
    g = globals()
    keep = {k: g[k] for k in ("DETAILS", "BASIS", "ADDED", "rebuild_page", "_row")}
    import confirmed_values as _cvx
    keep_fs = _cvx.for_slug
    tmp = tempfile.mkdtemp(prefix="additems_")
    try:
        g["DETAILS"] = tmp
        g["BASIS"] = os.path.join(tmp, "basis.json")
        g["ADDED"] = os.path.join(tmp, "added.json")
        g["_row"] = lambda s: row
        _cvx.for_slug = lambda s, data=None: conf
        dp = os.path.join(tmp, "zzz_g.json")
        raw0 = json.dumps(detail, ensure_ascii=False, indent=1) + "\n"
        io.open(dp, "w", encoding="utf-8", newline="").write(raw0)
        fp = os.path.join(tmp, "dec.json")
        good = dec({"op": "add_section", "title": "有利区間", "body": ["有利区間は最大3000Gまでです。"],
                    "fields": ["ceiling#yuuri"]})
        io.open(fp, "w", encoding="utf-8").write(json.dumps(dict(good, source_sha256="x" * 64), ensure_ascii=False))
        t("★記事の指紋が違えば書かない★", "指紋" in " ".join(apply_decision(fp, True)["problems"]))
        io.open(fp, "w", encoding="utf-8").write(json.dumps(dict(good, source_sha256=article_sha(raw0)),
                                                            ensure_ascii=False))
        g["rebuild_page"] = lambda s, a: False
        r = apply_decision(fp, True)
        t("★★ページを作り直せなければ記事を元に戻す（足した欄の一覧も戻す）★★",
          r["problems"] and io.open(dp, encoding="utf-8", newline="").read() == raw0
          and not os.path.exists(g["ADDED"]))
        g["rebuild_page"] = lambda s, a: True
        r = apply_decision(fp, True)
        now = json.loads(io.open(dp, encoding="utf-8").read())
        t("★★書けたら記事に欄が入り、控えに残る（育成が持ち越す欄の名前になる）★★",
          r.get("written") and "有利区間" in [s["title"] for s in now["sections"]]
          and added_titles("zzz_g", json.loads(io.open(g["BASIS"], encoding="utf-8").read())) >= {"有利区間"}
          and json.loads(io.open(g["ADDED"], encoding="utf-8").read()) == {"zzz_g": ["有利区間"]})
    finally:
        g.update(keep)
        _cvx.for_slug = keep_fs
    print(f"\n{ok[0]}/{sum(ok)} 合格")
    return 0 if ok[1] == 0 else 1


def main() -> int:
    for _st in (sys.stdout, sys.stderr):
        try:
            _st.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                        # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description="大手にあって記事に無い項目を足す")
    ap.add_argument("--gather")
    ap.add_argument("--apply")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.gather:
        print(json.dumps(gather(a.gather), ensure_ascii=False, indent=1))
        return 0
    if a.apply:
        r = apply_decision(a.apply, not a.dry_run)
        if r.get("problems"):
            print("★書きません★")
            for x in r["problems"]:
                print("  - " + x)
            return 1
        print(("書きました: " if r.get("written") else "（空打ち）通ります: ") + "・".join(r.get("done") or []))
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())

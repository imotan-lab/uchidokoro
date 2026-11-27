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
import html as _html_mod
import io
import json
import os
import re
import subprocess
import sys
import unicodedata as _ud

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
OPS = ("add_section", "add_rows", "add_line", "add_fact")
# ★設定示唆の札に書ける数字は「設定1〜6」だけ★＝その形の後ろに数字・単位・記号が続くものは数字として残す
#   ★後ろに続いてよい語を決める★（「設定6割」のような数量を番号として通さない＝Codexの7回目）
_SETTEI_N = r"設定[1-6](?=以上|以下|濃厚|否定|示唆|確定|期待|優遇|が|の|を|は|と|・|、|。|）|\)|$)"
# ★漢数字・大字（数字の判定 numerals.is_numeral_char が拾わないものも含む）★＝札では使えない（Codexの6回目）
_KANJI_DIGITS = set("〇一二三四五六七八九十百千万零壱弐貳参參肆伍陸漆柒捌玖拾佰仟萬")
# ★基本情報表に1行足す（昔の機種だけ）★（2026-11-10・洗い出し4回目＝導入日・メーカーの行が92機種で無かった）
#   導入日は fields に "legacy_release" と書くと、昔の機種の導入日の控え（DMMの機種ページの値）の数字が使える
LEGACY_RELEASE_FIELD = "legacy_release"
FACT_LABEL_MAX = 20
FACT_VALUE_MAX = 60
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


ITEM_FIELD = "item_values"
# ★表示値の前後にあってはいけない字★＝数の続き（桁・小数点・区切り・分数）と符号（−500枚 の 500枚 を通さない）
_ITEM_BEFORE = r"0-9０-９.．,，/／+＋\-−－‐‑–—―▲△*"   # ★* も＝太字の内側から照合を始めさせない（Codex review317 の1）★
_ITEM_AFTER = "0-9０-９.．,，/／%％‰‱"
# ★数で終わる表示値（「500」）の後ろに単位を足させない★（Codex review312 の2＝500 を根拠に「500G」が通った）
_ITEM_UNIT_AFTER = "A-Za-zＡ-Ｚａ-ｚ枚回円個秒倍周十百千万億兆゠-ヺー-ヿｦ-ﾟ"   # ★中点（・）は区切りなので外す★（Codex review313 の5）


def item_strings(fields: list, confirmed: dict) -> list:
    """★名指しした item_values の表示値（出典の書き方のまま）★＝長いものから（短い表示値が長いものの一部を先に消さないように）"""
    import confirmed_values as _cv
    out = set()
    for f in fields or []:
        if _cv.base_field(f) != ITEM_FIELD:
            continue
        rec = (confirmed or {}).get(f)
        if not isinstance(rec, dict):
            continue
        try:
            out |= {s0 for s0 in record_strings(f, rec.get("value")) if s0}
        except Exception:                                        # noqa: BLE001
            continue
    return sorted(out, key=lambda x: (-len(x), x))


def mask_items(text: str, strs) -> str:
    """★文の中の item_values の表示値を、まるごとの形で在るときだけ伏せる★（2026-11-27・Codex review309 の1）

    ★数をばらして照合しない★＝1/8192 と 約42% から 42/8192 や 8192% を作れないように、
    表示値がそのまま（前後に数の続きも符号も無く）在るときだけ、その中の数を「出どころがある」とみなす。
    """
    if not isinstance(text, str):
        return text                       # ★文字でないものは形の検査が断る★（ここで例外にしない＝Codex review314 の4）
    if not strs:
        return text                       # ★伏せる表示値が無ければ文はそのまま★（記事の側の数の読み方とそろえる＝Codex review319 の2）
    # ★空白の連なりは1つに詰めてから見る★（「-  500枚」の空白2つで符号を飛び越えさせない＝Codex review311 の1）
    #   ★改行しない空白（U+00A0）なども空白として詰める★（Codex review318 の2）
    text = re.sub(r"[\s　]+", " ", text)
    for s0 in strs or ():
        # ★表示値の空白も同じ形にそろえる★（Codex review312 の6＝「1　/　8192」を出典どおりに書いても通らなかった）
        s1 = re.sub(r"[\s　]+", " ", s0)
        after = _ITEM_AFTER + (_ITEM_UNIT_AFTER if s1[-1:].isdigit() else "")
        # ★前の字と表示値のあいだに空白が1つある形（「- 500枚」「1 / 8192」の 8192）も伏せない★（Codex review310 の1・review313 の2）
        # ★表示値の前後の太字の印（**）は読み飛ばして境目を見る★（「**500**万枚」で桁を変えさせない＝Codex review315 の2。
        #   文ぜんたいからは外さない＝記事の側の数の読み方とそろえる・Codex review316 の4）。数で終わる値の後ろの「まいる」も単位（review316 の3）
        unit_tail = "|まいる" if s1[-1:].isdigit() else ""
        text = re.sub(r"(?<![%s])(?<![%s] )(?:\*\*)?%s(?!(?:\*\*)?\s*(?:[%s]%s))(?:\*\*)?"
                      % (_ITEM_BEFORE, _ITEM_BEFORE, re.escape(s1), after, unit_tail),
                      lambda mo, _s=s1: mo.group(0) if _ranged(_s, mo) else "〓", text)
    return text


def _ranged(s1: str, mo) -> bool:
    """★符号で始まる表示値の前が「数＋単位」なら、範囲の区切りなので伏せない★（100枚-500枚＝Codex review317 の2）"""
    import confirmed_values as _cv
    # ★全角の数字と、先に伏せた表示値（〓）も「数」とみなす★（Codex review318 の1＝１００枚-500枚・100.0枚-500枚 がすり抜けた）
    _rng = _cv.ITEM_RANGE_BEFORE.replace("[0-9]", "[0-9０-９〓]", 1)
    return s1[:1] in "+＋-−－‐‑–—―▲△" and bool(re.search(_rng, mo.string[:mo.start()]))


def items_used(text: str, strs) -> list:
    """★その文の照合で実際に伏せた表示値★（長いものから順に・Codex review312 の4＝使っていない短い表示値まで控えていた）"""
    used, cur = [], mask_items(text, [])
    for s0 in strs or ():
        nxt = mask_items(cur, [s0])
        if nxt != cur:
            used.append(s0)
        cur = nxt
    return used


def _numbers_ok(texts: list, pool: tuple, rec_toks=frozenset(), items=()) -> list:
    """★足す数字は、いまの記事・機種行に同じ単位で在るか、名指しした確定値の照合済みの値に在るものだけ★

    ★item_values の数は、表示値がまるごと文に在るときだけ★（items＝item_strings の戻り値）
    """
    import decide_now as _dn
    pairs, toks = pool
    bad = []
    for t in [mask_items(t0, items) for t0 in texts]:
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
        if f == LEGACY_RELEASE_FIELD:
            try:
                import legacy_release as _lr
                rel = _lr.release_for(slug)
            except Exception:                                    # noqa: BLE001
                rel = ""
            if not rel:
                missing.append(f)
                continue
            for tok, _unit in _dn.numbers_with_unit(rel):
                toks.add(tok)
                toks.add(_canon(tok))
            continue
        rec = confirmed.get(f)
        if not isinstance(rec, dict) or "value" not in rec:
            missing.append(f)
            continue
        try:
            strings = record_strings(f, rec["value"])
        except Exception:                                        # noqa: BLE001
            missing.append(f)
            continue
        import confirmed_values as _cv0
        if _cv0.base_field(f) == ITEM_FIELD:
            continue                       # ★数をばらして使わない★（item_strings と mask_items が見る）
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
    import decimal
    try:
        v = decimal.Decimal(tok)          # ★十進数のまま比べる★（float だと 31.000000000000001 が 31 になる・Codexの指摘）
    except decimal.InvalidOperation:
        return tok
    if not v.is_finite():
        return tok
    t = format(v.normalize(), "f")
    return t if "." not in t else t.rstrip("0").rstrip(".")


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
    done, new_facts, fix_facts = [], [], {}
    for i, a in enumerate(adds):
        w = f"additions[{i}]"
        if not isinstance(a, dict) or a.get("op") not in OPS:
            p.append(f"{w}: op は {'/'.join(OPS)} のどれかです")
            continue
        if a["op"] == "add_fact":
            if auto:
                p.append(f"{w}: 基本情報表に足せるのは昔の機種だけです（新台は育成が作り直す）")
                continue
            rw = a.get("row")
            if not isinstance(rw, list) or len(rw) != 2:
                p.append(f"{w}: row は [項目, 内容] の2つです")
                continue
            p += _text_ng(rw[0], f"{w} 項目", 1, FACT_LABEL_MAX)
            p += _text_ng(rw[1], f"{w} 内容", 1, FACT_VALUE_MAX)
            ft = detail.get("factTable")
            if ft is not None and not isinstance(ft, list):
                p.append(f"{w}: 基本情報表の形が違います")
                continue
            # ★この道具で前に足した行（控えに同じ行がある）なら、同じ項目名で直せる★（2026-11-10・代役の指摘＝
            #   導入日の控えが変わっても、足した行を直す道が無かった）。それ以外の既存の行は今までどおり断る
            _same = [x for x in (ft or []) if isinstance(x, list) and x and x[0] == rw[0]]
            _slug0 = row.get("slug") or detail.get("slug") or ""
            _mine = False
            if _same and rw[0] not in fix_facts:
                try:
                    _bas0 = _read_json_or(BASIS, [], list)
                except Exception:                                # noqa: BLE001
                    _bas0 = []
                _mine = all(any(isinstance(b, dict) and b.get("slug") == _slug0
                                and any(isinstance(a2, dict) and a2.get("op") == "add_fact" and a2.get("row") == x
                                        for a2 in (b.get("additions") or []))
                                for b in _bas0) for x in _same)
            if (_same and not _mine) or rw[0] in fix_facts \
                    or any(isinstance(x, list) and x and x[0] == rw[0] for x in new_facts):
                p.append(f"{w}: 基本情報表に「{rw[0]}」の行がもうあります（直すなら decide_now）")
            rec_toks, miss = _record_pool(_slug0, a.get("fields") or [], confirmed)
            if miss:
                p.append(f"{w}: 確定値の記録がありません・形が違います: {'・'.join(miss)}")
            # ★直すときは、古い行の数字を「記事にある」に数えない★
            _pool_f = _ds.number_pool(dict(detail, factTable=[x for x in (ft or []) if x not in _same]), row or {}) \
                if _mine else base_pool
            made = _numbers_ok([x for x in rw if isinstance(x, str)], _pool_f, rec_toks,
                               item_strings(a.get("fields") or [], confirmed))
            if made:
                p.append(f"{w}: 確定値にも記事にも無い数字があります: {'・'.join(made)}")
            if not p:
                if _mine:
                    fix_facts[rw[0]] = list(rw)
                    done.append(f"基本情報表の「{rw[0]}」を直した")
                else:
                    new_facts.append(list(rw))
                    done.append(f"基本情報表に「{rw[0]}」")
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
            # ★この道具で前に足した欄なら、同じ名前で置き換えられる★（2026-11-10・Codexの指摘＝新台は行・文を足せないので、
            #   確定値が直っても足した欄を直す道が無かった）。それ以外の既存の欄は今までどおり断る
            _replace = False
            if title in titles:
                try:
                    import build_new_article as _bna
                    _added_all = set(_bna.added_section_titles(row.get("slug") or detail.get("slug") or ""))
                    _replace = title in _added_all
                except Exception:                                # noqa: BLE001
                    _replace = False
                if not _replace:
                    p.append(f"{w}: 「{title}」はもう記事にあります（add_rows か add_line で足す）")
                else:
                    # ★★置き換えるときは、古い欄の数字を「記事にある」に数えない★★（2026-11-10・代役の指摘＝
                    #   数えると、確定値が直っても古い数字のまま「直した」ことにできた）
                    #   ★この道具で足したほかの欄の数字も数えない★（代役の指摘＝同じ古い確定値から足した別の欄があると抜けた）
                    pool = _ds.number_pool(dict(detail, sections=[
                        s for s in (detail.get("sections") or [])
                        if not (isinstance(s, dict) and s.get("title") in (_added_all | {title}))]),
                        row or {})
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
                if _replace:
                    secs[[s.get("title") if isinstance(s, dict) else None for s in secs].index(title)] = new
                    done.append(f"欄「{title}」を直した")
                else:
                    secs.insert(_place(secs, title), new)
                    done.append(f"欄「{title}」")
                # ★足した数字の出どころを控える★（確定値から来た数字は、持ち越すとき今の確定値と照らす）
                import decide_now as _dn0
                # ★★名指しした確定値に在る数字は「確定値から」と控える（記事にもあっても）★★（2026-11-10・代役の指摘＝
                #   記事の別の欄に同じ数字があると article と控え、確定値が直っても持ち越しで照らされなかった）
                a["_number_sources"] = sorted({
                    f"{nu[0]}{nu[1]}|" + ("fields" if (nu[0] in rec_toks or _canon(nu[0]) in rec_toks)
                                          else "article")
                    for t0 in texts for nu in _dn0.numbers_with_unit(t0)}
                    # ★★どの item_values の表示値で照合したかも控える★★（Codex review311 の3＝確定値を訂正・取り消しても、
                    #   別の欄に同じ数があると古い欄が持ち越されていた）。持ち越すとき、その表示値がまだ控えにあるかを見る
                    # ★どの項目の表示値かも控える★（Codex review315 の3＝別の項目の同じ値が、訂正を隠していた）
                    | {f"{s0}|item:{f}" for t0 in texts for s0 in items_used(t0, item_strings(fields, confirmed))
                       for f in fields if s0 in item_strings([f], confirmed)})
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
            elif sec.get("type") == "settei" and "tables" not in sec:
                # ★★設定示唆の札（rows＝要素 trigger と示唆 hint）に行を足す★★（2026-11-26）
                #   札は trigger と hint の2つの文字だけ（強さの印など知らない鍵は断る）・同じ札は足さない・
                #   数字は表と同じく記事か名指しした確定値に在るものだけ（下の _numbers_ok が見る）
                #   ★描画と同じ形の欄だけ★（tables の鍵が在る欄はブラウザが表として描くので、ここでは足さない＝Codexの指摘）
                import numerals as _nm
                import build_new_article as _bna_v
                if not isinstance(sec.get("rows"), list):
                    p.append(f"{w}: 欄「{sec.get('title')}」の札（rows）が読めません（記事データを確かめてください）")
                    continue
                rows = a.get("rows")
                if not isinstance(rows, list) or not rows:
                    p.append(f"{w}: rows は1行以上です")
                    continue
                for r_i, r in enumerate(rows):
                    if not isinstance(r, dict) or set(r) != {"trigger", "hint"}:
                        p.append(f"{w}: rows[{r_i}] は設定示唆の札なので trigger と hint の2つだけで書きます")
                        continue
                    for k in ("trigger", "hint"):
                        p += _text_ng(r[k], f"{w} rows[{r_i}].{k}", 1, CELL_MAX)
                        # ★出どころの照合には「設定N」を除いた文字を渡す★（設定の番号を「6濃」のような単位つきの数と読ませない＝Codexの7回目）
                        texts.append(re.sub(_SETTEI_N, "設定", r[k]) if isinstance(r[k], str) else "")
                        if isinstance(r[k], str) and not _bna_v.visible_text(r[k], markdown=False).strip():
                            p.append(f"{w}: rows[{r_i}].{k} は読者に見える文字で書きます")
                        # ★文字参照（&#55; など）は書かない★＝ブラウザでは数字に化けて照合をすり抜ける（Codexの2回目の指摘）
                        if isinstance(r[k], str) and _html_mod.unescape(r[k]) != r[k]:
                            p.append(f"{w}: rows[{r_i}].{k} に文字参照（&…;）は書けません")
                        # ★桁区切り（2,300G）は書かない★＝数字が2と300Gに分かれて照合をすり抜ける（Codexの3回目の指摘）
                        if isinstance(r[k], str) and re.search(r"[0-9０-９][,，][0-9０-９]", r[k]):
                            p.append(f"{w}: rows[{r_i}].{k} の数字に桁区切りのカンマは使えません")
                        # ★数字のすぐ前後に空白を置かない・半角の - 以外のマイナス記号を使わない★
                        #   （空白は単位の区切り、全角マイナスは符号と読まれず照合をすり抜ける＝Codexの4回目の指摘）
                        if isinstance(r[k], str) and (re.search(r"[0-9０-９]\s|\s[0-9０-９]", r[k])
                                                      or re.search(r"[－−‐―ー‑–—][0-9０-９]", r[k])):
                            p.append(f"{w}: rows[{r_i}].{k} の数字の前後に空白やマイナス記号を置けません")
                        # ★★札に書ける数字は「設定1〜6」の形だけ★★（Codexの5回目＝分数・小数・符号が共有の数字照合をすり抜ける）
                        #   示唆は「設定6濃厚」「設定4以上濃厚」の形で足りる。確率などの数字は表（add_section）で書く
                        #   ★数としての値を持つ文字は全部数字とみなす★（unicodedata.numeric＝廿・貮など名簿に無い異体字も拾う＝Codexの8回目）
                        if isinstance(r[k], str) and any(_nm.is_numeral_char(ch) or ch in _KANJI_DIGITS
                                                         or _ud.numeric(ch, None) is not None
                                                         for ch in re.sub(_SETTEI_N, "", r[k])):
                            p.append(f"{w}: rows[{r_i}].{k} の札に書ける数字は「設定1〜6」だけです（確率などは表で足す）")
                        # ★数字は半角の算用数字だけ★（漢数字・丸数字は数字の照合をすり抜けるため＝Codexの指摘）
                        if isinstance(r[k], str) and any(_nm.is_numeral_char(ch) and not ("0" <= ch <= "9")
                                                         for ch in r[k]):
                            p.append(f"{w}: rows[{r_i}].{k} の数字は半角の算用数字で書きます（漢数字・丸数字は使えません）")
                    # ★既存の札は辞書のほか配列（[要素, 示唆]）の形もある★＝見える2セルで比べる（Codexの指摘）
                    def _cell_txt(c):
                        return re.sub(r"\s+", " ", str(c.get("text") if isinstance(c, dict) else c)).strip()
                    _have = {(_cell_txt(x.get("trigger")), _cell_txt(x.get("hint"))) if isinstance(x, dict)
                             else tuple(_cell_txt(c) for c in x[:2])
                             for x in (sec.get("rows") or []) if isinstance(x, (dict, list))}
                    if (_cell_txt(r["trigger"]), _cell_txt(r["hint"])) in _have:
                        p.append(f"{w}: rows[{r_i}] と同じ札がもうあります（同じ行がもうあります）")
                    elif r in (sec.get("rows") or []) or (_cell_txt(r["trigger"]), _cell_txt(r["hint"])) in {
                            (_cell_txt(x.get("trigger")), _cell_txt(x.get("hint")))
                            for x in rows[:r_i] if isinstance(x, dict)}:
                        p.append(f"{w}: rows[{r_i}] と同じ行がもうあります")
                if not p:
                    sec.setdefault("rows", []).extend(rows)
                    done.append(f"「{sec['title']}」の札に{len(rows)}行")
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
                        # ★★設定示唆の欄の表（見出しつき）は、示唆のセルを札（text と badge）で書ける★★（2026-11-27・
                        #   朝タスクの後回し＝discup_ur のトロフィーの行が2社にあるのに足せなかった）。強さの印は描画と同じ5つだけ
                        _settei_tbl = sec.get("type") == "settei"
                        if _settei_tbl and isinstance(c, dict):
                            import gates as _gt_b
                            if set(c) != {"text", "badge"} or c.get("badge") not in _gt_b._BADGE_VALUES \
                                    or not isinstance(c.get("text"), str):
                                p.append(f"{w}: rows[{r_i}][{c_i}] の札は text と badge"
                                         f"（{'/'.join(_gt_b._BADGE_VALUES)}）の2つで書きます")
                                continue
                            c = c["text"]
                        if not isinstance(c, str):
                            p.append(f"{w}: rows[{r_i}][{c_i}] は文字で書きます（設定示唆の札は設定示唆の欄の表だけ）")
                            continue
                        p += _text_ng(c, f"{w} rows[{r_i}][{c_i}]", 1, CELL_MAX)
                        # ★設定示唆の欄の表は、数字を半角の算用数字だけで書く★（Codex review309 の3＝丸数字・漢数字は
                        #   数字の照合をすり抜け「設定⑦以上濃厚」「九十九％」が通った。札の行と同じ決まり）
                        import numerals as _nm_t
                        if _settei_tbl and any((_nm_t.is_numeral_char(ch) or ch in _KANJI_DIGITS
                                                or _ud.numeric(ch, None) is not None) and not ("0" <= ch <= "9")
                                               for ch in c):
                            p.append(f"{w}: rows[{r_i}][{c_i}] の数字は半角の算用数字で書きます（漢数字・丸数字は使えません）")
                        # ★半角の - 以外のマイナス記号（－・− など）は書かない★（数字の照合で符号と読まれない＝Codex review310 の4。札の行と同じ）
                        if _settei_tbl and re.search(r"[－−‐―ー‑–—]\s*[0-9０-９]", c):
                            p.append(f"{w}: rows[{r_i}][{c_i}] の数字の前にマイナス記号（－・− など）を置けません")
                        # ★設定示唆の欄の表だけ、「設定N」の番号は数字の照合から外す★（札と同じ決まり・_SETTEI_N）
                        texts.append(re.sub(_SETTEI_N, "設定", c) if _settei_tbl else c)
                    if r in (tbl.get("rows") or []):
                        p.append(f"{w}: rows[{r_i}] と同じ行がもうあります")
                if not p:
                    tbl.setdefault("rows", []).extend(rows)
                    done.append(f"「{sec['title']}」の表に{len(rows)}行")
        made = _numbers_ok(texts, pool, rec_toks, item_strings(fields, confirmed))
        if made:
            p.append(f"{w}: 確定値にも記事にも無い数字があります: {'・'.join(made)}"
                     "（独立2出典で確かめて confirmed_values.py --record し、fields に名指しする）")
    if p:
        return {"problems": p}
    out = {"problems": [], "sections": secs, "done": done}
    if new_facts or fix_facts:
        out["factTable"] = [list(fix_facts.get(x[0], x)) if isinstance(x, list) and x else x
                            for x in json.loads(json.dumps(detail.get("factTable") or []))] + new_facts
    return out


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
    if "factTable" in got:
        after["factTable"] = got["factTable"]
    new_raw = json.dumps(after, ensure_ascii=False, indent=_indent_of(raw))
    if raw.endswith("\n"):
        new_raw += "\n"
    if "\r\n" in raw:
        new_raw = new_raw.replace("\r\n", "\n").replace("\n", "\r\n")
    _entry = {"at": _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "slug": slug,
              "by": dec["by"], "why": dec["why"], "compared": dec.get("compared") or [],
              "additions": dec["additions"], "old_sha256": now_sha, "new_sha256": article_sha(new_raw)}
    basis.append(_entry)
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
        # ★★控えの記録も外す★★（2026-11-10・代役の指摘＝書けなかった欄の出どころが残り、
        #   同じ名前の欄を持ち越すときにそちらが「最後に足したとき」として読まれていた）
        try:
            _b2 = _read_json_or(BASIS, [], list)
            _write_atomic(BASIS, json.dumps([x for x in _b2 if x != _entry], ensure_ascii=False, indent=1) + "\n")
        except Exception as e:                                   # noqa: BLE001
            got.setdefault("notes", []).append(f"控えの記録を外せませんでした: {type(e).__name__}: {e}")
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
                              "confirmed_values.py --record してから fields に名指しする（決まった形の項目が無い数字は "
                              "item_values#<見出し> に、出典の書き方のままの表示値の並び {\"values\": [...]} で控える）。"
                              "文は自分の言葉で、です・ます。"
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
    t("　書き方をそろえても、違う数は同じにしない（31.000000000000001 は 31 ではない）",
      _canon("31.0") == "31" and _canon("31.50") == "31.5" and _canon("31.000000000000001") != "31"
      and _canon("100") == "100")
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
    _dd = dec({"op": "add_section", "title": "有利区間", "body": ["有利区間は最大3000Gまでです。"],
               "fields": ["ceiling#yuuri"]})
    _d3 = dict(detail, sections=list(detail["sections"]) + [{"title": "補足", "body": ["有利区間は3000Gです。"]}])
    r = plan(_d3, row, _dd, conf)
    t("★★名指しした確定値に在る数字は、記事の別の欄にもあっても「確定値から」と控える★★（代役の指摘）",
      not r["problems"] and _dd["additions"][0].get("_number_sources") == ["3000G|fields"])
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
    import legacy_release as _lrx
    _keep_rf = _lrx.release_for
    try:
        _lrx.release_for = lambda s, path=None: "2025-06-02"
        r = plan(dict(detail, factTable=[["機械割", "97.7%"]]), row,
                 dec({"op": "add_fact", "row": ["導入日", "2025年6月2日"], "fields": ["legacy_release"]}), conf)
        t("★★昔の機種の基本情報表に、導入日の行を足す（DMMの導入日の控えの数字）★★",
          not r["problems"] and r["factTable"][-1] == ["導入日", "2025年6月2日"])
        r = plan(dict(detail, factTable=[]), row,
                 dec({"op": "add_fact", "row": ["導入日", "2025年7月2日"], "fields": ["legacy_release"]}), conf)
        t("　控えと違う日付は足せない（数字を作らない）", any("無い数字" in x for x in r["problems"]))
        r = plan(dict(detail, factTable=[["導入日", "2025年6月2日"]]), row,
                 dec({"op": "add_fact", "row": ["導入日", "2025年6月2日"], "fields": ["legacy_release"]}), conf)
        t("　同じ項目の行がもうあれば足さない", any("もうあります" in x for x in r["problems"]))
        _keep_bs = globals()["BASIS"]
        _bt = os.path.join(tempfile.mkdtemp(prefix="additems_b_"), "basis.json")
        try:
            globals()["BASIS"] = _bt
            io.open(_bt, "w", encoding="utf-8").write(json.dumps([{"slug": row["slug"], "additions": [
                {"op": "add_fact", "row": ["導入日", "2024年1月1日"], "fields": ["legacy_release"]}]}],
                ensure_ascii=False))
            r = plan(dict(detail, factTable=[["機械割", "97.7%"], ["導入日", "2024年1月1日"]]), row,
                     dec({"op": "add_fact", "row": ["導入日", "2025年6月2日"], "fields": ["legacy_release"]}), conf)
            r2 = plan(dict(detail, factTable=[["導入日", "2024年1月1日"]]), row,
                      dec({"op": "add_fact", "row": ["導入日", "2024年1月1日"]}), conf)
        finally:
            globals()["BASIS"] = _keep_bs
        t("★★この道具で足した基本情報表の行は、同じ項目名の add_fact で直せる（控えの導入日が変わったとき）★★（代役の指摘）",
          not r["problems"] and r["factTable"] == [["機械割", "97.7%"], ["導入日", "2025年6月2日"]])
        t("　直すとき、古い行の数字は「記事にある」に数えない（確定値を名指ししなければ古い日付で直せない）",
          any("無い数字" in x for x in r2["problems"]))
        r = plan(dict(detail, factTable=[]), row,
                 dec({"op": "add_fact", "row": ["メーカー", "サミー"]}, {"op": "add_fact", "row": ["メーカー", "北電子"]}),
                 conf)
        t("★同じ決定の中で、同じ項目の行を2つ足さない★（Codexの指摘）", any("もうあります" in x for x in r["problems"]))
        r = plan(detail, dict(row, publication_policy="page-decision/v2"),
                 dec({"op": "add_fact", "row": ["メーカー", "サミー"]}), conf)
        t("　新台の基本情報表には足さない（育成が作り直す）", any("昔の機種だけ" in x for x in r["problems"]))
    finally:
        _lrx.release_for = _keep_rf
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
        t("★★作り直せずに戻したら、控えの記録も残さない★★（代役の指摘＝書けなかった欄の出どころが読まれていた）",
          json.loads(io.open(g["BASIS"], encoding="utf-8").read()) == [])
        g["rebuild_page"] = lambda s, a: True
        r = apply_decision(fp, True)
        now = json.loads(io.open(dp, encoding="utf-8").read())
        t("★★書けたら記事に欄が入り、控えに残る（育成が持ち越す欄の名前になる）★★",
          r.get("written") and "有利区間" in [s["title"] for s in now["sections"]]
          and added_titles("zzz_g", json.loads(io.open(g["BASIS"], encoding="utf-8").read())) >= {"有利区間"}
          and json.loads(io.open(g["ADDED"], encoding="utf-8").read()) == {"zzz_g": ["有利区間"]})
        _bas = json.loads(io.open(g["BASIS"], encoding="utf-8").read())
        t("★足した数字の出どころ（確定値から・記事から）を控えに残す★（持ち越すときに今の確定値と照らす）",
          _bas[-1]["additions"][0].get("_number_sources") == ["3000G|fields"])
        import build_new_article as _bna2
        _keep_as = _bna2.ADDED_SECTIONS
        try:
            _bna2.ADDED_SECTIONS = g["ADDED"]
            _now2 = json.loads(io.open(dp, encoding="utf-8").read())
            r = plan(_now2, row, dec({"op": "add_section", "title": "有利区間",
                                      "body": ["有利区間は最大3000Gまでで、終わると通常へ戻ります。"],
                                      "fields": ["ceiling#yuuri"]}), conf)
            r2 = plan(_now2, row, dec({"op": "add_section", "title": "ゲーム性", "body": ["置き換える文です。"]}), conf)
        finally:
            _bna2.ADDED_SECTIONS = _keep_as
        t("★★この道具で足した欄は、同じ名前で直し直せる（新台で確定値が直ったとき）★★（Codexの指摘）",
          not r["problems"] and [s["title"] for s in r["sections"]].count("有利区間") == 1
          and any("終わると通常" in b for s in r["sections"] if s["title"] == "有利区間" for b in s["body"]))
        t("　足していない既存の欄は、同じ名前でも置き換えない", any("もう記事にあります" in x for x in r2["problems"]))
        try:
            _bna2.ADDED_SECTIONS = g["ADDED"]
            r3 = plan(_now2, row, dec({"op": "add_section", "title": "有利区間",
                                       "body": ["有利区間は最大3000Gまでです。"]}), conf)
        finally:
            _bna2.ADDED_SECTIONS = _keep_as
        t("★★置き換えるとき、古い欄にしか無い数字は「記事にある」に数えない★★（代役の指摘＝確定値を名指しせず古い数字のまま直せた）",
          any("無い数字" in x for x in r3["problems"]))
        _now3 = dict(_now2, sections=list(_now2["sections"]) + [{"title": "有利区間の補足", "body": ["最大3000Gです。"]}])
        try:
            _bna2.ADDED_SECTIONS = g["ADDED"]
            io.open(g["ADDED"], "w", encoding="utf-8").write(
                json.dumps({"zzz_g": ["有利区間", "有利区間の補足"]}, ensure_ascii=False))
            r4 = plan(_now3, row, dec({"op": "add_section", "title": "有利区間",
                                       "body": ["有利区間は最大3000Gまでです。"]}), conf)
        finally:
            _bna2.ADDED_SECTIONS = _keep_as
        t("　この道具で足したほかの欄に残る古い数字も「記事にある」に数えない（代役の指摘）",
          any("無い数字" in x for x in r4["problems"]))
    finally:
        g.update(keep)
        _cvx.for_slug = keep_fs
    # ★★設定示唆の札（type=settei の rows＝要素と示唆）にも行を足せる★★（2026-11-26・朝の更新タスク）
    #   直す前は add_rows が表（tables）しか見ず、大手にあって記事に無い札（例＝BT中のムービーの別パターン）を足す道が無かった
    _st = {"slug": "zzz_g", "sections": [
        {"title": "天井・恩恵", "body": ["天井は非搭載です。"]},
        {"title": "設定示唆まとめ", "type": "settei", "rows": [{"trigger": "終了画面：カヲル", "hint": "設定6濃厚"}]}]}
    _rs = plan(_st, row, dec({"op": "add_rows", "section": "設定示唆まとめ",
                              "rows": [{"trigger": "ムービー：リリス", "hint": "高設定示唆（強）"}]}), conf)
    t("★★設定示唆の札にも、要素と示唆の行を足せる★★",
      not _rs["problems"] and _rs["sections"][1]["rows"][-1] == {"trigger": "ムービー：リリス", "hint": "高設定示唆（強）"})
    _rs2 = plan(_st, row, dec({"op": "add_rows", "section": "設定示唆まとめ",
                               "rows": [{"trigger": "ムービー：リリス", "hint": "高設定示唆", "badge": "strong"}]}), conf)
    t("　札の行は trigger と hint の2つだけ（知らない鍵は断る）", bool(_rs2["problems"]))
    _rs3 = plan(_st, row, dec({"op": "add_rows", "section": "設定示唆まとめ",
                               "rows": [{"trigger": "終了画面：カヲル", "hint": "設定6濃厚"}]}), conf)
    t("　同じ札がもうあれば断る", any("同じ行" in x for x in _rs3["problems"]))
    _rs4 = plan(_st, row, dec({"op": "add_rows", "section": "設定示唆まとめ",
                               "rows": [{"trigger": "ムービー：謎", "hint": "設定7以上濃厚"}]}), conf)
    t("　札の文字にも、記事と確定値に無い数字は書けない", any("無い数字" in x for x in _rs4["problems"]))
    _rs5 = plan(_st, row, dec({"op": "add_rows", "section": "設定示唆まとめ",
                               "rows": [{"trigger": "ムービー：謎", "hint": "設定七濃厚"}]}), conf)
    t("　漢数字・丸数字で数字の照合をすり抜けられない（Codexの指摘）", any("算用数字" in x for x in _rs5["problems"]))
    _rs6 = plan(_st, row, dec({"op": "add_rows", "section": "設定示唆まとめ",
                               "rows": [{"trigger": "ムービー：リリス", "hint": "高設定示唆（強）"},
                                        {"trigger": "ムービー：リリス", "hint": "高設定示唆（強）"}]}), conf)
    t("　同じ依頼の中の重複も断る（Codexの指摘）", any("同じ行" in x for x in _rs6["problems"]))
    _rs7 = plan(_st, row, dec({"op": "add_rows", "section": "設定示唆まとめ",
                               "rows": [{"trigger": "ムービー：リリス", "hint": "&nbsp;"}]}), conf)
    t("　読者に見えない文字だけの札は断る（Codexの指摘）", any("見える文字" in x for x in _rs7["problems"]))
    _rs10 = plan(_st, row, dec({"op": "add_rows", "section": "設定示唆まとめ",
                                "rows": [{"trigger": "ムービー：謎", "hint": "設定&#55 濃厚"}]}), conf)
    _st11 = {"slug": "zzz_g", "sections": [{"title": "設定示唆まとめ", "type": "settei",
                                           "rows": [["終了画面：全員集合", "設定2以上濃厚"]]}]}
    _rs11 = plan(_st11, row, dec({"op": "add_rows", "section": "設定示唆まとめ",
                                  "rows": [{"trigger": "終了画面：全員集合", "hint": "設定2以上濃厚"}]}), conf)
    t("　配列の形の既存の札と同じ内容も断る（Codexの2回目の指摘）", any("同じ札" in x for x in _rs11["problems"]))
    _st12 = {"slug": "zzz_g", "sections": [{"title": "設定示唆まとめ", "type": "settei",
                                           "rows": [{"trigger": "終了画面：リオ", "hint": {"text": "奇数設定期待度アップ", "badge": "weak"}}]}]}
    _rs12 = plan(_st12, row, dec({"op": "add_rows", "section": "設定示唆まとめ",
                                  "rows": [{"trigger": "終了画面：リオ", "hint": "奇数設定期待度アップ"}]}), conf)
    t("　示唆セルが text と badge の辞書の既存の札と同じ内容も断る（Codexの3回目の指摘）",
      any("同じ札" in x for x in _rs12["problems"]))
    _rs13 = plan(_st, row, dec({"op": "add_rows", "section": "設定示唆まとめ",
                                "rows": [{"trigger": "ムービー：謎", "hint": "1,6G以内で濃厚"}]}), conf)
    t("　桁区切りのカンマで数字を分けて照合をすり抜けられない（Codexの3回目の指摘）",
      any("桁区切り" in x for x in _rs13["problems"]))
    _rs14 = plan(_st, row, dec({"op": "add_rows", "section": "設定示唆まとめ",
                                "rows": [{"trigger": "ムービー：謎", "hint": "6 回到達で濃厚"}]}), conf)
    _rs15 = plan(_st, row, dec({"op": "add_rows", "section": "設定示唆まとめ",
                                "rows": [{"trigger": "ムービー：謎", "hint": "－6枚で濃厚"}]}), conf)
    t("　数字の後ろの空白・全角マイナスで単位や符号を隠せない（Codexの4回目の指摘）",
      any("空白やマイナス" in x for x in _rs14["problems"]) and any("空白やマイナス" in x for x in _rs15["problems"]))
    _rs16 = plan(_st, row, dec({"op": "add_rows", "section": "設定示唆まとめ",
                                "rows": [{"trigger": "終了画面：カヲル", "hint": "設定6濃厚"}]}), conf)
    _st17 = {"slug": "zzz_g", "sections": [{"title": "設定示唆まとめ", "type": "settei",
                                           "rows": [{"trigger": "終了画面 加賀&赤城", "hint": "高設定示唆"}]}]}
    _rs17 = plan(_st17, row, dec({"op": "add_rows", "section": "設定示唆まとめ",
                                  "rows": [{"trigger": "終了画面  加賀&赤城", "hint": "高設定示唆"}]}), conf)
    _bad = [plan(_st, row, dec({"op": "add_rows", "section": "設定示唆まとめ",
                                "rows": [{"trigger": "ムービー：謎", "hint": h}]}), conf)
            for h in ("1/100で出現", ".6%で濃厚", "﹣6枚で濃厚", "設定16濃厚", "設定柒濃厚", "出現率の設定6%で濃厚",
                      "出現率の設定6割で濃厚", "設定貳以上濃厚", "設定6濃厚（出現率は廿%）", "出現率は貮割")]
    t("★★札に書ける数字は「設定1〜6」だけ（分数・小数・符号で照合をすり抜けない＝Codexの5回目）★★",
      all(any("設定1〜6」だけ" in x for x in b["problems"]) for b in _bad))
    _ok6 = plan(_st, row, dec({"op": "add_rows", "section": "設定示唆まとめ",
                               "rows": [{"trigger": "REG中キャラ紹介：カヲル", "hint": "設定6濃厚"}]}), conf)
    t("　「設定6濃厚」は書ける", not _ok6["problems"])
    _st_b = {"slug": "zzz_g", "sections": [
        {"title": "天井・恩恵", "body": ["天井は非搭載です。設定6の機械割は高めです。"]},
        {"title": "設定示唆まとめ", "type": "settei", "rows": [{"trigger": "終了画面：アスカ", "hint": "高設定示唆"}]}]}
    _ok6b = plan(_st_b, row, dec({"op": "add_rows", "section": "設定示唆まとめ",
                                  "rows": [{"trigger": "REG中キャラ紹介：カヲル", "hint": "設定6濃厚"}]}), conf)
    t("　記事に「設定6濃厚」の文字が無くても「設定6濃厚」は書ける（Codexの7回目＝濃を単位と読んで断っていた）",
      not _ok6b["problems"])
    _rs18 = plan(_st, row, dec({"op": "add_rows", "section": "設定示唆まとめ",
                                "rows": [{"trigger": "終了画面 加賀&赤城", "hint": "高設定示唆"},
                                         {"trigger": "終了画面  加賀&赤城", "hint": "高設定示唆"}]}), conf)
    t("　同じ依頼の中で空白の数だけ違う札も断る（Codexの5回目の重箱）", any("同じ行" in x for x in _rs18["problems"]))
    t("　空白の数だけ違う同じ札も断る（Codexの4回目の重箱）",
      any("同じ札" in x for x in _rs16["problems"]) and any("同じ札" in x for x in _rs17["problems"]))
    t("　文字参照（&#55 など）で表示上の数字を隠せない（Codexの2回目の指摘）",
      any("文字参照" in x for x in _rs10["problems"]))
    _st8 = {"slug": "zzz_g", "sections": [{"title": "設定示唆まとめ", "type": "settei", "tables": [],
                                          "rows": [{"trigger": "終了画面：カヲル", "hint": "設定6濃厚"}]}]}
    _rs8 = plan(_st8, row, dec({"op": "add_rows", "section": "設定示唆まとめ",
                                "rows": [{"trigger": "ムービー：リリス", "hint": "高設定示唆（強）"}]}), conf)
    t("　表の鍵（tables）を持つ欄には札を足さない（ブラウザは表として描く＝Codexの指摘）", bool(_rs8["problems"]))
    try:
        _rs9 = plan({"slug": "zzz_g", "sections": [{"title": "設定示唆まとめ", "type": "settei", "rows": None}]}, row,
                    dec({"op": "add_rows", "section": "設定示唆まとめ",
                         "rows": [{"trigger": "ムービー：リリス", "hint": "高設定示唆（強）"}]}), conf)
    except Exception as _e9:                      # noqa: BLE001  ★例外で落ちたら❌と数える★
        _rs9 = {"problems": [f"例外: {type(_e9).__name__}"]}
    t("　札が壊れた欄では例外で落ちずに理由を返す（Codexの指摘）", any("読めません" in x for x in _rs9["problems"]))
    # ★★設定示唆の欄の表（見出しつき）に札の行を足す★★（2026-11-27・discup_ur）
    _stT = {"slug": "zzz_g", "sections": [{"title": "設定示唆まとめ", "type": "settei", "tables": [
        {"label": "終了画面のトロフィー", "headers": ["演出", "示唆"],
         "rows": [["銅トロフィー", {"text": "設定2以上濃厚", "badge": "weak"}]]}]}]}

    def _tr(cell, sec=None):
        return plan(sec or _stT, row, dec({"op": "add_rows", "section": "設定示唆まとめ", "table": 0,
                                           "rows": [["キリン柄トロフィー", cell]]}), conf)
    _rT1 = _tr({"text": "設定4以上濃厚", "badge": "mid"})
    t("★★設定示唆の欄の表に、札（text と badge）の行を「設定N」つきで足せる★★",
      not _rT1["problems"] and _rT1["sections"][0]["tables"][0]["rows"][-1]
      == ["キリン柄トロフィー", {"text": "設定4以上濃厚", "badge": "mid"}])
    t("　強さの印は描画と同じ5つだけ", bool(_tr({"text": "設定4以上濃厚", "badge": "big"})["problems"]))
    t("　札は text と badge の2つだけ", bool(_tr({"text": "設定4以上濃厚", "badge": "mid", "x": "1"})["problems"]))
    t("　「設定N」以外の数字は今までどおり記事か確定値に要る",
      any("無い数字" in x for x in _tr({"text": "1/9999で設定4以上濃厚", "badge": "mid"})["problems"]))
    _stP = {"slug": "zzz_g", "sections": [{"title": "仕様", "type": "table", "tables": [
        {"headers": ["演出", "示唆"], "rows": [["銅", "設定2以上"]]}]}]}
    _rP = plan(_stP, row, dec({"op": "add_rows", "section": "仕様", "table": 0,
                               "rows": [["キリン柄", "設定4以上濃厚"]]}), conf)
    t("　設定示唆の欄でない表では、「設定N」の数字も照合する（外すのは設定示唆の欄だけ）",
      any("無い数字" in x for x in _rP["problems"]))
    t("　設定示唆の欄でない表には札（辞書）を書けない",
      bool(plan(_stP, row, dec({"op": "add_rows", "section": "仕様", "table": 0,
                                "rows": [["キリン柄", {"text": "設定2以上", "badge": "mid"}]]}), conf)["problems"]))
    # ★★記事に足す項目の数字（item_values）を、名指しして使える★★（2026-11-27）
    _confI = dict(conf, **{"item_values#red": {"value": {"values": ["1/8192", "約80%"]}}})
    _rI = plan(detail, row, dec({"op": "add_section", "title": "赤BB直撃",
                                 "table": {"headers": ["項目", "確率"], "rows": [["赤BB直撃", "1/8192"]]},
                                 "fields": ["item_values#red"]}), _confI)
    t("★★item_values の控えの数字で欄を足せる★★", not _rI["problems"])
    _rI2 = plan(detail, row, dec({"op": "add_section", "title": "赤BB直撃",
                                  "table": {"headers": ["設定", "確率"], "rows": [["1", "1/8193"]]},
                                  "fields": ["item_values#red"]}), _confI)
    t("　控えに無い数字は足せない", any("無い数字" in x for x in _rI2["problems"]))
    # ★★item_values の数はばらして使わない★★（Codex review309 の1＝42/8192・8192% が通った）
    _confI2 = dict(conf, **{"item_values#red": {"value": {"values": ["1/8192", "約42%"]}}})

    def _ivp(cell):
        return plan(detail, row, dec({"op": "add_section", "title": "赤BB直撃",
                                      "table": {"headers": ["項目", "値"], "rows": [["確率", cell]]},
                                      "fields": ["item_values#red"]}), _confI2)["problems"]
    t("★★item_values の数を組み替えた値（42/8192・8192%・11/8192・-1/8192）は足せない★★",
      all(any("無い数字" in x for x in _ivp(c)) for c in ("42/8192", "8192%", "11/8192", "1/8192600", "-1/8192")))
    t("　表示値がまるごと在れば足せる（約42%・1/8192）", not _ivp("約42%") and not _ivp("1/8192"))
    _confI3 = dict(conf, **{"item_values#d": {"value": {"values": ["500枚", "1/8192"]}}})

    def _ivq(cell):
        return plan(detail, row, dec({"op": "add_section", "title": "差枚",
                                      "table": {"headers": ["項目", "値"], "rows": [["差枚", cell]]},
                                      "fields": ["item_values#d"]}), _confI3)["problems"]
    t("★★表示値の前に符号と空白（- 500枚）・後ろに％（1/8192%）があれば足せない★★（Codex review310 の1）",
      any("無い数字" in x for x in _ivq("- 500枚")) and any("無い数字" in x for x in _ivq("1/8192%"))
      and not _ivq("平均500枚"))
    t("★★設定示唆の欄の表に、全角マイナスつきの数（－500枚）は書けない★★（Codex review310 の4）",
      bool(_tr({"text": "－600で設定4以上濃厚", "badge": "mid"})["problems"]))
    t("★★空白を挟んだ符号（-  500枚）・空白つきの％や‰（1/8192 %・1/8192‰）も足せない★★（Codex review311 の1）",
      all(any("無い数字" in x for x in _ivq(c)) for c in ("-  500枚", "1/8192 %", "1/8192‰")))
    t("　設定示唆の欄の表で、マイナス記号と数のあいだに空白があっても断る（－ 600）（Codex review311 の5）",
      bool(_tr({"text": "－ 600で設定4以上濃厚", "badge": "mid"})["problems"]))
    _dIs = dec({"op": "add_section", "title": "差枚",
                "table": {"headers": ["項目", "値"], "rows": [["差枚", "平均500枚"]]}, "fields": ["item_values#d"]})
    _rIs = plan(detail, row, _dIs, _confI3)
    t("★★足した欄の数字の出どころに、照合した item_values の表示値を控える★★（持ち越しで訂正を見るため・Codex review311 の3）",
      not _rIs["problems"] and "500枚|item:item_values#d" in (_dIs["additions"][0].get("_number_sources") or []))
    # ★★Codex review312 の2・4・6★★
    _confI4 = dict(conf, **{"item_values#n": {"value": {"values": ["500", "約500枚", "1　/　8192"]}},
                            "item_values#m": {"value": {"values": ["約500枚", "500枚"]}}})

    def _ivr(cell, f="item_values#n"):
        return plan(detail, row, dec({"op": "add_section", "title": "差枚",
                                      "table": {"headers": ["項目", "値"], "rows": [["差枚", cell]]},
                                      "fields": [f]}), _confI4)["problems"]
    t("★★数で終わる表示値（500）に単位を足せない（500G・500ゲーム）★★",
      any("無い数字" in x for x in _ivr("500G")) and any("無い数字" in x for x in _ivr("500ゲーム"))
      and not _ivr("平均500です"))
    t("★★全角空白を含む表示値（1　/　8192）を出典どおりに書ける★★", not _ivr("1　/　8192"))
    # ★★Codex review313 の2・3・5★★
    _confI5 = dict(conf, **{"item_values#k": {"value": {"values": ["8192", "500", "600"]}}})

    def _ivk(cell):
        return plan(detail, row, dec({"op": "add_section", "title": "差枚",
                                      "table": {"headers": ["項目", "値"], "rows": [["差枚", cell]]},
                                      "fields": ["item_values#k"]}), _confI5)["problems"]
    t("★★空白を挟んだ分数の分母（600 / 8192 の 8192）を表示値として伏せない★★",
      any("無い数字" in x for x in _ivk("600 / 8192")))
    t("★★半角カナ（500 ｹﾞｰﾑ）・周（500周）を数だけの表示値の後ろに足せない★★",
      any("無い数字" in x for x in _ivk("500 ｹﾞｰﾑ")) and any("無い数字" in x for x in _ivk("500周")))
    t("　中点で区切った列挙（500・600）は通る", not _ivk("500・600"))
    t("★★桁を変える字（500万枚・500 万枚）を数だけの表示値の後ろに足せない★★（Codex review314 の2）",
      any("無い数字" in x for x in _ivk("500万枚")) and any("無い数字" in x for x in _ivk("500 万枚")))
    try:
        _rL = plan(detail, row, dec({"op": "add_section", "title": "差枚",
                                     "table": {"headers": ["項目", "値"], "rows": [["差枚", "500"]], "label": 42},
                                     "fields": ["item_values#k"]}), _confI5)["problems"]
    except Exception as _eL:                      # noqa: BLE001  ★例外で落ちたら❌と数える★
        _rL = [f"例外: {type(_eL).__name__}"]
    t("★★太字の印を挟んで桁を変えられない（**500**万枚）★★（Codex review315 の2）",
      any("無い数字" in x for x in _ivk("**500**万枚")) and not _ivk("**500**です"))
    t("★★数だけの表示値の後ろに「まいる」を足せない（500まいる・**500**まいる）★★（Codex review316 の3）",
      any("無い数字" in x for x in _ivk("500まいる")) and any("無い数字" in x for x in _ivk("**500**まいる")))
    _detB = {"slug": "zzz_g", "sections": [{"title": "天井・恩恵", "body": ["平均は**500**枚です。"]},
                                           {"title": "噂・未確定情報", "type": "rumor", "body": ["噂です。"]}]}
    _rB = plan(_detB, row, dec({"op": "add_section", "title": "平均", "body": ["平均の獲得は**500**枚です。"]}), conf)
    t("　記事にある太字の書き方（**500**枚）は、足す文でも今までどおり通る（Codex review316 の4）", not _rB["problems"])
    # ★★Codex review317 の1・2★★
    _confI6 = dict(conf, **{"item_values#s": {"value": {"values": ["500枚", "-500枚"]}}})

    def _ivs(cell, det=detail):
        return plan(det, row, dec({"op": "add_section", "title": "差枚",
                                   "table": {"headers": ["項目", "値"], "rows": [["差枚", cell]]},
                                   "fields": ["item_values#s"]}), _confI6)["problems"]
    t("★★太字の内側から照合を始めて符号を見落とさない（-**500枚**）★★",
      any("無い数字" in x for x in _ivs("平均差枚は-**500枚**です")) and not _ivs("平均は**500枚**です"))
    _det100 = {"slug": "zzz_g", "sections": [{"title": "天井・恩恵", "body": ["最低100枚です。"]},
                                             {"title": "噂・未確定情報", "type": "rumor", "body": ["噂です。"]}]}
    t("★★記事の「100枚」と控えの「-500枚」から範囲（100枚-500枚）を作れない★★",
      any("無い数字" in x for x in _ivs("100枚-500枚", _det100)) and not _ivs("平均差枚は-500枚です", _det100))
    # ★★Codex review318 の1・2★★
    _det100w = {"slug": "zzz_g", "sections": [{"title": "天井・恩恵", "body": ["最低１００枚です。"]},
                                              {"title": "噂・未確定情報", "type": "rumor", "body": ["噂です。"]}]}
    _confI7 = dict(conf, **{"item_values#s": {"value": {"values": ["100.0枚", "-500枚", "500枚"]}}})
    _rW = plan(_det100w, row, dec({"op": "add_section", "title": "差枚",
                                   "table": {"headers": ["項目", "値"], "rows": [["差枚", "１００枚-500枚"]]},
                                   "fields": ["item_values#s"]}), _confI6)["problems"]
    _rD = plan(detail, row, dec({"op": "add_section", "title": "差枚",
                                 "table": {"headers": ["項目", "値"], "rows": [["差枚", "100.0枚-500枚"]]},
                                 "fields": ["item_values#s"]}), _confI7)["problems"]
    t("★★全角の数字・先に伏せた表示値のあとの範囲（１００枚-500枚・100.0枚-500枚）も作れない★★",
      any("無い数字" in x for x in _rW) and any("無い数字" in x for x in _rD))
    _detN = {"slug": "zzz_g", "sections": [{"title": "天井・恩恵", "body": ["平均差枚は-\u00a0500枚です。"]},
                                           {"title": "噂・未確定情報", "type": "rumor", "body": ["噂です。"]}]}
    t("　確定値を名指ししない足す文は、記事と同じ書き方（改行しない空白つき）のまま通る（Codex review319 の2）",
      not plan(_detN, row, dec({"op": "add_section", "title": "差枚",
                                "table": {"headers": ["項目", "値"], "rows": [["差枚", "-\u00a0500枚"]]}}), conf)["problems"])
    t("★★改行しない空白を挟んだ符号（- 500枚）も足せない★★",
      any("無い数字" in x for x in _ivs("平均差枚は- 500枚です")))
    _dIk = dec({"op": "add_section", "title": "差枚",
                "table": {"headers": ["項目", "値"], "rows": [["差枚", "平均500です"]]}, "fields": ["item_values#k"]})
    plan(detail, row, _dIk, _confI5)
    t("★★出どころの控えに、どの項目の表示値かも残す（500|item:item_values#k）★★（Codex review315 の3）",
      "500|item:item_values#k" in (_dIk["additions"][0].get("_number_sources") or []))
    t("　表の label が文字でなくても例外にせず、入力の誤りとして返す（Codex review314 の4）",
      bool(_rL) and not any(x.startswith("例外") for x in _rL))
    _dIu = dec({"op": "add_section", "title": "差枚",
                "table": {"headers": ["項目", "値"], "rows": [["差枚", "約500枚"]]}, "fields": ["item_values#m"]})
    plan(detail, row, _dIu, _confI4)
    t("★★照合に使った表示値だけを控える（約500枚 だけ・500枚 は控えない）★★",
      [x for x in (_dIu["additions"][0].get("_number_sources") or []) if "|item" in x] == ["約500枚|item:item_values#m"])
    t("★★設定示唆の欄の表に丸数字・漢数字は書けない（設定⑦・九十九％）★★（Codex review309 の3）",
      bool(_tr({"text": "設定⑦以上濃厚", "badge": "mid"})["problems"])
      and bool(_tr({"text": "設定4以上で九十九％です", "badge": "mid"})["problems"])
      and bool(_tr("設定４以上濃厚")["problems"]))
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

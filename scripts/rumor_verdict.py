# -*- coding: utf-8 -*-
"""★2AIが決めた噂を、記事の噂の欄（type=rumor）へ書く唯一の場所★（2026-11-05新設）

★運営者の指示★
  ＞ 噂項目あるじゃん？ そこは大手じゃなくてもいいからね？ あくまで噂だし。（2026-11-04）
  ＞ 新台は噂はそもそもないでしょ。更新タスクで探してみてあれば作成や修正
  ＞ 例えば噂を公式が否定したとかね。その場合は噂を書いて横線で消して
  ＞ 〇年〇月〇日メーカー公式Xで否定 とか書いて欲しい（2026-11-05）

★なぜ要るか★＝噂を書き足す道がどこにも無かった（新台の記事づくりは噂の欄を作らず、
  朝の更新の記事直し decide_now は「消す・言い換える」だけ）。

★決めるのは2AI。機械が見るのは形と「言うだけでは通らないこと」だけ★（鉄則0z）
  ・判断者の組（claude+codex／Codexの利用制限時は claude-agent-a+claude-agent-b）・理由15字以上
  ・噂の文に HTML・見えない字・サイト名・「どこから採ったか」の言い回しが無い
  ・★数字を作らない★＝噂の文の数値は、その操作の出典の引用のどれかに全部在る
  ・before は噂の欄の要素とちょうど1つ完全一致
  ・否定の形は `rumor_format` の1か所（日付が実在・未来でない・名前に「公式」）
★出典の引用はページを取り直して照合しない★＝噂は大手でなくてよい（運営者）ので、
  機械が取りに行けない先（X など）も多い。URL と引用はリポジトリの外の控え
  `Documents/uchidokoro/rumor_basis.json` に残す（★記事には出さない★）。
★噂の数値を道具（天井・狙い目・チェッカー・ポチポチくん）の根拠にしない★＝
  それらの道具は噂の欄を読まない。ここからも書かない。

CLI:
  python scripts/rumor_verdict.py --apply <決定ファイル> [--dry-run]
  python scripts/rumor_verdict.py --selftest

決定ファイル（★Writeツールで Documents/uchidokoro/ops/ に書く★）:
  {"slug": "<機種>", "by": ["claude", "codex"], "why": "<どう突き合わせたか・15字以上>",
   "section_title": "<噂の欄が2つ以上ある機種だけ>",
   "empty_section": "remove",            # 消したあと噂が残らないときだけ（欄ごと消す）
   "actions": [
     {"op": "add", "text": "<噂の文>", "sources": [{"url": "...", "quote": "..."}]},
     {"op": "add", "text": "<噂の文>", "sources": [...],
      "denied": {"date": "YYYY-MM-DD", "by": "メーカー公式X", "url": "...", "quote": "..."}},
     {"op": "deny", "before": "<いまの噂の文>", "date": "YYYY-MM-DD", "by": "メーカー公式X",
      "url": "...", "quote": "..."},
     {"op": "revise", "before": "<いまの噂の文>", "text": "<新しい文>", "sources": [...]},
     {"op": "drop", "before": "<いまの噂の文>"},
     {"op": "split", "before": "<いまの噂の文>", "parts": ["<前半>", "<後半>"]}]}
  ・★`source_sha256`（判断したときの記事の指紋）が必ず要る★＝記事データの中身を
    CRLF を LF にそろえてから sha256（decide_now と同じ）。記事が変わっていたら書かない
  ・★出典と否定の根拠には `material`（読んだページの本文を写したファイル）が要る★＝
    `Documents/uchidokoro/ops/` か `research/` の下。quote がその本文に在ることを機械が確かめる
    （空白の違いと全角・半角は問わない）。ページを取り直しはしない
  ・split＝1つの段落の一部だけを公式が否定したとき、段落を分けてから deny する
    （parts をつなぐと before と1文字も違わないこと）
"""
import datetime as _dt
import hashlib
import io
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
BASE = os.path.dirname(HERE)

import local_paths as _lp                                       # noqa: E402
import rumor_format as _rf                                      # noqa: E402

DETAILS = os.path.join(BASE, "assets", "data", "machine-details")
BASIS = os.path.join(_lp.DOCS, "rumor_basis.json")
LOGS = _lp.LOGS
_IN_SELFTEST = False
# ★読んだページの写しを置いてよい場所★（試験では一時の場所へ向ける）
MATERIAL_ROOTS = [os.path.join(_lp.DOCS, "ops"), _lp.RESEARCH]

TITLE = "噂・未確定情報"
# ★新しく作る欄の断り書き★（「噂・公式未確認」と必ず明記する決まり）
LEAD = "**噂・公式未確認**の情報です。実際の挙動と異なる可能性があります。"
WHY_MIN = 15
QUOTE_MIN, QUOTE_MAX = 4, 400
OPS = ("add", "deny", "revise", "drop", "split")
_SLUG = re.compile(r"^[a-z0-9_]{1,60}$")


def _log(msg: str) -> None:
    if _IN_SELFTEST:
        return
    try:
        os.makedirs(LOGS, exist_ok=True)
        now = _dt.datetime.now()
        p = os.path.join(LOGS, f"rumor_verdict_{now:%Y-%m-%d}.log")
        with io.open(p, "a", encoding="utf-8") as f:
            f.write(f"[{now:%Y/%m/%d %H:%M:%S}] {msg}\n")
    except Exception:                                            # noqa: BLE001
        pass


def _judges_ok(by) -> bool:
    import confirmed_values as _cv
    if not isinstance(by, list) or not all(isinstance(x, str) for x in by):
        return False
    return len(by) == len(set(by)) and frozenset(by) in _cv.JUDGE_PAIRS


def _numbers(s: str) -> set:
    import decide_now as _dn
    return set(_dn._numbers(s))


def text_problems(text) -> list:
    """噂の文そのものの検査（★意味は見ない★）。"""
    import gates as _g
    out = []
    if not isinstance(text, str) or not text.strip():
        return ["噂の文が空です"]
    t = text.strip()
    if t != text:
        out.append("噂の文の前後に空白があります")
    if re.search(r"[<>]", t):
        out.append("噂の文にタグ（< >）は使えません（強調は ** だけ）")
    if _rf.STRIKE in t or "~" in t:
        out.append("噂の文に ~ は使えません（取り消し線は deny / denied で付けます）")
    for f in (_g.invisible_unsafe, _g.html_unsafe):
        p = f(t)
        if p:
            out.append(f"噂の文に使えない文字があります: {p}")
    try:
        import html as _h
        p = _g.invisible_unsafe(_h.unescape(t))
        if p:
            out.append(f"噂の文に使えない文字があります（実体参照）: {p}")
    except Exception:                                            # noqa: BLE001
        pass
    import audit_site as _a
    for n in _a.COMPETITOR_NAMES:
        if n in t:
            out.append(f"噂の文にサイト名があります: {n}")
    for h in _a._judge_54_wording(t):
        out.append(f"噂の文に「どこから採ったか」の言い回しがあります: {h}")
    if len(t) > 400:
        out.append("噂の文が長すぎます（400字まで）")
    return out


def _norm(t: str) -> str:
    import unicodedata
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(t or "")))


def _material_root_ok(path: str) -> bool:
    try:
        rp = os.path.realpath(path)
    except Exception:                                            # noqa: BLE001
        return False
    for root in MATERIAL_ROOTS:
        r = os.path.realpath(root)
        if rp.startswith(r + os.sep):
            return True
    return False


def _material_problem(s: dict) -> str:
    """★引用が、読んだページの本文の写しに在るか★（Codex review289）＝言うだけでは通さない。"""
    m = s.get("material")
    if not isinstance(m, str) or not m:
        return "material（読んだページの本文を写したファイル）が要ります"
    if not _material_root_ok(m):
        return "material は Documents/uchidokoro/ops か research の下のファイルだけです"
    if not os.path.isfile(m):
        return "material のファイルがありません"
    try:
        body = io.open(m, encoding="utf-8").read()
    except Exception as e:                                       # noqa: BLE001
        return f"material を読めません: {type(e).__name__}"
    if _norm(s.get("quote")) not in _norm(body):
        return "quote が material の本文に見当たりません"
    return ""


def _material_sha(path: str) -> str:
    return hashlib.sha256(io.open(path, "rb").read()).hexdigest()


def _source_problems(src, need=1) -> list:
    out = []
    if not isinstance(src, list) or len(src) < need:
        return [f"出典（sources）が{need}件以上要ります"]
    for i, s in enumerate(src):
        if not isinstance(s, dict):
            out.append(f"出典{i + 1}が辞書ではありません")
            continue
        u, q = s.get("url"), s.get("quote")
        if not isinstance(u, str) or not re.match(r"^https?://[^\s]+$", u):
            out.append(f"出典{i + 1}の url が正しくありません")
        if not isinstance(q, str) or not (QUOTE_MIN <= len(q.strip()) <= QUOTE_MAX):
            out.append(f"出典{i + 1}の quote は{QUOTE_MIN}〜{QUOTE_MAX}字で書いてください")
            continue
        extra = sorted(set(s) - {"url", "quote", "material"})
        if extra:
            out.append(f"出典{i + 1}に知らない項目があります: " + "、".join(extra))
            continue
        mp = _material_problem(s)
        if mp:
            out.append(f"出典{i + 1}: {mp}")
    return out


def _numbers_problem(text, quotes) -> str:
    """★数字を作らない★＝噂の文の数値が、引用のどれかに全部在ること。"""
    have = set()
    for q in quotes:
        have |= _numbers(q)
    miss = sorted(_numbers(text) - have)
    if miss:
        return "噂の文の数値が引用にありません（数字を作らない）: " + "、".join(miss[:5])
    return ""


def _denial_parts(a) -> tuple:
    """deny と add の denied から（日付, 名前, 引用の辞書）を取り出す。"""
    if a.get("op") == "deny":
        return a.get("date"), a.get("by"), {"url": a.get("url"), "quote": a.get("quote"),
                                             "material": a.get("material")}
    d = a.get("denied")
    if not isinstance(d, dict) or set(d) - {"date", "by", "url", "quote", "material"}:
        return None
    return d.get("date"), d.get("by"), {"url": d.get("url"), "quote": d.get("quote"),
                                         "material": d.get("material")}


def plan(detail: dict, dec: dict, today=None) -> dict:
    """★書き換えたあとの sections を組む（ファイルには触らない）★
    返すもの: {"sections": [...], "problems": [...], "done": [...]}"""
    import copy
    probs, done = [], []
    if not isinstance(detail, dict) or not isinstance(detail.get("sections"), list):
        return {"problems": ["記事データの形が正しくありません"]}
    secs = copy.deepcopy(detail["sections"])
    acts = dec.get("actions")
    if not isinstance(acts, list) or not acts:
        return {"problems": ["actions がありません"]}
    if not _judges_ok(dec.get("by")):
        probs.append("判断者（by）は [\"claude\",\"codex\"] か、Codexの利用制限のときの "
                     "[\"claude-agent-a\",\"claude-agent-b\"] です")
    w = dec.get("why")
    if not isinstance(w, str) or len(w.strip()) < WHY_MIN:
        probs.append(f"理由（why）は{WHY_MIN}字以上で書いてください")
    known = {"slug", "by", "why", "section_title", "empty_section", "actions", "source_sha256"}
    extra = sorted(set(dec) - known)
    if extra:
        probs.append("知らない項目があります（書き間違いを黙って無視しない）: " + "、".join(extra))
    # ★書く欄を決める★
    idx = [i for i, s in enumerate(secs) if isinstance(s, dict) and s.get("type") == "rumor"]
    st = dec.get("section_title")
    if st is not None:
        idx = [i for i in idx if secs[i].get("title") == st]
        if len(idx) != 1:
            probs.append(f"section_title「{str(st)[:30]}」の噂の欄がちょうど1つではありません")
    elif len(idx) > 1:
        probs.append("噂の欄が2つ以上あります。section_title でどの欄かを言ってください")
    if probs:
        return {"problems": probs}
    import build_new_article as _bna
    placeholder = list(_bna.RUMOR_SECTION["body"])
    if idx:
        si = idx[0]
        sec = secs[si]
        if not isinstance(sec.get("body"), list) or \
                not all(isinstance(x, str) for x in sec["body"]):
            return {"problems": ["噂の欄の body が文字列の並びではありません"]}
        if sec["body"] == placeholder:
            sec["body"] = [LEAD]
            done.append("決まり文句だけの欄に断り書きを入れた")
    else:
        si = None
    for k, a in enumerate(acts):
        tag = f"操作{k + 1}"
        if not isinstance(a, dict) or a.get("op") not in OPS:
            probs.append(f"{tag}: op は {'/'.join(OPS)} のどれかです")
            continue
        op = a["op"]
        allowed = {"add": {"op", "text", "sources", "denied"},
                   "deny": {"op", "before", "date", "by", "url", "quote", "material"},
                   "revise": {"op", "before", "text", "sources"},
                   "drop": {"op", "before"},
                   "split": {"op", "before", "parts"}}[op]
        ex = sorted(set(a) - allowed)
        if ex:
            probs.append(f"{tag}: {op} に使えない項目があります: " + "、".join(ex))
            continue
        # 否定の部分
        den = _denial_parts(a) if op in ("deny", "add") else None
        if op == "deny" or (op == "add" and "denied" in a):
            if not den:
                probs.append(f"{tag}: denied は辞書で書いてください")
                continue
            dd, dby, dsrc = den
            ps = _source_problems([dsrc])
            if ps:
                probs += [f"{tag}（否定の根拠）: {x}" for x in ps]
                continue
            try:
                _mk_denied("仮の文です。", dd, dby, today)
            except ValueError as e:
                probs.append(f"{tag}: {e}")
                continue
        # 本文の部分
        if op in ("add", "revise"):
            tp = text_problems(a.get("text"))
            sp = _source_problems(a.get("sources"))
            if tp or sp:
                probs += [f"{tag}: {x}" for x in tp + sp]
                continue
            # ★書き換えでは、元の文にもともと在る数値は「作った数値」ではない★
            _have = [s["quote"] for s in a["sources"]]
            if op == "revise" and isinstance(a.get("before"), str):
                _have.append(a["before"])
            np_ = _numbers_problem(a["text"], _have)
            if np_:
                probs.append(f"{tag}: {np_}")
                continue
        # 当てる
        if op == "add":
            line = a["text"]
            if "denied" in a:
                dd, dby, _ = den
                line = _mk_denied(line, dd, dby, today)
            if si is None:
                secs.append({"title": TITLE, "type": "rumor", "body": [LEAD]})
                si = len(secs) - 1
                done.append("噂の欄を作った")
            if line in secs[si]["body"] or a["text"] in [
                    _strip_denied(x) for x in secs[si]["body"]]:
                probs.append(f"{tag}: 同じ噂がもう欄にあります")
                continue
            secs[si]["body"].append(line)
            done.append(f"足した: {line[:40]}")
            continue
        if si is None:
            probs.append(f"{tag}: 噂の欄がありません（{op} は今ある噂にしか当てられません）")
            continue
        b = a.get("before")
        body = secs[si]["body"]
        hits = [j for j, x in enumerate(body) if x == b]
        if not isinstance(b, str) or len(hits) != 1:
            probs.append(f"{tag}: before が噂の欄の要素とちょうど1つ一致しません（{len(hits)}件）")
            continue
        j = hits[0]
        if j == 0 and body[0] == LEAD:
            probs.append(f"{tag}: 断り書きには当てられません")
            continue
        if op == "drop":
            body.pop(j)
            done.append(f"消した: {b[:40]}")
            continue
        if op == "split":
            parts = a.get("parts")
            if _rf.is_denied(b):
                probs.append(f"{tag}: 否定の印が付いた行は分けられません")
                continue
            if not isinstance(parts, list) or len(parts) < 2 or \
                    not all(isinstance(x, str) and x.strip() and x == x.strip() for x in parts):
                probs.append(f"{tag}: parts は前後に空白の無い文字列を2つ以上です")
                continue
            if "".join(parts) != b:
                probs.append(f"{tag}: parts をつなぐと before と一致しません（分けるだけで、文は変えない）")
                continue
            if len(set(parts)) != len(parts) or any(x in body for x in parts):
                probs.append(f"{tag}: 分けた文が欄の中で重複します")
                continue
            body[j:j + 1] = parts
            done.append(f"分けた: {b[:30]} → {len(parts)}つ")
            continue
        if _rf.is_denied(b):
            probs.append(f"{tag}: すでに否定の印が付いた噂です（{op} では触れません）")
            continue
        if op == "deny":
            dd, dby, _ = den
            try:
                body[j] = _mk_denied(b, dd, dby, today)
            except ValueError as e:
                probs.append(f"{tag}: {e}")
                continue
            done.append(f"否定の印を付けた: {b[:40]}")
            continue
        if op == "revise":
            if a["text"] == b:
                probs.append(f"{tag}: 書き換え前と同じ文です")
                continue
            body[j] = a["text"]
            done.append(f"書き換えた: {b[:30]} → {a['text'][:30]}")
    if probs:
        return {"problems": probs}
    # ★噂が残らない欄★
    if si is not None:
        body = secs[si]["body"]
        rest = [x for x in body if x not in (LEAD,)]
        if not body or (len(body) <= 1 and any(a.get("op") == "drop" for a in acts)):
            if dec.get("empty_section") == "remove":
                secs.pop(si)
                done.append("噂が残らないので欄ごと消した")
            elif not rest:
                return {"problems": ["噂が1つも残りません。欄ごと消すなら empty_section: \"remove\" を書いてください"]}
            else:
                return {"problems": ["噂の欄に要素が1つしか残りません。それが断り書きなら "
                                     "empty_section: \"remove\" で欄ごと消してください（残すなら噂を足す）"]}
        elif dec.get("empty_section") is not None:
            return {"problems": ["empty_section は噂が残らないときだけ書けます"]}
    elif dec.get("empty_section") is not None:
        return {"problems": ["empty_section は噂が残らないときだけ書けます"]}
    after = dict(detail)
    after["sections"] = secs
    fp = _rf.detail_problems(after, today=today)
    if fp:
        return {"problems": ["書いたあとの形が決まりから外れます: " + x for x in fp]}
    return {"sections": secs, "problems": [], "done": done}


def _mk_denied(text, date_iso, by, today):
    """★作る場所は rumor_format.denied_line の1か所★（試験では「今日」を渡す）。"""
    return _rf.denied_line(text, date_iso, by, today=today)


def _strip_denied(x: str) -> str:
    return _rf.denied_body(x) or x


def _article_sha(raw: str) -> str:
    """decide_now と同じ数え方（CRLF を LF にそろえてから）"""
    return hashlib.sha256(raw.encode("utf-8").replace(b"\r\n", b"\n")).hexdigest()


def _indent_of(raw: str) -> int:
    lines = raw.split("\n")
    if len(lines) > 1:
        m = re.match(r"^( +)\S", lines[1])
        if m:
            return len(m.group(1))
    return 1


def _read_basis(path: str) -> list:
    if not os.path.exists(path):
        return []
    import safe_json as _sj
    d = _sj.read_json(path, expect=list)
    return d


def _write_atomic(path: str, text: str, verify=None) -> None:
    """一時ファイルに書き、★読み直して verify を通ってから★置き換える（通らなければ元のまま）。"""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".rumor_", suffix=".tmp")
    try:
        with io.open(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        if verify is not None:
            why = verify(io.open(tmp, encoding="utf-8").read())
            if why:
                raise ValueError(why)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def apply_decision(path: str, apply_it: bool, today=None) -> dict:
    import safe_json as _sj
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
    raw = io.open(p, encoding="utf-8").read()
    want = dec.get("source_sha256")
    now_sha = _article_sha(raw)
    if not isinstance(want, str) or want != now_sha:
        r = {"slug": slug, "problems": [
            "判断したときの記事の指紋（source_sha256）が無いか、いまの記事と違います"
            f"（いま {now_sha[:12]}…）。記事を読み直して決め直してください"]}
        _log(f"{slug}: {r['problems'][0]}")
        return r
    detail = json.loads(raw)
    got = plan(detail, dec, today=today)
    got["slug"] = slug
    if got["problems"]:
        _log(f"{slug}: 断りました: " + " ／ ".join(got["problems"])[:600])
        return got
    if not apply_it:
        return got
    # ★控えが読めなければ書かない★（何を根拠にしたか残せないため）
    try:
        basis = _read_basis(BASIS)
    except Exception as e:                                       # noqa: BLE001
        got["problems"] = [f"噂の控えを読めないので書きません: {type(e).__name__}: {e}"]
        _log(f"{slug}: {got['problems'][0]}")
        return got
    after = dict(detail)
    after["sections"] = got["sections"]
    ind = _indent_of(raw)
    new_raw = json.dumps(after, ensure_ascii=False, indent=ind)
    if raw.endswith("\n"):
        new_raw += "\n"
    now = _dt.datetime.now()
    mats = {}
    for a in dec["actions"]:
        for s_ in (a.get("sources") or []) + ([a] if a.get("op") == "deny" else []) + \
                ([a["denied"]] if isinstance(a.get("denied"), dict) else []):
            if isinstance(s_.get("material"), str) and os.path.isfile(s_["material"]):
                mats[s_["material"]] = _material_sha(s_["material"])
    basis.append({"at": now.strftime("%Y-%m-%dT%H:%M:%S"), "slug": slug,
                  "by": dec["by"], "why": dec["why"], "actions": dec["actions"],
                  "material_sha256": mats,
                  "decision_sha256": hashlib.sha256(
                      json.dumps(dec, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                  "old_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                  "new_sha256": hashlib.sha256(new_raw.encode("utf-8")).hexdigest()})
    # ★控えを先に書く★（記事を書いて控えに失敗すると根拠が残らない）
    _write_atomic(BASIS, json.dumps(basis, ensure_ascii=False, indent=1) + "\n")
    def _verify(txt):
        again = json.loads(txt)
        if again.get("sections") != got["sections"]:
            return "書く内容を読み直したら違っていました"
        fp = _rf.detail_problems(again, today=today)
        return ("書いたあとの形が決まりから外れます: " + fp[0]) if fp else ""
    try:
        _write_atomic(p, new_raw, verify=_verify)
    except ValueError as e:
        got["problems"] = [f"{e}（記事は書き換えていません）"]
        _log(f"{slug}: {got['problems'][0]}")
        return got
    got["written"] = True
    _log(f"{slug}: 書きました（{len(got['done'])}件）: " + " ／ ".join(got["done"])[:600])
    return got


# ---------------------------------------------------------------- 自己試験
def selftest() -> int:
    global DETAILS, BASIS, _IN_SELFTEST, MATERIAL_ROOTS
    keep = (DETAILS, BASIS, _IN_SELFTEST, MATERIAL_ROOTS)
    ok = ng = 0
    T = _dt.date(2026, 11, 5)

    def t(name, cond):
        nonlocal ok, ng
        if cond:
            ok += 1
        else:
            ng += 1
            print("NG", name)

    with tempfile.TemporaryDirectory(prefix="rumor_selftest_") as tmp:
        try:
            _selftest_body(tmp, t, T)
        finally:
            DETAILS, BASIS, _IN_SELFTEST, MATERIAL_ROOTS = keep
    print(f"{ok}/{ok + ng} 合格")
    return 0 if ng == 0 else 1


def _selftest_body(tmp, t, T):
    global DETAILS, BASIS, _IN_SELFTEST, MATERIAL_ROOTS
    _IN_SELFTEST = True
    DETAILS = os.path.join(tmp, "d")
    BASIS = os.path.join(tmp, "basis.json")
    MATERIAL_ROOTS = [os.path.join(tmp, "ops")]
    os.makedirs(DETAILS)
    os.makedirs(MATERIAL_ROOTS[0])
    MAT = os.path.join(MATERIAL_ROOTS[0], "page.txt")
    io.open(MAT, "w", encoding="utf-8").write(
        "掲示板の話\n朝一は 1/8192 でフリーズという話\nメーカー公式：そのような仕様はありません。\n事実ではありません")
    OUTSIDE = os.path.join(tmp, "outside.txt")
    io.open(OUTSIDE, "w", encoding="utf-8").write("朝一は1/8192でフリーズという話")
    LINE = "設定6は朝一にフリーズするという噂があります。"
    base_detail = {"slug": "zzz_r", "sections": [
        {"title": "天井・恩恵", "body": ["天井は999Gです。"]},
        {"title": TITLE, "type": "rumor", "body": ["※以下は公式未確認の噂情報です。", LINE]}]}
    DP = os.path.join(DETAILS, "zzz_r.json")

    def put(d):
        io.open(DP, "w", encoding="utf-8", newline="").write(
            json.dumps(d, ensure_ascii=False, indent=1) + "\n")

    def sha():
        return _article_sha(io.open(DP, encoding="utf-8").read())

    def run(d, apply_it=True):
        q = os.path.join(tmp, "dec.json")
        d = dict(d)
        d.setdefault("source_sha256", sha())
        io.open(q, "w", encoding="utf-8").write(json.dumps(d, ensure_ascii=False))
        return apply_decision(q, apply_it, today=T)

    def read():
        return json.loads(io.open(DP, encoding="utf-8").read())

    SRC = [{"url": "https://example.com/a", "quote": "朝一は1/8192でフリーズという話", "material": MAT}]
    DEN = {"url": "https://x.com/maker/status/1", "quote": "そのような仕様はありません", "material": MAT}
    BY = ["claude", "codex"]
    WHY = "2AIで同じページを読み、内容が一致したので載せます"
    # 1) 否定の印
    put(base_detail)
    r = run({"slug": "zzz_r", "by": BY, "why": WHY, "actions": [
        {"op": "deny", "before": LINE, "date": "2026-11-01", "by": "メーカー公式X", **DEN}]})
    t("否定の印を付けられる", r.get("written") and read()["sections"][1]["body"][1] ==
      "~~" + LINE + "~~（2026年11月1日 メーカー公式Xで否定）")
    t("控えに残る（本文の写しの指紋つき）", len(_read_basis(BASIS)) == 1
      and MAT in _read_basis(BASIS)[0]["material_sha256"])
    denied_now = read()["sections"][1]["body"][1]
    # 2) 否定済みにはもう触れない（deny / revise / split）
    for act in ({"op": "deny", "before": denied_now, "date": "2026-11-01", "by": "メーカー公式X", **DEN},
                {"op": "revise", "before": denied_now, "text": LINE, "sources": SRC},
                {"op": "split", "before": denied_now, "parts": [denied_now[:5], denied_now[5:]]}):
        r = run({"slug": "zzz_r", "by": BY, "why": WHY, "actions": [act]})
        t(f"否定済みは {act['op']} で触れない", r["problems"] and not r.get("written"))
    # 2b) ほかの道具（decide_now）からも触れない
    import decide_now as _dn
    det = read()
    t("decide_now 側の守り: 否定の行の一部", _rf.touches_denied(det, ["2026年11月1日"]) != "")
    t("decide_now 側の守り: ~~ を含む", _rf.touches_denied(det, ["~~x"]) != "")
    t("decide_now 側の守り: 無関係な文は通す", _rf.touches_denied(det, ["天井は999Gです。"]) == "")
    t("decide_now が守りを呼んでいる", "_rfm.touches_denied(" in io.open(_dn.__file__, encoding="utf-8").read())
    # 3) 足す（数値は引用に在る）
    put(base_detail)
    r = run({"slug": "zzz_r", "by": BY, "why": WHY, "actions": [
        {"op": "add", "text": "朝一は1/8192でフリーズするという噂があります。", "sources": SRC}]})
    t("噂を足せる", r.get("written") and read()["sections"][1]["body"][-1].startswith("朝一は1/8192"))
    # 4) 数字を作ると断る／引用が本文に無いと断る／写しの置き場の外は断る
    put(base_detail)
    before = read()
    r = run({"slug": "zzz_r", "by": BY, "why": WHY, "actions": [
        {"op": "add", "text": "朝一は1/4096でフリーズするという噂があります。", "sources": SRC}]})
    t("引用に無い数値は断る", r["problems"] and read() == before)
    r = run({"slug": "zzz_r", "by": BY, "why": WHY, "actions": [
        {"op": "add", "text": "噂です。", "sources": [dict(SRC[0], quote="本文に無い引用です")]}]})
    t("本文に無い引用は断る", r["problems"] and read() == before)
    r = run({"slug": "zzz_r", "by": BY, "why": WHY, "actions": [
        {"op": "add", "text": "噂です。", "sources": [dict(SRC[0], material=OUTSIDE)]}]})
    t("置き場の外の写しは断る", r["problems"] and read() == before)
    r = run({"slug": "zzz_r", "by": BY, "why": WHY, "actions": [
        {"op": "add", "text": "噂です。", "sources": [{"url": SRC[0]["url"], "quote": SRC[0]["quote"]}]}]})
    t("写しの無い出典は断る", r["problems"] and read() == before)
    r = run({"slug": "zzz_r", "by": BY, "why": WHY, "actions": [
        {"op": "deny", "before": LINE, "date": "2026-11-01", "by": "メーカー公式X",
         **dict(DEN, quote="写しに無い否定の文")}]})
    t("否定の根拠も本文に無ければ断る", r["problems"] and read() == before)
    # 4b) 記事の指紋
    r = run({"slug": "zzz_r", "by": BY, "why": WHY, "source_sha256": "0" * 64, "actions": [
        {"op": "add", "text": "噂です。", "sources": SRC}]})
    t("記事の指紋が違えば断る", r["problems"] and read() == before)
    # 5) 判断者・理由
    for bad_by in (["claude"], ["claude", "claude-agent-b"], ["claude", "codex", "x"], "claude,codex"):
        r = run({"slug": "zzz_r", "by": bad_by, "why": WHY, "actions": [
            {"op": "add", "text": "噂です。", "sources": SRC}]})
        t(f"判断者の組を断る {bad_by}", r["problems"] and read() == before)
    r = run({"slug": "zzz_r", "by": ["claude-agent-a", "claude-agent-b"], "why": WHY, "actions": [
        {"op": "add", "text": "代役の噂です。", "sources": SRC}]}, apply_it=False)
    t("代役の組は通る（空打ち）", not r["problems"])
    r = run({"slug": "zzz_r", "by": BY, "why": "短い", "actions": [
        {"op": "add", "text": "噂です。", "sources": SRC}]})
    t("理由が短いと断る", r["problems"])
    # 6) 欄が無い機種には作る（末尾・断り書きつき）
    put({"slug": "zzz_r", "sections": [{"title": "天井・恩恵", "body": ["天井は999Gです。"]}]})
    r = run({"slug": "zzz_r", "by": BY, "why": WHY, "actions": [
        {"op": "add", "text": "新しい噂があります。", "sources": SRC,
         "denied": {"date": "2026-10-30", "by": "メーカー公式X", **DEN}}]})
    s_ = read()["sections"]
    t("欄を末尾に作る", r.get("written") and s_[-1]["type"] == "rumor" and s_[-1]["body"][0] == LEAD
      and s_[-1]["body"][1] == "~~新しい噂があります。~~（2026年10月30日 メーカー公式Xで否定）")
    # 7) 公式でない否定・未来の日付・名前にHTMLや見えない字
    put(base_detail)
    for bad in ({"date": "2026-10-30", "by": "有名ライター"}, {"date": "2026-11-09", "by": "メーカー公式X"},
                {"date": "2026-10-30", "by": "公式<b>X"}, {"date": "2026-10-30", "by": "公式​X"}):
        r = run({"slug": "zzz_r", "by": BY, "why": WHY, "actions": [
            {"op": "deny", "before": LINE, **DEN, **bad}]})
        t(f"否定の形を断る {bad['by']}/{bad['date']}", r["problems"] and read() == base_detail)
    # 8) サイト名・どこから採ったか・~・HTML
    for bad_text in ("掲示板で話題の噂です。", "噂です~。", "<b>噂</b>です。"):
        r = run({"slug": "zzz_r", "by": BY, "why": WHY, "actions": [
            {"op": "add", "text": bad_text, "sources": SRC}]})
        t(f"噂の文を断る {bad_text}", r["problems"] and read() == base_detail)
    # 9) 全か無か（2件目が駄目なら1件目も書かない）
    r = run({"slug": "zzz_r", "by": BY, "why": WHY, "actions": [
        {"op": "add", "text": "よい噂です。", "sources": SRC},
        {"op": "drop", "before": "無い文です。"}]})
    t("全か無か", r["problems"] and read() == base_detail)
    # 10) 全部消すときは欄ごと（明示）
    r = run({"slug": "zzz_r", "by": BY, "why": WHY, "actions": [{"op": "drop", "before": LINE}]})
    t("断り書きだけ残るなら止まる", r["problems"] and read() == base_detail)
    r = run({"slug": "zzz_r", "by": BY, "why": WHY, "empty_section": "remove",
             "actions": [{"op": "drop", "before": LINE}]})
    t("明示すれば欄ごと消す", r.get("written") and
      not any(x.get("type") == "rumor" for x in read()["sections"]))
    # 11) 知らない項目・書き換え
    put(base_detail)
    r = run({"slug": "zzz_r", "by": BY, "why": WHY, "note": "x", "actions": [
        {"op": "add", "text": "噂です。", "sources": SRC}]})
    t("知らない項目は断る", r["problems"] and read() == base_detail)
    r = run({"slug": "zzz_r", "by": BY, "why": WHY, "actions": [
        {"op": "revise", "before": LINE,
         "text": "設定6は朝一に1/8192でフリーズするという噂があります。", "sources": SRC}]})
    t("書き換えられる", r.get("written") and "1/8192" in read()["sections"][1]["body"][1])
    # 11b) 一部だけの否定＝分けてから否定する
    A, B = "Aの噂があります。", "Bの噂があります。"
    put({"slug": "zzz_r", "sections": [{"title": TITLE, "type": "rumor", "body": [LEAD, A + B]}]})
    r = run({"slug": "zzz_r", "by": BY, "why": WHY, "actions": [
        {"op": "split", "before": A + B, "parts": [A, B]},
        {"op": "deny", "before": A, "date": "2026-11-01", "by": "メーカー公式X", **DEN}]})
    t("分けてから一部だけ否定できる", r.get("written") and read()["sections"][0]["body"] ==
      [LEAD, "~~" + A + "~~（2026年11月1日 メーカー公式Xで否定）", B])
    put({"slug": "zzz_r", "sections": [{"title": TITLE, "type": "rumor", "body": [LEAD, A + B]}]})
    r = run({"slug": "zzz_r", "by": BY, "why": WHY, "actions": [
        {"op": "split", "before": A + B, "parts": [A, "Cの噂があります。"]}]})
    t("分けるときに文を変えると断る", r["problems"])
    # 12) 決まり文句だけの欄（新台の雛形）は断り書きへ入れ替える
    import build_new_article as _bna
    put({"slug": "zzz_r", "sections": [dict(_bna.RUMOR_SECTION)]})
    r = run({"slug": "zzz_r", "by": BY, "why": WHY, "actions": [
        {"op": "add", "text": "噂です。", "sources": SRC}]})
    t("決まり文句を入れ替える", r.get("written") and read()["sections"][0]["body"] == [LEAD, "噂です。"])
    # 13) 空打ちは書かない
    put(base_detail)
    r = run({"slug": "zzz_r", "by": BY, "why": WHY, "actions": [
        {"op": "add", "text": "噂です。", "sources": SRC}]}, apply_it=False)
    t("空打ちは書かない", not r["problems"] and read() == base_detail)
    # 14) slug
    r = run({"slug": "../x", "by": BY, "why": WHY, "actions": []})
    t("slug を断る", r["problems"])
    # 15) 置き換える前に検査する（検査が落ちたら記事は元のまま）
    try:
        _write_atomic(DP, "{}", verify=lambda txt: "わざと断る")
        t("検査が落ちたら置き換えない", False)
    except ValueError:
        t("検査が落ちたら置き換えない", read() == base_detail)
    t("一時ファイルを残さない", not [x for x in os.listdir(DETAILS) if x.startswith(".rumor_")])
    # 16) 書き換えで新しい数値を作ると断る（元の文にも引用にも無い数値）
    put(base_detail)
    r = run({"slug": "zzz_r", "by": BY, "why": WHY, "actions": [
        {"op": "revise", "before": LINE, "text": "設定6は朝一に1/4096でフリーズするという噂があります。",
         "sources": SRC}]})
    t("書き換えでも数値を作ると断る", r["problems"] and read() == base_detail)
    # 17) ★描画は2か所とも、本文だけに線を引き、日付と名前は線の外★
    import build_machine_pages as _bmp
    sample = "~~噂の文です。~~（2026年11月1日 メーカー公式Xで否定）"
    t("事前に作るページの描画", _bmp.md(sample) ==
      "<del>噂の文です。</del>（2026年11月1日 メーカー公式Xで否定）")
    js = io.open(os.path.join(BASE, "machine.html"), encoding="utf-8").read()
    t("JS の描画も同じ置き換え", ".replace(/~~([^~]+?)~~/g, '<del>$1</del>')" in js)
    # 17-b) ★記事データの改行の印は、事前に作るページでも改行にする★（2026-11-09）
    #   JS は innerHTML で描くので改行になる。ここが文字のままだと「<br>」が読者とGoogleに見える。
    t("事前に作るページも改行の印を改行にする",
      _bmp.md("**周期天井**：5周期<br>スルー天井") ==
      "<strong>周期天井</strong>：5周期<br>スルー天井")
    t("要約の箱・基本情報の印も同じ", _bmp.safe_tags(_bmp.esc("A<br/>B")) == "A<br>B")
    t("属性つきの印は文字のまま", _bmp.md('A<br class="x">B') == "A&lt;br class=&quot;x&quot;&gt;B")
    t("許していない印は文字のまま", _bmp.md("A<i>B</i>") == "A&lt;i&gt;B&lt;/i&gt;")
    # 18) 文体の見張りが、否定の注記を文として数えない
    import style_check as _sc
    o = []
    _sc._check_text(o, "zzz_r", TITLE, sample)
    t("文体の見張りは否定の行を違反にしない", o == [])
    o = []
    _sc._check_text(o, "zzz_r", TITLE, "~~噂の文だ。~~（2026年11月1日 メーカー公式Xで否定）")
    t("取り消した本文の文体は今までどおり見る", len(o) == 1)
    # 19) ★否定の印を付けても、文体の違反の印は変わらない★（Codex review290）
    plain = "設定6のみ優遇との話あり"
    o1, o2 = [], []
    _sc._check_text(o1, "zzz_r", TITLE, plain)
    _sc._check_text(o2, "zzz_r", TITLE, "~~" + plain + "~~（2026年11月1日 メーカー公式Xで否定）")
    t("否定の前後で文体の違反が同じ", len(o1) == 1 and o1 == o2)


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:                                        # noqa: BLE001
            pass
    a = sys.argv[1:]
    if "--selftest" in a:
        return selftest()
    if "--apply" in a:
        i = a.index("--apply")
        if i + 1 >= len(a):
            print("--apply <決定ファイル>")
            return 2
        r = apply_decision(a[i + 1], "--dry-run" not in a)
        for x in r.get("done", []):
            print("  ・" + x)
        for x in r.get("problems", []):
            print("★" + x + "★")
        if r.get("problems"):
            return 1
        print("書きました" if r.get("written") else "★書いていません★（空打ち）")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())

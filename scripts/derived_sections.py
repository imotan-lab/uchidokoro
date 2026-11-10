# -*- coding: utf-8 -*-
"""★新台の「狙い目の根拠」「ヤメ時の判断」「立ち回りのコツ」を、2AIが書く唯一の場所★（2026-11-10新設）

★なぜ要るか★（2026-11-10・運営者「更新されないページ他にもないの？」で見つかった）
  新台経路の記事は、この3つの欄を作るとき必ず「未確認」にする（build_new_article.build_detail）。
  ところが、あとで書き込む道がどのタスクにも無かった（記事直しの decide_now は消す・言い換えるだけ）。
  実測＝新台29機種すべてで3欄とも「未確認」のまま（87欄）＝記事の下半分がずっと空の箱だった。

★書く中身はサイトの中で閉じる★（_design/items_2ai_2026-09-18.md の「B. 書く項目」）
  ＝その機種の記事と機種行に、もう載っている事実（天井・恩恵・機械割・狙い目・モード・ゾーン・
  朝一・リセットなど）から2AIが文章にする。★外の出典は見に行かない★。

★決めるのは2AI。機械が見るのは形と「数字を作っていないか」だけ★（鉄則0z）
  ・判断者の組（claude+codex／Codexの利用制限時は claude-agent-a+claude-agent-b）・理由15字以上
  ・判断したときの記事の指紋（source_sha256）がいまの記事と同じ
  ・新台経路の機種だけ（旧形式の記事はこの3欄を人が書いてある。直すのは decide_now）
  ・欄はこの3つだけ・段落は1〜4・1段落20〜300字・タグ・見えない字・サイト名・「どこから採ったか」の言い回しなし
  ・★数字を作らない★＝書く文の数値は、この3欄を除いた記事と機種行（identity・判定書を除く）に
    同じ単位で載っているものだけ（数値の読み方は decide_now の1か所）
★書いたら、その機種のページを作り直す★（作り直せなければ記事を元に戻す）。
★毎朝の育成（grow_machine）は記事を作り直すとこの3欄を「未確認」で作るので、
  前の記事に書いてあれば持ち越す★（grow_machine.carry_written_sections）。

CLI:
  python scripts/derived_sections.py --next 3                 # 今日書く機種（未確認の欄が残っている新台）
  python scripts/derived_sections.py --gather <slug>          # 2AIに渡す材料（記事の全文と、書いてよい数値）
  python scripts/derived_sections.py --apply <決定ファイル> [--dry-run]
  python scripts/derived_sections.py --tried <slug> --result NOT_ENOUGH|UNDECIDED
  python scripts/derived_sections.py --check                   # 毎朝進んでいるか（番人）
  python scripts/derived_sections.py --selftest

決定ファイル（★Writeツールで Documents/uchidokoro/ops/ に書く★）:
  {"slug": "<機種>", "by": ["claude", "codex"], "why": "<何をもとにどう書いたか・15字以上>",
   "source_sha256": "<--gather が出した記事の指紋>",
   "sections": [{"title": "狙い目の根拠", "body": ["<段落>", ...]}, ...]}
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

import local_paths as _lp            # noqa: E402

DETAILS = os.path.join(BASE, "assets", "data", "machine-details")
MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
BASIS = os.path.join(_lp.DOCS, "derived_sections_basis.json")
QUEUE = os.path.join(_lp.DOCS, "derived_sections_queue.json")
LOGS = _lp.LOGS

TITLES = ("狙い目の根拠", "ヤメ時の判断", "立ち回りのコツ")
WHY_MIN = 15
PARA_MIN, PARA_MAX, PARAS_MAX = 20, 300, 4
RETRY_DAYS = 3          # 「書けるだけの事実が無い」「決まらない」と答えた機種は3日あける
STALL_DAYS = 3          # 未確認の欄が残っているのに、3日だれも試していなければ番人が知らせる
RESULTS = ("NOT_ENOUGH", "UNDECIDED")
_SLUG = re.compile(r"^[a-z0-9_]{1,60}$")
_SKIP_ROW_KEYS = ("identity", "page_decision")   # ★機種IDや判定書の数字を「載っている事実」に数えない★
_IN_SELFTEST = False


class StopWrite(ValueError):
    pass


def _log(msg: str) -> None:
    if _IN_SELFTEST:
        return
    try:
        os.makedirs(LOGS, exist_ok=True)
        p = os.path.join(LOGS, "derived_sections_%s.log" % _dt.date.today().isoformat())
        with io.open(p, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (_dt.datetime.now().strftime("%H:%M:%S"), msg))
    except OSError:
        pass


def _pending_texts():
    import build_new_article as _ba
    return tuple(_ba.PENDING_TEXTS)


def is_pending(sec) -> bool:
    """★その欄が「未確認」のままか★（本文が未確認の決まり文句だけ）"""
    if not isinstance(sec, dict):
        return False
    body = sec.get("body")
    return (isinstance(body, list) and len(body) == 1
            and isinstance(body[0], str) and body[0].strip() in _pending_texts())


def pending_titles(detail: dict) -> list:
    return [s.get("title") for s in (detail.get("sections") or [])
            if isinstance(s, dict) and s.get("title") in TITLES and is_pending(s)]


def article_sha(raw: str) -> str:
    """decide_now・rumor_verdict と同じ数え方（CRLF を LF にそろえてから）"""
    return hashlib.sha256(raw.encode("utf-8").replace(b"\r\n", b"\n")).hexdigest()


def _judges_ok(by) -> bool:
    import confirmed_values as _cv
    if not isinstance(by, list) or not all(isinstance(x, str) for x in by):
        return False
    return len(by) == len(set(by)) and frozenset(by) in _cv.JUDGE_PAIRS


def number_pool(detail: dict, row: dict) -> tuple:
    """★書いてよい数値★＝この3欄を除いた記事と、機種行（identity・判定書を除く）に載っているもの

    戻り値＝(（数値, 単位）の組の集合, 数値だけの集合)
    """
    import decide_now as _dn
    d = json.loads(json.dumps(detail))
    d["sections"] = [s for s in (d.get("sections") or [])
                     if not (isinstance(s, dict) and s.get("title") in TITLES)]
    r = {k: v for k, v in (row or {}).items() if k not in _SKIP_ROW_KEYS}
    strings = _dn._strings_in(d) + _dn._strings_in(r)
    # ★機種行の数値（チェッカーの線など）は文字ではなく数なので、文字にして足す★
    def _nums(o):
        if isinstance(o, bool):
            return
        if isinstance(o, (int, float)):
            yield str(int(o)) if float(o).is_integer() else str(o)
        elif isinstance(o, list):
            for x in o:
                yield from _nums(x)
        elif isinstance(o, dict):
            for x in o.values():
                yield from _nums(x)
    pairs, toks = set(), set()
    for s in strings:
        for tok, unit in _dn.numbers_with_unit(s):
            pairs.add((tok, unit))
            toks.add(tok)
    for n in _nums(r):
        toks.add(n)
    return pairs, toks


def text_problems(text) -> list:
    """★書く文そのものの検査★（意味は見ない）"""
    import gates as _g
    import audit_site as _a
    out = []
    if not isinstance(text, str) or not text.strip():
        return ["段落が空です"]
    t = text
    if t.strip() != t:
        out.append("段落の前後に空白があります")
    if not PARA_MIN <= len(t) <= PARA_MAX:
        out.append(f"段落は{PARA_MIN}〜{PARA_MAX}字で書きます（いま{len(t)}字）")
    if re.search(r"[<>~]", t):
        out.append("段落にタグ（< >）や ~ は使えません（強調は ** だけ）")
    if t.strip() in _pending_texts():
        out.append("未確認の決まり文句を書いても埋まりません")
    for f in (_g.invisible_unsafe, _g.html_unsafe):
        p = f(t)
        if p:
            out.append(f"段落に使えない文字があります: {p}")
    try:
        import html as _h
        p = _g.invisible_unsafe(_h.unescape(t))
        if p:
            out.append(f"段落に使えない文字があります（実体参照）: {p}")
    except Exception:                                            # noqa: BLE001
        pass
    for n in _a.COMPETITOR_NAMES:
        if n in t:
            out.append(f"段落にサイト名があります: {n}")
    for h in _a._judge_54_wording(t):
        out.append(f"段落に「どこから採ったか」の言い回しがあります: {h}")
    return out


def plan(detail: dict, row: dict, dec: dict) -> dict:
    """★書く前に全部確かめる★（1つでも外れたら何も書かない）。戻り値の sections が書いたあとの姿"""
    import page_decision as _pd
    import decide_now as _dn
    p = []
    if not _pd.is_auto(row or {}):
        return {"problems": ["新台経路の機種ではありません（旧形式の記事は decide_now で直します）"]}
    if not _judges_ok(dec.get("by")):
        p.append("判断者の組がそろっていません（claude と codex、または代役の2人）")
    if len(str(dec.get("why") or "").strip()) < WHY_MIN:
        p.append(f"理由（why）は{WHY_MIN}字以上で書きます")
    want = dec.get("sections")
    if not isinstance(want, list) or not 1 <= len(want) <= len(TITLES):
        return {"problems": p + ["sections は1〜3件で書きます"]}
    titles = [w.get("title") if isinstance(w, dict) else None for w in want]
    if len(set(titles)) != len(titles) or any(t not in TITLES for t in titles):
        return {"problems": p + [f"書ける欄は {'・'.join(TITLES)} だけです（同じ欄は1回）"]}
    secs = json.loads(json.dumps(detail.get("sections") or []))
    pos = {s.get("title"): i for i, s in enumerate(secs) if isinstance(s, dict)}
    pairs, toks = number_pool(detail, row)
    done = []
    for w in want:
        ti = w["title"]
        if ti not in pos:
            p.append(f"{ti}: 記事にこの欄がありません")
            continue
        body = w.get("body")
        if (not isinstance(body, list) or not 1 <= len(body) <= PARAS_MAX
                or set(w) - {"title", "body"}):
            p.append(f"{ti}: body は段落の並び（1〜{PARAS_MAX}件）だけで書きます")
            continue
        for para in body:
            for x in text_problems(para):
                p.append(f"{ti}: {x}")
            if not isinstance(para, str):
                continue
            for tok, unit in _dn.numbers_with_unit(para):
                if (tok, unit) in pairs or (unit == "" and tok in toks):
                    continue
                p.append(f"{ti}: 記事に載っていない数値です（数字を作らない）: {tok}{unit}")
        sec = secs[pos[ti]]
        if sec.get("type") not in (None, ""):
            p.append(f"{ti}: 表の欄には書けません")
            continue
        new = {"title": ti, "body": list(body)}
        secs[pos[ti]] = new
        done.append(ti)
    if p:
        return {"problems": p}
    return {"problems": [], "sections": secs, "done": done}


def _read_rows() -> list:
    import safe_json as _sj
    return _sj.read_rows(MACHINES)


def _row(slug: str) -> dict:
    return next((m for m in _read_rows() if m.get("slug") == slug), {}) or {}


def _indent_of(raw: str) -> int:
    lines = raw.split("\n")
    if len(lines) > 1:
        m = re.match(r"^( +)\S", lines[1])
        if m:
            return len(m.group(1))
    return 1


def _write_atomic(path: str, text: str) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".derived_", suffix=".tmp")
    try:
        with io.open(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _read_json_or(path: str, default, expect):
    if not os.path.exists(path):
        return default
    import safe_json as _sj
    return _sj.read_json(path, expect=expect)


def rebuild_page(slug: str) -> bool:
    import build_machine_pages as _b
    return (_b._rebuild_auto(slug) or 0) == 0


def apply_decision(path: str, apply_it: bool) -> dict:
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
    raw = io.open(p, encoding="utf-8", newline="").read()
    now_sha = article_sha(raw)
    if dec.get("source_sha256") != now_sha:
        r = {"slug": slug, "problems": [
            "判断したときの記事の指紋（source_sha256）が無いか、いまの記事と違います"
            f"（いま {now_sha[:12]}…）。--gather で読み直して決め直してください"]}
        _log(f"{slug}: {r['problems'][0]}")
        return r
    detail = json.loads(raw)
    got = plan(detail, _row(slug), dec)
    got["slug"] = slug
    if got["problems"]:
        _log(f"{slug}: 断りました: " + " ／ ".join(got["problems"])[:600])
        return got
    if not apply_it:
        return got
    try:
        basis = _read_json_or(BASIS, [], list)
        queue = _read_json_or(QUEUE, {"tried": {}}, dict)
    except Exception as e:                                       # noqa: BLE001
        got["problems"] = [f"控えを読めないので書きません: {type(e).__name__}: {e}"]
        _log(f"{slug}: {got['problems'][0]}")
        return got
    after = dict(detail)
    after["sections"] = got["sections"]
    new_raw = json.dumps(after, ensure_ascii=False, indent=_indent_of(raw))
    if raw.endswith("\n"):
        new_raw += "\n"
    if "\r\n" in raw:
        new_raw = new_raw.replace("\r\n", "\n").replace("\n", "\r\n")
    now = _dt.datetime.now()
    basis.append({"at": now.strftime("%Y-%m-%dT%H:%M:%S"), "slug": slug,
                  "by": dec["by"], "why": dec["why"], "sections": dec["sections"],
                  "old_sha256": now_sha, "new_sha256": article_sha(new_raw)})
    # ★控えを先に書く★（記事を書いて控えに失敗すると根拠が残らない）
    _write_atomic(BASIS, json.dumps(basis, ensure_ascii=False, indent=1) + "\n")
    _write_atomic(p, new_raw)
    try:
        ok = rebuild_page(slug)
    except Exception as e:                                       # noqa: BLE001
        ok = False
        got.setdefault("notes", []).append(f"作り直しで例外: {type(e).__name__}: {e}")
    if not ok:
        # ★ページを作り直せなければ記事を元に戻す★（記事とページを食い違わせない）
        if io.open(p, encoding="utf-8", newline="").read() == new_raw:
            _write_atomic(p, raw)
        got["problems"] = ["ページを作り直せなかったので、記事を元に戻しました"]
        _log(f"{slug}: {got['problems'][0]}")
        return got
    queue.setdefault("tried", {})[slug] = {"at": now.strftime("%Y-%m-%d"), "result": "WRITTEN",
                                           "left": pending_titles(after)}
    _write_atomic(QUEUE, json.dumps(queue, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    got["written"] = True
    _log(f"{slug}: 書きました: {'・'.join(got['done'])}（判断者 {','.join(dec['by'])}）")
    return got


# ---------------------------------------------------------------- 割り当て
def candidates(rows: list, details: dict) -> list:
    """★未確認の欄が残っている新台★（機種一覧の並び順）"""
    import page_decision as _pd
    out = []
    for m in rows:
        s = m.get("slug")
        if not _pd.is_auto(m) or s not in details:
            continue
        if pending_titles(details[s]):
            out.append(s)
    return out


def pick(n: int, cands: list, tried: dict, today: str) -> list:
    """★純関数★ 一度も試していない機種→最後に試した日が古い順。今日試した機種と、
    「書けない・決まらない」と答えて RETRY_DAYS 日たっていない機種は外す"""
    cut = (_dt.date.fromisoformat(today) - _dt.timedelta(days=RETRY_DAYS)).isoformat()
    never, old = [], []
    for s in cands:
        t = tried.get(s)
        if not t:
            never.append(s)
            continue
        at = str(t.get("at", ""))
        if at >= today:
            continue
        if t.get("result") in RESULTS and at > cut:
            continue
        old.append(s)
    old.sort(key=lambda s: str(tried[s].get("at", "")))
    return (never + old)[:max(0, n)]


def stall_problem(cands: list, tried: dict, today: str):
    """★未確認の欄が残っているのに、STALL_DAYS 日だれも試していなければ問題★（文 か None）"""
    if not cands:
        return None
    last = max([str((tried.get(s) or {}).get("at", "")) for s in tried] or [""])
    if not last:
        return f"未確認の欄が残っている新台が{len(cands)}機種あるのに、一度も書こうとしていません"
    days = (_dt.date.fromisoformat(today) - _dt.date.fromisoformat(last)).days
    if days >= STALL_DAYS:
        return f"未確認の欄が残っている新台が{len(cands)}機種あるのに、{days}日だれも書こうとしていません"
    return None


def _load_all():
    import safe_json as _sj
    rows = _read_rows()
    details = {}
    for m in rows:
        p = os.path.join(DETAILS, str(m.get("slug")) + ".json")
        if os.path.isfile(p):
            details[m["slug"]] = _sj.read_json(p, expect=dict)
    return rows, details


def gather(slug: str) -> dict:
    p = os.path.join(DETAILS, slug + ".json")
    raw = io.open(p, encoding="utf-8", newline="").read()
    detail = json.loads(raw)
    row = _row(slug)
    pairs, _toks = number_pool(detail, row)
    return {"slug": slug, "name": detail.get("name") or row.get("name"),
            "source_sha256": article_sha(raw),
            "pending": pending_titles(detail),
            "how_to_write": [
                "この機種の記事と機種行にもう載っている事実だけを使って、未確認の欄を書く（外の出典は見ない）。",
                "数値は「書いてよい数値」に同じ単位で在るものだけ（機械が断る）。",
                "です・ます で書く。打ち方をすすめる言い方（積極的に・しましょう・おすすめ 等）は使わない。",
                "サイト名や「解析では」「出典」など、どこから採ったかが分かる言い方は書かない。",
                "書けるだけの事実が無い欄は書かない（--tried <slug> --result NOT_ENOUGH）。"],
            "allowed_numbers": sorted(f"{t}{u}" for t, u in pairs),
            "article": detail,
            "machine_row": {k: v for k, v in row.items() if k not in _SKIP_ROW_KEYS}}


# ---------------------------------------------------------------- 自己試験
def selftest() -> int:
    global _IN_SELFTEST
    _IN_SELFTEST = True
    ok = [0, 0]

    def t(name, cond):
        ok[0 if cond else 1] += 1
        print(("✅ " if cond else "❌ ") + name)

    import build_new_article as _ba
    P = _ba.PENDING_TEXT
    row = {"slug": "zzz_d", "name": "試験機D", "publication_policy": "page-decision/v2",
           "checker": {"normal": {"good": 630, "ceiling": 850}},
           "identity": {"dmm_id": 5999}}
    detail = {"name": "試験機D", "sections": [
        {"title": "天井・恩恵", "body": ["通常時の天井は850G+αで、到達するとATに当選します。"]},
        {"title": "当サイトの狙い目", "body": ["5.6枚交換の狙い目は、通常時630G〜です。"]},
        {"title": "狙い目の根拠", "body": [P]},
        {"title": "ヤメ時の判断", "body": [P]},
        {"title": "立ち回りのコツ", "body": [P]}]}
    good = {"slug": "zzz_d", "by": ["claude", "codex"], "why": "天井と狙い目の記載だけから書いた",
            "sections": [{"title": "狙い目の根拠",
                          "body": ["天井が850G+αなので、5.6枚交換では630Gからを当サイトの目安にしています。"]}]}
    r = plan(detail, row, json.loads(json.dumps(good)))
    t("★★記事に載っている数値だけで書いた文は通り、その欄だけが変わる★★",
      not r["problems"] and r["sections"][2]["body"] == good["sections"][0]["body"]
      and r["sections"][3]["body"] == [P] and r["sections"][0] == detail["sections"][0])
    t("　書いたあとは「未確認」の欄が2つに減る",
      not r["problems"] and pending_titles({"sections": r["sections"]}) == ["ヤメ時の判断", "立ち回りのコツ"])

    def bad(mut, why, must, rw=None):
        d = json.loads(json.dumps(good))
        mut(d)
        try:
            rr = plan(detail, rw or row, d)
        except Exception as e:                                   # noqa: BLE001  ★落ちたら不合格★
            rr = {"problems": [f"例外 {type(e).__name__}"], "sections": None}
        t(why, bool(rr["problems"]) and any(must in x for x in rr["problems"]))

    bad(lambda d: d["sections"][0].__setitem__("body", ["天井が999G+αなので、630Gからを当サイトの目安にしています。"]),
        "★★記事に無い数値は断る（数字を作らない）★★", "記事に載っていない数値")
    bad(lambda d: d["sections"][0].__setitem__("body", ["天井が850枚なので、630Gからを当サイトの目安にしています。"]),
        "★単位が違えば別の数値として断る（850G と 850枚）★", "記事に載っていない数値")
    bad(lambda d: d["sections"][0].__setitem__("body", ["機種IDの5999を手がかりに、630Gからを当サイトの目安にしています。"]),
        "★機種行の identity の数字は「載っている事実」に数えない★", "記事に載っていない数値")
    bad(lambda d: d.update(by=["claude"]), "　判断者が1人なら断る", "判断者の組")
    bad(lambda d: d.update(why="短い"), "　理由が短ければ断る", "理由")
    bad(lambda d: d["sections"][0].update(title="天井・恩恵"), "★書けるのは3つの欄だけ★", "書ける欄は")
    bad(lambda d: d["sections"].append(json.loads(json.dumps(d["sections"][0]))),
        "　同じ欄を2回書けない", "書ける欄は")
    bad(lambda d: d["sections"][0].__setitem__("body", ["<b>天井850G</b>なので630Gからを目安にしています。"]),
        "　タグは使えない", "タグ")
    bad(lambda d: d["sections"][0].__setitem__("body", ["スロパチクエストによると630Gからを目安にしています。"]),
        "★サイト名は書けない★", "サイト名")
    bad(lambda d: d["sections"][0].__setitem__("body", ["短い"]), "　段落が短すぎれば断る", "字で書きます")
    bad(lambda d: d["sections"][0].__setitem__("body", [P]), "★未確認の決まり文句を書いても埋めたことにしない★",
        "未確認の決まり文句")
    bad(lambda d: None, "★★新台経路でない機種には書かない★★", "新台経路の機種ではありません",
        rw={"slug": "zzz_d", "name": "試験機D"})
    # ★数値の入れ物★
    pairs, toks = number_pool(detail, row)
    t("★この3欄そのものの数値は「載っている事実」に数えない（書いた文で次の文を通さない）★",
      ("850", "G") in pairs and "5999" not in toks)
    # ★割り当て★
    tried = {"a": {"at": "2026-11-09", "result": "WRITTEN"},
             "b": {"at": "2026-11-09", "result": "NOT_ENOUGH"},
             "c": {"at": "2026-11-10", "result": "WRITTEN"},
             "d": {"at": "2026-11-01", "result": "UNDECIDED"}}
    t("★★一度も試していない機種→古い順。今日試した機種と、断って3日たたない機種は外す★★",
      pick(5, ["a", "b", "c", "d", "e"], tried, "2026-11-10") == ["e", "d", "a"])
    t("　数を守る", pick(1, ["a", "e"], tried, "2026-11-10") == ["e"])
    t("★だれも試していなければ番人に知らせる★", stall_problem(["a"], {}, "2026-11-10") is not None)
    t("★3日だれも試していなければ知らせる★",
      stall_problem(["a"], {"a": {"at": "2026-11-07"}}, "2026-11-10") is not None)
    t("　（対照）2日なら知らせない", stall_problem(["a"], {"a": {"at": "2026-11-08"}}, "2026-11-10") is None)
    t("　未確認が残っていなければ知らせない", stall_problem([], {}, "2026-11-10") is None)
    t("　未確認の欄の見分けは決まり文句だけ（中身がある欄は未確認ではない）",
      is_pending({"body": [P]}) and not is_pending({"body": ["中身"]}) and not is_pending({"body": [P, "x"]}))
    # ★本物の書き込み（写しの上で）★
    g = globals()
    keep = {k: g[k] for k in ("DETAILS", "MACHINES", "BASIS", "QUEUE", "rebuild_page")}
    tmp = tempfile.mkdtemp(prefix="derived_")
    try:
        g["DETAILS"] = os.path.join(tmp, "details")
        g["MACHINES"] = os.path.join(tmp, "machines.json")
        g["BASIS"] = os.path.join(tmp, "basis.json")
        g["QUEUE"] = os.path.join(tmp, "queue.json")
        os.makedirs(g["DETAILS"])
        dp = os.path.join(g["DETAILS"], "zzz_d.json")
        raw = json.dumps(detail, ensure_ascii=False, indent=2) + "\n"
        io.open(dp, "w", encoding="utf-8", newline="").write(raw)
        io.open(g["MACHINES"], "w", encoding="utf-8").write(json.dumps([row], ensure_ascii=False))
        dec = json.loads(json.dumps(good))
        dec["source_sha256"] = article_sha(raw)
        df = os.path.join(tmp, "d.json")
        io.open(df, "w", encoding="utf-8").write(json.dumps(dec, ensure_ascii=False))
        g["rebuild_page"] = lambda s: False
        ra = apply_decision(df, True)
        t("★★ページを作り直せなければ、記事を元に戻す★★",
          not ra.get("written") and io.open(dp, encoding="utf-8", newline="").read() == raw)
        g["rebuild_page"] = lambda s: True
        dec2 = dict(dec, source_sha256="0" * 64)
        io.open(df, "w", encoding="utf-8").write(json.dumps(dec2, ensure_ascii=False))
        rs = apply_decision(df, True)
        t("★判断したときと記事が変わっていたら書かない★",
          not rs.get("written") and io.open(dp, encoding="utf-8", newline="").read() == raw)
        io.open(df, "w", encoding="utf-8").write(json.dumps(dec, ensure_ascii=False))
        rd = apply_decision(df, False)
        t("　下見では書かない", not rd["problems"] and io.open(dp, encoding="utf-8", newline="").read() == raw)
        rb = apply_decision(df, True)
        now = json.loads(io.open(dp, encoding="utf-8").read())
        t("★★作り直せたら書き、控えと割り当ての記録を残す★★",
          rb.get("written") and now["sections"][2]["body"] == good["sections"][0]["body"]
          and json.loads(io.open(g["BASIS"], encoding="utf-8").read())[-1]["slug"] == "zzz_d"
          and json.loads(io.open(g["QUEUE"], encoding="utf-8").read())["tried"]["zzz_d"]["result"] == "WRITTEN")
    finally:
        g.update(keep)
    # ★育成が書いた欄を持ち越すか（grow_machine の配線）★
    import inspect
    import grow_machine as _gm
    t("★★毎朝の育成が、書いた欄を「未確認」に戻さない（持ち越しが配線されている）★★",
      "carry_written_sections(old_detail, detail, machine)" in inspect.getsource(_gm.plan_one))
    print(f"{ok[0]}/{sum(ok)} 合格")
    return 0 if ok[1] == 0 else 1


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--next", type=int)
    ap.add_argument("--gather")
    ap.add_argument("--apply")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tried")
    ap.add_argument("--result", choices=RESULTS)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                                            # noqa: BLE001
        pass
    if a.selftest:
        return selftest()
    today = _dt.date.today().isoformat()
    if a.apply:
        r = apply_decision(a.apply, not a.dry_run)
        if r.get("problems"):
            print("書きませんでした:")
            for x in r["problems"]:
                print("  - " + x)
            return 1
        print("下見: 通ります（書いていません）" if a.dry_run else "書きました（記事・ページ）")
        return 0
    if a.gather:
        if not _SLUG.match(a.gather):
            print("slug が不正です")
            return 1
        print(json.dumps(gather(a.gather), ensure_ascii=False, indent=1))
        return 0
    if a.tried:
        if not a.result or not _SLUG.match(a.tried):
            print("--tried には --result（NOT_ENOUGH か UNDECIDED）が要ります")
            return 1
        q = _read_json_or(QUEUE, {"tried": {}}, dict)
        q.setdefault("tried", {})[a.tried] = {"at": today, "result": a.result}
        _write_atomic(QUEUE, json.dumps(q, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
        _log(f"{a.tried}: {a.result}")
        print(f"{a.tried}: {a.result} を記録しました（{RETRY_DAYS}日あけて、また対象になります）")
        return 0
    try:
        rows, details = _load_all()
        q = _read_json_or(QUEUE, {"tried": {}}, dict)
    except Exception as e:                                       # noqa: BLE001
        print(f"★読めません: {type(e).__name__}: {e}★")
        return 2
    cands = candidates(rows, details)
    if a.next is not None:
        got = pick(a.next, cands, q.get("tried") or {}, today)
        for s in got:
            print(f"{s}: 未確認＝{'・'.join(pending_titles(details[s]))}")
        if not got:
            print("今日書く機種はありません")
        return 0
    if a.check:
        pr = stall_problem(cands, q.get("tried") or {}, today)
        print(pr or f"進んでいます（未確認の欄が残っている新台 {len(cands)}機種）")
        return 1 if pr else 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

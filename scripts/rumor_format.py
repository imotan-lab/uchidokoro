# -*- coding: utf-8 -*-
"""★噂の欄の「書き方の決まり」を1か所に置く★（2026-11-05新設）

★運営者の指示（2026-11-05）★
  ＞ 例えば噂を公式が否定したとかね。
  ＞ その場合は噂を書いて横線で消して〇年〇月〇日メーカー公式Xで否定 とか書いて欲しい

★表し方★＝記事データ（machine-details）の噂の欄（type=rumor）の body に、文字列のまま
    ~~<噂の文>~~（2026年11月1日 メーカー公式Xで否定）
  と書く。描画（machine.html の md() と build_machine_pages.md()）が `~~x~~` を `<del>x</del>` にする。
★文字列のままにする理由★＝body を読む道具（文体の見張り・重複の候補・危ない表現・
  再検査の text_gone・decide_now）が全部「文字列の並び」を前提にしている。辞書を混ぜると読み落としが出る。

★機械が見るのは形だけ★（鉄則0z の③「決まった形で書く」）＝
  ①`~~` が出てよいのは噂の欄の要素だけ（ほかの欄・lead・表・要約には出さない）
  ②`~~` を含む要素は、まるごと否定の形だけ（日付は実在する日・未来でない・名前に「公式」）
  ③取り消す本文の中に `~~` や否定の注記を重ねない
★噂が本当かどうか・否定が本当かどうかは2AIの判断★（ここでは見ない）。

CLI: `python scripts/rumor_format.py --check`（全機種）／`--selftest`
"""
import datetime as _dt
import glob
import io
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DETAILS = os.path.join(BASE, "assets", "data", "machine-details")

STRIKE = "~~"
# ★否定の注記の形★＝（YYYY年M月D日 <名前>で否定）。名前は2〜20字・括弧と ~~ を含まない
_NOTE = r"（(?P<y>\d{4})年(?P<m>\d{1,2})月(?P<d>\d{1,2})日 (?P<by>[^（）()~\s][^（）()~]{0,18}[^（）()~\s])で否定）"
DENIED_RE = re.compile(r"^~~(?P<body>.+?)~~" + _NOTE + r"$", re.S)
BY_MIN, BY_MAX = 2, 20


def jst_today() -> _dt.date:
    return (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=9)).date()


def denial_note(date_iso: str, by: str, today=None) -> str:
    """否定の注記を作る（★作る場所はここだけ★）。形が外れていれば ValueError。"""
    d = parse_date(date_iso, today=today)
    p = by_problem(by)
    if p:
        raise ValueError(p)
    return f"（{d.year}年{d.month}月{d.day}日 {by}で否定）"


def denied_line(text: str, date_iso: str, by: str, today=None) -> str:
    """噂の文を取り消し線つきの行にする。"""
    t = str(text or "").strip()
    if not t or STRIKE in t or DENIED_RE.match(t):
        raise ValueError("取り消す文が空か、すでに取り消し線を含んでいます")
    line = f"{STRIKE}{t}{STRIKE}{denial_note(date_iso, by, today=today)}"
    p = line_problem(line, in_rumor=True, today=today)
    if p:
        raise ValueError(p)
    return line


def parse_date(date_iso: str, today=None) -> _dt.date:
    s = str(date_iso or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        raise ValueError(f"日付は YYYY-MM-DD で書いてください: {s[:20]}")
    d = _dt.date.fromisoformat(s)
    if d > (today or jst_today()):
        raise ValueError(f"未来の日付は書けません: {s}")
    if d.year < 2000:
        raise ValueError(f"日付が古すぎます: {s}")
    return d


def by_problem(by) -> str:
    b = str(by or "")
    if not (BY_MIN <= len(b) <= BY_MAX):
        return f"否定した側の名前は{BY_MIN}〜{BY_MAX}字で書いてください"
    if "公式" not in b:
        return "否定した側の名前に「公式」を含めてください（例: メーカー公式X）＝取り消し線は公式の否定だけに使う"
    if re.search(r"[（）()~\s<>&\"'`]", b):
        return "否定した側の名前に括弧・空白・~・< > & 引用符は使えません"
    try:
        import gates as _g
        for f in (_g.invisible_unsafe, _g.html_unsafe):
            if f(b):
                return f"否定した側の名前に使えない文字があります: {f(b)}"
    except ImportError:
        return "文字の検査を読み込めないので通しません"
    return ""


def line_problem(line, in_rumor: bool, today=None) -> str:
    """1行が決まりどおりか（★判定はここだけ★）。空文字なら問題なし。"""
    if not isinstance(line, str) or STRIKE not in line:
        return ""
    if not in_rumor:
        return "取り消し線（~~）は噂の欄だけに使えます"
    m = DENIED_RE.match(line)
    if not m:
        return "取り消し線の行は「~~噂の文~~（YYYY年M月D日 〇〇公式〇〇で否定）」の形だけです"
    body = m.group("body")
    if STRIKE in body or not body.strip() or "~" in body[:1] + body[-1:]:
        return "取り消す本文の中に ~~ を重ねられません"
    y, mo, da, by = int(m.group("y")), int(m.group("m")), int(m.group("d")), m.group("by")
    try:
        d = _dt.date(y, mo, da)
    except ValueError:
        return f"実在しない日付です: {y}年{mo}月{da}日"
    try:
        parse_date(d.isoformat(), today=today)
    except ValueError as e:
        return str(e)
    p = by_problem(by)
    if p:
        return p
    return ""


def is_denied(line) -> bool:
    """★否定の印が付いた行か★（読むのはここだけ）"""
    return isinstance(line, str) and bool(DENIED_RE.match(line))


def denied_body(line) -> str:
    """否定の印が付いた行の、取り消された本文（印が無ければ空文字）"""
    m = DENIED_RE.match(line) if isinstance(line, str) else None
    return m.group("body") if m else ""


def denied_lines(detail) -> list:
    """記事データの噂の欄にある、否定の印が付いた行を全部"""
    out = []
    for s in (detail or {}).get("sections") or []:
        if isinstance(s, dict) and s.get("type") == "rumor":
            out += [x for x in (s.get("body") or []) if is_denied(x)]
    return out


def touches_denied(detail, strings) -> str:
    """★否定の印が付いた行に、ほかの道具（decide_now）が触ろうとしていないか★

    （2026-11-05・Codex review289 の指摘）＝言い換えで取り消し線だけを外すと、
    公式が否定した噂が「未確認の噂」に戻って読者に出る。形の検査では気づけない。
    ★否定の印の行は rumor_verdict だけが扱う★＝ほかの道具の before / text が
    `~~` を含むか、否定の行の一部に当たるなら断る（安全側＝同じ文字が別の所に
    あっても断る）。
    """
    lines = denied_lines(detail)
    for t in strings:
        if not isinstance(t, str) or not t:
            continue
        if STRIKE in t:
            return "取り消し線（~~）の行は rumor_verdict.py で扱います"
        for ln in lines:
            if t in ln:
                return ("否定の印が付いた噂の行に当たります（rumor_verdict.py で扱います）: "
                        + t[:30])
    return ""


def _strings(x):
    """入れ子の中の文字列を全部（場所の名前つきで）挙げる。"""
    if isinstance(x, str):
        yield "", x
    elif isinstance(x, list):
        for i, v in enumerate(x):
            for w, s in _strings(v):
                yield f"[{i}]{w}", s
    elif isinstance(x, dict):
        for k, v in x.items():
            for w, s in _strings(v):
                yield f".{k}{w}", s


def detail_problems(detail, today=None) -> list:
    """1機種ぶんの記事データを見る。★噂の欄の body だけが ~~ を持てる★"""
    out = []
    if not isinstance(detail, dict):
        return ["記事データが辞書ではありません"]
    secs = detail.get("sections")
    rumor_body_ids = set()
    if isinstance(secs, list):
        for i, s in enumerate(secs):
            if isinstance(s, dict) and s.get("type") == "rumor" \
                    and isinstance(s.get("body"), list):
                rumor_body_ids.add(i)
                for j, t in enumerate(s["body"]):
                    p = line_problem(t, True, today=today)
                    if p:
                        out.append(f"sections[{i}].body[{j}]: {p}")
    # ★ほかの場所の ~~ は全部だめ★（噂の欄の body の外）
    for where, s in _strings(detail):
        if STRIKE not in s:
            continue
        m = re.match(r"^\.sections\[(\d+)\]\.body\[\d+\]$", where)
        if m and int(m.group(1)) in rumor_body_ids:
            continue
        out.append(f"{where.lstrip('.')}: 取り消し線（~~）は噂の欄だけに使えます")
    return out


def check_all(details_dir=None, today=None) -> list:
    out = []
    for p in sorted(glob.glob(os.path.join(details_dir or DETAILS, "*.json"))):
        slug = os.path.splitext(os.path.basename(p))[0]
        try:
            d = json.loads(io.open(p, encoding="utf-8").read())
        except Exception as e:                                   # noqa: BLE001
            out.append(f"{slug}: 読めません: {type(e).__name__}")
            continue
        out += [f"{slug}: {x}" for x in detail_problems(d, today=today)]
    return out


def selftest() -> int:
    ok = ng = 0

    def t(name, cond):
        nonlocal ok, ng
        if cond:
            ok += 1
        else:
            ng += 1
            print("NG", name)

    T = _dt.date(2026, 11, 5)
    good = "~~設定6は朝一に必ずフリーズするという噂がありました。~~（2026年11月1日 メーカー公式Xで否定）"
    t("良い形は通る", line_problem(good, True, today=T) == "")
    t("噂の欄の外は止める", line_problem(good, False, today=T) != "")
    t("~~の無い行は見ない", line_problem("ふつうの噂です。", True, today=T) == "")
    t("注記の無い取り消しは止める",
      line_problem("~~噂です。~~", True, today=T) != "")
    t("途中だけの取り消しは止める",
      line_problem("前の文です。~~噂です。~~（2026年11月1日 メーカー公式Xで否定）", True, today=T) != "")
    t("後ろに文が続くのは止める",
      line_problem(good + "続きです。", True, today=T) != "")
    t("未来の日付は止める",
      line_problem("~~噂です。~~（2026年11月6日 メーカー公式Xで否定）", True, today=T) != "")
    t("実在しない日付は止める",
      line_problem("~~噂です。~~（2026年2月30日 メーカー公式Xで否定）", True, today=T) != "")
    t("公式でない否定は止める",
      line_problem("~~噂です。~~（2026年11月1日 有名ライターで否定）", True, today=T) != "")
    t("重ねた取り消しは止める",
      line_problem("~~~~噂です。~~~~（2026年11月1日 メーカー公式Xで否定）", True, today=T) != "")
    t("当日は通る",
      line_problem("~~噂です。~~（2026年11月5日 メーカー公式Xで否定）", True, today=T) == "")
    # 作る側と見る側が同じ形
    import unittest.mock as _m
    with _m.patch(__name__ + ".jst_today", return_value=T):
        made = denied_line("噂の文です。", "2026-11-01", "メーカー公式X")
    t("作った行は見る側を通る", line_problem(made, True, today=T) == ""
      and made == "~~噂の文です。~~（2026年11月1日 メーカー公式Xで否定）")
    for bad in ("", "~~重ね~~", made):
        try:
            denied_line(bad, "2026-11-01", "メーカー公式X")
            t(f"作る側が断る: {bad[:8]}", False)
        except ValueError:
            t(f"作る側が断る: {bad[:8]}", True)
    for bad_by in ("X", "ライター", "メーカー公式 X", "公式(X)"):
        try:
            denial_note("2026-11-01", bad_by)
            t(f"名前を断る: {bad_by}", False)
        except ValueError:
            t(f"名前を断る: {bad_by}", True)
    # 記事データの中の場所
    d_ok = {"sections": [{"title": "天井・恩恵", "body": ["天井は999Gです。"]},
                         {"title": "噂・未確定情報", "type": "rumor", "body": ["断り書きです。", good]}]}
    t("噂の欄の body は通る", detail_problems(d_ok, today=T) == [])
    d_ng = {"sections": [{"title": "天井・恩恵", "body": [good]}]}
    t("ほかの欄は止める", len(detail_problems(d_ng, today=T)) == 1)
    d_ng2 = {"lead": "~~x~~", "sections": [{"title": "噂", "type": "rumor", "body": ["a"]}]}
    t("lead は止める", any("lead" in x for x in detail_problems(d_ng2, today=T)))
    d_ng3 = {"sections": [{"title": "噂", "type": "rumor", "body": ["a"],
                           "tables": [{"rows": [[good]]}]}]}
    t("噂の欄の表は止める", any("tables" in x for x in detail_problems(d_ng3, today=T)))
    d_ng4 = {"sections": [{"title": "噂", "type": "rumor", "body": ["~~噂~~"]}]}
    t("噂の欄でも形が外れたら止める", len(detail_problems(d_ng4, today=T)) == 1)
    d_ng5 = {"sections": [{"title": "噂", "body": [good]}]}
    t("type が rumor でない欄は止める", len(detail_problems(d_ng5, today=T)) == 1)
    print(f"{ok}/{ok + ng} 合格")
    return 0 if ng == 0 else 1


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:                                        # noqa: BLE001
            pass
    args = sys.argv[1:]
    if "--selftest" in args:
        return selftest()
    if "--check" in args:
        probs = check_all()
        for p in probs:
            print(p)
        print(f"噂の欄の書き方: 問題 {len(probs)} 件")
        return 1 if probs else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())

"""★担当が回ってきた機種を、全項目そろえて1機種ずつ仕上げる点検表★（2026-11-10・運営者の指示）

運営者の言葉：
  > 人気機種と新台優先で更新してるでしょ？ それにあわせてその機種のポチポチくんとかゾーンとか
  > その他諸々最新にしたい。その機種を一つ一つ完璧に直す感じで。
  > 他にも全項目更新対象だからね。
  > 無い項目ももしも見つかったら付け足すんだよ？ 大手サイトと比較して無いものはこっちのサイトにも書きたい

★なぜ要るか★＝項目ごとに別々の順番（ポチポチくん90日・モード60日・天井の一覧…）で回していて、
人気機種でも項目によっては何か月も見られなかった。担当が回ってきた機種（朝の STEP 1：新台→人気→その他）は、
★その日に全項目を確かめ直す★。項目ごとの間隔は、担当が回ってこなかったときの保険としてだけ残す。

使い方（朝の更新タスク STEP 1-T）：
  python scripts/machine_turn.py --start <slug>            担当を始める（その機種の項目の記録を今日から数え直す）
  python scripts/machine_turn.py --plan <slug>             その機種でやる項目と、今回の結果を並べる
  python scripts/machine_turn.py --done <slug> --item <項目> --result OK|FIXED|ADDED|ASKED|NA|LATER [--note-file F]
  python scripts/machine_turn.py --pending                 全項目そろっていない担当（翌日まず仕上げる）
  python scripts/machine_turn.py --check                   見張り番：前の日までに始めて、そろっていない担当（1＝🟡／2＝読めない）
  python scripts/machine_turn.py --selftest

結果の意味：OK＝確かめて変わりなし／FIXED＝直した／ADDED＝足りない項目を足した／ASKED＝運営者へ回した（owner_questions）／
  NA＝この機種には当てはまらない（理由を --note-file に）／LATER＝締切・利用制限で翌日に回す（理由を --note-file に）。
★LATER と記録の無い項目が残る担当は「そろっていない」★＝翌朝の STEP 1-T が新しい機種より先に仕上げる。
★どの項目をどう直すかは2AI★（このファイルは並べて記録するだけ）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import local_paths as _lp            # noqa: E402

BASE = os.path.dirname(HERE)
MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
STORE = os.path.join(_lp.DOCS, "machine_turn.json")
RESULTS = ("OK", "FIXED", "ADDED", "ASKED", "NA", "LATER")
NEED_NOTE = ("NA", "LATER")
CHECK_DAYS = 7
_IN_SELFTEST = False

# ★項目の一覧★（キー → 何をするか・手順書のどの段の流れで）。並びは記事の上から下＋道具の順
COMMON_ITEMS = (
    ("article", "記事の中身（本文・表・要約・リード・基本情報表・文体）を読んで直す", "STEP 1-Q"),
    ("ceiling", "天井・恩恵を出典の原文で確かめ直す（種類・G数・数え方・恩恵・短縮）", "STEP 1-C の流れ"),
    ("checker", "狙い目チェッカーの線（線の無い欄・交換率ごと・回数の行・注記・天井）", "checker_verdict --slug"),
    ("mode", "モード・ゾーンの欄（間隔に関係なく今回確かめる）", "STEP 1-M の流れ"),
    ("pochipochi", "ポチポチくん（対応・小役の差・手書きの表・設定差なし）", "STEP 1-P（--status --turn）"),
    ("rumor", "噂・未確定情報の欄（新しい噂・公式の否定）", "STEP 1-R"),
    ("sources", "出典の控え（隔離・題の変化・本人性）", "machine_sources --check --slug"),
    ("gap", "★大手サイトにあって、この記事に無い項目を足す★（数値は独立2出典で確かめたものだけ）", "STEP 1-G（add_items）"),
)
LEGACY_ITEMS = (
    ("binding", "DMMの機種ページとの結び付け（無ければ2AIで結ぶ）", "legacy_binding --bind"),
    ("release", "導入日（DMMの機種ページの値）", "legacy_release --refresh 1 --slug --apply"),
    ("hall", "全国の設置店舗数（ランキングから外すかの数）", "hall_presence --refresh 1 --slug --apply"),
    ("tenjo_rank", "天井ランキングの値と書き方（tenjo_games・tenjo_display）", "tenjo_verdict"),
)
AUTO_ITEMS = (
    ("derived", "狙い目の根拠・ヤメ時の判断・立ち回りのコツ（記事の事実から2AIが書く）", "STEP 0-K の流れ"),
    ("meta", "通称（検索の別名）とタイプ", "STEP 0-L の流れ"),
    ("grow_questions", "育成が出した問い（空欄・型・天井・線）に答える", "STEP 0 / 0-Q"),
)


def _log(msg: str) -> None:
    if _IN_SELFTEST:
        return
    try:
        os.makedirs(_lp.LOGS, exist_ok=True)
        p = os.path.join(_lp.LOGS, "machine_turn_%s.log" % _dt.date.today().isoformat())
        with io.open(p, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (_dt.datetime.now().strftime("%H:%M:%S"), msg))
    except OSError:
        pass


class TurnError(RuntimeError):
    pass


def read(path: str = None) -> dict:
    """★無いは空・壊れていたら例外★（黙って0件と読まない）"""
    p = path or STORE
    if not os.path.isfile(p):
        return {"turns": {}}
    with io.open(p, encoding="utf-8") as f:
        d = json.load(f)
    if not isinstance(d, dict) or not isinstance(d.get("turns"), dict):
        raise TurnError("machine_turn.json の形が違います")
    for s, tr in d["turns"].items():
        if not isinstance(tr, dict) or not isinstance(tr.get("items"), dict) or not tr.get("started"):
            raise TurnError(f"machine_turn.json の {s} が壊れています")
        for k, v in tr["items"].items():
            if not isinstance(v, dict) or v.get("result") not in RESULTS:
                raise TurnError(f"machine_turn.json の {s}/{k} が壊れています")
    return d


def _save(d: dict, path: str = None) -> None:
    p = path or STORE
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(d, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, p)


def _rows():
    import safe_json as _sj
    return _sj.read_rows(MACHINES)


def items_for(row: dict) -> list:
    """★その機種でやる項目★（新台経路か昔からの機種かで変わる）"""
    import page_decision as _pd
    out = list(COMMON_ITEMS)
    out += list(AUTO_ITEMS if _pd.is_auto(row or {}) else LEGACY_ITEMS)
    return out


def start(slug: str, today: str = None, path: str = None) -> dict:
    today = today or _dt.date.today().isoformat()
    d = read(path)
    d["turns"][slug] = {"started": today, "items": {}}
    _save(d, path)
    _log(f"{slug}: 担当を始めました")
    return d["turns"][slug]


def done(slug: str, item: str, result: str, note: str = "", rows=None, today: str = None,
         path: str = None) -> dict:
    today = today or _dt.date.today().isoformat()
    if result not in RESULTS:
        raise TurnError(f"結果は {'/'.join(RESULTS)} のどれかです")
    if result in NEED_NOTE and len(str(note or "").strip()) < 10:
        raise TurnError(f"{result} は理由（--note-file・10字以上）が要ります")
    rows = _rows() if rows is None else rows
    row = next((m for m in rows if isinstance(m, dict) and m.get("slug") == slug), None)
    if row is None:
        raise TurnError(f"{slug} は機種一覧にありません")
    keys = [k for k, _w, _s in items_for(row)]
    if item not in keys:
        raise TurnError(f"{slug} の項目は {'/'.join(keys)} です（{item} はありません）")
    d = read(path)
    tr = d["turns"].get(slug)
    if tr is None:
        raise TurnError(f"{slug} の担当が始まっていません（先に --start）")
    tr["items"][item] = {"at": today, "result": result, "note": str(note or "").strip()[:300]}
    _save(d, path)
    _log(f"{slug}: {item} = {result}" + (f"（{str(note).strip()[:120]}）" if note else ""))
    return tr


def missing(slug: str, tr: dict, rows) -> list:
    """★まだ記録が無いか LATER の項目★"""
    row = next((m for m in rows if isinstance(m, dict) and m.get("slug") == slug), None)
    if row is None:
        return []
    return [k for k, _w, _s in items_for(row)
            if (tr.get("items") or {}).get(k, {}).get("result") in (None, "LATER")]


def pending(rows=None, path: str = None) -> list:
    """★全項目そろっていない担当★（始めた日が古い順）"""
    rows = _rows() if rows is None else rows
    d = read(path)
    out = []
    for s, tr in d["turns"].items():
        left = missing(s, tr, rows)
        if left:
            out.append((tr["started"], s, left))
    return [(s, left) for _st, s, left in sorted(out)]


def check_problem(today: str = None, rows=None, path: str = None):
    """★見張り番：前の日までに始めたのに、そろっていない担当（CHECK_DAYS 日以内）★"""
    today = today or _dt.date.today().isoformat()
    rows = _rows() if rows is None else rows
    d = read(path)
    cut = (_dt.date.fromisoformat(today) - _dt.timedelta(days=CHECK_DAYS)).isoformat()
    bad = []
    for s, tr in d["turns"].items():
        if cut <= tr["started"] < today:
            left = missing(s, tr, rows)
            if left:
                bad.append(f"{s}（{'・'.join(left)}）")
    if bad:
        return f"担当したのに全項目そろっていない機種が{len(bad)}件あります: " + " ／ ".join(bad[:8])
    return None


def plan_text(slug: str, rows=None, path: str = None) -> str:
    rows = _rows() if rows is None else rows
    row = next((m for m in rows if isinstance(m, dict) and m.get("slug") == slug), None)
    if row is None:
        raise TurnError(f"{slug} は機種一覧にありません")
    tr = read(path)["turns"].get(slug) or {"started": "", "items": {}}
    lines = [f"{slug}（{row.get('name')}）担当開始: {tr['started'] or '（まだ --start していません）'}"]
    for k, what, step in items_for(row):
        got = tr["items"].get(k)
        mark = f"{got['result']}（{got['at']}）" if got else "未"
        lines.append(f"  [{mark}] {k}: {what} ／ {step}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 自己試験
def selftest() -> int:
    global _IN_SELFTEST
    _IN_SELFTEST = True
    import tempfile
    ok = [0, 0]

    def t(name, cond):
        ok[0 if cond else 1] += 1
        print(("✅ " if cond else "❌ ") + name)

    p = os.path.join(tempfile.mkdtemp(prefix="mturn_"), "t.json")
    rows = [{"slug": "zzz_l", "name": "昔の機種"},
            {"slug": "zzz_a", "name": "新台", "publication_policy": "page-decision/v2"}]
    kl = [k for k, _w, _s in items_for(rows[0])]
    ka = [k for k, _w, _s in items_for(rows[1])]
    t("★★昔の機種は共通＋導入日・結び付け・設置店舗数・天井ランキング、新台は共通＋根拠3欄・通称・育成の問い★★",
      "pochipochi" in kl and "mode" in kl and "gap" in kl and "release" in kl and "derived" not in kl
      and "derived" in ka and "meta" in ka and "release" not in ka)
    try:
        done("zzz_l", "article", "OK", rows=rows, today="2026-11-10", path=p)
        _no_start = False
    except TurnError:
        _no_start = True
    t("　担当を始めていなければ記録できない", _no_start)
    start("zzz_l", today="2026-11-10", path=p)
    for k in kl:
        if k != "mode":
            done("zzz_l", k, "OK", rows=rows, today="2026-11-10", path=p)
    t("★★1項目でも残っていれば「そろっていない」★★",
      pending(rows, p) == [("zzz_l", ["mode"])])
    t("　当日に始めた担当は見張り番は知らせない（朝のタスクがまだ動いている）",
      check_problem("2026-11-10", rows, p) is None)
    t("★★前の日に始めてそろっていない担当は、見張り番が知らせる★★",
      check_problem("2026-11-11", rows, p) is not None)
    try:
        done("zzz_l", "mode", "LATER", note="短い", rows=rows, today="2026-11-10", path=p)
        _short = False
    except TurnError:
        _short = True
    t("　LATER・NA は理由（10字以上）が要る", _short)
    done("zzz_l", "mode", "LATER", note="Codexが利用制限のため翌日に回す", rows=rows, today="2026-11-10", path=p)
    t("★LATER の項目は、まだそろっていない扱い★", pending(rows, p) == [("zzz_l", ["mode"])])
    done("zzz_l", "mode", "FIXED", rows=rows, today="2026-11-11", path=p)
    t("　全項目がそろえば、そろっていない担当から外れる", pending(rows, p) == []
      and check_problem("2026-11-12", rows, p) is None)
    try:
        done("zzz_l", "derived", "OK", rows=rows, today="2026-11-11", path=p)
        _wrong = False
    except TurnError:
        _wrong = True
    t("　その機種に無い項目は記録できない（昔の機種の derived）", _wrong)
    start("zzz_l", today="2026-11-17", path=p)
    t("★次に担当が回ってきたら、全項目を数え直す★", len(pending(rows, p)[0][1]) == len(kl))
    with io.open(p, "w", encoding="utf-8") as f:
        f.write('{"turns": {"zzz_l": {"started": "", "items": {}}}}')
    try:
        read(p)
        t("★控えが壊れていたら例外（黙って0件と読まない）★", False)
    except TurnError:
        t("★控えが壊れていたら例外（黙って0件と読まない）★", True)
    print(f"\n{ok[0]}/{sum(ok)} 合格")
    return 0 if ok[1] == 0 else 1


def main() -> int:
    for _st in (sys.stdout, sys.stderr):
        try:
            _st.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                        # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description="担当機種の全項目の点検表")
    ap.add_argument("--start")
    ap.add_argument("--plan")
    ap.add_argument("--done")
    ap.add_argument("--item")
    ap.add_argument("--result", choices=RESULTS)
    ap.add_argument("--note-file")
    ap.add_argument("--pending", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    try:
        if a.start:
            start(a.start)
            print(plan_text(a.start))
            return 0
        if a.plan:
            print(plan_text(a.plan))
            return 0
        if a.done:
            note = ""
            if a.note_file:
                with io.open(a.note_file, encoding="utf-8") as f:
                    note = f.read().strip()
            done(a.done, a.item, a.result, note)
            print(plan_text(a.done))
            return 0
        if a.pending:
            got = pending()
            for s, left in got:
                print(f"{s}: 残り {'・'.join(left)}")
            if not got:
                print("そろっていない担当はありません")
            return 0
        if a.check:
            pr = check_problem()
            print(("🟡 " + pr) if pr else "🟢 担当した機種は全項目そろっています")
            return 1 if pr else 0
    except (TurnError, ValueError, OSError) as e:
        print(f"🟠 点検表を扱えません: {e}")
        return 2
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())

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
  python scripts/machine_turn.py --done <slug> --item <項目> --result OK|FIXED|ADDED|ASKED|NA|LATER [--note-file F] [--codex-limited]
  python scripts/machine_turn.py --can-finish     # 朝のタスクが終わってよいか（0=よい／1=まだ続ける）
  python scripts/machine_turn.py --pending                 全項目そろっていない担当（翌日まず仕上げる）
  python scripts/machine_turn.py --check                   見張り番：前の日までに始めて、そろっていない担当（1＝🟡／2＝読めない）
  python scripts/machine_turn.py --selftest

結果の意味：OK＝確かめて変わりなし／FIXED＝直した／ADDED＝足りない項目を足した／ASKED＝運営者へ回した（owner_questions）／
  NA＝この機種には当てはまらない（理由を --note-file に）／LATER＝締切で翌日に回す（理由を --note-file に）。
  ★LATER は締切の45分前からしか受け付けない★（Codexの利用制限で天井を回すときだけ --codex-limited で前でも可）。
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
_IN_SELFTEST = False
# ★人気機種の「今週見終わった」印と突き合わせ始める週★（この週より前の印は点検表ができる前のもの）
TURN_SINCE_WEEK = "2026-11-16"
# ★「点検済み」の日付（state.json の quality_review.last_reviewed）と突き合わせ始める日★
TURN_SINCE_DAY = "2026-11-11"
# ★最後の点検からこの日数を超えた機種があれば見張り番が知らせる★（その他の一周は約35日・それより十分長く）
REVIEW_STALE_DAYS = 60
# ★点検済みの印を覆える点検表は、その日から何日前までに始めたものか★（LATER を翌朝に仕上げる分の余裕）
REVIEW_TURN_DAYS = 3
# ★★LATER（翌日へ回す）は締切のこの分数前からしか受け付けない★★（2026-11-12）
#   なぜ＝11/11の朝、6機種とも重い5項目を「締切（11:00）までに回し切れない」と見込んで LATER にし、9:36に終わった
#   （1時間半残っていた）。翌朝はその残りから片づけるので、新しい機種が減り、その他の一周が延びる。
#   ★見込みでは翌日へ回させない★＝締切が本当に近づいたときだけ。
LATER_WINDOW_MIN = 45
# ★朝のタスクの時間帯★（この時刻から締切の LATER_WINDOW_MIN 分前までは LATER と「終わってよいか」を断る）
#   ★これより前・締切の45分前を過ぎた後（対話セッションが午後に記録するなど）は断らない★
MORNING_FROM = "05:00"
UPDATE_TASK = "uchidokoro-update-machine"

# ★項目の一覧★（キー → 何をするか・手順書のどの段の流れで）。並びは記事の上から下＋道具の順
COMMON_ITEMS = (
    ("article", "記事の中身（本文・表・要約・リード・基本情報表・文体）を読んで直す（新台は育成の材料＝確定値で直す）", "STEP 1-Q"),
    ("ceiling", "天井・恩恵を出典の原文で確かめ直す（種類・G数・数え方・恩恵・短縮）", "STEP 1-C の流れ"),
    ("values", "天井以外の数値（機械割・純増・CZ・朝一リセット・設定示唆・ヤメ時）を出典の原文で確かめ直す",
     "collect_evidence --topic at,cz,reset,settei,yame → 2AI"),
    ("checker", "狙い目チェッカーの線（線の無い欄・交換率ごと・回数の行・注記・天井）", "checker_verdict --slug"),
    ("mode", "モード・ゾーンの欄（間隔に関係なく今回確かめる）", "STEP 1-M の流れ"),
    ("pochipochi", "ポチポチくん（対応・小役の差・手書きの表・設定差なし）", "STEP 1-P（--status --turn）"),
    ("sources", "出典の控え（同じページのままか・隔離・題の変化）", "machine_sources --recheck --slug"),
    ("gap", "★大手サイトにあって、この記事に無い項目を足す★（数値は独立2出典で確かめたものだけ）", "STEP 1-G（add_items）"),
    ("hall", "全国の設置店舗数（ランキングから外すかの数）", "hall_presence --refresh 1 --slug --apply（候補でなければ NA）"),
)
# ★噂は昔の機種だけ★＝運営者「新台は噂はそもそもない」（STEP 1-R の決まり）
LEGACY_ITEMS = (
    ("rumor", "噂・未確定情報の欄（新しい噂・公式の否定）", "STEP 1-R"),
    ("binding", "DMMの機種ページとの結び付け（無ければ2AIで結ぶ）", "legacy_binding --bind"),
    ("release", "導入日（DMMの機種ページの値・machines.json にある機種は食い違いを見る）",
     "legacy_release --refresh 1 --slug --apply"),
    ("facts", "基本情報表（導入日・メーカーなど大手にある行）", "STEP 1-G（add_items の add_fact）"),
    ("meta", "通称（検索の別名）とタイプ", "STEP 0-L の流れ（machine_meta・昔の機種も可）"),
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


def later_window(budget=None) -> tuple:
    """（LATER を受け付け始める時刻, 締切）。締切は task-budget.json の朝のタスクの締切"""
    import task_guard as _tg
    b = _tg.budget() if budget is None else budget
    dl = _tg.deadline_for(UPDATE_TASK, b)
    if not dl or len(dl) != 5 or dl[2] != ":":
        raise TurnError(f"朝のタスクの締切が読めません: {dl!r}")
    t = int(dl[:2]) * 60 + int(dl[3:]) - LATER_WINDOW_MIN
    return f"{t // 60:02d}:{t % 60:02d}", dl


def _in_work_hours(now_hhmm: str, budget=None) -> tuple:
    """（いまが朝のタスクの時間帯で、まだ LATER を受け付けない時刻か, 受け付け始める時刻, 締切）"""
    start, dl = later_window(budget)
    return (MORNING_FROM <= now_hhmm < start), start, dl


def _now_hhmm() -> str:
    return _dt.datetime.now().strftime("%H:%M")


def done(slug: str, item: str, result: str, note: str = "", rows=None, today: str = None,
         path: str = None, now_hhmm: str = None, codex_limited: bool = False, budget=None) -> dict:
    today = today or _dt.date.today().isoformat()
    if result not in RESULTS:
        raise TurnError(f"結果は {'/'.join(RESULTS)} のどれかです")
    if result in NEED_NOTE and len(str(note or "").strip()) < 10:
        raise TurnError(f"{result} は理由（--note-file・10字以上）が要ります")
    if codex_limited and not (result == "LATER" and item == "ceiling"):
        raise TurnError("Codexの利用制限で翌日へ回せるのは天井（ceiling）だけです（ほかは STEP 4-F のエージェント2体で続ける）")
    if result == "LATER" and not codex_limited:
        _early, _start, _dl = _in_work_hours(now_hhmm or _now_hhmm(), budget)
        if _early:
            raise TurnError(f"★締切（{_dl}）の{LATER_WINDOW_MIN}分前（{_start}）までは LATER にできません★"
                            "＝見込みで翌日へ回さず、この項目を続けてください")
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
    if codex_limited:
        tr["items"][item]["codex_limited"] = True
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


def finish_problem(today: str = None, now_hhmm: str = None, rows=None, path: str = None,
                   budget=None):
    """★★朝のタスクがまだ終わってはいけない理由★★（2026-11-12・終わってよければ None）

    締切の LATER_WINDOW_MIN 分前までに、そろっていない担当（記録の無い項目・LATER の項目）が残っていれば終われない。
    ★例外＝今日 Codexの利用制限で翌日へ回した天井★（その日はもう決められない）。
    """
    today = today or _dt.date.today().isoformat()
    _early, _start, _dl = _in_work_hours(now_hhmm or _now_hhmm(), budget)
    if not _early:
        return None
    rows = _rows() if rows is None else rows
    d = read(path)
    left = []
    for s, tr in sorted(d["turns"].items(), key=lambda kv: kv[1]["started"]):
        its = tr.get("items") or {}
        miss = [k for k in missing(s, tr, rows)
                if not (its.get(k, {}).get("codex_limited") and its.get(k, {}).get("at") == today)]
        if miss:
            left.append(f"{s}（{'・'.join(miss)}）")
    if not left:
        return None
    return (f"まだ {_start} 前です（締切 {_dl}）。そろっていない担当が{len(left)}件あります＝続けてください: "
            + " ／ ".join(left[:8]))


def popular_without_turn(today: str, done_this_week: list, week: str, path: str = None) -> list:
    """★人気機種を「今週見終わった」と印したのに、その週に点検表を始めていない機種★（2026-11-10・洗い出し4回目）

    なぜ＝点検表を一度も使わずに印だけ付けると、見張り番は「担当は0件」と読んで🟢を出していた。
    """
    if not week or week < TURN_SINCE_WEEK:
        return []
    d = read(path)
    out = []
    for s9 in done_this_week or []:
        tr = d["turns"].get(s9)
        if not tr or tr["started"] < week:
            out.append(s9)
    return out


def reviewed_without_turn(last_reviewed: dict, path: str = None) -> list:
    """★「点検済み」にしたのに、その日に点検表を始めていない機種★（2026-11-10・Codexの5回目の洗い出し）

    なぜ＝その他の機種は mark_reviewed で点検済みにでき、点検表を一度も使わなくても見張り番は🟢だった。
    """
    d = read(path)
    out = []
    for s9, day in (last_reviewed or {}).items():
        day = str(day or "")[:10]
        if day < TURN_SINCE_DAY:
            continue
        tr = d["turns"].get(s9)
        # ★★点検済みの日より後に始めた点検表があれば、その印は挙げない★★（2026-11-10・代役の指摘＝
        #   点検表は機種ごとに最新の1つしか残らないので、翌週の担当が始まると先週の印が「無い」と誤って出た）
        #   ＝下の条件に「始めた日が点検済みの日より後」を入れない
        # ★★点検済みの日の REVIEW_TURN_DAYS 日前より古い点検表では、その印を覆わない★★（代役の指摘＝
        #   ずっと前の点検表に項目の記録があるだけで、その後の点検済みの印が全部通っていた）
        lo = (_dt.date.fromisoformat(day) - _dt.timedelta(days=REVIEW_TURN_DAYS)).isoformat()
        if not tr or tr["started"] < lo or (tr["started"] < day and _stale_turn(tr, day)):
            out.append(s9)
    return sorted(out)


def _stale_turn(tr: dict, day: str) -> bool:
    """その点検表が、点検済みにした日より前に始めて、その日までに1項目も記録していないか"""
    return not any(str(v.get("at") or "") >= tr["started"] and str(v.get("at") or "") <= day
                   for v in (tr.get("items") or {}).values())


def stale_reviews(last_reviewed: dict, rows, today: str) -> list:
    """★最後の点検から REVIEW_STALE_DAYS 日を超えた機種（一覧にあるものだけ）★（2026-11-10・代役の指摘）

    点検済みの記録が無い昔の機種も含める（新台は育成が見るので、記録が無ければ数えない）。
    """
    lim = (_dt.date.fromisoformat(today) - _dt.timedelta(days=REVIEW_STALE_DAYS)).isoformat()
    import grow_machine as _gm
    out = []
    for m in rows or []:
        if not isinstance(m, dict) or not m.get("slug"):
            continue
        # ★新台期間の機種は数えない★（育成が毎日見ていて、その他の巡回には入らない）
        if "publication_policy" in m and (_gm.is_new_machine(m.get("release_date"), _dt.date.fromisoformat(today))
                                          or str(m.get("release_date") or "")[:len(today)] > today):
            continue                     # ★導入前の新台も数えない★（代役の指摘＝導入日まで🟡が鳴り続けた）
        last = str((last_reviewed or {}).get(m["slug"]) or "")[:10]
        # ★新台期間を過ぎた新台も、点検記録が無ければ数える★（Codexの指摘＝dmm_5089 が永久に対象外だった）
        _rel = str(m.get("release_date") or "")
        if (last and last < lim) or (not last and ("publication_policy" not in m or bool(_rel))):
            out.append(m["slug"])
    return out


class _NotProduction(Exception):
    """試験の点検表を見ているので、本番の控えは読まない"""


def check_problem(today: str = None, rows=None, path: str = None):
    """★見張り番：前の日までに始めたのに、そろっていない担当★

    ★古い担当も外さない★（2026-11-10・Codexの指摘＝直す前は7日より前に始めた担当を見ず、そろっていないまま正常に見えた）。
    次に担当が回ってきて --start し直すと数え直される。
    """
    today = today or _dt.date.today().isoformat()
    rows = _rows() if rows is None else rows
    d = read(path)
    bad = []
    for s, tr in d["turns"].items():
        if tr["started"] < today:
            left = missing(s, tr, rows)
            if left:
                bad.append(f"{s}（{'・'.join(left)}）")
    msgs = []
    if bad:
        msgs.append(f"担当したのに全項目そろっていない機種が{len(bad)}件あります: " + " ／ ".join(bad[:8]))
    # ★★本番の控え（人気機種の今週の記録・点検済みの記録）を読むのは、本番の点検表を見るときだけ★★（2026-11-12）
    #   なぜ＝試験の点検表に本番の点検済みの記録を突き合わせ、11/11以降の本物の記録のせいで手元の自己試験が赤になっていた
    #   （CIには控えが無いので緑＝手元でだけ落ちる）。stale_reviews と同じ決まりにそろえる。
    try:
        if path is not None:
            raise _NotProduction()
        import popular_machines as _pm
        st = _pm.load_store()
        rot = st.get("rotation") or {}
        wk = _pm.week_start(today)
        nt = popular_without_turn(today, rot.get("done") if rot.get("week") == wk else [], wk, path)
    except _NotProduction:
        nt = []
    except Exception as e:                                   # noqa: BLE001
        nt = []
        msgs.append(f"人気機種の今週の記録を読めません（{type(e).__name__}）")
    if nt:
        msgs.append(f"人気機種を今週見終わったと印したのに、点検表（STEP 1-T）を始めていない機種が{len(nt)}件: "
                    + "・".join(nt[:8]))
    _st = {}
    try:
        if path is not None:
            raise _NotProduction()
        import json as _js2
        _sp = _lp.doc("state.json")
        _st = _js2.loads(io.open(_sp, encoding="utf-8").read()) if os.path.isfile(_sp) else {}
        rv = reviewed_without_turn(((_st.get("quality_review") or {}).get("last_reviewed") or {}), path)
    except _NotProduction:
        rv = []
    except Exception as e:                                   # noqa: BLE001
        rv = []
        msgs.append(f"点検済みの記録を読めません（{type(e).__name__}）")
    if rv:
        msgs.append(f"点検済みにしたのに、その日に点検表（STEP 1-T）を始めていない機種が{len(rv)}件: " + "・".join(rv[:8]))
    try:
        # ★本番の控えを見るときだけ★（試験の機種一覧と本番の点検記録を混ぜない）
        sv = stale_reviews(((_st.get("quality_review") or {}).get("last_reviewed") or {}), rows, today)             if path is None else []
    except Exception as e:                                   # noqa: BLE001
        sv = []
        msgs.append(f"点検の古さを数えられません（{type(e).__name__}）")
    if sv:
        msgs.append(f"最後の点検から{REVIEW_STALE_DAYS}日を超えた機種が{len(sv)}件（点検の巡回が遅れています）: "
                    + "・".join(sv[:8]))
    return " ／ ".join(msgs) if msgs else None


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
      and "derived" in ka and "meta" in ka and "release" not in ka
      and "values" in kl and "values" in ka and "hall" in ka and "facts" in kl)
    t("★噂は昔の機種だけ（運営者「新台は噂はそもそもない」）★", "rumor" in kl and "rumor" not in ka)
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
    t("★★古い担当（8日以上前に始めた）でも、そろっていなければ知らせ続ける★★（Codexの指摘）",
      check_problem("2026-11-30", rows, p) is not None)
    try:
        done("zzz_l", "mode", "LATER", note="短い", rows=rows, today="2026-11-10", path=p)
        _short = False
    except TurnError:
        _short = True
    t("　LATER・NA は理由（10字以上）が要る", _short)
    _B = {"deadline_hhmm": "11:00", "deadline_by_task": {}}
    try:
        done("zzz_l", "mode", "LATER", note="締切までに回し切れないので翌日へ", rows=rows, today="2026-11-10", path=p,
             now_hhmm="09:30", budget=_B)
        _early_later = False
    except TurnError:
        _early_later = True
    t("★★締切の45分前（10:15）までは LATER にできない（見込みで翌日へ回さない）★★（11/11に9:36で終えた）", _early_later)
    t("★★そろっていない担当があれば、10:15前は「まだ終われない」★★",
      finish_problem("2026-11-10", "09:30", rows, p, _B) is not None
      and finish_problem("2026-11-10", "10:20", rows, p, _B) is None)
    t("　朝のタスクの時間帯の外（午後の対話セッションなど）は断らない",
      finish_problem("2026-11-10", "14:00", rows, p, _B) is None)
    try:
        done("zzz_l", "mode", "LATER", note="Codexが利用制限のため翌日に回す", rows=rows, today="2026-11-10", path=p,
             now_hhmm="09:30", codex_limited=True, budget=_B)
        _cl_other = False
    except TurnError:
        _cl_other = True
    t("　Codexの利用制限で翌日へ回せるのは天井だけ", _cl_other)
    done("zzz_l", "mode", "LATER", note="締切が近いので翌日に回す", rows=rows, today="2026-11-10", path=p,
         now_hhmm="10:20", budget=_B)
    t("★LATER の項目は、まだそろっていない扱い★", pending(rows, p) == [("zzz_l", ["mode"])])
    _p4 = p + ".cl.json"
    with io.open(_p4, "w", encoding="utf-8") as f:
        f.write(json.dumps({"turns": {"zzz_l": {"started": "2026-11-10", "items": {
            k: {"at": "2026-11-10", "result": "OK", "note": ""} for k in kl if k != "ceiling"}}}}))
    done("zzz_l", "ceiling", "LATER", note="Codexが利用制限のため翌日に回す", rows=rows, today="2026-11-10", path=_p4,
         now_hhmm="06:00", codex_limited=True, budget=_B)
    t("★Codexの利用制限で今日回した天井は、締切前でも終わってよい（翌日はまた残り扱い）★",
      finish_problem("2026-11-10", "06:00", rows, _p4, _B) is None
      and finish_problem("2026-11-11", "06:00", rows, _p4, _B) is not None)
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
    t("★★人気機種を今週見終わったと印したのに、その週に点検表を始めていなければ挙げる★★",
      popular_without_turn("2026-11-18", ["zzz_l", "zzz_x"], "2026-11-16", p) == ["zzz_x"]
      and popular_without_turn("2026-11-24", ["zzz_l"], "2026-11-23", p) == ["zzz_l"])
    t("　点検表ができる前の週の印は見ない", popular_without_turn("2026-11-10", ["zzz_x"], "2026-11-10", p) == [])
    t("★★点検済みにしたのに、その日に点検表を始めていない機種を挙げる★★（Codexの指摘＝その他の機種は印だけ付けられた）",
      reviewed_without_turn({"zzz_l": "2026-11-17", "zzz_y": "2026-11-12", "zzz_old": "2026-11-01"}, p)
      == ["zzz_y"])
    _p3 = p + ".rv.json"
    with io.open(_p3, "w", encoding="utf-8") as f:
        f.write(json.dumps({"turns": {"zzz_o": {"started": "2026-11-11", "items": {
            "mode": {"at": "2026-11-11", "result": "OK", "note": ""}}}}}))
    t("★★点検済みの日より後に始めた点検表があれば、その前の印は挙げない★★（代役の指摘＝翌週の担当で誤って出た）",
      reviewed_without_turn({"zzz_l": "2026-11-12"}, p) == [])
    t("★★最後の点検から60日を超えた機種を挙げる（記録の無い昔の機種も）★★（代役の指摘＝巡回の遅れに見張りが無かった）",
      stale_reviews({"a": "2026-09-01", "b": "2026-10-20", "z": "2026-01-01", "y": "2026-09-01"},
                    [{"slug": "a"}, {"slug": "b"}, {"slug": "c"}, {"slug": "d", "publication_policy": "page-decision/v2"},
                     {"slug": "z", "publication_policy": "page-decision/v2", "release_date": "2026-11-01"},
                     {"slug": "y", "publication_policy": "page-decision/v2", "release_date": "2026-12-07"},
                     {"slug": "e", "publication_policy": "page-decision/v2", "release_date": "2026-08-03"}],
                    "2026-11-10") == ["a", "c", "e"])
    import inspect as _insp
    t("　（配線）見張り番の点検が、点検の古さを数えている",
      ("stale_" + "reviews(") in _insp.getsource(check_problem))
    t("★★ずっと前の点検表では、その後の点検済みの印を覆わない（3日前まで）★★（代役の指摘）",
      reviewed_without_turn({"zzz_o": "2026-11-20"}, _p3) == ["zzz_o"]
      and reviewed_without_turn({"zzz_o": "2026-11-13"}, _p3) == [])
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
    ap.add_argument("--can-finish", action="store_true",
                    help="朝のタスクが終わってよいか（そろっていない担当が残り、締切の45分前より前なら1）")
    ap.add_argument("--codex-limited", action="store_true",
                    help="Codexの利用制限で天井（ceiling）を翌日へ回す（LATER のときだけ・締切前でも可）")
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
            done(a.done, a.item, a.result, note, codex_limited=a.codex_limited)
            print(plan_text(a.done))
            return 0
        if a.pending:
            got = pending()
            for s, left in got:
                print(f"{s}: 残り {'・'.join(left)}")
            if not got:
                print("そろっていない担当はありません")
            return 0
        if a.can_finish:
            fp = finish_problem()
            print(("⏳ " + fp) if fp else "🟢 終わってよい（そろっていない担当が無いか、締切の45分前を過ぎています）")
            return 1 if fp else 0
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

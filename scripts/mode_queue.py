"""★モード・ゾーンの欄を、毎朝どの機種でやるかを決め、進み具合を見張る★（2026-10-26・運営者「それで進めて」）

使い方:
  python scripts/mode_queue.py --next 2                       … 今朝やる機種（人気の機種から・まだ決まっていない機種）
  python scripts/mode_queue.py --tried <slug> --result <結果>  … やった結果を記録する（結果は下の RESULTS）
  python scripts/mode_queue.py --check                        … 進み具合（番人が毎朝呼ぶ・止まっていれば終了コード1）
  python scripts/mode_queue.py --selftest

★なぜ要るか★＝2026-09-02 に朝の更新タスクへ「担当した機種に欄が無ければやる」を入れたが、
  タスクは毎回「Codexの枠を夜の新台と分け合っているから」と書いて飛ばし、★2か月近く1件も入らなかった★
  （1機種で相手のサイトを最大50ページ読み、2AIが読む量が多いため）。
  やらなくても誰も気づかない作りだったので、★毎朝の割り当て★にして、★止まったら番人が知らせる★。
★決まった機種★＝mode_verdict の控えに mode と zone の両方の答え（HAS / NONE_CONFIRMED）がある機種。
★順番★＝①まだ一度もやっていない機種（人気の順→機種一覧の順） ②前回やってから日が経った機種（古い順）。
  ★直近 RETRY_DAYS 日にやった機種は選ばない★（決まらなかった機種が毎朝の枠を占領しないため）。
"""
import argparse
import datetime as _dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import local_paths as _lp  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
STORE = _lp.doc("mode_queue.json")
RESULTS = ("APPLIED", "NO_BOX", "UNDECIDED", "UNREADABLE", "ERROR")
#   APPLIED    … 2AIで決まり、記事に反映した
#   NO_BOX     … 2AIで決まったが、記事に出る欄は無い（片方だけ決まった等・控えには残る）
#   UNDECIDED  … 2AIで決まらなかった（欄は出さない）
#   UNREADABLE … 材料が読めなかった（mode_ask が止まった）
#   ERROR      … 道具が止まった（その日のログに理由）
DONE_RESULTS = ("APPLIED", "NO_BOX")   # ★記事への反映まで済んだ結果★（これだけを「進んだ」と数える）
RETRY_DAYS = 14          # 決まらなかった機種をもう一度選ぶまでの日数
STALL_DAYS = 3           # これだけ1件も進まなければ番人が知らせる
NEW_DAYS = 60            # 導入からこの日数までは「新台」として早めに見直す
RECHECK_NEW = 7          # 新台の済んだ機種を見直すまでの日数（解析は導入後に出そろう）
RECHECK_OLD = 60         # それ以外の済んだ機種を見直すまでの日数
RECHECK_PER_DAY = 1      # 先頭に置く見直しの件数（残りの枠は、まだやっていない機種へ）
JST = _dt.timezone(_dt.timedelta(hours=9))


def _today() -> str:
    return _dt.datetime.now(JST).date().isoformat()


def _read_store() -> dict:
    """★無いは空・壊れていたら例外★（黙って0件と読まない）"""
    if not os.path.isfile(STORE):
        return {}
    with open(STORE, encoding="utf-8") as f:
        got = json.load(f)
    if not isinstance(got, dict) or not isinstance(got.get("tried", {}), dict):
        raise ValueError("mode_queue.json の形が違います")
    return got


def _write_store(data: dict) -> None:
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=1) + "\n")
    os.replace(tmp, STORE)


def decided_slugs() -> set:
    """★mode と zone の両方の答えが控えにある機種★"""
    import mode_verdict as _mv
    got = _mv._read()
    have = {}
    for k in got:
        if "::" in k:
            s, kind = k.split("::", 1)
            have.setdefault(s, set()).add(kind)
    return {s for s, kinds in have.items() if {"mode", "zone"} <= kinds}


def _order(machines: list) -> list:
    import popular_machines as _pm
    alive = [str(m["slug"]) for m in machines if isinstance(m, dict) and m.get("slug")]
    pop = [s for s in _pm.popular_slugs(machines) if s in alive]
    return pop + [s for s in alive if s not in pop]


def done_slugs(decided: set, tried: dict) -> set:
    """★済んだ機種＝控えが mode と zone の両方にあり、最後の結果が記事への反映まで済んでいる★
    （review285 の2）＝控えだけで済み扱いにすると、反映が失敗した機種が二度と選ばれない"""
    return {s for s in decided if str((tried.get(s) or {}).get("result", "")) in DONE_RESULTS}


def recheck_days(m: dict, today: str) -> int:
    """★済んだ機種を見直すまでの日数★（2026-11-10・運営者「更新されないページ他にもないの？」）

    ★直す前は「済んだ機種は二度と選ばない」★＝導入から数日で決めた新台の表が、
    解析が出そろったあとも古いまま固定されていた（済んだ25機種のうち新台7機種は導入直後に固定）。
    導入から NEW_DAYS 日以内の機種は RECHECK_NEW 日おき、それ以外は RECHECK_OLD 日おきに見直す。
    ★見直しは軽い★＝mode_ask は2回目以降、本体1ページだけ読み、証拠の集合が変わったときだけ2AIへ回す。
    """
    rel = str((m or {}).get("release_date") or "")
    try:
        if len(rel) == 7:
            rel += "-01"
        age = (_dt.date.fromisoformat(today) - _dt.date.fromisoformat(rel[:10])).days
    except ValueError:
        return RECHECK_OLD
    # ★導入前は新台の周期にしない★（2026-11-10・洗い出し2回目）＝解析は導入後に出そろうので、
    #   導入前に7日おきで見直しても証拠はほとんど変わらず、枠だけを使う
    return RECHECK_NEW if 0 <= age <= NEW_DAYS else RECHECK_OLD


def pick(n: int, machines: list, decided: set, tried: dict, today: str) -> list:
    """★純関数★（試験はここを直接呼ぶ）

    順番＝①まだ一度もやっていない ②決まらなかった・失敗して RETRY_DAYS 日たった（古い順）
    ③済んだが見直しの日が来た（古い順・recheck_days）
    """
    done = done_slugs(decided, tried)
    order = [s for s in _order(machines) if s not in done]
    cutoff = (_dt.date.fromisoformat(today) - _dt.timedelta(days=RETRY_DAYS)).isoformat()
    never = [s for s in order if s not in tried]
    old = sorted([s for s in order if s in tried and str(tried[s].get("at", "")) <= cutoff],
                 key=lambda s: str(tried[s].get("at", "")))
    rows = {str(m.get("slug")): m for m in machines if isinstance(m, dict)}
    due = []
    for s in _order(machines):
        if s not in done:
            continue
        at = str((tried.get(s) or {}).get("at", ""))
        try:
            gap = (_dt.date.fromisoformat(today) - _dt.date.fromisoformat(at)).days
        except ValueError:
            gap = 10 ** 6
        if gap >= recheck_days(rows.get(s), today):
            due.append(s)
    due.sort(key=lambda s: str((tried.get(s) or {}).get("at", "")))
    # ★新台の見直しは先頭★（運営者の優先順位＝①新台 ②人気 ③その他）＝まだやっていない旧機種が
    #   100機種以上あり、後ろに置くと新台の見直しが何か月も回ってこない
    due_new = [s for s in due if recheck_days(rows.get(s), today) == RECHECK_NEW]
    due_old = [s for s in due if s not in due_new]
    # ★★見直しは1日 RECHECK_PER_DAY 件まで★★（2026-11-10・洗い出し2回目＝新台の見直しが1日約3件出て、
    #   1日2件の枠を埋め、一度も見ていない昔の機種が進まなくなっていた）。
    #   新台の見直しを1件だけ先頭に置き、残りの枠はまだやっていない機種へ回す。
    head = due_new[:RECHECK_PER_DAY]
    rest_due = due_new[RECHECK_PER_DAY:] + due_old
    return (head + never + old + rest_due)[:max(0, n)]


NEW_STALL_DAYS = 7       # まだ一度も済んでいない機種が、これだけ1件も済まなければ止まっている


def record_tried(prev: dict, today: str, result: str) -> dict:
    """★やった結果の記録★（初めて済んだ日 first_done_at は残す）

    ★なぜ★（2026-11-10・洗い出し2回目）＝見直しで済んだ結果が毎日記録されると「進んでいる」に見え、
    一度も見ていない機種が進んでいなくても番人が気づけない。初めて済んだ日を別に持つ。
    """
    rec = {"at": today, "result": result}
    first = str((prev or {}).get("first_done_at") or "")
    if not first and result in DONE_RESULTS:
        first = today
    if first:
        rec["first_done_at"] = first
    return rec


def progress(machines: list, decided: set, tried: dict, today: str) -> dict:
    alive = {str(m["slug"]) for m in machines if isinstance(m, dict) and m.get("slug")}
    done = done_slugs(decided, tried)
    remaining = len(alive - done)
    # ★「進んだ」日＝反映まで済んだ結果を記録した最後の日★（review285 の1）＝
    #   試した日で見ると、毎朝失敗（ERROR・UNREADABLE）を記録し続けるだけで🟢になる
    last = max((str(v.get("at", "")) for v in tried.values()
                if str(v.get("result", "")) in DONE_RESULTS), default="")
    stalled = False
    if remaining:
        if not last:
            stalled = True
        else:
            gap = (_dt.date.fromisoformat(today) - _dt.date.fromisoformat(last)).days
            stalled = gap >= STALL_DAYS
        # ★★見直しばかりで、まだ一度も済んでいない機種が進んでいなければ止まっている★★（2026-11-10）
        #   初めて済んだ日（first_done_at）が無い古い記録は、済んだ日（at）で代える
        _firsts = [str(v.get("first_done_at") or (v.get("at") if str(v.get("result", "")) in DONE_RESULTS else ""))
                   for v in tried.values()]
        _first_last = max((x for x in _firsts if x), default="")
        if _first_last:
            _gap_new = (_dt.date.fromisoformat(today) - _dt.date.fromisoformat(_first_last[:10])).days
            stalled = stalled or _gap_new >= NEW_STALL_DAYS
    # ★欄が入った機種★＝反映まで済んだうち、記事に欄が出たもの（NO_BOX は決まったが欄は出ない）
    applied = len({s for s in alive & done if str(tried[s].get("result", "")) == "APPLIED"})
    return {"decided": len(alive & done), "applied": applied, "total": len(alive), "remaining": remaining,
            "last_tried": last or "（まだ1機種も済んでいない）", "stalled": stalled}


def selftest() -> int:
    results = []

    def t(name, cond):
        results.append(bool(cond))
        print(("✅" if cond else "❌") + " " + name)

    import popular_machines as _pm
    keep = _pm.popular_slugs
    try:
        _pm.popular_slugs = lambda machines=None: ["c", "zz"]
        ms = [{"slug": s} for s in ("a", "b", "c", "d")]
        t("★人気の機種が先・済んだ機種は選ばない★",
          pick(2, ms, {"a"}, {"a": {"at": "2026-09-01", "result": "APPLIED"}}, "2026-10-26") == ["c", "b"])
        t("★直近にやった機種は選ばない★（決まらなかった機種が枠を占領しない）",
          pick(4, ms, set(), {"c": {"at": "2026-10-25"}}, "2026-10-26") == ["a", "b", "d"])
        t("★日が経った機種は、まだやっていない機種のあとで古い順に選ぶ★",
          pick(4, ms, set(), {"c": {"at": "2026-09-01"}, "a": {"at": "2026-08-01"}}, "2026-10-26")
          == ["b", "d", "a", "c"])
        p = progress(ms, {"a"}, {"a": {"at": "2026-10-22", "result": "APPLIED"}}, "2026-10-26")
        t("★3日以上1件も進まなければ止まっている★（番人が知らせる）", p["stalled"] and p["remaining"] == 3)
        t("★昨日済んでいれば止まっていない★",
          not progress(ms, {"b"}, {"b": {"at": "2026-10-25", "result": "NO_BOX"}}, "2026-10-26")["stalled"])
        t("★一度もやっていなければ止まっている★", progress(ms, set(), {}, "2026-10-26")["stalled"])
        _all = {s: {"at": "2026-10-25", "result": "APPLIED"} for s in "abcd"}
        t("★全部済めば止まっていない★", not progress(ms, {"a", "b", "c", "d"}, _all, "2026-10-26")["stalled"])
        _mix = {"a": {"at": "2026-10-25", "result": "APPLIED"}, "b": {"at": "2026-10-25", "result": "NO_BOX"}}
        t("★欄が入った機種は APPLIED だけを数える★（決まったが欄が出ない NO_BOX は数えない）",
          progress(ms, {"a", "b"}, _mix, "2026-10-26")["applied"] == 1
          and progress(ms, {"a", "b"}, _mix, "2026-10-26")["decided"] == 2)
        _fail = {s: {"at": "2026-10-25", "result": "ERROR"} for s in "ab"}
        t("★★毎朝失敗を記録するだけでは「進んだ」にならない★★（review285 の1）",
          progress(ms, set(), _fail, "2026-10-26")["stalled"])
        t("★★控えはあるが反映が失敗した機種は、もう一度選ぶ★★（review285 の2）",
          "a" in pick(4, ms, {"a"}, {"a": {"at": "2026-09-01", "result": "ERROR"}}, "2026-10-26"))
        t("★控えがあり反映まで済んだ機種は、見直しの日が来るまで選ばない★",
          "a" not in pick(4, ms, {"a"}, {"a": {"at": "2026-09-01", "result": "APPLIED"}}, "2026-10-26"))
        # ★★済んだ機種も見直す★★（2026-11-10＝直す前は二度と選ばれず、導入直後の表が固定されていた）
        _ms2 = [{"slug": "a", "release_date": "2026-11-02"}, {"slug": "b", "release_date": "2025-01-01"},
                {"slug": "c"}, {"slug": "d"}]
        _tr2 = {"a": {"at": "2026-11-03", "result": "APPLIED"}, "b": {"at": "2026-09-01", "result": "NO_BOX"},
                "c": {"at": "2026-11-01", "result": "UNDECIDED"}}
        t("★★導入から60日以内の済んだ機種は、7日たったら見直す★★",
          "a" in pick(4, _ms2, {"a", "b"}, _tr2, "2026-11-10"))
        t("　（対照）6日なら見直さない", "a" not in pick(4, _ms2, {"a", "b"}, _tr2, "2026-11-09"))
        t("★それ以外の済んだ機種は、60日たったら見直す★",
          "b" in pick(4, _ms2, {"a", "b"}, _tr2, "2026-10-31")
          and "b" not in pick(4, _ms2, {"a", "b"}, _tr2, "2026-10-30"))
        t("★★新台の見直しは先頭、それ以外の見直しは、まだやっていない機種と決まらなかった機種のあと★★",
          pick(4, _ms2, {"a", "b"}, _tr2, "2026-11-20") == ["a", "d", "c", "b"])
        # ★★見直しは1日1件まで先頭（残りの枠はまだやっていない機種へ）★★（2026-11-10・洗い出し2回目）
        _ms3 = [{"slug": s, "release_date": "2026-11-02"} for s in ("n1", "n2", "n3")] + [{"slug": "o1"}]
        _tr3 = {s: {"at": "2026-11-03", "result": "APPLIED"} for s in ("n1", "n2", "n3")}
        t("★★新台の見直しが3件あっても先頭は1件、2件目の枠はまだやっていない機種★★",
          pick(2, _ms3, {"n1", "n2", "n3"}, _tr3, "2026-11-20") == ["n1", "o1"])
        t("★導入前の機種は7日おきの見直しにしない★",
          recheck_days({"release_date": "2026-12-07"}, "2026-11-20") == RECHECK_OLD)
        # ★★見直しばかりで、まだやっていない機種が進まなければ止まっている★★（2026-11-10）
        _r1 = record_tried(None, "2026-11-01", "APPLIED")
        _r2 = record_tried(_r1, "2026-11-12", "APPLIED")
        t("　初めて済んだ日は、見直しで記録し直しても残る",
          _r1["first_done_at"] == "2026-11-01" and _r2["first_done_at"] == "2026-11-01" and _r2["at"] == "2026-11-12")
        _pr = progress([{"slug": "a"}, {"slug": "b"}], {"a"}, {"a": _r2}, "2026-11-12")
        t("★★見直しで毎日済んでいても、まだやっていない機種が7日以上進まなければ止まっている★★",
          _pr["stalled"] and _pr["remaining"] == 1)
        t("　（対照）まだやっていない機種が最近済んでいれば止まっていない",
          not progress([{"slug": "a"}, {"slug": "b"}], {"a"}, {"a": record_tried(None, "2026-11-10", "APPLIED")},
                       "2026-11-12")["stalled"])
        t("　導入日の書き方が月だけでも読める・読めなければ60日",
          recheck_days({"release_date": "2026-11"}, "2026-11-10") == RECHECK_NEW
          and recheck_days({}, "2026-11-10") == RECHECK_OLD)
    finally:
        _pm.popular_slugs = keep
    # ★★記録が壊れていたら、本物の起動で終了コード2になる★★（review286）＝
    #   画面の文字コードを cp932 にして起動する（このPCの既定と同じ）
    import subprocess
    import tempfile
    d = tempfile.mkdtemp(prefix="mq_")
    with open(os.path.join(d, "mode_queue.json"), "w", encoding="utf-8") as f:
        f.write("{壊れた")
    env = dict(os.environ, UCHIDOKORO_DOCS=d, PYTHONIOENCODING="cp932")
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "--check"], env=env,
                       capture_output=True)
    t("★★記録が壊れていたら終了コード2（🟠）★★（画面が cp932 でも1にならない・review286）",
      r.returncode == 2)
    print(f"\n{sum(results)}/{len(results)} 合格")
    return 0 if all(results) else 1


def main() -> int:
    """終了コード：0＝進んでいる／1＝止まっている（🟡）／2＝記録や控えが読めない（🟠・review285 の3）"""
    # ★表示を UTF-8 に固定★（review286）＝このPCの標準出力は cp932 で、🟠 を出した瞬間に止まり
    #   終了コード1（＝🟡と取り違える）になっていた
    for _st in (sys.stdout, sys.stderr):
        try:
            _st.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                # noqa: BLE001
            pass
    try:
        return _main()
    except Exception as e:                                # noqa: BLE001
        print(f"🟠 モード・ゾーンの欄の記録を読めません（{type(e).__name__}: {e}）")
        return 2


def _main() -> int:
    ap = argparse.ArgumentParser(description="モード・ゾーンの欄を毎朝どの機種でやるか")
    ap.add_argument("--next", type=int, default=0)
    ap.add_argument("--tried", default="")
    ap.add_argument("--result", default="", choices=("",) + RESULTS)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    with open(MACHINES, encoding="utf-8") as f:
        machines = json.load(f)
    store = _read_store()
    tried = store.get("tried", {})
    today = _today()
    if a.tried:
        if not a.result:
            print("★--result が要ります★（" + " / ".join(RESULTS) + "）")
            return 1
        if a.tried not in {str(m.get("slug")) for m in machines if isinstance(m, dict)}:
            print(f"★機種一覧に無い機種です: {a.tried}★")
            return 1
        tried[a.tried] = record_tried(tried.get(a.tried), today, a.result)
        store["tried"] = tried
        _write_store(store)
        print(f"記録しました: {a.tried} {a.result}（{today}）")
        return 0
    decided = decided_slugs()
    if a.next:
        got = pick(a.next, machines, decided, tried, today)
        print(" ".join(got) if got else "（今朝やる機種はありません）")
        return 0
    p = progress(machines, decided, tried, today)
    line = (f"モード・ゾーンの欄：{p['decided']}/{p['total']} 機種で済んだ（うち欄が入った {p['applied']}・"
            f"残り {p['remaining']}・"
            f"最後に済んだ日 {p['last_tried']}）")
    if p["stalled"]:
        print(f"🟡 {line}。★{STALL_DAYS}日以上1件も進んでいません★（朝の更新タスクの STEP 1-M）")
        return 1
    print(f"🟢 {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

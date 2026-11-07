"""★ランキング専用タスク（毎日17:00）が、その日どの5件を確かめるかを決め、進み具合を見張る★（2026-11-07）

使い方:
  python scripts/ranking_queue.py --next 5                                   … 今日やる件（ランキングと機種の組）
  python scripts/ranking_queue.py --tried <slug> --ranking reset|tenjo --result <結果>   … やった結果を記録する
  python scripts/ranking_queue.py --check                                    … 進み具合（番人が毎朝呼ぶ）
  python scripts/ranking_queue.py --selftest

★対象★＝朝一リセットランキングの上位10と、天井ランキングの上位10（運営者の決定「ベスト10・毎日5機種」）。
  ★ページと同じ並べ方から毎回取る★（`build_hub_pages.ranking_top`）＝別の数え方を作らない。
★1件＝（ランキング, 機種）の組★＝同じ機種が両方の上位10に入っても、片方の確認で他方を済みにしない。
★順番★（運営者の選択「朝一リセット → 天井の順」・Codex review_ranking_task の4）
  ①まだ一度も確かめ終えていない件（リセットの順位順 → 天井の順位順）
  ②確かめ終えた日が古い件（同じ日ならリセットが先・次に順位）
  ★今日すでに試した件は選ばない★（同じ日に動かし直しても同じ件を2回やらない）。
  ★確かめられなかった件（UNDECIDED・UNREADABLE・ERROR）は RETRY_DAYS 日あけてから選び直す★
  （毎日同じ件が枠を占領しないため）。
★「試した日」と「確かめ終えた日」は分けて持つ★＝読めなかった日を「見た」と数えると、
  確かめていないのに一周したことになる。
"""
import argparse
import datetime as _dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import local_paths as _lp  # noqa: E402

STORE = _lp.doc("ranking_queue.json")
RANKINGS = ("reset", "tenjo")
RESULTS = ("NOTE", "FIXED", "NO_NOTE", "ASKED", "UNDECIDED", "UNREADABLE", "ERROR")
#   NOTE       … 数字は合っていて、一言を書いた（または今の一言のままでよいと2AIで確かめた）
#   FIXED      … ランキングの数字（線・天井）か記事を直した（一言も確かめ終えた）
#   NO_NOTE    … 確かめ終えたが、載せられる一言は無い（今の一言があれば取り下げた）
#   ASKED      … 3回で割れたので運営者へ回した（owner_questions）
#   UNDECIDED  … その回では決まらなかった（締切・Codexの止まりなど）
#   UNREADABLE … 材料が読めなかった
#   ERROR      … 道具が止まった（その日のログに理由）
DONE_RESULTS = ("NOTE", "FIXED", "NO_NOTE", "ASKED")
RETRY_DAYS = 2
STALL_DAYS = 3          # これだけ1件も確かめ終えなければ番人が知らせる
STALE_DAYS = 10         # 上位10のどれかが、これより長く確かめ直されていなければ番人が知らせる
JST = _dt.timezone(_dt.timedelta(hours=9))


def _today() -> str:
    return _dt.datetime.now(JST).date().isoformat()


def _key(ranking: str, slug: str) -> str:
    return f"{ranking}::{slug}"


def read_store(path: str = "") -> dict:
    """★無いは空・壊れていたら例外★（黙って0件と読まない）"""
    path = path or STORE
    if not os.path.isfile(path):
        return {"items": {}}
    with open(path, encoding="utf-8") as f:
        got = json.load(f)
    if not isinstance(got, dict) or not isinstance(got.get("items"), dict):
        raise ValueError("ranking_queue.json の形が違います")
    # ★中身も1件ずつ確かめる★（壊れた1件で並べ替えが例外になり、番人が「進んでいない」と取り違えないため）
    for k, e in got["items"].items():
        rk, _, slug = str(k).partition("::")
        if rk not in RANKINGS or not slug or not isinstance(e, dict):
            raise ValueError(f"ranking_queue.json の項目の形が違います: {k!r}")
        for f in ("tried_at", "done_at"):
            v = e.get(f)
            if v is not None:
                _dt.date.fromisoformat(str(v))   # 日付でなければ ValueError
        if e.get("result") is not None and e.get("result") not in RESULTS:
            raise ValueError(f"ranking_queue.json の結果が知らない値です: {k!r}")
    plan = got.get("plan")
    if plan is not None:
        if not isinstance(plan, dict) or not isinstance(plan.get("keys"), list) \
                or not all(isinstance(x, str) and "::" in x for x in plan["keys"]):
            raise ValueError("ranking_queue.json の今日の割り当て（plan）の形が違います")
        _dt.date.fromisoformat(str(plan.get("date")))
    return got


def _write_store(data: dict, path: str = "") -> None:
    path = path or STORE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=1) + "\n")
    os.replace(tmp, path)


def targets() -> list:
    """★いまページに出ている上位10★ → [(ranking, slug, 順位)]（リセットが先）"""
    import build_hub_pages as _bh
    rows = _bh.load_rows()
    out = []
    for ranking in RANKINGS:
        for i, r in enumerate(_bh.ranking_top(rows, ranking), 1):
            out.append((ranking, r["slug"], i))
    return out


def pick(n: int, items: list, store: dict, today: str) -> list:
    """★純関数★（試験はここを直接呼ぶ）`items`＝[(ranking, slug, 順位)]"""
    st = store.get("items") or {}
    cutoff = (_dt.date.fromisoformat(today) - _dt.timedelta(days=RETRY_DAYS)).isoformat()

    def usable(it):
        e = st.get(_key(it[0], it[1])) or {}
        tried = str(e.get("tried_at") or "")
        if tried == today:
            return False
        if tried and str(e.get("result") or "") not in DONE_RESULTS and tried > cutoff:
            return False
        return True

    order = {r: i for i, r in enumerate(RANKINGS)}
    cand = [it for it in items if usable(it)]

    def ent(it):
        return st.get(_key(it[0], it[1])) or {}
    # ★まだ一度も試していない件 → 確かめ終えられなかった件（試した日が古い順）→ 確かめ終えた件★
    #   （2026-11-07・Codexの指摘）＝読めない件を「まだの件」と同じ並びに置くと、リセット10件が
    #   読めない日が続くたびに1〜5位・6〜10位を交互に選び続け、天井へ永久に進まなかった
    def failed_last(it):
        """★いちばん新しい結果が「確かめ終えられなかった」★（前に確かめ終えた日があっても）"""
        e = ent(it)
        return bool(e.get("tried_at")) and str(e.get("result") or "") not in DONE_RESULTS

    fresh = [it for it in cand if not ent(it).get("tried_at") and not ent(it).get("done_at")]
    fresh.sort(key=lambda it: (order[it[0]], it[2]))
    retry = [it for it in cand if failed_last(it)]
    retry.sort(key=lambda it: (str(ent(it)["tried_at"]), order[it[0]], it[2]))
    old = [it for it in cand if it not in fresh and it not in retry]
    old.sort(key=lambda it: (str(ent(it).get("done_at") or ""), order[it[0]], it[2]))
    # ★1日の件数は「今日すでに試した件」を差し引く★（動かし直しても1日5件を超えない）
    done_today = sum(1 for e in st.values() if str((e or {}).get("tried_at") or "") == today)
    # ★確かめ終えられなかった件と、確かめ終えた件を1件ずつ交互に★（2026-11-07・Codexの2回目・3回目）
    #   ＝読めない件を全部先にすると、読めない件が続くかぎり確かめ直しに一度も枠が回らなかった。
    #   件数で切る（2件まで先）形は、同じ日に動かし直すと枠が戻って偏った。交互なら、何回動かし直しても
    #   どちらか一方だけが続くことはない（片方が尽きたら残りはもう一方）
    mixed = []
    for i in range(max(len(retry), len(old))):
        if i < len(retry):
            mixed.append(retry[i])
        if i < len(old):
            mixed.append(old[i])
    return (fresh + mixed)[:max(0, n - done_today)]


def plan_today(n: int, items: list, store: dict, today: str) -> tuple:
    """★その日の割り当ては、その日の最初の呼び出しで決めて控え、同じ日は使い回す★（Codexの4回目）
    ＝呼ぶたびに選び直すと、1件終えて動かし直すたびに交互の順番が最初（失敗側）に戻り、
    5件とも失敗側になった。→ (今日やる残りの件, 控えに書く plan か None)"""
    st = store.get("items") or {}
    plan = store.get("plan") or {}
    if plan.get("date") == today:
        by_key = {_key(it[0], it[1]): it for it in items}
        left = [by_key[k] for k in plan["keys"]
                if k in by_key and str((st.get(k) or {}).get("tried_at") or "") != today]
        # ★控えた割り当てでも、今日すでに試した件の数を差し引く★（Codexの5回目＝割り当ての外で
        #   STEP 0 の件をやって止まり、動かし直すと1日6件になった）
        done_today = sum(1 for e in st.values() if str((e or {}).get("tried_at") or "") == today)
        return left[:max(0, n - done_today)], None
    got = pick(n, items, store, today)
    return got, {"date": today, "keys": [_key(it[0], it[1]) for it in got]}


def next_items(n: int, path: str = "", today: str = "", items=None) -> list:
    today = today or _today()
    data = read_store(path)
    got, plan = plan_today(n, targets() if items is None else items, data, today)
    if plan is not None:
        data["plan"] = plan
        _write_store(data, path)
    return got


def tried(slug: str, ranking: str, result: str, today: str = "", path: str = "") -> dict:
    if ranking not in RANKINGS:
        raise ValueError(f"ranking は {' / '.join(RANKINGS)} のどれかです")
    if result not in RESULTS:
        raise ValueError(f"result は {' / '.join(RESULTS)} のどれかです")
    today = today or _today()
    data = read_store(path)
    e = dict(data["items"].get(_key(ranking, slug)) or {})
    e["tried_at"] = today
    e["result"] = result
    if result in DONE_RESULTS:
        e["done_at"] = today
    data["items"][_key(ranking, slug)] = e
    _write_store(data, path)
    return e


def progress(items: list, store: dict, today: str) -> dict:
    st = store.get("items") or {}
    d0 = _dt.date.fromisoformat(today)
    never, stale = [], []
    for ranking, slug, rank in items:
        e = st.get(_key(ranking, slug)) or {}
        done = str(e.get("done_at") or "")
        if not done:
            never.append(f"{ranking} {rank}位 {slug}")
        elif (d0 - _dt.date.fromisoformat(done)).days > STALE_DAYS:
            stale.append(f"{ranking} {rank}位 {slug}（{done}）")
    last = max((str(e.get("done_at") or "") for e in st.values()), default="")
    stalled = False
    if never or stale:
        stalled = (not last) or (d0 - _dt.date.fromisoformat(last)).days >= STALL_DAYS
    return {"total": len(items), "never": never, "stale": stale,
            "last_done": last or "（まだ1件も確かめ終えていない）", "stalled": stalled}


def check() -> int:
    """終了コード 0=順調 / 1=止まっている・古い件がある（🟡）/ 2=読めない（🟠）"""
    try:
        items = targets()
        store = read_store()
        p = progress(items, store, _today())
    except Exception as e:                       # noqa: BLE001
        print(f"★ランキングの順番の控えを読めません★: {type(e).__name__}: {e}")
        return 2
    print(f"ランキングの確認：上位{p['total']}件・まだの件{len(p['never'])}・"
          f"{STALE_DAYS}日以上前の件{len(p['stale'])}・最後に確かめ終えた日 {p['last_done']}")
    if p["stalled"]:
        print(f"★{STALL_DAYS}日以上、1件も確かめ終えていません★（ランキングのタスクが止まっている疑い）")
    for x in p["stale"]:
        print(f"  ★古い★ {x}")
    return 1 if (p["stalled"] or p["stale"]) else 0


def selftest() -> int:
    import tempfile
    results = []

    def t(name, cond):
        results.append(bool(cond))
        print(("✅" if cond else "❌") + " " + name)

    items = [("reset", "a", 1), ("reset", "b", 2), ("tenjo", "c", 1), ("tenjo", "a", 2)]
    today = "2026-11-10"
    t("★初めはリセットの順位順 → 天井の順位順★",
      [(r, s) for r, s, _ in pick(4, items, {"items": {}}, today)]
      == [("reset", "a"), ("reset", "b"), ("tenjo", "c"), ("tenjo", "a")])
    st = {"items": {"reset::a": {"tried_at": "2026-11-09", "done_at": "2026-11-09", "result": "NOTE"}}}
    t("★同じ機種でも、ランキングが違えば別の件★（リセットを確かめても天井は残る）",
      ("tenjo", "a") in [(r, s) for r, s, _ in pick(4, items, st, today)])
    t("★確かめ終えた件は、まだの件のあと★",
      [(r, s) for r, s, _ in pick(4, items, st, today)][-1] == ("reset", "a"))
    st2 = {"items": {"reset::a": {"tried_at": today, "result": "NOTE", "done_at": today}}}
    # ★枠を大きく取って試す★＝件数で切れて、たまたま選ばれないのと区別するため
    t("★今日すでに試した件は選ばない★", ("reset", "a") not in [(r, s) for r, s, _ in pick(10, items, st2, today)])
    st3 = {"items": {"reset::a": {"tried_at": "2026-11-09", "result": "UNREADABLE"}}}
    t("★読めなかった件は日をあけてから選び直す★",
      ("reset", "a") not in [(r, s) for r, s, _ in pick(4, items, st3, today)])
    st4 = {"items": {"reset::a": {"tried_at": "2026-11-07", "result": "UNREADABLE"}}}
    t("日があけば読めなかった件も選び直す",
      ("reset", "a") in [(r, s) for r, s, _ in pick(4, items, st4, today)])
    st5 = {"items": {"reset::a": {"tried_at": "2026-11-01", "done_at": "2026-11-01", "result": "NOTE"},
                     "reset::b": {"tried_at": "2026-11-05", "done_at": "2026-11-05", "result": "NOTE"},
                     "tenjo::c": {"tried_at": "2026-11-01", "done_at": "2026-11-01", "result": "NOTE"},
                     "tenjo::a": {"tried_at": "2026-11-03", "done_at": "2026-11-03", "result": "NOTE"}}}
    t("★一周したあとは確かめ終えた日が古い順（同じ日ならリセットが先）★",
      [(r, s) for r, s, _ in pick(4, items, st5, today)]
      == [("reset", "a"), ("tenjo", "c"), ("tenjo", "a"), ("reset", "b")])
    t("件数どおりに切る", len(pick(2, items, {"items": {}}, today)) == 2)
    st8 = {"items": {"reset::a": {"tried_at": "2026-11-05", "result": "UNREADABLE"},
                     "reset::b": {"tried_at": "2026-11-06", "result": "UNREADABLE"}}}
    t("★読めない件が続いても、まだ試していない天井の件を先に選ぶ★（天井へ永久に進まない形）",
      [(r, s) for r, s, _ in pick(2, items, st8, today)] == [("tenjo", "c"), ("tenjo", "a")])
    t("　読めなかった件は、試した日が古い順に後ろへ",
      [(r, s) for r, s, _ in pick(4, items, st8, today)][2:] == [("reset", "a"), ("reset", "b")])
    many = [("reset", f"r{i}", i) for i in range(1, 11)] + [("tenjo", f"t{i}", i) for i in range(1, 11)]
    st10 = {"items": {f"reset::r{i}": {"tried_at": "2026-11-0" + str(i % 7 + 1), "result": "UNREADABLE"}
                      for i in range(1, 11)}}
    st10["items"].update({f"tenjo::t{i}": {"tried_at": "2026-11-01", "done_at": "2026-11-01", "result": "NOTE"}
                          for i in range(1, 11)})
    got10 = [(r, s) for r, s, _ in pick(5, many, st10, today)]
    t("★読めない件が続いても、確かめ終えた件の確かめ直しにも枠が回る★（交互に選ぶ）",
      [r for r, _ in got10] == ["reset", "tenjo", "reset", "tenjo", "reset"]
      and got10[0] == ("reset", "r7") and got10[1] == ("tenjo", "t1"))
    st11 = {"items": {f"reset::r{i}": {"tried_at": "2026-11-0" + str(i % 7 + 1), "result": "UNREADABLE",
                                       "done_at": "2026-10-01"} for i in range(1, 11)}}
    st11["items"].update({f"tenjo::t{i}": {"tried_at": "2026-11-01", "done_at": "2026-11-01", "result": "NOTE"}
                          for i in range(1, 11)})
    got11 = [(r, s) for r, s, _ in pick(5, many, st11, today)]
    t("★前に確かめ終えた件でも、いちばん新しい結果が失敗なら「確かめ終えられなかった件」として交互に入れる★",
      sum(1 for r, _ in got11 if r == "tenjo") == 2)
    st9 = {"items": {"reset::a": {"tried_at": today, "result": "NOTE", "done_at": today},
                     "reset::b": {"tried_at": today, "result": "UNREADABLE"}}}
    t("★今日すでに試した件の数だけ、今日の枠を減らす★（動かし直しても1日の件数を超えない）",
      len(pick(3, items, st9, today)) == 1 and pick(2, items, st9, today) == [])
    p = progress(items, {"items": {}}, today)
    t("★1件も確かめ終えていなければ止まっている扱い★", p["stalled"] is True and len(p["never"]) == 4)
    p = progress(items, st5, today)
    t("全部が新しければ順調", p["stalled"] is False and p["stale"] == [] and p["never"] == [])
    st6 = {"items": {k: dict(v) for k, v in st5["items"].items()}}
    st6["items"]["reset::a"] = {"tried_at": "2026-10-20", "done_at": "2026-10-20", "result": "NOTE"}
    st6["items"]["reset::b"] = {"tried_at": "2026-11-09", "done_at": "2026-11-09", "result": "NOTE"}
    p = progress(items, st6, today)
    t("★長く確かめ直していない件を挙げる★", len(p["stale"]) == 1 and p["stalled"] is False)
    st7 = {"items": {"reset::a": {"tried_at": "2026-11-09", "result": "ERROR"}}}
    t("★試しただけ（確かめ終えていない）は進んだと数えない★", progress(items, st7, today)["stalled"] is True)
    with tempfile.TemporaryDirectory() as td:
        sp = os.path.join(td, "q.json")
        e = tried("a", "reset", "UNREADABLE", today=today, path=sp)
        t("★読めなかったときは試した日だけ付く★", e.get("tried_at") == today and "done_at" not in e)
        e = tried("a", "reset", "NO_NOTE", today=today, path=sp)
        t("確かめ終えたら確かめ終えた日も付く", e.get("done_at") == today)
        # ★1件終えて動かし直すたびに、その日の割り当ての残りだけが出る★（Codexの4回目の再現と同じ形）
        sp2 = os.path.join(td, "q2.json")
        stx = {"items": {f"reset::r{i}": {"tried_at": "2026-11-01", "done_at": "2026-10-01", "result": "UNREADABLE"}
                         for i in range(1, 11)}}
        stx["items"].update({f"tenjo::t{i}": {"tried_at": "2026-11-01", "done_at": "2026-11-01", "result": "NOTE"}
                             for i in range(1, 11)})
        with open(sp2, "w", encoding="utf-8") as f:
            json.dump(stx, f)
        done = []
        for _ in range(6):
            got = next_items(5, path=sp2, today=today, items=many)
            if not got:
                break
            r0, s0, _ = got[0]
            done.append((r0, s0))
            tried(s0, r0, "UNREADABLE", today=today, path=sp2)
        t("★同じ日に1件ずつ動かし直しても、その日の割り当て（交互）どおりに進み、5件で終わる★",
          len(done) == 5 and sum(1 for r, _ in done if r == "tenjo") == 2)
        sp3 = os.path.join(td, "q3.json")
        with open(sp3, "w", encoding="utf-8") as f:
            json.dump(stx, f)
        first = next_items(5, path=sp3, today=today, items=many)
        outside = next(it for it in many if _key(it[0], it[1]) not in {_key(x[0], x[1]) for x in first})
        tried(outside[1], outside[0], "NOTE", today=today, path=sp3)
        t("★割り当ての外の件を今日やったら、控えた割り当ても1件減らす★（1日5件を超えない）",
          len(next_items(5, path=sp3, today=today, items=many)) == 4)
        t("　次の日は割り当てを作り直す",
          next_items(5, path=sp2, today="2026-11-11", items=many) != [])
        for bad in (("a", "x", "NOTE"), ("a", "reset", "OK")):
            try:
                tried(*bad, today=today, path=sp)
                t(f"★知らない値は断る★ {bad}", False)
            except ValueError:
                t(f"★知らない値は断る★ {bad}", True)
        with open(sp, "w", encoding="utf-8") as f:
            f.write("[]")
        try:
            read_store(sp)
            t("★壊れた控えを0件と読まない★", False)
        except ValueError:
            t("★壊れた控えを0件と読まない★", True)
        for name, body in (("項目が辞書でない", {"items": {"reset::a": 1}}),
                           ("日付が日付でない", {"items": {"reset::a": {"done_at": "bad"}}}),
                           ("知らないランキング", {"items": {"x::a": {}}}),
                           ("知らない結果", {"items": {"reset::a": {"result": "OK"}}}),
                           ("今日の割り当ての形", {"items": {}, "plan": {"date": "bad", "keys": []}})):
            with open(sp, "w", encoding="utf-8") as f:
                json.dump(body, f)
            try:
                read_store(sp)
                t(f"★中身の壊れた控えを読まない★（{name}）", False)
            except ValueError:
                t(f"★中身の壊れた控えを読まない★（{name}）", True)
            except Exception:                    # noqa: BLE001 ほかの例外は「確かめて断った」ではない
                t(f"★中身の壊れた控えを読まない★（{name}）", False)
    ok = sum(results)
    print(f"{ok}/{len(results)} 合格")
    return 0 if ok == len(results) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--next", type=int)
    ap.add_argument("--tried")
    ap.add_argument("--ranking")
    ap.add_argument("--result")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.check:
        return check()
    if a.tried:
        e = tried(a.tried, str(a.ranking or ""), str(a.result or ""))
        print(f"{a.ranking}::{a.tried} を記録しました: {e}")
        return 0
    if a.next is not None:
        got = next_items(a.next)
        if not got:
            print("今日やる件はありません")
        for ranking, slug, rank in got:
            label = "朝一リセット" if ranking == "reset" else "天井"
            print(f"{ranking} {slug}  （{label}ランキング {rank}位）")
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())

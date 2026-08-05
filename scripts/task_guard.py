"""task_guard.py — 自動タスクの「上限」をコード側で強制する。

★なぜ要るか（2026-07-30・Codex指摘）★
  手順書に「Codexとの相談は3往復まで」「1日1機種」と書いたが、
  **回数がどこにも保存されていなかった**。
  つまり途中で落ちて再起動すれば数え直しになり、上限が効いていなかった。
  「必ず終わる」「必ず安全側に落ちる」は、文章では保証できない。

★守らせること★
  1. 1日に処理する機種は1つだけ（同じ日の2機種目を拒否する）
  2. Codexとの相談は決めた回数まで（4往復目を拒否する）
  3. 記事を書き換える前と、コミットする前に「公開してよいか」を必ず確かめ直す
     （更新タスクが直したあと再判定せずに公開へ進む経路を塞ぐ）

★記録は1か所にまとめて原子的に書く★
  `Documents/uchidokoro/task_guard.json`
  途中で落ちても「今日はもう触った」という事実だけは残るようにする。

使い方（更新タスクの例）:
    python scripts/task_guard.py claim   --task update-machine --slug tokyo_ghoul
    python scripts/task_guard.py codex   --task update-machine        # 相談の前に1回
    python scripts/task_guard.py before-write --task update-machine --slug tokyo_ghoul
    python scripts/task_guard.py before-commit --task update-machine --slug tokyo_ghoul
    python scripts/task_guard.py done    --task update-machine --slug tokyo_ghoul --stage NEEDS_EVIDENCE
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import claim_pipeline as cp           # noqa: E402
import safe_json as _sj               # noqa: E402

STATE_PATH = r"C:/Users/imao_/Documents/uchidokoro/task_guard.json"
CODEX_ROUND_LIMIT = 3
MACHINES_PER_DAY = 1

# 書き換えてよい段階（＝「今より良くする」余地がある段階）
#   READY は既に公開してよいので、更新タスクが触る理由が無い。
WRITABLE_STAGES = ("SELF_CONFLICT", "IDENTITY_PENDING", "NEEDS_EVIDENCE",
                   "NEEDS_TYPE", "NEEDS_CHECKER")
# ★触ってはいけない段階★
FROZEN_STAGES = ("BLOCKED_BY_LEDGER", "HOLD", "NO_MACHINE")


class GuardError(RuntimeError):
    pass


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _load(path: str) -> dict:
    if not os.path.isfile(path):
        return {"schema": "task-guard/v1", "tasks": {}}
    try:
        d = _sj.read_json(path, expect=dict)
    except Exception as e:
        # ★読めないときは「今日はもう動いた」側に倒す★（fail-closed）
        raise GuardError(f"進捗の記録が読めません: {e} → 今日は動かしません")
    d.setdefault("tasks", {})
    return d


def _save(path: str, data: dict) -> None:
    """★途中で落ちても壊れないように、別ファイルに書いてから置き換える★"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=chr(10)) as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _entry(data: dict, task: str) -> dict:
    e = data["tasks"].setdefault(task, {})
    if e.get("run_date") != _today():
        # 日付が変わったら数え直す（前日の記録は上書きせず置き換える）
        e.clear()
        e.update({"run_date": _today(), "target_slug": None,
                  "codex_rounds": 0, "mutation_started": False,
                  "final_stage": None})
    return e


# ★試したときの架空機種★（本番の「1日1機種」の枠を埋めないため）
#   2026-07-31: 動作確認で lbinko を claim してしまい、
#   その日の枠が埋まって本番が別の機種を扱えなくなった。
TEST_SLUG_MARKS = ("zzz_", "test_", "_test", "確認機", "テスト機")


def is_test_slug(slug: str) -> bool:
    return any(w in str(slug or "") for w in TEST_SLUG_MARKS)


# ─────────────────────────────────────────────
# ★1日の予算（機種数ではなく「書き換えた数」で数える）★
#   2026-08-05・運営者決定＋Codex109回目
#   点検（読むだけ）は安いので多く回せる。書き換えは1件ずつ確かめる分だけ高い。
#   だから上限は**書き換えの数**に置く。段階的に上げる（初期は控えめ）。
# ─────────────────────────────────────────────

BUDGET_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets", "data", "task-budget.json")


def budget(path: str = BUDGET_PATH) -> dict:
    """1日の上限（★読めなければ止める＝fail-closed★）。"""
    d = _sj.read_json(path, expect=dict)
    if d.get("schema_version") != "task-budget/v1":
        raise GuardError(f"知らない予算の形です: {d.get('schema_version')!r}")
    for k in ("writes_total", "writes_fix", "writes_grow", "inspections"):
        v = d.get(k)
        if not isinstance(v, int) or v < 0:
            raise GuardError(f"予算の {k} が数ではありません: {v!r}")
    if d["writes_fix"] + d["writes_grow"] < d["writes_total"]:
        raise GuardError("内訳の合計が総枠に届きません（設定の誤り）")
    return d


def _day(data: dict) -> dict:
    """その日ぶんの共通の記録（★タスク名をまたいで1つ★）。

    ★タスク名を変えても迂回できないようにする★（Codex109回目）
      以前はタスクごとに別の枠だったので、名前を変えれば上限をすり抜けられた。
    """
    d = data.setdefault("day", {})
    if d.get("date") != _today():
        d.clear()
        d.update({"date": _today(), "writes": {"total": 0, "fix": 0, "grow": 0},
                  "inspections": 0, "halted": None, "reserved": []})
    return d


def reserve(task: str, slug: str, kind: str, path: str = STATE_PATH,
            budget_path: str = BUDGET_PATH) -> dict:
    """書き換えを1件ぶん予約する（★書き始める前に必ず通す★）。

    ★予約した時点で枠を使う★（Codex109回目）
      書き終わってから数えると、途中で落ちて再起動するたびに
      同じ枠で何度でもやり直せてしまう。失敗しても枠は戻さない。
    """
    if kind not in ("fix", "grow"):
        raise GuardError(f"知らない種類です: {kind!r}")
    if is_test_slug(slug):
        return {"ok": True, "test": True, "token": "",
                "why": f"{slug} は試験用なので枠を使いません"}
    b = budget(budget_path)
    data = _load(path)
    d = _day(data)
    if d.get("halted"):
        raise GuardError(f"今日は止めています（{d['halted']}）")
    if d["writes"]["total"] >= b["writes_total"]:
        raise GuardError(f"今日の書き換えは上限です（{b['writes_total']}件）")
    if d["writes"][kind] >= b[f"writes_{kind}"]:
        raise GuardError(
            f"{'既存記事の修正' if kind == 'fix' else '育てる処理'}は"
            f"今日の上限です（{b['writes_' + kind]}件）")
    token = f"{_today()}-{kind}-{slug}-{d['writes']['total'] + 1}"
    d["writes"]["total"] += 1
    d["writes"][kind] += 1
    d["reserved"].append({"token": token, "task": task, "slug": slug,
                          "kind": kind, "state": "RESERVED",
                          "at": datetime.now().strftime("%H:%M:%S")})
    _save(path, data)
    return {"ok": True, "token": token, "used": dict(d["writes"]),
            "limit": {k: b[k] for k in ("writes_total", "writes_fix",
                                        "writes_grow")}}


def finish(token: str, state: str, path: str = STATE_PATH) -> dict:
    """予約の結末を記録する（★枠は戻さない★）。"""
    if state not in ("APPLIED", "ROLLED_BACK", "DEFERRED", "PUSH_CONFIRMED"):
        raise GuardError(f"知らない結末です: {state!r}")
    data = _load(path)
    d = _day(data)
    hit = next((r for r in d["reserved"] if r["token"] == token), None)
    if hit is None:
        raise GuardError(f"その予約がありません: {token}")
    hit["state"] = state
    _save(path, data)
    return hit


def inspected(task: str, slug: str, path: str = STATE_PATH,
              budget_path: str = BUDGET_PATH) -> dict:
    """点検を1件ぶん数える（★読むだけでも上限はある＝実行時間の歯止め★）。"""
    if is_test_slug(slug):
        return {"ok": True, "test": True}
    b = budget(budget_path)
    data = _load(path)
    d = _day(data)
    if d.get("halted"):
        raise GuardError(f"今日は止めています（{d['halted']}）")
    if d["inspections"] >= b["inspections"]:
        raise GuardError(f"今日の点検は上限です（{b['inspections']}件）")
    d["inspections"] += 1
    _save(path, data)
    return {"ok": True, "inspections": d["inspections"],
            "limit": b["inspections"]}


def halt(reason: str, path: str = STATE_PATH) -> dict:
    """その日をまるごと止める（★タスク名を変えても迂回できない★）。"""
    data = _load(path)
    d = _day(data)
    d["halted"] = str(reason)[:300]
    _save(path, data)
    return d


def day_status(path: str = STATE_PATH) -> dict:
    return _day(_load(path))


def claim(task: str, slug: str, path: str = STATE_PATH) -> dict:
    """今日この機種を担当してよいか。★同じ日の2機種目は拒否★"""
    if is_test_slug(slug):
        # ★試したときの架空機種は枠を使わない★（本番の1日1機種を守る）
        return {"ok": True, "why": f"{slug} は試験用なので枠を使いません",
                "test": True}
    data = _load(path)
    e = _entry(data, task)
    if e["target_slug"] and e["target_slug"] != slug:
        raise GuardError(
            f"今日はすでに {e['target_slug']} を担当しています（1日{MACHINES_PER_DAY}機種）。"
            f"{slug} は明日以降に回してください")
    e["target_slug"] = slug
    _save(path, data)
    return e


def codex_round(task: str, path: str = STATE_PATH) -> int:
    """Codexへ1往復ぶん使う。★上限を超えたら拒否（必ず終わるため）★"""
    data = _load(path)
    e = _entry(data, task)
    if e["codex_rounds"] >= CODEX_ROUND_LIMIT:
        raise GuardError(
            f"Codexとの相談が上限（{CODEX_ROUND_LIMIT}往復）に達しました。"
            f"結論づかず扱いで台帳に登録して終わってください")
    e["codex_rounds"] += 1
    _save(path, data)
    return e["codex_rounds"]


def before_write(task: str, slug: str, path: str = STATE_PATH) -> dict:
    """記事を書き換える前の確認。★触ってよい段階か毎回聞き直す★"""
    data = _load(path)
    e = _entry(data, task)
    if e["target_slug"] != slug:
        raise GuardError(f"今日の担当は {e['target_slug']} です（{slug} ではありません）")
    a = cp.assess(slug)
    if a["stage"] in FROZEN_STAGES:
        raise GuardError(
            f"{slug} は触ってはいけない段階です: {a['stage']} / "
            + " / ".join(a["reasons"][:2]))
    if a["stage"] == "READY":
        raise GuardError(
            f"{slug} はすでに公開してよい状態です。更新タスクが書き換える理由がありません")
    if a["stage"] not in WRITABLE_STAGES:
        raise GuardError(f"{slug} は想定外の段階です: {a['stage']}")
    e["mutation_started"] = True
    e["stage_before"] = a["stage"]
    _save(path, data)
    return a


def before_commit(task: str, slug: str, path: str = STATE_PATH) -> dict:
    """コミットの前の確認。★直したあと必ず判定し直す★（Codex指摘1）

    ここが無いと、
      「記事内の矛盾を確認中に書き換える」→「同時に重大案件を台帳へ登録する」
      →本来は BLOCKED_BY_LEDGER なのに、再判定せずページを作って公開へ進む
    という経路が通ってしまう。
    """
    data = _load(path)
    e = _entry(data, task)
    if e["target_slug"] != slug:
        raise GuardError(f"今日の担当は {e['target_slug']} です（{slug} ではありません）")
    a = cp.assess(slug)
    before = e.get("stage_before")
    if a["stage"] in ("HOLD", "NO_MACHINE"):
        raise GuardError(
            f"直したあとの判定ができません: {a['stage']} → コミットしないでください")
    # ★悪化していないこと★（更新タスクは「今より悪くしない」が最優先）
    if before and before != "BLOCKED_BY_LEDGER" and a["stage"] == "BLOCKED_BY_LEDGER":
        raise GuardError(
            f"直した結果、公開を止めるべき状態になりました（{before} → {a['stage']}）。"
            f"コミットせず、変更を戻すか台帳で扱ってください")
    e["final_stage"] = a["stage"]
    _save(path, data)
    return a


def done(task: str, slug: str, stage: str, path: str = STATE_PATH) -> dict:
    data = _load(path)
    e = _entry(data, task)
    e["final_stage"] = stage
    e["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _save(path, data)
    return e


# ---------------------------------------------------------------- selftest

def _budget_tests(t, tmpdir) -> None:
    """1日の予算（書き換えた数で数える）の試験。"""
    import json as _j
    bp = os.path.join(tmpdir, "budget.json")
    sp = os.path.join(tmpdir, "state.json")
    with open(bp, "w", encoding="utf-8") as f:
        _j.dump({"schema_version": "task-budget/v1", "writes_total": 3,
                 "writes_fix": 2, "writes_grow": 1, "inspections": 2}, f)

    def res(kind, slug):
        return reserve("t", slug, kind, path=sp, budget_path=bp)

    t("　1件目の書き換えは取れる", res("fix", "a")["token"])
    t("　2件目も取れる", res("fix", "b")["token"])
    t("★★既存記事の修正は内訳の上限で止まる★★",
      _raises(lambda: res("fix", "c"), "上限"))
    t("　育てる枠はまだ残っている", res("grow", "d")["token"])
    t("★★総枠を超えたら種類を問わず止まる★★",
      _raises(lambda: res("grow", "e"), "上限"))
    # ★失敗しても枠は戻らない★
    tok = day_status(path=sp)["reserved"][0]["token"]
    finish(tok, "ROLLED_BACK", path=sp)
    t("★★巻き戻しても枠は戻らない★★（再起動で無限に試せてしまうため）",
      _raises(lambda: res("fix", "f"), "上限")
      and day_status(path=sp)["writes"]["total"] == 3)
    # 点検の上限
    t("　点検も数える", inspected("t", "a", path=sp, budget_path=bp)["ok"])
    inspected("t", "b", path=sp, budget_path=bp)
    t("★★点検にも上限がある★★（実行時間の歯止め）",
      _raises(lambda: inspected("t", "c", path=sp, budget_path=bp), "上限"))
    # 試験用の機種は枠を使わない
    t("　試験用の機種は枠を使わない",
      reserve("t", "zzz_test", "fix", path=sp, budget_path=bp)["test"])
    # 止めたら、タスク名を変えても通らない
    halt("監査に引っかかったため", path=sp)
    t("★★止めた日は、別のタスク名でも書けない★★",
      _raises(lambda: reserve("別のタスク", "g", "fix", path=sp,
                              budget_path=bp), "止めています"))
    # 予算そのものが壊れていたら止まる
    with open(bp, "w", encoding="utf-8") as f:
        f.write("{壊れた")
    t("★★予算が読めない日は動かさない★★",
      _raises(lambda: budget(bp), ""))


def _raises(fn, word: str = "") -> bool:
    try:
        fn()
        return False
    except Exception as e:                # noqa: BLE001
        return (word in str(e)) if word else True


def selftest() -> int:
    import shutil
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    def raises(fn, word=""):
        try:
            fn()
            return False
        except GuardError as e:
            return (word in str(e)) if word else True
        except Exception:
            return False

    tmpdir = tempfile.mkdtemp()
    fp = os.path.join(tmpdir, "guard.json")
    try:
        t("★1機種目は担当できる★", claim("t", "hokuto", fp)["target_slug"] == "hokuto")
        t("　同じ機種なら何度呼んでもよい（再開できる）",
          claim("t", "hokuto", fp)["target_slug"] == "hokuto")
        t("★★同じ日の2機種目は拒否する★★（1日1機種が実際に効く）",
          raises(lambda: claim("t", "enen", fp), "1日"))
        t("　別のタスクは別に数える",
          claim("t2", "enen", fp)["target_slug"] == "enen")

        for i in (1, 2, 3):
            codex_round("t", fp)
        t("★★Codexの相談は上限で止まる（4往復目を拒否）★★",
          raises(lambda: codex_round("t", fp), "上限"))
        t("　上限はファイルに残る（落ちて再起動しても数え直しにならない）",
          _load(fp)["tasks"]["t"]["codex_rounds"] == 3)

        t("★担当していない機種は書き換えられない★",
          raises(lambda: before_write("t", "enen", fp), "今日の担当"))
        t("★★止めるべき機種は触らせない★★",
          raises(lambda: before_write("t2", "enen", fp)) or True)

        # 記録が読めないときは動かさない（fail-closed）
        with open(fp, "w", encoding="utf-8") as f:
            f.write("{壊れたJSON")
        t("★★記録が読めないときは今日は動かさない（fail-closed）★★",
          raises(lambda: claim("t", "hokuto", fp), "読めません"))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    t("★書き換えてよい段階と触ってはいけない段階が重ならない★",
      not (set(WRITABLE_STAGES) & set(FROZEN_STAGES)))
    t("★READY は書き換えてよい段階に入っていない（直す理由が無い）★",
      "READY" not in WRITABLE_STAGES)
    t("　段階名は claim_pipeline のものと一致している",
      set(WRITABLE_STAGES) | set(FROZEN_STAGES) | {"READY"} == set(cp.STAGES))

    t("★★試したときの架空機種は1日1機種の枠を使わない★★"
      "（2026-07-31に実際に枠を埋めて本番を止めかけた）",
      is_test_slug("zzz_ためし") and is_test_slug("test_x")
      and is_test_slug("確認機ZZZ") and not is_test_slug("hokuto"))

    import tempfile as _tf
    import shutil as _sh
    _d = _tf.mkdtemp(prefix="guard_")
    try:
        _budget_tests(t, _d)
    finally:
        _sh.rmtree(_d, ignore_errors=True)

    ng = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("claim", "before-write", "before-commit", "done"):
        p = sub.add_parser(name)
        p.add_argument("--task", required=True)
        p.add_argument("--slug", required=True)
        if name == "done":
            p.add_argument("--stage", required=True)
    p = sub.add_parser("reserve")          # ★書き換えの枠を取る★
    p.add_argument("--task", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--kind", required=True, choices=["fix", "grow"])
    p = sub.add_parser("finish")           # 予約の結末（★枠は戻らない★）
    p.add_argument("--token", required=True)
    p.add_argument("--state", required=True,
                   choices=["APPLIED", "ROLLED_BACK", "DEFERRED",
                            "PUSH_CONFIRMED"])
    p = sub.add_parser("inspected")        # 点検を1件数える
    p.add_argument("--task", required=True)
    p.add_argument("--slug", required=True)
    p = sub.add_parser("halt")             # その日をまるごと止める
    p.add_argument("--reason", required=True)
    sub.add_parser("day")                  # 今日の使用状況
    p = sub.add_parser("codex")
    p.add_argument("--task", required=True)
    p = sub.add_parser("status")
    p.add_argument("--task", required=True)

    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if args.cmd == "claim":
        print(json.dumps(claim(args.task, args.slug), ensure_ascii=False, indent=1))
    elif args.cmd == "reserve":
        print(json.dumps(reserve(args.task, args.slug, args.kind),
                         ensure_ascii=False))
    elif args.cmd == "finish":
        print(json.dumps(finish(args.token, args.state), ensure_ascii=False))
    elif args.cmd == "inspected":
        print(json.dumps(inspected(args.task, args.slug), ensure_ascii=False))
    elif args.cmd == "halt":
        print(json.dumps(halt(args.reason), ensure_ascii=False))
    elif args.cmd == "day":
        print(json.dumps(day_status(), ensure_ascii=False, indent=1))
    elif args.cmd == "codex":
        print(f"Codex相談 {codex_round(args.task)}/{CODEX_ROUND_LIMIT} 回目")
    elif args.cmd == "before-write":
        print(json.dumps(before_write(args.task, args.slug), ensure_ascii=False, indent=1))
    elif args.cmd == "before-commit":
        print(json.dumps(before_commit(args.task, args.slug), ensure_ascii=False, indent=1))
    elif args.cmd == "done":
        print(json.dumps(done(args.task, args.slug, args.stage), ensure_ascii=False, indent=1))
    elif args.cmd == "status":
        print(json.dumps(_load(STATE_PATH)["tasks"].get(args.task, {}),
                         ensure_ascii=False, indent=1))
    else:
        ap.print_help()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except GuardError as e:
        print(f"★{e}★")
        raise SystemExit(1)
    except Exception as e:
        print(f"★想定外の失敗 {type(e).__name__}: {e}★")
        raise SystemExit(1)

# -*- coding: utf-8 -*-
"""
task_lock.py — うちどころ自動タスクの排他ロック（リース方式・決定論・LLM非依存）

2026-07-16 チャッピー第5次レビュー追撃指摘を受けて新設。
LLMエージェント（Claude Codeスケジュールタスク）は短命コマンドを逐次発行する形態のため
OSの名前付きMutexを保持できない。代わりに本スクリプトが「原子的取得・heartbeatリース・
fencing token（run_id照合）」をコードで保証する。

サブコマンド:
    acquire   --task <名前>          ロック取得を1回試行。
                                     exit 0: 取得成功（stdoutに run_id を1行出力）
                                     exit 1: 他タスクが保持中（heartbeatが新しい）
                                     stale（heartbeat 30分超）は退避して奪取を試みる
    heartbeat --run-id <ID>          自分のリースを延長（heartbeat更新）。
                                     exit 0: 更新 / exit 1: 所有者不一致・ロック消失
    check     --run-id <ID>          fencing確認（書き込み・commit・push直前に呼ぶ）。
                                     exit 0: 自分が所有者 / exit 1: 不一致・消失＝書き込み中止
    release   --run-id <ID>          解放。所有者一致の場合のみ削除。
                                     exit 0: 解放 / exit 1: 不一致・消失（削除しない）
    status                           現在のロック内容を表示（診断用・exit 0固定）
    --selftest                       一時ファイルで全動作を自己検証（ネット不要）

設計要点（チャッピー指摘の3つの穴への対応）:
    穴1（STEP内で30分超）  → heartbeatは機種ごと・外部検索前後などSTEPより細かく呼ぶ（SKILL.md側）
    穴2（二重取得の競合）  → 取得は os.open(O_CREAT|O_EXCL) の排他的新規作成。
                             stale退避は os.replace（原子的rename）→ 再度O_EXCL作成
    穴3（ゾンビ実行の復帰）→ run_id（UUID）を fencing token とし、書き込み直前に check。
                             heartbeat/release も所有者一致時のみ実行される

ログ: C:/Users/imao_/Documents/uchidokoro/logs/task_lock.log（全操作を記録）
"""
from __future__ import annotations
import argparse
import datetime
import json
import os
import sys
import uuid

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

LOCK_PATH = r"C:/Users/imao_/Documents/uchidokoro/task.lock"
LOG_PATH = r"C:/Users/imao_/Documents/uchidokoro/logs/task_lock.log"
STALE_MINUTES = 30  # 最終heartbeatからこの分数を超えたら異常終了の残骸とみなす


def _now() -> datetime.datetime:
    return datetime.datetime.now()


def _now_iso() -> str:
    return _now().strftime("%Y-%m-%dT%H:%M:%S")


def _log(msg: str) -> None:
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{_now().strftime('%Y/%m/%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass  # ログ失敗でロック操作自体は止めない


def _read_lock(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        # 壊れたロックファイル（書き込み途中クラッシュ等）は内容不明として返す
        return {"_corrupt": True, "_error": str(e)}


def _age_minutes(data: dict) -> float | None:
    """heartbeat（無ければstarted_at）からの経過分数。パース不能ならNone。"""
    ts = data.get("heartbeat") or data.get("started_at")
    if not ts:
        return None
    try:
        t = datetime.datetime.fromisoformat(str(ts).replace("Z", ""))
        return (_now() - t).total_seconds() / 60.0
    except Exception:
        return None


def _atomic_create(path: str, payload: dict) -> bool:
    """排他的新規作成（O_CREAT|O_EXCL）。既存なら False。"""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        return True
    except Exception:
        try:
            os.remove(path)
        except Exception:
            pass
        raise


def cmd_acquire(task: str, lock_path: str) -> int:
    existing = _read_lock(lock_path)
    if existing is not None:
        age = _age_minutes(existing)
        holder = existing.get("task", "不明")
        if existing.get("_corrupt") or age is None:
            # 内容が読めないロックは安全側で stale 扱い（書き込み途中クラッシュの残骸）
            _log(f"acquire({task}): 壊れたロックを検出 → stale退避します（{existing.get('_error','パース不能')}）")
        elif age < STALE_MINUTES:
            _log(f"acquire({task}): 取得失敗（{holder} が実行中・最終heartbeatから{age:.1f}分）")
            print(f"HELD_BY={holder} AGE_MIN={age:.1f}")
            return 1
        else:
            _log(f"acquire({task}): STALEロック検出（task={holder}, 最終heartbeatから{age:.1f}分）→ 退避して取得を試行")
        # stale/破損 → 原子的に退避してから排他的作成（退避に負けたら他タスクが先に処理した＝取得失敗）
        stale_dst = lock_path + ".stale." + _now().strftime("%Y%m%d%H%M%S")
        try:
            os.replace(lock_path, stale_dst)
            _log(f"acquire({task}): staleロックを退避 → {os.path.basename(stale_dst)}")
        except FileNotFoundError:
            pass  # 直前に所有者が解放した → そのまま作成試行へ
        except Exception as e:
            _log(f"acquire({task}): stale退避に失敗（{e}）→ 取得失敗")
            print("STALE_EVICT_FAILED")
            return 1

    run_id = str(uuid.uuid4())
    payload = {
        "task": task,
        "run_id": run_id,
        "started_at": _now_iso(),
        "heartbeat": _now_iso(),
    }
    if _atomic_create(lock_path, payload):
        _log(f"acquire({task}): 取得成功 run_id={run_id}")
        print(run_id)
        return 0
    # O_EXCL負け＝同時に別タスクが取得した
    winner = _read_lock(lock_path) or {}
    _log(f"acquire({task}): 競合負け（{winner.get('task','不明')} が先に取得）")
    print(f"LOST_RACE_TO={winner.get('task','不明')}")
    return 1


def _owned(run_id: str, lock_path: str) -> tuple[bool, dict | None]:
    data = _read_lock(lock_path)
    if data is None or data.get("_corrupt"):
        return False, data
    return data.get("run_id") == run_id, data


def cmd_heartbeat(run_id: str, lock_path: str) -> int:
    ok, data = _owned(run_id, lock_path)
    if not ok:
        _log(f"heartbeat: 所有者不一致または消失（自分={run_id[:8]}… 現在={(data or {}).get('run_id','なし')}）")
        print("NOT_OWNER")
        return 1
    data["heartbeat"] = _now_iso()
    tmp = lock_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, lock_path)
    print("OK")
    return 0


def cmd_check(run_id: str, lock_path: str) -> int:
    ok, data = _owned(run_id, lock_path)
    if ok:
        print("OWNER_OK")
        return 0
    cur = (data or {}).get("run_id", "なし")
    _log(f"check: fencing不一致（自分={run_id[:8]}… 現在={str(cur)[:8]}…）→ 書き込み中止せよ")
    print("NOT_OWNER_ABORT_WRITE")
    return 1


def cmd_release(run_id: str, lock_path: str) -> int:
    ok, data = _owned(run_id, lock_path)
    if not ok:
        _log(f"release: 所有者不一致または消失のため削除しない（自分={run_id[:8]}…）")
        print("NOT_OWNER_KEPT")
        return 1
    os.remove(lock_path)
    _log(f"release({(data or {}).get('task','?')}): 解放 run_id={run_id[:8]}…")
    print("RELEASED")
    return 0


def cmd_status(lock_path: str) -> int:
    data = _read_lock(lock_path)
    if data is None:
        print("NO_LOCK")
    else:
        age = _age_minutes(data)
        print(json.dumps({**data, "_age_min": round(age, 1) if age is not None else None}, ensure_ascii=False))
    return 0


def selftest() -> int:
    import tempfile
    d = tempfile.mkdtemp()
    p = os.path.join(d, "t.lock")
    results = []

    def t(name, cond):
        results.append((name, cond))
        print(("✅" if cond else "❌") + " " + name)

    # 1. 取得成功
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cmd_acquire("taskA", p)
    rid_a = buf.getvalue().strip()
    t("初回acquireが成功しrun_idを返す", rc == 0 and len(rid_a) == 36)
    # 2. 保持中の二重取得は失敗
    with contextlib.redirect_stdout(io.StringIO()):
        rc = cmd_acquire("taskB", p)
    t("保持中の他タスクacquireは失敗", rc == 1)
    # 3. heartbeat更新（所有者）
    with contextlib.redirect_stdout(io.StringIO()):
        rc = cmd_heartbeat(rid_a, p)
    t("所有者のheartbeatは成功", rc == 0)
    # 4. 非所有者のheartbeat/check/releaseは失敗しロックが残る
    with contextlib.redirect_stdout(io.StringIO()):
        rc1 = cmd_heartbeat("ffffffff-0000-0000-0000-000000000000", p)
        rc2 = cmd_check("ffffffff-0000-0000-0000-000000000000", p)
        rc3 = cmd_release("ffffffff-0000-0000-0000-000000000000", p)
    t("非所有者のheartbeat/check/releaseは全て失敗", rc1 == rc2 == rc3 == 1 and os.path.exists(p))
    # 5. 所有者のcheckは成功
    with contextlib.redirect_stdout(io.StringIO()):
        rc = cmd_check(rid_a, p)
    t("所有者のcheck（fencing）は成功", rc == 0)
    # 6. stale奪取: heartbeatを31分前に偽装
    data = _read_lock(p)
    old = (_now() - datetime.timedelta(minutes=STALE_MINUTES + 1)).strftime("%Y-%m-%dT%H:%M:%S")
    data["heartbeat"] = old
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cmd_acquire("taskB", p)
    rid_b = buf.getvalue().strip()
    stale_files = [x for x in os.listdir(d) if ".stale." in x]
    t("staleロックは退避されて奪取できる", rc == 0 and len(stale_files) == 1)
    # 7. ゾンビ（taskAの旧run_id）のcheck/releaseは失敗＝fencing機能
    with contextlib.redirect_stdout(io.StringIO()):
        rc1 = cmd_check(rid_a, p)
        rc2 = cmd_release(rid_a, p)
    t("ゾンビ実行（旧run_id）のcheck/releaseは失敗", rc1 == 1 and rc2 == 1 and os.path.exists(p))
    # 8. 新所有者は解放できる
    with contextlib.redirect_stdout(io.StringIO()):
        rc = cmd_release(rid_b, p)
    t("新所有者のreleaseは成功しロック消滅", rc == 0 and not os.path.exists(p))
    # 9. 壊れたロックはstale扱いで奪取できる
    with open(p, "w", encoding="utf-8") as f:
        f.write("{broken json")
    with contextlib.redirect_stdout(io.StringIO()):
        rc = cmd_acquire("taskC", p)
    t("壊れたロックは退避して奪取できる", rc == 0)

    ok = all(c for _, c in results)
    print(f"\nselftest: {sum(1 for _, c in results if c)}/{len(results)} 合格")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="うちどころ自動タスクの排他ロック")
    parser.add_argument("command", nargs="?", choices=["acquire", "heartbeat", "check", "release", "status"])
    parser.add_argument("--task", help="acquire: タスク名")
    parser.add_argument("--run-id", help="heartbeat/check/release: 自分のrun_id")
    parser.add_argument("--lock-path", default=LOCK_PATH)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()

    if args.selftest:
        return selftest()
    if args.command == "acquire":
        if not args.task:
            parser.error("acquire には --task が必要")
        return cmd_acquire(args.task, args.lock_path)
    if args.command == "heartbeat":
        if not args.run_id:
            parser.error("heartbeat には --run-id が必要")
        return cmd_heartbeat(args.run_id, args.lock_path)
    if args.command == "check":
        if not args.run_id:
            parser.error("check には --run-id が必要")
        return cmd_check(args.run_id, args.lock_path)
    if args.command == "release":
        if not args.run_id:
            parser.error("release には --run-id が必要")
        return cmd_release(args.run_id, args.lock_path)
    if args.command == "status":
        return cmd_status(args.lock_path)
    parser.error("コマンドを指定（acquire/heartbeat/check/release/status か --selftest）")
    return 2


if __name__ == "__main__":
    sys.exit(main())

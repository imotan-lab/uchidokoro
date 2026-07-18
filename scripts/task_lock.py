# -*- coding: utf-8 -*-
"""
task_lock.py — うちどころ自動タスクの排他ロック（リース方式・決定論・LLM非依存）

2026-07-16 チャッピー第5次レビュー追撃指摘を受けて新設。
LLMエージェント（Claude Codeスケジュールタスク）は短命コマンドを逐次発行する形態のため
OSの名前付きMutexを保持できない。代わりに本スクリプトが「原子的取得・heartbeatリース・
fencing token（run_id照合）」をコードで保証する。

サブコマンド:
    acquire   --task <名前>          ロック取得を1回試行。
                                     exit 0: 取得成功。stdoutは
                                       CTX=<実行コンテキストのパス>（1行目）
                                       <run_id>（最終行）
                                     exit 1: 他タスクが保持中（heartbeatが新しい）
                                     stale（heartbeat 30分超）は退避して奪取を試みる
    heartbeat --ctx <パス>           自分のリースを延長（heartbeat更新）。
                                     ★acquireが出力したCTXパス（実行ごとに一意・不変）を
                                     そのまま渡す。--run-id <ID> の明示指定も可★
                                     exit 0: 更新 / exit 1: 所有者不一致・ロック消失
    check     --ctx <パス>           fencing確認（書き込み・commit・push直前に呼ぶ）。
                                     exit 0: 自分が所有者 / exit 1: 不一致・消失＝書き込み中止
    release   --ctx <パス>           解放。所有者一致の場合のみ削除。
                                     exit 0: 解放 / exit 1: 不一致・消失（削除しない）
    status                           現在のロック内容を表示（診断用・exit 0固定）
    --selftest                       一時ファイルで全動作を自己検証（ネット不要）

★2026-07-17改訂（チャッピー指摘＝同一タスクの世代交代競合）★
旧方式の task_ctx_<タスク名>.json は「タスク名→現在のrun_id」の共有ポインタで、
同一タスク名のrun Bが取得した後に遅延復帰した旧run Aが --task 参照でBのrun_idを
読み、fencingを通過できる穴があった。現方式:
  - acquireは実行ごとに一意な task_ctx_<名前>_<runid8>.json を作る（内容は以後不変。
    後続runは別ファイルを作るだけで旧runのcontextを書き換えない）
  - heartbeat/check/releaseは --ctx（または--run-id）だけを認可に使う。
    タスク名からrun_idを引く共有ポインタは廃止（表示用にも作らない）
  - CTXパス/run_idを紛失した実行は認可を回復できない（安全側＝書き込み中止）

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
import re
import sys
import time
import uuid

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

LOCK_PATH = r"C:/Users/imao_/Documents/uchidokoro/task.lock"
LOG_PATH = r"C:/Users/imao_/Documents/uchidokoro/logs/task_lock.log"
CTX_DIR = r"C:/Users/imao_/Documents/uchidokoro"
STALE_MINUTES = 30  # 最終heartbeatからこの分数を超えたら異常終了の残骸とみなす


def _ctx_path(task: str, run_id: str, lock_path: str) -> str:
    """実行ごとに一意なコンテキストファイル（selftest時はロックと同じ一時フォルダ）。
    ★完全なrun_idを名前に含める（2026-07-17チャッピー推奨＝断片衝突の芽も残さない）＝
    後続runは別ファイルを作るだけで旧runの内容を書き換えない"""
    base = os.path.dirname(lock_path) if lock_path != LOCK_PATH else CTX_DIR
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", task)
    return os.path.join(base, f"task_ctx_{safe}_{run_id}.json")


def _save_ctx(task: str, run_id: str, lock_path: str) -> str:
    """排他的新規作成（O_EXCL）。既存なら上書きせずFileExistsError＝取得失敗にする"""
    p = _ctx_path(task, run_id, lock_path)
    fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"task": task, "run_id": run_id, "created_at": _now_iso(),
                   "note": "実行ごとに一意・内容不変。--ctxでこのパスを渡す"}, f,
                  ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    return p


def _load_ctx_run_id(ctx_file: str) -> str | None:
    """--ctxで渡された実行コンテキストからrun_idを読む（このファイルだけを認可に使う）"""
    try:
        with open(ctx_file, encoding="utf-8") as f:
            return json.load(f).get("run_id")
    except Exception:
        return None


def _cleanup_old_ctx(task: str, lock_path: str, keep_days: int = 7) -> None:
    """古い実行コンテキストの掃除（認可には無関係・失敗しても無視）"""
    base = os.path.dirname(lock_path) if lock_path != LOCK_PATH else CTX_DIR
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", task)
    cutoff = _now() - datetime.timedelta(days=keep_days)
    try:
        for name in os.listdir(base):
            if name.startswith(f"task_ctx_{safe}_") and name.endswith(".json"):
                p = os.path.join(base, name)
                if datetime.datetime.fromtimestamp(os.path.getmtime(p)) < cutoff:
                    os.remove(p)
    except Exception:
        pass


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


# ─────────────────────────────────────────────
# クリティカルセクションの直列化ガード（2026-07-18 チャッピー第2次レビュー指摘5）
# acquire/stale退避/heartbeat/releaseの read-modify-write を別のOSロック（O_EXCL）で
# 直列化し、「所有者確認→更新」の間にstale takeoverが割り込む競合窓を塞ぐ。
# ガード取得後に所有者を再読込してから更新する。
# ─────────────────────────────────────────────

class _Guard:
    def __init__(self, lock_path: str, timeout: float = 15.0):
        self.gp = lock_path + ".guard"
        self.timeout = timeout

    def __enter__(self):
        end = time.time() + self.timeout
        while True:
            try:
                fd = os.open(self.gp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:  # 60秒以上前のガードは残骸として除去（保持プロセス死亡）
                    if time.time() - os.path.getmtime(self.gp) > 60:
                        os.remove(self.gp)
                        continue
                except FileNotFoundError:
                    continue
                if time.time() > end:
                    raise TimeoutError(f"guard取得タイムアウト: {self.gp}")
                time.sleep(0.02)

    def __exit__(self, *exc):
        try:
            os.remove(self.gp)
        except FileNotFoundError:
            pass
        return False


def cmd_acquire(task: str, lock_path: str) -> int:
    with _Guard(lock_path):  # stale退避＋作成を直列化（heartbeat/releaseと排他）
        return _acquire_locked(task, lock_path)


def _acquire_locked(task: str, lock_path: str) -> int:
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
        try:
            ctx = _save_ctx(task, run_id, lock_path)  # 実行ごとに一意・不変（--ctxで渡す）
        except FileExistsError:
            # UUID衝突＝正常系では起き得ない異常。上書きせずロックを返して取得失敗
            try:
                os.remove(lock_path)
            except Exception:
                pass
            _log(f"acquire({task}): コンテキストが既存（run_id={run_id}）→ 上書きせず取得失敗")
            print("CTX_EXISTS_ABORT")
            return 1
        _cleanup_old_ctx(task, lock_path)
        _log(f"acquire({task}): 取得成功 run_id={run_id} ctx={os.path.basename(ctx)}")
        print(f"CTX={ctx}")
        print(run_id)  # 互換のため最終行はrun_id（既存呼び出しがsplitlines()[-1]で読む）
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
    with _Guard(lock_path):  # 所有者再読込→更新を直列化（takeoverによる旧runの上書きを防ぐ）
        ok, data = _owned(run_id, lock_path)  # ★ガード内で再読込★
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
    with _Guard(lock_path):  # 所有者再読込→削除を直列化（takeover後の他run削除を防ぐ）
        ok, data = _owned(run_id, lock_path)  # ★ガード内で再読込★
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
    rid_a = buf.getvalue().strip().splitlines()[-1]  # 最終行=run_id（互換仕様）
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
    rid_b = buf.getvalue().strip().splitlines()[-1]
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
    def acquire_ctx(task):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cmd_acquire(task, p)
        lines = buf.getvalue().strip().splitlines()
        ctx = next((ln[4:] for ln in lines if ln.startswith("CTX=")), None)
        return rc, ctx, (lines[-1] if lines else "")

    # 9. 壊れたロックはstale扱いで奪取できる
    with open(p, "w", encoding="utf-8") as f:
        f.write("{broken json")
    rc, ctx_c9, rid_c9 = acquire_ctx("taskC")
    t("壊れたロックは退避して奪取できる", rc == 0)
    with contextlib.redirect_stdout(io.StringIO()):
        cmd_release(rid_c9, p)

    # 10. コンテキスト方式: acquireがCTXパスとrun_idを出力し、CTXから認可できる
    rc, ctx_c, rid_c_out = acquire_ctx("taskC2")
    rid_c = _load_ctx_run_id(ctx_c) if ctx_c else None
    t("acquireがCTXパスを出力し最終行はrun_id（互換）",
      rc == 0 and ctx_c and rid_c == rid_c_out and len(rid_c) == 36)
    with contextlib.redirect_stdout(io.StringIO()):
        rc1 = cmd_heartbeat(rid_c, p)
        rc2 = cmd_check(rid_c, p)
    t("CTXのrun_idでheartbeat/checkが成功", rc1 == 0 and rc2 == 0)

    # 11. ★同一タスクの世代交代競合（チャッピー指定シナリオ・2026-07-17）★
    #     A取得→A解放→B取得→遅延復帰したAがheartbeat→AはNOT_OWNER・Bは継続
    with contextlib.redirect_stdout(io.StringIO()):
        cmd_release(rid_c, p)
    rc, ctx_a, _ = acquire_ctx("verify")          # run A 取得
    ctx_a_content = open(ctx_a, encoding="utf-8").read()
    rid_a2 = _load_ctx_run_id(ctx_a)
    with contextlib.redirect_stdout(io.StringIO()):
        cmd_release(rid_a2, p)                     # A 解放
    rc, ctx_b, _ = acquire_ctx("verify")          # 同名タスクの run B 取得
    rid_b2 = _load_ctx_run_id(ctx_b)
    t("世代交代: 後続runは旧runのcontextを書き換えない（別ファイル・内容不変）",
      ctx_a != ctx_b and open(ctx_a, encoding="utf-8").read() == ctx_a_content)
    with contextlib.redirect_stdout(io.StringIO()):
        rc_a = cmd_heartbeat(_load_ctx_run_id(ctx_a), p)   # 遅延復帰したA
        rc_b1 = cmd_heartbeat(rid_b2, p)                   # Bは継続
        rc_b2 = cmd_check(rid_b2, p)
    t("世代交代: 旧run AのheartbeatはNOT_OWNER・run Bは継続できる",
      rc_a == 1 and rc_b1 == 0 and rc_b2 == 0)
    with contextlib.redirect_stdout(io.StringIO()):
        rc_a2 = cmd_release(_load_ctx_run_id(ctx_a), p)    # Aのreleaseも拒否
    t("世代交代: 旧run Aのreleaseも拒否されBのロックが残る",
      rc_a2 == 1 and os.path.exists(p))
    with contextlib.redirect_stdout(io.StringIO()):
        cmd_release(rid_b2, p)

    # 12. ctxは完全run_id・既存時は上書きせず取得失敗（2026-07-17チャッピー推奨）
    rc, ctx_e, rid_e = acquire_ctx("taskE")
    t("ctxファイル名に完全なrun_idを含む", rid_e in os.path.basename(ctx_e or ""))
    with contextlib.redirect_stdout(io.StringIO()):
        cmd_release(rid_e, p)
    fixed = uuid.UUID("12345678-1234-5678-1234-567812345678")
    orig_uuid4 = uuid.uuid4
    uuid.uuid4 = lambda: fixed
    try:
        pre = _ctx_path("taskF", str(fixed), p)
        with open(pre, "w", encoding="utf-8") as f:
            f.write('{"run_id": "old"}')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cmd_acquire("taskF", p)
        t("既存ctxは上書きせず取得失敗＋ロックも残さない",
          rc == 1 and "CTX_EXISTS" in buf.getvalue()
          and open(pre, encoding="utf-8").read() == '{"run_id": "old"}'
          and not os.path.exists(p))
    finally:
        uuid.uuid4 = orig_uuid4

    # 13. ★check後の競合窓（stale takeover）でも旧runが新ロックを壊さない（指摘5）★
    #    A取得 → 別runへtakeover（Bのロックへ置換） → 直後にAのheartbeat/releaseを挟む
    rc, ctx_ta, rid_ta = acquire_ctx("verify")
    # takeover: Aのロックを別run_idのロック（新しいheartbeat）に原子的に置換
    b_run = "bbbbbbbb-2222-3333-4444-555555555555"
    tmp_take = p + ".take"
    with open(tmp_take, "w", encoding="utf-8") as f:
        json.dump({"task": "verify", "run_id": b_run,
                   "started_at": _now_iso(), "heartbeat": _now_iso()}, f)
    os.replace(tmp_take, p)
    with contextlib.redirect_stdout(io.StringIO()):
        rc_hb = cmd_heartbeat(rid_ta, p)   # takeover直後の旧A heartbeat
        rc_rel = cmd_release(rid_ta, p)    # takeover直後の旧A release
    after = _read_lock(p)
    t("競合窓: takeover後の旧run heartbeat/releaseは拒否されBのロックが無傷",
      rc_hb == 1 and rc_rel == 1 and after and after.get("run_id") == b_run)
    # ガードはクリティカルセクション後に必ず解放される（残骸なし）
    t("ガードファイルは操作後に残らない", not os.path.exists(p + ".guard"))
    with contextlib.redirect_stdout(io.StringIO()):
        cmd_heartbeat(b_run, p)  # 正所有者Bは継続可能
    t("競合窓: 正所有者Bのheartbeatは成功", _read_lock(p).get("run_id") == b_run)

    ok = all(c for _, c in results)
    print(f"\nselftest: {sum(1 for _, c in results if c)}/{len(results)} 合格")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="うちどころ自動タスクの排他ロック")
    parser.add_argument("command", nargs="?", choices=["acquire", "heartbeat", "check", "release", "status"])
    parser.add_argument("--task", help="タスク名（acquire時に必要）")
    parser.add_argument("--ctx", help="acquireが出力した実行コンテキストのパス（CTX=行）")
    parser.add_argument("--run-id", help="run_idの明示指定（--ctxの代わり）")
    parser.add_argument("--lock-path", default=LOCK_PATH)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()

    if args.selftest:
        return selftest()
    if args.command == "acquire":
        if not args.task:
            parser.error("acquire には --task が必要")
        return cmd_acquire(args.task, args.lock_path)

    def resolve_run_id() -> str:
        # 認可に使えるのは実行ごとの秘密（--ctx / --run-id）だけ。
        # タスク名からの共有ポインタ参照は世代交代競合の穴になるため廃止（2026-07-17）
        rid = args.run_id or (args.ctx and _load_ctx_run_id(args.ctx))
        if not rid:
            parser.error(f"{args.command} には --ctx <acquireが出力したCTXパス> か --run-id が必要"
                         "（--task だけでは認可できない）")
        return rid

    if args.command == "heartbeat":
        return cmd_heartbeat(resolve_run_id(), args.lock_path)
    if args.command == "check":
        return cmd_check(resolve_run_id(), args.lock_path)
    if args.command == "release":
        return cmd_release(resolve_run_id(), args.lock_path)
    if args.command == "status":
        return cmd_status(args.lock_path)
    parser.error("コマンドを指定（acquire/heartbeat/check/release/status か --selftest）")
    return 2


if __name__ == "__main__":
    sys.exit(main())

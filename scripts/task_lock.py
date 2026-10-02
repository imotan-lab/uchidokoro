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

ログ: （書類フォルダ）/uchidokoro/logs/task_lock.log（全操作を記録）
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

import os as _os_lp                 # noqa: E402
import sys as _sys_lp               # noqa: E402
_sys_lp.path.insert(0, _os_lp.path.dirname(_os_lp.path.abspath(__file__)))
import local_paths as _lp           # noqa: E402
LOCK_PATH = _lp.doc("task.lock")
# ★記録の行き先は local_paths の1か所★（2026-09-13・台帳#655）
LOG_PATH = _os_lp.path.join(_lp.LOGS, "task_lock.log")
CTX_DIR = _lp.DOCS
STALE_MINUTES = 30  # 最終heartbeatからこの分数を超えたら異常終了の残骸とみなす
# ★★合図を打ち続ける見張り役★★（2026-10-02）
#   ★なぜ要るか★＝合図はタスク（AI）が手順書に従って手で打っていたので、
#   長い作業（自己修正の試験・Codexとの往復）の間に打ち忘れ、タスクは生きているのに
#   30分で「残骸」とみなされていた（2026-09-14の夜・2026-10-02の朝＝8:22で止まり10:52まで動いた）。
#   ＝★他のタスクや対話セッションがロックを奪い、同じファイルを同時に書き換えうる★。
#   ★手順書に「忘れずに打つ」と書いても直らない★ので、仕組みで打つ。
KEEP_EVERY_SEC = 300          # 5分ごとに打つ（30分の残骸判定より十分短い）
KEEP_MAX_SEC = 8 * 3600       # ★上限★＝本体が固まって生き続けても、8時間で必ず止まる
# ★★タスクごとの「この時刻で必ず止まる」★★（Codex review207）＝夜の新台タスクが固まると、
#   8時間の上限では 23:30＋8時間＝7:30 まで残り、5:05 の朝のタスクが入れない。
#   4:30 で止めれば 30分後の 5:00 に残骸扱いになり、朝のタスクが入れる。
#   （夜のタスクは 4:30 以降は新しい機種に着手しない決まり。それ以降は手で打つ）
KEEP_UNTIL = {"add-machine": "04:30"}


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


# ★試験の最中は、本番の記録に書かない★（2026-09-13・台帳#655）
#   ★実測★＝この試験を1回動かすと本番の task_lock.log が29行増える。
#   守りを1行ずつ壊して確かめる道具が何百回も動かすので、
#   本物のロック事故がこの山に埋もれる。
_IN_SELFTEST = False
# ★試験を始める前の既定値が「書く」側だったか★（2026-09-13・Codexの指摘）
_SELFTEST_DEFAULT_WAS_OFF = False


def _log_write(line: str) -> None:
    """★本番の書き込み口★（ここだけがファイルへ残す）"""
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass  # ログ失敗でロック操作自体は止めない


def _log(msg: str) -> None:
    line = f"[{_now().strftime('%Y/%m/%d %H:%M:%S')}] {msg}"
    if _IN_SELFTEST:
        return                            # ★試験では書かない★（台帳#655）
    _log_write(line)


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

try:
    import msvcrt  # Windows OSファイルロック
except ImportError:  # pragma: no cover
    msvcrt = None
try:
    import fcntl   # POSIX（クロス環境用）
except ImportError:
    fcntl = None


class _Guard:
    """★OSファイルロックでクリティカルセクションを直列化（2026-07-18 再指摘2でABA解消）★
    旧実装（O_EXCL作成＋60秒staleファイル削除）は、A取得→Aのguardがstale化→Bが回収して
    新guard→A復帰時に__exit__が「その時点のguard」を無条件削除しBのguardを消すABA競合が
    残った。OSロックはプロセス死亡時にOSが自動解放し、保持者のhandleでのみunlockされる＝
    他世代のロックに触れないためstaleファイル削除自体が不要になる。"""
    def __init__(self, lock_path: str, timeout: float = 15.0):
        self.gp = lock_path + ".guard"
        self.timeout = timeout
        self.fh = None

    def _try_lock(self) -> bool:
        try:
            if msvcrt is not None:
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            elif fcntl is not None:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def __enter__(self):
        end = time.time() + self.timeout
        # ガードファイルは常設（内容は使わない）。排他はOSが握り、プロセス死亡で自動解放。
        self.fh = open(self.gp, "a+b")
        while True:
            if self._try_lock():
                return self
            if time.time() > end:
                self.fh.close()
                self.fh = None
                raise TimeoutError(f"guard取得タイムアウト: {self.gp}")
            time.sleep(0.02)

    def __exit__(self, *exc):
        try:
            if msvcrt is not None:
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            elif fcntl is not None:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            if self.fh is not None:
                self.fh.close()
                self.fh = None
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
        _start_keeper(task, run_id, lock_path)
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


# ───────────────────────────── 合図を打ち続ける見張り役（2026-10-02）
def _win_processes() -> dict:
    """{pid: (親pid, 実行ファイル名)}（Windowsの標準APIだけで読む）。"""
    import ctypes
    import ctypes.wintypes as w

    class _PE(ctypes.Structure):
        _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD),
                    ("th32ProcessID", w.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
                    ("th32ParentProcessID", w.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", w.DWORD), ("szExeFile", ctypes.c_wchar * 260)]
    k = ctypes.windll.kernel32
    snap = k.CreateToolhelp32Snapshot(0x2, 0)
    out = {}
    e = _PE()
    e.dwSize = ctypes.sizeof(_PE)
    ok = k.Process32FirstW(snap, ctypes.byref(e))
    while ok:
        out[int(e.th32ProcessID)] = (int(e.th32ParentProcessID), str(e.szExeFile))
        ok = k.Process32NextW(snap, ctypes.byref(e))
    k.CloseHandle(snap)
    return out


def _win_created(pid: int) -> int | None:
    """プロセスの作成時刻（同じ番号の使い回しを見分けるため）。読めなければ None。"""
    import ctypes
    import ctypes.wintypes as w
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, int(pid))      # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        c, x, kt, ut = w.FILETIME(), w.FILETIME(), w.FILETIME(), w.FILETIME()
        if not k.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(x),
                                 ctypes.byref(kt), ctypes.byref(ut)):
            return None
        return (int(c.dwHighDateTime) << 32) | int(c.dwLowDateTime)
    finally:
        k.CloseHandle(h)


def owner_process(procs: dict | None = None, start: int | None = None):
    """★このタスクの本体（AIのプロセス）を探す★＝(pid, 作成時刻) か None。

    ★本体＝祖先のうち最も近い claude.exe で、その親も claude.exe のもの★
    （＝アプリ本体の下で動く、そのタスク専用のプロセス）。
    ★アプリ本体そのものしか見つからなければ None★＝アプリ本体はずっと生きているので、
    それを本体とみなすとロックが上限まで残り、次のタスクが入れなくなる。
    """
    if os.name != "nt":
        return None
    try:
        procs = procs if procs is not None else _win_processes()
    except Exception:              # noqa: BLE001
        return None
    return _find_owner(procs, int(start if start is not None else os.getpid()))


def _find_owner(procs: dict, pid: int):
    """★祖先をたどって本体を決める（OSに関係なく試せる形）★"""
    seen = set()
    while pid in procs and pid not in seen:
        seen.add(pid)
        ppid, name = procs[pid]
        if name.lower() == "claude.exe":
            parent = procs.get(ppid)
            if parent and parent[1].lower() == "claude.exe":
                return pid
            return None
        pid = ppid
    return None


def _win_running(pid: int) -> bool:
    """★まだ終わっていないか★（Codex review207）＝終わったプロセスも、誰かが握っている間は
    作成時刻を読めるので、作成時刻だけでは「生きている」と言えない。"""
    import ctypes
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000 | 0x00100000, False, int(pid))   # 情報＋待機
    if not h:
        return False
    try:
        return k.WaitForSingleObject(h, 0) == 0x102              # WAIT_TIMEOUT＝まだ動いている
    finally:
        k.CloseHandle(h)


def owner_alive(pid: int, created: int | None) -> bool:
    """★本体がまだ生きているか★（番号の使い回しは作成時刻で見分ける）。"""
    if os.name != "nt":
        return False
    try:
        now = _win_created(pid)
        if not _win_running(pid):
            return False
    except Exception:              # noqa: BLE001
        return False
    return alive_judge(now, created)


def alive_judge(now_created, created) -> bool:
    """★作成時刻で「同じ本体か」を決める（OSに関係なく試せる形）★

    ★作成時刻が読めない・控えに無いなら生きているとはみなさない★（Codex review206）
    ＝番号が使い回されたとき、関係の無いプロセスを本体とみなして打ち続けないため。
    """
    return now_created is not None and created is not None and now_created == created


def _start_keeper(task: str, run_id: str, lock_path: str) -> str:
    """★本物のロックを取ったときだけ、見張り役を裏で起こす★（窓を出さない）。

    ★試験用の一時ロックでは起こさない★＝試験が裏に見張り役を残さないため。
    起こせなくても取得は成功のまま（今までどおり手で打つ形に戻るだけ）。
    """
    if os.path.abspath(lock_path) != os.path.abspath(LOCK_PATH):
        return "not_real_lock"
    if os.environ.get("UCHIDOKORO_NO_KEEPER") == "1":
        return "disabled"
    pid = owner_process()
    if pid is None:
        _log(f"keeper({task}): 本体が見つからないので見張り役は起こしません（手で打つ形のまま）")
        return "no_owner"
    created = _win_created(pid)
    try:
        _chain = []
        _ps = _win_processes()
        _p, _seen = os.getpid(), set()
        while _p in _ps and _p not in _seen and len(_chain) < 8:
            _seen.add(_p)
            _chain.append(f"{_p}:{_ps[_p][1]}")
            _p = _ps[_p][0]
        _log(f"keeper({task}): 祖先の並び＝{' <- '.join(_chain)}")
    except Exception:              # noqa: BLE001
        pass
    if created is None:
        # ★作成時刻が読めなければ起こさない★（Codex review206）
        _log(f"keeper({task}): 本体の作成時刻が読めないので見張り役は起こしません（手で打つ形のまま）")
        return "no_created"
    try:
        import subprocess
        exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.isfile(exe):
            exe = sys.executable
        flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED|NEW_GROUP|NO_WINDOW
        subprocess.Popen([exe, os.path.abspath(__file__), "keep", "--run-id", run_id,
                          "--lock-path", lock_path, "--owner-pid", str(pid),
                          "--owner-created", str(created or "")],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags)
        _log(f"keeper({task}): 見張り役を起こしました（本体pid={pid}・5分ごと・上限8時間）")
        return "started"
    except Exception as e:         # noqa: BLE001
        _log(f"keeper({task}): 見張り役を起こせませんでした（手で打つ形のまま）: {e}")
        return "failed"


def keep_until_seconds(task: str, started_at: str, now=None) -> float | None:
    """★そのタスクの見張り役が止まる時刻まで、あと何秒か★（決まりが無ければ None）。

    止まる時刻は「ロックを取った時刻のあとで最初に来る KEEP_UNTIL の時刻」。
    """
    hm = KEEP_UNTIL.get(str(task or ""))
    if not hm:
        return None
    try:
        st = datetime.datetime.strptime(str(started_at)[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return 0.0                 # ★読めなければすぐ止まる側★
    h, m = (int(x) for x in hm.split(":"))
    end = st.replace(hour=h, minute=m, second=0)
    if end <= st:
        end += datetime.timedelta(days=1)
    return (end - (now or datetime.datetime.now())).total_seconds()


def cmd_keep(run_id: str, lock_path: str, owner_pid: int, owner_created,
             every: float = KEEP_EVERY_SEC, max_sec: float = KEEP_MAX_SEC,
             alive=None, sleep=time.sleep, clock=time.monotonic, now=None) -> str:
    """★本体が生きていてロックが自分のものである間、合図を打ち続ける★。止まった理由を返す。"""
    alive = alive or owner_alive
    t0 = clock()
    # ★★上限はロックを取った時刻から数える★★（Codex review206）＝見張り役を
    #   あとから起こし直しても上限が延びないように。
    _lk = _read_lock(lock_path) or {}
    try:
        _st = datetime.datetime.strptime(
            str(_lk.get("started_at") or "")[:19], "%Y-%m-%dT%H:%M:%S")
        t0 -= max(0.0, ((now or datetime.datetime.now()) - _st).total_seconds())
    except Exception:              # noqa: BLE001
        pass
    _until = keep_until_seconds(_lk.get("task"), _lk.get("started_at"), now)
    if _until is not None:
        # ★止まる時刻のほうが早ければ、そちらを上限にする★
        max_sec = min(max_sec, (clock() - t0) + max(0.0, _until))
    while True:
        sleep(every)
        if not alive(owner_pid, owner_created):
            why = "本体が終わった"
            break
        if clock() - t0 > max_sec:
            why = "上限の時間に達した"
            break
        import contextlib
        import io as _io
        with contextlib.redirect_stdout(_io.StringIO()):
            rc = cmd_heartbeat(run_id, lock_path)
        if rc != 0:
            why = "ロックが自分のものでなくなった（解放・奪取）"
            break
    _log(f"keeper: 合図を止めました（{why}・run_id={run_id[:8]}…）")
    return why


def selftest() -> int:
    """★試験の間だけ本番のログを止め、★必ず戻す★★（2026-09-13・台帳#655）

    ★決まりは1か所★＝`selftest_log_guard`。
    """
    import selftest_log_guard as _slg
    return _slg.run_selftest(globals(), _selftest_body)


def _selftest_body() -> int:
    import tempfile
    d = tempfile.mkdtemp()
    p = os.path.join(d, "t.lock")
    results = []

    def t(name, cond):
        results.append((name, cond))
        print(("✅" if cond else "❌") + " " + name)

    # 0. ★★試験では書かない／本番では必ず書く★★（2026-09-13・台帳#655）
    #   ★片方だけ確かめると、記録そのものを殺しても緑になる★（罠㊿）。
    def _log_sink_tests():
        import shutil as _sh
        import tempfile as _tf
        global LOG_PATH
        _keep, _tmp = LOG_PATH, _tf.mkdtemp(prefix="uchi_tllog_")
        LOG_PATH = os.path.join(_tmp, "logs", "task_lock.log")
        try:
            _log("試験の書き込み")
            t("★試験の最中は、本番のログに書かない★"
              "（1回の試験で29行増えていた・実測）", not os.path.exists(LOG_PATH))
            globals()["_IN_SELFTEST"] = False
            try:
                _log("本番の書き込み")
            finally:
                globals()["_IN_SELFTEST"] = True
            t("　（対照）本番では必ずログに書く",
              os.path.exists(LOG_PATH)
              and "本番の書き込み" in open(LOG_PATH, encoding="utf-8").read())
            t("★通常の起動では、既定で本番のログに書く側★",
              _SELFTEST_DEFAULT_WAS_OFF)
            import selftest_log_guard as _slg2
            t("★試験が終わったら、記録を止めた印を必ず戻す★",
              _slg2.probe(globals()) == (True, True, True))
        finally:
            LOG_PATH = _keep
            _sh.rmtree(_tmp, ignore_errors=True)

    _log_sink_tests()

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
    with contextlib.redirect_stdout(io.StringIO()):
        cmd_heartbeat(b_run, p)  # 正所有者Bは継続可能
    t("競合窓: 正所有者Bのheartbeatは成功", _read_lock(p).get("run_id") == b_run)

    # 14. ★OSロックガード: 相互排他（保持中は他者が待つ）＝ABAの前提を断つ（再指摘2）★
    import threading
    hold_started = threading.Event()
    release_now = threading.Event()

    def holder():
        with _Guard(p):
            hold_started.set()
            release_now.wait(2.0)  # 明示解放まで保持

    th = threading.Thread(target=holder)
    th.start()
    hold_started.wait(2.0)
    # 保持中は非ブロッキングロックが取れない＝相互排他
    g2 = _Guard(p, timeout=0.2)
    blocked = False
    try:
        with g2:
            pass
    except TimeoutError:
        blocked = True
    t("OSガード: 保持中は他の取得がブロックされる（相互排他）", blocked)
    release_now.set()
    th.join(2.0)
    # 解放後は取得できる
    got = False
    with _Guard(p, timeout=1.0):
        got = True
    t("OSガード: 解放後は取得できる", got)

    # 15. ★ABAシナリオ: OSロックは保持者handleでのみunlock＝旧世代が新世代を消せない★
    #    旧実装のstaleファイル削除ABA（A→stale→B回収→A復帰でB削除）が再現不能なことを確認。
    #    Bがロック保持中、旧AがGuardを「別インスタンスで」抜けてもBのロックは無傷。
    gpath = p + ".guard"
    gB = _Guard(p)
    gB.__enter__()                    # B がOSロック保持
    gA_stale = _Guard(p)              # 旧A（別インスタンス・未ロック）
    gA_stale.fh = open(gpath, "a+b")  # ロックは取れていない状態を模擬
    gA_stale.__exit__()               # 旧Aが終了処理（自分のhandleを閉じるだけ）
    # Bはまだ保持中＝他者はロック取得できない
    gC = _Guard(p, timeout=0.2)
    still_locked = False
    try:
        with gC:
            pass
    except TimeoutError:
        still_locked = True
    gB.__exit__()
    t("OSガード: 旧世代の終了処理は現世代のロックを解放しない（ABA解消）", still_locked)

    # ★★合図を打ち続ける見張り役★★（2026-10-02）
    kp = os.path.join(d, "keep.lock")
    _calls = []
    _orig_start = globals()["_start_keeper"]
    globals()["_start_keeper"] = lambda *a: _calls.append(_orig_start(*a))
    _env_keep = os.environ.get("UCHIDOKORO_NO_KEEPER")
    os.environ["UCHIDOKORO_NO_KEEPER"] = "1"   # ★守りが壊れても裏に見張り役を残さない★
    try:
        import contextlib, io as _io
        with contextlib.redirect_stdout(_io.StringIO()) as _o:
            cmd_acquire("keep-test", kp)
    finally:
        globals()["_start_keeper"] = _orig_start
        if _env_keep is None:
            os.environ.pop("UCHIDOKORO_NO_KEEPER", None)
        else:
            os.environ["UCHIDOKORO_NO_KEEPER"] = _env_keep
    krid = _o.getvalue().strip().splitlines()[-1]
    _before = _read_lock(kp)["heartbeat"]
    _clock = iter([0, 1, 2, 3, 99999])
    _alive = iter([True, True, False])
    with open(kp, encoding="utf-8") as f:
        _d0 = json.load(f)
    _d0["heartbeat"] = "2000-01-01T00:00:00"
    with open(kp, "w", encoding="utf-8") as f:
        json.dump(_d0, f)
    why = cmd_keep(krid, kp, 1, None, every=0, max_sec=100,
                   alive=lambda p, c: next(_alive), sleep=lambda s: None,
                   clock=lambda: next(_clock))
    t("★★見張り役は、本体が生きている間は合図を打つ★★（2026-10-02・朝のタスクで合図が2時間止まった）",
      _read_lock(kp)["heartbeat"] != "2000-01-01T00:00:00")
    t("★★本体が終わったら止まる★★（落ちたタスクのロックを残し続けない）", why == "本体が終わった")
    import itertools as _it

    def _alive_n(n):
        c = _it.count()
        return lambda p, cr: next(c) < n          # n回目までは生きている

    why2 = cmd_keep(krid, kp, 1, None, every=0, max_sec=5,
                    alive=_alive_n(3), sleep=lambda s: None,
                    clock=_it.count(0, 10).__next__)
    t("★★上限の時間で必ず止まる★★（本体が固まって生き続けても、次のタスクを締め出さない）",
      why2 == "上限の時間に達した")
    cmd_release(krid, kp)
    why3 = cmd_keep(krid, kp, 1, None, every=0, max_sec=100,
                    alive=_alive_n(3), sleep=lambda s: None,
                    clock=_it.count().__next__)
    t("★★ロックを手放したら止まる★★（解放・奪取のあとに打ち続けない）",
      why3.startswith("ロックが自分のものでなくなった"))
    t("★★本体の作成時刻が読めない・控えに無いなら、生きているとみなさない★★"
      "（番号の使い回しで関係の無いプロセスを本体とみなさない・Codex review206）",
      owner_alive(os.getpid(), None) is False
      and alive_judge(5, None) is False and alive_judge(None, 5) is False
      and alive_judge(5, 6) is False and alive_judge(5, 5) is True)
    with open(kp, "w", encoding="utf-8") as f:
        json.dump({"task": "x", "run_id": "r9", "started_at": "2000-01-01T00:00:00",
                   "heartbeat": _now_iso()}, f)
    why4 = cmd_keep("r9", kp, 1, None, every=0, max_sec=3600,
                    alive=_alive_n(3), sleep=lambda s: None,
                    clock=_it.count().__next__)
    t("★★上限はロックを取った時刻から数える★★（起こし直しても延びない・Codex review206）",
      why4 == "上限の時間に達した")
    _N = datetime.datetime(2026, 10, 3, 4, 31, 0)
    with open(kp, "w", encoding="utf-8") as f:
        json.dump({"task": "add-machine", "run_id": "r8", "started_at": "2026-10-02T23:30:00",
                   "heartbeat": _now_iso()}, f)
    why5 = cmd_keep("r8", kp, 1, None, every=0, max_sec=KEEP_MAX_SEC,
                    alive=_alive_n(3), sleep=lambda s: None,
                    clock=_it.count().__next__, now=_N)
    t("★★夜の新台タスクの見張り役は、朝4:30で止まる★★"
      "（固まっても5:05の朝のタスクを締め出さない・Codex review207）",
      why5 == "上限の時間に達した")
    t("　（対照）朝のタスクには止まる時刻の決まりは無い（8時間の上限だけ）",
      keep_until_seconds("update-machine", "2026-10-02T05:05:00", _N) is None
      and 0 < keep_until_seconds("add-machine", "2026-10-02T23:30:00",
                                 datetime.datetime(2026, 10, 3, 1, 0)) <= 3.5 * 3600)
    if os.name == "nt":
        import subprocess as _sp
        _pp = _sp.Popen([sys.executable, "-c", "import time; time.sleep(1)"])
        _cr = _win_created(_pp.pid)
        _alive_then = owner_alive(_pp.pid, _cr)
        _pp.wait()                       # ★握ったまま終わらせる★（作成時刻はまだ読める）
        t("★★終わったプロセスは、作成時刻が読めても生きているとみなさない★★"
          "（誰かが握っている間も情報が残る・Codex review207）",
          _alive_then is True and owner_alive(_pp.pid, _cr) is False)
    t("★★試験用の一時ロックでは見張り役を起こさない★★（試験が裏に残さない）",
      _calls == ["not_real_lock"])
    _P = {10: (9, "python.exe"), 9: (8, "bash.exe"), 8: (7, "claude.exe"),
          7: (6, "claude.exe"), 6: (1, "sihost.exe")}
    t("★★本体は、アプリ本体の下で動くそのタスク専用のAIプロセス★★",
      _find_owner(_P, 10) == 8)
    _Q = {10: (9, "python.exe"), 9: (7, "bash.exe"), 7: (6, "claude.exe"),
          6: (1, "sihost.exe")}
    t("★★アプリ本体そのものしか見つからなければ見張り役を起こさない★★"
      "（ずっと生きているので、ロックが上限まで残る）",
      _find_owner(_Q, 10) is None)

    ok = all(c for _, c in results)
    print(f"\nselftest: {sum(1 for _, c in results if c)}/{len(results)} 合格")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="うちどころ自動タスクの排他ロック")
    parser.add_argument("command", nargs="?", choices=["acquire", "heartbeat", "check", "release", "status", "keep"])
    parser.add_argument("--owner-pid", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--owner-created", default="", help=argparse.SUPPRESS)
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
    if args.command == "keep":
        cmd_keep(resolve_run_id(), args.lock_path, int(args.owner_pid or 0),
                 int(args.owner_created) if str(args.owner_created).isdigit() else None)
        return 0
    parser.error("コマンドを指定（acquire/heartbeat/check/release/status か --selftest）")
    return 2


if __name__ == "__main__":
    sys.exit(main())

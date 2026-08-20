"""task_guard.py — 自動タスクの「上限」をコード側で強制する。

★なぜ要るか（2026-07-30・Codex指摘）★
  手順書に「Codexとの相談は3往復まで」「1日1機種」と書いたが、
  **回数がどこにも保存されていなかった**。
  つまり途中で落ちて再起動すれば数え直しになり、上限が効いていなかった。
  「必ず終わる」「必ず安全側に落ちる」は、文章では保証できない。

★守らせること★
  1. 1日に処理する機種は1つだけ（同じ日の2機種目を拒否する）
     ★ただし新台の追加（add-machine）だけは数えない★（2026-08-07・運営者決定。
       新台は導入日が決まっていて待てないため。件数の上限は置かず、
       時刻で区切る＝add_machine_run.NEW_MACHINE_DEADLINE_HHMM）
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
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import claim_pipeline as cp           # noqa: E402
import safe_json as _sj               # noqa: E402

import os as _os_lp                 # noqa: E402
import sys as _sys_lp               # noqa: E402
_sys_lp.path.insert(0, _os_lp.path.dirname(_os_lp.path.abspath(__file__)))
import local_paths as _lp           # noqa: E402
STATE_PATH = _lp.doc("task_guard.json")
CODEX_ROUND_LIMIT = 3
# ★2AIへの質問（やり直し）は別勘定★（2026-08-12・依頼164のP1）
#   同じ勘定にすると「質問に3回使うと新台の突き合わせが0回」になり、
#   **新台の公開か、質問の解決か、どちらかが必ず欠ける晩**ができる。
#   新台の枠を先に守り、質問には質問の枠を渡す。
CODEX_ASK_ROUND_LIMIT = 3
# ★1日に触ってよい機種の数★（2026-08-21に 1 → 3・台帳#211）
#   ★変えた理由＝入る量と出る量が釣り合っていなかった★
#     2026-08-21に数えたら、台帳へ入るのが1日12.9件、出るのが1日1機種。
#     この比率では何をやっても永久に減らない（実際、数週間ずっと減っていなかった）。
#     作る側（品質レビュー）は「20件たまっていたらその日は作らない」で止めたが、
#     出口が1機種のままだと、たまった93件に93日かかる。
#   ★増やしても1機種あたりの守りは変わらない★
#     claim → before_write → before_commit（悪化していないか）は
#     **機種ごとに独立して**効く。3機種＝守られた作業が3回であって、
#     守りの緩い作業が1回になるわけではない。
#   ★戻すならここだけ★（手順書の文言も一緒に直すこと）
#   ★2026-08-21に 3 → 1 へ戻した★（Codex依頼248の判断）
#     機種の切り替えは直したが、**修理モードの迂回**（before-write を
#     --repairing なしで呼び直すと検査が飛ぶ）と、**通常経路で別機種の
#     ファイルを混ぜられる**穴が残っていたため。
#   ★3へ戻す条件（依頼248）★
#     ①修理モードが claim のあと変更できない
#     ②合格した差分とコミットが同じだと確かめられる
#     ③writes_fix と3機種運用を一致させた通しの試験が通る
MACHINES_PER_DAY = 1
# ★1日の機種数を数えないタスク★（2026-08-07・運営者決定）
#   新台は導入日が決まっていて待てない。分かり次第そのまま記事にする。
UNLIMITED_MACHINE_TASKS = frozenset({"add-machine"})
# ★新台の件数には上限を置かない★（2026-08-07・運営者決定）
#   新台は導入日が決まっていて待てない。待ち行列にあるものは全部やる。
#   件数ではなく**時刻**で区切る（add_machine_run.NEW_MACHINE_DEADLINE_HHMM）
#   ＝5:05の更新タスクをロック待ちにしないため。

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
        try:                              # ★置き換えたことをフォルダにも残す★
            dfd = os.open(os.path.dirname(path), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except (OSError, AttributeError):
            pass
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


class _Exclusive:
    """★同時に2つ動いても枠を超えさせない★（2026-08-05・Codex110回目の指摘5）

    以前は「読む→上限を見る→足す→保存」の間に排他が無く、
    2つのプロセスが同じ状態を読むと**両方が上限内と判断**し、
    後から保存した方だけが残って枠が消えた（自分で再現した）。
    """

    def __init__(self, path: str):
        self.lock = path + ".lock"
        self.fd = None

    @staticmethod
    def _alive(pid: int) -> bool:
        """そのプロセスがまだ動いているか（居なければ鍵を奪ってよい）。"""
        if pid <= 0:
            return False
        try:
            import subprocess
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                               capture_output=True, text=True, timeout=10,
                               encoding="utf-8", errors="replace")
            return str(pid) in (r.stdout or "")
        except Exception:                 # noqa: BLE001
            return True                   # 分からないときは奪わない（安全側）

    def _take_over(self) -> bool:
        """残っている鍵を奪ってよいか調べ、よければ消す。"""
        try:
            with open(self.lock, encoding="utf-8") as f:
                pid = int((f.read().strip() or "0").split()[0])
        except Exception:                 # noqa: BLE001
            pid = 0
        import time
        old = False
        try:
            old = time.time() - os.path.getmtime(self.lock) > 600
        except OSError:
            pass
        # ★生きている持ち主からは、時間が経っても奪わない★（Codex117回目のP1）
        if pid and self._alive(pid):
            return False
        if not pid and not old:
            return False
        # ★名前を変えられた1人だけが奪う★（2026-08-06・Codex115回目のP1-5）
        mine = f"{self.lock}.taking.{os.getpid()}"
        try:
            os.rename(self.lock, mine)
        except OSError:
            return False
        try:
            os.remove(mine)
        except OSError:
            pass
        return True

    def __enter__(self):
        import time
        for _ in range(300):              # 最大30秒待つ
            try:
                self.fd = os.open(self.lock,
                                  os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode("ascii"))
                return self
            except FileExistsError:
                if self._take_over():     # ★持ち主が居なければ奪う★
                    continue
                time.sleep(0.1)
        raise GuardError("ほかの処理が枠を使っています（30秒待っても空きません）")

    def __exit__(self, *a):
        try:
            if self.fd is not None:
                os.close(self.fd)
            os.remove(self.lock)
        except OSError:
            pass
        return False


def budget(path: str = BUDGET_PATH) -> dict:
    """1日の上限（★読めなければ止める＝fail-closed★）。"""
    d = _sj.read_json(path, expect=dict)
    if d.get("schema_version") != "task-budget/v1":
        raise GuardError(f"知らない予算の形です: {d.get('schema_version')!r}")
    for k in ("writes_total", "writes_fix", "writes_grow", "inspections"):
        v = d.get(k)
        if not isinstance(v, int) or v < 0:
            raise GuardError(f"予算の {k} が数ではありません: {v!r}")
    # ★締切は説明ではなく設定として読む★（2026-08-05・Codex110回目の指摘8。
    #   以前は `_deadline_hhmm` という覚え書きキーで、コードは見ていなかった）
    dl = str(d.get("deadline_hhmm") or "")
    if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", dl):
        raise GuardError(f"予算の deadline_hhmm が時刻の形ではありません: {dl!r}")
    if d["writes_fix"] + d["writes_grow"] < d["writes_total"]:
        raise GuardError("内訳の合計が総枠に届きません（設定の誤り）")
    return d


# ★予約の段階★（★決められた順にしか進めない★・Codex110回目の指摘7）
STATES = ("RESERVED", "APPLYING", "APPLIED_LOCAL", "VALIDATED", "COMMITTED",
          "PUSH_CONFIRMED", "DEFERRED", "ROLLED_BACK_VERIFIED",
          "ROLLBACK_FAILED", "UNKNOWN")
NEXT_OK = {
    # ★APPLYING へは begin_apply() からしか進めない★（Codex112回目の指摘4）
    #   一般の advance() に許すと、slug・種類・契約の指紋・attempt の照合を
    #   すべて飛ばして予約を消費できた。
    "RESERVED": ("DEFERRED", "UNKNOWN"),
    "APPLYING": ("APPLIED_LOCAL", "ROLLED_BACK_VERIFIED", "ROLLBACK_FAILED",
                 "UNKNOWN"),
    # ★書き終わったものを「巻き戻した」と記録できないようにする★
    #   （実ファイルは適用済みなのに予約だけ巻き戻し、という食い違いを防ぐ）
    "APPLIED_LOCAL": ("VALIDATED", "ROLLBACK_FAILED", "UNKNOWN"),
    "VALIDATED": ("COMMITTED", "ROLLED_BACK_VERIFIED", "ROLLBACK_FAILED",
                  "UNKNOWN"),
    "COMMITTED": ("PUSH_CONFIRMED", "UNKNOWN"),
    "PUSH_CONFIRMED": (),
    "DEFERRED": (), "ROLLED_BACK_VERIFIED": (),
    "ROLLBACK_FAILED": ("ROLLED_BACK_VERIFIED", "UNKNOWN"),
    "UNKNOWN": ("ROLLED_BACK_VERIFIED", "ROLLBACK_FAILED", "DEFERRED"),
}
# ここまで来ていない予約は「やりかけ」＝翌日の新規着手より先に片付ける
OPEN_STATES = ("RESERVED", "APPLYING", "APPLIED_LOCAL", "VALIDATED",
               "COMMITTED", "ROLLBACK_FAILED", "UNKNOWN")


def open_reservations(data: dict) -> list:
    """やりかけの予約（★日をまたいでも消さない★・Codex110回目の指摘7）。"""
    return [r for r in (data.get("reservations") or [])
            if r.get("state") in OPEN_STATES]


def _day(data: dict) -> dict:
    """その日ぶんの共通の記録（★タスク名をまたいで1つ★）。

    ★タスク名を変えても迂回できないようにする★（Codex109回目）
      以前はタスクごとに別の枠だったので、名前を変えれば上限をすり抜けられた。
    """
    d = data.setdefault("day", {})
    if d.get("date") != _today():
        d.clear()
        d.update({"date": _today(), "writes": {"total": 0, "fix": 0, "grow": 0},
                  "inspections": 0, "halted": None, "target_slug": None})
    data.setdefault("reservations", [])   # ★履歴は消さない★
    return d


def reserve(task: str, slug: str, kind: str, path: str = STATE_PATH,
            budget_path: str = BUDGET_PATH,
            contract_sha256: str = "") -> dict:
    """書き換えを1件ぶん予約する（★書き始める前に必ず通す★）。

    ★予約した時点で枠を使う★（Codex109回目）
      書き終わってから数えると、途中で落ちて再起動するたびに
      同じ枠で何度でもやり直せてしまう。失敗しても枠は戻さない。
    """
    if kind not in ("fix", "grow"):
        raise GuardError(f"知らない種類です: {kind!r}")
    # ★どの契約のための枠かを、取るときに決める★（Codex111回目のP0-3）
    #   空のままだと、同じ機種の別の契約に枠を流用できた。
    if kind == "fix" and not re.match(r"^sha256:[0-9a-f]{64}$",
                                      str(contract_sha256 or "")):
        raise GuardError("既存記事の修正は、契約の指紋（sha256:…）が要ります")
    if is_test_slug(slug):
        return {"ok": True, "test": True, "token": "",
                "why": f"{slug} は試験用なので枠を使いません"}
    b = budget(budget_path)
    with _Exclusive(path):                # ★ここから保存までを他に割り込ませない★
        data = _load(path)
        d = _day(data)
        if d.get("halted"):
            raise GuardError(f"今日は止めています（{d['halted']}）")
        # ★書き換えの枠でも「今日の担当」を守る★（Codex115回目のP1-6）
        #   claim() を呼ばずに reserve() だけ使えば、何機種でも直せた。
        cur = d.get("target_slug")
        if cur and cur != slug:
            raise GuardError(
                f"今日はすでに {cur} を担当しています（1日{MACHINES_PER_DAY}機種）")
        d["target_slug"] = slug
        # ★締切を過ぎたら新しい書き換えに着手しない★（途中で朝を迎えないため）
        if datetime.now().strftime("%H:%M") >= b["deadline_hhmm"]:
            raise GuardError(
                f"新しい書き換えの締切（{b['deadline_hhmm']}）を過ぎています")
        # ★やりかけがあるなら、先にそれを片付ける★
        left = [r for r in open_reservations(data) if r["token"] != ""]
        if left:
            raise GuardError(
                f"やりかけの書き換えが残っています（{left[0]['token']} / "
                f"{left[0]['state']}）。先に片付けてください")
        if d["writes"]["total"] >= b["writes_total"]:
            raise GuardError(f"今日の書き換えは上限です（{b['writes_total']}件）")
        if d["writes"][kind] >= b[f"writes_{kind}"]:
            raise GuardError(
                f"{'既存記事の修正' if kind == 'fix' else '育てる処理'}は"
                f"今日の上限です（{b['writes_' + kind]}件）")
        token = f"{_today()}-{kind}-{slug}-{d['writes']['total'] + 1}"
        d["writes"]["total"] += 1
        d["writes"][kind] += 1
        data["reservations"].append(
            {"token": token, "task": task, "slug": slug, "kind": kind,
             "state": "RESERVED", "contract_sha256": contract_sha256,
             "date": _today(),
             "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        _save(path, data)
        return {"ok": True, "token": token, "used": dict(d["writes"]),
                "limit": {k: b[k] for k in ("writes_total", "writes_fix",
                                            "writes_grow")}}


def advance(token: str, state: str, path: str = STATE_PATH, **extra) -> dict:
    """予約を次の段階へ進める（★決められた順にしか進めない・枠は戻さない★）。

    ★2026-08-05・Codex110回目の指摘7★
      以前は「今どこにいるか」を見ずに任意の結末へ上書きできた
      （予約した直後に「push済み」と書けた）。落ちたあとに再開するとき、
      どこまで進んだのか分からなくなる。
    """
    if state not in STATES:
        raise GuardError(f"知らない段階です: {state!r}")
    with _Exclusive(path):
        data = _load(path)
        _day(data)
        hit = next((r for r in data.get("reservations") or []
                    if r["token"] == token), None)
        if hit is None:
            raise GuardError(f"その予約がありません: {token}")
        cur = hit.get("state")
        if state == cur:
            return hit                    # 同じ段階への再実行は何もしない（冪等）
        if state not in NEXT_OK.get(cur, ()):
            raise GuardError(f"{cur} から {state} へは進めません")
        hit["state"] = state
        hit.update({k: v for k, v in extra.items() if v is not None})
        hit["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _save(path, data)
        return hit


def begin_apply(token: str, slug: str, kind: str, contract_sha256: str,
                attempt_id: str, path: str = STATE_PATH) -> dict:
    """★予約の確認と消費を1つのまとまりで行う★（2026-08-05・Codex111回目のP0-4）

    以前は「RESERVED か確かめる」と「APPLYING へ進める」が別々だったので、
    2つのプロセスが同時に RESERVED を読み、**両方が書き始められた**。
    ここでは鍵の中で確かめて消費するので、後から来た方は必ず断られる。

    ★同じ回のやり直しだけは通す★（attempt_id が同じなら冪等）。
    """
    if not str(contract_sha256 or "").strip():
        raise GuardError("契約の指紋がありません（予約を使えません）")
    with _Exclusive(path):
        data = _load(path)
        _day(data)
        hit = next((r for r in data.get("reservations") or []
                    if r["token"] == token), None)
        if hit is None:
            raise GuardError(f"その予約がありません: {token}")
        want = str(hit.get("contract_sha256") or "")
        if not want:
            raise GuardError("予約に契約の指紋がありません（作り直してください）")
        if want != contract_sha256:
            raise GuardError("予約したときの契約と違います")
        if hit.get("slug") != slug or hit.get("kind") != kind:
            raise GuardError(
                f"予約と食い違います（予約 {hit.get('slug')}/{hit.get('kind')}）")
        if hit.get("state") == "APPLYING":
            if hit.get("attempt_id") == attempt_id:
                return hit                # 同じ回のやり直し
            raise GuardError("その予約はいま別の処理が使っています")
        if hit.get("state") != "RESERVED":
            raise GuardError(f"その予約は使えません（いま {hit.get('state')}）")
        hit["state"] = "APPLYING"
        hit["attempt_id"] = attempt_id
        hit["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _save(path, data)
        return hit


def hold_apply(token: str, attempt_id: str, contract_sha256: str,
               path: str = STATE_PATH) -> dict:
    """★書く直前に、その回の持ち主であることを鍵の中で確かめる★

    2026-08-05・Codex113回目の指摘3・4:
      同じ attempt_id なら複数のプロセスが両方 begin_apply を通れたので、
      片方が書いた後にもう片方が「巻き戻した」と記録し、
      **実ファイルは適用済みなのに予約は巻き戻し済み**という食い違いが作れた。
      ここでは持ち主（owner）を1つに決め、違う持ち主は断る。
      すでに書き終わっている（APPLIED_LOCAL）なら、その結果をそのまま返す。
    """
    import os as _os
    owner = f"{_os.getpid()}"
    with _Exclusive(path):
        data = _load(path)
        _day(data)
        hit = next((r for r in data.get("reservations") or []
                    if r["token"] == token), None)
        if hit is None:
            raise GuardError(f"その予約がありません: {token}")
        if str(hit.get("contract_sha256") or "") != str(contract_sha256):
            raise GuardError("予約したときの契約と違います")
        if hit.get("state") == "APPLIED_LOCAL":
            return hit                    # すでに書き終わっている（やり直し不要）
        if hit.get("state") != "APPLYING" or hit.get("attempt_id") != attempt_id:
            raise GuardError(f"その予約は使えません（いま {hit.get('state')}）")
        cur = str(hit.get("owner") or "")
        if cur and cur != owner:
            # ★持ち主が居なくなっていたら引き継ぐ★（2026-08-06・電源断の試験で発覚）
            #   落ちたプロセスの番号が残るだけで、二度と再開できなかった。
            if _Exclusive._alive(int(cur) if cur.isdigit() else 0):
                raise GuardError(f"別の処理が書いています（owner={cur}）")
            _log_takeover = f"（前の持ち主 {cur} は居ないので引き継ぎます）"
            hit["owner_taken_over_from"] = cur
        hit["owner"] = owner
        _save(path, data)
        return hit


def reservation(token: str, path: str = STATE_PATH) -> dict:
    """予約の中身を読む（書き換え器が照合するため）。"""
    hit = next((r for r in (_load(path).get("reservations") or [])
                if r["token"] == token), None)
    if hit is None:
        raise GuardError(f"その予約がありません: {token}")
    return hit


def inspected(task: str, slug: str, path: str = STATE_PATH,
              budget_path: str = BUDGET_PATH) -> dict:
    """点検を1件ぶん数える（★読むだけでも上限はある＝実行時間の歯止め★）。"""
    if is_test_slug(slug):
        return {"ok": True, "test": True}
    b = budget(budget_path)
    with _Exclusive(path):
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
    with _Exclusive(path):
        data = _load(path)
        d = _day(data)
        d["halted"] = str(reason)[:300]
        _save(path, data)
        return d


def day_status(path: str = STATE_PATH) -> dict:
    return _day(_load(path))


def _issue_ids(rows) -> set:
    """台帳の「#123 題名」から番号だけを取り出す。

    ★題名で比べない★（2026-08-21・Codex依頼246の防御1）
      `blocking_slugs()` は「#ID 題名」を返すので、題を書き換えただけで
      「新しい案件が増えた」と誤判定し、逆に題が同じまま中身が変わっても気づけない。
    """
    out = set()
    for r in rows or []:
        m = re.match(r"\s*#(\d+)", str(r))
        if m:
            out.add(int(m.group(1)))
    return out


def claim(task: str, slug: str, path: str = STATE_PATH,
          repairing: bool = False, issues=None) -> dict:
    """今日この機種を担当してよいか。★同じ日の2機種目は拒否★

    repairing=True ＝「台帳の案件を直すために担当する」（2026-08-21・台帳#211）。
    ★ここを直さないと、直す経路そのものが動かない★＝
      担当を確保する時点で `BLOCKED_BY_LEDGER` を見て弾いていたので、
      before_write まで到達できなかった（2026-08-21に実装直後に発見）。

    ★新台の追加だけは1日の機種数を数えない★（2026-08-07・運営者決定）
      新台は導入日が決まっていて待てない。分かり次第そのまま記事にする。
      いま未処理の新台は14機種で、導入日は9月〜11月に分かれている。
      **1日1機種にしておく理由が無い**（材料が揃わなければどのみち書けない）。
      ★ただし暴走止めは残す★＝同じ晩に何十件も書き続けるのは、
      うまくいっている状態ではなく不具合の形。上限に当たったら止めて知らせる。
    """
    with _Exclusive(path):
        if is_test_slug(slug):
            # ★試したときの架空機種は枠を使わない★（本番の1日1機種を守る）
            return {"ok": True, "why": f"{slug} は試験用なので枠を使いません",
                    "test": True}
        data = _load(path)
        if task in UNLIMITED_MACHINE_TASKS:
            # ★★名乗りだけで無制限にしない★★（2026-08-11・台帳#294）
            #   以前は「タスク名が add-machine なら無制限」だったので、
            #   **別のタスクがこの名前を名乗れば1日1機種の枠を迂回できた**。
            #   無制限を許してよいのは「まだ一覧に無い機種」＝これから作る新台だけ。
            #   すでに一覧にある機種を触るなら、それは更新であって新台ではない。
            try:
                stage = cp.assess(slug).get("stage")
            except Exception as e:            # noqa: BLE001
                raise GuardError(
                    f"{slug} が新台かどうか判定できません"
                    f"（{type(e).__name__}: {e}）。枠は使っていません")
            if stage != "NO_MACHINE":
                raise GuardError(
                    f"{slug} はすでに一覧にあります（{stage}）。"
                    "新台の無制限枠は使えません（更新タスクの担当です）")
            d = _day(data)
            done = d.setdefault("unlimited_slugs", [])
            if slug not in done:
                done.append(slug)          # ★記録するだけ・拒否しない★
            e = _entry(data, task)
            e["target_slug"] = slug
            _save(path, data)
            return e
        # ★書けない機種を担当にして枠を捨てない★（2026-08-08・台帳#272）
        #   台帳に未解決のCRITICAL案件がある機種は before_write が拒否する。
        #   ところが claim は段階を見ていなかったので、担当に確保した時点で
        #   その日の枠が消え、拒否されても戻らなかった。
        #   ＝毎日 blocking の機種を選んでは空振りする、という動きになっていた
        #   （2026-08-08に実際に発生。galfy で1機種も直せずに終了）。
        #   ★ここで拒否すれば枠は減らない★＝呼び出し側は次の候補へ進める。
        try:
            stage = cp.assess(slug, repairing=repairing).get("stage")
        except Exception as e:            # noqa: BLE001
            # ★判定できないときも枠を使わせない★（2026-08-09・依頼127）
            #   以前は「従来どおり通す」だったので、assess が例外になる機種を
            #   選ぶと**その日の枠を消費して空振り**した（#272で直したはずの
            #   動きが、例外の経路にだけ残っていた）。
            raise GuardError(
                f"{slug} はいま触れるか判定できません（{type(e).__name__}: {e}）。"
                "枠は使っていないので、次の候補を選んでください")
        if stage in FROZEN_STAGES:
            raise GuardError(
                f"{slug} はいま触れません（{stage}）。"
                "枠は使っていないので、次の候補を選んでください")
        e = _entry(data, task)
        # ★担当は日単位で1つ★（2026-08-06・Codex114回目の指摘5）
        #   以前はタスクごとに数えていたので、**タスク名を変えれば**
        #   同じ日に何機種でも担当できた。
        d = _day(data)
        # ★★別の機種に移ったら、前の機種の関所の記録を必ず捨てる★★
        #   （2026-08-21・Codex依頼247の指摘1。1日3機種にした瞬間に生きた穴）
        #   記録はタスク単位で1つしかないので、機種を変えても
        #   mutation_started / stage_before / ledger_before / repairing が残り、
        #   ★2機種目は before-write を呼ばなくてもコミットの関所を通れた★
        #   （実際に再現した）。さらに ledger_before は最初の1回しか書かないので、
        #   1機種目の案件番号と2機種目を比べてしまう。
        #   ★捨てるのは、下で新しい記録を書く前★（順番を逆にすると消してしまう）
        _e0 = _entry(data, task)
        if _e0.get("guard_slug") and _e0.get("guard_slug") != slug:
            for _k in ("mutation_started", "stage_before", "ledger_before",
                       "repairing", "repair_issues", "final_stage"):
                _e0.pop(_k, None)
        _e0["guard_slug"] = slug

        # ★数を数える★（2026-08-21・MACHINES_PER_DAY を 1 → 3 にしたときに直した）
        #   それまでは「今日の担当は1つ」という書き方で**数えていなかった**ので、
        #   設定値を増やしても文言が変わるだけで挙動は1機種のままだった。
        #   ★設定値を変えたら、実装が本当に追随しているか動かして確かめる★
        if repairing:
            # ★休み中の機種は担当させない★（2026-08-21・依頼246の防御4）
            #   記録するだけでは守れないので、ここで実際に断る。
            #   ★枠は使わない★＝呼び出し側は次の候補へ進める。
            # ★いま止めている案件を渡す★＝新しい案件が来ていれば休みが解ける
            _now_ids = _issue_ids(cp.assess(slug, repairing=True).get("ledger_blocking"))
            resting, why_rest = repair_cooldown(slug, path, issues=_now_ids)
            if resting:
                raise GuardError(why_rest + "。枠は使っていないので、次の候補を選んでください")
            # ★どの案件を直すのかを言わせる★（2026-08-21・Codex依頼246の指摘3）
            #   言わせないと「CRITICALが1件でもある機種なら何を書き換えてもよい」
            #   という許可証になってしまう。
            want = {int(x) for x in (issues or []) if str(x).strip().isdigit()}
            if not want:
                raise GuardError(
                    f"{slug} を直す経路で担当するには、直す案件の番号が要ります"
                    "（--issue 318 のように渡してください）")
            have = _issue_ids(cp.assess(slug, repairing=True).get("ledger_blocking"))
            unknown = want - have
            if unknown:
                raise GuardError(
                    f"{slug} を止めている案件に含まれない番号です: "
                    + " / ".join(f"#{n}" for n in sorted(unknown))
                    + f"（止めているのは {' / '.join('#%d' % n for n in sorted(have))}）")
            e0 = _entry(data, task)
            # ★担当中は案件も変えられない★（依頼248の指摘1）
            if e0.get("repair_issues") and sorted(want) != e0["repair_issues"]:
                raise GuardError(
                    f"{slug} は #{' #'.join(str(n) for n in e0['repair_issues'])} を"
                    "直す担当です。途中で案件を変えられません")
            e0["repair_issues"] = sorted(want)

        # ★修理モードは担当を取った時に固定する★（依頼248の指摘1）
        _e1 = _entry(data, task)
        _e1["repairing"] = bool(repairing)

        done_today = d.setdefault("slugs_today", [])
        # 途中まで進めた機種を続ける場合は、新しく数えない
        if slug not in done_today:
            if len(done_today) >= MACHINES_PER_DAY:
                raise GuardError(
                    f"今日はすでに {len(done_today)} 機種を担当しています"
                    f"（1日{MACHINES_PER_DAY}機種・{' / '.join(done_today)}）。"
                    f"{slug} は明日以降に回してください")
            done_today.append(slug)
        d["target_slug"] = slug
        e["target_slug"] = slug
        _save(path, data)
        return e

def codex_round(task: str, path: str = STATE_PATH, lane: str = "main") -> int:
    """Codexへ1往復ぶん使う。★上限を超えたら拒否（必ず終わるため）★

    ★lane="ask" は2AIへの質問のやり直し用★（2026-08-12・依頼164のP1）
      新台の突き合わせと同じ勘定にすると枠を食い合い、
      どちらかが必ず欠ける晩ができる。勘定を分けて両立させる。
    """
    key = "codex_rounds" if lane == "main" else f"codex_rounds_{lane}"
    limit = CODEX_ROUND_LIMIT if lane == "main" else CODEX_ASK_ROUND_LIMIT
    with _Exclusive(path):
        data = _load(path)
        e = _entry(data, task)
        used = int(e.get(key) or 0)
        if used >= limit:
            raise GuardError(
                f"Codexとの相談が上限（{limit}往復・{lane}）に達しました。"
                f"結論づかず扱いで台帳に登録して終わってください")
        e[key] = used + 1
        _save(path, data)
        return e[key]

# ★同じ機種で空振りが続いたら、しばらく選ばない★（2026-08-21・依頼246の防御4）
#   手順書にだけ書いてあって実装が無かったので、無人実行が守る保証が無かった。
REPAIR_FAIL_LIMIT = 2          # 続けてこの「日数」だけ直せなかったら休ませる
REPAIR_COOLDOWN_DAYS = 7       # 休ませる日数
# ★数え方をはっきりさせる★（2026-08-21・依頼247の防御3）
#   `cooldown_until` = 休みに入った日 + 7日。`until > today` の間だけ断る。
#   ＝**休みに入った日を1日目として7日間**休み、8日目から選べる。
#   （「翌日から7日」ではない。曖昧なままにしない）
#
# ★1日の機種数と、書き換えの予算は別物★（2026-08-21・依頼247の防御4）
#   MACHINES_PER_DAY … 1日に「担当してよい機種の数」
#   assets/data/task-budget.json … 1日に「書き換えてよい数」を種類別に決める
#     writes_total=3 / writes_fix=2 / writes_grow=1
#   ★3機種＝修正2＋育成1★なので、両者は食い違っていない。
#   ★予算を上げるのは段階のルールに従う★（task-budget.json の _next_stage：
#     7日連続で完走し既存記事の修正が10件以上できたら writes_fix=3 へ）。
#   ここを勝手に上げない。


def _repair_book(data: dict) -> dict:
    return data.setdefault("repair", {})


def repair_cooldown(slug: str, path: str = STATE_PATH, issues=None) -> tuple[bool, str]:
    """その機種はいま休み中か。戻り値 (休み中か, 理由)。

    ★数えるのは「材料が揃っても直せなかった」回数だけ★（依頼246の防御4）
      通信の失敗・ロック待ち・Codexの利用制限は数えない（呼び出し側が渡さない）。

    ★新しい案件が出たら休みは解ける★（2026-08-21・依頼247の指摘4）
      休みは slug 単位なので、#100 の空振りで休んでいる間に
      新しい誤情報の案件 #200 が同じ機種へ来ても、最大7日直せなかった。
      **公開済みの記事を直すための経路でそれは実害**なので、
      休みに入った時点の案件より新しいものが来たら解く。
    """
    data = _load(path)
    rec = _repair_book(data).get(slug) or {}
    until = rec.get("cooldown_until")
    if not until:
        return False, ""
    today = _today()
    if str(until) <= today:
        return False, ""
    known = set(rec.get("issues") or [])
    now_ids = set(issues or [])
    # ★何を見て休みに入ったか分からないときは解かない★
    #   （控えが空だと「いまある案件は全部新しい」に見えて、休みが無意味になる）
    fresh = (now_ids - known) if known else set()
    if fresh:
        return False, (f"新しい案件（{' / '.join('#%d' % n for n in sorted(fresh))}）"
                       "が来たので休みを解きます")
    return True, (f"{slug} は {until} まで休みです"
                  f"（続けて {rec.get('fails', 0)} 回直せませんでした）")


def record_repair(slug: str, fixed: bool, path: str = STATE_PATH,
                  why: str = "", issues=None) -> dict:
    """直せたか直せなかったかを記録する。★直せたら回数は0に戻す★

    fixed=True  … 何かしら前へ進んだ（回数を0に戻し、休みも解除）
    fixed=False … 材料が揃っても直せなかった（回数+1。上限に達したら休みへ）
    """
    with _Exclusive(path):
        data = _load(path)
        # ★「直せた」は自己申告では通さない★（2026-08-21・依頼247の防御2）
        #   直す前は `--fixed yes` と言うだけで回数が0に戻り、休みも解けた。
        #   ★機械が確かめられる根拠＝コミットの関所を通ったこと★
        #   （before_commit が通ると final_stage が入る）。
        #   ★通っていなければ「直せた」とは認めない★
        if fixed:
            passed = any(
                isinstance(v, dict) and v.get("guard_slug") == slug
                and v.get("final_stage") and v.get("run_date") == _today()
                for v in (data.get("tasks") or {}).values())
            if not passed:
                raise GuardError(
                    f"{slug} は今日コミットの関所を通っていません"
                    "（before-commit を通ってから --fixed yes を記録してください）")
        book = _repair_book(data)
        rec = book.setdefault(slug, {"fails": 0, "cooldown_until": None, "why": ""})
        if fixed:
            rec.update({"fails": 0, "cooldown_until": None, "why": "", "issues": []})
        elif rec.get("last_fail_date") == _today():
            # ★同じ日に何度呼んでも1回★（2026-08-21・依頼247の防御1）
            #   仕様は「2**日**続けて直せなかったら」。呼んだ回数ではない。
            #   直す前は、同じ晩に2回記録するだけで休みに入れた。
            rec["why"] = why[:200]
        else:
            # ★★「続けて」＝日が飛んだら数え直す★★（2026-08-21・依頼248の指摘4）
            #   直す前は「今日と同じか」しか見ていなかったので、
            #   8月1日と8月20日の失敗でも2回と数えて休みに入れた。
            prev = str(rec.get("last_fail_date") or "")
            if prev:
                try:
                    gap = (datetime.strptime(_today(), "%Y-%m-%d")
                           - datetime.strptime(prev, "%Y-%m-%d")).days
                except ValueError:
                    gap = 99
                if gap > 1:
                    rec["fails"] = 0        # 間が空いた＝続いていない
            rec["fails"] = int(rec.get("fails") or 0) + 1
            rec["why"] = why[:200]
            rec["last_fail_date"] = _today()
            if rec["fails"] >= REPAIR_FAIL_LIMIT:
                until = datetime.now() + timedelta(days=REPAIR_COOLDOWN_DAYS)
                rec["cooldown_until"] = until.strftime("%Y-%m-%d")
                # ★休みに入った時点で見ていた案件を控える★
                #   これより新しい案件が来たら休みを解く（依頼247の指摘4）
                rec["issues"] = sorted({int(x) for x in (issues or [])
                                        if str(x).strip().isdigit()})
        rec["last_seen"] = _today()
        _save(path, data)
        return dict(rec)


def _unrelated_changes(slug: str) -> list:
    """★その機種と関係のない変更が混ざっていないか★（2026-08-21・依頼246の指摘3）

    直す経路で触ってよいのは、その機種の記事データと、その機種のページだけ。
    ★コマンドは固定の引数配列で呼ぶ★（シェルを通さない）。
    見られなければ「関係ないものがある」と答える（fail-closed）。
    """
    allow = (f"assets/data/machine-details/{slug}.json",
             f"machines/{slug}/")
    try:
        p = subprocess.run(["git", "-C", str(BASE), "status", "--porcelain"],
                           capture_output=True, text=True, timeout=30)
    except Exception as e:                                   # noqa: BLE001
        return [f"変更を確認できません（{type(e).__name__}）"]
    if p.returncode != 0:
        return ["変更を確認できません（git status が失敗）"]
    out = []
    for line in p.stdout.splitlines():
        name = line[3:].strip().strip('"')
        if not name:
            continue
        # ★名前の変更は「移動元」も見る★（2026-08-21・依頼247の指摘3）
        #   移動先だけ見ていたので、`R 別機種/index.html -> 対象機種/index.html`
        #   のように**別機種のファイルを消して流用する**変更が同じコミットに入れた。
        names = [x.strip().strip('"') for x in name.split(" -> ")] if " -> " in name \
            else [name]
        for nm in names:
            if nm and not any(nm.startswith(a) for a in allow):
                out.append(nm)
    return out


def _git(*args) -> tuple[int, str]:
    """★固定の引数配列でgitを呼ぶ★（シェルを通さない）。戻り値 (終了コード, 標準出力)"""
    try:
        p = subprocess.run(["git", "-C", str(BASE), *args],
                           capture_output=True, text=True, timeout=60)
    except Exception as e:                                   # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"
    return p.returncode, p.stdout


def _changed_files() -> tuple[list, str]:
    """いま変わっているファイルの一覧。読めなければ理由を返す。"""
    rc, out = _git("status", "--porcelain")
    if rc != 0:
        return [], "git status が失敗しました"
    names = []
    for line in out.splitlines():
        nm = line[3:].strip().strip('"')
        if not nm:
            continue
        if " -> " in nm:                    # 名前の変更は「変更後」を見る
            nm = nm.split(" -> ", 1)[1].strip().strip('"')
        names.append(nm)
    return sorted(set(names)), ""


def _file_digest(rel: str) -> str:
    """手元のファイルの中身の指紋。消えている場合は DELETED。"""
    p = os.path.join(BASE, rel.replace("/", os.sep))
    if not os.path.isfile(p):
        return "DELETED"
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _commit_digest(commit: str, rel: str) -> str:
    """そのコミットの中でのファイルの中身の指紋。無ければ DELETED。"""
    rc, out = _git("cat-file", "-e", f"{commit}:{rel}")
    if rc != 0:
        return "DELETED"
    try:
        p = subprocess.run(["git", "-C", str(BASE), "show", f"{commit}:{rel}"],
                           capture_output=True, timeout=60)
    except Exception:                                        # noqa: BLE001
        return "ERROR"
    if p.returncode != 0:
        return "ERROR"
    return hashlib.sha256(p.stdout).hexdigest()


def verify_commit(task: str, slug: str, commit: str,
                  path: str = STATE_PATH) -> dict:
    """★関所が見た内容と、実際のコミットが同じか確かめる★
    （2026-08-21・Codex依頼248の指摘3）

    ★なぜ要るのか★
      before_commit が「OK」と言ったあとに、別のファイルを足してから
      コミットできた。関所が見た内容と、公開される内容が結び付いていなかった。

    確かめること:
      ①コミットが触ったファイルが、関所のときと**過不足なく同じ**
      ②その中身の指紋が、関所のときと同じ
      ③そのコミットが、いまの HEAD である

    ★これが通らないうちは push しない★（手順書で必須にする）
    """
    if not re.fullmatch(r"[0-9a-f]{7,40}", str(commit or "")):
        raise GuardError(f"コミットの指定が不正です: {commit!r}")
    with _Exclusive(path):
        data = _load(path)
        e = _entry(data, task)
        if e.get("guard_slug") != slug:
            raise GuardError(
                f"{slug} の関所の記録がありません（記録は {e.get('guard_slug')!r}）")
        approved = e.get("approved_files")
        if not isinstance(approved, dict) or not approved:
            raise GuardError(
                f"{slug} は関所の記録に「見た内容」がありません"
                "（before-commit を通っていない＝pushさせません）")

        rc, head = _git("rev-parse", "HEAD")
        if rc != 0:
            raise GuardError("いまのコミットを読めません")
        head = head.strip()
        if not head.startswith(commit) and not commit.startswith(head[:len(commit)]):
            raise GuardError(f"いまのコミットが違います（HEAD={head[:12]}）")

        rc, out = _git("diff-tree", "--no-commit-id", "--name-only", "-r", commit)
        if rc != 0:
            raise GuardError("コミットの中身を読めません")
        in_commit = sorted({x.strip() for x in out.splitlines() if x.strip()})

        extra = [x for x in in_commit if x not in approved]
        if extra:
            raise GuardError(
                "関所が見ていないファイルがコミットに入っています: "
                + " / ".join(extra[:3])
                + (f" ほか{len(extra) - 3}件" if len(extra) > 3 else ""))
        missing = [x for x in approved if x not in in_commit]
        if missing:
            raise GuardError(
                "関所が見たファイルがコミットに入っていません: "
                + " / ".join(missing[:3])
                + (f" ほか{len(missing) - 3}件" if len(missing) > 3 else ""))
        changed = [x for x in in_commit if _commit_digest(commit, x) != approved[x]]
        if changed:
            raise GuardError(
                "関所が見たあとで中身が変わったファイルがあります: "
                + " / ".join(changed[:3])
                + (f" ほか{len(changed) - 3}件" if len(changed) > 3 else ""))

        e["verified_commit"] = head
        _save(path, data)
        return {"slug": slug, "commit": head, "files": len(in_commit),
                "ok": True}


def before_write(task: str, slug: str, path: str = STATE_PATH,
                 repairing: bool = False) -> dict:
    """記事を書き換える前の確認。★触ってよい段階か毎回聞き直す★

    repairing=True ＝「台帳の案件を直すために触る」（2026-08-21・台帳#211）。
    ★台帳による停止だけを飛ばす★（公開済みの記事に限る＝claim_pipeline.repairable）。
    ★飛ばしても、そのとき台帳に何件あったかを記録する★＝
      あとで「直した結果、案件が増えていないか」を比べるため。
    """
    with _Exclusive(path):
        data = _load(path)
        e = _entry(data, task)
        if e["target_slug"] != slug:
            raise GuardError(f"今日の担当は {e['target_slug']} です（{slug} ではありません）")
        # ★関所の記録がこの機種のものか確かめる★（依頼247の指摘1）
        if e.get("guard_slug") != slug:
            raise GuardError(
                f"{slug} の担当を確保していません（記録は {e.get('guard_slug')!r} のものです）")
        a = cp.assess(slug, repairing=repairing)
        # ★★修理モードは担当を取った時に決まる。あとから変えられない★★
        #   （2026-08-21・Codex依頼248の指摘1）
        #   直す前は呼ぶたびに上書きしていたので、
        #     ①--repairing で claim ②--repairing で before-write
        #     ③台帳から案件が消える ④--repairing **なし**で before-write を呼び直す
        #   とすると repairing=False になり、
        #   「案件が消えたら止める」も「その機種以外のファイルを載せない」も
        #   **まるごと飛んだ**。
        claimed = bool(e.get("repairing"))
        if bool(repairing) != claimed:
            raise GuardError(
                f"{slug} は "
                + ("直す経路で担当しています" if claimed else "ふつうに担当しています")
                + "。担当を取ったときと違う呼び方はできません"
                + ("（--repairing を付けてください）" if claimed
                   else "（--repairing は付けられません）"))
        # ★基準は最初の1回だけ★（2026-08-21・Codex依頼246の指摘2）
        #   呼ぶたびに上書きしていたので、
        #     ①書き始める ②新しい重大案件を見つけて台帳へ登録する
        #     ③もう一度 before-write を呼ぶ → その案件が「元からあった」ことになる
        #   という順で、増えた案件が比較の基準に取り込まれ、素通りできた。
        if "ledger_before" not in e or not e.get("mutation_started"):
            e["ledger_before"] = list(a.get("ledger_blocking") or [])
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
    with _Exclusive(path):
        data = _load(path)
        e = _entry(data, task)
        if e["target_slug"] != slug:
            raise GuardError(f"今日の担当は {e['target_slug']} です（{slug} ではありません）")
        # ★書き換えを始めた記録が無ければコミットさせない★
        #   （2026-08-05・Codex113回目の指摘5。before-write を通らずに
        #     コミットへ来た場合、悪化していないかを比べる基準が無い）
        # ★その記録がこの機種のものであること★（依頼247の指摘1）
        #   これが無いと、1機種目の記録で2機種目のコミットが通った。
        if e.get("guard_slug") != slug:
            raise GuardError(
                f"{slug} の関所の記録がありません（記録は {e.get('guard_slug')!r} のものです）"
                "＝コミットさせません")
        if not e.get("mutation_started") or not e.get("stage_before"):
            raise GuardError(
                f"{slug} は書き換えを始めた記録がありません"
                "（before-write を通っていない＝コミットさせません）")
        repairing = bool(e.get("repairing"))
        a = cp.assess(slug, repairing=repairing)
        if repairing:
            # ★直す経路では「案件が増えていないこと」を見る★（2026-08-21・台帳#211）
            #   段階だけ見ると、元から BLOCKED_BY_LEDGER なので何も比べられない。
            #   ★比べるのは番号★（題名で比べると、題を書き換えただけで誤判定する）
            before_ids = _issue_ids(e.get("ledger_before"))
            after_ids = _issue_ids(a.get("ledger_blocking"))
            grew = after_ids - before_ids
            if grew:
                raise GuardError(
                    f"直した結果、台帳の止める案件が増えました（{len(grew)}件）: "
                    + " / ".join(f"#{n}" for n in sorted(grew)[:3])
                    + " → コミットせず、変更を戻すか台帳で扱ってください")
            # ★直すと言った案件が、いまも「直す対象」であること★（依頼247の指摘2）
            #   番号を控えるだけで一度も見ていなかったので、
            #   指定した案件と無関係な変更でも通り得た。
            #   ★機械が言えるのはここまで★＝
            #     「その案件がまだ生きている（＝直したと称して消えていない）」
            #     「案件が増えていない」「その機種のファイルしか触っていない」。
            #   ★中身がその案件の修理かどうかは2AIの領分★（機械では決めない）。
            said = set(e.get("repair_issues") or [])
            if not said:
                raise GuardError(
                    f"{slug} は直す案件が控えられていません"
                    "（--repairing で claim し直してください）")
            gone = said - after_ids
            if gone:
                # ★無人タスクは台帳を閉じない★＝消えていたら、想定外のことが起きている
                raise GuardError(
                    "直すと言った案件が台帳から消えています: "
                    + " / ".join(f"#{n}" for n in sorted(gone))
                    + "（無人タスクは台帳を閉じません。コミットせずに終わってください）")
            # ★直すと言った案件と関係ないファイルを載せない★（依頼246の指摘3）
            #   これが無いと「CRITICALが1件でもある機種なら何を書き換えてもよい」
            #   という許可証になる。触ってよいのはその機種のものだけ。
            bad = _unrelated_changes(slug)
            if bad:
                raise GuardError(
                    f"直す経路では {slug} 以外のファイルを一緒にコミットできません: "
                    + " / ".join(bad[:3])
                    + (f" ほか{len(bad) - 3}件" if len(bad) > 3 else ""))
        else:
            # ★ふつうの更新でも、別の機種のデータは混ぜない★
            #   （2026-08-21・Codex依頼248の指摘2。直す経路にだけ付けていたので、
            #     機種Aの関所を通しながら機種Bのデータを同じコミットへ入れられた。
            #     Bの中身は誰も検査していない）
            #   ★ふつうの更新は、その機種の外にも正当に触るものがある★
            #     （machines.json・sitemap・service-worker・ハブページ）ので、
            #     ★別の機種のデータだけを見る★（機種をまたぐ混入だけを止める）。
            other = [x for x in _unrelated_changes(slug)
                     if x.startswith("assets/data/machine-details/")
                     or x.startswith("machines/")]
            if other:
                raise GuardError(
                    f"{slug} の担当なのに、別の機種のファイルが混ざっています: "
                    + " / ".join(other[:3])
                    + (f" ほか{len(other) - 3}件" if len(other) > 3 else "")
                    + " → 分けてコミットしてください")
        # ★知らない段階なら止める★（fail-closed）
        if a["stage"] not in set(WRITABLE_STAGES) | set(FROZEN_STAGES) | {"READY"}:
            raise GuardError(f"直したあとの段階が想定外です: {a['stage']}")
        before = e.get("stage_before")
        # ★止めるべき段階になっていたら、理由を問わずコミットさせない★
        if a["stage"] in FROZEN_STAGES and before not in FROZEN_STAGES:
            raise GuardError(
                f"直した結果、触ってはいけない段階になりました（{before} → {a['stage']}）")
        if a["stage"] in ("HOLD", "NO_MACHINE"):
            raise GuardError(
                f"直したあとの判定ができません: {a['stage']} → コミットしないでください")
        # ★悪化していないこと★（更新タスクは「今より悪くしない」が最優先）
        if before and before != "BLOCKED_BY_LEDGER" and a["stage"] == "BLOCKED_BY_LEDGER":
            raise GuardError(
                f"直した結果、公開を止めるべき状態になりました（{before} → {a['stage']}）。"
                f"コミットせず、変更を戻すか台帳で扱ってください")
        # ★★見た内容の指紋を残す★★（2026-08-21・Codex依頼248の指摘3）
        #   ここが無いと、「OK」と言ったあとに別のファイルを足してコミットできた。
        #   push の前に `verify-commit` でこの指紋と突き合わせる。
        files, why_f = _changed_files()
        if why_f:
            raise GuardError(f"変更を確認できないのでコミットさせません: {why_f}")
        if not files:
            raise GuardError(
                f"{slug} は変更がありません（コミットするものがありません）")
        e["approved_files"] = {f: _file_digest(f) for f in files}
        e["verified_commit"] = None
        e["final_stage"] = a["stage"]
        _save(path, data)
        return a

def done(task: str, slug: str, stage: str, path: str = STATE_PATH) -> dict:
    """その機種の作業を終える。

    ★直す経路で担当した機種は、結果を記録しないと終われない★
      （2026-08-21・依頼247の防御2。`repaired` を呼ばずに終われたので、
        空振りが数えられず、いつまでも同じ機種を選び続けられた）
    """
    with _Exclusive(path):
        data = _load(path)
        e = _entry(data, task)
        if e.get("repairing") and e.get("guard_slug") == slug:
            rec = _repair_book(data).get(slug) or {}
            marked = (rec.get("last_seen") == _today())
            if not marked:
                raise GuardError(
                    f"{slug} は直す経路で担当したので、終える前に結果の記録が要ります: "
                    f"python scripts/task_guard.py repaired --slug {slug} "
                    "--fixed yes|no --why …")
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
                 "writes_fix": 2, "writes_grow": 1, "inspections": 2,
                 "deadline_hhmm": "23:59"}, f)

    def res(kind, slug, close=True):
        # ★1日1機種の縛りとは別に、予算の増減だけを見たい★
        #   （担当は日ごとに1つなので、担当を書き換えてから取る）
        _d0 = _load(sp)
        _day(_d0)["target_slug"] = None
        _save(sp, _d0)
        r = reserve("t", slug, kind, path=sp, budget_path=bp,
                    contract_sha256="sha256:" + "a" * 64)
        if close and r.get("token"):
            # ★1件ずつ片付けてから次へ★（やりかけを2つ持たない）
            begin_apply(r["token"], slug, kind, "sha256:" + "a" * 64, "t-" + slug, path=sp)
            advance(r["token"], "ROLLED_BACK_VERIFIED", path=sp)
        return r

    t("　1件目の書き換えは取れる", res("fix", "a")["token"])
    t("　2件目も取れる", res("fix", "b")["token"])
    t("★★既存記事の修正は内訳の上限で止まる★★",
      _raises(lambda: res("fix", "c"), "上限"))
    t("　育てる枠はまだ残っている", res("grow", "d")["token"])
    t("★★総枠を超えたら種類を問わず止まる★★",
      _raises(lambda: res("grow", "e"), "上限"))
    # ★失敗しても枠は戻らない★
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
      reserve("t", "zzz_test", "fix", path=sp, budget_path=bp,
              contract_sha256="sha256:" + "a" * 64)["test"])
    # 止めたら、タスク名を変えても通らない
    halt("監査に引っかかったため", path=sp)
    sp6 = os.path.join(tmpdir, "state_claim.json")
    reserve("t", "きめた機種", "fix", path=sp6, budget_path=bp,
            contract_sha256="sha256:" + "a" * 64)
    t("★★claim を呼ばなくても、1日1機種は守られる★★（Codex115回目のP1-6）",
      _raises(lambda: reserve("t", "ちがう機種", "fix", path=sp6, budget_path=bp,
                              contract_sha256="sha256:" + "a" * 64),
              "担当しています"))
    t("★★止めた日は、別のタスク名でも書けない★★",
      _raises(lambda: reserve("別のタスク", "g", "fix", path=sp,
                              budget_path=bp, contract_sha256="sha256:" + "a" * 64),
              "止めています"))
    # ★予約の確認と消費が1つのまとまりになっているか★（Codex111回目のP0-4）
    sp4 = os.path.join(tmpdir, "state_begin.json")
    tk = reserve("t", "m", "fix", path=sp4, budget_path=bp,
                 contract_sha256="sha256:" + "a" * 64)["token"]
    begin_apply(tk, "m", "fix", "sha256:" + "a" * 64, "attempt-1", path=sp4)
    t("★★同じ予約を別の処理が同時に使えない★★",
      _raises(lambda: begin_apply(tk, "m", "fix", "sha256:" + "a" * 64, "attempt-2",
                                  path=sp4), "別の処理"))
    t("　同じ回のやり直しは通る",
      begin_apply(tk, "m", "fix", "sha256:" + "a" * 64, "attempt-1",
                  path=sp4)["state"] == "APPLYING")
    t("★★契約が違えば同じ予約を使えない★★",
      _raises(lambda: begin_apply(tk, "m", "fix", "sha256:" + "b" * 64,
                                  "attempt-3", path=sp4), "契約と違います"))
    t("★★機種が違えば同じ予約を使えない★★",
      _raises(lambda: begin_apply(tk, "ちがう機種", "fix", "sha256:" + "a" * 64, "attempt-4",
                                  path=sp4), "食い違います"))
    # ★締切を過ぎたら新しい書き換えに着手しない★
    bp2 = os.path.join(tmpdir, "budget_late.json")
    sp2 = os.path.join(tmpdir, "state_late.json")
    with open(bp2, "w", encoding="utf-8") as f:
        _j.dump({"schema_version": "task-budget/v1", "writes_total": 3,
                 "writes_fix": 2, "writes_grow": 1, "inspections": 2,
                 "deadline_hhmm": "00:00"}, f)
    t("★★締切を過ぎたら新しい書き換えに着手しない★★",
      _raises(lambda: reserve("t", "z", "fix", path=sp2, budget_path=bp2,
                              contract_sha256="sha256:" + "a" * 64), "締切"))
    t("★★契約の指紋なしでは修正の枠を取れない★★（別の契約に流用させない）",
      _raises(lambda: reserve("t", "z", "fix", path=sp2, budget_path=bp2),
              "指紋"))
    # ★やりかけがあるうちは次を始めない★
    sp3 = os.path.join(tmpdir, "state_open.json")
    tok3 = reserve("t", "p", "fix", path=sp3, budget_path=bp,
                   contract_sha256="sha256:" + "a" * 64)["token"]
    begin_apply(tok3, "p", "fix", "sha256:" + "a" * 64, "t-p", path=sp3)
    t("★★やりかけの書き換えがあるうちは次を始めない★★",
      _raises(lambda: reserve("t", "p", "fix", path=sp3, budget_path=bp,
                              contract_sha256="sha256:" + "a" * 64),
              "やりかけ"))
    t("★★決められた順にしか進めない★★（予約直後にpush済みとは書けない）",
      _raises(lambda: advance(tok3, "PUSH_CONFIRMED", path=sp3), "進めません"))
    t("★★普通の前進では予約を消費できない★★（照合を飛ばす経路を塞いだ）",
      _raises(lambda: advance(
          reserve("t", "r", "fix", path=os.path.join(tmpdir, "s5.json"),
                  budget_path=bp, contract_sha256="sha256:" + "a" * 64)["token"],
          "APPLYING", path=os.path.join(tmpdir, "s5.json")), "進めません"))
    t("　同じ段階への再実行は何も起きない（冪等）",
      begin_apply(tok3, "p", "fix", "sha256:" + "a" * 64, "t-p", path=sp3)["state"] == "APPLYING")
    t("★★日をまたいでも予約の履歴は消えない★★（再開できる）",
      len(_load(sp3).get("reservations") or []) == 1)
    # 予算そのものが壊れていたら止まる
    with open(bp, "w", encoding="utf-8") as f:
        f.write("{壊れた")
    t("★★予算が読めない日は動かさない★★",
      _raises(lambda: budget(bp), ""))

    # ★★無制限の枠は「まだ一覧に無い機種」だけ★★（2026-08-11・台帳#294）
    #   以前はタスク名の自己申告だけで無制限になったので、
    #   別のタスクがこの名前を名乗れば1日1機種の枠を迂回できた。
    _keep_assess = cp.assess
    try:
        cp.assess = lambda slug: {"stage": "NO_MACHINE"}
        _p = os.path.join(tempfile.mkdtemp(), "guard.json")
        got = claim("add-machine", "aarakuma1", path=_p)
        t("★★新台（まだ一覧に無い機種）は無制限で通る★★",
          isinstance(got, dict))
        cp.assess = lambda slug: {"stage": "READY"}
        try:
            claim("add-machine", "aarakuma2", path=_p)
            _ng = False
        except GuardError as e:
            _ng = "すでに一覧にあります" in str(e)
        t("★★すでに一覧にある機種では無制限を使えない★★"
          "（名乗りだけで1日1機種の枠を迂回できた）", _ng)
        cp.assess = lambda slug: (_ for _ in ()).throw(RuntimeError("読めません"))
        try:
            claim("add-machine", "aarakuma3", path=_p)
            _ng2 = False
        except GuardError:
            _ng2 = True
        t("　新台かどうか判定できないときも通さない（枠は使わない）", _ng2)
    finally:
        cp.assess = _keep_assess


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
        # ★触れない機種を選んでも枠を減らさない★（2026-08-08・台帳#272）
        #   以前は claim が段階を見ておらず、blocking の機種を担当に確保した
        #   時点でその日の枠が消え、before_write に拒否されても戻らなかった。
        fp0 = os.path.join(tmpdir, "guard0.json")
        _keep_assess = cp.assess
        cp.assess = lambda s, *a, **k: {"stage": "BLOCKED_BY_LEDGER"}
        try:
            blocked = raises(lambda: claim("t", "galfy", fp0), "触れません")
        finally:
            cp.assess = _keep_assess
        t("★★台帳で止まっている機種は担当にできない★★（台帳#272）", blocked)
        # ★ここから先は本番の台帳を見ない★（2026-08-12）
        #   以前は素の claim を呼んでいたので、本番の台帳で hokuto が
        #   止まっている日は**自己テストがそこで落ちた**（実際に発生）。
        #   道具の振る舞いを見る試験が、その日のデータで変わってはいけない。
        #   既に一覧にある機種は READY、まだ無い機種は NO_MACHINE（実際と同じ形）
        # ★1日の上限を数える試験ぶんも、ここに入れておく★（2026-08-21）
        #   上限を増やしたら「担当できる機種」も増やす必要がある。
        #   本番データを見に行かせない（その日の台帳で試験の結果が変わらないように）。
        _spares = ["sp_a", "sp_b", "sp_c", "sp_d", "sp_e", "sp_f"]
        _known = {"hokuto", "enen", "galfy"} | set(_spares)
        cp.assess = lambda sl, *a, **k: {
            "stage": "READY" if sl in _known else "NO_MACHINE"}
        t("　断られた日でも枠は残る（次の候補を選べる）",
          claim("t", "hokuto", fp0)["target_slug"] == "hokuto")

        t("★1機種目は担当できる★", claim("t", "hokuto", fp)["target_slug"] == "hokuto")
        t("　同じ機種なら何度呼んでもよい（再開できる）",
          claim("t", "hokuto", fp)["target_slug"] == "hokuto")
        # ★上限は「数」で効く★（2026-08-21・MACHINES_PER_DAY を 1 → 3 にした）
        #   設定値を変えたら実装が本当に追随するか、ここで確かめる。
        for i in range(1, MACHINES_PER_DAY):
            _s = _spares[i - 1]
            t(f"　{i + 1}機種目までは担当できる",
              claim("t", _s, fp)["target_slug"] == _s)
        t("★★上限を超えた機種は拒否する★★（1日の上限が実際に効く）",
          raises(lambda: claim("t", "enen", fp), "1日"))
        t("★★タスク名を変えても1日の上限は迂回できない★★（Codex114回目の指摘5）",
          raises(lambda: claim("t2", "enen", fp), "1日"))
        t("　上限に入っている機種なら、別タスクからでも続けられる",
          claim("t2", "hokuto", fp)["target_slug"] == "hokuto")

        # ★新台の追加だけは機種数を数えない★（2026-08-07・運営者決定）
        #   新台は導入日が決まっていて待てないため。
        fp2 = os.path.join(tmpdir, "guard2.json")
        many = [claim("add-machine", "n%d" % i, fp2)["target_slug"]
                for i in range(5)]
        t("★★新台の追加は同じ晩に何機種でも担当できる★★",
          many == ["n%d" % i for i in range(5)])
        t("　新台を何件やっても他のタスクの1日の上限は残る",
          claim("t3", "hokuto", fp2)["target_slug"] == "hokuto"
          and all(claim("t3", _spares[i - 1], fp2)["target_slug"] == _spares[i - 1]
                  for i in range(1, MACHINES_PER_DAY))
          and raises(lambda: claim("t3", "enen", fp2), "1日"))
        for i in range(5, 40):
            claim("add-machine", "n%d" % i, fp2)
        t("★★新台には件数の上限を置かない★★（2026-08-07・運営者決定）",
          claim("add-machine", "zzz", fp2)["target_slug"] == "zzz"
          and len(_load(fp2)["day"]["unlimited_slugs"]) == 41)

        for i in (1, 2, 3):
            codex_round("t", fp)
        t("★★Codexの相談は上限で止まる（4往復目を拒否）★★",
          raises(lambda: codex_round("t", fp), "上限"))
        t("　上限はファイルに残る（落ちて再起動しても数え直しにならない）",
          _load(fp)["tasks"]["t"]["codex_rounds"] == 3)

        t("★担当していない機種は書き換えられない★",
          raises(lambda: before_write("t", "enen", fp), "今日の担当"))
        claim("t2", "hokuto", fp)      # 別タスクでも担当は同じ機種だけ
        # ★段階を作って確かめる★（2026-08-05・Codex110回目の指摘11。
        #   以前は `or True` が付いていて、何が起きても合格していた）
        _real_assess = cp.assess
        try:
            # ★偽物は本番と同じ形で受け取る★（2026-08-21）
            #   `repairing=` を足したとき、`lambda slug:` のままだと
            #   TypeError で落ち、関所の判定を試験できていなかった。
            cp.assess = lambda slug, **k: {"stage": FROZEN_STAGES[0],
                                           "reasons": ["試験"]}
            t("★★止めるべき機種は触らせない★★",
              raises(lambda: before_write("t2", "hokuto", fp), "触ってはいけない"))
            cp.assess = lambda slug, **k: {"stage": "READY", "reasons": []}
            t("★すでに公開してよい機種は書き換えない★",
              raises(lambda: before_write("t2", "hokuto", fp), "理由がありません"))
            cp.assess = lambda slug, **k: {"stage": "でたらめ", "reasons": []}
            t("★知らない段階なら書かない★",
              raises(lambda: before_write("t2", "hokuto", fp), "想定外"))

            # --- ★直す経路（台帳#211・2026-08-21）★
            #   台帳で止まっている機種でも、公開済みの記事なら直せる。
            #   ただし「案件が増えたらコミットさせない」は必ず効くこと。
            calls = {}

            def _fake_assess(slug, repairing=False):
                calls["repairing"] = repairing
                if repairing:
                    return {"stage": "IDENTITY_PENDING", "reasons": [],
                            "ledger_blocking": ["#1 もとからある案件"]}
                return {"stage": "BLOCKED_BY_LEDGER", "reasons": ["#1 もとからある案件"],
                        "ledger_blocking": ["#1 もとからある案件"]}

            cp.assess = _fake_assess
            # ★ファイルの範囲の見張りは、この試験では差し替える★
            #   本物は「いま手元に未コミットの変更があるか」を見るので、
            #   このファイル自身を編集している最中は必ず作動する（＝正しい動作）。
            #   範囲の見張りそのものは、下の専用の試験で確かめる。
            _keep_unrel0 = globals()["_unrelated_changes"]
            globals()["_unrelated_changes"] = lambda s: []
            t("★ふつうに入ると、いままでどおり止まる★",
              raises(lambda: before_write("t2", "hokuto", fp), "触ってはいけない"))
            # ★直す経路は「どの案件を直すか」を控えてから★（依頼247の指摘2）
            claim("t2", "hokuto", fp, repairing=True, issues=["1"])
            got = before_write("t2", "hokuto", fp, repairing=True)
            t("★直す経路なら書き込みへ進める★", got["stage"] == "IDENTITY_PENDING")
            t("★飛ばしたことが判定側にも伝わっている★", calls.get("repairing") is True)

            # 案件が増えていなければコミットできる
            ok = before_commit("t2", "hokuto", fp)
            t("★案件が増えていなければコミットできる★",
              ok["stage"] == "IDENTITY_PENDING")

            # ★案件が増えたらコミットさせない★（対照実験）
            def _grew(slug, repairing=False):
                return {"stage": "IDENTITY_PENDING", "reasons": [],
                        "ledger_blocking": ["#1 もとからある案件", "#2 直した拍子に増えた案件"]}

            cp.assess = _grew
            t("★★直した結果、案件が増えたらコミットさせない★★",
              raises(lambda: before_commit("t2", "hokuto", fp), "増えました"))

            # --- ★担当の確保も直す経路を通す★（実装直後に見つけた穴）
            #   ここが古い判定のままだと、before_write まで到達できず
            #   直す経路が丸ごと動かない。
            cp.assess = _fake_assess
            fp3 = os.path.join(tmpdir, "guard_repair.json")
            t("★ふつうに担当しようとすると弾かれる（枠は減らない）★",
              raises(lambda: claim("t3", "kabaneri", fp3), "いま触れません"))
            t("★★どの案件を直すか言わないと担当できない★★（依頼246の指摘3）",
              raises(lambda: claim("t3", "kabaneri", fp3, repairing=True), "案件の番号"))
            t("★止めていない案件の番号は受け付けない★",
              raises(lambda: claim("t3", "kabaneri", fp3, repairing=True,
                                   issues=["999"]), "含まれない番号"))
            got2 = claim("t3", "kabaneri", fp3, repairing=True, issues=["1"])
            t("★直す経路なら担当できる★", got2["target_slug"] == "kabaneri")
            t("　直す案件が控えに残る",
              _load(fp3)["tasks"]["t3"].get("repair_issues") == [1])

            # --- ★比較の基準は最初の1回だけ★（依頼246の指摘2の対照実験）
            fp4 = os.path.join(tmpdir, "guard_base.json")
            claim("t4", "kabaneri", fp4, repairing=True, issues=["1"])
            before_write("t4", "kabaneri", fp4, repairing=True)
            base1 = list(_load(fp4)["tasks"]["t4"]["ledger_before"])

            def _more(slug, repairing=False):
                return {"stage": "IDENTITY_PENDING", "reasons": [],
                        "ledger_blocking": ["#1 もとからある案件", "#2 途中で増えた案件"]}

            cp.assess = _more
            before_write("t4", "kabaneri", fp4, repairing=True)   # ★2回目★
            t("★★2回目の before-write で基準が上書きされない★★",
              _load(fp4)["tasks"]["t4"]["ledger_before"] == base1)
            t("★増えた案件はコミット前に見つかる★",
              raises(lambda: before_commit("t4", "kabaneri", fp4), "増えました"))
            cp.assess = _fake_assess

            # --- ★番号で比べる（題を書き換えただけでは増えたことにしない）★
            t("番号だけを取り出せる",
              _issue_ids(["#12 あ", " #7 い", "番号なし"]) == {12, 7})

            def _renamed(slug, repairing=False):
                return {"stage": "IDENTITY_PENDING", "reasons": [],
                        "ledger_blocking": ["#1 題名を書き換えただけ"]}

            fp5 = os.path.join(tmpdir, "guard_rename.json")
            claim("t5", "kabaneri", fp5, repairing=True, issues=["1"])
            before_write("t5", "kabaneri", fp5, repairing=True)
            cp.assess = _renamed
            t("★題名が変わっただけなら増えた扱いにしない★",
              before_commit("t5", "kabaneri", fp5)["stage"] == "IDENTITY_PENDING")
            cp.assess = _fake_assess

            # --- ★関係ないファイルを一緒にコミットさせない★（依頼246の指摘3）
            fp6 = os.path.join(tmpdir, "guard_scope.json")
            claim("t6", "kabaneri", fp6, repairing=True, issues=["1"])
            before_write("t6", "kabaneri", fp6, repairing=True)
            globals()["_unrelated_changes"] = lambda s: ["scripts/nazono.py"]
            t("★★直す経路で関係ないファイルがあれば止める★★",
              raises(lambda: before_commit("t6", "kabaneri", fp6), "以外のファイル"))
            globals()["_unrelated_changes"] = lambda s: []
            t("　その機種のものだけなら通る",
              before_commit("t6", "kabaneri", fp6)["stage"] == "IDENTITY_PENDING")

            # --- ★空振りが続いたら休ませる（依頼246の防御4）★
            fp7 = os.path.join(tmpdir, "guard_cool.json")
            t("　はじめは休みではない", repair_cooldown("kabaneri", fp7)[0] is False)
            # ★数えるのは「日」なので、日をまたがせて試す★（依頼247の防御1）
            #   ★理由の文も仕様どおりに★＝記録するのは
            #   「材料が揃っても直せなかった」ときだけ（材料不足は記録しない）
            for i in range(REPAIR_FAIL_LIMIT):
                record_repair("kabaneri", False, fp7,
                              why="材料は揃ったが、どちらが正しいか決められなかった")
                if i == 0:
                    t(f"　{REPAIR_FAIL_LIMIT - 1}日目ではまだ休まない",
                      repair_cooldown("kabaneri", fp7)[0] is False)
                # ★「昨日」にする★（同じ日に何度呼んでも1回、が効いているため）
                #   ★日が飛ぶと数え直される★ので、必ず前日にする（依頼248の指摘4）
                _d = _load(fp7)
                _yesterday = (datetime.strptime(_today(), "%Y-%m-%d")
                              - timedelta(days=1)).strftime("%Y-%m-%d")
                _repair_book(_d)["kabaneri"]["last_fail_date"] = _yesterday
                _save(fp7, _d)
            t("★★続けて直せなければ休みに入る★★", repair_cooldown("kabaneri", fp7)[0])
            t("★★休み中は担当できない（枠は使わない）★★",
              raises(lambda: claim("t7", "kabaneri", fp7, repairing=True,
                                   issues=["1"]), "休みです"))
            # ★「直せた」は自己申告では通らない★（依頼247の防御2・対照実験）
            t("★★コミットの関所を通っていなければ直せた扱いにできない★★",
              raises(lambda: record_repair("kabaneri", True, fp7), "通っていません"))
            # 関所を通った記録を作ってから、もう一度
            _d7 = _load(fp7)
            _d7.setdefault("tasks", {})["tX"] = {
                "run_date": _today(), "guard_slug": "kabaneri",
                "final_stage": "IDENTITY_PENDING"}
            _save(fp7, _d7)
            record_repair("kabaneri", True, fp7)
            t("★直せたら休みは解ける★", repair_cooldown("kabaneri", fp7)[0] is False)
            t("　直せたら回数も0に戻る",
              (_load(fp7)["repair"]["kabaneri"]["fails"]) == 0)

            # --- ★名前の変更は移動元も見る★（依頼247の指摘3）
            #   ★`or True` を付けた書き方をやめた★＝何が起きても合格していた。
            #   実際のパーサーに、gitの出力の形をそのまま食わせる。
            globals()["_unrelated_changes"] = _keep_unrel0
            _keep_run = subprocess.run

            class _R:
                returncode = 0
                stdout = ("R  machines/other/index.html -> machines/target/index.html\n"
                          " M assets/data/machine-details/target.json\n"
                          " M scripts/nazono.py\n")

            try:
                subprocess.run = lambda *a, **k: _R()
                bad2 = _unrelated_changes("target")
                t("★★名前の変更は移動元も見つける★★",
                  "machines/other/index.html" in bad2)
                t("　移動先が対象内なら、そこは挙げない",
                  "machines/target/index.html" not in bad2)
                t("　その機種の記事データは挙げない",
                  "assets/data/machine-details/target.json" not in bad2)
                t("　関係ないスクリプトは挙げる", "scripts/nazono.py" in bad2)
            finally:
                subprocess.run = _keep_run
            globals()["_unrelated_changes"] = lambda s: []

            # --- ★同じ日に何度呼んでも1回★（依頼247の防御1）
            fp8 = os.path.join(tmpdir, "guard_day.json")
            for _ in range(5):
                record_repair("zzz_same_day", False, fp8, why="材料が揃っても決められない")
            t("★★同じ日に何回記録しても休みには入らない★★",
              repair_cooldown("zzz_same_day", fp8)[0] is False)
            t("　数えているのは日数（呼んだ回数ではない）",
              _load(fp8)["repair"]["zzz_same_day"]["fails"] == 1)

            # --- ★新しい案件が来たら休みは解ける★（依頼247の指摘4）
            fp9 = os.path.join(tmpdir, "guard_fresh.json")
            data9 = _load(fp9)
            _repair_book(data9)["zzz_rest"] = {
                "fails": 2, "cooldown_until": "2099-12-31", "issues": [100]}
            _save(fp9, data9)
            t("　同じ案件のままなら休み",
              repair_cooldown("zzz_rest", fp9, issues={100})[0])
            t("★★新しい案件が来たら休みが解ける★★",
              repair_cooldown("zzz_rest", fp9, issues={100, 200})[0] is False)

            # --- ★別の機種に移ったら前の記録を捨てる★（依頼247の指摘1・対照実験）
            fpA = os.path.join(tmpdir, "guard_two.json")
            claim("t8", "aaa", fpA, repairing=True, issues=["1"])
            before_write("t8", "aaa", fpA, repairing=True)
            # ★1日の上限に関係なく、機種を切り替えたときの守りを見る試験★
            #   （上限が1でも3でも、この守りは同じように効かなければならない）
            _dA = _load(fpA)
            _day(_dA)["slugs_today"] = []
            _save(fpA, _dA)
            claim("t8", "bbb", fpA, repairing=True, issues=["1"])
            t("★★2機種目は before-write を呼ばないとコミットできない★★",
              raises(lambda: before_commit("t8", "bbb", fpA), "記録がありません"))
            before_write("t8", "bbb", fpA, repairing=True)
            t("　2機種目も before-write を通せばコミットできる",
              before_commit("t8", "bbb", fpA)["stage"] == "IDENTITY_PENDING")

            # --- ★関所が見た内容とコミットを結び付ける★（依頼248の指摘3）
            #   本物のgitを呼ぶので、gitの出力だけ差し替えて筋を確かめる。
            fpG = os.path.join(tmpdir, "guard_bind.json")
            claim("tE", "kabaneri", fpG, repairing=True, issues=["1"])
            before_write("tE", "kabaneri", fpG, repairing=True)

            _keep_changed = globals()["_changed_files"]
            _keep_fd = globals()["_file_digest"]
            _keep_cd = globals()["_commit_digest"]
            _keep_git = globals()["_git"]
            try:
                globals()["_changed_files"] = lambda: (
                    ["assets/data/machine-details/kabaneri.json"], "")
                globals()["_file_digest"] = lambda rel: "AAA"
                before_commit("tE", "kabaneri", fpG)
                t("　関所が見た内容の指紋が残る",
                  _load(fpG)["tasks"]["tE"]["approved_files"]
                  == {"assets/data/machine-details/kabaneri.json": "AAA"})

                globals()["_git"] = lambda *a: (
                    (0, "abc1234\n") if a[0] == "rev-parse"
                    else (0, "assets/data/machine-details/kabaneri.json\n"))
                globals()["_commit_digest"] = lambda c, rel: "AAA"
                t("★関所と同じ内容ならpushしてよい★",
                  verify_commit("tE", "kabaneri", "abc1234", fpG)["ok"])

                globals()["_commit_digest"] = lambda c, rel: "BBB"
                t("★★関所のあとで中身が変わっていたら止める★★",
                  raises(lambda: verify_commit("tE", "kabaneri", "abc1234", fpG),
                         "中身が変わった"))

                globals()["_commit_digest"] = lambda c, rel: "AAA"
                globals()["_git"] = lambda *a: (
                    (0, "abc1234\n") if a[0] == "rev-parse"
                    else (0, "assets/data/machine-details/kabaneri.json\n"
                             "scripts/nazono.py\n"))
                t("★★関所が見ていないファイルが入っていたら止める★★",
                  raises(lambda: verify_commit("tE", "kabaneri", "abc1234", fpG),
                         "見ていないファイル"))

                globals()["_git"] = lambda *a: (
                    (0, "abc1234\n") if a[0] == "rev-parse" else (0, "\n"))
                t("★関所が見たファイルが入っていなければ止める★",
                  raises(lambda: verify_commit("tE", "kabaneri", "abc1234", fpG),
                         "入っていません"))

                globals()["_git"] = lambda *a: (
                    (0, "999999\n") if a[0] == "rev-parse"
                    else (0, "assets/data/machine-details/kabaneri.json\n"))
                t("★別のコミットなら止める★",
                  raises(lambda: verify_commit("tE", "kabaneri", "abc1234", fpG),
                         "いまのコミットが違います"))
            finally:
                globals()["_changed_files"] = _keep_changed
                globals()["_file_digest"] = _keep_fd
                globals()["_commit_digest"] = _keep_cd
                globals()["_git"] = _keep_git

            # ★関所を通っていなければ verify も通らない★
            fpH = os.path.join(tmpdir, "guard_nobind.json")
            claim("tF", "kabaneri", fpH, repairing=True, issues=["1"])
            t("★★before-commit を通っていなければ push させない★★",
              raises(lambda: verify_commit("tF", "kabaneri", "abc1234", fpH),
                     "見た内容"))

            # --- ★修理モードは担当のあと変えられない★（依頼248の指摘1・対照実験）
            fpD = os.path.join(tmpdir, "guard_mode.json")

            def _two(slug, repairing=False):
                return {"stage": "IDENTITY_PENDING", "reasons": [],
                        "ledger_blocking": ["#1 ひとつめ", "#2 ふたつめ"]}

            cp.assess = _two
            claim("tB", "kabaneri", fpD, repairing=True, issues=["1"])
            before_write("tB", "kabaneri", fpD, repairing=True)
            t("★★--repairing なしで呼び直して通常モードへ落とせない★★",
              raises(lambda: before_write("tB", "kabaneri", fpD), "違う呼び方"))
            t("★担当中に案件を差し替えられない★",
              raises(lambda: claim("tB", "kabaneri", fpD, repairing=True,
                                   issues=["2"]), "案件を変えられません"))
            # ふつうに担当した機種へ、あとから --repairing は付けられない
            _dD = _load(fpD)
            _day(_dD)["slugs_today"] = []
            _save(fpD, _dD)
            claim("tC", "hokuto", fpD)
            t("　ふつうに担当した機種に --repairing は付けられない",
              raises(lambda: before_write("tC", "hokuto", fpD, repairing=True),
                     "違う呼び方"))
            cp.assess = _fake_assess

            # --- ★日が飛んだら「続けて」ではない★（依頼248の指摘4）
            fpE = os.path.join(tmpdir, "guard_gap.json")
            record_repair("zzz_gap", False, fpE, why="決められなかった")
            _dE = _load(fpE)
            _repair_book(_dE)["zzz_gap"]["last_fail_date"] = "2026-08-01"
            _save(fpE, _dE)
            record_repair("zzz_gap", False, fpE, why="ずっとあとの日にまた失敗")
            t("★★日が飛んでいたら数え直す（休みに入らない）★★",
              repair_cooldown("zzz_gap", fpE)[0] is False)
            t("　数え直されている", _load(fpE)["repair"]["zzz_gap"]["fails"] == 1)

            # --- ★ふつうの経路でも別機種のデータは混ぜられない★（依頼248の指摘2）
            fpF = os.path.join(tmpdir, "guard_mix.json")
            cp.assess = lambda s, **k: {"stage": "IDENTITY_PENDING", "reasons": [],
                                        "ledger_blocking": []}
            claim("tD", "kabaneri", fpF)
            before_write("tD", "kabaneri", fpF)
            globals()["_unrelated_changes"] = lambda s: [
                "assets/data/machine-details/hokuto.json", "sitemap.xml"]
            t("★★ふつうの更新でも、別機種のデータが混ざっていたら止める★★",
              raises(lambda: before_commit("tD", "kabaneri", fpF), "別の機種のファイル"))
            globals()["_unrelated_changes"] = lambda s: ["sitemap.xml",
                                                        "service-worker.js"]
            t("　その機種の外でも、機種データでなければ通る（sitemap等）",
              before_commit("tD", "kabaneri", fpF)["stage"] == "IDENTITY_PENDING")
            globals()["_unrelated_changes"] = lambda s: []
            cp.assess = _fake_assess

            # --- ★直す経路は、結果を記録しないと終われない★（依頼247の防御2）
            fpC = os.path.join(tmpdir, "guard_done.json")
            claim("tA", "kabaneri", fpC, repairing=True, issues=["1"])
            before_write("tA", "kabaneri", fpC, repairing=True)
            t("★★結果を記録せずに終えようとすると断られる★★",
              raises(lambda: done("tA", "kabaneri", "IDENTITY_PENDING", fpC),
                     "結果の記録が要ります"))
            record_repair("kabaneri", False, fpC, why="材料は揃ったが決められなかった")
            t("　記録してあれば終えられる",
              done("tA", "kabaneri", "IDENTITY_PENDING", fpC)["final_stage"]
              == "IDENTITY_PENDING")

            # --- ★直すと言った案件が消えていたら止める★（依頼247の指摘2）
            fpB = os.path.join(tmpdir, "guard_said.json")
            claim("t9", "kabaneri", fpB, repairing=True, issues=["1"])
            before_write("t9", "kabaneri", fpB, repairing=True)

            def _closed(slug, repairing=False):
                return {"stage": "IDENTITY_PENDING", "reasons": [],
                        "ledger_blocking": []}       # ★#1 が消えた★

            cp.assess = _closed
            t("★★直すと言った案件が台帳から消えていたら止める★★",
              raises(lambda: before_commit("t9", "kabaneri", fpB), "消えています"))
            cp.assess = _fake_assess
        finally:
            cp.assess = _real_assess
            # ★差し替えた見張りを必ず戻す★（試験のあとに本番の関所が緩まないように）
            globals()["_unrelated_changes"] = _keep_unrel0

        # 記録が読めないときは動かさない（fail-closed）
        with open(fp, "w", encoding="utf-8") as f:
            f.write("{壊れたJSON")
        t("★★記録が読めないときは今日は動かさない（fail-closed）★★",
          raises(lambda: claim("t", "hokuto", fp), "読めません"))
    finally:
        cp.assess = _keep_assess          # ★本物へ戻す★
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
        if name in ("claim", "before-write"):
            # ★台帳の案件を直すために触る★（2026-08-21・台帳#211／Codex依頼246の指摘1）
            #   ここが無いと、関数には経路があるのに**コマンドから使えず**、
            #   無人実行では修理対象を確保できなかった。
            p.add_argument("--repairing", action="store_true",
                           help="台帳で止まっている公開済み機種を、直すために担当する")
            p.add_argument("--issue", action="append", default=[],
                           help="直す対象の案件番号（例 --issue 318）。"
                                "--repairing のときは1つ以上必須")
        if name == "done":
            p.add_argument("--stage", required=True)
    p = sub.add_parser("reserve")          # ★書き換えの枠を取る★
    p.add_argument("--task", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--kind", required=True, choices=["fix", "grow"])
    p.add_argument("--contract-sha", default="",
                   help="修正の枠には必須（契約JSONの sha256:…）")
    p = sub.add_parser("advance")          # 予約を次の段階へ（★枠は戻らない★）
    p.add_argument("--token", required=True)
    p.add_argument("--state", required=True, choices=list(STATES))
    p = sub.add_parser("inspected")        # 点検を1件数える
    p.add_argument("--task", required=True)
    p.add_argument("--slug", required=True)
    p = sub.add_parser("halt")             # その日をまるごと止める
    p.add_argument("--reason", required=True)
    sub.add_parser("day")                  # 今日の使用状況
    p = sub.add_parser("codex")
    p.add_argument("--task", required=True)
    # ★質問のやり直しは別勘定★（2026-08-12・依頼164）新台の枠を食わない
    p.add_argument("--lane", default="main", choices=["main", "ask"])
    p = sub.add_parser("status")
    p.add_argument("--task", required=True)
    # ★直せたか直せなかったかを記録する★（2026-08-21・依頼246の防御4）
    p = sub.add_parser("repaired")
    p.add_argument("--slug", required=True)
    p.add_argument("--fixed", choices=["yes", "no"], required=True,
                   help="yes=前へ進んだ / no=材料が揃っても直せなかった"
                        "（通信の失敗やロック待ちは記録しない）")
    p.add_argument("--why", default="", help="no のときの理由（短く）")
    p = sub.add_parser("cooldown")
    p.add_argument("--slug", required=True)
    # ★関所が見た内容と、実際のコミットが同じか確かめる★（依頼248の指摘3）
    p = sub.add_parser("verify-commit")
    p.add_argument("--task", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--commit", required=True,
                   help="いま作ったコミット（git rev-parse HEAD の値）")

    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if args.cmd == "claim":
        print(json.dumps(claim(args.task, args.slug,
                               repairing=bool(getattr(args, "repairing", False)),
                               issues=getattr(args, "issue", []) or []),
                         ensure_ascii=False, indent=1))
    elif args.cmd == "reserve":
        print(json.dumps(
            reserve(args.task, args.slug, args.kind,
                    contract_sha256=args.contract_sha), ensure_ascii=False))
    elif args.cmd == "advance":
        print(json.dumps(advance(args.token, args.state), ensure_ascii=False))
    elif args.cmd == "inspected":
        print(json.dumps(inspected(args.task, args.slug), ensure_ascii=False))
    elif args.cmd == "halt":
        print(json.dumps(halt(args.reason), ensure_ascii=False))
    elif args.cmd == "day":
        print(json.dumps(day_status(), ensure_ascii=False, indent=1))
    elif args.cmd == "codex":
        _lane = getattr(args, "lane", "main") or "main"
        _lim = CODEX_ROUND_LIMIT if _lane == "main" else CODEX_ASK_ROUND_LIMIT
        print(f"Codex相談 {codex_round(args.task, lane=_lane)}/{_lim} 回目"
              + ("" if _lane == "main" else f"（{_lane}の枠）"))
    elif args.cmd == "before-write":
        print(json.dumps(before_write(args.task, args.slug,
                                      repairing=bool(getattr(args, "repairing", False))),
                         ensure_ascii=False, indent=1))
    elif args.cmd == "before-commit":
        print(json.dumps(before_commit(args.task, args.slug), ensure_ascii=False, indent=1))
    elif args.cmd == "done":
        print(json.dumps(done(args.task, args.slug, args.stage), ensure_ascii=False, indent=1))
    elif args.cmd == "verify-commit":
        print(json.dumps(verify_commit(args.task, args.slug, args.commit),
                         ensure_ascii=False, indent=1))
    elif args.cmd == "repaired":
        # ★休みに入るときは「何を見ていたか」を控える★（依頼247の指摘4）
        #   手で渡させると忘れるので、そのとき止めている案件をここで引く。
        try:
            _ids = _issue_ids(cp.assess(args.slug, repairing=True).get("ledger_blocking"))
        except Exception as _e:                              # noqa: BLE001
            # ★控えが取れないまま休みに入れない★（2026-08-21・依頼248の指摘4）
            #   空集合で控えると、あとで新しい重大案件が来ても休みが解けない
            #   （＝公開済みの誤情報の修正が最大7日遅れる）。
            print(f"★案件の控えを取れませんでした（{type(_e).__name__}）★")
            print("  この状態では記録しません。原因を直してから、もう一度実行してください")
            return 3
        print(json.dumps(record_repair(args.slug, args.fixed == "yes",
                                       why=args.why, issues=_ids),
                         ensure_ascii=False, indent=1))
    elif args.cmd == "cooldown":
        resting, why = repair_cooldown(args.slug)
        print(json.dumps({"slug": args.slug, "resting": resting, "why": why},
                         ensure_ascii=False))
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

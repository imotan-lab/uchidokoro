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
import json
import os
import re
import sys
import tempfile
from datetime import datetime

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


def claim(task: str, slug: str, path: str = STATE_PATH) -> dict:
    """今日この機種を担当してよいか。★同じ日の2機種目は拒否★

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
            stage = cp.assess(slug).get("stage")
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
        cur = d.get("target_slug")
        if cur and cur != slug:
            raise GuardError(
                f"今日はすでに {cur} を担当しています（1日{MACHINES_PER_DAY}機種）。"
                f"{slug} は明日以降に回してください")
        d["target_slug"] = slug
        if e["target_slug"] and e["target_slug"] != slug:
            raise GuardError(
                f"今日はすでに {e['target_slug']} を担当しています（1日{MACHINES_PER_DAY}機種）。"
                f"{slug} は明日以降に回してください")
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

def before_write(task: str, slug: str, path: str = STATE_PATH) -> dict:
    """記事を書き換える前の確認。★触ってよい段階か毎回聞き直す★"""
    with _Exclusive(path):
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
    with _Exclusive(path):
        data = _load(path)
        e = _entry(data, task)
        if e["target_slug"] != slug:
            raise GuardError(f"今日の担当は {e['target_slug']} です（{slug} ではありません）")
        # ★書き換えを始めた記録が無ければコミットさせない★
        #   （2026-08-05・Codex113回目の指摘5。before-write を通らずに
        #     コミットへ来た場合、悪化していないかを比べる基準が無い）
        if not e.get("mutation_started") or not e.get("stage_before"):
            raise GuardError(
                f"{slug} は書き換えを始めた記録がありません"
                "（before-write を通っていない＝コミットさせません）")
        a = cp.assess(slug)
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
        e["final_stage"] = a["stage"]
        _save(path, data)
        return a

def done(task: str, slug: str, stage: str, path: str = STATE_PATH) -> dict:

    with _Exclusive(path):
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
        _known = {"hokuto", "enen", "galfy"}
        cp.assess = lambda sl, *a, **k: {
            "stage": "READY" if sl in _known else "NO_MACHINE"}
        t("　断られた日でも枠は残る（次の候補を選べる）",
          claim("t", "hokuto", fp0)["target_slug"] == "hokuto")

        t("★1機種目は担当できる★", claim("t", "hokuto", fp)["target_slug"] == "hokuto")
        t("　同じ機種なら何度呼んでもよい（再開できる）",
          claim("t", "hokuto", fp)["target_slug"] == "hokuto")
        t("★★同じ日の2機種目は拒否する★★（1日1機種が実際に効く）",
          raises(lambda: claim("t", "enen", fp), "1日"))
        t("★★タスク名を変えても1日1機種は迂回できない★★（Codex114回目の指摘5）",
          raises(lambda: claim("t2", "enen", fp), "1日"))

        # ★新台の追加だけは機種数を数えない★（2026-08-07・運営者決定）
        #   新台は導入日が決まっていて待てないため。
        fp2 = os.path.join(tmpdir, "guard2.json")
        many = [claim("add-machine", "n%d" % i, fp2)["target_slug"]
                for i in range(5)]
        t("★★新台の追加は同じ晩に何機種でも担当できる★★",
          many == ["n%d" % i for i in range(5)])
        t("　新台を何件やっても他のタスクの1日1機種は残る",
          claim("t3", "hokuto", fp2)["target_slug"] == "hokuto"
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
            cp.assess = lambda slug: {"stage": FROZEN_STAGES[0],
                                      "reasons": ["試験"]}
            t("★★止めるべき機種は触らせない★★",
              raises(lambda: before_write("t2", "hokuto", fp), "触ってはいけない"))
            cp.assess = lambda slug: {"stage": "READY", "reasons": []}
            t("★すでに公開してよい機種は書き換えない★",
              raises(lambda: before_write("t2", "hokuto", fp), "理由がありません"))
            cp.assess = lambda slug: {"stage": "でたらめ", "reasons": []}
            t("★知らない段階なら書かない★",
              raises(lambda: before_write("t2", "hokuto", fp), "想定外"))
        finally:
            cp.assess = _real_assess

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

    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if args.cmd == "claim":
        print(json.dumps(claim(args.task, args.slug), ensure_ascii=False, indent=1))
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

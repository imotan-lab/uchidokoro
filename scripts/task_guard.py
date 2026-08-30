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
# ★★いったん上限なし★★（2026-08-27・運営者の指示）
#   ★理由＝「ちゃんと通ったことがないので、まず通してから回数を決める」★
#   0 = 上限なし。★走り続ける心配は無い★＝タスクには締切があり、
#   そちらで必ず終わる。ここは「何回相談したか」を数えるだけになる。
#   ★通し確認ができたら、実測をもとに回数を決め直す★
CODEX_ROUND_LIMIT = 0
# ★2AIへの質問（やり直し）は別勘定★（2026-08-12・依頼164のP1）
#   同じ勘定にすると「質問に3回使うと新台の突き合わせが0回」になり、
#   **新台の公開か、質問の解決か、どちらかが必ず欠ける晩**ができる。
#   新台の枠を先に守り、質問には質問の枠を渡す。
CODEX_ASK_ROUND_LIMIT = 0      # ★同上・いったん上限なし★
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
#
# ★★同日中に 1 → 3 へ戻した（条件3つとも満たしたため）★★（2026-08-21）
#   ①claim() が repairing を確定し、before_write が食い違いを断る
#     （_e1["repairing"] を claim で書き、before_write が照合する）
#   ②verify_commit() が approved_files の指紋と HEAD を突き合わせる
#     ＝関所が見た差分と、実際のコミットが同じだと確かめられる
#   ③_machines_per_day_tests を新設した。
#     ★この試験を書いたら、その場で本物の穴が出た★＝
#       reserve() は target_slug（1つだけ）で数えていたので、
#       ★MACHINES_PER_DAY を3にしても2機種目は予約の段階で必ず断られた★。
#       ＝設定値だけ変えても動かない形だった（文言だけ「1日3機種」になる）。
#       これが依頼248の言う「writes_fix と3機種運用の不一致」の正体。
#       reserve() を claim() と同じ slugs_today で数えるように直した。
#
#   ★数の根拠★ 3機種 = 修正2 + 育成1（assets/data/task-budget.json）。
#     この一致を試験が毎回確かめる（片方だけ動かしたら落ちる）。
#   ★戻すならここだけ★（手順書の文言も一緒に直すこと）
# ★★0 は「上限なし」★★（2026-08-25・運営者の指示で育成の上限を撤廃）
#   ★件数で止めるのをやめ、締切（deadline_hhmm）で止める★。
#   頻度の表は「導入日〜30日後は毎日」と言っているのに、
#   1日3機種（修正2＋育成1）だったため、育成対象10機種では
#   **1機種あたり10日に1回**しか見られていなかった（表と実装の食い違い）。
#   ★修正（writes_fix）の枠は据え置き★＝記事の中身を書き換える側なので、
#   段階ルールを続ける（予算の側で見る）。
MACHINES_PER_DAY = 0
# ★新台の暴走止め★（2026-08-26・台帳#479）
#   件数の制限ではなく「明らかにおかしい数」で止める安全弁。
#   実績＝1晩に処理する新台候補は多くても数件。
UNLIMITED_RUNAWAY_CAP = 20
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


# ★★「一晩」の区切り★★（2026-08-26・Codex29回目の指摘3）
#   ★暦日で数えると、日付をまたぐ夜は上限が2倍になる★
#   新台タスクは 23:30 に始まり、明け方まで走る。
#   暦日で数えていたので 23:30〜23:59 に20件、00:00〜 にもう20件が通った。
#   ★昼を境にする★＝「一晩」は 12:00 から翌 11:59 まで。
#   （明け方の 04:30 を境にすると、締切を過ぎて走った分が
#     翌晩に繰り上がって、また枠が空いてしまう）
NIGHT_ROLLOVER_HOUR = 12


def _night_id(now=None) -> str:
    """その時刻が属する「晩」の名前（＝晩が始まった日の日付）。"""
    now = now or datetime.now()
    if now.hour < NIGHT_ROLLOVER_HOUR:
        now = now - timedelta(days=1)
    return now.strftime("%Y-%m-%d")


def past_deadline(deadline_hhmm: str, now_hhmm: str = "") -> bool:
    """★締切を「一晩」の中で見る★（2026-08-28・本番で実害）

    ★時刻の文字だけで比べてはいけない★＝
    夜のタスクは23時30分に始まるので、朝07:20の締切と文字で比べると
    「もう過ぎている」になり、★毎晩30分なにもできなくなる★。

    一晩は 12:00〜翌11:59（`_night_id` と同じ考え方）。
      ・いまが夜で、締切が朝 → まだ来ていない
      ・いまが朝で、締切が夜 → 過ぎている
      ・同じ側なら、そのまま比べる
    """
    dl = str(deadline_hhmm or "")
    if not dl:
        return False
    now = now_hhmm or datetime.now().strftime("%H:%M")
    now_evening = now >= "12:00"
    dl_evening = dl >= "12:00"
    if now_evening and not dl_evening:
        # ★締切は翌朝＝まだ来ていない★
        return False
    if (not now_evening) and dl_evening:
        # ★締切は昨夜＝過ぎている★
        return True
    return now >= dl


def _night(data: dict, now=None) -> dict:
    """★一晩ぶんの記録★（`_day` とは別の入れ物。暦日で消えない）"""
    nid = _night_id(now)
    n = data.setdefault("night", {})
    if n.get("id") != nid:
        n.clear()
        n.update({"id": nid, "slugs": []})
    return n


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


def _alive_posix(pid: int, killer=None) -> bool:
    """★POSIX（Linux・CI）での生き死に★

    ★関数に切り出した理由★（2026-08-21）
      ★Windowsからは、この道を一度も動かせない★＝
      `os.name` で分けただけでは、CI（Linux）で通る道が手元で未実行のまま
      push されることになる。**それがまさに今回CIを赤くした形**。
      問い合わせる手だてを差し替えられるようにして、
      手元でも3つの答え（居る／居ない／権限が無い）を実際に動かして確かめる。

    `os.kill(pid, 0)` は POSIX では**問い合わせ**（何も送らない）。
    ★Windows では問い合わせではなく終了させるので、ここへは来ない★
    """
    k = killer or os.kill
    try:
        k(pid, 0)
        return True
    except ProcessLookupError:
        return False                      # ★居ないと確かめられた★
    except PermissionError:
        return True                       # 居るが自分のものではない＝奪わない
    except Exception:                     # noqa: BLE001
        return True                       # 分からないときは奪わない（安全側）


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
        """そのプロセスがまだ動いているか（居なければ鍵を奪ってよい）。

        ★★OSで見方が違う★★（2026-08-21・CIが赤くなって分かった）
          ★直す前は `tasklist` だけを見ていた★＝Windows専用のコマンド。
          Linux（CI）には無いので例外になり、「分からない＝生きている」に
          倒れていた。＝**Linuxでは、どんなPIDを渡しても常に「生きている」**。
          手元（Windows）では正しく動くので、★ci_repro では再現できない★。
          ★これはCLAUDE.mdに書いてあるOSの罠を2度踏んだ形★。

        ・Windows … `tasklist` で問い合わせる
          （`os.kill(pid, 0)` は Windows では問い合わせではなく**終了させる**）
        ・それ以外 … `os.kill(pid, 0)`（POSIXでは問い合わせ。送信しない）
        ★分からないときは「生きている」と答える★＝奪わない（安全側）。
        """
        if pid <= 0:
            return False
        if os.name == "nt":
            try:
                import subprocess
                r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                   capture_output=True, text=True, timeout=10,
                                   encoding="utf-8", errors="replace")
                return str(pid) in (r.stdout or "")
            except Exception:             # noqa: BLE001
                return True               # 分からないときは奪わない（安全側）
        return _alive_posix(pid)

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
    # ★★0 は「上限なし」★★（2026-08-25・運営者の指示で育成の上限を撤廃）
    #   ★件数で止めるのをやめ、締切（deadline_hhmm）だけで止める★。
    #   頻度の表は「導入日〜30日後は毎日」と言っているのに、
    #   育成が1日1機種だったため、対象10機種では**1機種あたり10日に1回**しか
    #   見られていなかった（表と実装の食い違い）。
    #   ★修正（writes_fix）は据え置き★＝あちらは記事の中身を書き換えるので
    #   段階ルールを続ける。
    #   ★内訳の検算は、どちらも上限ありのときだけ★
    #   （0 が混ざると「合計が総枠に届かない」が常に成り立ってしまう）。
    if d["writes_total"] and d["writes_fix"] and d["writes_grow"]:
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
            contract_sha256: str = "", now_hhmm: str = "") -> dict:
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
        #
        # ★★数える場所を1つにする★★（2026-08-21・MACHINES_PER_DAY の条件③）
        #   直す前は、claim() が slugs_today の**件数**で数えるのに対し、
        #   ここは target_slug（1つだけ）で見ていた。
        #   ＝★MACHINES_PER_DAY を3にしても、2機種目は予約の段階で必ず断られる★。
        #   設定値だけ変えても動かない形だった（文言には「1日3機種」と出るのに）。
        #   これが「writes_fix と3機種運用が一致していない」の正体。
        #   ★同じ規則を2か所に書かない★＝claim() と同じ slugs_today で数える。
        _seen_today = d.setdefault("slugs_today", [])
        if slug not in _seen_today:
            if MACHINES_PER_DAY and len(_seen_today) >= MACHINES_PER_DAY:
                raise GuardError(
                    f"今日はすでに {len(_seen_today)} 機種を担当しています"
                    f"（1日{MACHINES_PER_DAY}機種・{' / '.join(_seen_today)}）。"
                    f"{slug} は明日以降に回してください")
            _seen_today.append(slug)
        d["target_slug"] = slug
        # ★締切を過ぎたら新しい書き換えに着手しない★（途中で朝を迎えないため）
        if past_deadline(b["deadline_hhmm"], now_hhmm):
            raise GuardError(
                f"新しい書き換えの締切（{b['deadline_hhmm']}）を過ぎています")
        # ★やりかけがあるなら、先にそれを片付ける★
        left = [r for r in open_reservations(data) if r["token"] != ""]
        if left:
            raise GuardError(
                f"やりかけの書き換えが残っています（{left[0]['token']} / "
                f"{left[0]['state']}）。先に片付けてください")
        # ★0 は「上限なし」＝件数では止めない（締切だけで止める）★
        if b["writes_total"] and d["writes"]["total"] >= b["writes_total"]:
            raise GuardError(f"今日の書き換えは上限です（{b['writes_total']}件）")
        if b[f"writes_{kind}"] and d["writes"][kind] >= b[f"writes_{kind}"]:
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
          repairing: bool = False, issues=None, finding=None,
          scheduled=None) -> dict:
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
        # ★★未コミットのコードでは、どのタスクも担当を取らない★★
        #   （2026-08-26・台帳#478。★新台タスクだけ効いていなかった★）
        #   ★歯止めは下（718行あたり）にあったが、新台は上の分岐で
        #     先に return していたので、一度も通っていなかった★。
        #   ＝鉄則4「レビューされていないコードで公開処理を走らせない」が
        #     いちばん危ない経路（公開してpushする側）で破れていた。
        #   実際、2026-08-25の夜は別の理由（契約のズレ）で偶然止まっただけ。
        _dirty0 = unattended_dirty_code(task)
        if _dirty0:
            raise GuardError(
                "コミットされていないスクリプトがあります: "
                + " / ".join(_dirty0[:3])
                + "（レビューされていないコードで公開処理は走らせません）")
        # ★★その日に一度でも無人で担当したら、もう戻さない★★
        #   （2026-08-30・Codexの指摘1。★自分で再現した★）
        #   直す前はタスクごとに1つだけ持って上書きしていたので、
        #   ★無人 → 手動の順に担当すると、無人だった記録が消えた★
        #   ＝無人が作った未照合のコミットまで push できた。
        # ★★分岐より手前で記録する★★（2026-08-30・Codexの指摘1の後半）
        #   ★新台の担当は下の分岐で先に return する★ので、
        #   後ろに置いていた間は**新台では一度も保存されていなかった**
        #   （実測で確認）。＝--scheduled を渡しても効いていなかった。
        # ★★三値★★（2026-08-30・Codexの指摘1）
        #   True … 無人だと申告された
        #   False … ★手動だと明示された★
        #   None … 申告が無い（古い呼び出し）→ ロックで推測する
        #   ★ロックでは区別できない★＝手動の add_machine_run も
        #   ロックの取得が必須なので、ロックを見るだけでは無人に見える。
        _un = lock_is_live() if scheduled is None else bool(scheduled)
        _entry(data, task)["unattended"] = _un
        _d0 = _day(data)
        _d0["had_unattended"] = bool(_d0.get("had_unattended")) or _un
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
            # ★★数えるのは「一晩」ぶん★★（2026-08-26・Codex29回目の指摘3）
            #   ★`_day` は暦日で消えるので、日付をまたぐ夜は上限が2倍になった★
            done = _night(data).setdefault("slugs", [])
            # ★暦日の記録も残す★（その日に何件作ったかを見るため。上限には使わない）
            d.setdefault("unlimited_slugs", [])
            # ★★暴走止め★★（2026-08-26・台帳#479）
            #   ★説明文には「上限に当たったら止めて知らせる」と書いてあるのに、
            #     実装は記録するだけで拒否していなかった★。
            #   同じ晩に何十件も作り続けるのは、うまくいっている状態ではなく
            #   不具合の形（DMMのカレンダーの読み違い等）。
            #   ★新台の件数そのものは制限しない★＝導入日が決まっていて待てない。
            #   ここは「明らかにおかしい数」で止めるだけの安全弁。
            if slug not in done and len(done) >= UNLIMITED_RUNAWAY_CAP:
                raise GuardError(
                    f"同じ晩に新台を {len(done)} 件も作っています"
                    f"（上限 {UNLIMITED_RUNAWAY_CAP} 件）。"
                    "うまくいっている状態ではないので止めます")
            if slug not in done:
                done.append(slug)
            if slug not in d["unlimited_slugs"]:
                d["unlimited_slugs"].append(slug)
            e = _entry(data, task)
            e["target_slug"] = slug
            _save(path, data)
            return e
        # ★★修理モードは担当を取った時に固定する★★（依頼248の指摘1）
        #
        # ★2026-08-21に本当に固定した★（Codexの再指摘）
        #   ★直す前は、あとから無条件に上書きしていた★＝
        #   同じ機種をもう一度 claim すれば、モードを好きに変えられた。
        #   実際に両方向とも通ることを確かめた（対照実験）：
        #     直す経路（#318）で担当 → repairing=False で取り直す
        #       → repairing が False になり、**案件番号だけが残った**
        #     ふつうに担当 → repairing=True で取り直す
        #       → ★ふつうの担当が、あとから直す担当に化けた★
        #   ＝台帳の関門を後から外せる（＝「直す」の名目で何でも書ける）。
        #
        # ★★どの検査よりも先に置く★★＝
        #   後ろに置くと「触ってはいけない段階です」など**別の理由**で
        #   断られてしまい、この関門が効いているのか分からない
        #   （実際、段階の検査の後ろに置いたら試験が別の文言で落ちた）。
        #
        # ★★記録は「日ごと・機種ごと」に持つ★★（2026-08-21・Codexの再指摘）
        #   ★直す前の穴＝A→B→A★
        #     ①Aを直す経路で担当 ②Bを担当（このときAの記録は捨てられる）
        #     ③Aをふつうの経路で担当 → ★通ってしまった★
        #   タスク単位の記録（guard_slug）は機種を替えると捨てるので、
        #   戻ってきたときに「前は何だったか」が残っていなかった。
        #   ★タスク名を変える迂回も同じ★（記録がタスクごとだったため）。
        #
        #   → day（日ごと・タスク名をまたいで1つ）に機種ごとのモードを残す。
        #     日が変われば _day() が丸ごと作り直すので、翌日は自由に選べる。
        _modes = _day(data).setdefault("claim_modes_by_slug", {})
        _prev_mode = _modes.get(slug)
        if _prev_mode is not None and bool(_prev_mode) != bool(repairing):
            raise GuardError(
                f"{slug} は今日"
                + ("直す経路" if _prev_mode else "ふつうの経路")
                + "で担当しました。同じ日に"
                + ("ふつうの経路" if _prev_mode else "直す経路")
                + "へ変えられません（日を改めるか、別の機種にしてください）"
                "。枠は使っていません")

        # ★★無人タスクは、記録されていないコードでは動かない★★
        #   （2026-08-21・台帳#237/#270）
        _dirty = unattended_dirty_code(task)
        if _dirty:
            raise GuardError(
                "コミットされていないスクリプトがあります: "
                + " / ".join(_dirty[:3])
                + (f" ほか{len(_dirty) - 3}件" if len(_dirty) > 3 else "")
                + "。★無人タスクは記録されたコードだけで動きます★"
                "（対話セッションでコミットしてください）。枠は使っていません")

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
                       "repairing", "repair_issues", "final_stage",
                       # ★照合の記録も捨てる★（2026-08-21・Codex依頼249）
                       #   これが残ると、1機種目の「見た内容」で
                       #   2機種目の verify-commit が通ってしまう。
                       "approved_files", "verified_commit"):
                _e0.pop(_k, None)
        _e0["guard_slug"] = slug
        # ★★無人で動いているのか、人が手で動かしたのか★★（2026-08-30）
        #   push前の関所は「無人だった担当がある日」だけ照合を求める。
        #   ★直す前は見分けていなかった★ので、
        #   タスクを手で試した日は、対話セッションが記事データを触った
        #   コミットを一切 push できなかった
        #   ＝**タスクを手で試すと、その日は仕事が出せない**。


        # ★数を数える★（2026-08-21・MACHINES_PER_DAY を 1 → 3 にしたときに直した）
        #   それまでは「今日の担当は1つ」という書き方で**数えていなかった**ので、
        #   設定値を増やしても文言が変わるだけで挙動は1機種のままだった。
        #   ★設定値を変えたら、実装が本当に追随しているか動かして確かめる★
        if finding:
            # ★★台帳番号ではなく「見つけたもの」で担当する経路★★
            #   （2026-08-21・Codexの設計レビュー）
            #   ★なぜ要るか★＝直す経路は台帳番号を必須にしているので、
            #   「その場で2AIが決めて直す」流れをそのままでは通せなかった。
            #   台帳は人が付けた札で、しかも人しか閉じない。
            #   ★札の代わりに、いまのHEADで見つけ直した内容そのもの★を鍵にする。
            import repair_journal as _rj
            try:
                _rec = _rj.load(str(finding))
            except Exception as _e:           # noqa: BLE001
                raise GuardError(f"見つけたものの記録を読めません: {_e}")
            if _rec.get("slug") != slug:
                raise GuardError(
                    f"#{finding} は {_rec.get('slug')!r} のものです（{slug} ではありません）")
            if _rec.get("state") == _rj.ESCALATED:
                raise GuardError(
                    f"#{finding} は人へ回した後です（{_rj.MAX_ATTEMPTS}回で決まらなかった）")
            _entry(data, task)["decision_finding"] = str(finding)

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

        # ★修理モードの記録★（固定の検査は上へ移した）
        _e1 = _entry(data, task)
        _e1["repairing"] = bool(repairing)
        # ★日ごと・機種ごとにも残す★＝機種を替えて戻ってきても変えられない
        _day(data).setdefault("claim_modes_by_slug", {})[slug] = bool(repairing)

        done_today = d.setdefault("slugs_today", [])
        # 途中まで進めた機種を続ける場合は、新しく数えない
        if slug not in done_today:
            if MACHINES_PER_DAY and len(done_today) >= MACHINES_PER_DAY:
                raise GuardError(
                    f"今日はすでに {len(done_today)} 機種を担当しています"
                    f"（1日{MACHINES_PER_DAY}機種・{' / '.join(done_today)}）。"
                    f"{slug} は明日以降に回してください")
            done_today.append(slug)
        d["target_slug"] = slug
        e["target_slug"] = slug
        _save(path, data)
        return e

def codex_round(task: str, path: str = STATE_PATH, lane: str = "main",
                budget_path: str = BUDGET_PATH, now_hhmm: str = "") -> int:
    """Codexへ1往復ぶん使う。★上限を超えたら拒否（必ず終わるため）★

    ★lane="ask" は2AIへの質問のやり直し用★（2026-08-12・依頼164のP1）
      新台の突き合わせと同じ勘定にすると枠を食い合い、
      どちらかが必ず欠ける晩ができる。勘定を分けて両立させる。
    """
    # ★★相談のときも締切を見る★★（2026-08-27・Codexの5回目の指摘4）
    #   ★回数の上限を外したので、締切だけが「必ず終わる」保証になった★。
    #   ところが締切の検査は**書き換えの予約のときにしか無かった**ので、
    #   相談だけを続ければ締切を越えられた。
    _dl = str((budget(budget_path) or {}).get("deadline_hhmm") or "")
    if past_deadline(_dl, now_hhmm):
        raise GuardError(
            f"締切（{_dl}）を過ぎているので、これ以上は相談しません")
    key = "codex_rounds" if lane == "main" else f"codex_rounds_{lane}"
    limit = CODEX_ROUND_LIMIT if lane == "main" else CODEX_ASK_ROUND_LIMIT
    with _Exclusive(path):
        data = _load(path)
        e = _entry(data, task)
        used = int(e.get(key) or 0)
        # ★0 は上限なし★（2026-08-27）＝数えるだけで断らない
        if limit and used >= limit:
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


SHARED_FILES = ("assets/data/machines.json", "service-worker.js", "sitemap.xml")


def _shared_file_touches_others(rel: str, slug: str) -> bool:
    """全機種共通のファイルで、その機種以外の項目まで変わっていないか。

    ★なぜ要るのか★（2026-08-21・台帳#429）
      狙い目チェッカーの値は `assets/data/machines.json`（全機種共通）にある。
      直す経路が「その機種のファイルだけ」しか許していなかったので、
      **チェッカーの値を直す道が完全に塞がっていた**
      （2026-08-21の更新タスクが実際に行き止まりに当たった）。
    ★かといって丸ごと許すと、他の機種の値も一緒に変えられる★
      → **その機種の項目だけが変わっているか**を見る。

    戻り値 True＝他の機種にも変更が及んでいる（＝止めるべき）。
    ★読めないときは True★（fail-closed）。
    """
    if rel == "assets/data/machines.json":
        rc, out = _git("show", f"HEAD:{rel}")
        if rc != 0:
            return True
        try:
            old = json.loads(out)
            with open(os.path.join(BASE, rel.replace("/", os.sep)),
                      encoding="utf-8") as f:
                new = json.load(f)
        except Exception:                                    # noqa: BLE001
            return True
        if not isinstance(old, list) or not isinstance(new, list):
            return True
        # ★並びと重複まで見る★（2026-08-21・Codex依頼249の防御1）
        #   辞書にすると、同じslugを増やしても集合が変わらず
        #   「機種の増減を止める」という約束を守れていなかった。
        if len(old) != len(new):
            return True
        for a, b in zip(old, new):
            sa, sb = a.get("slug"), b.get("slug")
            if sa != sb:
                return True          # 同じ位置に同じ機種が居ない＝並びが動いた
            if sa == slug:
                continue
            if a != b:
                return True
        return False
    if rel == "service-worker.js":
        # ★★これは全読者への応答を変えられる実行コード★★
        #   （2026-08-21・Codex依頼249の指摘3。「中身を持たない共有ファイル」ではない）
        #   記事を直すときに触ってよいのは**キャッシュ名の1行だけ**。
        rc, out = _git("show", f"HEAD:{rel}")
        if rc != 0:
            return True
        try:
            with open(os.path.join(BASE, rel.replace("/", os.sep)),
                      encoding="utf-8") as f:
                now = f.read()
        except Exception:                                    # noqa: BLE001
            return True
        a = _normalize_eol(out.encode("utf-8")).decode("utf-8").splitlines()
        b = _normalize_eol(now.encode("utf-8")).decode("utf-8").splitlines()
        if len(a) != len(b):
            return True                  # 行数が変わっている＝1行の差し替えではない
        diff = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
        if len(diff) != 1:
            return True
        line = b[diff[0]]
        # ★変わってよいのは CACHE_NAME の行だけ★
        return not re.match(r"^const CACHE_NAME = 'uchidokoro-v\d+';\s*$", line)
    if rel == "sitemap.xml":
        # ★その機種のURLの出入りだけを許す★（他の機種の行が動いていたら止める）
        rc, out = _git("show", f"HEAD:{rel}")
        if rc != 0:
            return True
        try:
            with open(os.path.join(BASE, rel.replace("/", os.sep)),
                      encoding="utf-8") as f:
                now = f.read()
        except Exception:                                    # noqa: BLE001
            return True
        mine = f"/machines/{slug}/"
        a = [x.strip() for x in out.splitlines() if mine not in x]
        b = [x.strip() for x in now.splitlines() if mine not in x]
        return a != b
    return True                          # ★知らない共有ファイルは許さない★


def _unrelated_changes(slug: str) -> list:
    """★その機種と関係のない変更が混ざっていないか★（2026-08-21・依頼246の指摘3）

    直す経路で触ってよいのは、
      ①その機種の記事データ ②その機種のページ
      ③全機種共通のファイル（machines.json / service-worker.js / sitemap.xml）
        ただし **machines.json はその機種の項目だけが変わっている場合に限る**
        （2026-08-21・台帳#429。チェッカーの値がここにあるため）
    ★コマンドは固定の引数配列で呼ぶ★（シェルを通さない）。
    見られなければ「関係ないものがある」と答える（fail-closed）。
    """
    allow = (f"assets/data/machine-details/{slug}.json",
             f"machines/{slug}/")
    # ★gitを呼ぶ入口は1つにそろえる★（2026-08-21）
    #   ここだけ subprocess を直接呼んでいたので、試験で差し替えても
    #   **本物のgitを見に行き、その時のリポジトリの状態で結果が変わっていた**。
    rc, stdout = _git("status", "--porcelain")
    if rc != 0:
        return ["変更を確認できません（git status が失敗）"]
    out = []
    for line in stdout.splitlines():
        name = line[3:].strip().strip('"')
        if not name:
            continue
        # ★名前の変更は「移動元」も見る★（2026-08-21・依頼247の指摘3）
        #   移動先だけ見ていたので、`R 別機種/index.html -> 対象機種/index.html`
        #   のように**別機種のファイルを消して流用する**変更が同じコミットに入れた。
        names = [x.strip().strip('"') for x in name.split(" -> ")] if " -> " in name \
            else [name]
        for nm in names:
            if not nm:
                continue
            if any(nm.startswith(a) for a in allow):
                continue
            if nm in SHARED_FILES:
                # ★共通ファイルは、その機種の話に収まっているときだけ許す★
                if _shared_file_touches_others(nm, slug):
                    out.append(nm + "（他の機種にも変更が及んでいます）")
                continue
            out.append(nm)
    return out


def _git(*args) -> tuple[int, str]:
    """★固定の引数配列でgitを呼ぶ★（シェルを通さない）。戻り値 (終了コード, 標準出力)

    ★文字コードは必ず UTF-8 で読む★（2026-08-21・実データで落ちた）
      `text=True` だけだと Windows の既定（cp932）で読もうとして、
      日本語を含むファイルの中身を取り出したときに例外になり、
      **戻り値が None になって呼び出し側が AttributeError で落ちた**。
      ★試験では気づけない型★＝差し替えた偽物は日本語を含まなかった。
    """
    try:
        p = subprocess.run(["git", "-C", str(BASE), *args],
                           capture_output=True, timeout=60)
    except Exception as e:                                   # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"
    try:
        out = p.stdout.decode("utf-8")
    except UnicodeDecodeError:
        # ★読めない中身は「読めない」と返す★（勝手に化けさせない）
        return 1, ""
    return p.returncode, out


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


# ★★無人タスクは「記録されたコード」だけで動く★★（2026-08-21・台帳#237/#270）
#   ★実際に起きたこと★
#     2026-08-05 の add-machine 実行中に、対話セッションが
#     scripts/task_guard.py を書き換えていた（23:37:57 開始 / 23:38:57 変更）。
#     2026-08-07 は、未コミットの scripts/add_machine_run.py と task_guard.py で
#     無人実行がまるごと走った。
#   ★何がまずいか★
#     ①レビューも記録もされていないコードで公開処理が動く
#     ②作業ツリーにしか無いので、PCが壊れたら消える
#     ③1回の実行の中で新旧が混ざりうる
#       （起動時に読んだ版で走りつつ、途中の subprocess は新しい版を読む）
#   ★止める場所★＝担当を取るところ（claim）。ここで断れば枠も使わない。
#   ★対話セッションは止めない★＝ここを通るのは無人タスクだけ。
UNATTENDED_TASKS = ("add-machine", "update-machine", "quality-review",
                    "uchidokoro-add-machine", "uchidokoro-update-machine",
                    "uchidokoro-quality-review")


def lock_is_live() -> bool:
    """★いま無人タスクのロックが生きているか★（2026-08-30）

    手順書は無人タスクに `task_lock.py acquire` を必須にしている。
    ＝ロックが生きていれば「無人で動いている」、無ければ「人が手で動かした」。
    ★読めないときは「生きている」と答える★（fail-closed）＝
      分からないのに「手動だ」と言うと、関所を素通りさせてしまう。
    """
    try:
        import task_lock as _tl
        data = _tl._read_lock(_tl.LOCK_PATH)
        if not data:
            return False
        age = _tl._age_minutes(data)
        if age is None:
            return True
        return age < _tl.STALE_MINUTES
    except Exception:                                        # noqa: BLE001
        return True


def unattended_dirty_code(task: str) -> list:
    """無人タスクが「記録されていないコード」で動こうとしていないか。

    ★見るのは scripts/ の中だけ★＝記事データや台帳は、
      無人タスク自身が書くものなので対象にしない。
    ★読めないときは空を返す★＝git が無い環境で止めない（呼ぶ側が判断）。
    """
    if not any(str(task).endswith(t) or str(task) == t
               for t in UNATTENDED_TASKS):
        return []
    names, why = _changed_files()
    if why:
        return []
    return sorted(n for n in names
                  if n.startswith("scripts/") and n.endswith(".py"))


def _file_digest(rel: str) -> str:
    """手元のファイルの中身の指紋。消えている場合は DELETED。

    ★改行の書き方の違いで食い違わせない★（2026-08-21・台帳#430）
      この会社PCは `core.autocrlf=true` なので、リポジトリの中はLF、
      作業ファイルはCRLFで保存される。**中身は同じで改行だけが違う**のに、
      バイトで比べると必ず食い違い、verify-commit が毎回止まっていた
      （2026-08-21の更新タスクが実測: コミット10168バイト/CRLF 0個 対
        作業ファイル10392バイト/CRLF 224個）。
      → ★gitがリポジトリへ入れる形（LF）にそろえてから指紋を取る★
      ★中身が本当に変わっていれば、そろえても指紋は変わる★ので守りは弱まらない。
    """
    p = os.path.join(BASE, rel.replace("/", os.sep))
    if not os.path.isfile(p):
        return "DELETED"
    with open(p, "rb") as f:
        data = f.read()
    return hashlib.sha256(_normalize_eol(data)).hexdigest()


def _normalize_eol(data: bytes) -> bytes:
    """改行をLFにそろえる（★中身の比較のためだけ★）。

    ★画像などのバイナリは触らない★＝NUL文字が入っていたらそのまま返す
    （gitも同じ考え方でバイナリを判定する）。
    """
    if b"\x00" in data[:8000]:
        return data
    return data.replace(b"\r\n", b"\n")


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
    # ★手元と同じ物差しで比べる★（台帳#430。git の中はLFなので普通はそのまま）
    return hashlib.sha256(_normalize_eol(p.stdout)).hexdigest()


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
        # ★★その日ぶんは消さずにためる★★（2026-08-21・Codexの指摘3）
        #   ★直す前に何が起きるか★＝
        #     記録はタスクに1つしかなく、機種を替えると捨てられる。
        #     1日3機種にしたので、3機種ぶんをコミットしてから
        #     **最後にまとめて push すると、1・2機種目が「照合していない」
        #     扱いになり、push の関所が止める**。
        #   ＝機種ごとに push すれば動くが、まとめると動かない、という
        #     やり方に依存した作りだった。
        #   ★日ごとの一覧は機種を替えても消さない★（_day は日付で切り替わる）。
        _dv = _day(data).setdefault("verified_commits", [])
        if head not in _dv:
            _dv.append(head)
        _save(path, data)
        return {"slug": slug, "commit": head, "files": len(in_commit),
                "ok": True, "verified_today": len(_dv)}


def before_write(task: str, slug: str, path: str = STATE_PATH,
                 repairing: bool = False, finding=None) -> dict:
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
        if finding or e.get("decision_finding"):
            # ★★「2AIが決めた」ことを、書く直前にもう一度確かめる★★
            #   （2026-08-21・Codexの設計レビュー
            #    「適用直前に変更前指紋を再照合」「AI合意が書き換え許可証に
            #     ならないようにする」）
            import repair_journal as _rj
            _fid = str(finding or e.get("decision_finding"))
            if e.get("decision_finding") and _fid != e["decision_finding"]:
                raise GuardError(
                    f"{slug} は #{e['decision_finding']} を直す担当です。"
                    "途中で対象を変えられません")
            try:
                _rec = _rj.load(_fid)
            except Exception as _ex:          # noqa: BLE001
                raise GuardError(f"見つけたものの記録を読めません: {_ex}")
            if _rec.get("slug") != slug:
                raise GuardError(f"#{_fid} は {_rec.get('slug')!r} のものです")
            if _rec.get("state") != "AGREED":
                raise GuardError(
                    f"#{_fid} はまだ書いてよい段階ではありません"
                    f"（いま {_rec.get('state')} ／ AGREED になってから書けます）")
            # ★見つけたときから記事が変わっていないか★
            _want = _rec.get("source_sha256") or ""
            if _want:
                _dp = os.path.join(BASE, "assets", "data", "machine-details",
                                   slug + ".json")
                if not os.path.exists(_dp):
                    raise GuardError(f"{slug} の記事データがありません")
                with open(_dp, encoding="utf-8") as _f:
                    _now = hashlib.sha256(_f.read().encode("utf-8")
                                          .replace(b"\r\n", b"\n")).hexdigest()
                if _now != _want:
                    raise GuardError(
                        f"#{_fid} を見つけたときから記事が変わっています"
                        f"（{_want[:12]}… → {_now[:12]}…）。見つけ直してください")

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
        # ★★何もしていない完了を、記録の上で見えるようにする★★
        #   （2026-08-27・Codexの指摘17）
        #   ★断らない★＝段階名の言い回しは手順書ごとに違うので、
        #   ここで拒否すると正しく動いているタスクを止めかねない。
        #   ★まず見えるようにする★＝「静かに0件が続く」を数えるのが先。
        # ★★タスク単位で数える★★（2026-08-27・Codexの2回目の指摘7）
        #   ★直す前はその日全体の数を見ていた★ので、
        #   ★別のタスクが書いた日は、何もしなくても印が付かなかった★
        #   ＝まさに見たかった「静かな0件」を見落とす。
        #   ★予約の履歴にはタスク名が入っている★ので、それで数える。
        _mine = [r for r in (data.get("reservations") or [])
                 if r.get("task") == task and r.get("date") == _today()]
        e["work_today"] = {"reserved": len(_mine), "task": task}
        e["no_work"] = not _mine
        e["final_stage"] = stage
        e["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _save(path, data)
        return e

def _no_work_tests(t, tmpdir) -> None:
    """★何もしていない完了が、記録の上で見えること★（Codexの指摘17）

    ★断らない★＝段階名の言い回しは手順書ごとに違うので、
    ここで拒否すると正しく動いているタスクを止めかねない。
    ★まず見えるようにする★＝「静かに0件が続く」を数えるのが先。
    """
    import json as _jn
    sp = os.path.join(tmpdir, "nowork.json")

    def _put(res):
        with open(sp, "w", encoding="utf-8") as f:
            _jn.dump({"tasks": {}, "reservations": res,
                      "day": {"date": _today(),
                              "writes": {"total": len(res), "fix": len(res),
                                         "grow": 0},
                              "slugs_today": [r["slug"] for r in res]}}, f)

    _put([])
    e = done("t_nowork", "", "COMPLETED_NO_CHANGE", path=sp)
    t("★★何もしていない完了に印が付く★★"
      "／★直す前は、作業0件の正常終了を誰も数えられなかった★",
      e.get("no_work") is True and e["work_today"]["reserved"] == 0)

    # ★★別のタスクが書いた日でも、自分が何もしていなければ印が付く★★
    #   （2026-08-27・Codexの2回目の指摘7）
    #   ★直す前はその日全体の数を見ていた★ので、
    #   ★別のタスクが書いた日は、何もしなくても印が付かなかった★
    #   ＝まさに見たかった「静かな0件」を見落とす。
    _put([{"token": "x", "task": "よそのタスク", "slug": "a", "kind": "fix",
           "state": "RESERVED", "date": _today()}])
    e2 = done("t_nowork", "", "COMPLETED", path=sp)
    t("★★別のタスクが書いた日でも、自分の0件は見落とさない★★",
      e2.get("no_work") is True and e2["work_today"]["reserved"] == 0)

    _put([{"token": "y", "task": "t_nowork", "slug": "b", "kind": "fix",
           "state": "RESERVED", "date": _today()}])
    e3 = done("t_nowork", "", "COMPLETED", path=sp)
    t("　（対照）自分が書いた日は印が付かない",
      e3.get("no_work") is False and e3["work_today"]["reserved"] == 1)


# ---------------------------------------------------------------- selftest

def _machines_per_day_tests(t, tmpdir) -> None:
    """★★1日の機種数と、書き換えの予算を「一緒に」通す試験★★

    （2026-08-21・MACHINES_PER_DAY を 3 へ戻す条件③）

    ★なぜ要るか★
      それまでの試験は、この2つを**別々にしか見ていなかった**。
        ・_budget_tests は担当（target_slug / slugs_today）を毎回消してから
          予算だけを見る
        ・claim の試験は予算を通らない
      そのため「機種数は3にできるのに、予約の段階で2機種目が断られる」
      という食い違いが**どちらの試験にも映らなかった**。
      実際そうなっていた＝reserve() は target_slug（1つだけ）で見ていて、
      MACHINES_PER_DAY を3にしても2機種目で必ず止まった。

    ★ここで確かめること★
      1日 = MACHINES_PER_DAY 機種 で、
      その内訳が writes_fix + writes_grow ちょうどに収まること。
      （設定＝3機種 ＝ 修正2 ＋ 育成1）
    """
    import json as _j
    bp = os.path.join(tmpdir, "mpd_budget.json")
    sp = os.path.join(tmpdir, "mpd_state.json")
    # ★本番と同じ内訳を使う★（assets/data/task-budget.json と同じ形）
    real = budget()
    with open(bp, "w", encoding="utf-8") as f:
        _j.dump({"schema_version": "task-budget/v1",
                 "writes_total": real["writes_total"],
                 "writes_fix": real["writes_fix"],
                 "writes_grow": real["writes_grow"],
                 "inspections": real["inspections"],
                 "deadline_hhmm": "23:59"}, f)
    SHA = "sha256:" + "a" * 64

    # ★★取り決めが変わった★★（2026-08-25・運営者の指示）
    #   ★育成は件数で止めない。止めるのは締切（deadline_hhmm）★。
    #   修正（writes_fix）は据え置きで段階ルールを続ける。
    #   ここで確かめるのは「数の辻褄」ではなく、
    #   ★上限なし（0）と上限あり（正の数）が、それぞれ意図どおり働くこと★。
    t("★★育成は件数で止めない（上限なしの設定になっている）★★"
      "／★1日1機種だと、対象10機種で1機種あたり10日に1回しか見られない★",
      real["writes_grow"] == 0 and MACHINES_PER_DAY == 0)
    t("　既存記事の修正は、いまも件数で止める（段階ルールを続ける）",
      real["writes_fix"] > 0)

    def take(kind, slug):
        """予約 → 着手 → 巻き戻し、を1機種ぶん通す（担当は消さない）"""
        r = reserve("t", slug, kind, path=sp, budget_path=bp, now_hhmm="20:00",
                    contract_sha256=SHA)
        begin_apply(r["token"], slug, kind, SHA, "t-" + slug, path=sp)
        advance(r["token"], "ROLLED_BACK_VERIFIED", path=sp)
        return r

    # ★担当を消さずに、機種を替えながら枠を使い切る★
    fixes = real["writes_fix"]
    ok = True
    for i in range(fixes):
        ok = ok and bool(take("fix", "mpd_fix_%d" % i)["token"])
    t(f"★★修正の枠ぶん（{fixes}機種）を、担当を消さずに続けて取れる★★"
      "（直す前は2機種目の予約で必ず断られた）", ok)

    # ★★育成は、機種を替えながらいくつでも取れる★★
    #   （2026-08-25・上限を撤廃したので、ここが止まらないことを確かめる）
    for i in range(12):                    # 育成対象10機種より多く試す
        ok = ok and bool(take("grow", "mpd_grow_%d" % i)["token"])
    t("★★育てる処理は、機種を替えれば何機種でも取れる★★"
      "／★止まると、頻度の表（導入30日以内は毎日）が守れない★", ok)

    t("★★修正の枠は、いまも使い切ったら断る★★"
      "／★こちらは記事の中身を書き換えるので、段階ルールを続ける★",
      _raises(lambda: take("fix", "mpd_over"), "上限"))

    st = day_status(path=sp)
    t("　使った数の内訳が、実際に取った数と合っている",
      st["writes"]["fix"] == real["writes_fix"]
      and st["writes"]["grow"] == 12
      and st["writes"]["total"] == real["writes_fix"] + 12)
    t("　担当した機種の数も、取った数と同じ",
      len(_load(sp)["day"]["slugs_today"]) == real["writes_fix"] + 12)

    # ★同じ機種を続けるのは、いつでも通る（やり直しを塞がない）★
    #   ★別の記録で試す★＝上で枠を使い切っているため
    sp2 = os.path.join(tmpdir, "mpd_state2.json")
    r1 = reserve("t", "mpd_same", "fix", path=sp2, budget_path=bp, now_hhmm="20:00",
                 contract_sha256=SHA)
    begin_apply(r1["token"], "mpd_same", "fix", SHA, "t-same", path=sp2)
    advance(r1["token"], "ROLLED_BACK_VERIFIED", path=sp2)
    r2 = reserve("t", "mpd_same", "fix", path=sp2, budget_path=bp, now_hhmm="20:00",
                 contract_sha256=SHA)
    t("　同じ機種なら、担当の数は増えない（やり直しを塞がない）",
      bool(r2["token"]) and _load(sp2)["day"]["slugs_today"] == ["mpd_same"])


def _alive_posix_tests(t) -> None:
    """★Linux側の道を、Windowsからも実際に動かす★（2026-08-21）

    ★なぜ要るか★＝`os.name` で分けると、CIで通る道が手元で一度も
    動かないまま push される。実際それでCIが赤くなった。
    """
    def gone(pid, sig):
        raise ProcessLookupError

    def denied(pid, sig):
        raise PermissionError

    def here(pid, sig):
        return None

    def broken(pid, sig):
        raise OSError("よく分からない失敗")

    t("　（Linux）居るプロセスは生きている", _alive_posix(1, here) is True)
    t("★★（Linux）居ないプロセスは死んでいると分かる★★"
      "（tasklist だけだとLinuxでは常に生きている扱いだった）",
      _alive_posix(999999, gone) is False)
    t("　（Linux）権限が無いときは奪わない", _alive_posix(1, denied) is True)
    t("　（Linux）分からないときは奪わない（安全側）",
      _alive_posix(1, broken) is True)


def _finding_tests(t, tmpdir) -> None:
    """★見つけたもの（finding）で担当する経路の試験★

    ★なぜ要るか★＝直す経路は台帳番号を必須にしていたので、
    「その場で2AIが決めて直す」流れをそのまま通せなかった。
    台帳番号は人が付けた札で、しかも人しか閉じない。

    ★守っているもの★
      ・合意する前は書けない（AI合意が書き換え許可証にならないように）
      ・別の機種の記録では担当できない
      ・打ち切った後の記録では担当できない
      ・見つけたときから記事が変わっていたら書かせない
      ・担当した対象を途中で変えられない
    """
    import repair_journal as rj

    keep_store = rj.STORE
    rj.STORE = os.path.join(tmpdir, "repairs")
    fp = os.path.join(tmpdir, "finding_state.json")
    try:
        # ★実データから「いま書ける機種」を選ぶ★
        #   （固定名にすると、その機種が台帳で止まった日に試験が落ちる）
        slug = None
        for _m in _sj.read_rows(os.path.join(BASE, "assets", "data", "machines.json")):
            try:
                if cp.assess(_m["slug"])["stage"] in WRITABLE_STAGES:
                    slug = _m["slug"]
                    break
            except Exception:             # noqa: BLE001
                continue
        if not slug:
            t("　（いま書ける機種が1つも無いので、この確認は行わない）", True)
            return
        dp = os.path.join(BASE, "assets", "data", "machine-details",
                          slug + ".json")
        with open(dp, encoding="utf-8") as f:
            sha = hashlib.sha256(
                f.read().encode("utf-8").replace(b"\r\n", b"\n")).hexdigest()

        fid = rj.detect(slug, "text_gone", "この文はためしの文です。", "x",
                        source_sha256=sha)["finding_id"]

        claim("t_find", slug, fp, finding=fid)
        t("　見つけたもので担当できる（台帳番号は要らない）",
          day_status(fp).get("target_slug") == slug)

        t("★★合意する前は書けない★★（AI合意を書き換え許可証にしない）",
          _raises(lambda: before_write("t_find", slug, fp), "AGREED"))

        _other = "zzz_other_machine"
        fid_other = rj.detect(_other, "text_gone", "よその機種の文です。",
                              "y", source_sha256="9" * 64)["finding_id"]
        t("★★別の機種の記録では担当できない★★",
          _raises(lambda: claim("t_find2", slug, fp, finding=fid_other),
                  _other))

        fid_esc = rj.detect(slug, "text_gone", "打ち切る文です。",
                            "z", source_sha256="9" * 64)["finding_id"]
        # ★★本番と同じ順で3回まわす★★（2026-08-27・Codexの2回目の指摘3）
        #   ★直す前は、封もCodexの受け取りもせずに3回数えていた★
        #   ＝この試験そのものが「判断せずに人へ回せる」穴の実演だった。
        _vesc = os.path.join(tmpdir, "v_esc.md")
        with open(_vesc, "w", encoding="utf-8") as _f:
            _f.write("私の判定です。この件は決められませんでした。")
        for _i in range(rj.MAX_ATTEMPTS):
            rj.seal_claude(fid_esc, _vesc)
            rj.record_codex(fid_esc, "c" * 64,
                            f"{_i + 1}回目のCodexの判定です。決まりません。")
            rj.attempt(fid_esc, "決まらない")
        t("★★人へ回した後の記録では担当できない★★",
          _raises(lambda: claim("t_find3", slug, fp, finding=fid_esc),
                  "人へ回した"))

        vp = os.path.join(tmpdir, "v.md")
        with open(vp, "w", encoding="utf-8") as f:
            f.write("私の判定: この文は前と同じ内容なので消してよいと考えます。")

        def _decf(_fid, _slug, _sha, _name,
                  _text="この文はためしの文です。"):
            """★合意は決定ファイルそのものを読む★（2026-08-27・Codexの指摘6）

            ★打ち直した配列は受け取らない★＝合意した中身と、
            実際に当てる中身を同じものにするため。
            """
            _p = os.path.join(tmpdir, _name + ".json")
            with open(_p, "w", encoding="utf-8") as _f:
                json.dump({"schema_version": "decide-now/v1", "slug": _slug,
                           "finding_id": _fid, "source_sha256": _sha,
                           "decided_by": ["Claude", "codex"],
                           # ★指摘された一文を実際に触る決定にする★
                           #   （2026-08-29・台帳#499の案B）
                           #   触らない決定では合意できなくなった。
                           "actions": [{"op": "drop", "text": _text,
                                        "why": "重複"}]},
                          _f, ensure_ascii=False)
            return _p

        rj.seal_claude(fid, vp)
        rj.record_codex(fid, "b" * 64, "Codexの判定です。同じく消してよいです。")
        rj.agree(fid, _decf(fid, slug, sha, "d_find"), "text_gone",
                 ["Claude", "codex"])
        t("　合意したら書ける", not _raises(lambda: before_write("t_find", slug, fp)))

        fid_moved = rj.detect(slug, "text_gone", "別のためしの文です。", "w",
                              source_sha256="0" * 64)["finding_id"]
        rj.seal_claude(fid_moved, vp)
        rj.record_codex(fid_moved, "b" * 64,
                        "Codexの判定です。同じく消してよいと考えます。")
        rj.agree(fid_moved, _decf(fid_moved, slug, "0" * 64, "d_moved",
                                  "別のためしの文です。"),
                 "text_gone", ["Claude", "codex"])
        claim("t_find4", slug, fp, finding=fid_moved)
        t("★★見つけたときから記事が変わっていたら書かせない★★",
          _raises(lambda: before_write("t_find4", slug, fp), "変わっています"))

        t("★★担当した対象を途中で変えられない★★",
          _raises(lambda: before_write("t_find4", slug, fp, finding=fid),
                  "変えられません"))
    finally:
        rj.STORE = keep_store


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
        # ★1日の機種数の縛りとは別に、予算の増減だけを見たい★
        #   （担当をいったん白紙にしてから取る）
        # ★slugs_today も消す★（2026-08-21）＝reserve() が claim() と同じ
        #   数え方になったので、target_slug だけ消しても機種数で止まる。
        #   ★両方を一緒に見る試験は _machines_per_day_tests にある★
        _d0 = _load(sp)
        _day(_d0)["target_slug"] = None
        _day(_d0)["slugs_today"] = []
        _save(sp, _d0)
        r = reserve("t", slug, kind, path=sp, budget_path=bp, now_hhmm="20:00",
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
      reserve("t", "zzz_test", "fix", path=sp, budget_path=bp, now_hhmm="20:00",
              contract_sha256="sha256:" + "a" * 64)["test"])
    # 止めたら、タスク名を変えても通らない
    halt("監査に引っかかったため", path=sp)
    sp6 = os.path.join(tmpdir, "state_claim.json")
    # ★claim を呼ばずに reserve だけ使っても、1日の機種数は守られる★
    #   （Codex115回目のP1-6。★2026-08-21に「1機種」から「MACHINES_PER_DAY
    #     機種」へ書き直した★＝reserve も claim と同じ slugs_today で数える）
    # ★★上限を撤廃しても、迂回防止の仕組みは残す★★（2026-08-25）
    #   ★本番は上限なし（0）★なので、試験の中だけ上限を立てて
    #   「claim を呼ばずに予約だけしても、機種数が守られる」ことを確かめ続ける。
    _keep_mpd6 = MACHINES_PER_DAY
    globals()["MACHINES_PER_DAY"] = 3
    try:
        for _i in range(MACHINES_PER_DAY):
            reserve("t", "きめた機種%d" % _i, "fix", path=sp6, budget_path=bp, now_hhmm="20:00",
                    contract_sha256="sha256:" + "a" * 64)
            _d6 = _load(sp6)
            _day(_d6)["writes"] = {"total": 0, "fix": 0, "grow": 0}
            _d6["reservations"] = []
            _save(sp6, _d6)
        t("★★claim を呼ばなくても、1日の機種数は守られる★★"
          "（Codex115回目のP1-6・★上限を立てたときに効くこと★）",
          _raises(lambda: reserve("t", "ちがう機種", "fix", path=sp6,
                                  budget_path=bp, now_hhmm="20:00",
                                  contract_sha256="sha256:" + "a" * 64),
                  "機種"))
    finally:
        globals()["MACHINES_PER_DAY"] = _keep_mpd6
    t("★★止めた日は、別のタスク名でも書けない★★",
      _raises(lambda: reserve("別のタスク", "g", "fix", path=sp,
                              budget_path=bp, now_hhmm="20:00",
                              contract_sha256="sha256:" + "a" * 64),
              "止めています"))
    # ★予約の確認と消費が1つのまとまりになっているか★（Codex111回目のP0-4）
    sp4 = os.path.join(tmpdir, "state_begin.json")
    tk = reserve("t", "m", "fix", path=sp4, budget_path=bp, now_hhmm="20:00",
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
                              contract_sha256="sha256:" + "a" * 64,
                              now_hhmm="07:00"), "締切"))
    t("★★契約の指紋なしでは修正の枠を取れない★★（別の契約に流用させない）",
      _raises(lambda: reserve("t", "z", "fix", path=sp2, budget_path=bp2),
              "指紋"))
    # ★やりかけがあるうちは次を始めない★
    sp3 = os.path.join(tmpdir, "state_open.json")
    tok3 = reserve("t", "p", "fix", path=sp3, budget_path=bp, now_hhmm="20:00",
                   contract_sha256="sha256:" + "a" * 64)["token"]
    begin_apply(tok3, "p", "fix", "sha256:" + "a" * 64, "t-p", path=sp3)
    t("★★やりかけの書き換えがあるうちは次を始めない★★",
      _raises(lambda: reserve("t", "p", "fix", path=sp3, budget_path=bp, now_hhmm="20:00",
                              contract_sha256="sha256:" + "a" * 64),
              "やりかけ"))
    t("★★決められた順にしか進めない★★（予約直後にpush済みとは書けない）",
      _raises(lambda: advance(tok3, "PUSH_CONFIRMED", path=sp3), "進めません"))
    t("★★普通の前進では予約を消費できない★★（照合を飛ばす経路を塞いだ）",
      _raises(lambda: advance(
          reserve("t", "r", "fix", path=os.path.join(tmpdir, "s5.json"),
                  budget_path=bp, now_hhmm="20:00",
                  contract_sha256="sha256:" + "a" * 64)["token"],
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
    # ★★試験中は「未コミットの歯止め」を通す★★（2026-08-26）
    #   ★この歯止めは本番のためのもの★＝試験は必ず作業ツリーが汚れた状態で
    #   走る（いま直しているコード自体が未コミット）ので、
    #   ここで止まると**自分の直しを一度も試せない**。
    #   ★歯止めそのものは、専用の試験で確かめる★（下の _dirty_guard_tests）。
    _keep_dirty = globals()["unattended_dirty_code"]
    globals()["unattended_dirty_code"] = lambda task: []

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
        # ★★手で動かした日に、対話セッションが仕事を出せなくなる欠陥★★
        #   （2026-08-30・運営者「手動実行は例外としないとテストできないじゃん」）
        # ★★そして、その直しに私が作った抜け穴★★（Codexの指摘1・自分で再現した）
        #   直す前はタスクごとに1つだけ持って上書きしていたので、
        #   ★無人 → 手動の順に担当すると、無人だった記録が消えた★。
        fpm = os.path.join(tmpdir, "manual.json")
        _keep_lock = globals()["lock_is_live"]
        try:
            globals()["lock_is_live"] = lambda: False
            claim("update-machine", "hokuto", path=fpm)
            _d = _load(fpm)
            t("★★手だけで動かした日は「無人はいなかった」と記録する★★"
              "（＝この日は関所が照合を求めない）",
              _d["tasks"]["update-machine"].get("unattended") is False
              and _d.get("day", {}).get("had_unattended") is False)

            globals()["lock_is_live"] = lambda: True
            _d = _load(fpm)
            _d["tasks"]["update-machine"]["guard_slug"] = None
            _save(fpm, _d)
            claim("update-machine", "sao2", path=fpm)
            t("　★ロックが生きていれば「無人」と記録する★",
              _load(fpm)["tasks"]["update-machine"].get("unattended") is True)

            globals()["lock_is_live"] = lambda: False
            _d = _load(fpm)
            _d["tasks"]["update-machine"]["guard_slug"] = None
            _save(fpm, _d)
            claim("update-machine", "enen2", path=fpm)
            _d = _load(fpm)
            t("★★一度でも無人で担当したら、あとから手動で消せない★★"
              "（＝無人の未照合コミットまで push できた穴・Codexの指摘1）",
              _d["tasks"]["update-machine"].get("unattended") is False
              and _d.get("day", {}).get("had_unattended") is True)

            fps = os.path.join(tmpdir, "sched.json")
            t("★★ロックが無くても、申告があれば無人扱い★★"
              "（＝ロックを取り忘れた無人タスクが手動に見える穴）",
              claim("update-machine", "hokuto", path=fps,
                    scheduled=True).get("ok") is not False
              and _load(fps).get("day", {}).get("had_unattended") is True)

            # ★★ロックが生きていても、手動だと明示すれば手動★★
            #   （2026-08-30・Codexの指摘1。★これが本題だった★）
            #   手動で本番手順を安全に動かすときも**ロックの取得は必須**なので、
            #   ロックを見るだけでは無人に見える。
            #   ＝手動の add_machine_run --apply --ctx が踏む形。
            globals()["lock_is_live"] = lambda: True
            fpx = os.path.join(tmpdir, "manual_with_lock.json")
            claim("add-machine", "dmm_99998", path=fpx, scheduled=False)
            t("★★ロックが生きていても、手動だと明示すれば手動★★"
              "（＝手で試した日に仕事が出せなくなる欠陥の本体）",
              _load(fpx).get("day", {}).get("had_unattended") is False)

            fpy = os.path.join(tmpdir, "silent_with_lock.json")
            claim("add-machine", "dmm_99997", path=fpy)
            t("　★申告が無ければロックで推測する★（古い呼び出し・fail-closed）",
              _load(fpy).get("day", {}).get("had_unattended") is True)
            globals()["lock_is_live"] = lambda: False

            # ★★新台の担当でも印が保存されること★★（Codexの指摘1）
            #   ★新台の分岐は先に return する★ので、記録を後ろに置いていた間は
            #   **新台では一度も保存されていなかった**（実測で確認）。
            #   ★試験用の名前（zzz_ など）を使うと、いちばん手前から返って
            #     新台分岐を一度も通らない★ので、実在しない DMM の形を使う。
            fpa = os.path.join(tmpdir, "add.json")
            claim("add-machine", "dmm_99999", path=fpa, scheduled=True)
            t("★★新台の担当でも「無人」の印が保存される★★"
              "（＝分岐が先に return して記録されない穴）",
              _load(fpa).get("day", {}).get("had_unattended") is True)
        finally:
            globals()["lock_is_live"] = _keep_lock

        # ★★ロックが読めないときは「無人」に倒す★★（fail-closed）
        #   ★分からないのに「手動だ」と答えると、関所を素通りさせてしまう★
        import task_lock as _tl9
        _keep_read = _tl9._read_lock

        def _boom(_p):
            raise OSError("読めません")

        try:
            _tl9._read_lock = _boom
            t("★★ロックが読めないときは「無人」に倒す★★"
              "（分からないのに関所を素通りさせない）", lock_is_live() is True)
        finally:
            _tl9._read_lock = _keep_read

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
        # ★上限なしの試験で使う機種も、ここで既知にしておく★
        #   （2026-08-25。架空のslugのままだと NO_MACHINE で断られ、
        #     「上限で止まった」と読み違える）
        _many_slugs = ["u%d" % i for i in range(12)]
        _known = {"hokuto", "enen", "galfy"} | set(_spares) | set(_many_slugs)
        cp.assess = lambda sl, *a, **k: {
            "stage": "READY" if sl in _known else "NO_MACHINE"}
        t("　断られた日でも枠は残る（次の候補を選べる）",
          claim("t", "hokuto", fp0)["target_slug"] == "hokuto")

        # --- ★★無人タスクは記録されていないコードでは動かない★★
        #   （2026-08-21・台帳#237/#270）
        #   ★実際に起きたこと★＝無人実行の最中に対話セッションが
        #   task_guard.py を書き換えていた／未コミットのまま一晩走った。
        _keep_ch = globals()["_changed_files"]
        _keep_udc = globals()["unattended_dirty_code"]
        try:
            # ★ここだけ本物に戻す★＝この試験は歯止めそのものを確かめるもの
            #   （selftest の先頭で、他の試験のために迂回させている）
            globals()["unattended_dirty_code"] = _keep_dirty
            globals()["_changed_files"] = lambda: (
                ["scripts/task_guard.py", "assets/data/machines.json"], "")
            # ★★新台タスクでも効くこと★★（2026-08-26・台帳#478）
            #   ★直す前は add-machine だけ上の分岐で先に return していた★ので、
            #   **いちばん危ない経路（公開してpushする側）で破れていた**。
            _fp478 = os.path.join(tmpdir, "guard478.json")
            _keep_as = cp.assess
            try:
                cp.assess = lambda sl, *a, **k: {"stage": "NO_MACHINE"}
                t("★★新台タスクでも、未コミットなら担当できない★★"
                  "／★ここが破れると、レビュー前のコードで公開してpushする★",
                  raises(lambda: claim("add-machine", "dmm_7777", _fp478),
                         "コミット"))
            finally:
                cp.assess = _keep_as
            t("★★無人タスクは、未コミットのスクリプトがあると担当できない★★",
              raises(lambda: claim("update-machine", "hokuto",
                                   os.path.join(tmpdir, "dirty.json")),
                     "コミットされていない"))
            t("　見るのは scripts/ の中だけ（記事データは無人タスク自身が書く）",
              unattended_dirty_code("update-machine")
              == ["scripts/task_guard.py"])
            t("★対話セッションは止めない★",
              unattended_dirty_code("interactive") == [])
            globals()["_changed_files"] = lambda: ([], "git status が失敗しました")
            t("　git が読めないときは止めない（呼ぶ側が判断する）",
              unattended_dirty_code("update-machine") == [])
        finally:
            globals()["_changed_files"] = _keep_ch
            globals()["unattended_dirty_code"] = _keep_udc

        # ★★上限を撤廃しても、迂回防止の仕組みは残す★★
        #   （2026-08-25・運営者の指示で本番の上限は 0＝なしにした）
        #   ★仕組みごと失わないため、試験の中だけ上限を立てて確かめる★
        #   ＝将来また上限を置いたときに、迂回できないことを保証し続ける。
        _keep_mpd = MACHINES_PER_DAY
        globals()["MACHINES_PER_DAY"] = 3
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
        # ★★新台は「1日◯機種」の枠を使わない★★（2026-08-07・運営者決定）
        #   ★ただし暴走止めはある★（2026-08-26・台帳#479）＝
        #   同じ晩に何十件も作り続けるのは不具合の形なので、
        #   UNLIMITED_RUNAWAY_CAP で止めて知らせる。
        for i in range(5, UNLIMITED_RUNAWAY_CAP):
            claim("add-machine", "n%d" % i, fp2)
        # ★鍵が無いときに例外で落ちない★（2026-08-26）
        #   ★落ちると「試験が❌」ではなく「ただ落ちた」になり、
        #     この守りを見ている試験がある証拠にならない★（壊し方の道具の指摘）
        _nrec = (_load(fp2).get("night") or {}).get("slugs") or []
        t("★★新台は普通の枠を使わない（上限まで続けて担当できる）★★"
          "／★記録は「一晩」の入れ物に入る（暦日の入れ物ではない）★",
          len(_nrec) == UNLIMITED_RUNAWAY_CAP)
        t("　上限に当たったら止めて知らせる",
          raises(lambda: claim("add-machine", "n999", fp2), "同じ晩"))
        # ★★日付をまたいでも同じ晩として数える★★
        #   （2026-08-26・Codex29回目の指摘3。★実際に2倍通っていた★）
        #   ★暦日で数えると 23:30〜23:59 に20件、00:00〜04:30 にもう20件★
        #   ＝説明文の「1晩20件」と実装が食い違っていた。
        _n_pre = _night_id(datetime(2026, 8, 26, 23, 45))
        _n_post = _night_id(datetime(2026, 8, 27, 3, 15))
        _n_next = _night_id(datetime(2026, 8, 27, 23, 45))
        t("★★23:45 と、その日をまたいだ 03:15 は同じ晩★★"
          "／★暦日で数えると別々になり、上限が2倍になる★",
          _n_pre == _n_post == "2026-08-26")
        t("　翌日の 23:45 は次の晩（いつまでも同じ晩にはならない）",
          _n_next == "2026-08-27" and _n_next != _n_pre)
        t("　昼（12:00）でその日の晩に切り替わる",
          _night_id(datetime(2026, 8, 27, 11, 59)) == "2026-08-26"
          and _night_id(datetime(2026, 8, 27, 12, 0)) == "2026-08-27")
        # ★対照実験★＝入れ物そのものが日付で入れ替わることを見る
        _nd = {}
        _night(_nd, datetime(2026, 8, 26, 23, 45))["slugs"].append("zzz_a")
        _keep_a = list(_night(_nd, datetime(2026, 8, 27, 3, 15))["slugs"])
        _keep_b = list(_night(_nd, datetime(2026, 8, 27, 23, 45))["slugs"])
        t("★★日付をまたいでも記録が残る（消えたら上限が空く）★★",
          _keep_a == ["zzz_a"])
        t("　次の晩になれば記録は空になる（いつまでも溜まらない）",
          _keep_b == [])
        t("★★暴走止めに当たったら止めて知らせる★★"
          "／★説明文には「ある」と書いてあるのに、実装は記録するだけだった★",
          raises(lambda: claim("add-machine", "n_over", fp2), "上限"))
        t("　すでに担当した機種なら、上限に当たっていても続けられる",
          claim("add-machine", "n5", fp2)["target_slug"] == "n5")
        globals()["MACHINES_PER_DAY"] = _keep_mpd
        # ★本番の設定（上限なし）でも、機種を替えて何機種でも担当できる★
        fp3 = os.path.join(tmpdir, "guard3.json")
        _many3 = [claim("t", "u%d" % i, fp3)["target_slug"] for i in range(12)]
        t("★★上限なしの設定では、何機種でも担当できる★★"
          "／★ここが止まると、頻度の表（導入30日以内は毎日）が守れない★",
          _many3 == ["u%d" % i for i in range(12)])

        # ★★いまは上限なし（0）★★（2026-08-27・運営者の指示）
        #   ★仕組みは残す★＝あとで回数を決め直すときに、また効くように。
        # ★試験は自分の締切を使う★（本番の締切を読むと、
        #   実行した時刻で落ちたり通ったりする＝たまに落ちる検査になる）
        _bp = os.path.join(tmpdir, "budget_codex.json")
        with open(_bp, "w", encoding="utf-8") as _f:
            json.dump({"schema_version": "task-budget/v1",
                       "writes_total": 0, "writes_fix": 0, "writes_grow": 0,
                       "inspections": 0, "deadline_hhmm": "23:59"}, _f)
        for i in (1, 2, 3, 4, 5):
            codex_round("t", fp, budget_path=_bp, now_hhmm="20:00")
        t("★★上限なし（0）なら、何回でも相談できる★★"
          "／★まず通ることを確かめてから回数を決める、という運営者の判断★",
          _load(fp)["tasks"]["t"]["codex_rounds"] == 5)
        t("　回数はファイルに残る（落ちて再起動しても数え直しにならない）",
          _load(fp)["tasks"]["t"]["codex_rounds"] == 5)
        # ★対照★＝上限を入れれば、ちゃんと止まる
        _keep_lim = globals()["CODEX_ROUND_LIMIT"]
        try:
            globals()["CODEX_ROUND_LIMIT"] = 5   # ★既に5回使っている★
            t("　（対照）上限を入れれば止まる",
              raises(lambda: codex_round("t", fp, budget_path=_bp,
                                        now_hhmm="20:00"), "上限"))
        finally:
            globals()["CODEX_ROUND_LIMIT"] = _keep_lim

        # ★★締切を過ぎたら相談しない★★（2026-08-27・Codexの5回目の指摘4）
        #   ★回数の上限を外したので、締切だけが「必ず終わる」保証★
        _bp2 = os.path.join(tmpdir, "budget_over.json")
        with open(_bp2, "w", encoding="utf-8") as _f:
            json.dump({"schema_version": "task-budget/v1",
                       "writes_total": 0, "writes_fix": 0, "writes_grow": 0,
                       "inspections": 0, "deadline_hhmm": "00:00"}, _f)
        t("★★締切を過ぎたら、これ以上は相談しない★★"
          "／★回数の上限を外したので、ここが唯一の歯止め★",
          raises(lambda: codex_round("t2", fp, budget_path=_bp2,
                                    now_hhmm="07:00"), "締切"))
        # ★★締切は「一晩」の中で見る★★（2026-08-28・本番で実害）
        #   ★夜11時半に始まるタスクが、朝7時20分の締切を
        #   「もう過ぎた」と判定して、毎晩30分なにもできなかった★
        t("★★夜11時半は、朝7時20分の締切をまだ過ぎていない★★"
          "／★文字だけで比べて、毎晩30分止まっていた★",
          past_deadline("07:20", "23:30") is False)
        t("　朝8時なら、朝7時20分の締切は過ぎている",
          past_deadline("07:20", "08:00") is True)
        t("　朝6時なら、まだ過ぎていない",
          past_deadline("07:20", "06:00") is False)
        t("　夜の締切（23:59）は、夜11時半にはまだ過ぎていない",
          past_deadline("23:59", "23:30") is False)
        t("　夜の締切を、翌朝は過ぎている扱いにする",
          past_deadline("23:59", "05:00") is True)
        t("　締切が無ければ、いつでも通す", past_deadline("", "23:30") is False)
        # ★境目そのものを試す★（2026-08-28・Codexの8回目の指摘4）
        #   ★等号を壊しても落ちない試験しか無かった★
        t("★★締切ちょうどは「過ぎている」★★（1分でも取りこぼさない）",
          past_deadline("07:20", "07:20") is True)
        t("　締切の1分前は、まだ過ぎていない",
          past_deadline("07:20", "07:19") is False)
        t("　締切の1分後は、過ぎている",
          past_deadline("07:20", "07:21") is True)
        t("　朝のうちは、ずっと過ぎている扱い（11:59）",
          past_deadline("07:20", "11:59") is True)
        t("★★昼12時になったら、次の晩として開き直す★★"
          "（一晩の区切りが 12:00 であること）",
          past_deadline("07:20", "12:00") is False)
        # ★★入口から通して確かめる★★（鉄則5e＝最終状態を直接置かない）
        #   ★本番で止まったのは `codex_round` の入口★なので、
        #   関数だけでなく入口から呼ぶ。
        _bp3 = os.path.join(tmpdir, "budget_morning.json")
        with open(_bp3, "w", encoding="utf-8") as _f:
            json.dump({"schema_version": "task-budget/v1",
                       "writes_total": 0, "writes_fix": 0, "writes_grow": 0,
                       "inspections": 0, "deadline_hhmm": "07:20"}, _f)
        _n0 = _load(fp)["tasks"].get("t3", {}).get("codex_rounds", 0)
        codex_round("t3", fp, budget_path=_bp3, now_hhmm="23:30")
        t("★★夜11時半の新台タスクは、朝7時20分の締切で断られない★★"
          "／★これが本番で毎晩起きていた（相談も書き換えも全部断られた）★",
          _load(fp)["tasks"]["t3"]["codex_rounds"] == _n0 + 1)
        t("　（対照）同じ締切でも、朝8時なら断る",
          raises(lambda: codex_round("t3", fp, budget_path=_bp3,
                                     now_hhmm="08:00"), "締切"))

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
            # ★★試験は本物のgitの状態に依存させない★★（2026-08-21）
            #   CIは作業ツリーが綺麗なので `_changed_files()` が空を返し、
            #   before_commit の「変更がありません」で落ちた。
            #   手元は編集中で汚れていたため通っており、★手元とCIで結果が違った★。
            #   道具の振る舞いを見る試験が、その時のリポジトリの状態で変わってはいけない。
            _keep_changed0 = globals()["_changed_files"]
            _keep_fd0 = globals()["_file_digest"]
            globals()["_changed_files"] = lambda: (
                ["assets/data/machine-details/kabaneri.json"], "")
            globals()["_file_digest"] = lambda rel: "TESTDIGEST"
            t("★ふつうに入ると、いままでどおり止まる★",
              raises(lambda: before_write("t2", "hokuto", fp), "触ってはいけない"))
            # ★★別の記録で試す★★（2026-08-21）
            #   同じ機種で「ふつう→直す」へ変えるのは、
            #   モードの固定を入れた時点で**断られるのが正しい**
            #   （この試験は「直す経路なら進める」ことを見たいので分ける）。
            fp = os.path.join(tmpdir, "guard_repair_lane.json")
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

            # --- ★★担当を取ったあと、経路を変えられない★★
            #   （2026-08-21・Codexの再指摘。★両方向を試す★）
            #   ★直す前は、同じ機種をもう一度 claim すれば
            #     モードを好きに変えられた（対照実験で両方向とも通った）★
            #     ＝台帳の関門を後から外せる。
            t("★★直す担当を、あとからふつうの担当に変えられない★★",
              raises(lambda: claim("t3", "kabaneri", fp3), "変えられません"))
            fp3b = os.path.join(tmpdir, "guard_repair_rev.json")
            cp.assess = lambda sl, repairing=False: {
                "stage": "IDENTITY_PENDING", "reasons": [],
                "ledger_blocking": ["#1 テストの案件"]}
            claim("t3b", "kabaneri", fp3b)
            t("★★ふつうの担当を、あとから直す担当に変えられない★★",
              raises(lambda: claim("t3b", "kabaneri", fp3b, repairing=True,
                                   issues=["1"]), "変えられません"))
            t("　同じ経路で取り直すのは通る（やり直しを塞がない）",
              claim("t3b", "kabaneri", fp3b)["target_slug"] == "kabaneri")
            # --- ★★機種を替えて戻ってきてもモードは変えられない★★
            #   （2026-08-21・Codexの再指摘。★A→B→A で通っていた★）
            #   記録がタスク単位（guard_slug）だったので、機種を替えた時点で
            #   捨てられ、戻ってきたときに「前は何だったか」が残らなかった。
            #   タスク名を変える迂回も同じ理由で通っていた。
            fp3c = os.path.join(tmpdir, "guard_aba.json")
            claim("tA", "kabaneri", fp3c, repairing=True, issues=["1"])
            claim("tA", "hokuto", fp3c, repairing=True, issues=["1"])
            t("★★A→B→A でモードを変えられない★★",
              raises(lambda: claim("tA", "kabaneri", fp3c), "変えられません"))
            t("★★タスク名を変えても変えられない★★",
              raises(lambda: claim("tZ", "kabaneri", fp3c), "変えられません"))
            t("　同じ経路なら戻ってこられる",
              claim("tA", "kabaneri", fp3c, repairing=True,
                    issues=["1"])["target_slug"] == "kabaneri")
            cp.assess = _fake_assess

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
                # ★本番と同じ形で返す★（2026-08-21）
                #   `_git` は subprocess の**バイト列**を UTF-8 で読む実装なので、
                #   偽物が str を返すと本番では起きない失敗になる。
                returncode = 0
                stdout = ("R  machines/other/index.html -> machines/target/index.html\n"
                          " M assets/data/machine-details/target.json\n"
                          " M scripts/nazono.py\n").encode("utf-8")

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
                # ★★その日ぶんの照合済みは、機種を替えても消えない★★
                #   （2026-08-21・Codexの指摘3）
                #   ★直す前は、機種を替えると verified_commit が捨てられ、
                #     3機種ぶんをためて最後にまとめて push すると
                #     1・2機種目が「照合していない」ことになって止まった★
                t("　その日の照合済み一覧に残る",
                  "abc1234" in _load(fpG)["day"].get("verified_commits", []))
                _e_sw = _entry(_load(fpG), "tE")
                _dat_sw = _load(fpG)
                _entry(_dat_sw, "tE")["guard_slug"] = "ちがう機種"
                _save(fpG, _dat_sw)
                t("★★機種を替えても、その日の照合済み一覧は消えない★★",
                  "abc1234" in _load(fpG)["day"].get("verified_commits", []))
                _dat_sw2 = _load(fpG)
                _entry(_dat_sw2, "tE")["guard_slug"] = "kabaneri"
                _save(fpG, _dat_sw2)

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

            # --- ★共通ファイルは「その機種の話に収まっていれば」許す★（台帳#429）
            _keep_shared = globals()["_shared_file_touches_others"]
            _keep_git2 = globals()["_git"]
            # ★本物の _unrelated_changes を試す★
            #   （この試験のかたまりでは差し替えてあるので、ここだけ戻す）
            globals()["_unrelated_changes"] = _keep_unrel0
            try:
                globals()["_git"] = lambda *a: (0, "M  assets/data/machines.json\n")
                globals()["_shared_file_touches_others"] = lambda rel, s: False
                t("★★チェッカーの値を直すために machines.json を触れる★★",
                  _unrelated_changes("kabaneri") == [])
                globals()["_shared_file_touches_others"] = lambda rel, s: True
                got3 = _unrelated_changes("kabaneri")
                t("★★他の機種まで変わっていたら止める★★",
                  len(got3) == 1 and "他の機種" in got3[0])
                globals()["_git"] = lambda *a: (0, "M  scripts/nazono.py\n")
                globals()["_shared_file_touches_others"] = lambda rel, s: False
                t("　共通ファイル以外は、いままでどおり止める",
                  _unrelated_changes("kabaneri") == ["scripts/nazono.py"])
            finally:
                globals()["_shared_file_touches_others"] = _keep_shared
                globals()["_git"] = _keep_git2
                globals()["_unrelated_changes"] = lambda s: []   # 元の差し替えに戻す

            # --- ★共通ファイルの中身まで見る★（依頼249の指摘3・防御1）
            _keep_git3 = globals()["_git"]
            try:
                # service-worker.js はキャッシュ名の1行だけ
                base_sw = ("const CACHE_NAME = 'uchidokoro-v1';\n"
                           "self.addEventListener('fetch', e => {});\n")
                globals()["_git"] = lambda *a: (0, base_sw)
                import builtins as _bi0
                _real_open0 = _bi0.open

                def _sw_open(p, *a, **k):
                    if str(p).endswith("service-worker.js"):
                        import io as _io0
                        return _io0.StringIO(_sw_open.payload)
                    return _real_open0(p, *a, **k)

                _bi0.open = _sw_open
                try:
                    _sw_open.payload = ("const CACHE_NAME = 'uchidokoro-v2';\n"
                                        "self.addEventListener('fetch', e => {});\n")
                    t("★★キャッシュ名の1行だけなら許す★★",
                      _shared_file_touches_others("service-worker.js", "a") is False)
                    _sw_open.payload = ("const CACHE_NAME = 'uchidokoro-v1';\n"
                                        "self.addEventListener('fetch', e => {evil();});\n")
                    t("★★中のコードを変えたら止める★★",
                      _shared_file_touches_others("service-worker.js", "a"))
                    _sw_open.payload = base_sw + "// 追加\n"
                    t("★★行を増やしたら止める★★",
                      _shared_file_touches_others("service-worker.js", "a"))
                    _sw_open.payload = ("const CACHE_NAME = 'evil';\n"
                                        "self.addEventListener('fetch', e => {});\n")
                    t("★キャッシュ名の形が違えば止める★",
                      _shared_file_touches_others("service-worker.js", "a"))
                finally:
                    _bi0.open = _real_open0

                # machines.json の並び・重複
                globals()["_git"] = lambda *a: (
                    0, json.dumps([{"slug": "a", "x": 1}, {"slug": "b", "x": 2}],
                                  ensure_ascii=False))
                _keep_open = None
                import builtins as _bi
                _real_open = _bi.open

                def _fake_open(p, *a, **k):
                    if str(p).endswith("machines.json"):
                        import io as _io2
                        return _io2.StringIO(_fake_open.payload)
                    return _real_open(p, *a, **k)

                _bi.open = _fake_open
                try:
                    _fake_open.payload = json.dumps(
                        [{"slug": "a", "x": 9}, {"slug": "b", "x": 2}],
                        ensure_ascii=False)
                    t("　自分の機種の項目だけ変わっているなら許す",
                      _shared_file_touches_others("assets/data/machines.json", "a")
                      is False)
                    t("★★他の機種が変わっていたら止める★★",
                      _shared_file_touches_others("assets/data/machines.json", "b"))
                    _fake_open.payload = json.dumps(
                        [{"slug": "b", "x": 2}, {"slug": "a", "x": 1}],
                        ensure_ascii=False)
                    t("★★並びが入れ替わっていたら止める★★",
                      _shared_file_touches_others("assets/data/machines.json", "a"))
                    _fake_open.payload = json.dumps(
                        [{"slug": "a", "x": 1}, {"slug": "a", "x": 1},
                         {"slug": "b", "x": 2}], ensure_ascii=False)
                    t("★★機種が増えていたら止める（重複でも）★★",
                      _shared_file_touches_others("assets/data/machines.json", "a"))
                finally:
                    _bi.open = _real_open
                t("★知らない共有ファイルは許さない★",
                  _shared_file_touches_others("assets/img/logo.png", "a"))
            finally:
                globals()["_git"] = _keep_git3

            # --- ★改行の違いでは食い違わせない★（台帳#430・対照実験）
            t("　改行だけ違うものは同じ指紋になる",
              hashlib.sha256(_normalize_eol(b"a\r\nb\r\n")).hexdigest()
              == hashlib.sha256(_normalize_eol(b"a\nb\n")).hexdigest())
            t("★★中身が変われば、改行をそろえても指紋は変わる★★",
              hashlib.sha256(_normalize_eol(b"a\r\nb\r\n")).hexdigest()
              != hashlib.sha256(_normalize_eol(b"a\r\nc\r\n")).hexdigest())
            t("★バイナリは触らない（NULがあればそのまま）★",
              _normalize_eol(b"\x00a\r\nb") == b"\x00a\r\nb")

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
            globals()["_changed_files"] = _keep_changed0
            globals()["_file_digest"] = _keep_fd0

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
        _machines_per_day_tests(t, _d)
        _no_work_tests(t, _d)
        _budget_tests(t, _d)
        # ★★台帳番号ではなく「見つけたもの」で担当する経路★★
        #   （2026-08-21・Codexの設計レビュー）
        _finding_tests(t, _d)
        # ★★Linux（CI）側の生き死にの見方も、手元で動かして確かめる★★
        #   （2026-08-21・これを怠ってCIが赤くなった）
        _alive_posix_tests(t)
    finally:
        _sh.rmtree(_d, ignore_errors=True)

    globals()["unattended_dirty_code"] = _keep_dirty   # ★必ず戻す★
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
        if name == "claim":
            # ★★無人か手動かを呼ぶ側が申告する★★（2026-08-30）
            #   ★ロックでは区別できない★＝手動で本番手順を安全に動かすときも
            #   ロックの取得は必須なので、ロックを見るだけでは無人に見える。
            #   ★申告が無ければロックで推測する★（移行中の古い呼び出し向け）。
            #   ★これは無人であることの証明ではない★（同じ権限からは偽装できる）
            #   ＝事故を止める綱であって、悪意への境界ではない。
            g = p.add_mutually_exclusive_group()
            g.add_argument("--scheduled", dest="sched_flag",
                           action="store_true", default=None,
                           help="無人（スケジューラ）から動かしている")
            g.add_argument("--manual", dest="sched_flag",
                           action="store_false",
                           help="人が手で動かしている（試しているとき）")
        if name in ("claim", "before-write"):
            # ★台帳の案件を直すために触る★（2026-08-21・台帳#211／Codex依頼246の指摘1）
            #   ここが無いと、関数には経路があるのに**コマンドから使えず**、
            #   無人実行では修理対象を確保できなかった。
            p.add_argument("--repairing", action="store_true",
                           help="台帳で止まっている公開済み機種を、直すために担当する")
            p.add_argument("--issue", action="append", default=[],
                           help="直す対象の案件番号（例 --issue 318）。"
                                "--repairing のときは1つ以上必須")
            # ★★台帳番号ではなく「見つけたもの」で担当する★★
            #   （2026-08-21・Codexの設計レビュー）
            #   台帳番号は人が付けた札で、しかも人しか閉じない。
            #   その場で2AIが決めて直す流れは、いまのHEADで見つけ直した
            #   内容そのもの（repair_journal の finding_id）を鍵にする。
            p.add_argument("--decision", default=None, metavar="FINDING_ID",
                           help="見つけたものの番号（repair_journal --list で出る）。"
                                "書くには AGREED まで進んでいることが必要")
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
                               issues=getattr(args, "issue", []) or [],
                               finding=getattr(args, "decision", None),
                               scheduled=getattr(args, "sched_flag", None)),
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
                                      repairing=bool(getattr(args, "repairing", False)),
                                      finding=getattr(args, "decision", None)),
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

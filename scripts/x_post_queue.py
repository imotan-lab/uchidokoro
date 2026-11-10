"""★Xの更新告知を、投稿待ちの控えに入れて、あとで確実に投稿する★（2026-11-10）

★なぜ★（運営者「更新されないページ他にもないの？」の2回目の洗い出しで見つかった）
  朝の更新タスクは `post_update_to_x.py --detach` で、投稿前に0〜180分ランダムに待つ処理を裏で走らせていた。
  ところがタスクが終わると、その裏の処理も一緒に終わってしまい、★待っている間に告知が消えていた★
  （記録では待ち始めた109件のうち13件が投稿されないまま消えた・2026-11-10の2件も同じ）。誰にも知らされなかった。

★いまの形★
  ・告知したいときは `add` で控え（Documents/uchidokoro/x_post_queue.json）に入れるだけ（待たない・裏で走らせない）。
    投稿してよい時刻（due）は今から DELAY_MIN〜DELAY_MAX 分後をランダムに決める（bot検知対策の待ちはここで作る）。
  ・`run` が時刻の来たものを1件ずつ投稿する（post_update_to_x.py を --skip-jitter で呼ぶ・投稿の間は30〜120秒あける）。
    自動タスク（見張り番 8:03・ランキング 17:00・新台 23:30）が始まるときに呼ぶ＝タスクの中で終わるので消えない。
  ・失敗したら30分あけて MAX_TRIES 回まで。それでも駄目なら控えから外し、ログとメール（post_update_to_x が送る）に残る。
  ・`--check`＝時刻を過ぎて STALE_HOURS 時間たっても投稿されていない控えがあれば終了コード1（見張り番が🟡）。

CLI:
  python scripts/x_post_queue.py add --mode promotion|correction --slug <slug> [--change-file <ファイル>]
  python scripts/x_post_queue.py run [--max N]
  python scripts/x_post_queue.py --check
  python scripts/x_post_queue.py --selftest
"""
from __future__ import annotations

import argparse
import datetime as _dt
import io
import json
import os
import random
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import local_paths as _lp            # noqa: E402

QUEUE = _lp.doc("x_post_queue.json")
LOCK = _lp.doc("x_post_queue.lock")
DELAY_MIN, DELAY_MAX = 5, 60          # 投稿してよい時刻までの分（ランダム）
RETRY_MIN = 30
MAX_TRIES = 3
STALE_HOURS = 30                      # 時刻を過ぎてこれだけ投稿されなければ見張り番が知らせる（次のタスクまで最長約15時間）
MODES = ("promotion", "correction")
_SLUG = re.compile(r"^[a-z0-9_]{1,60}$")
_IN_SELFTEST = False


def _log(msg: str) -> None:
    if _IN_SELFTEST:
        return
    try:
        os.makedirs(_lp.LOGS, exist_ok=True)
        p = os.path.join(_lp.LOGS, "x_post_queue_%s.log" % _dt.date.today().isoformat())
        with io.open(p, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (_dt.datetime.now().strftime("%H:%M:%S"), msg))
    except OSError:
        pass


def _now() -> _dt.datetime:
    return _dt.datetime.now().replace(microsecond=0)


def read(path: str = None) -> dict:
    """★無いは空・壊れていたら例外★（黙って0件と読まない）"""
    p = path or QUEUE
    if not os.path.isfile(p):
        return {"items": []}
    with io.open(p, encoding="utf-8") as f:
        d = json.load(f)
    if not isinstance(d, dict) or not isinstance(d.get("items"), list):
        raise ValueError("x_post_queue.json の形が違います")
    for it in d["items"]:
        if (not isinstance(it, dict) or it.get("mode") not in MODES
                or not _SLUG.match(str(it.get("slug") or "")) or not it.get("due")):
            raise ValueError("x_post_queue.json に壊れた控えがあります")
    return d


def _save(d: dict, path: str = None) -> None:
    p = path or QUEUE
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(d, ensure_ascii=False, indent=1) + "\n")
    os.replace(tmp, p)


def add(mode: str, slug: str, change: str = "", now=None, rng=None, path: str = None) -> dict:
    if mode not in MODES:
        raise ValueError(f"mode は {MODES} のどれかです")
    if not _SLUG.match(str(slug or "")):
        raise ValueError("slug が不正です")
    now = now or _now()
    rng = rng or random
    d = read(path)
    # ★同じ機種・同じ種類がまだ投稿待ちなら足さない★（同じ機種へ続けて告知しない）
    for it in d["items"]:
        if it["slug"] == slug and it["mode"] == mode:
            _log(f"もう投稿待ちなので足しません: {mode} {slug}")
            return it
    due = now + _dt.timedelta(minutes=rng.randint(DELAY_MIN, DELAY_MAX))
    it = {"mode": mode, "slug": slug, "change": str(change or "")[:200],
          "added": now.isoformat(), "due": due.isoformat(), "tries": 0}
    d["items"].append(it)
    _save(d, path)
    _log(f"投稿待ちに入れました: {mode} {slug}（{due:%m/%d %H:%M} 以降）")
    return it


def _post(it: dict) -> int:
    args = [sys.executable, os.path.join(HERE, "post_update_to_x.py"), it["mode"], "--slug", it["slug"],
            "--skip-jitter"]
    if it["mode"] == "correction" and it.get("change"):
        args += ["--change", it["change"]]
    try:
        r = subprocess.run(args, cwd=os.path.dirname(HERE), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=900)
        return r.returncode
    except Exception as e:                                       # noqa: BLE001
        _log(f"投稿を呼べませんでした: {it['slug']}: {type(e).__name__}: {e}")
        return 99


class _Lock:
    """★2つの run を同時に走らせない★（古い印は60分で奪う）"""

    def __enter__(self):
        os.makedirs(os.path.dirname(LOCK), exist_ok=True)
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.time() - os.path.getmtime(LOCK) < 3600:
                raise RuntimeError("ほかの run が投稿中です")
            os.remove(LOCK)
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return self

    def __exit__(self, *exc):
        try:
            os.remove(LOCK)
        except OSError:
            pass
        return False


def run_due(now=None, post=None, sleep=None, rng=None, path: str = None, max_n: int = 10) -> dict:
    """★時刻の来た控えを1件ずつ投稿する★（成功＝控えから外す／失敗＝30分あけて再挑戦・MAX_TRIES で諦める）"""
    now = now or _now()
    post = post or _post
    sleep = sleep if sleep is not None else time.sleep
    rng = rng or random
    d = read(path)
    due = [it for it in d["items"] if it["due"] <= now.isoformat()]
    due.sort(key=lambda it: it["due"])
    done, failed, gave_up = [], [], []
    for i, it in enumerate(due[:max(0, max_n)]):
        if i:
            sleep(rng.randint(30, 120))       # ★投稿の間をあける★（bot検知対策）
        rc = post(it)
        if rc == 0:
            d["items"].remove(it)
            done.append(it["slug"])
            _log(f"投稿しました: {it['mode']} {it['slug']}")
            continue
        it["tries"] = int(it.get("tries", 0)) + 1
        if it["tries"] >= MAX_TRIES:
            d["items"].remove(it)
            gave_up.append(it["slug"])
            _log(f"★{MAX_TRIES}回失敗したので投稿をあきらめました★: {it['mode']} {it['slug']}（終了コード {rc}）")
        else:
            it["due"] = (now + _dt.timedelta(minutes=RETRY_MIN)).isoformat()
            failed.append(it["slug"])
            _log(f"投稿に失敗しました（{it['tries']}回目・{RETRY_MIN}分あけて再挑戦）: {it['slug']}（終了コード {rc}）")
        _save(d, path)
    if done:
        _save(d, path)
    return {"done": done, "failed": failed, "gave_up": gave_up, "left": len(d["items"])}


def stale_problem(d: dict, now=None):
    now = now or _now()
    old = [it for it in d["items"]
           if (now - _dt.datetime.fromisoformat(it["due"])).total_seconds() >= STALE_HOURS * 3600]
    if old:
        return f"投稿の時刻を{STALE_HOURS}時間以上過ぎた告知が{len(old)}件あります（" + "・".join(
            it["slug"] for it in old[:5]) + "）"
    return None


def selftest() -> int:
    global _IN_SELFTEST
    _IN_SELFTEST = True
    import tempfile
    ok = [0, 0]

    def t(name, cond):
        ok[0 if cond else 1] += 1
        print(("✅ " if cond else "❌ ") + name)

    p = os.path.join(tempfile.mkdtemp(prefix="xq_"), "q.json")
    now = _dt.datetime(2026, 11, 10, 9, 0, 0)

    class _R:
        def randint(self, a, b):
            return a
    it = add("correction", "zzz_a", "直した", now=now, rng=_R(), path=p)
    t("★★告知は控えに入れるだけ（待たない・裏で走らせない）★★",
      read(p)["items"] == [it] and it["due"] == (now + _dt.timedelta(minutes=DELAY_MIN)).isoformat())
    add("correction", "zzz_a", "もう一度", now=now, rng=_R(), path=p)
    t("★同じ機種・同じ種類が投稿待ちなら足さない★（続けて告知しない）", len(read(p)["items"]) == 1)
    calls = []
    r0 = run_due(now=now, post=lambda x: calls.append(x) or 0, sleep=lambda s: None, rng=_R(), path=p)
    t("　時刻の来ていない告知は投稿しない", r0["done"] == [] and not calls)
    later = now + _dt.timedelta(minutes=10)
    r1 = run_due(now=later, post=lambda x: calls.append(x["slug"]) or 0, sleep=lambda s: None, rng=_R(), path=p)
    t("★★時刻の来た告知を投稿し、成功したら控えから外す★★",
      r1["done"] == ["zzz_a"] and calls == ["zzz_a"] and read(p)["items"] == [])
    add("promotion", "zzz_b", now=now, rng=_R(), path=p)
    run_due(now=later, post=lambda x: 1, sleep=lambda s: None, rng=_R(), path=p)
    t("　失敗したら次の挑戦は30分あける（すぐ再挑戦しない）",
      read(p)["items"][0]["due"] == (later + _dt.timedelta(minutes=RETRY_MIN)).isoformat())
    for k in range(1, MAX_TRIES):
        run_due(now=later + _dt.timedelta(hours=k), post=lambda x: 1, sleep=lambda s: None, rng=_R(), path=p)
    t(f"★失敗したら30分あけて再挑戦し、{MAX_TRIES}回で控えから外す（ずっと居座らない）★", read(p)["items"] == [])
    add("promotion", "zzz_c", now=now, rng=_R(), path=p)
    t("★★時刻を30時間以上過ぎて投稿されない告知があれば、見張り番が知らせる★★",
      stale_problem(read(p), now + _dt.timedelta(hours=31)) is not None
      and stale_problem(read(p), now + _dt.timedelta(hours=10)) is None)
    slept = []
    add("promotion", "zzz_d", now=now, rng=_R(), path=p)
    run_due(now=later, post=lambda x: 0, sleep=lambda s: slept.append(s), rng=_R(), path=p)
    t("　2件続けて投稿するときは間をあける", slept == [30])
    with io.open(p, "w", encoding="utf-8") as f:
        f.write('{"items": [{"mode": "x"}]}')
    try:
        read(p)
        t("★控えが壊れていたら例外★（黙って0件と読まない）", False)
    except ValueError:
        t("★控えが壊れていたら例外★（黙って0件と読まない）", True)
    print(f"\n{ok[0]}/{sum(ok)} 合格")
    return 0 if ok[1] == 0 else 1


def main() -> int:
    for _st in (sys.stdout, sys.stderr):
        try:
            _st.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                        # noqa: BLE001
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", choices=("add", "run"))
    ap.add_argument("--mode", choices=MODES)
    ap.add_argument("--slug")
    ap.add_argument("--change-file")
    ap.add_argument("--max", type=int, default=10)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    try:
        if a.check:
            pr = stale_problem(read())
            print(("🟡 " + pr) if pr else f"🟢 Xの投稿待ち {len(read()['items'])}件")
            return 1 if pr else 0
        if a.cmd == "add":
            change = ""
            if a.change_file:
                with io.open(a.change_file, encoding="utf-8") as f:
                    change = f.read().strip()
            it = add(a.mode, a.slug, change)
            print(f"投稿待ちに入れました: {it['mode']} {it['slug']}（{it['due']} 以降・次の自動タスクで投稿）")
            return 0
        if a.cmd == "run":
            with _Lock():
                r = run_due(max_n=a.max)
            print(f"投稿 {len(r['done'])}件／失敗 {len(r['failed'])}件／あきらめ {len(r['gave_up'])}件／残り {r['left']}件")
            return 0
    except Exception as e:                                       # noqa: BLE001
        print(f"🟠 Xの投稿待ちを扱えません（{type(e).__name__}: {e}）")
        _log(f"扱えません: {type(e).__name__}: {e}")
        return 2
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())

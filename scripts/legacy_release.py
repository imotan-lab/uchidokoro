"""★昔からの機種の導入日を、2AIが結んだDMMの機種ページから控える★（2026-11-10）

★なぜ★（運営者「更新されないページ他にもないの？」で見つかった）
  サイトの並び順は「21位以降は導入日の新しい順」（reorder_machines）だが、昔からの機種120のうち
  99機種は machines.json に導入日が無く、記事の本文から取り出さない決まりのため、
  ★その99機種は古い並びのまま固定されていた★（埋める道がどこにも無かった）。

★どこまでを機械がやるか★
  ・どの機種がDMMのどのページかは★2AIが決めた結び付け★（legacy_binding）だけを使う（推測しない）。
  ・導入日はDMMの機種ページが決まった形で出している値を dmm_machine.parse で読むだけ（記事に出る導入日はDMMの値＝決まり）。
  ・★machines.json は書かない★＝控え assets/data/legacy-release.json に置き、並べ替え（reorder_machines.release_of）が
    machines.json に導入日が無いときだけ読む。記事の本文や判定は変えない。
  ・「未定」・読めない・転送されたページは書かない（前の記録は残す）。

置き場＝assets/data/legacy-release.json（リポジトリの中＝CIでも並べ替えが読める）
CLI:
  --refresh N [--apply]  … 結び付け済みで導入日の無い昔からの機種を、最後に試した日が古い順に N 件取り直す
                           （試した日 tries と確かめた日 checked を分ける）。終了コード 10＝控えが変わった／0＝変わらない／2＝読めない
  --check                … 番人用。結び付け済みで導入日の無い機種があるのに、STALL_DAYS 日だれも試していない → 1／読めない → 2
  --list / --selftest
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE = os.path.join(BASE, "assets", "data", "legacy-release.json")
MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
SCHEMA = "legacy-release/v1"
STALL_DAYS = 3
# ★★確かめてから30日は取り直さない★★（2026-11-10・洗い出し2回目）＝直す前は確かめ済みの機種まで毎日20件取り直し、
#   控え（リポジトリの中）の日付だけが変わって毎日コミットしていた。昔の機種の導入日はまず変わらない
RECHECK_DAYS = 30
_ID_RE = re.compile(r"^https://p-town\.dmm\.com/machines/([0-9]+)$")
_REL_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class LegacyReleaseError(RuntimeError):
    pass


def read(path: str = None) -> dict:
    """★無いは空・壊れていたら例外★（黙って0件と読まない）"""
    p = path or STORE
    if not os.path.isfile(p):
        return {"schema": SCHEMA, "machines": {}, "tries": {}}
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
    if not isinstance(d, dict) or d.get("schema") != SCHEMA:
        raise LegacyReleaseError("legacy-release.json の形が違います")
    if not isinstance(d.get("machines"), dict) or not isinstance(d.get("tries", {}), dict):
        raise LegacyReleaseError("legacy-release.json の中身が壊れています")
    for s, e in d["machines"].items():
        if (not isinstance(e, dict) or not _REL_RE.match(str(e.get("release") or ""))
                or not _ID_RE.match(str(e.get("url") or ""))
                or not _DATE_RE.match(str(e.get("checked") or ""))):
            raise LegacyReleaseError(f"legacy-release.json の {s} が壊れています")
    d.setdefault("tries", {})
    return d


def release_for(slug: str, path: str = None) -> str:
    """★控えの導入日★（無ければ空・読めなければ例外）"""
    return str(((read(path).get("machines") or {}).get(slug) or {}).get("release") or "")


def _save(d: dict, path: str = None) -> None:
    p = path or STORE
    tmp = p + ".tmp"
    d = {"schema": SCHEMA, "machines": dict(sorted(d["machines"].items())),
         "tries": dict(sorted(d.get("tries", {}).items()))}
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(d, ensure_ascii=False, indent=1) + "\n")
    os.replace(tmp, p)


def targets(rows: list) -> list:
    """★昔からの機種で、machines.json に導入日が無いもの★（機種一覧の並び）"""
    out = []
    for m in rows:
        if not isinstance(m, dict) or "publication_policy" in m:
            continue
        if str(m.get("release_date") or "").strip():
            continue
        out.append(str(m.get("slug") or ""))
    return [s for s in out if s]


def _bound(slug: str):
    import hall_presence as _hp
    return _hp.bound_url(slug)


def _fetch_release(url: str) -> str:
    import dmm_machine as _dm
    mid = _ID_RE.match(url).group(1)
    got = _dm.fetch(mid)
    return str(got.get("release_date") or "")


def refresh(n: int, apply: bool, rows=None, fetch=None, bound=None, today: str = None,
            path: str = None, log=print) -> dict:
    import safe_json as _sj
    d = read(path)
    rows = _sj.read_rows(MACHINES) if rows is None else rows
    fetch = fetch or _fetch_release
    bound = bound or _bound
    today = today or datetime.date.today().isoformat()
    # ★★結び付けが消えた・変わった機種の導入日は捨てる★★（2026-11-10・Codexの指摘＝実害）＝
    #   誤った結び付けを2AIが取り消しても、その（別の機種の）導入日が残り、並べ替えが使い続けた。
    #   ★結び付けが読めない機種も捨てる★（確かめられない値を並べ替えに使わない）。
    purged = []
    for s in list(d["machines"]):
        try:
            u = bound(s)
        except Exception:                                        # noqa: BLE001
            u = None
        if not u or u != d["machines"][s].get("url"):
            d["machines"].pop(s)
            purged.append(s)
            log(f"結び付けが無い・変わったので控えから外した {s}")
    cands = []
    for s in targets(rows):
        try:
            u = bound(s)
        except Exception as e:                                   # noqa: BLE001
            log(f"結び付けが使えません {s}: {str(e)[:100]}")
            continue
        if u and _ID_RE.match(u):
            rec = d["machines"].get(s) or {}
            if rec.get("url") == u and _DATE_RE.match(str(rec.get("checked") or "")) and (
                    datetime.date.fromisoformat(today)
                    - datetime.date.fromisoformat(rec["checked"])).days < RECHECK_DAYS:
                continue
            cands.append((s, u))
    cands.sort(key=lambda su: str(d["tries"].get(su[0], "")))
    done, failed = [], []
    for s, u in cands[:max(0, n)]:
        d["tries"][s] = today
        try:
            rel = fetch(u)
        except Exception as e:                                   # noqa: BLE001
            failed.append((s, str(e)[:120]))
            log(f"読めなかった {s}: {str(e)[:120]}")
            continue
        if not _REL_RE.match(rel):
            failed.append((s, f"導入日の形が違います: {rel!r}"))
            continue
        old = (d["machines"].get(s) or {}).get("release")
        d["machines"][s] = {"release": rel, "url": u, "checked": today}
        done.append((s, old, rel))
        log(f"確かめた {s}: {old} → {rel}")
    changed = any(o != r for _s, o, r in done) or bool(purged)
    if apply and (done or failed or purged):
        _save(d, path)
    return {"done": done, "failed": failed, "candidates": len(cands), "changed": changed,
            "purged": purged}


def unbound(rows, bound=None, ranked=None) -> list:
    """★導入日が無く、まだDMMと結び付けていない昔の機種★（2026-11-10・洗い出し2回目）

    ★なぜ★＝結び付けの一覧（hall_presence --unbound）はランキングに入りうる機種しか出さないので、
    ジャグラー系・ハナハナ系など27機種は結ばれず、導入日が永久に入らなかった。
    ★ランキングの候補（ranked）は後ろへ★（そちらは hall_presence --unbound が先に出す）。
    """
    bound = bound or _bound
    out = []
    for s in targets(rows):
        try:
            u = bound(s)
        except Exception:                                        # noqa: BLE001
            u = "（読めない）"
        if not u:
            out.append(s)
    ranked = set(ranked or [])
    return [s for s in out if s not in ranked] + [s for s in out if s in ranked]


def stall_problem(rows, d: dict, today: str, bound=None):
    bound = bound or _bound
    need = []
    for s in targets(rows):
        if s in d["machines"]:
            continue
        try:
            u = bound(s)
        except Exception:                                        # noqa: BLE001
            continue
        if u and _ID_RE.match(u):
            need.append(s)
    if not need:
        return None
    last = max([str(v) for v in d.get("tries", {}).values()] or [""])
    if not last:
        return f"導入日の無い昔からの機種が{len(need)}件あるのに、一度も取りに行っていません"
    days = (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(last)).days
    if days >= STALL_DAYS:
        return f"導入日の無い昔からの機種が{len(need)}件あるのに、{days}日だれも取りに行っていません"
    return None


def selftest() -> int:
    import tempfile
    res = []

    def t(name, cond):
        res.append(bool(cond))
        print(("✅ " if cond else "❌ ") + name)

    p = os.path.join(tempfile.mkdtemp(prefix="lrel_"), "lr.json")
    rows = [{"slug": "a"}, {"slug": "b", "release_date": "2025-01-01"},
            {"slug": "c", "publication_policy": "page-decision/v2"}, {"slug": "d"}, {"slug": "e"}]
    U = "https://p-town.dmm.com/machines/%d"
    bound = {"a": U % 1, "d": U % 4, "e": None}.get
    t("★対象は昔からの機種で導入日の無いものだけ★（新台経路・導入日がある機種は外す）",
      targets(rows) == ["a", "d", "e"])
    r = refresh(5, True, rows=rows, fetch=lambda u: {U % 1: "2025-06-02", U % 4: "未定"}[u],
                bound=bound, today="2026-11-10", path=p, log=lambda *a: None)
    with open(p, encoding="utf-8") as f:
        d = json.load(f)                   # ★生のまま読む★（形の検査で落ちると、狙った守りを試せない）
    t("★形の違う値（未定など）は書かない★", "d" not in d["machines"] and r["failed"])
    t("★★結び付けたDMMの機種ページの導入日を控える★★",
      d["machines"].get("a", {}).get("release") == "2025-06-02" and release_for("a", p) == "2025-06-02")
    t("★結び付けの無い機種は取りに行かない★（推測しない）", "e" not in d["tries"])
    t("　試した日は全部残る", d["tries"] == {"a": "2026-11-10", "d": "2026-11-10"})
    r2 = refresh(5, True, rows=rows, fetch=lambda u: {U % 1: "2025-06-02", U % 4: "未定"}[u],
                 bound=bound, today="2026-11-11", path=p, log=lambda *a: None)
    t("★★確かめて30日たっていない機種は取り直さない★★（直す前は毎日20件取り直し、毎日コミットしていた）",
      not r2["done"] and [x[0] for x in r2["failed"]] == ["d"])
    p2 = os.path.join(os.path.dirname(p), "lr_re.json")
    _save({"machines": {"a": {"release": "2025-06-02", "url": U % 1, "checked": "2026-10-11"}},
           "tries": {"a": "2026-10-11"}}, p2)
    _r29 = refresh(5, True, rows=rows, fetch=lambda u: "2025-06-02", bound={"a": U % 1}.get,
                   today="2026-11-09", path=p2, log=lambda *a: None)
    _r30 = refresh(5, True, rows=rows, fetch=lambda u: "2025-06-02", bound={"a": U % 1}.get,
                   today="2026-11-10", path=p2, log=lambda *a: None)
    t("　30日たったら取り直す（29日なら取り直さない）",
      _r29["candidates"] == 0 and [x[0] for x in _r30["done"]] == ["a"])
    _save({"machines": {"a": {"release": "2025-06-02", "url": U % 1, "checked": "2026-09-01"}},
           "tries": {}}, p2)
    t("★読めなければ書かず、前の記録は残す★",
      refresh(1, True, rows=rows, fetch=lambda u: (_ for _ in ()).throw(ValueError("x")),
              bound={"a": U % 1}.get, today="2026-11-12", path=p2, log=lambda *a: None)["failed"]
      and release_for("a", p2) == "2025-06-02")
    _r3 = refresh(0, True, rows=rows, fetch=lambda u: "2025-06-02", bound={"d": U % 4}.get,
                  today="2026-11-12", path=p, log=lambda *a: None)
    t("★★結び付けを取り消した機種の導入日は、控えから捨てる★★（Codexの指摘＝別の機種の日付が残った）",
      _r3["purged"] == ["a"] and release_for("a", p) == "")
    refresh(1, True, rows=rows, fetch=lambda u: "2025-06-02", bound=bound, today="2026-11-12",
            path=p, log=lambda *a: None)
    _r4 = refresh(0, True, rows=rows, fetch=lambda u: "2025-06-02", bound={"a": U % 7, "d": U % 4}.get,
                  today="2026-11-12", path=p, log=lambda *a: None)
    t("　結び付けのURLが変わった機種の導入日も捨てる", "a" in _r4["purged"] and release_for("a", p) == "")
    refresh(1, True, rows=rows, fetch=lambda u: "2025-06-02", bound=bound, today="2026-11-13",
            path=p, log=lambda *a: None)
    t("★番人：導入日の無い結び付け済み機種があるのに3日だれも試していなければ知らせる★",
      stall_problem(rows, read(p), "2026-11-16", bound) is not None
      and stall_problem(rows, read(p), "2026-11-13", bound) is None)
    t("★★結び付けていない昔の機種を挙げる（ランキングの候補外が先）★★",
      unbound(rows, bound, ranked=["e"]) == ["e"]
      and unbound([{"slug": "x"}, {"slug": "y"}], {}.get, ranked=["x"]) == ["y", "x"])
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"schema": "legacy-release/v1", "machines": {"a": {"release": "いつか"}}}')
    try:
        read(p)
        t("★控えが壊れていたら例外★（黙って0件と読まない）", False)
    except LegacyReleaseError:
        t("★控えが壊れていたら例外★（黙って0件と読まない）", True)
    # ★並べ替えが控えを読む配線★（罠③）
    import inspect
    import reorder_machines as _rm
    # ★並べ替えが読み込む「legacy_release」の置き場を向け直す★（このファイルを直接動かすと別の入れ物になるため）
    import legacy_release as _me
    _keep = _me.STORE
    try:
        _me.STORE = os.path.join(os.path.dirname(p), "lr2.json")
        _save({"machines": {"zzz_old": {"release": "2024-03-04", "url": U % 9, "checked": "2026-11-10"}},
               "tries": {}}, _me.STORE)
        t("★★並べ替えは、machines.json に導入日が無いとき控えの導入日を読む★★",
          _rm.release_of({"slug": "zzz_old"}) == "2024-03-04"
          and _rm.release_of({"slug": "zzz_old", "release_date": "2023-01-01"}) == "2023-01-01"
          and _rm.release_of({"slug": "zzz_old", "publication_policy": "page-decision/v2"}) == "")
    finally:
        _me.STORE = _keep
    t("　（配線）並べ替えの関数が控えを読んでいる", "legacy_release" in inspect.getsource(_rm.release_of))
    print(f"\n{sum(res)}/{len(res)} 合格")
    return 0 if all(res) else 1


def main() -> int:
    for _st in (sys.stdout, sys.stderr):
        try:
            _st.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                        # noqa: BLE001
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", type=int)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--unbound", action="store_true",
                    help="導入日が無く、まだDMMと結び付けていない昔の機種（ランキングの候補外が先）")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    try:
        import safe_json as _sj
        rows = _sj.read_rows(MACHINES)
        d = read()
    except Exception as e:                                       # noqa: BLE001
        print(f"🟠 昔からの機種の導入日の控えを読めません（{type(e).__name__}: {e}）")
        return 2
    if a.refresh is not None:
        r = refresh(a.refresh, a.apply)
        print(f"取り直し: {len(r['done'])}件／読めなかった {len(r['failed'])}件／対象 {r['candidates']}件"
              + ("（下見・書いていません）" if not a.apply else ""))
        return 10 if (a.apply and r["changed"]) else 0
    if a.check:
        pr = stall_problem(rows, d, datetime.date.today().isoformat())
        print(("🟡 " + pr) if pr else f"🟢 昔からの機種の導入日：控え {len(d['machines'])}件")
        return 1 if pr else 0
    if a.unbound:
        import hall_presence as _hp
        try:
            ranked = _hp.candidates()
        except Exception:                                        # noqa: BLE001
            ranked = []
        names = {m.get("slug"): m.get("name") for m in rows if isinstance(m, dict)}
        got = unbound(rows, ranked=ranked)
        for s in got[:max(0, a.limit)]:
            print(f"{s}  {names.get(s)}")
        print(f"まだ結び付けていない導入日の無い昔の機種 {len(got)}件")
        return 0
    if a.list:
        for s, e in sorted(d["machines"].items()):
            print(f"{s}: {e['release']}（{e['checked']}）")
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())

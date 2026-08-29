# -*- coding: utf-8 -*-
"""machines.json の並び順を決める（＝トップページの「人気機種」の順）。

★★運営者の指示★★（2026-08-29）
> 「今のHPの人気ランキングってのも20位までの表示でいいから
>   DMMから引っ張ったものの順に変えたい　それ以降は導入日順でいいよ」

★なぜ要るか★＝トップページは machines.json の**並び順そのまま**を
「人気機種 TOP10（稼働率をもとに毎週自動更新）」として出している。
ところがこの並びは**手で並べ替えるしかなく、最後に並べ替えたのは
2026-05-04＝約4か月前**だった。＝★書いてあることが本当ではなかった★。

★決まり★
  1〜20位   … DMMの人気順（`popular_machines` の控え）
  つぎ      … 導入済みを、導入日の新しい順
  つぎ      … ★導入日が分からない機種は、いまの並びのまま★
  いちばん後ろ… ★未導入を、導入が近い順★（2026-08-29・運営者の指示）
              ＝トップページには「近日導入」の欄が別にあるので、
                まだ打てない機種が一覧の上を占めても役に立たない。
              （2026-08-29の実測＝133機種のうち98機種は
                機械が読める導入日を持っていない。
                ★記事の本文から取り出さない★＝
                「**導入日**：…」と ["導入日", "…"] の2つの形があり、
                場合分けを足すのは意味の判断＝2AIの仕事）

★人気順が無い日は並べ替えない★（fail-closed）＝
古い順番のほうが、中途半端に混ざった順番より読者に親切。
"""
from __future__ import annotations
import argparse
import io
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
_S = os.path.join(BASE, "scripts")
if _S not in sys.path:
    sys.path.insert(0, _S)

import popular_machines as _pm                          # noqa: E402
import safe_json as _sj                                 # noqa: E402

MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
TOP_N = _pm.TOP_N


class ReorderError(Exception):
    """並べ替えられないときの合図。★黙って中途半端に並べない★"""


def release_of(m: dict) -> str:
    """★機械が読める導入日だけを見る★（記事の本文は読まない）

    ★2か所ある★＝旧方式は `release_date`、新台経路は
    `identity.market_release_date`。どちらも「YYYY-MM-DD」か「YYYY-MM」。
    """
    if not isinstance(m, dict):
        return ""
    got = m.get("release_date") or ""
    if not got:
        got = (m.get("identity") or {}).get("market_release_date") or ""
    return str(got or "").strip()


def _month_end(d: str) -> str:
    """月までしか分からない導入日を、★その月の本当の最終日★にする。

    ★2026-08-30・Codexの3周目の指摘6★＝
    ★直す前は "2026-09-99" と置いていた★ので、
    9月30日になっても「未導入」のままで、10月1日に初めて導入済みになった。
    """
    if len(d) != 7:
        return d
    import calendar as _cal
    try:
        y, mo = int(d[:4]), int(d[5:7])
        return f"{d}-{_cal.monthrange(y, mo)[1]:02d}"
    except (ValueError, _cal.IllegalMonthError):
        return d + "-28"                # ★読めなければ安全側（早い日）★


def _sort_key(pair, today: str = ""):
    """並べ替えの物差し。★月までしか分からないものは月末として扱う★

    ★★段は3つ★★（2026-08-29・運営者の指示「未導入は後ろだね」）
      0 … 導入済み（導入日の新しい順）
      1 … 導入日が分からない機種（★いまの並びのまま★）
      2 … 未導入（★いちばん後ろ★・導入が近い順）
    ★未導入を後ろにする理由★＝トップページには「近日導入」の欄が別にあり、
    そこに出ている機種が一覧の上のほうを占めても読者の役に立たない。
    """
    i, m = pair
    d = _month_end(release_of(m))
    if not d:
        return (1, "", i)
    if today and d > str(today):
        return (2, d, i)                # ★未導入は導入が近い順★
    return (0, _rev(d), i)              # ★導入済みは新しい順★


def _rev(d: str) -> str:
    """新しい順にするための並べ替え用の文字列（大きいほど前へ）"""
    return "".join(chr(0x10FFFD - ord(c)) if c.isdigit() else c for c in d)


def plan(machines: list, ranked: list, today: str = "") -> list:
    """並べ替えたあとの slug の並びを返す。★書き込みはしない★"""
    if not ranked:
        raise ReorderError(
            "人気順の控えがありません（先に popular_machines を流してください）"
            "／★並べ替えません★")
    by_slug = {m.get("slug"): m for m in machines if isinstance(m, dict)}
    if len(by_slug) != len(machines):
        raise ReorderError("machines.json に slug の重なりか欠けがあります")
    head = [s for s in ranked[:TOP_N] if s in by_slug]
    seen = set(head)
    rest = [(i, m) for i, m in enumerate(machines)
            if m.get("slug") not in seen]
    rest.sort(key=lambda p: _sort_key(p, today))
    return head + [m.get("slug") for _i, m in rest]


def apply_order(machines: list, order: list) -> list:
    """並びだけを入れ替える。★中身は1文字も変えない★"""
    by_slug = {m.get("slug"): m for m in machines}
    if sorted(order) != sorted(by_slug):
        raise ReorderError("並べ替えの前後で機種の顔ぶれが変わっています")
    return [by_slug[s] for s in order]


def _today() -> str:
    import datetime as _dt
    return _dt.date.today().isoformat()


def _fingerprint() -> str:
    """machines.json の中身の指紋（読んでから書くまでの変化を見る）"""
    import hashlib
    with open(MACHINES, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def run(apply_it: bool = False, machines=None, ranked=None,
        today: str = "", allow_stale: bool = False) -> dict:
    rows = machines
    fp = ""
    if rows is None:
        fp = _fingerprint()              # ★読んだときの指紋★
        rows = _sj.read_json(MACHINES, expect=list)
    got = ranked
    if got is None:
        # ★★今週まだ取り直せていない人気順は使わない★★
        #   （2026-08-30・Codexの3周目の指摘3）
        #   ★直す前は、決められない機種が残って取り直せていない週でも、
        #     前の週の一覧を「いまの人気順」として黙って並びに使っていた★。
        #   ★並べ替えないほうが安全★＝古い並びのまま置いておける。
        _stale = _pm.plan_today(today or _today(), 0, rows).get("stale")
        if _stale and not allow_stale:
            raise ReorderError(
                f"人気順は {_stale} に取ったもので、今週まだ取り直せていません"
                "（決められない機種を2AIで片づけてください）"
                "／★並べ替えません★")
        got = _pm.popular_slugs(rows)
    if not today:
        # ★試験は必ず日付を渡す★（昼と夜で答えが変わる検査を作らない＝鉄則5e）
        today = _today()
    order = plan(rows, got, today)
    before = [m.get("slug") for m in rows]
    moved = sum(1 for a, b in zip(before, order) if a != b)
    # ★★人気枠の件数はそのまま出す★★（2026-08-30・Codexの3周目の指摘5）
    #   ★直す前は order[:20] を「人気順の20件」と呼んでいた★ので、
    #   打ち切りで19件しか無い週は、20件目（導入日順の普通の機種）まで
    #   「DMMの人気順」として表示していた。
    _pop = [s for s in got[:TOP_N] if s in {m.get("slug") for m in rows}]
    out = {"order": order, "before": before, "moved": moved,
           "top": _pop,
           "dated": sum(1 for m in rows if release_of(m)),
           "total": len(rows)}
    if apply_it and moved:
        # ★★書く直前に、読んだときと同じ中身かを確かめる★★
        #   （2026-08-30・Codexの3周目の指摘1）
        #   ★直す前は全体を読んで全体を置き換えていた★ので、
        #   その間に新台の公開が1件足すと、★その新台を消していた★。
        #   顔ぶれの検査は**古い写しの中だけ**なので気づけない。
        if machines is None and _fingerprint() != fp:
            raise ReorderError(
                "並べ替えている間に machines.json が変わりました"
                "（新台の公開などと重なった可能性があります）"
                "／★書きません。やり直してください★")
        rows = apply_order(rows, order)
        tmp = f"{MACHINES}.tmp{os.getpid()}"
        io.open(tmp, "w", encoding="utf-8", newline="\n").write(
            json.dumps(rows, ensure_ascii=False, indent=1) + "\n")
        os.replace(tmp, MACHINES)       # ★途中で止まっても空にしない★
    return out


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("OK   " if cond else "NG   ") + name)

    def M(slug, d=None, ident=None):
        m = {"slug": slug, "name": slug}
        if d:
            m["release_date"] = d
        if ident:
            m["identity"] = {"market_release_date": ident}
        return m

    _ms = [M("a", "2026-01-05"), M("b"), M("c", "2026-07-01"),
           M("d"), M("e", ident="2026-03")]
    _T = "2026-08-29"                   # ★試験は必ず日付を渡す★（鉄則5e）
    t("★★人気順が先頭に来る★★",
      plan(_ms, ["d", "b"], _T)[:2] == ["d", "b"])
    t("　★そのあとは導入日の新しい順★",
      plan(_ms, ["d", "b"], _T)[2:4] == ["c", "e"])
    t("　★導入日が分からない機種は、そのあと・いまの並びのまま★",
      plan(_ms, ["c"], _T)[-2:] == ["b", "d"])
    t("　★月までのものは、その月の終わりとして扱う★",
      plan(_ms, ["a"], _T)[1:3] == ["c", "e"])

    # ★★未導入は最後尾★★（2026-08-29・運営者の指示）
    _up = _ms + [M("z1", "2026-11-02"), M("z2", "2026-09-07")]
    _got = plan(_up, ["c"], _T)
    t("★★未導入の機種は、いちばん後ろ★★",
      _got[-2:] == ["z2", "z1"])
    t("　★未導入どうしは、導入が近い順★",
      _got.index("z2") < _got.index("z1"))
    t("　★導入日が分からない機種より後ろ★",
      _got.index("b") < _got.index("z2"))
    t("　★日付を渡さなければ全部「導入済み」として並べる★"
      "（試験が時刻で揺れないように）",
      plan(_up, ["c"])[1] == "z1")

    t("★★人気順が無ければ並べ替えない★★",
      _raises(lambda: plan(_ms, [], _T)))
    t("　★控えに無い機種は先頭に入れない★",
      "zzz" not in plan(_ms, ["zzz", "c"], _T))
    t("　★顔ぶれは変えない★",
      sorted(plan(_ms, ["d"], _T)) == sorted(m["slug"] for m in _ms))
    t("　★重なりがあれば断る★",
      _raises(lambda: plan(_ms + [M("a")], ["a"], _T)))
    t("★★中身は1文字も変えない★★",
      apply_order(_ms, plan(_ms, ["c"], _T))[0] is _ms[2])
    t("　★顔ぶれが変わる並びは断る★",
      _raises(lambda: apply_order(_ms, ["a", "b"])))

    r = run(False, machines=_ms, ranked=["d", "b"], today=_T)
    t("★★下見では書かない★★", r["moved"] > 0 and r["order"][0] == "d")
    t("　★導入日が分かる機種の数を数える★", r["dated"] == 3)

    # ★★指摘6：月までの導入日は、その月の本当の最終日★★
    t("★★月までの導入日は、その月の最終日として扱う★★",
      _month_end("2026-09") == "2026-09-30"
      and _month_end("2026-02") == "2026-02-28"
      and _month_end("2028-02") == "2028-02-29")
    t("　★9月30日には、もう未導入ではない★",
      plan([M("q", "2026-09"), M("r", "2026-01-05")], ["r"],
           "2026-09-30")[1] == "q")
    t("　★9月29日には、まだ未導入★",
      plan([M("q", "2026-09"), M("r", "2026-01-05")], ["r"],
           "2026-09-29")[-1] == "q")
    t("　★日まで分かっているものは触らない★",
      _month_end("2026-09-07") == "2026-09-07")

    # ★★指摘5：人気枠の件数はそのまま出す★★
    r5 = run(False, machines=_ms, ranked=["d"], today=_T)
    t("★★人気枠が1件なら、1件と言う★★"
      "（打ち切りで19件の週に、20件目まで人気順と呼ばない）",
      r5["top"] == ["d"] and len(r5["order"]) == len(_ms))
    t("　★控えに無い機種は人気枠に数えない★",
      run(False, machines=_ms, ranked=["zzz", "d"], today=_T)["top"]
      == ["d"])

    # ★★指摘1：読んでから書くまでに変わったら書かない★★
    import json as _js2
    import tempfile as _tf2
    import shutil as _sh2
    global MACHINES
    _keep, _dir = MACHINES, _tf2.mkdtemp(prefix="reorder_test_")
    try:
        MACHINES = _dir + "/machines.json"
        io.open(MACHINES, "w", encoding="utf-8", newline="\n").write(
            _js2.dumps(_ms, ensure_ascii=False) + "\n")
        _real_read = _sj.read_json

        def _sneaky(path, expect=None):
            got = _real_read(path, expect=expect)
            if path == MACHINES:
                # ★読んだ直後に、別の処理が新台を1件足したことにする★
                io.open(MACHINES, "w", encoding="utf-8",
                        newline="\n").write(
                    _js2.dumps(_ms + [M("newbie", "2026-08-28")],
                               ensure_ascii=False) + "\n")
            return got

        _sj.read_json = _sneaky
        try:
            _err = _raises(lambda: run(True, ranked=["d", "b"], today=_T))
        finally:
            _sj.read_json = _real_read
        t("★★読んでから書くまでに変わっていたら、書かない★★"
          "（新台の公開と重なっても消さない）", _err)
        _now = _js2.loads(io.open(MACHINES, encoding="utf-8").read())
        t("　★足された新台がそのまま残っている★",
          [m["slug"] for m in _now][-1] == "newbie")
    finally:
        MACHINES = _keep
        _sh2.rmtree(_dir, ignore_errors=True)

    # ★★指摘3：今週まだ取り直せていない人気順は使わない★★
    _real_plan, _real_slugs = _pm.plan_today, _pm.popular_slugs
    try:
        _pm.popular_slugs = lambda *_a, **_k: ["d"]
        _pm.plan_today = lambda *_a, **_k: {"stale": "2026-08-24"}
        t("★★今週取り直せていない人気順では、並べ替えない★★",
          _raises(lambda: run(False, machines=_ms, today=_T)))
        t("　★はっきり指定すれば使える★",
          isinstance(run(False, machines=_ms, today=_T,
                         allow_stale=True), dict))
        _pm.plan_today = lambda *_a, **_k: {"stale": ""}
        t("　★今週取り直せていれば、そのまま並べ替える★",
          isinstance(run(False, machines=_ms, today=_T), dict))
    finally:
        _pm.plan_today, _pm.popular_slugs = _real_plan, _real_slugs

    ng = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def _raises(fn) -> bool:
    try:
        fn()
    except ReorderError:
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(
        description="machines.json の並び順（＝トップページの人気機種）を決める")
    ap.add_argument("--apply", action="store_true",
                    help="実際に machines.json を並べ替える")
    ap.add_argument("--allow-stale", dest="allow_stale",
                    action="store_true",
                    help="今週まだ取り直せていない人気順でも並べ替える")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    try:
        got = run(apply_it=a.apply, allow_stale=a.allow_stale)
    except (ReorderError, _pm.PopularError) as e:
        print(f"★{e}★")
        return 1
    print(f"機種 {got['total']} 件"
          f"（機械が読める導入日を持つのは {got['dated']} 件）")
    print(f"人気順で決まっている先頭 {len(got['top'])} 件:")
    for i, s in enumerate(got["top"], 1):
        print(f"  {i:>2}  {s}")
    print(f"並びが変わる機種: {got['moved']} 件")
    if not a.apply:
        print("★下見です★（--apply で machines.json を書き換えます）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

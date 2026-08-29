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
    d = release_of(m)
    if len(d) == 7:                     # "2026-09" → その月の終わり
        d = d + "-99"
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


def run(apply_it: bool = False, machines=None, ranked=None,
        today: str = "") -> dict:
    rows = machines
    if rows is None:
        rows = _sj.read_json(MACHINES, expect=list)
    got = ranked
    if got is None:
        got = _pm.popular_slugs(rows)
    if not today:
        # ★試験は必ず日付を渡す★（昼と夜で答えが変わる検査を作らない＝鉄則5e）
        import datetime as _dt
        today = _dt.date.today().isoformat()
    order = plan(rows, got, today)
    before = [m.get("slug") for m in rows]
    moved = sum(1 for a, b in zip(before, order) if a != b)
    out = {"order": order, "before": before, "moved": moved,
           "top": order[:TOP_N],
           "dated": sum(1 for m in rows if release_of(m)),
           "total": len(rows)}
    if apply_it and moved:
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
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    try:
        got = run(apply_it=a.apply)
    except (ReorderError, _pm.PopularError) as e:
        print(f"★{e}★")
        return 1
    print(f"機種 {got['total']} 件"
          f"（機械が読める導入日を持つのは {got['dated']} 件）")
    print("先頭20件（DMMの人気順）:")
    for i, s in enumerate(got["top"], 1):
        print(f"  {i:>2}  {s}")
    print(f"並びが変わる機種: {got['moved']} 件")
    if not a.apply:
        print("★下見です★（--apply で machines.json を書き換えます）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

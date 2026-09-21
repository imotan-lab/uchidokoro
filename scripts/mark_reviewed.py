#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""mark_reviewed.py — 「その機種は点検済み」の日付を書く唯一の場所。

★★なぜ要るか★★（2026-09-21・運営者の指示）
  手順書には「見つけたものを記録できていない機種は『点検済み』にしない」と
  **文章で**書いてあるだけで、★機械は何も止めていなかった★。
  ＝うっかり日付を押すと、その機種は順番から外れる。

★運営者が心配したこと★＝
  「Codexが止まっている間に拾おうとした台が、翌日また別の台になって
    忘れ去られないか」。
  ★新台（夜）は待ち行列に残る★ので消えない。
  ★更新（朝）で唯一あぶないのが、この日付のスタンプ★だった。

★断る条件★（1つでも当てはまれば書かない）
  ①その機種に、まだ終わっていない直しの記録がある
    （＝Codexが返らずに待っている件・途中で止まっている件）
  ②直しの記録が読めない／壊れている（fail-closed）
  ③状態ファイルが読めない・壊れている

★断っても困らない★＝日付を押さなければ、その機種は次の巡回で**また先頭に来る**
（古い順に拾うため）。押してしまうと直近10日は飛ばされる。

使い方:
    python scripts/mark_reviewed.py --slug <slug>          # 点検済みにする
    python scripts/mark_reviewed.py --check --slug <slug>  # 断る理由だけ見る
    python scripts/mark_reviewed.py --selftest
"""
from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import local_paths as _lp                                    # noqa: E402
import safe_json as _sj                                      # noqa: E402

STATE = _lp.doc("state.json")

# ★終わっている段階★（これ以外が残っていれば、その機種はまだ見終わっていない）
#   ★DONE と ESCALATED は「終わり」★＝直したか、人へ回したか。
#   ★BROKEN は終わりではない★＝読めないものを「無い」と言わない（fail-closed）。
FINISHED = ("DONE", "ESCALATED")


def unfinished(slug: str, rows=None) -> list:
    """★その機種で、まだ終わっていない直しの記録★

    ★`listing()` を使う★＝終わった記録は横へよけられるが、
    DONE / ESCALATED は次の再発まで残るので、段階でも見る。
    """
    if rows is None:
        import repair_journal as _rj
        rows = _rj.listing()
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            out.append({"finding_id": "?", "state": "BROKEN",
                        "why": "記録が辞書ではありません"})
            continue
        if str(r.get("slug") or "") != str(slug or ""):
            continue
        if str(r.get("state") or "") in FINISHED:
            continue
        out.append(r)
    return out


def problems(slug: str, rows=None, load_state=None) -> list:
    """★日付を押してはいけない理由★（空なら押してよい）"""
    ng = []
    if not str(slug or "").strip():
        return ["機種（slug）が要ります"]
    try:
        left = unfinished(slug, rows)
    except Exception as e:                                   # noqa: BLE001
        # ★読めないものを「無い」と言わない★
        return [f"直しの記録を読めません（{type(e).__name__}: {str(e)[:60]}）"]
    for r in left:
        ng.append("まだ終わっていない直しがあります"
                  f"（{r.get('finding_id')} / {r.get('state')}）"
                  "／★Codexが返るのを待っている件かもしれません★")
    try:
        (load_state or _read_state)()
    except Exception as e:                                   # noqa: BLE001
        ng.append(f"状態ファイルを読めません（{type(e).__name__}: {str(e)[:60]}）")
    return ng


def _read_state() -> dict:
    if not os.path.exists(STATE):
        return {}
    got = _sj.read_json(STATE, expect=dict)
    return got if isinstance(got, dict) else {}


def _write_state(st: dict) -> None:
    tmp = STATE + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(st, ensure_ascii=False, indent=1))
    os.replace(tmp, STATE)


def mark(slug: str, today: str = "", rows=None,
         load_state=None, save_state=None) -> tuple:
    """★点検済みにする★ → (書いたか, 理由)

    ★書いたら読み直して確かめる★（書けたつもりで終わらない）。
    """
    ng = problems(slug, rows, load_state)
    if ng:
        return False, "／".join(ng)
    day = str(today or datetime.date.today().isoformat())
    st = (load_state or _read_state)()
    st.setdefault("quality_review", {}).setdefault("last_reviewed", {})
    st["quality_review"]["last_reviewed"][slug] = day
    (save_state or _write_state)(st)
    back = (load_state or _read_state)()
    got = ((back.get("quality_review") or {}).get("last_reviewed") or {})
    if str(got.get(slug) or "") != day:
        return False, "書いた後の読み直しが合いません（手で確かめてください）"
    return True, f"{slug} を {day} に点検済みとして記録しました"


def selftest() -> int:
    ok = [0, 0]

    def t(name, cond):
        ok[1] += 1
        if cond:
            ok[0] += 1
            print("✅", name)
        else:
            print("❌", name)

    box = {"st": {}}

    def _load():
        import copy
        return copy.deepcopy(box["st"])

    def _save(x):
        box["st"] = x

    done = [{"slug": "zz", "state": "DONE", "finding_id": "a" * 16}]
    esc = [{"slug": "zz", "state": "ESCALATED", "finding_id": "b" * 16}]
    waiting = [{"slug": "zz", "state": "CLAUDE_SEALED", "finding_id": "c" * 16}]
    broken = [{"slug": "zz", "state": "BROKEN", "finding_id": "d" * 16}]
    other = [{"slug": "yy", "state": "CLAUDE_SEALED", "finding_id": "e" * 16}]

    t("　見つけたものが無ければ押せる",
      mark("zz", "2026-09-21", [], _load, _save)[0] is True)
    t("　押した日付が入る",
      ((box["st"].get("quality_review") or {}).get("last_reviewed") or {})
      .get("zz") == "2026-09-21")
    t("　終わった件（DONE）だけなら押せる",
      mark("zz", "2026-09-21", done, _load, _save)[0] is True)
    t("　人へ回した件（ESCALATED）だけなら押せる",
      mark("zz", "2026-09-21", esc, _load, _save)[0] is True)
    # ★★本題★★＝Codexが返らずに待っている件があるときは押さない
    box["st"] = {}
    _r = mark("zz", "2026-09-21", waiting, _load, _save)
    t("★★Codexを待っている件があれば押さない★★"
      "（★これが無いと、その機種は直近10日ぶん順番から外れる★）",
      _r[0] is False and "まだ終わっていない" in _r[1])
    t("　押さなかったときは、日付を1文字も書かない", box["st"] == {})
    t("★読めない記録は「無い」と言わない★（fail-closed）",
      mark("zz", "2026-09-21", broken, _load, _save)[0] is False)
    t("　別の機種の待ち件では止めない",
      mark("zz", "2026-09-21", other, _load, _save)[0] is True)
    t("　機種を言わなければ断る",
      mark("", "2026-09-21", [], _load, _save)[0] is False)

    def _boom():
        raise RuntimeError("壊れています")

    t("★状態ファイルを読めないときも押さない★",
      mark("zz", "2026-09-21", [], _boom, _save)[0] is False)

    # ★記録の一覧そのものが読めないとき★
    class _Bad:
        def __iter__(self):
            raise RuntimeError("一覧が読めません")

    t("　記録の一覧を読めないときも押さない",
      mark("zz", "2026-09-21", _Bad(), _load, _save)[0] is False)
    # ★書いた後の読み直し★
    # ★前の試験が書いた日付に助けられないよう、まだ無い機種で試す★（罠④）
    t("★書けていなければ、書けたと言わない★",
      mark("zz_never", "2026-09-21", [], _load, lambda x: None)[0] is False)

    print(f"\n{ok[0]}/{ok[1]} 合格")
    return 0 if ok[0] == ok[1] else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.slug:
        print("--slug が要ります")
        return 1
    if a.check:
        ng = problems(a.slug)
        if ng:
            print("★押しません★")
            for x in ng:
                print("  ・" + x)
            return 1
        print(f"{a.slug}: 押せます")
        return 0
    wrote, why = mark(a.slug)
    print(("" if wrote else "★押しません★ ") + why)
    return 0 if wrote else 1


if __name__ == "__main__":
    raise SystemExit(main())

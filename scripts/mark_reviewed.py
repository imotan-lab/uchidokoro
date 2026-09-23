#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""mark_reviewed.py — 「その機種は点検済み」の日付を書く唯一の場所。

★★なぜ要るか★★（2026-09-21・運営者の指示）
  手順書には「見つけたものを記録できていない機種は『点検済み』にしない」と
  **文章で**書いてあるだけで、★機械は何も止めていなかった★。

★運営者が心配したこと★＝
  「Codexが止まっている間に拾おうとした台が、翌日また別の台になって
    忘れ去られないか」。
  ★新台（夜）は待ち行列に残る★ので消えない。
  ★更新（朝）で唯一あぶないのが、この日付のスタンプ★だった。

★★2026-09-23に直した（Codexの指摘・実測で確かめた）★★
  ★直す前は「終わっていない直しが1件でもあれば押さない」だった★。
  ところが実際には、Codexとは関係なく**詰まって動けない記録**がある
  （実測＝終わっていない41件のうち、合意より後で止まっている機種が11）。
  ＝★その機種は永久に押せず、毎朝の枠を占領し続ける★
  （直す前は10日ごとに戻ってくるだけだった＝★私の変更で悪化させていた★）。
  罠⓸「断る守りだけを足して、直す道を作らない」そのもの。

★いまの決まり★
  ①押さないのは **2AIの判断待ちの段階**（DETECTED / CLAUDE_SEALED /
    CODEX_RECEIVED）の記録があるときだけ
    ＝Codexが要る部分。運営者が心配したのはここ。
    ★合意より後の段階は止めない★（Codexが要らない＝止めても戻らない）
  ②★壊れた記録は、どの機種か分からないので全機種を止める★
    （`listing()` は壊れた記録の機種名を空で返す。機種名で先に絞ると
     ★一度も効かない★＝2026-09-23にCodexが見つけた）
  ③★止めるのは最大 MAX_REFUSAL_DAYS 日★。それを超えたら押して通常の順番へ
    戻す（★記録そのものは直しの控えに残る＝忘れない★）。
    数えるのは「断った日の数」（同じ日に何度呼んでも1回）。
  ④記録の一覧や状態ファイルが読めないときは押さない（上限なし・大きな故障）

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

# ★2AIの判断待ちの段階★＝ここにある記録だけが「Codexが戻れば進む」もの
WAITING_2AI = ("DETECTED", "CLAUDE_SEALED", "CODEX_RECEIVED")
# ★止める日数の上限★（それを超えたら押して通常の順番へ戻す）
MAX_REFUSAL_DAYS = 2


def waiting(slug: str, rows=None) -> list:
    """★その機種で、2AIの判断を待っている直しの記録★（壊れた記録も含む）"""
    if rows is None:
        import repair_journal as _rj
        rows = _rj.listing()
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            out.append({"finding_id": "?", "state": "BROKEN",
                        "why": "記録が辞書ではありません"})
            continue
        state = str(r.get("state") or "")
        # ★★壊れた記録は機種名より先に見る★★（2026-09-23・Codexの指摘）
        #   `listing()` は壊れた記録の機種名を空で返すので、
        #   機種名で先に絞ると★どの機種にも一度も効かなかった★。
        if state == "BROKEN":
            out.append(r)
            continue
        if str(r.get("slug") or "") != str(slug or ""):
            continue
        if state in WAITING_2AI:
            out.append(r)
    return out


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


def problems(slug: str, rows=None, load_state=None) -> list:
    """★押してはいけない理由★（空なら押してよい）＝日数の上限は見ない"""
    if not str(slug or "").strip():
        return ["機種（slug）が要ります"]
    try:
        left = waiting(slug, rows)
    except Exception as e:                                   # noqa: BLE001
        return [f"直しの記録を読めません（{type(e).__name__}: {str(e)[:60]}）"]
    try:
        (load_state or _read_state)()
    except Exception as e:                                   # noqa: BLE001
        return [f"状態ファイルを読めません（{type(e).__name__}: {str(e)[:60]}）"]
    ng = []
    for r in left:
        if r.get("state") == "BROKEN":
            ng.append(f"壊れた直しの記録があります（{r.get('finding_id')}）"
                      "／★どの機種か分からないので全機種で止めます★")
        else:
            ng.append("2AIの判断待ちの直しがあります"
                      f"（{r.get('finding_id')} / {r.get('state')}）"
                      "／★Codexが戻れば続きから進みます★")
    return ng


def _fatal(ng: list) -> bool:
    """★読めない故障か★（日数の上限を当てない＝直すまで押さない）"""
    return any(x.startswith(("直しの記録を読めません", "状態ファイルを読めません",
                             "機種（slug）が要ります")) for x in ng)


def mark(slug: str, today: str = "", rows=None,
         load_state=None, save_state=None) -> tuple:
    """★点検済みにする★ → (書いたか, 理由)

    ★書いたら読み直して確かめる★（書けたつもりで終わらない）。
    """
    day = str(today or datetime.date.today().isoformat())
    ng = problems(slug, rows, load_state)
    if ng and _fatal(ng):
        return False, "／".join(ng)
    st = (load_state or _read_state)()
    qr = st.setdefault("quality_review", {})
    refused = qr.setdefault("stamp_refused", {})
    days = [str(x) for x in (refused.get(slug) or []) if str(x)]
    if ng:
        if day not in days:
            days.append(day)
        if len(days) <= MAX_REFUSAL_DAYS:
            refused[slug] = days
            (save_state or _write_state)(st)
            return False, ("／".join(ng)
                           + f"（★{len(days)}日目／{MAX_REFUSAL_DAYS}日までは"
                             "押さずに翌朝また拾います★）")
        note = (f"★{MAX_REFUSAL_DAYS}日待っても終わらないので、"
                "通常の順番へ戻します（直しの記録は残っています）★")
    else:
        note = ""
    refused.pop(slug, None)
    qr.setdefault("last_reviewed", {})[slug] = day
    (save_state or _write_state)(st)
    back = (load_state or _read_state)()
    got = ((back.get("quality_review") or {}).get("last_reviewed") or {})
    if str(got.get(slug) or "") != day:
        return False, "書いた後の読み直しが合いません（手で確かめてください）"
    return True, (f"{slug} を {day} に点検済みとして記録しました"
                  + (f"／{note}" if note else ""))


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
        import copy
        box["st"] = copy.deepcopy(x)

    def _lr(slug):
        return ((box["st"].get("quality_review") or {})
                .get("last_reviewed") or {}).get(slug)

    done = [{"slug": "zz", "state": "DONE", "finding_id": "a" * 16}]
    esc = [{"slug": "zz", "state": "ESCALATED", "finding_id": "b" * 16}]
    waiting_ = [{"slug": "zz", "state": "CLAUDE_SEALED", "finding_id": "c" * 16}]
    after = [{"slug": "zz", "state": "PUSH_CONFIRMED", "finding_id": "f" * 16}]
    # ★本物の `listing()` が返す形★＝壊れた記録は機種名が空（罠①）
    broken = [{"slug": "", "state": "BROKEN", "finding_id": "d" * 16,
               "check": "", "quote": "", "_broken": "JSONDecodeError"}]
    other = [{"slug": "yy", "state": "CLAUDE_SEALED", "finding_id": "e" * 16}]

    t("　見つけたものが無ければ押せる",
      mark("zz", "2026-09-21", [], _load, _save)[0] is True
      and _lr("zz") == "2026-09-21")
    t("　終わった件（DONE）だけなら押せる",
      mark("zz", "2026-09-21", done, _load, _save)[0] is True)
    t("　人へ回した件（ESCALATED）だけなら押せる",
      mark("zz", "2026-09-21", esc, _load, _save)[0] is True)

    # ★★本題★★＝2AIの判断待ちがあれば押さない
    box["st"] = {}
    _r = mark("zz", "2026-09-21", waiting_, _load, _save)
    t("★★2AIの判断待ちがあれば押さない★★"
      "（★Codexが止まっている間に拾った台を翌日また拾うため★）",
      _r[0] is False and "判断待ち" in _r[1])
    t("　押さなかったときは、点検済みの日付を書かない", _lr("zz") is None)

    # ★★合意より後で詰まっている記録は止めない★★（2026-09-23）
    box["st"] = {}
    t("★★合意より後で詰まっている記録では止めない★★"
      "（★Codexが要らない＝止めても戻らず、毎朝の枠を占領し続けた★）",
      mark("zz", "2026-09-21", after, _load, _save)[0] is True)

    # ★★壊れた記録＝本物の形（機種名が空）で、全機種を止める★★
    box["st"] = {}
    t("★★壊れた記録（機種名が空）でも止める★★"
      "（★直す前は機種名で先に絞っていたので一度も効かなかった★）",
      mark("zz", "2026-09-21", broken, _load, _save)[0] is False)
    t("　壊れた記録は、どの機種でも止める（どれの記録か分からないため）",
      mark("yy", "2026-09-21", broken, _load, _save)[0] is False)

    # ★★止めるのは最大2日★★（罠⓸＝断るだけで出口が無いと永久に居座る）
    box["st"] = {}
    r1 = mark("zz", "2026-09-24", waiting_, _load, _save)
    r1b = mark("zz", "2026-09-24", waiting_, _load, _save)
    r2 = mark("zz", "2026-09-25", waiting_, _load, _save)
    r3 = mark("zz", "2026-09-26", waiting_, _load, _save)
    t("　1日目は押さない", r1[0] is False and "1日目" in r1[1])
    t("　同じ日に何度呼んでも1日と数える", r1b[0] is False and "1日目" in r1b[1])
    t("　2日目も押さない", r2[0] is False and "2日目" in r2[1])
    t("★★3日目は押して通常の順番へ戻す★★"
      "（★これが無いと、終わらない記録が1件あるだけで永久に枠を占領する★）",
      r3[0] is True and "通常の順番へ戻します" in r3[1]
      and _lr("zz") == "2026-09-26")
    t("　押したら断った日数は消す（次の件はまた2日待つ）",
      "zz" not in ((box["st"].get("quality_review") or {})
                   .get("stamp_refused") or {}))

    # ★★2台並べて、先頭が待っていても2台目は同じ朝に押せる★★（Codexが求めた試験）
    box["st"] = {}
    rows2 = waiting_ + [{"slug": "ww", "state": "DONE", "finding_id": "g" * 16}]
    a1 = mark("zz", "2026-09-24", rows2, _load, _save)
    a2 = mark("ww", "2026-09-24", rows2, _load, _save)
    t("★2台目は、1台目が待っていても同じ朝に押せる★",
      a1[0] is False and a2[0] is True and _lr("ww") == "2026-09-24")

    t("　別の機種の待ち件では止めない",
      mark("zz", "2026-09-21", other, _load, _save)[0] is True)
    t("　機種を言わなければ断る",
      mark("", "2026-09-21", [], _load, _save)[0] is False)

    def _boom():
        raise RuntimeError("壊れています")

    t("★状態ファイルを読めないときは押さない★（日数の上限も当てない）",
      mark("zz", "2026-09-21", [], _boom, _save)[0] is False)

    class _Bad:
        def __iter__(self):
            raise RuntimeError("一覧が読めません")

    box["st"] = {}
    for d in ("2026-09-24", "2026-09-25", "2026-09-26", "2026-09-27"):
        _rb = mark("zz", d, _Bad(), _load, _save)
    t("★記録の一覧を読めないときは、何日たっても押さない★（大きな故障）",
      _rb[0] is False and _lr("zz") is None)
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

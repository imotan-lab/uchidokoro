# -*- coding: utf-8 -*-
"""★新台の待ち行列で「同じ機種か」「ページが消えた機種をどうするか」を、2AIが決めた答えで書く唯一の場所★（2026-11-15）

使い方:
  python scripts/pending_same.py --apply <決定ファイル> [--dry-run]
  python scripts/pending_same.py --questions          … いまの問いを見る（書かない）
  python scripts/pending_same.py --selftest

★なぜ要るか★（運営者「変わる可変のものは基準にするのやめるのはどう？」「単純に機種名でDMMで2AIなんだからわかるくない？」）
  待ち行列はDMMの機種の番号で機種を見分けていた。DMMが番号を振り直すと（Lアサルトリリィ 5118→5147）、
  同じ機種が別の行として入り、古い行は毎晩404で空回りした。
  ★番号は変わってよい中身の1つ★にして、同じ機種か・消えたかは2AIが決める。
  問いは夜の新台タスクの実行器（add_machine_run.py）が「★2AIに聞くこと（待ち行列）」として出す
  （作るのは pending_machines.same_machine_questions）。

★決定ファイル★（Writeツールで書く・by は claude と codex の組、Codexが利用制限なら claude-agent-a と claude-agent-b）
  同じ機種 … {"decision": "SAME", "keep": "q_0045", "drop": "q_0030", "by": [...], "why": "<15字以上>"}
             ＝drop を keep へまとめる（★ページが在る方を keep に★）。drop の中身は keep の merged_from に残る
  別の機種 … {"decision": "DIFFERENT", "ids": ["q_0001", "q_0002"], "by": [...], "why": "..."}
             ＝同じページの組のあいだは、もう聞かない
  新しいページ … {"decision": "MOVE", "id": "q_0030", "url": "https://p-town.dmm.com/machines/5147", "by": [...], "why": "..."}
             ＝その行を新しい番号のページへ移す（古いページは moved_from に残る）
  消えた … {"decision": "GONE", "id": "q_0030", "by": [...], "why": "..."}
             ＝待ち行列から外して「外した機種の控え」へ移す（消さない・30日ごとに試し直す）。
             その晩のログに [PENDING_PAGE_GONE] を残す（朝の点検が🟡で知らせる）

★機械が見るのは形だけ★＝判断者の組・理由15字・行が待ち行列にある・
  DMMの機種ページの形・同じページを持つ別の行が無い（MOVE）・
  ★ページが無かった印のある行だけを MOVE・GONE できる★（生きている機種を消さない）・
  ★今晩ページが無かった行を、ページの在る行に重ねて残さない★（SAME の keep）。
  同じ機種かどうか・消えたかどうか（意味の判断）は2AI。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import confirmed_values as _cv                            # noqa: E402
import pending_machines as _pend                          # noqa: E402

MIN_WHY = 15
DECISIONS = ("SAME", "DIFFERENT", "MOVE", "GONE")


def _amr():
    import add_machine_run as m
    return m


def _log(msg: str) -> bool:
    """夜の新台タスクと同じログへ書く（★朝の点検が [PENDING_PAGE_GONE] を数えるのはそのログ★）"""
    return _amr()._log(msg)


def problems(dec: dict, data: dict) -> list:
    """★決定ファイルが書けるかを見る★（空なら書ける）"""
    out = []
    if not isinstance(dec, dict):
        return ["決定ファイルが辞書ではありません"]
    kind = dec.get("decision")
    if kind not in DECISIONS:
        return [f"decision は {'/'.join(DECISIONS)} のどれかです: {kind!r}"]
    if not _cv.judges_exact(dec.get("by")):
        out.append("by は claude と codex の組（または claude-agent-a と claude-agent-b の組）です")
    if len(str(dec.get("why") or "").strip()) < MIN_WHY:
        out.append(f"why は{MIN_WHY}字以上で、なぜそう決めたかを書いてください")
    items = data.get("items") or {}

    def row(q):
        it = items.get(str(q or ""))
        if not isinstance(it, dict):
            out.append(f"待ち行列にない行です: {q!r}")
        return it

    if kind == "SAME":
        k, d = row(dec.get("keep")), row(dec.get("drop"))
        if str(dec.get("keep")) == str(dec.get("drop")):
            out.append("keep と drop が同じ行です")
        elif k and d and _pend.page_gone_tonight(k) and not _pend.page_gone_tonight(d):
            out.append("今晩ページが無かった行を残そうとしています（ページが在る方を keep にしてください）")
    elif kind == "DIFFERENT":
        ids = dec.get("ids")
        if not (isinstance(ids, list) and len(ids) == 2 and ids[0] != ids[1]):
            out.append("ids は別々の2行です")
        else:
            row(ids[0]), row(ids[1])
    else:
        it = row(dec.get("id"))
        if it is not None and not it.get("page_gone"):
            out.append("DMMの機種ページが無かった印のない行は、移す・外すことができません（生きている機種を外さない）")
        if kind == "MOVE":
            url = str(dec.get("url") or "").strip()
            mid = _amr()._dmm_machine_id(url)
            if not mid:
                out.append(f"url はDMMの機種ページ（https://p-town.dmm.com/machines/<番号>）です: {url!r}")
            elif it is not None:
                if str(it.get("source_machine_id") or "") == mid:
                    out.append("いまと同じページです")
                for other in items.values():
                    if (isinstance(other, dict) and other is not it
                            and (str(other.get("source_machine_id") or "") == mid
                                 or str(other.get("identity_url") or "").rstrip("/") == url.rstrip("/"))):
                        out.append(f"そのページはもう別の行（{other.get('queue_id')}）にあります＝SAME でまとめてください")
    return out


def apply(dec: dict, data: dict, today: str = "") -> str:
    """★書く★（problems が空のときだけ呼ぶ）。ログに残す一文を返す"""
    kind, by, why = dec["decision"], list(dec["by"]), str(dec["why"]).strip()
    if kind == "SAME":
        k = _pend.merge_same(data, dec["keep"], dec["drop"], by, why, today)
        return f"★待ち行列の2行を1つにまとめました（2AI）★ {dec['drop']} → {dec['keep']}「{k.get('name', '')}」 {k.get('identity_url', '')}"
    if kind == "DIFFERENT":
        _pend.mark_not_same(data, dec["ids"][0], dec["ids"][1], by, why, today)
        return f"★別の機種と決めました（2AI）★ {dec['ids'][0]} / {dec['ids'][1]}"
    if kind == "MOVE":
        url = str(dec["url"]).strip()
        it = _pend.move_page(data, dec["id"], url, _amr()._dmm_machine_id(url), by, why, today)
        return f"★新しい機種ページへ移しました（2AI）★ {dec['id']}「{it.get('name', '')}」 → {url}"
    it = _pend.retire_gone(data, dec["id"], today, by, why)
    return f"★消えた機種として待ち行列から外しました（2AI）★ {dec['id']}「{(it or {}).get('name', '')}」"


def run(path: str, dry_run: bool = False, today: str = "") -> int:
    try:
        with open(path, encoding="utf-8") as f:
            dec = json.load(f)
    except (OSError, ValueError) as e:
        print(f"★決定ファイルを読めません: {e}★")
        return 1
    data = _pend.load()
    bad = problems(dec, data)
    if bad:
        for p in bad:
            print("  ✗ " + p)
        print("★書きませんでした★")
        return 1
    if dec["decision"] == "GONE":
        it = data["items"][str(dec["id"])]
        g = it.get("page_gone") or {}
        detail = (f"{it.get('name', '')} / {it.get('identity_url', '')} / 404 {g.get('first')}〜{g.get('last')}"
                  f"（続けて{g.get('nights')}晩）\n理由（2AI {','.join(dec['by'])}）: {str(dec['why']).strip()}\n"
                  "★外した機種の控えに残します（30日ごとに試し直す）★")
        if dry_run:
            print("（下見）消えた機種として外します: " + detail.splitlines()[0])
            return 0
        # ★ログに残せたときだけ外す★（残せないまま外すと、どこにも記録が無い機種になる）
        if not _amr()._record_issue("site", "structural", "MATERIAL", "PENDING_PAGE_GONE",
                                    "DMMの機種ページが無く、2AIが消えた機種と決めたので待ち行列から外しました", detail):
            print("★ログに残せなかったので外しません★")
            return 1
    msg = apply(dec, data, today)
    if dry_run:
        print("（下見）" + msg)
        return 0
    _pend.save(data)
    _log("  " + msg + f" 理由（{','.join(dec['by'])}）: {str(dec['why']).strip()}")
    return 0


def selftest() -> int:
    ok = fail = 0

    def t(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print("❌ " + name)

    amr = _amr()
    real_store, real_flag = _pend.STORE, amr._IN_SELFTEST
    real_before = open(real_store, "rb").read() if os.path.exists(real_store) else None
    tmp = tempfile.mkdtemp()
    _pend.STORE = os.path.join(tmp, "pending.json")
    amr._IN_SELFTEST = True                    # ★試験では本番のログへ書かない★
    try:
        D = "https://p-town.dmm.com/machines/"
        data = _pend._empty()
        _pend.add(data, "L アサルトリリィ", D + "5118", "dmm:スパイキー", "2026-12", source_machine_id="5118")
        _pend.add(data, "Lアサルトリリィ", D + "5147", "dmm:スパイキー", "2026-12", source_machine_id="5147")
        _pend.add(data, "生きている機種", D + "7", "m", "2026-12", source_machine_id="7")
        _pend.add(data, "消える機種", D + "8", "m", "2026-12", source_machine_id="8")
        _pend.note_page_gone(data["items"]["q_0001"])
        _pend.note_page_gone(data["items"]["q_0004"])
        _pend.save(data)
        by, why = ["claude", "codex"], "DMMが番号を振り直した同じ機種と2AIで確かめた"

        def dec_file(obj):
            p = os.path.join(tmp, "dec.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False)
            return p

        t("判断者の組が無ければ書かない",
          run(dec_file({"decision": "SAME", "keep": "q_0002", "drop": "q_0001", "by": ["claude"], "why": why})) == 1
          and len(_pend.load()["items"]) == 4)
        t("理由が短ければ書かない",
          run(dec_file({"decision": "SAME", "keep": "q_0002", "drop": "q_0001", "by": by, "why": "同じ"})) == 1)
        t("★今晩ページが無かった行を残す向きでは書かない★",
          run(dec_file({"decision": "SAME", "keep": "q_0001", "drop": "q_0002", "by": by, "why": why})) == 1
          and len(_pend.load()["items"]) == 4)
        t("下見は書かない",
          run(dec_file({"decision": "SAME", "keep": "q_0002", "drop": "q_0001", "by": by, "why": why}), dry_run=True) == 0
          and len(_pend.load()["items"]) == 4)
        t("★★2AIが同じと決めたら1行にまとめる★★",
          run(dec_file({"decision": "SAME", "keep": "q_0002", "drop": "q_0001", "by": by, "why": why})) == 0
          and "q_0001" not in _pend.load()["items"]
          and _pend.load()["items"]["q_0002"]["merged_from"][0]["identity_url"] == D + "5118")
        t("★生きている機種（ページが無かった印が無い）は外せない★",
          run(dec_file({"decision": "GONE", "id": "q_0003", "by": by, "why": why})) == 1
          and "q_0003" in _pend.load()["items"])
        t("★生きている機種は別のページへ移せない★",
          run(dec_file({"decision": "MOVE", "id": "q_0003", "url": D + "70", "by": by, "why": why})) == 1)
        t("★もう別の行にあるページへは移さない（SAME でまとめる）★",
          run(dec_file({"decision": "MOVE", "id": "q_0004", "url": D + "7", "by": by, "why": why})) == 1)
        t("DMMの機種ページの形でなければ移さない",
          run(dec_file({"decision": "MOVE", "id": "q_0004", "url": "https://example.com/machines/9", "by": by, "why": why})) == 1)
        t("知らない決定は書かない",
          run(dec_file({"decision": "MERGE", "id": "q_0004", "by": by, "why": why})) == 1)
        t("★★2AIが新しいページを見つけたら、その行を移す★★",
          run(dec_file({"decision": "MOVE", "id": "q_0004", "url": D + "80", "by": by, "why": why})) == 0
          and _pend.load()["items"]["q_0004"]["identity_url"] == D + "80"
          and "page_gone" not in _pend.load()["items"]["q_0004"])
        _w = _pend.load()
        _pend.note_page_gone(_w["items"]["q_0004"])
        _pend.save(_w)
        _seen = []
        _real_rec = amr._record_issue
        amr._record_issue = lambda *a: (_seen.append(a[3]), False)[1]
        t("★ログに残せなければ外さない★",
          run(dec_file({"decision": "GONE", "id": "q_0004", "by": by, "why": why})) == 1
          and "q_0004" in _pend.load()["items"] and _seen == ["PENDING_PAGE_GONE"])
        amr._record_issue = lambda *a: (_seen.append(a[3]), True)[1]
        t("★★2AIが消えたと決めたら外した機種の控えへ移し、[PENDING_PAGE_GONE] を残す★★",
          run(dec_file({"decision": "GONE", "id": "q_0004", "by": ["claude-agent-a", "claude-agent-b"], "why": why})) == 0
          and "q_0004" not in _pend.load()["items"]
          and (_pend.load().get("gave_up") or {}).get("q_0004", {}).get("gone_reason") == "DMM_PAGE_GONE"
          and _seen[-1] == "PENDING_PAGE_GONE")
        amr._record_issue = _real_rec
        _pend.add(_w2 := _pend.load(), "L似た機種", D + "11", "m", "2026-12", source_machine_id="11")
        _pend.add(_w2, "似た機種", D + "12", "m", "2026-12", source_machine_id="12")
        _pend.save(_w2)
        _ids = sorted(q for q, x in _w2["items"].items() if "似た" in x["name"])
        t("★★2AIが別の機種と決めたら、もう聞かない★★",
          run(dec_file({"decision": "DIFFERENT", "ids": _ids, "by": by, "why": "L版と無印で導入日もメーカーの型も違う"})) == 0
          and _pend.same_machine_questions(_pend.load()) == [])
    finally:
        _pend.STORE, amr._IN_SELFTEST = real_store, real_flag
    t("★試験は本番の待ち行列に書かない★",
      (open(real_store, "rb").read() if os.path.exists(real_store) else None) == real_before)
    print(f"{ok}/{ok + fail} 合格")
    return 0 if fail == 0 else 1


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--questions", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.questions:
        qs = _pend.same_machine_questions(_pend.load())
        for q in qs:
            print(f"★2AIに聞くこと（待ち行列）: {q['text']}")
        print(f"問い {len(qs)} 件")
        return 0
    if a.apply:
        return run(a.apply, a.dry_run)
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

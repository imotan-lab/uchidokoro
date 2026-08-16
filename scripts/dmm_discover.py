# -*- coding: utf-8 -*-
"""dmm_discover.py — DMMぱちタウンの新台カレンダーから新台を見つけて待たせる。

★なぜDMMなのか（2026-08-16・台帳#376）★
  P-WORLDの利用規約がプログラムからのアクセスとデータ収集を禁じていたため、
  新台の入口をDMMへ移しました。★通信は blocked_hosts.py が止めます★

★この子の仕事は3つだけ★
  ①カレンダーを見て、まだ記事が無い機種を拾う
  ②機種ページで同定できたものを待ち行列へ入れる
  ③**DMMに載るのを待っていた控えを、載った時点で結び直す**
    （P-WORLD時代に見つけたのに、DMMのカレンダーにまだ無い機種がある）

★値は読み取らない★
  ここで取るのは「どの機種が・いつ・どのページか」まで。
  天井や機械割といった中身は 2AI の突き合わせで決めます。

★迷ったら待たせる★
  同定できなければ捨てずに待ち行列へ入れ、翌晩やり直します
  （捨てると、その機種は二度と出てきません）。

使い方:
    python scripts/dmm_discover.py            # 下見（何も書かない）
    python scripts/dmm_discover.py --apply
    python scripts/dmm_discover.py --selftest
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
import unicodedata

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

MAKER_CATALOG = os.path.join(BASE, "assets", "data", "maker-catalogs.json")
MACHINES = os.path.join(BASE, "assets", "data", "machines.json")

# 何か月先まで見るか（★先の月にしか無い新台を拾う★）
MONTHS_AHEAD = 3


def _norm(s: str) -> str:
    """メーカー名を突き合わせる形に整える（★推測はしない★）。"""
    s = unicodedata.normalize("NFKC", str(s or ""))
    return re.sub(r"[\s　・（）()＆&,、。.]+", "", s).lower()


def maker_index(path: str = MAKER_CATALOG) -> dict:
    """メーカーの表示名 → 既存のメーカーID。

    ★文字の似ている名前を勝手に結び付けない★
      名簿に書いてある名前（name と directory_names）と**完全に一致**した時だけ。
      「ユニバーサル」と「ユニバーサルブロス」は別会社なので、
      前方一致や部分一致で結ぶと別会社の機種になる。
    """
    import safe_json as _sj
    data = _sj.read_json(path, expect=dict)
    out: dict = {}
    for mid, info in (data.get("catalogs") or {}).items():
        names = [info.get("name")] + list(info.get("directory_names") or [])
        for n in names:
            key = _norm(n)
            if not key:
                continue
            if key in out and out[key] != mid:
                # ★同じ名前が2社にぶら下がっていたら決められない★
                out[key] = ""
            else:
                out.setdefault(key, mid)
    return {k: v for k, v in out.items() if v}


def _machines() -> list:
    import safe_json as _sj
    d = _sj.read_json(MACHINES, expect=(dict, list))
    return d["machines"] if isinstance(d, dict) else d


def candidates(today=None, months_ahead: int = MONTHS_AHEAD) -> tuple:
    """まだ記事が無い新台の候補。返すもの: (候補, 読めなかった月)

    ★読めなかった月を隠さない★＝「新台なし」と「読めなかった」は別。
    """
    import claim_identity as _ci
    import dmm_calendar as _dc
    today = today or datetime.date.today()
    known = {_ci.normalize_core(m.get("name") or "") for m in _machines()}
    known.discard("")
    out, bad = [], []
    for y, mo in _dc.months_ahead(today, months_ahead):
        try:
            rows = _dc.fetch(y, mo)
        except _dc.CalendarError as e:
            bad.append(f"{y}年{mo}月: {str(e)[:120]}")
            continue
        for r in rows:
            if _ci.normalize_core(r["name"]) in known:
                continue                    # もう記事がある
            out.append(r)
    return out, bad


def check_one(row: dict, index: dict) -> dict:
    """1機種ぶん、機種ページで同定してメーカーIDまで決める。

    ★同定できるか／メーカーが名簿にあるかは別の話★
      メーカーが決まらなくても機種ページの同定は成り立つので、
      **理由を分けて返す**（呼ぶ側が「待たせる／2AIへ回す」を選べる）。
    """
    import dmm_machine as _dm
    out = dict(row)
    try:
        got = _dm.fetch(row["id"])
    except _dm.MachineError as e:
        out["ok"] = False
        out["reason"] = f"機種ページを確かめられません: {str(e)[:200]}"
        return out
    ok, why = _dm.name_matches(got["heading"], row["name"])
    if not ok:
        out["ok"] = False
        out["reason"] = f"カレンダーと機種ページの機種名が合いません: {why[:200]}"
        return out
    # ★導入日は日まで分かるカレンダー側を正とする★
    if not row["release_date"].startswith(got["release_date"]):
        out["ok"] = False
        out["reason"] = (f"導入日が食い違います"
                         f"（カレンダー{row['release_date']} / "
                         f"機種ページ{got['release_date']}）")
        return out
    out["dmm_maker"] = got["maker"]
    out["model_code"] = got["model_code"]
    out["has_model_code"] = got["has_model_code"]
    out["url"] = got["url"]
    mid = index.get(_norm(got["maker"]))
    if not mid:
        # ★ここで場合分けを足さない★＝名簿で決まらないものは2AIの出番
        #   （CLAUDE.md「名簿で決まらないメーカー欄は、人を待たずその場で
        #     2AIへ回す」＝新台SKILL.mdのSTEP 3-B-M）
        out["ok"] = False
        out["reason"] = (f"メーカーが名簿にありません: {got['maker']!r}"
                         "／★2AIで照合してください（STEP 3-B-M）★")
        return out
    out["maker_id"] = mid
    out["ok"] = True
    out["reason"] = ""
    return out


def rebind_waiting(data: dict, rows: list, checked: dict | None = None) -> list:
    """★DMMに載るのを待っていた控えを、載った時点で結び直す★

    P-WORLD時代に見つけた機種が、DMMのカレンダーに遅れて載ることがある
    （実データ: 聖闘士星矢はまだ載っていない）。控えを消さずに待たせ、
    現れたら**同じ控え**へ機種IDを入れる（新しい控えを作らない）。

    ★結び付けるのは機種名の芯が完全に一致したときだけ★
      前方一致・似ている判定はしない（当サイトの鉄則）。
      決まらないものは二重に持ったまま人・2AIに見てもらう。

    ★機種ページを確かめてからでないと READY にしない★
      （2026-08-16・依頼213の指摘3）
      前はカレンダーに名前が出ただけで READY にしていた。
      機種ページの取得・機種名・導入日・メーカーのどれかで落ちても
      **一度 READY になった状態は戻らない**ので、
      確かめられていない控えが記事づくりの列に入ってしまう。
      `checked` は check_one() の結果（機種ID → 判定）。
      ★合格したものだけ結び直す★
    """
    import pending_machines as _pm
    done = []
    for r in rows:
        got = (checked or {}).get(r["id"]) or {}
        for it in _pm.find_by_core(data, r["name"]):
            if it.get("state") != _pm.AWAITING_DMM_ID:
                continue
            # ★機種IDは、確かめられなくても先に結ぶ★
            #   （2026-08-16・依頼214の指摘3）
            #   結ばずに先へ進むと、そのあと `add()` が**同じ機種の控えを
            #   もう1件**作る（機種IDで探しても見つからないため）。
            #   翌晩に確かめられて元の控えも READY になると、
            #   **同じ機種の控えが2件**になる。
            it["source_machine_id"] = r["id"]
            it["identity_url"] = got.get("url") or r["url"]
            it["identity_source"] = "dmm"
            it["release"] = r["release_date"]
            # ★機種IDが取れたら READY にする★（2026-08-16・依頼215の指摘2）
            #   READY は「公開してよい」ではなく
            #   **「機種ページが分かっていて、記事づくりの列でもう一度
            #   確かめられる状態」**と決める。
            #   確かめられなかった分を AWAITING_DMM_ID に残すと、
            #   ①記事づくりの対象外 ②60日打ち切りの対象外
            #   ③「DMMに載っていない」と誤って知らせる、の3つが重なり、
            #   **確かめられない機種が永久に溜まる**（新規の候補は
            #   READY で入るので、扱いも食い違う）。
            it["state"] = _pm.READY
            if not got.get("ok"):
                it["last_reason"] = ("DMMに載りましたが、まだ確かめられません: "
                                     + str(got.get("reason") or "")[:200])
                continue
            it["maker"] = got.get("maker_id") or it.get("maker") or ""
            # ★覚えた表示名は上書きしない★（違う値は食い違いとして残す）
            _remember(it, "dmm_maker", got.get("dmm_maker"))
            it["last_reason"] = "DMMのカレンダーに載ったので結び直しました"
            done.append(it)
    return done


def _remember(item: dict, key: str, value) -> None:
    """★一度覚えたものは変えない★（違う値は食い違いとして残す）

    （2026-08-16・依頼214）待ち行列の `add()` と同じ扱いにする。
    ここだけ直接代入にすると、**公開直前の照合がその値に合わせて緩む**。
    """
    if not value:
        return
    old = item.get(key)
    if old and old != value:
        item.setdefault(key + "_conflict", []).append(value)
        item[key + "_conflict"] = sorted(set(item[key + "_conflict"]))[:5]
        return
    item[key] = value


def run(apply_it: bool = False, today=None) -> dict:
    """カレンダーを見て、確かめられた新台を待ち行列へ入れる。"""
    import pending_machines as _pend
    out = {"looked": 0, "queued": [], "held": [], "problems": [],
           "rebound": []}
    try:
        rows, bad = candidates(today=today)
    except Exception as e:                  # noqa: BLE001
        # ★読めなかったことを「新台なし」にしない★
        out["problems"].append(f"カレンダーを読めません: {type(e).__name__}: {e}")
        return out
    out["problems"] += [f"カレンダーを読めません {b}" for b in bad]
    out["looked"] = len(rows)
    index = maker_index()
    data = _pend.load() if apply_it else None
    # ★先に全部の機種ページを確かめる★（2026-08-16・依頼213の指摘3）
    #   確かめてからでないと「待っていた控え」を READY にできない。
    checked = {row["id"]: check_one(row, index) for row in rows}
    if apply_it:
        # ★確かめられたものだけ、待っていた控えへ結び直す★
        #   （新しい控えを二重に作らないため、記事づくりより先にやる）
        out["rebound"] = [{"queue_id": x["queue_id"], "name": x["name"]}
                          for x in rebind_waiting(data, rows, checked)]
    for row in rows:
        got = checked[row["id"]]
        if not got["ok"]:
            out["held"].append({"name": row["name"], "reason": got["reason"],
                                "maker": got.get("dmm_maker", "")})
            # ★飛ばさずに待ち行列へ残す★（翌晩また試す）
            if apply_it:
                # ★あとで引き直せない手掛かりを必ず残す★（台帳#335の項目4）
                _pend.add(data, row["name"], row["url"], "", "",
                          reason=got["reason"],
                          source_machine_id=row["id"], identity_source="dmm",
                          extra={"dmm_maker": got.get("dmm_maker", ""),
                                 "dmm_id": row["id"]})
            continue
        out["queued"].append({"name": row["name"], "url": got["url"],
                              "maker": got["maker_id"],
                              "release": row["release_date"],
                              "dmm_id": row["id"]})
        if apply_it:
            # ★最初に確かめた表示名を覚える★（台帳#335の項目5）
            #   公開直前の再確認で内部IDしか無いと、同じIDにぶら下がる
            #   別名（ミズホ／メーシー…）のどれでも通ってしまう。
            _pend.add(data, row["name"], got["url"], got["maker_id"],
                      row["release_date"], reason="DMMのカレンダーから",
                      source_machine_id=row["id"], identity_source="dmm",
                      extra={"dmm_maker": got.get("dmm_maker", ""),
                             "dmm_id": row["id"],
                             "dmm_model_code": got.get("model_code", "")})
    if apply_it:
        _pend.save(data)
    return out


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅ " if cond else "❌ ") + name)

    import blocked_hosts as _bh
    import pending_machines as _pm

    t("★★規約で禁止された先には通信しない★★（P-WORLDのカレンダー）",
      _bh.is_blocked("https://www.p-world.co.jp/database/machine/"
                     "introduce_calendar.cgi"))
    idx = maker_index()
    t("　メーカー名簿を読める（表示名からメーカーIDが引ける）", len(idx) > 20)
    t("　同じ表示名が2社にぶら下がるものは載せない", all(idx.values()))

    # ★★待っていた控えを結び直す★★（DMMに遅れて載る機種）
    d = _pm._empty()
    it = _pm.add(d, "スマスロ ラグナドール", "", "", "2026-11",
                 reason="DMMのカレンダーに無い", state=_pm.AWAITING_DMM_ID)
    rows = [{"id": "5079", "name": "スマスロ ラグナドール",
             "url": "https://p-town.dmm.com/machines/5079",
             "release_date": "2026-11-02", "kind": "パチスロ"}]
    okc = {"5079": {"ok": True, "url": rows[0]["url"], "maker_id": "mizuho",
                    "dmm_maker": "ミズホ"}}
    # ★★確かめられていないものは結ばない★★（2026-08-16・依頼213の指摘3）
    #   前は名前が出ただけで READY にしていた。機種ページの取得・機種名・
    #   導入日・メーカーのどれかで落ちても、**一度 READY になった状態は
    #   戻らない**ので、確かめていない控えが記事づくりの列に入っていた。
    ngc = {"5079": {"ok": False, "reason": "機種ページを確かめられません"}}
    t("★★確かめられなかった機種を『結び直した』ことにしない★★"
      "（メーカーや導入日を、確かめないまま控えへ書かない）",
      rebind_waiting(d, rows, ngc) == []
      and not d["items"][it["queue_id"]].get("maker"))
    t("★★ただし機種IDが取れたら記事づくりの列には入れる★★"
      "（待ち状態のまま置くと、60日打ち切りにもDMM未掲載の知らせにも"
      "当たらず永久に溜まる）",
      d["items"][it["queue_id"]]["state"] == _pm.READY
      and d["items"][it["queue_id"]]["source_machine_id"] == "5079")
    # ★確かめられた晩は、別の控えで見る★
    #   （上で READY になっているので、同じ控えはもう結び直しの対象外）
    d = _pm._empty()
    it = _pm.add(d, "スマスロ ラグナドール", "", "", "2026-11",
                 reason="DMMのカレンダーに無い", state=_pm.AWAITING_DMM_ID)
    got = rebind_waiting(d, rows, okc)
    t("★★DMMに載ったら、待っていた控えをそのまま使う★★"
      "（新しい控えを二重に作らない）",
      len(got) == 1 and len(d["items"]) == 1
      and d["items"][it["queue_id"]]["state"] == _pm.READY
      and d["items"][it["queue_id"]]["source_machine_id"] == "5079")
    t("　結び直したら導入日も日まで入る",
      d["items"][it["queue_id"]]["release"] == "2026-11-02")
    t("　確かめたメーカーも一緒に控える",
      d["items"][it["queue_id"]]["maker"] == "mizuho"
      and d["items"][it["queue_id"]]["dmm_maker"] == "ミズホ")
    # 2回目は何も起きない（もう待っていない）
    t("　同じものを二度結び直さない", rebind_waiting(d, rows, okc) == [])
    # 別機種は結び付かない
    d2 = _pm._empty()
    _pm.add(d2, "L聖闘士星矢 黄金十二宮", "", "", "2026-11",
            state=_pm.AWAITING_DMM_ID)
    t("★★別の機種には結び付けない★★（前方一致で寄せない）",
      rebind_waiting(d2, rows, okc) == [])

    # ★★確認に失敗した晩と、成功した晩を通しで見る★★
    #   （2026-08-16・依頼214の指摘3）
    #   失敗した晩に機種IDを結んでおかないと、そのあと add() が
    #   **同じ機種の控えをもう1件**作り、翌晩に元の控えも READY になって
    #   **同じ機種が2件**になる。
    d3 = _pm._empty()
    w3 = _pm.add(d3, "スマスロ ラグナドール", "", "", "2026-11",
                 reason="DMMのカレンダーに無い", state=_pm.AWAITING_DMM_ID)
    ng3 = {"5079": {"ok": False, "reason": "メーカーが名簿にありません"}}
    rebind_waiting(d3, rows, ng3)
    # 失敗した晩も、巡回は「待たせる」ぶんを控えへ入れる（run() と同じ道）
    _pm.add(d3, "スマスロ ラグナドール", rows[0]["url"], "", "",
            reason="メーカーが名簿にありません", source_machine_id="5079",
            identity_source="dmm")
    t("★★確認に失敗した晩に、控えが2件に増えない★★"
      "（機種IDを先に結んでおくから同じ控えが見つかる）",
      len(d3["items"]) == 1)
    # 翌晩、確かめられた
    rebind_waiting(d3, rows, okc)
    _pm.add(d3, "スマスロ ラグナドール", rows[0]["url"], "mizuho", "2026-11-02",
            reason="DMMのカレンダーから", source_machine_id="5079",
            identity_source="dmm")
    t("★★翌晩に確かめられても、控えは1件のまま★★",
      len(d3["items"]) == 1
      and d3["items"][w3["queue_id"]]["state"] == _pm.READY)
    # ★覚えた表示名は上書きしない★
    d4 = _pm._empty()
    w4 = _pm.add(d4, "スマスロ ラグナドール", "", "", "2026-11",
                 state=_pm.AWAITING_DMM_ID)
    rebind_waiting(d4, rows, okc)
    rebind_waiting(d4, rows, {"5079": dict(okc["5079"], dmm_maker="メーシー")})
    t("　覚えたメーカーの表示名は上書きしない（食い違いは残す）",
      d4["items"][w4["queue_id"]].get("dmm_maker") == "ミズホ")

    # ★★候補は「まだ記事が無いもの」だけ★★（保存したカレンダーで試す）
    import dmm_calendar as _dc
    p = os.path.join(BASE, "tests", "fixtures", "dmm_calendar_2026_09.html")
    if os.path.isfile(p):
        import io
        cal = _dc.parse(io.open(p, encoding="utf-8").read(), 2026, 9)
        import claim_identity as _ci
        known = {_ci.normalize_core(m.get("name") or "") for m in _machines()}
        fresh = [r for r in cal if _ci.normalize_core(r["name"]) not in known]
        t("★★もう記事がある機種は候補にしない★★"
          "（同じ機種を二度作らない）", len(fresh) < len(cal))
    else:
        t("★試験用の保存ページがありません（tests/fixtures）★", False)

    # ★★読めなかった月を「新台なし」にしない★★
    _keep = _dc.fetch
    try:
        _dc.fetch = lambda y, m: (_ for _ in ()).throw(
            _dc.CalendarError("わざと失敗"))
        _rows, _bad = candidates(today=datetime.date(2026, 9, 1),
                                 months_ahead=1)
        t("★★カレンダーを読めない月は問題として残す★★"
          "（黙って『新台なし』にしない）", _rows == [] and len(_bad) == 2)
    finally:
        _dc.fetch = _keep

    ng = sum(1 for _, o in results if not o)
    print()
    print("%d/%d 合格" % (len(results) - ng, len(results)))
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="DMMの新台カレンダーから新台を見つける")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if a.selftest:
        return selftest()
    got = run(apply_it=a.apply)
    print("見た候補: %d件 / 待ち行列へ: %d件 / 待たせる: %d件"
          % (got["looked"], len(got["queued"]), len(got["held"])))
    for q in got["queued"]:
        print("  入れました %-34s %s  %s" % (q["name"][:32], q["release"],
                                        q["url"]))
    for h in got["held"]:
        print("  もう一度みます %-30s %s" % (h["name"][:30], h["reason"][:86]))
    for r in got["rebound"]:
        print("  結び直し   %-8s %s" % (r["queue_id"], r["name"][:40]))
    for p in got["problems"]:
        print("  ★" + p[:150])
    if not a.apply:
        print(chr(10) + "★下見です（--apply で待ち行列に書きます）★")
    return 1 if got["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

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


def rebind_waiting(data: dict, rows: list) -> list:
    """★DMMに載るのを待っていた控えを、載った時点で結び直す★

    P-WORLD時代に見つけた機種が、DMMのカレンダーに遅れて載ることがある
    （実データ: 聖闘士星矢はまだ載っていない）。控えを消さずに待たせ、
    現れたら**同じ控え**へ機種IDを入れる（新しい控えを作らない）。

    ★結び付けるのは機種名の芯が完全に一致したときだけ★
      前方一致・似ている判定はしない（当サイトの鉄則）。
      決まらないものは二重に持ったまま人・2AIに見てもらう。
    """
    import pending_machines as _pm
    done = []
    for r in rows:
        for it in _pm.find_by_core(data, r["name"]):
            if it.get("state") != _pm.AWAITING_DMM_ID:
                continue
            it["state"] = _pm.READY
            it["identity_url"] = r["url"]
            it["identity_source"] = "dmm"
            it["source_machine_id"] = r["id"]
            it["release"] = r["release_date"]
            it["last_reason"] = "DMMのカレンダーに載ったので結び直しました"
            done.append(it)
    return done


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
    if apply_it:
        # ★待っていた控えを先に結び直す★（新しい控えを二重に作らないため）
        out["rebound"] = [{"queue_id": x["queue_id"], "name": x["name"]}
                          for x in rebind_waiting(data, rows)]
    for row in rows:
        got = check_one(row, index)
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
    got = rebind_waiting(d, rows)
    t("★★DMMに載ったら、待っていた控えをそのまま使う★★"
      "（新しい控えを二重に作らない）",
      len(got) == 1 and len(d["items"]) == 1
      and d["items"][it["queue_id"]]["state"] == _pm.READY
      and d["items"][it["queue_id"]]["source_machine_id"] == "5079")
    t("　結び直したら導入日も日まで入る",
      d["items"][it["queue_id"]]["release"] == "2026-11-02")
    # 2回目は何も起きない（もう待っていない）
    t("　同じものを二度結び直さない", rebind_waiting(d, rows) == [])
    # 別機種は結び付かない
    d2 = _pm._empty()
    _pm.add(d2, "L聖闘士星矢 黄金十二宮", "", "", "2026-11",
            state=_pm.AWAITING_DMM_ID)
    t("★★別の機種には結び付けない★★（前方一致で寄せない）",
      rebind_waiting(d2, rows) == [])

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
        print("  待たせます %-34s %s" % (h["name"][:32], h["reason"][:90]))
    for r in got["rebound"]:
        print("  結び直し   %-8s %s" % (r["queue_id"], r["name"][:40]))
    for p in got["problems"]:
        print("  ★" + p[:150])
    if not a.apply:
        print(chr(10) + "★下見です（--apply で待ち行列に書きます）★")
    return 1 if got["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

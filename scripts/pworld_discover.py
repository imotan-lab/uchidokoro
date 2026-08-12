# -*- coding: utf-8 -*-
"""pworld_discover.py — P-WORLDのカレンダーから待ち行列へ新台を入れる。

★入口を1本にする★（2026-08-12・運営者決定／正本＝
  _design/new_machine_discovery_2026-08-12.md）
  これまでは `new_machine_watch.discover()` がメーカー公式11社の一覧を見ていた。
  これからは **P-WORLDの導入カレンダー一本**。

やること
  ①カレンダーから機種を取る（`pworld_calendar`）
  ②記事がまだ無いものだけ残す（候補を出すだけ）
  ③機種ページで身元を確かめる（`pworld_machine`）
  ④確かめられたものを**待ち行列へ入れる**（以降は今までと同じ流れ）

★ここでも値は読み取らない★
  記事の中身（天井・機械割・純増…）は2AIが原文を読んで決める。

★決められないことは黙って飛ばさない★
  メーカー名が名簿に無い／身元が確かめられない場合は、
  理由を付けて待ち行列に残す（翌晩また試す）。

使い方:
  python scripts/pworld_discover.py --list        # 何が入るかを見るだけ
  python scripts/pworld_discover.py --apply       # 待ち行列へ入れる
  python scripts/pworld_discover.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAKER_CATALOG = os.path.join(BASE, "assets", "data", "maker-catalogs.json")


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


def candidates(machines: list | None = None, before: bool = False) -> list:
    """記事がまだ無い新台の候補（★身元はまだ確かめていない★）。"""
    import pworld_calendar as _cal
    rows = _cal.upcoming(before)
    if machines is None:
        machines = _cal._load_machines()
    return _cal.without_article(rows, machines)


def check_one(row: dict, index: dict) -> dict:
    """1機種ぶん、身元を確かめてメーカーIDまで決める。

    返すのは {"ok": bool, "reason": str, ...}。
    ★ok が真のときだけ待ち行列へ入れる★
    """
    import pworld_machine as _pm
    out = dict(row)
    mid = index.get(_norm(row.get("maker")))
    if not mid:
        out["ok"] = False
        out["reason"] = f"メーカーが名簿にありません: {row.get('maker')!r}"
        return out
    out["maker_id"] = mid
    got = _pm.verify(row["machine_id"], row["name"],
                     expect_maker=row.get("maker", ""),
                     expect_release=row.get("release_date", ""))
    if got.get("problems"):
        out["ok"] = False
        out["reason"] = "／".join(got["problems"])[:280]
        return out
    out["ok"] = True
    out["reason"] = ""
    for k in ("model_code", "shinsa", "type", "release"):
        if got.get(k):
            out[k] = got[k]
    return out


def run(apply_it: bool = False, before: bool = False) -> dict:
    """カレンダーを見て、確かめられた新台を待ち行列へ入れる。"""
    import pending_machines as _pend
    out = {"looked": 0, "queued": [], "held": [], "problems": []}
    try:
        rows = candidates(before=before)
    except Exception as e:                  # noqa: BLE001
        # ★読めなかったことを「新台なし」にしない★
        out["problems"].append(f"カレンダーを読めません: {type(e).__name__}: {e}")
        return out
    out["looked"] = len(rows)
    index = maker_index()
    data = _pend.load() if apply_it else None
    for row in rows:
        got = check_one(row, index)
        if not got["ok"]:
            # ★メーカー名も一緒に返す★（名簿に無い会社を知らせるため）
            out["held"].append({"name": row["name"], "reason": got["reason"],
                                "maker": row.get("maker", "")})
            # ★飛ばさずに待ち行列へ残す★（翌晩また試す）
            if apply_it:
                _pend.add(data, row["name"], row["url"], "", "",
                          reason=got["reason"])
            continue
        out["queued"].append({"name": row["name"], "url": row["url"],
                              "maker": got["maker_id"],
                              "release": got.get("release")
                              or row["release_date"]})
        if apply_it:
            _pend.add(data, row["name"], row["url"], got["maker_id"],
                      got.get("release") or row["release_date"],
                      reason="P-WORLDのカレンダーから")
    if apply_it:
        _pend.save(data)
    return out


# ---------------------------------------------------------------- selftest
def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    idx = maker_index()
    t("★★名簿の名前からメーカーIDが引ける★★",
      idx.get(_norm("北電子")) == "kitadenshi" and idx.get(_norm("サミー")) == "sammy")
    #   ★系列名は名簿に書いてあるものだけ★
    #   （「ユニバーサルブロス」「ミズホ」は名簿の directory_names に
    #     ユニバーサル系列として登録済み＝意図した対応づけ）
    t("　名簿に書いてある系列名は引ける",
      idx.get(_norm("ユニバーサルブロス")) == "universal")
    t("★★名簿に無い名前は、似ていても結び付けない★★（前方一致で拾わない）",
      idx.get(_norm("北電子ホールディングス")) is None
      and idx.get(_norm("サミー商事")) is None)
    t("　名簿に無い会社は引けない", idx.get(_norm("そんな会社")) is None)
    t("　書き方の違い（全角・記号）は吸収する",
      idx.get(_norm("北電子　")) == "kitadenshi")

    # ★同じ名前が2社にぶら下がっていたら決めない★
    import safe_json as _sj
    import tempfile
    tmp = os.path.join(tempfile.mkdtemp(), "m.json")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"schema_version": "maker-catalogs/v1", "catalogs": {
            "a": {"name": "かぶる社"}, "b": {"name": "かぶる社"}}}, f,
            ensure_ascii=False)
    t("★★同じ名前が2社にあるときは決めない★★",
      maker_index(tmp).get(_norm("かぶる社")) is None)

    # 身元が確かめられなければ待ち行列へ理由付きで残す
    class _FakePM:
        @staticmethod
        def verify(mid, name, expect_maker="", expect_release=""):
            return {"problems": ["機種名が一致しません（試験）"]}

    sys.modules["pworld_machine"] = _FakePM
    got = check_one({"machine_id": "1", "name": "x", "maker": "北電子",
                     "release_date": "2026-10-05",
                     "url": "https://www.p-world.co.jp/machine/database/1"}, idx)
    del sys.modules["pworld_machine"]
    t("★★身元が確かめられなければ入れない★★（理由は残す）",
      got["ok"] is False and "機種名が一致しません" in got["reason"])

    got2 = check_one({"machine_id": "1", "name": "x", "maker": "知らない社",
                      "release_date": "2026-10-05", "url": "u"}, idx)
    t("　メーカーが名簿に無ければ入れない",
      got2["ok"] is False and "名簿にありません" in got2["reason"])

    ng = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--before", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    got = run(apply_it=args.apply, before=args.before)
    print(f"カレンダーの候補: {got['looked']} 機種")
    for q in got["queued"]:
        print("  入れる: %s（%s・%s）" % (q["name"], q["maker"], q["release"]))
    for h in got["held"]:
        print("  待たせる: %s ← %s" % (h["name"], h["reason"][:90]))
    for p in got["problems"]:
        print("★問題★", p)
    return 1 if got["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())

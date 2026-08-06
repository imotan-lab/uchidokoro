#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""directory_coverage.py — 名鑑の「入口の登録漏れ」を見つける。

★なぜ要るか（2026-08-06）★
  ちょんぼりすたは登録済みの名鑑なのに、入口を『全機種一覧』と『ベルコ』の
  2つしか登録していなかった。実際には**49社ぶんの入口**があり、
  ユニバーサル等の新台は「載っているのに一覧に無い」扱いになっていた。
  ★登録漏れは、黙って「情報が無い」に化ける★のがいちばん危ない。
  人が気づけないので、機械が毎回数える。

★何を見るか★
  1. 名鑑の入口ページに並んでいる「別の入口へのリンク」を数える
  2. 名簿（directory-catalogs.json）に登録済みの入口と突き合わせる
  3. 登録されていない入口があれば知らせる（＝取りこぼしている可能性）

使い方:
    python scripts/directory_coverage.py            # 全名鑑を点検
    python scripts/directory_coverage.py --json     # 機械可読
    python scripts/directory_coverage.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import new_machine_watch as _w           # noqa: E402
import safe_json as _sj                  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOGS = os.path.join(BASE, "assets", "data", "directory-catalogs.json")


def surface_candidates(html: str, base_url: str, pattern: str) -> set:
    """入口ページから「別の入口」らしいリンクを集める。"""
    rx = re.compile(pattern)
    out = set()
    for href, _t in (_w._visible_anchor_pairs(html) or []):
        u = urllib.parse.urljoin(base_url, href).split("#")[0].split("?")[0]
        if not u.endswith("/"):
            u += "/"
        if rx.search(u):
            out.add(u)
    return out


def check(dir_id: str, conf: dict) -> dict:
    """1つの名鑑の入口が足りているか。"""
    out = {"directory": dir_id, "missing": [], "problems": [],
           "registered": len(conf.get("surfaces") or [])}
    pat = str(conf.get("surface_pattern") or "")
    if not pat:
        out["problems"].append("入口の形（surface_pattern）が登録されていません")
        return out
    have = set()
    for s in conf.get("surfaces") or []:
        u = str(s.get("url") or "")
        have.add(u if u.endswith("/") else u + "/")
    seen = set()
    for s in (conf.get("surfaces") or [])[:2]:   # 先頭の入口だけ読む（負担を抑える）
        try:
            html = _w._get(s["url"])
        except Exception as e:            # noqa: BLE001
            out["problems"].append(f"{s['url']}: 取得できません（{e}）")
            continue
        seen |= surface_candidates(html, s["url"], pat)
    out["found"] = len(seen)
    out["missing"] = sorted(seen - have)
    return out


def run(json_out: bool = False) -> int:
    cats = _sj.read_json(CATALOGS, expect=dict)["directories"]
    rows = [check(k, v) for k, v in cats.items() if v.get("status") == "ACTIVE"]
    if json_out:
        print(json.dumps(rows, ensure_ascii=False))
        return 1 if any(r["missing"] or r["problems"] for r in rows) else 0
    bad = 0
    for r in rows:
        mark = "❌" if (r["missing"] or r["problems"]) else "✅"
        print(f"{mark} {r['directory']}: 登録 {r['registered']} / "
              f"見つかった {r.get('found', 0)} / ★未登録 {len(r['missing'])}★")
        for p in r["problems"]:
            print("    -", p)
        for m in r["missing"][:5]:
            print("    未登録の入口:", m)
        bad += 1 if (r["missing"] or r["problems"]) else 0
    print(f"\n{len(rows) - bad}/{len(rows)} の名鑑が最新です")
    return 1 if bad else 0


def selftest() -> int:
    ok, ran = True, [0]

    def t(name, cond):
        nonlocal ok
        ran[0] += 1
        print(("✅ " if cond else "❌ ") + name)
        ok = ok and bool(cond)

    HTML = ('<a href="/slot/universal-slot/">ユニバーサル</a>'
            '<a href="/slot/belko-slot/">ベルコ</a>'
            '<a href="/slot/universal-slot/12345/">機種ページ</a>'
            '<a href="https://other.example/slot/x/">よそ</a>')
    got = surface_candidates(HTML, "https://chonborista.com/slot/",
                             r"^https://chonborista\.com/slot/[a-z0-9\-]+/$")
    t("★★入口の登録漏れを見つける★★（機種ページやよそのサイトは数えない）",
      got == {"https://chonborista.com/slot/universal-slot/",
              "https://chonborista.com/slot/belko-slot/"})
    conf = {"status": "ACTIVE", "surface_pattern": "x",
            "surfaces": [{"url": "https://a.example/1/"}]}
    r = check("t", {**conf, "surface_pattern": ""})
    t("★★入口の形が登録されていなければ知らせる★★",
      any("surface_pattern" in p for p in r["problems"]))
    print(f"\n{ran[0]}/{ran[0]} 合格" if ok else "\n不合格あり")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="名鑑の入口の登録漏れを点検")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    return run(a.json)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except _sj.SafeJsonError as e:
        print(f"★入力データが読めません: {e}★")
        raise SystemExit(1)

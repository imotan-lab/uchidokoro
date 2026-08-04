# -*- coding: utf-8 -*-
"""quarantine_machines.py — メーカー側の障害URLの別枠保留（隔離）。

★なぜ要るか（2026-08-04・Codex65回目の指摘）★
  初回登録メーカーの個別ページがSSL等で全滅すると、既存ラインナップの
  全URL（藤商事116件・オーイズミ29件）が「読めない＝明日やり直す」の救済で
  通常の待ち行列へ無差別流入し、本物の新台が最悪29晩後回しになった（台帳#210）。

  かといって行列に入れないだけだと、URLは基準(seen)として覚えたままなので
  **復旧後も二度と分類されない**＝一覧に既載だった未導入の新台
  （実例: L喰霊-零-Re）が誰にも気づかれず永久に沈む。

  そこで「通常の行列」でも「破棄」でもない第三の置き場＝隔離を用意する。
  - 隔離中は毎晩メーカーにつき1URLだけ復旧を確かめる（障害中サイトへの負荷を抑える）
  - 復旧したら隔離分を分類し直し、新台の範囲のものだけ通常の行列へ移す
  - 分類し直して古い機種と分かった分は、そのまま隔離から外す（seenには残る）

★覚えるのは事実だけ★
  URL・一覧カードの年月ヒント・いつ隔離したか・最後に確かめた日・確かめた回数。

使い方:
    python scripts/quarantine_machines.py list
    python scripts/quarantine_machines.py --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import safe_json as _sj                # noqa: E402

STORE = os.path.join(os.path.expanduser("~"), "Documents", "uchidokoro",
                     "add_machine_quarantine.json")
SCHEMA = "add-machine-quarantine/v1"


class QuarantineError(RuntimeError):
    pass


def _today() -> str:
    return date.today().isoformat()


def load() -> dict:
    """隔離簿を読む。★壊れていたら止まる（黙って空にしない）★"""
    if not os.path.exists(STORE):
        return {"schema": SCHEMA, "makers": {}}
    got = _sj.read_json(STORE, expect=dict)
    if got.get("schema") != SCHEMA:
        raise QuarantineError(f"隔離簿の形が違います: {got.get('schema')!r}")
    if not isinstance(got.get("makers"), dict):
        raise QuarantineError("隔離簿の中身が壊れています")
    return got


def save(data: dict) -> None:
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".new"
    with open(tmp, "w", encoding="utf-8", newline=chr(10)) as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write(chr(10))
    os.replace(tmp, STORE)              # ★書き換え中に壊れないように★


def add(data: dict, maker: str, urls: dict, reason: str) -> dict:
    """URL群を隔離する。urls は {url: 一覧カードの年月ヒント(無ければ空文字)}。"""
    m = data["makers"].setdefault(
        maker, {"since": _today(), "reason": str(reason)[:300], "urls": {}})
    for url, hint in urls.items():
        if url in m["urls"]:
            continue                    # ★既にある記録（日付・回数）を上書きしない★
        m["urls"][url] = {"hint": str(hint or ""), "first_seen": _today(),
                          "last_probe": "", "probes": 0}
    return data


def makers(data: dict) -> list:
    return sorted(data.get("makers") or {})


def urls_of(data: dict, maker: str) -> dict:
    return dict((data["makers"].get(maker) or {}).get("urls") or {})


def pick_probe(data: dict, maker: str, today: str = "") -> str:
    """今晩確かめる1URLを選ぶ（最後に確かめた日が最も古いもの・当日分は選ばない）。"""
    today = today or _today()
    m = data["makers"].get(maker)
    if not m or not m["urls"]:
        return ""
    order = sorted(m["urls"].items(),
                   key=lambda kv: (kv[1].get("last_probe") or "", kv[0]))
    url, rec = order[0]
    if rec.get("last_probe") == today:
        return ""                       # ★一晩に1回だけ★
    return url


def mark_probe(data: dict, maker: str, url: str, today: str = "") -> dict:
    """確かめた記録を残す（まだ読めなかった時）。"""
    rec = data["makers"].get(maker, {}).get("urls", {}).get(url)
    if rec is not None:
        rec["last_probe"] = today or _today()
        rec["probes"] = int(rec.get("probes") or 0) + 1
    return data


def remove_url(data: dict, maker: str, url: str) -> dict:
    """1件を隔離から外す（分類し直しが済んだ時）。メーカーが空になれば畳む。"""
    m = data["makers"].get(maker)
    if m and url in m.get("urls", {}):
        del m["urls"][url]
        if not m["urls"]:
            del data["makers"][maker]
    return data


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    import tempfile
    global STORE
    real = STORE
    ok_all = True
    ran = [0]

    def t(name, cond):
        nonlocal ok_all
        ran[0] += 1
        ok_all = ok_all and bool(cond)
        print(("✅" if cond else "❌") + " " + name)

    tmpdir = tempfile.mkdtemp(prefix="uchi_quar_")
    STORE = os.path.join(tmpdir, "q.json")
    try:
        d = load()
        t("★無ければ空の隔離簿から始まる★", d["makers"] == {})
        add(d, "m1", {"https://x/1": "2026-09", "https://x/2": ""}, "SSL全滅")
        save(d)
        d2 = load()
        t("★保存して読み直せる★", set(urls_of(d2, "m1")) == {"https://x/1", "https://x/2"})
        t("★ヒントを保持する★", d2["makers"]["m1"]["urls"]["https://x/1"]["hint"] == "2026-09")
        add(d2, "m1", {"https://x/1": "9999-99"}, "again")
        t("★再追加で既存の記録を上書きしない★",
          d2["makers"]["m1"]["urls"]["https://x/1"]["hint"] == "2026-09")
        u = pick_probe(d2, "m1", "2026-08-05")
        t("★確かめる1URLを選べる★", u in ("https://x/1", "https://x/2"))
        mark_probe(d2, "m1", u, "2026-08-05")
        u2 = pick_probe(d2, "m1", "2026-08-05")
        t("★同じ晩は別のURLを選ぶ（確かめ済みを選び直さない）★", u2 and u2 != u)
        mark_probe(d2, "m1", u2, "2026-08-05")
        t("★全URLが当日確認済みなら選ばない＝一晩の上限★",
          pick_probe(d2, "m1", "2026-08-05") == "")
        t("　翌日はまた選べる", pick_probe(d2, "m1", "2026-08-06") != "")
        remove_url(d2, "m1", "https://x/1")
        remove_url(d2, "m1", "https://x/2")
        t("★全部外れたらメーカーごと畳む★", makers(d2) == [])
        # 壊れた形は止まる
        with open(STORE, "w", encoding="utf-8") as f:
            f.write('{"schema": "other/v9", "makers": {}}')
        try:
            load()
            t("★形が違う隔離簿は読まない★", False)
        except QuarantineError:
            t("★形が違う隔離簿は読まない★", True)
    finally:
        STORE = real
        __import__("shutil").rmtree(tmpdir, ignore_errors=True)
    print(f"{ran[0]}/{ran[0]} 合格" if ok_all else "不合格あり")
    return 0 if ok_all else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="メーカー障害URLの隔離簿")
    ap.add_argument("cmd", nargs="?", choices=["list"], default="list")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    d = load()
    if not makers(d):
        print("隔離中のメーカーはありません")
        return 0
    for mid in makers(d):
        m = d["makers"][mid]
        print(f"{mid}: {len(m['urls'])}件 / {m['since']}から / {m['reason']}")
        for url, rec in sorted(m["urls"].items()):
            print(f"  {url} hint={rec['hint'] or '-'} "
                  f"最終確認={rec['last_probe'] or '未'} {rec['probes']}回")
    return 0


if __name__ == "__main__":
    sys.exit(main())

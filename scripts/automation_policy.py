# -*- coding: utf-8 -*-
"""automation_policy.py — ★機械が自動で通信してよい先を決める唯一の場所★

★なぜ要るのか（2026-08-16・台帳#376／Codex依頼213・214の助言）★
  P-WORLDと一撃へ、**規約を読まないまま毎晩アクセスしていた**。
  原因は「巡回先の設定（directory-catalogs.json）にURLを足すだけで
  通信できてしまう」ことで、規約の確認がどこにも要求されていなかった。

  そこで**通信してよい先を、規約の確認つきで1か所に集めた**。
  ここに `APPROVED` として載っていて、用途と道筋が合っているときだけ通す。

★blocked_hosts.py との違い★
  blocked_hosts は「絶対に通さない先」の**最後の砦**（黒い名簿）。
  こちらは「通してよい先」の**入口の名簿**（白い名簿）。
  ★黒が常に勝つ★＝ここで APPROVED でも、あちらで止めていれば止まる。

★載っていない先はどうなるか★
  `allows()` は偽を返す。呼び出し側が「通さない」を選べる。
  ★今すぐ全部の通信を白名簿にはしない★＝メーカー公式の巡回先が
  何十社もあり、一度に切り替えると新台が止まる。
  **新しく巡回先を足すときの関所**として先に置き、
  既存の巡回先は順に載せていく（載っていない先は監査が知らせる）。

使い方:
    python scripts/automation_policy.py --check
    python scripts/automation_policy.py --url https://p-town.dmm.com/machines/1
    python scripts/automation_policy.py --selftest
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys
import urllib.parse

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

POLICY = os.path.join(BASE, "assets", "data", "automation-policy.json")
SCHEMA = "automation-policy/v1"

APPROVED, PENDING, BLOCKED = "APPROVED", "PENDING", "BLOCKED"
STATUSES = (APPROVED, PENDING, BLOCKED)

# 1件に必ず要る欄（★足りなければ読まずに止める★）
REQUIRED = ("purpose", "path_prefixes", "method", "terms_url", "checked_at",
            "checked_by", "why", "evidence_ref", "recheck_by", "status")


class PolicyError(Exception):
    """名簿を読めない・壊れている（★迷ったら通さない★）。"""


def load(path: str | None = None) -> dict:
    import safe_json as _sj
    d = _sj.read_json(path or POLICY, expect=dict)
    if d.get("schema") != SCHEMA:
        raise PolicyError(f"通信の名簿の形が違います: {d.get('schema')!r}")
    hosts = d.get("hosts")
    if not isinstance(hosts, dict):
        raise PolicyError("通信の名簿の中身が壊れています")
    for h, c in hosts.items():
        if not isinstance(c, dict):
            raise PolicyError(f"通信の名簿の項目が壊れています: {h}")
        miss = [k for k in REQUIRED if k not in c]
        if miss:
            raise PolicyError(f"{h}: 欄が足りません: {miss}")
        if c["status"] not in STATUSES:
            raise PolicyError(f"{h}: 知らない状態です: {c['status']!r}")
        if c["status"] == APPROVED:
            for k in ("terms_url", "checked_at", "checked_by", "why",
                      "recheck_by"):
                if not str(c.get(k) or "").strip():
                    raise PolicyError(
                        f"{h}: 通してよいと書くなら {k} が要ります"
                        "／★規約を確かめずに通さない★")
            if not c["path_prefixes"] or not c["purpose"]:
                raise PolicyError(
                    f"{h}: 用途と道筋（path_prefixes）が要ります"
                    "／★サイト丸ごとを許可しない★")
    return d


def _host_of(url: str) -> str:
    return (urllib.parse.urlsplit(str(url or "")).hostname or "").lower()


def allows(url: str, purpose: str = "", today: str = "",
           policy: dict | None = None) -> tuple:
    """★このURLへ、この用途で通信してよいか★

    返すもの: (よい?, 理由)
    ★迷ったら通さない★＝載っていない・期限切れ・道筋違いは全部だめ。
    """
    import blocked_hosts as _bh
    # ★黒い名簿が常に勝つ★
    if _bh.is_blocked(url):
        return False, "★規約で禁止された先です（blocked_hosts）★"
    try:
        d = policy if policy is not None else load()
    except Exception as e:                # noqa: BLE001
        return False, f"通信の名簿を読めません: {str(e)[:100]}"
    host = _host_of(url)
    if not host:
        return False, f"URLからホストを読めません: {str(url)[:60]}"
    conf = d["hosts"].get(host)
    if conf is None:
        return False, (f"通信の名簿に載っていません: {host}"
                       "／★規約を確かめて automation-policy.json に"
                       "足してください★")
    if conf["status"] != APPROVED:
        return False, (f"{host} は {conf['status']} です: "
                       f"{str(conf.get('why') or '')[:120]}")
    today = today or datetime.date.today().isoformat()
    if str(conf.get("recheck_by") or "") < today:
        return False, (f"{host} の規約の確認期限を過ぎています"
                       f"（{conf.get('recheck_by')}）"
                       "／★読み直して日付を更新してください★")
    path = urllib.parse.urlsplit(str(url)).path or "/"
    if not any(path.startswith(p) for p in conf["path_prefixes"]):
        return False, (f"{host} で通してよい道筋ではありません"
                       f"（{path[:40]} / 許可: {conf['path_prefixes']}）")
    if purpose and purpose not in conf["purpose"]:
        return False, (f"{host} をこの用途では通しません"
                       f"（{purpose} / 許可: {conf['purpose']}）")
    return True, f"{host} は確認済みです（規約 {conf['checked_at']} 確認）"


def disagreements(policy: dict | None = None) -> list:
    """★名簿どうしの食い違い★（巡回先・出典・黒い名簿と突き合わせる）"""
    import blocked_hosts as _bh
    import safe_json as _sj
    ng = []
    try:
        d = policy if policy is not None else load()
    except Exception as e:                # noqa: BLE001
        return [f"通信の名簿を読めません: {str(e)[:120]}"]
    for h, c in d["hosts"].items():
        blocked = _bh.is_blocked("https://" + h + "/")
        if c["status"] == APPROVED and blocked:
            ng.append(f"{h}: 通してよいと書いてあるのに、"
                      "黒い名簿（blocked_hosts）が止めています")
        if c["status"] == BLOCKED and not blocked:
            ng.append(f"{h}: 通さないと書いてあるのに、"
                      "黒い名簿（blocked_hosts）に入っていません"
                      "／★最後の砦が効きません★")
    # 巡回先の設定に、名簿で通していない先が生きていないか
    p = os.path.join(BASE, "assets", "data", "directory-catalogs.json")
    if os.path.isfile(p):
        try:
            cats = _sj.read_json(p, expect=dict)
        except Exception as e:            # noqa: BLE001
            return ng + [f"巡回先の設定を読めません: {str(e)[:100]}"]
        for cid, conf in (cats.get("catalogs") or {}).items():
            if not isinstance(conf, dict) or conf.get("status") == "OFF_TOS":
                continue
            for key in ("list_url", "base_url", "url"):
                u = conf.get(key)
                if not u:
                    continue
                ok, why = allows(u, policy=d)
                if not ok and "載っていません" in why:
                    ng.append(f"巡回先 {cid} の {key} が通信の名簿に"
                              f"ありません: {_host_of(u)}"
                              "／★規約を確かめて載せてください★")
                break
    return ng


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅ " if cond else "❌ ") + name)

    d = load()
    t("★★通信の名簿を読める★★（欄がそろっている）", len(d["hosts"]) >= 3)
    ok, why = allows("https://p-town.dmm.com/machines/5049",
                     "machine_identity", "2026-08-16", d)
    t("★★確認済みの先は通る★★（DMMの機種ページ）", ok)
    ok, why = allows("https://www.p-world.co.jp/machine/database/10513",
                     "machine_identity", "2026-08-16", d)
    t("★★規約で禁止した先は通さない★★（P-WORLD）", not ok)
    ok, why = allows("https://1geki.jp/kaiseki/1", "", "2026-08-16", d)
    t("★★もう一方の禁止先も通さない★★（一撃）", not ok)
    ok, why = allows("https://shiranai.example/x", "", "2026-08-16", d)
    t("★★名簿に載っていない先は通さない★★"
      "（URLを足すだけで通信できたのが今回の原因）",
      not ok and "載っていません" in why)
    ok, why = allows("https://p-town.dmm.com/shops/1", "", "2026-08-16", d)
    t("★★同じサイトでも、許した道筋の外は通さない★★"
      "（新台の発見に許した先で、店舗情報を取りに行かない）", not ok)
    ok, why = allows("https://p-town.dmm.com/machines/1", "x_no_such",
                     "2026-08-16", d)
    t("★★許していない用途では通さない★★", not ok)
    ok, why = allows("https://p-town.dmm.com/machines/1", "", "2099-01-01", d)
    t("★★規約の確認期限を過ぎたら通さない★★（規約は書き換わる）",
      not ok and "期限" in why)

    def raises(fn, word=""):
        try:
            fn()
            return False
        except PolicyError as e:
            return (word in str(e)) if word else True

    import copy
    bad = copy.deepcopy(d)
    bad["hosts"]["p-town.dmm.com"].pop("why")
    t("　欄が足りなければ読まずに止める", raises(lambda: _check(bad)))
    bad2 = copy.deepcopy(d)
    bad2["hosts"]["p-town.dmm.com"]["recheck_by"] = ""
    t("★★通してよいと書くなら、確認の記録が要る★★"
      "（規約を確かめずに通さない）", raises(lambda: _check(bad2)))
    bad3 = copy.deepcopy(d)
    bad3["hosts"]["p-town.dmm.com"]["path_prefixes"] = []
    t("★★サイト丸ごとの許可はできない★★", raises(lambda: _check(bad3)))

    t("★★黒い名簿と食い違っていない★★（BLOCKED は必ず止まる）",
      not [x for x in disagreements(d) if "黒い名簿" in x])

    ng = sum(1 for _, o in results if not o)
    print()
    print("%d/%d 合格" % (len(results) - ng, len(results)))
    return 1 if ng else 0


def _check(d: dict) -> dict:
    """load() の検査だけをその場で掛ける（試験用）。"""
    import json
    import tempfile
    fd, p = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        return load(p)
    finally:
        os.unlink(p)


def main() -> int:
    ap = argparse.ArgumentParser(description="自動で通信してよい先の名簿")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--url")
    ap.add_argument("--purpose", default="")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if a.selftest:
        return selftest()
    if a.url:
        ok, why = allows(a.url, a.purpose)
        print(("通せます: " if ok else "★通しません★ ") + why)
        return 0 if ok else 1
    try:
        d = load()
    except PolicyError as e:
        print("★" + str(e) + "★")
        return 1
    ng = disagreements(d)
    for h, c in sorted(d["hosts"].items()):
        print("  %-9s %-22s 用途=%s 期限=%s"
              % (c["status"], h, ",".join(c["purpose"]) or "-",
                 c["recheck_by"] or "-"))
    if ng:
        print()
        print("★食い違い★")
        for x in ng:
            print("  -", x)
        return 1
    print()
    print("食い違いはありません")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

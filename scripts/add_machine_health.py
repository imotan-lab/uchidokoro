"""add_machine_health.py — 新台追加タスクが健全に動いたかを朝いちで点検する。

★なぜ要るか★
  無人で動くので、翌朝ログを開いて自分で読むのは手間だし見落とす。
  「見るべき5点」を機械が代わりに見て、要るときだけ知らせる。

★見る5点（台帳 #176）★
  ① 「開始★」と「終了★」が対で出ているか（片方だけならサイレント死）
  ② 各社の状態が OK 以外になっていないか（FETCH_FAILED / PARSE_SUSPECT）
  ③ 残存率が下がっていないか（一覧の作りが変わった兆候）
  ④ 「前回の公開が途中で終わっています」が出ていないか
  ⑤ 待ち行列が増え続けていないか（名鑑に載らないまま60日で台帳へ）

使い方:
    python scripts/add_machine_health.py             # 昨日ぶんを見る
    python scripts/add_machine_health.py --date 2026-08-01
    python scripts/add_machine_health.py --selftest
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

LOG_DIR = os.path.join(os.path.expanduser("~"), "Documents", "uchidokoro", "logs")

# 待ち行列がこの日数を超えたら知らせる（60日で台帳へ行く前の予告）
PENDING_WARN_DAYS = 30


def log_path(day: str) -> str:
    return os.path.join(LOG_DIR, f"add_machine_{day}.log")


def check_log(day: str) -> list:
    """その日のログを読んで、気になる点を返す。"""
    path = log_path(day)
    if not os.path.isfile(path):
        return [f"{day} のログがありません（タスクが動いていない可能性）"]
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    ng = []
    starts = text.count("★新台追加タスク 開始★")
    ends = text.count("★新台追加タスク 終了★")
    if starts == 0:
        ng.append(f"{day}: 開始の記録がありません")
    elif starts != ends:
        # ★片方だけ＝途中で黙って死んだ★（いちばん見つけにくい壊れ方）
        ng.append(f"{day}: 開始 {starts} 回に対して終了 {ends} 回"
                  "（途中で止まっています）")
    for m in re.finditer(r"見張り (\S+): 状態=(\S+)", text):
        if m.group(2) not in ("OK", "FIRST_TIME"):
            ng.append(f"{day}: {m.group(1)} が {m.group(2)}（一覧を読めていません）")
    for m in re.finditer(r"見張り (\S+):.*残存率=([0-9.]+)", text):
        try:
            if float(m.group(2)) < 1.0:
                ng.append(f"{day}: {m.group(1)} の残存率が {m.group(2)}"
                          "（一覧の作りが変わった兆候）")
        except ValueError:
            pass
    if "前回の公開が途中で終わっています" in text:
        # ★いま残っているかどうかで判断する★（2026-07-31・自分で気づいた）
        #   ログは「そのとき出た」記録なので、既に戻してあっても残る。
        #   ログだけで判定すると、解決済みの話を毎日蒸し返してしまう。
        import publish_new_machine as _pub
        if _pub.unfinished():
            ng.append(f"{day}: 公開が途中で終わっています"
                      "（--recover --apply で戻してください）")
    return ng


def check_pending() -> list:
    """待ち行列が長引いていないか。"""
    import pending_machines as _pend
    ng = []
    try:
        data = _pend.load()
    except Exception as e:                # noqa: BLE001
        return [f"待ち行列を読めません: {e}"]
    for it in _pend.due(data):
        days = _pend.waited_days(it)
        if days >= PENDING_WARN_DAYS:
            ng.append(f"{it['name']} が {days} 日待っています"
                      f"（{_pend.GIVE_UP_DAYS} 日で台帳へ）")
    return ng


def check_now() -> list:
    """いまのサイトの状態（目印・監査）。"""
    import publish_new_machine as _pub
    ng = []
    left = _pub.unfinished()
    if left:
        ng.append(f"公開が途中で終わっています（{left.get('slug')}）")
    ng += _pub.run_site_audit()
    return ng


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []
    nl = chr(10)
    import tempfile

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    global LOG_DIR
    real = LOG_DIR
    d = tempfile.mkdtemp(prefix="uchi_health_")
    try:
        LOG_DIR = d

        def write(day, body):
            with open(os.path.join(d, f"add_machine_{day}.log"), "w",
                      encoding="utf-8") as f:
                f.write(body)

        write("2026-08-01",
              "★新台追加タスク 開始★" + nl
              + "見張り bellco: 状態=OK 一覧=97件 新しいURL=0件 残存率=1.0" + nl
              + "★新台追加タスク 終了★" + nl)
        t("★ふつうに終わっていれば何も言わない★", check_log("2026-08-01") == [])

        write("2026-08-02", "★新台追加タスク 開始★" + nl + "見張り bellco: 状態=OK")
        t("★★開始だけで終了が無ければ気づく★★（いちばん見つけにくい壊れ方）",
          any("途中で止まって" in x for x in check_log("2026-08-02")))

        write("2026-08-03",
              "★新台追加タスク 開始★" + nl
              + "見張り sammy: 状態=PARSE_SUSPECT 一覧=3件" + nl
              + "★新台追加タスク 終了★")
        t("★一覧を読めていない社に気づく★",
          any("PARSE_SUSPECT" in x for x in check_log("2026-08-03")))

        write("2026-08-04",
              "★新台追加タスク 開始★" + nl
              + "見張り bellco: 状態=OK 一覧=90件 新しいURL=0件 残存率=0.85" + nl
              + "★新台追加タスク 終了★")
        t("★残存率が下がったら気づく★（一覧の作りが変わった兆候）",
          any("残存率" in x for x in check_log("2026-08-04")))

        # ★ログに記録が残っていても、いま解決済みなら言わない★
        write("2026-08-05",
              "★新台追加タスク 開始★" + nl
              + "★前回の公開が途中で終わっています（x / y）★" + nl
              + "★新台追加タスク 終了★")
        import publish_new_machine as _pub2
        t("★★ログに記録が残っていても、いま解決済みなら蒸し返さない★★"
          "（毎日同じことを言われると本当の異常が埋もれる）",
          bool(_pub2.unfinished()) == any("--recover" in x
                                          for x in check_log("2026-08-05")))

        t("　ログが無ければ「動いていない可能性」と言う",
          any("ログがありません" in x for x in check_log("2026-08-09")))
    finally:
        LOG_DIR = real
        __import__("shutil").rmtree(d, ignore_errors=True)

    t("　いまのサイトの状態も見られる", isinstance(check_now(), list))
    t("　待ち行列も見られる", isinstance(check_pending(), list))

    ng = [n for n, ok in results if not ok]
    print(f"{nl}{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="見る日（既定は昨日）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    day = args.date or (date.today() - timedelta(days=1)).isoformat()
    ng = check_log(day) + check_pending() + check_now()
    if not ng:
        print(f"✅ {day}: 気になる点はありません")
        return 0
    print(f"★{day}: 確かめてほしい点が {len(ng)} 件★")
    for x in ng:
        print("  ✗ " + x[:160])
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:                # noqa: BLE001
        print(f"★想定外の失敗 {type(e).__name__}: {e}★")
        raise SystemExit(1)

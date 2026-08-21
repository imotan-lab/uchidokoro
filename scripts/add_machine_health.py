"""add_machine_health.py — 新台追加タスクが健全に動いたかを朝いちで点検する。

★なぜ要るか★
  無人で動くので、翌朝ログを開いて自分で読むのは手間だし見落とす。
  「見るべき5点」を機械が代わりに見て、要るときだけ知らせる。

★見る6点（台帳 #176／⑥は2026-08-22追加）★
  ① 「開始★」と「終了★」が対で出ているか（片方だけならサイレント死）
  ② 各社の状態が OK 以外になっていないか（FETCH_FAILED / PARSE_SUSPECT）
  ③ 残存率が下がっていないか（一覧の作りが変わった兆候）
  ④ 「前回の公開が途中で終わっています」が出ていないか
  ⑤ 待ち行列が増え続けていないか（名鑑に載らないまま60日で台帳へ）
  ⑥ ★機種ページは分かっているのに、何度やっても記事にできていない機種★
     （2026-08-22追加。5日連続で公開0件だったのに誰も気づかなかったため）

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
# ★機種ページが分かっているのに作れていない回数のしきい値★（2026-08-22）
#   毎晩1回挑むので、7回＝1週間ぶん詰まっている状態。
#   ★実測（2026-08-22）★ q_0001=13回 / q_0004=20回 だった。
STUCK_TRIES = 7
# ★同じ理由で続けて止まった回数のしきい値★（2026-08-22・Codexの助言）
#   ★主監視はこちら★＝機種ごとに見るので、他の機種が公開されていても
#   「1機種だけ永久に止まっている」を見つけられる。
#   2回＝1回だと正常な未成立（名鑑にまだ載っていない等）が多すぎ、
#   3回だと夜間タスク3回ぶんを失って遅い。
BLOCKER_STREAK = 2
# ★このタスクを動かし始めた日★（それより前は「ログが無い」のが当たり前）
FIRST_RUN_DATE = "2026-07-31"


def log_path(day: str) -> str:
    return os.path.join(LOG_DIR, f"add_machine_{day}.log")


def check_log(day: str) -> list:
    """その日のログを読んで、気になる点を返す。"""
    if day < FIRST_RUN_DATE:
        return []          # ★動かし始める前のことは言わない★
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


def check_stuck() -> list:
    """★★何度やっても記事にできていない機種がないか★★（2026-08-22新設）

    ★なぜ要るか（実際に5日間気づかなかった）★
      新台タスクは 8/17〜8/21 の5日連続で **1本も公開していない**のに、
      毎日エラーなく完走していた（STATUS: COMPLETED_NO_CHANGE）。
      うちは「静かなのが正常」なのでメールも来ず、番人も異常と見なさない。
      ＝★「異常なし」と「何も作れていない」を区別できていなかった★。

    ★ログの文章を読み取らない★（2026-08-22の判断）
      「公開なし: 候補4件すべて…」のような文はいつでも書き換わる。
      そこを読み取ると、文言を直しただけで見張りが壊れる。
      ★代わりに、待ち行列がすでに持っている数を見る★＝
      `tries`（その機種に何回挑んだか）は毎晩機械が増やしている。

    ★待っているだけの機種と、詰まっている機種を分ける★
      AWAITING_DMM_ID … DMMのカレンダーにまだ載っていない
                        ＝**待つのが正常**（うちの都合ではない）
      READY           … 機種ページは分かっている
                        ＝**材料さえ採れれば作れるはず**なのに作れていない
      前者は check_pending（日数）が見る。ここが見るのは後者だけ。

    実測（2026-08-22）＝q_0001 は13回、q_0004 は20回試して未達だった。
    """
    import pending_machines as _pend
    ng = []
    try:
        data = _pend.load()
    except Exception as e:                # noqa: BLE001
        return [f"待ち行列を読めません: {e}"]
    items = data.get("items") or {}
    rows = list(items.values()) if isinstance(items, dict) else list(items)
    for it in rows:
        if not isinstance(it, dict):
            continue
        if str(it.get("state") or "") != "READY":
            continue          # まだカレンダーに載っていない＝待つのが正常
        # ★★主監視：同じ理由で続けて止まっていないか★★
        #   （2026-08-22・Codexの指摘）
        #   ★全体の「公開0件」だけを見ていると足りない★＝
        #   他の機種が毎日1件ずつ公開される裏で、1機種だけ永久に止まっていても
        #   見つからない。★機種ごとに、同じ理由が続いた回数を見る★。
        #   Nは2＝1回だと「名鑑にまだ載っていない」等の正常な未成立が多すぎ、
        #   3回だと夜間タスク3回ぶんを失って遅い（Codexの助言）。
        streak = int(it.get("blocker_streak") or 0)
        code = str(it.get("last_blocker") or "")
        if code and streak >= BLOCKER_STREAK:
            ng.append(
                f"{it.get('name')} が同じ理由で {streak} 回続けて止まっています"
                f"（理由: {code}／{it.get('identity_url')}）")
            continue          # 同じ機種を二重に挙げない

        # ★補助：理由が毎回変わっていても、長く作れていないなら知らせる★
        tries = int(it.get("tries") or 0)
        if tries >= STUCK_TRIES:
            ng.append(
                f"{it.get('name')} は機種ページが分かっているのに "
                f"{tries} 回作れていません（★材料が採れない理由を人が見てください★"
                f"／{it.get('identity_url')}）")
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
        t("★★動かし始める前の日については何も言わない★★"
          "（毎朝『ログがありません』と言われると本当の異常が埋もれる）",
          check_log("2026-07-01") == [])
    finally:
        LOG_DIR = real
        __import__("shutil").rmtree(d, ignore_errors=True)

    # ★★何度やっても作れていない機種を見つける★★（2026-08-22新設）
    #   ★これが無かったので、5日連続で公開0件でも誰も気づかなかった★
    #   （毎日エラーなく COMPLETED_NO_CHANGE で完走していた）
    import pending_machines as _pend_t
    _keep_load = _pend_t.load
    try:
        def _fake(items):
            _pend_t.load = lambda: {"schema": "x", "next_id": 9, "items": items}

        _fake({"q_1": {"name": "詰まっている機種", "state": "READY",
                       "tries": STUCK_TRIES,
                       "identity_url": "https://p-town.dmm.com/machines/1"}})
        _r = check_stuck()
        t("★★機種ページが分かっているのに作れていない機種を見つける★★"
          "（5日連続0件に誰も気づかなかった型）",
          len(_r) == 1 and "詰まっている機種" in _r[0] and str(STUCK_TRIES) in _r[0])

        _fake({"q_1": {"name": "あと1回", "state": "READY",
                       "tries": STUCK_TRIES - 1}})
        t("　しきい値の手前では知らせない", check_stuck() == [])

        # ★★主監視：同じ理由で続けて止まっている機種★★（2026-08-22）
        #   ★これが要る理由★＝全体の「公開0件」だけを見ていると、
        #   他の機種が毎日1件ずつ公開される裏で、1機種だけ永久に止まっていても
        #   見つからない（Codexの指摘）。
        _fake({"q_1": {"name": "同じ理由で止まる機種", "state": "READY",
                       "tries": 2, "last_blocker": "TAIL_CONFLICT",
                       "blocker_streak": BLOCKER_STREAK,
                       "identity_url": "https://p-town.dmm.com/machines/1"}})
        _r2 = check_stuck()
        t("★★同じ理由で2回続けて止まったら知らせる★★"
          "（★試した回数がまだ少なくても★）",
          len(_r2) == 1 and "TAIL_CONFLICT" in _r2[0]
          and "同じ理由で止まる機種" in _r2[0])

        _fake({"q_1": {"name": "1回だけ", "state": "READY", "tries": 1,
                       "last_blocker": "TAIL_CONFLICT", "blocker_streak": 1}})
        t("　1回だけなら知らせない（正常な未成立が多いため）", check_stuck() == [])

        _fake({"q_1": {"name": "毎回ちがう理由", "state": "READY", "tries": 3,
                       "last_blocker": "", "blocker_streak": 0}})
        t("　理由が続いていなければ、回数のほうで見る（まだしきい値未満）",
          check_stuck() == [])

        _fake({"q_1": {"name": "両方あてはまる", "state": "READY",
                       "tries": STUCK_TRIES, "last_blocker": "NO_MATERIAL",
                       "blocker_streak": BLOCKER_STREAK}})
        t("★同じ機種を二重に挙げない★", len(check_stuck()) == 1)

        _fake({"q_1": {"name": "カレンダー待ちで理由あり",
                       "state": "AWAITING_DMM_ID", "tries": 9,
                       "last_blocker": "NOT_ENOUGH_DIRECTORIES",
                       "blocker_streak": 9}})
        t("★★カレンダー待ちは理由が続いても知らせない★★"
          "（うちの都合ではない）", check_stuck() == [])

        _fake({"q_1": {"name": "カレンダー待ち", "state": "AWAITING_DMM_ID",
                       "tries": 99}})
        t("★★DMMのカレンダー待ちは何回でも知らせない★★"
          "（うちの都合ではないので、待つのが正常）", check_stuck() == [])

        _fake({})
        t("　待ち行列が空なら何も言わない", check_stuck() == [])

        _pend_t.load = lambda: (_ for _ in ()).throw(RuntimeError("読めません"))
        t("　待ち行列を読めないときは、そう言う（黙らない）",
          any("読めません" in x for x in check_stuck()))
    finally:
        _pend_t.load = _keep_load

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
    ng = check_log(day) + check_pending() + check_stuck() + check_now()
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

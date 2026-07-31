"""pending_machines.py — 見つけたが記事にできなかった新台を覚えておく。

★なぜ要るか（2026-07-31・実データで見つけた）★
  メーカー公式で新台を見つけても、名鑑サイトにはまだページが無いことがある。
  実例: 平和「L青春ブタ野郎はバニーガール先輩の夢を見ない」（2026年9月登場）
        → 名鑑2件のうち1件にしか載っておらず、材料を集められず止まった。

  ところが見つけたURLは「既知」として記録されるので、
  **翌日はもう『新台』に出てこない＝二度と処理されない**。
  早く見つけた機種ほど取りこぼすことになり、鮮度を上げる目的と正反対だった。

  そこで「見つけたが記事にできていない機種」をここに残し、毎日やり直す。

★覚えるのは事実だけ★
  機種名・公式URL・メーカー・登場年月・いつ見つけたか・何回試したか・直近の理由。
  数値や記事の中身は持たない（作り直すたびに出典から採り直す）。

★あきらめる時は必ず記録に残す★
  黙って消すと、誰も気づかないまま機種が抜ける。

使い方:
    python scripts/pending_machines.py list
    python scripts/pending_machines.py add --name ... --url ... --maker ... --release ...
    python scripts/pending_machines.py done --url ...
    python scripts/pending_machines.py --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import safe_json as _sj                # noqa: E402

STORE = os.path.join(os.path.expanduser("~"), "Documents", "uchidokoro",
                     "add_machine_pending.json")
SCHEMA = "add-machine-pending/v1"

# ★これ以上待っても載らないなら、人に見てもらう★
GIVE_UP_DAYS = 60


class PendingError(RuntimeError):
    pass


def _today() -> str:
    return date.today().isoformat()


def load() -> dict:
    """待ち行列を読む。★壊れていたら止まる（黙って空にしない）★"""
    if not os.path.exists(STORE):
        return {"schema": SCHEMA, "items": {}}
    got = _sj.read_json(STORE, expect=dict)
    if got.get("schema") != SCHEMA:
        raise PendingError(f"待ち行列の形が違います: {got.get('schema')!r}")
    if not isinstance(got.get("items"), dict):
        raise PendingError("待ち行列の中身が壊れています")
    return got


def save(data: dict) -> None:
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".new"
    with open(tmp, "w", encoding="utf-8", newline=chr(10)) as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write(chr(10))
    os.replace(tmp, STORE)              # ★書き換え中に壊れないように★


def add(data: dict, name: str, url: str, maker: str, release: str,
        reason: str = "") -> dict:
    """待ち行列に入れる（既にあれば試した回数と理由を更新する）。"""
    # ★名前が無くても覚える★（2026-07-31・Codex17回目）
    #   公式ページを読めなかったURLは名前が取れない。
    #   そこで拒否すると、**そのURLは既知になったまま二度と出てこない**＝機種が消える。
    #   名前は翌日もう一度公式を見て埋める（記事にできるかは別の所で見る）。
    if not url:
        raise PendingError("公式URLは必ず要ります")
    it = data["items"].get(url)
    if it:
        it["tries"] = int(it.get("tries", 0)) + 1
        it["last_try"] = _today()
        it["last_reason"] = reason[:300]
        # ★名前や登場年月が変わることがある（公式の書き換え）★
        it["name"], it["maker"], it["release"] = name, maker, release
    else:
        data["items"][url] = {
            "name": name, "url": url, "maker": maker, "release": release,
            "first_seen": _today(), "last_try": _today(), "tries": 1,
            "last_reason": reason[:300]}
    return data["items"][url]


def done(data: dict, url: str) -> bool:
    """記事にできたので外す。"""
    return data["items"].pop(url, None) is not None


def waited_days(item: dict, today: str = "") -> int:
    """見つけてから何日たったか。"""
    try:
        a = datetime.fromisoformat(item.get("first_seen") or "")
        b = datetime.fromisoformat(today or _today())
    except ValueError:
        return 0
    return (b - a).days


def give_up(data: dict, today: str = "") -> list:
    """★待ちすぎたものを外して返す（黙って消さず台帳へ）★

    ★一度も記事づくりを試していないものは打ち切らない★（Codex21回目）
      待ち行列の先頭が詰まっていると後ろは一度も試されない。
      試してもいないものを日数だけで捨てると、機種が黙って消える。
    """
    today = today or _today()
    out = []
    for url, it in list(data["items"].items()):
        if waited_days(it, today) < GIVE_UP_DAYS:
            continue
        if int(it.get("runs", 0)) < 1:
            continue                      # ★まだ一度も試していない★
        out.append(data["items"].pop(url))
    return out


def mark_tried(data: dict, url: str) -> None:
    """★実際に記事づくりを試したことを残す★（2026-07-31・Codex21回目）

    これが無いと、詰まっている先頭の数件だけを毎晩見続けて、
    **6件目以降は一度も試されないまま60日で打ち切られる**。
    """
    it = data["items"].get(url)
    if not it:
        return
    it["last_try"] = _today()
    it["runs"] = int(it.get("runs", 0)) + 1


def due(data: dict) -> list:
    """今日やり直すもの。★古いものから★（先に見つけたものを先に）"""
    return sorted(data["items"].values(),
                  key=lambda x: (x.get("first_seen") or "", x.get("url") or ""))


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []
    nl = chr(10)

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    d = {"schema": SCHEMA, "items": {}}
    add(d, "テスト機", "https://m.example/x/", "m", "2026-09", "名鑑にまだ無い")
    t("★見つけた機種を覚える★", len(d["items"]) == 1)
    t("　覚えるのは事実だけ（数値や記事は持たない）",
      set(d["items"]["https://m.example/x/"]) == {
          "name", "url", "maker", "release", "first_seen", "last_try",
          "tries", "last_reason"})
    add(d, "テスト機", "https://m.example/x/", "m", "2026-09", "まだ無い")
    t("★★同じ機種を二重に持たない（試した回数が増える）★★",
      len(d["items"]) == 1 and d["items"]["https://m.example/x/"]["tries"] == 2)
    add(d, "テスト機（改名）", "https://m.example/x/", "m", "2026-10")
    t("　公式が名前や登場月を書き換えたら追従する",
      d["items"]["https://m.example/x/"]["name"] == "テスト機（改名）"
      and d["items"]["https://m.example/x/"]["release"] == "2026-10")

    t("★記事にできたら外す★", done(d, "https://m.example/x/") and not d["items"])
    t("　無いものを外そうとしても壊れない", done(d, "https://m.example/none/") is False)

    d2 = {"schema": SCHEMA, "items": {}}
    add(d2, "古い機種", "https://m.example/old/", "m", "2026-01")
    d2["items"]["https://m.example/old/"]["first_seen"] = "2026-01-01"
    mark_tried(d2, "https://m.example/old/")      # ★一度は試している★
    add(d2, "新しい機種", "https://m.example/new/", "m", "2026-09")
    t("★★待ちすぎたものだけ取り出す★★（黙って消さない・台帳に残すため）",
      [x["name"] for x in give_up(d2, "2026-07-31")] == ["古い機種"]
      and len(d2["items"]) == 1)
    t("　まだ待てるものは残る", "https://m.example/new/" in d2["items"])

    d3 = {"schema": SCHEMA, "items": {}}
    add(d3, "あと", "https://m.example/b/", "m", "2026-09")
    d3["items"]["https://m.example/b/"]["first_seen"] = "2026-07-30"
    add(d3, "さき", "https://m.example/a/", "m", "2026-09")
    d3["items"]["https://m.example/a/"]["first_seen"] = "2026-07-01"
    d4 = {"schema": SCHEMA, "items": {}}
    add(d4, "一度も試していない機種", "https://m.example/never/", "m", "2026-01")
    d4["items"]["https://m.example/never/"]["first_seen"] = "2026-01-01"
    t("★★一度も記事づくりを試していないものは打ち切らない★★"
      "（先頭が詰まると後ろは一度も試されない・Codex21回目）",
      give_up(d4) == [] and "https://m.example/never/" in d4["items"])
    mark_tried(d4, "https://m.example/never/")
    t("　一度でも試したものは、待ちすぎたら取り出す",
      len(give_up(d4)) == 1)

    t("★先に見つけたものから試す★", [x["name"] for x in due(d3)] == ["さき", "あと"])

    t("★★名前が無くても覚える★★"
      "（読めなかったURLを拒否すると、既知のまま二度と出てこない・Codex17回目）",
      add({"items": {}}, "", "https://x/", "m", "2026-09")["url"] == "https://x/")
    t("　公式URLだけは必ず要る",
      _raises(lambda: add({"items": {}}, "X", "", "m", "2026-09")))
    t("★★形が違う待ち行列は読まずに止まる★★（黙って空にしない）",
      _raises(lambda: _check_schema({"schema": "べつのもの", "items": {}})))
    t("　中身が壊れていても止まる",
      _raises(lambda: _check_schema({"schema": SCHEMA, "items": []})))

    ng = [n for n, ok in results if not ok]
    print(f"{nl}{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def _check_schema(got: dict) -> None:
    if got.get("schema") != SCHEMA:
        raise PendingError("形が違います")
    if not isinstance(got.get("items"), dict):
        raise PendingError("中身が壊れています")


def _raises(fn) -> bool:
    try:
        fn()
    except Exception:                    # noqa: BLE001
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", choices=["list", "add", "done"])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--name", default="")
    ap.add_argument("--url", default="")
    ap.add_argument("--maker", default="")
    ap.add_argument("--release", default="")
    ap.add_argument("--reason", default="")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    data = load()
    if args.cmd == "add":
        add(data, args.name, args.url, args.maker, args.release, args.reason)
        save(data)
        print(f"待ち行列に入れました: {args.name}")
    elif args.cmd == "done":
        if done(data, args.url):
            save(data)
            print("外しました")
        else:
            print("待ち行列にありません")
    else:
        items = due(data)
        print(f"待っている新台: {len(items)} 件")
        for it in items:
            print(f"  {it['release']} {it['name'][:36]}"
                  f"（{waited_days(it)}日待ち・{it['tries']}回試した）")
            print(f"    理由: {it.get('last_reason', '')[:90]}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except (PendingError, _sj.SafeJsonError) as e:
        print(f"★待ち行列を扱えません: {e}★")
        raise SystemExit(1)
    except Exception as e:                # noqa: BLE001
        print(f"★想定外の失敗 {type(e).__name__}: {e}★")
        raise SystemExit(1)

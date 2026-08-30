# -*- coding: utf-8 -*-
"""GitHub の検査（Actions）が赤くなっていないかを見る。

★★運営者の判断（2026-08-30）★★
> 4 それで。

★何が起きていたか★＝GitHub の検査が赤いと**運営者にだけメールが届く**。
私（対話セッション）も無人タスクも知らないので、
★運営者が気づいて教えてくれるまで、赤いまま放置される★。
実際、2026-08-30 に私の push で赤くなり、運営者の指摘で初めて分かった。

★やること★＝いちばん新しい結果だけを見て、赤ければ知らせる。
  ・見るのは**各ワークフローの最新の完了ぶん**（過去の赤は追わない）
  ・★動いている途中は赤扱いにしない★（まだ結果が出ていないだけ）
  ・★配信（publish-pages）が赤いのは重い★＝読者にページが届いていない
  ・検査（pages-rehearsal）が赤いのは、公開物ではなく守りの問題

★通信は読み取りだけ★＝公開リポジトリなので認証も要らない。
★試験は通信しない★＝取ってくる処理を差し替えて試す。

使い方:
  python scripts/ci_status.py            # 人が見る
  python scripts/ci_status.py --json     # 機械が読む
  python scripts/ci_status.py --selftest

終了コード: 0=緑 / 3=赤いものがある / 1=見に行けなかった
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

API = ("https://api.github.com/repos/imotan-lab/uchidokoro"
       "/actions/runs?per_page=20&branch=main")

# ★重さの順★＝配信が赤いのは読者に届いていないということ
WEIGHT = {"publish-pages": "🔴 読者にページが届いていない可能性",
          "pages-rehearsal": "🟠 守りの検査が赤い（公開物は別）"}


def _fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "uchidokoro"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def latest_per_workflow(runs: list) -> list:
    """★各ワークフローの、いちばん新しい「終わったもの」★

    ★動いている途中は入れない★＝まだ結果が出ていないだけなので、
      赤扱いにすると毎回まちがって知らせることになる。
    """
    out, seen = [], set()
    for r in runs:
        if not isinstance(r, dict):
            continue
        name = str(r.get("name") or "")
        if not name or name in seen:
            continue
        if str(r.get("status") or "") != "completed":
            continue          # ★途中のものは飛ばす（次の回で見る）★
        seen.add(name)
        out.append(r)
    return out


def check(fetch=None) -> dict:
    """→ {"red": [...], "ok": [...], "why": ""}"""
    try:
        data = (fetch or _fetch)(API)
    except Exception as e:                                   # noqa: BLE001
        return {"red": [], "ok": [],
                "why": f"見に行けませんでした（{type(e).__name__}）"}
    runs = data.get("workflow_runs")
    if not isinstance(runs, list):
        return {"red": [], "ok": [], "why": "返事の形が違います"}
    red, ok = [], []
    for r in latest_per_workflow(runs):
        row = {"name": str(r.get("name") or ""),
               "sha": str(r.get("head_sha") or "")[:9],
               "at": str(r.get("updated_at") or ""),
               "url": str(r.get("html_url") or ""),
               "title": str((r.get("head_commit") or {}).get("message") or "")
               .splitlines()[:1]}
        if str(r.get("conclusion") or "") == "success":
            ok.append(row)
        else:
            row["conclusion"] = str(r.get("conclusion") or "")
            row["weight"] = WEIGHT.get(row["name"], "🟠 検査が赤い")
            red.append(row)
    return {"red": red, "ok": ok, "why": ""}


def main() -> int:
    ap = argparse.ArgumentParser(description="GitHub の検査が赤くないか")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()

    got = check()
    if a.json:
        print(json.dumps(got, ensure_ascii=False, indent=1))
    if got["why"]:
        if not a.json:
            print("★" + got["why"] + "★")
        return 1
    if not a.json:
        for r in got["ok"]:
            print(f"✅ {r['name']} ({r['sha']})")
        for r in got["red"]:
            print(f"{r['weight']}  {r['name']} = {r['conclusion']}"
                  f" ({r['sha']})")
            print("   " + r["url"])
    return 3 if got["red"] else 0


def selftest() -> int:
    ng, ran = [], [0]

    def t(name, cond):
        ran[0] += 1
        print(("✅ " if cond else "❌ ") + name)
        if not cond:
            ng.append(name)

    def fake(runs):
        return lambda _u: {"workflow_runs": runs}

    def run(name, sha, status, concl):
        return {"name": name, "head_sha": sha, "status": status,
                "conclusion": concl, "updated_at": "2026-08-30T00:00:00Z",
                "html_url": "https://example.invalid",
                "head_commit": {"message": "x"}}

    t("★全部成功なら緑★",
      check(fake([run("publish-pages", "a" * 40, "completed", "success"),
                  run("pages-rehearsal", "a" * 40, "completed",
                      "success")]))["red"] == [])
    got = check(fake([run("pages-rehearsal", "a" * 40, "completed",
                          "failure")]))
    t("★赤いものは拾う★", len(got["red"]) == 1)
    t("　配信が赤いほうが重いと分かる",
      "🔴" in check(fake([run("publish-pages", "a" * 40, "completed",
                             "failure")]))["red"][0]["weight"])
    t("★★動いている途中は赤扱いにしない★★"
      "（まだ結果が出ていないだけ・毎回まちがって知らせない）",
      check(fake([run("pages-rehearsal", "a" * 40, "in_progress",
                      None)]))["red"] == [])
    t("★★見るのは各ワークフローのいちばん新しい終わったものだけ★★"
      "（前の赤を引きずらない）",
      check(fake([run("pages-rehearsal", "b" * 40, "completed", "success"),
                  run("pages-rehearsal", "a" * 40, "completed",
                      "failure")]))["red"] == [])
    t("★見に行けなければ、赤ではなく「分からない」と言う★",
      check(lambda _u: (_ for _ in ()).throw(OSError("x")))["why"] != "")
    t("　返事の形が違っても落ちない",
      check(lambda _u: {"x": 1})["why"] != "")

    print(f"\n{ran[0] - len(ng)}/{ran[0]} " + ("合格" if not ng else "不合格"))
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


if __name__ == "__main__":
    raise SystemExit(main())

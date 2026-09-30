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
import re
import subprocess
import sys
import urllib.request

# ★★自分の出力の文字の扱いを固定する★★（2026-08-31・自分で踏んだ）
#   Windowsの既定（cp932）では結果の行に含まれる ✅ が書けず、
#   **UnicodeEncodeError で落ちて終了コードが 1 になる**
#   ＝番人から見ると「見に行けなかった」。
#   ★緑でも赤でも毎回そうなる★ので、この見張りは一度も働かない。
#   ★罠⑪と同じ型★（ci_repro / mutation_check / pre_push_check は対策済み）。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:              # noqa: BLE001
        pass                       # ★書けなくても見に行くことは続ける★

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ★★公開中のコミットを名指しして聞く★★（2026-09-28・実際に古い緑を出していた）
#   ★直す前は「main の検査を新しい順に20件」（`?branch=main`）を読み、
#     先頭から拾っていた★。ところがこの一覧は**古いまま返ることがある**
#   （実測＝2026-09-28 20:30、先頭が9月8日の分で、その日の分が1件も入っていなかった。
#     同じ時刻にコミットを名指しして聞くと、最新の結果がすぐ返った）。
#   ＝★今日の検査が赤でも、20日前の緑を「いまの結果」として出していた★。
#   2本の検査はどちらも main への push のたびに必ず動く（道筋の絞り込みなし）ので、
#   名指しすれば必ず結果がある。
API_SHA = ("https://api.github.com/repos/imotan-lab/uchidokoro"
           "/actions/runs?per_page=20&head_sha={sha}")

# ★見張る検査★（どちらも main への push のたびに必ず動く）
EXPECTED = ("publish-pages", "pages-rehearsal")

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
    ★取り消されたものも入れない★（2026-09-09）＝
      続けてpushすると、古いコミットの検査は「新しい方に追い越された」と
      して取り消される。これは★結果が出ていない★のであって失敗ではない。
      赤扱いにすると番人が翌朝🟠で知らせ、★本物の赤がその中に埋もれる★。
      飛ばして、その1つ前の結果を見る（何も無ければ「分からない」）。
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
        if str(r.get("conclusion") or "") == "cancelled":
            continue          # ★追い越されただけ＝結果が出ていない★
        seen.add(name)
        out.append(r)
    return out


def published_sha() -> str:
    """★いま main に載っているコミット★（このリポジトリの origin/main）

    無人タスクも対話セッションもこのリポジトリから push するので、
    origin/main は公開中のコミットを指している。
    ★読めなければ空を返す★＝呼ぶ側が「分からない」と言う（緑にしない）。
    """
    try:
        r = subprocess.run(["git", "rev-parse", "origin/main"],
                           cwd=BASE, capture_output=True, text=True,
                           timeout=30)
        s = (r.stdout or "").strip()
        return s if r.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", s) \
            else ""
    except Exception:                                        # noqa: BLE001
        return ""


def check(fetch=None, sha=None) -> dict:
    """→ {"red": [...], "ok": [...], "pending": [...], "why": ""}

    ★見るのは公開中のコミット1つだけ★（`sha` を渡さなければ origin/main）。
    """
    sha = published_sha() if sha is None else str(sha or "")
    if not sha:
        return {"red": [], "ok": [], "pending": [],
                "why": "公開中のコミットが分かりません（git が読めません）"}
    try:
        data = (fetch or _fetch)(API_SHA.format(sha=sha))
    except Exception as e:                                   # noqa: BLE001
        return {"red": [], "ok": [], "pending": [],
                "why": f"見に行けませんでした（{type(e).__name__}）"}
    runs = data.get("workflow_runs")
    if not isinstance(runs, list):
        return {"red": [], "ok": [], "pending": [],
                "why": "返事の形が違います"}
    # ★★名指ししたコミットの結果だけを見る★★＝返事に別のコミットが
    #   混ざっていても拾わない（★古いコミットの緑を、いまの結果にしない★）
    runs = [r for r in runs if isinstance(r, dict)
            and str(r.get("head_sha") or "") == sha]
    # ★★見張る2本が両方そろって初めて判定する★★（2026-09-30・Codexの指摘）
    #   ★結果が1件も無い場合もここで「分からない」になる★（push直後はまだ出ていない）
    #   ★直す前は「返事に出てきた名前」だけを見ていた★ので、片方が丸ごと
    #   欠けても（起動されなかった・一覧から漏れた）残る1本が緑なら緑だった。
    #   2本ともどのpushでも必ず動く（道筋の絞り込みなし）ので、欠けは「分からない」。
    _seen = {str(r.get("name") or "") for r in runs}
    _missing = [n for n in EXPECTED if n not in _seen]
    if _missing:
        return {"red": [], "ok": [], "pending": [],
                "why": f"公開中のコミット {sha[:9]} の検査が見つかりません: "
                       + "・".join(_missing)}
    red, ok = [], []
    _latest = latest_per_workflow(runs)
    _done = {str(r.get("name") or "") for r in _latest}
    # ★動いている途中のものは赤でも緑でもない★（まだ結果が出ていないだけ）
    pending = sorted({str(r.get("name") or "") for r in runs
                      if r.get("name") and str(r.get("name")) not in _done})
    for r in _latest:
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
    return {"red": red, "ok": ok, "pending": pending, "why": "",
            "sha": sha[:9]}


def _cancel_tests(t) -> None:
    """★追い越されて取り消された検査は、赤にしない★（2026-09-09）

    ★続けてpushすると必ず起きる★ので、赤扱いにすると番人が毎朝🟠を出し、
    ★本物の赤がその中に埋もれる★。
    """
    def _run(name, sha, status, concl):
        return {"name": name, "head_sha": sha, "status": status,
                "conclusion": concl, "updated_at": "2026-09-09T00:00:00Z",
                "html_url": "https://example.invalid/x", "head_commit": {}}

    _runs = [_run("pages-rehearsal", "bbbbbbbbb", "completed", "cancelled"),
             _run("pages-rehearsal", "aaaaaaaaa", "completed", "success")]
    _got = latest_per_workflow(_runs)
    t("★★追い越されて取り消された検査は、結果として数えない★★"
      "（★赤にすると毎回知らせることになり、本物の赤が埋もれる★）",
      len(_got) == 1 and _got[0]["head_sha"] == "aaaaaaaaa")
    t("　（対照）本当に失敗したものは、いちばん新しいものを見る",
      latest_per_workflow(
          [_run("pages-rehearsal", "bbbbbbbbb", "completed", "failure"),
           _run("pages-rehearsal", "aaaaaaaaa", "completed", "success")]
      )[0]["head_sha"] == "bbbbbbbbb")
    t("　取り消しか途中しか無ければ、何も返さない（分からない）",
      latest_per_workflow(
          [_run("pages-rehearsal", "bbbbbbbbb", "completed", "cancelled"),
           _run("pages-rehearsal", "aaaaaaaaa", "in_progress", None)]) == [])


def main() -> int:
    ap = argparse.ArgumentParser(description="GitHub の検査が赤くないか")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--render-check", action="store_true",
                    help=argparse.SUPPRESS)   # ★試験専用★
    a = ap.parse_args()
    if a.render_check:
        return _render_check()
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
        for name in got.get("pending") or []:
            print(f"⏳ {name} ({got.get('sha', '')}) 動いている途中")
        for r in got["red"]:
            print(f"{r['weight']}  {r['name']} = {r['conclusion']}"
                  f" ({r['sha']})")
            print("   " + r["url"])
    return 3 if got["red"] else 0


# ★試験のためだけの入口★（通信しない・記号を書いて終わるだけ）
#   ★これがあるおかげで、子を cp932 で起こして本当に試せる★
def _render_check() -> int:
    print("✅ 🔴 🟠 ❌ ⏳")
    return 0


def selftest() -> int:
    ng, ran = [], [0]

    def t(name, cond):
        ran[0] += 1
        print(("✅ " if cond else "❌ ") + name)
        if not cond:
            ng.append(name)

    _cancel_tests(t)

    def fake(runs):
        return lambda _u: {"workflow_runs": runs}

    def run(name, sha, status, concl):
        return {"name": name, "head_sha": sha, "status": status,
                "conclusion": concl, "updated_at": "2026-08-30T00:00:00Z",
                "html_url": "https://example.invalid",
                "head_commit": {"message": "x"}}

    # ★★子を cp932 で起こして、記号を書けるか見る★★（2026-08-31）
    #   ★`sys.stdout.encoding` を見るだけでは試験にならない★＝
    #   呼び出し側が PYTHONIOENCODING=utf-8 を渡していれば、
    #   守りを外しても通ってしまう（実際に外して通った）。
    import subprocess as _sp
    _env = dict(os.environ, PYTHONIOENCODING="cp932")
    _env.pop("PYTHONUTF8", None)
    _r = _sp.run([sys.executable, os.path.abspath(__file__), "--render-check"],
                 capture_output=True, env=_env)
    # ★★終了コードだけでは足りない★★（2026-08-31・Codexの指摘）
    #   `cp932 + errors="replace"` に変えても例外は出ず終了コードは0になる
    #   （記号が「?」になるだけ）＝試験の名前どおりの保証になっていない。
    #   → ★出てきた生バイトが utf-8 そのものであること★まで見る。
    t("★★自分の出力を utf-8 に固定している★★"
      "（Windowsの既定のままだと合格の記号が書けず、"
      "緑でも赤でも毎回「見に行けなかった」になる）",
      _r.returncode == 0
      and _r.stdout.replace(b"\r\n", b"\n")
      == "✅ 🔴 🟠 ❌ ⏳\n".encode("utf-8"))
    A, B = "a" * 40, "b" * 40
    # ★見張る2本のうち、試験で触らないほうを緑でそろえる★（両方そろわないと判定しない）
    PUB_OK = run("publish-pages", A, "completed", "success")
    REH_OK = run("pages-rehearsal", A, "completed", "success")
    _all_ok = check(fake([PUB_OK, REH_OK]), sha=A)
    t("★全部成功なら緑★",
      _all_ok["red"] == [] and _all_ok["why"] == ""
      and len(_all_ok["ok"]) == 2)
    got = check(fake([PUB_OK, run("pages-rehearsal", A, "completed",
                                  "failure")]), sha=A)
    t("★赤いものは拾う★", len(got["red"]) == 1)
    t("　配信が赤いほうが重いと分かる",
      "🔴" in check(fake([run("publish-pages", A, "completed", "failure"),
                         REH_OK]), sha=A)["red"][0]["weight"])
    # ★★見張る2本の片方が丸ごと欠けたら、緑ではなく「分からない」★★
    #   （2026-09-30・Codexの指摘＝直す前は残る1本が緑なら緑だった）
    for _only, _gone in ((PUB_OK, "pages-rehearsal"),
                         (REH_OK, "publish-pages")):
        _half = check(fake([_only]), sha=A)
        t(f"★★{_gone} が返事に無ければ、緑にせず「分からない」★★",
          _half["ok"] == [] and _gone in _half["why"])
    # ★★問い合わせは公開中のコミットを名指しする★★（2026-09-30・Codexの指摘）
    #   ★偽物がURLを捨てていたので、`branch=main` に戻しても緑だった★
    _urls = []

    def _rec(u):
        _urls.append(u)
        return {"workflow_runs": [PUB_OK, REH_OK]}
    check(_rec, sha=A)
    t("★★問い合わせのURLが、公開中のコミットを名指ししている★★",
      len(_urls) == 1 and ("head_sha=" + A) in _urls[0]
      and "branch=" not in _urls[0])
    _pend = check(fake([PUB_OK,
                        run("pages-rehearsal", A, "in_progress", None)]),
                  sha=A)
    t("★★動いている途中は赤扱いにしない★★"
      "（まだ結果が出ていないだけ・毎回まちがって知らせない）",
      _pend["red"] == [] and _pend["pending"] == ["pages-rehearsal"])
    # ★★古いコミットの緑を、いまの結果にしない★★（2026-09-28・実際に起きた）
    #   ★直す前★＝一覧が古いまま返り、20日前の緑を出していた。
    #   ★返事に別のコミットしか無ければ「分からない」★（緑にしない）。
    _stale = check(fake([run("publish-pages", B, "completed", "success"),
                         run("pages-rehearsal", B, "completed",
                             "success")]), sha=A)
    t("★★古いコミットの緑を、いまの結果として出さない★★"
      "（★一覧が古いまま返ると、今日の赤に気づけなかった★）",
      _stale["ok"] == [] and _stale["why"] != "")
    t("★★公開中のコミットの赤は、別のコミットの緑に埋もれない★★",
      len(check(fake([run("pages-rehearsal", B, "completed", "success"),
                      PUB_OK,
                      run("pages-rehearsal", A, "completed", "failure")]),
                sha=A)["red"]) == 1)
    t("★公開中のコミットが分からなければ、緑ではなく「分からない」★",
      check(fake([run("publish-pages", A, "completed", "success")]),
            sha="")["why"] != "")
    # ★★本番の入口が origin/main を名指ししている★★（罠③＝配線の綱）
    _keep_ps = globals()["published_sha"]
    try:
        globals()["published_sha"] = lambda: A
        _wired = check(fake([PUB_OK, run("pages-rehearsal", A, "completed",
                                         "failure")]))
        t("★★何も渡さなければ、公開中のコミット（origin/main）を見る★★",
          len(_wired["red"]) == 1 and _wired.get("sha") == A[:9])
    finally:
        globals()["published_sha"] = _keep_ps
    t("★見に行けなければ、赤ではなく「分からない」と言う★",
      check(lambda _u: (_ for _ in ()).throw(OSError("x")),
            sha=A)["why"] != "")
    t("　返事の形が違っても落ちない",
      check(lambda _u: {"x": 1}, sha=A)["why"] != "")

    print(f"\n{ran[0] - len(ng)}/{ran[0]} " + ("合格" if not ng else "不合格"))
    if ng:
        # ★ASCIIだけの目印も出す★（2026-08-31）＝
        #   文字の扱いを壊す試験では、子の出力が cp932 になって
        #   日本語が化ける。化けても残る目印が無いと、
        #   壊し方の道具が「ただ落ちただけ」と読み違える。
        print("NG " + str(len(ng)))
        print("失敗:", ng)
    return 1 if ng else 0


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""pre_push_check.py — push の前に、忘れがちな検査を機械的に流す。

★なぜ要るか（2026-08-04）★
  記事データ（machine-details / machines.json）を触ると、文言の変化で
  台帳（ledger.json）のALLOWが外れ、その機種が公開対象から外れる。
  そのままpushすると **リハーサルCIが赤くなり、運営者にエラーメールが届く**。
  2026-07-31に6通、2026-08-04に4通。★2回とも「次から流す」と決めた直後に再発★
  なので、人の記憶ではなく**機械で止める**。

★何をするか★
  直前のコミット群で記事データが変わっていれば、
    - crosscheck_gates.py（公開ゲートと独立監査の突き合わせ）
    - audit_site.py（サイト構造監査）
  を流し、どちらかがNGなら**pushを止める**（終了コード1）。

使い方（gitのフックから呼ぶ）:
    python scripts/pre_push_check.py            # 変更の有無を自分で見て判断
    python scripts/pre_push_check.py --always   # 変更に関係なく流す
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import glob
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import local_paths as _lp        # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# この形のファイルが変わっていたら、記事データを触ったとみなす
WATCH = ("assets/data/machine-details/", "assets/data/machines.json")


def _changed_paths() -> list:
    """push対象（origin/main..HEAD）で変わったファイル。取れなければ空。"""
    for rng in ("origin/main..HEAD", "HEAD~1..HEAD"):
        r = subprocess.run(["git", "diff", "--name-only", rng], cwd=BASE,
                           capture_output=True, text=True, encoding="utf-8")
        if r.returncode == 0:
            return [x.strip() for x in r.stdout.splitlines() if x.strip()]
    return []


def _warn_unreported() -> None:
    """Codexへ未報告のスクリプト変更があれば、pushの直前に知らせる。
    ★止めない★（当日中にpushする鉄則があるため）。目に入れるのが目的。
    """
    state = _lp.doc("last_codex_report.json")
    try:
        with open(state, encoding="utf-8") as fh:
            last = json.load(fh).get("commit")
    except Exception:                      # noqa: BLE001
        return
    if not last:
        return
    r = subprocess.run(["git", "log", "--oneline", f"{last}..HEAD", "--", "scripts/"],
                       cwd=BASE, capture_output=True)
    if r.returncode != 0:
        return
    lines = [x for x in r.stdout.decode("utf-8", "replace").splitlines() if x.strip()]
    if not lines:
        return
    print()
    print("★★Codexへ未報告のスクリプト変更が %d 件あります★★" % len(lines))
    for ln in lines[:5]:
        print("   " + ln[:100])
    print("   → 実コードを見せて報告し、"
          "python scripts/codex_reported.py --receipt <領収書> を実行してください")
    print("   （鉄則1b: 作ったら報告する。報告は運営者に言われる前にやる）")
    print()


def _verified_range() -> list:
    """★無人タスクが直したコミットが、照合を通っているか★（2026-08-21・Codex依頼249）

    ★なぜ要るのか★
      `task_guard verify-commit`（関所が見た内容と実際のコミットの突き合わせ）を
      作ったが、**push の側が見ていなかった**。
      ・verify-commit を省いても push できた
      ・先に未pushのコミットが積まれていると、最新1件だけ照合しても
        それ以前が一緒に push された

    ここでは「無人タスクが作ったコミット」だけを対象にする
    （対話セッションの手作業まで止めると、鉄則4「当日中にpush」が守れない）。

    戻り値: 止めるべき理由の一覧（空なら通してよい）
    """
    state = _lp.doc("task_guard.json")
    try:
        with open(state, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:                      # noqa: BLE001
        return []                          # 記録が無い＝無人タスクは動いていない
    # ★対象はその日の無人タスクだけ★（2026-08-21）
    #   全期間の記録を見ると、対話セッションが手で作ったコミットまで
    #   「照合していない」と止めてしまい、鉄則4（当日中にpush）が守れない。
    today = datetime.now().strftime("%Y-%m-%d")
    verified, active = set(), False
    # ★★その日ぶんの照合済みは、機種を替えても消えない★★
    #   （2026-08-21・Codexの指摘3）
    #   タスクごとの verified_commit は機種を替えると捨てられるので、
    #   ★3機種ぶんをためて最後にまとめて push すると、
    #     1・2機種目が「照合していない」ことになって止まった★。
    _day = (data.get("day") or {})
    if str(_day.get("date") or "") == today:
        for c in (_day.get("verified_commits") or []):
            verified.add(str(c))
    for name, e in (data.get("tasks") or {}).items():
        if not isinstance(e, dict) or e.get("run_date") != today:
            continue
        if not e.get("guard_slug"):
            continue
        active = True              # 今日、関所を通った機種がある
        if e.get("verified_commit"):
            verified.add(str(e["verified_commit"]))
    if not active:
        return []                  # 今日は無人タスクが機種を触っていない
    # いま push しようとしているコミットのうち、無人タスクが作ったもの
    r = subprocess.run(["git", "log", "--format=%H %s", "origin/main..HEAD"],
                       cwd=BASE, capture_output=True)
    if r.returncode != 0:
        return []
    out = []
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        sha, _, subject = line.partition(" ")
        if sha not in verified:
            out.append(f"{sha[:12]} {subject[:60]}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="push前の検査")
    ap.add_argument("--always", action="store_true")
    a = ap.parse_args()

    # ★Codexへの未報告をpushの直前に必ず目に入れる★（2026-08-09）
    #   「作ったら報告する」は鉄則1bにもメモリにも書いてあるのに、
    #   この日また言われるまで動けなかった。覚え直すのではなく、
    #   **忘れようのない場所（pushの瞬間）に出す**。
    #   ★止めはしない★＝当日中のpushは別の鉄則（未pushで残すと夜の公開が止まる）。
    _warn_unreported()

    # ★★照合を通っていないコミットは push させない★★（2026-08-21・Codex依頼249）
    #   verify-commit を作っても、push側が見ていなければ素通りできた。
    #   ★対象は「無人タスクが照合の記録を残している場合」だけ★
    #   （対話セッションの手作業まで止めると、当日中にpushする鉄則が守れない）
    unverified = _verified_range()
    if unverified:
        print()
        print("★★関所の照合を通っていないコミットがあります★★")
        for x in unverified[:5]:
            print("   " + x)
        print("   → python scripts/task_guard.py verify-commit "
              "--task <タスク> --slug <機種> --commit <コミット>")
        print("   （関所が見た内容と、実際にpushする内容が同じかを確かめてください）")
        print()
        return 1

    changed = _changed_paths()
    hit = [p for p in changed if any(p.startswith(w) or p == w for w in WATCH)]
    if not a.always and not hit:
        print("pre-push: 記事データの変更なし（検査は流しません）")
        return 0
    if hit:
        print(f"pre-push: 記事データの変更 {len(hit)} 件 → 検査を流します")

    # ★★古い読み込み結果を無効にする★★（2026-08-27・2回踏んだ）
    #   Pythonは「元の日時（秒）と大きさ」だけで作り直すかを決めるので、
    #   ★大きさが変わらない直しを同じ秒のうちに書くと、古い結果が使われる★
    #   ＝直したのに直っていない状態のまま、検査が緑になる。
    #   ★中身は1文字も変えない★（日時だけ）。
    for _f in glob.glob(os.path.join(BASE, "scripts", "*.py")):
        try:
            os.utime(_f, None)
        except OSError:
            pass                       # ★触れなくても検査は続ける★
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    ng = []
    for name, cmd in (("crosscheck_gates", ["crosscheck_gates.py"]),
                      ("audit_site", ["audit_site.py"])):
        r = subprocess.run([sys.executable, os.path.join(BASE, "scripts", *cmd)],
                           cwd=BASE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env)
        tail = "\n".join((r.stdout or "").strip().splitlines()[-6:])
        print(f"--- {name} (exit={r.returncode}) ---")
        print(tail)
        if r.returncode != 0:
            ng.append(name)
    # ★★壊し方の目印が消えていないか★★（2026-08-27・実際に2回やった）
    #   ★試験は走らせない★＝目印の文字が実在するか数えるだけ（一瞬で終わる）。
    #   `ci_repro` の赤を読まずに push して、CIを落としたのがこの型。
    _mut = subprocess.run(
        [sys.executable, os.path.join(BASE, "scripts", "mutation_check.py"),
         "--selftest"],
        cwd=BASE, capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env)
    if _mut.returncode != 0:
        print()
        print("★★壊し方の目印が消えています★★（CIが必ず落ちます）")
        for _l in (_mut.stdout or "").splitlines():
            if _l.startswith("❌") or "合格" in _l:
                print("   " + _l)
        print("   → 直した場所に合わせて mutation_check.py の目印を直してください")
        ng.append("壊し方の目印")
    # ★★触ったスクリプトの守りを、実際に壊して試す★★（2026-08-28）
    #   ★きっかけ★＝守りを手前に足したせいで奥の守りの試験が消え、
    #   それを見落として push し、★GitHubのCIが実際に赤くなった★。
    #   道具は正しく「★NG」と出していたのに、こちらが読み違えた。
    #   ★人の注意では止まらないので、機械に止めさせる★。
    #   ★全部回すと数分かかる★ので、**触ったファイルの分だけ**回す。
    _touched = sorted({p for p in _changed_paths()
                       if p.startswith("scripts/") and p.endswith(".py")})
    if _touched:
        print()
        print("--- 壊し方（触ったスクリプトの分だけ） ---")
        print("   対象: " + " / ".join(_touched))
        _mf = subprocess.run(
            [sys.executable, os.path.join(BASE, "scripts", "mutation_check.py"),
             "--files", ",".join(_touched)],
            cwd=BASE, capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env)
        for _l in (_mf.stdout or "").splitlines():
            if _l.startswith("  ★ND") or _l.startswith("  ★NG") \
                    or "守られていません" in _l or "試したものは" in _l \
                    or "試験はありません" in _l:
                print("   " + _l.strip())
        if _mf.returncode != 0:
            print("★★守りを壊しても、試験が赤くなりません★★"
                  "（その守りを見ている試験がありません）")
            print("   → 手前に別の守りを足したせいで、"
                  "奥の守りを一度も試さなくなっていないか確かめてください")
            ng.append("壊し方（触った分）")
    if ng:
        print()
        print("★pushを止めました★ 失敗: " + " / ".join(ng))
        print("  記事の文言を変えると台帳のALLOWが外れ、公開対象から外れます。")
        print("  想定値を直すか文言を戻すかを判断してから、もう一度 push してください。")
        return 1
    print("pre-push: 検査OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

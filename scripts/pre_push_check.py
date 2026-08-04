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
import os
import subprocess
import sys

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


def main() -> int:
    ap = argparse.ArgumentParser(description="push前の検査")
    ap.add_argument("--always", action="store_true")
    a = ap.parse_args()

    changed = _changed_paths()
    hit = [p for p in changed if any(p.startswith(w) or p == w for w in WATCH)]
    if not a.always and not hit:
        print("pre-push: 記事データの変更なし（検査は流しません）")
        return 0
    if hit:
        print(f"pre-push: 記事データの変更 {len(hit)} 件 → 検査を流します")

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

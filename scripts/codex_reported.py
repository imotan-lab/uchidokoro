"""codex_reported.py — 「ここまでCodexへ報告した」と記録する。

★なぜ要るか（2026-07-31）★
  「作ったらCodexへ報告する」を3か所に書いたが3回とも守れなかった。
  そこで audit_site の項目31が「未報告のスクリプト変更がたまっていないか」を
  毎回見る。報告したら、このコマンドでその時点を記録して数え直す。

使い方:
    python scripts/codex_reported.py                    # 今のHEADまで報告済みにする
    python scripts/codex_reported.py --show             # いまの記録を見る
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = r"C:/Users/imao_/Documents/uchidokoro/last_codex_report.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--note", default="", help="何を報告したかの覚え書き")
    a = ap.parse_args()
    if a.show:
        if os.path.isfile(STATE):
            print(open(STATE, encoding="utf-8").read())
        else:
            print("まだ記録がありません")
        return 0
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=BASE, text=True,
                          capture_output=True).stdout.strip()
    if not head:
        print("★HEADを取れません（gitリポジトリですか）★")
        return 1
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8", newline=chr(10)) as f:
        json.dump({"commit": head, "at": datetime.now().isoformat(timespec="seconds"),
                   "note": a.note}, f, ensure_ascii=False, indent=1)
    print(f"ここまで報告済みとして記録しました: {head[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""codex_reported.py — 「ここまでCodexへ報告した」と記録する。

★なぜ要るか（2026-07-31）★
  「作ったらCodexへ報告する」を3か所に書いたが3回とも守れなかった。
  そこで audit_site の項目31が「未報告のスクリプト変更がたまっていないか」を
  毎回見る。報告したら、このコマンドでその時点を記録して数え直す。

★なぜ領収書が要るのか（2026-08-09・依頼126）★
  以前は**引数なしで実行しさえすれば印が付いた**。
  2026-08-08、無人タスクが台帳の文章に
    「`python scripts/codex_reported.py` を実行する必要がある」
  と書いたところ、バッククォートをシェルが**コマンド置換として実行**し、
  Codexを一度も呼んでいないのに印が付いた（未報告9件が緑になった）。

  Codexの指摘: 応答ファイルの中身を見るだけでは証明にならない
  （古い応答・別依頼の応答・手で作ったファイルでも通る）。

  ★そこで、Codexを実際に呼んだときにだけ出る領収書を消費する形にした★
    ・領収書が無ければ印は付かない（事故で走っても何も起きない）
    ・印を付けるのは**領収書に書かれたレビュー対象のコミット**であって、
      実行時のHEADではない（レビュー後に進めた分まで報告済みにしない）
    ・一度使った領収書は二度使えない

使い方:
    python scripts/codex_reported.py --receipt <領収書のパス> --note "何を報告したか"
    python scripts/codex_reported.py --show             # いまの記録を見る
    python scripts/codex_receipt.py list                # 領収書を並べる
"""
from __future__ import annotations
import argparse, json, os, sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import codex_receipt as _cr          # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import os as _os_lp                 # noqa: E402
import sys as _sys_lp               # noqa: E402
_sys_lp.path.insert(0, _os_lp.path.dirname(_os_lp.path.abspath(__file__)))
import local_paths as _lp           # noqa: E402
STATE = _lp.doc("last_codex_report.json")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                    # noqa: BLE001
    pass


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Codexへ報告した時点を記録する（領収書が要ります）")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--receipt", default="",
                    help="codex_review.sh が発行した領収書のパス")
    ap.add_argument("--note", default="", help="何を報告したかの覚え書き")
    a = ap.parse_args()

    if a.show:
        if os.path.isfile(STATE):
            print(open(STATE, encoding="utf-8").read())
        else:
            print("まだ記録がありません")
        return 0

    if not a.receipt:
        # ★引数なしでは絶対に印を付けない★（今回の事故の再発防止）
        print("★領収書が要ります★ Codexを実際に呼んだときに発行された"
              " 領収書のパスを --receipt で渡してください。")
        print("  一覧: python scripts/codex_receipt.py list")
        return 2

    if "\n" in a.note or "\r" in a.note or any(
            ord(c) < 32 and c not in "\t" for c in a.note):
        print("★--note に改行や制御文字は入れられません★")
        return 2

    try:
        rec = _cr.consume(a.receipt)
    except _cr.ReceiptError as e:
        print("★" + str(e) + "★ 印は付けません。")
        return 1

    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8", newline=chr(10)) as f:
        json.dump({
            "commit": rec["reviewed_commit"],
            "at": datetime.now().isoformat(timespec="seconds"),
            "note": a.note,
            # ★何を根拠に印を付けたか★（後から追える）
            "receipt": os.path.abspath(a.receipt),
            "receipt_run_id": rec.get("run_id"),
            "response": rec.get("response_path"),
            "response_sha256": rec.get("response_sha256"),
            "scripts_tree": rec.get("scripts_tree"),
        }, f, ensure_ascii=False, indent=1)
    print("ここまで報告済みとして記録しました: %s（領収書 %s）"
          % (rec["reviewed_commit"][:12], rec.get("run_id")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

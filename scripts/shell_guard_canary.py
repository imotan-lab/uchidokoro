# -*- coding: utf-8 -*-
"""シェル差し込みガードの回帰テスト（カナリア）。

★なぜリポジトリに置くか（2026-08-09・依頼128）★
  最初は一時フォルダに置いていたため、**あとから誰も再実行できなかった**。
  この見張りは「文章を書いただけでコマンドが実行される」事故（2026-08-08）への
  対策なので、抜け道が見つかるたびにここへ足して、二度と戻らないようにする。

見張り本体は `（Claudeの設定フォルダ）/shell_guard.py`（リポジトリ外）。
PreToolUse（matcher: Bash）に登録してある。

使い方:
    python scripts/shell_guard_canary.py
"""
from __future__ import annotations

import json
import subprocess
import sys

import os as _os_lp                 # noqa: E402
import sys as _sys_lp               # noqa: E402
_sys_lp.path.insert(0, _os_lp.path.dirname(_os_lp.path.abspath(__file__)))
import local_paths as _lp           # noqa: E402
GUARD = _lp.claude("shell_guard.py")
BQ = chr(96)                 # バッククォート
SUB = chr(36) + "("          # コマンド置換の始まり
DQ = chr(34)
NL = chr(10)
Q = chr(39)
SH = "bash"
EV = "eval"

# (説明, コマンド, 止めるべきか)
CASES = [
    # ---- 2026-08-08 の事故そのもの ----
    ("★台帳にコマンド名を飾りで書く（事故の再現）★",
     'python scripts/open_issues.py add --detail "' + BQ
     + 'python scripts/codex_reported.py' + BQ + ' を実行する"', True),

    # ---- 依頼126で挙がったカナリア ----
    ("バッククォート", 'echo "' + BQ + 'canary' + BQ + '"', True),
    ("コマンド置換", 'echo "' + SUB + 'canary)"', True),
    ("入れ子の算術", 'echo "' + chr(36) + '(( ' + SUB + 'canary) ))"', True),
    ("外部サイトの機種名にバッククォート",
     'python scripts/check_duplicate.py --name "スマスロ' + BQ + 'canary'
     + BQ + '北斗"', True),
    ("URLにコマンド置換",
     'python scripts/machine_sources.py --check --slug x --url '
     '"https://a.example/' + SUB + 'canary)"', True),
    ("メール件名",
     'python send_notify.py notify --subject "NG ' + BQ + 'canary' + BQ
     + ' 件"', True),
    ("コミット文", 'git commit -m "fix: ' + BQ + 'canary' + BQ + '"', True),
    ("ログ1行",
     'python log.py "update_machine_2026-08-09" "STEP1 ' + BQ + 'canary'
     + BQ + '"', True),

    # ---- 依頼127で実際にすり抜けた5件 ----
    ("★すり抜け1: コメント行でヒアドキュメントに見せかける★",
     "# <<" + Q + "SAFE" + Q + NL + SUB + "canary)" + NL + "SAFE", True),
    ("★すり抜け2: 引用ありと引用なしのヒアドキュメントを混ぜる★",
     "cat <<EOF <<" + Q + "SAFE" + Q + NL + SUB + "canary)" + NL + "EOF"
     + NL + "ignored" + NL + "SAFE", True),
    ("★すり抜け3: dateという関数を先に作って日付の形を使う★",
     "date(){ canary; }; echo " + SUB + "date +%Y-%m-%d)", True),
    ("★すり抜け4: プロセス置換★", "diff <(canary) b.txt", True),
    ("★すり抜け5: 日付の形（例外を廃止）★",
     'python log.py "auto_add_' + SUB + 'date +%Y-%m-%d)" "STEP 1開始"', True),
    ("ヒアドキュメントの中身も止める",
     "python - <<" + Q + "PY" + Q + NL + "print(" + Q + SUB + "canary)" + Q
     + ")" + NL + "PY", True),

    # ---- 依頼128で指摘された二段目のシェル ----
    ("★二段目: ファイルの中身を bash -c に渡す★",
     "read -r p < external.txt; bash -c " + DQ + chr(36) + "p" + DQ, True),
    ("★二段目: sh -c★", "sh -c " + DQ + chr(36) + "p" + DQ, True),
    ("★二段目: eval★", "eval " + chr(36) + "p", True),
    ("★二段目: source★", "source ./external.sh", True),

    # ---- 依頼129で見つかった二段目のすり抜け ----
    ("★すり抜け: 先頭に空白の " + EV + "★", " " + EV + " " + DQ + "$p" + DQ, True),
    ("★すり抜け: command " + EV + "★", "command " + EV + " " + DQ + "$p" + DQ, True),
    ("★すり抜け: /bin/" + SH + " -c★", "/bin/" + SH + " -c " + DQ + "$p" + DQ, True),
    ("★すり抜け: " + SH + " -lc★", SH + " -lc " + DQ + "$p" + DQ, True),
    ("★すり抜け: " + SH + " --noprofile -c★",
     SH + " --noprofile -c " + DQ + "$p" + DQ, True),
    ("★すり抜け: dash -c★", "dash -c " + DQ + "$p" + DQ, True),
    ("★すり抜け: here-string★", SH + " <<< " + DQ + "$p" + DQ, True),
    ("★すり抜け: シェルへ流し込む★", SH + " < external.txt", True),

    # ---- 通ってよいもの（誤って止めない）----
    ("　普通のコマンドは通る", "python scripts/audit_site.py", False),
    ("　二重引用符の日本語も通る",
     'python scripts/open_issues.py add --title "天井が採れません"', False),
    ("　シングルクォート内の文章も通る",
     "python scripts/open_issues.py add --title " + Q + "天井が採れません" + Q,
     False),
    ("　日付を実値で書けば通る", 'python log.py "auto_add_2026-08-09" "開始"',
     False),
    ("　記号を含まないヒアドキュメントは通る",
     "python - <<" + Q + "PY" + Q + NL + "print(1)" + NL + "PY", False),
    ("　パイプや&&も通る", "cd /tmp && python x.py | head -5", False),
    ("　セミコロン区切りも通る", "echo a; echo b", False),
    ("　bash でスクリプトを走らせるのは通る（-c ではない）",
     "bash scripts/codex_with_lock.sh ctx.json a b", False),
    ("　ファイル名に source を含んでも通る",
     "python scripts/x.py --file source_registry.json", False),
    ("　文章の中の source は通る（誤検知を減らした）",
     'git commit -m ' + DQ + 'docs: x; source を確認' + DQ, False),
]

# (説明, 入力そのもの) ＝ どれも止めるべき
BROKEN_INPUT = [
    ("入力がJSONでない", "これはJSONではない"),
    ("commandキーが無い", json.dumps({"tool_name": "Bash", "tool_input": {}})),
    ("tool_inputが無い", json.dumps({"tool_name": "Bash"})),
    ("入力が空", ""),
    ("commandが空文字", json.dumps({"tool_name": "Bash",
                                    "tool_input": {"command": ""}})),
]


def ask(payload: str):
    p = subprocess.run([sys.executable, GUARD], input=payload, text=True,
                       capture_output=True, encoding="utf-8")
    # ★見張り自身が落ちていたら合格にしない★（2026-08-09・依頼129）
    #   終了コードを見ていなかったので、出力が無いまま落ちても
    #   「通してよいもの」は合格に見えていた。
    if p.returncode != 0:
        return None, "見張りが異常終了しました（終了コード %d）%s" % (
            p.returncode, (p.stderr or "")[:120])
    try:
        out = json.loads(p.stdout or "{}")
    except Exception:                      # noqa: BLE001
        return None, p.stdout
    blocked = out.get("decision") == "block"
    # ★新旧どちらの書き方でも「止める」と言えているか★
    hso = (out.get("hookSpecificOutput") or {}).get("permissionDecision")
    if blocked and hso != "deny":
        return None, "hookSpecificOutput が deny になっていません"
    return blocked, ""


def main() -> int:
    ng = 0
    for name, cmd, want in CASES:
        blocked, why = ask(json.dumps({"tool_name": "Bash",
                                       "tool_input": {"command": cmd}}))
        ok = (blocked == want)
        if not ok:
            ng += 1
        print(("✅" if ok else "❌") + " " + name
              + ("  → 止めた" if blocked else "  → 通した")
              + (("  " + why) if why else ""))
    for name, payload in BROKEN_INPUT:
        blocked, why = ask(payload)
        ok = bool(blocked)
        if not ok:
            ng += 1
        print(("✅" if ok else "❌") + " ★入力がおかしいときも通さない★: " + name
              + (("  " + why) if why else ""))

    total = len(CASES) + len(BROKEN_INPUT)
    print()
    print("%d/%d 合格" % (total - ng, total))
    if ng:
        print("★この見張りは事故を止めるためのもので、悪意への境界ではありません★")
        print("  本当の境界は自由文をファイル渡し・引数配列で渡すこと。")
    return 1 if ng else 0


if __name__ == "__main__":
    raise SystemExit(main())

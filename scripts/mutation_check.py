# -*- coding: utf-8 -*-
"""mutation_check.py — ★守りをわざと壊して、試験が赤くなるか確かめる★

★なぜ要るか（2026-08-23・Codexの再レビュー指摘5）★
  2026-08-23の一日で、私は★「自分で作った材料で採点する試験」を4回★書いた。
  どれも「試験が通った」を根拠に完成と報告しかけ、
  ★毎回Codexか対照実験が止めた★。

  実例:
    ・text_kept の試験が LEAD_TEMPLATE と比べていた（テンプレを変えると両辺が動く）
    ・待ち行列の形を手で真似ていた（本物の鍵が変わっても気づかない）
    ・page_decision の試験材料が必ず basis を持っていた（黒名簿の危険が出ない）
    ・通し試験の材料も手作りで、抽出器の保存漏れを検出できない

  ★人の注意では止まらない★ので、機械が毎回試す。

★やること★
  守りの1行を壊した写しを作り、**その試験が赤くなること**を求める。
  赤くならなければ「その守りは試験で守られていない」＝★NG★。

★★作業ツリーは触らない★★
  一時ディレクトリへ写してから壊す。元のファイルは読むだけ。

使い方:
    python scripts/mutation_check.py            # 全部試す
    python scripts/mutation_check.py --list     # 何を壊すかだけ見る
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

# ★自分の出力も utf-8 で★（親が cp932 だと理由を出す所で落ちる）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                            # noqa: BLE001
    pass

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ★壊し方の一覧★（Codexが挙げた6つ＋自分で踏んだ分）
#   file … 壊すファイル / before → after / run … 赤くなるべき試験
# ★★外した壊し方（理由を残す）★★
#   2026-08-26、運営者の指示で**記事にサイト名を出さない**ことにした
#   （「ほかサイトのコピーと思われたくない」）。
#   これにより `strip_allowed_basis`（サイト名入りの名乗りを監査17の
#   対象から外す仕組み）は、外すものが無くなった＝**壊しても何も起きない**。
#   ・「根拠の名乗りも他サイト名として弾く」（Codex16回目）
#   ・「recheck が監査17と別の見方をする」（Codex17回目）
#   ★仕組み自体は残している★＝将来また名乗りを付けるときに要る。
#   ★同じ壊し方を戻さないこと★＝いまの取り決めでは必ず「捕まえられない」
#   と出て、本物の見落としが埋もれる。
MUTATIONS = [
    # ─── 2026-09-01・Codexのレビュー29で塞いだ穴 ─────────────
    {
        "why": "★票の数え方の見張りを黙らせる"
               "（独立2出典の 2 を自前で数える場所が見逃され、土台が崩れる）★",
        "file": "scripts/audit_site.py",
        "before": "    import ast\n    out = []\n    try:\n        tree = ast.parse(src)",
        "after": "    import ast\n    return []\n    try:\n        tree = ast.parse(src)",
        "run": ["scripts/audit_site.py"],
    },
    {
        "why": "★日をまたいだ回まで「大丈夫」にする"
               "（本物の途中死を見逃す＝いちばん見つけにくい壊れ方）★",
        "file": "scripts/add_machine_health.py",
        "before": ("    head = t.split(START_MARK, 1)[0]"
                   " if START_MARK in t else t\n"
                   "    return head.count(END_MARK)"),
        "after": "    return t.count(END_MARK)",
        "run": ["scripts/add_machine_health.py"],
    },
    {
        "why": "★ひな型のずれを見つけられなくする"
               "（ひな型を直した日に、既存の記事が永久に古いまま残る）★",
        "file": "scripts/grow_machine.py",
        "before": '        return ["書き出しの言い回しが、いまのひな型と違います"]',
        "after": "        return []",
        "run": ["scripts/grow_machine.py"],
    },
    {
        "why": "★ずれていても様子見で飛ばす"
               "（出典が変わらない機種は二度と追いつかない）★",
        "file": "scripts/grow_machine.py",
        "before": '                if _pr.get("skip") and not template_drift(cur, _od0):',
        "after": '                if _pr.get("skip"):',
        "run": ["scripts/grow_machine.py"],
    },
    {
        "why": "★ずれていても「育てるものがありません」で止める★"
               "（記事を作るところまで来ても書かない）",
        "file": "scripts/grow_machine.py",
        "before": "    if nn and template_drift(machine, old_detail):\n"
                  "        return []",
        "after": "    if False:\n        return []",
        "run": ["scripts/grow_machine.py"],
    },
    {
        "why": "★記事データの危険なHTMLを素通りさせる"
               "（JS側は innerHTML に入れるので読者のブラウザで動く）★",
        "file": "scripts/publish_new_machine.py",
        "before": "    ng += _unsafe_places(detail)",
        "after": "    ng += []",
        "run": ["scripts/publish_new_machine.py"],
    },
    {
        "why": "★HTMLの安全判定を読めないときに素通りさせる"
               "（守りが黙って消える）★",
        "file": "scripts/publish_new_machine.py",
        "before": ('        return [f"HTMLの安全判定を読めません:'
                   ' {type(e).__name__}: {e}"]'),
        "after": "        return []",
        "run": ["scripts/publish_new_machine.py"],
    },
    {
        "why": "★CIと本番でPythonの版がずれても素通りさせる"
               "（Unicodeの表が違い、事前CIが緑でも本番だけ落ちる）★",
        "file": "scripts/pre_push_check.py",
        "before": '    if len(set(vals)) != 1:',
        "after": "    if False:",
        "run": ["scripts/pre_push_check.py"],
    },
    {
        "why": "★summaryBoxes の契約を描画側と食い違わせる"
               "（描画側が読む label/value を拒否し、"
               "読まない title/body を通していた）★",
        "file": "scripts/publish_new_machine.py",
        "before": ('                if not isinstance(b, dict)'
                   ' or set(b) - {"label", "value"}:'),
        "after": ('                if not isinstance(b, dict)'
                  ' or set(b) - {"title", "body", "type"}:'),
        "run": ["scripts/publish_new_machine.py"],
    },
    {
        "why": "★settei の空の表で見出し検査を飛ばす"
               "（JSは行が空でも headers.map を呼ぶのでページごと落ちる）★",
        "file": "scripts/publish_new_machine.py",
        "before": ('                  and (sec.get("type") == "settei"'
                   ' or (tb.get("rows") or []))'),
        "after": '                  and (tb.get("rows") or [])',
        "run": ["scripts/publish_new_machine.py"],
    },
    {
        "why": "★表の小見出し・セルが読者に見えなくても通す"
               "（<br> だけの小見出しや値の欄が空のまま出る）★",
        "file": "scripts/publish_new_machine.py",
        "before": ('                elif k in tb and tb[k] != ""'
                   ' and not _visible(tb[k], sec):'),
        "after": "                elif False:",
        "run": ["scripts/publish_new_machine.py"],
    },
    {
        "why": "★Pythonの版をコメントから読む"
               "（実際の設定ではなくコメントを読み、ずれが緑になる）★",
        "file": "scripts/pre_push_check.py",
        "before": '        out[rel] = hits[0] if len(hits) == 1 else None',
        "after": "        out[rel] = hits[0] if hits else None",
        "run": ["scripts/pre_push_check.py"],
    },
    {
        "why": "★文字参照で書いた方向制御を素通りさせる"
               "（ブラウザは &#x202e; を解釈するので、書き方を変えるだけで"
               "前回塞いだ穴が残る）★",
        "file": "scripts/publish_new_machine.py",
        "before": "                   or _g.invisible_unsafe(_html.unescape(o))",
        "after": "                   or None",
        "run": ["scripts/publish_new_machine.py"],
    },
    {
        "why": "★見出しが「あるだけ」で通す"
               "（[\"\", \"\"] は列数もそろうので通り、"
               "読者には見出しの無い値だけが出る）★",
        "file": "scripts/publish_new_machine.py",
        "before": ("    return all(isinstance(h, str)"
                   " and _ba_h.visible_text(h, markdown=False)\n"
                   "               for h in headers)"),
        "after": "    return True",
        "run": ["scripts/publish_new_machine.py"],
    },
    {
        "why": "★settei を見出し検査の対象から外す"
               "（settei のJSは headers.map を直接呼ぶので"
               "見出しが無いとページごと描かれない）★",
        "file": "scripts/publish_new_machine.py",
        "before": '            elif (sec.get("type") in ("table", "settei")',
        "after": '            elif (sec.get("type") in ("table",)',
        "run": ["scripts/publish_new_machine.py"],
    },
    {
        "why": "★settei に本文を置けるようにする"
               "（描画器は描かないので、読者には出ない）★",
        "file": "scripts/publish_new_machine.py",
        "before": ('        if sec.get("type") == "settei"'
                   ' and "body" in sec:'),
        "after": "        if False:",
        "run": ["scripts/publish_new_machine.py"],
    },
    {
        "why": "★改行まで止める"
               "（CRLF由来の \\r が混ざるだけで、その機種が公開できなくなる）★",
        "file": "scripts/gates.py",
        "before": "        if cat in _INVISIBLE_CATS and ch not in _ALLOWED_WS:",
        "after": "        if cat in _INVISIBLE_CATS:",
        "run": ["scripts/publish_new_machine.py"],
    },
    {
        "why": "★混ざった方向制御文字を素通りさせる"
               "（画面上の語順が入れ替わる・普通の文字に混ぜると"
               "可視の判定もHTMLの判定も通る）★",
        "file": "scripts/publish_new_machine.py",
        "before": "            why = (_g.invisible_unsafe(o)",
        "after": "            why = (None or None",
        "run": ["scripts/publish_new_machine.py"],
    },
    {
        "why": "★描き方が違う型を同じ物差しで測る"
               "（settei の「** **」は画面に出るのに空と数える）★",
        "file": "scripts/recheck.py",
        "before": '    _md = section.get("type") != "settei"',
        "after": "    _md = True",
        "run": ["scripts/audit_site.py"],
    },
    {
        "why": "★ハングル埋め字を「文字あり」に数える"
               "（読者には何も見えない箱が公開される）★"
               "／★直す前の壊し方は何も壊していなかった★"
               "＝Cf は手前で落ち、Cc は最後の字形判定で落ちるので"
               "結果が変わらず、別の失敗を「捕まえた」と読んでいた"
               "（2026-09-03・Codexの6回目の指摘2）",
        "file": "scripts/build_new_article.py",
        "before": '    (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),',
        "after": '    (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),',
        "run": ["scripts/audit_site.py"],
    },
    {
        "why": "★Markdownだけの中身を「文字あり」に数える"
               "（読者には何も見えない箱が公開される）★",
        "file": "scripts/build_new_article.py",
        "before": '    h = re.sub(r"<[^>]*>", "", h)',
        "after": '    h = h',
        "run": ["scripts/audit_site.py"],
    },
    {
        "why": "★type=table でも直下の rows を数える"
               "（描画器は tables しか読まないので見出しだけになる）★",
        "file": "scripts/build_new_article.py",
        "before": ('    if sec.get("type") == "table":\n'
                   '        sec = {k: v for k, v in sec.items()'
                   ' if k != "rows"}'),
        "after": "    if False:\n        sec = dict(sec)",
        "run": ["scripts/audit_site.py"],
    },
    {
        "why": "★型を書かない表も「中身あり」に数える"
               "（描画側は描かないので、見出しだけのページが読者に届く）★",
        "file": "scripts/build_new_article.py",
        "before": ('    if not isinstance(sec, dict)'
                   ' or sec.get("type") not in TABLE_TYPES:\n'
                   "        return 0"),
        "after": "    if not isinstance(sec, dict):\n        return 0",
        "run": ["scripts/audit_site.py"],
    },
    {
        "why": "★表の入れ物だけで「中身あり」にする"
               "（見出しだけで本文のないページが読者に届く）★",
        "file": "scripts/build_new_article.py",
        "before": "        tables = renderable_tables(sec)",
        "after": '        tables = sec.get("tables") or []',
        "run": ["scripts/audit_site.py"],
    },
    {
        "why": "★行の数え方が読めないとき「中身あり」に倒す"
               "（守りが黙って消える）★",
        "file": "scripts/build_new_article.py",
        "before": "    if why:\n        return 0\n    return int(n or 0)",
        "after": "    return 1",
        "run": ["scripts/audit_site.py"],
    },
    {
        "why": "★新台の記事が空っぽでも通す"
               "（読者の画面が真っ白になる＝2026-09-03にCodexが見つけた穴）★",
        "file": "scripts/audit_site.py",
        "before": "        for ng in _ba56.article_contract_problems(d):\n"
                  "            ngs.append(f\"{slug}: {ng}\")",
        "after": "        for ng in []:\n"
                 "            ngs.append(f\"{slug}: {ng}\")",
        "run": ["scripts/audit_site.py"],
    },
    {
        "why": "★新台経路の機種を1つも見ない★"
               "（見張りが空振りする）",
        "file": "scripts/audit_site.py",
        "before": '        if "publication_policy" not in m:\n'
                  "            continue\n"
                  "        slug = m[\"slug\"]",
        "after": '        if "publication_policy" in m:\n'
                 "            continue\n"
                 "        slug = m[\"slug\"]",
        "run": ["scripts/audit_site.py"],
    },
    {
        "why": "★56の対照実験そのものを黙らせる★"
               "（見張りが働いているかを誰も確かめなくなる）",
        "file": "scripts/audit_site.py",
        "before": "    return ngs + _check_56_selftest()",
        "after": "    return ngs",
        "run": ["scripts/audit_site.py"],
    },
    {
        "why": "★タスクの契約が消えても黙って通す"
               "（見張りが静かに消え、止めたタスクの手順が生き返る）★",
        "file": "scripts/audit_site.py",
        "before": '        return {}, ("★タスクの契約がありません★"\n'
                  '                    "（置き場はあるのに契約が無い＝見張りが効きません）")',
        "after": '        return {}, ""',
        "run": ["scripts/audit_site.py"],
    },
    {
        "why": "★道筋の無い裸のファイル名まで実在検査する"
               "（どこにあるか分からないものを探して、誤って手順書を止める）★",
        "file": "scripts/audit_site.py",
        "before": '                if "/" in tok or "\\\\" in tok:',
        "after": "                if True:",
        "run": ["scripts/audit_site.py"],
    },
    {
        "why": "★関所の本体から早見表の点検を外す"
               "（引き金の試験だけでは捕まらず、昨日と同じことが起きる）★",
        "file": "scripts/pre_push_check.py",
        "before": "    _hub_ng = hub_check_problem(changed, _hub_run)",
        "after": '    _hub_ng = ""',
        "run": ["scripts/pre_push_check.py"],
    },
    # ─── 2026-09-01・早見表が古いままかを見る関所 ─────────────
    {
        "why": "★早見表が古いことを見つけない"
               "（並べ替えたのに作り直さず、CIが赤いまま公開される）★",
        "file": "scripts/build_hub_pages.py",
        "before": "        elif got != built[rel]:",
        "after": "        elif False:",
        "run": ["scripts/build_hub_pages.py"],
    },
    {
        "why": "★早見表のページが消えたのを見逃す"
               "（作り直したら生まれるページを『無いだけ』で通す）★",
        "file": "scripts/build_hub_pages.py",
        "before": '            out.append(f"{rel}（ありません）")',
        "after": "            pass",
        "run": ["scripts/build_hub_pages.py"],
    },
    {
        "why": "★並べ替えても早見表を見に行かない"
               "（関所が引き金を引かず、昨日と同じことが起きる）★",
        "file": "scripts/pre_push_check.py",
        "before": "            if p == w or p.startswith(w):",
        "after": "            if False:",
        "run": ["scripts/pre_push_check.py"],
    },
    # ─── 2026-09-01・対話セッション用スキルの見張り ────────────
    {
        "why": "★スキルが止めたタスクを実行しろと書いていても通す"
               "（消したタスクの手順が生き返る）★",
        "file": "scripts/audit_site.py",
        "before": '\n            if st in line and "を実行" in line:',
        "after": "\n            if False:",
        "run": ["scripts/audit_site.py"],
    },
    {
        "why": "★スキルが実在しないスクリプトを指していても通す"
               "（手順書が静かに古くなる）★",
        "file": "scripts/audit_site.py",
        "before": "        if not exists(rel):",
        "after": "        if False:",
        "run": ["scripts/audit_site.py"],
    },
    {
        "why": "★見せた日の控えを共有の state.json に戻す"
               "（別の処理の更新を、古い内容で上書きする）★",
        "file": "scripts/ledger_sweep.py",
        "before": 'SITE_STATE_NAME = "ledger_site_state.json"',
        "after": 'SITE_STATE_NAME = "state.json"',
        "run": ["scripts/ledger_sweep.py"],
    },
    # ─── 2026-08-30・git が読めなかったことを残す ────────────────
    {
        "why": "★git が読めなかったことを記録しない"
               "（レビュー前のコードで公開処理が走っても誰も気づけない）★",
        "file": "scripts/task_guard.py",
        "before": "        if _gw:\n            _day(data)[\"git_unreadable\"] = {",
        "after": "        if False:\n            _day(data)[\"git_unreadable\"] = {",
        "run": ["scripts/task_guard.py"],
    },
    {
        "why": "★git が読めないときに担当を断る"
               "（運営者の決定に反して、夜の公開が丸ごと飛ぶ）★",
        "file": "scripts/task_guard.py",
        "before": "        if _gw:",
        "after": "        if _gw:\n            raise GuardError(\"git\")\n        if _gw:",
        "run": ["scripts/task_guard.py"],
    },
    # ─── 2026-08-30・一覧とチェッカーの食い違いを全機種で見る ────────
    {
        "why": "★通常時のモードしか見ない"
               "（CZ間・AT間だけの機種を94件飛ばす・実測42件の食い違いが隠れる）★",
        "file": "scripts/recheck.py",
        "before": "    slots = _al.slots(checker)",
        "after": "    slots = [s for s in _al.slots(checker) "
                 "if s[\"mode\"] == \"通常\"]",
        "run": ["scripts/recheck.py"],
    },
    {
        "why": "★交換率を選べない機種を飛ばす（39件が対象外に戻る）★",
        "file": "scripts/recheck.py",
        "before": "    dflt = _al.default_rate(checker)\n"
                  "    slots = _al.slots(checker)",
        "after": "    dflt = _al.default_rate(checker)\n"
                 "    if not dflt:\n"
                 "        return _result(NOT_APPLICABLE, \"x\", args)\n"
                 "    slots = _al.slots(checker)",
        "run": ["scripts/recheck.py"],
    },
    # ─── 2026-08-30・手で動かした日に仕事が出せなくなる欠陥 ─────────
    {
        "why": "★ロックが読めないとき「手動」に倒す"
               "（分からないのに関所を素通りさせる）★",
        "file": "scripts/task_guard.py",
        "before": "    except Exception:                                        # noqa: BLE001\n        return True\n\n\ndef unattended_code_state",
        "after": "    except Exception:                                        # noqa: BLE001\n        return False\n\n\ndef unattended_code_state",
        "run": ["scripts/task_guard.py"],
    },
    {
        "why": "★その日に無人がいた印を、あとの手動の担当で消せる"
               "（無人が作った未照合コミットまで push できる）★",
        "file": "scripts/task_guard.py",
        "before": '        _d0["had_unattended"] = '
                  'bool(_d0.get("had_unattended")) or _un',
        "after": '        _d0["had_unattended"] = _un',
        "run": ["scripts/task_guard.py"],
    },
    {
        "why": "★無人かどうかを、担当ごとの記録にしか残さない"
               "（その日に無人がいたかを誰も見なくなる）★",
        "file": "scripts/task_guard.py",
        "before": '        _entry(data, task)["unattended"] = _un\n'
                  '        _d0 = _day(data)',
        "after": '        _d0 = _day(data)',
        "run": ["scripts/task_guard.py"],
    },
    {
        "why": "★手動だと明示されてもロックを優先する"
               "（手で試した日に仕事が出せなくなる＝本題の欠陥）★",
        "file": "scripts/task_guard.py",
        "before": "        _un = lock_is_live() if scheduled is None "
                  "else bool(scheduled)",
        "after": "        _un = bool(scheduled) or lock_is_live()",
        "run": ["scripts/task_guard.py"],
    },
    {
        "why": "★申告が無いときに手動へ倒す"
               "（古い呼び出しが素通りする・fail-open）★",
        "file": "scripts/task_guard.py",
        "before": "        _un = lock_is_live() if scheduled is None "
                  "else bool(scheduled)",
        "after": "        _un = bool(scheduled)",
        "run": ["scripts/task_guard.py"],
    },
    # ─── 2026-08-26・確定値が検索の濃さに届くか（Codex35回目）────
    {
        "why": "★確定値に根拠を刻まない（第2出典を見つけても検索へ載らない）★",
        "file": "scripts/confirmed_values.py",
        "before": "        if (INDEX_COUNTABLE_FIELDS is None\n"
                  "                or base_field(field) in "
                  "INDEX_COUNTABLE_FIELDS):",
        "after": "        if False:",
        "run": ["scripts/confirmed_values.py"],
    },
    {
        "why": "★系列を数え直さず、出典の数だけで『独立2出典』を名乗る★",
        "file": "scripts/confirmed_values.py",
        "before": "    try:\n        n = _sl.independent(keys)",
        "after": "    try:\n        n = len(rec.get(\"sources\") or [])",
        "run": ["scripts/confirmed_values.py"],
    },
    {
        "why": "★発行者が分からなくても名乗る（fail-closed が崩れる）★",
        "file": "scripts/confirmed_values.py",
        "before": '        except Exception:                                # noqa: BLE001\n'
                  '            return ""                                    '
                  '# ★分からなければ名乗らない★',
        "after": "        except Exception:                                # noqa: BLE001\n"
                 "            pass",
        "run": ["scripts/confirmed_values.py"],
    },
    {
        # ★2026-08-29に方針が変わった★＝確定値もDMM単独も濃さに数える。
        #   ★壊し方は「数えなくする」側★（運営者の判断を戻す形）。
        "why": "★確定値と単独確認を、検索の濃さに数えなくする★",
        "file": "scripts/page_decision.py",
        "before": 'INDEX_COUNTABLE_BASIS = ("INDEPENDENT_MULTI", '
                  '"DMM_SINGLE_NEAR_RELEASE")',
        "after": 'INDEX_COUNTABLE_BASIS = ()',
        "run": ["scripts/page_decision.py", "scripts/adoption_basis.py"],
    },
    {
        "why": "★2AIの印を名乗る行を、控えの照合なしで通す★",
        "file": "scripts/build_new_article.py",
        "before": '            if slug and c.get("_from") == "confirmed_values":',
        "after": '            if False:',
        "run": ["scripts/build_new_article.py"],
    },
    {
        "why": "★箱の行に根拠を刻まない（天井・AT・CZが濃さに届かない）★",
        "file": "scripts/confirmed_values.py",
        "before": '            if "basis" in stamped:\n'
                  '                row["basis"] = stamped["basis"]',
        "after": '            if False:\n'
                 '                row["basis"] = stamped["basis"]',
        "run": ["scripts/confirmed_values.py"],
    },
    # ─── 2026-08-26・題の行つきの表（Codex33回目）────────────
    {
        "why": "★spanの位置を残さない（題の行だけか証明できなくなる）★",
        "file": "scripts/html_tables.py",
        "before": '                    self.cur.setdefault("spans", []).append(',
        "after": '                    [].append(',
        "run": ["scripts/spec_lookup.py"],
    },
    {
        "why": "★spanが題セル以外にもある表を通す（列が1つずれる）★",
        "file": "scripts/spec_lookup.py",
        "before": "        if len(spans) != 1:\n            return None",
        "after": "        if False:\n            return None",
        "run": ["scripts/spec_lookup.py"],
    },
    {
        "why": "★題セルの幅と見出しの列数が合わなくても通す★",
        "file": "scripts/spec_lookup.py",
        "before": '        if sp.get("colspan") != len(head):\n            return None',
        "after": "        if False:\n            return None",
        "run": ["scripts/spec_lookup.py"],
    },
    {
        "why": "★データ行の列数がそろっていなくても通す★",
        "file": "scripts/spec_lookup.py",
        "before": "            if len(r) != len(head):\n                return None",
        "after": "            if False:\n                return None",
        "run": ["scripts/spec_lookup.py"],
    },
    {
        "why": "★見出しの先頭が『設定』でなくても読む（順位表まで拾う）★",
        "file": "scripts/spec_lookup.py",
        "before": '    if not head or head[0] != "設定":\n        return None',
        "after": "    if not head:\n        return None",
        "run": ["scripts/spec_lookup.py"],
    },
    {
        "why": "★per_setting でも同じ設定の重複を黙って最初だけ残す★",
        "file": "scripts/spec_lookup.py",
        "before": "                if key in got and got[key] != v:\n"
                  "                    return {}, True",
        "after": "                if False:\n                    return {}, True",
        "run": ["scripts/spec_lookup.py"],
    },
    {
        "why": "★ボーナス確率が採れなくても2AIに聞かない"
               "（その機種は永久に検索へ載らない）★",
        "file": "scripts/build_new_article.py",
        "before": '    if _prof == "BONUS" and not (adopted.get("bonus_prob") or {}).get("value"):',
        "after": "    if False:",
        "run": ["scripts/build_new_article.py"],
    },
    # ─── 2026-08-26・発行の切替点と bonus_prob の残りの守り ────
    {
        "why": "★名乗りを判定書と別に決める（片方だけ v2 になり食い違う）★",
        "file": "scripts/build_new_article.py",
        "before": '        "publication_policy": decision["schema_version"],',
        "after": '        "publication_policy": _pd.SCHEMA,',
        "run": ["scripts/build_new_article.py"],
    },
    {
        "why": "★発行する版が『置いてよい版か』を確かめない"
               "（作れるのに置けない機種を毎晩作る）★",
        "file": "scripts/build_new_article.py",
        "before": "    if _pd.EMIT_SCHEMA not in _pd.ENABLED_PUBLICATION_SCHEMAS:",
        "after": "    if False:",
        "run": ["scripts/build_new_article.py"],
    },
    {
        "why": "★合算がある設定と無い設定の混在を許す"
               "（記事の『列ごと出さない』と食い違う）★",
        "file": "scripts/spec_lookup.py",
        "before": "    if any(_has) and not all(_has):",
        "after": "    if False:",
        "run": ["scripts/spec_lookup.py", "scripts/confirmed_values.py"],
    },
    {
        "why": "★同じ内部列が2つある表も採る（後のセルが黙って上書き）★",
        "file": "scripts/spec_lookup.py",
        "before": "        if len(set(cols.values())) != len(cols):\n"
                  "            continue",
        "after": "        if False:\n            continue",
        "run": ["scripts/spec_lookup.py"],
    },
    {
        "why": "★同じ設定が2行あって値が違っても最初だけ残す★",
        "file": "scripts/spec_lookup.py",
        "before": "                if _st in got and got[_st] != cell:\n"
                  "                    return {}, True",
        "after": "                if False:\n                    return {}, True",
        "run": ["scripts/spec_lookup.py"],
    },
    # ─── 2026-08-26・ボーナス確率（設定×BIG/REG/合算）─────────
    {
        "why": "★形の検査を素通りさせる（壊れた値が記事まで届く）★",
        "file": "scripts/spec_lookup.py",
        "before": '    if not isinstance(value, dict) or not value:\n'
                  '        raise BonusShapeError("ボーナス確率が空でない辞書ではありません")',
        "after": "    return None",
        "run": ["scripts/spec_lookup.py", "scripts/build_new_article.py"],
    },
    # ★外した壊し方（2026-08-26）★＝「必須の列が無い表も採る」。
    #   見出し側の検査は、行ごとの検査と**二重**だった
    #   （片方を消しても結果は同じ＝罠③）。★見出し側を消して1つにした★ので、
    #   行ごとの検査を壊す形は下の「形の検査を素通り」が見ている。
    {
        "why": "★同じページの中の食い違いを見ない（ボーナス確率）★",
        "file": "scripts/spec_lookup.py",
        "before": "            if st in merged and merged[st] != cell:\n"
                  "                conflict = True",
        "after": "            if False:\n                conflict = True",
        "run": ["scripts/spec_lookup.py"],
    },
    {
        "why": "★形の検査を、根拠による除外より後ろへ戻す"
               "（単独確認の壊れた値が素通りする）★",
        "file": "scripts/page_decision.py",
        "before": "    import spec_lookup as _sp_bp\n"
                  "    _sp_bp.validate_bonus_prob_value(v.get(\"value\"))\n"
                  "    if _skip_for_index(v, count_confirmed):\n"
                  "        return []",
        "after": "    if _skip_for_index(v, count_confirmed):\n"
                 "        return []",
        # ★記事づくりにも同じ検査がある★ので、そちらでは助けられてしまう。
        #   claim を数える側を直接たたく試験（page_decision）で見る。
        "run": ["scripts/page_decision.py"],
    },
    {
        "why": "★受け口（confirmed_values）が形を確かめない★",
        "file": "scripts/confirmed_values.py",
        "before": "            _sp.validate_bonus_prob_value(value)",
        "after": "            pass",
        "run": ["scripts/confirmed_values.py"],
    },
    {
        "why": "★記事の表が、採れていない列も出す（未確認のセルを作る）★",
        "file": "scripts/build_new_article.py",
        "before": '        _keys = [k for k in ("big", "reg", "total")\n'
                  '                 if any(k in c for c in _bp["value"].values())]',
        "after": '        _keys = ["big", "reg", "total"]',
        "run": ["scripts/build_new_article.py"],
    },
    # ─── 2026-08-26・Codex29回目で足した守り ───────────────
    {
        "why": "★入口（plan）から区分の判定を外す（凍結と版の食い違いを通す）★",
        "file": "scripts/apply_indexing_policy.py",
        "before": "        _pd.machine_class(m, policy)",
        "after": "        pass",
        "run": ["scripts/apply_indexing_policy.py"],
    },
    {
        "why": "★経路の判定を『既知の版の名簿』に戻す（未知版が旧形式へ落ちる）★",
        "file": "scripts/page_decision.py",
        "before": '    return "publication_policy" in machine',
        "after": '    return machine.get("publication_policy") in SCHEMAS',
        "run": ["scripts/page_decision.py"],
    },
    {
        "why": "★監査54から OGP（property=）の読み取りを外す★",
        "file": "scripts/audit_site.py",
        "before": "        metas = list(_hc54.parse(html).meta_contents)",
        "after": "        metas = []",
        "run": ["scripts/audit_site.py"],
    },
    {
        "why": "★監査54の対象を index.html だけに戻す（ポチポチくんを見ない）★",
        "file": "scripts/audit_site.py",
        "before": '    for hf in sorted((base / "machines").glob("*/*.html")):',
        "after": '    for hf in sorted((base / "machines").glob("*/index.html")):',
        "run": ["scripts/audit_site.py"],
    },
    # ★外した壊し方（2026-08-26）★
    #   「経路の判定を v1 限定に戻す」＝`is_auto` を
    #   「鍵があるか」に直した（Codex31回目のP0）ことで、
    #   下の「既知の版の名簿に戻す」と**同じ穴**になった。
    #   ★同じ穴を2通りで数えない★
    {
        "why": "★緊急スイッチを v1 の式で固定する（v2 を v1 の形で上書き）★",
        "file": "scripts/apply_indexing_policy.py",
        "before": '        pd_new = _pd.recompute(pd_old, policy["mode"])',
        "after": '        pd_new = _pd.decide_from_claims('
                  'pd_old["claims"], policy["mode"], pd_old["decided_at"])',
        "run": ["scripts/apply_indexing_policy.py"],
    },
    {
        "why": "★名乗りと中身の版の食い違いを見ない★",
        "file": "scripts/page_decision.py",
        "before": "    if _pdver != pub:",
        "after": "    if False:",
        "run": ["scripts/page_decision.py",
                "scripts/apply_indexing_policy.py"],
    },
    # ★外した壊し方（2026-08-26）★＝「置いてよい版の名簿を広げる」。
    #   解凍して `ENABLED_PUBLICATION_SCHEMAS` と `SCHEMAS` が同じ中身に
    #   なったので、★「知らない版」の検査が先に拾う★＝名簿を広げても
    #   結果が変わらない（罠③＝二重の守り）。
    #   ★名簿の検査そのものは、下の「静かに旧形式扱いにする」が見ている★
    #   （名簿をわざと狭めて試す場所を run に入れてある）。
    {
        "why": "★名簿に無い版を、例外ではなく静かに旧形式扱いにする★",
        "file": "scripts/page_decision.py",
        "before": '    if pub not in ENABLED_PUBLICATION_SCHEMAS:\n'
                  '        raise DecisionError(',
        "after": '    if False:\n'
                 '        raise DecisionError(',
        # ★名簿をわざと狭めて試す場所で見る★（2026-08-26）
        #   ★page_decision の試験だけでは足りない★＝解凍後は
        #   名簿と「読める版」が同じ中身なので、そこでは差が出ない。
        "run": ["scripts/apply_indexing_policy.py",
                "scripts/build_new_article.py"],
    },
    # ★外した壊し方（2026-08-26）★
    #   「発行の試験を『どちらの版でも合格』に戻す」＝
    #   ★これは守り（コード）ではなく**試験の判定式**を緩める操作★。
    #   試験を緩めれば試験は通る。当たり前なので、これを
    #   「守られていない」と数えると道具の判定が濁る。
    #   ★同じ穴は page_decision 側の2件（凍結を外す・例外にしない）で見ている★
    {
        "why": "★ひな型そのものの断り書きを変える（二重管理の食い違い）★",
        "file": "machine.html",
        "before": "このページは確認が取れた項目のみ掲載しています。",
        "after": "このページは出典で確認が取れた項目のみ掲載しています。",
        "run": ["scripts/publish_new_machine.py"],
    },
    {
        "why": "★ひな型との突き合わせを外す★",
        "file": "scripts/publish_new_machine.py",
        "before": "    _tn = check_template_notice(_raw_template)",
        "after": "    _tn = []",
        "run": ["scripts/publish_new_machine.py"],
    },
    {
        "why": "★断り書きの文言を突き合わせない（黙って食い違う）★",
        "file": "scripts/publish_new_machine.py",
        "before": "    ng += check_notice_text(html)",
        "after": "",
        "run": ["scripts/publish_new_machine.py"],
    },
    {
        "why": "★暴走止めを暦日で数える（日をまたぐ夜は2倍通る）★",
        "file": "scripts/task_guard.py",
        "before": "    if now.hour < NIGHT_ROLLOVER_HOUR:\n"
                  "        now = now - timedelta(days=1)",
        "after": "    pass",
        "run": ["scripts/task_guard.py"],
    },
    {
        "why": "★一晩の記録を暦日の入れ物に戻す★",
        "file": "scripts/task_guard.py",
        "before": '            done = _night(data).setdefault("slugs", [])',
        "after": '            done = d.setdefault("unlimited_slugs", [])',
        "run": ["scripts/task_guard.py"],
    },
    {
        "why": "★どこから採ったかの見張り（54）を黙らせる★",
        "file": "scripts/audit_site.py",
        "before": '    hits = []\n'
                  '    for w in _SOURCE_WORDS:',
        "after": '    hits = []\n'
                 '    for w in ():',
        "run": ["scripts/audit_site.py"],
    },
    {
        "why": "★見張り54の例外を、一文ではなくファイルごとに広げる★",
        "file": "scripts/audit_site.py",
        "before": '    for ok in _SOURCE_ALLOWED_SENTENCES:\n'
                  '        t = t.replace(ok, "")',
        "after": '    for ok in _SOURCE_ALLOWED_SENTENCES:\n'
                 '        if ok in t:\n'
                 '            return []',
        "run": ["scripts/audit_site.py"],
    },
    {
        "why": "天井の抽出器が根拠を保存し忘れる",
        "file": "scripts/ceiling_lookup.py",
        "before": '            c["basis"] = next(sup["basis"] for k3, v3, sup in _sups\n'
                  '                              if k3 == agreed[0][0])',
        "after": "",
        "run": ["scripts/adoption_basis.py", "scripts/page_decision.py"],
    },
    {
        "why": "基本スペックの抽出器が根拠を保存し忘れる",
        "file": "scripts/spec_lookup.py",
        "before": '                            "basis": _sups[agreed[0][0]]["basis"]}',
        "after": '                            }',
        "run": ["scripts/adoption_basis.py", "scripts/page_decision.py"],
    },
    {
        "why": "記事が根拠を名乗らなくなる",
        "file": "scripts/build_new_article.py",
        "before": "    return _basis_tag((row or {}).get(key))",
        "after": '    return ""',
        "run": ["scripts/build_new_article.py", "scripts/adoption_basis.py"],
    },
    {
        "why": "検索の数え方を白名簿から黒名簿へ戻す",
        "file": "scripts/page_decision.py",
        "before": '    return str((v or {}).get("basis") or "") '
                  'not in INDEX_COUNTABLE_BASIS \\\n'
                  '        if isinstance(v, dict) else True',
        "after": "    return _from_2ai(v) or _single_source(v)",
        "run": ["scripts/adoption_basis.py"],
    },
    {
        "why": "控えに別の出典があっても無視する",
        "file": "scripts/adoption_basis.py",
        "before": '    if c.get("other_sources_known"):',
        "after": "    if False and c.get(\"other_sources_known\"):",
        "run": ["scripts/adoption_basis.py"],
    },
    {
        "why": "控えが読めないとき「知らない」に倒す",
        "file": "scripts/adoption_basis.py",
        "before": '        return True, f"控えを読めません（{str(e)[:40]}）"',
        "after": '        return False, ""',
        "run": ["scripts/adoption_basis.py"],
    },
    {
        "why": "投稿欄の件数の条件を外す",
        "file": "scripts/user_area.py",
        "before": "    miss_b = [r for r in need_b\n"
                  "              if _required_now(r) and not _find(root, [r])]",
        "after": "    miss_b = [r for r in need_b if not _find(root, [r])]",
        "run": ["scripts/user_area.py"],
    },
    {
        "why": "件数の場所を名指しせず、最初の「N件」を拾う",
        "file": "scripts/user_area.py",
        "before": '    where = rule.get("count_in")',
        "after": '    where = None',
        "run": ["scripts/user_area.py"],
    },
    {
        "why": "spec系の検査を飛ばす（壊れた材料が黙って通る）",
        "file": "scripts/page_decision.py",
        "before": '        if isinstance(v, dict) and "value" in v and _bad_value_deep(v["value"]):\n'
                  '            raise DecisionError(f"{key} の値がありません: {v!r}")',
        "after": "",
        "run": ["scripts/page_decision.py"],
    },
    {
        "why": "設定別の表が根拠を名乗らない（前回の見落とし）",
        "file": "scripts/build_new_article.py",
        "before": '        rows = [[f"設定{k}", f"{got[\'value\'][k]}{_mark}"]\n'
                  '                for k in sorted(got["value"])]',
        "after": '        rows = [[f"設定{k}", got["value"][k]]\n'
                 '                for k in sorted(got["value"])]',
        "run": ["scripts/build_new_article.py"],
    },
    {
        "why": "根拠のない値を空で流す（読者に断りなしで出る・Codex3回目P0）",
        "file": "scripts/build_new_article.py",
        "before": '    t = str(basis or "")\n'
                  "    if t not in BASIS_SUFFIX:\n"
                  "        raise BuildError(\n"
                  '            f"採用した値に根拠がありません（区分: {basis!r}）／"\n'
                  '            "★根拠の分からない値は記事にしません★"\n'
                  '            "／抽出器が basis を保存し忘れていないか確かめてください")\n'
                  "    return BASIS_SUFFIX[t]",
        "after": '    return BASIS_SUFFIX.get(str(basis or ""), "")',
        "run": ["scripts/adoption_basis.py"],
    },
    {
        "why": "ATの抽出器が根拠を保存し忘れる（Codex3回目・未カバーだった）",
        "file": "scripts/at_spec_lookup.py",
        "before": '            c["basis"] = next(sup["basis"] for v, sup in _sups\n'
                  "                              if v is agreed[0])",
        "after": "",
        "run": ["scripts/adoption_basis.py"],
    },
    {
        "why": "CZの抽出器が根拠を保存し忘れる（Codex3回目・未カバーだった）",
        "file": "scripts/cz_lookup.py",
        "before": '                        "basis": _sup["basis"],',
        "after": "",
        "run": ["scripts/adoption_basis.py"],
    },
    {
        "why": "2AIの印を自己申告で通す（誰でも関所を開けられる・Codex4回目）",
        "file": "scripts/build_new_article.py",
        "before": "    if not slug:\n"
                  "        return False                     "
                  "# ★どの機種の控えを見ればよいか分からない★",
        "after": "    return True",
        "run": ["scripts/build_new_article.py"],
    },
    {
        "why": "ゲーム性・リセットを名簿から外す（Codex4回目・素通りしていた）",
        "file": "scripts/page_decision.py",
        "before": '    "gameplays": ("basis",),          # ゲームの流れ\n'
                  '    "resets": ("basis",),             # 朝一・リセット',
        "after": "",
        "run": ["scripts/build_new_article.py"],
    },
    {
        "why": "2AIだけが答える項目の表示名を引かない（新台追加がKeyErrorで止まる）",
        "file": "scripts/add_machine_run.py",
        "before": "    lab = _cv.AI_ONLY_LABELS.get(k)\n"
                  "    if lab:\n"
                  "        return lab",
        "after": "",
        "run": ["scripts/add_machine_run.py"],
    },
    {
        "why": "控えの項目名を照合しない（別項目の控えで通る・Codex5回目）",
        "file": "scripts/build_new_article.py",
        "before": "    rec = (recs or {}).get(field)",
        "after": "    rec = ((recs or {}).get(field)\n"
                 "           or next(iter((recs or {}).values()), None))",
        "run": ["scripts/build_new_article.py"],
    },
    {
        "why": "天井の網羅性を申告だけで信じる（読者を守る一文が消える・Codex5回目）",
        "file": "scripts/build_new_article.py",
        "before": "                     and _confirmed_by_2ai(_cflag, slug, _recs)\n"
                  "                     and ((_cflag or {}).get(\"value\") or {}).get(\n"
                  "                         \"complete\") == \"YES\")",
        "after": "                     )",
        "run": ["scripts/build_new_article.py"],
    },
    {
        "why": "関所が名簿ではなく自前の表を読む（名簿が飾りになる・Codex5回目）",
        "file": "scripts/build_new_article.py",
        "before": "_BASIS_REQUIRED = tuple(_pd.READER_BOXES.items())",
        "after": '_BASIS_REQUIRED = (("adopted", ("basis",)),\n'
                 '                   ("ceilings", ("basis",)),\n'
                 '                   ("at_specs", ("basis",)),\n'
                 '                   ("czs", ("basis", "games_basis",\n'
                 '                            "rate_basis")))',
        "run": ["scripts/build_new_article.py"],
    },
    {
        "why": "内側の値だけ見て通す（外側に別の表示値を足せる・Codex6回目）",
        "file": "scripts/build_new_article.py",
        "before": '    return _core(val) == got or {"value": val} == got',
        "after": '    return (_core(val) == got or {"value": val} == got\n'
                 '            or val == got.get("value"))',
        "run": ["scripts/build_new_article.py"],
    },
    {
        "why": "控えを読むとき契約を確かめない（偽の記録が通る・Codex6回目）",
        "file": "scripts/confirmed_values.py",
        "before": "    if strict:\n        bad = []",
        "after": "    if False:\n        bad = []",
        "run": ["scripts/confirmed_values.py"],
    },
    {
        "why": "控えが読めなくても新台を作る（2AIの値が抜けた記事が出る・Codex6回目）",
        "file": "scripts/add_machine_run.py",
        "before": 'BLOCKING = ("CONFIRMED_VALUES_UNREADABLE",\n'
                  '            "AMBIGUOUS_CANDIDATES", "CATALOG_UNHEALTHY",',
        "after": 'BLOCKING = ("AMBIGUOUS_CANDIDATES", "CATALOG_UNHEALTHY",',
        "run": ["scripts/add_machine_run.py"],
    },
    {
        "why": "純増を引用と照合しない（出典に無い数を載せられる・Codex7回目）",
        "file": "scripts/confirmed_values.py",
        "before": '                        "quoted": ("values",)},',
        "after": '                        "quoted": ()},',
        "run": ["scripts/confirmed_values.py"],
    },
    {
        "why": "控えが消えても0件として通す（確定値が全部抜けた記事が出る・Codex7回目）",
        "file": "scripts/confirmed_values.py",
        "before": "        if require_exists:",
        "after": "        if False:",
        "run": ["scripts/confirmed_values.py"],
    },
    {
        "why": "控えの系列を数え直さない（1出典の記録が通る・Codex7回目）",
        "file": "scripts/confirmed_values.py",
        # ★項目ごとの最小値になったので目印を合わせた★（2026-08-27）
        "before": "                _need = min_sources(base)\n"
                  "                if len(got) < _need:",
        "after": "                _need = min_sources(base)\n"
                 "                if False:",
        "run": ["scripts/confirmed_values.py"],
    },
    {
        "why": "数を部分一致で照合する（13.1の中の3.1が通る・Codex8回目）",
        "file": "scripts/confirmed_values.py",
        "before": "    if not _NUMBERISH.match(t):\n"
                  "        return t in q                      "
                  "# 文字の値は今までどおり",
        "after": "    if True:\n        return t in q",
        "run": ["scripts/confirmed_values.py"],
    },
    {
        "why": "系列が空なら比べない（0件の記録が通る・Codex8回目）",
        "file": "scripts/confirmed_values.py",
        "before": "                if keep != got:",
        "after": "                if keep and keep != got:",
        "run": ["scripts/confirmed_values.py"],
    },
    {
        "why": ("★新台を導入日なしで作れる"
                "（トップページの並びに入れられなくなる"
                "・2026-08-29の運営者の指示）★"),
        "file": "scripts/build_new_article.py",
        "before": '    if not str(release or "").strip():',
        "after": "    if False:",
        "run": ["scripts/build_new_article.py"],
    },
    {
        "why": "手作業の口から公開できる（控えを通らない・Codex8回目）",
        "file": "scripts/build_new_article.py",
        "before": "        return 1\n    return 0",
        "after": "        print(apply(slug, machine, detail))\n"
                 "        return 0\n    return 0",
        "run": ["scripts/build_new_article.py"],
    },
    {
        "why": "公開直前の再検証を呼ばない（控えの手書きを見破れない・Codex8回目）",
        "file": "scripts/add_machine_run.py",
        "before": "            _rv = _cv.reverify(out[\"slug\"], name=name,\n"
                  "                               official_url=official_url)",
        "after": "            _rv = []",
        "run": ["scripts/add_machine_run.py"],
    },
    {
        "why": "再検証で本文の変化を見ない（2AI判断の前提が崩れても通す・Codex8回目）",
        "file": "scripts/confirmed_values.py",
        "before": "                elif old[\"text_sha256\"] != now_sha:",
        "after": "                elif False:",
        "run": ["scripts/confirmed_values.py"],
    },
    {
        "why": "出典の投稿欄を落とさない（読者の書き込みを根拠にできる・Codex9回目）",
        "file": "scripts/confirmed_values.py",
        "before": "    got = _fp.fetch(url, \"claim_material\")",
        # ★取りに行く行は組み立てて書く★
        #   （監査42が、壊し方の定義文を本物のコードと誤認するため）
        "after": ("    import new_machine_watch as _w" + chr(10)
                  + '    with _w.fetching("claim_material"):' + chr(10)
                  + "        _raw = _w." + "_g" + "et(url)" + chr(10)
                  + "    import fetched_page as _fp2" + chr(10)
                  + "    got = _fp2.FetchedPage(url, url, _raw)"),
        "run": ["scripts/confirmed_values.py"],
    },
    {
        "why": "同じURLの2件目の引用を照合しない（Codex9回目）",
        "file": "scripts/confirmed_values.py",
        "before": "                got = verify_source(dict(src), name or slug,\n"
                  "                                    lambda _u, _h=html: _h)",
        "after": "                if url in globals().setdefault('_RVSEEN', set()):\n"
                 "                    continue\n"
                 "                globals()['_RVSEEN'].add(url)\n"
                 "                got = verify_source(dict(src), name or slug,\n"
                 "                                    lambda _u, _h=html: _h)",
        "run": ["scripts/confirmed_values.py"],
    },
    {
        "why": "読み直しの判断者を「2つあればよい」に緩める（Codex9回目）",
        "file": "scripts/confirmed_values.py",
        "before": "    if not isinstance(who, list) or not (\n"
                  "            set(REQUIRED_JUDGES) <= {str(x).lower() for x in who}):",
        "after": "    if not isinstance(who, list) or len(\n"
                 "            {str(x).lower() for x in who}) < 2:",
        "run": ["scripts/confirmed_values.py"],
    },
    {
        "why": "控えが無ければ黙って作る（消失と初回を区別しない・Codex9回目）",
        "file": "scripts/confirmed_values.py",
        "before": "    data = load(strict=False, require_exists=True)",
        "after": "    data = load(strict=False)",
        "run": ["scripts/confirmed_values.py"],
    },
    {
        "why": "転送先を捨てる（投稿欄の決まりと本文が食い違う・Codex10回目）",
        "file": "scripts/confirmed_values.py",
        "before": "    if a != b:",
        "after": "    if False:",
        "run": ["scripts/confirmed_values.py"],
    },
    {
        "why": "指紋の箱ごと消されても通す（Codex10回目）",
        "file": "scripts/confirmed_values.py",
        "before": "            if not (x.get(\"identity_why\") "
                  "or x.get(\"identity_proof\")):\n"
                  "                continue",
        "after": "            continue",
        "run": ["scripts/confirmed_values.py"],
    },
    {
        "why": "壊れた入れ物を「0件」と読む（Codex10回目）",
        "file": "scripts/confirmed_values.py",
        "before": "    if slug in raw and not isinstance(raw[slug], dict):",
        "after": "    if False:",
        "run": ["scripts/confirmed_values.py"],
    },
    {
        "why": "引用の照合だけ素通しに戻す（投稿文が根拠になる・Codex12回目）",
        "file": "scripts/confirmed_values.py",
        "before": "    text = text_of(html)",
        "after": '    text = " ".join(_w._visible_text(html).split())',
        "run": ["scripts/confirmed_values.py"],
    },
    {
        "why": "行で切る前に1行へ潰す（行切りが効かなくなる・2026-08-24に自分で踏んだ）",
        "file": "scripts/confirmed_values.py",
        "before": "    raw = _w2._visible_text(html)",
        "after": '    raw = " ".join(_w2._visible_text(html).split())',
        "run": ["scripts/confirmed_values.py"],
    },
    {
        "why": "投稿欄の確認の期限切れを見ない（Codex12回目）",
        "file": "scripts/audit_site.py",
        "before": "            if due < _dt.date.today():",
        "after": "            if False:",
        "run": ["scripts/audit_site.py"],
    },
    {
        "why": "再確認の指紋を別の作り方で出す（本文が同じでも止まる・Codex13回目）",
        "file": "scripts/confirmed_values.py",
        "before": "                now_sha = _hl.sha256(\n"
                  "                    page_text(html, url).encode(\"utf-8\")"
                  ").hexdigest()",
        "after": "                now_sha = _hl.sha256(\n"
                 "                    \" \".join(_w9._visible_text(html)"
                 ".split()).encode(\"utf-8\")).hexdigest()",
        "run": ["scripts/confirmed_values.py"],
    },
    {
        "why": "掃除が理解できない決まりごとでも通す（Codex13回目）",
        "file": "scripts/audit_site.py",
        "before": "        bad = [r for r in drops if not (r.get(\"id\") "
                  "or r.get(\"class\"))]",
        "after": "        bad = []",
        "run": ["scripts/audit_site.py"],
    },
    {
        "why": "残存検査を「決まりごとが無いサイト」だけにする（Codex14回目）",
        "file": "scripts/fetched_page.py",
        "before": "    hint = _ua.looks_like_user_area(cleaned)",
        "after": "    hint = ([] if [r for r in "
                 "(_ua.conf_for_url(url).get(\"drop\") or [])\n"
                 "            if isinstance(r, dict)]\n"
                 "            else _ua.looks_like_user_area(cleaned))",
        "run": ["scripts/fetched_page.py"],
    },
    {
        "why": "投稿欄の語を行切りと別々に持つ（片方だけ見逃す・Codex14回目）",
        "file": "scripts/ceiling_lookup.py",
        "before": "    for w in _user_area_words():",
        "after": "    for w in _USER_AREA:",
        "run": ["scripts/user_area.py"],
    },
    {
        "why": "名前を部分一致で見る（moreView が review に当たる・実ページで踏んだ）",
        "file": "scripts/user_area.py",
        "before": "            for w in _UA_ATTR_WEAK:\n                if w in toks:",
        "after": "            for w in _UA_ATTR_WEAK:\n                if w in names.lower():",
        "run": ["scripts/user_area.py"],
    },
    {
        "why": "必須箱が無くても通す（相手が名前を変えたら素通り・Codex14回目）",
        "file": "scripts/audit_site.py",
        "before": "        if not [r for r in (ua.get(\"require_before\") or [])\n"
                  "                if isinstance(r, dict)]:",
        "after": "        if False:",
        "run": ["scripts/audit_site.py"],
    },
    {
        "why": "弱い語も見出しに含むだけで止める（編集部コメントで止まる・Codex15回目）",
        "file": "scripts/user_area.py",
        "before": "                if bare.lower() in (w.lower(), w.lower() + \"一覧\",",
        "after": "                if w.lower() in low or bare.lower() in (w.lower(),",
        "run": ["scripts/user_area.py"],
    },
    {
        "why": "弱い語を見出しでまったく見ない（名前が弱い第二投稿欄が通る・Codex15回目）",
        "file": "scripts/user_area.py",
        "before": "            for w in USER_AREA_WEAK:",
        "after": "            for w in ():",
        "run": ["scripts/user_area.py"],
    },
    {
        "why": "既定ポートをそろえない（正常な転送で止まる・Codex15回目）",
        "file": "scripts/maker_identity_cache.py",
        "before": "        port = f\":{sp.port}\" if sp.port and "
                  "sp.port != _default else \"\"",
        "after": "        port = f\":{sp.port}\" if sp.port else \"\"",
        "run": ["scripts/maker_identity_cache.py"],
    },
    {
        "why": "弱い名前の箱を、中身を見ずに素通しする（見出し無しの投稿表・Codex16回目）",
        "file": "scripts/user_area.py",
        "before": "                if w in toks:\n                    return w           # ★中身を見てから決める★",
        "after": "                if False:\n                    return w",
        "run": ["scripts/user_area.py"],
    },
    {
        "why": "弱い名前でも即止める（実戦レビューで止まる・Codex16回目）",
        "file": "scripts/user_area.py",
        "before": "            for w in _UA_ATTR_STRONG:",
        "after": "            for w in _UA_ATTR_HINTS:",
        "run": ["scripts/user_area.py"],
    },
    {
        "why": "対応しない終了タグでも積みを崩す（箱が早く閉じて見逃す・Codex17回目）",
        "file": "scripts/user_area.py",
        "before": "            if tag in _VOID or tag not in "
                  "[x[0] for x in self.stack]:\n"
                  "                return",
        "after": "            if False:\n                return",
        "run": ["scripts/user_area.py"],
    },
    {
        "why": "育てる側で出典を確かめ直さない（控えの手書きが通る・Codex17回目）",
        "file": "scripts/grow_machine.py",
        "before": "            _rv = _cv.reverify(slug, name=vo.get(\"identity_name\") or name,\n"
                  "                               official_url=url)",
        "after": "            _rv = []",
        "run": ["scripts/grow_machine.py"],
    },
    {
        "why": "型が不明なとき、黙ってATの線に倒す（ノーマル機が永久に載らない原因が隠れる・Codex27回目）",
        "file": "scripts/page_decision.py",
        "before": "        reasons.append(\"MACHINE_PROFILE_UNKNOWN\")",
        "after": "        pass",
        "run": ["scripts/page_decision.py"],
    },
    {
        "why": "ボーナスタイプにも at:/cz: を要求する（ノーマル機が原理的に検索へ載せられない・今回の欠陥そのもの）",
        "file": "scripts/page_decision.py",
        "before": "        if not any(c in _BONUS_CLAIMS for c in claims):",
        "after": "        if not any(c.startswith((\"at:\", \"cz:\")) for c in claims):",
        "run": ["scripts/page_decision.py"],
    },
    {
        "why": "機種の区分を v1 の式で計算し直す（v2 の機種が永久に AUTO_PENDING になる）",
        "file": "scripts/page_decision.py",
        # ★分岐を recompute にまとめたので、目印もそちらへ移した★（2026-08-26）
        # ★ありうる形にする★（2026-08-26）＝版の分岐を書き間違えると、
        #   「知らない版」で例外になるのではなく**v1の式で計算**してしまう。
        #   例外で落ちると「ただ落ちただけ」に分類され、守りの証拠にならない。
        "before": '        return decide_from_claims_v2(\n'
                  '            pd["claims"], mode, pd["machine_profile"],\n'
                  '            pd["ceiling_state"], pd["decided_at"])',
        "after": '        return decide_from_claims(\n'
                 '            pd["claims"], mode, pd["decided_at"])',
        "run": ["scripts/page_decision.py",
                "scripts/apply_indexing_policy.py"],
    },
    {
        "why": "天井の有無を型から推論する（「ボーナスタイプだから天井なし」＝出典に無い断定・Codex27回目）",
        "file": "scripts/page_decision.py",
        "before": "    if (material.get(\"ceilings\") or {}).get(\"adopted\"):\n        return \"PRESENT\"",
        "after": "    if True:\n        return \"NONE\"",
        "run": ["scripts/page_decision.py"],
    },
    {
        "why": "新台タスクだけ未コミットの歯止めを飛ばす（レビュー前のコードで公開してpushする・台帳#478）",
        "file": "scripts/task_guard.py",
        "before": "        _dirty0, _gw = unattended_code_state(task)",
        "after": "        _dirty0, _gw = [], \"\"",
        "run": ["scripts/task_guard.py"],
    },
    {
        "why": "新台の暴走止めを外す（同じ晩に何十件も作り続けても止まらない・台帳#479）",
        "file": "scripts/task_guard.py",
        "before": "            if slug not in done and len(done) >= UNLIMITED_RUNAWAY_CAP:",
        "after": "            if False:",
        "run": ["scripts/task_guard.py"],
    },
    {
        "why": "記事にサイト名を出す（ほかサイトのコピーに見える・2026-08-26の運営者の指示）",
        "file": "scripts/build_new_article.py",
        "before": "    \"DMM_SINGLE_NEAR_RELEASE\": \"（確認1件のみ）\",",
        "after": "    \"DMM_SINGLE_NEAR_RELEASE\": \"（DMMぱちタウン単独確認）\",",
        "run": ["scripts/build_new_article.py"],
    },
    {
        "why": "始める前からある変更を便乗させる（この公開が作っていない書き換えが公開される・Codex25回目）",
        "file": "scripts/prepush_gate.py",
        "before": "    ng = list(preexisting(slug))",
        "after": "    ng = []",
        "run": ["scripts/prepush_gate.py"],
    },
    {
        "why": "控えが無くても便乗の検査を通す（fail-closed が崩れる・Codex25回目）",
        "file": "scripts/prepush_gate.py",
        "before": "        if \"dirty_before\" not in m:\n            return None, (f\"★{name}に「始める前の状態」が控えられていません★\"\n                          \"／★この公開が作った変更だけかを確かめられません★\")",
        "after": "        if \"dirty_before\" not in m:\n            return [], \"\"",
        "run": ["scripts/prepush_gate.py"],
    },
    {
        "why": "push先の確認に時間制限を付けない（固まると理由も残さず止まり続ける・Codex25回目）",
        "file": "scripts/add_machine_run.py",
        "before": "            encoding=\"utf-8\", errors=\"replace\", timeout=NET_TIMEOUT)",
        "after": "            encoding=\"utf-8\", errors=\"replace\")",
        "run": ["scripts/add_machine_run.py"],
    },
    {
        # ★2026-08-29に方針が変わった★＝名乗りだけの免除は消した。
        #   ★壊し方は逆向き★＝「名乗りだけで通す」に戻す形で試す。
        "why": "設定表を、名乗りだけで根拠ありと認める（控えと違う値が通る）",
        "file": "scripts/recheck.py",
        "before": "            _field = _TBL_FIELD.get(",
        "after": ("            if any(_v.endswith(_b) for _b in _BASIS_MARKS):"
                  "\n                return True"
                  "\n            _field = _TBL_FIELD.get("),
        "run": ["scripts/recheck.py"],
    },
    {
        "why": "名乗りを完全一致で見ない（1/300（実際は1/3000）のような別の断定が通る・Codex24回目）",
        "file": "scripts/recheck.py",
        "before": "            return any(_v == want + _bs for _bs in _BASIS_MARKS)",
        "after": "            return _v.startswith(want)",
        "run": ["scripts/recheck.py"],
    },
    {
        "why": "記事の「未確認」の言い方を知らない（まだ何も書いていない箱を『書いている』と言う・2026-08-25の通し試験）",
        "file": "scripts/recheck.py",
        "before": "                   or any(_pt and _pt in x for _pt in _PENDING_ALL)]",
        "after": "                   ]",
        "run": ["scripts/recheck.py"],
    },
    {
        "why": "身元の行を根拠のない断定に数える（機種名・登場時期で正しい記事が止まる・2026-08-25の通し試験）",
        "file": "scripts/recheck.py",
        "before": "                 if x not in _isnote and not _identity_line(x)\n                 and not _backed(x, topic)]",
        "after": "                 if x not in _isnote and not _backed(x, topic)]",
        "run": ["scripts/recheck.py"],
    },
    {
        "why": "表に合わない行があっても、本文が断り書きだけなら飛ばす（表の不一致が消える・Codex24回目）",
        "file": "scripts/recheck.py",
        "before": "        if _isnote and len(_isnote) == len(_nonempty) and not _tbl_bad:",
        "after": "        if _isnote and len(_isnote) == len(_nonempty):",
        "run": ["scripts/recheck.py"],
    },
    {
        "why": "表を項目ごとに分けない（別項目の値を根拠にできる・Codex23回目）",
        "file": "scripts/recheck.py",
        "before": "            _field = _TBL_FIELD.get(str(tbl.get(\"label\") or \"\").strip())",
        "after": "            _field = \"at_prob\"",
        "run": ["scripts/recheck.py"],
    },
    {
        "why": "表の値を部分一致で見る（1/300 が 1/3000 を通す・Codex23回目）",
        "file": "scripts/recheck.py",
        "before": "            if _v == want:\n                return True",
        "after": "            if True:\n                return True",
        "run": ["scripts/recheck.py"],
    },
    {
        "why": "表の行を本文の判定へ渡す（値のコピーが免除される・Codex23回目）",
        "file": "scripts/recheck.py",
        "before": "        _left += _tbl_bad",
        "after": "        pass",
        "run": ["scripts/recheck.py"],
    },
    {
        "why": "控えを読めなくても黙って進む（正しい記事を毎回止める・Codex22回目）",
        "file": "scripts/recheck.py",
        "before": "        return _result(ERROR, f\"確定値を読めません: {_e_cv}\", args)",
        "after": "        _by_topic, _pairs = {}, {}",
        "run": ["scripts/recheck.py"],
    },
    {
        "why": "表の行を見ない（設定別の値が一度も検査されない・Codex22回目）",
        "file": "scripts/recheck.py",
        "before": "        for _i_tb2, _tb in enumerate(sec.get(\"tables\") or []):",
        "after": "        for _i_tb2, _tb in enumerate([]):",
        "run": ["scripts/recheck.py"],
    },
    {
        "why": "表の行を組で見ない（正しい表を毎回『直せ』にする・Codex22回目）",
        "file": "scripts/recheck.py",
        "before": "                if _row_backed(_tb, _cells[0], \"：\".join(_cells[1:])):",
        "after": "                if False:",
        "run": ["scripts/recheck.py"],
    },
    {
        "why": "記事が読まない項目も受け取る（2AIの答えが迷子になる・Codex22回目）",
        "file": "scripts/confirmed_values.py",
        "before": "    for _k in CLOSED_FIELDS:",
        "after": "    for _k in ():",
        "run": ["scripts/confirmed_values.py"],
    },
    {
        "why": "確定値の形が判定できなくても黙って進む（Codex22回目）",
        "file": "scripts/recheck.py",
        "before": "                return _result(ERROR, f\"確定値の項目の話題が決まっていません: \"\n                                      f\"{_e_tp}\", args)\n            if not _tp:\n                continue          # 読者に出さない項目（型式名など）\n            try:\n                _tk = [str(x) for x in _cv_rc.check_shape(\n                    _b, (_rec or {}).get(\"value\")) if str(x).strip()]\n            except Exception as _e_sh:                       # noqa: BLE001\n                # ★★形が判定できないときも止まる★★\n                #   （2026-08-25・Codexの22回目。話題の例外と同じ扱い）\n                #   ★直す前は continue で飛ばしていた★ので、\n                #   その項目が根拠にならないまま静かに進み、\n                #   正しい記事を止める側に倒れていた。\n                return _result(ERROR, f\"確定値の形を判定できません: {_e_sh}\",\n                               args)",
        "after": "                continue",
        "run": ["scripts/recheck.py"],
    },
    {
        "why": "根拠の行を数の境界なしで見る"
               "（確定値600が記事の1600Gを根拠にする・Codex21回目）",
        "file": "scripts/recheck.py",
        "before": "        return any(all(_cv_rc.token_in_quote(tk, line) "
                  "for tk in _tk)",
        "after": "        return any(all(tk in line for tk in _tk)",
        "run": ["scripts/recheck.py"],
    },
    {
        "why": "けた区切りのカンマを数の境界と見ない"
               "（600 が 1,600G に一致する・Codex21回目）",
        "file": "scripts/confirmed_values.py",
        "before": "        if (before and before in \"0123456789.,\") \\",
        "after": "        if (before and before in \"0123456789.\") \\",
        "run": ["scripts/confirmed_values.py"],
    },
    {
        "why": "話題が決まっていない項目を黙って飛ばす"
               "（fail-closed にならず、正しい記事を止める・Codex21回目）",
        "file": "scripts/recheck.py",
        "before": "                return _result(ERROR, f\"確定値の項目の話題が決まっていません: \"\n                                      f\"{_e_tp}\", args)",
        "after": "                continue",
        "run": ["scripts/recheck.py"],
    },
    {
        "why": "文頭・文末の数字を照合できなくする"
               "（2AIが正しく確定した値が記録できない・2026-08-25）",
        "file": "scripts/confirmed_values.py",
        "before": "        if (before and before in \"0123456789.,\") \\",
        "after": "        if (before in \"0123456789.,\") \\",
        "run": ["scripts/confirmed_values.py"],
    },
    {
        "why": "確定値の項目に、記事の話題を決めなくてよくする"
               "（reset・純増が根拠にならず、正しい記事を止める・Codex20回目）",
        "file": "scripts/recheck.py",
        "before": "                _tp = _cv_rc.topic_of(_b)",
        "after": "                _tp = \"spec\"",
        "run": ["scripts/recheck.py"],
    },
    {
        "why": "見出しだけで投稿欄が残っていると判定する"
               "（レビューが付いた機種が全部、出典に使えなくなる・台帳#473）",
        "file": "scripts/user_area.py",
        "before": "            if _hit or len(body) >= _PEND_MIN:",
        "after": "            if True:",
        "run": ["scripts/user_area.py"],
    },
    {
        "why": "見出しの下を「次の見出しまで」で数える"
               "（末尾が投稿欄のページでfooterの文字を拾う・台帳#473）",
        "file": "scripts/user_area.py",
        "before": "                if self.pend and len(self.stack) < self.pend[3]:",
        "after": "                if False:",
        "run": ["scripts/user_area.py"],
    },
    {
        "why": "記事ページ自身の無効化を検査が見ない"
               "（新台が作った瞬間に必ず監査46で落ちる・台帳#469）",
        "file": "scripts/recheck.py",
        "before": "    _by_page = page_disables_pochipochi(mh, _machine(slug) or {})",
        "after": "    _by_page = False",
        "run": ["scripts/recheck.py"],
    },
    {
        "why": "止めた理由より先に材料の注意書きを見る"
               "（見張りがいちばん無害な理由を報告する・台帳#474）",
        "file": "scripts/add_machine_run.py",
        "before": "    hit = _pick(res.get(\"blocked\"))",
        "after": "    hit = \"\"",
        "run": ["scripts/add_machine_run.py"],
    },
    {
        "why": "復旧が空の機種ディレクトリを監査より後に消す"
               "（強制終了のあと復旧が永久に詰まり、新台公開が全部止まる）",
        "file": "scripts/publish_new_machine.py",
        "before": "        if os.path.isdir(_d0) and not os.listdir(_d0):",
        "after": "        if False:",
        "run": ["scripts/publish_new_machine.py"],
    },
    {
        "why": "復旧の退避ファイルを『まだある物』として数える"
               "（監査が孤児ディレクトリと言い、復旧が自分の後始末を取り消す）",
        "file": "scripts/publish_new_machine.py",
        "before": "        _only_held = bool(_in_dir) and _in_dir <= _held_names",
        "after": "        _only_held = False",
        "run": ["scripts/publish_new_machine.py"],
    },
    {
        "why": "写しへの向け直しを1つ漏らす"
               "（試験が本物のリポジトリを汚し、夜の公開が丸ごと止まる）",
        "file": "scripts/publish_new_machine.py",
        "before": "        if (_k4.isupper() and isinstance(_v4, str)",
        "after": "        if (_k4.isupper() and _k4 != \"DETAILS\" "
                 "and isinstance(_v4, str)",
        "run": ["scripts/publish_new_machine.py"],
    },
    {
        "why": "閉じ忘れた投稿欄を確定しない"
               "（ページが途中で終わると投稿欄を見逃す・Codex18回目）",
        "file": "scripts/user_area.py",
        "before": "        _p.close_all()",
        "after": "        pass",
        "run": ["scripts/user_area.py"],
    },
    {
        "why": "画面に出ない要素の中身まで読む"
               "（template の中を見て正常なページを止める・Codex18回目）",
        "file": "scripts/user_area.py",
        "before": "            if self.hidden:\n"
                  "                if tag not in _VOID:",
        "after": "            if False:\n"
                 "                if tag not in _VOID:",
        "run": ["scripts/user_area.py"],
    },
    {
        "why": "話題まるごと免除に戻す"
               "（根拠のない断定が同じ箱に紛れると素通り・Codex18回目）",
        "file": "scripts/recheck.py",
        "before": "        _left = [x for x in _nonempty\n                 if x not in _isnote and not _identity_line(x)\n                 and not _backed(x, topic)]",
        "after": "        _left = [] if any(_backed(x, topic)\n                          for x in _nonempty) else _nonempty",
        "run": ["scripts/recheck.py"],
    },
    {
        "why": "確定値の語を「どれか1つ」で免除する"
               "（無関係な断定が短い語の一致だけで通る・Codex19回目）",
        "file": "scripts/recheck.py",
        "before": "        return any(all(_cv_rc.token_in_quote(tk, line) "
                  "for tk in _tk)",
        "after": "        return any(any(_cv_rc.token_in_quote(tk, line) "
                 "for tk in _tk)",
        "run": ["scripts/recheck.py"],
    },
    {
        "why": "確定値を話題で分けない"
               "（別の話題の値で免除される・Codex19回目）",
        "file": "scripts/recheck.py",
        "before": "                   for _tk in _by_topic.get(topic) or [])",
        "after": "                   for _v in _by_topic.values() for _tk in _v)",
        "run": ["scripts/recheck.py"],
    },
    {
        "why": "「未確認」で始まる箱を丸ごと免除する"
               "（2行目の断定が素通り・Codex19回目）",
        "file": "scripts/recheck.py",
        "before": "        if _isnote and len(_isnote) == len(_nonempty) and not _tbl_bad:",
        "after": "        if _isnote and len(_isnote) == len(_nonempty):",
        "run": ["scripts/recheck.py"],
    },
    {
        "why": "画面に出ない中の自己終了タグを読む"
               "（template の中の input だけで正常なページを止める・Codex19回目）",
        "file": "scripts/user_area.py",
        "before": "            if self.hidden:\n"
                  "                return\n"
                  "            self._look(tag, attrs)     # <img /> の形（積まない）",
        "after": "            self._look(tag, attrs)     # <img /> の形（積まない）",
        "run": ["scripts/user_area.py"],
    },
    {
        "why": "障害注入が発火しなくても合格にする"
               "（手前で止まって巻き戻しを一度も試さない・Codex19回目）",
        "file": "scripts/publish_new_machine.py",
        "before": "            if need_fire and not _seen[\"fired\"]:",
        "after": "            if False:",
        "run": ["scripts/publish_new_machine.py"],
    },
    {
        # ★2026-08-29に方針が変わった★＝DMMの名乗りだけの免除は消した。
        #   ★いまの免除は「この話題の確定値の語が全部そろう行」だけ★なので、
        #   そこを「どれか1つでも当たれば免除」に緩める形で壊す。
        "why": "確定値の語が1つ当たれば免除する（根拠のない断定が通る）",
        "file": "scripts/recheck.py",
        "before": "        return any(all(_cv_rc.token_in_quote(tk, line) "
                  "for tk in _tk)",
        "after": "        return any(any(_cv_rc.token_in_quote(tk, line) "
                 "for tk in _tk)",
        "run": ["scripts/recheck.py"],
    },
    {
        "why": "試験用の偽の機種を掃除しない（2026-08-24・自分で踏んだ）",
        "file": "scripts/publish_new_machine.py",
        "before": "        if apply_it:\n"
                  "            _sh.rmtree(d, ignore_errors=True)",
        "after": "        if False:\n"
                 "            _sh.rmtree(d, ignore_errors=True)",
        # ★この1本だけ4分ほどかかる★（本番と同じ経路を丸ごと通すため）
        "run": ["scripts/publish_new_machine.py"],
    },
    {
        "why": "★確かめられなかったものを緑にする★"
               "（2026-09-04に実際にこれをやり、"
               "読めない中身に入れた本物の鍵が終了コード0で通った）",
        "file": "scripts/backup_guard.py",
        "before": "        (known if ok else fresh).append((rel, findings))",
        "after": ("        (known if ok else"
                  " (known if unverifiable else fresh))"
                  ".append((rel, findings))"),
        "run": ["scripts/backup_guard.py"],
    },
    {
        "why": "★調べられなかったフォルダがあっても緑にする★"
               "（cmd_accept は断るのに cmd_scan だけ素通りしていた）",
        "file": "scripts/backup_guard.py",
        "before": ('        _log(f"scan: ★読めないフォルダ'
                   ' {len(walk_ng)} 件のため非0★")\n        return 1'),
        "after": ('        _log(f"scan: ★読めないフォルダ'
                  ' {len(walk_ng)} 件のため非0★")'),
        "run": ["scripts/backup_guard.py"],
    },
    {
        "why": "★ZIPの中の読めない要素を、名前だけで飛ばす★"
               "（外側ZIPに鍵入りPDFを1つ入れるだけで通っていた）",
        "file": "scripts/backup_guard.py",
        "before": ('                    out.append(\n'
                   '                        f"content:ZIP内 {nm} を'
                   '読めないので確かめられません")\n'
                   "                    continue"),
        "after": ("                    if nm.lower().endswith"
                  "(('.pdf', '.png')):\n"
                  "                        continue\n"
                  '                    out.append(\n'
                  '                        f"content:ZIP内 {nm} を'
                  '読めないので確かめられません")\n'
                  "                    continue"),
        "run": ["scripts/backup_guard.py"],
    },
    {
        "why": "★承知済みの「読めないファイル」の指紋を比べない★"
               "（無害な状態で承知させ、中身を鍵入りに差し替えると通った）",
        "file": "scripts/backup_guard.py",
        "before": ('              and str(want.get("sha256") or "")'
                   " == sha)"),
        "after": ('              and (unverifiable or'
                  ' str(want.get("sha256") or "") == sha))'),
        "run": ["scripts/backup_guard.py"],
    },
    {
        "why": "★UTF-32 の印を UTF-16 と取り違える★"
               "（文字の間のNUL検査まで免除され、鍵が検知0件になっていた）",
        "file": "scripts/backup_guard.py",
        "before": "            text = _decode_wide(raw)",
        "after": "            text = _decode_utf16(raw)",
        "run": ["scripts/backup_guard.py"],
    },
    {
        "why": "★入れ子ZIPが先だと、その後ろを検査しない★",
        "file": "scripts/backup_guard.py",
        "before": ('                    out.append("content:ZIPの中にZIPが'
                   'あるので確かめられません"\n'
                   '                               f"（{nm}）")\n'
                   "                    continue"),
        "after": ('                    out.append("content:ZIPの中にZIPが'
                  'あるので確かめられません"\n'
                  '                               f"（{nm}）")\n'
                  "                    return out"),
        "run": ["scripts/backup_guard.py"],
    },
    {
        "why": "★指紋が取れないものを「同じ」と見なす★"
               "（空文字どうしを「変わっていない」と読む）",
        "file": "scripts/backup_guard.py",
        "before": '              and sha != ""',
        "after": '              and True',
        "run": ["scripts/backup_guard.py"],
    },
    {
        "why": "★名前が「/」で終わる要素を、中身を見ずに外す★"
               "（ZIPは `名前/` にも中身を書けるので完全に素通りした）",
        "file": "scripts/backup_guard.py",
        "before": ("            infos = [zi for zi in z.infolist()\n"
                   "                     if not (zi.is_dir() and"
                   " zi.file_size == 0)]"),
        "after": ("            infos = [zi for zi in z.infolist()\n"
                  '                     if not zi.filename.endswith("/")]'),
        "run": ["scripts/backup_guard.py"],
    },
    {
        "why": "★ZIPの中で UTF-32 を UTF-16 と取り違える★"
               "（外側だけ直して中に同じ穴が残る）",
        "file": "scripts/backup_guard.py",
        "before": "                    txt = _decode_wide(data)",
        "after": "                    txt = _decode_utf16(data)",
        "run": ["scripts/backup_guard.py"],
    },
    {
        "why": "★ZIPかどうかを、文字として読めるかより後に見る★"
               "（UTF-8として読める小さなZIPが一度も開かれなかった）",
        "file": "scripts/backup_guard.py",
        "before": "        _zf0 = _zip_findings(path, raw)",
        "after": "        _zf0 = None",
        "run": ["scripts/backup_guard.py"],
    },
    {
        "why": "★ZIPの名前の一巡をやめる★"
               "（件数・サイズ・読み取りの手前で名前を見なくなる）",
        "file": "scripts/backup_guard.py",
        "before": ("            for _zi in z.infolist():\n"
                   "                _b = _zip_base(_zi.filename)"),
        "after": ("            for _zi in []:\n"
                  "                _b = _zip_base(_zi.filename)"),
        "run": ["scripts/backup_guard.py"],
    },
    {
        "why": "★形で許す名簿に、拒否名を上書きさせる★"
               "（gmail_config_SKILL.md が名前検査を飛ばす）",
        "file": "scripts/backup_guard.py",
        "before": "        return not name_findings(basename)",
        "after": "        return True",
        "run": ["scripts/backup_guard.py"],
    },
    {
        "why": "★UTF-8の印（BOM）を落とさずに中身を見る★"
               "（BOM付きJSONが「JSONでない」と判定される）",
        "file": "scripts/backup_guard.py",
        "before": '    _txt = text.lstrip("\\ufeff").strip()',
        "after": "    _txt = text.strip()",
        "run": ["scripts/backup_guard.py"],
    },
    {
        "why": "★フォルダのつなぎを黙って飛ばす★"
               "（つなぎしか無い場所を「検知なし」で通す）",
        "file": "scripts/backup_guard.py",
        "before": ('                        bad.append('
                   'f"フォルダのつなぎは中を見ていません: {full}")'),
        "after": "                        pass",
        "run": ["scripts/backup_guard.py"],
    },
    {
        "why": "★Windowsのジャンクションを「つなぎ」と見なさない★"
               "（islink では偽になるが os.walk は入ってしまう）",
        "file": "scripts/backup_guard.py",
        "before": ('        _ij = getattr(os.path, "isjunction", None)\n'
                   "        if _ij is not None and _ij(path):\n"
                   "            return True"),
        "after": "        pass",
        "run": ["scripts/backup_guard.py"],
    },
    {
        "why": "★走査先そのものがつなぎでも記録しない★"
               "（どこを見ているのか残らない）",
        "file": "scripts/backup_guard.py",
        "before": ('            bad.append('
                   'f"走査先そのものがフォルダのつなぎです: {root}")'),
        "after": "            pass",
        "run": ["scripts/backup_guard.py"],
    },
    {
        "why": "★ジャンクションを見分けられないPythonでも緑にする★"
               "（見分けられないことを『つなぎではない』と答える）",
        "file": "scripts/backup_guard.py",
        "before": ('    return os.name == "nt" and not '
                   'hasattr(os.path, "isjunction")'),
        "after": "    return False",
        "run": ["scripts/backup_guard.py"],
    },
    {
        "why": "★見分けられないのに走査を始めてしまう★"
               "（親を指すつなぎで入り続け、止まる場所へ届かない）",
        "file": "scripts/backup_guard.py",
        "before": "        return iter(())\n    try:",
        "after": "    try:",
        "run": ["scripts/backup_guard.py"],
    },
    {
        "why": "保存名の案内を出さない（台帳#464の再発）",
        "file": "scripts/backup_guard.py",
        "before": '        findings.append("allowlist:リスト外" + hint)',
        "after": '        findings.append("allowlist:リスト外")',
        "run": ["scripts/backup_guard.py"],
    },
    # ─── 2026-08-27・機械割の範囲を設定別の値から書く ───────────
    {
        "why": "★読めない値を黙って飛ばす（残った値だけで範囲を作る）★",
        "file": "scripts/page_decision.py",
        "before": "            return None               "
                  "# ★読めない値が1つでもあれば作らない★",
        "after": "            continue",
        "run": ["scripts/build_new_article.py"],
    },
    {
        "why": "★設定1つだけでも「範囲」と書く（同じ値を2度並べる）★",
        "file": "scripts/page_decision.py",
        "before": "    if len(nums) < 2:",
        "after": "    if len(nums) < 1:",
        "run": ["scripts/build_new_article.py"],
    },
    {
        "why": "★2AIの確定値からも範囲を作る"
               "（裏付けが話題をまたぎ、判定書と記事が食い違う）★",
        "file": "scripts/page_decision.py",
        "before": '    if got.get("_from") == "confirmed_values":',
        "after": "    if False:",
        # ★通しの試験だけが捕まえる★＝判定書・記事・検査を繋いだ時だけ矛盾する
        "run": ["scripts/recheck.py"],
    },
    {
        "why": "★範囲を検索の濃さにも数える（同じ表から2件＝水増し）★",
        "file": "scripts/page_decision.py",
        "before": '    if count_confirmed and "payout_range" not in got:',
        "after": '    if "payout_range" not in got:',
        "run": ["scripts/page_decision.py"],
    },
    {
        "why": "★設定別の出玉率が基本スペックに出ることを忘れる"
               "（判定書が未確認と言い、記事が書く）★",
        "file": "scripts/page_decision.py",
        "before": '            if c == "payout_rate":',
        "after": "            if False:",
        "run": ["scripts/page_decision.py", "scripts/recheck.py"],
    },
    # ─── 2026-08-27・台帳#485 待ち行列の新台へ記録できるか ───────
    {
        "why": "★空のURLも当てる（DMM待ちの機種へ誤って結び付く）★",
        "file": "scripts/pending_machines.py",
        "before": "    if not want:\n        return None\n    for it in",
        "after": "    for it in",
        "run": ["scripts/pending_machines.py"],
    },
    {
        "why": "★待ち行列を鍵で探す形へ戻す"
               "（2AIが決めた値を新台へ一件も記録できない）★",
        "file": "scripts/confirmed_values.py",
        "before": "        _hit = _pm.find_by_url(_pm.load(), official_url)",
        "after": "        _hit = (_pm.load().get(\"items\") or {}).get(official_url)",
        "run": ["scripts/confirmed_values.py"],
    },
    # ─── 2026-08-27・台帳#487 節の外（表・要約・リード文）を直せる ───
    {
        "why": "★節の外を数え直しに入れない"
               "（表の数値が消えても素通りする）★",
        "file": "scripts/decide_now.py",
        # ★目印は前の行ごと取る★（字下げ違いの同じ行に部分一致するため）
        "before": "        elif kind in OUTSIDE_KINDS:",
        "after": "        elif False:",
        "run": ["scripts/decide_now.py"],
    },
    {
        "why": ("★部分置換の検査を、指定した範囲だけで済ませる"
                "（数値を含まない書き換えで係り先を黙って変えられる）★"),
        "file": "scripts/decide_now.py",
        "before": "    s, t = _unit_of(e, i, j)",
        "after": "    s, t = (i, j)",
        "run": ["scripts/decide_now.py"],
    },
    {
        "why": "★部分置換で、同じ文字が2か所にあっても直す★",
        "file": "scripts/decide_now.py",
        "before": "    if len(hit) > 1:",
        "after": "    if False:",
        "run": ["scripts/decide_now.py"],
    },
    {
        "why": "★部分置換で、一文をまたぐ指定を許す★",
        "file": "scripts/decide_now.py",
        "before": '    if any(ch in _UNIT_END for ch in b[:-1]):',
        "after": "    if False:",
        "run": ["scripts/decide_now.py"],
    },
    {
        "why": "★書き戻す欄を取り違える（隣の欄を壊す）★",
        "file": "scripts/decide_now.py",
        "before": ("        d[\"factTable\"][i1][i2] = "
                   "_put(d[\"factTable\"][i1][i2])"),
        "after": ("        d[\"factTable\"][i1][0] = "
                  "_put(d[\"factTable\"][i1][i2])"),
        "run": ["scripts/decide_now.py"],
    },
    # ─── 2026-08-27・Codexのレビュー（更新タスク）で塞いだ穴 ────
    {
        "why": "★記事に無い言葉を書き足せる"
               "（意味の反転・新しい事実が素通りする）★",
        "file": "scripts/decide_now.py",
        "before": "            new_w = [w for w in _words(a[\"after\"]) "
                  "if w not in _blob]",
        "after": "            new_w = []",
        "run": ["scripts/decide_now.py"],
    },
    {
        "why": "★同じ文字が2か所にあっても場所を言わせない"
               "（表を直す決定が本文を変える）★",
        "file": "scripts/decide_now.py",
        "before": "            if len(spots) > 1:",
        "after": "            if False:",
        "run": ["scripts/decide_now.py"],
    },
    {
        "why": "★機種の名前を検査しない（置き場の外を書き換えられる）★",
        "file": "scripts/decide_now.py",
        "before": "    if not _SLUG_OK.fullmatch(s):",
        "after": "    if False:",
        "run": ["scripts/decide_now.py"],
    },
    # ─── 2026-08-27・Codexのレビュー（記録・育成）────────────────
    {
        "why": "★封をした判定を、合意のときに取り直さない"
               "（答えを見てから書き換えられる＝2AI一致が自己申告）★",
        "file": "scripts/repair_journal.py",
        "before": "    if _now != _want:",
        "after": "    if False:",
        "run": ["scripts/repair_journal.py"],
    },
    {
        "why": "★決まらなかった回のあと、はじめへ戻さない"
               "（2回目に入れず、その件が永久に止まる）★",
        "file": "scripts/repair_journal.py",
        "before": '    rec["state"] = "DETECTED"',
        "after": "    pass",
        "run": ["scripts/repair_journal.py"],
    },
    {
        "why": "★記事の指紋なしでも記録できる"
               "（後段の照合が丸ごと働かない）★",
        "file": "scripts/repair_journal.py",
        "before": '    if len(str(source_sha256 or "")) != 64:',
        "after": "    if False:",
        "run": ["scripts/repair_journal.py"],
    },
    {
        "why": "★壊れた記録を黙って外す"
               "（途中の直しが一覧から消えて誰も気づけない）★",
        "file": "scripts/repair_journal.py",
        # ★一覧に出さない形に壊す★（それが本当の穴の姿）
        #   ★直す前は `_broken` の行を狙っていた★ので、
        #   中身が空になるだけで BROKEN としては出続け、
        #   守りが効いたままなのに「守られていない」と報告された。
        "before": '            out.append({"state": "BROKEN", '
                  '"finding_id": n[:-5],\n'
                  '                        "slug": "", "check": "", "quote": "",\n'
                  '                        "_broken": '
                  'f"{type(e).__name__}: {str(e)[:80]}"})',
        "after": "            pass",
        "run": ["scripts/repair_journal.py"],
    },
    {
        "why": "★判定書が壊れた機種を黙って外す"
               "（その機種だけ永久に育たない）★",
        "file": "scripts/grow_machine.py",
        "before": "            if broken is not None:",
        "after": "            if False:",
        "run": ["scripts/grow_machine.py"],
    },
    {
        "why": "★合意した操作と、当てる操作を突き合わせない"
               "（無害な合意を別の書き換えの許可証にできる）★",
        "file": "scripts/decide_now.py",
        "before": "    _b = _agreement_problem(slug, dec)",
        "after": "    _b = None",
        "run": ["scripts/decide_now.py"],
    },
    {
        "why": "★合意が、打ち直した操作の配列を受け取る"
               "（決定ファイルと結び付かない）★",
        "file": "scripts/repair_journal.py",
        "before": "    elif isinstance(ops, list):",
        "after": "    elif False:",
        "run": ["scripts/repair_journal.py"],
    },
    {
        "why": "★何もしていない完了に印を付けない"
               "（作業0件の正常終了を誰も数えられない）★",
        "file": "scripts/task_guard.py",
        "before": '        e["no_work"] = not _mine',
        "after": '        e["no_work"] = False',
        "run": ["scripts/task_guard.py"],
    },
    # ─── 2026-08-27・Codexの2回目（作った守り自体の穴）────────────
    {
        "why": "★出どころの逐語が実在するか見ない"
               "（架空の逐語で新語の検査を抜けられる）★",
        "file": "scripts/decide_now.py",
        "before": "                if _src_w and _src_w not in published:",
        "after": "                if False:",
        "run": ["scripts/decide_now.py"],
    },
    {
        "why": "★場所を種類でしか数えない"
               "（同じ本文に同じ行が2つあると先頭が黙って変わる）★",
        "file": "scripts/decide_now.py",
        "before": '        got += ["本文" for x in (sec.get("body") or []) '
                  "if x == before]",
        "after": '        got += ["本文"] if any(x == before '
                 'for x in (sec.get("body") or [])) else []',
        "run": ["scripts/decide_now.py"],
    },
    {
        "why": "★記録が読めないときに通す（合意の検査を丸ごと外せる）★",
        "file": "scripts/decide_now.py",
        "before": "    if _bk:",
        "after": "    if False:",
        "run": ["scripts/decide_now.py"],
    },
    {
        "why": "★指摘された一文を触らない決定でも合意できる"
               "（押し切ったのに直っていない件ができ、誰も直せなくなる）★",
        "file": "scripts/repair_journal.py",
        "before": "        if not _touch:",
        "after": "        if False:",
        "run": ["scripts/repair_journal.py"],
    },
    {
        "why": "★検査が落ちていなくても、押し切った件を開け直せる"
               "（合格した直しを、あとから開け直せてしまう）★",
        "file": "scripts/repair_journal.py",
        "before": "    if rec.get(\"state\") in _after and _recheck_failing(rec):",
        "after": "    if rec.get(\"state\") in _after:",
        "run": ["scripts/repair_journal.py"],
    },
    {
        "why": "★判断していないのに回数を数える"
               "（封もCodexもせずに3回呼べば人へ回せる）★",
        "file": "scripts/repair_journal.py",
        "before": '    elif rec.get("state") != "CODEX_RECEIVED":',
        "after": "    elif False:",
        "run": ["scripts/repair_journal.py"],
    },
    {
        "why": "★決定ファイルの指紋が空でも通す"
               "（同じ記事を見て作られたかを確かめない）★",
        "file": "scripts/repair_journal.py",
        "before": "    if not _s:",
        "after": "    if False:",
        "run": ["scripts/repair_journal.py"],
    },
    {
        "why": "★形が正しければ中身を見ない"
               "（空の記録・知らない版が黙って一覧から消える）★",
        "file": "scripts/repair_journal.py",
        "before": "        _why = _broken_why(rec)",
        "after": '        _why = ""',
        "run": ["scripts/repair_journal.py"],
    },
    {
        "why": "★何もしていない印を、その日全体で数える"
               "（別のタスクが書いた日は見落とす）★",
        "file": "scripts/task_guard.py",
        "before": '                 if r.get("task") == task '
                  'and r.get("date") == _today()]',
        "after": '                 if r.get("date") == _today()]',
        "run": ["scripts/task_guard.py"],
    },
    # ─── 2026-08-27・Codexの3回目（骨組み・指紋・判断者）─────────
    {
        "why": "★判断者を件数だけで見る（同じ名前2つでも2AI扱い）★",
        "file": "scripts/decide_now.py",
        "before": "    if not isinstance(by, list) or "
                  "len({str(x).strip().lower()\n"
                  "                                        for x in by "
                  "if str(x).strip()}) < 2:",
        # ★件数だけで見る形（同じ名前2つでも通る）★
        "after": "    if not isinstance(by, list) or "
                 "len([str(x).strip().lower()\n"
                 "                                        for x in by "
                 "if str(x).strip()]) < 2:",
        "run": ["scripts/decide_now.py"],
    },
    {
        "why": "★記事の指紋を必須にしない（いつの記事への判断か分からない）★",
        "file": "scripts/decide_now.py",
        "before": "    if len(_s) != 64 or any(c not in "
                  '"0123456789abcdef" for c in _s.lower()):',
        "after": "    if False:",
        "run": ["scripts/decide_now.py"],
    },
    {
        "why": "★合意の指紋を操作だけにする"
               "（あとから numbers_removed を足して免除できる）★",
        "file": "scripts/repair_journal.py",
        "before": "                 ops=ops, ops_sha256=decision_digest(_dec_raw),",
        "after": "                 ops=ops, ops_sha256=ops_digest(ops),",
        "run": ["scripts/repair_journal.py"],
    },
    {
        "why": "★Codexへ渡した材料の指紋を必須にしない★",
        "file": "scripts/repair_journal.py",
        "before": "    if len(_m) != 64 or any(c not in "
                  '"0123456789abcdef" for c in _m.lower()):',
        "after": "    if False:",
        "run": ["scripts/repair_journal.py"],
    },
    {
        "why": "★段階ごとの必須欄を見ない（中身が空の合意が健康扱い）★",
        "file": "scripts/repair_journal.py",
        "before": "    _need = {",
        # ★{} or {…} は元の辞書のまま★＝何も壊れない（実際にそうなっていた）
        "after": "    _need = {} and {",
        "run": ["scripts/repair_journal.py"],
    },
    {
        "why": "★合意の指紋に「誰が決めたか」を入れない"
               "（合意後に判断者を書き換えられる）★",
        "file": "scripts/repair_journal.py",
        "before": '                 "decided_by")',
        "after": "                 )",
        "run": ["scripts/repair_journal.py"],
    },
    {
        "why": "★決定ファイルの判断者と突き合わせない"
               "（記録と決定で誰が決めたかが食い違う）★",
        "file": "scripts/repair_journal.py",
        "before": "        if not _same_deciders(_dec_raw.get"
                  '("decided_by"), decided_by):',
        "after": "        if False:",
        "run": ["scripts/repair_journal.py"],
    },
    {
        "why": "★言い回しが変わっても2AIに聞かない"
               "（意味の反転が黙って通る）★",
        "file": "scripts/decide_now.py",
        "before": '            if _wording(a["before"]) != _wording(a["after"]):',
        "after": "            if False:",
        "run": ["scripts/decide_now.py"],
    },
    {
        "why": "★理由が空でも通す（2AIが判断した記録が残らない）★",
        "file": "scripts/decide_now.py",
        "before": "                if len(_mw) < 15:",
        "after": "                if False:",
        "run": ["scripts/decide_now.py"],
    },
    {
        "why": "★行き詰まったら、2AIに聞かずすぐ人へ回す"
               "（人が来るまで止まったまま）★",
        "file": "scripts/grow_machine.py",
        "before": "    if n < STUCK_ASK_LIMIT:",
        "after": "    if False:",
        "run": ["scripts/grow_machine.py"],
    },
    {
        "why": "★うまく育っても回数を0に戻さない"
               "（昔の失敗を数え続け、すぐ人へ回す）★",
        "file": "scripts/grow_machine.py",
        "before": "        _stuck_clear(slug)\n"
                  '        return {"do": "ok"}',
        "after": '        return {"do": "ok"}',
        "run": ["scripts/grow_machine.py"],
    },
    {
        "why": "★出どころと数値の付き先を照らさない"
               "（出どころと逆の対応で書ける）★",
        "file": "scripts/decide_now.py",
        "before": "                if _miss_p:",
        "after": "                if False:",
        "run": ["scripts/decide_now.py"],
    },
    {
        "why": "★設定示唆の注記を、材料に保存された古い一覧から決める"
               "（6段すべて載せている表に「掲載していません」と書く）★",
        "file": "scripts/build_new_article.py",
        "before": '        un = _missing_labels(material, got["value"], key)',
        "after": '        un = material.get("setting_labels_unconfirmed") or []',
        "run": ["scripts/build_new_article.py"],
    },
    {
        "why": "★ボーナス確率の表の注記も、古い一覧から決める"
               "（載せている設定を「載せていない」と書く）★",
        "file": "scripts/build_new_article.py",
        "before": '        _un_bp = _missing_labels(material, _bp["value"], "bonus_prob")',
        "after": '        _un_bp = material.get("setting_labels_unconfirmed") or []',
        "run": ["scripts/build_new_article.py"],
    },
    {
        "why": "★設定の名簿が無いとき、項目をまたいだ古い一覧で代用する"
               "（この表に無い設定を、少なく言う側に外れる）★",
        "file": "scripts/build_new_article.py",
        "before": '        if material.get("setting_labels_unconfirmed") is not None:',
        "after": "        if False:",
        "run": ["scripts/build_new_article.py"],
    },
    {
        "why": "★記事に触っていないコミットにも照合を求める"
               "（対話セッションの直しが push できず、夜の手当てが届かない）★",
        "file": "scripts/pre_push_check.py",
        "before": "        if p.startswith(\"assets/data/machine-details/\") \\",
        "after": "        if True or p.startswith(\"assets/data/machine-details/\") \\",
        "run": ["scripts/pre_push_check.py"],
    },
    {
        "why": "★関所が自分の出力の文字の扱いを固定しない"
               "（Windowsの既定では合格の記号が書けず、"
               "検査が通っているのに push が拒否される）★",
        "file": "scripts/pre_push_check.py",
        "before": '        _s.reconfigure(encoding="utf-8", errors="replace")',
        "after": "        pass",
        "run": ["scripts/pre_push_check.py"],
    },
    {
        "why": "★読者に出ない項目まで「材料あり」と数える"
               "（中身ゼロのページが黙って公開される）★",
        "file": "scripts/add_machine_run.py",
        "before": "           if _cv.topic_of(k)}",
        "after": "           if k != \"model_code\"}",
        "run": ["scripts/add_machine_run.py"],
    },
    {
        "why": "★写している間に消えたファイルで、写しごと失敗する"
               "（同時に別の作業をしていると、試験が丸ごと落ちる）★",
        "file": "scripts/publish_new_machine.py",
        "before": "    except FileNotFoundError:\n        return dst",
        "after": "    except ZeroDivisionError:\n        return dst",
        "run": ["scripts/publish_new_machine.py"],
    },
    {
        "why": "★重複として通した消し方が、同じ文を全部消せる"
               "（数値の無い事実が記事から丸ごと消える）★",
        "file": "scripts/decide_now.py",
        "before": "        if _have - _n < 1:",
        "after": "        if False:",
        "run": ["scripts/decide_now.py"],
    },
    {
        "why": "★合図を確かめずに「自分のサーバー」と言う"
               "（同時に動かすと、他人の応答を自分のものと数える）★",
        "file": "scripts/render_check.py",
        "before": "        if nonce in _read(log_path):",
        "after": "        if True:",
        "run": ["scripts/render_check.py"],
    },
    {
        "why": "★新しい枝を送るとき、範囲の始まりに「向こうに無い」を使う"
               "（git が読めず、その push を一切検査できない）★",
        "file": "scripts/pre_push_check.py",
        "before": "            out.append(local_sha)"
                  "          # 新しい枝＝そのコミットまで全部",
        "after": "            out.append(f\"{remote_sha}..{local_sha}\")",
        "run": ["scripts/pre_push_check.py"],
    },
    {
        "why": "★数値の並びが変わっても通す"
               "（ラベルの中の数字で係り先が空になり、入れ替えが素通りする）★",
        "file": "scripts/decide_now.py",
        "before": "                if [n for _w, n in _sb] != [n for _w, n in _sa]:",
        "after": "                if False:",
        "run": ["scripts/decide_now.py"],
    },
    {
        "why": "★消す先を毎回もとの記事から探す"
               "（同じ「消す」を2件並べると、2件やったと報告して1件しか消さない）★",
        "file": "scripts/decide_now.py",
        "before": "            _sp = drop_spot(d, a[\"text\"], used=_used_drop)",
        "after": "            _sp = drop_spot(d, a[\"text\"])",
        "run": ["scripts/decide_now.py"],
    },
    {
        "why": "★係り先を「内容の文字だけ」で比べる"
               "（ひらがな・数字が落ちて、対応の入れ替えが黙って通る）★",
        "file": "scripts/decide_now.py",
        "before": '    return "".join(str(a or "").split())',
        "after": '    return "".join(_words(a))',
        "run": ["scripts/decide_now.py"],
    },
    {
        "why": "★消してよいかを、別の入れ物の重複で数える"
               "（別条件の事実を、よその節の重複を根拠に消せる）★",
        "file": "scripts/decide_now.py",
        "before": "    body = ((d.get(\"sections\") or [])[si] or {}).get(\"body\") or []\n"
                  "    return sum(1 for x in body if x == text)",
        "after": "    return sum(1 for sec in (d.get(\"sections\") or [])\n"
                 "               for x in (sec.get(\"body\") or []) if x == text)",
        "run": ["scripts/decide_now.py"],
    },
    {
        "why": "★締切ちょうどを「まだ来ていない」と読む（1分ぶん取りこぼす）★",
        "file": "scripts/task_guard.py",
        "before": "    return now >= dl",
        "after": "    return now > dl",
        "run": ["scripts/task_guard.py"],
    },
    {
        "why": "★相談のときに締切を見ない（締切を越えて相談し続ける）★",
        "file": "scripts/task_guard.py",
        "before": "    if past_deadline(_dl, now_hhmm):",
        "after": "    if False:",
        "run": ["scripts/task_guard.py"],
    },
    {
        "why": "★締切を時刻の文字だけで比べる"
               "（夜11時半のタスクが、朝7時20分の締切を"
               "「もう過ぎた」と判定して、毎晩30分なにもできない）★",
        "file": "scripts/task_guard.py",
        "before": "    if now_evening and not dl_evening:",
        "after": "    if False and not dl_evening:",
        "run": ["scripts/task_guard.py"],
    },
    {
        "why": "★朝に、前の晩の締切を「まだ来ていない」と読む"
               "（締切が効かなくなり、朝まで書き換え続ける）★",
        "file": "scripts/task_guard.py",
        "before": "    if (not now_evening) and dl_evening:",
        "after": "    if False and dl_evening:",
        "run": ["scripts/task_guard.py"],
    },
    {
        "why": "★同じ日に何度動いても数え続ける"
               "（2AIが検討していなくても人へ回る）★",
        "file": "scripts/grow_machine.py",
        "before": '    if add and rec.get("day") != today:',
        "after": "    if add:",
        "run": ["scripts/grow_machine.py"],
    },
    {
        "why": "★係り先を直前の1語で見る"
               "（通常時とリセット時が同じ『天井』になり、逆に書ける）★",
        "file": "scripts/decide_now.py",
        "before": "        ws = [txt[_prev:m.start()].strip()]",
        "after": "        ws = _words(txt[:m.start()])",
        "run": ["scripts/decide_now.py"],
    },
    {
        "why": "★消すときに、数値を伏せた部分一致で見る"
               "（数値だけ違う行が残れば消せる）★",
        "file": "scripts/decide_now.py",
        "before": '                lost = [] if _dup_count(d, a["text"]) '
                  '>= 2 else nums',
        "after": '                lost = [] if _wording(a["text"]) '
                 'in _wording(raw) else nums',
        "run": ["scripts/decide_now.py"],
    },
    {
        "why": "★骨組みで符号を見ない（+500枚→-500枚が同じ値に見える）★",
        "file": "scripts/decide_now.py",
        "before": '        _SHAPE_RE = _re3.compile(r"[-−▲△+＋]?'
                  '\\d+(?:\\.\\d+)?")',
        "after": '        _SHAPE_RE = _re3.compile(r"\\d+(?:\\.\\d+)?")',
        "run": ["scripts/decide_now.py"],
    },
    {
        "why": "★出どころの係り先をゆるく照らす"
               "（別条件の値を持ち込める・条件を落として一般化できる）★",
        "file": "scripts/decide_now.py",
        "before": "    return any(_slot_key(q[0]) == key and q[1] == p[1] "
                  "for q in src_pairs)",
        "after": "    return any(q[1] == p[1] for q in src_pairs)",
        "run": ["scripts/decide_now.py"],
    },
    {
        "why": "★2AIに基本情報表を見せない（食い違いに気づけない）★",
        "file": "scripts/decide_now.py",
        # ★2行まとめて置き換える★（1行だけ切ると構文エラーになり、
        #   「ただ落ちただけ」になって守りの証拠にならない）
        "before": "        \"factTable\": [list(r) for r in "
                  "(d.get(\"factTable\") or [])\n"
                  "                      if isinstance(r, (list, tuple))],",
        "after": "        \"factTable\": [],",
        "run": ["scripts/decide_now.py"],
    },

    # ─── 2026-08-30・台帳を毎朝のタスクへ戻す（ledger_sweep）────────
    #   ★Codexの指摘で作り直した★＝語の名簿で自動的に閉じるのをやめ、
    #     2AIが名指しした検査を機械が全部やり直す形にした。
    {
        "why": "★検査を1つも渡されなくても通す（空で閉じられる）★",
        "file": "scripts/ledger_sweep.py",
        "before": "    if not checks and not texts:\n"
                  "        return False, [\"確かめる検査が1件もありません\"]",
        "after": "    if not checks and not texts:\n        return True, []",
        "run": ["scripts/ledger_sweep.py"],
    },
    {
        "why": "★1件でも通らなければ閉じない、をやめる"
               "（片方だけ確かめて閉じる＝#284の型）★",
        "file": "scripts/ledger_sweep.py",
        "before": "        whys.append(f\"{'○' if ok else '×'} "
                  "text_gone[{t[:30]}] ／ {why}\")\n"
                  "        if not ok:\n"
                  "            return False, whys",
        "after": "        whys.append(f\"{'○' if ok else '×'} "
                 "text_gone[{t[:30]}] ／ {why}\")\n"
                 "        if not ok:\n            pass",
        "run": ["scripts/ledger_sweep.py"],
    },
    {
        "why": "★案件の機種を見ない"
               "（別機種の存在しない文で、どの案件でも閉じられる）★",
        "file": "scripts/ledger_sweep.py",
        "before": "    if str(row.get(\"slug\") or \"\") != slug:",
        "after": "    if False:",
        "run": ["scripts/ledger_sweep.py"],
    },
    {
        "why": "★閉じている案件をもう一度閉じられる／"
               "存在しない番号でも進む★",
        "file": "scripts/ledger_sweep.py",
        "before": "    if row is None:\n"
                  "        return False, f\"#{issue_id} という案件がありません\"",
        "after": "    if row is None:\n        return True, \"\"",
        "run": ["scripts/ledger_sweep.py"],
    },
    {
        "why": "★文体の検査だけで閉じる"
               "（19通りの文末しか見ていないのに「直った」にする）★",
        "file": "scripts/ledger_sweep.py",
        "before": "NEED_COMPANION = (\"plain_style_gone\",)",
        "after": "NEED_COMPANION = ()",
        "run": ["scripts/ledger_sweep.py"],
    },
    {
        "why": "★観測どまりの検査でも閉じる★",
        "file": "scripts/ledger_sweep.py",
        "before": "        if not meta.get(\"closeable\"):",
        "after": "        if False:",
        "run": ["scripts/ledger_sweep.py"],
    },
    {
        "why": "★案件に書かれていない逐語でも閉じる"
               "（でたらめな文字列でどの案件でも閉じられる）★",
        "file": "scripts/ledger_sweep.py",
        "before": "    bad = [t for t in texts if t not in body]",
        "after": "    bad = []",
        "run": ["scripts/ledger_sweep.py"],
    },
    {
        "why": "★裏取り待ちの案件を、文が消えただけで閉じる"
               "（載せるのをやめただけかもしれないのに）★",
        "file": "scripts/ledger_sweep.py",
        "before": 'TEXT_GONE_NOT_ENOUGH = ("external_value",)',
        "after": "TEXT_GONE_NOT_ENOUGH = ()",
        "run": ["scripts/ledger_sweep.py"],
    },

    # ─── 2026-08-30・一覧の狙い目を既定表示にそろえる（align_strategy）───
    {
        "why": "★一覧が交換率を名乗っていても数値を替える"
               "（呼び名と中身が食い違う・実測8機種）★",
        "file": "scripts/align_strategy.py",
        "before": "    named = rate_words(ck, strat)\n"
                  "    if named:",
        "after": "    named = rate_words(ck, strat)\n    if False:",
        "run": ["scripts/align_strategy.py"],
    },
    {
        "why": "★当たる枠が複数で値が割れていても、どれかを選んで書く★",
        "file": "scripts/align_strategy.py",
        "before": "        if len(vals) != 1:",
        "after": "        if False:",
        "run": ["scripts/align_strategy.py"],
    },
    {
        "why": "★もう既定の値になっている数値も書き換える"
               "（＝2回目に走らせると値が壊れる・2026-08-30に実測3機種）★",
        "file": "scripts/align_strategy.py",
        "before": "        if aligned:",
        "after": "        if False:",
        "run": ["scripts/align_strategy.py"],
    },
    {
        "why": "★区切りのモードを見ずに、全部の枠から値だけで探す"
               "（別のモードの枠に引き寄せられる）★",
        "file": "scripts/align_strategy.py",
        "before": '        here = [s for s in sl if s["mode"] and s["mode"] in seg]',
        "after": "        here = list(sl)",
        "run": ["scripts/align_strategy.py"],
    },
    {
        "why": "★「もう揃っている」と「まだずれている」が両方成り立っても書く"
               "（同じモードの中で値が交差していると古い数値が残る）★",
        "file": "scripts/align_strategy.py",
        "before": "        if cand and aligned:",
        "after": "        if False:",
        "run": ["scripts/align_strategy.py"],
    },
    {
        "why": "★区切りから決まるモードが1つでなくても書く"
               "（別のモードの枠から書き換えられる）★",
        "file": "scripts/align_strategy.py",
        "before": "        if len(keys) != 1:",
        "after": "        if not keys:",
        "run": ["scripts/align_strategy.py"],
    },
    {
        "why": "★小数も受け取る（350.5 を黙って 350G に切り捨てる）★",
        "file": "scripts/align_strategy.py",
        "before": "    return v if type(v) is int else None",
        "after": "    return v if isinstance(v, (int, float)) else None",
        "run": ["scripts/align_strategy.py"],
    },
    {
        "why": "★数字の境目を見ない（12345G の後ろ4桁に食いつく）★",
        "file": "scripts/align_strategy.py",
        "before": 'GNUM = re.compile(r"(?<!\\d)(\\d{1,4})(?!\\d)\\s*G")',
        "after": 'GNUM = re.compile(r"(\\d{1,4})\\s*G")',
        "run": ["scripts/align_strategy.py"],
    },
    {
        "why": "★G が付いていない数値まで書き換える（周期・スルー回数）★",
        "file": "scripts/align_strategy.py",
        "before": 'GNUM = re.compile(r"(?<!\\d)(\\d{1,4})(?!\\d)\\s*G")',
        "after": 'GNUM = re.compile(r"(?<!\\d)(\\d{1,4})(?!\\d)")',
        "run": ["scripts/align_strategy.py"],
    },
    # ─── 2026-08-31・目次表（台帳#523）────────────────────────────
    {
        "why": "★新台・preview でも道具が目次に並ぶことにする"
               "（隠れている箱への行が出て、押しても飛べない）★",
        "file": "scripts/audit_render.py",
        "before": '    hide_tools = cls in ("AUTO_INDEXABLE", "AUTO_PENDING",'
                  ' "LEGACY_PREVIEW")',
        "after": "    hide_tools = False",
        "run": ["scripts/audit_render.py"],
    },
    {
        "why": "★目次の並びを見ない"
               "（節が落ちても、目次と本文が同じように壊れれば通る）★",
        "file": "scripts/audit_render.py",
        "before": "    if got != want:\n"
                  '        return [f"R14: 目次の中身が違います',
        "after": "    if False:\n"
                 '        return [f"R14: 目次の中身が違います',
        "run": ["scripts/audit_render.py"],
    },
    {
        "why": "★飛び先が実在するかを見ない（押しても動かない行が出る）★",
        "file": "scripts/audit_render.py",
        "before": '        if not it.get("exists"):',
        "after": "        if False:",
        "run": ["scripts/audit_render.py"],
    },
    {
        "why": "★目次そのものが見えていなくても通す（opacity:0 等）★",
        "file": "scripts/audit_render.py",
        "before": '    if not toc.get("block_shown"):\n'
                  '        return [f"R14: 目次が読者に見えていません',
        "after": '    if False:\n'
                 '        return [f"R14: 目次が読者に見えていません',
        "run": ["scripts/audit_render.py"],
    },
    # ─── 2026-08-31・CI再現の道具が「嘘の赤」を出さないこと ──────
    {
        "why": "★python を動かしている行を読み飛ばす"
               "（その検査を飛ばしたまま『全部通りました』と言う）★",
        "file": "scripts/ci_repro.py",
        "before": "        if _RUNS_PY.search(head):",
        "after": "        if False:",
        "run": ["scripts/ci_repro.py"],
    },
    {
        "why": "★引用符を自分で切る（本物のワークフローの行で、"
               "引用符ごと引数に渡る＝すでにCIと違うものを動かす）★",
        "file": "scripts/ci_repro.py",
        "before": "        toks = shlex.split(raw, posix=True)",
        "after": "        toks = raw.split()",
        "run": ["scripts/ci_repro.py"],
    },
    {
        "why": "★再現できないシェルの書き方を、黙って切って通す"
               "（パイプ・入力のリダイレクト・複数コマンド）★",
        "file": "scripts/ci_repro.py",
        "before": "    return not r\n\n\ndef _parse",
        "after": "    return True\n\n\ndef _parse",
        "run": ["scripts/ci_repro.py"],
    },
    {
        "why": "★引数に混ざったシェルの記号を見ない"
               "（`python a.py|b` のように空白が無い形が素通りする）★",
        "file": "scripts/ci_repro.py",
        "before": "        if any(ch in a for ch in _META):",
        "after": "        if False:",
        "run": ["scripts/ci_repro.py"],
    },
    {
        "why": "★argv を作れない行を黙って通す（検査を飛ばしたのに緑になる）★",
        "file": "scripts/ci_repro.py",
        "before": '    if len(args) < 2 or args[0] != "python":',
        "after": "    if False:",
        "run": ["scripts/ci_repro.py"],
    },
    # ─── 2026-08-31・GitHubの検査を見る道具（番人が毎朝使う）──────
    {
        "why": "★自分の出力の文字の扱いを固定しない"
               "（Windowsの既定では合格の記号が書けず、"
               "緑でも赤でも毎回「見に行けなかった」になる）★",
        "file": "scripts/ci_status.py",
        "before": '        _s.reconfigure(encoding="utf-8", errors="replace")',
        "after": "        pass",
        "run": ["scripts/ci_status.py"],
    },
    {
        "why": "★utf-8 ではなく cp932 に固定する"
               "（例外は出ないが印が ? になる＝名前どおりの保証にならない）★",
        "file": "scripts/ci_status.py",
        "before": '        _s.reconfigure(encoding="utf-8", errors="replace")',
        "after": '        _s.reconfigure(encoding="cp932", errors="replace")',
        "run": ["scripts/ci_status.py"],
    },
    {
        "why": "★番人が毎朝呼ぶ点検が、自分の出力の文字の扱いを固定しない"
               "（他人の取り込みの副作用に寄りかかった状態へ戻る）★",
        "file": "scripts/add_machine_health.py",
        "before": '        _s.reconfigure(encoding="utf-8", errors="replace")',
        "after": "        pass",
        "run": ["scripts/add_machine_health.py"],
    },
    # ─── 2026-08-31・手作業のコミットの記録（台帳#527）──────────
    {
        "why": "★理由が無くても手作業のコミットを記録する"
               "（何のために通したのか、あとから誰にも分からなくなる）★",
        "file": "scripts/task_guard.py",
        "before": '    if len(str(why or "").strip()) < 10:',
        "after": "    if False:",
        "run": ["scripts/task_guard.py"],
    },
    {
        "why": "★手作業の記録を読まない"
               "（記録して通す道が塞がり、--no-verify に戻る）★",
        "file": "scripts/pre_push_check.py",
        "before": '    for r in (data.get("manual_commits") or []):',
        "after": "    for r in []:",
        "run": ["scripts/pre_push_check.py"],
    },
    # ─── 2026-08-31・base タグを構造で見る（実際に事故を起こした）──
    {
        "why": "★base タグを構造で見ない"
               "（コメントの中の文字列を実タグと誤認し、"
               "120ページから base が消える／2個でも通る）★",
        "file": "scripts/html_check.py",
        "before": '    if bases != ["/"]:',
        "after": "    if False:",
        "run": ["scripts/html_check.py"],
    },
    {
        "why": "★ふつうの表を公開データに残さない"
               "（許可値には通るのに、表が消えて節ごと落ちる）★",
        "file": "scripts/gates.py",
        "before": '        if new.get("type") == "table":',
        "after": "        if False:",
        "run": ["scripts/gates.py"],
    },
    {
        "why": "★ふつうの表のセルが文字かを見ない（辞書が公開データに入る）★",
        "file": "scripts/gates.py",
        "before": "        if not all(_is_str(c) for c in cells):",
        "after": "        if False:",
        "run": ["scripts/gates.py"],
    },
    {
        "why": "★新台経路の機種まで表へ移す"
               "（毎晩のタスクが作り直すので戻る／夜の公開を止めうる）★",
        "file": "scripts/tableize_spec.py",
        "before": "        if slug in auto:",
        "after": "        if False:",
        "run": ["scripts/tableize_spec.py"],
    },
    {
        "why": "★「出典が読めなかった」を「材料が無い」より後に見る"
               "（読めない晩まで黙って、29回失敗しても無音になる）★",
        "file": "scripts/add_machine_run.py",
        "before": '    ("材料のページを取れません", "SOURCE_FETCH_FAILED"),',
        "after": '    ("__使わない__", "SOURCE_FETCH_FAILED"),',
        "run": ["scripts/add_machine_health.py"],
    },
    {
        "why": "★廃止した決まり文句を比べる単位に数える"
               "（その文を持つ既存記事が永久に育たなくなる・実害2機種）★",
        "file": "scripts/grow_machine.py",
        "before": "        if t in RETIRED_BOILERPLATE:",
        "after": "        if False:",
        "run": ["scripts/grow_machine.py"],
    },
    {
        "why": "★まだ作っている文を免除していないかを見ない"
               "（本物の情報が消えても気づかない）★",
        "file": "scripts/grow_machine.py",
        "before": "        if t in src:",
        "after": "        if False:",
        "run": ["scripts/grow_machine.py"],
    },
    {
        "why": "★文を落とすとき、完全な一文かを見ない"
               "（文の途中で切って意味を壊す）★",
        "file": "scripts/decide_now.py",
        "before": "    if len(spans) != 1:",
        "after": "    if False:",
        "run": ["scripts/decide_now.py"],
    },
    {
        "why": "★文を落としたあと、要素が空になっても通す★",
        "file": "scripts/decide_now.py",
        "before": "    if not out.strip():",
        "after": "    if False:",
        "run": ["scripts/decide_now.py"],
    },
    {
        "why": "★文の範囲が、元の文字を丸ごと切り出せるかを見ない"
               "（1文字も変えていないことの証明にならない）★",
        "file": "scripts/style_check.py",
        "before": "            if t[start:i + 1].strip():",
        "after": "            if t[start:i + 1].strip() and False:",
        "run": ["scripts/style_check.py"],
    },
    {
        "why": "★行を分けるとき、元のセルと一致するかを見ない"
               "（分けるついでに文字を書き換えられる）★",
        "file": "scripts/decide_now.py",
        "before": "        if joined != src:",
        "after": "        if False:",
        "run": ["scripts/decide_now.py"],
    },
    {
        "why": "★分けたあとの列の数を見ない（表が崩れる）★",
        "file": "scripts/decide_now.py",
        "before": "        if not isinstance(r, (list, tuple)) or len(r) != ncol:",
        "after": "        if False:",
        "run": ["scripts/decide_now.py"],
    },
    {
        "why": "★行の場所の書き方を確かめない（別の場所を書き換えられる）★",
        "file": "scripts/decide_now.py",
        "before": "    m = _ROW_AT.match(str(where or \"\"))",
        "after": "    m = _ROW_AT.match(\"sections[0].tables[0].rows[0]\")",
        "run": ["scripts/decide_now.py"],
    },
    {
        "why": "★基本スペックの本文と表を、別のものとして数える"
               "（表へ移した瞬間に13機種が永久に育たなくなる）★",
        "file": "scripts/grow_machine.py",
        "before": "            _sr = spec_row(t) if title == SPEC_TITLE else None",
        "after": "            _sr = None",
        "run": ["scripts/grow_machine.py"],
    },
    {
        "why": "★表の基本スペックだけ別の形で数える（同上・表側）★",
        "file": "scripts/grow_machine.py",
        "before": "                if title == SPEC_TITLE and len(cells) == 2 \\",
        "after": "                if False and len(cells) == 2 \\",
        "run": ["scripts/grow_machine.py"],
    },
    {
        "why": "★再検査で、基本スペックの表を本文と同じに見ない"
               "（機種名や未確認セルまで『根拠がない』と誤検知する）★",
        "file": "scripts/recheck.py",
        "before": '        if title == "基本スペック":',
        "after": "        if False:",
        "run": ["scripts/recheck.py"],
    },
    {
        "why": "★新台の基本スペックを表で作らない（要望どおりにならない）★",
        "file": "scripts/build_new_article.py",
        "before": '        "title": "基本スペック", "type": "table",',
        "after": '        "title": "基本スペック", "type": "settei",',
        "run": ["scripts/build_new_article.py"],
    },
    # ─── 2026-08-31・Codexの13回目で入れた守り ──────────────────
    {
        "why": "★書くのに、どこへ書くかを言わせない"
               "（--apply だけで60機種すべてが書き換わる）★",
        "file": "scripts/tableize_spec.py",
        "before": "    if apply and not slugs and not want_all:",
        "after": "    if False:",
        "run": ["scripts/tableize_spec.py"],
    },
    {
        "why": "★機種の書き方を見ない（置き場の外のファイルを書ける）★",
        "file": "scripts/tableize_spec.py",
        "before": "        if bad:\n            return f\"★機種の書き方が違います: {bad}★\"",
        "after": "        if False:\n            return f\"★機種の書き方が違います: {bad}★\"",
        "run": ["scripts/tableize_spec.py"],
    },
    {
        "why": "★前の失敗が残した退避を上書きする"
               "（戻すための控えを自分で壊す）★",
        "file": "scripts/tableize_spec.py",
        "before": "            if os.path.exists(bak):\n                raise RuntimeError(",
        "after": "            if False:\n                raise RuntimeError(",
        "run": ["scripts/tableize_spec.py"],
    },
    # ─── 2026-08-31・Codexの11〜12回目で入れた守り ──────────────
    {
        "why": "★書いたあと読み直して確かめない"
               "（書いた内容とファイルに残った内容は別物）★",
        "file": "scripts/tableize_spec.py",
        "before": "        bad = [slug for p, slug, after, tmp in staged if _load(p) != after]",
        "after": "        bad = []",
        "run": ["scripts/tableize_spec.py"],
    },
    {
        "why": "★失敗しても元に戻さない"
               "（置き換えた分だけが変わったまま残る）★",
        "file": "scripts/tableize_spec.py",
        "before": "                shutil.copy2(bak, p)",
        "after": "                pass",
        "run": ["scripts/tableize_spec.py"],
    },
    {
        "why": "★変換前の本文が空でも通す（空の表を作れる）★",
        "file": "scripts/tableize_spec.py",
        "before": """    if not isinstance(body, list) or not body:
        return "変換前の本文が、空でない配列ではありません\"""",
        "after": """    if False:
        return "変換前の本文が、空でない配列ではありません\"""",
        "run": ["scripts/tableize_spec.py"],
    },
    {
        "why": "★変換後の全体を期待値と比べない"
               "（対象の節の中は何をしても通る）★",
        "file": "scripts/tableize_spec.py",
        "before": "    if after != expected:",
        "after": "    if False:",
        "run": ["scripts/tableize_spec.py"],
    },
    {
        "why": "★旧基準値の重複を見ない"
               "（[A,B,B] と [A,B] が一致して乗り換えが通る）★",
        "file": "scripts/style_check.py",
        "before": "            if len(was) != len(items):",
        "after": "            if False:",
        "run": ["scripts/style_check.py"],
    },
    {
        "why": "★乗り換えで、材料が基準値と同じかを見ない"
               "（丸ごと入れ替わっても通る）★",
        "file": "scripts/style_check.py",
        "before": "            if mine != was:",
        "after": "            if False:",
        "run": ["scripts/style_check.py"],
    },
    {
        "why": "★木の比較で class を見ない"
               "（settei-table と data-table を取り違えても通る）★",
        "file": "scripts/audit_render.py",
        "before": '    if got.get("cls") != want.get("cls"):',
        "after": "    if False:",
        "run": ["scripts/audit_render.py"],
    },
    {
        "why": "★木の比較で文字を見ない"
               "（<th>A</th><td>B</td> と <th>AB</th><td></td> が同じになる）★",
        "file": "scripts/audit_render.py",
        "before": "        if got != want:",
        "after": "        if False:",
        "run": ["scripts/audit_render.py"],
    },
    {
        "why": "★木の比較で属性（href/colspan）を見ない★",
        "file": "scripts/audit_render.py",
        "before": '    if (got.get("at") or {}) != (want.get("at") or {}):',
        "after": "    if False:",
        "run": ["scripts/audit_render.py"],
    },
    # ─── 2026-08-31・文体の印（強調の記号を外す）────────────────
    {
        "why": "★印から強調の記号を外さない"
               "（<strong> を ** へ直すたびに違反が湧いて赤くなる）★",
        "file": "scripts/style_check.py",
        "before": '    t = _STRONG.sub("**", str(text or ""))',
        "after": '    t = str(text or "")',
        "run": ["scripts/style_check.py"],
    },
    {
        "why": "★印から文そのものを外す（どの文も同じ印＝違反の入れ替えが通る）★",
        "file": "scripts/style_check.py",
        "before": '    return _SPACES.sub(" ", t).strip()',
        "after": '    return ""',
        "run": ["scripts/style_check.py"],
    },
    {
        "why": "★乗り換えで、材料と基準値の件数が合うかを見ない★",
        "file": "scripts/style_check.py",
        "before": "            if len(items) != len(rows):",
        "after": "            if False:",
        "run": ["scripts/style_check.py"],
    },
    # ─── 2026-08-31・移す道具と、足りていなかった壊し方 ──────────
    {
        "why": "★gates が行の列数を見ない"
               "（見出しとずれた表が公開データに入る）★",
        "file": "scripts/gates.py",
        "before": "        if len(cells) != len(headers):",
        "after": "        if False:",
        "run": ["scripts/gates.py"],
    },
    {
        "why": "★新台の関所が表の行の列数を見ない"
               "（入口の層が抜ける・3層のうち1層）★",
        "file": "scripts/publish_new_machine.py",
        "before": "                bad_rows = [i for i, r in enumerate(tb[\"rows\"]) if len(r) != w]",
        "after": "                bad_rows = []",
        "run": ["scripts/publish_new_machine.py"],
    },
    {
        "why": "★変換前の節の形（題と本文だけ）を確かめない★",
        "file": "scripts/tableize_spec.py",
        "before": "    if set(src.keys()) != _SECTION_KEYS_BEFORE:",
        "after": "    if False:",
        "run": ["scripts/tableize_spec.py"],
    },
    {
        "why": "★下見の中身を、変換前の本文と突き合わせない"
               "（下見を偽れば、偽の値を書ける）★",
        "file": "scripts/tableize_spec.py",
        "before": "    if again != [list(r) for r in rows_in]:",
        "after": "    if False:",
        "run": ["scripts/tableize_spec.py"],
    },
    {
        "why": "★太字でない行まで自動で移す"
               "（文章の途中の「：」で切って、文章を表にする）★",
        "file": "scripts/tableize_spec.py",
        "before": '_BOLD = re.compile(r"^\\*\\*(?P<label>[^*]+)\\*\\*：(?P<value>.*)$")',
        "after": '_BOLD = re.compile(r"^\\*?\\*?(?P<label>[^：]+)\\*?\\*?：(?P<value>.*)$")',
        "run": ["scripts/tableize_spec.py"],
    },
    {
        "why": "★本文に文字列でない要素があっても黙って進む（中身が消える）★",
        "file": "scripts/tableize_spec.py",
        "before": "    if not all(isinstance(x, str) for x in body):",
        "after": "    if False:",
        "run": ["scripts/tableize_spec.py"],
    },
    # ─── 2026-08-31・ふつうの表（要望③の土台）────────────────────
    {
        "why": "★ふつうの表の中身を見ない"
               "（セルの辞書・列数のずれが素通りし、読者が列を取り違える）★",
        "file": "scripts/audit_public.py",
        "before": '        if s.get("type") == "table":\n'
                  "            problems += _table_body_problems(slug, i, s)",
        "after": "        if False:\n            pass",
        "run": ["scripts/audit_public.py"],
    },
    {
        "why": "★表のセルが文字かを見ない（辞書がそのまま画面に出る）★",
        "file": "scripts/audit_public.py",
        "before": "            if not all(isinstance(c, str) for c in cells):",
        "after": "            if False:",
        "run": ["scripts/audit_public.py"],
    },
    {
        "why": "★列の数を見ない（見出しと行がずれた表を公開する）★",
        "file": "scripts/audit_public.py",
        "before": "            if len(cells) != len(heads):",
        "after": "            if False:",
        "run": ["scripts/audit_public.py"],
    },
    # ─── 2026-08-31・文体（です・ます）の検査 ────────────────────
    {
        "why": "★名簿に無い常体を見逃す形へ戻す"
               "（「…となる。」のような言い方が素通りする）★",
        "file": "scripts/style_check.py",
        "before": "    return bool(_OK_TAIL.search(t))",
        "after": "    return True",
        "run": ["scripts/style_check.py"],
    },
    {
        "why": "★入れ替えを見つけない（古い違反を直して別に入れれば"
               "件数が同じまま通る＝走るたびに表記が変わる）★",
        "file": "scripts/style_check.py",
        "before": "    new = sorted(set(now) - want)",
        "after": "    new = []",
        "run": ["scripts/style_check.py"],
    },
    {
        "why": "★同じ節に同じ文が2回あっても1つに畳む"
               "（2件目を足しても、片方を直しても集合が変わらない）★",
        "file": "scripts/style_check.py",
        "before": """                      # ★同じ文が同じ節に何度も出るときの通し番号★
                      str(row.get("nth") or 1)])""",
        "after": """                      # ★同じ文が同じ節に何度も出るときの通し番号★
                      ""])""",
        "run": ["scripts/style_check.py"],
    },
    {
        "why": "★基準値が無いときに黙って作る"
               "（消してから実行すると、新しい違反を丸ごと取り込める）★",
        "file": "scripts/style_check.py",
        "before": "    if not os.path.isfile(path) and not init:",
        "after": "    if False:",
        "run": ["scripts/style_check.py"],
    },
    {
        "why": "★base の置き場所を見ない"
               "（template の中や body の base を『入っている』と数え、"
               "実タグが入らなくなる）★",
        "file": "scripts/html_check.py",
        "before": '            if _names[-1:] == ["head"] and "template" not in _names:',
        "after": "            if True:",
        "run": ["scripts/html_check.py"],
    },
    {
        "why": "★gitが答えなかったことを『違う』に潰す"
               "（時間切れでも『祖先ではない』と読んで素通りする）★",
        "file": "scripts/task_guard.py",
        "before": "        return GIT_UNKNOWN, f\"{type(e).__name__}: {e}\"",
        "after": "        return 1, f\"{type(e).__name__}: {e}\"",
        "run": ["scripts/task_guard.py"],
    },
    {
        "why": "★lead と表の注記を見ない（読者に出る文章の一部が対象外）★",
        "file": "scripts/style_check.py",
        "before": '    _check_text(out, slug, "lead", detail.get("lead"))',
        "after": "    pass",
        "run": ["scripts/style_check.py"],
    },
    {
        "why": "★括弧の中の「。」でも切る"
               "（文でない断片が『体言止め』に見え、数が水増しされる）★",
        "file": "scripts/style_check.py",
        "before": "        if ch == \"。\" and depth == 0:",
        "after": "        if ch == \"。\":",
        "run": ["scripts/style_check.py"],
    },
    {
        "why": "★ラベルと値の行まで文体を求める"
               "（表へ移すべき行が毎日「直せ」と出続ける）★",
        "file": "scripts/style_check.py",
        "before": "    return bool(_LABEL.match(t))",
        "after": "    return False",
        "run": ["scripts/style_check.py"],
    },
]


_SCORE = re.compile(r"(\d+)\s*/\s*(\d+)\s*合格")
_SCORE_ANY = __import__("re").compile(r"(\d+)\s*/\s*(\d+)")


def _run_tests(root: str, scripts: list) -> tuple:
    """その写しで試験を流す。

    返すもの: (1つでも赤いか, どの試験がなぜ赤いか)
    ★理由を返す★＝「壊す前から赤い」とだけ言われても原因に迫れない
      （2026-08-23に実際そうなって、切り分けに時間を使った）。
    """
    # ★★子の文字コードを必ず指定する★★（2026-08-23・実際に踏んだ）
    #   Windowsの既定は cp932 なので、試験が出す「✅」で子が落ちる。
    #   ＝★守りが壊れていなくても赤くなる★ので、道具の判定が全部無意味になる。
    #   手で試すときは PYTHONIOENCODING を付けていたので通り、
    #   道具から呼ぶと落ちる、という食い違いになっていた。
    # ★★読み込み済みファイル（__pycache__）を作らせない★★
    #   （2026-08-27・実際に踏んだ）
    #   写しは1つを使い回すので、前の試験が残した読み込み済みファイルが
    #   次の壊し方に持ち越される。Pythonは **元の日時（秒）と大きさ** だけで
    #   置き換えを判断するので、★大きさが変わらない壊し方（「2」→「1」など）を
    #   同じ秒のうちに書くと、古いものがそのまま使われる★
    #   ＝壊したのに壊れておらず、「守られていません」と誤って報告する。
    #   ★同じ壊し方が、単独だと捕まえ、まとめて回すと捕まえない★という
    #   再現しない答えになり、道具そのものが信用できなくなる。
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
               PYTHONDONTWRITEBYTECODE="1")
    for rel in scripts:
        r = subprocess.run([sys.executable, os.path.join(root, rel),
                            "--selftest"],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", cwd=root,
                           env=env)
        if r.returncode != 0:
            out = (r.stdout or "") + (r.stderr or "")
            # ★★試験の書き方は1つではない★★（2026-08-27）
            #   ★直す前は「❌」で始まる行しか見ていなかった★ので、
            #   「NG 」で書く試験（repair_journal / confirmed_values など）の
            #   失敗を**全部「ただ落ちただけ」に分類**していた。
            #   ＝守られているのに「守られていない」と報告する。
            ng = [x for x in out.splitlines()
                  if x.startswith("❌") or x.startswith("NG ")
                  or x.startswith("失敗: ")]
            # ★★どう捕まえたのかを区別する★★（2026-08-24・Codexの3回目の指摘2）
            #   ★直す前は「終了コードが0以外＝捕まえた」だけだった★ので、
            #   壊し方が**構文エラーになっただけ**でも合格に見えた。
            #   ＝「その守りを見ている試験がある」証拠にならない。
            #   ★試験が❌を出したのか、ただ落ちたのかを必ず表に出す★。
            if ng:
                return "試験が❌", f"{rel}: {ng[0][:70]}"
            # ★★「N/M 合格」も試験の失敗★★（2026-08-24・Codexの19回目）
            #   ★直す前は ❌ で始まる行しか見ていなかった★ので、
            #   「83/84 合格」と出している**本物の試験の失敗**まで
            #   「ただ落ちただけ」に分類していた（20件中18件がこれ）。
            #   ＝道具の分類が雑で、質の判定が信用できなくなっていた。
            for line in out.splitlines():
                m = _SCORE.search(line)
                if m and int(m.group(1)) < int(m.group(2)):
                    return "試験が❌", f"{rel}: {line.strip()[:70]}"
                # ★「N/M 不合格」も試験の失敗★（2026-08-27）
                #   ★合格の形だけ見ていた★ので、
                #   不合格と書いてある行を読み落としていた。
                if "不合格" in line and _SCORE_ANY.search(line):
                    return "試験が❌", f"{rel}: {line.strip()[:70]}"
            why = (out.strip().splitlines() or [""])[-1][:70]
            return "落ちただけ", f"{rel}: {why}"
    return "", ""


# ★時間のかかる試験★（1本で4分ほど＝本番と同じ経路を丸ごと通すため）
#   CIでは外し、手元の通し確認で回す。
SLOW = ("scripts/publish_new_machine.py",)


def _rmtree_hard(path) -> None:
    """★読み取り専用でも消す★（2026-08-28・実測で15GB溜まっていた）

    Windows では `.git` の中に読み取り専用のファイルがあるので、
    ふつうの消し方は失敗する。`ignore_errors=True` だと
    ★黙って失敗して、写しが溜まり続ける★（実測: 500件超・15GB）。
    """
    import os as _os_r
    import shutil as _sh_r
    import stat as _st_r

    def _force(func, p, _exc):
        try:
            _os_r.chmod(p, _st_r.S_IWRITE)
            func(p)
        except Exception:                  # noqa: BLE001
            pass

    try:
        _sh_r.rmtree(path, onerror=_force)
    except Exception:                      # noqa: BLE001
        pass


def check(only: str = "", fast: bool = False, only_index=None) -> int:
    tmp = tempfile.mkdtemp(prefix="mut_")
    ng, weak, skipped = [], [], []
    # ★★写しは1つだけ作って使い回す★★（2026-08-23）
    #   ★直す前は壊し方の数だけ丸ごと複製していた★ので、
    #   11回の複製で不安定になり、**全部が「壊す前から赤い」**になった
    #   （道具の判定が信用できない状態＝直したい病気そのもの）。
    #   1つ作って、壊したファイルを毎回**元の中身へ戻す**ほうが速くて確実。
    root = os.path.join(tmp, "work")
    # ★★.git も一緒に写す★★（2026-08-24・壊し方12を足したら判明）
    #   ★直す前は .git を除いていた★ので、
    #   git に問い合わせる検査（監査29の「追跡ファイルの一覧」など）が
    #   **壊す前から赤い**状態になり、その守りを一切確かめられなかった。
    #   ＝★写しが本物と違うと、道具そのものが役に立たなくなる★。
    #   22MB ほどなので、写す方を選ぶ。
    # ★★追跡ファイルだけを写す★★（2026-09-01・Codexのレビュー31の指摘3）
    #   ★はじめは「除外の名簿」にしたが、それは `.gitignore` と一致しない★＝
    #   claim-evidence/raw ／ machines_prev.json ／ x_post_result.json ／
    #   cc.log・as.log ／ ロック類 が、いまだに写しへ入っていた。
    #   ＝★手元にだけある物で壊し方が通る★型を、完全には塞げていなかった。
    #   ★`git ls-files` は index を読むだけで、本体の index を変えない★。
    #   ★`.git` は別に丸ごと写す★＝gitに問い合わせる検査が
    #   「壊す前から赤い」にならないため。
    _ls = subprocess.run(["git", "-C", BASE, "ls-files", "-z"],
                         capture_output=True)
    if _ls.returncode != 0:
        raise SystemExit("★写しを作れません（git ls-files が失敗）★")
    _rels = [x for x in _ls.stdout.decode("utf-8").split("\0") if x]
    if len(_rels) < 100:
        # ★少なすぎるのは、写しが本物と違う状態★（fail-closed）
        raise SystemExit(f"★追跡ファイルが {len(_rels)} 件しかありません★")
    os.makedirs(root, exist_ok=True)
    for _rel in _rels:
        _src = os.path.join(BASE, _rel)
        if not os.path.isfile(_src):
            continue               # index にあるが手元に無い（消した直後など）
        _dst = os.path.join(root, _rel)
        os.makedirs(os.path.dirname(_dst), exist_ok=True)
        shutil.copy2(_src, _dst)
    shutil.copytree(os.path.join(BASE, ".git"), os.path.join(root, ".git"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    try:
        _want = [x.strip() for x in str(only or "").split(",") if x.strip()]
        tried = 0
        for i, m in enumerate(MUTATIONS, 1):
            if only_index is not None and i not in only_index:
                continue
            if _want and not any(w in m["why"] for w in _want):
                continue
            tried += 1
            if fast and any(x in SLOW for x in m["run"]):
                print(f"  --   {i}. {m['why']}（★時間がかかるので飛ばした★）")
                skipped.append(m["why"])
                continue
            p = os.path.join(root, m["file"])
            src = open(p, encoding="utf-8").read()
            if src.count(m["before"]) != 1:
                print(f"  ★ND {i}. {m['why']}"
                      f"（目印が {src.count(m['before'])} 件）")
                ng.append(m["why"] + "（目印が見つからない）")
                continue
            # ★★壊す前に、その写しで試験が通ることを確かめる★★
            #   （2026-08-23・作った直後に自分で踏んだ）
            #   ★直す前は「終了コードが0以外＝捕まえた」としていた★ので、
            #   写しの環境エラー（コピーから外したフォルダ等）まで
            #   「捕まえた」と数えていた。＝★道具自身が嘘をつく★。
            #   壊す前が赤いなら、その結果は何の証拠にもならない。
            _red, _why = _run_tests(root, m["run"])
            if _red:
                print(f"  ★ND {i}. {m['why']}"
                      "（★壊す前から赤い＝この写しでは確かめられない★）")
                print(f"        {_why}")
                ng.append(m["why"] + "（壊す前から赤い）")
                continue
            open(p, "w", encoding="utf-8", newline="\n").write(
                src.replace(m["before"], m["after"], 1))
            try:
                caught, _cwhy = _run_tests(root, m["run"])
            finally:
                # ★★必ず元の中身へ戻す★★（次の壊し方に持ち越さない）
                open(p, "w", encoding="utf-8", newline="\n").write(src)
            print(("  OK   " if caught == "試験が❌" else "  ★NG ")
                  + f"{i}. {m['why']}"
                  + (f"  ［{caught}］" if caught else ""))
            if caught == "落ちただけ":
                # ★★合格に数えない★★（2026-08-24・Codexの19回目）
                #   ★直す前は合格に数えていた★ので、
                #   構文エラー・環境エラーで落ちただけのものが
                #   「その守りを見ている試験がある」証拠に化けていた。
                weak.append(f"{i}. {m['why']}（{_cwhy[:60]}）")
                ng.append(m["why"] + "（試験が❌を出していない＝ただ落ちただけ）")
            elif not caught:
                ng.append(m["why"])
    finally:
        _rmtree_hard(tmp)          # ★読み取り専用でも消す★
    print()
    if skipped:
        # ★飛ばしたことを黙らない★（「全部OK」に見せない）
        print(f"★{len(skipped)}件は時間の都合で飛ばしました"
              "（手元で python scripts/mutation_check.py を回してください）★")
        for x in skipped:
            print("   -", x)
        print()
    if weak:
        print("★『試験が❌を出した』ではなく『ただ落ちた』もの★"
              "（守りを見ている試験がある証拠にはなりません）")
        for x in weak:
            print("   -", x)
        print()
    if ng:
        print(f"★{len(ng)}件の守りが、試験で守られていません★")
        for x in ng:
            print("   -", x)
        return 1
    # ★飛ばした分を数に含めない★（2026-08-24＝「全部OK」に見せない）
    #   ★1件飛ばして 15/15 と出していた★＝この書き方がまさに
    #   プロジェクトが禁じている「黙って削る」だった。
    # ★★1件も試していないのに「全部OK」と言わない★★（2026-08-24）
    #   ★--only が一致しなくても 61/61 と出ていた★＝
    #   **プロジェクトが禁じている「早すぎる数え方」を道具自身がやっていた**
    #   （監査51で見張っている型そのもの）。
    if tried == 0:
        print("★1件も試していません★（--only が何にも一致しませんでした）")
        return 1
    done = tried - len(skipped)
    print(f"{done}/{tried} 試したものは、すべて試験が捕まえます"
          + (f"（★{len(skipped)}件は未確認★）" if skipped else "")
          + (f"／★全{len(MUTATIONS)}通りのうち絞り込み中★"
             if tried != len(MUTATIONS) else ""))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="守りを壊して試験が赤くなるか見る")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--files", default="",
                    help="このファイルを壊す分だけ回す（カンマ区切り）"
                         "＝push の直前に、触ったところだけ確かめる")
    ap.add_argument("--fast", action="store_true",
                    help="時間のかかる試験を飛ばす（CI用）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.list:
        for i, m in enumerate(MUTATIONS, 1):
            print(f"{i:2}. {m['why']}  → {m['file']}")
        return 0
    if a.selftest:
        # ★この道具自身の試験★＝壊し方の目印が実在するか
        bad = []
        for m in MUTATIONS:
            p = os.path.join(BASE, m["file"])
            src = open(p, encoding="utf-8").read()
            if src.count(m["before"]) != 1:
                bad.append(f"{m['why']}（目印が {src.count(m['before'])} 件）")
        for x in bad:
            print("❌ " + x)
        print(f"{len(MUTATIONS) - len(bad)}/{len(MUTATIONS)} 合格")
        return 1 if bad else 0
    if a.files:
        want = {x.strip().replace("\\", "/")
                for x in a.files.split(",") if x.strip()}
        idx = [i for i, m in enumerate(MUTATIONS, 1)
               if m["file"].replace("\\", "/") in want]
        if not idx:
            print("★このファイルを壊す試験はありません★: " + ", ".join(sorted(want)))
            return 0
        return check("", fast=a.fast, only_index=set(idx))
    return check(a.only, fast=a.fast)


if __name__ == "__main__":
    raise SystemExit(main())

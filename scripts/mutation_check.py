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
MUTATIONS = [
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
        "before": '    return not (isinstance(v, dict)\n'
                  '                and str(v.get("basis") or "") == "INDEPENDENT_MULTI")',
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
        "before": "                if len(got) < 2:\n"
                  '                    ng.append(f"{field}: '
                  '独立した2系列になっていません（{got}）")',
        "after": "                pass",
        "run": ["scripts/confirmed_values.py"],
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
        "why": "保存名の案内を出さない（台帳#464の再発）",
        "file": "scripts/backup_guard.py",
        "before": '        findings.append("allowlist:リスト外" + hint)',
        "after": '        findings.append("allowlist:リスト外")',
        "run": ["scripts/backup_guard.py"],
    },
]


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
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    for rel in scripts:
        r = subprocess.run([sys.executable, os.path.join(root, rel),
                            "--selftest"],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", cwd=root,
                           env=env)
        if r.returncode != 0:
            out = (r.stdout or "") + (r.stderr or "")
            ng = [x for x in out.splitlines() if x.startswith("❌")]
            # ★★どう捕まえたのかを区別する★★（2026-08-24・Codexの3回目の指摘2）
            #   ★直す前は「終了コードが0以外＝捕まえた」だけだった★ので、
            #   壊し方が**構文エラーになっただけ**でも合格に見えた。
            #   ＝「その守りを見ている試験がある」証拠にならない。
            #   ★試験が❌を出したのか、ただ落ちたのかを必ず表に出す★。
            if ng:
                return "試験が❌", f"{rel}: {ng[0][:70]}"
            why = (out.strip().splitlines() or [""])[-1][:70]
            return "落ちただけ", f"{rel}: {why}"
    return "", ""


# ★時間のかかる試験★（1本で4分ほど＝本番と同じ経路を丸ごと通すため）
#   CIでは外し、手元の通し確認で回す。
SLOW = ("scripts/publish_new_machine.py",)


def check(only: str = "", fast: bool = False) -> int:
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
    shutil.copytree(BASE, root, ignore=shutil.ignore_patterns(
        "__pycache__", "node_modules", ".preview-site", "_site"))
    try:
        for i, m in enumerate(MUTATIONS, 1):
            if only and only not in m["why"]:
                continue
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
            print(("  OK   " if caught else "  ★NG ")
                  + f"{i}. {m['why']}"
                  + (f"  ［{caught}］" if caught else ""))
            if caught == "落ちただけ":
                # ★合格には数えるが、質は落ちる★＝
                #   「その守りを見ている試験がある」ではなく
                #   「壊すと動かなくなる」しか言えていない。
                weak.append(f"{i}. {m['why']}（{_cwhy[:60]}）")
            if not caught:
                ng.append(m["why"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
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
    done = len(MUTATIONS) - len(skipped)
    print(f"{done}/{len(MUTATIONS)} 試したものは、すべて試験が捕まえます"
          + (f"（★{len(skipped)}件は未確認★）" if skipped else ""))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="守りを壊して試験が赤くなるか見る")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--only", default="")
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
    return check(a.only, fast=a.fast)


if __name__ == "__main__":
    raise SystemExit(main())

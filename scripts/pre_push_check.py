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

# ★★自分の出力は utf-8 に固定する★★（2026-08-28・★実際に push が全部止まった★）
#   ★何が起きたか★＝この関所は git のフックから呼ばれるので、
#   出力先が Windows の既定（cp932）になる。
#   検査は全部通っていたのに、結果の行に含まれる ✅ を書こうとして
#   UnicodeEncodeError で落ち、★終了コードが 1 になって push が拒否された★。
#   ＝「検査が通ったのに push できない」状態。しかも
#     読む側には「関所が止めた」ようにしか見えない。
#   ★これは罠⑪と同じ型★（ci_repro / mutation_check では対策済みだったが、
#     この関所は 2026-08-28 に作ったばかりで、同じ手当てが漏れていた）。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:              # noqa: BLE001
        pass                       # ★書けなくても検査は続ける★

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# この形のファイルが変わっていたら、記事データを触ったとみなす
WATCH = ("assets/data/machine-details/", "assets/data/machines.json")


_CHANGED = None          # ★1回だけ確定して使い回す★
_RANGES = None           # ★今回 push する範囲★（git が標準入力で渡す）


def push_ranges(stdin_text: str) -> list:
    """★git が渡してくる「今回 push する参照と指紋」を読む★

    pre-push フックは1行につき
        <こちらの参照> <こちらの指紋> <向こうの参照> <向こうの指紋>
    を渡す。★向こうの指紋が全部ゼロ＝新しい枝★（相手にまだ無い）。
    ★こちらの指紋が全部ゼロ＝枝を消す★（送る中身は無い）。

    ★決め打ちの `origin/main..HEAD` をやめた理由★
      （2026-08-28・Codexの10回目の指摘3）
      ・別の枝・タグ・HEAD以外を送ると、検査の対象から外せた
      ・`origin/main` が無い環境では `HEAD~1..HEAD` に落ち、
        ★複数のコミットを送っても最後の1件しか見なかった★
    """
    zero = "0" * 40
    out = []
    for line in (stdin_text or "").splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local_sha, remote_sha = parts[1], parts[3]
        if set(local_sha) == {"0"}:        # 枝を消す＝送る中身は無い
            continue
        if remote_sha.startswith(zero[:7]) and set(remote_sha) == {"0"}:
            out.append(local_sha)          # 新しい枝＝そのコミットまで全部
        else:
            out.append(f"{remote_sha}..{local_sha}")
    return out


_COMMITS = None
_GIT_FAILED = []          # ★gitに聞けなかったもの★（あれば push を止める）


def _git(*a):
    r = subprocess.run(["git"] + list(a), cwd=BASE, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        # ★★聞けなかった＝「無い」ではない★★（2026-08-28・Codexの13回目）
        #   ★直す前は None を「対象0件」と読んで続けていた★＝
        #   照合も変更ファイルの検査も記事の監査も**全部飛ばして push できた**。
        _GIT_FAILED.append(" ".join(a)[:80])
        return None
    return r.stdout


def git_unknown() -> list:
    """★gitに聞けなかったもの★（1つでもあれば push を止める）"""
    return list(_GIT_FAILED)


def push_commits() -> list:
    """★今回 push するコミット★（(指紋, 件名) の一覧・1回だけ決める）

    ★ここが唯一の出どころ★（2026-08-28・Codexの12回目）
      ★直す前は、変更ファイルの側だけ直して、照合の検査は
        `origin/main..HEAD` 決め打ちのままだった★＝同じ穴が残っていた。

    ★新しい枝は「先端の1コミット」ではない★（同・P1-b）
      実測＝`git show HEAD` は1件、範囲では5件。
      新しい枝の途中で記事を変えて、最後に無関係なコミットを置くと
      検査から外せた。→ ★どの遠隔にも無いコミットを全部★数える。
    """
    global _COMMITS
    if _COMMITS is not None:
        return _COMMITS
    out, seen = [], set()

    def _add(text):
        for line in (text or "").splitlines():
            sha, _, sub = line.strip().partition(" ")
            if sha and sha not in seen:
                seen.add(sha)
                out.append((sha, sub))

    rngs = list(_RANGES or [])
    if rngs:
        for rng in rngs:
            if ".." in rng:
                _add(_git("log", "--format=%H %s", rng))
            else:
                # ★新しい枝＝どの遠隔にも無いコミットを全部★
                got = _git("log", "--format=%H %s", rng, "--not", "--remotes")
                if got is None:
                    got = _git("log", "--format=%H %s", rng)
                _add(got)
    else:
        # ★標準入力が無いとき（手で動かしたとき）だけ、今までの見方★
        for rng in ("origin/main..HEAD", "HEAD~1..HEAD"):
            got = _git("log", "--format=%H %s", rng)
            if got is not None:
                _add(got)
                break
    _COMMITS = out
    return _COMMITS


def _changed_paths() -> list:
    """★今回 push するもので変わったファイル★（コミットの一覧から導く）"""
    global _CHANGED
    if _CHANGED is not None:
        return _CHANGED
    got = []
    for sha, _sub in push_commits():
        got += [x.strip() for x in
                (_git("show", "--name-only", "--format=", sha) or "").splitlines()
                if x.strip()]
    _CHANGED = sorted(set(got))
    return _CHANGED


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


# ★早見表（ハブ4ページ）が古くなり得る変更★（2026-09-01）
#   ★★中身だけでなく、作る側も入れる★★（Codexの指摘1）＝
#   直す前は材料のJSONだけ見ていたので、★ひな型・並べ方・区分の判定を
#   変えてページを作り直さなくても、点検が呼ばれなかった★。
#
#   machines.json      … 材料（load_rows が読む唯一のデータ）
#   scripts/hub_prose.json … 材料（ページの説明文）
#   build_hub_pages.py … 作る側そのもの
#   page_decision.py   … machine_class() を使うので出力が変わる
#   safe_json.py       … 材料の読み取り
#   guide-*            … 手で書き換えたのを捕まえる
#
#   ★machine-details/ は入れない★＝この生成器は読んでいない
#   （直す前のコメントは事実と違っていた）。
_HUB_SRC = ("assets/data/machines.json", "scripts/hub_prose.json",
            "scripts/build_hub_pages.py", "scripts/page_decision.py",
            "scripts/safe_json.py", "guide-")


def touches_hub(paths) -> bool:
    """★早見表が古くなり得る変更か★（2026-09-01）

    ★実際に起きたこと★＝並べ替えたのに `--legacy` を流さず、
    早見表が古いまま残り、CIが3回続けて赤くなった。
    ★手元では何も止まらなかった★ので、ここで見る。
    """
    for p in paths:
        p = str(p or "").replace("\\", "/")
        for w in _HUB_SRC:
            if p == w or p.startswith(w):
                return True
    return False


def hub_check_problem(changed, run) -> str:
    """★早見表の点検が要るなら流して、駄目なら理由を返す★（2026-09-01）

    run(引数の並び) -> (終了コード, 出力)  … 試験では差し替える

    ★切り出した理由★（Codexの指摘2）＝
      直す前は引き金（`touches_hub`）の試験しか無かったので、
      ★関所の本体から呼び出しを丸ごと外しても、試験は緑のまま★だった。
      いまは `selftest` が「本体がこれを呼んでいるか」も見る。
    """
    if not touches_hub(changed):
        return ""
    code, out = run(["build_hub_pages.py", "--check"])
    for line in str(out or "").strip().splitlines():
        print("   " + line)
    return "早見表が古いまま" if code != 0 else ""


def _check_hub_wiring() -> list:
    """★早見表の点検が「本当に呼ばれ、失敗が関所へ伝わる」か★

    （2026-09-01・Codexのレビュー31の指摘1）
    ★構文木で順序を見るだけでは足りない★＝
      `if False:` で囲む／入れ子の関数に囮を置く／結果を `ng` に入れない、
      はどれも順序の検査では捕まらない。
    ★本物の `main()` を1回通す★＝外に出る所（git・別プロセス）は
      全部差し替えるので、実の git には触らない。
    """
    import contextlib as _cl
    import io as _io

    calls = []

    class _R:
        def __init__(self, code, out):
            self.returncode = code
            self.stdout = out
            self.stderr = ""

    def fake_run(cmd, *a, **k):
        joined = " ".join(str(x) for x in list(cmd))
        calls.append(joined)
        if "build_hub_pages.py" in joined and "--check" in joined:
            return _R(1, "★早見表が古いままです★（試験の偽物）")
        return _R(0, "")

    g = globals()
    fakes = {
        "_warn_unreported": lambda: None,
        "_verified_range": lambda: [],
        # ★作る側だけを変えた push★＝記事データは1件も変わっていない
        "_changed_paths": lambda: ["scripts/build_hub_pages.py"],
        "git_unknown": lambda: [],
        "push_ranges": lambda *_a, **_k: [],
    }
    real = {n: g[n] for n in fakes}
    real_run = subprocess.run
    keep_argv = sys.argv
    # ★★標準入力も差し替える★★（2026-09-02・実際に固まった）
    #   `main()` は
    #     push_ranges("" if sys.stdin.isatty() else sys.stdin.read())
    #   と書いてあり、★`push_ranges` を差し替えても
    #   `sys.stdin.read()` は先に評価される★。
    #   親から開いたままのパイプを受け継ぐと**永久に待つ**。
    #   ★CIの一覧にこの自己試験があるので、CIを固めるところだった★。
    _keep_stdin = sys.stdin
    for n, f in fakes.items():
        g[n] = f
    subprocess.run = fake_run
    sys.argv = ["pre_push_check.py"]
    sys.stdin = _io.StringIO("")
    try:
        buf = _io.StringIO()
        with _cl.redirect_stdout(buf):
            code = main()
    except Exception as e:                                   # noqa: BLE001
        return [f"関所の本体が例外で終わりました（{type(e).__name__}: {e}）"]
    finally:
        for n in real:
            g[n] = real[n]
        subprocess.run = real_run
        sys.argv = keep_argv
        sys.stdin = _keep_stdin

    bad = []
    if not any("build_hub_pages.py" in c and "--check" in c for c in calls):
        bad.append("早見表の点検が呼ばれていません"
                   "（作る側だけを変えた push で届いていない）")
    if code != 1:
        bad.append(f"早見表の点検が赤なのに push を止めません（返り {code}）")
    if not any("audit_site.py" in c and "--skill-audit" in c for c in calls):
        bad.append("手順書の監査が呼ばれていません")
    return bad


def hub_check_reachable() -> str:
    """★早見表の点検が、記事用の早期returnより前にあるか★（2026-09-01）

    ★文字が在るかだけでは足りない★（Codexのレビュー30の指摘1）＝
      呼び出しが早期returnの後ろにあると、`build_hub_pages.py` だけを
      変えた push では**一度も到達しない**（実際にそうなっていた）。
    ★構文木で位置を比べる★＝並べ替えたら赤くなる。
    """
    import ast as _ast
    import inspect as _insp
    try:
        src = _insp.getsource(main)
        tree = _ast.parse(src)
    except Exception as e:                                   # noqa: BLE001
        return f"関所の本体を読めません（{type(e).__name__}）"

    call_line = None
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Call) and isinstance(node.func, _ast.Name) \
                and node.func.id == "hub_check_problem":
            call_line = (node.lineno if call_line is None
                         else min(call_line, node.lineno))
    if call_line is None:
        return "関所の本体が早見表の点検を呼んでいません"

    ret_line = None
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.If):
            continue
        cond = _ast.get_source_segment(src, node.test) or ""
        if "not a.always" in cond and "not hit" in cond:
            ret_line = (node.lineno if ret_line is None
                        else min(ret_line, node.lineno))
    if ret_line is None:
        return ("記事データの早期returnが見つかりません"
                "（この検査の前提が変わっています）")
    if call_line > ret_line:
        return ("★早見表の点検が、記事データの早期returnより後ろにあります★"
                f"（点検 {call_line}行目 ／ 早期return {ret_line}行目）"
                "＝作る側だけを変えた push で一度も流れません")
    return ""


def touches_articles(paths) -> bool:
    """★読者に出る記事を書き換えているか★（照合を求める範囲）

    ★この関所（verify-commit）が守っているのは記事の書き換え★なので、
    スクリプトだけの変更には照合を求めない。
    """
    for p in paths:
        p = str(p or "").replace("\\", "/")
        if p.startswith("assets/data/machine-details/") \
                or p == "assets/data/machines.json" \
                or p.startswith("machines/"):
            return True
    return False


# ★新台の公開でも必ず変わる共通のファイル★（レーンの判定から外す）
_SHARED = ("assets/data/machines.json", "sitemap.xml")


def new_machine_lane(paths, lane_slugs) -> bool:
    """★その日の新台レーンだけを触ったコミットか★（2026-08-28）

    ★照合（verify-commit）は修理レーンのための関所★で、
    新台には別の関所（`prepush_gate` と公開経路の検査）がある。
    ★新台レーンのコミットまで止めると、押し出せなくなる★
    （新台タスクが遅れた日や手で回した日に必ず起きる）。
    """
    slugs = set(lane_slugs or ())
    if not slugs:
        return False
    saw = False
    for p in paths:
        p = str(p or "").replace("\\", "/")
        if p in _SHARED or p.startswith("guide-"):
            continue
        if p.startswith("assets/data/machine-details/"):
            s = p.split("/")[-1][:-5] if p.endswith(".json") else ""
            if s in slugs:
                saw = True
                continue
            return False
        if p.startswith("machines/"):
            s = p.split("/")[1] if "/" in p[9:] + "/" else ""
            if s in slugs:
                saw = True
                continue
            return False
        if touches_articles([p]):
            return False
    return saw


def gate_active(data: dict, today: str) -> tuple:
    """★今日、照合を求めるか★ → (求めるか, 照合済みコミットの集合)

    ★切り出した理由★＝対照実験が空振りだった（罠④）。
      `_verified_range()` は、この判断のあとに
      ★記事を触った未pushのコミットが実際にあるか★も見るので、
      手元にそれが無い日は**どの場合も「通す」**になり、
      狙った判断を一度も通らずに緑になっていた。

    ★★その日に一度でも無人がいたか★★（2026-08-30・Codexの指摘1）
      ★タスクごとの `unattended` を見てはいけない★＝
      あれは担当のたびに上書きされるので、
      ★無人 → 手動の順に動かすと、無人だった記録が消える★
      （自分で再現した：無人が作った未照合コミットまで通った）。
      `day.had_unattended` は**増える方向にしか動かない**。
    ★目印が無い古い記録は「無人だった」とみなす★（fail-closed）
    """
    verified, active = set(), False
    # ★★手作業の記録は日をまたいでも生きる★★（2026-08-31・台帳#527）
    #   `--no-verify` の置き換え。由来だけを人の記録で埋め、
    #   他の検査は全部通す（Codexの4回目の指摘）。
    for r in (data.get("manual_commits") or []):
        if isinstance(r, dict) and r.get("commit"):
            verified.add(str(r["commit"]))
    _day = (data.get("day") or {})
    same_day = str(_day.get("date") or "") == today
    if same_day:
        for c in (_day.get("verified_commits") or []):
            verified.add(str(c))
    had = _day.get("had_unattended") if same_day else None
    for _name, e in (data.get("tasks") or {}).items():
        if not isinstance(e, dict) or e.get("run_date") != today:
            continue
        if not e.get("guard_slug"):
            continue
        if had is None:
            active = True
        if e.get("verified_commit"):
            verified.add(str(e["verified_commit"]))
    if had is True:
        active = True
    return active, verified


def _verified_range() -> list:
    """★無人タスクが直したコミットが、照合を通っているか★（2026-08-21・Codex依頼249）

    ★なぜ要るのか★
      `task_guard verify-commit`（関所が見た内容と実際のコミットの突き合わせ）を
      作ったが、**push の側が見ていなかった**。
      ・verify-commit を省いても push できた
      ・先に未pushのコミットが積まれていると、最新1件だけ照合しても
        それ以前が一緒に push された

    ★★実際の範囲★★（2026-08-30に言い直した）
      ここは「無人タスクが作ったコミットか」を見分けていない。
      ★その日に**無人で動いた**担当があれば、
        記事を書き換えた未pushのコミットは全部★照合を求める
      （対話セッションの手作業も含む）。

    ★★手で動かした担当は数えない★★（2026-08-30・運営者の指摘
      「手動実行は例外としないとテストできないじゃん」）
      ★直す前は無人か手動かを見ていなかった★ので、
      タスクを手で試した日は、対話セッションが記事データを触った
      コミットを**一切 push できなかった**
      ＝★タスクを手で試すと、その日は仕事が出せない＝テストできない★。
      無人かどうかは `claim` のときに `task.lock` が生きていたかで記録する
      （`task_guard.lock_is_live` / `unattended`）。
      ★目印が無い古い記録は「無人だった」とみなす★（fail-closed）。

    戻り値: 止めるべき理由の一覧（空なら通してよい）
    """
    state = _lp.doc("task_guard.json")
    if not os.path.exists(state):
        return []                          # 記録が無い＝無人タスクは動いていない
    try:
        with open(state, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as e:                 # noqa: BLE001
        # ★★読めない・壊れているときは止める★★（2026-08-30・Codexの指摘4）
        #   直す前は「記録が無い」と同じ扱いで**素通し**だった。
        #   ＝ロックの判定をどれだけ厳しくしても、
        #     ★最後の関所が状態を読めないだけで全部通ってしまう★。
        return [f"担当の記録が読めません（{type(e).__name__}）: "
                "壊れていないか確かめてください"]
    # ★対象はその日の無人タスクだけ★（2026-08-21）
    #   全期間の記録を見ると、対話セッションが手で作ったコミットまで
    #   「照合していない」と止めてしまい、鉄則4（当日中にpush）が守れない。
    today = datetime.now().strftime("%Y-%m-%d")
    # ★★その日ぶんの照合済みは、機種を替えても消えない★★
    #   （2026-08-21・Codexの指摘3）
    #   タスクごとの verified_commit は機種を替えると捨てられるので、
    #   ★3機種ぶんをためて最後にまとめて push すると、
    #     1・2機種目が「照合していない」ことになって止まった★。
    active, verified = gate_active(data, today)
    if not active:
        return []                  # 今日は無人タスクが機種を触っていない
    # ★いま push しようとしているコミット★（決めるのは push_commits だけ）
    #   ★直す前はここだけ `origin/main..HEAD` 決め打ちだった★
    #   （2026-08-28・Codexの12回目）＝別の枝・タグで外せた。
    _d = (data.get("day") or {})
    lane = list((_d.get("unlimited_slugs") or [])
                if str(_d.get("date") or "") == today else [])
    out = []
    for sha, subject in push_commits():
        if sha in verified:
            continue
        # ★★記事に触っていないコミットには照合を求めない★★（2026-08-28）
        #   ★直す前は「今日タスクが動いていたら未pushの全部」だった★ので、
        #   対話セッションが作ったスクリプトの直しまで push できなくなった
        #   （実際に、夜の新台タスクを直すコミットが止められた）。
        #   照合は**記事の書き換え**を守る仕組みで、
        #   無人タスクの記事コミットは必ず記事データに触るので守りは弱まらない。
        _f = subprocess.run(["git", "show", "--name-only", "--format=", sha],
                            cwd=BASE, capture_output=True)
        if _f.returncode == 0:
            _paths = [x.strip() for x in
                      _f.stdout.decode("utf-8", "replace").splitlines()
                      if x.strip()]
            if not touches_articles(_paths):
                continue
            # ★新台レーンは別の関所が見ている★（2026-08-28）
            if new_machine_lane(_paths, lane):
                continue
        out.append(f"{sha[:12]} {subject[:60]}")
    return out


_TODAY = datetime.now().strftime("%Y-%m-%d")


def _selftest() -> int:
    """★自分の出力が utf-8 で書けること★（2026-08-28・実際に push が全部止まった）

    ★この関所は git のフックから呼ばれる★ので、出力先が
    Windows の既定（cp932）になる。検査は全部通っているのに、
    結果の行に含まれる ✅ を書こうとして落ち、
    ★終了コードが 1 になって push が拒否された★。
    （読む側には「関所が止めた」ようにしか見えない）
    """
    ok = []

    def t(name, cond):
        print(("✅ " if cond else "❌ ") + name)
        ok.append(bool(cond))

    # ★★本番と同じ形で確かめる★★（2026-08-28・罠④）
    #   ★いまの設定を見るだけでは足りない★＝試験は
    #   `PYTHONIOENCODING=utf-8` で走ることが多いので、
    #   ★固定していなくても utf-8 になり、守りを外しても緑のまま★だった。
    #   関所は git のフックから **cp932 の設定で** 呼ばれるので、
    #   自分を その設定で呼び直して、合格の記号が書けるかを見る。
    _env = {**os.environ, "PYTHONIOENCODING": "cp932"}
    _env.pop("PYTHONUTF8", None)
    r = subprocess.run([sys.executable, os.path.abspath(__file__),
                        "--echo-check"],
                       capture_output=True, env=_env, cwd=BASE)
    t("★★Windowsの既定（cp932）で呼ばれても、合格の記号を書ける★★"
      "／★固定しないと、合格と表示しようとして失敗し、"
      "『検査は通ったのに push できない』になる★",
      r.returncode == 0)
    if r.returncode != 0:
        print("   " + (r.stderr or b"").decode("utf-8", "replace")
              .strip().splitlines()[-1][:120])
    # ★★今回 push するものを、git から受け取って読む★★
    #   （2026-08-28・Codexの10回目の指摘3）
    #   ★決め打ちの `origin/main..HEAD` では、別の枝・タグを送ると
    #     検査の対象から外せた／`origin/main` が無い環境では
    #     複数コミットのうち最後の1件しか見なかった★
    Z = "0" * 40
    t("★★送る範囲を、渡された参照から作る★★",
      push_ranges(f"refs/heads/main aaa refs/heads/main {Z[:39]}1")
      == [f"{Z[:39]}1..aaa"])
    t("★★新しい枝は、そのコミットまで全部を見る★★"
      "／★『向こうに無い』を範囲の始まりにすると git が読めない★",
      push_ranges(f"refs/heads/x bbb refs/heads/x {Z}") == ["bbb"])
    t("　枝を消すときは、送る中身が無いので何も見ない",
      push_ranges(f"(delete) {Z} refs/heads/x ccc") == [])
    t("★★2つの参照を同時に送っても、両方を見る★★",
      len(push_ranges(f"refs/heads/a aaa refs/heads/a {Z[:39]}1\n"
                      f"refs/heads/b bbb refs/heads/b {Z[:39]}2")) == 2)
    t("　渡されなければ空（手で動かしたときは今までの見方に戻る）",
      push_ranges("") == [] and push_ranges("こわれた行") == [])

    # ★★新台レーンのコミットは、修理レーンの照合を求めない★★
    #   （2026-08-28・手動実行で実際に踏んだ＝新台の記事が push できなかった）
    _np = ["assets/data/machine-details/dmm_5090.json",
           "machines/dmm_5090/index.html",
           "assets/data/machines.json", "guide-ichiran.html"]
    t("★★新台だけを触ったコミットは、照合を求めない★★"
      "／★求めていたので、新台タスクが遅れた日は公開できなかった★",
      new_machine_lane(_np, ["dmm_5090"]) is True)
    t("★★新台以外の記事が混ざっていたら、今までどおり照合を求める★★",
      new_machine_lane(_np + ["assets/data/machine-details/hanabi.json"],
                       ["dmm_5090"]) is False)
    t("　その日の新台レーンが空なら、何も免除しない",
      new_machine_lane(_np, []) is False)
    t("　共通のファイルだけなら、新台とは言えない",
      new_machine_lane(["assets/data/machines.json"], ["dmm_5090"]) is False)

    # ★★gitに聞けなかったら、pushを止める★★（2026-08-28・Codexの13回目）
    #   ★私が今日入れた穴★＝失敗を「対象0件」と読んで、
    #   検査を全部飛ばして push できた（安全と反対側）。
    _keep_failed = list(_GIT_FAILED)
    try:
        _GIT_FAILED.clear()
        t("　（前提）ふつうは聞けている", _git("rev-parse", "HEAD") is not None
          and not git_unknown())
        _GIT_FAILED.clear()
        _bad = _git("log", "--format=%H", "0000000000000000000000000000000000000000..HEAD")
        t("★★聞けなかったことを覚えている★★"
          "／★覚えていないと『変更が無い』と読んで検査を全部飛ばす★",
          _bad is None and len(git_unknown()) == 1)
    finally:
        _GIT_FAILED.clear()
        _GIT_FAILED.extend(_keep_failed)

    # ★★新しい枝は「先端の1コミット」ではない★★
    #   （2026-08-28・Codexの12回目・実測で確かめた）
    #   `git show <指紋>` は先端しか出さないので、
    #   ★枝の途中で記事を変えて、最後に無関係なコミットを置くと外せた★。
    _tip = (_git("rev-parse", "HEAD") or "").strip()
    _prev = (_git("rev-parse", "HEAD~3") or "").strip()
    if _tip and _prev:
        _one = [x for x in
                (_git("show", "--name-only", "--format=", _tip) or "").splitlines()
                if x.strip()]
        _all = [x for x in
                (_git("log", "--format=%H", f"{_prev}..{_tip}") or "").splitlines()
                if x.strip()]
        t("　（前提）直近3コミットぶんの範囲がある", len(_all) == 3)
        t("★★先端だけを見ると、手前のコミットの変更を見落とす★★"
          "／★新しい枝を送るときに、これで検査から外せた★",
          len(_all) > 1 and len(_one) >= 1)

    # ★★早見表が古くなり得る変更か★★（2026-09-01新設）
    #   ★実際に起きたこと★＝並べ替えたのに作り直さず、CIが3回赤くなった。
    #   ★手元では1本も止めていなかった★（実測：build_hub_pages --selftest /
    #   audit_site / crosscheck_gates とも素通り）。
    t("★早見表：並べ替えたら見る★",
      touches_hub(["assets/data/machines.json"]) is True)
    t("　説明文を触ったときも見る",
      touches_hub(["scripts/hub_prose.json"]) is True)
    # ★★作る側の変更でも見る★★（2026-09-01・Codexの指摘1）＝
    #   直す前は材料のJSONだけ見ていたので、ひな型・並べ方・区分の判定を
    #   変えてページを作り直さなくても、点検が呼ばれなかった。
    t("★早見表：作る側そのものを変えたら見る★",
      touches_hub(["scripts/build_hub_pages.py"]) is True)
    t("★早見表：区分の判定を変えたら見る★",
      touches_hub(["scripts/page_decision.py"]) is True)
    t("　材料の読み取りを変えたときも見る",
      touches_hub(["scripts/safe_json.py"]) is True)
    # ★この生成器は機種の記事データを読んでいない★（実装で確認）
    t("★早見表：記事データは材料ではないので見ない★",
      touches_hub(["assets/data/machine-details/hokuto.json"]) is False)
    t("　早見表そのものを手で書き換えたときも見る",
      touches_hub(["guide-ichiran.html"]) is True)
    t("★早見表：関係ない変更では見ない★",
      touches_hub(["scripts/pre_push_check.py", "README.md"]) is False)
    t("★早見表：似た名前に釣られない★",
      touches_hub(["assets/data/machines-old.json",
                   "guides/x.html", "myhub_prose.json"]) is False)
    t("　Windowsの区切りでも見る",
      touches_hub(["assets\\data\\machines.json"]) is True)
    t("　空やNoneが混ざっても落ちない",
      touches_hub([None, "", "guide-ichiran.html"]) is True)

    # ★★関所の本体が、点検を本当に呼んでいるか★★（2026-09-01・Codexの指摘2）
    #   ★引き金の試験だけでは、呼び出しを外したことに気づけない★
    import inspect as _insp37
    _msrc = _insp37.getsource(main)
    t("★早見表：関所の本体が点検を呼んでいる★"
      "／★呼び出しを外しても引き金の試験だけでは捕まらない★",
      "hub_check_problem(" in _msrc)
    # ★★呼んでいるだけでは足りない。届く位置にあるか★★
    #   （2026-09-01・Codexのレビュー30の指摘1）＝
    #   直す前は記事用の早期returnの後ろにあり、
    #   `build_hub_pages.py` だけの push では一度も流れなかった。
    t("★早見表：点検が記事の早期returnより前にある★"
      "／★後ろだと、作る側だけを変えた push で一度も流れない★",
      hub_check_reachable() == "")
    # ★★本体を1回通して、呼ばれることと失敗が伝わることを見る★★
    #   （2026-09-01・Codexのレビュー31の指摘1）＝
    #   順序の検査だけでは `if False:` で囲む壊し方などを捕まえられない。
    for _x in _check_hub_wiring():
        t("★関所の配線★ " + _x, False)
    t("★★関所の本体を1回通すと、早見表の点検が呼ばれ、失敗が伝わる★★",
      not _check_hub_wiring())
    t("　点検が要らない変更では流さない（流したら失敗にする）",
      hub_check_problem(["README.md"],
                        lambda a: (1, "★呼ばれてはいけません★")) == "")
    t("★早見表：点検が赤なら push を止める★",
      hub_check_problem(["assets/data/machines.json"],
                        lambda a: (1, "古い")) != "")
    t("　点検が緑なら止めない",
      hub_check_problem(["assets/data/machines.json"],
                        lambda a: (0, "一致")) == "")

    # ★★照合を求める範囲★★（2026-08-28・実際に push が止まった）
    t("★★記事を書き換えたコミットには照合を求める★★",
      touches_articles(["assets/data/machine-details/dmm_5086.json"]) is True)
    t("　機種一覧を書き換えた場合も同じ",
      touches_articles(["assets/data/machines.json"]) is True)
    t("　公開ページを書き換えた場合も同じ",
      touches_articles(["machines/dmm_5089/index.html"]) is True)
    t("★★スクリプトだけの変更には照合を求めない★★"
      "／★求めていたので、夜のタスクを直すコミットが push できなかった★",
      touches_articles(["scripts/pre_push_check.py",
                        "scripts/mutation_check.py"]) is False)
    t("　似た名前でも、記事の置き場でなければ求めない",
      touches_articles(["assets/data/machines-backup.json",
                        "assets/css/practical.css"]) is False)
    # ★★この関数を実際に呼ぶ★★（2026-08-30・Codexの指摘＋自分で踏んだ）
    #   判断を gate_active() へ切り出したとき、消し忘れた局所変数のせいで
    #   ★push しようとした瞬間に例外★になった。
    #   ここが試験で一度も通っていなかったので、誰も気づかなかった。
    try:
        _verified_range()
        t("★★push前の照合の判断が、例外なく最後まで動く★★"
          "（＝切り出しの消し残しで push が落ちた）", True)
    except Exception as e:                                   # noqa: BLE001
        t(f"★★push前の照合の判断が動く★★（{type(e).__name__}: {e}）", False)

    # ★判断そのものも直接たたく★（記事コミットの有無に左右されない）
    t("★手だけの日は照合を求めない★",
      gate_active({"day": {"date": _TODAY, "had_unattended": False},
                   "tasks": {"u": {"run_date": _TODAY,
                                   "guard_slug": "x"}}}, _TODAY)[0] is False)
    t("★無人がいた日は照合を求める★",
      gate_active({"day": {"date": _TODAY, "had_unattended": True},
                   "tasks": {"u": {"run_date": _TODAY,
                                   "guard_slug": "x"}}}, _TODAY)[0] is True)
    # ★★手作業の記録は日をまたいでも生きる★★（2026-08-31・台帳#527）
    t("★★手作業の記録があれば、そのコミットは照合済みとして通す★★"
      "（--no-verify の置き換え）",
      "abc1234" in gate_active(
          {"day": {"date": _TODAY, "had_unattended": True},
           "manual_commits": [{"commit": "abc1234", "why": "運営者の指示"}],
           "tasks": {"u": {"run_date": _TODAY, "guard_slug": "x"}}},
          _TODAY)[1])
    t("　昨日の手作業の記録も生きている（日で消えない）",
      "abc1234" in gate_active(
          {"day": {"date": "1999-01-01"},
           "manual_commits": [{"commit": "abc1234", "why": "運営者の指示"}]},
          _TODAY)[1])
    t("　記録に無いコミットは照合済みにしない",
      "zzz9999" not in gate_active(
          {"day": {"date": _TODAY, "had_unattended": True},
           "manual_commits": [{"commit": "abc1234", "why": "運営者の指示"}],
           "tasks": {"u": {"run_date": _TODAY, "guard_slug": "x"}}},
          _TODAY)[1])
    t("★目印が無い古い記録は照合を求める★（fail-closed）",
      gate_active({"day": {"date": _TODAY},
                   "tasks": {"u": {"run_date": _TODAY,
                                   "guard_slug": "x"}}}, _TODAY)[0] is True)

    ng = ok.count(False)
    print(f"{len(ok) - ng}/{len(ok)} 合格")
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="push前の検査")
    ap.add_argument("--always", action="store_true")
    ap.add_argument("--selftest", action="store_true",
                    help="この関所自身の試験（出力の文字の扱いを確かめる）")
    ap.add_argument("--echo-check", action="store_true",
                    help="★試験が内側から呼ぶ★＝合格の記号を書けるか試すだけ")
    a = ap.parse_args()
    if not (a.echo_check or a.selftest):
        # ★git が渡す「今回 push する参照」を読む★（無ければ空）
        global _RANGES
        try:
            _RANGES = push_ranges("" if sys.stdin.isatty() else sys.stdin.read())
        except Exception:                  # noqa: BLE001
            _RANGES = []
    if a.echo_check:
        # ★合格の記号を書くだけ★（書けなければ例外で終了コードが1になる）
        print("✅❌★ 監査が使う記号")
        return 0
    if a.selftest:
        return _selftest()

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
        print("   → 無人タスクなら: python scripts/task_guard.py verify-commit "
              "--task <タスク> --slug <機種> --commit <コミット>")
        print("   → 手作業なら: python scripts/task_guard.py manual-commit "
              "--commit <コミット> --why-file <理由を書いたファイル>")
        print("   （★--no-verify で迂回しないこと★＝あれは後段の監査まで外します）")
        print("   （関所が見た内容と、実際にpushする内容が同じかを確かめてください）")
        print()
        return 1

    changed = _changed_paths()
    # ★★gitに聞けなかったら止める★★（2026-08-28・Codexの13回目）
    #   ★「分からない」を「変更が無い」と読まない★＝
    #   読み違えると、検査を全部飛ばして push できてしまう。
    if git_unknown():
        print()
        print("★★gitに聞けませんでした★★（何を push するのか分かりません）")
        for _q in git_unknown()[:4]:
            print("   git " + _q)
        print("   → 相手の指紋が手元に無い（fetch していない）ことがあります。"
              "`git fetch` を試してから、もう一度 push してください")
        print("★pushを止めました★")
        return 1
    hit = [p for p in changed if any(p.startswith(w) or p == w for w in WATCH)]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    ng = []
    # ★★触ったスクリプトの守りを、実際に壊して試す★★（2026-08-28）
    #   ★きっかけ★＝守りを手前に足したせいで奥の守りの試験が消え、
    #   それを見落として push し、★GitHubのCIが実際に赤くなった★。
    #   道具は正しく「★NG」と出していたのに、こちらが読み違えた。
    #   ★人の注意では止まらないので、機械に止めさせる★。
    #   ★全部回すと数分かかる★ので、**触ったファイルの分だけ**回す。
    _touched = sorted({p for p in changed
                       if p.startswith("scripts/") and p.endswith(".py")})
    if _touched:
        print()
        print("--- 壊し方（触ったスクリプトの分だけ） ---")
        print("   対象: " + " / ".join(_touched))
        _mf = subprocess.run(
            [sys.executable, os.path.join(BASE, "scripts", "mutation_check.py"),
             # ★時間のかかるものは飛ばす★（2026-08-28・push が10分で切れた）
             #   ★飛ばした件数は必ず出る★ので「全部OK」には見えない。
             #   飛ばした分は CI と、手元で全部回したときに見る。
             "--fast", "--files", ",".join(_touched)],
            cwd=BASE, capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env)
        for _l in (_mf.stdout or "").splitlines():
            if _l.startswith("  ★ND") or _l.startswith("  ★NG") \
                    or "守られていません" in _l or "試したものは" in _l \
                    or "飛ばしました" in _l \
                    or "試験はありません" in _l:
                print("   " + _l.strip())
        if _mf.returncode != 0:
            print("★★守りを壊しても、試験が赤くなりません★★"
                  "（その守りを見ている試験がありません）")
            print("   → 手前に別の守りを足したせいで、"
                  "奥の守りを一度も試さなくなっていないか確かめてください")
            ng.append("壊し方（触った分）")
    # ★★早見表（ハブ4ページ）が古いままでないか★★（2026-09-01新設）
    #   ★実際に起きたこと★＝machines.json を並べ替えたのに
    #   `build_hub_pages.py --legacy` を流さず、早見表が古いまま残り、
    #   ★CIが3回続けて赤くなった★（運営者にだけエラーメールが届く）。
    #   ★手元では気づけなかった★＝この関所が見ていなかった。
    #
    #   ★★記事の検査より前に流す★★（2026-09-01・Codexのレビュー30の指摘1）
    #   ★直す前は下の「記事データの変更なし → return 0」より後ろに置いていた★ので、
    #   `build_hub_pages.py` だけを変えた push では**一度も到達しなかった**
    #   ＝「作る側の変更でも見る」と書いた引き金が、実際には効いていなかった。
    def _hub_run(args):
        r = subprocess.run(
            [sys.executable, os.path.join(BASE, "scripts", args[0])]
            + list(args[1:]),
            cwd=BASE, capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env)
        return r.returncode, (r.stdout or "")

    _hub_ng = hub_check_problem(changed, _hub_run)
    if _hub_ng:
        ng.append(_hub_ng)

    # ★★手順書（スキル・無人タスク）の監査は毎回流す★★
    #   （2026-09-01・Codexのレビュー30の指摘3）
    #   ★記事の変更が無くても流す★＝手順書はスクリプトだけの変更でも古くなる。
    #   ★`--required` を付ける★＝この機械では置き場が在るはずなので、
    #   置き場ごと消えたときに「別PC」と読み違えて黙らないため。
    #   ★一瞬で終わる★ので、関所が重くなる心配はない（罠㉓）。
    _sk = subprocess.run(
        [sys.executable, os.path.join(BASE, "scripts", "audit_site.py"),
         "--skill-audit", "--required"],
        cwd=BASE, capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env)
    # ★流れたことを必ず見せる★（2026-09-01）＝
    #   直す前は失敗したときしか何も出さなかったので、
    #   ★呼び出しが消えても、届かない位置にあっても画面は同じ★だった。
    for _l in (_sk.stdout or "").strip().splitlines():
        print("   " + _l)
    if _sk.returncode != 0:
        ng.append("手順書の監査")
    if ng:
        print()
        print("★pushを止めました★ 失敗: " + " / ".join(ng))
        return 1
    if not a.always and not hit:
        print("pre-push: 記事データの変更なし（記事の検査は流しません）")
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

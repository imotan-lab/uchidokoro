#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""★CIが流す検査を、手元でCIと同じ条件で再現する★（2026-08-21新設）

## なぜ要るのか

2026-08-21、CIが3回続けて落ちた。どれも**手元では通るのにCIで落ちる**型だった。

  ①`before_commit` の「変更が無ければコミットさせない」
    → CIは作業ツリーが綺麗なので必ず引っかかる。手元は編集中で汚れていて通った
  ②`grow_machine --selftest` が本物の名鑑を取りに行く
    → CIは通信できないので落ちる。手元は通信できるので数分待たされるだけ
  ③`material_contract --check` が依存の増加で止める
    → `--approve` を忘れていた。手元では `--selftest` しか流していなかった

★共通しているのは「手元とCIで条件が違う」こと★。
毎回コミットしてから気づくのは遅い。**push する前に同じ条件で試す**。

## 何をするか

  ・`.github/workflows/pages-rehearsal.yml` の Self-tests から**実際のコマンドを読む**
    （手で並べ直さない＝ワークフローが増えたら自動で追随する）
  ・★通信を断つ★（sitecustomize で socket の接続を塞ぐ）
  ・作業ツリーが綺麗な写しで流す（`--clone` を付けたとき）
  ・落ちたものだけ、出力の末尾を見せる

## 使い方

    python scripts/ci_repro.py              # いまの作業ツリーで
    python scripts/ci_repro.py --clone      # 綺麗な写しを作って（CIに最も近い）
    python scripts/ci_repro.py --audits     # Repository audits も流す
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

# ★★自分の出力は必ず utf-8★★（2026-08-24・自分で踏んだ）
#   ★結果をファイルへ落とすと、Windowsの既定（cp932）で書かれる★。
#   それを utf-8 として読むと日本語が化け、
#   ★「★NG」を探しても見つからず「NGなし」と誤読する★（実際にやった）。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                            # noqa: BLE001
    pass

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(BASE, ".github", "workflows", "pages-rehearsal.yml")

# ★通信を断つ差し込み★（Pythonが起動時に必ず読む名前）
# ★★外へは出さない。ただし自分自身（localhost）は通す★★（2026-08-22）
#   ★直す前に起きていたこと★＝localhost まで塞いでいたので、
#   「公開したページを実際にHTTPで引いて確かめる」試験が
#   ★ci_repro では絶対に通らなかった★（毎回1件が赤）。
#   ＝**CIでは通るのに手元の再現では落ちる**という、
#   ci_repro の存在意義そのものに反する食い違いだった。
#   本物のCIは localhost の簡易サーバを普通に使えるので、そちらへ合わせる。
#
#   ★外部への通信は今までどおり止める★＝
#   CIは通信できない前提で組んであり、そこは変えない。
BLOCK = (
    "import socket\n"
    "_real_conn = socket.socket.connect\n"
    "_real_create = socket.create_connection\n"
    "_LOCAL = ('127.0.0.1', '::1', 'localhost')\n"
    "def _is_local(addr):\n"
    "    try:\n"
    "        return str(addr[0]) in _LOCAL\n"
    "    except Exception:\n"
    "        return False\n"
    "def _conn(self, addr, *a, **k):\n"
    "    if _is_local(addr):\n"
    "        return _real_conn(self, addr, *a, **k)\n"
    "    raise OSError('通信は禁止（CI再現）')\n"
    "def _create(addr, *a, **k):\n"
    "    if _is_local(addr):\n"
    "        return _real_create(addr, *a, **k)\n"
    "    raise OSError('通信は禁止（CI再現）')\n"
    "socket.socket.connect = _conn\n"
    "socket.create_connection = _create\n"
)


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


# ★「この行は python を動かしているか」を単語で見る★（2026-08-31）
#   `PYTHONUTF8=1 python a.py` / `timeout 30 python a.py` /
#   `cd x && python a.py` を見つけるため。
#   ★`python` を含む語（pythonpath 等）に当たらないように前後を切る★
# ★★道つきの呼び出しも見つける★★（2026-08-31・Codexの4回目のP2）
#   ★直す前は / の直後を対象外にしていた★ので、
#     /usr/bin/python scripts/a.py
#     ./venv/bin/python scripts/a.py
#   が検出できず、また黙って飛んでいた
#   （前回と同じ型の穴を残していた）。
#   ★python_helper.py のような語には当たらない★（後ろが文字なら外す）。
_RUNS_PY = re.compile(r"(?<![\w.-])python[0-9.]*(?![\w])")


def _scan_block(lines: list, step: str) -> tuple:
    """ステップの中の行を分類する。返すもの: (実行する行, 読めない行)

    ★読めない行を黙って飛ばさない★（2026-08-31・Codexの3回目のP2）＝
    直す前は `python ` で始まる行だけを拾っていたので、
    前置きのある呼び出しは**そもそも届かず**、赤にもならなかった
    ＝★その検査を飛ばしたまま「全部通りました」と言っていた★。
    """
    out, bad, inside = [], [], False
    for ln in lines:
        if re.match(r"\s*- name: " + re.escape(step) + r"\s*$", ln):
            inside = True
            continue
        if not inside:
            continue
        if re.match(r"\s*- name: ", ln):
            break
        s = ln.strip()
        if not s or s.startswith("#"):
            continue                      # 空行とコメントは読み飛ばす
        if s.startswith("python "):
            out.append(s)
            continue
        # ★コメントの中で python の話をしているだけの行は数えない★
        head = s.split(" #")[0]
        if _RUNS_PY.search(head):
            bad.append(s)
    return out, bad


def _commands(step: str) -> tuple:
    """ワークフローの指定ステップから python コマンドを読む。

    返すもの: (実行する行, 読めない行)
    """
    if not os.path.isfile(WF):
        return [], []
    return _scan_block(
        open(WF, encoding="utf-8").read().splitlines(), step)


# ★ワークフローの行は「シェルの文」★（2026-08-31・自分で踏んだ）
#   `python x.py > cc.log 2>&1 || rc=1` をそのまま argv にしていたので、
#   `>` `2>&1` `||` `rc=1` が**引数として**渡っていた。
#   ＝argparse の厳しいスクリプトは必ず落ち、★毎回1本、嘘の赤が出ていた★。
#   ＝argparse の緩いスクリプトは落ちないが、ゴミの引数つきで走っていた。
#   ★CIの再現なのに、CIと違うものを動かしていた★ので、
#   この道具の答えそのものが信用できなかった。
_STOP = (">", ">>", "<", "<<", "|", "||", "&&", ";", "&",
         "2>", "1>", "&>", "2>&1", "1>&2")
# ★引数の中に混ざっていたら、この道具では再現できない★
#   （`python a.py|b` のように空白が無いと、切れ目として現れない）
_META = "|&;<>"


def argv_of(cmd: str) -> list:
    """シェルの文から argv を取り出す（作れないときは空）。

    ★引用符は shlex に任せる★（2026-08-31・Codexの指摘）＝
    直す前は空白で切っていたので、
      python scripts/check_duplicate.py --name "存在しない機種テスト用XYZ"
    が **引用符ごと** 引数に渡っていた＝★すでにCIと違うものを動かしていた★。
    """
    return _parse(cmd)[0]


def argv_problem(cmd: str, args: list) -> str:
    """argv として使えないなら理由を返す（使えるなら空文字）。

    ★読み取れない行を黙って飛ばさない★＝飛ばした検査は
    「守っている」ことにならない（CLAUDE.md「無ければ飛ばすで逃げない」）。
    ★再現できない書き方は、通さずに赤にする★＝
    パイプ・入力のリダイレクト・複数コマンド・変数の前置き・置換・行の継続。
    """
    why = _parse(cmd)[1]
    if why:
        return why
    if len(args) < 2 or args[0] != "python":
        return f"ワークフローの行から argv を作れません: {str(cmd)[:70]}"
    return ""


def _tail_ok(rest: list) -> bool:
    """コマンドのあとに許すのは「出力のリダイレクト」と `|| rc=1` だけ。"""
    r = list(rest)
    if r[:1] and r[0] in (">", ">>"):
        if len(r) < 2 or r[1] in _STOP:
            return False
        r = r[2:]
    if r[:1] == ["2>&1"]:
        r = r[1:]
    if r[:2] == ["||", "rc=1"]:
        r = r[2:]
    return not r


def _parse(cmd: str) -> tuple:
    """(argv, 理由) を返す。理由が空文字なら argv を使ってよい。"""
    import shlex
    raw = str(cmd or "").strip()
    for ng, why in ((chr(96), "バッククォート"), ("$(", "コマンド置換"),
                    ("${", "変数の展開"), (" #", "コメント")):
        if ng in raw:
            return [], f"シェルの{why}があるので再現できません: {raw[:60]}"
    if raw.endswith(chr(92)):
        return [], f"行が次へ続いているので再現できません: {raw[:60]}"
    try:
        toks = shlex.split(raw, posix=True)
    except ValueError as e:                # noqa: BLE001
        return [], f"引用符が閉じていません: {e}"
    args, rest = [], []
    for i, t in enumerate(toks):
        if t in _STOP:
            rest = toks[i:]
            break
        args.append(t)
    for a in args:
        if any(ch in a for ch in _META):
            return [], f"引数にシェルの記号が混ざっています: {a[:40]}"
    if not _tail_ok(rest):
        return [], ("この道具では再現できないシェルの書き方です: "
                    + " ".join(rest)[:50])
    return args, ""


def _run(cmds: list, root: str, no_net: bool) -> list:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    # ★★控えの有無という食い違い★★（2026-08-24・実際にCIが2回赤くなった）
    #   ★手元には控えがあり、CIの機械には無い★。
    #   OSの違いは手元では再現できないが、**これは再現できる**。
    if os.environ.get("UCHI_EMPTY_DOCS"):
        import tempfile as _tf2
        env["UCHIDOKORO_DOCS"] = _tf2.mkdtemp(prefix="ci_empty_docs_")
    sc = None
    if no_net:
        # ★★リポジトリの中に置かない★★（2026-08-24・自分で踏んだ）
        #   ★直す前は root（＝作業ツリー）に sitecustomize.py を作っていた★。
        #   この道具を強制終了すると片付けが走らず、**通信を塞ぐファイルが
        #   リポジトリに居座る**。python -c や python -m を作業場所から
        #   動かすと読み込まれるので、★通信できない理由が分からなくなる★。
        #   （実際に残っていたのを git status で見つけた）
        #   別の場所に置いて、そこだけを見せれば残っても誰にも当たらない。
        import tempfile as _tf
        _d = _tf.mkdtemp(prefix="ci_repro_nonet_")
        sc = os.path.join(_d, "sitecustomize.py")
        with open(sc, "w", encoding="utf-8") as f:
            f.write(BLOCK)
        env["PYTHONPATH"] = _d
    bad = []
    try:
        for c in cmds:
            args = argv_of(c)
            why = argv_problem(c, args)
            if why:
                print(f"  ★NG {str(c)[:66]}  ({why})")
                bad.append((c, "", "★" + why + "★"))
                continue
            try:
                r = subprocess.run([sys.executable] + args[1:], cwd=root, env=env,
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=600)
                code, out, err = r.returncode, r.stdout or "", r.stderr or ""
            except subprocess.TimeoutExpired:
                code, out, err = 124, "", "★時間切れ（通信待ちの疑い）★"
            mark = "OK  " if code == 0 else "★NG"
            print(f"  {mark} {' '.join(args[1:])[:66]}  (exit {code})")
            if code != 0:
                bad.append((c, out[-800:], err[-400:]))
    finally:
        if sc:
            import shutil as _sh2
            _rmtree_hard(os.path.dirname(sc))
    return bad


def selftest() -> int:
    """★この道具自身の試験★（2026-08-31）。"""
    ok_all, ran = True, [0]

    def t(name, cond):
        nonlocal ok_all
        ran[0] += 1
        ok_all = ok_all and bool(cond)
        print(("✅" if cond else "❌") + " " + name)

    def _ok(c):
        return argv_problem(c, argv_of(c)) == ""

    t("★★リダイレクトと条件つなぎを引数にしない★★"
      "（毎回1本、嘘の赤が出ていた）",
      argv_of("python scripts/crosscheck_gates.py > cc.log 2>&1 || rc=1")
      == ["python", "scripts/crosscheck_gates.py"]
      and _ok("python scripts/crosscheck_gates.py > cc.log 2>&1 || rc=1"))
    t("　ふつうの引数はそのまま残す",
      argv_of("python scripts/x.py --check --slug abc")
      == ["python", "scripts/x.py", "--check", "--slug", "abc"])
    t("★★引用符を外して渡す★★（本物のワークフローの行・Codexの指摘）",
      argv_of('python scripts/check_duplicate.py '
              '--name "存在しない機種テスト用XYZ"')
      == ["python", "scripts/check_duplicate.py", "--name",
          "存在しない機種テスト用XYZ"])
    t("　引用符の中の空白は1つの引数のまま",
      argv_of('python a.py --name "あ い"')
      == ["python", "a.py", "--name", "あ い"])
    t("★★再現できない書き方は赤にする（黙って切らない）★★",
      not _ok("python a.py --fast | tail -n 5")
      and not _ok("python a.py < in.txt")
      and not _ok("cd x && python a.py")
      and not _ok("VAR=x python a.py")
      and not _ok("python a.py; python b.py"))
    t("　空白の無い記号でも赤にする（切れ目として現れない）",
      not _ok("python a.py|b") and not _ok("python a.py>log"))
    t("　置換・変数・行の継続も赤にする",
      not _ok("python a.py $(date)")
      and not _ok("python a.py ${X}")
      and not _ok("python a.py " + chr(92)))
    t("　追記のリダイレクトは通す",
      argv_of("python a.py >> log.txt") == ["python", "a.py"]
      and _ok("python a.py >> log.txt"))
    t("★★読み取れない行は黙って飛ばさない★★",
      argv_problem("> x", argv_of("> x")) != "")
    t("　python 以外の行も断る",
      argv_problem("bash a.sh", argv_of("bash a.sh")) != "")
    t("　まっとうな行は断らない",
      argv_problem("python a.py --x", argv_of("python a.py --x")) == "")
    # ★★本物のワークフローの全部の行が通ること★★
    #   （手作りの例だけで採点しない・罠①）
    _c1, _b1 = _commands("Self-tests")
    _c2, _b2 = _commands("Repository audits")
    _lines = _c1 + _c2
    _ng = [c for c in _lines if argv_problem(c, argv_of(c))]
    t(f"★★いまのワークフローの {len(_lines)} 行が全部読める★★",
      bool(_lines) and not _ng)
    t("★★いまのワークフローに、読み取れない呼び出しが無い★★",
      not (_b1 + _b2))
    # ★★前置きのある呼び出しを「読み飛ばさない」★★（Codexの3回目のP2）
    #   ★直す前は `python ` で始まる行だけを拾っていたので、
    #     この形は _parse に届きもせず、黙って検査を飛ばしていた★
    _fake = ["    - name: Self-tests",
             "      run: |",
             "        # python の話をしているだけのコメント",
             "        set -euo pipefail",
             "        python scripts/ok.py",
             "        PYTHONUTF8=1 python scripts/a.py",
             "        timeout 30 python scripts/b.py",
             "        cd sub && python scripts/c.py",
             "        /usr/bin/python scripts/d.py",
             "        ./venv/bin/python scripts/e.py",
             "    - name: 次"]
    _fc, _fb = _scan_block(_fake, "Self-tests")
    t("★★前置きのある python 呼び出しを見つける★★"
      "（読み飛ばすと、その検査を飛ばしたまま緑になる）",
      _fc == ["python scripts/ok.py"] and len(_fb) == 5)
    t("　コメントや入れ物の行は読み飛ばす",
      not any("set -euo" in x for x in _fb)
      and not any("コメント" in x for x in _fb))
    print(f"{ran[0]}/{ran[0]} 合格" if ok_all else "不合格あり")
    return 0 if ok_all else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="CIの検査を手元で再現する")
    ap.add_argument("--empty-docs", action="store_true",
                    help="★控えが1つも無い機械として流す★（CIと同じ条件）")
    ap.add_argument("--clone", action="store_true",
                    help="綺麗な写しを作って流す（★CIに最も近い★）")
    ap.add_argument("--audits", action="store_true",
                    help="Repository audits も流す")
    ap.add_argument("--with-net", action="store_true",
                    help="通信を断たない（ふだんは断つ）")
    ap.add_argument("--selftest", action="store_true",
                    help="この道具自身の試験")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.empty_docs:
        os.environ["UCHI_EMPTY_DOCS"] = "1"

    cmds, unread = _commands("Self-tests")
    if a.audits:
        _c2, _b2 = _commands("Repository audits")
        cmds += _c2
        unread += _b2
    if not cmds:
        print("★ワークフローからコマンドを読めませんでした★")
        return 2
    if unread:
        # ★読めない行があるなら「全部通りました」と言わない★
        print(f"★python を動かしているのに読み取れない行が {len(unread)} 本あります★")
        for x in unread:
            print("   " + x[:90])
        print("  （この形はこの道具では再現できません。"
              "ワークフローの書き方を戻すか、この道具を直してください）")
        return 2
    print(f"CIが流す検査: {len(cmds)} 本"
          + ("（通信あり）" if a.with_net else "（★通信なし★）"))

    root, tmp = BASE, None
    if a.clone:
        tmp = tempfile.mkdtemp(prefix="ci_repro_")
        root = os.path.join(tmp, "repo")
        print(f"綺麗な写しを作ります: {root}")
        r = subprocess.run(["git", "clone", "-q", BASE, root],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("★写しを作れませんでした★")
            return 2
        # ★リモートは本物に合わせる★（clone すると写し元を指してしまい、
        #   push先を見る検査が本番と違う結果になる）
        url = subprocess.run(["git", "-C", BASE, "remote", "get-url", "origin"],
                             capture_output=True, text=True).stdout.strip()
        if url:
            subprocess.run(["git", "-C", root, "remote", "set-url", "origin", url],
                           capture_output=True)
    print()
    try:
        bad = _run(cmds, root, not a.with_net)
    finally:
        if tmp:
            _rmtree_hard(tmp)

    print()
    if not bad:
        print("★全部通りました（CIと同じ条件）★")
        # ★★同じ条件ではない部分を、毎回はっきり言う★★
        #   （2026-08-21・実際にこれで8コミットぶんCIが赤くなった）
        #   ★何が起きたか★＝「動いている処理の目印は奪えない」という試験を
        #   入れた。Windows では正しいが、★Linux では開いたままでも
        #   rename できるので成り立たない★。ci_repro は手元（Windows）で
        #   走るので、この差は絶対に出ない。
        #   ＝★「全部通りました」を「CIも通る」と読まないための注意書き★
        import platform as _pf
        if _pf.system() != "Linux":
            print()
            print("★ただしOSは同じではありません★"
                  f"（ここ={_pf.system()} ／ CI=Linux）")
            print("  ★OSで振る舞いが変わるもの★＝"
                  "開いたままのファイルを消す・名前を変える／"
                  "ファイル名の大文字小文字／改行／パスの区切り")
            print("  ★これらに依存する試験を書いたら、ここでは分かりません★"
                  "（2026-08-21に実際にCIが8コミットぶん赤くなりました）")
        return 0
    print(f"★落ちたもの: {len(bad)} 本★")
    for c, out, err in bad:
        print("=" * 60)
        print(c)
        print(out)
        if err.strip():
            print("--- stderr ---")
            print(err)
    return 1


if __name__ == "__main__":
    sys.exit(main())

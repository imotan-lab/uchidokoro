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


def _commands(step: str) -> list:
    """ワークフローの指定ステップから python コマンドを読む。"""
    if not os.path.isfile(WF):
        return []
    out, inside = [], False
    for ln in open(WF, encoding="utf-8").read().splitlines():
        if re.match(r"\s*- name: " + re.escape(step) + r"\s*$", ln):
            inside = True
            continue
        if inside:
            if re.match(r"\s*- name: ", ln):
                break
            s = ln.strip()
            if s.startswith("python "):
                out.append(s)
    return out


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
            args = c.split()
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
            _sh2.rmtree(os.path.dirname(sc), ignore_errors=True)
    return bad


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
    a = ap.parse_args()
    if a.empty_docs:
        os.environ["UCHI_EMPTY_DOCS"] = "1"

    cmds = _commands("Self-tests")
    if a.audits:
        cmds += _commands("Repository audits")
    if not cmds:
        print("★ワークフローからコマンドを読めませんでした★")
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
            shutil.rmtree(tmp, ignore_errors=True)

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

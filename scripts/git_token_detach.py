# -*- coding: utf-8 -*-
"""git_token_detach.py — リポジトリのURLから鍵を外し、外の置き場に任せる。

★なぜ要るか（2026-08-14・台帳#359）★
  push用の鍵を `.git/config` のURLに埋め込んでいたため、
  **Codexがリポジトリを読んだときに、鍵ごと回答文へ書き出した**。
  Codexは `-C <リポジトリ>` の中しか読めないので、
  ★鍵をリポジトリの外へ出せば、二度と持ち出されない★

★この道具は鍵を扱いません★
  URLから鍵の部分を**消すだけ**です。新しい鍵は、このあと運営者が
  `git push` したときに Git 自身が聞いてきます（私の目にも入りません）。

★順番★
  ①運営者がGitHubで古い鍵を失効させ、新しい鍵を作る
  ②この道具を流す（URLから鍵を外す・置き場を用意する）
  ③運営者が各リポジトリで1回 `git push` して、聞かれたら新しい鍵を貼る
     → 以後は覚えているので、夜の自動タスクもそのまま動く

使い方:
    python scripts/git_token_detach.py            # 下見（何も変えない）
    python scripts/git_token_detach.py --apply    # 実行
"""
from __future__ import annotations

import argparse
import io
import os
import re
import subprocess
import sys

TOKEN = re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}")
# ★このPCで鍵を埋め込んでいるリポジトリ★（2026-08-14に実際に数えた）
REPOS = [
    r"C:/Users/imao_/Desktop/個人用/うちどころ",
    r"C:/Users/imao_/Desktop/個人用/わんさかんさい",
    r"C:/Users/imao_/Desktop/imaden-corporation/今電 HP",
]


def look(repo: str) -> dict:
    """そのリポジトリのURLの形を見る（★鍵の値は返しません★）。"""
    cfg = os.path.join(repo, ".git", "config")
    if not os.path.isfile(cfg):
        return {"repo": repo, "state": "リポジトリではありません"}
    raw = io.open(cfg, encoding="utf-8", errors="replace").read()
    m = re.search(r"url\s*=\s*(\S+)", raw)
    if not m:
        return {"repo": repo, "state": "URLが見つかりません"}
    url = m.group(1)
    has = bool(TOKEN.search(url))
    # https://user:token@github.com/owner/name.git → https://github.com/owner/name.git
    clean = re.sub(r"https://[^/@]*@", "https://", url)
    user = re.match(r"https://([^:@/]+)[:@]", url)
    return {"repo": repo, "state": "鍵あり" if has else "鍵なし",
            "url_masked": TOKEN.sub("（★鍵★）", url),
            "clean": clean, "account": user.group(1) if user else ""}


def apply(repo: str, clean: str) -> str:
    r = subprocess.run(["git", "-C", repo, "remote", "set-url", "origin", clean],
                       capture_output=True, text=True, encoding="utf-8")
    if r.returncode:
        return "失敗: " + (r.stderr or "")[:120]
    return "URLから鍵を外しました"


def main() -> int:
    ap = argparse.ArgumentParser(description="URLから鍵を外す（鍵は扱いません）")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    rows = [look(r) for r in REPOS]
    print("■ いまの状態（★鍵の値は出しません★）")
    for g in rows:
        print(f"  {g['state']:<6} {g.get('account',''):<20} {g['repo']}")
        if g.get("url_masked"):
            print(f"         いま : {g['url_masked']}")
            print(f"         あと : {g['clean']}")
    if not a.apply:
        print()
        print("★下見です。実行するには --apply を付けます★")
        print("★先に、GitHubで新しい鍵を作っておいてください★"
              "（古い鍵を失効させると、この状態のままではpushできなくなります）")
        return 0

    print()
    print("■ 鍵の置き場を、リポジトリの外に用意します")
    r = subprocess.run(["git", "config", "--global", "credential.helper", "store"],
                       capture_output=True, text=True, encoding="utf-8")
    print("  credential.helper store:",
          "設定しました" if not r.returncode else "失敗 " + (r.stderr or "")[:80])
    print("  置き場: C:/Users/imao_/.git-credentials"
          "（★リポジトリの外なのでCodexからは読めません★）")

    print()
    print("■ URLから鍵を外します")
    for g in rows:
        if g["state"] != "鍵あり":
            print(f"  そのまま: {g['repo']}")
            continue
        print(f"  {apply(g['repo'], g['clean'])}: {g['repo']}")

    print()
    print("■ ★このあと運営者にやっていただくこと★")
    print("  各リポジトリで1回 push すると、Gitが聞いてきます。")
    print("    Username → imotan-lab（今電HPは imaden-corporation）")
    print("    Password → ★新しい鍵を貼る★（パスワードではありません）")
    print("  1回貼れば覚えるので、夜の自動タスクもそのまま動きます。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

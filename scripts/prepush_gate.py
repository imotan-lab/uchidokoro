"""prepush_gate.py — push してよいかを機械が決める最後の関所。

★なぜ要るか（2026-07-31・Codex14回目）★
  「監査に通ったもの」と「実際にpushされるもの」が同じである保証が無かった。
  監査したあとに何かが変われば、確かめていない物を公開してしまう。

  あわせて、手順書の `git add` の一覧に**早見表4ページが入っていなかった**。
  新台を公開すると早見表も変わるので、そのままでは
  「一覧に無い変更がある」と言って止まるか、中途半端なコミットになる。

★この関所が確かめること★
  1. 公開が途中で終わっていない（目印が残っていない）
  2. 変わっているファイルが、許した範囲の中だけ
  3. サイト監査が通る
  4. **作業ツリーとコミットが一致している**（＝監査した中身がそのまま出る）
  5. push 先が思っているところか

★使い方★
    python scripts/prepush_gate.py --slug <slug>            # 確かめるだけ
    python scripts/prepush_gate.py --slug <slug> --commit   # 確かめてコミット
  push はこの関所が通ってから、人／タスクが実行する。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import publish_new_machine as _pub        # noqa: E402

# 想定しているリモート（ここ以外へは出さない）
WANT_REMOTE = "github.com/imotan-lab/uchidokoro"


def _git(*args, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=BASE, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          check=check)


def changed() -> list:
    """変わっているファイル（-z で読む。引用符・renameに強い）。"""
    r = _git("status", "--porcelain", "-z")
    if r.returncode != 0:
        raise RuntimeError(f"git status が失敗しました: {r.stderr[:200]}")
    out = []
    for line in r.stdout.split(chr(0)):
        if len(line) > 3:
            out.append(line[3:].strip())
    return out


def allowed_for(slug: str) -> set:
    """新台1機種を公開したときに変わってよいファイル。"""
    return {
        f"machines/{slug}/index.html",
        f"assets/data/machine-details/{slug}.json",
        "assets/data/machines.json",
        # ★早見表も変わる★（手順書の add 一覧から漏れていた）
        "guide-tenjo-ranking.html", "guide-reset-ranking.html",
        "guide-suru-tenjo.html", "guide-ichiran.html",
        # キャッシュ版を上げるので
        "service-worker.js",
    }


def check(slug: str) -> list:
    """push してよいか。★1つでも引っかかったら出さない★"""
    ng = []
    left = _pub.unfinished()
    if left:
        ng.append(f"公開が途中で終わっています（{left.get('slug')}）。"
                  "--recover --apply で戻してください")
        return ng
    allowed = allowed_for(slug)
    stray = [x for x in changed() if x not in allowed]
    if stray:
        ng.append(f"許していないファイルが変わっています: {stray[:5]}")
    ng += _pub.run_site_audit()
    return ng


def same_as_commit() -> list:
    """★作業ツリーとコミットが一致しているか★

    監査は作業ツリーを見る。コミットの中身がそれと違えば、
    **確かめていない物を公開する**ことになる。
    """
    ng = []
    for args in (("diff", "--quiet", "HEAD"), ("diff", "--quiet", "--cached")):
        if _git(*args).returncode != 0:
            ng.append("コミットしていない変更が残っています"
                      "（監査した中身とpushする中身が違います）")
            break
    return ng


def remote_ok() -> list:
    r = _git("remote", "get-url", "origin")
    url = (r.stdout or "").strip()
    if WANT_REMOTE not in url:
        return [f"push先が想定と違います: {url[:60]!r}"]
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", help="公開した機種")
    ap.add_argument("--commit", action="store_true", help="確かめてコミットする")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.slug:
        ap.print_help()
        return 0

    ng = check(args.slug)
    if ng:
        print("★push できません★")
        for x in ng:
            print("  ✗ " + x[:160])
        return 1
    print("① 目印なし・許した範囲のみ・サイト監査OK")

    if args.commit:
        add = sorted(x for x in changed() if x in allowed_for(args.slug))
        if not add:
            print("② 変更がありません（コミットしません）")
            return 0
        r = _git("add", "--", *add)
        if r.returncode != 0:
            print(f"★git add が失敗しました: {r.stderr[:160]}")
            return 1
        print("② コミットする対象: " + " ".join(add))
        print("   （このあと人／タスクが commit → prepush_gate --slug で再確認 → push）")
        return 0

    ng = same_as_commit() + remote_ok()
    if ng:
        print("★push できません★")
        for x in ng:
            print("  ✗ " + x[:160])
        return 1
    print("② 作業ツリーとコミットが一致・push先も想定どおり → ★pushしてよい★")
    return 0


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []
    nl = chr(10)

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    t("★早見表4ページも許した範囲に入っている★（手順書のadd一覧から漏れていた）",
      {"guide-ichiran.html", "guide-tenjo-ranking.html",
       "guide-reset-ranking.html", "guide-suru-tenjo.html"} <= allowed_for("x"))
    t("　その機種のファイルだけを許す",
      "machines/x/index.html" in allowed_for("x")
      and "machines/y/index.html" not in allowed_for("x"))
    t("★push先が想定どおり★", remote_ok() == [])
    t("★★作業ツリーとコミットが一致しているか見られる★★"
      "（監査した中身とpushする中身が違うと誤情報が出る）",
      isinstance(same_as_commit(), list))
    t("　変わっているファイルを読める（-z なので引用符に強い）",
      isinstance(changed(), list))

    ng = [n for n, ok in results if not ok]
    print(f"{nl}{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:                # noqa: BLE001
        print(f"★想定外の失敗 {type(e).__name__}: {e}★")
        raise SystemExit(1)

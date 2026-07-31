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
WANT_HOST = "github.com"
WANT_PATH = "imotan-lab/uchidokoro"


def _same_repo(url: str) -> bool:
    """★URLが同じ置き場を指しているか★（2026-07-31・Codex16回目）

    以前は「文字列が含まれるか」で見ていた。
    それだと `github.com/imotan-lab/uchidokoro-evil` のような
    **別の置き場でも通ってしまう**。ホストと道筋を丸ごと比べる。
    """
    u = (url or "").strip()
    u = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", u)   # 方式
    u = re.sub(r"^[^@/]*@", "", u)                      # 認証情報
    u = u.split("?", 1)[0].split("#", 1)[0]
    if ":" in u.split("/", 1)[0]:                       # git@host:owner/repo
        head, _, rest = u.partition(":")
        u = head.split(":")[0] + "/" + rest
    u = u.rstrip("/")
    if u.lower().endswith(".git"):
        u = u[:-4]
    host, _, path = u.partition("/")
    return (host.split(":")[0].lower() == WANT_HOST
            and path.strip("/").lower() == WANT_PATH.lower())


def push_remote(branch: str) -> str:
    """★引数なし `git push` が実際に使う先★（gitと同じ順で決める）

    2026-07-31・Codex16回目: `origin` だけを見ていたが、
    `branch.<名前>.pushRemote` や `remote.pushDefault` があると
    **確かめた先とは別の場所へ出る**。
    """
    for args in ((f"branch.{branch}.pushRemote",), ("remote.pushDefault",),
                 (f"branch.{branch}.remote",)):
        v = (_git("config", "--get", *args).stdout or "").strip()
        if v:
            return v
    return "origin"


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
    # ★追跡していないファイルも見る★（2026-07-31・Codex15回目）
    #   git diff は untracked を見ないので、
    #   手元にだけある物を監査が読んでいると、コミットと違う物を確かめたことになる。
    r = _git("ls-files", "--others", "--exclude-standard", "-z")
    others = [x for x in (r.stdout or "").split(chr(0)) if x]
    if others:
        ng.append(f"コミットに入らないファイルがあります: {others[:5]}")
    return ng


def push_scope() -> dict:
    """★これから何がpushされるか★（2026-07-31・Codex15〜16回目）

    最新のコミットだけ正しくても、**手前の未pushコミットも一緒に出ます**。
    リモートの先端から今のHEADまで、全部を見る。

    ★見るのは「実際にpushされる先」★
      追跡先（upstream）と push 先が別々に設定できるので、
      **push先の先端**からの差分を数える。
    """
    br = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    head = _git("rev-parse", "HEAD").stdout.strip()
    remote = push_remote(br)
    up = _git("rev-parse", "--abbrev-ref", "@{upstream}")
    upstream = up.stdout.strip() if up.returncode == 0 else ""
    # ★差分の基準は push 先のブランチ★（追跡先ではない）
    merge = (_git("config", "--get", f"branch.{br}.merge").stdout or "").strip()
    dest = merge.rsplit("/", 1)[-1] if merge else br
    base = f"{remote}/{dest}"
    if _git("rev-parse", "--verify", "--quiet", base).returncode != 0:
        base = ""
    commits, files = [], []
    if base:
        r = _git("rev-list", f"{base}..HEAD")
        commits = [x for x in r.stdout.split() if x]
        if commits:
            r2 = _git("diff", "--name-only", "-z", f"{base}..HEAD")
            files = [x for x in (r2.stdout or "").split(chr(0)) if x]
    return {"branch": br, "head": head, "upstream": upstream, "remote": remote,
            "base": base, "dest": dest, "commits": commits, "files": files}


def check_push_scope(slug: str) -> list:
    """push予定の全コミットが、許した範囲だけか。"""
    ng = []
    sc = push_scope()
    if not sc["base"]:
        ng.append(f"push先の枝（{sc['remote']}/{sc['dest']}）が手元にありません。"
                  "`git fetch` してから、もう一度実行してください")
        return ng
    if sc["branch"] != "main" or sc["dest"] != "main":
        ng.append(f"main 以外へ出そうとしています（{sc['branch']} → "
                  f"{sc['remote']}/{sc['dest']}）")
    # ★追跡先と push 先がずれていたら止める★
    #   「別の追跡先との差分を確かめて、別の場所へ出す」を防ぐ。
    if sc["upstream"] and sc["upstream"] != f"{sc['remote']}/{sc['dest']}":
        ng.append(f"追跡先（{sc['upstream']}）と push 先"
                  f"（{sc['remote']}/{sc['dest']}）が違います")
    if not sc["commits"]:
        return ng                          # 出すものが無い
    allowed = allowed_for(slug)
    stray = [x for x in sc["files"] if x not in allowed]
    if stray:
        ng.append(f"pushされる変更に、許していないファイルがあります: {stray[:5]}"
                  f"（{len(sc['commits'])} コミット分をまとめて出そうとしています）")
    return ng


def remote_ok() -> list:
    """★push先を確かめる★（読み取り用と書き込み用が別々に設定できる）"""
    ng = []
    br = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    remote = push_remote(br)
    for kind, args in (("fetch", ("remote", "get-url", remote)),
                       ("push", ("remote", "get-url", "--push", remote))):
        url = (_git(*args).stdout or "").strip()
        if not _same_repo(url):
            ng.append(f"push先（{remote} の{kind}用）が想定と違います: {url[:60]!r}")
    return ng


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
        # ★先に stage されている物が混ざらないようにする★（Codex15回目）
        if _git("diff", "--quiet", "--cached").returncode != 0:
            print("★すでに stage されている変更があります★")
            print("  `git reset` で戻してから、もう一度実行してください")
            return 1
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

    ng = same_as_commit() + remote_ok() + check_push_scope(args.slug)
    if ng:
        print("★push できません★")
        for x in ng:
            print("  ✗ " + x[:160])
        return 1
    sc = push_scope()
    print(f"② 作業ツリーとコミットが一致・push先も想定どおり")
    print(f"   出すもの: {len(sc['commits'])} コミット / "
          f"{len(sc['files'])} ファイル → {sc['remote']}/{sc['dest']}")
    print(f"   push元: {sc['head'][:12]}")
    print("★pushしてよい★")
    return 0


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    import inspect
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
    _sc = push_scope()
    t("★★これから何がpushされるか分かる★★（手前の未pushコミットも一緒に出る）",
      isinstance(_sc.get("commits"), list) and _sc.get("branch") == "main")
    t("　push用URLも読み取り用と別に確かめる",
      len(remote_ok()) == 0)
    t("★★作業ツリーとコミットが一致しているか見られる★★"
      "（監査した中身とpushする中身が違うと誤情報が出る）",
      isinstance(same_as_commit(), list))
    t("　変わっているファイルを読める（-z なので引用符に強い）",
      isinstance(changed(), list))

    t("★★置き場の名前が似ているだけの別リポジトリを弾く★★"
      "（含まれるかで見ていたので通っていた・Codex16回目）",
      _same_repo("https://github.com/imotan-lab/uchidokoro.git")
      and _same_repo("git@github.com:imotan-lab/uchidokoro")
      and _same_repo("https://tok@github.com/imotan-lab/uchidokoro.git")
      and not _same_repo("https://github.com/imotan-lab/uchidokoro-evil.git")
      and not _same_repo("https://evil.com/github.com/imotan-lab/uchidokoro")
      and not _same_repo("https://github.com/other/uchidokoro"))
    t("★★push先を git と同じ順で決める★★"
      "（pushRemote / pushDefault があると別の場所へ出る）",
      "pushRemote" in inspect.getsource(push_remote)
      and "remote.pushDefault" in inspect.getsource(push_remote))
    t("　差分の基準も push 先の枝にする",
      push_scope().get("base") == "origin/main")
    t("　追跡先と push 先がずれていたら止める",
      "追跡先" in inspect.getsource(check_push_scope))

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

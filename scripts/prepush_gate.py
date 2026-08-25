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

# ★出力の文字コードを固定する★（2026-08-01・実際にpushまで通して見つけた）
#   Windowsでパイプ越しに動かすと出力がcp932になり、
#   止まった理由（✗つきの行）を印字した瞬間に落ちて理由が失われていた。
for _s in (sys.stdout, sys.stderr):
    if _s is not None and hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

import publish_new_machine as _pub        # noqa: E402
import safe_json as _sj                 # noqa: E402

# ★push待ちの目印★（公開の目印は関所より先に消えるため）
PUSH_PENDING = os.path.join(_pub.BASE, ".push-pending.json")

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
    return _run_capped(["git", *args], cwd=BASE, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          check=check)


def changed() -> list:
    """変わっているファイル（-z で読む。引用符・renameに強い）。

    ★-uall が必須★（2026-08-01・実際にpushまで通して見つけた）
      git は新しいフォルダを「フォルダごと1行」（machines/xxx/）で報告する。
      許可リストはファイル単位なので突き合わせられず、
      **新台（必ず新フォルダを作る）のpushを全部拒否していた**。
      公開側（publish_new_machine の changed_paths）は同じ穴を先に直してあったのに、
      この関所だけ直っていなかった。-uall なら git が中のファイルを1つずつ返す。
    """
    r = _git("status", "--porcelain", "-z", "-uall")
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
        # ★★service-worker.js は許可しない★★（2026-08-25・Codexの25回目）
        #   ★新台の公開経路は、このファイルを1文字も書いていない★
        #   （publish_new_machine / build_new_article のどちらにも登場しない）。
        #   丸ごと許可していたので、**実行前から残っていた変更が
        #   新台のコミットに便乗して公開できた**。
        #   ＝関所が「この機種の公開に伴う変更だけ」を通す約束を破っていた。
        # ★AUTO_INDEXABLE の公開では sitemap にも1件足す★（2026-08-04・Codex72回目）
        "sitemap.xml",
    }


# ★★外部プロセスには必ず打ち切り時間を付ける★★（2026-08-25・Codexの26回目）
PROC_TIMEOUT = 300


def _run_capped(args, **kw):
    """打ち切り時間つきで外部プロセスを動かす（既定 PROC_TIMEOUT 秒）。"""
    kw.setdefault("timeout", PROC_TIMEOUT)
    return subprocess.run(args, **kw)   # ★ここだけ素の呼び出し★


def _dirty_before(slug: str):
    """★「公開を始める前に変わっていたファイル」を、目印から取る★

    ★★2つの目印を見る★★（2026-08-25・Codexの26回目）
      ★公開の目印（.publish-in-progress.json）は、関所を呼ぶ**前**に
        消される★ので、それだけを見ていた前の作りでは
        **通常の経路で一度も読めていなかった**（＝遮断が働いていなかった）。
      push をやり直す経路も push待ちの目印しか読まないので、両方を見る。

    返すもの: (一覧, 止める理由)
      一覧が None のときは「分からない」＝止める。
    """
    for path, name in ((_pub.IN_PROGRESS, "公開の目印"),
                       (PUSH_PENDING, "push待ちの目印")):
        try:
            m = _sj.read_json(path, expect=dict, allow_missing=True,
                              default=None)
        except Exception as e:                               # noqa: BLE001
            return None, f"★{name}を読めません（{type(e).__name__}）★"
        if not isinstance(m, dict):
            continue                       # その目印は無い
        if str(m.get("slug") or "") != slug:
            return None, (f"★{name}の機種（{m.get('slug')!r}）が"
                          f"いま出そうとしている機種（{slug!r}）と違います★")
        if "dirty_before" not in m:
            return None, (f"★{name}に「始める前の状態」が控えられていません★"
                          "／★この公開が作った変更だけかを確かめられません★")
        v = m.get("dirty_before")
        # ★★null を「綺麗だった」と読まない★★（Codexの26回目）
        #   git status が失敗すると None が入る。空配列と同じ扱いにすると、
        #   ★確かめられなかったものを「確かめた」ことにしてしまう★。
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            return None, (f"★{name}の「始める前の状態」が読めない形です"
                          f"（{type(v).__name__}）★")
        return v, ""
    return None, ""                        # どちらの目印も無い＝この経路ではない


def preexisting(slug: str) -> list:
    """★この公開が作ったのではない変更が、許可対象に残っていないか★

    ★なぜ要るか（2026-08-25・Codexの25回目）★
      許可一覧は「新台1機種の公開で変わってよいファイル」を並べたもので、
      ★変更の**理由**は見ていない★。
      そのため、実行前から残っていた別の変更（例：既存機種の記述を
      書き換えた `machines.json`）が、同じ名前のファイルというだけで
      **新台のコミットに便乗して公開**できた。
    ★読めないときは「分からない」と答える★（fail-closed）。
    """
    got, why = _dirty_before(slug)
    if why:
        return [why]
    if got is None:
        return []                          # どちらの目印も無い（この経路ではない）
    rode = sorted(set(got) & allowed_for(slug))
    if rode:
        return ["★この公開が作ったのではない変更が、許可対象に残っています"
                "（便乗して公開されます）: " + " / ".join(rode[:5]) + "★"]
    return []


def check(slug: str) -> list:
    """push してよいか。★1つでも引っかかったら出さない★"""
    ng = list(preexisting(slug))
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


def _hide(url: str) -> str:
    """★URLに埋めてある鍵を隠す★（2026-07-31・自分で気づいた）

    このリポジトリの remote URL には利用者名と個人アクセストークンが
    埋め込まれている。エラー文にそのまま出すと、
    **ログや画面にトークンが残る**。
    """
    return re.sub(r"//[^@/]*@", "//***@", url or "")


def remote_ok() -> list:
    """★push先を確かめる★（読み取り用と書き込み用が別々に設定できる）

    ★URLは1つとは限らない★（2026-07-31・Codex19回目）
      `remote.<名>.pushurl` は複数書ける。書いてあれば **全部へ** push される。
      `get-url` は既定で先頭しか返さないので、先頭だけ見ていると
      確かめていない置き場へも出てしまう。
    """
    ng = []
    br = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    remote = push_remote(br)
    for kind, args in (("fetch", ("remote", "get-url", "--all", remote)),
                       ("push", ("remote", "get-url", "--push", "--all", remote))):
        urls = [x for x in (_git(*args).stdout or "").split() if x]
        if not urls:
            ng.append(f"push先（{remote} の{kind}用）が分かりません")
            continue
        for url in urls:
            if not _same_repo(url):
                ng.append(f"push先（{remote} の{kind}用）が想定と違います: "
                          f"{_hide(url)[:60]!r}")
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
    # ★新しいフォルダを「フォルダごと1行」ではなくファイル単位で見る★
    #   （2026-08-01・実際にpushまで通して見つけた。
    #     新台は必ず新フォルダを作るので、これが無いと全部の新台pushを拒否する）
    _d = os.path.join(BASE, "machines", "zzz_gate_selftest")
    try:
        os.makedirs(_d, exist_ok=True)
        for _n in ("a.html", "b.html"):
            with open(os.path.join(_d, _n), "w", encoding="utf-8") as _f:
                _f.write("<!-- selftest -->")
        _got = changed()
        t("★★新フォルダの中身がファイル単位で見える★★"
          "（フォルダごと1行だと許可リストと突き合わせられない）",
          "machines/zzz_gate_selftest/a.html" in _got
          and "machines/zzz_gate_selftest/b.html" in _got
          and "machines/zzz_gate_selftest/" not in _got)
    finally:
        import shutil as _sh
        _sh.rmtree(_d, ignore_errors=True)

    t("★★エラー文に鍵を出さない★★（remote URL にトークンが埋めてある）",
      "***@" in _hide("https://user:ghp_secret@github.com/a/b.git")
      and "ghp_secret" not in _hide("https://user:ghp_secret@github.com/a/b.git"))
    t("★★push先のURLを全部見る★★（pushurl は複数書けて、全部へ出る）",
      "--all" in inspect.getsource(remote_ok))
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

    # ★★この公開が作ったのではない変更を、便乗させない★★
    #   （2026-08-25・Codexの25回目）
    #   ★許可一覧は「変わってよいファイル名」しか見ていない★ので、
    #   実行前から残っていた別の変更が新台のコミットに便乗して公開できた。
    import json as _json25
    import tempfile as _tf25
    _keep_ip = _pub.IN_PROGRESS
    try:
        _pub.IN_PROGRESS = os.path.join(_tf25.mkdtemp(prefix="pg25_"),
                                        ".publish-in-progress.json")

        def _mark25(before):
            _d = {"slug": "dmm_9999", "name": "試験機", "restore": {},
                  "created": {}}
            if before is not None:
                _d["dirty_before"] = before
            _json25.dump(_d, open(_pub.IN_PROGRESS, "w", encoding="utf-8"),
                         ensure_ascii=False)

        _mark25(["assets/data/machines.json"])
        t("★★始める前からある変更が許可対象なら止める★★"
          "／★変更の『理由』を見ないと、別の書き換えが便乗して公開される★",
          bool(preexisting("dmm_9999")))
        _mark25([])
        t("　始める前が綺麗なら通す", not preexisting("dmm_9999"))
        _mark25(None)
        t("★★控えが無ければ『分からない』と答えて止まる★★（fail-closed）",
          bool(preexisting("dmm_9999")))
        os.remove(_pub.IN_PROGRESS)
        t("　目印が無いとき（この経路ではない）は何も言わない",
          not preexisting("dmm_9999"))
    finally:
        _pub.IN_PROGRESS = _keep_ip
    # ★★★本番と同じ順序で通す試験★★★（2026-08-25・Codexの26回目）
    #   ★★前の試験は preexisting() を直接呼んでいた★★ので、
    #   **本番では公開の目印が関所より先に消える**ことに気づけなかった。
    #   ＝便乗の遮断は、通常の経路で一度も働いていなかった。
    #   → 「公開の目印を作る → 消える → push待ちの目印だけが残る → 関所」
    #     の順を、そのまま並べて確かめる。
    import json as _json26
    import tempfile as _tf26
    _keep_ip26, _keep_pp26 = _pub.IN_PROGRESS, PUSH_PENDING
    try:
        _d26 = _tf26.mkdtemp(prefix="pg26_")
        _pub.IN_PROGRESS = os.path.join(_d26, ".publish-in-progress.json")
        globals()["PUSH_PENDING"] = os.path.join(_d26, ".push-pending.json")

        def _seq26(dirty, drop_ip=True, pp_has=True):
            """公開の目印を作り、消し、push待ちの目印だけを残す。"""
            _json26.dump({"slug": "dmm_9999", "name": "試験機",
                          "restore": {}, "created": {},
                          "dirty_before": dirty},
                         open(_pub.IN_PROGRESS, "w", encoding="utf-8"),
                         ensure_ascii=False)
            _pp = {"slug": "dmm_9999", "sha": "x", "stage": "COMMITTED",
                   "parent": "y", "at": "2026-08-25 00:00:00"}
            if pp_has:
                _pp["dirty_before"] = dirty
            _json26.dump(_pp, open(PUSH_PENDING, "w", encoding="utf-8"),
                         ensure_ascii=False)
            if drop_ip:                    # ★本番はここで消える★
                os.remove(_pub.IN_PROGRESS)

        _seq26(["assets/data/machines.json"])
        t("★★★本番の順序でも、便乗した変更を止める★★★"
          "／★公開の目印は関所より先に消えるので、引き継ぎが要る★",
          bool(preexisting("dmm_9999")))
        _seq26([])
        t("　本番の順序で、始める前が綺麗なら通す",
          not preexisting("dmm_9999"))
        _seq26([], pp_has=False)
        t("★★引き継がれていなければ止まる★★（＝遮断が働いていない状態）",
          bool(preexisting("dmm_9999")))
        _seq26(None)
        t("★★null（git status が失敗）を「綺麗だった」と読まない★★",
          bool(preexisting("dmm_9999")))
        _seq26([])
        _pp26 = _json26.load(open(PUSH_PENDING, encoding="utf-8"))
        _pp26["slug"] = "dmm_1111"
        _json26.dump(_pp26, open(PUSH_PENDING, "w", encoding="utf-8"),
                     ensure_ascii=False)
        t("　目印の機種が違えば止める",
          bool(preexisting("dmm_9999")))
        for _f26 in (_pub.IN_PROGRESS, PUSH_PENDING):
            if os.path.isfile(_f26):
                os.remove(_f26)
        t("　どちらの目印も無いとき（この経路ではない）は何も言わない",
          not preexisting("dmm_9999"))
    finally:
        _pub.IN_PROGRESS = _keep_ip26
        globals()["PUSH_PENDING"] = _keep_pp26

    # ★★関所の本体が、この検査を通っていること★★（2026-08-25）
    #   ★直す前は preexisting() を直接呼ぶ試験しか無かった★ので、
    #   `check()` からの配線を切っても**試験は緑のまま**だった
    #   （壊し方の通し確認が検知）。
    _keep_pre = preexisting
    try:
        globals()["preexisting"] = lambda slug: ["zzz_便乗の目印"]
        _out_pre = check("dmm_9999")
    except Exception as _e_pre:                              # noqa: BLE001
        _out_pre = [f"例外: {type(_e_pre).__name__}"]
    finally:
        globals()["preexisting"] = _keep_pre
    t("★★関所の本体が、便乗の検査を必ず通る★★"
      "／★配線を切ると、便乗した変更がそのまま公開される★",
      any("zzz_便乗の目印" in str(x) for x in (_out_pre or [])))

    t("★★service-worker.js は許可しない★★"
      "／★新台経路は1文字も書かないのに、丸ごと許可されていた★",
      "service-worker.js" not in allowed_for("dmm_9999"))
    # ★★集計は、すべての試験の後ろに置く★★（2026-08-25・監査51が検知）
    #   ★足した5件が集計より前に無かった★ので、
    #   その分は数えられず、**落ちても合格と出る**状態だった。
    #   ＝プロジェクトが見張っている「早すぎる数え方」を自分でやっていた。
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

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


def remote_main_tip(timeout: int = 120) -> tuple:
    """★GitHubのmainの先端はどれか★（2026-08-29・Codexのレビュー17〜18）

    ★★これは「読者が見ているもの」ではない★★（レビュー18・重大）
      このサイトは GitHub Actions で配信されるので、
      ★mainへ出した＝読者に届いた、ではない★
      （非同期・配信の切替が mirror でなければ動かない・途中で落ちうる）。
      読者が見ているものは `deployed_tip()` で聞く。
      ここが答えるのは「★出したか★」まで。

    ★返すもの★＝(先端のSHA, 出せない理由)。
      先端が返るのは、次が全部そろったときだけ（fail-closed）。
        ①公開先が main（枝も出す先も）
        ②追跡先とpush先がずれていない
        ③読み取り用・書き込み用のURLが、どちらも想定の置き場
        ④実際のリモートに聞いた先端が、1行・2列・聞いた枝・16進
        ⑤手元の基準（origin/main）が、その先端と同じ

    ★★手元にあるものを公開先と取り違えない★★
      `git branch -r --contains` も `origin/<いまの枝>` も、
      ★手元の写しでしかない★（古いことも、別の枝であることもある）。
      同じ取り違えを新台と更新で1回ずつやったので、ここに集約する。
    """
    try:
        sc = push_scope()
    except Exception as e:                # noqa: BLE001
        return "", f"push先を調べられません（{str(e)[:60]}）"
    base = sc.get("base") or ""
    remote = sc.get("remote") or ""
    dest = sc.get("dest") or ""
    branch = sc.get("branch") or ""
    if not (base and remote and dest):
        return "", "push先が分かりません"
    if branch != "main" or dest != "main":
        return "", f"公開先が main ではありません（{branch} → {remote}/{dest}）"
    up = sc.get("upstream") or ""
    if up and up != f"{remote}/{dest}":
        return "", f"追跡先（{up}）とpush先（{remote}/{dest}）が違います"
    try:
        bad = remote_ok()
    except Exception as e:                # noqa: BLE001
        return "", f"push先のURLを確かめられません（{str(e)[:60]}）"
    if bad:
        return "", "push先のURLが想定と違います: " + str(bad[0])[:80]
    want = f"refs/heads/{dest}"
    try:
        lr = _run_capped(["git", "ls-remote", "--refs", "--exit-code",
                          remote, want], cwd=BASE, capture_output=True,
                         text=True, encoding="utf-8", errors="replace",
                         timeout=timeout)
    except Exception as e:                # noqa: BLE001
        return "", f"リモートの先端を確かめられません（{str(e)[:60]}）"
    if lr.returncode != 0:
        return "", "リモートの先端を確かめられません"
    lines = [x for x in (lr.stdout or "").splitlines() if x.strip()]
    if len(lines) != 1:
        return "", f"リモートの答えが1行ではありません（{len(lines)}行）"
    cols = lines[0].split()
    if len(cols) != 2 or cols[1] != want:
        return "", "リモートの答えの形が違います"
    tip = cols[0]
    # ★16進であること★（長さは決め打ちにしない＝Gitのハッシュ方式は変わり得る）
    if not tip or any(c not in "0123456789abcdef" for c in tip.lower()):
        return "", "リモートの先端が16進表記ではありません"
    try:
        b = _run_capped(["git", "rev-parse", base], cwd=BASE,
                        capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    except Exception as e:                # noqa: BLE001
        return "", f"手元の基準を調べられません（{str(e)[:60]}）"
    base_sha = (b.stdout or "").strip()
    if b.returncode != 0 or not base_sha:
        return "", "手元の基準を調べられません"
    if base_sha != tip:
        return "", (f"手元の基準（{base_sha[:12]}）とリモートの先端"
                    f"（{tip[:12]}）が違います")
    return tip, ""


def _api(path: str, timeout: int = 60):
    """★GitHubに問い合わせる（読み取りだけ・認証なし）★

    ★返すもの★＝(中身, 理由)。読めなければ中身は None（fail-closed）。
    """
    import json as _json
    import urllib.error
    import urllib.request
    url = f"https://api.github.com/repos/{WANT_PATH}{path}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "uchidokoro-deploy-check"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as f:
            if f.status != 200:
                return None, f"配信の記録を読めません（{f.status}）"
            return _json.loads(f.read().decode("utf-8")), ""
    except Exception as e:                # noqa: BLE001
        return None, f"配信の記録を読めません（{type(e).__name__}）"


# ★配信してよいワークフロー★（2026-08-29・Codexのレビュー19）
#   ★同じコミットでも、出す中身は別★＝
#     publish-pages.yml  … リポジトリ直下をそのまま出す（mirror）
#     pages-rehearsal.yml … 組み立てた `_site` を出す（verified）
#   いまの検査はリポジトリの編集用データを読むので、
#   ★verified で配信されていたら、読者が見ていないものを検査している★。
#   当面は mirror だけ受け取り、それ以外は断る（fail-closed）。
MIRROR_WORKFLOW = ".github/workflows/publish-pages.yml"


def _latest_status(statuses: list):
    """★いちばん新しい状態を選ぶ★（並び順を仮定しない）

    （2026-08-29・Codexのレビュー19・軽微）実測では新しい順だが、
    仕様に明記が見つからないので、番号と時刻で選ぶ。
    """
    ok = [s for s in statuses if isinstance(s, dict)]
    if not ok:
        return None
    return max(ok, key=lambda s: (str(s.get("created_at") or ""),
                                  int(s.get("id") or 0)))


def _deploy_workflow(status: dict, timeout: int) -> tuple:
    """★その配信を出したワークフローの道筋★（分からなければ空＝fail-closed）"""
    url = str(status.get("target_url") or status.get("log_url") or "")
    m = re.search(r"/actions/runs/(\d+)", url)
    if not m:
        return "", "配信を出した仕組みが分かりません"
    run, why = _api(f"/actions/runs/{m.group(1)}", timeout=timeout)
    if run is None:
        return "", why
    if not isinstance(run, dict):
        return "", "配信を出した仕組みの記録の形が違います"
    path = str(run.get("path") or "")
    if not path:
        return "", "配信を出した仕組みの道筋がありません"
    return path, ""


def deployed_tip(timeout: int = 60) -> tuple:
    """★いま読者に届いているのはどのコミットか★
       （2026-08-29・Codexのレビュー18〜19）

    ★返すもの★＝(配信されたSHA, 届いていると言えない理由, 配信の番号)

    ★★なぜ main の先端では駄目か★★
      このサイトは GitHub Actions で配信される。
      ・pushのあと★非同期で★動く
      ・配信の切替（PAGES_DEPLOY_MODE）が mirror でなければ**動かない**
      ・中の検査で落ちることがある
      ＝main が新しくなった直後でも、★読者はまだ前の中身を見ている★。

    ★★「いちばん新しい試み」ではなく「いま生きている成功版」★★
      （レビュー19・中）新しい配信が成功すると、前の配信は `inactive` になる。
      ＝いちばん新しい配信が失敗・進行中でも、
      ★読者はひとつ前の成功版を見ている★。
      直す前は「いちばん新しいのが成功でなければ答えない」だったので、
      ★配信が一度失敗すると、次の成功まで再検査できなくなる★。

    ★★同じコミットでも、出す中身は別★★（レビュー19・重大）
      配信の記録に入っているのはコミットであって中身の指紋ではない。
      いまの検査はリポジトリの編集用データを読むので、
      ★mirror で配信されたものだけ★受け取る。
    """
    try:
        bad = remote_ok()
    except Exception as e:                # noqa: BLE001
        return "", f"push先のURLを確かめられません（{str(e)[:60]}）", 0
    if bad:
        return "", "push先のURLが想定と違います: " + str(bad[0])[:80], 0
    got, why = _api("/deployments?environment=github-pages&per_page=10",
                    timeout=timeout)
    if got is None:
        return "", why, 0
    if not isinstance(got, list) or not got:
        return "", "配信の記録がありません", 0
    # ★新しい順に見て、いま生きている成功版を探す★
    for dep in got:
        if not isinstance(dep, dict):
            return "", "配信の記録の形が違います", 0
        dep_id = dep.get("id")
        if not isinstance(dep_id, int):
            return "", "配信の記録に番号がありません", 0
        st, why2 = _api(f"/deployments/{dep_id}/statuses?per_page=20",
                        timeout=timeout)
        if st is None:
            return "", why2, 0
        if not isinstance(st, list):
            return "", "配信の状態が分かりません", 0
        cur = _latest_status(st)
        if cur is None:
            return "", "配信の状態が分かりません", 0
        state = str(cur.get("state") or "")
        if state in ("queued", "waiting", "pending", "in_progress"):
            continue          # ★まだ切り替わっていない＝前の版を見ている★
        if state != "success":
            continue          # ★失敗・inactive は、いま出ているものではない★
        sha = str(dep.get("sha") or "")
        if not sha or any(c not in "0123456789abcdef" for c in sha.lower()):
            return "", "配信されたコミットが16進表記ではありません", 0
        wf, why3 = _deploy_workflow(cur, timeout)
        if not wf:
            return "", why3, 0
        if wf != MIRROR_WORKFLOW:
            # ★同じコミットでも中身が違う★＝検査するものが読者の見ているものと違う
            return "", (f"いまの配信は {wf} が出したものです"
                        "（リポジトリの中身をそのまま出す配信ではないので、"
                        "手元のデータを検査しても読者の見ているものとは"
                        "限りません）"), 0
        return sha, "", dep_id
    return "", "いま生きている成功した配信が見つかりません", 0


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
    # ★★「いま読者に出ている先端」の求め方★★
    #   （2026-08-29・Codexのレビュー17）
    #   ★同じ取り違えを2回やった★＝新台は実行時の枝、
    #   更新は手元の追跡ref を「公開先」と取り違えていた。
    #   ★条件は1つずつ壊す★（2つ同時に変えると、片方の検査を削っても緑）
    _PT = {}

    def _pt_reset():
        _PT.clear()
        _PT.update({"scope": {"base": "origin/main", "remote": "origin",
                              "dest": "main", "branch": "main",
                              "upstream": "origin/main"},
                    "bad": [], "ls_rc": 0,
                    "lsout": "aaa\trefs/heads/main", "base": "aaa"})

    def _pt_run(args, **kw):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        r = R()
        a = list(args)
        if "ls-remote" in a:
            r.stdout = _PT["lsout"]
            r.returncode = _PT["ls_rc"]
        elif "rev-parse" in a:
            r.stdout = _PT["base"]
        return r

    _pt_reset()
    _keep_run_pt = globals()["_run_capped"]
    _keep_scope = globals()["push_scope"]
    _keep_rok = globals()["remote_ok"]
    try:
        globals()["_run_capped"] = _pt_run
        globals()["push_scope"] = lambda: _PT["scope"]
        globals()["remote_ok"] = lambda: _PT["bad"]
        t("　★全部そろえば先端を返す★", remote_main_tip() == ("aaa", ""))
        _pt_reset()
        _PT["scope"] = dict(_PT["scope"], branch="side")
        t("★★手元の枝が main でなければ先端を返さない★★"
          "／★別の枝の先端を公開先と取り違えない★",
          remote_main_tip()[0] == "")
        _pt_reset()
        _PT["scope"] = dict(_PT["scope"], dest="side",
                            upstream="origin/side")
        t("★★出す先が main でなければ先端を返さない★★",
          remote_main_tip()[0] == "")
        _pt_reset()
        _PT["scope"] = dict(_PT["scope"], upstream="origin/betsu")
        t("　★追跡先とpush先がずれていたら返さない★",
          remote_main_tip()[0] == "")
        _pt_reset()
        _PT["bad"] = ["push先が想定と違います"]
        t("★★読み書きのURLが想定と違えば返さない★★"
          "／★読み取り側にだけ載っていても通ってしまう★",
          remote_main_tip()[0] == "")
        _pt_reset()
        _PT["ls_rc"] = 2
        t("　★リモートを見に行けなければ返さない★",
          remote_main_tip()[0] == "")
        _pt_reset()
        _PT["lsout"] = "aaa\trefs/heads/main\nbbb\trefs/heads/main"
        t("　★答えが1行でなければ返さない★", remote_main_tip()[0] == "")
        _pt_reset()
        _PT["lsout"] = "aaa\trefs/heads/betsu"
        t("　★聞いた枝と違う答えなら返さない★", remote_main_tip()[0] == "")
        _pt_reset()
        _PT["lsout"] = "zzz\trefs/heads/main"
        _PT["base"] = "zzz"
        t("　★16進でない先端は返さない★", remote_main_tip()[0] == "")
        _pt_reset()
        _PT["base"] = "bbb"
        t("★★手元の基準が実リモートの先端と違えば返さない★★"
          "／★手元の写しが古いまま判断させない★",
          remote_main_tip()[0] == "")
        _pt_reset()

        def _pt_boom(args, **kw):
            raise OSError("ためしの時間切れ")
        globals()["_run_capped"] = _pt_boom
        t("　★外部プロセスが落ちても、例外にせず理由を返す★",
          remote_main_tip()[0] == "")
    finally:
        globals()["_run_capped"] = _keep_run_pt
        globals()["push_scope"] = _keep_scope
        globals()["remote_ok"] = _keep_rok

    # ★★「読者に届いたか」の条件を1つずつ試す★★
    #   （2026-08-29・Codexのレビュー18〜19）
    #   ★mainへ出した＝読者に届いた、ではない★＝
    #   このサイトは GitHub Actions が非同期で配信するので、
    #   出した直後でも読者はまだ古い中身を見ている。
    _DP = {}
    _MIRROR_URL = ("https://github.com/x/y/actions/runs/111/job/9")
    _VERIF_URL = ("https://github.com/x/y/actions/runs/222/job/9")

    def _dp_reset():
        _DP.clear()
        _DP.update({
            "bad": [],
            "dep": [{"id": 9, "sha": "abc123"}],
            # 配信の番号 → その状態の並び
            "st": {9: [{"id": 2, "state": "success",
                        "created_at": "2026-08-29T01:00:02Z",
                        "target_url": _MIRROR_URL},
                       {"id": 1, "state": "in_progress",
                        "created_at": "2026-08-29T01:00:01Z",
                        "target_url": _MIRROR_URL}]},
            "runs": {"111": {"path": MIRROR_WORKFLOW},
                     "222": {"path": ".github/workflows/pages-rehearsal.yml"}},
        })

    def _dp_api(path, timeout=60):
        if path.startswith("/deployments?"):
            return (_DP["dep"], "") if _DP["dep"] is not None \
                else (None, "読めません")
        if path.startswith("/deployments/"):
            did = int(path.split("/")[2].split("?")[0])
            got = _DP["st"].get(did)
            return (got, "") if got is not None else (None, "読めません")
        if path.startswith("/actions/runs/"):
            rid = path.rsplit("/", 1)[-1]
            got = _DP["runs"].get(rid)
            return (got, "") if got is not None else (None, "読めません")
        return None, "知らない問い合わせ"

    _dp_reset()
    _keep_api = globals()["_api"]
    _keep_rok_dp = globals()["remote_ok"]
    try:
        globals()["_api"] = _dp_api
        globals()["remote_ok"] = lambda: _DP["bad"]
        t("　★生きている成功した配信があれば、そのコミットと番号を返す★",
          deployed_tip() == ("abc123", "", 9))

        # ★★いちばん新しい試みではなく、いま生きている成功版★★
        #   （レビュー19・中）新しい配信が成功すると前は inactive になる。
        #   ★いちばん新しいのが失敗・進行中でも、
        #     読者はひとつ前の成功版を見ている★。
        #   直す前は「いちばん新しいのが成功でなければ答えない」だったので、
        #   ★配信が一度失敗すると、次の成功まで再検査できなくなっていた★。
        for _bad_state in ("in_progress", "failure", "error"):
            _dp_reset()
            _DP["dep"] = [{"id": 10, "sha": "def456"},
                          {"id": 9, "sha": "abc123"}]
            _DP["st"][10] = [{"id": 5, "state": _bad_state,
                              "created_at": "2026-08-29T02:00:00Z",
                              "target_url": _MIRROR_URL}]
            t(f"★★いちばん新しい配信が {_bad_state} でも、"
              "いま生きている成功版を返す★★"
              "／★直す前は、一度失敗すると再検査できなくなっていた★",
              deployed_tip() == ("abc123", "", 9))
        _dp_reset()
        _DP["dep"] = [{"id": 10, "sha": "def456"},
                      {"id": 9, "sha": "abc123"}]
        _DP["st"][10] = [{"id": 5, "state": "success",
                          "created_at": "2026-08-29T02:00:00Z",
                          "target_url": _MIRROR_URL}]
        _DP["st"][9] = [{"id": 6, "state": "inactive",
                         "created_at": "2026-08-29T02:00:01Z",
                         "target_url": _MIRROR_URL},
                        {"id": 2, "state": "success",
                         "created_at": "2026-08-29T01:00:02Z",
                         "target_url": _MIRROR_URL}]
        t("★★古い成功版が inactive になったら、そちらは答えにしない★★",
          deployed_tip() == ("def456", "", 10))

        # ★★同じコミットでも、出す中身は別★★（レビュー19・重大）
        _dp_reset()
        _DP["st"][9][0]["target_url"] = _VERIF_URL
        t("★★組み立てた中身を出す配信なら、答えない★★"
          "／★手元のデータを検査しても、読者が見ているものとは限らない★",
          deployed_tip()[0] == "")
        _dp_reset()
        _DP["st"][9][0].pop("target_url")
        t("★★配信を出した仕組みが分からなければ答えない★★",
          deployed_tip()[0] == "")
        _dp_reset()
        _DP["runs"] = {}
        t("　★配信を出した仕組みの記録を読めなければ答えない★",
          deployed_tip()[0] == "")

        # ★★状態の並び順を仮定しない★★（レビュー19・軽微）
        _dp_reset()
        _DP["st"][9] = list(reversed(_DP["st"][9]))
        t("　★状態が古い順に並んでいても、いちばん新しいものを見る★",
          deployed_tip() == ("abc123", "", 9))

        _dp_reset()
        _DP["dep"] = []
        t("　★配信の記録が無ければ答えない★", deployed_tip()[0] == "")
        _dp_reset()
        _DP["dep"] = None
        t("　★配信の記録を読めなければ答えない★", deployed_tip()[0] == "")
        _dp_reset()
        _DP["st"] = {}
        t("　★配信の状態を読めなければ答えない★", deployed_tip()[0] == "")
        _dp_reset()
        _DP["dep"] = [{"id": 9, "sha": "zzz"}]
        t("　★16進でないコミットは答えない★", deployed_tip()[0] == "")
        _dp_reset()
        _DP["dep"] = [{"sha": "abc123"}]
        t("　★配信の記録に番号が無ければ答えない★",
          deployed_tip()[0] == "")
        _dp_reset()
        _DP["bad"] = ["push先が想定と違います"]
        t("★★置き場が想定と違えば、配信も聞きに行かない★★"
          "（別の置き場の配信を答えにしない）",
          deployed_tip()[0] == "")
    finally:
        globals()["_api"] = _keep_api
        globals()["remote_ok"] = _keep_rok_dp

    # ★★本物の git で試す★★（2026-08-29・Codexのレビュー17）
    #   ★偽物だけで固めると、実際の ref の扱いを一度も試していない★
    #   ここでは一時の置き場（bare）と作業用の写しを本当に作って動かす。
    #   ★URLの検査だけは外す★＝一時リポジトリはうちどころの置き場ではないので、
    #   そこで落ちると他の条件を一度も試せない（罠④）。
    import io as _io
    import shutil as _sh
    import subprocess as _sp
    import tempfile as _tf
    _rg = _tf.mkdtemp()
    _keep_base_rg = globals()["BASE"]
    _keep_rok_rg = globals()["remote_ok"]

    def _rgit(*a, cwd=None):
        return _sp.run(["git", *a], cwd=cwd or _work, capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       timeout=60)
    try:
        _bare = os.path.join(_rg, "okiba.git")
        _work = os.path.join(_rg, "tesaki")
        _sp.run(["git", "init", "--bare", "-b", "main", _bare],
                capture_output=True, timeout=60)
        _sp.run(["git", "clone", _bare, _work], capture_output=True,
                timeout=60)
        _rgit("config", "user.email", "t@example.invalid")
        _rgit("config", "user.name", "t")
        _io.open(os.path.join(_work, "a.txt"), "w",
                encoding="utf-8").write("1")
        _rgit("add", "a.txt")
        _rgit("commit", "-m", "one")
        _rgit("push", "-u", "origin", "main")
        globals()["BASE"] = _work
        globals()["remote_ok"] = lambda: []
        _tip1 = _rgit("rev-parse", "HEAD").stdout.strip()
        t("★★本物のgit：ふつうに出した直後は、その先端を返す★★",
          remote_main_tip() == (_tip1, ""))

        # ★別の枝に切り替えると返さない★
        _rgit("checkout", "-b", "side")
        _io.open(os.path.join(_work, "b.txt"), "w",
                encoding="utf-8").write("2")
        _rgit("add", "b.txt")
        _rgit("commit", "-m", "two")
        _rgit("push", "-u", "origin", "side")
        t("★★本物のgit：別の枝にいるときは先端を返さない★★"
          "／★その枝に載っているだけで「公開した」ことにしない★",
          remote_main_tip()[0] == "")
        _rgit("checkout", "main")

        # ★手元の写しが古いと返さない★
        #   （別の作業から置き場を進め、手元は取り直さない）
        _work2 = os.path.join(_rg, "betsu")
        _sp.run(["git", "clone", _bare, _work2], capture_output=True,
                timeout=60)
        _rgit("config", "user.email", "t@example.invalid", cwd=_work2)
        _rgit("config", "user.name", "t", cwd=_work2)
        _io.open(os.path.join(_work2, "c.txt"), "w",
                encoding="utf-8").write("3")
        _rgit("add", "c.txt", cwd=_work2)
        _rgit("commit", "-m", "three", cwd=_work2)
        _rgit("push", "origin", "main", cwd=_work2)
        t("★★本物のgit：手元の写しが古ければ先端を返さない★★"
          "／★古い中身を「いま読者が見ているもの」と取り違えない★",
          remote_main_tip()[0] == "")
        _rgit("fetch", "origin")
        _rgit("reset", "--hard", "origin/main")
        _tip3 = _rgit("rev-parse", "HEAD").stdout.strip()
        t("　本物のgit：取り直せば、また先端を返す",
          remote_main_tip() == (_tip3, "") and _tip3 != _tip1)
    finally:
        globals()["BASE"] = _keep_base_rg
        globals()["remote_ok"] = _keep_rok_rg
        _sh.rmtree(_rg, ignore_errors=True)

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

"""local_paths.py — このパソコンの中の置き場を1か所で決める。

★何のためか★（2026-08-14・運営者の指示）
  ★このリポジトリは公開されている★。にもかかわらず、スクリプトの中に
  **利用者フォルダの絶対パスをそのまま書いていた**ため、
  運営者の本名（Windowsのログイン名）とフォルダ構成が誰にでも読める
  状態だった（25ファイル・52か所）。
  サイトのページには一度も出ていないが、公開する理由が何も無い。

★決め方★
  ①環境変数があればそれを使う（別のパソコンや別の置き場に移せる）
  ②無ければ**ログインしている人のホーム**から組み立てる
  ＝どのパソコンでも動き、名前をコードに書かなくて済む。

★今後もコードに名前を書かない★
  監査項目38が「公開されるファイルにホームのパスが直書きされていないか」を
  見張る。うっかり書き戻したら止まる。

使い方:
    import local_paths as _lp
    _lp.DOCS      # 台帳・控え・状態ファイルの置き場
    _lp.CLAUDE    # 自動タスクの道具（メール送信・ログ）の置き場
    _lp.doc("open_issues.json")     # 上の下のファイル
"""
from __future__ import annotations

import os

# ★ここだけが「このパソコンの事情」を知っている★
HOME = os.environ.get("UCHIDOKORO_HOME") or os.path.expanduser("~")

# 台帳・確定値・控え・状態・ログ
DOCS = os.environ.get("UCHIDOKORO_DOCS") or os.path.join(
    HOME, "Documents", "uchidokoro")

# 自動タスクの道具（send_notify.py / log.py / secrets / scheduled-tasks）
CLAUDE = os.environ.get("UCHIDOKORO_CLAUDE") or os.path.join(HOME, ".claude")

# よく使うもの
LOGS = os.path.join(DOCS, "logs")
OPS = os.path.join(DOCS, "ops")
SECRETS = os.path.join(CLAUDE, "secrets")
TASKS = os.path.join(CLAUDE, "scheduled-tasks")
NOTIFY = os.path.join(CLAUDE, "send_notify.py")
LOG_PY = os.path.join(CLAUDE, "log.py")


# ★リポジトリの場所（このファイルの2つ上）★
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DESIGN = os.path.join(REPO, "_design")

# ★Dropboxの控え先★（環境変数が無ければホームの下を探す）
#   ★フォルダ名に本名が入るので、コードには書かない★
def _find_dropbox() -> str:
    """バックアップして良い場所を1つに決める。★迷ったら空を返す★

    ★ここは「触ってよい範囲の上限」になる★（2026-08-14・依頼185のP0）
      取り違えると、別の同期先へ書類を書き出してしまう。
      そこで**曖昧なときは絶対に返さない**。
      呼ぶ側（backup_guard）は空なら断る。

    ★見つけ方★
      ①環境変数 `UCHIDOKORO_DROPBOX` があればそれ（★中身を検査する★）
      ②無ければホームの下を探し、**候補がちょうど1つで、
        その中の個人フォルダもちょうど1つ**のときだけ返す。
        0個・2個以上・階層が曖昧なら空。
    """
    def ok(p: str) -> str:
        """使ってよい場所か確かめる（★危ない場所は断る★）。"""
        if not p:
            return ""
        p = os.path.abspath(p)
        if not os.path.isdir(p):
            return ""
        # ★ドライブの根・ホームそのものは断る★（範囲が広すぎる）
        if os.path.dirname(p) == p or os.path.normcase(p) == \
                os.path.normcase(os.path.abspath(HOME)):
            return ""
        return p

    named = os.environ.get("UCHIDOKORO_DROPBOX")
    if named:
        return ok(named)
    # ②リポジトリの外に置いた設定（★公開されない場所★）
    #   自動探索はホームに似た名前のフォルダが複数あると決められない
    #   （実際に文字化けした「◯◯ Dropbox」が並んでいた）。
    #   置き場をここに書いておけば、名前を公開せずに正確に指せる。
    try:
        import json
        with open(os.path.join(CLAUDE, "uchidokoro_paths.json"),
                  encoding="utf-8") as f:
            got = json.load(f)
        if isinstance(got, dict) and got.get("dropbox"):
            return ok(str(got["dropbox"]))
    except Exception:                     # noqa: BLE001
        pass
    if not os.path.isdir(HOME):
        return ""
    tops = [os.path.join(HOME, n) for n in sorted(os.listdir(HOME))
            if "Dropbox" in n and os.path.isdir(os.path.join(HOME, n))]
    if len(tops) != 1:
        return ""                          # ★0個・2個以上は決めない★
    top = tops[0]
    # 隠しフォルダと共有フォルダを除く（`.dropbox.cache`／「◯◯ チーム フォルダ」）
    subs = [x for x in os.listdir(top)
            if os.path.isdir(os.path.join(top, x))
            and not x.startswith(".")
            and "チーム" not in x and "team" not in x.lower()]
    if len(subs) != 1:
        return ""                          # ★親へ広げない★（依頼185のP0）
    return ok(os.path.join(top, subs[0]))


DROPBOX = _find_dropbox()


def doc(*parts: str) -> str:
    """台帳などの置き場の下のファイル。"""
    return os.path.join(DOCS, *parts)


def claude(*parts: str) -> str:
    """自動タスクの道具の置き場の下のファイル。"""
    return os.path.join(CLAUDE, *parts)


def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅ " if cond else "❌ ") + name)

    # ★このパソコンのログイン名そのものを探す★
    #   語（「利用者フォルダ」など）ではなく実際の名前を見る。
    #   そうしないと、この試験の文言自体に引っかかる。
    _me = os.path.basename(os.path.expanduser("~"))
    t("★★このファイル自身にログイン名が入っていない★★（公開されるため）",
      bool(_me) and _me not in open(__file__, encoding="utf-8").read())
    t("　置き場はホームから組み立てる",
      DOCS.startswith(HOME) or "UCHIDOKORO_DOCS" in os.environ)
    t("　環境変数があればそちらを使う",
      (lambda keep: (os.environ.__setitem__("UCHIDOKORO_DOCS", "X:/test"),
                     __import__("importlib").reload(
                         __import__("local_paths")).DOCS == "X:/test",
                     os.environ.pop("UCHIDOKORO_DOCS"),
                     os.environ.update({"UCHIDOKORO_DOCS": keep} if keep else {}),
                     )[1])(os.environ.get("UCHIDOKORO_DOCS")))
    t("　下のファイルを組み立てられる",
      doc("open_issues.json").endswith("open_issues.json")
      and claude("send_notify.py").endswith("send_notify.py"))

    ng = sum(1 for _, o in results if not o)
    print()
    print("%d/%d 合格" % (len(results) - ng, len(results)))
    return 1 if ng else 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    print(f"HOME  : {HOME}")
    print(f"DOCS  : {DOCS}")
    print(f"CLAUDE: {CLAUDE}")

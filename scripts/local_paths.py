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
    """ホームの下から Dropbox の置き場を探す。

    ★中に本名のフォルダが1つだけあるなら、そこまで降りる★
      （会社の共有Dropboxは「◯◯ Dropbox / 個人名」の作りになっている）。
      見つからなければ空。呼ぶ側が「使えない」と分かるようにする。
    """
    root = os.environ.get("UCHIDOKORO_DROPBOX")
    if root:
        return root
    if not os.path.isdir(HOME):
        return ""
    for name in sorted(os.listdir(HOME)):
        if "Dropbox" not in name:
            continue
        top = os.path.join(HOME, name)
        if not os.path.isdir(top):
            continue
        # ★隠しフォルダと共有フォルダを除いて数える★
        #   （`.dropbox.cache` や「◯◯ チーム フォルダ」が混ざるため）
        #   ★1つに絞れないときは降りない★＝バックアップ先を取り違えない。
        subs = [x for x in os.listdir(top)
                if os.path.isdir(os.path.join(top, x))
                and not x.startswith(".")
                and "チーム" not in x and "team" not in x.lower()]
        return os.path.join(top, subs[0]) if len(subs) == 1 else top
    return ""


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

"""ci_safe.py — 公開されるCIログに、未公開の原稿を出さないための共通部品。

★なぜ要るか（2026-07-30・Codex 17巡目 (a)-6 / 18巡目 (a)-5）★
  GitHub Actions のログは公開される。検査が「どこが悪いか」を親切に出すほど、
  **まだ公開していない原稿がそのままログに載る**。
  手元では原文を出し、CIでは「種類・件数・場所・短い指紋」だけにする。

使い方:
    from ci_safe import redact, in_ci
    print(f"{path}: 未検証の数値 {redact(token)}")
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

# ★実行ごとに変わる鍵で指紋を作る★（2026-07-30・Codex 19巡目 (a)-4）
#   素のSHA-256の先頭8桁だと、`memo` `draft` のような候補の少ない値は
#   総当たりで言い当てられる（＝伏せたことにならない）。
#   1回の実行の中で「同じ文字列か」を見分けられれば十分なので、
#   プロセスごとの鍵でHMACを取る。文字数も出さない。
#   ★同じ実行の中では、別のスクリプト同士でも突き合わせたい★（Codex 20巡目 (b)-3）
#     workflow が最初に作った鍵を環境変数で渡す。無ければプロセスごとの鍵にする。
_ENV_KEY = os.environ.get("UCHIDOKORO_LOG_KEY", "")
_RUN_KEY = _ENV_KEY.encode("utf-8") if len(_ENV_KEY) >= 16 else secrets.token_bytes(32)


def in_ci() -> bool:
    """公開されるログに出力している状況か。"""
    return os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("CI") == "true"


def fingerprint(text: str) -> str:
    """原文を出さずに「同じ文字列かどうか」を突き合わせるための指紋。

    ★この実行の中でだけ意味がある★（別の実行とは突き合わせられない）。
    """
    return hmac.new(_RUN_KEY, str(text).encode("utf-8"), hashlib.sha256).hexdigest()[:12]


def redact(text, limit: int = 60) -> str:
    """原文をログに出してよいかを判断して整形する。

    手元 → « 原文 »
    CI   → （伏せ字 指紋abc…）※長さも出さない
    """
    s = str(text)
    if in_ci():
        return f"（伏せ字 指紋{fingerprint(s)}）"
    return f"« {s[:limit]} »"


# ★出してよい構造キーの一覧★（Codex 21巡目 (a)-5）
#   「ASCIIなら出す」だと `UNPUBLISHED_X9` のようなキー名がそのままログに出る。
#   公開してよい既知の骨組みだけを列挙し、それ以外は指紋にする。
STRUCTURAL_KEYS = frozenset({
    "$", "tenjo", "reset", "suru", "ichiran",
    "title", "h1", "lead", "paras", "note", "meta_description",
    "sections", "body", "items", "list", "blocks", "intro", "outro",
    "faq", "q", "a", "related", "label", "text",
})


def safe_path(path: str) -> str:
    """JSONのキー名などのパスを、原文を出さない形に整える。

    ★キー名そのものに原稿を書ける★（Codex 19巡目 (b)-1）
      `$.未公開の見出し` のようなパスをそのまま出すと原文が漏れる。
      ASCIIの短いキーはそのまま、それ以外は指紋にする。
    """
    if not in_ci():
        return path
    import re as _re
    out = []
    for part in str(path).split("."):
        # ★添字として残すのは [数字] だけ★（Codex 26巡目 (a)-2）
        #   `title[DRAFT_SECRET_X9]` のようなキー名を「添字」と誤認して素通ししていた。
        m = _re.fullmatch(r"([^\[\]]*)((?:\[\d+\])*)", part)
        if not m:
            out.append(f"<{fingerprint(part)}>")
            continue
        base, index = m.group(1), m.group(2)
        if base in STRUCTURAL_KEYS:
            out.append(base + index)
        else:
            out.append(f"<{fingerprint(base)}>{index}")
    return ".".join(out)


def format_path(segments) -> str:
    """("key", 名前) / ("index", 数) の並びを、表示できる形にする。

    ★文字列にしてから判定しない★（Codex 27巡目 (a)-6）
      `DRAFT[314159]` のようなキー名を「添字」と誤認して数字を出していた。
    """
    out = ["$"]
    for kind, value in segments:
        if kind == "index":
            out.append(f"[{value}]")
        elif kind == "note":
            out.append(f"（{value}）")
        elif not in_ci() or value in STRUCTURAL_KEYS:
            out.append(f".{value}")
        else:
            out.append(f".<{fingerprint(value)}>")
    return "".join(out)

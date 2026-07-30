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
import os


def in_ci() -> bool:
    """公開されるログに出力している状況か。"""
    return os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("CI") == "true"


def fingerprint(text: str) -> str:
    """原文を出さずに「同じ文字列かどうか」を突き合わせるための短い指紋。"""
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:8]


def redact(text, limit: int = 60) -> str:
    """原文をログに出してよいかを判断して整形する。

    手元 → « 原文 »
    CI   → （伏せ字 指紋 abcd1234・N文字）
    """
    s = str(text)
    if in_ci():
        return f"（伏せ字 指紋{fingerprint(s)}・{len(s)}文字）"
    return f"« {s[:limit]} »"

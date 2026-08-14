# -*- coding: utf-8 -*-
"""redact_secrets.py — ファイルの中の鍵らしい値を、その場で伏せる。

★なぜ要るか（2026-08-14・台帳#359）★
  設計メモ `_design/codex184_res.md` に、いま使っている GitHub のトークンが
  そのまま文字で残っていました。

  ★入り込んだ道★
    Codex は `-C <リポジトリ> -s read-only` で呼んでいるので、
    **リポジトリの中を読めます**。`.git/config` には push 用のトークンが
    URLに埋め込まれているので、Codex がそれを読んで**回答文に書いた**のです。
    ＝私が貼ったのではなく、**相手が読んで持ち出した**形。

  だから守りは2か所に要ります。
    ①こちらから渡す依頼文（うっかり貼った場合）
    ②相手から返ってきた回答文（読んで持ち出された場合）★今回はこちら★

★伏せるだけで、止めません★
  鍵が混ざっていても処理は続けます（止めると夜のタスクが動かなくなる）。
  ただし**何件伏せたかは必ずログに残します**。★値は絶対に出しません★

★これは最後の砦であって、根本ではありません★
  根本は「トークンを `.git/config` に置かないこと」です。
  置き場を変えるまでは、この伏せ字で持ち出しを止めます。

使い方:
    python scripts/redact_secrets.py --file <パス>        # その場で伏せる
    python scripts/redact_secrets.py --file <パス> --check # 数えるだけ
    python scripts/redact_secrets.py --selftest
"""
from __future__ import annotations

import argparse
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backup_guard as _bg           # noqa: E402

MASK = "（★鍵らしい値を伏せました★）"


def count(text: str) -> dict:
    """種類ごとに何件あるか（★値は返しません★）。"""
    got = {}
    for name, pat in _bg.DENY_VALUE_PATTERNS:
        n = len(pat.findall(str(text or "")))
        if n:
            got[name] = n
    return got


def mask(text: str) -> tuple:
    """伏せた本文と、種類ごとの件数を返す。"""
    out = str(text or "")
    got = {}
    for name, pat in _bg.DENY_VALUE_PATTERNS:
        out, n = pat.subn(MASK, out)
        if n:
            got[name] = n
    return out, got


def mask_file(path: str, check: bool = False) -> dict:
    """ファイルの中の鍵を、その場で伏せる（★値は出さない★）。"""
    if not os.path.isfile(path):
        return {}
    raw = io.open(path, encoding="utf-8", errors="replace").read()
    if check:
        return count(raw)
    out, got = mask(raw)
    if got:
        tmp = f"{path}.{os.getpid()}.tmp"
        io.open(tmp, "w", encoding="utf-8", newline="\n").write(out)
        os.replace(tmp, path)
    return got


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅ " if cond else "❌ ") + name)

    tok = "ghp_" + "A" * 36
    body = (".git/config の中身\n"
            "\turl = https://imotan-lab:" + tok + "@github.com/x/y.git\n"
            "ここは普通の文章です。token: str のような書き方は伏せません。\n")
    out, got = mask(body)
    t("★★GitHubのトークンを伏せる★★", tok not in out and got.get("github_token") == 1)
    t("　伏せ字に置き換わっている", MASK in out)
    t("★★普通の文章は触らない★★（token: str のような書き方）",
      "token: str のような書き方は伏せません" in out)
    t("　URLの他の部分は残る（何が起きたか読めるように）",
      "github.com/x/y.git" in out and "imotan-lab" in out)
    t("　数えるだけもできる（値は返さない）",
      count(body) == {"github_token": 1})

    t("★★他の種類も伏せる★★",
      mask("AIza" + "b" * 35)[1].get("google_api_key") == 1
      and mask("Bearer " + "c" * 30)[1].get("bearer_header") == 1
      and mask("-----BEGIN RSA PRIVATE KEY-----")[1].get(
          "private_key_block") == 1)

    import tempfile
    d = tempfile.mkdtemp()
    p = os.path.join(d, "x.md")
    io.open(p, "w", encoding="utf-8").write(body)
    got = mask_file(p)
    left = io.open(p, encoding="utf-8").read()
    t("★★ファイルをその場で書き換える★★",
      got.get("github_token") == 1 and tok not in left)
    t("　もう一度かけても何も起きない（べき等）", mask_file(p) == {})
    io.open(p, "w", encoding="utf-8").write(body)
    t("　--check は書き換えない",
      mask_file(p, check=True).get("github_token") == 1
      and tok in io.open(p, encoding="utf-8").read())
    os.unlink(p)
    os.rmdir(d)

    t("　無いファイルでも落ちない", mask_file(os.path.join(d, "nai.md")) == {})

    ng = sum(1 for _, o in results if not o)
    print()
    print("%d/%d 合格" % (len(results) - ng, len(results)))
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="ファイルの中の鍵らしい値を伏せる")
    ap.add_argument("--file")
    ap.add_argument("--check", action="store_true", help="数えるだけ（書き換えない）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.file:
        ap.print_help()
        return 0
    got = mask_file(a.file, a.check)
    if got:
        # ★値は出さない★＝種類と件数だけ
        print("★鍵らしい値: " + " / ".join(f"{k}×{v}" for k, v in got.items())
              + ("（数えただけ）" if a.check else "（伏せました）"))
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

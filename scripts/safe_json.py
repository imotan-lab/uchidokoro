"""safe_json.py — 壊れた入力で「例外で落ちる」のではなく「診断して止まる」ための共通reader。

★なぜ要るか（2026-07-30・Codex 閉鎖条件5）★
  条件5は「任意の壊れたJSON・欠落・型違いに対して、未処理例外なしで必ずDENYを返す」。
  いまは各スクリプトが素の `json.load()` を呼んでいるので、
  壊れたファイルが1つあるだけで **traceback で落ちる**。
  止まること自体は安全側だが、「何が壊れているか」が分からず、
  それまでに集めた診断もろとも失われる。

★使い方★
    from safe_json import read_json, SafeJsonError
    data = read_json(path, expect=list)     # 型まで確かめる
    # 壊れていたら SafeJsonError（メッセージは原文を含めない）

★原文をメッセージに入れない★
  公開されるCIログに未公開の原稿が出ないよう、値そのものは載せず
  「場所・型・件数」だけを伝える。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ci_safe import redact  # noqa: E402


class SafeJsonError(RuntimeError):
    """読めない／形が違うJSON（呼び出し側は必ず「公開しない」に倒す）。"""


def _no_duplicate_keys(pairs):
    """同名キーの重複を黙って上書きさせない（後勝ちで検査を欺けるため）。"""
    seen = {}
    for k, v in pairs:
        if k in seen:
            raise SafeJsonError(f"同じキーが2回あります: {redact(k)}")
        seen[k] = v
    return seen


def _control_chars(node, path: str = "$") -> list:
    """値の中の制御文字（改行・タブ以外）を探す。場所だけ返し、原文は返さない。"""
    out = []
    if isinstance(node, str):
        if any(ord(c) < 32 and c not in "\n\t" for c in node):
            out.append(path)
    elif isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and any(ord(c) < 32 and c not in "\n\t" for c in k):
                out.append(f"{path}.<キー名>")
            out.extend(_control_chars(v, f"{path}.{k if k.isascii() else '<key>'}"))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out.extend(_control_chars(v, f"{path}[{i}]"))
    return out


def read_json(path, expect=None, allow_missing: bool = False, default=None):
    """JSONを読む。読めない・形が違うなら SafeJsonError。

    path        : ファイルパス
    expect      : 期待する型（dict / list など）。None なら型は見ない
    allow_missing: ファイルが無くてもよいか（その場合 default を返す）
    """
    p = Path(path)
    if not p.is_file():
        if allow_missing:
            return default
        raise SafeJsonError(f"ファイルがありません: {p.name}")
    try:
        raw = p.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise SafeJsonError(f"{p.name}: UTF-8として読めません（{e.reason}）") from None
    except OSError as e:
        raise SafeJsonError(f"{p.name}: 読み込めません（{e.strerror}）") from None
    if raw.startswith("﻿"):
        raise SafeJsonError(f"{p.name}: 先頭にBOMがあります（UTF-8で保存し直すこと）")
    try:
        data = json.loads(raw, object_pairs_hook=_no_duplicate_keys)
    except SafeJsonError:
        raise
    except json.JSONDecodeError as e:
        raise SafeJsonError(f"{p.name}: JSONとして壊れています（{e.lineno}行{e.colno}文字）") from None
    # ★値に制御文字を入れさせない★（2026-07-30に実害のあるバグを踏んだため）
    #   見た目に出ないので、混ざると気づけないまま検査を壊せる。
    bad = _control_chars(data)
    if bad:
        raise SafeJsonError(f"{p.name}: 値に制御文字が入っています（{bad[0]}）")
    if expect is not None and not isinstance(data, expect):
        want = getattr(expect, "__name__", str(expect))
        raise SafeJsonError(f"{p.name}: {want} であるべきですが {type(data).__name__} でした")
    return data


def read_rows(path, allow_missing: bool = False):
    """「辞書の配列」を読む（機種一覧のような形）。要素の型まで確かめる。"""
    data = read_json(path, expect=list, allow_missing=allow_missing, default=[])
    for i, row in enumerate(data or []):
        if not isinstance(row, dict):
            raise SafeJsonError(
                f"{Path(path).name}: {i}番目の要素が辞書ではありません"
                f"（{type(row).__name__}）")
    return data or []


# ---------------------------------------------------------------- selftest
def _broken_inputs():
    """壊し方を機械的に並べる（★変異テストの種★）。"""
    return {
        "空ファイル": "",
        "途中で切れている": '[{"slug": "a"',
        "末尾のカンマ": '[{"slug": "a"},]',
        "シングルクォート": "[{'slug': 'a'}]",
        "BOM付き": "﻿[]",
        "同名キーの重複": '{"slug": "a", "slug": "b"}',
        "数値だけ": "42",
        "文字列だけ": '"x"',
        "null": "null",
        "真偽値": "true",
        "配列の中に文字列": '["x"]',
        "配列の中にnull": "[null]",
        "深い入れ子だけ": "[[[[[]]]]]",
        "制御文字入り": '[{"slug": "a\\u0008b"}]',
    }


def selftest() -> int:
    import tempfile
    ok, total = 0, 0
    tmp = Path(tempfile.mkdtemp(prefix="safejson-"))

    def check(name, fn, want_error=True):
        nonlocal ok, total
        total += 1
        try:
            fn()
            got_error = False
        except SafeJsonError:
            got_error = True
        except Exception as e:                       # ★ここに来たら条件5の違反★
            print(f"❌ {name}: SafeJsonError 以外の例外 {type(e).__name__}: {e}")
            return
        if got_error == want_error:
            ok += 1
            print(f"✅ {name}")
        else:
            print(f"❌ {name}: {'止まらなかった' if want_error else '止まってしまった'}")

    for name, body in _broken_inputs().items():
        f = tmp / "x.json"
        f.write_text(body, encoding="utf-8")
        # 「辞書の配列」として読む入口は、上のどれでも必ず診断で止まる
        check(f"壊れた入力で止まる: {name}", lambda f=f: read_rows(f))

    good = tmp / "good.json"
    good.write_text('[{"slug": "a"}]', encoding="utf-8")
    check("正しい入力は通る", lambda: read_rows(good), want_error=False)
    obj = tmp / "obj.json"
    obj.write_text("{}", encoding="utf-8")
    check("型が違えば止まる（listを期待してdict）", lambda: read_json(obj, expect=list))
    check("無いファイルは止まる", lambda: read_json(tmp / "none.json"))
    check("無くてよい指定なら通る",
          lambda: read_json(tmp / "none.json", allow_missing=True, default={}),
          want_error=False)

    print(f"\n{ok}/{total} 合格")
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(selftest() if "--selftest" in sys.argv[1:] else selftest())

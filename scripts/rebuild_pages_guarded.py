# -*- coding: utf-8 -*-
"""公開中の機種ページを、許した差分だけに限って作り直す。

★なぜ要るか（2026-08-09・依頼128）★
  裏取りゲート（claim-gate）が無効な間は `build_pages_artifact.py` が使えない。
  そのため公開中のページを直すには `build_machine_pages.render_page` を
  直接呼ぶしかないが、**素で呼ぶと本文以外まで作り直してしまう**。

  実際に起きたこと（2026-08-09）:
    冒頭の1文だけを直したかったのに、`pochipochi_public` の指定を誤ったら
    題（title）・説明文（description）・OGPまで変わった出力になった。
    その場で差分を突き合わせて気づいたが、**その検査はコミットに残らず**、
    次に同じことをする人は同じ手作りをやり直すことになる。

★この道具の約束★
  ①作り直した結果と今のファイルを行単位で突き合わせる
  ②変わった行が「許した文字列」を含むものだけであることを確かめる
  ③1行でも想定外があれば**1枚も書かない**（全部か、無しか）
  ④既定は下見（`--apply` を付けたときだけ書く）

使い方:
  # 許した文字列を1行ずつ書いたファイルを用意する（変更前と変更後の両方）
  python scripts/rebuild_pages_guarded.py --slug sf6 --slug tonsuki \\
      --expect-file C:/Users/imao_/Documents/uchidokoro/ops/expected.txt
  python scripts/rebuild_pages_guarded.py --slug sf6 --expect-file ... --apply

  # 何が変わるのか分からないときは、まず下見だけ流して差分を読む
  python scripts/rebuild_pages_guarded.py --slug sf6 --show-diff
"""
from __future__ import annotations

import argparse
import difflib
import io
import json
import os
import sys
from pathlib import Path

BASE = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(BASE / "scripts"))

import build_machine_pages as _b        # noqa: E402


def load_machines() -> dict:
    ms = json.loads((BASE / "assets/data/machines.json").read_text(encoding="utf-8"))
    if isinstance(ms, dict):
        ms = ms["machines"]
    return {m["slug"]: m for m in ms}


def changed_lines(cur: str, new: str) -> list:
    out = []
    for d in difflib.unified_diff(cur.splitlines(), new.splitlines(),
                                  lineterm="", n=0):
        if d.startswith(("+++", "---")):
            continue
        if d.startswith(("+", "-")):
            out.append(d)
    return out


def run(slugs: list, expects: list, pochipochi_public: bool,
        apply: bool, show_diff: bool) -> int:
    template = _b.prepare_template(
        (BASE / "machine.html").read_text(encoding="utf-8"))
    reasons = _b.extract_pochipochi_reasons(template)
    by_slug = load_machines()

    planned = []
    bad = 0
    for slug in slugs:
        m = by_slug.get(slug)
        if not m:
            print("★machines.json にありません: %s★" % slug)
            return 1
        dp = BASE / ("assets/data/machine-details/%s.json" % slug)
        detail = json.loads(dp.read_text(encoding="utf-8")) if dp.is_file() else None
        out = _b.render_page(template, m, detail, reasons,
                             pochipochi_public=pochipochi_public)
        path = BASE / ("machines/%s/index.html" % slug)
        if not path.is_file():
            print("★ページがありません: %s★" % path)
            return 1
        cur = path.read_text(encoding="utf-8")
        diff = changed_lines(cur, out)
        unexpected = [d for d in diff
                      if not any(e and e in d for e in expects)]
        print("■ %-20s 変わる行 %d / 想定外 %d" % (slug, len(diff), len(unexpected)))
        if show_diff:
            for d in diff[:20]:
                print("    " + d[:160])
        else:
            for d in unexpected[:6]:
                print("    ★想定外★ " + d[:150])
        if unexpected:
            bad += 1
            continue
        if diff:
            planned.append((path, out))

    if bad:
        print()
        print("★想定外の差分がある機種が %d 件あるので、1枚も書きません★" % bad)
        print("  許す文字列を --expect-file に足すか、"
              "--pochipochi-public の指定を見直してください")
        return 1
    if not planned:
        print()
        print("書き換えるページはありません（差分なし）")
        return 0
    if not apply:
        print()
        print("下見です。書き込むには --apply を付けてください（%d枚）" % len(planned))
        return 0
    for path, out in planned:
        path.write_text(out, encoding="utf-8", newline="\n")
        print("書き換えました: %s" % path.relative_to(BASE))
    print()
    print("%d枚を書き換えました。★service-worker のキャッシュ版数を上げること★"
          % len(planned))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="許した差分だけに限って機種ページを作り直す")
    ap.add_argument("--slug", action="append", default=[],
                    help="作り直す機種（何度でも指定できる）")
    ap.add_argument("--expect-file", default="",
                    help="変わってよい行に含まれる文字列を1行ずつ書いたファイル")
    ap.add_argument("--expect", action="append", default=[],
                    help="変わってよい行に含まれる文字列（直接指定）")
    ap.add_argument("--pochipochi-public", default="true",
                    choices=["true", "false"],
                    help="公開中のページと同じ設定にする（既定 true）")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--show-diff", action="store_true",
                    help="差分を出すだけ（何を許せばよいか調べる）")
    a = ap.parse_args()

    if not a.slug:
        print("--slug が要ります")
        return 2
    expects = list(a.expect)
    if a.expect_file:
        p = Path(a.expect_file)
        if not p.is_file():
            print("★--expect-file がありません: %s★" % a.expect_file)
            return 2
        expects += [x.strip() for x in
                    io.open(p, encoding="utf-8").read().splitlines() if x.strip()]
    if not expects and not a.show_diff:
        print("★許す文字列がありません★ --expect / --expect-file を指定するか、"
              "まず --show-diff で差分を見てください")
        return 2
    return run(a.slug, expects, a.pochipochi_public == "true",
               a.apply, a.show_diff)


if __name__ == "__main__":
    raise SystemExit(main())

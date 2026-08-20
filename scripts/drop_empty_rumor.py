#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""★中身の無い噂の箱を外す★（2026-08-21・台帳#334）

## なぜ

運営者の決定（2026-08-12・CLAUDE.md）:
  「rumor（噂・未確定情報ボックス）は★中身ができてから出す★。
    噂や小ネタが無い機種のほうが多く、空の箱は『あるのに載せていない』と読める」

実際には56機種で、箱の中に「現時点で目立った噂・未確定情報はありません」と
書いたまま読者に見せていた（`recheck.py --check rumor_not_declared_empty`）。

## ★外してよいのは、箱の中身が全部うちの定型文のときだけ★

このスクリプトが触るのは、噂の箱の本文が

  ① 「噂・未確定情報はありません」等の**無いという宣言**
  ② 「※以下は公式未発表・未確認の情報です」等の**前置き**

だけで出来ている箱に限る。★それ以外の文が1行でもあれば触らない★
（「解析待ちです」のような文が中身にあたるかは**意味の判断**なので2AIへ回す）。

★新しい文章は書かない★＝やるのは箱ごと外すことだけ。
★1機種に噂の箱が複数あるときは触らない★（順序の意図が読めないため）。

## 使い方

    python scripts/drop_empty_rumor.py            # 下見（既定・書き換えない）
    python scripts/drop_empty_rumor.py --apply    # 実行
    python scripts/drop_empty_rumor.py --selftest
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DETAILS = os.path.join(BASE, "assets", "data", "machine-details")

# ★「無い」と宣言している文★（うちの生成物の定型文）
NONE_PHRASES = (
    "噂・未確定情報はありません",
    "噂はありません",
    "未確定情報はありません",
)


def line_kind(line: str) -> str:
    """その行が「無いという宣言」「前置き」「それ以外」のどれか。"""
    if not isinstance(line, str):
        return "OTHER"
    if any(p in line for p in NONE_PHRASES):
        return "DECLARE_EMPTY"
    st = line.strip()
    if st.startswith("※") and ("公式未" in st or "未確認" in st):
        return "PREFACE"
    return "OTHER"


def droppable(section: dict) -> bool:
    """★この噂の箱は外してよいか★

    条件＝①中身が全部「宣言」か「前置き」 ②「無い」という宣言が1行以上ある
    （②が無い箱＝単に前置きだけの箱は、意図が読めないので触らない）
    """
    if not isinstance(section, dict) or section.get("type") != "rumor":
        return False
    body = section.get("body")
    if not isinstance(body, list):
        return False
    lines = [b for b in body if isinstance(b, str) and b.strip()]
    if not lines:
        return False                       # 空配列の箱はここでは扱わない
    if len(lines) != len([b for b in body if isinstance(b, str)]):
        pass                               # 空行は無視してよい
    kinds = [line_kind(b) for b in lines]
    if "OTHER" in kinds:
        return False
    return "DECLARE_EMPTY" in kinds


def plan():
    """外す対象を挙げる（読むだけ）。"""
    out = []
    for path in sorted(glob.glob(os.path.join(DETAILS, "*.json"))):
        with io.open(path, encoding="utf-8") as f:
            detail = json.load(f)
        sections = detail.get("sections")
        if not isinstance(sections, list):
            continue
        rumor_idx = [i for i, s in enumerate(sections)
                     if isinstance(s, dict) and s.get("type") == "rumor"]
        if len(rumor_idx) != 1:
            continue                       # ★複数あるときは触らない★
        i = rumor_idx[0]
        if droppable(sections[i]):
            out.append((path, detail.get("slug"), i,
                        [b for b in sections[i].get("body") or []]))
    return out


def apply_plan(items) -> int:
    n = 0
    for path, _slug, idx, _body in items:
        with io.open(path, encoding="utf-8") as f:
            detail = json.load(f)
        sections = detail["sections"]
        # ★もう一度確かめてから消す★（下見と実行の間に変わっている可能性）
        if not (isinstance(sections[idx], dict) and droppable(sections[idx])):
            print(f"  skip（下見のときと違います）: {path}")
            continue
        del sections[idx]
        with io.open(path, "w", encoding="utf-8") as f:
            json.dump(detail, f, ensure_ascii=False, indent=2)
            f.write("\n")
        n += 1
    return n


def _selftest() -> int:
    ok = total = 0

    def t(name, cond):
        nonlocal ok, total
        total += 1
        ok += 1 if cond else 0
        print(("OK   " if cond else "NG   ") + name)

    pre = "※以下は公式未発表・未確認の情報です。実際の挙動と異なる可能性があります。"
    none = "現時点で目立った噂・未確定情報はありません。新しい情報が入り次第更新します。"
    real = "スルー回数の振り分けの詳細な確率は解析待ちです。"

    t("前置きを見分ける", line_kind(pre) == "PREFACE")
    t("「無い」宣言を見分ける", line_kind(none) == "DECLARE_EMPTY")
    t("★それ以外は OTHER★", line_kind(real) == "OTHER")
    t("★文字列でなければ OTHER★", line_kind({"x": 1}) == "OTHER")

    t("前置き＋宣言だけなら外す",
      droppable({"type": "rumor", "body": [pre, none]}))
    t("宣言だけでも外す", droppable({"type": "rumor", "body": [none]}))
    t("★ほかの文が1行でもあれば外さない★",
      not droppable({"type": "rumor", "body": [pre, none, real]}))
    t("★前置きだけの箱は外さない★", droppable({"type": "rumor", "body": [pre]}) is False)
    t("★中身だけの箱は外さない★", not droppable({"type": "rumor", "body": [real]}))
    t("★噂の箱でなければ外さない★",
      not droppable({"type": "settei", "body": [none]}))
    t("★本文が配列でなければ外さない★",
      not droppable({"type": "rumor", "body": none}))
    t("★本文が空なら外さない★", not droppable({"type": "rumor", "body": []}))

    print()
    print(f"{ok}/{total} 合格")
    return 0 if ok == total else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="中身の無い噂の箱を外す")
    ap.add_argument("--apply", action="store_true", help="実際に書き換える")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return _selftest()

    items = plan()
    print(f"外せる噂の箱: {len(items)} 機種")
    for _path, slug, _i, body in items:
        first = (body[0] if body else "")[:52]
        print(f"  {slug}: {len(body)}行 / {first}…")
    if not a.apply:
        print()
        print("★下見です（何も書き換えていません）★ 実行するなら --apply")
        return 0
    n = apply_plan(items)
    print()
    print(f"★{n} 機種の噂の箱を外しました★")
    print("  このあと: build_machine_pages.py → crosscheck_gates.py → audit_site.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())

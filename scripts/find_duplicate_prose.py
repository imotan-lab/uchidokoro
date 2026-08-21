#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""★同じ判断を2度読ませている箇所を「候補として」挙げる★（台帳#121・#141）

★この道具がやること★
  記事の1つのセクションの中に、**言い方を変えただけの同じ話**が
  並んでいないかを探して、**候補として並べる**だけ。

★この道具がやらないこと★
  ・どちらを消すか決めない
  ・自動で書き換えない
  ＝★同じ意味かどうかは意味の判断★なので、機械には決めさせない
    （CLAUDE.md の鉄則。正規表現や例外リストを足したくなったら手を止める合図）。
  候補を読んで決めるのは2AI（Claudeとcodex）。

★なぜ道具として残すか★
  台帳#121は「検出スクリプトは未コミット（scratchpadに scan_dup.py）」と
  書かれていた。★次に同じことを調べる人が同じ数を出せない★＝
  30機種という数の根拠が確かめられない。だからリポジトリに置く。

使い方:
  python scripts/find_duplicate_prose.py                 （既定のセクション全部）
  python scripts/find_duplicate_prose.py --section ヤメ時の判断
  python scripts/find_duplicate_prose.py --slug monkeyv --show
  python scripts/find_duplicate_prose.py --min 0.50
  python scripts/find_duplicate_prose.py --json
  python scripts/find_duplicate_prose.py --selftest
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import safe_json as _sj      # noqa: E402

DETAILS = os.path.join(BASE, "assets", "data", "machine-details")

# ★見るセクション★（台帳#121＝ヤメ時／#141＝立ち回り）
DEFAULT_SECTIONS = ("ヤメ時の判断", "立ち回りのコツ", "狙い目の根拠")

# ★比べる前に落とすもの★
#   強調の記号と、文の区切り。★言い回しの違いだけを残したい★ので、
#   飾りは落としてから比べる。
#   ★ここに「例外」を足し始めたら手を止める★＝それは意味の判断。
_DECOR = re.compile(r"[*＊「」『』（）()、。・：:；;！!？?　 ]")


def _plain(s: str) -> str:
    return _DECOR.sub("", str(s or ""))


def _sentences(body) -> list:
    """セクションの本文を「文」に割る（行またぎもばらす）。"""
    out = []
    for line in (body or []):
        if not isinstance(line, str):
            continue
        for piece in re.split(r"(?<=[。！？])", line):
            piece = piece.strip()
            if len(_plain(piece)) >= 8:      # 短すぎるものは比べない
                out.append(piece)
    return out


def similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _plain(a), _plain(b)).ratio()


def pairs_in(body, min_ratio: float = 0.62) -> list:
    """似ている文の組を返す（★決めない・並べるだけ★）。"""
    sents = _sentences(body)
    got = []
    for i in range(len(sents)):
        for j in range(i + 1, len(sents)):
            r = similar(sents[i], sents[j])
            if r >= min_ratio:
                got.append({"ratio": round(r, 3), "a": sents[i], "b": sents[j]})
    got.sort(key=lambda x: -x["ratio"])
    return got


def scan(sections=DEFAULT_SECTIONS, min_ratio: float = 0.62, slug=None) -> list:
    out = []
    names = sorted(os.listdir(DETAILS)) if os.path.isdir(DETAILS) else []
    for fn in names:
        if not fn.endswith(".json"):
            continue
        sl = fn[:-5]
        if slug and sl != slug:
            continue
        try:
            d = _sj.read_json(os.path.join(DETAILS, fn), expect=dict)
        except Exception as e:
            out.append({"slug": sl, "error": str(e)[:120]})
            continue
        for sec in (d.get("sections") or []):
            title = str(sec.get("title") or "")
            if sections and title not in sections:
                continue
            found = pairs_in(sec.get("body"), min_ratio)
            if found:
                out.append({"slug": sl, "section": title, "pairs": found,
                            "n_body": len(sec.get("body") or [])})
    return out


def _selftest() -> int:
    ng = []

    def t(name, cond):
        print(("✅ " if cond else "❌ ") + name)
        if not cond:
            ng.append(name)

    t("★言い方を変えただけの繰り返しを候補に挙げる★",
      len(pairs_in(["ターミナルゾーン終了後はヤメでOK。",
                    "ターミナルゾーン終了後にヤメてOKです。"])) == 1)
    t("　1つの行の中にある繰り返しも見つける（行でしか切らないと見逃す）",
      len(pairs_in(["前兆が無ければ即ヤメでOKです。"
                    "前兆が無ければ即ヤメで問題ありません。"],
                   min_ratio=0.55)) == 1)
    t("★違う話は候補に挙げない★",
      pairs_in(["天井は999Gです。", "リセット時は555Gから狙えます。"]) == [])
    t("　短すぎる文は比べない（『ヤメです。』どうしが必ず当たってしまう）",
      pairs_in(["ヤメです。", "ヤメです。"]) == [])
    t("　飾り（強調・かっこ）の違いだけでは別物にしない",
      similar("**天井**は999Gです", "天井は999Gです") == 1.0)
    # ★言い換えが強い型は、閾値を下げても機械では届かない★（台帳#141の指摘）
    #   0.35〜0.52 に留まるので、0.62 では出ない。
    #   ★だからこの道具は「候補を出すだけ」で終わる★＝残りは2AIが読む。
    _para = ["通常時は前兆を確認してからヤメましょう。",
             "前兆のチェックを済ませてから離席するのが基本です。"]
    t("★言い換えが強い型は機械では届かない（＝2AIが読むしかない）★",
      pairs_in(_para, min_ratio=0.62) == []
      and similar(_para[0], _para[1]) < 0.62)
    # ★★書き換える口が無いことを、実際に引数を渡して確かめる★★
    #   （ソースの字面で見ると、この試験自身の文字列に当たって誤判定する）
    import subprocess as _sp
    _r = _sp.run([sys.executable, __file__, "--apply"],
                 capture_output=True, cwd=BASE)
    t("★★この道具は書き換えない★★（決めるのは2AI）", _r.returncode != 0)

    print()
    print(f"{7 - len(ng)}/7 " + ("合格" if not ng else "不合格"))
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--section", action="append",
                    help="見るセクション（複数可・既定は3つ）")
    ap.add_argument("--slug", help="1機種だけ")
    ap.add_argument("--min", type=float, default=0.62, help="似ている度の下限")
    ap.add_argument("--show", action="store_true", help="文そのものを出す")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()

    secs = tuple(a.section) if a.section else DEFAULT_SECTIONS
    got = scan(secs, a.min, a.slug)
    if a.json:
        print(json.dumps(got, ensure_ascii=False, indent=1))
        return 0

    by_slug = {}
    for g in got:
        by_slug.setdefault(g["slug"], []).append(g)
    print(f"★候補★ {len(by_slug)} 機種 / {sum(len(g.get('pairs') or []) for g in got)} 組"
          f"（似ている度 {a.min} 以上・セクション: {' / '.join(secs)}）")
    print("★どちらを消すかは決めていません★（読んで2AIで決めてください）")
    print()
    for sl in sorted(by_slug):
        for g in by_slug[sl]:
            if g.get("error"):
                print(f"  {sl}: 読めません（{g['error']}）")
                continue
            print(f"  {sl} / {g['section']}（{len(g['pairs'])}組・本文{g['n_body']}行）")
            if a.show:
                for pr in g["pairs"]:
                    print(f"      {pr['ratio']}  A: {pr['a'][:70]}")
                    print(f"             B: {pr['b'][:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

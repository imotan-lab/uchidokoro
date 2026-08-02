"""html_tables.py — HTMLの表を「区画」として読む共通部品。

★なぜ要るか（2026-07-31・Codex指摘4を自分で再現した）★
  本文を平らな行の列にしてから読むと、**表の境目が消える**。
  そのため「見出しをさかのぼって名前を探す」やり方では、
  別の表の値に、上にある別の見出しの名前が付いてしまう。

    CZ「Aチャレンジ」
    ボーナス          ← ここから別の表なのに、行の列では分からない
    継続G数 / 7G
    期待度 / 約50%
                     → 7G・約50% が「Aチャレンジ」の値として出る（実際に再現）

  そこで **<table> ごとに切り出し、その表の直前の見出しだけを名前とする**。
  値は必ず同じ表の中から採るので、別の表の値が混ざらない。

★この部品は「読む」だけ★
  何を採用してよいかは呼び出し側が決める。ここでは判断しない。
"""

from __future__ import annotations

import html as _html
import re
import unicodedata

_S = "[ \t\r\n]*"          # バックスラッシュを直接書かない（制御文字に化ける事故が続いたため）


def _text(fragment: str) -> str:
    """タグを外して素の文字にする。"""
    t = re.sub("(?is)<[^>]+>", " ", str(fragment or ""))
    t = _html.unescape(t)
    return unicodedata.normalize("NFKC", " ".join(t.split()))


def tables(html: str) -> list:
    """表を1つずつ、直前の見出しと一緒に返す。

    返すもの: [{"title": 直前の見出し, "pairs": [(左, 右), ...], "cells": [...]}]
    """
    out = []
    prev_end = 0
    for m in re.finditer("(?is)<table[^>]*>(.*?)</table" + _S + ">", html or ""):
        body = m.group(1)
        pairs, cells, rows = [], [], []
        # ★rowspan/colspan のある表は「ずれあり」と印を付ける★
        #   （2026-08-03・Codex60回目。物理セルしか持たないので、
        #     多段見出しの表は列見出しと値が必ずずれる。展開はせず、
        #     読む側がこの印の表を不採用にする）
        has_span = bool(re.search(
            r"(?is)<t[hd][^>]*\b(?:row|col)span\s*=\s*[\"']?\s*(?!1[\"'\s>])",
            body))
        for row in re.finditer("(?is)<tr[^>]*>(.*?)</tr" + _S + ">", body):
            got = [_text(c) for c in re.findall(
                "(?is)<t[hd][^>]*>(.*?)</t[hd]" + _S + ">", row.group(1))]
            cells.extend(got)
            # ★全列を rows に残す★（2026-08-03・Codex59回目）
            #   pairs は(左,右)の2列しか持たず、P-WORLDの「CZ/AT確率」のような
            #   3列の表（設定|CZ合成|AT初当り確率）の3列目が読めなかった。
            rows.append(got)
            if len(got) >= 2:
                pairs.append((got[0], got[1]))
        # ★見出しは「前の表とこの表の間」にあるものだけ★
        #   （2026-08-03・Codex60回目。文書全体で最後の見出しを使うと、
        #     見出しを持たない次の表が前の表の見出しを引き継いでしまい、
        #     ボーナス表を上位AT表として読めた）
        between = html[prev_end:m.start()]
        heads = re.findall("(?is)<h[1-6][^>]*>(.*?)</h[1-6]" + _S + ">", between)
        caps = re.findall("(?is)<caption[^>]*>(.*?)</caption" + _S + ">", body)
        title = _text(caps[-1]) if caps else (_text(heads[-1]) if heads else "")
        prev_end = m.end()
        out.append({"title": title, "pairs": pairs, "cells": cells,
                    "rows": rows, "has_span": has_span})
    return out


def value_of(pairs: list, labels) -> str:
    """表の中から、指定の見出しの右にある値を取る。★同じ表の中だけ★"""
    for left, right in pairs:
        if left in labels:
            return right
    return ""


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []
    nl = chr(10)

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    H = ('<div><h3><span>CZ「Aチャレンジ」</span></h3> <table><tbody>'
         '<tr><th>タイプ</th><td>ST</td></tr>'
         '<tr><th>継続G数</th><td>4G＋α</td></tr>'
         '<tr><th>期待度</th><td>約40%</td></tr></tbody></table></div>'
         '<div><h3>CZ「Bチャレンジ」</h3> <table><tbody>'
         '<tr><th>継続G数</th><td>7G</td></tr>'
         '<tr><th>期待度</th><td>約50%</td></tr></tbody></table></div>')
    tb = tables(H)
    t("★表を1つずつ切り出す★", len(tb) == 2)
    t("★★表ごとに直前の見出しを持つ★★（別の表の名前が混ざらない）",
      tb[0]["title"] == "CZ「Aチャレンジ」" and tb[1]["title"] == "CZ「Bチャレンジ」")
    t("★★値は同じ表の中からしか採らない★★",
      value_of(tb[1]["pairs"], ("継続G数",)) == "7G"
      and value_of(tb[0]["pairs"], ("継続G数",)) == "4G+α")
    t("　全角の＋αも半角にそろえる",
      value_of(tb[0]["pairs"], ("継続G数",)) == "4G+α")
    t("　無い見出しは空を返す", value_of(tb[0]["pairs"], ("純増",)) == "")
    t("　表が無ければ空", tables("<p>表はありません</p>") == [])
    t("　タグの中の文字は本文にしない",
      tables('<h3>見出し</h3><table><tr><th>a<span>b</span></th>'
             '<td>c</td></tr></table>')[0]["pairs"] == [("a b", "c")])
    t("★caption があれば見出しより優先する★",
      tables('<h3>ちがう見出し</h3><table><caption>本当の名前</caption>'
             '<tr><th>a</th><td>b</td></tr></table>')[0]["title"] == "本当の名前")
    t("　実体参照をほどく",
      tables("<h3>x</h3><table><tr><th>a</th><td>1&nbsp;G</td></tr></table>"
             )[0]["pairs"] == [("a", "1 G")])

    ng = [n for n, ok in results if not ok]
    print(f"{nl}{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


if __name__ == "__main__":
    import sys
    raise SystemExit(selftest() if "--selftest" in sys.argv else selftest())

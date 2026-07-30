"""numerals.py — 「これは数値か」の判定を1か所にまとめる。

★なぜ要るか（2026-07-30）★
  同じ判定が gates.py と audit_public.py に**別々の方式で**書かれていた。

    gates.py       … Unicode の数値属性（unicodedata.numeric）で見る
    audit_public.py… 文字を列挙した正規表現で見る

  Python 3.12（Unicode 15.0）では両者の結果が一致していたが、
  **Python 3.13（Unicode 15.1）で「京」に数値属性が付いた**ため、
  「東京喰種」を gates は数値ありと判定し、audit_public は数値なしと判定した。
  結果、CIでだけ「数値の無い面が表示要件にある」で止まり続けた。
  手元（3.12）では再現しないので原因が分からなかった。

★教訓★ 同じ意味の判定を2か所に書くと、いつか必ずずれる。
  ホスト名の見せ方でも同じ事故を起こしたので、判定は必ず1か所にする。

★どう決めるか★
  1. Unicode の **数の分類（Nd/Nl/No）** に入る文字は数値
     （半角/全角の 0-9、アラビア数字 ٩、丸数字 ①、ローマ数字 Ⅰ など）
  2. 日本語で数として書かれる漢字は**明示した一覧だけ**を数値とする
  ★文字の「数値属性」は使わない★
     「京」「兆」のように、数の意味を持つが**普通の言葉としても使う**漢字が
     Unicode の版が上がるたびに数値属性を得る。機種名の「東京」を
     数値と見なしてしまうし、Pythonの版で結果が変わる。
"""

from __future__ import annotations

import unicodedata

# 日本語で数として書かれる漢字（★ここに無い漢字は数値と見なさない★）
#   「京」「兆」を入れないのは、機種名の「東京」を数値にしないため。
#   数として書かれた「京」を拾えなくなるが、記事でその桁は使わない。
KANJI_NUMERALS = frozenset("一二三四五六七八九十百千万零壱弐参拾")

# 数の分類（Unicodeの版が上がっても変わらない）
_NUMBER_CATEGORIES = ("Nd", "Nl", "No")


def is_numeral_char(ch: str) -> bool:
    """1文字が数値かどうか。"""
    return unicodedata.category(ch) in _NUMBER_CATEGORIES or ch in KANJI_NUMERALS


def has_numeral(text) -> bool:
    """文字列に数値が含まれるか。★gates も audit_public もこれを使う★"""
    if not isinstance(text, str):
        return False
    return any(is_numeral_char(c) for c in text)


def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    t("★★機種名の「東京」を数値と見なさない★★（CIでだけ落ちた原因）",
      not has_numeral("東京喰種") and not has_numeral("東京リベンジャーズ"))
    t("　「兆」「京」も普通の言葉として使うので数値にしない",
      not has_numeral("兆し") and not has_numeral("京都"))
    t("★半角・全角の数字は数値★", has_numeral("1200G") and has_numeral("１２００Ｇ"))
    t("★他の言語の数字も数値★", has_numeral("٩٩٩"))
    t("★丸数字・ローマ数字も数値★", has_numeral("①") and has_numeral("Ⅲ"))
    t("★日本語の漢数字は数値★",
      all(has_numeral(x) for x in ("千二百", "五十", "壱万")))
    t("　数値を含まない文は数値なし",
      not has_numeral("狙い目の目安です") and not has_numeral(""))
    t("　文字列でなければ数値なし", not has_numeral(None) and not has_numeral(1200))
    t("★★Pythonの版が変わっても結果が変わらない★★"
      "（文字の数値属性ではなく分類で見ている）",
      not any(unicodedata.category(c) in _NUMBER_CATEGORIES for c in "京兆東"))

    ng = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    raise SystemExit(selftest() if a.selftest else (ap.print_help() or 0))

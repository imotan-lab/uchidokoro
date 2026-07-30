"""lineage_check.py — 出典どうしが「同じ内容の転載」でないか調べる。

★なぜ要るか（2026-07-31・実際に見つけた）★
  やんちゃプレスは、ちょんぼりすたと**本文が17行そのまま同じ**だった。
  別ドメインなので、登録簿に書かなければ「独立2出典」として数えてしまう。
  それでは独立2出典の意味が無くなる。

  同じ日に、P-WORLD がページ末尾で解析情報の出所を「HAZUSE調べ」と
  明記していることも見つかった。**別ドメインでも中身が同じ**ことがある。

★どう調べるか★
  2つのページの本文から、意味のある長さの行だけを取り出して突き合わせる。
  一致率が高ければ「転載の疑い」として報告する。
  ★機械が決めるのは「疑い」まで★。同じ系列として登録するかは人が決める
  （登録簿を書き換えるのは対話セッションの仕事）。

使い方:
    python scripts/lineage_check.py --url <URL1> --url <URL2>
    python scripts/lineage_check.py --selftest
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import new_machine_watch as _w        # noqa: E402
import spec_lookup as _sl             # noqa: E402

# 突き合わせに使う行の長さ（短い行は決まり文句が多く、偶然一致する）
MIN_LEN, MAX_LEN = 20, 200
# この割合を超えたら転載の疑い
SUSPECT_RATIO = 0.20


def core_lines(text: str) -> list:
    return [x.strip() for x in text.splitlines()
            if MIN_LEN < len(x.strip()) < MAX_LEN]


# ★一致率だけで決めない★（Codex指摘・実際に再現した）
#   比較対象が1行しかないと、その1行が一致しただけで100%になる。
#   一致した行数の下限も置く。
MIN_SAME_LINES = 3
MIN_COMPARABLE = 5      # 比べる行がこれ未満なら判定しない


def similarity(text_a: str, text_b: str) -> dict:
    a, b = core_lines(text_a), core_lines(text_b)
    out = {"ratio": 0.0, "same": 0, "a_lines": len(a), "b_lines": len(b),
           "examples": [], "judgeable": False}
    if not a or not b:
        return out
    same = set(a) & set(b)
    out["same"] = len(same)
    out["examples"] = sorted(same)[:3]
    out["ratio"] = len(same) / min(len(a), len(b))
    # ★判定してよいのは、比べる行が十分にあるときだけ★
    out["judgeable"] = min(len(a), len(b)) >= MIN_COMPARABLE
    return out


def check(urls: list) -> dict:
    """URLどうしを総当たりで比べ、転載の疑いを返す。"""
    texts, errs = {}, []
    for u in urls:
        try:
            texts[u] = _w._visible_text(_w._get(u))
        except Exception as e:
            errs.append(f"{u}: 取得できません（{e}）")
    # ★低い一致率は「独立である証拠」にはならない★（Codex指摘）
    #   書き直した転載や、同じプレス資料を各社が要約した場合は検出できない。
    #   ここで分かるのは「そのまま写した疑いがあるか」だけ。
    out = {"suspects": [], "checked": [], "problems": errs,
           "_note": "一致率が低くても独立の証明にはなりません（書き直した転載は検出できません）"}
    for x, y in itertools.combinations(sorted(texts), 2):
        s = similarity(texts[x], texts[y])
        hx = x.split("/")[2].lower().removeprefix("www.")
        hy = y.split("/")[2].lower().removeprefix("www.")
        already = _sl._lineage(hx) == _sl._lineage(hy)
        rec = {"a": hx, "b": hy, "ratio": round(s["ratio"], 3),
               "same_lines": s["same"], "already_same_lineage": already,
               "examples": s["examples"]}
        out["checked"].append(rec)
        rec["judgeable"] = s["judgeable"]
        if (s["judgeable"] and s["ratio"] >= SUSPECT_RATIO
                and s["same"] >= MIN_SAME_LINES and not already):
            # ★登録簿にまだ書かれていない転載の疑い★
            out["suspects"].append(rec)
    return out


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []
    nl = chr(10)

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    A = nl.join([f"これはそこそこ長さのある本文の行です。番号は{i}です。" for i in range(10)])
    B = nl.join([f"これはそこそこ長さのある本文の行です。番号は{i}です。" for i in range(5)]
                + [f"こちらは別の記事にしかない独自の文章です。番号は{i}です。" for i in range(5)])
    C = nl.join([f"まったく違う内容の記事です。話題も違います。番号は{i}です。" for i in range(10)])

    t("★★そのまま転載していれば高い一致率になる★★",
      similarity(A, A)["ratio"] == 1.0)
    t("★半分が同じなら 0.5 前後になる★",
      0.4 <= similarity(A, B)["ratio"] <= 0.6)
    t("　内容が違えば一致しない", similarity(A, C)["ratio"] == 0.0)
    t("　短い行は突き合わせに使わない（決まり文句で偶然一致するため）",
      similarity("はい" + nl + "いいえ", "はい" + nl + "いいえ")["ratio"] == 0.0)
    t("　片方が空なら 0", similarity(A, "")["ratio"] == 0.0)

    t("★★比べる行が少なすぎるときは判定しない★★（1行だけで100%になる穴）",
      similarity(one_line := "これはそこそこ長さのある本文の行がひとつだけです。",
                 one_line)["judgeable"] is False)
    t("　行が十分あれば判定する", similarity(A, A)["judgeable"] is True)

    real = _w._get
    try:
        pages = {"https://a.example/1": A, "https://b.example/1": B,
                 "https://c.example/1": C}
        _w._get = lambda u, timeout=20: pages[u]          # noqa: E731
        r = check(list(pages))
        pair = {(x["a"], x["b"]) for x in r["suspects"]}
        t("★★転載の疑いを見つける★★", ("a.example", "b.example") in pair)
        t("　似ていない相手は疑いに入れない",
          not any("c.example" in (x["a"], x["b"]) for x in r["suspects"]))
        t("　全部の組み合わせを調べる（3件なら3組）", len(r["checked"]) == 3)
        _w._get = lambda u, timeout=20: (_ for _ in ()).throw(RuntimeError("落ちた"))
        r2 = check(list(pages))
        t("　取得できなければ理由を残す（黙って0件にしない）",
          len(r2["problems"]) == 3)
    finally:
        _w._get = real

    t("★すでに同じ系列として登録済みなら疑いに出さない★",
      _sl._lineage("yancha-press.com") == _sl._lineage("chonborista.com"))
    t("　P-WORLDとHAZUSEも同じ系列として登録済み",
      _sl._lineage("p-world.co.jp") == _sl._lineage("hazuse.com"))

    ng = [n for n, ok in results if not ok]
    print(f"{nl}{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--url", action="append")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.url or len(args.url) < 2:
        ap.print_help()
        return 0
    r = check(args.url)
    for c in r["checked"]:
        mark = "★疑い★" if c in r["suspects"] else ("（登録済み）"
                                                 if c["already_same_lineage"] else "")
        print(f"{c['a']:22} × {c['b']:22} 一致率 {c['ratio']:.0%} "
              f"（{c['same_lines']}行）{mark}")
    for p in r["problems"]:
        print("  ✗ " + p)
    if r["suspects"]:
        print(chr(10) + "★登録簿に無い転載の疑いがあります★")
        print("  同じ系列なら assets/data/source-registry.json の "
              "content_lineage_id を揃えてください（★対話セッションで判断★）")
        for s in r["suspects"]:
            for ex in s["examples"]:
                print(f"    同一文: {ex[:74]}")
    return 1 if r["suspects"] or r["problems"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(f"★想定外の失敗 {type(e).__name__}: {e}★")
        raise SystemExit(1)

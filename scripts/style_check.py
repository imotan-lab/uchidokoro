# -*- coding: utf-8 -*-
"""★記事の文体が「です・ます」でそろっているか★（2026-08-31・運営者の指示）

★運営者の言葉★
> 文体は統一したいね　今後も。
> タスクが走るたびに表記変わるのは避けたい

★★負の検査をやめる★★＝
いままで文体を見ていた `recheck.plain_style_gone` は
「常体の文末**19通り**」という名簿で、名簿に無い言い方は素通りした
（対照実験で「…となる。」が通ることを確認済み・CLAUDE.mdに記載）。
ここは**正の検査**＝「です・ます で終わっていること」を求め、
外れたものを全部挙げる。★名簿が増えない形★。

★★文でないものは数えない★★
  ・ラベルと値の行（「**天井**：1200G」「等価交換：520G〜から狙い目」）
    ＝これは表へ移すもの（運営者の要望③）。文体の対象ではない。
  ・「未確認（確認でき次第掲載します）」等の決まり文句

使い方:
  python scripts/style_check.py --all            # 全機種の件数と内訳
  python scripts/style_check.py --slug hokuto    # 1機種の場所
  python scripts/style_check.py --all --json     # 機械が読む
  python scripts/style_check.py --selftest

終了コード: 0=基準値以内 / 3=基準値より増えた / 1=読めなかった
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

# ★自分の出力の文字の扱いを固定する★（台帳#525・Windowsの既定では記号が書けない）
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                      # noqa: BLE001
        pass

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DETAILS = os.path.join(BASE, "assets", "data", "machine-details")
BASELINE = os.path.join(BASE, "assets", "data", "style-baseline.json")

# 認める文末（です・ます系）。閉じ括弧・強調記号が後ろに付いてもよい。
_OK_TAIL = re.compile(
    r"(?:です|ます|ません|でした|ました|ませんでした|でしょう|ましょう|"
    r"ください|でしょうか|ますか|ですか)"
    r"[）\)」』】\*＊\s]*$")

# ラベルと値の行（文ではない）
#   「**天井**：1200G」／「等価交換：520G〜から狙い目」
#   ★短い見出しのあとに「：」が来て、文の区切り（。）が無い★
_LABEL = re.compile(r"^\s*(?:\*\*)?([^：:。、\n]{1,14})(?:\*\*)?\s*[：:]")

# 記事が使う決まり文句（文体の対象にしない）
_STOCK = ("未確認（確認でき次第掲載します）",
          "未確認です。確認でき次第、この欄に掲載します。")


def is_label_line(line: str) -> bool:
    """★ラベルと値の行か★（文ではないので文体を求めない）。

    ★「。」があれば文とみなす★＝
    「天井は1200Gです。ただし…」のような文を取りこぼさないため。
    """
    t = str(line or "").strip()
    if not t or "。" in t:
        return False
    return bool(_LABEL.match(t))


# 括弧の対応（この中の「。」では切らない）
_OPEN = "（(「『【［〈"
_CLOSE = "）)」』】］〉"


def sentences(line: str) -> list:
    """段落を文へ切る。★「。」が無い段落は、それ全体を1文として見る★
    （体言止めを見つけるため）。

    ★括弧の中の「。」では切らない★（2026-08-31・実データで誤検知が出た）
      「…**（基準。天井950G+α）」を切ると、
      「天井950G+α）」という**文でない断片**が「体言止め」に見える。
    ★丸ごと括弧だけの断片は数えない★＝前の文に付く注記なので、
      それ自体に文体を求めるのは筋が違う。
    """
    t = str(line or "").replace("\n", "").strip()
    if not t:
        return []
    out, buf, depth = [], [], 0
    for ch in t:
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth = max(0, depth - 1)
        if ch == "。" and depth == 0:
            if "".join(buf).strip():
                out.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    if "".join(buf).strip():
        out.append("".join(buf).strip())
    return [x for x in out if not _only_paren(x)]


def _only_paren(text: str) -> bool:
    """その断片が、まるごと括弧1つだけか（前の文に付く注記）。"""
    t = str(text or "").strip()
    if len(t) < 2 or t[0] not in _OPEN or t[-1] not in _CLOSE:
        return False
    depth = 0
    for i, ch in enumerate(t):
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth -= 1
            if depth == 0 and i != len(t) - 1:
                return False
    return depth == 0


# 文末に付く注記の括弧（「…見込めます（8000G想定）」）
_TAIL_PAREN = re.compile(r"(?:（[^（）]*）|\([^()]*\))\s*$")


def ok_ending(sentence: str) -> bool:
    """その文が「です・ます」で終わっているか。

    ★文末の注記の括弧は外してから見る★＝
    「約850枚のプラスが見込めます（8000G想定）」は文体としては正しい。
    ★外しすぎない★＝括弧を外して何も残らないなら、外さずに見る。
    """
    t = str(sentence or "").strip()
    for _ in range(2):
        m = _TAIL_PAREN.search(t)
        if not m:
            break
        rest = t[:m.start()].strip()
        if not rest:
            break
        t = rest
    return bool(_OK_TAIL.search(t))


def problems(slug: str, detail) -> list:
    """1機種ぶんの「です・ます で終わっていない文」を挙げる。"""
    out = []
    if not isinstance(detail, dict):
        return out
    for si, sec in enumerate(detail.get("sections") or []):
        if not isinstance(sec, dict):
            continue
        title = str(sec.get("title") or "")
        for li, line in enumerate(sec.get("body") or []):
            if not isinstance(line, str) or not line.strip():
                continue
            if line.strip() in _STOCK or is_label_line(line):
                continue
            for s in sentences(line):
                if ok_ending(s):
                    continue
                out.append({"slug": slug, "section": title,
                            "line": li, "sentence": s})
    return out


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def scan_all() -> list:
    """全機種を見る（★読むだけ★）。"""
    out = []
    for p in sorted(glob.glob(os.path.join(DETAILS, "*.json"))):
        slug = os.path.basename(p)[:-5]
        try:
            d = _load(p)
        except Exception:                  # noqa: BLE001
            continue
        out += problems(slug, d)
    return out


def selftest() -> int:
    ok = []

    def t(name, cond):
        ok.append(bool(cond))
        print(("✅" if cond else "❌") + " " + name)

    t("★です・ます は通す★", ok_ending("天井は1200Gです"))
    t("　「ません」も通す", ok_ending("設定差はありません"))
    t("　閉じ括弧が後ろにあっても通す", ok_ending("約850枚のプラスが見込めます（8000G想定）"))
    t("★★名簿に無い常体も捕まえる★★"
      "（いままでの19通りの名簿では素通りしていた型）",
      not ok_ending("この場合は天井狙いとなる")
      and not ok_ending("設定6であることが確定する"))
    t("★★体言止めも捕まえる★★", not ok_ending("リセット時は天井が600G+αに短縮"))
    t("★ラベルと値の行は文体を求めない★",
      is_label_line("**天井**：1200G+α")
      and is_label_line("等価交換：ボーナス間520G〜から狙い目"))
    t("★★「。」がある行はラベルにしない★★"
      "（「天井は1200Gです。ただし…」を取りこぼさないため）",
      not is_label_line("天井：1200Gです。ただし前兆があります。"))
    t("　長い前置きのある文はラベルにしない",
      not is_label_line("この機種の通常時における天井のゲーム数：1200G"))

    t("★★括弧の中の「。」では切らない★★（実データで誤検知が出た）",
      sentences("目安です（基準。天井950G+α）")
      == ["目安です（基準。天井950G+α）"])
    t("　まるごと括弧だけの断片は数えない",
      sentences("天井は1200Gです。（スマスロ北斗）") == ["天井は1200Gです"])
    t("　ふつうの文は今までどおり切る",
      sentences("天井は1200Gです。狙い目は600Gです。")
      == ["天井は1200Gです", "狙い目は600Gです"])

    det = {"sections": [{"title": "天井・恩恵",
                         "body": ["天井は1200Gです。",
                                  "**メーカー**：サミー",
                                  "リセット時は600G+αに短縮"]}]}
    got = problems("zzz", det)
    t("★★記事から、直すべき文だけを挙げる★★",
      len(got) == 1 and got[0]["sentence"].endswith("短縮"))
    t("　決まり文句は挙げない",
      problems("zzz", {"sections": [{"title": "x",
                                     "body": [_STOCK[0]]}]}) == [])

    ng = ok.count(False)
    print(f"{len(ok) - ng}/{len(ok)} 合格")
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="記事の文体（です・ます）を見る")
    ap.add_argument("--slug")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()

    if a.slug:
        p = os.path.join(DETAILS, a.slug + ".json")
        if not os.path.isfile(p):
            print(f"★{a.slug} の記事データがありません★")
            return 1
        rows = problems(a.slug, _load(p))
    else:
        rows = scan_all()

    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0

    if a.slug:
        print(f"{a.slug}: です・ます で終わっていない文 {len(rows)} 件")
        for r in rows:
            print(f"  [{r['section']}] {r['sentence'][-46:]}")
        return 0

    by_slug = {}
    for r in rows:
        by_slug[r["slug"]] = by_slug.get(r["slug"], 0) + 1
    print(f"です・ます で終わっていない文: {len(rows)} 件 / {len(by_slug)} 機種")
    for sl, n in sorted(by_slug.items(), key=lambda x: -x[1])[:12]:
        print(f"  {n:4d}  {sl}")
    try:
        limit = int(_load(BASELINE)["max_lines"])
    except Exception as e:                 # noqa: BLE001
        print(f"★基準値を読めません: {e}★")
        return 1
    if len(rows) > limit:
        print(f"★増えています: {len(rows)} 件（基準 {limit} 件）★")
        return 3
    if len(rows) < limit:
        print(f"減りました（{limit} → {len(rows)}）。"
              "assets/data/style-baseline.json の max_lines を下げてください")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

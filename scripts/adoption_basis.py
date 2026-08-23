# -*- coding: utf-8 -*-
"""adoption_basis.py — ★その値を採用してよいか／どんな根拠で採ったか★

★なぜ要るか（2026-08-23・運営者決定）★
  「新台公開1週間前でもDMMしかない状態なら、そのDMMのだけを正として
    記事にしていいよ」

  実測で、解析2サイトが記事を書かない機種が実在する。
  LBトリプルクラウンX-300（DMM 5089・2026-08導入済み）は
  ちょんぼりすた・なな徹が両方 HEALTHY_NO_MATCH で、
  ★独立2出典に原理的に届かない★＝60日で打ち切られていた。

★★票の数え方は絶対に触らない★★
  `source_lineage.independent()` が「何票あるか」を決める唯一の場所、
  という設計はそのまま。ここが決めるのは
  **「その票数で採用してよいか」と「どんな根拠で採ったか」**だけ。

★★「1票でよい」にはしない★★（2026-08-23・Codexの指摘）
  `need_votes(ctx) -> 1` の形にすると、
  ★ちょんぼりすた単独・なな徹単独まで1票で通る★。
  例外はDMM単独に限る必要があるので、票数ではなく
  **支持の状態を分類して返す**形にした。

★根拠区分★
  INDEPENDENT_MULTI          … 独立2票以上で一致（今までどおり）
  DMM_SINGLE_NEAR_RELEASE    … DMM単独。導入7日前以降の例外
  （それ以外は採用しない）

★検索の品質点には数えない★
  DMM単独の値は `index_countable=False`。
  ＝記事には載せるが、検索に載せてよい濃さには数えない
  （2AIの確定値と同じ二層化）。★件数に期待して安全だと思わない★＝
  DMMだけで機械割・天井・AT・CZが採れると claim 5件・カテゴリ4種になり、
  何もしなければ AUTO_INDEXABLE になり得る（Codexの指摘で気づいた）。

使い方:
    python scripts/adoption_basis.py --selftest
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import source_lineage as _sl              # noqa: E402

# ★根拠区分★（この4つ以外を作らない）
INDEPENDENT_MULTI = "INDEPENDENT_MULTI"
DMM_SINGLE_NEAR_RELEASE = "DMM_SINGLE_NEAR_RELEASE"
NOT_ADOPTED = "NOT_ADOPTED"

# ★例外を許す出典はDMMだけ★（票のかたまりの名前で見る）
_DMM_VOTE_KEYS = ("vote:dmm-ptown", "dmm-ptown")
# ★導入の何日前から例外を認めるか★（運営者決定）
NEAR_RELEASE_DAYS = 7


class BasisError(RuntimeError):
    """判定に必要なものが足りない（★迷ったら採用しない★）。"""


def _today_jst() -> _dt.date:
    """★判定日は日本時間で決める★（2026-08-23・Codexの指摘）

    CIはUTCで動くので、素の today() だと**日付が1日ずれる**。
    「導入7日前以降か」の境目がずれると、例外の入り時期が変わる。
    """
    return (_dt.datetime.now(_dt.timezone.utc)
            + _dt.timedelta(hours=9)).date()


def is_dmm_only(vote_keys) -> bool:
    """その値を支持しているのがDMMだけか。

    ★票のかたまりで見る★＝同じ発行者の2ページは1票なので、
    URLの数ではなく `source_lineage` が作るキーで判断する。
    """
    keys = {str(k).strip() for k in (vote_keys or ()) if str(k or "").strip()}
    if not keys:
        return False
    return all(any(k == d or k.endswith(d) for d in _DMM_VOTE_KEYS)
               for k in keys)


def near_release(release_date: str, today: _dt.date | None = None) -> bool:
    """★導入7日前以降か（導入済みも含む）★

    ★日まで分かっているときだけ判定する★（2026-08-23・Codexの指摘）
      「2026-08」のような月だけの値では境目を決められない。
      ★決められないものを「まだ先」とも「もう近い」とも言わない★＝
      例外を適用しない側（安全側）に倒す。
    """
    t = str(release_date or "").strip()
    if len(t) != 10 or t[4] != "-" or t[7] != "-":
        return False
    try:
        d = _dt.date.fromisoformat(t)
    except ValueError:
        return False
    return (today or _today_jst()) >= d - _dt.timedelta(days=NEAR_RELEASE_DAYS)


def classify_support(vote_keys, ctx: dict | None = None,
                     today: _dt.date | None = None,
                     registry: dict | None = None) -> dict:
    """★その値を採用してよいか／どんな根拠か★

    ctx に要るもの:
      release_date        … DMMで確かめた導入日（YYYY-MM-DD）
      release_source      … その導入日をどこから取ったか（"dmm-ptown"）
      identity_verified   … DMMの機種ページで本人性を確かめたか（True/False）
      rival_values        … 同じ項目で別の値を出している出典があるか（真偽）

    ★足りないものがあれば例外にしない★（fail-closed）。
    """
    n = _sl.independent(vote_keys, registry)
    if n >= 2:
        return {"accepted": True, "independent_votes": n,
                "basis": INDEPENDENT_MULTI, "index_countable": True,
                "why": "独立2出典で一致"}
    c = dict(ctx or {})
    reasons = []
    if n != 1:
        reasons.append(f"票が{n}件")
    if not is_dmm_only(vote_keys):
        reasons.append("支持がDMMだけではありません")
    if not c.get("identity_verified"):
        reasons.append("DMMの機種ページで本人性を確かめていません")
    if str(c.get("release_source") or "") not in ("dmm-ptown", "dmm"):
        reasons.append("導入日の出どころがDMMではありません")
    if not near_release(c.get("release_date"), today):
        reasons.append("導入7日前より前か、導入日が日まで分かりません")
    if c.get("rival_values"):
        # ★反対の値がある値は例外にしない★＝食い違いを1出典で決めない
        reasons.append("同じ項目で別の値を出している出典があります")
    if reasons:
        return {"accepted": False, "independent_votes": n,
                "basis": NOT_ADOPTED, "index_countable": False,
                "why": "／".join(reasons)}
    return {"accepted": True, "independent_votes": 1,
            "basis": DMM_SINGLE_NEAR_RELEASE,
            # ★検索の濃さには数えない★（記事には載せる）
            "index_countable": False,
            "why": "DMMぱちタウン単独確認（導入7日前以降の例外）"}


# 読者に見せる言い方（★根拠区分から作る。表示文から逆算しない★）
READER_LABEL = {
    INDEPENDENT_MULTI: "独立2出典で一致",
    DMM_SINGLE_NEAR_RELEASE: "DMMぱちタウン単独確認",
}


def reader_label(basis: str) -> str:
    """★読者に出す言い方★（知らない区分は空＝何も名乗らない）"""
    return READER_LABEL.get(str(basis or ""), "")


def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅ " if cond else "❌ ") + name)

    D = "vote:dmm-ptown"
    C = "vote:chonborista"
    N = "vote:nana-press"
    DAY = _dt.date(2026, 8, 23)
    OK_CTX = {"release_date": "2026-08-20", "release_source": "dmm-ptown",
              "identity_verified": True, "rival_values": False}

    t("　独立2票なら今までどおり採用（濃さにも数える）",
      classify_support([D, C], OK_CTX, DAY)["basis"] == INDEPENDENT_MULTI
      and classify_support([D, C], OK_CTX, DAY)["index_countable"] is True)
    t("★★DMM単独＋導入7日前以降なら採用する★★（運営者決定）",
      classify_support([D], OK_CTX, DAY)["accepted"]
      and classify_support([D], OK_CTX, DAY)["basis"]
      == DMM_SINGLE_NEAR_RELEASE)
    t("★★DMM単独の値は検索の濃さに数えない★★"
      "／★件数に期待して安全だと思わない★（Codexの指摘）",
      classify_support([D], OK_CTX, DAY)["index_countable"] is False)
    # ★★ここが「1票でよい」にしなかった理由★★
    t("★★ちょんぼりすた単独は通さない★★（例外はDMMだけ）",
      not classify_support([C], OK_CTX, DAY)["accepted"])
    t("★★なな徹単独も通さない★★",
      not classify_support([N], OK_CTX, DAY)["accepted"])
    t("★★導入がまだ8日以上先なら通さない★★",
      not classify_support([D], {**OK_CTX, "release_date": "2026-09-30"},
                           DAY)["accepted"])
    t("　ちょうど7日前は通す（境目）",
      classify_support([D], {**OK_CTX, "release_date": "2026-08-30"},
                       DAY)["accepted"])
    t("　8日前は通さない（境目のもう一歩）",
      not classify_support([D], {**OK_CTX, "release_date": "2026-08-31"},
                           DAY)["accepted"])
    t("★★導入済みでも通す★★（X-300のような機種を救うため）",
      classify_support([D], {**OK_CTX, "release_date": "2026-08-01"},
                       DAY)["accepted"])
    t("★★導入日が月までしか分からなければ通さない★★"
      "／決められないものを「近い」と言わない",
      not classify_support([D], {**OK_CTX, "release_date": "2026-08"},
                           DAY)["accepted"])
    t("★★本人性を確かめていなければ通さない★★",
      not classify_support([D], {**OK_CTX, "identity_verified": False},
                           DAY)["accepted"])
    t("★★導入日の出どころがDMMでなければ通さない★★",
      not classify_support([D], {**OK_CTX, "release_source": "chonborista"},
                           DAY)["accepted"])
    t("★★同じ項目で別の値を出している出典があれば通さない★★"
      "／食い違いを1出典で決めない",
      not classify_support([D], {**OK_CTX, "rival_values": True},
                           DAY)["accepted"])
    t("　票が0なら通さない",
      not classify_support([], OK_CTX, DAY)["accepted"])
    t("　DMMと別の1票の組（＝独立2票）は今までどおり",
      classify_support([D, N], OK_CTX, DAY)["basis"] == INDEPENDENT_MULTI)
    t("　読者に出す言い方は根拠区分から作る",
      reader_label(INDEPENDENT_MULTI) == "独立2出典で一致"
      and reader_label(DMM_SINGLE_NEAR_RELEASE) == "DMMぱちタウン単独確認"
      and reader_label("知らない区分") == "")
    t("★★判定日は日本時間で決める★★（CIのUTCで1日ずれない）",
      isinstance(_today_jst(), _dt.date))

    ng = sum(1 for _, o in results if not o)
    print()
    print("%d/%d 合格" % (len(results) - ng, len(results)))
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="採用の根拠を決める")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

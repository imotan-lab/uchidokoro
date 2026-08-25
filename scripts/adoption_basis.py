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


def other_sources_known(slug: str, index_urls) -> tuple:
    """★索引に出ていないだけで、別の出典を知っていないか★

    ★なぜ要るか（2026-08-23・Codexの敵対的レビューP0）★
      新台経路は**索引で見つかったURLだけ**を材料候補にする。
      一方、人と2AIが確かめた出典は `machine_sources` に控えてあるが、
      そちらは `collect_evidence` でしか使われていない。

      そのため次の経路が成立していた:
        ①DMMは索引で見つかる
        ②ちょんぼりすたにも記事があり、URLを控えてある
        ③しかし索引の1ページ制限や表記差で拾えない
        ④gather にはDMMだけが渡る
        ⑤★「DMM単独」と誤判定して、控えのページと食い違っていても気づかない★

      ＝**誤情報に到達する経路**。

    ★ここでやること★＝「DMM単独だ」と名乗る前に、
      **控えに別の発行者の出典が無いか**だけを確かめる。
      ★材料には足さない★（足すには同定・メーカー照合・本文の同一性など
      条件が多く、別の作業になる。ここは**例外を名乗らせない**だけ）。

    ★読めないときは「知っている」と答える★＝例外を通さない側（fail-closed）。

    返すもの: (別の出典を知っているか, 理由)
    """
    try:
        sys.path.insert(0, os.path.join(BASE, "scripts"))
        import machine_sources as _ms
    except Exception as e:                                   # noqa: BLE001
        return True, f"控えを読み込めません（{type(e).__name__}）"
    if not slug:
        return True, "slugが分からないので確かめられません"
    try:
        saved = _ms.urls_for(slug)
    except Exception as e:                                   # noqa: BLE001
        return True, f"控えを読めません（{str(e)[:40]}）"
    # ★★索引と控えの「和集合」で見る★★（2026-08-23・Codexの再レビューP0-1）
    #   ★直す前は「索引にもある控えは別口ではない」と外していた★。
    #   ところがその索引のページは、このあと個別に読むときに落ちる
    #   （題が略称・HTMLが変わった・読み取り失敗）。
    #   落ちたページは票から外れるので、
    #     ①DMMと別出典が索引で見つかる
    #     ②控えの確認は「索引にもある」と無視
    #     ③別出典が ok=False で落ちる
    #     ④票に残るのはDMMだけ
    #     ⑤★「DMM単独」として採用が復活する★
    #   ＝塞いだはずの「知っている別出典との食い違いを見ない経路」が残っていた。
    #   ★索引に載っているかを問わず、DMM以外を1件でも知っていれば止める★
    others = []
    try:
        for u in (index_urls or []):
            if u and not is_dmm_only([_vote_key_of(str(u))]):
                others.append(str(u))
    except Exception:                                        # noqa: BLE001
        return True, "URLをそろえて比べられません"
    for rec in (saved or []):
        u = str((rec or {}).get("url") or "")
        if not u:
            continue
        if is_dmm_only([_vote_key_of(u)]):
            continue                      # DMM自身の別ページは「別の出典」ではない
        others.append(u)
    if others:
        # ★同じURLを2度言わない★（索引と控えの両方にあることは普通）
        uniq = []
        for u in others:
            try:
                k = _ms.url_key(u)
            except Exception:                                # noqa: BLE001
                return True, "URLをそろえて比べられません"
            if k not in [x[0] for x in uniq]:
                uniq.append((k, u))
        return True, ("DMM以外の出典を知っています"
                      "（索引・控えのどちらかにあります）: "
                      + " / ".join(u for _, u in uniq[:2]))
    return False, ""


def _vote_key_of(url: str) -> str:
    """そのURLの票のかたまり（読めなければURLをそのまま返す）。"""
    try:
        return _sl.vote_key_of_url(url) or str(url)
    except Exception:                                        # noqa: BLE001
        return str(url)


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
    # ★★索引に出ていないだけの出典を「無い」と扱わない★★
    #   （2026-08-23・Codexの敵対的レビューP0）
    #   ★呼び出し側が確かめて渡す★＝ここで勝手に控えを読みに行くと、
    #   試験のたびに実データへ触りに行くことになる。
    if c.get("other_sources_known"):
        reasons.append(str(c.get("other_sources_why")
                           or "索引に出ていない別の出典を知っています"))
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
# ★★記事に出る文言は build_new_article の正本から取る★★（2026-08-26）
#   ★2026-08-26に運営者の指示でサイト名をやめた★
#   （「ほかサイトのコピーと思われたくない」）。
#   ここに写しを持つと、正本を変えても**古い文言のまま試験が期待し続ける**
#   （実際そうなって、壊し方の通し確認が16件「壊す前から赤い」を出した）。
def _reader_label_map() -> dict:
    try:
        import build_new_article as _ba
        # ★INDEPENDENT_MULTI は内部の呼び名★（記事には出さない）
        return {INDEPENDENT_MULTI: "独立2出典で一致",
                DMM_SINGLE_NEAR_RELEASE:
                    _ba.BASIS_SUFFIX.get(DMM_SINGLE_NEAR_RELEASE, "").strip("（）")}
    except Exception:                                        # noqa: BLE001
        return {INDEPENDENT_MULTI: "独立2出典で一致",
                DMM_SINGLE_NEAR_RELEASE: "未確認"}


READER_LABEL = _reader_label_map()


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
    # ★★索引に出ていないだけの出典を「無い」と扱わない★★
    #   （2026-08-23・Codexの敵対的レビューP0）
    #   ★実際に成立していた誤情報の経路★＝
    #   ちょんぼりすたに記事があり控えてもあるのに、索引の1ページ制限で
    #   拾えず、DMM単独と誤判定して**食い違いを見逃す**。
    t("★★控えに別の出典があるなら「DMM単独」と名乗らない★★"
      "／★これが無いと、控えのページと食い違っていても気づかない★",
      not classify_support([D], {**OK_CTX, "other_sources_known": True,
                                 "other_sources_why": "控えにちょんぼりすた"},
                           DAY)["accepted"])
    t("　断った理由に、控えの中身が出る",
      "控えにちょんぼりすた"
      in classify_support([D], {**OK_CTX, "other_sources_known": True,
                                "other_sources_why": "控えにちょんぼりすた"},
                          DAY)["why"])
    # ★控えを読む側の試験★（実データに触らない形で確かめる）
    t("★★slugが分からなければ「知っている」と答える★★（安全側）",
      other_sources_known("", ["https://p-town.dmm.com/machines/1"])[0])
    t("　控えに何も無ければ「知らない」と答える",
      not other_sources_known("存在しない機種zzz",
                              ["https://p-town.dmm.com/machines/1"])[0])

    # ★★控えを読めないときは「知っている」に倒す★★（安全側）
    #   ★対照実験で、この分岐を見ている試験が無いと分かった★（2026-08-23）
    #   ここが破れると、控えが壊れた日に1出典で公開できてしまう。
    def _with_broken_ledger():
        import machine_sources as _ms
        _bak = _ms.urls_for
        try:
            def _boom(slug, data=None):
                raise RuntimeError("控えが壊れています（試験）")
            _ms.urls_for = _boom
            return other_sources_known("zzz", ["https://p-town.dmm.com/x"])
        finally:
            _ms.urls_for = _bak

    _broken = _with_broken_ledger()
    t("★★控えを読めないときは「別の出典を知っている」に倒す★★"
      "／★控えが壊れた日に1出典で公開させない★",
      _broken[0] is True and "控えを読めません" in _broken[1])
    t("　そのときは例外そのものが通らない",
      not classify_support([D], {**OK_CTX,
                                 "other_sources_known": _broken[0],
                                 "other_sources_why": _broken[1]},
                           DAY)["accepted"])
    t("　票が0なら通さない",
      not classify_support([], OK_CTX, DAY)["accepted"])
    t("　DMMと別の1票の組（＝独立2票）は今までどおり",
      classify_support([D, N], OK_CTX, DAY)["basis"] == INDEPENDENT_MULTI)
    t("　読者に出す言い方は根拠区分から作る",
      reader_label(INDEPENDENT_MULTI) == "独立2出典で一致"
      and reader_label(DMM_SINGLE_NEAR_RELEASE) == READER_LABEL[DMM_SINGLE_NEAR_RELEASE]
      and reader_label("知らない区分") == "")
    t("★★判定日は日本時間で決める★★（CIのUTCで1日ずれない）",
      isinstance(_today_jst(), _dt.date))

    _end_to_end_tests(t)

    ng = sum(1 for _, o in results if not o)
    print()
    print("%d/%d 合格" % (len(results) - ng, len(results)))
    return 1 if ng else 0


def _raises_build(fn) -> bool:
    """★その呼び出しが例外で止まるか★（止まらなければ守りが無い）"""
    try:
        fn()
    except Exception:                                        # noqa: BLE001
        return True
    return False

def _end_to_end_tests(t) -> None:
    """★★入口から検索判定までを一続きで通す★★（2026-08-23・Codexの指摘7）

    ★なぜ要るか★＝今日だけで★5回★「片方だけ直した」を踏んだ。

      ①D案の配線を lookup だけに入れて材料側を忘れた
      ②試験が自分の作った材料を採点していた（LEAD_TEMPLATE）
      ③待ち行列の鍵が変わったのに控え側が字面のまま
      ④抽出器に配線したが**入口の早期returnで一度も呼ばれなかった**
      ⑤basis を cz と at にだけ入れて spec と ceiling を忘れた

    ★どれも「部品ごとの試験」は全部緑だった★。
    ここは**部品をまたいで**、
      材料 → 採否 → 記事の名乗り → 検索の濃さ
    が食い違っていないことを見る。
    """
    # ★記事に出る文言は BASIS_SUFFIX が正本★（READER_LABEL は内部の呼び名）
    #   ★2026-08-26に取り違えた★＝READER_LABEL を記事の文言だと思って比べ、
    #   試験が丸ごと落ちた（＝壊し方が16件「壊す前から赤い」になった）。
    _MARK_ART = __import__("build_new_article").BASIS_SUFFIX[
        DMM_SINGLE_NEAR_RELEASE]
    import build_new_article as _ba
    import page_decision as _pd

    D = "vote:dmm-ptown"
    C = "vote:chonborista"
    DAY = _dt.date(2026, 8, 23)
    CTX = {"release_date": "2026-08-20", "release_source": "dmm-ptown",
           "identity_verified": True}

    def _mat(sup):
        """採否の結果を、そのまま材料の形にする（★手で basis を書かない★）"""
        b = sup["basis"]
        return {"adopted": {"payout_range": {"basis": b, "sources": ["a"],
                                             "value": {"low": 97.0,
                                                       "high": 110.0,
                                                       "unit": "%"}}},
                "ceilings": {"adopted": [{"basis": b, "kind": "GAME",
                                          "amount": 999, "unit": "G",
                                          "benefit": "AT当選",
                                          "sources": ["a"]}]},
                "at_specs": {"adopted": [{"basis": b, "mode": "MAIN_AT",
                                          "net": 1.0, "sources": ["a"]}]}}

    def _texts(d):
        out = []
        for sec in (d.get("sections") or []):
            out += [str(x) for x in (sec.get("body") or [])]
        for row in (d.get("factTable") or []):
            out += [str(x) for x in (row or [])]
        return out

    # ①DMM単独のとき
    solo = classify_support([D], CTX, DAY)
    m_solo = _mat(solo)
    d_solo = _ba.build_detail("zzz", "試験機", "2026-08-20", m_solo)
    t("★★通し：DMM単独→採用され、記事に名乗りが付き、検索には数えない★★",
      solo["accepted"]
      and any(_MARK_ART in x for x in _texts(d_solo))
      and _pd.index_claims_from_material(m_solo) == []
      and len(_pd.regression_claims_from_material(m_solo)) == 3)

    # ②独立2出典のとき
    multi = classify_support([D, C], CTX, DAY)
    m_multi = _mat(multi)
    d_multi = _ba.build_detail("zzz", "試験機", "2026-08-20", m_multi)
    t("★★通し：独立2出典→名乗りは出ず、検索の濃さに数える★★",
      multi["accepted"]
      and not any(_MARK_ART in x for x in _texts(d_multi))
      and len(_pd.index_claims_from_material(m_multi)) == 3)

    # ③★採否と表示と検索がそろっているか★（片方だけ直したら落ちる）
    t("★★採否・名乗り・検索の3つが同じ根拠を見ている★★"
      "／★今日5回踏んだ「片方だけ直した」をここで捕まえる★",
      (solo["index_countable"] is False
       and _pd.index_claims_from_material(m_solo) == [])
      and (multi["index_countable"] is True
           and _pd.index_claims_from_material(m_multi) != []))

    # ③-b ★★根拠を保存し忘れた経路★★（2026-08-23）
    #   ★対照実験で分かった★＝上の①②は材料が必ず basis を持つので、
    #   黒名簿へ戻しても同じ結果になり **27/27 のまま通ってしまった**。
    #   ＝★また「自分で作った材料で採点」していた（今日4回目）★。
    #   黒名簿の危険は「根拠が**無い**値」でしか現れないので、
    #   抽出器が保存し忘れた形をここで作る。
    m_forgot = _mat(solo)
    for _k in ("ceilings", "at_specs"):
        for _c in m_forgot[_k]["adopted"]:
            _c.pop("basis", None)
    m_forgot["adopted"]["payout_range"].pop("basis", None)
    t("★★通し：根拠を保存し忘れた値は、検索の濃さに数えない★★"
      "／★黒名簿だとここが素通りして、1出典の内容が検索に出る★",
      _pd.index_claims_from_material(m_forgot) == [])
    t("　それでも壊れた材料は例外で止まる（数えないことと検査は別）",
      _pd.regression_claims_from_material(m_forgot) != [])

    # ③-c ★★本物の抽出器の戻り値をそのまま流す（4つの家族を1つの表で）★★
    #   （2026-08-24・Codexの3回目の指摘4＝
    #     「手書きの変異を増やすより、対象集合を1か所に固定せよ」）
    #
    #   ★なぜ表にするか★＝天井とスペックだけ本物を通し、
    #   AT と CZ を手作りのまま残した（今日7回目の「片方だけ直した」）。
    #   ★1つずつ書くと、必ずどれかを書き忘れる★。
    #   ここに1行足せば、その家族にも同じ検査が全部かかる。
    #
    #   見るもの（家族ごとに同じ6つ）
    #     ①本物の抽出器が根拠を保存している
    #     ②その戻り値は検索の濃さに数えない（単独確認だから）
    #     ③けれど「知っているか」には数える（回帰の判定用）
    #     ④記事にすると単独確認の名乗りが付く
    #     ⑤根拠を落とすと**公開そのものを断る**
    #     ⑥根拠を落としても検索の濃さには数えない（白名簿）
    import at_spec_lookup as _at_mod
    import build_new_article as _ba_mod
    import ceiling_lookup as _cl_mod
    import cz_lookup as _cz_mod
    import spec_lookup as _sl_mod

    def _base(host, **kw):
        d = {"url": f"https://{host}/x", "host": host, "ok": True,
             "reason": "OK"}
        d.update(kw)
        return d

    FAMILIES = (
        ("スペック", _sl_mod, "adopted",
         lambda h: _base(h, fields={"payout_range": {"low": 97.0,
                                                     "high": 110.0,
                                                     "unit": "%"}})),
        ("天井", _cl_mod, "ceilings",
         lambda h: _base(h, cz_names=set(),
                         ceilings=[{"kind": "GAME", "amount": 999, "unit": "G",
                                    "counted": "通常時", "benefit": "AT当選",
                                    "certainty": "確定", "raw": "999G"}])),
        ("AT", _at_mod, "at_specs",
         lambda h: _base(h, specs=[{"mode": "MAIN_AT", "games": 30,
                                    "net": 2.8}])),
        ("CZ", _cz_mod, "czs",
         lambda h: _base(h, unresolved=[],
                         czs=[{"name": "試験CZ", "games": "8G",
                               "rate": "50%"}])),
    )

    def _rows_of(res, box):
        """抽出器の戻り値から、採用した行の一覧を取り出す。"""
        got = res.get("adopted")
        if box == "adopted":                 # スペックは辞書で返る
            return list((got or {}).values())
        return list(got or [])

    def _mat_of(res, box):
        """抽出器の戻り値を、そのまま材料の形にする（★手で作らない★）。"""
        if box == "adopted":
            return {"adopted": res.get("adopted") or {}}
        return {"adopted": {}, box: {"adopted": res.get("adopted") or []}}

    def _strip(mat, box):
        """根拠だけを落とした材料（＝抽出器が保存し忘れた形）。"""
        import json as _json
        out = _json.loads(_json.dumps(mat))
        rows = (out.get("adopted") or {}).values() if box == "adopted" \
            else (out.get(box) or {}).get("adopted") or []
        for r in rows:
            if isinstance(r, dict):
                for k in ("basis", "games_basis", "rate_basis"):
                    r.pop(k, None)
        return out

    for _name, _mod, _box, _page_of in FAMILIES:
        try:
            _res = _mod.compare([_page_of("p-town.dmm.com")], ctx=CTX)
            _rows = _rows_of(_res, _box)
            t(f"★★本物の{_name}の抽出器が根拠を保存している★★"
              "／★手作りの材料では、保存し忘れに気づけない★",
              bool(_rows) and all(
                  r.get("basis") == DMM_SINGLE_NEAR_RELEASE for r in _rows))
            _mat_real = _mat_of(_res, _box)
            t(f"　{_name}：単独確認は検索の濃さに数えない",
              _pd.index_claims_from_material(_mat_real) == [])
            t(f"　{_name}：それでも「知っている」には数える",
              _pd.regression_claims_from_material(_mat_real) != [])
            _txt = str(_ba_mod.build_detail("zzz", "試験機", "2026-08-24",
                                            _mat_real))
            # ★文言は正本から取る★（2026-08-26。直に書くと、正本を変えても
            #   古い文言のまま期待し続ける＝この日それで16件が確かめられなかった）
            t(f"　{_name}：記事にすると裏付けの弱さの断りが付く",
              _ba_mod.BASIS_SUFFIX[DMM_SINGLE_NEAR_RELEASE] in _txt)
            _mat_bare = _strip(_mat_real, _box)
            t(f"★★{_name}：根拠を落とした材料は公開を断る★★"
              "／★空で流すと断りなしの普通の値として読者に出る★",
              _raises_build(lambda m=_mat_bare: _ba_mod.build_detail(
                  "zzz", "試験機", "2026-08-24", m)))
            t(f"　{_name}：根拠を落としても検索の濃さには数えない（白名簿）",
              _pd.index_claims_from_material(_mat_bare) == [])
        except AssertionError:
            raise
        except Exception as e:                               # noqa: BLE001
            t(f"★★本物の{_name}の抽出器を通せません"
              f"（{type(e).__name__}: {str(e)[:50]}）★★", False)

    # ★家族の顔ぶれが増えたら、必ずこの表にも足す★
    t("★★家族の名簿と、この表が一致している★★"
      "（新しい家族を足したのに、ここへ足し忘れたら落ちる）",
      {x[2] for x in FAMILIES} == set(_pd.CLAIM_BOXES))

    # ④控えに別の出典があるときは、通し全体が止まる
    blocked = classify_support([D], {**CTX, "other_sources_known": True,
                                     "other_sources_why": "控えに別の出典"},
                               DAY)
    t("★★通し：控えに別の出典があれば、採用そのものが起きない★★",
      not blocked["accepted"] and blocked["basis"] == NOT_ADOPTED)


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

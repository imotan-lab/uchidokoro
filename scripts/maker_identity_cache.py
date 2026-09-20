"""maker_identity_cache.py — 「この名鑑ページを、この機種の材料に使うか」の控え。

★何を決める器か（2026-08-17・依頼228でv2へ）★
  ★★これは「会社が同じか」を決める器ではない★★
  平和とオリンピアエステート、三洋物産とサンスリーは**別法人**である。
  それなのに v1 は結論を `MATCH`（＝同じ）と書き、ログにも
  「同じメーカーと決めてあります」と出していた。**事実と合っていない**
  （Codex依頼228の指摘5）。決めているのは会社の同一性ではなく、
  **その名鑑ページを、このDMM機種の材料として使うかどうか**でしかない。

  そこで結論を言い換えた:
    ACCEPT_MATERIAL … この名鑑ページを、この機種の材料に使う
    REJECT_MATERIAL … 使わない
  控えには `basis_scope`（何を根拠にしたか）と
  `relationship_verified`（会社の関係を機械で確かめたか＝いまは常に false）を残す。

★なぜ要るか★
  名鑑によって同じ機種のメーカー欄が違う。
    L転生王女 … DMM「オリンピアエステート」／ちょんぼりすた・なな徹「平和」
  これまでは名簿（maker-catalogs.json）に**人が足すまで**その機種が止まっていた。

★会社の関係はどこで見るか（2026-08-17・運営者判断）★
  ★メーカー公式へは通信しない★（運営者が2026-08-16に取りやめ）。
  グループ関係は `maker-catalogs.json` の `maker_relation_group` に入っている。
  **これはリポジトリの中にあり、変更に承認が要るファイル**で、
  根拠（日本遊技機工業組合のグループ会社一覧を人が読んだこと）は
  `_group_why` に書いてある。★機械はそこへも取りに行かない★＝
  名簿を読むだけ。だから新しい通信先が増えない。

★この器を見るのは2つの場合★（2026-09-12に書き直した・Codexの指摘）
  ★直す前の説明は「RELATEDのときだけ／MATCHはこの器が要らない」だった★が、
  ★題で救う型（title_name_core_mismatch / title_tail_conflict）では
  MATCH でも控えが要る★（題が読めないので、本人かどうかを2AIが決める）。
  ＝説明が実装と食い違っていた。

  ①メーカー欄の話（maker_field）
    名簿で一致（MATCH）           … そのまま使う（この器は要らない）
    関係のある社（RELATED）       … **この器を見る**
  ②題が読めない話（title_name_core_mismatch / title_tail_conflict）
    ★MATCH でも RELATED でも、この器を見る★
    （題で同定できていないので、本人かどうかは2AIが決める）

  ★どちらの場合も通さないもの★
  ③どの社か分からない（UNKNOWN）    … ★救わない★＝常に除く
  ④明らかに別の社（MISMATCH）        … 常に除く
  ★UNKNOWN を控えで救ってはいけない★＝名簿に無いだけの**任意の別会社**まで
  同じ扱いになる。同名で別メーカーの機種は実在する
  （パチスロ犬夜叉＝2016年ロデオ／2022年クロスアルファ）。

★答えが出ない状態は保存しない★
  レコードが無い＝「まだ決めていない」（毎回もう一度考える）。

置き場: Documents/uchidokoro/maker_identity_cache.json（リポジトリ外・公開しない）

使い方:
    python scripts/maker_identity_cache.py --list
    python scripts/maker_identity_cache.py --record \\
        --machine-url https://p-town.dmm.com/machines/5086 \\
        --expected olympia_estate --seen 平和 --verdict ACCEPT_MATERIAL \\
        --why <理由> --by claude,codex \\
        --evidence "https://…|逐語引用|directory_observation" \\
        --evidence "https://…|逐語引用|directory_observation"
    python scripts/maker_identity_cache.py --selftest
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import claim_identity as _ci          # noqa: E402
import local_paths as _lp             # noqa: E402

STORE = _lp.doc("maker_identity_cache.json")
# ★v2＝意味を変えた（2026-08-17・依頼228）★
#   「会社が同じか」ではなく「この名鑑ページを材料に使うか」。
#   版を上げるので、v1の控えはそのままでは読めない（＝黙って混ざらない）。
# ★v3＝鍵を「機種＋対象ページ」にした（2026-08-17・台帳#390／Codex依頼233）★
#   v2は (機種・期待する社・名鑑の表記) で引き、そのあと対象URLが根拠に
#   含まれるかを見ていました。すると
#     ①メーカーの食い違いが無い場合（題が略称、など）は鍵を作れない
#     ②根拠が2ページあると、そのどちらも対象ページになり得る
#   ので、★対象ページを独立した必須の項目★にしました。
# ★★v4＝「そのページをこの機種の材料に使うか」を2AIが決めた控え★★
#   （2026-09-17・運営者の指示。正本＝`_design/material_decision_2ai_2026-09-17.md`）
#   ＞ もうさ、機械的に見るのやめたら？
#   ＞ シンプルに行かない？ 検索する項目だけ決めてさ、2AIで拾ってくるだけ。
#
#   ★v3から何を外したか★＝どれも「機械に意味を判定させていた」ところ。
#     ・証明の型（proof_profile）と落ち方の並び（reason_codes）
#     ・メーカー欄の表記（expected / seen / seen_maker）と会社の関係
#     ・証拠ページを題の分解（page_is_machine）で同定すること
#     ・引用に「機種名・メーカー欄・導入日」が3つとも入っていること
#     ・独立2名鑑を証明に要求すること（★値の独立2出典とは別物★）
#     ・証拠の役割（target / support）の使い分け
#
#   ★なぜ外したか（実測）★＝この層は歯止めとして働いていなかった。
#     ・正しい答えを止めていた（モンハンライズが8晩・ウミンチュ・聖闘士星矢）
#     ・見ていたのは「題名の字が同じ形に分解できるか」であって、
#       本当に知りたい「このページはこの機種のことを書いているか」ではない。
#     ・1つ直すと次の書き方で止まる、を繰り返していた（2026-09-16に7件）。
#
#   ★歯止めは2AIと、機械が確かめられること★＝
#     ①判断者が claude と codex の2つ（Claudeは相手の答えを見る前に封をする）
#     ②引用が、機械が取り直した本文に**そのまま在る**（言うだけでは通さない）
#     ③本文の指紋が、判断したときと同じ
#     ④値の採否は今までどおり独立2出典（source_lineage）
# ★★v5＝指紋の意味が変わった★★（2026-09-18・台帳#696）
#   v4 までの `body_sha256` は **掃除済みHTMLの全文**の指紋だった。
#   ★取ってくるたびに変わるので、2AIの控えが数秒で失効していた★
#   （なな徹はCSSのURLにそのときのunix秒／DMMは csrf-token と画像の `?t=`）。
#   いまは **`user_area.readable_text`（2AIが読む文字）** の指紋。
#   ★版を上げる理由★＝どちらも64桁の16進なので、控えだけ見ても区別できない。
#   別のPCやバックアップに残った古い控えを、形の上で確実に断るため。
SCHEMA = "maker-identity-cache/v5"
VERDICTS = ("ACCEPT_MATERIAL", "REJECT_MATERIAL")


def _page_sha(page) -> str:
    """★その器がいま持っている本文から、指紋をその場で数え直す★

    ★作った時の値を読まない★（2026-08-17・Codex依頼238の厚みと同じ理由）＝
    器は書き換えられるので、`page.text_sha256` を信じない。
    ★既定値つきの `getattr` にしない★＝直し忘れたときに空文字になって
    **黙って「一致しない」**になり、原因が分からなくなる。
    """
    if page is None:
        return ""
    import user_area as _uas
    # ★既定値つきの getattr にしない★（2026-09-18・Codexの指摘）＝
    #   器を取り違えたときに AttributeError ではなく「空の本文の指紋」になり、
    #   **黙って一致しない**になる。説明にそう書きながら実装がそうなっていた。
    return _uas.readable_sha256(page.cleaned_html)
# ★★v3の証明の型（廃止）★★＝読むためだけに名前を残す。
#   ★新しい控えには書かない★／★この型を持つ古い控えは使わない★
#   （fail-closed＝2AIが決め直す。移行の分岐を作らない）
_V3_PROFILES = {
    # 名鑑のメーカー欄がDMMと違う（v2からの継続）
    #   → ★独立した名鑑2つ★の観測が要る
    "maker_field": {"min_directories": 2, "needs_maker": True},
    # 名鑑の題・見出しが略称で、機種の同定に落ちる
    #   → ★対象ページ自身＋DMM★でよい（2件目の名鑑は別途正規の同定を通る）
    #   → ただし★メーカー欄が読めて、名簿で解決できること★が必須
    #     （題もメーカーも食い違うページを、弱い側で通さないため）
    #     ★通すのは一致（MATCH）と、同じグループと確認されている社（RELATED）★
    #     ★どの社か分からない（UNKNOWN）・別の社（MISMATCH）は通さない★
    #     （2026-09-12・台帳#607／#608。RELATED はもともと
    #       「控えで決めてあるときだけ材料に使う」印なので、控えで通す）
    "title_name_core_mismatch": {"min_directories": 1, "needs_maker": True},
    # ★題の後ろの飾りを分解できない★（2026-08-26・実測で25%が該当）
    #   例＝「機種名 スロット 新台 設定判別 打ち方 プレミアム 解析」の
    #   「プレミアム」。★飾りの辞書に足す直し方は採らない★ので、
    #   機械では決められない＝2AIが決めて控える。
    #   → ★対象ページ自身＋DMM★でよい（上と同じ扱い）
    #   → ただし★メーカー欄が読めて、名簿で解決できること★が必須
    #     ★通すのは一致（MATCH）と、同じグループと確認されている社（RELATED）★
    #     ★どの社か分からない（UNKNOWN）・別の社（MISMATCH）は通さない★
    #     （2026-09-12。★直す前はこの型に検査が当たっておらず、
    #       契約に「一致が必須」と書いてあるのに別の社でも通っていた★）
    #   ★救えるのはちょうど TAIL_CONFLICT のときだけ★＝
    #   別機種（NAME_CORE_MISMATCH）・規格違い（GEN_MARK_CONFLICT）・
    #   派生機（DERIV_MARK_CONFLICT）は今までどおり拒否する。
    "title_tail_conflict": {"min_directories": 1, "needs_maker": True},
}
# ★何を根拠にしたか★＝控えを読む人・監査が、守りの範囲を取り違えないための印。
BASIS_SCOPE = "directory_consensus_only"
# ★「2AIで決めます」を機械の約束にする★（2026-08-14・依頼193のP2）
#   以前は ["foo", "bar"] のような**架空のID2つ**でも「違う2者」だった。
#   ★これは本人確認ではない★＝手で ["claude","codex"] と書くことは防げない。
#   増えたらここだけ直す。
ALLOWED_AGREERS = frozenset({"claude", "codex"})
MIN_QUOTE = 8                          # 逐語引用の最低の長さ
# ★引用は「事実の欄の写し」までにとどめる★（2026-08-17・Codex依頼229の指摘3）
#   なな徹の規約（第7条1項(1)「入手したコンテンツの複製」）について、
#   運営者は2026-08-17に**「機種名・メーカー欄・導入日という事実の欄だけ・
#   1件50字前後・記事本文や表は保存しない」という前提で**「続ける」と判断した。
#   ★その前提をコードで守らせる★＝以前は下限しか無く、記事本文を丸ごと
#   引用として保存できた（判断の前提を実装が保証していなかった）。
MAX_QUOTE = 120                        # 逐語引用の最大の長さ
MAX_EVIDENCE = 4                       # 根拠の件数の上限
# ★根拠は名鑑の観測だけ★（2026-08-17・依頼228）
#   `official_relationship`（メーカー公式の会社関係ページ）は**削除した**。
#   運営者が「メーカー公式は使わない」と決めたため（止めずに消す）。
KINDS = ("directory_observation",)
# ★理由は「書いてあること」だけ見る★＝中身は機械が判定しない（意味の判断）。
MIN_WHY = 15


class CacheError(Exception):
    """控えに関する異常（★迷ったら記録しない★）。"""


def _empty() -> dict:
    return {"schema_version": SCHEMA, "machines": {}}


def canon_slug(slug: str) -> str:
    """★控えの鍵にする形★（2026-08-26・実際に踏んだ穴）

    移行前に公開した機種は、サイト側のslugが `pw_...` のままで、
    控えの保存側は DMM のURLから `dmm_<機種ID>` を作る。
    ★そのままだと保存と参照の鍵が一致せず、2AIの結論が永久に効かない★。
    ★増やせない対応表（slug_binding）で必ず同じ形に寄せる★。
    """
    s = str(slug or "").strip()
    if not s:
        return s
    try:
        import slug_binding as _sb
    except Exception:                     # noqa: BLE001
        return s
    return _sb.LEGACY_BINDINGS.get(s, s)


def load() -> dict:
    """控えを読む。★壊れていたら黙って「無い」ことにしない★"""
    if not os.path.exists(STORE):
        return _empty()
    try:
        with open(STORE, encoding="utf-8") as f:
            got = json.load(f)
    except Exception as e:              # noqa: BLE001
        raise CacheError(f"控えを読めません（直すまで使いません）: {e}")
    if not isinstance(got, dict) or got.get("schema_version") != SCHEMA:
        raise CacheError(
            f"控えの版が違います（{got.get('schema_version') if isinstance(got, dict) else '?'}）")
    if not isinstance(got.get("machines"), dict):
        raise CacheError("控えの中身が壊れています（machines が組ではありません）")
    # ★読むときも中身を確かめる★（2026-08-14・依頼190のP1）
    #   書くときだけ検査していたので、手で書き足したレコードが
    #   **根拠も判断者も無いまま信用される**経路があった。
    # ★出どころの登録簿は1回だけ読む★（レコードごとに読み直さない）
    _reg = None
    if any(rows for rows in got["machines"].values()):
        import source_lineage as _sl
        try:
            _reg = _sl.load_registry()
        except Exception as e:              # noqa: BLE001
            raise CacheError(f"出典の登録簿を読めません（控えを使いません）: {e}")
    for slug, rows in got["machines"].items():
        if not isinstance(rows, list):
            raise CacheError(f"控えが壊れています（{slug} が並びではありません）")
        for rec in rows:
            _check_record(slug, rec, _reg)
    return got


_DATE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}$")
# ★導入前の新台はDMMも月までしか書かない★（2026-08-21・台帳#424）
#   日精度を必須にしていたので、**2AIが決めても控えられなかった**
#   （2026-08-20に実際に発生: dmm_5073 は "2026-11"／"2026年11月上旬予定"）。
#   導入日は「機種を取り違えないための鍵」なので、★DMMが持っている精度で鍵にする★。
#   ★粗くしたぶんは、突き合わせも同じ精度で行う★（下の _release_same）。
_MONTH = __import__("re").compile(r"^\d{4}-\d{2}$")












def _check_record(slug: str, rec, reg=None, require_final: bool = True) -> None:
    """1件ぶんの控えを確かめる（★読むときも書くときも同じ物差し★）。

    ★★見るのは「意味を読まなくても分かること」だけ★★
      （2026-09-17・運営者の指示。正本＝
        `_design/material_decision_2ai_2026-09-17.md`）
      ①結論が2つのどちらか
      ②どのページの採否かを名乗っている（対象ページ）
      ③判断者に claude と codex がそろっている
      ④理由が書いてある（★中身は読まない★＝それは意味の判断）
      ⑤証拠が1件以上・対象ページの証拠がちょうど1件
      ⑥引用の長さと写しの量（★規約の前提★）
      ⑦判断したときの本文の指紋がある

    ★引用が本当にそのページに在るかは `verify_evidence` が取り直して見る★
      （ここは形だけ。言うだけで通さない歯止めはあちら側）

    ★★v3の控えは使わない★★＝`proof_profile` を持つものは、
      機械が意味を判定していた前提で作られている。移行の分岐を作らず、
      2AIが決め直す（fail-closed）。
    """
    if not isinstance(rec, dict):
        raise CacheError(f"控えが壊れています（{slug}）")
    if rec.get("proof_profile") is not None:
        raise CacheError(
            f"古い形の控えです（{slug}）／★{SCHEMA} では使いません★"
            "＝2AIが決め直します")
    if rec.get("verdict") not in VERDICTS:
        raise CacheError(f"控えの結論が不正です（{slug}）: {rec.get('verdict')!r}")
    # ★★対象ページは、根拠から推測せず、控え自身が名乗る★★
    #   （2026-08-17・台帳#390）前は「根拠のURLのどれか」＝
    #   **2ページあればどちらも対象になり得た**。
    #   「使わない」側も対象URLで引くので、結論によらず必須。
    tgt = str(rec.get("target_url") or "")
    if not tgt.startswith("https://"):
        raise CacheError(f"控えに target_url がありません（{slug}）"
                         "／★どのページの採否かを名乗らせます★")
    for k in ("why", "decided_at"):
        if not str(rec.get(k) or "").strip():
            raise CacheError(f"控えに「{k}」がありません（{slug}）")
    if len(str(rec.get("why") or "").strip()) < MIN_WHY:
        raise CacheError(f"控えの理由が短すぎます（{slug}）"
                         f"／★{MIN_WHY}文字以上★（中身は機械が読みません）")
    # ★★判断者は claude と codex の2つ★★（2026-08-14・依頼193のP2）
    #   以前は ["foo", "bar"] のような**架空のID2つ**でも「違う2者」だった。
    #   ★これは本人確認ではない★＝手で書くことは防げない。
    # ★判断者の名簿は「AIごとの判断」から導く★（2026-09-17・罠④）
    #   ★直す前★＝`agreed_by` にも同じ検査を書いていたので、
    #   ★どちらを壊してももう片方が拾い、守りを壊しても試験が赤くならなかった★。
    by = rec.get("agreed_by")
    if not isinstance(by, list) or sorted(
            str(x).strip().casefold() for x in by) != sorted(
                str(k).strip().casefold()
                for k in (rec.get("decisions") or {})):
        raise CacheError(
            f"控えの判断者が、AIごとの判断と合いません（{slug}）: {by!r}")
    # ★★AIごとの判断が、そろって一致していること★★
    #   （2026-09-17・Codexの指摘1）
    #   ★名前が2つ並んでいるだけでは「2つ動いた」と言えない★＝
    #   片方が実行されていなくても同じ形になる（配線切れに気づけない）。
    _dec = rec.get("decisions")
    if not isinstance(_dec, dict):
        raise CacheError(
            f"控えにAIごとの判断（decisions）がありません（{slug}）"
            "／★名前が2つ並んでいるだけでは、2つ動いた証拠になりません★")
    if {str(k).strip().casefold() for k in _dec} != set(ALLOWED_AGREERS):
        raise CacheError(
            f"控えの判断がそろっていません（{slug}）: {sorted(_dec)}"
            f"／★{sorted(ALLOWED_AGREERS)} の両方が要ります★")
    for _k, _d in _dec.items():
        if not isinstance(_d, dict) or _d.get("verdict") not in VERDICTS:
            raise CacheError(f"{_k} の判断が不正です（{slug}）")
        if len(str(_d.get("why") or "").strip()) < MIN_WHY:
            raise CacheError(f"{_k} の理由が短すぎます（{slug}）")
        if _d["verdict"] != rec["verdict"]:
            raise CacheError(
                f"{_k} の結論が、控えの結論と違います（{slug}）"
                "／★一致したときだけ控えます★")
        if str(_d.get("body_sha256") or "") != str(rec.get("body_sha256") or ""):
            raise CacheError(
                f"{_k} が読んだ本文が、控えた本文と違います（{slug}）"
                "／★同じページを読んだ上での一致でなければ意味がありません★")
    # ★★判断したときの本文の指紋★★（2026-09-17）
    #   ★これが控えの有効期限★＝ページが書き換わったら効かない。
    #   題の分解をやめた代わりに、ここが「同じものを見ている」保証になる。
    _sha = str(rec.get("body_sha256") or "")
    if len(_sha) != 64 or any(c not in "0123456789abcdef" for c in _sha):
        raise CacheError(
            f"控えに本文の指紋（body_sha256）がありません（{slug}）"
            "／★判断したときと同じ本文かを確かめられません★")
    ev = rec.get("evidence")
    if not isinstance(ev, list) or not ev:
        raise CacheError(f"控えに根拠がありません（{slug}）")
    if len(ev) > MAX_EVIDENCE:
        raise CacheError(f"控えの根拠が多すぎます（{slug}）: {len(ev)}件"
                         f"／★{MAX_EVIDENCE}件までです（写しは最小限に）★")
    for e in ev:
        if not isinstance(e, dict):
            raise CacheError(f"控えの根拠が組ではありません（{slug}）")
        if not str(e.get("url") or "").startswith(("http://", "https://")):
            raise CacheError(f"控えの根拠のURLが不正です（{slug}）: {e.get('url')!r}")
        q1 = " ".join(str(e.get("quote") or "").split())
        if len(q1) < MIN_QUOTE:
            raise CacheError(f"控えの逐語引用が短すぎます（{slug}）")
        # ★長すぎる引用は受け取らない★（2026-08-17・Codex依頼229の指摘3）
        #   規約の判断は「事実の欄の写しにとどまる」という前提で出ている。
        if len(q1) > MAX_QUOTE:
            raise CacheError(
                f"控えの逐語引用が長すぎます（{slug}）: {len(q1)}字"
                f"／★{MAX_QUOTE}字までです＝事実の欄だけを写します"
                "（記事本文や表は写しません）★")
        if e.get("kind") not in KINDS:
            raise CacheError(f"控えの根拠の種類が不正です（{slug}）: {e.get('kind')!r}")
    # ★★写しの量の制限は、結論によらず先に効かせる★★
    #   （2026-08-17・Codex依頼230の指摘2）
    #   前は「使う」と決めた控えの検査の中にあったので、
    #   **「使わない」の控えなら同じ名鑑から4件まで写せた**＝
    #   規約について運営者が許した保存の範囲を、結論を変えるだけで越えられた。
    import source_lineage as _sl
    try:
        per = [_sl.vote_key_of_url(str(e.get("url")), reg) for e in ev]
    except Exception as e:                 # noqa: BLE001
        raise CacheError(f"根拠の出どころを数えられません（{slug}）: {e}")
    if len(set(per)) != len(per):
        raise CacheError(f"同じ名鑑から2件以上の引用を控えています（{slug}）"
                         "／★1つの名鑑につき1件までです★")
    # ★★対象ページの根拠はちょうど1件★★（結論によらず・形の検査）
    #   ＝2AIが任意のページを「対象」と名乗れると、採否の対象がぼやける。
    _tgt_ev = [e for e in ev if url_key(e.get("url")) == url_key(tgt)]
    if len(_tgt_ev) != 1:
        raise CacheError(
            f"対象ページの根拠がちょうど1件ではありません（{slug}）: "
            f"{len(_tgt_ev)}件／★採否を決めたページ自身の観測を1件入れます★")
    # ★守りの範囲を控え自身に書かせる★（2026-08-17・Codex依頼228の指摘5）
    #   これを読み落として「会社が同じと確かめた」と誤読されないようにする。
    if rec.get("basis_scope") != BASIS_SCOPE:
        raise CacheError(f"控えの basis_scope は {BASIS_SCOPE} です（{slug}）: "
                         f"{rec.get('basis_scope')!r}")
    # ★到達先は必須★（2026-08-17・Codex依頼236の厚み）
    #   使うときに「記録時と同じ所へ着いたか」を比べる相手なので、
    #   無い控えは比べようがない＝受け取らない（fail-closed）。
    _ofu_ok = str(rec.get("observed_final_url") or "").startswith("https://")
    if require_final and not _ofu_ok:
        raise CacheError(
            f"控えに observed_final_url がありません（{slug}）"
            "／★記録した時にどこへ着いたかが無いと、使うときに比べられません★")


def save(got: dict) -> None:
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = f"{STORE}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(got, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STORE)


def key_of(seen: str) -> str:
    """メーカー欄の表記から、比べるための芯を作る。"""
    return _ci.normalize_core(str(seen or "")).replace("株式会社", "")


def url_key(url: str) -> str:
    """★URLを比べるときの唯一のそろえ方★（2026-08-17・Codex依頼235の指摘1）

    ★穴だったところ★＝同じURLを、ある所では末尾の `/` を外して比べ、
      別の所（到達先の表）では**生の文字列**を鍵にしていた。
      すると「控えは `/` 付き・実行時は `/` 無し」というだけで
      **到達先の表を引けず、照合が丸ごと飛んで**しまい、
      そのまま「使う」に到達した。
    ★そろえ方を1か所にする★＝比べる時は必ずこれを通す。

    ★★www の有無もそろえる★★（2026-08-24・Codexの14回目）
      ★直す前は末尾の `/` だけ★だったので、
      `https://nana-press.com/...` → `https://www.nana-press.com/...` の
      ような**正常な転送**でも「別のページへ飛ばされた」と見なし、
      ★新台タスクが止まった★（＝守りを厳しくして本番を止める型）。
      ★別のページへの転送は今までどおり止まる★＝道筋が違えば鍵も違う。
    """
    import urllib.parse as _up
    t = str(url or "")
    try:
        sp = _up.urlsplit(t)
        host = (sp.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if not host:
            return t
        # ★既定のポートは書いても書かなくても同じ★（2026-08-24・Codexの15回目）
        #   もう一方の正規化器（machine_sources.url_key）と扱いをそろえる。
        _default = {"http": 80, "https": 443}.get((sp.scheme or "").lower())
        port = f":{sp.port}" if sp.port and sp.port != _default else ""
        # ★道筋の末尾だけを落とす★（2026-08-24・Codexの16回目）
        #   ★URL全体の末尾で落としていた★ので、
        #   `/x/?a=1` と `/x?a=1` が別ページ扱いになり、
        #   正常な末尾スラッシュの転送でも止まった。
        path = sp.path.rstrip("/")
        return _up.urlunsplit((sp.scheme, host + port, path, sp.query, ""))
    except Exception:                                        # noqa: BLE001
        return t


# ★★救える落ち方の表は、まるごと廃止しました★★（2026-09-17・運営者の指示）
#   ＞ もうさ、機械的に見るのやめたら？
#   ＞ シンプルに行かない？ 検索する項目だけ決めてさ、2AIで拾ってくるだけ。
#
#   ★何があったか★＝「どの落ち方なら控えで救ってよいか」を機械が決める形で、
#   名前の表 →（表に無い落ち方が黙って外れる）→ 落ち方の並び →
#   （並びの照合・証明の型・必要な引用欄…）と**場合分けが増え続けた**。
#   ★歯止めとして働いていなかった★＝正しい答えを止め（モンハンライズが8晩）、
#   見ていたのは「題名の字が同じ形に分解できるか」であって、
#   本当に知りたい「このページはこの機種のことを書いているか」ではなかった。
#
#   ★いまの形★＝そのページを材料に使うかは**2AIが本文を読んで決める**。
#   機械は「引用が実在するか」「同じ本文か」「判断者が2つか」だけを見る。

def verdict_for(slug: str, store=None, fetch=None, material_url: str = "",
                runtime_page=None):
    """この機種について、★このページを★使うと決めてあるか（無ければ None）。

    ★★鍵は (機種・対象ページ)★★（2026-08-17・台帳#390）
      v2は (機種・期待する社・名鑑の表記) で引いていたが、
        ①メーカーの食い違いが無い場合は鍵を作れない
        ②根拠が2ページあるとどちらも対象になり得る
      ので、対象ページを鍵にした。
      ★「使わない」も必ず対象ページで引く★（表記だけで流用しない）

    ★★効く条件は2つだけ★★（2026-09-17・運営者の指示）
      ①判断したときと**同じ本文**を見ていること（指紋）
      ②根拠の引用が、いま取り直した本文にも**そのまま在る**こと
      ★証明の型・落ち方・メーカー欄の一致は見ない★＝
      それは機械が意味を判定していた層で、正しい答えを止めていた。

    ★「材料に使う」として使う時だけ、根拠が実在するか確かめ直す★
      （2026-08-14・依頼192のP1）控えは手で書き足せるただのファイルなので、
      形だけ整った偽の根拠で `ACCEPT_MATERIAL` を作れてしまう。
      使う直前に取り直せば、それが通らない。
      ★取れない・引用が見つからないなら「決めていない」と同じ扱い★
      （None を返す＝もう一度2AIへ回る。fail-closed）
      `REJECT_MATERIAL` は「使わない」側なので取り直さない。
    """
    if not slug or not material_url:
        return None
    got = store if store is not None else load()
    _t = url_key(material_url)
    # ★鍵は必ず同じ形にそろえる★（移行した機種は pw_ のまま来る）
    for rec in (got.get("machines") or {}).get(canon_slug(slug)) or []:
        if url_key(rec.get("target_url")) != _t:
            continue
        v = rec.get("verdict")
        if v != "ACCEPT_MATERIAL":
            return v                       # ★使わない側は対象が合えば返す★
        # ★★①判断したときと同じ本文か★★（2026-09-17）
        #   ★渡されなければ答えない（fail-closed）★＝
        #   「確かめた本文」と「あとで読む本文」を必ず同じ物にする。
        if runtime_page is None:
            return None
        if str(rec.get("body_sha256") or "") != _page_sha(runtime_page):
            return None                    # ★ページが書き換わったら効かない★
        # ★②根拠が今もそのページに実在するか（毎回取り直す）★
        try:
            finals = verify_evidence(rec.get("evidence") or [], fetch, rec=rec,
                                     runtime_target=str(material_url),
                                     runtime_page=runtime_page)
        except CacheError:
            return None
        # ★★③いま取ってきた到達先が、控えた対象ページと同じか★★
        #   （2026-08-17・Codex依頼234の指摘1）
        #   ★穴だったところ★＝記録するときは転送を拒否し、到達先も残して
        #   いたのに、**使うときは一度も比べていなかった**。
        #   同じ名鑑の中の**別の機種ページ**へ転送されると、
        #   転送先も機種ページの形に合うので転送自体は止まらず、
        #   4つの読取器が**転送先の本文から値を読む**経路が残っていた。
        _fin = str((finals or {}).get(_t, ""))
        # ★到達先が取れないときは拒否する★（2026-08-17・依頼235）
        #   前は空なら素通りだったので、鍵のそろえ方がずれた瞬間に
        #   照合が丸ごと飛んだ。取れない＝確かめていない。
        if not _fin or _fin != _t:
            return None
        _ofu = url_key(rec.get("observed_final_url"))
        if _ofu and _fin != _ofu:
            return None
        return v
    return None


def _host_of(url: str) -> str:
    """URLからホストを取り出す（★文字列の前方一致で見ない★）。"""
    import urllib.parse
    return (urllib.parse.urlsplit(str(url or "")).hostname or "").lower()


# ★official_hosts() は削除しました★（2026-08-17・依頼228／運営者判断）
#   メーカー公式の会社関係ページを機械が取りに行く仕組みでした。
#   会社の関係は maker-catalogs.json の maker_relation_group（承認が要る
#   リポジトリ内のファイル）で見ます。根拠は同ファイルの _group_why に、
#   日本遊技機工業組合のグループ会社一覧を**人が読んだ記録**として残しています。
#   ★止めずに消す★＝残すと「まだ生きている」と誤読され、実際に誤報しました。


def directory_of(host: str) -> dict:
    """★そのホストの名鑑の設定★（ACTIVEのものだけ・無ければ例外）"""
    import directory_index as _di
    import safe_json as _sj
    import source_lineage as _sl
    h = str(host or "").lower()
    try:
        reg = _sl.load_registry()
        cats = _sj.read_json(_di.CATALOGS, expect=dict).get("directories") or {}
    except Exception as e:                 # noqa: BLE001
        raise CacheError(f"名鑑の登録簿を読めません（根拠を確かめられません）: {e}")
    pubs = {pid: p for pid, p in (reg.get("publishers") or {}).items()
            if p.get("status") == "ACTIVE"}
    for c in cats.values():
        if not isinstance(c, dict) or c.get("status") != "ACTIVE":
            continue
        p = pubs.get(str(c.get("publisher_id") or ""))
        for ch in (p or {}).get("canonical_hosts") or []:
            if str(ch).strip().lower() == h:
                return c
    raise CacheError(f"名鑑として登録されていないサイトです: {host}"
                     "／★観測の根拠は登録済みの名鑑から採ります★")




def check_evidence_source(e: dict) -> None:
    """★根拠のURLが、その種類にふさわしい出どころか★（依頼192のP1）

    directory_observation … 登録済みの名鑑の、★機種ページ★

    ★ホストだけでは足りない★（2026-08-17・Codex依頼228の指摘4）
      v1は「登録済みの名鑑のホストか」しか見ていなかったので、
      同じ名鑑の**一覧ページ・特集記事・別機種のページ**でも通った。
      名鑑ごとに決めてある機種ページの形（machine_page_pattern）まで見る。
    """
    url = str(e.get("url") or "")
    if not url.startswith("https://"):
        raise CacheError(f"根拠は https のページだけです: {url}")
    host = _host_of(url)
    if not host:
        raise CacheError(f"根拠のURLからホストを取れません: {url}")
    kind = e.get("kind")
    if kind != "directory_observation":
        raise CacheError(f"根拠の種類が不正です: {kind!r}"
                         f"／★いまの種類は {'/'.join(KINDS)} だけです★")
    # ★「登録済みの発行者」ではなく「登録済みの名鑑」に限る★
    #   （2026-08-14・依頼193のP2）source-registry には解析サイトや
    #   メーカー公式も ACTIVE で載っているので、それだけで見ると
    #   **名鑑でないページを「名鑑での観測」として渡せた**＝役割の分離が崩れる。
    conf = directory_of(host)
    pat = str(conf.get("machine_page_pattern") or "")
    if not pat:
        # ★決めていない名鑑は使わない★（fail-closed）
        raise CacheError(
            f"この名鑑には機種ページの形が決めてありません: {host}"
            "／★directory-catalogs.json の machine_page_pattern に書きます★")
    import re as _re
    if not _re.match(pat, url):
        raise CacheError(
            f"その名鑑の機種ページではありません: {url}"
            f"／★形: {pat}★（一覧・特集・別機種のページは根拠にしません）")






def verify_evidence(evidence: list, fetch=None, rec=None,
                    runtime_target: str = "",
                    runtime_page=None) -> dict:
    """★根拠の逐語引用が、本当にそのページにあるか確かめる★

    ★なぜ要るか（2026-08-14・依頼190のP1）★
      形（URLらしい文字列・8文字以上の引用）だけを見ていたので、
      **URLも引用も「言うだけ」で通った**。
      当サイトの原則は「言うだけでは通さない＝そのページに実在する逐語を
      根拠に出させ、機械が確かめる」なので、ここが抜けていると
      根拠の無い MATCH を控えられてしまう。

    ★取れなければ控えない★（fail-closed）＝取得できない・引用が見つからない
      ときは例外にする。「たぶん合っている」で通さない。
    """
    if fetch is None:
        import new_machine_watch as _w

        def fetch(u):
            # ★何のために取りに行くかを名乗る★（2026-08-17）
            #   8/16に「名乗らなければ通さない」形にしたとき、ここを
            #   直し忘れて**必ず例外**になっていた（実際に新台が止まった）。
            #   用途＝メーカーの同定（逐語引用が本当にそのページにあるか確かめる）
            with _w.fetching("maker_identity"):
                return _w._get(u)
    import new_machine_watch as _w
    finals = {}          # ★URLごとの到達先★（対象ページの転送を見るため）
    # ★錨が弱い（芯の一致だけ）で救ったページ★（2026-09-15・台帳#675）
    _weak_anchor = set()
    _tgt_key = url_key((rec or {}).get("target_url"))
    for e in evidence:
        url = str(e.get("url") or "")
        # ★★対象ページは「実行時に見つかったURL」で取りに行く★★
        #   （2026-08-17・Codex依頼236）
        #   ★穴だったところ★＝控えに保存したURLだけを取り直していたので、
        #   「保存した / 付きは正常・実行時の / 無しだけ別機種へ転送」
        #   という**非対称**を一度も見ていなかった。
        #   許可証には実行時のURLが入り、読取器はそちらを取りに行くので、
        #   別機種の本文が材料に入り得た。
        #   ★確かめる相手と、あとで読む相手を同じにする★
        _use_page = None
        if runtime_target and _tgt_key and url_key(url) == _tgt_key:
            url = runtime_target
            e = dict(e, url=url)
            # ★★確かめる本文と、あとで読む本文を同じ物にする★★
            #   （2026-08-17・台帳#393）取ってきた器が渡されていれば、
            #   ここで取り直さずその本文を確かめる。
            #   ★取り直すと「確かめた本文」と「読む本文」が別物になり得る★
            _use_page = runtime_page
        # ★出どころの検査は必ず通す★（2026-09-17）＝
        #   ★直す前は expected があるときだけ★だったので、
        #   メーカーを期待しない呼び方では規約の検査ごと飛んでいた。
        check_evidence_source(e)
        if _use_page is not None:
            html = _use_page.cleaned_html
            _w.LAST_FINAL_URL["url"] = _use_page.final_url
            _pre_cleaned = True
        else:
            _pre_cleaned = False
            try:
                html = fetch(url)
            except Exception as ex:        # noqa: BLE001
                raise CacheError(f"根拠のページを取得できません（{url}）: "
                                 f"{str(ex)[:80]}")
        # ★転送された先も同じ許可の中か見る★（依頼192のP1）
        #   許可したURLから許可外へ飛ばされたら、それは別の出どころ。
        # ★ホストが同じでも必ず見る★（2026-08-14・依頼193のP2）
        #   以前は「ホストが変わったときだけ」だったので、
        #   同じ社の https → http という降格が素通りした
        #   （＝そのあとの本文は通信経路で書き換えられうる）。
        fin = _w.LAST_FINAL_URL.get("url")
        # ★取れなかったものを「同じURLに着いた」ことにしない★
        #   （2026-08-17・依頼235）＝以前は `fin or url` と補っていたので、
        #   到達先を一度も観測できていなくても照合が通ってしまった。
        finals[url_key(url)] = url_key(fin) if fin else ""
        if fin:
            check_evidence_source(dict(e, url=fin))
        # ★★本体とまったく同じ下ごしらえをする★★
        #   （2026-08-17・Codex依頼231の指摘2）
        #   本体（model_code_lookup.lookup）は取ってきた直後に
        #   **投稿欄・AIがまとめた欄を箱ごと落として**から読む。
        #   控えの再確認はそれを通していなかったので、
        #   ★読者の書き込みに含まれる文字が根拠になり得た★。
        #   「本体と同じ物差し」と書きながら、実装が違っていた。
        #   ★落としきれないページは使わない★（fail-closed）
        if not _pre_cleaned:
            import user_area as _ua
            try:
                html = _ua.clean_html(html or "", url)
            except Exception as ex:        # noqa: BLE001
                raise CacheError(
                    f"投稿欄を落としきれないページです（{url}）: "
                    f"{str(ex)[:80]}")
        # ★★指紋と同じ物差しで探す★★（2026-09-18・台帳#696）
        #   ★「確かめた本文」と「引用を探す本文」は必ず同じもの★
        import user_area as _ua_rt
        body = _ua_rt.compare_text(html or "")
        q = " ".join(str(e.get("quote") or "").split())
        if q not in body:
            raise CacheError(
                f"根拠の逐語引用がそのページに見つかりません（{url}）: "
                f"{q[:40]}／★写した文だけを根拠にします★")
        # ★★そのページがその機種のページかは、2AIが読んで決める★★
        #   （2026-09-17・運営者の指示）
        #   ★ここにあった題の分解（page_is_machine）は外した★＝
        #   見ていたのは「題名の字が同じ形に分解できるか」であって、
        #   本当に知りたい「このページはこの機種のことを書いているか」
        #   ではなかった。実測で、題名を略しただけのページを落とし続け、
        #   モンハンライズが8晩止まっていた。
        #   ★メーカー欄の照合も同じ理由で外した★。
        #   ★機械がここで守るのは「引用がこの本文にそのまま在る」だけ★
        #   （すぐ上で確かめている）。
    return finals


def remember(slug: str, decisions, evidence: list,
             decided_at: str, target_url: str = "",
             store=None, fetch=None, runtime_page=None) -> dict:
    """2AIが決めた「そのページを材料に使うか」を控える。

    ★★AIごとの判断を別々に受け取る★★（2026-09-17・Codexの指摘1）
      `decisions` = {"claude": {"verdict": …, "why": …, "body_sha256": …},
                     "codex":  {…}}
      ★直す前★＝結論は1つで、`agreed_by` に名前を2つ並べるだけだった。
      ＝機械が確かめられるのは「名前が2つ書かれた」ことだけで、
      ★片方が実行されていなくても同じ形になった★
      （1AIの答えに --by claude,codex と書けば通る）。
      ★本人確認の話ではない★＝**実行漏れ・配線切れに気づけない**のが問題。
      ★両方の結論と、読んだ本文の指紋が一致したときだけ控える★

    ★★機械が確かめるのは、意味を読まなくても分かることだけ★★
      （2026-09-17・運営者の指示）
      ①結論が2つのどちらか ②どのページの採否かを名乗っている
      ③判断者が2つ ④理由が書いてある（中身は読まない）
      ⑤根拠の引用が、機械が取ってきたその本文に**そのまま在る**
      ⑥判断したときの本文の指紋を残す

    ★逐語引用は実際にそのページから取ってきて照合する★（依頼190のP1）
    ★どのページの採否かを名乗らせる★（2026-08-17・台帳#390。根拠から推測しない）
    """
    # ★★検査は1か所（_check_record）に寄せる★★（2026-09-17・罠③）
    #   ★ここで同じことを見ない★＝2か所に書くと、どちらを壊しても
    #   もう片方が拾うので、★守りを壊しても試験が赤くならない★
    #   （実測＝7件の壊し方がどれも捕まらなかった）。
    #   ★ここは「組み立てる」だけ★。合っているかは読むときと同じ物差しで見る。
    if not isinstance(decisions, dict):
        raise CacheError("AIごとの判断（decisions）が要ります")
    _norm = {}
    for k, d in decisions.items():
        if not isinstance(d, dict):
            raise CacheError(f"{k} の判断が組ではありません")
        _norm[str(k).strip().casefold()] = {
            "verdict": d.get("verdict"),
            "why": " ".join(str(d.get("why") or "").split())[:300],
            "body_sha256": str(d.get("body_sha256") or ""),
        }
    if not _norm:
        raise CacheError("AIごとの判断（decisions）が要ります")
    # ★代表の結論は、決まった順の先頭から取る★（食い違いは _check_record が見る）
    _head = _norm[sorted(_norm)[0]]
    verdict = _head["verdict"]
    why = " ／ ".join(f"{k}: {_norm[k]['why']}" for k in sorted(_norm))
    by = sorted(_norm)
    for k, v in (("slug", slug), ("target_url", target_url),
                 ("decided_at", decided_at)):
        if not str(v or "").strip():
            raise CacheError(f"「{k}」が要ります")
    if not isinstance(evidence, list) or not evidence:
        raise CacheError("根拠（URLと逐語引用）が要ります")
    for e in evidence:
        if not isinstance(e, dict):
            raise CacheError("根拠は組（辞書）で書きます")
    # ★★本文の指紋は、機械が自分で数える★★（呼ぶ側に名乗らせない）
    _sha = _page_sha(runtime_page) if runtime_page is not None else ""
    rec = {"target_url": target_url, "verdict": verdict, "why": why,
           "evidence": evidence, "agreed_by": by, "decided_at": decided_at,
           "body_sha256": _sha, "basis_scope": BASIS_SCOPE,
           # ★AIごとの判断をそのまま残す★（あとから「本当に2つ動いたか」を見る）
           "decisions": _norm}
    # ★書く前に、読むときと同じ物差しを通す★（順番を変えない）
    #   ★①形だけ先に見る★（形が違う控えのために外へ出ない）
    #   到達先はまだ取りに行っていないので、そこだけ後回しにする。
    _check_record(slug, rec, require_final=False)
    _finals = verify_evidence(evidence, fetch, rec,
                              runtime_target=str(target_url),
                              runtime_page=runtime_page)
    # ★最後に着いたURLも残す★（記録時と使用時で転送先が変わるのを防ぐ）
    #   ★根拠の「最後に取ったページ」ではなく、対象ページのぶんを見る★
    #   （2026-08-17。最初そこを間違え、根拠2件目の到達先と比べていた）
    _fin = str((_finals or {}).get(url_key(target_url), ""))
    if not _fin or _fin != url_key(target_url):
        raise CacheError(
            f"対象ページが転送されました（{target_url} → {_fin}）"
            "／★転送先を対象として控えるかは、2AIが決め直します★")
    rec["observed_final_url"] = _fin
    # ★③到達先まで入れて、読むときとまったく同じ物差しで見直す★
    _check_record(slug, rec)
    got = store if store is not None else load()
    rows = got.setdefault("machines", {}).setdefault(slug, [])
    _t = url_key(target_url)
    for i, old in enumerate(rows):
        if url_key(old.get("target_url")) == _t:
            rows[i] = rec                # ★同じページは上書き（増やさない）★
            break
    else:
        rows.append(rec)
    if store is None:
        save(got)
    return rec




def forget(slug: str, target_url: str, store=None) -> bool:
    """控えを消す（判断を取り消すとき）。★対象ページで指す★"""
    got = store if store is not None else load()
    rows = (got.get("machines") or {}).get(canon_slug(slug)) or []
    _t = url_key(target_url)
    left = [r for r in rows
            if url_key(r.get("target_url")) != _t]
    if len(left) == len(rows):
        return False
    if left:
        got["machines"][slug] = left
    else:
        got["machines"].pop(slug, None)
    if store is None:
        save(got)
    return True


# ---------------------------------------------------------------- selftest

# ★試験も本物の名鑑・本物の機種ページの形を使う★（2026-08-17・依頼228）
#   架空のホスト（x.test）では `directory_of` も `machine_page_pattern` も
#   通らない。**関所を素通りする偽物を使うと、関所の試験にならない**。
#   取ってくる役だけを差し替える（通信はしない）。
_MN = "L転生王女と天才令嬢の魔法革命"
_SEEN = "平和"
_REL = "2026-10-05"
_C = "https://chonborista.com/slot/orinpia-slot/264134/"    # 名鑑①の機種ページ
_N = "https://nana-press.com/kaiseki/machine/1233/"         # 名鑑②の機種ページ
_LIST = "https://chonborista.com/slot/orinpia-slot/"        # 一覧（機種ページでない）
_KIT = "https://www.kitadenshi.co.jp/company/"              # 名鑑ではない登録先
# ★引用は「事実の欄の写し」まで★（規約の前提をコードで守る）
_QC = f"機種名 {_MN} メーカー {_SEEN} 導入日 2026年10月5日"
_QN = f"機種名 {_MN} メーカー {_SEEN} 導入日 2026/10/5"
_SHA = "a" * 64


def _rec(**kw) -> dict:
    """試験用の、正しい形の控え1件（★v4＝2AIが決めた採否★）。"""
    _w0 = "2つのAIが本文を読んで同じ機種のページだと判断しました"
    base = {"target_url": _C, "verdict": "ACCEPT_MATERIAL",
            "why": _w0,
            "agreed_by": ["claude", "codex"],
            "decided_at": "2026-09-17", "basis_scope": BASIS_SCOPE,
            "body_sha256": _SHA,
            "observed_final_url": url_key(_C),
            "decisions": {
                "claude": {"verdict": "ACCEPT_MATERIAL", "why": _w0,
                           "body_sha256": _SHA},
                "codex": {"verdict": "ACCEPT_MATERIAL", "why": _w0,
                          "body_sha256": _SHA}},
            "evidence": [{"url": _C, "quote": _QC,
                          "kind": "directory_observation"}]}
    base.update(kw)
    return base


def _rec_sha(sha: str) -> dict:
    """★控えとAIごとの判断で、指紋をそろえて壊す★（試験用）"""
    r = _rec(body_sha256=sha)
    for d in r["decisions"].values():
        d["body_sha256"] = sha
    return r


def _bad_load() -> bool:
    """★手で書き足した控えを、読むときに弾けるか★（試験用）

    ★ここが v1 で足りなかったところ★＝書くときだけ検査していたので、
      手で書き足したレコードが根拠も判断者も無いまま信用される経路があった。
    """
    import copy
    bads = [
        {"verdict": "ACCEPT_MATERIAL"},                # 形をなしていない
        _rec(agreed_by=["claude"]),                    # 1人だけ
        _rec(agreed_by=["claude", "claude"]),          # 同じ人を2回
        _rec(agreed_by=["claude", "gemini"]),          # 知らない判断者
        _rec(evidence=[]),                             # 根拠なし
        _rec(why="短い"),                              # 理由が短い
        _rec(why=""),                                  # 理由なし
        _rec(target_url=""),                           # どのページの採否か不明
        _rec(decided_at=""),                           # いつ決めたか不明
        # ★指紋の形だけが違う材料★（2026-09-17・罠④）
        #   ★AIごとの指紋もそろえて壊す★＝そろえないと「AIの指紋と
        #   控えの指紋が違う」ほうが先に断り、形の検査を一度も通らない。
        _rec_sha(""),                                  # 本文の指紋が無い
        _rec_sha("zz"),                                # 指紋の形が違う
        _rec(basis_scope="whatever"),                  # 守りの範囲を偽る
        # ★★古い形の控えは使わない★★（2026-09-17・v4）
        _rec(proof_profile="maker_field"),
        # ★写しが長すぎる／多すぎる／同じ名鑑から2件★（依頼229の指摘3）
        _rec(evidence=[{"url": _C, "quote": _QC + "。" + "解析情報。" * 30,
                        "kind": "directory_observation"}]),
        _rec(evidence=[{"url": _C, "quote": _QC,
                        "kind": "directory_observation"},
                       {"url": "https://chonborista.com/slot/x/2/",
                        "quote": _QC, "kind": "directory_observation"}]),
        _rec(evidence=[{"url": _C, "quote": _QC,
                        "kind": "directory_observation"}] * 5),
        _rec(evidence=[{"url": _C, "quote": "短い",
                        "kind": "directory_observation"}]),
        _rec(evidence=[{"url": _C, "quote": _QC, "kind": "なにか"}]),
        # ★対象ページの根拠がちょうど1件でない★
        _rec(evidence=[{"url": _N, "quote": _QN,
                        "kind": "directory_observation"}]),
    ]
    reg = None
    try:
        import source_lineage as _sl
        reg = _sl.load_registry()
    except Exception:                      # noqa: BLE001
        return False
    for bad in bads:
        g = copy.deepcopy(bad)
        try:
            _check_record("dmm_5086", g, reg)
            return False                   # ★通ってしまった＝不合格★
        except CacheError:
            pass
    return True


def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅ " if cond else "❌ ") + name)

    st = _empty()

    # ★名鑑のページと同じ形で作る★（2026-08-17・Codex依頼229の指摘2）
    #   ★関所を通る形の偽物でなければ、関所の試験にならない★
    #   ①投稿欄（hyouka / commentlist）と本体（entry）
    #     … 投稿欄を箱ごと落とす処理（user_area.clean_html）が
    #       「その形のページか」を確かめるので、無いと必ず例外になる
    def _page(maker=_SEEN, day="2026年10月5日", name=_MN, title=None,
              posts="読者の書き込みです", user_area=True):
        """★`user_area=False` は「投稿欄が無い名鑑」の形★（2026-09-21）

        ★なぜ要るか★＝掃除のあとの見張りを `clean_html` の中へ移したので、
        ★決まりごとが無いサイトのページに投稿欄の作りが入っていると止まる★
        （それが本来の狙い＝未知の箱にある書き込みを材料にしない）。
        なな徹は「投稿欄が無いことを実ページで確かめて記録した」サイトなので、
        ★試験の材料も、その形にそろえる★（本物と違う材料で採点しない・罠①）。
        """
        t0 = (title if title is not None
              else f"{name} スロット 新台 天井 解析 | ちょんぼりすた")
        ua = ('<a class="rating-btn">みんなの評価 (平均0)</a>'
              '<div id="hyouka">星の評価</div>'
              f'<ul class="commentlist"><li>{posts}</li></ul>'
              ) if user_area else ""
        return (f"<title>{t0}</title>" + ua
                + '<div id="entry">'
                + f"<div>機種名 {name}</div>"
                + f"<div>メーカー {maker}</div>"
                + f"<div>導入日 {day}</div>"
                + "</div>")

    _pages = {
        _C: _page(),
        # ★なな徹は投稿欄が無い★（決まりごとも無い＝掃除の対象外）
        _N: _page(day="2026/10/5", user_area=False),
        _LIST: _page(),
        _KIT: _page(user_area=False),
    }

    def _w_last(u):
        """取ってくる役が「最後に着いたURL」を控える（本物と同じ形）。"""
        import new_machine_watch as _w
        _w.LAST_FINAL_URL["url"] = u
        return u

    def _fetch(u):
        if u not in _pages:
            raise RuntimeError("404")
        _w_last(u)                         # ★転送なし＝最後のURLは自分自身★
        return _pages[u]

    import fetched_page as _fp

    def _pg(url=_C, html=None):
        """★本物の器で試す★（2026-09-18・台帳#696）

        ★手書きの偽物を置かない★＝指紋の作り方を変えた日に、
        偽物だけ古い作り方のまま残り、★試験は緑のまま本番が止まる★
        （実際に、この試験の偽物は全文の指紋を数えていた）。
        """
        import user_area as _ua
        raw = _pages[url] if html is None else html
        return _fp.FetchedPage(url, url, _ua.clean_html(raw, url))

    _P = _pg()

    def _ask(slug="dmm_5086", store=None, fetch=None, url=_C, page=None):
        """★本番と同じ渡し方で引く★（対象URLと、取ってきた器）"""
        return verdict_for(slug, st if store is None else store,
                           _fetch if fetch is None else fetch,
                           material_url=url,
                           runtime_page=_P if page is None else page)

    _WHY = "2つのAIが本文を読んで同じ機種のページだと判断しました"

    def _dec(verdict="ACCEPT_MATERIAL", sha=None, who=("claude", "codex"),
             why=None, only=None):
        """★AIごとの判断★（試験用。片方だけ・食い違いも作れる）"""
        out = {}
        for k in who:
            out[k] = {"verdict": verdict, "why": why or _WHY,
                      "body_sha256": sha if sha is not None else _P.text_sha256}
        if only:
            out.update(only)
        return out

    def _ok(**kw):
        base = dict(slug="dmm_5086", target_url=_C,
                    decisions=_dec(),
                    evidence=[{"url": _C, "quote": _QC,
                               "kind": "directory_observation"}],
                    decided_at="2026-09-17", store=st, fetch=_fetch,
                    runtime_page=_P)
        base.update(kw)
        try:
            remember(**base)
            return True
        except CacheError:
            return False

    # ─── ★★2AIが決めたことを控えられる★★（2026-09-17・運営者の指示） ───
    #   ＞ もうさ、機械的に見るのやめたら？
    #   ＞ シンプルに行かない？ 検索する項目だけ決めてさ、2AIで拾ってくるだけ。
    t("★★2AIが決めれば、名鑑1件でも控えられる★★"
      "（★直す前は独立2名鑑を証明に要求していた★）", _ok())
    t("　控えた結論を引ける", _ask() == "ACCEPT_MATERIAL")

    # ★★題が略称のページでも控えられる★★（2026-09-17・モンハンライズ）
    #   ★直す前★＝証拠ページに題の分解（page_is_machine）をかけていたので、
    #   「モンスターハンターライズ」を「モンハンライズ」と略した題のページが
    #   落ち、★2AIが決めても保存できず8晩止まっていた★。
    # ★なな徹のURLで使うので、投稿欄の無い形にする★（2026-09-21）
    _ABBR = _page(title="モンハンライズ 解析 | ちょんぼりすた",
                  user_area=False)
    _pages[_N] = _ABBR
    t("★★題が略称のページでも、2AIが決めれば控えられる★★"
      "（★これで8晩止まっていた＝モンハンライズ★）",
      _ok(target_url=_N, runtime_page=(_pgN := _pg(_N)),
          decisions=_dec(sha=_pgN.text_sha256),
          evidence=[{"url": _N, "quote": _QC,
                     "kind": "directory_observation"}]))
    # ★なな徹は投稿欄が無い形にそろえる★（2026-09-21・見張りを掃除側へ移した）
    _pages[_N] = _page(day="2026/10/5", user_area=False)

    # ─── ★★機械が守る線（意味を読まなくても分かること）★★ ───────────
    # ★★2AIが読んだ本文と、いま控える本文が同じか★★
    #   （2026-09-17・Codexの指摘2）
    #   ★直す前★＝控えるときに機械が改めて取った本文から指紋を作っていたので、
    #   ★2AIが読んだあと相手がページを書き換えても、引用さえ残っていれば
    #   書き換わった本文に許可が付いた★。
    t("★★2AIが読んだ本文と違うページには控えられない★★"
      "（★相手が書き換えたなら、読み直して決め直す★）",
      not _ok(decisions=_dec(sha="b" * 64)))
    t("　（対照）2AIが読んだ本文と同じなら控えられる", _ok())
    t("★★2AIが読んだ本文の指紋を名乗らないと控えられない★★"
      "（★名乗らないと「同じページを読んだ」と言えない★）",
      not _ok(decisions=_dec(sha="")))
    t("★★言うだけでは通さない★★"
      "＝引用がそのページに無ければ控えられない",
      not _ok(evidence=[{"url": _C, "quote": "どこにも書いていない文です",
                         "kind": "directory_observation"}]))
    t("★★判断が2つそろわないと控えられない★★"
      "（★名前を並べるだけでは「2つ動いた」と言えない★"
      "＝片方の実行漏れ・配線切れをここで止める）",
      not _ok(decisions=_dec(who=("claude",))))
    t("　知らない判断者は受け取らない",
      not _ok(decisions=_dec(who=("claude", "gemini"))))
    t("★★2つのAIの結論が割れていたら控えない★★"
      "（★割れたら詰めて決め直す★）",
      not _ok(decisions=_dec(only={"codex": {
          "verdict": "REJECT_MATERIAL", "why": _WHY,
          "body_sha256": _P.text_sha256}})))
    t("★★2つのAIが違う本文を読んでいたら控えない★★"
      "（★同じページを読んだ上での一致でなければ意味がない★）",
      not _ok(decisions=_dec(only={"codex": {
          "verdict": "ACCEPT_MATERIAL", "why": _WHY,
          "body_sha256": "c" * 64}})))
    t("★★理由が無い・短いと控えられない★★（中身は機械が読みません）",
      not _ok(decisions=_dec(why="短い")))
    t("★★名鑑の機種ページ以外は根拠にできない★★（規約）",
      not _ok(target_url=_LIST, runtime_page=(_pLIST := _pg(_LIST)),
              decisions=_dec(sha=_pLIST.text_sha256),
              evidence=[{"url": _LIST, "quote": _QC,
                         "kind": "directory_observation"}]))
    t("　名鑑でない登録先も根拠にできない",
      not _ok(target_url=_KIT, runtime_page=(_pKIT := _pg(_KIT)),
              decisions=_dec(sha=_pKIT.text_sha256),
              evidence=[{"url": _KIT, "quote": _QC,
                         "kind": "directory_observation"}]))
    t("★★写しは最小限★★（長すぎる引用は控えない・規約の前提）",
      not _ok(evidence=[{"url": _C, "quote": _QC + "。" + "解析。" * 40,
                         "kind": "directory_observation"}]))
    t("　同じ名鑑から2件以上は控えない",
      not _ok(evidence=[{"url": _C, "quote": _QC,
                         "kind": "directory_observation"},
                        {"url": "https://chonborista.com/slot/x/2/",
                         "quote": _QC, "kind": "directory_observation"}]))
    t("★★対象ページ自身の観測が根拠に要る★★",
      not _ok(evidence=[{"url": _N, "quote": _QN,
                         "kind": "directory_observation"}]))

    # ─── ★★控えが効く条件★★ ─────────────────────────────────
    t("★★本文が書き換わったら控えは効かない★★"
      "（★題の分解をやめた代わりに、ここが「同じものを見ている」保証★）",
      _ask(page=_pg(_C, _page(day="2026年11月2日"))) is None)
    t("★★取ってきた器を渡さなければ答えない★★（fail-closed）",
      verdict_for("dmm_5086", st, _fetch, material_url=_C) is None)
    t("★★対象ページを渡さなければ答えない★★（fail-closed）",
      verdict_for("dmm_5086", st, _fetch, runtime_page=_P) is None)
    t("★★控えの根拠に入っていないページには効かない★★",
      _ask(url="https://chonborista.com/slot/orinpia-slot/999999/") is None)

    # ★★引用が本文から消えていたら効かせない★★
    #   ★器を渡すと取り直さないのが正しい★＝「確かめる本文」と
    #   「あとで読む本文」を必ず同じ物にするため（同じ型の穴を5回踏んでいる）。
    #   ＝★取り直しの代わりに、渡された本文そのものと照合する★。
    #   ★指紋の検査に助けられないようにする★（罠④）＝
    #   引用の無い本文で、指紋は合っている控えを作って試す。
    _NOQ = _page(name="別の機種", day="2026年12月1日")
    _PNOQ = _pg(_C, _NOQ)
    _st_noq = _empty()
    _rec_noq = _rec(body_sha256=_PNOQ.text_sha256)
    for _d0 in _rec_noq["decisions"].values():
        _d0["body_sha256"] = _PNOQ.text_sha256
    _st_noq["machines"]["dmm_5086"] = [_rec_noq]
    t("★★引用がその本文に無ければ効かせない★★"
      "（控えは手で書き足せるファイルなので、使う時に照合し直す）",
      verdict_for("dmm_5086", _st_noq, _fetch, material_url=_C,
                  runtime_page=_PNOQ) is None)

    # ★★転送されたら効かせない★★（同じ名鑑の別機種へ飛ばされる形）
    #   ★取ってきた器が「どこへ着いたか」を持っている★ので、それを見る。
    _PMOV = _pg()
    _PMOV.final_url = _N                   # ★別のページへ着いた★
    t("★★取ってきた先が対象ページと違えば効かせない★★"
      "（同じ名鑑の別機種へ飛ばされると、転送そのものは止まらない）",
      _ask(page=_PMOV) is None)

    # ─── ★★「使わない」は落ち方によらず効き続ける★★ ───────────────
    st2 = _empty()
    t("　「使わない」も控えられる",
      _ok(store=st2, decisions=_dec(verdict="REJECT_MATERIAL")))
    t("★★「使わない」は対象が合えば返る★★（取り直さない）",
      verdict_for("dmm_5086", st2, _fetch, material_url=_C,
                  runtime_page=_P) == "REJECT_MATERIAL")

    # ─── ★★読むときも書くときと同じ物差し★★ ─────────────────────
    t("★★手で書き足した控えは、読むときに弾く★★"
      "（★書く口だけ厳しくしても、読む口が緩ければ意味がない★）",
      _bad_load())

    # ★★古い形の控えは使わない★★（2026-09-17・v4へ移行）
    _st3 = _empty()
    _st3["machines"]["dmm_5086"] = [_rec(proof_profile="maker_field")]
    t("★★古い形（証明の型を持つ）の控えは使わない★★"
      "（★移行の分岐を作らない＝2AIが決め直す・fail-closed★）",
      _bad3(_st3))

    # ─── ★★控えの置き場と鍵★★ ───────────────────────────────
    # ★★URLの鍵は、意味の変わらない書き方の違いを吸収する★★
    #   ★既定のポートをそろえないと★＝正常な転送（https → https:443）で
    #   「別のページへ着いた」と読み、控えが効かなくなる（Codex15回目）。
    t("★既定のポート付きでも同じページとして引ける★",
      url_key("https://chonborista.com:443/slot/x/1/")
      == url_key("https://chonborista.com/slot/x/1/"))
    t("　（対照）別のポートは別のページ",
      url_key("https://chonborista.com:8443/slot/x/1/")
      != url_key("https://chonborista.com/slot/x/1/"))
    t("★slugの書き方が違っても同じ機種として引ける★",
      canon_slug("pw_5086") == canon_slug("pw_5086"))
    _st4 = _empty()
    _ok(store=_st4)
    _ok(store=_st4, decisions=_dec(why="2つのAIがもう一度読んで同じ結論です"))
    t("★★同じページの控えは増やさず上書きする★★",
      len(_st4["machines"]["dmm_5086"]) == 1)
    t("　消せる", forget("dmm_5086", _C, _st4)
      and not _st4["machines"].get("dmm_5086"))

    ng = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗: " + str(ng))
    return 1 if ng else 0


def _bad3(store) -> bool:
    """★古い形の控えが使われないこと★（試験用）"""
    import source_lineage as _sl
    try:
        _reg = _sl.load_registry()
    except Exception:                      # noqa: BLE001
        return False
    for slug, rows in (store.get("machines") or {}).items():
        for rec in rows:
            try:
                _check_record(slug, rec, _reg)
            except CacheError:
                return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="機種ごとのメーカー同一性の控え")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--forget", action="store_true")
    # ★DMMの機種ページだけ★（2026-08-17・依頼228／台帳#376でP-WORLDは停止）
    ap.add_argument("--machine-url", dest="machine_url",
                    help="DMMの機種ページ https://p-town.dmm.com/machines/<ID>"
                         "（slug・導入日はここから決める）")
    ap.add_argument("--machine-name", dest="machine_name", default="",
                    help="カレンダーに載っている機種名（DMMの見出しと照合する）")
    # ★どのページの採否かを名乗らせる★（2026-08-17・台帳#390）
    ap.add_argument("--target-url", dest="target_url", default="",
                    help="採否を決める名鑑の機種ページURL")
    # ★★--proof-profile / --expected / --seen / --reason-code は廃止★★
    #   （2026-09-17・運営者の指示）＝どれも「機械に意味を判定させる」ための
    #   引数だった。そのページを使うかは2AIが本文を読んで決める。
    ap.add_argument("--verdict", choices=VERDICTS,
                    help="（--decision を使うときは要りません）")
    ap.add_argument("--decision", action="append", default=[],
                    dest="decision",
                    help="★AIごとの判断★ claude|<結論>|<本文の指紋>|<理由>"
                         "／★2つそろって、結論も読んだ本文も一致したときだけ"
                         "控えます（片方だけの実行・配線切れをここで止めます）★")
    ap.add_argument("--decision-why-file", action="append", default=[],
                    dest="decision_why_file",
                    help="★理由をファイルで渡す★ claude|<理由のファイル>"
                         "（鉄則1c。--decision の4つ目の代わり）")
    ap.add_argument("--why")
    # ★自由文はファイルでも渡せる★（2026-08-14）
    #   長い理由をコマンドに書くと、中の記号がシェルに実行される
    #   （2026-08-08に実際に発生）。台帳・メールと同じ受け取り方にそろえる。
    ap.add_argument("--why-file", dest="why_file", default="",
                    help="理由を書いたファイル（--why と同時には使えません）")
    ap.add_argument("--by", help="判断した者（カンマ区切り・2つ以上）")
    ap.add_argument("--evidence", action="append", default=[],
                    help="URL|逐語引用|種類（種類: "
                         + "/".join(KINDS) + "）")
    ap.add_argument("--body-sha256", dest="body_sha256", default="",
                    help="★2AIが読んだ本文の指紋★"
                         "（問いに書いてある値をそのまま渡します。"
                         "いま取ってきた本文と違えば控えません＝"
                         "相手がページを書き換えたので読み直し）")
    ap.add_argument("--at", help="決めた日（省略時は今日）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    # ★ファイル渡しは台帳と同じ受け取り方を使う★（置き場の制限つき）
    try:
        import open_issues as _oi
        a.why = _oi._read_text_arg(a.why or "", a.why_file, "why")
    except SystemExit as e:
        print(str(e))
        return 2
    try:
        if a.list:
            got = load()
            for slug, rows in sorted((got.get("machines") or {}).items()):
                print(f"■ {slug}")
                for r in rows:
                    print(f"   {r.get('target_url', '')}  {r['verdict']}")
                    print(f"      {r.get('why', '')[:80]}")
                    print(f"      {'/'.join(r.get('agreed_by') or [])}"
                          f"（{r.get('decided_at')}）")
            return 0
        if not (a.record or a.forget):
            ap.print_help()
            return 0
        # ★slugは自己申告させない★＝DMMの機種URLから決める
        #   ★形を厳しく見る★（2026-08-17・Codex依頼228の指摘2）
        #   前は共通の slug_from_url に任せていたので、DMM/P-WORLD以外の
        #   URLは**末尾がそのままslug**になり、
        #   `https://example.com/dmm_5086/` のような外部URLでも
        #   狙ったslugの控えを作れた。
        import re as _re
        if not a.machine_url:
            print("★--machine-url が要ります（slugをそこから決めます）★")
            return 1
        m = _re.match(r"^https://p-town\.dmm\.com/machines/(\d+)/?$",
                      str(a.machine_url).strip())
        if not m:
            print(f"★DMMの機種ページのURLだけです: {a.machine_url}★"
                  "／形: https://p-town.dmm.com/machines/<機種ID>")
            return 1
        slug = "dmm_" + m.group(1)
        if a.forget:
            # ★v3は対象ページで指す★（2026-08-17・Codex依頼234の指摘4）
            #   旧いまま (expected, seen) を渡していたので、3番目の引数に
            #   文字列が入り、控えを取り消す手順そのものが壊れていた。
            if not a.target_url:
                print("★--target-url が要ります（どのページの控えを消すか）★")
                return 1
            ok = forget(slug, a.target_url)
            print("消しました" if ok else "その控えはありません")
            return 0 if ok else 1
        ev = []
        for spec in a.evidence:
            parts = [x.strip() for x in str(spec).split("|")]
            if len(parts) != 3:
                print("★--evidence は『URL|逐語引用|種類』です★"
                      "／役割・導入日・メーカー欄は2AIが読んで決めるので"
                      "控えません")
                return 1
            ev.append({"url": parts[0], "quote": parts[1], "kind": parts[2]})
        import datetime
        # ★★対象ページは、機械がその場で取ってくる★★（2026-09-17）
        #   ★呼ぶ側に本文の指紋を名乗らせない★＝名乗らせると、
        #   2AIが読んだ本文と控える本文が別物になる道が残る。
        import fetched_page as _fp
        try:
            _pg = _fp.fetch(a.target_url, "maker_identity")
        except Exception as e:             # noqa: BLE001
            print(f"★対象ページを取れません: {str(e)[:120]}★")
            return 1
        # ★★AIごとの判断を組み立てる★★（2026-09-17・Codexの指摘1）
        _why_files = {}
        for spec in a.decision_why_file:
            parts = [x.strip() for x in str(spec).split("|", 1)]
            if len(parts) != 2:
                print("★--decision-why-file は『AI名|理由のファイル』です★")
                return 1
            try:
                _why_files[parts[0].casefold()] = io.open(
                    parts[1], encoding="utf-8").read().strip()
            except Exception as e:         # noqa: BLE001
                print(f"★理由のファイルを読めません: {str(e)[:120]}★")
                return 1
        _decisions = {}
        for spec in a.decision:
            parts = [x.strip() for x in str(spec).split("|", 3)]
            if len(parts) < 3:
                print("★--decision は『AI名|結論|本文の指紋|理由』です★"
                      "（理由は --decision-why-file でも渡せます）")
                return 1
            _who = parts[0].casefold()
            _decisions[_who] = {
                "verdict": parts[1],
                "body_sha256": parts[2],
                "why": (parts[3] if len(parts) >= 4 and parts[3]
                        else _why_files.get(_who, "")),
            }
        if not _decisions:
            print("★--decision が要ります★"
                  "＝AIごとの判断を別々に渡します"
                  "（名前を並べるだけでは「2つ動いた」と言えません）")
            return 1
        rec = remember(slug, _decisions, ev,
                       a.at or datetime.date.today().isoformat(),
                       target_url=a.target_url, runtime_page=_pg)
        print(json.dumps({"state": "RECORDED", "slug": slug, **rec},
                         ensure_ascii=False)[:300])
        return 0
    except CacheError as e:
        print("★" + str(e) + "★")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

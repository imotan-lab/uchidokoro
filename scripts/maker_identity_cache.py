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

★使ってよいのは RELATED のときだけ★（2026-08-17・Codex依頼228の指摘1）
  ①名簿で一致（MATCH）              … そのまま使う（この器は要らない）
  ②関係のありそうな社（RELATED）    … **この器を見る**
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
SCHEMA = "maker-identity-cache/v3"
VERDICTS = ("ACCEPT_MATERIAL", "REJECT_MATERIAL")
# ★何をもって「使ってよい」と言えるか★＝原因ごとに必要な根拠が違う。
#   ★説明ではなく、読むときにも厳格に確かめる判別子★（Codexの指示）
PROOF_PROFILES = {
    # 名鑑のメーカー欄がDMMと違う（v2からの継続）
    #   → ★独立した名鑑2つ★の観測が要る
    "maker_field": {"min_directories": 2, "needs_maker": True},
    # 名鑑の題・見出しが略称で、機種の同定に落ちる
    #   → ★対象ページ自身＋DMM★でよい（2件目の名鑑は別途正規の同定を通る）
    #   → ただし★メーカー欄が読めてDMMと一致していること★が必須
    #     （題もメーカーも食い違うページを、弱い側で通さないため）
    "title_name_core_mismatch": {"min_directories": 1, "needs_maker": True},
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
# ★材料に使うと決めるのに要る、独立した名鑑の数★
MIN_DIRECTORIES = 2


class CacheError(Exception):
    """控えに関する異常（★迷ったら記録しない★）。"""


def _empty() -> dict:
    return {"schema_version": SCHEMA, "machines": {}}


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


def _release_key_ok(v) -> bool:
    """控えの鍵として使える形か（日まで／月まで）。"""
    s = str(v or "")
    return bool(_DATE.match(s) or _MONTH.match(s))


def _release_same(a, b) -> bool:
    """控えの導入日と、いまDMMで確かめた導入日が同じか。

    ★片方が月までなら、月で比べる★（粗いほうに合わせる）。
      控え "2026-11" ／ いま "2026-11-07" → 同じ扱い
      控え "2026-11" ／ いま "2026-12-01" → 違う
    ★どちらかが空なら「同じ」とは言わない★
    """
    x, y = str(a or ""), str(b or "")
    if not x or not y:
        return False
    if _MONTH.match(x) or _MONTH.match(y):
        return x[:7] == y[:7]
    return x == y


def _has_core(haystack: str, needle: str) -> bool:
    """★表記ゆれをならしてから、含まれているか見る★

    機種名は名鑑ごとに空白・記号の入れ方が違う（「L転生王女と天才令嬢の魔法革命」
    ／「L転生王女と天才令嬢の 魔法革命」）ので、同定に使っている芯の作り方
    （claim_identity）にそろえる。★ここで新しい正規表現を書かない★
    """
    n = _ci.normalize_core(str(needle or ""))
    if not n:
        return False
    return n in _ci.normalize_core(str(haystack or ""))


def _check_record(slug: str, rec, reg=None, require_final: bool = True) -> None:
    """1件ぶんの控えを確かめる（★読むときも書くときも同じ物差し★）。"""
    if not isinstance(rec, dict):
        raise CacheError(f"控えが壊れています（{slug}）")
    if rec.get("verdict") not in VERDICTS:
        raise CacheError(f"控えの結論が不正です（{slug}）: {rec.get('verdict')!r}")
    for k in ("expected", "seen", "why", "decided_at"):
        if not str(rec.get(k) or "").strip():
            raise CacheError(f"控えに「{k}」がありません（{slug}）")
    by = rec.get("agreed_by")
    # ★表記ゆれを「違う2者」にしない★（2026-08-14・依頼192のP2）
    #   ["codex", " codex"] や ["codex", None] が2者として通っていた。
    if not isinstance(by, list) or not all(
            isinstance(x, str) and x.strip() for x in by):
        raise CacheError(f"控えの判断者が不正です（{slug}）: {by!r}")
    ids = {x.strip().casefold() for x in by}
    if not ids <= ALLOWED_AGREERS:
        raise CacheError(
            f"控えに知らない判断者がいます（{slug}）: "
            f"{sorted(ids - ALLOWED_AGREERS)}／★{sorted(ALLOWED_AGREERS)} だけです★")
    if len(ids) < 2:
        raise CacheError(f"控えの判断者が足りません（{slug}）: {by!r}"
                         "／★違う2者で決めます★")
    ev = rec.get("evidence")
    if not isinstance(ev, list) or not ev:
        raise CacheError(f"控えに根拠がありません（{slug}）")
    if len(ev) > MAX_EVIDENCE:
        raise CacheError(f"控えの根拠が多すぎます（{slug}）: {len(ev)}件"
                         f"／★{MAX_EVIDENCE}件までです（写しは最小限に）★")
    kinds = set()
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
                f"／★{MAX_QUOTE}字までです＝機種名・メーカー欄・導入日の"
                "欄だけを写します（記事本文や表は写しません）★")
        if e.get("kind") not in KINDS:
            raise CacheError(f"控えの根拠の種類が不正です（{slug}）: {e.get('kind')!r}")
        kinds.add(e.get("kind"))
    # ★★写しの量の制限は、結論によらず先に効かせる★★
    #   （2026-08-17・Codex依頼230の指摘2）
    #   前はここが「使う」と決めた控えの検査の中にあったので、
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
    # ★★対象ページは、根拠から推測せず、控え自身が名乗る★★
    #   （2026-08-17・台帳#390／Codex依頼233の指摘1）
    #   前は「根拠のURLのどれか」＝**2ページあればどちらも対象になり得た**。
    #   「使わない」側も対象URLで引くので、結論によらず必須。
    tgt = str(rec.get("target_url") or "")
    if not tgt.startswith("https://"):
        raise CacheError(f"控えに target_url がありません（{slug}）"
                         "／★どのページの採否かを名乗らせます★")
    prof = rec.get("proof_profile")
    if prof not in PROOF_PROFILES:
        raise CacheError(f"控えの proof_profile が不正です（{slug}）: {prof!r}"
                         f"／★{'/'.join(sorted(PROOF_PROFILES))} のどれか★")
    if rec["verdict"] != "ACCEPT_MATERIAL":
        return
    # ★★弱い型で救えるのは、メーカー欄が本当に一致している時だけ★★
    #   （2026-08-17・Codex依頼233の指摘2）
    #   題の不一致は「弱い証明」なので、メーカー欄まで食い違うページを
    #   ここで通すと**メーカーの関門を丸ごと迂回**できてしまう。
    #   ★RELATED（関係のある社）も救わない★＝それは maker_field の話。
    if prof == "title_name_core_mismatch":
        import model_code_lookup as _mcl1
        _owners = _mcl1._maker_core_owners(key_of(rec.get("seen")))
        if str(rec.get("expected") or "") not in _owners:
            raise CacheError(
                f"題の不一致で救えるのは、メーカー欄が名簿で一致する時だけです"
                f"（{slug}）: 期待 {rec.get('expected')!r}／"
                f"名鑑「{rec.get('seen')}」→ {sorted(_owners) or '（不明）'}"
                "／★関係のある社・不明・別の社は、この弱い型では救いません★")
    # ---------------- ここから下は「材料に使う」と決めた控えだけの検査 ----------
    # ★対象ページ自身が根拠に入っていること★（対象と根拠の取り違えを防ぐ）
    if url_key(tgt) not in {url_key(e.get("url")) for e in ev}:
        raise CacheError(
            f"対象ページが根拠に入っていません（{slug}）: {tgt}"
            "／★採否を決めたページ自身の観測を根拠に入れます★")
    # ★①どの機種のページかを、控え自身が名乗る★（2026-08-17・依頼228の指摘2）
    #   v1は「登録済みの名鑑のホストである」ことしか見ていなかったので、
    #   **同じ名鑑の別機種ページ・関連記事・同名別メーカー機のページ**でも
    #   通った。機種名と導入日を控えに持たせ、根拠がその機種を指すか見る。
    for k in ("machine_name", "release_date"):
        if not str(rec.get(k) or "").strip():
            raise CacheError(f"控えに「{k}」がありません（{slug}）"
                             "／★どの機種のページかを名乗らせます★")
    if not _release_key_ok(str(rec.get("release_date"))):
        raise CacheError(f"控えの導入日は YYYY-MM-DD か YYYY-MM で書きます（{slug}）: "
                         f"{rec.get('release_date')!r}")
    # ★②逐語引用そのものに、機種名とメーカー欄が入っていること★
    #   ページのどこかにあるだけでは足りない（別機種の欄でも通ってしまう）。
    for e in ev:
        q = str(e.get("quote") or "")
        if not _has_core(q, str(rec.get("machine_name"))):
            raise CacheError(
                f"根拠の逐語引用に機種名が入っていません（{slug}）: "
                f"{q[:40]}／★その機種のページだと示す引用にします★")
        if not _has_core(q, str(rec.get("seen"))):
            raise CacheError(
                f"根拠の逐語引用にメーカー欄の表記が入っていません（{slug}）: "
                f"{q[:40]}／★「{rec.get('seen')}」を含む引用にします★")
        # ★導入日も同じ引用の中に入れる★（2026-08-17・Codex依頼231の判断）
        #   ★なぜ「ページのどこかにある」ではだめか★＝更新日・関連記事・
        #   別の説明の中の日付でも通ってしまい、その日付が
        #   **この機種のもの**だと言えない。同じ引用に入っていれば、
        #   機種名・メーカー欄・導入日が同じ場所にあると確かめられる。
        #   ★実データで収まることを確かめてから入れた★（2026-08-17）＝
        #   ちょんぼりすた52字・なな徹48字（上限120字）。
        _days = date_forms(str(rec.get("release_date")))
        if not _days:
            # ★説明文で落ちない★（2026-08-22・台帳#454）
            #   ここへ来るのは鍵の形が想定外のときだけ。
            #   ★異常終了させると「拒否」ではなく「壊れた」に見える★ので、
            #   理由の分かる断り方にする。
            raise CacheError(
                f"控えの導入日の形が分かりません（{slug}）: "
                f"{rec.get('release_date')!r}／★YYYY-MM-DD か YYYY-MM で書きます★")
        if not any(d in q for d in _days):
            raise CacheError(
                f"根拠の逐語引用に導入日が入っていません（{slug}）: "
                f"{q[:40]}／★{_days[0]} などを含む引用にします★")
    # ★③独立した名鑑が2つ以上★（2026-08-17・依頼228）
    #   ★票の数は source_lineage.independent() だけで決める★
    #   （自前で len() すると共同制作の組をまとめ忘れる＝監査39が見張る）
    keys = set(per)     # ★1つの名鑑につき1件は上で確かめ済み★
    # ★必要な名鑑の数は「なぜ機械が決められなかったか」で変わる★
    #   maker_field              … 名鑑どうしの一致が要るので2つ
    #   title_name_core_mismatch … そのページ自身＋DMMで足りるので1つ
    #     （★2件目の名鑑は別途、正規の同定を通っている★）
    _need = PROOF_PROFILES[prof]["min_directories"]
    if _sl.independent(keys, reg) < _need:
        raise CacheError(
            f"「{prof}」で材料に使うと決めるには独立した名鑑が{_need}つ要ります"
            f"（{slug}）: いまは {_sl.independent(keys, reg)}")
    # ★④守りの範囲を控え自身に書かせる★（2026-08-17・Codex依頼228の指摘5）
    #   これを読み落として「会社が同じと確かめた」と誤読されないようにする。
    if rec.get("basis_scope") != BASIS_SCOPE:
        raise CacheError(f"控えの basis_scope は {BASIS_SCOPE} です（{slug}）: "
                         f"{rec.get('basis_scope')!r}")
    if rec.get("relationship_verified") is not False:
        raise CacheError(
            f"控えの relationship_verified は false です（{slug}）"
            "／★会社の関係は機械で確かめていません★")
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
    t = str(url or "").rstrip("/")
    try:
        sp = _up.urlsplit(t)
        host = (sp.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if not host:
            return t
        port = f":{sp.port}" if sp.port else ""
        return _up.urlunsplit((sp.scheme, host + port, sp.path, sp.query,
                               ""))
    except Exception:                                        # noqa: BLE001
        return t


def verdict_for(slug: str, expected: str = "", seen: str = "", store=None,
                fetch=None, material_url: str = "",
                machine_name: str = "", release_date: str = "",
                want_profile: str = "", runtime_page=None):
    """この機種について、★このページを★使うと決めてあるか（無ければ None）。

    ★★鍵は (機種・対象ページ) の2つ★★（2026-08-17・台帳#390／Codex依頼233）
      v2は (機種・期待する社・名鑑の表記) で引いていました。しかし
        ①メーカーの食い違いが無い場合（題が略称、など）は鍵を作れない
        ②根拠が2ページあると、そのどちらも対象になり得る
      ので、対象ページを鍵にしました。
      ★「使わない」も必ず対象ページで引きます★（表記だけで流用しない）

    ★★「使う」と答えるには、対象そのものと結び付いていること★★
      （2026-08-17・Codex依頼229の指摘1）
      ・控えの `target_url` が `material_url` と一致する
      ・控えの `machine_name` / `release_date` が、DMMで確かめた値と一致する
      ・控えの `proof_profile` が、呼ぶ側が求めている証明の型と一致する
        （★題の不一致で作った控えを、メーカーの食い違いに流用させない★）
      ・（メーカーの食い違いで作った控えなら）期待する社・名鑑の表記も一致する
      ★どれか1つでも渡されていなければ答えません★（fail-closed）

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
    for rec in (got.get("machines") or {}).get(slug) or []:
        if url_key(rec.get("target_url")) != _t:
            continue
        v = rec.get("verdict")
        if v != "ACCEPT_MATERIAL":
            return v                       # ★使わない側は対象が合えば返す★
        # ★①求めている証明の型と一致するか★
        if not want_profile or rec.get("proof_profile") != want_profile:
            return None
        # ★②メーカーの食い違いで決めた控えなら、その組も一致すること★
        if PROOF_PROFILES[want_profile].get("needs_maker") \
                and rec.get("expected") is not None \
                and str(rec.get("expected") or ""):
            if rec.get("expected") != expected \
                    or key_of(rec.get("seen")) != key_of(seen):
                return None
        # ★③控えが名乗る機種が、DMMで確かめた機種と同じか★
        if not machine_name or not release_date:
            return None
        if not _has_core(str(rec.get("machine_name") or ""), machine_name) \
                or not _release_same(rec.get("release_date"), release_date):
            return None
        # ★④根拠が今もそのページに実在するか（毎回取り直す）★
        try:
            finals = verify_evidence(rec.get("evidence") or [], fetch,
                                     expected, rec,
                                     runtime_target=str(material_url),
                                     runtime_page=runtime_page)
        except CacheError:
            return None
        # ★★⑤いま取ってきた到達先が、控えた対象ページと同じか★★
        #   （2026-08-17・Codex依頼234の指摘1）
        #   ★穴だったところ★＝記録するときは転送を拒否し、到達先も残して
        #   いたのに、**使うときは一度も比べていなかった**。
        #   同じ名鑑の中の**別の機種ページ**へ転送されると、
        #   転送先も機種ページの形に合うので転送自体は止まらず、
        #   名前・メーカー・日付・引用がそこにも在れば「使う」を返し、
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


def directory_hosts() -> set:
    """★名鑑として登録されているホスト★（directory-catalogs.json の ACTIVE）

    ★新しい名簿は作らない★＝すでにある名鑑の登録簿から導く。
    """
    import directory_index as _di
    import safe_json as _sj
    import source_lineage as _sl
    # ★読めないときも CacheError にする★（2026-08-14・依頼194のP2）
    #   ここで別の例外が出ると、verdict_for が拾えず**新台の処理ごと落ちる**。
    #   守りたいのは「根拠を確かめられないなら使わない」であって、
    #   その晩の処理を全部止めることではない。
    try:
        reg = _sl.load_registry()
        pubs = {pid: p for pid, p in (reg.get("publishers") or {}).items()
                if p.get("status") == "ACTIVE"}
        cats = _sj.read_json(_di.CATALOGS, expect=dict).get("directories") or {}
    except CacheError:
        raise
    except Exception as e:                 # noqa: BLE001
        raise CacheError(f"名鑑の登録簿を読めません（根拠を確かめられません）: {e}")
    out = set()
    for c in cats.values():
        if not isinstance(c, dict) or c.get("status") != "ACTIVE":
            continue
        p = pubs.get(str(c.get("publisher_id") or ""))
        for h in (p or {}).get("canonical_hosts") or []:
            if str(h).strip():
                out.add(str(h).strip().lower())
    if not out:
        raise CacheError("名鑑の登録簿が空です（根拠を確かめられません）")
    return out


def check_evidence_source(e: dict, expected: str) -> None:
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


def date_forms(iso: str) -> list:
    """★同じ1つの日付を、よくある書き方に直すだけ★（2026-08-17・依頼228）

    ★これは「サイトごとの場合分け」ではない★＝相手の作りを読むのではなく、
      **こちらが持っている1つの日付**を標準的な書式で並べるだけ。
      実データ: ちょんぼりすた「2026年10月5日」／なな徹「2026/10/5」

    ★★月までしか分からない導入日も扱う★★（2026-08-22・台帳#454）
      ★直す前に起きていたこと★＝
        `date_forms("2026-11")` が **空の配列**を返していた。
        `any(d in q for d in [])` は必ず偽なので、
        ★どんな逐語を出しても照合に通らない★。
        しかも通らなかったときの説明文が `_days[0]` を読むので
        **IndexError で異常終了**し、機械にも人にも理由が伝わらなかった。
        ＝導入前の新台は控えを作れず、dmm_5073 は **13回** 空振りした。

      ★なぜ月で通してよいか★（同じ引用の中で他も見ているため）
        この照合は「逐語引用に導入日が入っているか」の1つで、
        **同じ引用の中に機種名の芯とメーカー欄の表記も要求**している。
        月まで粗くしても、★別機種の行が通るには「この機種の名前」が
        同じ引用に入っていないといけない★ので、実質的に弱くならない。

      ★日まで分かるときは今までどおり日で見る★（粗くするのは月精度の鍵のときだけ）。
      ★月の書き方は日つきの引用にも含まれる★＝
        「2026/11」は「導入日 2026/11/2」の中にそのまま現れるので、
        名鑑が日まで書いていても通る（実データで確認）。
    """
    v = str(iso or "")
    if _DATE.match(v):
        y, mo, d = v[:4], int(v[5:7]), int(v[8:10])
        out = []
        for sep in ("/", ".", "-"):
            out.append(f"{y}{sep}{mo}{sep}{d}")
            out.append(f"{y}{sep}{mo:02d}{sep}{d:02d}")
        out.append(f"{y}年{mo}月{d}日")
        out.append(f"{y}年{mo:02d}月{d:02d}日")
        return out
    if _MONTH.match(v):
        y, mo = v[:4], int(v[5:7])
        out = []
        for sep in ("/", ".", "-"):
            # ★★桁を詰めない書き方には区切りを付ける★★（2026-08-22・作った直後に発見）
            #   ★付けないと何が起きるか★＝
            #     「2026/1」は **「2026/12/1」の中にそのまま現れる**。
            #     ＝1月の鍵が10月・11月・12月の引用に当たってしまう
            #     （実データで再現。2〜9月は次の桁が無いので起きない）。
            #   区切りを付ければ「2026/1/」は「2026/12/1」に現れない。
            #   名鑑が日まで書く形（なな徹「2026/11/2」）はこれで拾える。
            out.append(f"{y}{sep}{mo}{sep}")
            out.append(f"{y}{sep}{mo:02d}{sep}")
            out.append(f"{y}{sep}{mo:02d}")     # 桁を詰めた形は前方一致の心配がない
        # 「2026年11月上旬予定」のような書き方（★月の字が区切りになる★）
        out.append(f"{y}年{mo}月")
        out.append(f"{y}年{mo:02d}月")
        return out
    return []


def verify_evidence(evidence: list, fetch=None, expected: str = "",
                    rec=None, runtime_target: str = "",
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
        if expected:
            check_evidence_source(e, expected)
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
        if expected and fin:
            check_evidence_source(dict(e, url=fin), expected)
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
        body = " ".join(_w._visible_text(html or "").split())
        q = " ".join(str(e.get("quote") or "").split())
        if q not in body:
            raise CacheError(
                f"根拠の逐語引用がそのページに見つかりません（{url}）: "
                f"{q[:40]}／★写した文だけを根拠にします★")
        # ★★そのページが本当にこの機種のページか、本体と同じ物差しで見る★★
        #   （2026-08-17・Codex依頼230）
        #   ★穴だったところ★＝材料になるページ自身は本体の同定（identity_ok）を
        #   通るが、**控えの2件目以降の根拠には同じ検査が無かった**。
        #   別機種のページの「関連機種」欄に対象名・メーカー・日付が並んでいれば、
        #   独立2名鑑の1票になり得た。
        #   ★名鑑ごとの新しい読み取りは書かない★＝本体が使う page_is_machine を通す。
        mn = str((rec or {}).get("machine_name") or "")
        if mn:
            import model_code_lookup as _mcl0
            # ★本体と同じ厳しさで呼ぶ★（2026-08-17・Codex依頼231の指摘2）
            #   本体は strict_all_tail=True と、そのメーカーの通称を渡している。
            #   既定値のまま呼ぶと**未知の版名が付いたページ**が通り得た。
            _ok_id, _why_id = _mcl0.page_is_machine(
                html or "", mn, strict_all_tail=True,
                extra_tail_ok=(_mcl0.maker_brand_cores(expected)
                               if expected else None))
            # ★★救う対象そのものを、同じ検査で拒否していた★★
            #   （2026-08-17・Codex依頼234の指摘2）
            #   `title_name_core_mismatch` は**題が合わないページを救う**ための
            #   型なのに、ここで厳格な同定をかけ直していたので
            #   **控えを作れず、許可証が永久に生まれなかった**（機能しない）。
            #   ★対象ページだけ、その型に合った確かめ方をする★
            #     ①落ち方が厳密に NAME_CORE_MISMATCH であること
            #       （別機種・規格違い・題が無い等は今までどおり拒否）
            #     ②投稿欄を落とした本文に、DMMの正式名が**完全一致**であること
            #     ③メーカー欄がDMMと一致すること（すぐ下の共通処理で見る）
            #   ★「本人だ」と決めるのは2AI★＝機械は上の3つを確かめるだけ。
            _prof = str((rec or {}).get("proof_profile") or "")
            _is_target = (url_key(url)
                          == url_key((rec or {}).get("target_url")))
            if not _ok_id and _prof == "title_name_core_mismatch" and _is_target:
                if _why_id != "NAME_CORE_MISMATCH":
                    raise CacheError(
                        f"題の不一致以外の理由で落ちています（{url}）: "
                        f"{str(_why_id)[:60]}／★この型で救えるのは題だけです★")
                if str(mn).strip() not in body:
                    raise CacheError(
                        f"本文にDMMの正式名がそのままありません（{url}）"
                        "／★略称の題を救うには、正式名が本文にあることが要ります★")
            elif not _ok_id:
                raise CacheError(
                    f"そのページはこの機種のページではありません（{url}）: "
                    f"{str(_why_id)[:60]}")
        # ★★メーカー欄そのものを取り出して比べる★★
        #   （2026-08-17・Codex依頼229の指摘2）
        #   前は「seen という文字がページのどこかにあるか」しか見ていなかった。
        #   それだと、メーカー欄は別の社なのに本文のどこかに「平和」と
        #   書いてあるページでも「平和表記の2件目」に数えられた。
        #   ★新しい読み取りを書かない★＝名鑑のメーカー欄を読む役は
        #   model_code_lookup.extract_maker_name にあるので、そこを通す。
        seen = str((rec or {}).get("seen") or "")
        if seen:
            import model_code_lookup as _mcl
            mk = _mcl.extract_maker_name(html or "")
            if not mk:
                raise CacheError(
                    f"そのページのメーカー欄を読めません（{url}）"
                    "／★読めないものを「確かめた」ことにしません★")
            if key_of(mk) != key_of(seen):
                raise CacheError(
                    f"そのページのメーカー欄が控えと違います（{url}）: "
                    f"ページ「{mk[:20]}」／控え「{seen[:20]}」")
        # ★導入日は「引用の中に入っていること」で見る★
        #   （2026-08-17・Codex依頼231の判断で、ページ本文のどこか、をやめた）
        #   引用そのものに機種名・メーカー欄・導入日が入っていることは
        #   `_check_record` が確かめ、その引用がページに実在することは
        #   すぐ上で確かめている。だからここに別の日付検査は要らない。
    return finals


def remember(slug: str, expected: str, seen: str, verdict: str,
             why: str, by: list, evidence: list, decided_at: str,
             machine_name: str = "", release_date: str = "",
             target_url: str = "", proof_profile: str = "maker_field",
             store=None, fetch=None) -> dict:
    """結論を控える。★根拠が無ければ受け取らない★

    ★逐語引用は実際にそのページから取ってきて照合する★（依頼190のP1）
    ★機種名と導入日はDMMの機種ページから取る★（呼ぶ側に名乗らせない）
    ★どのページの採否かを名乗らせる★（2026-08-17・台帳#390。根拠から推測しない）
    """
    if verdict not in VERDICTS:
        raise CacheError(f"結論は {'/'.join(VERDICTS)} のどちらかです: {verdict!r}")
    if proof_profile not in PROOF_PROFILES:
        raise CacheError(f"証明の型が不正です: {proof_profile!r}"
                         f"／★{'/'.join(sorted(PROOF_PROFILES))} のどれか★")
    for k, v in (("slug", slug), ("target_url", target_url),
                 ("expected", expected), ("seen", seen),
                 ("why", why), ("decided_at", decided_at)):
        if not str(v or "").strip():
            raise CacheError(f"「{k}」が要ります")
    if not isinstance(by, list) or len(by) < 2:
        raise CacheError("判断した者を2つ以上書きます（例: claude, codex）")
    if not isinstance(evidence, list) or not evidence:
        raise CacheError("根拠（URLと逐語引用）が要ります")
    for e in evidence:
        if not isinstance(e, dict):
            raise CacheError("根拠は組（辞書）で書きます")
        if not str(e.get("url") or "").strip():
            raise CacheError("根拠にURLが要ります")
        q = " ".join(str(e.get("quote") or "").split())
        if len(q) < MIN_QUOTE:
            raise CacheError(f"逐語引用は{MIN_QUOTE}文字以上で書きます: {q!r}")
        if e.get("kind") not in KINDS:
            raise CacheError(f"根拠の種類は {'/'.join(KINDS)} のどれかです: "
                             f"{e.get('kind')!r}")
    rec = {"target_url": target_url, "proof_profile": proof_profile,
           "expected": expected, "seen": seen, "verdict": verdict,
           "why": why, "evidence": evidence, "agreed_by": by,
           "decided_at": decided_at}
    if verdict == "ACCEPT_MATERIAL":
        rec.update({"machine_name": machine_name,
                    "release_date": release_date,
                    "basis_scope": BASIS_SCOPE,
                    "relationship_verified": False})
    # ★書く前に、読むときと同じ物差しを通す★（順番を変えない）
    #   先に形を確かめてから通信する＝形が違う控えのために外へ出ない。
    # ★①形だけ先に見る★（形が違う控えのために外へ出ない）
    #   到達先はまだ取りに行っていないので、そこだけ後回しにする。
    _check_record(slug, rec, require_final=False)
    _finals = verify_evidence(evidence, fetch, expected, rec,
                              runtime_target=str(target_url))
    # ★最後に着いたURLも残す★（記録時と使用時で転送先が変わるのを防ぐ）
    #   ★根拠の「最後に取ったページ」ではなく、対象ページのぶんを見る★
    #   （2026-08-17。最初そこを間違え、根拠2件目の到達先と比べていた＝自己試験が検知）
    _fin = str((_finals or {}).get(url_key(target_url), ""))
    if not _fin or _fin != url_key(target_url):
        raise CacheError(
            f"対象ページが転送されました（{target_url} → {_fin}）"
            "／★転送先を対象として控えるかは、2AIが決め直します★")
    if _fin:
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


def _fin_url() -> str:
    """取ってくる役が最後に着いたURL（試験で差し替えた時は空）。"""
    import new_machine_watch as _w
    return str((getattr(_w, "LAST_FINAL_URL", {}) or {}).get("url") or "")


def forget(slug: str, target_url: str, store=None) -> bool:
    """控えを消す（判断を取り消すとき）。★対象ページで指す★"""
    got = store if store is not None else load()
    rows = (got.get("machines") or {}).get(slug) or []
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
_EXPECTED = "olympia_estate"
_REL = "2026-10-05"
_C = "https://chonborista.com/slot/orinpia-slot/264134/"    # 名鑑①の機種ページ
_N = "https://nana-press.com/kaiseki/machine/1233/"         # 名鑑②の機種ページ
_LIST = "https://chonborista.com/slot/orinpia-slot/"        # 一覧（機種ページでない）
_KIT = "https://www.kitadenshi.co.jp/company/"              # 名鑑ではない登録先
# ★引用には機種名・メーカー欄・導入日の3つが入る★（2026-08-17・依頼231）
#   実在の2ページで52字・48字に収まることを確かめてから決めた形。
_QC = f"機種名 {_MN} メーカー {_SEEN} 導入日 2026年10月5日"
_QN = f"機種名 {_MN} メーカー {_SEEN} 導入日 2026/10/5"


def _rec(**kw) -> dict:
    """試験用の、正しい形の控え1件。"""
    base = {"target_url": _C, "proof_profile": "maker_field",
            "expected": _EXPECTED, "seen": _SEEN, "verdict": "ACCEPT_MATERIAL",
            "why": "理由", "agreed_by": ["claude", "codex"],
            "decided_at": "2026-08-17", "machine_name": _MN,
            "release_date": _REL, "basis_scope": BASIS_SCOPE,
            "relationship_verified": False,
            "evidence": [{"url": _C, "quote": _QC,
                          "kind": "directory_observation"},
                         {"url": _N, "quote": _QN,
                          "kind": "directory_observation"}]}
    base.update(kw)
    return base


def _bad_load() -> bool:
    """★手で書き足した控えを、読むときに弾けるか★（試験用）

    ★ここが v1 で足りなかったところ★＝書くときだけ検査していたので、
      手で書き足したレコードが根拠も判断者も無いまま信用される経路があった。
    """
    import copy
    bads = [
        {"verdict": "ACCEPT_MATERIAL", "expected": _EXPECTED, "seen": _SEEN},
        _rec(agreed_by=["claude"]),                    # 1人だけ
        _rec(agreed_by=["claude", "claude"]),          # 同じ人を2回
        _rec(evidence=[]),                             # 根拠なし
        _rec(evidence=[{"url": _C, "quote": _QC,
                        "kind": "directory_observation"}]),   # 名鑑1つだけ
        _rec(machine_name=""),                         # どの機種か名乗らない
        _rec(release_date=""),                         # 導入日を名乗らない
        _rec(release_date="2026/10/05"),               # 日付の形が違う
        _rec(basis_scope="whatever"),                  # 守りの範囲を偽る
        _rec(relationship_verified=True),              # 会社の関係を確かめた、と偽る
        # ★写しが長すぎる／多すぎる／同じ名鑑から2件★（依頼229の指摘3）
        _rec(evidence=[{"url": _C, "quote": _QC + "。" + "解析情報。" * 30,
                        "kind": "directory_observation"},
                       {"url": _N, "quote": _QN,
                        "kind": "directory_observation"}]),
        _rec(evidence=[{"url": _C, "quote": _QC,
                        "kind": "directory_observation"},
                       {"url": "https://chonborista.com/slot/x/2/",
                        "quote": _QC, "kind": "directory_observation"}]),
        _rec(evidence=[{"url": _C, "quote": _QC,
                        "kind": "directory_observation"}] * 5),
        # 引用に機種名が入っていない（別機種の欄でも通っていた）
        _rec(evidence=[{"url": _C, "quote": "メーカー 平和 の機種一覧です",
                        "kind": "directory_observation"},
                       {"url": _N, "quote": "メーカー 平和 の解析一覧です",
                        "kind": "directory_observation"}]),
        # ★機種名とメーカーは正しいが、導入日だけ無い★（依頼232の指摘）
        #   この検査だけ将来消えたときに、試験が気づけるようにする
        _rec(evidence=[{"url": _C, "quote": f"機種名 {_MN} メーカー {_SEEN}",
                        "kind": "directory_observation"},
                       {"url": _N, "quote": f"機種名 {_MN} メーカー {_SEEN}",
                        "kind": "directory_observation"}]),
        # 引用にメーカー欄の表記が入っていない
        _rec(evidence=[{"url": _C, "quote": f"機種名 {_MN} の解析",
                        "kind": "directory_observation"},
                       {"url": _N, "quote": f"機種名 {_MN} の天井",
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
    #   メーカー欄は「行の頭がメーカー」で読み取る（extract_maker_name）ので、
    #   1行にべた書きした偽ページでは**本番の読み取りを通らない**。
    #   ★関所を通る形の偽物でなければ、関所の試験にならない★
    def _page(maker=_SEEN, day="2026年10月5日", name=_MN, title=None,
              posts="読者の書き込みです"):
        """★本物の名鑑ページと同じ形の偽ページ★

        ★ここで手を抜くと関門の試験にならない★（2026-08-17に3回やった）
          ①題（title）… 本人性の検査（page_is_machine）が見る
          ②行として立つメーカー欄 … extract_maker_name が見る
          ③投稿欄（hyouka / commentlist）と本体（entry）
            … 投稿欄を箱ごと落とす処理（user_area.clean_html）が
              「その形のページか」を確かめるので、無いと必ず例外になる
        """
        # ★題も実在の名鑑と同じ言い回しにする★＝本人性の検査は厳格モードで
        #   呼ぶので、知らない語（「名鑑」等）が入ると弾かれる（実際に弾かれた）。
        t = (title if title is not None
             else f"{name} スロット 新台 天井 解析 | ちょんぼりすた")
        return (f"<title>{t}</title>"
                f'<div id="hyouka">星の評価</div>'
                f'<ul class="commentlist"><li>{posts}</li></ul>'
                f'<div id="entry">'
                f"<div>機種名 {name}</div>"
                f"<div>メーカー {maker}</div>"
                f"<div>導入日 {day}</div>"
                f"</div>")

    _pages = {
        _C: _page(),
        _N: _page(day="2026/10/5"),
        _LIST: _page(),
        _KIT: _page(),
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

    ev = _rec()["evidence"]

    def _ask(slug="dmm_5086", expected=_EXPECTED, seen=_SEEN, store=None,
             fetch=None, url=_C, name=_MN, day=_REL,
             profile="maker_field"):
        """★本番と同じ渡し方で引く★（対象URL・DMMで確かめた機種名と導入日）"""
        return verdict_for(slug, expected, seen,
                           st if store is None else store,
                           _fetch if fetch is None else fetch,
                           material_url=url, machine_name=name,
                           release_date=day, want_profile=profile)

    def _ok(**kw):
        base = dict(slug="dmm_5086", target_url=_C,
                    proof_profile="maker_field",
                    expected=_EXPECTED, seen=_SEEN,
                    verdict="ACCEPT_MATERIAL", why="理由",
                    by=["claude", "codex"], evidence=ev,
                    decided_at="2026-08-17", machine_name=_MN,
                    release_date=_REL, store=st, fetch=_fetch)
        base.update(kw)
        try:
            remember(**base)
            return True
        except CacheError:
            return False

    t("★★独立2名鑑の根拠つきなら控えられる★★", _ok())
    t("　控えた結論を引ける", _ask(seen="株式会社平和") == "ACCEPT_MATERIAL")

    # ★★★2026-08-17・Codex依頼229の指摘1★★★
    #   控えの鍵が (機種・期待する社・表記) の3つだけだったので、
    #   ①別機種の名前と日付を手で書いた控えでも「使う」を返せた
    #   ②いま決めようとしているURLを渡していないので、
    #     控えの根拠に入っていない別のページまで「使う」になった
    t("★★★控えの根拠に入っていないページには効かない★★★"
      "（前は採否対象のURLを渡していなかったので、同じ名鑑の別ページまで通った）",
      _ask(url="https://chonborista.com/slot/orinpia-slot/999999/") is None)
    t("　（対照）控えた対象ページなら通る", _ask(url=_C) == "ACCEPT_MATERIAL")
    # ★★根拠に入っているだけのページは、対象にならない★★
    #   （2026-08-17・台帳#390／Codex依頼233の指摘1）
    #   v2は根拠のどれでも対象になり得たので、2ページぶんの採否が
    #   1件の控えで決まっていた。v3は1ページにつき1件。
    t("★★★根拠に入っているだけのページは、それだけでは使えない★★★"
      "（採否は対象ページごとに控える）",
      _ask(url=_N) is None)
    t("　（対照）そのページも対象として控えれば使える",
      _ok(target_url=_N, slug="dmm_5086") and _ask(url=_N) == "ACCEPT_MATERIAL")

    # ★★★題が略称のときの証明（2026-08-17・台帳#390）★★★
    st2 = _empty()

    # ★弱い型はメーカー欄が名簿で一致する組でしか使えない★（依頼233の指摘2）
    #   ＝平和⇔オリンピアエステートは RELATED なのでここでは使えない。
    #   実例に合わせて 京楽（kyoraku）で試す。
    _KY, _KYS = "kyoraku", "京楽"
    _QKY = f"機種名 {_MN} メーカー {_KYS} 導入日 2026年10月5日"

    def _page_ky(title=None):
        """★京楽の名鑑ページ★（弱い型はメーカー欄が名簿で一致する組だけ）"""
        t0 = (title if title is not None
              else f"{_MN} スロット 新台 天井 解析 | ちょんぼりすた")
        return (f"<title>{t0}</title>"
                f'<div id="hyouka">星の評価</div>'
                f'<ul class="commentlist"><li>読者の書き込み</li></ul>'
                f'<div id="entry"><div>機種名 {_MN}</div>'
                f"<div>メーカー {_KYS}</div>"
                f"<div>導入日 2026年10月5日</div></div>")

    def _fetch_ky(u):
        _w_last(u)
        return _page_ky()

    def _ok2(**kw):
        base = dict(slug="dmm_5073", target_url=_C,
                    proof_profile="title_name_core_mismatch",
                    expected=_KY, seen=_KYS,
                    verdict="ACCEPT_MATERIAL", why="理由",
                    by=["claude", "codex"],
                    evidence=[{"url": _C, "quote": _QKY,
                               "kind": "directory_observation"}],
                    decided_at="2026-08-17", machine_name=_MN,
                    release_date=_REL, store=st2, fetch=_fetch_ky)
        base.update(kw)
        try:
            remember(**base)
            return True
        except CacheError:
            return False

    # ★★★ここは「本物の題の不一致」で試す★★★（2026-08-17・Codex依頼234の指摘2）
    #   ★5回目の同じ失敗★＝前は普通の題のページで試していたので、
    #   救う対象そのもの（NAME_CORE_MISMATCH）を一度も通しておらず、
    #   **控えを作れない＝機能しない**ことに気づけなかった。
    _NICK = _page_ky(title="【ガンゲイル(スマスロ)】解析情報まとめ 天井・設定判別")

    def _fetch_nick(u):
        _w_last(u)
        return _NICK

    import model_code_lookup as _mcl_t
    _pim = _mcl_t.page_is_machine(_NICK, _MN, strict_all_tail=True)
    t("　（前提）この偽ページは本当に題の不一致で落ちる",
      _pim == (False, "NAME_CORE_MISMATCH"))
    t("★★★題が略称の本物のページで、控えを作れる★★★"
      "（前は救う対象そのものを厳格同定で拒否していて、控えを作れなかった）",
      _ok2(fetch=_fetch_nick, slug="dmm_nick"))
    t("★★★作った控えで、そのページを材料に使えるところまで通る★★★",
      verdict_for("dmm_nick", _KY, _KYS, st2, _fetch_nick,
                  material_url=_C, machine_name=_MN, release_date=_REL,
                  want_profile="title_name_core_mismatch")
      == "ACCEPT_MATERIAL")
    # ★★★使うときにも到達先を見る★★★（2026-08-17・Codex依頼234の指摘1）
    #   ★穴だったところ★＝記録時は転送を拒否し到達先も残していたのに、
    #   使うときは一度も比べていなかった。同じ名鑑の**別の機種ページ**へ
    #   転送されると、転送先も機種ページの形に合うので止まらず、
    #   4つの読取器が転送先の本文から値を読めた。
    _C2 = "https://chonborista.com/slot/orinpia-slot/777777/"
    t("★★★控えたページが別の機種ページへ転送されていたら使わない★★★",
      verdict_for("dmm_nick", _KY, _KYS, st2,
                  lambda u: (_w_last(_C2), _NICK)[1],
                  material_url=_C, machine_name=_MN, release_date=_REL,
                  want_profile="title_name_core_mismatch") is None)
    # ★★★末尾の / の違いで、到達先の照合を迂回できないこと★★★
    #   （2026-08-17・Codex依頼235の指摘1）
    #   ★穴だったところ★＝対象の同一性は「/ を外して」比べるのに、
    #   到達先の表は**生の文字列**を鍵にしていた。
    #   「控えは / 付き・実行時は / 無し」というだけで表を引けず、
    #   照合が丸ごと飛んで「使う」に到達した。
    _C_NOSLASH = _C.rstrip("/")
    t("　（前提）控えと実行時で末尾の / が違っても、同じ対象として引ける",
      url_key(_C) == url_key(_C_NOSLASH))
    t("★★★末尾の / が違っても、到達先の照合は飛ばない★★★",
      verdict_for("dmm_nick", _KY, _KYS, st2, _fetch_nick,
                  material_url=_C_NOSLASH, machine_name=_MN,
                  release_date=_REL,
                  want_profile="title_name_core_mismatch")
      == "ACCEPT_MATERIAL")
    t("★★★その形でも、別の機種ページへ転送されていれば拒否する★★★"
      "（前はここが素通りだった）",
      verdict_for("dmm_nick", _KY, _KYS, st2,
                  lambda u: (_w_last(_C2), _NICK)[1],
                  material_url=_C_NOSLASH, machine_name=_MN,
                  release_date=_REL,
                  want_profile="title_name_core_mismatch") is None)
    t("　到達先が取れないときも拒否する（取れない＝確かめていない）",
      verdict_for("dmm_nick", _KY, _KYS, st2,
                  lambda u: (_w_last(""), _NICK)[1],
                  material_url=_C, machine_name=_MN, release_date=_REL,
                  want_profile="title_name_core_mismatch") is None)
    t("★★題の不一致“以外”で落ちるページは、この型でも救わない★★",
      not _ok2(slug="dmm_notitle",
               fetch=lambda u: (_w_last(u), _page_ky(title=""))[1]))
    t("★★本文にDMMの正式名がそのまま無ければ救わない★★",
      not _ok2(slug="dmm_noname",
               fetch=lambda u: (_w_last(u),
                                _NICK.replace(_MN, "L別のなにか"))[1]))
    t("★★題が略称のときは、そのページ1件で控えられる★★"
      "（2件目の名鑑は別途、正規の同定を通っている）", _ok2())
    t("　（対照）メーカーの食い違いのほうは、今までどおり2名鑑が要る",
      not _ok2(proof_profile="maker_field", slug="dmm_x"))
    t("★★★題の不一致で作った控えを、メーカーの食い違いに流用できない★★★"
      "（証明の型が違えば効かない）",
      verdict_for("dmm_5073", _KY, _KYS, st2, _fetch_ky,
                  material_url=_C, machine_name=_MN, release_date=_REL,
                  want_profile="maker_field") is None)
    t("　（対照）同じ型で引けば効く",
      verdict_for("dmm_5073", _KY, _KYS, st2, _fetch_ky,
                  material_url=_C, machine_name=_MN, release_date=_REL,
                  want_profile="title_name_core_mismatch")
      == "ACCEPT_MATERIAL")
    t("　証明の型を勝手に作れない",
      not _ok2(proof_profile="でっちあげ", slug="dmm_y"))
    t("　対象ページを名乗らなければ控えられない",
      not _ok2(target_url="", slug="dmm_z"))
    t("★★★控えが名乗る機種がDMMと違えば効かない★★★"
      "（別機種の機種名・導入日を手で書いた控えを、読むときに落とす）",
      _ask(name="L別の機種") is None and _ask(day="2026-11-02") is None)
    t("★★対象を渡さなければ答えない★★（fail-closed）",
      _ask(url="") is None and _ask(name="") is None and _ask(day="") is None)

    # ★★使うときにも根拠を取り直す（2026-08-14・依頼192のP1）★★
    t("★★根拠のページが取れなくなったら材料に使わない★★"
      "／手で書き足した偽の根拠を、使う直前に落とす",
      _ask(fetch=lambda u: (_ for _ in ()).throw(RuntimeError("404"))) is None)
    t("　（対照）取り直せるうちは今までどおり使える",
      _ask() == "ACCEPT_MATERIAL")
    t("　引用が消えていたら使わない",
      _ask(fetch=lambda u: "<p>ページが作り替えられました</p>") is None)

    # ★★2026-08-17・依頼228で足した守り★★
    t("★★導入日がそのページに無ければ材料に使わない★★"
      "／同名で別メーカーの機種は導入年が違う（犬夜叉＝2016年／2022年）",
      _ask(fetch=lambda u: (_w_last(u), _page(day="未定"))[1]
           if u == _C else (_w_last(u), _pages[u])[1]) is None)
    # ★★★2026-08-17・Codex依頼229の指摘2★★★
    #   前は「seen という文字がページのどこかにあるか」しか見ていなかったので、
    #   メーカー欄が別の社でも、本文のどこかに「平和」とあれば通った。
    t("★★★メーカー欄が別の社なら、本文に同じ文字があっても使わない★★★"
      "（前はページのどこかに文字があれば「2件目の名鑑」に数えられた）",
      _ask(fetch=lambda u: (_w_last(u), _page(maker="サミー")
                            + "<div>関連: 平和 の機種はこちら</div>")[1]
           if u == _C else (_w_last(u), _pages[u])[1]) is None)
    # ★★★2026-08-17・Codex依頼230★★★
    #   材料になるページ自身は本体の同定を通るが、控えの2件目以降の根拠には
    #   同じ検査が無かった。別機種のページの「関連機種」欄に対象名・メーカー・
    #   日付が並んでいれば、独立2名鑑の1票になり得た。
    # ★題は実在の言い回しにする★（2026-08-17・Codex依頼232の指摘）
    #   「| 名鑑」のような知らない語を入れると、**機種名の照合が壊れていても
    #   末尾語だけで拒否されて試験が通る**（違う理由で合格してしまう）。
    t("★★★別機種のページは、対象名もメーカーも日付も載っていても使わない★★★"
      "（本体と同じ本人性の検査を、控えの根拠にも通す）",
      _ask(fetch=lambda u: (_w_last(u),
                            _page(title="L別の機種 スロット 新台 天井 解析"
                                        " | ちょんぼりすた"))[1]
           if u == _C else (_w_last(u), _pages[u])[1]) is None)
    # ★★★2026-08-17・Codex依頼231の指摘2★★★
    #   本体は取得直後に投稿欄・AI欄を箱ごと落としてから読む。控えの再確認は
    #   それを通していなかったので、★読者の書き込みが根拠になり得た★。
    t("★★★投稿欄に書かれた別のメーカー名を、根拠にしない★★★"
      "（本体と同じく、投稿欄を箱ごと落としてから読む）",
      _ask(fetch=lambda u: (_w_last(u),
                            _page(posts="メーカー サミー だと思う"))[1]
           if u == _C else (_w_last(u), _pages[u])[1]) == "ACCEPT_MATERIAL")
    t("　投稿欄を落とせない形のページは使わない（fail-closed）",
      _ask(fetch=lambda u: (_w_last(u),
                            "<title>L転生王女と天才令嬢の魔法革命 スロット 新台"
                            " 解析 | ちょんぼりすた</title>"
                            "<div>機種名 L転生王女と天才令嬢の魔法革命</div>"
                            "<div>メーカー 平和</div>"
                            "<div>導入日 2026年10月5日</div>")[1]
           if u == _C else (_w_last(u), _pages[u])[1]) is None)
    t("　題の無いページも使わない（本人性を確かめられない）",
      _ask(fetch=lambda u: (_w_last(u), _page(title=""))[1]
           if u == _C else (_w_last(u), _pages[u])[1]) is None)
    t("　メーカー欄を読めないページも使わない（読めない＝確かめていない）",
      _ask(fetch=lambda u: (_w_last(u), f"<div>{_QC} 導入日 2026年10月5日</div>")[1]
           if u == _C else (_w_last(u), _pages[u])[1]) is None)
    t("★★名鑑1つだけでは控えられない★★（独立2名鑑が要る）",
      not _ok(evidence=[ev[0]], slug="dmm_1"))
    t("　（対照）2つあれば通る＝厳しすぎるのではない", _ok(slug="dmm_2"))
    t("★★同じ名鑑の2ページでは2つに数えない★★",
      not _ok(evidence=[ev[0], dict(ev[0], url=_LIST)], slug="dmm_same"))
    t("★★その名鑑の機種ページでないURLは根拠にできない★★"
      "／一覧・特集・別機種のページ",
      not _ok(evidence=[dict(ev[0], url=_LIST), ev[1]], slug="dmm_list"))
    t("★★引用に機種名が入っていなければ控えられない★★",
      not _ok(evidence=[dict(ev[0], quote="メーカー 平和 の一覧"),
                        dict(ev[1], quote="メーカー 平和 の解析")],
              slug="dmm_noname"))
    t("★★引用にメーカー欄の表記が入っていなければ控えられない★★",
      not _ok(evidence=[dict(ev[0], quote=f"機種名 {_MN} の解析"),
                        dict(ev[1], quote=f"機種名 {_MN} の天井")],
              slug="dmm_nomaker"))
    t("★★「公式の関係」という根拠はもう受け取らない★★"
      "／メーカー公式を見るのをやめた（運営者判断・2026-08-17）",
      not _ok(evidence=[ev[0], dict(ev[1], kind="official_relationship")],
              slug="dmm_off"))
    t("　根拠の種類を勝手に作れない",
      not _ok(evidence=[ev[0], dict(ev[1], kind="でっちあげ")],
              slug="dmm_kind"))

    # ★★★2026-08-17・Codex依頼229の指摘3★★★
    #   運営者の判断（なな徹の規約・「事実の欄だけ・短い抜粋なので続ける」）を
    #   コードで守らせる。前は下限しか無く、記事本文を丸ごと控えられた。
    _C2 = "https://chonborista.com/slot/orinpia-slot/264135/"
    _pages[_C2] = _page()
    _long = _QC + "。" + "この機種の解析情報をお届けします。" * 12
    _pages[_C] = _page() + f"<div>{_long}</div>"
    t("★★★長すぎる引用は控えられない★★★"
      "（記事本文を丸ごと写せた＝規約の判断の前提が守られていなかった）",
      len(_long) > MAX_QUOTE
      and not _ok(evidence=[dict(ev[0], quote=_long), ev[1]], slug="dmm_long"))
    _pages[_C] = _page()
    t("　（対照）欄の写しの長さなら通る＝厳しすぎるのではない",
      len(_QC) <= MAX_QUOTE and _ok(slug="dmm_short"))
    t("★★同じ名鑑から2件は控えない★★（写しは最小限に）",
      not _ok(evidence=[ev[0], dict(ev[0], url=_C2)], slug="dmm_two"))
    t("★★根拠の件数にも上限がある★★",
      not _ok(evidence=[ev[0], ev[1], dict(ev[0], url=_C2),
                        dict(ev[1], url=_N), dict(ev[0], url=_C)],
              slug="dmm_many"))

    # ★★以前からの守り（v1で入れたもの）が生きているか★★
    t("★★機種が違えば効かない★★（全機種に一律で効かせない）",
      verdict_for("dmm_9999", _EXPECTED, _SEEN, st) is None)
    t("　期待する社が違えば効かない",
      verdict_for("dmm_5086", "sammy", _SEEN, st) is None)
    t("★★答えが出ない状態は控えない★★", not _ok(verdict="UNKNOWN"))
    t("★★根拠が無ければ受け取らない★★",
      not _ok(evidence=[]) and not _ok(
          evidence=[dict(ev[0], quote="短い"), ev[1]]))
    t("★★判断した者が1人だけなら受け取らない★★（2AIで決める）",
      not _ok(by=["claude"]))
    t("★★判断者は決めた2つ以外を受け取らない★★"
      "／以前は架空のID2つでも「違う2者」だった",
      not _ok(by=["foo", "bar"], slug="dmm_by")
      and not _ok(by=["claude", "gemini"], slug="dmm_by2"))
    t("　（対照）決めた2つなら通る", _ok(by=["codex", "claude"], slug="dmm_by3"))
    t("　同じ対象ページを2度控えても増えない",
      (_ok(why="別の理由")
       and sum(1 for r in st["machines"]["dmm_5086"]
               if r.get("target_url") == _C) == 1))
    t("★★逐語引用がそのページに無ければ受け取らない★★"
      "／以前はURLも引用も言うだけで通った",
      not _ok(evidence=[dict(ev[0], quote=f"機種名 {_MN} メーカー {_SEEN} 嘘"),
                        ev[1]], slug="dmm_lie"))
    t("　ページを取れなければ控えない（fail-closed）",
      not _ok(evidence=[dict(ev[0], url=_C.replace("264134", "999999")),
                        ev[1]], slug="dmm_nai"))
    t("★★名鑑でない登録済みサイトを「名鑑での観測」にできない★★"
      "／source-registry には解析サイトも載っているので、"
      "「登録済みの発行者」だけで見ると役割の分離が崩れる",
      "www.kitadenshi.co.jp" not in directory_hosts()
      and "nana-press.com" in directory_hosts()
      and not _ok(evidence=[dict(ev[0], url=_KIT), ev[1]], slug="dmm_kit"))
    t("★★同じ社でも https から http へ落とされたら受け取らない★★"
      "／ホストが変わったときだけ見ていたので素通りしていた",
      not _ok(slug="dmm_down",
              fetch=lambda u: (_w_last(u.replace("https://", "http://")),
                               _pages[u])[1]))
    t("　http（暗号化なし）の根拠は受け取らない",
      not _ok(evidence=[dict(ev[0], url=_C.replace("https://", "http://")),
                        ev[1]], slug="dmm_http"))
    t("★★許可したURLから許可外へ転送されたら受け取らない★★",
      not _ok(slug="dmm_redir",
              fetch=lambda u: (_w_last(_KIT), _pages[_C])[1]
              if u == _C else (_w_last(u), _pages[u])[1]))
    t("　（対照）「使わない」と決めるのは名鑑1つでもよい",
      _ok(verdict="REJECT_MATERIAL", evidence=[ev[0]], slug="dmm_rej"))
    t("★★読むときも同じ物差しで確かめる★★（手で書き足しても信用しない）",
      _bad_load())
    t("　取り消せる",
      forget("dmm_5086", _C, st)
      and verdict_for("dmm_5086", _EXPECTED, _SEEN, st) is None)

    # ★日付の書き方をならすところ★（相手の作りを読むのではない）
    t("　同じ日付を、名鑑ごとの書き方に直せる"
      "（ちょんぼりすた「2026年10月5日」／なな徹「2026/10/5」）",
      "2026年10月5日" in date_forms(_REL) and "2026/10/5" in date_forms(_REL)
      and date_forms("") == [] and date_forms("2026/10/05") == [])

    # --- ★導入前の新台（月精度）でも控えられる★（2026-08-21・台帳#424）
    #   直す前は日精度を必須にしていたので、**2AIが決めても控えられなかった**
    #   （2026-08-20に実際に発生: dmm_5073 は "2026-11"／"2026年11月上旬予定"）。
    t("★★月までしか分からなくても鍵として使える★★", _release_key_ok("2026-11"))
    t("　日まで分かっていれば当然使える", _release_key_ok("2026-11-07"))
    t("★年だけ・空・でたらめは使えない★",
      not _release_key_ok("2026") and not _release_key_ok("")
      and not _release_key_ok("2026年11月"))

    t("★★控えが月まで・いまが日までなら、月で比べて同じ扱い★★",
      _release_same("2026-11", "2026-11-07"))
    t("　逆向き（控えが日まで・いまが月まで）も同じ",
      _release_same("2026-11-07", "2026-11"))
    t("★★月が違えば別物★★", not _release_same("2026-11", "2026-12-01"))
    t("★年が違えば別物★", not _release_same("2026-11", "2027-11-07"))
    t("　日まで同士は、いままでどおり完全一致で見る",
      _release_same("2026-11-07", "2026-11-07")
      and not _release_same("2026-11-07", "2026-11-08"))
    t("★どちらかが空なら「同じ」とは言わない★",
      not _release_same("", "2026-11-07") and not _release_same("2026-11", ""))

    print()
    # ★★月までしか分からない導入日★★（2026-08-22・台帳#454）
    #   ★直す前★＝date_forms("2026-11") が空の配列を返し、
    #   ①どんな逐語も照合に通らない ②説明文が _days[0] を読んで IndexError。
    #   ＝導入前の新台は控えを作れず、dmm_5073 が13回空振りした。
    #
    #   ★作った直後にもう1つ穴が出た★＝桁を詰めない「2026/1」は
    #   **「2026/12/1」の中にそのまま現れる**ので、1月の鍵が
    #   10月・11月・12月の引用に当たっていた（実データで再現）。
    for _key, _q, _want, _why in (
            ("2026-11",
             "機種名 L ソードアート・オンライン オルタナティブ ガンゲイル・オンライン"
             " メーカー 京楽 仕様 不明 導入日 2026/11/2", True,
             "★本番の逐語（なな徹・dmm_5073）に当たる★"),
            ("2026-11", "導入日 2026年11月上旬予定", True, "DMMの書き方"),
            ("2026-11", "導入日 2026年11月5日", True, "ちょんぼりすたの書き方"),
            ("2026-01", "導入日 2026/12/1", False,
             "★★1月の鍵が12月の引用に当たらない★★（前方一致の穴）"),
            ("2026-01", "導入日 2026/10/6", False,
             "★★1月の鍵が10月の引用に当たらない★★"),
            ("2026-01", "導入日 2026/1/15", True, "1月の逐語には当たる"),
            ("2026-01", "導入日 2026年1月15日", True, "1月（漢字）"),
            ("2026-12", "導入日 2026/12/1", True, "12月"),
            ("2026-11", "導入日 2026/12/1", False, "別の月には当たらない"),
    ):
        t(f"　月精度の照合: {_why}",
          any(_d in _q for _d in date_forms(_key)) is _want)
    t("★日まで分かるときは今までどおり日で見る★",
      date_forms("2026-11-02")[0] == "2026/11/2"
      and "2026/11" not in date_forms("2026-11-02"))
    t("　形が想定外なら空を返す（説明文で落ちない）",
      date_forms("へんな値") == [] and date_forms("") == [])

    # ★★数えるのは、全部の試験が終わったこの場所だけ★★（2026-08-22）
    #   ★直す前★＝ここより手前で数えていたので、あとに続く11件が
    #   ❌でも「84/84 合格」終了コード0 になっていた。
    #   ★実証★＝台帳#454の直しをわざと壊すと❌が6件出るのに緑のまま通った。
    ng = sum(1 for _, o in results if not o)
    print("%d/%d 合格" % (len(results) - ng, len(results)))
    return 1 if ng else 0

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
    ap.add_argument("--proof-profile", dest="proof_profile",
                    default="maker_field",
                    choices=sorted(PROOF_PROFILES),
                    help="なぜ機械が決められなかったか（証明の型）")
    ap.add_argument("--expected", help="期待している社（名簿のキー）")
    ap.add_argument("--seen", help="名鑑のメーカー欄に書かれていた表記")
    ap.add_argument("--verdict", choices=VERDICTS)
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
                    print(f"   {r['expected']} ⇔ {r['seen']}  {r['verdict']}")
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
                print("★--evidence は『URL|逐語引用|種類』の形で書きます★")
                return 1
            ev.append({"url": parts[0], "quote": parts[1], "kind": parts[2]})
        import datetime
        # ★機種名と導入日はDMMの機種ページから取る★（2026-08-17・依頼228）
        #   ★呼ぶ側の自己申告で決めない★＝控えは手で書けるファイルなので、
        #   ここを言うだけで通すと「別機種の名前と日付」で根拠を作れてしまう。
        #   「使わない」と決めるだけなら機種の紐づけは要らない（取りに行かない）。
        machine_name, release_date = "", ""
        if a.verdict == "ACCEPT_MATERIAL":
            if not str(a.machine_name or "").strip():
                print("★--machine-name が要ります"
                      "（カレンダーに載っている機種名）★")
                return 1
            import dmm_machine as _dm
            try:
                got_m = _dm.fetch(m.group(1))
            except Exception as e:         # noqa: BLE001
                print(f"★DMMの機種ページを読めません: {str(e)[:120]}★")
                return 1
            ok_name, why_name = _dm.name_matches(got_m.get("heading") or "",
                                                 a.machine_name)
            if not ok_name:
                print(f"★その機種名はDMMの機種ページと一致しません: {why_name}★")
                return 1
            machine_name = str(a.machine_name).strip()
            release_date = str(got_m.get("release_date") or "")
            if not _release_key_ok(release_date):
                print(f"★DMMから導入日を取れません（{release_date!r}）★"
                      "／年月すら分からない機種は控えられません")
                return 1
        # ★CLIでは取ってくる役を差し替えない★＝本物のページで照合する
        rec = remember(slug, a.expected or "", a.seen or "", a.verdict or "",
                       a.why or "", [x.strip() for x in
                                     str(a.by or "").split(",") if x.strip()],
                       ev, a.at or datetime.date.today().isoformat(),
                       machine_name, release_date,
                       target_url=a.target_url,
                       proof_profile=a.proof_profile)
        print(json.dumps({"state": "RECORDED", "slug": slug, **rec},
                         ensure_ascii=False)[:300])
        return 0
    except CacheError as e:
        print("★" + str(e) + "★")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

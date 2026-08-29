#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""★その場で2AIが決めて直す★ための実行記録（自動修理トランザクション）

★なぜ作ったか（運営者の指示・2026-08-21）★
  > だから台帳をなくそうよ　その場で２AI判断で記事作成してってば。

  台帳は「人が後で片付けるもの置き場」になっていた。
  実測＝未処理118件のうち73件が品質レビューの積み残しで、
  ★閉じたのはこの30日ぜんぶ対話セッション（人の手）★だった。

★これは置き場ではない★（Codexの設計レビュー・2026-08-21）
  > 台帳の代わりに、人が処理しない「自動修理トランザクション記録」が必要です。
  > これは未処理置き場ではなく、落ちても自動再開するための実行記録です。

  ＝1件の直しが「どこまで進んだか」を残すだけの帳面。
  途中で落ちても、翌朝そこから続けられる。溜まったら知らせて終わる。

★段階★
  DETECTED       機械が見つけた（まだ誰も判断していない）
  CLAUDE_SEALED  ★Claudeの判定を先に封をした★（Codexの答えを見る前に）
  CODEX_RECEIVED Codexの判定を受け取った
  AGREED         2つが一致した（＝直してよい）
  APPLIED        記事データを書き換えた
  COMMIT_VERIFIED 差分とコミットが結び付いた
  PUSH_CONFIRMED  push できた
  RECHECK_PASS    ★機械が直ったことを確かめ直した★
  DONE            終わり
  ESCALATED       3回やっても決まらなかった → 台帳＋メール（人の出番）

★守っていること★
  1. ★Claudeの判定は、Codexを呼ぶ前にファイルへ書いて指紋を取る★
     （Codexへ渡す材料に含めない＝答えを見てから書き換えられないようにする）
  2. ★閉じられる検査が無い型は受け付けない★
     `recheck.CHECKS[...]["closeable"]` が真でなければ AGREED にできない。
     ＝「直せても機械的に閉じられない」ものを自動で触らせない
  3. ★段階を飛ばせない★（順番どおりにしか進めない）
  4. ★記事が変わっていたらやり直し★（source_sha256 を照合）
  5. ★3回で打ち切る★（材料が同じなら同じ結論しか出ない）

★台帳との関係★
  台帳は消さない。ただし ★人の手を待つのは ESCALATED だけ★ にする。
  RECHECK_PASS まで来たものは、元になった台帳案件があれば
  `recheck.closeable()` を通して自動で閉じる（AIの宣言では閉じない）。

使い方:
  python scripts/repair_journal.py --list
  python scripts/repair_journal.py --show <finding_id>
  python scripts/repair_journal.py --selftest
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import safe_json as _sj                # noqa: E402

SCHEMA = "repair-journal/v1"
STORE = os.path.join(os.path.expanduser("~"), "Documents", "uchidokoro",
                     "repairs")

# ★段階の並び★（この順にしか進めない）
FLOW = (
    "DETECTED",
    "CLAUDE_SEALED",
    "CODEX_RECEIVED",
    "AGREED",
    "APPLIED",
    "COMMIT_VERIFIED",
    "PUSH_CONFIRMED",
    "RECHECK_PASS",
    "DONE",
)
ESCALATED = "ESCALATED"
STATES = FLOW + (ESCALATED,)

MAX_ATTEMPTS = 3          # ★その晩のうちに3回まで★（CLAUDE.md の決まり）

# ★2AIが選べる操作★（自由文のパッチは作らせない）
#   Codexの設計レビュー: 「AIは自由文パッチを作らず、許可済み操作を選ぶだけ」
ALLOWED_OPS = ("drop", "replace")


class JournalError(Exception):
    pass


# --- 置き場 ---------------------------------------------------------------

def _store() -> str:
    os.makedirs(STORE, exist_ok=True)
    return STORE


def _path(finding_id: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{16}", finding_id or ""):
        raise JournalError(f"見覚えのない番号です: {finding_id!r}")
    return os.path.join(_store(), finding_id + ".json")


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8").replace(b"\r\n", b"\n")).hexdigest()


def finding_id(slug: str, check: str, quote: str, where: str = "") -> str:
    """★同じ問題なら、いつ数えても同じ番号になる★

    ＝台帳番号の代わり。番号を採番せずに済むので、
    「昨日の#318」のような**人が付けた札**に頼らなくてよくなる。
    ★HEADで見つけ直したときに同じ番号になることが肝★
      （Codexの指摘: 既存の自由文を修正命令として使わず、
        いまのHEADで再検出して finding を作り直す）
    """
    src = json.dumps({"slug": slug, "check": check, "quote": quote,
                      "where": where}, ensure_ascii=False, sort_keys=True)
    return _sha(src)[:16]


# --- 読み書き -------------------------------------------------------------

def load(fid: str) -> dict:
    p = _path(fid)
    if not os.path.exists(p):
        raise JournalError(f"#{fid} の記録がありません")
    with io.open(p, encoding="utf-8") as f:
        rec = json.load(f)
    if rec.get("schema_version") != SCHEMA:
        raise JournalError(f"知らない形の記録です: {rec.get('schema_version')!r}")
    return rec


def _save(rec: dict) -> None:
    p = _path(rec["finding_id"])
    tmp = p + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, p)


def _step(rec: dict, to: str, note: str, **extra) -> dict:
    """★段階を1つだけ進める★（飛ばせない・戻れない）"""
    cur = rec["state"]
    if to == ESCALATED:
        rec["state"] = ESCALATED
    else:
        if cur == ESCALATED:
            raise JournalError(f"#{rec['finding_id']} は人へ回した後です")
        if cur not in FLOW:
            raise JournalError(f"知らない段階です: {cur!r}")
        want = FLOW[FLOW.index(cur) + 1] if cur != FLOW[-1] else None
        if to != want:
            raise JournalError(
                f"段階を飛ばせません（いま {cur} ／ 次は {want} ／ 頼まれたのは {to}）")
        rec["state"] = to
    rec.update(extra)
    rec.setdefault("history", []).append({"to": rec["state"], "note": note})
    _save(rec)
    return rec


# --- 各段階 ---------------------------------------------------------------

def _archive(fid: str, rec: dict) -> None:
    """終わった記録を横へよける（2026-08-27・Codexの指摘9）。

    ★消さない★＝いつ何をしたかは残す。名前を変えて置いておくだけ。
    ★再発したときに、同じ finding_id で立て直せるようにする★のが目的。
    """
    src = _path(fid)
    n = 1
    while os.path.exists(src + f".closed{n}"):
        n += 1
    try:
        os.replace(src, src + f".closed{n}")
    except OSError as e:                                     # noqa: BLE001
        raise JournalError(f"終わった記録をよけられません: {e}")


def detect(slug: str, check: str, quote: str, where: str = "",
           source_sha256: str = "", detail: str = "") -> dict:
    """機械が見つけた。★まだ何も判断していない★"""
    if not slug or not check or not quote:
        raise JournalError("機種・検査名・逐語の3つが要ります")
    # ★★記事の指紋は必ず要る★★（2026-08-27・Codexの指摘11）
    #   ★直す前は省略できた★＝空だと後段の照合が丸ごと働かず、
    #   ★見つけたときから記事が変わっていても書けた★。
    #   この仕組みの土台なので、無いなら受け取らない。
    if len(str(source_sha256 or "")) != 64:
        raise JournalError(
            "見つけた時点の記事の指紋（source_sha256・64桁）が要ります")
    fid = finding_id(slug, check, quote, where)
    p = _path(fid)
    if os.path.exists(p):
        # ★★終わった件と同じ指摘が再発したら、新しく立て直す★★
        #   （2026-08-27・Codexの指摘9）
        #   ★直す前は DONE/ESCALATED でもそのまま返していた★ので、
        #   ★同じ誤りが再発しても、二度と直せなかった★
        #   （終わった記録は先へ進めないので、その機種だけ永久に止まる）。
        got = load(fid)
        if got.get("state") in (ESCALATED, "DONE"):
            # ★記事が当時と同じなら、本当に同じ話なので触らない★
            if str(got.get("source_sha256") or "") == str(source_sha256):
                return got
            _archive(fid, got)
        else:
            return got                # ★途中のものは二重に立てない★
    rec = {
        "schema_version": SCHEMA,
        "finding_id": fid,
        "slug": slug,
        "check": check,
        "quote": quote,
        "where": where,
        "source_sha256": source_sha256,
        "detail": detail,
        "state": "DETECTED",
        "attempts": 0,
        "history": [{"to": "DETECTED", "note": detail or check}],
    }
    _save(rec)
    return rec


def seal_claude(fid: str, verdict_path: str) -> dict:
    """★Codexを呼ぶ前に、Claudeの判定へ封をする★

    （Codexの設計レビュー: 「Claudeの判定はファイルに保存・指紋化してから
      Codexを呼び、Codexのプロンプトには含めない」）
    ★ここで指紋を取っておかないと★、Codexの答えを見てから
    「私も同じ判定でした」と書き換えられてしまう。
    """
    rec = load(fid)
    if not os.path.exists(verdict_path):
        raise JournalError(f"判定のファイルがありません: {verdict_path}")
    with io.open(verdict_path, encoding="utf-8") as f:
        text = f.read()
    if len(text.strip()) < 20:
        raise JournalError("判定が短すぎます（20字以上で書いてください）")
    return _step(rec, "CLAUDE_SEALED", "Claudeの判定に封をした",
                 claude_verdict_sha256=_sha(text),
                 claude_verdict_path=os.path.abspath(verdict_path))


def record_codex(fid: str, material_sha256: str, verdict_text: str) -> dict:
    """Codexの判定を受け取る。★封をしてからでないと受け取らない★"""
    rec = load(fid)
    if not rec.get("claude_verdict_sha256"):
        raise JournalError("先にClaudeの判定へ封をしてください")
    # ★★材料の指紋は必ず要る★★（2026-08-27・Codexの3回目の指摘6）
    #   ★直す前は空でも進めた★ので、
    #   「予定した材料をCodexが受け取った」ことを何も確かめていなかった。
    _m = str(material_sha256 or "")
    if len(_m) != 64 or any(c not in "0123456789abcdef" for c in _m.lower()):
        raise JournalError(
            "Codexへ渡した材料の指紋（64桁）が要ります")
    if len((verdict_text or "").strip()) < 20:
        raise JournalError("Codexの判定が短すぎます")
    return _step(rec, "CODEX_RECEIVED", "Codexの判定を受け取った",
                 codex_material_sha256=material_sha256,
                 codex_verdict_sha256=_sha(verdict_text))


def _ops_from_decision(path: str, rec: dict) -> list:
    """決定ファイルから操作を取り出す（★同じ件・同じ記事であること★）。

    ★ここで確かめる3つ★
      ①その決定ファイルが、この件（finding_id）のものだと名乗っていること
      ②機種が同じこと
      ③見つけたときと同じ記事に対する判断であること
    """
    got = _sj.read_json(path, expect=dict)
    if str(got.get("finding_id") or "") != str(rec["finding_id"]):
        raise JournalError(
            "決定ファイルが、この件のものだと名乗っていません"
            f"（finding_id: {got.get('finding_id')!r}）")
    if str(got.get("slug") or "") != str(rec.get("slug") or ""):
        raise JournalError(
            f"決定ファイルの機種が違います（{got.get('slug')!r}）")
    # ★★指紋は必ず要る★★（2026-08-27・Codexの2回目の指摘5）
    #   ★直す前は「空なら照合しない」だった★ので、
    #   書かなければ「同じ記事を見て作られた」の確認を丸ごと外せた。
    _s = str(got.get("source_sha256") or "")
    if not _s:
        raise JournalError(
            "決定ファイルに、見つけたときの記事の指紋（source_sha256）が"
            "ありません")
    if _s != str(rec.get("source_sha256") or ""):
        raise JournalError(
            "決定ファイルは、見つけたときとは別の記事に対する判断です")
    acts = got.get("actions")
    if not isinstance(acts, list) or not acts:
        raise JournalError("決定ファイルに actions がありません")
    return acts


def _same_deciders(a, b) -> bool:
    """判断者の顔ぶれが同じか（★大文字小文字と前後の空白は無視★）。"""
    def _n(x):
        return {str(v).strip().lower() for v in (x or []) if str(v).strip()}
    return _n(a) == _n(b)


DECISION_KEYS = ("schema_version", "slug", "finding_id",
                 "source_sha256", "actions", "numbers_removed",
                 # ★「誰が決めたか」も指紋に入れる★（2026-08-27・Codexの4回目）
                 #   ★入れていなかった★ので、合意のあとで判断者を
                 #   書き換えても指紋は一致したままだった。
                 #   適用のときはこの欄で「2AIで決めたか」を判定している。
                 "decided_by")


def decision_digest(dec) -> str:
    """★決定ファイル全体の指紋★（2026-08-27・Codexの3回目の指摘3）

    ★なぜ actions だけでは足りないか★＝
      合意のあとで `numbers_removed` を**追記**すれば、
      本来止まる「記事から数値が消える削除」を免除できた。
      actions は変わっていないので、操作だけの指紋は一致したまま。
    ★適用の結果を変えうる欄は、全部ここに入れる★
    """
    d = dec if isinstance(dec, dict) else {}
    return _sha(json.dumps({k: d.get(k) for k in DECISION_KEYS},
                           ensure_ascii=False, sort_keys=True))


def ops_digest(ops) -> str:
    """★合意した操作そのものの指紋★（2026-08-27・Codexの指摘6）

    ★なぜ要るか★＝合意した内容と、実際に当てる決定ファイルを
    結び付けるものが無かった。＝★無害な合意を、同じ機種への
    まったく別の書き換えの許可証にできた★。
    ★並びも中身も同じでなければ一致しない★（並べ替えでごまかせない）。
    """
    return _sha(json.dumps(ops, ensure_ascii=False, sort_keys=True))


def agree(fid: str, ops: list, recheck_name: str, decided_by: list) -> dict:
    """2つの判定が一致した。★ここで初めて「直してよい」になる★

    ★閉じられる検査が無い型は受け付けない★（Codexの設計レビュー）
      直したあとに機械が確かめ直せないなら、自動で触らせない。
    """
    rec = load(fid)
    # ★★違う名前が2つ以上★★（2026-08-27・Codexの3回目）
    #   ★件数だけ見ていた★ので ["Claude","Claude"] でも通った。
    if not isinstance(decided_by, list) \
            or len({str(x).strip().lower()
                    for x in decided_by if str(x).strip()}) < 2:
        raise JournalError(
            f"**違う**判断者が2つ以上要ります（2AIで決めるため）: "
            f"{str(decided_by)[:40]}")
    # ★★受け取るのは「決定ファイルそのもの」★★（2026-08-27・Codexの指摘6）
    #   ★直す前は、AIが打ち直した操作の配列を受け取っていた★ので、
    #   合意した中身と、実際に当てる決定ファイルを結ぶものが無かった。
    #   ＝★無害な合意を、同じ機種への別の書き換えの許可証にできた★。
    _dec_raw = {}
    if isinstance(ops, str):
        _dec_raw = _sj.read_json(ops, expect=dict)
        ops = _ops_from_decision(ops, rec)
        # ★★決定ファイルの判断者と、合意の判断者が同じであること★★
        #   （2026-08-27・Codexの4回目の指摘3）
        #   ★直す前は別々に見ていた★ので、記録と決定ファイルで
        #   「誰が決めたか」が食い違っていても分からなかった。
        if not _same_deciders(_dec_raw.get("decided_by"), decided_by):
            raise JournalError(
                "決定ファイルの判断者と、合意の判断者が違います"
                f"（{str(_dec_raw.get('decided_by'))[:30]} ／ "
                f"{str(decided_by)[:30]}）")
    elif isinstance(ops, list):
        raise JournalError(
            "操作の配列ではなく、決定ファイルのパスを渡してください"
            "（合意した中身と、実際に当てる中身を同じものにするため）")
    if not ops:
        raise JournalError("やる操作がありません")
    for o in ops:
        if o.get("op") not in ALLOWED_OPS:
            raise JournalError(
                f"選べない操作です: {o.get('op')!r}（選べるのは {ALLOWED_OPS}）")
        if not o.get("why"):
            raise JournalError("理由の無い操作は受け取りません")

    # ★★封をした判定を、ここで取り直して確かめる★★
    #   （2026-08-27・Codexの指摘7）
    #   ★直す前は、封をしたときの指紋を控えるだけだった★ので、
    #   ★Codexの答えを見たあとで判定ファイルを書き換えても気づけなかった★
    #   ＝「2AIが一致した」が自己申告になっていた（封をした意味が無い）。
    _vp = str(rec.get("claude_verdict_path") or "")
    _want = str(rec.get("claude_verdict_sha256") or "")
    if not _vp or not _want:
        raise JournalError("Claudeの判定に封がされていません")
    if not os.path.exists(_vp):
        raise JournalError(f"封をした判定のファイルがありません: {_vp}")
    with io.open(_vp, encoding="utf-8") as _f:
        _now = _sha(_f.read())
    if _now != _want:
        raise JournalError(
            "封をしたあとで、Claudeの判定が書き換えられています"
            f"（{_want[:12]}… → {_now[:12]}…）")
    if not rec.get("codex_verdict_sha256"):
        raise JournalError("Codexの判定を受け取っていません")

    import recheck as _r
    spec = _r.CHECKS.get(recheck_name)
    if not spec:
        raise JournalError(f"そんな検査はありません: {recheck_name!r}")
    if not spec.get("closeable"):
        raise JournalError(
            f"{recheck_name} は観測どまりの検査です。"
            "直したことを機械で確かめられない型なので、自動では触りません")

    # ★★目印にした一文を、実際に触る決定であること★★
    #   （2026-08-29・台帳#499の案B／自分で再現した）
    #   ★直す前は、その件の逐語を1文字も触らない決定でも合意できた★ので、
    #   そのまま押し切れて「押し切ったのに直っていない」状態になり、
    #   ★前にも後ろにも進めない件ができた★（basilisk_tenzen で実際に発生）。
    #   ★言葉の意味は見ない★（それは2AIの仕事）＝
    #   「消す対象がその一文か」「書き換えの前がその一文か」だけを文字で見る。
    #   ★既存の守りを全部通してから最後に見る★（順番を変えないため）
    _q = str(rec.get("quote") or "").strip()
    if _q:
        _touch = any(str(o.get("text") or "").strip() == _q
                     or str(o.get("before") or "").strip() == _q
                     for o in ops)
        if not _touch:
            raise JournalError(
                "この決定は、指摘された一文を触っていません"
                f"（{_q[:36]}…）。"
                "★触らない決定で合意すると、押し切ったのに直っていない件が"
                "できて、あとから誰も直せなくなります★")

    return _step(rec, "AGREED", "2AIが一致した",
                 ops=ops, ops_sha256=decision_digest(_dec_raw),
                 recheck={"name": recheck_name,
                          "version": spec["version"]},
                 decided_by=list(decided_by))


def applied(fid: str, after_sha256: str) -> dict:
    rec = load(fid)
    return _step(rec, "APPLIED", "記事データを書き換えた",
                 after_sha256=after_sha256)


def commit_verified(fid: str, commit: str) -> dict:
    rec = load(fid)
    if not re.fullmatch(r"[0-9a-f]{7,40}", commit or ""):
        raise JournalError(f"コミットの形が違います: {commit!r}")
    return _step(rec, "COMMIT_VERIFIED", "差分とコミットが結び付いた",
                 commit=commit)


def _pushed(commit: str) -> tuple:
    """★そのコミットが、GitHubのmainへ出してあるか★

    ★これは「読者に届いた」ではない★（2026-08-29・Codexのレビュー18）
      配信は GitHub Actions が非同期でやるので、
      出してあることと届いていることは別。届いたかは `_delivered`。

    ★返すもの★＝(出してある?, 理由)
    """
    full = _full_sha(commit)
    if not full:
        return False, f"コミットを特定できません: {str(commit)[:12]}"
    try:
        import prepush_gate as _pg
        tip, why = _pg.remote_main_tip()
    except Exception as e:                                   # noqa: BLE001
        return False, f"公開先の先端を調べられません: {type(e).__name__}"
    if not tip:
        return False, why
    return _is_ancestor(full, tip, "はまだ出ていません")


def _delivered(commit: str) -> tuple:
    """★そのコミットが、いま読者に届いている中身に含まれているか★
       （2026-08-29・Codexのレビュー18・重大）

    ★返すもの★＝(届いている?, 理由, (いま届いているコミット, 配信の番号))

    ★★配信の番号まで見る★★（2026-08-29・Codexのレビュー19）
      ★同じコミットでも、出す中身は別のことがある★
      （リポジトリをそのまま出す配信と、組み立てた中身を出す配信）。
      コミットだけを見ていると、同じコミットの別の配信に切り替わっても
      気づけない。番号まで揃って初めて「同じ配信を検査した」と言える。

    ★★祖先でよい／ただし検査はいま届いている中身でやる★★（レビュー18・中②）
      「記録したコミットそのものが先端」を求めるのは★厳しすぎた★＝
      直したあとに**無関係な正常コミットD**が乗るだけで、
      直っていても永久に進めなくなる（コミットを結び直す口も無い）。
      正しい条件は
        ・直しのコミットCが、いま届いているPの★祖先★であること
        ・★Pの中身で★検査が合格すること
      これなら、Dで再発していれば検査が落ち、
      無関係なDならふつうに閉じられる。
    """
    full = _full_sha(commit)
    if not full:
        return False, f"コミットを特定できません: {str(commit)[:12]}", ("", 0)
    try:
        import prepush_gate as _pg
        tip, why, dep_id = _pg.deployed_tip()
    except Exception as e:                                   # noqa: BLE001
        return False, f"配信の記録を読めません: {type(e).__name__}", ("", 0)
    if not tip:
        return False, why, ("", 0)
    ok, why2 = _is_ancestor(full, tip, "はまだ読者に届いていません")
    return ok, why2, ((tip, dep_id) if ok else ("", 0))


def _is_ancestor(full: str, tip: str, ng_word: str) -> tuple:
    """★その先端に、このコミットが含まれているか★（確かめられなければ止める）"""
    import subprocess
    try:
        r = subprocess.run(["git", "merge-base", "--is-ancestor", full, tip],
                           cwd=BASE, capture_output=True, text=True,
                           timeout=60)
        rc = r.returncode
    except Exception as e:                                   # noqa: BLE001
        return False, f"含まれているか確かめられません: {type(e).__name__}"
    if rc == 1:
        return False, f"{full[:8]} {ng_word}（いま {tip[:8]}）"
    if rc != 0:
        # ★確かめられなかっただけのものを「出ていない」と断定しない★
        return False, (f"{full[:8]} が {tip[:8]} に含まれるか"
                       f"確かめられません（終了値 {rc}）")
    return True, ""


def _full_sha(commit: str) -> str:
    """★40桁へそろえる★（曖昧・不正・実在しないなら空＝fail-closed）

    ★★40桁でも存在を確かめる★★（2026-08-29・Codexのレビュー18・軽微④）
      ★直す前は40桁ならそのまま返していた★ので、
      実在しない "0"*40 も有効な答えになっていた
      （「無いコミットは空を返す」という説明と食い違っていた）。
    """
    c = str(commit or "")
    if not re.fullmatch(r"[0-9a-f]{7,40}", c):
        return ""
    import subprocess
    try:
        rp = subprocess.run(["git", "rev-parse", "--verify", "--quiet",
                             c + "^{commit}"], cwd=BASE,
                            capture_output=True, text=True, timeout=60)
    except Exception:                                        # noqa: BLE001
        return ""
    out = (rp.stdout or "").strip()
    return out if rp.returncode == 0 and re.fullmatch(
        r"[0-9a-f]{40}", out) else ""


# ★配信が終わるのを待つ長さ★（2026-08-29）
#   ★実測13〜17秒★だが、混んでいれば延びる。
#   ★問い合わせの回数に上限がある★（認証なしで60回/時）ので、
#   間隔は広めにして回数を抑える（1件あたり最大4回＝約12問い合わせ）。
DELIVER_WAIT_SECONDS = 120
DELIVER_POLL_SECONDS = 40


def _delivered_wait(commit: str, wait_seconds=None, sleep=None) -> tuple:
    """★届くまで少しだけ待ってから答える★（2026-08-29）

    ★なぜ待つのか★＝更新タスクは `git push` の直後にここを呼ぶ。
    配信は非同期なので、その瞬間はまだ進行中で、
    読者はひとつ前の版を見ている。
    ★待たないと、どの直しも最後の一歩へ届かず、記録が置き去りになる★
    （手順書に「途中から再開する」段取りは無い）。

    ★待つのは有限★＝時間切れになったら、いままでどおり理由を返して断る。
    ★待っても駄目な理由（配信が失敗した・組み立てた中身が出ている）は
      待っても変わらないが、区別せずに待つ★＝
      **理由の文で分岐すると、文言を変えるたびに壊れる**（既存の決まり）。
    """
    import time as _t
    budget = (DELIVER_WAIT_SECONDS if wait_seconds is None
              else int(wait_seconds))
    slp = sleep or _t.sleep
    while True:
        ok, why, mark = _delivered(commit)
        if ok or budget <= 0:
            return ok, why, mark
        budget -= DELIVER_POLL_SECONDS
        slp(DELIVER_POLL_SECONDS)


def _recheck_mod():
    """★検査の道具を差し替えられるようにする入口★（試験用の継ぎ目）"""
    import recheck
    return recheck


def push_confirmed(fid: str) -> dict:
    """★push できたことを確かめてから★（Codexの設計レビュー）

    > 台帳のcloseは、push確認と再検査PASSの後であるべきで、
    > ローカルコミット直後ではありません。
    """
    rec = load(fid)
    ok, why = _pushed(rec.get("commit"))
    if not ok:
        raise JournalError(why)
    return _step(rec, "PUSH_CONFIRMED", "pushを確かめた")


def recheck_pass(fid: str, wait_seconds=None, sleep=None) -> dict:
    """★機械が自分で検査をやり直して合格したときだけ進む★

    結果の辞書を受け取らない（＝偽の合格を作れない）。

    ★★合格は「実際に出したもの」で決める★★（2026-08-29・台帳#501）
      ★直す前は、いまの作業ツリーで検査をやり直すだけだった★ので、
      ・未コミットの変更が残ったまま
      ・あとから積んだ別のコミットの中身
      でも「合格」と記録できた（＝出したものとは別の中身）。
      いまは3つを確かめる。どれも★記録を信じず、その場でやり直す★。
        ①そのコミットが本当に出してあるか（`_pushed`）
        ②記録されたコミットが、いまの先端であること
        ③未コミットの変更が無いこと＋検査のやり直しが合格すること
      ②③は `recheck.closeable()` が既に持っている規則なので、
      ★ここで書き直さずに、それを通す★（同じ規則を2か所に書かない）。

    ★★分かっている限界★★（2026-08-29・Codexのレビュー17）
      `closeable()` が見るのは**検査の前と後**なので、
      ★検査の最中だけ中身を差し替えて、終わる前に戻す形は捕まえない★。
      「検査中に動いていないことを見ている」は言い過ぎだった。
      本当に塞ぐなら、記録したコミットから作った隔離された写しの上で
      検査する必要がある（★別の設計の話なので、ここではやらない★）。
    """
    rec = load(fid)
    name = (rec.get("recheck") or {}).get("name")
    if not name:
        raise JournalError("通すべき検査が決まっていません")
    commit = str(rec.get("commit") or "")
    # ★①記録された段階を信じず、いま自分で確かめ直す★
    #   ★見るのは「出したか」ではなく「届いたか」★（レビュー18・重大）
    # ★★入口では、配信が終わるのを少しだけ待つ★★（2026-08-29）
    #   push の直後に呼ばれるので、待たないと必ず時期尚早で断る。
    ok, why, mark = _delivered_wait(commit, wait_seconds=wait_seconds,
                                    sleep=sleep)
    if not ok:
        raise JournalError(f"読者に届いたことを確かめられません: {why}")
    tip, dep_id = mark
    _r = _recheck_mod()
    meta = (_r.CHECKS or {}).get(name)
    if not meta:
        raise JournalError(f"知らない検査です: {name}")
    args = {"slug": rec["slug"]}
    if "text" in (meta.get("args_spec") or {}):
        args["text"] = rec["quote"]
    # ★★合意したときの検査の版を渡す★★（Codexのレビュー17・中）
    #   ★直す前はいまの版を渡していた★ので、
    #   `closeable()` の「合意後に検査が変わったら閉じない」が
    #   ★常に自己一致になり、まったく効いていなかった★。
    want_ver = (rec.get("recheck") or {}).get("version")
    # ★★検査するのは「いま届いている中身」★★（レビュー18・中②）
    #   直しのコミットではなく、配信されているコミットで見る。
    #   ＝そのあと別のコミットで再発していれば、ここで落ちる。
    ok2, why2, got = _r.closeable({"check": name,
                                   "version": want_ver,
                                   "args": args,
                                   "expected_commit": tip})
    if not ok2:
        raise JournalError(f"{name} を合格にできません: {why2}")
    # ★★検査のあいだに配信が進んでいないか、もう一度見る★★
    #   （レビュー18・中③）前だけ見ていると、検査中に別の中身が
    #   届いていても、古い判断のまま「直った」と記録できる。
    ok3, why3, mark2 = _delivered(commit)
    if not ok3 or mark2 != mark:
        raise JournalError(
            f"検査のあいだに配信が変わりました（{tip[:8]}/{dep_id} → "
            f"{(mark2[0] or '不明')[:8]}/{mark2[1]}）: {why3}")
    # ★★この記録の意味★★（2026-08-29・Codexのレビュー19・軽微）
    #   「★この配信（番号つき）を検査して合格した★」まで。
    #   保存したあとに別の配信が成功して再発する余地は残るが、
    #   それは**あとの配信で再発した**のであって、
    #   この配信についての偽りではない。
    return _step(rec, "RECHECK_PASS", f"{name} が合格した",
                 recheck_result=got,
                 verified_deploy={"sha": tip, "deployment_id": dep_id})


def done(fid: str, closed_issues=None) -> dict:
    rec = load(fid)
    return _step(rec, "DONE", "終わり",
                 closed_issues=list(closed_issues or []))


def _recheck_failing(rec: dict) -> bool:
    """★その件の検査が、いま落ちているか★（2026-08-29・台帳#499）

    ★自分で確かめ直す★＝記録された結果を信じない
    （偽の合格・偽の不合格を作れないようにする）。
    ★読めないときは False★＝分からないものを「開けてよい」にしない。
    """
    try:
        import recheck as _rc
        got = _rc.run(str(rec.get("check") or ""),
                      {"slug": rec.get("slug"), "text": rec.get("quote")})
        return str(got.get("result")) == "FAIL"
    except Exception:                      # noqa: BLE001
        return False


def attempt(fid: str, why: str) -> dict:
    """決まらなかった回を数える。★3回で人へ回す★

    ★数えないもの★（Codexの設計レビュー）
      利用制限・時間切れ・ロックを取れなかった、は「1回」に数えない。
      判断をやってみて決まらなかった回だけを数える。
    """
    rec = load(fid)
    # ★★判断をやってみた後でなければ、回数に数えない★★
    #   （2026-08-27・Codexの2回目の指摘3）
    #   ★直す前は状態を見ていなかった★ので、
    #   ★封もCodexの受け取りもせずに3回呼べば人へ回せた★
    #   ＝「3回やっても決まらなかった」が嘘になる。
    #   （さらに APPLIED や DONE からでも呼べて、DETECTED へ戻せた）
    # ★★押し切ったのに直っていない件は、やり直せる★★
    #   （2026-08-29・台帳#499／自分で再現した）
    #   段階を進める applied / commit_verified / push_confirmed は
    #   「その件の検査に通ったか」を見ていない（見るのは最後の1手前だけ）。
    #   ＝「直した」と「押し切った」が別々に進むので、
    #   ★目印の一文を直さないまま push まで行ける★。
    #   そこから先は RECHECK_PASS しか無く、検査は FAIL なので進めず、
    #   やり直しも断られて★誰にも直せない★状態になっていた（実際に1件できた）。
    #   ★開けてよいのは「検査が FAIL のとき」だけ★＝
    #   合格した件（DONE）や、まだ判断していない件は今までどおり断る。
    _after = ("AGREED", "APPLIED", "COMMIT_VERIFIED", "PUSH_CONFIRMED")
    if rec.get("state") in _after and _recheck_failing(rec):
        pass                              # ★やり直しを認める★
    elif rec.get("state") != "CODEX_RECEIVED":
        raise JournalError(
            f"まだ判断していません（いま {rec.get('state')}）。"
            "Claudeの判定に封をして、Codexの判定を受け取ってから数えます"
            "／仕組みの都合なら infra_failure を使ってください")
    rec["attempts"] = int(rec.get("attempts") or 0) + 1
    rec.setdefault("history", []).append(
        {"to": rec["state"], "note": f"決まらなかった（{rec['attempts']}回目）: {why}"})
    _save(rec)
    if rec["attempts"] >= MAX_ATTEMPTS:
        return _step(rec, ESCALATED, f"{MAX_ATTEMPTS}回やっても決まらなかった")
    # ★★次の回をやり直せるように、はじめへ戻す★★
    #   （2026-08-27・Codexの指摘8）
    #   ★直す前は段階がそのまま（CODEX_RECEIVED）だった★ので、
    #   段階は飛ばせない・戻れない決まりにより
    #   ★2回目のClaude封印へ入れず、「3回やる」が実行不能だった★。
    #   ＝1回で決まらなければ、その件は永久に止まる。
    #   ★封と受け取りは捨てる★＝次の回は材料を増やしてやり直すので、
    #   前の回の判定を持ち越すと「答えを見てから書いた」を防げない。
    for k in ("claude_verdict_sha256", "claude_verdict_path",
              "codex_material_sha256", "codex_verdict_sha256"):
        rec.pop(k, None)
    rec["state"] = "DETECTED"
    rec.setdefault("history", []).append(
        {"to": "DETECTED", "note": "次の回のためにはじめへ戻した（材料を増やす）"})
    _save(rec)
    return rec


def infra_failure(fid: str, why: str) -> dict:
    """★仕組みの都合で進めなかった★（回数に数えない）"""
    rec = load(fid)
    rec.setdefault("history", []).append(
        {"to": rec["state"], "note": f"仕組みの都合で中断（回数に数えない）: {why}"})
    _save(rec)
    return rec


# --- 一覧 -----------------------------------------------------------------

def _broken_why(rec):
    """記録として成り立っていない理由（無ければ空）。2026-08-27・指摘6。

    ★「JSONとして読めた」＝「記録として正しい」ではない★。
    """
    if not isinstance(rec, dict):
        return f"辞書ではありません（{type(rec).__name__}）"
    if rec.get("schema_version") != SCHEMA:
        return f"知らない版です（{rec.get('schema_version')!r}）"
    for k in ("finding_id", "slug", "check", "quote", "state"):
        if not rec.get(k):
            return f"{k} がありません"
    if rec.get("state") not in FLOW and rec.get("state") != ESCALATED:
        return f"知らない段階です（{rec.get('state')!r}）"
    if len(str(rec.get("source_sha256") or "")) != 64:
        return "記事の指紋がありません"
    # ★★段階ごとに、そこまでで揃っているはずの欄を見る★★
    #   （2026-08-27・Codexの3回目の指摘5）
    #   ★直す前は5欄だけ見ていた★ので、
    #   「AGREED なのに合意の中身が空」でも健康扱いだった。
    _need = {"CLAUDE_SEALED": ("claude_verdict_sha256",),
             "CODEX_RECEIVED": ("claude_verdict_sha256",
                                "codex_verdict_sha256"),
             # ★操作の指紋は入れない★（2026-08-27）
             #   ★今日足した欄なので、それ以前の記録が全部
             #     「壊れている」ことになり、全機種が止まった★。
             #   指紋が無いのは壊れているのではなく**古い**。
             #   ★合意との突き合わせ側が、指紋の無い記録を既に断る★ので、
             #   その機種だけが止まる（止める範囲が正しくなる）。
             "AGREED": ("claude_verdict_sha256", "codex_verdict_sha256",
                        "ops", "recheck", "decided_by")}
    _st = rec.get("state")
    _order = list(FLOW)
    for _s, _keys in _need.items():
        # ★その段階を通り過ぎていれば、欄は揃っているはず★
        if _st != ESCALATED and _st in _order \
                and _order.index(_st) >= _order.index(_s):
            for _k in _keys:
                if not rec.get(_k):
                    return f"{_st} なのに {_k} がありません"
    return ""


def listing(state: str | None = None) -> list:
    out = []
    if not os.path.isdir(_store()):
        return out
    for n in sorted(os.listdir(_store())):
        if not n.endswith(".json"):
            continue
        try:
            with io.open(os.path.join(_store(), n), encoding="utf-8") as f:
                rec = json.load(f)
        except Exception as e:                               # noqa: BLE001
            # ★★壊れた記録を黙って外さない★★（2026-08-27・Codexの指摘10）
            #   ★直す前は黙って飛ばしていた★ので、
            #   ★途中まで進んでいた直しが一覧から消えて、誰も気づけなかった★。
            #   一覧は「翌朝そこから続ける」ための入口なので、
            #   消えるとその件は永久に止まる。
            out.append({"state": "BROKEN", "finding_id": n[:-5],
                        "slug": "", "check": "", "quote": "",
                        "_broken": f"{type(e).__name__}: {str(e)[:80]}"})
            continue
        # ★★形が正しくても、中身が壊れていれば BROKEN★★
        #   （2026-08-27・Codexの2回目の指摘6）
        #   ★直す前は「JSONとして読めたか」だけ見ていた★ので、
        #   空の辞書・知らない版・欄の欠けは**黙って一覧から消えた**。
        _why = _broken_why(rec)
        # ★★ファイル名と中身の名前が食い違っていたら壊れている★★
        #   （2026-08-27・Codexの3回目の指摘5）
        if not _why and rec.get("finding_id") != n[:-5]:
            _why = (f"ファイル名（{n[:-5]}）と中身の名前"
                    f"（{rec.get('finding_id')}）が食い違います")
        if _why:
            out.append({"state": "BROKEN", "finding_id": n[:-5],
                        "slug": "", "check": "", "quote": "",
                        "_broken": _why})
            continue
        if state and rec.get("state") != state:
            continue
        out.append(rec)
    return out


def _selftest() -> int:
    import shutil
    import tempfile
    ng = []

    ran = [0]

    def t(name, cond):
        ran[0] += 1
        print(("OK   " if cond else "NG   ") + name)
        if not cond:
            ng.append(name)

    td = tempfile.mkdtemp()
    keep = globals()["STORE"]
    globals()["STORE"] = td

    def _decfile(fid, slug, actions, sha="a" * 64, name="dec"):
        """★本物の決定ファイルを書く★（2026-08-27・Codexの指摘6）

        ★合意は、これを読む★＝AIが打ち直した配列は受け取らない。
        """
        # ★記録の置き場には書かない★（2026-08-27）
        #   ★同じ場所に置いたら、一覧が決定ファイルまで拾った★
        #   ＝試験が実際に落ちて分かった。
        _dd = os.path.join(td, "decisions")
        os.makedirs(_dd, exist_ok=True)
        p = os.path.join(_dd, f"{name}.json")
        io.open(p, "w", encoding="utf-8").write(json.dumps(
            {"schema_version": "decide-now/v1", "slug": slug,
             "finding_id": fid, "source_sha256": sha,
             "decided_by": ["Claude", "codex"], "actions": actions},
            ensure_ascii=False))
        return p
    try:
        r = detect("hokuto", "text_gone", "この文はおかしいです。", "sections[0].body[2]",
                   source_sha256="a" * 64, detail="文体")
        fid = r["finding_id"]
        t("　見つけたら記録できる", r["state"] == "DETECTED")
        t("★同じ問題を二重に立てない★",
          detect("hokuto", "text_gone", "この文はおかしいです。",
                 "sections[0].body[2]",
                 source_sha256="a" * 64)["finding_id"] == fid)
        # ★★記事の指紋は必ず要る★★（2026-08-27・Codexの指摘11）
        #   ★直す前はこの試験自身が指紋なしで呼んでいた★＝穴の実演。
        try:
            detect("zzz", "text_gone", "指紋なしで立てます。")
            t("★★記事の指紋なしでは記録できない★★", False)
        except JournalError as e:
            t("★★記事の指紋なしでは記録できない★★", "指紋" in str(e))
        t("★番号は毎回同じ★（HEADで見つけ直しても一致する）",
          finding_id("hokuto", "text_gone", "この文はおかしいです。",
                     "sections[0].body[2]") == fid)

        # 段階を飛ばせない
        try:
            _step(load(fid), "AGREED", "飛ばす")
            t("★★段階を飛ばせない★★", False)
        except JournalError as e:
            t("★★段階を飛ばせない★★", "飛ばせません" in str(e))

        # 封をする前にCodexを受け取れない
        try:
            record_codex(fid, "b" * 64, "Codexの判定です。" * 3)
            t("★★Claudeの判定に封をする前はCodexを受け取れない★★", False)
        except JournalError as e:
            t("★★Claudeの判定に封をする前はCodexを受け取れない★★",
              "封をして" in str(e))

        vp = os.path.join(td, "verdict.md")
        io.open(vp, "w", encoding="utf-8").write(
            "私の判定: この文は前の段落と同じ内容なので消してよいと考えます。")
        rec = seal_claude(fid, vp)
        t("　Claudeの判定に封をできる",
          rec["state"] == "CLAUDE_SEALED" and len(rec["claude_verdict_sha256"]) == 64)

        # ★対照実験★ 封のあと中身を書き換えても指紋は変わらない＝すり替えが分かる
        sealed = rec["claude_verdict_sha256"]
        _orig = "私の判定: この文は前の段落と同じ内容なので消してよいと考えます。"
        io.open(vp, "w", encoding="utf-8").write(
            "私の判定: やっぱりCodexと同じで、こちらを残すべきでした。")
        again = _sha(io.open(vp, encoding="utf-8").read())
        t("★★封のあと判定を書き換えたら指紋が食い違う★★", sealed != again)
        # ★示したので元へ戻す★（2026-08-27）
        #   ★戻さないと、この先の合意が新しい守りに正しく止められる★
        #   （＝守りが効いている証拠だが、この試験の目的は別のところにある）。
        io.open(vp, "w", encoding="utf-8").write(_orig)

        record_codex(fid, "b" * 64, "Codexの判定です。同じく消してよいと考えます。")

        # 閉じられない検査では合意できない
        try:
            agree(fid, _decfile(fid, "hokuto",
                                [{"op": "drop", "text": "指紋なしで立てます。", "why": "重複"}]),
                  "strategy_vs_checker", ["Claude", "codex"])
            t("★★閉じられない検査では合意できない★★", False)
        except JournalError as e:
            t("★★閉じられない検査では合意できない★★", "観測どまり" in str(e))

        # 知らない操作
        try:
            agree(fid, _decfile(fid, "hokuto",
                                [{"op": "rewrite", "why": "…"}]),
                  "text_gone", ["Claude", "codex"])
            t("　選べない操作は受け取らない", False)
        except JournalError:
            t("　選べない操作は受け取らない", True)

        # 判断者1人
        try:
            agree(fid, _decfile(fid, "hokuto",
                                [{"op": "drop", "text": "指紋なしで立てます。", "why": "重複"}]),
                  "text_gone", ["Claude"])
            t("　判断者が1人では合意にしない", False)
        except JournalError:
            t("　判断者が1人では合意にしない", True)

        _ops_ok = [{"op": "drop", "text": "この文はおかしいです。",
                    "why": "前の段落と同じ内容"}]
        rec = agree(fid, _decfile(fid, "hokuto", _ops_ok),
                    "text_gone", ["Claude", "codex"])
        t("　2AIが一致したら合意になる", rec["state"] == "AGREED")

        applied(fid, "c" * 64)
        try:
            commit_verified(fid, "ぜんぜん違う")
            t("　コミットの形を見る", False)
        except JournalError:
            t("　コミットの形を見る", True)
        commit_verified(fid, "0" * 40)

        # push されていないコミットでは進めない
        try:
            push_confirmed(fid)
            t("★★pushを確かめるまで先へ進めない★★", False)
        except JournalError as e:
            # ★理由の言い回しは変わり得るので、進んでいないことも見る★
            t("★★pushを確かめるまで先へ進めない★★",
              load(fid)["state"] == "COMMIT_VERIFIED" and bool(str(e)))

        # ★★開け直してよいのは「検査が落ちているとき」だけ★★
        #   （2026-08-29・台帳#499）
        #   ★押し切ったのに直っていない件★は開け直せる（袋小路を作らない）。
        #   ★けれど検査が通っている件まで開け直せてはいけない★
        #   （合格した直しを、あとから開け直せてしまう）。
        _ng_open = False
        try:
            attempt(fid, "検査は落ちていないのに開け直そうとする")
        except JournalError as _e:
            _ng_open = "まだ判断していません" in str(_e)
        t("★★検査が落ちていない件は、開け直せない★★"
          "／★合格した直しを、あとから開け直せてはいけない★", _ng_open)

        # ★★直しの「合格」は、実際に出したもので決める★★
        #   （2026-08-29・台帳#501・Codexの指摘1）
        #   ★直す前は、いまの作業ツリーで検査をやり直すだけだった★ので、
        #   ・未コミットの変更が残ったまま
        #   ・あとから積んだ別のコミットの中身
        #   でも「合格」と記録できた（＝出したものとは別の中身）。
        #   ★recheck_pass には試験が1つも無かった★＝
        #   手前の push_confirmed が本物の git で必ず落ちるので、
        #   ここまで一度も到達していなかった（罠④）。
        f501 = detect("hokuto", "text_gone", "出したものと結び付けます。",
                      "sections[0].body[9]",
                      source_sha256="a" * 64)["finding_id"]
        vp5 = os.path.join(td, "verdict501.md")
        io.open(vp5, "w", encoding="utf-8").write(
            "私の判定: この文は前の段落と同じ内容なので消してよいと考えます。")
        seal_claude(f501, vp5)
        record_codex(f501, "b" * 64,
                     "Codexの判定です。同じく消してよいと考えます。")
        agree(f501, _decfile(f501, "hokuto",
                             [{"op": "drop",
                               "text": "出したものと結び付けます。",
                               "why": "前の段落と同じ内容"}],
                             name="dec501"),
              "text_gone", ["Claude", "codex"])
        applied(f501, "c" * 64)
        commit_verified(f501, "1" * 40)

        class _FakeRecheck:
            # ★記録された版と見分けるために、わざと違う値にする★
            CHECKS = {"text_gone": {"version": 999, "closeable": True,
                                    "args_spec": {"slug": (str, True, ()),
                                                  "text": (str, True, ())}}}
            asked = []
            verdict = (False, "作業ツリーに未コミットの変更があります", None)

            @classmethod
            def closeable(cls, cond):
                cls.asked.append(cond)
                return cls.verdict

            @staticmethod
            def run(name, args):
                # ★★これを見て合格にしてはいけない★★
                #   直す前の実装はこちらを見ていたので、
                #   偽の合格でも RECHECK_PASS へ進めた。
                return {"result": "PASS", "detail": "偽の合格"}

        _keep_pushed = globals()["_pushed"]
        _keep_deliv = globals()["_delivered"]
        _keep_mod = globals()["_recheck_mod"]
        try:
            globals()["_pushed"] = lambda c: (False,
                                              "まだ origin にありません")
            globals()["_recheck_mod"] = lambda: _FakeRecheck
            _ng1 = ""
            try:
                push_confirmed(f501)
            except JournalError as e:
                _ng1 = str(e)
            t("★★出していないコミットでは、pushを確かめたことにしない★★",
              "origin" in _ng1)

            globals()["_pushed"] = lambda c: (True, "")
            push_confirmed(f501)

            # ★★出しただけでは足りない。届いたかを見る★★
            #   （2026-08-29・Codexのレビュー18・重大）
            #   ★このサイトは GitHub Actions が配信する★ので、
            #   mainへ出した直後でも読者はまだ古い中身を見ている。
            #   それを「公開済み」と数えると、
            #   ★直っていないものを「直った」と記録できる★。
            globals()["_delivered"] = lambda c: (
                False, "いま生きている成功した配信が見つかりません",
                ("", 0))
            _ngd = ""
            try:
                # ★ここは「待っても駄目」を見る試験なので待たせない★
                recheck_pass(f501, wait_seconds=0)
            except JournalError as e:
                _ngd = str(e)
            t("★★出しただけで、まだ届いていなければ合格にしない★★"
              "／★mainへ出した＝読者に届いた、ではない★",
              "届いたこと" in _ngd
              and load(f501)["state"] == "PUSH_CONFIRMED")

            # ★★検査するのは「いま届いている中身」★★（レビュー18・中②）
            #   ★直しのコミットそのものを求めると、無関係な正常コミットが
            #     後から乗るだけで永久に進めなくなる★（実際そうしていた）。
            _TIP = "d" * 40
            globals()["_delivered"] = lambda c: (True, "", (_TIP, 9))
            _ng2 = ""
            try:
                recheck_pass(f501, wait_seconds=0)
            except JournalError as e:
                _ng2 = str(e)
            t("★★手元の状態だけで合格にしない★★"
              "／★未コミットの変更や別のコミットの中身で「直した」に"
              "できてはいけない★",
              "未コミット" in _ng2
              and load(f501)["state"] == "PUSH_CONFIRMED")
            t("★★検査するのは、いま届いているコミットの中身★★"
              "／★直しのコミットで見ると、そのあと再発していても"
              "気づけない★",
              bool(_FakeRecheck.asked)
              and _FakeRecheck.asked[-1].get("expected_commit") == _TIP
              and _FakeRecheck.asked[-1].get("expected_commit") != "1" * 40)
            t("　★検査の名前と引数は、記録から組み立てる★",
              _FakeRecheck.asked[-1].get("check") == "text_gone"
              and (_FakeRecheck.asked[-1].get("args") or {}).get("text")
              == "出したものと結び付けます。")
            # ★★合意したときの検査の版を渡す★★
            #   （2026-08-29・Codexのレビュー17・中）
            #   ★直す前はいまの版を渡していた★ので、
            #   「合意後に検査が変わったら閉じない」が
            #   ★常に自己一致になり、まったく効いていなかった★。
            _rec_ver = (load(f501).get("recheck") or {}).get("version")
            t("★★合意したときの検査の版を渡す★★"
              "／★いまの版を渡すと「検査が変わったら閉じない」が"
              "効かなくなる★",
              _FakeRecheck.asked[-1].get("version") == _rec_ver
              and _FakeRecheck.asked[-1].get("version") != 999)

            # ★★検査のあいだに配信が変わったら合格にしない★★
            #   （2026-08-29・Codexのレビュー18・中③）
            _FakeRecheck.verdict = (True, "再検査が合格しました",
                                    {"result": "PASS"})
            _seq = [(True, "", (_TIP, 9)), (True, "", (_TIP, 10))]
            globals()["_delivered"] = (
                lambda c, _s=_seq: _s.pop(0) if _s
                else (True, "", (_TIP, 10)))
            _ng4 = ""
            try:
                recheck_pass(f501, wait_seconds=0)
            except JournalError as e:
                _ng4 = str(e)
            t("★★同じコミットでも、検査のあいだに別の配信へ切り替わったら合格にしない★★"
              "／★同じコミットでも出す中身は別のことがある★",
              "配信が変わりました" in _ng4
              and load(f501)["state"] == "PUSH_CONFIRMED")

            # ★対照実験：届いていて、その中身で合格すれば進む★
            globals()["_delivered"] = lambda c: (True, "", (_TIP, 9))
            _rec5 = recheck_pass(f501, wait_seconds=0)
            t("　対照実験：届いていて、その中身で合格すれば進む",
              _rec5["state"] == "RECHECK_PASS")
            t("　★どの配信で確かめたかを番号まで記録に残す★",
              _rec5.get("verified_deploy")
              == {"sha": _TIP, "deployment_id": 9})
            # ★★配信が終わるのを少しだけ待つ★★（2026-08-29・自分で気づいた）
            #   ★このタスクは push の直後にここを呼ぶ★ので、
            #   待たないと配信は必ず進行中で、
            #   ★どの直しも最後の一歩へ届かず記録が置き去りになる★
            #   （手順書に「途中から再開する」段取りは無い）。
            #   ★時間では判定しない★＝何回呼んだか・何回眠ったかで見る。
            _calls, _naps = [0], []
            _late = [(False, "まだ配信中です", ("", 0)),
                     (False, "まだ配信中です", ("", 0)),
                     (True, "", (_TIP, 9))]

            def _deliv_late(c, _s=_late, _n=_calls):
                _n[0] += 1
                return _s[min(_n[0] - 1, len(_s) - 1)]
            globals()["_delivered"] = _deliv_late
            _ok_wait, _why_wait, _mark_wait = _delivered_wait(
                "0" * 40, wait_seconds=120, sleep=lambda s: _naps.append(s))
            t("★★配信が終わるまで少しだけ待つ★★"
              "／★待たないと、どの直しも最後の一歩へ届かない★",
              _ok_wait and _mark_wait == (_TIP, 9)
              and _calls[0] == 3 and _naps == [40, 40])

            _calls[0] = 0
            _naps.clear()
            # ★差し替えた偽物も回数を数える★
            #   （数えないと、何回聞いたかを見る試験が意味をなさない）
            globals()["_delivered"] = (
                lambda c, _n=_calls: (_n.__setitem__(0, _n[0] + 1)
                                      or (False, "まだ配信中です",
                                          ("", 0))))
            _ok_to, _why_to, _ = _delivered_wait(
                "0" * 40, wait_seconds=120, sleep=lambda s: _naps.append(s))
            t("★★待ちきれなければ、理由を返して断る★★"
              "／★いつまでも待たない★",
              _ok_to is False and "配信中" in _why_to
              and _calls[0] == 4 and _naps == [40, 40, 40])

            _calls[0] = 0
            _naps.clear()
            _delivered_wait("0" * 40, wait_seconds=0,
                            sleep=lambda s: _naps.append(s))
            t("　★待たない指定なら、一度だけ聞いて眠らない★",
              _calls[0] == 1 and _naps == [])
        finally:
            globals()["_pushed"] = _keep_pushed
            globals()["_delivered"] = _keep_deliv
            globals()["_recheck_mod"] = _keep_mod

        # ★★短い形の伸ばし方を、本物の git で試す★★
        #   （2026-08-29・Codexのレビュー17）
        #   ★偽物だけで固めると、実際の git の答え方を試していない★
        import shutil as _sh4
        import subprocess as _sp4
        import tempfile as _tf4
        _g4 = _tf4.mkdtemp()
        _keep_base4 = globals()["BASE"]
        try:
            _sp4.run(["git", "init", "-b", "main", _g4],
                     capture_output=True, timeout=60)
            for _a in (("config", "user.email", "t@example.invalid"),
                       ("config", "user.name", "t")):
                _sp4.run(["git", *_a], cwd=_g4, capture_output=True,
                         timeout=60)
            io.open(os.path.join(_g4, "a.txt"), "w",
                    encoding="utf-8").write("1")
            _sp4.run(["git", "add", "a.txt"], cwd=_g4,
                     capture_output=True, timeout=60)
            _sp4.run(["git", "commit", "-m", "one"], cwd=_g4,
                     capture_output=True, timeout=60)
            globals()["BASE"] = _g4
            _sha4 = _sp4.run(["git", "rev-parse", "HEAD"], cwd=_g4,
                             capture_output=True, text=True,
                             timeout=60).stdout.strip()
            t("　本物のgit：短い形を40桁へ伸ばせる",
              _full_sha(_sha4[:8]) == _sha4)
            t("　本物のgit：40桁はそのまま", _full_sha(_sha4) == _sha4)
            t("★★本物のgit：無い形・無いコミットは空を返す★★"
              "／★分からないものを通さない★",
              _full_sha("zzzzzzz") == "" and _full_sha("") == ""
              and _full_sha("1234567") == "")
            # ★★40桁でも存在を確かめる★★
            #   （2026-08-29・Codexのレビュー18・軽微④）
            #   ★直す前は40桁ならそのまま返していた★ので、
            #   実在しない 0 だけの40桁も有効な答えになっていた。
            t("★★本物のgit：40桁でも、実在しなければ空を返す★★"
              "／★説明は「無いコミットは空」なのに食い違っていた★",
              _full_sha("0" * 40) == "")
        finally:
            globals()["BASE"] = _keep_base4
            _sh4.rmtree(_g4, ignore_errors=True)

        # 打ち切り
        f2 = detect("hokuto", "text_gone", "別のおかしな文です。", "x",
                    source_sha256="9" * 64)["finding_id"]
        # ★★本番と同じ順で3回まわす★★（2026-08-27・Codexの2回目の指摘3）
        #   ★直す前は、封もCodexの受け取りもせずに3回数えていた★
        #   ＝この試験そのものが「判断せずに人へ回せる」穴の実演だった。
        # ★例外を受け止めて❌として数える★（2026-08-27）
        #   ★受け止めないと、はじめへ戻す守りを壊したとき
        #     試験そのものが死ぬ★＝構文エラーと区別がつかない。
        _st2 = ""
        try:
            for i in range(MAX_ATTEMPTS):
                seal_claude(f2, vp)
                record_codex(f2, "b" * 64,
                             f"{i + 1}回目のCodexの判定です。" * 2)
                r2 = attempt(f2, f"{i + 1}回目")
            _st2 = load(f2)["state"]
        except JournalError as e:
            _st2 = f"進めません: {e}"
        t("★★3回で人へ回す★★（本番と同じ順で3回まわす）", _st2 == ESCALATED)
        t("　打ち切った後は先へ進めない",
          _try_fail(lambda: seal_claude(f2, vp)))

        f3 = detect("hokuto", "text_gone", "また別の文です。", "y",
                    source_sha256="9" * 64)["finding_id"]
        infra_failure(f3, "利用制限")
        infra_failure(f3, "時間切れ")
        t("★★仕組みの都合は回数に数えない★★",
          load(f3)["attempts"] == 0 and load(f3)["state"] == "DETECTED")

        # ★番号で見る（件数の決め打ちにしない）★
        #   ★件数だけだと、試験を足しただけで落ちる★（罠⑪）
        t("　一覧が引ける",
          {x["finding_id"] for x in listing()} == {fid, f2, f3, f501})
        t("　段階でしぼれる", len(listing(ESCALATED)) == 1)

        # ── 2026-08-27・Codexのレビューで塞いだ穴 ───────────────
        # ⑦封をしたあとに書き換えたら、合意させない
        r7 = detect("zzz7", "text_gone", "七番の文です。",
                    source_sha256="c" * 64)
        f7 = r7["finding_id"]
        v7 = os.path.join(td, "v7.md")
        io.open(v7, "w", encoding="utf-8").write("私の判定です。" * 5)
        seal_claude(f7, v7)
        record_codex(f7, "d" * 64, "Codexの判定です。" * 3)
        io.open(v7, "w", encoding="utf-8").write("あとから書き換えました。" * 3)
        try:
            agree(f7, _decfile(f7, "zzz7",
                               [{"op": "drop", "text": "七番の文です。", "why": "重複"}],
                               sha="c" * 64, name="d7"),
                  "text_gone", ["Claude", "codex"])
            t("★★封のあと判定を書き換えたら合意できない★★", False)
        except JournalError as e:
            t("★★封のあと判定を書き換えたら合意できない★★"
              "／★直す前は指紋を控えるだけで、取り直していなかった★",
              "書き換え" in str(e))

        # ★★指摘された一文を触らない決定では合意できない★★
        #   （2026-08-29・台帳#499の案B／実際に袋小路が1件できた）
        #   ★触らない決定で合意すると、そのまま押し切れて
        #   「押し切ったのに直っていない」件ができ、誰も直せなくなる★
        r7c = detect("zzz7c", "text_gone", "七番cの文です。",
                     source_sha256="e" * 64)
        f7c = r7c["finding_id"]
        v7c = os.path.join(td, "v7c.md")
        io.open(v7c, "w", encoding="utf-8").write("私の判定です。" * 5)
        seal_claude(f7c, v7c)
        record_codex(f7c, "f" * 64, "Codexの判定です。" * 3)
        _ng7c = False
        try:
            agree(f7c, _decfile(f7c, "zzz7c",
                                [{"op": "drop", "text": "関係ない行です。",
                                  "why": "重複"}],
                                sha="e" * 64, name="d7c"),
                  "text_gone", ["Claude", "codex"])
        except JournalError as _e:
            _ng7c = "触っていません" in str(_e)
        t("★★指摘された一文を触らない決定では合意できない★★"
          "／★触らないまま押し切れて、あとから誰も直せない件ができた★",
          _ng7c)

        # ★対照★＝書き換えていなければ合意できる
        r7b = detect("zzz7b", "text_gone", "七番bの文です。",
                     source_sha256="c" * 64)
        f7b = r7b["finding_id"]
        v7b = os.path.join(td, "v7b.md")
        io.open(v7b, "w", encoding="utf-8").write("私の判定です。" * 5)
        seal_claude(f7b, v7b)
        record_codex(f7b, "d" * 64, "Codexの判定です。" * 3)
        _ops7b = [{"op": "drop", "text": "七番bの文です。", "why": "重複"}]
        _a7 = agree(f7b, _decfile(f7b, "zzz7b", _ops7b,
                                  sha="c" * 64, name="d7b"),
                    "text_gone", ["Claude", "codex"])
        t("　（対照）書き換えていなければ合意できる", _a7["state"] == "AGREED")
        # ★★指紋は「決定ファイル全体」★★（2026-08-27・Codexの3回目の指摘3）
        #   ★操作だけだと、合意のあとで numbers_removed を追記して
        #     「記事から数値が消える削除」を免除できた★
        #   （操作は変わっていないので指紋は一致したまま）。
        _d7b = json.load(io.open(os.path.join(td, "decisions", "d7b.json"),
                                 encoding="utf-8"))
        t("★合意した決定の指紋を残す（適用と結び付けるため）★",
          len(str(_a7.get("ops_sha256") or "")) == 64
          and _a7["ops_sha256"] == decision_digest(_d7b))
        t("★★消してよい数値の名指しを足しただけでも指紋が変わる★★"
          "／★操作だけの指紋では、これを免除できた★",
          decision_digest(_d7b)
          != decision_digest({**_d7b,
                              "numbers_removed": [{"n": "500",
                                                   "why": "わざと"}]}))

        # ★★別の件・別の機種の決定ファイルでは合意できない★★
        #   （2026-08-27・Codexの指摘6＝合意と適用の結線）
        r6 = detect("zzz6", "text_gone", "六番の文です。",
                    source_sha256="6" * 64)
        f6 = r6["finding_id"]
        v6 = os.path.join(td, "v6.md")
        io.open(v6, "w", encoding="utf-8").write("私の判定です。" * 5)
        seal_claude(f6, v6)
        record_codex(f6, "6" * 64, "Codexの判定です。" * 3)
        _ops6 = [{"op": "drop", "text": "六番の文です。", "why": "重複"}]
        try:
            agree(f6, _decfile("よその件", "zzz6", _ops6,
                               sha="6" * 64, name="d6a"),
                  "text_gone", ["Claude", "codex"])
            t("★★別の件の決定ファイルでは合意できない★★", False)
        except JournalError as e:
            t("★★別の件の決定ファイルでは合意できない★★"
              "／★直す前は、無害な合意を別の書き換えの許可証にできた★",
              "この件のもの" in str(e))
        try:
            agree(f6, _decfile(f6, "よその機種", _ops6,
                               sha="6" * 64, name="d6b"),
                  "text_gone", ["Claude", "codex"])
            t("　別の機種の決定ファイルでも合意できない", False)
        except JournalError as e:
            t("　別の機種の決定ファイルでも合意できない", "機種が違" in str(e))
        try:
            agree(f6, _decfile(f6, "zzz6", _ops6,
                               sha="7" * 64, name="d6c"),
                  "text_gone", ["Claude", "codex"])
            t("　別の記事に対する判断でも合意できない", False)
        except JournalError as e:
            t("　別の記事に対する判断でも合意できない", "別の記事" in str(e))
        _a6 = agree(f6, _decfile(f6, "zzz6", _ops6,
                                 sha="6" * 64, name="d6d"),
                    "text_gone", ["Claude", "codex"])
        t("　（対照）同じ件・同じ機種・同じ記事なら合意できる",
          _a6["state"] == "AGREED" and len(_a6["ops_sha256"]) == 64)

        # ★★決定ファイルの指紋が空なら断る★★
        #   （2026-08-27・Codexの2回目の指摘5）
        #   ★直す前は「空なら照合しない」だった★ので、
        #   書かなければ「同じ記事を見て作られた」の確認を外せた。
        r5n = detect("zzz5n", "text_gone", "五番nの文です。",
                     source_sha256="5" * 64)
        f5n = r5n["finding_id"]
        v5n = os.path.join(td, "v5n.md")
        io.open(v5n, "w", encoding="utf-8").write("私の判定です。" * 5)
        seal_claude(f5n, v5n)
        record_codex(f5n, "5" * 64, "Codexの判定です。" * 3)
        _p5n = os.path.join(td, "decisions", "d5n.json")
        os.makedirs(os.path.dirname(_p5n), exist_ok=True)
        io.open(_p5n, "w", encoding="utf-8").write(json.dumps(
            {"schema_version": "decide-now/v1", "slug": "zzz5n",
             "finding_id": f5n, "decided_by": ["Claude", "codex"],
             "actions": [{"op": "drop", "text": "五番nの文です。", "why": "重複"}]},
            ensure_ascii=False))
        try:
            agree(f5n, _p5n, "text_gone", ["Claude", "codex"])
            t("★★指紋の無い決定ファイルでは合意できない★★", False)
        except JournalError as e:
            t("★★指紋の無い決定ファイルでは合意できない★★",
              "指紋" in str(e))

        # ★★打ち直した操作の配列は受け取らない★★（2026-08-27）
        #   ★これが無いと、合意と決定ファイルが結び付かない★
        #   （合意はAIが打った配列、適用は決定ファイル＝別物になりうる）
        r6b = detect("zzz6b", "text_gone", "六番bの文です。",
                     source_sha256="8" * 64)
        f6b = r6b["finding_id"]
        v6b = os.path.join(td, "v6b.md")
        io.open(v6b, "w", encoding="utf-8").write("私の判定です。" * 5)
        seal_claude(f6b, v6b)
        record_codex(f6b, "8" * 64, "Codexの判定です。" * 3)
        try:
            agree(f6b, [{"op": "drop", "text": "六番bの文です。", "why": "重複"}],
                  "text_gone", ["Claude", "codex"])
            t("★★打ち直した操作の配列では合意できない★★", False)
        except JournalError as e:
            t("★★打ち直した操作の配列では合意できない★★"
              "／★合意と、実際に当てる決定を同じものにするため★",
              "決定ファイルのパス" in str(e))

        # ★★判断者は「決定ファイルの値」と同じであること★★
        #   （2026-08-27・Codexの4回目の指摘3）
        #   ★直す前は別々に見ていた★ので、記録と決定ファイルで
        #   「誰が決めたか」が食い違っていても分からなかった。
        r6d = detect("zzz6d", "text_gone", "六番dの文です。",
                     source_sha256="d" * 64)
        f6d = r6d["finding_id"]
        v6d = os.path.join(td, "v6d.md")
        io.open(v6d, "w", encoding="utf-8").write("私の判定です。" * 5)
        seal_claude(f6d, v6d)
        record_codex(f6d, "d" * 64, "Codexの判定です。" * 3)
        _p6d = _decfile(f6d, "zzz6d",
                        [{"op": "drop", "text": "六番dの文です。", "why": "重複"}],
                        sha="d" * 64, name="d6e")
        try:
            agree(f6d, _p6d, "text_gone", ["Claude", "だれか別の人"])
            t("★★決定ファイルの判断者と食い違えば合意できない★★", False)
        except JournalError as e:
            t("★★決定ファイルの判断者と食い違えば合意できない★★",
              "判断者が違います" in str(e))
        _a6d = agree(f6d, _p6d, "text_gone", ["Claude", "codex"])
        t("　（対照）同じ顔ぶれなら合意できる", _a6d["state"] == "AGREED")
        t("★★「誰が決めたか」を書き換えると指紋も変わる★★"
          "／★指紋に入れていなかったので、合意後に書き換えられた★",
          decision_digest({"slug": "x", "decided_by": ["Claude", "codex"]})
          != decision_digest({"slug": "x", "decided_by": ["Claude", "ほか"]}))

        # ⑧決まらなかったら、次の回をやり直せる
        r8 = detect("zzz8", "text_gone", "八番の文です。",
                    source_sha256="e" * 64)
        f8 = r8["finding_id"]
        v8 = os.path.join(td, "v8.md")
        io.open(v8, "w", encoding="utf-8").write("私の判定です。" * 5)
        seal_claude(f8, v8)
        record_codex(f8, "f" * 64, "Codexの判定です。" * 3)
        _r8 = attempt(f8, "1回目は決まらなかった")
        t("★★決まらなかったら、はじめへ戻して次の回をやり直せる★★"
          "／★直す前は段階が進んだままで、2回目に入れなかった★",
          _r8["state"] == "DETECTED" and _r8["attempts"] == 1)
        t("　前の回の封と受け取りは捨てる（答えを見てから書けないように）",
          not _r8.get("claude_verdict_sha256")
          and not _r8.get("codex_verdict_sha256"))
        # ★例外を受け止めて❌として数える★（2026-08-27）
        #   ★受け止めないと、壊したときに試験そのものが死ぬ★＝
        #   構文エラーと区別がつかず、守りの証拠にならない。
        try:
            io.open(v8, "w", encoding="utf-8").write("2回目の判定です。" * 5)
            seal_claude(f8, v8)
            record_codex(f8, "ab" * 32, "2回目のCodexの判定です。" * 2)
            _st8 = load(f8)["state"]
        except JournalError as e:
            _st8 = f"進めません: {e}"
        t("　2回目をちゃんと最後まで通せる", _st8 == "CODEX_RECEIVED")
        attempt(f8, "2回目も決まらなかった")
        io.open(v8, "w", encoding="utf-8").write("3回目の判定です。" * 5)
        seal_claude(f8, v8)
        record_codex(f8, "cd" * 32, "3回目のCodexの判定です。" * 2)
        _r8c = attempt(f8, "3回目も決まらなかった")
        t("★3回で人へ回す（回数は数え続ける）★",
          _r8c["state"] == ESCALATED and _r8c["attempts"] == 3)

        # ★★判断していないのに回数を数えさせない★★
        #   （2026-08-27・Codexの2回目の指摘3）
        r3s = detect("zzz3s", "text_gone", "三番sの文です。",
                     source_sha256="a" * 64)
        try:
            attempt(r3s["finding_id"], "判断していないのに数える")
            t("★★判断していないのに回数を数えない★★", False)
        except JournalError as e:
            t("★★判断していないのに回数を数えない★★"
              "／★直す前は、封もCodexもせずに3回呼べば人へ回せた★",
              "まだ判断していません" in str(e))

        # ★★Codexへ渡した材料の指紋は必ず要る★★
        #   （2026-08-27・Codexの3回目の指摘6）
        #   ★直す前は空でも進めた★ので、
        #   「予定した材料をCodexが受け取った」ことを何も確かめていなかった。
        r6m = detect("zzz6m", "text_gone", "六番mの文です。",
                     source_sha256="6" * 64)
        f6m = r6m["finding_id"]
        v6m = os.path.join(td, "v6m.md")
        io.open(v6m, "w", encoding="utf-8").write("私の判定です。" * 5)
        seal_claude(f6m, v6m)
        try:
            record_codex(f6m, "", "Codexの判定です。" * 3)
            t("★★材料の指紋が空では進めない★★", False)
        except JournalError as e:
            t("★★材料の指紋が空では進めない★★", "材料の指紋" in str(e))
        try:
            record_codex(f6m, "zz" * 32, "Codexの判定です。" * 3)
            t("　16進でない指紋も断る", False)
        except JournalError:
            t("　16進でない指紋も断る", True)

        # ★★段階ごとに、そこまでで揃っているはずの欄を見る★★
        #   （2026-08-27・Codexの3回目の指摘5）
        #   ★直す前は5欄だけ見ていた★ので、
        #   「AGREED なのに合意の中身が空」でも健康扱いだった。
        io.open(os.path.join(td, "half_agreed.json"), "w",
                encoding="utf-8").write(json.dumps(
                    {"schema_version": SCHEMA, "finding_id": "half_agreed",
                     "slug": "x", "check": "text_gone", "quote": "x",
                     "source_sha256": "a" * 64, "state": "AGREED"},
                    ensure_ascii=False))
        io.open(os.path.join(td, "wrong_name.json"), "w",
                encoding="utf-8").write(json.dumps(
                    {"schema_version": SCHEMA, "finding_id": "べつの名前",
                     "slug": "x", "check": "text_gone", "quote": "x",
                     "source_sha256": "a" * 64, "state": "DETECTED"},
                    ensure_ascii=False))
        _lst3 = {x.get("finding_id"): x for x in listing()}
        t("★★中身が空の合意は健康扱いしない★★"
          "／★直す前は必須の5欄だけ見ていた★",
          _lst3.get("half_agreed", {}).get("state") == "BROKEN")
        t("　ファイル名と中身の名前が食い違えば壊れている",
          _lst3.get("wrong_name", {}).get("state") == "BROKEN")

        # ★★形は正しいが中身が壊れた記録も BROKEN★★（同・指摘6）
        io.open(os.path.join(td, "empty_rec.json"), "w",
                encoding="utf-8").write("{}")
        io.open(os.path.join(td, "oldver_rec.json"), "w",
                encoding="utf-8").write(
                    '{"schema_version": "repair-journal/v0"}')
        _lst2 = listing()
        t("★★形は正しくても、中身が壊れていれば BROKEN★★"
          "／★直す前は「JSONとして読めたか」だけ見ていた★",
          {"empty_rec", "oldver_rec"}
          <= {x.get("finding_id") for x in _lst2
              if x.get("state") == "BROKEN"})

        # ⑨終わった件と同じ指摘が、記事が変わってから再発したら立て直す
        r9 = detect("zzz9", "text_gone", "九番の文です。",
                    source_sha256="1" * 64)
        f9 = r9["finding_id"]
        _step(load(f9), ESCALATED, "人へ回した")
        t("　同じ記事のままなら、終わった記録をそのまま返す",
          detect("zzz9", "text_gone", "九番の文です。",
                 source_sha256="1" * 64)["state"] == ESCALATED)
        _r9 = detect("zzz9", "text_gone", "九番の文です。",
                     source_sha256="2" * 64)
        t("★★記事が変わってから再発したら、新しく立て直す★★"
          "／★直す前は終わった記録を返すだけで、二度と直せなかった★",
          _r9["state"] == "DETECTED")

        # ⑩壊れた記録を黙って外さない
        io.open(os.path.join(td, "broken_one.json"), "w",
                encoding="utf-8").write("{壊れています")
        _lst = listing()
        # ★どの壊れ方かを名指しで見る★（2026-08-27）
        #   ★直す前は「BROKENが1件でもあれば合格」だった★ので、
        #   読み込みに失敗する記録を黙って外しても、
        #   ★別の壊れ方（中身が壊れている）が拾って緑のままだった★（罠③）。
        t("★★読み込めない記録を黙って外さない★★"
          "／★消えると、途中まで進んでいた直しが誰にも見えなくなる★",
          any(x.get("finding_id") == "broken_one"
              and x.get("state") == "BROKEN" for x in _lst))
    finally:
        globals()["STORE"] = keep
        shutil.rmtree(td, ignore_errors=True)

    # ★実際に試した数を数える★（2026-08-27）
    #   ★直す前は手書きの「18」だった★ので、試験を足しても分母が増えず、
    #   ★足した分が数えられなかった★（監査51が見張っている型）。
    total = ran[0]
    print()
    print(f"{total - len(ng)}/{total} " + ("合格" if not ng else "不合格"))
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def _try_fail(fn) -> bool:
    try:
        fn()
        return False
    except Exception:                                        # noqa: BLE001
        return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--state", default=None)
    ap.add_argument("--show", default=None)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return _selftest()
    if a.show:
        print(json.dumps(load(a.show), ensure_ascii=False, indent=1))
        return 0
    if a.list or a.state:
        rows = listing(a.state)
        if not rows:
            print("記録はありません")
            return 0
        for r in rows:
            print(f"{r['finding_id']}  {r['state']:15} {r['slug']:22} "
                  f"{r['check']:22} {r['quote'][:34]}")
        print(f"\n計 {len(rows)} 件")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

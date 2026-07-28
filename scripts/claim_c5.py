#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""claim_c5.py — 引用文から値を導き出して claim と突き合わせる（意味の検証）

★C5とは何か★
  C0〜C4 は「URLが実在する」「引用がページにある」「値が引用の中にある」までを見る。
  しかしそれだけでは、**引用の中の数字が本当にその項目の値か**が分からない。
  例：「設定1の機械割は97.2%、設定6は106.5%」という引用に対し、
      「設定6の機械割=97.2%」という claim も、値も引用も一致してしまう。

  C5は**引用文を解析して (機種・設定・項目・値・単位) を導き**、
  claim と**完全一致したときだけ PASS** にする。

★設計上の約束（Codex 3回目）★
  - C5の結果を台帳から受け取らない。**必ずここで計算し直す**
  - 最初の数字だけを取らない。曖昧な文字列は FAIL
  - 導出結果が claim と完全一致した場合だけ PASS

使い方:
    python scripts/claim_c5.py --selftest
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata
from decimal import Decimal, InvalidOperation

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 「設定N の 機械割 は X%」という組を、文中から**すべて**拾う
_SETTING = r"(?:設定|設)\s*([1-6])"
_PCT_RAW = r"[0-9]{1,3}(?:\.[0-9]+)?\s*[%％]"
_PCT = r"(" + _PCT_RAW + r")"
_KIKAIWARI_WORD = r"(?:機械割|出玉率)"
_NUM_RE = re.compile(r"[0-9]{1,3}(?:\.[0-9]+)?")

# 設定と値をつなぐ言い回し。★任意の文字は挟ませない★
#   （挟ませると「設定1…（別の話）…106.5%」を組と誤読する）
# ★★組の中に「機械割／出玉率」を必須にする★★（Codex (a)-3）
#   これが任意だと「設定1の勝率は97.2%。設定6の機械割は106.5%」から
#   勝率97.2%を設定1の機械割として導いてしまう。
_WORD = r"(?:機械割|出玉率)"
_CONNECT_LABELED = (r"(?:\s*の?\s*" + _WORD
                    # 「機械割（出玉率）」のような言い換えの併記
                    + r"\s*(?:[（(]\s*" + _WORD + r"\s*[）)])?"
                    + r"\s*[:：はが＝=→]?\s*)")
# 列挙形式の区切り。★空白だけの区切りも許す★（Codex 3巡目 (b)-1）
#   「機械割：設定1 97.2%／設定6 106.5%」を落としていた。
#   残余検査があるので、別項目の語が混ざれば結局落ちる。
_CONNECT_BARE = r"(?:\s*[:：＝=]\s*|\s+)"

# ★★組の書き方は「許可した形」だけ★★（Codex 2巡目 (a)-1）
#   括弧形（「97.2%（設定1）」）にも項目語を必須にする。項目語なしを許すと
#   「設定6の機械割は106.5%。勝率97.2%（設定1）」から勝率を拾ってしまう。
_PAIR_ANY = re.compile(
    # ① 設定1の機械割は97.2%
    r"(?P<s1>(?:設定|設)\s*[1-6])" + _CONNECT_LABELED + r"(?P<p1>" + _PCT_RAW + r")"
    # ② 機械割は97.2%（設定1）
    r"|" + _WORD + r"\s*[はが:：＝=]?\s*(?P<p2>" + _PCT_RAW + r")"
    r"\s*[（(]\s*(?P<s2>(?:設定|設)\s*[1-6])\s*[）)]")

# ③ 列挙形式：「機械割：設定1 97.2% ／ 設定6 106.5%」
#   ★★引用**全体**がこの形であるときだけ使う★★（Codex 4巡目 (a)-1）
#     引用のどこかに「機械割」があればよい、という作りだと
#     「メーカー公表値は設定1 97.2%。機械割は設定6 106.5%」から
#     設定1の公表値を機械割として拾ってしまう。
_PAIR_BARE = re.compile(
    r"(?P<s3>(?:設定|設)\s*[1-6])" + _CONNECT_BARE + r"(?P<p3>" + _PCT_RAW + r")")
_ENUM_SEP = r"(?:[\s、，,・/／|｜]+)"
_ENUM_FULL = re.compile(
    r"\s*[*＊【\[]*\s*" + _WORD
    + r"(?:\s*[（(]\s*" + _WORD + r"\s*[）)])?"
    + r"\s*[*＊】\]]*\s*[:：＝=はが]?\s*"
    + r"(?:" + _PAIR_BARE.pattern.replace("s3", "x3").replace("p3", "y3") + r")"
    + r"(?:" + _ENUM_SEP + _PAIR_BARE.pattern.replace("s3", "x4").replace("p3", "y4")
    + r")*\s*[。]?\s*")

# ★★組を取り除いた残りに、意味を変える語が残っていないこと★★（Codex 2巡目 (a)-2）
#   「禁止語を並べる」方式は、から／ではありません／下回る／推定／概算 のように
#   いくらでも回避できる。**残ってよい語を決め打ちし、それ以外が残ったら拒否**する。
_RESIDUE_OK = re.compile(
    r"(?:機械割|出玉率|"                         # 項目語
    r"です|ます|でした|である|となります|になります|"   # 言い切りの語尾
    r"メーカー公表値|公表値|公称値|"                   # 出所の但し書き
    r"[はがのとやも、。：:／/・|｜，,＝=\s\*＊【】\[\]（）()]"
    r")+")

# 値を1つに確定できない書き方（範囲・比較・条件つき・否定）
_AMBIGUOUS = re.compile(
    # 範囲（97.2%〜106.5% / 97.2%-99.9% / 97.2%–99.9%）
    r"[〜~～\-–—－]\s*[0-9]|[0-9]\s*[〜~～\-–—－]|"
    # 比較・概算（97.2%未満 / 以上 / 超 / 約97.2% / 前後）
    r"未満|以下|以上|超え?|前後|程度|およそ|約\s*[0-9]|最大|最低|平均|"
    # 否定・訂正（97.2%ではなく99.9%）
    r"ではなく|で(?:は)?ない|誤り|訂正|"
    r"完全攻略|技術介入|フル攻略|"             # 条件つきの別値
    r"実戦値|実測|サンプル")                   # 解析値でない


def parse_percent(raw: str):
    """'97.2%' を Decimal に。全体が一致しなければ None（先頭だけ取らない）。"""
    m = re.fullmatch(r"\s*([0-9]{1,3}(?:\.[0-9]+)?)\s*[%％]\s*", str(raw or ""))
    if not m:
        return None
    try:
        return Decimal(m.group(1))
    except InvalidOperation:
        return None


_CELL_SEP = re.compile(r"[｜|\t]")


def _drop_label_cells(text: str) -> str:
    """表の行から「数字を含まない見出しセル」を取り除く。

    証拠を表の行ごと受け取るようにしたので、1列目に機種名や項目名が入る。
    数字を含まないセルは値を持ち得ないので、解析対象から外す。
    """
    if not _CELL_SEP.search(text):
        return text
    cells = [c for c in _CELL_SEP.split(text)]
    keep = [c for c in cells if re.search(r"[0-9]", c)]
    return " ".join(keep).strip() if keep else text


def derive_kikaiwari(quote: str) -> dict:
    """後方互換：値だけを返す（理由が要るときは derive_kikaiwari_ex）。"""
    return derive_kikaiwari_ex(quote)[0]


def derive_kikaiwari_ex(quote: str):
    """引用文から {設定番号: 機械割} を導く。曖昧なら空を返す。

    ★導出できないことと、値が無いことを区別する★
      条件つきの別値（完全攻略時など）や範囲が混ざる文は、
      どの数字がその設定の機械割か決まらないので**何も返さない**。
    """
    # ★★全角で書かれた正しい引用を落とさない★★（Codex 6巡目 (b)-5）
    #   「設定１の機械割は９７．２％」は意味上正しいのに、
    #   正規表現がASCII前提なので導出できなかった。
    q = unicodedata.normalize("NFKC", str(quote or ""))
    if not re.search(_KIKAIWARI_WORD, q):
        return {}, "NO_KIKAIWARI_WORD"     # 機械割の話をしていない
    m_amb = _AMBIGUOUS.search(q)
    if m_amb:
        # ★どの語で落ちたかを出す★（Codex 8巡目 (b)-4）
        return {}, f"AMBIGUOUS_EXPRESSION:{m_amb.group(0)}@{m_amb.start()}"
    # ★表の行を丸ごと受け取るので、数字を含まない見出しセルは取り除く★
    #   （「スマスロテスト機｜機械割は設定1:97.2%」の1列目など）
    #   曖昧・否定の検査は**取り除く前の全文**に対して済ませてある。
    q = _drop_label_cells(q)

    def _collect(matches, skeys, pkeys):
        found: dict = {}
        for m in matches:
            st = next((m.group(k) for k in skeys if m.group(k)), None)
            pc = next((m.group(k) for k in pkeys if m.group(k)), None)
            if not st or not pc:
                return None, "NO_ALLOWED_PAIR"
            sm, pm_ = _NUM_RE.search(st), _NUM_RE.search(pc)
            if not sm or not pm_:
                return None, "NO_ALLOWED_PAIR"
            try:
                val = Decimal(pm_.group(0))
            except InvalidOperation:
                return None, "NO_ALLOWED_PAIR"
            setting = sm.group(0)
            if setting in found and found[setting] != val:
                return None, "DUPLICATE_SETTING_CONFLICT"
            found[setting] = val
        return found, "OK"

    # ① 文章形式（組ごとに項目語が入っている書き方）
    ms = list(_PAIR_ANY.finditer(q))
    if ms:
        found, why = _collect(ms, ("s1", "s2"), ("p1", "p2"))
        if not found:
            return {}, why
        # ★★残った文字に、意味を変える語が無いことを確かめる★★
        #   「97.2%から99.9%」「97.2%ではありません」「推定では…97.2%」は
        #   組の外に語が残るので、ここで全部落ちる（禁止語を数え上げなくてよい）。
        rest = _RESIDUE_OK.sub("", _PAIR_ANY.sub("", q)).strip()
        if rest:
            return {}, f"UNALLOWED_RESIDUE:{rest[:20]}"
        return found, "OK"

    # ② 列挙形式（★引用**全体**が「項目語＋組の並び」であるときだけ★）
    if not _ENUM_FULL.fullmatch(q):
        return {}, "NO_ALLOWED_PAIR"
    found, why = _collect(list(_PAIR_BARE.finditer(q)), ("s3",), ("p3",))
    if not found:
        return {}, why
    return found, "OK"


def check_c5_kikaiwari(claim: dict, quote: str) -> dict:
    """機械割 claim を引用文と突き合わせる。戻り値は {verdict, code}。"""
    cond = claim.get("conditions") or {}
    val = claim.get("value") or {}
    setting = str(cond.get("setting") or "").strip()

    if claim.get("field_key") != "kikaiwari.setting":
        return {"verdict": "SKIP", "code": "NOT_KIKAIWARI"}
    if setting not in ("1", "2", "3", "4", "5", "6"):
        # ★設定が決まらない機械割 claim は作らせない★
        return {"verdict": "FAIL", "code": "SETTING_MISSING"}
    if val.get("unit") not in ("%", "％"):
        return {"verdict": "FAIL", "code": "UNIT_NOT_PERCENT"}
    if val.get("operator") != "EXACT":
        return {"verdict": "FAIL", "code": "OPERATOR_NOT_EXACT"}

    want = parse_percent(val.get("raw"))
    if want is None:
        return {"verdict": "FAIL", "code": "RAW_NOT_PERCENT"}
    amt = val.get("amount")
    if amt is None or Decimal(str(amt)) != want:
        return {"verdict": "FAIL", "code": "RAW_AMOUNT_MISMATCH"}

    if val.get("plus_alpha"):
        # ★機械割に +α は無い★（Codex 3巡目 (a)-3）
        return {"verdict": "FAIL", "code": "PLUS_ALPHA_NOT_ALLOWED"}
    derived, why = derive_kikaiwari_ex(quote)
    if not derived:
        return {"verdict": "FAIL", "code": why}
    if setting not in derived:
        # ★引用に「その設定の」機械割が無い★（別の設定の値を流用させない）
        return {"verdict": "FAIL", "code": "SETTING_NOT_IN_QUOTE"}
    if derived[setting] != want:
        return {"verdict": "FAIL", "code": "VALUE_MISMATCH"}
    return {"verdict": "PASS", "code": "OK"}


# ------------------------------------------------- 公開ゲートでの再計算

# 意味の検証ができる項目だけを載せる。★ここに無い項目は自動採用しない★
_SEMANTIC_CHECKERS = {
    "kikaiwari.setting": check_c5_kikaiwari,
}


def identity_verdict(src: dict, identity: dict | None):
    """★出典が本当にその機種のページか、証拠から計算し直す★（Codex 4巡目 (a)-3）

    以前は `machine_variant_key_matched` という**台帳が書いた文字列**を
    比べていただけなので、別バージョンの記事でも同じ値を書けば通った。
    出典が持ってきた「ページ見出し」と「本文の抜粋」から判定する。
    証拠が無ければ数えない（既定拒否）。
    """
    ok, viol = identity_violations(src, identity)
    return ok, (viol[0] if viol else "OK")


def identity_violations(src: dict, identity: dict | None):
    """★検査は全部実行し、違反を配列で返す★（Codex 8巡目 (b)-3）

    最初の1件で return すると「訂正文＋別機種名＋パチンコ版」が同時にあっても
    1つしか見えず、直すたびに次が出てくる。判定は AND のまま、診断は全部返す。
    """
    import claim_identity as cid

    if not identity:
        return False, ["NO_IDENTITY_SPEC"]
    ev, why_ev = _identity_evidence_of(src)
    if ev is None:
        return False, [why_ev]          # 証拠を引けない＝数えない
    title = ev.get("page_title")
    unit = evidence_unit_text(src)
    if not title or not unit:
        return False, ["NO_EVIDENCE_UNIT"]

    cores = identity.get("machine_cores") or []
    viol = []
    # ★引用は証拠単位の中の逐語であること★（台帳が別の場所の文を貼れないように）
    q = str(src.get("quote") or "")
    if not q or unicodedata.normalize("NFKC", q) not in unicodedata.normalize(
            "NFKC", unit):
        viol.append("QUOTE_NOT_IN_EVIDENCE_UNIT")
    ok, why = _unit_binding_ok(unit, cores)
    if not ok:
        viol.append(why)
    ok, why = cid.check_title(title, cores, identity.get("reject_cores") or [],
                              identity.get("reject_name_cores") or [])
    if not ok:
        viol.append(f"TITLE_NG:{why[:40]}")
    ok, why = cid.check_tags(title, identity.get("machine_tags") or [], cores)
    if not ok:
        viol.append(f"TAG_NG:{why[:40]}")
    # ★★見出しの「機種名区間の外」も見る★★（Codex 5巡目 (a)-3）
    #   check_tags は機種名区間しか見ないので、
    #   「【Lバンドリ！】天井・設定判別｜パチンコ版」が通ってしまう。
    ok, why = _whole_text_ok(title, cores, identity)
    if not ok:
        viol.append(f"TITLE_OUTSIDE_NG:{why}")
    # ★証拠単位に、別機種・別媒体・続編が混ざっていないこと★
    ok, why = _whole_text_ok(unit, cores, identity)
    if not ok:
        viol.append(f"UNIT_NG:{why}")
    ok, why = cid.check_body(unit, cores)
    if not ok:
        viol.append(f"BODY_NG:{why[:40]}")
    return (not viol), viol


def _unit_binding_ok(unit: str, cores):
    """証拠単位そのものに機種名があり、比較の言い回しが無いこと。"""
    import claim_identity as cid

    u = unicodedata.normalize("NFKC", str(unit or ""))
    m = _COMPARISON.search(u)
    if m:
        return False, f"COMPARISON_IN_UNIT:{m.group(0)}"
    if not any(c and c in cid.normalize_core(u) for c in cores):
        return False, "MACHINE_NAME_NOT_IN_UNIT"
    return True, "OK"


# 引用の周りにあってはいけない言い回し（他機種の値を並べている合図）
# ★語の一覧は「他機種の値を並べている合図」に絞る★（Codex 7巡目 (b)-5）
#   「シリーズ」「ちなみに」は普通の記事本文にも出るので外し、
#   代わりに取りこぼしていた 対比／対照／一方／vs を足した。
_COMPARISON = re.compile(
    r"比較|対比|対照|旧作|前作|先代|前機種|他機種|参考まで|参考値|歴代|"
    r"前バージョン|旧台|旧機種|一方、|(?<![A-Za-z])vs(?![A-Za-z])", re.I)
# 引用の周りで機種名を探す幅（これより離れた機種名は結び付いていないとみなす）
_BIND_WINDOW = 60


# 証拠として受け取ってよい単位（DOM上の塊）。自由文の切り出しは受け取らない
EVIDENCE_UNIT_TYPES = ("TABLE_ROW", "TABLE_CELL", "LIST_ITEM", "PARAGRAPH",
                       "HEADING", "DEFINITION_ITEM")


def resolve_evidence(src: dict):
    """★出典の証拠を、台帳の外から引く★（Codex 8巡目・閉鎖条件③）

    台帳に書かれた見出し・証拠単位・型式は**使わない**。
    `evidence_ref`（証拠の指紋）で別ディレクトリから引き、
    中身の指紋が一致したものだけを証拠として認める。

    戻り値 (identity_evidence 相当の辞書, 理由)。
    """
    import claim_evidence as ce

    ver = (src.get("verification") or {})
    ref = ver.get("evidence_ref")
    if not ref:
        return None, "NO_EVIDENCE_REF"
    ev, why = ce.load_evidence(ref)
    if ev is None:
        return None, why
    return ce.as_identity_evidence(ev), "OK"


def _identity_evidence_of(src: dict):
    """検証に使う証拠を1か所で決める（別置きの証拠を正とする）。"""
    got, why = resolve_evidence(src)
    if got is not None:
        return got, why
    return None, why


def evidence_unit_text(src: dict):
    """出典が示した「証拠単位」の本文を返す（無ければ None）。

    ★★自由文からの切り出しを受け取らない★★（Codex 8巡目 (a)-1）
      「引用」と「その周辺」という作りだと、
        ・同じ設定に別の値が併記された文から片方だけ切り出す
        ・訂正が次の行にあるので、行で切れば見えなくなる
      という抜け道が残る。**表の行・セル・箇条書きの項目**のように、
      画面上で1つの塊になっている単位を丸ごと受け取り、丸ごと解析する。
    """
    ev, _ = _identity_evidence_of(src)
    unit = (ev or {}).get("evidence_unit")
    if not isinstance(unit, dict):
        return None
    if unit.get("unit_type") not in EVIDENCE_UNIT_TYPES:
        return None
    text = unit.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return text


def _context_binding_ok(quote: str, context: str, cores):
    """★引用が「その機種の話」として書かれているかを見る★（Codex 6巡目 (a)-2）

    「quote が context のどこかに含まれる」だけでは、
    「スマスロテスト機との比較。旧作・吉宗：設定1の機械割は97.2%」のような
    比較欄から値を切り出せてしまう。引用の**すぐ近く**に機種名があり、
    かつ近くに比較の言い回しが無いことを求める。
    """
    import claim_identity as cid

    ctx = unicodedata.normalize("NFKC", str(context or ""))
    q = unicodedata.normalize("NFKC", str(quote or ""))
    pos = ctx.find(q)
    if pos < 0 or not q:
        return False, "QUOTE_NOT_IN_CONTEXT"

    # ★★引用を「文の途中」で切らせない★★（Codex 7巡目 (a)-1）
    #   「設定1の機械割は97.2%ではなく99.9%です」から前半だけを引用すると、
    #   訂正前の値が正しい値として通ってしまう。
    #   引用を含む**文まるごと**を取り出し、それに曖昧・否定の検査を掛ける。
    starts = [0] + [m.end() for m in re.finditer(r"[。\n]", ctx)]
    ends = [m.end() for m in re.finditer(r"[。\n]", ctx)] + [len(ctx)]
    s0 = max([s for s in starts if s <= pos], default=0)
    e0 = min([e for e in ends if e >= pos + len(q)], default=len(ctx))
    sentence = ctx[s0:e0]
    if _AMBIGUOUS.search(sentence):
        return False, "AMBIGUOUS_IN_SENTENCE"

    lo = max(0, pos - _BIND_WINDOW)
    hi = min(len(ctx), pos + len(q) + _BIND_WINDOW)
    window = ctx[lo:hi]
    if _COMPARISON.search(window):
        return False, "COMPARISON_NEAR_QUOTE"
    wn = cid.normalize_core(window)
    if not any(c and c in wn for c in cores):
        return False, "MACHINE_NAME_NOT_NEAR_QUOTE"
    return True, "OK"


_LOOSE_CACHE: dict = {}


def _loose_pattern(core: str):
    """機種名の文字の間に、合計4文字までの挿入を許すパターンを作る。

    「真打版の吉宗」「真打ver吉宗」のように修飾語を挟まれても
    「真打吉宗」という別機種の名前として検出するため。
    """
    if core not in _LOOSE_CACHE:
        chars = [re.escape(ch) for ch in core]
        # 隙間は合計4文字まで（貪欲すぎると無関係な箇所を結んでしまう）
        body = chars[0] + "".join(r"[^\s]{0,2}" + ch for ch in chars[1:])
        _LOOSE_CACHE[core] = re.compile(body)
    return _LOOSE_CACHE[core]


def _whole_text_ok(text: str, cores, identity: dict):
    """文字列**全体**に、別媒体・別機種・続編の手掛かりが無いかを見る。

    claim_identity の検査は「機種名区間」だけを見る作りなので、
    区間の外に置かれた「｜パチンコ版」「続編2」を拾えない（Codex 5巡目 (a)-3）。
    ここでは全体を見て、疑わしければ落とす（安全側）。
    """
    import claim_identity as cid

    raw = unicodedata.normalize("NFKC", str(text or ""))
    t = cid.normalize_core(raw)
    if not t:
        return False, "EMPTY"
    # ★別媒体の表記は日本語だけではない★（Codex 6巡目 (a)-3）
    if cid.is_pachinko_text(raw) or _PACHINKO_EXTRA.search(raw):
        return False, "PACHINKO"
    # ★★他機種の名前は「位置」で判定する★★（Codex 7巡目 (a)-2）
    #   以前は「自分の芯を含む名前」を無条件に見逃していたので、
    #   「真打吉宗」は「吉宗」を含むという理由で素通りし、
    #   L吉宗の記事に真打吉宗の値を混ぜられた。
    #   自分の名前が出ている**その場所の中に収まっている**ときだけ見逃す。
    mine = []
    for c in cores:
        if c:
            mine.extend((mm.start(), mm.end()) for mm in re.finditer(re.escape(c), t))
    # ★別名は汎用語を含むので、短いものは使わない★（Codex 7巡目 (b)-3）
    #   「ゴッド」のような別名で正しい文脈（ゴッドモード中の挙動）を落とさないため、
    #   正式名は2文字以上、別名だけのものは5文字以上を対象にする。
    names = set(identity.get("reject_name_cores") or [])
    alls = set(identity.get("reject_all_cores") or identity.get("reject_cores") or [])
    targets = {rc for rc in names if rc and len(rc) >= 2}
    targets |= {rc for rc in (alls - names) if rc and len(rc) >= 5}
    for rc in sorted(targets):
        if not rc:
            continue
        # ★★語を挿し込んでも検出する★★（Codex 8巡目 (a)-2）
        #   「真打版の吉宗」は「真打吉宗」が連続しないので素通りしていた。
        #   文字の間に少しの隙間（合計4文字まで）を許して探す。
        for mm in _loose_pattern(rc).finditer(t):
            inside = any(s <= mm.start() and mm.end() <= e for s, e in mine)
            if not inside:
                return False, f"OTHER_MACHINE:{rc[:12]}"
    # ★続編の合図は数字だけではない★（Ⅱ・第二弾・ツー・リターンズ 等）
    for c in cores:
        if not c:
            continue
        for mm in re.finditer(re.escape(c), t):
            tail = t[mm.end():mm.end() + 6]
            m2 = _SEQUEL_MARK.match(tail)
            if m2:
                return False, f"SEQUEL_SUFFIX:{c[:10]}{m2.group(0)}"
    return True, "OK"


# 別媒体（パチンコ・遊技球）の表記ゆれ。claim_identity の日本語判定を補う
_PACHINKO_EXTRA = re.compile(
    r"pachinko|遊技球|玉あたり|1玉|"
    r"(?<![A-Za-z])[PpＰｐ]機|(?<![A-Za-z])[Ee]機", re.I)
# 続編・世代違いの合図（芯の直後）。数字は「2つ」等の助数詞を除く
_SEQUEL_MARK = re.compile(
    # 「6号機」「2台」「2025年」「2機種」などは続編ではない
    r"[0-9](?![つ人回種択段個倍円枚g号台年機日月分秒%])"
    r"|[ⅡⅢⅣⅤⅥⅦⅧⅨⅩ]|ii(?![a-z])|iii|"
    r"第[二三四五2345]弾|ツー|スリー|リターンズ|ネクスト|続編|再臨|新章", re.I)


def semantic_artifact(claim: dict, machine_variant_key: str,
                      registry: dict | None = None,
                      identity: dict | None = None,
                      physical_key: str | None = None,
                      expected_identity: dict | None = None) -> dict:
    """claim を出典の引用から検証し直した「検証結果の控え」を作る。

    ★台帳が書いている C5 の結果は読まない★（Codex 3回目 重大4）
      台帳は AI が書き込めるので、そこに PASS と書くだけで通ってしまう。
      公開の可否は**この関数がその場で計算した結果**だけで決める。

    ★台帳の申告は「減点」にしか使わない★
      C0〜C4（URL実在・引用一致など）は引用文だけでは再計算できないので、
      台帳が PASS と言っていることを**必要条件**として使う。
      台帳が FAIL と言っていれば当然数えない。PASS と言っていても、
      ここで C5 を計算し直して不合格なら数えない。
    """
    from claim_ledger import resolve_publisher, load_registry, CHECK_IDS

    registry = registry if registry is not None else load_registry()
    field = claim.get("field_key")
    checker = _SEMANTIC_CHECKERS.get(field)
    art = {"field_key": field, "machine_variant_key": machine_variant_key,
           "verifier_version": VERIFIER_VERSION, "sources": [],
           "counted_votes": 0, "verified": False}
    if checker is None:
        art["reason"] = "NO_SEMANTIC_CHECKER"     # 意味の検証器が無い項目
        return art

    counted, owners, lineages = [], set(), set()
    for i, src in enumerate(claim.get("sources") or []):
        row = {"index": i, "final_url": src.get("final_url")}
        ver = (src.get("verification") or {})
        checks = (ver.get("checks") or {})

        # 1) 台帳が自分で FAIL と言っているものは、その時点で数えない
        #    ★★C5だけでなく、出典全体の判定と票の扱いも見る★★（Codex (a)-2）
        #      これが無いと、台帳が「票に数えない(FAIL)」とした出典を
        #      C5の再計算だけで票に復活させられてしまう。
        declared_bad = [cid for cid in CHECK_IDS if cid != "C5"
                        and (checks.get(cid) or {}).get("verdict") != "PASS"]
        if ver.get("verdict") != "PASS":
            declared_bad.append("verdict")
        if ver.get("vote_disposition") != "COUNTED":
            declared_bad.append("vote_disposition")
        # 2) 発行者を**URLから**引き直す（台帳の申告は使わない）
        pub = resolve_publisher(src.get("final_url"), registry)
        # 3) 機種の型番までの一致（★同名の別バージョンを混ぜない★）
        #    ★★台帳が名乗る文字列ではなく、出典が示した型式から計算する★★
        #      （Codex 7巡目 (a)-3）。証拠に型式が無ければ数えない。
        import claim_inventory as _ci
        _ev, _ = _identity_evidence_of(src)
        ev_ident = (_ev or {}).get("machine_identity")
        variant_ok = (physical_key is not None
                      and _ci.physical_key(ev_ident) == physical_key)
        # ★どの項目が食い違ったかを残す★（Codex 8巡目 (b)-2）
        variant_diag = _ci.identity_diff(ev_ident, expected_identity)
        # 4) 出典が本当にその機種のページか、証拠から判定し直す
        id_ok, id_viol = identity_violations(src, identity)
        id_why = ";".join(id_viol) if id_viol else "OK"
        # 5) ★証拠単位を丸ごと解析する★（切り出した引用は使わない）
        unit = evidence_unit_text(src)
        c5 = (checker(claim, unit) if unit
              else {"verdict": "FAIL", "code": "NO_EVIDENCE_UNIT"})
        row.update({"c5": c5, "declared_bad": declared_bad,
                    "publisher_id": (pub or {}).get("publisher_id"),
                    "variant_matched": variant_ok, "variant_diff": variant_diag,
                    "identity": id_why, "identity_violations": id_viol})

        if c5["verdict"] != "PASS":
            row["disposition"] = "NOT_COUNTED_C5"
        elif declared_bad:
            row["disposition"] = "NOT_COUNTED_DECLARED_FAIL"
        elif pub is None:
            row["disposition"] = "NOT_COUNTED_UNKNOWN_PUBLISHER"
        elif not variant_ok:
            row["disposition"] = "NOT_COUNTED_VARIANT_MISMATCH"
        elif not id_ok:
            row["disposition"] = f"NOT_COUNTED_IDENTITY({id_why})"
        elif pub["ownership_group_id"] in owners:
            row["disposition"] = "NOT_COUNTED_SAME_OWNER"
        elif pub["content_lineage_id"] in lineages:
            row["disposition"] = "NOT_COUNTED_LINEAGE"
        else:
            row["disposition"] = "COUNTED"
            owners.add(pub["ownership_group_id"])
            lineages.add(pub["content_lineage_id"])
            counted.append(pub["publisher_id"])
        art["sources"].append(row)

    art["counted_publishers"] = sorted(counted)
    art["counted_votes"] = len(counted)
    # ★独立2出典で同じ値が導けたときだけ VERIFIED★
    art["verified"] = len(counted) >= 2
    if not art["verified"]:
        art["reason"] = "NOT_ENOUGH_INDEPENDENT_VOTES"
    art["artifact_sha256"] = _artifact_sha(art)
    return art


VERIFIER_VERSION = "claim_c5/1"


def _artifact_sha(art: dict) -> str:
    import hashlib
    import json as _json
    body = {k: v for k, v in art.items() if k != "artifact_sha256"}
    return hashlib.sha256(
        _json.dumps(body, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":")).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- selftest

def _tampered_evidence_blocked(_src, _c, artifact, VK, IDENT, PHYS, Q1, Q2, ce):
    """証拠ファイルを保存後に書き換えたら、その出典が数えられなくなること。"""
    import json as _json
    s1 = _src("https://chonborista.com/a", Q1)
    ref = s1["verification"]["evidence_ref"]
    path = ce.evidence_path(ref)
    ev = _json.load(open(path, encoding="utf-8"))
    ev["evidence_unit"]["text"] = "スマスロテスト機｜機械割は設定1:99.9%"
    _json.dump(ev, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    art = artifact(_c([s1, _src("https://nana-press.com/b", Q2)]),
                   VK, None, IDENT, PHYS)
    return not art["verified"]


def _claim(setting="1", raw="97.2%", amount=97.2, operator="EXACT", unit="%"):
    return {"field_key": "kikaiwari.setting",
            "conditions": {"setting": setting},
            "value": {"kind": "PERCENT", "raw": raw, "amount": amount,
                      "unit": unit, "operator": operator}}


def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    Q = "機械割は設定1:97.2% / 設定2:98.2% / 設定6:106.5%"

    t("引用と一致する機械割は PASS",
      check_c5_kikaiwari(_claim("1", "97.2%", 97.2), Q)["verdict"] == "PASS")
    t("　設定6も同じ引用から取れる",
      check_c5_kikaiwari(_claim("6", "106.5%", 106.5), Q)["verdict"] == "PASS")

    # ★★Codex が必須と挙げた反例★★
    t("★★設定と値の入れ替えを止める（設定6に設定1の値）★★",
      check_c5_kikaiwari(_claim("6", "97.2%", 97.2), Q)["code"] == "VALUE_MISMATCH")
    t("★★引用に無い設定の値を作れない（設定3は引用に無い）★★",
      check_c5_kikaiwari(_claim("3", "99.4%", 99.4), Q)["code"] == "SETTING_NOT_IN_QUOTE")
    t("★設定が欠けた claim は作れない",
      check_c5_kikaiwari(_claim("", "97.2%", 97.2), Q)["code"] == "SETTING_MISSING")
    t("★★範囲値の引用からは導出しない（97.2%〜106.5%）★★",
      check_c5_kikaiwari(_claim("1", "97.2%", 97.2),
                         "機械割97.2%〜106.5%")["verdict"] == "FAIL")
    t("★★完全攻略など条件つきの別値が混ざる引用からは導出しない★★",
      check_c5_kikaiwari(_claim("1", "98.4%", 98.4),
                         "設定1の機械割は98.4%（完全攻略103.0%）"
                         )["verdict"] == "FAIL")
    t("★実戦値の引用は解析値として使わない",
      check_c5_kikaiwari(_claim("6", "106.5%", 106.5),
                         "設定6の機械割は106.5%（実戦値）"
                         )["verdict"] == "FAIL")
    t("★機械割の話をしていない引用からは導出しない",
      check_c5_kikaiwari(_claim("1", "97.2%", 97.2),
                         "設定1のボーナス出現率は97.2%です"
                         )["verdict"] == "FAIL")
    t("★同じ設定に別の値が併記される引用は曖昧として止める",
      check_c5_kikaiwari(_claim("1", "97.2%", 97.2),
                         "機械割 設定1:97.2% ところにより 設定1:99.9%"
                         )["verdict"] == "FAIL")
    t("★raw と amount が食い違えば止める",
      check_c5_kikaiwari(_claim("1", "97.2%", 106.5), Q)["code"] == "RAW_AMOUNT_MISMATCH")
    t("★単位が % でなければ止める",
      check_c5_kikaiwari(_claim("1", "97.2%", 97.2, unit="G"), Q)["code"]
      == "UNIT_NOT_PERCENT")
    t("★operator が EXACT でなければ止める（機械割に MAX は無い）",
      check_c5_kikaiwari(_claim("1", "97.2%", 97.2, operator="MAX"), Q)["code"]
      == "OPERATOR_NOT_EXACT")
    t("★raw が % の形でなければ止める（先頭の数字だけ取らない）",
      check_c5_kikaiwari(_claim("1", "97.2", 97.2), Q)["code"] == "RAW_NOT_PERCENT")
    t("★機械割以外の claim には使わない（SKIP）",
      check_c5_kikaiwari({**_claim(), "field_key": "ceiling.normal"}, Q)["verdict"]
      == "SKIP")

    # 実データに近い書き方でも導出できること
    real = ("**機械割**：設1：97.2% / 設2：98.2% / 設3：99.4% / "
            "設4：101.6% / 設5：103.8% / 設6：106.5%")
    d = derive_kikaiwari(real)
    t("実データの書き方（設1：97.2% / …）から6設定ぶん導出できる",
      len(d) == 6 and d["6"] == Decimal("106.5"))

    # ------------------------------------------------ 公開ゲートの再計算
    VK = "gogo_juggler3:SS-01"

    # ★出典の機種同定に使う一式（実物と同じ作り方）★
    import claim_identity as _cid
    _MACHINES = [{"slug": "x", "name": "スマスロテスト機", "info": "スマスロAT"},
                 {"slug": "y", "name": "スマスロ別機", "info": "スマスロAT"}]
    IDENT = _cid.identity_spec(_MACHINES[0], _MACHINES)
    def _unit(text, utype="TABLE_ROW"):
        return {"unit_type": utype, "dom_path": "table[1]/tbody/tr[2]",
                "text": text}

    EV = {"page_title": "スマスロテスト機 天井・機械割・設定判別",
          "evidence_unit": _unit("スマスロテスト機｜機械割は設定1:97.2% / 設定6:106.5%")}

    IDENT_PHYS = {"manufacturer_id": "test-maker",
                  "regulatory_model_code": "TEST-001",
                  "release_date": "2026-01-01"}
    import claim_inventory as _ci2
    PHYS = _ci2.physical_key(IDENT_PHYS)

    # ★検査用の証拠置き場（実物と同じ経路で書き、指紋で引く）★
    import claim_evidence as _ce
    import tempfile as _tf
    _ce.EVIDENCE_DIR = _tf.mkdtemp()

    def _put_evidence(url, ie, machine_identity):
        """台帳ではなく証拠置き場に書き、その指紋を返す。"""
        unit = ie.get("evidence_unit") or {}
        body = {
            "schema_version": _ce.SCHEMA_VERSION,
            "fetch": {"requested_url": url, "final_url": url,
                      "fetched_at": "2026-07-28T09:00:00Z", "http_status": 200,
                      "response_sha256": "a" * 64},
            "page": {"title": ie.get("page_title") or "（無題）",
                     "body_sha256": "b" * 64},
            "evidence_unit": unit,
            "machine_identity": machine_identity or {
                "manufacturer_id": "x", "regulatory_model_code": "x",
                "release_date": "x"},
            "fetcher_version": "selftest/1",
        }
        try:
            return _ce.write_evidence(body)
        except _ce.EvidenceError:
            # 形が不正な証拠は保存できない＝参照先が無い状態を作る
            return "0" * 64

    def _src(url, quote, c5_declared="PASS", others="PASS", vk=VK,
             verdict="PASS", disp="COUNTED", ev=None, phys="__default__"):
        checks = {c: {"verdict": others} for c in ("C0", "C1", "C2", "C3", "C4")}
        checks["C5"] = {"verdict": c5_declared}
        # ★証拠は台帳の外に置き、指紋で参照する★
        # 既定では「機種名｜引用」という表の1行を証拠にする（実物に近い形）
        base = (dict(ev) if ev is not None
                else {"page_title": EV["page_title"],
                      "evidence_unit": _unit("スマスロテスト機｜" + quote)})
        mi = (IDENT_PHYS if phys == "__default__" else phys)
        ref = _put_evidence(url, base, mi)
        return {"final_url": url, "quote": quote,
                "verification": {"checks": checks, "verdict": verdict,
                                 "vote_disposition": disp,
                                 "evidence_ref": ref}}

    def _c(sources, setting="1", raw="97.2%", amount=97.2):
        return {**_claim(setting, raw, amount), "sources": sources}

    Q1 = "機械割は設定1:97.2% / 設定6:106.5%"
    Q2 = "設定1の機械割は97.2%です"
    ok2 = _c([_src("https://chonborista.com/a", Q1),
              _src("https://nana-press.com/b", Q2)])

    a = semantic_artifact(ok2, VK, None, IDENT, PHYS)
    t("独立2出典で同じ値が導ければ VERIFIED", a["verified"] and a["counted_votes"] == 2)
    t("　検証結果の控えに指紋がつく", len(a.get("artifact_sha256", "")) == 64)

    t("★★台帳がC5をPASSと書いていても、引用が合わなければ数えない★★",
      not semantic_artifact(
          _c([_src("https://chonborista.com/a", "設定1の機械割は99.9%",
                   ev={"page_title": EV["page_title"],
                       "evidence_unit": _unit("スマスロテスト機｜設定1の機械割は99.9%")}),
              _src("https://nana-press.com/b", Q2)]), VK, None, IDENT,
          PHYS)["verified"])
    t("★1出典しか導けなければ VERIFIED にしない",
      not semantic_artifact(_c([_src("https://chonborista.com/a", Q1)]),
                            VK, None, IDENT, PHYS)["verified"])
    t("★レジストリに無いホストは票に数えない",
      not semantic_artifact(
          _c([_src("https://chonborista.com/a", Q1),
              _src("https://example.com/b", Q2)]), VK, None, IDENT, PHYS)["verified"])
    t("★★機種の型番が一致しない出典は数えない（同名の別バージョン混入）★★",
      not semantic_artifact(
          _c([_src("https://chonborista.com/a", Q1),
              _src("https://nana-press.com/b", Q2, vk="gogo_juggler3:SS-02")]),
          VK)["verified"])
    t("★機種の型番の記録が無い出典も数えない（既定で不合格）",
      [r["disposition"] for r in semantic_artifact(
          _c([_src("https://chonborista.com/a", Q1),
              {**_src("https://nana-press.com/b", Q2),
               "verification": {"verdict": "PASS", "vote_disposition": "COUNTED",
                                "checks": {c: {"verdict": "PASS"}
                                           for c in ("C0", "C1", "C2", "C3", "C4", "C5")}}}
              ]), VK, None, IDENT, PHYS)["sources"]][1] == "NOT_COUNTED_C5")
    # ★★Codex 2回目 (a)-2：台帳が票に数えないとした出典を復活させない★★
    t("★★台帳が FAIL とした出典を、C5が合っても票に復活させない★★",
      not semantic_artifact(
          _c([_src("https://chonborista.com/a", Q1, verdict="FAIL",
                   disp="NOT_COUNTED_UNKNOWN"),
              _src("https://nana-press.com/b", Q2, verdict="FAIL",
                   disp="NOT_COUNTED_UNKNOWN")]), VK, None, IDENT, PHYS)["verified"])
    t("　票に数えないと書かれた出典も同様",
      not semantic_artifact(
          _c([_src("https://chonborista.com/a", Q1, disp="NOT_COUNTED_SAME_OWNER"),
              _src("https://nana-press.com/b", Q2)]), VK, None, IDENT, PHYS)["verified"])
    # ★★Codex 2回目 (a)-3：別項目の百分率を機械割として導出しない★★
    t("★★別項目の％を機械割として導出しない（勝率97.2%／機械割106.5%）★★",
      check_c5_kikaiwari(_claim("1", "97.2%", 97.2),
                         "設定1の勝率は97.2%。設定6の機械割は106.5%"
                         )["verdict"] == "FAIL")
    t("　★別項目が混ざる引用は、正しい設定6の値ごと拒否する（安全側）",
      check_c5_kikaiwari(_claim("6", "106.5%", 106.5),
                         "設定1の勝率は97.2%。設定6の機械割は106.5%"
                         )["verdict"] == "FAIL")
    t("★列挙形式でも、組以外の語が混ざれば導出しない",
      check_c5_kikaiwari(_claim("1", "97.2%", 97.2),
                         "機械割 設定1:97.2% 勝率 設定2:98.2%"
                         )["verdict"] == "FAIL")
    # ★★Codex 2回目 (a)-4：範囲・比較・否定をEXACTとして通さない★★
    t("★★括弧形でも項目語を必須にする（勝率97.2%（設定1）を拾わない）★★",
      check_c5_kikaiwari(_claim("1", "97.2%", 97.2),
                         "設定6の機械割は106.5%。勝率97.2%（設定1）"
                         )["verdict"] == "FAIL")
    t("　項目語つきの括弧形は導ける（機械割は106.5%（設定6））",
      check_c5_kikaiwari(_claim("6", "106.5%", 106.5),
                         "機械割は106.5%（設定6）")["verdict"] == "PASS")
    for bad_q in ("設定1の機械割は97.2%-99.9%", "設定1の機械割は97.2%～99.9%",
                  "設定1の機械割は97.2%未満", "設定1の機械割は97.2%以上",
                  "設定1の機械割は97.2%ではなく99.9%", "設定1の機械割は約97.2%",
                  # ★Codex 2巡目 (a)-2：禁止語リストでは防げなかった書き方
                  "設定1の機械割は97.2%から99.9%",
                  "設定1の機械割は97.2%ではありません",
                  "設定1の機械割は97.2%を下回る",
                  "推定では設定1の機械割は97.2%",
                  "概算で設定1の機械割は97.2%"):
        t(f"★★比較・範囲・否定を EXACT にしない：{bad_q}★★",
          check_c5_kikaiwari(_claim("1", "97.2%", 97.2), bad_q)["verdict"] == "FAIL")

    # ★★Codex 3巡目 (a)-3・(b)-1★★
    t("★★機械割に +α を付けた claim は通さない★★",
      check_c5_kikaiwari({**_claim("1", "97.2%", 97.2),
                          "value": {**_claim()["value"], "plus_alpha": True}},
                         Q1)["code"] == "PLUS_ALPHA_NOT_ALLOWED")
    for ok_q in ("設定1の機械割は97.2%です（メーカー公表値）。",
                 "設定1の機械割（出玉率）は97.2%です。",
                 "機械割：設定1 97.2%／設定6 106.5%"):
        t(f"　安全な書き方は通す：{ok_q}",
          check_c5_kikaiwari(_claim("1", "97.2%", 97.2), ok_q)["verdict"] == "PASS")
    t("★★列挙形式は引用全体がその形のときだけ使う★★（Codex 4巡目 (a)-1）",
      check_c5_kikaiwari(_claim("1", "97.2%", 97.2),
                         "メーカー公表値は設定1 97.2%。機械割は設定6 106.5%"
                         )["verdict"] == "FAIL")
    t("★落ちた理由が原因ごとに分かれている",
      check_c5_kikaiwari(_claim("1", "97.2%", 97.2), "設定1のぶどうは1/6.25"
                         )["code"] == "NO_KIKAIWARI_WORD"
      and check_c5_kikaiwari(_claim("1", "97.2%", 97.2), "設定1の機械割は約97.2%"
                             )["code"].startswith("AMBIGUOUS_EXPRESSION")
      and check_c5_kikaiwari(_claim("1", "97.2%", 97.2),
                             "設定1の勝率は97.2%。設定6の機械割は106.5%"
                             )["code"].startswith("UNALLOWED_RESIDUE"))

    t("★台帳が C2 を FAIL と書いていればその出典は数えない",
      not semantic_artifact(
          _c([_src("https://chonborista.com/a", Q1),
              _src("https://nana-press.com/b", Q2, others="FAIL")]),
          VK)["verified"])
    t("★★同じ運営元の別ホストは1票（www有無で水増しできない）★★",
      not semantic_artifact(
          _c([_src("https://slopachi-quest.com/a", Q1),
              _src("https://www.slopachi-quest.com/b", Q2)]), VK, None, IDENT, PHYS)["verified"])
    # ★★Codex 4巡目 (a)-3：出典の機種同定を証拠から計算し直す★★
    t("★★機種同定の証拠が無い出典は数えない（既定拒否）★★",
      not semantic_artifact(
          _c([_src("https://chonborista.com/a", Q1, ev={}),
              _src("https://nana-press.com/b", Q2, ev={})]),
          VK, None, IDENT, PHYS)["verified"])
    t("★★別機種のページを持ってきても数えない★★",
      not semantic_artifact(
          _c([_src("https://chonborista.com/a", Q1,
                   ev={"page_title": "スマスロ別機 天井・機械割",
                       "evidence_unit": _unit("スマスロ別機｜" + Q1)}),
              _src("https://nana-press.com/b", Q2)]),
          VK, None, IDENT, PHYS)["verified"])
    # ★★Codex 5巡目★★
    t("★★引用が同定の文脈に無ければ数えない（比較欄から切り出せない）★★",
      not semantic_artifact(
          _c([_src("https://chonborista.com/a", "設定1の機械割は88.8%",
                   ev={"page_title": EV["page_title"],
                       "evidence_unit": _unit("スマスロテスト機の記事。"
                                              "比較欄では旧作の設定1の機械割は88.8%。",
                                              "PARAGRAPH")}),
              _src("https://nana-press.com/b", Q2)],
             raw="88.8%", amount=88.8), VK, None, IDENT, PHYS)["verified"])
    t("★★機種名区間の外に置いた媒体表示も見る（｜パチンコ版）★★",
      not semantic_artifact(
          _c([_src("https://chonborista.com/a", Q1,
                   ev={"page_title": "【スマスロテスト機】天井・設定判別｜パチンコ版",
                       "evidence_unit": EV["evidence_unit"]}),
              _src("https://nana-press.com/b", Q2)], ), VK, None, IDENT, PHYS)["verified"])
    t("★★機種名区間の外に置いた続編表示も見る★★",
      not semantic_artifact(
          _c([_src("https://chonborista.com/a", Q1,
                   ev={"page_title": "【スマスロテスト機】天井・設定判別｜スマスロテスト機2",
                       "evidence_unit": EV["evidence_unit"]}),
              _src("https://nana-press.com/b", Q2)], ), VK, None, IDENT, PHYS)["verified"])
    # ★★Codex 6巡目★★
    t("★★比較欄の他機種の値を切り出せない（旧作・吉宗：…）★★",
      not semantic_artifact(
          _c([_src("https://chonborista.com/a", "設定1の機械割は97.2%",
                   ev={"page_title": EV["page_title"],
                       "evidence_unit": _unit("スマスロテスト機との比較。"
                                              "旧作・吉宗：設定1の機械割は97.2%",
                                              "PARAGRAPH")}),
              _src("https://nana-press.com/b", Q2)]), VK, None, IDENT, PHYS)["verified"])
    t("★★機種名が引用から遠ければ結び付いていないとみなす★★",
      not semantic_artifact(
          _c([_src("https://chonborista.com/a", "設定1の機械割は97.2%",
                   ev={"page_title": EV["page_title"],
                       "evidence_unit": _unit("あ" * 200 + "設定1の機械割は97.2%",
                                              "PARAGRAPH")}),
              _src("https://nana-press.com/b", Q2)]), VK, None, IDENT, PHYS)["verified"])
    for bad_title in ("【スマスロテスト機】天井・解析｜PACHINKO版",
                      "【スマスロテスト機】天井・解析｜遊技球版",
                      "【スマスロテスト機】天井・解析｜スマスロテスト機Ⅱ",
                      "【スマスロテスト機】天井・解析｜スマスロテスト機第二弾"):
        t(f"★★回避表記も落とす：{bad_title[-12:]}★★",
          not semantic_artifact(
              _c([_src("https://chonborista.com/a", Q1,
                       ev={"page_title": bad_title,
                           "evidence_unit": EV["evidence_unit"]}),
                  _src("https://nana-press.com/b", Q2)]),
              VK, None, IDENT, PHYS)["verified"])
    t("★全角で書かれた正しい引用も導ける（設定１の機械割は９７．２％）",
      check_c5_kikaiwari(_claim("1", "97.2%", 97.2),
                         "設定１の機械割は９７．２％")["verdict"] == "PASS")
    # ★★Codex 7巡目★★
    t("★★訂正文の途中で引用を切っても通さない（97.2%ではなく99.9%）★★",
      not semantic_artifact(
          _c([_src("https://chonborista.com/a", "設定1の機械割は97.2%",
                   ev={"page_title": EV["page_title"],
                       "evidence_unit": _unit("スマスロテスト機の訂正情報。"
                                              "設定1の機械割は97.2%ではなく99.9%です。",
                                              "PARAGRAPH")}),
              _src("https://nana-press.com/b", Q2)]), VK, None, IDENT,
          PHYS)["verified"])
    t("★★自機種名を含む別機種名（真打◯◯）を見逃さない★★",
      _whole_text_ok("真打スマスロテスト機：設定1の機械割は97.2%",
                     IDENT["machine_cores"],
                     {"reject_name_cores": ["真打スマスロテスト機"],
                      "reject_all_cores": ["真打スマスロテスト機"]})[0] is False)
    t("　自機種名そのものは落とさない",
      _whole_text_ok("スマスロテスト機：設定1の機械割は97.2%",
                     IDENT["machine_cores"], IDENT)[0] is True)
    t("★★型式は出典が示した値から計算する（台帳の申告では通らない）★★",
      not semantic_artifact(
          _c([_src("https://chonborista.com/a", Q1, phys=None),
              _src("https://nana-press.com/b", Q2, phys=None)]),
          VK, None, IDENT, PHYS)["verified"])
    t("　型式が違えば数えない",
      not semantic_artifact(
          _c([_src("https://chonborista.com/a", Q1,
                   phys={"manufacturer_id": "other", "regulatory_model_code": "X",
                         "release_date": "2020-01-01"}),
              _src("https://nana-press.com/b", Q2)]),
          VK, None, IDENT, PHYS)["verified"])
    t("★「6号機」「2台」は続編扱いしない（過剰拒否の回避）",
      _whole_text_ok("スマスロテスト機 6号機の機械割・設定判別",
                     IDENT["machine_cores"], IDENT)[0] is True)
    # ★★Codex 8巡目★★
    t("★★同じ設定に別の値が併記された塊は、片方だけ取り出せない★★",
      not semantic_artifact(
          _c([_src("https://chonborista.com/a", "設定1の機械割は97.2%",
                   ev={"page_title": EV["page_title"],
                       "evidence_unit": _unit(
                           "スマスロテスト機｜設定1の機械割は97.2%、"
                           "設定1の機械割は99.9%", "PARAGRAPH")}),
              _src("https://nana-press.com/b", Q2)]), VK, None, IDENT,
          PHYS)["verified"])
    t("★★次の行に訂正がある塊も、行で切って逃げられない★★",
      not semantic_artifact(
          _c([_src("https://chonborista.com/a", "設定1の機械割は97.2%",
                   ev={"page_title": EV["page_title"],
                       "evidence_unit": _unit(
                           "スマスロテスト機\n設定1の機械割は97.2%\n"
                           "※訂正：正しくは99.9%です。", "LIST_ITEM")}),
              _src("https://nana-press.com/b", Q2)]), VK, None, IDENT,
          PHYS)["verified"])
    t("★★語を挿し込んだ別機種名も見つける（真打版の◯◯）★★",
      _whole_text_ok("真打版のスマスロテスト機：設定1の機械割は97.2%",
                     IDENT["machine_cores"],
                     {"reject_name_cores": ["真打スマスロテスト機"],
                      "reject_all_cores": ["真打スマスロテスト機"]})[0] is False)
    t("★同定の違反は全部返す（最初の1件で打ち切らない）",
      len(identity_violations(
          _src("https://chonborista.com/a", "設定1の機械割は97.2%",
               ev={"page_title": "【スマスロテスト機】解析｜パチンコ版",
                   "evidence_unit": _unit("旧作との比較：設定1の機械割は97.2%",
                                          "PARAGRAPH")}), IDENT)[1]) >= 2)
    t("★曖昧で落ちたとき、どの語かが分かる",
      "@" in check_c5_kikaiwari(_claim("1", "97.2%", 97.2),
                                "設定1の機械割は約97.2%")["code"])
    # ★★証拠の外部化（Codex 8巡目・閉鎖条件③）★★
    def _no_ref(url, quote):
        s = _src(url, quote)
        s["verification"].pop("evidence_ref")
        return s

    t("★★証拠の指紋が無い出典は数えない（台帳だけでは通らない）★★",
      not semantic_artifact(
          _c([_no_ref("https://chonborista.com/a", Q1),
              _no_ref("https://nana-press.com/b", Q2)]),
          VK, None, IDENT, PHYS)["verified"])
    t("★指し示す証拠が存在しなければ数えない",
      not semantic_artifact(
          _c([{**_src("https://chonborista.com/a", Q1),
               "verification": {**_src("https://chonborista.com/a", Q1)["verification"],
                                "evidence_ref": "f" * 64}},
              _src("https://nana-press.com/b", Q2)]),
          VK, None, IDENT, PHYS)["verified"])
    t("★★保存後に証拠を書き換えたら数えなくなる★★",
      _tampered_evidence_blocked(_src, _c, semantic_artifact, VK, IDENT, PHYS,
                                 Q1, Q2, _ce))
    t("★台帳が証拠の中身を貼っても、判定には使われない（外の証拠が正）",
      semantic_artifact(
          _c([{**_src("https://chonborista.com/a", Q1),
               "verification": {**_src("https://chonborista.com/a", Q1)["verification"],
                                "identity_evidence": {"page_title": "偽の見出し"}}},
              _src("https://nana-press.com/b", Q2)]),
          VK, None, IDENT, PHYS)["verified"])
    t("★同定の一式が無ければ、その場で止める（NO_IDENTITY_SPEC）",
      not semantic_artifact(ok2, VK, None, None, PHYS)["verified"])
    t("★意味の検証器が無い項目は VERIFIED にしない（既定拒否）",
      semantic_artifact({**ok2, "field_key": "ceiling.normal"}, VK, None, IDENT, PHYS)["reason"]
      == "NO_SEMANTIC_CHECKER")
    t("★引用の内容が同じでも、出典が0件なら VERIFIED にしない",
      not semantic_artifact(_c([]), VK, None, IDENT, PHYS)["verified"])

    ng = [n for n, ok in results if not ok]
    print("")
    print(f"{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

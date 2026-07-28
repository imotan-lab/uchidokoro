#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""claim_inventory.py — 「検証しなければならない claim slot の一覧」を決定論で作る

★これは何か★
  inventory は「正しい値の一覧」ではない。**検証しないと公開できない枠の一覧**。
  これが無いと、ClaudeとCodexが**2人そろって同じ項目を見落としても成功扱い**になる。
  （Codex の指摘：「何を調べるかをAIと相談」だけでは網羅が保証されない）

★決定論で作る★
  記事の文章から自由に claim を起こす処理は**作らない**。
  有限のラベル辞書で型に落ちたものだけを slot にし、
  落ちなかった事実は `unclassified_atoms` として**止める**。
  （推測で claim を起こすと、今回排除したい曖昧推定に戻る）

★Phase 1 の範囲★
  在庫を作って数えるだけ。記事生成・ビルド・公開には一切つながない。

使い方:
    python scripts/claim_inventory.py --selftest
    python scripts/claim_inventory.py --slug tokyo_ghoul
    python scripts/claim_inventory.py --all           # 公開予定79機種の集計
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import claim_ledger as cl  # noqa: E402

DATA = os.path.join(BASE, "assets", "data")
OUT_DIR = os.path.join(DATA, "claim-inventory")
GENERATOR_VERSION = "claim_inventory/1.0.0"
SCHEMA_VERSION = "claim-inventory/v1"

# ---------------------------------------------------------------- ラベル辞書
# ★有限の辞書だけで型に落とす。未知ラベルは推測せず unclassified へ送る★
#   (正規表現, field_key, mode, scope, counter_basis, value_kind, unit)
LABEL_RULES = [
    # --- G数天井（現在 C5 が実装済みなのはこの系統だけ）
    # ★★数え方(counter_basis)はラベルから推測しない★★
    #   東京喰種は「AT間=メニュー画面 / CZ間=液晶右下」だが、実データに反例がある：
    #     gundam_uc2「AT間1400G（液晶）」 sao2「CZ間499G+α（実ゲーム数）」
    #     kengan_ashura「CZ間ゲーム数はデータカウンターで確認」
    #   1機種の実測を79機種へ一般化すると、数え方を取り違えたまま値だけ揃えてしまう。
    #   よって basis は UNKNOWN から始め、**出典の逐語引用で確定させる**（C5の仕事）。
    (r"^AT間天井$|^AT間$", "ceiling.normal.at", "NORMAL", "AT_GAP", "UNKNOWN",
     "INTEGER", "G"),
    (r"^CZ間天井$|^CZ間$", "ceiling.normal.cz", "NORMAL", "CZ_GAP", "UNKNOWN",
     "INTEGER", "G"),
    (r"^ボーナス間天井$", "ceiling.normal.bonus", "NORMAL", "BONUS_GAP", "UNKNOWN",
     "INTEGER", "G"),
    (r"^ST間天井$", "ceiling.normal.st", "NORMAL", "ST_GAP", "UNKNOWN",
     "INTEGER", "G"),
    (r"^天井$|^通常時の天井$|^G数天井$", "ceiling.normal", "NORMAL", "NONE",
     "UNKNOWN", "INTEGER", "G"),
    (r"^設定変更後天井$|^リセット天井$|^リセット後天井$|^リセット$|^リセット後$|"
     r"^リセット短縮$|^リセット後の天井$|^設定変更後$", "ceiling.reset", "RESET",
     "NONE", "UNKNOWN", "INTEGER", "G"),
    (r"^リセット後AT間天井$|^リセットAT間$", "ceiling.reset.at", "RESET", "AT_GAP",
     "UNKNOWN", "INTEGER", "G"),
    (r"^リセット後CZ間天井$|^リセットCZ間$", "ceiling.reset.cz", "RESET", "CZ_GAP",
     "UNKNOWN", "INTEGER", "G"),
    # --- 回数・周期・ポイントの天井（C5未実装なので自動採用はされない）
    (r"^スルー天井$|^天井\(スルー\)$|^天井（スルー）$", "ceiling.through", "NORMAL",
     "NONE", "THROUGH", "INTEGER", "回"),
    (r"^周期天井$", "ceiling.cycle", "NORMAL", "NONE", "CYCLE", "INTEGER", "周期"),
    (r"^ポイント天井$|^pt天井$", "ceiling.point", "NORMAL", "NONE", "POINT",
     "INTEGER", "pt"),
    (r"^BIG後天井$|^BIG後$", "ceiling.normal.big_after", "NORMAL", "BIG_AFTER",
     "UNKNOWN", "INTEGER", "G"),
    (r"^REG後天井$|^REG後$", "ceiling.normal.reg_after", "NORMAL", "REG_AFTER",
     "UNKNOWN", "INTEGER", "G"),
    # ★「通常時」は天井とは限らない★（通常時の純増・通常時の確率などにも使われる）
    #   ラベルだけで天井と決めつけない。未知として止める。
    (r"^通常天井$", "ceiling.normal", "NORMAL", "NONE", "UNKNOWN", "INTEGER", "G"),
    # --- 恩恵（何が起きるか）。文章なので TEXT。★数値ではないが事実★
    (r"^恩恵$|^天井恩恵$", "benefit.ceiling", "NORMAL", "NONE", "NONE", "TEXT", ""),
    (r"^リセット恩恵$", "benefit.reset", "RESET", "NONE", "NONE", "TEXT", ""),
    (r"^スルー恩恵$", "benefit.through", "NORMAL", "NONE", "THROUGH", "TEXT", ""),
    # --- スペック
    (r"^機械割$|^出玉率$", "kikaiwari.setting", "ANY", "NONE", "NONE",
     "PERCENT", "%"),
    (r"^機械割\(設定(\d)\)$|^機械割（設定(\d)）$", "kikaiwari.setting", "ANY", "NONE",
     "NONE", "PERCENT", "%"),
    (r"^AT純増$|^純増$", "net_increase.phase", "ANY", "NONE", "COIN",
     "DECIMAL", "枚/G"),
    # ★表の列見出しは短い（BIG / REG / 合算）。設定別表の列としても拾う★
    (r"^BIG$|^BIG確率$|^BB$|^BB確率$|^(?:BIG|BB)確率[（(]設定(\d)[)）]$",
     "prob.big", "ANY", "NONE", "NONE", "PROBABILITY", "1/x"),
    (r"^REG$|^REG確率$|^RB$|^RB確率$|^(?:REG|RB)確率[（(]設定(\d)[)）]$",
     "prob.reg", "ANY", "NONE", "NONE", "PROBABILITY", "1/x"),
    (r"^初当たり確率$|^初当たり確率[（(]設定(\d)[)）]$|^AT初当たり確率$",
     "prob.first_hit", "ANY", "NONE", "NONE", "PROBABILITY", "1/x"),
    (r"^BIG獲得枚数$|^BB獲得枚数$", "payout.big", "ANY", "NONE", "COIN",
     "INTEGER", "枚"),
    (r"^REG獲得枚数$|^RB獲得枚数$", "payout.reg", "ANY", "NONE", "COIN",
     "INTEGER", "枚"),
    (r"^ベース$", "base_game", "ANY", "NONE", "COIN", "DECIMAL", "G/50枚"),
    (r"^コイン単価$", "coin_unit_price", "ANY", "NONE", "COIN", "DECIMAL", "円"),
    (r"^ぶどう確率$|^ブドウ確率$", "prob.grape", "ANY", "NONE", "NONE",
     "PROBABILITY", "1/x"),
    (r"^ボーナス合算確率$|^合算$|^合算確率$|^ボーナス合算$|^ボーナス合算[（(]設定(\d)[)）]$",
     "prob.bonus_total", "ANY", "NONE", "NONE", "PROBABILITY", "1/x"),
    (r"^コイン持ち$", "coin_persistence", "ANY", "NONE", "COIN", "DECIMAL", "G/50枚"),
]
_LABEL_RE = [(re.compile(p), fk, m, s, cb, vk, u)
             for p, fk, m, s, cb, vk, u in LABEL_RULES]

# ★編集判断（B区分）。事実ではないので裏取りの対象にしない★
EDITORIAL_LABELS = re.compile(
    r"狙い目|ヤメ時|やめ時|立ち回り|基本方針|ページの役割|おすすめツール|主な狙い方|"
    r"判別の軸|区切り方|見方|ゲーム性|とは$|"
    # ★交換率のラベルは「その交換率での狙い目」を指す行＝編集判断（B区分）★
    r"^等価|^5\.6枚|^5\.5枚|^5\.0枚|^4\.5枚|^現金|持ちメダル|交換$|投資$")

# ★設定示唆の表★（トロフィー・終了画面・ボイス等）。事実だが数値ではなく、
#   1項目ずつ型に落とすより「表ごと」で扱うべきもの。Phase 1では未分類にしない。
HINT_TABLE_HEADERS = ("示唆", "見方", "設定示唆")
HINT_ROW_LABELS = re.compile(
    r"^(?:銅|銀|金|虹|レインボー)(?:トロフィー)?$|トロフィー$|^設定示唆$|^設定$")

# ★数値を含まない案内文など。claim にならないが未分類でもない★
NONCLAIM_LABELS = re.compile(
    r"^機種名$|^メーカー$|^タイプ$|^設定段階$|^導入日$|^導入予定日$|^設置台数$|"
    r"^コンプリート機能$|^有利区間$")

_NUM = re.compile(r"[0-9０-９]")

# ★本文の文章に紛れた「事実らしい数値」★（構造化されていないので型に落とせない）
#   これを黙って捨てると「未分類ゼロ」が網羅の証明にならない。
#   ★★語→数字だけでなく、数字→語の順も見る★★（Codex 2回目 (a)-5）
#     「9999Gで天井に到達します」は数字が先なので、旧実装では素通りしていた。
_FACT_WORD = r"(?:天井|機械割|出玉率|純増|確率|獲得枚数|コイン持ち|スルー|周期|" \
             r"BIG|REG|BB|RB|ボーナス|初当たり|ベース|設定)"
# ★★語と数字の距離で判定しない★★（Codex 2巡目 (a)-4）
#   「機械割については…（30文字）…設定1は99.9%です」のように離すだけで
#   すり抜けた。**数字に単位が付いていれば、その文は事実を語っている**とみなす。
_NUM_WITH_UNIT = re.compile(
    r"[0-9０-９]+(?:\.[0-9０-９]+)?\s*(?:[%％]|[GgＧ]|pt|枚|回|周期|円)"
    r"|1\s*/\s*[0-9０-９]")


def _sha(obj) -> str:
    return cl.canonical_sha256(obj)


# ★★値の中身から単位を確定する（ラベルだけで決めない）★★
#   Codex 指摘＋実データの反例：
#     bandori「天井：最大10周期」          → 周期なのにG数天井にしていた
#     basilisk_tenzen「天井：BC間333G+α＋BC7スルー」→ 2つの天井を1値に潰していた
#     tekken6「リセット：200Gから狙い目」   → 狙い目なのに天井にしていた
#     tekken6「リセット：ポイント天井500pt」→ ptなのにGにしていた
_UNIT_PATTERNS = [
    (re.compile(r"(\d+)\s*周期"), "CYCLE", "周期"),
    (re.compile(r"(\d+)\s*pt"), "POINT", "pt"),
    (re.compile(r"(\d+)\s*スルー|(\d+)\s*回目"), "THROUGH", "回"),
    (re.compile(r"(\d+)\s*G"), None, "G"),          # basis は別途 C5 で確定
]
# 天井の値として認めない語（狙い目・編集判断が値側に入っている）
_VALUE_NOT_CEILING = re.compile(r"狙い目|から狙|目安|候補|様子見|ヤメ|やめ")


def value_shape(value: str):
    """値の文字列から (単位候補の集合, 数の個数) を返す。

    複数の単位が混ざる／数が複数ある値は、1つの claim に潰してはいけない。
    """
    units = []
    for rx, basis, unit in _UNIT_PATTERNS:
        if rx.search(value or ""):
            units.append(unit)
    nums = re.findall(r"\d+", value or "")
    return units, len(nums)


def resolve_ceiling(label: str, value: str):
    """天井系ラベルの型を、**値の中身から**確定する。確定できなければ None。

    返り値 None は「型に落ちない＝未分類として止める」を意味する。
    """
    if _VALUE_NOT_CEILING.search(value or ""):
        return None                       # 値が狙い目などを語っている
    units, n_nums = value_shape(value)
    if len(set(units)) != 1:
        return None                       # 単位が無い／複数混在（複合天井を潰さない）
    # ★同じ単位でも数が複数あれば1つの claim に潰さない★（Codex 指摘5）
    #   例：「通常1200G、設定変更後800G」「最大1200G（平均620G）」
    #   別々の枠にすべきものを1値にすると、どちらの数字か決まらない。
    if n_nums != 1:
        return None
    unit = units[0]
    if unit == "G":
        return {"unit": "G", "counter_basis": "UNKNOWN", "value_kind": "INTEGER"}
    if unit == "周期":
        return {"unit": "周期", "counter_basis": "CYCLE", "value_kind": "INTEGER"}
    if unit == "pt":
        return {"unit": "pt", "counter_basis": "POINT", "value_kind": "INTEGER"}
    if unit == "回":
        return {"unit": "回", "counter_basis": "THROUGH", "value_kind": "INTEGER"}
    return None


def classify_label(label: str):
    """ラベルを型に落とす。落ちなければ None。

    ★ラベル自体に設定が書いてあれば拾う★（「機械割（設定1）」「BIG確率(設定6)」）
      表の設定欄からしか取らないと、ラベル埋め込み型の記事が全部止まる。
    """
    lb = (label or "").strip()
    for rx, fk, mode, scope, cb, vk, unit in _LABEL_RE:
        m = rx.match(lb)
        if m:
            st = next((g for g in (m.groups() or ()) if g), None)
            return {"field_key": fk, "mode": mode, "scope": scope,
                    "counter_basis": cb, "value_kind": vk, "unit": unit,
                    "setting_from_label": normalize_setting(st) if st else None}
    return None


_ALPHA = re.compile(r"[+＋]\s*[aαａ]", re.I)

# ★「ちょうどN」と言い切れない書き方★（範囲・比較・概算・否定）
#   これを見逃すと「97.2%未満」を EXACT 97.2% として公開してしまう
#   （Codex 2回目 (a)-4）。+α は天井の慣用表現なので別扱い。
# ★★「禁止語を並べる」のではなく「残ってよい文字だけ」を決める★★
#   （Codex 2巡目 (a)-2）。から／を下回る／ではありません／推定 のように、
#   禁止語はいくらでも増やせるので、値の周りに**何も付いていない**ことを求める。
_VALUE_RESIDUE_OK = re.compile(
    r"(?:[\s、。：:（）()【】\[\]\*＊/／])+")             # 記号だけ
# 天井（MAX）でだけ許す語
_MAX_WORDS_OK = re.compile(r"最大|最低|上限|まで")

# ★★表示されている単位を実際に読み取る★★（Codex 3巡目 (a)-2）
#   以前は「単位らしき文字」をまとめて許していたため、記事が「97.2円」でも
#   枠の期待単位 % として通ってしまった。長い単位から順に取り除いて判定する。
_UNIT_TOKENS = [
    (re.compile(r"[GgＧ]\s*/\s*50\s*枚"), "G/50枚"),
    (re.compile(r"枚\s*/\s*[GgＧ]"), "枚/G"),
    (re.compile(r"[%％]"), "%"),
    (re.compile(r"pt|ポイント"), "pt"),
    (re.compile(r"周期"), "周期"),
    (re.compile(r"枚"), "枚"),
    (re.compile(r"回"), "回"),
    (re.compile(r"円"), "円"),
    (re.compile(r"[GgＧ]"), "G"),
]


def detect_units(value: str):
    """表示文に書かれている単位を **出現回数つき** で取り出す。

    ★回数を数えないと「97.2%％」「1200GG」が正常扱いになる★
      （Codex 4巡目 (a)-4）。集合にしてしまうと重複が消えるため、
      期待単位がちょうど1回であることを確かめられなくなる。
    """
    from collections import Counter
    rest, found = str(value or ""), Counter()
    for rx, u in _UNIT_TOKENS:
        hits = rx.findall(rest)
        if hits:
            found[u] += len(hits)
            rest = rx.sub("", rest)
    return found, rest


def normalize_value(value: str, unit: str, operator: str = "EXACT"):
    """後方互換：値だけを返す（理由が要るときは normalize_value_ex）。"""
    return normalize_value_ex(value, unit, operator)[0]


def normalize_value_ex(value: str, unit: str, operator: str = "EXACT"):
    """記事の値を「数 と 単位 と +α」に正規化する。決まらなければ None。

    ★これが無いと、記事を1300Gに変えても1200Gの古い claim で通る★
      （Codex 3回目 重大1）。slot_id には値が入らないので、値そのものを
      突き合わせないと「記事と台帳がずれたまま公開」できてしまう。
    """
    if not isinstance(value, str):
        return None, "NOT_A_STRING"
    # ★確率（1/x）は形そのものを見る★
    if unit == "1/x":
        m = re.fullmatch(r"\s*1\s*/\s*([0-9]+(?:\.[0-9]+)?)\s*", value)
        if not m:
            return None, "NOT_PROBABILITY_FORM"
        return {"amount": float(m.group(1)), "unit": unit, "plus_alpha": False}, "OK"
    nums = re.findall(r"\d+(?:\.\d+)?", value)
    if len(nums) != 1:
        return None, f"NUMBER_COUNT_{len(nums)}"
    # ★★表示されている単位が、枠の期待単位とちょうど同じであること★★
    units, rest = detect_units(value)
    seen = "+".join(f"{u}x{n}" for u, n in sorted(units.items())) or "なし"
    if unit:
        if units.get(unit) != 1 or len(units) != 1:
            # 「97.2円」を % の枠に入れない／「97.2%％」も通さない
            return None, f"UNIT_MISMATCH: 期待={unit} 表示={seen}"
    elif units:
        return None, f"UNIT_UNEXPECTED: 期待=なし 表示={seen}"
    # ★+α は天井（MAX）でだけ許す★（Codex 3巡目 (a)-3）
    #   機械割に「97.2%+α」を認めると、引用が「ちょうど97.2%」でも
    #   +α つきの記述を裏付けた扱いになってしまう。
    plus = bool(_ALPHA.search(value))
    if plus and operator != "MAX":
        return None, "PLUS_ALPHA_NOT_ALLOWED"
    if plus:
        rest = _ALPHA.sub("", rest)
    # ★残りは数と記号だけ★
    #   「97.2%を下回る」「約97.2%」「97.2%から99.9%」はここで落ちる。
    rest = re.sub(r"[0-9]+(?:\.[0-9]+)?", "", rest)
    if operator == "MAX":
        rest = _MAX_WORDS_OK.sub("", rest)
    leftover = _VALUE_RESIDUE_OK.sub("", rest).strip()
    if leftover:
        return None, f"UNALLOWED_RESIDUE: {leftover[:20]}"
    return {"amount": float(nums[0]), "unit": unit, "plus_alpha": plus}, "OK"


# ★枠が期待する演算子★（Codex 3回目 手順2）
#   天井は「最大N」（MAX）だが、機械割や確率は「ちょうどN」（EXACT）。
#   これを枠側に持たせないと、台帳が MAX と書いた機械割が通ってしまう。
_EXPECTED_OPERATOR = {
    "PERCENT": "EXACT", "PROBABILITY": "EXACT", "DECIMAL": "EXACT",
    "TEXT": "EXACT", "BOOLEAN": "EXACT",
}


def expected_operator(field_key: str, value_kind: str) -> str:
    if str(field_key).startswith("ceiling."):
        return "MAX"
    return _EXPECTED_OPERATOR.get(value_kind, "EXACT")


# ★表示上の単位の同定子★
#   「97.2%」と「0.972」は同じ意味でも別の書き方。どちらの書き方で
#   突き合わせるかを固定しないと、記事と出典で桁がずれても気づけない。
_RENDER_UNIT = {
    "%": "PERCENT_100BASE", "1/x": "PROBABILITY_ONE_OVER_X",
    "G": "GAMES", "pt": "POINTS", "周期": "CYCLES", "回": "TIMES",
    "枚": "COINS", "枚/G": "COINS_PER_GAME", "G/50枚": "GAMES_PER_50COINS",
    "円": "YEN", "": "NONE",
}


def render_unit_id(unit: str) -> str:
    return _RENDER_UNIT.get(unit, "UNKNOWN")


# 「設定1」「設1」「1」→ "1"。「設定V」「設定1〜3」などは決められないので None
_SETTING_RE = re.compile(r"^(?:設定|設)?\s*([1-6])\s*$")


def normalize_setting(raw):
    """設定欄の表記を正規値 "1"〜"6" に直す。決められなければ None。

    ★正規値にしないと突き合わせが効かない★（Codex 3回目 手順1）
      枠が「設定1」・台帳が "1" だと、同じ設定なのに別物と判定されるか、
      逆にどちらでも通ってしまう。表記を1つに固定する。
      「設定V」（ハナハナ系）や「設定1〜3」は決められないので None＝止める。
    """
    m = _SETTING_RE.match(str(raw or "").strip().translate(
        str.maketrans("０１２３４５６", "0123456")))
    return m.group(1) if m else None


# 「設1：97.4%」「設定6:106.5%」のような 設定と値の組
_SETTING_PAIR = re.compile(
    r"(?:設定|設)\s*([1-6])\s*[:：]\s*((?:1\s*/\s*)?[0-9]+(?:\.[0-9]+)?\s*[%％]?)")
# 組と組をつなぐだけの文字（これ以外が残るなら別の情報が混ざっている）
_SEPARATORS = re.compile(r"[\s/／、，,・|｜～〜\-—－]+")


def split_by_setting(value: str):
    """「設1：97.4% / 設2：98.2% …」を {設定: 値} に分解する。決まらなければ None。

    ★1つのセルに6設定ぶんの事実が入っている書き方への対応★
      まとめて1つの枠にすると「どの設定の値か」を検証できない。
      逆に、勝手に切り出すと取りこぼしが起きるので、
      **文字列が組だけで出来ている**ことを確かめてから分解する。
    """
    v = str(value or "")
    pairs = _SETTING_PAIR.findall(v)
    if len(pairs) < 2:
        return None                     # 1組だけなら通常の経路で扱う
    settings = [s for s, _ in pairs]
    if len(set(settings)) != len(settings):
        return None                     # 同じ設定が2回＝どちらか決まらない
    rest = _SETTING_PAIR.sub("", v)
    if _SEPARATORS.sub("", rest):
        return None                     # 組以外の情報が混ざっている
    return {s: val.strip() for s, val in pairs}


def identity_tuple(machine: dict) -> dict:
    """★機種を特定するための「変わらない情報」だけを取り出す★

    型番を機種データ全体の指紋にすると、記事の書き換えのような
    同定と関係ない変更でも型番が変わってしまう（Codex 4巡目 (a)-3）。
    同定に使う項目だけを固定する。
    """
    return {"slug": machine.get("slug"),
            "name": machine.get("name"),
            "info": machine.get("info"),
            "release_date": machine.get("release_date")}


def variant_key(slug: str, machine: dict) -> str:
    """★機種の型番を、同定情報から計算する★（Codex 3巡目 (a)-5 / 4巡目 (a)-3）

    台帳が自由に書ける文字列だと、出典側と台帳側に同じ嘘を書くだけで
    「型番一致」になってしまう（slug:FAKE ↔ slug:FAKE）。
    """
    return f"{slug}:{_sha(identity_tuple(machine))[:12]}"


def slot_id(slug: str, spec: dict, pointer: str) -> str:
    """slot の同定子。★表示文ではなく「型＋条件＋出力先」で決める★"""
    key = (f"{slug}|{spec['field_key']}|mode={spec['mode']};scope={spec['scope']};"
           f"basis={spec['counter_basis']}|{pointer}")
    return f"{slug}:{spec['field_key']}:{_sha(key)[:12]}"


def _walk(node, pointer: str, out: list):
    """記事JSONを JSON Pointer で全走査する（表示される文字列を集める）。"""
    if isinstance(node, str):
        out.append((pointer, node))
    elif isinstance(node, dict):
        for k, v in node.items():
            _walk(v, f"{pointer}/{k}", out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk(v, f"{pointer}/{i}", out)


def consumed_leaves(detail: dict) -> set:
    """「項目：値」として実際に読み取った**葉**の位置を返す。

    ★★親の位置で除外しない★★（Codex 3巡目 (a)-4）
      以前は組の位置（/factTable/1）の配下をまとめて除外していたため、
      「狙い目」欄の値に混ぜた天井の数値が文章検査に届かなかった。
      実際に消費した葉だけを除外する。
    """
    leaves = set()
    for i, row in enumerate(detail.get("factTable") or []):
        if isinstance(row, list) and len(row) >= 2:
            leaves.update({f"/factTable/{i}/0", f"/factTable/{i}/1"})
    for i, box in enumerate(detail.get("summaryBoxes") or []):
        if isinstance(box, dict) and "label" in box and "value" in box:
            leaves.update({f"/summaryBoxes/{i}/label", f"/summaryBoxes/{i}/value"})
    for si, sec in enumerate(detail.get("sections") or []):
        for ri, row in enumerate(sec.get("rows") or []):
            if isinstance(row, dict) and "trigger" in row:
                base = f"/sections/{si}/rows/{ri}"
                leaves.update({f"{base}/trigger", f"{base}/hint",
                               f"{base}/hint/text"})
        for ti, tbl in enumerate(sec.get("tables") or []):
            headers = [str(h) for h in (tbl.get("headers") or [])]
            by_setting = bool(headers) and headers[0].strip() in ("設定", "設定値")
            for ri, row in enumerate(tbl.get("rows") or []):
                if not isinstance(row, list) or len(row) < 2:
                    continue
                base = f"/sections/{si}/tables/{ti}/rows/{ri}"
                if by_setting:
                    leaves.add(f"{base}/0")
                    for ci in range(1, min(len(row), len(headers))):
                        leaves.update({f"{base}/{ci}", f"{base}/{ci}/text"})
                else:
                    leaves.update({f"{base}/0", f"{base}/1",
                                   f"{base}/0/text", f"{base}/1/text"})
        for bi, body in enumerate(sec.get("body") or []):
            if isinstance(body, str) and re.match(
                    r"^\*{0,2}([^：:*]{2,20})\*{0,2}\s*[：:]\s*(.+)$", body.strip()):
                leaves.add(f"/sections/{si}/body/{bi}")
    return leaves


def _pairs_from_detail(detail: dict) -> list:
    """記事から「ラベルと値の組」を取り出す（factTable / summaryBoxes / 表の行）。"""
    pairs = []
    for i, row in enumerate(detail.get("factTable") or []):
        if isinstance(row, list) and len(row) >= 2:
            pairs.append((f"/factTable/{i}", str(row[0]), str(row[1])))
    for i, box in enumerate(detail.get("summaryBoxes") or []):
        if isinstance(box, dict) and "label" in box and "value" in box:
            pairs.append((f"/summaryBoxes/{i}", str(box["label"]), str(box["value"])))
    for si, sec in enumerate(detail.get("sections") or []):
        for ri, row in enumerate(sec.get("rows") or []):
            if isinstance(row, dict) and "trigger" in row:
                hint = row.get("hint")
                val = hint.get("text") if isinstance(hint, dict) else hint
                pairs.append((f"/sections/{si}/rows/{ri}", str(row["trigger"]),
                              str(val)))
        for ti, tbl in enumerate(sec.get("tables") or []):
            headers = [str(h) for h in (tbl.get("headers") or [])]
            # ★1列目が「設定」の表は、列見出しが項目・行が設定★
            #   （行ラベル「設定1」だけでは何の値か決まらない。列と組にして初めて意味を持つ）
            by_setting = bool(headers) and headers[0].strip() in ("設定", "設定値")
            for ri, row in enumerate(tbl.get("rows") or []):
                if not isinstance(row, list) or len(row) < 2:
                    continue
                cells = [c.get("text") if isinstance(c, dict) else c for c in row]
                base = f"/sections/{si}/tables/{ti}/rows/{ri}"
                if by_setting:
                    setting = str(cells[0]).strip()
                    for ci in range(1, min(len(cells), len(headers))):
                        pairs.append((f"{base}/{ci}", headers[ci].strip(),
                                      str(cells[ci]), setting,
                                      f"{tbl.get('label') or ''}"))
                else:
                    pairs.append((base, str(cells[0]), str(cells[1])))
        # 「項目：値」形式の本文行（基本スペック欄など）
        for bi, body in enumerate(sec.get("body") or []):
            if not isinstance(body, str):
                continue
            m = re.match(r"^\*{0,2}([^：:*]{2,20})\*{0,2}\s*[：:]\s*(.+)$", body.strip())
            if m:
                pairs.append((f"/sections/{si}/body/{bi}", m.group(1).strip(),
                              m.group(2).strip()))
    return pairs


def _scan_excluded_value(unsupported: list, pointer: str, label: str,
                         value: str, reason: str) -> None:
    """★裏取り対象から外す欄でも、値に混ざった事実は必ず記録する★

    ラベルだけで「これは事実ではない」と決めると、値の中に書かれた
    天井や機械割が、検証も記録もされないまま公開される（Codex 3巡目・4巡目）。
    """
    v = value or ""
    if _NUM_WITH_UNIT.search(v) and re.search(_FACT_WORD, v):
        unsupported.append({"pointer": f"{pointer}#excluded_value",
                            "reason": reason, "label": label,
                            "excerpt": v[:70],
                            "content_sha256": _sha(f"{label}|{v}")})


def build_inventory(slug: str, machine: dict, detail: dict) -> dict:
    """1機種ぶんの在庫を作る。"""
    slots, unclassified = [], []
    unsupported: list = []
    # ★除外したものを黙って捨てない★（Codex 指摘）
    #   「未分類ゼロ」が網羅の証明になるためには、除外した理由も残っている必要がある。
    excluded_editorial, excluded_nonclaim = [], []
    seen_slots = set()

    # ★★構造化されていない本文の数値を取りこぼさない★★（Codex 3回目 重大3）
    #   「AT間天井は1200Gです」のような文章は「項目：値」形式でないため
    #   在庫にも未分類にも残らなかった。＝「抽出できなかった」と「事実が無い」を
    #   区別できていない状態。文章中の事実らしい数値は UNSUPPORTED として残す。
    strings: list = []
    _walk(detail, "", strings)
    structured = consumed_leaves(detail)
    for pointer, text in strings:
        # ★実際に「項目：値」として読み取った葉だけを除外する★
        if pointer in structured:
            continue
        # ★★文ごとに判定する★★（Codex 2回目 (a)-5）
        #   段落まるごとで判定していたため、「狙い目は300Gだが、天井は9999Gです」
        #   のように編集判断の語が1つ入るだけで、同じ段落の事実が消えていた。
        for si_, sent in enumerate(re.split(r"(?<=[。\n])", str(text or ""))):
            if not sent.strip():
                continue
            # ★単位つきの数字を含む文は、事実を語っているとみなす★
            if not _NUM_WITH_UNIT.search(sent):
                continue
            # ★編集判断の文でも、項目語が入っていれば事実として拾う★
            #   「狙い目は300G〜」＝編集判断（対象外）
            #   「狙い目は300Gだが、天井は9999Gです」＝天井の事実を含む（対象）
            if EDITORIAL_LABELS.search(sent) and not re.search(_FACT_WORD, sent):
                continue
            unsupported.append({"pointer": f"{pointer}#{si_}",
                                "reason": "FACT_IN_PROSE_NOT_EXTRACTED",
                                "excerpt": sent.strip()[:70],
                                "content_sha256": _sha(sent)})

    for item in _pairs_from_detail(detail):
        pointer, label, value = item[0], item[1], item[2]
        setting_raw = item[3] if len(item) > 3 else None
        table_label = item[4] if len(item) > 4 else None
        # ★設定は「表の設定欄」→「ラベル埋め込み」の順で決める★
        _lab = classify_label(label)
        _lab_setting = _lab.get("setting_from_label") if _lab else None
        setting = normalize_setting(setting_raw) if setting_raw is not None else None
        # ★★2つの設定表示が食い違ったら止める★★（Codex 2回目 (a)-6）
        #   行が「設定1」・列見出しが「機械割（設定6）」のような記事を
        #   片方だけ採ると、公開表と検証値が別の設定になる。
        if setting and _lab_setting and setting != _lab_setting:
            unclassified.append({"pointer": pointer, "label": label,
                                 "reason": "SETTING_SIGNAL_CONFLICT",
                                 "value_excerpt": f"行={setting} / 見出し={_lab_setting}"})
            continue
        if setting is None:
            setting = _lab_setting
        if setting_raw is not None and setting is None:
            # ★どの設定の値か決められない行は枠にしない★
            #   （「設定V」「設定1〜3」など。値だけ拾うと設定を取り違える）
            unclassified.append({"pointer": pointer, "label": label,
                                 "reason": "SETTING_NOT_NORMALIZED",
                                 "value_excerpt": str(setting_raw)[:30]})
            continue
        if EDITORIAL_LABELS.search(label):
            # 編集判断（B区分）は裏取り対象外。ただし記録は残す
            excluded_editorial.append({"pointer": pointer, "label": label,
                                       "reason": "EDITORIAL_JUDGMENT"})
            # ★★編集判断の欄に混ぜた事実を見逃さない★★（Codex 3巡目 (a)-4）
            #   「狙い目: 300G。天井は9999Gです」のように、ラベルが編集判断でも
            #   値の中に事実が入っていることがある。
            _scan_excluded_value(unsupported, pointer, label, value,
                                 "FACT_IN_EDITORIAL_VALUE")
            continue
        if NONCLAIM_LABELS.match(label.strip()):
            excluded_nonclaim.append({"pointer": pointer, "label": label,
                                      "reason": "NOT_A_NUMERIC_CLAIM"})
            # ★★案内文の欄に混ぜた事実も見逃さない★★（Codex 4巡目 (a)-2）
            _scan_excluded_value(unsupported, pointer, label, value,
                                 "FACT_IN_NONCLAIM_VALUE")
            continue
        if HINT_ROW_LABELS.match(label.strip()):
            # ★設定示唆はA区分の事実★ 型が未実装なので「未対応の事実」として残す。
            #   黙って素通りさせると「未分類ゼロ」が嘘になる（Codex 指摘）。
            unsupported.append({"pointer": pointer, "label": label,
                                "reason": "UNSUPPORTED_FACT_TABLE",
                                "kind": "SETTING_HINT_TABLE",
                                "content_sha256": _sha(f"{label}|{value}")})
            continue
        spec = classify_label(label)
        # ★天井系は値の中身で単位を確定できたときだけ型を起こす★
        if spec is not None and spec["field_key"].startswith("ceiling."):
            shape = resolve_ceiling(label, value)
            # ★ラベルが具体的な天井（AT間等）なのに単位がGでなければ止める★
            #   「AT間天井: 500pt」を field_key=ceiling.normal.at のまま
            #   unit だけ pt に差し替えて通してしまうのを防ぐ。
            if (shape and spec["field_key"] not in ("ceiling.normal", "ceiling.reset")
                    and shape["unit"] != spec.get("unit", "G")):
                unclassified.append({"pointer": pointer, "label": label,
                                     "reason": "CEILING_UNIT_MISMATCH",
                                     "value_excerpt": (value or "")[:60]})
                continue
            if shape is None:
                unclassified.append({"pointer": pointer, "label": label,
                                     "reason": "AMBIGUOUS_CEILING_VALUE",
                                     "value_excerpt": (value or "")[:60]})
                continue
            spec = {**spec, **shape}
        if spec is None:
            # ★数値を含むのに型に落ちない＝止める対象★
            if _NUM.search(value):
                unclassified.append({"pointer": pointer, "label": label,
                                     "reason": "UNKNOWN_LABEL_WITH_NUMBER"})
            continue
        # ★★設定ごとの値なのに設定が分からない枠は起こさない★★
        #   （Codex 3回目 手順1）「機械割 97.2%〜106.5%」のような
        #   設定が特定できない書き方を、設定なしの枠として通してしまうと
        #   どの設定の値かを検証できないまま公開経路に乗る。
        emit = [(pointer, value, setting)]
        if spec["field_key"] in cl.SETTING_REQUIRED_FIELDS and not setting:
            # 「設1：97.4% / 設2：98.2% …」と1セルに詰まっている書き方を分解する
            split = split_by_setting(value)
            if not split:
                unclassified.append({"pointer": pointer, "label": label,
                                     "reason": "SETTING_REQUIRED_BUT_MISSING",
                                     "value_excerpt": (value or "")[:60]})
                continue
            emit = [(f"{pointer}/setting={s}", split[s], s)
                    for s in sorted(split)]
        for pointer, value, setting in emit:
            _emit_slot(slots, seen_slots, slug, spec, pointer, label, value,
                       setting, table_label)

    return _finish(slug, machine, detail, slots, unclassified, unsupported,
                   excluded_editorial, excluded_nonclaim)


def _emit_slot(slots, seen_slots, slug, spec, pointer, label, value,
               setting, table_label):
        # ★設定1〜6は行単位で束ねる（1つ欠けたら行ごと止めるため）★
        group = None
        if setting:
            group = f"{slug}:{spec['field_key']}:{table_label or 'by_setting'}"
        _op = expected_operator(spec["field_key"], spec["value_kind"])
        _cv, _cvwhy = normalize_value_ex(value, spec["unit"], _op)
        sid = slot_id(slug, spec, pointer)
        if sid in seen_slots:
            return
        seen_slots.add(sid)
        slots.append({
            "slot_id": sid,
            "field_key": spec["field_key"],
            "conditions": {"mode": spec["mode"], "scope": spec["scope"],
                           "counter_basis": spec["counter_basis"],
                           "setting": setting},
            "atomic_group_id": group,
            "expected_value_kind": spec["value_kind"],
            "expected_unit": spec["unit"],
            # ★枠が期待する演算子と書き方（台帳の申告と突き合わせる）★
            "expected_operator": _op,
            "expected_setting": setting,
            "render_unit_id": render_unit_id(spec["unit"]),
            "source_pointer": pointer,
            "current_text": value,
            # ★記事の表示文そのものの指紋★（記事が書き換わったら気づく）
            "source_text_sha256": _sha(f"{label}|{value}"),
            # ★記事に載っている値そのもの（台帳と突き合わせる）★
            "current_value": _cv,
            # ★値にできなかった理由を残す★（Codex 4巡目 (b)-1）
            "value_issue": None if _cv else _cvwhy,
            # ★許可リスト照合には枠が期待する演算子を渡す★（Codex (b)-3）
            #   常に MAX を渡していたため、EXACT の機械割が型外に見えていた。
            "allowlisted_type": cl.allowlisted_type_candidate(
                {"field_key": spec["field_key"],
                 "value": {"kind": spec["value_kind"], "unit": spec["unit"],
                           "operator": _op},
                 "conditions": {"mode": spec["mode"], "scope": spec["scope"]}}),
            "verify_state": "UNVERIFIED",
        })


def _finish(slug, machine, detail, slots, unclassified, unsupported,
            excluded_editorial, excluded_nonclaim):
    return {
        "schema_version": SCHEMA_VERSION,
        "inventory_id": f"{slug}:inventory:{GENERATOR_VERSION}",
        "slug": slug,
        "generator_version": GENERATOR_VERSION,
        "input_hashes": {
            "machine_record_sha256": _sha(machine),
            "detail_json_sha256": _sha(detail),
        },
        "slots": slots,
        "unclassified_atoms": unclassified,
        "unsupported_facts": unsupported,
        "excluded_editorial_atoms": excluded_editorial,
        "excluded_nonclaim_atoms": excluded_nonclaim,
        "coverage": {
            "slots_total": len(slots),
            "allowlisted_type": sum(1 for s in slots if s["allowlisted_type"]),
            "unclassified_atoms": len(unclassified),
            "unsupported_facts": len(unsupported),
            # ★記事の値を1つに特定できない枠★（Codex 3巡目 (b)-2）
            #   これがあると公開できない（reconcile が止める）ので、
            #   在庫の集計にも出しておく。
            "unnormalized_slots": sum(1 for s in slots if s["current_value"] is None),
            "excluded_editorial": len(excluded_editorial),
            "excluded_nonclaim": len(excluded_nonclaim),
            # ★未分類も「型が未実装の事実」も残っていれば公開不可★
            #   （素通りさせると「未分類ゼロ」が網羅の証明にならない）
            "publishable": (len(unclassified) == 0 and len(unsupported) == 0
                            and all(s["current_value"] is not None for s in slots)),
        },
    }


def load_machine(slug: str):
    ms = json.load(open(os.path.join(DATA, "machines.json"), encoding="utf-8"))
    m = next((x for x in ms if x.get("slug") == slug), None)
    dp = os.path.join(DATA, "machine-details", f"{slug}.json")
    d = json.load(open(dp, encoding="utf-8")) if os.path.isfile(dp) else {}
    return m, d


def _basis_unknown_blocks_verified() -> bool:
    """counter_basis が UNKNOWN のまま VERIFIED にできないことを確かめる。"""
    c = cl._mk_claim()
    c["conditions"] = {**c["conditions"], "counter_basis": "UNKNOWN"}
    try:
        cl.validate_claim(c, "t")
    except cl.LedgerError:
        return True
    return False


def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    t("★AT間天井とCZ間天井を別の型に落とす（同じ数字でも別物）",
      classify_label("AT間天井")["field_key"] == "ceiling.normal.at"
      and classify_label("CZ間天井")["field_key"] == "ceiling.normal.cz")
    t("★★数え方(counter_basis)はラベルから推測しない★★",
      classify_label("AT間天井")["counter_basis"] == "UNKNOWN"
      and classify_label("CZ間天井")["counter_basis"] == "UNKNOWN")
    t("　（理由）実データに反例がある：gundam_uc2はAT間が液晶、sao2はCZ間が実ゲーム数",
      True)
    t("★数え方が未確定のまま VERIFIED にできない",
      _basis_unknown_blocks_verified())
    t("★「通常時」だけでは天井と決めつけない（純増・確率にも使われる語）",
      classify_label("通常時") is None)
    t("★★同じ単位でも数が複数なら1値に潰さない★★（Codex 指摘5）",
      resolve_ceiling("AT間天井", "通常1200G、設定変更後800G") is None
      and resolve_ceiling("AT間天井", "最大1200G（平均620G）") is None)
    t("　単一の値なら通す", resolve_ceiling("AT間天井", "1200G+α") is not None)
    t("★★ラベルの天井種別と単位が食い違えば止める（AT間天井:500pt）★★",
      build_inventory("x", {"slug": "x"},
                      {"factTable": [["AT間天井", "500pt"]]}
                      )["unclassified_atoms"][0]["reason"] == "CEILING_UNIT_MISMATCH")
    t("★★本文の文章に紛れた事実を取りこぼさない★★（Codex 3回目 重大3）",
      build_inventory("x", {"slug": "x"},
                      {"sections": [{"body": ["AT間天井は1200Gです"]}]}
                      )["coverage"]["unsupported_facts"] == 1)
    t("　その場合は公開不可になる",
      build_inventory("x", {"slug": "x"},
                      {"sections": [{"body": ["AT間天井は1200Gです"]}]}
                      )["coverage"]["publishable"] is False)
    t("★記事の値を正規化して枠に持たせる（台帳と突き合わせるため）",
      build_inventory("x", {"slug": "x"},
                      {"factTable": [["AT間天井", "1200G+α"]]}
                      )["slots"][0]["current_value"] ==
      {"amount": 1200.0, "unit": "G", "plus_alpha": True})
    t("★未知ラベルは推測せず None（勝手に型を作らない）",
      classify_label("謎の項目") is None)
    t("編集判断のラベルは裏取り対象にしない",
      bool(EDITORIAL_LABELS.search("狙い目")) and bool(EDITORIAL_LABELS.search("ヤメ時の判断")))

    det = {"factTable": [["AT間天井", "1200G+α"], ["CZ間天井", "600G+α"],
                         ["謎の指標", "1234"], ["主な狙い方", "CZ間250G〜"]]}
    inv = build_inventory("x", {"slug": "x"}, det)
    t("在庫に2つのslotができる", inv["coverage"]["slots_total"] == 2)
    t("★型に落ちない数値は未分類として止める",
      inv["coverage"]["unclassified_atoms"] == 1
      and inv["unclassified_atoms"][0]["label"] == "謎の指標")
    t("★未分類があれば公開不可になる", inv["coverage"]["publishable"] is False)
    t("編集判断（主な狙い方）は未分類に入れない",
      all(u["label"] != "主な狙い方" for u in inv["unclassified_atoms"]))
    t("★AT間天井は自動採用の対象・CZ間天井は対象外（許可リストどおり）",
      [s["allowlisted_type"] for s in inv["slots"]
       if s["field_key"] == "ceiling.normal.at"][0] is True
      and [s["allowlisted_type"] for s in inv["slots"]
           if s["field_key"] == "ceiling.normal.cz"][0] is False)

    inv2 = build_inventory("x", {"slug": "x"}, det)
    t("★同じ入力なら同じ在庫になる（決定論）", _sha(inv) == _sha(inv2))
    det3 = {"factTable": [["AT間天井", "1300G+α"]]}
    t("★入力が変われば指紋も変わる",
      build_inventory("x", {"slug": "x"}, det3)["input_hashes"]["detail_json_sha256"]
      != inv["input_hashes"]["detail_json_sha256"])

    # -------- 設定ごとの値・期待する演算子（Codex 3回目 手順1・2）
    kw = build_inventory("x", {"slug": "x"},
                         {"factTable": [["機械割", "97.2%〜106.5%"]]})
    t("★★設定が分からない機械割は枠にしない（未分類で止める）★★",
      kw["coverage"]["slots_total"] == 0
      and kw["unclassified_atoms"][0]["reason"] == "SETTING_REQUIRED_BUT_MISSING")
    kw2 = build_inventory("x", {"slug": "x"},
                          {"factTable": [["機械割(設定6)", "106.5%"]]})
    t("ラベルに書かれた設定を拾う（機械割(設定6)）",
      kw2["coverage"]["slots_total"] == 1
      and kw2["slots"][0]["conditions"]["setting"] == "6")
    kw3 = build_inventory("x", {"slug": "x"}, {"factTable": [
        ["機械割", "設1：97.4% / 設2：98.2% / 設3：100.1% / "
                   "設4：104.1% / 設5：107.3% / 設6：110.2%"]]})
    t("★1セルに詰まった6設定を、設定ごとの枠に分解する",
      kw3["coverage"]["slots_total"] == 6
      and kw3["slots"][5]["current_value"]["amount"] == 110.2)
    t("　分解した枠は同じ束に入る（1つ欠けたら行ごと止める）",
      len({s["atomic_group_id"] for s in kw3["slots"]}) == 1)
    t("★★組以外の情報が混ざっていたら分解しない★★",
      split_by_setting("設1：97.4% / 設2：98.2%（完全攻略時）") is None)
    t("★同じ設定が2回出てきたら分解しない",
      split_by_setting("設1：97.4% / 設1：98.2%") is None)
    t("★設定Vなど数字でない設定は正規化しない（止める）",
      normalize_setting("設定V") is None and normalize_setting("設定1〜3") is None)
    t("　全角数字は正規化する", normalize_setting("設定６") == "6")
    # -------- Codex 2回目の反例
    t("★★「97.2%未満」を ちょうど97.2% として扱わない★★",
      normalize_value("97.2%未満", "%") is None
      and normalize_value("約97.2%", "%") is None
      and normalize_value("97.2%〜99.9%", "%") is None)
    t("★★禁止語リストでは防げない書き方も落とす（を下回る／から）★★",
      normalize_value("97.2%を下回る", "%") is None
      and normalize_value("97.2%から99.9%", "%") is None
      and normalize_value("97.2%ではありません", "%") is None)
    t("　素直な値は通る（97.2% / 1200G+α）",
      normalize_value("97.2%", "%")["amount"] == 97.2
      and normalize_value("1200G+α", "G", "MAX")["plus_alpha"] is True)
    t("　天井の「最大1200G」は MAX なので通す",
      normalize_value("最大1200G", "G", "MAX")["amount"] == 1200.0
      and normalize_value("最大97.2%", "%", "EXACT") is None)
    t("★★項目語と数字が離れていても取りこぼさない★★（Codex 2巡目 (a)-4）",
      build_inventory("x", {"slug": "x"}, {"sections": [{"body": [
          "機械割についてはメーカー資料の算出条件、対象遊技状態、技術条件を"
          "慎重に確認した結果として、設定1は99.9%です。"]}]}
          )["coverage"]["unsupported_facts"] == 1)
    t("★★項目語辞書に無い書き方も取りこぼさない（BIGは設定1で1/999）★★",
      build_inventory("x", {"slug": "x"},
                      {"sections": [{"body": ["BIGは設定1で1/999です。"]}]}
                      )["coverage"]["unsupported_facts"] == 1)
    t("★★数字が先の本文事実も取りこぼさない（9999Gで天井に到達）★★",
      build_inventory("x", {"slug": "x"},
                      {"sections": [{"body": ["9999Gで天井に到達します"]}]}
                      )["coverage"]["unsupported_facts"] == 1)
    t("★★編集判断の語が同じ段落にあっても、事実の文は拾う★★",
      build_inventory("x", {"slug": "x"},
                      {"sections": [{"body": ["狙い目は300Gだが、天井は9999Gです"]}]}
                      )["coverage"]["unsupported_facts"] == 1)
    t("★★行の設定と見出しの設定が食い違えば止める★★",
      build_inventory("x", {"slug": "x"}, {"sections": [{"tables": [
          {"headers": ["設定", "機械割（設定6）"],
           "rows": [["設定1", "97.2%"]]}]}]}
          )["unclassified_atoms"][0]["reason"] == "SETTING_SIGNAL_CONFLICT")
    # -------- Codex 3巡目の反例
    t("★★表示単位が枠の期待単位と違えば値にしない（97.2円を%の枠に入れない）★★",
      normalize_value("97.2円", "%") is None
      and normalize_value("97.2G", "%") is None
      and normalize_value("97.2枚", "%") is None)
    t("★★+α は天井（MAX）でだけ許す（機械割97.2%+αを通さない）★★",
      normalize_value("97.2%+α", "%", "EXACT") is None
      and normalize_value("1200G+α", "G", "MAX")["plus_alpha"] is True)
    t("★確率は 1/x の形そのものを見る",
      normalize_value("1/259.0", "1/x")["amount"] == 259.0
      and normalize_value("259.0", "1/x") is None)
    t("★★編集判断の欄に混ぜた事実を見逃さない★★",
      build_inventory("x", {"slug": "x"}, {"factTable": [
          ["機械割(設定1)", "97.2%"],
          ["狙い目", "300G。天井は9999Gです"]]}
          )["coverage"]["unsupported_facts"] == 1)
    t("★★表の3列目以降の数値文も隠さない★★",
      build_inventory("x", {"slug": "x"}, {"sections": [{"tables": [
          {"headers": ["項目", "値", "備考"],
           "rows": [["AT間天井", "1200G", "天井は9999Gという情報もあります"]]}]}]}
          )["coverage"]["unsupported_facts"] >= 1)
    t("★★機種の型番は機種データから計算する（台帳が名乗れない）★★",
      variant_key("x", {"slug": "x", "name": "A"})
      != variant_key("x", {"slug": "x", "name": "B"})
      and variant_key("x", {"slug": "x"}).startswith("x:"))
    t("★値を1つに特定できない枠があれば公開不可になる",
      build_inventory("x", {"slug": "x"},
                      {"factTable": [["機械割(設定1)", "97.2%です"]]}
                      )["coverage"]["publishable"] is False)
    # -------- Codex 4巡目の反例
    t("★★同じ単位を2回書いた値を通さない（97.2%％ / 1200GG）★★",
      normalize_value("97.2%％", "%") is None
      and normalize_value("1200GG", "G", "MAX") is None)
    t("★★案内文の欄に混ぜた事実も見逃さない（機種名の欄）★★",
      build_inventory("x", {"slug": "x"}, {"factTable": [
          ["機種名", "テスト機。設定1の機械割は99.9%"],
          ["機械割(設定1)", "97.2%"]]}
          )["coverage"]["unsupported_facts"] == 1)
    t("★値にできない理由が残る（単位違いが分かる）",
      "UNIT_MISMATCH" in (normalize_value_ex("97.2円", "%")[1] or ""))
    t("★機種の型番は同定情報だけから計算する（記事の書き換えで変わらない）",
      variant_key("x", {"slug": "x", "name": "A", "seo": {"title": "old"}})
      == variant_key("x", {"slug": "x", "name": "A", "seo": {"title": "new"}})
      and variant_key("x", {"slug": "x", "name": "A"})
      != variant_key("x", {"slug": "x", "name": "B"}))
    t("★機械割の枠が許可リストの型として数えられる（演算子EXACTで照合）",
      build_inventory("x", {"slug": "x"},
                      {"factTable": [["機械割(設定6)", "106.5%"]]}
                      )["slots"][0]["allowlisted_type"] is True)
    t("★機械割の枠は EXACT を期待する（最大N ではない）",
      expected_operator("kikaiwari.setting", "PERCENT") == "EXACT")
    t("★天井の枠は MAX を期待する",
      expected_operator("ceiling.normal.at", "INTEGER") == "MAX")
    t("★％と1/xの書き方を枠が固定する（桁ずれに気づけるように）",
      render_unit_id("%") == "PERCENT_100BASE"
      and render_unit_id("1/x") == "PROBABILITY_ONE_OVER_X")
    t("★枠は記事の表示文の指紋を持つ（記事が書き換わったら気づく）",
      len(inv["slots"][0]["source_text_sha256"]) == 64)

    ng = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--slug")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--write", action="store_true", help="在庫をファイルへ書き出す")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if args.slug:
        m, d = load_machine(args.slug)
        if m is None:
            print("機種が見つからない:", args.slug)
            return 1
        inv = build_inventory(args.slug, m, d)
        print(json.dumps(inv["coverage"], ensure_ascii=False, indent=1))
        for s in inv["slots"]:
            mark = "型OK" if s["allowlisted_type"] else "型外"
            # ★値を1つに特定できない枠は、型OKでも公開できない★
            if s["current_value"] is None:
                mark = "値NG"
            print(f"  [{mark}] {s['field_key']:<22} {s['current_text'][:40]}")
        for u in inv["unclassified_atoms"]:
            print(f"  [未分類] {u['label']}  ({u['pointer']}) "
                  f"{u.get('reason')} {u.get('value_excerpt', '')}")
        # ★型が未実装の事実も場所と中身を出す★（Codex (b)-2）
        for u in inv["unsupported_facts"]:
            print(f"  [未対応] {u.get('pointer')} {u.get('reason')} "
                  f"{(u.get('excerpt') or u.get('label') or '')[:60]}")
        if args.write:
            os.makedirs(OUT_DIR, exist_ok=True)
            json.dump(inv, open(os.path.join(OUT_DIR, f"{args.slug}.json"), "w",
                                encoding="utf-8"), ensure_ascii=False, indent=1)
            print("書き出し:", os.path.join(OUT_DIR, f"{args.slug}.json"))
        return 0

    if args.all:
        rel = json.load(open(os.path.join(BASE, "_design", "release_slugs.json"),
                             encoding="utf-8"))
        tot_slots = tot_auto = tot_unc = 0
        tot_uns = unc_zero = tot_unnorm = 0
        pub_ok = 0
        from collections import Counter
        fk = Counter()
        reasons = Counter()
        for slug in rel["publish"]:
            m, d = load_machine(slug)
            if m is None:
                continue
            inv = build_inventory(slug, m, d)
            tot_slots += inv["coverage"]["slots_total"]
            tot_auto += inv["coverage"]["allowlisted_type"]
            tot_unc += inv["coverage"]["unclassified_atoms"]
            tot_uns += inv["coverage"]["unsupported_facts"]
            tot_unnorm += inv["coverage"]["unnormalized_slots"]
            for s in inv["slots"]:
                if s["current_value"] is None:
                    reasons["VALUE_" + str(s.get("value_issue")).split(":")[0]] += 1
            unc_zero += 1 if inv["coverage"]["unclassified_atoms"] == 0 else 0
            pub_ok += 1 if inv["coverage"]["publishable"] else 0
            for s in inv["slots"]:
                fk[s["field_key"]] += 1
            for u in inv["unclassified_atoms"]:
                reasons[u.get("reason", "?")] += 1
            for u in inv["unsupported_facts"]:
                reasons[u.get("reason", "?")] += 1
        n = len(rel["publish"])
        print("=" * 62)
        print(f"公開予定 {n} 機種の在庫")
        print(f"  検証が要る slot 合計 : {tot_slots}")
        print(f"  うち許可リストに載る型: {tot_auto}  ※型が載っているだけ。"
              f"自動採用には検証済み(VERIFIED)が別途必要")
        # ★「未分類ゼロ」と「本文の未対応もゼロ」を分けて出す★（Codex 2巡目 (b)-4）
        print(f"  型に落ちない（未分類）: {tot_unc}")
        print(f"  型が未実装の事実      : {tot_uns}")
        print(f"  値を特定できない枠    : {tot_unnorm}")
        print(f"  未分類ゼロの機種      : {unc_zero} / {n}")
        print(f"  未分類も未対応もゼロ  : {pub_ok} / {n}  ※これが公開の前提")
        print("-" * 62)
        print("■ 止まっている理由の内訳")
        for k, v in reasons.most_common():
            print(f"   {k:<32} {v:>5}")
        print("-" * 62)
        print("■ 型ごとの slot 数")
        for k, v in fk.most_common():
            print(f"   {k:<24} {v:>4}")
        print("=" * 62)
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

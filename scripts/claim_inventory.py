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
    (r"^BIG$|^BIG確率$|^BB$|^BB確率$|^BIG確率[（(]設定\d[)）]$|^BB確率[（(]設定\d[)）]$",
     "prob.big", "ANY", "NONE", "NONE", "PROBABILITY", "1/x"),
    (r"^REG$|^REG確率$|^RB$|^RB確率$|^REG確率[（(]設定\d[)）]$|^RB確率[（(]設定\d[)）]$",
     "prob.reg", "ANY", "NONE", "NONE", "PROBABILITY", "1/x"),
    (r"^初当たり確率$|^初当たり確率[（(]設定\d[)）]$|^AT初当たり確率$",
     "prob.first_hit", "ANY", "NONE", "NONE", "PROBABILITY", "1/x"),
    (r"^BIG獲得枚数$|^BB獲得枚数$", "payout.big", "ANY", "NONE", "COIN",
     "INTEGER", "枚"),
    (r"^REG獲得枚数$|^RB獲得枚数$", "payout.reg", "ANY", "NONE", "COIN",
     "INTEGER", "枚"),
    (r"^ベース$", "base_game", "ANY", "NONE", "COIN", "DECIMAL", "G/50枚"),
    (r"^コイン単価$", "coin_unit_price", "ANY", "NONE", "COIN", "DECIMAL", "円"),
    (r"^ぶどう確率$|^ブドウ確率$", "prob.grape", "ANY", "NONE", "NONE",
     "PROBABILITY", "1/x"),
    (r"^ボーナス合算確率$|^合算$|^合算確率$|^ボーナス合算$|^ボーナス合算[（(]設定\d[)）]$",
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
_FACT_IN_PROSE = re.compile(
    r"(?:天井|機械割|出玉率|純増|確率|獲得枚数|コイン持ち|スルー|周期)"
    r"[^。]{0,20}?[0-9０-９]")


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
    """ラベルを型に落とす。落ちなければ None。"""
    lb = (label or "").strip()
    for rx, fk, mode, scope, cb, vk, unit in _LABEL_RE:
        if rx.match(lb):
            return {"field_key": fk, "mode": mode, "scope": scope,
                    "counter_basis": cb, "value_kind": vk, "unit": unit}
    return None


_ALPHA = re.compile(r"[+＋]\s*[aαａ]", re.I)


def normalize_value(value: str, unit: str):
    """記事の値を「数 と 単位 と +α」に正規化する。決まらなければ None。

    ★これが無いと、記事を1300Gに変えても1200Gの古い claim で通る★
      （Codex 3回目 重大1）。slot_id には値が入らないので、値そのものを
      突き合わせないと「記事と台帳がずれたまま公開」できてしまう。
    """
    if not isinstance(value, str):
        return None
    nums = re.findall(r"\d+(?:\.\d+)?", value)
    if len(nums) != 1:
        return None
    return {"amount": float(nums[0]), "unit": unit,
            "plus_alpha": bool(_ALPHA.search(value))}


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
    structured = {it[0] for it in _pairs_from_detail(detail)}
    for pointer, text in strings:
        if pointer in structured or not _FACT_IN_PROSE.search(text or ""):
            continue
        if EDITORIAL_LABELS.search(text or ""):
            continue                        # 狙い目などの編集判断は対象外
        unsupported.append({"pointer": pointer,
                            "reason": "FACT_IN_PROSE_NOT_EXTRACTED",
                            "excerpt": (text or "")[:70],
                            "content_sha256": _sha(text)})

    for item in _pairs_from_detail(detail):
        pointer, label, value = item[0], item[1], item[2]
        setting = item[3] if len(item) > 3 else None
        table_label = item[4] if len(item) > 4 else None
        if EDITORIAL_LABELS.search(label):
            # 編集判断（B区分）は裏取り対象外。ただし記録は残す
            excluded_editorial.append({"pointer": pointer, "label": label,
                                       "reason": "EDITORIAL_JUDGMENT"})
            continue
        if NONCLAIM_LABELS.match(label.strip()):
            excluded_nonclaim.append({"pointer": pointer, "label": label,
                                      "reason": "NOT_A_NUMERIC_CLAIM"})
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
        # ★設定1〜6は行単位で束ねる（1つ欠けたら行ごと止めるため）★
        group = None
        if setting:
            group = f"{slug}:{spec['field_key']}:{table_label or 'by_setting'}"
        sid = slot_id(slug, spec, pointer)
        if sid in seen_slots:
            continue
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
            "source_pointer": pointer,
            "current_text": value,
            # ★記事に載っている値そのもの（台帳と突き合わせる）★
            "current_value": normalize_value(value, spec["unit"]),
            "allowlisted_type": cl.allowlisted_type_candidate(
                {"field_key": spec["field_key"],
                 "value": {"kind": spec["value_kind"], "unit": spec["unit"],
                           "operator": "MAX"},
                 "conditions": {"mode": spec["mode"], "scope": spec["scope"]}}),
            "verify_state": "UNVERIFIED",
        })

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
            "excluded_editorial": len(excluded_editorial),
            "excluded_nonclaim": len(excluded_nonclaim),
            # ★未分類も「型が未実装の事実」も残っていれば公開不可★
            #   （素通りさせると「未分類ゼロ」が網羅の証明にならない）
            "publishable": len(unclassified) == 0 and len(unsupported) == 0,
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
            print(f"  [{mark}] {s['field_key']:<22} {s['current_text'][:40]}")
        for u in inv["unclassified_atoms"]:
            print(f"  [未分類] {u['label']}  ({u['pointer']})")
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
        pub_ok = 0
        from collections import Counter
        fk = Counter()
        for slug in rel["publish"]:
            m, d = load_machine(slug)
            if m is None:
                continue
            inv = build_inventory(slug, m, d)
            tot_slots += inv["coverage"]["slots_total"]
            tot_auto += inv["coverage"]["allowlisted_type"]
            tot_unc += inv["coverage"]["unclassified_atoms"]
            pub_ok += 1 if inv["coverage"]["publishable"] else 0
            for s in inv["slots"]:
                fk[s["field_key"]] += 1
        n = len(rel["publish"])
        print("=" * 62)
        print(f"公開予定 {n} 機種の在庫")
        print(f"  検証が要る slot 合計 : {tot_slots}")
        print(f"  うち許可リストに載る型: {tot_auto}  ※型が載っているだけ。"
              f"自動採用には検証済み(VERIFIED)が別途必要")
        print(f"  未分類（要対応）     : {tot_unc}")
        print(f"  未分類ゼロの機種     : {pub_ok} / {n}")
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

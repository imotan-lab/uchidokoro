"""spec_lookup.py — 大手の名鑑2件から記事の材料を引き、一致したものだけ採る。

★運営者の方針（2026-07-31）★
  「P-WORLD と DMMぱちタウンは大手で信用できる。両方が同じならその内容を使う。
    違ったら別サイトも調べて裏取りする」

★それでも形の検査は外さない★
  素朴に見出しの近くの値を拾うと取り違える。実際、最初の実装では
  「出玉率」の欄に `1/498.7`（＝AT確率）を拾ってしまった。
  そこで**項目ごとに期待する単位**を決め、`claim_inventory.normalize_value`
  に通らない値は捨てる。単位が合わない値はそもそも採らない。

★一致の数え方★
  - 同じ運営元は1票（`source-registry.json` の系列で判定）
  - 2件が一致 → 採用
  - 2件が食い違う → **採らずに「第三の出典が要る」として返す**
  - 片方しか取れない → 採らない

使い方:
    python scripts/spec_lookup.py --name "Lすーぱぁびん娘" \\
        --url https://www.p-world.co.jp/machine/database/10496 \\
        --url https://p-town.dmm.com/machines/5038
    python scripts/spec_lookup.py --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import claim_inventory as _ci         # noqa: E402
import adoption_basis as _ab        # noqa: E402
import html_tables as _ht             # noqa: E402
import model_code_lookup as _mc       # noqa: E402
import new_machine_watch as _w        # noqa: E402
import fetched_page as _fp
import user_area as _ua              # noqa: E402
import safe_json as _sj               # noqa: E402

# 取りに行く項目。★項目ごとに期待する単位を決める★
#   単位が合わない値は捨てる（見出しの近くの別の値を拾う事故を防ぐ）
FIELDS = {
    # --- 設定ごとの表（P-WORLDは持っているが、DMMは範囲でしか持っていない）
    # ★per_setting は表の「列見出し」で対応づけて読む★（2026-08-03・Codex59回目）
    #   columns がその列見出しの許可リスト。行の走査（旧 per_setting_values）は
    #   同じ表の同単位2列（P-WORLD「設定|CZ合成|AT初当り確率」＝実在）を
    #   区別できず、CZ合成をAT確率として採れたので廃止。
    "at_prob":      {"columns": ("AT初当り確率", "AT初当たり確率", "AT確率",
                                 "AT"),
                     "unit": "1/x", "kind": "per_setting",
                     "jp": "AT初当たり確率"},
    "payout_rate":  {"columns": ("出玉率", "機械割"),
                     "unit": "%", "kind": "per_setting", "jp": "出玉率"},
    # --- 1つの値（★両サイトが同じ形で持っているのはこちら★）
    #   実データで確認: P-WORLD「97.3% ~ 112.5%」／DMM「97.3% 〜 112.5%」
    #   波ダッシュの字が違うので、比べる前に形をそろえる。
    "payout_range": {"labels": ("機械割",), "kind": "range", "jp": "機械割の範囲"},
    "model_code":   {"labels": ("型式名",), "kind": "text", "jp": "型式名"},
    # 50枚あたりのゲーム数（両サイトにある・実データで確認）
    #   P-WORLD「50枚あたりのゲーム数 約31G」／ちょんぼりすた「回転数/50枚 → 約31G」
    "games_per_50": {"labels": ("50枚あたりのゲーム数", "回転数/50枚", "50枚あたり"),
                     "kind": "games", "jp": "50枚あたりのゲーム数"},
    # ★条件（どのモードか）を書かないと載せられない項目★
    #   収集器はまだ条件を取れないので、集めても採用はされず保留になる。
    "net_increase": {"labels": ("純増",), "kind": "text", "jp": "純増"},
    # ★★ボーナス確率（設定 × BIG/REG/合算）★★（2026-08-26）
    #   ★AT を持たない機種（ジャグラー等）の「その機種らしさ」★＝
    #   判定書v2が BONUS 型の掲載条件として要求する唯一の値。
    #   ★2次元なので専用の kind★（per_setting は1列しか読めない）。
    "bonus_prob":   {"kind": "per_setting_matrix", "unit": "1/x",
                     "jp": "ボーナス確率"},
}
_SETTING_KEY = re.compile(r"[1-6]")
# ★★設定 × 列 の表（ボーナス確率）★★（2026-08-26・Codex31回目の設計）
#   ★見出しの日本語を内部の鍵にしない★＝出典ごとに書き方が違うので、
#   別名を吸収して `big` / `reg` / `total` に寄せる。
#   ★合算はこちらで計算しない★＝出典に書いてある時だけ採る（数値を作らない）。
#   BIG・REGの表示値は丸められているので、計算した合算は出典と一致しない。
BONUS_COLUMNS = {
    "big":   ("BIG", "BB", "ビッグ", "BIGボーナス", "BB確率", "BIG確率"),
    "reg":   ("REG", "RB", "レギュラー", "REGボーナス", "RB確率", "REG確率"),
    "total": ("合算", "合成", "ボーナス合算", "ボーナス合成", "合算確率",
              "合成確率"),
}
# ★読者に出すときの呼び方★（内部の鍵とは分ける）
BONUS_COLUMN_LABELS = {"big": "BIG", "reg": "REG", "total": "合算"}
# ★★どの列が無いと採らないか★★（抽出器に暗黙で埋めない・Codexの助言）
BONUS_REQUIRED = ("big", "reg")


class BonusShapeError(ValueError):
    """ボーナス確率の形が契約に合わない。★黙って読み飛ばさない★"""


def validate_bonus_prob_value(value) -> None:
    """★bonus_prob の値の形を確かめる唯一の場所★

    ★3か所から呼ぶ★＝収集器 / confirmed_values / page_decision。
    ★静かに「claimなし」に落とさない★（Codex31回目）＝
    形が壊れた値を黙って読み飛ばすと、
    **単独確認の壊れた値**などが誰にも気づかれずに素通りする。
    """
    if not isinstance(value, dict) or not value:
        raise BonusShapeError("ボーナス確率が空でない辞書ではありません")
    for st, cols in value.items():
        if not _SETTING_KEY.fullmatch(str(st)):
            raise BonusShapeError(f"設定の書き方が違います: {st!r}")
        if not isinstance(cols, dict) or not cols:
            raise BonusShapeError(f"設定{st}の中身が空でない辞書ではありません")
        for ck, cv in cols.items():
            if ck not in BONUS_COLUMNS:
                raise BonusShapeError(
                    f"設定{st}に知らない列があります: {ck!r}"
                    f"（{sorted(BONUS_COLUMNS)} のどれか）")
            if _ci.normalize_value(cv, "1/x") is None:
                raise BonusShapeError(
                    f"設定{st}の{ck}が確率の形ではありません: {cv!r}")
        missing = [c for c in BONUS_REQUIRED if c not in cols]
        if missing:
            raise BonusShapeError(
                f"設定{st}に{'・'.join(missing)}がありません")
    # ★★合算は「全部あるか、全部無いか」★★（2026-08-26・Codex32回目のP1）
    #   ★混ざると、記事が「1設定でも合算があれば列を出し、欠けたセルは未確認」に
    #     なり、「合算が採れていない機種は列ごと出さない」と食い違う★。
    _has = [("total" in c) for c in value.values()]
    if any(_has) and not all(_has):
        _nan = [st for st, c in value.items() if "total" not in c]
        raise BonusShapeError(
            "合算がある設定と無い設定が混ざっています"
            f"（無い設定: {'・'.join(sorted(_nan))}）"
            "／★全部あるか、全部無いかにしてください★")


def bonus_matrix_from_tables(html: str) -> tuple:
    """設定ごとの BIG / REG / 合算 を、列見出しで対応づけて読む。

    ★`per_setting_from_tables` と同じ作り★＝表単位＋列見出しの対応。
    行の走査はしない（同単位の別の列を取り違えるため）。
    """
    alias = {}
    for key, names in BONUS_COLUMNS.items():
        for n in names:
            alias[n] = key
    cands = []
    for tb in _ht.tables(html):
        # ★見出しとデータ行の取り出しは1か所★（per_setting と同じ関数）
        st = setting_table(tb)
        if st is None:
            continue
        header, body = st
        cols = {i: alias[h] for i, h in enumerate(header)
                if i >= 1 and h in alias}
        # ★★同じ内部列が2つある表は採らない★★（2026-08-26・Codex32回目のP1）
        #   例＝同じ表に「BIG」と「BB」＝どちらも big になり、
        #   ★後のセルが黙って上書き★していた（どちらが正しいか決められない）。
        if len(set(cols.values())) != len(cols):
            continue
        # ★必須の列は行ごとに見る★（2026-08-26）
        #   ★見出しでも見る二重の検査にしていた★ので、片方を消しても
        #   もう片方が拾い、壊し方の試験で「守られていない」と出た。
        #   ＝どちらか1つにする（行ごとの検査だけで結果は同じ）。
        got = {}
        for _st_key, r in setting_rows(header, body):
            cell = {}
            for ci, key in cols.items():
                if len(r) <= ci:
                    continue
                v = " ".join(str(r[ci]).split())
                if _ci.normalize_value(v, "1/x") is not None:
                    cell[key] = v
            if all(c in cell for c in BONUS_REQUIRED):
                # ★★同じ設定が2行あって値が違えば食い違い★★
                #   （2026-08-26・Codex32回目のP1。
                #     ★直す前は setdefault で最初の行だけ黙って残していた★）
                _st = _st_key
                if _st in got and got[_st] != cell:
                    return {}, True
                got.setdefault(_st, cell)
        if got:
            cands.append(got)
    # ★同じページの中で食い違っていたら採らない★（per_setting と同じ扱い）
    merged, conflict = {}, False
    for got in cands:
        for st, cell in got.items():
            if st in merged and merged[st] != cell:
                conflict = True
            merged.setdefault(st, cell)
    if conflict:
        return {}, True
    if merged:
        try:
            validate_bonus_prob_value(merged)
        except BonusShapeError:
            return {}, False        # ★契約に合わないものは採らない★
    return merged, False



# 範囲の書き方をそろえる（「97.3% ~ 112.5%」も「97.3% 〜 112.5%」も同じ）
_RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*[~〜～\-–—]\s*(\d+(?:\.\d+)?)\s*%")


def normalize_range(raw: str):
    """『97.3% 〜 112.5%』を比べられる形にする。読めなければ None。"""
    m = _RANGE_RE.search(unicodedata.normalize("NFKC", str(raw or "")))
    if not m:
        return None
    lo, hi = float(m.group(1)), float(m.group(2))
    if not (50 <= lo <= hi <= 200):
        return None          # 出玉率としてありえない値は採らない
    return {"low": lo, "high": hi, "unit": "%"}


_GAMES_RE = re.compile(r"約?\s*(\d{1,3}(?:\.\d)?)\s*G")


def normalize_games(raw: str):
    """『約31G』を比べられる形にする。ありえない値は採らない。"""
    m = _GAMES_RE.search(unicodedata.normalize("NFKC", str(raw or "")))
    if not m:
        return None
    v = float(m.group(1))
    if not (5 <= v <= 100):
        return None          # 50枚で5G未満・100G超はありえない
    return {"games": v, "unit": "G"}


def single_value(lines: list, labels: tuple, kind: str):
    """『見出し → 値』の1つ組を読む。見出し行に値が続く形にも対応。"""
    seps = "：:  　"
    for i, line in enumerate(lines):
        lab = next((x for x in labels if line.startswith(x)), None)
        if lab is None:
            continue
        # 見出しの直後は区切りか行末でなければならない（別の語の一部を拾わない）
        rest = line[len(lab):]
        # ★見出しの直後が区切り・空白・行末のいずれか★
        #   `&nbsp;` をほどくと「50枚あたりのゲーム数 約31G」のように
        #   空白1つで値が続く形になる（P-WORLDがこの形）。
        if rest and rest[0] not in seps and not rest[0].isspace():
            continue
        cand = rest.lstrip(seps).strip()
        if not cand and i + 1 < len(lines):
            cand = lines[i + 1].strip()      # 次の行に値がある形（P-WORLD）
        if not cand:
            continue
        if kind == "range":
            v = normalize_range(cand)
            if v:
                return v
        elif kind == "games":
            v = normalize_games(cand)
            if v:
                return v
        elif kind == "text":
            v = unicodedata.normalize("NFKC", cand)
            if _mc._CODE_OK.match(v) and v not in _mc._CODE_NG:
                return v
    return None


# ★出典から値を採るときのルール★（assets/data/collection-rules.json）
#   Codexとのやり取りで出た指摘のうち、他の機種でも効くものを外部ファイルに置く。
#   ★手順書（文章）ではなくここで効かせる理由★
#     文章のルールはAIが読み飛ばせば終わり。コードが読めば必ず効く。
RULES_PATH = os.path.join(BASE, "assets", "data", "collection-rules.json")


def load_rules() -> dict:
    """採取ルールを読む。★読めなければ止める★（ルール無しで採らない）"""
    try:
        return _sj.read_json(RULES_PATH, expect=dict)
    except Exception as e:
        raise RuntimeError(f"採取ルールが読めません: {e} → 値を採りません")


def phrasing_equal(a: str, b: str, rules: dict | None = None) -> bool:
    """2つの書き方を『同じ値』と数えてよいか。

    ★書き方が違うものを一致と数えない★
      「50%以上」と「約50%」は、下限を示す表現と概数で意味の幅が違う。
      文字が違えば元々一致しないが、**将来ゆるい比較を入れたときの歯止め**として
      ここに明示しておく（実際に2026-07-31 に指摘された組合せ）。
    """
    rules = rules or load_rules()
    if a == b:
        return True
    for ex in (rules.get("phrasing_not_equal") or {}).get("examples") or []:
        if {a, b} == {ex.get("a"), ex.get("b")}:
            return False
    return False


def needs_conditions(field_key: str, rules: dict | None = None):
    """その項目は条件を書かないと載せられないか。要るなら何を書くか返す。"""
    rules = rules or load_rules()
    r = (rules.get("conditions_required") or {}).get(field_key)
    return (r or {}).get("must_state") or []


def settings_may_be_non_contiguous(rules: dict | None = None) -> bool:
    """設定が1〜6の連番だと決めつけてよいか。★決めつけない★"""
    rules = rules or load_rules()
    return bool((rules.get("settings_layout") or {}).get("non_contiguous_allowed"))


_SETTING_RE = re.compile(r"^設定\s*([1-6])$")
# ★数字でない設定★（設定L・設定V など）。
#   過去に「設定3なし」と誤記した事故があり、**設定の段数を取り違えると誤情報**になる。
#   値が採れなくても「そういう設定がある」ことは掴んでおき、黙って落とさない。
_SETTING_ANY_RE = re.compile("^設定" + chr(92) + "s*([0-9A-Za-z]{1,2})$")


def setting_labels(lines: list) -> list:
    """表に出てくる設定の名前をすべて拾う（値が採れるかは問わない）。"""
    out = []
    for line in lines:
        m = _SETTING_ANY_RE.match(str(line).strip())
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


def _lines(html: str) -> list:
    return [x.strip() for x in _w._visible_text(html).splitlines()]


# ★★「設定」で始まる表の、見出しとデータ行を取り出す唯一の場所★★
#   （2026-08-26・Codex33回目の設計）
#
#   ★なぜ要るか★＝DMMの設定表は**1行目が題**（1セルの colspan）で、
#   本当の見出しは2行目にある。実測（ファンキージャグラー2）＝
#     rows[0] = ['打ち方ごとの機械割']
#     rows[1] = ['設定', '市場調査値', 'チェリー狙い', 'フル攻略']
#     rows[2] = ['1', '97.0%', ...]        ←★「設定1」ではなく「1」★
#   いままでは `has_span` の表を丸ごと捨てていたので、
#   ★DMMからは設定ごとの値を1つも採れていなかった★（実測で確認）。
#
#   ★★列数だけで判断しない★★（Codexの指摘）＝
#   `has_span` は真偽しか持っていなかったので、
#   「題の行にしかspanが無い」ことを**証明できなかった**。
#   生のセル数がそろっていても、データ行に colspan=2 があれば
#   画面上は1列多く、以後の対応づけが1列ずれる。
#   → `html_tables` に span の位置を残し、ここで**唯一のspanが題セル**か見る。
_SETTING_CELL = re.compile(r"^(?:設定)?\s*([1-6])$")


def setting_table(tb: dict):
    """(見出し, データ行) を返す。設定の表でなければ None。"""
    rows = [r for r in (tb.get("rows") or [])]
    if len(rows) < 2:
        return None
    spans = tb.get("spans") or []
    if not tb.get("has_span"):
        head, body = rows[0], rows[1:]
    else:
        # ★題の行つきの形に、ぴったり合うときだけ通す★
        first = [c for c in (rows[0] or []) if str(c).strip()]
        if len(first) != 1 or len(rows) < 3:
            return None
        if len(spans) != 1:
            return None
        sp = spans[0]
        if sp.get("row") != 0 or sp.get("col") != 0:
            return None
        if sp.get("rowspan") != 1:
            return None
        head, body = rows[1], rows[2:]
        if sp.get("colspan") != len(head):
            return None
        for r in body:
            if not r or not any(str(c).strip() for c in r):
                continue
            if len(r) != len(head):
                return None
    head = [" ".join(str(c).split()) for c in (head or [])]
    if not head or head[0] != "設定":
        return None
    return head, body


def setting_rows(head: list, body: list):
    """データ行を (設定の番号, 行) で返す。★この表の中だけ数字を許す★

    ★見出しの先頭が「設定」であることを確かめてから★数字を認める。
    「1位」「01」「1個」は完全一致しないので落ちる。
    """
    for r in body:
        if not r:
            continue
        m = _SETTING_CELL.match(" ".join(str(r[0]).split()))
        if m:
            yield m.group(1), r


def per_setting_from_tables(html: str, columns: tuple, unit: str) -> dict:
    """設定ごとの値を、表の「列見出し」で対応づけて読む。

    ★行の走査をやめた理由★（2026-08-03・Codex59回目）
      P-WORLDの実在表「設定|CZ合成|AT初当り確率」は同単位の2列が並ぶ。
      「設定行の後で最初に単位が合う値」を採る旧方式では、
      CZ合成の確率をAT初当り確率として採れた（列を区別できない）。
      また見出しから80行の走査は、間に挟まる別の表（CZ確率）まで
      読めた。表単位＋列見出しの対応なら、どちらも起きない。

    実在の形（2026-08-03・実ページで確認）:
      P-WORLD       設定|CZ合成|AT初当り確率
      ちょんぼりすた 設定|AT|出玉率
    """
    cands: list = []
    for tb in _ht.tables(html):
        # ★見出しとデータ行の取り出しは1か所★（2026-08-26・Codex33回目）
        #   ★題の行つきの表（DMM）もここで扱う★
        st = setting_table(tb)
        if st is None:
            continue
        header, body = st
        for ci in range(1, len(header)):
            if header[ci] not in columns:
                continue
            got: dict = {}
            for key, r in setting_rows(header, body):
                if len(r) <= ci:
                    continue
                v = " ".join(str(r[ci]).split())
                if _ci.normalize_value(v, unit) is None:
                    continue
                # ★★同じ設定が2行あって値が違えば食い違い★★
                #   （2026-08-26・Codex33回目。★bonus 側だけ直していた★＝
                #     こちらは setdefault で後の行を黙って捨てていた）
                if key in got and got[key] != v:
                    return {}, True
                got.setdefault(key, v)
            if got:
                cands.append(got)
    # ★同じページの別の表が同じ設定に別の値を出していたら食い違い★
    #   （2026-08-03・Codex60回目。最大の表だけ残すと、更新途中などで
    #     片方だけ値が変わったページ内の反対情報が compare() に届かない）
    merged: dict = {}
    conflict = False
    for got in cands:
        for k, v in got.items():
            if k in merged and merged[k] != v:
                conflict = True
            merged.setdefault(k, v)
    best = max(cands, key=len, default={})
    return best, conflict


def read_page(url: str, official_name: str, *,
              expected_maker: str = "", grant=None, page=None,
              dmm_identity: dict | None = None) -> dict:
    """名鑑1件ぶんを読む。★機種が違えば何も採らない★

    ★dmm_identity を渡すと、DMMの機種ページは DMM自身の決まりで確かめる★
      （2026-08-22・台帳#453）。渡さなければ今までどおり汎用の題検査。
      ★束の中身の検査は `material_page_identity_ok` に任せる★
      （同じ規則を4か所に写さない）。
    """
    out = {"url": url, "host": url.split("/")[2].lower().removeprefix("www."),
           "ok": False, "reason": "", "fields": {}}
    try:
        # ★用途を名乗ってから取りに行く★（2026-08-16・依頼218）
        # ★★取り直さない★★（2026-08-17・台帳#393／Codex依頼237の診断）
        #   ★穴の根本★＝ここで各読取器が自分で取り直していたので、
        #   「検証した本文」と「実際に読む本文」が同じであることを
        #   誰も保証していなかった（同じ型の穴が5回続いた原因）。
        #   渡されたら**その本文をそのまま読む**。
        #   渡されないときだけ自分で取る（投稿欄を落とすのも器の中でやる）。
        if page is None:
            page = _fp.fetch(url, "claim_material")
        html = page.cleaned_html
    except Exception as e:
        out["reason"] = f"取得できません: {e}"
        return out
    # ★材料の照合も厳格側で★（2026-08-02・Codex55回目。緩い側だと
    #   「機種名 新台 BLACK」のような未知の版名が装飾語の後ろで通り、
    #   別バージョンの値を2媒体一致で採用できた）
    # ★材料に使ってよいかは共通の関所で見る★（2026-08-17・台帳#390）
    #   ここに例外の扱いを写さない（4か所に写すと必ずずれる）
    ok, why = _mc.material_page_identity_ok(
        page, official_name, url=url,
        expected_maker=expected_maker, grant=grant,
        dmm_identity=dmm_identity)
    if not ok:
        out["reason"] = why
        return out
    lines = _lines(html)
    for key, spec in FIELDS.items():
        if spec["kind"] == "per_setting_matrix":
            v, conflict = bonus_matrix_from_tables(html)
            if conflict:
                out["fields"] = {}
                out["reason"] = (f"同じページの中で{spec['jp']}が"
                                 "食い違っています（要確認）")
                return out
        elif spec["kind"] == "per_setting":
            v, conflict = per_setting_from_tables(html, spec["columns"],
                                                  spec["unit"])
            if conflict:
                # ★ページ内の反対情報を握りつぶさない★（Codex60回目）
                out["fields"] = {}
                out["reason"] = (f"同じページの中で{spec['jp']}の"
                                 "設定値が食い違っています（要確認）")
                return out
        else:
            v = single_value(lines, spec["labels"], spec["kind"])
        if v:
            out["fields"][key] = v
    out["setting_labels"] = setting_labels(lines)
    out["ok"] = True
    out["reason"] = "OK"
    return out


def _lineage(host: str) -> str:
    """同じ運営元・同じ転載系列を1票にまとめるための鍵。"""
    try:
        reg = _sj.read_json(os.path.join(BASE, "assets", "data",
                                         "source-registry.json"), expect=dict)
    except Exception:
        return host
    for pid, pub in (reg.get("publishers") or {}).items():
        for h in (pub.get("canonical_hosts") or []):
            if h.lower().removeprefix("www.") == host:
                return pub.get("content_lineage_id") or pid
    return host          # 未登録は他と束ねない（＝1票として扱う）


def _indep(keys) -> int:
    """★独立した票の数（共同制作の組はまとめる）★＝数える場所は1つ"""
    import source_lineage as _sl2
    return _sl2.independent(keys)


def vote_lineage(host: str) -> str:
    """★票を数えるときの系列★ 登録されていないサイトは空を返す。

    ★なぜ分けたか（2026-08-09・依頼127）★
      `_lineage()` は未登録のホストをそのまま返すので、**知らないサイトが
      1票として数えられて**いた。source-registry の方針は
      「ここに無いホストは票に数えない（default deny）」なので逆だった。
      いまは名鑑（すべて登録済み）からしか来ないので実害は出ていないが、
      出所が増えた瞬間に「知らないサイト2つが一致したから採用」が成立する。

      `_lineage()` 自体は転載検知（lineage_check）が使っているので触らず、
      **票を数える側だけ**をこちらに寄せる。
    """
    import source_lineage as _sl2
    try:
        return _sl2.vote_key_of_url("https://" + str(host or "").lstrip("/"))
    except _sl2.LineageError:
        # ★登録されていないサイトだけを「票にしない」★
        return ""
    # ★登録簿そのものが読めない等は握りつぶさない★（2026-08-09・依頼129）
    #   全部を「未登録」に倒すと、材料が一斉に0になっても
    #   「材料不足」に見えて原因が分からなくなる。例外はそのまま上げる。


def compare(pages: list, ctx: dict | None = None) -> dict:
    """★2件が一致したものだけ採る★ 食い違いは『第三の出典が要る』として返す。

    ★採取ルールを必ず読む★（読めなければ例外で止まる＝ルール無しで採らない）

    ★★採否は adoption_basis が決める★★（2026-08-23）
      ★ctx を渡さなければ今までとまったく同じ判定★＝独立2票のみ採用。
    """
    _ctx = dict(ctx or {})
    rules = load_rules()
    adopted: dict = {}
    need_third: dict = {}
    thin: dict = {}
    usable = [p for p in pages if p["ok"] and p["fields"]]
    # ★出典に出てくる設定の名前をすべて集める★
    #   値が採れた設定より多ければ、**段数を取り違えている恐れ**があるので知らせる。
    #   （過去に「設定3なし」と誤記した事故と同じ型）
    seen_labels: list = []
    for p in usable:
        for lb in (p.get("setting_labels") or []):
            if lb not in seen_labels:
                seen_labels.append(lb)
    for key in FIELDS:
        votes: dict = {}
        for p in usable:
            v = p["fields"].get(key)
            if not v:
                continue
            fp = json.dumps(v, ensure_ascii=False, sort_keys=True)
            lin = vote_lineage(p["host"])
            if not lin:      # ★登録されていないサイトは票に数えない★
                continue
            votes.setdefault(fp, set()).add(lin)
        if not votes:
            continue
        # ★票の数は source_lineage が決める★（2026-08-14・依頼192のP1）
        #   共同制作の組（一撃×DMM）を独立2票と数えないため。
        # ★採ってよいかは adoption_basis が決める★（2026-08-23）
        _rival = len(votes) > 1      # ★別の値を出している出典がある★
        _sups = {fp: _ab.classify_support(
            s, {**_ctx, "rival_values": _rival}) for fp, s in votes.items()}
        agreed = [(fp, s) for fp, s in votes.items()
                  if _sups[fp]["accepted"]]
        # ★反対票が1票でもあれば採らない★（2026-08-02・Codex56回目。
        #   「97.8% 2票＋99.9% 1票」を97.8%で採用し、不一致を報告にも
        #   残していなかった。値が割れている間は保留＝人・翌日へ）
        if len(agreed) == 1 and len(votes) == 1:
            must = needs_conditions(key, rules)
            if must:
                # ★条件を書かないと載せられない項目★（純増・継続率・天井など）
                #   いまの収集器は条件を取れないので、採用せず保留にする。
                need_third[key] = {
                    "why": "条件を書かないと載せられない項目です: " + " / ".join(must),
                    "value": json.loads(agreed[0][0])}
                continue
            adopted[key] = {"value": json.loads(agreed[0][0]),
                            "sources": sorted(agreed[0][1]),
                            # ★どんな根拠で採ったかを必ず残す★
                            #   （2026-08-23・Codexの指摘P0）
                            #   ★保存し忘れると検索の濃さに数えられてしまう★＝
                            #   ここが抜けていたので、DMM単独の機械割・コイン持ちが
                            #   普通のclaimとして数えられていた。
                            "basis": _sups[agreed[0][0]]["basis"]}
        elif len(votes) > 1:
            need_third[key] = {fp[:200]: sorted(s) for fp, s in votes.items()}
        else:
            thin[key] = {"why": "1つの出典しか取れていません",
                         "sources": sorted(next(iter(votes.values())))}
    got_labels = set()
    for key, spec in FIELDS.items():
        if (spec["kind"] in ("per_setting", "per_setting_matrix")
                and key in adopted):
            got_labels |= set(adopted[key]["value"])
    unconfirmed = [x for x in seen_labels if x not in got_labels]
    return {"adopted": adopted, "need_third": need_third, "thin": thin,
            "setting_labels_seen": seen_labels,
            "setting_labels_unconfirmed": unconfirmed}


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []
    nl = chr(10)

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    # ★実在の2形（P-WORLD 3列・ちょんぼりすた 3列）を列見出しで読む★
    HP = ("<h3>CZ/AT確率</h3><table>"
          "<tr><th>設定</th><th>CZ合成</th><th>AT初当り確率</th></tr>"
          "<tr><td>設定1</td><td>1/395.7</td><td>1/498.7</td></tr>"
          "<tr><td>設定2</td><td>1/394.8</td><td>1/477.8</td></tr>"
          "<tr><td>設定L</td><td>調査中</td><td>調査中</td></tr></table>")
    t("★★同じ表の同単位2列（CZ合成|AT初当り確率）を列見出しで区別する★★"
      "（行の走査ではCZ合成をAT確率として採れた・P-WORLD実在形・Codex59回目）",
      per_setting_from_tables(HP, ("AT初当り確率",), "1/x")[0]
      == {"1": "1/498.7", "2": "1/477.8"}
      and per_setting_from_tables(HP, ("CZ合成",), "1/x")[0]
      == {"1": "1/395.7", "2": "1/394.8"})
    HC = ("<h3>AT確率・機械割</h3><table>"
          "<tr><th>設定</th><th>AT</th><th>出玉率</th></tr>"
          "<tr><td>設定1</td><td>1/498.7</td><td>97.8%</td></tr>"
          "<tr><td>設定2</td><td>1/477.8</td><td>98.5%</td></tr></table>")
    t("★項目ごとに正しい列を読む★（ちょんぼりすた実在形）",
      per_setting_from_tables(HC, ("AT",), "1/x")[0]
      == {"1": "1/498.7", "2": "1/477.8"}
      and per_setting_from_tables(HC, ("出玉率",), "%")[0]
      == {"1": "97.8%", "2": "98.5%"})
    t("★★単位が合わない値は採らない★★"
      "（出玉率の欄に確率を拾った実際の事故）",
      per_setting_from_tables(
          "<table><tr><th>設定</th><th>出玉率</th></tr>"
          "<tr><td>設定1</td><td>1/498.7</td></tr></table>",
          ("出玉率",), "%")[0] == {})
    t("　設定の行が無ければ何も採らない",
      per_setting_from_tables(
          "<table><tr><th>設定</th><th>AT</th></tr>"
          "<tr><td>備考</td><td>なし</td></tr></table>", ("AT",), "1/x")[0] == {})
    t("★★非表示の設定表を採らない★★（Codex63回目）",
      per_setting_from_tables(
          '<div hidden>' + HP + "</div>", ("AT初当り確率",), "1/x")[0] == {}
      and per_setting_from_tables(
          HP + '<div style="display:none">'
          + HP.replace("1/498.7", "1/999.9") + "</div>",
          ("AT初当り確率",), "1/x")
      == ({"1": "1/498.7", "2": "1/477.8"}, False))
    t("★★同じページの重複表の食い違いを見逃さない★★（Codex60回目）",
      per_setting_from_tables(
          HP + HP.replace("1/498.7", "1/999.9"),
          ("AT初当り確率",), "1/x")[1] is True
      and per_setting_from_tables(HP + HP, ("AT初当り確率",), "1/x")[1] is False)
    t("★★多段見出し（rowspan/colspan）の表は不採用★★（列がずれる・Codex60回目）",
      per_setting_from_tables(
          '<table><tr><th rowspan="2">設定</th><th colspan="2">AT</th></tr>'
          "<tr><th>CZ合成</th><th>AT初当り</th></tr>"
          "<tr><td>設定1</td><td>1/395.7</td><td>1/498.7</td></tr></table>",
          ("AT",), "1/x")[0] == {})
    t("★★見出しの後の別の表（CZ確率）まで走査しない★★（Codex59回目）",
      per_setting_from_tables(
          "<h3>AT確率</h3><p>調査中</p><h3>CZ確率</h3>"
          "<table><tr><th>設定</th><th>CZ確率</th></tr>"
          "<tr><td>設定1</td><td>1/395.7</td></tr></table>",
          ("AT初当り確率", "AT確率", "AT"), "1/x")[0] == {})

    t("★★波ダッシュの字が違っても同じ範囲として扱う★★（実データの差）",
      normalize_range("97.3% ~ 112.5%") == normalize_range("97.3% 〜 112.5%")
      == {"low": 97.3, "high": 112.5, "unit": "%"})
    t("★出玉率としてありえない値は採らない★",
      normalize_range("5% 〜 900%") is None and normalize_range("112.5% 〜 97.3%") is None)
    t("　範囲として読めなければ採らない", normalize_range("約2.8枚") is None)
    t("★見出しの行に値が続く形も、次の行にある形も読む★",
      single_value(["機械割  :", "97.3% ~ 112.5%"], ("機械割",), "range")
      == single_value(["機械割：97.3% 〜 112.5%"], ("機械割",), "range"))
    t("　型式名は許可した形だけ採る（説明文を拾わない）",
      single_value(["型式名", "Lびん娘NY1"], ("型式名",), "text") == "Lびん娘NY1"
      and single_value(["型式名", "記載なし"], ("型式名",), "text") is None)

    t("★50枚あたりのゲーム数を読む（両サイトの書き方の差を吸収）★",
      normalize_games("約31G") == normalize_games("31G") == {"games": 31.0, "unit": "G"})
    t("　ありえない値は採らない",
      normalize_games("約3G") is None and normalize_games("約300G") is None)
    t("　G数として読めなければ採らない", normalize_games("約2.8枚") is None)

    A = {"url": "https://nana-press.com/x", "host": "nana-press.com", "ok": True,
         "reason": "OK", "fields": {"payout_rate": {"1": "97.8%"}}}
    B = {"url": "https://p-town.dmm.com/y", "host": "p-town.dmm.com", "ok": True,
         "reason": "OK", "fields": {"payout_rate": {"1": "97.8%"}}}
    C = {"url": "https://p-town.dmm.com/z", "host": "p-town.dmm.com", "ok": True,
         "reason": "OK", "fields": {"payout_rate": {"1": "99.9%"}}}
    r = compare([A, B])
    t("★★2件が一致したら採る★★",
      r["adopted"].get("payout_rate", {}).get("value") == {"1": "97.8%"})
    r2 = compare([A, C])
    t("★★食い違ったら採らず『第三の出典が要る』と返す★★",
      "payout_rate" in r2["need_third"] and not r2["adopted"])
    r3 = compare([A])
    t("　1件だけなら採らない", not r3["adopted"] and "payout_rate" in r3["thin"])
    D = {"url": "https://chonborista.com/w", "host": "chonborista.com", "ok": True,
         "reason": "OK", "fields": {"payout_rate": {"1": "99.9%"}}}
    r23 = compare([A, B, D])
    t("★★2票一致でも反対票が1票あれば採らない★★"
      "（97.8%×2＋99.9%×1を採用し不一致を報告にも残さなかった・Codex56回目）",
      not r23["adopted"] and "payout_rate" in r23["need_third"])
    B2 = {**B, "url": "https://nana-press.com/y", "host": "nana-press.com"}
    r4 = compare([A, B2])
    t("★同じ運営元の2ページを2票と数えない★", not r4["adopted"])
    r5 = compare([{**A, "ok": False, "fields": {}}, B])
    t("　機種が違うページの内容は混ぜない", not r5["adopted"])

    # -------- 採取ルール（assets/data/collection-rules.json）が実際に効くか
    R = load_rules()
    t("★★ルールが読めなければ値を採らない★★（ルール無しで採らない）",
      isinstance(R, dict) and R.get("schema_version", "").startswith("collection-rules/"))
    t("★★『50%以上』と『約50%』を一致と数えない★★"
      "（下限を示す表現と概数は別物・2026-07-31 Codex指摘）",
      not phrasing_equal("50%以上", "約50%", R)
      and not phrasing_equal("82%以上", "約82%", R))
    t("　同じ書き方どうしは一致とする", phrasing_equal("97.3%", "97.3%", R))
    t("★★条件が要る項目は、2出典一致でも採用しない★★"
      "（純増はどのモードか書かないと誤情報・2026-07-31 Codex指摘）",
      needs_conditions("net_increase", R)
      and needs_conditions("at_continuation_rate", R)
      and needs_conditions("ceiling", R))
    t("　条件の要らない項目は空を返す", needs_conditions("payout_rate", R) == [])

    t("★★数字でない設定（設定L・設定V）も名前として拾う★★",
      setting_labels(["設定", "設定1", "1/1", "設定L", "調査中"]) == ["1", "L"])
    t("　設定判別・設定L搭載機などの文は設定名にしない",
      setting_labels(["設定判別", "設定L搭載機", "設定6以上"]) == [])
    PW = {"url": "https://nana-press.com/x", "host": "nana-press.com", "ok": True,
          "reason": "OK", "setting_labels": ["1", "6", "L"],
          "fields": {"payout_rate": {"1": "97.8%", "6": "112.5%"}}}
    CB = {**PW, "url": "https://chonborista.com/y", "host": "chonborista.com",
          "setting_labels": ["1", "6"]}
    rr = compare([PW, CB])
    t("★★値が採れなかった設定を黙って落とさない★★（設定Lを見落とすと段数を誤る）",
      rr["setting_labels_unconfirmed"] == ["L"])
    t("　値が採れた設定は未確認に入れない",
      set(rr["setting_labels_seen"]) == {"1", "6", "L"}
      and "1" not in rr["setting_labels_unconfirmed"])
    t("★設定が1〜6の連番だと決めつけない★"
      "（L/1/2/4/5/6 のように飛ぶ機種がある）",
      settings_may_be_non_contiguous(R) is True)

    # ★実際に compare を通したときに効くか★（宣言だけで終わらせない）
    P = {"url": "https://nana-press.com/x", "host": "nana-press.com", "ok": True,
         "reason": "OK", "fields": {"net_increase": "約2.8枚"}}
    Q = {"url": "https://chonborista.com/y", "host": "chonborista.com", "ok": True,
         "reason": "OK", "fields": {"net_increase": "約2.8枚"}}
    _r = compare([P, Q])
    t("★★条件が要る項目は compare でも止まる★★（宣言だけで終わっていない）",
      "net_increase" not in _r["adopted"] and "net_increase" in _r["need_third"])


    # ─── ★ボーナス確率（設定 × BIG/REG/合算）★（2026-08-26）──────
    #   ★実物に近い形で試す★＝手作りの辞書を採点しない（罠①）
    _BON = ("<html><body><table>"
            "<tr><th>設定</th><th>BIG</th><th>REG</th><th>合算</th></tr>"
            "<tr><td>設定1</td><td>1/273.1</td><td>1/439.8</td>"
            "<td>1/168.5</td></tr>"
            "<tr><td>設定2</td><td>1/270.8</td><td>1/399.6</td>"
            "<td>1/161.0</td></tr>"
            "<tr><td>設定6</td><td>1/240.1</td><td>1/240.1</td>"
            "<td>1/120.0</td></tr>"
            "</table></body></html>")
    _bv, _bc = bonus_matrix_from_tables(_BON)
    t("★★設定×BIG/REG/合算の表を読める★★",
      not _bc and _bv.get("1") == {"big": "1/273.1", "reg": "1/439.8",
                                   "total": "1/168.5"})
    t("　連番でなくてもよい（設定1・2・6だけの表）",
      sorted(_bv) == ["1", "2", "6"])
    # ★見出しの別名を吸収する★（出典ごとに書き方が違う）
    _bv2, _ = bonus_matrix_from_tables(
        _BON.replace("<th>BIG</th>", "<th>BB</th>")
        .replace("<th>REG</th>", "<th>RB</th>")
        .replace("<th>合算</th>", "<th>合成</th>"))
    t("　BB/RB/合成 という書き方でも同じ形で読める", _bv2 == _bv)
    # ★合算が無くてもBIG/REGがあれば採る（★計算では埋めない★）
    _no_total = _BON.replace("<th>合算</th>", "<th>備考</th>")
    _bv3, _ = bonus_matrix_from_tables(_no_total)
    t("★★合算が無くても採る／★こちらでは計算しない★★",
      _bv3 and all("total" not in c for c in _bv3.values()))
    # ★必須の列が欠けたら採らない★
    _no_reg = _BON.replace("<th>REG</th>", "<th>備考</th>")
    t("★REGが無い表は採らない（必須の列）★",
      bonus_matrix_from_tables(_no_reg)[0] == {})
    # ★確率の形でない値は採らない★
    _bad = _BON.replace("<td>1/439.8</td>", "<td>約440回</td>", 1)
    t("　確率の形でないセルがある設定は落とす",
      "1" not in bonus_matrix_from_tables(_bad)[0])
    # ★同じページの中で食い違ったら採らない★
    _conf = _BON.replace("</table></body>",
                         "</table><table>"
                         "<tr><th>設定</th><th>BIG</th><th>REG</th></tr>"
                         "<tr><td>設定1</td><td>1/999.9</td>"
                         "<td>1/439.8</td></tr></table></body>")
    t("★★同じページの中で食い違ったら採らない★★",
      bonus_matrix_from_tables(_conf) == ({}, True))
    # ★多段見出し（rowspan/colspan）は列がずれるので採らない★
    _span = _BON.replace("<th>BIG</th>", '<th colspan="2">BIG</th>')
    t("　多段見出しの表は採らない（列がずれる）",
      bonus_matrix_from_tables(_span)[0] == {})
    # ★★同じ内部列が2つある表は採らない★★（2026-08-26・Codex32回目のP1）
    #   ★直す前は後のセルが黙って上書き★＝どちらが正しいか決められない。
    _dup_col = _BON.replace("<th>合算</th>", "<th>BB</th>")
    t("★★同じ表に BIG と BB があったら採らない（黙って上書きしない）★★",
      bonus_matrix_from_tables(_dup_col)[0] == {})
    # ★★同じ設定が2行あって値が違えば食い違い★★
    #   ★直す前は最初の行だけ黙って残していた★
    _dup_row = _BON.replace(
        "</table>",
        "<tr><td>設定1</td><td>1/999.9</td><td>1/439.8</td>"
        "<td>1/168.5</td></tr></table>")
    t("★★同じ設定が2行あって値が違えば採らない★★",
      bonus_matrix_from_tables(_dup_row) == ({}, True))
    _same_row = _BON.replace(
        "</table>",
        "<tr><td>設定1</td><td>1/273.1</td><td>1/439.8</td>"
        "<td>1/168.5</td></tr></table>")
    t("　同じ値の重複行なら止めない（食い違いではない）",
      bonus_matrix_from_tables(_same_row)[0].get("1"))

    # --- 保存契約（★3か所から呼ぶ唯一の検査★）
    def _shape_ng(v):
        try:
            validate_bonus_prob_value(v)
            return False
        except BonusShapeError:
            return True

    t("★★契約：正しい形は通る★★", not _shape_ng(_bv))
    t("　空は通さない", _shape_ng({}) and _shape_ng(None))
    t("　設定7 のような鍵は通さない",
      _shape_ng({"7": {"big": "1/273", "reg": "1/439"}}))
    t("　知らない列は通さない",
      _shape_ng({"1": {"big": "1/273", "reg": "1/439", "zzz": "1/1"}}))
    t("　確率の形でない値は通さない",
      _shape_ng({"1": {"big": "273", "reg": "1/439"}}))
    t("★必須の列が欠けていたら通さない★",
      _shape_ng({"1": {"big": "1/273"}}))
    t("　昔の平たい形（設定→文字列）は通さない",
      _shape_ng({"1": "1/273.1"}))
    t("　合算だけは通さない（BIG/REGが要る）",
      _shape_ng({"1": {"total": "1/168.5"}}))
    t("★★合算がある設定と無い設定が混ざったら通さない★★"
      "／★記事の『列ごと出さない』という決めと食い違うため★",
      _shape_ng({"1": {"big": "1/273", "reg": "1/439", "total": "1/168"},
                 "6": {"big": "1/240", "reg": "1/240"}}))
    t("　全設定に合算が無いのは通る",
      not _shape_ng({"1": {"big": "1/273", "reg": "1/439"},
                     "6": {"big": "1/240", "reg": "1/240"}}))

    # ─── ★票の数え方（compare）を本物の入口として通す★（Codex32回目のP2）
    #   ★直す前は、抽出した辞書を手で材料へ入れていた★＝
    #   「2出典で完全一致したときだけ採用」を一度も確かめていなかった。
    def _bpage(host, val):
        # ★名簿に載っている実在の出典を使う★（2026-08-26）
        #   ★作り物のホスト名だと票に数えられず、常に「採用しない」になる★
        #   ＝どんな壊し方をしても緑になる試験になってしまう。
        return {"ok": True, "url": f"https://{host}/x", "host": host,
                "reason": "OK", "fields": {"bonus_prob": val},
                "setting_labels": []}

    _H1, _H2 = "nana-press.com", "p-town.dmm.com"

    _V = {"1": {"big": "1/273.1", "reg": "1/439.8", "total": "1/168.5"},
          "6": {"big": "1/240.1", "reg": "1/240.1", "total": "1/120.0"}}
    _r_bp = compare([_bpage(_H1, _V), _bpage(_H2, _V)])
    t("★★①2出典が完全に一致したら採用する★★",
      _r_bp["adopted"].get("bonus_prob", {}).get("value") == _V)
    _V1 = {**_V, "1": {**_V["1"], "big": "1/274.0"}}
    t("★★②1セット違うだけで採用しない★★",
      "bonus_prob" not in compare(
          [_bpage(_H1, _V), _bpage(_H2, _V1)])["adopted"])
    _V2 = {k: v for k, v in _V.items() if k != "6"}
    t("　③設定が1つ足りないだけで採用しない",
      "bonus_prob" not in compare(
          [_bpage(_H1, _V), _bpage(_H2, _V2)])["adopted"])
    _V3 = {k: {kk: vv for kk, vv in v.items() if kk != "total"}
           for k, v in _V.items()}
    t("　④片方に合算が無ければ採用しない",
      "bonus_prob" not in compare(
          [_bpage(_H1, _V), _bpage(_H2, _V3)])["adopted"])
    t("★★⑤1出典だけでは採用しない★★",
      "bonus_prob" not in compare([_bpage(_H1, _V)])["adopted"])
    # ★別名の見出しでも、正規化されたあとは同じ票になる★
    _alias_html = _BON.replace("<th>BIG</th>", "<th>BB</th>") \
        .replace("<th>REG</th>", "<th>RB</th>") \
        .replace("<th>合算</th>", "<th>合成</th>")
    _va, _ = bonus_matrix_from_tables(_BON)
    _vb, _ = bonus_matrix_from_tables(_alias_html)
    t("★★⑥見出しの書き方が違う2出典でも、同じ票になる★★",
      compare([_bpage(_H1, _va),
               _bpage(_H2, _vb)])["adopted"].get(
                   "bonus_prob", {}).get("value") == _va)


    # ─── ★題の行つきの表★（2026-08-26・Codex33回目）─────────────
    #   ★実物の形（DMM・ファンキージャグラー2）をそのまま写す★
    #     rows[0] = 題（1セルの colspan）
    #     rows[1] = 本当の見出し
    #     rows[2] = 「設定1」ではなく「1」
    _CAP = ("<table>"
            "<tr><th colspan='4'>打ち方ごとの機械割</th></tr>"
            "<tr><th>設定</th><th>BIG</th><th>REG</th><th>合算</th></tr>"
            "<tr><td>1</td><td>1/266.4</td><td>1/439.8</td>"
            "<td>1/165.9</td></tr>"
            "<tr><td>6</td><td>1/219.9</td><td>1/262.1</td>"
            "<td>1/119.6</td></tr>"
            "</table>")
    _cv, _cc = bonus_matrix_from_tables(_CAP)
    t("★★題の行つきの表を読める（DMMの形）★★"
      "／★読めないと、DMMからは設定ごとの値を1つも採れない★",
      not _cc and _cv.get("1") == {"big": "1/266.4", "reg": "1/439.8",
                                   "total": "1/165.9"})
    t("　設定の欄が『1』でも読める（『設定1』と同じ）", "6" in _cv)
    # ★題セル以外にも span がある表は通さない★（列が1つずれる）
    _CAP_SPAN = _CAP.replace("<td>1/266.4</td>",
                             "<td colspan='2'>1/266.4</td>")
    t("★★題セル以外に span があれば通さない（列がずれる）★★",
      bonus_matrix_from_tables(_CAP_SPAN)[0] == {})
    # ★題セルの幅と見出しの列数が合わない表は通さない★
    t("★題セルの幅が見出しの列数と違えば通さない★",
      bonus_matrix_from_tables(
          _CAP.replace("colspan='4'", "colspan='3'"))[0] == {})
    # ★データ行の列数がそろっていない表は通さない★
    t("★データ行の列数がそろっていなければ通さない★",
      bonus_matrix_from_tables(
          _CAP.replace("<tr><td>6</td><td>1/219.9</td><td>1/262.1</td>"
                       "<td>1/119.6</td></tr>",
                       "<tr><td>6</td><td>1/219.9</td></tr>"))[0] == {})
    # ★見出しの先頭が「設定」でなければ読まない★（順位表を拾わない）
    _RANK = _CAP.replace("<th>設定</th>", "<th>順位</th>")
    t("★★見出しの先頭が『設定』でなければ読まない（順位表を拾わない）★★",
      bonus_matrix_from_tables(_RANK)[0] == {})
    # ★数字は完全一致だけ★（1位・01・1個は落とす）
    for _bad in ("1位", "01", "1個"):
        t(f"　設定の欄が『{_bad}』なら採らない",
          "1" not in bonus_matrix_from_tables(
              _CAP.replace("<td>1</td>", f"<td>{_bad}</td>"))[0])
    # ★spanの無い表は今までどおり★
    t("　題の行が無い表は今までどおり読める",
      bonus_matrix_from_tables(_BON)[0] == _bv)
    # ★per_setting も同じ関数を通る★
    _CAP2 = ("<table>"
             "<tr><th colspan='3'>基本スペック</th></tr>"
             "<tr><th>設定</th><th>出玉率</th><th>備考</th></tr>"
             "<tr><td>1</td><td>97.0%</td><td>-</td></tr>"
             "<tr><td>6</td><td>109.0%</td><td>-</td></tr>"
             "</table>")
    t("★★題の行つきの表から、出玉率も読める（同じ関数を通る）★★",
      per_setting_from_tables(_CAP2, ("出玉率", "機械割"), "%")[0]
      == {"1": "97.0%", "6": "109.0%"})
    # ★per_setting でも、同じ設定が2行あって値が違えば食い違い★
    _DUP2 = _CAP2.replace("</table>",
                          "<tr><td>1</td><td>99.9%</td><td>-</td></tr></table>")
    t("★★per_setting でも、同じ設定の重複を黙って捨てない★★"
      "／★bonus 側だけ直していた★",
      per_setting_from_tables(_DUP2, ("出玉率", "機械割"), "%") == ({}, True))
    t("　同じ値の重複行なら止めない",
      per_setting_from_tables(
          _CAP2.replace("</table>",
                        "<tr><td>1</td><td>97.0%</td><td>-</td></tr></table>"),
          ("出玉率", "機械割"), "%")[0].get("1") == "97.0%")

    ng = [n for n, ok in results if not ok]
    print(f"{nl}{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--name", help="メーカー公式の正式名称")
    ap.add_argument("--url", action="append", help="名鑑ページのURL（2件以上）")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.name or not args.url:
        ap.print_help()
        return 0
    pages = [read_page(u, args.name) for u in args.url]
    for p in pages:
        got = {k: len(v) for k, v in p["fields"].items()}
        print(f"{p['host']:20} {p['reason']:22} {got}")
    r = compare(pages)
    print(chr(10) + json.dumps(r, ensure_ascii=False, indent=1))
    return 0 if r["adopted"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(f"★想定外の失敗 {type(e).__name__}: {e}★")
        raise SystemExit(1)

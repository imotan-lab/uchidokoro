#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""gates.py — 公開ゲートの単一情報源（Phase 1・fail-closed 状態機械）

設計正本: _design/site_policy_2026-07-24.md / _design/phase1_gates_design_v2_2026-07-24.md
第3版。Codex 敵対的レビュー2巡（値漏れ計13件）を反映。

■ 公開API（これ以外を本番経路から呼ばない）
    publish_view(machine, detail, ledger) -> {"gates":…, "machine":…, "detail":…}
        validate → compute → project を**不可分**に実行。外から gates を渡せない。
        検証エラー・未分類のリスク表現があれば GateError（＝ビルドを止める）。
    audit_view(machine, detail, ledger) -> 診断のみ（原文を返さない）
    compute_gates(machine) / validate_machine(machine)

■ 設計の要（第3版で変えた最重要点）
  ★判定単位は「葉」ではなく「実際に表示される塊（原子）」★
    見出し「期待値」＋値「580G〜」のように、単体では無害でも**結合すると断定になる**組み合わせを
    見逃さないため、label+value・title+段落・見出し行+表の行 を結合した正規形で分類する。
  ★原子の中に未知フィールドがあれば、その原子ごと拒否する★
    「未確認」等の但し書きだけ捨てて断定部分を残す事故を防ぐ（黙って捨てない）。
  ★動的キーは識別子形式のみ許可★（キーに散文を入れて診断pathから漏らす経路を塞ぐ）
  ★LEGACY_SEARCH は「当サイトの目安」表示を必須要件として明示的に出力する★

■ 不変の原則
  - fail-closed: 未指定・未知・型不正はすべて全OFF。
  - 射影は許可リスト方式。入れ子も許可スキーマで再構築する。
  - 判定順は「preview禁止話題 → 絶対禁止 → 台帳DROP → 台帳ALLOW → リスク語なし → 未分類」。
  - ads は無条件 False。SEARCH_READY / CURATED_ADS は常時閉鎖。
  - 純粋関数（ファイル書き込み・ネットワークアクセスをしない）。

  実行: python scripts/gates.py --selftest
"""
from __future__ import annotations

import hashlib
import re
import sys

# ---------------------------------------------------------------- 定数

LIFECYCLES = ("CANDIDATE", "VERIFIED_PREVIEW", "LEGACY_SEARCH", "SEARCH_READY", "CURATED_ADS")
PERMANENTLY_CLOSED = ("SEARCH_READY", "CURATED_ADS")
CHECKER_MODE_STATES = ("VERIFIED", "DISABLED", "UNVERIFIED")
HUB_NONE, HUB_PREVIEW_ONLY, HUB_FULL = "none", "preview_only", "full"
ALLOW, DROP, UNCLASSIFIED = "ALLOW", "DROP", "UNCLASSIFIED"

# LEGACY_SEARCH で狙い目数値を出すときに必ず併記する文言（設計v2 §3.2）
LEGACY_DISCLAIMER = "当サイトの目安です（メーカー公表値・確定解析ではありません）"

# 【第1層】絶対禁止。台帳ALLOWでも解除できない。
ABSOLUTE_DENY = (
    "期待収支", "プラス域", "プラス圏", "プラスライン", "プラス期待値", "期待値プラス",
    "期待値がプラス", "プラスに転じ", "期待枚数", "獲得枚数期待", "期待差枚",
    "損益分岐", "時給", "利益ゾーン", "確実な利益", "プラス収支",
)
ABSOLUTE_DENY_PAT = re.compile("|".join(re.escape(t) for t in ABSOLUTE_DENY))

# 設定段階の非存在断定（公式/複数解析の確認なしに書かない・過去に誤記事故あり）。
# 実データに「設定3・4は非搭載」「設定3・4がない」「設定3・4が存在しない」等があるため、
# 数字の列挙（・、,／/～-）と原子の区切り(" / ")を跨ぐ表現も捕まえる。
_SET_NUM = r"[1-6１-６一二三四五六]"
SETTING_DENY_PAT = re.compile(
    r"(?:設定\s*" + _SET_NUM + r"(?:\s*[・、,／/～\-〜と]\s*(?:設定\s*)?" + _SET_NUM + r")*"
    r"\s*(?:設定)?\s*(?:段階)?\s*(?:[はがもをの]|/|\s)*\s*"
    r"(?:搭載\s*(?:は|が|して)?\s*)?"
    r"(?:なし|無し|ない|無い|非搭載|未搭載|存在しない|ありません|ございません|いない|いません))"
)

# 設定の「列挙」で欠番を作り、暗に非搭載を主張する表現（実データ: "スマスロ A+BT（設定1/2/4/5/6）"）
_SET_ENUM_PAT = re.compile(r"設定\s*([1-6](?:\s*[・、,／/･]\s*[1-6]){1,5})")


def _implies_missing_setting(text: str) -> bool:
    """設定の列挙に欠番があれば「その設定は無い」と主張しているのと同じ。"""
    for m in _SET_ENUM_PAT.finditer(text):
        nums = sorted({int(x) for x in re.findall(r"[1-6]", m.group(1))})
        if len(nums) >= 2 and set(range(nums[0], nums[-1] + 1)) - set(nums):
            return True
    return False

# 【第2層】これを含む原子は台帳で明示分類されていなければ通さない（未分類=fail-closed）。
RISK_TOKENS = (
    "期待値", "収支", "プラス", "マイナス", "黒字", "赤字", "利益", "儲", "時給", "損益",
    "分岐", "勝て", "得する", "回収", "枚数", "有利", "円", "旨味", "リターン", "費用対効果",
    "機械割", "純増", "出玉",
    # Codex 4巡目で実データから検出（「約400G前後と試算」「投資効率は優秀」「狙うとお得」等）
    "試算", "計算", "投資", "収益", "お得", "甘い", "アドバンテージ", "価値",
    # 設定段階の主張は公式/複数解析の確認なしに出さない（未登録なら止める）
    "設定",
)
RISK_PAT = re.compile("|".join(re.escape(t) for t in RISK_TOKENS))

# preview（導入前）で出してはいけない話題（policy §5）
PREVIEW_FORBIDDEN_TOPICS = (
    "天井", "狙い目", "恩恵", "ヤメ", "やめ", "設定", "示唆", "解析", "予想", "スルー",
    "リセット", "期待", "純増", "機械割", "モード", "ゾーン", "確率",
)
PREVIEW_FORBIDDEN_PAT = re.compile("|".join(re.escape(t) for t in PREVIEW_FORBIDDEN_TOPICS))

# 動的キー（mode名・交換率キー等）は識別子形式のみ許可。
# → キーに散文や秘密を入れて公開/診断pathから漏らす経路を塞ぐ。
_KEY_PAT = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
# mode名として使えない予約語（既知フィールドの上書きを防ぐ）
RESERVED_CHECKER_KEYS = frozenset({
    "unit", "modes", "limit", "equivOnly", "exchangeRates", "defaultRate",
    "hasSuru", "hasCycle", "suruMax", "ok", "ng", "modeData", "byRate",
})

_SLUG_PAT = re.compile(r"^[a-z0-9_]+$")
_DATE_PAT = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_URL_PAT = re.compile(r"^https://[A-Za-z0-9./_\-?=&%#]+$")


class GateError(Exception):
    """fail-closed 違反（ビルドを止めるための例外）"""


def _is_str(v) -> bool:
    return isinstance(v, str)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _ok_key(k) -> bool:
    return _is_str(k) and bool(_KEY_PAT.match(k))


# ---------------------------------------------------------------- 検証

def validate_machine(machine: dict) -> list[str]:
    """ゲート関連フィールドを厳格検査し、エラー文の一覧を返す（空なら合格）。"""
    if not isinstance(machine, dict):
        return [f"machine が辞書でない（{type(machine).__name__}）"]
    errs: list[str] = []
    slug = machine.get("slug", "?")

    lc = machine.get("lifecycle")
    if lc is None:
        errs.append(f"{slug}: lifecycle が未指定（意図的な非公開でも 'CANDIDATE' と明示すること）")
    elif not _is_str(lc):
        errs.append(f"{slug}: lifecycle が文字列でない（{type(lc).__name__}）")
    elif lc not in LIFECYCLES:
        errs.append(f"{slug}: lifecycle が未知の値 '{lc}'")
    elif lc in PERMANENTLY_CLOSED:
        errs.append(f"{slug}: lifecycle '{lc}' は現時点では使用禁止"
                    f"（SEARCH_READY=claim検証機構待ち / CURATED_ADS=人の承認機構待ち）")

    cm = machine.get("checker_modes")
    if cm is not None:
        if not isinstance(cm, dict):
            errs.append(f"{slug}: checker_modes が辞書でない（{type(cm).__name__}）")
        else:
            for k, v in cm.items():
                if not _ok_key(k):
                    errs.append(f"{slug}: checker_modes のキーが識別子形式でない")
                elif k in RESERVED_CHECKER_KEYS:
                    errs.append(f"{slug}: checker_modes に予約キー '{k}' は使えない")
                elif v not in CHECKER_MODE_STATES:
                    errs.append(f"{slug}: checker_modes['{k}'] が未知の状態 {v!r}")

    ks = machine.get("checker_kill_switch")
    if ks is not None and not isinstance(ks, bool):
        errs.append(f"{slug}: checker_kill_switch が真偽値でない（{type(ks).__name__}）")
    return errs


# ---------------------------------------------------------------- ゲート算出

CLOSED_GATES = {
    "lifecycle": "CANDIDATE", "public": False, "index": False, "ads": False,
    "checker": False, "checker_modes": [], "hub": HUB_NONE,
    "affiliate": False, "affiliate_original": False, "profile": None,
}


def compute_gates(machine: dict) -> dict:
    """6ゲートを算出（純粋関数）。validate に1件でもエラーがあれば全OFF。"""
    if validate_machine(machine):
        return dict(CLOSED_GATES)

    lc = machine.get("lifecycle")
    if not (_is_str(lc) and lc in LIFECYCLES) or lc in PERMANENTLY_CLOSED:
        return dict(CLOSED_GATES)

    public = lc in ("VERIFIED_PREVIEW", "LEGACY_SEARCH")
    index = public and lc == "LEGACY_SEARCH"
    hub = HUB_PREVIEW_ONLY if lc == "VERIFIED_PREVIEW" else (HUB_FULL if index else HUB_NONE)

    ks = machine.get("checker_kill_switch", False)
    kill = (ks is not False)
    cm = machine.get("checker_modes")
    modes = [] if (kill or not isinstance(cm, dict)) else sorted(
        k for k, v in cm.items()
        if _ok_key(k) and k not in RESERVED_CHECKER_KEYS and v == "VERIFIED")
    checker = bool(public and index and modes)
    if not checker:
        modes = []

    affiliate = bool(public and index)
    gates = {
        "lifecycle": lc,
        "public": public,
        "index": index,
        "ads": False,
        "checker": checker,
        "checker_modes": modes,
        "hub": hub,
        "affiliate": affiliate,
        "affiliate_original": bool(affiliate and isinstance(machine.get("original"), dict)),
        "profile": {"VERIFIED_PREVIEW": "preview_basic", "LEGACY_SEARCH": "legacy_safe"}.get(lc),
    }
    assert_invariants(gates)
    return gates


def assert_invariants(g: dict) -> None:
    if g["index"] and not g["public"]:
        raise GateError("不変条件違反: index ⇒ public")
    if g["checker"] and not g["index"]:
        raise GateError("不変条件違反: checker ⇒ index")
    if g["affiliate"] and not g["index"]:
        raise GateError("不変条件違反: affiliate ⇒ index")
    if g["affiliate_original"] and not g["affiliate"]:
        raise GateError("不変条件違反: affiliate_original ⇒ affiliate")
    if g["ads"]:
        raise GateError("不変条件違反: Phase 1 で ads が True")
    if g["checker"] and not g["checker_modes"]:
        raise GateError("不変条件違反: checker=True なら表示modeが1つ以上必要")
    if not g["public"] and g["hub"] != HUB_NONE:
        raise GateError("不変条件違反: 非公開機種を hub に載せない")
    if g["public"] and g["profile"] is None:
        raise GateError("不変条件違反: 公開するのに profile が無い")
    if g["lifecycle"] in PERMANENTLY_CLOSED:
        raise GateError(f"不変条件違反: {g['lifecycle']} は閉鎖中")


# ---------------------------------------------------------------- 原子の分類

_ZERO_WIDTH = re.compile(r"[​-‏  ﻿­]")
_TAG = re.compile(r"<[^>]*>")
_MD = re.compile(r"[*_`~]")


def _to_display(s: str) -> str:
    """生JSON文字列を「ブラウザで実際に見える文字列」に近づける。

    ★なぜ要るか★ 本文は innerHTML で描画されるため、`設定3&#12394;&#12375;` や
    `設<span>定3</span>なし` は正規表現上は無害でも、画面では禁止表現として表示される。
    実データには既に <br> が存在する。
    """
    import html as _html
    import unicodedata
    prev = None
    # 多重エスケープ（&amp;#12394; 等）を展開しきる
    for _ in range(5):
        if s == prev:
            break
        prev, s = s, _html.unescape(s)
    s = _TAG.sub("", s)                       # タグ除去（分断による回避を潰す）
    s = _MD.sub("", s)                        # Markdown記号による語中分断を潰す
    s = _ZERO_WIDTH.sub("", s)                # ゼロ幅文字
    s = unicodedata.normalize("NFKC", s)      # 全角/半角・互換文字の揺れを吸収
    return re.sub(r"\s+", " ", s).strip()


def normalize_atom(parts) -> str:
    """表示される塊を正規形に。空要素を除き ' / ' で連結する。"""
    xs = [_to_display(p) for p in parts if _is_str(p)]
    return " / ".join(x for x in xs if x)


def atom_id(text: str, profile: str | None = None) -> str:
    """分類台帳のキー。profile を含めるので preview と legacy の判断を混同しない。"""
    return hashlib.sha256(f"{profile or '-'}|{text}".encode("utf-8")).hexdigest()


def classify_atom(parts, ledger: dict | None, profile: str | None = None) -> str:
    """表示原子を ALLOW / DROP / UNCLASSIFIED に判定する。

    判定順:
      0. preview で禁止話題を含む → DROP
      1. 絶対禁止／設定段階の非存在断定 → DROP（★台帳ALLOWでも解除できない★）
      2. 台帳 DROP → DROP
      3. 台帳 ALLOW → ALLOW
      4. リスク語を含まない → ALLOW
      5. リスク語ありで未登録 → UNCLASSIFIED
    """
    text = normalize_atom(parts if isinstance(parts, (list, tuple)) else [parts])
    if not text:
        return ALLOW
    if profile == "preview_basic" and PREVIEW_FORBIDDEN_PAT.search(text):
        return DROP
    if (ABSOLUTE_DENY_PAT.search(text) or SETTING_DENY_PAT.search(text)
            or _implies_missing_setting(text)):
        return DROP
    entry = (ledger or {}).get(atom_id(text, profile))
    verdict = entry.get("verdict") if isinstance(entry, dict) else None
    if verdict in (DROP, ALLOW):
        return verdict
    if not RISK_PAT.search(text):
        return ALLOW
    return UNCLASSIFIED


# ---------------------------------------------------------------- 射影

class _Ctx:
    """射影中の診断を集める（★原文も動的キーの中身も保持しない★）。"""

    def __init__(self, profile: str, ledger: dict | None):
        self.profile = profile
        self.ledger = ledger
        self.unclassified: list[dict] = []
        self.dropped: list[dict] = []
        # ★スキーマ破壊は「内容の判定」と別チャネル。必ずビルドを止める（黙って一部を落とさない）★
        self.errors: list[dict] = []

    def atom(self, parts, path: str) -> bool:
        """表示原子を判定する。落ちた場合は原子ごと出さない。"""
        v = classify_atom(parts, self.ledger, self.profile)
        if v == ALLOW:
            return True
        text = normalize_atom(parts if isinstance(parts, (list, tuple)) else [parts])
        rec = {"atom_id": atom_id(text, self.profile)[:16], "path": path}
        (self.unclassified if v == UNCLASSIFIED else self.dropped).append(rec)
        return False

    def reject(self, path: str, reason: str) -> None:
        """スキーマ破壊（未知フィールド・不正形式・構造不整合）。publish を失敗させる。"""
        self.errors.append({"path": path, "reason": reason})


def _only_keys(d: dict, allowed: set) -> bool:
    """原子の中に未知フィールドが無いこと（あれば原子ごと拒否する）。"""
    return isinstance(d, dict) and set(d.keys()) <= allowed


# --- checker
_MODE_NUM_KEYS = ("excellent", "good", "caution", "limit", "suruMax", "target", "count")
_MODE_ALLOWED = set(_MODE_NUM_KEYS) | {"note", "cycle", "suru", "byRate", "_disabled"}
_RATE_ALLOWED = {"excellent", "good", "caution", "target", "note", "suruMax"}


def _project_mode(conf, ctx: _Ctx, path: str, ctx_label: str) -> dict | None:
    """mode設定を既知キーのみで再構築。未知キーがあれば mode ごと拒否（黙って捨てない）。"""
    if not isinstance(conf, dict):
        return None
    if not _only_keys(conf, _MODE_ALLOWED):
        ctx.reject(path, "未知フィールドを含むため mode ごと拒否")
        return None
    out: dict = {}
    # ★既知フィールドの型不正は必ず止める★（noteが配列/辞書だと「注意書き無し」と誤認して
    #   数値だけ残る。数値フィールドが文字列でも同様。AI・運営者が普通に起こす型ミス）
    for k in _MODE_NUM_KEYS:
        if k in conf and not _is_num(conf[k]):
            ctx.reject(f"{path}.{k}", "数値フィールドの型不正")
            return None
    if "note" in conf and not _is_str(conf["note"]):
        ctx.reject(f"{path}.note", "noteの型不正（注意書きを失って数値だけ残るのを防ぐ）")
        return None
    nums = {k: conf[k] for k in _MODE_NUM_KEYS if _is_num(conf.get(k))}
    note = conf.get("note") if _is_str(conf.get("note")) else None
    # ★数値と注意書きは1つの原子★
    #   別々に判定すると「期待収支は算出していません」という注意書きだけが禁止語で消え、
    #   数値だけが残る意味反転が起きる（Codex指摘）。落ちたら mode ごと落とす。
    if not ctx.atom([ctx_label, note, *[f"{k}={v}" for k, v in nums.items()]], path):
        return None
    out.update(nums)
    if note:
        out["note"] = note
    # cycle / suru は「1行でも落ちたら mode ごと落とす」（部分的に残すと段が飛んで誤判定になる）
    for field in ("cycle", "suru"):
        seq = conf.get(field)
        if field not in conf:
            continue
        if not isinstance(seq, list) or not seq:
            ctx.reject(f"{path}.{field}", f"{field}の型不正")
            return None
        if field == "cycle" and all(_is_num(x) for x in seq):
            out["cycle"] = list(seq)
            continue
        rows = []
        for i, x in enumerate(seq):
            if not isinstance(x, dict):
                ctx.reject(f"{path}.{field}[{i}]", "配列要素が辞書でない")
                return None
            r = _project_mode(x, ctx, f"{path}.{field}[{i}]", ctx_label)
            if r is None:
                ctx.reject(f"{path}.{field}[{i}]", "行が公開基準を満たさない（部分的に出さない）")
                return None
            rows.append(r)
        out[field] = rows
    by = conf.get("byRate")
    if "byRate" in conf and not isinstance(by, dict):
        ctx.reject(f"{path}.byRate", "byRateの型不正")
        return None
    if isinstance(by, dict):
        rates = {}
        for rk, rv in by.items():
            # ★1つの交換率でも落ちたら mode ごと落とす★
            #   （等価だけ消えて5.6枚が残る等、条件の欠けた表示は誤誘導になる）
            if not _ok_key(rk):
                ctx.reject(f"{path}.byRate", "交換率キーが識別子形式でない")
                return None
            if not _only_keys(rv, _RATE_ALLOWED):
                ctx.reject(f"{path}.byRate.{rk}", "未知フィールドを含むため拒否")
                return None
            if "note" in rv and not _is_str(rv["note"]):
                ctx.reject(f"{path}.byRate.{rk}.note", "noteの型不正")
                return None
            r = {k: rv[k] for k in ("excellent", "good", "caution", "target", "suruMax")
                 if _is_num(rv.get(k))}
            rnote = rv.get("note") if _is_str(rv.get("note")) else None
            # 交換率別も「数値＋注意書き」で1原子（注意書きだけ消えて数値が残るのを防ぐ）
            if not ctx.atom([ctx_label, rk, rnote, *[f"{k}={v}" for k, v in r.items()]],
                            f"{path}.byRate.{rk}"):
                return None
            if rnote:
                r["note"] = rnote
            if r:
                rates[rk] = r
        if rates:
            out["byRate"] = rates
    return out or None


def _project_checker(checker, allowed_modes: list[str], ctx: _Ctx) -> dict | None:
    if not allowed_modes:
        return None
    # ★VERIFIED指定なのに checker 本体が無い／型不正は矛盾（gateは開くのに中身が出ない）★
    if not isinstance(checker, dict):
        ctx.reject("checker", "VERIFIED指定だが checker 本体が無い/辞書でない")
        return None
    if "modes" in checker and not isinstance(checker["modes"], list):
        ctx.reject("checker.modes", "modesの型不正")
        return None
    out: dict = {}
    if _is_str(checker.get("unit")) and ctx.atom([checker["unit"]], "checker.unit"):
        out["unit"] = checker["unit"]
    if isinstance(checker.get("equivOnly"), bool):
        out["equivOnly"] = checker["equivOnly"]
    if _is_num(checker.get("limit")):
        out["limit"] = checker["limit"]
    for lab in ("ok", "ng"):
        if _is_str(checker.get(lab)) and ctx.atom([checker[lab]], f"checker.{lab}"):
            out[lab] = checker[lab]
    for flag in ("hasSuru", "hasCycle"):
        if isinstance(checker.get(flag), bool):
            out[flag] = checker[flag]
    if _is_num(checker.get("suruMax")):
        out["suruMax"] = checker["suruMax"]

    er = checker.get("exchangeRates")
    if isinstance(er, list):
        rates = []
        for i, r in enumerate(er):
            if not (isinstance(r, dict) and _ok_key(r.get("key"))):
                continue
            if not _only_keys(r, {"key", "label"}):
                ctx.reject(f"checker.exchangeRates[{i}]", "未知フィールドを含むため拒否")
                continue
            e = {"key": r["key"]}
            if _is_str(r.get("label")) and ctx.atom([r["label"]], f"checker.exchangeRates[{i}].label"):
                e["label"] = r["label"]
            rates.append(e)
        if rates:
            out["exchangeRates"] = rates
            dr = checker.get("defaultRate")
            if _is_str(dr) and any(r["key"] == dr for r in rates):
                out["defaultRate"] = dr

    decl = checker.get("modes")
    if isinstance(decl, list):
        kept = []
        for i, m in enumerate(decl):
            if not (isinstance(m, dict) and m.get("key") in allowed_modes):
                continue
            if not _only_keys(m, {"key", "label", "hasSuru", "hasCycle"}):
                ctx.reject(f"checker.modes[{i}]", "未知フィールドを含むため拒否")
                continue
            e = {"key": m["key"]}
            if _is_str(m.get("label")) and ctx.atom([m["label"]], f"checker.modes[{i}].label"):
                e["label"] = m["label"]
            for flag in ("hasSuru", "hasCycle"):
                if isinstance(m.get(flag), bool):
                    e[flag] = m[flag]
            kept.append(e)
        if kept:
            out["modes"] = kept

    md = checker.get("modeData") if isinstance(checker.get("modeData"), dict) else {}
    # ★宣言集合は「元データのmodes」から取る★（射影後のoutから取ると、
    #   宣言に無いmodeを VERIFIED にした場合に検査自体がすり抜ける）
    declared = None
    if isinstance(decl, list):
        declared = {m.get("key") for m in decl if isinstance(m, dict)}
    for key in allowed_modes:
        if key in RESERVED_CHECKER_KEYS or key in out:
            ctx.reject(f"checker.{key}", "予約キーと衝突するmode名は使えない")
            continue
        top, alt = checker.get(key), md.get(key)
        # ★構造不整合はビルドを止める（黙って一部だけ出すと逆判定・実行時例外の元）★
        if isinstance(top, dict) and isinstance(alt, dict) and top != alt:
            ctx.reject(f"checker.{key}", "checker直下とmodeDataに食い違う同名configがある")
            continue
        conf = top if isinstance(top, dict) else alt
        if not isinstance(conf, dict):
            ctx.reject(f"checker.{key}", "VERIFIED指定だがconfigが存在しない")
            continue
        if "_disabled" in conf:
            ctx.reject(f"checker.{key}", "停止マーカー(_disabled)付きのmodeをVERIFIEDにできない")
            continue
        if declared is not None and key not in declared:
            ctx.reject(f"checker.{key}", "modes宣言に無いmodeをVERIFIEDにできない")
            continue
        pm = _project_mode(conf, ctx, f"checker.{key}", key)
        if pm is None:
            ctx.reject(f"checker.{key}", "modeの内容が公開基準を満たさない（部分的に出さない）")
            continue
        out[key] = pm
    # VERIFIEDなのに1つも出せないなら checker を出さない（UIが空configで例外になるのを防ぐ）
    if not any(k in out for k in allowed_modes):
        return None
    return out or None


# --- 記事本文（段落＝原子。見出しと結合して判定する）
_SECTION_ALLOWED = {"title", "type", "body", "tables", "rows"}
_TABLE_ALLOWED = {"label", "headers", "rows", "note", "wide"}
_CELL_ALLOWED = {"text", "badge"}


def _project_sections(sections, ctx: _Ctx) -> list | None:
    if sections is None:
        return None
    if not isinstance(sections, list):
        ctx.reject("sections", "sectionsの型不正")
        return None
    out = []
    for i, sec in enumerate(sections):
        p = f"sections[{i}]"
        if not isinstance(sec, dict):
            ctx.reject(p, "セクションが辞書でない")
            continue
        for k, typ in (("body", list), ("tables", list), ("rows", list), ("title", str)):
            if k in sec and not isinstance(sec[k], typ):
                ctx.reject(f"{p}.{k}", "既知フィールドの型不正")
                break
        else:
            pass
        if not _only_keys(sec, _SECTION_ALLOWED):
            ctx.reject(p, "未知フィールドを含むためセクションごと拒否")
            continue
        title = sec.get("title")
        if not (_is_str(title) and title.strip() and ctx.atom([title], f"{p}.title")):
            continue                                  # 見出しが落ちたらセクションごと落とす
        new: dict = {"title": title}
        if sec.get("type") in ("rumor", "settei"):
            new["type"] = sec["type"]

        body = sec.get("body")
        if isinstance(body, list):
            kept = []
            for j, el in enumerate(body):
                if not _is_str(el):
                    ctx.reject(f"{p}.body[{j}]", "文字列でない本文要素")
                    continue
                # ★見出し＋段落を結合して判定（単体では無害な組み合わせ断定を捕まえる）★
                if ctx.atom([title, el], f"{p}.body[{j}]"):
                    kept.append(el)
            if kept:
                new["body"] = kept

        if new.get("type") == "settei":
            tables = sec.get("tables")
            if isinstance(tables, list):
                kt = [t for t in (_project_settei_table(tb, ctx, f"{p}.tables[{ti}]", title)
                                  for ti, tb in enumerate(tables)) if t]
                if kt:
                    new["tables"] = kt
            rows = sec.get("rows")
            if isinstance(rows, list):
                kr = _project_simple_rows(rows, ctx, f"{p}.rows", title)
                if kr:
                    new["rows"] = kr

        if len(new) > (2 if "type" in new else 1):
            out.append(new)
    return out or None


def _cell_text(c, ctx: _Ctx, path: str):
    """セルを (表示文字列, 出力値) に。未知フィールドがあれば None。"""
    if _is_str(c):
        return c, c
    if isinstance(c, dict):
        if not _only_keys(c, _CELL_ALLOWED):
            ctx.reject(path, "未知フィールドを含むセル")
            return None, None
        txt = c.get("text")
        if not _is_str(txt):
            return None, None
        cell = {"text": txt}
        if _is_str(c.get("badge")):
            cell["badge"] = c["badge"]
        return normalize_atom([c.get("badge"), txt]), cell
    return None, None


def _project_settei_table(tbl, ctx: _Ctx, path: str, section_title: str) -> dict | None:
    if not isinstance(tbl, dict):
        return None
    if not _only_keys(tbl, _TABLE_ALLOWED):
        ctx.reject(path, "未知フィールドを含むため表ごと拒否")
        return None
    out: dict = {}
    label = tbl.get("label")
    if _is_str(label):
        if not ctx.atom([section_title, label], f"{path}.label"):
            return None                                # 表の見出しが落ちたら表ごと落とす
        out["label"] = label
    headers = tbl.get("headers")
    head_txt = []
    if isinstance(headers, list) and all(_is_str(h) for h in headers):
        if not ctx.atom([section_title, *headers], f"{path}.headers"):
            return None
        out["headers"] = list(headers)
        head_txt = list(headers)
    if isinstance(tbl.get("wide"), bool):
        out["wide"] = tbl["wide"]

    rows = tbl.get("rows")
    kept_rows = []
    if isinstance(rows, list):
        for ri, row in enumerate(rows):
            cells = row if isinstance(row, list) else [row]
            texts, vals, ok = [], [], True
            for ci, c in enumerate(cells):
                tx, val = _cell_text(c, ctx, f"{path}.rows[{ri}][{ci}]")
                if tx is None:
                    ok = False
                    break
                texts.append(tx)
                vals.append(val)
            # ★セクション見出し＋表見出し＋表の見出し行＋行 を結合して判定★
            #   （表labelが抜けていると「表label=期待値／行=580G〜」の複合断定を見逃す）
            if ok and vals and ctx.atom([section_title, label if _is_str(label) else None,
                                         *head_txt, *texts], f"{path}.rows[{ri}]"):
                kept_rows.append(vals)
    if kept_rows:
        out["rows"] = kept_rows
    note = tbl.get("note")
    if _is_str(note):
        # ★表の注意書きは表全体と1原子★（注意書きだけ消して表を残す意味反転を防ぐ）
        if not ctx.atom([section_title, label if _is_str(label) else None, note],
                        f"{path}.note"):
            return None
        out["note"] = note
    return out if out.get("rows") else None


def _project_simple_rows(rows, ctx: _Ctx, path: str, section_title: str):
    kept = []
    for ri, row in enumerate(rows):
        if isinstance(row, list):
            cells = row
        elif isinstance(row, dict):
            # 実データに存在する行形式（既知のものだけ許可・未知の形は構造エラー）
            for keys in ({"trigger", "hint"}, {"left", "right"}, {"label", "value"},
                         {"title", "badge", "value"}, {"title", "value"},
                         {"label", "badge", "value"}):
                # ★完全一致で判定★（部分集合だと {"value": "580G"} のような片側だけの行を
                #   通してしまい、対になる条件を失った数値が公開される）
                if set(row.keys()) == keys:
                    cells = [row[k] for k in
                             [x for x in ("trigger", "left", "label", "title") if x in row]
                             + [x for x in ("badge",) if x in row]
                             + [x for x in ("hint", "right", "value") if x in row]]
                    break
            else:
                ctx.reject(f"{path}[{ri}]", "未知の行形式")
                continue
        else:
            ctx.reject(f"{path}[{ri}]", "未知の行形式")
            continue
        texts, vals, ok = [], [], True
        for ci, c in enumerate(cells):
            tx, val = _cell_text(c, ctx, f"{path}[{ri}][{ci}]")
            if tx is None:
                ok = False
                break
            texts.append(tx)
            vals.append(val)
        if ok and vals and ctx.atom([section_title, *texts], f"{path}[{ri}]"):
            kept.append(vals)
    return kept or None


def _project_sources(v, ctx: _Ctx) -> list | None:
    if not isinstance(v, list):
        return None
    out = []
    for i, s in enumerate(v):
        if not (isinstance(s, dict) and _only_keys(s, {"url", "title", "confirmed_at"})):
            ctx.reject(f"sources[{i}]", "未知フィールドを含む出典")
            continue
        url = s.get("url")
        if not (_is_str(url) and _URL_PAT.match(url)):
            if url is not None:
                ctx.reject(f"sources[{i}].url", "URLがhttpsの想定形式でない")
            continue
        # ★クエリ・フラグメントを落とす★（署名付きURLや ?token=… を出典として
        #   貼ったときに、そのまま公開される事故を防ぐ。悪意なく普通に起こる）
        e = {"url": re.split(r"[?#]", url, 1)[0]}
        if _is_str(s.get("title")) and ctx.atom([s["title"]], f"sources[{i}].title"):
            e["title"] = s["title"]
        if _is_str(s.get("confirmed_at")) and _DATE_PAT.match(s["confirmed_at"]):
            e["confirmed_at"] = s["confirmed_at"]
        out.append(e)
    return out or None


def _project_machine(machine: dict, gates: dict, ctx: _Ctx) -> dict:
    profile = gates["profile"]
    out: dict = {}

    def s(field):
        v = machine.get(field)
        if _is_str(v) and v.strip() and ctx.atom([v], field):
            out[field] = v

    if _is_str(machine.get("slug")) and _SLUG_PAT.match(machine["slug"]):
        out["slug"] = machine["slug"]
    s("name")
    s("manufacturer")
    for f in ("release_date", "confirmed_at"):
        if _is_str(machine.get(f)) and _DATE_PAT.match(machine[f]):
            out[f] = machine[f]
    src = _project_sources(machine.get("sources"), ctx)
    if src:
        out["sources"] = src
    s("info")

    if profile == "preview_basic":
        return out

    # --- legacy_safe
    s("strategy")
    s("tenjo_display")
    al = machine.get("aliases")
    if isinstance(al, list):
        kept = [x for j, x in enumerate(al) if _is_str(x) and ctx.atom([x], f"aliases[{j}]")]
        if kept:
            out["aliases"] = kept
    lim = machine.get("limit")
    if _is_num(lim):
        out["limit"] = lim
    elif isinstance(lim, dict):
        d = {k: v for k, v in lim.items() if _ok_key(k) and _is_num(v)}
        if d:
            out["limit"] = d
    sbr = machine.get("strategyByRate")
    if isinstance(sbr, dict):
        d = {}
        for k, v in sbr.items():
            if not _ok_key(k):
                ctx.reject("strategyByRate", "交換率キーが識別子形式でない")
                continue
            if _is_str(v) and ctx.atom([k, v], f"strategyByRate.{k}"):
                d[k] = v
        if d:
            out["strategyByRate"] = d
    seo = machine.get("seo")
    if isinstance(seo, dict):
        d = {}
        for k in ("title", "description"):
            if _is_str(seo.get(k)) and ctx.atom([seo[k]], f"seo.{k}"):
                d[k] = seo[k]
        if d:
            out["seo"] = d
    if gates.get("affiliate_original") and isinstance(machine.get("original"), dict):
        o = machine["original"]
        if _only_keys(o, {"title", "kind", "search"}):
            d = {k: o[k] for k in ("title", "kind", "search")
                 if _is_str(o.get(k)) and ctx.atom([o[k]], f"original.{k}")}
            if d:
                out["original"] = d
        else:
            ctx.reject("original", "未知フィールドを含む原作情報")
    pc = _project_checker(machine.get("checker"), gates.get("checker_modes", []), ctx)
    if pc:
        out["checker"] = pc

    return out


def _project_detail(detail, gates: dict, ctx: _Ctx) -> dict:
    if gates["profile"] == "preview_basic" or not isinstance(detail, dict):
        return {}
    out: dict = {}
    lead = detail.get("lead")
    if _is_str(lead) and lead.strip() and ctx.atom([lead], "lead"):
        out["lead"] = lead

    boxes = detail.get("summaryBoxes")
    if isinstance(boxes, list):
        kept = []
        for i, b in enumerate(boxes):
            if not isinstance(b, dict):
                continue
            if not _only_keys(b, {"label", "value"}):
                ctx.reject(f"summaryBoxes[{i}]", "未知フィールドを含むため箱ごと拒否")
                continue
            lb, vl = b.get("label"), b.get("value")
            # ★label+value を結合して判定（「期待値」＋「580G〜」を見逃さない）★
            if _is_str(lb) and _is_str(vl) and ctx.atom([lb, vl], f"summaryBoxes[{i}]"):
                kept.append({"label": lb, "value": vl})
        if kept:
            out["summaryBoxes"] = kept

    ft = detail.get("factTable")
    if isinstance(ft, list):
        rows = []
        for i, r in enumerate(ft):
            if not isinstance(r, list):
                continue
            if len(r) != 2:      # ★3列目に但し書きがある行は切り捨てずに拒否する★
                ctx.reject(f"factTable[{i}]", "2列でない行（但し書きの切り捨てを防ぐため拒否）")
                continue
            th, td = r
            if _is_str(th) and _is_str(td) and ctx.atom([th, td], f"factTable[{i}]"):
                rows.append([th, td])
        if rows:
            out["factTable"] = rows

    secs = _project_sections(detail.get("sections"), ctx)
    if secs:
        out["sections"] = secs
    return out


# ---------------------------------------------------------------- 公開API

_NUM_IN_TEXT = re.compile(r"[0-9０-９]")


def _numeric_surfaces(pm: dict, pd: dict) -> list[str]:
    """公開物のうち、実際に数値が載っている表示面を列挙する（目安ラベルの必要判定）。"""
    found: list[str] = []

    def has_num(node) -> bool:
        if _is_str(node):
            return bool(_NUM_IN_TEXT.search(node))
        if _is_num(node):
            return True
        if isinstance(node, list):
            return any(has_num(x) for x in node)
        if isinstance(node, dict):
            return any(has_num(v) for v in node.values())
        return False

    for k, v in pm.items():
        if k in ("slug", "release_date", "confirmed_at", "sources", "disclaimer",
                 "display_requirements"):
            continue
        if has_num(v):
            found.append(k)
    for k, v in pd.items():
        if has_num(v):
            found.append(f"detail.{k}")
    return sorted(found)


def publish_view(machine: dict, detail: dict | None = None,
                 ledger: dict | None = None) -> dict:
    """validate → compute → project を不可分に実行。★外から gates を渡せない★

    - 検証エラー（lifecycle欠落・型不正など）は GateError（黙ってCANDIDATE扱いにしない）
    - 未分類のリスク原子があれば GateError（黙って公開しない）
    """
    errs = validate_machine(machine)
    if errs:
        raise GateError("スキーマ検証エラー: " + " / ".join(errs[:3]))
    gates = compute_gates(machine)
    if not gates["public"]:
        return {"gates": gates, "machine": {}, "detail": {}}

    ctx = _Ctx(gates["profile"], ledger)
    pm = _project_machine(machine, gates, ctx)
    pd = _project_detail(detail, gates, ctx)

    # ★スキーマ破壊は内容判定と別チャネルで必ず止める★
    if ctx.errors:
        e = ctx.errors[0]
        raise GateError(f"{machine.get('slug','?')}: 構造エラー {len(ctx.errors)}件 → 公開不可"
                        f" 例: path={e['path']} 理由={e['reason']}")
    if ctx.unclassified:
        u = ctx.unclassified[0]
        raise GateError(
            f"{machine.get('slug','?')}: 未分類のリスク表現 {len(ctx.unclassified)}件 → 公開不可"
            f"（分類台帳に ALLOW/DROP を登録すること） 例: path={u['path']} id={u['atom_id']}")

    # ★狙い目・数値を出すなら「当サイトの目安」表示を必須要件として明示する★
    #   machine側だけでなく detail 側（要約・表・本文）だけに数値がある場合も対象にする。
    # ★「キーがあるか」ではなく「実際に数値が載っているか」で判定する★
    #   文字だけの本文に一律で目安ラベルを付けるのは品質を損ね、
    #   info/seo/original に数値がある場合を見落とすため。
    surfaces = _numeric_surfaces(pm, pd)
    if surfaces:
        pm["disclaimer"] = LEGACY_DISCLAIMER
        # どの表示面に併記が必要かをフィールド単位で返す。
        # 実際に併記されることは配線時の preflight（最終HTML検査）で検証すること。
        pm["display_requirements"] = {"disclaimer": LEGACY_DISCLAIMER, "surfaces": surfaces}
    return {"gates": gates, "machine": pm, "detail": pd}


def audit_view(machine: dict, detail: dict | None = None,
               ledger: dict | None = None) -> dict:
    """診断専用。★原文も動的キーの中身も返さない★（atom_id・path・件数のみ）。"""
    errs = validate_machine(machine)
    gates = compute_gates(machine)
    if errs or not gates["public"]:
        return {"gates": gates, "errors": errs, "unclassified": [], "dropped": [],
                "ok": not errs}
    ctx = _Ctx(gates["profile"], ledger)
    _project_machine(machine, gates, ctx)
    _project_detail(detail, gates, ctx)
    return {"gates": gates, "errors": ctx.errors, "unclassified": ctx.unclassified,
            "dropped": ctx.dropped, "ok": not (ctx.unclassified or ctx.errors)}


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    import json
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    LEG = "LEGACY_SEARCH"
    base = {"slug": "x", "lifecycle": LEG, "name": "テスト機"}

    # ===== fail-closed =====
    t("lifecycle未指定 → 全OFF", compute_gates({"slug": "x"}) == CLOSED_GATES)
    t("lifecycle未知値 → 全OFF", compute_gates({"slug": "x", "lifecycle": "COMPLETE"}) == CLOSED_GATES)
    t("machineが辞書でない → 全OFF", compute_gates("nope") == CLOSED_GATES)
    t("SEARCH_READY → 常時閉鎖", compute_gates({"slug": "x", "lifecycle": "SEARCH_READY"}) == CLOSED_GATES)
    t("CURATED_ADS → 常時閉鎖", compute_gates({"slug": "x", "lifecycle": "CURATED_ADS"}) == CLOSED_GATES)
    t("phase引数が存在しない", "phase" not in compute_gates.__code__.co_varnames)

    # ★検証エラーはビルドを止める（黙ってCANDIDATE扱いにしない）
    raised = False
    try:
        publish_view({"slug": "x"})
    except GateError:
        raised = True
    t("★lifecycle欠落は GateError（黙って空を返さない）", raised)
    t("audit_view: 検証エラーを ok=False で報告",
      audit_view({"slug": "x"})["ok"] is False and audit_view({"slug": "x"})["errors"])

    # ===== ゲート =====
    gp = compute_gates({"slug": "x", "lifecycle": "VERIFIED_PREVIEW"})
    t("PREVIEW: public=ON/index=OFF/hub=preview_only",
      gp["public"] and not gp["index"] and gp["hub"] == HUB_PREVIEW_ONLY)
    gl = compute_gates(base)
    t("LEGACY: public/index/affiliate=ON・ads=OFF",
      gl["public"] and gl["index"] and gl["affiliate"] and not gl["ads"])
    t("checker: VERIFIEDのmodeだけ",
      compute_gates({**base, "checker_modes": {"normal": "VERIFIED", "suru": "UNVERIFIED"}}
                    )["checker_modes"] == ["normal"])
    t("★kill switch型不正でも開かない",
      not compute_gates({**base, "checker_modes": {"normal": "VERIFIED"},
                         "checker_kill_switch": "true"})["checker"])
    t("★予約キーをmode名にできない",
      not compute_gates({**base, "checker_modes": {"unit": "VERIFIED"}})["checker"])

    # ===== 複合断定（第3版の主眼）=====
    led_kv = {atom_id("期待値", "legacy_safe"): {"verdict": ALLOW}}
    t("★複合断定: label『期待値』+value『580G〜』は素通りしない",
      classify_atom(["期待値", "580G〜"], led_kv, "legacy_safe") == UNCLASSIFIED)
    t("★複合断定: 見出し＋段落も結合判定",
      classify_atom(["期待値の目安", "580G〜"], None, "legacy_safe") == UNCLASSIFIED)
    t("複合でも絶対禁止は必ずDROP",
      classify_atom(["目安", "580Gから期待収支がプラス"],
                    {atom_id("目安 / 580Gから期待収支がプラス", "legacy_safe"): {"verdict": ALLOW}},
                    "legacy_safe") == DROP)
    t("★設定段階の非存在断定はDROP",
      classify_atom(["設定3なし"], None, "legacy_safe") == DROP
      and classify_atom(["設定3は存在しない"], None, "legacy_safe") == DROP)
    t("台帳キーはprofileを含む（previewとlegacyの判断を混同しない）",
      atom_id("同じ文", "preview_basic") != atom_id("同じ文", "legacy_safe"))

    sb = {"summaryBoxes": [{"label": "期待値", "value": "580G〜"},
                           {"label": "天井", "value": "999G+α"}]}
    a = audit_view({**base, "checker_modes": {}}, sb)
    t("★複合断定を含む箱は未分類として止まる", len(a["unclassified"]) == 1)
    t("安全な箱は残る", not any("summaryBoxes[1]" == u["path"] for u in a["unclassified"]))

    # ===== 但し書きの切り捨て禁止 =====
    ftl = {"factTable": [["設定6機械割", "110%", "未確認・推測値"], ["天井", "999G+α"]]}
    av = audit_view(base, ftl)
    t("★3列目に但し書きがある行は切り捨てず構造エラーにする",
      any(e["path"] == "factTable[0]" for e in av["errors"]) and av["ok"] is False)
    t("2列の安全な行は構造エラーにならない",
      not any(e["path"] == "factTable[1]" for e in av["errors"]))

    # ===== 走査されない文字列が無いこと =====
    bad_checker = {**base, "checker_modes": {"normal": "VERIFIED"},
                   "checker": {"unit": "期待収支がプラス", "ok": "狙い目OK",
                               "modes": [{"key": "normal", "label": "期待収支がプラス"}],
                               "normal": {"excellent": 600}}}
    pcj = json.dumps(publish_view(bad_checker)["machine"], ensure_ascii=False)
    t("★checker.unit も分類される（危険なら落ちる）", "期待収支" not in pcj)
    t("★modes[].label も分類される", "期待収支" not in pcj)

    badge = {"sections": [{"title": "設定示唆まとめ", "type": "settei",
                           "tables": [{"label": "終了画面", "headers": ["画面", "示唆"],
                                       "rows": [[{"text": "白", "badge": "期待収支がプラス"}, "弱"]]}]}]}
    # 「設定示唆まとめ」は 設定 を含むため台帳が要る（未分類なら止まるのが正しい挙動）
    led_badge = {atom_id(s, "legacy_safe"): {"verdict": ALLOW} for s in
                 ("設定示唆まとめ", "設定示唆まとめ / 終了画面", "設定示唆まとめ / 画面 / 示唆")}
    t("★設定表の badge も分類される", "期待収支" not in json.dumps(
        publish_view(base, badge, led_badge)["detail"], ensure_ascii=False))

    # ===== 未知フィールドは原子ごと拒否 =====
    unk = {"summaryBoxes": [{"label": "天井", "value": "999G+α", "note": "未確認"}]}
    t("★未知フィールド（但し書き）を含む箱は構造エラー",
      any(e["path"] == "summaryBoxes[0]" for e in audit_view(base, unk)["errors"]))
    unk2 = {**base, "checker_modes": {"normal": "VERIFIED"},
            "checker": {"modes": [{"key": "normal"}],
                        "normal": {"excellent": 600, "private_note": 123}}}
    raised = False
    try:
        publish_view(unk2)
    except GateError:
        raised = True
    t("★未知フィールドを含むmodeは公開を止める", raised)

    # ===== 動的キーの安全性 =====
    dyn = {**base, "strategyByRate": {"秘密の文章です": "600G〜"}}
    aj = json.dumps(audit_view(dyn), ensure_ascii=False)
    t("★診断pathに散文キーが入らない", "秘密" not in aj)
    raised = False
    try:
        publish_view(dyn)
    except GateError:
        raised = True
    t("識別子形式でないキーは公開を止める", raised)

    # ===== preview =====
    prev = {"slug": "x", "lifecycle": "VERIFIED_PREVIEW", "name": "テスト機",
            "manufacturer": "メーカーA", "release_date": "2026-08-01",
            "info": {"天井": 999, "note": "600Gから狙い目"},
            "strategy": "等価600G〜", "limit": 999}
    pv = publish_view(prev, {"lead": "リード", "sections": [{"title": "天井・恩恵", "body": ["天井は999Gです。"]}]})
    dumped = json.dumps(pv["machine"], ensure_ascii=False)
    t("preview: 入れ子infoも狙い目も出さない",
      "999" not in dumped and "狙い目" not in dumped and "strategy" not in pv["machine"])
    t("preview: 記事本文を出さない", pv["detail"] == {})
    t("preview: 名称・メーカー・導入日は出す",
      pv["machine"]["name"] == "テスト機" and pv["machine"]["release_date"] == "2026-08-01")

    # ===== LEGACY の目安ラベル =====
    lv = publish_view({**base, "strategy": "等価670G〜 / 5.6枚680G〜"})
    t("★LEGACY: 狙い目を出すなら目安ラベルを必ず添える",
      lv["machine"].get("disclaimer") == LEGACY_DISCLAIMER)
    t("LEGACY: 狙い目が無ければラベルは付けない",
      "disclaimer" not in publish_view(base)["machine"])

    # ===== 実データ形状を落とさない =====
    real = {**base, "checker_modes": {"normal": "VERIFIED"},
            "checker": {"unit": "G", "hasSuru": True, "suruMax": 6,
                        "exchangeRates": [{"key": "eq56", "label": "5.6枚"}],
                        "defaultRate": "eq56", "modes": [{"key": "normal", "label": "通常"}],
                        "normal": {"excellent": 700, "target": 570,
                                   "byRate": {"eq56": {"excellent": 680, "target": 570}}},
                        }}
    rc = publish_view(real)["machine"]["checker"]
    t("実データ形状: exchangeRates/defaultRate/target/hasSuru を落とさない",
      rc["exchangeRates"][0]["key"] == "eq56" and rc["defaultRate"] == "eq56"
      and rc["normal"]["target"] == 570 and rc["hasSuru"] is True
      and rc["normal"]["byRate"]["eq56"]["target"] == 570)
    cyc = {**base, "checker_modes": {"cycle": "VERIFIED"},
           "checker": {"cycle": {"cycle": [{"count": 1, "excellent": 800}]}}}
    t("実データ形状: 周期(辞書配列)を落とさない",
      publish_view(cyc)["machine"]["checker"]["cycle"]["cycle"][0]["count"] == 1)

    # ===== 診断に原文を出さない =====
    unc = {"sections": [{"title": "収支の話", "body": ["この台は1000円くらい得します。"]}]}
    av2 = audit_view(base, unc)
    t("★audit_viewは原文を返さない",
      "得します" not in json.dumps(av2, ensure_ascii=False) and len(av2["unclassified"]) >= 1)
    raised = False
    try:
        publish_view(base, unc)
    except GateError:
        raised = True
    t("未分類があれば公開不可", raised)

    # ===== 段落の原子性 =====
    led = {atom_id("期待値の目安 / 天井は999Gです。", "legacy_safe"): {"verdict": ALLOW},
           atom_id("期待値の目安", "legacy_safe"): {"verdict": ALLOW}}
    atom = {"sections": [{"title": "期待値の目安",
                          "body": ["580Gから期待収支がプラスになります。", "天井は999Gです。"]}]}
    pa = publish_view(base, atom, led)
    aj2 = json.dumps(pa["detail"], ensure_ascii=False)
    t("段落: 絶対禁止を含む段落は丸ごと落ちる", "期待収支" not in aj2 and "580" not in aj2)
    t("段落: 同セクションの安全な段落は残る", "天井は999Gです。" in aj2)

    # ===== 第3版で塞いだ経路（Codex 3巡目の指摘）=====
    t("★HTMLエンティティで回避できない",
      classify_atom(["設定3&#12394;&#12375;"], None, "legacy_safe") == DROP)
    t("★タグで語を分断しても回避できない",
      classify_atom(["設<span>定3</span>なし"], None, "legacy_safe") == DROP)
    t("★エンティティで断定を隠せない",
      classify_atom(["期待&#21454;&#25903;がプラス"], None, "legacy_safe") == DROP)
    t("★ゼロ幅文字で回避できない",
      classify_atom(["期待​収支がプラス"], None, "legacy_safe") == DROP)
    t("★実データの複数設定表現を捕まえる（設定3・4は非搭載 等）",
      all(classify_atom([s], None, "legacy_safe") == DROP for s in
          ("設定3・4は非搭載", "設定3・4がない", "設定3・4が存在しない", "設定3がない")))
    t("★原子の区切りを跨いだ『設定3』＋『なし』も捕まえる",
      classify_atom(["設定3", "なし"], None, "legacy_safe") == DROP)

    # 注意書きだけ消して数値を残す意味反転が起きないこと
    inv = {**base, "checker_modes": {"normal": "VERIFIED"},
           "checker": {"modes": [{"key": "normal"}],
                       "normal": {"good": 580, "excellent": 700,
                                  "note": "期待収支は算出していません"}}}
    raised = False
    try:
        publish_view(inv)
    except GateError:
        raised = True
    t("★注意書きが落ちる場合は数値だけ残さず公開を止める（意味反転しない）", raised)
    t("　（診断でも数値だけの通過を許さない）",
      audit_view(inv)["ok"] is False)

    # 構造エラーは公開を止める
    for bad_checker, label in (
        ({"modes": [{"key": "normal"}], "normal": {"excellent": 600, "private": 1}}, "未知フィールド"),
        ({"modes": [{"key": "normal"}]}, "configが無い"),
        ({"modes": [{"key": "normal"}], "normal": {"excellent": 600, "_disabled": "停止"}}, "_disabled付き"),
        ({"modes": [{"key": "other"}], "normal": {"excellent": 600}}, "modes宣言に無い"),
    ):
        raised = False
        try:
            publish_view({**base, "checker_modes": {"normal": "VERIFIED"}, "checker": bad_checker})
        except GateError:
            raised = True
        t(f"★構造エラーで公開を止める（{label}）", raised)
    t("audit_view: 構造エラーを ok=False で報告",
      audit_view({**base, "checker_modes": {"normal": "VERIFIED"},
                  "checker": {"modes": [{"key": "normal"}]}})["ok"] is False)

    # 表label込みの複合断定
    tbl_led = {atom_id(s, "legacy_safe"): {"verdict": ALLOW}
               for s in ("設定示唆まとめ", "設定示唆まとめ / 期待値")}
    tbl = {"sections": [{"title": "設定示唆まとめ", "type": "settei",
                         "tables": [{"label": "期待値", "rows": [["580G〜", "◎"]]}]}]}
    t("★表label＋行の複合断定を見逃さない",
      any(u["path"].endswith("rows[0]") for u in audit_view(base, tbl, tbl_led)["unclassified"]))

    # 目安ラベルは detail だけに数値がある場合も付く
    d_only = publish_view(base, {"summaryBoxes": [{"label": "狙い目", "value": "580G〜"}]})
    t("★detailだけに狙い目がある場合も目安ラベルが付く",
      d_only["machine"].get("disclaimer") == LEGACY_DISCLAIMER)

    # ===== Codex 4巡目 (a)反例の回帰テスト =====
    t("★設定の列挙で欠番を作る暗示もDROP（実データ: 設定1/2/4/5/6）",
      classify_atom(["スマスロ A+BT（設定1/2/4/5/6）"], None, "legacy_safe") == DROP)
    t("　欠番の無い列挙は通す（設定1/2/3/4/5/6）",
      classify_atom(["設定1/2/3/4/5/6"], {atom_id("設定1/2/3/4/5/6", "legacy_safe"):
                                          {"verdict": ALLOW}}, "legacy_safe") == ALLOW)
    t("★複数設定を並べた非搭載表現もDROP",
      all(classify_atom([s], None, "legacy_safe") == DROP for s in
          ("設定3／設定4は非搭載", "設定3、設定4がない", "設定3と4を搭載していない")))
    t("★計算断定の語彙を追加（試算・投資効率・お得）",
      all(classify_atom([s], None, "legacy_safe") == UNCLASSIFIED for s in
          ("平均当選Gは約400G前後と試算されます", "投資効率は優秀です", "狙うとお得です")))

    # 型不正で「注意書きだけ消えて数値が残る」ことがない
    for bad_conf, label in (
        ({"good": 580, "note": ["期待収支は算出していません"]}, "noteが配列"),
        ({"good": "580", "note": "注意"}, "数値が文字列"),
        ({"good": 580, "byRate": {"eq56": {"excellent": 600, "note": ["注意"]}}}, "byRateのnote型不正"),
        ({"good": 580, "cycle": "1周期"}, "cycleの型不正"),
    ):
        raised = False
        try:
            publish_view({**base, "checker_modes": {"normal": "VERIFIED"},
                          "checker": {"modes": [{"key": "normal"}], "normal": bad_conf}})
        except GateError:
            raised = True
        t(f"★型不正で公開を止める（{label}）", raised)

    raised = False
    try:
        publish_view({**base, "checker_modes": {"normal": "VERIFIED"}})   # checker本体が無い
    except GateError:
        raised = True
    t("★VERIFIED指定なのにchecker本体が無ければ止める", raised)

    t("★行形式は完全一致で判定（片側だけの行を通さない）",
      any(e["path"].startswith("sections[0].rows") for e in audit_view(
          base, {"sections": [{"title": "設定示唆まとめ", "type": "settei",
                               "rows": [{"value": "580G"}]}]},
          {atom_id("設定示唆まとめ", "legacy_safe"): {"verdict": ALLOW}})["errors"]))

    t("★出典URLのクエリ・フラグメントを落とす",
      publish_view({**base, "sources": [{"url": "https://example.com/a?token=SECRET#x"}]}
                   )["machine"]["sources"][0]["url"] == "https://example.com/a")

    t("★目安ラベルは実際に数値がある時だけ付く",
      "disclaimer" not in publish_view(base, {"lead": "数字のない紹介文です。"})["machine"]
      and publish_view({**base, "strategy": "等価600G〜"})["machine"]["disclaimer"] == LEGACY_DISCLAIMER)
    t("　どの表示面に必要かを返す",
      "strategy" in publish_view({**base, "strategy": "等価600G〜"}
                                 )["machine"]["display_requirements"]["surfaces"])
    t("★sections/型不正を構造エラーにする",
      audit_view(base, {"sections": "本文"})["ok"] is False)

    # ===== 不変条件 =====
    for bad, label in (({"public": False, "index": True}, "index⇒public"),
                       ({"public": True, "index": True, "ads": True}, "ads禁止")):
        g = {**CLOSED_GATES, "profile": "legacy_safe", **bad}
        try:
            assert_invariants(g)
            ok = False
        except GateError:
            ok = True
        t(f"不変条件: {label} 違反を検出", ok)

    ng = [n for n, c in results if not c]
    print(f"\n{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    print(__doc__)

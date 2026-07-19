# -*- coding: utf-8 -*-
"""
consensus_resolver.py — 2AI自動検証・修正のコア（決定論・LLM非依存）

フェーズB第一段: ★条件同一性チェック（C5）★
「値がquoteに存在するか」ではなく「その値が"主張の条件"の値として同一文脈で
述べられているか」を確認する。権威ページから"条件違いの数字"を拾って誤公開する
最悪ケース（リセット短縮900Gを通常時天井として載せる=kaguya型）を防ぐ。

条件は2軸で扱う（混同しない）:
  - mode : normal(通常時) / reset(リセット・設定変更後) / None(=plain扱い)
  - scope: AT間 / CZ間 / ボーナス間 / 液晶 / 有利区間 / GG間 / None

値を支配する条件は「近さ（文・読点で区切った文節）」で決める。文節内に別の値があれば
その条件は別の値を支配しているとみなし、当該値には及ばない。

設計書: Documents/uchidokoro/gpt_research/consensus_design.md（v1.1・C5）
"""
from __future__ import annotations
import math
import re
import sys
import unicodedata
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HARD_DELIMS = "。．.!！?？\n；;"   # 文の区切り
SOFT_DELIMS = "、,／/"            # 文節の区切り（条件-値ペアの並列）

# 天井であることの強い指標（項目=天井のとき、値の文節にこれが要る＝ただの数字を拾わない）。
# ★「まで」「ハマり」等の弱い語は除外（fail-closed・Codex指摘「まで単独は強すぎ」）。
CEILING_INDICATORS = ["天井", "到達", "放出", "最大", "上限", "短縮"]

# モード（通常時 vs リセット）
MODE_LABELS = {
    "normal": ["通常時", "通常ゲーム", "通常時ゲーム"],
    "reset": ["リセット", "設定変更後", "設定変更時", "設定変更", "据え置き解除", "朝一", "朝イチ"],
}
# 短縮・別トリガー系マーカー（REG後/BIG後/AT後等）＝リセット(設定変更)とは別物。
# normal claim とも reset claim とも別条件なので、これが値を支配していたら両方で不採用。
SHORT_MARKERS = ["reg後", "reg時", "big後", "bb後", "rb後", "at後", "art後",
                 "rt後", "gg後", "cz後", "後は", "ボーナス終了後", "ボーナス後",
                 "引き継ぎ", "有利区間リセット"]

# 否定（値が打ち消されている）: 「1500Gではなく800G」の1500を拾わない
# ★「到達しません/届かない」等の否定も追加（Codex指摘・「1000Gに到達しません」を誤PASSしない）
NEGATION_MARKERS = ["ではなく", "では無く", "でなく", "ではない", "ではなし",
                    "されない", "はしない", "ないと", "無く",
                    "到達しない", "到達しません", "しません", "しない",
                    "届かない", "届きません"]

# 比較・不等号（その値は上限そのものでない）: 「1000G未満/以下/超える」の1000を天井にしない
# ★「以上」は「設定4以上」等と紛らわしいので入れない（fail-closed・誤REJECTを避ける）
COMPARISON_MARKERS = ["未満", "以下", "より下", "を下回", "を超え", "超える"]

# 天井でない数値（狙い目/ヤメ時/ゾーン/ボーダー等）＝天井claimでこれが文脈にあれば拒否
ANTI_CEILING = ["狙い目", "狙える", "ヤメ時", "やめ時", "やめどき", "ゾーン",
                "ボーダー", "期待値", "closeライン", "打ち始め"]

# 不確実表現（確定値にしない→REVIEW）
UNCERTAIN_MARKERS = ["推定", "予想", "おそらく", "思われ", "可能性", "未判明",
                     "不明", "かもしれ", "とみられ", "見られる", "暫定"]

# 訂正・旧情報の文脈（値が現行か旧誤りか不明→REVIEW・Codex指摘）
CORRECTION_MARKERS = ["旧情報", "誤り", "誤情報", "訂正", "修正前", "正しくは",
                      "古い情報", "旧スペック", "だった"]

# 短縮"量"の検出（「300G短縮」の300は天井値でなく短縮幅）。
# 「800Gに短縮」等は結果値なのでOK＝直前に に/へ/まで があるかで区別。
# 短縮以外の言い回し（短くなる/浅くなる/下がる/減る）も量として扱う（Codex指摘）。
_AMOUNT_RE = re.compile(r"(?<![にへまで])(短縮|短くな|浅くな|下がる|減る)")

# 範囲（scope）
SCOPE_LABELS = {
    "AT間": ["at間", "at後"],
    "CZ間": ["cz間", "cz後"],
    "ボーナス間": ["ボーナス間", "bb間", "big間", "ボーナス後", "big後", "bb後"],
    "液晶": ["液晶"],
    "有利区間": ["有利区間"],
    "GG間": ["gg間"],
}


def normalize(s: str) -> str:
    """NFKC正規化＋英字小文字化。空白・句読点は保持（文節の切り出しに使う）。
    ★数字中の桁区切りカンマ（1,000）は除去＝文節区切りと誤認しない＋値照合を単純化。"""
    s = unicodedata.normalize("NFKC", s or "")
    s = re.sub(r"(?<=\d)[,，](?=\d)", "", s)   # 1,000 → 1000
    return s.replace("〜", "~").replace("～", "~").lower()


def _norm_mode(mode) -> str | None:
    if mode in (None, ""):
        return None
    m = str(mode).strip().lower()
    if m in ("normal", "通常", "通常時"):
        return "normal"
    if m in ("reset", "リセット", "設定変更後", "設定変更"):
        return "reset"
    return None


def _norm_scope(scope) -> str | None:
    if scope in (None, "", "通常時", "通常"):
        return None  # 通常時=範囲指定なし扱い
    s = str(scope).strip().replace("ＡＴ", "AT").replace("ＣＺ", "CZ").replace("ＢＢ", "BB")
    return s


def _value_variants(value) -> list[str]:
    v = value
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v)
    out = {s}
    if s.isdigit() and len(s) > 3:
        out.add(f"{int(s):,}")
    return list(out)


def _find_value_positions(nq: str, value) -> list[tuple[int, int]]:
    """値の全出現(start, end)（数字境界つき＝1000が10000/1000の一部に誤一致しない）。
    ★endは実際にマッチした文字列の終端＝文脈切り出しに使う（str(value)の長さと
    マッチ長がズレる小数・カンマ表記のバグ対策・Codex指摘）。"""
    spans = []
    for tok in _value_variants(value):
        for m in re.finditer(re.escape(normalize(tok)), nq):
            i, j = m.start(), m.end()
            before = nq[i - 1] if i > 0 else ""
            after = nq[j] if j < len(nq) else ""
            if before.isdigit() or before == "." or after.isdigit() or after == ".":
                continue
            spans.append((i, j))
    return sorted(set(spans))


def _has_number(seg: str) -> bool:
    return bool(re.search(r"\d", seg))


def _governing_context(nq: str, pos: int, vlen: int) -> str:
    """値を支配する文脈を切り出す。値を含む文（HARD区切り）→ SOFTで文節分割し、
    値のいる文節を基本にする。値の文節に条件語が無ければ、直前の"数字を含まない"文節も足す
    （「通常時は、天井1000G」型を拾う）。直前文節が数字を含むなら足さない（別ペアの条件を混ぜない）。"""
    left = 0
    for i in range(pos - 1, -1, -1):
        if nq[i] in HARD_DELIMS:
            left = i + 1
            break
    right = len(nq)
    for i in range(pos + vlen, len(nq)):
        if nq[i] in HARD_DELIMS:
            right = i
            break
    sent = nq[left:right]
    off = pos - left

    # SOFTで文節分割
    segs, s = [], 0
    for i, ch in enumerate(sent):
        if ch in SOFT_DELIMS:
            segs.append((s, i))
            s = i + 1
    segs.append((s, len(sent)))
    seg_i = next((k for k, (a, b) in enumerate(segs) if a <= off < b), len(segs) - 1)
    a, b = segs[seg_i]
    ctx = sent[a:b]
    # 直前文節が数字を含まなければ条件語の供給源として足す
    if seg_i > 0:
        pa, pb = segs[seg_i - 1]
        prev = sent[pa:pb]
        if not _has_number(prev):
            ctx = prev + ctx
    return ctx


def _hit(ctx: str, labels: list[str]) -> list[str]:
    return [lb for lb in labels if normalize(lb) in ctx]


PASS, REJECT, REVIEW = "PASS", "REJECT", "REVIEW"


def check_claim_identity(item, mode, scope, value, unit, quote,
                         operator=None) -> tuple[str, str, str]:
    """★C5（fail-closed・3値）: 値が「その mode/scope の・その項目の」値として同一文脈で
    述べられているか★（Codexレビュー反映・2026-07-19）
    戻り値 (verdict, reason, code)。★code は機械可読な理由コード（Codex指摘6）★:
      OK / MISSING_QUOTE / VALUE_ABSENT / NEGATED / REDUCTION_AMOUNT / UNCERTAIN /
      CORRECTION / NOT_CEILING / MISSING_UNIT / MISSING_CEILING_INDICATOR /
      MODE_SCOPE_AMBIGUOUS(=claim定義混在) / MODE_MISMATCH / MODE_MISSING /
      SCOPE_MISMATCH / SCOPE_MISSING / OPERATOR_MISMATCH / CONTRADICTION / INSUFFICIENT
    verdict:
      PASS  … quoteが明確にこの主張を支持（自動採用してよい）
      REJECT… quoteが明確に否定/別条件/否定文/短縮量/狙い目等（値は誤り）
      REVIEW… 判断材料が不足・不確実（自動採用しない＝現状維持/preview・人へ）
    fail-closed: 明確にPASSでない曖昧なものは全てREVIEW（誤公開しない）。"""
    nq = normalize(quote)
    if not nq:
        return REVIEW, "quoteが空", "MISSING_QUOTE"
    positions = _find_value_positions(nq, value)
    if not positions:
        return REJECT, f"値 {value} がquoteに存在しない（quoteが主張を支持しない）", "VALUE_ABSENT"

    nunit = normalize(str(unit)) if unit not in (None, "") else ""
    m = _norm_mode(mode)
    sc = _norm_scope(scope)
    is_ceiling = (item in (None, "天井", "ceiling"))

    saw_contradiction = False
    review_reason = "判断材料が不足（fail-closed→REVIEW）"
    review_code = "INSUFFICIENT"
    reject_reason = "別条件/否定/短縮量/狙い目等の疑い"
    reject_code = "CONTRADICTION"

    for pos, end in positions:
        vlen = end - pos
        ctx = _governing_context(nq, pos, vlen)
        after = nq[pos: min(len(nq), end + 8)]  # 値の直後（否定・短縮量の隣接判定）

        # ★否定★「1500Gではなく」→この出現は打ち消し＝矛盾（直後＋支配文節の両方を見る）
        if _hit(after, NEGATION_MARKERS) or _hit(ctx, NEGATION_MARKERS):
            saw_contradiction = True
            reject_reason, reject_code = "否定文脈＝値が打ち消されている", "NEGATED"
            continue
        # ★比較・不等号★「1000G未満/以下/超える」＝その値は上限そのものでない（Codex指摘）
        if _hit(after, COMPARISON_MARKERS) or _hit(ctx, COMPARISON_MARKERS):
            saw_contradiction = True
            reject_reason, reject_code = "比較表現（未満/以下/超える）＝上限そのものでない", "COMPARISON"
            continue
        # ★短縮"量"★「300G短縮/短くなる」（に/へ/までが無い）＝天井値でなく短縮幅
        if _AMOUNT_RE.search(after):
            saw_contradiction = True
            reject_reason = "『N G短縮/短くなる』＝短縮量であって天井値でない"
            reject_code = "REDUCTION_AMOUNT"
            continue
        # 不確実表現 → REVIEW（確定値にしない）
        if _hit(ctx, UNCERTAIN_MARKERS):
            review_reason = f"不確実表現（{_hit(ctx, UNCERTAIN_MARKERS)}）＝確定値にしない"
            review_code = "UNCERTAIN"
            continue
        # 訂正/旧情報の文脈 → REVIEW（現行値か旧誤りか一意に決められない）
        if _hit(ctx, CORRECTION_MARKERS):
            review_reason = f"訂正/旧情報の文脈（{_hit(ctx, CORRECTION_MARKERS)}）"
            review_code = "CORRECTION"
            continue
        # 天井claimなのに狙い目/ゾーン等の文脈 → 別種の数字＝矛盾
        if is_ceiling and _hit(ctx, ANTI_CEILING):
            saw_contradiction = True
            reject_reason = f"天井でない数値の文脈（{_hit(ctx, ANTI_CEILING)}）"
            reject_code = "NOT_CEILING"
            continue
        # (a) 単位（指定時）が文脈に無い → 材料不足
        if nunit and nunit not in ctx:
            review_reason, review_code = "単位が値の文脈に無い", "MISSING_UNIT"
            continue
        # (b) 天井らしさ指標が無い → 材料不足（ただの数字かも）
        if is_ceiling and not _hit(ctx, CEILING_INDICATORS):
            review_reason = "天井を示す語（天井/到達/放出/最大/短縮等）が文脈に無い"
            review_code = "MISSING_CEILING_INDICATOR"
            continue

        # (c) モード判定
        reset_hits = _hit(ctx, MODE_LABELS["reset"])
        short_hits = _hit(ctx, SHORT_MARKERS)
        normal_hits = _hit(ctx, MODE_LABELS["normal"])
        ctx_scopes = [fam for fam, labels in SCOPE_LABELS.items() if _hit(ctx, labels)]
        # ★曖昧: 同一文脈に複数mode/複数scope → 一意に決められない＝REVIEW（fail-closed）
        #   これがclaim定義混在の主症状＝コード MODE_SCOPE_AMBIGUOUS（Cの claim_def 分類に使う）
        if (normal_hits and reset_hits) or len(ctx_scopes) > 1:
            review_reason = "同一文脈に複数の条件（mode/scope）＝一意に決められない"
            review_code = "MODE_SCOPE_AMBIGUOUS"
            continue
        if m == "reset":
            if short_hits:
                saw_contradiction = True
                reject_reason = f"reset claimだが別トリガー（{short_hits}）が支配"
                reject_code = "MODE_MISMATCH"
                continue
            if not reset_hits:
                if normal_hits:
                    saw_contradiction = True
                    reject_reason = "reset claimだが文脈は通常時"
                    reject_code = "MODE_MISMATCH"
                else:
                    review_reason = "reset claimだがリセット(設定変更)の語が文脈に無い"
                    review_code = "MODE_MISSING"
                continue
        else:  # normal / None は「素の通常天井」を要求（短縮された値は素の通常天井でない）
            reduced_hits = _hit(ctx, ["短縮", "ダウン", "減算"])
            if reset_hits or short_hits or reduced_hits:
                saw_contradiction = True
                reject_reason = ("通常時claimだが別モード/別トリガー/短縮: "
                                 f"{reset_hits + short_hits + reduced_hits}")
                reject_code = "MODE_MISMATCH"
                continue

        # (d) 範囲判定（ctx_scopes は (c) で算出済み）
        if sc:
            if ctx_scopes and sc not in ctx_scopes:
                saw_contradiction = True
                reject_reason = f"別scope（{ctx_scopes}）の値をscope『{sc}』として拾おうとした"
                reject_code = "SCOPE_MISMATCH"
                continue
            if sc not in ctx_scopes:
                review_reason = f"scope『{sc}』が文脈で確認できない"
                review_code = "SCOPE_MISSING"
                continue
        else:
            if ctx_scopes:
                saw_contradiction = True
                reject_reason = f"scope無指定claimだが文脈は特定scope: {ctx_scopes}"
                reject_code = "SCOPE_MISMATCH"
                continue

        # (e) operator整合（任意・矛盾のみ）
        if operator:
            has_max = ("最大" in ctx) or ("上限" in ctx) or ("max" in ctx)
            has_reach = bool(re.search(r"到達で|消化で|ちょうど|きっかり", ctx)) or \
                bool(re.search(r"g(で|にて)(天井|当選|放出|突入)", ctx))
            if operator == "max" and has_reach and not has_max:
                saw_contradiction = True
                reject_reason = "operator=maxだが文脈は到達確定表現"
                reject_code = "OPERATOR_MISMATCH"
                continue
            if operator == "exact" and has_max and not has_reach:
                saw_contradiction = True
                reject_reason = "operator=exactだが文脈は最大表現"
                reject_code = "OPERATOR_MISMATCH"
                continue

        return PASS, f"C5合格（mode={m or 'plain'}/scope={sc or 'なし'}・文脈一致）", "OK"

    # PASSする出現が無かった
    if saw_contradiction:
        return REJECT, reject_reason, reject_code
    return REVIEW, review_reason, review_code


# ══════════════════════════════════════════════════════════════
# B-2: 項目別 異常検知（Codex 3段設計・2026-07-19）
#   1) ハード制約 → REJECT（型/単位/小数桁/物理的に不可能な範囲）
#   2) 関係制約  → REVIEW（設定間の単調性等・機種仕様依存なので断定しない）
#   3) 異常値検知 → REVIEW（前回公開値との差/小数点移動/一桁置換/典型帯外）
#   公開の必要条件: C5==PASS かつ anomaly_check==PASS かつ 配列検査==PASS
#   （REJECTもREVIEWも自動公開を止める。関係制約REVIEWは人手確認へ・Codex指摘で修正）
# ══════════════════════════════════════════════════════════════

ITEM_SPEC = {
    # key:            kind   unit  hard範囲(物理限界)   典型帯(soft)       小数  跳ね閾値(絶対,相対)
    "ceiling.game":  {"kind": "int",   "unit": "G",  "hard": (50, 5000),   "typical": (150, 2800), "jump_abs": 400,  "jump_rel": 0.2},
    "ceiling.point": {"kind": "int",   "unit": "pt", "hard": (100, 9000),  "typical": (300, 5000), "jump_abs": 1000, "jump_rel": 0.3},
    "ceiling.through": {"kind": "int", "unit": None, "hard": (1, 40),      "typical": (1, 20),     "jump_abs": 3,    "jump_rel": 0.5},
    "ceiling.cycle": {"kind": "int",   "unit": None, "hard": (1, 40),      "typical": (1, 20),     "jump_abs": 3,    "jump_rel": 0.5},
    "kikaiwari":     {"kind": "float", "unit": "%",  "hard": (90.0, 135.0), "typical": (95.0, 120.0), "decimals": 1, "jump_abs": 3.0, "jump_rel": 0.05},
}


def _as_num(v):
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):   # 巨大int(10**10000)等も安全に無効化（指摘）
        return None
    return f if math.isfinite(f) else None


def _is_decimal_shift(a: float, b: float) -> bool:
    """a が b の10倍/1/10（小数点移動）に近いか。"""
    if b == 0:
        return False
    for factor in (10.0, 0.1):
        if abs(a - b * factor) <= max(1e-9, abs(b * factor) * 1e-6):
            return True
    return False


def anomaly_check(item_key: str, value, current=None, unit=None) -> tuple[str, list[str]]:
    """単一値の異常検知。戻り値 (verdict, flags)。
    verdict: REJECT(ハード違反) / REVIEW(異常フラグあり) / PASS(問題なし)。"""
    spec = ITEM_SPEC.get(item_key)
    if spec is None:
        return REVIEW, [f"未知の項目 {item_key}（仕様未定義＝自動採用しない）"]
    if isinstance(value, bool):  # ★True/Falseはfloat()で1.0/0.0になり素通りするので明示REJECT
        return REJECT, ["真偽値は数値でない"]
    x = _as_num(value)
    if x is None:
        return REJECT, ["数値でない"]
    # 単位不一致（指定時）→ REJECT（ceiling.game=1000pt のような単位取り違え）
    if unit is not None and spec.get("unit") and \
            normalize(str(unit)) != normalize(str(spec["unit"])):
        return REJECT, [f"単位不一致: {unit}（{item_key}は{spec['unit']}）"]
    # (1) ハード: 型（★許容誤差でなく厳密に判定＝微小小数での回避を防ぐ・Codex8次）
    if spec["kind"] == "int" and not x.is_integer():
        return REJECT, [f"整数であるべき項目に小数 {value}"]
    if spec["kind"] == "float" and "decimals" in spec:
        # ★小数桁は Decimal(str) の指数で厳密判定（"%.10f" 丸めで 97.800...001 を1桁と誤認しない）
        try:
            exp = Decimal(str(value)).normalize().as_tuple().exponent
        except (InvalidOperation, ValueError):
            return REJECT, [f"数値として解釈不能: {value}"]
        decimals = -exp if isinstance(exp, int) and exp < 0 else 0
        if decimals > spec["decimals"]:
            return REJECT, [f"小数桁が仕様({spec['decimals']})超過: {value}"]
    # (1) ハード: 物理的に不可能な範囲
    lo, hi = spec["hard"]
    if not (lo <= x <= hi):
        return REJECT, [f"物理的に不可能な範囲: {value}（許容 {lo}〜{hi}）"]

    flags = []
    # (3) 異常値: 典型帯外（soft）
    tlo, thi = spec["typical"]
    if not (tlo <= x <= thi):
        flags.append(f"典型帯({tlo}〜{thi})外: {value}")
    # (3) 異常値: 前回公開値との比較
    c = _as_num(current)
    if c is not None:
        if _is_decimal_shift(x, c):
            flags.append(f"小数点/桁移動の疑い（{current}→{value}）")
        jabs = abs(x - c)
        # 閾値ちょうども異常側に倒す（>= ・Codex指摘: >だと境界の誤入力を見逃す）
        if jabs >= spec["jump_abs"] or (c and jabs >= spec["jump_rel"] * abs(c)):
            flags.append(f"前回公開値から大きく変化（{current}→{value}）")
    return (REVIEW if flags else PASS), flags


def anomaly_check_setting_array(item_key: str, settings: dict,
                                current: dict | None = None) -> tuple[str, list[str]]:
    """設定別の値配列（{設定番号: 値}）の関係制約＋各値のハード/異常検査。
    ★前回の設定別配列 current を渡すと、各設定を前回値と比較する（配列一括+10化けを検知）★
    単調性・同一値は REVIEW（機種仕様依存＝断定しない）。各値のハード違反は REJECT。"""
    flags = []
    for s, v in settings.items():
        cur = (current or {}).get(s)
        vd, fl = anomaly_check(item_key, v, current=cur)
        if vd == REJECT:
            return REJECT, [f"設定{s}: " + "; ".join(fl)]
        flags += [f"設定{s}: {f}" for f in fl]
    vals = [settings[s] for s in sorted(settings)]
    if len(set(vals)) < len(vals):
        flags.append("設定間で同一値あり（要確認）")
    if vals != sorted(vals):
        flags.append("設定順で単調増加でない（機種仕様なら可・要確認）")
    return (REVIEW if flags else PASS), flags


# ══════════════════════════════════════════════════════════════
# B-3: 出典の格による決着（決定論・AIのメンツは0点・2026-07-19）
#   入力: 同一claimに対する候補値のリスト。各候補は複数の source を持つ。
#     source = {domain, verified(C5+再取得OK), group(転載系列=独立票の単位), official}
#   規則: ①verified な source だけ数える ②転載系列(group)は1票 ③項目別の格付けと
#         複数独立一致で勝者 ④決まらねば REVIEW（無理に勝者を作らない）
#   ★どちらのAIが言ったかは一切見ない（source の格と裏取りだけ）★
# ══════════════════════════════════════════════════════════════

# 大手解析ドメイン（tier2）。公式は OFFICIAL_DOMAINS で tier1（自己申告フラグは信用しない）。
MAJOR_DOMAINS = {"chonborista.com", "1geki.jp", "nana-press.com",
                 "slopachi-quest.com", "dmm.com"}
# ★メーカー公式ドメイン（tier1）＝サーバー側の許可リストで判定（呼び出し側のofficial自己申告は無視）。
#   実ドメインは要確認の暫定。誤って落ちても tier2/3 に下がるだけで安全側（過大評価はしない）。
OFFICIAL_DOMAINS = {"kitadenshi.jp", "sammy.co.jp", "daito.co.jp", "universal-777.com"}
NERAI_BASIS = "slopachi-quest.com"
MIN_DOMAINS = {"ceiling.game": 2, "ceiling.point": 2, "ceiling.through": 2,
               "ceiling.cycle": 2, "kikaiwari": 2, "nerai": 1}
_MULTI_SUFFIX = {"co.jp", "ne.jp", "or.jp", "go.jp", "ac.jp", "ad.jp", "lg.jp",
                 "gr.jp", "ed.jp", "co.uk", "com.au"}


def _domain_key(domain: str) -> str:
    """★独立票の単位を"サーバー側で"算出（呼び出し側の申告は使わない・Codex指摘3）★
    URL/ホスト申告を urlsplit で実ホスト解析→eTLD+1 へ厳格正規化。
    ★弾く: 非str / http(s)以外のscheme / userinfo(user:pass@) / IP / 空白 /
      空ラベル(連続ドット a..b) / 英数ハイフン以外のラベル文字 / 単一ラベル★
    （偽装で公式tier1へ昇格する事故を防ぐ）。正規化できない入力は空文字＝票にならない(安全側)。"""
    if not isinstance(domain, str):
        return ""
    d = domain.strip().lower()
    if not d or re.search(r"\s", d):   # 内部の空白/タブ/改行を拒否（sammy.co.\njp 対策・指摘）
        return ""
    probe = d if "://" in d else "//" + d   # スキーム無しは // 前置で netloc として解釈
    try:
        u = urlsplit(probe)
        host = u.hostname or ""
        _ = u.port          # 不正ポート(:99999 / :abc)はここで ValueError（指摘）
    except ValueError:
        return ""
    if u.scheme and u.scheme not in ("http", "https"):
        return ""
    if "@" in u.netloc:   # userinfo(sammy.co.jp@evil.example / 空 :@ も)を全部拒否（指摘）
        return ""
    host = host.rstrip(".")
    if not host or re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host):
        return ""   # 空 / IPv4 は独立ドメイン票にしない
    labels = host.split(".")
    if any(not lb for lb in labels):
        return ""   # 空ラベル（a..b の連続ドット）は不正
    if not all(re.fullmatch(r"[a-z0-9-]+", lb) for lb in labels):
        return ""   # 英数ハイフン以外（IDNAはxn--でここを通る・非ASCIIは安全側で拒否）
    if len(labels) < 2:
        return ""   # 単一ラベル(localhost等)は eTLD+1 にならない
    if len(labels) >= 3 and ".".join(labels[-2:]) in _MULTI_SUFFIX:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _norm_value(v):
    """値の正規化（1000 と 1000.0 を同一視して候補統合）。
    ★数値型(int/float)のみ受理。bool・文字列・NaN/Inf・float化不能な巨大数 は None（票にしない）。
      整数は float に潰さず元の整数で返す（2^53超の別整数を誤統合しない・Codex7次）。
      float は4桁丸めせず、整数値の 1000.0 のみ int へ寄せる（1000.00004 は別値・Codex6次）。★"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if isinstance(v, int):
        try:
            if not math.isfinite(float(v)):
                return None
        except (OverflowError, ValueError):
            return None
        return v
    if not math.isfinite(v):
        return None
    return int(v) if v.is_integer() else v


def classify_source(item_key: str, domain: str) -> int:
    """source の格（tier・小さいほど強い）。1=公式/基準 2=大手解析 3=その他。
    ★公式判定は許可リストのみ・自己申告フラグは受け取らない★"""
    dk = _domain_key(domain)
    if dk in OFFICIAL_DOMAINS:
        return 1
    if item_key == "nerai" and dk == NERAI_BASIS:
        return 1  # 狙い目はスロパチクエストが基準＝最上位
    if dk in MAJOR_DOMAINS:
        return 2
    return 3


def _score(item_key: str, candidates: list) -> dict:
    """resolve/diagnose 共通の内部集計（★単一情報源・二重管理を作らない★）。
    verified な source だけを「正規化値 → {独立ドメイン: 元の値}」へ集約（同一値を統合）し、
    1ドメインが複数値を支持したら ambiguous に隔離。C5判定の内訳も数える（Cの終端分類用）。"""
    from collections import defaultdict
    val_domains: dict = defaultdict(dict)
    c5_counts = {PASS: 0, REJECT: 0, REVIEW: 0, "NONE": 0}
    n_sources = 0
    claim_def_hits = 0   # C5=MODE_SCOPE_AMBIGUOUS の数（Cの claim_def 分類に使う・指摘6）
    for cand in candidates:
        nv = _norm_value(cand.get("value"))
        for s in (cand.get("sources") or []):
            n_sources += 1
            cv = s.get("c5_verdict")
            # ★cvがunhashable(list等)でも落ちない（Codex指摘4）: strのみ集計・他は"NONE"
            c5_counts[cv if (isinstance(cv, str) and cv in c5_counts) else "NONE"] += 1
            # ★claim定義混在は「c5=REVIEW かつ code=MODE_SCOPE_AMBIGUOUS」だけ数える（指摘8）
            #   （REJECT+code や 別codeの誤加算を防ぐ）
            if cv == REVIEW and s.get("c5_code") == "MODE_SCOPE_AMBIGUOUS":
                claim_def_hits += 1
            # ★verifiedはbool True値のみ・C5合格必須（自己申告"false"/型崩れ/verified⇔c5不整合を
            #   弾く・Codex指摘2/5）。多層防御: 検証器の付与ミスもここで再度落とす。
            if s.get("verified") is not True or cv != PASS:
                continue
            # ★unit取り違えを弾く（ceiling.game に「1000pt」等）: canonical unit と明示不一致は票にしない
            spec_unit = ITEM_SPEC.get(item_key, {}).get("unit")
            su = s.get("unit")
            if spec_unit and su is not None and normalize(str(su)) != normalize(str(spec_unit)):
                continue
            dk = _domain_key(s.get("domain", ""))
            if dk and nv is not None:
                val_domains[nv].setdefault(dk, cand.get("value"))
    # 1ドメインが複数の値を支持 → そのドメインは解釈が不定＝全候補から除外（Codex指摘）
    dom_vals: dict = defaultdict(set)
    for nv, doms in val_domains.items():
        for dk in doms:
            dom_vals[dk].add(nv)
    ambiguous = {dk for dk, vs in dom_vals.items() if len(vs) > 1}
    scored = []
    for nv, doms in val_domains.items():
        good = [dk for dk in doms if dk not in ambiguous]
        if not good:
            continue
        tiers = [classify_source(item_key, dk) for dk in good]
        scored.append({"value": val_domains[nv][good[0]], "best_tier": min(tiers),
                       "indep": len(set(good)), "has_official": 1 in tiers})
    scored.sort(key=lambda c: (c["best_tier"], -c["indep"]))
    return {"scored": scored, "ambiguous": ambiguous, "c5_counts": c5_counts,
            "n_sources": n_sources, "any_verified": bool(val_domains),
            "claim_def_hits": claim_def_hits, "val_domains": dict(val_domains)}


def resolve(item_key: str, candidates: list):
    """候補値を出典の格で決着。戻り値 (verdict, winning_value|None, reason, scored, info)。
    verdict: PASS(採用値決定) / REVIEW(決まらない=止める)。
    ★info は _score の診断dict（Cフェーズが往復判断に使う・第5要素は後方互換で追加）★
    ★source.verified は「C5合格＋再取得OK＝その値を支持」の意味（取得成功だけでは不可）★"""
    info = _score(item_key, candidates)
    scored = info["scored"]
    if not info["any_verified"]:
        return REVIEW, None, "裏取り済みの候補が無い（自動採用しない）", [], info
    if not scored:
        return REVIEW, None, "曖昧な出典を除くと裏取り候補が残らない", [], info
    # ★公式(tier1)が複数値を支持しambiguous化＝公式が対立を隠している→自動決着禁止（Codex指摘・3次）
    if any(classify_source(item_key, dk) == 1 for dk in info["ambiguous"]):
        return REVIEW, None, "公式ソースが複数値を支持（対立を隠す疑い）＝人手判断", scored, info
    top = scored[0]
    min_dom = MIN_DOMAINS.get(item_key, 2)
    # ★公式(tier1)が複数の値に割れている＝票数で決めず必ず止める（Codex指摘・公式対立）
    if sum(1 for c in scored if c["has_official"]) >= 2:
        vals = [c["value"] for c in scored if c["has_official"]]
        return REVIEW, None, f"公式(tier1)が対立＝人手判断（{vals}）", scored, info
    # ★tier3(無名サイト)だけの合意は信用しない＝大手/公式(tier<=2)が最低1つ要る（シビル攻撃対策）
    if top["best_tier"] >= 3:
        return REVIEW, None, \
            f"信頼できる情報源(大手/公式)が無い（tier3のみ・独立{top['indep']}）", scored, info
    if not top["has_official"] and top["indep"] < min_dom:
        return REVIEW, None, \
            f"裏取り不足（独立ドメイン{top['indep']}<必要{min_dom}・公式なし）", scored, info
    if len(scored) == 1:
        return PASS, top["value"], \
            f"単独裏取り勝ち（tier{top['best_tier']}・独立{top['indep']}）", scored, info
    second = scored[1]
    strictly_better = (top["best_tier"] < second["best_tier"]) or \
        (top["best_tier"] == second["best_tier"] and top["indep"] > second["indep"])
    if strictly_better:
        return PASS, top["value"], \
            f"格上/複数一致で決着（{top['value']} 採用・対抗 {second['value']}）", scored, info
    return REVIEW, None, \
        f"信頼出典間で競合＝DISPUTED（{top['value']} vs {second['value']}・同格同数）", scored, info


# ══════════════════════════════════════════════════════════════
# C: 話し合いループ（Claude ↔ Codex）の終端状態機械（決定論・設計書v1.3＋Codexレビュー）
#   上限往復後は無限ループでなく明示的終端へ。終端8種:
#     PASS                       決着値が決まり最終異常ゲートも通過（呼び出し側で verify_claims 必須）
#     REVIEW_RESEARCHABLE        独立裏取り不足/無名tier3のみ（後日 大手/公式を探せば解ける見込み）
#     REVIEW_MANUAL              信頼ソース対立(公式対立含む) or 往復尽きて未決（人手判断へ・既定）
#     REVIEW_CLAIM_DEFINITION    claim条件定義の混在(C5=MODE_SCOPE_AMBIGUOUS支配・抽出側を直す)
#     REVIEW_SOURCE_CONFLICT     1ソースが複数値を支持し解釈不定（ソースの質の問題）
#     REVIEW_ANOMALY             決着値が異常検知に掛かった（bool/範囲外/桁移動・要人手）
#     BLOCKED_NO_EVIDENCE        裏取りできるソースが皆無
#     BLOCKED_VERIFICATION_ERROR 検証器が例外/取得不能で一部証拠を検証できず（安全側に停止）
#                                （※ask_codex/researchの例外は input_bad 扱いでPASSを止めない）
#   ★AI/callbackは"生の証拠"を出すだけ・裏取り印(verified)は verify(検証器)だけが付与★
#   ★決着・遷移・停止は全てこのコード（決定論）／Codex呼び出しはコールバックで注入＝
#     テストはモックで決定論的に回す（「AIが何を返しても不正PASSしない」を担保）★
# ══════════════════════════════════════════════════════════════

DPASS = "PASS"
DR_RESEARCHABLE = "REVIEW_RESEARCHABLE"
DR_MANUAL = "REVIEW_MANUAL"
DR_CLAIM_DEF = "REVIEW_CLAIM_DEFINITION"
DR_SRC_CONFLICT = "REVIEW_SOURCE_CONFLICT"
DR_ANOMALY = "REVIEW_ANOMALY"                    # 決着値が異常検知に掛かった(bool/範囲外/桁移動・指摘2)
DBLOCKED = "BLOCKED_NO_EVIDENCE"
DBLOCKED_VERIFY = "BLOCKED_VERIFICATION_ERROR"   # 検証器が例外/取得不能で証拠を検証できず（指摘11）

MAX_EVIDENCE = 200          # 1ラウンドで処理する証拠の上限（暴走/DoSガード・Codex指摘6）
_RESERVED = ("verified", "c5_verdict", "c5_code", "official", "group")  # 検証器へ渡す前にevから剥奪


def _classify_failure(item_key: str, info: dict, scored: list,
                      incomplete: bool = False) -> str:
    """REVIEW の失敗を機械可読コードへ（往復の遷移と終端の決定に使う・決定論）。
    incomplete=検証未完了(検証器の例外/取得不能)。入力ゴミ(非dict等)はここに含めない。"""
    min_dom = MIN_DOMAINS.get(item_key, 2)
    if not info["any_verified"]:
        # 優先: 検証未完了 > claim定義混在(支配) > 裏取り皆無。
        if incomplete:
            return "verification_error"
        c5 = info["c5_counts"]
        hits = info.get("claim_def_hits", 0)
        # ★claim定義混在は REVIEW の過半(支配)を占め、PASSが皆無の時だけ（Codex指摘8）
        if hits > 0 and c5[PASS] == 0 and hits * 2 >= c5[REVIEW]:
            return "claim_def"
        return "no_evidence"
    if not scored:
        # verifiedはあるが全て ambiguous(1ドメイン複数値)で消えた＝評価が成立しない
        return "source_conflict"
    # ★公式(tier1)がambiguous＝公式が対立を隠している→自動決着させない（Codex指摘・3次）
    if any(classify_source(item_key, dk) == 1 for dk in info.get("ambiguous", ())):
        return "source_conflict"
    # ★公式(tier1)が複数値に割れている＝票数で決めず人手（Codex指摘・公式対立）
    if sum(1 for c in scored if c["has_official"]) >= 2:
        return "disputed"
    top = scored[0]
    # ★tier3(無名)のみ＝信頼源不足。大手/公式を探せば解ける見込み（research・シビル対策）
    if top["best_tier"] >= 3:
        return "low_tier"
    if len(scored) >= 2:
        # ★複数の値が残って競合＝独立ソース対立。ambiguousの有無に関わらず disputed（Codex指摘7）
        return "disputed"
    # 以下 scored はちょうど1値
    if not top["has_official"] and 0 < top["indep"] < min_dom:
        return "shortfall"   # 1値だが独立票が必要数に届かない＝research で足せる見込み
    return "disputed"


def _terminal(failure: str, round_no: int, max_rounds: int) -> str:
    """失敗コード→終端状態。★shortfallは往復を使い切ったら MANUAL・余地を残した打ち切りなら
    RESEARCHABLE（後日の自動再調査へ・設計書v1.3「2往復後はMANUAL」と整合・Codex指摘D）★"""
    # shortfall(独立票不足) と low_tier(tier3のみ＝大手/公式を探す) は「もっと調べれば解ける」型。
    if failure in ("shortfall", "low_tier"):
        return DR_MANUAL if round_no >= max_rounds else DR_RESEARCHABLE
    return {"no_evidence": DBLOCKED, "claim_def": DR_CLAIM_DEF,
            "source_conflict": DR_SRC_CONFLICT, "disputed": DR_MANUAL,
            "verification_error": DBLOCKED_VERIFY,
            None: DR_MANUAL}.get(failure, DR_MANUAL)


def _sanitize_evidence(items) -> tuple:
    """callback/seed の戻りを検証前にサニタイズ（dictのみ通す）。戻り (clean_list, had_bad)。
    ★不正戻り型（非list・非dict要素）を握りつぶさず had_bad で記録＝状態機械を必ず終端させる★"""
    if not isinstance(items, list):
        return [], True
    clean, bad = [], False
    for e in items:
        if isinstance(e, dict):
            clean.append(e)
        else:
            bad = True
    return clean, bad


def _strip_reserved(ev: dict) -> dict:
    """検証器へ渡す前に evidence から裏取り印の予約フィールドを剥奪（Codex指摘1）。
    検証器が誤って {**ev} を返しても自己申告が復活しないようにする。"""
    return {k: v for k, v in ev.items() if k not in _RESERVED}


def _verify_one(verify, item_key: str, ev: dict) -> tuple:
    """1証拠を検証器に通す（例外捕捉）。戻り (source|None, err_kind)。
    err_kind: None=正常 / "input"=入力不正(evが非dict) / "incomplete"=検証未完了(検証器欠如・
    例外・非dict戻り・値不一致)。★入力不正と検証未完了を分離＝検証未完了はPASSを止めるが、
    入力ゴミはDoSにしないため止めない（Codex指摘5次）★
    ★ev の予約フィールドを剥奪して渡す／verify結果の値が元evidenceの値と一致するか照合し、
      値は必ず evidence 側を採用（検証器は"検証するだけ"で値を書き換えられない・Codex指摘1）★
    ★domain は検証器が実アクセスした最終URL由来を採用（申告URLでない）★"""
    if not isinstance(ev, dict):
        return None, "input"
    if not callable(verify):
        return None, "incomplete"
    try:
        s = verify(item_key, _strip_reserved(ev))
    except Exception:
        return None, "incomplete"     # 取得失敗/例外＝この証拠は検証未完了（PASSを止める）
    if not isinstance(s, dict):
        return None, "incomplete"
    # ★検証器が別の値を返したら不整合＝その主張は未検証（架空値への書き換えを封じる・厳密一致）
    #   丸め比較でなく生値の厳密一致（650.00001 と 650.00004 を同一視しない・Codex指摘）
    if s.get("value") != ev.get("value"):
        return None, "incomplete"
    return {"value": ev.get("value"),   # 値は evidence 側で固定（検証器は書き換え不可）
            "domain": s.get("domain", ""),
            "unit": ev.get("unit"),      # ★unit取り違え検査用に保持（ceiling.gameに pt 等・Codex指摘）
            "verified": s.get("verified") is True,
            "c5_verdict": s.get("c5_verdict"),
            "c5_code": s.get("c5_code")}, None


def _group_sources(sources: list) -> list:
    """検証済みsourceを値ごとに束ねて candidates（B-3 resolveの入力形）にする。"""
    from collections import defaultdict
    by_val: dict = defaultdict(list)
    order = []
    for s in sources:
        nv = _norm_value(s.get("value"))
        key = nv if nv is not None else ("raw", str(s.get("value")))
        if key not in by_val:
            order.append(key)
        by_val[key].append(s)
    return [{"value": by_val[k][0].get("value"), "sources": by_val[k]} for k in order]


def _progress_set(info: dict) -> frozenset:
    """「利用可能な裏取り」の進展指標（Codex指摘9）: ambiguous除外後の (正規化値, ドメイン) ペア。
    NaN/bool/範囲外(=_norm_valueでNone)や ambiguous は既に除外済み。同一ドメインの別値連投・
    無意味証拠でラウンドを空回りさせないための、決定論の進展計測。"""
    amb = info.get("ambiguous", set())
    return frozenset((nv, dk)
                     for nv, doms in info.get("val_domains", {}).items()
                     for dk in doms if dk not in amb)


# ITEM_SPEC に範囲を持たない（＝範囲検査しない）が公開を許す項目のホワイトリスト。
# ★これ以外の未知item_key（typo含む）は自動公開しない（Codex指摘・任意キー素通り防止）★
ALLOWED_NON_SPEC_ITEMS = {"nerai"}   # 狙い目=当サイトの編集基準（計算値・範囲spec無し）


def _anomaly_gate(item_key: str, value, current) -> tuple:
    """★決着値の最終異常ゲート（bool/型/範囲/桁移動・未知項目を弾く・Codex指摘2/3次）★
    戻り (ok, reason)。ITEM_SPEC にあれば anomaly_check（PASS のみ通す）、
    ALLOWED_NON_SPEC_ITEMS(狙い目等)は「有限数値か」だけ保証。それ以外の未知項目は不可。
    決着(resolve PASS)してもこの関門を通らなければ自動公開しない（誤公開の最後の砦）。"""
    if item_key in ITEM_SPEC:
        av, fl = anomaly_check(item_key, value, current=current)
        return av == PASS, "; ".join(fl)
    if item_key in ALLOWED_NON_SPEC_ITEMS:
        num = _norm_value(value) is not None
        return num, ("" if num else "数値でない")
    return False, f"未知の項目 {item_key}（許可外・自動公開しない）"


def build_codex_question(item_key: str, candidates: list, round_no: int,
                         excluded, scored: list = None) -> dict:
    """★構造化再質問（自由文でなく構造で渡す・設計書v1.3）★
    Codexに「不足独立ドメイン数・対立中の値・公式優先・条件quote必須・除外済みドメイン」を明示。"""
    info = _score(item_key, candidates)
    if scored is None:
        scored = info["scored"]
    min_dom = MIN_DOMAINS.get(item_key, 2)
    top = scored[0] if scored else None
    need = max(0, min_dom - top["indep"]) if top else min_dom
    have_domains = sorted({dk for (_nv, dk) in _progress_set(info)})
    return {
        "item_key": item_key,
        "round": round_no,
        "candidate_values": [_norm_value(c.get("value")) for c in candidates],
        "verified_independent_domains": have_domains,
        "need_independent_domains": need,
        # 票は足りていても信頼ソース同士が対立(DISPUTED)の時に格差をつける追加裏取りが欲しい
        "conflicting_values": [c["value"] for c in scored] if len(scored) > 1 else [],
        "official_preferred": item_key != "nerai",   # 狙い目はスロパチ基準・他は公式優先
        "condition_quote_required": True,
        "excluded_domains": sorted(excluded or []),
        "instructions": ("値・単位・条件(通常時/リセット/scope)を同一文に含む逐語quoteと"
                         "取得元URLを各値に必須。転載まとめ/出典不明/日付不明/AI生成は不可。"),
    }


def resolve_with_dialogue(item_key: str, seed_evidence, verify, ask_codex=None,
                          research=None, max_rounds: int = 2, current=None) -> dict:
    """★C: 話し合いループ本体（決定論の状態機械）★（Codexレビュー2巡反映・2026-07-19）
    ★入力は"生の証拠(evidence)"＝{value,url/domain,quote,mode,scope,unit,operator,...}。
      裏取り印(verified)は verify(検証器)だけが付与でき、seed/callbackの'verified'申告は
      一切信用しない（指摘1）。検証器の戻り値も value一致照合・スキーマ検査で信用しすぎない★
    verify(item_key, evidence) -> {value, domain(実アクセスした最終URL由来), verified:bool,
      c5_verdict:PASS/REJECT/REVIEW, c5_code} を返す決定論検証器（DでC5＋実ページ再取得を実装）。
    current: 当該項目の現行公開値（anomaly の前回比較に使う・任意）。
    各ラウンド: pending証拠を検証→値ごとに束ね→resolve→PASSなら【異常ゲート】→終了 /
      REVIEWなら失敗分類→「上限到達 or 進展なし」で終端／それ以外は研究/第二AIで生証拠を集め次へ。
    ★進展判定は「利用可能な(値,ドメイン)ペアが増えたか」（無意味証拠で空回りさせない・指摘9）。
      決着してもboolや範囲外・桁移動は _anomaly_gate で止める（指摘2）。終端は必ずマージ後に分類（指摘7）★
    戻り値 dict: {state,value,reason,rounds,transcript,question,tried_domains}。
    ★verify_claims 合格の最終確認は呼び出し側（Dフェーズ配線）で必須★"""
    if not (isinstance(max_rounds, int) and 0 <= max_rounds <= 5):
        raise ValueError("max_rounds は 0〜5 の整数（暴走ガード・Codex指摘5）")
    transcript = []
    sources: list = []
    tried: set = set()
    prev_progress = None
    last_question = None
    round_no = 0
    truncated = False          # 証拠が上限超過で切り捨てられたか（対立を見落とす可能性・指摘3次）
    verify_incomplete = False  # 検証器の例外/取得不能（PASSを止める・入力ゴミとは別・指摘5次）
    input_bad = False          # 入力スキーマ違反(非dict等)。DoS回避のためPASSは止めない
    # seed=None は空listと同じ「証拠なし」に統一（指摘10）
    pending, input_bad = _sanitize_evidence(seed_evidence if seed_evidence is not None else [])
    while True:
        # 1) pending証拠を検証器に通す（★予約フィールド剥奪・値照合は _verify_one 内／暴走ガード）
        if len(pending) > MAX_EVIDENCE:
            pending = pending[:MAX_EVIDENCE]
            truncated = True
        for ev in pending:
            s, kind = _verify_one(verify, item_key, ev)
            if kind == "incomplete":
                verify_incomplete = True
            elif kind == "input":
                input_bad = True
            if s is not None:
                sources.append(s)
                dk = _domain_key(s.get("domain", ""))
                if dk:
                    tried.add(dk)   # ★検証器が実アクセスした最終URLのdomainだけ除外に積む（指摘11）
        # 2) 値ごとに束ねて決着（★マージ後の状態で分類＝指摘7）
        candidates = _group_sources(sources)
        verdict, value, reason, scored, info = resolve(item_key, candidates)
        cur_progress = _progress_set(info)
        if verdict == PASS:
            # ★証拠が上限で切り捨てられた回は、隠れた出典対立の可能性→自動公開しない（指摘3次）
            if truncated:
                transcript.append({"round": round_no, "verdict": verdict, "value": value,
                                   "reason": reason, "failure": "truncated_batch",
                                   "scored": scored, "verified_pairs": sorted(cur_progress)})
                return {"state": DR_MANUAL, "value": None,
                        "reason": "証拠が上限超過で切り捨てられ出典対立を見落とした可能性（人手）",
                        "rounds": round_no, "transcript": transcript,
                        "question": last_question, "tried_domains": sorted(tried)}
            # ★一部の証拠が検証未完了(取得失敗/例外)なら、公式対立候補を見逃した可能性→止める（指摘5次）
            if verify_incomplete:
                transcript.append({"round": round_no, "verdict": verdict, "value": value,
                                   "reason": reason, "failure": "verification_incomplete",
                                   "scored": scored, "verified_pairs": sorted(cur_progress)})
                return {"state": DBLOCKED_VERIFY, "value": None,
                        "reason": "一部の証拠が検証未完了（取得失敗/例外）＝対立候補を見逃した可能性",
                        "rounds": round_no, "transcript": transcript,
                        "question": last_question, "tried_domains": sorted(tried)}
            # ★決着しても最終の異常ゲート（bool/型/範囲/桁移動/未知項目）を通す（指摘2/3次）
            ok, areason = _anomaly_gate(item_key, value, current)
            transcript.append({"round": round_no, "verdict": verdict, "value": value,
                               "reason": reason, "failure": None if ok else "anomaly",
                               "scored": scored, "verified_pairs": sorted(cur_progress)})
            return {"state": DPASS if ok else DR_ANOMALY,
                    "value": value if ok else None,
                    "reason": reason if ok else f"決着したが異常検知で保留: {areason}",
                    "rounds": round_no, "transcript": transcript,
                    "question": last_question, "tried_domains": sorted(tried)}
        failure = _classify_failure(item_key, info, scored, verify_incomplete)
        transcript.append({"round": round_no, "verdict": verdict, "value": value,
                           "reason": reason, "failure": failure, "scored": scored,
                           "verified_pairs": sorted(cur_progress)})
        # 3) 終端判定: 往復を使い切った or 進展(利用可能な裏取りペア増)なし（指摘7/9）
        stalled = (prev_progress is not None and cur_progress == prev_progress)
        if round_no >= max_rounds or stalled:
            note = "（再調査で新しい裏取りが得られず）" if stalled else ""
            return {"state": _terminal(failure, round_no, max_rounds), "value": None,
                    "reason": reason + note, "rounds": round_no,
                    "transcript": transcript, "question": last_question,
                    "tried_domains": sorted(tried)}
        # 4) まだ往復できる → 構造化質問で"生証拠"を集める（研究→第二AIの順）
        q = build_codex_question(item_key, candidates, round_no + 1, tried, scored)
        last_question = q
        prev_progress = cur_progress
        pending = []
        for cb in (research, ask_codex):
            if not cb:
                continue
            try:
                got = cb(q)
            except Exception:
                input_bad = True   # 追加収集の失敗（PASSは止めない・進展なしで自然終端）
                continue
            clean, bad = _sanitize_evidence(got if got is not None else [])
            input_bad = input_bad or bad
            pending += clean
        round_no += 1


def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, cond))
        print(("OK " if cond else "NG ") + name)

    def C(*a, **k):
        return check_claim_identity(*a, **k)[0]  # verdict のみ

    kaguya = "ボーナス間は最大1100G+αで当選。設定変更後は800G+α、REGは900G+αへ短縮される。"
    reset_q = "通常時の天井は1268G。設定変更後（リセット）は800Gに短縮。"
    scope_q = "CZ間は最大800G、AT間は1500Gが天井。"

    # 正しくPASSすべき
    t("通常時1000G・素直な文 → PASS",
      C("天井", "normal", None, 1000, "G", "通常時の天井は1000Gで当選します") == PASS)
    t("kaguya型: ボーナス間=1100G → PASS",
      C("天井", "normal", "ボーナス間", 1100, "G", kaguya) == PASS)
    t("リセット800Gをリセットとして拾う → PASS",
      C("天井", "reset", None, 800, "G", reset_q) == PASS)
    t("AT間1500GをAT間として拾う → PASS",
      C("天井", "normal", "AT間", 1500, "G", scope_q) == PASS)
    t("通常時のボーナス間天井 → PASS",
      C("天井", "normal", "ボーナス間", 1100, "G", "ボーナス間の天井は最大1100Gまで") == PASS)
    t("多義: 通常時1500Gは PASS",
      C("天井", "normal", None, 1500, "G",
        "通常時の天井は1500G、リセット時は1500Gではなく800Gに短縮。") == PASS)
    t("カンマ表記 1,000G → PASS",
      C("天井", "normal", None, 1000, "G", "通常時の天井は1,000Gに到達") == PASS)

    # 明確にREJECTすべき（誤公開を防ぐ核心）
    t("kaguya型: 通常時=900G(実はREG後短縮) → REJECT",
      C("天井", "normal", None, 900, "G", kaguya) == REJECT)
    t("リセット800Gを通常時として拾う → REJECT",
      C("天井", "normal", None, 800, "G", reset_q) == REJECT)
    t("AT間1500GをCZ間として拾う → REJECT",
      C("天井", "normal", "CZ間", 1500, "G", scope_q) == REJECT)
    t("REG後900Gをリセットとして拾わない → REJECT",
      C("天井", "reset", None, 900, "G", "REG後は900Gへ短縮") == REJECT)
    t("否定文『1500Gではなく』の1500を拾わない → REJECT",
      C("天井", "normal", None, 1500, "G", "天井は1500Gではなく実際は1200G") == REJECT)
    t("短縮量『300G短縮』を天井値にしない → REJECT",
      C("天井", "reset", None, 300, "G", "設定変更後は300G短縮される") == REJECT)
    t("狙い目の数字を天井にしない → REJECT",
      C("天井", "normal", None, 800, "G", "通常時の狙い目は800G消化から期待") == REJECT)
    t("値がquoteに無い → REJECT",
      C("天井", "normal", None, 1234, "G", "通常時の天井は1000G") == REJECT)
    t("operator=maxだが到達確定表現 → REJECT",
      C("天井", "normal", "CZ間", 800, "G", "CZ間は800G到達で当選", operator="max") == REJECT)

    # 判断保留 REVIEW（fail-closed＝自動採用しない）
    t("推定・可能性は確定値にしない → REVIEW",
      C("天井", "normal", None, 1000, "G", "天井はおそらく1000G前後と推定される") == REVIEW)
    t("天井らしさが無い(材料不足) → REVIEW",
      C("天井", "normal", None, 1000, "G", "通常時のデータは1000Gあたりが目安") == REVIEW)
    t("scopeが文脈で確認できない → REVIEW",
      C("天井", "normal", "AT間", 1000, "G", "天井は最大1000Gまで") == REVIEW)

    # ★Codex第2次レビューの反例（v4で対応）★
    t("『300G短くなる』(短縮の別表現) → REJECT",
      C("天井", "reset", None, 300, "G", "設定変更後は天井が300G短くなる") == REJECT)
    t("『RB後は900Gが天井』を通常時にしない → REJECT",
      C("天井", "normal", None, 900, "G", "RB後は900Gが天井") == REJECT)
    t("『まで』だけで天井扱いしない(実戦記録) → REVIEW",
      C("天井", "normal", None, 1000, "G", "通常時は1000Gまで回して様子を見る") == REVIEW)
    t("★実バグ★ 小数値で『。』を跨いで隣文を拾わない → PASSしない",
      C("天井", "normal", None, 1000.0, "G", "1000。通常時の天井は1200G") != PASS)
    t("訂正/旧情報の文脈は確定値にしない → REVIEW",
      C("天井", "normal", None, 1000, "G", "旧情報では1000Gだったが正しくは1200G") == REVIEW)
    t("同一文脈に複数mode → REVIEW",
      C("天井", "normal", None, 1000, "G", "通常時とリセットで天井は1000G") == REVIEW)

    # ── B-2 異常検知 ──────────────────────────────
    A = anomaly_check
    t("★機械割 97.8→107.8（範囲内だが大変化）→ REVIEW",
      A("kikaiwari", 107.8, current=97.8)[0] == REVIEW)
    t("機械割 97.8→97.9（正常な微変化）→ PASS",
      A("kikaiwari", 97.9, current=97.8)[0] == PASS)
    t("機械割 300%（物理的に不可能）→ REJECT", A("kikaiwari", 300)[0] == REJECT)
    t("機械割 小数桁過多 97.85 → REJECT", A("kikaiwari", 97.85)[0] == REJECT)
    t("天井 1268→12680（桁移動）→ REVIEW",
      A("ceiling.game", 12680, current=1268)[0] in (REVIEW, REJECT))  # 12680は範囲外→REJECTでも可
    t("天井 1268→126.8（小数点移動）→ REVIEW",
      A("ceiling.game", 126.8, current=1268)[0] in (REVIEW, REJECT))
    t("天井 1268→1258（下位桁の微修正10G）→ PASS",
      A("ceiling.game", 1258, current=1268)[0] == PASS)
    t("天井 1268→2268（高位桁の打ち間違い）→ REVIEW",
      A("ceiling.game", 2268, current=1268)[0] == REVIEW)
    t("天井 1268→1280（正常な微修正）→ PASS",
      A("ceiling.game", 1280, current=1268)[0] == PASS)
    t("天井 整数項目に小数 1268.5 → REJECT", A("ceiling.game", 1268.5)[0] == REJECT)
    t("スルー天井 7回（正常）→ PASS", A("ceiling.through", 7)[0] == PASS)
    t("スルー天井 99回（不可能）→ REJECT", A("ceiling.through", 99)[0] == REJECT)
    # 設定配列: 単調でない/同一値は REVIEW、範囲外は REJECT
    t("機械割配列 単調増加 → PASS",
      anomaly_check_setting_array("kikaiwari",
                                  {1: 97.8, 2: 98.5, 5: 105.0, 6: 110.0})[0] == PASS)
    t("機械割配列 設定6が設定1より低い（単調崩れ）→ REVIEW",
      anomaly_check_setting_array("kikaiwari",
                                  {1: 105.0, 2: 103.0, 6: 98.0})[0] == REVIEW)
    # ★Codex第2次: 設定配列に前回値を渡し、一括+10化けを検知★
    t("★設定配列 全設定が前回から+10化け → REVIEW",
      anomaly_check_setting_array(
          "kikaiwari", {1: 107.8, 2: 108.0, 5: 115.0, 6: 118.0},
          current={1: 97.8, 2: 98.0, 5: 105.0, 6: 108.0})[0] == REVIEW)
    t("設定配列 前回値と整合（微変化）→ PASS",
      anomaly_check_setting_array(
          "kikaiwari", {1: 97.9, 2: 98.1, 5: 105.1, 6: 108.2},
          current={1: 97.8, 2: 98.0, 5: 105.0, 6: 108.0})[0] == PASS)
    t("真偽値 True は数値でない → REJECT", A("ceiling.through", True)[0] == REJECT)
    t("単位取り違え ceiling.game=1000pt → REJECT",
      A("ceiling.game", 1000, unit="pt")[0] == REJECT)
    t("境界ちょうど 100.0→103.0(diff=jump_abs) → REVIEW",
      A("kikaiwari", 103.0, current=100.0)[0] == REVIEW)

    # ★mutation test★: gold値の"意味のある変異"（桁移動/大跳ね/高位桁置換）は必ず非PASS。
    # 下1桁の微差は正常変化なので変異に含めない（それはPASSでよい）。
    GOLD = [("ceiling.game", 1268), ("ceiling.game", 800), ("kikaiwari", 97.8),
            ("kikaiwari", 110.0), ("ceiling.point", 1000), ("ceiling.through", 6)]
    mut_ok = True
    for item, good in GOLD:
        if A(item, good, current=good)[0] != PASS:
            mut_ok = False
            print(f"   NG 原値がPASSでない: {item} {good}")
        g = float(good)
        muts = {g * 10, g / 10.0, g + ITEM_SPEC[item]["jump_abs"] * 2}  # 桁移動＋大跳ね（必ず異常）
        for mv in muts:
            if abs(mv - g) < 1e-9:
                continue
            if A(item, mv, current=good)[0] == PASS:
                mut_ok = False
                print(f"   NG 変異値がPASSされた: {item} {good}→{mv}")
    t("mutation test: gold変異(桁移動/大跳ね/高位桁置換)は全て非PASS・原値PASS", mut_ok)

    # ── B-3 出典の格による決着 ──────────────────────
    def src(dom, verified=True, c5="PASS", **kw):
        return {"domain": dom, "verified": verified, "c5_verdict": c5, **kw}

    R = resolve
    v, val = R("ceiling.game", [{"value": 1268, "sources": [
        src("chonborista.com"), src("1geki.jp")]}])[:2]
    t("1値・大手2独立 → PASS(1268)", v == PASS and val == 1268)
    t("1値・大手1のみ(要2) → REVIEW",
      R("ceiling.game", [{"value": 1268, "sources": [src("chonborista.com")]}])[0] == REVIEW)
    v, val = R("kikaiwari", [{"value": 108.0, "sources": [src("kitadenshi.jp")]}])[:2]
    t("1値・公式1(許可リスト) → PASS(108.0)", v == PASS and val == 108.0)
    v, val = R("kikaiwari", [
        {"value": 108.0, "sources": [src("sammy.co.jp")]},
        {"value": 110.0, "sources": [src("chonborista.com"), src("1geki.jp")]}])[:2]
    t("2値・公式108 vs 大手110 → PASS(公式108)", v == PASS and val == 108.0)
    t("2値・同格2独立ずつ → DISPUTED REVIEW",
      R("ceiling.game", [
          {"value": 1268, "sources": [src("chonborista.com"), src("nana-press.com")]},
          {"value": 1300, "sources": [src("1geki.jp"), src("dmm.com")]}])[0] == REVIEW)
    v, val = R("ceiling.game", [
        {"value": 1268, "sources": [src("chonborista.com"), src("nana-press.com"), src("1geki.jp")]},
        {"value": 1300, "sources": [src("dmm.com")]}])[:2]
    t("2値・3独立 vs 1独立 → PASS(多い方1268)", v == PASS and val == 1268)
    t("裏取り0の候補のみ → REVIEW",
      R("ceiling.game", [{"value": 1268, "sources": [src("blog.x", verified=False)]}])[0] == REVIEW)
    v, val = R("nerai", [{"value": 650, "sources": [src("slopachi-quest.com")]}])[:2]
    t("狙い目・スロパチ1つ → PASS(650)", v == PASS and val == 650)
    # ★Codex偽装対策★
    t("group水増し: 同一ドメインをfake groupで2票化しても独立1票 → REVIEW",
      R("ceiling.game", [{"value": 1268, "sources": [
          src("chonborista.com", group="A"), src("www.chonborista.com", group="B")]}])[0] == REVIEW)
    t("official偽装: ブログをofficial=True申告しても無視 → REVIEW",
      R("ceiling.game", [{"value": 1268, "sources": [src("myblog.example", official=True)]}])[0] == REVIEW)
    v, val = R("ceiling.game", [
        {"value": 1000, "sources": [src("chonborista.com")]},
        {"value": 1000.0, "sources": [src("1geki.jp")]}])[:2]
    t("同一値未統合対策: 1000と1000.0を統合し独立2 → PASS(1000)",
      v == PASS and val in (1000, 1000.0))
    t("1ドメインが複数値を支持→そのドメイン除外で裏取り不足 → REVIEW",
      R("ceiling.game", [
          {"value": 1000, "sources": [src("chonborista.com"), src("nana-press.com")]},
          {"value": 1300, "sources": [src("chonborista.com")]}])[0] == REVIEW)
    # ★Codex指摘2: verified="false"(文字列truthy)は票にしない
    t("★verified=\"false\"文字列は票にしない → REVIEW",
      R("ceiling.game", [{"value": 999, "sources": [
          {"domain": "a.example", "verified": "false", "c5_verdict": PASS},
          {"domain": "b.example", "verified": "false", "c5_verdict": PASS}]}])[0] == REVIEW)
    # ★Codex指摘5: verified=True でも c5_verdict!=PASS は票にしない
    t("★verified=True かつ c5=REJECTは票にしない → REVIEW",
      R("ceiling.game", [{"value": 999, "sources": [
          src("chonborista.com", c5="REJECT"), src("1geki.jp", c5="REJECT")]}])[0] == REVIEW)
    # ★Codex指摘3: userinfo偽装URLは丸ごと拒否（空key）＝公式にも独立票にもしない
    t("★userinfo偽装URLは丸ごと空keyで拒否・公式化しない",
      _domain_key("https://sammy.co.jp:443@evil.example/p") == ""
      and classify_source("kikaiwari", "https://sammy.co.jp@evil.example") == 3)
    t("★IP/単一ラベルは独立票にしない(空key)",
      _domain_key("http://192.168.0.1/x") == "" and _domain_key("localhost") == "")
    t("正常 www.付き→eTLD+1 / co.jp3ラベル保持",
      _domain_key("https://www.chonborista.com/page") == "chonborista.com"
      and _domain_key("https://sammy.co.jp/x") == "sammy.co.jp")
    # ★Codex指摘11: 値NaNは統合キーにならず票にしない
    t("★値NaNは票にしない → REVIEW",
      R("ceiling.game", [{"value": float("nan"), "sources": [
          src("chonborista.com"), src("1geki.jp")]}])[0] == REVIEW)

    # ── C 話し合いループ（生証拠＋検証器モック注入・Codexレビュー全反映） ──────
    D = resolve_with_dialogue

    def ev(value, domain, verdict="PASS", code="OK", **kw):
        """生の証拠。_verdict/_code は"検証器がこの証拠をどう判定するか"を仕込む
        （＝callback自身の自己申告verifiedではない）。"""
        return {"value": value, "domain": domain, "_verdict": verdict, "_code": code, **kw}

    def verify_mock(item_key, e):
        """検証器モック。生証拠eを"検証した結果"(B-3のsource形)で返す。"""
        vd = e.get("_verdict", "REJECT")
        return {"value": e.get("value"), "domain": e.get("domain", ""),
                "verified": vd == "PASS", "c5_verdict": vd, "c5_code": e.get("_code")}

    # 初回2独立で即PASS
    r = D("ceiling.game", [ev(1268, "chonborista.com"), ev(1268, "1geki.jp")], verify_mock)
    t("C: 初回2独立で即PASS", r["state"] == DPASS and r["value"] == 1268)
    # ★指摘1核心: callbackが verified/c5 を自己申告しても検証器がREJECTなら票にならない
    r = D("ceiling.game", [ev(1268, "blog.invalid", "REJECT")], verify_mock,
          ask_codex=lambda q: [{"value": 999, "domain": "sammy.co.jp",
                                "verified": True, "c5_verdict": "PASS"}])
    t("C: ★callback自己申告verifiedを無視→架空値は非PASS(BLOCKED)",
      r["state"] == DBLOCKED and r["value"] is None)
    # 往復でask_codexの"生証拠"が検証を通りPASS
    r = D("ceiling.game", [ev(1268, "chonborista.com")], verify_mock,
          ask_codex=lambda q: [ev(1268, "1geki.jp")])
    t("C: 往復でask_codexの生証拠が検証を通りPASS",
      r["state"] == DPASS and r["value"] == 1268 and r["rounds"] == 1)
    # 独立不足・往復で増えず（余地あり）→ REVIEW_RESEARCHABLE
    r = D("ceiling.game", [ev(1268, "chonborista.com")], verify_mock, ask_codex=lambda q: [])
    t("C: 独立不足・往復で増えず → REVIEW_RESEARCHABLE", r["state"] == DR_RESEARCHABLE)
    # 独立不足・往復を使い切り(max_rounds=1) → REVIEW_MANUAL（指摘D）
    r = D("ceiling.game", [ev(1268, "chonborista.com")], verify_mock,
          ask_codex=lambda q: [], max_rounds=1)
    t("C: shortfall・往復上限到達 → REVIEW_MANUAL(指摘D)", r["state"] == DR_MANUAL)
    # 裏取り皆無（検証器REJECT・エラーなし）→ BLOCKED_NO_EVIDENCE
    r = D("ceiling.game", [ev(1268, "blog.invalid", "REJECT")], verify_mock,
          ask_codex=lambda q: [])
    t("C: 裏取り皆無 → BLOCKED_NO_EVIDENCE", r["state"] == DBLOCKED)
    # C5条件混在(MODE_SCOPE_AMBIGUOUS)支配 → REVIEW_CLAIM_DEFINITION
    r = D("ceiling.game", [ev(1268, "chonborista.com", "REVIEW", "MODE_SCOPE_AMBIGUOUS")],
          verify_mock, ask_codex=lambda q: [])
    t("C: C5条件混在支配 → REVIEW_CLAIM_DEFINITION", r["state"] == DR_CLAIM_DEF)
    # ★指摘6: 一般REVIEW(quote空等)はclaim_defにせず no_evidence 扱い
    r = D("ceiling.game", [ev(1268, "chonborista.com", "REVIEW", "MISSING_QUOTE")],
          verify_mock, ask_codex=lambda q: [])
    t("C: ★一般REVIEWはclaim_defにせず → BLOCKED(指摘6)", r["state"] == DBLOCKED)
    # 1ドメインが複数値 → REVIEW_SOURCE_CONFLICT
    r = D("ceiling.game", [ev(1000, "chonborista.com"), ev(1300, "chonborista.com")],
          verify_mock, ask_codex=lambda q: [])
    t("C: 1ドメイン複数値 → REVIEW_SOURCE_CONFLICT", r["state"] == DR_SRC_CONFLICT)
    # ★指摘4: 独立1票ずつの別値対立(大手tier2)は shortfall でなく disputed → REVIEW_MANUAL
    r = D("ceiling.game", [ev(1000, "chonborista.com"), ev(1300, "1geki.jp")],
          verify_mock, ask_codex=lambda q: [])
    t("C: ★大手1票ずつの別値対立 → REVIEW_MANUAL(disputed・指摘4)", r["state"] == DR_MANUAL)
    # 同格2独立ずつの対立 → REVIEW_MANUAL
    r = D("ceiling.game", [ev(1268, "chonborista.com"), ev(1268, "nana-press.com"),
                           ev(1300, "1geki.jp"), ev(1300, "dmm.com")],
          verify_mock, ask_codex=lambda q: [])
    t("C: DISPUTED同格同数 → REVIEW_MANUAL", r["state"] == DR_MANUAL)
    # research(Claude)が生証拠を足しPASS
    r = D("kikaiwari", [ev(108.0, "chonborista.com")], verify_mock,
          research=lambda q: [ev(108.0, "1geki.jp")])
    t("C: research(Claude)が生証拠を足しPASS", r["state"] == DPASS and r["rounds"] == 1)
    # ★指摘11: 検証器が全例外 → BLOCKED_VERIFICATION_ERROR（クラッシュしない）
    def vboom(item_key, e):
        raise RuntimeError("verifier crash")
    r = D("ceiling.game", [ev(1268, "chonborista.com")], vboom, ask_codex=lambda q: [])
    t("C: ★検証器が全例外 → BLOCKED_VERIFICATION_ERROR(指摘11)",
      r["state"] == DBLOCKED_VERIFY)
    # ★指摘11: ask_codex例外でもクラッシュせず非PASS終端
    def boom(q):
        raise RuntimeError("codex down")
    r = D("ceiling.game", [ev(1268, "chonborista.com")], verify_mock, ask_codex=boom)
    t("C: ★ask_codex例外でもクラッシュせず非PASS終端(指摘11)", r["state"] != DPASS)
    # ★指摘11: ask_codexが非list戻り → 握りつぶさず非PASS終端
    r = D("ceiling.game", [ev(1268, "chonborista.com")], verify_mock,
          ask_codex=lambda q: {"not": "a list"})
    t("C: ★ask_codex非list戻り → 非PASS終端(指摘11)", r["state"] != DPASS)
    # 構造化再質問の中身（不足独立数・除外・候補値・公式優先）
    q = build_codex_question(
        "ceiling.game",
        _group_sources([{"value": 1268, "domain": "chonborista.com",
                         "verified": True, "c5_verdict": PASS}]),
        1, {"chonborista.com"})
    t("C: 再質問が構造化（不足独立数=1・公式優先・quote必須・除外反映）",
      q["need_independent_domains"] == 1 and q["official_preferred"] is True
      and q["condition_quote_required"] is True
      and q["excluded_domains"] == ["chonborista.com"])
    t("C: 再質問はitem_key/round/候補値を含む",
      q["item_key"] == "ceiling.game" and q["round"] == 1
      and q["candidate_values"] == [1268])
    t("C: 狙い目は公式優先しない（official_preferred=False）",
      build_codex_question("nerai", [], 1, set())["official_preferred"] is False)
    # ★指摘10: 試したドメインが tried_domains に載る（除外の更新）
    r = D("ceiling.game", [ev(1268, "blog.invalid", "REJECT")], verify_mock,
          ask_codex=lambda q: [])
    t("C: ★試したドメインがtried_domainsに載る(指摘10)",
      "blog.invalid" in r["tried_domains"])
    # 往復上限のガード（max_rounds=2で3回目のask_codexを呼ばない・毎回別値=決着しない）
    calls = {"n": 0}

    def _greedy(q):
        calls["n"] += 1
        return [ev(2000 + calls["n"], f"d{calls['n']}.example")]
    r = D("ceiling.game", [ev(1268, "chonborista.com")], verify_mock,
          ask_codex=_greedy, max_rounds=2)
    t("C: 往復上限2で停止（3回目のask_codexを呼ばない）",
      r["rounds"] == 2 and calls["n"] == 2)
    # ★C5 code（指摘6の土台・3要素化）
    t("C5 code: 複数mode → MODE_SCOPE_AMBIGUOUS",
      check_claim_identity("天井", "normal", None, 1000, "G",
                           "通常時とリセットで天井は1000G")[2] == "MODE_SCOPE_AMBIGUOUS")
    t("C5 code: quote空 → MISSING_QUOTE",
      check_claim_identity("天井", "normal", None, 1000, "G", "")[2] == "MISSING_QUOTE")
    t("C5 code: 素直な通常時 → OK",
      check_claim_identity("天井", "normal", None, 1000, "G",
                           "通常時の天井は1000Gに到達")[2] == "OK")

    # ── Codex 2次レビュー対応の追加反例（Critical/High/Medium） ──────
    # ★C1: 検証器が別の値を返す(架空値書換え) → 値照合で弾き非PASS
    def broken_verify(item_key, e):
        return {"value": 999, "domain": "sammy.co.jp", "verified": True,
                "c5_verdict": "PASS", "c5_code": "OK"}
    r = D("ceiling.game", [ev(1268, "chonborista.com")], broken_verify)
    t("C: ★検証器が別値を返す→値照合で弾き非PASS(C1)",
      r["state"] != DPASS and r["value"] is None)
    # ★C1: 予約フィールドは検証器へ渡す前に剥奪される
    seen_keys = {}

    def spy_verify(item_key, e):
        seen_keys["k"] = set(e.keys())
        return verify_mock(item_key, e)
    D("ceiling.game", [ev(1268, "chonborista.com", verified=True, c5_verdict="PASS")],
      spy_verify, ask_codex=lambda q: [])
    t("C: ★予約フィールド(verified/c5_verdict)は検証器に渡らない(C1)",
      "verified" not in seen_keys["k"] and "c5_verdict" not in seen_keys["k"])
    # ★C2: 決着しても範囲外値は最終異常ゲートで REVIEW_ANOMALY
    r = D("ceiling.game", [ev(9999, "chonborista.com"), ev(9999, "1geki.jp")], verify_mock)
    t("C: ★決着値が範囲外 → REVIEW_ANOMALY(C2)", r["state"] == DR_ANOMALY)
    # ★C2: 決着値が真偽値 → 票にならず非PASS
    r = D("ceiling.through", [ev(True, "a.example"), ev(True, "b.example")], verify_mock)
    t("C: ★決着値がbool → 非PASS(C2)", r["state"] != DPASS)
    # ★C3: 連続ドット/userinfo偽装は公式昇格しない
    t("C: ★連続ドット a..sammy.co.jp → 空key(C3)",
      _domain_key("https://a..sammy.co.jp/x") == "")
    r = D("kikaiwari", [ev(999.0, "https://sammy.co.jp@evil1.example/x"),
                        ev(999.0, "https://sammy.co.jp@evil2.example/x")], verify_mock)
    t("C: ★userinfo偽装2票でも公式化せず非PASS(C3)", r["state"] != DPASS)
    # ★High4: 検証器の型崩れ戻り(domain非str/c5_verdict=[])でクラッシュしない
    def junk_verify(item_key, e):
        return {"value": e.get("value"), "domain": 123, "verified": True,
                "c5_verdict": [], "c5_code": None}
    r = D("ceiling.game", [ev(1268, "chonborista.com")], junk_verify, ask_codex=lambda q: [])
    t("C: ★検証器の型崩れ戻りでクラッシュせず非PASS(High4)", r["state"] != DPASS)
    # ★High5: max_rounds不正(inf)は即エラー
    inf_err = False
    try:
        D("ceiling.game", [ev(1268, "chonborista.com")], verify_mock, max_rounds=float("inf"))
    except ValueError:
        inf_err = True
    t("C: ★max_rounds=inf は ValueError(High5)", inf_err)
    # ★M7: ambiguous除外後に大手が独立1票ずつ対立 → disputed(MANUAL)
    r = D("ceiling.game", [ev(1000, "chonborista.com"), ev(1000, "nana-press.com"),
                           ev(1300, "1geki.jp"), ev(1300, "nana-press.com")],
          verify_mock, ask_codex=lambda q: [])
    t("C: ★ambiguous除外後の大手独立対立 → REVIEW_MANUAL(M7)", r["state"] == DR_MANUAL)
    # ★M8: claim混在が非支配(1/3) → claim_defにせず BLOCKED
    r = D("ceiling.game",
          [ev(1268, "a.example", "REVIEW", "MODE_SCOPE_AMBIGUOUS"),
           ev(1268, "b.example", "REVIEW", "MISSING_QUOTE"),
           ev(1268, "c.example", "REVIEW", "MISSING_QUOTE")],
          verify_mock, ask_codex=lambda q: [])
    t("C: ★claim混在が非支配(1/3) → BLOCKED(M8)", r["state"] == DBLOCKED)
    # ★M8: claim混在が支配(2/3) → REVIEW_CLAIM_DEFINITION
    r = D("ceiling.game",
          [ev(1268, "a.example", "REVIEW", "MODE_SCOPE_AMBIGUOUS"),
           ev(1300, "b.example", "REVIEW", "MODE_SCOPE_AMBIGUOUS"),
           ev(1400, "c.example", "REVIEW", "MISSING_QUOTE")],
          verify_mock, ask_codex=lambda q: [])
    t("C: ★claim混在が支配(2/3) → REVIEW_CLAIM_DEFINITION(M8)", r["state"] == DR_CLAIM_DEF)
    # ★M10: seed=None は空listと同じ BLOCKED_NO_EVIDENCE
    t("C: ★seed=None は空扱いで BLOCKED_NO_EVIDENCE(M10)",
      D("ceiling.game", None, verify_mock)["state"] == DBLOCKED)

    # ── Codex 3次レビュー対応の追加反例（シビル/公式対立/型契約/巨大current/C5意味） ──────
    # ★Critical(3次): 無名(tier3)2ドメインだけの合意は信用しない（シビル攻撃）→ 非PASS
    r = D("ceiling.game", [ev(1268, "attacker-a.example"), ev(1268, "attacker-b.example")],
          verify_mock, ask_codex=lambda q: [])
    t("C: ★tier3二ドメインのシビル合意 → 非PASS(RESEARCHABLE)",
      r["state"] == DR_RESEARCHABLE and r["value"] is None)
    # ★大手(tier2)が最低1つ入れば信頼できる（tier3補助票と合わせて）→ PASS
    r = D("ceiling.game", [ev(1268, "chonborista.com"), ev(1268, "attacker-a.example")],
          verify_mock)
    t("C: tier2+tier3の2独立 → PASS（大手が最低1つ）",
      r["state"] == DPASS and r["value"] == 1268)
    # ★High(3次): 公式(tier1)同士の対立は低tier票の多数決で決着しない → REVIEW_MANUAL
    r = D("kikaiwari", [ev(108.0, "sammy.co.jp"), ev(108.0, "chonborista.com"),
                        ev(110.0, "kitadenshi.jp")], verify_mock, ask_codex=lambda q: [])
    t("C: ★公式(tier1)対立は低tier票で決着しない → REVIEW_MANUAL", r["state"] == DR_MANUAL)
    # ★Medium(3次): 数値文字列 "1268" は数値型契約で票にならない → 非PASS
    r = D("ceiling.game", [ev("1268", "chonborista.com"), ev("1268", "1geki.jp")],
          verify_mock, ask_codex=lambda q: [])
    t("C: ★数値文字列は票にならない → 非PASS", r["state"] != DPASS)
    # ★High(3次): 巨大currentでも例外終了せず正常終端
    r = D("ceiling.game", [ev(1268, "chonborista.com"), ev(1268, "1geki.jp")],
          verify_mock, current=10 ** 400)
    t("C: ★巨大currentでクラッシュせず終端(PASS)", r["state"] == DPASS)
    # ★C5(D責務だが安価対応): 否定・比較表現は非PASS
    t("C5: 『1000Gに到達しません』→ 非PASS(否定)",
      check_claim_identity("天井", "normal", None, 1000, "G",
                           "通常時の天井は1000Gに到達しません")[0] != PASS)
    t("C5: 『1000G未満』→ 非PASS(比較)",
      check_claim_identity("天井", "normal", None, 1000, "G",
                           "通常時の天井は1000G未満です")[0] != PASS)
    t("C5: 『1000Gを超える』→ 非PASS(比較)",
      check_claim_identity("天井", "normal", None, 1000, "G",
                           "通常時の天井は1000Gを超えることがある")[0] != PASS)
    # ★ドメイン厳格化の残り（制御文字/空userinfo/不正port）
    t("C: ★内部改行ホストは空key", _domain_key("sammy.co.\njp") == "")
    t("C: ★空userinfo :@ は空key", _domain_key("https://:@sammy.co.jp/x") == "")
    t("C: ★不正ポートは空key", _domain_key("https://sammy.co.jp:99999/x") == "")

    # ── Codex 4次レビュー対応の追加反例（切り捨て/公式ambiguous/未知key/unit） ──────
    # ★High(4次): 証拠が上限超過で切り捨て → 出典対立を見落とす可能性→自動決着させない
    big_seed = ([ev(1268, "chonborista.com"), ev(1268, "1geki.jp")]
                + [ev(1268, f"d{i}.example") for i in range(205)])
    r = D("ceiling.game", big_seed, verify_mock)
    t("C: ★証拠上限超過(切り捨て) → 非PASS(MANUAL)",
      r["state"] == DR_MANUAL and r["value"] is None)
    # ★High(4次): 公式(tier1)が複数値を支持→ambiguous化しても低tierで決着させない
    r = D("kikaiwari", [ev(108.0, "sammy.co.jp"), ev(108.0, "chonborista.com"),
                        ev(108.0, "1geki.jp"), ev(110.0, "sammy.co.jp")],
          verify_mock, ask_codex=lambda q: [])
    t("C: ★公式がambiguous(複数値) → 自動決着させない 非PASS", r["state"] != DPASS)
    # ★(4次): 未知item_key(typo)は有限数値でも自動公開しない → REVIEW_ANOMALY
    r = D("ceilling.game", [ev(999, "chonborista.com"), ev(999, "1geki.jp")], verify_mock)
    t("C: ★未知item_key(typo)は 非PASS(REVIEW_ANOMALY)", r["state"] == DR_ANOMALY)
    # ★(4次): unit取り違え(ceiling.game に pt) → 票にならず非PASS
    r = D("ceiling.game", [ev(1000, "chonborista.com", unit="pt"),
                           ev(1000, "1geki.jp", unit="pt")], verify_mock, ask_codex=lambda q: [])
    t("C: ★unit取り違え(pt) → 票にならず非PASS", r["state"] != DPASS)
    # 正常: unit一致(G)なら通る
    r = D("ceiling.game", [ev(1268, "chonborista.com", unit="G"),
                           ev(1268, "1geki.jp", unit="G")], verify_mock)
    t("C: unit一致(G)なら PASS", r["state"] == DPASS and r["value"] == 1268)

    # ── Codex 5次レビュー対応（検証未完了 vs 入力不正の分離） ──────
    # ★High(5次): 公式の対立候補だけ検証が例外→検証未完了でPASSさせない
    def verify_partial(item_key, e):
        if e.get("domain") == "sammy.co.jp":
            raise TimeoutError("取得失敗")
        return verify_mock(item_key, e)
    r = D("ceiling.game",
          [ev(1268, "chonborista.com"), ev(1268, "1geki.jp"), ev(1300, "sammy.co.jp")],
          verify_partial, max_rounds=0)
    t("C: ★一部検証が例外(公式対立候補)→検証未完了で BLOCKED_VERIFICATION_ERROR(5次)",
      r["state"] == DBLOCKED_VERIFY and r["value"] is None)
    # ★入力不正(非dict)が混じっても、検証済みの正しい証拠で PASS（DoSにしない・分離）
    r = D("ceiling.game",
          [ev(1268, "chonborista.com"), ev(1268, "1geki.jp"), "not-a-dict", 12345],
          verify_mock)
    t("C: ★入力不正が混在しても正しい証拠で PASS（検証未完了と分離）",
      r["state"] == DPASS and r["value"] == 1268)

    # ── Codex 6次レビュー対応（4桁丸めによる異値統合の防止） ──────
    # ★High(6次): 1000 と 1000.00004 は別値→統合せず→2ドメインが同値と誤認しない→非PASS
    r = D("ceiling.game", [ev(1000, "chonborista.com"), ev(1000.00004, "1geki.jp")],
          verify_mock, ask_codex=lambda q: [])
    t("C: ★1000と1000.00004は統合せず別値 → 非PASS(6次)", r["state"] != DPASS)
    # 正常: 1000 と 1000.0 は同値として統合 → PASS
    r = D("ceiling.game", [ev(1000, "chonborista.com"), ev(1000.0, "1geki.jp")],
          verify_mock)
    t("C: 1000と1000.0は同値統合 → PASS", r["state"] == DPASS and r["value"] in (1000, 1000.0))

    # ── Codex 7次レビュー対応（大整数の float 精度消失を防ぐ） ──────
    big1, big2 = 9007199254740992, 9007199254740993   # 2^53, 2^53+1（float化で潰れる）
    t("C: _norm_value 大整数は元の整数を保持（float精度で潰さない）",
      _norm_value(big1) == big1 and _norm_value(big2) == big2 and big1 != big2)
    r = D("nerai", [ev(big1, "chonborista.com"), ev(big2, "1geki.jp")],
          verify_mock, ask_codex=lambda q: [])
    t("C: ★2^53超の異値を統合せず対立扱い → 非PASS(7次)", r["state"] != DPASS)

    # ── Codex 8次レビュー対応（anomaly精度・微小小数でハード制約を回避させない） ──────
    t("anomaly: 整数項目 1000.0000000001 は整数でない → REJECT",
      A("ceiling.game", 1000.0000000001)[0] == REJECT)
    t("anomaly: 機械割 97.80000000001(過剰精度) → REJECT",
      A("kikaiwari", 97.80000000001)[0] == REJECT)
    t("anomaly: 機械割 97.8(正常1桁) → PASS", A("kikaiwari", 97.8)[0] == PASS)
    r = D("kikaiwari", [ev(97.80000000001, "chonborista.com"),
                        ev(97.80000000001, "1geki.jp")], verify_mock, current=97.8)
    t("C: ★過剰精度小数は最終異常ゲートで REVIEW_ANOMALY(8次)", r["state"] == DR_ANOMALY)

    ok_all = all(c for _, c in results)
    print(f"\nselftest: {sum(1 for _, c in results if c)}/{len(results)} 合格")
    return 0 if ok_all else 1


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        sys.exit(selftest())
    ap.error("--selftest を指定")

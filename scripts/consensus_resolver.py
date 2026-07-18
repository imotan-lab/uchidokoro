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
import re
import sys
import unicodedata

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
NEGATION_MARKERS = ["ではなく", "では無く", "でなく", "ではない", "ではなし",
                    "されない", "はしない", "ないと", "無く"]

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
                         operator=None) -> tuple[str, str]:
    """★C5（fail-closed・3値）: 値が「その mode/scope の・その項目の」値として同一文脈で
    述べられているか★（Codexレビュー反映・2026-07-19）
    戻り値 (verdict, reason)。verdict:
      PASS  … quoteが明確にこの主張を支持（自動採用してよい）
      REJECT… quoteが明確に否定/別条件/否定文/短縮量/狙い目等（値は誤り）
      REVIEW… 判断材料が不足・不確実（自動採用しない＝現状維持/preview・人へ）
    fail-closed: 明確にPASSでない曖昧なものは全てREVIEW（誤公開しない）。"""
    nq = normalize(quote)
    if not nq:
        return REVIEW, "quoteが空"
    positions = _find_value_positions(nq, value)
    if not positions:
        return REJECT, f"値 {value} がquoteに存在しない（quoteが主張を支持しない）"

    nunit = normalize(str(unit)) if unit not in (None, "") else ""
    m = _norm_mode(mode)
    sc = _norm_scope(scope)
    is_ceiling = (item in (None, "天井", "ceiling"))

    saw_contradiction = False
    review_reason = "判断材料が不足（fail-closed→REVIEW）"
    reject_reason = "別条件/否定/短縮量/狙い目等の疑い"

    for pos, end in positions:
        vlen = end - pos
        ctx = _governing_context(nq, pos, vlen)
        after = nq[pos: min(len(nq), end + 8)]  # 値の直後（否定・短縮量の隣接判定）

        # ★否定★「1500Gではなく」→この出現は打ち消し＝矛盾（直後＋支配文節の両方を見る）
        if _hit(after, NEGATION_MARKERS) or _hit(ctx, NEGATION_MARKERS):
            saw_contradiction = True
            reject_reason = "否定文脈＝値が打ち消されている"
            continue
        # ★短縮"量"★「300G短縮/短くなる」（に/へ/までが無い）＝天井値でなく短縮幅
        if _AMOUNT_RE.search(after):
            saw_contradiction = True
            reject_reason = "『N G短縮/短くなる』＝短縮量であって天井値でない"
            continue
        # 不確実表現 → REVIEW（確定値にしない）
        if _hit(ctx, UNCERTAIN_MARKERS):
            review_reason = f"不確実表現（{_hit(ctx, UNCERTAIN_MARKERS)}）＝確定値にしない"
            continue
        # 訂正/旧情報の文脈 → REVIEW（現行値か旧誤りか一意に決められない）
        if _hit(ctx, CORRECTION_MARKERS):
            review_reason = f"訂正/旧情報の文脈（{_hit(ctx, CORRECTION_MARKERS)}）"
            continue
        # 天井claimなのに狙い目/ゾーン等の文脈 → 別種の数字＝矛盾
        if is_ceiling and _hit(ctx, ANTI_CEILING):
            saw_contradiction = True
            reject_reason = f"天井でない数値の文脈（{_hit(ctx, ANTI_CEILING)}）"
            continue
        # (a) 単位（指定時）が文脈に無い → 材料不足
        if nunit and nunit not in ctx:
            review_reason = "単位が値の文脈に無い"
            continue
        # (b) 天井らしさ指標が無い → 材料不足（ただの数字かも）
        if is_ceiling and not _hit(ctx, CEILING_INDICATORS):
            review_reason = "天井を示す語（天井/到達/放出/最大/短縮等）が文脈に無い"
            continue

        # (c) モード判定
        reset_hits = _hit(ctx, MODE_LABELS["reset"])
        short_hits = _hit(ctx, SHORT_MARKERS)
        normal_hits = _hit(ctx, MODE_LABELS["normal"])
        ctx_scopes = [fam for fam, labels in SCOPE_LABELS.items() if _hit(ctx, labels)]
        # ★曖昧: 同一文脈に複数mode/複数scope → 一意に決められない＝REVIEW（fail-closed）
        if (normal_hits and reset_hits) or len(ctx_scopes) > 1:
            review_reason = "同一文脈に複数の条件（mode/scope）＝一意に決められない"
            continue
        if m == "reset":
            if short_hits:
                saw_contradiction = True
                reject_reason = f"reset claimだが別トリガー（{short_hits}）が支配"
                continue
            if not reset_hits:
                if normal_hits:
                    saw_contradiction = True
                    reject_reason = "reset claimだが文脈は通常時"
                else:
                    review_reason = "reset claimだがリセット(設定変更)の語が文脈に無い"
                continue
        else:  # normal / None は「素の通常天井」を要求（短縮された値は素の通常天井でない）
            reduced_hits = _hit(ctx, ["短縮", "ダウン", "減算"])
            if reset_hits or short_hits or reduced_hits:
                saw_contradiction = True
                reject_reason = ("通常時claimだが別モード/別トリガー/短縮: "
                                 f"{reset_hits + short_hits + reduced_hits}")
                continue

        # (d) 範囲判定（ctx_scopes は (c) で算出済み）
        if sc:
            if ctx_scopes and sc not in ctx_scopes:
                saw_contradiction = True
                reject_reason = f"別scope（{ctx_scopes}）の値をscope『{sc}』として拾おうとした"
                continue
            if sc not in ctx_scopes:
                review_reason = f"scope『{sc}』が文脈で確認できない"
                continue
        else:
            if ctx_scopes:
                saw_contradiction = True
                reject_reason = f"scope無指定claimだが文脈は特定scope: {ctx_scopes}"
                continue

        # (e) operator整合（任意・矛盾のみ）
        if operator:
            has_max = ("最大" in ctx) or ("上限" in ctx) or ("max" in ctx)
            has_reach = bool(re.search(r"到達で|消化で|ちょうど|きっかり", ctx)) or \
                bool(re.search(r"g(で|にて)(天井|当選|放出|突入)", ctx))
            if operator == "max" and has_reach and not has_max:
                saw_contradiction = True
                reject_reason = "operator=maxだが文脈は到達確定表現"
                continue
            if operator == "exact" and has_max and not has_reach:
                saw_contradiction = True
                reject_reason = "operator=exactだが文脈は最大表現"
                continue

        return PASS, f"C5合格（mode={m or 'plain'}/scope={sc or 'なし'}・文脈一致）"

    # PASSする出現が無かった
    if saw_contradiction:
        return REJECT, reject_reason
    return REVIEW, review_reason


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

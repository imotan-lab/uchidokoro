# -*- coding: utf-8 -*-
"""
consensus_verify.py — Dフェーズ D-1a: verify検証器（Cの verify コールバックの実体）

resolve_with_dialogue(consensus_resolver.py) が要求する verify(item_key, evidence) を、
既存 verify_claims.py の 実ページ再取得(SSRF対策)＋C0〜C4 と、consensus_resolver.py の
C5(check_claim_identity) を組み合わせて実装する。
★consensus_resolver.py には一切手を入れない（Cフェーズの合格を崩さない）★

安全要件（Codex Dフェーズ設計相談 v1.5 ＋ D-1a第1回レビュー反映）:
 - mode/scope/operator/unit はAIに定義させず ITEM_REGISTRY(フィールド定義)からサーバー側で確定
 - raw_value(元の十進文字列)を必須保持し、float化より前に Decimal で桁数・型・値域を検査
   （★元scaleで判定＝967.0/97.40 を拒否・int項目は小数点を構文禁止★）
 - value必須・型厳密(bool拒否・項目型一致)・canonical と一致（★検証器は値を書き換えない★）
 - callbackの item_key と field_key の anomaly_key の一致を強制（confused-deputy防止）
 - 1回のverifyは同一取得スナップショットでC0〜C5＋★quote出現位置の周辺に機種identity（局所束縛）★
 - 項目別C5が未実装のitem(機械割/狙い目/純増/スルー天井…)は必ず REVIEW（自動公開しない）
 - identity は 非空list[str]・空要素なし・最低1つ3文字以上 をサーバー検査
 - 検証器が返すのは Cの source 形 {value, domain, verified, c5_verdict, c5_code}

設計書: Documents/uchidokoro/gpt_research/consensus_design.md（v1.5）
"""
from __future__ import annotations
import hashlib
import math
import os
import re
import sys
from collections import namedtuple
from decimal import Decimal, InvalidOperation

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify_claims as vc
from consensus_resolver import check_claim_identity, PASS, REJECT, REVIEW

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ★D-1a-2-2 構造保持の器: DocumentSnapshot（設計書v1.7 Q3）★
#   この段では「取得を単一化し、hash/警告だけ先に持つ」＝束縛は従来の平坦テキスト(_sentences_with_value)
#   のまま変更しない。h1_candidates / units / parse_warnings の構造抽出は D-1a-2-3 以降で埋める。
#   verify_claims/fetch_page は不変（影響隔離）＝この型は D-1a 側(consensus_verify)に置く。
DocumentSnapshot = namedtuple(
    "DocumentSnapshot",
    "final_url title h1_candidates response_sha256 html_sha256 rendered_text_sha256 parse_warnings units")

# canonical十進のみ許可（指数e / 16進 / カンマ / 単位付き / Unicode数字 / 先頭+ / 不要な先頭ゼロ を拒否）
# 監査用 raw_value の一意性のため 00967 / +967 も拒否（Codex D-1a再レビュー Minor）。
_DECIMAL_RE = re.compile(r"^(0|[1-9][0-9]*)(\.[0-9]+)?$")

# ── ITEM_REGISTRY: 公開先フィールドの定義（★mode/scope/unit/rangeはここで確定＝AI申告不可★）──
#   anomaly_key = consensus_resolver の ITEM_SPEC/MIN_DOMAINS キー（範囲・型・独立数の判定用）
#   c5_ready    = 項目別C5が使えるか（False=自動公開しない＝REVIEW固定・Codex指摘）
#   ntype/decimals = raw_value の Decimal 桁・型検査 / range = 値域(Dのraw処理でも検査・設計書v1.5)
#   ★天井のG数系のみ c5_ready=True（check_claim_identity が天井の意味検査を実装済み）。
#     スルー天井/機械割/狙い目/純増等は項目別C5を実装するまで c5_ready=False（＝REVIEW）。★
ITEM_REGISTRY = {
    "ceiling.normal":    {"anomaly_key": "ceiling.game",   "c5_item": "天井", "c5_ready": True,  "mode": "normal", "scope": None,    "unit": "G",  "ntype": "int",   "range": (50, 5000)},
    "ceiling.normal.at": {"anomaly_key": "ceiling.game",   "c5_item": "天井", "c5_ready": True,  "mode": "normal", "scope": "AT間", "unit": "G",  "ntype": "int",   "range": (50, 5000)},
    "ceiling.reset":     {"anomaly_key": "ceiling.game",   "c5_item": "天井", "c5_ready": True,  "mode": "reset",  "scope": None,    "unit": "G",  "ntype": "int",   "range": (50, 5000)},
    # ── 以下 c5_ready=False（項目別C5が未実装＝自動公開しない・必ずREVIEW）──
    "ceiling.through":   {"anomaly_key": "ceiling.through", "c5_item": "天井", "c5_ready": False, "mode": "normal", "scope": None,    "unit": None, "ntype": "int",   "range": (1, 40)},
    "kikaiwari.set1":    {"anomaly_key": "kikaiwari",       "c5_item": "機械割", "c5_ready": False, "mode": "normal", "scope": None, "unit": "%", "ntype": "float", "decimals": 1, "range": (90.0, 135.0)},
    "kikaiwari.set6":    {"anomaly_key": "kikaiwari",       "c5_item": "機械割", "c5_ready": False, "mode": "normal", "scope": None, "unit": "%", "ntype": "float", "decimals": 1, "range": (90.0, 135.0)},
    "nerai.normal":      {"anomaly_key": "nerai",           "c5_item": "狙い目", "c5_ready": False, "mode": "normal", "scope": None, "unit": "G", "ntype": "int",   "range": (100, 4000)},
}


def anomaly_key_of(field_key: str) -> str:
    """resolve_with_dialogue へ渡す item_key（＝anomaly/MIN_DOMAINSキー）を field_key から引く。"""
    reg = ITEM_REGISTRY.get(field_key)
    return reg["anomaly_key"] if reg else field_key


def _src(value, domain, verified, c5_verdict, c5_code, field_key, raw, snapshot=None):
    """Cの source 形（＋監査用に raw_value/field_key/理由コード/snapshotを保持）。
    snapshot={final_url, body_sha256, html_sha256, response_sha256} は後段(D-1b/d)の
    再取得整合・監査材料（Codex E9・D-1a-2-2でhash拡充。response_sha256は今段None）。"""
    s = {"value": value, "domain": domain, "verified": verified,
         "c5_verdict": c5_verdict, "c5_code": c5_code,
         "field_key": field_key, "raw_value": raw}
    if snapshot is not None:
        s["snapshot"] = snapshot
    return s


def _valid_identity(identity) -> bool:
    """identity は 非空 list/tuple[str]・空要素なし・最低1つ3文字以上（Codex A6）。"""
    if not isinstance(identity, (list, tuple)) or not identity:
        return False
    if any((not isinstance(s, str)) or not s.strip() for s in identity):
        return False
    return any(len(vc.normalize(s)) >= 3 for s in identity)


def _check_raw_value(reg, raw_value):
    """★float化より前に raw_value(元の十進文字列)を Decimal で検査（設計書v1.5・Codex）★
    戻り (ok, canonical_value, code)。指数/カンマ/Unicode数字/桁過多/型不一致/値域外 を拒否。
    ★scaleは normalize しない元の as_tuple().exponent で判定（967.0/97.40 を通さない）★"""
    if not isinstance(raw_value, str) or not raw_value:
        return False, None, "RAW_VALUE_MISSING"
    if raw_value != raw_value.strip():              # 前後空白はcanonicalでない（Codex Minor）
        return False, None, "RAW_VALUE_SYNTAX"
    s = raw_value
    if not _DECIMAL_RE.fullmatch(s):
        return False, None, "RAW_VALUE_SYNTAX"
    if len(s) > 12:                                 # 総桁数上限（巨大数・DoS・Codex C）
        return False, None, "RAW_VALUE_TOO_LONG"
    if reg["ntype"] == "int" and "." in s:
        return False, None, "RAW_VALUE_NOT_INT"   # int項目は小数点自体を禁止（967.0も拒否）
    try:
        d = Decimal(s)
    except InvalidOperation:
        return False, None, "RAW_VALUE_DECIMAL"
    if not d.is_finite():
        return False, None, "RAW_VALUE_NONFINITE"
    exp = d.as_tuple().exponent          # ★normalizeしない元scale（97.40→2桁）
    decimals = -exp if isinstance(exp, int) and exp < 0 else 0
    if reg["ntype"] == "int":
        cval = int(d)
    else:
        if decimals > reg.get("decimals", 1):
            return False, None, "RAW_VALUE_TOO_MANY_DECIMALS"
        f = float(d)
        if not math.isfinite(f):
            return False, None, "RAW_VALUE_NONFINITE"
        cval = f
    lo, hi = reg.get("range", (None, None))
    if lo is not None and not (lo <= cval <= hi):
        return False, None, "RAW_VALUE_OUT_OF_RANGE"
    return True, cval, "OK"


def build_document_snapshot(hs, page) -> DocumentSnapshot:
    """★D-1a-2-2: 単一取得(fetch_html→HtmlSnapshot)と、それを平坦化した Page から DocumentSnapshot を組む。★
    この段では hash（html/rendered）だけ確定的に持ち、束縛には使わない（構造化はD-1a-2-3以降）。
    - html_sha256          : 取得したdecoded HTMLのhash（HtmlSnapshotが計算済み）
    - rendered_text_sha256 : 平坦化後テキスト(page.text)のhash＝flatがそのHTML由来である監査痕跡
    - response_sha256      : 生ワイヤーhash。verify_claims影響隔離のため今段はNone（fetch_html拡張で後段に）
    - h1_candidates/units/parse_warnings : 構造抽出はD-1a-2-3以降（今は空）"""
    rendered_sha = hashlib.sha256((page.text or "").encode("utf-8", "replace")).hexdigest()
    return DocumentSnapshot(
        final_url=hs.final_url,
        title=hs.title,
        h1_candidates=(),
        response_sha256=None,
        html_sha256=hs.html_sha256,
        rendered_text_sha256=rendered_sha,
        parse_warnings=(),
        units=(),
    )


_SENT_SPLIT = re.compile(r"[。！？\n；;]")  # 文区切り（★半角 . は小数点(97.4)保護のため使わない）


def _sentences_with_value(ntext: str, raw: str):
    """正規化本文を文単位に割り、値(raw)を桁境界つきで含む文を返す（機種×値の意味束縛用・Codex Critical）。
    ★束縛はページ由来の「値を含む文」で行う＝claimantのquote水増しで別機種を束縛できない★"""
    pat = re.compile(r"(?<![0-9.])" + re.escape(vc.normalize(raw)) + r"(?![0-9.])")
    return [s for s in _SENT_SPLIT.split(ntext) if pat.search(s)]


def verify_evidence(field_key, evidence, identity, allowed_domains=None,
                    fetch_fn=None, *, expect_item_key):
    """1つの生証拠を検証し Cの source 形を返す（★mode/scope/unit/rangeはregから確定・AI申告無視★）。
    verified=True は C0〜C5全通過＋raw Decimal OK＋c5_ready＋局所束縛OK のときだけ。
    identity: 機種同定文字列リスト（正規化前・非空str）。expect_item_key: callbackのitem_key（一致強制）。
    ★D-1a-2-2: 取得は fetch_html を1回だけ呼び、平坦Pageは page_from_html_snapshot で導出（二重取得しない）。★"""
    fetch = fetch_fn or vc.fetch_html
    if not isinstance(evidence, dict):
        return _src(None, "", False, REVIEW, "EVIDENCE_NOT_DICT", field_key, None)
    reg = ITEM_REGISTRY.get(field_key)
    if reg is None:
        return _src(None, "", False, REVIEW, "UNREGISTERED_FIELD", field_key, None)
    # ★callbackの item_key と field_key の anomaly_key の一致を強制（confused-deputy防止・Codex A3）
    #   expect_item_key はキーワード必須＝直接呼びでも省略できない（None も不一致でREJECT・再レビューHigh）
    if expect_item_key != reg["anomaly_key"]:
        return _src(None, "", False, REJECT, "ITEM_KEY_MISMATCH", field_key, None)
    # identity の健全性（Codex A6）
    if not _valid_identity(identity):
        return _src(None, "", False, REVIEW, "BAD_IDENTITY", field_key, None)

    raw = evidence.get("raw_value")
    ok, cval, code = _check_raw_value(reg, raw)
    if not ok:
        return _src(None, "", False, REJECT, code, field_key, raw)
    # ★value必須・bool拒否・項目型一致・canonicalと一致（検証器は値を書き換えない・Codex A5/C）
    if "value" not in evidence:
        return _src(None, "", False, REJECT, "VALUE_MISSING", field_key, raw)
    ev_val = evidence.get("value")
    if isinstance(ev_val, bool):
        return _src(ev_val, "", False, REJECT, "VALUE_BOOL", field_key, raw)
    if reg["ntype"] == "int" and not isinstance(ev_val, int):
        return _src(ev_val, "", False, REJECT, "VALUE_TYPE", field_key, raw)
    if reg["ntype"] == "float" and not isinstance(ev_val, (int, float)):
        return _src(ev_val, "", False, REJECT, "VALUE_TYPE", field_key, raw)
    if ev_val != cval:
        return _src(ev_val, "", False, REJECT, "VALUE_MISMATCH_RAW", field_key, raw)

    url = evidence.get("url", "")
    quote = evidence.get("quote", "")
    nquote = vc.normalize(quote)
    nvalue = vc.normalize(raw)   # ★C4は検証済み raw（canonical十進文字列）で照合
    nids = [vc.normalize(str(s)) for s in identity if vc.normalize(str(s))]

    # C0: 体裁（url/quote非空・退化quote拒否・blocked拒否・許可ドメイン）
    if not url or not nquote:
        return _src(cval, "", False, REJECT, "C0_EMPTY", field_key, raw)
    if len(nquote) < 8 or not vc._CJK.search(nquote):
        return _src(cval, "", False, REJECT, "C0_DEGENERATE_QUOTE", field_key, raw)
    if vc.is_blocked_source(url):
        return _src(cval, "", False, REJECT, "C0_BLOCKED_SOURCE", field_key, raw)
    # ★空allowlist [] を fail-open にしない（is not None＝[]なら全拒否・Codex E7）
    if allowed_domains is not None and not vc.host_matches_allowlist(url, allowed_domains):
        return _src(cval, "", False, REJECT, "C0_DOMAIN_NOT_ALLOWED", field_key, raw)

    # C4: 値がquote内（決定論・取得前に検査できる）
    tokens = vc._value_tokens(nvalue)
    nquote_sp = vc.normalize_spaced(quote)
    if tokens:
        if any(not vc._token_in(t, nquote_sp) for t in tokens):
            return _src(cval, "", False, REJECT, "C4_VALUE_NOT_IN_QUOTE", field_key, raw)
    elif nvalue not in nquote:
        return _src(cval, "", False, REJECT, "C4_VALUE_NOT_IN_QUOTE", field_key, raw)

    # C1: 取得（★単一取得＝fetch_html で HtmlSnapshot を1回取り、平坦Pageは導出。以降は同一スナップショット★）
    snapshot = fetch(url, allowed=allowed_domains)
    if snapshot is None:
        return _src(cval, "", False, REVIEW, "C1_FETCH_FAILED", field_key, raw)
    page = vc.page_from_html_snapshot(snapshot)   # 平坦Page（再取得しない・同一スナップショット由来）
    doc = build_document_snapshot(snapshot, page)  # ★D-1a-2-2 二重化：構造snapshotを併走（束縛には未使用）
    if vc.is_blocked_source(page.final_url):
        return _src(cval, "", False, REJECT, "C0_REDIRECT_BLOCKED", field_key, raw)
    # ★最終URLの許可ドメイン再検査（verify_evidence側の多層確認・キャッシュ経路も塞ぐ・Codex E8）
    if allowed_domains is not None and not vc.host_matches_allowlist(page.final_url, allowed_domains):
        return _src(cval, "", False, REJECT, "C1_FINAL_URL_NOT_ALLOWED", field_key, raw)
    dom = vc.domain_of(page.final_url)
    ntext = vc.normalize(page.text)
    ntitle = vc.normalize(page.title)
    # ★hashは DocumentSnapshot 由来で持つ（body_sha256は後方互換キー＝rendered_text_sha256と同値）★
    snap = {"final_url": doc.final_url,
            "body_sha256": doc.rendered_text_sha256,
            "html_sha256": doc.html_sha256,
            "response_sha256": doc.response_sha256}

    # C2: 同定（title＋本文の両方に identity・旧機種/一覧ページ対策）
    if any(not vc._ident_in(s, ntext) for s in nids):
        return _src(cval, dom, False, REJECT, "C2_IDENT_NOT_IN_BODY", field_key, raw)
    if not ntitle or any(not vc._ident_in(s, ntitle) for s in nids):
        return _src(cval, dom, False, REJECT, "C2_IDENT_NOT_IN_TITLE", field_key, raw)

    # C3: claimant quote が逐語でページに実在（quoteが本物のページ引用であること）
    if nquote not in ntext:
        return _src(cval, dom, False, REJECT, "C3_QUOTE_NOT_ON_PAGE", field_key, raw)

    # ★機種×値の意味束縛（Codex Critical・quote水増し攻撃対策）★
    #   claimantのquote全体を束縛単位にせず、★ページ本文から「値を含む文」を抽出★し、その文に
    #   canonical identity(3文字以上)があり、かつ C5 がその文でPASSすることを要求する。
    #   別機種の値の文には対象名が同じ文に無いので弾ける。判定はページ由来の原子単位で行う
    #   （claimantが値と対象名を1つのquoteに詰め込んでも、値の"文"に対象名が無ければ通らない）。
    strong_ids = [s for s in nids if len(s) >= 3]     # 短縮語/一般語は束縛に使わない（Codex）
    if not strong_ids:
        return _src(cval, dom, False, REVIEW, "NO_STRONG_IDENTITY", field_key, raw, snapshot=snap)
    sents = _sentences_with_value(ntext, raw)
    if not sents:
        return _src(cval, dom, False, REVIEW, "VALUE_NOT_ON_PAGE", field_key, raw, snapshot=snap)
    # 項目別C5が未実装なら（値のページ実在は確認済みだが）自動公開しない
    if not reg["c5_ready"]:
        return _src(cval, dom, False, REVIEW, "C5_NOT_IMPLEMENTED", field_key, raw, snapshot=snap)
    # 「値を含む文に対象identityがあり、その文でC5がPASS」する主張が1つでもあれば verified
    for sent in sents:
        if not any(sid in sent for sid in strong_ids):
            continue   # 別機種の値の文（同一文に対象名が無い）
        verdict, _reason, c5code = check_claim_identity(
            reg["c5_item"], reg["mode"], reg["scope"], cval, reg["unit"], sent)
        if verdict == PASS:
            # 返す value は evidence 側（検証器は値を書き換えない・cvalと一致確認済み）
            return _src(ev_val, dom, True, PASS, "OK", field_key, raw, snapshot=snap)
    # 対象機種と同一文で C5 PASS する値の主張が見つからない → 自動公開しない
    return _src(cval, dom, False, REVIEW, "C5_NO_LOCAL_MATCH", field_key, raw, snapshot=snap)


def make_verifier(field_key, identity, allowed_domains=None, fetch_fn=None):
    """resolve_with_dialogue に渡す verify(item_key, evidence) を作る（field_key/identityを束縛）。
    ★resolveへ渡す item_key は anomaly_key_of(field_key)。verify内で item_key の一致を強制★"""
    def verify(item_key, evidence):
        return verify_evidence(field_key, evidence, identity, allowed_domains,
                               fetch_fn, expect_item_key=item_key)
    return verify


# ══════════════════════════════════════════════════════════════
# selftest（★fetchはモック＝vc._html_cache に合成HtmlSnapshotを差す・実ネットに出ない★）
#   verify_evidence は fetch_html→page_from_html_snapshot で平坦Pageを導出するため、
#   モックHTMLはタグ無しの平坦テキストでよい（_flatten_htmlは恒等でPageに戻る）。
# ══════════════════════════════════════════════════════════════

def selftest() -> int:
    from consensus_resolver import resolve_with_dialogue, DPASS
    results = []
    vc._html_cache.clear()
    vc._page_cache.clear()

    def t(name, cond):
        results.append((name, cond))
        print(("OK " if cond else "NG ") + name)

    IDENT = ["アクダマ"]

    def mock_page(url, body, title="アクダマ 天井・狙い目", final_url=None):
        # body は平坦テキスト（タグ無し）＝_flatten_htmlは恒等→page_from_html_snapshotでPage(body,...)へ
        h = hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()
        vc._html_cache[url] = vc.HtmlSnapshot(body, title, (final_url or url), h)

    def ev(value, url, quote, raw=None):
        return {"value": value, "raw_value": (raw if raw is not None else str(value)),
                "url": url, "quote": quote}

    def V(field_key, evidence, identity, **kw):
        # ★expect_item_key を自動付与（本番は make_verifier が付与・テストの利便）
        kw.setdefault("expect_item_key", anomaly_key_of(field_key))
        return verify_evidence(field_key, evidence, identity, **kw)

    # ★quoteには対象機種名を含める（別機種取り違え防止・Codex Critical・運用と同じ）
    Q_AT967 = "アクダマの通常時の天井はAT間967Gで当選"
    Q_THROUGH = "アクダマはボーナス6スルーで7回目AT確定の天井"
    Q_KW = "アクダマの機械割は設定1で97.4%"
    U1 = "https://chonborista.com/akudama"
    U2 = "https://nana-press.com/akudama"
    mock_page(U1, "Lアクダマドライブ。" + Q_AT967 + "。設定変更後（リセット）は745Gに短縮。" + Q_THROUGH + "。")
    mock_page(U2, "アクダマドライブ。" + Q_AT967 + "する。" + Q_KW + "。")

    # ── 正常系（天井・C5実装済み）→ verified True ──
    r = V("ceiling.normal.at", ev(967, U1, Q_AT967), IDENT)
    t("天井967(通常AT間)・C0〜C5全通過 → verified True/PASS",
      r["verified"] is True and r["c5_verdict"] == PASS and r["value"] == 967)
    # ── リセット混同 → C5でREJECT ──
    r = V("ceiling.reset", ev(967, U1, Q_AT967), IDENT)
    t("★reset claimだがページ本文は通常時の値のみ → verified False(REVIEW/該当文なし)",
      r["verified"] is False and r["c5_verdict"] == REVIEW)
    # ── スルー天井(c5_ready=False) → REVIEW ──
    r = V("ceiling.through", ev(6, U1, Q_THROUGH, raw="6"), IDENT)
    t("★スルー天井(c5_ready=False) → REVIEW(C5_NOT_IMPLEMENTED)",
      r["verified"] is False and r["c5_verdict"] == REVIEW and r["c5_code"] == "C5_NOT_IMPLEMENTED")
    # ── 機械割(c5_ready=False) → REVIEW ──
    r = V("kikaiwari.set1", ev(97.4, U2, Q_KW, raw="97.4"), IDENT)
    t("★機械割(項目別C5未実装) → REVIEW(C5_NOT_IMPLEMENTED)",
      r["c5_verdict"] == REVIEW and r["c5_code"] == "C5_NOT_IMPLEMENTED")
    # ── 未登録field → REVIEW ──
    t("★未登録field → REVIEW(UNREGISTERED_FIELD)",
      V("unknown.field", ev(967, U1, Q_AT967), IDENT)["c5_code"] == "UNREGISTERED_FIELD")

    # ── raw_value 桁/型/構文/値域/canonical（Codex A4/C・再レビューMinor）──
    t("★整数項目に小数raw 967.5 → REJECT(RAW_VALUE_NOT_INT)",
      V("ceiling.normal.at", ev(967, U1, "x", raw="967.5"), IDENT)["c5_code"] == "RAW_VALUE_NOT_INT")
    t("★整数項目に 967.0(末尾ゼロ) → REJECT(RAW_VALUE_NOT_INT)",
      V("ceiling.normal.at", ev(967, U1, "x", raw="967.0"), IDENT)["c5_code"] == "RAW_VALUE_NOT_INT")
    t("★指数表記 9.67e2 → REJECT(RAW_VALUE_SYNTAX)",
      V("ceiling.normal.at", ev(967, U1, "x", raw="9.67e2"), IDENT)["c5_code"] == "RAW_VALUE_SYNTAX")
    t("★カンマ 1,000 → REJECT(RAW_VALUE_SYNTAX)",
      V("ceiling.normal", ev(1000, U1, "x", raw="1,000"), IDENT)["c5_code"] == "RAW_VALUE_SYNTAX")
    t("★raw先頭+ +967 → REJECT(RAW_VALUE_SYNTAX)",
      V("ceiling.normal.at", ev(967, U1, "x", raw="+967"), IDENT)["c5_code"] == "RAW_VALUE_SYNTAX")
    t("★raw先頭ゼロ 00967 → REJECT(RAW_VALUE_SYNTAX)",
      V("ceiling.normal.at", ev(967, U1, "x", raw="00967"), IDENT)["c5_code"] == "RAW_VALUE_SYNTAX")
    t("★機械割 桁過多 97.45 → REJECT(TOO_MANY_DECIMALS)",
      V("kikaiwari.set1", ev(97.45, U2, "x", raw="97.45"), IDENT)["c5_code"] == "RAW_VALUE_TOO_MANY_DECIMALS")
    t("★機械割 97.40(末尾ゼロで2桁) → REJECT(TOO_MANY_DECIMALS)",
      V("kikaiwari.set1", ev(97.4, U2, "x", raw="97.40"), IDENT)["c5_code"] == "RAW_VALUE_TOO_MANY_DECIMALS")
    t("★値域外 天井 30G → REJECT(OUT_OF_RANGE)",
      V("ceiling.normal.at", ev(30, U1, "x", raw="30"), IDENT)["c5_code"] == "RAW_VALUE_OUT_OF_RANGE")
    t("★raw桁数過多 → REJECT(RAW_VALUE_TOO_LONG)",
      V("ceiling.normal.at", ev(967, U1, "x", raw="9" * 15), IDENT)["c5_code"] == "RAW_VALUE_TOO_LONG")
    t("★raw前後空白 ' 967 ' → REJECT(RAW_VALUE_SYNTAX)",
      V("ceiling.normal.at", ev(967, U1, "x", raw=" 967 "), IDENT)["c5_code"] == "RAW_VALUE_SYNTAX")

    # ── value 必須・型・一致（Codex A5/C）──
    t("★value欠落 → REJECT(VALUE_MISSING)",
      V("ceiling.normal.at", {"raw_value": "967", "url": U1, "quote": Q_AT967}, IDENT)["c5_code"] == "VALUE_MISSING")
    t("★value bool → REJECT(VALUE_BOOL)",
      V("ceiling.normal.at", {"value": True, "raw_value": "967", "url": U1, "quote": "x"}, IDENT)["c5_code"] == "VALUE_BOOL")
    t("★int項目にfloat value 967.0 → REJECT(VALUE_TYPE)",
      V("ceiling.normal.at", {"value": 967.0, "raw_value": "967", "url": U1, "quote": "x"}, IDENT)["c5_code"] == "VALUE_TYPE")
    t("★value と raw不一致(968 vs 967) → REJECT(VALUE_MISMATCH_RAW)",
      V("ceiling.normal.at", {"value": 968, "raw_value": "967", "url": U1, "quote": "x"}, IDENT)["c5_code"] == "VALUE_MISMATCH_RAW")

    # ── identity 健全性（Codex A6）──
    t("★identity空 → REVIEW(BAD_IDENTITY)",
      V("ceiling.normal.at", ev(967, U1, Q_AT967), [])["c5_code"] == "BAD_IDENTITY")
    t("★identity 3文字未満のみ → REVIEW(BAD_IDENTITY)",
      V("ceiling.normal.at", ev(967, U1, Q_AT967), ["A"])["c5_code"] == "BAD_IDENTITY")
    t("★identity 文字列を直接渡す(list化忘れ) → REVIEW(BAD_IDENTITY)",
      V("ceiling.normal.at", ev(967, U1, Q_AT967), "アクダマ")["c5_code"] == "BAD_IDENTITY")

    # ── C0/C4/C1/C2/C3 ──
    t("★退化quote → REJECT(C0_DEGENERATE_QUOTE)",
      V("ceiling.normal.at", ev(967, U1, "967G"), IDENT)["c5_code"] == "C0_DEGENERATE_QUOTE")
    t("★値がquote内に無い(1200) → REJECT(C4)",
      V("ceiling.normal.at", ev(1200, U1, Q_AT967, raw="1200"), IDENT)["c5_code"] == "C4_VALUE_NOT_IN_QUOTE")
    vc._html_cache["https://chonborista.com/none"] = None  # 取得失敗を決定論モック（実ネットに出ない）
    t("★取得失敗 → REVIEW(C1_FETCH_FAILED)",
      V("ceiling.normal.at", ev(967, "https://chonborista.com/none", Q_AT967), IDENT)["c5_code"] == "C1_FETCH_FAILED")
    t("★同定NG(別機種identity) → REJECT(C2)",
      V("ceiling.normal.at", ev(967, U1, Q_AT967), ["ケンガン"])["c5_verdict"] == REJECT)
    mock_page("https://chonborista.com/akudama2", "Lアクダマドライブ 別の記事本文。天井の話は無い。")
    t("★quoteがページに実在しない → REJECT(C3)",
      V("ceiling.normal.at", ev(967, "https://chonborista.com/akudama2", Q_AT967), IDENT)["c5_code"] == "C3_QUOTE_NOT_ON_PAGE")

    # ── ★Critical: quoteに対象機種名が無い(別機種quote・近くに対象名があっても) → REVIEW ──
    mock_page("https://chonborista.com/mixed",
              "アクダマドライブの解析情報はこちら。バベルの通常時の天井は745Gで当選する。",
              title="アクダマ 天井情報")
    r = V("ceiling.normal", ev(745, "https://chonborista.com/mixed",
                               "バベルの通常時の天井は745Gで当選する", raw="745"), IDENT)
    t("★別機種quote(値の文に対象名なし) → REVIEW(C5_NO_LOCAL_MATCH)",
      r["verified"] is False and r["c5_code"] == "C5_NO_LOCAL_MATCH")
    # ★Critical反例: quote水増し(対象名を別文に混ぜて丸ごと引用)でも、値の"文"に対象名が無ければ弾く
    mock_page("https://chonborista.com/pad",
              "アクダマの解析情報はこちら。バベルの通常時の天井は745Gで当選する。",
              title="アクダマ 天井情報")
    r = V("ceiling.normal", ev(745, "https://chonborista.com/pad",
                               "アクダマの解析情報はこちら。バベルの通常時の天井は745Gで当選する", raw="745"), IDENT)
    t("★quote水増し(対象名を別文に混ぜる) → REVIEW(C5_NO_LOCAL_MATCH)",
      r["verified"] is False and r["c5_code"] == "C5_NO_LOCAL_MATCH")

    # ── ★item_key一致強制（confused-deputy・Codex A3/再レビューHigh）──
    verify_kw = make_verifier("ceiling.normal.at", IDENT)
    t("★誤配線 verify(kikaiwari,天井ev) → REJECT(ITEM_KEY_MISMATCH)",
      verify_kw("kikaiwari", ev(967, U1, Q_AT967))["c5_code"] == "ITEM_KEY_MISMATCH")
    t("正配線 verify(ceiling.game,天井ev) → verified True",
      verify_kw(anomaly_key_of("ceiling.normal.at"), ev(967, U1, Q_AT967))["verified"] is True)
    t("★verify_evidence直接呼びで item_key不一致(省略不可) → REJECT(ITEM_KEY_MISMATCH)",
      verify_evidence("ceiling.normal.at", ev(967, U1, Q_AT967), IDENT,
                      expect_item_key="kikaiwari")["c5_code"] == "ITEM_KEY_MISMATCH")

    # ── ★空allowlist fail-close(E7)・最終URL許可外(E8)──
    t("★空allowlist [] は全拒否(fail-open防止) → REJECT(C0_DOMAIN_NOT_ALLOWED)",
      V("ceiling.normal.at", ev(967, U1, Q_AT967), IDENT,
        allowed_domains=[])["c5_code"] == "C0_DOMAIN_NOT_ALLOWED")
    # リダイレクトで最終URLが許可外へ出る想定（final_url=evil）。キャッシュ経路でも最終URL再検査で弾く
    mock_page("https://chonborista.com/redir", "Lアクダマドライブ。" + Q_AT967,
              title="アクダマ 天井", final_url="https://evil.example/x")
    t("★最終URLが許可外 → REJECT(C1_FINAL_URL_NOT_ALLOWED)",
      V("ceiling.normal.at", ev(967, "https://chonborista.com/redir", Q_AT967),
        IDENT, allowed_domains=["chonborista.com"])["c5_code"] == "C1_FINAL_URL_NOT_ALLOWED")
    t("正常系はsnapshot(final_url/body_sha256)を返す",
      "snapshot" in V("ceiling.normal.at", ev(967, U1, Q_AT967), IDENT)
      and V("ceiling.normal.at", ev(967, U1, Q_AT967), IDENT)["snapshot"].get("body_sha256"))

    # ── ★D-1a-2-2: DocumentSnapshotとの二重化（束縛は未変更・hashだけ先に持つ）★ ──
    r_ok = V("ceiling.normal.at", ev(967, U1, Q_AT967), IDENT)
    t("★二重化: 正常系snapshotに html_sha256/body_sha256 を持つ",
      bool(r_ok["snapshot"].get("html_sha256")) and bool(r_ok["snapshot"].get("body_sha256")))
    _html = "<h1>アクダマ</h1><p>天井は967G</p>"
    _hs = vc.HtmlSnapshot(_html, "T", "https://x.test/a",
                          hashlib.sha256(_html.encode("utf-8")).hexdigest())
    _pg = vc.page_from_html_snapshot(_hs)
    _doc = build_document_snapshot(_hs, _pg)
    t("★二重化: html_sha256はHtmlSnapshot(取得HTML)由来", _doc.html_sha256 == _hs.html_sha256)
    t("★二重化: rendered_text_sha256は平坦テキストのhash",
      _doc.rendered_text_sha256 == hashlib.sha256(_pg.text.encode("utf-8", "replace")).hexdigest())
    t("★二重化: units/warnings/h1は空・response_sha256はNone(構造化は後段)",
      _doc.units == () and _doc.parse_warnings == () and _doc.h1_candidates == ()
      and _doc.response_sha256 is None)
    # ★取得は1検証で厳密に1回だけ（二重取得しない）をスパイで直接保証（Codex非ブロッキング提案）
    _calls = {"n": 0}
    def _spy_fetch(url, allowed=None):
        _calls["n"] += 1
        return vc.fetch_html(url, allowed=allowed)
    V("ceiling.normal.at", ev(967, U1, Q_AT967), IDENT, fetch_fn=_spy_fetch)
    t("★二重化: 1検証で取得は厳密に1回（二重取得しない）", _calls["n"] == 1)

    # ── E2E: verify検証器 × resolve_with_dialogue ──
    fk = "ceiling.normal.at"
    verify = make_verifier(fk, IDENT)
    seed = [ev(967, U1, Q_AT967), ev(967, U2, Q_AT967)]
    r = resolve_with_dialogue(anomaly_key_of(fk), seed, verify, ask_codex=lambda q: [])
    t("★E2E: 大手2独立で天井967が決着 → state PASS/967",
      r["state"] == DPASS and r["value"] == 967)
    r = resolve_with_dialogue(anomaly_key_of(fk), [ev(967, U1, Q_AT967)],
                              verify, ask_codex=lambda q: [])
    t("★E2E: 大手1独立だけ → 非PASS(裏取り不足)", r["state"] != DPASS)
    Q745 = "アクダマの通常時の天井はAT間745Gで当選"
    mock_page("https://1geki.jp/akudama", "アクダマドライブ。" + Q745 + "する。")
    r = resolve_with_dialogue(anomaly_key_of(fk),
                              [ev(967, U1, Q_AT967),
                               ev(745, "https://1geki.jp/akudama", Q745, raw="745")],
                              verify, ask_codex=lambda q: [])
    t("★E2E: 値が対立(967 vs 745) → 非PASS(対立は自動決着しない)", r["state"] != DPASS)

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

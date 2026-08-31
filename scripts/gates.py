#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""gates.py — 公開ゲートの単一情報源（Phase 1・fail-closed 状態機械）

設計正本: _design/site_policy_2026-07-24.md / _design/phase1_gates_design_v2_2026-07-24.md
第8版。Codex 敵対的レビュー7巡の指摘を反映。

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
import math
import re
import sys as _sys
import os as _os
import sys
import unicodedata

# ---------------------------------------------------------------- 定数

LIFECYCLES = ("CANDIDATE", "VERIFIED_PREVIEW", "LEGACY_SEARCH", "SEARCH_READY", "CURATED_ADS")
PERMANENTLY_CLOSED = ("SEARCH_READY", "CURATED_ADS")
# checker mode の状態。★「構造が正しい」と「数値が裏取り済み」は別の事実★
#   STRUCT_OK … 入力軸と判定軸が一致していることを確認済み。数値は未裏取り。
#               → 「当サイトの目安」と明示した上で表示してよい（運営者決定 2026-07-27）。
#                 Phase 0 の事故（回数入力なのにG数判定）は構造バグであり、この検査で防げる。
#   VERIFIED  … 数値まで出典で裏取り済み（Phase 2以降）。
#   DISABLED  … 意図的に停止（Phase 0で止めた20モード等）。
#   UNVERIFIED… 未評価。既定値であり非表示。
CHECKER_MODE_STATES = ("VERIFIED", "STRUCT_OK", "DISABLED", "UNVERIFIED")
# 表示してよい状態（STRUCT_OK は目安ラベルが必須になる）
CHECKER_SHOWABLE = ("VERIFIED", "STRUCT_OK")
HUB_NONE, HUB_PREVIEW_ONLY, HUB_FULL = "none", "preview_only", "full"
ALLOW, DROP, UNCLASSIFIED = "ALLOW", "DROP", "UNCLASSIFIED"

# LEGACY_SEARCH で狙い目数値を出すときに必ず併記する文言（設計v2 §3.2）
LEGACY_DISCLAIMER = "当サイトの目安です（メーカー公表値・確定解析ではありません）"

# 【第1層】絶対禁止。台帳ALLOWでも解除できない。
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from ci_safe import redact as _ci_redact   # noqa: E402  ★CIでは原文を出さない★

ABSOLUTE_DENY = (
    "期待収支", "プラス域", "プラス圏", "プラスライン", "プラス期待値", "期待値プラス",
    "期待値がプラス", "プラスに転じ", "期待枚数", "獲得枚数期待", "期待差枚",
    "損益分岐", "時給", "利益ゾーン", "確実な利益", "プラス収支",
    # 設計正本 §3.4 の deny-pattern（台帳ALLOWでも通さない）
    # 活用形を取りこぼさないよう語幹で持つ（乗る/乗ります/乗って …）
    "期待値が乗", "期待値が積み", "期待収支が積み", "期待値の絶対値が積み",
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
# 半角/全角/漢数字、区切りだけの列挙（設定1/2/4）と各項目に設定を繰り返す形（設定1/設定2/設定4）の両方。
_KANJI_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}
_ONE = r"[1-6１-６一二三四五六]"
_SET_ENUM_PAT = re.compile(
    r"設定\s*" + _ONE + r"(?:\s*[・、,／/･･]\s*(?:設定\s*)?" + _ONE + r"){1,5}")


def _num_of(ch: str) -> int:
    if ch in _KANJI_NUM:
        return _KANJI_NUM[ch]
    return int(str(ch).translate(str.maketrans("１２３４５６", "123456")))


# 「設定Nは（…）存在しない」型：介在語が長い直接断定も捕まえる（文の区切りは跨がない）
_SET_ABSENT_LONG = re.compile(
    r"設定\s*" + _ONE + r"(?:\s*[・、,／/･]\s*(?:設定\s*)?" + _ONE + r")*"
    r"(?:(?!。|設定)[^。]){0,30}?"
    r"(?:非搭載|未搭載|存在しない|搭載していない|搭載されていない|ありません|無い|ない|なし|無し)")
# 「設定1/2/3/4/5のみ」＝残りの設定が無いという主張
_SET_ONLY = re.compile(r"のみ|だけ|に限[らりる]")


def _implies_missing_setting(text: str) -> bool:
    """設定の列挙・断定から「その設定は無い」という主張を読み取る。"""
    for m in _SET_ENUM_PAT.finditer(text):
        nums = sorted({_num_of(c) for c in re.findall(_ONE, m.group(0))})
        if len(nums) >= 2 and set(range(nums[0], nums[-1] + 1)) - set(nums):
            return True                    # 列挙の内部に欠番（例: 1/2/4/5/6）
        # 端の欠番でも「のみ」が続けば非搭載の主張（例: 設定1/2/3/4/5のみ）
        tail = text[m.end():m.end() + 8]
        if len(nums) >= 2 and set(nums) != {1, 2, 3, 4, 5, 6} and _SET_ONLY.search(tail):
            return True
    # 介在語が長い直接断定（「設定3はメーカー資料上の仕様として明確に存在しない」）。
    # ただし「設定1では出現しない」のような挙動の否定は対象外。
    for m in _SET_ABSENT_LONG.finditer(text):
        span = m.group(0)
        if re.search(r"設定\s*" + _ONE + r"\s*で\s*は", span):
            continue
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
    # ★mode直下の値がどの交換率のものかを明示する（Codex 24巡目 #6）★
    #   defaultRate は「最初に選ぶ交換率」であって、直下の値がその交換率だという
    #   証明にはならない。暗黙の継承と入力漏れを区別するために別フィールドにする。
    "baseRateKey",
    # ★天井と50枚あたりG数（2026-08-12）★ 刻みの表が読む。
    #   limit（入力欄の上限）は丸めてあることが多いので、天井は別に持つ。
    "ceiling", "coinRate", "hitRate",
})

_SLUG_PAT = re.compile(r"^[a-z0-9_]+$")
_DATE_PAT = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _valid_date(v) -> bool:
    """字面だけでなく実在する日付か（2026-02-31 を通さない）。"""
    if not (_is_str(v) and _DATE_PAT.match(v)):
        return False
    from datetime import date as _d
    try:
        _d(*(int(x) for x in v.split("-")))
        return True
    except ValueError:
        return False
# ホスト名は厳格に（.example / example..com / -example / example- を弾く）。
# クエリ・フラグメントは「受理してから除去する」のが仕様なので、ここでは許容する。
# 各ラベルが英数字で始まり英数字で終わること（-example / example- / a..b を拒否）
_HOST_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
_URL_PAT = re.compile(
    r"^https://(?:" + _HOST_LABEL + r"\.)+[A-Za-z]{2,}(?::\d{1,5})?(?:[/?#][^\s]*)?$")


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
    "checker": False, "checker_modes": [], "checker_is_estimate": False, "hub": HUB_NONE,
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
        if _ok_key(k) and k not in RESERVED_CHECKER_KEYS and v in CHECKER_SHOWABLE)
    checker = bool(public and index and modes)
    if not checker:
        modes = []
    # 数値が裏取り済みでないmodeが1つでもあれば、チェッカーは「目安」扱い（ラベル必須）
    checker_is_estimate = bool(
        checker and isinstance(cm, dict)
        and any(cm.get(k) == "STRUCT_OK" for k in modes))

    affiliate = bool(public and index)
    gates = {
        "lifecycle": lc,
        "public": public,
        "index": index,
        "ads": False,
        "checker": checker,
        "checker_modes": modes,
        "checker_is_estimate": checker_is_estimate,
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
    if g.get("checker_is_estimate") and not g["checker"]:
        raise GateError("不変条件違反: 非表示のcheckerが目安扱いになっている")
    if not g["public"] and g["hub"] != HUB_NONE:
        raise GateError("不変条件違反: 非公開機種を hub に載せない")
    if g["public"] and g["profile"] is None:
        raise GateError("不変条件違反: 公開するのに profile が無い")
    if g["lifecycle"] in PERMANENTLY_CLOSED:
        raise GateError(f"不変条件違反: {g['lifecycle']} は閉鎖中")


# ---------------------------------------------------------------- 原子の分類

def _strip_invisible(s: str) -> str:
    """★不可視文字をUnicodeカテゴリ基準で除去する★
    列挙方式だと U+2066 双方向分離記号などを取りこぼす（Codex 20巡目 #3）。
    Cf(書式), Cc(制御), Co(私用), Cs(サロゲート) と分離子・空白カテゴリを対象にする。
    """
    import unicodedata as _u
    return "".join(ch for ch in s
                   if _u.category(ch) not in ("Cf", "Cc", "Co", "Cs", "Zl", "Zp"))
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
    s = _strip_invisible(s)                   # 不可視文字（カテゴリ基準）
    s = unicodedata.normalize("NFKC", s)      # 全角/半角・互換文字の揺れを吸収
    return re.sub(r"\s+", " ", s).strip()


# 記事本文は innerHTML に入るため、許可タグ以外は公開しない（XSSと検査迂回の防止）
_ALLOWED_TAGS = ("br", "strong", "b", "em", "span")
# 許可される形だけを列挙する（属性を書く余地が構文上ない）
_TAG_OK = re.compile(r"<\s*/?\s*(?:%s)\s*/?\s*>" % "|".join(_ALLOWED_TAGS), re.I)
_DANGEROUS_ATTR = re.compile(r"(?:\bon[a-z]+\s*=|javascript:|data:text/html|<\s*script)", re.I)


# ★公開文字列に含めてはいけない文字（除去ではなく拒否）★
#   Cf(書式・双方向制御) / Cc(制御) / Co(私用) / Cs(サロゲート) / Zl,Zp(行・段落区切り)。
#   通常の日本語記事にこれらが入る理由はない。
_INVISIBLE_CATS = ("Cf", "Cc", "Co", "Cs", "Zl", "Zp")


def invisible_unsafe(text: str) -> str | None:
    """不可視・方向制御文字が含まれていれば理由を返す（原文は返さない）。"""
    if not _is_str(text):
        return None
    import unicodedata as _u
    for ch in text:
        cat = _u.category(ch)
        if cat in _INVISIBLE_CATS and ch not in ("\n", "\t"):
            return (f"不可視・方向制御文字を含む（U+{ord(ch):04X} 分類{cat}）"
                    f"＝画面上の語順が入れ替わり得る")
    return None


def html_unsafe(text: str) -> str | None:
    """公開してよいHTMLか。危険なら理由を返す（無害化ではなく拒否＝fail-closed）。

    ★契約（Codex 21巡目 #5 で厳密化）★
      「<」が出てきたら、それは _ALLOWED_TAGS の開始/終了タグ**だけ**でなければならない。
      属性は値の有無にかかわらず一切許可しない（`<span class>` のような値なし属性も不可）。
      コメント・DOCTYPE・CDATA・処理命令など、タグ以外のHTML構文も通さない。
      生の「<」も通さない（innerHTML では未知タグの開始として解釈され得るため）。
    """
    if not _is_str(text):
        return None
    if _DANGEROUS_ATTR.search(text):
        return "イベント属性・スクリプト等の危険なHTML"
    # ★許可タグを取り除いたあとに「<」が残るなら、それは許可されていないHTML構文★
    rest = _TAG_OK.sub("", text)
    if "<" in rest:
        # どこが問題か分かる程度の理由を返す（原文そのものは返さない）
        m = re.search(r"<\s*/?\s*([A-Za-z][A-Za-z0-9]*)([^<>]*)>", rest)
        if m:
            name = m.group(1).lower()
            if name in _ALLOWED_TAGS:
                return f"タグ属性は許可しない <{name}>"
            return f"許可されていないタグ <{name}>"
        return "タグ以外のHTML構文（コメント・生の「<」等）は許可しない"
    return None
    if _DANGEROUS_ATTR.search(text):
        return "イベント属性・スクリプト等の危険なHTML"
    for m in _TAG_ANY.finditer(text):
        if m.group(1).lower() not in _ALLOWED_TAGS:
            return f"許可されていないタグ <{m.group(1)}>"
        if "=" in (m.group(2) or ""):
            return f"タグ属性は許可しない <{m.group(1)}>"
    return None


def normalize_atom(parts) -> str:
    """表示される塊を正規形に。空要素を除き ' / ' で連結する。"""
    xs = [_to_display(p) for p in parts if _is_str(p)]
    return " / ".join(x for x in xs if x)


def atom_id(text: str, profile: str | None = None) -> str:
    """分類台帳のキー。profile を含めるので preview と legacy の判断を混同しない。"""
    return hashlib.sha256(f"{profile or '-'}|{text}".encode("utf-8")).hexdigest()


# ★否定形は「収益の断定」ではない★（2026-07-27）
#   「ゲーム数狙いではプラス期待値が出ません」は利用者への注意喚起であって、
#   儲かるという主張ではない。これを消すと記事から警告だけが消えて危険になる。
#   ただし1か所でも否定でない出現があれば、その文は断定として扱う（安全側）。
_NEGATION_AFTER = re.compile(
    r"^.{0,14}?(?:出ません|出ない|ありません|無い|ない(?:です)?|入りません|入らない|"
    r"なりません|ならない|見込めません|見込めない|狙えません|狙えない|"
    r"期待できません|期待できない|わけではありません|とは限りません)")


def _asserts_profit(text: str) -> bool:
    """絶対禁止の言い回しが、否定でない形で使われているか。"""
    found = False
    for m in ABSOLUTE_DENY_PAT.finditer(text):
        found = True
        if not _NEGATION_AFTER.match(text[m.end():]):
            return True          # 否定が続かない＝断定として使われている
    return False if found else False


def classify_atom(parts, ledger: dict | None, profile: str | None = None,
                  slug: str | None = None) -> str:
    """表示原子を ALLOW / DROP / UNCLASSIFIED に判定する。

    判定順:
      0. preview で禁止話題を含む → DROP
      1. 絶対禁止／設定段階の非存在断定 → DROP（★台帳ALLOWでも解除できない★）
      2. 台帳 DROP → DROP
      3. 台帳 ALLOW → ALLOW（★slugs 指定があればその機種でだけ有効★）
      4. リスク語を含まない → ALLOW
      5. リスク語ありで未登録 → UNCLASSIFIED

    ★台帳の適用範囲（Codex 23巡目 #13）★
      atom_id は「表示テキスト」だけから作るので、同じ文言が別の機種にもあると
      同じキーになる。機種Aで個別に確かめて ALLOW にした事実が、機種Bへ
      未確認のまま伝播してしまう。これを防ぐため、台帳の項目に

          {"verdict": "ALLOW", "slugs": ["hokuto"]}

      と書けるようにした。slugs があるときは、その機種以外では効かない
      （＝未分類のまま止まる）。slugs が無い項目は「文言そのものが安全」という
      パターン全体への承認として扱う（見出し・スペックのラベル等）。
    """
    text = normalize_atom(parts if isinstance(parts, (list, tuple)) else [parts])
    if not text:
        return ALLOW
    # ★区切り記号を除いた形でも絶対禁止を判定する★
    #   ["期待値が", "プラス"] → "期待値が / プラス" は文字列一致を外れるため、
    #   区切りを詰めた形も併せて見る（台帳ALLOWで通せる穴を塞ぐ）
    # ★区切り記号だけでなく、空白・中黒・読点で分断された形も同じ文として見る★
    #   （Codex 23巡目 #9）「期 待 値 が / プ ラ ス」は人には読めるが、
    #   連続一致だけでは禁止語に当たらない。
    _sep = r"[\s　/／|｜・,、.\-‐―ー~〜]"
    variants = (text,
                re.sub(r"\s*[/／|｜]\s*", "", text),
                re.sub(r"\s*[/／|｜]\s*", " ", text),
                re.sub(_sep + "+", "", text))
    if profile == "preview_basic" and any(PREVIEW_FORBIDDEN_PAT.search(v) for v in variants):
        return DROP
    if any(_asserts_profit(v) or SETTING_DENY_PAT.search(v)
           or _implies_missing_setting(v) for v in variants):
        return DROP
    entry = (ledger or {}).get(atom_id(text, profile))
    verdict = entry.get("verdict") if isinstance(entry, dict) else None
    if verdict == DROP:
        return DROP                      # DROPは範囲を絞らない（安全側）
    if verdict == ALLOW:
        scope = entry.get("slugs") if isinstance(entry, dict) else None
        if scope is None:
            return ALLOW                 # 文言そのものへの承認
        if not isinstance(scope, (list, tuple)):
            return UNCLASSIFIED          # 壊れた台帳は効かせない（fail-closed）
        if slug is not None and slug in scope:
            return ALLOW                 # その機種で確かめた事実
        return UNCLASSIFIED              # 別の機種へは伝播させない
    if not RISK_PAT.search(text):
        return ALLOW
    return UNCLASSIFIED


# ---------------------------------------------------------------- 射影

class _Ctx:
    """射影中の診断を集める（★原文も動的キーの中身も保持しない★）。"""

    def __init__(self, profile: str, ledger: dict | None, slug: str | None = None):
        self.profile = profile
        self.slug = slug
        self.ledger = ledger
        self.unclassified: list[dict] = []
        self.dropped: list[dict] = []
        # ★スキーマ破壊は「内容の判定」と別チャネル。必ずビルドを止める（黙って一部を落とさない）★
        self.errors: list[dict] = []

    def atom(self, parts, path: str) -> bool:
        """表示原子を判定する。落ちた場合は原子ごと出さない。"""
        # ★危険なHTMLは無害化せず拒否（innerHTML へ入るため）★
        for p_ in (parts if isinstance(parts, (list, tuple)) else [parts]):
            if not _is_str(p_):
                continue
            # ★不可視・方向制御文字は「除いて判定」ではなく、含まれた時点で拒否★
            #   （Codex 22巡目 #2）判定用に除去しても公開文字列には残るため、
            #   ブラウザ上で U+202E などにより見た目の語順が逆転し、
            #   「スラプが値待期」が「期待値がプラス」と読める形になり得る。
            why = invisible_unsafe(p_) or html_unsafe(p_)
            if why:
                self.reject(path, why)
                return False
        v = classify_atom(parts, self.ledger, self.profile, self.slug)
        if v == ALLOW:
            return True
        text = normalize_atom(parts if isinstance(parts, (list, tuple)) else [parts])
        rec = {"atom_id": atom_id(text, self.profile)[:16], "path": path}
        (self.unclassified if v == UNCLASSIFIED else self.dropped).append(rec)
        return False

    def reject(self, path: str, reason: str) -> None:
        """スキーマ破壊（未知フィールド・不正形式・構造不整合）。publish を失敗させる。"""
        self.errors.append({"path": path, "reason": reason})

    def content_drop(self, path: str, reason: str) -> None:
        """★方針による除去★（危険な表現を含むので出さない）。データは壊れていないので
        publish は失敗させず、その塊を出さないだけにする。構造エラーと混同しない。"""
        self.dropped.append({"atom_id": None, "path": path, "reason": reason})


# ★節の種別★（2026-08-31・要望③で "table" を足した）
#   ★"table" は バッジも凡例も持たない、ふつうの表★
_SEC_TYPES = ("rumor", "settei", "table")


def _only_keys(d: dict, allowed: set) -> bool:
    """原子の中に未知フィールドが無いこと（あれば原子ごと拒否する）。"""
    return isinstance(d, dict) and set(d.keys()) <= allowed


def _types_ok(ctx: "_Ctx", d: dict, path: str, spec: dict) -> bool:
    """★既知フィールドの型が違えば必ず構造エラーにする（黙って捨てない）★

    もぐら叩きを避けるため、型検査はこの1関数に集約する。
    spec: {フィールド名: 型 or 型のタプル}
    """
    for k, typ in spec.items():
        if k not in d:
            continue
        v = d[k]
        if v is None:
            continue        # null は「その項目は無い」の明示（実データで checker: null が実在）
        if typ in (int, float, (int, float)) and isinstance(v, bool):
            ctx.reject(f"{path}.{k}", "数値フィールドに真偽値")
            return False
        if not isinstance(v, typ):
            ctx.reject(f"{path}.{k}", "既知フィールドの型不正")
            return False
    return True


# --- checker
_MODE_NUM_KEYS = ("excellent", "good", "caution", "limit", "suruMax", "target",
                  "count", "ceiling")
# ★層ごとに「UIが実際に読むキー」だけを許可する（Codex 21巡目 #6）★
#   machine.html が読むのは:
#     mode直下 … excellent/good/caution/target/note/byRate/suru/cycle
#                 ＋ checker[mode].limit（入力上限）・checker[mode].suruMax（回数の上限）
#     行(suru/cycle[]) … count＋閾値＋note＋byRate（行の limit/suruMax や入れ子は読まない）
#     byRate配下 … 閾値＋note（suruMax は読まない）
#   共通の許可契約にすると、UIが参照しない値が公開データに残る。
_MODE_ALLOWED = {"excellent", "good", "caution", "target", "note",
                 "limit", "suruMax", "cycle", "suru", "byRate", "_disabled",
                 "ceiling"}
_ROW_ALLOWED = {"count", "excellent", "good", "caution", "target", "note",
                "byRate", "_disabled"}
_RATE_ALLOWED = {"excellent", "good", "caution", "target", "note"}


def _project_mode(conf, ctx: _Ctx, path: str, ctx_label: str,
                  layer: str = "mode") -> dict | None:
    """mode設定を既知キーのみで再構築。未知キーがあれば mode ごと拒否（黙って捨てない）。

    layer="mode" は mode直下、layer="row" は suru[]/cycle[] の行。
    層によってUIが読むキーが違うので、許可集合を分けている。
    """
    if not isinstance(conf, dict):
        return None
    allowed = _MODE_ALLOWED if layer == "mode" else _ROW_ALLOWED
    if not _only_keys(conf, allowed):
        ctx.reject(path, "UIが参照しないフィールドを含むため拒否")
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
    bad = _count_sanity(conf, path)
    if bad:
        ctx.reject(path, bad)
        return None
    # ★天井は正の数だけ★（2026-08-12。0以下だと「天井まで残り」がマイナスになる）
    if "ceiling" in conf and (not _is_num(conf["ceiling"]) or conf["ceiling"] <= 0):
        ctx.reject(f"{path}.ceiling", "天井が正の数でない")
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
        if layer != "mode":     # 行の中にさらに行は持てない（許可集合でも塞いでいる）
            ctx.reject(f"{path}.{field}", "行の入れ子は公開しない")
            return None
        if not isinstance(seq, list) or not seq:
            ctx.reject(f"{path}.{field}", f"{field}の型不正")
            return None
        if field == "cycle" and all(_is_num(x) for x in seq):
            # ★数値だけの周期配列は現行UIが解釈できない（配線後に判定が壊れる）★
            #   Phase 1 では拒否し、周期は行（count＋閾値）形式に統一する。
            ctx.reject(f"{path}.cycle", "数値配列の周期は未対応（count付きの行で持つこと）")
            return None
        rows = []
        for i, x in enumerate(seq):
            if not isinstance(x, dict):
                ctx.reject(f"{path}.{field}[{i}]", "配列要素が辞書でない")
                return None
            if "_disabled" in x:
                # 停止マーカーだけ落として数値行を公開する経路を塞ぐ
                ctx.reject(f"{path}.{field}[{i}]", "停止マーカー(_disabled)付きの行は公開しない")
                return None
            r = _project_mode(x, ctx, f"{path}.{field}[{i}]", ctx_label, layer="row")
            if r is None:
                ctx.content_drop(f"{path}.{field}[{i}]", "行が公開基準を満たさない（部分的に出さない）")
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
            # ★数値の型不正も止める（"600" のような文字列が黙って消えると
            #   他の交換率だけ残り「1つでも落ちたらmodeごと」の約束が破れる）★
            for nk in ("excellent", "good", "caution", "target"):
                if nk in rv and not _is_num(rv[nk]):
                    ctx.reject(f"{path}.byRate.{rk}.{nk}", "数値フィールドの型不正")
                    return None
            r = {k: rv[k] for k in ("excellent", "good", "caution", "target")
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


# 入力軸（何を数えて入力するか）と判定軸の対応。
# Phase 0 の事故＝「回数入力なのにG数の閾値で判定」を機械的に防ぐ（方針書§6 条件3）。
_COUNT_AXIS_MODES = ("suru", "through", "cycle")   # 回数・周期で数えるmode
_AXIS_MAX_COUNT = 30                                # 回数系の閾値がこれを超えたらG数の混入を疑う


def _count_sanity(conf: dict, where: str) -> str | None:
    """回数・上限の健全性（有限・非負・整数）。UIの入力上限に使われるため実害が出る。"""
    import math
    for k in ("limit", "suruMax"):
        v = conf.get(k)
        if v is None:
            continue
        if not _is_num(v) or not math.isfinite(v) or v < 0 or float(v) != int(v):
            return f"{where} の {k} が0以上の整数でない"
    return None


def _judgeable(conf: dict) -> bool:
    """UIが全入力域で判定を確定できるか。

    machine.html の判定は good を主軸にし、無いと目安値が undefined になる箇所がある
    （Codex 20巡目 #9）。よって good を判定材料の必須条件にする。
    """
    # 早見表（renderEvTable）は good と excellent の両方で行を作る。
    # good だけだと「○ 目安以上」の行が作られず、表が欠ける（Codex 21巡目 #3）。
    return _is_num(conf.get("good")) and _is_num(conf.get("excellent"))


def _rate_inheritance_gap(conf: dict, rate_keys, where: str,
                          base_rate=None) -> str | None:
    """★交換率別の値があるのに一部の交換率が欠けている状態を止める★（Codex 22巡目 #6）

    UIは byRate に該当キーが無ければ mode直下の値へ落ちる。これが「意図した継承」なのか
    「入力漏れ」なのかはデータから区別できない。

    ただし1つだけ例外を認める：checker に `baseRateKey` が明示されていて、
    欠けているのがちょうどその交換率だけの場合は「mode直下の値がその交換率の値」
    という**宣言された**取り決めとして扱う（Codex 24巡目 #6）。
    以前は defaultRate で代用していたが、既定＝最初に選ぶ交換率にすぎず、
    直下の値がその交換率だという証明にならないので分離した。
    """
    by = conf.get("byRate") if isinstance(conf.get("byRate"), dict) else {}
    if not by or not rate_keys:
        return None
    missing = [k for k in rate_keys if not isinstance(by.get(k), dict)]
    if not missing:
        return None
    if base_rate is not None and missing == [base_rate]:
        return None          # 宣言された基準交換率 ＝ mode直下の値
    return (f"{where} は交換率別の値を持つのに {missing} が無い"
            f"（画面では別の交換率の値で判定される）")


def _judgeable_all_rates(conf: dict, rate_keys) -> bool:
    """★交換率を選び直しても判定材料が揃っているか★（Codex 21巡目 #2）

    UI は mode直下に byRate を重ねて使う。どれか1つの交換率にだけ good があっても、
    別の交換率を選んだ瞬間に目安値が確定しなくなる。よって「宣言された全交換率」の
    実効configが判定可能であることを条件にする。byRate が無い場合は直下だけを見る。
    """
    by = conf.get("byRate") if isinstance(conf.get("byRate"), dict) else {}
    if not by:
        return _judgeable(conf)
    # 宣言された交換率（無ければ byRate のキー）すべてを見る
    keys = list(rate_keys) if rate_keys else list(by.keys())
    for k in keys:
        rv = by.get(k)
        eff = {**conf, **rv} if isinstance(rv, dict) else conf
        if not _judgeable(eff):
            return False
    # byRate 側に宣言外のキーがあっても、それを選べる実装ではないので見ない
    return True


def _threshold_int(conf: dict, where: str, unit) -> str | None:
    """★閾値は整数であること（単位を問わない）★（Codex 22巡目 #7 / 24巡目 #7）

    早見表は小数をそのまま「600.5〜」と表示するが、利用者の入力は parseInt される。
    表示と判定がずれるので小数は公開しない。入力は単位に関係なく parseInt なので、
    G系だけでなく pt・周期・あべし でも同じ扱いにする。
    """
    for k in ("caution", "good", "target", "excellent"):
        v = conf.get(k)
        if _is_num(v) and float(v) != int(v):
            return f"{where} の {k}={v} が整数でない（入力は整数に丸められるため判定がずれる）"
    return None


def _threshold_sanity(conf: dict, where: str) -> str | None:
    """閾値の健全性（有限・非負・caution<=good<=excellent の順序）を検査する。

    順序が壊れていると判定が反転する（浅いのに「◎」になる等）。実害が出るので構造エラー。
    """
    import math
    vals = {}
    for k in ("caution", "good", "excellent", "target"):
        v = conf.get(k)
        if v is None:
            continue
        if not _is_num(v) or not math.isfinite(v):
            return f"{where} の {k} が有限の数値でない"
        if v < 0:
            return f"{where} の {k} が負の値"
        vals[k] = v
    order = [vals[k] for k in ("caution", "good", "excellent") if k in vals]
    if order != sorted(order):
        return f"{where} の閾値の順序が壊れている（caution<=good<=excellent）: {order}"
    return None


_SENTINEL = 99999


# 要約の自由文に書かれた「◯◯G〜」を拾う（単位は checker.unit も許す）
_SUMMARY_NUM = re.compile(r"(\d{2,5})\s*(?:G|g|pt|周期|あべし)")


def _effective_thresholds(checker: dict, rate_key: str, base_rate) -> set:
    """その交換率を選んだときに、実際に判定へ使われる閾値をすべて集める。"""
    vals: set = set()
    for k, v in checker.items():
        if not isinstance(v, dict) or k in ("modeData", "byRate"):
            continue
        units = v.get("suru") or v.get("cycle") or [v]
        for u in units:
            if not isinstance(u, dict):
                continue
            over = (u.get("byRate") or {}).get(rate_key) if rate_key != base_rate else None
            eff = {**u, **(over or {})}
            for kk in ("caution", "good", "target", "excellent"):
                if _is_num(eff.get(kk)):
                    vals.add(int(eff[kk]))
    return vals


def _summary_conflict(machine: dict) -> str | None:
    """★要約の狙い目と、チェッカーの判定値が食い違っていないか★（Codex 25巡目 #4）

    同じ画面で「580Gから狙い目」と書きながら、その交換率のチェッカーが590Gで
    判定していると、580Gと入力した利用者は「目安の手前」と言われる。
    どちらの数字が外部的に正しいかは決めない（それはPhase 2）。ここで止めるのは
    **同じ画面状態での食い違い**だけ。
    """
    checker = machine.get("checker") if isinstance(machine.get("checker"), dict) else None
    sbr = machine.get("strategyByRate")
    if not checker or not isinstance(sbr, dict):
        return None
    base = checker.get("baseRateKey")
    for rk, txt in sbr.items():
        if not _is_str(txt):
            continue
        vals = _effective_thresholds(checker, rk, base)
        if not vals:
            continue
        miss = [int(n) for n in _SUMMARY_NUM.findall(txt) if int(n) not in vals]
        if miss:
            return (f"要約の狙い目({rk})に、その交換率のチェッカーが持たない数字がある"
                    f"（同じ画面で判定と食い違う）: {sorted(set(miss))}")
    return None


def _rate_sync_gap(machine: dict) -> str | None:
    """★交換率を切り替えたとき、要約が連動しない状態を止める★（Codex 25巡目 #3）

    UIは strategyByRate があるときだけ要約の狙い目を更新する。交換率ごとに
    チェッカーの値が違うのに要約が無いと、別の交換率を選んでも古い数字が残る。
    """
    checker = machine.get("checker") if isinstance(machine.get("checker"), dict) else None
    if not checker:
        return None
    rates = [r.get("key") for r in (checker.get("exchangeRates") or [])
             if isinstance(r, dict) and isinstance(r.get("key"), str)]
    if len(rates) < 2 or isinstance(machine.get("strategyByRate"), dict):
        return None
    base = checker.get("baseRateKey")
    # ★UIは交換率別の狙い目文が無ければ、選択中のチェッカーから組み立てる★
    #   （machine.html buildTargetFromChecker）。したがって「文が無いこと」自体は
    #   契約違反ではない。組み立ての材料（表示するmodeの good）が交換率ごとに
    #   取れることだけを条件にする。取れないと要約が空になるか古い値が残る。
    for r in rates:
        ok = False
        for m_ in (checker.get("modes") or []):
            if not isinstance(m_, dict):
                continue
            conf = checker.get(m_.get("key"))
            if not isinstance(conf, dict):
                continue
            rows = conf.get("suru") if isinstance(conf.get("suru"), list) else (
                conf.get("cycle") if isinstance(conf.get("cycle"), list) else None)
            unit = rows[0] if rows else conf
            if not isinstance(unit, dict):
                continue
            over = (unit.get("byRate") or {}).get(r) if r != base else None
            if _is_num({**unit, **(over or {})}.get("good")):
                ok = True
                break
        if not ok:
            return (f"交換率 {r} を選んだときに要約へ出す狙い目が取れない"
                    f"（交換率を変えても古い数字が残る）")
    return None


def _row_coverage_gap(mode_key: str, conf: dict) -> str | None:
    """★入力できる回数がすべて行で覆われているか★（Codex 22巡目 #5）

    machine.html の getConfig は、要求した回数の行が無ければ
    「その回数以下で最大の行」→無ければ「先頭行」へ落とす。
    したがって 0スルーの行が無いデータで 0 を入力すると、1スルーの閾値で
    「目安に到達」と表示される。確認していない回数を別の回数の値で判定させない。

    - スルー: 0 から suruMax（無ければ最大count）まで、欠番があれば止める
    - 周期  : 1 から最大count まで連番（既存の連番検査と同じ範囲）
    """
    for field, first in (("suru", 0), ("cycle", 1)):
        rows = conf.get(field)
        if not isinstance(rows, list) or not rows:
            continue
        counts = [r.get("count") for r in rows if isinstance(r, dict)]
        if not all(isinstance(c, int) and not isinstance(c, bool) for c in counts):
            return None          # 型の問題は別の検査が止める
        top = conf.get("suruMax") if field == "suru" else max(counts)
        want = set(range(first, int(top) + 1))
        missing = sorted(want - set(counts))
        if missing:
            return (f"mode({mode_key})の{field}に回数 {missing} の行が無い"
                    f"（その回数を入力すると別の回数の閾値で判定される）")
    return None


def _sentinel_protocol(machine: dict, mode_key: str, conf: dict) -> str | None:
    """★99999（天井なし＝設定狙い専用）の取り決めが崩れていないか★（Codex 21巡目 #7）

    machine.html は `machine.limit が無い && checker.normal.excellent >= 99999` のときだけ
    「設定狙い専用」表示に切り替える。それ以外の場所にセンチネルが混ざると、
    到達できない閾値がそのまま判定に使われる。
    """
    def _sent(c):
        return [k for k in ("caution", "good", "excellent")
                if _is_num(c.get(k)) and c[k] >= _SENTINEL]
    units = [conf] + [u for u in (conf.get("suru") or conf.get("cycle") or [])
                      if isinstance(u, dict)]
    for u in units:
        cfgs = [u] + [{**u, **rv} for rv in (u.get("byRate") or {}).values()
                      if isinstance(rv, dict)]
        for c in cfgs:
            got = _sent(c)
            if not got:
                continue
            if mode_key != "normal" or machine.get("limit") is not None or len(got) != 3:
                return (f"mode({mode_key})のセンチネル値(99999)が「天井なし」の取り決めの形に"
                        f"なっていない（設定狙い専用は normal・limit無し・3値そろい）")
    return None


def _limit_vs_threshold(machine: dict, checker: dict, mode_key: str, conf: dict) -> str | None:
    """UIの入力上限（machine.limit → checker.limit → mode.limit）と閾値の整合。

    上限より大きい閾値は入力できず、その判定に到達できない（Codex 20巡目 #8）。
    """
    lim = None
    ml = machine.get("limit")
    if _is_num(ml):
        lim = ml
    elif isinstance(ml, dict) and _is_num(ml.get(mode_key)):
        lim = ml[mode_key]
    elif _is_num(checker.get("limit")):
        lim = checker["limit"]
    elif _is_num(conf.get("limit")):
        lim = conf["limit"]
    if lim is None:
        return None
    def _chk(c, where):
        # ★excellent も上限検査の対象に戻す（Codex 21巡目 #1）★
        #   machine.html の早見表は `excellent G〜（天井 limit G）` という行を生成するため、
        #   excellent > limit だと「760G〜（天井700G）」という矛盾した行が表示される。
        #   入力もクランプされ到達できないので、これは誤情報になる。
        for k in ("caution", "good", "target", "excellent"):
            v = c.get(k)
            if _is_num(v) and v != _SENTINEL and v > lim:
                return f"{where} の {k}={v} が入力上限 {lim} を超える（判定に到達できない）"
        return None
    for i, u in enumerate(conf.get("suru") or conf.get("cycle") or [conf]):
        if not isinstance(u, dict):
            continue
        bad = _chk(u, f"{mode_key}[{i}]")
        if bad:
            return bad
        for rk, rv in (u.get("byRate") or {}).items():
            if isinstance(rv, dict):
                bad = _chk({**u, **rv}, f"{mode_key}[{i}].byRate.{rk}")
                if bad:
                    return bad
    return None


def _axis_conflict(mode_key: str, conf: dict, unit: str | None,
                   declared_flags: dict | None = None,
                   rate_keys=None, base_rate=None) -> str | None:
    """入力軸と判定軸の食い違いを、閾値の大小ではなく**構造**で検出する。

    ★実データで確認した判別条件（2026-07-27）★
      Phase 0 で停止した20モード：直下に閾値(6/5/4＝スルー回数)を持ち、unit は 'G'。
        → 利用者はG数を入力するのに、閾値は回数。これが「回数入力なのにG数判定」の実体。
      正常な16モード（valvrave2 等）：直下に閾値を**持たず**、入れ子の suru[]/cycle[] に
        「回数ごとのG数」を持つ。これが正しい二軸構造。

      よって判別は「回数系modeが直下に閾値を持っているか」で書ける。
      閾値の大小で判定してはいけない（大きければ誤検知686件・小さければ事故を見逃す）。

    ★この検査は _disabled マーカーに依存しない★
      停止マーカーを消しても、構造そのものが不整合なら必ず止まる。
      （Codex 10巡目の指摘：人が付けた印だけを根拠にしてはいけない）
    """
    # 回数軸とみなす条件: mode名 または hasSuru/hasCycle 宣言
    has_suru_flag = conf.get("hasSuru") is True or (declared_flags or {}).get("hasSuru") is True
    has_cycle_flag = conf.get("hasCycle") is True or (declared_flags or {}).get("hasCycle") is True
    is_count = mode_key in _COUNT_AXIS_MODES or has_suru_flag or has_cycle_flag

    suru_rows = conf.get("suru") if isinstance(conf.get("suru"), list) else None
    cycle_rows = conf.get("cycle") if isinstance(conf.get("cycle"), list) else None
    # ★宣言と実体の対応を検査する（hasSuruなのに cycle[] を持つ等の取り違えを止める）★
    if has_suru_flag and has_cycle_flag:
        return f"mode({mode_key})が hasSuru と hasCycle を同時に宣言している（軸が一意でない）"
    if has_suru_flag and cycle_rows is not None:
        return f"mode({mode_key})は hasSuru 宣言だが cycle[] を持つ（宣言と実体の不一致）"
    if has_cycle_flag and suru_rows is not None:
        return f"mode({mode_key})は hasCycle 宣言だが suru[] を持つ（宣言と実体の不一致）"
    # ★逆方向も検査（行を持つのに宣言が無い＝UIが回数入力欄を出さない）★
    # ★mode名だけの例外を廃止★ UIは宣言フラグだけで入力欄を出すため、
    #   宣言が無いと行はあるのにカウンターが出ず、初期行以外を選べない。
    if suru_rows is not None and not has_suru_flag:
        return f"mode({mode_key})は suru[] を持つのに hasSuru 宣言が無い（UIに入力欄が出ない）"
    if cycle_rows is not None and not has_cycle_flag:
        return f"mode({mode_key})は cycle[] を持つのに hasCycle 宣言が無い（UIに入力欄が出ない）"
    if suru_rows is not None and cycle_rows is not None:
        return f"mode({mode_key})が suru[] と cycle[] の両方を持つ（軸が一意でない）"

    rows = suru_rows if suru_rows is not None else cycle_rows
    direct = [k for k in ("excellent", "good", "caution", "target") if _is_num(conf.get(k))]

    if not is_count:
        # G数軸のmodeに回数の入れ子を持たせるのも軸の混在
        if rows is not None and not direct:
            return (f"mode({mode_key})が回数の行だけを持ち、G数の閾値を持たない"
                    f"＝入力軸が判別できない")
        # ★判定材料が無いmodeを公開しない（noteだけのタブを出さない）★
        gap = _rate_inheritance_gap(conf, rate_keys, f"mode({mode_key})", base_rate)
        if gap:
            return gap
        if not _judgeable_all_rates(conf, rate_keys):
            return (f"mode({mode_key})に判定の主軸(good/excellent)が無い交換率がある"
                    f"（その交換率を選ぶと判定が確定しない）")
        bad = _threshold_sanity(conf, mode_key) or _threshold_int(conf, mode_key, unit)
        if bad:
            return bad
        by = conf.get("byRate") if isinstance(conf.get("byRate"), dict) else {}
        if isinstance(by, dict):
            for rk, rv in by.items():
                if isinstance(rv, dict):
                    bad = _threshold_sanity(rv, f"{mode_key}.byRate.{rk}")
                    if bad:
                        return bad
                    # ★UIは mode直下に byRate を重ねる。重ねた後の順序も検査★
                    _eff = {**conf, **rv}
                    bad = (_threshold_sanity(_eff, f"{mode_key}.byRate.{rk}(適用後)")
                           or _threshold_int(_eff, f"{mode_key}.byRate.{rk}(適用後)", unit))
                    if bad:
                        return bad
        return None

    # --- ここから回数軸のmode ---
    if rows is not None and direct:
        # ★直下閾値と入れ子の併存は、どちらで判定されるか決まらない★
        return (f"回数系mode({mode_key})が直下閾値{direct}と行の両方を持つ"
                f"＝判定軸が一意に決まらない")
    if rows is None:
        if direct:
            return (f"回数系mode({mode_key})が直下に閾値{direct}を持つ"
                    f"（入力単位={unit!r}）＝入力軸と判定軸の食い違い。"
                    f"回数ごとの閾値は suru[]/cycle[] の行として持つこと")
        # 閾値も行も無い（noteだけ等）＝判定できないのに表示対象になる
        return f"回数系mode({mode_key})に判定材料が無い（閾値も行も持たない）"

    # ★回数系modeの直下 byRate はUIが参照しない（getConfigが選択行だけを返すため）★
    if isinstance(conf.get("byRate"), dict) and conf["byRate"]:
        return (f"回数系mode({mode_key})の直下に byRate がある"
                f"（UIは行のbyRateしか使わないため反映されない）")

    # --- 入力単位は必須（欠落を素通りさせない）---
    if not _is_str(unit) or unit not in ("G", "g"):
        return (f"回数系mode({mode_key})の入力単位が{unit!r}＝"
                f"行ごとのG数閾値と単位が一致しない（回数系modeでは 'G' が必須）")

    # ★スルーは入力できる上限を明示させる★（Codex 23巡目 #5）
    #   UIは mode直下に suruMax が無ければ上限99で入力させる。上限が決まらないと
    #   「行の無い回数」が別の行の閾値で判定される。実データは全modeが suruMax を持つ。
    if suru_rows is not None and not _is_num(conf.get("suruMax")):
        return (f"回数系mode({mode_key})に suruMax が無い"
                f"（画面は99スルーまで入力でき、行の無い回数が別の行で判定される）")

    # ★行・byRate・マージ後の閾値健全性も検査する★
    #   UIは mode直下に byRate を重ねて使うので、重ねた後の順序が壊れていないかを見る。
    for i, row in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(row, dict):
            continue
        bad = (_threshold_sanity(row, f"{mode_key}[{i}]")
               or _threshold_int(row, f"{mode_key}[{i}]", unit))
        if bad:
            return bad
        for rk, rv in (row.get("byRate") or {}).items():
            if not isinstance(rv, dict):
                continue
            bad = _threshold_sanity(rv, f"{mode_key}[{i}].byRate.{rk}")
            if bad:
                return bad
            _eff = {**row, **rv}
            bad = (_threshold_sanity(_eff, f"{mode_key}[{i}].byRate.{rk}(適用後)")
                   or _threshold_int(_eff, f"{mode_key}[{i}].byRate.{rk}(適用後)", unit))
            if bad:
                return bad

    # --- 行の契約: count は必須・0以上の"整数型"・一意・昇順／各行にG数の判定材料が要る ---
    counts = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            return f"回数系mode({mode_key})の行[{i}]が辞書でない"
        cv = row.get("count")
        # ★型そのものが int であること（1.0 のような小数表記を通さない）★
        if type(cv) is not int or isinstance(cv, bool):
            return f"回数系mode({mode_key})の行[{i}]の count={cv!r} が整数でない（回数が特定できない）"
        if cv < 0:
            return f"回数系mode({mode_key})の行[{i}]の count={cv} が0以上でない"
        counts.append(cv)
        # ★各行にG数の判定材料が最低1つ必要（countだけの行は判定できない）★
        gap = _rate_inheritance_gap(row, rate_keys, f"mode({mode_key})の行[{i}]",
                                    base_rate)
        if gap:
            return gap
        if not _judgeable_all_rates(row, rate_keys):
            return (f"回数系mode({mode_key})の行[{i}]に判定の主軸(good/excellent)が無い交換率がある"
                    f"（その交換率を選ぶと判定が確定しない）")
    is_cycle = isinstance(conf.get("cycle"), list)
    if counts:
        if is_cycle:
            # 周期UIは 1..配列長 を選ばせる。count がその範囲外だと到達できない
            if sorted(counts) != list(range(1, len(counts) + 1)):
                return (f"周期mode({mode_key})の count が 1..{len(counts)} の連番でない: {counts}"
                        f"（UIは配列長で選択肢を作るため到達できない行が出る）")
        else:
            # ★スルーUIの実装（machine.html getConfig）を確認済み★
            #   「要求count以下で最大の行 → 無ければ最浅の行」へフォールバックする設計。
            #   よって先頭が0でなくても、0スルーは最浅行が使われ判定は成立する
            #   （実データ sao/bandori/hanma_baki が count=1 始まり）。
            #   ここで見るべきは「行が昇順で、上限を超える到達不能行が無いこと」だけ。
            pass
    cap = conf.get("suruMax") if _is_num(conf.get("suruMax")) else (99 if not is_cycle else None)
    if _is_num(cap) and counts and max(counts) > cap:
        return (f"回数系mode({mode_key})の行に上限({cap})を超える count={max(counts)} がある"
                f"＝UIで選べず到達できない")
    if len(set(counts)) != len(counts):
        return f"回数系mode({mode_key})の count が重複している: {counts}"
    if counts != sorted(counts):
        return f"回数系mode({mode_key})の count が昇順でない: {counts}"
    return None


def _project_checker(checker, allowed_modes: list[str], ctx: _Ctx,
                     machine_ref: dict | None = None) -> dict | None:
    if not allowed_modes:
        return None
    # ★VERIFIED指定なのに checker 本体が無い／型不正は矛盾（gateは開くのに中身が出ない）★
    if not isinstance(checker, dict):
        ctx.reject("checker", "VERIFIED指定だが checker 本体が無い/辞書でない")
        return None
    if "modes" in checker and not isinstance(checker["modes"], list):
        ctx.reject("checker.modes", "modesの型不正")
        return None
    # ★checker直下の未知フィールドを黙って捨てない★
    #   辞書だからといって mode 候補として通さない（"warning": {"text": "未確認"} を防ぐ）。
    #   許されるのは 予約キー / VERIFIED指定された mode名 / modeData配下のmode名 のみ。
    if "modeData" in checker and not isinstance(checker["modeData"], dict):
        ctx.reject("checker.modeData", "modeDataが辞書でない")
        return None
    for _k, _v in (checker.get("modeData") or {}).items():
        if not isinstance(_v, dict):
            ctx.reject(f"checker.modeData.{_k}", "modeDataの値が辞書でない")
            return None
    md_keys = set(checker.get("modeData").keys()) if isinstance(checker.get("modeData"), dict) else set()
    # ★宣言されたmodeのconfigが非dictなら、その時点で構造矛盾として止める★
    for _m in (checker.get("modes") or []):
        if isinstance(_m, dict) and isinstance(_m.get("key"), str):
            _k = _m["key"]
            if _k in checker and not isinstance(checker[_k], dict):
                ctx.reject(f"checker.{_k}", "宣言されたmodeのconfigが辞書でない")
                return None
    known = RESERVED_CHECKER_KEYS | set(allowed_modes) | md_keys
    for k, v in checker.items():
        if k in known:
            continue
        if isinstance(v, dict) and "_disabled" in v:
            continue                     # Phase 0で意図的に停止したmode
        ctx.reject(f"checker.{k}", "checker直下の未知フィールド（mode候補として黙って通さない）")
        return None
    for k, typ in (("unit", str), ("equivOnly", bool), ("exchangeRates", list),
                   ("defaultRate", str), ("baseRateKey", str), ("ok", str), ("ng", str),
                   ("limit", (int, float)), ("hasSuru", bool), ("hasCycle", bool),
                   ("suruMax", (int, float)), ("modeData", dict),
                   ("ceiling", (int, float)), ("coinRate", (int, float)),
                   ("hitRate", (int, float))):
        if k in checker and not isinstance(checker[k], typ):
            ctx.reject(f"checker.{k}", "既知フィールドの型不正")
            return None
        if k in ("limit", "suruMax", "ceiling", "coinRate", "hitRate") and k in checker \
                and isinstance(checker[k], bool):
            ctx.reject(f"checker.{k}", "数値フィールドに真偽値")
            return None
    bad = _count_sanity(checker, "checker")
    if bad:
        ctx.reject("checker", bad)
        return None
    out: dict = {}
    # ★単位・ラベルが落ちたら checker 全体を閉じる★
    #   （識別子と数値だけ残ると、何の数字か分からないまま公開される）
    if _is_str(checker.get("unit")):
        if not ctx.atom([checker["unit"]], "checker.unit"):
            ctx.content_drop("checker", "単位が公開できないため checker ごと除去")
            return None
        out["unit"] = checker["unit"]
    if isinstance(checker.get("equivOnly"), bool):
        out["equivOnly"] = checker["equivOnly"]
    if _is_num(checker.get("limit")):
        out["limit"] = checker["limit"]
    # ★天井・50枚あたりG数（2026-08-12）★
    #   表は「天井 − 現在G」を出すので、0以下だと残りゲーム数がマイナスになる。
    #   丸めた入力上限を天井として使わないよう、値は別フィールドで持つ。
    for _k in ("ceiling", "coinRate", "hitRate"):
        if _k in checker:
            _min = 1 if _k == "hitRate" else 0
            if not _is_num(checker[_k]) or checker[_k] <= _min:
                # ★hitRateは確率の分母★＝1未満だと 1/hitRate が1を超え、
                #   期待ゲーム数の計算が壊れる（2026-08-12・依頼163）
                ctx.reject(f"checker.{_k}", "天井・コイン持ち・初当たりが正の数でない")
                return None
            out[_k] = checker[_k]
    # ★UIが参照しないフィールドは公開しない（公開契約と消費契約を一致させる）★
    #   判定文は固定文言・カウンター表示は modes[] のフラグ・上限は mode直下の suruMax を使う。
    for lab in ("ok", "ng"):
        if lab in checker:
            continue        # 実データに存在するが未使用。公開射影には含めない
        if _is_str(checker.get(lab)):
            # ★判定ラベルが落ちたら checker ごと閉じる（判定文が消えた表示にしない）★
            if not ctx.atom([checker[lab]], f"checker.{lab}"):
                ctx.content_drop("checker", "判定ラベルが公開できないため checker ごと除去")
                return None
            out[lab] = checker[lab]
    # checker直下の hasSuru/hasCycle/suruMax はUIが参照しない（modes[]とmode直下を使う）

    # ★equivOnly（等価前提）と交換率の選択肢は同時に持てない★（Codex 23巡目 #7）
    #   UIは equivOnly が真なら判定文・早見表に「（等価）」を固定で付ける。
    #   5.6枚を選べる状態でこれが出ると、選んだ交換率と表示が食い違う。
    if checker.get("equivOnly") is True and (checker.get("exchangeRates")
                                             or checker.get("defaultRate")):
        ctx.reject("checker.equivOnly",
                   "等価前提(equivOnly)なのに交換率を選べる（選択と表示が食い違う）")
        return None

    # ★入力単位は必須★（Codex 24巡目 #7）
    #   UIは `checker.unit || "G"` で補うため、pt/周期の機種で unit を書き忘れると
    #   画面にはG数として出る。表示すると空になる値も認めない。
    if not _is_str(checker.get("unit")) or not normalize_atom([checker["unit"]]).strip():
        ctx.reject("checker.unit", "入力単位(unit)が無い（画面ではG数として表示される）")
        return None

    er = checker.get("exchangeRates")
    if isinstance(er, list):
        rates = []
        for i, r in enumerate(er):
            # ★1要素でも不正なら全体を止める（部分削除すると選べる交換率が黙って減る）★
            if not (isinstance(r, dict) and _ok_key(r.get("key"))):
                ctx.reject(f"checker.exchangeRates[{i}]", "交換率の要素が不正")
                return None
            if not _only_keys(r, {"key", "label"}):
                ctx.reject(f"checker.exchangeRates[{i}]", "未知フィールドを含むため拒否")
                return None
            if not _types_ok(ctx, r, f"checker.exchangeRates[{i}]", {"key": str, "label": str}):
                return None
            e = {"key": r["key"]}
            if not _is_str(r.get("label")) or not normalize_atom([r["label"]]).strip():
                ctx.reject(f"checker.exchangeRates[{i}].label", "交換率の表示ラベルが無い/表示すると空")
                return None
            if _is_str(r.get("label")):
                if not ctx.atom([r["label"]], f"checker.exchangeRates[{i}].label"):
                    ctx.content_drop("checker", "交換率ラベルが公開できないため checker ごと除去")
                    return None
                e["label"] = r["label"]
            if any(x["key"] == e["key"] for x in rates):
                ctx.reject(f"checker.exchangeRates[{i}]", "交換率のkeyが重複している")
                return None
            if any(normalize_atom([x.get("label") or ""]) == normalize_atom([e.get("label") or ""])
                   for x in rates):
                ctx.reject(f"checker.exchangeRates[{i}].label", "交換率の表示ラベルが重複している")
                return None
            rates.append(e)
        if rates:
            out["exchangeRates"] = rates
    # ★参照整合性は exchangeRates の有無に関わらず検査する★
    #   （選択肢が無い/空なのに defaultRate だけある場合も黙って消さない）
    dr = checker.get("defaultRate")
    if dr is not None:
        if not (_is_str(dr) and any(r["key"] == dr for r in out.get("exchangeRates") or [])):
            ctx.reject("checker.defaultRate", "既定の交換率が選択肢に存在しない")
            return None
        out["defaultRate"] = dr

    decl = checker.get("modes")
    if not isinstance(decl, list) or not decl:
        # ★modes宣言を必須にする（無いと宣言・ラベル検査を丸ごと迂回できる）★
        ctx.reject("checker.modes", "modes宣言が無い（表示するmodeを明示すること）")
        return None
    if isinstance(decl, list):
        kept = []
        seen_decl_keys: set = set()
        for i, m in enumerate(decl):
            if not isinstance(m, dict) or not _ok_key(m.get("key")):
                ctx.reject(f"checker.modes[{i}]", "modes宣言の要素が不正")
                return None
            if not _only_keys(m, {"key", "label", "hasSuru", "hasCycle"}):
                ctx.reject(f"checker.modes[{i}]", "未知フィールドを含むため拒否")
                return None
            if not _types_ok(ctx, m, f"checker.modes[{i}]",
                             {"key": str, "label": str, "hasSuru": bool, "hasCycle": bool}):
                return None
            # ★重複検査は「公開対象外を除外する前」に行う（非表示mode同士の重複も拾う）★
            if m["key"] in seen_decl_keys:
                ctx.reject(f"checker.modes[{i}]", "modes宣言に重複したkey")
                return None
            seen_decl_keys.add(m["key"])
            if m["key"] not in allowed_modes:
                continue                 # 表示対象でないmodeの宣言は出さない（正常）
            e = {"key": m["key"]}
            if not _is_str(m.get("label")) or not normalize_atom([m["label"]]).strip():
                ctx.reject(f"checker.modes[{i}].label", "modeの表示ラベルが無い/表示すると空")
                return None
            if _is_str(m.get("label")):
                if not ctx.atom([m["label"]], f"checker.modes[{i}].label"):
                    ctx.content_drop("checker", "modeラベルが公開できないため checker ごと除去")
                    return None
                e["label"] = m["label"]
            for flag in ("hasSuru", "hasCycle"):
                if isinstance(m.get(flag), bool):
                    e[flag] = m[flag]
            if any(normalize_atom([x.get("label") or ""]) == normalize_atom([e.get("label") or ""])
                   for x in kept):
                ctx.reject(f"checker.modes[{i}].label", "modeの表示ラベルが重複している")
                return None
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
        # ★入力軸と判定軸の整合検査（Phase 0の事故型を機構で防ぐ）★
        # modes宣言の中身（hasSuru/hasCycle）から回数軸かどうかを拾う。
        # declared は key の集合なので、宣言そのものは decl から探す。
        _decl = next((m for m in (decl if isinstance(decl, list) else [])
                      if isinstance(m, dict) and m.get("key") == key), {})
        # ★宣言された交換率の集合を渡す（どの交換率を選んでも判定できることを条件にする）★
        _rate_keys = [r.get("key") for r in (checker.get("exchangeRates") or [])
                      if isinstance(r, dict) and isinstance(r.get("key"), str)]
        axis = _axis_conflict(key, conf, checker.get("unit"), _decl, _rate_keys,
                              checker.get("baseRateKey"))
        if axis:
            ctx.reject(f"checker.{key}", axis)
            continue
        sent = _sentinel_protocol(machine_ref or {}, key, conf)
        if sent:
            ctx.reject(f"checker.{key}", sent)
            continue
        gap = _row_coverage_gap(key, conf)
        if gap:
            # 入力できる回数の一部に「その回数の行」が無い。UIは近い行へ落とすため、
            # 確認していない回数を別の回数の閾値で判定してしまう（Codex 22巡目 #5）。
            # データの型は壊れていないので、その mode を出さない扱いにする。
            ctx.content_drop(f"checker.{key}", gap)
            continue
        bad = _limit_vs_threshold(machine_ref or {}, checker, key, conf)
        if bad:
            # ★データの型は正しく「値が到達不能」なだけなので、記事ごと止めずに
            #   その mode を出さない（内容による除去）。機種ページ自体は公開する。★
            ctx.content_drop(f"checker.{key}", bad)
            continue
        before = len(ctx.errors)
        pm = _project_mode(conf, ctx, f"checker.{key}", key)
        if pm is None:
            # データが壊れている場合は _project_mode が既に reject 済み。
            # そうでなく「内容が公開基準を満たさない」だけなら方針による除去として扱う。
            if len(ctx.errors) == before:
                ctx.content_drop(f"checker.{key}",
                                 "modeの内容が公開基準を満たさない（部分的に出さない）")
            continue
        out[key] = pm
    # ★宣言と実体を一致させる★
    #   内容除去で一部modeのconfigだけ消えると「タブは出るが判定できない」状態になる。
    #   実際に出せた mode だけを宣言に残し、公開ゲート側にもその集合を返す。
    # ★交換率の到達性★ 選択肢のどれを選んでも、そのmodeで閾値に到達できること
    rate_keys = [r["key"] for r in out.get("exchangeRates") or []]
    if not rate_keys:
        # 選択肢が無いのに交換率別の判定値だけある＝どれも選べず到達不能
        for mk in [k for k in allowed_modes if k in out]:
            def _has_by(c):
                if isinstance(c, dict) and isinstance(c.get("byRate"), dict) and c["byRate"]:
                    return True
                for seq in ("suru", "cycle"):
                    for r in (c.get(seq) or []) if isinstance(c.get(seq), list) else []:
                        if isinstance(r, dict) and isinstance(r.get("byRate"), dict) and r["byRate"]:
                            return True
                return False
            if _has_by(out[mk]):
                ctx.reject(f"checker.{mk}.byRate",
                           "交換率別の判定値があるのに選択肢(exchangeRates)が無い")
                return None
    if rate_keys:
        def _units(conf: dict):
            """判定値を持つ単位（mode直下、または回数系の各行）を列挙する。"""
            rows = conf.get("suru") if isinstance(conf.get("suru"), list) else                 (conf.get("cycle") if isinstance(conf.get("cycle"), list) else None)
            return rows if rows else [conf]

        for mk in [k for k in allowed_modes if k in out]:
            for i, unit_conf in enumerate(_units(out[mk])):
                if not isinstance(unit_conf, dict):
                    continue
                by = unit_conf.get("byRate") if isinstance(unit_conf.get("byRate"), dict) else {}
                has_base = _is_num(unit_conf.get("good"))
                def _rate_has_value(rv):
                    # ★交換率を選んだときの実効configに good があること★
                    #   （good が無いとUIの目安値が未確定になる）
                    return isinstance(rv, dict) and _is_num({**unit_conf, **rv}.get("good"))
                for rk in rate_keys:
                    # ★キーが在るだけでなく「判定値がある」ことを要求★
                    if _rate_has_value(by.get(rk)) or has_base:
                        continue
                    ctx.reject(f"checker.{mk}[{i}].byRate",
                               f"交換率 {rk} を選ぶと判定値に到達できない（基準値も無い）")
                    return None
                for rk in by:
                    if rk not in rate_keys:
                        ctx.reject(f"checker.{mk}[{i}].byRate.{rk}",
                                   "選択肢に無い交換率の判定値がある（到達不能）")
                        return None

    live = [k for k in allowed_modes if k in out]
    if not live:
        return None                      # 1つも出せないなら checker ごと出さない
    if isinstance(out.get("modes"), list):
        kept = [m for m in out["modes"] if m.get("key") in live]
        if kept:
            out["modes"] = kept
        else:
            out.pop("modes", None)
    out["_live_modes"] = live            # 呼び出し側が gates を整合させるための内部連絡（後で除去）
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
        # type は enum。誤型なら tables/rows が黙って消えるので構造エラーにする
        if "type" in sec and sec["type"] not in _SEC_TYPES:
            ctx.reject(f"{p}.type", "セクション種別が未知の値")
            continue
        title = sec.get("title")
        if not (_is_str(title) and title.strip()):
            # ★見出しの欠落・空文字は構造エラー（黙って落とさない）★
            ctx.reject(f"{p}.title", "セクション見出しが無い/空")
            continue
        # settei 以外に表データが置かれているのは構造の取り違え
        if sec.get("type") not in ("settei", "table") \
                and ("tables" in sec or "rows" in sec):
            ctx.reject(p, "表を持てない種別のセクションに表データがある")
            continue
        if not ctx.atom([title], f"{p}.title"):
            continue                                  # 見出しが落ちたらセクションごと落とす
        new: dict = {"title": title}
        if sec.get("type") in _SEC_TYPES:
            new["type"] = sec["type"]

        # ★UIが描かない組み合わせを公開しない★（Codex 23巡目 #2）
        #   machine.html の settei 分岐は body を一切描かず、tables があれば rows も描かない。
        #   「以下の数値は未確認です」のような但し書きが公開JSONにだけ残る事故を止める。
        if sec.get("type") == "settei" and sec.get("body"):
            ctx.reject(f"{p}.body", "設定表セクションの本文は画面に描かれない（別セクションにする）")
            continue
        if sec.get("tables") and sec.get("rows"):
            ctx.reject(f"{p}.rows", "tables と rows の併存（画面は rows を描かない）")
            continue

        body = sec.get("body")
        if isinstance(body, list):
            kept, lost = [], False
            for j, el in enumerate(body):
                if not _is_str(el):
                    ctx.reject(f"{p}.body[{j}]", "文字列でない本文要素")
                    continue
                # ★見出し＋段落を結合して判定（単体では無害な組み合わせ断定を捕まえる）★
                if ctx.atom([title, el], f"{p}.body[{j}]"):
                    kept.append(el)
                else:
                    lost = True
            # ★1段落でも落ちたらセクションごと落とす★
            #   「580G〜です」「期待収支は算出していません」のうち但し書きだけ消えると
            #   意味が反転する。兄弟段落との関係を保証できないため塊ごと出さない。
            if lost:
                ctx.content_drop(p, "同じセクション内に公開できない段落があるためセクションごと除去")
                continue
            if kept:
                new["body"] = kept

        if new.get("type") == "table":
            # ★★ふつうの表を射影に残す★★（2026-08-31・Codexの指摘）
            #   ★直す前は settei だけを射影していた★ので、
            #   ふつうの表は `tables` が公開データに入らず、
            #   最後の「中身がある節だけ出す」判定で**節ごと消えた**。
            #   ＝契約は通るのに読者には表が出ない。
            tables = sec.get("tables")
            if not isinstance(tables, list) or not tables:
                ctx.reject(p, "表の種別なのに表がない")
                continue
            kt = [t for t in (_project_plain_table(tb, ctx,
                                                   f"{p}.tables[{ti}]", title)
                              for ti, tb in enumerate(tables)) if t]
            if len(kt) != len(tables):
                # ★1つでも落ちたら節ごと落とす★（表が虫食いで出ない）
                ctx.content_drop(p, "公開できない表があるため節ごと除去")
                continue
            new["tables"] = kt

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


# UIが装飾を持っている示唆の強さ（machine.html の badgeClass と一致させること）
_BADGE_VALUES = ("hint", "weak", "mid", "strong", "ok")


def _cell_text(c, ctx: _Ctx, path: str):
    """セルを (表示文字列, 出力値) に。未知フィールドがあれば None。"""
    if _is_str(c):
        return c, c
    if isinstance(c, dict):
        if not _only_keys(c, _CELL_ALLOWED):
            ctx.reject(path, "未知フィールドを含むセル")
            return None, None
        if "badge" in c and not _is_str(c["badge"]):
            ctx.reject(f"{path}.badge", "badgeの型不正（示唆の強さを失って本文だけ残るのを防ぐ）")
            return None, None
        # ★UIが知っている値だけ許す★（Codex 23巡目 #4）
        #   machine.html の badgeClass は hint/weak/mid/strong/ok のみ。綴り違いは
        #   `|| ""` で無視され、強示唆の装飾が黙って消える（強度情報の欠落）。
        if "badge" in c and c["badge"] not in _BADGE_VALUES:
            ctx.reject(f"{path}.badge",
                       "未知のbadge値（画面で示唆の強さが表示されない）: "
                       + _ci_redact(c["badge"]))
            return None, None
        txt = c.get("text")
        if not _is_str(txt):
            ctx.reject(path, "セルのtextが文字列でない")
            return None, None
        cell = {"text": txt}
        if _is_str(c.get("badge")):
            cell["badge"] = c["badge"]
        return normalize_atom([c.get("badge"), txt]), cell
    ctx.reject(path, "セルが文字列でも辞書でもない")
    return None, None


def _project_plain_table(tbl, ctx: _Ctx, path: str,
                         section_title: str) -> dict | None:
    """★ふつうの表★を公開射影へ写す（2026-08-31・要望③）。

    ★settei と違うところ★
      ・セルは**文字だけ**（バッジの辞書は受け取らない）
      ・`label` は無くてもよい（項目と値だけの表に見出しは要らない）
      ・行の長さは見出しと同じでなければならない
    ★危ない表現の判定（atom）は settei と同じように通す★
      ＝1つでも落ちたら表ごと落とす（虫食いの表を出さない）。
    """
    if not isinstance(tbl, dict):
        ctx.reject(path, "表が辞書でない")
        return None
    if not _only_keys(tbl, _TABLE_ALLOWED):
        ctx.reject(path, "未知フィールドを含むため表ごと拒否")
        return None
    if not _types_ok(ctx, tbl, path, {"label": str, "headers": list,
                                      "rows": list, "note": str,
                                      "wide": bool}):
        return None
    headers = tbl.get("headers")
    if not isinstance(headers, list) or not headers:
        ctx.reject(f"{path}.headers", "表の列見出しが無い（描画が止まる）")
        return None
    if not all(_is_str(h) for h in headers):
        ctx.reject(f"{path}.headers", "見出しに非文字列が含まれる")
        return None
    if not ctx.atom([section_title, *headers], f"{path}.headers"):
        return None
    out: dict = {"headers": list(headers)}
    label = tbl.get("label")
    if _is_str(label) and label.strip():
        if not ctx.atom([section_title, label], f"{path}.label"):
            return None
        out["label"] = label
    kept = []
    for ri, row in enumerate(tbl.get("rows") or []):
        cells = row if isinstance(row, list) else [row]
        if not all(_is_str(c) for c in cells):
            ctx.reject(f"{path}.rows[{ri}]", "ふつうの表のセルは文字だけ")
            return None
        if len(cells) != len(headers):
            ctx.reject(f"{path}.rows[{ri}]", "行の列数が見出しと違う")
            return None
        if not ctx.atom([section_title, *cells], f"{path}.rows[{ri}]"):
            return None                    # ★1行でも落ちたら表ごと落とす★
        kept.append(list(cells))
    if not kept:
        ctx.reject(f"{path}.rows", "表に行が無い")
        return None
    out["rows"] = kept
    note = tbl.get("note")
    if _is_str(note) and note.strip():
        if not ctx.atom([section_title, note], f"{path}.note"):
            return None
        out["note"] = note
    return out


def _project_settei_table(tbl, ctx: _Ctx, path: str, section_title: str) -> dict | None:
    if not isinstance(tbl, dict):
        ctx.reject(path, "表が辞書でない")
        return None
    if not _only_keys(tbl, _TABLE_ALLOWED):
        ctx.reject(path, "未知フィールドを含むため表ごと拒否")
        return None
    if not _types_ok(ctx, tbl, path, {"label": str, "headers": list, "rows": list,
                                      "note": str, "wide": bool}):
        return None
    out: dict = {}
    # ★UIは tbl.label を無条件に表示し、tbl.headers.map(...) を呼ぶ★（Codex 23巡目 #3）
    #   欠けていると undefined が見出しに出るか、描画処理がそこで止まる。
    if not _is_str(tbl.get("label")) or not tbl["label"].strip():
        ctx.reject(f"{path}.label", "表の見出し(label)が無い（画面に undefined が出る）")
        return None
    if not isinstance(tbl.get("headers"), list) or not tbl["headers"]:
        ctx.reject(f"{path}.headers", "表の列見出し(headers)が無い（描画が止まる）")
        return None
    label = tbl.get("label")
    if _is_str(label):
        if not ctx.atom([section_title, label], f"{path}.label"):
            return None                                # 表の見出しが落ちたら表ごと落とす
        out["label"] = label
    headers = tbl.get("headers")
    head_txt = []
    if isinstance(headers, list):
        # ★1つでも非文字列なら表ごと止める（見出しだけ黙って消えて行が残るのを防ぐ）★
        if not all(_is_str(h) for h in headers):
            ctx.reject(f"{path}.headers", "見出しに非文字列が含まれる")
            return None
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
            if not ok:
                return None              # セル不正は既に reject 済み
            if not vals:
                continue
            if not ctx.atom([section_title, label if _is_str(label) else None,
                             *head_txt, *texts], f"{path}.rows[{ri}]"):
                # ★1行でも落ちたら表ごと落とす★（示唆の一部だけ消えると誤誘導になる）
                ctx.content_drop(f"{path}.rows[{ri}]", "公開できない行があるため表ごと除去")
                return None
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
            # ★UI・静的ビルダーが実際に読む形だけを許す★（2026-07-27）
            #   machine.html / build_machine_pages.py はどちらも辞書行から
            #   row.trigger / row.hint しか読まない。それ以外の形（left/right,
            #   label/value, title/badge/value）は **表が全行空欄で描画される**。
            #   実際に super_rio_ace2・takt_opus・shaman_king・tenken・valvrave の
            #   計28行が本番で空欄表示になっていた（Codex 22巡目 #4 から発覚）。
            for keys in ({"trigger", "hint"},):
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
        if not ok:
            return None                   # セル不正は既に reject 済み
        # ★UIは先頭2セルしか描かない（Codex 22巡目 #4）★
        #   machine.html / build_machine_pages.py とも row[0], row[1] だけを出す。
        #   3セル目に「未確認」のような但し書きがあると、それだけが黙って消える。
        if len(vals) > 2:
            ctx.reject(f"{path}[{ri}]",
                       f"表の行が3セル以上（画面は先頭2セルしか描かないため"
                       f"{len(vals) - 2}セルが黙って消える）")
            return None
        if not vals:
            continue
        if not ctx.atom([section_title, *texts], f"{path}[{ri}]"):
            # ★1行でも落ちたら表相当ごと落とす（示唆の一部だけ消えると誤誘導になる）★
            ctx.content_drop(f"{path}[{ri}]", "公開できない行があるため表ごと除去")
            return None
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
        if not _types_ok(ctx, s, f"sources[{i}]",
                         {"url": str, "title": str, "confirmed_at": str}):
            continue
        url = s.get("url")
        if not (_is_str(url) and _URL_PAT.match(url)):
            # ★URLの欠落も構造エラー（出典なのに参照先が無い）★
            ctx.reject(f"sources[{i}].url", "URLが無い/httpsの想定形式でない")
            continue
        # ★クエリ・フラグメントを落とす★（署名付きURLや ?token=… を出典として
        #   貼ったときに、そのまま公開される事故を防ぐ。悪意なく普通に起こる）
        e = {"url": re.split(r"[?#]", url, 1)[0]}
        if _is_str(s.get("title")) and ctx.atom([s["title"]], f"sources[{i}].title"):
            e["title"] = s["title"]
        if s.get("confirmed_at") is not None:
            if not _valid_date(s["confirmed_at"]):
                ctx.reject(f"sources[{i}].confirmed_at", "確認日の形式が不正（YYYY-MM-DD）")
                continue
            e["confirmed_at"] = s["confirmed_at"]
        out.append(e)
    return out or None


_MACHINE_TYPES = {
    "slug": str, "name": str, "manufacturer": str, "info": str, "strategy": str,
    "tenjo_display": str, "release_date": str, "confirmed_at": str,
    "aliases": list, "sources": list, "seo": dict, "original": dict,
    "strategyByRate": dict, "checker": dict,
    # ★機種の型式（identity v2・2026-07-30）★
    #   メーカーIDと型式コードで「同じ台」を決める。これが未知フィールド扱いだと、
    #   machines.json に足した途端に公開射影が全機種を拒否する。
    "identity": dict,
}
# authoring 側に存在してよいキー（公開射影に出さないものも含む）。
#   lifecycle/checker_modes … Phase 1 の状態軸
#   status/limit … 旧形式の状態と入力上限
_MACHINE_KNOWN = set(_MACHINE_TYPES) | {
    "limit", "status", "lifecycle", "checker_modes",
    # ★緊急停止スイッチ（Codex 23巡目 #12）★
    #   ゲート算出では正式に見ているのに allowlist に無く、立てた途端に
    #   GateError で「止めた版がデプロイできない」状態になっていた。
    "checker_kill_switch",
}


def _project_machine(machine: dict, gates: dict, ctx: _Ctx) -> dict:
    profile = gates["profile"]
    out: dict = {}
    # ★機種フィールドの既知型不正は構造エラーにする（黙って落とさない）★
    if not _types_ok(ctx, machine, "machine", _MACHINE_TYPES):
        return out
    # ★表示面どうしの整合（Codex 25巡目 #3・#4）★
    for _bad in (_summary_conflict(machine), _rate_sync_gap(machine)):
        if _bad:
            ctx.reject("strategyByRate", _bad)
            return out

    # ★未知フィールドを黙って捨てない（Codex 22巡目 #3）★
    #   例:「strategy_note: 誤記のため公開禁止」のような注記が黙って消え、
    #   strategy だけが表示される事故を防ぐ。authoring 用の既知キーは列挙する。
    # ★1件目で打ち切らず、未知フィールドは全部挙げてから止める★（Codex 17巡目 (b)-2）
    unknown = [k for k in machine if k not in _MACHINE_KNOWN]
    for k in unknown:
        # ★キー名そのものに原稿を入れられる★（Codex 18巡目 (a)-5）
        #   公開されるCIログに出さないよう、CIでは指紋に置き換える。
        ctx.reject(f"machine.{_ci_redact(k)}", "未知フィールド（公開対象か判断できない）")
    if unknown:
        return out
    lim = machine.get("limit")
    if "limit" in machine and lim is not None and not (_is_num(lim) or isinstance(lim, dict)):
        ctx.reject("machine.limit", "limitが数値でも辞書でもない")
        return out

    def s(field):
        v = machine.get(field)
        if _is_str(v) and v.strip() and ctx.atom([v], field):
            out[field] = v

    # ★slug は公開物の同定子。欠落・不正のまま公開しない★
    if not (_is_str(machine.get("slug")) and _SLUG_PAT.match(machine["slug"])):
        ctx.reject("slug", "slugが欠落または不正（公開物を同定できない）")
        return out
    out["slug"] = machine["slug"]
    # 機種名は公開物の必須項目（欠落・空文字のまま公開しない）
    if not (_is_str(machine.get("name")) and normalize_atom([machine["name"]]).strip()):
        # ★ゼロ幅文字だけの名前など、表示すると空になるものも拒否★
        ctx.reject("name", "機種名が無い/表示すると空になる")
        return out
    s("name")
    if "name" not in out:
        ctx.reject("name", "機種名が公開できない（内容除去された）")
        return out
    s("manufacturer")
    for f in ("release_date", "confirmed_at"):
        if f in machine and machine[f] is not None:
            if not _valid_date(machine[f]):
                ctx.reject(f, "日付が不正（YYYY-MM-DD・実在する日付）")
                return out
            out[f] = machine[f]
    src = _project_sources(machine.get("sources"), ctx)
    if src:
        out["sources"] = src
    s("info")

    # ★★公開状態（先行記事かどうか）は必ず射影に出す★★
    #   （2026-07-30・Codex 14巡目 (a)-2）
    #   status を落としていたため、公開データを受け取る側からは
    #   **先行記事（解析待ち）が通常記事と見分けられなかった**。
    #   その結果、noindex も「⚠先行記事」バナーも付かないページが作られる。
    st = machine.get("status")
    if st is not None:
        if not (_is_str(st) and st in ("complete", "preview")):
            ctx.reject("status", "status は complete / preview のいずれか")
            return out
        out["status"] = st

    if profile == "preview_basic":
        return out

    # --- legacy_safe
    s("strategy")
    s("tenjo_display")
    al = machine.get("aliases")
    if isinstance(al, list):
        if not all(_is_str(x) for x in al):
            ctx.reject("aliases", "別名に文字列でない要素がある")
            return out
        kept = [x for j, x in enumerate(al) if ctx.atom([x], f"aliases[{j}]")]
        if kept:
            out["aliases"] = kept
    # ★checker を先に射影し、到達性は「実際に公開される集合」で見る★（Codex 21巡目 #4）
    #   内容除去で mode や交換率が消えたあとに、それを指す limit/strategyByRate が
    #   公開データへ残ると、UIから到達できない値が公開されたままになる。
    pc = _project_checker(machine.get("checker"), gates.get("checker_modes", []), ctx, machine)
    live_modes = {k for k in gates.get("checker_modes", []) if isinstance(pc, dict) and k in pc}
    live_rates = {r.get("key") for r in ((pc or {}).get("exchangeRates") or [])
                  if isinstance(r, dict)}

    lim = machine.get("limit")
    if _is_num(lim):
        if not (math.isfinite(lim) and lim >= 0 and float(lim) == int(lim)):
            ctx.reject("limit", "limitが0以上の整数でない（UIの入力上限が壊れる）")
            return out
        out["limit"] = lim
    elif isinstance(lim, dict):
        if not all(_ok_key(k) and _is_num(v) and float(v) == int(v) and v >= 0
                   and math.isfinite(v) for k, v in lim.items()):
            ctx.reject("limit", "limit辞書に不正なキーまたは非数値がある")
            return out
        # ★集合が空でも迂回しない（空＝どのキーも到達できない）★
        #   到達できないキーはデータの型が壊れているわけではないので、記事ごと止めず
        #   「その値を公開しない」扱いにする（内容除去）。
        reach = {k: v for k, v in lim.items() if k in live_modes}
        if len(reach) != len(lim):
            ctx.content_drop("limit", "公開されるmodeに無いキーは参照されない")
        if reach:
            out["limit"] = reach
    sbr = machine.get("strategyByRate")
    if isinstance(sbr, dict):
        if not all(_ok_key(k) and _is_str(v) for k, v in sbr.items()):
            ctx.reject("strategyByRate", "交換率キーが識別子でない、または値が文字列でない")
            return out
        # ★キー到達性★ 公開される交換率に無いキーは参照されない（空集合でも迂回しない）
        reach = {k: v for k, v in sbr.items() if k in live_rates}
        if len(reach) != len(sbr):
            ctx.content_drop("strategyByRate", "公開される交換率に無いキーは参照されない")
        # ★逆向きの欠落も見る★（Codex 24巡目 #6）
        #   選べる交換率に対応する文が無いと、その交換率を選んだとき狙い目欄だけが
        #   更新されず、別の交換率の値が残る（bandori の既定=equal で実発生）。
        #   基準交換率（baseRateKey）は machine.strategy が担うので除く。
        _ck = machine.get("checker") if isinstance(machine.get("checker"), dict) else {}
        _base = _ck.get("baseRateKey")
        _gap = sorted(r for r in live_rates if r not in sbr and r != _base)
        if _gap:
            ctx.reject("strategyByRate",
                       f"選べる交換率に対応する狙い目の文が無い（画面が更新されない）: {_gap}")
            return out
        d = {k: v for k, v in reach.items() if ctx.atom([k, v], f"strategyByRate.{k}")}
        if d:
            out["strategyByRate"] = d
    seo = machine.get("seo")
    if isinstance(seo, dict):
        if not _only_keys(seo, {"title", "description"}):
            ctx.reject("seo", "未知フィールドを含むSEO情報")
            return out
        if not _types_ok(ctx, seo, "seo", {"title": str, "description": str}):
            return out
        d = {k: seo[k] for k in ("title", "description")
             if _is_str(seo.get(k)) and ctx.atom([seo[k]], f"seo.{k}")}
        if d:
            out["seo"] = d
    if gates.get("affiliate_original") and isinstance(machine.get("original"), dict):
        o = machine["original"]
        if not _only_keys(o, {"title", "kind", "search"}):
            ctx.reject("original", "未知フィールドを含む原作情報")
            return out
        if not _types_ok(ctx, o, "original", {"title": str, "kind": str, "search": str}):
            return out
        d = {k: o[k] for k in ("title", "kind", "search")
             if _is_str(o.get(k)) and ctx.atom([o[k]], f"original.{k}")}
        if d:
            out["original"] = d
    if pc:
        out["checker"] = pc

    return out


def _project_detail(detail, gates: dict, ctx: _Ctx) -> dict:
    if gates["profile"] == "preview_basic":
        return {}
    if detail is None:
        return {}
    # ★記事が別の機種のものでないことを確かめる★（Codex 25巡目 #4 の最小検査5）
    #   ファイルの取り違えは「機種Aのページに機種Bの解説が出る」という
    #   最も分かりやすい誤情報になる。射影の入口で止める。
    if isinstance(detail, dict) and _is_str(detail.get("slug")) and ctx.slug:
        if detail["slug"] != ctx.slug:
            ctx.reject("detail.slug",
                       "記事データの機種が一致しない（別機種の記事が付いている）")
            return {}

    if not isinstance(detail, dict):
        # ★記事データが壊れているのに「本文なしで正常公開」しない★
        ctx.reject("detail", "記事データが辞書でない")
        return {}
    out: dict = {}
    lead = detail.get("lead")
    if _is_str(lead) and lead.strip() and ctx.atom([lead], "lead"):
        out["lead"] = lead

    # ★記事直下の未知フィールドを黙って捨てない★
    #   slug/name/updated は同定・更新日の管理用、evTable はJS側が実行時に組み立てる作業用。
    #   いずれも公開射影には含めないが、authoring 上は正当なので既知として扱う。
    if not _only_keys(detail, {"lead", "summaryBoxes", "factTable", "sections",
                               "slug", "name", "updated", "evTable"}):
        ctx.reject("detail", "記事直下に未知フィールド")
        return out
    if not _types_ok(ctx, detail, "detail",
                     {"lead": str, "summaryBoxes": list, "factTable": list, "sections": list}):
        return out
    boxes = detail.get("summaryBoxes")
    if isinstance(boxes, list):
        kept = []
        for i, b in enumerate(boxes):
            if not isinstance(b, dict):
                ctx.reject(f"summaryBoxes[{i}]", "要約欄の要素が辞書でない")
                continue
            if not _only_keys(b, {"label", "value"}):
                ctx.reject(f"summaryBoxes[{i}]", "未知フィールドを含むため箱ごと拒否")
                continue
            if not _types_ok(ctx, b, f"summaryBoxes[{i}]", {"label": str, "value": str}):
                continue
            lb, vl = b.get("label"), b.get("value")
            if not (_is_str(lb) and _is_str(vl)):
                ctx.reject(f"summaryBoxes[{i}]", "label または value が欠落している")
                continue
            # ★label+value を結合して判定（「期待値」＋「580G〜」を見逃さない）★
            if ctx.atom([lb, vl], f"summaryBoxes[{i}]"):
                kept.append({"label": lb, "value": vl})
        if kept:
            out["summaryBoxes"] = kept

    ft = detail.get("factTable")
    if isinstance(ft, list):
        rows = []
        for i, r in enumerate(ft):
            if not isinstance(r, list) or len(r) != 2:
                # ★3列目の但し書きを切り捨てない／不正要素を黙って飛ばさない★
                ctx.reject(f"factTable[{i}]", "2要素の配列でない行")
                continue
            th, td = r
            if not (_is_str(th) and _is_str(td)):
                ctx.reject(f"factTable[{i}]", "セルが文字列でない")
                continue
            if ctx.atom([th, td], f"factTable[{i}]"):
                rows.append([th, td])
        if rows:
            out["factTable"] = rows

    secs = _project_sections(detail.get("sections"), ctx)
    if secs:
        out["sections"] = secs
    return out


# ---------------------------------------------------------------- 公開API

# ★数値の表記は算用数字だけではない（Codex 22巡目 #8）★
#   漢数字・丸数字・上付き/下付き数字・ローマ数字も「数値情報」として扱う。
#   これを取りこぼすと「天井九百九十九G」に目安ラベルが付かない。
_NUM_IN_TEXT = re.compile(
    r"[0-9０-９]"                       # 算用数字（半角・全角）
    r"|[一二三四五六七八九十百千万零壱弐参拾]"   # 漢数字
    r"|[①-⓿]"                  # 丸数字・囲み数字
    r"|[⁰-₟]"                  # 上付き・下付き数字
    r"|[Ⅰ-ⅿ]"                  # ローマ数字
)


def _has_numeral(text: str) -> bool:
    """★判定は numerals.py に1本化★（2026-07-30）

    以前はここで unicodedata の数値属性を見ていたが、audit_public は
    文字を列挙した別方式で見ていた。Python 3.13（Unicode 15.1）で
    「京」に数値属性が付き、**「東京喰種」を数値ありと判定して**
    独立監査と食い違い、CIでだけ止まり続けた。
    同じ意味の判定を2か所に書かない。
    """
    from numerals import has_numeral as _hn
    return _hn(text)


def _numeric_surfaces(pm: dict, pd: dict) -> list[str]:
    """公開物のうち、実際に数値が載っている表示面を列挙する（目安ラベルの必要判定）。"""
    found: list[str] = []

    def has_num(node) -> bool:
        if _is_str(node):
            return _has_numeral(node)
        if _is_num(node):
            return True
        if isinstance(node, list):
            return any(has_num(x) for x in node)
        if isinstance(node, dict):
            return any(has_num(v) for v in node.values())
        return False

    for k, v in pm.items():
        # ★同定子・見出しは対象外★
        #   機種名やSEOタイトルに含まれる数字は「絆2」「SAO Ⅱ」のような名前の一部であって
        #   判断に使う数値ではない。ここを数値面にすると機種名の隣に目安ラベルを
        #   求めることになり、意味が合わない（Codex 22巡目 #8 の対応で漢数字・
        #   ローマ数字も拾うようになったため、除外を明示する）。
        if k in ("slug", "name", "seo", "aliases", "release_date", "confirmed_at",
                 "disclaimer", "display_requirements"):
            continue
        if k == "sources":
            # URL・確認日は対象外だが、出典タイトルの数値は表示されるので対象にする
            if any(has_num(s.get("title", "")) for s in v if isinstance(s, dict)):
                found.append("sources.title")
            continue
        if has_num(v):
            found.append(k)
    for k, v in pd.items():
        if has_num(v):
            found.append(f"detail.{k}")
    return sorted(found)


def publish_view(machine: dict, detail: dict | None = None,
                 ledger: dict | None = None, allow_drops: bool = False) -> dict:
    """validate → compute → project を不可分に実行。★外から gates を渡せない★

    - 検証エラー（lifecycle欠落・型不正など）は GateError（黙ってCANDIDATE扱いにしない）
    - 未分類のリスク原子があれば GateError（黙って公開しない）
    """
    errs = validate_machine(machine)
    if errs:
        raise GateError("スキーマ検証エラー: " + " / ".join(errs))   # ★全件（Codex 16巡目 (b)-2）
    gates = compute_gates(machine)
    if not gates["public"]:
        return {"gates": gates, "machine": {}, "detail": {}}

    ctx = _Ctx(gates["profile"], ledger, machine.get("slug"))
    pm = _project_machine(machine, gates, ctx)
    pd = _project_detail(detail, gates, ctx)
    # ★方針による除去でcheckerが空になったら、ゲート表示も閉じて自己矛盾を残さない★
    if gates["checker"] and "checker" not in pm:
        gates = {**gates, "checker": False, "checker_modes": [], "checker_is_estimate": False}
        assert_invariants(gates)
    # ★一部modeだけ消えた場合も、宣言・ゲート・実体を一致させる★
    #   （「タブは出るが中身が無い」状態を公開しない）
    elif isinstance(pm.get("checker"), dict) and "_live_modes" in pm["checker"]:
        live = pm["checker"].pop("_live_modes")
        if sorted(live) != sorted(gates["checker_modes"]):
            cm = machine.get("checker_modes") or {}
            gates = {**gates, "checker_modes": sorted(live),
                     "checker_is_estimate": any(cm.get(k) == "STRUCT_OK" for k in live)}
            assert_invariants(gates)

    # ★スキーマ破壊は内容判定と別チャネルで必ず止める★
    # ★理由は全件出す★（Codex 16巡目 (b)-2）1件だけだと直すたびに再実行が要る。
    if ctx.errors:
        detail = " / ".join(f"path={e['path']} 理由={e['reason']}" for e in ctx.errors)
        raise GateError(f"{machine.get('slug','?')}: 構造エラー {len(ctx.errors)}件 → 公開不可"
                        f" [{detail}]")
    if ctx.unclassified:
        # ★atom_id は安定した生SHAなので、候補が少ない原稿は総当たりで当てられる★
        #   （Codex 20巡目 (a)-4）CIでは実行ごとの鍵で伏せる。
        detail = " / ".join(
            f"path={u['path']} id={_ci_redact(u['atom_id'])}" for u in ctx.unclassified)
        raise GateError(
            f"{machine.get('slug','?')}: 未分類のリスク表現 {len(ctx.unclassified)}件 → 公開不可"
            f"（分類台帳に ALLOW/DROP を登録すること） [{detail}]")

    # ★方針による除去（DROP）が残っている原稿は公開しない★（Codex 24巡目 #5）
    #   従来は「その塊を出さない」だけで公開していたが、兄弟の但し書きが消えて
    #   数値だけ残るような意味反転を、除去の粒度によっては防ぎきれない。
    #   ゼロ基準で運用するので、除去が1件でもあれば原稿を直させる（ビルドを止める）。
    #   ※どうしても除去のまま出す必要が生じたら、台帳で ALLOW/DROP を明示すること。
    if ctx.dropped and not allow_drops:
        detail = " / ".join(
            f"path={d['path']} 理由={d.get('reason', '公開基準を満たさない表現')}"
            for d in ctx.dropped)
        raise GateError(
            f"{machine.get('slug','?')}: 公開できない表現 {len(ctx.dropped)}件 → 公開不可"
            f"（原稿を直すこと） [{detail}]")

    # ★狙い目・数値を出すなら「当サイトの目安」表示を必須要件として明示する★
    #   machine側だけでなく detail 側（要約・表・本文）だけに数値がある場合も対象にする。
    # ★「キーがあるか」ではなく「実際に数値が載っているか」で判定する★
    #   文字だけの本文に一律で目安ラベルを付けるのは品質を損ね、
    #   info/seo/original に数値がある場合を見落とすため。
    surfaces = _numeric_surfaces(pm, pd)
    # 数値未裏取りのチェッカーを出す場合は、必ず目安ラベルの対象にする
    if gates.get("checker_is_estimate") and "checker" in pm and "checker" not in surfaces:
        surfaces.append("checker")
        surfaces.sort()
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
    ctx = _Ctx(gates["profile"], ledger, machine.get("slug"))
    _project_machine(machine, gates, ctx)
    _project_detail(detail, gates, ctx)
    return {"gates": gates, "errors": ctx.errors, "unclassified": ctx.unclassified,
            "dropped": ctx.dropped, "ok": not (ctx.unclassified or ctx.errors)}


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    import json

    def bl_provisional_lifecycle(m):
        """★本体（build_ledger.provisional）そのものを呼ぶ★（Codex 21巡目 #9）

        以前は同じロジックをここに書き写していたため、本体が壊れても自己試験だけ
        合格し得た。ローカル編集ツール側の関数なので import できないときは
        「検証していない」を明示するために失敗させる。
        """
        import os
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import build_ledger
        return build_ledger.provisional(m).get("lifecycle")
    def _pv(*a, **k):
        """★selftest用★ 内容除去そのものを検証したい場面が多いので、
        既定では除去を許して射影結果を見る。除去でビルドが止まることは
        下の 24-5 の試験で別途確かめる。"""
        k.setdefault("allow_drops", True)
        return publish_view(*a, **k)

    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    LEG = "LEGACY_SEARCH"
    base = {"slug": "x", "lifecycle": LEG, "name": "テスト機"}

    # ===== ふつうの表（2026-08-31・要望③）=====
    #   ★★許可値に足すだけでは、表が公開データから消える★★
    #     （射影の分岐が settei だけだったので、節ごと落ちていた。
    #       Codexの指摘を自分で再現して直した）
    def _tbl_sections(sec):
        ledg = {"x": {"ALLOW": True}}
        v = publish_view({**base}, {"sections": [sec]},
                         {"allow_all": True})
        return (v["detail"] or {}).get("sections")

    _plain = {"title": "基本スペック", "type": "table",
              "tables": [{"label": "", "headers": ["項目", "内容"],
                          "rows": [["機種名", "テスト機"],
                                   ["メーカー", "サミー"]]}]}
    _got = _tbl_sections(_plain)
    t("★★ふつうの表が公開データに残る★★（許可値だけ足すと節ごと消えていた）",
      bool(_got) and _got[0].get("tables")
      and _got[0]["tables"][0]["rows"] == [["機種名", "テスト機"],
                                          ["メーカー", "サミー"]])
    def _tbl_stops(sec):
        """その表が公開を止めるか（構造エラーは例外で来る）。"""
        try:
            return not (_tbl_sections(sec) or [])
        except GateError:
            return True

    t("★ふつうの表のセルにバッジの辞書を入れたら止める★",
      _tbl_stops({"title": "x", "type": "table",
                  "tables": [{"headers": ["項目", "内容"],
                              "rows": [["a", {"text": "b",
                                             "badge": "ok"}]]}]}))
    t("★行の列数が見出しと違えば止める★",
      _tbl_stops({"title": "x", "type": "table",
                  "tables": [{"headers": ["項目", "内容"],
                              "rows": [["a"]]}]}))
    t("　表が無ければ止める",
      _tbl_stops({"title": "x", "type": "table", "body": ["a"]}))

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
        _pv({"slug": "x"})
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
    # ★構造の正しさ(STRUCT_OK)と数値の裏取り(VERIFIED)を分ける（運営者決定 2026-07-27）
    gso = compute_gates({**base, "checker_modes": {"normal": "STRUCT_OK", "suru": "DISABLED"}})
    t("★STRUCT_OK は表示する（構造は正常＝Phase0の事故は防げている）",
      gso["checker"] and gso["checker_modes"] == ["normal"])
    t("★STRUCT_OK を含むなら目安扱いになる", gso["checker_is_estimate"] is True)
    t("　VERIFIEDだけなら目安扱いにしない",
      compute_gates({**base, "checker_modes": {"normal": "VERIFIED"}})["checker_is_estimate"] is False)
    t("　DISABLED/UNVERIFIED は表示しない",
      not compute_gates({**base, "checker_modes": {"a": "DISABLED", "b": "UNVERIFIED"}})["checker"])
    t("★kill switch型不正でも開かない",
      not compute_gates({**base, "checker_modes": {"normal": "VERIFIED"},
                         "checker_kill_switch": "true"})["checker"])
    t("★予約キーをmode名にできない",
      not compute_gates({**base, "checker_modes": {"unit": "VERIFIED"}})["checker"])
    # -------- identity v2（2026-07-30・Codex指摘 穴4）
    #   machines.json に identity を足した途端、未知フィールド扱いで
    #   **全機種の公開射影が拒否される**状態だった。
    t("★identity を持つ機種が未知フィールドで拒否されない★",
      _pv({**base, "identity": {"manufacturer_id": "kitadenshi",
                                "regulatory_model_code": "SテストKA"}}
          )["machine"].get("slug") == base["slug"])
    def _blocked(m):
        """公開射影が止まること（例外でも空射影でも「止まった」とみなす）。"""
        try:
            return _pv(m)["machine"] == {}
        except GateError:
            return True
    t("　identity 以外の未知フィールドは今までどおり拒否する",
      _blocked({**base, "identity_note": "メモ"}))
    t("　identity が辞書でなければ拒否する",
      _blocked({**base, "identity": "kitadenshi"}))

    # ===== 複合断定（第3版の主眼）=====
    led_kv = {atom_id("期待値", "legacy_safe"): {"verdict": ALLOW}}
    t("★複合断定: label『期待値』+value『580G〜』は素通りしない",
      classify_atom(["期待値", "580G〜"], led_kv, "legacy_safe") == UNCLASSIFIED)
    t("★複合断定: 見出し＋段落も結合判定",
      classify_atom(["当サイトの狙い目",
                     "580G〜（機械割108%）"],
                    None, "legacy_safe") == UNCLASSIFIED)
    t("　見出しを『当サイトの狙い目』に替えても、判断そのものは通す（B区分）",
      classify_atom(["当サイトの狙い目", "580G〜"],
                    None, "legacy_safe") == ALLOW)
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
                               "normal": {"good": 600, "excellent": 800}}}
    pcj = json.dumps(_pv(bad_checker)["machine"],
                     ensure_ascii=False)
    t("★checker.unit も分類される（危険なら落ちる）", "期待収支" not in pcj)
    t("★modes[].label も分類される", "期待収支" not in pcj)

    badge = {"sections": [{"title": "設定示唆まとめ", "type": "settei",
                           "tables": [{"label": "終了画面", "headers": ["画面", "示唆"],
                                       "rows": [[{"text": "期待収支がプラス", "badge": "weak"},
                                                 "弱"]]}]}]}
    # 「設定示唆まとめ」は 設定 を含むため台帳が要る（未分類なら止まるのが正しい挙動）
    led_badge = {atom_id(s, "legacy_safe"): {"verdict": ALLOW} for s in
                 ("設定示唆まとめ", "設定示唆まとめ / 終了画面", "設定示唆まとめ / 画面 / 示唆")}
    t("★設定表のセル本文も分類される", "期待収支" not in json.dumps(
        _pv(base, badge, led_badge)["detail"], ensure_ascii=False))
    t("★23-4: UIが知らない badge 値は停止（示唆の強さが黙って消える）",
      bool(audit_view(base, {"sections": [
          {"title": "設定示唆まとめ", "type": "settei",
           "tables": [{"label": "終了画面", "headers": ["画面", "示唆"],
                       "rows": [[{"text": "白", "badge": "strnog"}, "弱"]]}]}]},
                      led_badge)["errors"]))
    _scoped = {atom_id("狙い目 / 600G〜", "legacy_safe"):
               {"verdict": ALLOW, "slugs": ["hokuto"]}}
    t("★23-13: 機種を指定した台帳ALLOWは、その機種でだけ効く",
      classify_atom(["狙い目", "600G〜"], _scoped, "legacy_safe", "hokuto") == ALLOW
      and classify_atom(["狙い目", "600G〜"], _scoped, "legacy_safe", "baki") == UNCLASSIFIED)
    t("　slugs 無しの台帳ALLOWは文言そのものへの承認として全機種に効く",
      classify_atom(["狙い目", "600G〜"],
                    {atom_id("狙い目 / 600G〜", "legacy_safe"): {"verdict": ALLOW}},
                    "legacy_safe", "baki") == ALLOW)
    t("　slugs が壊れていたら効かせない（fail-closed）",
      classify_atom(["狙い目", "600G〜"],
                    {atom_id("狙い目 / 600G〜", "legacy_safe"):
                     {"verdict": ALLOW, "slugs": "hokuto"}},
                    "legacy_safe", "hokuto") == UNCLASSIFIED)
    t("★否定形（注意喚起）は断定として消さない",
      classify_atom(["ゲーム数狙いではプラス期待値が出ません"], None,
                    "legacy_safe") != DROP)
    t("　肯定の断定はこれまで通り止める",
      classify_atom(["400G〜からプラス期待値に入ります"], None, "legacy_safe") == DROP)
    t("　否定と肯定が混在する文は止める（安全側）",
      classify_atom(["プラス域には入りませんが、実質プラス域です"], None, "legacy_safe") == DROP)
    _drop_case = {"sections": [{"title": "狙い目", "body": ["580G〜です"]},
                               {"title": "注意",
                                "body": ["期待収支は算出していません"]}]}
    _stopped = False
    try:
        publish_view(base, _drop_case)
    except GateError:
        _stopped = True
    t("★24-5: 公開できない表現が残る原稿はビルドを止める（但し書きだけ消えない）",
      _stopped)
    t("　除去を許す指定をすれば射影自体は動く（検査用の逃げ道）",
      isinstance(publish_view(base, _drop_case, allow_drops=True)["detail"], dict))
    t("★24-7: 入力単位(unit)が無い checker は停止",
      bool(audit_view({**base, "checker_modes": {"normal": "STRUCT_OK"},
                       "checker": {"modes": [{"key": "normal", "label": "通常"}],
                                   "normal": {"good": 600, "excellent": 800}}})["errors"]))
    t("　小数の閾値は単位を問わず停止（入力は整数に丸められる）",
      bool(audit_view({**base, "checker_modes": {"normal": "STRUCT_OK"},
                       "checker": {"unit": "pt",
                                   "modes": [{"key": "normal", "label": "通常"}],
                                   "normal": {"good": 10.5, "excellent": 20.5}}})["errors"]))
    t("★24-6: 選べる交換率に狙い目の文が無ければ停止",
      bool(audit_view({**base, "checker_modes": {"normal": "STRUCT_OK"},
                       "strategyByRate": {"eq56": "600G〜"},
                       "checker": {"unit": "G",
                                   "exchangeRates": [{"key": "eq56", "label": "5.6枚"},
                                                     {"key": "rate50", "label": "5.0枚"}],
                                   "defaultRate": "eq56",
                                   "modes": [{"key": "normal", "label": "通常"}],
                                   "normal": {"byRate": {
                                       "eq56": {"good": 600, "excellent": 800},
                                       "rate50": {"good": 650, "excellent": 850}}}}})["errors"]))
    t("　baseRateKey を明示すれば mode直下を基準値として認める",
      _pv({**base, "checker_modes": {"normal": "STRUCT_OK"},
           "strategyByRate": {"equal": "600G〜", "eq56": "620G〜"},
           "checker": {"unit": "G", "baseRateKey": "equal",
                       "exchangeRates": [{"key": "equal", "label": "等価"},
                                         {"key": "eq56", "label": "5.6枚"}],
                       "defaultRate": "equal",
                       "modes": [{"key": "normal", "label": "通常"}],
                       "normal": {"good": 600, "excellent": 800,
                                  "byRate": {"eq56": {"good": 620, "excellent": 820}}}}}
          )["gates"]["checker"] is True)
    # ===== Phase 1 閉鎖確認で要求された回帰試験（Codex 2026-07-27）=====
    _rates2 = [{"key": "eq56", "label": "5.6枚"}, {"key": "rate50", "label": "5.0枚"}]
    _ck2 = {"unit": "G", "exchangeRates": _rates2, "defaultRate": "eq56",
            "modes": [{"key": "normal", "label": "通常"}],
            "normal": {"byRate": {"eq56": {"good": 600, "excellent": 800},
                                  "rate50": {"good": 650, "excellent": 850}}}}
    t("★閉鎖-1: 交換率別の狙い目文が無くても、チェッカーから材料が取れるなら通す",
      _pv({**base, "checker_modes": {"normal": "STRUCT_OK"},
           "checker": _ck2})["gates"]["checker"] is True)
    t("　材料(good)が取れない交換率があれば停止",
      bool(audit_view({**base, "checker_modes": {"normal": "STRUCT_OK"},
                       "checker": {**_ck2,
                                   "normal": {"byRate": {
                                       "eq56": {"good": 600, "excellent": 800},
                                       "rate50": {"caution": 650,
                                                  "excellent": 850}}}}})["errors"]))
    t("★閉鎖-2: 要約の数字がその交換率のチェッカーに無ければ停止",
      bool(audit_view({**base, "checker_modes": {"normal": "STRUCT_OK"},
                       "strategyByRate": {"eq56": "580G〜", "rate50": "650G〜"},
                       "checker": _ck2})["errors"]))
    t("　要約の数字がチェッカーと一致していれば通す",
      _pv({**base, "checker_modes": {"normal": "STRUCT_OK"},
           "strategyByRate": {"eq56": "600G〜", "rate50": "650G〜"},
           "checker": _ck2})["gates"]["checker"] is True)
    t("★25-5: 別機種の記事が付いていたら停止",
      bool(audit_view(base, {"slug": "別の機種", "lead": "x"})["errors"]))
    t("★23-9: 空白で分断した禁止表現も止める",
      classify_atom(["期 待 値 が", "プ ラ ス"], None, "legacy_safe") == DROP
      and classify_atom(["設 定 3 は", "非 搭 載"], None, "legacy_safe") == DROP)
    t("★23-10: アラビア・インド数字も数値として扱う",
      _has_numeral("٩٩٩G") and not _has_numeral("あいうえお"))
    t("★23-7: equivOnly と交換率の選択肢は同時に持てない",
      bool(audit_view({**base, "checker_modes": {"normal": "STRUCT_OK"},
                       "checker": {"unit": "G", "equivOnly": True,
                                   "exchangeRates": [{"key": "eq56", "label": "5.6枚"}],
                                   "defaultRate": "eq56",
                                   "modes": [{"key": "normal", "label": "通常"}],
                                   "normal": {"good": 600, "excellent": 800}}})["errors"]))
    t("★23-2: 設定表セクションの body は停止（画面に描かれない）",
      bool(audit_view(base, {"sections": [
          {"title": "設定示唆まとめ", "type": "settei",
           "body": ["以下の数値は未確認です"],
           "tables": [{"label": "終了画面", "headers": ["画面", "示唆"],
                       "rows": [["赤", "設定6"]]}]}]}, led_badge)["errors"]))
    t("　tables と rows の併存も停止",
      bool(audit_view(base, {"sections": [
          {"title": "設定示唆まとめ", "type": "settei",
           "tables": [{"label": "終了画面", "headers": ["画面", "示唆"],
                       "rows": [["赤", "設定6"]]}],
           "rows": [{"trigger": "青", "hint": "弱"}]}]}, led_badge)["errors"]))
    t("★23-5: スルーのmodeに suruMax が無ければ停止",
      bool(audit_view({**base, "checker_modes": {"suru": "STRUCT_OK"},
                       "checker": {"unit": "G",
                                   "modes": [{"key": "suru", "label": "スルー",
                                              "hasSuru": True}],
                                   "suru": {"suru": [{"count": 0, "good": 600,
                                                      "excellent": 800}]}}})["errors"]))
    t("★23-3: 表の label / headers が無ければ停止",
      bool(audit_view(base, {"sections": [
          {"title": "設定示唆まとめ", "type": "settei",
           "tables": [{"rows": [["赤", "強示唆"]]}]}]}, led_badge)["errors"]))

    # ===== 未知フィールドは原子ごと拒否 =====
    unk = {"summaryBoxes": [{"label": "天井", "value": "999G+α", "note": "未確認"}]}
    t("★未知フィールド（但し書き）を含む箱は構造エラー",
      any(e["path"] == "summaryBoxes[0]" for e in audit_view(base, unk)["errors"]))
    unk2 = {**base, "checker_modes": {"normal": "VERIFIED"},
            "checker": {"unit": "G", "modes": [{"key": "normal", "label": "normal"}],
                        "normal": {"good": 600, "excellent": 800, "private_note": 123}}}
    raised = False
    try:
        _pv(unk2)
    except GateError:
        raised = True
    t("★未知フィールドを含むmodeは公開を止める", raised)

    # ===== 動的キーの安全性 =====
    dyn = {**base, "strategyByRate": {"秘密の文章です": "600G〜"}}
    aj = json.dumps(audit_view(dyn), ensure_ascii=False)
    t("★診断pathに散文キーが入らない", "秘密" not in aj)
    raised = False
    try:
        _pv(dyn)
    except GateError:
        raised = True
    t("識別子形式でないキーは公開を止める", raised)

    # ===== preview =====
    # info が辞書（型不正）の場合は、射影で落とすより早く構造エラーで止まる
    raised = False
    try:
        _pv({"slug": "x", "lifecycle": "VERIFIED_PREVIEW", "name": "テスト機",
                      "info": {"天井": 999, "note": "600Gから狙い目"}})
    except GateError:
        raised = True
    t("★入れ子になったinfo（型不正）は構造エラーで止める", raised)

    prev = {"slug": "x", "lifecycle": "VERIFIED_PREVIEW", "name": "テスト機",
            "manufacturer": "メーカーA", "release_date": "2026-08-01",
            "info": "天井999GのスマスロAT",
            "strategy": "等価600G〜", "limit": 999}
    pv = _pv(prev, {"lead": "リード", "sections": [{"title": "天井・恩恵", "body": ["天井は999Gです。"]}]})
    dumped = json.dumps(pv["machine"], ensure_ascii=False)
    t("preview: 禁止話題を含むinfoも狙い目も出さない",
      "999" not in dumped and "狙い目" not in dumped and "strategy" not in pv["machine"])
    t("preview: 記事本文を出さない", pv["detail"] == {})
    t("preview: 名称・メーカー・導入日は出す",
      pv["machine"]["name"] == "テスト機" and pv["machine"]["release_date"] == "2026-08-01")

    # ===== LEGACY の目安ラベル =====
    lv = _pv({**base, "strategy": "等価670G〜 / 5.6枚680G〜"})
    t("★LEGACY: 狙い目を出すなら目安ラベルを必ず添える",
      lv["machine"].get("disclaimer") == LEGACY_DISCLAIMER)
    t("LEGACY: 狙い目が無ければラベルは付けない",
      "disclaimer" not in _pv(base)["machine"])

    # ===== 実データ形状を落とさない =====
    real = {**base, "checker_modes": {"normal": "VERIFIED"},
            "checker": {"unit": "G", "hasSuru": True, "suruMax": 6,
                        "exchangeRates": [{"key": "eq56", "label": "5.6枚"}],
                        "defaultRate": "eq56", "modes": [{"key": "normal", "label": "通常"}],
                        "normal": {"good": 700, "target": 570, "excellent": 900,
                                   "byRate": {"eq56": {"good": 680, "target": 570,
                                                       "excellent": 880}}},
                        }}
    rc = _pv(real)["machine"]["checker"]
    t("実データ形状: exchangeRates/defaultRate/target/hasSuru を落とさない",
      rc["exchangeRates"][0]["key"] == "eq56" and rc["defaultRate"] == "eq56"
      and rc["normal"]["target"] == 570
      and rc["normal"]["byRate"]["eq56"]["target"] == 570)
    t("　UIが参照しないchecker直下フィールドは公開しない",
      all(k not in rc for k in ("hasSuru", "hasCycle", "suruMax", "ok", "ng")))
    # 回数系modeは入力単位(G)が必須になったので明示する
    cyc = {**base, "checker_modes": {"cycle": "VERIFIED"},
           "checker": {"unit": "G",
                       "modes": [{"key": "cycle", "label": "周期", "hasCycle": True}],
                       "cycle": {"cycle": [{"count": 1, "good": 800, "excellent": 1000}]}}}
    t("実データ形状: 周期(辞書配列)を落とさない",
      _pv(cyc)["machine"]["checker"]["cycle"]["cycle"][0]["count"] == 1)

    # ===== 診断に原文を出さない =====
    unc = {"sections": [{"title": "収支の話", "body": ["この台は1000円くらい得します。"]}]}
    av2 = audit_view(base, unc)
    t("★audit_viewは原文を返さない",
      "得します" not in json.dumps(av2, ensure_ascii=False) and len(av2["unclassified"]) >= 1)
    raised = False
    try:
        _pv(base, unc)
    except GateError:
        raised = True
    t("未分類があれば公開不可", raised)

    # ===== 段落の原子性 =====
    led = {atom_id("当サイトの狙い目 / 天井は999Gです。", "legacy_safe"): {"verdict": ALLOW},
           atom_id("当サイトの狙い目", "legacy_safe"): {"verdict": ALLOW}}
    atom = {"sections": [{"title": "当サイトの狙い目",
                          "body": ["580Gから期待収支がプラスになります。", "天井は999Gです。"]}]}
    pa = _pv(base, atom, led)
    aj2 = json.dumps(pa["detail"], ensure_ascii=False)
    t("段落: 絶対禁止を含む段落は丸ごと落ちる", "期待収支" not in aj2 and "580" not in aj2)
    # ★兄弟段落との関係を保証できないため、1段落でも落ちたらセクションごと落とす★
    #   （「580G〜です」「期待収支は算出していません」の但し書きだけ消える意味反転を防ぐ）
    t("段落: 1段落でも落ちたらセクションごと落とす", "天井は999Gです。" not in aj2)
    t("　安全な段落だけのセクションは残る",
      "天井は999Gです。" in json.dumps(_pv(
          base, {"sections": [{"title": "天井・恩恵", "body": ["天井は999Gです。"]}]}
      )["detail"], ensure_ascii=False))

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
           "checker": {"unit": "G", "modes": [{"key": "normal", "label": "normal"}],
                       "normal": {"good": 580, "excellent": 700,
                                  "note": "期待収支は算出していません"}}}
    # ★注意書きが落ちるなら、数値だけ残さず mode ごと出さない（意味反転しない）★
    #   データは壊れていないので公開自体は止めず、その塊を出さない扱いにする。
    inv_view = _pv(inv)
    t("★注意書きが落ちる場合は数値だけ残さない（modeごと出さない）",
      "checker" not in inv_view["machine"])
    t("　ゲート表示も閉じて自己矛盾を残さない",
      inv_view["gates"]["checker"] is False and inv_view["gates"]["checker_modes"] == [])
    t("　診断では「方針による除去」として記録される（構造エラーにしない）",
      audit_view(inv)["errors"] == []
      and any("公開基準" in (d.get("reason") or "") for d in audit_view(inv)["dropped"]))

    # 構造エラーは公開を止める
    for bad_checker, label in (
        ({"modes": [{"key": "normal", "label": "normal"}], "normal": {"excellent": 600, "private": 1}}, "未知フィールド"),
        ({"modes": [{"key": "normal", "label": "normal"}]}, "configが無い"),
        ({"modes": [{"key": "normal", "label": "normal"}], "normal": {"excellent": 600, "_disabled": "停止"}}, "_disabled付き"),
        ({"modes": [{"key": "other", "label": "other"}], "normal": {"good": 600, "excellent": 800}}, "modes宣言に無い"),
    ):
        raised = False
        try:
            _pv({**base, "checker_modes": {"normal": "VERIFIED"}, "checker": bad_checker})
        except GateError:
            raised = True
        t(f"★構造エラーで公開を止める（{label}）", raised)
    t("audit_view: 構造エラーを ok=False で報告",
      audit_view({**base, "checker_modes": {"normal": "VERIFIED"},
                  "checker": {"unit": "G", "modes": [{"key": "normal", "label": "normal"}]}})["ok"] is False)

    # 表label込みの複合断定
    tbl_led = {atom_id(s, "legacy_safe"): {"verdict": ALLOW}
               for s in ("設定示唆まとめ", "設定示唆まとめ / 期待値",
                         "設定示唆まとめ / G数 / 判定")}
    tbl = {"sections": [{"title": "設定示唆まとめ", "type": "settei",
                         "tables": [{"label": "期待値", "headers": ["G数", "判定"],
                                     "rows": [["580G〜", "◎"]]}]}]}
    t("★表label＋行の複合断定を見逃さない",
      any(u["path"].endswith("rows[0]") for u in audit_view(base, tbl, tbl_led)["unclassified"]))

    # 目安ラベルは detail だけに数値がある場合も付く
    d_only = _pv(base, {"summaryBoxes": [{"label": "狙い目", "value": "580G〜"}]})
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
            _pv({**base, "checker_modes": {"normal": "VERIFIED"},
                          "checker": {"unit": "G", "modes": [{"key": "normal", "label": "normal"}], "normal": bad_conf}})
        except GateError:
            raised = True
        t(f"★型不正で公開を止める（{label}）", raised)

    raised = False
    try:
        _pv({**base, "checker_modes": {"normal": "VERIFIED"}})   # checker本体が無い
    except GateError:
        raised = True
    t("★VERIFIED指定なのにchecker本体が無ければ止める", raised)

    t("★行形式は完全一致で判定（片側だけの行を通さない）",
      any(e["path"].startswith("sections[0].rows") for e in audit_view(
          base, {"sections": [{"title": "設定示唆まとめ", "type": "settei",
                               "rows": [{"value": "580G"}]}]},
          {atom_id("設定示唆まとめ", "legacy_safe"): {"verdict": ALLOW}})["errors"]))

    t("★出典URLのクエリ・フラグメントを落とす",
      _pv({**base, "sources": [{"url": "https://example.com/a?token=SECRET#x"}]}
                   )["machine"]["sources"][0]["url"] == "https://example.com/a")

    t("★目安ラベルは実際に数値がある時だけ付く",
      "disclaimer" not in _pv(base, {"lead": "数字のない紹介文です。"})["machine"]
      and _pv({**base, "strategy": "等価600G〜"})["machine"]["disclaimer"] == LEGACY_DISCLAIMER)
    # ★入力軸と判定軸の整合（Phase 0の事故型を機構で防ぐ・方針書§6 条件3）
    # ★軸の食い違いは閾値の大小でなく構造で判定する（実データで20/20検出・誤検知0）★
    #   停止マーカー(_disabled)を消しても止まることが重要（人の印だけを根拠にしない）
    for bad_val, label in ((400, "大きい閾値"), (4, "小さい閾値＝Phase 0の実データ形")):
        t(f"★回数系modeが直下に閾値を持てば止める（{label}）",
          any("入力軸と判定軸の食い違い" in e["reason"] for e in audit_view(
              {**base, "checker_modes": {"suru": "STRUCT_OK"},
               "checker": {"unit": "G", "modes": [{"key": "suru", "label": "スルー", "hasSuru": True}],
                           "suru": {"good": bad_val}}})["errors"]))
    t("　停止マーカーを消しても止まる（マーカー非依存）",
      any("入力軸と判定軸の食い違い" in e["reason"] for e in audit_view(
          {**base, "checker_modes": {"through": "STRUCT_OK"},
           "checker": {"unit": "G", "modes": [{"key": "through", "label": "through"}],
                       "through": {"excellent": 4, "good": 3, "caution": 2}}})["errors"]))
    t("　正しい二軸構造（回数ごとのG数）は通す",
      _pv({**base, "checker_modes": {"suru": "STRUCT_OK"},
                    "checker": {"unit": "G", "modes": [{"key": "suru", "label": "スルー", "hasSuru": True}],
                                "suru": {"suruMax": 0, "suru": [{"count": 0, "good": 600, "excellent": 800}]}}}
                   )["gates"]["checker"] is True)

    # ★軸契約の完全化（Codex 11巡目の指定反例を全件固定）★
    def _axis_stops(ck, modes=None):
        # 軸契約に触れる構造エラーが1件でも出ること（メッセージ文言に依存させない）
        return bool(audit_view({**base, "checker_modes": modes or {"suru": "STRUCT_OK"},
                                "checker": ck})["errors"])

    t("★軸契約: 直下閾値＋suru[] の併存 → 停止",
      _axis_stops({"unit": "G", "modes": [{"key": "suru", "label": "スルー", "hasSuru": True}],
                   "suru": {"good": 4, "suru": [{"count": 1, "good": 600, "excellent": 800}]}}))
    t("★軸契約: unit='回' ＋ G数の行 → 停止",
      _axis_stops({"unit": "回", "modes": [{"key": "suru", "label": "スルー", "hasSuru": True}],
                   "suru": {"suru": [{"count": 1, "good": 600, "excellent": 800}]}}))
    t("★軸契約: count 欠落 → 停止",
      _axis_stops({"unit": "G", "modes": [{"key": "suru", "label": "スルー", "hasSuru": True}],
                   "suru": {"suru": [{"good": 600}]}}))
    for rows, label in (([{"count": 1, "good": 600, "excellent": 800}, {"count": 1, "good": 500, "excellent": 700}], "重複"),
                        ([{"count": 2, "good": 600, "excellent": 800}, {"count": 1, "good": 500, "excellent": 700}], "降順"),
                        ([{"count": 1.5, "good": 600, "excellent": 800}], "小数"),
                        ([{"count": -1, "good": 600, "excellent": 800}], "負数")):
        t(f"★軸契約: count {label} → 停止",
          _axis_stops({"unit": "G", "modes": [{"key": "suru", "label": "スルー", "hasSuru": True}], "suru": {"suru": rows}}))
    t("★軸契約: key='at'でも hasSuru宣言＋直下閾値 → 停止",
      _axis_stops({"unit": "G", "modes": [{"key": "at", "hasSuru": True}],
                   "at": {"good": 4}}, {"at": "STRUCT_OK"}))
    # Codex 12巡目の指定反例
    t("★軸契約: 入力単位の欠落 → 停止",
      _axis_stops({"modes": [{"key": "suru", "label": "スルー", "hasSuru": True}], "suru": {"suru": [{"count": 1, "good": 600, "excellent": 800}]}}))
    t("★軸契約: count が小数表記(1.0) → 停止",
      _axis_stops({"unit": "G", "modes": [{"key": "suru", "label": "スルー", "hasSuru": True}],
                   "suru": {"suru": [{"count": 1.0, "good": 600, "excellent": 800}]}}))
    t("★軸契約: 行にG数の判定材料が無い（countだけ）→ 停止",
      _axis_stops({"unit": "G", "modes": [{"key": "suru", "label": "スルー", "hasSuru": True}],
                   "suru": {"suru": [{"count": 1}]}}))
    t("★軸契約: 宣言と実体の不一致（hasSuru宣言なのにcycle[]）→ 停止",
      _axis_stops({"unit": "G", "modes": [{"key": "suru", "hasCycle": True}],
                   "suru": {"suru": [{"count": 1, "good": 600, "excellent": 800}]}}))
    t("★軸契約: hasSuruとhasCycleの同時宣言 → 停止",
      _axis_stops({"unit": "G", "modes": [{"key": "suru", "hasSuru": True, "hasCycle": True}],
                   "suru": {"suru": [{"count": 1, "good": 600, "excellent": 800}]}}))
    t("★軸契約: suru[]とcycle[]の併存 → 停止",
      _axis_stops({"unit": "G", "modes": [{"key": "suru", "label": "スルー", "hasSuru": True}],
                   "suru": {"suru": [{"count": 1, "good": 600, "excellent": 800}],
                            "cycle": [{"count": 1, "good": 500, "excellent": 700}]}}))
    t("　行のbyRateにG数があれば判定材料として認める",
      _pv({**base, "checker_modes": {"suru": "STRUCT_OK"},
                    "checker": {"unit": "G",
                                "modes": [{"key": "suru", "label": "スルー", "hasSuru": True}],
                                "exchangeRates": [{"key": "eq56", "label": "5.6枚"}],
                                "suru": {"suruMax": 0,
                                         "suru": [{"count": 0,
                                                   "byRate": {"eq56": {"good": 600,
                                                                       "excellent": 800}}}]}}}
                   )["gates"]["checker"] is True)
    t("★軸契約: noteだけの周期mode → 停止（実データ sengoku_otome5 と同型）",
      _axis_stops({"unit": "G", "modes": [{"key": "cycle", "hasCycle": True}],
                   "cycle": {"note": "周期天井は最大6周期"}}, {"cycle": "STRUCT_OK"}))
    t("★分割された絶対禁止を台帳ALLOWで通せない",
      classify_atom(["期待値が", "プラス"],
                    {atom_id("期待値が / プラス", "legacy_safe"): {"verdict": ALLOW}},
                    "legacy_safe") == DROP)
    t("★目安チェッカーを出すなら必ず目安ラベルの対象になる",
      "checker" in _pv(
          {**base, "checker_modes": {"normal": "STRUCT_OK"},
           "checker": {"unit": "G", "modes": [{"key": "normal", "label": "normal"}],
                       "normal": {"good": 580, "excellent": 700}}}
      )["machine"]["display_requirements"]["surfaces"])
    t("　どの表示面に必要かを返す",
      "strategy" in _pv({**base, "strategy": "等価600G〜"}
                                 )["machine"]["display_requirements"]["surfaces"])
    t("★sections/型不正を構造エラーにする",
      audit_view(base, {"sections": "本文"})["ok"] is False)

    # ===== Codex 5巡目で不足を指摘された負例 =====
    def _raises(machine, detail=None, ledger=None):
        try:
            _pv(machine, detail, ledger)
            return False
        except GateError:
            return True

    ck = lambda conf, **kw: {**base, "checker_modes": {"normal": "VERIFIED"},
                            "checker": {"unit": "G", "modes": [{"key": "normal", "label": "normal"}], "normal": conf, **kw}}
    t("★modes が非list → 停止",
      _raises({**base, "checker_modes": {"normal": "VERIFIED"},
               "checker": {"modes": "normal", "normal": {"good": 580}}}))
    t("★byRate 本体が非dict → 停止", _raises(ck({"good": 580, "byRate": "eq56"})))
    t("★suru が非list → 停止", _raises(ck({"good": 580, "suru": {"1": 1}})))
    t("★byRate配下の数値が文字列 → 停止（黙って消さない）",
      _raises(ck({"good": 580, "byRate": {"eq56": {"excellent": "600"}}})))
    t("★複数byRateのうち1件だけ不正でも mode ごと停止",
      _raises(ck({"good": 580, "byRate": {"eq56": {"excellent": 600},
                                          "rate50": {"excellent": "700"}}})))
    t("★複数suru行のうち1行だけ不正でも停止",
      _raises(ck({"good": 580, "suru": [{"count": 1, "good": 500, "excellent": 700},
                                        {"count": 2, "good": "400"}]})))
    t("★checker直下の未知フィールド（但し書き）→ 停止",
      _raises(ck({"good": 580}, warning="未確認")))
    t("★設定表の note が配列 → 表ごと停止",
      _raises(base, {"sections": [{"title": "設定示唆まとめ", "type": "settei",
                                   "tables": [{"label": "終了画面", "rows": [["白", "弱"]],
                                               "note": ["未確認"]}]}]},
              {atom_id("設定示唆まとめ", "legacy_safe"): {"verdict": ALLOW}}))
    t("★セルの badge が非文字列 → 停止",
      _raises(base, {"sections": [{"title": "設定示唆まとめ", "type": "settei",
                                   "tables": [{"label": "終了画面",
                                               "rows": [[{"text": "白", "badge": 1}, "弱"]]}]}]},
              {atom_id("設定示唆まとめ", "legacy_safe"): {"verdict": ALLOW}}))
    t("★設計正本のdeny-pattern（期待値が乗る/積み上がる）は台帳ALLOWでも通さない",
      all(classify_atom([s], {atom_id(s, "legacy_safe"): {"verdict": ALLOW}},
                        "legacy_safe") == DROP
          for s in ("浅めから期待値が乗ります", "深いほど期待値が積み上がります")))
    t("★設定欠番の暗示：漢数字・設定を繰り返す列挙も検出",
      classify_atom(["設定一・二・四・五・六"], None, "legacy_safe") == DROP
      and classify_atom(["設定1/設定2/設定4/設定5/設定6"], None, "legacy_safe") == DROP)
    t("★出典タイトルの数値も目安ラベルの対象",
      "sources.title" in _pv(
          {**base, "sources": [{"url": "https://a.example/x", "title": "狙い目580Gの解析"}]}
      )["machine"]["display_requirements"]["surfaces"])

    # ===== Codex 17巡目の反例（回帰テスト）=====
    ck17 = lambda ck, modes=None: bool(audit_view(
        {**base, "checker_modes": modes or {"normal": "STRUCT_OK"}, "checker": ck})["errors"])
    base_ck = {"unit": "G", "modes": [{"key": "normal", "label": "通常"}],
               "normal": {"excellent": 700, "good": 600, "caution": 500}}
    t("★17-3: 判定材料の無いmode（noteだけ）→ 停止",
      ck17({**base_ck, "normal": {"note": "最大6周期"}}))
    t("★17-4: 閾値の順序が壊れていたら停止（判定が反転する）",
      ck17({**base_ck, "normal": {"excellent": 500, "good": 600, "caution": 700}}))
    t("　閾値が負・無限なら停止",
      ck17({**base_ck, "normal": {"good": -1}})
      and ck17({**base_ck, "normal": {"good": float("inf")}}))
    t("★17-5: 選択肢の交換率で判定値に到達できなければ停止",
      ck17({**base_ck, "exchangeRates": [{"key": "eq56", "label": "5.6枚"}],
            "normal": {"byRate": {"rate50": {"good": 600}}}}))
    t("　選択肢に無い交換率の判定値も停止",
      ck17({**base_ck, "exchangeRates": [{"key": "eq56", "label": "5.6枚"}],
            "normal": {"good": 600, "byRate": {"nope": {"good": 500}}}}))
    t("★17-6: mode・交換率の表示ラベルが無ければ停止",
      ck17({**base_ck, "modes": [{"key": "normal"}]})          # ラベル欠落（意図的）
      and ck17({**base_ck, "exchangeRates": [{"key": "eq56"}],  # ラベル欠落（意図的）
                "normal": {"good": 600, "byRate": {"eq56": {"good": 600, "excellent": 800}}}}))
    t("★17-7: 数値配列の周期は未対応として停止",
      ck17({"unit": "G", "modes": [{"key": "cycle", "label": "周期", "hasCycle": True}],
            "cycle": {"cycle": [1, 2, 3]}}, {"cycle": "STRUCT_OK"}))
    t("★17-11: 実在しない日付は停止（2026-02-31）",
      bool(audit_view({**base, "release_date": "2026-02-31"})["errors"]))
    t("★17-13: 機種名が内容除去されたら公開しない",
      bool(audit_view({**base, "name": "期待収支がプラスの台"})["errors"]))
    t("★16-1: 一部modeだけ除去されたら宣言・ゲート・実体を一致させる",
      _pv({**base, "checker_modes": {"a": "STRUCT_OK", "b": "STRUCT_OK"},
                    "checker": {"unit": "G",
                                "modes": [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}],
                                "a": {"good": 600, "excellent": 800},
                                "b": {"good": 500, "excellent": 700, "note": "580Gから期待収支がプラス"}}}
                   )["gates"]["checker_modes"] == ["a"])

    # ===== Codex 18巡目の反例（回帰テスト）=====
    b18 = {"unit": "G", "modes": [{"key": "suru", "label": "スルー", "hasSuru": True}],
           "suru": {"suruMax": 1, "suru": [{"count": 0, "good": 600, "excellent": 800},
                                           {"count": 1, "good": 500, "excellent": 700}]}}
    ax18 = lambda ck, modes=None: bool(audit_view(
        {**base, "checker_modes": modes or {"suru": "STRUCT_OK"}, "checker": ck})["errors"])
    t("★18-1: 行の閾値順序が壊れていたら停止",
      ax18({**b18, "suru": {"suru": [{"count": 0, "caution": 700, "good": 600, "excellent": 500}]}}))
    t("★18-2: byRate適用後に順序が壊れる場合も停止",
      ax18({**b18, "exchangeRates": [{"key": "eq56", "label": "5.6枚"}],
            "suru": {"suru": [{"count": 0, "caution": 300, "good": 600, "excellent": 700,
                               "byRate": {"eq56": {"excellent": 200}}}]}}))
    t("★18-3: limit/suruMax が整数でなければ停止", ax18({**b18, "limit": 1.5}))
    t("★18-4: 交換率キーはあるが判定値が無い → 停止",
      ax18({**b18, "exchangeRates": [{"key": "eq56", "label": "5.6枚"}],
            "suru": {"suru": [{"count": 0, "byRate": {"eq56": {"note": "x"}}}]}}))
    t("　交換率別の値があるのに選択肢が無い → 停止",
      ax18({**b18, "suru": {"suru": [{"count": 0, "byRate": {"eq56": {"good": 600, "excellent": 800}}}]}}))
    t("★18-5: modes宣言が無ければ停止",
      ax18({"unit": "G", "suru": {"suruMax": 0, "suru": [{"count": 0, "good": 600, "excellent": 800}]}}))
    t("★18-6: 行があるのに hasSuru/hasCycle 宣言が無ければ停止",
      ax18({"unit": "G", "modes": [{"key": "at", "label": "AT"}],
            "at": {"suru": [{"count": 0, "good": 600, "excellent": 800}]}}, {"at": "STRUCT_OK"}))
    t("★18-7: 上限を超える回数行・周期の連番崩れは停止",
      ax18({**b18, "suru": {"suruMax": 1, "suru": [{"count": 0, "good": 600, "excellent": 800},
                                                   {"count": 5, "good": 500, "excellent": 700}]}})
      and ax18({"unit": "G", "modes": [{"key": "cycle", "label": "周期", "hasCycle": True}],
                "cycle": {"cycle": [{"count": 1, "good": 600, "excellent": 800}, {"count": 3, "good": 500, "excellent": 700}]}},
               {"cycle": "STRUCT_OK"}))
    t("★18-8: 表示ラベルの重複は停止",
      ax18({"unit": "G", "modes": [{"key": "a", "label": "同じ"}, {"key": "b", "label": "同じ"}],
            "a": {"good": 600, "excellent": 800}, "b": {"good": 500, "excellent": 700}}, {"a": "STRUCT_OK", "b": "STRUCT_OK"}))
    t("★18-10: 表示すると空になる機種名は停止",
      bool(audit_view({**base, "name": "​​"})["errors"]))
    t("★18-12: modeDataと直下で片方が壊れていたら停止",
      ax18({**b18, "modeData": {"suru": "壊れた値"}}))
    t("★22-5: count=1始まり（0スルーの行が無い）modeは出さない",
      _pv({**base, "checker_modes": {"suru": "STRUCT_OK"},
                    "checker": {"unit": "G",
                                "modes": [{"key": "suru", "label": "スルー", "hasSuru": True}],
                                "suru": {"suruMax": 2,
                                         "suru": [{"count": 1, "good": 600, "excellent": 800},
                                                  {"count": 2, "good": 500, "excellent": 700}]}}}
                   )["gates"]["checker"] is False)
    t("　0スルーの行があれば通す",
      _pv({**base, "checker_modes": {"suru": "STRUCT_OK"},
                    "checker": {"unit": "G",
                                "modes": [{"key": "suru", "label": "スルー", "hasSuru": True}],
                                "suru": {"suruMax": 2,
                                         "suru": [{"count": 0, "good": 700, "excellent": 900},
                                                  {"count": 1, "good": 600, "excellent": 800},
                                                  {"count": 2, "good": 500, "excellent": 700}]}}}
                   )["gates"]["checker"] is True)

    # ===== Codex 19巡目の反例（回帰テスト）=====
    b19 = {"unit": "G", "modes": [{"key": "suru", "label": "スルー", "hasSuru": True}],
           "suru": {"suruMax": 0, "suru": [{"count": 0, "good": 600, "excellent": 800}]}}
    ax19 = lambda ck, modes=None: bool(audit_view(
        {**base, "checker_modes": modes or {"suru": "STRUCT_OK"}, "checker": ck})["errors"])
    t("★19-1: mode名がsuruでも宣言フラグが無ければ停止",
      ax19({**b19, "modes": [{"key": "suru", "label": "スルー"}]}))
    t("★19-2: suruMax未指定なら既定上限99を超える行は停止",
      ax19({**b19, "suru": {"suru": [{"count": 100, "good": 600, "excellent": 800}]}}))
    t("★19-3: 回数系modeの直下byRateは停止（UIが使わない）",
      ax19({**b19, "exchangeRates": [{"key": "eq56", "label": "5.6枚"}],
            "suru": {"suruMax": 3, "byRate": {"eq56": {"good": 600, "excellent": 800}},
                     "suru": [{"count": 0, "byRate": {"eq56": {"good": 600, "excellent": 800}}}]}}))
    t("★19-5: 表示すると同じになるラベルは重複として停止",
      ax19({"unit": "G", "modes": [{"key": "a", "label": "通常"},
                                   {"key": "b", "label": "通​常"}],
            "a": {"good": 600, "excellent": 800}, "b": {"good": 500, "excellent": 700}}, {"a": "STRUCT_OK", "b": "STRUCT_OK"}))
    t("　表示すると空になるラベルも停止",
      ax19({**b19, "modes": [{"key": "suru", "label": "⁠", "hasSuru": True}]}))
    t("★19-6: U+2060だけの機種名も停止",
      bool(audit_view({**base, "name": "⁠⁠"})["errors"]))
    t("★19-7: ホスト名の不正（example-.com / a.-b.com）を停止",
      bool(audit_view({**base, "sources": [{"url": "https://example-.com/x"}]})["errors"])
      and bool(audit_view({**base, "sources": [{"url": "https://a.-b.com/x"}]})["errors"]))
    t("★19-8: machine.limit が負・小数なら停止",
      bool(audit_view({**base, "limit": -1})["errors"])
      and bool(audit_view({**base, "limit": 1.5})["errors"]))
    t("★19-12: 直下が非dictでmodeDataが正常でも停止",
      ax19({**b19, "suru": "壊れた値", "modeData": {"suru": {"good": 600}}}))

    # ===== Codex 20巡目の反例（回帰テスト）=====
    b20 = {"unit": "G", "modes": [{"key": "suru", "label": "スルー", "hasSuru": True}],
           "suru": {"suruMax": 0, "suru": [{"count": 0, "good": 600, "excellent": 800}]}}
    ax20 = lambda ck, extra=None, modes=None: bool(audit_view(
        {**base, **(extra or {}), "checker_modes": modes or {"suru": "STRUCT_OK"},
         "checker": ck})["errors"])
    t("★20-1: 直下が非dictならmodeDataが正常でも停止",
      ax20({**b20, "suru": "壊れた値",
            "modeData": {"suru": {"suruMax": 3, "suru": [{"count": 0, "good": 600, "excellent": 800}]}}}))
    t("★20-2: 危険なHTMLは無害化せず停止",
      bool(audit_view({**base, "name": "<img src=x onerror=alert(1)>"})["errors"])
      and bool(audit_view({**base, "strategy": "<span class=a>600G〜</span>"})["errors"]))
    t("　許可タグ(br/strong)は通す",
      not audit_view({**base, "strategy": "等価600G〜<br><strong>目安</strong>"})["errors"])
    t("★20-3: U+2066など双方向制御だけの名前も停止",
      bool(audit_view({**base, "name": "⁦⁩"})["errors"]))
    t("★20-4: 未知のstatusは公開しない（fail-closed）",
      bl_provisional_lifecycle({"slug": "x", "status": "compelte"}) == "CANDIDATE")
    # ★21-4: 到達性は「実際に公開された集合」で見る。型は壊れていないので記事は止めず、
    #        その値を公開しない（内容除去）。
    _v20_7a = _pv({**base, "checker": {**b20, "exchangeRates":
                                                [{"key": "eq56", "label": "5.6枚"}]},
                            "checker_modes": {"suru": "STRUCT_OK"},
                            "strategyByRate": {"nope": "600G〜", "eq56": "600G〜"}})
    t("★20-7: UIで選べない交換率キーは公開しない",
      "nope" not in (_v20_7a["machine"].get("strategyByRate") or {})
      and _v20_7a["machine"]["strategyByRate"]["eq56"] == "600G〜")
    _v20_7b = _pv({**base, "checker": b20, "checker_modes": {"suru": "STRUCT_OK"},
                            "limit": {"nope": 999, "suru": 900}})
    t("　公開されるmodeに無いlimitキーも公開しない",
      "nope" not in (_v20_7b["machine"].get("limit") or {})
      and _v20_7b["machine"]["limit"]["suru"] == 900)
    # ★21-4b: checker が丸ごと落ちたら、それを指す limit/strategyByRate も残らない
    # （閾値が入力上限を超えて mode が内容除去され、checker が空になる形）
    _v21_4 = _pv({**base, "checker": {**b20, "exchangeRates":
                                               [{"key": "eq56", "label": "5.6枚"}]},
                           "checker_modes": {"suru": "STRUCT_OK"},
                           "strategyByRate": {"eq56": "600G〜"}, "limit": {"suru": 500}})
    t("★21-4: checkerが公開されなければ strategyByRate / limit辞書 も公開しない",
      "checker" not in _v21_4["machine"]
      and "strategyByRate" not in _v21_4["machine"] and "limit" not in _v21_4["machine"])
    t("★20-8: 入力上限を超える good はそのmodeを出さない（記事は公開する）",
      _pv({**base, "limit": 700, "checker_modes": {"suru": "STRUCT_OK"},
                    "checker": {**b20, "suru": {"suruMax": 3,
                                                "suru": [{"count": 0, "good": 760, "excellent": 960}]}}}
                   )["gates"]["checker"] is False)
    t("★21-1: 入力上限を超える excellent もそのmodeを出さない（早見表が到達不能な行を作る）",
      _pv({**base, "limit": 700, "checker_modes": {"suru": "STRUCT_OK"},
                    "checker": {**b20, "suru": {"suruMax": 3,
                                                "suru": [{"count": 0, "good": 600,
                                                          "excellent": 760}]}}}
                   )["gates"]["checker"] is False)
    t("★21-3: 早見表が描けない（excellent が無い）構造は止める",
      ax20({**b20, "suru": {"suruMax": 3, "suru": [{"count": 0, "good": 600}]}}))
    t("★21-2: 交換率ごとの実効configにも good が要る（片方だけ excellent は不可）",
      bool(audit_view({**base, "checker_modes": {"normal": "STRUCT_OK"},
                       "checker": {"unit": "G",
                                   "exchangeRates": [{"key": "eq56", "label": "5.6枚"},
                                                     {"key": "rate50", "label": "5.0枚"}],
                                   "defaultRate": "eq56",
                                   "modes": [{"key": "normal", "label": "通常"}],
                                   "normal": {"byRate": {
                                       "eq56": {"good": 600, "excellent": 800},
                                       "rate50": {"excellent": 850}}}}})["errors"]))
    t("　全交換率に判定材料が揃っていれば通す",
      _pv({**base, "checker_modes": {"normal": "STRUCT_OK"},
                    "strategyByRate": {"eq56": "600G〜", "rate50": "650G〜"},
                    "checker": {"unit": "G",
                                "exchangeRates": [{"key": "eq56", "label": "5.6枚"},
                                                  {"key": "rate50", "label": "5.0枚"}],
                                "defaultRate": "eq56",
                                "modes": [{"key": "normal", "label": "通常"}],
                                "normal": {"byRate": {
                                    "eq56": {"good": 600, "excellent": 800},
                                    "rate50": {"good": 650, "excellent": 850}}}}}
                   )["gates"]["checker"] is True)
    t("★20-9: 判定の主軸(good)が無ければ停止",
      ax20({**b20, "suru": {"suruMax": 3, "suru": [{"count": 0, "excellent": 600}]}}))

    # ===== 危ない属性の検知（2026-07-30に実害のあるバグを発見）=====
    #   正規表現の `\b` が制御文字（0x08）に化けていて、
    #   **onclick= などのイベント属性を1つも検知できていなかった**。
    for _bad in ("onclick=alert(1)", "<div onmouseover=x>", "javascript:x",
                 "data:text/html,x", "<script>"):
        t(f"危ない書き方を検知: {_bad[:18]}", bool(_DANGEROUS_ATTR.search(_bad)))
    t("ふつうの文は検知しない", not _DANGEROUS_ATTR.search("ボタンを押すと表示されます"))

    # ===== 公開状態（先行記事かどうか）=====（Codex 14巡目 (a)-2）
    _stbase = {"slug": "x", "lifecycle": LEG, "name": "テスト機", "info": "スマスロAT"}
    t("★status: preview は公開データに残る",
      _pv({**_stbase, "status": "preview"})["machine"].get("status") == "preview")
    t("★status: complete も公開データに残る",
      _pv({**_stbase, "status": "complete"})["machine"].get("status") == "complete")
    t("★status: 未指定なら出さない",
      "status" not in _pv(dict(_stbase))["machine"])
    def _status_rejected(v):
        try:
            return _pv({**_stbase, "status": v})["machine"].get("status") is None
        except GateError:
            return True
    t("★status: 想定外の値は公開しない", _status_rejected("draft"))
    t("★status: 文字列でなければ公開しない", _status_rejected(1))

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

    # ★天井（ceiling）と50枚あたりG数（coinRate）★（2026-08-12）
    #   刻みの表が「天井 − 現在G」を出すので、0以下や真偽値が混ざると
    #   「天井まで残り -200G」のような表示になる。関所で止める。
    def _ck_ceil(**over):
        base = {"unit": "G", "modes": [{"key": "normal", "label": "通常"}],
                "normal": {"good": 500, "caution": 400, "excellent": 700}}
        base.update(over)
        return _project_checker(base, ["normal"], _Ctx("strict", None, "zzz"), None)

    _good = _ck_ceil(coinRate=30.7,
                     normal={"good": 500, "caution": 400, "excellent": 700,
                             "ceiling": 1499})
    t("★★天井とコイン持ちは公開データに残る★★（刻みの表が読む）",
      bool(_good) and _good.get("coinRate") == 30.7
      and (_good.get("normal") or {}).get("ceiling") == 1499)
    t("　コイン持ちが0なら止まる", not _ck_ceil(coinRate=0))
    t("　コイン持ちが真偽値なら止まる", not _ck_ceil(coinRate=True))
    t("　天井が0なら止まる",
      not _ck_ceil(normal={"good": 500, "caution": 400, "ceiling": 0}))
    t("　天井が文字なら止まる",
      not _ck_ceil(normal={"good": 500, "caution": 400, "ceiling": "1499"}))
    # ★初当たり確率は「分母」★（2026-08-12・依頼163）
    #   1未満だと 1/hitRate が1を超え、期待ゲーム数の計算が壊れる。
    t("★★初当たり確率は1以上でないと止まる★★（確率が1を超える形を通さない）",
      bool(_ck_ceil(hitRate=400)) and not _ck_ceil(hitRate=0.5)
      and not _ck_ceil(hitRate=1) and not _ck_ceil(hitRate=0))

    # ★★数えるのは、全部の試験が終わったこの場所だけ★★（2026-08-22）
    #   ★直す前★＝ここより手前で数えていたので、あとに続く6件の試験が
    #   ❌でも「合格」と表示され、終了コードも0だった。
    #   ＝**試験が落ちても緑に見える**という、いちばん危ない壊れ方。
    #   pending_machines で実際にこれに引っかかった（2026-08-22）。
    ng = [n for n, c in results if not c]
    print(f"\n{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    print(__doc__)

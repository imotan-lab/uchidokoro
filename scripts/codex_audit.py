# -*- coding: utf-8 -*-
"""codex_audit — 既存記事の誤りを第二AI（Codex）に探させ、裏取りできたものだけ自動修正する。

★役割分担（ここを取り違えない）★
  Codex        = 誤りの「発見器」。精度は高くなくてよい。候補と出典を出すだけ。
  verify_claims= 「関所」。出典URLをコードが再取得し、引用の逐語一致・機種同定・
                 値が引用の中にあることを機械確認する。ここを通らない値は絶対に書かない。
  この分離により「Codexが間違えても誤情報は公開されない」。

★自動修正の条件（すべて満たした時だけ）★
  1. サイトの構造化値とCodexの主張値が食い違う（numeric_divergence / MISMATCH）
  2. Codexの主張が★独立2ドメイン★で verify_claims exit0（＝evidence_strength=verified_policy）
  3. Codexのscopeが限定条件付きでない（AT間/CZ間等の部分天井は自動修正しない）
  4. 矛盾スキャン: 裏取りに使ったページに★旧値も同じ単位で載っていない★
     （両方載っている＝どちらが正か機械で決まらない → 触らない）
  5. apply_external_fix が構造化値と本文の両方を曖昧さなく直せる
  上記を1つでも欠けば【現状維持】し、要確認台帳（open_issues）へ登録する。

使い方:
  python scripts/codex_audit.py --run                 # 本番（既定3機種・修正上限2件）
  python scripts/codex_audit.py --run --slugs a,b     # 対象指定
  python scripts/codex_audit.py --dry-run --slugs a   # Codexは呼ぶが書き込まない
  python scripts/codex_audit.py --selftest            # 判定ロジックの内蔵テスト（通信なし）
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
BASE = Path(__file__).resolve().parent.parent
SCRIPTS = BASE / "scripts"
sys.path.insert(0, str(SCRIPTS))

import apply_external_fix          # noqa: E402（書き戻し器）
import claim_identity              # noqa: E402
import shadow_claims               # noqa: E402（claim抽出・比較器）
import shadow_codex                # noqa: E402（Codex実行・出典再取得検証）
import shadow_gold                 # noqa: E402（許可ドメイン定義）
import verify_claims               # noqa: E402（ページ取得＝矛盾スキャン用）

DOC = Path(r"C:/Users/imao_/Documents/uchidokoro")
STATE_PATH = DOC / "state.json"
LOG_DIR = DOC / "logs"
AUDIT_DIR = DOC / "codex_audit"
SEND_NOTIFY = r"C:/Users/imao_/.claude/send_notify.py"
OPEN_ISSUES = SCRIPTS / "open_issues.py"
TASK_ID = "uchidokoro-codex-audit"

DEFAULT_MACHINES = 3          # 1日の点検機種数（約40日で全120機種を1巡）
MAX_FIXES_PER_RUN = 2         # 1日の自動修正上限（verify STEP2.9と同じ保守的な上限）
MACHINE_BUDGET_SEC = 180      # 1機種3分
TASK_BUDGET_SEC = 900         # 1タスク15分（★時間切れは必ず現状維持＋台帳。公開しない★）

# 自動修正してよいscope（限定条件のない主天井のみ）。
# 「AT間」「CZ間」等の部分天井は、サイト側の構造化値が同じものを指す保証がないので対象外。
PLAIN_SCOPES = (None, "", "通常時", "not_applicable", "液晶")
# 引用文にこれらが含まれる＝限定条件つきの天井なので、主天井の置き換えには使わない
# （2026-07-21 Codex5巡目 指摘7: scope=null と申告しても引用が「AT間天井は1000G」なら別物）
# ★限定条件の検出（2026-07-21 Codex6巡目 指摘2・3）★
# 単語の完全一致では取りこぼす（AT後↔AT終了後 / 全角ＡＴ / 空白入り）ため、
# NFKC正規化＋空白除去＋小文字化した上で【パターン】で判定する。
_COND_PATTERNS = (
    r"[a-z]{2,4}間",                       # at間 / cz間 / st間 / art間 / reg間 / big間
    r"(ボーナス|ｂｏｎｕｓ|初当たり|初当り|bb|rb|ct)間",
    r"有利区間",
    # ★「後は」単独は主天井の説明（天井到達後はAT当選）まで巻き込むので使わない★
    #   限定の対象を明示した形だけを条件とする（2026-07-21 Codex7巡目 指摘2）
    r"(at|cz|art|st|bb|rb|ボーナス|ｂｏｎｕｓ|有利区間|設定変更|リセット|リセ)"
    r"(終了後|当選後|抜け後|後)",
    r"(設定変更|リセット|朝一|朝イチ|据え置き|据置)",
    r"モード[a-zａ-ｚ0-9]",                 # モードB / モード2 の天井
    r"(通常|天国|準備)[a-zａ-ｚ0-9]",       # 通常B / 通常2 等のモード限定
    r"(天国|準備モード|高確|低確|チャンスモード)",  # モード名が出たら主天井ではない
    # ★「前兆」単独は主天井の説明にも出る（規定ゲーム数消化後は前兆へ）ので前兆中に限定★
    r"(短縮|初回のみ|初回限定|前兆中|引き戻し|引戻し)",
    # ★「AT天井」「CZ天井」「ボーナス天井」＝主天井ではない別系統の天井（指摘3）★
    r"(at|cz|art|st|bb|rb|reg|big|ボーナス|ｂｏｎｕｓ|周期|スルー)天井",
    r"(非当選|非経由|未経由)",
    r"[0-9]周目",
)
_COND_RE = None


def _norm_text(s: str) -> str:
    import unicodedata
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(s or ""))).lower()


# ★天井種別ごとに「その機種では主天井を表す語」（2026-07-21 実データ較正）★
#   周期天井の機種にとって「周期天井」は主天井そのものであり条件語ではない。
#   逆にG数天井の機種で「周期天井」が出てきたら別系統なので条件語のまま。
_TYPE_VOCAB = {
    "game": ("g数天井", "ゲーム数天井", "ゲーム数", "g数"),
    "point": ("ポイント天井", "pt天井", "ポイント", "pt", "gポイント"),
    "cycle": ("周期天井", "周期数", "周期"),
    "through": ("スルー回数天井", "スルー天井", "スルー回数", "スルー"),
}


def conditional_hit(text: str, ceiling_type: str = "") -> str | None:
    """限定条件つき（主天井ではない）を示す表現があれば、その語を返す。

    ceiling_type を渡すと、その種別自身を表す語（周期天井・スルー天井等）は
    条件語とみなさない（＝周期天井の機種で「周期天井」を条件扱いしない）。
    """
    global _COND_RE
    if _COND_RE is None:
        _COND_RE = re.compile("|".join(_COND_PATTERNS))
    n = _norm_text(text)
    own = [_norm_text(w) for w in _TYPE_VOCAB.get(ceiling_type or "", ())]
    for m in _COND_RE.finditer(n):
        g = m.group(0)
        if any(g in w or w in g for w in own if len(w) >= 2):
            continue          # その機種自身の天井種別を指す語
        return g
    return None


# ★「主天井である」ことを肯定的に確認する（2026-07-21 Codex8巡目 指摘1）★
#   限定条件語の除外リストは原理的に取りこぼす（設定6のみ／CZ失敗時／2回目のみ…）。
#   そこで発想を逆にし、【値の前に書かれている語が、主天井を表す定型語だけであること】を
#   要求する。日本語では条件は値より前に来る（「設定変更時は…800G」）ため、
#   値より前の部分（prefix）だけを見れば条件の有無が判定できる。
#   値より後ろ（恩恵の説明）は判定に使わない＝正しい出典を落とさない。
_PREFIX_ALLOWED = (
    "通常時", "通常", "最大", "天井", "規定", "ゲーム数", "g数", "消化", "到達",
    "まで", "また", "なお", "この", "その", "本機", "は", "を", "で", "に", "の", "が",
    "と", "も", "や", "へ", "から", "・", "、", "。", "「", "」", "※", "→", "-", "—",
    "＋", "+", "(", ")", "[", "]", "…", "！", "!", "／", "/",
    # 機種名に付く型式記号・販売区分語（引用文が機種名から始まる場合）
    "スマートパチスロ", "スマスロ", "パチスロ", "スロット", "l", "s",
)
_PREFIX_RESIDUAL_MAX = 2       # 定型語を除いた残りがこれ以下なら「主天井の記述」とみなす


# 肯定確認で「値の単位」と認める表記（★スルーは裸の「回」を認めない★）
#  2026-07-21 Codex11巡目 指摘4: 「最大4回継続でAT当選」の"4回"をスルー天井と誤読していた
_POSITIVE_UNITS = {
    "game": ("G", "ゲーム"), "point": ("pt", "ポイント"),
    "cycle": ("周期",), "through": ("スルー",),
}


def _unit_pat(ceiling_type: str) -> str:
    """その天井種別で値の直後に来る単位表記の正規表現。

    ★右境界を付ける（2026-07-21 Codex10巡目 指摘6）★
      「4回転継続」の"4回"をスルー天井4回と誤読していた。単位の直後に
      続きの文字（転・数・目・分…）が来る場合は単位として認めない。
    """
    units = _POSITIVE_UNITS.get(ceiling_type or "", ())
    pat = "|".join(_norm_text(u) for u in units) or "g|ゲーム"
    return f"(?:{pat})(?![転数目分列個])"


# 天井の話だと分かる語（これが無い文は天井の記述として認めない）
# 「最大967G＋α消化でボーナスに当選」のように天井の語が無い書き方もあるため
# 「消化」「当選」も天井の記述を示す語に含める（「最大100pt獲得」「最大4回転継続」は除外）
# ★2026-07-21 Codex14巡目 指摘3★「消化」「当選」は一般的な遊技挙動の説明にも出るため
#   天井の語から外す（「最大800G消化でCZに突入」を主天井として通していた）。
_CEILING_RE = re.compile(r"天井|到達|規定ゲーム|規定g|ハマり|はまり")
# 「消化」「当選」は一般的な挙動説明にも出るため単独では認めず、
# 「通常時」の明示がある場合だけ天井の記述として扱う（Codex14巡目 指摘3への折衷）。
_CEILING_WEAK_RE = re.compile(r"消化(?!率)|当選(?!率|確率|割合)")


def quote_splits(quote: str, value, ceiling_type: str):
    """引用文を「値の前」「値の後」に割る。★値は単位とセットで探す★

    2026-07-21 Codex9巡目 指摘1: 値だけで探すと「最大800枚獲得。設定変更時の天井は800G」の
    最初の「800枚」を天井値だと解釈してしまう。単位付きの出現だけを値とみなす。
    戻り値 (prefix, suffix) / 見つからなければ (None, None)
    """
    n = _norm_text(quote)
    vals = []
    if float(value).is_integer():
        vals = [str(int(float(value))), f"{int(float(value)):,}"]
    else:
        vals = [str(value)]
    up = _unit_pat(ceiling_type)
    out = []
    for v in vals:
        for m in re.finditer(
                rf"(?<![0-9.,]){re.escape(_norm_text(v))}(?![0-9])\s*(?:\+?α?)?(?:{up})", n):
            out.append((n[:m.start()], n[m.end():]))
    return out


def _page_furniture() -> tuple:
    """ページ見出し・パンくずに出る定型語（平坦化で本文と連結されるため許可する）。
    2026-07-21 Codex11巡目 指摘6: 「スマスロ北斗の拳 天井・設定判別」＋本文の連結で
    正しい出典を落としていた。条件語（設定変更・AT間等）は含まれないので安全側は保たれる。"""
    # ★条件に化ける語（高設定・モード・ゾーン・リセット・朝一・有利区間・スルー等）は
    #   許可しない（2026-07-21 Codex12巡目 指摘2）★
    ng = {"高設定", "モード", "ゾーン", "リセット", "朝一", "有利区間", "スルー",
          "設定示唆", "設定差", "有利", "天国"}
    return tuple(w for w in claim_identity._STOP_WORDS if w not in ng)


def _consume_allowed(prefix: str, cores=()) -> str:
    """先頭から定型語・機種名を1語ずつ食べていき、残りを返す。

    ★全体置換ではなく先頭からの消費にする（Codex9巡目 指摘3）★
      全体置換だと「裏天井」から「天井」を抜いて「裏」だけにする等、
      未知語の内部を削って条件を見逃す。
    """
    toks = sorted([_norm_text(c) for c in (cores or ()) if c] +
                  [_norm_text(w) for w in _PREFIX_ALLOWED] +
                  [_norm_text(w) for w in _page_furniture()], key=len, reverse=True)
    rest = prefix
    changed = True
    while changed and rest:
        changed = False
        for t in toks:
            if t and rest.startswith(t):
                rest, changed = rest[len(t):], True
                break
    return rest


def _until_sentence_end(text: str) -> str:
    """文末記号までを返す（★固定文字数の窓は使わない・Codex10巡目 指摘5★）。

    ただし括弧が開いたままなら閉じ括弧まで伸ばす。注記の中に句点があると
    「天井は800G（…確認してください。設定変更時のみ）」の条件を取り逃がすため。
    """
    ends = [x for x in (text.find(d) for d in ("。", "！", "？")) if x >= 0]
    seg = text[:min(ends)] if ends else text
    while seg.count("(") > seg.count(")") or seg.count("（") > seg.count("）"):
        rest = text[len(seg):]
        nxt = min([x for x in (rest.find(")"), rest.find("）")) if x >= 0] or [-1])
        if nxt < 0:
            return text            # 閉じないなら全部見る
        seg = text[:len(seg) + nxt + 1]
    return seg


def main_ceiling_quote(quote: str, value, cores=(), ceiling_type: str = "game") -> str | None:
    """主天井の記述だと肯定的に確認できなければ理由を返す（＝修正に使わない）。"""
    if value is None or not isinstance(value, (int, float)) or isinstance(value, bool):
        return f'値が数値でない（{value!r}）'
    n_all = _norm_text(quote)
    # ★複数の文にまたがる引用は拒否（2026-07-21 Codex12巡目 指摘1）★
    #   2文目に条件を隠せば前後文の検査を迂回できるため。
    if re.search(r"[。！？](?=[^\s。！？」』）)】\]・…]) ?", n_all):
        return "引用が複数の文にまたがる＝どの文の値か決められない"
    if not (_CEILING_RE.search(n_all) or _CEILING_WEAK_RE.search(n_all)):
        # 「最大100pt獲得」「最大4回転継続」「最大800G継続、AT当選率は1/300」等
        # ＝天井の記述ではない（Codex10巡目 指摘6 / 11巡目 指摘3）
        return "天井を表す語（天井・到達・規定・消化・当選）が無い＝天井の記述と確認できない"
    # ※「同単位の値が複数ある」ことだけを理由に拒否はしない。
    #   「天井は1280G+α、設定変更時は900G+α」のような頻出表記を救うため、
    #   ★値ごとに、その値が属する節だけを見て判定する★（節の切り出しは下記）。
    splits = quote_splits(quote, value, ceiling_type)
    if not splits:
        return f"引用の中に値({value})が単位付きで見つからない"
    # ★値が複数回出るなら、どれか1つが主天井の文法に合えばよい（Codex9巡目 指摘8）★
    #   例「設定変更時は800G、通常時の天井も800G」
    reasons = []
    for prefix, suffix in splits:
        # ★天井語は「値と同じ節」に無ければならない（2026-07-21 Codex13巡目 指摘1）★
        #   「最大800G継続、通常時の天井は1000G」で後半の「天井」を借りるのを防ぐ。
        # ★節の切り出し（2026-07-21 実データ較正）★
        #   読点でも割る。ただし直前の節に【同単位の値が無い】場合だけ連結する。
        #   ・「通常時の天井は、最大1000Gです」→ 直前節に値が無いので連結して天井語を認める
        #   ・「最大800G継続、通常時の天井は1000G」→ 800の節は先頭で連結相手が無く、
        #     「継続」もあるので不合格（別の値の天井語を借りられない）
        _clauses = re.split(r"[、。！？：:／/]", prefix)
        clause_pre = _clauses[-1]
        up_ = _unit_pat(ceiling_type)
        for k in range(len(_clauses) - 2, -1, -1):
            prev = _clauses[k]
            if re.search(rf"(?<![0-9.,])[0-9]{{1,5}}\s*(?:[＋+]\s*[αa])?(?:{up_})", prev):
                break                     # 直前節に別の値がある＝そこは別の主張
            clause_pre = prev + clause_pre
            if not re.search(r"[はがも]$", prev.strip()):
                break                     # 主題提示（〜は/が/も）でなければ連結を止める
        clause_suf = re.split(r"[、。！？：:／/]", suffix or "")[0]
        clause = clause_pre + clause_suf
        strong = bool(_CEILING_RE.search(clause))
        weak = bool(_CEILING_WEAK_RE.search(clause)) and "通常時" in clause
        if not (strong or weak):
            reasons.append("値と同じ節に天井を表す語が無い")
            continue
        # ★「消化」「当選」だけで天井とみなす場合は、別の挙動の説明でないことを確認する★
        #   （天井・到達・規定が明示されている節はその語を信頼し、この検査を行わない。
        #     「スルー回数天井は最大6スルー到達で発動する」を落とさないため）
        if not strong:
            ng = re.search(r"終了|継続|獲得|上乗せ|払い?出し|純増|枚数|突破|完走|"
                           r"突入|移行|発動|昇格|転落", clause)
            if ng:
                reasons.append(f"値と同じ節が天井ではない挙動の説明（「{ng.group(0)}」）")
                continue
        # ★非断定の表現は使わない（指摘3: 「約1000G」から断定表記へ直さない）★
        if re.search(r"約|およそ|程度|前後|くらい|ほど|以上|以下|超|未満|目安|付近|"
                     r"弱|強|以内|相当|クラス|レベル", clause_pre + clause_suf[:8]):
            reasons.append("値が非断定の表現（約・程度・前後）で書かれている")
            continue
        hit = conditional_hit(clause_pre, ceiling_type)
        if hit:
            reasons.append(f"値の前に限定条件「{hit}」がある")
            continue
        # ★値の直後の括弧注記は、定型語だけで説明できなければ拒否（指摘2）★
        #   列挙方式では未知の条件（設定6のみ／偶数設定のみ／仮天井）を取り逃がす。
        # ★注記は括弧に限らない（※ ／ ： 、 で導かれる断り書きも同じ扱い・指摘2）★
        note_txt = None
        m_note = re.match(r"^\s*[（(]([^）)]*)[）)]", suffix or "")
        if m_note:
            note_txt = m_note.group(1)
        else:
            m_note2 = re.match(r"^[^。！？]{0,4}?[※／/：:、]\s*([^。！？、]{1,40})", suffix or "")
            if m_note2:
                # ★その区間に【別の値】があるなら、注記ではなく独立した主張★
                #   「天井は1280G+α、設定変更時は900G+αまで短縮」＝リセット天井の記述であり
                #   1280G に掛かる注記ではない。「、設定6のみ」のように値が無ければ注記。
                seg = m_note2.group(1)
                if not re.search(rf"(?<![0-9.,])[0-9]{{1,5}}\s*(?:[＋+]\s*[αa])?"
                                 rf"(?:{_unit_pat(ceiling_type)})", seg):
                    note_txt = seg
        if note_txt:
            rest_note = _consume_allowed(_norm_text(note_txt),
                                         list(cores or ()) +
                                         list(_TYPE_VOCAB.get(ceiling_type or "", ())))
            rest_note = _CEILING_RE.sub("", rest_note)
            rest_note = re.sub(r"[a-z0-9]", "", rest_note)   # AT/BB等の機能名は許す
            if rest_note:
                reasons.append(f"値の直後の注記が定型語で説明できない（「{note_txt[:16]}」）")
                continue
        # ★条件語の検査も「同じ節」まで（次の読点以降は独立した主張）★
        #   値を持たない断り書きは上の注記検査が捕まえるので、ここを節単位にしても
        #   「天井は800G、設定6のみ」のような迂回は塞がったまま。
        hit2 = conditional_hit(_until_sentence_end(clause_suf), ceiling_type)
        if hit2:
            reasons.append(f"値の直後に限定条件「{hit2}」がある")
            continue
        rest = _consume_allowed(prefix, list(cores or ()) +
                                list(_TYPE_VOCAB.get(ceiling_type or "", ())))
        if rest:
            reasons.append(f"値の前に定型語以外の記述がある（「{rest[:20]}」）")
            continue
        return None            # 1つでも主天井の文法に合えば合格
    return "主天井と断定できない（" + " / ".join(reasons[:2]) + "）"


# 矛盾スキャンで使う単位表記（旧値がページに載っていないかを見る）
UNIT_WORDS = {
    "game": ("G", "ゲーム"), "point": ("pt", "ポイント"),
    "cycle": ("周期",), "through": ("スルー", "回"),
}

# 天井の種類ごとに「あり得る値の範囲」（2026-07-21 Codex5巡目 指摘10）
# これを外れる値は、たとえ2ドメインで裏取りできても書かない（要確認へ）。
VALUE_RANGE = {
    "game": (100, 3000), "point": (10, 20000),
    "cycle": (1, 50), "through": (1, 20),
}
MAX_CHANGE_RATIO = 3.0   # 旧値の3倍超／3分の1未満になる修正は人の確認へ


def value_sanity(ceiling_type: str, old, new) -> str | None:
    """値そのものの妥当性。問題があれば理由文字列を返す（＝修正しない）。"""
    import math
    for label, v in (("旧値", old), ("新値", new)):
        if v is None or not isinstance(v, (int, float)) or isinstance(v, bool):
            return f"{label}が数値でない（{v!r}）"
        if not math.isfinite(float(v)):
            return f"{label}が有限の数値でない（{v!r}）"
        if float(v) != int(float(v)):
            return f"{label}が整数でない（{v!r}）"
        if float(v) <= 0:
            return f"{label}が正の数でない（{v!r}）"
    lo, hi = VALUE_RANGE.get(ceiling_type or "", (1, 100000))
    if not (lo <= float(new) <= hi):
        return f"新値{new}が{ceiling_type}の想定範囲({lo}〜{hi})の外"
    ratio = float(new) / float(old)
    if ratio > MAX_CHANGE_RATIO or ratio < 1 / MAX_CHANGE_RATIO:
        return f"変化が大きすぎる（{old}→{new}）＝人の確認が必要"
    return None

EXTRA_RULES = """

追加ルール（重要）:
A. 各claimには★異なる2ドメインの出典★を evidence に入れること（chonborista.com /
   1geki.jp / nana-press.com / slopachi-quest.com のうち2つ以上）。1つしか確認できない
   claimも返してよいが、その場合は1件だけ入れる（水増し・同一ドメインの別ページで
   2件にするのは禁止）。
B. 主天井（通常時のゲーム数天井など）と、条件付きの部分天井（AT間・CZ間・ボーナス間）を
   混同しない。条件付きの場合は必ず scope にその条件を書く。
"""


LOCK_PY = SCRIPTS / "task_lock.py"


def _lock(cmd: str, *args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(LOCK_PY), cmd, *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120, creationflags=_NO_WINDOW)


def lock_acquire() -> str | None:
    """4タスク共通ロックを取得。CTXパスを返す（取れなければNone）。"""
    r = _lock("acquire", "--task", TASK_ID)
    ctx = next((ln[4:] for ln in (r.stdout or "").splitlines() if ln.startswith("CTX=")), None)
    if not ctx:
        log(f"ロック取得できず: {(r.stdout or '').strip()[:200]}")
    return ctx


def heartbeat(ctx: str, note: str = "") -> None:
    if ctx:
        _lock("heartbeat", "--ctx", ctx)
        if note:
            log(f"  ・{note}")


def fencing_ok(ctx: str) -> bool:
    """★書き込み（commit/push/state）の直前に必ず呼ぶ。世代交代したゾンビ実行を止める★"""
    if not ctx:
        return False
    r = _lock("check", "--ctx", ctx)
    return "OWNER_OK" in (r.stdout or "")


def now() -> datetime.datetime:
    return datetime.datetime.now()


def iso(dt=None) -> str:
    return (dt or now()).strftime("%Y-%m-%dT%H:%M:%S")


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{now():%H:%M:%S}] {msg}"
    with open(LOG_DIR / f"codex_audit_{now():%Y-%m-%d}.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def load_json(p: Path, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json_atomic(p: Path, obj) -> None:
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


# ─────────────────────────────────────────────
# 対象選定（評価日が最も古い順＝日付ベース。位置番号方式は使わない）
# ─────────────────────────────────────────────

def select_slugs(machines: list[dict], state: dict, n: int) -> list[str]:
    seen = (state.get("codex_audit") or {}).get("last_audited") or {}
    cands = [m for m in machines if (m.get("status") or "complete") == "complete"]
    cands.sort(key=lambda m: (seen.get(m["slug"], ""), m["slug"]))
    return [m["slug"] for m in cands[:n]]


# ─────────────────────────────────────────────
# 判定（純関数・通信なし＝selftestで検証できる）
# ─────────────────────────────────────────────

def classify(site_claims: list[dict], codex_claims: list[dict],
             comparison: list[dict], auto_fix_allowed: bool = True,
             cores=()) -> dict:
    """比較結果を「修正候補 / 要確認 / 変更なし」に仕分ける。書き込みはしない。

    auto_fix_allowed=False の機種（＝スマスロ機でない機種）は、同名の旧世代機と
    ページを区別する材料がタイトルに無いため【自動修正しない】（要確認へ回す）。
    2026-07-21 Codex3巡目 指摘7・8: 4号機/5号機/6号機や増台版は表記だけでは判別不能。
    """
    by_key = {}
    for c in codex_claims:
        key = shadow_gold.migrate_claim_key(c.get("claim_key") or "",
                                            c.get("ceiling_type") or "")
        by_key.setdefault(key, []).append(c)
    site_by_key = {}
    dup_site_keys = set()
    for sc in site_claims:
        k = sc.get("claim_key")
        if k in site_by_key:
            dup_site_keys.add(k)     # 同じ項目が2つ＝構造が曖昧（黙って上書きしない）
        site_by_key[k] = sc

    out = {"fix_candidates": [], "reviews": [], "unchanged": []}
    for r in comparison:
        key = r["claim_key"]
        verdict, sub = r["verdict"], r.get("sub")
        site = site_by_key.get(key) or {}
        cands = by_key.get(key) or []
        codex = cands[0] if len(cands) == 1 else None

        diverged = (verdict == "MISMATCH") or (verdict == "UNKNOWN" and sub == "numeric_divergence")
        if not diverged:
            if verdict in ("MATCH",) or sub == "numeric_alignment":
                out["unchanged"].append({"claim_key": key, "detail": r["detail"]})
            elif verdict in ("MISSING_IN_CODEX", "MISSING_IN_SITE"):
                # 構造そのものの食い違い（例: サイトはG数天井・Codexは周期天井）は
                # 値の差し替えでは直らない＝人が構造を判断する必要がある
                out["reviews"].append({"claim_key": key, "kind": "structure",
                                       "detail": r["detail"], "auto_fixable": False})
            else:
                # ★どの分類にも入らない判定を黙って捨てない（Codex5巡目 指摘12）★
                #   捨てると「確認もされず誤りが残る」状態になる。
                out["reviews"].append({"claim_key": key, "kind": "other",
                                       "detail": r["detail"], "auto_fixable": False,
                                       "reason": f"判定={verdict}/{sub}（自動修正の対象外）"})
            continue

        why = None
        ev_ok = [e for e in (codex or {}).get("evidence_results") or [] if e.get("verified")]
        ev_domains = sorted({shadow_codex._etld1(e.get("source_url") or "")
                             for e in ev_ok} - {""})
        # ★検証に成功したURLの引用だけを使う（Codex6巡目 指摘5）★
        #   evidence と evidence_results が対応していないと、検証していない引用で
        #   条件検査を通してしまう。URLで突き合わせ、対応が取れない場合は修正しない。
        ok_urls = {e.get("source_url") for e in ev_ok}
        # ★URLごとに1件へ正規化（2026-07-21 Codex13巡目 指摘7）★
        #   同じURLの引用を2件入れて件数だけ揃える水増しを防ぐ。
        _by_url = {}
        for e in ((codex or {}).get("evidence") or []):
            if e.get("source_url") in ok_urls and e.get("raw_quote"):
                _by_url.setdefault(e.get("source_url"), e)
        ev_used = list(_by_url.values())
        quotes = " / ".join((e.get("raw_quote") or "") for e in ev_used)
        if not auto_fix_allowed:
            why = ("スマスロ機ではないため自動修正の対象外"
                   "（同名の旧世代機とページを機械的に区別できない）")
        elif codex is None:
            why = "同じ項目にCodexの主張が複数あり一意に決まらない" if cands else "Codex主張が取れない"
        # ★申告を信用せず、実際の検証結果から数え直す（2026-07-21 Codex5巡目 指摘8）★
        elif codex.get("assertion_status") != "asserted":
            why = f"Codexの主張が確定でない（assertion_status={codex.get('assertion_status')}）"
        elif {e.get("source_url") for e in ev_used} != ok_urls:
            why = "検証に成功した出典と引用文が1対1で対応していない（内部不整合）"
        elif len(ev_domains) < 2:
            why = (f"裏取りが独立2ドメインに届かない"
                   f"（検証成功ドメイン={ev_domains} / 申告={codex.get('evidence_strength')}）")
        elif codex.get("evidence_strength") != "verified_policy":
            why = f"出典強度の申告が不足（{codex.get('evidence_strength')}）"
        elif codex.get("scope") not in PLAIN_SCOPES:
            why = f"条件付きの天井（scope={codex.get('scope')}）＝サイトの値と同じものか決まらない"
        # ★引用文に限定条件が書かれていれば主天井の置き換えに使わない（指摘7）★
        # ★全ての引用が「主天井の記述」だと肯定的に確認できること（Codex8巡目 指摘1）★
        elif any(main_ceiling_quote(e.get("raw_quote"), codex.get("value"), cores,
                                    site.get("ceiling_type") or "") for e in ev_used):
            bad = next(x for x in (main_ceiling_quote(
                e.get("raw_quote"), codex.get("value"), cores,
                site.get("ceiling_type") or "") for e in ev_used) if x)
            why = f"引用を主天井の記述と確認できない（{bad}）"
        elif site.get("value") is None or codex.get("value") is None:
            why = "値が欠けている"
        elif shadow_claims._norm_unit(site.get("unit")) != shadow_claims._norm_unit(codex.get("unit")):
            why = f"単位が違う（site={site.get('unit')} / codex={codex.get('unit')}）"
        # ★天井の種類が一致していること（指摘9: 単位すり替えでの誤修正を防ぐ）★
        elif key in dup_site_keys:
            why = "サイト側に同じ項目が複数あり、どれを直すか決まらない（構造の要確認）"
        elif (site.get("ceiling_type") or "") != (codex.get("ceiling_type") or ""):
            why = (f"天井の種類が違う（site={site.get('ceiling_type')} / "
                   f"codex={codex.get('ceiling_type')}）")
        # ★scopeについて★: サイト側の構造化データに scope は無い（machines.json の
        #   limit は定義上「主天井」）。したがって scope 未構造化そのものは修正を止める
        #   理由にしない。代わりに「Codexが限定条件を名乗っていないこと（PLAIN_SCOPES）」と
        #   「引用文に限定条件語が無いこと」で守る（上の2条件）。
        #   ただしCodexが★既知の語彙に無いscope★を出した場合は意味が確定しないので止める。
        elif len({alpha_in_quote(e.get("raw_quote"), codex.get("value"),
                                 site.get("ceiling_type") or "") for e in ev_used}) > 1:
            why = "出典ごとに＋αの有無が食い違う"
        elif (codex.get("plus_alpha") is not None
              and next(iter({alpha_in_quote(e.get("raw_quote"), codex.get("value"),
                                            site.get("ceiling_type") or "")
                             for e in ev_used}), None) is not None
              and bool(codex.get("plus_alpha")) != next(iter(
                  {alpha_in_quote(e.get("raw_quote"), codex.get("value"),
                                  site.get("ceiling_type") or "") for e in ev_used}))):
            why = "Codexの＋α申告と引用文の表記が食い違う"
        elif (codex.get("operator") and any(
                operator_in_quote(e.get("raw_quote"), codex.get("value"),
                                  site.get("ceiling_type") or "") not in
                (None, codex.get("operator")) for e in ev_used)):
            why = "Codexのoperator申告と引用文の表現が食い違う"
        elif (codex.get("operator") or "exact") not in ("exact", "max"):
            # 「約」「以上」等は数値を断定表記へ置き換えると意味が変わる（指摘5）
            why = f"出典の表現が断定でない（operator={codex.get('operator')}）"
        elif any(site.get(a) is not None and codex.get(a) != site.get(a)
                 for a in ("operator", "plus_alpha")):
            why = ("operator/plus_alpha が食い違う"
                   f"（site={site.get('operator')}/{site.get('plus_alpha')} "
                   f"codex={codex.get('operator')}/{codex.get('plus_alpha')}）")
        elif "scope_unverified" in (r.get("attrs_unverified") or []):
            why = f"Codexのscopeが未知の語（{codex.get('scope')}）＝同じ項目と確定できない"
        else:
            why = value_sanity(site.get("ceiling_type"), site.get("value"), codex.get("value"))

        rec = {"claim_key": key, "site_value": site.get("value"),
               "codex_value": (codex or {}).get("value"),
               "unit": (codex or {}).get("unit") or site.get("unit"),
               "ceiling_type": (codex or {}).get("ceiling_type") or site.get("ceiling_type"),
               "site_ceiling_type": site.get("ceiling_type"),
               "evidence": [e.get("source_url") for e in ev_used],
               "pairs": [(e.get("source_url"), e.get("raw_quote")) for e in ev_used],
               "cores": list(cores or ()),
               "plus_alpha": next(iter({alpha_in_quote(
                   e.get("raw_quote"), (codex or {}).get("value"),
                   site.get("ceiling_type") or "") for e in ev_used}), None)
               if ev_used else (codex or {}).get("plus_alpha"),
               "verified_domains": (codex or {}).get("verified_domains") or [],
               "detail": r["detail"]}
        if why:
            rec.update(kind="value", auto_fixable=False, reason=why)
            out["reviews"].append(rec)
        else:
            rec.update(kind="value", auto_fixable=True)
            out["fix_candidates"].append(rec)
    return out


# ─────────────────────────────────────────────
# 矛盾スキャン（旧値も同じページに載っていないか）
# ─────────────────────────────────────────────



def contradiction_scan(cand: dict, allowed, pairs=()) -> str | None:
    """裏取りページを実際に開いて2つを見る（問題があれば理由文字列＝修正しない）。

    1. 旧値が同じ単位でページに載っていないか（どちらが正か機械で決まらない）
    2. ★引用の前後（見出し・直前文）に限定条件が書かれていないか★
       （2026-07-21 Codex6巡目 指摘3: 「設定変更時の天井」という見出しの下に
         「天井は800GでAT当選。」とある場合、引用文だけ見ても条件が分からない）
    """
    old = cand.get("site_value")
    if old is None:
        return "旧値が無い"
    # ★サイト側の天井種別で単位を決める（Codexの申告でのすり替えを防ぐ・指摘9）★
    units = UNIT_WORDS.get(cand.get("site_ceiling_type") or cand.get("ceiling_type") or "", ())
    if not units:
        return f"単位が特定できない（ceiling_type={cand.get('ceiling_type')}）"
    olds = apply_external_fix._num_variants(old)
    for url, quote in (pairs or [(u, None) for u in (cand.get("evidence") or [])]):
        page = verify_claims.fetch_page(url, allowed=allowed)
        if page is None:
            return f"矛盾スキャンでページを取得できない（{url}）"
        text = verify_claims.normalize(page.text)
        for o in olds:
            for u in units:
                # ★桁境界つきで探す（Codex9巡目 指摘11: 旧値600が1600Gに一致していた）★
                if re.search(rf"(?<![0-9.,]){re.escape(o)}\s*(?:\+?α?)?{re.escape(u)}"
                             rf"(?![転数目分列個])", text):
                    return (f"裏取りページに旧値「{o}{u}」も載っている（{url}）"
                            f"＝どちらが正しいか機械で決められない")
        # ★ページ本文そのものに「主天井の文法」を当てる（2026-07-21 Codex10巡目）★
        #   引用文だけを見る方式は、引用を値で打ち切るだけで迂回できた（指摘1）。
        #   ページ上で引用が出てくる【文】を切り出し、引用と同じ肯定確認を適用する。
        #   文の区切りは「。！？」だけ（【】「」・ は条件ラベルの区切りにもなるので
        #   境界にしない＝「【設定変更時】天井は800G」を取り逃がさない・指摘2）。
        #   改行も境界にしない（見出しの直後に値が来る形を取り逃がさない・指摘3）。
        if not quote:
            continue
        ntext = _norm_text(page.text)
        nq = _norm_text(quote)
        hits = [i for i in range(len(ntext)) if ntext.startswith(nq, i)]
        if not hits:
            return f"引用がページ上で見つからない（{url}）＝文脈を確認できない"
        if len(hits) > 1:
            # 同じ引用がページ内に複数ある＝どの文脈の値か決められない（指摘4）
            return f"引用がページ内に複数ある（{url}）＝どの文脈の値か決められない"
        i = hits[0]
        # ★引用を含む「文」＋前後の文まで見る（2026-07-21 Codex11巡目 指摘1・2）★
        #   ・引用の文 → 主天井の文法で肯定確認
        #   ・前後の文 → 条件語があり、かつ別の値が書かれていなければ、その条件は
        #     この値に掛かっているとみなして不合格（「設定変更時だけ適用される短縮天井
        #     です。天井は800G」／「天井は800G。※設定変更時のみ適用」）
        #   ・括弧が開いたままなら閉じ括弧まで文を伸ばす（括弧内の句点対策）
        def _sentence_bounds(text: str, pos: int, length: int):
            st = max(text.rfind(d, 0, pos) for d in ("。", "！", "？"))
            ends = [x for x in (text.find(d, pos + length) for d in ("。", "！", "？"))
                    if x >= 0]
            en = min(ends) if ends else len(text) - 1
            seg = text[st + 1:en + 1]
            while seg.count("(") > seg.count(")") or seg.count("（") > seg.count("）"):
                rest = text[en + 1:]
                nxt = min([x for x in (rest.find(")"), rest.find("）")) if x >= 0] or [-1])
                if nxt < 0:
                    en = len(text) - 1
                    break
                en = en + 1 + nxt
                seg = text[st + 1:en + 1]
            return st, en, seg

        s_start, s_end, sentence = _sentence_bounds(ntext, i, len(nq))
        # ★平坦化でページ見出し（<title>）が本文と連結された分は取り除く（指摘9）★
        ntitle = _norm_text(page.title or "")
        if ntitle and sentence.startswith(ntitle):
            sentence = sentence[len(ntitle):]
        why = main_ceiling_quote(sentence, cand.get("codex_value"),
                                 cand.get("cores") or (),
                                 cand.get("site_ceiling_type") or "")
        if why:
            return (f"ページ上の該当文を主天井の記述と確認できない（{url}）: {why}"
                    f"／該当文=「{sentence[:60]}」")
        # 前後の文に「この値に掛かる条件」が書かれていないか
        units = UNIT_WORDS.get(cand.get("site_ceiling_type") or "", ())
        prev_seg = _sentence_bounds(ntext, max(0, s_start), 0)[2] if s_start > 0 else ""
        next_seg = _sentence_bounds(ntext, min(s_end + 1, len(ntext) - 1), 0)[2]
        for label, seg in (("直前の文", prev_seg), ("直後の文", next_seg)):
            hit = conditional_hit(seg, cand.get("site_ceiling_type") or "")
            if not hit:
                continue
            # ★条件語と【同じ節】に別の値があるときだけ「その値の話」とみなす★
            #   （2026-07-21 Codex13巡目 指摘5: 文のどこかに別の値があるだけで
            #     迂回できた。「設定変更時のみ適用（狙い目は500Gから）。天井は800G」）
            exempt = False
            for cl in re.split(r"[、。！？（）()]", seg):
                if not conditional_hit(cl, cand.get("site_ceiling_type") or ""):
                    continue
                # ★別の値があるだけでは緩和しない。その節が【天井の話】であること★
                #   （2026-07-21 Codex14巡目 指摘5: 「狙い目は500Gから」で迂回できた）
                if not (_CEILING_RE.search(cl) or "短縮" in cl):
                    continue
                if re.search(rf"(?<![0-9.,])(?!{re.escape(str(int(float(cand['codex_value']))))}"
                             rf"(?![0-9]))[0-9]{{2,5}}\s*(?:\+?α?)?"
                             rf"(?:{_unit_pat(cand.get('site_ceiling_type') or '')})", cl):
                    exempt = True
                    break
            if exempt:
                continue
            return (f"{label}に限定条件「{hit}」があり、別の値も書かれていない（{url}）"
                    f"＝この値に掛かる条件の可能性がある／該当文=「{seg[-50:]}」")
    return None


# ─────────────────────────────────────────────
# 台帳・通知
# ─────────────────────────────────────────────

ISSUE_FAILURES: list[str] = []


def add_issue(slug: str, kind: str, title: str, detail: str) -> None:
    """要確認台帳へ登録。★無人タスクはcloseしない（登録のみ）★

    ★登録に失敗したら記録して後で異常として扱う（黙って消さない）★
    """
    try:
        r = subprocess.run([sys.executable, str(OPEN_ISSUES), "add",
                            "--source", "codex-audit", "--slug", slug,
                            "--kind", kind, "--title", title, "--detail", detail],
                           capture_output=True, text=True, timeout=60,
                           creationflags=_NO_WINDOW)
        if r.returncode != 0:
            ISSUE_FAILURES.append(f"{slug}:{title[:40]}")
            log(f"台帳登録が失敗（処理は継続）: {(r.stderr or r.stdout or '')[:200]}")
    except Exception as e:
        ISSUE_FAILURES.append(f"{slug}:{title[:40]}")
        log(f"台帳登録に失敗（処理は継続）: {e}")


def notify(subject: str, body: str) -> None:
    try:
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        bf = AUDIT_DIR / "notify_body.txt"
        bf.write_text(body, encoding="utf-8")
        subprocess.run([sys.executable, SEND_NOTIFY, "notify",
                        "--subject", subject, "--body-file", str(bf)],
                       capture_output=True, text=True, timeout=120,
                       creationflags=_NO_WINDOW)
    except Exception as e:
        log(f"メール送信失敗（処理は継続）: {e}")


# ─────────────────────────────────────────────
# 1機種の点検
# ─────────────────────────────────────────────

def audit_machine(machine: dict, machines: list[dict], run_id: str,
                  deadline: datetime.datetime) -> dict:
    slug = machine["slug"]
    res = {"slug": slug, "error": None, "classified": None, "fixes": [], "reviews": []}
    m_deadline = min(deadline, now() + datetime.timedelta(seconds=MACHINE_BUDGET_SEC))
    meta = shadow_codex.run_codex(machine, run_id, m_deadline,
                                  prompt=shadow_codex.build_prompt(machine, EXTRA_RULES))
    if meta.get("error"):
        res["error"] = meta["error"]
        return res
    claims = (meta["output"] or {}).get("claims") or []
    for c in claims:
        c["evidence_results"] = shadow_codex.verify_evidence(machine, c, run_id)
        oks = sorted({shadow_codex._etld1(er["source_url"])
                      for er in c["evidence_results"] if er.get("verified")} - {""})
        c["verified_domains"] = oks
        if c.get("assertion_status") == "asserted":
            c["evidence_strength"] = ("cannot_verify" if not oks else
                                      "verified_single" if len(oks) == 1 else "verified_policy")
            if not oks:
                c["assertion_status"] = "cannot_verify"
    site_claims = shadow_claims.extract_site_claims(machine)
    comparison = shadow_claims.compare_claims(
        site_claims, claims,
        codex_key=lambda c: shadow_gold.migrate_claim_key(
            c.get("claim_key") or "", c.get("ceiling_type") or ""))
    if not comparison:
        res["empty_comparison"] = True
    all_ms = json.loads((BASE / "assets" / "data" / "machines.json").read_text(encoding="utf-8"))
    res["classified"] = classify(
        site_claims, claims, comparison,
        auto_fix_allowed=("smart" in claim_identity.machine_tags(machine)),
        cores=claim_identity.accept_cores_for(machine, all_ms))
    res["site_claims"] = site_claims
    res["codex_claims"] = claims
    res["comparison"] = comparison
    return res


# ─────────────────────────────────────────────
# 修正の適用（1件ずつ・全部通ってから書く）
# ─────────────────────────────────────────────

_ALPHA_RE = re.compile(r"[＋+]\s*(?:[αa]|ａ|アルファ)|プラスアルファ")


def operator_in_quote(quote: str, value, ceiling_type: str) -> str | None:
    """引用から operator を独立抽出する（最大→max / それ以外→exact）。
    2026-07-21 Codex14巡目 指摘4: Codexの申告と引用の食い違いを検出するため。"""
    n = _norm_text(quote)
    up = _unit_pat(ceiling_type)
    v = str(int(float(value))) if float(value).is_integer() else str(value)
    m = re.search(rf"(?<![0-9.,]){re.escape(v)}(?![0-9])\s*(?:[＋+]\s*[αa])?(?:{up})", n)
    if not m:
        return None
    pre = re.split(r"[、。！？]", n[:m.start()])[-1]
    return "max" if re.search(r"最大|max", pre) else "exact"


def alpha_in_quote(quote: str, value, ceiling_type: str) -> bool | None:
    """引用の中で、その値に＋αが付いているか（判定できなければ None）。
    2026-07-21 Codex13巡目 指摘4: Codexの申告だけを信じず引用から独立抽出する。"""
    n = _norm_text(quote)
    up = _unit_pat(ceiling_type)
    v = str(int(float(value))) if float(value).is_integer() else str(value)
    m = re.search(rf"(?<![0-9.,]){re.escape(v)}(?![0-9])\s*(?:[＋+]\s*[αa])?(?:{up})"
                  rf"(?:\s*(?:[＋+]\s*(?:[αa]|アルファ)|プラスアルファ))?", n)
    if not m:
        return None
    return bool(re.search(r"[＋+]\s*(?:[αa]|アルファ)|プラスアルファ", m.group(0)))


def alpha_mismatch(slug: str, cand: dict) -> str | None:
    """記事本文の「+α」の有無と、出典側の plus_alpha 主張が食い違えば理由を返す。

    ★判定は「実際に書き換える箇所」だけで行う（2026-07-21 Codex12巡目 指摘4）★
      記事JSON全体から最初の同値表記を拾うと、別機能の「900G+α継続」を
      主天井の表記と取り違える。
    """
    want = cand.get("plus_alpha")
    if want is None:
        return None                      # 出典が言及していなければ判定しない
    path = BASE / "assets" / "data" / "machine-details" / f"{slug}.json"
    if not path.exists():
        return None
    try:
        detail = json.loads(path.read_text(encoding="utf-8"))
        edits = apply_external_fix.plan_prose_edits(
            detail, cand["claim_key"], float(cand["site_value"]),
            float(cand["codex_value"]))
    except Exception as e:
        return f"本文の置換箇所を特定できない（{type(e).__name__}: {e}）"
    if not edits:
        return None                      # 本文に該当表記が無ければ判定しない
    units = UNIT_WORDS.get(cand.get("site_ceiling_type") or "", ())
    olds = apply_external_fix._num_variants(cand["site_value"])
    seen = set()
    for e in edits:
        for o in olds:
            for u in units:
                # ★+α は単位の前後どちらにも来る（1268G+α / 1268+αG）。
                #   長い方を先に試すのではなく、1つのパターンで両方を吸収する。
                for m in re.finditer(
                        rf"(?<![0-9.,]){re.escape(o)}\s*(?:[＋+]\s*[αa])?"
                        rf"{re.escape(u)}"
                        rf"(?:\s*(?:[＋+]\s*(?:[αa]|ａ|アルファ)|プラスアルファ))?",
                        e["before"]):
                    seen.add(bool(_ALPHA_RE.search(m.group(0))))
    if not seen:
        return None
    if len(seen) > 1:
        return "本文の同値表記で＋αの有無が混在している＝どれを直すか決められない"
    if bool(want) != seen.pop():
        return (f"本文の表記と出典の＋αの有無（{want}）が食い違う"
                f"＝数字だけ置換すると意味が変わる")
    return None


def apply_one(slug: str, cand: dict, allowed, apply_mode: bool) -> dict:
    field = cand["claim_key"]
    why = contradiction_scan(cand, allowed, cand.get("pairs") or ())
    if why:
        return {"applied": False, "reason": why, "field": field, **_cand_view(cand)}
    # ★記事本文の「＋α」の有無が出典の主張と一致するか（Codex11巡目 指摘5）★
    #   数字だけ置換すると「800G＋α」の出典で記事を「800Gちょうど」と読ませてしまう。
    #   本文側の表記と出典側の plus_alpha が食い違うなら直さず要確認へ回す。
    why_alpha = alpha_mismatch(slug, cand)
    if why_alpha:
        return {"applied": False, "reason": why_alpha, "field": field,
                "struct_path": None, "prose_edits": [], **_cand_view(cand)}
    r = apply_external_fix.run(BASE, slug, field, float(cand["site_value"]),
                               float(cand["codex_value"]), apply_mode)
    # ★書き込み後、記事データから claim を抽出し直して承認内容と一致するか確認する★
    #   （2026-07-21 Codex9巡目 指摘7: 数値だけ書き換えて意味の組を再検証していない）
    if r["applied"]:
        ms = json.loads((BASE / "assets" / "data" / "machines.json").read_text(encoding="utf-8"))
        m2 = next((x for x in ms if x.get("slug") == slug), None)
        got = next((c for c in shadow_claims.extract_site_claims(m2 or {})
                    if c.get("claim_key") == field), None)
        expect_unit = shadow_claims._norm_unit(cand.get("unit"))
        if (got is None or float(got.get("value") or -1) != float(cand["codex_value"])
                or shadow_claims._norm_unit(got.get("unit")) != expect_unit
                or (got.get("ceiling_type") or "") != (cand.get("site_ceiling_type") or "")):
            # ★書き込みを巻き戻す（Codex10巡目 指摘8: 不一致でも書き込みが残っていた）★
            _restore([{"slug": slug, "field": field}])
            return {"applied": False, "field": field,
                    "reason": f"書き込み後の再抽出が承認内容と一致しない（{got}）",
                    "struct_path": r["struct_path"], "prose_edits": r["prose_edits"],
                    **_cand_view(cand)}
    return {"applied": r["applied"], "reason": r["reason"], "field": field,
            "struct_path": r["struct_path"], "prose_edits": r["prose_edits"],
            **_cand_view(cand)}


def _sh(cmd: list[str], timeout=600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          creationflags=_NO_WINDOW)


def _git_show(path: str) -> str | None:
    r = _sh(["git", "show", f"HEAD:{path}"])
    return r.stdout if r.returncode == 0 else None


def semantic_diff_ok(fixes: list[dict]) -> tuple[bool, str]:
    """★コミット直前に「中身の差分」を検査する（2026-07-21 Codex6巡目 指摘1）★

    ファイル単位の git add では、実行前から作業ツリーにあった【未検証の変更】まで
    一緒に公開してしまう。そこで HEAD の内容と現在の内容を読み比べ、
    ★今回直すと決めた (slug, 項目) 以外に値の変化が無いこと★を確認する。
    """
    intended = {(f["slug"], f["field"]): (float(f["old"]), float(f["new"])) for f in fixes}
    cur_raw = (BASE / "assets" / "data" / "machines.json").read_text(encoding="utf-8")
    head_raw = _git_show("assets/data/machines.json")
    if head_raw is None:
        return False, "HEADのmachines.jsonを読めない"
    try:
        cur = {m["slug"]: m for m in json.loads(cur_raw)}
        head = {m["slug"]: m for m in json.loads(head_raw)}
    except Exception as e:
        return False, f"machines.jsonを読めない: {e}"
    if set(cur) != set(head):
        return False, f"機種の増減がある（{sorted(set(cur) ^ set(head))[:5]}）"
    for slug in cur:
        a, b = json.dumps(head[slug], ensure_ascii=False, sort_keys=True),             json.dumps(cur[slug], ensure_ascii=False, sort_keys=True)
        if a == b:
            continue
        keys = [k for (sl, k) in intended if sl == slug]
        if not keys:
            return False, f"今回の対象でない機種に変更がある（{slug}）→公開しない"
        # 変更が「意図した項目の値」だけであることを確認する
        try:
            cont_h, key_h, val_h, _ = apply_external_fix.locate_struct(head[slug], keys[0])
            cont_c, key_c, val_c, _ = apply_external_fix.locate_struct(cur[slug], keys[0])
        except Exception as e:
            return False, f"{slug}: 変更箇所を特定できない（{e}）"
        exp_old, exp_new = intended[(slug, keys[0])]
        if float(val_h) != exp_old or float(val_c) != exp_new:
            return False, (f"{slug}: 想定外の値変化（HEAD={val_h} / 現在={val_c} / "
                           f"想定={exp_old}→{exp_new}）")
        probe = json.loads(json.dumps(cur[slug]))
        try:
            c2, k2, _, _ = apply_external_fix.locate_struct(probe, keys[0])
            c2[k2] = val_h                     # 値を戻したらHEADと一致するはず
        except Exception as e:
            return False, f"{slug}: 差分検査に失敗（{e}）"
        if json.dumps(probe, ensure_ascii=False, sort_keys=True) != a:
            return False, f"{slug}: 値以外の変更が混ざっている→公開しない"

    # ★記事本文（machine-details）側も、今回の置換以外に変化が無いことを確認する★
    #   置換は「旧値→新値」の文字列差し替えだけのはずなので、
    #   新値を旧値へ戻したらHEADの内容と一致しなければならない。
    for f in fixes:
        rel = f"assets/data/machine-details/{f['slug']}.json"
        head_txt = _git_show(rel)
        cur_txt = (BASE / rel).read_text(encoding="utf-8")
        if head_txt is None:
            return False, f"HEADの{rel}を読めない"
        if head_txt == cur_txt:
            continue                      # 本文に該当表記が無かった機種（構造化値のみ修正）
        try:
            head_j, cur_j = json.loads(head_txt), json.loads(cur_txt)
        except Exception as e:
            return False, f"{rel}を読めない: {e}"
        # ★HEADの本文に「同じ決定論の置換」を適用したら現在の本文と一致するはず★
        #   （置換記録の文字列に頼らず、同じ関数で再現して突き合わせる）
        try:
            edits = apply_external_fix.plan_prose_edits(
                head_j, f["field"], float(f["old"]), float(f["new"]))
        except apply_external_fix.Abort as e:
            return False, f"{rel}: 置換を再現できない（{e}）→公開しない"
        for e in edits:
            e["container"][e["key"]] = e["after"]
        if json.dumps(head_j, ensure_ascii=False, sort_keys=True) !=                 json.dumps(cur_j, ensure_ascii=False, sort_keys=True):
            return False, f"{rel}: 本文に今回の置換以外の変更がある→公開しない"
    return True, "差分は今回の修正だけ（機種データ・記事本文とも）"


def _restore(fixes: list[dict]) -> None:
    """★書き込み後に公開できなかった時は作業ツリーを必ず元へ戻す★

    残したままにすると、翌朝5:05のverifyタスクが「未公開・未検証の変更」を
    巻き込んでコミットしてしまう（自動タスク同士の事故）。
    """
    slugs = sorted({f["slug"] for f in fixes})
    paths = ["assets/data/machines.json", "sitemap.xml", "service-worker.js"]
    paths += [f"assets/data/machine-details/{s_}.json" for s_ in slugs]
    paths += [f"machines/{s_}/index.html" for s_ in slugs]
    paths += ["guide-tenjo-ranking.html", "guide-reset-ranking.html",
              "guide-suru-tenjo.html", "guide-ichiran.html"]
    exist = [q for q in paths if (BASE / q).exists()]
    _sh(["git", "reset", "--"] + exist)
    _sh(["git", "checkout", "--"] + exist)


def publish(fixes: list[dict], ctx: str) -> tuple[bool, str]:
    """修正後の再ビルド→検査→コミット→push。★検査が通らなければコミットしない★

    戻り値 (公開したか, 説明)。失敗時は作業ツリーを元に戻す（中途半端に公開しない）。
    """
    slugs = sorted({f["slug"] for f in fixes})
    ok, why = semantic_diff_ok(fixes)
    if not ok:
        _restore(fixes)
        return False, f"差分検査で不合格（{why}）→修正を取り消した"
    steps = [
        (["python", "scripts/build_machine_pages.py"], "記事ページ再生成"),
        (["python", "scripts/build_hub_pages.py"], "ハブ4ページ再生成"),
        (["python", "scripts/validate_machine_data.py"], "数値整合チェック"),
        (["python", "scripts/audit_site.py"], "サイト構造監査"),
    ]
    for cmd, label in steps:
        r = _sh(cmd)
        if r.returncode != 0:
            tail = ((r.stdout or "") + (r.stderr or ""))[-800:]
            _sh(["git", "checkout", "--", "."])
            return False, f"{label}が不合格（rc={r.returncode}）→修正を取り消した:\n{tail}"
        log(f"  ・{label} OK")
        heartbeat(ctx)

    # コミット対象は明示パスのみ（add -A 禁止）
    paths = ["assets/data/machines.json", "sitemap.xml", "service-worker.js",
             "guide-tenjo-ranking.html", "guide-reset-ranking.html",
             "guide-suru-tenjo.html", "guide-ichiran.html"]
    paths += [f"assets/data/machine-details/{s}.json" for s in slugs]
    paths += [f"machines/{s}/index.html" for s in slugs]
    exist = [p for p in paths if (BASE / p).exists()]
    _sh(["git", "add", "--"] + exist)
    st = _sh(["git", "status", "--porcelain"])
    unstaged = [ln for ln in (st.stdout or "").splitlines()
                if ln[:2] not in ("M ", "A ", "D ") and ln.strip()]
    if unstaged:
        log(f"  ⚠ 対象外の変更を検知（台帳へ）: {unstaged[:5]}")
        add_issue("site", "other", "[codex-audit] コミット対象外の変更を検知",
                  "\n".join(unstaged)[:1000])

    lines = [f"- {f['slug']} {f['field']}: {f['old']} → {f['new']}"
             f"（出典 {', '.join(f.get('domains') or [])}）" for f in fixes]
    msg = ("fix(codex-audit): 裏取り済みの外部数値を自動修正\n\n" + "\n".join(lines) +
           "\n\n出典は verify_claims.py の関所を独立2ドメインで通過したもののみ。\n\n"
           "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>")
    if not fencing_ok(ctx):
        _restore(fixes)
        return False, "ロックの世代が変わっている（別実行に交代済み）→書き込みを中止した"
    r = _sh(["git", "commit", "-m", msg])
    if r.returncode != 0:
        out = ((r.stdout or "") + (r.stderr or ""))[-400:]
        _restore(fixes)
        return False, f"コミット失敗（修正は取り消した）: {out}"
    r = _sh(["git", "push"], timeout=300)
    if r.returncode != 0:
        return False, f"push失敗（コミットは済み・次回再送）: {((r.stderr or ''))[-400:]}"
    return True, f"{len(fixes)}件を公開（{', '.join(slugs)}）"


def _cand_view(cand: dict) -> dict:
    return {"old": cand.get("site_value"), "new": cand.get("codex_value"),
            "unit": cand.get("unit"), "sources": cand.get("evidence"),
            "domains": cand.get("verified_domains")}


# ─────────────────────────────────────────────
# selftest（通信なし）
# ─────────────────────────────────────────────

def selftest() -> int:
    ok = fail = 0

    def eq(got, want, label):
        nonlocal ok, fail
        if got == want:
            ok += 1
        else:
            fail += 1
            print(f"  NG {label}: got={got!r} want={want!r}")

    def C(key, value, strength="verified_policy", scope=None, unit="G",
          ctype="game", ev=2, status="asserted", quote="天井は{v}Gで直撃"):
        doms = ["a.com", "b.com"][:ev]
        return {"claim_key": key, "ceiling_type": ctype, "value": value, "unit": unit,
                "scope": scope, "assertion_status": status,
                "evidence_strength": strength,
                "verified_domains": doms,
                "evidence": [{"source_url": f"https://{d}/x",
                              "raw_quote": quote.format(v=value)} for d in doms],
                "evidence_results": [{"source_url": f"https://{d}/x", "verified": True}
                                     for d in doms]}

    def S(key, value, unit="G", ctype="game"):
        return {"claim_key": key, "ceiling_type": ctype, "value": value, "unit": unit,
                "scope": None, "operator": None, "plus_alpha": None,
                "assertion_status": "asserted"}

    def R(key, verdict, sub=None, detail=""):
        return {"claim_key": key, "verdict": verdict, "sub": sub, "detail": detail,
                "attrs_unverified": []}

    K = "ceiling.normal.game"
    # 1. 2ドメイン裏取り＋値が違う → 修正候補
    c = classify([S(K, 900)], [C(K, 1000)], [R(K, "UNKNOWN", "numeric_divergence")])
    eq(len(c["fix_candidates"]), 1, "修正候補になる")
    eq(c["fix_candidates"][0]["old"] if False else c["fix_candidates"][0]["site_value"], 900, "旧値")
    eq(c["fix_candidates"][0]["codex_value"], 1000, "新値")
    # 2. 1ドメインしか裏取りできない → 要確認
    c = classify([S(K, 900)], [C(K, 1000, strength="verified_single", ev=1)],
                 [R(K, "UNKNOWN", "numeric_divergence")])
    eq(len(c["fix_candidates"]), 0, "1ドメインは修正しない")
    eq("2ドメイン" in c["reviews"][0]["reason"], True, "1ドメインの理由")
    # 3. 裏取りできていない → 要確認
    c = classify([S(K, 900)], [C(K, 1000, strength="cannot_verify", ev=0)],
                 [R(K, "UNKNOWN", "numeric_divergence")])
    eq(len(c["fix_candidates"]), 0, "未検証は修正しない")
    # 4. 条件付き天井（AT間）→ 要確認
    c = classify([S(K, 900)], [C(K, 1000, scope="AT間")],
                 [R(K, "UNKNOWN", "numeric_divergence")])
    eq(len(c["fix_candidates"]), 0, "条件付き天井は修正しない")
    eq("条件付き" in c["reviews"][0]["reason"], True, "条件付きの理由")
    # 5. 単位違い → 修正しない
    c = classify([S(K, 900)], [C(K, 1000, unit="pt")], [R(K, "MISMATCH")])
    eq(len(c["fix_candidates"]), 0, "単位違いは修正しない")
    # 6. 値一致 → 変更なし
    c = classify([S(K, 900)], [C(K, 900)], [R(K, "UNKNOWN", "numeric_alignment")])
    eq(len(c["fix_candidates"]), 0, "一致は修正しない")
    eq(len(c["unchanged"]), 1, "一致は変更なしに入る")
    # 7. 同じ項目にCodex主張が2つ → 一意に決まらないので修正しない
    c = classify([S(K, 900)], [C(K, 1000), C(K, 1200)],
                 [R(K, "UNKNOWN", "numeric_divergence")])
    eq(len(c["fix_candidates"]), 0, "複数主張は修正しない")
    eq("一意に決まらない" in c["reviews"][0]["reason"], True, "複数主張の理由")
    # 8. 構造の食い違い → 要確認（自動修正しない）
    c = classify([S(K, 900)], [], [R(K, "MISSING_IN_CODEX")])
    eq(c["reviews"][0]["kind"], "structure", "構造の食い違いは要確認")
    eq(c["reviews"][0]["auto_fixable"], False, "構造は自動修正しない")
    # 9. MISMATCH（属性まで検証済み）も修正候補になる
    c = classify([S(K, 900)], [C(K, 1000)], [R(K, "MISMATCH")])
    eq(len(c["fix_candidates"]), 1, "MISMATCHも修正候補")

    # 9.1 引用に限定条件（AT間）が入っていれば主天井の置き換えに使わない
    c = classify([S(K, 900)], [C(K, 1000, quote="AT間天井は{v}G")],
                 [R(K, "UNKNOWN", "numeric_divergence")])
    eq(len(c["fix_candidates"]), 0, "AT間の引用は修正しない")
    eq("限定条件" in c["reviews"][0]["reason"], True, "AT間の理由")
    # 9.2 検証済みドメインが実は1件（申告だけverified_policy）→ 修正しない
    fake = C(K, 1000)
    fake["evidence_results"] = [{"source_url": "https://a.com/x", "verified": True},
                                {"source_url": "https://b.com/x", "verified": False}]
    c = classify([S(K, 900)], [fake], [R(K, "UNKNOWN", "numeric_divergence")])
    eq(len(c["fix_candidates"]), 0, "申告だけの2ドメインは信用しない")
    # 9.3 assertion_status が asserted でない → 修正しない
    c = classify([S(K, 900)], [C(K, 1000, status="cannot_verify")],
                 [R(K, "UNKNOWN", "numeric_divergence")])
    eq(len(c["fix_candidates"]), 0, "cannot_verifyは修正しない")
    # 9.4 天井の種類が違う（G数天井 vs スルー天井）→ 修正しない
    c = classify([S(K, 900)], [C(K, 1000, ctype="through")],
                 [R(K, "UNKNOWN", "numeric_divergence")])
    eq(len(c["fix_candidates"]), 0, "天井の種類違いは修正しない")
    # 9.45 Codexのscopeが未知語→修正しない
    r_unknown = R(K, "UNKNOWN", "numeric_divergence")
    r_unknown["attrs_unverified"] = ["scope_unverified"]
    c = classify([S(K, 900)], [C(K, 1000, scope="謎の区間")], [r_unknown])
    eq(len(c["fix_candidates"]), 0, "未知のscopeは修正しない")

    # 9.5 スマスロ機でなければ自動修正しない（同名旧機種と区別できないため）
    c = classify([S(K, 900)], [C(K, 1000)], [R(K, "UNKNOWN", "numeric_divergence")],
                 auto_fix_allowed=False)
    eq(len(c["fix_candidates"]), 0, "非スマスロ機は自動修正しない")
    eq("スマスロ機ではない" in c["reviews"][0]["reason"], True, "非スマスロ機の理由")

    # 9.6 値そのものの妥当性（Codex5巡目 指摘10）
    eq(value_sanity("game", 900, 1000), None, "妥当な値はNone")
    eq(bool(value_sanity("game", 900, -1)), True, "負の値は却下")
    eq(bool(value_sanity("game", 900, float("inf"))), True, "無限大は却下")
    eq(bool(value_sanity("game", 900, float("nan"))), True, "NaNは却下")
    eq(bool(value_sanity("game", 900, 999999999)), True, "桁外れは却下")
    eq(bool(value_sanity("game", 900, 1000.5)), True, "整数でない値は却下")
    eq(bool(value_sanity("game", 900, 50)), True, "範囲外(50G)は却下")
    eq(bool(value_sanity("game", 900, 2900)), True, "変化が大きすぎる（3倍超）は却下")
    eq(value_sanity("cycle", 10, 8), None, "周期の妥当な値")
    eq(bool(value_sanity("cycle", 10, 80)), True, "周期の範囲外は却下")
    c = classify([S(K, 900)], [C(K, 5000)], [R(K, "UNKNOWN", "numeric_divergence")])
    eq(len(c["fix_candidates"]), 0, "桁外れの新値は修正しない")

    # 9.7 サイト側に同じ項目が2つ→修正しない（黙って上書きしない・指摘11）
    c = classify([S(K, 900), S(K, 950)], [C(K, 1000)],
                 [R(K, "UNKNOWN", "numeric_divergence")])
    eq(len(c["fix_candidates"]), 0, "サイト側の重複項目は修正しない")

    # 9.8 どの分類にも入らない判定も要確認へ落ちる（黙って消さない・指摘12）
    c = classify([S(K, 900)], [], [R(K, "UNKNOWN", None, "サイト構造化なし")])
    eq(len(c["reviews"]), 1, "分類外の判定も要確認へ")
    eq(c["reviews"][0]["kind"], "other", "分類外の種別")

    # 9.9 限定条件の検出（表記ゆれ・言い換えを含む・Codex6巡目 指摘2）
    for t in ("設定変更時の天井は800G", "リセット時の天井は800G", "朝一は800Gで天井",
              "AT終了後は800Gで天井", "CZ終了後は800Gで天井", "ボーナス終了後は800G",
              "モードBの天井は800G", "短縮天井は800G", "初回のみ800G",
              "ST間天井は800G", "ART間天井は800G", "初当たり間800G", "REG間800G",
              "ＡＴ間天井は800G", "AT 間天井は800G", "有利区間の天井は800G"):
        eq(bool(conditional_hit(t)), True, f"限定条件を検出: {t}")
    for t in ("AT天井は1000G", "朝イチは800G", "リセ後は800G", "据置時は900G",
              "ボーナス非当選のまま1000G", "AT非経由で1000G", "天国準備モードは800G",
              "通常Bは800G", "2周目のみ天井短縮", "CZ天井は300G", "前兆中は加算されない"):
        eq(bool(conditional_hit(t)), True, f"限定条件を検出(7巡目): {t}")
    for t in ("天井は1000Gで直撃", "通常時の天井は1000G", "天井到達で1000G消化",
              "天井は1000G。天井到達後はATに当選する", "規定ゲーム数消化後は前兆へ",
              "天井当選時は上位ATが確定"):
        eq(bool(conditional_hit(t)), False, f"主天井は素通し: {t}")
    c = classify([S(K, 900)], [C(K, 800, quote="設定変更時の天井は{v}G")],
                 [R(K, "UNKNOWN", "numeric_divergence")])
    eq(len(c["fix_candidates"]), 0, "設定変更時の引用では修正しない")

    # 9.95 検証成功URLと引用の対応が取れないものは修正しない（指摘5）
    broken = C(K, 1000)
    broken["evidence"] = [{"source_url": "https://zzz.com/x", "raw_quote": "天井は1000G"}]
    c = classify([S(K, 900)], [broken], [R(K, "UNKNOWN", "numeric_divergence")])
    eq(len(c["fix_candidates"]), 0, "検証URLと引用が対応しないものは修正しない")
    empty_ev = C(K, 1000)
    empty_ev["evidence"] = []
    c = classify([S(K, 900)], [empty_ev], [R(K, "UNKNOWN", "numeric_divergence")])
    eq(len(c["fix_candidates"]), 0, "引用が無いものは修正しない")

    # 9.99 主天井であることの肯定確認（Codex8巡目 指摘1）
    for q, v, ct in (("通常時を最大1268G+α消化で天井到達", 1268, "game"),
                     ("・通常時最大 1268G+α で天井到達", 1268, "game"),
                     ("通常時は最大10周期到達でAT当選。", 10, "cycle"),
                     ("通常時を最大967G＋α消化でボーナスに当選。", 967, "game"),
                     ("天井は1268G＋α消化でバトルボーナスに当選", 1268, "game")):
        eq(main_ceiling_quote(q, v, ceiling_type=ct), None, f"主天井と確認できる: {q[:20]}")
    for q, v, ct in (("設定変更時は天井G数が短縮され、800G+αで天井到達となる。", 800, "game"),
                     ("設定6のみ天井は800G", 800, "game"), ("AT終了時は800G", 800, "game"),
                     ("CZ失敗時の天井は800G", 800, "game"), ("AT天井は1000G", 1000, "game"),
                     # 値を単位とセットで探す（Codex9巡目 指摘1: 800枚を天井値としない）
                     ("最大800枚獲得。設定変更時の天井は800G", 800, "game"),
                     # 値の直後の注記（指摘2）
                     ("天井は800G（設定変更時のみ）", 800, "game"),
                     # 残余を0文字必須にした分（指摘3）
                     ("初回は800G", 800, "game"), ("裏天井は800G", 800, "game"),
                     ("第2天井は800G", 800, "game"), ("非通常時は800G", 800, "game"),
                     ("天井Aは800G", 800, "game"),
                     ("ST単発終了4連続後にST当選でスルー回数天井に到達し、", 4, "through"),
                     # ★同じ文の中に条件があれば、後半に主天井の記述があっても安全側で落とす★
                     #   （値は同じなので実害は無く、他の出典で拾える。Codex9巡目 指摘8への判断）
                     ("設定変更時は800G、通常時の天井も800G", 800, "game")):
        eq(bool(main_ceiling_quote(q, v, ceiling_type=ct)), True,
           f"主天井と確認できない: {q[:20]}")
    eq(bool(main_ceiling_quote("天井はG数管理", 900)), True, "値が無い引用は不合格")
    # 機種名が引用に入っていても落とさない（指摘9）
    eq(main_ceiling_quote("「Lバンドリ！」の通常時天井は1000G", 1000,
                          ["バンドリ"], "game"), None, "引用中の機種名は除去して判定")
    c = classify([S(K, 900)], [C(K, 800, quote="設定6のみ天井は{v}G")],
                 [R(K, "UNKNOWN", "numeric_divergence")])
    eq(len(c["fix_candidates"]), 0, "設定限定の引用では修正しない")
    c = classify([S(K, 900)], [C(K, 1000, quote="通常時を最大{v}G消化で天井到達")],
                 [R(K, "UNKNOWN", "numeric_divergence")])
    eq(len(c["fix_candidates"]), 1, "主天井の引用なら修正候補になる")

    def _scan(page_text, quote, new=800, old=900):
        verify_claims._page_cache.clear()
        verify_claims._page_cache["https://t.test/x"] = verify_claims.Page(
            page_text, "【スマスロ テスト】天井", "https://t.test/x")
        cand = {"site_value": old, "codex_value": new, "site_ceiling_type": "game",
                "ceiling_type": "game", "evidence": ["https://t.test/x"], "cores": []}
        try:
            return contradiction_scan(cand, None, [("https://t.test/x", quote)])
        finally:
            verify_claims._page_cache.clear()

    # 9.993 Codex11巡目の残り（単位の厳格化・見出し連結・＋α整合）
    eq(bool(main_ceiling_quote("最大4回継続でAT当選", 4, ceiling_type="through")), True,
       "スルーは裸の「回」を単位と認めない")
    eq(main_ceiling_quote("スルー回数天井は最大6スルー到達で発動する。", 6,
                          ceiling_type="through"), None, "「スルー」付きなら認める")
    eq(main_ceiling_quote("スマスロ北斗の拳 天井・設定判別 通常時は最大800Gで天井到達。",
                          800, ["北斗の拳"], "game"), None,
       "ページ見出しが本文と連結されても落とさない")
    eq(bool(main_ceiling_quote("スマスロ北斗の拳 天井・設定判別 設定変更時は最大800Gで天井到達。",
                               800, ["北斗の拳"], "game")), True,
       "見出しがあっても条件は検出する")
    _acand = {"claim_key": "ceiling.normal.game", "site_value": 1268,
              "codex_value": 1300, "site_ceiling_type": "game"}
    eq(bool(alpha_mismatch("hokuto", {**_acand, "plus_alpha": False})), True,
       "本文が＋α表記なのに出典が＋α無しなら修正しない")
    eq(alpha_mismatch("hokuto", {**_acand, "plus_alpha": True}), None, "＋αが一致すれば可")
    eq(alpha_mismatch("hokuto", {**_acand, "plus_alpha": None}), None,
       "出典が言及しなければ判定しない")

    # 9.990 Codex14巡目
    for q, v, ct in (("最大800G：通常時の天井は1000G", 800, "game"),
                     ("最大800G／通常時の天井は1000G", 800, "game"),
                     ("通常時の天井は800G ※設定6のみ", 800, "game"),
                     ("通常時の天井は800G、設定6のみ", 800, "game"),
                     ("通常時の天井は800G／仮天井", 800, "game"),
                     ("最大800G消化でCZに突入", 800, "game"),
                     ("通常時、最大800G消化でCZに突入", 800, "game"),
                     ("最大800G消化で上位ATへ移行", 800, "game"),
                     ("最大10周期消化でCZに突入", 10, "cycle"),
                     ("通常時の天井は1000Gが目安", 1000, "game")):
        eq(bool(main_ceiling_quote(q, v, ceiling_type=ct)), True,
           f"14巡目の攻撃を拒否: {q[:22]}")
    for q, v, ct in (("通常時の天井は、最大1000Gです", 1000, "game"),
                     ("スルー回数天井は最大6スルー到達で発動する。", 6, "through"),
                     ("通常時を最大967G＋α消化でボーナスに当選。", 967, "game")):
        eq(main_ceiling_quote(q, v, ceiling_type=ct), None, f"正しい引用は通す: {q[:22]}")
    eq(operator_in_quote("通常時は最大1000Gで天井到達", 1000, "game"), "max", "operatorの抽出:max")
    eq(operator_in_quote("通常時の天井は1000G", 1000, "game"), "exact", "operatorの抽出:exact")

    # 9.991 Codex13巡目の残り（複数文判定の精度・URL1対1・見出し除去）
    for q in ("「通常時の天井は1000G。」", "通常時の天井は1000G!!"):
        eq(main_ceiling_quote(q, 1000, ceiling_type="game"), None,
           f"閉じ括弧・感嘆符を2文目と誤認しない: {q}")
    dup = C(K, 1000, quote="通常時の天井は{v}G")
    dup["evidence"] = [{"source_url": "https://a.com/x", "raw_quote": "通常時の天井は1000G"},
                       {"source_url": "https://a.com/x", "raw_quote": "通常時の天井は1000G"}]
    dup["evidence_results"] = [{"source_url": "https://a.com/x", "verified": True},
                               {"source_url": "https://b.com/x", "verified": True}]
    c = classify([S(K, 900)], [dup], [R(K, "UNKNOWN", "numeric_divergence")])
    eq(len(c["fix_candidates"]), 0, "同一URLの引用で2ドメインを水増しできない")

    # 9.992 Codex13巡目: 天井語の借用・接尾の注記・非断定表現
    for q, v in (("最大800G継続、通常時の天井は1000G", 800),
                 ("最大800G消化でAT終了、通常時の天井は1000G", 800),
                 ("最大800G継続、規定ゲーム数の天井は1000G", 800),
                 ("天井は800G（設定6のみ）", 800), ("天井は800G（偶数設定のみ）", 800),
                 ("天井は800G（CZ失敗時のみ）", 800), ("天井は800G（2回目のみ）", 800),
                 ("天井は800G（仮天井）", 800), ("通常時の天井は約1000G", 1000)):
        eq(bool(main_ceiling_quote(q, v, ceiling_type="game")), True,
           f"天井語の借用・注記・非断定を拒否: {q[:22]}")
    for q, v, ct in (("通常時を最大967G＋α消化でボーナスに当選。", 967, "game"),
                     ("天井は1268G＋α消化でバトルボーナスに当選", 1268, "game"),
                     ("通常時は最大10周期到達でAT当選。", 10, "cycle")):
        eq(main_ceiling_quote(q, v, ceiling_type=ct), None,
           f"正しい引用は通す: {q[:22]}")

    # 9.994 前後の文・括弧内に条件を追い出す迂回（Codex11巡目 指摘1・2）
    for pt, q in (("設定変更時だけ適用される短縮天井です。天井は800GでAT当選。", "天井は800GでAT当選"),
                  ("天井は800GでAT当選。※この天井は設定変更時のみ適用されます。", "天井は800GでAT当選"),
                  ("天井は800G（適用条件は次項を参照。設定変更時のみ）", "天井は800G")):
        eq(bool(_scan(pt, q)), True, f"前後の文/括弧内の条件を検出: {pt[:20]}")
    for pt, q in (("本機の解説。通常時は最大800Gで天井到達となります。狙い目は600Gから。",
                   "通常時は最大800Gで天井到達"),
                  ("通常時の天井は800Gです。設定変更時は450Gに短縮されます。",
                   "通常時の天井は800Gです")):
        eq(_scan(pt, q), None, f"別の値が書かれた隣接文では落とさない: {pt[:20]}")
    eq(bool(main_ceiling_quote("最大800G継続、AT当選率は1/300。", 800, ceiling_type="game")),
       True, "当選率は天井語として数えない（Codex11巡目 指摘3）")

    # 9.996 天井の記述であることの肯定確認（Codex10巡目 指摘6）
    for q, v, ct in (("最大100pt獲得", 100, "point"), ("最大4回転継続", 4, "through"),
                     ("最大800G継続", 800, "game"), ("最大10周期遊技", 10, "cycle")):
        eq(bool(main_ceiling_quote(q, v, ceiling_type=ct)), True,
           f"天井の記述でないものは不合格: {q}")
    eq(main_ceiling_quote("通常時を最大967G＋α消化でボーナスに当選。", 967,
                          ceiling_type="game"), None, "天井の語が無くても消化・当選なら可")
    # 注記の中に句点がある場合も条件を取り逃がさない（指摘5）
    eq(bool(main_ceiling_quote(
        "天井は800G（詳しい適用条件については別項の注意事項を必ず確認してください。設定変更時のみ）",
        800, ceiling_type="game")), True, "括弧内の句点をまたいで条件を検出")
    eq(bool(main_ceiling_quote(
        "天井は1268G＋α消化でバトルボーナスに当選。天井ATは継続率優遇。本前兆中に天井到達",
        1268, ceiling_type="game")), True,
       "複数文の引用は拒否（2文目に条件を隠せるため・Codex12巡目 指摘1）")
    eq(bool(main_ceiling_quote("天井は800G。※設定変更時のみ適用。", 800,
                               ceiling_type="game")), True, "2文目に条件を置く迂回を拒否")
    eq(bool(main_ceiling_quote("最大800G継続。通常時の天井は1000G。", 800,
                               ceiling_type="game")), True, "別の文の天井語を借りる迂回を拒否")
    for q in ("高設定では天井は800G", "モード天井は800G", "ゾーン天井は800G"):
        eq(bool(main_ceiling_quote(q, 800, ceiling_type="game")), True,
           f"見出し語に化けた条件を拒否: {q}")

    # 9.997 天井種別ごとの語彙（実データ較正・周期/スルー機の主天井表現）
    eq(main_ceiling_quote("最大10周期到達で周期天井に到達し、STに当選する。", 10,
                          ceiling_type="cycle"), None, "周期機の「周期天井」は条件語でない")
    eq(main_ceiling_quote("スルー回数天井は最大6スルー到達で発動する。", 6,
                          ceiling_type="through"), None, "スルー機の「スルー回数天井」も同様")
    eq(bool(main_ceiling_quote("最大10周期到達で周期天井に到達する。", 10,
                               ceiling_type="game")), True,
       "G数天井の機種で周期天井が出たら別系統として拒否")
    eq(bool(main_ceiling_quote("AT間を最大2500G消化でCZに突入する。", 2500,
                               ceiling_type="game")), True, "AT間は種別に関係なく拒否")
    eq(bool(main_ceiling_quote("ボーナス間967G＋αが通常天井です。", 967,
                               ceiling_type="game")), True, "ボーナス間も拒否")

    # 9.995 ページ本文側の文脈検査（Codex10巡目: 引用の外に条件を追い出す攻撃）
    for pt, q in (("天井は800G（設定変更時のみ）", "天井は800G"),
                  ("【設定変更時】天井は800GでAT当選", "天井は800GでAT当選"),
                  ("設定変更時・天井は800G", "天井は800G"),
                  ("「設定変更時」天井は800G", "天井は800G"),
                  ("設定変更時の天井" + chr(10) + "天井は800GでAT当選", "天井は800GでAT当選"),
                  ("設定6限定：天井は800G", "天井は800G"),
                  ("偶数設定のみ：天井は800G", "天井は800G"),
                  ("高設定域では：天井は800G", "天井は800G")):
        eq(bool(_scan(pt, q)), True, f"ページ側の条件を検出: {pt[:18]}")
    for pt, q in (("本機の解説です。通常時は最大800Gで天井到達となります。",
                   "通常時は最大800Gで天井到達"),
                  ("天井は800G＋α消化でATに当選。", "天井は800G＋α消化でATに当選")):
        eq(_scan(pt, q), None, f"正常なページは通す: {pt[:18]}")
    eq(bool(_scan("天井は800G", "天井は900G")), True, "引用がページに無ければ不合格")
    eq(bool(_scan("天井は800G。別枠でも天井は800G", "天井は800G")), True,
       "引用がページ内に複数あれば不合格")
    eq(bool(_scan("通常時は最大800Gで天井到達。旧解析では900Gだった", "通常時は最大800Gで天井到達")),
       True, "旧値もページにあれば不合格")

    # 10. 選定は評価日が古い順・preview機種は対象外
    ms = [{"slug": "a"}, {"slug": "b"}, {"slug": "c", "status": "preview"}]
    st = {"codex_audit": {"last_audited": {"a": "2026-07-20", "b": "2026-07-01"}}}
    eq(select_slugs(ms, st, 3), ["b", "a"], "古い順・previewは除外")
    eq(select_slugs(ms, {}, 1), ["a"], "未点検が最優先")

    print(f"codex_audit selftest: {ok}/{ok + fail}")
    return 0 if fail == 0 else 1


# ─────────────────────────────────────────────
# 本体
# ─────────────────────────────────────────────

def run(slugs_override, n_machines: int, apply_mode: bool) -> int:
    started = now()
    deadline = started + datetime.timedelta(seconds=TASK_BUDGET_SEC)
    run_id = uuid.uuid4().hex[:8]
    log(f"=== codex_audit 開始 run_id={run_id} 期限={deadline:%H:%M} "
        f"モード={'本番' if apply_mode else 'dry-run'} ===")
    ctx = lock_acquire() if apply_mode else None
    if apply_mode and not ctx:
        log("=== codex_audit 中止（ロックが取れない＝他タスク実行中） STATUS=SKIPPED_LOCKED ===")
        write_marker("SKIPPED_LOCKED", run_id, started, now(), [], 0, 0)
        return 0
    # ★開始時に作業ツリーの汚れを確認（Codex6巡目 指摘1）★
    #   既に未検証の変更があるなら、それを巻き込んで公開しないよう自動修正を止める。
    dirty = [ln for ln in (_sh(["git", "status", "--porcelain"]).stdout or "").splitlines()
             if ln.strip() and not ln.startswith("??")]
    if dirty and apply_mode:
        log(f"⚠ 作業ツリーに未コミットの変更あり→自動修正は行わず点検のみ: {dirty[:5]}")
        add_issue("site", "environment", "[codex-audit] 作業ツリーが汚れていて自動修正を中止",
                  " / ".join(dirty)[:1000])
    dirty_tree = bool(dirty)
    machines = json.loads((BASE / "assets" / "data" / "machines.json").read_text(encoding="utf-8"))
    by = {m["slug"]: m for m in machines}
    state = load_json(STATE_PATH, {}) or {}
    slugs = slugs_override or select_slugs(machines, state, n_machines)
    log(f"対象: {slugs}")

    allowed = list(shadow_gold.ALLOWED_DOMAINS)
    fixes_done, all_reviews, all_fixes, errors = 0, [], [], []
    timeout_hit = False
    for slug in slugs:
        if now() >= deadline:
            timeout_hit = True
            rest = slugs[slugs.index(slug):]
            log(f"⏰ 時間切れ: {rest} は実施しない（現状維持）")
            if apply_mode:
                add_issue("site", "environment", "[codex-audit] 時間切れで未点検の機種がある",
                          f"run_id={run_id} / 未点検={rest}")
            break
        m = by.get(slug)
        if not m:
            log(f"⚠ {slug}: machines.json に無い")
            errors.append((slug, "ERR_UNKNOWN_SLUG"))
            if apply_mode:
                add_issue(slug, "other", "[codex-audit] 対象slugがmachines.jsonに無い",
                          f"指定slug={slug}")
            continue
        log(f"── {slug} 点検開始")
        heartbeat(ctx, f"{slug} 点検開始")
        r = audit_machine(m, machines, run_id, deadline)
        heartbeat(ctx, f"{slug} Codex応答受領")
        if r["error"]:
            errors.append((slug, r["error"]))
            log(f"── {slug} エラー: {r['error']}")
            if apply_mode:
                add_issue(slug, "environment", f"[codex-audit] 点検できなかった（{r['error']}）",
                          f"run_id={run_id} / error={r['error']} / detail={str(r.get('detail'))[:300]}")
            continue
        if r.get("empty_comparison") and apply_mode:
            add_issue(slug, "other", "[codex-audit] 比較結果が空（点検が成立していない）",
                      f"run_id={run_id} / site_claims={len(r.get('site_claims') or [])} "
                      f"/ codex_claims={len(r.get('codex_claims') or [])}")
        cls = r["classified"]
        log(f"── {slug} 一致{len(cls['unchanged'])}件 / 修正候補{len(cls['fix_candidates'])}件 "
            f"/ 要確認{len(cls['reviews'])}件")

        for cand in cls["fix_candidates"]:
            if fixes_done >= MAX_FIXES_PER_RUN:
                cls["reviews"].append({**cand, "kind": "value", "auto_fixable": False,
                                       "reason": f"本日の自動修正上限（{MAX_FIXES_PER_RUN}件）に到達"})
                continue
            fr = apply_one(slug, cand, allowed, apply_mode and not dirty_tree)
            fr["slug"] = slug
            if fr["applied"]:
                fixes_done += 1
                all_fixes.append(fr)
                log(f"   ✅ 修正: {slug} {fr['field']} {fr['old']}→{fr['new']} "
                    f"（{fr['struct_path']} ＋本文{len(fr['prose_edits'])}箇所）")
            else:
                log(f"   ⏸ 修正見送り: {slug} {fr['field']} 理由={fr['reason']}")
                cls["reviews"].append({**cand, "kind": "value", "auto_fixable": False,
                                       "reason": fr["reason"]})

        for rv in cls["reviews"]:
            rv["slug"] = slug
            all_reviews.append(rv)

        # 状態更新（1機種終わるごと＝途中で落ちても進捗が残る）
        state.setdefault("codex_audit", {}).setdefault("last_audited", {})[slug] = \
            f"{now():%Y-%m-%d}"
        if apply_mode and fencing_ok(ctx):
            save_json_atomic(STATE_PATH, state)
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        save_json_atomic(AUDIT_DIR / f"{now():%Y%m%d%H%M%S}_{slug}_{run_id}.json", r)

    # 修正を公開（再ビルド→検査→コミット→push）。検査が通らなければ取り消す
    published, pub_note = False, "修正なし"
    if all_fixes and apply_mode:
        try:
            published, pub_note = publish(all_fixes, ctx)
        except Exception as e:
            _restore(all_fixes)
            published, pub_note = False, f"公開中に例外（修正は取り消した）: {type(e).__name__}: {e}"
        log(f"公開: {'✅' if published else '❌'} {pub_note}")
        if not published:
            for sl in sorted({f["slug"] for f in all_fixes}):
                add_issue(sl, "other", "[codex-audit] 自動修正の公開に失敗", pub_note[:1500])

    # 要確認は台帳へ（自動修正できなかったもの全て）★無人タスクはcloseしない★
    if apply_mode:
        for rv in all_reviews:
            kind = "structural" if rv.get("kind") == "structure" else "external_value"
            title = (f"{rv.get('claim_key')} 要確認: "
                     f"サイト{rv.get('site_value')} / Codex{rv.get('codex_value')}"
                     if rv.get("kind") == "value" else
                     f"{rv.get('claim_key')} の構造が食い違う")
            add_issue(rv["slug"], kind, title[:120],
                      json.dumps(rv, ensure_ascii=False)[:1500])

    summary = (f"修正{len(all_fixes)}件 / 要確認{len(all_reviews)}件 / エラー{len(errors)}件"
               + (" / 時間切れあり" if timeout_hit else ""))
    log(f"=== codex_audit 完了 {summary} ===")

    if apply_mode:
        status = ("PARTIAL" if (errors or timeout_hit or ISSUE_FAILURES
                                or (all_fixes and not published))
                  else ("COMPLETED" if slugs else "COMPLETED_NO_CHANGE"))
        write_marker(status, run_id, started, now(), slugs, len(slugs) - len(errors),
                     len(errors), note=pub_note)
        # ★「静かなのが正常」＝修正した/台帳に載せた/異常が出た時だけメール★
        if all_fixes or all_reviews or errors or timeout_hit:
            icon = "🔴" if errors or ISSUE_FAILURES or (all_fixes and not published) else (
                "🟡" if all_reviews or timeout_hit else "🟢")
            body = [f"対象: {', '.join(slugs)}", summary, f"公開: {pub_note}", ""]
            for f in all_fixes:
                body.append(f"✅ 修正 {f['slug']} {f['field']}: {f['old']} → {f['new']}"
                            f"（本文{len(f['prose_edits'])}箇所・出典 {', '.join(f.get('domains') or [])}）")
            for rv in all_reviews[:20]:
                body.append(f"⏸ 要確認 {rv['slug']} {rv.get('claim_key')}: "
                            f"{rv.get('reason') or rv.get('detail')}")
            for s, e in errors:
                body.append(f"❌ エラー {s}: {e}")
            if ISSUE_FAILURES:
                body.append(f"🔴 台帳登録に失敗（見落としの恐れ）: {ISSUE_FAILURES[:10]}")
            notify(f"{icon} codex-audit: {summary}", "\n".join(body))
        if ctx:
            _lock("release", "--ctx", ctx)
    return 0 if not errors else 1


COMPLETION_MARKER = DOC / "codex_audit" / "codex_audit_last_run.json"


def write_marker(status: str, run_id: str, started, ended, selected,
                 success: int, errors: int, note: str = "") -> None:
    """完走マーカー（task-watchdogが「起動したが黙って死んだ」を検知するための足跡）"""
    save_json_atomic(COMPLETION_MARKER, {
        "status": status, "run_id": run_id, "started_at": iso(started),
        "ended_at": iso(ended), "target_count": len(selected),
        "success_count": success, "error_count": errors,
        "selected": list(selected), "note": note[:300]})


def main() -> int:
    ap = argparse.ArgumentParser(description="既存記事の誤りをCodex＋関所で自動修正")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--slugs")
    ap.add_argument("--max-machines", type=int, default=DEFAULT_MACHINES)
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    slugs = a.slugs.split(",") if a.slugs else None
    if a.dry_run:
        return run(slugs, a.max_machines, apply_mode=False)
    if a.run:
        return run(slugs, a.max_machines, apply_mode=True)
    ap.error("--run / --dry-run / --selftest のいずれかを指定")
    return 2


if __name__ == "__main__":
    sys.exit(main())

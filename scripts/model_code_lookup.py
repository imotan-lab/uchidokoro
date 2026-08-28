"""model_code_lookup.py — 名鑑から機種の「型式名」を引く。

★なぜ要るか（2026-07-31）★
  メーカー公式ページには型式名が載っていないことが多い（登場年月だけ）。
  一方で名鑑（P-WORLD・DMMぱちタウン）には**導入前から**型式名が載る。

  以前は「型式は導入前には無い」と思い込んでいたが、それは
  **誤った機種名で検索していたため見つからなかっただけ**だった。
  実際、Lすーぱぁびん娘（2026-08-03導入）は導入前に
  P-WORLD・DMM・ゼンリンの3件に「Lびん娘NY1」として載っていた。

★引くときの名前はメーカー公式のものを使う★
  まとめサイトの名前で引くと取り違える（「ビンゴライブ」という
  実在しない名前で探して空振りした実例がある）。
  メーカー公式の一覧から取った正式名称だけを使う。

★同じ機種だと認めるための条件★
  名前が一致しただけでは足りない。続編・パチンコ版・L版と無印がある。
  そこで**名前の芯が完全に一致**することを求め、さらに
  **独立2つの名鑑で型式名が一致**して初めて採用する。

使い方:
    python scripts/model_code_lookup.py --url https://www.p-world.co.jp/machine/database/10496 \\
                                        --name "Lすーぱぁびん娘"
    python scripts/model_code_lookup.py --selftest
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

import claim_identity as _ci          # noqa: E402
import new_machine_watch as _w        # noqa: E402
import user_area as _ua              # noqa: E402

# 型式名が書かれている形（★見出しの次の行に値がある形もある★）
_LABELS = ("型式名", "型式")
# 型式名として認める形。★これ以外は採らない★（許可した形だけ通す）
#   英数字・記号・かな・漢字が混じる短い1行。文や説明を拾わない。
_CODE_OK = re.compile(r"^[0-9A-Za-zぁ-んァ-ヶ一-龥ー･・／/＋+\-−–—．.　 ]{2,40}$")
# ★型式名だけの許可文字★（2026-08-06・台帳#238）
#   ★共用しない理由★: _CODE_OK は spec_lookup の「文字の値」（純増など）でも
#   使っている。型式名のために文字を足すと、関係のない収集まで一緒にゆるむ。
#   実データで確かめた追加文字だけを、ここに1つずつ足していく。
#     「!」… Lやじきた道中記参る!BG（P-WORLD 10489 / DMMぱちタウン 5027 の2社一致）
#   ※「:」「~」「☆」も実在するとCodexは言うが**当方未確認**なので足さない。
#     足すときは公的な検定一覧などで1件ずつ確かめること。
#   ★短すぎる形は採らない★（「L!」のような1文字＋記号を型式名にしない）
#     ただし最小長は3。**4は「型式名は4文字以上」という未確認の思い込み**で、
#     取りこぼす側の危険があるとCodex122回目に指摘されたので下げた
#     （L! と !! は先読みの条件だけで拒否できる）。
_MODEL_CODE_OK = re.compile(
    r"^(?=.*[0-9A-Za-z])(?=.*[0-9A-Za-zぁ-んァ-ヶ一-龥]{2})"
    r"[0-9A-Za-zぁ-んァ-ヶ一-龥ー･・／/＋+\-−–—．.!　 ]{3,40}$")
# ★型式名に混じっていたら採らない語★（見出しや注記を型式名にしない）
_MODEL_CODE_NG_WORDS = ("予定", "導入", "発売", "登場", "新台", "解析", "初打ち")
# 明らかに型式名ではない語（見出しの取り違え防止）
_CODE_NG = ("記載なし", "不明", "未定", "調査中")


class LookupError_(RuntimeError):
    pass


# ★型式名の候補として拒む語★（2026-08-02・Codex24回目を再現して直した）
#   「型式名：」の次の行が別の見出しだと、その見出しを型式名として採っていた。
_LABEL_LIKE = ("メーカー名", "機種名", "型式名", "型式", "メーカー", "導入開始",
               "導入日", "登場日", "検定日", "タイプ", "仕様", "備考")


def extract_model_code(html: str):
    """名鑑ページの本文から型式名を1つ取り出す。決まらなければ None と理由。"""
    lines = _w._visible_text(html).splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        for lab in _LABELS:
            if not s.startswith(lab):
                continue
            # ★見出しの直後は区切りか行末に限る★（2026-08-02・Codex24回目）
            #   「型式名について」の「について」を値として採っていた。
            after = s[len(lab):]
            if after and after[0] not in "：: 　\t":
                continue          # 「型式名○○」は見出しではない
            # 「型式名：Lびん娘NY1」の形
            rest = after.lstrip("：: 　").strip()
            cand = rest
            if not cand and i + 1 < len(lines):
                # 「型式名 :」の次の行に値がある形（P-WORLD）
                cand = lines[i + 1].strip()
            if not cand:
                continue
            if cand in _CODE_NG:
                return None, "MODEL_CODE_NOT_STATED"
            # ★別の見出しを値にしない★（次の行が「メーカー名」等だった・Codex24回目）
            if cand in _LABEL_LIKE:
                continue
            norm = unicodedata.normalize("NFKC", cand)
            # ★英数字を1文字も含まない語は採らない★（2026-08-02・Codex24回目）
            #   このタスクが扱う新台（L/S世代）の型式名は必ず英数字を含む
            #   （Lびん娘NY1 等）。日本語だけの語は見出し・説明の可能性が高い。
            #   本物を取りこぼしても「まだ載っていない」扱い＝安全側に落ちる。
            if not re.search(r"[0-9A-Za-z]", norm):
                continue
            if any(w in cand for w in _MODEL_CODE_NG_WORDS):
                continue                  # ★注記が混じった行は型式名ではない★
            if not _MODEL_CODE_OK.match(cand):
                continue          # 説明文などを拾ってしまった。次の候補へ
            return norm, "OK"
    return None, "MODEL_CODE_NOT_FOUND"


# ★機種名のすぐ後ろに来てよい語★（題の飾り）
#   ここに無い語が名前の直後に来たら、**別の機種**とみなす。
#   「モンキーターン V」の "V"、「すーぱぁびん娘 SP」の "SP" を止めるため。
#   ★「パチンコ」を入れない★（2026-08-01・Codex23回目を再現して直した）
#     飾りではなく**別の種目の印**。入れていたせいで
#     「北斗の拳 パチンコ 新台」のようなパチンコ版のページを本人にできた。
_DECOR = {
    "新台", "天井", "解析", "スペック", "設定", "判別", "設定判別", "設定差",
    "設定示唆", "やめどき", "ヤメ時", "やめ時", "狙い目", "初打ち", "打ち方",
    "機械割", "導入日", "設置店", "掲示板", "有利区間", "期待値", "評価",
    "感想", "演出", "攻略", "実践", "動画", "画像", "一覧", "情報", "恩恵",
    "ボーナス", "フリーズ", "ちょんぼりすた", "pworld", "ぱちタウン", "dmm",
    "dmmぱちタウン", "パチスロ解析", "解析情報", "スロット新台",
    # ★「解析まとめ」の「まとめ」が無く、ちょんぼりすたの実在題
    #   「…やめどき 解析まとめ | ちょんぼりすた」が材料のstrict化（55回目）で
    #   全滅していた（2026-08-03・Codex57回目の指摘を再現する過程で発見）
    "まとめ", "解析まとめ",
    "機種情報", "新台情報", "ゾーン", "製品情報",
    "公式サイト", "製品サイト", "特設サイト", "機種サイト", "はこちら",
    # ★ベルコの定型尾部（実在形・丸ごと1語で許可）★（2026-08-02・Codex49回目）
    #   「パチンコ」を一般の飾りへ戻すのは危険（パチンコ版の印・23回目）なので、
    #   この定型句だけを完全一致で許す。
    "パチンコ・パチスロメーカー",
}
_DECOR_CORES = {_ci.normalize_core(w) or w for w in _DECOR}

# ★題を区切る記号★（サイト側の飾りを切り離す）
#   ★「・」「-」は入れない★＝機種名そのものに使われる
#   （「すーぱぁびん娘・極」を割ると、別機種を本人にしてしまう）
#   ★「、」「,」も入れない★（2026-08-13・実データで発生）
#     「Lパチスロ 彼女、お借りします」が「彼女」と「お借りします」に割れ、
#     題に機種名が丸ごと載っている3つの名鑑すべてが NAME_CORE_MISMATCH で
#     落ちていた（ちょんぼりすた・DMM・ナナプレスで実測）。
#     読点は原作タイトルにそのまま入る（「彼女、お借りします」）。
#     飾りが読点で並ぶ題は、区切らなくても飾り語として扱われるので困らない。
_TITLE_SEPS = "|｜(（)）[［]］【】/／<＞>＜"


def title_parts(title: str) -> list:
    """題を区切って、機種名らしいかたまりに分ける。"""
    out, buf = [], []
    for ch in title or "":
        if ch in _TITLE_SEPS:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return [x.strip() for x in out if x.strip()]


# ★メーカー名（名鑑の題の括弧に入る）★（2026-08-02・Codex26回目）
#   名簿から読む。読めなければ空＝厳しい側（メーカー括弧つきの題を通さない）。
_MAKER_CORES = None


def _maker_name_cores() -> set:
    global _MAKER_CORES
    if _MAKER_CORES is None:
        try:
            got = json.load(open(_w.CATALOGS, encoding="utf-8"))
            _MAKER_CORES = {
                _ci.normalize_core(str(c.get("name") or ""))
                for c in (got.get("catalogs") or {}).values()
                if isinstance(c, dict)} - {""}
        except Exception:                 # noqa: BLE001
            _MAKER_CORES = set()
    return _MAKER_CORES


# ★明確な派生の印★（2026-08-02・Codex33回目）
#   材料の照合は別名を許す必要があり全語検査はできないが、
#   これらの印が名前の後ろに出たページを本人として扱うのは危険すぎる。
#   （許可制の本線＝strict_all_tail は型式・公式の照合に掛けてある。
#     これは材料経路のための拒否リスト＝防御の厚み。網羅はできない）
_DERIV_MARKS = {"sp", "ex", "dx", "v", "z", "zero", "改", "極", "新章",
                "新装版", "廻", "頂"}


def _has_deriv_mark(cores: list) -> bool:
    return any(c in _DERIV_MARKS or (c.isdigit() and len(c) <= 2)
               for c in cores)


# ★規格の印（L/S）を読む語★（2026-08-01・Codex23回目を再現して直した）
#   スマスロ系の言い換えはLと同じ規格。パチスロ/スロットはどちらとも言えない。
_GEN_L_WORDS = ("スマートパチスロ", "スマートスロット", "スマスロ", "メダルレス")
_GEN_NEUTRAL_WORDS = ("パチスロ", "ぱちすろ", "スロット")
_GEN_PREFIX_RE = re.compile(r"^[ls](?![a-z])")


def _gen_mark(s: str) -> str:
    """名前の頭に付く規格の印を返す（'L'／'S'／''＝書かれていない）。

    ★なぜ要るか（2026-08-01・Codex23回目。自分で再現した）★
      芯の比較は表記ゆれを吸収するためにL/Sを落とす。そのせいで
      「S北斗の拳」のページを「L北斗の拳」の本人にできた。
      L版とS版は規格も中身も別の機種なので、印どうしが食い違ったら弾く。
      印が書かれていない題は従来どおり通す（芯とその後ろの検査は別にある）。
    """
    t = unicodedata.normalize("NFKC", s or "").lower().lstrip(" 　")
    while t:
        hit = False
        for w in _GEN_L_WORDS:
            if t.startswith(unicodedata.normalize("NFKC", w).lower()):
                return "L"
        for w in _GEN_NEUTRAL_WORDS:
            wl = unicodedata.normalize("NFKC", w).lower()
            if t.startswith(wl):
                t = t[len(wl):].lstrip(" 　")
                hit = True
                break
        if not hit:
            m = _GEN_PREFIX_RE.match(t)
            return m.group(0).upper() if m else ""
    return ""


_MODEL_GEN_RE = re.compile(r"^([ls])b?(?![a-z])")


def model_gen_mark(code: str) -> str:
    """★型式名の規格印（L/S）★（2026-08-02・Codex54回目）

    題名用の _gen_mark を型式名に流用すると、実在のBT型式
    「LB/タコスロBD」（スマスロ タコスロ・ユニバーサルブロス・
    P-WORLDニュースで確認）の L の直後が英字のため印なし扱いになり、
    2名鑑一致でも型式が捨てられた。LB/SB はL系/S系のBT型式として読む。
    それ以外の英字連なり（LBX… 等）は従来どおり印なし＝人の確認へ。
    """
    t = unicodedata.normalize("NFKC", str(code or "")).strip().lower()
    m = _MODEL_GEN_RE.match(t)
    return m.group(1).upper() if m else ""


def page_is_machine(html: str, official_name: str,
                    extra_tail_ok: set | None = None,
                    strict_all_tail: bool = False):
    """★その名鑑ページが本当にその機種か★

    ★ただの前方一致をやめた★（2026-07-31・Codex22回目。実際に再現した）
      以前は「題の芯が指定名の芯で**始まる**こと」だけを見て、
      数字と続編記号しか弾いていなかった。そのため
        「すーぱぁびん娘新章」「すーぱぁびん娘SP」「すーぱぁびん娘・極」
      がどれも本人として通り、**別機種の公式URLと指定名を組み合わせて**
      記事を作れる穴になっていた。

    いまは題を「区切り記号」と「空白」で語に分け、
      ①続いた語をつないだものが、指定名の芯と**丸ごと同じ**
      ②その次の語が、飾り（新台・天井・解析…）か、区切りか、題の終わり
    の両方を求める。②が無いと「モンキーターン V」を
    「モンキーターン」として通してしまう。

    実データで通ることを確かめた形:
      「L青春ブタ野郎は…(スマスロ 青ブタ) パチスロ新台 … | P-WORLD」
      「スマスロ 甲鉄城のカバネリ 海門(うなと)決戦 パチスロ新台 …」← 名前に括弧
      「スマスロ 真打吉宗 スロット 新台 … | ちょんぼりすた …」
      「スマスロ東京喰種 スロット 新台 … 東京グール | ちょんぼりすた …」← 別名つき
    """
    title = _w.page_title(html)
    if not title:
        return False, "PAGE_TITLE_MISSING"
    core = _ci.normalize_core(official_name)
    if not core:
        return False, "OFFICIAL_NAME_HAS_NO_CORE"
    want_gen = _gen_mark(official_name)
    # ★★落ちた理由を混ぜない★★（2026-08-22・Codexの指摘／実害あり）
    #   ★直す前★＝4つの別々の落ち方が全部 GEN_MARK_CONFLICT を名乗っていた。
    #   実際 pw_10510（タコスロ）は「規格印の食い違い」と報告されていたが、
    #   本当の理由は★題の後ろの「ボーナストリガー」を飾りとして分解できない★
    #   ことだった（L/Sの比較は成功していた）。
    #   ＝**丸1日、原因を取り違えて調べた**（台帳#453の記述も誤っていた）。
    #   ★採否は1文字も変えない★＝名前を分けるだけ。異常の正体を隠さないため。
    gen_conflict = False        # 規格の印（L/S）が食い違う＝別機種
    tail_conflict = False       # 名前の後ろの語を飾りとして説明できない
    deriv_conflict = False      # 派生機の印（SP等）が後ろにある
    # ★題そのものも候補に入れる★（機種名の中に括弧が入ることがある）
    #   「甲鉄城のカバネリ 海門(うなと)決戦」は、区切ると名前が割れてしまう。
    # ★断片にしても「元の題でその前に何があったか」を持ち歩く★
    #   （2026-08-02・Codex25回目を再現して直した）
    #   断片ごとに独立して見ていたので、「別機種 | L北斗の拳」の後ろの断片が
    #   まっさらな前置として通っていた。前置の検査は元の題の全部に対して行う。
    # ★直後の断片も持ち歩く★（2026-08-02・Codex26回目を再現して直した）
    #   「Lすーぱぁびん娘（SP）」のように、派生機の印が括弧で
    #   区切られると誰も見ていなかった（25回目の「前」と逆向きの穴）。
    # ★機種見出し（h1）を先に見る★（2026-08-02・Codex54回目）
    #   P-WORLDのSEO用の題は括弧に略称・読み仮名を詰める
    #   （「…6(Lスト6 SF6)」「…参る!(やじきた参 やじきた3)」＝実在）。
    #   部分列で確かめられない略称のたびに実在の票を失っていた。
    #   h1は正式名そのもの（実ページ3件で確認）。h1にも題と**同じ**
    #   厳格検査を通す（DMMのh1はSEO文言込みなので、緩めない）。
    #   前後の検査は各ソースの中で閉じる（題の飾りをh1に持ち込まない）。
    # ★h1を使うのは「可視のh1が1本だけ」の時に限る★（2026-08-02・Codex55回目）
    #   複数のh1があると、別機種の題のページでも節のh1の1本が一致すれば
    #   本人にできた。実在の名鑑（P-WORLD・DMM）はh1が1本（実ページで確認）。
    #   2本以上ならh1は根拠にせず、題だけで判定する（安全側）。
    #   CSSクラスで隠したh1は静的には見抜けないが、この規則により
    #   「h1を足す」細工はh1経路を無効にする方向にしか働かない。
    _h1s = _w._visible_h1s(html)
    if len(_h1s) != 1:
        _h1s = []
    cands = []
    for _src in _h1s + [title]:
        _segs = title_parts(_src)
        cands.append((_src, [], []))
        _seen = []
        for _ix, s in enumerate(_segs):
            # ★後ろは「直後の1断片」ではなく残り全部★（2026-08-02・Codex28回目）
            # ★ただし断片の区切りを保つ★（2026-08-02・Codex32回目）
            #   平らにつなぐと、別の断片の「パチスロ」が
            #   「(SP)」の括弧の規格語として数えられてしまった。
            cands.append((s, list(_seen), _segs[_ix + 1:]))
            _seen.extend(_ci.normalize_core(w) for w in s.split())
    for seg, before, after in cands:
        raw = seg.split()
        words = [_ci.normalize_core(w) for w in raw]
        for i in range(len(words)):
            joined = ""
            for j in range(i, len(words)):
                joined += words[j]
                # ★世代表記の同値化を主名称にも★（2026-08-02・Codex50回目）
                #   公式「…2」↔名鑑「…II」（SAO2の実在形）が一致しなかった。
                if joined != core and                         _ci.canon_num_tail(joined) != _ci.canon_num_tail(core):
                    continue
                # ★次の語を見る★（飾りか、そこで終わりならOK）
                k = j + 1
                while k < len(words) and words[k] == "":
                    k += 1               # 販売区分語などは芯が空になる
                # ★飾りを連結した語も飾りとして見る★（2026-08-13・実データ）
                #   「新台解析」「やめどきまとめ」のように、サイトが飾りを
                #   つないで1語で書くことがある。後ろの断片を見る _after_ok は
                #   _decor_compound で同じ扱いをしていたのに、ここだけ
                #   単語の完全一致だったため、**題に機種名が丸ごと載っている
                #   ちょんぼりすたのページが落ちて**いた（実測）。
                # ★ただし厳格な経路だけに限る★（2026-08-13・依頼173のP1）
                #   ゆるい経路は「残り全部」を見ないので、
                #   「L対象機 新台解析 L別機種」の後ろの別機種を見落とす
                #   （Codexの反例。実際に (True,'OK') になることを確認した）。
                #   材料集め（天井・AT・CZ・スペック）と型式照合はすべて
                #   strict_all_tail=True なので、これで目的は満たせる。
                if (k >= len(words) or words[k] in _DECOR_CORES
                        or (strict_all_tail and _decor_compound(raw[k]))):
                    # ★名前より前の語も全部見る★（2026-08-02・Codex24〜25回目）
                    #   「P 北斗の拳」「パチンコ 北斗の拳」「別機種 | L北斗の拳」の
                    #   ように**前に別の意味の語がある題**が通っていた。
                    #   前に許すのは、芯が空になる語（規格・販売区分）と飾りだけ。
                    #   ★前は断片の中だけでなく、元の題のそこまで全部★
                    if any(w != "" and w not in _DECOR_CORES
                           for w in before + words[:i]):
                        continue          # 名前の前に知らない語＝別の話かもしれない
                    # ★規格の印（L/S）が食い違ったら別機種★（2026-08-01・Codex23回目）
                    #   芯の比較は印を落とすので、S版のページがL版の本人になれた。
                    #   印は、名前に融合した頭（「S北斗の拳」）と、
                    #   直前に独立して置かれた語（「L 東京喰種」「スマスロ 甲鉄城…」）
                    #   の両方から読む。
                    pre = i
                    while pre > 0 and words[pre - 1] == "":
                        pre -= 1          # 芯が空＝規格・販売区分の語
                    got_gen = _gen_mark(" ".join(raw[pre:i + 1]))
                    if want_gen and got_gen and got_gen != want_gen:
                        gen_conflict = True
                        continue          # 印が違う＝別機種。他の候補を探す
                    # ★直後の断片も確かめる★（2026-08-02・Codex26回目）
                    #   許すのは①規格・販売区分（芯が空）②飾り③メーカー名
                    #   ④本人の略称＝「(規格 略称)」の形だけ
                    # ★公式の照合では extra_tail_ok（社名・銘柄）を許す★
                    #   （2026-08-02・Codex27回目。検査を丸ごと外すと
                    #     派生機の公式URL「…（SP）|BELLCO」が通ってしまった）
                    if not all(_after_ok(a, core, official_name, extra_tail_ok)
                               for a in after):
                        tail_conflict = True
                        continue
                    # ★同じ断片の残りの語も全部見る★（2026-08-02・Codex32回目）
                    #   「名前 新台 SP」は最初の飾り（新台）で検査を
                    #   打ち切っていたので、後ろの派生印SPを見ていなかった。
                    # ★strict_all_tail の経路だけ★＝型式の名鑑照合と公式照合。
                    #   材料の解析サイトの題は別名を後ろに書くのが通例
                    #   （「…解析 東京グール | ちょんぼりすた」）で、
                    #   そこまで縛ると実在の出典を失う。
                    if strict_all_tail and not _after_ok(
                            " ".join(raw[j + 1:]), core,
                            official_name, extra_tail_ok):
                        tail_conflict = True
                        continue
                    # ★材料の照合でも、明確な派生の印だけは拒む★
                    #   （2026-08-02・Codex33回目。独立2つの解析サイトが
                    #     「名前 新台 SP」でSP版の値を載せていると、
                    #     2票一致も規格印も通ってしまうため）
                    if not strict_all_tail and _has_deriv_mark(
                            [_ci.normalize_core(w) for w in raw[j + 1:]]):
                        deriv_conflict = True
                        continue
                    return True, "OK"
    # ★強い証拠から順に名乗る★（同時に立ちうるため）
    if gen_conflict:
        return False, "GEN_MARK_CONFLICT"
    if deriv_conflict:
        return False, "DERIV_MARK_CONFLICT"
    if tail_conflict:
        return False, "TAIL_CONFLICT"
    return False, "NAME_CORE_MISMATCH"


_SEP_LOW = "・、,/／　 -‐―–—!！。"
_COMPOUND_TOKENS = None


def _compound_tokens() -> list:
    """飾り語・販売区分語（小文字NFKC・長い順）。連結語の分解に使う。"""
    global _COMPOUND_TOKENS
    if _COMPOUND_TOKENS is None:
        toks = set()
        for w in list(_DECOR) + ["スマートパチスロ", "スマートスロット",
                                 "スマスロ", "メダルレス", "パチスロ",
                                 "ぱちスロ", "スロット"]:
            toks.add(unicodedata.normalize("NFKC", w).lower())
        _COMPOUND_TOKENS = sorted(toks, key=len, reverse=True)
    return _COMPOUND_TOKENS


def _decor_compound(word: str) -> bool:
    """★飾り語・販売区分語だけの連結語か★（2026-08-02・とんスキ実データ）

    DMMの実在の題「(新台スマスロ)パチスロ|設定判別・天井・ゾーン・解析…」の
    「新台スマスロ」「設定判別・天井・…」を、語単位の照合では読めなかった。
    許可済みの語だけで最後まで分解できる時に限り通す（残りが出たら不合格）。
    """
    t = unicodedata.normalize("NFKC", word or "").lower()
    i = 0
    while i < len(t):
        if t[i] in _SEP_LOW:
            i += 1
            continue
        for tok in _compound_tokens():
            if t.startswith(tok, i):
                i += len(tok)
                break
        else:
            return False
    return True


def _after_ok(after_seg: str, core: str, official_name: str,
              extra: set | None = None) -> bool:
    """名前の後ろの断片（括弧の中身など）が、本人のページとして自然か。

    ★1つの断片＝1つの括弧★（2026-08-02・Codex32回目）
      複数の断片をつないで渡さないこと。つなぐと、別の断片の規格語が
      「(SP)」の括弧の規格語として数えられてしまう。

    extra: 呼び出し元が追加で許す語（メーカー公式の照合では社名・銘柄）。
    """
    if not after_seg:
        return True
    words = after_seg.split()
    cores = [_ci.normalize_core(w) for w in words]
    # ★略称を許すのは「(規格 略称)」の形だけ★（2026-08-02・Codex31回目）
    #   文字の集合で見ると、名前に偶然SとPが入る機種では派生印SPまで通った。
    #   ①**同じ断片（括弧）**に規格・販売区分の語（芯が空）があること
    #   ②語が名前の芯の**順番どおりの部分列**であること（青ブタ⊂青春ブタ野郎…）
    #   の両方を求める。
    has_platform = any(c == "" for c in cores)
    want_gen = _gen_mark(official_name)

    def _subseq(small: str, big: str) -> bool:
        it = iter(big)
        return all(ch in it for ch in small)

    def _word_status(w: str, c: str) -> str:
        """'ok'＝通す / 'abbrev'＝本人の略称として通す / 'ng'＝通せない。"""
        # ★メーカー語は正規化後の完全一致だけ★（2026-08-02・Codex28回目）
        #   部分一致だと「SPBELLCO」のような合成語まで許してしまう。
        #   「株式会社サミー」「株式会社北電子」は株式会社を外してから比べる。
        c2 = c.replace("株式会社", "")
        if c == "" or c in _DECOR_CORES:
            return "ok"
        if extra is not None:
            if c in extra or c2 in extra:
                return "ok"               # 期待するメーカーの社名・銘柄
            # ★期待メーカーの照合中は、他社の社名を許さない★
            #   （2026-08-02・Codex53回目。「L試験機(サミー)」が平和の照合を
            #     通り、メーカー欄が無いページでは後段の照合も無いため、
            #     別メーカーの同名機を2名鑑一致で採用できた）
            if c in _maker_name_cores() or c2 in _maker_name_cores():
                return "ng"
        elif c in _maker_name_cores() or c2 in _maker_name_cores():
            return "ok"
        # ★明確な派生印は、略称より先に拒む★（2026-08-02・Codex38回目）
        #   名前の芯に偶然 s,p が順に並ぶ機種だと、「(スマスロ SP)」の
        #   SP が部分列として略称扱いになっていた。
        if c in _DERIV_MARKS or (c.isdigit() and len(c) <= 2):
            return "ng"
        if has_platform and len(c) >= 2 and _subseq(c, core):
            return "abbrev"               # (規格 略称) の形の略称
        # ★機種と同じ規格印つきの略称★（2026-08-02・とんスキ実データ）
        #   P-WORLDの実在の題「(Lとんスキ)」。L自体が規格の注記なので、
        #   印が機種と同じで、残りが名前の順番どおりの部分列なら通す。
        if want_gen and _gen_mark(w) == want_gen \
                and len(c) >= 2 and _subseq(c, core):
            return "abbrev"
        # ★正式名から導ける別名★（2026-08-02・Codex40回目。P-WORLD実データ）
        #   「マイジャグラーVI(マイジャグラー6 マイジャグ6)」のように、
        #   規格語もL/S印も無い別名括弧が現に在る。
        #   頭の文字が同じ・順番どおりの部分列・世代表記（VI↔6）は同値、
        #   の3条件がそろった語だけを別名として通す
        #   （派生印の拒否が先に効くので SP・改 等はここへ来ない）。
        if len(c) >= 2 and c[:1] == core[:1] \
                and _subseq(_canon_gen_num(c), _canon_gen_num(core)):
            return "abbrev"
        # ★飾り語・販売区分語だけの連結語★（新台スマスロ・設定判別・天井・…）
        if _decor_compound(w):
            return "ok"
        return "ng"

    # ★読み仮名を略称から推定しない★（2026-08-02・Codex54回目で撤去）
    #   52回目に「(スマスロ 青ブタ あおぶた)」の読み仮名を条件つきで
    #   通したが、「くろぶた」「にせぶた」も条件を満たすと反証された。
    #   文字列から読みの正しさは確定できない。実在形はh1（機種見出し＝
    #   正式名そのもの）の同定で通るので、題の読み別名は根拠にしない。
    return all(_word_status(w, c) != "ng"
               for w, c in zip(words, cores))


# ★世代表記の同値化★（VI↔6。共通部品 claim_identity.canon_num_tail へ委譲）
def _canon_gen_num(t: str) -> str:
    return _ci.canon_num_tail(t)


def maker_brand_cores(maker_id: str) -> set:
    """★そのメーカーの社名・銘柄として許す芯★（公式ページの題の尾部用）

    名簿の日本語名・メーカーID・公式の場所（link_prefix）のホスト名の
    部品から作る。読めなければ空＝厳しい側。
    """
    out = set()
    try:
        got = json.load(open(_w.CATALOGS, encoding="utf-8"))
        conf = (got.get("catalogs") or {}).get(maker_id) or {}
    except Exception:                     # noqa: BLE001
        return out
    toks = [str(conf.get("name") or ""), str(maker_id or "")]
    # ★名鑑での別名（パオン・ディーピー等）も、その社の銘柄として許す★
    #   （2026-08-02・Codex47回目。メーカー欄にしか効いておらず、
    #     題の括弧（パオン・ディーピー）で正しい票を失っていた）
    toks += [str(x) for x in (conf.get("directory_names") or [])]
    for tok in toks:
        c = _ci.normalize_core(tok)
        if c:
            out.add(c)
    host = re.sub(r"^[a-z]+://", "", str(conf.get("link_prefix") or ""))
    host = host.split("/", 1)[0]
    for part in re.split(r"[.\-]", host):
        if len(part) >= 3 and part not in ("www", "com", "net"):
            c = _ci.normalize_core(part)
            if len(c) >= 3:
                out.add(c)
    return out


_INTRO_LABELS = ("導入開始日", "導入開始", "導入予定日", "導入日")


def release_near_identity(text: str) -> str:
    """★型式名の近くにある導入開始日★（対象機の基本情報ブロックの値）

    （2026-08-02・Codex48回目。DMMの実ページで確認＝本体の導入開始日は
      型式名の数行後・「シリーズ機種」の日付は1000行以上離れている）
    見つからなければ空文字（呼び出し元がページ全体の単独月へ退避）。
    """
    lines = text.splitlines()
    ti = next((i for i, l in enumerate(lines)
               if l.strip().startswith("型式名")), None)
    if ti is None:
        return ""
    for i in range(ti, min(ti + 25, len(lines))):
        s2 = lines[i].strip()
        for lab in _INTRO_LABELS:
            if not s2.startswith(lab):
                continue
            rest = s2[len(lab):].lstrip("：: 　").strip()
            cand = rest or (lines[i + 1].strip() if i + 1 < len(lines) else "")
            m = _w._RELEASE_RE.search(cand)
            if m and 1 <= int(m.group(2)) <= 12:
                return f"{m.group(1)}-{int(m.group(2)):02d}"
    return ""


def extract_maker_name(html: str) -> str:
    """名鑑ページの「メーカー名」欄の値（無ければ空）。"""
    lines = _w._visible_text(html).splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        for lab in ("メーカー名", "メーカー"):
            if not s.startswith(lab):
                continue
            after = s[len(lab):]
            if after and after[0] not in "：: 　\t":
                continue
            v = after.lstrip("：: 　").strip()
            if not v and i + 1 < len(lines):
                v = lines[i + 1].strip()
            if v and v not in _LABEL_LIKE:
                return v
    return ""


def material_page_identity_ok(page, official_name: str, *,
                              url: str = "", expected_maker: str = "",
                              extra_tail_ok: set | None = None,
                              grant=None, dmm_identity: dict | None = None):
    """★材料に使ってよいページかを見る、唯一の場所★（2026-08-17・台帳#390）

    返すもの: (通してよいか, 理由)

    ★なぜ1か所にまとめるか★（Codex依頼233の指摘7）
      基本スペック・天井・CZ・AT仕様の読取器が**それぞれ**同定をやり直して
      いたので、材料集めの段階でページを通しても**値を読む段階でまた落ちた**。
      例外の扱いを4か所に写すと、必ずどこかがずれる。

    ★★page は FetchedPage（取ってきた本文と指紋を持つ器）★★
      （2026-08-17・台帳#393／Codex依頼237の診断）
      ★以前は生のHTMLとURLを別々に受け取っていた★ので、
      「確かめた本文」と「あとで読む本文」が同じであることを
      **誰も保証していなかった**。同じ型の穴が5回続いた原因がここ。

    ★grant＝2AIが「材料に使ってよい」と決めた★本文の指紋★の集まり★
      ★URLではなく指紋で照合する★＝URLの書き方の違い（末尾の / 等）や
        転送で結び直しが必要になる余地を、そもそも作らない。
      ★弱い側で救えるのは題の不一致（NAME_CORE_MISMATCH）だけ★＝
        別機種・規格違い・題が読めない等の落ち方は、控えがあっても通さない
        （Codexの指摘3＝複数の失敗理由を弱い方へ落とさない）
      ★通すときも、メーカー欄が今もDMMと合っているかをその場で見る★
        （Codexの指摘2＝弱いプロファイルでメーカー関門を迂回させない）
    """
    html = getattr(page, "cleaned_html", page)   # 器でも生HTMLでも受ける
    # ★★器で渡されたら、指紋をその場で数え直す★★
    #   （2026-08-17・Codex依頼238の厚み）器は書き換えられるので、
    #   作った時の指紋を信じない。★いま持っている本文から数える★
    _sha = ""
    if not isinstance(page, str):
        import hashlib
        _sha = hashlib.sha256(str(html or "").encode("utf-8")).hexdigest()
        # ★★取りに行った先と着いた先が違うページは、通常でも使わない★★
        #   （2026-08-17・Codex依頼238のP1）
        #   ★穴だったところ★＝控えで救う側は転送を拒否していたのに、
        #   **厳格な同定に通る「普通のページ」は、ここで即座に通していた**。
        #   別ページへ転送されていても、その本文が同じ機種に見えれば
        #   材料として採用され、公開まで到達し得た（例外側は直したが、
        #   通常側が隣で残っていた）。
        import maker_identity_cache as _micr
        _req = getattr(page, "requested_url", "")
        _fin = getattr(page, "final_url", "")
        if _req and _micr.url_key(_req) != _micr.url_key(_fin):
            return False, "REDIRECTED"
    # ★★DMMの機種ページは、DMM自身の決まりで確かめる★★（2026-08-22・台帳#453）
    #   ★ここにも要る理由★＝lookup（型式の票）だけ直しても、
    #   材料を読む側はこの関所を通るので、**同じ理由でもう一度落ちる**。
    #   実測（2026-08-22）＝lookup を直した直後の走行で、
    #   「基本スペック／天井／ATの仕様／CZ」が全部 TAIL_CONFLICT で落ちた。
    #   ★片方だけ直した★＝CLAUDE.md の監査42・43と同じ轍。
    if dmm_identity:
        ok, why = dmm_identity_ok(html, dmm_identity)
        if ok:
            return True, "OK"
        return False, why
    ok, why = page_is_machine(html, official_name, strict_all_tail=True,
                              extra_tail_ok=extra_tail_ok)
    if ok:
        return True, "OK"
    # ★★許可証で救える落ち方★★（2026-08-26。TAIL_CONFLICT を足した）
    #   ★片方だけ直すと、材料を読む側で同じ理由でもう一度落ちる★
    #   （CLAUDE.md に前例あり。実際にまた踏んだ）
    #   ★別機種・規格違い・派生機は今までどおり救わない★
    # ★救える落ち方は控えの側が正本★（2026-08-29・台帳#498）
    import maker_identity_cache as _mic_r
    if not _mic_r.rescuable_reason(why):
        return False, why
    if not grant:
        return False, why
    # ★★指紋で照合する★★（URLではない）
    #   器で渡されていないと指紋が無い＝確かめようがないので通さない。
    if not _sha:
        return False, "GRANT_NO_PAGE_FINGERPRINT"
    if _sha not in set(grant):
        return False, "GRANT_CONTENT_MISMATCH"
    # ★このページを使うと決めた前提（メーカー欄が合う）が今も成り立つか★
    mk = extract_maker_name(html)
    if not mk:
        return False, "GRANT_MAKER_UNREADABLE"
    if expected_maker:
        owners = _maker_core_owners(
            _ci.normalize_core(mk).replace("株式会社", ""))
        if expected_maker not in owners:
            return False, "GRANT_MAKER_MISMATCH"
    return True, "OK_BY_GRANT"


def _maker_core_owners(core_text: str) -> set:
    """その文字列が名簿のどの社を指すか（名前・IDの芯の**包含**で見る）。

    ★包含にする理由★ 名鑑は「コナミアミューズメント(メーカー公式サイト)」の
    ように飾りを足す。逆に名簿に無い表記（コナミ…はKPEの名鑑表記）は
    どの社も指さない＝判定不能として扱う。
    """
    hits = []
    try:
        got = json.load(open(_w.CATALOGS, encoding="utf-8"))
        for mid, conf in (got.get("catalogs") or {}).items():
            # ★巡回しない社（list_url なし）も名簿として見る★（2026-08-13）
            #   名簿の役割は「巡回する先」から「メーカーの同定」へ広がった。
            #   巡回対象の絞り込みは呼ぶ側（is_catalog＋status=ACTIVE）が持つ。
            #   外していたため、京楽・サンスリー等の名鑑ページが
            #   「解決できません」で材料から全部落ちていた（実ログで発生）。
            if not isinstance(conf, dict):
                continue
            # ★名鑑での別名（directory_names）も見る★（2026-08-02・Codex44回目）
            #   KPE↔コナミアミューズメント、ユニバーサル↔ミズホ/メーシー/アクロス等。
            #   別名が解決できるほど「別の社」の検知が広がる（誤拒否は増えない）。
            toks = [str(conf.get("name") or ""), str(mid)]
            toks += [str(x) for x in (conf.get("directory_names") or [])]
            best = 0
            for tok in toks:
                c = _ci.normalize_core(tok)
                if c and c in core_text:
                    best = max(best, len(c))
            if best:
                hits.append((best, mid))
    except Exception:                     # noqa: BLE001
        return set()
    # ★★最も具体的に当たった社だけを採る★★（2026-08-13）
    #   素直な包含だと「オリンピア」（平和の別名）が
    #   「オリンピアエステート」に当たり、**別の社の機種**として
    #   P-WORLD自身のページまで弾いていた（実ログで発生）。
    #   当たった芯のうち最長のものだけを残す＝より具体的な名前が勝つ。
    #   同じ長さで複数社が並ぶのは名簿の矛盾（下の自動試験が見張る）。
    if not hits:
        return set()
    top = max(n for n, _ in hits)
    best = {mid for n, mid in hits if n == top}
    # ★同じ長さで複数社が並んだら「決められない」とする★（2026-08-13・依頼172）
    #   どちらでも通ってしまうと、メーカー違いの材料を採る恐れがある。
    #   空集合＝DIRECTORY_MAKER_UNRESOLVED で止まる（fail-closed）。
    return best if len(best) == 1 else set()


def _sl_votes(hosts) -> int:
    """★独立した票の数★（同じ会社の別ホストは1票・共同制作の組もまとめる）"""
    import source_lineage as _sl2
    keys = set()
    for h in hosts or ():
        try:
            keys.add(_sl2.vote_key_of_url("https://" + str(h).lstrip("/")))
        except _sl2.LineageError:
            continue          # ★登録されていないサイトは票に数えない★
    return _sl2.independent(keys)


def _relation_group(maker_id: str) -> str:
    """★2AIへ回す価値のある関係★（2026-08-14・依頼189）

    ★これは「同じ会社」という許可ではない★
      公式は「グループ会社」と書いているだけで、
      **全機種でメーカー名を入れ替えてよいとは書いていない**。
      1回の判断ミスが以後すべての機種で関門を無効にするので、
      ここは「即座に別物と決めつけず、機種ごとに2AIへ聞く」ための印にする。
    """
    if not maker_id:
        return ""
    try:
        got = json.load(open(_w.CATALOGS, encoding="utf-8"))
        conf = (got.get("catalogs") or {}).get(maker_id)
        if isinstance(conf, dict):
            # ★古い名前は読まずに止める★（2026-08-14・依頼190のP2）
            #   `maker_identity_group` は「全機種で自動的に通す許可」だった。
            #   互換で読み続けると、名簿に旧名を足すだけで**廃止した挙動が
            #   静かに戻る**。名前だけ変えても意味が戻せるなら直っていない。
            if "maker_identity_group" in conf:
                raise LookupError_(
                    f"名簿に古い項目 maker_identity_group があります"
                    f"（{maker_id}）／★これは廃止しました★＝"
                    "全機種で自動的に通す許可でした。"
                    "2AIへ回す印にするなら maker_relation_group に書き換えます")
            return str(conf.get("maker_relation_group") or "")
    except LookupError_:
        raise
    except Exception:                     # noqa: BLE001
        return ""
    return ""


def _related(expected: str, owners: set) -> bool:
    """期待する社と、名鑑が指した社が「関係のありそうな」間柄か。"""
    g = _relation_group(expected)
    return bool(g) and any(_relation_group(o) == g for o in owners)


# ★旧 `_identity_group` / `_same_identity_group` は削除した★
#   （2026-08-14・依頼190のP2）どこからも呼ばれていないのに
#   **旧項目を読むコードだけが残っていた**。読む場所が残っていると、
#   あとで誰かが繋ぎ直せてしまう。



def dmm_identity_ok(html: str, ident: dict) -> tuple:
    """★DMMの機種ページを、DMM自身の決まりで確かめる★（2026-08-22・台帳#453）

    ★なぜ別の契約にするか（Codexの設計レビュー）★
      DMMの機種ページには**専用の同定経路がすでにある**
      （機種ID・canonical・転送先・機種名・種別・メーカー・導入年月）。
      そこを通ったページに、さらに**汎用のSEO題検査**を重ねると、
      ★DMMが題に何を書くかという、こちらに関係のない事情で落ちる★。

      実際 pw_10510（スマスロ タコスロ）は、題の後ろの「ボーナストリガー」を
      飾りとして分解できないという理由だけで材料からも票からも外れ、
      ★5日間ずっと記事にできなかった★。
      「ボーナストリガー」を飾りの辞書に足す直し方は採らない
      （未知のSEO語が出るたびに辞書が増える構造が残るため）。

    ★「DMMのページなら無条件で通す」ではない★
      呼ぶ側が**確かめ済みの束**（機種ID・機種名・メーカー・導入日）を渡し、
      いま材料として取ってきた**その本文**に対して同じ束を確かめ直す。
      ＝URL文字列だけを信用しない（転送・canonicalの差し替え・
        カレンダーの誤リンク・中身の入れ替えを見る）。

    渡す束: {"machine_id": "5049", "name": "スマスロ タコスロ",
             "maker_names": ["ユニバーサルブロス", …], "release": "2026-09-07"}
    返り値: (ok, 理由)
    """
    import dmm_machine as _dm
    want_id = str((ident or {}).get("machine_id") or "")
    if not want_id:
        return False, "DMM_IDENTITY_NOT_GIVEN"
    try:
        got = _dm.parse(html, want_id)
    except Exception as e:                # noqa: BLE001
        return False, f"DMM_PAGE_UNREADABLE:{type(e).__name__}"
    # ★機種名★（DMM自身の決まりで見る＝SEOの飾りは name_matches が扱う）
    want_name = str((ident or {}).get("name") or "")
    if not want_name:
        return False, "DMM_IDENTITY_NOT_GIVEN"
    ok, why = _dm.name_matches(got.get("heading") or "", want_name)
    if not ok:
        return False, f"DMM_NAME_MISMATCH:{str(why)[:60]}"
    # ★★束が欠けていたら答えない★★（2026-08-22・Codexの指摘で直した）
    #   ★直す前★＝maker_names が空なら照合を飛ばし、release が空でも飛ばし、
    #   **本文から導入日を読めなくても飛ばして**最後に成功していた。
    #   ＝「渡されなかったものは確かめない」＝関門として成り立っていない。
    #   呼ぶ側（_ident_for）が完全な束しか作らないので実害は出ていなかったが、
    #   ★この関数自身が束を確かめる★契約でなければ、隣を変えた日に破れる。
    # ★メーカー★（読めなかったことを、確かめたことにしない）
    want_makers = [str(x) for x in ((ident or {}).get("maker_names") or []) if x]
    if not want_makers:
        return False, "DMM_IDENTITY_NOT_GIVEN:maker_names"
    page_maker = str(got.get("maker") or "")
    if not page_maker:
        return False, "DMM_MAKER_UNREADABLE"
    import dmm_discover as _dd
    if _dd._norm(page_maker) not in {_dd._norm(x) for x in want_makers}:
        return False, f"DMM_MAKER_MISMATCH:{page_maker[:30]}"
    # ★導入日★（★機種ページが月までのときは月で比べる★＝日は勝手に決めない）
    want_rel = str((ident or {}).get("release") or "")
    if not want_rel:
        return False, "DMM_IDENTITY_NOT_GIVEN:release"
    page_rel = str(got.get("release_date") or "")
    if not page_rel:
        # ★読めなかったことを、確かめたことにしない★
        return False, "DMM_RELEASE_UNREADABLE"
    if want_rel[:7] != page_rel[:7]:
        return False, f"DMM_RELEASE_MISMATCH:{page_rel[:10]}"
    return True, "OK_DMM_IDENTITY"


def lookup(url: str, official_name: str, expected_maker: str = "",
           dmm_identity: dict | None = None) -> dict:
    """1つの名鑑ページから型式名を引く。★機種が違えば採らない★

    ★dmm_identity を渡すと、DMMの機種ページは DMM自身の決まりで確かめる★
      （2026-08-22・台帳#453）。渡さなければ今までどおり汎用の題検査。
    """
    # ★identity_ok＝このページが本人だと確かめられたか★（2026-08-02・Codex56回目）
    #   型式照合で不合格（他社名の題等）になったページが、理由の文字列が
    #   DIRECTORY_MAKER_* でないため材料収集に復活していた。
    #   呼び出し元は identity_ok が偽のページを材料からも外す。
    out = {"url": url, "official_name": official_name,
           "model_code": None, "reason": "", "identity_ok": False}
    try:
        # ★用途を名乗ってから取りに行く★（2026-08-16・依頼218）
        with _w.fetching("claim_material"):
            html = _w._get(url)
        # ★取ってきた直後に、投稿欄・AI欄を箱ごと落とす★（2026-08-14・台帳#345）
        #   ここを通さないと、**表を生のHTMLから読む処理**に読者の書き込みが入る。
        #   落としきれないときは例外＝そのページは使わない（fail-closed）。
        html = _ua.clean_html(html, url)
    except Exception as e:
        out["reason"] = f"取得できません: {e}"
        return out
    if dmm_identity:
        # ★発行元ごとの同定方式★（機種別の例外でも「2段目」でもない）
        ok, why = dmm_identity_ok(html, dmm_identity)
    else:
        ok, why = page_is_machine(
            html, official_name, strict_all_tail=True,
            extra_tail_ok=maker_brand_cores(expected_maker) if expected_maker
            else None)
    if not ok:
        out["reason"] = why
        # ★2AIへ回す価値があるかの印★（2026-08-17・台帳#390／Codexの③）
        #   ★これは「本人だ」という判断ではない★＝機械がしてよいのは
        #   「完全一致の文字列があるか見る」までで、そこから本人性を
        #   結論づけるのは二段目の意味判断（Codex依頼233の4）。
        #   ★2AIへ回してよい落ち方だけ、候補として印を付ける★（2026-08-26）
        #     ・NAME_CORE_MISMATCH … 題が略称
        #     ・TAIL_CONFLICT     … 題の後ろの飾りを分解できない
        #   ★別機種・規格違い・派生機は今までどおり回さない★
        #   （GEN_MARK_CONFLICT / DERIV_MARK_CONFLICT は候補にしない）
        if why in ("NAME_CORE_MISMATCH", "TAIL_CONFLICT") and official_name:
            body = " ".join(_w._visible_text(html).split())
            out["name_in_body"] = str(official_name).strip() in body
            # ★メーカー欄は「見えた事実」として返す★
            #   （2026-08-17・Codex依頼234の指摘2）
            #   題の不一致で先に戻っていたので、メーカー欄を一度も読まず、
            #   救う側（title_name_core_mismatch）が**必ず空の表記で控えを引き**、
            #   永久に一致しなかった。★状態（maker_check）は作らない★＝
            #   4つの判定を増やさない。ここは事実の観測だけ。
            out["observed_maker"] = extract_maker_name(html)
        return out
    out["identity_ok"] = True
    # ★同定に通ったページの導入年月を控えとして返す★（2026-08-02・Codex47回目）
    #   公式が年月を画像でしか出さない機種のため。使ってよいのは
    #   「型式が一致した同じ2名鑑」の月が一致した時だけ（呼び出し元が判定）。
    # ★対象機の基本情報ブロックの導入開始日だけを読む★（2026-08-02・Codex48回目）
    #   DMMは同じページに「シリーズ機種」の導入開始日も並ぶ（実ページで確認）。
    #   ページ全体で読むと複数月＝Noneになり、山佐系の控えが消えていた。
    out["release_hint"] = release_near_identity(_w._visible_text(html))
    if not out["release_hint"]:
        _rm = _w.release_month(_w._visible_text(html))
        out["release_hint"] = str((_rm or {}).get("value") or "")
    # ★名鑑のメーカー欄が「名簿にある別の社」を指していたら採らない★
    #   （2026-08-02・Codex40回目。同名機の別メーカー票を防ぐ）
    #   ★表記ゆれ・別名（KPE↔コナミアミューズメント等）は拒否しない★
    #     ＝欄の値が名簿のどの社か判定できた時だけ、期待する社と比べる。
    #     素直な一致要求だと実在のとんスキ（メーカー欄=コナミアミューズメント・
    #     名簿=KPE）をまた弾いてしまう（実ページで確認済み）。
    if expected_maker:
        mk = extract_maker_name(html)
        if not mk:
            # ★メーカー欄が読めないのも「どの社か分からない」★
            #   （2026-08-17・依頼226のCodex指摘3）
            #   前は4つの判定を一度も通らず、そのまま材料にも型式の票にも
            #   使えていた（隠れた5つ目の状態になっていた）。
            out["maker_check"] = {"state": "UNKNOWN", "seen": "",
                                  "expected": expected_maker, "owners": []}
            out["reason"] = ("DIRECTORY_MAKER_UNRESOLVED（名鑑のメーカー欄を"
                             "読めません）")
            return out
        if mk:
            owners = _maker_core_owners(
                _ci.normalize_core(mk).replace("株式会社", ""))
            # ★★見えた事実を返す・使ってよいかは呼ぶ側が決める★★
            #   （2026-08-14・依頼189。Codexの設計）
            #   MATCH    … 名簿で一致（そのまま使える）
            #   UNKNOWN  … 解決できない／関係のありそうな社（★2AIへ回す★）
            #   MISMATCH … 明らかに別の社（使わない）
            # ★「関係のある社」と「まったく分からない社」を分ける★
            #   （2026-08-17・依頼225のCodex指摘2）
            #   前はどちらも UNKNOWN にまとめていたので、
            #   「名簿に無いだけの任意の別会社」まで同じ扱いになっていた。
            #   ★同名で別メーカーの機種は実在する★
            #   （パチスロ犬夜叉＝2016年ロデオ／2022年クロスアルファ）ので、
            #   まったく分からない社は通してはいけない。
            if expected_maker in owners:
                _state = "MATCH"
            elif owners and _related(expected_maker, owners):
                _state = "RELATED"      # 関係のありそうな社（2AIへ回す）
            elif not owners:
                _state = "UNKNOWN"      # どの社か分からない（使わない）
            else:
                _state = "MISMATCH"     # 明らかに別の社（使わない）
            out["maker_check"] = {"state": _state, "seen": mk,
                                  "expected": expected_maker,
                                  "owners": sorted(owners)}
            if _state == "MISMATCH":
                out["reason"] = (f"DIRECTORY_MAKER_MISMATCH（名鑑のメーカー欄が"
                                 f"別の社を指しています: {mk[:30]}）")
                return out
            if _state == "RELATED":
                # ★関係のありそうな社★＝2AIへ回す印。
                #   ★材料に使えるのは、機種ごとの控えで ACCEPT_MATERIAL と
                #     決めてある時だけ★（2026-08-17・依頼226と228）。
                #   以前ここには「材料には使ってよい」と書いてあったが、
                #   採否の実装（add_machine_run.maker_material_decision）とは
                #   逆で、次に読む人が古い説明を正本だと思う元になっていた。
                #   ★型式名の票には入れない★（同定の芯なので厳しいまま）。
                out["reason"] = (f"DIRECTORY_MAKER_RELATED（名鑑のメーカー欄は"
                                 f"関係のある社です: {mk[:30]}。同一かは2AIで"
                                 f"決めてください）")
                return out
            if _state == "UNKNOWN":
                # ★解決できない表記の票は採用しない★（2026-08-02・Codex51回目）
                #   44回目は「ログだけ残して育てる」段階案だったが、
                #   同名別会社機を異なる2名鑑が載せると誤った型式を
                #   2票一致として公開できてしまう（誤情報側の穴）。
                #   実在の別名（レオスター等）は directory_names に足せば通る
                #   ＝不採用は「名簿を直せば直る」待ち行列側の失敗にとどまる。
                out["reason"] = (f"DIRECTORY_MAKER_UNRESOLVED（名鑑のメーカー欄を"
                                 f"名簿で解決できません: {mk[:30]}。実在の別名なら"
                                 f" directory_names へ追加）")
                return out
    code, why = extract_model_code(html)
    out["model_code"] = code
    out["reason"] = why
    return out


def agree(results: list) -> dict:
    """★独立2つ以上の名鑑で型式名が一致して初めて採用する★

    ★比較は空白を無視した鍵で行う★（2026-08-02・Codex24回目を再現して直した）
      「Lびん娘NY1」と「Lびん娘 NY1」（空白差）を別の型式として CONFLICT にし、
      やり直しても直らない理由なので**機種ごと自動経路から外していた**。
      表示に使う値は最初に見つかった書き方のまま残す。
    """
    def _key(c: str) -> str:
        t = unicodedata.normalize("NFKC", c)
        t = re.sub(r"[\s　]+", "", t)
        # ★ダッシュの種類の違いを食い違いにしない★（2026-08-02・Codex32回目）
        #   抽出は - − – — 等を許すのに、比較で別物にすると恒久CONFLICTになる。
        return re.sub("[‐‑–—−]", "-", t)

    # ★ホストの数ではなく「独立した票の数」で数える★（2026-08-14・依頼192のP1）
    #   同じ会社の別ホスト（P-WORLDと羽伏せ）が2票になり、
    #   共同制作の組（一撃×DMM）も2票になっていた。
    #   ＝「独立2出典」の土台が、型式名のところだけ崩れていた。
    codes = {}          # 比較鍵 -> hosts集合
    shown = {}          # 比較鍵 -> 最初に見つかった表示値
    for r in results:
        if r.get("model_code"):
            host = r["url"].split("/")[2].lower().removeprefix("www.")
            k = _key(r["model_code"])
            codes.setdefault(k, set()).add(host)
            shown.setdefault(k, r["model_code"])
    # ★食い違いを先に見る★（2026-07-31・Codex22回目。実際に再現した）
    #   以前は「2票そろった型式」を見つけた時点で採用していたので、
    #   A=2票・B=1票 のときAをそのまま採り、食い違いに気づかなかった。
    #   型式が食い違う＝別の機種の資料が混じっているので、材料ごと信用できない。
    if len(codes) >= 2:
        return {"model_code": None, "adopted": False, "state": "CONFLICT",
                "why": "名鑑ごとに型式名が食い違っています: "
                       + json.dumps({k: sorted(v) for k, v in codes.items()},
                                    ensure_ascii=False)}
    for code, hosts in codes.items():
        if _sl_votes(hosts) >= 2:
            return {"model_code": shown[code], "hosts": sorted(hosts),
                    "adopted": True}
    # ★「まだ載っていない」と「食い違う」を分ける★（2026-07-31・Codex21回目）
    #   どちらも同じ文言だったので、
    #   **明日には載るかもしれない新台**まで「やり直しても無駄」と扱い、
    #   初回で待ち行列から外していた。
    detail = json.dumps({k: sorted(v) for k, v in codes.items()},
                        ensure_ascii=False)
    if len(codes) >= 2:
        return {"model_code": None, "adopted": False, "state": "CONFLICT",
                "why": f"名鑑ごとに型式名が食い違っています: {detail}"}
    if not codes:
        return {"model_code": None, "adopted": False, "state": "NOT_YET",
                "why": "型式名がまだどの名鑑にも載っていません"}
    # ★1つしか無い型式も「観測した値」として返す★（2026-08-09・依頼130 P1-2）
    #   採用（記事に出す・独立2出典）はしないが、**捨てると同定に使えない**。
    #   実測: 型式を載せているのは P-WORLD だけなので、捨てると
    #   ①L/Sの規格印の矛盾を型式から見つけられない
    #   ②型式が同じ既存機種との重複を見つけられない
    #   という、取り違えを防ぐ検査の入力が丸ごと消えていた。
    only = next(iter(codes)) if len(codes) == 1 else None
    return {"model_code": None, "adopted": False, "state": "NOT_YET",
            "observed_model_code": shown.get(only) if only else None,
            "observed_hosts": sorted(codes[only]) if only else [],
            "why": f"型式名が1つの名鑑にしか載っていません: {detail}"}


# ------------------------------------------------------- selftest の補助

def _catalog_tokens(conf: dict, mid: str) -> set:
    """★照合に実際に使う呼び名の全部★（name・別名・メーカーID）。"""
    toks = [str(conf.get("name") or ""), str(mid)]
    toks += [str(x) for x in (conf.get("directory_names") or [])]
    return {_ci.normalize_core(x) for x in toks if x} - {""}


def _catalog_name_collisions() -> list:
    """同じ呼び名が2社にまたがっていないか（またがると1社に絞れない）。"""
    seen: dict = {}
    try:
        cats = json.load(open(_w.CATALOGS, encoding="utf-8"))["catalogs"]
    except Exception:                     # noqa: BLE001
        return [("名簿が読めません", [])]
    for mid, conf in cats.items():
        if not isinstance(conf, dict):
            continue
        for c in _catalog_tokens(conf, mid):
            seen.setdefault(c, set()).add(mid)
    return [(c, sorted(ms)) for c, ms in seen.items() if len(ms) > 1]


def _write_tmp_catalogs(cats: dict) -> None:
    """試験のあいだだけ名簿を差し替える（本物には触らない）。"""
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"catalogs": cats}, f, ensure_ascii=False)
    _w.CATALOGS = tmp


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []
    # ★試験の作り物HTMLには投稿欄の箱が無い★（2026-08-14・台帳#345）
    #   ここで見たいのは型式名とメーカー欄の判定であって、箱落としではない。
    #   箱落としそのものは user_area の試験と、実ページで確かめている。
    _ua.clean_html = lambda html, url="", conf=None: html

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    nl = chr(10)
    t("★『型式名：値』の形から取れる★",
      extract_model_code("<p>型式名：Lびん娘NY1</p>")[0] == "Lびん娘NY1")
    t("★★見出しの次の行に値がある形からも取れる★★（P-WORLDがこの形）",
      extract_model_code("<p>型式名  :</p><p>Lびん娘NY1</p>")[0] == "Lびん娘NY1")
    t("　全角は揃える",
      extract_model_code("<p>型式名：Ｌびん娘ＮＹ１</p>")[0] == "Lびん娘NY1")
    t("★『記載なし』を型式名にしない★",
      extract_model_code("<p>型式名：記載なし</p>") == (None, "MODEL_CODE_NOT_STATED"))
    t("　型式の記載が無ければ理由を返す",
      extract_model_code("<p>導入日：2026年8月3日</p>")[1] == "MODEL_CODE_NOT_FOUND")
    t("★説明文を型式名として拾わない★",
      extract_model_code(
          "<p>型式名：この機種の型式については後日公表される予定となっています。"
          "なお導入は8月です。</p>")[0] is None)

    t("★★独立2つの名鑑で一致して初めて採用★★",
      agree([{"url": "https://nana-press.com/x", "model_code": "Lびん娘NY1"},
             {"url": "https://p-town.dmm.com/y", "model_code": "Lびん娘NY1"}])["adopted"]
      is True)
    t("　1つだけでは採用しない",
      agree([{"url": "https://nana-press.com/x",
              "model_code": "Lびん娘NY1"}])["adopted"] is False)
    t("★同じサイトの2ページを2票と数えない★",
      agree([{"url": "https://nana-press.com/x", "model_code": "A1"},
             {"url": "https://nana-press.com/y", "model_code": "A1"}])["adopted"] is False)
    t("　食い違ったら採用しない（理由を残す）",
      agree([{"url": "https://nana-press.com/x", "model_code": "A1"},
             {"url": "https://p-town.dmm.com/y", "model_code": "B2"}])["adopted"] is False)

    t("★★一致する場合はちゃんと通る★★（全部落ちていて気づかない事故を防ぐ）",
      page_is_machine("<title>Lすーぱぁびん娘(スマスロ) パチスロ新台 | P-WORLD</title>",
                      "Lすーぱぁびん娘") == (True, "OK"))
    t("　全角・サイト名つきでも通る",
      page_is_machine("<title>Ｌすーぱぁびん娘｜DMMぱちタウン</title>",
                      "Lすーぱぁびん娘")[0] is True)
    t("★★続編を本人と誤認しない★★（前方一致だけだと通ってしまう）",
      page_is_machine("<title>Lすーぱぁびん娘2 | P-WORLD</title>",
                      "Lすーぱぁびん娘")[0] is False)
    # ★★ここから Codex22回目の反例★★（前方一致＋数字だけの検査を通っていた）
    for _bad in ("Lすーぱぁびん娘新章 | P-WORLD", "Lすーぱぁびん娘SP | P-WORLD",
                 "Lすーぱぁびん娘・極 | P-WORLD", "Lすーぱぁびん娘 SP | P-WORLD",
                 "Lすーぱぁびん娘 改 パチスロ新台 | P-WORLD"):
        t(f"★★別機種を本人にしない: {_bad[:22]}★★",
          page_is_machine(f"<title>{_bad}</title>", "Lすーぱぁびん娘")[0] is False)
    t("★★名前の中の括弧を割らない★★（実データ・甲鉄城のカバネリ）",
      page_is_machine("<title>スマスロ 甲鉄城のカバネリ 海門(うなと)決戦 "
                      "パチスロ新台 スロット 機械割</title>",
                      "スマスロ 甲鉄城のカバネリ 海門(うなと)決戦")[0] is True)
    t("★★別名が題に入っていても通る★★（実データ・東京喰種／東京グール）",
      page_is_machine("<title>スマスロ東京喰種 スロット 新台 天井 設定判別 解析 "
                      "東京グール | ちょんぼりすた パチスロ解析</title>",
                      "L 東京喰種")[0] is True)
    t("★名前の芯が違うページからは採らない★",
      page_is_machine("<title>Lスーパービンゴネオ|P-WORLD</title>",
                      "Lすーぱぁびん娘")[0] is False)
    # ★★Codex23回目（自分で再現してから直した）★★
    t("★★S版のページをL版の本人にしない★★（規格が違えば別機種・Codex23回目）",
      page_is_machine("<title>S北斗の拳 新台 | P-WORLD</title>",
                      "L北斗の拳") == (False, "GEN_MARK_CONFLICT"))

    # ★★落ちた理由を混ぜない★★（2026-08-22・実害があった）
    #   ★直す前★＝4つの別々の落ち方が全部 GEN_MARK_CONFLICT を名乗っていた。
    #   pw_10510（タコスロ）は「規格印の食い違い」と報告されていたが、
    #   本当は★題の後ろの「ボーナストリガー」を飾りとして分解できない★だけで、
    #   L/Sの比較は成功していた。＝**丸1日、原因を取り違えて調べた**。
    def _pg(_t):
        return "<html><head><title>" + _t + "</title></head><body></body></html>"

    t("★★後ろの飾りを説明できないのは TAIL_CONFLICT★★"
      "（規格印の食い違いと名乗っていた）",
      page_is_machine(
          _pg("スマスロ タコスロ(新台スマスロ)パチスロ|"
              "ボーナストリガー・設定判別・天井・ゾーン・解析・打ち方・ヤメ時"),
          "スマスロ タコスロ", strict_all_tail=True)
      == (False, "TAIL_CONFLICT"))
    t("　飾りが全部説明できる題は今までどおり通る",
      page_is_machine(
          _pg("スマスロ ゴジラ対エヴァンゲリオン(新台スマスロ)パチスロ|"
              "ボーナス・設定判別・天井・解析"),
          "スマスロ ゴジラ対エヴァンゲリオン", strict_all_tail=True)
      == (True, "OK"))
    t("★規格印が本当に食い違うときは今までどおり GEN_MARK_CONFLICT★",
      page_is_machine(_pg("S北斗の拳 パチスロ新台"), "L北斗の拳")
      == (False, "GEN_MARK_CONFLICT"))
    t("　芯がそもそも合わないときは NAME_CORE_MISMATCH",
      page_is_machine(_pg("まったく別の機種 パチスロ新台"), "L北斗の拳")
      == (False, "NAME_CORE_MISMATCH"))
    t("　逆（L版のページをS版の本人に）も弾く",
      page_is_machine("<title>L北斗の拳 新台 | P-WORLD</title>",
                      "S北斗の拳")[0] is False)
    t("　独立した語の印（L 東京喰種）でも読める",
      page_is_machine("<title>S 東京喰種 新台 | P-WORLD</title>",
                      "L 東京喰種")[0] is False)
    t("　スマスロ表記はLと同じ規格として通す（実データの形）",
      page_is_machine("<title>スマスロ東京喰種 スロット 新台 天井 解析 | x</title>",
                      "L 東京喰種")[0] is True)
    t("　印が書かれていない題は従来どおり通る",
      page_is_machine("<title>北斗の拳 新台 | P-WORLD</title>",
                      "L北斗の拳")[0] is True)
    t("★★「パチンコ」を飾り扱いしない★★（パチンコ版のページを本人にできた・Codex23回目）",
      page_is_machine("<title>北斗の拳 パチンコ 新台 | P-WORLD</title>",
                      "北斗の拳")[0] is False)
    t("　英字の機種名をL/Sの印と取り違えない（lucky等）",
      _gen_mark("lucky trigger") == "" and _gen_mark("L北斗の拳") == "L"
      and _gen_mark("スマスロ北斗の拳") == "L" and _gen_mark("パチスロ北斗の拳") == "")
    # ★★Codex24回目（自分で再現してから直した）★★
    t("★★空白つきの別種目の印を弾く: 「P 北斗の拳」★★（Codex24回目）",
      page_is_machine("<title>P 北斗の拳 新台 | P-WORLD</title>",
                      "L北斗の拳")[0] is False)
    t("　前置の「パチンコ」も弾く",
      page_is_machine("<title>パチンコ 北斗の拳 新台 | P-WORLD</title>",
                      "L北斗の拳")[0] is False)
    t("★★対象名が副次的に現れる題を弾く★★（前に知らない語がある・Codex24回目）",
      page_is_machine("<title>L別機種の話 L北斗の拳 新台 | x</title>",
                      "L北斗の拳")[0] is False)
    t("　名前の前が飾りと規格語だけなら通る",
      page_is_machine("<title>新台 スマスロ 北斗の拳 天井 | x</title>",
                      "L北斗の拳")[0] is True)
    t("★★「型式名について」の「について」を値にしない★★（Codex24回目）",
      extract_model_code("<p>型式名についての説明</p><p>Lびん娘NY1</p>")
      == (None, "MODEL_CODE_NOT_FOUND"))
    t("★★次の行の別見出し（メーカー名）を型式名にしない★★（Codex24回目）",
      extract_model_code("<p>型式名：</p><p>メーカー名</p>")
      == (None, "MODEL_CODE_NOT_FOUND"))
    t("　英数字を含まない語は型式名として採らない（安全側＝まだ載っていない扱い）",
      extract_model_code("<p>型式名：ぱちすろほくと</p>")
      == (None, "MODEL_CODE_NOT_FOUND"))
    t("★★型式名の空白差を食い違いにしない★★"
      "（CONFLICTだと機種ごと自動経路から外れていた・Codex24回目）",
      agree([{"url": "https://nana-press.com/x", "model_code": "Lびん娘NY1"},
             {"url": "https://p-town.dmm.com/y", "model_code": "Lびん娘 NY1"}])
      == {"model_code": "Lびん娘NY1",
          "hosts": sorted(["p-town.dmm.com", "nana-press.com"]),
          "adopted": True})
    # ★★Codex25回目（自分で再現してから直した）★★
    t("★★区切りの後ろの断片でも、元の題の前置を見る★★（Codex25回目）",
      page_is_machine("<title>別機種 | L北斗の拳 新台</title>",
                      "L北斗の拳")[0] is False)
    t("　【】区切りでも同じ",
      page_is_machine("<title>別機種【L北斗の拳 新台】</title>",
                      "L北斗の拳")[0] is False)
    t("　／区切りでも同じ",
      page_is_machine("<title>別機種の話/L北斗の拳</title>",
                      "L北斗の拳")[0] is False)
    t("　前が飾りの断片は通る（【新台】L北斗の拳 | P-WORLD）",
      page_is_machine("<title>【新台】L北斗の拳 | P-WORLD</title>",
                      "L北斗の拳")[0] is True)
    t("　実データ形（名前が先頭・後ろにサイト名）は通る",
      page_is_machine("<title>L北斗の拳(サミー) パチスロ 機種情報 | P-WORLD</title>",
                      "L北斗の拳")[0] is True)
    t("★全角の型式名も従来どおり取れる★（本文抽出でNFKC済み・Codex25回目の指摘は非該当）",
      extract_model_code("<p>型式名：Ｌびん娘ＮＹ１</p>") == ("Lびん娘NY1", "OK"))
    # ★★Codex26回目（自分で再現してから直した）★★
    t("★★名前の直後の括弧の派生印を弾く: （SP）★★（Codex26回目）",
      page_is_machine("<title>Lすーぱぁびん娘（SP） | P-WORLD</title>",
                      "Lすーぱぁびん娘")[0] is False)
    t("　【新章】・／極 も弾く",
      page_is_machine("<title>Lすーぱぁびん娘【新章】</title>",
                      "Lすーぱぁびん娘")[0] is False
      and page_is_machine("<title>Lすーぱぁびん娘／極</title>",
                          "Lすーぱぁびん娘")[0] is False)
    t("　直後の括弧がメーカー名なら通る（実データ形）",
      page_is_machine("<title>L北斗の拳(サミー) パチスロ 機種情報 | P-WORLD</title>",
                      "L北斗の拳")[0] is True)
    # ★★Codex28回目★★
    t("★★許可した社名の後ろの派生印も見る★★（…|BELLCO|SP が通っていた・Codex28回目）",
      page_is_machine("<title>Lすーぱぁびん娘|EXAMPLE|SP</title>",
                      "Lすーぱぁびん娘",
                      extra_tail_ok={"example"})[0] is False)
    t("★★メーカー語は完全一致だけ★★（部分一致だとSPBELLCOまで許した・Codex28回目）",
      _after_ok("SPBELLCO", "x", "x", {"bellco"}) is False
      and _after_ok("BELLCO", "x", "x", {"bellco"}) is True
      and _after_ok("株式会社BELLCO", "x", "x", {"bellco"}) is True)
    # ★★Codex32回目★★
    t("★★別の断片の規格語を「(SP)」の括弧の規格語に数えない★★（Codex32回目）",
      page_is_machine("<title>L SP TEST（SP） パチスロ 新台 | P-WORLD</title>",
                      "L SP TEST")[0] is False)
    t("★★飾り語の後ろの派生印も見る（型式・公式の照合）★★（Codex32回目）",
      page_is_machine("<title>Lすーぱぁびん娘 新台 SP | P-WORLD</title>",
                      "Lすーぱぁびん娘", strict_all_tail=True)[0] is False)
    t("★★材料の照合でも明確な派生印（SP等）は拒む★★"
      "（独立2サイトがSP版の値で一致すると混入できた・Codex33回目）",
      page_is_machine("<title>Lすーぱぁびん娘 新台 SP | 解析サイトA</title>",
                      "Lすーぱぁびん娘")[0] is False
      and page_is_machine("<title>Lすーぱぁびん娘 新台 2 | 解析サイトA</title>",
                          "Lすーぱぁびん娘")[0] is False)
    t("　材料の照合（別名が題の後ろに入る解析サイト）は従来どおり",
      page_is_machine("<title>スマスロ東京喰種 スロット 新台 天井 設定判別 解析 "
                      "東京グール | ちょんぼりすた パチスロ解析</title>",
                      "L 東京喰種")[0] is True)
    t("★★型式名のダッシュ表記差を食い違いにしない★★（Codex32回目）",
      agree([{"url": "https://nana-press.com/x", "model_code": "LTEST-A"},
             {"url": "https://p-town.dmm.com/y", "model_code": "LTEST−A"}])
      ["adopted"] is True)
    t("★★「(スマスロ SP)」の派生印を略称として許さない★★"
      "（名前の芯に偶然s,pが並ぶ機種で素通りした・Codex38回目）",
      page_is_machine("<title>L SP TEST（スマスロ SP） | P-WORLD</title>",
                      "L SP TEST", strict_all_tail=True)[0] is False)
    # ★★とんスキ実データ（2026-08-02・更新タスク初回が実際に弾いた）★★
    t("★★実在の題「(Lとんスキ)」＝規格印つき略称を通す★★（P-WORLD実データ）",
      page_is_machine("<title>スマスロ とんでもスキルで異世界放浪メシ(Lとんスキ) "
                      "パチスロ新台 スロット 機械割 天井 初打ち 打ち方 スペック "
                      "掲示板 設置店 | P-WORLD</title>",
                      "スマスロ とんでもスキルで異世界放浪メシ",
                      strict_all_tail=True)[0] is True)
    t("★★実在の題「(新台スマスロ)…設定判別・天井・…」＝飾りの連結語を通す★★"
      "（DMM実データ）",
      page_is_machine("<title>スマスロ とんでもスキルで異世界放浪メシ(新台スマスロ)"
                      "パチスロ|設定判別・天井・ゾーン・解析・打ち方・ヤメ時</title>",
                      "スマスロ とんでもスキルで異世界放浪メシ",
                      strict_all_tail=True)[0] is True)
    t("　規格印つきでも派生印（L改）は弾く",
      page_is_machine("<title>Lすーぱぁびん娘（L改） | P-WORLD</title>",
                      "Lすーぱぁびん娘", strict_all_tail=True)[0] is False)
    t("　飾りの連結を装った派生印（SP新台）は弾く",
      _decor_compound("SP新台") is False and _decor_compound("新台スマスロ") is True
      and _decor_compound("設定判別・天井・ゾーン・解析・打ち方・ヤメ時") is True)
    # ★★Codex40回目★★
    t("★★実在の題「マイジャグラーVI(マイジャグラー6 マイジャグ6)」を通す★★"
      "（規格語なしの別名括弧・P-WORLD実データ・Codex40回目）",
      page_is_machine("<title>マイジャグラーVI(マイジャグラー6 マイジャグ6) "
                      "パチスロ新台 スロット 機械割 天井 | P-WORLD</title>",
                      "マイジャグラーVI", strict_all_tail=True)[0] is True)
    t("　世代表記の同値化はVI↔6の形だけ（SPや新章は従来どおり弾く）",
      _canon_gen_num("まいじゃぐらーvi") == "まいじゃぐらー6"
      and page_is_machine("<title>Lすーぱぁびん娘（SP） | P-WORLD</title>",
                          "Lすーぱぁびん娘", strict_all_tail=True)[0] is False)
    t("★★名鑑のメーカー欄が名簿の別の社なら採らない★★（Codex40回目）",
      (lambda: (setattr(_w, "_get_bak40", _w._get),
                setattr(_w, "_get", lambda u, timeout=20:
                    "<title>L試験機 パチスロ新台 | P-WORLD</title>"
                    "<p>メーカー名：サミー</p><p>型式名：L試験1</p>"),
                lookup("https://nana-press.com/x", "L試験機",
                       expected_maker="heiwa"),
                setattr(_w, "_get", _w._get_bak40))[2])()
      ["reason"].startswith("DIRECTORY_MAKER_MISMATCH"))
    # ★★2026-08-13・実ログ（手動実行）で3機種が材料0件になった件★★
    #   名鑑のメーカー欄を名簿で解決できず、P-WORLD自身のページまで
    #   「別の社」として弾かれていた。原因は2つで、両方ここで見張る。
    # ★★2026-08-13・実データ（Lパチスロ 彼女、お借りします）★★
    #   3つの名鑑すべてが題に機種名を丸ごと載せているのに全部落ちていた。
    _kanojo = "Lパチスロ 彼女、お借りします"
    t("★★機種名の読点で名前を割らない★★"
      "（「彼女、お借りします」が「彼女」と「お借りします」に割れていた）",
      title_parts("Lパチスロ 彼女、お借りします 新台解析")
      == ["Lパチスロ 彼女、お借りします 新台解析"])
    t("★★飾りを連結した語（新台解析）が次に来ても本人と分かる★★"
      "（後ろの断片を見る _after_ok とここの扱いが食い違っていた）",
      page_is_machine("<title>Lパチスロ 彼女、お借りします 新台解析|"
                      "天井・設定判別・やめどきまとめ | "
                      "ちょんぼりすた パチスロ解析</title>",
                      _kanojo, strict_all_tail=True)[0] is True)
    t("　同じ機種をDMM・ナナプレスの実在の題でも通す",
      page_is_machine("<title>Lパチスロ 彼女、お借りします(新台スマスロ)"
                      "パチスロ|設定判別・天井・ゾーン・解析・打ち方・ヤメ時"
                      "</title>", _kanojo, strict_all_tail=True)[0] is True
      and page_is_machine("<title>【彼女、お借りします(スマスロ)】解析情報まとめ"
                          " 天井・設定判別・スペック・打ち方・やめどき</title>",
                          _kanojo, strict_all_tail=True)[0] is True)
    t("★★緩めていないこと＝派生印・別規格・前置は今までどおり弾く★★",
      page_is_machine("<title>Lすーぱぁびん娘 SP新台|天井・解析</title>",
                      "Lすーぱぁびん娘", strict_all_tail=True)[0] is False
      and page_is_machine("<title>S北斗の拳 新台解析 | x</title>",
                          "L北斗の拳", strict_all_tail=True)[0] is False
      and page_is_machine("<title>P 北斗の拳 新台解析 | x</title>",
                          "L北斗の拳", strict_all_tail=True)[0] is False
      and page_is_machine("<title>別機種の話 L北斗の拳 新台解析</title>",
                          "L北斗の拳", strict_all_tail=True)[0] is False)
    # ★★2026-08-13・依頼173のP1（Codexの反例）★★
    #   飾りの連結語をゆるい経路でも許すと、後ろの別機種を見落とす。
    #   ゆるい経路は「残り全部」を見ないため、ここだけは締めておく。
    t("★★ゆるい経路では、飾りの連結語の後ろに別機種があれば本人にしない★★"
      "（confirmed_values / machine_sources が使う経路・依頼173のP1）",
      page_is_machine("<title>L対象機 新台解析 L別機種</title>",
                      "L対象機")[0] is False)
    t("　厳格な経路（材料集め・型式照合）では、その題は規格印の食い違いで弾く",
      page_is_machine("<title>L対象機 新台解析 L別機種</title>",
                      "L対象機", strict_all_tail=True)[0] is False)
    t("★★読点を区切りから外しても、並んだ別機種は本人にしない★★"
      "（Codexが挙げた3つの形・依頼173）",
      all(page_is_machine(f"<title>{_t}</title>", "L対象機")[0] is False
          and page_is_machine(f"<title>{_t}</title>", "L対象機",
                              strict_all_tail=True)[0] is False
          for _t in ("L対象機、L別機種", "L対象機、 L別機種",
                     "L対象機、新台解析、L別機種")))
    t("★★名鑑が「三洋物産」と書いたサンスリー製の機種を本人と認める★★"
      "（名簿の解決だけでなく、照合そのものを見る・依頼173のP2）",
      (lambda: (setattr(_w, "_get_bak173", _w._get),
                setattr(_w, "_get", lambda u, timeout=20:
                        "<title>L試験機 パチスロ新台 | P-WORLD</title>"
                        "<p>メーカー名：三洋物産</p><p>型式名：L試験1</p>"),
                lookup("https://nana-press.com/x", "L試験機",
                       expected_maker="sanslay"),
                setattr(_w, "_get", _w._get_bak173))[2])()
      ["identity_ok"] is True)
    t("　グループの外の社なら、今までどおり別の社として弾く",
      (lambda: (setattr(_w, "_get_bak174", _w._get),
                setattr(_w, "_get", lambda u, timeout=20:
                        "<title>L試験機 パチスロ新台 | P-WORLD</title>"
                        "<p>メーカー名：サミー</p><p>型式名：L試験1</p>"),
                lookup("https://nana-press.com/x", "L試験機",
                       expected_maker="sanslay"),
                setattr(_w, "_get", _w._get_bak174))[2])()
      ["reason"].startswith("DIRECTORY_MAKER_MISMATCH"))
    t("★★巡回しない社（list_urlなし）も名簿として解決できる★★"
      "（京楽・サンスリー等が『解決できません』で落ちていた）",
      _maker_core_owners(_ci.normalize_core("京楽産業.")) == {"kyoraku"}
      and _maker_core_owners(_ci.normalize_core("サンスリー")) == {"sanslay"})
    t("★★別名が別名を含むときは、より具体的に当たった社が勝つ★★"
      "（『オリンピア』＝平和の別名が『オリンピアエステート』に当たっていた）",
      _maker_core_owners(_ci.normalize_core("オリンピアエステート"))
      == {"olympia_estate"}
      and _maker_core_owners(_ci.normalize_core("オリンピア")) == {"heiwa"})
    t("　従来どおり、名簿に無い表記はどの社も指さない（判定不能）",
      _maker_core_owners(_ci.normalize_core("架空スロット工業")) == set())
    t("★★名簿の中で同じ呼び名が2社にまたがっていない★★"
      "（またがると『最も具体的な社』が決められず、材料が全部落ちる）"
      "／★照合に使うメーカーIDも同じ土俵で見る★（依頼172）",
      not _catalog_name_collisions())
    t("★★同じ長さで2社が並んだら『決められない』で止める★★（依頼172のP2）"
      "／どちらでも通ると、メーカー違いの材料を採る恐れがある",
      (lambda: (setattr(_w, "_cat_bak", _w.CATALOGS),
                _write_tmp_catalogs({
                    "aa": {"name": "テスト社", "status": "WATCH_OFF"},
                    "bb": {"name": "テスト社", "status": "WATCH_OFF"}}),
                _maker_core_owners(_ci.normalize_core("テスト社")),
                setattr(_w, "CATALOGS", _w._cat_bak))[2])() == set())
    # ★★2026-08-14・依頼189で方針を変えた★★
    #   以前は「同じグループなら全機種で自動的に通す」だったが、
    #   公式は「グループ会社」と書いているだけで、
    #   ★全機種でメーカー名を入れ替えてよいとは書いていない★。
    #   1回の判断ミスが以後すべての機種で関門を無効にするので、
    #   いまは「2AIへ回す価値のある関係」の印にとどめる。
    t("★★同じグループでも自動では通さない★★（依頼189・方針変更）"
      "／『関係あり』として2AIへ回す印にする",
      _related("olympia_estate", {"heiwa"})
      and _related("heiwa", {"olympia_estate"})
      and _related("sanslay", {"sanyo_bussan"}))
    t("　関係の無い社どうしは、そのまま別の社",
      _related("sammy", {"universal"}) is False
      and _related("", {"heiwa"}) is False)

    def _old_field_stops():
        """★旧項目を足せば元に戻せる、を塞げたか★（依頼190のP2）"""
        _bak = _w.CATALOGS
        try:
            _write_tmp_catalogs({"zz": {"name": "テスト社", "status": "WATCH_OFF",
                                        "maker_identity_group": "grp"}})
            try:
                _relation_group("zz")
                return False
            except LookupError_:
                return True
        finally:
            _w.CATALOGS = _bak

    t("★★廃止した旧項目 maker_identity_group は読まずに止める★★"
      "（2026-08-14・依頼190のP2）／互換で読み続けると、名簿に旧名を足すだけで"
      "「全機種で自動的に通す」が静かに戻る",
      _old_field_stops())
    t("★★メーカー欄の判定が四つに分かれる★★（2026-08-17・依頼225／228）"
      "／MATCH＝名簿で一致（使う）"
      "／RELATED＝関係のある社（2AIへ・★控えで決めてある時だけ材料に使う★）"
      "／UNKNOWN＝どの社か分からない（★控えでも救わない★）"
      "／MISMATCH＝別の社（使わない）"
      "★同名で別メーカーの機種は実在する★のでUNKNOWNは通さない",
      (lambda f: [f("サンスリー"), f("三洋物産"), f("サミー"), f("架空社")]
       == ["MATCH", "RELATED", "MISMATCH", "UNKNOWN"])(
          lambda seen: (lambda: (
              setattr(_w, "_get_bak189", _w._get),
              setattr(_w, "_get", lambda u, timeout=20:
                      "<title>L試験機 パチスロ新台 | P-WORLD</title>"
                      f"<p>メーカー名：{seen}</p><p>型式名：L試験1</p>"),
              lookup("https://x.test/a", "L試験機",
                     expected_maker="sanslay"),
              setattr(_w, "_get", _w._get_bak189))[2])()
          ["maker_check"]["state"]))
    # ★この位置にあった試験は消した★（2026-08-21・台帳#379の【4】）
    #   scan_maker（メーカー公式の巡回）そのものを消したため。
    #   ★止めた仕組みの試験だけを残さない★＝残すと「まだ生きている」と読める。
    t("　名簿の別名（コナミアミューズメント→KPE）は解決されて通る"
      "（KPEのとんスキ実データを弾かないため）",
      (lambda: (setattr(_w, "_get_bak41", _w._get),
                setattr(_w, "_get", lambda u, timeout=20:
                    "<title>L試験機 パチスロ新台 | P-WORLD</title>"
                    "<p>メーカー名：コナミアミューズメント</p><p>型式名：L試験1</p>"),
                lookup("https://nana-press.com/x", "L試験機",
                       expected_maker="kpe"),
                setattr(_w, "_get", _w._get_bak41))[2])()["model_code"] == "L試験1")
    # ★★Codex51回目★★
    t("★★名簿で解決できないメーカー欄の票は採用しない★★（Codex51回目・"
      "同名別会社機の2名鑑一致で誤った型式を公開できた）",
      (lambda: (setattr(_w, "_get_bak51", _w._get),
                setattr(_w, "_get", lambda u, timeout=20:
                    "<title>L試験機 パチスロ新台 | P-WORLD</title>"
                    "<p>メーカー名：名簿にない別会社</p><p>型式名：L別物1</p>"),
                lookup("https://nana-press.com/x", "L試験機",
                       expected_maker="heiwa"),
                setattr(_w, "_get", _w._get_bak51))[2])()
      ["reason"].startswith("DIRECTORY_MAKER_UNRESOLVED"))
    t("　レオスター（エンターライズの名鑑表記・P-WORLD実データ）は解決される",
      "enterrise" in _maker_core_owners("レオスター"))
    # ★★Codex52回目★★
    t("★★オリンピア（平和の名鑑表記・P-WORLD実データ）は解決される★★"
      "（無いと51回目の不採用化で現行の青ブタを公開できない・Codex52回目）",
      "heiwa" in _maker_core_owners("オリンピア"))
    _AOBUTA = "L青春ブタ野郎はバニーガール先輩の夢を見ない"
    t("★★実在形（P-WORLDの読み仮名つき題＋h1が正式名）を通す★★"
      "（青ブタ実データ・題だけでは読み仮名を確かめられない→h1で同定・Codex54回目）",
      page_is_machine(f"<title>{_AOBUTA}(スマスロ 青ブタ あおぶた) "
                      "パチスロ新台 スロット | P-WORLD</title>"
                      f"<h1>{_AOBUTA}</h1>",
                      _AOBUTA, strict_all_tail=True)[0] is True)
    t("　h1が無ければ読み仮名つきの題だけでは通さない（読みは推定しない）",
      page_is_machine(f"<title>{_AOBUTA}(スマスロ 青ブタ あおぶた) "
                      "パチスロ新台 スロット | P-WORLD</title>",
                      _AOBUTA, strict_all_tail=True)[0] is False)
    t("★★SEO題の略称（Lスト6 SF6・やじきた3）でもh1で通す★★"
      "（P-WORLD実在2ページ・実在の票を失っていた・Codex54回目）",
      page_is_machine("<title>スマスロ ストリートファイター6(Lスト6 SF6) "
                      "パチスロ新台 スロット | P-WORLD</title>"
                      "<h1>スマスロ ストリートファイター6</h1>",
                      "スマスロ ストリートファイター6",
                      strict_all_tail=True)[0] is True
      and page_is_machine("<title>スマスロ やじきた道中記参る!(やじきた参 "
                          "やじきた3) パチスロ新台 | P-WORLD</title>"
                          "<h1>スマスロ やじきた道中記参る!</h1>",
                          "スマスロ やじきた道中記参る!",
                          strict_all_tail=True)[0] is True)
    t("★★h1が2本以上なら、h1は同定の根拠にしない★★"
      "（節のh1の1本一致で別機種ページを本人にできた・Codex55回目）",
      page_is_machine("<title>L別機種</title><h1>L別機種</h1>"
                      "<section><h1>L対象機</h1></section>",
                      "L対象機", strict_all_tail=True)[0] is False)
    t("★★材料の緩い照合でも未知の版名（BLACK）は通さない★★"
      "（材料4モジュールをstrictへ・Codex55回目）",
      page_is_machine("<title>L対象機 新台 BLACK | 解析サイトA</title>",
                      "L対象機", strict_all_tail=True)[0] is False)
    t("★★ちょんぼりすたの実在題（…やめどき 解析まとめ）をstrictで通す★★"
      "（「まとめ」が飾り語に無く材料のstrict化で全滅していた・Codex57回目の過程で発見）",
      page_is_machine("<title>Lすーぱぁびん娘 スロット 新台 天井 設定判別 "
                      "やめどき 解析まとめ | ちょんぼりすた パチスロ解析</title>",
                      "Lすーぱぁびん娘", strict_all_tail=True)[0] is True)
    t("　h1が別機種・派生機なら通さない（隠しh1も数えない）",
      page_is_machine("<title>Lすーぱぁびん娘（SP） | P-WORLD</title>"
                      "<h1>Lすーぱぁびん娘SP</h1>",
                      "Lすーぱぁびん娘", strict_all_tail=True)[0] is False
      and page_is_machine("<title>Lすーぱぁびん娘（SP） | P-WORLD</title>"
                          "<h1 hidden>Lすーぱぁびん娘</h1>",
                          "Lすーぱぁびん娘", strict_all_tail=True)[0] is False)
    t("★★同定に落ちたページは identity_ok=偽で返す★★"
      "（材料収集への復活を呼び出し元が止めるため・Codex56回目）",
      (lambda: (setattr(_w, "_get_bak56", _w._get),
                setattr(_w, "_get", lambda u, timeout=20:
                    "<title>L対象機(サミー) パチスロ新台 | P-WORLD</title>"
                    "<p>型式名：L別物1</p>"),
                lookup("https://nana-press.com/x", "L対象機",
                       expected_maker="heiwa"),
                setattr(_w, "_get", _w._get_bak56))[2])()
      .get("identity_ok") is False
      and (lambda: (setattr(_w, "_get_bak57", _w._get),
                    setattr(_w, "_get", lambda u, timeout=20:
                        "<title>L対象機 パチスロ新台 | P-WORLD</title>"
                        "<p>まだ型式は載っていません</p>"),
                    lookup("https://nana-press.com/x", "L対象機",
                           expected_maker="heiwa"),
                    setattr(_w, "_get", _w._get_bak57))[2])()
      .get("identity_ok") is True)
    t("★★型式名の規格印は専用判定★★（LB/タコスロBD＝実在のBT型式・Codex54回目）",
      model_gen_mark("LB/タコスロBD") == "L"
      and model_gen_mark("SマイジャグラーVI KK") == "S"
      and model_gen_mark("L青春ブタ野郎L1") == "L"
      and model_gen_mark("LBX試験") == ""
      and model_gen_mark("") == "")
    t("　読み仮名を装った別機種・派生印・読みだけの括弧は通さない",
      page_is_machine(f"<title>{_AOBUTA}(スマスロ 青ブタ ほくと) | P</title>",
                      _AOBUTA, strict_all_tail=True)[0] is False
      and page_is_machine(f"<title>{_AOBUTA}(スマスロ 青ブタ スペシャル) | P</title>",
                          _AOBUTA, strict_all_tail=True)[0] is False
      and page_is_machine(f"<title>{_AOBUTA}(スマスロ あおぶた) | P</title>",
                          _AOBUTA, strict_all_tail=True)[0] is False)
    # ★★Codex53〜54回目★★
    t("★★読みを装う語（あおぶたかい・くろぶた）は題からは通らない★★"
      "（読みの推定を撤去・Codex54回目）",
      page_is_machine(f"<title>{_AOBUTA}(スマスロ 青ブタ あおぶたかい) "
                      "パチスロ新台 スロット | P-WORLD</title>",
                      _AOBUTA, strict_all_tail=True)[0] is False
      and page_is_machine(f"<title>{_AOBUTA}(スマスロ 青ブタ くろぶた) "
                          "パチスロ新台 スロット | P-WORLD</title>",
                          _AOBUTA, strict_all_tail=True)[0] is False)
    t("★★期待メーカーの照合中は他社の社名を題末尾に許さない★★"
      "（メーカー欄の無いページで別メーカーの同名機を採用できた・Codex53回目）",
      page_is_machine("<title>L試験機(サミー) パチスロ新台 | P-WORLD</title>",
                      "L試験機", strict_all_tail=True,
                      extra_tail_ok=maker_brand_cores("heiwa"))[0] is False
      and page_is_machine("<title>L試験機(平和) パチスロ新台 | P-WORLD</title>",
                          "L試験機", strict_all_tail=True,
                          extra_tail_ok=maker_brand_cores("heiwa"))[0] is True)
    # ★★Codex43回目（北電子・実データ）★★
    t("★★実在の題「マイジャグラーVI|パチスロ製品情報|株式会社北電子」を通す★★"
      "（北電子の新台を出せない経路だった・Codex43回目）",
      page_is_machine("<title>マイジャグラーVI｜パチスロ製品情報｜株式会社北電子"
                      "</title>", "マイジャグラーVI",
                      strict_all_tail=True)[0] is True)
    # ★★Codex44回目★★
    t("★★名鑑の別名（directory_names）で別の社を検知できる★★（Codex44回目）",
      "universal" in _maker_core_owners("ミズホ")
      and "kpe" in _maker_core_owners("コナミアミューズメント")
      and _maker_core_owners("そんな社は無い") == set())
    # ★★Codex47回目★★
    t("★★期待メーカーの名鑑別名（パオン・ディーピー）が題の括弧でも通る★★"
      "（メーカー欄にしか効いておらず正しい票を失った・Codex47回目）",
      page_is_machine("<title>スロット ワールドダイスター(パオン・ディーピー) "
                      "パチスロ新台 | P-WORLD</title>",
                      "スロット ワールドダイスター", strict_all_tail=True,
                      extra_tail_ok=maker_brand_cores("daitogiken"))[0] is True)
    t("　別の社の照合では通らない（別名は期待メーカーの分だけ渡す）",
      page_is_machine("<title>スロット ワールドダイスター(パオン・ディーピー) "
                      "パチスロ新台 | P-WORLD</title>",
                      "スロット ワールドダイスター", strict_all_tail=True,
                      extra_tail_ok=maker_brand_cores("sammy"))[0] is False)
    # ★★Codex48回目（DMM実在形）★★
    _nl48 = chr(10)
    t("★★シリーズ機種の月が並んでも、対象機の導入開始日だけを読む★★"
      "（DMM実在形・名鑑2票の月控えが消えていた・Codex48回目）",
      release_near_identity(_nl48.join(
          ["メーカー名", "テスト社", "型式名", "L試験1", "タイプ", "AT",
           "導入開始日", "2026年10月05日(月)予定"]
          + ["x"] * 30
          + ["シリーズ機種", "導入開始日: 2025年06月02日(月)", "旧機種A",
             "導入開始日: 2024年05月07日(火)", "旧機種B"])) == "2026-10")
    t("　型式名が無いページでは空（ページ全体の単独月へ退避）",
      release_near_identity("導入開始日" + _nl48 + "2026年10月05日") == "")
    # ★★Codex49回目（ベルコ実在形）★★
    t("★★ベルコの実在題（…BELLCO(ベルコ株式会社):パチンコ・パチスロメーカー）を通す★★"
      "（同テンプレートの新台が全滅する経路だった・Codex49回目）",
      page_is_machine("<title>Lすーぱぁびん娘|機種情報|BELLCO(ベルコ株式会社)"
                      ":パチンコ・パチスロメーカー</title>",
                      "Ｌすーぱぁびん娘", strict_all_tail=True,
                      extra_tail_ok=maker_brand_cores("bellco"))[0] is True)
    t("　「パチンコ」単独の印は引き続き弾く（23回目の穴を再び開けない）",
      page_is_machine("<title>北斗の拳 パチンコ 新台 | P-WORLD</title>",
                      "北斗の拳")[0] is False)
    # ★★Codex50回目（SAO2実在形）★★
    t("★★公式「…2」と名鑑「…II」を同じ機種として照合できる★★"
      "（SAO2の実在形・独立2名鑑を確保できなかった・Codex50回目）",
      page_is_machine("<title>スロット ソード・アート・オンラインII "
                      "パチスロ新台 | P-WORLD</title>",
                      "スロット ソード・アート・オンライン2",
                      strict_all_tail=True)[0] is True)
    t("　直後の括弧が本人の略称なら通る（実データ・青ブタ）",
      page_is_machine(
          "<title>L青春ブタ野郎はバニーガール先輩の夢を見ない"
          "(スマスロ 青ブタ) パチスロ新台 | P-WORLD</title>",
          "L青春ブタ野郎はバニーガール先輩の夢を見ない")[0] is True)
    t("　タイトルが無ければ採らない",
      page_is_machine("<p>本文だけ</p>", "Lすーぱぁびん娘")[0] is False)

    # ★型式名の許可文字は専用に分ける★（2026-08-06・台帳#238）
    def _mc_ok(x):
        return bool(_MODEL_CODE_OK.match(x)) and not any(
            w in x for w in _MODEL_CODE_NG_WORDS)

    t("★★「!」を含む型式名を採れる★★（2名鑑一致でも採れず登録できなかった）",
      _mc_ok("Lやじきた道中記参る!BG"))
    t("　これまで採れていた型式名は引き続き採れる",
      _mc_ok("LとんでもスキルKM") and _mc_ok("Lパチスロ喰霊零Re/L3"))
    t("★★注記が混じった行は型式名にしない★★（「… 予定」など）",
      not _mc_ok("8月3日導入予定!") and not _mc_ok("新台 Lなんとか"))
    t("★★1文字＋記号のような形は採らない★★（「L!」）",
      not _mc_ok("L!") and not _mc_ok("!!"))
    t("★★純増などの共用ルールは広げていない★★（型式名専用に切り分けた）",
      not _CODE_OK.match("Lやじきた道中記参る!BG"))

    ng = [n for n, ok in results if not ok]
    print(f"{nl}{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--url", action="append", help="名鑑ページのURL（複数指定可）")
    ap.add_argument("--name", help="メーカー公式の正式名称")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.url or not args.name:
        ap.print_help()
        return 0
    rs = [lookup(u, args.name) for u in args.url]
    for r in rs:
        print(f"{r['url']}{chr(10)}  型式名={r['model_code']!r} 理由={r['reason']}")
    v = agree(rs)
    print(chr(10) + json.dumps(v, ensure_ascii=False, indent=1))
    return 0 if v["adopted"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(f"★想定外の失敗 {type(e).__name__}: {e}★")
        raise SystemExit(1)

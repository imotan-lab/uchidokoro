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

# 型式名が書かれている形（★見出しの次の行に値がある形もある★）
_LABELS = ("型式名", "型式")
# 型式名として認める形。★これ以外は採らない★（許可した形だけ通す）
#   英数字・記号・かな・漢字が混じる短い1行。文や説明を拾わない。
_CODE_OK = re.compile(r"^[0-9A-Za-zぁ-んァ-ヶ一-龥ー･・／/＋+\-−–—．.　 ]{2,40}$")
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
            if not _CODE_OK.match(cand):
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
    "機種情報", "新台情報",
}
_DECOR_CORES = {_ci.normalize_core(w) or w for w in _DECOR}

# ★題を区切る記号★（サイト側の飾りを切り離す）
#   ★「・」「-」は入れない★＝機種名そのものに使われる
#   （「すーぱぁびん娘・極」を割ると、別機種を本人にしてしまう）
_TITLE_SEPS = "|｜(（)）[［]］【】/／<＞>＜、,"


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


def page_is_machine(html: str, official_name: str,
                    extra_tail_ok: set | None = None):
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
    gen_conflict = False
    # ★題そのものも候補に入れる★（機種名の中に括弧が入ることがある）
    #   「甲鉄城のカバネリ 海門(うなと)決戦」は、区切ると名前が割れてしまう。
    # ★断片にしても「元の題でその前に何があったか」を持ち歩く★
    #   （2026-08-02・Codex25回目を再現して直した）
    #   断片ごとに独立して見ていたので、「別機種 | L北斗の拳」の後ろの断片が
    #   まっさらな前置として通っていた。前置の検査は元の題の全部に対して行う。
    # ★直後の断片も持ち歩く★（2026-08-02・Codex26回目を再現して直した）
    #   「Lすーぱぁびん娘（SP）」のように、派生機の印が括弧で
    #   区切られると誰も見ていなかった（25回目の「前」と逆向きの穴）。
    _segs = title_parts(title)
    cands = [(title, [], "")]
    _seen = []
    for _ix, s in enumerate(_segs):
        # ★後ろは「直後の1断片」ではなく残り全部★（2026-08-02・Codex28回目）
        #   「…|BELLCO|SP」は、直後のBELLCOを許した時点で
        #   SPを一度も見ずに通っていた。
        cands.append((s, list(_seen), " ".join(_segs[_ix + 1:])))
        _seen.extend(_ci.normalize_core(w) for w in s.split())
    for seg, before, after in cands:
        raw = seg.split()
        words = [_ci.normalize_core(w) for w in raw]
        for i in range(len(words)):
            joined = ""
            for j in range(i, len(words)):
                joined += words[j]
                if joined != core:
                    continue
                # ★次の語を見る★（飾りか、そこで終わりならOK）
                k = j + 1
                while k < len(words) and words[k] == "":
                    k += 1               # 販売区分語などは芯が空になる
                if k >= len(words) or words[k] in _DECOR_CORES:
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
                    #   ④本人の略称＝**本人の名前の文字だけでできている語**
                    #   （「青ブタ」は通る・「SP」「新章」「極」は本人の文字に無いので弾く）
                    # ★公式の照合では extra_tail_ok（社名・銘柄）を許す★
                    #   （2026-08-02・Codex27回目。検査を丸ごと外すと
                    #     派生機の公式URL「…（SP）|BELLCO」が通ってしまった）
                    if not _after_ok(after, core, official_name, extra_tail_ok):
                        gen_conflict = True
                        continue
                    return True, "OK"
    if gen_conflict:
        return False, "GEN_MARK_CONFLICT"
    return False, "NAME_CORE_MISMATCH"


def _after_ok(after_seg: str, core: str, official_name: str,
              extra: set | None = None) -> bool:
    """名前の直後の断片（括弧の中身など）が、本人のページとして自然か。

    extra: 呼び出し元が追加で許す語（メーカー公式の照合では社名・銘柄）。
      その語を**含む**尾部も許す（「株式会社サミー」等）。
    """
    if not after_seg:
        return True
    okc = set(core) | set(unicodedata.normalize("NFKC", official_name or "").lower())
    for w in after_seg.split():
        c = _ci.normalize_core(w)
        if c == "" or c in _DECOR_CORES or c in _maker_name_cores():
            continue
        # ★メーカー語は正規化後の完全一致だけ★（2026-08-02・Codex28回目）
        #   部分一致だと「SPBELLCO」のような合成語まで許してしまう。
        #   「株式会社サミー」は株式会社を外してから比べる。
        c2 = c.replace("株式会社", "")
        if extra and (c in extra or c2 in extra):
            continue                      # 期待するメーカーの社名・銘柄
        if set(c) <= okc:
            continue                      # 本人の名前の文字だけ＝略称・読み
        return False
    return True


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
    for tok in (str(conf.get("name") or ""), str(maker_id or "")):
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


def lookup(url: str, official_name: str) -> dict:
    """1つの名鑑ページから型式名を引く。★機種が違えば採らない★"""
    out = {"url": url, "official_name": official_name,
           "model_code": None, "reason": ""}
    try:
        html = _w._get(url)
    except Exception as e:
        out["reason"] = f"取得できません: {e}"
        return out
    ok, why = page_is_machine(html, official_name)
    if not ok:
        out["reason"] = why
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
        return re.sub(r"[\s　]+", "", unicodedata.normalize("NFKC", c))

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
        if len(hosts) >= 2:
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
    return {"model_code": None, "adopted": False, "state": "NOT_YET",
            "why": f"型式名が1つの名鑑にしか載っていません: {detail}"}


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []

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
      agree([{"url": "https://www.p-world.co.jp/x", "model_code": "Lびん娘NY1"},
             {"url": "https://p-town.dmm.com/y", "model_code": "Lびん娘NY1"}])["adopted"]
      is True)
    t("　1つだけでは採用しない",
      agree([{"url": "https://www.p-world.co.jp/x",
              "model_code": "Lびん娘NY1"}])["adopted"] is False)
    t("★同じサイトの2ページを2票と数えない★",
      agree([{"url": "https://www.p-world.co.jp/x", "model_code": "A1"},
             {"url": "https://p-world.co.jp/y", "model_code": "A1"}])["adopted"] is False)
    t("　食い違ったら採用しない（理由を残す）",
      agree([{"url": "https://www.p-world.co.jp/x", "model_code": "A1"},
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
      agree([{"url": "https://www.p-world.co.jp/x", "model_code": "Lびん娘NY1"},
             {"url": "https://p-town.dmm.com/y", "model_code": "Lびん娘 NY1"}])
      == {"model_code": "Lびん娘NY1", "hosts": ["p-town.dmm.com", "p-world.co.jp"],
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
    t("　直後の括弧が本人の略称なら通る（実データ・青ブタ）",
      page_is_machine(
          "<title>L青春ブタ野郎はバニーガール先輩の夢を見ない"
          "(スマスロ 青ブタ) パチスロ新台 | P-WORLD</title>",
          "L青春ブタ野郎はバニーガール先輩の夢を見ない")[0] is True)
    t("　タイトルが無ければ採らない",
      page_is_machine("<p>本文だけ</p>", "Lすーぱぁびん娘")[0] is False)

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

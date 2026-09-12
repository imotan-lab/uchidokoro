# -*- coding: utf-8 -*-
"""★狙い目の文を作る唯一の場所★（読むだけ・書かない）

★★なぜ要るか★★（2026-09-11・実測）
狙い目の文が**4か所**にあり、どれも手書きで、全部食い違っていた。

    machines.json の strategy            トップページの一覧
    machines.json の strategyByRate      機種ページ（交換率あり・28機種）
    machine-details の summaryBoxes      機種ページ（71ファイル・89箱）
    checker.<mode>.byRate.<率>.good      小役カウンターの判定

★実測★＝交換率の切替を持つ51機種のうち、
一覧と機種ページが一致している機種は**0**だった。
うち12機種は数値そのものが違い、3機種は
★文が「手前帯」の値で、カウンターは「まだ手前」と判定していた★。

★8月に「同じ構造化データから描く」と決めていた★（machine.html:825 のコメント）。
そのとき手書きを消さなかったので、以後ずっと分岐し続けた。
＝**この生成器を入れたら、手書きの側を消す**（残すと同じことが起きる）。

★★出す数値は good だけ★★
`machine.html` の判定は `good` で「目安に到達しています」と言う（897行）。
`caution` は2026-07-30の決定で「手前帯に入った」だけを表す（903行のコメント）。
＝**狙い目として caution を出さない**。

★★新しい数字は作らない★★
チェッカーが持っている値をそのまま指すだけ。
（`page_decision.derived_payout_range` と同じ考え方）

使い方:
  python scripts/target_display.py --all           # 下見（全機種）
  python scripts/target_display.py --slug hokuto   # 下見（1機種）
  python scripts/target_display.py --selftest
"""
from __future__ import annotations
import argparse
import io
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_S = os.path.join(BASE, "scripts")
for _p in (BASE, _S):
    if _p not in sys.path:
        sys.path.insert(0, _p)

MACHINES = os.path.join(BASE, "assets", "data", "machines.json")

# ★「天井なし・設定狙い」の印★＝データ側が前から使っている番兵の値。
#   実在の狙い目で最大は1500台なので、取り違えは起きない。
#   ★この値を狙い目として出さない★（「設定狙い99999G〜」になる）。
NO_TARGET = 99999

# ★サイトが交換率を指すときの言い方★
#   モードの呼び名にこれが入っていると、
#   「7.0枚 通常（5.6枚交換基準）760G〜」のような自己矛盾になる。
#   ★機械では直せない（呼び名の書き換えは意味の判断）ので2AIへ回す★
RATE_WORDS = ("等価", "現金", "5.6枚", "56枚", "6.0枚", "6.5枚", "7.0枚")


def mode_conf(ck: dict, key):
    """★モード設定は checker 直下と checker.modeData 配下の2系統ある★

    （CLAUDE.md「必ず共通アクセサ経由で読む」＝集計漏れ事故があった）
    """
    if isinstance(ck.get(key), dict):
        return ck[key]
    md = ck.get("modeData")
    if isinstance(md, dict) and isinstance(md.get(key), dict):
        return md[key]
    return None


def default_rate(ck: dict) -> str:
    """★読者が最初に見る交換率★（machine.html:786 と同じ選び方）"""
    rates = ck.get("exchangeRates")
    if not isinstance(rates, list) or not rates:
        return ""
    return str(ck.get("defaultRate") or rates[0].get("key") or "")


def rate_label(ck: dict, rk: str) -> str:
    for r in (ck.get("exchangeRates") or []):
        if isinstance(r, dict) and str(r.get("key")) == rk:
            return str(r.get("label") or "")
    return ""


def _int(v):
    """★整数だけ受け取る★（小数を許すと黙って切り捨てる）"""
    return v if type(v) is int else None


def effective_good(conf: dict, rk: str):
    """★その交換率で読者が見る狙い目★（machine.html:402 と同じ重ね方）

    `Object.assign({}, unit, unit.byRate[rateKey]).good`
    ★鍵が在れば、中身が null でも上書きする★（2026-09-11・Codexの指摘）＝
      JSの重ね書きは「鍵が在るか」で決まる。null で上書きされると
      `typeof good !== "number"` で**その行は出ない**。
      ★直す前は「読めない値なら土台へ戻る」だったので、
        JSは何も出さないのにこちらは土台の値を出していた★
      （いまのデータには該当が無いが、契約が食い違っていた）。
    """
    if not isinstance(conf, dict):
        return None
    br = conf.get("byRate")
    if isinstance(br, dict) and isinstance(br.get(rk), dict) \
            and "good" in br[rk]:
        return _int(br[rk]["good"])
    return _int(conf.get("good"))


def is_off(conf) -> bool:
    """★読者の画面に出ていない軸か★（2026-09-12・Codexの指摘6）

    ★なぜ外すか★＝`_disabled` が付いた軸は、Phase 0（2026-07-24）で
    「回数入力なのにG数判定だった」ため**画面から消してある**。
    ★実機で確かめた★＝モンハンライズ（停止中）はモードが normal だけで
    スルーを選べない／鉄拳6（停止していない）は normal・suru・reset が出る。
    ＝★一覧で「5スルー〜」と書いても、読者はその軸を使えない★。
    ★値そのものも #96 の二軸化で作り直す予定★なので、狙い目として出さない。
    """
    #   ★鍵が在るかで見る★（2026-09-12・Codexの指摘）＝
    #   公開側は鍵の有無で止めている。真偽で見ると、
    #   `"_disabled": ""` や `0` を書かれたときに食い違う。
    return isinstance(conf, dict) and "_disabled" in conf


def _rows(conf: dict):
    """回数系の行（スルー／周期）。無ければ (None, [])"""
    if not isinstance(conf, dict):
        return None, []
    for kind in ("suru", "cycle"):
        arr = conf.get(kind)
        if isinstance(arr, list) and arr:
            return kind, [r for r in arr if isinstance(r, dict)]
    return None, []


def row_ends(rows: list, rk: str):
    """★回数系は「両端」を出す★（2026-09-11・Codexの指摘1）

    ★直す前は「G数がいちばん小さい行」1つだけ★にしていた。
    ★それは代表値にならない★＝回数とG数は**2つの条件の組**なので、
    「3スルーで0G〜」は正しい1条件だが「2スルー100G〜」の代わりにはならない。
    しかも多くの機種で最後の行（0G）が選ばれ、要約が痩せていた。

    ★出すもの★＝回数がいちばん小さい行と、狙い目がいちばん浅い行。
    同じ行なら1つだけ。★どちらもデータに在る値★（新しい数字は作らない）。
    """
    got = []
    for r in rows:
        g = effective_good(r, rk)
        c = _int(r.get("count"))
        if g is None or c is None or g >= NO_TARGET:
            continue                      # ★番兵はここでも除く★
        got.append((c, g))
    if not got:
        return []
    first = min(got, key=lambda x: x[0])
    deep = min(got, key=lambda x: (x[1], x[0]))
    return [first] if first == deep else [first, deep]


def parts(ck: dict, rk: str) -> list:
    """★その交換率で読者に見せる狙い目の内訳★

    → [{"kind": ..., "text": "通常550G〜"}]
    ★出さないもの★＝番兵（設定狙い）／読めない値／回数系の途中の行。
    """
    out = []
    if not isinstance(ck, dict):
        return out
    unit = str(ck.get("unit") or "G")
    for md in (ck.get("modes") or []):
        if not isinstance(md, dict):
            continue
        conf = mode_conf(ck, md.get("key"))
        if not isinstance(conf, dict) or is_off(conf):
            continue                      # ★停止中の軸は出さない★
        label = str(md.get("label") or md.get("key") or "")
        kind, rows = _rows(conf)
        if rows:
            ends = row_ends(rows, rk)
            if not ends:
                continue
            word = "スルー" if kind == "suru" else "周期目"
            # ★モードの呼び名を落とさない★（2026-09-11・Codexの指摘1）＝
            #   落とすと、東京喰種の「AT間」もヴァルヴレイヴ2の「決戦」も
            #   消えて、どのモードの話か分からなくなる。
            body = "／".join("%d%s%d%s〜" % (c, word, g, unit)
                             for c, g in ends)
            out.append({"kind": "rows", "text": "%s %s" % (label, body)})
            continue
        g = effective_good(conf, rk)
        if g is None or g >= NO_TARGET:
            continue
        out.append({"kind": "mode", "text": "%s%d%s〜" % (label, g, unit)})
    # ★回数そのものがしきい値の形★
    #   （checker.suru / through / cycle が「行」ではなく good を直に持つとき）
    #   ★3つの名前がある★＝suru と through は同じ意味で、データに両方ある。
    #   ★行を持つものは上のモード側で出しているので、ここでは出さない★
    #     （でないと同じ機種で二重に出る）
    for key, word in (("suru", "スルー"), ("through", "スルー"),
                      ("cycle", "周期目")):
        ts = ck.get(key)
        if not isinstance(ts, dict) or is_off(ts):
            continue                      # ★停止中の軸は出さない★
        if any(isinstance(ts.get(k), list) for k in ("suru", "cycle")):
            continue
        g = effective_good(ts, rk)
        if g is None or g >= NO_TARGET:
            continue
        txt = "%d%s〜" % (g, word)
        if txt not in [o["text"] for o in out]:
            out.append({"kind": "count", "text": txt})
    return out


# ★★交換率の切替を持たない機種★★（2026-09-12・Codexと詰めた）
#   ★何が起きていたか★＝一覧が「等価730G〜 / 5.6枚740G〜 / 現金820G〜」と
#   書いているのに、チェッカーには交換率の区別が無く、値は1本だけ。
#   ★その1本が何の交換率なのかは復元できない★（実測20機種＝
#   現金7・等価7・5.6枚2・5.6枚と等価1・どれとも一致しない3）。
#   ＝「現金」とも「等価」とも名乗れない。
#   ★読者に起きていたこと★＝記事のほうが浅い機種では、
#   記事どおりに座るとチェッカーが「まだ手前」と言う
#   （バベル 730G / チェッカー900G ＝170G早い、など）。
#   ★呼び名★＝`チェッカー基準`（Codexの助言）。
#     「狙い目900G〜」とは書かない（全交換率に当てはまる値だと読める）。
NORATE_PREFIX = "チェッカー基準"


# ★★直したあとも検査を続ける機種★★（2026-09-12・Codexの重大指摘）
#   ★何が危なかったか★＝対象を「いまの一覧に交換率の呼び名があるか」で
#   決めていたので、★直した瞬間に呼び名が消えて対象から外れ★、
#   以後どれだけ数値が食い違っても専用の検査が動かなかった。
#   ＝**直しが成功した瞬間に、守りが消える**形だった。
#   ★だから名簿をコードに固定する★（いまの一覧の中身と無関係に毎回見る）。
NORATE_TARGETS = (
    "sengoku_collection6", "milliongod_kiseki", "ultraman_final",
    "nangoku_special", "yabachiba", "rotis", "biohazard_re3",
    "bigdream_pusher", "super_rio_ace2", "gundam_uc2", "kyokousuiri",
    "animal_dotch", "yorumungando", "akudama", "gineiden_dnt",
    "tolove_darkness", "revue_starlight", "zenigata5", "enen", "railgun2",
    "gundam_seed", "youjitsu", "midoridon_viva", "code_geass",
    "kengan_ashura", "goji_eva", "basilisk_tenzen", "madomagi_forte",
    "burning_express", "lupin_daikokaisha", "zombieland_saga",
    "zettai_shougeki4", "babel", "takt_opus",
)

# ★★基準を名乗っているので触らない★★（2026-09-12・2AIで決めた）
#   ★この2機種は誤りではない★＝どの交換率の値かを**自分で名乗り**、
#   ほかの交換率は「未確定」と正直に書いている。
#   ＝今回の目的（読者に、サイトが持っていない区別を見せない）に反しない。
#   ★「チェッカー基準」へ書き換えると、かえって名乗っている基準が消える★。
#   ★名簿に載せるだけでは足りない★（Codexの指摘）＝
#   slug が表にあるだけで無条件に外れると、
#   ★あとから「未確定」の断り書きを消しても誰も気づかない★。
#   だから★一覧に残っていなければならない文言★も一緒に持つ。
NORATE_KEEP = {
    "karakuri2": {
        "why": "一覧が「等価 液晶800G〜（当サイト目安）」と基準を名乗り、"
               "5.6枚・現金は「個別ライン未確定」と書いてある",
        "must": ("当サイト目安", "個別ライン未確定"),
    },
    "enen2": {
        "why": "一覧が「5.6枚交換 630G〜」と基準を名乗り、"
               "他交換率は「算定条件を確認中」と書いてある",
        "must": ("5.6枚交換", "算定条件を確認中"),
    },
}


def keep_problems(m: dict) -> list:
    """★外してある機種が、外してよい姿のままか★"""
    slug = str(m.get("slug") or "")
    spec = NORATE_KEEP.get(slug)
    if not spec:
        return []
    strat = str(m.get("strategy") or "")
    miss = [w for w in spec["must"] if w not in strat]
    if miss:
        return ["%s: 外してある前提の文言が消えています（%s）"
                % (slug, "・".join(miss))]
    return []


def _norate_target(m: dict) -> bool:
    """★直す対象か★＝一覧が交換率を名乗っているのに、チェッカーに区別が無い機種

    ★名乗っていない機種は放っておく★＝食い違いが見えていないので、
    今回の目的（読者に嘘の区別を見せない）から外れる。
    """
    ck = m.get("checker")
    if not isinstance(ck, dict) or ck.get("exchangeRates"):
        return False
    slug = str(m.get("slug") or "")
    if slug in NORATE_KEEP:
        return False                      # ★理由つきで外してある★
    # ★名簿に載っていれば、いまの一覧の中身に関係なく対象★
    #   （直したあとも毎回照合し続けるため）
    return slug in NORATE_TARGETS


def norate_unclassified(ms) -> list:
    """★名簿に載っていない新しい候補を見つける★（対象の決定とは分ける）

    ★分ける理由★＝対象を「いまの一覧の中身」で決めると、直した瞬間に
    外れて検査が止まる。名簿は固定し、こちらは「増えていないか」だけ見る。
    """
    out = []
    for m in ms:
        ck = m.get("checker")
        if not isinstance(ck, dict) or ck.get("exchangeRates"):
            continue
        slug = str(m.get("slug") or "")
        if slug in NORATE_TARGETS or slug in NORATE_KEEP:
            continue
        if any(w in str(m.get("strategy") or "") for w in RATE_WORDS):
            out.append(slug)
    return out


def norate_text(m: dict) -> tuple:
    """切替を持たない機種の一覧の文を作る → (文, 駄目な理由)

    ★作れるかどうかは「全部そろったか」で決める★（Codexの指摘3）＝
    読めない値や番兵を飛ばして残りだけで作ると、
    ★一部しか出ていない一覧を「直した」ことにしてしまう★。
    """
    ck = m.get("checker")
    if not isinstance(ck, dict) or ck.get("exchangeRates"):
        return "", "交換率の切替を持つ機種です"
    # ★モードの呼び名に交換率が入っていたら作らない★＝
    #   「チェッカー基準 通常（等価基準）800G〜」という自己矛盾になる（実測2機種）
    bad = problems(m)
    if bad:
        return "", " / ".join(bad)
    want, got = expected_axes(ck), parts(ck, "")
    if not want:
        return "", "チェッカーに狙い目の軸がありません"
    if len(got) != len(want):
        return "", ("作れた軸が足りません（%d / %d）" % (len(got), len(want)))
    return "%s %s" % (NORATE_PREFIX,
                      " / ".join(p["text"] for p in got)), ""


def expected_axes(ck: dict) -> list:
    """★出るはずの軸を数える★（あとで「全部出たか」を見るため）

    ★直す前は、出す側とまったく同じ絞り込みをしていた★ので、
    ★読めない値があっても「期待も出力も1つ」で必ず一致し、
      条件（全部そろったか）が一度も効いていなかった★
    （2026-09-12・Codexの指摘3を実装したつもりで、していなかった）。

    ★いまの数え方★＝軸を3つに分ける。
      出す     … 読める値があり、番兵でない
      数えない … 番兵（設定狙いの99999）／停止中（画面に出ていない）
      ★読めない★ … 値が在るのに整数として読めない → ここが1つでもあれば
                    その機種は触らない（呼ぶ側が len で判定する）
    """
    out = []
    for md in (ck.get("modes") or []):
        if not isinstance(md, dict):
            continue
        conf = mode_conf(ck, md.get("key"))
        if not isinstance(conf, dict) or is_off(conf):
            continue                      # 停止中は数えない
        kind, rows = _rows(conf)
        if rows:
            # ★★行が1つでも読めなければ、その機種は触らない★★
            #   （2026-09-12・Codexの指摘）＝有効な行が1件でもあれば
            #   軸を1つと数えていたので、★別の行が読めなくても件数が一致★し、
            #   部分的にしか出ていない一覧を「直した」ことにできた。
            bad = [r for r in rows
                   if _int(r.get("count")) is None
                   or effective_good(r, "") is None]
            if bad:
                out.append(("★読めない行★", md.get("key")))
            if row_ends(rows, ""):
                out.append(("rows", md.get("key")))
            continue
        raw = conf.get("good")
        if raw is None:
            continue                      # そもそも狙い目を持たない軸
        g = effective_good(conf, "")
        if g is not None and g >= NO_TARGET:
            continue                      # 番兵（設定狙い）
        out.append(("mode", md.get("key")))   # ★読めなくても数える★
    seen = set()
    for key, word in (("suru", "スルー"), ("through", "スルー"),
                      ("cycle", "周期目")):
        ts = ck.get(key)
        if not isinstance(ts, dict) or is_off(ts):
            continue
        if any(isinstance(ts.get(k), list) for k in ("suru", "cycle")):
            continue
        if ts.get("good") is None:
            continue
        g = effective_good(ts, "")
        if g is not None and g >= NO_TARGET:
            continue
        txt = "%d%s〜" % (g, word) if g is not None else ("?" + key)
        if txt in seen:
            continue                      # suru と through は同じ意味
        seen.add(txt)
        out.append(("count", key))
    return out


def problems(m: dict) -> list:
    """★機械では決められないもの★（2AIへ回す）"""
    bad = []
    ck = m.get("checker")
    if not isinstance(ck, dict):
        return bad
    for md in (ck.get("modes") or []):
        if not isinstance(md, dict):
            continue
        label = str(md.get("label") or "")
        hit = [w for w in RATE_WORDS if w in label]
        if hit:
            bad.append("モードの呼び名に交換率が入っている: %s" % label)
    return bad


def for_machine(m: dict) -> dict:
    """1機種ぶんの生成結果

    ★2つの文字列を出す理由★＝出す場所で名乗りの要否が違う。
      一覧（index.html）  … 交換率を選べないので**呼び名が要る**
      機種ページの箱      … 箱の見出しが「5.6枚狙い目」なので**要らない**
    ★計算する場所は1つ★＝どちらも同じ `parts()` から作る。
    """
    ck = m.get("checker")
    if not isinstance(ck, dict):
        return {}
    rates = ck.get("exchangeRates")
    keys = ([str(r.get("key")) for r in rates if isinstance(r, dict)]
            if isinstance(rates, list) else [])
    if not keys:
        return {}
    out = {"defaultRate": default_rate(ck), "byRate": {}, "plain": {},
           "problems": problems(m)}
    for rk in keys:
        ps = [p["text"] for p in parts(ck, rk)]
        plain = " / ".join(ps)
        out["plain"][rk] = plain
        lab = rate_label(ck, rk)
        out["byRate"][rk] = ("%s %s" % (lab, plain)).strip() if plain else ""
    return out


# ────────────────────────────────────────────────────────────
# ★ここから下は「書く側」★
# ────────────────────────────────────────────────────────────

DETAILS = os.path.join(BASE, "assets", "data", "machine-details")


# ★★狙い目の表示に属する箱の対応表★★（2026-09-11・2AIで1機種ずつ決めた）
#
#   ★なぜ表なのか★＝見出しとモードの結び付きを機械に推測させると誤る。
#   実例＝箱の見出し「通常時」とモードの呼び名「通常」は**別の文字**。
#   文字を正規化する規則を足していくと、罠（例外リストの型）に落ちる。
#   ★だから「人が1機種ずつ見て決めた」ことを、そのまま表にする★。
#
#   ★ここに挙げた見出しの箱は、生成した狙い目の箱1つへまとめる★。
#   ★挙げていない箱（天井・ヤメ時・純増・機械割など）は1つも触らない★。
#
#   ★なぜまとめてよいか★＝挙げた箱の中身は、
#   「様子見＝caution」「狙い目＝good」「強め＝excellent」で、
#   ★どれもチェッカーが持っていて、狙い目早見表が交換率ごとに出している★。
#   ＝読者は同じ数値を、正しい交換率で、表から読める。
#   ★実測で数値がズレていた★（例＝ゴブリンスレイヤーの箱は680G、
#   チェッカーは690G／バキの箱は340G、チェッカーは300G）。
BOX_PLAN = {
    "tokyo_ghoul": ["AT間 0スルー", "AT間 1スルー", "AT間 2スルー以上",
                    "リセット"],
    "kabaneri": ["通常時", "通常時 強め", "リセット"],
    "monkeyv": ["通常時", "通常時 強め", "リセット", "周期目安"],
    "valvrave2": ["決戦スルー", "決戦スルー 続き", "リセット"],
    "hokuto_tensei2": ["通常時", "通常時 強め", "リセット"],
    "koukaku": ["CZ間", "AT間", "リセット", "強めライン"],
    "tekken6": ["通常時", "1スルー", "2スルー", "3スルー", "リセット"],
    "kaguya": ["BIG後", "REG後", "スルー回数", "スルー回数 続き", "リセット"],
    "godeater": ["通常時", "通常時 強め", "駆け抜け後", "リセット"],
    "chibaryo2": ["通常時", "通常時 強め", "リセット", "リセット 強め"],
    "goblin": ["通常時", "通常時 強め", "リセット", "リセット 強め"],
    "banchou4": ["通常時", "1スルー", "2スルー", "リセット", "通常時 強め"],
    "biohazard": ["通常時", "通常時 強め", "リセット"],
    "dumbbell": ["CZ間", "スルー 0から2回", "スルー 3から5回", "リセット",
                 "CZ間 強め"],
    "tensura": ["通常時", "通常時 強め", "リセット"],
    "baki": ["CZ間", "スルー 0回", "スルー 1から2回", "スルー 3回", "リセット"],
    # ★狙い目の箱がもともと無い機種★＝生成した箱を1つ足す
    "neoplanet": [],
    "bofuri": [],
    "tenken": [],
    "valvrave": [],
}

# ★★まとめると失われる言葉を、先に別の箱へ移す★★（2026-09-11）
#   ★逐語で移すだけ★＝書き換えない・足さない。
#   ★なぜ要るか★＝この3つだけは、チェッカーにも早見表にも無い事実。
#   （slug, どの箱から, 移す一文, どの箱へ）
#   ★★移すのは1件だけ★★＝ほかの2件は記事本文に同じ文が既にあった
#     （モンキーターンV「5周期目以降が本命で、6周期目はAT確定です。」／
#       鉄拳6「3スルーは次回ボーナスでAT確定です。」）。
#     ★消す前に、その事実がほかに在るかを必ず確かめる★
#     （確かめずに移すと、同じ文が2か所に増える）。
BOX_MOVES = (
    ("valvrave2", "リセット", "周期天井3周期に短縮", "周期天井"),
)

# ★★一回限りの移行は終わった（2026-09-12）★★
#   印の無い箱に残っていた狙い目を落とす表（STRAY_PLAN）は**外した**。
#   ★なぜ外すか★（Codexの指摘）＝表を残すと、同じ文がもう一度入ったときに
#   ★黙って消してしまう★。「当てはまったら止めて2AIへ回す」と食い違う。
#   ★何を落としたかはコミット 1253c792 と、その次のコミットに残っている★
#   （かばねり／ヴァルヴレイヴ2／北斗転生2／SAO／範馬刃牙／沖ドキGOLD・BLACK／
#     プリズムナナ の計9箇所）。

# ★★印の無い箱は、狙い目を言ってはいけない★★
#   ★なぜ言葉で見るのか★＝「どの箱が狙い目を言っているか」は
#   サイト自身が使う言い方なので、外の世界の意味ではない。
#   機械が作る箱はこの言い方を値に使わない（見出しにだけ「狙い目」が入る）。
#   ★当てはまったら止めて2AIへ回す★（勝手に消さない）。
#   ★言葉を広げた★（2026-09-12・Codexの指摘）＝
#   「狙い目」だけを見ていたので、★サイトで実際に使っている言い換えが素通り★した
#   （「0Gから狙える」「即打ち」「打ち始め」。machines.json で狙え=22回・即打ち=18回）。
#   ★裸の開始値も見る★＝「50G〜」のように語を一つも使わない形が2箱あった。
CLAIM_WORDS = ("狙", "強め", "候補", "様子見", "着席",
               "即打ち", "打ち始め", "打ち出し", "から打", "打てる")
# ★数値＋単位＋「〜」＝それだけで狙い目の宣言★
START_VALUE = re.compile(r"\d+\s*(?:G|pt|枚|あべし|周期|スルー|回|個)\s*[〜~]")
_HAS_NUM = re.compile(r"\d").search


def stray_claims(sb) -> list:
    """印の無い箱が狙い目を言っていないか"""
    bad = []
    for b in sb or []:
        if not isinstance(b, dict) or b.get("role") == BOX_ROLE:
            continue
        lab = str(b.get("label") or "")
        val = str(b.get("value") or "")
        if "狙い" in lab:
            bad.append("印の無い箱の見出しが狙い目を名乗っています: %s" % lab)
            continue
        # ★★言葉だけでは広すぎる★★（2026-09-12・実測10機種）＝
        #   「基本方針：天井狙い＋リセット狙い」は**やり方の説明**で、
        #   狙い目の宣言ではない。★数値を伴うときだけ宣言とみなす★。
        hit = [w for w in CLAIM_WORDS if w in val] if _HAS_NUM(val) else []
        if hit:
            bad.append("印の無い箱が狙い目を言っています: %s（%s）"
                       % (lab, "・".join(hit)))
            continue
        if START_VALUE.search(val):
            bad.append("印の無い箱に裸の開始値があります: %s（%s）"
                       % (lab, START_VALUE.search(val).group(0)))
    return bad


# ★狙い目の箱を置く場所★＝まとめる箱のうち、いちばん先頭にあったところ。
#   もともと無い機種は、天井の箱の次（無ければ先頭）。


def _indent_of(path: str) -> int:
    """★元のファイルと同じ字下げで書く★（2026-09-11・自分で踏んだ）

    ★何をやらかしたか★＝`indent=2` で書いたら、
    ★機種一覧が丸ごと組み直され、2万4千行の差分になった★。
    ＝変えたのは51機種の2つの項目なのに、全部が変わったように見える。
    ★実害★＝新台の復旧が「行の字面ではなく中身で判断する」試験で落ちた。
    ★読む側にも意味がある★＝差分が大きいと、人もCodexもレビューできない。
    """
    try:
        with io.open(path, encoding="utf-8") as f:
            f.readline()                      # 1行目（[ か {）
            line = f.readline()
    except OSError:
        return 1
    n = len(line) - len(line.lstrip(" "))
    return n if n > 0 else 1


def _dump(obj, path: str) -> str:
    return json.dumps(obj, ensure_ascii=False,
                      indent=_indent_of(path)) + "\n"


def _digest(path: str) -> str:
    """★読んでから書くまでに中身が変わっていないか★"""
    import hashlib
    with io.open(path, "rb") as f:
        return hashlib.sha256(f.read().replace(b"\r\n", b"\n")).hexdigest()


# ★生成した箱に付ける印★（2026-09-11・Codexの指摘2）
#   ★直す前は「見出しに『狙い目』と書いてあるか」で持ち主を決めていた★。
#   ＝生成したあとに見出しを「通常時」へ変えて手書きの値を入れると、
#     次の点検は**その箱を対象外として通す**（罠㉑＝「無いこと」は
#     負の検査では守れない）。
#   ★印で決めれば、見出しを変えても持ち主は変わらない★。
BOX_ROLE = "target"


def _target_boxes(sb):
    """狙い目の箱の位置を返す

    ★印が1つでも在れば、印だけで決める★（移行が済んだ機種）。
    ★印が無い機種だけ、見出しの文字で拾う★（最初の1回の移行のため）。
    ★天井の箱は狙い目ではない★
    """
    roled = [i for i, b in enumerate(sb or [])
             if isinstance(b, dict) and b.get("role") == BOX_ROLE]
    if roled:
        return roled
    out = []
    for i, b in enumerate(sb or []):
        if not isinstance(b, dict):
            continue
        lab = str(b.get("label") or "")
        if "狙い目" in lab and "天井" not in lab:
            out.append(i)
    return out


def plan_all():
    """★書く前の下書きを全機種ぶん作る★（1件でも駄目なら何も書かない）

    ★手をつける範囲★（2026-09-11）
      ①一覧（strategy）と機種ページの狙い目文（strategyByRate）
        …交換率を持つ機種だけ
      ②見出しに「狙い目」と書いてある箱
        …中身を作ったものへそろえ、★2つ目以降は消す★
          （画面の切り替えは1つ目しか直さないので、2つ目は古いまま残る）

    ★手をつけないもの★＝見出しがモード名の箱（「通常時」「CZ間」「1スルー」）。
      見出しとモードの結び付けが文字で一致しないので、機械では決められない。
      ★2AIへ回す★（実測＝交換率ありで18機種）。
    """
    ms = _load()
    machines, details, skipped = [], [], []
    for m in ms:
        got = for_machine(m)
        if not got:
            # ★★交換率の切替を持たない機種★★（2026-09-12）
            #   ★書くのは一覧（strategy）だけ★（Codexの指摘2）＝
            #   交換率ごとの文も、記事データの箱も作らない・触らない。
            # ★★対象は「一覧が交換率を名乗っている機種」だけ★★
            #   （2026-09-12・Codexの指摘1＝対象を先に固定する）
            #   ★名乗っていない機種まで書き換えない★＝
            #   実測3機種（アズールレーン・ゴジラ・リゼロ2）は
            #   嘘の区別を出しておらず、書き換えると
            #   「単発後210G〜」のような別の情報が落ちる。
            if not _norate_target(m):
                continue
            _t, _why = norate_text(m)
            if _t:
                if str(m.get("strategy") or "") != _t:
                    machines.append({"slug": str(m.get("slug") or ""),
                                     "strategy": _t,
                                     "strategyByRate": None})   # ★作らない★
            elif _norate_target(m):
                skipped.append((str(m.get("slug") or ""), _why))
            continue
        slug = str(m.get("slug") or "")
        dr = got["defaultRate"]
        want_strategy = got["byRate"].get(dr) or ""
        if not want_strategy:
            skipped.append((slug, "狙い目を1つも作れない"))
            continue
        if got["problems"]:
            skipped.append((slug, " / ".join(got["problems"])))
            continue
        if (m.get("strategy") != want_strategy
                or m.get("strategyByRate") != got["plain"]):
            machines.append({"slug": slug,
                             "strategy": want_strategy,
                             "strategyByRate": dict(got["plain"])})
        # ── 記事データ側の箱 ──
        p = os.path.join(DETAILS, slug + ".json")
        if not os.path.exists(p):
            continue
        import safe_json as _sj
        o = _sj.read_json(p, expect=dict)
        sb = o.get("summaryBoxes")
        if not isinstance(sb, list):
            # ★「残り」を数えない★（罠㊽）＝黙って飛ばすと
            #   「決められないのは18機種」と答えてしまう（本当は20）。
            if slug not in BOX_PLAN:
                skipped.append((slug, "要約の箱そのものが無い（2AIへ）"))
                continue
            sb = []
        new_label = "%s狙い目" % rate_label(m["checker"], dr)
        new_value = got["plain"].get(dr) or ""
        new_sb, why = _rebuild_boxes(slug, sb, new_label, new_value)
        if why:
            skipped.append((slug, why))
            continue
        if new_sb != sb:
            details.append({"slug": slug, "path": p, "summaryBoxes": new_sb,
                            "before": [(str(x.get("label")), str(x.get("value")))
                                       for x in sb if isinstance(x, dict)],
                            "after": [(str(x.get("label")), str(x.get("value")))
                                      for x in new_sb if isinstance(x, dict)]})
    return machines, details, skipped


def _find_label(sb, label):
    for i, b in enumerate(sb):
        if isinstance(b, dict) and str(b.get("label") or "") == label:
            return i
    return -1


def _rebuild_boxes(slug, sb, new_label, new_value):
    """★狙い目の箱を1つにまとめる★ → (新しい箱の並び, 駄目な理由)

    ★1つでも表と食い違ったら、その機種は何も書かない★（fail-closed）。
    """
    sb = [b for b in sb if isinstance(b, dict)]
    idx = _target_boxes(sb)
    plan = BOX_PLAN.get(slug)
    if not idx and plan is None:
        return None, "狙い目の箱が無く、対応表にも載っていない（2AIへ）"
    # ★★印が在るときに、印の無い狙い目の箱が足されていたら止める★★
    #   （2026-09-11・Codexの指摘3）＝`_target_boxes` は印が1つでも在れば
    #   ★印だけを見て、それ以外を一切見ない★。
    #   ＝生成した箱の隣に「通常時: 手書き600G〜」を足しても点検が緑になる。
    roled = [i for i, b in enumerate(sb) if b.get("role") == BOX_ROLE]
    if roled:
        if len(roled) != 1:
            return None, "狙い目の箱の印が %d 個あります" % len(roled)
        left = [str(b.get("label") or "") for b in sb
                if b.get("role") != BOX_ROLE
                and str(b.get("label") or "") in (plan or [])]
        if left:
            return None, "手書きの狙い目の箱が残っています: " + " / ".join(left)
        # ★見出しで拾う箱も残っていないこと★
        strays = [str(b.get("label") or "") for b in sb
                  if b.get("role") != BOX_ROLE
                  and "狙い目" in str(b.get("label") or "")
                  and "天井" not in str(b.get("label") or "")]
        if strays:
            return None, "印の無い狙い目の箱が残っています: " + " / ".join(strays)
    # ── 先に「失われる言葉」を移す ──
    moves = [mv for mv in BOX_MOVES if mv[0] == slug]
    out = [dict(b) for b in sb]
    for _sg, src, text, dst in moves:
        di = _find_label(out, dst)
        if di < 0:
            return None, "移す先の箱が見つからない: %s" % dst
        dv = str(out[di].get("value") or "")
        # ★★もう移してあるなら、それで終わり★★（2026-09-11・自分で踏んだ）＝
        #   移した元の箱は、このあと狙い目の箱へまとめられて消える。
        #   ★2回目に「元が見つからない」と言って断っていた★
        #   ＝1回書いたら二度と点検が通らない（冪等でない）。
        if text in dv:
            continue
        si = _find_label(out, src)
        if si < 0 or text not in str(out[si].get("value") or ""):
            return None, "移す一文が見つからない: %s" % text
        out[di] = dict(out[di])
        out[di]["value"] = (dv + "<br>" + text) if dv else text
    # ── まとめる箱を決める ──
    if idx:
        drop = list(idx)
    else:
        drop = []
        for lab in plan:
            i = _find_label(out, lab)
            if i < 0:
                return None, "対応表の箱が見つからない: %s" % lab
            drop.append(i)
        drop.sort()
    if drop:
        keep = drop[0]
    else:
        # ★もともと狙い目の箱が無い機種★＝天井の次へ置く
        keep = None
    new = []
    placed = False
    for i, b in enumerate(out):
        if keep is not None and i == keep:
            nb = dict(b)
            nb["label"] = new_label
            nb["value"] = new_value
            nb["role"] = BOX_ROLE
            new.append(nb)
            placed = True
            continue
        if keep is not None and i in drop:
            continue
        new.append(b)
    if not placed:
        nb = {"label": new_label, "value": new_value, "role": BOX_ROLE}
        at = 0
        for i, b in enumerate(new):
            if "天井" in str(b.get("label") or ""):
                at = i + 1
        new.insert(at, nb)
    # ★★印の無い箱が狙い目を言っていたら止める★★（fail-closed）
    stray = stray_claims(new)
    if stray:
        return None, " ／ ".join(stray)
    return new, ""


TEMPLATE = os.path.join(BASE, "machine.html")

# ★★ひな型が守るべきこと★★（2026-09-11・Codexの指摘2）
#   ★なぜ要るか★＝push前の点検は「作った文とデータが一致するか」しか見ない。
#   ＝ひな型（画面を描く側）を古い形へ戻しても、データが一致していれば緑だった。
#   ★限界を正直に書く★＝これは**文字が在るか**の検査で、
#   「本当に動くか」を示すものではない。動く証拠は R16（画面の検査）。
#   ここは「気づかずに戻す」を止めるための綱。
TEMPLATE_MUST = (
    # 「鍵が無い」と「中身が空」を分ける（||に戻すと古い予備生成器へ落ちる）
    ("hasOwnProperty.call(_sbr, rateKey)",
     "交換率ごとの文の有無を hasOwnProperty で見ていません"),
    # 狙い目の箱は所有権の印で探す
    ('box && box.role === "target"',
     "狙い目の箱を所有権の印で探していません"),
    # 画面にも印を出す（検査が数えられるように）
    ('x && x.role ? ` data-role="${x.role}"` : ""',
     "画面に所有権の印を出していません"),
    # リセットの箱は天井を巻き込まない
    ('lb.includes("リセット") && !lb.includes("天井")',
     "リセットの箱が天井の箱を巻き込みます"),
)
TEMPLATE_MUST_NOT = (
    ("(_sbr && _sbr[rateKey]) || buildTargetFromChecker",
     "交換率ごとの文が空のとき、古い予備生成器へ落ちます"),
)


def template_problems(src: str | None = None) -> list:
    """ひな型が契約どおりかを見る（読むだけ）"""
    if src is None:
        try:
            src = io.open(TEMPLATE, encoding="utf-8").read()
        except OSError as e:
            return ["ひな型を読めません: %s" % type(e).__name__]
    bad = []
    for needle, why in TEMPLATE_MUST:
        if needle not in src:
            bad.append(why)
    for needle, why in TEMPLATE_MUST_NOT:
        if needle in src:
            bad.append(why)
    return bad


def check() -> int:
    """★作り直したら中身が変わるかだけ見る（書かない）★

    ★決められないものが1つでも在れば止める★（2026-09-11・Codexの指摘2）＝
      直す前は `skipped` を報せるだけで終了コードは0だった。
      ＝★生成できない機種が増えても、関所は静かに通す★（fail-open）。
    """
    tpl = template_problems()
    if tpl:
        print("★ひな型（機種ページを描く側）が契約から外れています★")
        for x in tpl:
            print("   " + x)
        print()
    machines, details, skipped = plan_all()
    # ★★切替なし機種の継続検査★★（2026-09-12・Codexの重大指摘）
    #   ★直した瞬間に対象から外れて検査が止まる形だった★ので、
    #   固定名簿（NORATE_TARGETS）を毎回見る。
    _ms = _load()
    _by = {str(m.get("slug")): m for m in _ms}
    nr = []
    for _slug in NORATE_TARGETS:
        _m = _by.get(_slug)
        if _m is None:
            nr.append("%s: 機種が見つかりません（名簿と食い違い）" % _slug)
            continue
        # ①作った文と一致しているか（数値が動いたら気づく）
        _t, _why = norate_text(_m)
        if not _t:
            nr.append("%s: 狙い目の文を作れません（%s）" % (_slug, _why))
        elif str(_m.get("strategy") or "") != _t:
            nr.append("%s: 一覧が作った文と違います" % _slug)
        # ②呼び名が戻っていないか
        if any(w in str(_m.get("strategy") or "") for w in RATE_WORDS):
            nr.append("%s: 一覧に交換率の呼び名が戻っています" % _slug)
        # ③記事データが在るか（無いと機種ページの天井欄が一覧で埋まる）
        if not os.path.isfile(os.path.join(DETAILS, _slug + ".json")):
            nr.append("%s: 記事データがありません"
                      "（機種ページの天井欄が一覧で埋まります）" % _slug)
    # ④外してある機種が、外してよい姿のままか
    for _slug in NORATE_KEEP:
        _m = _by.get(_slug)
        if _m is None:
            nr.append("%s: 機種が見つかりません（外す表と食い違い）" % _slug)
            continue
        nr += keep_problems(_m)
    # ⑤名簿に載っていない新しい候補
    _new = norate_unclassified(_ms)
    if _new:
        nr.append("名簿に無い候補があります: " + " / ".join(_new[:6]))
    if nr:
        print("★切替なし機種の検査: %d件★" % len(nr))
        for x in nr[:10]:
            print("   " + x)
        print()
    if skipped:
        print("★機械では決められない機種があります: %d★" % len(skipped))
        for slug, why in skipped:
            print("   %-22s %s" % (slug, why))
        print()
    if not machines and not details and not skipped and not tpl and not nr:
        print("★一致しています（狙い目の文は作ったものと同じ）★")
        return 0
    if (tpl or nr) and not machines and not details and not skipped:
        return 1
    if machines or details:
        print("★食い違っています★")
    for r in machines:
        print("  一覧 : %s" % r["slug"])
    for r in details:
        print("  箱   : %s" % r["slug"])
    print()
    print("直す: python scripts/target_display.py --apply")
    return 1


def apply_all() -> int:
    machines, details, skipped = plan_all()
    # ★★決められない機種が1つでも在れば、1文字も書かない★★
    #   （2026-09-11・Codexの指摘1）＝直す前は点検だけ fail-closed で、
    #   ★書く側は素通り★だった。しかも同じ機種が
    #   「一覧は直せる／箱は決められない」になり得るので、
    #   ★一覧だけ書き換える部分適用★が起きる。
    if skipped:
        print("★機械では決められない機種があるので、何も書きません: %d★"
              % len(skipped))
        for slug, why in skipped:
            print("   %-22s %s" % (slug, why))
        return 1
    if not machines and not details:
        print("変えるものはありません")
        return 0
    import safe_json as _sj
    d0 = _digest(MACHINES)
    data = _sj.read_json(MACHINES, expect=(dict, list))
    rows = data if isinstance(data, list) else (data.get("machines") or [])
    by = {str(x.get("slug")): x for x in rows}
    for r in machines:
        m = by.get(r["slug"])
        if m is None:
            print("★機種が見つかりません:", r["slug"])
            return 1
        m["strategy"] = r["strategy"]
        # ★★None は「触らない」の意味★★（切替なし機種）＝
        #   交換率ごとの文は作らないし、あれば残す（消しもしない）。
        if r["strategyByRate"] is not None:
            m["strategyByRate"] = r["strategyByRate"]
    # ★全部そろってから書く★（途中で止まっても部分適用にしない）
    outs = [(MACHINES, _dump(data, MACHINES), d0)]
    for r in details:
        o = _sj.read_json(r["path"], expect=dict)
        o["summaryBoxes"] = r["summaryBoxes"]
        outs.append((r["path"], _dump(o, r["path"]), _digest(r["path"])))
    # ★★書き方★★（2026-09-11・Codexの指摘2）
    #   直す前は、照合したあと**本物のファイルを順に切り詰めて書いて**いた。
    #   ＝途中で止まると、壊れたJSONが残る／半分だけ直った状態になる。
    #   ★一時ファイルへ全部書き切ってから、置き換える★。
    #   ★置き換えそのものは1ファイルずつ★＝複数ファイルを本当に
    #     まとめて置き換える方法は無い。★代わりに、もう一度流せば
    #     同じところへ収束する（冪等）★ことを試験にしている。
    tmps = []
    try:
        for path, text, dg in outs:
            if _digest(path) != dg:
                print("★読んでから書くまでに中身が変わりました:", path)
                return 1
            tmp = path + ".tmp-target"
            io.open(tmp, "w", encoding="utf-8", newline="\n").write(text)
            tmps.append((tmp, path))
        for tmp, path in tmps:
            os.replace(tmp, path)
        tmps = []
    finally:
        for tmp, _p in tmps:
            try:
                os.remove(tmp)
            except OSError:
                pass
    print("一覧を直した機種: %d" % len(machines))
    print("箱を直した機種  : %d" % len(details))
    if skipped:
        print()
        print("★2AIへ回すもの: %d 機種★" % len(skipped))
        for slug, why in skipped:
            print("   %-22s %s" % (slug, why))
    return 0


def _load():
    import safe_json as _sj
    d = _sj.read_json(MACHINES, expect=(dict, list))
    return d if isinstance(d, list) else (d.get("machines") or [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--check", action="store_true",
                    help="作り直したら中身が変わるかだけ見る（書かない）")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.check:
        return check()
    if a.apply:
        return apply_all()
    ms = _load()
    if a.slug:
        ms = [m for m in ms if m.get("slug") == a.slug]
        if not ms:
            print("そんな機種はありません:", a.slug)
            return 1
    elif not a.all:
        ap.print_help()
        return 1
    n = 0
    for m in ms:
        got = for_machine(m)
        if not got:
            continue
        n += 1
        print("== %s" % m.get("slug"))
        print("   いまの一覧 : %s" % (m.get("strategy") or ""))
        dr = got["defaultRate"]
        print("   作った一覧 : %s" % got["byRate"].get(dr, ""))
        for rk, t in got["byRate"].items():
            if rk != dr:
                print("       %-8s %s" % (rk, t))
        for p in got["problems"]:
            print("   ★2AIへ: %s" % p)
    print()
    print("交換率を持つ機種: %d" % n)
    return 0


def _real_file_tests(t):
    """★偽物だけで固めない★（罠㉖）＝本物の写しを作って、実際に書く。

    ★向け直しは名前を並べない★（罠⑭）＝BASE から作った定数を機械的に全部
    たどり、1つでも残っていたら試験のはじめに赤くする。
    """
    import shutil
    import tempfile
    g = globals()
    based = sorted(k for k, v in g.items()
                   if k.isupper() and isinstance(v, str)
                   and v.startswith(BASE))
    tmp = tempfile.mkdtemp(prefix="target_display_test_")
    keep = {k: g[k] for k in based}
    # ★★元の場所を先に控える★★（2026-09-11・自分で踏んだ）＝
    #   `BASE` 自身もこの名簿に入るので、回している最中に書き換わる。
    #   すると2つ目からの `replace(BASE, tmp)` が**何も置き換えず**、
    #   ★試験が本物のリポジトリを書き換えた★（罠⑭そのもの）。
    root = keep["BASE"]
    try:
        shutil.copytree(os.path.join(root, "assets"),
                        os.path.join(tmp, "assets"))
        # ★ひな型も写す★＝点検がひな型の契約も見るようになったので、
        #   写しに無いと「読めません」で全部赤くなる。
        shutil.copy2(os.path.join(root, "machine.html"),
                     os.path.join(tmp, "machine.html"))
        for k in based:
            g[k] = keep[k].replace(root, tmp)
        left = [k for k in based if g[k].startswith(root)]
        t("★写しへの向け直しに漏れが無い★", not left)
        if left:
            # ★★漏れたまま先へ進まない★★＝本物を書き換えてしまう。
            for k in based:
                g[k] = keep[k]
            shutil.rmtree(tmp, ignore_errors=True)
            return

        # ① 1回目で書ける
        code1 = apply_all()
        t("★本物の写しに書ける★", code1 == 0)
        # ② 2回目は何も動かない（★2回かけて壊れない★＝罠㉘）
        snap = {}
        for root, _d, files in os.walk(os.path.join(tmp, "assets")):
            for fn in files:
                fp = os.path.join(root, fn)
                snap[fp] = io.open(fp, "rb").read()
        code2 = apply_all()
        t("★2回目も終わる★", code2 == 0)
        same = all(io.open(fp, "rb").read() == b for fp, b in snap.items())
        t("★★2回かけても1バイトも変わらない★★"
          "／★変わるなら「いまの値を手がかりに、いまの値を書き換える」道具★",
          same)
        # ③ 書いたあとは点検が通る
        t("★書いたあとは点検が緑★", check() == 0)
        # ★★字下げを変えない★★（2026-09-11・自分で踏んだ）
        #   `indent=2` で書いて、機種一覧が丸ごと組み直された
        #   （2万4千行の差分・新台の復旧の試験が落ちた）。
        def _ind(p):
            with io.open(p, encoding="utf-8") as f:
                f.readline()
                ln = f.readline()
            return len(ln) - len(ln.lstrip(" "))
        t("★★書いても字下げが変わらない★★"
          "／★変わると、変えていない機種まで差分に出て誰も読めない★",
          _ind(g["MACHINES"]) == _ind(os.path.join(BASE, "assets", "data",
                                                   "machines.json"))
          and _ind(os.path.join(g["DETAILS"], "hokuto.json"))
          == _ind(os.path.join(BASE, "assets", "data", "machine-details",
                               "hokuto.json")))

        # ④ ★見出しを変えても持ち主は変わらない★（印で決めているか）
        import safe_json as _sj
        p_hokuto = os.path.join(g["DETAILS"], "hokuto.json")
        o = _sj.read_json(p_hokuto, expect=dict)
        hit = [b for b in o["summaryBoxes"] if b.get("role") == BOX_ROLE]
        t("　印が付いている", len(hit) == 1)
        hit[0]["label"] = "通常時"
        hit[0]["value"] = "手で書いた値600G〜"
        io.open(p_hokuto, "w", encoding="utf-8", newline="\n").write(
            json.dumps(o, ensure_ascii=False, indent=2) + "\n")
        t("★★見出しを変えて手書きを入れても点検が捕まえる★★"
          "／★見出しの文字で持ち主を決めると、ここで素通りする★",
          check() != 0)

        # ⑤ ★チェッカーの値を消したら、古い文を残して通さない★
        code_restore = apply_all()
        t("　直せば緑に戻る", code_restore == 0 and check() == 0)
        d = _sj.read_json(g["MACHINES"], expect=(dict, list))
        rows = d if isinstance(d, list) else (d.get("machines") or [])
        for m in rows:
            if m.get("slug") == "hokuto":
                m["checker"]["normal"]["byRate"]["eq56"]["good"] = None
                m["checker"]["normal"]["good"] = None
        io.open(g["MACHINES"], "w", encoding="utf-8", newline="\n").write(
            json.dumps(d, ensure_ascii=False, indent=2) + "\n")
        t("★★狙い目が読めなくなったら、古い文を残して通さない★★",
          check() != 0)

        # ⑥ ★★「決められない機種が在る」だけで止まるか★★
        #   ★ここを分けて試す理由★（2026-09-11・壊し方が捕まらなかった）＝
        #   ⑤は「文が食い違う」でも赤くなるので、
        #   ★『決められないものが在る』という条件を消しても緑のまま★だった。
        #   ＝狙った1つだけが効く形にする（罠④）。
        d2 = _sj.read_json(g["MACHINES"], expect=(dict, list))
        rows2 = d2 if isinstance(d2, list) else (d2.get("machines") or [])
        for m in rows2:
            if m.get("slug") == "hokuto":
                m["checker"]["normal"]["byRate"]["eq56"]["good"] = 570
                m["checker"]["normal"]["good"] = 550
        io.open(g["MACHINES"], "w", encoding="utf-8", newline="\n").write(
            json.dumps(d2, ensure_ascii=False, indent=2) + "\n")
        apply_all()
        t("　いったん全部そろえた", check() == 0)
        keep_plan = dict(BOX_PLAN)
        p_tensura = os.path.join(g["DETAILS"], "tensura.json")
        o2 = _sj.read_json(p_tensura, expect=dict)
        # ★壊す前の姿を控える★＝戻せないと、後ろの試験が全部
        #   「対応表の箱が見つからない」で落ちる（実際に落ちた）。
        _tensura_orig = io.open(p_tensura, encoding="utf-8").read()
        o2.pop("summaryBoxes", None)
        io.open(p_tensura, "w", encoding="utf-8", newline="\n").write(
            json.dumps(o2, ensure_ascii=False, indent=2) + "\n")
        try:
            BOX_PLAN.pop("tensura", None)
            mm, dd, sk = plan_all()
            t("　（前提）食い違いは無く、決められないものだけが在る",
              not mm and not dd and len(sk) == 1)
            t("★★決められない機種が1つでも在れば止める★★"
              "／★止めないと、作れない機種が増えても関所が静かに通す★",
              check() != 0)
            # ★★書く側も止まる★★（2026-09-11・Codexの指摘1）
            #   ★点検だけ止めても、書く側が素通りなら部分適用が起きる★
            t("★★決められない機種が在るときは、書く側も1文字も書かない★★",
              apply_all() != 0)
        finally:
            BOX_PLAN.clear()
            BOX_PLAN.update(keep_plan)
        # ⑦ ★★印の隣に手書きの箱を足したら止まる★★（Codexの指摘3）
        io.open(p_tensura, "w", encoding="utf-8", newline="\n").write(
            _tensura_orig)
        apply_all()
        t("　いったん全部そろえた（2）", check() == 0)
        p_goblin = os.path.join(g["DETAILS"], "goblin.json")
        o3 = _sj.read_json(p_goblin, expect=dict)
        # ★狙った守りだけが効く材料にする★（罠④）＝
        #   値に数値や狙い目の語を入れると、あとから足した
        #   stray_claims が先に止めてしまい、
        #   ★この守り（対応表の箱が残っている）を一度も通らない★。
        o3["summaryBoxes"].append({"label": "通常時",
                                   "value": "前兆に注意"})
        io.open(p_goblin, "w", encoding="utf-8", newline="\n").write(
            json.dumps(o3, ensure_ascii=False, indent=1) + "\n")
        t("★★印の在る機種に、印の無い手書きの箱を足したら止める★★"
          "／★印だけを見ていると、隣に足された手書きを見逃す★",
          check() != 0)
        # ★対照★＝狙い目と関係ない箱を足しても止めない
        o3["summaryBoxes"] = [b for b in o3["summaryBoxes"]
                              if b.get("label") != "通常時"]
        o3["summaryBoxes"].append({"label": "純増", "value": "約2.8枚/G"})
        io.open(p_goblin, "w", encoding="utf-8", newline="\n").write(
            json.dumps(o3, ensure_ascii=False, indent=1) + "\n")
        t("　（対照）狙い目と関係ない箱は止めない", check() == 0)
        # ⑨★★切替なし機種は、直したあとも検査され続けるか★★
        #   （2026-09-12・Codexの重大指摘）＝直す前は、直した瞬間に
        #   一覧から交換率の呼び名が消えて対象から外れ、
        #   ★以後どれだけ数値が食い違っても検査が動かなかった★。
        _d = _sj.read_json(g["MACHINES"], expect=(dict, list))
        _rows = _d if isinstance(_d, list) else (_d.get("machines") or [])
        _by2 = {str(x.get("slug")): x for x in _rows}

        def _write_machines():
            io.open(g["MACHINES"], "w", encoding="utf-8",
                    newline=chr(10)).write(
                json.dumps(_d, ensure_ascii=False, indent=1) + chr(10))

        _b = _by2.get("babel")
        _keep_strategy = str(_b.get("strategy") or "")
        _keep_good = _b["checker"]["normal"]["good"]
        # (1) チェッカーの値だけ動かす
        _b["checker"]["normal"]["good"] = 999
        _write_machines()
        t("★★書き換え済みの機種でチェッカーの値が動いたら止める★★"
          "／★直したあと対象から外れる形だと、ここが素通りした★",
          check() != 0)
        _b["checker"]["normal"]["good"] = _keep_good
        # (2) 一覧だけ動かす
        _b["strategy"] = _keep_strategy.replace("900G", "800G")
        _write_machines()
        t("★★書き換え済みの機種で一覧だけ動いたら止める★★", check() != 0)
        # (3) 呼び名が戻る
        _b["strategy"] = "等価730G〜 / 5.6枚740G〜 / 現金820G〜"
        _write_machines()
        t("★★一覧に交換率の呼び名が戻ったら止める★★", check() != 0)
        _b["strategy"] = _keep_strategy
        _write_machines()
        t("　戻せば緑", check() == 0)
        # (4) 外してある機種から、未確定の断り書きを消す
        _k = _by2.get("karakuri2")
        _keep_k = str(_k.get("strategy") or "")
        _k["strategy"] = _keep_k.replace("（個別ライン未確定）", "")
        _write_machines()
        t("★★外してある機種から「未確定」の断り書きが消えたら止める★★"
          "／★名簿に載せるだけだと、断り書きを消しても誰も気づかない★",
          check() != 0)
        _k["strategy"] = _keep_k
        _write_machines()
        # (5) 記事データを1件欠かす
        _p_babel = os.path.join(g["DETAILS"], "babel.json")
        _keep_babel = io.open(_p_babel, encoding="utf-8").read()
        os.remove(_p_babel)
        t("★★名簿の機種の記事データが無くなったら止める★★"
          "／★無いと機種ページの天井欄が一覧の文で埋まる★",
          check() != 0)
        io.open(_p_babel, "w", encoding="utf-8",
                newline=chr(10)).write(_keep_babel)
        t("　全部戻せば緑", check() == 0)

        # ⑧ ★★印の無い箱に狙い目を残したら止まる★★（Codexの指摘）
        #   ★関数だけの試験では、関門に配線されている証拠にならない★（罠③）
        #   ＝実データが綺麗なので、関門を殺しても何も変わらなかった。
        o4 = _sj.read_json(p_goblin, expect=dict)
        for _b in o4["summaryBoxes"]:
            if str(_b.get("label") or "") == "天井":
                _b["value"] = str(_b.get("value") or "") + "<br>600Gから狙い目"
        io.open(p_goblin, "w", encoding="utf-8",
                newline=chr(10)).write(
            json.dumps(o4, ensure_ascii=False, indent=1) + chr(10))
        t("★★天井の箱に狙い目を残したら止める★★"
          "／★これが実際に公開されていた姿（ヴァルヴレイヴ2）★",
          check() != 0)
        # ★★書く側も止まり、1文字も書かない★★（2026-09-12・Codexの指摘）＝
        #   ★移行の表を残していたときは、同じ文が戻っても黙って消していた★。
        #   いまは表を外したので、止まるのが正しい。
        _before = io.open(p_goblin, "rb").read()
        t("★★書く側も止まる★★", apply_all() != 0)
        t("★★止まったとき、記事は1バイトも変わらない★★",
          io.open(p_goblin, "rb").read() == _before)
    finally:
        for k in based:
            g[k] = keep[k]
        shutil.rmtree(tmp, ignore_errors=True)


def selftest() -> int:
    ok = [0]
    bad = []

    def t(name, cond):
        if cond:
            ok[0] += 1
        else:
            bad.append(name)

    ck = {"unit": "G",
          "exchangeRates": [{"key": "eq56", "label": "5.6枚"},
                            {"key": "rate45", "label": "7.0枚"}],
          "defaultRate": "eq56",
          "modes": [{"key": "normal", "label": "通常"}],
          "normal": {"good": 500, "caution": 400,
                     "byRate": {"eq56": {"good": 550, "caution": 420},
                                "rate45": {"caution": 600}}}}
    t("既定の交換率を読む", default_rate(ck) == "eq56")
    t("good を重ねて読む", effective_good(ck["normal"], "eq56") == 550)
    t("★byRate に good が無ければ土台を使う★",
      effective_good(ck["normal"], "rate45") == 500)
    t("caution は出さない",
      "420" not in " ".join(p["text"] for p in parts(ck, "eq56")))
    t("モードの文", parts(ck, "eq56")[0]["text"] == "通常550G〜")

    # ★番兵（設定狙い）は出さない★
    ck2 = {"unit": "G", "exchangeRates": [{"key": "eq56", "label": "5.6枚"}],
           "modes": [{"key": "settei", "label": "設定狙い"}],
           "settei": {"good": NO_TARGET}}
    t("★設定狙いの番兵を出さない★", parts(ck2, "eq56") == [])

    # ★回数がしきい値の形★
    ck3 = dict(ck)
    ck3["suru"] = {"good": 3, "byRate": {"eq56": {"good": 4}}}
    txt = [p["text"] for p in parts(ck3, "eq56")]
    t("★スルー回数のしきい値を出す★", "4スルー〜" in txt)

    # ★回数系の行★
    ck4 = {"unit": "G", "exchangeRates": [{"key": "eq56", "label": "5.6枚"}],
           "modes": [{"key": "suru", "label": "スルー天井"}],
           "suru": {"suru": [{"count": 0, "good": 450},
                             {"count": 1, "good": 350},
                             {"count": 3, "good": 0},
                             {"count": 4, "good": 0}]}}
    t("★回数系は両端を出し、モードの呼び名を残す★",
      parts(ck4, "eq56")[0]["text"] == "スルー天井 0スルー450G〜／3スルー0G〜")
    t("★行を持つ checker.suru は回数のしきい値として二重に出さない★",
      len(parts(ck4, "eq56")) == 1)
    # ★両端が同じ行なら1つだけ★
    ck4b = {"unit": "G", "exchangeRates": [{"key": "eq56", "label": "5.6枚"}],
            "modes": [{"key": "suru", "label": "スルー天井"}],
            "suru": {"suru": [{"count": 0, "good": 100},
                              {"count": 1, "good": 400}]}}
    t("★いちばん浅いのが先頭の行なら1つだけ出す★",
      parts(ck4b, "eq56")[0]["text"] == "スルー天井 0スルー100G〜")
    # ★回数系でも番兵を出さない★
    ck4c = {"unit": "G", "exchangeRates": [{"key": "eq56", "label": "5.6枚"}],
            "modes": [{"key": "suru", "label": "スルー天井"}],
            "suru": {"suru": [{"count": 0, "good": NO_TARGET}]}}
    t("★回数系でも番兵（設定狙い）は出さない★", parts(ck4c, "eq56") == [])
    # ★停止中の軸は出さない★（2026-09-12・Codexの指摘6）
    ck_off = {"unit": "G", "exchangeRates": [{"key": "eq56", "label": "5.6枚"}],
              "modes": [{"key": "normal", "label": "通常"}],
              "normal": {"good": 500},
              "suru": {"good": 4, "_disabled": "2026-07-24 Phase0"}}
    t("★★画面に出ていない軸は狙い目に出さない★★"
      "／★出すと、押しても使えないものを一覧が約束する★",
      [p["text"] for p in parts(ck_off, "eq56")] == ["通常500G〜"])
    ck_off2 = {"unit": "G", "exchangeRates": [{"key": "eq56", "label": "5.6枚"}],
               "modes": [{"key": "normal", "label": "通常"},
                         {"key": "suru", "label": "スルー天井"}],
               "normal": {"good": 500},
               "suru": {"_disabled": "止めた",
                        "suru": [{"count": 0, "good": 450}]}}
    t("　モードそのものが停止中でも出さない",
      [p["text"] for p in parts(ck_off2, "eq56")] == ["通常500G〜"])
    t("　（対照）停止していなければ出す",
      any("4スルー〜" in p["text"] for p in parts(
          {"unit": "G", "exchangeRates": [{"key": "eq56", "label": "5.6枚"}],
           "modes": [], "suru": {"good": 4}}, "eq56")))

    # ★byRate に good が null で在るときは「無い」と読む★（JSと同じ）
    ck4d = {"unit": "G", "exchangeRates": [{"key": "eq56", "label": "5.6枚"}],
            "modes": [{"key": "normal", "label": "通常"}],
            "normal": {"good": 500, "byRate": {"eq56": {"good": None}}}}
    t("★good が null で上書きされていたら出さない★"
      "／★JSは null で上書きするので、土台へ戻ってはいけない★",
      parts(ck4d, "eq56") == [])
    t("　鍵そのものが無ければ土台の値を使う",
      effective_good({"good": 500, "byRate": {"eq56": {"caution": 1}}},
                     "eq56") == 500)
    # ★箱の持ち主は印で決める（見出しを変えても持ち主は変わらない）★
    t("★印が在れば印で決める★",
      _target_boxes([{"label": "天井"},
                     {"label": "通常時", "role": BOX_ROLE}]) == [1])
    t("　印がまだ無い機種は見出しで拾う（移行の1回だけ）",
      _target_boxes([{"label": "リセット天井"},
                     {"label": "等価狙い目"}]) == [1])
    t("★天井の箱は狙い目ではない★",
      _target_boxes([{"label": "リセット天井狙い目"}]) == [])

    ck3b = dict(ck)
    ck3b.pop("suru", None)
    ck3b["through"] = {"good": 3}
    t("★through も スルー回数のしきい値★",
      "3スルー〜" in [p["text"] for p in parts(ck3b, "eq56")])
    ck3c = dict(ck)
    ck3c.pop("suru", None)
    ck3c["cycle"] = {"good": 5}
    t("★cycle のしきい値は周期目★",
      "5周期目〜" in [p["text"] for p in parts(ck3c, "eq56")])
    ck3d = {"unit": "G", "exchangeRates": [{"key": "eq56", "label": "5.6枚"}],
            "modes": [{"key": "suru", "label": "スルー天井"}],
            "suru": {"suru": [{"count": 2, "good": 0}]}}
    t("★行を持つものを二重に出さない★", len(parts(ck3d, "eq56")) == 1)

    # ★呼び名の付け方★
    got = for_machine({"checker": ck})
    t("一覧は呼び名つき", got["byRate"]["eq56"] == "5.6枚 通常550G〜")
    t("箱は呼び名なし", got["plain"]["eq56"] == "通常550G〜")

    # ★モードの呼び名に交換率が入っていたら2AIへ★
    ck5 = json.loads(json.dumps(ck))
    ck5["modes"][0]["label"] = "通常（5.6枚交換基準）"
    t("★呼び名に交換率が入っていたら知らせる★",
      len(problems({"checker": ck5})) == 1)

    # ★交換率を持たない機種は何も作らない★
    t("交換率が無ければ作らない", for_machine({"checker": {"unit": "G"}}) == {})

    # ★★交換率の切替を持たない機種★★（2026-09-12）
    _nr = {"unit": "G",
           "modes": [{"key": "normal", "label": "通常"}],
           "normal": {"good": 900},
           "reset": {"good": 500}}
    _m_nr = {"slug": "x", "checker": dict(_nr, modes=[
        {"key": "normal", "label": "通常"}, {"key": "reset", "label": "リセット"}]),
        "strategy": "等価730G〜 / 5.6枚740G〜 / 現金820G〜"}
    t("★★切替なしは「チェッカー基準」を付けて1本だけ作る★★"
      "／★呼び名なしだと、全交換率に当てはまる値だと読める★",
      norate_text(_m_nr)[0] == "チェッカー基準 通常900G〜 / リセット500G〜")
    t("★交換率の切替を持つ機種はここで作らない★",
      norate_text({"checker": {"exchangeRates": [{"key": "eq56"}]}})[1] != "")
    # ★一部しか作れないときは作らない★（Codexの指摘3）
    _bad = {"slug": "y", "strategy": "等価500G〜",
            "checker": {"unit": "G",
                        "modes": [{"key": "normal", "label": "通常"},
                                  {"key": "cz", "label": "CZ間"}],
                        "normal": {"good": 900},
                        "cz": {"good": "よめない"}}}
    t("★★軸が1つでも作れなければ、その機種は触らない★★"
      "／★残った分だけで作ると、一部しか出ていない一覧を「直した」ことにする★",
      norate_text(_bad)[0] == "")
    # ★★回数の行が1つでも読めなければ作らない★★（Codexの指摘）
    #   ★有効な行が1つでもあれば軸を1つと数えていた★ので、
    #   別の行が読めなくても件数が一致し、部分的な一覧を作れた。
    _rows_bad = {"unit": "G",
                 "modes": [{"key": "suru", "label": "スルー天井"}],
                 "suru": {"suru": [{"count": 0, "good": 450},
                                   {"count": 1, "good": "よめない"}]}}
    t("★★回数の行が1つ読めなければ、その機種は触らない★★"
      "／★読める行だけで作ると、一部しか出ていない一覧になる★",
      norate_text({"slug": "babel", "strategy": "等価450G〜",
                   "checker": _rows_bad})[0] == "")
    t("　（対照）全部読めれば作る",
      norate_text({"slug": "babel", "strategy": "等価450G〜",
                   "checker": {"unit": "G",
                               "modes": [{"key": "suru",
                                          "label": "スルー天井"}],
                               "suru": {"suru": [{"count": 0, "good": 450},
                                                 {"count": 1, "good": 0}]}}
                   })[0] != "")
    # ★★停止中の判定は「鍵が在るか」★★（Codexの指摘）
    #   ★公開側は鍵の有無で止めている★＝真偽で見ると、
    #   空文字や0を書かれたときに食い違う。
    t("★★_disabled が空文字でも停止中とみなす★★"
      "／★真偽で見ると、空文字を書かれた瞬間に画面と食い違う★",
      is_off({"_disabled": "", "good": 5}) is True)
    t("　鍵が無ければ停止中ではない", is_off({"good": 5}) is False)

    # ★対象の決め方は「固定の名簿」★（2026-09-12・Codexの重大指摘）
    #   ★いまの一覧の中身で決めない★＝直した瞬間に対象から外れ、
    #   以後どれだけ数値が食い違っても検査が動かなくなる。
    t("★★名簿に載っていれば、一覧の中身に関係なく対象★★"
      "／★中身で決めると、直した瞬間に守りが消える★",
      _norate_target({"slug": "babel",
                      "strategy": "チェッカー基準 通常900G〜",
                      "checker": {"unit": "G", "modes": []}}) is True)
    t("　名簿に無い機種は対象にしない",
      _norate_target({"slug": "z", "strategy": "等価170G〜",
                      "checker": {"unit": "G", "modes": []}}) is False)
    t("★基準を名乗っている機種は、理由つきで外してある★",
      _norate_target({"slug": "karakuri2", "strategy": "等価 液晶800G〜",
                      "checker": {"unit": "G", "modes": []}}) is False
      and len(NORATE_KEEP["karakuri2"]["why"]) > 10)
    # ★外してある前提が崩れたら言う★
    t("★★外してある機種から断り書きが消えたら言う★★",
      keep_problems({"slug": "karakuri2",
                     "strategy": "等価 液晶800G〜"}) != [])
    t("　断り書きが在れば言わない",
      keep_problems({"slug": "karakuri2",
                     "strategy": "等価 液晶800G〜（当サイト目安）"
                                 "/ 個別ライン未確定"}) == [])
    # ★名簿に無い新しい候補を見つける（対象の決定とは別）★
    t("★名簿に無い候補は別に数える★",
      norate_unclassified([{"slug": "zzz", "strategy": "等価500G〜",
                            "checker": {"unit": "G", "modes": []}}])
      == ["zzz"])

    # ★印の無い箱が狙い目を言っていないか★（2026-09-11・Codexの指摘）
    #   ★読者に矛盾が見えていた★＝ヴァルヴレイヴ2の天井の箱に
    #   「600Gから狙い目」が残り、作った箱の550Gと食い違っていた。
    t("★印の無い箱が狙い目を言っていたら止める★",
      any("狙い目を言っています" in x for x in stray_claims(
          [{"label": "CZ間天井", "value": "999G+α<br>600Gから狙い目"}])))
    t("★見出しで狙い目を名乗っていても止める★"
      "／★『スルー狙い』は「狙い目」という語を含まない★",
      any("見出しが狙い目を名乗っています" in x for x in stray_claims(
          [{"label": "スルー狙い", "value": "4スルー〜"}])))
    t("　作った箱（印つき）は見ない", stray_claims(
        [{"label": "5.6枚狙い目", "value": "通常570G〜", "role": BOX_ROLE}])
        == [])
    t("　狙い目を言っていない箱は通す", stray_claims(
        [{"label": "天井", "value": "999G+α"},
         {"label": "ヤメ時", "value": "前兆を確認して区切る"}]) == [])
    # ★★言い換えでも止める★★（2026-09-12・Codexの指摘）＝
    #   「狙い目」だけを見ていたので、サイトで実際に使っている言い換えが素通りした
    #   （machines.json で 狙え=22回・即打ち=18回）。
    for _lab, _val, _w in (
            ("リセット", "0Gから狙える", "狙"),
            ("スルー", "4スルー以降は即打ち推奨", "即打ち"),
            ("通常", "45Gから打ち始め", "打ち始め"),
            ("通常", "600Gから打てる", "打てる")):
        t("★言い換え「%s」でも止める★" % _w,
          any("狙い目を言っています" in x
              for x in stray_claims([{"label": _lab, "value": _val}])))
    t("★★裸の開始値だけでも止める★★"
      "／★語を一つも使わない形が実際に2箱あった★",
      any("裸の開始値" in x for x in stray_claims(
          [{"label": "リセット", "value": "50G〜"}])))
    # ★★言葉だけでは広すぎる★★＝数値を伴うときだけ宣言とみなす
    t("★★やり方の説明は止めない★★"
      "／★『天井狙い＋リセット狙い』で止めると10機種が公開できなくなる★",
      stray_claims([{"label": "基本方針",
                     "value": "天井狙い＋リセット狙い"}]) == [])
    t("　天井の値そのものは止めない",
      stray_claims([{"label": "天井",
                     "value": "通常999G ／ チャンス・引き戻し200G"}]) == [])

    # ★ひな型の契約★（2026-09-11・Codexの指摘2）
    _tpl_ok = io.open(TEMPLATE, encoding="utf-8").read()
    t("★いまのひな型は契約どおり★", template_problems(_tpl_ok) == [])
    t("★★古い予備生成器の形へ戻したら止める★★"
      "／★戻しても、データが一致していれば点検は緑だった★",
      any("古い予備生成器" in x for x in template_problems(
          _tpl_ok.replace("const normalText = _hasRate ? _sbr[rateKey]",
                          "const normalText = (_sbr && _sbr[rateKey]) "
                          "|| buildTargetFromChecker(rateKey); //")
          .replace("hasOwnProperty.call(_sbr, rateKey)", "false"))))
    t("★所有権の印で探すのをやめたら止める★",
      any("所有権の印で探していません" in x for x in template_problems(
          _tpl_ok.replace('box && box.role === "target"', "false"))))
    t("★画面に印を出すのをやめたら止める★",
      any("印を出していません" in x for x in template_problems(
          _tpl_ok.replace('data-role="${x.role}"', ""))))
    t("★★条件だけを殺しても止める★★"
      "／★文字だけを見ていると、手前の条件を false にされて素通りする★",
      any("印を出していません" in x for x in template_problems(
          _tpl_ok.replace("x && x.role ? ` data-role=",
                          "false ? ` data-role="))))
    t("★リセットの箱が天井を巻き込む形に戻したら止める★",
      any("天井の箱を巻き込みます" in x for x in template_problems(
          _tpl_ok.replace('lb.includes("リセット") && !lb.includes("天井")',
                          'lb.includes("リセット")'))))
    t("★ひな型を読めないときも止める（fail-closed）★",
      template_problems("") != [])

    # ★★点検が本当にひな型の契約を見に行くか★★（罠③）
    #   ★関数の試験だけでは、呼び出しを外したことに気づけない★
    _keep_tp = globals()["template_problems"]
    globals()["template_problems"] = lambda src=None: ["わざとの不合格"]
    try:
        _called = check() != 0
    finally:
        globals()["template_problems"] = _keep_tp
    t("★★点検の本体がひな型の契約を呼んでいる★★"
      "／★呼び出しを外しても、関数だけの試験は緑のまま★", _called)
    t("　戻せば点検は緑", check() == 0)

    # ★実データで落ちないこと★
    real = _load()
    made = [for_machine(m) for m in real]
    t("実データで例外が出ない", len(made) == len(real))
    t("実データでも交換率つきが作れている",
      sum(1 for g in made if g) > 40)

    _real_file_tests(t)

    print("%d/%d 合格" % (ok[0], ok[0] + len(bad)))
    for b in bad:
        print("  NG:", b)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

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
        if not isinstance(conf, dict):
            continue
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
        if not isinstance(ts, dict):
            continue
        if any(isinstance(ts.get(k), list) for k in ("suru", "cycle")):
            continue
        g = effective_good(ts, rk)
        if g is None or g >= NO_TARGET:
            continue
        txt = "%d%s〜" % (g, word)
        if txt not in [o["text"] for o in out]:
            out.append({"kind": "count", "text": txt})
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
    if skipped:
        print("★機械では決められない機種があります: %d★" % len(skipped))
        for slug, why in skipped:
            print("   %-22s %s" % (slug, why))
        print()
    if not machines and not details and not skipped and not tpl:
        print("★一致しています（狙い目の文は作ったものと同じ）★")
        return 0
    if tpl and not machines and not details and not skipped:
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
        o3["summaryBoxes"].append({"label": "通常時",
                                   "value": "手書き600G〜"})
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

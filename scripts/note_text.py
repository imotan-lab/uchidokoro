# -*- coding: utf-8 -*-
"""★小役カウンターの注記を直す唯一の場所★

★★なぜ要るか★★（2026-09-12・実測）
記事本文の狙い目は `target_display.py` でカウンターの値へそろえたが、
★カウンターのすぐ下に出る「注記」は手書きのまま★で、同じ食い違いが残っていた。

  バベル   画面に出る注記「等価730G〜・5.6枚740G〜・現金820G〜が狙い目。」
           カウンター自身の判定       通常 900G
           ★交換率の切替そのものが無い機種★＝読者は「等価」を選べない
  ＝読者は、選べない呼び名で、判定より160G早く座らされる。

★画面に出る注記の決まり★（machine.html の resolveRateConfig と同じ）＝
  出る注記 = byRate[交換率].note が**在れば**それ、無ければモード直下の note。
  ★「データに在る件数」と「画面に出る件数」は違う★（実測 504 本が画面に出る）。

★★この道具が守る線★★（どれも意味の判断ではない＝機械に分かる）
  ①`after` は `before` から**文字を消しただけ**であること（部分列）
    ★これが守るのは「文字を足していない」ことだけ★（2026-09-12・Codexの指摘1）。
    ★意味は守らない★＝「500Gからは狙い目ではない」から「ではない」を消せば
    「500Gから狙い目」が作れるし、離れた数字をつなげて別の数値も作れる。
    ＝**意味が変わっていないことは2AIが見る**（機械には分からない）。
  ②残った注記に、その機種で**選べない交換率の呼び名**を残さない
  ③残った注記の「ここから」の数値は、その枝の境目
    （候補 caution / 狙い目 good / 強め excellent）のどれかであること
  ④消えて記事にも機種データにも無くなる数値は、理由つきで名指しすること
  ⑤判断者が2つ以上／機種データの指紋が一致／1件でも通らなければ何も書かない
★どれを消すかは2AIが決める★（機械は上の5つを確かめるだけ）。

使い方:
  python scripts/note_text.py --list            # 読むべき注記を挙げる
  python scripts/note_text.py --check           # 関所（1件でも残っていれば非0）
  python scripts/note_text.py --file 決定 [--apply]
  python scripts/note_text.py --selftest
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

import target_display as T   # noqa: E402

MACHINES = T.MACHINES
SCHEMA = "note-text/v1"

# ★交換率を指す言い方★（正本は target_display と同じ集合＋現金系）
RATE_WORDS = ("等価", "現金", "5.0枚", "5.6枚", "56枚", "6.0枚", "6.5枚", "7.0枚")

# ★「ここから座れ」の形★＝数値のすぐ後ろが 〜 か「から」
#   ★範囲の書き方は着席の基準ではない★（2026-09-12・自分で踏んだ）＝
#   「天井600〜699pt」「301〜400Gにゾーン」の左側まで
#   「ここから座れ」と読んで、正しい注記を2本も挙げていた。
#   → 「〜」のすぐ後ろに数字が続くものは範囲なので数えない。
#   ★桁数で切らない★（2026-09-12・Codexの指摘3）＝
#   `\d{2,4}` にしていたので「8周期〜」「0Gから」を1本も見ていなかった。
#   ★単位も取る★＝別の軸の数値（スルー・周期）は境目と比べない（手がかりへ）。
SIT = re.compile(
    r"(\d+)\s*(G|Ｇ|pt|枚|あべし|回|個|周期|スルー)?\s*"
    r"(?:〜(?!\s*\d)|から)")
# ★呼び名との突き合わせでは、つなぎ語を問わずに拾う★
#   （「420G基準」「760G以上」には 〜 も から も無い）
SIT_ANY = re.compile(r"(\d+)\s*(G|Ｇ|pt|枚|あべし|回|個|周期|スルー)")
# ★★行動をすすめる語の名簿はやめた★★（2026-09-12・Codexの指摘3）
#   ★名簿に「目安」「本線」「基準」「十分」が無いだけで素通りしていた★
#   （実際、バキの「2スルー時はより浅く見られます」が通っていた）。
#   ★名簿を足す方向には行かない★＝語を増やすのは意味を機械に決めさせる形。
#   → ★「ここから」の形をした数値は、語に関係なく全部境目と突き合わせる★。
#   （天井のような「ここから」でない数値と、範囲の左側は SIT が外す）

# ★★カウンターが画面で使う3つの呼び名★★
#   ★これは「語の名簿」ではない★＝カウンターの判定そのものの呼び名
#   （machine.html が候補／狙い目／強めの3段で答える）。
#   ★なぜ要るか★＝直す前は「候補・狙い目・強めのどれかと一致すればよい」に
#   していたので、★候補の値を「狙い目」と書いた注記が通っていた★
#   （バベルのリセット「360G〜が狙い目」／その枝の候補=360・狙い目=500）。
GOOD_WORDS = ("狙い目", "本線", "基準", "本命", "着席")
CAUTION_WORDS = ("候補", "手前", "様子見")

# ★軸の呼び名と、その軸のモード鍵★
AXIS_WORDS = (("スルー", "suru"), ("周期", "cycle"))
# ★軸に数がついた形★（「2スルー時」「4周期目」）＝別の軸の基準を出している印
AXIS_NUM = re.compile(r"(\d+)\s*(スルー|周期)")

_NUM = re.compile(r"\d+(?:\.\d+)?")


def _is_subsequence(small: str, big: str) -> bool:
    """★消しただけか★＝small の文字が big に同じ順で現れるか。

    ★これが守るのは「文字を足していない」ことだけ★（2026-09-12・Codex）。
    ★意味は守らない★＝「500Gからは狙い目ではない」から「ではない」を
    消せば「500Gから狙い目」になるし、離れた数字をつなげて別の数値も作れる。
    ＝意味が変わっていないことは2AIが見る（機械には分からない）。
    """
    it = iter(big)
    return all(ch in it for ch in small)


def rate_labels(m: dict):
    ck = m.get("checker") or {}
    rates = ck.get("exchangeRates")
    if not isinstance(rates, list):
        return []
    return [(str(r.get("key")), str(r.get("label")))
            for r in rates if isinstance(r, dict)]


def bounds(conf: dict, rk: str) -> dict:
    """その枝で読者に出る3つの境目（無い鍵は入れない）。"""
    got = {}
    br = conf.get("byRate")
    leaf = br.get(rk) if isinstance(br, dict) and isinstance(br.get(rk), dict) \
        else {}
    for key in ("caution", "good", "excellent"):
        v = leaf[key] if key in leaf else conf.get(key)
        v = T._int(v)
        if v is not None and v < T.NO_TARGET:
            got[key] = v
    return got


def off_axes(m: dict):
    """★画面に出ない軸★＝止めてある、またはモード一覧に載っていない。"""
    ck = m.get("checker") or {}
    live = {str(md.get("key")) for md in (ck.get("modes") or [])
            if isinstance(md, dict)}
    got = []
    for word, key in AXIS_WORDS:
        c = T.mode_conf(ck, key)
        if isinstance(c, dict) and (T.is_off(c) or key not in live):
            got.append(word)
    return got


def states(m: dict):
    """★読者が作れる画面の状態を全部たどる★（2026-09-12・Codexの指摘1）

    ★直す前はモード直下しか見ていなかった★＝
    画面（machine.html の `getConfig`）は
    `modeData.suru[] / cycle[]` の**行**を選び、その行の注記を出す。
    ＝行を持つモードでは**モード直下の注記は一度も出ない**のに、
    それを「出ている注記」として数え、行の注記は1本も見ていなかった。
    実測＝15機種・90行が丸ごと検査の外にあった。

    返すもの（1つの状態＝読者が選べる モード×回数×交換率 の組）:
      mode / count（行が無ければ None）/ rate / note / bounds
      holder … その注記が**実際に入っている辞書**（書き換える先）
    """
    ck = m.get("checker") or {}
    rl = rate_labels(m)
    out = []
    for md in (ck.get("modes") or []):
        if not isinstance(md, dict):
            continue
        k = str(md.get("key") or "")
        conf = T.mode_conf(ck, k)
        if not isinstance(conf, dict) or T.is_off(conf):
            continue          # ★止めてある軸は読者に届かない★
        rowsets = None
        for axis in ("suru", "cycle"):
            if isinstance(conf.get(axis), list) and conf[axis]:
                rowsets = [(r, T._int(r.get("count")))
                           for r in conf[axis] if isinstance(r, dict)]
                break
        for node, count in (rowsets if rowsets else [(conf, None)]):
            br = node.get("byRate") if isinstance(node.get("byRate"), dict) else {}
            for rk, _label in (rl or [("", "")]):
                leaf = br.get(rk) if isinstance(br.get(rk), dict) else {}
                holder = leaf if "note" in leaf else node
                note = holder.get("note")
                out.append({"mode": k, "count": count, "rate": rk,
                            "note": note if isinstance(note, str) else "",
                            "bounds": bounds(node, rk), "holder": holder,
                            "node": node, "own": holder is not node,
                            "mode_label": str(md.get("label") or k),
                            "rate_label": _label})
    return out


def shown_notes(m: dict):
    """★読者の画面に出る注記だけ★"""
    return [s for s in states(m) if s["note"].strip()]


def native_unit(m: dict) -> str:
    """★その機種のカウンターが数えている単位★（G / pt / あべし / 周期）"""
    return str((m.get("checker") or {}).get("unit") or "G")


def note_problems(m: dict, note: str, bd: dict, mode: str = "",
                  axes=None) -> list:
    """★注記そのものの問題★（消す前・消したあとの両方に当てる）。

    ★単位を見る★（2026-09-12・Codexの指摘3）＝
    その機種のカウンターが数えている単位の「ここから」だけを境目と比べる。
    別の軸（スルー・周期）の数値は `note_hints` へ回す
    （天井の仕様・単位そのもの・条件つきの基準を構文で区別できないため）。

    ★境目が1つも無い枝で素通りさせない★＝
    直す前は `if bd:` で、境目が空だと「500G〜から狙い目」も通っていた。
    """
    got = []
    labels = {lb for _k, lb in rate_labels(m)}
    if labels:                     # ★切替が在る機種★
        bad = [w for w in RATE_WORDS if w in note and w not in labels]
        if bad and SIT.search(note):
            got.append("選べない交換率の呼び名で基準を書いています: "
                       + "/".join(bad))
    else:                          # ★切替が無い機種★
        bad = [w for w in RATE_WORDS if w in note]
        if bad and SIT.search(note) and m.get("slug") not in T.NORATE_KEEP:
            got.append("交換率を選べない機種なのに呼び名で基準を書いています: "
                       + "/".join(bad))
    unit = native_unit(m)
    ok = set(bd.values())
    off = []
    for num, u in SIT.findall(note):
        u = (u or "").replace("Ｇ", "G")
        if u in ("スルー", "周期") and u != unit:
            continue               # ★別の軸＝手がかりへ回す★
        if u and u != unit and u not in ("回", "個"):
            continue               # ★別の単位の話（枚数など）は見ない★
        if int(num) not in ok:
            off.append(num)
    if off:
        got.append(
            "カウンターの境目（候補%s / 狙い目%s / 強め%s）のどれとも"
            "違う値で「ここから」と書いています: %s"
            % (bd.get("caution"), bd.get("good"), bd.get("excellent"),
               "/".join(off)))
    got += mislabeled(note, bd, unit)
    return got


def _sentences(note: str):
    out, cur = [], ""
    for ch in str(note or ""):
        cur += ch
        if ch == "。":
            out.append(cur)
            cur = ""
    if cur.strip():
        out.append(cur)
    return out


def mislabeled(note: str, bd: dict, unit: str) -> list:
    """★候補の値を「狙い目」と呼んでいないか★（2026-09-12）

    ★カウンターの3段のうち、どれを指しているかは文字で分かる★＝
    「候補」と書いてあれば候補の値、「狙い目・基準・本命」と書いてあれば
    狙い目の値。★値そのものはカウンターが持っている★ので、
    機械が突き合わせられる（意味の判断ではない）。
    """
    good, caution = bd.get("good"), bd.get("caution")
    if good is None or caution is None or good == caution:
        return []
    got = []
    for sent in _sentences(note):
        if not any(w in sent for w in GOOD_WORDS):
            continue
        if any(w in sent for w in CAUTION_WORDS):
            continue          # ★両方の呼び名がある文は、2AIが読む★
        # ★★「前提が違う場合」の断り書きは、いまの枝の基準ではない★★
        #   （2026-09-12・自分で踏んだ）＝
        #   「★この判定は『1スルー以降』を前提にしています★
        #     （0スルー台は360G〜が狙い目なので、判定が△でも狙える場合があります）」
        #   は**正しい**。カウンターが△と言う帯が、別の前提では◯になる、という
        #   カウンター自身の限界の説明。ここを機械が「候補を狙い目と呼んでいる」と
        #   読んで値を書き換えると、★正しい情報を壊す★（2機種で実際に起きかけた）。
        if "前提" in sent or "判定" in sent:
            continue
        nums = [int(n) for n, u in SIT_ANY.findall(sent)
                if (u or "").replace("Ｇ", "G") == unit]
        if caution in nums and good not in nums:
            got.append("候補の値（%s）を「狙い目」として書いています"
                       "（この枝の狙い目は %s）: %s"
                       % (caution, good, sent.strip()[:36]))
    return got


def note_hints(m: dict, note: str, mode: str = "", axes=None) -> list:
    """★読む手がかり★（止める判断には使わない・2AIが読んで決める）

    ★★なぜ止める側に入れないか★★（2026-09-12・実測して決めた）
    「いま見ているモードと違う軸に数がついている」で止めようとしたら、
    ★79本のうち37本が挙がり、その大半が正しい注記だった★＝
      ・単位そのものが周期の機種（「8周期〜が狙い目」は正しい）
      ・天井の仕様（「周期最大4周期に短縮」は行動基準ではない）
    ＝**この形は機械に決められない**（罠＝場合分けを足したくなる合図）。
    ★機械は挙げるだけ／落とすか残すかは記事とカウンターを読んで2AIが決める★。

    ★これで見つけられないもの★＝生きている別の軸の基準
    （バキの「2スルー時はより浅く見られます」）。
    ★機械は捕まえない★ので、注記を触るときは必ず2AIが読むこと。
    """
    got = []
    here = {"suru": "スルー", "cycle": "周期"}.get(str(mode or ""))
    for ax in (axes if axes is not None else off_axes(m)):
        if ax in note and ax != here:
            got.append("画面に出ていない軸の話: " + ax)
    for _n, ax in AXIS_NUM.findall(note):
        if ax != here and ax != native_unit(m):
            got.append("いま見ているモードと違う軸に数がついている: " + _n + ax)
            break
    return got


def survey(machines=None):
    """★読むべき注記を挙げる★（決めない＝候補を出すだけ）。"""
    rows, seen_total = [], 0
    for m in (machines if machines is not None else T._load()):
        axes = off_axes(m)
        for st in shown_notes(m):
            seen_total += 1
            got = note_problems(m, st["note"], st["bounds"], st["mode"], axes)
            hints = note_hints(m, st["note"], st["mode"], axes)
            if got or hints:
                row = {"slug": m.get("slug"), "name": m.get("name")}
                row.update({k: v for k, v in st.items() if k != "holder"})
                row["problems"], row["hints"] = got, hints
                rows.append(row)
    return rows, seen_total


# --- 決定ファイルを当てる ------------------------------------------------

def _numbers(s: str):
    return _NUM.findall(str(s or ""))


def _num_counts(s: str):
    """★数値を「まとまり」で数える★（2026-09-12・Codexの指摘5）

    ★直す前は文字列に含まれるかで見ていた★ので、
    消えた `50` が無関係な `150` に含まれているだけで「残っている」と読めた。
    """
    from collections import Counter
    return Counter(_NUM.findall(str(s or "")))


def _public_blob(m: dict, slug: str) -> str:
    """その機種について公開しているもの（機種データ＋記事データ）。"""
    out = [json.dumps(m, ensure_ascii=False)]
    p = os.path.join(BASE, "assets", "data", "machine-details", slug + ".json")
    if os.path.exists(p):
        try:
            with io.open(p, encoding="utf-8") as f:
                out.append(f.read())
        except OSError:
            pass
    return "\n".join(out)


def displaying_branches(m: dict, holder):
    """★その置き場の注記を出す状態を全部返す★（2026-09-12・Codexの指摘4）

    ★直す前は、土台の注記を直したのに土台の境目でしか見ていなかった★。
    土台の注記は「自分の枝に note を持たない交換率」ぜんぶに出るので、
    ★別の交換率では境目が違い、そちらでは食い違ったまま★になりうる。
    ★行（スルー・周期）も同じ★＝holder が同じ状態を全部見る。
    """
    return [st for st in states(m) if st["holder"] is holder]


def _locate(m: dict, mode: str, rate: str, count=None):
    """★注記の置き場（辞書そのもの）★を返す。無ければ None。

    ★読者に届く状態からしか選ばせない★（2026-09-12・Codexの指摘）＝
    知らない交換率・止めてあるモード・無い行を指した決定ファイルで
    「届く枝がある」と判定できてしまっていた。
    """
    for st in states(m):
        if st["mode"] != mode or st["count"] != count:
            continue
        if rate:
            # ★その交換率が自分の注記を持っているときだけ、そこを指せる★
            if st["rate"] == rate and st["own"]:
                return st["holder"]
            continue
        # ★rate を空にしたら、土台（モード直下、または行）を指す★
        return st["node"]
    return None


def retarget_text(note: str, bd: dict, unit: str):
    """★候補の値を、その枝の狙い目の値へ置き換える★（2026-09-12）

    ★2AIが決めるのは「どの注記を直すか」だけ★。
    ★何に書き換えるかは機械が決める★＝どちらの値もカウンターが持っているので、
    新しい数字を作る余地が無い（言い換えも起きない）。
    """
    good, caution = bd.get("good"), bd.get("caution")
    if good is None or caution is None or good == caution:
        return None, "その枝に候補と狙い目の両方がありません"
    pat = re.compile(r"(?<!\d)%d\s*(%s)" % (caution, re.escape(unit)))
    after, n = pat.subn(lambda m: "%d%s" % (good, m.group(1)), note)
    if not n:
        return None, "候補の値（%d%s）がこの注記にありません" % (caution, unit)
    return after, ""


def apply_decision(path: str, apply_it: bool = False) -> dict:
    """★1件でも通らなければ何も書かない★"""
    res = {"problems": [], "done": []}
    try:
        with io.open(path, encoding="utf-8") as f:
            dec = json.load(f)
    except Exception as e:                                   # noqa: BLE001
        res["problems"].append("決定ファイルを読めません: %s" % e)
        return res
    if dec.get("schema_version") != SCHEMA:
        res["problems"].append("知らない版です: %r" % dec.get("schema_version"))
        return res
    by = [str(x).strip() for x in (dec.get("decided_by") or []) if str(x).strip()]
    if len({b.lower() for b in by}) < 2:
        res["problems"].append("**違う**判断者が2つ以上要ります（2AIで決めるため）")
        return res
    now = T._digest(MACHINES)
    if str(dec.get("source_sha256") or "") != now:
        res["problems"].append(
            "機種データが、決めたときから変わっています（%s… → %s…）"
            % (str(dec.get("source_sha256"))[:12], now[:12]))
        return res

    with io.open(MACHINES, encoding="utf-8") as f:
        data = json.load(f)
    index = {str(x.get("slug")): x for x in data if isinstance(x, dict)}

    removed = {}
    for r in (dec.get("numbers_removed") or []):
        if not isinstance(r, dict) or len(str(r.get("why") or "").strip()) < 10:
            res["problems"].append("numbers_removed には理由（10字以上）が要ります")
            return res
        # ★機種ごとに名指しする★（2026-09-12・Codexの指摘5）＝
        #   直す前は「500」の理由が1つあれば、どの機種の500にも効いた。
        if not str(r.get("slug") or "").strip():
            res["problems"].append(
                "numbers_removed には、どの機種の数値かを slug で書いてください")
            return res
        removed[(str(r.get("slug")), str(r.get("n")))] = str(r.get("why"))

    plan = []
    for a in (dec.get("actions") or []):
        slug = str(a.get("slug") or "")
        m = index.get(slug)
        if m is None:
            res["problems"].append("知らない機種です: %r" % slug)
            return res
        spot = _locate(m, str(a.get("mode") or ""), str(a.get("rate") or ""),
                       a.get("count"))
        if spot is None:
            res["problems"].append(
                "%s にその注記の置き場がありません（mode=%r count=%r rate=%r）"
                "＝読者がその画面を作れません"
                % (slug, a.get("mode"), a.get("count"), a.get("rate")))
            return res
        before = str(a.get("before") or "")
        if spot.get("note") != before:
            res["problems"].append(
                "%s のいまの注記と before が違います: %r"
                % (slug, str(spot.get("note"))[:40]))
            return res
        op = str(a.get("op") or "trim")
        if op not in ("trim", "retarget"):
            res["problems"].append("%s: 知らない操作です: %r" % (slug, op))
            return res
        if op == "retarget":
            # ★書き換える先は機械が決める★（2AIは場所だけ選ぶ）
            branches = displaying_branches(m, spot)
            pairs = {(st["bounds"].get("caution"), st["bounds"].get("good"))
                     for st in branches}
            if len(pairs) != 1:
                res["problems"].append(
                    "%s: この注記は境目の違う枝に出るので、値を直せません"
                    "（%s）" % (slug, sorted(pairs)))
                return res
            after, why_ng = retarget_text(before, branches[0]["bounds"],
                                          native_unit(m))
            if after is None:
                res["problems"].append("%s: %s" % (slug, why_ng))
                return res
            if a.get("after") and str(a.get("after")) != after:
                res["problems"].append(
                    "%s: 機械が作る文と、決定に書かれた after が違います"
                    % slug)
                return res
        else:
            after = str(a.get("after") or "")
            if not after.strip():
                res["problems"].append(
                    "%s: after が空です（注記ごと消せません）" % slug)
                return res
            # ①消しただけか
            if not _is_subsequence(after, before):
                res["problems"].append(
                    "%s: after が before から文字を消しただけになっていません"
                    "（文字を足せません）: %r" % (slug, after[:40]))
                return res
        if len(str(a.get("meaning_why") or "").strip()) < 15:
            res["problems"].append(
                "%s: 何を落としたのかの理由を meaning_why に書いてください"
                "（15字以上）" % slug)
            return res
        if len(str(a.get("why") or "").strip()) < 10:
            res["problems"].append("%s: why（10字以上）が要ります" % slug)
            return res
        # ②③残ったものに問題が無いか
        #   ★その注記を出す交換率の枝ぜんぶで見る★（Codexの指摘4）
        seen_branch = displaying_branches(m, spot)
        if not seen_branch:
            res["problems"].append(
                "%s: その注記を出す状態が1つもありません（読者に届きません）" % slug)
            return res
        for st in seen_branch:
            rest = note_problems(m, after, st["bounds"], st["mode"])
            if rest:
                res["problems"].append(
                    "%s（%s%s%s）: 直したあとにも残ります: %s"
                    % (slug, st["mode"],
                       ("・%s回" % st["count"]) if st["count"] is not None else "",
                       ("・" + st["rate"]) if st["rate"] else "",
                       " ／ ".join(rest)))
                return res
        plan.append((slug, spot, before, after, a))

    # ④消える数値が、公開しているどこにも無くならないか
    #   ★★機種ごとに1回だけ、全部当てたあとで比べる★★
    #   （2026-09-12・Codexの2回目の指摘2）＝
    #   直す前は操作ごとに比べていたので、★同じ機種の注記AとBに同じ数値があり
    #   両方で消す場合、Aを見るときはBに残り、Bを見るときはAに残る★ため
    #   どちらも理由が要らず、全部当てたあとには消えていた。
    by_slug = {}
    for slug, spot, before, after, a in plan:
        by_slug.setdefault(slug, []).append((spot, before, after))
    for slug, items in by_slug.items():
        m = index[slug]
        before_blob = _num_counts(_public_blob(m, slug))
        keep = [(spot, spot.get("note")) for spot, _b, _af in items]
        try:
            for spot, _b, after in items:
                spot["note"] = after          # ★写しではなく本体を一時的に★
            after_blob = _num_counts(_public_blob(m, slug))
        finally:
            for spot, orig in keep:
                spot["note"] = orig           # ★必ず戻す★
        for n, cnt in before_blob.items():
            if after_blob.get(n):
                continue
            if (slug, n) not in removed:
                res["problems"].append(
                    "%s: 全部当てると %s が機種データからも記事からも"
                    "無くなります（消してよいなら numbers_removed に "
                    "slug と理由つきで名指ししてください）" % (slug, n))
                return res

    if not plan:
        res["problems"].append("やる操作がありません")
        return res
    if not apply_it:
        res["done"] = ["%s %s" % (s, a.get("why")) for s, _sp, _b, _af, a in plan]
        return res

    for _slug, spot, _before, after, _a in plan:
        spot["note"] = after
    tmp = MACHINES + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(T._dump(data, MACHINES))
    os.replace(tmp, MACHINES)
    res["done"] = ["%s %s" % (s, a.get("why")) for s, _sp, _b, _af, a in plan]
    return res


# --- 関所 -----------------------------------------------------------------

def check_problems(machines=None) -> list:
    """★止める理由だけ★（手がかりは入れない＝関所を騒がしくしない）"""
    rows, _ = survey(machines)
    return ["%s（%s%s）: %s" % (r["slug"], r["mode_label"],
                                ("・" + r["rate_label"]) if r["rate_label"] else "",
                                " ／ ".join(r["problems"]))
            for r in rows if r["problems"]]


# --- 自己試験 --------------------------------------------------------------

def _selftest() -> int:
    ok = [0, 0]

    def t(name, cond):
        ok[1] += 1
        if cond:
            ok[0] += 1
            print("OK   " + name)
        else:
            print("NG   " + name)

    t("★消しただけなら通る★", _is_subsequence("あいう", "あXいYうZ"))
    t("★1文字でも足したら通さない★", not _is_subsequence("あいえ", "あいう"))
    t("　順番が入れ替わったら通さない", not _is_subsequence("うあ", "あいう"))
    t("　空は部分列（別の検査で断る）", _is_subsequence("", "あ"))

    sw = {"slug": "zzz", "checker": {
        "exchangeRates": [{"key": "eq56", "label": "5.6枚"}],
        "modes": [{"key": "normal", "label": "通常"}],
        "normal": {"caution": 400, "good": 500, "excellent": 600,
                   "note": "5.6枚は500G〜が狙い目。等価400G〜。天井900G+α。"}}}
    t("★選べない呼び名で基準を書いていたら挙げる★",
      any("選べない" in x for x in note_problems(
          sw, sw["checker"]["normal"]["note"], bounds(sw["checker"]["normal"], "eq56"))))
    t("　選べる呼び名と境目だけなら挙げない",
      not note_problems(sw, "5.6枚は500G〜が狙い目。天井900G+α。",
                        bounds(sw["checker"]["normal"], "eq56")))
    t("★★候補・強めの値も正しい★★（good としか比べないと正しい注記まで挙がる）",
      not note_problems(sw, "5.6枚は500G〜が狙い目、400G〜が候補です。",
                        bounds(sw["checker"]["normal"], "eq56")))
    t("★境目のどれとも違う値は挙げる★",
      any("境目" in x for x in note_problems(
          sw, "5.6枚は470G〜が狙い目。", bounds(sw["checker"]["normal"], "eq56"))))
    t("　天井のような「ここから」でない数値は挙げない",
      not note_problems(sw, "天井900G+α。5.6枚は500G〜が狙い目。",
                        bounds(sw["checker"]["normal"], "eq56")))
    t("★★範囲の書き方を着席の基準と読まない★★"
      "（「天井600〜699pt」の600を『ここから』と読んで正しい注記を挙げていた）",
      not note_problems(sw, "天井600〜699pt+αに短縮。5.6枚は500G〜が狙い目。",
                        bounds(sw["checker"]["normal"], "eq56")))
    t("　（対照）範囲の右側が「ここから」なら、ちゃんと挙げる",
      any("境目" in x for x in note_problems(
          sw, "5.6枚は350〜470G〜が狙い目。",
          bounds(sw["checker"]["normal"], "eq56"))))
    t("　ゾーンの範囲も挙げない",
      not note_problems(sw, "5.6枚は500G〜が狙い目。301〜400Gにゾーン。",
                        bounds(sw["checker"]["normal"], "eq56")))

    nosw = {"slug": "zzz2", "checker": {
        "modes": [{"key": "normal", "label": "通常"}],
        "normal": {"caution": 730, "good": 900, "note": "x"}}}
    t("★切替が無い機種は、どの呼び名でも挙げる★",
      any("選べない" in x or "選べません" in x or "選べ" in x
          for x in note_problems(
              nosw, "等価730G〜が狙い目。", bounds(nosw["checker"]["normal"], ""))))

    t("★★語の名簿をやめた★★＝行動をすすめる語が無くても、"
      "境目と違う「ここから」は挙げる（バキの「より浅く見られます」が通っていた）",
      any("境目" in x for x in note_problems(
          sw, "5.6枚は470G〜。", bounds(sw["checker"]["normal"], "eq56"))))

    # --- ★手がかり★（止める判断には使わない） -------------------------
    off = {"slug": "zzz3", "checker": {
        "modes": [{"key": "normal", "label": "通常"}],
        "normal": {"good": 300, "note": "170G〜で狙い目。スルー4回以降は即打ち可。"},
        "suru": {"_disabled": "止めてある", "good": 3}}}
    t("★画面に出ない軸の話は手がかりに出す★",
      any("画面に出ていない軸" in x for x in note_hints(
          off, off["checker"]["normal"]["note"], "normal")))
    t("★★手がかりは関所を止めない★★"
      "（止める側に入れたら、正しい注記まで37本挙がった）",
      not any("軸" in x for x in note_problems(
          off, off["checker"]["normal"]["note"], {"good": 170}, "normal")))
    t("　（対照）軸が生きていれば、画面に出ない軸としては挙げない",
      not any("画面に出ていない軸" in x for x in note_hints(
          {"slug": "z", "checker": {
              "modes": [{"key": "normal"}, {"key": "suru"}],
              "normal": {"good": 300}, "suru": {"good": 3}}},
          "170G〜で狙い目。スルー4回以降は即打ち可。", "normal")))
    t("★いま見ているモードと違う軸に数がついていたら手がかりに出す★"
      "（バキ「2スルー時はより浅く見られます」）",
      any("違う軸" in x for x in note_hints(
          {"slug": "z", "checker": {
              "modes": [{"key": "cz"}, {"key": "suru"}],
              "cz": {"good": 300}, "suru": {"good": 2}}},
          "5.6枚交換ならCZ300Gから狙い目。2スルー時はより浅く見られます。", "cz")))
    t("　その軸を見ているときは挙げない（周期が単位の機種）",
      not note_hints(
          {"slug": "z", "checker": {
              "modes": [{"key": "cycle"}], "cycle": {"good": 8}}},
          "10周期天井。5.6枚持ちは8周期〜が狙い目。", "cycle"))

    t("★止めてある軸の注記は、読者に届かないので数えない★",
      not any(s["mode"] == "suru" for s in shown_notes(
          {"slug": "z", "checker": {
              "modes": [{"key": "normal", "label": "通常"},
                        {"key": "suru", "label": "スルー"}],
              "normal": {"good": 1, "note": "あ"},
              "suru": {"_disabled": "止めた", "good": 1, "note": "い"}}})))
    t("★★交換率ごとの注記が、モード直下の注記を上書きする★★"
      "（画面の決まりと同じ・ここを取り違えると件数が狂う）",
      [s["note"] for s in shown_notes(
          {"slug": "z", "checker": {
              "exchangeRates": [{"key": "eq56", "label": "5.6枚"}],
              "modes": [{"key": "normal", "label": "通常"}],
              "normal": {"good": 1, "note": "土台",
                         "byRate": {"eq56": {"note": "枝"}}}}})] == ["枝"])
    t("　鍵が無ければ土台の注記が出る",
      [s["note"] for s in shown_notes(
          {"slug": "z", "checker": {
              "exchangeRates": [{"key": "eq56", "label": "5.6枚"}],
              "modes": [{"key": "normal", "label": "通常"}],
              "normal": {"good": 1, "note": "土台",
                         "byRate": {"eq56": {"good": 2}}}}})] == ["土台"])

    # --- ★土台の注記は、それが出る交換率ぜんぶで見る★（Codexの指摘4） ---
    multi = {"slug": "zzz4", "checker": {
        "exchangeRates": [{"key": "eq56", "label": "5.6枚"},
                          {"key": "rate55", "label": "6.0枚"}],
        "modes": [{"key": "normal", "label": "通常"}],
        "normal": {"good": 500, "note": "500G〜が狙い目。",
                   "byRate": {"eq56": {"good": 500},
                              "rate55": {"good": 600}}}}}
    _base = _locate(multi, "normal", "")
    t("★土台の注記を出す枝を全部返す★",
      [st["rate"] for st in displaying_branches(multi, _base)]
      == ["eq56", "rate55"])
    t("　枝が自分の注記を持っていれば、その枝だけ",
      [st["rate"] for st in displaying_branches(
          {"slug": "z", "checker": {
              "exchangeRates": [{"key": "eq56", "label": "5.6枚"},
                                {"key": "rate55", "label": "6.0枚"}],
              "modes": [{"key": "normal"}],
              "normal": {"good": 1,
                         "byRate": {"eq56": {"note": "あ"},
                                    "rate55": {"good": 2}}}}},
          {"note": "あ"})] == [])
    t("★★片方の交換率でだけ食い違う注記を、見逃さない★★"
      "（土台の境目だけで見ていたら通っていた）",
      note_problems(multi, "500G〜が狙い目。",
                    [st for st in displaying_branches(multi, _base)
                     if st["rate"] == "rate55"][0]["bounds"]))

    t("★★数値は「まとまり」で数える★★"
      "（消えた50が150に含まれているだけで「残っている」と読んでいた）",
      _num_counts("150G")["50"] == 0 and _num_counts("50G/150G")["50"] == 1)

    # --- ★スルー・周期の「行」の注記も画面に出る★（Codexの2回目の指摘1） ---
    rows_m = {"slug": "zzz5", "checker": {
        "unit": "G",
        "modes": [{"key": "suru", "label": "スルー"}],
        "suru": {"suru": [
            {"count": 0, "good": 700, "note": "0スルーは700G〜。"},
            {"count": 2, "good": 600, "note": "2スルーは600G〜。"}]}}}
    t("★★行ごとの注記を数える★★"
      "（直す前は1本も見ておらず、出ないモード直下の注記を数えていた）",
      sorted(st["note"] for st in shown_notes(rows_m))
      == ["0スルーは700G〜。", "2スルーは600G〜。"])
    t("　行の境目で見る（土台の境目ではない）",
      [st["bounds"].get("good") for st in shown_notes(rows_m)] == [700, 600])
    t("★行を指せる（count）★",
      _locate(rows_m, "suru", "", 2) is not None
      and _locate(rows_m, "suru", "", 5) is None)
    t("★★行を持つモードでは、モード直下の注記は画面に出ない★★",
      not [st for st in shown_notes({"slug": "z", "checker": {
          "unit": "G", "modes": [{"key": "suru", "label": "ス"}],
          "suru": {"note": "出ない注記です。",
                   "suru": [{"count": 0, "good": 700}]}}})])

    # --- ★呼び名と値の突き合わせ★（候補の値を「狙い目」と書いていないか） ---
    _bd = {"caution": 360, "good": 500, "excellent": 900}
    t("★★候補の値を「狙い目」と書いていたら止める★★"
      "（バベルは判定900Gに対し注記730G＝160G早く座らせていた）",
      mislabeled("設定変更後は900G+αに短縮。360G〜が狙い目。", _bd, "G"))
    t("　狙い目の値ならもちろん通る",
      not mislabeled("設定変更後は900G+αに短縮。500G〜が狙い目。", _bd, "G"))
    t("　「候補」と書いてある文は、候補の値で正しい",
      not mislabeled("360G〜が候補です。", _bd, "G"))
    t("★★「前提が違う場合」の断り書きは止めない★★"
      "（カウンターの限界の説明であって、いまの枝の基準ではない）",
      not mislabeled("★この判定は「1スルー以降」を前提にしています★"
                     "（0スルー台は360G〜が狙い目）。", _bd, "G"))
    t("　つなぎ語が無い形も見る（420G基準）",
      mislabeled("6.0枚交換は360G基準。", _bd, "G"))

    t("★★桁数で切らない★★（8周期〜・0Gから を1本も見ていなかった）",
      [n for n, _u in SIT.findall("0Gから狙い目。8周期〜。")] == ["0", "8"])
    t("★★境目が1つも無い枝でも止める★★（直す前は素通りだった）",
      note_problems({"slug": "z", "checker": {"unit": "G",
                                              "modes": [{"key": "normal"}]}},
                    "500G〜から狙い目。", {}))
    t("　別の軸の数値は、境目と比べない（手がかりへ回す）",
      not note_problems({"slug": "z", "checker": {"unit": "G",
                                                  "modes": [{"key": "normal"}]}},
                        "2スルー〜。", {"good": 300}))

    # --- ★書く側★（ここまで1件も試験していなかった） ----------------
    import shutil
    import tempfile

    real = MACHINES
    real_before = T._digest(real)
    tmpdir = tempfile.mkdtemp(prefix="notetext_")
    fake = os.path.join(tmpdir, "machines.json")
    data = [{"slug": "zzz_fake", "name": "試験用",
             "checker": {
                 "exchangeRates": [{"key": "eq56", "label": "5.6枚"}],
                 "modes": [{"key": "normal", "label": "通常"}],
                 "normal": {"caution": 400, "good": 500, "excellent": 600,
                            "note": "5.6枚は500G〜が狙い目。等価470G〜。天井900G+α。",
                            "byRate": {"eq56": {"good": 500}}}}}]

    def _write_fake():
        with io.open(fake, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=1) + "\n")

    def _dec(**kw):
        base = {"schema_version": SCHEMA, "decided_by": ["Claude", "codex"],
                "source_sha256": T._digest(fake),
                # ★470 はこの試験用の機種にしか無いので、落とすと消える★
                #   （その守りは下で別に試す）
                "numbers_removed": [
                    {"slug": "zzz_fake", "n": "470",
                     "why": "選べない呼び名の値なので落とす（試験用）"}],
                "actions": [{"slug": "zzz_fake", "mode": "normal", "rate": "",
                             "before": data[0]["checker"]["normal"]["note"],
                             "after": "5.6枚は500G〜が狙い目。天井900G+α。",
                             "why": "選べない呼び名を落とす",
                             "meaning_why": "2AIで読み比べ、選べない呼び名の"
                                            "着席基準だけを落としました"}]}
        base.update(kw)
        path = os.path.join(tmpdir, "dec.json")
        with io.open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(base, ensure_ascii=False))
        return path

    def _note():
        with io.open(fake, encoding="utf-8") as f:
            return json.load(f)[0]["checker"]["normal"]["note"]

    _keep_n, _keep_t = MACHINES, T.MACHINES
    try:
        globals()["MACHINES"] = fake
        T.MACHINES = fake
        _write_fake()

        t("★書く：通る決定なら注記が変わる★",
          not apply_decision(_dec(), apply_it=True)["problems"]
          and _note() == "5.6枚は500G〜が狙い目。天井900G+α。")

        _write_fake()
        t("★下見では1文字も書かない★",
          not apply_decision(_dec(), apply_it=False)["problems"]
          and _note() == data[0]["checker"]["normal"]["note"])

        _write_fake()
        d = _dec()
        got = json.load(io.open(d, encoding="utf-8"))
        got["actions"][0]["before"] = "いまと違う注記です。"
        io.open(d, "w", encoding="utf-8").write(
            json.dumps(got, ensure_ascii=False))
        t("★いまの注記と before が違えば断る★",
          apply_decision(d, apply_it=True)["problems"]
          and _note() == data[0]["checker"]["normal"]["note"])

        _write_fake()
        t("★★言い換え（部分列でない）は断る★★"
          "＝これが無いと注記がまた手書きに戻る",
          apply_decision(_dec(actions=[dict(
              slug="zzz_fake", mode="normal", rate="",
              before=data[0]["checker"]["normal"]["note"],
              after="5.6枚は500Gから狙い目です。天井900G+α。",
              why="言い換えてみる",
              meaning_why="意味は変えていないつもりです（試験用）")]),
              apply_it=True)["problems"])

        _write_fake()
        t("★選べない呼び名を残す決定は断る★",
          apply_decision(_dec(actions=[dict(
              slug="zzz_fake", mode="normal", rate="",
              before=data[0]["checker"]["normal"]["note"],
              after="等価470G〜。天井900G+α。",
              why="わざと残す",
              meaning_why="選べない呼び名を残す試験用の決定です")]),
              apply_it=True)["problems"])

        _write_fake()
        t("★境目と違う「ここから」を残す決定は断る★",
          apply_decision(_dec(actions=[dict(
              slug="zzz_fake", mode="normal", rate="",
              before=data[0]["checker"]["normal"]["note"],
              after="5.6枚は470G〜が狙い目。天井900G+α。",
              why="わざと残す",
              meaning_why="境目と違う値を残す試験用の決定です")]),
              apply_it=True)["problems"])

        _write_fake()
        t("★判断者が1つなら断る★",
          apply_decision(_dec(decided_by=["Claude"]),
                         apply_it=True)["problems"]
          and _note() == data[0]["checker"]["normal"]["note"])

        _write_fake()
        t("★機種データが変わっていたら断る★",
          apply_decision(_dec(source_sha256="0" * 64),
                         apply_it=True)["problems"])

        _write_fake()
        t("★注記を空にする決定は断る★",
          apply_decision(_dec(actions=[dict(
              slug="zzz_fake", mode="normal", rate="", after="",
              before=data[0]["checker"]["normal"]["note"],
              why="空にしてみる",
              meaning_why="空にする試験用の決定です")]), apply_it=True)["problems"])

        _write_fake()
        t("★★1件でも通らなければ、通る側も書かない★★",
          apply_decision(_dec(actions=[
              dict(slug="zzz_fake", mode="normal", rate="",
                   before=data[0]["checker"]["normal"]["note"],
                   after="5.6枚は500G〜が狙い目。天井900G+α。",
                   why="通る側", meaning_why="選べない呼び名を落とす試験用です"),
              dict(slug="zzz_fake", mode="normal", rate="",
                   before="いまと違う注記です。", after="あ。",
                   why="通らない側", meaning_why="断られる側の試験用です")]),
              apply_it=True)["problems"]
          and _note() == data[0]["checker"]["normal"]["note"])

        _write_fake()
        t("★消えて公開データから無くなる数値は、理由が要る★",
          apply_decision(_dec(actions=[dict(
              slug="zzz_fake", mode="normal", rate="",
              before=data[0]["checker"]["normal"]["note"],
              after="5.6枚は500G〜が狙い目。",
              why="天井の数値ごと落とす試験用",
              meaning_why="天井の数値まで落とす試験用の決定です")]),
              apply_it=True)["problems"])

        _write_fake()
        t("　理由を名指しすれば通る",
          not apply_decision(_dec(
              numbers_removed=[
                  {"slug": "zzz_fake", "n": "470",
                   "why": "選べない呼び名の値なので落とす（試験用）"},
                  {"slug": "zzz_fake", "n": "900",
                   "why": "天井も落とす試験用の理由です（十分な長さ）"}],
              actions=[dict(
                  slug="zzz_fake", mode="normal", rate="",
                  before=data[0]["checker"]["normal"]["note"],
                  after="5.6枚は500G〜が狙い目。",
                  why="天井の数値ごと落とす試験用",
                  meaning_why="天井の数値まで落とす試験用の決定です")]),
              apply_it=True)["problems"])
    finally:
        globals()["MACHINES"] = _keep_n
        T.MACHINES = _keep_t
        shutil.rmtree(tmpdir, ignore_errors=True)

    t("★★試験が本物の機種データを1文字も触っていない★★"
      "（写しへの向け直しが漏れると本物を汚す）",
      T._digest(real) == real_before)

    print("\n%d/%d 合格" % (ok[0], ok[1]))
    return 0 if ok[0] == ok[1] else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--file")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    if a.file:
        r = apply_decision(a.file, apply_it=a.apply)
        for x in r["problems"]:
            print("  ✗ " + x)
        if r["problems"]:
            print("★書きませんでした★")
            return 1
        print("★%s★ %d 件" % ("書きました" if a.apply else "下見", len(r["done"])))
        for d in r["done"]:
            print("  " + d)
        return 0
    if a.check:
        bad = check_problems()
        for x in bad:
            print("  ✗ " + x)
        if bad:
            print("★注記が %d 本、カウンターの判定と噛み合っていません★" % len(bad))
            return 1
        print("★注記はカウンターの判定と噛み合っています★")
        return 0
    rows, total = survey()
    print("画面に出る注記 %d 本のうち、読むべきもの %d 本（機種 %d）"
          % (total, len(rows), len({r["slug"] for r in rows})))
    for r in rows:
        print("\n== %-20s %s ／ %s%s"
              % (r["slug"], r["name"], r["mode_label"],
                 ("・" + r["rate_label"]) if r["rate_label"] else ""))
        print("   境目: %s" % r["bounds"])
        print("   注記: %s" % r["note"])
        for g in r["problems"]:
            print("   → %s" % g)
        for g in r.get("hints") or []:
            print("   ・手がかり（止めない）: %s" % g)
    return 0


if __name__ == "__main__":
    sys.exit(main())

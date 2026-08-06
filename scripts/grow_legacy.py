#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""grow_legacy.py — 旧方式の先行記事に、裏取りできた材料を書き足す。

★何のための道具か（2026-08-06）★
  8月3日に導入された7機種の記事が「当サイトでは未確認です」のまま残っていた。
  名鑑の索引を直した結果、型式名・機械割・天井などが**2出典一致で採れる**ように
  なったので、その分だけを記事へ入れる。

★やること／やらないこと★
  やる   : 未確認の箱を、2出典で一致した事実に差し替える
           足した事実と食い違う「まだ分かりません」を落とす
  やらない: 値を作る（材料に無いものは書かない）
           迷ったら書く（★決められない時は止める★）

★止める（fail-closed）ところ★（2026-08-06・Codex125回目の指摘を反映）
  ・すでに書いてある同じ項目の値が**違う**（どちらが正しいか機械には決められない）
  ・落とそうとした文に、**まだ分からない別の話**が混じっている
  ・書く直前にファイルが変わっていた（誰かが同時に触った）
  ・記事の中身が別機種だった（slug・機種名が名簿と合わない）

使い方:
    python scripts/grow_legacy.py                 # 対象を見る
    python scripts/grow_legacy.py --slug xxx      # 1機種だけ（下見）
    python scripts/grow_legacy.py --slug xxx --apply
    python scripts/grow_legacy.py --selftest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import page_decision as _pd              # noqa: E402
import safe_json as _sj                  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DETAILS = os.path.join(BASE, "assets", "data", "machine-details")
PENDING = "当サイトでは未確認です。確認でき次第、この欄に掲載します。"
SOURCED = "（出典2件で一致）"

# 天井の種類ごとの見出し（★材料に無い言葉を足さない★）
_KIND_JP = {"GAME": "ゲーム数天井", "CYCLE": "周期天井",
            "POINT": "ポイント天井", "THROUGH": "スルー天井"}

# 項目キー → 「解析待ちの項目」の箇条書きで使われている言葉
#   ★言葉は厳しくする★（「・リセット時の天井短縮」を巻き込まないため）
_PENDING_WORDS = {"型式名": ("型式名",),
                  "天井GAME": ("天井ゲーム数",),
                  "天井CYCLE": ("周期天井",),
                  "天井POINT": ("ポイント天井",),
                  "天井THROUGH": ("スルー天井",)}
# 項目キー → 打ち消し文を探す時の言葉
#   ★天井は種類ごとに厳密に分ける★（2026-08-06・Codex126回目 #1）
#     以前はどの天井にも「天井」を入れていたので、スルー天井が分かっただけで
#     「天井ゲーム数」の未判明表示まで消えた（やじきたで実際に起きた）。
_SENT_WORDS = {"機械割": ("機械割", "出玉率"), "型式名": ("型式名",),
               "天井GAME": ("天井ゲーム数", "ゲーム数天井", "G数天井"),
               "天井CYCLE": ("周期天井", "天井周期"),
               "天井POINT": ("ポイント天井", "天井ポイント"),
               "天井THROUGH": ("スルー天井", "天井スルー", "スルー回数")}
# 天井を指す言葉の全体（★これが1つも無いのに「天井」だけある＝どの天井か決まらない★）
_CEILING_WORDS = tuple(w for k, ws in _SENT_WORDS.items()
                       if k.startswith("天井") for w in ws)

# ★「まだ分かりません」を表す言い方★
_UNKNOWN_MARK = ("判明していない", "判明していません", "判明しておらず",
                 "公開されていません", "解析判明後", "判明次第", "未解析",
                 "未判明", "調査中", "揃っていません", "確認できていません",
                 "分かっていません", "不明です", "解析待ち")
# ★まだ分からない別の話★（これが混じる文は勝手に落とさない）
_OTHER_TOPICS = ("狙い目", "リセット", "短縮", "ヤメ", "純増", "継続率",
                 "突入率", "設定示唆", "終了画面", "小役", "コイン単価",
                 "設定段階", "設定別", "有利区間", "引き戻し", "初当り")

_ENUM = re.compile(r"^\*\*(?P<items>[^*]+)\*\*\s*[：:]\s*解析判明次第追記します。?$")
_SEP = re.compile(r"[・／/、,]")
_LABELED = re.compile(r"^\*\*(?P<label>[^*]+)\*\*\s*[：:]\s*(?P<value>.+)$")


class Halt(Exception):
    """決められないので書かずに止める。"""


def _sha(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def targets(slug: str | None = None) -> list:
    ms = _sj.read_json(os.path.join(BASE, "assets", "data", "machines.json"),
                       expect=(dict, list))
    ms = ms["machines"] if isinstance(ms, dict) else ms
    out = []
    for m in ms:
        if slug and m.get("slug") != slug:
            continue
        try:
            if _pd.machine_class(m) == "LEGACY_PREVIEW":
                out.append(m)
        except Exception:                 # noqa: BLE001
            continue
    return out


# --------------------------------------------------------------- 材料 → 行

def _num(x):
    """数値だけを通す（★NaN・無限大は数値として扱わない★・Codex126回目 #7）。"""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return x if math.isfinite(x) else None


def ceiling_items(material: dict) -> list:
    """天井の材料 → [(項目キー, 見出し, 値の文, 節)]（★採用ぶんだけ★）。"""
    out = []
    for c in ((material.get("ceilings") or {}).get("adopted") or []):
        kind = c.get("kind")
        jp = _KIND_JP.get(kind)
        amount, unit = _num(c.get("amount")), str(c.get("unit") or "").strip()
        if not jp or amount is None or amount <= 0 or not unit:
            continue                      # ★空の値からは行を作らない★
        counted = str(c.get("counted") or "").strip()
        value = f"{amount:g}{unit}" + (f"（{counted}）" if counted else "")
        ben = str(c.get("benefit") or "").strip()
        if ben:
            value += f" → {ben}"
        out.append((f"天井{kind}", jp, value, "天井・恩恵"))
    return out


def spec_items(material: dict) -> list:
    """基本スペックの材料 → [(項目キー, 見出し, 値の文, 節)]。"""
    ad = material.get("adopted") or {}
    out = []
    mc = (ad.get("model_code") or {}).get("value")
    if isinstance(mc, str) and mc.strip():
        out.append(("型式名", "型式名", mc.strip(), "基本スペック"))
    rng = (ad.get("payout_range") or {}).get("value") or {}
    low, high = _num(rng.get("low")), _num(rng.get("high"))
    if low is not None and high is not None and 50 <= low <= high <= 200:
        out.append(("機械割", "機械割", f"{low}%〜{high}%", "基本スペック"))
    g50 = ((ad.get("games_per_50") or {}).get("value") or {}).get("games")
    g50 = _num(g50)
    if g50 is not None and 10 <= g50 <= 120:
        out.append(("G数50", "50枚あたりのゲーム数", f"約{g50:g}G", "基本スペック"))
    return out


def _dedupe(cand: list) -> list:
    """★同じ項目が2つ以上あれば止める★（2026-08-06・Codex126回目 #2）

    同じ種類の天井が「6スルー」「8スルー」の2件来ると、本文には両方載り、
    早見表には片方だけが載る。どちらが正しいかは機械には決められない。
    """
    seen, out = {}, []
    for item in cand:
        key, label, value = item[0], item[1], item[2]
        if key in seen:
            if seen[key] != value:
                raise Halt(f"同じ項目に違う値が2つあります: {label}"
                           f"（「{seen[key]}」と「{value}」）")
            continue                      # まったく同じなら1件にまとめる
        seen[key] = value
        out.append(item)
    return out


def _value_of(line: str, label: str):
    """本文の1行が同じ見出しなら、その値を返す（違う見出しなら None）。"""
    m = _LABELED.match(str(line).strip())
    if not m or m.group("label").strip() != label:
        return None
    return m.group("value").replace(SOURCED, "").strip().rstrip("。")


# ------------------------------------------------------- 食い違いを落とす

def _vague_ceiling(text: str, keys: set) -> bool:
    """「天井」とだけ書かれていて、どの天井を指すか決まらないか。

    ★天井が1種類でも分かっている時だけ問題になる★（Codex126回目 #1）。
    種類が書いてあれば決められるので、ここでは False。
    """
    if not any(k.startswith("天井") for k in keys):
        return False
    return "天井" in text and not any(w in text for w in _CEILING_WORDS)


def _residual_ceiling(text: str, keys: set) -> bool:
    """分かった天井の言葉を取り除いても、まだ「天井」の話が残るか。

    ★2026-08-06・Codex127回目 #2★
      「ゲーム数天井は判明しましたが、ほかの天井は未判明です。」のように、
      分かった天井と分からない天井が同じ文にあると、消してはいけない。
    """
    if not any(k.startswith("天井") for k in keys):
        return False
    t = text
    for k in keys:
        for w in _SENT_WORDS.get(k, ()):
            t = t.replace(w, "")
    return "天井" in t


def _removable(sent: str, keys: set) -> bool:
    """その文が『いま分かった事実』を「まだ分からない」と言っているか。"""
    if not any(w in sent for w in _UNKNOWN_MARK):
        return False
    return any(w in sent for k in keys for w in _SENT_WORDS.get(k, ()))


def _enum_rest(text: str, keys: set):
    """「A・B：解析判明次第追記します」から、分かった項目を抜く。

    戻り値は (残した文 or "") ／ 形が違えば None。
    """
    m = _ENUM.match(text.strip())
    if not m:
        return None
    raw = m.group("items")
    sep = (_SEP.search(raw).group(0) if _SEP.search(raw) else "・")
    keep = []
    for x in _SEP.split(raw):
        if any(w in x for k in keys for w in _SENT_WORDS.get(k, ())):
            continue                      # 分かった項目なので抜く
        if _vague_ceiling(x, keys):
            # ★どの天井を指すのか決まらない項目は、勝手に扱わない★
            raise Halt(f"どの天井を指すのか決まらない項目があります: {x[:24]}")
        keep.append(x)
    return f"**{sep.join(keep)}**：解析判明次第追記します。" if keep else ""


def resolve_contradictions(after: dict, keys: set, topics=()) -> list:
    """食い違う文を落とす計画を作る（★after を直接は書き換えない★）。

    戻り値は [(節の番号, 元の文, 直した文 or None)]。
    ★決められない時は Halt★（黙って消さない・黙って残さない）。
    """
    edits = []
    if not keys:
        return edits
    for i, sec in enumerate(after.get("sections") or []):
        if not isinstance(sec.get("body"), list):
            continue
        title = str(sec.get("title") or "")
        listing = ("解析待ち" in title) or ("未確認" in title)
        for b in sec["body"]:
            t = str(b)
            if listing and t.strip().startswith("・"):
                hit = any(w in t for k in keys
                          for w in _PENDING_WORDS.get(k, ()))
                if hit and _residual_ceiling(t, keys):
                    raise Halt(f"分かった天井と分からない天井が同じ項目にあります: "
                               f"{t[:28]}")
                if hit and any(w in t for w in topics):
                    raise Halt(f"まだ分からない話が同じ項目にあります: {t[:28]}")
                if not hit and _vague_ceiling(t, keys)                         and not any(w in t for w in _OTHER_TOPICS):
                    # ★「・リセット時の天井短縮」のような別の話は、そのまま残す★
                    raise Halt(f"どの天井を指すのか決まらない項目があります: {t[:28]}")
                if hit:
                    edits.append((i, t, None))
                continue
            rest = _enum_rest(t, keys)
            if rest is not None:
                if rest.strip() != t.strip():
                    edits.append((i, t, rest or None))
                continue
            sents = [s for s in re.split(r"(?<=。)", t) if s.strip()]
            drop = [s for s in sents if _removable(s, keys)]
            for s2 in sents:
                if s2 in drop or not any(w in s2 for w in _UNKNOWN_MARK):
                    continue
                if _vague_ceiling(s2, keys):
                    raise Halt("どの天井を指すのか決まらない文があります: "
                               + s2[:44])
            if not drop:
                continue
            # ★別の「まだ分からない話」が混じる文は自分で決めない★
            for s in drop:
                if any(w in s for w in tuple(_OTHER_TOPICS) + tuple(topics)):
                    raise Halt(f"落としてよいか決められない文があります: {s[:48]}")
                if _residual_ceiling(s, keys):
                    raise Halt("分かった天井と分からない天井が同じ文にあります: "
                               + s[:44])
            new = "".join(s for s in sents if s not in drop).strip()
            edits.append((i, t, new or None))
    return edits


def _apply_edits(after: dict, edits: list, adds: dict, facts=()) -> dict:
    """計画どおりに本文を組み立てる。

    ★未確認の断りは「その節に事実があるか」で外す★（Codex127回目 #1）
      以前は「今回足したか」で判断していたので、前回すでに書いてある節では
      事実と「未確認です」が並んだままになった。
    """
    drop = {(i, b): a for i, b, a in edits}
    has_fact = set(adds or {}) | set(facts or ())
    for i, sec in enumerate(after.get("sections") or []):
        if not isinstance(sec.get("body"), list):
            continue
        body = []
        for b in sec["body"]:
            key = (i, str(b))
            if key in drop:
                if drop[key]:
                    body.append(drop[key])
                continue
            if str(b).strip() == PENDING and i in has_fact:
                continue                  # ★中身が入ったら断りは外す★
            body.append(b)
        sec["body"] = adds.get(i, []) + body
        if not sec["body"]:
            sec["body"] = [PENDING]       # ★節を空にしない★
    return after


# -------------------------------------------------------------- 早見表

# 早見表の「まだ分かりません」を表す値
_BOX_PENDING = ("解析待ち", "未確認", "調査中", "-", "－", "")
# 早見表に出す天井の優先順（★1つだけ出す欄なので順番を決めておく★）
_BOX_ORDER = ("天井GAME", "天井CYCLE", "天井POINT", "天井THROUGH")


def _apply_boxes(detail: dict, boxes: list) -> dict:
    """早見表の計画を当てる（★計画にある欄だけ★）。"""
    for i, before, after_v in boxes:
        box = (detail.get("summaryBoxes") or [])[i]
        if str(box.get("value") or "").strip() != str(before):
            raise Halt("早見表が計画と違います（先に誰かが書き換えた可能性）")
        box["value"] = after_v
    return detail


def _plan_summary(detail: dict, ceilings: list, present: set) -> list:
    """★早見表の「天井：解析待ち」を、記事に載せた天井にそろえる★

    2026-08-06・Codex125回目 #1。本文に「1200G」と書きながら、同じページの
    早見表が「解析待ち」のままだった。読者には両方見える。
    """
    got = {k: v for k, _lb, v, _s in ceilings if k in present}
    if not got:
        return []
    key = next((k for k in _BOX_ORDER if k in got), None)
    if key is None:
        return []
    value = got[key].split(" → ")[0]      # 恩恵は長いので欄には出さない
    if len(got) > 1:
        value += " ほか"
    out = []
    for i, box in enumerate(detail.get("summaryBoxes") or []):
        if str(box.get("label") or "").strip() != "天井":
            continue
        before = str(box.get("value") or "").strip()
        if before == value:
            continue
        if before not in _BOX_PENDING:
            # ★すでに別の天井が書いてある＝どちらが正しいか決められない★
            raise Halt(f"早見表にすでに別の天井が書かれています（{before}）")
        out.append((i, before, value))
    return out


# ------------------------------------------------------------------ 計画

def plan(machine: dict, material: dict, detail: dict) -> dict:
    """記事に足す内容を決める（★書き込まない★・決められなければ Halt★）。"""
    if str(detail.get("slug") or "") != str(machine.get("slug")) or \
            str(detail.get("name") or "") != str(machine.get("name")):
        raise Halt("記事の中身が名簿と合いません（slug・機種名の不一致）")
    after = json.loads(json.dumps(detail))
    secs = after.get("sections") or []
    idx = {}
    for i, sec in enumerate(secs):        # ★同名の節があれば決めない★（#6）
        idx.setdefault(str(sec.get("title")), []).append(i)
    cand = _dedupe(ceiling_items(material) + spec_items(material))

    present, adds, added_lines, facts = set(), {}, [], set()
    for key, label, value, want in cand:
        same = False
        for i, s in enumerate(secs):      # ★全部の節を見る★（同じ見出しの重複防止）
            for b in (s.get("body") or []):
                got = _value_of(b, label)
                if got is None:
                    continue
                if got == value:
                    same = True
                    facts.add(i)          # ★この節にはもう事実がある★
                else:
                    raise Halt(f"すでに違う値が書かれています: {label} "
                               f"（記事「{got}」／材料「{value}」）")
        if same:
            present.add(key)              # もう書いてある
            continue
        if want not in idx:
            # ★置く節が無い＝記事に載らない★
            #   載っていない事実で打ち消し文を消すと、値がどこにも無いまま
            #   「まだ分かりません」だけが消える（2026-08-06・Codex125回目 #6）
            continue
        if len(idx[want]) != 1:
            raise Halt(f"同じ名前の節が{len(idx[want])}つあり、どこへ書くか決められません"
                       f"（{want}）")
        present.add(key)
        line = f"**{label}**：{value}{SOURCED}"
        adds.setdefault(idx[want][0], []).append(line)
        added_lines.append(f"{want}: {line}")

    # ★恩恵が分かっていない天井があるなら「恩恵」の話は守る★（Codex127回目 #2）
    topics = set()
    for c in ((material.get("ceilings") or {}).get("adopted") or []):
        if not str(c.get("benefit") or "").strip():
            topics.add("恩恵")
        if not str(c.get("counted") or "").strip():
            topics.add("何回")
    # ★食い違いを落としてよいのは「記事に載っている事実」だけ★
    edits = resolve_contradictions(after, present, topics)
    after = _apply_edits(after, edits, adds, facts)
    boxes = _plan_summary(after, _dedupe(ceiling_items(material)), present)
    after = _apply_boxes(after, boxes)
    return {"slug": machine["slug"], "detail": after, "before": detail,
            "added": added_lines, "edits": edits, "boxes": boxes,
            "adds": adds, "facts": sorted(facts),
            "added_lines": [x.split(": ", 1)[1] for x in added_lines]}


def check(before: dict, after: dict, edits=(), added_lines=(), boxes=(),
          adds=None, facts=()) -> list:
    """★計画どおりに組み立て直して、完全に一致するか確かめる★

    2026-08-06・Codex126回目 #5。以前は「知らない文が増えていないか」しか
    見ていなかったので、**計画したのに実行されていない**（早見表を直し忘れた・
    足すはずの行が無い・別の節に入った）を通してしまった。
    ここでは before に計画を当て直し、**一字一句同じ**であることを求める。
    """
    ng = []
    # --- 計画そのものの妥当性 ---
    b_secs = before.get("sections") or []
    for i, orig, _new in (edits or []):
        if i >= len(b_secs) or str(orig) not in [str(x) for x in
                                                 (b_secs[i].get("body") or [])]:
            ng.append(f"消す予定の文が{i}番目の節にありません（{str(orig)[:28]}…）")
    for line in (added_lines or []):
        if not _LABELED.match(str(line)) or SOURCED not in str(line):
            ng.append(f"足す行の形が決まりと違います（{str(line)[:32]}…）")
    if ng:
        return ng
    # --- 組み立て直して完全一致 ---
    try:
        expect = _apply_edits(json.loads(json.dumps(before)),
                              list(edits or []), dict(adds or {}),
                              tuple(facts or ()))
        expect = _apply_boxes(expect, list(boxes or []))
    except Exception as e:                # noqa: BLE001
        return [f"計画を組み立て直せません: {e}"]
    if expect == after:
        return []
    e_secs, a_secs = expect.get("sections") or [], after.get("sections") or []
    if len(e_secs) != len(a_secs):
        return ["節の数が計画と違います"]
    for i, (es, as_) in enumerate(zip(e_secs, a_secs)):
        if str(es.get("title")) != str(as_.get("title")):
            ng.append(f"{i}番目の節の名前が計画と違います")
        eb = [str(x) for x in (es.get("body") or [])]
        ab = [str(x) for x in (as_.get("body") or [])]
        if eb != ab:
            miss = [x for x in eb if x not in ab]
            extra = [x for x in ab if x not in eb]
            if miss:
                ng.append(f"{es.get('title')}: 計画にある文がありません"
                          f"（{miss[0][:30]}…）")
            if extra:
                ng.append(f"{es.get('title')}: 計画にない文があります"
                          f"（{extra[0][:30]}…）")
            if not miss and not extra:
                ng.append(f"{es.get('title')}: 文の並び・個数が計画と違います")
    eb_box = expect.get("summaryBoxes") or []
    ab_box = after.get("summaryBoxes") or []
    if eb_box != ab_box:
        ng.append("早見表が計画と違います")
    return ng or ["計画と中身が違います（本文・早見表以外）"]


# ------------------------------------------------------------------ 実行

def _lock(path: str) -> str:
    """★1つの記事を同時に書かないための鍵★（2026-08-06・Codex126回目 #4）"""
    lock = path + ".lock"
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return lock
        except FileExistsError:
            try:                          # 5分より古い鍵は置き去りとみなす
                if time.time() - os.path.getmtime(lock) > 300:
                    os.remove(lock)
                    continue
            except OSError:
                pass
            raise Halt("ほかの処理が同じ記事を書いています（鍵が取れません）")
    raise Halt("鍵が取れません")


def _write(path: str, detail: dict, sha_before: str) -> None:
    """★鍵の中で「もう一度確かめて→置き換える」★（間に何も挟まない）

    2026-08-06・Codex126回目 #4。以前は確かめたあとに import を挟んでいたので、
    その隙に誰かが書いた内容を上書きできた。
    """
    text = json.dumps(detail, ensure_ascii=False, indent=1) + "\n"
    lock = _lock(path)
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())          # ★中身が確実に書けてから★
        if _sha(path) != sha_before:      # ★置き換える直前に確かめる★
            raise Halt("書く直前にファイルが変わっていました（同時に触られた可能性）")
        os.replace(tmp, path)
    finally:
        for x in (tmp, lock):
            try:
                os.remove(x)
            except OSError:
                pass


def run(slug: str, apply_it: bool, gather=None) -> dict:
    ms = targets(slug)
    if not ms:
        return {"slug": slug, "problems": ["対象ではありません（旧方式の先行記事のみ）"]}
    m = ms[0]
    if gather is None:
        import add_machine_run as _amr
        gather = _amr.gather
    got = gather(m["name"])
    mat = got.get("material") or {}
    if not mat:
        return {"slug": slug, "problems": ["材料を集められません: "
                                           + " / ".join(got.get("problems") or [])[:160]]}
    path = os.path.join(DETAILS, f"{slug}.json")
    sha_before = _sha(path)
    detail = _sj.read_json(path, expect=dict)
    try:
        pl = plan(m, mat, detail)
    except Halt as e:
        return {"slug": slug, "problems": [f"★止めました★ {e}"]}
    ng = check(pl["before"], pl["detail"], pl["edits"], pl["added_lines"],
               pl["boxes"], pl["adds"], pl["facts"])
    if ng:
        return {"slug": slug, "problems": ng}
    res = {"slug": slug, "added": pl["added"],
           "boxes": [f"早見表 天井：{x[1]} → {x[2]}" for x in pl["boxes"]],
           "removed": [b for _i, b, a in pl["edits"] if a is None],
           "rewrote": [(b, a) for _i, b, a in pl["edits"] if a],
           "wrote": False, "problems": []}
    if apply_it and (pl["added"] or pl["edits"] or pl["boxes"]):
        try:
            _write(path, pl["detail"], sha_before)
        except Halt as e:
            return {"slug": slug, "problems": [f"★止めました★ {e}"]}
        res["wrote"] = True
    return res


# ------------------------------------------------------------------ selftest

def selftest() -> int:                    # noqa: C901
    ok, ran = True, [0]

    def t(name, cond):
        nonlocal ok
        ran[0] += 1
        print(("✅ " if cond else "❌ ") + name)
        ok = ok and bool(cond)

    MAT = {"adopted": {"model_code": {"value": "L機/1"},
                       "payout_range": {"value": {"low": 97.0, "high": 110.0}},
                       "games_per_50": {"value": {"games": 32}}},
           "ceilings": {"adopted": [
               {"kind": "THROUGH", "amount": 6, "unit": "スルー",
                "counted": "CZ", "benefit": ""}]}}
    t("★★採用された材料だけを行にする★★",
      ceiling_items(MAT) == [("天井THROUGH", "スルー天井", "6スルー（CZ）", "天井・恩恵")]
      and len(spec_items(MAT)) == 3)
    t("★★値が欠けた材料からは行を作らない★★",
      ceiling_items({"ceilings": {"adopted": [
          {"kind": "GAME", "amount": None, "unit": "G"},
          {"kind": "GAME", "amount": 0, "unit": "G"}]}}) == []
      and spec_items({"adopted": {"model_code": {"value": None},
                                  "payout_range": {"value": {"low": None,
                                                             "high": 110}}}}) == [])
    t("　あり得ない数字は採らない（機械割300%・50枚1G）",
      spec_items({"adopted": {"payout_range": {"value": {"low": 97, "high": 300}},
                              "games_per_50": {"value": {"games": 1}}}}) == [])

    MACH = {"slug": "x", "name": "機種X"}

    def D(sections):
        return {"slug": "x", "name": "機種X", "sections": sections}

    d = D([{"title": "天井・恩恵", "body": [PENDING]},
           {"title": "基本スペック", "body": ["**メーカー**：A社"]}])
    pl = plan(MACH, MAT, d)
    t("　足すだけなら通る",
      check(pl["before"], pl["detail"], pl["edits"], pl["added_lines"],
            pl["boxes"], pl["adds"]) == []
      and pl["detail"]["sections"][0]["body"]
      == ["**スルー天井**：6スルー（CZ）" + SOURCED])

    # --- ★同じ項目に違う値があったら止める★（Codex125 #3）
    d2 = D([{"title": "基本スペック",
             "body": ["**機械割**：97.0%〜105.0%"]}])
    halted = False
    try:
        plan(MACH, MAT, d2)
    except Halt as e:
        halted = "すでに違う値" in str(e)
    t("★★すでに違う値が書いてあったら書かずに止める★★", halted)
    d3 = D([{"title": "基本スペック",
             "body": [f"**機械割**：97.0%〜110.0%{SOURCED}"]}])
    pl3 = plan(MACH, MAT, d3)
    t("　同じ値なら二重に書かない",
      not any("機械割" in x for x in pl3["added"]))

    # --- ★別の話が混じる文は自分で決めない★（Codex125 #4）
    d4 = D([{"title": "天井・恩恵", "body": [PENDING]},
            {"title": "狙い目の根拠",
             "body": ["スルー天井は判明していませんので狙い目を出せません。"]}])
    halted4 = False
    try:
        plan(MACH, MAT, d4)
    except Halt as e:
        halted4 = "決められない" in str(e)
    t("★★落としてよいか決められない文があれば止める★★（狙い目の話が混じる）",
      halted4)
    d5 = D([{"title": "天井・恩恵", "body": [PENDING]},
            {"title": "ゲーム性",
             "body": ["スルー天井は判明していません。", "残す文。"]}])
    pl5 = plan(MACH, MAT, d5)
    t("　混ざり物が無ければ、その文だけ落とす",
      pl5["detail"]["sections"][1]["body"] == ["残す文。"]
      and check(pl5["before"], pl5["detail"], pl5["edits"],
                pl5["added_lines"], pl5["boxes"], pl5["adds"]) == [])

    d5b = D([{"title": "天井・恩恵", "body": [PENDING]},
             {"title": "ゲーム性", "body": ["天井は判明していません。"]}])
    halted5b = False
    try:
        plan(MACH, MAT, d5b)
    except Halt as e:
        halted5b = "どの天井" in str(e)
    t("★★どの天井を指すのか決まらない文は、消さずに止める★★"
      "（スルー天井だけ分かった時にG数天井の未判明を消さない）", halted5b)
    t("　種類が書いてあれば取り違えない",
      _enum_rest("**天井ゲーム数・スルー天井**：解析判明次第追記します。",
                 {"天井THROUGH"})
      == "**天井ゲーム数**：解析判明次第追記します。")

    # --- ★同じ項目に違う値が2つ★（Codex126 #2）
    dup = {"ceilings": {"adopted": [
        {"kind": "THROUGH", "amount": 6, "unit": "スルー", "counted": "CZ"},
        {"kind": "THROUGH", "amount": 8, "unit": "スルー", "counted": "CZ"}]}}
    halted_dup = False
    try:
        _dedupe(ceiling_items(dup))
    except Halt as e:
        halted_dup = "違う値が2つ" in str(e)
    t("★★同じ項目に違う値が2つ来たら止める★★", halted_dup)
    same = {"ceilings": {"adopted": [
        {"kind": "GAME", "amount": 1200, "unit": "G", "counted": "通常時"},
        {"kind": "GAME", "amount": 1200, "unit": "G", "counted": "通常時"}]}}
    t("　まったく同じなら1件にまとめる", len(_dedupe(ceiling_items(same))) == 1)
    t("★★NaN・無限大は数値として扱わない★★",
      ceiling_items({"ceilings": {"adopted": [
          {"kind": "GAME", "amount": float("nan"), "unit": "G"},
          {"kind": "GAME", "amount": float("inf"), "unit": "G"}]}}) == [])

    # --- ★同じ名前の節が2つあれば、どこへ書くか決めない★（Codex126 #6）
    d_two = D([{"title": "基本スペック", "body": ["A"]},
               {"title": "基本スペック", "body": ["B"]}])
    halted_two = False
    try:
        plan(MACH, {"adopted": MAT["adopted"]}, d_two)
    except Halt as e:
        halted_two = "どこへ書くか決められません" in str(e)
    t("★★同じ名前の節が2つあれば書かずに止める★★", halted_two)

    # --- ★計画したのに実行されていなければ止める★（Codex126 #5）
    d_np = D([{"title": "天井・恩恵", "body": [PENDING]},
              {"title": "基本スペック", "body": ["**メーカー**：A社"]}])
    pl_np = plan(MACH, MAT, d_np)
    t("★★計画どおりに組み立て直して一致する★★",
      check(pl_np["before"], pl_np["detail"], pl_np["edits"],
            pl_np["added_lines"], pl_np["boxes"], pl_np["adds"]) == [])
    t("★★計画したのに何も変えていなければ止める★★",
      check(pl_np["before"], pl_np["before"], pl_np["edits"],
            pl_np["added_lines"], pl_np["boxes"], pl_np["adds"]) != [])
    moved = json.loads(json.dumps(pl_np["detail"]))
    line = moved["sections"][0]["body"].pop(0)
    moved["sections"][1]["body"].insert(0, line)
    t("★★足す行を別の節に置いたら止める★★",
      check(pl_np["before"], moved, pl_np["edits"], pl_np["added_lines"],
            pl_np["boxes"], pl_np["adds"]) != [])

    # --- ★「解析判明次第」の並び★（Codex125 #5）
    t("★★区切りが「／」でも、分かった項目だけ抜く★★",
      _enum_rest("**機械割／コイン単価**：解析判明次第追記します。", {"機械割"})
      == "**コイン単価**：解析判明次第追記します。")
    t("　コロン前に空白があっても同じに扱う",
      _enum_rest("**機械割・コイン単価** ：解析判明次第追記します。", {"機械割"})
      == "**コイン単価**：解析判明次第追記します。")
    t("　全部分かったら行ごと落とす",
      _enum_rest("**機械割**：解析判明次第追記します。", {"機械割"}) == "")

    # --- ★足せなかった事実で打ち消し文を消さない★（Codex125 #6）
    d6 = D([{"title": "ゲーム性", "body": ["機械割は判明していません。"]}])
    pl6 = plan(MACH, {"adopted": MAT["adopted"]}, d6)
    t("★★置く節が無い時は、その項目の打ち消し文も消さない★★",
      pl6["detail"]["sections"][0]["body"] == ["機械割は判明していません。"])

    # --- ★「解析待ちの項目」の箇条書き★
    d7 = D([{"title": "天井・恩恵", "body": [PENDING]},
            {"title": "解析待ちの項目",
             "body": ["・天井ゲーム数と恩恵", "・スルー天井の有無と回数",
                      "・リセット時の天井短縮"]}])
    pl7 = plan(MACH, MAT, d7)
    t("★★分かった項目だけ『解析待ちの項目』から消す★★（似た言葉を巻き込まない）",
      pl7["detail"]["sections"][1]["body"]
      == ["・天井ゲーム数と恩恵", "・リセット時の天井短縮"])

    # --- ★勝手な削除・追加を止める柵★（Codex125 #4後段・#7）
    b8 = D([{"title": "A", "body": ["残す文。"]}, {"title": "A", "body": ["別の文。"]}])
    a8 = json.loads(json.dumps(b8))
    a8["sections"][0]["body"] = []
    t("★★同じ名前の節が2つあっても、消えたら気づく★★",
      any("計画にある文がありません" in x for x in check(b8, a8)))
    a9 = json.loads(json.dumps(b8))
    a9["sections"][0]["body"].append("勝手に足した文。")
    t("★★決めていない文が増えたら止める★★",
      any("計画にない文があります" in x for x in check(b8, a9)))

    # --- ★早見表と本文の食い違い★（Codex125 #1）
    d11 = {"slug": "x", "name": "機種X",
           "summaryBoxes": [{"label": "天井", "value": "解析待ち"},
                            {"label": "狙い目", "value": "解析待ち"}],
           "sections": [{"title": "天井・恩恵", "body": [PENDING]}]}
    pl11 = plan(MACH, MAT, d11)
    t("★★天井を載せたら早見表もそろえる★★（同じページで食い違わせない）",
      pl11["detail"]["summaryBoxes"][0]["value"] == "6スルー（CZ）"
      and pl11["detail"]["summaryBoxes"][1]["value"] == "解析待ち"
      and check(pl11["before"], pl11["detail"], pl11["edits"],
                pl11["added_lines"], pl11["boxes"], pl11["adds"]) == [])
    d12 = json.loads(json.dumps(d11))
    d12["summaryBoxes"][0]["value"] = "1200G"
    halted12 = False
    try:
        plan(MACH, MAT, d12)
    except Halt as e:
        halted12 = "早見表にすでに別の天井" in str(e)
    t("★★早見表に別の天井があれば書かずに止める★★", halted12)
    a13 = json.loads(json.dumps(pl11["detail"]))
    a13["summaryBoxes"][1]["value"] = "600G〜"
    t("★★決めていない欄が変わったら止める★★",
      any("早見表" in x for x in check(pl11["before"], a13, pl11["edits"],
                                       pl11["added_lines"], pl11["boxes"],
                                       pl11["adds"])))

    # --- ★Codex127回目に挙げられた迂回例★
    d14 = D([{"title": "天井・恩恵",
              "body": ["**スルー天井**：6スルー（CZ）" + SOURCED, PENDING]}])
    pl14 = plan(MACH, MAT, d14)
    t("★★すでに事実が書いてある節でも、未確認の断りは外す★★（#1の迂回例）",
      pl14["detail"]["sections"][0]["body"]
      == ["**スルー天井**：6スルー（CZ）" + SOURCED]
      and check(pl14["before"], pl14["detail"], pl14["edits"],
                pl14["added_lines"], pl14["boxes"], pl14["adds"],
                pl14["facts"]) == [])
    d15 = D([{"title": "天井・恩恵", "body": [PENDING]},
             {"title": "ゲーム性",
              "body": ["スルー天井の恩恵は判明していません。"]}])
    halted15 = False
    try:
        plan(MACH, MAT, d15)              # 材料の benefit は空
    except Halt as e:
        halted15 = "決められない" in str(e)
    t("★★回数だけ分かった天井の『恩恵は未判明』は消さない★★（#2の迂回例）",
      halted15)
    d16 = D([{"title": "天井・恩恵", "body": [PENDING]},
             {"title": "ゲーム性",
              "body": ["スルー天井は判明しましたが、ほかの天井は未判明です。"]}])
    halted16 = False
    try:
        plan(MACH, MAT, d16)
    except Halt as e:
        halted16 = "分からない天井が同じ文" in str(e)
    t("★★分かった天井と分からない天井が同じ文にあれば止める★★（#2の迂回例）",
      halted16)
    d17 = D([{"title": "天井・恩恵", "body": [PENDING]},
             {"title": "解析待ちの項目", "body": ["・天井の有無と回数"]}])
    halted17 = False
    try:
        plan(MACH, MAT, d17)
    except Halt as e:
        halted17 = "どの天井" in str(e)
    t("★★『・天井の有無と回数』のような曖昧な項目でも止める★★（#2の迂回例）",
      halted17)

    # --- ★別機種の記事には書かない★（Codex125 #9）
    halted10 = False
    try:
        plan(MACH, MAT, {"slug": "y", "name": "機種Y", "sections": []})
    except Halt as e:
        halted10 = "名簿と合いません" in str(e)
    t("★★記事の中身が別機種なら書かない★★", halted10)

    print(f"\n{ran[0]}/{ran[0]} 合格" if ok else "\n不合格あり")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="旧方式の先行記事に材料を足す")
    ap.add_argument("--slug")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.slug:
        print("対象:", " ".join(m["slug"] for m in targets()))
        return 0
    r = run(a.slug, a.apply)
    for p in r.get("problems") or []:
        print("  -", p)
    for x in r.get("added") or []:
        print("  ＋", x)
    for x in r.get("removed") or []:
        print("  －", str(x)[:70])
    for x in r.get("boxes") or []:
        print("  ◇", x)
    for b, x in r.get("rewrote") or []:
        print("  ＊", str(b)[:34], "→", str(x)[:34])
    if r.get("wrote"):
        print("書きました")
    elif not r.get("problems"):
        print("（下見です。--apply で書きます）"
              if (r.get("added") or r.get("removed") or r.get("rewrote")
                  or r.get("boxes"))
              else "足すものがありません")
    return 1 if r.get("problems") else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except _sj.SafeJsonError as e:
        print(f"★入力データが読めません: {e}★")
        raise SystemExit(1)

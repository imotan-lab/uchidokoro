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
import os
import re
import sys

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
_SENT_WORDS = {"機械割": ("機械割", "出玉率"), "型式名": ("型式名",),
               "天井GAME": ("天井",), "天井CYCLE": ("天井", "周期"),
               "天井POINT": ("天井",), "天井THROUGH": ("天井", "スルー")}

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
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


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


def _value_of(line: str, label: str):
    """本文の1行が同じ見出しなら、その値を返す（違う見出しなら None）。"""
    m = _LABELED.match(str(line).strip())
    if not m or m.group("label").strip() != label:
        return None
    return m.group("value").replace(SOURCED, "").strip().rstrip("。")


# ------------------------------------------------------- 食い違いを落とす

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
    keep = [x for x in _SEP.split(raw)
            if not any(w in x for k in keys for w in _SENT_WORDS.get(k, ()))]
    return f"**{sep.join(keep)}**：解析判明次第追記します。" if keep else ""


def resolve_contradictions(after: dict, keys: set) -> list:
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
                if any(w in t for k in keys for w in _PENDING_WORDS.get(k, ())):
                    edits.append((i, t, None))
                continue
            rest = _enum_rest(t, keys)
            if rest is not None:
                if rest.strip() != t.strip():
                    edits.append((i, t, rest or None))
                continue
            sents = [s for s in re.split(r"(?<=。)", t) if s.strip()]
            drop = [s for s in sents if _removable(s, keys)]
            if not drop:
                continue
            # ★別の「まだ分からない話」が混じる文は自分で決めない★
            for s in drop:
                if any(w in s for w in _OTHER_TOPICS):
                    raise Halt(f"落としてよいか決められない文があります: {s[:48]}")
            new = "".join(s for s in sents if s not in drop).strip()
            edits.append((i, t, new or None))
    return edits


def _apply_edits(after: dict, edits: list, adds: dict) -> dict:
    """計画どおりに本文を組み立てる。"""
    drop = {(i, b): a for i, b, a in edits}
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
            if str(b).strip() == PENDING and adds.get(i):
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


def _fix_summary(after: dict, ceilings: list, present: set) -> list:
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
    for i, box in enumerate(after.get("summaryBoxes") or []):
        if str(box.get("label") or "").strip() != "天井":
            continue
        before = str(box.get("value") or "").strip()
        if before == value:
            continue
        if before not in _BOX_PENDING:
            # ★すでに別の天井が書いてある＝どちらが正しいか決められない★
            raise Halt(f"早見表にすでに別の天井が書かれています（{before}）")
        box["value"] = value
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
    idx = {str(s.get("title")): i for i, s in enumerate(secs)}
    cand = ceiling_items(material) + spec_items(material)

    present, adds, added_lines = set(), {}, []
    for key, label, value, want in cand:
        same = False
        for i, s in enumerate(secs):      # ★全部の節を見る★（同じ見出しの重複防止）
            for b in (s.get("body") or []):
                got = _value_of(b, label)
                if got is None:
                    continue
                if got == value:
                    same = True
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
        present.add(key)
        line = f"**{label}**：{value}{SOURCED}"
        adds.setdefault(idx[want], []).append(line)
        added_lines.append(f"{want}: {line}")

    # ★食い違いを落としてよいのは「記事に載っている事実」だけ★
    edits = resolve_contradictions(after, present)
    after = _apply_edits(after, edits, adds)
    boxes = _fix_summary(after, ceiling_items(material), present)
    return {"slug": machine["slug"], "detail": after, "before": detail,
            "added": added_lines, "edits": edits, "boxes": boxes,
            "added_lines": [x.split(": ", 1)[1] for x in added_lines]}


def check(before: dict, after: dict, edits=(), added_lines=(), boxes=()) -> list:
    """★決めたとおりにだけ変わっているか★（節の番号で照合する）。"""
    ng = []
    # --- 早見表 ---
    b_box = before.get("summaryBoxes") or []
    a_box = after.get("summaryBoxes") or []
    if len(b_box) != len(a_box):
        ng.append("早見表の欄の数が変わります")
    else:
        allow_box = {i: (b, a) for i, b, a in (boxes or [])}
        for i, (bb, ab) in enumerate(zip(b_box, a_box)):
            if str(bb.get("label")) != str(ab.get("label")):
                ng.append(f"早見表{i}番目の見出しが変わります")
                continue
            bv, av = str(bb.get("value") or ""), str(ab.get("value") or "")
            if bv == av:
                continue
            want = allow_box.get(i)
            if not want or want[1] != av:
                ng.append(f"早見表『{bb.get('label')}』が決めていない値に変わります")
    # --- 本文 ---
    b_secs = before.get("sections") or []
    a_secs = after.get("sections") or []
    if len(b_secs) != len(a_secs):
        return ["節の数が変わります"]
    allow = {(i, b): a for i, b, a in (edits or [])}
    adds = {str(x) for x in (added_lines or [])}
    for i, (bs, as_) in enumerate(zip(b_secs, a_secs)):
        if str(bs.get("title")) != str(as_.get("title")):
            ng.append(f"{i}番目の節の名前が変わります")
            continue
        expect = []
        for b in (bs.get("body") or []):
            key = (i, str(b))
            if key in allow:
                if allow[key]:
                    expect.append(str(allow[key]))
                continue
            if str(b).strip() == PENDING:
                continue                  # 断りは外れてもよい
            expect.append(str(b))
        got = [str(x) for x in (as_.get("body") or [])]
        miss = [x for x in expect if x not in got]
        if miss:
            ng.append(f"{bs.get('title')}: 前からあった文が消えます（{miss[0][:32]}…）")
        extra = [x for x in got
                 if x not in expect and x not in adds and x.strip() != PENDING]
        if extra:
            ng.append(f"{bs.get('title')}: 決めていない文が増えます（{extra[0][:32]}…）")
    return ng


# ------------------------------------------------------------------ 実行

def _write(path: str, detail: dict, sha_before: str) -> None:
    """★書く直前にもう一度確かめて、置き換えで書く★。"""
    if _sha(path) != sha_before:
        raise Halt("書く直前にファイルが変わっていました（同時に触られた可能性）")
    sys.path.insert(0, os.path.join(BASE, "scripts"))
    from publish_new_machine import write_atomic
    write_atomic(path, json.dumps(detail, ensure_ascii=False, indent=1) + "\n")


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
               pl["boxes"])
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
      check(pl["before"], pl["detail"], pl["edits"], pl["added_lines"]) == []
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
             "body": ["天井は判明していませんので狙い目を出せません。"]}])
    halted4 = False
    try:
        plan(MACH, MAT, d4)
    except Halt as e:
        halted4 = "決められない" in str(e)
    t("★★落としてよいか決められない文があれば止める★★（狙い目の話が混じる）",
      halted4)
    d5 = D([{"title": "天井・恩恵", "body": [PENDING]},
            {"title": "ゲーム性", "body": ["天井は判明していません。", "残す文。"]}])
    pl5 = plan(MACH, MAT, d5)
    t("　混ざり物が無ければ、その文だけ落とす",
      pl5["detail"]["sections"][1]["body"] == ["残す文。"]
      and check(pl5["before"], pl5["detail"], pl5["edits"],
                pl5["added_lines"]) == [])

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
      any("消えます" in x for x in check(b8, a8)))
    a9 = json.loads(json.dumps(b8))
    a9["sections"][0]["body"].append("勝手に足した文。")
    t("★★決めていない文が増えたら止める★★",
      any("増えます" in x for x in check(b8, a9)))

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
                pl11["added_lines"], pl11["boxes"]) == [])
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
                                       pl11["added_lines"], pl11["boxes"])))

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

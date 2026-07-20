#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""claim_identity.py — 出典ページが「その機種のページか」を決定論で判定する。

背景（2026-07-21）:
  verify_claims.py の C2 同定は identity.must_contain（＝machines.json の name そのもの）が
  ページ本文とタイトルの両方に含まれることを要求する。ところが解析サイトの表記は
  「L」「スマスロ」「（スマスロ）」「全角！／半角!」「空白」等がうちの表記と食い違うため、
  実測33件中22件（67%）が【出典は正しいのに】C2で落ちていた。

方針:
  ・タイトルは「機種名らしき区間」を切り出し、**表記ゆれを落とした芯（core）の完全一致**で判定する。
    部分一致は使わない（「北斗の拳」が「北斗の拳 修羅の国篇」のページに当たる事故を防ぐため）。
  ・別名（machines.json の aliases）も芯の候補にするが、**3文字未満の芯は同定に使わない**
    （「北斗」「物語」のような汎用語で別機種のページに当たるのを防ぐ）。
  ・カタログ内の他機種の芯と衝突する候補がタイトルにある場合は不合格（曖昧なら通さない）。
  ・本文側は従来どおり緩い部分一致（ページ内に機種名が出てくること）。

  ★このモジュールは判定のみ。ファイルを書き換えない。LLMを呼ばない。★
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path

# ─────────────────────────────────────────────
# 芯（core）の作り方
# ─────────────────────────────────────────────

# 機種名の頭・中に付く販売区分の語（芯からは落とす）。
# ★「5号機」「6号機」「パチンコ」等は落とさない★＝それ自体が別機種を意味する語なので、
#   落とすと「【北斗の拳（5号機）】」を「北斗の拳」と誤認してしまう（2026-07-21 Codex指摘）。
_PLATFORM_WORDS = (
    "スマスロ", "パチスロ", "ぱちスロ", "スロット", "メダルレス",
)
# 名前の先頭に付くL/S等の型式記号（例: Lバンドリ！ / Sゴジラ）
_TYPE_PREFIX_RE = re.compile(r"^[lsｌｓ](?=[^a-z]|$)", re.IGNORECASE)
# 芯から落とす記号・空白（英数字とCJKだけ残す）
_NONCORE_RE = re.compile(r"[^0-9a-z぀-ヿ一-鿿ｦ-ﾟ]+")

MIN_ALIAS_CORE = 3   # 同定に使ってよい別名の芯の最短長（汎用語よけ）


def normalize_core(s: str) -> str:
    """表記ゆれを落とした「芯」を返す。判定は全てこの芯の完全一致で行う。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s)).lower()
    s = _TYPE_PREFIX_RE.sub("", s)
    for w in _PLATFORM_WORDS:
        s = s.replace(w.lower(), "")
        s = s.replace(unicodedata.normalize("NFKC", w).lower(), "")
    s = _NONCORE_RE.sub("", s)
    return s


def machine_cores(machine: dict) -> list[str]:
    """その機種を同定してよい芯の一覧（name は長さ不問・aliases は3文字以上）。"""
    out: list[str] = []
    nc = normalize_core(machine.get("name", ""))
    if nc:
        out.append(nc)
    for a in machine.get("aliases") or []:
        c = normalize_core(a)
        if len(c) >= MIN_ALIAS_CORE and c not in out:
            out.append(c)
    return out


def catalog_cores(machines: list[dict]) -> dict[str, set[str]]:
    """芯 -> それを名乗る slug 集合（衝突検知用）。"""
    idx: dict[str, set[str]] = {}
    for m in machines:
        for c in machine_cores(m):
            idx.setdefault(c, set()).add(m.get("slug", ""))
    return idx


def accept_cores_for(machine: dict, machines: list[dict]) -> list[str]:
    """同定に使ってよい芯＝カタログ内で**自分しか名乗っていない**芯だけ。

    ★複数機種が同じ芯を名乗る（例: 続編と無印が同じ別名を持つ）場合、その芯では
      どちらのページか決まらないので同定に使わない。結果その機種は検証不能になるが、
      「間違って別機種のページを根拠に自動修正する」より安全なので不能側に倒す。★
    """
    idx = catalog_cores(machines)
    slug = machine.get("slug")
    return [c for c in machine_cores(machine) if idx.get(c, set()) == {slug}]


def reject_cores_for(machine: dict, machines: list[dict]) -> list[str]:
    """この機種【以外】の芯の一覧（タイトルがそっちを指していたら不合格にする）。

    ★除くのは「同定に使える芯（＝自分しか名乗っていない芯）」だけ★。
      他機種と共有している別名は reject 側に残す（2026-07-21 Codex指摘5：
      共有別名をrejectから外すと、比較記事タイトルを合格にできてしまう）。
    """
    mine = set(accept_cores_for(machine, machines))
    out: set[str] = set()
    for m in machines:
        if m.get("slug") == machine.get("slug"):
            continue
        for c in machine_cores(m):
            if c not in mine:
                out.add(c)
    return sorted(out)


# ─────────────────────────────────────────────
# タイトルから「機種名らしき区間」を切り出す
# ─────────────────────────────────────────────

# タイトル内で機種名の後ろに続く定型語（ここで切る）
_STOP_WORDS = (
    "天井", "解析", "スペック", "設定判別", "設定示唆", "設定差", "やめどき", "ヤメ時",
    "やめ時", "期待値", "恩恵", "朝一", "リセット", "打ち方", "狙い目", "まとめ",
    "ゾーン", "モード", "新台", "導入日", "評価", "攻略", "終了画面", "高設定",
    "有利区間", "立ち回り", "解説", "について", "詳細", "考察", "実戦", "スルー",
    "純増", "フリーズ", "小役", "確率", "動画", "情報", "一覧",
)
# ハイフンは機種名の中でよく使う（例「アナザーゴッドハーデス-解き放たれし槍撃ver.-」）ので
# 無条件の区切りにしない。前後に空白がある場合だけサイト名区切りとみなす（下で処理）。
_SEPARATORS = ("|", "｜", "／", "»", "≫", "・パチスロ", " - ", " ‐ ", " – ", " — ", " / ")
_BRACKET_RE = re.compile(r"【(.+?)】")
# 区間をさらに割る記号（「アズールレーン スマスロ(アズレン)」→ 両方を候補にする）
_SPLIT_RE = re.compile(r"[()（）｜|／,、，]+")

# ─────────────────────────────────────────────
# 世代・媒体タグ（同名の旧機種／パチンコ版と混ざらないための必須条件）
# ─────────────────────────────────────────────
# 2026-07-21 Codexレビュー指摘: 「パチスロ北斗の拳(2003)」「パチスロ化物語(2013)」など
# 【うちのカタログに無い同名の旧機種】は、販売区分語を落とすと芯が完全に同じになる。
# タイトルの世代タグが自機種と食い違う場合は不合格にする。
_TAG_WORDS = {
    "smart": ("スマスロ", "スマートスロット", "スマート遊技機"),
    "legacy": ("パチスロ", "ぱちスロ", "5号機", "6号機", "6.5号機", "新基準", "aタイプ"),
    "pachinko": ("パチンコ", "スマパチ", "cr", "ぱちんこ"),
}


def detect_tags(s: str) -> set[str]:
    """文字列に含まれる世代・媒体タグ（複数可）。"""
    t = unicodedata.normalize("NFKC", str(s or "")).lower()
    tags = set()
    for tag, words in _TAG_WORDS.items():
        if any(unicodedata.normalize("NFKC", w).lower() in t for w in words):
            tags.add(tag)
    return tags


def machine_tags(machine: dict) -> set[str]:
    """自機種の世代タグ。名前が「L…」で始まる型式もスマスロ世代とみなす。"""
    name = str(machine.get("name") or "")
    tags = detect_tags(name)
    if _TYPE_PREFIX_RE.match(unicodedata.normalize("NFKC", name).lower()):
        tags.add("smart")
    return tags


def _cut_at_stopword(s: str) -> str:
    idx = len(s)
    for w in _STOP_WORDS + _SEPARATORS:
        p = s.find(w)
        if 0 < p < idx:
            idx = p
    return s[:idx]


def title_groups(title: str) -> list[list[str]]:
    """タイトルを「同じ機種名表記のまとまり」ごとに分けて返す。

    例「【アズールレーン スマスロ(アズレン)】天井」→ [["アズールレーン スマスロ", "アズレン"], …]
    まとまり内の断片は同じ機種を指すはずなので、1つが自機種に一致したら
    残りも自機種の別名か販売区分語でなければならない（そうでなければ別機種混在）。
    """
    if not title:
        return []
    t = unicodedata.normalize("NFKC", str(title)).strip()
    raw: list[str] = [m.group(1) for m in _BRACKET_RE.finditer(t)]   # 【…】が最優先
    if raw:
        # 【…】の外は別の話題（記事の説明文）なので、中身と混ぜて1つのまとまりにしない。
        # 外側は「他機種名が併記されていないか」を見るための独立したまとまりとして扱う。
        raw.append(_BRACKET_RE.sub(" ", t))
    else:
        raw.append(t)                          # 括弧が無いタイトルは丸ごと1まとまり

    groups: list[list[str]] = []
    for r in raw:
        head = _cut_at_stopword(r).strip()
        pieces = [p.strip() for p in _SPLIT_RE.split(head) if p.strip()]
        # 括弧等で割れる断片は「割った後」だけを候補にする。
        # 割る前の連結形（例「【スマスロ北斗の拳】ゲーム数」→"北斗の拳ゲーム数"）は
        # 機種名ではないのに“より長い機種名”に見えて誤って落ちるため候補にしない。
        g = [head] if len(pieces) <= 1 else pieces
        g = [p for p in g if p]
        if g and g not in groups:
            groups.append(g)
    return groups


def title_candidates(title: str) -> list[str]:
    """タイトルから機種名候補の文字列を列挙する（芯にする前の生の断片）。"""
    out: list[str] = []
    for g in title_groups(title):
        for p in g:
            if p not in out:
                out.append(p)
    return out


# ─────────────────────────────────────────────
# 判定
# ─────────────────────────────────────────────

def name_region(title: str, cores=()) -> str:
    """タイトルのうち「機種名が書かれている区間」だけを返す。

    ★サイト名の定型文（例「| ちょんぼりすた パチスロ解析」）を世代タグ判定に混ぜないため★。
    自機種の芯に一致するまとまりを優先し、無ければ先頭のまとまりを使う。
    """
    groups = title_groups(title)
    if not groups:
        return ""
    cores = set(cores or ())
    for g in groups:
        if any(normalize_core(p) in cores for p in g):
            return " ".join(g)
    return " ".join(groups[0])


def check_tags(title: str, my_tags, cores=()) -> tuple[bool, str]:
    """世代・媒体タグの整合。同名の旧機種／パチンコ版を弾く最後の砦。

    ・機種名区間のタグが自機種と食い違う → 不合格（自分=スマスロ / 相手=パチスロ・パチンコ）
    ・自機種がスマスロ世代なのに機種名区間に世代の手掛かりが無い → 不合格
      （うちのカタログに無い同名旧機種のページを拾わないため。実測では解析サイトの
        タイトルはほぼ必ず「スマスロ」「L」を含むので取りこぼしは小さい）
    """
    my_tags = set(my_tags or ())
    title = name_region(title, cores) or title
    t_tags = detect_tags(title)
    # ★食い違いの定義★（自機種名に世代表記が無い機種も多いので「無い＝旧世代」とは扱わない）
    #   ・パチンコ表記 → 常に不合格（当サイトはパチスロのみ）
    #   ・自分がスマスロ世代なのに相手が旧世代表記 → 不合格（同名旧機種）
    #   ・自分が旧世代表記なのに相手がスマスロ表記 → 不合格（後継のスマスロ版）
    conflict = None
    if "pachinko" in t_tags:
        conflict = "パチンコ版のページ"
    elif "legacy" in t_tags and "smart" in my_tags:
        conflict = "旧世代（パチスロ/号機表記）のページ"
    elif "smart" in t_tags and "legacy" in my_tags:
        conflict = "スマスロ版のページ（自機種は旧世代）"
    if conflict:
        return False, (f"世代・媒体が違う（{conflict} / タイトル={sorted(t_tags)}"
                       f" / 自機種={sorted(my_tags)}）")
    if "smart" in my_tags and "smart" not in t_tags:
        nt = unicodedata.normalize("NFKC", str(title)).lower()
        if not re.search(r"(?:^|[^a-z0-9])l[^a-z0-9]", nt):
            return False, "自機種はスマスロ世代だがタイトルに世代表記が無い（同名の旧機種の疑い）"
    return True, "世代タグOK"


def check_title(title: str, cores, reject_cores=()) -> tuple[bool, str]:
    """タイトルがこの機種のページを指しているか。(合格?, 理由) を返す。

    合格条件（すべて満たす）:
      1. タイトルの機種名候補のどれかの芯が cores のどれかと**完全一致**する
      2. 他機種の芯と完全一致する候補がタイトルに無い
      3. 一致した芯を**真に含むより長い候補**がタイトルに無い（シリーズ違い・続編よけ）
    """
    cores = [c for c in cores if c]
    if not cores:
        return False, "同定用の芯が空"
    rej = set(reject_cores or ())
    cands = title_candidates(title)
    if not cands:
        return False, "タイトルから機種名区間を取り出せない"

    cand_cores = []
    for c in cands:
        cc = normalize_core(c)
        if cc and cc not in cand_cores:
            cand_cores.append(cc)

    hit = [c for c in cand_cores if c in cores]
    if not hit:
        return False, f"タイトルの機種名が一致しない（候補={cand_cores[:4]} / 期待={cores[:4]}）"

    # ★複数の【…】区間があり、そのうち1つでも自機種と無関係な機種名らしき区間なら不合格★
    #   （2026-07-21 Codex指摘6: カタログ外の他機種と並ぶ比較記事を通してしまう）
    t_norm = unicodedata.normalize("NFKC", str(title))
    brackets = [m.group(1) for m in _BRACKET_RE.finditer(t_norm)]
    if len(brackets) >= 2:
        for b in brackets:
            pieces = [p.strip() for p in _SPLIT_RE.split(_cut_at_stopword(b)) if p.strip()]                 or [_cut_at_stopword(b).strip()]
            bc = [normalize_core(p) for p in pieces]
            if any(c in cores for c in bc):
                continue
            if any(c for c in bc):
                return False, (f"他機種らしい区間が併記されている（{[c for c in bc if c][:2]}）"
                               f"→比較記事等の疑いで不合格")

    # ★同じまとまりの中に「自機種で説明できない語」が混じっていたら不合格★
    #   例「【北斗の拳（パチンコ）】」「【北斗の拳（5号機）】」＝別媒体・別世代のページ。
    #   販売区分語（スマスロ等）は芯が空になるので無害として通す。
    for g in title_groups(title):
        gc = [normalize_core(p) for p in g]
        if not any(c in cores for c in gc):
            continue
        extra = [p for p, c in zip(g, gc) if c and c not in cores]
        if extra:
            return False, (f"機種名のまとまりに自機種で説明できない語がある（{extra[:3]}）"
                           f"→別媒体・別世代・別機種のページの疑い")

    conflict = [c for c in cand_cores if c in rej]
    if conflict:
        return False, f"タイトルに他機種の名前がある（{conflict[:3]}）→曖昧なので不合格"

    for h in hit:
        for c in cand_cores:
            if c == h or h not in c or len(c) <= len(h):
                continue
            # 一致した芯を含むより長い候補：余りが自分の別名だけで説明できるなら可
            #（例「アズールレーン スマスロ(アズレン)」＝アズールレーン＋アズレン）。
            # 説明できない余り（例「北斗の拳＋修羅の国篇」）が残るなら続編/別機種の疑い。
            rest = c
            for own in sorted(cores, key=len, reverse=True):
                rest = rest.replace(own, "")
            if len(rest) >= 2:
                return False, f"より長い機種名がタイトルにある（{c} / 余り「{rest}」）→続編・シリーズ違いの疑い"
    return True, f"タイトル同定OK（{hit[0]}）"


def check_body(body_norm: str, cores) -> tuple[bool, str]:
    """本文側は緩い部分一致（芯のどれかが本文に出てくればOK）。
    body_norm は呼び出し側で正規化済みの本文テキストを渡す（NFKC・空白除去は問わない）。"""
    bc = normalize_core(body_norm)
    for c in cores:
        if c and c in bc:
            return True, f"本文同定OK（{c}）"
    return False, f"本文に機種名が無い（期待={list(cores)[:4]}）"


def identity_spec(machine: dict, machines: list[dict]) -> dict:
    """verify_claims.py の identity に渡す辞書を組み立てる。"""
    return {
        "must_contain": [machine.get("name", "")],
        "machine_cores": accept_cores_for(machine, machines),
        "reject_cores": reject_cores_for(machine, machines),
        "machine_tags": sorted(machine_tags(machine)),
    }


def load_machines(path=None) -> list[dict]:
    p = Path(path) if path else Path(__file__).resolve().parent.parent / "assets" / "data" / "machines.json"
    return json.loads(p.read_text(encoding="utf-8"))


# ─────────────────────────────────────────────
# selftest
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

    # --- 芯の正規化 ---
    eq(normalize_core("Lバンドリ！"), "バンドリ", "core:Lバンドリ！")
    eq(normalize_core("スマスロ バンドリ！"), "バンドリ", "core:スマスロ バンドリ！")
    eq(normalize_core("バンドリ（スマスロ）"), "バンドリ", "core:括弧付き")
    eq(normalize_core("スマスロ北斗の拳"), "北斗の拳", "core:北斗")
    eq(normalize_core("Lスマスロ化物語KH"), "化物語kh", "core:化物語KH")
    eq(normalize_core("Lバキ 強くなりたくば喰らえ!!!"), "バキ強くなりたくば喰らえ", "core:バキ")
    eq(normalize_core(""), "", "core:空")
    eq(normalize_core("ｽﾏｽﾛ北斗の拳"), "北斗の拳", "core:半角カナ")
    # 「L」始まりでも語の一部なら消さない
    eq(normalize_core("ラブ嬢3"), "ラブ嬢3", "core:Lで始まらない日本語")
    eq(normalize_core("Lupin"), "lupin", "core:英単語の頭のLは残す（型式記号と区別）")

    # --- 別名の採否 ---
    m_hokuto = {"slug": "hokuto", "name": "スマスロ北斗の拳",
                "aliases": ["北斗", "ほくと", "hokuto", "北斗の拳"]}
    cs = machine_cores(m_hokuto)
    eq("北斗" in cs, False, "alias:2文字は不採用")
    eq("北斗の拳" in cs, True, "alias:4文字は採用")
    eq(cs[0], "北斗の拳", "alias:name由来が先頭")
    m_bake = {"slug": "bakemonogatari", "name": "Lスマスロ化物語KH",
              "aliases": ["化物語", "バケモノガタリ", "物語"]}
    eq("物語" in machine_cores(m_bake), False, "alias:汎用2文字を排除")
    eq("化物語" in machine_cores(m_bake), True, "alias:化物語は採用")

    # --- 実タイトル（2026-07-21 実取得）で合格すること ---
    cat = [m_hokuto, m_bake,
           {"slug": "bandori", "name": "Lバンドリ！", "aliases": ["バンドリ", "ばんどり"]},
           {"slug": "azurlane", "name": "Lアズールレーン THE ANIMATION",
            "aliases": ["アズレン", "アズールレーン"]},
           {"slug": "godzilla", "name": "Lゴジラ", "aliases": ["ゴジラ"]},
           {"slug": "babel", "name": "スマスロ バベル", "aliases": ["バベル"]},
           {"slug": "baki", "name": "Lバキ 強くなりたくば喰らえ!!!", "aliases": ["バキ"]},
           {"slug": "akudama", "name": "Lアクダマドライブ", "aliases": ["アクダマドライブ"]}]
    by = {m["slug"]: m for m in cat}

    def title_ok(slug, title):
        m = by[slug]
        if not check_tags(title, machine_tags(m), accept_cores_for(m, cat))[0]:
            return False
        return check_title(title, accept_cores_for(m, cat), reject_cores_for(m, cat))[0]

    real = [
        ("bandori", "【スマスロ バンドリ！】天井の恩恵や発動条件・天井期待値について"),
        ("bandori", "スマスロ バンドリ！ 天井 スペック 設定判別 やめどき | ちょんぼりすた パチスロ解析"),
        ("bandori", "【バンドリ（スマスロ）】天井とやめどき-朝一（リセット）恩恵についても解説！"),
        ("hokuto", "【北斗の拳（スマスロ）】天井と朝一（リセット）｜やめどきやゾーンなどの立ち回りについても解説！"),
        ("hokuto", "【スマスロ北斗の拳】朝一・設定変更(リセット)時の挙動や恩恵まとめ"),
        ("bakemonogatari", "【化物語（スマスロ）】天井とやめどき・朝一（リセット）恩恵、期待値についても解説！"),
        ("godzilla", "【スマスロ ゴジラ】天井の恩恵や発動条件・天井期待値について"),
        ("babel", "スマスロバベル 天井狙いまとめ｜天井解析 天井恩恵 ゾーン ヤメ時 リセット モード"),
        ("babel", "【バベル（スマスロ/スロット）】天井とやめどき｜朝一（リセット）恩恵についても解説！"),
        ("baki", "【Lバキ 強くなりたくば喰らえ!!!】解析情報まとめ 天井・設定判別・スペック・打ち方・やめどき"),
        ("akudama", "スマスロアクダマドライブ 天井狙いまとめ｜天井解析 天井恩恵 ゾーン ヤメ時 リセット モード"),
        ("azurlane", "スマスロアズールレーン 天井狙いまとめ｜天井解析 天井恩恵 ゾーン ヤメ時 リセット モード"),
        ("azurlane", "【アズールレーン スマスロ(アズレン)】天井の期待値や発動条件・恩恵"),
    ]
    for slug, t in real:
        eq(title_ok(slug, t), True, f"実タイトル合格:{slug}:{t[:22]}")

    # --- 別媒体・別世代（2026-07-21 Codexレビュー指摘）---
    # 同名の旧機種（パチスロ北斗の拳2003 / パチスロ化物語2013）とパチンコ版を落とす
    eq(title_ok("hokuto", "【パチスロ北斗の拳】天井・解析"), False, "同名の旧パチスロ機は不合格")
    eq(title_ok("bakemonogatari", "【パチスロ化物語】天井"), False, "同名の旧機種(化物語)は不合格")
    eq(title_ok("hokuto", "【北斗の拳】天井とやめどき"), False,
       "世代表記の無いタイトルは不合格（旧機種の疑い）")
    eq(check_tags("【スマスロ北斗の拳】天井", {"smart"})[0], True, "スマスロ表記あり")
    eq(check_tags("【Lバンドリ！】天井", {"smart"})[0], True, "L表記でも可")
    eq(check_tags("【パチンコ北斗の拳】天井", {"smart"})[0], False, "パチンコは不合格")
    eq(check_tags("マイジャグラーV 設定判別", set())[0], True, "世代タグ無し機種は要求しない")
    eq(machine_tags({"name": "スマスロ北斗の拳"}), {"smart"}, "自機種タグ:スマスロ")
    eq(machine_tags({"name": "Lバンドリ！"}), {"smart"}, "自機種タグ:L型式")
    eq(machine_tags({"name": "マイジャグラーV"}), set(), "自機種タグ:無し")
    # 括弧内の副題で続編が通らないこと（Codex指摘1）
    eq(title_ok("hokuto", "【スマスロ北斗の拳（修羅の国篇）】天井・解析"), False,
       "括弧内の副題つき続編は不合格")
    eq(title_ok("bandori", "【スマスロ バンドリ！（2）】天井"), False, "括弧内の続編番号は不合格")
    # ハイフンを含む機種名が壊れないこと（Codex指摘4）
    eq(normalize_core("アナザーゴッドハーデス-解き放たれし槍撃ver.-"),
       "アナザーゴッドハーデス解き放たれし槍撃ver", "ハイフン付き機種名の芯")
    eq(check_title("【アナザーゴッドハーデス-解き放たれし槍撃ver.-】天井",
                   ["アナザーゴッドハーデス"], [])[0], False,
       "副題違いのハーデスは不合格（芯が別物）")

    eq(title_ok("hokuto", "【北斗の拳（パチンコ）】天井"), False, "パチンコ版は不合格")
    eq(title_ok("hokuto", "【北斗の拳（5号機）】天井"), False, "5号機版は不合格")
    eq(title_ok("hokuto", "【北斗の拳 6号機】天井解析"), False, "号機表記付きは不合格")
    eq(title_ok("hokuto", "【e北斗の拳10】天井"), False, "パチンコe機は不合格")
    eq(title_ok("bandori", "【Pバンドリ】天井"), False, "パチンコP機は不合格")
    eq(title_ok("hokuto", "【スマスロ北斗の拳】【e北斗の拳10】天井比較"), False,
       "スロットとパチンコの比較記事は不合格")
    eq(title_ok("babel", "【バベル（スマスロ/スロット）】天井とやめどき"), True,
       "販売区分語だけの併記は合格")
    eq(normalize_core("北斗の拳（5号機）"), "北斗の拳5号機", "号機は芯から落とさない")
    eq(normalize_core("北斗の拳（パチンコ）"), "北斗の拳パチンコ", "パチンコは芯から落とさない")

    # --- Codex指摘の取りこぼし側（正しい出典を落とさない）---
    eq(normalize_core("ファミスタ回胴版!!"), "ファミスタ回胴版", "「版」で切らない")
    eq(check_title("【ファミスタ回胴版!!】天井・解析", ["ファミスタ回胴版"], [])[0], True,
       "回胴版が通る")
    eq(normalize_core("A-SLOT+ ディスクアップ ULTRAREMIX"), "aslotディスクアップultraremix",
       "ハイフン入り英字名の芯")
    eq(check_title("【A-SLOT+ ディスクアップ ULTRAREMIX】天井",
                   ["aslotディスクアップultraremix"], [])[0], True, "ハイフン入り英字名が通る")
    eq(check_title("【スマスロ劇場版 魔法少女まどか☆マギカ[前編]始まりの物語】天井",
                   [normalize_core("劇場版 魔法少女まどか☆マギカ[前編]始まりの物語")], [])[0],
       True, "[前編]を含む正式名称が通る")
    eq(normalize_core("L009 RE:CYBORG"), "009recyborg", "L+数字は型式記号として落とす")
    eq(check_title("【スマスロ 009 RE:CYBORG】天井", ["009recyborg"], [])[0], True,
       "L009が表記ゆれタイトルで通る")
    # --- 他機種併記（Codex指摘6）---
    eq(check_title("【スマスロ北斗の拳】【SLOT魔法少女まどか☆マギカ】天井比較",
                   ["北斗の拳"], [])[0], False, "カタログ外の他機種併記も不合格")
    eq(check_title("【スマスロ北斗の拳】天井の恩恵", ["北斗の拳"], [])[0], True,
       "単独の区間は通る")

    # --- 危険パターンは必ず落ちること ---
    # 芯が一致しないのに部分一致で通る、を許さない
    eq(title_ok("hokuto", "【スマスロ北斗の拳 修羅の国篇】天井の恩恵"), False, "続編ページは不合格")
    eq(title_ok("hokuto", "【北斗の拳 将】天井"), False, "シリーズ違いは不合格")
    eq(title_ok("bakemonogatari", "【スマスロ 続・終物語】天井とやめどき"), False, "物語シリーズ別機種は不合格")
    eq(title_ok("bandori", "パチスロ新台一覧｜天井解析まとめ"), False, "一覧ページは不合格")
    eq(title_ok("bandori", ""), False, "タイトル空は不合格")
    # カタログ内の他機種名がタイトルに混ざる（比較記事等）→曖昧なので不合格
    eq(title_ok("bandori", "【スマスロ バンドリ！】と【スマスロ ゴジラ】天井比較"), False,
       "他機種混在は不合格")
    # 芯の完全一致が必要（部分文字列では通さない）
    eq(check_title("【バンドリ2】天井", ["バンドリ"], [])[0], False, "続編数字付きは不合格")
    eq(check_title("【バンドリ】天井", ["バンドリ"], ["バンドリ2"])[0], True, "無印は合格")

    # --- 本文側 ---
    eq(check_body("本機スマスロ バンドリ！の天井は…", ["バンドリ"])[0], True, "本文一致")
    eq(check_body("別の機種の解説です", ["バンドリ"])[0], False, "本文不一致")

    # --- 同じ芯を2機種が名乗る場合は、その芯を同定に使わない ---
    twin = [{"slug": "enen", "name": "スマスロ炎炎ノ消防隊", "aliases": ["炎炎", "enen"]},
            {"slug": "enen2", "name": "スマスロ炎炎ノ消防隊2", "aliases": ["炎炎2", "enen"]}]
    eq("enen" in machine_cores(twin[0]), True, "衝突前:別名enenは芯に入る")
    eq("enen" in accept_cores_for(twin[0], twin), False, "衝突する芯は同定に使わない")
    eq(accept_cores_for(twin[0], twin), ["炎炎ノ消防隊"], "自分だけの芯は残る")
    eq(check_title("【スマスロ炎炎ノ消防隊2】天井", accept_cores_for(twin[0], twin),
                   reject_cores_for(twin[0], twin))[0], False, "続編ページを無印で通さない")
    eq(check_title("【スマスロ炎炎ノ消防隊】天井", accept_cores_for(twin[0], twin),
                   reject_cores_for(twin[0], twin))[0], True, "無印ページは通る")

    # --- 実カタログでの状態（衝突は許容し、同定に使わないだけ）---
    try:
        real_ms = load_machines()
        idx = catalog_cores(real_ms)
        dup = {c: sorted(s) for c, s in idx.items() if len(s) > 1}
        if dup:
            print(f"  (情報)カタログ内で衝突する芯 {len(dup)}件 → 同定に不使用: "
                  + ", ".join(f"{c}={s}" for c, s in list(dup.items())[:6]))
        # 名前そのものの芯が他機種と衝突するのは二重登録の疑い＝不合格
        namedup = {}
        for m in real_ms:
            nc = normalize_core(m["name"])
            eq(len(nc) >= 2, True, f"名前の芯が短すぎる: {m['slug']}")
            namedup.setdefault(nc, []).append(m["slug"])
        eq({k: v for k, v in namedup.items() if len(v) > 1}, {}, "機種名の芯が衝突（二重登録の疑い）")
        # 全機種が最低1つは同定に使える芯を持つこと（持たない機種は検証不能になる）
        blind = [m["slug"] for m in real_ms if not accept_cores_for(m, real_ms)]
        eq(blind, [], f"同定に使える芯が無い機種: {blind}")
        slugs = [m.get("slug") for m in real_ms]
        eq([x for x in slugs if not x], [], "slugが空の機種がある")
        eq([x for x in set(slugs) if slugs.count(x) > 1], [], "slugが重複している")
    except FileNotFoundError:
        print("  (machines.json 不在のためカタログ検査skip)")

    print(f"claim_identity selftest: {ok}/{ok + fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--spec" in sys.argv:
        slug = sys.argv[sys.argv.index("--spec") + 1]
        ms = load_machines()
        m = [x for x in ms if x["slug"] == slug][0]
        print(json.dumps(identity_spec(m, ms), ensure_ascii=False, indent=1))
        sys.exit(0)
    print(__doc__)

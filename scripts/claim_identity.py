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
    "スマートパチスロ", "スマートスロット", "スマスロ", "パチスロ", "ぱちスロ",
    "スロット", "メダルレス",
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
# 2026-07-21 Codexレビュー: 「パチスロ北斗の拳(2003)」「吉宗(2003/2013/2025)」など
# 【うちのカタログに無い同名の旧機種】は、販売区分語を落とすと芯が完全に同じになる。
#
# ★2巡目の是正★: 「スマスロ」と「パチスロ/6号機」は排他ではない（正式名称
#   「Lパチスロ 炎炎ノ消防隊」、メーカー分類「6号機（スマスロ）」が実在する）。
#   そこで判定は次の2点だけに絞る:
#     ・パチンコ機の表記があれば不合格（当サイトはパチスロのみ）
#     ・スマスロ機かどうかが自機種とタイトルで食い違えば不合格
#   自機種がスマスロかは machines.json の info（例「スマスロAT」「6.5号機 AT」
#   「Aタイプ」）を第一の根拠にする＝名前の表記ゆれに依存しない。
_SMART_WORDS = ("スマスロ", "スマートパチスロ", "スマートスロット", "スマート遊技機")
_PACHINKO_WORDS = ("パチンコ", "ぱちんこ", "スマパチ", "cr機", "cr ")
# P機・e機の接頭辞（「Pバンドリ」「e北斗の拳10」）＝パチンコ機（2026-07-21 Codex指摘4）
_PACHINKO_PREFIX_RE = re.compile(r"(?<![0-9a-z])[pe](?=[^0-9a-z\s])", re.IGNORECASE)
# 比較記事の語（単一機種の値の出典には使わない・指摘1/3/5）
_COMPARE_WORDS = ("比較", "VS", "ｖｓ", "vs", "どっち", "違い", "対決", "どちらが")
# 「L」「Lパチスロ」等の型式接頭辞（スマスロ機に付く）。語の先頭にある L のみ。
_L_PREFIX_RE = re.compile(r"(?:^|[\s　【\[(（])[lｌ](?=[^a-z]|$)", re.IGNORECASE)
# 「スマスロ○○」「L○○」＝機種名が名指しされている箇所（比較記事の検出に使う）
_MARKER_RE = re.compile(
    r"(?:スマスロ|スマートパチスロ|スマートスロット|(?<![0-9a-z])[lｌ](?=[^0-9a-z]))"
    r"\s*([^\s、。｜|【】]{2,20})", re.IGNORECASE)


def is_smart_text(s: str) -> bool:
    """文字列がスマスロ機を指しているか（スマスロ表記 or 型式接頭辞L）。"""
    t = unicodedata.normalize("NFKC", str(s or "")).lower()
    if any(unicodedata.normalize("NFKC", w).lower() in t for w in _SMART_WORDS):
        return True
    return bool(_L_PREFIX_RE.search(t))


def is_pachinko_text(s: str) -> bool:
    t = unicodedata.normalize("NFKC", str(s or "")).lower()
    if any(unicodedata.normalize("NFKC", w).lower() in t for w in _PACHINKO_WORDS):
        return True
    return bool(_PACHINKO_PREFIX_RE.search(t))


def detect_tags(s: str) -> set[str]:
    """文字列から読み取れるタグ（smart / pachinko）。"""
    tags = set()
    if is_smart_text(s):
        tags.add("smart")
    if is_pachinko_text(s):
        tags.add("pachinko")
    return tags


def machine_tags(machine: dict) -> set[str]:
    """自機種のタグ。★info（スマスロAT等）を第一の根拠にする★"""
    tags = set()
    if is_smart_text(machine.get("info") or "") or is_smart_text(machine.get("name") or ""):
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
    region = name_region(title, cores) or title
    t_tags = detect_tags(region)
    if "pachinko" in t_tags:
        return False, f"パチンコ版のページ（機種名区間=「{region[:40]}」）"
    my_smart, t_smart = ("smart" in my_tags), ("smart" in t_tags)
    if my_smart and not t_smart:
        return False, ("自機種はスマスロだがタイトルにスマスロ表記が無い"
                       f"（同名の旧機種の疑い・機種名区間=「{region[:40]}」）")
    if t_smart and not my_smart:
        return False, ("タイトルはスマスロ版だが自機種はスマスロではない"
                       f"（後継機のページの疑い・機種名区間=「{region[:40]}」）")
    return True, "世代タグOK"


def check_title(title: str, cores, reject_cores=(), reject_name_cores=()) -> tuple[bool, str]:
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

    longest_hit = max(hit, key=len)

    # ★(0-a) 比較記事そのものを拒否する（2026-07-21 Codex3巡目 指摘1・3・5）★
    #   停止語より後ろは切り落とされるため「天井性能を吉宗と比較」型は名前検査で拾えない。
    #   そもそも比較記事は単一機種の値の出典に向かないので、語の存在だけで落とす。
    tl = t_norm.lower()
    for w in _COMPARE_WORDS:
        if w.lower() in tl:
            return False, f"比較記事の疑い（タイトルに「{w}」）→単一機種の出典に使わない"

    # ★(0-b) タイトル全体を位置つきで走査する★（停止語で切らない・包含で除外しない）
    #   ・他機種の【正式名の芯】が、自機種名と重ならない位置に出てきたら不合格
    #     （親機種⇔続編の比較が両方向で抜けていた・指摘2/3）
    #   ・自機種名の直後に英数字や世代語が続く出現があれば不合格
    #     （まだカタログに無い続編「バンドリ2」等・指摘6）
    full = normalize_core(t_norm)
    mine_spans = []
    for c in cores:
        st = full.find(c)
        while st >= 0:
            mine_spans.append((st, st + len(c)))
            st = full.find(c, st + 1)
    for rc in reject_name_cores or ():
        if len(rc) < 2:
            continue
        st = full.find(rc)
        while st >= 0:
            en = st + len(rc)
            covered = any(s <= st and en <= e for s, e in mine_spans)
            if not covered:
                return False, (f"タイトルに他機種の名前がある（{rc}）"
                               f"→比較記事・別機種の疑いで不合格")
            st = full.find(rc, st + 1)
    for s_, e in mine_spans:
        nxt = full[e:e + 1]
        # 「新台」「新基準」等は続編記号ではなく記事の定型語（誤って落とさない）
        if full[e:e + 2] in ("新台", "新基", "新装"):
            continue
        if nxt and (re.match(r"[0-9a-z]", nxt) or nxt in "改真新極零"):
            if not any(full[s_:e] + nxt == c[:e - s_ + 1] and len(c) > e - s_ for c in cores):
                return False, (f"機種名の直後に「{nxt}」が続く（…{full[s_:e + 3]}…）"
                               f"→続編・派生機の疑いで不合格")

    for g in title_groups(title):
        gcore = normalize_core(" ".join(g))
        if not gcore:
            continue
        is_hit_group = any(normalize_core(p) in cores for p in g)
        # (a) 他機種の名前が区間の中に埋まっている（例「VS L吉宗」）
        for rc in rej:
            # 2文字の機種名（吉宗・番長等）も検出するため長さ2から見る
            if len(rc) < 2 or rc in longest_hit or any(rc in c for c in cores):
                continue
            if rc in gcore:
                return False, (f"タイトルに他機種の名前が埋まっている（{rc}）"
                               f"→比較記事等の疑いで不合格")
        # (b) 一致した芯の直後に英数字・世代語が続く（続編・派生機の疑い）
        if is_hit_group:
            pos = gcore.find(longest_hit)
            if pos >= 0:
                nxt = gcore[pos + len(longest_hit):pos + len(longest_hit) + 1]
                # ★日本語の文字は isalnum() が True になるので使わない（英数字だけ見る）★
                if nxt and (re.match(r"[0-9a-z]", nxt) or nxt in "改真新極零"):
                    return False, (f"機種名の直後に「{nxt}」が続く（{longest_hit}{nxt}…）"
                                   f"→続編・派生機の疑いで不合格")
        # (c) 機種名区間の外に「2」「V」等の続編記号だけが置かれている
        elif re.fullmatch(r"[0-9]{1,2}|v|vi|ex|z|改|真|新", gcore):
            return False, (f"機種名の外に続編記号「{gcore}」がある→別機種の疑いで不合格")

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
        # ★他機種の「正式名の芯」だけの一覧（別名は含めない）★
        #   タイトル全体を走査する検査に使う。別名（例「ディスク」）は汎用語に当たるため
        #   全体走査には使わない（正しい出典を誤って落とすため）。
        "reject_name_cores": sorted({normalize_core(m.get("name", "")) for m in machines
                                     if m.get("slug") != machine.get("slug")}
                                    - set(accept_cores_for(machine, machines)) - {""}),
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
        spec = identity_spec(m, cat)
        return check_title(title, spec["machine_cores"], spec["reject_cores"],
                           spec["reject_name_cores"])[0]

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

    # --- 2巡目Codexレビュー: スマスロ判定は info を根拠にする ---
    yoshimune_new = {"slug": "yoshimune_new", "name": "真打 吉宗", "info": "スマスロAT",
                     "aliases": ["吉宗"]}
    yoshimune_old = {"slug": "yoshimune_old", "name": "吉宗", "info": "5号機 AT"}
    eq(machine_tags(yoshimune_new), {"smart"}, "info=スマスロATならスマスロ扱い")
    eq(machine_tags(yoshimune_old), set(), "info=5号機ならスマスロではない")
    eq(check_tags("【吉宗】天井・解析", machine_tags(yoshimune_new))[0], False,
       "スマスロ機に無印タイトルは不合格")
    eq(check_tags("【スマスロ吉宗】天井・解析", machine_tags(yoshimune_old))[0], False,
       "旧機種にスマスロ版タイトルは不合格")
    eq(check_tags("【スマスロ吉宗】天井・解析", machine_tags(yoshimune_new))[0], True,
       "スマスロ同士は合格")
    # 正式名称に「パチスロ」を含むスマスロ機が自己不合格にならない（Codex指摘6）
    enen_l = {"slug": "enen_l", "name": "Lパチスロ 炎炎ノ消防隊", "info": "スマスロAT"}
    eq(machine_tags(enen_l), {"smart"}, "Lパチスロ…はスマスロ扱い")
    eq(check_tags("【Lパチスロ 炎炎ノ消防隊】天井", machine_tags(enen_l))[0], True,
       "Lパチスロ表記の正しいタイトルが通る（自己不合格しない）")
    valv = {"slug": "v2", "name": "Lパチスロ 革命機ヴァルヴレイヴ2", "info": "スマスロAT"}
    eq(check_tags("【Lパチスロ 革命機ヴァルヴレイヴ2】天井", machine_tags(valv))[0], True,
       "Lパチスロ+続編番号の正式名称が通る")
    # メダル機に対してスマスロ版タイトルは不合格（Codex指摘2）
    enen_medal = {"slug": "enen_m", "name": "パチスロ 炎炎ノ消防隊", "info": "6.5号機 AT"}
    eq(check_tags("【Lパチスロ 炎炎ノ消防隊】天井", machine_tags(enem := enen_medal))[0], False,
       "メダル機にスマスロ版タイトルは不合格")
    # スマートパチスロ表記（Codex指摘7）
    eq(is_smart_text("スマートパチスロ北斗の拳"), True, "スマートパチスロを認識")
    eq(normalize_core("スマートパチスロ北斗の拳"), "北斗の拳", "スマートパチスロを芯から落とす")
    eq(check_tags("【スマートパチスロ北斗の拳】天井", {"smart"})[0], True, "正式名称表記が通る")
    # L+数字（Codex指摘8）
    eq(check_tags("【L009 RE:CYBORG】天井", {"smart"})[0], True, "L009がスマスロと認識される")
    # 括弧の外の比較・続編（Codex指摘3・4）
    eq(check_title("【スマスロ北斗の拳】VS L吉宗 天井比較", ["北斗の拳"], ["吉宗"])[0], False,
       "括弧の外の他機種併記は不合格")
    eq(check_title("【スマスロ北斗の拳】天井性能をL吉宗と比較", ["北斗の拳"], ["吉宗"])[0], False,
       "文中の他機種併記も不合格")
    eq(check_title("【スマスロ炎炎ノ消防隊】2 天井", ["炎炎ノ消防隊"], [])[0], False,
       "括弧外の続編番号は不合格")
    eq(check_title("【スマスロ炎炎ノ消防隊2】天井", ["炎炎ノ消防隊2"], ["炎炎ノ消防隊"])[0], True,
       "続編機種自身のページは通る")

    # --- 3巡目Codexレビュー: 停止語の後ろ・親子機種・P/e機・未登録続編 ---
    valv2 = {"slug": "valv2", "name": "Lパチスロ 革命機ヴァルヴレイヴ2", "info": "スマスロAT"}
    valv1 = {"slug": "valv1", "name": "L革命機ヴァルヴレイヴ", "info": "スマスロAT"}
    vcat = [valv1, valv2]

    def vcheck(m, title):
        sp = identity_spec(m, vcat)
        if not check_tags(title, machine_tags(m), sp["machine_cores"])[0]:
            return False
        return check_title(title, sp["machine_cores"], sp["reject_cores"],
                           sp["reject_name_cores"])[0]

    eq(vcheck(valv2, "【Lパチスロ 革命機ヴァルヴレイヴ2】天井・解析"), True, "続編自身のページは通る")
    eq(vcheck(valv1, "【L革命機ヴァルヴレイヴ】天井・解析"), True, "前作自身のページは通る")
    eq(vcheck(valv2, "【Lパチスロ 革命機ヴァルヴレイヴ2】VS L革命機ヴァルヴレイヴ 天井比較"),
       False, "続編→前作の比較記事は不合格")
    eq(vcheck(valv1, "【L革命機ヴァルヴレイヴ】天井性能をL革命機ヴァルヴレイヴ2と比較"),
       False, "前作→続編の比較記事は不合格")
    # 停止語の後ろに他機種名がある比較記事（指摘1）
    hok = {"slug": "hokuto", "name": "スマスロ北斗の拳", "info": "スマスロAT", "aliases": ["北斗の拳"]}
    yos = {"slug": "yoshi", "name": "真打 吉宗", "info": "スマスロAT"}
    hcat = [hok, yos]

    def hcheck(title):
        sp = identity_spec(hok, hcat)
        if not check_tags(title, machine_tags(hok), sp["machine_cores"])[0]:
            return False
        return check_title(title, sp["machine_cores"], sp["reject_cores"],
                           sp["reject_name_cores"])[0]

    eq(hcheck("【スマスロ北斗の拳】天井性能を吉宗と比較"), False,
       "停止語の後ろの他機種併記も不合格")
    eq(hcheck("【スマスロ北斗の拳】VS SLOT魔法少女まどか☆マギカ 天井比較"), False,
       "カタログ外の他機種との比較記事も不合格（比較語で落とす）")
    eq(hcheck("【スマスロ北斗の拳】天井の恩恵や発動条件"), True, "単独ページは通る")
    # P機・e機（指摘4）
    eq(is_pachinko_text("【Pバンドリ！】スペック"), True, "P機を検出")
    eq(is_pachinko_text("【e北斗の拳10】天井"), True, "e機を検出")
    eq(is_pachinko_text("【スマスロ北斗の拳】天井"), False, "スマスロをP/e機と誤検出しない")
    eq(is_pachinko_text("【Lバキ 強くなりたくば喰らえ!!!】解析"), False, "L機を誤検出しない")
    eq(check_tags("【Pバンドリ！】スペック", {"smart"})[0], False, "P機タイトルは不合格")
    # カタログ未登録の続編（指摘6）
    eq(check_title("【スマスロ バンドリ！】バンドリ2 天井", ["バンドリ"], [], [])[0], False,
       "カタログに無い続編表記も不合格")
    eq(check_title("【スマスロ バンドリ！】バンドリ改 天井", ["バンドリ"], [], [])[0], False,
       "未登録の派生機表記も不合格")

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
        # ★負例の総当たり（2026-07-21 Codex指摘: 「落とす能力」を測っていない）★
        #   全機種について、別機種を意味する定型パターンのタイトルを機械生成し、
        #   1件でも合格したら不合格とする。
        neg_fail = []
        for m in real_ms:
            cs, rj, tg = accept_cores_for(m, real_ms), reject_cores_for(m, real_ms), machine_tags(m)
            if not cs:
                continue
            base = m["name"]
            other = next((o["name"] for o in real_ms
                          if not set(machine_cores(o)) & set(cs)), "スマスロ北斗の拳")
            for pat in (f"【{base}（パチンコ）】天井・解析",
                        f"【{base}（修羅の国篇）】天井",
                        f"【{base}2】天井とやめどき",
                        f"【{base}】【{other}】天井比較",
                        f"【{base}（5号機）】天井"):
                spec = identity_spec(m, real_ms)
                if check_tags(pat, tg, cs)[0] and check_title(
                        pat, cs, rj, spec["reject_name_cores"])[0]:
                    neg_fail.append((m["slug"], pat))
            # スマスロ機に「世代表記の無い同名タイトル」を当てる（同名旧機種の代表例）
            if "smart" in tg:
                plain = normalize_core(base)
                if check_tags(f"【{plain}】天井・解析", tg, cs)[0]:
                    neg_fail.append((m["slug"], f"世代表記なし:{plain}"))
        eq(neg_fail[:5], [], f"負例が合格した（{len(neg_fail)}件）")

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

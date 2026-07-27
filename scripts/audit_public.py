#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""audit_public.py — 公開射影の独立監査（gates.py の判断を信用しない最後の境界）

★なぜ別実装なのか★
  gates.py の射影が誤って通した場合、同じ関数・同じ正規表現を使う監査では気づけない
  （共通原因故障）。本ファイルは gates.py を **import せず**、禁止条件も許可契約も
  独自に持ち、「出来上がった公開データ」だけを見て判定する。

検査するもの:
  A. 公開契約   … 空でない／必須フィールド／slug形式・重複／許可フィールドのみ（allowlist）
                   ／URLの形式（httpsのみ・userinfo禁止・javascript等禁止）
  B. 表示内容   … 計算断定の残存／設定段階の非存在断定（列挙の欠番・引用の否定は除外）
                   ／数値があるのに目安ラベルが無い（固定文言と一致するか）
                   ／HTML描画後に現れる禁止語
  C. 表示原子   … label+value・表の行を**結合してから**判定（gatesの核心への独立な裏取り）

使い方:
    python scripts/audit_public.py --file dist/assets/data/machines.public.json
    python scripts/audit_public.py --selftest
終了コード: 0=合格 / 1=違反あり
"""
from __future__ import annotations

import argparse
import html as _html
import json
import re
import sys
import unicodedata

# 公開してよい固定文言（gates 側と同じ値だが、意図的にここへ独立に持つ）
EXPECTED_DISCLAIMER = "当サイトの目安です（メーカー公表値・確定解析ではありません）"

# --- 独自の禁止条件（gates.py とは別に持ち、表記も揃えない）---
FORBIDDEN_CLAIMS = (
    r"期待\s*収支", r"プラス\s*(?:域|圏|ライン)", r"プラス\s*期待値", r"期待値\s*(?:が)?\s*プラス",
    r"プラス\s*に\s*転じ", r"期待\s*枚数", r"獲得\s*枚数\s*期待", r"期待\s*差枚",
    r"損益\s*分岐", r"時給", r"利益\s*ゾーン", r"確実な\s*利益", r"プラス\s*収支",
    r"期待値\s*(?:の\s*絶対値\s*)?が\s*(?:乗|積み)", r"期待\s*収支\s*が\s*積み",
    r"[0-9０-９,]+\s*円\s*(?:以上)?\s*の\s*期待値",
)
FORBIDDEN_RE = re.compile("|".join(FORBIDDEN_CLAIMS))

_SET1 = r"[1-6１-６一二三四五六]"
# 設定の存在そのものを否定する形（語間が長い場合は「引用＋否定」かを見る）
SETTING_ABSENT_RE = re.compile(
    r"設定\s*" + _SET1 + r"(?:\s*[・、,／/･]\s*(?:設定\s*)?" + _SET1 + r")*"
    r"(?:(?!。|設定)[^。]){0,30}?"
    r"(?:非搭載|未搭載|存在しない|搭載していない|搭載されていない|ありません|無い|ない|なし|無し)")
# 「〜との声もありますが公式では6段階」のように、その断定を打ち消している文脈
REFUTATION_RE = re.compile(
    r"(?:との声|とのうわさ|との噂|と言われて|とされていますが|ですが)[^。]{0,40}"
    r"(?:公式|メーカー|ホール告知|表記されて|6段階|六段階)|"
    r"(?:公式|メーカー)[^。]{0,20}(?:6段階|六段階|と表記)")
# 「設定1では出現しない」のような、存在ではなく挙動の否定は対象外
BEHAVIOR_NEG_RE = re.compile(r"設定\s*" + _SET1 + r"\s*で\s*は")

# 公開してよいフィールド（allowlist。ここに無いキーは流出とみなす）
ALLOWED_MACHINE_KEYS = {
    "slug", "name", "manufacturer", "info", "release_date", "confirmed_at", "sources",
    "seo", "strategy", "strategyByRate", "aliases", "limit", "tenjo_display",
    "original", "checker", "disclaimer", "display_requirements",
}
REQUIRED_MACHINE_KEYS = {"slug", "name"}
# authoring 専用（絶対に公開物へ出てはいけない）
AUTHORING_ONLY = {"lifecycle", "status", "checker_modes", "checker_kill_switch", "_disabled"}

SECRET_HINT_RE = re.compile(r"token|key=|sig=|signature|auth|session|password", re.I)
SLUG_RE = re.compile(r"^[a-z0-9_]+$")

_TAG = re.compile(r"<[^>]*>")
_ZW = re.compile(r"[​-‏⁠﻿­]")
_MD = re.compile(r"[*_`~]")
_KANJI = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}
_ENUM = re.compile(r"設定\s*" + _SET1 + r"(?:\s*[・、,／/･]\s*(?:設定\s*)?" + _SET1 + r"){1,5}")
_ONE = re.compile(_SET1)
_NUM = re.compile(r"[0-9０-９]")


def as_displayed(s: str) -> str:
    """ブラウザで見える形に寄せる（gates.py とは別実装で同じ狙いを果たす）。"""
    for _ in range(6):
        t = _html.unescape(s)
        if t == s:
            break
        s = t
    s = _TAG.sub("", s)
    s = _MD.sub("", s)
    s = _ZW.sub("", s)
    return unicodedata.normalize("NFKC", s)


def _enum_gap(text: str) -> bool:
    for m in _ENUM.finditer(text):
        nums = sorted({_KANJI.get(c) or int(str(c).translate(str.maketrans("１２３４５６", "123456")))
                       for c in _ONE.findall(m.group(0))})
        if len(nums) >= 2 and set(range(nums[0], nums[-1] + 1)) - set(nums):
            return True
    return False


_ONLY_RE = re.compile(r"のみ|だけ|に限[らりる]")


def _asserts_missing_setting(text: str) -> bool:
    """設定の非存在を主張しているか。挙動の否定・引用の否定は「その主張ごとに」除外する。"""
    if _enum_gap(text):
        return True
    # 端の欠番でも「のみ」が続けば非搭載の主張（設定1/2/3/4/5のみ）
    for m in _ENUM.finditer(text):
        nums = sorted({_KANJI.get(c) or int(str(c).translate(str.maketrans("１２３４５６", "123456")))
                       for c in _ONE.findall(m.group(0))})
        if len(nums) >= 2 and set(nums) != {1, 2, 3, 4, 5, 6} and _ONLY_RE.search(text[m.end():m.end() + 8]):
            return True
    # ★1件目で判断せず、全ての一致を個別に見る★
    #   （「設定3は非搭載。設定4がないとの声もありますが公式では6段階」で
    #     後半の打ち消しが前半の断定まで免罪してしまうのを防ぐ）
    for m in SETTING_ABSENT_RE.finditer(text):
        span = m.group(0)
        if BEHAVIOR_NEG_RE.search(span):
            continue                      # 「設定1では出現しない」＝挙動の話
        # その主張を含む文（。区切り）の中に打ち消しがあるかだけを見る
        start = text.rfind("。", 0, m.start()) + 1
        end = text.find("。", m.end())
        sentence = text[start: end if end != -1 else len(text)]
        if REFUTATION_RE.search(sentence):
            continue
        return True
    return False


def _atoms(node, path, out):
    """表示される塊を組み立てる。文字列単体だけでなく label+value・表の行も結合する。"""
    if isinstance(node, str):
        out.append((path, node))
    elif isinstance(node, (int, float)) and not isinstance(node, bool):
        out.append((path, str(node)))     # ★数値も走査対象（limit:999 等の見落とし防止）★
    elif isinstance(node, list):
        # 配列が「行」なら結合した原子も作る（辞書セル {text,badge} も文字列化して結合）
        joined = []
        for x in node:
            if isinstance(x, (str, int, float)) and not isinstance(x, bool):
                joined.append(str(x))
            elif isinstance(x, dict):
                cell = " ".join(str(x[k]) for k in ("badge", "text")
                                if isinstance(x.get(k), (str, int, float))
                                and not isinstance(x.get(k), bool))
                if cell:
                    joined.append(cell)
        if len(joined) >= 2:
            out.append((path, " / ".join(joined)))
        for i, v in enumerate(node):
            _atoms(v, f"{path}[{i}]", out)
    elif isinstance(node, dict):
        for k in node:
            out.append((f"{path}.<key>", str(k)))
        # label+value / text+badge / trigger+hint 等を結合
        pairs = [("label", "value"), ("text", "badge"), ("trigger", "hint"),
                 ("left", "right"), ("title", "value")]
        for a, b in pairs:
            if a in node or b in node:
                joined = " / ".join(str(node[x]) for x in (a, b)
                                    if isinstance(node.get(x), (str, int, float))
                                    and not isinstance(node.get(x), bool))
                if joined:
                    out.append((path, joined))
        # ★見出し＋配下の内容を結合★（gatesの「見出し＋段落」「表label＋見出し行＋行」に対応）
        head = node.get("title") if isinstance(node.get("title"), str) else \
            (node.get("label") if isinstance(node.get("label"), str) else None)
        if head:
            for field in ("body", "rows", "headers", "tables"):
                seq = node.get(field)
                if not isinstance(seq, list):
                    continue
                for i, el in enumerate(seq):
                    if isinstance(el, str):
                        out.append((f"{path}.{field}[{i}]", f"{head} / {el}"))
                    elif isinstance(el, dict):
                        # ★見出し＋表ラベル＋見出し行＋行 を「全部つなげた原子」も作る★
                        #   例: section title=「期」/ table label=「待」/ row=["収支が","プラス"]
                        #   のように細かく割られた断定を、全要素同時連結で捕まえる
                        sub_label = el.get("label") if isinstance(el.get("label"), str) else None
                        heads = el.get("headers") if isinstance(el.get("headers"), list) else []
                        head_txt = [h for h in heads if isinstance(h, str)]
                        for sub in ("rows", "headers", "body"):
                            if not isinstance(el.get(sub), list):
                                continue
                            for j, row in enumerate(el[sub]):
                                cells = row if isinstance(row, list) else [row]
                                txt = [str(c) if not isinstance(c, dict)
                                       else " ".join(str(c[k]) for k in ("badge", "text")
                                                     if isinstance(c.get(k), str))
                                       for c in cells]
                                parts = [head, sub_label, *(head_txt if sub == "rows" else []), *txt]
                                out.append((f"{path}.{field}[{i}].{sub}[{j}]",
                                            " / ".join(p for p in parts if p)))
                    elif isinstance(el, list):
                        cells = [str(x) if not isinstance(x, dict)
                                 else " ".join(str(x[k]) for k in ("badge", "text")
                                               if isinstance(x.get(k), str))
                                 for x in el]
                        out.append((f"{path}.{field}[{i}]", " / ".join([head, *cells])))
        for k, v in node.items():
            _atoms(v, f"{path}.{k}", out)


def _check_url(u: str) -> str | None:
    if not isinstance(u, str):
        return "URLが文字列でない"
    if not u.startswith("https://"):
        return "httpsでないURL"
    if "@" in u.split("/", 3)[2]:
        return "userinfo付きURL"
    if re.search(r"[?#]", u) or SECRET_HINT_RE.search(u):
        return "秘密を含みうるURL（クエリ/フラグメント/認証情報）"
    return None


# checker の公開契約（gates とは別に、監査側で独自に持つ）
_CK_TOP = {"unit", "equivOnly", "limit", "modes", "exchangeRates", "defaultRate",
           "hasSuru", "hasCycle", "suruMax", "ok", "ng"}
_CK_MODE = {"excellent", "good", "caution", "limit", "suruMax", "target", "count",
            "note", "cycle", "suru", "byRate"}
_CK_NUM = {"excellent", "good", "caution", "limit", "suruMax", "target", "count"}


def _audit_checker_shape(slug: str, ck, path: str = "checker") -> list[str]:
    """checker の中身を再帰的に検査する（未知フィールド・数値の型崩れを通さない）。"""
    out: list[str] = []
    if ck is None:
        return out
    if not isinstance(ck, dict):
        return [f"{slug}: {path} が辞書でない"]
    for k, v in ck.items():
        if k in _CK_TOP:
            if k in ("limit", "suruMax") and not isinstance(v, (int, float)):
                out.append(f"{slug}: {path}.{k} の型が不正")
            if k in ("unit", "defaultRate", "ok", "ng") and not isinstance(v, str):
                out.append(f"{slug}: {path}.{k} の型が不正")
            if k in ("hasSuru", "hasCycle", "equivOnly") and not isinstance(v, bool):
                out.append(f"{slug}: {path}.{k} の型が不正")
            if k == "modes":
                if not isinstance(v, list):
                    out.append(f"{slug}: {path}.modes の型が不正")
                else:
                    for i, mm in enumerate(v):
                        if not (isinstance(mm, dict)
                                and set(mm.keys()) <= {"key", "label", "hasSuru", "hasCycle"}):
                            out.append(f"{slug}: {path}.modes[{i}] の形が不正")
            if k == "exchangeRates":
                if not isinstance(v, list):
                    out.append(f"{slug}: {path}.exchangeRates の型が不正")
                else:
                    for i, r in enumerate(v):
                        if not (isinstance(r, dict) and set(r.keys()) <= {"key", "label"}
                                and isinstance(r.get("key"), str)):
                            out.append(f"{slug}: {path}.exchangeRates[{i}] の形が不正")
            continue
        # mode 設定
        if not isinstance(v, dict):
            out.append(f"{slug}: {path}.{k} が辞書でない（未知フィールドの疑い）")
            continue
        if set(v.keys()) - _CK_MODE:
            out.append(f"{slug}: {path}.{k} に未知フィールド")
        for nk in _CK_NUM & set(v.keys()):
            if not isinstance(v[nk], (int, float)) or isinstance(v[nk], bool):
                out.append(f"{slug}: {path}.{k}.{nk} が数値でない")
        if "note" in v and not isinstance(v["note"], str):
            out.append(f"{slug}: {path}.{k}.note の型が不正")
        for seq in ("suru", "cycle"):
            if seq in v:
                if not isinstance(v[seq], list):
                    out.append(f"{slug}: {path}.{k}.{seq} の型が不正")
                else:
                    for i, row in enumerate(v[seq]):
                        if isinstance(row, dict):
                            out.extend(_audit_checker_shape(slug, {"row": row},
                                                            f"{path}.{k}.{seq}[{i}]"))
        if "byRate" in v:
            if not isinstance(v["byRate"], dict):
                out.append(f"{slug}: {path}.{k}.byRate の型が不正")
            else:
                for rk, rv in v["byRate"].items():
                    out.extend(_audit_checker_shape(slug, {rk: rv}, f"{path}.{k}.byRate"))
    return out


_COUNT_MODES = ("suru", "through", "cycle")


def _audit_axis(slug: str, ck) -> list[str]:
    """★軸契約を gates とは独立に検査する★

    gates 側が誤って通した場合に備え、同じ関数を使わずに書く（共通原因故障の回避）。
    回数系modeは「入力単位がG」「直下閾値を持たない」「回数ごとの行を持つ」
    「行は count(整数・一意・昇順)＋G数の判定材料を持つ」を満たすこと。
    """
    out: list[str] = []
    if not isinstance(ck, dict):
        return out
    unit = ck.get("unit")
    decl = ck.get("modes") if isinstance(ck.get("modes"), list) else []
    flags = {m.get("key"): m for m in decl if isinstance(m, dict)}
    for k, v in ck.items():
        if k in _CK_TOP or not isinstance(v, dict):
            continue
        d = flags.get(k, {})
        hs, hc = d.get("hasSuru") is True, d.get("hasCycle") is True
        if not (k in _COUNT_MODES or hs or hc):
            continue
        if hs and hc:
            out.append(f"{slug}: checker.{k} が hasSuru と hasCycle を同時宣言")
        rows = v.get("suru") if isinstance(v.get("suru"), list) else (
            v.get("cycle") if isinstance(v.get("cycle"), list) else None)
        if isinstance(v.get("suru"), list) and isinstance(v.get("cycle"), list):
            out.append(f"{slug}: checker.{k} が suru[] と cycle[] の両方を持つ")
        if hs and isinstance(v.get("cycle"), list):
            out.append(f"{slug}: checker.{k} は hasSuru 宣言だが cycle[] を持つ")
        if hc and isinstance(v.get("suru"), list):
            out.append(f"{slug}: checker.{k} は hasCycle 宣言だが suru[] を持つ")
        direct = [x for x in ("excellent", "good", "caution", "target")
                  if isinstance(v.get(x), (int, float)) and not isinstance(v.get(x), bool)]
        if rows is None:
            out.append(f"{slug}: checker.{k} が回数ごとの行を持たない"
                       + ("（直下閾値のみ＝軸の食い違い）" if direct else "（判定材料が無い）"))
            continue
        if direct:
            out.append(f"{slug}: checker.{k} が直下閾値と行を併存させている")
        if unit not in ("G", "g"):
            out.append(f"{slug}: checker.{k} の入力単位が {unit!r}（回数系modeでは 'G' が必須）")
        seen, prev = set(), None
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                out.append(f"{slug}: checker.{k}.rows[{i}] が辞書でない"); continue
            cv = row.get("count")
            if type(cv) is not int or isinstance(cv, bool) or cv < 0:
                out.append(f"{slug}: checker.{k}.rows[{i}] の count が0以上の整数でない"); continue
            if cv in seen:
                out.append(f"{slug}: checker.{k}.rows[{i}] の count が重複")
            if prev is not None and cv < prev:
                out.append(f"{slug}: checker.{k}.rows[{i}] の count が昇順でない")
            seen.add(cv); prev = cv
            ok_direct = any(isinstance(row.get(x), (int, float)) and not isinstance(row.get(x), bool)
                            for x in ("excellent", "good", "caution", "target"))
            by = row.get("byRate")
            ok_rate = isinstance(by, dict) and any(
                isinstance(rv, dict) and any(
                    isinstance(rv.get(x), (int, float)) and not isinstance(rv.get(x), bool)
                    for x in ("excellent", "good", "caution", "target"))
                for rv in by.values())
            if not (ok_direct or ok_rate):
                out.append(f"{slug}: checker.{k}.rows[{i}] にG数の判定材料が無い")
    return out


def audit_machine(pub: dict, seen_slugs: set | None = None) -> list[str]:
    """公開射影された1機種分を検査し、違反の一覧を返す。"""
    problems: list[str] = []
    if not isinstance(pub, dict) or not pub:
        return ["公開データが空または辞書でない"]
    slug = pub.get("slug", "?")

    # --- A. 公開契約 ---
    for k in REQUIRED_MACHINE_KEYS:
        v = pub.get(k)
        if not v:
            problems.append(f"{slug}: 必須フィールド {k} が無い")
        elif not isinstance(v, str):      # truthy だけで通さない（name: true を弾く）
            problems.append(f"{slug}: 必須フィールド {k} の型が不正")
    # 入れ子の型契約（想定と違う形のまま公開しない）
    for k, typ in (("info", str), ("strategy", str), ("aliases", list), ("seo", dict),
                   ("checker", dict), ("sources", list), ("strategyByRate", dict),
                   ("original", dict), ("display_requirements", dict),
                   ("manufacturer", str), ("tenjo_display", str)):
        if k in pub and not isinstance(pub[k], typ):
            problems.append(f"{slug}: フィールド {k} の型が不正")
    if not (isinstance(slug, str) and SLUG_RE.match(slug)):
        problems.append(f"{slug}: slugの形式が不正")
    elif seen_slugs is not None:
        if slug in seen_slugs:
            problems.append(f"{slug}: slugが重複")
        seen_slugs.add(slug)
    for k in pub:
        if k in AUTHORING_ONLY:
            problems.append(f"{slug}: authoring専用フィールドの流出 {k}")
        elif k not in ALLOWED_MACHINE_KEYS:
            problems.append(f"{slug}: 許可されていないフィールド {k}")
    # ★入れ子も allowlist ＋ 型契約で fail-closed にする★
    for i, s in enumerate(pub.get("sources") or []):
        if not isinstance(s, dict):
            problems.append(f"{slug}: sources[{i}] が辞書でない")
            continue
        if set(s.keys()) - {"url", "title", "confirmed_at"}:
            problems.append(f"{slug}: sources[{i}] に未知フィールド")
        for k in ("title", "confirmed_at"):
            if k in s and not isinstance(s[k], str):
                problems.append(f"{slug}: sources[{i}].{k} の型が不正")
        why = _check_url(s.get("url", ""))
        if why:
            problems.append(f"{slug}: {why} sources[{i}]")
    for fld, allowed in (("seo", {"title", "description"}),
                         ("original", {"title", "kind", "search"}),
                         ("display_requirements", {"disclaimer", "surfaces"})):
        v = pub.get(fld)
        if isinstance(v, dict):
            if set(v.keys()) - allowed:
                problems.append(f"{slug}: {fld} に未知フィールド")
            for k, vv in v.items():
                if k == "surfaces":
                    if not (isinstance(vv, list) and all(isinstance(x, str) for x in vv)):
                        problems.append(f"{slug}: display_requirements.surfaces の型が不正")
                elif not isinstance(vv, str):
                    problems.append(f"{slug}: {fld}.{k} の型が不正")
    problems.extend(_audit_checker_shape(slug, pub.get("checker")))
    problems.extend(_audit_axis(slug, pub.get("checker")))
    # ★checkerを出すなら、目安ラベルの対象にcheckerが入っていること★
    #   （gates側の新しい不変条件を、独立にも担保する）
    dr = pub.get("display_requirements")
    if "checker" in pub:
        if not isinstance(dr, dict) or not isinstance(dr.get("surfaces"), list):
            problems.append(f"{slug}: checkerを公開しているのに表示要件が無い")
        elif "checker" not in dr["surfaces"]:
            problems.append(f"{slug}: checkerが目安ラベルの表示面に含まれていない")
    if dr is not None and not isinstance(dr, dict):
        problems.append(f"{slug}: display_requirements の型が不正")

    # --- B/C. 表示内容（原子単位） ---
    leaves: list[tuple[str, str]] = []
    _atoms(pub, "machine", leaves)
    has_number = False
    for path, raw in leaves:
        shown = as_displayed(str(raw))
        if path.endswith(".<key>"):
            if re.match(r"^_", shown):
                problems.append(f"{slug}: 内部フィールドの流出 {shown}")
            continue
        # ★結合した原子は区切り記号を跨いでも判定する★
        #   （"期待値が / プラス" のように、別セルに割れた断定を取り逃がさない）
        # 区切りを空白に置換した形と、完全に除去した形の両方を見る
        # （gates 側と同じ強度にする。「期／待／収支が／プラス」のような分割を取り逃がさない）
        variants = (shown,
                    re.sub(r"\s*[/／|｜]\s*", " ", shown),
                    re.sub(r"\s*[/／|｜]\s*", "", shown))
        if any(FORBIDDEN_RE.search(v) for v in variants):
            problems.append(f"{slug}: 計算断定の残存 {path}")
        if any(_asserts_missing_setting(v) for v in variants):
            problems.append(f"{slug}: 設定段階の非存在断定 {path}")
        if (_NUM.search(shown)
                and not path.startswith(("machine.slug", "machine.release_date",
                                         "machine.confirmed_at", "machine.disclaimer",
                                         "machine.display_requirements"))
                and not (path.startswith("machine.sources") and not path.endswith(".title"))):
            has_number = True

    # --- 目安ラベル（固定文言と一致しているか）---
    if has_number:
        d = pub.get("disclaimer")
        if not isinstance(d, str) or d != EXPECTED_DISCLAIMER:
            problems.append(f"{slug}: 数値を公開しているのに目安ラベルが正しくない")
    return problems


ALLOWED_DETAIL_KEYS = {"lead", "summaryBoxes", "factTable", "sections"}


def audit_detail(slug: str, detail: dict, has_disclaimer: bool) -> list[str]:
    """公開射影された記事データを検査する（機種データとは契約が違う）。"""
    problems: list[str] = []
    if not isinstance(detail, dict):
        return [f"{slug}: 記事データが辞書でない"]
    for k in detail:
        if k in AUTHORING_ONLY:
            problems.append(f"{slug}: authoring専用フィールドの流出 {k}")
        elif k not in ALLOWED_DETAIL_KEYS:
            problems.append(f"{slug}: 記事に許可されていないフィールド {k}")
    # 入れ子の型契約（想定と違う形のまま公開しない）
    for k, typ in (("lead", str), ("summaryBoxes", list), ("factTable", list), ("sections", list)):
        if k in detail and not isinstance(detail[k], typ):
            problems.append(f"{slug}: 記事フィールド {k} の型が不正")
    for i, s in enumerate(detail.get("sections") or []):
        if not isinstance(s, dict):
            problems.append(f"{slug}: sections[{i}] が辞書でない")
            continue
        if "title" in s and not isinstance(s["title"], str):
            problems.append(f"{slug}: sections[{i}].title の型が不正")
        if "type" in s and s["type"] not in ("rumor", "settei"):
            problems.append(f"{slug}: sections[{i}].type が未知の値")
        if set(s.keys()) - {"title", "type", "body", "tables", "rows"}:
            problems.append(f"{slug}: sections[{i}] に未知フィールド")
        for f2, t2 in (("body", list), ("tables", list), ("rows", list)):
            if f2 in s and not isinstance(s[f2], t2):
                problems.append(f"{slug}: sections[{i}].{f2} の型が不正")
        for ti, tb in enumerate(s.get("tables") or []):
            if not isinstance(tb, dict):
                problems.append(f"{slug}: sections[{i}].tables[{ti}] が辞書でない")
                continue
            if set(tb.keys()) - {"label", "headers", "rows", "note", "wide"}:
                problems.append(f"{slug}: sections[{i}].tables[{ti}] に未知フィールド")
            for f3, t3 in (("label", str), ("headers", list), ("rows", list),
                           ("note", str), ("wide", bool)):
                if f3 in tb and not isinstance(tb[f3], t3):
                    problems.append(f"{slug}: sections[{i}].tables[{ti}].{f3} の型が不正")
            for ri, row in enumerate(tb.get("rows") or []):
                for c in (row if isinstance(row, list) else [row]):
                    if isinstance(c, dict) and set(c.keys()) - {"text", "badge"}:
                        problems.append(f"{slug}: sections[{i}].tables[{ti}].rows[{ri}] のセルに未知フィールド")
                    elif not isinstance(c, (str, dict)):
                        problems.append(f"{slug}: sections[{i}].tables[{ti}].rows[{ri}] のセル型が不正")
    for i, r in enumerate(detail.get("factTable") or []):
        if not (isinstance(r, list) and len(r) == 2 and all(isinstance(x, str) for x in r)):
            problems.append(f"{slug}: factTable[{i}] が2要素の文字列配列でない")
    for i, b in enumerate(detail.get("summaryBoxes") or []):
        if not (isinstance(b, dict) and set(b.keys()) == {"label", "value"}
                and all(isinstance(v, str) for v in b.values())):
            problems.append(f"{slug}: summaryBoxes[{i}] の形が不正")

    leaves: list[tuple[str, str]] = []
    _atoms(detail, "detail", leaves)
    has_number = False
    for path, raw in leaves:
        shown = as_displayed(str(raw))
        if path.endswith(".<key>"):
            if re.match(r"^_", shown):
                problems.append(f"{slug}: 内部フィールドの流出 {shown}")
            continue
        # 区切りを空白に置換した形と、完全に除去した形の両方を見る
        # （gates 側と同じ強度にする。「期／待／収支が／プラス」のような分割を取り逃がさない）
        variants = (shown,
                    re.sub(r"\s*[/／|｜]\s*", " ", shown),
                    re.sub(r"\s*[/／|｜]\s*", "", shown))
        if any(FORBIDDEN_RE.search(v) for v in variants):
            problems.append(f"{slug}: 計算断定の残存 {path}")
        if any(_asserts_missing_setting(v) for v in variants):
            problems.append(f"{slug}: 設定段階の非存在断定 {path}")
        if _NUM.search(shown):
            has_number = True
    if has_number and not has_disclaimer:
        problems.append(f"{slug}: 記事に数値があるのに目安ラベルが無い")
    return problems


def audit_file(path: str, expected_count: int | None = None,
               details_dir: str | None = None) -> int:
    import os
    data = json.load(open(path, encoding="utf-8"))
    machines = data if isinstance(data, list) else data.get("machines", [])
    problems: list[str] = []
    if not machines:
        problems.append("公開データが空（0機種）")
    seen: set = set()
    for m in machines:
        problems.extend(audit_machine(m, seen))
        # ★記事データも必ず検査する（機種データだけ見て合格にしない）★
        if details_dir and isinstance(m, dict) and isinstance(m.get("slug"), str):
            dp = os.path.join(details_dir, f"{m['slug']}.json")
            if os.path.isfile(dp):
                problems.extend(audit_detail(
                    m["slug"], json.load(open(dp, encoding="utf-8")),
                    has_disclaimer=isinstance(m.get("disclaimer"), str)
                    and m["disclaimer"] == EXPECTED_DISCLAIMER))
            else:
                # ★記事ファイルが無いことを「検査不要」にしない★
                #   （ファイルを置き忘れれば検査を素通りできてしまう）
                problems.append(f"{m['slug']}: 公開機種なのに記事データが見つからない")
    if details_dir is None:
        problems.append("記事データの検査先(--details-dir)が指定されていない")
    if expected_count is not None and len(machines) != expected_count:
        problems.append(f"機種数が想定と違う: {len(machines)} != {expected_count}")
    print(f"検査 {len(machines)} 機種 / 違反 {len(problems)} 件")
    for p in problems[:40]:
        print("  ✗", p)
    return 1 if problems else 0


def selftest() -> int:
    res = []

    def t(name, cond):
        res.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    ok = {"slug": "x", "name": "テスト機", "strategy": "等価600G〜",
          "disclaimer": EXPECTED_DISCLAIMER}
    t("正常な公開データは合格", audit_machine(ok) == [])
    t("数値があるのに目安ラベルが無ければ違反",
      any("目安ラベル" in p for p in audit_machine({k: v for k, v in ok.items() if k != "disclaimer"})))
    t("★目安ラベルが固定文言と違えば違反（truthyだけでは通さない）",
      any("目安ラベル" in p for p in audit_machine({**ok, "disclaimer": True}))
      and any("目安ラベル" in p for p in audit_machine({**ok, "disclaimer": "目安です"})))
    t("★数値型JSONも走査する（limit:999 だけでも検出）",
      any("目安ラベル" in p for p in audit_machine({"slug": "x", "name": "t", "limit": 999})))
    t("計算断定の残存を検出",
      any("計算断定" in p for p in audit_machine({**ok, "strategy": "580Gから期待収支がプラス"})))
    t("★分割された断定も結合して検出（label＋value）",
      any("計算断定" in p for p in audit_machine(
          {**ok, "checker": {"normal": {"label": "期待値が", "value": "プラス"}}})))
    t("★表の行も結合して検出（設定3 / 非搭載）",
      any("設定段階" in p for p in audit_machine(
          {**ok, "checker": {"normal": {"rows": [["設定3", "非搭載"]]}}})))
    t("★gates側の絶対禁止と揃っている（獲得枚数期待・期待値の絶対値が積み）",
      any("計算断定" in p for p in audit_machine({**ok, "strategy": "獲得枚数期待は大きい"}))
      and any("計算断定" in p for p in audit_machine({**ok, "strategy": "期待値の絶対値が積み上がる"})))
    t("タグ・エンティティ越しの断定も検出",
      any("計算断定" in p for p in audit_machine({**ok, "strategy": "期待<b>収支</b>がプラス"}))
      and any("計算断定" in p for p in audit_machine({**ok, "strategy": "期待&#21454;&#25903;がプラス"})))
    t("設定の非存在断定を検出",
      any("設定段階" in p for p in audit_machine({**ok, "info": "設定3は非搭載"})))
    t("★語間が長い直接断定も検出", any("設定段階" in p for p in audit_machine(
        {**ok, "info": "本機は設定3が実質存在しない仕様です。"})))
    t("設定列挙の欠番も検出",
      any("設定段階" in p for p in audit_machine({**ok, "info": "スマスロ（設定1/2/4/5/6）"})))
    t("正当な設定判別情報を誤検知しない（実データ hanabi）",
      audit_machine({**ok, "info": "1枚役成立5回以上のREGで出現率25%。設定1では出現しない。"}) == [])
    t("噂を否定する文を誤検知しない（実データ neoplanet）",
      audit_machine({**ok, "info": "一部では「設定3が実質存在しない」との声もありますが、"
                                   "メーカー公式では6段階設定と表記されています。"}) == [])
    t("秘密を含みうるURLを検出",
      any("URL" in p for p in audit_machine({**ok, "sources": [{"url": "https://a.example/x?token=S"}]})))
    t("★https以外・userinfo付きURLを拒否",
      any("URL" in p for p in audit_machine({**ok, "sources": [{"url": "http://a.example/x"}]}))
      and any("URL" in p for p in audit_machine({**ok, "sources": [{"url": "https://u:p@a.example/x"}]})))
    t("★authoring専用フィールドの流出を検出",
      any("authoring専用" in p for p in audit_machine({**ok, "lifecycle": "LEGACY_SEARCH"}))
      and any("authoring専用" in p for p in audit_machine({**ok, "checker_modes": {}})))
    t("★許可されていないフィールドを検出（allowlist方式）",
      any("許可されていない" in p for p in audit_machine({**ok, "memo": "内部メモ"})))
    t("★必須フィールド欠落・slug不正・重複を検出",
      any("必須" in p for p in audit_machine({"name": "t"}))
      and any("slug" in p for p in audit_machine({**ok, "slug": "BAD SLUG"}))
      and any("重複" in p for p in audit_machine(ok, {"x"})))
    t("★空の公開データを不合格にする", audit_machine({}) != [])

    # --- 軸契約の独立実装（Codex 12巡目 条件6）---
    def _ax(ck):
        return audit_machine({**ok, "checker": ck,
                              "display_requirements": {"surfaces": ["checker"]}})
    good_ax = {"unit": "G", "modes": [{"key": "suru", "hasSuru": True}],
               "suru": {"suru": [{"count": 0, "good": 600}, {"count": 1, "good": 500}]}}
    t("★軸契約(監査側): 正しい二軸は通す", _ax(good_ax) == [])
    for ck, label in (
        ({**good_ax, "suru": {"good": 4}}, "直下閾値のみ"),
        ({**good_ax, "suru": {**good_ax["suru"], "good": 4}}, "直下閾値と行の併存"),
        ({**good_ax, "unit": "回"}, "入力単位が回"),
        ({**good_ax, "unit": None}, "入力単位の欠落"),
        ({**good_ax, "suru": {"suru": [{"good": 600}]}}, "count欠落"),
        ({**good_ax, "suru": {"suru": [{"count": 1.0, "good": 600}]}}, "countが小数表記"),
        ({**good_ax, "suru": {"suru": [{"count": 1}]}}, "行に判定材料が無い"),
        ({**good_ax, "suru": {"suru": [{"count": 2, "good": 600}, {"count": 1, "good": 500}]}}, "count降順"),
        ({**good_ax, "modes": [{"key": "suru", "hasCycle": True}]}, "宣言と実体の不一致"),
        ({**good_ax, "modes": [{"key": "suru", "hasSuru": True, "hasCycle": True}]}, "両方宣言"),
        ({"unit": "G", "modes": [{"key": "cycle", "hasCycle": True}],
          "cycle": {"note": "周期天井"}}, "noteだけの周期mode"),
    ):
        t(f"★軸契約(監査側): {label} → 違反", _ax(ck) != [])
    t("★必須フィールドが型不正なら違反（name: true）",
      any("型が不正" in p for p in audit_machine({"slug": "x", "name": True})))

    # --- 記事データ（audit_detail）の負例 ---
    d_ok = {"lead": "紹介文です。", "sections": [{"title": "天井・恩恵", "body": ["天井は999Gです"]}]}
    t("記事: 正常データは合格", audit_detail("x", d_ok, has_disclaimer=True) == [])
    t("★記事: 見出しの計算断定を検出",
      any("計算断定" in p for p in audit_detail(
          "x", {"sections": [{"title": "期待収支がプラス", "body": ["本文"]}]}, True)))
    t("★記事: 見出し＋段落の分割断定を検出",
      any("計算断定" in p for p in audit_detail(
          "x", {"sections": [{"title": "期待値が", "body": ["プラス"]}]}, True)))
    t("★記事: 辞書セルの行も結合して検出",
      any("計算断定" in p for p in audit_detail(
          "x", {"sections": [{"title": "安全", "type": "settei",
                              "tables": [{"rows": [[{"text": "期待値が"}, {"text": "プラス"}]]}]}]}, True)))
    t("★記事: 数値があるのに目安ラベルが無ければ違反",
      any("目安ラベル" in p for p in audit_detail("x", d_ok, has_disclaimer=False)))
    t("★記事: 許可されていないフィールドを検出",
      any("許可されていない" in p for p in audit_detail("x", {"memo": "内部"}, True)))
    t("★1文目の断定を、後続文の打ち消しで免罪しない",
      any("設定段階" in p for p in audit_machine(
          {**ok, "info": "設定3は非搭載。設定4がないとの声もありますが公式では6段階です。"})))
    # --- 入れ子の契約（Codex 9巡目 (a)-2）---
    t("★sources のtitle型不正・未知フィールドを検出",
      any("sources[0].title" in p for p in audit_machine(
          {**ok, "sources": [{"url": "https://a.example/x", "title": {}}]}))
      and any("未知フィールド" in p for p in audit_machine(
          {**ok, "sources": [{"url": "https://a.example/x", "memo": "x"}]})))
    t("★seo/original/display_requirements の未知フィールド・型不正を検出",
      any("seo" in p for p in audit_machine({**ok, "seo": {"title": "t", "memo": "x"}}))
      and any("surfaces" in p for p in audit_machine(
          {**ok, "display_requirements": {"surfaces": "checker"}})))
    t("★checker内部の数値型崩れ・未知フィールドを検出",
      any("数値でない" in p for p in audit_machine(
          {**ok, "checker": {"normal": {"good": "580"}},
           "display_requirements": {"surfaces": ["checker"]}}))
      and any("未知フィールド" in p for p in audit_machine(
          {**ok, "checker": {"normal": {"good": 580, "private": 1}},
           "display_requirements": {"surfaces": ["checker"]}})))
    t("★記事の表・セルの未知フィールドを検出",
      any("未知フィールド" in p for p in audit_detail(
          "x", {"sections": [{"title": "安全", "type": "settei",
                              "tables": [{"rows": [["a", "b"]], "memo": "x"}]}]}, True))
      and any("セルに未知フィールド" in p for p in audit_detail(
          "x", {"sections": [{"title": "安全", "type": "settei",
                              "tables": [{"rows": [[{"text": "a", "hidden": 1}]]}]}]}, True)))
    t("★細かく割られた断定を全要素連結で検出（Codex 9巡目 (a)-3）",
      any("計算断定" in p for p in audit_detail(
          "x", {"sections": [{"title": "期", "type": "settei",
                              "tables": [{"label": "待", "rows": [["収支が", "プラス"]]}]}]}, True)))
    t("★語間の長い直接断定・端の欠番+のみ を検出",
      any("設定段階" in p for p in audit_machine(
          {**ok, "info": "設定3はメーカー資料上の仕様として明確に存在しない。"}))
      and any("設定段階" in p for p in audit_machine(
          {**ok, "info": "搭載設定は設定1/2/3/4/5のみ。"})))

    ng = [n for n, c in res if not c]
    print(f"\n{len(res) - len(ng)}/{len(res)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--details-dir", help="射影済み記事データのディレクトリ（必須級）")
    ap.add_argument("--expect-count", type=int)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.file:
        ap.error("--file か --selftest が必要")
    return audit_file(args.file, args.expect_count, args.details_dir)


if __name__ == "__main__":
    sys.exit(main())

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
    r"[^。]{0,10}?(?:非搭載|未搭載|存在しない|搭載していない|ありません|無い|ない|なし|無し)")
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


def _asserts_missing_setting(text: str) -> bool:
    """設定の非存在を主張しているか。挙動の否定・引用の否定は除外する。"""
    if _enum_gap(text):
        return True
    m = SETTING_ABSENT_RE.search(text)
    if not m:
        return False
    if BEHAVIOR_NEG_RE.search(m.group(0)):
        return False                      # 「設定1では出現しない」＝挙動の話
    if REFUTATION_RE.search(text):
        return False                      # 「〜との声もありますが公式では6段階」＝打ち消し
    return True


def _atoms(node, path, out):
    """表示される塊を組み立てる。文字列単体だけでなく label+value・表の行も結合する。"""
    if isinstance(node, str):
        out.append((path, node))
    elif isinstance(node, (int, float)) and not isinstance(node, bool):
        out.append((path, str(node)))     # ★数値も走査対象（limit:999 等の見落とし防止）★
    elif isinstance(node, list):
        # 配列が「行」なら結合した原子も作る
        if node and all(isinstance(x, (str, int, float)) and not isinstance(x, bool) for x in node):
            out.append((path, " / ".join(str(x) for x in node)))
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


def audit_machine(pub: dict, seen_slugs: set | None = None) -> list[str]:
    """公開射影された1機種分を検査し、違反の一覧を返す。"""
    problems: list[str] = []
    if not isinstance(pub, dict) or not pub:
        return ["公開データが空または辞書でない"]
    slug = pub.get("slug", "?")

    # --- A. 公開契約 ---
    for k in REQUIRED_MACHINE_KEYS:
        if not pub.get(k):
            problems.append(f"{slug}: 必須フィールド {k} が無い")
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
    for i, s in enumerate(pub.get("sources") or []):
        if isinstance(s, dict):
            why = _check_url(s.get("url", ""))
            if why:
                problems.append(f"{slug}: {why} sources[{i}]")

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
        variants = (shown, re.sub(r"\s*[/／|｜]\s*", " ", shown))
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

    leaves: list[tuple[str, str]] = []
    _atoms(detail, "detail", leaves)
    has_number = False
    for path, raw in leaves:
        shown = as_displayed(str(raw))
        if path.endswith(".<key>"):
            if re.match(r"^_", shown):
                problems.append(f"{slug}: 内部フィールドの流出 {shown}")
            continue
        variants = (shown, re.sub(r"\s*[/／|｜]\s*", " ", shown))
        if any(FORBIDDEN_RE.search(v) for v in variants):
            problems.append(f"{slug}: 計算断定の残存 {path}")
        if any(_asserts_missing_setting(v) for v in variants):
            problems.append(f"{slug}: 設定段階の非存在断定 {path}")
        if _NUM.search(shown):
            has_number = True
    if has_number and not has_disclaimer:
        problems.append(f"{slug}: 記事に数値があるのに目安ラベルが無い")
    return problems


def audit_file(path: str, expected_count: int | None = None) -> int:
    data = json.load(open(path, encoding="utf-8"))
    machines = data if isinstance(data, list) else data.get("machines", [])
    problems: list[str] = []
    if not machines:
        problems.append("公開データが空（0機種）")
    seen: set = set()
    for m in machines:
        problems.extend(audit_machine(m, seen))
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

    ng = [n for n, c in res if not c]
    print(f"\n{len(res) - len(ng)}/{len(res)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--expect-count", type=int)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.file:
        ap.error("--file か --selftest が必要")
    return audit_file(args.file, args.expect_count)


if __name__ == "__main__":
    sys.exit(main())

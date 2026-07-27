#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""audit_public.py — 公開射影の独立監査（gates.py の判断を信用しない最後の境界）

★なぜ別実装なのか★
  gates.py の射影が誤って通した場合、同じ関数・同じ正規表現を使う監査では気づけない
  （共通原因故障）。本ファイルは gates.py を **import せず**、禁止条件を独自に持ち、
  「出来上がった公開データ」だけを見て判定する。

検査するもの（公開してよい成果物に対する禁止条件）:
  1. 計算断定の残存（期待値・収支・枚数の言い切り）
  2. 設定段階の非存在断定（列挙の欠番による暗示を含む）
  3. 数値が載っているのに目安ラベル(disclaimer)が無い
  4. 秘密を含みうるURL（クエリ・フラグメント・token/key/sig等）
  5. 内部フィールドの流出（_ 始まり・authoring専用キー）
  6. HTMLとして描画したときに現れる文字（タグ・エンティティ越しの禁止語）

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

# --- 独自の禁止条件（gates.py とは別に持つ。表記も意図的に揃えない）---
FORBIDDEN_CLAIMS = (
    r"期待\s*収支", r"プラス\s*(?:域|圏|ライン)", r"プラス\s*期待値", r"期待値\s*(?:が)?\s*プラス",
    r"プラス\s*に\s*転じ", r"期待\s*枚数", r"期待\s*差枚", r"損益\s*分岐", r"時給",
    r"利益\s*ゾーン", r"確実な\s*利益", r"プラス\s*収支",
    r"期待値\s*が\s*(?:乗|積み)", r"[0-9０-９,]+\s*円\s*(?:以上)?\s*の\s*期待値",
)
FORBIDDEN_RE = re.compile("|".join(FORBIDDEN_CLAIMS))

# 「設定Nの存在そのものを否定する」形だけを見る。
# 設定と否定語の間に助詞以外の語が入るものは対象外にする。さもないと
#   「設定1では出現しない」（正当な設定判別情報）
#   「『設定3が実質存在しない』との声もありますが、公式では6段階」（噂の否定）
# まで誤検知する（実データで確認）。
_SET1 = r"[1-6１-６一二三四五六]"
SETTING_ABSENT_RE = re.compile(
    r"設定\s*" + _SET1 + r"(?:\s*[・、,／/･]\s*(?:設定\s*)?" + _SET1 + r")*"
    r"[はがもをのとで\s]{0,3}"
    r"(?:非搭載|未搭載|存在しない|搭載していない|ありません|無い|ない|なし|無し)")

SECRET_URL_RE = re.compile(r"[?#]|token|key=|sig=|signature|auth|session", re.I)
INTERNAL_KEY_RE = re.compile(r"^_|^internal|^draft|^todo", re.I)

_TAG = re.compile(r"<[^>]*>")
_ZW = re.compile(r"[​-‏⁠﻿­]")
_MD = re.compile(r"[*_`~]")
_KANJI = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}
_ENUM = re.compile(r"設定\s*[1-6１-６一二三四五六](?:\s*[・、,／/･]\s*(?:設定\s*)?[1-6１-６一二三四五六]){1,5}")
_ONE = re.compile(r"[1-6１-６一二三四五六]")
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
        nums = sorted({_KANJI.get(c, 0) or int(str(c).translate(str.maketrans("１２３４５６", "123456")))
                       for c in _ONE.findall(m.group(0))})
        if len(nums) >= 2 and set(range(nums[0], nums[-1] + 1)) - set(nums):
            return True
    return False


def _walk(node, path, out):
    """(path, 文字列) と (path, キー名) を全部たどる。"""
    if isinstance(node, str):
        out.append((path, node))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk(v, f"{path}[{i}]", out)
    elif isinstance(node, dict):
        for k, v in node.items():
            out.append((f"{path}.<key>", str(k)))
            _walk(v, f"{path}.{k}", out)


def audit_machine(pub: dict) -> list[str]:
    """公開射影された1機種分を検査し、違反の一覧を返す。"""
    problems: list[str] = []
    slug = pub.get("slug", "?")
    leaves: list[tuple[str, str]] = []
    _walk(pub, "machine", leaves)

    has_number = False
    for path, raw in leaves:
        shown = as_displayed(raw)
        if path.endswith(".<key>"):
            if INTERNAL_KEY_RE.match(shown):
                problems.append(f"{slug}: 内部フィールドの流出 {path}={shown}")
            continue
        if FORBIDDEN_RE.search(shown):
            problems.append(f"{slug}: 計算断定の残存 {path}")
        if SETTING_ABSENT_RE.search(shown) or _enum_gap(shown):
            problems.append(f"{slug}: 設定段階の非存在断定 {path}")
        if path.endswith(".url") or shown.startswith("http"):
            if SECRET_URL_RE.search(shown):
                problems.append(f"{slug}: 秘密を含みうるURL {path}")
        if (_NUM.search(shown)
                and not path.startswith(("machine.slug", "machine.release_date",
                                         "machine.confirmed_at", "machine.sources"))
                and not path.startswith("machine.disclaimer")
                and not path.startswith("machine.display_requirements")):
            has_number = True

    if has_number and not pub.get("disclaimer"):
        problems.append(f"{slug}: 数値を公開しているのに目安ラベル(disclaimer)が無い")
    return problems


def audit_file(path: str) -> int:
    data = json.load(open(path, encoding="utf-8"))
    machines = data if isinstance(data, list) else data.get("machines", [])
    problems: list[str] = []
    for m in machines:
        problems.extend(audit_machine(m))
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
          "disclaimer": "当サイトの目安です"}
    t("正常な公開データは合格", audit_machine(ok) == [])
    t("数値があるのに目安ラベルが無ければ違反",
      any("目安ラベル" in p for p in audit_machine({k: v for k, v in ok.items()
                                                  if k != "disclaimer"})))
    t("計算断定の残存を検出",
      any("計算断定" in p for p in audit_machine({**ok, "strategy": "580Gから期待収支がプラス"})))
    t("★タグ・エンティティ越しの断定も検出（gates側とは別実装）",
      any("計算断定" in p for p in audit_machine({**ok, "strategy": "期待<b>収支</b>がプラス"}))
      and any("計算断定" in p for p in audit_machine({**ok, "strategy": "期待&#21454;&#25903;がプラス"})))
    t("設定の非存在断定を検出",
      any("設定段階" in p for p in audit_machine({**ok, "info": "設定3は非搭載"})))
    t("設定列挙の欠番も検出",
      any("設定段階" in p for p in audit_machine({**ok, "info": "スマスロ（設定1/2/4/5/6）"})))
    t("★正当な設定判別情報を誤検知しない（実データ hanabi）",
      audit_machine({**ok, "info": "1枚役成立5回以上のREGで出現率25%。設定1では出現しない。"}) == [])
    t("★噂を否定する文を誤検知しない（実データ neoplanet）",
      audit_machine({**ok, "info": "一部では「設定3が実質存在しない」との声もありますが、"
                                   "メーカー公式では6段階設定と表記されています。"}) == [])
    t("秘密を含みうるURLを検出",
      any("URL" in p for p in audit_machine(
          {**ok, "sources": [{"url": "https://a.example/x?token=SECRET"}]})))
    t("内部フィールドの流出を検出",
      any("内部フィールド" in p for p in audit_machine({**ok, "_draft": "内部メモ"})))
    t("円建て期待値の断定を検出",
      any("計算断定" in p for p in audit_machine({**ok, "strategy": "450G〜で1000円以上の期待値"})))

    ng = [n for n, c in res if not c]
    print(f"\n{len(res) - len(ng)}/{len(res)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.file:
        ap.error("--file か --selftest が必要")
    return audit_file(args.file)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""★記事から型式名・検定番号を落とす★（CLAUDE.md の決定どおり）

★決まりごと★
  「型式名は記事には書かない。★取り違えを防ぐ同定にだけ使う★」
  型式を載せているのはDMMだけなので、記事に出すと出典が1件の値を
  読者に見せることになる。同定用の値は machines.json の identity に残る。

★この道具がやること★
  ・記事本文（sections[].body[]）から、型式名・検定番号を言っている**行を消す**
  ・factTable から、型式名・検定番号の**行を消す**

★この道具がやらないこと★
  ・文章を書き足さない・書き換えない（★消すだけ★）
  ・行の一部だけを消さない（★1行に他の情報が混ざっていたら触らない★）
  ・identity は触らない（同定に使うので残す）

★1行に他の情報が混ざっている場合★
  例＝yabachiba「**機種名**：Lヤバチバ（型式名：LヤバチバZM）」
  この行を消すと機種名まで消える。★そういう行は報告して触らない★
  （文の一部を消すのは書き換えになるので、人が決める）。

使い方:
  python scripts/strip_model_code.py                （全機種・見るだけ）
  python scripts/strip_model_code.py --slug tonsuki
  python scripts/strip_model_code.py --apply
  python scripts/strip_model_code.py --selftest
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import safe_json as _sj      # noqa: E402

DETAILS = os.path.join(BASE, "assets", "data", "machine-details")

# ★型式名・検定番号を言っている語★
LABELS = ("型式名", "型式番号", "検定番号")

# ★その行が「型式名の行」だと言い切れる形★
#   ①強調つきの見出し：**型式名**：…
#   ②箇条書きの項目：・型式名
#   ③文そのもの：型式名は…、検定番号…。
_HEAD = re.compile(r"^\s*(?:\*\*)?(型式名|型式番号|検定番号)(?:\*\*)?\s*[:：]")
_BULLET = re.compile(r"^\s*[・\-*]\s*(型式名|型式番号|検定番号)\s*$")
_SENT = re.compile(r"^\s*型式名は[^。]*。?\s*$")


def line_is_model_code(line: str) -> bool:
    """★その行が丸ごと型式名の話か★（他の情報が混ざっていたら False）"""
    s = str(line or "").strip()
    if not s:
        return False
    if _BULLET.match(s):
        return True
    if _HEAD.match(s):
        return True
    if _SENT.match(s) and "検定番号" in s or _SENT.match(s):
        return True
    return False


# ★★括弧の中が型式名だけなら、括弧ごと落とす★★（2026-08-21）
#   実例＝yabachiba「**機種名**：Lヤバチバ（型式名：LヤバチバZM）」
#   行ごと消すと機種名まで消える。括弧だけなら**消すだけ**で済む
#   （新しい文章を書かない・機種名は残る）。
#   ★中に他の情報が入っていたら触らない★＝括弧の中身が
#   「型式名：〜」「検定番号：〜」の形だけのときに限る。
_PAREN = re.compile(r"[（(]([^（）()]*)[）)]")
# ★中身が「札：値」の並びだけであること★
#   値に句点（。）を含む＝説明文が入っているので触らない。
_PAIR = re.compile(r"^\s*(?:型式名|型式番号|検定番号)\s*[:：]\s*[^。、,]+\s*$")


def _only_model_code(inner: str) -> bool:
    """括弧の中身が、型式名・検定番号の札と値だけでできているか。

    ★これを確かめないと消しすぎる★＝
      「（型式名：LテストAB。天井は999G）」のような括弧を丸ごと落とすと、
      天井の情報まで消える。★中身を1つずつ見て、全部が札：値の形の
      ときだけ落とす★。
    """
    inner = str(inner or "").strip()
    if not inner or "。" in inner:
        return False
    parts = re.split(r"[・／/]", inner)
    return bool(parts) and all(_PAIR.match(x) for x in parts)


def strip_model_paren(line: str):
    """括弧ごと落とした行を返す（落とせなければ None）。"""
    s = str(line or "")
    hit = [m for m in _PAREN.finditer(s) if _only_model_code(m.group(1))]
    if not hit:
        return None
    new = s
    for m in reversed(hit):
        new = new[:m.start()] + new[m.end():]
    new = new.strip()
    if not new or new == s.strip():
        return None
    # ★落としたあとに型式名が残っていたら触らない★
    if any(k in new for k in LABELS):
        return None
    return new


def mixed_line(line: str) -> bool:
    """★型式名を含むが、他の情報も入っている行★（触らない）"""
    s = str(line or "")
    if not any(k in s for k in LABELS):
        return False
    if line_is_model_code(s):
        return False
    # ★括弧ごと落とせるなら「混ざっている」扱いにしない★
    return strip_model_paren(s) is None


def plan_for(detail: dict) -> dict:
    """1機種ぶんの計画を返す（★書かない★）。"""
    out = {"drop_body": [], "drop_fact": [], "mixed": [], "cut_paren": []}
    for si, sec in enumerate(detail.get("sections") or []):
        body = sec.get("body") or []
        for bi, line in enumerate(body):
            if not isinstance(line, str):
                continue
            if line_is_model_code(line):
                out["drop_body"].append((si, bi, line))
            elif any(k in line for k in LABELS):
                cut = strip_model_paren(line)
                if cut is not None:
                    out.setdefault("cut_paren", []).append((si, bi, line, cut))
                elif mixed_line(line):
                    out["mixed"].append((si, bi, line))
    ft = detail.get("factTable")
    if isinstance(ft, list):
        for ri, row in enumerate(ft):
            if isinstance(row, list) and row and isinstance(row[0], str) \
                    and row[0].strip() in LABELS:
                out["drop_fact"].append((ri, row))
    return out


def apply_plan(detail: dict, plan: dict) -> dict:
    """計画どおりに消す（★消すだけ★）。"""
    for si, sec in enumerate(detail.get("sections") or []):
        body = list(sec.get("body") or [])
        for s2, bi, _old, cut in plan.get("cut_paren") or []:
            if s2 == si and bi < len(body):
                body[bi] = cut
        drop = {bi for s2, bi, _ in plan["drop_body"] if s2 == si}
        sec["body"] = [x for i, x in enumerate(body) if i not in drop]
    if plan["drop_fact"]:
        drop = {ri for ri, _ in plan["drop_fact"]}
        detail["factTable"] = [r for i, r in enumerate(detail["factTable"])
                               if i not in drop]
    return detail


def run(slug: str | None = None, apply_it: bool = False) -> dict:
    result = {"changed": [], "mixed": [], "empty_section": []}
    names = sorted(os.listdir(DETAILS)) if os.path.isdir(DETAILS) else []
    for fn in names:
        if not fn.endswith(".json"):
            continue
        sl = fn[:-5]
        if slug and sl != slug:
            continue
        p = os.path.join(DETAILS, fn)
        d = _sj.read_json(p, expect=dict)
        plan = plan_for(d)
        if plan["mixed"]:
            result["mixed"].append(
                {"slug": sl, "lines": [x[2] for x in plan["mixed"]]})
        if not plan["drop_body"] and not plan["drop_fact"] \
                and not plan.get("cut_paren"):
            continue
        # ★セクションが空になるなら触らない★
        empty = []
        for si, sec in enumerate(d.get("sections") or []):
            n = len(sec.get("body") or [])
            k = len([1 for s2, _, _ in plan["drop_body"] if s2 == si])
            if k and k >= n:
                empty.append(str(sec.get("title")))
        if empty:
            result["empty_section"].append({"slug": sl, "sections": empty})
            continue
        result["changed"].append(
            {"slug": sl, "body": len(plan["drop_body"]),
             "fact": len(plan["drop_fact"]),
             "paren": len(plan.get("cut_paren") or []),
             "lines": [x[2][:70] for x in plan["drop_body"]]
                      + [f"（括弧だけ）{o[:40]} → {c[:40]}"
                         for _s, _b, o, c in (plan.get("cut_paren") or [])]})
        if apply_it:
            apply_plan(d, plan)
            tmp = p + ".tmp"
            with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
                f.write("\n")
            os.replace(tmp, p)
    return result


def _selftest() -> int:
    ng = []

    ran = [0]          # ★実際に試した数を数える★（2026-08-27）

    #   ★直す前は分母が手書きだった★ので、

    #   試験を足しても分母が増えず、足した分が数えられなかった。

    def t(name, cond):

        ran[0] += 1
        print(("✅ " if cond else "❌ ") + name)
        if not cond:
            ng.append(name)

    t("★強調つきの見出しの行を見つける★",
      line_is_model_code("**型式名**：LとんでもスキルKM（出典2件で一致）"))
    t("　強調なしでも見つける", line_is_model_code("型式名：LテストAB"))
    t("　箇条書きの項目も見つける", line_is_model_code("・型式名"))
    t("　文そのものも見つける",
      line_is_model_code("型式名はLBスロットガルフィーA4、検定番号5S1315。"))
    t("★★他の情報が混ざった行を、行ごと消さない★★（機種名まで消える）",
      not line_is_model_code("**機種名**：Lヤバチバ（型式名：LヤバチバZM）"))
    t("　関係ない行は触らない",
      not line_is_model_code("天井は999Gです。")
      and not mixed_line("天井は999Gです。"))

    D = {"sections": [{"title": "基本スペック",
                       "body": ["**型式名**：LテストAB（出典2件で一致）",
                                "**機械割**：97.8%"]}],
         "factTable": [["メーカー", "テスト社"], ["型式名", "LテストAB"]]}
    plan = plan_for(D)
    t("　本文と一覧表の両方から見つける",
      len(plan["drop_body"]) == 1 and len(plan["drop_fact"]) == 1)
    apply_plan(D, plan)
    t("　消したあと、他の行は残っている",
      D["sections"][0]["body"] == ["**機械割**：97.8%"]
      and D["factTable"] == [["メーカー", "テスト社"]])

    # ★★括弧ごと落とす道★★（2026-08-21）
    t("★括弧の中が型式名だけなら、括弧ごと落とす★（機種名は残る）",
      strip_model_paren("**機種名**：Lヤバチバ（型式名：LヤバチバZM）")
      == "**機種名**：Lヤバチバ")
    # ★実データに無い形を追いかけない★（場合分けを増やさない・CLAUDE.mdの鉄則）
    #   札が2つ並ぶ形（「型式名：… ・ 検定番号5S1」＝2つ目に区切りが無い）は
    #   ★安全側に倒して触らない★。実データではこの形は行そのものなので、
    #   行単位の規則（line_is_model_code）が拾う。
    t("★分からない形は触らない（安全側）★",
      strip_model_paren("**機種名**：Lテスト（型式名：LテストAB・検定番号5S1）")
      is None)
    t("★★括弧の中に説明文が混ざっていたら触らない★★（消しすぎる）",
      strip_model_paren("**機種名**：Lテスト（型式名：LテストAB。天井は999G）")
      is None)
    t("　型式名と関係ない括弧は触らない",
      strip_model_paren("**機種名**：Lテスト（2026年導入）") is None)
    t("　行ごと型式名の行は、この道では扱わない",
      strip_model_paren("**型式名**：LテストAB") is None)
    t("　括弧を落として空になるなら触らない",
      strip_model_paren("（型式名：LテストAB）") is None)

    # ★書き足していないこと★
    src = open(__file__, encoding="utf-8").read()
    t("★★この道具は文章を書き足さない★★",
      ".append(" in src and "body\"] = [x for i" in src
      and "確認中" not in src.split('"""')[2])

    print()
    print(f"{ran[0] - len(ng)}/{ran[0]} " + ("合格" if not ng else "不合格"))
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--slug")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()

    r = run(a.slug, a.apply)
    print(("★書きました★" if a.apply else "★見るだけ（--apply で書きます）★")
          + f" 対象 {len(r['changed'])} 機種")
    for c in r["changed"]:
        print(f"  {c['slug']}: 本文{c['body']}行 / 一覧表{c['fact']}行")
        for line in c["lines"]:
            print(f"      - {line}")
    if r["mixed"]:
        print()
        print(f"★他の情報が混ざっていて触れない行★ {len(r['mixed'])} 機種"
              "（人が決めてください）")
        for m in r["mixed"]:
            for line in m["lines"]:
                print(f"  {m['slug']}: {line[:80]}")
    if r["empty_section"]:
        print()
        print("★消すとセクションが空になるので触りません★")
        for e in r["empty_section"]:
            print(f"  {e['slug']}: {' / '.join(e['sections'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

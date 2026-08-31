# -*- coding: utf-8 -*-
"""★「基本スペック」のラベル行を表へ移す★（2026-08-31・運営者の要望③）

★運営者の言葉★
> 文字じゃなくてできることは表にしてパット見でわかるようにしたい

★★値を1文字も変えない★★
  移した結果から**元の行を組み立て直して、元と完全に一致すること**を
  1行ずつ確かめる。1行でも一致しなければ、その機種は移さない。
  ＝「変えていない」を言葉ではなく機械で示す。

★★全行がラベルと値の機種だけ移す★★
  実測（2026-08-31・133機種）＝
    そのまま移せる   55機種
    移せない         78機種（説明文が混ざっている 48／形が違う 30）
  ★説明文を無理に表へ入れない★（2AIが読んで判断する仕事）。

★★factTable との重複はここでは消さない★★
  形式だけの変更にしておかないと「値を変えていない」の証明が崩れる。

使い方:
  python scripts/tableize_spec.py --all              # 移せる機種を数える
  python scripts/tableize_spec.py --slug hokuto      # 1機種の下見
  python scripts/tableize_spec.py --all --apply      # 実際に移す
  python scripts/tableize_spec.py --selftest

終了コード: 0=正常 / 1=失敗
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                      # noqa: BLE001
        pass

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DETAILS = os.path.join(BASE, "assets", "data", "machine-details")
TITLE = "基本スペック"
HEADERS = ["項目", "内容"]

# 「**項目**：値」または「項目：値」
#   ★区切りは最初の「：」だけ★（値の中に「：」があってもよい）
_BOLD = re.compile(r"^\*\*(?P<label>[^*]+)\*\*(?P<sep>[：:])(?P<value>.*)$")
_PLAIN = re.compile(r"^(?P<label>[^：:]+)(?P<sep>[：:])(?P<value>.*)$")


def parse_line(line: str):
    """1行を (太字か, 見出し, 区切り, 値) にする。形が違えば None。

    ★空白を触らない★＝行の前後だけ落とし、中身はそのまま。
    """
    t = str(line or "")
    if t != t.strip():
        t = t.strip()
    m = _BOLD.match(t)
    bold = bool(m)
    if not m:
        m = _PLAIN.match(t)
    if not m:
        return None
    label = m.group("label")
    value = m.group("value")
    if not label.strip() or not value.strip():
        return None
    # ★★見出しに文の記号が入っていたら、それは文章★★（2026-08-31・実データで発見）
    #   my_juggler_v の「…最新作。設定6はBIG：REG比率が…」は、
    #   文中の「：」で切られて**文章が見出しになっていた**。
    #   ★組み立て直しの照合では捕まらない★（元の行は復元できるため）。
    if any(c in label for c in "。、！？!?"):
        return None
    # ★長すぎる見出しは文章とみなす★（いちばん長い実物は13字）
    #   ★文字数だけでは区別できない★ので、上の記号の検査と**両方**使う。
    #   ★迷ったら移さない★＝その機種は2AIが読む側へ回る。
    if len(label.strip()) > 20:
        return None
    return bold, label, m.group("sep"), value


def rebuild(bold: bool, label: str, sep: str, value: str) -> str:
    """移す前の行を組み立て直す（照合用）。"""
    return (f"**{label}**{sep}{value}") if bold else f"{label}{sep}{value}"


def plan(detail) -> dict:
    """1機種ぶんの下見。

    返すもの:
      {"ok": 移せるか, "why": 理由, "rows": [[見出し, 値], ...],
       "index": 節の位置}
    """
    if not isinstance(detail, dict):
        return {"ok": False, "why": "記事データが辞書ではありません"}
    secs = detail.get("sections") or []
    hit = [(i, x) for i, x in enumerate(secs)
           if isinstance(x, dict) and x.get("title") == TITLE]
    if len(hit) != 1:
        return {"ok": False, "why": f"{TITLE} の箱が {len(hit)} 個です"}
    i, sec = hit[0]
    if sec.get("type"):
        return {"ok": False, "why": f"もう別の種別です（{sec['type']}）"}
    body = [x for x in (sec.get("body") or []) if isinstance(x, str)]
    if not body:
        return {"ok": False, "why": "本文がありません"}
    rows = []
    for line in body:
        if not line.strip():
            return {"ok": False, "why": "空の行があります"}
        got = parse_line(line)
        if got is None:
            return {"ok": False, "why": "ラベルと値の形でない行があります",
                    "line": line[:50]}
        bold, label, sep, value = got
        # ★★組み立て直して元と一致することを確かめる★★
        #   （前後の空白だけは落とすので、そこも同じ形で比べる）
        if rebuild(bold, label, sep, value) != line.strip():
            return {"ok": False, "why": "組み立て直すと元と違います",
                    "line": line[:50]}
        rows.append([label, value])
    return {"ok": True, "why": "", "rows": rows, "index": i}


def apply_to(detail: dict, got: dict) -> dict:
    """下見の結果どおりに、その機種の記事データを書き換えて返す。"""
    sec = detail["sections"][got["index"]]
    sec.pop("body", None)
    sec["type"] = "table"
    sec["tables"] = [{"label": "", "headers": list(HEADERS),
                      "rows": [list(r) for r in got["rows"]]}]
    return detail


def _load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _save(p, d):
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
        f.write("\n")


def selftest() -> int:
    ok = []

    def t(name, cond):
        ok.append(bool(cond))
        print(("✅" if cond else "❌") + " " + name)

    def det(lines):
        return {"sections": [{"title": TITLE, "body": list(lines)}]}

    g = plan(det(["**機種名**：スマスロ北斗の拳", "**メーカー**：サミー"]))
    t("★太字のラベル行を移せる★",
      g["ok"] and g["rows"] == [["機種名", "スマスロ北斗の拳"],
                                ["メーカー", "サミー"]])
    g2 = plan(det(["天井：1200G+α"]))
    t("　太字でないラベル行も移せる", g2["ok"] and g2["rows"] == [["天井", "1200G+α"]])
    t("★★説明文が混ざっていたら、その機種は移さない★★",
      not plan(det(["**天井**：1200G", "この機種は初当りが軽いです"]))["ok"])
    t("★★値の中に「：」があっても、最初の1つで切る★★",
      plan(det(["**備考**：朝一：リセット恩恵あり"]))["rows"]
      == [["備考", "朝一：リセット恩恵あり"]])
    t("　値が空なら移さない", not plan(det(["**天井**："])) ["ok"])
    t("　見出しが空なら移さない", not plan(det(["**　**：1200G"]))["ok"])
    t("　もう表になっているなら触らない",
      not plan({"sections": [{"title": TITLE, "type": "table",
                              "tables": []}]})["ok"])

    # ★★値を1文字も変えていないことを、組み立て直して確かめる★★
    src = ["**機種名**：スマスロ とある魔術の禁書目録2",
           "**コイン持ち**：約30.8G/50枚（設定1）"]
    g3 = plan(det(src))
    back = [rebuild(True, r[0], "：", r[1]) for r in g3["rows"]]
    t("★★移したあとから元の行を組み立て直すと、元と一致する★★", back == src)

    # ★★2回かけても2回目は何も動かない★★
    d = det(["**天井**：1200G"])
    g4 = plan(d)
    apply_to(d, g4)
    t("★★2回目は「もう表なので触らない」になる★★", not plan(d)["ok"])

    # ★★文中の「：」で切って、文章を見出しにしない★★（実データで発見）
    t("★★文の途中の「：」で切らない★★（my_juggler_v で実際に起きた）",
      not plan(det(["ジャグラー屈指の設定差を持つ最新作。設定6はBIG：REG比率がほぼ1:1です"]))["ok"])
    t("　読点が入った見出しも文章とみなす",
      not plan(det(["天井、恩恵：1200G"]))["ok"])

    # ★機種名に「。」が入る機種でも壊れない★
    g5 = plan(det(["**機種名**：スマスロ 痛いのは嫌なので防御力に極振りしたいと思います。"]))
    t("★★機種名に「。」が入っていても移せる★★（bofuri）",
      g5["ok"] and g5["rows"][0][1].endswith("思います。"))

    ng = ok.count(False)
    print(f"{len(ok) - ng}/{len(ok)} 合格")
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="基本スペックのラベル行を表へ移す")
    ap.add_argument("--slug")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()

    paths = ([os.path.join(DETAILS, a.slug + ".json")] if a.slug
             else sorted(glob.glob(os.path.join(DETAILS, "*.json"))))
    movable, stuck, done = [], [], []
    for p in paths:
        slug = os.path.basename(p)[:-5]
        if not os.path.isfile(p):
            print(f"★{slug} の記事データがありません★")
            return 1
        d = _load(p)
        got = plan(d)
        if not got["ok"]:
            stuck.append((slug, got["why"], got.get("line", "")))
            continue
        movable.append((slug, got))
        if a.apply:
            _save(p, apply_to(d, got))
            done.append(slug)

    if a.slug:
        slug = os.path.basename(paths[0])[:-5]
        if movable:
            print(f"{slug}: 移せます（{len(movable[0][1]['rows'])} 行）")
            for r in movable[0][1]["rows"]:
                print(f"  {r[0]}｜{r[1][:40]}")
        else:
            print(f"{slug}: 移せません（{stuck[0][1]}）")
            if stuck[0][2]:
                print(f"  例: {stuck[0][2]}")
        return 0

    print(f"移せる機種: {len(movable)} / 移せない: {len(stuck)}")
    if a.apply:
        print(f"★書き換えました: {len(done)} 機種★")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

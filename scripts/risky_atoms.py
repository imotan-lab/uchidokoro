#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""risky_atoms.py — 公開記事に残っている「絶対に載せられない表現」を探して消す。

★何のための道具か（2026-08-05）★
  Phase 0 で「行動をすすめる表現・期待値の断定」を落とす方針にしたのに、
  既存記事には残っている。公開ゲート（gates.py）は**公開の可否**を決めるだけで、
  記事そのものは直さないので、誰かが直さないと永久に残る。
  この道具は **gates.py と同じ判定** を使って場所を特定し、**同じ単位で消す**。

★なぜ「文字を消す」ではなく「原子ごと消す」のか（Codex100回目の指摘2）★
  禁止語だけを消すと、否定・条件・例外が外れて**意味が反転する**ことがある。
  例:「ゲーム数狙いではプラス期待値が出ません」から「プラス期待値」を消すと
  注意喚起が壊れる。だから消す単位は**表示原子（段落・箇条書きの1項目）**とし、
  部分文字列は絶対に消さない。

★自動で消してよいもの（canary。これ以外は全部「人送り」）★
  - `sections[i].body[j]` の段落1つ
  - 消しても **その節の本文が空にならない**
  - その段落に**注意喚起の否定形が含まれない**（gatesの否定契約と同じ考え方）
  - 表・summaryBox・lead・strategy・checker は**触らない**（人送り）

使い方:
    python scripts/risky_atoms.py                    # 全機種の一覧（消さない）
    python scripts/risky_atoms.py --slug hokuto      # 1機種だけ見る
    python scripts/risky_atoms.py --slug hokuto --apply   # ★実際に消す★
    python scripts/risky_atoms.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_ledger as _bl              # noqa: E402  射影の作り方を借りる
import gates                            # noqa: E402
import safe_json as _sj                 # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "assets", "data")
DETAILS = os.path.join(DATA, "machine-details")

# 自動で消してよい場所（★ここを広げるときは必ずレビューにかける★）
AUTO_PATH = re.compile(r"^sections\[(\d+)\]\.body\[(\d+)\]$")

# 人送りにする理由
WHY_TABLE = "表・箱・見出しは自動で消さない"
WHY_LAST = "消すとその節の本文が空になる"
WHY_WARN = "注意喚起（否定形）を含む"
WHY_MIX = "1つの段落に別の情報が混ざっている"
WHY_SETTING = "設定段階の書き方の問題＝消すのではなく言い換えが要る"


def drop_kind(text: str) -> str:
    """なぜ止まったのかを分ける（消してよいものと、言い換えが要るものは別）。

    ★2026-08-05・実データで気づいた★
      「設定段階：4段階（設定1・2・5・6）」は**事実の記載**であって、
      危ない表現ではない。止まる理由は「非存在を断定している」という
      **書き方**なので、消すと読者が知るべき情報だけが減る。
      消してよいのは「行動をすすめる／儲かると断定する」表現だけ。
    """
    # ★gatesと同じ判定を使う★（列挙の欠番から読み取る形も含める）
    if gates.SETTING_DENY_PAT.search(text) or gates._implies_missing_setting(text):
        return "setting"
    if gates.ABSOLUTE_DENY_PAT.search(text):
        return "profit"
    return "other"


class _Drop(_bl._Collector):
    """DROP と判定された原子を、場所つきで集める。"""

    def atom(self, parts, path):
        verdict = gates.classify_atom(
            parts, self.ledger, self.profile, slug=self.slug)
        if verdict == gates.DROP:
            text = gates.normalize_atom(
                parts if isinstance(parts, (list, tuple)) else [parts])
            self.items.append({"slug": self.slug, "path": path, "text": text})
        return gates._Ctx.atom(self, parts, path)


def _ledger() -> dict:
    p = os.path.join(DATA, "ledger.json")
    return _sj.read_json(p, expect=dict) if os.path.isfile(p) else {}


def _machines() -> list:
    ms = _sj.read_json(os.path.join(DATA, "machines.json"), expect=(dict, list))
    return ms["machines"] if isinstance(ms, dict) else ms


def _detail_path(slug: str) -> str:
    return os.path.join(DETAILS, f"{slug}.json")


def collect(machine: dict, detail: dict, ledger: dict) -> list:
    """1機種のDROP原子を、gates と同じ射影で集める。"""
    sim = _bl.provisional(machine)
    g = gates.compute_gates(sim)
    if not g["public"]:
        return []
    ctx = _Drop(g["profile"], ledger, machine["slug"])
    gates._project_machine(sim, g, ctx)
    gates._project_detail(detail, g, ctx)
    return ctx.items


# 注意喚起の否定形（gates の `_NEGATION_AFTER` と同じ考え方を段落全体に広げたもの）
_WARN = re.compile(
    r"(?:出ません|出ない|ありません|入りません|入らない|なりません|見込めません|"
    r"狙えません|期待できません|わけではありません|とは限りません|注意|避け|"
    r"厳禁|危険|やめ(?:て|ましょう)|控え)")


def judge(item: dict, detail: dict, ledger: dict | None = None,
          profile: str | None = None) -> dict:
    """その原子を自動で消してよいか決める（★迷ったら人送り★）。

    ★集めた時と同じ条件で判定する★（2026-08-05・Codex101回目の実バグ）
      以前は段落内の文を `classify_atom(s, {}, None)` で見ていた。
      台帳も profile も slug も渡していないので、**集めた時と別の判定**になり、
      「危ない文はこれだけ」という数え方が信用できなかった。
    """
    out = {**item, "auto": False, "why": ""}
    m = AUTO_PATH.match(item["path"])
    if not m:
        out["why"] = WHY_TABLE
        return out
    si, bi = int(m.group(1)), int(m.group(2))
    secs = detail.get("sections")
    if not isinstance(secs, list) or si >= len(secs):
        out["why"] = "場所が見つかりません"
        return out
    body = (secs[si] or {}).get("body")
    if not isinstance(body, list) or bi >= len(body):
        out["why"] = "場所が見つかりません"
        return out
    if len([x for x in body if str(x).strip()]) <= 1:
        out["why"] = WHY_LAST
        return out
    para = str(body[bi])
    if drop_kind(gates.normalize_atom([para])) == "setting":  # noqa: E501
        out["why"] = WHY_SETTING          # ★消さずに言い換える★
        return out
    if _WARN.search(para):
        out["why"] = WHY_WARN                 # ★警告を消さない★
        return out
    # ★1段落に複数の文があり、危ない文以外にも中身があるなら人送り★
    #   （消すと読者が知るべき事実まで一緒に消える）
    sentences = [s for s in re.split(r"(?<=。)", para) if s.strip()]
    risky = [s for s in sentences
             if gates.classify_atom([s], ledger or {}, profile,
                                    slug=item.get("slug")) == gates.DROP]
    if len(sentences) > 1 and len(risky) < len(sentences):
        out["why"] = WHY_MIX
        return out
    out["auto"] = True
    return out


def plan(slug: str | None = None) -> list:
    """消す候補の一覧（★書き込まない★）。"""
    ledger, rows = _ledger(), []
    for m in _machines():
        if slug and m.get("slug") != slug:
            continue
        p = _detail_path(m["slug"])
        detail = _sj.read_json(p, expect=dict) if os.path.isfile(p) else {}
        g = gates.compute_gates(_bl.provisional(m))
        for it in collect(m, detail, ledger):
            rows.append(judge(it, detail, ledger, g.get("profile")))
    return rows


def apply_slug(slug: str, rows: list, limit: int) -> dict:
    """1機種ぶんの自動削除を実行する（★全か無か★）。"""
    p = _detail_path(slug)
    before = _sj.read_json(p, expect=dict)
    detail = json.loads(json.dumps(before))     # 深い写し
    todo = [r for r in rows if r["auto"] and r["slug"] == slug]
    if len(todo) > limit:
        return {"wrote": False, "removed": 0,
                "problems": [f"消す数が上限を超えています（{len(todo)} > {limit}）"]}
    if not todo:
        return {"wrote": False, "removed": 0, "problems": []}
    # ★後ろから消す★（前から消すと添字がずれる）
    marks = sorted(
        ((int(AUTO_PATH.match(r["path"]).group(1)),
          int(AUTO_PATH.match(r["path"]).group(2))) for r in todo),
        reverse=True)
    for si, bi in marks:
        del detail["sections"][si]["body"][bi]
    # ★消したあとの形を確かめる★（空の節・空の本文を作らない）
    problems = []
    for i, s in enumerate(detail.get("sections") or []):
        b = s.get("body")
        if isinstance(b, list) and not [x for x in b if str(x).strip()]:
            problems.append(f"本文が空になった節があります: {s.get('title')!r}")
    # ★消した以外の場所が変わっていないこと★
    if _shape(before, marks) != _shape(detail):
        problems.append("消した場所以外にも差分があります")
    if problems:
        return {"wrote": False, "removed": 0, "problems": problems}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(detail, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return {"wrote": True, "removed": len(todo), "problems": []}


def _shape(d: dict, drop: list | None = None) -> str:
    """節の題と本文の中身を並べた形（消す予定の段落は除いて比べる）。"""
    drop = set(drop or [])
    out = []
    for i, s in enumerate(d.get("sections") or []):
        b = s.get("body")
        items = []
        if isinstance(b, list):
            items = [str(x) for j, x in enumerate(b) if (i, j) not in drop]
        out.append((str(s.get("title")), str(s.get("type") or ""), tuple(items)))
    return json.dumps([[a, b, list(c)] for a, b, c in out], ensure_ascii=False)


# ------------------------------------------------------------------ selftest

def selftest() -> int:
    ok, ran = True, [0]

    def t(name, cond):
        nonlocal ok
        ran[0] += 1
        print(("✅ " if cond else "❌ ") + name)
        ok = ok and bool(cond)

    D = {"sections": [
        {"title": "当サイトの狙い目", "body": [
            "170Gから期待値プラスに入ります。",
            "通常時は800Gで天井に到達します。"]},
        {"title": "立ち回りのコツ", "body": [
            "ゲーム数狙いではプラス期待値が出ません。"]},
        {"title": "ヤメ時の判断", "body": [
            "800Gで天井に到達します。プラス域に入るのは500Gからです。",
            "通常時のヤメ時は32G+αです。"]},
    ]}

    def j(path):
        return judge({"slug": "x", "path": path, "text": ""}, D)

    t("★★消せるのは段落だけ（表・箱は人送り）★★",
      not j("factTable[0].value")["auto"]
      and j("factTable[0].value")["why"] == WHY_TABLE)
    t("★★消すとその節が空になるなら人送り★★",
      not j("sections[1].body[0]")["auto"]
      and j("sections[1].body[0]")["why"] == WHY_LAST)
    t("　危ない段落が単独なら自動で消せる", j("sections[0].body[0]")["auto"])
    t("★★別の情報が混ざった段落は人送り★★（消すと事実まで消える）",
      not j("sections[2].body[0]")["auto"]
      and j("sections[2].body[0]")["why"] == WHY_MIX)
    # 注意喚起（否定形）は消さない
    D2 = {"sections": [{"title": "t", "body": [
        "この機種は天井が無いため期待値プラスにはなりません。", "他の文。"]}]}
    t("★★注意喚起（否定形）は消さない★★",
      not judge({"slug": "x", "path": "sections[0].body[0]", "text": ""},
                D2)["auto"])
    # 実際に消す
    import copy, tempfile
    tmp = tempfile.mkdtemp()
    global DETAILS
    real, DETAILS = DETAILS, tmp
    try:
        with open(os.path.join(tmp, "x.json"), "w", encoding="utf-8") as f:
            json.dump(copy.deepcopy(D), f, ensure_ascii=False)
        rows = [{"slug": "x", "path": "sections[0].body[0]", "text": "",
                 "auto": True, "why": ""}]
        r = apply_slug("x", rows, limit=10)
        got = json.load(open(os.path.join(tmp, "x.json"), encoding="utf-8"))
        t("　消した結果、その段落だけが消える",
          r["wrote"] and got["sections"][0]["body"] == ["通常時は800Gで天井に到達します。"]
          and got["sections"][1] == D["sections"][1])
        # 上限を超えたら何もしない
        with open(os.path.join(tmp, "y.json"), "w", encoding="utf-8") as f:
            json.dump(copy.deepcopy(D), f, ensure_ascii=False)
        r2 = apply_slug("y", [dict(rows[0], slug="y"),
                              dict(rows[0], slug="y",
                                   path="sections[0].body[1]")], limit=1)
        same = json.load(open(os.path.join(tmp, "y.json"), encoding="utf-8"))
        t("★★上限を超えたら1つも消さない★★",
          not r2["wrote"] and same["sections"][0]["body"] == D["sections"][0]["body"])
        # 全部消えて空になる指示は拒否
        with open(os.path.join(tmp, "z.json"), "w", encoding="utf-8") as f:
            json.dump({"sections": [{"title": "t", "body": ["a。", "b。"]}]},
                      f, ensure_ascii=False)
        r3 = apply_slug("z", [{"slug": "z", "path": "sections[0].body[0]",
                               "text": "", "auto": True, "why": ""},
                              {"slug": "z", "path": "sections[0].body[1]",
                               "text": "", "auto": True, "why": ""}], limit=10)
        t("★★本文が空になる消し方は拒否する★★",
          not r3["wrote"] and any("空になった" in x for x in r3["problems"]))
    finally:
        DETAILS = real
        __import__("shutil").rmtree(tmp, ignore_errors=True)
    print(f"\n{ran[0]}/{ran[0]} 合格" if ok else "\n不合格あり")
    return 0 if ok else 1


def main() -> int:
    # ★日本語Windowsの画面でも落ちないようにする★（2026-08-05・台帳#227）
    #   記事の本文には「—」など cp932 に無い字が混ざる。そのまま出すと
    #   無人タスクの点検が**途中で黙って落ちる**（実際に起きた）。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                     # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="危ない表現を原子ごと消す")
    ap.add_argument("--slug")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=1,
                    help="1機種で消してよい原子の数（★既定1＝canary★）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    rows = plan(a.slug)
    auto = [r for r in rows if r["auto"]]
    hand = [r for r in rows if not r["auto"]]
    print(f"消せる: {len(auto)}箇所 / 人が見る: {len(hand)}箇所 "
          f"/ 機種{len({r['slug'] for r in rows})}件")
    for r in rows:
        print(f"  {'自動' if r['auto'] else '人送り'} {r['slug']:<22} "
              f"{r['path']:<24} {r['text'][:52]}"
              + ("" if r["auto"] else f" ← {r['why']}"))
    if not a.apply:
        print("\n（下見です。消すには --slug を付けて --apply）")
        return 0
    if not a.slug:
        print("★--apply には --slug が要ります★（1機種ずつ）")
        return 1
    r = apply_slug(a.slug, rows, a.limit)
    print(json.dumps(r, ensure_ascii=False))
    return 0 if not r["problems"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except _sj.SafeJsonError as e:
        print(f"★入力データが読めません: {e}★")
        raise SystemExit(1)

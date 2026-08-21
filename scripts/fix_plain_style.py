#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""★常体の文末を「です・ます」へそろえる★（CLAUDE.md の文体ルール）

★決まりごと★
  「機種記事は**です・ます調で統一**する（だ・である は禁止）」

★この道具がやること★
  ★文末の言い切りだけ★を、決めてある対のとおりに置き換える。

★この道具がやらないこと★
  ・文の中身を変えない（数値・条件・言い回しは触らない）
  ・新しい文を作らない
  ・箇条書きの項目・体言止め・見出しには触らない

★触る条件（全部そろったときだけ）★
  ①その文が「。」で終わっている（＝箇条書きの項目ではない）
  ②12字以上（＝短い項目名を拾わない）
  ③行頭が「**」「・」「-」でない（＝見出しや箇条書きではない）
  ④文末が、下の表にある形とちょうど一致する

★なぜ表で持つのか★
  「〜する」で終わる語尾を機械で活用させると、機種名（「七つの魔剣が支配する」）
  まで書き換えてしまう。★実際、字面で探したら誤検知した★。
  だから**書き換えてよい形を1つずつ数え上げる**。
  表に無い形は触らない（安全側）。

使い方:
  python scripts/fix_plain_style.py              （全機種・見るだけ）
  python scripts/fix_plain_style.py --slug tenken
  python scripts/fix_plain_style.py --apply
  python scripts/fix_plain_style.py --selftest
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
import page_decision as _pd  # noqa: E402

DETAILS = os.path.join(BASE, "assets", "data", "machine-details")

# ★★書き換えてよい文末の対★★（★1つずつ数え上げる★）
#   左が常体の終わり方、右が「です・ます」の終わり方。
#   ★意味を変えない対だけ★＝丁寧さだけが変わる。
ENDINGS = (
    ("がある。", "があります。"),
    ("される。", "されます。"),
    ("られる。", "られます。"),
    ("できる。", "できます。"),
    ("広がる。", "広がります。"),
    ("変わる。", "変わります。"),
    ("増やせる。", "増やせます。"),
    ("狙える。", "狙えます。"),
    ("ヤメる。", "ヤメましょう。"),
    ("立ち回る。", "立ち回りましょう。"),
    ("覚えておく。", "覚えておきましょう。"),
    ("必要だ。", "必要です。"),
    ("重要だ。", "重要です。"),
    ("有効だ。", "有効です。"),
    ("である。", "です。"),
    # ★表の注記でだけ出てくる終わり方★（2026-08-21・台帳#332・実データ4件）
    #   dumbbell「…出ないので注意。」／gineiden_dnt「…状況判断が必要。」
    #   hanabi「設定1では出現しない。」／hokuto「…高設定否定にならない。」
    ("ので注意。", "ので注意してください。"),
    ("が必要。", "が必要です。"),
    ("出現しない。", "出現しません。"),
    ("ならない。", "なりません。"),
)
# ★左右が同じ対は入れない★（何も変わらない書き換えを作らないため）
ENDINGS = tuple((a, b) for a, b in ENDINGS if a != b)

_SKIP_HEAD = ("**", "・", "-", "＊", "※")


def rewrite_sentence(sent: str, min_len: int = 12):
    """1文を書き換えた結果を返す（変えないなら None）。"""
    s = str(sent or "")
    t = s.strip()
    if not t.endswith("。"):
        return None                     # ①句点で終わっていない
    if len(t) < min_len:
        # ②短すぎる
        # ★2026-08-21に20→12へ下げた★＝「朝一から複数の狙い目が広がる。」(14字)
        #   「スルー数はAT終了でリセットされる。」(17字) を取りこぼしていた。
        #   ★下げても安全★＝下の「表に載っている終わり方」で既に強く絞れている
        #   （表に無い形は触らないので、短い項目名を巻き込まない）。
        return None
    if t.startswith(_SKIP_HEAD):
        return None                     # ③見出し・箇条書き
    for a, b in ENDINGS:
        if t.endswith(a):
            return s.replace(a, b) if s.rstrip().endswith(a) else None
    return None


def _rewrite_line(line: str, min_len: int = 12):
    """1行ぶんを書き換えた結果を返す（変えないなら None）。

    ★注記は文が短い★（2026-08-21・台帳#332）＝
      「設定1では出現しない。」は11字で、本文用の下限（12字）に届かない。
      注記だけ下限を下げる。★表に載っている終わり方だけを直す★という
      強い絞りは変えていないので、下げても短い項目名は巻き込まない。
    """
    if not isinstance(line, str):
        return None
    parts = re.split(r"(?<=。)", line)
    new_parts, hit = [], False
    for p in parts:
        r = rewrite_sentence(p, min_len=min_len)
        if r is not None:
            new_parts.append(r)
            hit = True
        else:
            new_parts.append(p)
    return "".join(new_parts) if hit else None


def plan_for(detail: dict) -> list:
    """書き換える場所を返す（★書かない★）。

    ★★表の中の注記も見る★★（2026-08-21・台帳#332）
      ★直す前は body だけを見ていた★ので、
      設定示唆まとめの `tables[].note` に常体が残っていた
      （実例＝hokuto「…高設定否定にならない。」「…参考程度に。」）。
      台帳#332 が「#122のスキャンはbodyのみでtables配下のnoteを見ていない
      ＝検知の穴」と指摘していたとおりだった。

      ★設定示唆の節そのものは body を持たない★ので、
      「type が settei なら丸ごと飛ばす」も一緒に見直した
      （飛ばすのは行の並び＝rows/tables の中身であって、注記ではない）。

    戻り値の場所の書き方:
      ("body", si, bi)           … 本文の行
      ("table_note", si, ti)     … 表の注記
      ("sec_note", si, key)      … 節の注記（note / resultNote）
    """
    out = []
    for si, sec in enumerate(detail.get("sections") or []):
        if sec.get("type") != "settei":
            for bi, line in enumerate(sec.get("body") or []):
                got = _rewrite_line(line)
                if got is not None:
                    out.append((("body", si, bi), line, got,
                                str(sec.get("title") or "")))
        # ★表の注記は、設定示唆の節でも見る★
        for ti, tbl in enumerate(sec.get("tables") or []):
            got = _rewrite_line(tbl.get("note"), min_len=9)
            if got is not None:
                out.append((("table_note", si, ti), tbl.get("note"), got,
                            str(sec.get("title") or "") + "／表の注記"))
        for key in ("note", "resultNote"):
            got = _rewrite_line(sec.get(key), min_len=9)
            if got is not None:
                out.append((("sec_note", si, key), sec.get(key), got,
                            str(sec.get("title") or "") + f"／{key}"))
    return out


def run(slug: str | None = None, apply_it: bool = False) -> dict:
    rows = _sj.read_rows(os.path.join(BASE, "assets", "data", "machines.json"))
    result = {"changed": [], "n": 0}
    for m in rows:
        sl = m.get("slug")
        if slug and sl != slug:
            continue
        # ★旧形式の公開記事だけ★（新台経路は別の作り方）
        if _pd.machine_class(m) != "LEGACY_COMPLETE":
            continue
        p = os.path.join(DETAILS, sl + ".json")
        if not os.path.isfile(p):
            continue
        d = _sj.read_json(p, expect=dict)
        plan = plan_for(d)
        if not plan:
            continue
        result["n"] += len(plan)
        result["changed"].append({"slug": sl, "items": plan})
        if apply_it:
            for where, _old, new, _t in plan:
                kind, si, key = where
                sec = d["sections"][si]
                if kind == "body":
                    sec["body"][key] = new
                elif kind == "table_note":
                    sec["tables"][key]["note"] = new
                else:
                    sec[key] = new
            tmp = p + ".tmp"
            with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
                f.write("\n")
            os.replace(tmp, p)
    return result


def _selftest() -> int:
    ng = []

    def t(name, cond):
        print(("✅ " if cond else "❌ ") + name)
        if not cond:
            ng.append(name)

    t("★文末だけを丁寧にする★",
      rewrite_sentence("高設定域では通常時のモード移行率が優遇されているとの見方がある。")
      == "高設定域では通常時のモード移行率が優遇されているとの見方があります。")
    t("　受け身の言い切りも直す",
      rewrite_sentence("ボーナス間天井は通常ボーナスが当選した台に適用される。")
      == "ボーナス間天井は通常ボーナスが当選した台に適用されます。")
    t("★★句点で終わっていない行は触らない★★（箇条書きの項目）",
      rewrite_sentence("虚構連モードスルー時はゲーム数を引き継ぐ") is None)
    t("★★見出しの行は触らない★★",
      rewrite_sentence("**リセット**：天井が大幅短縮され0G〜から狙える。") is None)
    t("★★機種名を書き換えない★★（「支配する」で終わる実例がある）",
      rewrite_sentence("**機種名**：スマスロ 七つの魔剣が支配する") is None)
    t("　短すぎる文は触らない（7字）", rewrite_sentence("狙い目が広がる。") is None)
    t("　14字なら直す（下限を下げた分）",
      rewrite_sentence("朝一から複数の狙い目が広がる。")
      == "朝一から複数の狙い目が広がります。")
    t("★★表に無い形は触らない（安全側）★★",
      rewrite_sentence("この機種は朝から打つと勝ちやすいという噂もあるらしい。")
      is None)
    t("　もう丁寧な文は触らない",
      rewrite_sentence("高設定域ではモード移行率が優遇されている可能性があります。")
      is None)
    t("　1行に2文あっても、当てはまる文だけ直す",
      plan_for({"sections": [{"title": "x", "body": [
          "天井は999Gです。AT間天井とボーナス間天井は独立してカウントされる。"]}]})[0][2]
      == "天井は999Gです。AT間天井とボーナス間天井は独立してカウントされます。")
    t("　設定示唆の節の「行の並び」は触らない",
      plan_for({"sections": [{"title": "x", "type": "settei", "body": [
          "高設定域では通常時のモード移行率が優遇されているとの見方がある。"]}]}) == [])
    # ★★表の中の注記は見る★★（2026-08-21・台帳#332）
    #   ★直す前は body だけ見ていた★ので、設定示唆まとめの tables[].note に
    #   常体が残っていた（実例＝hokuto「…高設定否定にならない。」）。
    _tn = plan_for({"sections": [{"title": "設定示唆まとめ", "type": "settei",
                                  "tables": [{"note": "設定6を8000G回しても"
                                              "出現率は約20%あることがある。"}]}]})
    t("★★設定示唆の表の注記も直す★★（#122のスキャンが見ていなかった穴）",
      len(_tn) == 1 and _tn[0][0][0] == "table_note"
      and _tn[0][2].endswith("あります。"))
    t("　節の注記（resultNote）も見る",
      len(plan_for({"sections": [{"title": "x", "resultNote":
                                  "スイカ確率で設定を推測できる。"}]})) == 1)

    print()
    print(f"{13 - len(ng)}/13 " + ("合格" if not ng else "不合格"))
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
          + f" {len(r['changed'])} 機種 / {r['n']} 行")
    for c in r["changed"]:
        print(f"  {c['slug']}")
        for _where, old, new, title in c["items"]:
            # ★変わったところだけを見せる★
            i = 0
            while i < min(len(old), len(new)) and old[i] == new[i]:
                i += 1
            print(f"      [{title}] …{old[max(0, i - 26):][:46]}")
            print(f"      → …{new[max(0, i - 26):][:46]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

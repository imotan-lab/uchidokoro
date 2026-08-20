#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""★噂の箱について、2AIが決めた結果をそのとおりに書く★（2026-08-21・台帳#334）

★機械は判断しない★＝どの機種をどうするかは
`Documents/uchidokoro/decisions/rumor_boxes_2026-08-21.md` に記録した判断がすべて。
このスクリプトは、その決定を**取り違えずに書く**だけの役。

判断の基準（判断記録より）:
  噂の箱は「主張」があるときだけ出す。
  「Xは未確認／解析待ち／情報が入り次第更新」は何も言っていない＝中身なし。

  中身なし → 箱ごと外す
  中身あり → 「噂はありません」の行だけ消す（箱は残す＝読者に届く情報を消さない）

★どちらも新しい文章は書かない★（消すだけ）。

    python scripts/apply_rumor_decisions.py           # 下見
    python scripts/apply_rumor_decisions.py --apply
    python scripts/apply_rumor_decisions.py --selftest
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DETAILS = os.path.join(BASE, "assets", "data", "machine-details")

NONE_PHRASES = (
    "噂・未確定情報はありません",
    "噂はありません",
    "未確定情報はありません",
)

# ★A: 箱ごと外す（主張が無い15機種）★
DROP_BOX = (
    "akudama", "animal_dotch", "chibaryo2", "code_geass", "goji_eva",
    "gundam_uc2", "kengan_ashura", "kyokousuiri", "lupin_daikokaisha",
    "madomagi_forte", "mushoku", "rezero2", "sengoku_otome4",
    "takt_opus", "triple_crown_7",
)

# ★B: 「噂はありません」の行だけ消す（主張がある13機種）★
DROP_LINE = (
    "dark_haibi", "dragon_hanahana_senko", "goblin", "hanabi", "hihou",
    "king_hanahana", "mr_juggler", "new_king_hanahana_v", "okidoki_gorgeous",
    "onimusha3", "tolove_darkness", "umineko2", "yoshimune",
)


def says_none(line) -> bool:
    return isinstance(line, str) and any(p in line for p in NONE_PHRASES)


def _load(slug):
    path = os.path.join(DETAILS, f"{slug}.json")
    if not os.path.exists(path):
        return None, None
    with io.open(path, encoding="utf-8") as f:
        return path, json.load(f)


def _rumor_index(detail):
    """噂の箱の位置。★1つでなければ触らない★（順序の意図が読めないため）"""
    secs = detail.get("sections")
    if not isinstance(secs, list):
        return None
    idx = [i for i, s in enumerate(secs)
           if isinstance(s, dict) and s.get("type") == "rumor"]
    return idx[0] if len(idx) == 1 else None


def plan():
    out = []
    for slug in DROP_BOX:
        path, detail = _load(slug)
        if detail is None:
            out.append((slug, "MISSING", "記事データがありません", None))
            continue
        i = _rumor_index(detail)
        if i is None:
            out.append((slug, "SKIP", "噂の箱が1つではありません", None))
            continue
        body = detail["sections"][i].get("body") or []
        if not any(says_none(b) for b in body):
            # ★決定の前提が崩れていたら触らない★（あとから中身が入った等）
            out.append((slug, "SKIP", "「噂はありません」の行がもうありません", None))
            continue
        out.append((slug, "DROP_BOX", f"{len(body)}行の箱を外す", (path, i)))
    for slug in DROP_LINE:
        path, detail = _load(slug)
        if detail is None:
            out.append((slug, "MISSING", "記事データがありません", None))
            continue
        i = _rumor_index(detail)
        if i is None:
            out.append((slug, "SKIP", "噂の箱が1つではありません", None))
            continue
        body = detail["sections"][i].get("body") or []
        hits = [j for j, b in enumerate(body) if says_none(b)]
        if not hits:
            out.append((slug, "SKIP", "「噂はありません」の行がもうありません", None))
            continue
        rest = [b for j, b in enumerate(body) if j not in hits]
        if not any(isinstance(b, str) and b.strip() for b in rest):
            # ★消したら空になる箱は、行だけ消す対象ではない★
            out.append((slug, "SKIP", "その行を消すと箱が空になります", None))
            continue
        out.append((slug, "DROP_LINE", f"{len(hits)}行を消す", (path, i)))
    return out


def apply_plan(items) -> tuple[int, int]:
    dropped = lines = 0
    for slug, action, _why, where in items:
        if where is None:
            continue
        path, i = where
        with io.open(path, encoding="utf-8") as f:
            detail = json.load(f)
        # ★書く直前にもう一度確かめる★（下見と実行の間に変わっている可能性）
        j = _rumor_index(detail)
        if j != i:
            print(f"  skip（下見のときと違います）: {slug}")
            continue
        sec = detail["sections"][i]
        body = sec.get("body") or []
        if action == "DROP_BOX":
            if not any(says_none(b) for b in body):
                print(f"  skip（前提が変わりました）: {slug}")
                continue
            del detail["sections"][i]
            dropped += 1
        else:
            keep = [b for b in body if not says_none(b)]
            if len(keep) == len(body) or not keep:
                print(f"  skip（前提が変わりました）: {slug}")
                continue
            lines += len(body) - len(keep)
            sec["body"] = keep
        with io.open(path, "w", encoding="utf-8") as f:
            json.dump(detail, f, ensure_ascii=False, indent=2)
            f.write("\n")
    return dropped, lines


def _selftest() -> int:
    ok = total = 0

    def t(name, cond):
        nonlocal ok, total
        total += 1
        ok += 1 if cond else 0
        print(("OK   " if cond else "NG   ") + name)

    t("「無い」宣言を見分ける", says_none("現時点で目立った噂・未確定情報はありません。"))
    t("★ふつうの文は宣言ではない★", not says_none("フェザーランプに設定示唆があるとされます。"))
    t("★文字列でなければ宣言ではない★", not says_none(None))

    t("★2つの表に同じ機種を入れていない★",
      not (set(DROP_BOX) & set(DROP_LINE)))
    t("28機種ぶんある", len(DROP_BOX) + len(DROP_LINE) == 28)
    t("★重複が無い★",
      len(set(DROP_BOX)) == len(DROP_BOX) and len(set(DROP_LINE)) == len(DROP_LINE))

    d = {"sections": [{"type": "rumor"}, {"type": "settei"}]}
    t("噂の箱の位置が分かる", _rumor_index(d) == 0)
    t("★噂の箱が2つなら触らない★",
      _rumor_index({"sections": [{"type": "rumor"}, {"type": "rumor"}]}) is None)
    t("★箱が無ければ触らない★", _rumor_index({"sections": []}) is None)
    t("★本文の形が違えば触らない★", _rumor_index({"sections": "x"}) is None)

    print()
    print(f"{ok}/{total} 合格")
    return 0 if ok == total else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="噂の箱について決めたとおりに書く")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()

    items = plan()
    for slug, action, why, _w in items:
        print(f"  {action:9s} {slug}: {why}")
    skipped = [i for i in items if i[1] in ("SKIP", "MISSING")]
    print()
    print(f"外す箱 {len([i for i in items if i[1]=='DROP_BOX'])} / "
          f"行を消す {len([i for i in items if i[1]=='DROP_LINE'])} / "
          f"触らない {len(skipped)}")
    if not a.apply:
        print("★下見です（何も書き換えていません）★ 実行するなら --apply")
        return 0
    d, ln = apply_plan(items)
    print()
    print(f"★箱を{d}個外し、{ln}行を消しました★")
    print("  このあと: build_machine_pages.py --legacy → crosscheck_gates.py → audit_site.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""計算不能な断定（期待値/収支/枚数）の除去・目安化を、安全ガード付きで適用する。
- パス指定でJSON葉を書き換える（machines.json / machine-details/*.json）。
- ガード: ①数値保全（変更前後で数字列の多重集合が不変＝新しい数値を発明できない）
          ②禁止語スキャン（変更後テキストに断定語が残らない）
          ③楽観ロック（現在値が edits の old と一致する時だけ書く）
- 既定 dry-run。--apply で書き込み。round-trip整形は原本と一致することを確認済み。
使い方:
  python scripts/apply_claim_edits.py --edits _design/edits_batchN.json [--apply]
  python scripts/apply_claim_edits.py --scan            # 全corpusの禁止語残存を検査（検証器）
edits JSON: [{"file": "...", "path": "machines[12].checker.suru.note", "old": "...", "new": "..."}]
"""
import json, re, sys, os, glob, argparse
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "assets", "data")

# 断定語（変更後に1つでも残れば不合格）。狙い目の数値そのものは残す。
# 本バッチ=計算できない断定（期待値/収支/枚数）のみ。行動推奨「着席検討」は対象外。
BANNED = ["期待収支", "プラス域", "プラス圏", "プラスライン", "プラス期待値",
          "期待値プラス", "期待値がプラス", "プラスに転じ", "プラスになり", "プラスに入",
          "利益ゾーン", "確実な利益", "期待枚数", "獲得期待枚数", "期待差枚",
          "損益分岐", "時給", "プラスで完結"]
BANNED_PAT = re.compile("|".join(re.escape(t) for t in BANNED))


def digits(s):
    return sorted(re.findall(r"\d+", s))


def resolve(data, path):
    """path文字列から (parent, key) を返す。'machines[i]...' と 'root...' に対応。
    キーは英数混在可（eq56 等）。セグメントは '.' 区切り＋末尾に [n] 添字。"""
    toks = []
    for seg in path.split("."):
        m = re.match(r"^([^\[\].]+)((?:\[\d+\])*)$", seg)
        if not m:
            raise ValueError(f"bad path segment: {seg}")
        toks.append(m.group(1))
        for im in re.finditer(r"\[(\d+)\]", m.group(2)):
            toks.append(int(im.group(1)))
    if toks and toks[0] in ("root", "machines"):
        toks = toks[1:]  # 'root'=詳細JSONの全体 / 'machines'=リストの語を落とし添字を残す
    node = data
    for t in toks[:-1]:
        node = node[t]
    return node, toks[-1]


def load(fp):
    return json.loads(open(fp, encoding="utf-8").read())


def dump(fp, data):
    open(fp, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def scan_corpus():
    """全 authoring を走査し、禁止語が残っている葉を列挙（検証器）。"""
    hits = []

    def walk(node, path, fname):
        if isinstance(node, str):
            if BANNED_PAT.search(node):
                hits.append((fname, path, node))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]", fname)
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}", fname)

    machines = load(os.path.join(DATA, "machines.json"))
    for i, m in enumerate(machines):
        walk(m, f"machines[{i}]", "machines.json")
    for fp in sorted(glob.glob(os.path.join(DATA, "machine-details", "*.json"))):
        walk(load(fp), "root", os.path.basename(fp))
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--edits")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--scan", action="store_true")
    args = ap.parse_args()

    if args.scan:
        hits = scan_corpus()
        print(f"禁止語残存: {len(hits)} 件")
        for f, p, t in hits[:60]:
            print(f"  [{f}] {p}: {t[:80]}")
        sys.exit(0 if not hits else 2)

    edits = load(args.edits)
    # file単位にまとめる
    by_file = {}
    for e in edits:
        by_file.setdefault(e["file"], []).append(e)

    errors, applied = [], 0
    for fname, es in by_file.items():
        fp = os.path.join(BASE, fname) if fname.startswith("assets") else os.path.join(DATA, "machine-details", fname) if not fname.startswith("assets") and fname.endswith(".json") and "/" not in fname else os.path.join(BASE, fname)
        # 正規化: fnameは常に repo相対 (assets/data/...) を期待
        fp = os.path.join(BASE, fname)
        if not os.path.isfile(fp):
            errors.append(f"{fname}: file not found ({fp})"); continue
        data = load(fp)
        for e in es:
            parent, key = resolve(data, e["path"])
            cur = parent[key]
            if "old" in e and cur != e["old"]:
                errors.append(f"{fname} {e['path']}: LOCK FAIL 現在値が想定と不一致"); continue
            new = e["new"]
            # 数値ガード: new の数字は old の部分集合（追加=発明は禁止・削除は許可）
            if not (Counter(digits(new)) <= Counter(digits(cur))):
                added = list((Counter(digits(new)) - Counter(digits(cur))).elements())
                errors.append(f"{fname} {e['path']}: 新規数値の発明 {added}"); continue
            if BANNED_PAT.search(new):
                m = BANNED_PAT.search(new)
                errors.append(f"{fname} {e['path']}: 断定語残存 '{m.group(0)}' in new"); continue
            if not args.apply:
                print(f"[DRY] {e.get('slug','')} {e['path']}\n   - {cur}\n   + {new}")
            parent[key] = new
            applied += 1
        if args.apply and not errors:
            dump(fp, data)

    print(f"\n{'適用' if args.apply else 'DRY-RUN'}: {applied} 件 / エラー {len(errors)} 件")
    for er in errors:
        print("  ERROR:", er)
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()

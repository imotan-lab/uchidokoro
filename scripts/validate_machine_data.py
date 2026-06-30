# -*- coding: utf-8 -*-
"""機種データの数値整合性チェック（検知のみ・データは一切変更しない）。

「複数サイト照合」では防げない幻覚値や、内部の数値破綻を、外部アクセス無しで
機械的に検出するための決定論スクリプト。check_22(重複検知)の数値版。

★このスクリプトはデータを書き換えない（純粋な検査）。誤情報を生む経路が無い。★
★既存の自動タスクからはまだ呼ばれない（別ファイル）。導入は後日SKILL.md側で。★

検査内容（保守的・偽陽性を避ける設計。怪しいだけでは flag しない）:
  V1. checker閾値の順序破綻: caution ≤ good ≤ excellent ≤ limit が崩れている
      （99999センチネル・nullは設定狙い専用なので除外）
  V2. 99999/99998 センチネルが evTable.range に残留（天井あり機種のみ。設定専用は正当なので除外）
  V3. （保留）天井の構造化フィールド間照合: 機種は正当に複数天井(CZ間/AT間・通常A/B/C・
      G数/ポイント)を持つため単純比較は誤検知だらけ。天井種別マッチングが要るので v1 では未実装
  V4. 機械割の異常: factTableの機械割が 85〜120% の範囲外、または 設定1 ≥ 設定6（高設定の方が低い）

使い方:
  python scripts/validate_machine_data.py            # 全機種を検査（NG合計>0でexit1）
  python scripts/validate_machine_data.py --slug X   # 1機種だけ
  python scripts/validate_machine_data.py --quiet     # NG行のみ出力
"""
import argparse
import json
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent
MACHINES = BASE / "assets" / "data" / "machines.json"
DETAILS = BASE / "assets" / "data" / "machine-details"

SENTINELS = {99999, 99998, 999999}


def _num(s):
    """文字列から先頭付近の整数を1つ取り出す（'1250G+α'→1250, '最大2500G'→2500）。無ければNone。"""
    if isinstance(s, (int, float)):
        return int(s)
    if not isinstance(s, str):
        return None
    m = re.search(r"(\d[\d,]*)", s)
    return int(m.group(1).replace(",", "")) if m else None


def _pct(s):
    """'97.2%' → 97.2 を取り出す。無ければNone。"""
    if not isinstance(s, str):
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", s)
    return float(m.group(1)) if m else None


def _is_setting_only(machine):
    """設定狙い専用機（天井なし）か。checker.limit が null、または checker閾値が全て99999。"""
    if machine.get("limit") is None and machine.get("checker") is None:
        return True
    ch = machine.get("checker") or {}
    if ch.get("unit") and not ch.get("limit") and machine.get("limit") is None:
        # normal閾値が全てセンチネルなら設定専用
        n = ch.get("normal") or {}
        vals = [n.get(k) for k in ("excellent", "good", "caution") if isinstance(n.get(k), (int, float))]
        if vals and all(v in SENTINELS for v in vals):
            return True
    return False


def _iter_threshold_blocks(obj, path=""):
    """checker配下で excellent/good/caution(/limit) を持つ辞書を再帰的に列挙。"""
    if isinstance(obj, dict):
        keys = obj.keys()
        if ("good" in keys or "excellent" in keys or "caution" in keys) and \
           any(isinstance(obj.get(k), (int, float)) for k in ("good", "excellent", "caution")):
            yield path or "checker", obj
        for k, v in obj.items():
            yield from _iter_threshold_blocks(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _iter_threshold_blocks(v, f"{path}[{i}]")


def check_machine(machine, detail):
    ngs = []
    slug = machine.get("slug", "?")
    if machine.get("status") == "preview":
        return ngs  # 先行記事は解析待ちなので数値検査の対象外
    setting_only = _is_setting_only(machine)
    checker = machine.get("checker") or {}

    # --- V1: checker閾値の順序破綻 ---
    if not setting_only:
        for bpath, blk in _iter_threshold_blocks(checker):
            seq = []
            for key in ("caution", "good", "excellent", "limit"):
                v = blk.get(key)
                if isinstance(v, (int, float)) and v not in SENTINELS:
                    seq.append((key, v))
            for i in range(len(seq) - 1):
                (k1, v1), (k2, v2) = seq[i], seq[i + 1]
                if v1 > v2:
                    ngs.append(f"V1 checker閾値の順序破綻 [{bpath}]: {k1}={v1} > {k2}={v2}（caution≤good≤excellent≤limitであるべき）")

    # --- V2: evTableのセンチネル残留（天井あり機種のみ） ---
    if not setting_only and detail:
        for i, row in enumerate(detail.get("evTable") or []):
            rng = row.get("range") if isinstance(row, dict) else None
            n = _num(rng)
            if n is not None and n in SENTINELS:
                ngs.append(f"V2 evTableにセンチネル値残留: evTable[{i}].range='{rng}'（天井あり機種で99999は不正・要削除）")

    # --- V3: （保留）構造化フィールド間の天井不一致 ---
    # 機種は正当に複数の天井（CZ間/AT間・通常A/B/C・G数/ポイント）を持つため、
    # 単純な「天井ラベル同士の値比較」は誤検知だらけになる（ベースライン検査で確認済み）。
    # 同一の天井が複数箇所で食い違うケースだけを拾うには天井種別のマッチングが要るため、
    # 偽陽性ゼロ志向で v1 では実装しない。将来 per-天井-type 照合として再設計する。

    # --- V4: 機械割の異常 ---
    if detail:
        kw = {}
        for row in detail.get("factTable") or []:
            if isinstance(row, list) and len(row) == 2 and "機械割" in str(row[0]):
                p = _pct(row[1])
                if p is not None:
                    m = re.search(r"設定\s*([1-6])", str(row[0]))
                    kw[m.group(1) if m else row[0]] = p
        for label, p in kw.items():
            if p < 85 or p > 120:
                ngs.append(f"V4 機械割が異常レンジ: {label}={p}%（85〜120%想定）")
        if "1" in kw and "6" in kw and kw["1"] >= kw["6"]:
            ngs.append(f"V4 機械割の高低逆転: 設定1={kw['1']}% ≥ 設定6={kw['6']}%")

    return ngs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", default="", help="特定の1機種だけ検査")
    ap.add_argument("--quiet", action="store_true", help="NGのある機種のみ出力")
    args = ap.parse_args()

    machines = json.loads(MACHINES.read_text(encoding="utf-8"))
    if args.slug:
        machines = [m for m in machines if m.get("slug") == args.slug]

    total_ng = 0
    ng_machines = 0
    for m in machines:
        slug = m.get("slug", "?")
        dpath = DETAILS / f"{slug}.json"
        detail = json.loads(dpath.read_text(encoding="utf-8")) if dpath.is_file() else None
        ngs = check_machine(m, detail)
        if ngs:
            ng_machines += 1
            total_ng += len(ngs)
            print(f"⚠ {slug}（{m.get('name', '')}）")
            for ng in ngs:
                print(f"    - {ng}")
        elif not args.quiet:
            print(f"✅ {slug}")

    print("")
    print(f"=== 数値整合性チェック完了: {len(machines)}機種中 {ng_machines}機種 / NG合計 {total_ng}件 ===")
    sys.exit(1 if total_ng else 0)


if __name__ == "__main__":
    main()

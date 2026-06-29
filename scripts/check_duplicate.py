# -*- coding: utf-8 -*-
"""新台を machines.json に追加する【前】に実行する重複チェック。
候補の機種名（と任意でaliases）を既存全機種と照合し、同一機種の二重登録を未然に防ぐ。

使い方:
  python scripts/check_duplicate.py --name "スマスロ モンスターハンターライズ"
  python scripts/check_duplicate.py --name "L沖ドキ!DUOアンコール" --aliases "沖ドキ,DUO"

判定:
  重複の疑いあり -> 標準出力に「⚠ 重複の疑い」＋該当slug、exit code 1
  重複なし       -> 「✅ 重複なし（新規作成OK）」、exit code 0

★正規化ロジックは audit_site.py の check_22_duplicate_machines と必ず同一に保つこと★
（プレフィックス除去・記号除去・NFKC正規化）。片方だけ変えると検知漏れする。
"""
import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

# Windowsのcp932コンソールでも日本語・絵文字を出力できるようstdoutをUTF-8化
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent
MACHINES = BASE / "assets" / "data" / "machines.json"

_PREFIX = re.compile(r"^(スマスロ|スマパチ|パチスロ|ぱちすろ|L|Ｌ|P|Ｐ|新|新台)\s*")


def normalize_machine_name(name: str) -> str:
    s = unicodedata.normalize("NFKC", name or "")
    prev = None
    while prev != s:
        prev = s
        s = _PREFIX.sub("", s).strip()
    s = re.sub(r"[\s　・/／!！?？()（）\-—~〜【】\[\]、。,.'\"]+", "", s)
    return s.lower()


def _alias_key(a: str) -> str:
    return unicodedata.normalize("NFKC", a or "").strip().lower()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="新台の正式名称（候補）")
    ap.add_argument("--aliases", default="", help="候補のaliases（カンマ区切り・任意）")
    args = ap.parse_args()

    machines = json.loads(MACHINES.read_text(encoding="utf-8"))
    cand_norm = normalize_machine_name(args.name)
    cand_aliases = {_alias_key(a) for a in args.aliases.split(",") if a.strip()}

    hits = []
    for m in machines:
        # (1) 正規化名の一致
        if normalize_machine_name(m["name"]) == cand_norm:
            hits.append((m["slug"], m["name"], "名前が正規化一致"))
            continue
        # (2) 候補名 が既存aliasesに含まれる / 候補aliases が既存名・aliasesに含まれる
        existing_alias_keys = {_alias_key(a) for a in (m.get("aliases") or [])}
        existing_alias_keys.add(_alias_key(m["name"]))
        if _alias_key(args.name) in existing_alias_keys or (cand_aliases & existing_alias_keys):
            hits.append((m["slug"], m["name"], "別名が重複"))

    if hits:
        print(f"⚠ 重複の疑い: 候補『{args.name}』は既存機種と同一の可能性があります。")
        for slug, name, why in hits:
            print(f"   - 既存 slug='{slug}' name='{name}'（{why}）")
        print("→ 新しいslugで作らず、既存エントリを更新するか、人間に確認すること。")
        print("（スマスロ版/L版・無印/アンコール等の同一機種を二重登録しない）")
        sys.exit(1)
    else:
        print(f"✅ 重複なし（新規作成OK）: 『{args.name}』(正規化='{cand_norm}')")
        sys.exit(0)


if __name__ == "__main__":
    main()

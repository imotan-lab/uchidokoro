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
import urllib.parse
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


def norm_official_url(u: str) -> str:
    """公式URLを比べられる形にそろえる。★機種を分ける情報は消さない★

    ホスト名の大小・www・既定ポート・末尾スラッシュ・追跡パラメータだけを整理する。
    クエリは機種を分けている場合があるので、追跡用として知られたものだけ落とす。
    """
    if not u:
        return ""
    pr = urllib.parse.urlsplit(unicodedata.normalize("NFKC", str(u).strip()))
    host = pr.hostname or ""
    host = host.lower().removeprefix("www.")
    if pr.port and pr.port not in (80, 443):
        host = f"{host}:{pr.port}"
    keep = [(k, v) for k, v in urllib.parse.parse_qsl(pr.query, keep_blank_values=True)
            if not (k.lower().startswith("utm_")
                    or k.lower() in ("fbclid", "gclid", "yclid", "_ga"))]
    query = urllib.parse.urlencode(sorted(keep))
    path = (pr.path or "/").rstrip("/") or "/"
    return f"{host}{path}" + (f"?{query}" if query else "")


def norm_model_code(code: str) -> str:
    """型式名をそろえる。★記号を全部消さない★（別の型式とぶつかるため）"""
    t = unicodedata.normalize("NFKC", str(code or "")).lower()
    return re.sub(r"[\s　]+", "", t).replace("‐", "-").replace("−", "-")


def find_duplicates(name: str, aliases=None, official_urls=None,
                    model_codes=None) -> list:
    """★他のスクリプトから呼べる形★（新台追加の実行器が使う）

    2026-07-31: この判定はコマンドからしか使えず、`add_machine_run.py` が
    呼んでいなかった。そのため既存 `super_binmusume` があるのに
    別slug（lbinko）で二重登録できる状態だった（実際に確認）。
    """
    machines = json.loads(MACHINES.read_text(encoding="utf-8"))
    cand_norm = normalize_machine_name(name)
    cand_aliases = {_alias_key(a) for a in (aliases or []) if str(a).strip()}
    # ★名前以外の手がかりでも見る★（2026-07-31・Codex指摘）
    #   名前だけだと、表記を変えて別slugで二重登録する経路が残る。
    #   型式名は新台では無いことが多いので、**無くても警告にはしない**。
    cand_urls = {norm_official_url(u) for u in (official_urls or []) if u}
    cand_urls.discard("")
    cand_codes = {norm_model_code(c) for c in (model_codes or []) if c}
    cand_codes.discard("")
    hits = []
    for m in machines:
        ident = m.get("identity") or {}
        why = []
        if cand_urls and norm_official_url(ident.get("official_product_url")) in cand_urls:
            why.append("公式URLが一致")
        if cand_codes and norm_model_code(ident.get("regulatory_model_code")) in cand_codes:
            why.append("型式名が一致")
        if why:
            hits.append((m["slug"], m["name"], "／".join(why)))
            continue
        # (1) 正規化名の一致
        if normalize_machine_name(m["name"]) == cand_norm:
            hits.append((m["slug"], m["name"], "名前が正規化一致"))
            continue
        # (2) 候補名 が既存aliasesに含まれる / 候補aliases が既存名・aliasesに含まれる
        existing_alias_keys = {_alias_key(a) for a in (m.get("aliases") or [])}
        existing_alias_keys.add(_alias_key(m["name"]))
        if _alias_key(name) in existing_alias_keys or (cand_aliases & existing_alias_keys):
            hits.append((m["slug"], m["name"], "別名が重複"))
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="新台の正式名称（候補）")
    ap.add_argument("--aliases", default="", help="候補のaliases（カンマ区切り・任意）")
    ap.add_argument("--official-url", default="", help="候補の公式ページURL（任意）")
    ap.add_argument("--model-code", default="", help="候補の型式名（任意・新台では無いことが多い）")
    args = ap.parse_args()

    hits = find_duplicates(args.name,
                           [a for a in args.aliases.split(",") if a.strip()],
                           official_urls=[args.official_url] if args.official_url else [],
                           model_codes=[args.model_code] if args.model_code else [])
    cand_norm = normalize_machine_name(args.name)

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

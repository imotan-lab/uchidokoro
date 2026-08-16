# -*- coding: utf-8 -*-
"""migrate_identity_to_dmm.py — 公開済み機種の身元をP-WORLDからDMMへ移す。

★一度だけ使う道具★（2026-08-16・台帳#376）
  P-WORLDの利用規約がプログラムからのアクセスを禁じていたため、
  同定の正をDMMへ移しました。公開済み7機種の `identity` は
  P-WORLDのURLを指したままなので、ここで付け替えます。

★守る線★
  ①**slugは変えない**（読者のリンク・検索の登録が生きている）。
    slugとURLの対応は scripts/slug_binding.py の増やせない対応表が持つ。
  ②**型式名がDMM側にもあるなら、一致することを確かめてから書く**。
    食い違えば止める（別機種に付け替えてしまうため）。
  ③**P-WORLDで確かめた記録は消さない**（`_legacy_evidence_ref` に残す）。
    ★検定番号はDMMには無い★ので、これが唯一の記録になる。
  ④DMM側でも機種名・メーカー・導入日が合うことを毎回確かめ直す。

使い方:
    python scripts/migrate_identity_to_dmm.py            # 下見
    python scripts/migrate_identity_to_dmm.py --apply
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

MACHINES = os.path.join(BASE, "assets", "data", "machines.json")


def plan() -> tuple:
    """移す内容を組み立てる。返すもの: (移す一覧, 止める理由)"""
    import claim_identity as _ci
    import dmm_machine as _dm
    import safe_json as _sj
    import slug_binding as _sb

    raw = _sj.read_json(MACHINES, expect=(dict, list))
    ms = raw["machines"] if isinstance(raw, dict) else raw
    rows, ng = [], []
    today = datetime.date.today().isoformat()
    for m in ms:
        ident = m.get("identity") or {}
        url = str(ident.get("official_product_url") or "")
        if "p-world" not in url:
            continue
        slug = m.get("slug")
        want = _sb.LEGACY_BINDINGS.get(slug)
        if not want:
            ng.append(f"{slug}: 対応表にありません／★勝手に移しません★")
            continue
        mid = want.split("_", 1)[1]
        try:
            got = _dm.fetch(mid)
        except _dm.MachineError as e:
            ng.append(f"{slug}: DMMの機種ページを確かめられません: {str(e)[:120]}")
            continue
        # ★機種名が同じ機種を指しているか★
        ok, why = _dm.name_matches(got["heading"], m.get("name") or "")
        if not ok:
            ng.append(f"{slug}: 機種名が合いません: {why[:120]}")
            continue
        # ★型式名がDMMにもあるなら一致すること★
        have = str(ident.get("regulatory_model_code")
                   or ident.get("observed_model_code") or "")
        if have and got["model_code"]:
            if _ci.normalize_core(have) != _ci.normalize_core(got["model_code"]):
                ng.append(f"{slug}: 型式名が食い違います"
                          f"（手元={have} / DMM={got['model_code']}）")
                continue
        # ★導入日が合うこと★（DMMが月までなら月で比べる）
        rel = str(ident.get("market_release_date") or "")
        if rel and got["release_date"] and not rel.startswith(
                got["release_date"][:len(got["release_date"])]):
            ng.append(f"{slug}: 導入日が食い違います"
                      f"（手元={rel} / DMM={got['release_date']}）")
            continue
        new = dict(ident)
        # ★P-WORLDで確かめた記録は消さない★（検定番号はここにしか残らない）
        if ident.get("identity_evidence_ref"):
            new["_legacy_evidence_ref"] = ident["identity_evidence_ref"]
        new["_legacy_official_product_url"] = url
        new["official_product_url"] = got["url"]
        new["identity_binding"] = "DMM_MACHINE_PAGE"
        new["identity_evidence_ref"] = (
            f"dmm:{mid} "
            + (f"型式={got['model_code']} " if got["model_code"] else "型式=未掲載 ")
            + f"メーカー={got['maker']} 導入={got['release_date']} "
            f"確認日={today}")
        rows.append({"slug": slug, "name": m.get("name"), "old": url,
                     "new": got["url"], "dmm_id": mid,
                     "model_code_dmm": got["model_code"],
                     "model_code_ours": have, "maker": got["maker"],
                     "release": got["release_date"], "identity": new})
    return rows, ng


def apply(rows: list) -> int:
    import safe_json as _sj
    raw = _sj.read_json(MACHINES, expect=(dict, list))
    ms = raw["machines"] if isinstance(raw, dict) else raw
    by = {r["slug"]: r for r in rows}
    n = 0
    for m in ms:
        r = by.get(m.get("slug"))
        if r:
            m["identity"] = r["identity"]
            n += 1
    tmp = MACHINES + ".new"
    with open(tmp, "w", encoding="utf-8", newline=chr(10)) as f:
        json.dump(raw, f, ensure_ascii=False, indent=1)
        f.write(chr(10))
    os.replace(tmp, MACHINES)
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="公開済み機種の身元をDMMへ移す")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    rows, ng = plan()
    print("移す機種: %d件" % len(rows))
    for r in rows:
        print("  %-10s %-28s → %s" % (r["slug"], str(r["name"])[:26],
                                      r["new"]))
        print("      メーカー=%s 導入=%s 型式=%s"
              % (r["maker"], r["release"],
                 r["model_code_dmm"] or "（DMMには未掲載・手元の記録を残します）"))
    if ng:
        print()
        print("★移せないもの★")
        for x in ng:
            print("  -", x)
    if not a.apply:
        print()
        print("★下見です（--apply で書き換えます）★")
        return 1 if ng else 0
    if ng:
        print()
        print("★止めます★（1件でも確かめられないものがあるうちは書き換えません）")
        return 1
    n = apply(rows)
    print()
    print("%d件の身元をDMMへ移しました" % n)
    print("★slugは変えていません★（読者のリンクと検索の登録を守るため）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

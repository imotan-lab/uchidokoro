#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""★2AIが「消す」と決めた行を、そのとおりに消す★（台帳#121・#141）

★この道具がやること★
  決定ファイルに書いてある行を消すだけ。

★この道具がやらないこと★
  ・どの行を消すか自分で決めない（似ている度で自動削除しない）
  ・文章を書き足さない・書き換えない
  ＝★消すだけ★。新しい値も新しい言い回しも作らない。

★★どちらを残すかの順番★★（2026-08-21・Codexの助言）
  ①いま正しいと確かめられる側を残す
  ②★情報を包含する側を残す★（箇条書きか散文かは関係ない）
  ③中身が完全に同じときだけ、読みやすさで箇条書きを選ぶ
  ＝「箇条書きを残す」を最上位にしない。
    散文のほうが上位集合のことも、あとの訂正が散文だけに入っていることもある。
  ★重複ではない印★＝例外／数値／対象の時点／推奨の強さ／
    「解析待ち」などの確度／設定狙いと天井狙いの条件分け。
    これらが片方にしか無ければ、似ていても重複ではない。

★守る線★
  ①消す行は **本文そのもの（逐語）** で指定する。番号だけでは指定できない。
    ＝★記事が動いたら空振りして止まる★（別の行を消さない）
  ②逐語が実データと1文字でも違えば、その機種は触らない
  ③1つのセクションから**全部**は消さない（空のセクションを作らない）
  ④既定は dry-run。`--apply` で書く
  ⑤書いたら `machines/{slug}/index.html` の作り直しが要る（この道具はやらない）
  ⑥★全部を先に確かめてから、まとめて書く★（2026-08-21・Codexの指摘）
    1件ずつ書くと、後半で不一致が出たときに**途中まで書かれた状態**が残る。
  ⑦★触らなかった機種が1件でもあれば終了コードは 0 にしない★
    （黙って一部だけ適用されたことに気づけないため）
  ⑧★判断したときから記事全体が変わっていないこと★を指紋で確かめる
    （消す行だけ一致しても、他の行が書き換わっていれば判断はやり直し）

決定ファイルの形（JSON）:
  {"schema_version": "prose-dedup/v1",
   "decided_by": ["Claude", "codex"],
   "decided_at": "2026-08-21",
   "why": "…",
   "items": [
     {"slug": "basilisk_tenzen", "section": "ヤメ時の判断",
      "source_sha256": "判断したときの記事まるごとの指紋",
      "drop": ["消す行の逐語（完全一致）", "…"],
      "why": "[0][1] の言い換えで、新しい情報が無い"}
   ]}

使い方:
  python scripts/apply_prose_dedup.py --file <決定ファイル>
  python scripts/apply_prose_dedup.py --file <決定ファイル> --apply
  python scripts/apply_prose_dedup.py --selftest
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import safe_json as _sj      # noqa: E402

DETAILS = os.path.join(BASE, "assets", "data", "machine-details")
SCHEMA = "prose-dedup/v1"


# ★slugの形★（machines/{slug}/ に展開されるので、変な文字を通さない）
_SLUG = re.compile(r"^[a-z][a-z0-9_]{1,48}$")


class DedupError(Exception):
    pass


def file_sha256(path: str) -> str:
    """★記事まるごとの指紋★（判断したときから変わっていないかを見る）"""
    with open(path, "rb") as f:
        # ★改行の違いで別物にしない★（この環境は手元だけCRLFになる）
        return hashlib.sha256(f.read().replace(b"\r\n", b"\n")).hexdigest()


def _load_plan(path: str) -> dict:
    d = _sj.read_json(path, expect=dict)
    if d.get("schema_version") != SCHEMA:
        raise DedupError(f"知らない形です: {d.get('schema_version')!r}")
    if not isinstance(d.get("items"), list) or not d["items"]:
        raise DedupError("items がありません")
    # ★誰が決めたかを必ず残す★（2人以上）
    by = d.get("decided_by")
    if not isinstance(by, list) or len(by) < 2:
        raise DedupError("decided_by に判断者が2つ以上要ります（2AIで決めるため）")
    for it in d["items"]:
        for k in ("slug", "section", "drop", "why"):
            if not it.get(k):
                raise DedupError(f"items に {k} がありません: {it}")
        if not isinstance(it["drop"], list):
            raise DedupError(f"drop は一覧で書いてください: {it['slug']}")
        # ★slugを自己申告のまま使わない★（2026-08-21・Codexの指摘）
        if not _SLUG.match(str(it["slug"])):
            raise DedupError(f"slug の形が違います: {it['slug']!r}")
    return d


def plan_for(detail: dict, item: dict) -> dict:
    """1機種ぶんの結果を返す（★書かない★）。"""
    out = {"slug": item["slug"], "section": item["section"],
           "dropped": [], "problems": []}
    secs = detail.get("sections") or []
    hit = [s for s in secs if str(s.get("title") or "") == item["section"]]
    if len(hit) != 1:
        out["problems"].append(
            f"「{item['section']}」が {len(hit)} 個あります（1つでないと触りません）")
        return out
    body = list(hit[0].get("body") or [])
    keep = []
    want = list(item["drop"])
    for line in body:
        if line in want:
            want.remove(line)          # ★同じ行が2つあれば1つずつ消す★
            out["dropped"].append(line)
        else:
            keep.append(line)
    if want:
        out["problems"].append(
            f"実データに無い行が {len(want)} 件あります（記事が変わった可能性）: "
            + " / ".join(str(w)[:40] for w in want))
        return out
    if not keep:
        out["problems"].append("全部消すことになります（空のセクションは作りません）")
        return out
    out["keep"] = keep
    return out


def run(plan_path: str, apply_it: bool = False) -> dict:
    """★全部を先に確かめてから、まとめて書く★（2026-08-21・Codexの指摘）

    1件ずつ書くと、後半で不一致が出たときに**途中まで書かれた状態**が残り、
    しかもそれに気づけない。preflight が1件でも通らなければ**何も書かない**。
    """
    plan = _load_plan(plan_path)
    result = {"apply": apply_it, "ok": [], "skipped": [], "n_dropped": 0,
              "wrote": 0}
    ready = []
    for item in plan["items"]:
        p = os.path.join(DETAILS, item["slug"] + ".json")
        if not os.path.isfile(p):
            result["skipped"].append({"slug": item["slug"], "section": "",
                                      "problems": ["記事がありません"]})
            continue
        # ★判断したときから記事全体が変わっていないか★
        want_sha = str(item.get("source_sha256") or "")
        got_sha = file_sha256(p)
        if want_sha and want_sha != got_sha:
            result["skipped"].append({
                "slug": item["slug"], "section": item["section"],
                "problems": [f"判断したときから記事が変わっています"
                             f"（{want_sha[:12]}… → {got_sha[:12]}…）"
                             "。読み直して決め直してください"]})
            continue
        d = _sj.read_json(p, expect=dict)
        r = plan_for(d, item)
        if r["problems"]:
            result["skipped"].append(r)
            continue
        result["n_dropped"] += len(r["dropped"])
        result["ok"].append({"slug": r["slug"], "section": r["section"],
                             "n": len(r["dropped"]), "why": item["why"],
                             "sha256": got_sha})
        ready.append((p, d, r, item, got_sha))

    # ★★1件でも通らなければ書かない★★
    if result["skipped"]:
        result["blocked"] = True
        return result

    if apply_it:
        for p, d, r, item, got_sha in ready:
            # ★書く直前にもう一度、中身が動いていないか確かめる★
            if file_sha256(p) != got_sha:
                raise DedupError(
                    f"{item['slug']} が確認した直後に変わりました。何も書きません")
            for sec in d.get("sections") or []:
                if str(sec.get("title") or "") == item["section"]:
                    sec["body"] = r["keep"]
            tmp = p + ".tmp"
            with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
                f.write("\n")
            os.replace(tmp, p)
            result["wrote"] += 1
    return result


def _selftest() -> int:
    import tempfile
    ng = []

    ran = [0]          # ★実際に試した数を数える★（2026-08-27）

    #   ★直す前は分母が手書きだった★ので、

    #   試験を足しても分母が増えず、足した分が数えられなかった。

    def t(name, cond):

        ran[0] += 1
        print(("✅ " if cond else "❌ ") + name)
        if not cond:
            ng.append(name)

    D = {"sections": [{"title": "ヤメ時の判断",
                       "body": ["A の行です。", "B の行です。", "C の行です。"]}]}

    r = plan_for(D, {"slug": "x", "section": "ヤメ時の判断",
                     "drop": ["B の行です。"], "why": "言い換え"})
    t("★指定した行だけを消す★",
      r["dropped"] == ["B の行です。"]
      and r["keep"] == ["A の行です。", "C の行です。"])

    r2 = plan_for(D, {"slug": "x", "section": "ヤメ時の判断",
                      "drop": ["B の行です"], "why": "1文字違い"})
    t("★★逐語が1文字でも違えば触らない★★（別の行を消さないため）",
      bool(r2["problems"]) and not r2.get("keep"))

    r3 = plan_for(D, {"slug": "x", "section": "ヤメ時の判断",
                      "drop": ["A の行です。", "B の行です。", "C の行です。"],
                      "why": "全部"})
    t("★★1つのセクションから全部は消さない★★", bool(r3["problems"]))

    r4 = plan_for(D, {"slug": "x", "section": "無い見出し",
                      "drop": ["A の行です。"], "why": "…"})
    t("　見出しが無ければ触らない", bool(r4["problems"]))

    D2 = {"sections": [{"title": "ヤメ時の判断", "body": ["同じ行。", "同じ行。"]},
                       ]}
    r5 = plan_for(D2, {"slug": "x", "section": "ヤメ時の判断",
                       "drop": ["同じ行。"], "why": "重複"})
    t("　同じ行が2つあるときは1つだけ消す",
      r5["dropped"] == ["同じ行。"] and r5["keep"] == ["同じ行。"])

    D3 = {"sections": [{"title": "ヤメ時の判断", "body": ["a"]},
                       {"title": "ヤメ時の判断", "body": ["b"]}]}
    r6 = plan_for(D3, {"slug": "x", "section": "ヤメ時の判断",
                       "drop": ["a"], "why": "…"})
    t("★同じ見出しが2つある機種は触らない★（どちらか分からない）",
      bool(r6["problems"]))

    # ★決定ファイルの検査★
    td = tempfile.mkdtemp()
    bad = os.path.join(td, "bad.json")
    io.open(bad, "w", encoding="utf-8").write(json.dumps(
        {"schema_version": SCHEMA, "decided_by": ["Claude"],
         "items": [{"slug": "x", "section": "y", "drop": ["z"], "why": "w"}]}))
    try:
        _load_plan(bad)
        t("★★判断者が1人の決定ファイルは受け取らない★★", False)
    except DedupError as e:
        t("★★判断者が1人の決定ファイルは受け取らない★★", "2つ以上" in str(e))

    bad2 = os.path.join(td, "bad2.json")
    io.open(bad2, "w", encoding="utf-8").write(json.dumps(
        {"schema_version": "ちがう/v9", "decided_by": ["a", "b"],
         "items": [{"slug": "x", "section": "y", "drop": ["z"], "why": "w"}]}))
    try:
        _load_plan(bad2)
        t("　知らない形は受け取らない", False)
    except DedupError:
        t("　知らない形は受け取らない", True)

    bad3 = os.path.join(td, "bad3.json")
    io.open(bad3, "w", encoding="utf-8").write(json.dumps(
        {"schema_version": SCHEMA, "decided_by": ["a", "b"],
         "items": [{"slug": "x", "section": "y", "drop": ["z"]}]}))
    try:
        _load_plan(bad3)
        t("★理由の無い削除は受け取らない★", False)
    except DedupError:
        t("★理由の無い削除は受け取らない★", True)

    bad4 = os.path.join(td, "bad4.json")
    io.open(bad4, "w", encoding="utf-8").write(json.dumps(
        {"schema_version": SCHEMA, "decided_by": ["a", "b"],
         "items": [{"slug": "../evil", "section": "y", "drop": ["z"],
                    "why": "w"}]}))
    try:
        _load_plan(bad4)
        t("★★slug の形を確かめる★★（自己申告のまま使わない）", False)
    except DedupError as e:
        t("★★slug の形を確かめる★★（自己申告のまま使わない）",
          "slug の形" in str(e))

    # ★★1件でも通らなければ何も書かない★★（2026-08-21・Codexの指摘）
    #   ★直す前は1件ずつ書いていた★＝後半で不一致が出ると
    #   **途中まで書かれた状態**が残り、しかも終了コードは0だった。
    real = os.path.join(td, "details")
    os.makedirs(real, exist_ok=True)
    _keep_details = globals()["DETAILS"]
    globals()["DETAILS"] = real
    try:
        good = {"sections": [{"title": "ヤメ時の判断",
                              "body": ["のこす行です。", "けす行です。"]}]}
        gp = os.path.join(real, "aaa.json")
        with io.open(gp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(good, f, ensure_ascii=False, indent=1)
            f.write("\n")
        plan2 = os.path.join(td, "plan2.json")
        io.open(plan2, "w", encoding="utf-8").write(json.dumps(
            {"schema_version": SCHEMA, "decided_by": ["a", "b"],
             "items": [
                 {"slug": "aaa", "section": "ヤメ時の判断",
                  "drop": ["けす行です。"], "why": "言い換え"},
                 {"slug": "bbb", "section": "ヤメ時の判断",
                  "drop": ["ある行"], "why": "記事が無い機種"}]},
            ensure_ascii=False))
        rr = run(plan2, apply_it=True)
        t("★★1件でも通らなければ、通る分も書かない★★",
          rr.get("blocked") is True and rr["wrote"] == 0)
        with io.open(gp, encoding="utf-8") as f:
            t("　通る側の記事もそのまま",
              len(json.load(f)["sections"][0]["body"]) == 2)

        # ★指紋が違えば触らない★
        plan3 = os.path.join(td, "plan3.json")
        io.open(plan3, "w", encoding="utf-8").write(json.dumps(
            {"schema_version": SCHEMA, "decided_by": ["a", "b"],
             "items": [{"slug": "aaa", "section": "ヤメ時の判断",
                        "source_sha256": "0" * 64,
                        "drop": ["けす行です。"], "why": "言い換え"}]},
            ensure_ascii=False))
        rr3 = run(plan3, apply_it=True)
        t("★★判断したときから記事が変わっていたら触らない★★",
          rr3.get("blocked") is True and rr3["wrote"] == 0
          and "変わっています" in rr3["skipped"][0]["problems"][0])

        # ★正しい指紋なら書ける★
        plan4 = os.path.join(td, "plan4.json")
        io.open(plan4, "w", encoding="utf-8").write(json.dumps(
            {"schema_version": SCHEMA, "decided_by": ["a", "b"],
             "items": [{"slug": "aaa", "section": "ヤメ時の判断",
                        "source_sha256": file_sha256(gp),
                        "drop": ["けす行です。"], "why": "言い換え"}]},
            ensure_ascii=False))
        rr4 = run(plan4, apply_it=True)
        t("　指紋が合っていれば書ける", rr4["wrote"] == 1)
        with io.open(gp, encoding="utf-8") as f:
            t("　消したい行だけが消えている",
              json.load(f)["sections"][0]["body"] == ["のこす行です。"])
    finally:
        globals()["DETAILS"] = _keep_details

    print()
    print(f"{ran[0] - len(ng)}/{ran[0]} " + ("合格" if not ng else "不合格"))
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", help="決定ファイル（JSON）")
    ap.add_argument("--apply", action="store_true", help="実際に書く（既定は見るだけ）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    if not a.file:
        ap.error("--file が要ります")
    try:
        r = run(a.file, a.apply)
    except DedupError as e:
        print(f"★{e}★")
        return 1
    print(("★書きました★" if a.apply else "★見るだけ（--apply で書きます）★")
          + f" 消す行 {r['n_dropped']} 件 / 機種 {len(r['ok'])}"
          + (f" / 書いた {r['wrote']} 機種" if a.apply else ""))
    for o in r["ok"]:
        print(f"  {o['slug']} / {o['section']}: {o['n']}行  … {o['why'][:50]}")
    if r["skipped"]:
        print()
        print(f"★通らなかった機種★ {len(r['skipped'])} 件"
              "（★1件でもあれば何も書きません★）")
        for x in r["skipped"]:
            print(f"  {x['slug']}: " + " / ".join(x["problems"]))
        # ★黙って一部だけ適用しない★（2026-08-21・Codexの指摘）
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

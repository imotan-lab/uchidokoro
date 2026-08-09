# -*- coding: utf-8 -*-
"""2AIで突き合わせて確定した値を、記事の材料として受け取る口。

★なぜ要るか（2026-08-09・台帳#273）★
  機械の抽出は「載っているのに読めない」が普通に起きる。
  実測: パリピ孔明は名鑑4件すべてに天井の記述があるのに、
  4件とも「記述はあるが採れませんでした」で0件だった。
  そのため4夜連続で1件も公開できなかった。

  手順書には2AI突き合わせ（新台=STEP 3-B / 更新=STEP 2〜5）があるのに、
  **そこで確定した値を材料として受け取る場所が無かった**。
  だから機械が読めない機種は、何度回しても永久に空のまま公開され続ける。

★守る線（release_overrides と同じ形）★
  ┌────────────────────────────────────────────────┐
  │ ①2人（ClaudeとCodex）が同じ原文を読んで一致したこと│
  │ ②その根拠（出典URLと逐語の引用）が残っていること   │
  │ ③記録できるのは対話セッションだけ（無人は読むだけ）│
  └────────────────────────────────────────────────┘
  ★出典は独立2系列★＝同じ発行者の2ページは1票（source_lineage で数える）。
  ★値を発明しない★＝引用に現れない値は記録できない（機械が確かめる）。

置き場: C:/Users/imao_/Documents/uchidokoro/confirmed_values.json
        （リポジトリ外・Dropboxへ保全）

使い方:
  # 記録する（対話セッションのみ）
  python scripts/confirmed_values.py --record --slug prskkm --field ceiling \\
      --value-file <値のJSON> --source "p-world|https://…|天井は1000G+α" \\
      --source "nana-press|https://…|通常時1000G+αで天井" \\
      --by claude,codex --why "同じ原文を読んで一致"
  python scripts/confirmed_values.py --list [--slug prskkm]
  python scripts/confirmed_values.py --forget --slug prskkm --field ceiling
  python scripts/confirmed_values.py --selftest
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import safe_json as _sj              # noqa: E402
import source_lineage as _sl         # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE = r"C:/Users/imao_/Documents/uchidokoro/confirmed_values.json"
SCHEMA = "confirmed-values/v1"

# ★2人そろって初めて記録できる★（片方だけの読みは採らない）
REQUIRED_JUDGES = ("claude", "codex")
MIN_QUOTE = 6            # 逐語の引用がこれより短いものは根拠にしない


class ConfirmedError(Exception):
    """確定値に関する異常（★迷ったら記録しない★）。"""


def _empty() -> dict:
    return {"schema_version": SCHEMA, "machines": {}}


def load() -> dict:
    if not os.path.exists(STORE):
        return _empty()
    got = _sj.read_json(STORE, expect=dict)
    if got.get("schema_version") != SCHEMA:
        raise ConfirmedError(f"確定値の形が違います: {got.get('schema_version')}")
    got.setdefault("machines", {})
    return got


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, STORE)


def parse_source(spec: str) -> dict:
    """`発行者|URL|逐語の引用` を組に分ける。"""
    parts = [x.strip() for x in str(spec or "").split("|", 2)]
    if len(parts) != 3 or not all(parts):
        raise ConfirmedError(
            "出典は 発行者|URL|逐語の引用 の形で書きます: " + str(spec)[:60])
    pub, url, quote = parts
    if len(quote) < MIN_QUOTE:
        raise ConfirmedError(f"引用が短すぎます（{MIN_QUOTE}文字以上）: {quote}")
    return {"publisher": pub, "url": url, "quote": quote}


def check_sources(sources: list) -> list:
    """★独立2系列そろっているか★（同じ発行者の2ページは1票）"""
    if len(sources) < 2:
        raise ConfirmedError("出典が2つ要ります（独立した2系列）")
    keys = set()
    for s in sources:
        try:
            keys.add(_sl.vote_key(s["publisher"]))
        except _sl.LineageError as e:
            raise ConfirmedError(str(e))
    if len(keys) < 2:
        raise ConfirmedError(
            "同じ発行者の出典が2つあるだけです（独立した2系列が要ります）: "
            + " / ".join(s["publisher"] for s in sources))
    return sorted(keys)


def record(slug: str, field: str, value, sources: list, by: list,
           why: str) -> dict:
    """★2AIが一致した値だけを残す★（fail-closed）"""
    if not slug or not field:
        raise ConfirmedError("--slug と --field が要ります")
    who = sorted({x.strip() for x in (by or []) if x.strip()})
    for need in REQUIRED_JUDGES:
        if need not in who:
            raise ConfirmedError(
                "2人（%s）がそろって初めて記録できます: いまは %s"
                % ("/".join(REQUIRED_JUDGES), ",".join(who) or "なし"))
    if len(str(why or "").strip()) < 8:
        raise ConfirmedError("--why（どう突き合わせたか）は8文字以上で書きます")
    lineages = check_sources(sources)
    # ★値が引用に現れることを機械が確かめる★（値を発明させない）
    text = " ".join(s["quote"] for s in sources)
    for token in _tokens(value):
        if token not in text:
            raise ConfirmedError(
                f"値『{token}』が出典の引用に現れません（引用にある値だけ記録できます）")
    data = load()
    rec = {
        "value": value,
        "sources": sources,
        "lineages": lineages,
        "agreed_by": who,
        "why": str(why).strip()[:300],
        "decided_at": datetime.date.today().isoformat(),
    }
    data["machines"].setdefault(slug, {})[field] = rec
    _save(data)
    return {"state": "RECORDED", "slug": slug, "field": field,
            "lineages": lineages}


def _tokens(value) -> list:
    """値の中の「引用に現れるべき文字列」を取り出す。"""
    if isinstance(value, dict):
        out = []
        for k, v in value.items():
            if k.startswith("_") or k in ("unit", "note", "benefit", "counted",
                                          "phase", "role", "kind"):
                continue
            out += _tokens(v)
        return out
    if isinstance(value, list):
        return [t for v in value for t in _tokens(v)]
    if isinstance(value, bool) or value is None:
        return []
    return [str(value)]


def forget(slug: str, field: str) -> dict:
    data = load()
    fields = (data.get("machines") or {}).get(slug) or {}
    if field not in fields:
        return {"state": "NOT_FOUND"}
    fields.pop(field)
    if not fields:
        data["machines"].pop(slug, None)
    _save(data)
    return {"state": "FORGOTTEN"}


def for_slug(slug: str, data: dict | None = None) -> dict:
    """機械が毎回読む側（無人タスクはここだけ使う）。"""
    d = data if data is not None else load()
    return dict((d.get("machines") or {}).get(slug) or {})


def merge_into(material: dict, slug: str) -> list:
    """集めた材料に、2AIが確定した値を足す。★足したものの一覧を返す★

    ★機械が採れたものを上書きしない★（機械が採れているなら、それは
      すでに独立2出典で一致したもの。人の記録で塗り替えない）
    """
    added = []
    if not isinstance(material, dict):
        return added
    adopted = material.setdefault("adopted", {})
    for field, rec in for_slug(slug).items():
        if field in adopted:
            continue
        adopted[field] = {
            "value": rec["value"],
            "sources": [s["url"] for s in rec.get("sources") or []],
            # ★どこから来た値かを残す★（あとで追える）
            "_from": "confirmed_values",
            "_agreed_by": rec.get("agreed_by"),
            "_decided_at": rec.get("decided_at"),
        }
        added.append(field)
    return added


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    import tempfile

    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    def stops(name, fn):
        try:
            fn()
            t(name, False)
        except ConfirmedError:
            t(name, True)

    global STORE
    keep = STORE
    STORE = os.path.join(tempfile.mkdtemp(), "confirmed_values.json")
    try:
        S = [parse_source("chonborista|https://chonborista.com/1|天井は1000G+α"),
             parse_source("nana-press|https://nana-press.com/1|通常時1000G+αで天井")]

        stops("★★2人そろわないと記録できない★★（片方だけの読みは採らない）",
              lambda: record("x", "ceiling", "1000", S, ["claude"], "突き合わせました"))
        stops("　どう突き合わせたかを書かないと記録できない",
              lambda: record("x", "ceiling", "1000", S, ["claude", "codex"], "短い"))
        stops("★★出典が1つでは記録できない★★",
              lambda: record("x", "ceiling", "1000", S[:1], ["claude", "codex"],
                             "突き合わせました"))
        same = [parse_source("chonborista|https://chonborista.com/1|天井は1000G+α"),
                parse_source("yancha-press|https://yancha-press.com/1|天井は1000G+α")]
        stops("★★同じ転載系列の2つは1票★★（ちょんぼりすたとやんちゃプレス）",
              lambda: record("x", "ceiling", "1000", same, ["claude", "codex"],
                             "突き合わせました"))
        stops("★★引用に無い値は記録できない★★（値を発明させない）",
              lambda: record("x", "ceiling", "1234", S, ["claude", "codex"],
                             "突き合わせました"))
        stops("　登録されていない発行者は使えない",
              lambda: record("x", "ceiling", "1000",
                             [parse_source("shiranai|https://a.example/1|天井は1000G+α"),
                              S[1]], ["claude", "codex"], "突き合わせました"))

        r = record("x", "ceiling", {"amount": "1000", "unit": "G"}, S,
                   ["claude", "codex"], "同じ原文を読んで一致しました")
        t("　2人が一致し、独立2系列の引用があれば記録できる",
          r["state"] == "RECORDED" and len(r["lineages"]) == 2)
        t("　機械が毎回読む側から取り出せる",
          for_slug("x")["ceiling"]["value"]["amount"] == "1000")

        mat = {"adopted": {}}
        added = merge_into(mat, "x")
        t("★★材料に足される（ここが無かったので永久に空だった）★★",
          added == ["ceiling"]
          and mat["adopted"]["ceiling"]["value"]["amount"] == "1000"
          and mat["adopted"]["ceiling"]["_from"] == "confirmed_values")

        mat2 = {"adopted": {"ceiling": {"value": "機械が採った"}}}
        merge_into(mat2, "x")
        t("★★機械が採れている項目は上書きしない★★",
          mat2["adopted"]["ceiling"]["value"] == "機械が採った")

        t("　間違いは取り消せる", forget("x", "ceiling")["state"] == "FORGOTTEN"
          and for_slug("x") == {})
        t("　無いものを取り消しても壊れない",
          forget("x", "ceiling")["state"] == "NOT_FOUND")
        stops("　出典の書き方が違えば受け取らない",
              lambda: parse_source("URLだけ"))
    finally:
        STORE = keep

    ng = sum(1 for _, o in results if not o)
    print()
    print("%d/%d 合格" % (len(results) - ng, len(results)))
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="2AIで確定した値の受け取り口")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--forget", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--slug", default="")
    ap.add_argument("--field", default="")
    ap.add_argument("--value", default="", help="値（文字列）")
    ap.add_argument("--value-file", dest="value_file", default="",
                    help="値を書いたJSONファイル（構造のある値はこちら）")
    ap.add_argument("--source", action="append", default=[],
                    help="発行者|URL|逐語の引用（2つ以上）")
    ap.add_argument("--by", default="", help="判断した人（claude,codex）")
    ap.add_argument("--why", default="")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()
    try:
        if a.record:
            if a.value_file:
                value = _sj.read_json(a.value_file, expect=(dict, list, str, int, float))
            elif a.value:
                value = a.value
            else:
                print("--value か --value-file が要ります")
                return 2
            r = record(a.slug, a.field, value,
                       [parse_source(s) for s in a.source],
                       [x for x in a.by.split(",") if x.strip()], a.why)
            print(json.dumps(r, ensure_ascii=False))
            return 0
        if a.forget:
            print(json.dumps(forget(a.slug, a.field), ensure_ascii=False))
            return 0
        if a.list:
            data = load()
            for slug, fields in sorted((data.get("machines") or {}).items()):
                if a.slug and slug != a.slug:
                    continue
                print("■ " + slug)
                for f, rec in sorted(fields.items()):
                    print("   %-14s %s" % (f, json.dumps(rec["value"],
                                                         ensure_ascii=False)[:70]))
                    print("      %s ／ %s（%s）"
                          % (rec.get("why"), ",".join(rec.get("agreed_by") or []),
                             rec.get("decided_at")))
                    for s in rec.get("sources") or []:
                        print("      - %s %s" % (s["publisher"], s["url"][:70]))
            return 0
    except ConfirmedError as e:
        print("★" + str(e) + "★")
        return 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

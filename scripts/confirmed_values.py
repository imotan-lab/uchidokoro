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

# ★どの項目を、材料のどこへ入れるか★（2026-08-09・依頼130 P0-1）
#   最初の版は全部を material["adopted"] に入れていたが、
#   記事が天井を読むのは material["ceilings"]["adopted"] で、
#   しかも add_machine_run が spec_lookup.FIELDS を引くため
#   **記録した瞬間に KeyError で落ちた**（実機で確認）。
#   置き場を明示し、知らない項目は受け取らない。
FIELD_TARGETS = {
    "ceiling": "ceilings",      # 天井（1件ずつ）
    "at": "at_specs",           # ATの仕様
    "cz": "czs",                # CZ
}
# 基本スペック側（spec_lookup.FIELDS の鍵）はそのまま adopted へ入る


def allowed_fields() -> dict:
    """受け取ってよい項目 → 入れ先。"""
    import spec_lookup as _sp
    out = {k: "adopted" for k in _sp.FIELDS}
    out.update(FIELD_TARGETS)
    return out


# ★項目ごとに「値の形」を決める★（2026-08-09・依頼131 P0-3）
#   項目名しか見ていなかったので、benefit の無い天井を記録でき、
#   そのあと記事生成が c["benefit"] で落ち続ける状態になっていた。
#   ★引用と照合する表示値★も項目ごとに決める（内部の記号は照合しない）。
VALUE_SHAPES = {
    "ceiling": {"required": ("kind", "amount", "unit", "benefit"),
                "enums": {"kind": ("GAME", "CYCLE", "POINT")},
                "quoted": ("amount", "unit")},
    "at": {"required": ("mode", "games", "net"),
           "enums": {"mode": ("MAIN_AT", "UPPER_AT")},
           "quoted": ("games", "net")},
    "cz": {"required": ("name",), "enums": {}, "quoted": ("name",)},
}


def check_shape(field: str, value) -> list:
    """値の形を確かめ、★引用と照合すべき表示値★を返す。"""
    shape = VALUE_SHAPES.get(field)
    if not shape:
        # 基本スペック側は spec_lookup が形を持っているので、空でないことだけ見る
        toks = _tokens(value)
        if not toks:
            raise ConfirmedError(f"{field}: 確かめられる値がありません")
        return toks
    if not isinstance(value, dict):
        raise ConfirmedError(f"{field}: 値は組（辞書）で書きます")
    for k in shape["required"]:
        if k not in value or str(value[k] or "").strip() == "":
            raise ConfirmedError(f"{field}: 「{k}」が要ります（記事がこれを読みます）")
    for k, ok in shape["enums"].items():
        if value.get(k) not in ok:
            raise ConfirmedError(
                f"{field}: 「{k}」は {'/'.join(ok)} のどれかです（いま {value.get(k)!r}）")
    return [str(value[k]).strip() for k in shape["quoted"]]


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
    """`URL|逐語の引用` を組に分ける。

    ★発行者は名乗らせない★（2026-08-09・依頼130 P0-2）
      以前は `発行者|URL|引用` と自己申告させていたので、
      登録済みの発行者名を**別ホストのURLに付けて**通せた。
      発行者はURLのホストから機械が引く。
    """
    parts = [x.strip() for x in str(spec or "").split("|", 1)]
    if len(parts) != 2 or not all(parts):
        raise ConfirmedError(
            "出典は URL|逐語の引用 の形で書きます: " + str(spec)[:60])
    url, quote = parts
    if len(quote) < MIN_QUOTE:
        raise ConfirmedError(f"引用が短すぎます（{MIN_QUOTE}文字以上）: {quote}")
    import urllib.parse
    host = urllib.parse.urlsplit(url).hostname or ""
    try:
        pub = _sl.publisher_of_host(host)
    except _sl.LineageError as e:
        raise ConfirmedError(str(e))
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


def verify_source(src: dict, name: str, fetch=None) -> dict:
    """★出典のページを実際に取ってきて確かめる★（2026-08-09・依頼130 P0-2）

    以前は URL も引用も**言うだけ**で通った。そのため
    「機種Aについての本物の引用」を機種Bとして記録できた。
    ①そのページが本当にその機種のページか ②引用が本当にそこにあるか
    の2つを機械が確かめる。
    """
    if fetch is None:
        import new_machine_watch as _w

        def fetch(u):
            return _w._get(u)
    import model_code_lookup as _mc
    import new_machine_watch as _w
    try:
        html = fetch(src["url"])
    except Exception as e:                 # noqa: BLE001
        raise ConfirmedError(f"出典を取得できません（{src['url']}）: {str(e)[:80]}")
    ok, why = _mc.page_is_machine(html, name)
    if not ok:
        raise ConfirmedError(
            f"そのページは「{name}」のページだと確かめられません（{why}）: {src['url']}")
    text = " ".join(_w._visible_text(html).split())
    quote = " ".join(str(src["quote"]).split())
    if quote not in text:
        raise ConfirmedError(
            f"引用がそのページに見当たりません（{src['url']}）: {quote[:40]}")
    src["verified_at"] = datetime.date.today().isoformat()
    return src


def bind_machine(official_url: str) -> tuple:
    """公式URLから slug と正式名称を**正本から**引く。

    ★なぜ名前を名乗らせないか（2026-08-09・依頼131 P0-1）★
      `--slug` と `--name` を別々に受け取っていたので、
      **機種Aの本物のURL・引用を、機種Bのslugで記録できた**。
      三層の検査（発行者・ページの本人性・引用の実在）を全部通ってしまう。
      slugも名前も公式URLから導き、人に決めさせない。
    """
    import build_new_article as _ba
    slug = _ba.slug_from_url(official_url)
    if not slug:
        raise ConfirmedError(f"公式URLから機種の名前を作れません: {official_url}")
    # ①待ち行列（まだ登録されていない新台）
    try:
        pend = _sj.read_json(
            r"C:/Users/imao_/Documents/uchidokoro/add_machine_pending.json",
            expect=dict)
        for u, it in (pend.get("items") or {}).items():
            if u.rstrip("/") == str(official_url).rstrip("/"):
                return slug, str(it.get("name") or "")
    except Exception:                      # noqa: BLE001
        pass
    # ②すでに登録されている機種
    try:
        ms = _sj.read_json(os.path.join(BASE, "assets", "data", "machines.json"),
                           expect=(dict, list))
        ms = ms["machines"] if isinstance(ms, dict) else ms
        for m in ms:
            if m.get("slug") == slug:
                return slug, str(m.get("name") or "")
    except Exception:                      # noqa: BLE001
        pass
    raise ConfirmedError(
        f"その公式URLの機種が見つかりません（待ち行列にも一覧にも無い）: {official_url}")


def record(slug: str, field: str, value, sources: list, by: list,
           why: str, name: str = "", fetch=None,
           official_url: str = "") -> dict:
    """★2AIが一致した値だけを残す★（fail-closed）"""
    if not field:
        raise ConfirmedError("--field が要ります")
    if field not in allowed_fields():
        raise ConfirmedError(
            "受け取れない項目です: %s（使えるのは %s）"
            % (field, "/".join(sorted(allowed_fields()))))
    if official_url:
        # ★slugと名前は正本から引く★（人に名乗らせない）
        slug, name = bind_machine(official_url)
    if not slug:
        raise ConfirmedError("--official-url（推奨）か --slug が要ります")
    if not str(name or "").strip():
        raise ConfirmedError(
            "正式名称を決められません。--official-url を使ってください"
            "（slugと名前を正本から引きます＝機種の取り違えを防ぐため）")
    who = sorted({x.strip() for x in (by or []) if x.strip()})
    for need in REQUIRED_JUDGES:
        if need not in who:
            raise ConfirmedError(
                "2人（%s）がそろって初めて記録できます: いまは %s"
                % ("/".join(REQUIRED_JUDGES), ",".join(who) or "なし"))
    if len(str(why or "").strip()) < 8:
        raise ConfirmedError("--why（どう突き合わせたか）は8文字以上で書きます")
    lineages = check_sources(sources)
    # ★出典ごとに、その値を支えていることを確かめる★（2026-08-09・依頼130 P1-1）
    #   以前は全出典の引用をつなげてから探していたので、
    #   **1つの出典にしか無い値でも「2出典一致」として通った**。
    # ★値の形を確かめ、引用と照合する表示値を決める★（依頼131 P0-3・P1）
    #   単位や恩恵まで照合しないと、引用が「1000pt」でも値を「1000G」にできた。
    toks = check_shape(field, value)
    for s in sources:
        q = " ".join(str(s["quote"]).split())
        for token in toks:
            if token not in q:
                raise ConfirmedError(
                    f"値『{token}』が {s['publisher']} の引用にありません"
                    "（★出典ごとに同じ値を支えている必要があります★）")
    # ★引用が本当にそのページにあるか・そのページがその機種かを確かめる★
    sources = [verify_source(dict(s), name, fetch) for s in sources]
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
    ★入れ先を間違えない★（2026-08-09・依頼130 P0-1）
      天井・AT・CZは基本スペックとは別の場所に入る。全部を adopted に
      入れていたので、記事に届かないうえ KeyError で落ちていた。
    """
    added = []
    if not isinstance(material, dict):
        return added
    targets = allowed_fields()
    for field, rec in for_slug(slug).items():
        where = targets.get(field)
        if not where:
            # ★知らない項目は黙って捨てない★
            raise ConfirmedError(f"知らない項目です: {field}")
        stamped = {
            "value": rec["value"],
            "sources": [s["url"] for s in rec.get("sources") or []],
            # ★どこから来た値かを残す★（あとで追える）
            "_from": "confirmed_values",
            "_agreed_by": rec.get("agreed_by"),
            "_decided_at": rec.get("decided_at"),
        }
        if where == "adopted":
            adopted = material.setdefault("adopted", {})
            if field in adopted:
                continue
            adopted[field] = stamped
        else:
            box = material.setdefault(where, {})
            rows = box.setdefault("adopted", [])
            # ★同じ中身が既にあるなら足さない★（機械が採れていれば上書きしない）
            def _core(d):
                # 出所や出典URLは比べない（機械が採った行と形が違うだけで
                # 「別物」と見なして重複して増えていた・依頼131 P1）
                return {k: v for k, v in (d or {}).items()
                        if not k.startswith("_") and k != "sources"}
            if any(_core(r) == _core(rec["value"]) for r in rows):
                continue
            row = dict(rec["value"]) if isinstance(rec["value"], dict) else {
                "value": rec["value"]}
            row["_from"] = "confirmed_values"
            row["sources"] = stamped["sources"]
            rows.append(row)
        added.append(field)
    return added


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    import tempfile

    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("OK " if cond else "NG ") + name)

    def stops(name, fn):
        try:
            fn()
            t(name, False)
        except ConfirmedError:
            t(name, True)

    NAME = "L試験機"
    Q1 = "天井は1000G+α"
    Q2 = "通常時1000G+αで天井"

    def fake_fetch(url):
        q = Q1 if "chonborista" in url else Q2
        return ("<title>" + NAME + " スロット 新台 天井 | 解析</title>"
                "<body><h1>" + NAME + "</h1><p>" + q + "。" + ("説明。" * 30)
                + "</p></body>")

    def rec(**kw):
        base = dict(slug="x", field="ceiling",
                    value={"kind": "GAME", "amount": "1000", "unit": "G",
                           "benefit": "AT"},
                    sources=None, by=["claude", "codex"],
                    why="同じ原文を読んで一致しました", name=NAME,
                    fetch=fake_fetch)
        base.update(kw)
        if base["sources"] is None:
            base["sources"] = [parse_source("https://chonborista.com/1|" + Q1),
                               parse_source("https://nana-press.com/1|" + Q2)]
        return record(**base)

    global STORE
    keep = STORE
    STORE = os.path.join(tempfile.mkdtemp(), "confirmed_values.json")
    try:
        t("★★発行者は名乗らせずURLから引く★★（別ホストに名前を付けて通せた）",
          parse_source("https://chonborista.com/1|" + Q1)["publisher"]
          == "chonborista")
        stops("　登録されていないサイトは使えない",
              lambda: parse_source("https://a.example/1|" + Q1))

        stops("★★2人そろわないと記録できない★★", lambda: rec(by=["claude"]))
        stops("　どう突き合わせたかを書かないと記録できない", lambda: rec(why="短い"))
        stops("★★出典が1つでは記録できない★★",
              lambda: rec(sources=[parse_source("https://chonborista.com/1|" + Q1)]))
        stops("★★同じ転載系列の2つは1票★★",
              lambda: rec(sources=[parse_source("https://chonborista.com/1|" + Q1),
                                   parse_source("https://yancha-press.com/1|" + Q1)]))
        stops("★★引用に無い値は記録できない★★（値を発明させない）",
              lambda: rec(value={"kind": "GAME", "amount": "1234", "unit": "G"}))
        stops("★★出典ごとに同じ値を支えていないと記録できない★★"
              "（つなげて探していたので1出典だけでも通った）",
              lambda: rec(sources=[parse_source("https://chonborista.com/1|" + Q1),
                                   parse_source("https://nana-press.com/1|天井なし")]))
        stops("★★受け取れない項目は断る★★（入れ先が決まっていないもの）",
              lambda: rec(field="なにか"))
        stops("　正式名称が要る（機種の取り違えを防ぐため）", lambda: rec(name=""))

        def other_machine(url):
            return ("<title>別の機種 スロット 新台 | 解析</title>"
                    "<body><h1>別の機種</h1><p>" + Q1 + "。" + ("説明。" * 30)
                    + "</p></body>")
        stops("★★別機種のページの引用は記録できない★★"
              "（本物の引用でも、その機種のページでなければ採らない）",
              lambda: rec(fetch=other_machine))

        def no_quote(url):
            return ("<title>" + NAME + " スロット 新台 | 解析</title>"
                    "<body><h1>" + NAME + "</h1><p>" + ("説明。" * 40)
                    + "</p></body>")
        stops("★★引用がそのページに無ければ記録できない★★（言うだけでは通らない）",
              lambda: rec(fetch=no_quote))

        r = rec()
        t("　2人が一致し、独立2系列の引用が実在すれば記録できる",
          r["state"] == "RECORDED" and len(r["lineages"]) == 2)

        mat = {}
        added = merge_into(mat, "x")
        t("★★天井は ceilings の中へ入る★★（依頼130 P0-1。adopted に入れて落ちていた）",
          added == ["ceiling"]
          and mat["ceilings"]["adopted"][0]["amount"] == "1000"
          and mat["ceilings"]["adopted"][0]["_from"] == "confirmed_values"
          and "ceiling" not in (mat.get("adopted") or {}))

        import spec_lookup as _sp
        t("　基本スペック側の項目は spec_lookup が知っている鍵だけ",
          all(k in _sp.FIELDS for k, v in allowed_fields().items()
              if v == "adopted"))

        mat2 = {"ceilings": {"adopted": [{"kind": "GAME", "amount": "1000",
                                          "unit": "G", "benefit": "AT",
                                          "sources": ["x"]}]}}
        merge_into(mat2, "x")
        t("★★機械が採れている天井は増やさない★★",
          len(mat2["ceilings"]["adopted"]) == 1)

        stops("★★記事が読む項目（恩恵など）が無い値は記録できない★★"
              "（依頼131 P0-3。記録できてしまい、あとで記事生成が落ちていた）",
              lambda: rec(value={"kind": "GAME", "amount": "1000", "unit": "G"}))
        stops("　天井の種類が決まった語でないと記録できない",
              lambda: rec(value={"kind": "ナニカ", "amount": "1000",
                                 "unit": "G", "benefit": "AT"}))
        t("　間違いは取り消せる", forget("x", "ceiling")["state"] == "FORGOTTEN")
        t("　無いものを取り消しても壊れない",
          forget("x", "ceiling")["state"] == "NOT_FOUND")
        stops("　出典の書き方が違えば受け取らない", lambda: parse_source("URLだけ"))
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
    ap.add_argument("--official-url", dest="official_url", default="",
                    help="★推奨★ 公式URL（slugと正式名称を正本から引く）")
    ap.add_argument("--name", default="",
                    help="正式名称（--official-url が使えないときだけ）")
    ap.add_argument("--field", default="")
    ap.add_argument("--value", default="", help="値（文字列）")
    ap.add_argument("--value-file", dest="value_file", default="",
                    help="値を書いたJSONファイル（構造のある値はこちら）")
    ap.add_argument("--source", action="append", default=[],
                    help="URL|逐語の引用（2つ以上・発行者はURLから引く）")
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
                       [x for x in a.by.split(",") if x.strip()], a.why,
                       name=a.name, official_url=a.official_url)
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

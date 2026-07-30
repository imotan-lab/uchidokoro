"""spec_lookup.py — 大手の名鑑2件から記事の材料を引き、一致したものだけ採る。

★運営者の方針（2026-07-31）★
  「P-WORLD と DMMぱちタウンは大手で信用できる。両方が同じならその内容を使う。
    違ったら別サイトも調べて裏取りする」

★それでも形の検査は外さない★
  素朴に見出しの近くの値を拾うと取り違える。実際、最初の実装では
  「出玉率」の欄に `1/498.7`（＝AT確率）を拾ってしまった。
  そこで**項目ごとに期待する単位**を決め、`claim_inventory.normalize_value`
  に通らない値は捨てる。単位が合わない値はそもそも採らない。

★一致の数え方★
  - 同じ運営元は1票（`source-registry.json` の系列で判定）
  - 2件が一致 → 採用
  - 2件が食い違う → **採らずに「第三の出典が要る」として返す**
  - 片方しか取れない → 採らない

使い方:
    python scripts/spec_lookup.py --name "Lすーぱぁびん娘" \\
        --url https://www.p-world.co.jp/machine/database/10496 \\
        --url https://p-town.dmm.com/machines/5038
    python scripts/spec_lookup.py --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import claim_inventory as _ci         # noqa: E402
import model_code_lookup as _mc       # noqa: E402
import new_machine_watch as _w        # noqa: E402
import safe_json as _sj               # noqa: E402

# 取りに行く項目。★項目ごとに期待する単位を決める★
#   単位が合わない値は捨てる（見出しの近くの別の値を拾う事故を防ぐ）
FIELDS = {
    # --- 設定ごとの表（P-WORLDは持っているが、DMMは範囲でしか持っていない）
    "at_prob":      {"labels": ("AT確率", "初当り確率", "初当たり確率",
                                "AT確率・機械割", "AT確率・出玉率"),
                     "unit": "1/x", "kind": "per_setting",
                     "jp": "AT初当たり確率"},
    "payout_rate":  {"labels": ("出玉率", "AT確率・機械割", "AT確率・出玉率"),
                     "unit": "%", "kind": "per_setting", "jp": "出玉率"},
    # --- 1つの値（★両サイトが同じ形で持っているのはこちら★）
    #   実データで確認: P-WORLD「97.3% ~ 112.5%」／DMM「97.3% 〜 112.5%」
    #   波ダッシュの字が違うので、比べる前に形をそろえる。
    "payout_range": {"labels": ("機械割",), "kind": "range", "jp": "機械割の範囲"},
    "model_code":   {"labels": ("型式名",), "kind": "text", "jp": "型式名"},
    # 50枚あたりのゲーム数（両サイトにある・実データで確認）
    #   P-WORLD「50枚あたりのゲーム数 約31G」／ちょんぼりすた「回転数/50枚 → 約31G」
    "games_per_50": {"labels": ("50枚あたりのゲーム数", "回転数/50枚", "50枚あたり"),
                     "kind": "games", "jp": "50枚あたりのゲーム数"},
    # ★条件（どのモードか）を書かないと載せられない項目★
    #   収集器はまだ条件を取れないので、集めても採用はされず保留になる。
    "net_increase": {"labels": ("純増",), "kind": "text", "jp": "純増"},
}

# 範囲の書き方をそろえる（「97.3% ~ 112.5%」も「97.3% 〜 112.5%」も同じ）
_RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*[~〜～\-–—]\s*(\d+(?:\.\d+)?)\s*%")


def normalize_range(raw: str):
    """『97.3% 〜 112.5%』を比べられる形にする。読めなければ None。"""
    m = _RANGE_RE.search(unicodedata.normalize("NFKC", str(raw or "")))
    if not m:
        return None
    lo, hi = float(m.group(1)), float(m.group(2))
    if not (50 <= lo <= hi <= 200):
        return None          # 出玉率としてありえない値は採らない
    return {"low": lo, "high": hi, "unit": "%"}


_GAMES_RE = re.compile(r"約?\s*(\d{1,3}(?:\.\d)?)\s*G")


def normalize_games(raw: str):
    """『約31G』を比べられる形にする。ありえない値は採らない。"""
    m = _GAMES_RE.search(unicodedata.normalize("NFKC", str(raw or "")))
    if not m:
        return None
    v = float(m.group(1))
    if not (5 <= v <= 100):
        return None          # 50枚で5G未満・100G超はありえない
    return {"games": v, "unit": "G"}


def single_value(lines: list, labels: tuple, kind: str):
    """『見出し → 値』の1つ組を読む。見出し行に値が続く形にも対応。"""
    seps = "：:  　"
    for i, line in enumerate(lines):
        lab = next((x for x in labels if line.startswith(x)), None)
        if lab is None:
            continue
        # 見出しの直後は区切りか行末でなければならない（別の語の一部を拾わない）
        rest = line[len(lab):]
        # ★見出しの直後が区切り・空白・行末のいずれか★
        #   `&nbsp;` をほどくと「50枚あたりのゲーム数 約31G」のように
        #   空白1つで値が続く形になる（P-WORLDがこの形）。
        if rest and rest[0] not in seps and not rest[0].isspace():
            continue
        cand = rest.lstrip(seps).strip()
        if not cand and i + 1 < len(lines):
            cand = lines[i + 1].strip()      # 次の行に値がある形（P-WORLD）
        if not cand:
            continue
        if kind == "range":
            v = normalize_range(cand)
            if v:
                return v
        elif kind == "games":
            v = normalize_games(cand)
            if v:
                return v
        elif kind == "text":
            v = unicodedata.normalize("NFKC", cand)
            if _mc._CODE_OK.match(v) and v not in _mc._CODE_NG:
                return v
    return None


# ★出典から値を採るときのルール★（assets/data/collection-rules.json）
#   Codexとのやり取りで出た指摘のうち、他の機種でも効くものを外部ファイルに置く。
#   ★手順書（文章）ではなくここで効かせる理由★
#     文章のルールはAIが読み飛ばせば終わり。コードが読めば必ず効く。
RULES_PATH = os.path.join(BASE, "assets", "data", "collection-rules.json")


def load_rules() -> dict:
    """採取ルールを読む。★読めなければ止める★（ルール無しで採らない）"""
    try:
        return _sj.read_json(RULES_PATH, expect=dict)
    except Exception as e:
        raise RuntimeError(f"採取ルールが読めません: {e} → 値を採りません")


def phrasing_equal(a: str, b: str, rules: dict | None = None) -> bool:
    """2つの書き方を『同じ値』と数えてよいか。

    ★書き方が違うものを一致と数えない★
      「50%以上」と「約50%」は、下限を示す表現と概数で意味の幅が違う。
      文字が違えば元々一致しないが、**将来ゆるい比較を入れたときの歯止め**として
      ここに明示しておく（実際に2026-07-31 に指摘された組合せ）。
    """
    rules = rules or load_rules()
    if a == b:
        return True
    for ex in (rules.get("phrasing_not_equal") or {}).get("examples") or []:
        if {a, b} == {ex.get("a"), ex.get("b")}:
            return False
    return False


def needs_conditions(field_key: str, rules: dict | None = None):
    """その項目は条件を書かないと載せられないか。要るなら何を書くか返す。"""
    rules = rules or load_rules()
    r = (rules.get("conditions_required") or {}).get(field_key)
    return (r or {}).get("must_state") or []


def settings_may_be_non_contiguous(rules: dict | None = None) -> bool:
    """設定が1〜6の連番だと決めつけてよいか。★決めつけない★"""
    rules = rules or load_rules()
    return bool((rules.get("settings_layout") or {}).get("non_contiguous_allowed"))


_SETTING_RE = re.compile(r"^設定\s*([1-6])$")


def _lines(html: str) -> list:
    return [x.strip() for x in _w._visible_text(html).splitlines()]


def per_setting_values(lines: list, labels: tuple, unit: str) -> dict:
    """設定ごとの値が並ぶ表を読む。★単位が合う値だけ採る★

    サイトによって並びが違うので、両方の形に対応する。

      形A（P-WORLD）      形B（ちょんぼりすた）
        AT確率              AT確率・機械割
        設定1                設定 / AT / 出玉率
        1/498.7             設定1
        設定2                1/498.7   ← AT確率
        1/477.8             97.3%     ← 出玉率（同じ設定の行に2つ並ぶ）

    ★どちらの形でも「単位が合う値」だけを採る★
      形Bでは設定1の下に確率と出玉率が並ぶので、
      単位で選り分けないと取り違える（実際に一度やった）。
    """
    best: dict = {}
    for i, line in enumerate(lines):
        if line not in labels:
            continue
        got: dict = {}
        for j in range(i + 1, min(i + 80, len(lines))):
            if lines[j] in labels and j > i + 1:
                break
            m = _SETTING_RE.match(lines[j])
            if not m:
                continue
            # ★設定の行の後ろを数行見て、単位が合う最初の値を採る★
            #   次の「設定N」に当たったらそこで打ち切る。
            for k in range(j + 1, min(j + 5, len(lines))):
                if _SETTING_RE.match(lines[k]):
                    break
                if _ci.normalize_value(lines[k], unit) is not None:
                    got.setdefault(m.group(1), lines[k])
                    break
        if len(got) > len(best):
            best = got
    return best


def read_page(url: str, official_name: str) -> dict:
    """名鑑1件ぶんを読む。★機種が違えば何も採らない★"""
    out = {"url": url, "host": url.split("/")[2].lower().removeprefix("www."),
           "ok": False, "reason": "", "fields": {}}
    try:
        html = _w._get(url)
    except Exception as e:
        out["reason"] = f"取得できません: {e}"
        return out
    ok, why = _mc.page_is_machine(html, official_name)
    if not ok:
        out["reason"] = why
        return out
    lines = _lines(html)
    for key, spec in FIELDS.items():
        if spec["kind"] == "per_setting":
            v = per_setting_values(lines, spec["labels"], spec["unit"])
        else:
            v = single_value(lines, spec["labels"], spec["kind"])
        if v:
            out["fields"][key] = v
    out["ok"] = True
    out["reason"] = "OK"
    return out


def _lineage(host: str) -> str:
    """同じ運営元・同じ転載系列を1票にまとめるための鍵。"""
    try:
        reg = _sj.read_json(os.path.join(BASE, "assets", "data",
                                         "source-registry.json"), expect=dict)
    except Exception:
        return host
    for pid, pub in (reg.get("publishers") or {}).items():
        for h in (pub.get("canonical_hosts") or []):
            if h.lower().removeprefix("www.") == host:
                return pub.get("content_lineage_id") or pid
    return host          # 未登録は他と束ねない（＝1票として扱う）


def compare(pages: list) -> dict:
    """★2件が一致したものだけ採る★ 食い違いは『第三の出典が要る』として返す。

    ★採取ルールを必ず読む★（読めなければ例外で止まる＝ルール無しで採らない）
    """
    rules = load_rules()
    adopted: dict = {}
    need_third: dict = {}
    thin: dict = {}
    usable = [p for p in pages if p["ok"] and p["fields"]]
    for key in FIELDS:
        votes: dict = {}
        for p in usable:
            v = p["fields"].get(key)
            if not v:
                continue
            fp = json.dumps(v, ensure_ascii=False, sort_keys=True)
            votes.setdefault(fp, set()).add(_lineage(p["host"]))
        if not votes:
            continue
        agreed = [(fp, s) for fp, s in votes.items() if len(s) >= 2]
        if len(agreed) == 1:
            must = needs_conditions(key, rules)
            if must:
                # ★条件を書かないと載せられない項目★（純増・継続率・天井など）
                #   いまの収集器は条件を取れないので、採用せず保留にする。
                need_third[key] = {
                    "why": "条件を書かないと載せられない項目です: " + " / ".join(must),
                    "value": json.loads(agreed[0][0])}
                continue
            adopted[key] = {"value": json.loads(agreed[0][0]),
                            "sources": sorted(agreed[0][1])}
        elif len(votes) > 1:
            need_third[key] = {fp[:200]: sorted(s) for fp, s in votes.items()}
        else:
            thin[key] = {"why": "1つの出典しか取れていません",
                         "sources": sorted(next(iter(votes.values())))}
    return {"adopted": adopted, "need_third": need_third, "thin": thin}


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []
    nl = chr(10)

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    L = ["AT確率", "設定1", "1/498.7", "設定2", "1/477.8",
         "出玉率", "設定1", "97.8%", "設定2", "98.5%"]
    t("★項目ごとに正しい表を読む★",
      per_setting_values(L, ("AT確率",), "1/x") == {"1": "1/498.7", "2": "1/477.8"}
      and per_setting_values(L, ("出玉率",), "%") == {"1": "97.8%", "2": "98.5%"})
    t("★★単位が合わない値は採らない★★"
      "（出玉率の欄に確率を拾った実際の事故）",
      per_setting_values(["出玉率", "設定1", "1/498.7"], ("出玉率",), "%") == {})
    t("　設定の行が無ければ何も採らない",
      per_setting_values(["AT確率", "備考", "なし"], ("AT確率",), "1/x") == {})

    t("★★波ダッシュの字が違っても同じ範囲として扱う★★（実データの差）",
      normalize_range("97.3% ~ 112.5%") == normalize_range("97.3% 〜 112.5%")
      == {"low": 97.3, "high": 112.5, "unit": "%"})
    t("★出玉率としてありえない値は採らない★",
      normalize_range("5% 〜 900%") is None and normalize_range("112.5% 〜 97.3%") is None)
    t("　範囲として読めなければ採らない", normalize_range("約2.8枚") is None)
    t("★見出しの行に値が続く形も、次の行にある形も読む★",
      single_value(["機械割  :", "97.3% ~ 112.5%"], ("機械割",), "range")
      == single_value(["機械割：97.3% 〜 112.5%"], ("機械割",), "range"))
    t("　型式名は許可した形だけ採る（説明文を拾わない）",
      single_value(["型式名", "Lびん娘NY1"], ("型式名",), "text") == "Lびん娘NY1"
      and single_value(["型式名", "記載なし"], ("型式名",), "text") is None)

    t("★50枚あたりのゲーム数を読む（両サイトの書き方の差を吸収）★",
      normalize_games("約31G") == normalize_games("31G") == {"games": 31.0, "unit": "G"})
    t("　ありえない値は採らない",
      normalize_games("約3G") is None and normalize_games("約300G") is None)
    t("　G数として読めなければ採らない", normalize_games("約2.8枚") is None)

    A = {"url": "https://www.p-world.co.jp/x", "host": "p-world.co.jp", "ok": True,
         "reason": "OK", "fields": {"payout_rate": {"1": "97.8%"}}}
    B = {"url": "https://p-town.dmm.com/y", "host": "p-town.dmm.com", "ok": True,
         "reason": "OK", "fields": {"payout_rate": {"1": "97.8%"}}}
    C = {"url": "https://p-town.dmm.com/z", "host": "p-town.dmm.com", "ok": True,
         "reason": "OK", "fields": {"payout_rate": {"1": "99.9%"}}}
    r = compare([A, B])
    t("★★2件が一致したら採る★★",
      r["adopted"].get("payout_rate", {}).get("value") == {"1": "97.8%"})
    r2 = compare([A, C])
    t("★★食い違ったら採らず『第三の出典が要る』と返す★★",
      "payout_rate" in r2["need_third"] and not r2["adopted"])
    r3 = compare([A])
    t("　1件だけなら採らない", not r3["adopted"] and "payout_rate" in r3["thin"])
    B2 = {**B, "url": "https://p-world.co.jp/y", "host": "p-world.co.jp"}
    r4 = compare([A, B2])
    t("★同じ運営元の2ページを2票と数えない★", not r4["adopted"])
    r5 = compare([{**A, "ok": False, "fields": {}}, B])
    t("　機種が違うページの内容は混ぜない", not r5["adopted"])

    # -------- 採取ルール（assets/data/collection-rules.json）が実際に効くか
    R = load_rules()
    t("★★ルールが読めなければ値を採らない★★（ルール無しで採らない）",
      isinstance(R, dict) and R.get("schema_version", "").startswith("collection-rules/"))
    t("★★『50%以上』と『約50%』を一致と数えない★★"
      "（下限を示す表現と概数は別物・2026-07-31 Codex指摘）",
      not phrasing_equal("50%以上", "約50%", R)
      and not phrasing_equal("82%以上", "約82%", R))
    t("　同じ書き方どうしは一致とする", phrasing_equal("97.3%", "97.3%", R))
    t("★★条件が要る項目は、2出典一致でも採用しない★★"
      "（純増はどのモードか書かないと誤情報・2026-07-31 Codex指摘）",
      needs_conditions("net_increase", R)
      and needs_conditions("at_continuation_rate", R)
      and needs_conditions("ceiling", R))
    t("　条件の要らない項目は空を返す", needs_conditions("payout_rate", R) == [])
    t("★設定が1〜6の連番だと決めつけない★"
      "（L/1/2/4/5/6 のように飛ぶ機種がある）",
      settings_may_be_non_contiguous(R) is True)

    # ★実際に compare を通したときに効くか★（宣言だけで終わらせない）
    P = {"url": "https://www.p-world.co.jp/x", "host": "p-world.co.jp", "ok": True,
         "reason": "OK", "fields": {"net_increase": "約2.8枚"}}
    Q = {"url": "https://chonborista.com/y", "host": "chonborista.com", "ok": True,
         "reason": "OK", "fields": {"net_increase": "約2.8枚"}}
    _r = compare([P, Q])
    t("★★条件が要る項目は compare でも止まる★★（宣言だけで終わっていない）",
      "net_increase" not in _r["adopted"] and "net_increase" in _r["need_third"])

    ng = [n for n, ok in results if not ok]
    print(f"{nl}{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--name", help="メーカー公式の正式名称")
    ap.add_argument("--url", action="append", help="名鑑ページのURL（2件以上）")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.name or not args.url:
        ap.print_help()
        return 0
    pages = [read_page(u, args.name) for u in args.url]
    for p in pages:
        got = {k: len(v) for k, v in p["fields"].items()}
        print(f"{p['host']:20} {p['reason']:22} {got}")
    r = compare(pages)
    print(chr(10) + json.dumps(r, ensure_ascii=False, indent=1))
    return 0 if r["adopted"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(f"★想定外の失敗 {type(e).__name__}: {e}★")
        raise SystemExit(1)

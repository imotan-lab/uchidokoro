#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""★案件が本当に直ったかを、機械が確かめ直す唯一の場所★（2026-08-20）

## なぜ要るのか

台帳の案件は「無人タスクは閉じない」決まりだった。機械が「直しました」と
自己申告して閉じたら、誤情報が野放しになるからである。
その結果 **人が手を動かすまで永久に減らない**状態になり、
未解決CRITICALのある54機種が更新タスクの対象から外れ続けていた。

★抜け道★＝「直ったか」をAIに**宣言させる**のではなく、
**同じ検査を機械にやり直させる**。再現であって判断ではない。

## ★ここで守る線★（Codex依頼242・243で指摘され、実データで確かめたもの）

1. **終了コード0を「合格」と読まない。** 既存スクリプトの exit 0 は
   「何も言わなかった」であって「案件が直った」ではない（2026-08-20に実測）:
     - `validate_machine_data.py --slug 存在しない機種` → exit 0
     - `risky_atoms.py --slug X`（下見）→ 危ない表現が残っていても exit 0
     - `claim_pipeline.py --slug X` → 台帳で止まっている間は BLOCKED_BY_LEDGER で exit 0
       （★案件があるせいで、その案件を確かめる検査に到達しない＝堂々巡り★）
   だから4値を返す: **PASS / FAIL / ERROR / NOT_APPLICABLE**

2. **★PASS だけでは閉じない★**（依頼243の指摘3）。`closeable()` は
   「どの検査の・どの版で・どの機種の・どの食い違いを・どのコミットで見たか」が
   **呼び出し側の期待と全部一致**したときだけ真を返す。
   PASS という字面だけの辞書では閉じられない。

3. **台帳の値をコマンドに渡さない。** `CHECKS` は固定の名簿、`args_spec` で型と
   列挙を検査、未知キーは拒否。★subprocess を使わない★（2026-08-08のシェル事故の型）

4. **意味の判断をしない。** 読むのは**形の決まったデータ**だけ。
   本文の日本語から数字を読み取ろうとしない（それは2AIの仕事）。
   形が想定と違ったら **NOT_APPLICABLE**（推測で埋めない）

5. **閉じる根拠にできるのは「読者に届いているもの」だけ。**（依頼243の指摘1）
   記事データの `evTable` は**公開ページで使われていない**（ページはチェッカーの
   数値からその場で表を作る／`gates.py` も公開用データから除外している）。
   そういう検査は `closeable=False` にして**観測どまり**にする。

## 使い方

    python scripts/recheck.py --list
    python scripts/recheck.py --check settei_filled --slug hokuto
    python scripts/recheck.py --check settei_filled --all --json
    python scripts/recheck.py --selftest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
DETAILS = os.path.join(BASE, "assets", "data", "machine-details")

PASS = "PASS"
FAIL = "FAIL"
ERROR = "ERROR"
NOT_APPLICABLE = "NOT_APPLICABLE"
RESULTS = (PASS, FAIL, ERROR, NOT_APPLICABLE)

SLUG_RE = re.compile(r"^[a-z0-9_]+$")     # ★形の決まったslugだけ★（パス移動を防ぐ）


# --- 土台 -----------------------------------------------------------------

def _sha(text: str) -> str:
    """★64桁のまま使う★（依頼243の防御3: 切り詰めると衝突耐性を語れない）"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _read_json(path):
    return json.loads(_read_text(path))


def head_commit() -> str:
    """いま見ているコミット。★subprocess を使わずに .git から読む★

    取れなければ空文字（＝閉じる根拠にはできない）。
    """
    try:
        head = _read_text(os.path.join(BASE, ".git", "HEAD")).strip()
        if head.startswith("ref: "):
            ref = head[5:].strip()
            p = os.path.join(BASE, ".git", *ref.split("/"))
            if os.path.exists(p):
                return _read_text(p).strip()
            packed = os.path.join(BASE, ".git", "packed-refs")
            if os.path.exists(packed):
                for line in _read_text(packed).splitlines():
                    if line.endswith(" " + ref):
                        return line.split(" ", 1)[0].strip()
            return ""
        return head if re.fullmatch(r"[0-9a-f]{40}", head) else ""
    except Exception:                                        # noqa: BLE001
        return ""


def _machines():
    data = _read_json(MACHINES)
    if not isinstance(data, list):       # ★machines.json はリスト★
        raise ValueError("machines.json がリストではありません")
    return data


def _machine(slug: str):
    for m in _machines():
        if m.get("slug") == slug:
            return m
    return None


def valid_slug(slug) -> bool:
    """★slugは自己申告させない★ 形が正しく、machines.json に実在するものだけ。"""
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        return False
    return _machine(slug) is not None


def _detail_path(slug: str):
    """★記事ファイルは必ず DETAILS の直下★（`..` などで外へ出させない）"""
    path = os.path.abspath(os.path.join(DETAILS, f"{slug}.json"))
    root = os.path.abspath(DETAILS) + os.sep
    return path if path.startswith(root) else None


def _load_detail(slug: str):
    """記事データを読む。★中身のslugが違えば読まない★（依頼243の指摘6）

    戻り値: (detail, 生テキスト, 理由)。読めないときは (None, None, 理由)。
    """
    path = _detail_path(slug)
    if not path or not os.path.exists(path):
        return None, None, "記事データがありません"
    raw = _read_text(path)
    try:
        detail = json.loads(raw)
    except Exception as e:                                   # noqa: BLE001
        return None, None, f"記事データを読めません: {type(e).__name__}"
    if not isinstance(detail, dict):
        return None, None, "記事データの形が想定外です"
    inner = detail.get("slug")
    if inner != slug:
        # ★別機種の中身が入っているファイルを、その機種の記事として扱わない★
        return None, None, f"記事データの中の機種名が違います（{inner!r}）"
    return detail, raw, ""


def _is_int(v) -> bool:
    """★bool を整数として通さない★（Pythonでは True は 1 と等しい）"""
    return type(v) is int and v >= 0


# --- 検査①: 設定示唆まとめの箱が、中身なしで出ていないか -------------------
#   ★読者に見える★＝machine.html は type:"settei" のとき、
#     見出しとバッジ凡例（弱/中/強/確）を必ず描いてから表を並べる。
#     中身が無いと「見出しと凡例だけ」が残る（台帳#150 の型）。

def check_settei_filled(args: dict) -> dict:
    slug = args.get("slug")
    if not valid_slug(slug):
        return _result(NOT_APPLICABLE, "machines.json にその機種がありません", args)

    detail, raw, why = _load_detail(slug)
    if detail is None:
        return _result(NOT_APPLICABLE, why, args)

    sections = detail.get("sections")
    if not isinstance(sections, list):
        return _result(NOT_APPLICABLE, "記事に本文の箱がありません", args)

    settei = [s for s in sections
              if isinstance(s, dict) and s.get("type") == "settei"]
    if not settei:
        return _result(NOT_APPLICABLE, "設定示唆まとめの箱がありません", args,
                       observed={"boxes": 0})

    empty = []
    for i, s in enumerate(settei):
        tables = s.get("tables")
        rows = s.get("rows")
        filled = False
        if isinstance(tables, list):
            filled = any(isinstance(t, dict) and t.get("rows") for t in tables)
        if not filled and isinstance(rows, list) and rows:
            filled = True
        if not filled:
            empty.append({"index": i, "title": s.get("title")})

    observed = {"boxes": len(settei), "empty": empty,
                "detail_digest": _sha(raw)}
    if empty:
        return _result(FAIL,
                       f"設定示唆まとめの箱が中身なしで出ています（{len(empty)}個）",
                       args, observed=observed)
    return _result(PASS, "設定示唆まとめの箱にはすべて中身があります",
                   args, observed=observed)


# --- 検査②（観測どまり）: 記事の目安表とチェッカーの区切り -------------------
#   ★closeable=False★ 記事の evTable は公開ページで使われていないため、
#     ここで一致していても「読者に正しく届いた」ことにはならない（依頼243の指摘1）。
#     作業用データの手入れの目印として残す。

_RANGE_BOTH = re.compile(r"^(\d+)〜(\d+)G$")
_RANGE_OPEN = re.compile(r"^(\d+)G〜$")


def _evtable_bounds(ev_table):
    """目安表の区切りを取り出す。★形が少しでも崩れていれば None★

    ①各行が「a〜bG」（最後だけ「aG〜」）②最初が0から ③昇順
    ④前の段の終わりと次の段の始まりが連続している ⑤範囲が逆転していない
    （依頼243の指摘5: 開始値だけ見ると、重なった表や逆転した表でも通ってしまう）
    """
    if not isinstance(ev_table, list) or len(ev_table) < 2:
        return None
    starts, ends = [], []
    for i, row in enumerate(ev_table):
        if not isinstance(row, dict):
            return None
        rng = row.get("range")
        if not isinstance(rng, str):
            return None
        both = _RANGE_BOTH.match(rng)
        openx = _RANGE_OPEN.match(rng)
        if both:
            a, b = int(both.group(1)), int(both.group(2))
            if a >= b:
                return None                  # 逆転・つぶれ
            starts.append(a)
            ends.append(b)
        elif openx:
            if i != len(ev_table) - 1:
                return None                  # 「N G〜」は最後の段だけ
            starts.append(int(openx.group(1)))
            ends.append(None)
        else:
            return None
    if starts[0] != 0:
        return None
    for i in range(1, len(starts)):
        if starts[i] <= starts[i - 1]:
            return None                      # 昇順でない
        prev_end = ends[i - 1]
        if prev_end is None:
            return None
        # ★連続しているか★（「0〜300G」の次は「300〜」か「301〜」だけ許す）
        if starts[i] not in (prev_end, prev_end + 1):
            return None
    return starts[1:]


def _rate_thresholds(mode_conf, rate_key, rate_was_explicit):
    """その交換率の閾値 [caution, good, excellent]。取れなければ None。

    ★明示された交換率が byRate に無ければ、基準値へ落とさない★
    （依頼243の指摘7: 落とすと、たまたま一致したときに通ってしまう）
    """
    if not isinstance(mode_conf, dict):
        return None
    by_rate = mode_conf.get("byRate")
    if isinstance(by_rate, dict) and rate_key in by_rate:
        src = by_rate[rate_key]
    elif rate_was_explicit:
        return None
    else:
        src = mode_conf
    if not isinstance(src, dict):
        return None
    out = []
    for key in ("caution", "good", "excellent"):
        v = src.get(key)
        if not _is_int(v):
            return None
        out.append(v)
    if not (out[0] <= out[1] <= out[2]):     # ★順序が壊れていたら読まない★
        return None
    return out


def check_evtable_vs_checker(args: dict) -> dict:
    slug = args.get("slug")
    if not valid_slug(slug):
        return _result(NOT_APPLICABLE, "machines.json にその機種がありません", args)

    m = _machine(slug)
    checker = m.get("checker")
    if not isinstance(checker, dict):
        return _result(NOT_APPLICABLE, "狙い目チェッカーの設定がありません", args)
    if checker.get("unit") != "G":
        return _result(NOT_APPLICABLE, "G数以外の単位の機種は対象外です", args)

    detail, raw, why = _load_detail(slug)
    if detail is None:
        return _result(NOT_APPLICABLE, why, args)
    ev = detail.get("evTable")
    if ev is None:
        return _result(NOT_APPLICABLE, "記事に目安表がありません", args)

    bounds = _evtable_bounds(ev)
    if bounds is None:
        return _result(NOT_APPLICABLE, "目安表の書き方が想定外です（読みません）", args)

    mode_key = "normal" if isinstance(checker.get("normal"), dict) else None
    if mode_key is None:
        modes = checker.get("modes")
        if isinstance(modes, list) and modes and isinstance(modes[0], dict):
            mode_key = modes[0].get("key")
    if not mode_key or not isinstance(checker.get(mode_key), dict):
        return _result(NOT_APPLICABLE, "基準にするモードが決まりません", args)

    rate_was_explicit = "rate" in args
    rate_key = args.get("rate") or checker.get("defaultRate")
    if not isinstance(rate_key, str) or not rate_key:
        return _result(NOT_APPLICABLE, "既定の交換率が決まりません", args)

    th = _rate_thresholds(checker[mode_key], rate_key, rate_was_explicit)
    if th is None:
        return _result(NOT_APPLICABLE, "その交換率の閾値が取れません", args)

    observed = {"evtable": bounds, "checker": th, "mode": mode_key,
                "rate": rate_key, "detail_digest": _sha(raw)}
    if len(bounds) != len(th):
        # ★段数が違うのは「食い違い」ではなく「対応が決められない」★
        #   （依頼243の指摘2: okidoki_encore の追加段はゾーンで、閾値とは別物）
        return _result(NOT_APPLICABLE,
                       f"段の数が違うので対応を決められません"
                       f"（記事{len(bounds)}個 / チェッカー{len(th)}個）",
                       args, observed=observed)

    diff = [(i, b, t) for i, (b, t) in enumerate(zip(bounds, th)) if b != t]
    if diff:
        where = " / ".join(f"{i + 1}段目 記事{b}G↔チェッカー{t}G" for i, b, t in diff)
        return _result(FAIL, f"目安表とチェッカーの区切りが食い違います: {where}",
                       args, observed=observed)
    return _result(PASS, "目安表とチェッカーの区切りは一致しています",
                   args, observed=observed)


# --- 結果の作り方 ---------------------------------------------------------

def _result(result: str, detail: str, args: dict, observed=None) -> dict:
    """★あとから「同じものを見たか」を確かめられる形で返す★

    - finding_key: 何を見た検査かを表す安定した名前（結果が変わっても同じ）
    - observation_digest: いま読んだ中身の指紋（1文字でも変われば変わる）
    - commit_sha: どのコミットで見たか
    """
    if result not in RESULTS:
        raise ValueError(f"知らない結果です: {result}")
    obs = observed or {}
    key_src = json.dumps({"args": args}, ensure_ascii=False, sort_keys=True)
    obs_src = json.dumps({"args": args, "observed": obs, "result": result,
                          "detail": detail}, ensure_ascii=False, sort_keys=True)
    return {
        "result": result,
        "detail": detail,
        "observed": obs,
        "args": dict(args),
        "finding_key": _sha(key_src),
        "observation_digest": _sha(obs_src),
        "commit_sha": head_commit(),
    }


# --- 検査の名簿 -----------------------------------------------------------
# ★ここに無い名前は動かない★（台帳から来た文字列でコマンドを組み立てない）

CHECKS = {
    "settei_filled": {
        "version": 1,
        "closeable": True,          # ★読者に見える★ 見出しと凡例だけが残る型
        "title": "設定示唆まとめの箱が中身なしで出ていないか",
        "fn": check_settei_filled,
        "args_spec": {"slug": (str, True, None)},
    },
    "evtable_vs_checker": {
        "version": 2,
        "closeable": False,         # ★観測どまり★ evTable は公開ページで使われない
        "title": "記事の目安表とチェッカーの区切り（作業用データ・公開されない）",
        "fn": check_evtable_vs_checker,
        "args_spec": {
            "slug": (str, True, None),
            "rate": (str, False, ("eq56", "rate55", "rate50", "rate45")),
        },
    },
}


def validate_args(check: str, args) -> str:
    """★未知のキー・型違い・列挙外は拒否★ 問題なければ空文字。"""
    if check not in CHECKS:
        return f"知らない検査です: {check}"
    if not isinstance(args, dict):
        return "args が辞書ではありません"
    spec = CHECKS[check]["args_spec"]
    for key in args:
        if key not in spec:
            return f"知らない引数です: {key}"
    for key, (typ, required, allowed) in spec.items():
        if key not in args:
            if required:
                return f"引数が足りません: {key}"
            continue
        val = args[key]
        if type(val) is not typ:                 # ★bool を str/int と混同しない★
            return f"引数の型が違います: {key}"
        if allowed is not None and val not in allowed:
            return f"引数の値が許されていません: {key}={val}"
    return ""


def run(check: str, args: dict) -> dict:
    """検査を1つ動かす。★例外は ERROR にして返す（落ちない・閉じない）★"""
    why = validate_args(check, args)
    meta = CHECKS.get(check, {})
    if why:
        return {"check": check, "version": meta.get("version"),
                "closeable_check": bool(meta.get("closeable")),
                "result": ERROR, "detail": why, "observed": {}, "args": {},
                "finding_key": "", "observation_digest": "", "commit_sha": ""}
    try:
        out = meta["fn"](args)
    except Exception as e:                                   # noqa: BLE001
        return {"check": check, "version": meta["version"],
                "closeable_check": bool(meta.get("closeable")),
                "result": ERROR, "detail": f"{type(e).__name__}: {e}",
                "observed": {}, "args": dict(args),
                "finding_key": "", "observation_digest": "", "commit_sha": ""}
    out["check"] = check
    out["version"] = meta["version"]
    out["closeable_check"] = bool(meta.get("closeable"))
    return out


def closeable(res, expect=None) -> bool:
    """★この結果で台帳を閉じてよいか★（依頼243の指摘3）

    PASS という字面だけでは足りない。呼び出し側が
    「どの検査の・どの版で・どの食い違いを・どのコミットで見たか」を
    `expect` で示し、**全部一致したときだけ**真を返す。

    expect に要るもの:
      check / version / finding_key / commit_sha
      （任意で observation_digest。渡せば中身が変わっていないことも見る）
    """
    if not isinstance(res, dict) or not isinstance(expect, dict):
        return False
    if res.get("result") != PASS:
        return False
    if not res.get("closeable_check"):
        return False                      # ★観測どまりの検査では閉じない★
    for key in ("check", "version", "finding_key", "commit_sha"):
        want = expect.get(key)
        if want in (None, "", 0):
            return False                  # ★示されていない項目があれば閉じない★
        if res.get(key) != want:
            return False
    if not re.fullmatch(r"[0-9a-f]{40}", str(res.get("commit_sha") or "")):
        return False                      # ★どのコミットか分からなければ閉じない★
    want_obs = expect.get("observation_digest")
    if want_obs is not None and res.get("observation_digest") != want_obs:
        return False
    return True


# --- CLI ------------------------------------------------------------------
# ★終了コードは「運用が失敗したか」だけを表す★（判定の結果を終了コードで語らない）
#   0 = 検査を最後まで動かせた / 2 = 使い方が違う / 3 = 検査が ERROR を返した

def _cmd_list():
    for name, c in sorted(CHECKS.items()):
        mark = "閉じられる" if c["closeable"] else "★観測どまり（閉じない）★"
        print(f"{name}  v{c['version']}  [{mark}]  {c['title']}")
        for key, (typ, required, allowed) in c["args_spec"].items():
            extra = f" 値={allowed}" if allowed else ""
            print(f"    {key}: {typ.__name__} ({'必須' if required else '任意'}){extra}")
    return 0


def _cmd_run(check, slug, rate, run_all, as_json):
    slugs = [m.get("slug") for m in _machines() if m.get("slug")] if run_all else [slug]
    rows = []
    for s in slugs:
        args = {"slug": s}
        if rate:
            args["rate"] = rate
        rows.append(run(check, args))

    tally = {r: 0 for r in RESULTS}
    for r in rows:
        tally[r["result"]] = tally.get(r["result"], 0) + 1

    if as_json:
        print(json.dumps({"check": check,
                          "total": len(rows),
                          "commit_sha": head_commit(),
                          "tally": tally,
                          "results": rows}, ensure_ascii=False, indent=1))
    else:
        for r in rows:
            if run_all and r["result"] == PASS:
                continue
            print(f"[{r['result']}] {r['args'].get('slug')}: {r['detail']}")
        print()
        print(f"total={len(rows)}  " + "  ".join(f"{k}={v}" for k, v in tally.items() if v))
    return 3 if tally.get(ERROR) else 0


def _selftest():
    ok = 0
    total = 0

    def t(name, cond):
        nonlocal ok, total
        total += 1
        if cond:
            ok += 1
        print(("OK   " if cond else "NG   ") + name)

    # --- 目安表の読み取り（形が崩れていれば読まない）
    good = [{"range": "0〜300G"}, {"range": "300〜449G"},
            {"range": "450〜999G"}, {"range": "1000G〜"}]
    t("ふつうの目安表から区切りを取れる", _evtable_bounds(good) == [300, 450, 1000])
    t("★書き方が想定外なら読まない★",
      _evtable_bounds([{"range": "0-300G"}, {"range": "300G以上"}]) is None)
    t("★0から始まらない表は読まない★",
      _evtable_bounds([{"range": "100〜300G"}, {"range": "300G〜"}]) is None)
    t("★開いた段が途中にあれば読まない★",
      _evtable_bounds([{"range": "0G〜"}, {"range": "300〜400G"}]) is None)
    t("★範囲が逆転していれば読まない★",
      _evtable_bounds([{"range": "0〜300G"}, {"range": "300〜10G"}, {"range": "450G〜"}]) is None)
    t("★段が重なっていれば読まない★",
      _evtable_bounds([{"range": "0〜999G"}, {"range": "300〜449G"}, {"range": "450G〜"}]) is None)
    t("★段が飛んでいれば読まない★",
      _evtable_bounds([{"range": "0〜300G"}, {"range": "400〜449G"}, {"range": "450G〜"}]) is None)
    t("表が短すぎれば読まない", _evtable_bounds([{"range": "0〜300G"}]) is None)

    # --- 引数の検査
    t("★知らない検査は拒否★", validate_args("nope", {"slug": "x"}) != "")
    t("★知らない引数は拒否★",
      validate_args("evtable_vs_checker", {"slug": "revengers", "z": 1}) != "")
    t("★列挙にない交換率は拒否★",
      validate_args("evtable_vs_checker", {"slug": "revengers", "rate": "zzz"}) != "")
    t("★型が違えば拒否★", validate_args("evtable_vs_checker", {"slug": 3}) != "")
    t("必須が無ければ拒否", validate_args("evtable_vs_checker", {}) != "")

    # --- slug の扱い（形とパス）
    t("★変な形のslugは通さない★", not valid_slug("../secrets"))
    t("★空のslugは通さない★", not valid_slug(""))
    t("★記事ファイルは置き場の外へ出ない★", _detail_path("../../x") is None)

    # --- 閾値の読み方
    t("★true/false を数値として通さない★",
      _rate_thresholds({"caution": True, "good": 2, "excellent": 3}, "eq56", False) is None)
    t("★順序が壊れていたら読まない★",
      _rate_thresholds({"caution": 500, "good": 200, "excellent": 900}, "eq56", False) is None)
    t("★明示した交換率が無ければ基準値へ落とさない★",
      _rate_thresholds({"caution": 1, "good": 2, "excellent": 3,
                        "byRate": {"rate55": {}}}, "eq56", True) is None)
    t("明示しなければ基準値でよい",
      _rate_thresholds({"caution": 1, "good": 2, "excellent": 3}, "eq56", False) == [1, 2, 3])

    # --- 結果の扱い（閉じてよいかの判定）
    real = run("settei_filled", {"slug": _machines()[0]["slug"]})
    exp = {k: real.get(k) for k in ("check", "version", "finding_key", "commit_sha")}
    t("★合った期待とPASSがそろえば閉じられる★",
      (real["result"] != PASS) or closeable(real, exp))
    t("★期待を渡さなければ閉じない★", not closeable(real, None))
    t("★別の検査の期待では閉じない★",
      not closeable(real, {**exp, "check": "evtable_vs_checker"}))
    t("★別の食い違いの期待では閉じない★",
      not closeable(real, {**exp, "finding_key": "x" * 64}))
    t("★別のコミットの期待では閉じない★",
      not closeable(real, {**exp, "commit_sha": "0" * 40}))
    t("★版が違えば閉じない★", not closeable(real, {**exp, "version": 99}))
    t("★中身が変わっていれば閉じない★",
      not closeable(real, {**exp, "observation_digest": "z" * 64}))
    t("★手で作ったPASSの辞書では閉じない★",
      not closeable({"result": PASS, "closeable_check": True}, exp))
    t("★FAIL では閉じない★", not closeable({**real, "result": FAIL}, exp))
    t("★ERROR では閉じない★", not closeable({**real, "result": ERROR}, exp))
    t("★対象外では閉じない★", not closeable({**real, "result": NOT_APPLICABLE}, exp))

    # --- 観測どまりの検査では閉じない
    ev = run("evtable_vs_checker", {"slug": "hokuto"})
    ev_exp = {k: ev.get(k) for k in ("check", "version", "finding_key", "commit_sha")}
    t("★観測どまりの検査は、PASSでも閉じられない★", not closeable(ev, ev_exp))

    # --- 存在しない機種・別機種の中身
    r = run("settei_filled", {"slug": "zzz_not_exist"})
    t("★存在しない機種は PASS にならない★", r["result"] == NOT_APPLICABLE)
    t("★存在しない機種では閉じられない★", not closeable(r, exp))

    # --- 例外は ERROR（落ちない）
    keep = CHECKS["settei_filled"]["fn"]
    try:
        def boom(_args):
            raise RuntimeError("わざと壊す")
        CHECKS["settei_filled"]["fn"] = boom
        r = run("settei_filled", {"slug": _machines()[0]["slug"]})
        t("★中で落ちても ERROR で返る★", r["result"] == ERROR)
        t("★落ちた検査では閉じられない★", not closeable(r, exp))
    finally:
        CHECKS["settei_filled"]["fn"] = keep

    # --- 指紋
    a = _result(FAIL, "x", {"slug": "a"}, observed={"n": 1})
    b = _result(FAIL, "x", {"slug": "a"}, observed={"n": 2})
    t("★見たものが違えば指紋も違う★",
      a["observation_digest"] != b["observation_digest"])
    t("同じ食い違いなら名前は同じ", a["finding_key"] == b["finding_key"])
    t("★指紋は切り詰めない（64桁）★", len(a["observation_digest"]) == 64)
    t("コミットが分かる", re.fullmatch(r"[0-9a-f]{40}", a["commit_sha"] or "") is not None)

    print()
    print(f"{ok}/{total} 合格")
    return 0 if ok == total else 1


def main():
    ap = argparse.ArgumentParser(description="案件が本当に直ったかを機械が確かめ直す")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--check")
    ap.add_argument("--slug")
    ap.add_argument("--rate")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return _selftest()
    if a.list or not a.check:
        return _cmd_list()
    if not a.slug and not a.all:
        print("--slug か --all が要ります")
        return 2
    return _cmd_run(a.check, a.slug, a.rate, a.all, a.json)


if __name__ == "__main__":
    sys.exit(main())

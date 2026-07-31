"""add_machine_run.py — 新台追加タスクの本体（部品を1本につなぐ）。

★これ1つで通る★
  メーカー公式の一覧を見る → 新台を見つける → 名鑑のURLを名前から探す
  → 型式名を確定 → 記事の材料を集める → 記事データを組み立てる

★止まる所は必ず理由を残す★
  「新台なし」で静かに終わるのが一番こわいので、
  取れなかった・決められなかったときは要確認台帳に残す。

★既定は dry-run★
  `--apply` を付けたときだけ書き込む。書き込む前に
  `task_guard`（1日1機種）と `task_lock`（ロック）を必ず通す。

使い方:
    python scripts/add_machine_run.py                 # 見るだけ
    python scripts/add_machine_run.py --apply --ctx <CTXパス>
    python scripts/add_machine_run.py --name "Lすーぱぁびん娘" \\
        --official-url https://... --maker bellco --release 2026-08   # 1機種だけ試す
    python scripts/add_machine_run.py --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import build_new_article as _ba       # noqa: E402
import at_spec_lookup as _at        # noqa: E402
import ceiling_lookup as _cl         # noqa: E402
import cz_lookup as _cz              # noqa: E402
import directory_index as _di         # noqa: E402
import lineage_check as _lc          # noqa: E402
import model_code_lookup as _mc       # noqa: E402
import new_machine_watch as _nw       # noqa: E402
import safe_json as _sj               # noqa: E402
import spec_lookup as _sl             # noqa: E402


def _ledger(slug, kind, severity, code, title, detail) -> None:
    """要確認台帳に残す。★止まった理由を必ず残すため★"""
    subprocess.run(
        [sys.executable, os.path.join(BASE, "scripts", "open_issues.py"), "add",
         "--source", "add-machine", "--slug", slug, "--kind", kind,
         "--severity", severity, "--reason-code", code,
         "--title", title, "--detail", detail],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})


def discover() -> dict:
    """メーカー公式の一覧から新台候補を出す。"""
    cats = _sj.read_json(_nw.CATALOGS, expect=dict)["catalogs"]
    seen = _nw._load_seen()
    out = {"candidates": [], "problems": [], "first_time": []}
    for mid, conf in cats.items():
        if conf.get("status") != "ACTIVE":
            continue
        r = _nw.scan_maker(mid, conf, seen)
        if r["problem"]:
            out["problems"].append(f"{mid}: {r['problem']}")
            continue
        if r["first_time"]:
            out["first_time"].append(f"{mid}（{r['total']}件を記録）")
            continue
        for url in r["new"]:
            c = _nw.classify(url, None)
            if c["ok"]:
                out["candidates"].append({"maker": mid, **c})
            else:
                out["problems"].append(f"{url}: " + " / ".join(c["reasons"]))
    _nw._save_seen(seen)
    return out


def gather(name: str) -> dict:
    """1機種ぶんの材料を集める。★止まった理由も返す★"""
    got = {"name": name, "urls": [], "model_code": None, "material": None,
           "problems": []}
    fr = _di.find(name)
    for did, v in fr["results"].items():
        if v["state"] != "FOUND":
            got["problems"].append(f"{did}: {v['state']} {v['why']}"[:160])
    got["urls"] = _di.found_urls(fr)
    if len(got["urls"]) < 2:
        got["problems"].append(
            f"名鑑の個別ページが {len(got['urls'])} 件しか見つかりません（2件以上が要る）")
        return got
    # ★出典どうしが転載でないか確かめる★（2026-07-31・実際に見つけた）
    #   やんちゃプレスはちょんぼりすたと本文が17行そのまま同じだった。
    #   登録簿に無い転載を2票に数えると、独立2出典の意味が無くなる。
    lin = _lc.check(got["urls"])
    for sp in lin["suspects"]:
        got["problems"].append(
            f"転載の疑い: {sp['a']} と {sp['b']} の本文が {sp['ratio']:.0%} 一致"
            f"（登録簿に系列が書かれていません）")
    mv = _mc.agree([_mc.lookup(u, name) for u in got["urls"]])
    got["model_code"] = mv.get("model_code")
    if not mv["adopted"]:
        got["problems"].append("型式名: " + str(mv.get("why", ""))[:160])
    got["material"] = _sl.compare([_sl.read_page(u, name) for u in got["urls"]])
    # ★天井は一式で採る★（値だけ先に載せない）
    got["material"]["ceilings"] = _cl.compare(
        [_cl.read_page(u, name) for u in got["urls"]])
    for nt in got["material"]["ceilings"]["need_third"]:
        got["problems"].append(f"{nt['jp']}: {nt['why']}")
    # ★ATの仕様はモードごとに★（純増を混ぜたら誤情報）
    got["material"]["at_specs"] = _at.compare(
        [_at.read_page(u, name) for u in got["urls"]])
    # ★CZは名前ごとに★（どのCZの期待度か分からないと誤情報）
    got["material"]["czs"] = _cz.compare([_cz.read_page(u, name) for u in got["urls"]])
    for nt in got["material"]["czs"]["need_third"]:
        got["problems"].append(f"CZ「{nt['name']}」: {nt['why']}")
    for nt in got["material"]["at_specs"]["need_third"]:
        jp = "メインAT" if nt["mode"] == "MAIN_AT" else "上位AT"
        got["problems"].append(f"{jp}の仕様: {nt['why']}")
    return got


# ★書き込みを止める理由★（Codex指摘3・自分で再現を確認）
#   以前は problems を文字列で並べるだけで、**中身を見ずに書き込めた**。
#   機種の同定に関わる問題が1つでもあれば、材料が採れていても書かない。
BLOCKING = ("AMBIGUOUS_CANDIDATES", "CATALOG_UNHEALTHY", "型式名",
            "公式ページと名前が一致しません", "2件以上",
            "転載の疑い")   # ★登録簿に無い転載があれば止める★


def _blocking(problems: list) -> list:
    return [p for p in problems if any(w in p for w in BLOCKING)]


def verify_official(name: str, official_url: str) -> list:
    """★公式ページが本当にその機種か確かめる★（Codex指摘1・実際に再現した穴）

    以前は名前とURLを別々に受け取り、照合していなかった。
    そのため「機種Aの名前 ＋ 機種Bの公式URL」で、
    **中身が別機種の記事**を作れてしまった（実際に再現）。
    """
    try:
        html = _nw._get(official_url)
    except Exception as e:
        return [f"公式ページを取得できません: {e}"]
    ok, why = _mc.page_is_machine(html, name)
    if not ok:
        return [f"公式ページと名前が一致しません（{why}）: "
                f"公式のタイトル={_nw.page_title(html)[:40]!r} / 指定名={name!r}"]
    return []


def run_one(name, official_url, maker, release, apply_it=False) -> dict:
    """1機種を最後まで進める。"""
    out = {"name": name, "slug": None, "wrote": [], "problems": [], "blocked": []}
    # ★①まず公式ページと名前が同じ機種を指しているか★
    out["problems"] += verify_official(name, official_url)
    got = gather(name)
    out["problems"] += got["problems"]
    if not got["material"]:
        out["blocked"] = _blocking(out["problems"])
        return out
    out["slug"] = _ba.slug_from_url(official_url)
    mat = got["material"]
    out["adopted"] = sorted(_sl.FIELDS[k]["jp"] for k in mat["adopted"])
    out["held"] = sorted(_sl.FIELDS[k]["jp"] for k in mat["need_third"])
    out["thin"] = sorted(_sl.FIELDS[k]["jp"] for k in mat["thin"])
    if not mat["adopted"]:
        out["problems"].append("採用できた材料がありません（記事を作りません）")
    # ★②同定に関わる問題があれば、材料が採れていても作らない★
    out["blocked"] = _blocking(out["problems"])
    if out["blocked"] or not mat["adopted"]:
        return out
    machine = _ba.build_machine(out["slug"], name, maker, official_url, release, mat)
    detail = _ba.build_detail(out["slug"], name, release, mat)
    out["preview"] = {"machine": machine, "detail": detail}
    if apply_it:
        out["wrote"] = _ba.apply(out["slug"], machine, detail)
    return out


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []
    nl = chr(10)

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    real_find, real_read, real_lookup = _di.find, _sl.read_page, _mc.lookup
    try:
        _di.find = lambda n, c=None: {"results": {
            "a": {"state": "FOUND", "url": "https://a.example/1", "why": "",
                  "candidates": [], "surfaces": "1/1", "index_size": 9, "problems": []},
            "b": {"state": "HEALTHY_NO_MATCH", "url": None, "why": "載っていません",
                  "candidates": [], "surfaces": "1/1", "index_size": 9, "problems": []},
        }}
        g = gather("X")
        t("★見つからない名鑑があっても、理由を残して進む★",
          len(g["urls"]) == 1 and any("HEALTHY_NO_MATCH" in p for p in g["problems"]))
        t("★★名鑑が1件だけなら材料を集めに行かない★★（2件以上が要る）",
          g["material"] is None
          and any("2件以上" in p for p in g["problems"]))

        _di.find = lambda n, c=None: {"results": {
            k: {"state": "FOUND", "url": f"https://{k}.example/1", "why": "",
                "candidates": [], "surfaces": "1/1", "index_size": 9, "problems": []}
            for k in ("a", "b")}}
        _mc.lookup = lambda u, n: {"url": u, "model_code": "C1", "reason": "OK"}
        _sl.read_page = lambda u, n: {
            "url": u, "host": u.split("/")[2], "ok": True, "reason": "OK",
            "fields": {"payout_rate": {"1": "97.3%"}}}
        g2 = gather("X")
        t("　2件そろえば型式名と材料を集める",
          g2["model_code"] == "C1" and g2["material"] is not None)

        r = run_one("X", "https://m.example/products/slot/zzz/", "m", "2026-09")
        t("★既定では書き込まない（dry-run）★", r["wrote"] == [])
        t("　組み立てた結果を返す（中身を見てから書ける）",
          r["preview"]["machine"]["status"] == "preview")
        t("　slugは公式URLから作る", r["slug"] == "zzz")

        _sl.read_page = lambda u, n: {"url": u, "host": u.split("/")[2], "ok": True,
                                      "reason": "OK", "fields": {}}
        r2 = run_one("X", "https://m.example/products/slot/zzz/", "m", "2026-09")
        t("★★材料がゼロなら記事を作らない★★",
          "preview" not in r2 and any("採用できた材料" in p for p in r2["problems"]))

        # -------- Codexの反例（2026-07-31・自分で再現を確認してから修正）
        _sl.read_page = lambda u, n: {
            "url": u, "host": u.split("/")[2], "ok": True, "reason": "OK",
            "fields": {"payout_rate": {"1": "97.3%"}}}
        _mc.lookup = lambda u, n: {"url": u, "model_code": None,
                                   "reason": "MODEL_CODE_NOT_FOUND"}
        r3 = run_one("X", "https://m.example/products/slot/zzz/", "m", "2026-09")
        t("★★型式名が確定していなければ記事を作らない★★"
          "（材料が採れていても作れてしまう穴があった）",
          "preview" not in r3 and any("型式名" in x for x in r3["blocked"]))

        _mc.lookup = lambda u, n: {"url": u, "model_code": "C1", "reason": "OK"}
        _di.find = lambda n, c=None: {"results": {
            "a": {"state": "FOUND", "url": "https://a.example/1", "why": "",
                  "candidates": [], "surfaces": "1/1", "index_size": 9, "problems": []},
            "b": {"state": "FOUND", "url": "https://b.example/1", "why": "",
                  "candidates": [], "surfaces": "1/1", "index_size": 9, "problems": []},
            "c": {"state": "AMBIGUOUS_CANDIDATES", "url": None, "why": "候補が3件",
                  "candidates": [1, 2, 3], "surfaces": "1/1", "index_size": 9,
                  "problems": []}}}
        r4 = run_one("X", "https://m.example/products/slot/zzz/", "m", "2026-09")
        t("★★1つでも候補を絞れない名鑑があれば記事を作らない★★",
          "preview" not in r4
          and any("AMBIGUOUS" in x for x in r4["blocked"]))

        real_get, real_page = _nw._get, _mc.page_is_machine
        try:
            _nw._get = lambda u, timeout=20: "<title>ぜんぜん別の機種</title>"
            _mc.page_is_machine = real_page
            v = verify_official("Lすーぱぁびん娘", "https://m.example/products/slot/other/")
            t("★★公式ページが別機種なら止める★★"
              "（機種Aの名前＋機種BのURLで記事ができた穴・実際に再現した）",
              v and "一致しません" in v[0])
            _nw._get = lambda u, timeout=20: "<title>Lすーぱぁびん娘|BELLCO</title>"
            t("　同じ機種なら通る",
              verify_official("Lすーぱぁびん娘",
                              "https://m.example/products/slot/lbinko/") == [])
        finally:
            _nw._get, _mc.page_is_machine = real_get, real_page
    finally:
        _di.find, _sl.read_page, _mc.lookup = real_find, real_read, real_lookup

    ng = [n for n, ok in results if not ok]
    print(f"{nl}{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--apply", action="store_true", help="実際に書き込む")
    ap.add_argument("--ctx", help="task_lock の CTX パス（--apply に必須）")
    ap.add_argument("--name", help="1機種だけ試す：正式名称")
    ap.add_argument("--official-url", dest="official_url")
    ap.add_argument("--maker")
    ap.add_argument("--release", default="")
    args = ap.parse_args()
    if args.selftest:
        return selftest()

    if args.apply and not args.ctx:
        print("★--apply には --ctx（ロックのCTXパス）が必要です★")
        return 1
    if args.apply:
        r = subprocess.run(
            [sys.executable, os.path.join(BASE, "scripts", "task_lock.py"),
             "check", "--ctx", args.ctx], capture_output=True, text=True)
        if r.returncode != 0:
            print("★ロックを持っていません → 何も書かずに終了します★")
            return 1
        # ★task_guard も必ず通す★（Codex指摘4・通していなかった）
        if args.name and args.official_url:
            slug = _ba.slug_from_url(args.official_url)
            g = subprocess.run(
                [sys.executable, os.path.join(BASE, "scripts", "task_guard.py"),
                 "claim", "--task", "add-machine", "--slug", slug],
                capture_output=True, text=True,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            if g.returncode != 0:
                print("★今日の担当ではありません → 何も書かずに終了します★")
                print((g.stdout or g.stderr or "").strip()[:200])
                return 1

    if args.name:
        if not (args.official_url and args.maker):
            print("★--name と一緒に --official-url --maker が必要です★")
            return 1
        res = run_one(args.name, args.official_url, args.maker, args.release, args.apply)
        print(json.dumps({k: v for k, v in res.items() if k != "preview"},
                         ensure_ascii=False, indent=1))
        return 0 if res.get("wrote") or not args.apply else 1

    d = discover()
    for x in d["first_time"]:
        print("初回として記録:", x)
    print(f"新台候補: {len(d['candidates'])} 件 / 確認が要る: {len(d['problems'])} 件")
    for c in d["candidates"]:
        print(f"  ★{c['official_name']}（{c['maker']}／{(c['release'] or {}).get('value')}）")
        print(f"    {c['url']}")
    for p in d["problems"]:
        print("  ✗ " + p[:150])
    if d["problems"]:
        _ledger("site", "structural", "MATERIAL", "WATCH_PROBLEM",
                "新台の見張りで確認が要る点が出ました",
                " / ".join(d["problems"])[:1500])
    return 1 if d["problems"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except _sj.SafeJsonError as e:
        print(f"★入力データが読めません: {e}★")
        raise SystemExit(1)
    except Exception as e:
        print(f"★想定外の失敗 {type(e).__name__}: {e}★")
        raise SystemExit(1)

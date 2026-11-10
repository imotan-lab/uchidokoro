"""★新台の「通称（検索の別名）」と「機種のタイプ」を、2AIが決めて機種行へ書く唯一の場所★（2026-11-10）

★なぜ★（運営者「更新されないページ他にもないの？」の2回目の洗い出しで見つかった）
  新台の機種行は aliases=[]・info="" で作られ（build_new_article.build_machine＝未確認のことを固定値で書かない）、
  埋める道がどこにも無かった＝新台29機種すべてで、通称（「リコリコ」など）で検索しても出てこず、早見表のタイプ欄も空。
  しかも毎朝の育成が機種行を作り直すので、手で足しても空に戻っていた（いまは grow_machine.carry_machine_meta が持ち越す）。

★決めるのは2AI。機械が見るのは形だけ★（鉄則0z）
  ・新台経路の機種だけ／判断者の組／理由15字以上／いまの値（before_aliases・before_info）が機種行と同じ
  ・通称は1〜10件・1件1〜30字・タグ・見えない字・サイト名なし・重複なし・機種名そのものは入れない
  ・タイプは1〜20字（例：スマスロAT・スマスロBT・Aタイプ＝昔の機種と同じ書き方）
★原作（楽天のリンク）はここでは書かない★＝広告に関わるので運営者の確認が要る（鉄則3）。
書いたら早見表など（build_hub_pages --legacy）とその機種のページを作り直す（できなければ元に戻す）。

CLI:
  python scripts/machine_meta.py --next 3
  python scripts/machine_meta.py --apply <決定ファイル> [--dry-run]
  python scripts/machine_meta.py --tried <slug> --result NOT_ENOUGH|UNDECIDED
  python scripts/machine_meta.py --selftest
決定ファイル: {"slug", "by": ["claude","codex"], "why", "before_aliases": [...], "before_info": "",
              "aliases": ["通称", ...], "info": "スマスロAT"}
"""
from __future__ import annotations

import argparse
import datetime as _dt
import io
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
BASE = os.path.dirname(HERE)
import local_paths as _lp            # noqa: E402

MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
QUEUE = _lp.doc("machine_meta_queue.json")
WHY_MIN = 15
RETRY_DAYS = 7
RESULTS = ("NOT_ENOUGH", "UNDECIDED")
_SLUG = re.compile(r"^[a-z0-9_]{1,60}$")


def _log(msg: str) -> None:
    try:
        os.makedirs(_lp.LOGS, exist_ok=True)
        with io.open(os.path.join(_lp.LOGS, "machine_meta_%s.log" % _dt.date.today().isoformat()),
                     "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (_dt.datetime.now().strftime("%H:%M:%S"), msg))
    except OSError:
        pass


def _text_ng(name: str, s, lo: int, hi: int) -> list:
    import gates as _g
    import audit_site as _a
    if not isinstance(s, str) or not lo <= len(s.strip()) <= hi or s != s.strip():
        return [f"{name} は{lo}〜{hi}字で、前後に空白を入れません: {s!r}"]
    out = []
    if re.search(r"[<>&\"'`\\]", s) or _g.html_unsafe(s) or _g.invisible_unsafe(s):
        out.append(f"{name} に使えない文字やタグがあります: {s!r}")
    for n in _a.COMPETITOR_NAMES:
        if n in s:
            out.append(f"{name} にサイト名があります: {n}")
    return out


def plan(row: dict, dec: dict) -> dict:
    import confirmed_values as _cv
    import page_decision as _pd
    p = []
    if not _pd.is_auto(row or {}):
        return {"problems": ["新台経路の機種ではありません（昔の機種の通称・タイプは手で書いてあります）"]}
    if _cv.judges_pair(dec.get("by") or []) is None:
        p.append("判断者の組がそろっていません（claude と codex、または代役の2人）")
    if len(str(dec.get("why") or "").strip()) < WHY_MIN:
        p.append(f"理由（why）は{WHY_MIN}字以上で書きます")
    if list(dec.get("before_aliases") or []) != list(row.get("aliases") or []) \
            or str(dec.get("before_info") or "") != str(row.get("info") or ""):
        p.append("判断したときの通称・タイプ（before_aliases・before_info）が、いまの機種行と違います（読み直して決め直す）")
    al = dec.get("aliases")
    if not isinstance(al, list) or not 1 <= len(al) <= 10:
        p.append("aliases は1〜10件の並びで書きます")
        al = []
    for a in al:
        p += _text_ng("通称", a, 1, 30)
    if len(set(al)) != len(al):
        p.append("通称が重なっています")
    if str(row.get("name") or "") in al:
        p.append("機種名そのものは通称に入れません（もう検索に引っかかります）")
    info = dec.get("info")
    p += _text_ng("タイプ", info, 1, 20)
    if p:
        return {"problems": p}
    return {"problems": [], "aliases": list(al), "info": info}


def _rows():
    import safe_json as _sj
    return _sj.read_rows(MACHINES)


def _write_rows(rows: list) -> None:
    import publish_new_machine as _pub
    _pub.write_atomic(MACHINES, json.dumps(rows, ensure_ascii=False, indent=1) + "\n")


def _rebuild(slug: str) -> bool:
    import build_machine_pages as _b
    if (_b._rebuild_auto(slug) or 0) != 0:
        return False
    r = subprocess.run([sys.executable, os.path.join(HERE, "build_hub_pages.py"), "--legacy"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode == 0


def apply_decision(path: str, apply_it: bool) -> dict:
    import safe_json as _sj
    try:
        dec = _sj.read_json(path, expect=dict)
    except Exception as e:                                       # noqa: BLE001
        return {"problems": [f"決定ファイルを読めません: {type(e).__name__}: {e}"]}
    slug = dec.get("slug")
    if not isinstance(slug, str) or not _SLUG.match(slug):
        return {"problems": ["slug が不正です"]}
    raw = io.open(MACHINES, encoding="utf-8", newline="").read()
    rows = json.loads(raw)
    row = next((m for m in rows if isinstance(m, dict) and m.get("slug") == slug), None)
    if row is None:
        return {"problems": ["機種一覧にありません"]}
    got = plan(row, dec)
    if got["problems"] or not apply_it:
        if got["problems"]:
            _log(f"{slug}: 断りました: " + " ／ ".join(got["problems"])[:500])
        return got
    row["aliases"] = got["aliases"]
    row["info"] = got["info"]
    _write_rows(rows)
    keep_pages = {}
    if not _rebuild(slug):
        io.open(MACHINES, "w", encoding="utf-8", newline="").write(raw)
        _rebuild(slug)
        got["problems"] = ["ページ・早見表を作り直せなかったので、機種行を元に戻しました"]
        _log(f"{slug}: {got['problems'][0]}")
        return got
    _log(f"{slug}: 通称 {got['aliases']}・タイプ {got['info']!r} を書きました（{','.join(dec['by'])}）")
    got["written"] = True
    return got


def pick(n: int, rows: list, tried: dict, today: str) -> list:
    """★通称かタイプが空の新台★（試したことの無い機種→古い順。断って RETRY_DAYS 日たたない機種は外す）"""
    import page_decision as _pd
    cut = (_dt.date.fromisoformat(today) - _dt.timedelta(days=RETRY_DAYS)).isoformat()
    cands = [m["slug"] for m in rows if isinstance(m, dict) and _pd.is_auto(m)
             and (not m.get("aliases") or not m.get("info"))]
    never = [s for s in cands if s not in tried]
    old = sorted([s for s in cands if s in tried and str(tried[s]) <= cut and str(tried[s]) < today],
                 key=lambda s: str(tried[s]))
    return (never + old)[:max(0, n)]


def selftest() -> int:
    ok = [0, 0]

    def t(name, cond):
        ok[0 if cond else 1] += 1
        print(("✅ " if cond else "❌ ") + name)

    row = {"slug": "zzz_m", "name": "スマスロ 試験機", "publication_policy": "page-decision/v2",
           "aliases": [], "info": ""}
    good = {"slug": "zzz_m", "by": ["claude", "codex"], "why": "DMMの機種ページの題とタイプ欄を2AIで読んだ",
            "before_aliases": [], "before_info": "", "aliases": ["試験機", "しけんき"], "info": "スマスロAT"}
    r = plan(row, json.loads(json.dumps(good)))
    t("★★2AIが決めた通称とタイプを受け取る★★", not r["problems"] and r["aliases"] == ["試験機", "しけんき"])

    def bad(mut, why, must, rw=None):
        d = json.loads(json.dumps(good))
        mut(d)
        rr = plan(rw or row, d)
        t(why, any(must in x for x in rr["problems"]))
    bad(lambda d: d.update(by=["claude"]), "　判断者が1人なら断る", "判断者")
    bad(lambda d: d.update(why="短い"), "　理由が短ければ断る", "理由")
    bad(lambda d: d.update(before_info="スマスロBT"), "★判断したときと機種行が違えば断る（上書きしない）★", "いまの機種行と違います")
    bad(lambda d: d.update(aliases=["<b>x</b>"]), "　タグは使えない", "使えない文字")
    bad(lambda d: d.update(aliases=["スロパチクエスト"]), "　サイト名は使えない", "サイト名")
    bad(lambda d: d.update(aliases=["a", "a"]), "　通称の重複は断る", "重なって")
    bad(lambda d: d.update(aliases=["スマスロ 試験機"]), "　機種名そのものは通称に入れない", "機種名そのもの")
    bad(lambda d: d.update(info=""), "　タイプが空なら断る", "タイプ")
    bad(lambda d: None, "★★新台経路でない機種には書かない★★", "新台経路の機種ではありません",
        rw={"slug": "zzz_m", "name": "x", "aliases": [], "info": ""})
    rows = [row, dict(row, slug="zzz_n", aliases=["a"], info="x"), dict(row, slug="zzz_o", info="x")]
    t("★通称かタイプが空の新台だけを選ぶ★", pick(5, rows, {}, "2026-11-10") == ["zzz_m", "zzz_o"])
    t("　断って7日たたない機種は外す", pick(5, rows, {"zzz_m": "2026-11-05"}, "2026-11-10") == ["zzz_o"])
    import inspect
    import grow_machine as _gm
    t("★★毎朝の育成が、通称とタイプを空に戻さない（持ち越しが配線されている）★★",
      "carry_machine_meta(cur, machine)" in inspect.getsource(_gm.plan_one))
    print(f"\n{ok[0]}/{sum(ok)} 合格")
    return 0 if ok[1] == 0 else 1


def main() -> int:
    for _st in (sys.stdout, sys.stderr):
        try:
            _st.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                        # noqa: BLE001
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--next", type=int)
    ap.add_argument("--apply")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tried")
    ap.add_argument("--result", choices=RESULTS)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    today = _dt.date.today().isoformat()
    q = {}
    if os.path.isfile(QUEUE):
        with io.open(QUEUE, encoding="utf-8") as f:
            q = json.load(f)
    if a.apply:
        r = apply_decision(a.apply, not a.dry_run)
        if r.get("problems"):
            print("書きませんでした:")
            for x in r["problems"]:
                print("  - " + x)
            return 1
        print("下見: 通ります（書いていません）" if a.dry_run else "書きました（機種行・ページ・早見表）")
        return 0
    if a.tried:
        if not a.result or not _SLUG.match(a.tried):
            print("--tried には --result が要ります")
            return 1
        q.setdefault("tried", {})[a.tried] = today
        os.makedirs(os.path.dirname(QUEUE), exist_ok=True)
        with io.open(QUEUE, "w", encoding="utf-8") as f:
            f.write(json.dumps(q, ensure_ascii=False, indent=1) + "\n")
        print(f"{a.tried}: {a.result} を記録しました（{RETRY_DAYS}日あけてまた対象）")
        return 0
    if a.next is not None:
        got = pick(a.next, _rows(), q.get("tried") or {}, today)
        print(" ".join(got) if got else "今日決める機種はありません")
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())

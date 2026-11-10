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
              "aliases": ["通称", ...], "info": "スマスロAT",
              （任意）"before_series": null, "series": "バジリスク"}   ← "" はシリーズ無しと決めた印
★シリーズ名（series）★（2026-11-11・運営者の判断「5A」）＝同じシリーズ名の機種どうしを記事ページの「関連機種」でつなぐ。
  楽天リンクの原作欄（original）とは別物（広告に触れない）。昔の機種にも書ける（担当の点検表 STEP 1-T の meta 項目）。
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


_IN_SELFTEST = False


def _log(msg: str) -> None:
    if _IN_SELFTEST:                 # ★試験の行で本番のログを埋めない★（2026-11-10・洗い出し4回目）
        return
    try:
        os.makedirs(_lp.LOGS, exist_ok=True)
        with io.open(os.path.join(_lp.LOGS, "machine_meta_%s.log" % _dt.date.today().isoformat()),
                     "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (_dt.datetime.now().strftime("%H:%M:%S"), msg))
    except OSError:
        pass


_NO_SERIES = object()      # ★機種行にシリーズ名の鍵が無かった★（戻すときに鍵ごと外す）


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
    # ★昔の機種も受け取る★（2026-11-10・洗い出し4回目＝担当の点検表 STEP 1-T の meta 項目で見直す）。
    #   割り当て（--next）は今までどおり新台だけ（昔の機種は担当が回ってきたときに見る）
    if not isinstance(row, dict) or not row.get("slug"):
        return {"problems": ["機種一覧の行がありません"]}
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
    # ★★シリーズ名（任意）★★（2026-11-11・運営者の判断「5A」）＝関連機種をつなぐ。楽天リンク（原作）とは別。
    #   ★書くときは判断したときの値（before_series）も要る★（鍵が無い＝まだ決めていない／""＝シリーズ無しと決めた）
    out = {"problems": [], "aliases": list(al), "info": info}
    if "series" in dec:
        if "before_series" not in dec or dec.get("before_series") != row.get("series"):
            p.append("判断したときのシリーズ名（before_series）が、いまの機種行と違います"
                     "（まだ決めていなければ null と書く）")
        sv = dec.get("series")
        if sv != "":
            p += _text_ng("シリーズ名", sv, 1, 30)
        out["series"] = sv
    if p:
        return {"problems": p}
    return out


def _rows():
    import safe_json as _sj
    return _sj.read_rows(MACHINES)


def _write_rows(rows: list) -> None:
    import publish_new_machine as _pub
    _pub.write_atomic(MACHINES, json.dumps(rows, ensure_ascii=False, indent=1) + "\n")


def _rebuild(slug: str) -> bool:
    import build_machine_pages as _b
    import page_decision as _pd
    row = next((m for m in _rows() if isinstance(m, dict) and m.get("slug") == slug), {})
    if _pd.is_auto(row):
        if (_b._rebuild_auto(slug) or 0) != 0:
            return False
    else:
        r0 = subprocess.run([sys.executable, os.path.join(HERE, "build_machine_pages.py"), "--legacy", "--slug", slug],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r0.returncode != 0:
            return False
    r = subprocess.run([sys.executable, os.path.join(HERE, "build_hub_pages.py"), "--legacy"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode == 0


def restore_row(slug: str, got: dict, old: tuple) -> bool:
    """★この機種の通称・タイプだけを元へ戻す★（一覧全体を書き戻さない＝別機種の更新を消さない）

    自分が書いた値のままのときだけ戻す（そのあいだに別の処理が書いていれば触らない）。
    """
    import publish_new_machine as _pub
    try:
        with _pub._OnlyOne():
            rows = _rows()
            row = next((m for m in rows if isinstance(m, dict) and m.get("slug") == slug), None)
            if row is None or row.get("aliases") != got["aliases"] or row.get("info") != got["info"]                     or ("series" in got and row.get("series") != got["series"]):
                return False
            row["aliases"], row["info"] = old[0], old[1]
            if "series" in got:
                if len(old) > 2 and old[2] is not _NO_SERIES:
                    row["series"] = old[2]
                else:
                    row.pop("series", None)
            _write_rows(rows)
            return True
    except Exception as e:                                       # noqa: BLE001
        _log(f"{slug}: 元に戻せません: {type(e).__name__}: {e}")
        return False


def apply_decision(path: str, apply_it: bool) -> dict:
    import safe_json as _sj
    try:
        dec = _sj.read_json(path, expect=dict)
    except Exception as e:                                       # noqa: BLE001
        return {"problems": [f"決定ファイルを読めません: {type(e).__name__}: {e}"]}
    slug = dec.get("slug")
    if not isinstance(slug, str) or not _SLUG.match(slug):
        return {"problems": ["slug が不正です"]}
    import publish_new_machine as _pub
    # ★★読んでから書くまでを公開ロックの中で行う★★（2026-11-10・Codexの指摘＝実害）＝
    #   ロックの外で一覧全体を読み書きすると、そのあいだに別の処理が直した別機種の値を古い値へ戻す。
    #   作り直し（_rebuild_auto）は自分でロックを取るので、ロックの外で呼ぶ。
    try:
        with _pub._OnlyOne():
            rows = _rows()
            row = next((m for m in rows if isinstance(m, dict) and m.get("slug") == slug), None)
            if row is None:
                return {"problems": ["機種一覧にありません"]}
            got = plan(row, dec)
            if got["problems"] or not apply_it:
                if got["problems"]:
                    _log(f"{slug}: 断りました: " + " ／ ".join(got["problems"])[:500])
                return got
            old = (json.loads(json.dumps(row.get("aliases"))), row.get("info"), row.get("series", _NO_SERIES))
            row["aliases"] = got["aliases"]
            row["info"] = got["info"]
            if "series" in got:
                row["series"] = got["series"]
            _write_rows(rows)
    except _pub.PublishError as e:
        return {"problems": [f"公開ロックを取れないので書きません: {e}"]}
    try:
        ok = _rebuild(slug)
    except Exception as e:                                       # noqa: BLE001
        # ★例外でも元に戻す★（2026-11-10・Codexの指摘＝ロック競合などで機種行が変わったまま残った）
        ok = False
        _log(f"{slug}: 作り直しで例外: {type(e).__name__}: {e}")
    if not ok:
        back = restore_row(slug, got, old)
        try:
            _rebuild(slug)
        except Exception as e:                                   # noqa: BLE001
            _log(f"{slug}: 戻したあとの作り直しで例外: {type(e).__name__}: {e}")
        got["problems"] = ["ページ・早見表を作り直せなかったので、機種行を元に戻しました" if back
                           else "★ページ・早見表を作り直せず、機種行も戻せませんでした（手で確かめてください）★"]
        _log(f"{slug}: {got['problems'][0]}")
        return got
    _log(f"{slug}: 通称 {got['aliases']}・タイプ {got['info']!r}"
         + (f"・シリーズ名 {got['series']!r}" if "series" in got else "") + f" を書きました（{','.join(dec['by'])}）")
    got["written"] = True
    return got


STALL_DAYS = 3


def stall_problem(rows: list, q: dict, today: str):
    """★見張り番：通称かタイプが空の新台があるのに、STALL_DAYS 日だれも書こうとしていない★（2026-11-10・洗い出し4回目）

    なぜ＝朝の STEP 0-L を飛ばしても誰も気づけなかった（新台29機種すべてで通称・タイプが空）。
    「試した日」（--tried）と「書いた日」（written）のいちばん新しい日で見る。
    """
    import page_decision as _pd
    cands = [m["slug"] for m in rows if isinstance(m, dict) and _pd.is_auto(m)
             and (not m.get("aliases") or not m.get("info") or "series" not in m)]
    if not cands:
        return None
    last = max([str(v) for v in (q.get("tried") or {}).values()] + [str(v) for v in (q.get("written") or {}).values()]
               or [""])
    if not last:
        return f"通称かタイプが空の新台が{len(cands)}件あるのに、一度も書こうとしていません（朝の STEP 0-L）"
    days = (_dt.date.fromisoformat(today) - _dt.date.fromisoformat(last[:10])).days
    if days >= STALL_DAYS:
        return f"通称かタイプが空の新台が{len(cands)}件あるのに、{days}日だれも書こうとしていません（朝の STEP 0-L）"
    return None


def pick(n: int, rows: list, tried: dict, today: str) -> list:
    """★通称かタイプが空・シリーズ名が未決の新台★（試したことの無い機種→古い順。断って RETRY_DAYS 日たたない機種は外す）"""
    import page_decision as _pd
    cut = (_dt.date.fromisoformat(today) - _dt.timedelta(days=RETRY_DAYS)).isoformat()
    cands = [m["slug"] for m in rows if isinstance(m, dict) and _pd.is_auto(m)
             and (not m.get("aliases") or not m.get("info") or "series" not in m)]
    never = [s for s in cands if s not in tried]
    old = sorted([s for s in cands if s in tried and str(tried[s]) <= cut and str(tried[s]) < today],
                 key=lambda s: str(tried[s]))
    return (never + old)[:max(0, n)]


def selftest() -> int:
    global _IN_SELFTEST
    _IN_SELFTEST = True
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
    bad(lambda d: d.update(before_aliases=["古い通称"]), "　通称が判断したときと違っても断る", "いまの機種行と違います")
    bad(lambda d: d.update(aliases=["<b>x</b>"]), "　タグは使えない", "使えない文字")
    bad(lambda d: d.update(aliases=["スロパチクエスト"]), "　サイト名は使えない", "サイト名")
    bad(lambda d: d.update(aliases=["a", "a"]), "　通称の重複は断る", "重なって")
    bad(lambda d: d.update(aliases=["スマスロ 試験機"]), "　機種名そのものは通称に入れない", "機種名そのもの")
    bad(lambda d: d.update(info=""), "　タイプが空なら断る", "タイプ")
    _rl = plan({"slug": "zzz_m", "name": "昔の機種", "aliases": ["古い通称"], "info": "スマスロAT"},
               dict(good, before_aliases=["古い通称"], before_info="スマスロAT"))
    t("★★昔の機種の通称・タイプも、判断したときの値が今と同じなら受け取る（担当の点検表の meta）★★",
      not _rl["problems"] and _rl["aliases"] == ["試験機", "しけんき"])
    # ★★シリーズ名（任意）★★（2026-11-11・運営者の判断「5A」）
    _s1 = plan(row, dict(good, before_series=None, series="バジリスク"))
    _s2 = plan(row, dict(good, before_series=None, series=""))
    _s3 = plan(dict(row, series="バジリスク"), dict(good, before_series=None, series="北斗"))
    _s4 = plan(row, dict(good, series="バジリスク"))
    _s5 = plan(row, dict(good, before_series=None, series="<b>x</b>"))
    _s6 = plan(row, json.loads(json.dumps(good)))
    t("★★シリーズ名を受け取る（\"\" はシリーズ無しの印）・書かなければ触らない★★",
      _s1.get("series") == "バジリスク" and _s2.get("series") == "" and not _s1["problems"]
      and not _s2["problems"] and "series" not in _s6)
    t("　判断したときのシリーズ名が違う・書いていなければ断る／タグは断る",
      any("before_series" in x for x in _s3["problems"]) and any("before_series" in x for x in _s4["problems"])
      and any("使えない文字" in x for x in _s5["problems"]))
    _old_r, _new_r = {"aliases": ["a"], "info": "x", "series": ""}, {"aliases": [], "info": ""}
    _gm0 = __import__("grow_machine")
    _gm0.carry_machine_meta(_old_r, _new_r)
    t("★★毎朝の育成が、シリーズ名（シリーズ無しの印も）を持ち越す★★", _new_r.get("series") == "")
    _tpl = io.open(os.path.join(BASE, "machine.html"), encoding="utf-8").read()
    t("★★記事ページの「関連機種」が、同じシリーズ名の機種をつなぐ（\"\" はつながない）★★",
      "const sameName = curName && seriesName(x) === curName;" in _tpl
      and "return sameOrig || sameSeries || sameName;" in _tpl
      and '(typeof m.series === "string" && m.series) ? m.series : null' in _tpl)
    import gates as _gx
    import audit_public as _apx
    import publish_new_machine as _pnx
    t("　機種行の鍵の許可一覧（射影・独立監査・新台の関所）にシリーズ名がある",
      _gx._MACHINE_TYPES.get("series") is str and "series" in _apx.ALLOWED_MACHINE_KEYS
      and "series" in _pnx._MACHINE_KEYS)
    rows = [row, dict(row, slug="zzz_n", aliases=["a"], info="x", series=""), dict(row, slug="zzz_o", info="x"),
            dict(row, slug="zzz_p", aliases=["a"], info="x")]
    t("★通称かタイプかシリーズ名が決まっていない新台だけを選ぶ★",
      pick(5, rows, {}, "2026-11-10") == ["zzz_m", "zzz_o", "zzz_p"])
    t("　断って7日たたない機種は外す", pick(5, rows, {"zzz_m": "2026-11-05"}, "2026-11-10") == ["zzz_o", "zzz_p"])
    # ★★書き込み：ロックの中で読み直す・作り直しが例外でもこの機種だけ戻す★★（2026-11-10・Codexの指摘）
    import tempfile
    import publish_new_machine as _pubx
    g = globals()
    keep = {k: g[k] for k in ("MACHINES", "_rebuild")}
    keep_lock = _pubx._OnlyOne
    tmp = tempfile.mkdtemp(prefix="mmeta_")
    try:
        g["MACHINES"] = os.path.join(tmp, "machines.json")
        other = dict(row, slug="zzz_other", aliases=[], info="")
        io.open(g["MACHINES"], "w", encoding="utf-8").write(json.dumps([row, other], ensure_ascii=False))

        class _Lock:                     # ★ロックを取った瞬間に、別の処理が別機種を直し終えた形★
            def __enter__(self):
                rs = json.loads(io.open(g["MACHINES"], encoding="utf-8").read())
                for m in rs:
                    if m["slug"] == "zzz_other" and not m["info"]:
                        m["info"] = "別の処理が書いた"
                io.open(g["MACHINES"], "w", encoding="utf-8").write(json.dumps(rs, ensure_ascii=False))
                return self

            def __exit__(self, *a):
                return False
        _pubx._OnlyOne = _Lock
        dp = os.path.join(tmp, "dec.json")
        io.open(dp, "w", encoding="utf-8").write(json.dumps(good, ensure_ascii=False))
        g["_rebuild"] = lambda s: True
        r1 = apply_decision(dp, True)
        rs1 = {m["slug"]: m for m in json.loads(io.open(g["MACHINES"], encoding="utf-8").read())}
        t("★★書き込みはロックの中で読み直す（そのあいだに別の処理が直した別機種の値を消さない）★★",
          r1.get("written") and rs1["zzz_m"]["info"] == "スマスロAT"
          and rs1["zzz_other"]["info"] == "別の処理が書いた")
        io.open(g["MACHINES"], "w", encoding="utf-8").write(json.dumps([row, other], ensure_ascii=False))

        def _boom(s):
            raise RuntimeError("ロック競合")
        g["_rebuild"] = _boom
        try:
            r2 = apply_decision(dp, True)
        except Exception:                                        # noqa: BLE001
            r2 = {"problems": [], "crashed": True}
        rs2 = {m["slug"]: m for m in json.loads(io.open(g["MACHINES"], encoding="utf-8").read())}
        t("★★作り直しが例外でも、この機種の通称・タイプを元に戻す（別機種はそのまま）★★",
          r2["problems"] and not r2.get("written") and rs2["zzz_m"]["aliases"] == []
          and rs2["zzz_m"]["info"] == "" and rs2["zzz_other"]["info"] == "別の処理が書いた")
        io.open(g["MACHINES"], "w", encoding="utf-8").write(json.dumps([row, other], ensure_ascii=False))
        io.open(dp, "w", encoding="utf-8").write(json.dumps(dict(good, before_series=None, series="試験"),
                                                            ensure_ascii=False))
        g["_rebuild"] = lambda s: True
        r3 = apply_decision(dp, True)
        rs3 = {m["slug"]: m for m in json.loads(io.open(g["MACHINES"], encoding="utf-8").read())}
        io.open(g["MACHINES"], "w", encoding="utf-8").write(json.dumps([row, other], ensure_ascii=False))
        g["_rebuild"] = _boom
        try:
            r4 = apply_decision(dp, True)
        except Exception:                                        # noqa: BLE001
            r4 = {"problems": [], "crashed": True}
        rs4 = {m["slug"]: m for m in json.loads(io.open(g["MACHINES"], encoding="utf-8").read())}
        t("★★シリーズ名を書く・作り直せなければ鍵ごと元に戻す★★",
          r3.get("written") and rs3["zzz_m"].get("series") == "試験"
          and r4["problems"] and "series" not in rs4["zzz_m"])
    finally:
        g.update(keep)
        _pubx._OnlyOne = keep_lock
    _rows_s = [row, dict(row, slug="zzz_full", aliases=["a"], info="x", series="")]
    t("★★空の新台があるのに3日だれも書こうとしていなければ、見張り番が知らせる★★",
      stall_problem(_rows_s, {"tried": {"zzz_m": "2026-11-05"}}, "2026-11-10") is not None
      and stall_problem(_rows_s, {"written": {"zzz_x": "2026-11-09"}}, "2026-11-10") is None
      and stall_problem(_rows_s, {}, "2026-11-10") is not None)
    t("　空の新台が無ければ知らせない", stall_problem(_rows_s[1:], {}, "2026-11-10") is None)
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
    ap.add_argument("--check", action="store_true", help="見張り番：空の新台があるのに進んでいなければ1")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    today = _dt.date.today().isoformat()
    q = {}
    try:
        if os.path.isfile(QUEUE):
            with io.open(QUEUE, encoding="utf-8") as f:
                q = json.load(f)
    except Exception as e:                                       # noqa: BLE001
        print(f"🟠 通称・タイプの割り当ての控えを読めません: {type(e).__name__}: {e}")
        return 2
    if a.check:
        pr = stall_problem(_rows(), q, today)
        print(("🟡 " + pr) if pr else "🟢 新台の通称・タイプは進んでいます")
        return 1 if pr else 0
    if a.apply:
        r = apply_decision(a.apply, not a.dry_run)
        if r.get("problems"):
            print("書きませんでした:")
            for x in r["problems"]:
                print("  - " + x)
            return 1
        if not a.dry_run:
            q.setdefault("written", {})[r.get("slug") or ""] = today
            os.makedirs(os.path.dirname(QUEUE), exist_ok=True)
            with io.open(QUEUE, "w", encoding="utf-8") as f:
                f.write(json.dumps(q, ensure_ascii=False, indent=1) + "\n")
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

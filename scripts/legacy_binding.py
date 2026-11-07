"""★昔からある機種と、DMMの機種ページの結び付け★（2026-11-07）

★なぜ要るか★
  2AIで確定した値の控え（confirmed_values）は、記録するときに
  「どの機種の値か」を**DMMの機種ページのURLから**引く（`bind_machine`）。
  人が名乗った slug を信じると、機種Aの本物の根拠を機種Bとして保存できるため。
  ところが昔からある記事の機種（machines.json に identity が無いもの・約120機種）は
  そのURLを持っていないので、★2AIが正しく確かめても1件も記録できなかった★。
  実害＝夕方のランキングのタスクの初回（2026-11-07）で、上位10の20機種すべてが
  この形だったため、恩恵の一言を1件も書けなかった。

★やること★
  2AIが「この機種のDMMのページはここ」と決めたら、その結び付けを控える。
  `confirmed_values.bind_machine` は、一覧の identity で見つからなかったとき
  ここを引く。★記事データ（machines.json）は1文字も変えない★
  （identity の有無で動きが変わる処理が多く、影響が読み切れないため）。

★機械が見ること（言うだけでは通らないこと・形だけ）★
  ①その slug が一覧に在り、まだ identity を持たない（＝昔からある機種）
  ②URLはDMMの機種ページの形（https://p-town.dmm.com/machines/<数字>）
  ③そのURLを、一覧の identity・待ち行列・ほかの結び付けが使っていない
    （★1つのページに2つの機種を結ばない／1つの機種を2つのページに結ばない★）
  ④判断者の組（claude と codex など・confirmed_values.JUDGE_PAIRS）・理由15字以上
  ⑤根拠の逐語（8〜120字）が、取ってきたそのページの本文に実在する
  ★そのページが本当にその機種かは2AIが決める★（機械は意味を判定しない）。

置き場＝Documents/uchidokoro/legacy_binding.json（リポジトリの外）
CLI:
  --bind --slug X --url URL --by claude,codex --why-file F --proof-file F
  --list / --forget --slug X --why-file F / --selftest
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import local_paths as _lp      # noqa: E402
import safe_json as _sj        # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE = os.path.join(_lp.DOCS, "legacy_binding.json")
MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
SCHEMA = "legacy-binding/v1"
DMM_URL_RE = re.compile(r"^https://p-town\.dmm\.com/machines/[0-9]+$")
WHY_MIN = 15
PROOF_MIN, PROOF_MAX = 8, 120


class BindingError(Exception):
    pass


def norm_url(url) -> str:
    return str(url or "").strip().rstrip("/")


def _machines() -> list:
    ms = _sj.read_json(MACHINES, expect=(dict, list))
    return ms["machines"] if isinstance(ms, dict) else ms


def _ident_url(m) -> str:
    return norm_url(((m or {}).get("identity") or {}).get("official_product_url"))


def load() -> dict:
    """★控えを読む★（無ければ空・壊れていれば止める）"""
    data = _sj.read_json(STORE, expect=dict, allow_missing=True,
                         default={"schema_version": SCHEMA, "bindings": {}})
    if data.get("schema_version") != SCHEMA \
            or not isinstance(data.get("bindings"), dict):
        raise BindingError("結び付けの控えの形が違います: %s" % STORE)
    return data


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, STORE)


def entry_problems(slug: str, e, machines: list) -> list:
    """★1件の結び付けが、いまも使える形か★（読むたびに見る）"""
    import confirmed_values as _cv
    ng = []
    if not isinstance(e, dict):
        return ["%s: 結び付けの形が違います" % slug]
    url = norm_url(e.get("url"))
    if not DMM_URL_RE.match(url):
        ng.append("%s: URLがDMMの機種ページの形ではありません（%s）" % (slug, url))
    if _cv.judges_pair(e.get("by") or []) is None:
        ng.append("%s: 判断者の組がそろっていません" % slug)
    if len(str(e.get("why") or "").strip()) < WHY_MIN:
        ng.append("%s: 理由が%d字に足りません" % (slug, WHY_MIN))
    pr = str(e.get("proof") or "")
    if not PROOF_MIN <= len(pr) <= PROOF_MAX:
        ng.append("%s: 根拠の逐語は%d〜%d字です" % (slug, PROOF_MIN, PROOF_MAX))
    hit = [m for m in machines if (m or {}).get("slug") == slug]
    if len(hit) != 1:
        ng.append("%s: 一覧にその機種がありません" % slug)
    elif _ident_url(hit[0]):
        ng.append("%s: もう identity を持っています（結び付けは要りません）" % slug)
    for m in machines:
        if _ident_url(m) and _ident_url(m) == url:
            ng.append("%s: そのURLは一覧の別の機種（%s）が使っています"
                      % (slug, m.get("slug")))
    return ng


def slug_for_url(official_url: str):
    """★DMMのURLから、結び付けた昔からある機種を引く★（無ければ None）

    ★壊れた控え・使えない結び付けは当てない（例外で止める）★。
    """
    want = norm_url(official_url)
    if not want:
        return None
    data = load()
    hits = [(s, e) for s, e in data["bindings"].items()
            if isinstance(e, dict) and norm_url(e.get("url")) == want]
    if not hits:
        return None
    if len(hits) != 1:
        raise BindingError("1つのURLに2つ以上の機種が結ばれています: %s" % want)
    slug, e = hits[0]
    ms = _machines()
    bad = entry_problems(slug, e, ms)
    # ★結んだときの組（slug・URL・根拠・名前・判断者・理由）が変わっていないか★
    #   （2026-11-07・Codex）＝控えの鍵を別の機種へ移す・URLだけ差し替える、を止める。
    #   ★封は同じ権限から作り直せる★＝うっかりの書き換えを止めるもので、悪意への境界ではない。
    if e.get("seal") != seal(slug, e):
        bad.append("%s: 結んだときの組から変わっています（結び直してください）" % slug)
    name = next((str(m.get("name") or "") for m in ms if m.get("slug") == slug), "")
    if not name or name != str(e.get("name") or ""):
        bad.append("%s: 一覧の機種名が結んだときと違います（%s → %s）"
                   % (slug, e.get("name"), name))
    # ★待ち行列の新台がそのURLを使い始めたら当てない／読めなければ止める★（Codex）
    import pending_machines as _pm
    try:
        if _pm.find_by_url(_pm.load(), want):
            bad.append("%s: そのURLは待ち行列の新台が使っています" % slug)
    except Exception as ex:                                  # noqa: BLE001
        bad.append("待ち行列が読めないので結び付けを使えません（%s）" % str(ex)[:60])
    if bad:
        raise BindingError("／".join(bad[:3]))
    return slug, name


def seal(slug: str, e: dict) -> str:
    """★結んだときの組の指紋★"""
    body = [slug, norm_url(e.get("url")), str(e.get("proof") or ""),
            str(e.get("name") or ""), sorted(e.get("by") or []),
            str(e.get("why") or ""), str(e.get("page_sha256") or "")]
    return hashlib.sha256(json.dumps(body, ensure_ascii=False).encode("utf-8")).hexdigest()


def _page_text(url: str, fetch=None) -> str:
    import confirmed_values as _cv
    if fetch is not None:
        html = fetch(url)
    else:
        import fetched_page as _fp
        got = _fp.fetch(url, "machine_identity")
        if norm_url(got.final_url) != norm_url(url):
            raise BindingError("別のページへ転送されています（%s → %s）"
                               % (url, got.final_url))
        html = got.cleaned_html
    return _cv.page_form(_cv.page_text(html, url))


def bind(slug: str, url: str, by, why: str, proof: str, fetch=None) -> dict:
    """★2AIが決めた結び付けを控える★（1件でも合わなければ書かない）"""
    import pending_machines as _pm
    import confirmed_values as _cv
    url = norm_url(url)
    who = sorted({x.strip() for x in (by if isinstance(by, list)
                                         else str(by or "").split(","))
                  if x.strip()})
    e = {"url": url, "by": who, "why": str(why or "").strip(),
         "proof": str(proof or "").strip()}
    ms = _machines()
    ng = entry_problems(slug, e, ms)
    try:
        if _pm.find_by_url(_pm.load(), url):
            ng.append("そのURLは待ち行列の新台が使っています")
    except Exception as ex:                                  # noqa: BLE001
        ng.append("待ち行列が読めません（%s）" % str(ex)[:60])
    data = load()
    for s, old in data["bindings"].items():
        if s != slug and norm_url((old or {}).get("url")) == url:
            ng.append("そのURLはもう %s に結んであります" % s)
    old = data["bindings"].get(slug)
    if old and norm_url(old.get("url")) != url:
        ng.append("%s はもう別のURL（%s）に結んであります。先に --forget してください"
                  % (slug, old.get("url")))
    if ng:
        raise BindingError("／".join(ng[:4]))
    text = _page_text(url, fetch)
    if _cv.page_form(e["proof"]) not in text:
        raise BindingError("根拠の逐語がそのページに見当たりません: %s" % e["proof"][:40])
    e["name"] = next(str(m.get("name") or "") for m in ms if m.get("slug") == slug)
    e["bound_at"] = datetime.date.today().isoformat()
    e["page_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    e["seal"] = seal(slug, e)
    data["bindings"][slug] = e
    _save(data)
    return e


def forget(slug: str, why: str) -> dict:
    if len(str(why or "").strip()) < WHY_MIN:
        raise BindingError("取り消す理由は%d字以上です" % WHY_MIN)
    data = load()
    if slug not in data["bindings"]:
        raise BindingError("%s の結び付けはありません" % slug)
    gone = data["bindings"].pop(slug)
    _save(data)
    return gone


# ───────────────────────── 自己試験 ─────────────────────────

def selftest() -> int:
    import tempfile
    import confirmed_values as _cv
    import pending_machines as _pm
    g = globals()
    fails, n = [], [0]

    def ok(cond, label):
        n[0] += 1
        if not cond:
            fails.append(label)

    # ★確定値の側は import legacy_binding で別の写しを読む★（__main__ で動かすと
    #   この globals とは別物）＝両方を同じ一時の場所へ向ける
    import legacy_binding as _me
    keep = (g["STORE"], g["MACHINES"], _pm.load, _me.STORE, _me.MACHINES)
    tmp = tempfile.mkdtemp(prefix="lb_selftest_")
    g["STORE"] = _me.STORE = os.path.join(tmp, "legacy_binding.json")
    g["MACHINES"] = _me.MACHINES = os.path.join(tmp, "machines.json")
    with open(g["MACHINES"], "w", encoding="utf-8") as fh:
        json.dump({"machines": [
            {"slug": "zz_old", "name": "スマスロ試験機"},
            {"slug": "zz_old2", "name": "スマスロ試験機2"},
            {"slug": "zz_new", "name": "新台",
             "identity": {"official_product_url": "https://p-town.dmm.com/machines/9002"}},
        ]}, fh, ensure_ascii=False)
    _pm.load = lambda: {"items": {}}
    U = "https://p-town.dmm.com/machines/9001"
    PAGE = "<html><body><h1>スマスロ試験機</h1><p>スマスロ試験機の機種ページです</p></body></html>"
    fetch = lambda u: PAGE                                   # noqa: E731
    W = "DMMの題と導入日がこの機種と一致したため結び付けた"
    try:
        def refused(**kw):
            a = dict(slug="zz_old", url=U, by="claude,codex", why=W,
                     proof="スマスロ試験機の機種ページです", fetch=fetch)
            a.update(kw)
            # ★誤って通った結び付けを、次の試験へ持ち越さない★（1件ずつ独立に判定する）
            before = (open(g["STORE"], encoding="utf-8").read()
                      if os.path.exists(g["STORE"]) else None)
            try:
                bind(**a)
                return False
            except BindingError:
                return True
            finally:
                if before is None:
                    if os.path.exists(g["STORE"]):
                        os.remove(g["STORE"])
                else:
                    with open(g["STORE"], "w", encoding="utf-8", newline="\n") as fh:
                        fh.write(before)

        ok(refused(by="claude"), "判断者1人で結べる")
        ok(refused(why="短い"), "理由なしで結べる")
        ok(refused(url="https://example.com/machines/9001"), "DMM以外のURLで結べる")
        ok(refused(slug="zz_new"), "identity を持つ機種に結べる")
        ok(refused(slug="zz_none"), "一覧に無い機種に結べる")
        ok(refused(url="https://p-town.dmm.com/machines/9002", slug="zz_old"),
           "一覧の別機種のURLに結べる")
        ok(refused(proof="ページに無い文です"), "ページに無い根拠で結べる")
        ok(refused(proof="試験機"), "短すぎる根拠（ページには在る）で結べる")
        _pm.load = lambda: {"items": {"q_1": {"identity_url": U}}}
        ok(refused(), "待ち行列の新台のURLに結べる")
        _pm.load = lambda: {"items": {}}
        e = bind("zz_old", U + "/", "claude,codex", W, "スマスロ試験機の機種ページです", fetch)
        ok(e["url"] == U and e["name"] == "スマスロ試験機", "結べない")
        ok(refused(slug="zz_old2"), "1つのURLに2機種を結べる")
        ok(refused(url="https://p-town.dmm.com/machines/9003"),
           "1機種を2つのURLに結べる")
        ok(slug_for_url(U) == ("zz_old", "スマスロ試験機"), "URLから引けない")
        ok(slug_for_url("https://p-town.dmm.com/machines/1") is None,
           "結んでいないURLで機種が返る")

        # ★読む側の守り★（控えを書き換える試験は、終わったら元へ戻す）
        def read_refused(change=None, url=U, pm=None, rename=False):
            snap = open(g["STORE"], encoding="utf-8").read()
            msnap = open(g["MACHINES"], encoding="utf-8").read()
            keep_pm = _pm.load
            try:
                if change:
                    d = load()
                    change(d["bindings"])
                    _save(d)
                if rename is not False:
                    mm = json.loads(msnap)
                    mm["machines"][0]["name"] = ("スマスロ試験機 改" if rename is True
                                                 else rename)
                    with open(g["MACHINES"], "w", encoding="utf-8") as fh:
                        json.dump(mm, fh, ensure_ascii=False)
                if pm:
                    _pm.load = pm
                try:
                    slug_for_url(url)
                    return False
                except BindingError:
                    return True
            finally:
                _pm.load = keep_pm
                for p_, s_ in ((g["STORE"], snap), (g["MACHINES"], msnap)):
                    with open(p_, "w", encoding="utf-8", newline="\n") as fh:
                        fh.write(s_)

        ok(read_refused(lambda b: b.update({"zz_old2": b.pop("zz_old")})),
           "控えの鍵を別の機種へ移すと、その機種として通る")
        ok(read_refused(lambda b: b["zz_old"].update(
            {"url": "https://p-town.dmm.com/machines/9004"}),
            url="https://p-town.dmm.com/machines/9004"),
           "控えのURLだけ差し替えると通る")
        ok(read_refused(rename=True), "一覧の機種名が変わっても通る")
        ok(read_refused(rename=""), "一覧の機種名が空になっても通る")
        ok(read_refused(pm=lambda: {"items": {"q_9": {"identity_url": U}}}),
           "あとで待ち行列の新台が同じURLを使っても通る")

        def _broken():
            raise RuntimeError("壊れた待ち行列")
        ok(read_refused(pm=_broken), "待ち行列が読めなくても通る")
        ok(slug_for_url(U) == ("zz_old", "スマスロ試験機"), "読む側の試験が控えを戻していない")
        # ★確定値の記録は、ここを通って昔からある機種を引く★（配線）
        try:
            got_cv = _cv.bind_machine(U)
        except Exception as ex:                              # noqa: BLE001
            got_cv = ("例外", str(ex)[:60])
        ok(got_cv == ("zz_old", "スマスロ試験機"),
           "confirmed_values.bind_machine が結び付けを引かない")
        # ★使えなくなった結び付けは当てない★（その後 identity が付いた）
        with open(g["MACHINES"], "w", encoding="utf-8") as fh:
            json.dump({"machines": [{"slug": "zz_old", "name": "スマスロ試験機",
                                     "identity": {"official_product_url":
                                                  "https://p-town.dmm.com/machines/9009"}}]},
                      fh, ensure_ascii=False)
        try:
            slug_for_url(U)
            ok(False, "使えなくなった結び付けが当たる")
        except BindingError:
            ok(True, "")
        # ★手で書き換えた控えも読むたびに断る★（判断者を1人に）
        with open(g["MACHINES"], "w", encoding="utf-8") as fh:
            json.dump({"machines": [{"slug": "zz_old", "name": "スマスロ試験機"}]},
                      fh, ensure_ascii=False)
        d = load()
        d["bindings"]["zz_old"]["by"] = ["claude"]
        _save(d)
        try:
            slug_for_url(U)
            ok(False, "手で書き換えた判断者1人の結び付けが当たる")
        except BindingError:
            ok(True, "")
        ok(forget("zz_old", W) and "zz_old" not in load()["bindings"], "取り消せない")
    finally:
        g["STORE"], g["MACHINES"], _pm.load, _me.STORE, _me.MACHINES = keep
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    for f in fails:
        print("  ❌", f)
    print("legacy_binding selftest: %d/%d 合格" % (n[0] - len(fails), n[0]))
    return 1 if fails else 0


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read().strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="昔からある機種とDMMの機種ページの結び付け")
    ap.add_argument("--bind", action="store_true")
    ap.add_argument("--forget", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--slug", default="")
    ap.add_argument("--url", default="")
    ap.add_argument("--by", default="")
    ap.add_argument("--why-file", dest="why_file", default="")
    ap.add_argument("--proof-file", dest="proof_file", default="")
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if a.selftest:
        return selftest()
    try:
        if a.list:
            for s, e in sorted(load()["bindings"].items()):
                print(s, e.get("url"), ",".join(e.get("by") or []), e.get("bound_at"))
            return 0
        if a.bind:
            if not (a.slug and a.url and a.why_file and a.proof_file):
                print("--slug --url --by --why-file --proof-file が要ります")
                return 1
            e = bind(a.slug, a.url, a.by, _read(a.why_file), _read(a.proof_file))
            print("結びました:", a.slug, "→", e["url"])
            return 0
        if a.forget:
            if not (a.slug and a.why_file):
                print("--slug --why-file が要ります")
                return 1
            forget(a.slug, _read(a.why_file))
            print("取り消しました:", a.slug)
            return 0
    except (BindingError, _sj.SafeJsonError) as e:
        print("断りました:", e)
        return 1
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())

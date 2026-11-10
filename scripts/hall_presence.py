"""★ホールにほとんど無い機種を、ランキングから外す★（2026-11-09・運営者の決定）

★なぜ★
  朝一リセット・天井のランキングは、記事のある昔からの機種を古さに関係なく全部並べていた。
  読者はランキングを見てそのままホールで台を探すので、撤去された台が上にあると
  「探しても無い」になる（実測 2026-11-09：リセットの上位10に居た機種が全国14店だった）。
  ★古さでは切らない★＝古くても打てる台があり、導入日の分からない機種も多い（運営者の判断）。

★やること★
  DMMぱちタウンの機種ごとの設置店ページ（`<機種ページ>/shops`）に出る全国の設置店舗数を控え、
  ★線（全国 `line` 店）より少ない機種を、2つのランキングに載せない★。機種ページは残す。
  増えれば次に確かめた日に戻る。★外したという印は持たない★＝読む側が `stores < line` で決める。
  ★日付で判定しない★（データが同じなのに日が進むだけで結果が変わると、関所とCIが揺れる）。

★どこまでを機械がやるか★
  ・どの機種がDMMのどのページかは★2AIが決めた結び付け★（legacy_binding）だけを使う。
    機械は機種を推測しない（結び付けの無い機種は確かめず、今までどおり載せる）。
  ・★新台経路の機種は、記事を作るときにDMMの機種ページで確かめた identity を使う★（2026-11-09）
    （identity_binding が DMM_MACHINE_PAGE で、URLが機種ページの形のときだけ）。legacy_binding へは結ばない。
  ・件数はDMMのページが決まった形で出している数字を読むだけ（意味の判断ではない）。
    ★2か所（説明文の【N件掲載中】と本文の「設置店舗一覧 N件」）がちょうど1回ずつ・同じ数・
    題がパチスロの設置店のページ★のときだけ受け取る。どれか外れたら書かない（前の記録は残す）。

置き場＝assets/data/hall-presence.json（リポジトリの中＝CIでも生成器が読める）
CLI:
  --refresh N [--apply]  … 結び付け済みの対象機種を、最後に試した日が古い順（一度も試していない機種が先）に
                           N 件取り直す。★試した日（tries）と確かめ終えた日（checked）を分ける★＝
                           取れなかった機種が毎回先頭を占めて、ほかが確かめられなくなるのを防ぐ（Codex）
                           終了コード 10＝外す機種が変わった（ページを作り直す）／0＝変わらない／
                           2＝控え・結び付けが読めない（何も書かない）
  --unbound [--limit N]  … まだDMMと結び付けていない対象機種（ページに出る機種が先・残りの候補が後）
  --list                 … いまの記録と、外している機種
  --check                … 番人用。★外した機種も含めた★対象機種のうち、結び付け済みなのに記録が無い・
                           STALE_DAYS より古い・結び付けと記録のURLが違う → 1／読めない → 2
  --selftest
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE = os.path.join(BASE, "assets", "data", "hall-presence.json")
MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
SCHEMA = "hall-presence/v1"
PURPOSE = "hall_presence"
STALE_DAYS = 21
DMM_URL_RE = re.compile(r"^https://p-town\.dmm\.com/machines/[0-9]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# ★件数を読む2か所★（どちらもDMMが決まった形で出している）
META_RE = re.compile(r"【(\d+)件掲載中】")
BODY_RE = re.compile(r"設置店舗一覧\s*</[a-zA-Z0-9]+>\s*<[a-zA-Z0-9]+[^>]*>\s*(\d+)件")
TITLE_RE = re.compile(r"<title>([^<]*)</title>")
TITLE_TAIL = "（パチスロ）の設置店｜DMMぱちタウン"


class HallPresenceError(Exception):
    pass


# ───────────────────────── 読む・書く ─────────────────────────

def entry_problems(slug, e) -> list:
    ng = []
    if not isinstance(e, dict):
        return ["%s: 記録の形が違います" % slug]
    st = e.get("stores")
    if not isinstance(st, int) or isinstance(st, bool) or st < 0:
        ng.append("%s: 店舗数が0以上の整数ではありません（%r）" % (slug, st))
    ch = e.get("checked")
    if not isinstance(ch, str) or not DATE_RE.match(ch):
        ng.append("%s: 確かめた日の形が違います（%r）" % (slug, ch))
    else:
        try:
            datetime.date.fromisoformat(ch)
        except ValueError:
            ng.append("%s: 確かめた日が実在しません（%s）" % (slug, ch))
    if not isinstance(e.get("url"), str) or not DMM_URL_RE.match(e["url"]):
        ng.append("%s: URLがDMMの機種ページの形ではありません" % slug)
    extra = set(e) - {"stores", "checked", "url"}
    if extra:
        ng.append("%s: 知らない項目があります（%s）" % (slug, sorted(extra)))
    return ng


def read(path: str = None) -> dict:
    """★控えを読む★（無い・壊れている・形が違う → 例外。黙って「外す機種なし」にしない）"""
    path = path or STORE
    import safe_json as _sj
    data = _sj.read_json(path, expect=dict)
    if data.get("schema") != SCHEMA:
        raise HallPresenceError("設置の控えの版が違います: %s" % path)
    line = data.get("line")
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        raise HallPresenceError("線（line）が1以上の整数ではありません: %r" % (line,))
    if len(str(data.get("line_why") or "").strip()) < 15:
        raise HallPresenceError("線を決めた理由（line_why）が15字に足りません")
    ms = data.get("machines")
    if not isinstance(ms, dict):
        raise HallPresenceError("machines が辞書ではありません")
    bad = []
    for s, e in ms.items():
        if not isinstance(s, str) or not re.match(r"^[a-z0-9_]+$", s):
            bad.append("機種の名前の形が違います（%r）" % (s,))
            continue
        bad += entry_problems(s, e)
    tries = data.get("tries", {})
    if not isinstance(tries, dict):
        bad.append("tries が辞書ではありません")
    else:
        for s, d in tries.items():
            if not isinstance(d, str) or not DATE_RE.match(d):
                bad.append("%s: 試した日の形が違います（%r）" % (s, d))
    extra = set(data) - {"schema", "line", "line_why", "machines", "tries"}
    if extra:
        bad.append("知らない項目があります（%s）" % sorted(extra))
    if bad:
        raise HallPresenceError("／".join(bad[:4]))
    return data


def excluded(data: dict = None) -> set:
    """★ランキングから外す機種★＝記録があり、店舗数が線より少ない機種"""
    data = read() if data is None else data
    line = data["line"]
    return {s for s, e in data["machines"].items() if e["stores"] < line}


def _save(data: dict, path: str = None) -> None:
    path = path or STORE
    read_back_check = dict(data)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(read_back_check, fh, ensure_ascii=False, indent=1, sort_keys=False)
        fh.write("\n")
    os.replace(tmp, path)
    read(path)                       # ★書いたら読み直す★（形が崩れていれば止める）


# ───────────────────────── 件数を読む ─────────────────────────

def read_count(html: str) -> int:
    """★設置店ページから全国の店舗数を読む★（2か所がそろわなければ例外）"""
    t = TITLE_RE.findall(html or "")
    if len(t) != 1 or not t[0].strip().endswith(TITLE_TAIL):
        raise HallPresenceError("パチスロの設置店のページではありません（題: %s）"
                                % (t[0][:60] if t else "なし"))
    meta = META_RE.findall(html)
    body = BODY_RE.findall(html)
    if len(meta) != 1 or len(body) != 1:
        raise HallPresenceError("件数の出ている場所がちょうど1つずつではありません（説明文%d・本文%d）"
                                % (len(meta), len(body)))
    if meta[0] != body[0]:
        raise HallPresenceError("説明文と本文で件数が違います（%s と %s）" % (meta[0], body[0]))
    return int(meta[0])


def _fetch_shops(url: str) -> str:
    """★結び付けたURLの /shops だけを取る★（転送されたら使わない）"""
    import new_machine_watch as _nw
    want = url + "/shops"
    with _nw.fetching(PURPOSE):
        html = _nw._get(want)
    got = str(_nw.LAST_FINAL_URL.get("url") or "").rstrip("/")
    if got != want:
        raise HallPresenceError("別のページへ転送されています（%s → %s）" % (want, got))
    return html


# ───────────────────────── 対象の機種 ─────────────────────────

def _rows():
    import build_hub_pages as _bh
    return _bh.load_rows()


def candidates(rows=None) -> list:
    """★2つのランキングに入りうる機種★（外す前の並び・天井 → リセットの順・重複なし）"""
    import build_hub_pages as _bh
    rows = _rows() if rows is None else rows
    ranked = _bh.ranked_rows(rows)
    seen = []
    for r in _bh.dataset_A(ranked) + _bh.dataset_C(ranked):
        if r["slug"] not in seen:
            seen.append(r["slug"])
    return seen


def visible(rows=None) -> list:
    """★ページに出る機種★（外したあとの並び・天井は全部・リセットは上位 RESET_SHOWN）"""
    import build_hub_pages as _bh
    rows = _rows() if rows is None else rows
    pool = _bh.ranking_pool(rows)
    seen = []
    for r in _bh.dataset_A(pool) + _bh.dataset_C(pool)[:_bh.RESET_SHOWN]:
        if r["slug"] not in seen:
            seen.append(r["slug"])
    return seen


def bound_url(slug: str):
    """★2AIが結んだDMMの機種ページ★（無ければ None・使えない結び付けは例外）"""
    import legacy_binding as _lb
    e = _lb.load()["bindings"].get(slug)
    if not e:
        return None
    url = _lb.norm_url(e.get("url"))
    got = _lb.slug_for_url(url)          # 封・名前・重なりの検査を同じ道で通す
    if not got or got[0] != slug:
        raise _lb.BindingError("%s の結び付けを引き直すと別の機種になります" % slug)
    return url


def auto_urls(machines=None) -> dict:
    """★新台経路の機種 → DMMの機種ページ★（2026-11-09・新台もランキングに入るため）

    記事を作るときにDMMの機種ページで確かめた identity を使う。★形が違えば None★
    （＝使えない・壊れとして扱う。推測しない）。旧形式の機種は入れない。
    """
    import safe_json as _sj
    import legacy_binding as _lb
    ms = _sj.read_rows(MACHINES) if machines is None else machines
    out = {}
    for m in ms:
        if not isinstance(m, dict) or "publication_policy" not in m:
            continue
        idt = m.get("identity") if isinstance(m.get("identity"), dict) else {}
        u = _lb.norm_url(idt.get("official_product_url"))
        ok = idt.get("identity_binding") == "DMM_MACHINE_PAGE" and bool(DMM_URL_RE.match(u or ""))
        out[str(m.get("slug") or "")] = u if ok else None
    return out


def page_url(slug: str, autos: dict):
    """★その機種のDMMの機種ページ★（取り直しと点検が同じ道を通る・無ければ None）"""
    if slug in autos:
        if not autos[slug]:
            raise HallPresenceError("%s の識別（DMMの機種ページ）が使えない形です" % slug)
        return autos[slug]
    return bound_url(slug)


# ───────────────────────── 取り直す ─────────────────────────

def refresh(n: int, apply: bool, fetch=None, today: str = None, rows=None,
            path: str = None, log=print, only=None) -> dict:
    """only … 担当が回ってきた機種だけを取り直す（朝の STEP 1-T）。ランキングの候補でなければ何もしない"""
    data = read(path)
    data.setdefault("tries", {})
    before = excluded(data)
    today = today or datetime.date.today().isoformat()
    fetch = fetch or _fetch_shops
    cands = candidates(rows)
    if only is not None:
        cands = [s for s in cands if s in only]
    # ★控え全体が読めないときは機種ごとの失敗にしない★（Codex）＝84件すべてが「使えない結び付け」に
    #   吸収されて終了コード0になり、手順書の「読めない→2→🟠」を通らなかった。ここで例外にする。
    import legacy_binding as _lb
    import pending_machines as _pm
    _lb.load()
    _pm.load()
    autos = auto_urls()
    bound, broken = [], []
    for s in cands:
        try:
            u = page_url(s, autos)
        except Exception as ex:                              # noqa: BLE001
            broken.append((s, str(ex)[:100]))
            continue
        if u:
            bound.append((s, u))
    # ★最後に試した日が古い順（一度も試していない機種が先）・同じ日はランキングの並び順★
    order = {s: i for i, s in enumerate(cands)}
    bound.sort(key=lambda su: (data["tries"].get(su[0], ""), order[su[0]]))
    done, failed, dropped = [], [], []
    for s, u in bound[:max(0, n)]:
        data["tries"][s] = today
        old_e = data["machines"].get(s)
        # ★結び直してURLが変わった機種の古い記録は、取り直す前に捨てる★（Codex）＝
        #   新しいURLで取れなかったときに、別のページの数を「いまの値」として残さない。
        if old_e and old_e.get("url") != u:
            del data["machines"][s]
            dropped.append(s)
            old_e = None
        try:
            st = read_count(fetch(u))
        except Exception as ex:                              # noqa: BLE001
            failed.append((s, str(ex)[:120]))
            log("読めなかった %s: %s" % (s, str(ex)[:120]))
            continue
        old = (old_e or {}).get("stores")
        data["machines"][s] = {"stores": st, "checked": today, "url": u}
        done.append((s, old, st))
        log("確かめた %s: %s → %d店%s" % (s, old, st, "（線より少ない）" if st < data["line"] else ""))
    after = excluded(data)
    if apply and (done or failed or dropped):
        data["machines"] = dict(sorted(data["machines"].items()))
        data["tries"] = dict(sorted(data["tries"].items()))
        _save(data, path)
    return {"done": done, "failed": failed, "broken": broken, "dropped": dropped,
            "added": sorted(after - before), "restored": sorted(before - after),
            "bound": len(bound), "candidates": len(cands)}


def unbound(rows=None) -> list:
    """★まだ結び付けていない対象機種★（ページに出る機種が先・外した機種を含む残りの候補が後）"""
    import legacy_binding as _lb
    b = _lb.load()["bindings"]
    autos = auto_urls()                  # ★新台は結ばない★（identity を使う）
    vis = visible(rows)
    rest = [s for s in candidates(rows) if s not in vis]
    return [s for s in vis + rest if s not in b and s not in autos]


def check(today: str = None, rows=None, path: str = None) -> tuple:
    """★番人用★ → (終了コード, 行)

    ★外した機種も見る★（Codex）＝ページに出る機種だけを見ると、外した機種の取り直しが
    失敗し続けても気づけず、増えても戻らない。
    """
    today = datetime.date.fromisoformat(today or datetime.date.today().isoformat())
    try:
        data = read(path)
        import legacy_binding as _lb
        binds = _lb.load()["bindings"]
        autos = auto_urls()
    except Exception as ex:                                  # noqa: BLE001
        return 2, ["設置の控えか結び付けが読めません: %s" % str(ex)[:150]]
    lines, stale = [], []
    cands = candidates(rows)
    for s in cands:
        # ★取り直しと同じ道でURLを決める★（Codex＝新台も点検する）
        if s in autos:
            want = autos[s]
            if not want:
                stale.append("%s（識別のDMMページが使えない形）" % s)
                continue
        elif s in binds:
            want = _lb.norm_url((binds.get(s) or {}).get("url"))
        else:
            continue
        e = data["machines"].get(s)
        if not e:
            stale.append("%s（まだ一度も確かめていない）" % s)
            continue
        if e["url"] != want:
            stale.append("%s（結び付けと記録のページが違う）" % s)
            continue
        age = (today - datetime.date.fromisoformat(e["checked"])).days
        if age > STALE_DAYS:
            stale.append("%s（%d日前）" % (s, age))
    nb = len([s for s in cands if s not in binds and s not in autos])
    lines.append("ランキングの対象機種 %d（DMMと結び付け済み %d・まだ %d）／外している機種 %d（線＝全国%d店未満）"
                 % (len(cands), len(cands) - nb, nb, len(excluded(data)), data["line"]))
    if stale:
        lines.append("★設置を%d日より長く確かめていない機種★ %d件: %s"
                     % (STALE_DAYS, len(stale), "、".join(stale[:8])))
        return 1, lines
    return 0, lines


# ───────────────────────── 自己試験 ─────────────────────────

def selftest() -> int:
    import tempfile
    import legacy_binding as _lb
    ok = ng = 0

    def t(name, cond):
        nonlocal ok, ng
        if cond:
            ok += 1
        else:
            ng += 1
            print("  ❌", name)

    def page(meta, body, title="ファンキージャグラー2（パチスロ）の設置店｜DMMぱちタウン"):
        return ('<html><head><title>%s</title><meta name="description" content="【%s件掲載中】 x">'
                '</head><body><h2 class="t">設置店舗一覧</h2>\n<span class="n">%s件</span></body></html>'
                % (title, meta, body))

    # ── 件数の読み方 ──
    t("2か所がそろえば読める", read_count(page(14, 14)) == 14)
    t("0店も読める", read_count(page(0, 0)) == 0)
    for name, html in [("2か所の数が違えば読まない", page(14, 15)),
                       ("パチンコのページは読まない", page(5, 5, "CRx（パチンコ）の設置店｜DMMぱちタウン")),
                       ("題が違えば読まない", page(5, 5, "x")),
                       ("説明文が無ければ読まない", page(5, 5).replace("【5件掲載中】", "")),
                       ("本文が無ければ読まない", page(5, 5).replace("設置店舗一覧", "一覧")),
                       ("説明文が2回あれば読まない", page(5, 5) + "【5件掲載中】")]:
        try:
            read_count(html)
            t(name, False)
        except HallPresenceError:
            t(name, True)

    # ── 控えの読み方 ──
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "hp.json")

        def put(obj):
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(obj, fh, ensure_ascii=False)

        good = {"schema": SCHEMA, "line": 100, "line_why": "全国で100店より少ない機種は探しても見つかりにくい",
                "machines": {"a": {"stores": 14, "checked": "2026-11-09",
                                   "url": "https://p-town.dmm.com/machines/1"},
                             "b": {"stores": 100, "checked": "2026-11-09",
                                   "url": "https://p-town.dmm.com/machines/2"},
                             "c": {"stores": 99, "checked": "2026-11-09",
                                   "url": "https://p-town.dmm.com/machines/3"}}}
        put(good)
        t("線より少ない機種だけ外す（ちょうど線は残す）", excluded(read(p)) == {"a", "c"})
        for name, mut in [
                ("版が違えば止める", lambda d: d.update(schema="x")),
                ("線が0なら止める", lambda d: d.update(line=0)),
                ("線が文字なら止める", lambda d: d.update(line="100")),
                ("理由が短ければ止める", lambda d: d.update(line_why="短い")),
                ("店舗数が負なら止める", lambda d: d["machines"]["a"].update(stores=-1)),
                ("店舗数が真偽値なら止める", lambda d: d["machines"]["a"].update(stores=True)),
                ("日付が実在しなければ止める", lambda d: d["machines"]["a"].update(checked="2026-02-30")),
                ("URLがDMMでなければ止める", lambda d: d["machines"]["a"].update(url="https://x.example/1")),
                ("知らない項目があれば止める", lambda d: d["machines"]["a"].update(excluded=True))]:
            d = json.loads(json.dumps(good))
            mut(d)
            put(d)
            try:
                read(p)
                t(name, False)
            except Exception:                                # noqa: BLE001
                t(name, True)
        try:
            read(os.path.join(td, "none.json"))
            t("控えが無ければ止める", False)
        except Exception:                                    # noqa: BLE001
            t("控えが無ければ止める", True)

        # ── 取り直し（結び付けと取得を差し替え） ──
        put(dict(good, machines={}))
        keep = (_lb.load, _lb.slug_for_url, globals()["candidates"])
        binds = {"s1": {"url": "https://p-town.dmm.com/machines/11"},
                 "s2": {"url": "https://p-town.dmm.com/machines/12"}}
        try:
            _lb.load = lambda: {"bindings": binds}
            _lb.slug_for_url = lambda u: next(((s, "n") for s, e in binds.items()
                                               if e["url"] == u), None)
            globals()["candidates"] = lambda rows=None: ["s1", "s2", "s3"]
            got = {"https://p-town.dmm.com/machines/11": page(14, 14),
                   "https://p-town.dmm.com/machines/12": page(500, 500)}
            r = refresh(5, False, fetch=lambda u: got[u], today="2026-11-09", path=p, log=lambda *_: None)
            t("見るだけでは書かない", read(p)["machines"] == {})
            t("結び付けの無い機種は確かめない", [x[0] for x in r["done"]] == ["s1", "s2"])
            r = refresh(5, True, fetch=lambda u: got[u], today="2026-11-09", path=p, log=lambda *_: None)
            t("外す機種が増えたと返す", r["added"] == ["s1"])
            t("書いた記録で s1 だけ外れる", excluded(read(p)) == {"s1"})
            got["https://p-town.dmm.com/machines/11"] = page(300, 300)
            r = refresh(1, True, fetch=lambda u: got[u], today="2026-11-10", path=p, log=lambda *_: None)
            t("古い順に1件だけ取り直す", [x[0] for x in r["done"]] == ["s1"])
            _ro = refresh(5, False, fetch=lambda u: got[u], today="2026-11-10", path=p, log=lambda *_: None,
                          only=["s2"])
            t("★★担当の回（only）は、その機種だけ取り直す★★", [x[0] for x in _ro["done"]] == ["s2"])
            t("増えたら戻す", r["restored"] == ["s1"] and excluded(read(p)) == set())
            got["https://p-town.dmm.com/machines/12"] = page(5, 6)
            r = refresh(5, True, fetch=lambda u: got[u], today="2026-11-11", path=p, log=lambda *_: None)
            t("読めなかった機種は前の記録を残す",
              read(p)["machines"]["s2"]["stores"] == 500 and [x[0] for x in r["failed"]] == ["s2"])
            # ── 結び付けの控えそのものが読めなければ止める（Codex）──
            def _boom():
                raise _lb.BindingError("壊れた控え")
            _lb.load = _boom
            try:
                refresh(5, True, fetch=lambda u: got[u], today="2026-11-11", path=p, log=lambda *_: None)
                t("★結び付けの控えが読めなければ止める★", False)
            except _lb.BindingError:
                t("★結び付けの控えが読めなければ止める★", True)
            _lb.load = lambda: {"bindings": binds}
            # ── 取れない機種が先頭に居座らない（Codex）──
            put(dict(good, machines={}))
            got2 = {"https://p-town.dmm.com/machines/11": "壊れたページ",
                    "https://p-town.dmm.com/machines/12": page(500, 500)}
            r = refresh(1, True, fetch=lambda u: got2[u], today="2026-11-09", path=p, log=lambda *_: None)
            t("1日目は並び順の先頭（取れない）", [x[0] for x in r["failed"]] == ["s1"])
            r = refresh(1, True, fetch=lambda u: got2[u], today="2026-11-10", path=p, log=lambda *_: None)
            t("2日目は取れなかった機種を後ろへ回し、ほかを確かめる", [x[0] for x in r["done"]] == ["s2"])
            t("取れなかった日は確かめた日にしない", "s1" not in read(p)["machines"])
            # ── 結び直したら古い記録を残さない（Codex）──
            binds["s2"] = {"url": "https://p-town.dmm.com/machines/22"}
            got2["https://p-town.dmm.com/machines/22"] = "壊れたページ"
            r = refresh(5, True, fetch=lambda u: got2[u], today="2026-11-11", path=p, log=lambda *_: None)
            t("新しいページが取れなければ古い記録を捨てる",
              "s2" not in read(p)["machines"] and r["dropped"] == ["s2"])
            got2["https://p-town.dmm.com/machines/22"] = page(30, 30)
            r = refresh(5, True, fetch=lambda u: got2[u], today="2026-11-12", path=p, log=lambda *_: None)
            t("新しいページの数で外す", excluded(read(p)) == {"s2"})
            # ── 番人（外した機種も見る）──
            got2["https://p-town.dmm.com/machines/11"] = page(300, 300)
            refresh(5, True, fetch=lambda u: got2[u], today="2026-11-12", path=p, log=lambda *_: None)
            code, _ = check(today="2026-11-20", path=p)
            t("21日以内なら0", code == 0)
            # 外した機種（s2）だけが古い状態を作る＝ページに出る機種だけを見ると気づけない
            got2["https://p-town.dmm.com/machines/22"] = page(30, 30)
            refresh(1, True, fetch=lambda u: got2[u], today="2026-11-30", path=p, log=lambda *_: None)
            keepv = globals()["visible"]
            globals()["visible"] = lambda rows=None: ["s1", "s3"]
            try:
                code, _ = check(today="2026-12-04", path=p)
                t("★外した機種が古くなっても気づく★", code == 1 and excluded(read(p)) == {"s2"})
            finally:
                globals()["visible"] = keepv
            code, _ = check(today="2026-12-05", path=p)
            t("21日より古ければ1（外した機種も含む）", code == 1)
            binds["s3"] = {"url": "https://p-town.dmm.com/machines/13"}
            code, _ = check(today="2026-11-20", path=p)
            t("結び付けたのに確かめていなければ1", code == 1)
            del binds["s3"]
            binds["s1"] = {"url": "https://p-town.dmm.com/machines/31"}
            code, _ = check(today="2026-11-20", path=p)
            t("結び付けと記録のページが違えば1", code == 1)
            binds["s1"] = {"url": "https://p-town.dmm.com/machines/11"}
            put({"schema": "x"})
            code, _ = check(today="2026-11-20", path=p)
            t("読めなければ2", code == 2)
            # ── 新台（記事を作るときに確かめたDMMの機種ページを使う・結ばない）2026-11-09 ──
            keepa = globals()["auto_urls"]
            try:
                put(dict(good, machines={}))
                u13 = "https://p-town.dmm.com/machines/13"
                globals()["auto_urls"] = lambda machines=None: {"s3": u13}
                got3 = dict(got2)
                got3[u13] = page(40, 40)
                r = refresh(5, True, fetch=lambda u: got3[u], today="2026-11-20", path=p, log=lambda *_: None)
                t("★新台は識別のDMMページで確かめる（結び付けなし）★",
                  "s3" in [x[0] for x in r["done"]] and "s3" in excluded(read(p)))
                t("★新台は結び付けの候補に出さない★", "s3" not in unbound())
                code, _ = check(today="2026-11-21", path=p)
                t("確かめた新台は古くなければ0", code == 0)
                globals()["auto_urls"] = lambda machines=None: {"s3": "https://p-town.dmm.com/machines/14"}
                code, _ = check(today="2026-11-21", path=p)
                t("★点検も新台を見る（識別のページと記録のページが違えば1）★", code == 1)
                globals()["auto_urls"] = lambda machines=None: {"s3": None}
                r = refresh(5, True, fetch=lambda u: got3[u], today="2026-11-21", path=p, log=lambda *_: None)
                code, _ = check(today="2026-11-21", path=p)
                t("★識別のページが使えない形の新台は確かめず、点検で1★",
                  [x[0] for x in r["broken"]] == ["s3"] and code == 1)
            finally:
                globals()["auto_urls"] = keepa
            ms_t = [{"slug": "a1", "publication_policy": "page-decision/v1",
                     "identity": {"official_product_url": "https://p-town.dmm.com/machines/5104/",
                                  "identity_binding": "DMM_MACHINE_PAGE"}},
                    {"slug": "a2", "publication_policy": "page-decision/v1",
                     "identity": {"official_product_url": "https://p-town.dmm.com/machines/5105",
                                  "identity_binding": "LIST_CARD"}},
                    {"slug": "a3", "publication_policy": "page-decision/v1",
                     "identity": {"official_product_url": "https://example.com/machines/1",
                                  "identity_binding": "DMM_MACHINE_PAGE"}},
                    {"slug": "old", "identity": {"official_product_url": "https://p-town.dmm.com/machines/1",
                                                 "identity_binding": "DMM_MACHINE_PAGE"}}]
            t("★新台の識別：DMMの機種ページで確かめた形だけ使う・旧形式は入れない★",
              auto_urls(ms_t) == {"a1": "https://p-town.dmm.com/machines/5104", "a2": None, "a3": None})
        finally:
            _lb.load, _lb.slug_for_url, globals()["candidates"] = keep

    # ── 本番の控え ──
    try:
        read()
        t("本番の控えが読める", True)
    except Exception as ex:                                  # noqa: BLE001
        print("  ", ex)
        t("本番の控えが読める", False)

    print("hall_presence 自己試験: %d/%d 合格" % (ok, ok + ng))
    return 0 if ng == 0 else 1


# ───────────────────────── 入口 ─────────────────────────

def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--refresh", type=int)
    g.add_argument("--unbound", action="store_true")
    g.add_argument("--list", action="store_true")
    g.add_argument("--check", action="store_true")
    g.add_argument("--selftest", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--slug", nargs="+", help="担当が回ってきた機種だけ取り直す（--refresh と一緒に）")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.check:
        code, lines = check()
        for x in lines:
            print(x)
        return code
    if a.unbound:
        try:
            xs = unbound()
        except Exception as ex:                              # noqa: BLE001
            print("読めません:", ex)
            return 2
        for s in (xs[:a.limit] if a.limit else xs):
            print(s)
        print("（まだDMMと結び付けていない対象機種 %d件・ページに出る機種が先）" % len(xs))
        return 0
    if a.list:
        data = read()
        ex = excluded(data)
        for s, e in data["machines"].items():
            print("%s %d店 %s%s" % (s, e["stores"], e["checked"], "  ★外している" if s in ex else ""))
        print("線＝全国%d店未満を外す／外している機種 %d" % (data["line"], len(ex)))
        return 0
    try:
        r = refresh(a.refresh, a.apply, only=a.slug)
    except Exception as ex:                                  # noqa: BLE001
        print("控えか結び付けが読めないので何もしません:", ex)
        return 2
    for s, why in r["broken"]:
        print("結び付けが使えない %s: %s" % (s, why))
    print("確かめた %d件・読めなかった %d件・結び付けが使えない %d件（対象 %d・結び付け済み %d）%s"
          % (len(r["done"]), len(r["failed"]), len(r["broken"]), r["candidates"], r["bound"],
             "" if a.apply else "　★見るだけ（--apply なし）★"))
    if r["added"]:
        print("★新しく外す機種★", "、".join(r["added"]))
    if r["restored"]:
        print("★ランキングへ戻す機種★", "、".join(r["restored"]))
    return 10 if (r["added"] or r["restored"]) else 0


if __name__ == "__main__":
    sys.exit(main())

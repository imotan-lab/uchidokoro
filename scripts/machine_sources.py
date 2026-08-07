# -*- coding: utf-8 -*-
"""機種ごとの「この機種のページはここ」という控え。

★何のためにあるか★（2026-08-07・台帳#265）
  情報を1つ決めるには「大手2つが同じことを書いている」ことが要る。
  ところが名鑑（一覧ページ）から機種名で引く方法では、表記が違う機種を
  引き当てられない。実データで全121機種のうち38機種が2つに届かなかった。
    ・スマスロ防振り        ↔ 痛いのは嫌なので防御力に極振りしたいと思います
    ・SBニューキングハナハナV-30 ↔ ニューキングハナハナV-30
    ・LB不二子BT            ↔ 不二子BT
  これは「同じ機種か」という**意味の判断**なので、機械の照合では届かない。

★そこでAIに探させる。ただし守る線が1本ある★
  ┌────────────────────────────────────────────────┐
  │ AIが挙げたURLは、機械が実際に取ってきて          │
  │ 中身を確かめるまで採用しない                     │
  └────────────────────────────────────────────────┘
  過去にCodexが挙げたURLが404だった実例がある。この線さえ引いておけば、
  AIが間違ったURLを出しても無害＝存在しなければ落ち、別機種のページなら
  中身を読んだ時点で外れる。嘘のURLが記事に化ける経路が無い。

★探す先は登録済みの大手サイトだけ★（source-registry.json・default deny）
  「2つの出典が一致したら採用」の"2つ"を数えるには、その2つが本当に
  別系列かを知っている必要がある（P-WORLDと羽伏せは同じ系列で1票）。
  知らないサイトが出てくると独立性を数えられないので、票にできない。

★一度決めたら控えに残る★
  次からはAIを呼ばず機械が読むだけ。費用は機種あたり1回きり。

★使い方★
  # ①機械の検査（AIに渡す材料を出す。ここでは何も記録しない）
  python scripts/machine_sources.py --check --slug bofuri --url https://...
  # ②AIが「同じ機種だ」と判断したら記録する
  python scripts/machine_sources.py --record --slug bofuri --url https://... \
      --why "正式名称の略称。型式が一致" --by claude
  #   ★名前の形が合わないときは、判断した理由を書いて明示的に上書きする★
  #      --override-identity "略称なので題は一致しないが、型式番号が一致"
  # ③控えを見る / 手当てが要る機種を並べる
  python scripts/machine_sources.py --list [--slug bofuri]
  python scripts/machine_sources.py --missing
  python scripts/machine_sources.py --selftest
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import claim_identity as _ci          # noqa: E402
import model_code_lookup as _mc       # noqa: E402
import new_machine_watch as _w        # noqa: E402
import safe_json as _sj               # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY = os.path.join(BASE, "assets", "data", "source-registry.json")
MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
# ★控えはリポジトリの外★（release_overrides.json と同じ置き場）
#   公開物に他サイトのURL一覧を混ぜないため。控えはDropboxへ保全する。
STORE = r"C:/Users/imao_/Documents/uchidokoro/machine_sources.json"

SCHEMA = "machine-sources/v1"


class SourceError(Exception):
    """控えに関する異常（★迷ったら記録しない★）。"""


# ---------------------------------------------------------------- 出典の台帳

def _publishers() -> dict:
    """ホスト名 → (発行者ID, 系列ID) の対応。★ACTIVE だけ★"""
    reg = _sj.read_json(REGISTRY, expect=dict)
    out = {}
    for pid, p in (reg.get("publishers") or {}).items():
        if p.get("status") != "ACTIVE":
            continue
        for h in p.get("canonical_hosts") or []:
            out[str(h).lower()] = (pid, p.get("content_lineage_id") or "")
    return out


def publisher_of(url: str, pubs: dict | None = None):
    """URLの発行者と系列を返す。★登録が無ければ (None, None)★"""
    host = urllib.parse.urlsplit(str(url or "")).hostname or ""
    return (pubs if pubs is not None else _publishers()).get(host.lower(),
                                                             (None, None))


# ---------------------------------------------------------------- 控えの読み書き

def _empty() -> dict:
    return {"schema_version": SCHEMA, "machines": {}}


def load() -> dict:
    if not os.path.exists(STORE):
        return _empty()
    got = _sj.read_json(STORE, expect=dict)
    if got.get("schema_version") != SCHEMA:
        raise SourceError(f"控えの形が違います: {got.get('schema_version')}")
    got.setdefault("machines", {})
    return got


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, STORE)


def urls_for(slug: str, data: dict | None = None) -> list:
    """控えに入っている、この機種の出典（機械が毎日読む側）。"""
    d = data if data is not None else load()
    return list((d.get("machines") or {}).get(slug) or [])


# ---------------------------------------------------------------- 機種の情報

def machine(slug: str) -> dict:
    ms = _sj.read_json(MACHINES, expect=(dict, list))
    ms = ms["machines"] if isinstance(ms, dict) else ms
    for m in ms:
        if m.get("slug") == slug:
            return m
    raise SourceError(f"機種が見つかりません: {slug}")


def _text_of(html: str) -> str:
    return " ".join(_w._visible_text(html).split())


# ---------------------------------------------------------------- 機械の検査

def check(slug: str, url: str, html: str | None = None,
          pubs: dict | None = None) -> dict:
    """★AIに渡す材料を作る（記録はしない）★

    機械にできることだけを見る：
      ①登録済みの発行者か ②取れるか ③別のホストへ飛ばされないか
      ④中身から題・見出し・型式・機種名の出方を抜き出す
    「同じ機種か」は**ここでは決めない**（それがAIの仕事）。
    """
    m = machine(slug)
    name = str(m.get("name") or "")
    out = {"slug": slug, "name": name, "url": url, "ok": False,
           "problems": [], "publisher": None, "lineage": None,
           "title": "", "headings": [], "model_code": None,
           "name_core": _ci.normalize_core(name),
           "identity_verdict": None, "identity_why": "",
           "excerpt": "", "text_sha256": "", "text_len": 0,
           "already_recorded": False, "same_lineage_already": []}

    pid, lin = publisher_of(url, pubs)
    if not pid:
        out["problems"].append(
            "登録されていないサイトです（票に数えられないので使いません）")
        return out
    out["publisher"], out["lineage"] = pid, lin

    if html is None:
        try:
            html = _w._get(url)
        except Exception as e:              # noqa: BLE001
            out["problems"].append(f"取得できません（{e}）")
            return out
        final = _w.LAST_FINAL_URL.get("url") or url
        bad = _w.redirect_problem(url, final)
        if bad:
            out["problems"].append(f"転送されました（{bad}）")
            return out
        if publisher_of(final, pubs)[0] != pid:
            out["problems"].append("別のサイトへ飛ばされました")
            return out
    why = _w.bad_page(html)
    if why:
        out["problems"].append(f"一覧・記事のページではありません（{why}）")
        return out

    text = _text_of(html)
    out["title"] = _w.page_title(html) or ""
    out["headings"] = [h[:80] for h in _w._visible_h1s(html)][:5]
    out["model_code"] = _mc.extract_model_code(html)
    out["text_len"] = len(text)
    out["text_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    out["excerpt"] = text[:400]
    ok, reason = _mc.page_is_machine(html, name)
    out["identity_verdict"], out["identity_why"] = bool(ok), str(reason or "")

    if not out["title"]:
        out["problems"].append("題がありません（機種のページか確かめられません）")
        return out
    if out["text_len"] < 200:
        out["problems"].append("本文がほとんどありません")
        return out

    data = load()
    for rec in urls_for(slug, data):
        if rec.get("url") == url:
            out["already_recorded"] = True
        elif rec.get("lineage") == lin:
            out["same_lineage_already"].append(rec.get("url"))
    out["ok"] = True
    return out


# ---------------------------------------------------------------- 記録

def record(slug: str, url: str, why: str, by: list,
           override_identity: str = "", checked: dict | None = None) -> dict:
    """★検査を通ったものだけを控えに残す★（fail-closed）"""
    if not str(why or "").strip():
        raise SourceError("--why（なぜ同じ機種と判断したか）は必ず書きます")
    who = [x for x in (by or []) if x]
    if not who:
        raise SourceError("--by（誰が判断したか）は必ず書きます")
    got = checked if checked is not None else check(slug, url)
    if not got["ok"]:
        raise SourceError("機械の検査を通りません: " + " / ".join(got["problems"]))
    if got["already_recorded"]:
        return {"state": "ALREADY", "url": url}
    # ★名前の形が合わないときは、判断した理由を明示的に書かせる★
    #   黙って通すと「近いから同じでいいや」が積み上がる。
    if not got["identity_verdict"] and not str(override_identity or "").strip():
        raise SourceError(
            "題が機種名と一致しません（" + got["identity_why"] + "）。"
            "同じ機種だと判断したなら --override-identity に理由を書きます")

    data = load()
    rec = {
        "url": url,
        "publisher": got["publisher"],
        "lineage": got["lineage"],
        "title": got["title"][:120],
        "decided_at": datetime.date.today().isoformat(),
        "decided_by": who,
        "why": str(why).strip()[:300],
        # ★どの中身を見て決めたか★（後から同じものを見たか確かめられる）
        "text_sha256": got["text_sha256"],
        "identity_verdict": got["identity_verdict"],
    }
    if override_identity:
        rec["override_identity"] = str(override_identity).strip()[:300]
    data["machines"].setdefault(slug, []).append(rec)
    _save(data)
    return {"state": "RECORDED", "url": url, "lineage": got["lineage"],
            "sources_now": len(data["machines"][slug])}


def forget(slug: str, url: str) -> dict:
    """判断が間違っていたときに控えから外す（★人の操作専用★）。"""
    data = load()
    rows = urls_for(slug, data)
    left = [r for r in rows if r.get("url") != url]
    if len(left) == len(rows):
        return {"state": "NOT_FOUND"}
    data["machines"][slug] = left
    if not left:
        data["machines"].pop(slug, None)
    _save(data)
    return {"state": "FORGOTTEN", "sources_now": len(left)}


# ---------------------------------------------------------------- 手当てが要る機種

def missing(limit: int = 0) -> list:
    """2つの系列に届かない機種を並べる（★AIに探させる対象★）。"""
    import directory_index as _di

    cats = _sj.read_json(_di.CATALOGS, expect=dict)["directories"]
    scans = {k: _di.scan_directory(k, c) for k, c in cats.items()
             if c.get("status") == "ACTIVE"}
    ms = _sj.read_json(MACHINES, expect=(dict, list))
    ms = ms["machines"] if isinstance(ms, dict) else ms
    data = load()
    rows = []
    for m in ms:
        core = _ci.normalize_core(m.get("name") or "")
        lins, seen = [], set()
        for dir_id, r in scans.items():
            if not _di.lookup_hits(r["index"], core):
                continue
            lin = (cats[dir_id].get("content_lineage_id")
                   or "dir:" + dir_id)
            if lin not in seen:
                seen.add(lin)
                lins.append(dir_id)
        for rec in urls_for(m["slug"], data):
            if rec.get("lineage") not in seen:
                seen.add(rec.get("lineage"))
                lins.append(rec.get("publisher"))
        if len(seen) < 2:
            rows.append({"slug": m["slug"], "name": m.get("name"),
                         "have": lins})
    return rows[:limit] if limit else rows


# ---------------------------------------------------------------- 表示

def _print_check(got: dict) -> None:
    print("■ %s（%s）" % (got["name"], got["slug"]))
    print("  URL     : " + got["url"])
    print("  発行者  : %s / 系列 %s" % (got["publisher"], got["lineage"]))
    if got["problems"]:
        for p in got["problems"]:
            print("  ★使えません★ " + p)
        return
    print("  題      : " + got["title"][:110])
    for h in got["headings"]:
        print("  見出し  : " + h)
    # ★型式は組で返ることがある★（%書式に組を渡すと落ちるので必ず文字にする）
    print("  型式    : " + (str(got["model_code"]) if got["model_code"]
                            else "（見つかりません）"))
    print("  本文    : %d文字 / sha256 %s" % (got["text_len"],
                                              got["text_sha256"][:12]))
    print("  名前の形: %s（%s）"
          % ("一致" if got["identity_verdict"] else "★一致しません★",
             got["identity_why"]))
    if got["already_recorded"]:
        print("  ※すでに控えにあります")
    if got["same_lineage_already"]:
        print("  ※同じ系列の出典がすでにあります（票は増えません）: "
              + ", ".join(str(u) for u in got["same_lineage_already"]))
    print("  抜粋    : " + got["excerpt"][:300])


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    import tempfile

    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    global STORE
    keep = STORE
    tmpdir = tempfile.mkdtemp()
    STORE = os.path.join(tmpdir, "machine_sources.json")
    pubs = {"chonborista.com": ("chonborista", "lin-chonborista"),
            "nana-press.com": ("nana-press", "lin-nana-press")}
    ms = _sj.read_json(MACHINES, expect=(dict, list))
    ms = ms["machines"] if isinstance(ms, dict) else ms
    slug = ms[0]["slug"]
    name = ms[0]["name"]
    body = ("<title>" + name + " スロット 新台 天井 | ちょんぼりすた</title>"
            "<body><h1>" + name + "</h1><p>" + ("天井は999Gです。" * 40)
            + "</p></body>")

    try:
        t("★★登録されていないサイトは使わない★★（票に数えられない）",
          check(slug, "https://example.com/x", html=body, pubs=pubs)["problems"])

        got = check(slug, "https://chonborista.com/slot/a/1",
                    html=body, pubs=pubs)
        t("　登録済みなら発行者と系列が付く",
          got["publisher"] == "chonborista"
          and got["lineage"] == "lin-chonborista")
        t("　題・見出し・本文の指紋を材料として出す",
          got["title"] and got["headings"] and len(got["text_sha256"]) == 64)
        t("　本文が短すぎるページは使わない",
          not check(slug, "https://chonborista.com/slot/a/2",
                    html="<title>x</title><body>短い</body>",
                    pubs=pubs)["ok"])

        try:
            record(slug, got["url"], why="", by=["claude"], checked=got)
            ok = False
        except SourceError:
            ok = True
        t("★★なぜ同じ機種かを書かずには記録できない★★", ok)

        try:
            record(slug, got["url"], why="題が一致", by=[], checked=got)
            ok = False
        except SourceError:
            ok = True
        t("　誰が判断したかを書かずには記録できない", ok)

        bad = dict(got, ok=False, problems=["取得できません"])
        try:
            record(slug, got["url"], why="x", by=["claude"], checked=bad)
            ok = False
        except SourceError:
            ok = True
        t("★★機械の検査を通らないものは記録しない★★（AIの言い分だけでは残さない）",
          ok)

        ng = dict(got, identity_verdict=False, identity_why="TITLE_MISMATCH")
        try:
            record(slug, got["url"], why="x", by=["claude"], checked=ng)
            ok = False
        except SourceError:
            ok = True
        t("★★題が合わないときは理由を明示しないと記録できない★★", ok)

        r = record(slug, got["url"], why="x", by=["claude"],
                   override_identity="略称だが型式が一致", checked=ng)
        t("　理由を書けば記録できる", r["state"] == "RECORDED")
        t("　控えから読み出せる（機械が毎日使う側）",
          [x["url"] for x in urls_for(slug)] == [got["url"]])
        t("　同じURLは二重に入らない",
          record(slug, got["url"], why="x", by=["claude"],
                 checked=dict(got, already_recorded=True))["state"] == "ALREADY")
        t("　控えの形が読み出せる", load()["schema_version"] == SCHEMA)

        got2 = check(slug, "https://chonborista.com/slot/a/9",
                     html=body, pubs=pubs)
        t("★★同じ系列がすでにあるときは知らせる★★（票は増えない）",
          got2["same_lineage_already"] == [got["url"]])

        t("　間違いは控えから外せる",
          forget(slug, got["url"])["state"] == "FORGOTTEN"
          and urls_for(slug) == [])
        t("　無いものを外そうとしても壊れない",
          forget(slug, got["url"])["state"] == "NOT_FOUND")
    finally:
        STORE = keep

    bad = sum(1 for _, ok in results if not ok)
    print()
    print("%d/%d 合格" % (len(results) - bad, len(results)))
    return 1 if bad else 0


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser(description="機種ごとの出典URLの控え")
    ap.add_argument("--check", action="store_true",
                    help="機械の検査だけ（AIに渡す材料を出す）")
    ap.add_argument("--record", action="store_true", help="控えに残す")
    ap.add_argument("--forget", action="store_true", help="控えから外す")
    ap.add_argument("--list", action="store_true", help="控えを見る")
    ap.add_argument("--missing", action="store_true",
                    help="2つの系列に届かない機種を並べる")
    ap.add_argument("--slug")
    ap.add_argument("--url")
    ap.add_argument("--why", default="")
    ap.add_argument("--by", default="",
                    help="判断した人（claude / codex / 運営者。カンマ区切り）")
    ap.add_argument("--override-identity", default="",
                    help="題が機種名と一致しないときの理由")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()
    try:
        if a.check:
            if not (a.slug and a.url):
                print("--slug と --url が要ります")
                return 2
            _print_check(check(a.slug, a.url))
            return 0
        if a.record:
            if not (a.slug and a.url):
                print("--slug と --url が要ります")
                return 2
            r = record(a.slug, a.url, a.why,
                       [x.strip() for x in a.by.split(",") if x.strip()],
                       a.override_identity)
            print(json.dumps(r, ensure_ascii=False))
            return 0
        if a.forget:
            print(json.dumps(forget(a.slug, a.url), ensure_ascii=False))
            return 0
        if a.missing:
            rows = missing()
            print("★2つの系列に届かない機種: %d件★" % len(rows))
            for r in rows:
                print("  %-22s %s  （いま: %s）"
                      % (r["slug"], r["name"], "/".join(
                          str(x) for x in r["have"]) or "なし"))
            return 0
        if a.list:
            data = load()
            for slug, rows in sorted((data.get("machines") or {}).items()):
                if a.slug and slug != a.slug:
                    continue
                print("■ " + slug)
                for r in rows:
                    print("   [%s] %s" % (r.get("lineage"), r.get("url")))
                    print("      %s ／ %s（%s）"
                          % (r.get("why"), ",".join(r.get("decided_by") or []),
                             r.get("decided_at")))
            return 0
    except SourceError as e:
        print("★" + str(e) + "★")
        return 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

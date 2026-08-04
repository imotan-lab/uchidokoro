#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""grow_machine.py — 新台経路の機種を「育てて」検索に載せる専用の書き込み口。

★何のための道具か（2026-08-05）★
  新台は公開できるようになったが、材料が少ないうちは `AUTO_PENDING`＝
  **検索に載らない**（noindex・sitemap未掲載）。未確認の箱が埋まって
  品質ラインを越えたら `AUTO_INDEXABLE` へ上げる必要があるが、
  その経路がどのタスクにも繋がっていなかった。

★新規公開の経路を流用しない★（Codex100回目の助言）
  `publish_new_machine.py` は「新しく作る」専用で、既にあるファイルには触らない。
  上書きの経路を混ぜると、新規作成の安全策（既存を消さない）が緩む。
  ここは**上書き専用**として分け、条件を別に持つ。

★上げてよい条件（すべてAND・1つでも欠けたら何も書かない）★
  1. いまの区分がちょうど `AUTO_PENDING`
  2. 検索方針が `normal`（緊急スイッチが入っていない）
  3. 台帳に「止めるべき」案件が無い
  4. 公式（または同じ公式の一覧カード）で**本人性を確かめ直せる**
     ＝名前・メーカー・型式・登場年月が登録済みのものと**変わっていない**
  5. 材料は**増えるだけ**（既に確認済みの事実が消えたり変わったら中止）
  6. 作り直した記事・判定書・ページ・sitemap が**同時に**揃う
  7. 途中で1つでも失敗したら**全部元に戻す**

使い方:
    python scripts/grow_machine.py                 # 対象を探すだけ
    python scripts/grow_machine.py --slug xxx      # 下見（書き込まない）
    python scripts/grow_machine.py --slug xxx --apply
    python scripts/grow_machine.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_new_article as _ba          # noqa: E402
import open_issues as _oi                # noqa: E402
import page_decision as _pdz             # noqa: E402
import publish_new_machine as _pub       # noqa: E402
import safe_json as _sj                  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
DETAILS = os.path.join(BASE, "assets", "data", "machine-details")
SITEMAP = os.path.join(BASE, "sitemap.xml")


class GrowError(Exception):
    pass


def _log(msg: str) -> None:
    print(msg)


def targets(rows: list) -> list:
    """育てる対象（`AUTO_PENDING` の機種）。"""
    out = []
    for m in rows:
        try:
            if _pdz.machine_class(m) == "AUTO_PENDING":
                out.append(m["slug"])
        except _pdz.DecisionError:
            continue                       # 壊れているものは別途 audit が拾う
    return out


def identity_same(old: dict, new: dict) -> list:
    """本人性が変わっていないか（★変わっていたら育てない★）。"""
    ng = []
    for k, jp in (("manufacturer_id", "メーカー"),
                  ("regulatory_model_code", "型式名"),
                  ("announced_name", "公式の機種名"),
                  ("official_product_url", "公式URL"),
                  # ★登場年月も不変★（2026-08-05・Codex102回目の指摘3。
                  #   仕様に書いていたのに比べていなかった＝公式の誤抽出や
                  #   一時的な表記変更で「打てる時期」を書き換えられた）
                  ("market_release_date", "登場年月")):
        a, b = (old or {}).get(k), (new or {}).get(k)
        if a and b and a != b:
            ng.append(f"{jp}が変わっています（{a!r} → {b!r}）")
        if a and not b:
            ng.append(f"{jp}が取れなくなりました（登録済み: {a!r}）")
    return ng


def claims_grew(old_decision: dict, new_decision: dict) -> list:
    """材料が「増えるだけ」か（★減る・変わるのは中止★）。

    ★これだけでは足りない★（2026-08-05・Codex102回目）
      claim ID は「天井のゲーム数」までしか表さないので、
      **800G → 999G の書き換えは同じIDのまま通ってしまう**。
      値そのものは `text_kept()` で見る（前に載っていた文が消えないこと）。
    """
    old = list((old_decision or {}).get("claims") or [])
    new = list((new_decision or {}).get("claims") or [])
    lost = [c for c in old if c not in new]
    if lost:
        return [f"確認済みだった事実が消えます: {', '.join(sorted(lost)[:5])}"]
    if len(new) <= len(old):
        return ["材料が増えていません（育てるものがありません）"]
    return []


def _paragraphs(detail: dict) -> list:
    """記事の中身（未確認の箱の決まり文句は数えない）。"""
    out = []
    for s in (detail or {}).get("sections") or []:
        for b in (s.get("body") or []):
            t = str(b).strip()
            if t and t != _ba.PENDING_TEXT:
                out.append((str(s.get("title")), t))
    return out


def text_kept(old_detail: dict, new_detail: dict) -> list:
    """★前に載っていた文が、そのまま残っているか★（値の書き換えを止める）

    2026-08-05・Codex102回目の指摘2。claim ID の比較では
    「800G・AT当選」→「999G・CZ当選」を止められない。
    そこで**確認済みとして既に出ている文**を1つでも失う更新は拒否する
    （足すのは自由・消す/変えるのは不可＝本当の意味での単調追加）。
    """
    old, new = _paragraphs(old_detail), set(_paragraphs(new_detail))
    gone = [f"{t}: {b[:40]}" for t, b in old if (t, b) not in new]
    return [f"前に載っていた内容が消える/変わる更新です: {' / '.join(gone[:3])}"] \
        if gone else []


def ledger_once(slug: str, title: str, detail: str,
                severity: str = "MATERIAL") -> None:
    """★黙って止まり続けない★（2026-08-05・Codex102回目）

    確認済みだった内容が再現できなくなった時、毎日同じ理由で止まるだけだと
    **誰も気づかないまま古い内容が公開され続ける**。
    台帳へ1件だけ上げる（同じ題なら重複せず last_seen が更新される）。
    ★無人タスクは close しない★＝人が判断する。
    """
    import argparse as _ap
    try:
        _oi.cmd_add(_oi.DEFAULT_FILE, _ap.Namespace(
            source="grow-machine", slug=slug, kind="external_value",
            title=title, detail=detail, severity=severity,
            reason_code="GROW_VALUE_LOST"))
    except Exception as e:                # noqa: BLE001
        _log(f"  台帳に登録できませんでした: {type(e).__name__}: {e}")


def blocked_by_ledger(slug: str) -> list:
    """台帳に「止めるべき」案件があるか。"""
    try:
        got = _oi.blocking_slugs()
    except Exception as e:                # noqa: BLE001
        return [f"台帳を読めません: {e}"]   # ★読めない時は進めない★
    why = got.get(slug)
    return [f"台帳に止めるべき案件があります: {' / '.join(why)}"] if why else []


def _ensure_list(maker: str) -> None:
    """一覧カードで同定する社なら、健全に読めた一覧を控えておく。

    ★条件は夜の見張りと同じ★＝`state=OK` のときだけ控える。
    読めなければ何もしない（＝同定は「公式が読めない」で止まる）。
    """
    import add_machine_run as _amr
    import new_machine_watch as _nw
    if not maker or maker in _amr.LIST_SNAPSHOT:
        return
    try:
        cats = _sj.read_json(_nw.CATALOGS, expect=dict)["catalogs"]
        conf = cats.get(maker) or {}
        if not conf.get("allow_list_card_identity"):
            return
        r = _nw.scan_maker(maker, conf, _nw._load_seen())
        if r.get("state") == "OK" and r.get("list_html"):
            _amr.LIST_SNAPSHOT[maker] = r["list_html"]
    except Exception as e:                # noqa: BLE001
        _log(f"  一覧を読めませんでした（{maker}）: {type(e).__name__}: {e}")


def _read_rows() -> list:
    return _sj.read_rows(MACHINES)


def _detail_path(slug: str) -> str:
    return os.path.join(DETAILS, f"{slug}.json")


def plan_one(slug: str, gather=None, verify=None) -> dict:
    """育てられるか調べて、新しい機種データ・記事を作る（★書き込まない★）。"""
    out = {"slug": slug, "problems": [], "machine": None, "detail": None,
           "was": None, "now": None}
    rows = _read_rows()
    cur = next((m for m in rows if m.get("slug") == slug), None)
    if cur is None:
        out["problems"].append("その機種は一覧にありません")
        return out
    try:
        out["was"] = _pdz.machine_class(cur)
    except _pdz.DecisionError as e:
        out["problems"].append(f"いまの判定書が壊れています: {e}")
        return out
    if out["was"] != "AUTO_PENDING":
        out["problems"].append(f"育てる対象ではありません（いまの区分: {out['was']}）")
        return out
    mode = (_pdz.load_policy() or {}).get("mode")
    if mode != "normal":
        out["problems"].append(f"検索方針が通常ではありません（{mode}）")
        return out
    out["problems"] += blocked_by_ledger(slug)
    left = _pub.unfinished()
    if left:
        out["problems"].append(
            f"前回の公開が途中で終わっています（{left.get('slug')}）")
    if out["problems"]:
        return out

    ident = cur.get("identity") or {}
    name = ident.get("announced_name") or cur.get("name")
    maker = ident.get("manufacturer_id") or ""
    url = ident.get("official_product_url") or ""
    # ① 本人性を確かめ直す（公式が読めなければ同じ公式の一覧カード）
    import add_machine_run as _amr
    if verify is None:
        # ★一覧カードで同定する社は、先に一覧を読んでおく★（2026-08-05）
        #   夜の見張りとは別の実行なので控えが空で、
        #   「その晩に正常に読めた一覧がありません」で必ず止まっていた。
        #   ここでも**健全に読めた（state=OK）一覧だけ**を控える＝条件は同じ。
        _ensure_list(maker)
    verify = verify or _amr.verify_official
    # ★登録済みの登場年月を渡して照合させる★（空だと検査ごと素通りする）
    old_release = str(cur.get("release_date") or "")
    if old_release and str(ident.get("market_release_date") or "") != old_release:
        out["problems"].append(
            f"登録済みの登場年月が食い違っています（一覧 {old_release!r} / "
            f"identity {ident.get('market_release_date')!r}）")
        return out
    vo = verify(name, url, maker, old_release)
    if vo.get("problems"):
        out["problems"] += [f"本人性を確かめ直せません: {p}" for p in vo["problems"]]
        return out
    if old_release and vo.get("release") and vo["release"] != old_release:
        out["problems"].append(
            f"登場年月が変わっています（{old_release} → {vo['release']}）"
            "／自動では直しません")
        return out
    # ② 材料を集め直す
    gather = gather or _amr.gather
    got = gather(name, maker)
    # ★材料が返っても「書いてはいけない理由」があれば止める★
    #   （2026-08-05・Codex102回目の指摘1。転載の疑いなどは
    #     material が作られても新台側では公開を止めている）
    blk = _amr.blocking_problems(got.get("problems") or [])
    if blk:
        out["problems"] += [f"止めました: {p}" for p in blk]
        return out
    mat = got.get("material")
    if not mat:
        out["problems"].append("材料を集められません: "
                               + " / ".join(got.get("problems") or [])[:200])
        return out
    release = vo.get("release") or (cur.get("release_date") or "")
    machine = _ba.build_machine(
        slug, vo.get("identity_name") or name, maker, url, release, mat,
        identity_binding=ident.get("identity_binding", ""),
        identity_evidence_ref=ident.get("identity_evidence_ref", ""))
    detail = _ba.build_detail(slug, vo.get("identity_name") or name, release, mat)
    # ③ 本人性が変わっていないか
    out["problems"] += identity_same(ident, machine.get("identity") or {})
    # ④ 材料は増えるだけか（IDの増減と、載っている文の両方で見る）
    out["problems"] += claims_grew(cur.get("page_decision"),
                                   machine.get("page_decision"))
    dp = _detail_path(slug)
    old_detail = _sj.read_json(dp, expect=dict) if os.path.isfile(dp) else {}
    lost = text_kept(old_detail, detail)
    out["problems"] += lost
    # ★「前に載っていた内容が再現できない」は人へ回す★（黙って止め続けない）
    if lost or any("消えます" in p for p in out["problems"]):
        ledger_once(
            slug, "確認済みだった内容を再現できません（育てる処理を止めています）",
            " / ".join(lost + [p for p in out["problems"] if "消えます" in p])[:900]
            + "／出典の一時的な不調かもしれません。旧い内容は公開されたままです。"
            "人が出典を見て、直すか消すかを決めてください。")
    if out["problems"]:
        return out
    try:
        out["now"] = _pdz.machine_class(machine)
    except _pdz.DecisionError as e:
        out["problems"].append(f"新しい判定書が壊れています: {e}")
        return out
    out["machine"], out["detail"] = machine, detail
    return out


def _replace_row(rows: list, machine: dict) -> list:
    return [machine if m.get("slug") == machine["slug"] else m for m in rows]


def _row_fingerprint(slug: str) -> str:
    """いまの一覧に入っているその機種の姿（計画時と変わっていないか見る）。"""
    import hashlib
    cur = next((m for m in _read_rows() if m.get("slug") == slug), None)
    return hashlib.sha256(
        json.dumps(cur, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _file_sha(path: str) -> str:
    import hashlib
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def apply_one(got: dict, before_fp: dict | None = None) -> dict:
    """育てた結果を書き込む（★全部そろうか、何も残さないか★）。

    ★書く直前にもう一度確かめる★（2026-08-05・Codex102回目の指摘4）
      計画してから書くまでの間に取得を挟むので、その間に別の処理が
      同じ機種を書き換えていることがある。古い計画で上書きしないよう、
      **一覧の中身・検索方針・台帳・途中の目印**を書く直前に見直す。

    ★同時に2つ走らせない★＝新規公開と同じ鍵（`publish_new_machine` のロック）
    ★途中で落ちても分かるように目印を残す★＝`.publish-in-progress.json`
    """
    slug = got["slug"]
    machine, detail = got["machine"], got["detail"]
    out = {"slug": slug, "problems": [], "wrote": [], "was": got["was"],
           "now": got["now"]}
    indexable = got["now"] == "AUTO_INDEXABLE"
    html = _pub.render(slug, machine, detail)
    # ★書く前の検査（新規公開と同じものを使う）★
    out["problems"] += _pub.check_detail(slug, detail)
    out["problems"] += _pub.check_machine(slug, machine)
    out["problems"] += _pub.check_page(slug, html, expect_noindex=not indexable,
                                       detail=detail)
    out["problems"] += _pub.check_only_allowed_values(slug, machine, detail, html)
    out["problems"] += _pub.run_site_audit()
    if out["problems"]:
        return out

    page = _pub._page_path(slug)
    dp = _detail_path(slug)
    hubs_dst = [os.path.join(BASE, rel) for rel in _pub.HUB_FILES]
    watch = [page, dp, MACHINES, SITEMAP] + hubs_dst

    with _pub._OnlyOne():                  # ★同時に2つ走らせない★
        # ── 書く直前の再確認
        if before_fp:
            for p, want in before_fp.items():
                if not os.path.isfile(p) or _file_sha(p) != want:
                    out["problems"].append(
                        f"計画したときから中身が変わっています: "
                        f"{os.path.relpath(p, BASE)}")
        again = plan_one(slug, gather=lambda *a, **k: {"material": None,
                                                       "problems": []},
                         verify=lambda *a, **k: {"problems": [], "release": ""})
        # ↑ 取得はやり直さない。前提（区分・方針・台帳・目印）だけ見直す
        for p in again["problems"]:
            if "材料を集められません" in p or "本人性" in p:
                continue
            out["problems"].append(f"書く直前の確認で止めました: {p}")
        if out["problems"]:
            return out

        keep = {}
        for p in watch:
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    keep[p] = f.read()

        def _restore() -> list:
            bad = []
            for p, b in keep.items():
                try:
                    _pub.write_atomic(p, b.decode("utf-8"))
                except Exception as e:    # noqa: BLE001
                    bad.append(f"{os.path.relpath(p, BASE)}（{e}）")
            return bad

        # ★戻し方つきの目印★（電源が落ちても残る・新規公開と同じ場所）
        _pub.mark_start(slug, machine,
                        {p: b.decode("utf-8") for p, b in keep.items()})
        try:
            rows = _replace_row(_read_rows(), machine)
            _pub.write_atomic(dp, json.dumps(detail, ensure_ascii=False,
                                             indent=1) + "\n")
            _pub.write_atomic(page, html)
            _pub.write_atomic(MACHINES,
                              json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
            sm = keep[SITEMAP].decode("utf-8")
            sm2 = _pub.add_to_sitemap(sm, slug) if indexable \
                else _pub.remove_from_sitemap(sm, slug)
            if sm2 != sm:
                _pub.write_atomic(SITEMAP, sm2)
            # ★早見表も実際に書き直す★（2026-08-05・Codex102回目の指摘5。
            #   `build_hubs()` は描くだけで書かないのに、返り値の problems を
            #   見て終わりにしていた＝一覧が古いまま残る）
            built = _pub.build_hubs()
            for rel, page_html in built.items():
                full = os.path.join(BASE, rel)
                if not os.path.isfile(full) or \
                        open(full, encoding="utf-8").read() != page_html:
                    _pub.write_atomic(full, page_html)
                    out["wrote"].append(full)
            # ★書いたあとにもう一度そろっているか見る★
            # ★自分が置いた「途中」の目印は、この監査では無視する★
            #   （目印はまさに今この処理が付けたもの。ここで拾うと必ず失敗する）
            after = (_pub.run_site_audit(ignore_in_progress=True)
                     + _pub.check_hubs_untouched())
            if indexable:
                after += _pub.check_sitemap_added(sm, slug)
            if after:
                raise GrowError(" / ".join(after)[:300])
            out["wrote"] = [dp, page, MACHINES] + \
                ([SITEMAP] if sm2 != sm else []) + out["wrote"]
        except BaseException as e:        # noqa: BLE001
            bad = _restore()
            out["problems"].append(f"書き込みを取り消しました: {e}")
            out["wrote"] = []
            if bad:
                out["problems"].append("★元に戻せなかったファイルがあります: "
                                       + " / ".join(bad)
                                       + "／目印は残します★")
                return out                # ★戻せていないので目印を消さない★
            _pub.mark_done()
            return out
        _pub.mark_done()                   # ★全部そろってから消す★
    return out


# ------------------------------------------------------------------ selftest

def selftest() -> int:
    ok, ran = True, [0]

    def t(name, cond):
        nonlocal ok
        ran[0] += 1
        print(("✅ " if cond else "❌ ") + name)
        ok = ok and bool(cond)

    t("★★確認済みの事実が消える更新は拒否する★★",
      claims_grew({"claims": ["a", "b"]}, {"claims": ["a"]})
      and "消えます" in claims_grew({"claims": ["a", "b"]},
                                    {"claims": ["a"]})[0])
    t("　材料が増えていなければ何もしない",
      claims_grew({"claims": ["a"]}, {"claims": ["a"]}))
    t("　増えていれば通る", not claims_grew({"claims": ["a"]},
                                            {"claims": ["a", "b"]}))
    t("★★型式が変わったら育てない★★",
      any("型式" in x for x in identity_same(
          {"regulatory_model_code": "A/1"}, {"regulatory_model_code": "B/2"})))
    t("★★登録済みの識別子が取れなくなったら育てない★★",
      any("取れなくなりました" in x for x in identity_same(
          {"manufacturer_id": "oizumi"}, {})))
    t("　同じなら通る",
      not identity_same({"manufacturer_id": "a", "announced_name": "L機"},
                        {"manufacturer_id": "a", "announced_name": "L機"}))
    # 区分が AUTO_PENDING でなければ育てない
    got = plan_one("hokuto")
    t("★★既存機種（判定書なし）は育てる対象にしない★★",
      any("育てる対象ではありません" in p for p in got["problems"]))
    t("　知らないslugは対象にしない",
      any("一覧にありません" in p for p in plan_one("no_such_slug")["problems"]))
    # 台帳で止まっている機種は触らない
    real_blocking = _oi.blocking_slugs
    try:
        _oi.blocking_slugs = lambda: {"zz": ["#1 止める"]}
        t("★★台帳で止まっている機種は育てない★★",
          any("止めるべき案件" in x for x in blocked_by_ledger("zz"))
          and not blocked_by_ledger("other"))
        _oi.blocking_slugs = lambda: (_ for _ in ()).throw(RuntimeError("読めない"))
        t("★★台帳を読めない時は進めない★★",
          any("台帳を読めません" in x for x in blocked_by_ledger("zz")))
    finally:
        _oi.blocking_slugs = real_blocking
    print(f"\n{ran[0]}/{ran[0]} 合格" if ok else "\n不合格あり")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="新台経路の機種を育てる")
    ap.add_argument("--slug")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    rows = _read_rows()
    tg = targets(rows)
    if not a.slug:
        print(f"育てる対象: {len(tg)}機種 " + " ".join(tg[:10]))
        return 0
    got = plan_one(a.slug)
    if got["problems"]:
        print("できません:")
        for p in got["problems"]:
            print("  -", p)
        return 1
    print(f"{a.slug}: {got['was']} → {got['now']} "
          f"/ 事実 {len((got['machine'].get('page_decision') or {}).get('claims') or [])}件")
    if not a.apply:
        print("（下見です。書き込むには --apply）")
        return 0
    fp = {p: _file_sha(p) for p in
          (_detail_path(a.slug), _pub._page_path(a.slug), MACHINES, SITEMAP)
          if os.path.isfile(p)}
    r = apply_one(got, before_fp=fp)
    for p in r["problems"]:
        print("  -", p)
    if r["wrote"]:
        print("書きました: " + " ".join(os.path.relpath(x, BASE).replace(os.sep, "/")
                                         for x in r["wrote"]))
    return 1 if r["problems"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except _sj.SafeJsonError as e:
        print(f"★入力データが読めません: {e}★")
        raise SystemExit(1)

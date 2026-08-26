# -*- coding: utf-8 -*-
"""apply_indexing_policy.py — 緊急overrideを実際の成果物へ反映する。

★なぜ要るか（2026-08-04・Codex73回目の指摘1）★
  `indexing-policy.json` を切り替えても、既に公開したページは静的HTMLなので
  noindex も sitemap も変わらない。**スイッチを入れたつもりで何も起きない**、
  という一番危ない状態になっていた。

  そこでこのコマンドが、新台経路（page-decision/v1）の機種について
  ①機種行の判定書 ②ページのnoindex ③sitemap の3つを、
  いまのpolicyで計算し直した結果にそろえる。

★安全策★
  - 既定は下見（--apply で初めて書く）
  - 触るのは新台経路の機種だけ（既存120機種には指1本触れない）
  - 全部そろってから置き換える（途中で落ちたら元に戻す）
  - 反映後にサイト監査を回し、NGなら全部元に戻す

使い方:
    python scripts/apply_indexing_policy.py            # 何が変わるか見る
    python scripts/apply_indexing_policy.py --apply    # 反映する
    python scripts/apply_indexing_policy.py --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import page_decision as _pd            # noqa: E402
import publish_new_machine as _pub     # noqa: E402
import safe_json as _sj                # noqa: E402

MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
DETAILS = os.path.join(BASE, "assets", "data", "machine-details")
SITEMAP = os.path.join(BASE, "sitemap.xml")


class PolicyApplyError(RuntimeError):
    pass


def _page_has_noindex(path: str) -> bool:
    with open(path, encoding="utf-8") as f:
        vals = _pub._hc.meta_values(_pub._hc.parse(f.read()), "robots")
    return any("noindex" in v for v in vals)


def plan(policy: dict | None = None) -> dict:
    """いまのpolicyに対して、そろっていない成果物を挙げる（書き込まない）。

    ★判定書だけでなく、実際のHTMLとsitemapの状態まで見る★
      （2026-08-04・Codex74回目の指摘1。判定書だけを見ていたので、
        判定書を書いた直後に落ちると「差分なし」と判断し、
        古いHTML・sitemapを直せないまま収束できなかった）
      3つのどれか1つでもずれていれば「そろえる対象」にする＝
      途中で落ちても、もう一度走らせれば必ず追いつく。
    """
    policy = policy if policy is not None else _pd.load_policy()
    rows = _sj.read_rows(MACHINES)
    with open(SITEMAP, encoding="utf-8") as f:
        sm = f.read()
    out = {"mode": policy["mode"], "changes": [], "unchanged": []}
    for m in rows:
        if not _pd.is_auto(m):
            continue
        slug = m.get("slug")
        # ★★区分の判定をここでも通す★★（2026-08-26・Codex31回目のP0）
        #   ★直す前は判定書**単体**の検査しか呼んでいなかった★ので、
        #   `machine_class()` が持つ2つの守り
        #     ①凍結中の版 ②名乗りと中身の版の食い違い
        #   をどちらも通らずに、noindex と sitemap を書き換えられた。
        _pd.machine_class(m, policy)
        pd_old = m.get("page_decision") or {}
        _pd.validate_decision(pd_old)          # 壊れていればここで止まる
        # ★版に合わせて計算し直す★（2026-08-26・Codex28回目のP0）
        #   ★直す前は v1 の式で固定★＝v2 の機種の判定書を
        #   v1 の形で上書きしていた（緊急スイッチを切り替えた日に起きる）。
        pd_new = _pd.recompute(pd_old, policy["mode"])
        page = os.path.join(BASE, "machines", slug, "index.html")
        why = []
        if pd_new != pd_old:
            why.append("判定書")
        if not os.path.isfile(page):
            why.append("ページがありません")
        elif _page_has_noindex(page) == pd_new["indexable"]:
            why.append("ページのnoindex")
        if (_pub.sitemap_line(slug) in sm) != pd_new["indexable"]:
            why.append("sitemap")
        if not why:
            out["unchanged"].append(slug)
            continue
        out["changes"].append({
            "slug": slug,
            "from": pd_old["indexable"], "to": pd_new["indexable"],
            "why": why, "decision": pd_new,
        })
    return out


def apply(policy: dict | None = None, apply_it: bool = False) -> dict:
    """判定書・ページ・sitemap を、いまのpolicyの結果にそろえる。"""
    policy = policy if policy is not None else _pd.load_policy()
    got = plan(policy)
    got["wrote"] = []
    got["problems"] = []
    if not got["changes"] or not apply_it:
        return got

    with open(MACHINES, "rb") as f:
        machines_before = f.read()
    with open(SITEMAP, encoding="utf-8") as f:
        sitemap_before = f.read()
    pages_before = {}
    for c in got["changes"]:
        p = os.path.join(BASE, "machines", c["slug"], "index.html")
        if not os.path.isfile(p):
            got["problems"].append(f"ページがありません: {c['slug']}")
            return got
        with open(p, encoding="utf-8") as f:
            pages_before[p] = f.read()

    def _rollback() -> list:
        """★1つずつ独立に戻し、戻せたかを1件ずつ確かめる★

        （2026-08-04・Codex74回目の指摘2。全体を1つのtryで囲っていたので、
        最初の machines.json の復元に失敗すると sitemap もページも
        戻さないまま抜けていた）
        戻せなかったものの一覧を返す（空なら完全に戻った）。
        """
        failed = []
        targets = [(MACHINES, machines_before.decode("utf-8")),
                   (SITEMAP, sitemap_before)] + list(pages_before.items())
        for path_, text_ in targets:
            try:
                _pub.write_atomic(path_, text_)
                with open(path_, encoding="utf-8") as f:
                    if f.read() != text_:
                        failed.append(os.path.relpath(path_, BASE))
            except Exception as e:        # noqa: BLE001
                failed.append(f"{os.path.relpath(path_, BASE)}: {e}")
        if failed:
            got["problems"].append(
                "★元に戻せなかったファイルがあります（人が確かめてください）: "
                + " / ".join(str(x)[:80] for x in failed[:5]) + "★")
        return failed

    try:
        rows = _sj.read_rows(MACHINES)
        by_slug = {c["slug"]: c for c in got["changes"]}
        # ① 機種行の判定書を差し替える
        for m in rows:
            c = by_slug.get(m.get("slug"))
            if c:
                m["page_decision"] = c["decision"]
        # ② ページを描き直す（判定書ベースなので noindex が付け外しされる）
        new_pages = {}
        for m in rows:
            c = by_slug.get(m.get("slug"))
            if not c:
                continue
            dp = os.path.join(DETAILS, f"{m['slug']}.json")
            detail = _sj.read_json(dp, expect=dict)
            new_pages[os.path.join(BASE, "machines", m["slug"], "index.html")] = \
                _pub.render(m["slug"], m, detail)
        # ③ sitemap（index対象は載せる・そうでなければ外す）
        sm = sitemap_before
        for c in got["changes"]:
            if c["to"]:
                if _pub.sitemap_line(c["slug"]) not in sm:
                    sm = _pub.add_to_sitemap(sm, c["slug"])
            else:
                sm = _pub.remove_from_sitemap(sm, c["slug"])
        # ★全部そろってから置き換える★
        _pub.write_atomic(MACHINES,
                          json.dumps(rows, ensure_ascii=False, indent=1) + chr(10))
        got["wrote"].append(MACHINES)
        for p_, html_ in new_pages.items():
            _pub.write_atomic(p_, html_)
            got["wrote"].append(p_)
        if sm != sitemap_before:
            _pub.write_atomic(SITEMAP, sm)
            got["wrote"].append(SITEMAP)
    except BaseException as e:            # noqa: BLE001
        failed = _rollback()
        got["problems"].append(
            f"反映できませんでした（{'元に戻しました' if not failed else '戻し切れていません'}）: {e}")
        got["wrote"] = [] if not failed else got["wrote"]
        if isinstance(e, KeyboardInterrupt):
            raise
        return got

    # ★監査そのものが例外で落ちる場合も、変更を残して終わらない★
    #   （2026-08-04・Codex74回目の指摘2）
    try:
        ng = _pub.run_site_audit()
    except BaseException as e:            # noqa: BLE001
        ng = [f"監査を実行できませんでした: {e}"]
        if isinstance(e, KeyboardInterrupt):
            _rollback()
            raise
    if ng:
        failed = _rollback()
        got["problems"] += ng
        if failed:
            got["problems"].append(
                "★監査に落ちたので戻そうとしましたが、戻し切れていません★")
        else:
            got["problems"].append("★監査に落ちたので全部元に戻しました★")
            got["wrote"] = []
    return got


# ---------------------------------------------------------------- selftest

def _v2_wiring_tests(t) -> None:
    """★v2 の機種が、受け側すべてで v1 と同じ扱いになるか★（通し確認）

    ★★罠⑬の対策★★（2026-08-26）
      個別の試験はどれも緑だったのに、繋ぐと矛盾していた＝
        ・`is_auto()` が v1 だけ True → v2 は**旧形式扱い**（noindexが外れる）
        ・この関数が v1 の式で固定 → v2 の判定書を v1 の形で上書き
      ★どちらも「その関数だけ」を見ている限り見つからない★ので、
      同じ claims から作った v1 と v2 を並べて、受け側の答えを見比べる。
    """
    import build_ledger as _bl_v2

    def _raises(fn) -> bool:
        try:
            fn()
        except _pd.DecisionError:
            return True
        except Exception:                                # noqa: BLE001
            return False
        return False

    pol = {"mode": "normal"}
    d1 = _pd.decide_from_claims(
        ["ceiling:GAME:999", "payout_range", "at:MAIN_AT"],
        "normal", "2026-08-26")
    d2 = _pd.decide_from_claims_v2(
        ["ceiling:GAME:999", "payout_range", "bonus_prob"],
        "normal", "BONUS", "PRESENT", "2026-08-26")
    m1 = {"slug": "zzz_v1", "name": "試験v1",
          "publication_policy": _pd.SCHEMA, "page_decision": d1}
    m2 = {"slug": "zzz_v2", "name": "試験v2",
          "publication_policy": _pd.SCHEMA_V2, "page_decision": d2}
    legacy = {"slug": "zzz_legacy", "name": "旧形式"}

    t("★★v2 の機種も『新台経路』と判定する★★"
      "／★v1限定だと旧形式（公開・index）へ倒れる★",
      _pd.is_auto(m1) and _pd.is_auto(m2) and not _pd.is_auto(legacy))
    t("★★知らない版は置けない（名簿は生きている）★★",
      _raises(lambda: _pd.machine_class(
          {**m2, "publication_policy": "page-decision/v9"}, pol)))

    keep = _pd.ENABLED_PUBLICATION_SCHEMAS
    try:
        t("　v2 も v1 も区分が出る（同じ AUTO_INDEXABLE）",
          _pd.machine_class(m2, pol) == "AUTO_INDEXABLE"
          and _pd.machine_class(m1, pol) == "AUTO_INDEXABLE")
        t("★★台帳が v2 も CANDIDATE に倒す★★"
          "／★倒さないと gates の公開経路へ落ちる★",
          _bl_v2.provisional(m2)["lifecycle"] == "CANDIDATE"
          and _bl_v2.provisional(m1)["lifecycle"] == "CANDIDATE")
        t("　台帳は旧形式を今までどおり公開側にする",
          _bl_v2.provisional(legacy)["lifecycle"] != "CANDIDATE")
        t("★★名乗り v1・中身 v2 の機種は止める★★",
          _raises(lambda: _pd.machine_class(
              {**m2, "publication_policy": _pd.SCHEMA}, pol)))
    finally:
        _pd.ENABLED_PUBLICATION_SCHEMAS = keep
    t("　通し確認のあとで、置いてよい版の名簿が戻っている",
      _pd.ENABLED_PUBLICATION_SCHEMAS == keep)

    # ★緊急スイッチが版に合わせて計算し直すか★（ここが v1 固定で壊れていた）
    f2 = _pd.recompute(d2, "force_noindex_new_auto")
    t("★★緊急スイッチで v2 も noindex に倒れる★★",
      f2["indexable"] is False)
    t("　倒した結果が v2 の形のまま（v1 の形で上書きしない）",
      f2.get("schema_version") == _pd.SCHEMA_V2)
    t("★（対照）v1 の式で計算すると別物になる＝この分岐は効いている★",
      _pd.decide_from_claims(d2["claims"], "force_noindex_new_auto",
                             d2["decided_at"]) != f2)


def selftest() -> int:
    ok_all = True
    ran = [0]

    def t(name, cond):
        nonlocal ok_all
        ran[0] += 1
        ok_all = ok_all and bool(cond)
        print(("✅" if cond else "❌") + " " + name)

    NORMAL = {"schema_version": _pd.POLICY_SCHEMA, "mode": "normal",
              "reason": ""}
    claims = ["at:MAIN_AT", "model_code", "payout_range"]
    FORCE = {"schema_version": _pd.POLICY_SCHEMA,
             "mode": "force_noindex_new_auto", "reason": "試験"}
    t("★いまの本番データに新台経路の機種が無ければ、変えるものも無い★",
      plan(NORMAL)["changes"] == [])
    # ★成果物ベースの収束（Codex74回目の指摘1）★
    #   判定書だけ書き換わって落ちた状態＝ページとsitemapが古い、を作って
    #   plan() が「まだそろっていない」と言えることを確かめる。
    import tempfile as _tf, shutil as _sh, json as _js
    _real = (MACHINES, SITEMAP, BASE, DETAILS)
    _d = _tf.mkdtemp(prefix="uchi_pol_")
    try:
        g = globals()
        g["MACHINES"] = os.path.join(_d, "machines.json")
        g["SITEMAP"] = os.path.join(_d, "sitemap.xml")
        g["BASE"] = _d
        os.makedirs(os.path.join(_d, "machines", "zzz_pol"))
        # 判定書は「override反映済み（noindex側）」、ページは古い（noindexが無い）、
        # sitemapにも載ったまま＝落ちた直後の状態
        _pd_f = _pd.decide_from_claims(claims, "force_noindex_new_auto",
                                       "2026-08-04")
        with open(g["MACHINES"], "w", encoding="utf-8") as f:
            _js.dump([{"slug": "zzz_pol", "name": "試験",
                       "publication_policy": _pd.SCHEMA,
                       "page_decision": _pd_f}], f, ensure_ascii=False)
        with open(os.path.join(_d, "machines", "zzz_pol", "index.html"),
                  "w", encoding="utf-8") as f:
            f.write("<html><head><title>x</title></head><body></body></html>")
        with open(g["SITEMAP"], "w", encoding="utf-8") as f:
            f.write("<urlset>" + chr(10)
                    + _pub.sitemap_line("zzz_pol") + chr(10) + "</urlset>" + chr(10))
        got2 = plan(FORCE)
        t("★★判定書だけ反映されて落ちた状態を、もう一度走らせれば直せる★★"
          "（ページのnoindexとsitemapのずれを見つける・Codex74回目）",
          len(got2["changes"]) == 1
          and set(got2["changes"][0]["why"]) == {"ページのnoindex", "sitemap"})
        # 全部そろっていれば「変えるものは無い」
        with open(os.path.join(_d, "machines", "zzz_pol", "index.html"),
                  "w", encoding="utf-8") as f:
            f.write('<html><head><meta name="robots" content="noindex,follow">'
                    "</head><body></body></html>")
        with open(g["SITEMAP"], "w", encoding="utf-8") as f:
            f.write("<urlset>" + chr(10) + "</urlset>" + chr(10))
        t("　そろっていれば変えるものは無い（何度走らせても同じ）",
          plan(FORCE)["changes"] == [])
        # ★★障害を実際に起こして確かめる★★（2026-08-04・Codex75回目の助言）
        #   「実装は塞がっているが、退行を防ぐ試験が無い」と言われた3経路。
        _det = os.path.join(_d, "assets", "data", "machine-details")
        os.makedirs(_det)
        with open(os.path.join(_det, "zzz_pol.json"), "w",
                  encoding="utf-8") as f:
            _js.dump({"slug": "zzz_pol", "sections": []}, f)
        g["DETAILS"] = _det
        # 反映が必要な状態に戻す（ページのnoindexを外す）
        _page = os.path.join(_d, "machines", "zzz_pol", "index.html")
        with open(_page, "w", encoding="utf-8") as f:
            f.write("<html><head><title>x</title></head><body></body></html>")
        _real_render, _real_audit, _real_write = (
            _pub.render, _pub.run_site_audit, _pub.write_atomic)
        try:
            # ① 書込み中の例外 → 全部元に戻り、書いたことにしない
            _pub.render = lambda *a2, **k2: (_ for _ in ()).throw(
                RuntimeError("描画に失敗"))
            r1 = apply(FORCE, apply_it=True)
            with open(_page, encoding="utf-8") as f:
                _page_now = f.read()
            t("★★描画で落ちても、書いたものは全部元に戻る★★",
              r1["wrote"] == [] and "描画に失敗" in " ".join(r1["problems"])
              and "noindex" not in _page_now)
            # ② 監査が例外で落ちる → 変更を残さない
            _pub.render = _real_render
            _pub.run_site_audit = lambda **k2: (_ for _ in ()).throw(
                RuntimeError("監査が異常終了"))
            r2 = apply(FORCE, apply_it=True)
            t("★★監査そのものが落ちても、変更を残して終わらない★★",
              r2["wrote"] == []
              and any("監査を実行できません" in x for x in r2["problems"]))
            # ③ 戻すのに失敗 → 「全部戻しました」と言わない
            _pub.run_site_audit = lambda **k2: ["わざとNG"]
            _pub.write_atomic = lambda p3, t3, **k3: (
                _real_write(p3, t3, **k3) if "machines.json" not in p3
                else (_ for _ in ()).throw(OSError("戻せない")))
            r3 = apply(FORCE, apply_it=True)
            t("★★戻し切れないときに『全部元に戻しました』と言わない★★",
              any("戻し切れていません" in x for x in r3["problems"])
              and not any("全部元に戻しました" in x for x in r3["problems"]))
        finally:
            _pub.render, _pub.run_site_audit, _pub.write_atomic = (
                _real_render, _real_audit, _real_write)
        # ★★v2 の機種を、本物の入口（plan）に通す★★（2026-08-26）
        #   ★recompute を直接たたく試験だけでは足りない★＝
        #   ここが v1 の式で固定されていても、その試験は緑のままだった
        #   （壊し方の道具が「守られていない」と出して分かった）。
        _kv2 = _pd.ENABLED_PUBLICATION_SCHEMAS
        try:
            _pd.ENABLED_PUBLICATION_SCHEMAS = _pd.SCHEMAS
            _d2 = _pd.decide_from_claims_v2(
                ["ceiling:GAME:999", "payout_range", "bonus_prob"],
                "force_noindex_new_auto", "BONUS", "PRESENT", "2026-08-26")
            with open(g["MACHINES"], "w", encoding="utf-8") as f:
                _js.dump([{"slug": "zzz_pol", "name": "試験v2",
                           "publication_policy": _pd.SCHEMA_V2,
                           "page_decision": _d2}], f, ensure_ascii=False)
            # ★★凍結中の v2 は、入口が止める★★（2026-08-26・Codex31回目のP0）
            #   ★machine_class を直接たたく試験だけでは足りない★＝
            #   plan がそれを呼ばなくなっても緑のままだった。
            _pd.ENABLED_PUBLICATION_SCHEMAS = (_pd.SCHEMA,)
            _frozen_stopped = False
            try:
                plan(NORMAL)
            except _pd.DecisionError as _e_fr:
                _frozen_stopped = "置けません" in str(_e_fr)
            t("★★入口（plan）が、置いてよい版でないものを止める★★"
              "／★止めないと noindex と sitemap を書き換えられる★",
              _frozen_stopped)
            # ★★名乗り v1・中身 v2 も入口で止める★★
            with open(g["MACHINES"], "w", encoding="utf-8") as f:
                _js.dump([{"slug": "zzz_pol", "name": "試験混在",
                           "publication_policy": _pd.SCHEMA,
                           "page_decision": _d2}], f, ensure_ascii=False)
            _mix_stopped = False
            try:
                plan(NORMAL)
            except _pd.DecisionError as _e_mx:
                _mix_stopped = "判定書の版" in str(_e_mx)
            t("★★入口（plan）が、名乗りと中身の食い違いを止める★★",
              _mix_stopped)
            # 置いてよい版に戻して、正しい v2 は通ること
            _pd.ENABLED_PUBLICATION_SCHEMAS = _pd.SCHEMAS
            with open(g["MACHINES"], "w", encoding="utf-8") as f:
                _js.dump([{"slug": "zzz_pol", "name": "試験v2",
                           "publication_policy": _pd.SCHEMA_V2,
                           "page_decision": _d2}], f, ensure_ascii=False)
            _pv2 = plan(NORMAL)
            _ch2 = [c for c in _pv2["changes"] if c["slug"] == "zzz_pol"]
            t("★★v2 の機種も、緊急スイッチの入口で拾われる★★",
              len(_ch2) == 1)
            _new2 = _pd.recompute(_d2, "normal")
            t("★★入口が v2 の式で計算し直している★★"
              "／★v1 の式で固定すると、v2 の判定書が v1 の形で上書きされる★",
              _ch2 and _ch2[0]["to"] is _new2["indexable"]
              and _new2.get("schema_version") == _pd.SCHEMA_V2)
        finally:
            _pd.ENABLED_PUBLICATION_SCHEMAS = _kv2
    finally:
        (globals()["MACHINES"], globals()["SITEMAP"], globals()["BASE"],
         globals()["DETAILS"]) = _real
        _sh.rmtree(_d, ignore_errors=True)
    # 合成データで、切り替えが判定書に効くことを見る
    d_n = _pd.decide_from_claims(claims, "normal", "2026-08-04")
    d_f = _pd.decide_from_claims(claims, "force_noindex_new_auto", "2026-08-04")
    t("★★同じclaimsでも、override中は indexable が false になる★★",
      d_n["indexable"] and not d_f["indexable"]
      and "POLICY_FORCE_NOINDEX" in d_f["reason_codes"])
    t("　判定書のpolicy_modeで、成果物が古いかどうか分かる",
      _pd.stale_decisions(
          [{"slug": "a", "publication_policy": _pd.SCHEMA,
            "page_decision": d_n}], FORCE) == ["a"])
    t("　下見では何も書かない",
      apply(FORCE, apply_it=False)["wrote"] == [])
    # ★★v2 の配線の通し確認★★（2026-08-26・Codex28回目のP0）
    #   ★数える行より前に置く★（監査51＝あとから足した試験が数えられない）
    _v2_wiring_tests(t)
    print(f"{ran[0]}/{ran[0]} 合格" if ok_all else "不合格あり")
    return 0 if ok_all else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="緊急overrideを成果物へ反映する")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    got = apply(apply_it=a.apply)
    print(f"policy mode: {got['mode']}")
    if not got["changes"]:
        print("そろえる必要のある機種はありません"
              f"（新台経路 {len(got['unchanged'])} 機種は反映済み）")
        return 0
    for c in got["changes"]:
        print(f"  {c['slug']}: index {c['from']} → {c['to']}")
    if not a.apply:
        print("（下見）--apply で反映します")
        return 0
    for x in got["problems"]:
        print("  ✗ " + str(x)[:200])
    print(f"書いたファイル: {len(got['wrote'])} 件")
    return 1 if got["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())

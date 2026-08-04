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
        pd_old = m.get("page_decision") or {}
        _pd.validate_decision(pd_old)          # 壊れていればここで止まる
        pd_new = _pd.decide_from_claims(pd_old["claims"], policy["mode"],
                                        pd_old["decided_at"])
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
    _real = (MACHINES, SITEMAP, BASE)
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
    finally:
        globals()["MACHINES"], globals()["SITEMAP"], globals()["BASE"] = _real
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

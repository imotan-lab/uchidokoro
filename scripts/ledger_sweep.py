# -*- coding: utf-8 -*-
"""担当した機種の台帳を、その場で機械が確かめ直す。

★★運営者の指示（2026-08-30）★★
> 台帳って以前にあったタスクがどんどん詰んでいったものだよね
> 今ってそのタスクないんだけど2AIでそのへんのおかしいところを
> 見つけて更新する予定だったんだけど無理なの？
→ 「入れよう」

★何が起きていたか★＝毎朝のタスクは担当した機種の**記事だけ**を読み、
その機種について過去に書き留めたメモ（台帳）を**見ていなかった**。
運営者が「台帳を順番決めに使うな」と言ったのを受けて順番から外したとき、
★参照そのものもやめてしまった★。

  結果1＝直したのにメモが開いたまま残る
    （2026-08-30に実測。東京喰種の #284 は朝に直したのに開いたままだった）
  結果2＝記事を読むだけでは気づけない指摘が、メモにしか無いまま眠る
    （実測：58機種に68件が、いまも記事に残ったまま）

★★この道具がやること★★
  ①その機種の開いている案件に、機械の検査を当てる
    → ★合格したものだけ閉じる★（AIの宣言では閉じない）
  ②検査を当てられないものは「2AIに読ませる手がかり」として返す
    → ★順番は決めない★（運営者の決めた 新台→人気→その他 は変えない）

★★閉じてよいのは、機械がその型の検査を持っているときだけ★★
  ★「問題の文が消えた＝直った」とはしない★（2026-08-30に実測）＝
  東京喰種の #155 は「CZまたはAT当選」という文が消えていたが、
  それは直ったのではなく★恩恵が未確定で載せていない★からだった。
  文の有無だけで閉じると、直っていないものを閉じてしまう。
"""
from __future__ import annotations
import argparse
import io
import json
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_S = os.path.join(BASE, "scripts")
for _p in (BASE, _S):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import local_paths as _lp                                # noqa: E402
import recheck as _rc                                    # noqa: E402
import safe_json as _sj                                  # noqa: E402

LEDGER = _lp.doc("open_issues.json")

# ★題や詳細に出てくる言葉から、当てる検査を選ぶ★
#   ★ここに無い型は閉じない★（2AIへ手がかりとして渡すだけ）
#   ★語は「その検査が見ているもの」に限る★＝
#     広く取ると、関係のない案件に検査を当てて誤って閉じる。
WORD_TO_CHECK = (
    (("他サイト名", "サイト名の露出", "競合サイト"), "competitor_names_gone"),
    (("型式名", "検定番号"), "model_code_gone"),
    (("常体", "文体混在", "だ・である"), "plain_style_gone"),
    (("設定示唆まとめが空", "設定示唆が空", "settei が空"), "settei_filled"),
    (("噂の箱が空", "噂・未確定情報が空"), "rumor_not_declared_empty"),
    (("交換率別しきい値が逆転", "交換率が良いほど深い", "しきい値が逆転"),
     "rate_monotonic"),
    (("ポチポチくん",), "pochipochi_reachable"),
    (("同じ判断を2度", "重複行", "同一事実の重複"), "duplicate_prose_gone"),
)


def pick_check(issue: dict):
    """その案件に当てられる検査を選ぶ（無ければ None）。"""
    text = (str(issue.get("title") or "") + " "
            + str(issue.get("detail") or ""))
    for words, check in WORD_TO_CHECK:
        if any(w in text for w in words):
            return check
    return None


def _head() -> str:
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=BASE,
                       capture_output=True, text=True)
    return (r.stdout or "").strip()


def _dirty() -> bool:
    """★未コミットの変更があるうちは閉じない★（既存の決まりと同じ）"""
    r = subprocess.run(["git", "status", "--porcelain"], cwd=BASE,
                       capture_output=True, text=True)
    return bool((r.stdout or "").strip())


def for_slug(slug: str) -> dict:
    """その機種の案件を、閉じてよいもの／手がかり に分ける。★書かない★"""
    data = _sj.read_json(LEDGER, expect=(dict, list))
    rows = data if isinstance(data, list) else (data.get("issues") or [])
    mine = [r for r in rows
            if r.get("slug") == slug and r.get("status") != "closed"]
    out = {"slug": slug, "closeable": [], "hints": [], "checked": len(mine)}
    head = _head()
    for r in mine:
        check = pick_check(r)
        if not check:
            out["hints"].append({"id": r.get("id"),
                                 "title": str(r.get("title") or "")[:120],
                                 "why": "当てられる検査がありません"})
            continue
        meta = _rc.CHECKS.get(check) or {}
        ok, why, got = _rc.closeable(
            {"check": check, "version": meta.get("version"),
             "args": {"slug": slug}, "expected_commit": head})
        if ok:
            out["closeable"].append({"id": r.get("id"), "check": check,
                                     "title": str(r.get("title") or "")[:120],
                                     "why": why})
        else:
            out["hints"].append({"id": r.get("id"), "check": check,
                                 "title": str(r.get("title") or "")[:120],
                                 "why": why})
    return out


def close_them(slug: str, got: dict) -> list:
    """機械が合格と言ったものだけ閉じる。★理由をファイルで渡す★"""
    done = []
    ops = _lp.doc("ops")
    os.makedirs(ops, exist_ok=True)
    for c in got["closeable"]:
        p = os.path.join(ops, f"close_{c['id']}.txt")
        io.open(p, "w", encoding="utf-8", newline="\n").write(
            "この機種を担当したときに、機械が検査をやり直して合格しました。\n"
            f"検査: {c['check']}\n"
            f"確かめた内容: {c['why']}\n"
            "★AIの宣言ではなく、機械が記事を見て確かめた結果です★\n")
        r = subprocess.run(
            [sys.executable, os.path.join(_S, "open_issues.py"), "close",
             "--id", str(c["id"]), "--reason-file", p],
            cwd=BASE, capture_output=True, text=True, encoding="utf-8",
            errors="replace")
        if r.returncode == 0:
            done.append(c["id"])
    return done


def all_gone(slug: str, texts, head: str = "") -> tuple:
    """★指定された逐語が「全部」消えているか★ → (ok, 一件ずつの記録)

    ★★1件でも残っていたら閉じない★★（罠⑮＝免除の条件をゆるくしない）
      1つの案件に問題が2つ書いてあることがある（実例 #284＝
      「狙い目の逆転」と「句点後の半角スペース」）。
      片方だけ確かめて閉じると、もう片方が直っていないまま消える。
    ★逐語を1つも渡されなければ「全部消えた」にしない★（空で通さない）
    """
    if not texts:
        return False, ["確かめる逐語が1件もありません"]
    meta = _rc.CHECKS["text_gone"]
    head = head or _head()
    ok_all, whys = True, []
    for t in texts:
        ok, why, _got = _rc.closeable(
            {"check": "text_gone", "version": meta["version"],
             "args": {"slug": slug, "text": t},
             "expected_commit": head})
        whys.append(f"{'消' if ok else '残'}: {t} ／ {why}")
        if not ok:
            ok_all = False
            break
    return ok_all, whys


def main() -> int:
    ap = argparse.ArgumentParser(
        description="担当した機種の台帳を、機械が確かめ直す")
    ap.add_argument("--slug")
    ap.add_argument("--apply", action="store_true",
                    help="合格したものを実際に閉じる")
    ap.add_argument("--close-if-gone", dest="close_if_gone", metavar="番号",
                    help="2AIが「この文が消えていれば直り」と決めた案件を、"
                         "機械が確かめてから閉じる（--text と一緒に使う）")
    ap.add_argument("--text", action="append", default=[],
                    help="消えているはずの逐語（★1件の案件に問題が複数あるなら、"
                         "その数だけ並べる★＝1つだけ確かめて閉じない）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.close_if_gone:
        # ★★2AIが「どの文が消えれば直りか」を決め、機械が確かめる★★
        #   ★文の有無だけで機械が勝手に判断しない★（2026-08-30の実測）＝
        #   東京喰種 #155 は「CZまたはAT当選」が消えていたが、
        #   それは直ったのではなく恩恵が未確定で載せていないからだった。
        if not (a.slug and a.text):
            print("--slug と --text が要ります")
            return 1
        if _dirty():
            print("★未コミットの変更があるので閉じません★")
            return 1
        ok_all, whys = all_gone(a.slug, a.text)
        for w in whys:
            print("  " + w[:110])
        if not ok_all:
            print("★閉じません★（1件でも残っていたら閉じない）")
            return 1
        ops = _lp.doc("ops")
        os.makedirs(ops, exist_ok=True)
        p = os.path.join(ops, f"close_{a.close_if_gone}.txt")
        lines = [
            "2AIがこの案件を読み、直っていれば消えているはずの文を決めました。",
            "機械がいまの記事と公開HTMLを見て、その文が無いことを確かめました。",
            f"確かめた文: {len(a.text)} 件",
        ] + [f"  - {w}" for w in whys] + [
            "検査: text_gone（★1件でも残っていたら閉じない★）",
            "★AIの宣言ではなく、機械が確かめた結果です★",
        ]
        io.open(p, "w", encoding="utf-8", newline="\n").write(
            "\n".join(lines) + "\n")
        r = subprocess.run(
            [sys.executable, os.path.join(_S, "open_issues.py"), "close",
             "--id", str(a.close_if_gone), "--reason-file", p],
            cwd=BASE, capture_output=True, text=True, encoding="utf-8",
            errors="replace")
        print((r.stdout or "").strip()[-160:] or (r.stderr or "")[-160:])
        return r.returncode
    if not a.slug:
        print("--slug が要ります")
        return 1
    got = for_slug(a.slug)
    print(f"{a.slug}: 開いている案件 {got['checked']} 件")
    for c in got["closeable"]:
        print(f"  ★閉じてよい★ #{c['id']} [{c['check']}] {c['title'][:60]}")
    for h in got["hints"]:
        print(f"  手がかり       #{h['id']} {h['title'][:60]}")
        print(f"                 （{h['why'][:70]}）")
    if a.apply:
        if _dirty():
            print("★未コミットの変更があるので閉じません★"
                  "（いまの記事で確かめた結果だと言えないため）")
            return 1
        done = close_them(a.slug, got)
        print(f"閉じました: {len(done)} 件 {done}")
    elif got["closeable"]:
        print("★下見です★（--apply で閉じます）")
    return 0


def selftest() -> int:
    ng = []
    ran = [0]

    def t(name, cond):
        ran[0] += 1
        print(("✅ " if cond else "❌ ") + name)
        if not cond:
            ng.append(name)

    t("★★題の言葉から検査を選ぶ★★",
      pick_check({"title": "C評価: 他サイト名が本文に出ている"})
      == "competitor_names_gone")
    t("　★型式名★", pick_check({"title": "型式名が残っている"})
      == "model_code_gone")
    t("　★交換率の逆転★",
      pick_check({"title": "交換率別しきい値が逆転（5.6枚700G>5.5枚550G）"})
      == "rate_monotonic")
    t("★★当てられる検査が無ければ None★★"
      "（＝勝手に閉じず、2AIへ手がかりとして渡す）",
      pick_check({"title": "天井の恩恵が未確定"}) is None)
    t("　★詳細のほうに書いてあっても拾う★",
      pick_check({"title": "C評価", "detail": "常体が混ざっています"})
      == "plain_style_gone")

    # ★観測どまりの検査は選ばない★（閉じられないものを選ばない）
    for _w, c in WORD_TO_CHECK:
        m = _rc.CHECKS.get(c) or {}
        if not m.get("closeable"):
            ng.append(f"観測どまりの検査を選んでいます: {c}")
    t("★★選ぶのは「閉じられる検査」だけ★★", not [x for x in ng if "観測" in x])

    t("★★実データで動く★★（東京喰種の案件を分けられる）",
      isinstance(for_slug("tokyo_ghoul").get("hints"), list))

    # --- ★2AIが逐語を決めて閉じる道★（本物の記事で確かめる） -------------
    #   ★偽の記事を作らない★＝本番と同じ入口（recheck）を通す。
    real = ""
    _p = os.path.join(BASE, "assets", "data", "machine-details",
                      "tokyo_ghoul.json")
    if os.path.isfile(_p):
        _d = _sj.read_json(_p, expect=dict)
        for _s in (_d.get("sections") or []):
            for _b in (_s.get("body") or []):
                if isinstance(_b, str) and len(_b) > 30:
                    real = _b[:30]
                    break
            if real:
                break
    gone_txt = "この文はうちどころのどの記事にも存在しません2026"
    t("★★逐語を1件も渡されなければ閉じない★★（空で通さない）",
      all_gone("tokyo_ghoul", [])[0] is False)

    # ★★未コミットの木では closeable が必ず断る★★（既存の正しい守り）
    #   ＝汚れた木で「残っていたら閉じない」を試しても、
    #     ★汚れているから断られただけ★で、狙った守りを一度も通らない（罠④）。
    #   → 汚れているときは**飛ばしたと明示する**（合格に数えない）。
    #     CI と mutation_check は綺麗な写しで回すので、そちらで必ず動く。
    if _dirty():
        _ok, _why = all_gone("tokyo_ghoul", [gone_txt])
        t("★★未コミットの木では、消えている逐語でも閉じない★★",
          _ok is False and "未コミット" in _why[0])
        print("⏭ 木が汚れているので「逐語が消えたか」の4件は飛ばしました"
              "（CI・mutation_check の綺麗な写しで動きます）")
    else:
        t("★★消えている逐語なら閉じてよいと言う★★",
          all_gone("tokyo_ghoul", [gone_txt])[0] is True)
        t("★★まだ記事に残っている逐語なら閉じない★★",
          bool(real) and all_gone("tokyo_ghoul", [real])[0] is False)
        t("★★2件のうち1件でも残っていたら閉じない★★"
          "（＝片方だけ確かめて閉じる罠を塞ぐ）",
          bool(real) and all_gone("tokyo_ghoul", [gone_txt, real])[0] is False)
        t("　★順番を入れ替えても同じ★",
          bool(real) and all_gone("tokyo_ghoul", [real, gone_txt])[0] is False)

    print(f"\n{ran[0] - len(ng)}/{ran[0]} " + ("合格" if not ng else "不合格"))
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


if __name__ == "__main__":
    raise SystemExit(main())

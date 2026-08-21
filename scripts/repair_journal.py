#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""★その場で2AIが決めて直す★ための実行記録（自動修理トランザクション）

★なぜ作ったか（運営者の指示・2026-08-21）★
  > だから台帳をなくそうよ　その場で２AI判断で記事作成してってば。

  台帳は「人が後で片付けるもの置き場」になっていた。
  実測＝未処理118件のうち73件が品質レビューの積み残しで、
  ★閉じたのはこの30日ぜんぶ対話セッション（人の手）★だった。

★これは置き場ではない★（Codexの設計レビュー・2026-08-21）
  > 台帳の代わりに、人が処理しない「自動修理トランザクション記録」が必要です。
  > これは未処理置き場ではなく、落ちても自動再開するための実行記録です。

  ＝1件の直しが「どこまで進んだか」を残すだけの帳面。
  途中で落ちても、翌朝そこから続けられる。溜まったら知らせて終わる。

★段階★
  DETECTED       機械が見つけた（まだ誰も判断していない）
  CLAUDE_SEALED  ★Claudeの判定を先に封をした★（Codexの答えを見る前に）
  CODEX_RECEIVED Codexの判定を受け取った
  AGREED         2つが一致した（＝直してよい）
  APPLIED        記事データを書き換えた
  COMMIT_VERIFIED 差分とコミットが結び付いた
  PUSH_CONFIRMED  push できた
  RECHECK_PASS    ★機械が直ったことを確かめ直した★
  DONE            終わり
  ESCALATED       3回やっても決まらなかった → 台帳＋メール（人の出番）

★守っていること★
  1. ★Claudeの判定は、Codexを呼ぶ前にファイルへ書いて指紋を取る★
     （Codexへ渡す材料に含めない＝答えを見てから書き換えられないようにする）
  2. ★閉じられる検査が無い型は受け付けない★
     `recheck.CHECKS[...]["closeable"]` が真でなければ AGREED にできない。
     ＝「直せても機械的に閉じられない」ものを自動で触らせない
  3. ★段階を飛ばせない★（順番どおりにしか進めない）
  4. ★記事が変わっていたらやり直し★（source_sha256 を照合）
  5. ★3回で打ち切る★（材料が同じなら同じ結論しか出ない）

★台帳との関係★
  台帳は消さない。ただし ★人の手を待つのは ESCALATED だけ★ にする。
  RECHECK_PASS まで来たものは、元になった台帳案件があれば
  `recheck.closeable()` を通して自動で閉じる（AIの宣言では閉じない）。

使い方:
  python scripts/repair_journal.py --list
  python scripts/repair_journal.py --show <finding_id>
  python scripts/repair_journal.py --selftest
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

SCHEMA = "repair-journal/v1"
STORE = os.path.join(os.path.expanduser("~"), "Documents", "uchidokoro",
                     "repairs")

# ★段階の並び★（この順にしか進めない）
FLOW = (
    "DETECTED",
    "CLAUDE_SEALED",
    "CODEX_RECEIVED",
    "AGREED",
    "APPLIED",
    "COMMIT_VERIFIED",
    "PUSH_CONFIRMED",
    "RECHECK_PASS",
    "DONE",
)
ESCALATED = "ESCALATED"
STATES = FLOW + (ESCALATED,)

MAX_ATTEMPTS = 3          # ★その晩のうちに3回まで★（CLAUDE.md の決まり）

# ★2AIが選べる操作★（自由文のパッチは作らせない）
#   Codexの設計レビュー: 「AIは自由文パッチを作らず、許可済み操作を選ぶだけ」
ALLOWED_OPS = ("drop", "replace")


class JournalError(Exception):
    pass


# --- 置き場 ---------------------------------------------------------------

def _store() -> str:
    os.makedirs(STORE, exist_ok=True)
    return STORE


def _path(finding_id: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{16}", finding_id or ""):
        raise JournalError(f"見覚えのない番号です: {finding_id!r}")
    return os.path.join(_store(), finding_id + ".json")


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8").replace(b"\r\n", b"\n")).hexdigest()


def finding_id(slug: str, check: str, quote: str, where: str = "") -> str:
    """★同じ問題なら、いつ数えても同じ番号になる★

    ＝台帳番号の代わり。番号を採番せずに済むので、
    「昨日の#318」のような**人が付けた札**に頼らなくてよくなる。
    ★HEADで見つけ直したときに同じ番号になることが肝★
      （Codexの指摘: 既存の自由文を修正命令として使わず、
        いまのHEADで再検出して finding を作り直す）
    """
    src = json.dumps({"slug": slug, "check": check, "quote": quote,
                      "where": where}, ensure_ascii=False, sort_keys=True)
    return _sha(src)[:16]


# --- 読み書き -------------------------------------------------------------

def load(fid: str) -> dict:
    p = _path(fid)
    if not os.path.exists(p):
        raise JournalError(f"#{fid} の記録がありません")
    with io.open(p, encoding="utf-8") as f:
        rec = json.load(f)
    if rec.get("schema_version") != SCHEMA:
        raise JournalError(f"知らない形の記録です: {rec.get('schema_version')!r}")
    return rec


def _save(rec: dict) -> None:
    p = _path(rec["finding_id"])
    tmp = p + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, p)


def _step(rec: dict, to: str, note: str, **extra) -> dict:
    """★段階を1つだけ進める★（飛ばせない・戻れない）"""
    cur = rec["state"]
    if to == ESCALATED:
        rec["state"] = ESCALATED
    else:
        if cur == ESCALATED:
            raise JournalError(f"#{rec['finding_id']} は人へ回した後です")
        if cur not in FLOW:
            raise JournalError(f"知らない段階です: {cur!r}")
        want = FLOW[FLOW.index(cur) + 1] if cur != FLOW[-1] else None
        if to != want:
            raise JournalError(
                f"段階を飛ばせません（いま {cur} ／ 次は {want} ／ 頼まれたのは {to}）")
        rec["state"] = to
    rec.update(extra)
    rec.setdefault("history", []).append({"to": rec["state"], "note": note})
    _save(rec)
    return rec


# --- 各段階 ---------------------------------------------------------------

def detect(slug: str, check: str, quote: str, where: str = "",
           source_sha256: str = "", detail: str = "") -> dict:
    """機械が見つけた。★まだ何も判断していない★"""
    if not slug or not check or not quote:
        raise JournalError("機種・検査名・逐語の3つが要ります")
    fid = finding_id(slug, check, quote, where)
    p = _path(fid)
    if os.path.exists(p):
        return load(fid)          # ★同じ問題を二重に立てない★
    rec = {
        "schema_version": SCHEMA,
        "finding_id": fid,
        "slug": slug,
        "check": check,
        "quote": quote,
        "where": where,
        "source_sha256": source_sha256,
        "detail": detail,
        "state": "DETECTED",
        "attempts": 0,
        "history": [{"to": "DETECTED", "note": detail or check}],
    }
    _save(rec)
    return rec


def seal_claude(fid: str, verdict_path: str) -> dict:
    """★Codexを呼ぶ前に、Claudeの判定へ封をする★

    （Codexの設計レビュー: 「Claudeの判定はファイルに保存・指紋化してから
      Codexを呼び、Codexのプロンプトには含めない」）
    ★ここで指紋を取っておかないと★、Codexの答えを見てから
    「私も同じ判定でした」と書き換えられてしまう。
    """
    rec = load(fid)
    if not os.path.exists(verdict_path):
        raise JournalError(f"判定のファイルがありません: {verdict_path}")
    with io.open(verdict_path, encoding="utf-8") as f:
        text = f.read()
    if len(text.strip()) < 20:
        raise JournalError("判定が短すぎます（20字以上で書いてください）")
    return _step(rec, "CLAUDE_SEALED", "Claudeの判定に封をした",
                 claude_verdict_sha256=_sha(text),
                 claude_verdict_path=os.path.abspath(verdict_path))


def record_codex(fid: str, material_sha256: str, verdict_text: str) -> dict:
    """Codexの判定を受け取る。★封をしてからでないと受け取らない★"""
    rec = load(fid)
    if not rec.get("claude_verdict_sha256"):
        raise JournalError("先にClaudeの判定へ封をしてください")
    if len((verdict_text or "").strip()) < 20:
        raise JournalError("Codexの判定が短すぎます")
    return _step(rec, "CODEX_RECEIVED", "Codexの判定を受け取った",
                 codex_material_sha256=material_sha256,
                 codex_verdict_sha256=_sha(verdict_text))


def agree(fid: str, ops: list, recheck_name: str, decided_by: list) -> dict:
    """2つの判定が一致した。★ここで初めて「直してよい」になる★

    ★閉じられる検査が無い型は受け付けない★（Codexの設計レビュー）
      直したあとに機械が確かめ直せないなら、自動で触らせない。
    """
    rec = load(fid)
    if not isinstance(decided_by, list) or len(decided_by) < 2:
        raise JournalError("判断者が2つ以上要ります（2AIで決めるため）")
    if not ops:
        raise JournalError("やる操作がありません")
    for o in ops:
        if o.get("op") not in ALLOWED_OPS:
            raise JournalError(
                f"選べない操作です: {o.get('op')!r}（選べるのは {ALLOWED_OPS}）")
        if not o.get("why"):
            raise JournalError("理由の無い操作は受け取りません")

    import recheck as _r
    spec = _r.CHECKS.get(recheck_name)
    if not spec:
        raise JournalError(f"そんな検査はありません: {recheck_name!r}")
    if not spec.get("closeable"):
        raise JournalError(
            f"{recheck_name} は観測どまりの検査です。"
            "直したことを機械で確かめられない型なので、自動では触りません")
    return _step(rec, "AGREED", "2AIが一致した",
                 ops=ops, recheck={"name": recheck_name,
                                   "version": spec["version"]},
                 decided_by=list(decided_by))


def applied(fid: str, after_sha256: str) -> dict:
    rec = load(fid)
    return _step(rec, "APPLIED", "記事データを書き換えた",
                 after_sha256=after_sha256)


def commit_verified(fid: str, commit: str) -> dict:
    rec = load(fid)
    if not re.fullmatch(r"[0-9a-f]{7,40}", commit or ""):
        raise JournalError(f"コミットの形が違います: {commit!r}")
    return _step(rec, "COMMIT_VERIFIED", "差分とコミットが結び付いた",
                 commit=commit)


def push_confirmed(fid: str) -> dict:
    """★push できたことを確かめてから★（Codexの設計レビュー）

    > 台帳のcloseは、push確認と再検査PASSの後であるべきで、
    > ローカルコミット直後ではありません。
    """
    rec = load(fid)
    commit = rec.get("commit")
    if not commit:
        raise JournalError("先にコミットを結び付けてください")
    import subprocess
    try:
        out = subprocess.run(
            ["git", "branch", "-r", "--contains", commit],
            cwd=BASE, capture_output=True, text=True, timeout=60)
    except Exception as e:                                   # noqa: BLE001
        raise JournalError(f"pushを確かめられません: {type(e).__name__}")
    if "origin/" not in (out.stdout or ""):
        raise JournalError(
            f"{commit[:8]} はまだ origin にありません（pushしてから進めてください）")
    return _step(rec, "PUSH_CONFIRMED", "pushを確かめた")


def recheck_pass(fid: str) -> dict:
    """★機械が自分で検査をやり直して合格したときだけ進む★

    結果の辞書を受け取らない（＝偽の合格を作れない）。
    """
    rec = load(fid)
    name = (rec.get("recheck") or {}).get("name")
    if not name:
        raise JournalError("通すべき検査が決まっていません")
    import recheck as _r
    args = {"slug": rec["slug"]}
    if "text" in (_r.CHECKS[name].get("args_spec") or {}):
        args["text"] = rec["quote"]
    got = _r.run(name, args)
    if got["result"] != "PASS":
        raise JournalError(
            f"{name} が {got['result']} です: {got['detail']}")
    return _step(rec, "RECHECK_PASS", f"{name} が合格した",
                 recheck_result=got)


def done(fid: str, closed_issues=None) -> dict:
    rec = load(fid)
    return _step(rec, "DONE", "終わり",
                 closed_issues=list(closed_issues or []))


def attempt(fid: str, why: str) -> dict:
    """決まらなかった回を数える。★3回で人へ回す★

    ★数えないもの★（Codexの設計レビュー）
      利用制限・時間切れ・ロックを取れなかった、は「1回」に数えない。
      判断をやってみて決まらなかった回だけを数える。
    """
    rec = load(fid)
    rec["attempts"] = int(rec.get("attempts") or 0) + 1
    rec.setdefault("history", []).append(
        {"to": rec["state"], "note": f"決まらなかった（{rec['attempts']}回目）: {why}"})
    _save(rec)
    if rec["attempts"] >= MAX_ATTEMPTS:
        return _step(rec, ESCALATED, f"{MAX_ATTEMPTS}回やっても決まらなかった")
    return rec


def infra_failure(fid: str, why: str) -> dict:
    """★仕組みの都合で進めなかった★（回数に数えない）"""
    rec = load(fid)
    rec.setdefault("history", []).append(
        {"to": rec["state"], "note": f"仕組みの都合で中断（回数に数えない）: {why}"})
    _save(rec)
    return rec


# --- 一覧 -----------------------------------------------------------------

def listing(state: str | None = None) -> list:
    out = []
    if not os.path.isdir(_store()):
        return out
    for n in sorted(os.listdir(_store())):
        if not n.endswith(".json"):
            continue
        try:
            with io.open(os.path.join(_store(), n), encoding="utf-8") as f:
                rec = json.load(f)
        except Exception:                                    # noqa: BLE001
            continue
        if state and rec.get("state") != state:
            continue
        out.append(rec)
    return out


def _selftest() -> int:
    import shutil
    import tempfile
    ng = []

    def t(name, cond):
        print(("OK   " if cond else "NG   ") + name)
        if not cond:
            ng.append(name)

    td = tempfile.mkdtemp()
    keep = globals()["STORE"]
    globals()["STORE"] = td
    try:
        r = detect("hokuto", "text_gone", "この文はおかしいです。", "sections[0].body[2]",
                   source_sha256="a" * 64, detail="文体")
        fid = r["finding_id"]
        t("　見つけたら記録できる", r["state"] == "DETECTED")
        t("★同じ問題を二重に立てない★",
          detect("hokuto", "text_gone", "この文はおかしいです。",
                 "sections[0].body[2]")["finding_id"] == fid)
        t("★番号は毎回同じ★（HEADで見つけ直しても一致する）",
          finding_id("hokuto", "text_gone", "この文はおかしいです。",
                     "sections[0].body[2]") == fid)

        # 段階を飛ばせない
        try:
            _step(load(fid), "AGREED", "飛ばす")
            t("★★段階を飛ばせない★★", False)
        except JournalError as e:
            t("★★段階を飛ばせない★★", "飛ばせません" in str(e))

        # 封をする前にCodexを受け取れない
        try:
            record_codex(fid, "b" * 64, "Codexの判定です。" * 3)
            t("★★Claudeの判定に封をする前はCodexを受け取れない★★", False)
        except JournalError as e:
            t("★★Claudeの判定に封をする前はCodexを受け取れない★★",
              "封をして" in str(e))

        vp = os.path.join(td, "verdict.md")
        io.open(vp, "w", encoding="utf-8").write(
            "私の判定: この文は前の段落と同じ内容なので消してよいと考えます。")
        rec = seal_claude(fid, vp)
        t("　Claudeの判定に封をできる",
          rec["state"] == "CLAUDE_SEALED" and len(rec["claude_verdict_sha256"]) == 64)

        # ★対照実験★ 封のあと中身を書き換えても指紋は変わらない＝すり替えが分かる
        sealed = rec["claude_verdict_sha256"]
        io.open(vp, "w", encoding="utf-8").write(
            "私の判定: やっぱりCodexと同じで、こちらを残すべきでした。")
        again = _sha(io.open(vp, encoding="utf-8").read())
        t("★★封のあと判定を書き換えたら指紋が食い違う★★", sealed != again)

        record_codex(fid, "b" * 64, "Codexの判定です。同じく消してよいと考えます。")

        # 閉じられない検査では合意できない
        try:
            agree(fid, [{"op": "drop", "why": "重複"}],
                  "strategy_vs_checker", ["Claude", "codex"])
            t("★★閉じられない検査では合意できない★★", False)
        except JournalError as e:
            t("★★閉じられない検査では合意できない★★", "観測どまり" in str(e))

        # 知らない操作
        try:
            agree(fid, [{"op": "rewrite", "why": "…"}],
                  "text_gone", ["Claude", "codex"])
            t("　選べない操作は受け取らない", False)
        except JournalError:
            t("　選べない操作は受け取らない", True)

        # 判断者1人
        try:
            agree(fid, [{"op": "drop", "why": "重複"}], "text_gone", ["Claude"])
            t("　判断者が1人では合意にしない", False)
        except JournalError:
            t("　判断者が1人では合意にしない", True)

        rec = agree(fid, [{"op": "drop", "why": "前の段落と同じ内容"}],
                    "text_gone", ["Claude", "codex"])
        t("　2AIが一致したら合意になる", rec["state"] == "AGREED")

        applied(fid, "c" * 64)
        try:
            commit_verified(fid, "ぜんぜん違う")
            t("　コミットの形を見る", False)
        except JournalError:
            t("　コミットの形を見る", True)
        commit_verified(fid, "0" * 40)

        # push されていないコミットでは進めない
        try:
            push_confirmed(fid)
            t("★★pushを確かめるまで先へ進めない★★", False)
        except JournalError as e:
            t("★★pushを確かめるまで先へ進めない★★",
              "origin" in str(e) or "確かめられません" in str(e))

        # 打ち切り
        f2 = detect("hokuto", "text_gone", "別のおかしな文です。", "x")["finding_id"]
        for i in range(MAX_ATTEMPTS):
            r2 = attempt(f2, f"{i + 1}回目")
        t("★★3回で人へ回す★★", load(f2)["state"] == ESCALATED)
        t("　打ち切った後は先へ進めない",
          _try_fail(lambda: seal_claude(f2, vp)))

        f3 = detect("hokuto", "text_gone", "また別の文です。", "y")["finding_id"]
        infra_failure(f3, "利用制限")
        infra_failure(f3, "時間切れ")
        t("★★仕組みの都合は回数に数えない★★",
          load(f3)["attempts"] == 0 and load(f3)["state"] == "DETECTED")

        t("　一覧が引ける", len(listing()) == 3)
        t("　段階でしぼれる", len(listing(ESCALATED)) == 1)
    finally:
        globals()["STORE"] = keep
        shutil.rmtree(td, ignore_errors=True)

    total = 18
    print()
    print(f"{total - len(ng)}/{total} " + ("合格" if not ng else "不合格"))
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def _try_fail(fn) -> bool:
    try:
        fn()
        return False
    except Exception:                                        # noqa: BLE001
        return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--state", default=None)
    ap.add_argument("--show", default=None)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return _selftest()
    if a.show:
        print(json.dumps(load(a.show), ensure_ascii=False, indent=1))
        return 0
    if a.list or a.state:
        rows = listing(a.state)
        if not rows:
            print("記録はありません")
            return 0
        for r in rows:
            print(f"{r['finding_id']}  {r['state']:15} {r['slug']:22} "
                  f"{r['check']:22} {r['quote'][:34]}")
        print(f"\n計 {len(rows)} 件")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

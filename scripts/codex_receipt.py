# -*- coding: utf-8 -*-
"""Codexレビューの「領収書」を発行する。

★なぜ要るか（2026-08-09・依頼126）★
  「ここまでCodexへ報告した」という印は、これまで
  `codex_reported.py` を**実行しさえすれば付いた**。
  そのため2026-08-08、台帳の文章に書いたバッククォートをシェルが実行し、
  Codexを一度も呼んでいないのに印が付いた（未報告9件が緑になった）。

  Codexの指摘（依頼126・P0-2）:
    「応答ファイルに "tokens used" が入っていること」は証明にならない。
    古い応答・別依頼の応答・手で作ったファイルでも通る。

★そこで、Codexを実際に呼んだときにだけ発行される領収書を作る★
  領収書には次を入れる。**印はこの領収書からしか付けられない**。
    ・reviewed_commit … レビュー対象のコミット（呼び出し開始時のHEAD）
    ・scripts_tree    … その時点の scripts/ の中身の指紋
    ・prompt_sha256   … 依頼文の指紋
    ・response_sha256 … 回答の指紋
    ・exit_code       … Codexの終了コード（0以外は印にできない）
    ・run_id / issued_at
    ・consumed        … 一度使った領収書は二度使えない

使い方:
  # codex_review.sh が成功したときに自動で発行する
  python scripts/codex_receipt.py issue --prompt <依頼文> --response <回答> \
      --exit-code 0
  python scripts/codex_receipt.py list
  python scripts/codex_receipt.py --selftest
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import os as _os_lp                 # noqa: E402
import sys as _sys_lp               # noqa: E402
_sys_lp.path.insert(0, _os_lp.path.dirname(_os_lp.path.abspath(__file__)))
import local_paths as _lp           # noqa: E402
RECEIPT_DIR = _lp.doc("codex_receipts")
SCHEMA = "codex-receipt/v1"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                              # noqa: BLE001
    pass


class ReceiptError(Exception):
    """領収書に関する異常（★迷ったら印を付けない★）。"""


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git(*args: str) -> str:
    r = subprocess.run(["git", *args], cwd=BASE, capture_output=True)
    if r.returncode != 0:
        return ""
    return r.stdout.decode("utf-8", "replace").strip()


def head_commit() -> str:
    return _git("rev-parse", "HEAD")


def scripts_tree(commit: str = "HEAD") -> str:
    """scripts/ の中身の指紋（同じ中身なら同じ値）。"""
    return _git("rev-parse", f"{commit}:scripts")


_SAFE_RUN_ID = __import__("re").compile(r"^[A-Za-z0-9._-]{1,64}$")


def issue(prompt: str, response: str, exit_code: int,
          run_id: str = "", reviewed_commit: str = "") -> str:
    """★Codexを実際に呼んだ直後にだけ発行する★"""
    # ★run_id をファイル名に使うので形を厳しく見る★（2026-08-09・依頼127 P0-4）
    #   区切り文字を入れられると、領収書置き場の外へ書けてしまう。
    if run_id and not _SAFE_RUN_ID.match(run_id):
        raise ReceiptError(f"run_id に使えない文字が入っています: {run_id!r}")
    for p in (prompt, response):
        if not os.path.isfile(p):
            raise ReceiptError(f"ファイルがありません: {p}")
    commit = reviewed_commit or head_commit()
    if not commit:
        raise ReceiptError("HEADを取れません（gitリポジトリですか）")
    now = datetime.datetime.now()
    # ★同じ秒に2回発行しても別の領収書になるようにする★
    #   （自己テストで上書きが起き、前の領収書が消えた）
    rid = run_id or now.strftime("%Y%m%d-%H%M%S-%f")
    rec = {
        "schema_version": SCHEMA,
        "run_id": rid,
        "issued_at": now.isoformat(timespec="seconds"),
        "reviewed_commit": commit,
        "scripts_tree": scripts_tree(commit),
        "prompt_path": os.path.abspath(prompt),
        "prompt_sha256": sha256_of(prompt),
        "response_path": os.path.abspath(response),
        "response_sha256": sha256_of(response),
        "exit_code": int(exit_code),
        # ★見せたものが「そのコミットの中身」とは限らない★（依頼127・A-3 P1）
        #   レビュー中に未コミットの変更を見せていることが多い。
        #   コミット番号だけでは何を見せたか表せないので、手元の状態も残す。
        "worktree_dirty": bool(_git("status", "--porcelain")),
        "worktree_diff_sha256": hashlib.sha256(
            _git("diff", "HEAD").encode("utf-8")).hexdigest(),
        "consumed": False,
        "consumed_at": None,
    }
    os.makedirs(RECEIPT_DIR, exist_ok=True)
    path = os.path.join(RECEIPT_DIR, f"{rid}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(rec, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def load(path: str) -> dict:
    if not os.path.isfile(path):
        raise ReceiptError(f"領収書がありません: {path}")
    with open(path, encoding="utf-8") as fh:
        rec = json.load(fh)
    if rec.get("schema_version") != SCHEMA:
        raise ReceiptError(f"領収書の形が違います: {rec.get('schema_version')}")
    return rec


def validate(rec: dict, path: str) -> dict:
    """★印を付けてよい領収書か★（1つでも欠ければ付けない）"""
    if rec.get("exit_code") != 0:
        raise ReceiptError(
            f"Codexが正常に終わっていません（終了コード {rec.get('exit_code')}）")
    if rec.get("consumed"):
        raise ReceiptError(
            f"この領収書は既に使われています（{rec.get('consumed_at')}）。"
            "同じレビューで二度は印を付けられません")
    # ★依頼文と回答の両方を見る★（2026-08-09・依頼127 A-3 P1）
    #   以前は回答だけ見ていた。依頼文が差し替わっていれば、
    #   「何をレビューしてもらったか」が変わっている。
    for label, key_path, key_sha in (
            ("回答", "response_path", "response_sha256"),
            ("依頼文", "prompt_path", "prompt_sha256")):
        p = rec.get(key_path) or ""
        if not os.path.isfile(p):
            raise ReceiptError(f"{label}ファイルが見当たりません: {p}")
        if sha256_of(p) != rec.get(key_sha):
            raise ReceiptError(
                f"{label}ファイルが領収書の指紋と一致しません（差し替えられています）")
    commit = rec.get("reviewed_commit") or ""
    if not commit or not _git("cat-file", "-t", commit) == "commit":
        raise ReceiptError(f"レビュー対象のコミットが見つかりません: {commit}")
    return rec


def consume(path: str) -> dict:
    rec = validate(load(path), path)
    rec["consumed"] = True
    rec["consumed_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(rec, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, path)
    return rec


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    import tempfile

    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    global RECEIPT_DIR
    keep = RECEIPT_DIR
    tmp = tempfile.mkdtemp()
    RECEIPT_DIR = os.path.join(tmp, "receipts")
    try:
        pf = os.path.join(tmp, "req.md")
        rf = os.path.join(tmp, "res.md")
        open(pf, "w", encoding="utf-8").write("依頼です")
        open(rf, "w", encoding="utf-8").write("回答です tokens used")

        p = issue(pf, rf, 0)
        rec = load(p)
        t("　Codexを呼んだ直後に領収書が出る", os.path.isfile(p))
        t("　レビュー対象のコミットが入っている", len(rec["reviewed_commit"]) == 40)
        t("　依頼文と回答の指紋が入っている",
          len(rec["prompt_sha256"]) == 64 and len(rec["response_sha256"]) == 64)

        ok = False
        try:
            validate(load(issue(pf, rf, 20)), p)
        except ReceiptError:
            ok = True
        t("★★Codexが正常に終わっていない領収書では印を付けない★★", ok)

        consume(p)
        ok = False
        try:
            consume(p)
        except ReceiptError:
            ok = True
        t("★★一度使った領収書は二度使えない★★", ok)

        p2 = issue(pf, rf, 0)
        open(rf, "a", encoding="utf-8").write("あとから書き足した")
        ok = False
        try:
            validate(load(p2), p2)
        except ReceiptError:
            ok = True
        t("★★回答ファイルが差し替わっていたら印を付けない★★", ok)

        p3 = issue(pf, rf, 0)
        rec3 = load(p3)
        rec3["reviewed_commit"] = "0" * 40
        open(p3, "w", encoding="utf-8").write(
            json.dumps(rec3, ensure_ascii=False))
        ok = False
        try:
            validate(load(p3), p3)
        except ReceiptError:
            ok = True
        t("★★存在しないコミットの領収書は受け付けない★★", ok)

        ok = False
        try:
            load(os.path.join(tmp, "ない.json"))
        except ReceiptError:
            ok = True
        t("　領収書が無ければ止まる", ok)

        # ★2026-08-09・依頼127で見つかった穴を固定する★
        pf2 = os.path.join(tmp, "req2.md")
        rf2 = os.path.join(tmp, "res2.md")
        open(pf2, "w", encoding="utf-8").write("依頼です2")
        open(rf2, "w", encoding="utf-8").write("回答です2 tokens used")
        p4 = issue(pf2, rf2, 0)
        ok = True
        try:
            validate(load(p4), p4)     # ★発行した直後にそのまま使えること★
        except ReceiptError as e:
            ok = False
            print("   ", e)
        t("★★発行した直後の領収書がそのまま使える★★"
          "（回答ファイルへ追記して自分の指紋を壊さない）", ok)

        open(pf2, "a", encoding="utf-8").write("依頼文をあとから書き換えた")
        ok = False
        try:
            validate(load(p4), p4)
        except ReceiptError:
            ok = True
        t("★★依頼文が差し替わっていたら印を付けない★★", ok)

        for bad_id in ("../../nukedasu", "a/b", "a" + chr(92) + "b"):
            ok = False
            try:
                issue(pf2, rf2, 0, run_id=bad_id)
            except ReceiptError:
                ok = True
            t("★★領収書の名前に区切り文字を入れられない★★（%s）" % bad_id, ok)

        t("　手元に未コミットの変更があるかを残している",
          "worktree_dirty" in load(p4))
    finally:
        RECEIPT_DIR = keep

    bad = sum(1 for _, o in results if not o)
    print()
    print("%d/%d 合格" % (len(results) - bad, len(results)))
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Codexレビューの領収書")
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    i = sub.add_parser("issue", help="Codexを呼んだ直後に発行する")
    i.add_argument("--prompt", required=True)
    i.add_argument("--response", required=True)
    i.add_argument("--exit-code", dest="exit_code", type=int, required=True)
    i.add_argument("--run-id", dest="run_id", default="")
    i.add_argument("--reviewed-commit", dest="reviewed_commit", default="")
    sub.add_parser("list", help="領収書を並べる")
    a = ap.parse_args()

    if a.selftest:
        return selftest()
    try:
        if a.cmd == "issue":
            print(issue(a.prompt, a.response, a.exit_code, a.run_id,
                        a.reviewed_commit))
            return 0
        if a.cmd == "list":
            if not os.path.isdir(RECEIPT_DIR):
                print("領収書はまだありません")
                return 0
            for n in sorted(os.listdir(RECEIPT_DIR)):
                if not n.endswith(".json"):
                    continue
                try:
                    r = load(os.path.join(RECEIPT_DIR, n))
                except ReceiptError as e:
                    print(f"  {n}: ★読めません（{e}）★")
                    continue
                print("  %-22s %s %s %s"
                      % (n, r["reviewed_commit"][:12],
                         "使用済み" if r["consumed"] else "未使用",
                         os.path.basename(r["response_path"])))
            return 0
    except ReceiptError as e:
        print("★" + str(e) + "★")
        return 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""claim_pipeline.py — 新台追加タスクと記事更新タスクが**共通で呼ぶ入口**。

★なぜ共通にするか（2026-07-30・運営者決定）★
  「出典を取る → 機種を照合する → 値を取り出す → 独立2票 → 公開判定」は
  新台でも既存記事の修正でも同じ手順。ここを2つ作ると、
  **誤情報が通る穴が2か所**になる（Phase 0 で見つかった10経路の原因がこれ）。

★なぜタスクは分けるか★
  失敗したときに起きることが違う。
    新台 : ページが作られない（何も起きない）
    更新 : 誤ったページが残り続ける
  急ぎ度も人の関与も違うので、動かすタスクは別にする。
  ここが詰まった時に両方止まらないよう、この入口は**判定するだけ**で
  ファイルを書かない（書き込みは各タスクが自分で行う）。

★この入口が返すもの★
  機種1つぶんの「いまどの段階か」と「次に何をすべきか」。
  段階は Codex の状態機械に沿う:

    NO_MACHINE        機種データが無い
    IDENTITY_PENDING  メーカー・型式コードが未登録（ここが今の全機種）
    SELF_CONFLICT     記事が自分自身と矛盾している（出典を見るまでもない）
    NEEDS_TYPE        型に落ちない記述がある（在庫に載らない＝網羅を証明できない）
    NEEDS_CHECKER     型はあるが意味の検証器が無い
    NEEDS_EVIDENCE    出典・独立2票が足りない
    READY             公開してよい
    HOLD              検査そのものが失敗した（原因不明のまま先へ進めない）

  ★HOLD は「通す」ではない★ 判定できなかったという意味なので、
  呼び出し側は公開してはいけない。

使い方:
    python scripts/claim_pipeline.py --slug tokyo_ghoul
    python scripts/claim_pipeline.py --all          # 全機種の段階を数える
    python scripts/claim_pipeline.py --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import claim_inventory as ci          # noqa: E402
import claim_reconcile as cr          # noqa: E402
import open_issues as oi              # noqa: E402
import safe_json as _sj               # noqa: E402

# 段階（上から順に確かめる。★1つでも引っかかったらそこで止める★）
STAGES = ("NO_MACHINE", "IDENTITY_PENDING", "BLOCKED_BY_LEDGER", "SELF_CONFLICT",
          "NEEDS_TYPE", "NEEDS_CHECKER", "NEEDS_EVIDENCE", "READY", "HOLD")


def _machine(slug: str):
    rows = _sj.read_rows(os.path.join(BASE, "assets", "data", "machines.json"))
    return next((m for m in rows if m.get("slug") == slug), None)


def _detail(slug: str):
    fp = os.path.join(BASE, "assets", "data", "machine-details", f"{slug}.json")
    if not os.path.isfile(fp):
        return {}
    return _sj.read_json(fp, expect=dict)


def repairable(slug: str, machine: dict | None = None) -> tuple[bool, str]:
    """★その機種は「直すために触ってよい」か★（2026-08-21・台帳#211）

    ★なぜ要るのか★
      台帳に重い案件がある機種は BLOCKED_BY_LEDGER になり、更新タスクが触らない。
      しかし **止まっているのは直す作業だけで、読者は守られていない**
      （`page_decision` は台帳を見ないので、間違った記事はそのまま公開され続ける）。
      しかも無人タスクは台帳を閉じないので、**その機種は永久に解けない**。
      2026-08-21時点で54機種がこの状態だった。

    ★線の引き方★
      - すでに公開されている記事（LEGACY_COMPLETE）→ **直せる**
        （間違ったまま置くほうが有害。直す以外に案件が解ける道がない）
      - まだ公開していない新台（AUTO_PENDING 等）→ **止めたまま**
        （ここでは台帳の関門が本当の仕事をしている＝早すぎる公開を止めている）

    ★これは「公開してよい」という意味ではない★＝記事を直してよいだけ。
      公開の判定は page_decision が別に行う。
    """
    try:
        import page_decision as _pd
    except Exception as e:                                   # noqa: BLE001
        return False, f"区分を判定できません: {type(e).__name__}"
    try:
        m = machine if machine is not None else _machine(slug)
    except Exception as e:                                   # noqa: BLE001
        return False, f"機種データが読めません: {type(e).__name__}"
    if not m:
        return False, "machines.json にありません"
    try:
        klass = _pd.machine_class(m)
    except Exception as e:                                   # noqa: BLE001
        return False, f"区分を決められません: {type(e).__name__}"
    if klass != "LEGACY_COMPLETE":
        return False, f"公開済みの記事ではありません（{klass}）"
    return True, "公開済みの記事なので直してよい"


def assess(slug: str, repairing: bool = False) -> dict:
    """機種1つの段階と、次にやることを返す。★何も書き換えない★

    repairing=True のときは**台帳による停止だけを飛ばす**（2026-08-21・台帳#211）。
    ★飛ばしてよいのは `repairable()` が真の機種だけ★＝呼び出し側が確かめる。
    飛ばしても台帳の中身は `ledger_blocking` に必ず入れて返すので、
    「知らずに素通りした」にはならない。
    """
    out = {"slug": slug, "stage": "HOLD", "reasons": [], "next_action": ""}
    try:
        machine = _machine(slug)
    except Exception as e:
        out["reasons"] = [f"機種データが読めません: {type(e).__name__}: {e}"]
        out["next_action"] = "machines.json を直す"
        return out
    if not machine:
        out["stage"] = "NO_MACHINE"
        out["reasons"] = [f"machines.json に {slug} がありません"]
        out["next_action"] = "追加タスクが機種を登録する"
        return out

    # --- ① 台帳に「公開を止めるべき」案件が残っていないか
    #     ★仕分け前の案件は CRITICAL 扱いになる（severity_of の既定）★
    try:
        blocking = oi.blocking_slugs().get(slug) or []
    except Exception as e:
        out["reasons"] = [f"要確認台帳が読めません: {type(e).__name__}: {e}"]
        out["next_action"] = "台帳を直す（読めないうちは公開しない）"
        return out
    # ★台帳の中身は、飛ばす場合も必ず持ち帰る★（知らずに素通りしたことにしない）
    out["ledger_blocking"] = list(blocking)
    if blocking and not repairing:
        out["stage"] = "BLOCKED_BY_LEDGER"
        out["reasons"] = blocking
        out["next_action"] = "台帳の案件を解決してから閉じる"
        return out
    if blocking and repairing:
        # ★直すために飛ばしてよいのは、公開済みの記事だけ★（台帳#211）
        ok, why = repairable(slug, machine)
        if not ok:
            out["stage"] = "BLOCKED_BY_LEDGER"
            out["reasons"] = blocking + [f"直す経路も使えません: {why}"]
            out["next_action"] = "台帳の案件を解決してから閉じる"
            return out

    # --- ② 機種を特定できるか（メーカー・型式コード）
    missing = ci.identity_missing(machine)
    if missing:
        out["stage"] = "IDENTITY_PENDING"
        out["reasons"] = [f"型式の同定情報が足りません: {missing}"]
        out["next_action"] = ("メーカー公式の商品ページと公安委員会の型式公示から "
                              "manufacturer_id / regulatory_model_code を取る")
        return out

    # --- ③ 記事が自分自身と矛盾していないか（出典を見るまでもない）
    try:
        detail = _detail(slug)
        inv = ci.build_inventory(slug, machine, detail)
    except Exception as e:
        out["reasons"] = [f"在庫を作れません: {type(e).__name__}: {e}"]
        out["next_action"] = "記事データを直す"
        return out
    conflicts = inv.get("surface_conflicts") or []
    if conflicts:
        out["stage"] = "SELF_CONFLICT"
        out["reasons"] = [
            f"{c['field_key']}: " + " / ".join(
                f"{sf['source_pointer']}={sf['current_text']}" for sf in c["surfaces"])
            for c in conflicts]
        out["next_action"] = "記事内の食い違いを先に解消する（どちらが正しいか裏取り）"
        return out

    cov = inv["coverage"]
    if cov["unclassified_atoms"] or cov["unsupported_facts"]:
        out["stage"] = "NEEDS_TYPE"
        out["reasons"] = [
            f"型に落ちない記述 {cov['unclassified_atoms']} 件 / "
            f"型そのものが無い事実 {cov['unsupported_facts']} 件"]
        out["next_action"] = "型を増やすか、記事側の書き方を型に寄せるか、公開対象から外す"
        return out

    # --- ④ 出典・検証器・独立2票（公開判定そのものに聞く）
    try:
        ok, why = cr.publish_gate(slug)
    except Exception as e:
        out["reasons"] = [f"公開判定が例外で失敗: {type(e).__name__}: {e}"]
        out["next_action"] = "原因を調べる（判定できないうちは公開しない）"
        return out
    if ok:
        out["stage"] = "READY"
        out["next_action"] = "公開してよい"
        return out

    joined = " ".join(why or [])
    out["reasons"] = list(why or [])
    if "NO_SEMANTIC_CHECKER" in joined:
        out["stage"] = "NEEDS_CHECKER"
        out["next_action"] = "その型の意味の検証器を実装する"
    else:
        out["stage"] = "NEEDS_EVIDENCE"
        out["next_action"] = "出典を取り直して独立2票をそろえる"
    return out


def assess_all() -> list:
    rows = _sj.read_rows(os.path.join(BASE, "assets", "data", "machines.json"))
    return [assess(m["slug"]) for m in rows if m.get("slug")]


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    t("★段階の名前が想定どおり（呼び出し側が分岐に使う）★",
      set(STAGES) == {"NO_MACHINE", "IDENTITY_PENDING", "BLOCKED_BY_LEDGER",
                      "SELF_CONFLICT", "NEEDS_TYPE", "NEEDS_CHECKER",
                      "NEEDS_EVIDENCE", "READY", "HOLD"})
    t("★無い機種は NO_MACHINE（黙って READY にしない）★",
      assess("zzz_no_such_machine")["stage"] == "NO_MACHINE")

    # 実データ：いまは全機種が型式未登録か台帳で止まっているはず
    real = assess_all()
    stages = {r["stage"] for r in real}
    t("★実データで READY になる機種はまだ無い（公開できる機種0と一致）★",
      "READY" not in stages)
    t("　実データの段階が全部 STAGES に含まれる（未知の段階を作らない）",
      stages <= set(STAGES))
    t("★止まった理由が必ず付く（理由なしで止めない）★",
      all(r["reasons"] for r in real if r["stage"] != "READY"))
    t("★次にやることが必ず付く★", all(r["next_action"] for r in real))

    ng = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--slug", help="1機種の段階を見る")
    ap.add_argument("--all", action="store_true", help="全機種の段階を数える")
    ap.add_argument("--stage", help="この段階の機種だけ出す（--all と併用）")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if args.slug:
        print(json.dumps(assess(args.slug), ensure_ascii=False, indent=1))
        return 0
    if args.all:
        rows = assess_all()
        if args.stage:
            rows = [r for r in rows if r["stage"] == args.stage]
            for r in rows:
                print(f"{r['slug']:26} {r['reasons'][0][:70] if r['reasons'] else ''}")
            print(f"\n{args.stage}: {len(rows)} 機種")
            return 0
        import collections
        c = collections.Counter(r["stage"] for r in rows)
        print(f"{'段階':<20} 機種数")
        print("-" * 32)
        for st in STAGES:
            if c.get(st):
                print(f"{st:<20} {c[st]:>4}")
        print("-" * 32)
        print(f"{'合計':<20} {len(rows):>4}")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except _sj.SafeJsonError as e:
        print(f"★入力データが読めません: {e}★")
        raise SystemExit(1)
    except Exception as e:
        print(f"★想定外の失敗 {type(e).__name__}: {e}★")
        raise SystemExit(1)

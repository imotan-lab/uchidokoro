"""spec_confirm.py — 集めた材料を Codex に見せて確認してから先へ進む。

★運営者の方針（2026-07-31）★
  「揃ったら Codex にソースも投げて確認して進める感じのタスクにしたい」

★Codexは関所ではない★
  Codexが「合格」と言っても、それは公開の資格にはならない。
  公開の可否は決定論の判定（`claim_pipeline` / `claim_reconcile`）が決める。
  ここでCodexに見せるのは、**機械では気づけない種類の誤り**を拾うため:

    - 出典どうしが「同じ数字」でも、意味が違う（通常ATと上位ATの純増など）
    - 条件が落ちている（どのモードの値か書かれていない）
    - そもそも別機種の値が混ざっている
    - 単位や桁が明らかにおかしい

★渡すもの★
  採用しようとしている値と、**その値をどのページのどこから採ったか**（出典）。
  Codexが自分で出典を開いて確かめられるようにする。

★必ず終わる★
  往復は `task_guard.py codex` が数え、上限を超えたら拒否する。
  Codexが答えない・判断がつかない場合は「保留」で先へ進まない。

使い方:
    python scripts/spec_confirm.py --name "Lすーぱぁびん娘" --slug binmusume \\
        --url https://www.p-world.co.jp/machine/database/10496 \\
        --url https://p-town.dmm.com/machines/5038 \\
        --url https://chonborista.com/slot/belko-slot/260918/
    python scripts/spec_confirm.py --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import spec_lookup as _sl             # noqa: E402

# ★依頼文は作業フォルダの中に置く★（外だとCodexが読めない・実地で確認済み）
REQ_DIR = os.path.join(BASE, "_design", "spec_review")


def build_request(name: str, slug: str, pages: list, result: dict) -> str:
    """Codexへの依頼文を作る。★合格基準を書かない★（相手が引っ張られて甘くなる）"""
    lines = [
        f"# 新台の材料の確認：{name}（slug: {slug}）", "",
        f"作業フォルダ: `{BASE}`",
        "**この1機種の材料についてだけ答えてください。他の話題は挙げないでください。**", "",
        "## どうやって集めたか", "",
        "1. メーカー公式の機種一覧から新しいURLを見つけ、公式の正式名称を取った",
        "2. その名称で名鑑を引き、**独立2出典で一致した値だけ**を採用候補にした",
        "3. 同じ運営元・同じ転載系列は1票として数えている", "",
        "## 見に行ったページ", "",
    ]
    for p in pages:
        got = ", ".join(f"{_sl.FIELDS[k]['jp']}" for k in p["fields"]) or "（何も採れず）"
        lines.append(f"- {p['url']}")
        lines.append(f"  - 状態: {p['reason']} / 採れた項目: {got}")
    lines += ["", "## 採用しようとしている値", ""]
    if result["adopted"]:
        for k, v in result["adopted"].items():
            lines.append(f"### {_sl.FIELDS[k]['jp']}（`{k}`）")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(v["value"], ensure_ascii=False, indent=1))
            lines.append("```")
            lines.append(f"一致した出典系列: {v['sources']}")
            lines.append("")
    else:
        lines += ["（採用できた値はありません）", ""]

    if result["need_third"]:
        lines += ["## 出典どうしが食い違っている項目", ""]
        for k, v in result["need_third"].items():
            lines.append(f"- {_sl.FIELDS[k]['jp']}: {json.dumps(v, ensure_ascii=False)[:400]}")
        lines.append("")
    if result["thin"]:
        lines += ["## 1つの出典しか取れていない項目（採用していません）", ""]
        for k, v in result["thin"].items():
            lines.append(f"- {_sl.FIELDS[k]['jp']}: {v['sources']}")
        lines.append("")

    lines += [
        "## 見てほしいこと", "",
        "1. **上の値をこのまま記事に載せて誤情報になりませんか。**",
        "   出典を実際に開いて確かめてください。",
        "2. **数字は同じでも意味が違うものが混ざっていませんか。**",
        "   （通常ATと上位ATの純増、CZ間とAT間の天井、実G数と液晶G数など）",
        "3. **条件が落ちていませんか。**「どのモードの値か」が要る項目はありませんか。",
        "4. 単位・桁・設定の対応に無理はありませんか。",
        "5. **採るべきなのに採れていない項目**はありますか。あればどのページのどこにあるか。",
        "", "## 制約", "",
        "- 誤情報の公開は絶対に避ける。迷ったら出さない。",
        "- **範囲外（既存記事・Phase 1の穴・CI・他機種）は挙げないでください。**",
        "- 私の集め方が間違っているなら、遠慮なくそう言ってください。",
    ]
    return chr(10).join(lines)


def write_request(name: str, slug: str, pages: list, result: dict) -> str:
    os.makedirs(REQ_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fp = os.path.join(REQ_DIR, f"{slug}_{stamp}.md")
    with open(fp, "w", encoding="utf-8", newline=chr(10)) as f:
        f.write(build_request(name, slug, pages, result))
    return fp


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []
    nl = chr(10)

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    pages = [{"url": "https://www.p-world.co.jp/x", "host": "p-world.co.jp",
              "ok": True, "reason": "OK",
              "fields": {"payout_rate": {"1": "97.3%"}}},
             {"url": "https://chonborista.com/y", "host": "chonborista.com",
              "ok": True, "reason": "OK",
              "fields": {"payout_rate": {"1": "97.3%"}}}]
    res = _sl.compare(pages)
    req = build_request("Lテスト機", "test", pages, res)

    t("★依頼文に出典URLが全部入る（Codexが自分で開いて確かめられる）★",
      all(p["url"] in req for p in pages))
    t("★採用しようとしている値が入る★", "97.3%" in req)
    t("★★合格基準を書かない★★（書くと相手が引っ張られて甘くなる）",
      "合格" not in req and "問題なければ" not in req and "OKなら" not in req)
    t("★今の機種以外の話題を持ち出させない★",
      "範囲外" in req and "他機種" in req)
    t("　食い違いがあれば依頼文に出る",
      "食い違" in build_request("x", "x", pages, {
          "adopted": {}, "thin": {},
          "need_third": {"payout_rate": {"a": ["p-world"]}}}))
    t("　1出典だけの項目も隠さず書く",
      "1つの出典しか取れていない" in build_request("x", "x", pages, {
          "adopted": {}, "need_third": {},
          "thin": {"payout_rate": {"why": "x", "sources": ["p-world"]}}}))
    t("★採用できた値が無いことも隠さない★",
      "採用できた値はありません" in build_request("x", "x", pages, {
          "adopted": {}, "need_third": {}, "thin": {}}))
    t("　依頼文は作業フォルダの中に置く（外だとCodexが読めない）",
      REQ_DIR.startswith(BASE))

    ng = [n for n, ok in results if not ok]
    print(f"{nl}{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


import os as _os_lp                 # noqa: E402
import sys as _sys_lp               # noqa: E402
_sys_lp.path.insert(0, _os_lp.path.dirname(_os_lp.path.abspath(__file__)))
import local_paths as _lp           # noqa: E402
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--name")
    ap.add_argument("--slug")
    ap.add_argument("--url", action="append")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not (args.name and args.slug and args.url):
        ap.print_help()
        return 0
    pages = [_sl.read_page(u, args.name) for u in args.url]
    res = _sl.compare(pages)
    fp = write_request(args.name, args.slug, pages, res)
    print(f"依頼文を作りました: {fp}")
    print(f"採用候補 {len(res['adopted'])} 件 / 食い違い {len(res['need_third'])} 件 / "
          f"1出典のみ {len(res['thin'])} 件")
    print()
    print("次に実行してください（★往復の回数はガードが数えます★）:")
    print("  python scripts/task_guard.py codex --task add-machine")
    print(f'  bash scripts/codex_with_lock.sh <CTX> "{fp}" '
          f'_lp.doc("gpt_research/spec_{args.slug}.txt") 900 2 high')
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(f"★想定外の失敗 {type(e).__name__}: {e}★")
        raise SystemExit(1)

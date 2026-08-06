#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""fix_stale_preview.py — 古い先行記事から「時間で嘘になる文」を落とす。

★何のための道具か（2026-08-06）★
  導入前に作った先行記事が、導入後もそのまま公開され続けていた。
  7機種すべてが 2026-08-03 に導入済みなのに、本文は
  「2026年7月時点で解析データが公開されていません」「導入日に向けて
  各解析サイトで順次データが公開される予定です」のままだった。
  ★読者から見ると、出ている機種を『まだ出ていない』と書いている状態★。

★やること／やらないこと★
  やる   : 時間で嘘になる書き方を、時間が経っても嘘にならない書き方へ直す
  やらない: 新しい事実（天井・狙い目など）を書く。**値は1つも足さない**

使い方:
    python scripts/fix_stale_preview.py            # 下見（全機種の差分）
    python scripts/fix_stale_preview.py --slug x   # 1機種だけ
    python scripts/fix_stale_preview.py --apply    # 実際に直す
    python scripts/fix_stale_preview.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import safe_json as _sj                  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DETAILS = os.path.join(BASE, "assets", "data", "machine-details")

PENDING = "当サイトでは未確認です。確認でき次第、この欄に掲載します。"

# ★丸ごと落とす文★（時間が経つと嘘になる・予想を述べているだけ）
DROP_SENTENCE = (
    "導入日に近づくにつれて",
    "導入日に向けて",
    "順次情報が公開される",
    "順次データが公開される",
    "解析判明後に掲載します",
    "判明次第随時更新します",
    "判明次第このページを更新します",
)

# ★文ごと「未確認です」に置き換える★（部分置換だと文が繋がって壊れる）
#   例:「天井ゲーム数・恩恵は2026年7月時点で解析データが公開されていません。」
#      → 尻尾だけ置き換えると「…恩恵当サイトでは未確認です」になる。
STALE_SENTENCE = (
    "解析データが公開されていません",
    "解析データが揃っていません",
    "未解析です",
    "解析が出ておらず",
)

# ★文の中だけを言い換える★（文そのものは残す）
INLINE = (
    (re.compile(r"（20\d\d年\d+月時点）"), ""),
    (re.compile(r"【20\d\d年\d+月時点・公式未確認】"), "【公式未確認】"),
    # ★助詞まで含めて消す★（2026-08-06。「現時点では」の「現時点で」だけを
    #   消して「は具体的な…」という壊れた文を7機種に公開してしまった）
    # ★ただし、次がひらがなの時は触らない★（2026-08-06・Codex126回目 #3。
    #   「現時点ではっきりしていません」「現時点でもっと…」の「は」「も」は
    #   助詞ではなく語の一部で、消すと「っきり」「っと」になる）
    (re.compile(r"20\d\d年\d+月時点で(?:は|も)?、?(?![ぁ-ん])"), ""),
    (re.compile(r"現時点で(?:は|も)?、?(?![ぁ-ん])"), ""),
    (re.compile(r"導入済みですが、"), ""),
)


# ★言い換えたあとに出てはいけない並び★（消しすぎで文が壊れた印）
#   ★語の先頭は助詞ではない★（2026-08-06・Codex126回目 #8。
#     「。はじめに」「。もっとも」を壊れと誤判定していた）
_BROKEN = re.compile(
    r"[、。](?:は(?!じめ|っきり|たして|なは)|が(?!っかり|くじつ)"
    r"|を|に(?!わか)|も(?!し|う|っと|ちろん|はや|の)|で(?!き|も|は))")
#   ★助詞の後ろに文字が続くことを求めない★（2026-08-06・Codex129回目 #3。
#     「確認します。は。」のように助詞で文が終わる壊れ方を見逃していた）


class Broken(Exception):
    """言い換えた結果、日本語として壊れた（★書かずに止める★）。"""


def _sub_checked(pat, rep: str, text: str) -> str:
    """言い換えながら、★つなぎ目が壊れていないかその場で見る★

    2026-08-06・Codex127回目 #3。文字列全体の「壊れ方の数」を比べる方式では、
    同じ壊れ方が別の場所へ移っただけの時に見逃した（元から「、を見」があると、
    新しく「、を見」ができても数が変わらない）。
    消した場所そのもののつなぎ目を見れば、位置ごとに確実に分かる。
    """
    out, last = [], 0
    for m in pat.finditer(text):
        out.append(text[last:m.start()])
        out.append(rep)
        last = m.end()
        # ★右側は切らずに渡す★（2026-08-06・Codex128回目 #2。
        #   2文字だけだと「。はじめに」の「じめ」を見切れず、正しい文を
        #   壊れと誤判定した）
        joint = "".join(out)[-1:] + text[last:]
        if _BROKEN.match(joint):
            raise Broken(f"言い換えでつなぎ目が壊れます: …{joint[:14]}…")
    out.append(text[last:])
    return "".join(out)


def fix_text(t: str) -> str:
    """1つの文字列を直す（★値は足さない・消すか言い換えるだけ★）。"""
    out = []
    seen_pending = False
    for sent in re.split(r"(?<=。)", t):
        if not sent.strip():
            continue
        if any(w in sent for w in DROP_SENTENCE):
            continue                      # 時間が経つと嘘になる文は落とす
        if any(w in sent for w in STALE_SENTENCE):
            if not seen_pending:          # ★同じ断りを何度も並べない★
                out.append(PENDING)
                seen_pending = True
            continue
        for pat, rep in INLINE:
            sent = _sub_checked(pat, rep, sent)
        if sent.strip():
            out.append(sent.strip())
    got = re.sub(r"[ 　]{2,}", " ", "".join(out).strip())
    # ★文どうしのつなぎ目も見る★（文を丸ごと落とした結果の並び）
    new = Counter(_BROKEN.findall(got)) - Counter(_BROKEN.findall(t))
    if new:
        raise Broken(f"言い換えで文が壊れます: {got[:60]}")
    return got


def fix_detail(detail: dict) -> tuple:
    """記事全体を直す。(直した記事, 変えた場所の一覧) を返す。"""
    changes = []

    def walk(node, path):
        if isinstance(node, dict):
            return {k: walk(v, f"{path}.{k}") for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v, f"{path}[{i}]") for i, v in enumerate(node)]
        if isinstance(node, str):
            got = fix_text(node)
            if got != node:
                changes.append({"path": path, "before": node, "after": got})
            return got
        return node

    out = walk(detail, "")
    for sec in out.get("sections") or []:
        if not isinstance(sec.get("body"), list):
            continue
        # ★空になった段落は消す★（消した結果の空文字を残さない）
        body = [b for b in sec["body"] if str(b).strip()]
        # ★同じ断りは節に1つだけ★（2026-08-06。段落ごとに直したので、
        #   1つの節に「未確認です」が2回並ぶことがあった）
        seen = False
        kept = []
        for b in body:
            if str(b).strip() == PENDING:
                if seen:
                    continue
                seen = True
            kept.append(b)
        if kept != sec["body"]:
            changes.append({"path": f"sections[{sec.get('title')}].body",
                            "before": " / ".join(map(str, sec["body"]))[:60],
                            "after": " / ".join(map(str, kept))[:60]})
        sec["body"] = kept
    return out, changes


def targets(slug: str | None = None) -> list:
    """直す対象（旧方式の先行記事）。"""
    import page_decision as _pd
    ms = _sj.read_json(os.path.join(BASE, "assets", "data", "machines.json"),
                       expect=(dict, list))
    ms = ms["machines"] if isinstance(ms, dict) else ms
    out = []
    for m in ms:
        if slug and m.get("slug") != slug:
            continue
        try:
            if _pd.machine_class(m) == "LEGACY_PREVIEW":
                out.append(m["slug"])
        except Exception:                 # noqa: BLE001
            continue
    return out


def run(slug: str, apply_it: bool) -> dict:
    import grow_legacy as _gl              # ★書き込み方法をそろえる★
    p = os.path.join(DETAILS, f"{slug}.json")
    sha_before = _gl._sha(p)               # ★読む前に指紋を取る★
    before = _sj.read_json(p, expect=dict)
    after, changes = fix_detail(json.loads(json.dumps(before)))
    res = {"slug": slug, "changes": changes, "wrote": False}
    if not changes:
        return res
    # ★空の節を作らない★
    for sec in after.get("sections") or []:
        if isinstance(sec.get("body"), list) and not sec["body"] \
                and not sec.get("tables"):
            res["problems"] = [f"本文が空になる節があります: {sec.get('title')!r}"]
            return res
    if apply_it:
        # ★鍵の中で「もう一度確かめる→置き換える」★（2026-08-06・Codex127回目）
        #   同じ7機種を触る grow_legacy と書き込み方法をそろえる。
        try:
            _gl._write(p, after, sha_before)
        except _gl.Halt as e:
            res["problems"] = [f"★止めました★ {e}"]
            return res
        res["wrote"] = True
    return res


# ------------------------------------------------------------------ selftest

def selftest() -> int:
    ok, ran = True, [0]

    def t(name, cond):
        nonlocal ok
        ran[0] += 1
        print(("✅ " if cond else "❌ ") + name)
        ok = ok and bool(cond)

    t("★★『いつ時点で未解析』は文ごと差し替える★★（尻尾だけ直すと文が壊れる）",
      fix_text("天井ゲーム数・恩恵は2026年7月時点で解析データが公開されていません。")
      == PENDING)
    t("　カッコ書きの時点も消える",
      fix_text("公表されているスペックは以下の通りです（2026年7月時点）。")
      == "公表されているスペックは以下の通りです。")
    t("★★導入日を待つ書き方は落とす★★（もう導入されている）",
      "順次情報" not in fix_text(
          "天井は未確認です。導入日（2026年8月3日）に近づくにつれて"
          "各解析サイトで順次情報が公開される見込みです。"))
    t("　噂の見出しは『公式未確認』だけ残る",
      fix_text("【2026年7月時点・公式未確認】") == "【公式未確認】")
    t("★★事実はそのまま残す★★（メーカー・導入日・ゲーム性）",
      fix_text("**メーカー**：ユニバーサルエンターテインメント")
      == "**メーカー**：ユニバーサルエンターテインメント"
      and fix_text("2026年8月3日にホール導入されました。")
      == "2026年8月3日にホール導入されました。")
    t("★★数値を足さない★★（消すか言い換えるだけ）",
      not re.search(r"\d+G", fix_text("天井は2026年7月時点で未解析です。")))
    t("　同じ断りを何度も並べない",
      fix_text("天井は未解析です。狙い目も解析データが揃っていません。") == PENDING)
    d = {"sections": [{"title": "天井・恩恵", "body": [
        "天井は2026年7月時点で解析データが公開されていません。",
        "判明次第随時更新します。"]}]}
    got, ch = fix_detail(json.loads(json.dumps(d)))
    t("　落とした結果、空の段落は残さない",
      got["sections"][0]["body"] == [PENDING] and len(ch) >= 2)
    d2 = {"sections": [{"title": "天井・恩恵", "body": [
        "天井は2026年7月時点で解析データが公開されていません。",
        "狙い目も現時点で解析データが揃っていません。"]}]}
    got2, _ = fix_detail(json.loads(json.dumps(d2)))
    t("★★同じ断りは節に1つだけ★★（段落ごとに直すと2回並ぶ）",
      got2["sections"][0]["body"] == [PENDING])
    t("★★『現時点では』は助詞ごと消す★★（2026-08-06に7機種で壊した形）",
      fix_text("挙動が解析待ちのため、現時点では具体的な目安を出せません。")
      == "挙動が解析待ちのため、具体的な目安を出せません。")
    t("　『現時点で』単体も消える",
      fix_text("現時点で公表されている情報は以下です。")
      == "公表されている情報は以下です。")
    broke = False
    try:
        fix_text("XX現時点でZZ。")     # 消すと「XXZZ」になるだけ＝壊れない
        fix_text("確認します。導入済みですが、を見ます。")
    except Broken:
        broke = True
    t("★★壊れた並びが出たら書かずに止める★★", broke)
    t("★★次がひらがなの「は」「も」は助詞ではない★★（Codex126回目 #3）",
      fix_text("現時点ではっきりしていません。") == "現時点ではっきりしていません。"
      and fix_text("現時点でもっと詳しい情報を確認します。")
      == "現時点でもっと詳しい情報を確認します。")
    t("　語の先頭を壊れと誤判定しない",
      fix_text("注記です。（2026年7月時点）はじめに確認します。")
      == "注記です。はじめに確認します。")
    broke2 = False
    try:                                  # 元から壊れていても、増えたら止める
        fix_text("あ、を確認。導入済みですが、に注意します。")
        # ★同じ壊れ方が別の場所へ移るだけの迂回（Codex127回目 #3）★
        fix_text("確認します、導入済みですが、を見ます。")
    except Broken:
        broke2 = True
    t("★★元から壊れていても、増えたぶんは見逃さない★★", broke2)
    broke3 = False
    try:                                  # 助詞で文が終わる壊れ方（#3）
        fix_text("確認します。導入済みですが、は。")
    except Broken:
        broke3 = True
    t("★★助詞で文が終わる壊れ方も見つける★★（Codex129回目 #3）", broke3)

    # ★実際に書くところまで通す★（2026-08-06・Codex128回目 #1。
    #   書き込み部分を試験していなかったので、変数の書き忘れに気づけなかった）
    import tempfile
    tmp = tempfile.mkdtemp()
    keep = globals()["DETAILS"]
    try:
        globals()["DETAILS"] = tmp
        art = {"slug": "t1", "name": "試験機",
               "sections": [{"title": "天井・恩恵",
                             "body": ["天井は2026年7月時点で未解析です。"]}]}
        fp = os.path.join(tmp, "t1.json")
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(art, f, ensure_ascii=False, indent=1)
            f.write("\n")
        r = run("t1", True)
        got = json.load(open(fp, encoding="utf-8"))
        t("★★書くところまで実際に通る★★（--apply が動かない事故を止める）",
          r.get("wrote") and not r.get("problems")
          and got["sections"][0]["body"] == [PENDING])
    finally:
        globals()["DETAILS"] = keep
    print(f"\n{ran[0]}/{ran[0]} 合格" if ok else "\n不合格あり")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="古い先行記事の言い回しを直す")
    ap.add_argument("--slug")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    total = 0
    for s in targets(a.slug):
        r = run(s, a.apply)
        total += len(r["changes"])
        if r.get("problems"):
            print(f"★{s}: " + " / ".join(r["problems"]))
            continue
        print(f"{s}: {len(r['changes'])}箇所" + ("（書きました）" if r["wrote"] else ""))
        for c in r["changes"][:3]:
            print(f"    - {c['before'][:46]}")
            print(f"      → {c['after'][:46] or '（削除）'}")
    print(f"\n合計 {total}箇所" + ("" if a.apply else "（下見です。--apply で直します）"))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except _sj.SafeJsonError as e:
        print(f"★入力データが読めません: {e}★")
        raise SystemExit(1)

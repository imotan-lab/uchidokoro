#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""★その場で決めて直すための「材料そろえ」と「適用」★（2026-08-21）

★なぜ作ったか（運営者の指示）★
  > だから台帳をなくそうよ　その場で２AI判断で記事作成してってば。

  これは2026-08-12の運営者決定そのもの＝
    ★「人が直す」で終わる項目を作らない。順番は ①機械 → ②2AI → ③メール だけ★
  ところが品質レビューは「見つけて台帳へ積む」で止まり、
  台帳を閉じていたのは30日間ずっと対話セッション（人）だった。
  ＝★人を中継役にしていた★

★この道具の役割★
  ①機械が「その場で決められる候補」を1機種ぶん集めて、決まった形で出す（gather）
  ②2AIが決めた結果を受け取って、実際に直す（apply）

★決めるのは2AI（この道具ではない）★
  gather は候補を並べるだけ。どれを直すかは
  Claude と Codex が同じ材料を読んで決める。

★★その場で決めてよいのは「記事内で完結する食い違い」だけ★★
  品質レビューの評価基準がもともとそう縛られている＝
  「C評価の根拠にできるのは記事内で完結する事実のみ」。
  ＝定義上、外部の出典を見なくても判定できる。
  ★出典が要るもの（値そのものの正誤）はここで決めない★＝メールへ回す。

★★直してよいのは「消す・言い換える」だけ★★
  ・重複した行を消す
  ・常体の文末を「です・ます」にそろえる
  ・型式名・検定番号の行を落とす
  ・時間で嘘になる語を落とす
  ★新しい数値・新しい事実は書かない★（新値発明禁止）

★★「消す」も、選び方によっては事実の判断になる★★（Codexの設計レビュー）
  「500Gと600Gが矛盾している」とき 500G だけ消せば、
  ★600G を正解扱いしたことになる★。出典を見ずにそれを決めてはいけない。
  → ★消す行に出てくる数値が、消したあとの記事にも全部残っているときだけ許す★
    （＝重複を1つにするだけ。どちらが正解かは選んでいない）

使い方:
  python scripts/decide_now.py gather --slug hokuto            材料を出す
  python scripts/decide_now.py gather --slug hokuto --json
  python scripts/decide_now.py apply --file <2AIが決めたファイル>
  python scripts/decide_now.py apply --file <…> --apply
  python scripts/decide_now.py --selftest
"""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import safe_json as _sj              # noqa: E402
import find_duplicate_prose as _fdp  # noqa: E402
import fix_plain_style as _fps       # noqa: E402
import strip_model_code as _smc      # noqa: E402

DETAILS = os.path.join(BASE, "assets", "data", "machine-details")
SCHEMA = "decide-now/v1"

# ★★種類の名簿は「扱ってよいもの」ではない★★（2026-08-22・運営者の指摘）
#   > いやいやいや　だから機械的にやらずに２AIで判断して結論を出すようにしてよ
#
#   ★直す前の間違い★＝ここに「重複・文体・型式名・時制」と書いて、
#   **それ以外は扱わない**ことにしていた。＝先に型を決めておくやり方で、
#   「機械的にやらない」の逆だった（何度も指摘されている型）。
#
#   ★いまの役★＝機械が**先に気づけたものに付ける札**でしかない。
#   2AIはこの札に縛られず、記事そのものを読んで決める。
#   ★手がかりは網羅ではない★＝ここに出ないものも直してよい。
HINT_KINDS = ("重複", "文体", "型式名", "時制")

# ★★守るのは「型」ではなく「変更そのものの性質」★★
#   （Codexの線引きもこちらだった）
#   1. 新しい事実・新しい数値を書かない（消す・言い換えるだけ）
#   2. 数値が変わる言い換えは受け取らない
#   3. 消して数値が記事から無くなるなら受け取らない（＝どちらが正解かを選ばない）
#   4. 判断者は2つ以上
#   5. 記事の指紋を照合（見つけたときから変わっていたらやり直し）
#   6. 直したあと、その文が消えたことを機械が確かめられる（recheck の text_gone）
#   ★この6つを満たすなら、何を直すかは2AIが決める★


def _detail(slug: str):
    p = os.path.join(DETAILS, slug + ".json")
    if not os.path.isfile(p):
        return None
    return _sj.read_json(p, expect=dict)


def gather(slug: str) -> dict:
    """1機種ぶんの材料を集める（★決めない・直さない★）。

    ★★渡すのは記事そのもの★★（2026-08-22・運営者の指摘）
      機械が挙げる手がかりは**網羅ではない**。2AIは記事の全文を読んで、
      手がかりに無いものも含めて自分で見つけて結論を出す。
      ここが「候補の一覧だけ」を渡す作りだと、
      ★機械が気づけた型しか直らない★＝先に型を決めているのと同じになる。
    """
    out = {"slug": slug, "schema_version": SCHEMA,
           "hints": [], "candidates": []}
    d = _detail(slug)
    if not isinstance(d, dict):
        out["problems"] = ["記事データがありません"]
        return out

    # ★★記事の全文★★（2AIが読む本体。手がかりはこの下の「おまけ」）
    out["article"] = {
        "name": d.get("name") or slug,
        "sections": [
            {"title": sec.get("title") or "",
             "type": sec.get("type") or "",
             "body": [x for x in (sec.get("body") or [])
                      if isinstance(x, str)],
             "notes": [t.get("note") for t in (sec.get("tables") or [])
                       if isinstance(t, dict) and t.get("note")]}
            for sec in (d.get("sections") or []) if isinstance(sec, dict)
        ],
    }
    out["how_to_decide"] = (
        "★手がかりは網羅ではありません★。記事の全文を読んで、"
        "手がかりに無い食い違い・重複・言い回しの問題も自分で見つけてください。"
        "決めてよいのは『消す』『意味を変えずに言い換える』だけです。"
        "★新しい数値・新しい事実は書かないでください★。"
        "数字が食い違っているとき、片方を消すのは"
        "『もう片方が正解だ』と決めたのと同じなので、出典を見ずにやらないでください"
        "（機械も受け取りません）。"
    )

    # ① 同じ判断を2度読ませている候補（★どちらを消すかは決めない★）
    for g in _fdp.scan(_fdp.DEFAULT_SECTIONS, 0.62, slug):
        for pr in g.get("pairs") or []:
            out["candidates"].append({
                "kind": "重複", "section": g["section"],
                "ratio": pr["ratio"], "a": pr["a"], "b": pr["b"],
                "decide": "どちらか一方を消すなら、その行を逐語で drop に書く。"
                          "★情報を包含する側を残す★（箇条書きか散文かは関係ない）。"
                          "片方にしか無い情報があれば消さない。",
            })

    # ② 常体の文末（★書き換える対がある形だけ★）
    for where, old, new, title in _fps.plan_for(d):
        out["candidates"].append({
            "kind": "文体", "section": title,
            "before": old, "after": new,
            "decide": "文末の丁寧さだけが変わる。中身が変わらないなら apply でよい。",
        })

    # ③ 型式名・検定番号（★記事には書かない★という決まり）
    pl = _smc.plan_for(d)
    for _si, _bi, line in pl["drop_body"]:
        out["candidates"].append({
            "kind": "型式名", "section": "", "drop": line,
            "decide": "記事には書かない決まり。同定用の値は identity に残る。",
        })
    for _si, _bi, old, cut in pl.get("cut_paren") or []:
        out["candidates"].append({
            "kind": "型式名", "section": "", "before": old, "after": cut,
            "decide": "括弧の中が型式名だけなので括弧ごと落とす。",
        })
    for _si, _bi, line in pl["mixed"]:
        out["candidates"].append({
            "kind": "型式名", "section": "", "mixed": line,
            "decide": "★他の情報が混ざっている★。行ごと消すと情報が減るので、"
                      "どこを落とすかを2AIで決める。",
        })

    # ④ 時間で嘘になる語
    txt = io.open(os.path.join(DETAILS, slug + ".json"),
                  encoding="utf-8").read()
    for w in ("導入予定", "登場予定", "導入前", "導入後に"):
        if w in txt:
            out["candidates"].append({
                "kind": "時制", "section": "", "word": w,
                "decide": "時間で嘘になる語。落とすか言い換える（数値は触らない）。",
            })

    # ★機械が先に気づけたものは「手がかり」として別に置く★
    #   （candidates という名前のままだと「これが全部」と読める）
    out["hints"] = out["candidates"]
    out["counts"] = {k: sum(1 for c in out["hints"] if c["kind"] == k)
                     for k in HINT_KINDS}
    out["counts"]["_note"] = "★この数は機械が気づけた分だけ★（網羅ではない）"
    return out


def _load_decision(path: str) -> dict:
    d = _sj.read_json(path, expect=dict)
    if d.get("schema_version") != SCHEMA:
        raise ValueError(f"知らない形です: {d.get('schema_version')!r}")
    by = d.get("decided_by")
    if not isinstance(by, list) or len(by) < 2:
        raise ValueError("decided_by に判断者が2つ以上要ります（2AIで決めるため）")
    if not d.get("slug"):
        raise ValueError("slug がありません")
    acts = d.get("actions")
    if not isinstance(acts, list) or not acts:
        raise ValueError("actions がありません")
    for a in acts:
        if a.get("op") not in ("drop", "replace"):
            raise ValueError(f"知らない操作です: {a.get('op')!r}")
        if not a.get("why"):
            raise ValueError("理由の無い操作は受け取りません")
        if a["op"] == "drop" and not a.get("text"):
            raise ValueError("drop には消す行の逐語が要ります")
        if a["op"] == "replace" and not (a.get("before") and a.get("after")):
            raise ValueError("replace には before と after が要ります")
    return d


def _numbers(s: str) -> list:
    import re
    return re.findall(r"\d+(?:\.\d+)?", str(s or ""))


def _simulate(detail: dict, plan: list) -> str:
    """★決定を全部あてたら記事がどうなるか★（書かずに文字列で返す）

    ★書き込みと同じ手順でなければ意味がない★ので、
    実際に書く側（下の `apply_it` の中）と同じ順で当てる。
    """
    import copy
    d = copy.deepcopy(detail)
    dropping = {}
    for kind, si, bi, a in plan:
        if kind == "replace":
            d["sections"][si]["body"][bi] = a["after"]
        elif kind == "table_note":
            d["sections"][si]["tables"][bi]["note"] = a["after"]
        elif kind == "drop":
            dropping.setdefault(si, set()).add(bi)
    for si, idxs in dropping.items():
        body = d["sections"][si]["body"]
        d["sections"][si]["body"] = [x for i, x in enumerate(body)
                                     if i not in idxs]
    return json.dumps(d, ensure_ascii=False)


def apply_decision(path: str, apply_it: bool = False) -> dict:
    """2AIが決めたとおりに直す（★消す・言い換えるだけ★）。"""
    dec = _load_decision(path)
    slug = dec["slug"]
    p = os.path.join(DETAILS, slug + ".json")
    if not os.path.isfile(p):
        return {"slug": slug, "problems": ["記事データがありません"]}
    d = _sj.read_json(p, expect=dict)
    raw = io.open(p, encoding="utf-8").read()

    result = {"slug": slug, "done": [], "problems": []}

    # ★判断したときから記事が変わっていないか★
    import hashlib
    want = str(dec.get("source_sha256") or "")
    got = hashlib.sha256(raw.encode("utf-8").replace(b"\r\n", b"\n")).hexdigest()
    if want and want != got:
        result["problems"].append(
            f"判断したときから記事が変わっています（{want[:12]}… → {got[:12]}…）")
        return result

    for a in dec["actions"]:
        if a["op"] == "replace":
            # ★★数値が変わる言い換えは受け取らない★★（新値発明禁止）
            if _numbers(a["before"]) != _numbers(a["after"]):
                result["problems"].append(
                    f"数値が変わる言い換えは受け取りません: {a['before'][:40]!r}")
                return result
        if a["op"] == "drop":
            # ★★消すことで「どちらが正解か」を選んでしまう場合は受け取らない★★
            #   （2026-08-21・Codexの設計レビュー）
            #   ★指摘の要点★＝
            #     「500Gと600Gが矛盾している」とき 500G だけ消せば、
            #     ★600G を正解扱いしたことになる★。
            #     出典を見ずにそれを決めてはいけない。
            #
            #   ★許すのは「同じ事実が記事のどこかに残る」削除だけ★
            #     ＝重複を1つにするだけなら、どちらが正解かは選んでいない。
            #
            #   見方＝消す行に出てくる数値が、消したあとの記事にも
            #   すべて残っているか。1つでも消えるなら受け取らない。
            nums = _numbers(a["text"])
            if nums:
                # ★消すのは1行だけ★＝同じ文が2つあるなら1つは残る
                rest = raw.replace(a["text"], "", 1)
                # ★★数値は「文字が含まれるか」で見ない★★
                #   （2026-08-22・実データで素通りして分かった）
                #   ★直す前は n not in rest と書いていた★＝部分一致なので
                #   「97.7%」の中の 7 に当たって通ってしまった。
                #   ＝「最大7pt」を消しても「7は残っている」と誤判定した。
                lost = sorted(set(nums) - set(_numbers(rest)))
                if lost:
                    result["problems"].append(
                        "消すと記事から無くなる数値があります: "
                        + " / ".join(lost[:4])
                        + "（どちらが正解かを選ぶことになるので受け取りません。"
                        "出典で確かめてください）")
                    return result

    # 実際にあたるかを先に全部確かめる（1件でも外れたら何もしない）
    plan = []
    for a in dec["actions"]:
        hit = False
        for si, sec in enumerate(d.get("sections") or []):
            body = sec.get("body") or []
            for bi, line in enumerate(body):
                if not isinstance(line, str):
                    continue
                if a["op"] == "drop" and line == a["text"]:
                    plan.append(("drop", si, bi, a))
                    hit = True
                    break
                if a["op"] == "replace" and line == a["before"]:
                    plan.append(("replace", si, bi, a))
                    hit = True
                    break
            if hit:
                break
            # 表の注記も見る
            for ti, tbl in enumerate(sec.get("tables") or []):
                if a["op"] == "replace" and tbl.get("note") == a["before"]:
                    plan.append(("table_note", si, ti, a))
                    hit = True
                    break
            if hit:
                break
        if not hit:
            result["problems"].append(
                f"実データに無い行です（記事が変わった可能性）: "
                f"{(a.get('text') or a.get('before'))[:44]!r}")
            return result

    # ★セクションが空にならないこと★
    dropping = {}
    for kind, si, bi, _a in plan:
        if kind == "drop":
            dropping.setdefault(si, set()).add(bi)
    for si, idxs in dropping.items():
        n = len(d["sections"][si].get("body") or [])
        if len(idxs) >= n:
            result["problems"].append(
                f"「{d['sections'][si].get('title')}」が空になります")
            return result

    # ★★全部やったあとで数値が消えていないか★★（2026-08-22・実データで穴を発見）
    #   ★1件ずつ見るだけでは足りなかった★＝
    #   重複した2行を**両方**消す決定を出すと、
    #   どちらの行も「単体では、もう片方に同じ数値が残る」ので通ってしまい、
    #   ★結果としてその数値が記事から丸ごと消える★。
    #   実際 goji_eva で「700G＋α・5回・360G」が全部消える決定が通った。
    #   → ★決定を全部あてた後の姿で数え直す★。
    after_all = _simulate(d, plan)
    # ★数値は token で比べる★（部分一致だと「97.7」の中の「7」に当たる）
    lost = sorted(set(_numbers(raw)) - set(_numbers(after_all)))
    if lost:
        result["problems"].append(
            "全部やると記事から無くなる数値があります: " + " / ".join(lost[:6])
            + "（重複を1つにするだけのはずが、両方消える決定になっています）")
        return result

    for kind, si, bi, a in plan:
        result["done"].append({"op": a["op"], "why": a["why"][:60]})

    if apply_it:
        for kind, si, bi, a in plan:
            sec = d["sections"][si]
            if kind == "replace":
                sec["body"][bi] = a["after"]
            elif kind == "table_note":
                sec["tables"][bi]["note"] = a["after"]
        for si, idxs in dropping.items():
            body = d["sections"][si]["body"]
            d["sections"][si]["body"] = [x for i, x in enumerate(body)
                                         if i not in idxs]
        tmp = p + ".tmp"
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.replace(tmp, p)
        result["wrote"] = True
    return result


def _selftest() -> int:
    import tempfile
    ng = []

    def t(name, cond):
        print(("✅ " if cond else "❌ ") + name)
        if not cond:
            ng.append(name)

    td = tempfile.mkdtemp()
    _keep = globals()["DETAILS"]
    globals()["DETAILS"] = td
    try:
        D = {"slug": "x", "sections": [
            {"title": "ヤメ時の判断",
             "body": ["A の行です。", "B の行です。", "C の行です。"]}]}
        p = os.path.join(td, "x.json")
        with io.open(p, "w", encoding="utf-8", newline="\n") as f:
            json.dump(D, f, ensure_ascii=False, indent=1)
            f.write("\n")

        def dec(actions, by=("Claude", "codex"), sha=None):
            q = os.path.join(td, "d.json")
            io.open(q, "w", encoding="utf-8").write(json.dumps(
                {"schema_version": SCHEMA, "slug": "x",
                 "decided_by": list(by), "actions": actions,
                 **({"source_sha256": sha} if sha else {})},
                ensure_ascii=False))
            return q

        r = apply_decision(dec([{"op": "drop", "text": "B の行です。",
                                 "why": "言い換え"}]))
        t("★決めたとおりに消せる★", not r["problems"] and len(r["done"]) == 1)

        r2 = apply_decision(dec([{"op": "drop", "text": "B の行です",
                                  "why": "1文字違い"}]))
        t("★★逐語が1文字でも違えば何もしない★★", bool(r2["problems"]))

        r3 = apply_decision(dec([{"op": "replace",
                                  "before": "A の行です。",
                                  "after": "A の行は 999G です。",
                                  "why": "数値を足す"}]))
        t("★★数値が変わる言い換えは受け取らない★★（新値発明禁止）",
          bool(r3["problems"]))

        r4 = apply_decision(dec([{"op": "drop", "text": "A の行です。",
                                  "why": "…"},
                                 {"op": "drop", "text": "B の行です。",
                                  "why": "…"},
                                 {"op": "drop", "text": "C の行です。",
                                  "why": "…"}]))
        t("★★セクションが空になる決定は受け取らない★★", bool(r4["problems"]))

        try:
            _load_decision(dec([{"op": "drop", "text": "A の行です。",
                                 "why": "…"}], by=("Claude",)))
            t("★★判断者が1人の決定は受け取らない★★", False)
        except ValueError as e:
            t("★★判断者が1人の決定は受け取らない★★", "2つ以上" in str(e))

        try:
            _load_decision(dec([{"op": "drop", "text": "A の行です。"}]))
            t("　理由の無い操作は受け取らない", False)
        except ValueError:
            t("　理由の無い操作は受け取らない", True)

        try:
            _load_decision(dec([{"op": "rewrite", "text": "x", "why": "y"}]))
            t("　知らない操作は受け取らない", False)
        except ValueError:
            t("　知らない操作は受け取らない", True)

        r5 = apply_decision(dec([{"op": "drop", "text": "B の行です。",
                                  "why": "…"}], sha="0" * 64))
        t("★★判断したときから記事が変わっていたら何もしない★★",
          bool(r5["problems"]))

        # ★1件でも外れたら、通る分も書かない★
        r6 = apply_decision(dec([{"op": "drop", "text": "B の行です。",
                                  "why": "…"},
                                 {"op": "drop", "text": "無い行", "why": "…"}]),
                            apply_it=True)
        t("★★1件でも外れたら何も書かない★★", bool(r6["problems"]))
        with io.open(p, encoding="utf-8") as f:
            t("　記事はそのまま",
              len(json.load(f)["sections"][0]["body"]) == 3)

        # ★★消すことで「どちらが正解か」を選んでしまう場合★★
        #   （2026-08-21・Codexの設計レビュー。対照実験つき）
        E = {"slug": "y", "sections": [
            {"title": "天井・恩恵",
             "body": ["通常時の天井は 500G です。",
                      "通常時の天井は 600G です。",
                      "恩恵は AT 直撃です。"]}]}
        q = os.path.join(td, "y.json")
        with io.open(q, "w", encoding="utf-8", newline="\n") as f:
            json.dump(E, f, ensure_ascii=False, indent=1)
            f.write("\n")

        def dec_y(actions):
            r = os.path.join(td, "dy.json")
            io.open(r, "w", encoding="utf-8").write(json.dumps(
                {"schema_version": SCHEMA, "slug": "y",
                 "decided_by": ["Claude", "codex"], "actions": actions},
                ensure_ascii=False))
            return r

        r8 = apply_decision(dec_y([{"op": "drop",
                                    "text": "通常時の天井は 500G です。",
                                    "why": "600Gのほうが正しそう"}]))
        t("★★消すと数値が記事から無くなる決定は受け取らない★★"
          "（どちらが正解かを選ぶことになる）",
          bool(r8["problems"]) and "500" in "".join(r8["problems"]))

        # 同じ数値が他にも残っている＝重複を1つにするだけ → 通る
        F = {"slug": "z", "sections": [
            {"title": "天井・恩恵",
             "body": ["通常時の天井は 500G です。",
                      "通常時の天井は 500G です。",
                      "恩恵は AT 直撃です。"]}]}
        with io.open(os.path.join(td, "z.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump(F, f, ensure_ascii=False, indent=1)
            f.write("\n")
        rz = os.path.join(td, "dz.json")
        io.open(rz, "w", encoding="utf-8").write(json.dumps(
            {"schema_version": SCHEMA, "slug": "z",
             "decided_by": ["Claude", "codex"],
             "actions": [{"op": "drop",
                          "text": "通常時の天井は 500G です。",
                          "why": "同じ行が2つある"}]},
            ensure_ascii=False))
        r9 = apply_decision(rz)
        t("　同じ数値が他の行にも残る削除（重複）は通る", not r9["problems"])

        # ★★数値は token で比べる★★（2026-08-22・実データで素通りして分かった）
        #   ★直す前は「文字が含まれるか」で見ていた★ので、
        #   「最大7pt」を消しても「97.7%」の中の 7 に当たって通ってしまった。
        G = {"slug": "g", "sections": [
            {"title": "天井・恩恵",
             "body": ["ポイントは最大7ptです。",
                      "機械割は97.7%です。",
                      "天井は1000Gです。"]}]}
        with io.open(os.path.join(td, "g.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump(G, f, ensure_ascii=False, indent=1)
            f.write("\n")

        def dec_g(actions):
            r = os.path.join(td, "dg.json")
            io.open(r, "w", encoding="utf-8").write(json.dumps(
                {"schema_version": SCHEMA, "slug": "g",
                 "decided_by": ["Claude", "codex"], "actions": actions},
                ensure_ascii=False))
            return r

        rg = apply_decision(dec_g([{"op": "drop",
                                    "text": "ポイントは最大7ptです。",
                                    "why": "わざと"}]))
        t("★★消すと無くなる数値を、他の数値の一部で見逃さない★★"
          "（97.7%の中の7に当たっていた）",
          bool(rg["problems"]) and "7" in "".join(rg["problems"]))

        # ★★全部やったあとで数値が消えるのを見る★★
        #   1件ずつ見るだけだと、重複した2行を「両方」消す決定が通ってしまう。
        H = {"slug": "h", "sections": [
            {"title": "天井・恩恵",
             "body": ["リセットは700Gに短縮されます。",
                      "リセット時は700Gに短縮されます。",
                      "通常時の天井は1000Gです。"]}]}
        with io.open(os.path.join(td, "h.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump(H, f, ensure_ascii=False, indent=1)
            f.write("\n")

        def dec_h(actions):
            r = os.path.join(td, "dh.json")
            io.open(r, "w", encoding="utf-8").write(json.dumps(
                {"schema_version": SCHEMA, "slug": "h",
                 "decided_by": ["Claude", "codex"], "actions": actions},
                ensure_ascii=False))
            return r

        rh1 = apply_decision(dec_h([{"op": "drop",
                                     "text": "リセットは700Gに短縮されます。",
                                     "why": "重複の片方"}]))
        t("　重複の片方だけを消すのは通る", not rh1["problems"])

        rh2 = apply_decision(dec_h([
            {"op": "drop", "text": "リセットは700Gに短縮されます。",
             "why": "重複の片方"},
            {"op": "drop", "text": "リセット時は700Gに短縮されます。",
             "why": "もう片方も"}]))
        t("★★両方消して数値が記事から無くなる決定は受け取らない★★"
          "（1件ずつ見るだけでは両方とも通っていた）",
          bool(rh2["problems"]) and "700" in "".join(rh2["problems"]))

        r7 = apply_decision(dec([{"op": "drop", "text": "B の行です。",
                                  "why": "…"}]), apply_it=True)
        t("　通れば書ける", r7.get("wrote") is True)
        with io.open(p, encoding="utf-8") as f:
            t("　消したい行だけが消えている",
              json.load(f)["sections"][0]["body"]
              == ["A の行です。", "C の行です。"])
    finally:
        globals()["DETAILS"] = _keep

    print()
    print(f"{17 - len(ng)}/17 " + ("合格" if not ng else "不合格"))
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", nargs="?", choices=("gather", "apply"))
    ap.add_argument("--slug")
    ap.add_argument("--file")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    if a.command == "gather":
        if not a.slug:
            ap.error("--slug が要ります")
        g = gather(a.slug)
        if a.json:
            print(json.dumps(g, ensure_ascii=False, indent=1))
            return 0
        print(f"★{a.slug} の候補★ {len(g['candidates'])} 件  {g.get('counts')}")
        for c in g["candidates"]:
            print(f"  [{c['kind']}] {c.get('section') or ''}")
            for k in ("a", "b", "before", "after", "drop", "mixed", "word"):
                if c.get(k):
                    print(f"      {k}: {str(c[k])[:88]}")
        if not g["candidates"]:
            print("  （その場で決められる候補はありません）")
        return 0
    if a.command == "apply":
        if not a.file:
            ap.error("--file が要ります")
        try:
            r = apply_decision(a.file, a.apply)
        except ValueError as e:
            print(f"★{e}★")
            return 1
        if r["problems"]:
            print("★書きませんでした★")
            for x in r["problems"]:
                print("  ✗ " + str(x)[:140])
            return 3
        print(("★書きました★" if a.apply else "★見るだけ★")
              + f" {len(r['done'])} 件")
        for x in r["done"]:
            print(f"  {x['op']}: {x['why']}")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

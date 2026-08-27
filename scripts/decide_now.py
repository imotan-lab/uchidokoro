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

★★「記事内で決まらない」は、台帳へ回す理由にならない★★
  （2026-08-22・運営者の指摘「台帳に回すなって　その場で解決しろって」）

  ★私の間違い★＝この道具を「記事の中だけを見る」作りにしたので、
  記事だけで決まらないものが**その場で台帳へ落ちた**。
  ＝人の待ち行列を作らない、という目的そのものに反していた。

  ★正しい順番★＝**①機械 → ②2AI → ③メール**（CLAUDE.md・2026-08-12の決定）
    ②は1回ではない。★その晩のうちに3回まで・回ごとに材料を増やす★
      1回目 … 記事の中だけで詰める（この道具の gather）
      2回目 … ★出典の原文を取りに行く★（collect_evidence）
      3回目 … 検索して別系統の出典を足す
    決まったら値を控える（confirmed_values・★出典に無い値は機械が弾く★）
    ★3回やって決まらなかったものだけ★が人の出番（NOTIFY_HUMAN）。

  ★この道具の役どころ★＝**1回目**（記事の中だけで決まる分）。
  ここで決まらなかったものは、★台帳ではなく2回目へ送る★。
  送り先は `collect_evidence.py --slug X --topic …` と `confirmed_values.py`。
  進み具合は `repair_journal.py` が数える（`attempt()` が3回で ESCALATED）。

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
        # ★★節の外にも読者が読むものがある★★（2026-08-27・台帳#487）
        #   ★渡していなかったせいで起きたこと★＝
        #   2AIは「基本情報表と本文で用語が食い違う」と気づけず、
        #   気づいても直す口が無いので**永久に台帳へ落ちていた**。
        "factTable": [list(r) for r in (d.get("factTable") or [])
                      if isinstance(r, (list, tuple))],
        "summaryBoxes": [dict(b) for b in (d.get("summaryBoxes") or [])
                         if isinstance(b, dict)],
        "lead": d.get("lead") if isinstance(d.get("lead"), str) else "",
    }
    # ★★機種データも渡す★★（2026-08-22・実データで穴が出た）
    #   ★渡していなかったせいで起きたこと★＝
    #   goji_eva の「当サイトの狙い目」に何ゲームから狙うかが書いておらず、
    #   2AIは「外部の出典が要る」と結論して**台帳へ落とした**。
    #   ところが machines.json には
    #     strategy: 等価640G〜（状況不問） / リセット時360G〜
    #   と★サイト自身の狙い目が既に載っていた★。
    #   ＝外部など要らない、サイト内で閉じた食い違いだった。
    #   ★記事データだけを見せると、サイトが自分で持っている答えに気づけない★
    out["machine"] = {}
    try:
        rows = _sj.read_rows(os.path.join(BASE, "assets", "data",
                                          "machines.json"))
        m = next((x for x in rows if x.get("slug") == slug), None)
        if m:
            ck = m.get("checker") or {}
            out["machine"] = {
                "name": m.get("name"),
                "info": m.get("info"),
                # ★一覧・トップページに出ている、この機種の狙い目★
                "strategy": m.get("strategy"),
                "seo_title": (m.get("seo") or {}).get("title"),
                # ★チェッカーが既定で出す区切り★（読者が実際に押して見る数値）
                "checker_modes": [
                    {"key": md.get("key"), "label": md.get("label"),
                     "caution": md.get("caution"), "max": md.get("max")}
                    for md in (ck.get("modes") or []) if isinstance(md, dict)
                ],
            }
    except Exception as e:                                   # noqa: BLE001
        out.setdefault("problems", []).append(
            f"機種データを読めません: {type(e).__name__}")

    out["how_to_decide"] = (
        "★記事データと機種データの両方を読んでください★。"
        "『当サイトの狙い目』のような節は、機種データの strategy や "
        "チェッカーの区切りと食い違っていることがあります"
        "（★それは外部の出典を見なくても分かる食い違いです★）。"
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


# ★★節の外の直せる場所★★（2026-08-27・台帳#487）
#   ★言い換え（replace）だけ★＝行ごと消す・リード文を空にするは受け取らない。
#   節の本文と違い、同じ事実が他所に残っている保証が弱いので、
#   消すと「どちらが正解か」を選んだことになりやすい。
OUTSIDE_KINDS = ("fact", "summary", "lead")


def _outside_plan(d: dict, a: dict) -> tuple | None:
    """節の外（基本情報表・要約ボックス・リード文）で当たる場所を探す。

    返すもの: (種類, 場所1, 場所2, 決定) ／ 当たらなければ None
    ★言い換えのときだけ探す★
    """
    if a.get("op") != "replace":
        return None
    before = a.get("before")
    for ri, row in enumerate(d.get("factTable") or []):
        if not isinstance(row, (list, tuple)):
            continue
        for ci, cell in enumerate(row):
            if isinstance(cell, str) and cell == before:
                return ("fact", ri, ci, a)
    for bi, box in enumerate(d.get("summaryBoxes") or []):
        if not isinstance(box, dict):
            continue
        for key in ("value", "label"):
            if isinstance(box.get(key), str) and box[key] == before:
                return ("summary", bi, key, a)
    if isinstance(d.get("lead"), str) and d["lead"] == before:
        return ("lead", 0, 0, a)
    return None


def _write_outside(d: dict, kind: str, i1, i2, after: str) -> None:
    """節の外へ書き戻す（★探す側と書く側で同じ場所の指し方を使う★）。"""
    if kind == "fact":
        d["factTable"][i1][i2] = after
    elif kind == "summary":
        d["summaryBoxes"][i1][i2] = after
    elif kind == "lead":
        d["lead"] = after


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


def _machine_row(slug: str) -> dict:
    """この機種について machines.json が持っているもの（★読むだけ★）"""
    try:
        rows = _sj.read_rows(os.path.join(BASE, "assets", "data",
                                          "machines.json"))
        return next((x for x in rows if x.get("slug") == slug), {}) or {}
    except Exception:                                        # noqa: BLE001
        return {}


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
        elif kind in OUTSIDE_KINDS:
            # ★節の外も数え直しに入れる★（2026-08-27・台帳#487）
            #   ★入れないと★＝表の数値を書き換えても「消えた」と気づけない。
            _write_outside(d, kind, si, bi, a["after"])
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

    # ★サイトがこの機種について公開しているもの全部★（数値の出どころを照合する的）
    published = raw + "\n" + json.dumps(_machine_row(slug), ensure_ascii=False)

    for a in dec["actions"]:
        if a["op"] == "replace":
            nb, na = _numbers(a["before"]), _numbers(a["after"])
            if nb != na:
                # ★★数値が変わる言い換えは、出どころを言えたときだけ受け取る★★
                #   （2026-08-22・運営者の指摘「台帳に回すな　その場で解決しろ」）
                #
                #   ★これが無かったせいで起きたこと★＝
                #   goji_eva の「当サイトの狙い目」に何ゲームから狙うかが
                #   書かれておらず、2AIは直せずに**台帳へ落とした**。
                #   ところが machines.json には
                #     strategy: 等価640G〜（状況不問） / リセット時360G〜
                #   と★サイト自身の狙い目が既に載っていた★。
                #   ＝数値を作る話ではなく、サイトの中で閉じた食い違いだった。
                #
                #   ★受け取る条件★＝決定が `numbers_from` に**逐語**を書き、
                #     ①その逐語が、この機種についてサイトが公開しているもの
                #       （記事データ＋機種データ）に**そのまま**あること
                #     ②足す数値が、その逐語の中に全部あること
                #   ＝★機械は「どこから来た数値か」だけを確かめる★。
                #     どれを載せるかは2AIが決める（機械は判断しない）。
                src = a.get("numbers_from")
                if not src:
                    result["problems"].append(
                        f"数値が変わる言い換えには出どころが要ります"
                        f"（numbers_from に逐語で）: {a['before'][:34]!r}")
                    return result
                if src not in published:
                    result["problems"].append(
                        f"出どころの逐語が、この機種の公開データに見つかりません: "
                        f"{src[:44]!r}")
                    return result
                added = sorted(set(na) - set(nb))
                missing = [n for n in added if n not in _numbers(src)]
                if missing:
                    result["problems"].append(
                        "出どころに無い数値を足そうとしています: "
                        + " / ".join(missing[:4]))
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
            # ★節の外（基本情報表・要約ボックス・リード文）も見る★（台帳#487）
            got = _outside_plan(d, a)
            if got:
                plan.append(got)
                hit = True
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
    # ★消えてよい数値は、決定が1つずつ理由つきで名指ししたものだけ★
    #   （2026-08-22。goji_eva の「6.4割」＝640÷1000 の派生表現で、
    #    実際の天井は 1000G+α なので厳密な割合ではなかった。
    #    ★出典から取った事実ではなく、当サイトが計算した値★なので消せる。
    #    ★機械にはその区別が付かない★ので、2AIに名指しさせて記録に残す）
    ok_to_lose = {}
    for item in dec.get("numbers_removed") or []:
        if isinstance(item, dict) and item.get("n") and item.get("why"):
            ok_to_lose[str(item["n"])] = item["why"]
    still = [n for n in lost if n not in ok_to_lose]
    if still:
        result["problems"].append(
            "全部やると記事から無くなる数値があります: " + " / ".join(still[:6])
            + "（重複を1つにするだけなら消えないはずです。"
            "消してよいなら numbers_removed に理由つきで名指ししてください）")
        return result
    for n, why in ok_to_lose.items():
        result.setdefault("removed_numbers", []).append({"n": n, "why": why})

    for kind, si, bi, a in plan:
        result["done"].append({"op": a["op"], "why": a["why"][:60]})

    if apply_it:
        for kind, si, bi, a in plan:
            if kind in OUTSIDE_KINDS:
                # ★節の外は sections を触らない★（場所の指し方が違う）
                _write_outside(d, kind, si, bi, a["after"])
                continue
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

    ran = [0]

    def t(name, cond):
        # ★試した数を数える★（2026-08-27）
        #   ★直す前は分母が手書きの「22」だった★ので、
        #   試験を足しても分母が増えず、★足した分が数えられなかった★
        #   （監査51が見張っている型そのもの）。
        ran[0] += 1
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

        # ★★数値が変わる言い換えは、出どころを言えたときだけ通す★★
        #   （2026-08-22・運営者の指摘「台帳に回すな　その場で解決しろ」）
        #   ★これが無いと、サイト自身が既に公開している値でさえ書けず、
        #     決められないものが台帳（人の待ち行列）へ落ちた★
        K = {"slug": "k", "sections": [
            {"title": "当サイトの狙い目",
             "body": ["天井1000Gに対して6.4割のラインです。",
                      "スルーが多い台はさらに期待できます。"]}]}
        with io.open(os.path.join(td, "k.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump(K, f, ensure_ascii=False, indent=1)
            f.write("\n")

        def dec_k(act, removed=None):
            r = os.path.join(td, "dk.json")
            body = {"schema_version": SCHEMA, "slug": "k",
                    "decided_by": ["Claude", "codex"], "actions": [act]}
            if removed:
                body["numbers_removed"] = removed
            io.open(r, "w", encoding="utf-8").write(
                json.dumps(body, ensure_ascii=False))
            return r

        base_act = {"op": "replace",
                    "before": "天井1000Gに対して6.4割のラインです。",
                    "after": "当サイトの狙い目は640G〜です。",
                    "why": "…"}

        rk1 = apply_decision(dec_k(dict(base_act)))
        t("★★出どころを言わない数値の変更は受け取らない★★",
          bool(rk1["problems"]) and "出どころ" in "".join(rk1["problems"]))

        rk2 = apply_decision(dec_k(
            dict(base_act, numbers_from="サイトのどこにも無い逐語")))
        t("★★出どころの逐語がサイトに無ければ受け取らない★★",
          bool(rk2["problems"]) and "見つかりません" in "".join(rk2["problems"]))

        # ★出どころは、この機種の公開データに実在する逐語でなければならない★
        #   ここでは記事の中の別の行を出どころにする
        K2 = {"slug": "k2", "sections": [
            {"title": "当サイトの狙い目",
             "body": ["天井1000Gに対して6.4割のラインです。",
                      "スルーが多い台はさらに期待できます。"]},
            {"title": "狙い目の根拠",
             "body": ["等価狙い目は640G〜です。", "根拠はこうです。"]}]}
        with io.open(os.path.join(td, "k2.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump(K2, f, ensure_ascii=False, indent=1)
            f.write("\n")

        def dec_k2(act, removed=None):
            r = os.path.join(td, "dk2.json")
            body = {"schema_version": SCHEMA, "slug": "k2",
                    "decided_by": ["Claude", "codex"], "actions": [act]}
            if removed:
                body["numbers_removed"] = removed
            io.open(r, "w", encoding="utf-8").write(
                json.dumps(body, ensure_ascii=False))
            return r

        rk3 = apply_decision(dec_k2(
            dict(base_act, after="当サイトの狙い目は555G〜です。",
                 numbers_from="等価狙い目は640G〜です。"),
            removed=[{"n": "6.4", "why": "派生値"},
                     {"n": "1000", "why": "他にある"}]))
        t("★★出どころに無い数値は足せない★★（でっち上げを止める）",
          bool(rk3["problems"]) and "555" in "".join(rk3["problems"]))

        rk4 = apply_decision(dec_k2(
            dict(base_act, numbers_from="等価狙い目は640G〜です。")))
        t("★★消える数値を名指ししていなければ受け取らない★★",
          bool(rk4["problems"]) and "6.4" in "".join(rk4["problems"]))

        rk5 = apply_decision(dec_k2(
            dict(base_act, numbers_from="等価狙い目は640G〜です。"),
            removed=[{"n": "6.4", "why": "640÷1000の派生表現で独立した情報がない"},
                     {"n": "1000", "why": "わざと：ここでは消えないが名指ししても害はない"}]))
        t("　出どころが実在し、消える数値を理由つきで名指しすれば通る",
          not rk5["problems"])

        # ── 2026-08-27・台帳#487 節の外（表・要約・リード文）────────
        #   ★実例（真打 吉宗）★＝基本情報表が「スルーカウントリセット」、
        #   本文が「周期カウントがリセット」。この機種にスルー天井は無い。
        #   ★直す前は道具に口が無く、何回やり直しても直らなかった★
        M = {"slug": "m",
             "lead": "この機種は2026年4月6日導入。解析は順次更新予定。",
             "factTable": [["朝一リセット", "スルーカウントリセット"],
                           ["CZ間天井", "1000G+α（6周期）"],
                           ["機械割（設定6）", "114.0%"]],
             "summaryBoxes": [{"label": "狙い目", "value": "等価400G〜"}],
             "sections": [
                 {"title": "天井・恩恵",
                  "body": ["朝一は周期カウントがリセットされます。",
                           "CZ間天井は1000G+α（6周期）です。"]},
                 {"title": "当サイトの狙い目",
                  "body": ["等価400G〜が狙い目です。"]}]}
        with io.open(os.path.join(td, "m.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump(M, f, ensure_ascii=False, indent=1)
            f.write("\n")

        def dec_m(actions, removed=None):
            r = os.path.join(td, "dm.json")
            body = {"schema_version": SCHEMA, "slug": "m",
                    "decided_by": ["Claude", "codex"], "actions": actions}
            if removed:
                body["numbers_removed"] = removed
            io.open(r, "w", encoding="utf-8").write(
                json.dumps(body, ensure_ascii=False))
            return r

        rm1 = apply_decision(dec_m([
            {"op": "replace", "before": "スルーカウントリセット",
             "after": "周期カウントリセット",
             "why": "この機種にスルー天井は無い（本文と揃える）"}]),
            apply_it=True)
        with io.open(os.path.join(td, "m.json"), encoding="utf-8") as f:
            _m1 = json.load(f)
        t("★★★基本情報表の食い違いを直せる★★★"
          "／★これが無くて台帳#276が永久に閉じられなかった★",
          not rm1["problems"] and _m1["factTable"][0][1] == "周期カウントリセット")
        t("　直したい欄だけが変わる（隣の行は元のまま）",
          _m1["factTable"][1][1] == "1000G+α（6周期）"
          and _m1["factTable"][0][0] == "朝一リセット")

        rm2 = apply_decision(dec_m([
            {"op": "replace", "before": "等価400G〜",
             "after": "等価400G〜（CZ間）",
             "why": "本文と同じ言い方にそろえる"}]), apply_it=True)
        with io.open(os.path.join(td, "m.json"), encoding="utf-8") as f:
            _m2 = json.load(f)
        t("★要約ボックスも直せる★",
          not rm2["problems"]
          and _m2["summaryBoxes"][0]["value"] == "等価400G〜（CZ間）")

        rm3 = apply_decision(dec_m([
            {"op": "replace",
             "before": "この機種は2026年4月6日導入。解析は順次更新予定。",
             "after": "この機種は2026年4月6日に登場したスマスロAT機です。",
             "why": "時間で嘘になる文（順次更新予定）を落とす"}]),
            apply_it=True)
        with io.open(os.path.join(td, "m.json"), encoding="utf-8") as f:
            _m3 = json.load(f)
        t("★リード文も直せる★（時間で嘘になる文を落とせる）",
          not rm3["problems"] and "順次更新予定" not in _m3["lead"])

        # ★★節の外でも、全部やったあとの数え直しが効く★★
        #   ★★1件ずつの検査を通る形にしてある★★（2026-08-27・罠④）
        #   ★直す前は数値が丸ごと消える書き換えにしていた★ので、
        #   「出どころが要ります」の検査が**先に**断っており、
        #   数え直しは一度も動いていなかった（壊しても試験が緑だった）。
        #   ここでは出どころを言い、足す数値（400）はその逐語にある。
        #   ＝1件ずつの検査は通る。★消えるのは 114（表にしか無い）★。
        rm4 = apply_decision(dec_m([
            {"op": "replace", "before": "114.0%",
             "after": "等価400G〜", "why": "わざと：表にしか無い数値が消える",
             "numbers_from": "等価400G〜が狙い目です。"}]))
        t("★★節の外でも、数え直しが効く（表にしか無い数値が消える）★★"
          "／★数え直しに入れ忘れると素通りする★",
          bool(rm4["problems"]) and "114" in "".join(rm4["problems"]))

        rm5 = apply_decision(dec_m([
            {"op": "drop", "text": "朝一リセット", "why": "わざと：行ごと消す"}]))
        t("　表の行ごと消す決定は受け取らない（言い換えだけ）",
          bool(rm5["problems"]))

        rm6 = apply_decision(dec_m([
            {"op": "replace", "before": "どこにも無い文字列",
             "after": "x", "why": "わざと"}]))
        t("　節の外にも無ければ、いままでどおり断る", bool(rm6["problems"]))

        # ★★2AIに見せていないものは直せない★★（2026-08-27・台帳#487）
        #   ★口を足しただけでは足りない★＝渡していなければ、
        #   2AIはそこに食い違いがあることに気づけない。
        _gm = gather("m")["article"]
        t("★2AIに表・要約・リード文を見せる（見せないものは直せない）★",
          bool(_gm.get("factTable")) and bool(_gm.get("summaryBoxes"))
          and bool(_gm.get("lead")))

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
    print(f"{ran[0] - len(ng)}/{ran[0]} " + ("合格" if not ng else "不合格"))
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

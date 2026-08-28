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


_SLUG_OK = None


def _check_slug(slug: str) -> str:
    """★置き場の外を指せないようにする★（2026-08-27・Codexの指摘18）

    ★直す前は検査が無かった★ので、slug に `../` や絶対パスを書けば
    記事データの置き場の外のJSONを書き換えられた（実際に再現した）。
    ★機種のslugは英小文字・数字・下線だけ★なので、それ以外は断る。
    """
    global _SLUG_OK
    if _SLUG_OK is None:
        import re as _re
        # ★末尾の改行まで通さない★（`$` は改行の手前にも当たる）
        _SLUG_OK = _re.compile(r"[a-z0-9_]+\Z")
    s = str(slug or "")
    if not _SLUG_OK.fullmatch(s):
        raise ValueError(f"機種の名前として使えない文字が入っています: {s[:40]!r}")
    return s


def _detail(slug: str):
    slug = _check_slug(slug)
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
    # ★★条件・範囲・打ち消しの語がある行を教える★★（2026-08-27）
    #   ★止める判断には使わない★＝2AIが「意味が変わっていないか」を
    #   見るときの**手がかり**。名簿で機械が判定するのはやめた。
    for sec in out["article"]["sections"]:
        for line in sec.get("body") or []:
            hits = [w for w in HINT_MARKS if w in line]
            if hits:
                out["hints"].append(
                    {"kind": "意味が変わりやすい語", "section": sec["title"],
                     "words": hits[:5], "line": line[:80],
                     "note": "この行を書き換えるときは、意味が変わっていないか"
                             "2AIで確かめて meaning_why に理由を書く"})
    out["counts"]["_note"] = "★この数は機械が気づけた分だけ★（網羅ではない）"
    return out


# ★★節の外の直せる場所★★（2026-08-27・台帳#487）
#   ★言い換え（replace）だけ★＝行ごと消す・リード文を空にするは受け取らない。
#   節の本文と違い、同じ事実が他所に残っている保証が弱いので、
#   消すと「どちらが正解か」を選んだことになりやすい。
OUTSIDE_KINDS = ("fact", "summary", "lead")


def _slot_key(a: str) -> str:
    """係り先（★空白を詰めただけ★＝そのままの文字で比べる）。

    ★内容の文字だけにしてはいけない★（2026-08-28・Codexの8回目の指摘1）
      `_words` は漢字・カタカナ・英字しか拾わないので、
      ★ひらがなも数字も落ちる★。
      そのため「設定変更あり」と「設定変更なし」が同じ鍵になり、
      ★対応の入れ替えが meaning_why なしで通った★（自分で再現した）。

    ★飾りを落とす必要はもう無い★＝
      出どころ（numbers_from）は**書き換え後と同じ言い方の逐語**を
      選ぶ取り決めにしたので、係り先はそのまま一致するはず。
      一致しなければ★断る★（安全側）。
    """
    return "".join(str(a or "").split())


def _slot_ok(p, src_pairs) -> bool:
    """出どころの中に、★係り先が丸ごと同じで数値も同じ★ものがあるか。

    ★ゆるい照合はやめた★（2026-08-27・Codexの7回目の指摘1）＝
    「1つに定まれば正しい」にしていたので、
    ★唯一の候補が間違っていても通った★
    （「通常時の天井は500G」← 出どころは「リセット時の天井は500G」）。
    ★条件を落として一般化する形★（「天井は500G」← 「リセット時の天井は500G」）
    も通っていた。

    ★機械が決められるのは「丸ごと同じ」だけ★＝
    それ以外は2AIが理由を書く（設計どおり）。
    ★出どころは、書き換え後と同じ言い方の逐語を選ぶこと★
    """
    key = _slot_key(p[0])
    return any(_slot_key(q[0]) == key and q[1] == p[1] for q in src_pairs)


def drop_spot(d: dict, text: str, used=None):
    """★消す先を決める唯一の場所★（返すのは (節の番号, 行の番号) か None）

    ★書き込みの計画と、許可の判定が、同じ場所を見るために切り出した★
      （2026-08-28・Codexの8回目の指摘2）
      ★直す前は、許可は「いちばん多い入れ物」を見て、
        書き込みは「最初に当たった節」を書いていた★＝
      節Bに2件あれば、節Aの1件を消せた（自分で再現した）。

    ★`used` にもう決めた行を渡すと、その行は飛ばす★
      （2026-08-28・Codexの9回目の指摘2）
      ★毎回もとの記事から探すと、同じ「消す」を2件並べたときに
        両方が同じ行を指し、2件やったと報告して1件しか消さない★。
    """
    seen = used or set()
    for si, sec in enumerate(d.get("sections") or []):
        if not isinstance(sec, dict):
            continue
        for bi, line in enumerate(sec.get("body") or []):
            if isinstance(line, str) and line == text and (si, bi) not in seen:
                return si, bi
    return None


def _dup_count(d: dict, text: str) -> int:
    """★これから消す節の中に、丸ごと同じものがいくつあるか★

    ★他の入れ物の重複を根拠にしない★（2026-08-28・Codexの8回目の指摘2）
      消すのは `drop_spot` が決めた1行なので、
      数えるのも**その行が入っている節の本文だけ**。
    """
    spot = drop_spot(d, text)
    if spot is None:
        return 0
    si, _bi = spot
    body = ((d.get("sections") or [])[si] or {}).get("body") or []
    return sum(1 for x in body if x == text)


def _where_hits(d: dict, before) -> list:
    """その文字が記事のどこに何か所あるかを数える（2026-08-27・指摘4）。"""
    # ★★「何か所あるか」を数える★★（2026-08-27・Codexの2回目の指摘2）
    #   ★直す前は「本文・表…という種類」を数えていた★ので、
    #   ★同じ本文に同じ行が2つあっても1と数えて素通りした★
    #   ＝先頭が書き換わり、どちらを直したのか決められない。
    got = []
    for sec in (d.get("sections") or []):
        if not isinstance(sec, dict):
            continue
        got += ["本文" for x in (sec.get("body") or []) if x == before]
        got += ["表の注記" for t in (sec.get("tables") or [])
                if isinstance(t, dict) and t.get("note") == before]
    for row in (d.get("factTable") or []):
        if isinstance(row, (list, tuple)):
            got += ["基本情報表" for c in row if c == before]
    for box in (d.get("summaryBoxes") or []):
        if isinstance(box, dict):
            got += ["要約ボックス" for k in ("value", "label")
                    if box.get(k) == before]
    if d.get("lead") == before:
        got.append("リード文")
    return got


def _outside_plan(d: dict, a: dict, where: str = "") -> tuple | None:
    """節の外（基本情報表・要約ボックス・リード文）で当たる場所を探す。

    返すもの: (種類, 場所1, 場所2, 決定) ／ 当たらなければ None
    ★言い換えのときだけ探す★
    """
    if a.get("op") != "replace":
        return None
    before = a.get("before")
    if where and where not in ("fact", "summary", "lead"):
        return None
    for ri, row in enumerate(d.get("factTable") or []):
        if where and where != "fact":
            break
        if not isinstance(row, (list, tuple)):
            continue
        for ci, cell in enumerate(row):
            if isinstance(cell, str) and cell == before:
                return ("fact", ri, ci, a)
    for bi, box in enumerate(d.get("summaryBoxes") or []):
        if where and where != "summary":
            break
        if not isinstance(box, dict):
            continue
        for key in ("value", "label"):
            if isinstance(box.get(key), str) and box[key] == before:
                return ("summary", bi, key, a)
    if (not where or where == "lead") \
            and isinstance(d.get("lead"), str) and d["lead"] == before:
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
    # ★★判断者は「違う名前が2つ以上」★★（2026-08-27・Codexの3回目）
    #   ★直す前は件数だけ見ていた★ので、["Claude","Claude"] でも
    #   「2AIで決めた」ことになった＝ひとりで決めた結論が通る。
    if not isinstance(by, list) or len({str(x).strip().lower()
                                        for x in by if str(x).strip()}) < 2:
        raise ValueError(
            "decided_by に**違う**判断者が2つ以上要ります（2AIで決めるため）: "
            + str(by)[:40])
    if not d.get("slug"):
        raise ValueError("slug がありません")
    # ★★記事の指紋は必ず要る★★（2026-08-27・Codexの3回目の指摘4）
    #   ★直す前は「書いてあれば照合する」だった★ので、
    #   書かなければ「いつの記事に対する判断か」の確認を丸ごと外せた。
    _s = str(d.get("source_sha256") or "")
    if len(_s) != 64 or any(c not in "0123456789abcdef" for c in _s.lower()):
        raise ValueError(
            "判断したときの記事の指紋（source_sha256・64桁）が要ります")
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
    """★数値は「すぐ後ろの1文字」ごと見る★（2026-08-27・Codexの指摘3）

    ★直す前は裸の数字だけ見ていた★ので、
    「天井は500Gです」を消しても、無関係な「獲得は500枚」が残っていれば
    ★同じ数値が記事に残っている★と判定して通していた。
    ＝読者から天井の値が消える。

    ★単位の名簿は作らない★（例外リストの型になる）。
    すぐ後ろの1文字を付けるだけで G と 枚 は別物になる。
    """
    import re
    out = []
    for m in re.finditer(r"\d+(?:\.\d+)?", str(s or "")):
        tail = str(s or "")[m.end():m.end() + 1]
        # ★区切りや文の終わりは単位ではない★（付けると別物になってしまう）
        if tail in ("", " ", "\u3000", "、", "。", "／", "/", "・", "）", ")",
                    "」", "\n", "\t", "，", ","):
            out.append(m.group(0))
        else:
            out.append(m.group(0) + tail)
    return out


# ★★意味をひっくり返す印★★（2026-08-27・Codexの2回目の指摘1）
#   ★内容語だけ見ていた★ので、ひらがなだけで意味を反転できた＝
#     「天井は500Gです。」→「天井は500Gではありません。」が通っていた
#     （新しい内容語なし・数値も同じなので、どの検査にも当たらない）。
#   ★これは名簿だが、意味の判断ではない★＝
#     機械が「正しいか」を決めるのではなく、
#     ★打ち消し・大小の印が**新しく現れたら断る**★だけ。
#   ★断ってよい理由★＝打ち消しや大小を入れ替えるのは
#     **事実を変える**ことであって、言い換えではない。
#     事実を変えるには出典が要る（この道具の役目ではない）。
# ★★これは「手がかり」であって、判定の道具ではない★★
#   （2026-08-27・運営者から「機械的に無理なことは2AIで判断しろ」）
#   ★止める判断には使わない★＝gather が2AIに
#   「この行には条件・範囲・打ち消しの語があります」と**教える**だけ。
#   ★名簿で意味を判定しようとしたのが間違いだった★
#   （名簿は無限に増えるし、機械には意味が分からない）。
HINT_MARKS = (
    # 打ち消し
    "ない", "なく", "ません", "ませ", "無", "不可", "非",
    # 大小・範囲（★enen2 の「以内」→「以降」が実際に通っていた★）
    "以上", "以下", "未満", "超", "以内", "以降", "以前",
    # 論理（★bandori の「かつ」→「または」が実際に通っていた★）
    "かつ", "または", "もしくは", "および", "ならびに")


def _flips(s: str) -> list:
    """その文に出てくる「意味をひっくり返す印」。"""
    return [w for w in HINT_MARKS if w in str(s or "")]


_SHAPE_RE = None


def _shape(s: str) -> list:
    """★文の骨組み★（2026-08-27・Codexの3回目）

    「数値」と「意味をひっくり返す印」を**出てくる順に**並べ、
    それぞれに**直前の内容語**を添えたもの。
    ★丸ごと同じでなければ断る★＝増えた・減った・入れ替わった、を全部拾う。

    ★符号も見る★＝「+500枚」と「-500枚」は別物
      （数値だけ見ていると同じに見えてしまう）。
    """
    # ★★意味の語は入れない★★（2026-08-27・運営者から）
    #   ★名簿で「意味が変わったか」を機械に決めさせない★＝
    #   名簿は無限に増えるし、機械には意味が分からない。
    #   ここで見るのは★機械に分かること＝数値がどの言葉に付いているか★だけ。
    #   （「通常500G」→「リセット500G」は構造の変化なので分かる）
    #   意味の判断は2AIがして、理由を記録に残す（下の `meaning_why`）。
    global _SHAPE_RE
    if _SHAPE_RE is None:
        import re as _re3
        _SHAPE_RE = _re3.compile(r"[-−▲△+＋]?\d+(?:\.\d+)?")
    txt = str(s or "")
    out = []
    _prev = 0
    for m in _SHAPE_RE.finditer(txt):
        # ★★係り先は「前の数値からこの数値まで」★★
        #   （2026-08-27・Codexの6回目の指摘1）
        #   ★直す前は直前の1語だけ★だったので、
        #   「通常時の天井は300G／リセット時の天井は400G」は
        #   どちらも『天井』になり、逆の対応で書けた。
        ws = [txt[_prev:m.start()].strip()]
        tail = txt[m.end():m.end() + 1]
        # ★数値には単位（すぐ後ろの1文字）も添える★
        tok = m.group(0)
        if tok[-1].isdigit() and tail and not tail.isspace() \
                and tail not in ("、", "。", "／", "/", "・", "）", ")", "」",
                                 "，", ","):
            tok += tail
        # ★次の係り先に、この数値の単位を持ち込まない★（2026-08-27）
        #   ★直す前は「G／リセット」のように単位が頭に付いた★ので、
        #   入れ替えの見分けが狂った。
        _prev = m.end() + (len(tok) - len(m.group(0)))
        out.append((ws[-1] if ws else "", tok))
    return out


def _wording(s: str) -> str:
    """★言い回し★＝数値を伏せた見た目（2026-08-27）。

    これが変われば「言葉づかいが変わった」＝★意味が変わりうる★。
    ★機械はここまで★＝変わったことは分かるが、
    意味が変わったかは分からない。だから2AIに答えさせる。
    """
    # ★符号は伏せない★（2026-08-27・Codexの5回目）
    #   ★直す前は符号ごと伏せていた★ので、
    #   「差枚+500枚」→「差枚-600枚」が同じ見た目になり素通りした。
    import re as _re4
    return _re4.sub(r"\d+(?:\.\d+)?", "#", str(s or ""))


def _num_pairs(s: str) -> list:
    """★数値と、その直前の内容語の組★（2026-08-27・Codexの2回目の指摘1）

    ★なぜ要るか★＝「通常500G／リセット600G」→「リセット500G／通常600G」は
      **数値の並びが同じ**なので、並べ替えの検査にも当たらなかった。
      ＝★どちらがどちらの値かを丸ごと取り違えさせられる★。
    ★数値が2つ以上あるときだけ見る★＝1つしかない文で
      「天井は500Gです」→「500Gが天井です」まで止めると、
      正しい言い換えを妨げる。
    """
    import re as _re2
    out = []
    for m in _re2.finditer(r"\d+(?:\.\d+)?", str(s or "")):
        head = str(s or "")[:m.start()]
        ws = _words(head)
        out.append((ws[-1] if ws else "", m.group(0)))
    return out


from collections import Counter as _Counter    # noqa: E402


_WORD_RE = None


def _words(s: str) -> list:
    """★内容語★＝漢字・カタカナ・ラテン英字のかたまり。

    ★なぜ要るか（2026-08-27・Codexの指摘1）★
      ★直す前は数値しか見ていなかった★ので、数値が同じなら
      **意味が反対の言い換え**（「500Gです」→「500G以下です」）や、
      **記事に無い事実**（「スマスロAT機」）を書き足せた。
      ＝2AIが誤っても、機械はひとつも止められなかった。
      （★私が今日書いた試験そのものが、その穴を実演していた★）

    ★ひらがなは見ない★＝送り仮名・助詞なので、
    言い換え（「となる」→「となります」）を止めてしまう。
    """
    global _WORD_RE
    if _WORD_RE is None:
        import re as _re
        _WORD_RE = _re.compile(
            r"[\u4e00-\u9fff々〆ヵヶ]+|[\u30a0-\u30ff]+|[A-Za-z]+")
    return _WORD_RE.findall(str(s or ""))


def _content_blob(s: str) -> str:
    """★助詞・送り仮名を抜いた並び★（2026-08-27）

    ★なぜ必要か★＝語のかたまりで比べると、
    「カウントがリセット」と「カウントリセット」が別物になり、
    ★正しい言い換えを止めてしまう★（実際に踏んだ）。
    助詞を抜いた並びの中に入っているかで見れば、
    組み替えは通り、新しい語は止まる。
    """
    return "".join(_words(s))


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


def _agreement_problem(slug: str, dec: dict):
    """合意が生きているなら、その合意どおりかを見る（2026-08-27・指摘6）。

    返すもの: 問題があれば説明の文字列 ／ 無ければ None
    ★記録が読めないときは止める★（fail-closed）＝
      読めないことを理由に、合意を素通りさせない。
    """
    # ★★読めないときは止める★★（2026-08-27・Codexの2回目の指摘4）
    #   ★直す前は「読めないなら見ない」＝通していた★
    #   ＝記録の仕組みを壊せば、合意の検査を丸ごと外せた。
    try:
        import repair_journal as _rj
    except Exception as e:                 # noqa: BLE001
        return f"直しの記録の仕組みを読み込めません（{str(e)[:60]}）"
    try:
        rows = _rj.listing()
    except Exception as e:                 # noqa: BLE001
        return f"直しの記録を読めません（{str(e)[:60]}）"
    # ★★壊れた記録が1件でもあれば止める★★（同・指摘4）
    #   壊れた記録は機種が分からないので、
    #   ★この機種の合意が無い、と言い切れない★（安全側へ倒す）。
    _bk = [r for r in rows if r.get("state") == "BROKEN"]
    if _bk:
        return ("直しの記録に読めないものがあります"
                f"（{len(_bk)}件・例: {_bk[0].get('finding_id')}）。"
                "どの機種の合意か分からないので書きません")
    live = [r for r in rows
            if r.get("state") == "AGREED" and str(r.get("slug") or "") == slug]
    if not live:
        return None                        # ★合意が無いなら今までどおり★
    fid = str(dec.get("finding_id") or "")
    if not fid:
        return ("この機種には合意が生きています。"
                "決定ファイルに finding_id を書いてください"
                f"（{live[0].get('finding_id')}）")
    hit = [r for r in live if str(r.get("finding_id")) == fid]
    if not hit:
        return f"その件は、この機種の生きている合意ではありません（{fid}）"
    # ★★決定ファイル全体の指紋で比べる★★（2026-08-27・Codexの3回目の指摘3）
    #   ★直す前は actions だけだった★ので、合意のあとで
    #   `numbers_removed` を**追記**すれば、本来止まる削除を免除できた
    #   （actions は変わっていないので指紋は一致したまま）。
    want = str(hit[0].get("ops_sha256") or "")
    got = _rj.decision_digest(dec)
    if not want:
        return "合意に操作の指紋がありません（古い記録です。取り直してください）"
    if want != got:
        return ("合意した操作と、当てようとしている操作が違います"
                f"（{want[:12]}… → {got[:12]}…）")
    return None


def apply_decision(path: str, apply_it: bool = False) -> dict:
    """2AIが決めたとおりに直す（★消す・言い換えるだけ★）。"""
    dec = _load_decision(path)
    try:
        slug = _check_slug(dec["slug"])
    except ValueError as e:
        return {"slug": str(dec.get("slug"))[:40], "problems": [str(e)]}
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
    # ★空はもう来ない（_load_decision が断る）が、念のため不一致として扱う★
    if want != got:
        result["problems"].append(
            f"判断したときから記事が変わっています（{want[:12]}… → {got[:12]}…）")
        return result

    # ★★合意が生きている機種は、その合意どおりにしか書けない★★
    #   （2026-08-27・Codexの指摘6）
    #   ★直す前は、合意した中身と当てる中身を結ぶものが無かった★ので、
    #   無害な合意を取っておいて、同じ機種へ**まったく別の書き換え**を
    #   当てられた（途中の関所は「合意済みか」しか見ない）。
    #   ★合意が無い機種は今までどおり★（新台の経路などは変わらない）。
    _b = _agreement_problem(slug, dec)
    if _b:
        result["problems"].append(_b)
        return result

    # ★サイトがこの機種について公開しているもの全部★（数値の出どころを照合する的）
    published = raw + "\n" + json.dumps(_machine_row(slug), ensure_ascii=False)

    _dup_ok = []          # ★機械が「重複」と認めて通した消し方★
    for a in dec["actions"]:
        if a["op"] == "replace":
            nb, na = _numbers(a["before"]), _numbers(a["after"])
            # ★数値の並べ替えは、下の「骨組み」の検査が拾う★（2026-08-27）
            #   ★同じことを2か所で見ない★＝骨組みは順番つきなので、
            #   並べ替えは必ず食い違う。二重にすると、片方を壊しても
            #   もう片方が拾って試験が緑になる（罠③）。
            # ★★記事に無い語を持ち込ませない★★（2026-08-27・Codexの指摘1）
            #   ★直す前は数値しか見ていなかった★ので、
            #   「500Gです」→「500G以下です」（意味が反対）や、
            #   記事に無い「スマスロAT機」を書き足せた。
            #   ★物差しは数値と同じ★＝この機種についてサイトが公開している
            #   ものの中に、その語があること。無ければ出どころを言うこと。
            #   ★ひらがなは見ない★（送り仮名・助詞なので言い換えを妨げる）。
            # ★★骨組みが変わる言い換えは受け取らない★★
            #   （2026-08-27・Codexの3回目の指摘1・2）
            #   ★直す前は「印が**増えたら**断る」だった★ので、
            #     ・打ち消しを**消す**反転（「〜ではありません」→「〜です」）
            #     ・大小を**入れ替える**反転（以上⇄以下・顔ぶれは同じ）
            #     ・数値が1つのときのラベル差し替え（通常500G→リセット500G）
            #   が全部通っていた。★穴を1つずつ塞ぐ形では終わらない★。
            #   → 骨組み（数値・打ち消し・大小を、直前の言葉つきで順に並べたもの）
            #     が**丸ごと同じ**であることを求める。
            #   ★数値そのものが変わる言い換えには当てない★
            #     （そちらは「出どころの逐語」で見る＝骨組みは当然変わる）。
            # ★★係り先は、数値が変わるときも必ず見る★★
            #   （2026-08-27・Codexの5回目の指摘1）
            #   ★直す前は「数値の顔ぶれが同じとき」しか見ていなかった★ので、
            #   出どころを付けて数値ごと変えれば、
            #   「通常800G／リセット700G」のように**逆の対応**で書けた。
            _sb, _sa = _shape(a["before"]), _shape(a["after"])
            if sorted(nb) == sorted(na) and _sb != _sa:
                # ★★係り先の顔ぶれが同じなのに付き方が違う＝入れ替え★★
                #   （2026-08-27）これは機械に分かるので機械が断る。
                #   ★言い回しが変わっただけなら2AIへ回す★
                #   （丸ごと同じでないと断ると、正しい言い換えまで止まる）。
                # ★★入れ替え＝係り先の顔ぶれが同じで、
                #   どこかの係り先の**数値が変わった**とき★★
                #   （2026-08-27）★割り当てが変わっていなければ、
                #   それは言い回しの変化なので2AIへ回す★
                #   （「かつ→または」を入れ替えと誤判定していた）。
                # ★★数値の出てくる順番が変わったら、それは入れ替え★★
                #   （2026-08-28・Codexの9回目の指摘1）
                #   ★係り先を見ないので、係り先が空でも効く★＝
                #   `_shape` はラベルの中の数字（設定**1**）も値として読むので、
                #   「設定1は320G／設定2は314G」では
                #   ★本当の値の係り先が空文字★になり、
                #   組の集合が同じになって、どの検査にも当たらなかった。
                #   （自分で再現した＝そのまま通った）
                #   ★手順書の約束をそのまま機械にやらせる★＝
                #   「数値の並びを入れ替える言い換えは受け取らない」。
                if [n for _w, n in _sb] != [n for _w, n in _sa]:
                    result["problems"].append(
                        "数値の並びが入れ替わっています"
                        f"（{[n for _w, n in _sb][:4]}"
                        f" → {[n for _w, n in _sa][:4]}）"
                        "（どちらがどちらの値かを取り違えさせるので"
                        "受け取りません）")
                    return result
                _kb = [("".join(_words(w)), n) for w, n in _sb]
                _ka = [("".join(_words(w)), n) for w, n in _sa]
                if sorted(w for w, _ in _kb) == sorted(
                        w for w, _ in _ka) and sorted(_kb) != sorted(_ka):
                    result["problems"].append(
                        "数値の付き方が入れ替わっています"
                        f"（{_sb[:3]} → {_sa[:3]}）"
                        "（どちらがどちらの値かを取り違えさせるので"
                        "受け取りません）")
                    return result
            # ★数値が変わるときは、機械では正しさを決められない★
            #   （どの数値がどこに入るかは、出どころとの照合＝下の検査で見る。
            #     組み立てが変わること自体は2AIが判断して理由を残す）
            _blob = _content_blob(published)
            new_w = [w for w in _words(a["after"]) if w not in _blob]
            if new_w:
                # ★★出どころは、実在する逐語でなければならない★★
                #   （2026-08-27・Codexの2回目の指摘1）
                #   ★直す前は中身を見ていなかった★ので、
                #   numbers_from に**架空の逐語**を書けば新語の検査を抜けられた。
                _src_w = str(a.get("numbers_from") or "")
                if _src_w and _src_w not in published:
                    result["problems"].append(
                        "出どころの逐語が、この機種の公開データに"
                        f"見つかりません: {_src_w[:44]!r}")
                    return result
                _bsrc = _content_blob(_src_w)
                still_w = [w for w in new_w if w not in _bsrc]
                if still_w:
                    result["problems"].append(
                        "記事に無い言葉を書き足そうとしています: "
                        + " / ".join(sorted(set(still_w))[:5])
                        + "（言い換えは、この機種について公開しているものの"
                        "中で閉じている必要があります。"
                        "出どころがあるなら numbers_from に逐語で）")
                    return result
            # ★★言い回しが変わるなら、2AIが「意味は変わらない」と言うこと★★
            #   （2026-08-27・運営者から「機械的に無理なことは2AIで判断しろ」）
            #   ★機械には意味が分からない★＝
            #   「650G以内」→「650G以降」が意味の反転かどうかは判定できない。
            #   ★機械は「言い回しが変わった」ことだけを見つけて、
            #     2AIに答えさせ、その理由を記録に残す★。
            #   （理由の中身は機械が判定しない＝それは意味の判断だから）
            if _wording(a["before"]) != _wording(a["after"]):
                _mw = str(a.get("meaning_why") or "").strip()
                if len(_mw) < 15:
                    result["problems"].append(
                        "言い回しが変わる書き換えです。"
                        "意味が変わらない理由を meaning_why に書いてください"
                        "（2AIで判断して記録に残すため・15字以上）: "
                        f"{a['before'][:24]!r} → {a['after'][:24]!r}")
                    return result
                result.setdefault("meaning_judged", []).append(
                    {"before": a["before"][:60], "after": a["after"][:60],
                     "why": _mw[:120], "by": list(dec.get("decided_by") or [])})
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
                # ★★数値だけでなく「どの言葉に付くか」も出どころと同じこと★★
                #   （2026-08-27・Codexの5回目の指摘1）
                #   ★直す前は数値の存在だけ見ていた★ので、
                #   出どころが「通常700G／リセット800G」でも、
                #   「通常800G／リセット700G」と**逆**に書けた。
                _src_pairs = _shape(src)


                # ★★数え上げで比べる★★（2026-08-28・Codexの9回目）
                #   ★集合だと「同じ値が1件増える」変更が見えない★
                #   （500Gが1つ→2つ）。
                _cb = _Counter(_sb)
                _added_pairs = []
                for p in _sa:
                    if _cb.get(p):
                        _cb[p] -= 1
                    else:
                        _added_pairs.append(p)
                _miss_p = [p for p in _added_pairs
                           if not _slot_ok(p, _src_pairs)]
                if _miss_p:
                    result["problems"].append(
                        "出どころと、数値の付き先が違います: "
                        + " / ".join(f"{w}{n}" for w, n in _miss_p[:4])
                        + f"（出どころ: {_src_pairs[:4]}）")
                    return result
                # ★同上＝数え上げで見る★（2026-08-28・Codexの9回目）
                _cn = _Counter(nb)
                added = []
                for n in na:
                    if _cn.get(n):
                        _cn[n] -= 1
                    elif n not in added:
                        added.append(n)
                added = sorted(added)
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
            # ★★消すときも「どの言葉に付いた数値か」で見る★★
            #   （2026-08-27・Codexの5回目の指摘2）
            #   ★直す前は裸の数字で見ていた★ので、
            #   「通常時の天井は500G」を消しても
            #   「リセット時の天井は500G」が残れば通った＝別の事実が消える。
            # ★★丸ごと同じ要素がもう1つあるときだけ、機械が通す★★
            #   （2026-08-27・Codexの6回目の指摘2・3）
            #   ★直す前は「数値を伏せた部分一致」だった★ので、
            #     ・「通常時の天井は500G」を消しても
            #       「通常時の天井は600G」が残れば通った（600Gを正解扱い）
            #     ・数値のない文は検査を素通りしていた
            #   ★数値の有無に関係なく見る★／★表記違いは2AIへ回す★
            nums = [a["text"]]
            if True:
                # ★★読者に出る要素を数える★★（2026-08-27・Codexの6回目）
                #   ★記事のJSON全体への部分一致では見誤る★＝
                #   別の文の一部にたまたま含まれていても
                #   「残っている」に見えた。
                # ★同じ入れ物の中で2つ以上あるときだけ★（2026-08-27）
                lost = [] if _dup_count(d, a["text"]) >= 2 else nums
                if not lost:
                    # ★★機械が「重複」と認めた消し方を覚えておく★★
                    #   （2026-08-28・Codexの10回目の指摘1）
                    #   計画ができたあとに「1つ以上残るか」を確かめるため。
                    _dup_ok.append(a["text"])
                if lost:
                    # ★★機械で「重複だ」と言い切れないときは2AIへ★★
                    #   （2026-08-27・Codexの5回目の指摘2）
                    #   ★直す前は裸の数字で見ていた★ので、
                    #   「通常時の天井は500G」を消しても
                    #   「リセット時の天井は500G」が残れば通った
                    #   ＝別の事実が消えていた。
                    #   ★機械は「同じ言葉に付いた同じ数値が残るか」までしか
                    #     分からない★ので、残らないなら2AIが理由を書く。
                    _dw = str(a.get("meaning_why") or "").strip()
                    if len(_dw) < 15:
                        result["problems"].append(
                            "まったく同じ文が他にありません: "
                            + " / ".join(x[:40] for x in lost[:2])
                            + "（まったく同じ文の重複なら機械が通します。"
                            "言い方が違うだけの重複なら、"
                            "なぜ消してよいかを meaning_why に"
                            "書いてください・15字以上）")
                        return result
                    result.setdefault("meaning_judged", []).append(
                        {"drop": a["text"][:60], "why": _dw[:120],
                         "by": list(dec.get("decided_by") or [])})
                    # ★最後の数え直しは免除しない★（2026-08-27）
                    #   ★免除したら、重複を「両方」消して値が丸ごと消える
                    #     決定が通った★（実際に通った）。
                    #   1件ずつの検査は「別の事実を消していないか」、
                    #   最後の数え直しは「値が記事から消えていないか」で
                    #   ★役割が違う★。

    # 実際にあたるかを先に全部確かめる（1件でも外れたら何もしない）
    plan = []
    _used_drop = set()          # ★もう消すと決めた行★（同じ行を2度指さない）
    for a in dec["actions"]:
        hit = False
        # ★★同じ文字列が2か所にあるなら、どちらかを言わせる★★
        #   （2026-08-27・Codexの指摘4）
        #   ★直す前は本文が先に当たった★ので、
        #   「表を直したい」決定が**本文のほうを書き換えて**いた
        #   ＝誤った表が残り、正しい本文が変えられる（実際に再現した）。
        where = str(a.get("where") or "")
        if where and where not in ("body", "table_note",
                                   "fact", "summary", "lead"):
            result["problems"].append(f"直す場所の指定が不明です: {where!r}")
            return result
        if a.get("op") == "replace":
            spots = _where_hits(d, a.get("before"))
            if where:
                # ★場所を言っていても、その場所に2つあるなら決められない★
                _kind = {"body": "本文", "table_note": "表の注記",
                         "fact": "基本情報表", "summary": "要約ボックス",
                         "lead": "リード文"}[where]
                spots = [s for s in spots if s == _kind]
            if len(spots) > 1:
                result["problems"].append(
                    "同じ文字が" + "・".join(sorted(set(spots)))
                    + "に" + str(len(spots))
                    + "か所あります。どれを直すか決められません"
                    "（場所を where で絞る／同じ行が2つあるなら drop で"
                    "1つ消してください）")
                return result
        # ★消す先は `drop_spot` が決める★（許可の判定と同じ場所を見る）
        # ★★同じ行を2度指さない★★（2026-08-28・Codexの9回目の指摘2）
        #   ★直す前は毎回もとの記事から探していた★ので、
        #   同じ「消す」を2件並べると両方が同じ行を指し、
        #   ★2件やったと報告して1件しか消さなかった★（自分で再現した）。
        if a["op"] == "drop" and (not where or where == "body"):
            _sp = drop_spot(d, a["text"], used=_used_drop)
            if _sp:
                _used_drop.add(_sp)
                plan.append(("drop", _sp[0], _sp[1], a))
                hit = True
        for si, sec in enumerate(d.get("sections") or []):
            if hit:
                break
            if where and where not in ("body", "table_note"):
                break
            body = sec.get("body") or []
            for bi, line in enumerate(body):
                if not isinstance(line, str):
                    continue
                if a["op"] == "replace" and line == a["before"]:
                    plan.append(("replace", si, bi, a))
                    hit = True
                    break
            if hit:
                break
            # 表の注記も見る
            if where == "body":
                continue
            for ti, tbl in enumerate(sec.get("tables") or []):
                if a["op"] == "replace" and tbl.get("note") == a["before"]:
                    plan.append(("table_note", si, ti, a))
                    hit = True
                    break
            if hit:
                break
        if not hit:
            # ★節の外（基本情報表・要約ボックス・リード文）も見る★（台帳#487）
            got = None if where in ("body", "table_note") \
                else _outside_plan(d, a, where)
            if got:
                plan.append(got)
                hit = True
        if not hit:
            result["problems"].append(
                f"実データに無い行です（記事が変わった可能性）: "
                f"{(a.get('text') or a.get('before'))[:44]!r}")
            return result

    # ★★重複として通した消し方は、同じ節に1つ以上残ること★★
    #   （2026-08-28・Codexの10回目の指摘1／自分で再現した）
    #   ★1件ずつの検査は変更前の記事を見ている★ので、
    #   同じ「消す」を並べれば全部そろって許可され、
    #   ★事実が記事から丸ごと消えた★（数値の無い文は最後の数え直しも素通り）。
    _plan_drop = {}
    for _k, _si, _bi, _a in plan:
        if _k == "drop" and _a.get("text") in _dup_ok:
            _plan_drop[(_si, _a["text"])] = \
                _plan_drop.get((_si, _a["text"]), 0) + 1
    for (_si, _tx), _n in _plan_drop.items():
        _body = ((d.get("sections") or [])[_si] or {}).get("body") or []
        _have = sum(1 for x in _body if x == _tx)
        if _have - _n < 1:
            result["problems"].append(
                f"同じ文を全部消そうとしています（{_have}件のうち{_n}件）: "
                f"{_tx[:40]!r}"
                "（重複を1つにするだけなら通します。"
                "本当に全部消してよいなら、なぜかを meaning_why に"
                "書いてください）")
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
    # ★★組で数え直す★★（2026-08-27・Codexの5回目）
    #   裸の数字だと「別の言葉に付いた同じ数値」で消失を見逃す。
    # ★ここは「値が記事から丸ごと消えていないか」を見る★（2026-08-27）
    #   ★組で見ない★＝2AIが「同じ内容だ」と判断した重複の削除まで止まる。
    #   「別の事実を消していないか」は、1件ずつの検査（同じ文が残るか）の役。
    lost_pairs = sorted({("", n) for n in _numbers(raw)}
                        - {("", n) for n in _numbers(after_all)})
    lost = [n for _, n in lost_pairs]
    # ★消えてよい数値は、決定が1つずつ理由つきで名指ししたものだけ★
    #   （2026-08-22。goji_eva の「6.4割」＝640÷1000 の派生表現で、
    #    実際の天井は 1000G+α なので厳密な割合ではなかった。
    #    ★出典から取った事実ではなく、当サイトが計算した値★なので消せる。
    #    ★機械にはその区別が付かない★ので、2AIに名指しさせて記録に残す）
    ok_to_lose = {}
    for item in dec.get("numbers_removed") or []:
        if isinstance(item, dict) and item.get("n") and item.get("why"):
            ok_to_lose[str(item["n"])] = item["why"]
    # ★名指しは単位を書かなくても効く★（2026-08-27）
    #   数値の見方を「単位ごと」に変えたので、
    #   決定が「6.4」と名指ししていても「6.4割」に当たるようにする。
    #   ★別の数値には当てない★＝続きが数字や小数点なら別物
    #   （6.4 が 6.44 に当たらないように）。
    def _excused(pair) -> bool:
        """名指し（数値）と、2AIが判断した削除の分を許す。

        ★数値の部分で照らす★（2026-08-27）＝組にしたので
        「天井1000G」のような形になり、「1000」の名指しが当たらなくなっていた。
        """
        _w, num = pair
        for n in ok_to_lose:
            if num == n:
                return True
            if num.startswith(n) and not num[len(n):len(n) + 1].isdigit() \
                    and num[len(n):len(n) + 1] != ".":
                return True
        return False

    still = [n for w, n in lost_pairs if not _excused((w, n))]
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

    def _sha_of(slug):
        """★その記事のいまの指紋★（決定ファイルに必ず要る）。

        ★記事が無いときは合わない値を返す★＝
        その試験は「指紋が違う」か「機種が無い」で断られるのが正しい。
        """
        import hashlib as _h
        _p = os.path.join(td, str(slug) + ".json")
        if not os.path.isfile(_p):
            return "0" * 64
        _t = io.open(_p, encoding="utf-8").read().encode("utf-8")
        return _h.sha256(_t.replace(bytes([13, 10]), bytes([10]))).hexdigest()
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
                 "source_sha256": _sha_of("x"),
                 "decided_by": list(by), "actions": actions,
                 **({"source_sha256": sha} if sha else {})},
                ensure_ascii=False))
            return q

        r = apply_decision(dec([{"op": "drop", "text": "B の行です。",
                                 "why": "言い換え", "meaning_why": "2AIで読み比べ、同じ内容だと判断しました"}]))
        t("★決めたとおりに消せる★", not r["problems"] and len(r["done"]) == 1)

        r2 = apply_decision(dec([{"op": "drop", "text": "B の行です",
                                  "why": "1文字違い"}]))
        t("★★逐語が1文字でも違えば何もしない★★", bool(r2["problems"]))

        r3 = apply_decision(dec([{"op": "replace",
                                  "before": "A の行です。",
                                  "after": "A の行は 999G です。",
                                  "why": "数値を足す", "meaning_why": "2AIで読み比べ、意味は変わらないと判断しました"}]))
        t("★★数値が変わる言い換えは受け取らない★★（新値発明禁止）",
          bool(r3["problems"]))

        r4 = apply_decision(dec([{"op": "drop", "text": "A の行です。",
                                  "why": "…", "meaning_why": "2AIで読み比べ、同じ内容だと判断しました"},
                                 {"op": "drop", "text": "B の行です。",
                                  "why": "…", "meaning_why": "2AIで読み比べ、同じ内容だと判断しました"},
                                 {"op": "drop", "text": "C の行です。",
                                  "why": "…", "meaning_why": "2AIで読み比べ、同じ内容だと判断しました"}]))
        t("★★セクションが空になる決定は受け取らない★★", bool(r4["problems"]))

        try:
            _load_decision(dec([{"op": "drop", "text": "A の行です。",
                                 "why": "…", "meaning_why": "2AIで読み比べ、同じ内容だと判断しました"}], by=("Claude",)))
            t("★★判断者が1人の決定は受け取らない★★", False)
        except ValueError as e:
            t("★★判断者が1人の決定は受け取らない★★", "2つ以上" in str(e))

        try:
            _load_decision(dec([{"op": "drop", "text": "A の行です。", "meaning_why": "2AIで読み比べ、同じ内容だと判断しました"}]))
            t("　理由の無い操作は受け取らない", False)
        except ValueError:
            t("　理由の無い操作は受け取らない", True)

        try:
            _load_decision(dec([{"op": "rewrite", "text": "x", "why": "y"}]))
            t("　知らない操作は受け取らない", False)
        except ValueError:
            t("　知らない操作は受け取らない", True)

        r5 = apply_decision(dec([{"op": "drop", "text": "B の行です。",
                                  "why": "…", "meaning_why": "2AIで読み比べ、同じ内容だと判断しました"}], sha="0" * 64))
        t("★★判断したときから記事が変わっていたら何もしない★★",
          bool(r5["problems"]))

        # ★1件でも外れたら、通る分も書かない★
        r6 = apply_decision(dec([{"op": "drop", "text": "B の行です。",
                                  "why": "…", "meaning_why": "2AIで読み比べ、同じ内容だと判断しました"},
                                 {"op": "drop", "text": "無い行", "why": "…", "meaning_why": "2AIで読み比べ、同じ内容だと判断しました"}]),
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
                 "source_sha256": _sha_of("y"),
                 "decided_by": ["Claude", "codex"], "actions": actions},
                ensure_ascii=False))
            return r

        r8 = apply_decision(dec_y([{"op": "drop",
                                    "text": "通常時の天井は 500G です。",
                                    "why": "600Gのほうが正しそう", "meaning_why": "2AIで読み比べ、同じ内容だと判断しました"}]))
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
             "source_sha256": _sha_of("z"),
             "decided_by": ["Claude", "codex"],
             "actions": [{"op": "drop",
                          "text": "通常時の天井は 500G です。",
                          "why": "同じ行が2つある", "meaning_why": "2AIで読み比べ、同じ内容だと判断しました"}]},
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
                 "source_sha256": _sha_of("g"),
                 "decided_by": ["Claude", "codex"], "actions": actions},
                ensure_ascii=False))
            return r

        rg = apply_decision(dec_g([{"op": "drop",
                                    "text": "ポイントは最大7ptです。",
                                    "why": "わざと"}]))
        t("★★同じ文が他に残らない削除は、2AIの判断を求める★★"
          "／★裸の数字で見ていたころは、97.7%の中の7に当たって素通りした★",
          bool(rg["problems"])
          and "まったく同じ文が他にありません" in "".join(rg["problems"]))

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
                 "source_sha256": _sha_of("h"),
                 "decided_by": ["Claude", "codex"], "actions": actions},
                ensure_ascii=False))
            return r

        # ★言い回しが少し違う重複は、2AIが「同じ内容だ」と言う★
        #   （機械は「同じ言葉に付いた同じ数値が残るか」までしか分からない）
        rh1 = apply_decision(dec_h([{"op": "drop",
                                     "text": "リセットは700Gに短縮されます。",
                                     "why": "重複の片方",
                                     "meaning_why": "次の行とまったく同じ内容で、言い方だけが違います"}]))
        t("　重複の片方だけを消すのは通る", not rh1["problems"])

        # ★2AIの判断つきにして、最後の数え直しだけが効く形にする★
        #   （2026-08-27）★そうしないと1件ずつの検査が先に断って、
        #   狙った守りを一度も試せない★（罠④）。
        rh2 = apply_decision(dec_h([
            {"op": "drop", "text": "リセットは700Gに短縮されます。",
             "why": "重複の片方",
             "meaning_why": "次の行と同じ内容で、言い方だけが違います"},
            {"op": "drop", "text": "リセット時は700Gに短縮されます。",
             "why": "もう片方も",
             "meaning_why": "前の行と同じ内容で、言い方だけが違います"}]))
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
                    "source_sha256": _sha_of("k"),
                    "decided_by": ["Claude", "codex"], "actions": [act]}
            if removed:
                body["numbers_removed"] = removed
            io.open(r, "w", encoding="utf-8").write(
                json.dumps(body, ensure_ascii=False))
            return r

        base_act = {"op": "replace",
                    "before": "天井1000Gに対して6.4割のラインです。",
                    # ★出どころと同じ言い方で書く★（2026-08-27）
                    "after": "等価狙い目は640G〜です。",
                    "why": "…", "meaning_why": "2AIで読み比べ、意味は変わらないと判断しました"}

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
                    "source_sha256": _sha_of("k2"),
                    "decided_by": ["Claude", "codex"], "actions": [act]}
            if removed:
                body["numbers_removed"] = removed
            io.open(r, "w", encoding="utf-8").write(
                json.dumps(body, ensure_ascii=False))
            return r

        rk3 = apply_decision(dec_k2(
            dict(base_act, after="等価狙い目は555G〜です。",
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
            dict(base_act, numbers_from="等価狙い目は640G〜です。",
                 meaning_why="サイト自身が公開している狙い目に言い換えただけです"),
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
                    "source_sha256": _sha_of("m"),
                    "decided_by": ["Claude", "codex"], "actions": actions}
            if removed:
                body["numbers_removed"] = removed
            io.open(r, "w", encoding="utf-8").write(
                json.dumps(body, ensure_ascii=False))
            return r

        rm1 = apply_decision(dec_m([
            {"op": "replace", "before": "スルーカウントリセット",
             "after": "周期カウントリセット",
             "why": "この機種にスルー天井は無い（本文と揃える）", "meaning_why": "2AIで読み比べ、意味は変わらないと判断しました"}]),
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
             "why": "本文と同じ言い方にそろえる", "meaning_why": "2AIで読み比べ、意味は変わらないと判断しました"}]), apply_it=True)
        with io.open(os.path.join(td, "m.json"), encoding="utf-8") as f:
            _m2 = json.load(f)
        t("★要約ボックスも直せる★",
          not rm2["problems"]
          and _m2["summaryBoxes"][0]["value"] == "等価400G〜（CZ間）")

        # ★★この試験は「事実を作る」形だった★★（2026-08-27・Codexの指摘1）
        #   ★直す前は、元の文に無い「スマスロAT機」を足して合格していた★
        #   ＝★試験そのものが、塞ぐべき穴を実演していた★。
        #   いまは「時間で嘘になる一文を落とす」だけにしてある。
        rm3 = apply_decision(dec_m([
            {"op": "replace",
             "before": "この機種は2026年4月6日導入。解析は順次更新予定。",
             "after": "この機種は2026年4月6日導入。",
             "why": "時間で嘘になる文（順次更新予定）を落とす", "meaning_why": "2AIで読み比べ、意味は変わらないと判断しました"}]),
            apply_it=True)
        with io.open(os.path.join(td, "m.json"), encoding="utf-8") as f:
            _m3 = json.load(f)
        t("★リード文も直せる★（時間で嘘になる文を落とせる）",
          not rm3["problems"] and "順次更新予定" not in _m3["lead"])
        t("★★リード文にも、記事に無い事実は書き足せない★★"
          "／★私が書いた試験そのものが、この穴を実演していた★",
          bool(apply_decision(dec_m([
              {"op": "replace", "before": "この機種は2026年4月6日導入。",
               "after": "この機種は2026年4月6日に登場したスマスロAT機です。",
               "why": "わざと：記事に無い事実"}]))["problems"]))

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
             "numbers_from": "等価400G〜が狙い目です。",
             # ★2AIの判断は済んでいる形にする★＝
             #   ここで見たいのは「全部あてたあとの数え直し」なので、
             #   手前の検査で断られると、その守りを一度も試せない。
             "meaning_why": "この試験では、数え直しの守りだけを見ています"}]))
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

        # ── 2026-08-27・Codexのレビューで塞いだ穴 ────────────────
        N = {"slug": "n",
             "factTable": [["天井", "通常500G"]],
             "sections": [
                 {"title": "天井・恩恵",
                  "body": ["天井は500Gです。",
                           "AT中の獲得は500枚ほどです。",
                           "朝一は周期カウントがリセットされます。",
                           "通常500G"]}]}
        with io.open(os.path.join(td, "n.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump(N, f, ensure_ascii=False, indent=1)
            f.write("\n")

        def dec_n(actions):
            r = os.path.join(td, "dn.json")
            io.open(r, "w", encoding="utf-8").write(json.dumps(
                {"schema_version": SCHEMA, "slug": "n",
                 "source_sha256": _sha_of("n"),
                 "decided_by": ["Claude", "codex"], "actions": actions},
                ensure_ascii=False))
            return r

        # ①意味が反転する言い換え（数値は同じ）
        rn1 = apply_decision(dec_n([
            {"op": "replace", "before": "天井は500Gです。",
             "after": "天井は500G以下です。", "why": "わざと：意味が反転"}]))
        # ★この記事に「以下」は無いので、機械の検査が先に断る★
        #   （意味の判断まで行かない＝機械に分かることは機械が止める）
        t("★記事に無い言葉なら、意味の判断を待たずに断る★",
          bool(rn1["problems"])
          and "記事に無い言葉" in "".join(rn1["problems"]))
        # ★★ひらがなだけの反転も止める★★（2026-08-27・Codexの2回目）
        #   ★内容語だけ見ていたので、これは素通りしていた★
        rn1b = apply_decision(dec_n([
            {"op": "replace", "before": "天井は500Gです。",
             "after": "天井は500Gではありません。", "why": "わざと"}]))
        t("　ひらがなだけの打ち消しも、2AIの判断を求める",
          bool(rn1b["problems"])
          and "meaning_why" in "".join(rn1b["problems"]))
        rn1c = apply_decision(dec_n([
            {"op": "replace", "before": "天井は500Gです。",
             "after": "天井は500Gのスマスロです。", "why": "わざと：新しい語"}]))
        t("★★記事に無い言葉は書き足せない★★",
          bool(rn1c["problems"])
          and "記事に無い言葉" in "".join(rn1c["problems"]))
        # ★★出どころは実在する逐語でなければならない★★（同・2回目）
        #   ★直す前は中身を見ていなかった★ので、架空の逐語で抜けられた。
        rn1d = apply_decision(dec_n([
            {"op": "replace", "before": "天井は500Gです。",
             "after": "天井は500Gのスマスロです。",
             "numbers_from": "サイトのどこにも無い逐語です",
             "why": "わざと"}]))
        t("★★出どころの逐語が実在しなければ受け取らない★★"
          "／★架空の逐語を書けば、新語の検査を抜けられた★",
          bool(rn1d["problems"])
          and "見つかりません" in "".join(rn1d["problems"]))
        # ★対照★＝助詞をまたぐ組み替えは通る（正しい言い換えを止めない）
        rn2 = apply_decision(dec_n([
            {"op": "replace", "before": "朝一は周期カウントがリセットされます。",
             "after": "朝一は周期カウントリセットです。",
             "why": "言い換え", "meaning_why": "2AIで読み比べ、意味は変わらないと判断しました"}]))
        t("　（対照）助詞をまたぐ組み替えは通る", not rn2["problems"])

        # ②数値の並びを入れ替える
        rn3 = apply_decision(dec_n([
            {"op": "replace", "before": "天井は500Gです。",
             "after": "天井は500Gです。", "why": "同じ", "where": "body", "meaning_why": "2AIで読み比べ、意味は変わらないと判断しました"}]))
        t("　同じ内容の書き換えは通る（並び検査の対照）", not rn3["problems"])

        O = {"slug": "o", "sections": [
            {"title": "天井・恩恵",
             "body": ["通常500G／リセット600Gです。", "ほかの行です。"]}]}
        with io.open(os.path.join(td, "o.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump(O, f, ensure_ascii=False, indent=1)
            f.write("\n")
        ro = os.path.join(td, "do.json")
        io.open(ro, "w", encoding="utf-8").write(json.dumps(
            {"schema_version": SCHEMA, "slug": "o",
             "source_sha256": _sha_of("o"),
             "decided_by": ["Claude", "codex"],
             "actions": [{"op": "replace",
                          "before": "通常500G／リセット600Gです。",
                          "after": "通常600G／リセット500Gです。",
                          "why": "わざと：役割の入れ替え"}]},
            ensure_ascii=False))
        rn4 = apply_decision(ro)
        t("★★数値の付き方が入れ替わる書き換えは受け取らない★★"
          "／★どちらがどちらの値かを丸ごと取り違えさせられる★"
          "／★これは機械に分かる（構造の変化）ので機械が止める★",
          bool(rn4["problems"])
          and "入れ替わって" in "".join(rn4["problems"]))

        # ③単位が違えば別の数値
        #   ★専用の記事で試す★＝他所に「500G」があると、
        #   消しても本当に残っているので穴の再現にならない（実際に踏んだ）。
        U = {"slug": "u", "sections": [
            {"title": "天井・恩恵",
             "body": ["天井は500Gです。",
                      "AT中の獲得は500枚ほどです。",
                      "ほかの行です。"]}]}
        with io.open(os.path.join(td, "u.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump(U, f, ensure_ascii=False, indent=1)
            f.write("\n")
        ru = os.path.join(td, "du.json")
        io.open(ru, "w", encoding="utf-8").write(json.dumps(
            {"schema_version": SCHEMA, "slug": "u",
             "source_sha256": _sha_of("u"),
             "decided_by": ["Claude", "codex"],
             "actions": [{"op": "drop", "text": "天井は500Gです。",
                          "why": "わざと：残るのは500枚だけ"}]},
            ensure_ascii=False))
        rn5 = apply_decision(ru)
        t("★★同じ文が他に残らない削除は、2AIの判断を求める★★"
          "／★直す前は「獲得500枚」があれば天井500Gを消せた★",
          bool(rn5["problems"])
          and "まったく同じ文が他にありません" in "".join(rn5["problems"]))

        # ④同じ文字が2か所にある
        rn6 = apply_decision(dec_n([
            {"op": "replace", "before": "通常500G",
             "after": "通常500G", "why": "場所を言わない"}]))
        t("★★同じ文字が2か所にあれば、どちらかを言わせる★★"
          "／★直す前は本文が先に当たり、表を直したい決定が本文を変えた★",
          bool(rn6["problems"]) and "か所" in "".join(rn6["problems"]))
        rn7 = apply_decision(dec_n([
            {"op": "replace", "before": "通常500G", "after": "通常500G",
             "where": "fact", "why": "表を直す", "meaning_why": "2AIで読み比べ、意味は変わらないと判断しました"}]))
        t("　（対照）場所を言えば通る", not rn7["problems"])

        # ★★同じ場所に2つあっても数える★★（2026-08-27・Codexの2回目の指摘2）
        #   ★直す前は「本文・表…という種類」を数えていた★ので、
        #   ★同じ本文に同じ行が2つあっても1と数えて素通りした★
        V = {"slug": "v", "sections": [
            {"title": "天井・恩恵",
             "body": ["同じ行です。", "同じ行です。", "ほかの行です。"]}]}
        with io.open(os.path.join(td, "v.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump(V, f, ensure_ascii=False, indent=1)
            f.write("\n")
        rv = os.path.join(td, "dv.json")
        io.open(rv, "w", encoding="utf-8").write(json.dumps(
            {"schema_version": SCHEMA, "slug": "v",
             "source_sha256": _sha_of("v"),
             "decided_by": ["Claude", "codex"],
             "actions": [{"op": "replace", "before": "同じ行です。",
                          "after": "同じ行です。", "why": "場所不明"}]},
            ensure_ascii=False))
        rn10 = apply_decision(rv)
        t("★★同じ本文に同じ行が2つあれば、決められないと断る★★"
          "／★直す前は先頭が黙って書き換わった★",
          bool(rn10["problems"]) and "2か所" in "".join(rn10["problems"]))

        # ★★数値とラベルの対応を入れ替えさせない★★（同・指摘1）
        W = {"slug": "w", "sections": [
            {"title": "天井・恩恵",
             "body": ["通常500G／リセット600Gです。", "ほかの行です。"]}]}
        with io.open(os.path.join(td, "w.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump(W, f, ensure_ascii=False, indent=1)
            f.write("\n")
        rw = os.path.join(td, "dw.json")
        io.open(rw, "w", encoding="utf-8").write(json.dumps(
            {"schema_version": SCHEMA, "slug": "w",
             "source_sha256": _sha_of("w"),
             "decided_by": ["Claude", "codex"],
             "actions": [{"op": "replace",
                          "before": "通常500G／リセット600Gです。",
                          "after": "リセット500G／通常600Gです。",
                          "why": "わざと：ラベルの入れ替え"}]},
            ensure_ascii=False))
        rn11 = apply_decision(rw)
        t("★★数値とラベルの対応の入れ替えを止める（機械が断る）★★"
          "／★これは機械に分かる（構造の変化）ので機械が止める★",
          bool(rn11["problems"])
          and "入れ替わって" in "".join(rn11["problems"]))

        # ── 2026-08-27・Codexの3回目（骨組みで見る）────────────
        X = {"slug": "x2", "sections": [
            {"title": "天井・恩恵",
             "body": ["天井は500Gではありません。",
                      "通常は500G以上、リセットは600G以下です。",
                      "通常500G",
                      "リセットのときの話です。"]}]}
        with io.open(os.path.join(td, "x2.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump(X, f, ensure_ascii=False, indent=1)
            f.write("\n")

        def dec_x(act):
            r = os.path.join(td, "dx.json")
            io.open(r, "w", encoding="utf-8").write(json.dumps(
                {"schema_version": SCHEMA, "slug": "x2",
                 "source_sha256": _sha_of("x2"),
                 "decided_by": ["Claude", "codex"], "actions": [act]},
                ensure_ascii=False))
            return r

        rx1 = apply_decision(dec_x(
            {"op": "replace", "before": "天井は500Gではありません。",
             "after": "天井は500Gです。", "why": "わざと：打ち消しを消す"}))
        t("　打ち消しを消す書き換えも、2AIの判断を求める",
          bool(rx1["problems"])
          and "meaning_why" in "".join(rx1["problems"]))

        rx2 = apply_decision(dec_x(
            {"op": "replace",
             "before": "通常は500G以上、リセットは600G以下です。",
             "after": "通常は500G以下、リセットは600G以上です。",
             "why": "わざと：大小の入れ替え"}))
        t("　大小の入れ替えも、2AIの判断を求める",
          bool(rx2["problems"])
          and "meaning_why" in "".join(rx2["problems"]))

        rx3 = apply_decision(dec_x(
            {"op": "replace", "before": "通常500G", "after": "リセット500G",
             "where": "body", "why": "わざと：ラベルの差し替え"}))
        t("★数値が1つのラベル差し替えは、2AIの判断を求める★"
          "／★用語の直し（スルー→周期）と区別できないので機械では決めない★",
          bool(rx3["problems"])
          and "meaning_why" in "".join(rx3["problems"]))

        # ★★本番データで実際に通っていた反転★★（2026-08-27・Codexの4回目）
        #   enen2:「650G+α以内」→「以降」／bandori:「かつ」→「または」
        #   ★どちらも同じ記事の別の行に相手の語があるので、
        #     「記事に無い言葉」の検査には当たらなかった★
        Y = {"slug": "y2", "sections": [
            {"title": "天井・恩恵",
             "body": ["通常B以上では650G+α以内で当選します。",
                      "6周期以降かつスルー3以上が狙い目です。",
                      "650G+α以降は前兆を確認します。",
                      "または、リセット時は別の話です。"]}]}
        with io.open(os.path.join(td, "y2.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump(Y, f, ensure_ascii=False, indent=1)
            f.write("\n")

        def dec_y(act):
            r = os.path.join(td, "dy.json")
            io.open(r, "w", encoding="utf-8").write(json.dumps(
                {"schema_version": SCHEMA, "slug": "y2",
                 "source_sha256": _sha_of("y2"),
                 "decided_by": ["Claude", "codex"], "actions": [act]},
                ensure_ascii=False))
            return r

        ry1 = apply_decision(dec_y(
            {"op": "replace", "before": "通常B以上では650G+α以内で当選します。",
             "after": "通常B以上では650G+α以降で当選します。",
             "why": "わざと：範囲の反転"}))
        t("★★範囲の反転（以内→以降）は、2AIの判断を求める★★"
          "／★本番の enen2 で、何も言わずに通っていた★",
          bool(ry1["problems"])
          and "meaning_why" in "".join(ry1["problems"]))

        ry2 = apply_decision(dec_y(
            {"op": "replace", "before": "6周期以降かつスルー3以上が狙い目です。",
             "after": "6周期以降またはスルー3以上が狙い目です。",
             "why": "わざと：かつ→または"}))
        t("★★論理の反転（かつ→または）は、2AIの判断を求める★★"
          "／★本番の bandori で、何も言わずに通っていた★",
          bool(ry2["problems"])
          and "meaning_why" in "".join(ry2["problems"]))

        rx4 = apply_decision(dec_x(
            {"op": "replace", "before": "リセットのときの話です。",
             "after": "リセットのときの話。", "why": "文体をそろえる", "meaning_why": "2AIで読み比べ、意味は変わらないと判断しました"}))
        t("　（対照）骨組みが変わらない言い換えは通る", not rx4["problems"])

        # ★★判断者は「違う名前が2つ以上」★★（同・Codexの3回目）
        # ★ほかの検査は全部通る材料にする★（まったく同じ文が2つある）
        #   ＝判断者の守りだけが効く形（罠④を避ける）
        BY = {"slug": "by", "sections": [
            {"title": "天井・恩恵",
             "body": ["まったく同じ文です。", "まったく同じ文です。",
                      "ほかの行です。"]}]}
        with io.open(os.path.join(td, "by.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump(BY, f, ensure_ascii=False, indent=1)
            f.write("\n")
        _rby = os.path.join(td, "dby.json")
        io.open(_rby, "w", encoding="utf-8").write(json.dumps(
            {"schema_version": SCHEMA, "slug": "by",
             "source_sha256": _sha_of("by"),
             "decided_by": ["Claude", "Claude"],
             "actions": [{"op": "drop", "text": "まったく同じ文です。",
                          "why": "重複の片方", "meaning_why": "2AIで読み比べ、同じ内容だと判断しました"}]},
            ensure_ascii=False))
        # ★対照★＝違う名前なら、この決定は通る（他の検査は全部通る材料）
        _rby_ok = os.path.join(td, "dby_ok.json")
        io.open(_rby_ok, "w", encoding="utf-8").write(json.dumps(
            {"schema_version": SCHEMA, "slug": "by",
             "source_sha256": _sha_of("by"),
             "decided_by": ["Claude", "codex"],
             "actions": [{"op": "drop", "text": "まったく同じ文です。",
                          "why": "重複の片方", "meaning_why": "2AIで読み比べ、同じ内容だと判断しました"}]},
            ensure_ascii=False))
        t("　（対照）違う名前なら、この決定は通る",
          not apply_decision(_rby_ok)["problems"])
        def _stops(fn, word):
            """★断られること★を見る（例外でも問題の一覧でも合格）。"""
            try:
                r = fn()
            except Exception as e:                           # noqa: BLE001
                return word in str(e)
            return bool(r.get("problems")) and word in "".join(r["problems"])

        t("★★同じ名前を2つ並べても「2AIで決めた」にならない★★",
          _stops(lambda: apply_decision(_rby), "違う"))

        # ★★記事の指紋は必ず要る★★（同・Codexの3回目の指摘4）
        _rns = os.path.join(td, "dns.json")
        io.open(_rns, "w", encoding="utf-8").write(json.dumps(
            {"schema_version": SCHEMA, "slug": "x2",
             "decided_by": ["Claude", "codex"],
             "actions": [{"op": "drop", "text": "通常500G", "why": "x", "meaning_why": "2AIで読み比べ、同じ内容だと判断しました"}]},
            ensure_ascii=False))
        t("★★記事の指紋の無い決定ファイルは受け取らない★★"
          "／★直す前は「書いてあれば照合する」だった★",
          _stops(lambda: apply_decision(_rns), "指紋"))

        # ★★言い回しが変わるなら、2AIの判断を記録させる★★
        #   （2026-08-27・運営者から「機械的に無理なことは2AIで判断しろ」）
        #   ★機械には意味が分からない★ので、名簿で判定するのをやめた。
        #   代わりに「言い回しが変わった」ことだけを見つけて2AIに答えさせる。
        Z9 = {"slug": "z9", "sections": [
            {"title": "天井・恩恵",
             "body": ["天井は500Gです。", "ほかの行です。"]}]}
        with io.open(os.path.join(td, "z9.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump(Z9, f, ensure_ascii=False, indent=1)
            f.write("\n")

        def dec_z9(act):
            r = os.path.join(td, "dz9.json")
            io.open(r, "w", encoding="utf-8").write(json.dumps(
                {"schema_version": SCHEMA, "slug": "z9",
                 "source_sha256": _sha_of("z9"),
                 "decided_by": ["Claude", "codex"], "actions": [act]},
                ensure_ascii=False))
            return r

        rz1 = apply_decision(dec_z9(
            {"op": "replace", "before": "天井は500Gです。",
             "after": "天井は500Gとなります。", "why": "文体をそろえる"}))
        t("★★言い回しが変わるなら、2AIの判断が要る★★"
          "／★機械には意味が分からないので、名簿で判定しない★",
          bool(rz1["problems"])
          and "meaning_why" in "".join(rz1["problems"]))

        rz2 = apply_decision(dec_z9(
            {"op": "replace", "before": "天井は500Gです。",
             "after": "天井は500Gとなります。", "why": "文体をそろえる",
             "meaning_why": "文末をそろえただけで、天井の値も条件も同じです"}))
        t("　2AIが理由を書けば通る（理由の中身は機械が判定しない）",
          not rz2["problems"] and rz2.get("meaning_judged"))

        rz3 = apply_decision(dec_z9(
            {"op": "replace", "before": "天井は500Gです。",
             "after": "天井は500Gです。", "why": "同じ内容",
             "where": "body"}))
        t("　言い回しが変わらなければ、今までどおり通る", not rz3["problems"])

        # ── 2026-08-27・Codexの5回目（機械が守ると言った部分）────
        S5 = {"slug": "s5", "sections": [
            {"title": "天井・恩恵",
             "body": ["通常500G／リセット600G",
                      "通常700G／リセット800G",
                      "差枚+500枚がねらいです。",
                      "差枚-500枚がねらいです。",
                      "ほかの行です。"]}]}
        with io.open(os.path.join(td, "s5.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump(S5, f, ensure_ascii=False, indent=1)
            f.write("\n")

        def dec_s5(act):
            r = os.path.join(td, "ds5.json")
            io.open(r, "w", encoding="utf-8").write(json.dumps(
                {"schema_version": SCHEMA, "slug": "s5",
                 "source_sha256": _sha_of("s5"),
                 "decided_by": ["Claude", "codex"], "actions": [act]},
                ensure_ascii=False))
            return r

        # ★出どころと逆の対応で書けないこと★
        rs1 = apply_decision(dec_s5(
            {"op": "replace", "before": "通常500G／リセット600G",
             "after": "通常800G／リセット700G", "why": "わざと：逆の対応",
             "numbers_from": "通常700G／リセット800G",
             "meaning_why": "2AIで判断したことにしています（わざと）"}))
        t("★★出どころと、数値の付き先が違えば受け取らない★★"
          "／★数値の存在だけ見ていたので、逆の対応で書けた★",
          bool(rs1["problems"])
          and "付き先が違います" in "".join(rs1["problems"]))

        # ★対照★＝出どころどおりなら通る
        rs2 = apply_decision(dec_s5(
            {"op": "replace", "before": "通常500G／リセット600G",
             "after": "通常700G／リセット800G", "why": "出どころどおり",
             "numbers_from": "通常700G／リセット800G",
             "meaning_why": "出どころの値にそろえただけで、対応は同じです"}))
        t("　（対照）出どころどおりの対応なら通る",
          not [p for p in rs2["problems"] if "付き先" in p])

        # ★符号が違えば別の文★（消すときに「同じ文」と見なさない）
        rs3 = apply_decision(dec_s5(
            {"op": "drop", "text": "差枚+500枚がねらいです。",
             "why": "わざと：符号だけ違う行が残る"}))
        t("★★符号が違えば別の文として扱う★★"
          "／★符号を伏せていたら、+500枚と-500枚が同じ文に見えた★",
          bool(rs3["problems"])
          and "まったく同じ文が他にありません" in "".join(rs3["problems"]))

        # ★★書き換えで符号だけ変えるのも止める★★（2026-08-27）
        #   ★守っているのは骨組み（数値に符号を含める）★
        rs4 = apply_decision(dec_s5(
            {"op": "replace", "before": "差枚+500枚がねらいです。",
             "after": "差枚-500枚がねらいです。", "why": "わざと：符号の反転",
             "meaning_why": "2AIで判断したことにしています（わざと）"}))
        t("★★書き換えで符号だけ変えるのも止める★★"
          "／★符号を数値に含めていないと、同じ値に見える★",
          bool(rs4["problems"])
          and "入れ替わって" in "".join(rs4["problems"]))

        # ── 2026-08-27・Codexの6回目 ───────────────────────────
        S6 = {"slug": "s6", "sections": [
            {"title": "天井・恩恵",
             "body": ["通常時の天井は300G／リセット時の天井は400G",
                      "通常時の天井は500G／リセット時の天井は700G",
                      "控え：300G と 400G はここにも残ります",
                      "通常時の天井は500Gです。",
                      "通常時の天井は600Gです。",
                      "CZ間は500Gです。",
                      "天井は500Gです。",
                      "リセット後は天井が短縮されます。",
                      "ほかの行です。"]}]}
        with io.open(os.path.join(td, "s6.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump(S6, f, ensure_ascii=False, indent=1)
            f.write("\n")

        def dec_s6(act):
            r = os.path.join(td, "ds6.json")
            io.open(r, "w", encoding="utf-8").write(json.dumps(
                {"schema_version": SCHEMA, "slug": "s6",
                 "source_sha256": _sha_of("s6"),
                 "decided_by": ["Claude", "codex"], "actions": [act]},
                ensure_ascii=False))
            return r

        r61 = apply_decision(dec_s6(
            {"op": "replace",
             "before": "通常時の天井は300G／リセット時の天井は400G",
             "after": "通常時の天井は700G／リセット時の天井は500G",
             "why": "わざと：出どころと逆の対応",
             "numbers_from": "通常時の天井は500G／リセット時の天井は700G",
             "meaning_why": "2AIで判断したことにしています（わざと）"}))
        t("★★出どころと逆の対応は受け取らない★★"
          "／★直前の1語で見ていたころは「通常時」も「リセット時」も"
          "『天井』になり、逆に書けた★",
          bool(r61["problems"])
          and "付き先が違います" in "".join(r61["problems"]))

        r62 = apply_decision(dec_s6(
            {"op": "drop", "text": "通常時の天井は500Gです。",
             "why": "わざと：数値だけ違う行が残る"}))
        t("★★数値だけ違う行が残っても、機械は通さない★★"
          "／★数値を伏せて比べていたので、600Gを正解扱いできた★",
          bool(r62["problems"])
          and "まったく同じ文が他にありません" in "".join(r62["problems"]))

        r63 = apply_decision(dec_s6(
            {"op": "drop", "text": "天井は500Gです。",
             "why": "わざと：部分一致で残って見える"}))
        t("　部分一致で「残っている」に見える形も止める",
          bool(r63["problems"]))

        r64 = apply_decision(dec_s6(
            {"op": "drop", "text": "リセット後は天井が短縮されます。",
             "why": "わざと：数値が無い事実"}))
        t("★★数値のない事実の削除も、2AIの判断を求める★★"
          "／★数値がある文しか見ていなかったので、素通りしていた★",
          bool(r64["problems"]))

        # ★★出どころで1つに定まらなければ断る★★（2026-08-27）
        #   ★似た係り先が2つある出どころで、どちらの値か決められない★
        r65 = apply_decision(dec_s6(
            {"op": "replace",
             "before": "通常時の天井は300G／リセット時の天井は400G",
             # ★記事にある言葉だけ／数値は1つだけ★
             #   （2つあると、もう片方の検査が拾って
             #     狙った守りを試せない＝罠④）
             "after": "CZ間の天井は500G",
             "why": "わざと：どちらの値か決められない",
             "numbers_from": "通常時の天井は500G／リセット時の天井は700G",
             "meaning_why": "2AIで判断したことにしています（わざと）"}))
        t("★★出どころで1つに定まらなければ断る★★"
          "／★似た係り先が2つあると、どちらの値か決められない★",
          bool(r65["problems"])
          and "付き先が違います" in "".join(r65["problems"]))

        # ★対照★＝出どころどおりなら通る（前置きの違いは許す）
        r66 = apply_decision(dec_s6(
            {"op": "replace",
             "before": "通常時の天井は300G／リセット時の天井は400G",
             "after": "通常時の天井は500G／リセット時の天井は700G",
             "why": "出どころにそろえる",
             "numbers_from": "通常時の天井は500G／リセット時の天井は700G",
             "meaning_why": "出どころの値にそろえただけで、対応は同じです"}))
        t("　（対照）出どころどおりの対応なら通る",
          not [p for p in r66["problems"] if "付き先" in p])

        # ── 2026-08-27・Codexの7回目 ───────────────────────────
        S7 = {"slug": "s7", "sections": [
            {"title": "天井・恩恵",
             "body": ["通常時の天井は300G",
                      "リセット時の天井は500G",
                      "控え：300G はここにも残ります",
                      "天井は400Gです。"]},
            {"title": "【通常時】",
             "body": ["天井は500Gです。", "ほかの行です。"]},
            {"title": "【リセット時】",
             "body": ["天井は500Gです。", "べつの行です。"]}]}
        with io.open(os.path.join(td, "s7.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump(S7, f, ensure_ascii=False, indent=1)
            f.write("\n")

        def dec_s7(act):
            r = os.path.join(td, "ds7.json")
            io.open(r, "w", encoding="utf-8").write(json.dumps(
                {"schema_version": SCHEMA, "slug": "s7",
                 "source_sha256": _sha_of("s7"),
                 "decided_by": ["Claude", "codex"], "actions": [act]},
                ensure_ascii=False))
            return r

        r71 = apply_decision(dec_s7(
            {"op": "replace", "before": "通常時の天井は300G",
             "after": "通常時の天井は500G", "why": "わざと：別条件の値を持ち込む",
             "numbers_from": "リセット時の天井は500G",
             "meaning_why": "2AIで判断したことにしています（わざと）"}))
        t("★★出どころが別条件なら受け取らない★★"
          "／★『候補が1つに定まれば正しい』にしていたので、"
          "唯一の候補が間違っていても通った★",
          bool(r71["problems"])
          and "付き先が違います" in "".join(r71["problems"]))

        r72 = apply_decision(dec_s7(
            {"op": "replace", "before": "天井は400Gです。",
             "after": "天井は500Gです。", "why": "わざと：条件を落として一般化",
             "numbers_from": "リセット時の天井は500G",
             "meaning_why": "2AIで判断したことにしています（わざと）"}))
        t("★★条件を落として一般化する形も受け取らない★★"
          "／★終わりの一致で通していたので、条件が落ちた★",
          bool(r72["problems"])
          and "付き先が違います" in "".join(r72["problems"]))

        r73 = apply_decision(dec_s7(
            {"op": "drop", "text": "天井は500Gです。",
             "why": "わざと：別の節に同じ本文がある"}))
        t("★★節が違えば「重複」と見なさない★★"
          "／★平らに数えていたので、別条件の事実が黙って消えた★",
          bool(r73["problems"])
          and "まったく同じ文が他にありません" in "".join(r73["problems"]))

        # ── 2026-08-28・Codexの8回目 ───────────────────────────
        S8 = {"slug": "s8", "sections": [
            {"title": "朝一・リセット情報",
             "body": ["設定変更ありは500G／設定変更なしは600G",
                      "天井は500Gです。",
                      "ほかの行です。"]},
            {"title": "天井・恩恵",
             "body": ["天井は500Gです。", "天井は500Gです。",
                      "通常時の話です。"]}]}
        with io.open(os.path.join(td, "s8.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump(S8, f, ensure_ascii=False, indent=1)
            f.write("\n")

        def dec_s8(act):
            r = os.path.join(td, "ds8.json")
            io.open(r, "w", encoding="utf-8").write(json.dumps(
                {"schema_version": SCHEMA, "slug": "s8",
                 "source_sha256": _sha_of("s8"),
                 "decided_by": ["Claude", "codex"], "actions": [act]},
                ensure_ascii=False))
            return r

        r81 = apply_decision(dec_s8(
            {"op": "replace",
             "before": "設定変更ありは500G／設定変更なしは600G",
             "after": "設定変更ありは600G／設定変更なしは500G",
             "why": "わざと：ひらがなだけで区別するラベルの入れ替え",
             "numbers_from": "設定変更ありは500G／設定変更なしは600G"}))
        t("★★ひらがなだけで区別するラベルの入れ替えを止める★★"
          "／★係り先を「内容の文字だけ」で見ていたので、"
          "『あり』『なし』が同じ鍵になり黙って通った★",
          bool(r81["problems"]))

        r82 = apply_decision(dec_s8(
            {"op": "drop", "text": "天井は500Gです。",
             "why": "わざと：別の節の重複を根拠にする"}))
        t("★★別の節に2件あっても、消す節に1件しか無ければ通さない★★"
          "／★許可は「いちばん多い入れ物」、書き込みは「最初に当たった節」"
          "を見ていた＝別条件の事実を消せた★",
          bool(r82["problems"])
          and "まったく同じ文が他にありません" in "".join(r82["problems"]))

        # ★対照★＝同じ節の中に2つあるなら、今までどおり通る
        S8B = {"slug": "s8b", "sections": [
            {"title": "天井・恩恵",
             "body": ["天井は500Gです。", "天井は500Gです。", "残る行です。"]}]}
        with io.open(os.path.join(td, "s8b.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump(S8B, f, ensure_ascii=False, indent=1)
            f.write("\n")
        _r8b = os.path.join(td, "ds8b.json")
        io.open(_r8b, "w", encoding="utf-8").write(json.dumps(
            {"schema_version": SCHEMA, "slug": "s8b",
             "source_sha256": _sha_of("s8b"),
             "decided_by": ["Claude", "codex"],
             "actions": [{"op": "drop", "text": "天井は500Gです。",
                          "why": "同じ節の中の重複を1つにする"}]},
            ensure_ascii=False))
        t("　（対照）同じ節の中の重複なら、今までどおり通る",
          not apply_decision(_r8b)["problems"])

        # ★★奥の守り（係り先の照合）だけが効く材料★★
        #   （2026-08-28・罠⑫＝手前に守りを足すと、奥が試されなくなる）
        #   ・数値が**変わる**書き換え → 「並びの入れ替え」の検査は当たらない
        #   ・出どころの係り先がひらがなだけ違う
        #     → 内容の文字だけで比べると同じに見える
        S8H = {"slug": "s8h", "sections": [
            {"title": "朝一・リセット情報",
             "body": ["設定変更ありは500Gです。",
                      "設定変更なしは600Gです。",
                      "ほかの行です。"]}]}
        with io.open(os.path.join(td, "s8h.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump(S8H, f, ensure_ascii=False, indent=1)
            f.write("\n")
        _r8h = os.path.join(td, "ds8h.json")
        io.open(_r8h, "w", encoding="utf-8").write(json.dumps(
            {"schema_version": SCHEMA, "slug": "s8h",
             "source_sha256": _sha_of("s8h"),
             "decided_by": ["Claude", "codex"],
             "actions": [{"op": "replace",
                          "before": "設定変更ありは500Gです。",
                          "after": "設定変更ありは600Gです。",
                          "why": "わざと：別条件の値を持ち込む",
                          "numbers_from": "設定変更なしは600Gです。"}]},
            ensure_ascii=False))
        _g8h = apply_decision(_r8h)
        t("★★ひらがなだけ違う係り先の値を持ち込ませない★★"
          "／★係り先を内容の文字だけで比べると"
          "『設定変更あり』と『設定変更なし』が同じに見える★"
          "／★手前の「並びの入れ替え」検査は当たらない材料にしてある★",
          bool(_g8h["problems"])
          and "付き先が違います" in "".join(_g8h["problems"]))

        # ── 2026-08-28・Codexの9回目 ───────────────────────────
        S9 = {"slug": "s9", "sections": [
            {"title": "設定示唆まとめ",
             "body": ["設定1は320G／設定2は314G", "ほかの行です。"]},
            {"title": "天井・恩恵",
             "body": ["天井は500Gです。", "天井は500Gです。",
                      "天井は500Gです。", "残る行です。"]}]}
        with io.open(os.path.join(td, "s9.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump(S9, f, ensure_ascii=False, indent=1)
            f.write("\n")

        def dec_s9(acts, nm="ds9"):
            r = os.path.join(td, nm + ".json")
            io.open(r, "w", encoding="utf-8").write(json.dumps(
                {"schema_version": SCHEMA, "slug": "s9",
                 "source_sha256": _sha_of("s9"),
                 "decided_by": ["Claude", "codex"], "actions": acts},
                ensure_ascii=False))
            return r

        r91 = apply_decision(dec_s9([
            {"op": "replace",
             "before": "設定1は320G／設定2は314G",
             "after": "設定1は314G／設定2は320G",
             "why": "わざと：数字を含むラベルの入れ替え",
             "numbers_from": "設定1は320G／設定2は314G"}]))
        t("★★数字を含むラベル（設定1／設定2）の入れ替えを止める★★"
          "／★`_shape` はラベルの中の数字も値として読むので、"
          "本当の値の係り先が空文字になり、どの検査にも当たらなかった★",
          bool(r91["problems"])
          and "数値の並びが入れ替わっています" in "".join(r91["problems"]))

        # ★同じ「消す」を2件並べたら、2件ぶん消えること★
        #   ★直す前は、両方が同じ行を指して1件しか消えなかった★
        #   （報告は2件・結果は1件＝食い違い）
        _r92 = dec_s9([
            {"op": "drop", "text": "天井は500Gです。", "why": "重複を1つにする"},
            {"op": "drop", "text": "天井は500Gです。",
             "why": "重複をもう1つ消す"}], nm="ds9b")
        _got92 = apply_decision(_r92, apply_it=True)
        _after92 = json.load(io.open(os.path.join(td, "s9.json"),
                                     encoding="utf-8"))
        _left92 = [x for x in _after92["sections"][1]["body"]
                   if x == "天井は500Gです。"]
        t("★★同じ「消す」を2件並べたら、2件ぶん消える★★"
          "／★毎回もとの記事から探していたので、両方が同じ行を指し、"
          "2件やったと報告して1件しか消さなかった★",
          not _got92["problems"] and len(_got92["done"]) == 2
          and len(_left92) == 1)

        # ⑱置き場の外を指せない
        _bad_slug = os.path.join(td, "dbad.json")
        io.open(_bad_slug, "w", encoding="utf-8").write(json.dumps(
            {"schema_version": SCHEMA, "slug": "../x",
             "source_sha256": _sha_of("../x"),
             "decided_by": ["Claude", "codex"],
             "actions": [{"op": "drop", "text": "a", "why": "わざと"}]},
            ensure_ascii=False))
        rn9 = apply_decision(_bad_slug)
        t("★★機種の名前で置き場の外を指せない★★"
          "／★直す前は `../` や絶対パスで外のJSONを書き換えられた★",
          bool(rn9["problems"])
          and "使えない文字" in "".join(rn9["problems"]))

        # ── 2026-08-27・Codexの指摘6（合意と適用の結線）──────────
        #   ★本物の記録を作って確かめる★（手書きのJSONを置かない）
        import hashlib as _hl6
        import repair_journal as _rj6
        import shutil as _sh6
        _keep6 = _rj6.STORE
        _dir6 = tempfile.mkdtemp()
        try:
            _rj6.STORE = _dir6
            Q = {"slug": "q", "sections": [
                {"title": "天井・恩恵",
                 "body": ["合意する行です。", "残る行です。", "もう1行です。"]}]}
            _qp = os.path.join(td, "q.json")
            with io.open(_qp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(Q, f, ensure_ascii=False, indent=1)
                f.write("\n")
            _qraw = io.open(_qp, encoding="utf-8").read()
            _qsha = _hl6.sha256(
                _qraw.encode("utf-8").replace(b"\r\n", b"\n")).hexdigest()

            def _decq(actions, fid=None, name="decq"):
                p = os.path.join(td, name + ".json")
                body = {"schema_version": SCHEMA, "slug": "q",
                        "source_sha256": _sha_of("q"),
                        "source_sha256": _qsha,
                        "decided_by": ["Claude", "codex"], "actions": actions}
                if fid:
                    body["finding_id"] = fid
                io.open(p, "w", encoding="utf-8").write(
                    json.dumps(body, ensure_ascii=False))
                return p

            _ops_q = [{"op": "drop", "text": "合意する行です。",
                       "why": "前の段落と同じ内容", "meaning_why": "2AIで読み比べ、同じ内容だと判断しました"}]
            _r6 = _rj6.detect("q", "text_gone", "合意する行です。", "",
                              source_sha256=_qsha)
            _f6 = _r6["finding_id"]
            _v6 = os.path.join(td, "v_q.md")
            io.open(_v6, "w", encoding="utf-8").write("私の判定です。" * 5)
            _rj6.seal_claude(_f6, _v6)
            _rj6.record_codex(_f6, "0" * 64, "Codexの判定です。" * 3)
            _rj6.agree(_f6, _decq(_ops_q, fid=_f6, name="decq_agree"),
                       "text_gone", ["Claude", "codex"])

            _x1 = apply_decision(_decq(_ops_q))
            t("★★合意が生きている機種は、件を名乗らないと書けない★★"
              "／★直す前は、無害な合意を別の書き換えの許可証にできた★",
              bool(_x1["problems"])
              and "finding_id" in "".join(_x1["problems"]))

            _x2 = apply_decision(_decq(_ops_q, fid="よその件", name="decq2"))
            t("　その機種の生きている合意でなければ書けない",
              bool(_x2["problems"])
              and "生きている合意ではありません" in "".join(_x2["problems"]))

            _x3 = apply_decision(_decq(
                [{"op": "drop", "text": "残る行です。", "why": "すり替え", "meaning_why": "2AIで読み比べ、同じ内容だと判断しました"}],
                fid=_f6, name="decq3"))
            t("★★合意したあとで中身を差し替えても書けない★★",
              bool(_x3["problems"])
              and "違います" in "".join(_x3["problems"]))

            _x4 = apply_decision(_decq(_ops_q, fid=_f6, name="decq4"))
            t("　（対照）合意どおりなら書ける", not _x4["problems"])

            # ★★記録に読めないものがあれば止める★★
            #   （2026-08-27・Codexの2回目の指摘4）
            #   ★直す前は「読めないなら見ない」＝通していた★
            #   ＝記録の仕組みを壊せば、合意の検査を丸ごと外せた。
            #   ★壊れた記録は機種が分からない★ので、
            #   「この機種の合意が無い」と言い切れない（安全側へ倒す）。
            io.open(os.path.join(_dir6, "broken_x.json"), "w",
                    encoding="utf-8").write("{こわれています")
            _x5 = apply_decision(_decq(_ops_q, fid=_f6, name="decq5"))
            t("★★記録に読めないものがあれば、書かずに止まる★★"
              "／★直す前は、記録を壊せば合意の検査を外せた★",
              bool(_x5["problems"])
              and "読めない" in "".join(_x5["problems"]))
            os.remove(os.path.join(_dir6, "broken_x.json"))
        finally:
            _rj6.STORE = _keep6
            _sh6.rmtree(_dir6, ignore_errors=True)

        r7 = apply_decision(dec([{"op": "drop", "text": "B の行です。",
                                  "why": "…", "meaning_why": "2AIで読み比べ、同じ内容だと判断しました"}]), apply_it=True)
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
        # ★★まず記事の全文を出す★★（2026-08-27・Codexの指摘5）
        #   ★直す前は手がかりの一覧しか出なかった★ので、
        #   `--json` を付けない呼び方だと**読むものが無い**ように見えた。
        #   ★手がかりは網羅ではない★＝全文を読まないと見つからない
        #   食い違いのほうが多い（節をまたぐ重複・体言止め など）。
        art = g.get("article") or {}
        print(f"★{a.slug}（{art.get('name') or ''}）の記事 全文★")
        if art.get("lead"):
            print("【リード文】")
            print("  " + str(art["lead"]))
        if art.get("factTable"):
            print("【基本情報表】")
            for row in art["factTable"]:
                print("  " + " ｜ ".join(str(x) for x in row))
        if art.get("summaryBoxes"):
            print("【要約ボックス】")
            for box in art["summaryBoxes"]:
                print(f"  {box.get('label')} ｜ {box.get('value')}")
        for sec in art.get("sections") or []:
            print(f"【{sec.get('title')}】"
                  + (f"（{sec['type']}）" if sec.get("type") else ""))
            for line in sec.get("body") or []:
                print("  " + str(line))
            for note in sec.get("notes") or []:
                print("  （表の注記）" + str(note))
        print()
        print(f"★機械が気づけた手がかり★ {len(g['candidates'])} 件  "
              f"{g.get('counts')}")
        for c in g["candidates"]:
            print(f"  [{c['kind']}] {c.get('section') or ''}")
            for k in ("a", "b", "before", "after", "drop", "mixed", "word"):
                if c.get(k):
                    print(f"      {k}: {str(c[k])[:88]}")
        if not g["candidates"]:
            print("  （機械が気づけたものはありません。"
                  "★手がかりは網羅ではないので、上の全文を読んで決めてください★）")
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

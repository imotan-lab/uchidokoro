#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""collect_evidence.py — 1機種ぶんの「出典の原文」を集める（★判断はしない★）。

★何のための道具か（2026-08-07）★
  これまでは正規表現で値を取り出そうとしていた。しかしサイトごとに書き方が違い、
  「載っているのに読めない」が延々と続いた（実測：6機種に数時間かけて1機種ぶん）。
  ★機械は原文を集めるところまで★にして、**中身の判断はAIがやる**。
  ClaudeとCodexが同じ原文を別々に読み、突き合わせて、違いだけ相談する。
  （2026-08-06に6機種でこの方法に切り替えたら、1往復で全機種の判定が出た）

★この道具がやること／やらないこと★
  やる   : 3つの名鑑から機種ページを見つけ、話題ごとに**関連しそうな抜粋**を集める
           出典のURL・取得日時・本文の指紋・**取りこぼしたか**を必ず添える
  やらない: 値を決める・数字を作る・どれが正しいか選ぶ（★全部AIと人の仕事★）

★これは「原文そのまま」ではない★（2026-08-07・Codex143回目の指摘）
  キーワードの前後だけを切り出すので、**次の文に書かれた条件や例外は落ちる**。
  ClaudeとCodexが同じ欠けた抜粋を読めば、**両方が同じ誤判定をする**。
  だから ①切り捨てたら必ず知らせる（truncated）②切り捨てた話題は自動採用しない
  ③足りなければAIに「この抜粋には無い」と言わせる、の3つで守る。

使い方:
    python scripts/collect_evidence.py --slug yajikita_mairu
    python scripts/collect_evidence.py --slug xxx --topic ceiling
    python scripts/collect_evidence.py --slug xxx --out 依頼文.md
    python scripts/collect_evidence.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import safe_json as _sj                  # noqa: E402
import source_lineage as _sl             # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 話題ごとの「探す言葉」と「その周りをどれだけ取るか」
#   ★広めに取る★＝機械が切り詰めると、判断に要る条件（何を数えるか等）が落ちる。
TOPICS = {
    "ceiling": {"jp": "天井",
                "pat": r"[^。\n]{0,60}天井[^。\n]{0,90}",
                # ★「1,200G」「天井なし」も拾う★（2026-08-07・Codex143回目）
                "need": r"[\d,]{2,6}\s*[GＧ]|\d+\s*スルー|\d+\s*周期"
                        r"|\d+\s*まいる|天井(?:なし|非搭載|無し)"},
    "at": {"jp": "AT/ST/ボーナスの仕様",
           # ★AT・ST・ボーナス単独も拾う★（「ATは30G継続」を落としていた）
           "pat": r"[^。\n]{0,55}(?:純増|1セット|継続率|ループ率|初当り|突入率|上乗せ"
                  r"|AT|ST|ボーナス)[^。\n]{0,85}",
           "need": r"\d"},
    "gameplay": {"jp": "ゲーム性（数字が無い説明も拾う）",
                 "pat": r"[^。\n]{0,55}(?:ゲーム性|突入条件|終了条件|移行|抽選|契機"
                        r"|仕組み|概要)[^。\n]{0,85}",
                 "need": r""},
    "cz": {"jp": "CZ",
           "pat": r"[^。\n]{0,55}(?:CZ|チャレンジ|チャンス(?!アップ))[^。\n]{0,85}",
           "need": r"\d"},
    "reset": {"jp": "朝一・リセット",
              "pat": r"[^。\n]{0,55}(?:設定変更|電源|リセット|朝一|有利区間)"
                     r"[^。\n]{0,85}",
              "need": r""},
    "settei": {"jp": "設定示唆・設定差",
               "pat": r"[^。\n]{0,55}(?:設定[1-6]|設定示唆|高設定|終了画面|トロフィー)"
                      r"[^。\n]{0,85}",
               "need": r""},
    "yame": {"jp": "ヤメ時",
             "pat": r"[^。\n]{0,55}(?:ヤメ|やめ時|やめどき|即ヤメ)[^。\n]{0,85}",
             "need": r""},
}
PER_SOURCE = 12                           # 1出典・1話題あたりの件数の上限
QUOTE_MAX = 400                           # 1件の抜粋の長さの上限（★切ったら言う★）


def _now() -> str:
    import datetime
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def machine(slug: str) -> dict:
    rows = _sj.read_json(os.path.join(BASE, "assets", "data", "machines.json"),
                         expect=(dict, list))
    rows = rows["machines"] if isinstance(rows, dict) else rows
    got = [m for m in rows if m.get("slug") == slug]
    if not got:
        raise SystemExit(f"★そんな機種はありません: {slug}★")
    return got[0]


def quotes(text: str, topic: str, limit: int = PER_SOURCE) -> dict:
    """その話題の抜粋を集める。★取りこぼしたら必ず知らせる★

    戻り値: {"quotes": [...], "matched_total": n, "truncated": bool}
    ★truncated の話題は自動で採用しない★（2026-08-07・Codex143回目。
      13件目に大事な条件があっても、AIには「無い」のと区別が付かない）
    """
    conf = TOPICS[topic]
    need = re.compile(conf["need"]) if conf["need"] else None
    seen, out, total, cut = set(), [], 0, 0
    ends = "".join(("。", chr(10)))
    for m in re.compile(conf["pat"]).finditer(text):
        raw = m.group(0)
        g = " ".join(raw.split())
        if len(g) < 12 or (need and not need.search(g)):
            continue
        # ★重複は「全文が同じ」ときだけ★（2026-08-07・台帳#249）
        #   先頭34字で判定していたので、**書き出しが同じで中身が違う行**
        #   （表の行など）が黙って捨てられ、件数にも出なかった。
        if g in seen:
            continue
        seen.add(g)
        total += 1
        if len(out) < limit:
            # ★文の途中で切れたら必ず言う★（2026-08-07・台帳#249）
            #   切っているのは200字の上限ではなく**キーワードの前後の窓**。
            #   sf6で「バトルパート:15G+」の「+α」が窓の外に出て切れ、
            #   ★ClaudeもCodexも「原文に無い」と判断して気づけなかった★。
            tail_cut = m.end() < len(text) and text[m.end()] not in ends
            head_cut = m.start() > 0 and text[m.start() - 1] not in ends
            if len(g) > QUOTE_MAX:
                g, tail_cut = g[:QUOTE_MAX], True
            if head_cut:
                g = "★前が切れています★…" + g
            if tail_cut:
                g = g + "…★ここで切れています★"
            if head_cut or tail_cut:
                cut += 1
            out.append(g)
    return {"quotes": out, "matched_total": total,
            "truncated": total > len(out),
            "context_truncated": cut}


def collect(slug: str, topics: list, fetch=None, name: str = "") -> dict:
    """1機種ぶん集める。★取れなかった出典も理由つきで残す★

    ★name を渡せる理由（2026-08-09・台帳#273）★
      新台は machines.json にまだ無いので、slug では引けない。
      そのため手順書の2AI工程（新台=STEP 3-B）が**実行できなかった**
      （「そんな機種はありません」で止まる）。正式名称を直接渡せるようにする。
    """
    m = {"slug": slug, "name": name} if name else machine(slug)
    out = {"slug": slug, "name": m.get("name"), "topics": topics, "sources": {}}
    if fetch is None:
        import ceiling_lookup as _cl
        import directory_index as _di
        import new_machine_watch as _nw

        import machine_sources as _ms

        def _read(where, url, got, publisher=None):
            try:
                got[where] = {"url": url, "publisher": publisher,
                              "text": _cl.cut_user_area(
                                  _cl._norm(_nw._visible_text(_nw._get(url))))}
            except Exception as e:        # noqa: BLE001
                got[where] = {"url": url, "publisher": publisher,
                              "error": str(e)[:80]}

        def fetch(name):
            got = {}
            cats = _sj.read_json(_di.CATALOGS, expect=dict)["directories"]
            for dir_id, r in (_di.find(name).get("results") or {}).items():
                if r.get("state") != "FOUND":
                    got[dir_id] = {"state": r.get("state")}
                    continue
                _read(dir_id, r["url"], got,
                      (cats.get(dir_id) or {}).get("publisher_id"))
            # ★人が一度確かめた出典も足す★（2026-08-07・台帳#265）
            #   名鑑は機種名で引くので、表記が違う機種を引き当てられない。
            #   「スマスロ防振り」↔「痛いのは嫌なので防御力に極振り…」のような
            #   意味の判断はAIがして、確かめた結果をここから読む。
            #   ★控えが無くても止まらない★＝いままで通り名鑑だけで動く。
            try:
                saved = _ms.urls_for(slug)
            except Exception as e:        # noqa: BLE001
                got["_saved_"] = {"error": "控えを読めません: " + str(e)[:60]}
                saved = []
            for rec in saved:
                url = rec.get("url")
                if not url or any(v.get("url") == url for v in got.values()):
                    continue
                _read("控え:" + str(rec.get("publisher") or "?"), url, got,
                      rec.get("publisher"))
            return got
    for dir_id, r in (fetch(m.get("name")) or {}).items():
        if not r.get("text"):
            out["sources"][dir_id] = {k: v for k, v in r.items() if k != "text"}
            continue
        import hashlib
        rec = {"url": r["url"], "publisher": r.get("publisher"),
               # ★同じ原文を2人が読んだことを後から確かめられるように★
               "text_sha256": hashlib.sha256(r["text"].encode("utf-8")).hexdigest(),
               "text_len": len(r["text"]),
               # ★いつの原文か★（同じURLでも中身は日々変わる・台帳#250）
               "fetched_at": _now()}
        for t in topics:
            rec[t] = quotes(r["text"], t)
        out["sources"][dir_id] = rec
    # ★本文が空なら「使える出典」に数えない★（台帳#250）
    #   出典の数で公開の可否を決めるので、ここが緩いと土台が崩れる
    usable = [v for v in out["sources"].values()
              if v.get("url") and not v.get("error")
              and (v.get("text_len") or 0) > 0]
    out["usable_sources"] = len(usable)
    # ★「2つ揃った」を決めるのはページ数ではなく発行者の数★（2026-08-09・依頼125）
    #   名鑑と控えに同じ発行者が出ると、URLが違うだけで2件に見えていた。
    #   例: なな徹1社しか無い機種が「使える出典2件」と表示され、
    #       2AIの突き合わせが「大手2つが一致」と誤って判断できてしまう。
    keys, unknown = set(), []
    for v in usable:
        try:
            keys.add(_sl.vote_key(v.get("publisher")))
        except Exception:                 # noqa: BLE001
            # ★引けないものは票にしない★（仮の名前を作らない）
            unknown.append(v.get("url"))
    out["usable_lineages"] = len(keys)
    out["lineage_unknown"] = unknown
    return out


def as_request(got: dict) -> str:
    """AIへ渡す依頼文にする（★原文とURLだけ・こちらの判断は書かない★）。"""
    L = ["# 判定依頼 — 原文だけを見て中身を決めてください",
         "",
         f"機種: **{got['name']}**（slug: `{got['slug']}`）",
         "",
         "## お願い",
         "- **大手2サイトが合致したものだけ**を採用してください",
         "- **根拠にした引用**と**何出典で一致したか**を必ず書いてください",
         "- ★原文に無い数字を補わないでください★。足りなければ"
         "「この抜粋には無い」と書いてください",
         "- **別物を同じにしない**（例: 通常時とAT間、CZとCZorAT、メインATと上位AT）",
         "- 迷うものは保留にし、**なぜ迷うか**を書いてください",
         ""]
    for dir_id, r in got["sources"].items():
        if not r.get("url"):
            L.append(f"### {dir_id}: 見つかりません（{r.get('state')}）")
            continue
        if r.get("error"):
            L.append(f"### {dir_id}: 取得できません（{r['error']}）")
            continue
        L.append(f"### {dir_id}")
        L.append(f"{r['url']}")
        for t in got["topics"]:
            g = r.get(t) or {}
            qs = g.get("quotes") or []
            mark = ""
            if g.get("truncated"):
                mark = (f"　★抜粋を打ち切りました（全{g.get('matched_total')}件中"
                        f"{len(qs)}件）＝ここは自動で採用しないでください★")
            if g.get("context_truncated"):
                mark += (f"　★{g['context_truncated']}件は途中で切れています"
                         f"（「…★ここで切りました★」の行）＝その先は"
                         f"「原文に無い」ではなく「見えていない」と扱ってください★")
            L.append(f"- ＜{TOPICS[t]['jp']}＞ {len(qs)}件{mark}")
            for q in qs:
                L.append(f"  - 「{q}」")
        L.append("")
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------ selftest

def selftest() -> int:
    ok, ran = True, [0]

    def t(name, cond):
        nonlocal ok
        ran[0] += 1
        print(("✅ " if cond else "❌ ") + name)
        ok = ok and bool(cond)

    T = ("天井は通常時999G+αで、到達時はボーナスに当選。 "
         "AT純増は約8.0枚/G。 ST「ショウタイム」は10G継続、継続率は約50%。 "
         "CZ「テストチャレンジ」は7G、成功期待度約33%。 "
         "設定変更後は600G+αに短縮。 天井は近いのでヤメ時に注意。")
    def Q(text, topic):
        return quotes(text, topic)["quotes"]

    t("★★天井の抜粋を、値ごと取り出せる★★", any("999G" in q for q in Q(T, "ceiling")))
    t("　数字が無い天井の話は拾わない（見出しなど）", Q("天井・ゾーン・ヤメ時", "ceiling") == [])
    t("★★『1,200G』『天井なし』も拾う★★（2026-08-07・Codex143回目）",
      Q("通常時1,200Gで天井に到達する。", "ceiling")
      and Q("この機種は天井なしの設定狙い専用です。", "ceiling"))
    t("★★ATの仕様も拾える★★",
      any("8.0枚" in q for q in Q(T, "at")) and any("継続率" in q for q in Q(T, "at")))
    t("　『ATは30G継続』のような書き方も拾う（AT単独の語）",
      any("30G" in q for q in Q("メインATは30G継続で消化していきます。", "at")))
    t("　CZも拾える", any("テストチャレンジ" in q for q in Q(T, "cz")))
    t("　リセットも拾える", any("設定変更後" in q for q in Q(T, "reset")))
    t("　ゲーム性（数字が無い説明）も拾う",
      any("突入条件" in q for q in Q("CZの突入条件はレア役成立時となっています。",
                                     "gameplay")))
    _dup = "メインATの純増は約8.0枚/Gです。 メインATの純増は約8.0枚/Gです。"
    t("★★同じ書き出しの重複は1回だけ★★（同じ話が何度も出るページがある）",
      len(Q(_dup, "at")) == 1)
    t("　短すぎる断片は拾わない（見出しの切れ端）", Q("純増8枚", "at") == [])
    _many = "。".join(f"モード{i}の天井は{100+i}Gでボーナスに当選します"
                      for i in range(20)) + "。"
    _g = quotes(_many, "ceiling", limit=5)
    t("★★抜粋を打ち切ったら必ず知らせる★★"
      "（13件目に大事な条件があっても、AIには『無い』のと区別が付かない）",
      _g["truncated"] and _g["matched_total"] == 20 and len(_g["quotes"]) == 5)
    t("　打ち切っていなければ truncated は立たない",
      quotes(T, "ceiling")["truncated"] is False)

    # --- ★黙って切り落とさない★（2026-08-07・台帳#249）
    _long = "あ" * 80 + "天井は1000Gでボーナス当選" + "い" * 120 + "。"
    _lg = quotes(_long, "ceiling")
    t("★★文の途中で切れたら必ず言う★★"
      "（sf6で「15G+」の+αが窓の外に出て切れ、両AIが『原文に無い』と誤判定した）",
      _lg["context_truncated"] == 1
      and "★ここで切れています★" in _lg["quotes"][0]
      and "★前が切れています★" in _lg["quotes"][0])
    t("　文まるごと入っていれば切れたと言わない",
      quotes("天井は1000Gでボーナスに当選する。", "ceiling")["context_truncated"] == 0)
    _same = ("モードAの天井は500Gでボーナスに当選する。"
             "モードAの天井は600Gでボーナスに当選する。")
    _sg = quotes(_same, "ceiling")
    t("★★書き出しが同じでも中身が違えば両方残す★★"
      "（先頭34字で捨てていたので、表の行が黙って消えていた）",
      _sg["matched_total"] == 2 and len(_sg["quotes"]) == 2)
    t("　まったく同じ文は1回だけ",
      quotes("天井は1000Gでボーナス当選。天井は1000Gでボーナス当選。",
             "ceiling")["matched_total"] == 1)

    def _fake(name):
        return {"chonborista": {"url": "https://a.example/1", "text": T},
                "p-world": {"url": "https://b.example/2", "text": T},
                "dmm-ptown": {"state": "HEALTHY_NO_MATCH"}}
    keep = globals()["machine"]
    try:
        globals()["machine"] = lambda slug: {"slug": slug, "name": "試験機"}
        got = collect("t1", ["ceiling", "at"], fetch=_fake)
        t("★★取れなかった出典も理由つきで残す★★（黙って消さない）",
          got["sources"]["dmm-ptown"]["state"] == "HEALTHY_NO_MATCH")
        req = as_request(got)
        t("★★依頼文にURLと原文が入る★★",
          "https://a.example/1" in req and "999G" in req)
        t("★★依頼文にこちらの判断を書かない★★（AIを引っぱらない）",
          "採用" not in req.split("## お願い")[1].split("###")[0]
          .replace("採用してください", ""))
        t("　原文に無い数字を補うなと明記している", "補わないでください" in req)
    finally:
        globals()["machine"] = keep
    print(f"\n{ran[0]}/{ran[0]} 合格" if ok else "\n不合格あり")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="出典の原文を集める（判断はしない）")
    ap.add_argument("--slug")
    ap.add_argument("--name", default="",
                    help="正式名称（machines.jsonにまだ無い新台のとき）")
    ap.add_argument("--topic", action="append",
                    help=f"既定は全部。選べるもの: {', '.join(TOPICS)}")
    ap.add_argument("--out", help="依頼文の書き出し先（.md）")
    ap.add_argument("--json", help="生データの書き出し先（.json）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.slug:
        ap.print_help()
        return 0
    topics = a.topic or list(TOPICS)
    for t in topics:
        if t not in TOPICS:
            print(f"★知らない話題です: {t}★（選べるもの: {', '.join(TOPICS)}）")
            return 1
    got = collect(a.slug, topics, name=a.name)
    n = sum(len((v.get(t) or {}).get("quotes") or [])
            for v in got["sources"].values() for t in topics)
    for dir_id, r in got["sources"].items():
        # ★取得できなかったものを「使えた」ように見せない★（Codex143回目）
        if not r.get("url") or r.get("error"):
            print(f"  {dir_id}: 使えません（{r.get('state') or r.get('error')}）")
            continue
        parts = []
        for t in topics:
            g = r.get(t) or {}
            parts.append(f"{TOPICS[t]['jp']}{len(g.get('quotes') or [])}件"
                         + ("★打ち切り★" if g.get("truncated") else ""))
        print(f"  {dir_id}: " + " / ".join(parts))
    # ★「2つ揃ったか」を決めるのは発行者の数★（ページ数ではない・依頼125）
    print(f"使える出典: {got.get('usable_sources')} 件"
          f" ／ ★独立した発行者: {got.get('usable_lineages')} 社★"
          + ("（★1社しかありません＝2つ一致は作れません★）"
             if (got.get('usable_lineages') or 0) < 2 else ""))
    if got.get("lineage_unknown"):
        print("  ※発行者を引けないので票に数えなかったもの: "
              + ", ".join(str(u) for u in got["lineage_unknown"]))
    print(f"合計 {n} 件の原文を集めました")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(got, f, ensure_ascii=False, indent=1)
        print("生データ:", a.json)
    if a.out:
        with open(a.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(as_request(got))
        print("依頼文:", a.out)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except _sj.SafeJsonError as e:
        print(f"★入力データが読めません: {e}★")
        raise SystemExit(1)

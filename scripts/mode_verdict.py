# -*- coding: utf-8 -*-
"""★モード・ゾーンについて、2AIが決めたことを控える★（2026-09-02・台帳#523の②）

★なぜ `confirmed_values` と別にするか★
  あちらは「値」を控える口で、★出典の逐語引用を必ず求める★。
  ここで決めるのは次の2つで、**片方は引用できない**。

    HAS            … モード・ゾーンがある      → 引用できる
    NONE_CONFIRMED … 無いことを確認できた      → ★引用できない★
                     （存在しないものは引用できない）

  だから「無い」は、引用の代わりに
  ★「全部読めた」という記録（証拠の集合の指紋）★で支える。

★UNKNOWN は控えない★（Codexのレビュー34）＝
  「まだ分からない」は結論ではないので、記録に残さず毎回聞き直す。

★★機械が確かめること★★（意味は判定しない）
  1. 状態が HAS / NONE_CONFIRMED のどちらか
  2. ★証拠の集合が「全部読めた」状態★（欠けた材料で決めさせない）
  3. 判断者が2つ以上（別々の名前）
  4. 理由が書いてある（15字以上）
  5. HAS のときは★引用が1件以上あり、その逐語が本当に本文に在る★
  6. NONE_CONFIRMED のときは★引用を受け取らない★
     （引用できないはずのものが付いていたら、何かがおかしい）
  7. ★指紋が変わったら、その控えは使わない★
     （下位ページが1本増えただけで「無い」は覆るため）

★意味の判断は2AIの仕事★＝
  「そのモードは狙い方を変えるのか」「演出だけか」は、ここでは決めない。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                            # noqa: BLE001
    pass

import local_paths as _lp                                    # noqa: E402
import page_corpus as _pc                                     # noqa: E402
import source_lineage as _sl                                  # noqa: E402

STORE = _lp.doc("mode_verdicts.json")
KINDS = ("mode", "zone")
STATES = ("HAS", "NONE_CONFIRMED")
# ★★この2人がそろっていること★★（2026-09-02・Codexのレビュー35）
#   ★直す前は「違う文字列が2つ」だけ★だったので、
#   `judges="claude"` を渡すと**1文字ずつ6人**として通った（実際に再現）。
REQUIRED_JUDGES = ("claude", "codex")
# ★★「無い」と書くには独立2出典★★（2026-09-02・運営者の判断「2サイトだね」）
#   ★「ある」は引用できるので1つでよい★／
#   ★「無い」は引用できないので、1サイトの書き落としを見抜けない★。
MIN_SOURCES_FOR_NONE = 2
MIN_WHY = 15
MIN_QUOTE = 6
SCHEMA = "mode-verdict/v3"


def record_fp(v: dict) -> str:
    """★控えの中身そのものの指紋★（2026-09-02・Codexのレビュー36の重大③）

    ★直す前は「判断者が消えた」「引用が空」しか捕まえられなかった★＝
    ★控えの有効な形を保った改変★（引用を別の文字列に／表のセルを別の内容に）は
    素通りし、★正しい形の誤った表★が公開候補になった。
    """
    import hashlib as _h
    body = json.dumps({
        "slug": v.get("slug"), "kind": v.get("kind"), "state": v.get("state"),
        "machine_url": v.get("machine_url"), "corpus_fp": v.get("corpus_fp"),
        "decisions": v.get("decisions"), "quotes": v.get("quotes"),
        "table": v.get("table"),
    }, ensure_ascii=False, sort_keys=True)
    return "sha256:" + _h.sha256(body.encode("utf-8")).hexdigest()


def _read() -> dict:
    """控えを読む。★「無い」と「読めない」を分ける★（罠の常連）。"""
    if not os.path.isfile(STORE):
        return {}
    with open(STORE, encoding="utf-8") as f:
        got = json.load(f)          # ★壊れていたら例外で止める★
    if not isinstance(got, dict):
        raise ValueError("控えの形が違います（いちばん外側が辞書ではない）")
    return got


def _key(slug: str, kind: str) -> str:
    return f"{slug}::{kind}"


def agreed_state(decisions):
    """★2AIの答えから、機械が一致を導く★（2026-09-02・Codexのレビュー35）

    decisions … [{"judge": "claude", "state": "HAS", "why": "…"}, …]
    返り: (状態, 問題)  … 一致していなければ状態は空

    ★直す前は、呼び出し側が集約済みの答えを渡せた★＝一致は自己申告だった。
    """
    if not isinstance(decisions, (list, tuple)):
        return "", "判断は並びで渡してください（文字列は受け取りません）"
    seen = {}
    for d in decisions:
        if not isinstance(d, dict):
            return "", "判断の1件が辞書ではありません"
        j = str(d.get("judge") or "").strip().lower()
        st = str(d.get("state") or "").strip()
        why = str(d.get("why") or "").strip()
        if not j:
            return "", "判断者の名前が空です"
        if st not in STATES:
            return "", (f"{j} の答えが違います（{st}）"
                        "＝HAS か NONE_CONFIRMED／★UNKNOWN は控えません★")
        if len(why) < MIN_WHY:
            return "", f"{j} の理由が短すぎます（{MIN_WHY}字以上）"
        if j in seen and seen[j] != st:
            return "", f"{j} が2つの違う答えを出しています"
        seen[j] = st
    missing = [x for x in REQUIRED_JUDGES if x not in seen]
    if missing:
        return "", f"判断者が足りません（要る: {'/'.join(missing)}）"
    states = set(seen.values())
    if len(states) != 1:
        # ★一致しなければ結論にしない★（UNKNOWN のまま聞き直す）
        return "", f"2AIの答えが分かれています（{sorted(states)}）"
    return states.pop(), ""


def check(kind: str, machine_url: str, decisions, corpus: dict, pages: dict,
          quotes=None) -> tuple:
    """★控えてよいか★だけを判断する（書き込まない）。

    返り: (状態, 問題)  … 問題が空でなければ控えない。

    ★機種の取り違えを防ぐ★＝証拠束の本体URLを必ず渡させ、
      それが集合に在ることを確かめる（Codexのレビュー35）。
    ★偽の指紋を受け取らない★＝本文から計算し直して突き合わせる。
      ★本文は保存しない★（その場で確かめるだけ）。
    """
    if kind not in KINDS:
        return "", f"種類が違います（{kind}）＝mode か zone"
    state, bad = agreed_state(decisions)
    if bad:
        return "", bad
    if not isinstance(corpus, dict) or not corpus.get("complete"):
        return "", ("★証拠の集合が「全部読めた」状態ではありません★"
                    "＝欠けた材料で決めさせない")
    if not isinstance(pages, dict) or not pages:
        return "", "本文が渡されていません（指紋を確かめられません）"

    # ★★偽の指紋を受け取らない★★（2026-09-02・Codexのレビュー35）
    #   ★直す前は {"complete": True, "fp": "x"} だけで「無い」が通った★
    want = _pc.manifest(pages, True, corpus.get("gone"))
    if want["fp"] != corpus.get("fp"):
        return "", "証拠の集合の指紋が、渡された本文と合いません"

    # ★★機種の取り違えを防ぐ★★
    root = _pc._norm(str(machine_url or ""))
    if not root:
        return "", "証拠束の本体URLが渡されていません"
    if root not in set(want["urls"]):
        return "", f"本体URLが証拠の集合にありません（{root[:80]}）"

    for q in (quotes or []):
        if not isinstance(q, dict):
            return "", "引用の1件が辞書ではありません"
        u, text = _pc._norm(str(q.get("url") or "")), str(q.get("quote") or "")
        if len(text.strip()) < MIN_QUOTE:
            return "", f"引用が短すぎます（{MIN_QUOTE}字以上）"
        hit = [k for k in pages if _pc._norm(k) == u]
        if not hit:
            return "", f"引用元が証拠の集合にありません（{u[:80]}）"
        if text not in str(pages.get(hit[0]) or ""):
            return "", f"引用がそのページに在りません（{text[:40]}）"

    if state == "HAS" and not (quotes or []):
        return "", "★「ある」と言うなら、引用が1件以上要ります★"
    if state == "NONE_CONFIRMED":
        # ★★「無い」は引用できないので、独立2出典を求める★★
        #   （2026-09-02・運営者の判断「2サイトだね」）
        #   ★1サイトが書き落としていたら、それを「無い」と読んでしまう★。
        #   ★数えるのは source_lineage.independent() だけ★（監査39）
        n = _sl.independent(_pc.publishers(want["urls"]))
        if n < MIN_SOURCES_FOR_NONE:
            return "", (f"★「無い」と書くには独立した出典が "
                        f"{MIN_SOURCES_FOR_NONE} 件要ります（いま {n} 件）★"
                        "／★引用できないぶん、読み落としを2つ目で確かめる★")
    # ★「無い」に引用は要らないが、あれば受け取る★（Codexのレビュー35）
    #   ＝「モードはありません」と**書いてある**なら、それは根拠になる。
    return state, ""


def record(slug: str, kind: str, machine_url: str, decisions,
           corpus: dict, pages: dict, quotes=None, table=None) -> dict:
    """★機械が確かめてから控える★（1つでも通らなければ書かない）。"""
    if not str(slug or "").strip():
        raise ValueError("機種が空です")
    state, bad = check(kind, machine_url, decisions, corpus, pages, quotes)
    if bad:
        raise ValueError(bad)
    got = _read()
    got[_key(slug, kind)] = {
        "schema": SCHEMA,
        "slug": slug,
        "kind": kind,
        "state": state,
        "machine_url": _pc._norm(str(machine_url)),
        "corpus_fp": corpus.get("fp"),
        "corpus_urls": list(corpus.get("urls") or []),
        "corpus_gone": list(corpus.get("gone") or []),
        # ★各AIの答えをそのまま残す★＝一致は機械が導き直せる
        "decisions": [{"judge": str(d.get("judge")).strip().lower(),
                       "state": str(d.get("state")).strip(),
                       "why": str(d.get("why")).strip()}
                      for d in decisions],
        "quotes": list(quotes or []),
        "table": table or None,
    }
    # ★控え自身の指紋★（保存後の書き換えを見つける）
    got[_key(slug, kind)]["fp"] = record_fp(got[_key(slug, kind)])
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(got, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STORE)
    return got[_key(slug, kind)]


def verdict(slug: str, kind: str, corpus: dict):
    """★いまの証拠に対して、控えを使ってよいか★

    返り: (状態, 控え または 理由)  … 使えなければ ("UNKNOWN", 理由)

    ★★読み直すたびに、控えた内容そのものを検査し直す★★
      （2026-09-02・Codexのレビュー35）＝
      ★直す前は版・状態・指紋しか見ていなかった★ので、
      控えを手で書き換えて判断者や理由を消しても気づけなかった。
    """
    try:
        got = _read()
    except Exception as e:                                   # noqa: BLE001
        return "UNKNOWN", f"控えが読めません（{type(e).__name__}）"
    v = got.get(_key(slug, kind))
    if not isinstance(v, dict):
        return "UNKNOWN", "控えがありません"
    if v.get("schema") != SCHEMA:
        return "UNKNOWN", f"控えの版が違います（{v.get('schema')}）"
    if v.get("slug") != slug or v.get("kind") != kind:
        return "UNKNOWN", "控えの機種・種類が合いません"
    # ★★控えの中身そのものが書き換えられていないか★★
    #   （2026-09-02・Codexのレビュー36の重大③）
    #   ★形を保った改変（引用や表のすり替え）は、これでしか捕まらない★
    if v.get("fp") != record_fp(v):
        return "UNKNOWN", "控えの中身が書き換えられています"
    # ★控えた答えから、いまもう一度一致を導く★（保存後の書き換えを見つける）
    state, bad = agreed_state(v.get("decisions"))
    if bad:
        return "UNKNOWN", f"控えの判断が通りません（{bad}）"
    if state != v.get("state"):
        return "UNKNOWN", "控えの状態と、判断から導いた答えが違います"
    if state == "HAS" and not (v.get("quotes") or []):
        return "UNKNOWN", "「ある」なのに引用がありません"
    saved = {"complete": True, "fp": v.get("corpus_fp"),
             "urls": v.get("corpus_urls"), "gone": v.get("corpus_gone")}
    if not _pc.same_corpus(saved, corpus or {}):
        return "UNKNOWN", "証拠の集合が変わりました（聞き直しが要ります）"
    return state, v


def box_state(mode_state: str, zone_state: str) -> str:
    """★記事の箱をどうするか★（Codexのレビュー34の集約規則）

      どちらかが HAS            → HAS（表を出す）
      両方 NONE_CONFIRMED       → NONE_CONFIRMED（「ありません」）
      それ以外                  → UNKNOWN（★欄ごと出さない★）

    ★これが無いと★「モードは無いが、狙い方が変わるゾーンはある機種」を
    『モード・ゾーンなし』と誤表示する。
    """
    a, b = str(mode_state or ""), str(zone_state or "")
    if a == "HAS" or b == "HAS":
        return "HAS"
    if a == "NONE_CONFIRMED" and b == "NONE_CONFIRMED":
        return "NONE_CONFIRMED"
    return "UNKNOWN"


def selftest() -> int:
    import tempfile
    global STORE
    keep, STORE = STORE, os.path.join(tempfile.mkdtemp(prefix="mv_"),
                                      "mode_verdicts.json")
    ok, cases = 0, []

    def t(name, cond):
        nonlocal ok
        cases.append(name)
        if cond:
            ok += 1
        print(("✅" if cond else "❌") + " " + name)

    U = "https://nana-press.com/kaiseki/machine/644/"
    S = U + "1/"
    PAGES = {U: "天国モードは次回天井が短縮されます。", S: "ゾーンの話です。"}
    C = _pc.manifest(PAGES, True)
    # ★★2サイト目★★（2026-09-02・運営者の判断「2サイトだね」）
    #   ★「無い」は引用できないので、1サイトの書き落としを見抜けない★
    V = "https://chonborista.com/slot/sammy-slot/12345/"
    PAGES2 = {**PAGES, V: "ちょんぼりすた側の本文です。"}
    C2S = _pc.manifest(PAGES2, True)
    Q = [{"url": U, "quote": "天国モードは次回天井が短縮されます。"}]

    def dec(state, judges=("claude", "codex"),
            why="全ページを読んだうえで判断しました"):
        return [{"judge": j, "state": state, "why": why} for j in judges]

    def refused(reason: str, word: str) -> bool:
        """★断ったことだけでなく、★狙った理由で断ったか★まで見る★

        （2026-09-02・罠㉚／今日この試験自体が罠④を踏んだ）＝
        理由が短いなどの**別の検査**に助けられていないかを確かめる。
        """
        return bool(reason) and word in reason

    try:
        # ── ★Codexが挙げた穴★ ──
        t("★★判断者を文字列で渡しても通さない★★"
          "／★直す前は「claude」を1文字ずつ6人として数えた★",
          refused(agreed_state("claude")[1], "並びで"))
        t("★claude と codex の両方が要る★",
          refused(agreed_state(dec("HAS", ("claude", "gemini")))[1],
                  "足りません"))
        t("★2AIの答えが分かれたら結論にしない★",
          agreed_state([{"judge": "claude", "state": "HAS",
                         "why": "全ページを読んだうえで判断しました"},
                        {"judge": "codex", "state": "NONE_CONFIRMED",
                         "why": "全ページを読んだうえで判断しました"}])[1]
          != "")
        t("★同じAIが違う答えを2つ出したら通さない★",
          agreed_state([{"judge": "claude", "state": "HAS",
                         "why": "全ページを読んだうえで判断しました"},
                        {"judge": "claude", "state": "NONE_CONFIRMED",
                         "why": "全ページを読んだうえで判断しました"},
                        {"judge": "codex", "state": "HAS",
                         "why": "全ページを読んだうえで判断しました"}])[1] != "")
        t("★UNKNOWN は控えない★", agreed_state(dec("UNKNOWN"))[1] != "")
        t("★理由が短ければ通さない★",
          agreed_state(dec("HAS", why="短い"))[1] != "")
        t("　そろっていれば答えを導く", agreed_state(dec("HAS")) == ("HAS", ""))

        t("★★偽の指紋では通さない★★"
          "／★直す前は complete と fp を書くだけで「無い」が通った★",
          refused(check("mode", U, dec("NONE_CONFIRMED"),
                        {"complete": True, "fp": "sha256:x"}, PAGES)[1],
                  "指紋"))
        t("★本文が渡されなければ通さない★",
          refused(check("mode", U, dec("NONE_CONFIRMED"), C, None)[1],
                  "本文"))
        t("★★本体URLが証拠の集合に無ければ通さない★★"
          "／★これが無いと、機種Aの証拠で機種Bを記録できる★",
          refused(check("mode", "https://example.com/x/",
                        dec("NONE_CONFIRMED"), C, PAGES)[1], "本体URL"))
        t("★読めていない証拠では通さない★",
          refused(check("mode", U, dec("HAS"),
                        _pc.manifest(PAGES, False), PAGES, Q)[1],
                  "全部読めた"))
        t("★引用がそのページに無ければ通さない★",
          refused(check("mode", U, dec("HAS"), C, PAGES,
                        [{"url": U, "quote": "書かれていない文です"}])[1],
                  "在りません"))
        t("★引用元が証拠の集合の外なら通さない★",
          check("mode", U, dec("HAS"), C, PAGES,
                [{"url": "https://example.com/", "quote": "あいうえおか"}])[1]
          != "")
        t("★「ある」のに引用が無ければ通さない★",
          refused(check("mode", U, dec("HAS"), C, PAGES, [])[1],
                  "引用が1件以上"))
        t("　短すぎる引用は根拠にしない",
          check("mode", U, dec("HAS"), C, PAGES,
                [{"url": U, "quote": "天国"}])[1] != "")
        t("　種類が違えば通さない",
          check("speed", U, dec("HAS"), C, PAGES, Q)[1] != "")

        # ── 通る側 ──
        t("　「ある」は引用つきで通る",
          check("mode", U, dec("HAS"), C, PAGES, Q) == ("HAS", ""))
        t("★★「無い」は1サイトでは通さない★★"
          "／★引用できないぶん、読み落としを2つ目で確かめる★"
          "（MIN_SOURCES_FOR_NONE を満たすまで断る）",
          refused(check("mode", U, dec("NONE_CONFIRMED"), C, PAGES)[1],
                  "独立した出典"))
        t("　「無い」は2サイトそろえば引用なしで通る",
          check("mode", U, dec("NONE_CONFIRMED"), C2S, PAGES2)
          == ("NONE_CONFIRMED", ""))
        t("★「無い」でも、書いてある否定文は引用として受け取る★（Codexの助言）",
          check("mode", U, dec("NONE_CONFIRMED"), C2S, PAGES2,
                [{"url": S, "quote": "ゾーンの話です。"}])[0]
          == "NONE_CONFIRMED")
        t("★「ある」は1サイトでも通る★（その記述を引用できるから）",
          check("mode", U, dec("HAS"), C, PAGES, Q) == ("HAS", ""))
        t("　同じサイトの下位ページを何本足しても2件にはならない",
          refused(check("mode", U, dec("NONE_CONFIRMED"),
                        _pc.manifest({**PAGES, U + "2/": "あ", U + "3/": "い"},
                                     True),
                        {**PAGES, U + "2/": "あ", U + "3/": "い"})[1],
                  "独立した出典"))

        # ── 控えて、読み直す ──
        record("hokuto", "mode", U, dec("HAS"), C, PAGES, Q,
               table={"rows": [["天国", "…", "…"]]})
        st, v = verdict("hokuto", "mode", C)
        t("　控えたものを読み直せる", st == "HAS" and v["table"] is not None)

        C2 = _pc.manifest({**PAGES, U + "2/": "新しい記事"}, True)
        t("★★下位ページが1本増えたら、控えを使わない★★",
          verdict("hokuto", "mode", C2)[0] == "UNKNOWN")

        # ★★控えを手で書き換えても気づく★★（Codexのレビュー35の⑤）
        raw = json.load(open(STORE, encoding="utf-8"))
        raw["hokuto::mode"]["decisions"] = [
            {"judge": "claude", "state": "HAS", "why": "全ページを読んだうえで判断しました"}]
        json.dump(raw, open(STORE, "w", encoding="utf-8"), ensure_ascii=False)
        t("★★控えから判断者を1人消したら、使わない★★"
          "／★直す前は版・状態・指紋しか見ていなかった★",
          verdict("hokuto", "mode", C)[0] == "UNKNOWN")

        raw["hokuto::mode"]["decisions"] = dec("NONE_CONFIRMED")
        json.dump(raw, open(STORE, "w", encoding="utf-8"), ensure_ascii=False)
        t("★控えの状態と、判断から導いた答えが違えば使わない★",
          verdict("hokuto", "mode", C)[0] == "UNKNOWN")

        raw["hokuto::mode"]["decisions"] = dec("HAS")
        raw["hokuto::mode"]["quotes"] = []
        json.dump(raw, open(STORE, "w", encoding="utf-8"), ensure_ascii=False)
        t("★控えから引用を消したら、「ある」を使わない★",
          verdict("hokuto", "mode", C)[0] == "UNKNOWN")

        t("　控えが無ければ UNKNOWN",
          verdict("nothing", "mode", C)[0] == "UNKNOWN")

        # ★★控えの中身をすり替えても気づく★★
        #   （2026-09-02・Codexのレビュー36の重大③）
        #   ★形を保った改変は、控え自身の指紋でしか捕まらない★
        record("swap", "mode", U, dec("HAS"), C, PAGES, Q,
               table={"headers": ["モード"], "rows": [["天国"]]})
        t("　控えたものはそのまま読める", verdict("swap", "mode", C)[0] == "HAS")
        raw = json.load(open(STORE, encoding="utf-8"))
        raw["swap::mode"]["table"] = {"headers": ["モード"],
                                      "rows": [["★すり替えた★"]]}
        json.dump(raw, open(STORE, "w", encoding="utf-8"), ensure_ascii=False)
        t("★★控えの中身をすり替えたら使わない★★"
          "／★これが無いと、正しい形の誤った表が公開候補になる★",
          verdict("swap", "mode", C)[0] == "UNKNOWN")

        raw["swap::mode"]["table"] = {"headers": ["モード"], "rows": [["天国"]]}
        raw["swap::mode"]["quotes"] = [{"url": U, "quote": "別の引用に差し替え"}]
        json.dump(raw, open(STORE, "w", encoding="utf-8"), ensure_ascii=False)
        t("★引用をすり替えても使わない★",
          verdict("swap", "mode", C)[0] == "UNKNOWN")

        # ── 箱の状態 ──
        t("★どちらかが「ある」なら表を出す★",
          box_state("HAS", "UNKNOWN") == "HAS"
          and box_state("UNKNOWN", "HAS") == "HAS")
        t("★両方「無い」ときだけ「ありません」と書く★",
          box_state("NONE_CONFIRMED", "NONE_CONFIRMED") == "NONE_CONFIRMED")
        t("★★片方だけ「無い」では「ありません」と書かない★★"
          "／★これが無いと『モードは無いがゾーンはある機種』を誤表示する★",
          box_state("NONE_CONFIRMED", "UNKNOWN") == "UNKNOWN")
        t("　どちらも分からなければ欄ごと出さない",
          box_state("UNKNOWN", "UNKNOWN") == "UNKNOWN")
        t("　空でも落ちない", box_state("", None) == "UNKNOWN")
    finally:
        STORE = keep

    print(f"\n{ok}/{len(cases)} 合格")
    return 0 if ok == len(cases) else 1


def apply_file(path: str, read_pages=None) -> int:
    """★2AIが書いた決定ファイルのとおりに控える★（1つでも通らなければ書かない）

    決定ファイル（JSON）:
      slug / kind / machine_url / manifest（mode_ask が出したパス）/
      decisions / quotes / table

    ★本文は決定ファイルに書かせない★＝
      `mode_ask` が出した写しから読み直す。
      ★2AIが「本文にこう書いてある」と言い張れないようにする★。
    """
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if not isinstance(d, dict):
        print("★決定ファイルの形が違います★")
        return 1
    need = ("slug", "kind", "machine_url", "manifest", "decisions")
    miss = [k for k in need if not d.get(k)]
    if miss:
        print(f"★決定ファイルに足りないものがあります★（{miss}）")
        return 1

    mp = str(d["manifest"])
    if not os.path.isfile(mp):
        print(f"★証拠の記録が見つかりません★（{mp}）")
        return 1
    with open(mp, encoding="utf-8") as f:
        man = json.load(f)
    corpus = man.get("manifest") if isinstance(man, dict) else None
    if not isinstance(corpus, dict):
        print("★証拠の記録の形が違います★")
        return 1
    # ★★どの機種の証拠束かを照合する★★（Codexのレビュー36の重大②）
    #   ★直す前は、機種Aの証拠束で slug だけ機種Bにして控えられた★
    if man.get("slug") != d["slug"]:
        print(f"★証拠束は別の機種のものです★"
              f"（証拠束: {man.get('slug')} ／ 決定: {d['slug']}）")
        return 1
    roots = [str(x) for x in (man.get("roots") or [])]
    if not roots:
        print("★証拠束に本体URLの記録がありません★")
        return 1
    if _pc._norm(str(d["machine_url"])) not in {_pc._norm(x) for x in roots}:
        # ★下位URLを本体として渡させない★
        print(f"★本体URLが証拠束の本体一覧にありません★（{d['machine_url']}）")
        return 1

    # ★本文は写しから読み直す★（決定ファイルの言い分を信じない）
    reader = read_pages or _read_corpus_file
    pages, why = reader(mp)
    if why:
        print(f"★本文を読めません★（{why}）")
        return 1

    try:
        got = record(d["slug"], d["kind"], d["machine_url"], d["decisions"],
                     corpus, pages, d.get("quotes"), d.get("table"))
    except ValueError as e:                                  # noqa: BLE001
        print(f"★控えませんでした★ {e}")
        return 1
    print(f"控えました: {d['slug']} / {d['kind']} → {got['state']}")
    return 0


def _read_corpus_file(manifest_path: str):
    """★mode_ask が書いた本文の写しを読み直す★（{URL: 本文} に戻す）。

    ★分け方は書いた側の関数を使う★（2026-09-02）＝
      同じ規則を2か所に書くと必ずずれる（実際にずれて、
      「指紋が合いません」で何も控えられなかった）。
    """
    import mode_ask as _ma
    p = manifest_path.replace("_manifest.json", "_corpus.txt")
    if not os.path.isfile(p):
        return {}, f"本文の写しがありません（{p}）"
    with open(p, encoding="utf-8") as f:
        pages = _ma.load_pages(f.read())
    if not pages:
        return {}, "本文の写しが空です"
    return pages, ""


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="モード・ゾーンの判断を控える")
    ap.add_argument("--apply", metavar="決定ファイル",
                    help="2AIが書いた決定ファイルのとおりに控える")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        raise SystemExit(selftest())
    if a.apply:
        raise SystemExit(apply_file(a.apply))
    print("使い方: python scripts/mode_verdict.py --apply <決定ファイル>"
          " ／ --selftest")

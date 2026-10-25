"""機種ページ以外のページ（ガイド・トップ・固定ページ・ランキングの説明文）の文を、2AIが決めたとおりに直す

★なぜ要るか★（2026-10-25・運営者の指示「SEOのために作ったページも整合性を保ち、自動更新の対象にする」）＝
  ガイドに手書きした機種の狙い目が記事と食い違ったまま残っていた（北斗「等価550G〜」、
  ToLOVEる「リセット後70G〜」など）。朝の更新タスクが1日1ページずつ2AIで読み、この道具で直す。

★意味の判断は2AI★（鉄則0z）。機械が守るのは「言うだけでは通らないこと」だけ：
  ①直す前の文（before）が、そのファイルにちょうど1回ある（逐語）
  ②直した後の文に、直す前に無い数字を足さない（★数字を作らない★）
    ＝機種の数字は「データの印」（build_hub_pages.MARK_PAGES の <span data-dv=...>）で入れる。
      印の中身は数えない（生成器がデータから埋める）。
    ＝数字を直したいのに印にできないもの（天井など）は、数字を消して機種記事へ案内する。
  ③判断者は claude と codex の組（代役は claude-agent-a と claude-agent-b）・理由15字以上
  ④直せるのは決めたページだけ
  ⑤書いたあと build_hub_pages --legacy が通らなければ、元に戻す（読めない印を残さない）

使い方:
  python scripts/fix_static_text.py --file <決定ファイル.json>            # 確かめるだけ
  python scripts/fix_static_text.py --file <決定ファイル.json> --apply    # 書く
  python scripts/fix_static_text.py --selftest

決定ファイル:
  {"page": "guide-rate.html",
   "edits": [{"before": "…", "after": "…"}],
   "by": ["claude", "codex"],
   "why": "…（15字以上）"}

★書いたあとにやること（呼ぶ側）★＝service-worker.js の版を上げる／
  python scripts/build_pages_artifact.py --approve／before-commit。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import html as _html
import io
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ★直せるページ★＝機種ページ以外で、検索から人が入ってくるもの
PAGES = ("index.html", "about.html", "contact.html", "privacy.html", "404.html",
         "guide-haena.html", "guide-rate.html", "guide-reset.html",
         "guide-yamedoki.html", "guide-pochipochi.html",
         "scripts/hub_prose.json")

# ★1日1ページずつ読む順番★（朝の更新タスク STEP 1-S）。ランキング4ページは生成物なので、
#   直す先は scripts/hub_prose.json（表の中身は機種データから作られる）。
ROTATION = ("index.html", "guide-tenjo-ranking.html", "guide-reset-ranking.html",
            "guide-suru-tenjo.html", "guide-ichiran.html", "guide-haena.html", "guide-rate.html",
            "guide-reset.html", "guide-yamedoki.html", "guide-pochipochi.html",
            "about.html", "privacy.html", "contact.html", "404.html")
HUB_PAGES = ("guide-tenjo-ranking.html", "guide-reset-ranking.html",
             "guide-suru-tenjo.html", "guide-ichiran.html")
STATE_KEY = "static_review"

_MARK = re.compile(r'<span data-dv="([^"]*)">(.*?)</span>', re.S)
_NUM = re.compile(r"\d+(?:[.,]\d+)*")


def _log(msg: str) -> None:
    try:
        import local_paths
        d = local_paths.LOGS
        os.makedirs(d, exist_ok=True)
        now = _dt.datetime.now()
        with io.open(os.path.join(d, f"fix_static_text_{now:%Y-%m-%d}.log"), "a", encoding="utf-8") as f:
            f.write(f"[{now:%Y/%m/%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass


def visible_numbers(text: str, mark_ok=None) -> set:
    """★読者に見える文字の中の数字★（タグ・属性は数えない）

    ★印の中身を数えないのは、生成器が本当に埋める印だけ★（2026-10-25・Codex review248）＝
      mark_ok(鍵) が真のときだけ外す。直す前はどの印でも外していたので、
      `<span data-dv="">12345G</span>` や印を埋めないページ（about.html）の印で数字を作れた。
    """
    ok = mark_ok or (lambda key: False)
    t = _MARK.sub(lambda m: " " if ok(m.group(1)) else m.group(2), text)
    t = re.sub(r"<[^>]+>", " ", t)
    t = _html.unescape(t)
    import unicodedata
    t = unicodedata.normalize("NFKC", t)
    return set(_NUM.findall(t))


def problems(dec: dict, read) -> list:
    """★決定ファイルを確かめる★（書かない）。read(相対パス) → 中身"""
    out = []
    if not isinstance(dec, dict):
        return ["決定ファイルが辞書ではありません"]
    page = dec.get("page")
    if page not in PAGES:
        return [f"直せるページではありません: {page}"]
    from confirmed_values import judges_pair, JUDGES_LABEL
    if judges_pair(dec.get("by") or []) is None:
        out.append(f"判断者は {JUDGES_LABEL} が要ります")
    why = str(dec.get("why") or "").strip()
    if len(why) < 15:
        out.append("理由（why）は15字以上で書いてください")
    edits = dec.get("edits")
    if not isinstance(edits, list) or not edits:
        out.append("edits がありません")
        return out
    src = read(page)
    mark_ok = _mark_checker(page)
    spans = []
    for i, e in enumerate(edits, 1):
        if not isinstance(e, dict) or not isinstance(e.get("before"), str) \
                or not isinstance(e.get("after"), str) or not e["before"]:
            out.append(f"{i}件目: before と after を文字で書いてください")
            continue
        n = src.count(e["before"])
        if n != 1:
            out.append(f"{i}件目: 直す前の文がページに {n} 回あります（ちょうど1回のときだけ直せます）")
        else:
            st = src.index(e["before"])
            spans.append((st, st + len(e["before"]), i))
        added = visible_numbers(e["after"], mark_ok) - visible_numbers(e["before"], mark_ok)
        if added:
            out.append(f"{i}件目: 直す前に無い数字を足しています {sorted(added)}"
                       "（機種の数字はデータの印で入れる／入れられないものは消して記事へ案内する）")
    # ★直す場所は元の文章の上で決める★（Codex review248）＝順に replace すると、
    #   前の直しで生まれた文字を後の直しが書き換えて、指定と違う場所が変わる。
    spans.sort()
    for (a0, a1, ai), (b0, b1, bi) in zip(spans, spans[1:]):
        if b0 < a1:
            out.append(f"{ai}件目と{bi}件目の直す範囲が重なっています")
    return out


def _mark_checker(page):
    """★そのページで生成器が埋める印か★（埋めないページでは常に偽）"""
    import build_hub_pages as _bh
    if page not in _bh.MARK_PAGES:
        return lambda key: False
    import safe_json as _sj
    by = {m.get("slug"): m for m in _sj.read_rows(_bh.MACHINES) if isinstance(m, dict)}

    def ok(key):
        try:
            _bh._mark_text(key, by)
            return True
        except Exception:                                   # noqa: BLE001
            return False
    return ok


def _state_path():
    import local_paths
    return os.path.join(local_paths.DOCS, "state.json")


def _load_state() -> dict:
    p = _state_path()
    if not os.path.exists(p):
        return {}
    with io.open(p, encoding="utf-8") as f:
        return json.load(f)


def next_page(state: dict) -> str:
    """★読んだ日がいちばん古いページ（読んだことの無いページが先）★"""
    seen = (state.get(STATE_KEY) or {}) if isinstance(state, dict) else {}
    return min(ROTATION, key=lambda pg: (seen.get(pg) or "", ROTATION.index(pg)))


def mark_reviewed(page: str, day: str) -> None:
    """★両AIの照合が済み、直しがあれば公開まで済んだときだけ呼ぶ★（Codex review247）"""
    if page not in ROTATION:
        raise ValueError(f"読む順番に無いページです: {page}")
    p = _state_path()
    st = _load_state()
    st.setdefault(STATE_KEY, {})[page] = day
    tmp = p + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    _log(f"読み終わった: {page} {day}")


def apply_edits(src: str, edits: list) -> str:
    """★元の文章の上の位置で直す★（後ろから当てる・重なりは problems が断る）"""
    pos = sorted(((src.index(e["before"]), e) for e in edits), key=lambda x: -x[0])
    for st, e in pos:
        src = src[:st] + e["after"] + src[st + len(e["before"]):]
    return src


def _read(rel):
    with io.open(os.path.join(BASE, rel), encoding="utf-8") as f:
        return f.read()


def run(path: str, do_apply: bool) -> int:
    with io.open(path, encoding="utf-8") as f:
        dec = json.load(f)
    ng = problems(dec, _read)
    page = dec.get("page") if isinstance(dec, dict) else None
    if ng:
        for x in ng:
            print("★" + x + "★")
        _log(f"断った: {page} / {ng}")
        return 1
    src = _read(page)
    new = apply_edits(src, dec["edits"])
    if page.endswith(".json"):
        try:
            json.loads(new)
        except ValueError as e:
            print(f"★直したあと JSON として読めません: {e}★")
            _log(f"断った: {page} / JSON 不正")
            return 1
    if not do_apply:
        print(f"確かめました（書いていません）: {page} {len(dec['edits'])}件")
        return 0
    full = os.path.join(BASE, page)
    with io.open(full, "w", encoding="utf-8", newline="\n") as f:
        f.write(new)
    r = subprocess.run([sys.executable, os.path.join(HERE, "build_hub_pages.py"), "--legacy"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        with io.open(full, "w", encoding="utf-8", newline="\n") as f:
            f.write(src)                               # ★読めない印を残さない★
        print("★作り直しが通らないので元に戻しました★\n" + (r.stdout or "")[-800:])
        _log(f"戻した: {page} / 作り直しが通らない")
        return 1
    print(f"直しました: {page} {len(dec['edits'])}件（判断者 {dec['by']}）")
    _log(f"直した: {page} {len(dec['edits'])}件 by={dec['by']} why={dec['why']}")
    return 0


def selftest() -> int:
    ok = 0
    cases = []

    def t(name, cond):
        nonlocal ok
        cases.append(name)
        if cond:
            ok += 1
        print(("✅" if cond else "❌") + " " + name)

    def P(dec, rd_):
        """★落ちたら「例外」として返す★（守りを外したときに試験が❌を出せるように）"""
        try:
            return problems(dec, rd_)
        except Exception as e:                       # noqa: BLE001
            return [f"例外 {type(e).__name__}"]

    page = "<p>北斗は等価550G〜です。</p><p>天井は1268Gです。</p>"
    rd = lambda rel: page  # noqa: E731
    good = {"page": "guide-rate.html", "by": ["claude", "codex"],
            "why": "選べない等価の線を、データの印に置き換えるため",
            "edits": [{"before": "等価550G〜です。",
                       "after": '<span data-dv="target:hokuto:eq56:normal">570G〜</span>です。'}]}
    t("★印に置き換えるのは通る（印の中身の数字は数えない）★", problems(good, rd) == [])
    fake = dict(good, edits=[{"before": "等価550G〜です。",
                              "after": '<span data-dv="">12345G</span>です。'}])
    t("★空や読めない印の中の数字は数える（数字を作れない）★",
      any("数字を足して" in x for x in problems(fake, rd)))
    t("★印を埋めないページの印は数える★",
      any("数字を足して" in x for x in problems(dict(good, page="about.html"), rd)))
    over = dict(good, edits=[{"before": "<p>北斗は", "after": "<p>北斗は"},
                             {"before": "北斗は等価", "after": "北斗は等価"}])
    t("★直す範囲が重なれば断る★", any("重なって" in x for x in problems(over, rd)))
    t("★直す場所は元の文章の上で決める★",
      apply_edits("A B", [{"before": "A", "after": "B C"}, {"before": "B", "after": "D"}]) == "B C D")
    bad = dict(good, edits=[{"before": "等価550G〜です。", "after": "5.6枚570G〜です。"}])
    t("★直す前に無い数字を足すのは断る（数字を作らない）★",
      any("数字を足して" in x for x in problems(bad, rd)))
    drop = dict(good, edits=[{"before": "天井は1268Gです。", "after": "天井は機種ページで確かめてください。"}])
    t("数字を消して記事へ案内するのは通る", problems(drop, rd) == [])
    t("★直す前の文がページに無ければ断る★",
      any("0 回" in x for x in P(dict(good, edits=[{"before": "無い文", "after": "x"}]), rd)))
    t("★同じ文が2回あれば断る★",
      any("2 回" in x for x in P(dict(good, edits=[{"before": "<p>", "after": "<p>"}]), rd)))
    t("★判断者が1人なら断る★", any("判断者" in x for x in problems(dict(good, by=["claude"]), rd)))
    t("代役の組は通る", problems(dict(good, by=["claude-agent-a", "claude-agent-b"]), rd) == [])
    t("理由が短ければ断る", any("理由" in x for x in problems(dict(good, why="短い"), rd)))
    t("★決めたページ以外は断る★",
      any("直せるページ" in x for x in problems(dict(good, page="machine.html"), rd)))
    t("全角の数字も数える", visible_numbers("５５０Ｇ") == {"550"})
    t("属性の中の数字は数えない", visible_numbers('<a href="x2.html">a</a>') == set())
    t("書き換えは1回だけ", apply_edits("aXb", [{"before": "a", "after": "b"}]) == "bXb")
    t("★読む順番：読んだことの無いページが先★",
      next_page({STATE_KEY: {pg: "2026-10-01" for pg in ROTATION if pg != "about.html"}}) == "about.html")
    t("★読む順番：読んだ日がいちばん古いページ★",
      next_page({STATE_KEY: {pg: ("2026-09-01" if pg == "guide-rate.html" else "2026-10-01")
                             for pg in ROTATION}}) == "guide-rate.html")
    t("読む順番：控えが無ければ先頭から", next_page({}) == ROTATION[0])
    t("ランキングの説明文は hub_prose.json で直す", "scripts/hub_prose.json" in PAGES
      and all(h in ROTATION for h in HUB_PAGES))
    print(f"\n{ok}/{len(cases)} 合格")
    return 0 if ok == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--next", action="store_true", help="今日読むページを出す")
    ap.add_argument("--reviewed", help="読み終わったページを記録する（照合と公開が済んでから）")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.next:
        pg = next_page(_load_state())
        print(pg)
        print("（直す先: scripts/hub_prose.json）" if pg in HUB_PAGES else f"（直す先: {pg}）")
        return 0
    if a.reviewed:
        mark_reviewed(a.reviewed, _dt.date.today().isoformat())
        print(f"記録しました: {a.reviewed}")
        return 0
    if not a.file:
        print("★--file が要ります★")
        return 1
    return run(a.file, a.apply)


if __name__ == "__main__":
    sys.exit(main())

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
PAGES = ("index.html", "about.html", "contact.html", "privacy.html", "terms.html", "404.html",
         "guide-haena.html", "guide-rate.html", "guide-reset.html",
         "guide-yamedoki.html", "guide-pochipochi.html", "guide-site.html",
         "scripts/hub_prose.json",
         # ★ポチポチくんの画面★（2026-11-10・運営者「更新されないページ他にもないの？」）＝説明文と手書きの注記を
         #   2AIが読み直す道が無かった。★自動の区画（POCHI-AUTO）の中は直さない★（pochipochi.py の持ち物＝run が断る）
         "setting.html")

# ★1日1ページずつ読む順番★（朝の更新タスク STEP 1-S）。ランキング4ページは生成物なので、
#   直す先は scripts/hub_prose.json（表の中身は機種データから作られる）。
ROTATION = ("index.html", "guide-tenjo-ranking.html", "guide-reset-ranking.html",
            "guide-reset-ev-ranking.html",
            "guide-suru-tenjo.html", "guide-ichiran.html", "guide-haena.html", "guide-rate.html",
            "guide-reset.html", "guide-yamedoki.html", "guide-pochipochi.html", "guide-site.html",
            "about.html", "privacy.html", "terms.html", "contact.html", "404.html", "setting.html")
HUB_PAGES = ("guide-tenjo-ranking.html", "guide-reset-ranking.html",
             "guide-reset-ev-ranking.html",
             "guide-suru-tenjo.html", "guide-ichiran.html")
STATE_KEY = "static_review"
# ★★読み終われなかったページは、数日うしろへ回す★★（2026-11-10・Codexの3回目の洗い出し）＝
#   直す前は読んだ日がいちばん古いページを毎回選び、1ページが片づかないと後ろのページが何日たっても読まれなかった
TRIED_KEY = "static_review_tried"
FIRST_TRIED_KEY = "static_review_first_tried"   # ★まだ読み終えていないページを、初めて試した日★（Codexの指摘）
TRIED_SKIP_DAYS = 3
# ★巡回が止まっていないか（見張り番）★＝いちばん古いページの読んだ日が、これだけ前なら🟡（18ページ×2周分）
STALE_DAYS = 40
STALL_DAYS = 5

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


def next_page(state: dict, today: str = None) -> str:
    """★読んだ日がいちばん古いページ（読んだことの無いページが先）★

    ★読み終われなかったページ（--tried・TRIED_SKIP_DAYS 日以内・そのあと読み終えていない）は後ろへ回す★。
    全部が回されていれば、いつもどおり読んだ日がいちばん古いページ。
    """
    seen = (state.get(STATE_KEY) or {}) if isinstance(state, dict) else {}
    tried = (state.get(TRIED_KEY) or {}) if isinstance(state, dict) else {}
    today = today or _dt.date.today().isoformat()
    cut = (_dt.date.fromisoformat(today) - _dt.timedelta(days=TRIED_SKIP_DAYS)).isoformat()

    def _held(pg):
        t = tried.get(pg) or ""
        return bool(t) and t > cut and t > (seen.get(pg) or "")
    pool = [pg for pg in ROTATION if not _held(pg)] or list(ROTATION)
    return min(pool, key=lambda pg: (seen.get(pg) or "", ROTATION.index(pg)))


def mark_tried(page: str, day: str) -> None:
    """★読み終われなかった（照合が割れた・公開できなかった）ページを数日うしろへ回す★"""
    if page not in ROTATION:
        raise ValueError(f"読む順番に無いページです: {page}")
    p = _state_path()
    st = _load_state()
    st.setdefault(TRIED_KEY, {})[page] = day
    st.setdefault(FIRST_TRIED_KEY, {}).setdefault(page, day)
    tmp = p + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    _log(f"読み終われなかった（{TRIED_SKIP_DAYS}日うしろへ回す）: {page} {day}")


def stale_problem(state: dict, today: str = None):
    """★見張り番：読んでいないページ、または STALE_DAYS 日より前に読んだきりのページがあれば文★"""
    seen = (state.get(STATE_KEY) or {}) if isinstance(state, dict) else {}
    today = today or _dt.date.today().isoformat()
    cut = (_dt.date.fromisoformat(today) - _dt.timedelta(days=STALE_DAYS)).isoformat()
    # ★まだ読んでいないページは先に読まれる★ので数えない（足したばかりのページで鳴らさない）。
    #   代わりに「いちばん新しく読んだ日」が STALL_DAYS 日より前なら、巡回そのものが止まっている
    last = max([d for d in seen.values() if d] or [""])
    stall_cut = (_dt.date.fromisoformat(today) - _dt.timedelta(days=STALL_DAYS)).isoformat()
    if not last or last < stall_cut:
        return f"固定ページの読み直しが{STALL_DAYS}日以上進んでいません（最後に読んだ日: {last or 'なし'}）"
    first = (state.get(FIRST_TRIED_KEY) or {}) if isinstance(state, dict) else {}
    old = [pg for pg in ROTATION if seen.get(pg) and seen[pg] <= cut]   # ★40日目で知らせる★（Codexの指摘）
    # ★★一度も読み終えていないページも、初めて試した日から数える★★（2026-11-10・Codexの指摘＝
    #   同じページだけ毎回失敗し続けても、読んだ日が無いので古さの検査から外れていた）
    old += [pg for pg in ROTATION if not seen.get(pg) and (first.get(pg) or "9999") <= cut]
    if old:
        return (f"固定ページの読み直しが止まっています（{STALE_DAYS}日以上読んでいないページ {len(old)}件: "
                + "・".join(old[:5]) + "）")
    return None


def mark_reviewed(page: str, day: str) -> None:
    """★両AIの照合が済み、直しがあれば公開まで済んだときだけ呼ぶ★（Codex review247）"""
    if page not in ROTATION:
        raise ValueError(f"読む順番に無いページです: {page}")
    p = _state_path()
    st = _load_state()
    st.setdefault(STATE_KEY, {})[page] = day
    (st.get(FIRST_TRIED_KEY) or {}).pop(page, None)
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


def setting_guard(page: str, new: str, store: dict = None) -> list:
    """★ポチポチくんの画面を直したとき、自動の区画（控えから作る表）がそろったままか★（そろわなければ理由）"""
    if page != "setting.html":
        return []
    import pochipochi as _pp
    return _pp.check_problems(new, store)


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
    _sg = setting_guard(page, new)
    if _sg:
        print("★ポチポチくんの自動の区画は直せません（pochipochi.py で直す）★: " + " ／ ".join(_sg)[:300])
        _log(f"断った: {page} / 自動の区画")
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
    _sv = {pg: "2026-10-01" for pg in ROTATION}
    _sv["about.html"] = "2026-09-01"
    t("★★読み終われなかったページは3日うしろへ回し、次に古いページを読む★★（直す前は1ページで巡回が止まった）",
      next_page({STATE_KEY: _sv, TRIED_KEY: {"about.html": "2026-11-09"}}, "2026-11-10") != "about.html"
      and next_page({STATE_KEY: _sv, TRIED_KEY: {"about.html": "2026-11-09"}}, "2026-11-13") == "about.html")
    t("　読み終えたあとの古い「読み終われなかった」記録では回さない",
      next_page({STATE_KEY: dict(_sv, **{"about.html": "2026-09-01"}),
                 TRIED_KEY: {"about.html": "2026-08-01"}}, "2026-11-10") == "about.html")
    t("★見張り番：40日以上読んでいないページがあれば知らせる★",
      stale_problem({STATE_KEY: dict(_sv, **{"index.html": "2026-11-09"})}, "2026-11-10") is not None
      and stale_problem({STATE_KEY: {pg: "2026-11-01" for pg in ROTATION}}, "2026-11-04") is None)
    t("★★一度も読み終えていないページも、初めて試した日から40日で知らせる★★（Codexの指摘）",
      stale_problem({STATE_KEY: {pg: "2026-11-08" for pg in ROTATION if pg != "about.html"},
                     FIRST_TRIED_KEY: {"about.html": "2026-09-20"}}, "2026-11-10") is not None
      and stale_problem({STATE_KEY: {pg: "2026-11-08" for pg in ROTATION if pg != "about.html"},
                         FIRST_TRIED_KEY: {"about.html": "2026-11-01"}}, "2026-11-10") is None)
    t("　ちょうど40日目で知らせる（39日目は知らせない）",
      stale_problem({STATE_KEY: {pg: "2026-11-08" for pg in ROTATION if pg != "about.html"},
                     FIRST_TRIED_KEY: {"about.html": "2026-10-01"}}, "2026-11-10") is not None
      and stale_problem({STATE_KEY: {pg: "2026-11-08" for pg in ROTATION if pg != "about.html"},
                         FIRST_TRIED_KEY: {"about.html": "2026-10-02"}}, "2026-11-10") is None)
    t("　まだ読んでいないページだけなら鳴らさない（先に読まれる）／巡回が5日進まなければ鳴らす",
      stale_problem({STATE_KEY: {pg: "2026-11-08" for pg in ROTATION if pg != "about.html"}}, "2026-11-10") is None
      and stale_problem({STATE_KEY: {pg: "2026-11-01" for pg in ROTATION}}, "2026-11-10") is not None)
    t("ランキングの説明文は hub_prose.json で直す", "scripts/hub_prose.json" in PAGES
      and all(h in ROTATION for h in HUB_PAGES))
    # ★★ポチポチくんの画面も読み直す★★（2026-11-10＝直す前は巡回に入っていなかった）
    t("★★ポチポチくんの画面（setting.html）も直せる・読む順番に入っている★★",
      "setting.html" in PAGES and "setting.html" in ROTATION)
    import pochipochi as _pp
    _h = ("const MACHINE_CONFIGS = {\n" + _pp.BEGIN + "\n" + _pp.END + "\n};\n<p>説明の文です。</p>\n")
    _st = _pp.empty()
    _st["configs"]["zzz_a"] = {"settings": [1, 6], "fields": [{"id": "c", "label": "チェリー", "sub": "", "rateKey": "c"}],
                               "rates": {"c": {"1": "1/200.0", "6": "1/150.0"}}}
    _h = _pp.build(_h, _st)
    t("　自動の区画の外の説明文を直すのは通る",
      setting_guard("setting.html", _h.replace("説明の文です。", "説明の文を直しました。"), _st) == [])
    t("★★自動の区画（控えから作る表）を直していたら断る★★",
      bool(setting_guard("setting.html", _h.replace("1/200.0", "1/199.0"), _st)))
    t("　（配線）書く前に setting_guard を通している",
      "_sg = setting_guard(page, new)" in open(__file__, encoding="utf-8").read().split("def selftest", 1)[0])
    print(f"\n{ok}/{len(cases)} 合格")
    return 0 if ok == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--next", action="store_true", help="今日読むページを出す")
    ap.add_argument("--reviewed", help="読み終わったページを記録する（照合と公開が済んでから）")
    ap.add_argument("--tried", help="読み終われなかったページを記録する（3日うしろへ回す）")
    ap.add_argument("--check", action="store_true", help="見張り番：読み直しが止まっていないか")
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
    if a.tried:
        mark_tried(a.tried, _dt.date.today().isoformat())
        print(f"うしろへ回しました: {a.tried}")
        return 0
    if a.check:
        try:
            pr = stale_problem(_load_state())
        except Exception as e:                                   # noqa: BLE001
            print(f"🟠 読み直しの控えを読めません: {type(e).__name__}: {e}")
            return 2
        print(("🟡 " + pr) if pr else "🟢 固定ページの読み直しは回っています")
        return 1 if pr else 0
    if not a.file:
        print("★--file が要ります★")
        return 1
    return run(a.file, a.apply)


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""popular_machines.py — ★人気機種の順位を、DMMの人気順から取る★

★なぜ要るか（2026-08-29・運営者の指示）★
  更新タスクは「人気機種を優先して直す」ことになっているが、
  その順番（`machines.json` の並び）は★手で並べ替えるしかなく、
  最後に並べ替えたのは2026-05-04＝約4か月前★だった。
  ＝もう打たれていない機種を優先し続け、いま人気の機種が後回しになる。

  ★運営者の言葉★＝「いやいや　自動化しないと意味ないじゃん」

★どこから取るか★
  https://p-town.dmm.com/machines/popularity/slot （DMMぱちタウンの人気順）
  ・並びは `sort=pv_desc`＝★DMM上で見られている順★
  ・★DMMは規約を確認済みの許可先★（automation-policy）
  ・各機種に `/machines/<機種ID>` のリンクがある
    ＝★機種IDで結び付けられる★ので、名前の文字合わせに頼らない

★★名前の辞書を作らない★★（プロジェクトの決まり）
  DMMとうちどころで機種名の書き方が違う（実測4件）＝
    Lパチスロ からくりサーカス2 ／ Lからくりサーカス2
    スロット ソードアート・オンラインⅡ ／ スマスロ ソードアート・オンラインⅡ
  ★接頭辞の辞書を作ると、出るたびに増える★ので作らない。
  ①機種IDの控えで引く（一度結び付ければ永続。DMMのIDは変わらない）
  ②控えに無ければ、機種名が★完全に一致★するときだけ結び付けて控える
  ③どちらでも決まらなければ★2AIへの質問として出す★（機械は決めない）

★取れなかったら並べ替えない★（fail-closed）
  順番が壊れるより、古いままのほうがまし。

使い方:
    python scripts/popular_machines.py              # 下見（書き込まない）
    python scripts/popular_machines.py --apply      # 控えと一覧を書く
    python scripts/popular_machines.py --selftest
"""
from __future__ import annotations
import argparse
import html.parser
import io
import json
import os
import re
import sys
import unicodedata

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                        # noqa: BLE001
    pass

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import local_paths as _lp                                # noqa: E402
import safe_json as _sj                                  # noqa: E402

MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
# ★控えはリポジトリの外★（公開物ではないため）
STORE = _lp.doc("popular_machines.json")
SCHEMA = "popular-machines/v1"
URL = "https://p-town.dmm.com/machines/popularity/slot"
# ★人気枠に入れる数★（運営者の指示・2026-08-29）
TOP_N = 20


class PopularError(Exception):
    """人気順を読めないときの合図。★黙って古い順番を使わない★"""


class _Parser(html.parser.HTMLParser):
    """★HTML解析で読む★（正規表現で読まない＝プロジェクトの決まり）"""

    def __init__(self):
        super().__init__()
        self.rows: list = []
        self._mid = None
        self._buf: list = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "a":
            m = re.fullmatch(r"/machines/(\d+)", str(d.get("href") or ""))
            self._mid = m.group(1) if m else None
            self._buf = []
        elif tag == "img" and self._mid and d.get("alt"):
            self._buf.append(str(d["alt"]))

    def handle_data(self, data):
        if self._mid:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._mid:
            self.rows.append((self._mid, "".join(self._buf)))
            self._mid, self._buf = None, []


def _clean_name(raw: str) -> str:
    """★リンクの中の文字から機種名だけを取り出す★

    1〜10位は「機種名 ＋ N位 機種名 メーカー 機械割…」と説明が続き、
    11〜20位は「N位 機種名」だけ。★順位の印を目印に切る★。
    """
    t = re.sub(r"\s+", " ", str(raw or "")).strip()
    t = re.sub(r"^\s*\d+位\s*", "", t)          # 先頭の「N位」を落とす
    return re.split(r"\s*\d+位\s*", t)[0].strip()


def parse(html_text: str) -> list:
    """人気順のページから (順位, 機種ID, 機種名) を取り出す。"""
    p = _Parser()
    p.feed(str(html_text or ""))
    out, seen = [], set()
    for mid, raw in p.rows:
        if mid in seen:
            continue
        name = _clean_name(raw)
        if not name:
            continue                            # 画像だけのリンクは飛ばす
        seen.add(mid)
        out.append((len(out) + 1, mid, name))
    return out


def norm(s) -> str:
    """★見た目の揺れだけをそろえる★（意味は変えない）"""
    s = unicodedata.normalize("NFKC", str(s or ""))
    return re.sub(r"[\s　・･\-ー―‐/／()（）【】\[\]]", "", s).lower()


def load_store() -> dict:
    """機種ID → うちどころのslug の控え。★読めなければ空★"""
    if not os.path.isfile(STORE):
        return {"schema_version": SCHEMA, "by_dmm_id": {}}
    try:
        got = _sj.read_json(STORE, expect=dict)
    except Exception:                                    # noqa: BLE001
        return {"schema_version": SCHEMA, "by_dmm_id": {}}
    if str(got.get("schema_version") or "") != SCHEMA:
        return {"schema_version": SCHEMA, "by_dmm_id": {}}
    if not isinstance(got.get("by_dmm_id"), dict):
        got["by_dmm_id"] = {}
    return got


def resolve(top: list, machines: list, store: dict) -> dict:
    """人気順を、うちどころのslugへ結び付ける。

    ★返すもの★
      ranked    … 順位順のslug（うちどころに記事があるものだけ）
      learned   … 新しく結び付いた {機種ID: slug}（控えへ足す分）
      questions … 決められなかったもの（★2AIへ回す★）
    """
    by_id, by_name, slugs = {}, {}, set()
    for m in machines:
        if not isinstance(m, dict) or not m.get("slug"):
            continue
        slugs.add(m["slug"])
        u = str((m.get("identity") or {}).get("official_product_url") or "")
        g = re.search(r"/machines/(\d+)", u)
        if g:
            by_id[g.group(1)] = m["slug"]
        for nm in [m.get("name")] + list(m.get("aliases") or []):
            if not nm:
                continue
            # ★★同じ見た目が「別の機種」にあれば、名前では決めない★★
            #   ★同じ機種の中で名前と別名がぶつかるのは曖昧ではない★
            #   （実機で判明：真打 吉宗 は name「真打 吉宗」と
            #     alias「真打吉宗」が同じ形になり、
            #     正しく結び付くはずの機種が毎回2AIへの質問になっていた）
            k = norm(nm)
            if k in by_name and by_name[k] not in (None, m["slug"]):
                by_name[k] = None          # ★別の機種とぶつかった★
            elif k not in by_name:
                by_name[k] = m["slug"]

    kept = {k: v for k, v in (store.get("by_dmm_id") or {}).items()
            if v in slugs}                    # ★消えた機種の控えは使わない★
    ranked, learned, questions = [], {}, []
    for rank, mid, name in top:
        slug = kept.get(mid) or by_id.get(mid)
        if not slug:
            hit = by_name.get(norm(name))
            if hit:
                slug, learned[mid] = hit, hit
        if slug:
            if slug not in ranked:
                ranked.append(slug)
            continue
        questions.append({
            "dmm_id": mid, "rank": rank, "name": name,
            "text": ("★この機種が、うちどころのどの記事にあたるか判断してください★"
                     f"／DMMの人気{rank}位「{name}」"
                     f"（https://p-town.dmm.com/machines/{mid}）"
                     "／★記事が無ければ『無し』と答えてください★"
                     "／★機械は名前の書き方の違いを吸収しません★"
                     "（辞書を増やさないため）"),
        })
    return {"ranked": ranked, "learned": learned, "questions": questions}


def should_run(today, checked_at: str = "") -> tuple:
    """★今日、人気順を取り直すか★（2026-08-29・運営者の指示）

    ★返すもの★＝(取るか, 理由)

    ★決まり★＝月曜、または前回から7日以上経っていたら取る。
      ★月曜だけにしない★＝その日に通信が失敗すると
      ★丸1週間古いまま★になる。追いつけるようにしておく。
      ★同じ日に2回は取らない★（すでに今日取っていれば何もしない）。
    """
    import datetime as _dt
    if isinstance(today, str):
        today = _dt.date.fromisoformat(today)
    got = str(checked_at or "").strip()
    if not got:
        return True, "まだ一度も取っていません"
    try:
        last = _dt.date.fromisoformat(got)
    except ValueError:
        return True, f"前回の日付を読めません（{got!r}）"
    if last == today:
        return False, "今日はもう取っています"
    days = (today - last).days
    if days >= 7:
        return True, f"前回から{days}日たっています"
    if today.weekday() == 0:              # 0 = 月曜
        return True, "月曜です"
    return False, f"前回から{days}日（月曜でも7日でもありません）"


def fetch(fetcher=None) -> str:
    """★用途を名乗ってから取りに行く★（名簿を通る唯一の口）"""
    if fetcher is not None:
        return fetcher(URL)
    import new_machine_watch as _nw
    with _nw.fetching("popularity_rank"):
        return _nw._get(URL)


def run(apply_it: bool = False, fetcher=None, machines=None) -> dict:
    """人気順を取って、うちどころのslugへ並べ直す。"""
    rows = machines
    if rows is None:
        rows = _sj.read_json(MACHINES, expect=list)
    try:
        page = fetch(fetcher)
    except Exception as e:                               # noqa: BLE001
        raise PopularError(f"人気順を取れません: {type(e).__name__}: {e}")
    top = parse(page)
    if len(top) < TOP_N:
        # ★少なければ並べ替えない★（ページの作りが変わった合図）
        raise PopularError(
            f"人気順が {len(top)} 件しか取れません（{TOP_N} 件を期待）")
    top = top[:TOP_N]
    store = load_store()
    got = resolve(top, rows, store)
    got["checked_at"] = None                  # 書くときに入れる
    if apply_it:
        import datetime as _dt
        store["by_dmm_id"].update(got["learned"])
        store["ranked"] = got["ranked"]
        store["checked_at"] = _dt.date.today().isoformat()
        store["source"] = URL
        io.open(STORE, "w", encoding="utf-8", newline="\n").write(
            json.dumps(store, ensure_ascii=False, indent=1) + "\n")
        got["checked_at"] = store["checked_at"]
    return got


def popular_slugs() -> list:
    """★いまの人気機種（順位順）★（控えが無ければ空＝人気枠を使わない）"""
    store = load_store()
    got = store.get("ranked")
    return [s for s in got if isinstance(s, str)] if isinstance(got, list) \
        else []


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("OK   " if cond else "NG   ") + name)

    _PAGE = (
        '<a href="/machines/100"><img alt="L 東京喰種"> 1位 L 東京喰種 '
        'スパイキー 機械割: 97.5% 2位 別の機種</a>'
        '<a href="/machines/200">11位 Lからくりサーカス2 12位 次の機種</a>'
        '<a href="/machines/300">12位 知らない機種</a>'
        '<a href="/machines/100">重複するリンク</a>')
    got = parse(_PAGE)
    t("★★順位・機種ID・機種名を取り出せる★★",
      [(r, m, n) for r, m, n in got]
      == [(1, "100", "L 東京喰種"), (2, "200", "Lからくりサーカス2"),
          (3, "300", "知らない機種")])
    t("　★同じ機種が2回出ても1回だけ数える★", len(got) == 3)

    _MS = [{"slug": "tokyo_ghoul", "name": "L 東京喰種"},
           {"slug": "karakuri2", "name": "Lからくりサーカス2"},
           {"slug": "dmm_x", "name": "別名の機種",
            "identity": {"official_product_url":
                         "https://p-town.dmm.com/machines/300"}}]
    r = resolve(got, _MS, {"by_dmm_id": {}})
    t("★★人気順にslugが並ぶ★★",
      r["ranked"] == ["tokyo_ghoul", "karakuri2", "dmm_x"])
    t("　★機種IDで結び付いたものは、名前を見ない★",
      "300" not in r["learned"])
    t("　★名前が完全に一致したものは控えへ★",
      r["learned"] == {"100": "tokyo_ghoul", "200": "karakuri2"})

    r2 = resolve([(1, "999", "うちどころに無い機種")], _MS, {"by_dmm_id": {}})
    t("★★決められないものは2AIへの質問にする★★"
      "／★機械が名前の書き方の違いを埋めない（辞書を増やさない）★",
      r2["ranked"] == [] and len(r2["questions"]) == 1
      and "999" in r2["questions"][0]["text"])

    _DUP = [{"slug": "a", "name": "同じ名前"}, {"slug": "b", "name": "同じ名前"}]
    r3 = resolve([(1, "1", "同じ名前")], _DUP, {"by_dmm_id": {}})
    t("★★同じ見た目が2機種にあれば、名前では決めない★★",
      r3["ranked"] == [] and len(r3["questions"]) == 1)
    # ★★同じ機種の中で名前と別名がぶつかるのは、曖昧ではない★★
    #   （実機で判明：真打 吉宗 は name「真打 吉宗」と alias「真打吉宗」が
    #     同じ形になり、★正しく結び付く機種が毎回質問になっていた★）
    _SELF = [{"slug": "shinuchi", "name": "真打 吉宗",
              "aliases": ["真打吉宗", "しんうち"]}]
    r5 = resolve([(1, "1", "真打 吉宗")], _SELF, {"by_dmm_id": {}})
    t("★★同じ機種の中で名前と別名がぶつかっても、結び付く★★"
      "／★これが無いと、正しい機種が毎回2AIへの質問になる★",
      r5["ranked"] == ["shinuchi"] and r5["questions"] == [])

    r4 = resolve(got, _MS, {"by_dmm_id": {"100": "消えた機種"}})
    t("★★控えが指すslugが無くなっていたら、その控えは使わない★★",
      "tokyo_ghoul" in r4["ranked"])

    _raised = False
    try:
        run(fetcher=lambda u: '<a href="/machines/1">1位 ひとつだけ</a>',
            machines=_MS)
    except PopularError as e:
        _raised = "件しか取れません" in str(e)
    t("★★20件そろわなければ並べ替えない★★"
      "／★順番が壊れるより、古いままのほうがまし★", _raised)

    _raised2 = False
    try:
        def _boom(u):
            raise OSError("ためしの通信失敗")
        run(fetcher=_boom, machines=_MS)
    except PopularError:
        _raised2 = True
    t("★★取れなかったら並べ替えない★★", _raised2)

    # ★★週1回（月曜）だけ取る★★（2026-08-29・運営者の指示）
    t("　まだ一度も取っていなければ取る", should_run("2026-08-29", "")[0])
    t("★★月曜なら取る★★", should_run("2026-08-31", "2026-08-29")[0])
    t("　月曜でなく、前回から日が浅ければ取らない",
      should_run("2026-08-30", "2026-08-29")[0] is False)
    t("★★月曜に失敗しても、7日たてば追いつく★★"
      "／★月曜だけにすると、その日に失敗して丸1週間古いままになる★",
      should_run("2026-09-06", "2026-08-30")[0])
    t("　同じ日に2回は取らない",
      should_run("2026-09-01", "2026-09-01")[0] is False)
    t("　前回の日付が壊れていたら取る（古いまま使わない）",
      should_run("2026-08-29", "こわれています")[0])

    import inspect as _insp
    t("★★用途を名乗ってから取りに行く★★"
      "／★名乗らないと名簿の関所を通らない★",
      'fetching("popularity_rank")' in _insp.getsource(fetch))

    ng = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="人気機種の順位をDMMから取る")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--weekly", action="store_true",
                    help="月曜（または前回から7日以上）のときだけ取る")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.weekly:
        import datetime as _dt
        ok, why = should_run(_dt.date.today(),
                             str(load_store().get("checked_at") or ""))
        if not ok:
            print(f"今日は取りません（{why}）")
            return 0
        print(f"取ります（{why}）")
    try:
        got = run(apply_it=a.apply)
    except PopularError as e:
        print(f"★{e}★（並べ替えません）")
        return 1
    print(f"人気機種: {len(got['ranked'])} / {TOP_N} 件が"
          "うちどころにあります")
    for n, s in enumerate(got["ranked"], 1):
        print(f"  {n:>2}  {s}")
    if got["learned"]:
        print(f"新しく結び付けました: {got['learned']}")
    for q in got["questions"]:
        print("  ★2AIに聞くこと: " + q["text"][:170])
    if not a.apply:
        print("★下見です★（--apply で控えに書きます）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

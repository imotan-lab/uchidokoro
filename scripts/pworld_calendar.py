# -*- coding: utf-8 -*-
"""pworld_calendar.py — P-WORLDの導入カレンダーから新台を見つける。

★これ一つを正とする★（2026-08-12・運営者決定／正本＝
  _design/new_machine_discovery_2026-08-12.md）
  それまではメーカー公式11社の一覧を見張っていたが、
  「P-WORLDの導入カレンダー一つを正とする。公式サイトは忘れていい」に変えた。

取れるもの（★ここで取るのは「身元」と「いつ」だけ★）
  機種ID / 機種名 / メーカー名 / 導入予定日

★機械は値を読み取らない★
  天井・機械割などの中身は 2AI が原文を読んで決める（設計の正本を参照）。
  このファイルは「どの機種を、いつ記事にするか」を出すところまで。

★取りこぼしを黙って通さない★
  ページ自身が「全3機種 パチンコ2機種 / パチスロ1機種」と件数を書いている。
  読み取った数がそれと合わなければ**例外で止める**（0件を「今日は新台なし」と
  取り違えない）。ページの作りが変わったら、静かに止まるのではなく気づける。

使い方:
  python scripts/pworld_calendar.py --list            # これから出る新台
  python scripts/pworld_calendar.py --list --before   # 過去1か月分
  python scripts/pworld_calendar.py --json
  python scripts/pworld_calendar.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE_URL = "https://www.p-world.co.jp/database/machine/introduce_calendar.cgi"
MACHINE_URL = "https://www.p-world.co.jp/machine/database/%s"

# ★スロットだけを見る★（パチンコは扱わない）
SLOT_TYPE = "パチスロ"


class CalendarError(RuntimeError):
    """カレンダーを読めなかった（★0件と区別する★）。"""


class _Reader(HTMLParser):
    """導入カレンダーを読む。

    ★正規表現で切らない★（2026-08-12）
      HTMLは入れ子で、属性の書き方も変わる。文字の並びで切ると、
      少し変わっただけで黙って0件になる。タグとして読む。
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.days: list = []          # [{"date": "2026-08-17", "said": {...}, "items": [...]}]
        self._day = None
        self._item = None
        self._field = None            # いま中身を集めている項目名
        self._buf: list = []

    # -------------------------------------------------- 助け
    @staticmethod
    def _cls(attrs: dict) -> set:
        return set((attrs.get("class") or "").split())

    def _flush(self) -> str:
        s = "".join(self._buf)
        self._buf = []
        return " ".join(s.split())

    # -------------------------------------------------- タグ
    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        cls = self._cls(a)
        if tag == "div" and "machineList" in cls and a.get("data-yyyymmdd"):
            ymd = a["data-yyyymmdd"]
            m = re.match(r"^(\d{4})(\d{2})(\d{2})$", ymd)
            self._day = {"date": f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
                                 if m else "", "raw": ymd, "said": {}, "items": []}
            self.days.append(self._day)
            return
        if self._day is None:
            return
        if tag == "p" and "machineList-header-count" in cls:
            self._field, self._buf = "count", []
            return
        if tag == "li" and "machineList-item" in cls:
            self._item = {"type": "", "name": "", "maker": "",
                          "machine_id": "", "ids": set(), "title_id": ""}
            self._day["items"].append(self._item)
            return
        if self._item is None:
            return
        for key, name in (("machineList-item-type", "type"),
                          ("machineList-item-title", "name"),
                          ("machineList-item-maker", "maker")):
            if tag == "p" and key in cls:
                self._field, self._buf = name, []
                return
        if tag == "a":
            m = re.match(r"^/machine/database/(\d+)/?$", a.get("href", ""))
            if m:
                # ★行の中の機種リンクを全部覚える★（依頼165のP1）
                #   サムネイルがA・題がBのような壊れ方だと、
                #   AのIDにBの名前が付く（同名機なら名前の照合も抜ける）。
                self._item["ids"].add(m.group(1))
                if self._field == "name":
                    self._item["title_id"] = m.group(1)

    def handle_data(self, data):
        if self._field:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "p" and self._field:
            text = self._flush()
            if self._field == "count" and self._day is not None:
                self._day["said"] = _read_count(text)
            elif self._item is not None:
                self._item[self._field] = text
            self._field = None
        elif tag == "li" and self._item is not None:
            self._item = None
        elif tag == "div" and self._field == "count":
            self._field = None


def _read_count(text: str) -> dict:
    """「全3機種 パチンコ2機種 / パチスロ1機種」を読む（★答え合わせ用★）。"""
    out = {}
    for label, key in (("全", "all"), ("パチンコ", "pachi"), ("パチスロ", "slot")):
        m = re.search(re.escape(label) + r"\s*(\d+)\s*機種", text)
        if m:
            out[key] = int(m.group(1))
    return out


def parse(html: str) -> list:
    """カレンダーのHTMLから、スロットの新台を取り出す。

    ★読み取った数がページの申告と合わなければ止める★
      静かに0件を返すと「今日は新台なし」と区別が付かない。
    """
    r = _Reader()
    r.feed(html)
    if not r.days:
        raise CalendarError(
            "カレンダーに日付の区切りが1つもありません"
            "（ページの作りが変わった可能性があります）")
    out = []
    for day in r.days:
        said = day.get("said") or {}
        got_all = len(day["items"])
        got_slot = [x for x in day["items"] if x["type"] == SLOT_TYPE]
        got_pachi = [x for x in day["items"] if x["type"] and x["type"] != SLOT_TYPE]
        # ★件数の申告は必ず在ること★（2026-08-12・依頼165のP0）
        #   読めないときに比べずに通すと、行の読み取りが同時に壊れたとき
        #   **0件を「新台なし」として通してしまう**（狙いと逆）。
        missing = [k for k in ("all", "pachi", "slot") if k not in said]
        if missing:
            raise CalendarError(
                f"{day['date']}: 件数の申告が読めません（{'/'.join(missing)}）"
                "＝ページの作りが変わった可能性があります")
        if said["all"] != said["pachi"] + said["slot"]:
            raise CalendarError(
                f"{day['date']}: 申告の内訳が合いません"
                f"（全{said['all']} ≠ パチンコ{said['pachi']}＋パチスロ{said['slot']}）")
        if said["all"] != got_all:
            raise CalendarError(
                f"{day['date']}: ページは全{said['all']}機種と書いていますが "
                f"{got_all}機種しか読めませんでした（取りこぼし）")
        if said["slot"] != len(got_slot):
            raise CalendarError(
                f"{day['date']}: ページはパチスロ{said['slot']}機種と書いていますが "
                f"{len(got_slot)}機種しか読めませんでした（取りこぼし）")
        if said["pachi"] != len(got_pachi):
            raise CalendarError(
                f"{day['date']}: ページはパチンコ{said['pachi']}機種と書いていますが "
                f"{len(got_pachi)}機種しか読めませんでした（取りこぼし）")
        for it in got_slot:
            if not it["title_id"] or not it["name"]:
                raise CalendarError(
                    f"{day['date']}: 機種IDか機種名が読めない行があります: "
                    f"{it.get('name') or '(名前なし)'}")
            # ★行の中の機種リンクが全部同じIDか★（依頼165のP1）
            if len(it["ids"]) > 1:
                raise CalendarError(
                    f"{day['date']}: 1つの行に複数の機種IDがあります"
                    f"（{'/'.join(sorted(it['ids']))}）＝別機種が混ざっています")
            if not it["maker"]:
                raise CalendarError(
                    f"{day['date']}: メーカーが読めない行があります: {it['name']}")
            out.append({"machine_id": it["title_id"],
                        "name": it["name"],
                        "maker": it["maker"],
                        "release_date": day["date"],
                        "url": MACHINE_URL % it["title_id"]})
    return out


def fetch(before: bool = False) -> str:
    import new_machine_watch as _nw
    url = BASE_URL + ("?mode=before" if before else "")
    return _nw._get(url)


def upcoming(before: bool = False) -> list:
    return parse(fetch(before))


# ★名前を突き合わせるときの整え方★（2026-08-12）
#   P-WORLDと当サイトで、区切りの記号や空白の入れ方が違う。
#   ★これは「同じ機種か」の判定ではない★＝ここで落ちたものを
#   「新台だ」と決めつけない。**候補として出すだけ**で、
#   同じ機種かどうかは 2AI と check_duplicate が確かめる。
_TRIM = re.compile(r"[\s　・‐‑‒–—―ー\-~〜]+")


def _key(name: str) -> str:
    return _TRIM.sub("", str(name or "")).lower()


def without_article(rows: list, machines: list) -> list:
    """記事がまだ無さそうな機種を返す（★候補を出すだけ★）。"""
    have = set()
    for m in machines or []:
        have.add(_key(m.get("name")))
        for a in (m.get("aliases") or []):
            have.add(_key(a))
    return [r for r in rows if _key(r["name"]) not in have]


def _load_machines() -> list:
    import safe_json as _sj
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "assets", "data", "machines.json")
    return _sj.read_json(path, expect=list)


# ---------------------------------------------------------------- selftest
_FIX = """
<div class="machineList js-machineList" data-yyyymmdd="20260817" id="2026-08-17">
  <div class="machineList-header">
    <h2 class="machineList-header-date">2026/08/17&nbsp;新台予定</h2>
    <p class="machineList-header-count">全3機種 パチンコ2機種 / パチスロ1機種</p>
  </div>
  <div class="machineList-body">
  <ul class="machineList-grid machineList-grid--pachi">
    <li class="machineList-item">
      <p class="machineList-item-type">パチンコ</p>
      <p class="machineList-item-thumb"><a href="/machine/database/1"><img alt="P1"></a></p>
      <p class="machineList-item-maker"><a href="/x">メカA</a></p>
      <p class="machineList-item-title"><a href="/machine/database/1">P その1</a></p>
    </li>
    <li class="machineList-item">
      <p class="machineList-item-type">パチンコ</p>
      <p class="machineList-item-thumb"><a href="/machine/database/2"><img alt="P2"></a></p>
      <p class="machineList-item-maker"><a href="/x">メカB</a></p>
      <p class="machineList-item-title"><a href="/machine/database/2">P その2</a></p>
    </li>
  </ul>
  <ul class="machineList-grid machineList-grid--slot">
    <li class="machineList-item">
      <p class="machineList-item-type">パチスロ</p>
      <p class="machineList-item-thumb"><a href="/machine/database/10530"><img alt="S"></a></p>
      <p class="machineList-item-maker"><a href="/x">オーイズミ</a></p>
      <p class="machineList-item-title"><a href="/machine/database/10530">Lパチスロ 喰霊‐零‐Re</a></p>
      <p class="machineList-item-memo">機械割：97.8% ～ 110.0%</p>
    </li>
  </ul>
  </div>
</div>
"""


def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    def raises(fn, word=""):
        try:
            fn()
            return False
        except CalendarError as e:
            return (word in str(e)) if word else True
        except Exception:
            return False

    got = parse(_FIX)
    t("★★スロットの新台だけを取り出す★★（パチンコは混ぜない）",
      len(got) == 1 and got[0]["name"] == "Lパチスロ 喰霊‐零‐Re")
    t("　機種ID・メーカー・導入日が揃う",
      got[0]["machine_id"] == "10530" and got[0]["maker"] == "オーイズミ"
      and got[0]["release_date"] == "2026-08-17")
    t("　機種ページのURLを組み立てる",
      got[0]["url"] == "https://www.p-world.co.jp/machine/database/10530")

    # ★取りこぼしを黙って通さない★
    t("★★申告よりスロットが少なければ止まる★★（0件と取り違えない）",
      raises(lambda: parse(_FIX.replace(
          '<p class="machineList-item-type">パチスロ</p>',
          '<p class="machineList-item-type">パチンコ</p>')), "パチスロ1機種"))
    #   ★行が読めなくなった場合★（申告はそのまま・実物が減る）
    _one_less = _FIX.replace("""    <li class="machineList-item">
      <p class="machineList-item-type">パチンコ</p>
      <p class="machineList-item-thumb"><a href="/machine/database/2"><img alt="P2"></a></p>
      <p class="machineList-item-maker"><a href="/x">メカB</a></p>
      <p class="machineList-item-title"><a href="/machine/database/2">P その2</a></p>
    </li>""", "")
    t("★★申告より実物が少なければ止まる★★（読み取りが壊れたら気づく）",
      raises(lambda: parse(_one_less), "全3機種と書いています"))
    t("★★日付の区切りが無ければ止まる★★（ページの作りが変わった）",
      raises(lambda: parse("<html><body>なにもない</body></html>"), "日付の区切り"))
    t("　機種IDが読めない行があれば止まる",
      raises(lambda: parse(_FIX.replace("/machine/database/10530", "/x/10530")),
             "機種IDか機種名"))

    # ★申告が読めなければ止める★（2026-08-12・依頼165のP0で反転）
    #   以前は「読めたぶんを返す」を正常としていたが、
    #   行の読み取りが同時に壊れると0件を「新台なし」として通してしまう。
    no_count = _FIX.replace(
        '<p class="machineList-header-count">全3機種 パチンコ2機種 / パチスロ1機種</p>', "")
    t("★★件数の申告が読めなければ止まる★★（0件を新台なしと取り違えない）",
      raises(lambda: parse(no_count), "件数の申告が読めません"))
    t("★★申告の内訳が合わなければ止まる★★",
      raises(lambda: parse(_FIX.replace("パチンコ2機種", "パチンコ1機種")), "内訳が合いません"))
    t("★★1つの行に複数の機種IDがあれば止まる★★（サムネイルと題が別機種）",
      raises(lambda: parse(_FIX.replace(
          '<p class="machineList-item-thumb"><a href="/machine/database/10530">'
          '<img alt="S"></a></p>',
          '<p class="machineList-item-thumb"><a href="/machine/database/99999">'
          '<img alt="S"></a></p>')), "複数の機種ID"))
    t("　メーカーが読めない行があれば止まる",
      raises(lambda: parse(_FIX.replace(
          '<p class="machineList-item-maker"><a href="/x">オーイズミ</a></p>', "")),
          "メーカーが読めない"))

    # ★記事があるかの突き合わせ★（★候補を出すだけ・同定はしない★）
    t("　名前が一致する機種は候補から外れる",
      not without_article(got, [{"name": "Lパチスロ 喰霊‐零‐Re"}]))
    t("　通称（aliases）でも外れる",
      not without_article(got, [{"name": "別名", "aliases": ["Lパチスロ喰霊-零-Re"]}]))
    t("　記号や空白の入れ方が違っても外れる",
      not without_article(got, [{"name": "Ｌパチスロ　喰霊 ‐ 零 ‐ Ｒｅ".replace(
          "Ｌ", "L").replace("Ｒ", "R").replace("ｅ", "e")}]))
    t("★★知らない機種は候補として残る★★",
      len(without_article(got, [{"name": "スマスロ北斗の拳"}])) == 1)

    ng = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--todo", action="store_true",
                    help="記事がまだ無さそうな機種だけ（★候補を出すだけ★）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--before", action="store_true", help="過去1か月分を見る")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    rows = upcoming(args.before)
    if args.todo:
        rows = without_article(rows, _load_machines())
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("カレンダーにスロットの新台はありません")
        return 0
    for r in rows:
        print("%s  %-8s %s  （%s）"
              % (r["release_date"], r["maker"], r["name"], r["url"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

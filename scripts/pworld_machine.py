# -*- coding: utf-8 -*-
"""pworld_machine.py — P-WORLDの機種ページを読み、その機種か確かめる。

★機種IDを身元にする★（2026-08-12・運営者決定／正本＝
  _design/new_machine_discovery_2026-08-12.md）
  それまでは**メーカー公式11社**のページで同定していた。
  作りが11通りあるため、証明書切れ・soft404・隠しh1・派生機の紛れ込みと、
  対策が積み上がっていた。P-WORLDは1つの作りなので、同定はここに寄せる。

★ここで読むのは「身元」と「日程」だけ★
  機種名／メーカー／型式名／検定番号／導入開始／種目（パチスロか）。
  ★記事に載る値（天井・機械割・純増…）はここでは採らない★＝
  それらは2AIが原文を読んで決める（confirmed_values へ記録）。

使い方:
  python scripts/pworld_machine.py --id 10513
  python scripts/pworld_machine.py --id 10513 --name "マイジャグラーVI"
  python scripts/pworld_machine.py --selftest
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MACHINE_URL = "https://www.p-world.co.jp/machine/database/%s"
SLOT_TAG = "パチスロ"

# ページが名乗る項目（★この一覧に無いものは読まない★）
#   ★機械割は入れない★（2026-08-12・依頼165のP1）
#   「呼び出し元が使っていないから安全」ではなく、**読み口ごと持たない**。
#   記事に載る値は2AIが原文を読んで決める（confirmed_values へ記録）。
_LABELS = {"メーカー": "maker", "タイプ": "type",
           "検定番号": "shinsa", "型式名": "model_code", "導入開始": "release_text"}

# ★隠れている要素は読まない★（2026-08-12・依頼165のP0）
#   非表示の「パチスロ」を1つ置くだけで、パチンコのページを通せてしまう。
_HIDDEN_STYLE = ("display:none", "display: none", "visibility:hidden",
                 "visibility: hidden")


def _is_hidden(a: dict) -> bool:
    if "hidden" in a:
        return True
    if str(a.get("aria-hidden", "")).lower() == "true":
        return True
    st = str(a.get("style", "")).replace(" ", "").lower()
    return any(x.replace(" ", "") in st for x in _HIDDEN_STYLE)


class MachineError(RuntimeError):
    """機種ページを読めなかった（★空の結果と区別する★）。"""


class _Reader(HTMLParser):
    """機種ページの「機種ラベル」と「機種情報」だけを読む。

    ★正規表現でHTMLを切らない★（少し書き方が変わると黙って空になる）。
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: list = []          # ["パチスロ", "ノーマル"]
        self.rows: list = []          # 機種情報の各行のテキスト
        self.h1: list = []
        self._in = None               # "tag" / "info" / "h1"
        self._depth = 0
        self._buf: list = []
        self._row: list = []
        self._hide = 0            # 隠れている塊の深さ
        self.tag_blocks = 0       # 種目ラベルの塊がいくつ見つかったか
        self.info_blocks = 0      # 機種情報の塊がいくつ見つかったか

    @staticmethod
    def _cls(a: dict) -> set:
        return set((a.get("class") or "").split())

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        cls = self._cls(a)
        # ★隠れている塊はまるごと読まない★（依頼165のP0）
        if self._hide:
            self._hide += 1
            return
        if _is_hidden(a):
            self._hide = 1
            return
        if self._in is None:
            if tag == "p" and "kisyuTag" in cls:
                self._in, self._depth = "tag", 1
                self.tag_blocks += 1
            elif tag == "div" and "kisyuInfo" in cls:
                self._in, self._depth = "info", 1
                self.info_blocks += 1
            elif tag == "h1":
                self._in, self._depth, self._buf = "h1", 1, []
            return
        if tag in ("p", "div", "h1", "table", "tr", "td", "span"):
            self._depth += 1
        if self._in == "tag" and tag == "span":
            self._buf = []
        if self._in == "info" and tag == "tr":
            self._row = []

    def handle_data(self, data):
        if self._hide:
            return
        if self._in:
            self._buf.append(data)
            if self._in == "info":
                self._row.append(data)

    def handle_endtag(self, tag):
        if self._hide:
            self._hide -= 1
            return
        if self._in is None:
            return
        if self._in == "tag" and tag == "span":
            s = " ".join("".join(self._buf).split())
            if s:
                self.tags.append(s)
            self._buf = []
        if self._in == "info" and tag == "tr":
            s = " ".join("".join(self._row).split())
            if s:
                self.rows.append(s)
            self._row = []
        if self._in == "h1" and tag == "h1":
            s = " ".join("".join(self._buf).split())
            if s:
                self.h1.append(s)
        if tag in ("p", "div", "h1", "table", "tr", "td", "span"):
            self._depth -= 1
            if self._depth <= 0:
                self._in, self._buf = None, []


def _split_row(row: str) -> tuple:
    """「メーカー ： 北電子」→ ("maker", "北電子")。知らない見出しは捨てる。"""
    m = re.match(r"^\s*([^:：]{2,8}?)\s*[:：]\s*(.*)$", row)
    if not m:
        return ("", "")
    label = re.sub(r"[\s　]+", "", m.group(1))
    key = _LABELS.get(label, "")
    # ★知らない見出しは値ごと捨てる★（値だけ拾って別の項目に混ぜない）
    return (key, m.group(2).strip()) if key else ("", "")


def _read_date(text: str) -> str:
    """「2026年10月05日」→ "2026-10-05"。日が無ければ月まで。"""
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text or "")
    if m:
        # ★暦として実在するか確かめる★（依頼165のP1。2026年2月31日を通さない）
        try:
            datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return ""
        return "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3)))
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", text or "")
    return "%s-%02d" % (m.group(1), int(m.group(2))) if m else ""


def parse(html: str) -> dict:
    """機種ページから身元と日程を取り出す。★読めなければ止める★"""
    r = _Reader()
    r.feed(html)
    if not r.h1:
        raise MachineError("機種名（h1）が読めません（ページの作りが変わった可能性）")
    if not r.tags:
        raise MachineError("種目のラベル（パチスロ／パチンコ）が読めません")
    if not r.rows:
        raise MachineError("機種情報の表が読めません")
    # ★塊が2つ以上あれば止める★（依頼165のP0）
    #   別機種の表が混ざっていると、先に見つけた方を勝手に採ってしまう。
    if r.tag_blocks > 1 or r.info_blocks > 1:
        raise MachineError(
            f"同じ塊が複数あります（種目{r.tag_blocks}個・機種情報{r.info_blocks}個）"
            "＝別機種の情報が混ざっている可能性")
    if len(r.h1) > 1:
        raise MachineError(f"見出し(h1)が{len(r.h1)}個あります＝どれが機種名か決まりません")
    out = {"name": r.h1[0], "tags": r.tags, "is_slot": SLOT_TAG in r.tags}
    for row in r.rows:
        key, val = _split_row(row)
        if not (key and val):
            continue
        # ★同じ見出しで違う値なら止める★（先に来た方を勝手に採らない）
        if key in out and out[key] != val:
            raise MachineError(f"「{key}」が2つあり、中身が違います: {out[key]!r} / {val!r}")
        out[key] = val
    out["release"] = _read_date(out.get("release_text", ""))
    return out


_ID_RE = re.compile(r"^\d{1,7}$")


def _same_machine_url(url: str, machine_id: str) -> bool:
    """★同じ機種IDのページか★（依頼165のP0）

    取得後の最終URLを見ないと、機種AのURLが機種Bへ転送されたとき
    **Bの中身をAの機種IDとして**返してしまう。名前が同じなら照合も抜ける。
    """
    from urllib.parse import urlsplit
    u = urlsplit(url or "")
    if u.scheme not in ("http", "https"):
        return False
    if u.hostname not in ("www.p-world.co.jp", "p-world.co.jp"):
        return False
    if u.query or u.fragment:
        return False
    return u.path.rstrip("/") == "/machine/database/" + str(machine_id)


def verify(machine_id: str, name: str, html: str | None = None,
           expect_maker: str = "", expect_release: str = "",
           final_url: str | None = None) -> dict:
    """★カレンダーの機種名と、機種ページが同じ機種か★

    返すのは {"problems": [...], ...読み取った身元}。
    ★problems が空のときだけ、この機種として扱ってよい★
    """
    import model_code_lookup as _mc
    import new_machine_watch as _nw
    out = {"machine_id": str(machine_id), "url": MACHINE_URL % machine_id,
           "problems": []}
    if not _ID_RE.match(str(machine_id)):
        out["problems"].append(f"機種IDが数字ではありません: {machine_id!r}")
        return out
    if html is None:
        try:
            html = _nw._get(out["url"])
        except Exception as e:              # noqa: BLE001
            out["problems"].append(f"機種ページを取得できません: {e}")
            return out
        # ★最終URLは _get が控えている★（既存の verify_official と同じ見方）
        final_url = (_nw.LAST_FINAL_URL or {}).get("url") or final_url
    # ★転送先が同じ機種IDか★（依頼165のP0）
    if final_url and not _same_machine_url(final_url, machine_id):
        out["problems"].append(
            f"別のページへ転送されました（{final_url[:80]}）")
        return out
    why = _nw.bad_page(html)
    if why:
        out["problems"].append(f"機種ページが読める状態ではありません（{why}）")
        return out
    try:
        got = parse(html)
    except MachineError as e:
        out["problems"].append(str(e))
        return out
    out.update(got)
    # ★種目★ パチンコを新台として拾わない
    if not got["is_slot"]:
        out["problems"].append(
            f"パチスロのページではありません（ラベル: {'/'.join(got['tags'])}）")
    # ★同じ機種か★ 判定は名鑑ページ用の既存の仕組みに任せる
    #   （続編・派生・シリーズ違いを通さない・自前で緩めない）
    # ★同定は既存の全経路と同じ厳しさで★（依頼165のP0）
    #   既定は材料ページ向けの緩い方。公式の同定は strict_all_tail=True。
    ok, why2 = _mc.page_is_machine(html, name, strict_all_tail=True)
    if not ok:
        out["problems"].append(f"機種名が一致しません（{why2}）")
    # ★導入日★ 読めないまま「いつ出るか」を空で進めない
    if not got.get("release"):
        out["problems"].append("導入開始の日付が読めません")
    # ★メーカーが読めなければ止める★（2026-08-12・依頼167のP0）
    #   「値がある時だけ比べる」と書くと、**値が無い時は確認せずに通る**。
    #   確かめられなかったことを、確かめたことにしない。
    if not got.get("maker"):
        out["problems"].append("メーカーが読めません")
    # ★カレンダーの言い分と食い違えば止める★（依頼165のP1）
    if expect_maker and got.get("maker") and expect_maker != got["maker"]:
        out["problems"].append(
            f"メーカーが食い違います（カレンダー: {expect_maker} / "
            f"機種ページ: {got['maker']}）")
    if expect_release and got.get("release") and expect_release != got["release"]:
        out["problems"].append(
            f"導入日が食い違います（カレンダー: {expect_release} / "
            f"機種ページ: {got['release']}）")
    return out



# ---------------------------------------------------------------- selftest
_FIX = """<html><head><title>マイジャグラーVI パチスロ新台 | P-WORLD</title></head>
<body>
<h1>マイジャグラーVI</h1>
<p class="kisyuTag"><span class="kisyuTag-slot">パチスロ</span><span
   class="kisyuTag-slotType">ノーマル</span></p>
<div class="kisyuInfo">
  <table class="kisyuInfo-grid">
    <tr><td>メーカー　：<a href="/x">北電子</a></td></tr>
    <tr><td><table class="typeName"><tr><td>タイプ　　：</td><td>6号機、ノーマル</td></tr></table></td></tr>
    <tr><td><table class="typeName"><tr><td>機械割　　：</td><td>97.0% ～ 109.4%</td></tr></table></td></tr>
    <tr><td>検定番号　：5S0437</td></tr>
    <tr><td><table class="modelName"><tr><td>型式名　　：</td><td>SマイジャグラーVI KK</td></tr></table></td></tr>
    <tr><td>導入開始　：<a href="/y">2026年10月05日</a></td></tr>
  </table>
</div>
</body></html>"""


def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    def raises(fn, word=""):
        try:
            fn()
            return False
        except MachineError as e:
            return (word in str(e)) if word else True
        except Exception:
            return False

    g = parse(_FIX)
    t("★★身元と日程を読み取る★★",
      g["name"] == "マイジャグラーVI" and g["maker"] == "北電子"
      and g["model_code"] == "SマイジャグラーVI KK" and g["shinsa"] == "5S0437")
    t("　導入開始を日付に直す", g["release"] == "2026-10-05")
    t("　パチスロだと分かる", g["is_slot"] is True)
    t("★★記事に載る値はここでは採らない★★（2AIの仕事）",
      "ceiling" not in g and "純増" not in json.dumps(g, ensure_ascii=False))
    t("　知らない見出しは読まない", _split_row("なにか　：値") == ("", ""))

    # ★読めなければ黙って空にしない★
    t("★★機種名が読めなければ止まる★★",
      raises(lambda: parse(_FIX.replace("<h1>マイジャグラーVI</h1>", "")), "機種名"))
    t("★★種目のラベルが読めなければ止まる★★",
      raises(lambda: parse(_FIX.replace('class="kisyuTag"', 'class="other"')), "種目"))
    t("★★機種情報の表が読めなければ止まる★★",
      raises(lambda: parse(_FIX.replace('class="kisyuInfo"', 'class="other"')), "表"))

    pachi = _FIX.replace('<span class="kisyuTag-slot">パチスロ</span>',
                         '<span class="kisyuTag-slot">パチンコ</span>')
    t("　パチンコのページは種目で分かる", parse(pachi)["is_slot"] is False)

    v = verify("10513", "マイジャグラーVI", html=_FIX)
    t("★★同じ機種なら問題なし★★", not v["problems"] and v["release"] == "2026-10-05")
    v2 = verify("10513", "マイジャグラーV", html=_FIX)
    t("★★続編・別作を同じ機種と認めない★★",
      any("機種名が一致しません" in p for p in v2["problems"]))
    v3 = verify("10513", "マイジャグラーVI", html=pachi)
    t("★★パチンコのページは弾く★★",
      any("パチスロのページではありません" in p for p in v3["problems"]))
    v4 = verify("10513", "マイジャグラーVI",
                html=_FIX.replace("2026年10月05日", "近日"))
    t("★★導入日が読めなければ進めない★★",
      any("導入開始の日付が読めません" in p for p in v4["problems"]))
    # ★読めなかったことを、確かめたことにしない★（2026-08-12・依頼167のP0）
    #   「値がある時だけ比べる」と書くと、値が無い時は確認せずに通る。
    _no_maker = _FIX.replace(
        '<tr><td>メーカー　：<a href="/x">北電子</a></td></tr>', "")
    v5 = verify("10513", "マイジャグラーVI", html=_no_maker)
    t("★★メーカーが読めなければ進めない★★（無いのに確認済みにしない）",
      any("メーカーが読めません" in p for p in v5["problems"]))
    #   ★対照実験★＝直す前は「メーカーを渡さなければ何も言わない」形だった
    t("　（対照）中身が読めていれば問題は出ない",
      not verify("10513", "マイジャグラーVI", html=_FIX)["problems"])

    ng = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id")
    ap.add_argument("--name", default="", help="カレンダーの機種名（同定に使う）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.id:
        print("--id か --selftest が要ります")
        return 2
    if args.name:
        got = verify(args.id, args.name)
    else:
        import new_machine_watch as _nw
        got = parse(_nw._get(MACHINE_URL % args.id))
        got["problems"] = []
    if args.json:
        print(json.dumps(got, ensure_ascii=False, indent=2))
    else:
        for k in ("name", "maker", "type", "model_code", "shinsa", "release"):
            if got.get(k):
                print("%-10s %s" % (k, got[k]))
        for p in got.get("problems") or []:
            print("★問題★", p)
    return 1 if got.get("problems") else 0


if __name__ == "__main__":
    sys.exit(main())

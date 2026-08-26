"""html_tables.py — HTMLの表を「区画」として読む共通部品。

★なぜ要るか（2026-07-31・Codex指摘4を自分で再現した）★
  本文を平らな行の列にしてから読むと、**表の境目が消える**。
  そのため「見出しをさかのぼって名前を探す」やり方では、
  別の表の値に、上にある別の見出しの名前が付いてしまう。

    CZ「Aチャレンジ」
    ボーナス          ← ここから別の表なのに、行の列では分からない
    継続G数 / 7G
    期待度 / 約50%
                     → 7G・約50% が「Aチャレンジ」の値として出る（実際に再現）

  そこで **<table> ごとに切り出し、その表の直前の見出しだけを名前とする**。
  値は必ず同じ表の中から採るので、別の表の値が混ざらない。

★この部品は「読む」だけ★
  何を採用してよいかは呼び出し側が決める。ここでは判断しない。
"""

from __future__ import annotations

import html as _html
import re
import unicodedata

_S = "[ \t\r\n]*"          # バックスラッシュを直接書かない（制御文字に化ける事故が続いたため）


def _text(fragment: str) -> str:
    """タグを外して素の文字にする。"""
    t = re.sub("(?is)<[^>]+>", " ", str(fragment or ""))
    t = _html.unescape(t)
    return unicodedata.normalize("NFKC", " ".join(t.split()))


from html.parser import HTMLParser as _HTMLParser

# 読まない区画。script等は実行用、aside等は本文でない脇の区画
_SKIP_TAGS = ("script", "style", "noscript", "template",
              "aside", "nav", "footer", "header")
_VOID = ("br", "img", "hr", "input", "meta", "link", "wbr", "source")


def _span_num(v) -> int:
    """rowspan/colspan の数（読めなければ 1 ではなく大きい値＝安全側）"""
    s = str(v or "").strip()
    if not s:
        return 1
    try:
        n = int(s)
    except ValueError:
        return 9999          # ★読めない指定は「またいでいる」とみなす★
    return n if n >= 1 else 9999


class _TableParser(_HTMLParser):
    """★画面に出る表だけをHTML解析で読む★（2026-08-03・Codex63回目）

    生HTMLの正規表現走査は、HTMLコメント・<template>・hidden祖先の中の
    「読者には見えない旧値の表」まで採れた（4収集器すべてに波及）。
    パーサならコメントはそもそも本文にならず、hidden・脇区画は
    スタックの印で除外できる。解析に失敗したら空＝採らない側に倒す。
    """

    def __init__(self, hidden_classes: frozenset = frozenset()):
        super().__init__(convert_charrefs=True)
        self.out = []                    # 完成した表
        self.stack = []                  # (tag, skip, hidden, depthを増やしたか)
        self.hidden_classes = hidden_classes
        self.table_depth = 0
        self.cur = None                  # 取り込み中の表
        self.cell = None                 # 取り込み中のセルの文字
        self.in_caption = False
        self.cap_buf = []
        self.head_tag = None             # 取り込み中の見出し(h1-h6)
        self.head_buf = []
        self.last_heading = ""          # 前の表からここまでの最後の見出し
        # ★見出しの階層をそのまま持つ★（2026-08-06・Codex138回目）
        #   <h2>通常時</h2><h3>内部モードごとの特徴</h3><table> のとき、
        #   直前の見出し1つ（＝h3）だけでは**親の「通常時」が失われる**。
        #   AT中の表を通常時と取り違えると、実際より浅い天井を載せてしまう。
        self.head_levels = {}           # {1..6: 見出し文字列}

    def _is_hidden(self, attrs) -> bool:
        d = dict(attrs)
        if "hidden" in d:
            return True
        if str(d.get("aria-hidden", "")).lower() == "true":
            return True
        style = str(d.get("style", "")).lower().replace(" ", "")
        if "display:none" in style or "visibility:hidden" in style:
            return True
        # ★同じ文書の<style>で display:none にされたクラス★（Codex64回目）
        #   .old-spec { display:none } ＋ class="old-spec" の形。
        #   外部CSSまでは判定できない（描画なしの限界）。
        if self.hidden_classes:
            cls = set(str(d.get("class", "") or "").split())
            if cls & self.hidden_classes:
                return True
        return False

    def _suppressed(self) -> bool:
        return any(s or h for _t, s, h, _c in self.stack)

    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            return
        skip = tag in _SKIP_TAGS
        hidden = self._is_hidden(attrs)
        suppressed_before = self._suppressed()
        counted = False
        if tag == "table" and not (suppressed_before or skip or hidden):
            # ★depthを増やしたことをスタックに残す★（2026-08-03・Codex64回目。
            #   非表示の入れ子表の閉じタグで外の表が途中終了していた）
            counted = True
        self.stack.append((tag, skip, hidden, counted))
        if suppressed_before or skip or hidden:
            return
        if tag == "table":
            self.table_depth += 1
            if self.table_depth == 1:
                self.cur = {"title": self.last_heading, "pairs": [],
                            "cells": [], "rows": [], "has_span": False,
                            "headings": [self.head_levels[k] for k in
                                         sorted(self.head_levels)],
                            "caption": "",
                            "_cap": None, "_nested": False}
            else:
                # ★入れ子の表は丸ごと不採用★（2026-08-03・Codex64回目。
                #   内側の値が外側の見出しの値として混ざった）
                if self.cur is not None:
                    self.cur["_nested"] = True
            return
        if self.cur is not None:
            if tag == "tr":
                self.cur["rows"].append([])
            elif tag in ("th", "td"):
                self.cell = []
                d = dict(attrs)
                # ★★spanが「どこに」あるかを残す★★（2026-08-26・Codex33回目）
                #   ★真偽だけだと「題の行にしかspanが無い」を証明できない★＝
                #   列数がそろっていても、データ行のセルが colspan=2 なら
                #   画面上は1列多く、以後の対応づけが1列ずれる。
                _rs = _span_num(d.get("rowspan"))
                _cs = _span_num(d.get("colspan"))
                if _rs != 1 or _cs != 1:
                    self.cur["has_span"] = True
                    self.cur.setdefault("spans", []).append(
                        {"row": max(0, len(self.cur["rows"]) - 1),
                         "col": max(0, len(self.cur["rows"][-1]))
                         if self.cur["rows"] else 0,
                         "rowspan": _rs, "colspan": _cs})
            elif tag == "caption":
                self.in_caption = True
                self.cap_buf = []
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.head_tag = tag
            self.head_buf = []

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        # スタックを閉じタグまで巻き戻す（閉じ忘れHTMLに耐える）
        counted_closed = False
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                counted_closed = any(c for _t, _s, _h, c in self.stack[i:])
                del self.stack[i:]
                break
        if tag == "table":
            # ★depthを増やした開始タグに対応する閉じだけで減らす★（Codex64回目）
            if not counted_closed:
                return
            self.table_depth -= 1
            if self.table_depth == 0 and self.cur is not None:
                cur = self.cur
                if cur["_cap"]:
                    # ★caption は題を上書きするが、見出しは消さない★
                    cur["title"] = cur["_cap"]
                    cur["caption"] = cur["_cap"]
                nested = cur.pop("_nested")
                del cur["_cap"]
                if not nested:            # 入れ子を含む表は返さない
                    self.out.append(cur)
                self.cur = None
                self.last_heading = ""   # 見出しは「前の表との間」だけ有効
            return
        if self.cur is not None:
            if tag in ("th", "td") and self.cell is not None:
                text = unicodedata.normalize(
                    "NFKC", " ".join("".join(self.cell).split()))
                if self.cur["rows"]:
                    self.cur["rows"][-1].append(text)
                self.cur["cells"].append(text)
                self.cell = None
            elif tag == "tr" and self.cur["rows"]:
                got = self.cur["rows"][-1]
                if len(got) >= 2:
                    self.cur["pairs"].append((got[0], got[1]))
            elif tag == "caption":
                self.cur["_cap"] = unicodedata.normalize(
                    "NFKC", " ".join("".join(self.cap_buf).split()))
                self.in_caption = False
        elif tag == self.head_tag:
            got = unicodedata.normalize(
                "NFKC", " ".join("".join(self.head_buf).split()))
            self.last_heading = got
            try:                          # h1〜h6 の階層を更新する
                lv = int(str(self.head_tag)[1])
            except (ValueError, IndexError):
                lv = 6
            self.head_levels[lv] = got
            for k in [k for k in self.head_levels if k > lv]:
                del self.head_levels[k]   # 深い見出しは新しい親で無効になる
            self.head_tag = None

    def handle_data(self, data):
        if self._suppressed():
            return
        if self.cell is not None:
            self.cell.append(" " + data + " ")
        elif self.in_caption:
            self.cap_buf.append(data)
        elif self.head_tag:
            self.head_buf.append(data)


def tables(html: str) -> list:
    """表を1つずつ、直前の見出しと一緒に返す。

    返すもの: [{"title": 直前の見出し, "headings": [親からの見出し],
                "caption": 表の題, "pairs": [(左, 右), ...],
                "cells": [...], "rows": [...], "has_span": bool}]

    ★画面に出るものだけ★（2026-08-03・Codex63回目）
      HTMLコメント・template/script/style・hidden/aria-hidden/display:none
      の祖先・脇区画（aside/nav/footer/header）の中の表は返さない。
      見出しも同じ条件で「前の表とこの表の間」のものだけを題にする。
    """
    p = _TableParser(_css_hidden_classes(str(html or "")))
    try:
        p.feed(str(html or ""))
    except Exception:                     # noqa: BLE001
        return []                         # 解析できなければ採らない側に倒す
    return p.out


_STYLE_RE = re.compile("(?is)<style[^>]*>(.*?)</style" + _S + ">")
_HIDE_BODY_RE = re.compile(
    r"(?i)display\s*:\s*none|visibility\s*:\s*hidden")
_AT_BLOCK_RE = re.compile(
    r"(?is)@[^{};]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}")


def _css_hidden_classes(html: str) -> frozenset:
    """★同じ文書の<style>で非表示にされるクラス名★（2026-08-03・Codex64回目）

    .old-spec { display:none } のような同一文書内の定義だけを読む。
    外部CSS・JSによる切替は描画なしでは判定できない（そこは
    「両名鑑を偽造できる立場が要る」信頼境界の外として扱う）。

    ★セレクタが「単独クラスちょうど」の規則だけを数える★
      `.entry-content iframe{display:none}` のような複合セレクタで
      先頭のクラスを数えると、ちょんぼりすたの本文包み（entry-content）
      ごと非表示扱いになり、実在の全表を失った（実際に起きた）。
      @media の中も数えない（画面幅による切替＝常時非表示ではない）。
    """
    out = set()
    for m in _STYLE_RE.finditer(html or ""):
        css = _AT_BLOCK_RE.sub(" ", m.group(1))
        for rule in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
            if not _HIDE_BODY_RE.search(rule.group(2)):
                continue
            for part in rule.group(1).split(","):
                mm = re.match(r"^\.([A-Za-z0-9_-]+)$", part.strip())
                if mm:
                    out.add(mm.group(1))
    return frozenset(out)


def value_of(pairs: list, labels) -> str:
    """表の中から、指定の見出しの右にある値を取る。★同じ表の中だけ★"""
    for left, right in pairs:
        if left in labels:
            return right
    return ""


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []
    nl = chr(10)

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    H = ('<div><h3><span>CZ「Aチャレンジ」</span></h3> <table><tbody>'
         '<tr><th>タイプ</th><td>ST</td></tr>'
         '<tr><th>継続G数</th><td>4G＋α</td></tr>'
         '<tr><th>期待度</th><td>約40%</td></tr></tbody></table></div>'
         '<div><h3>CZ「Bチャレンジ」</h3> <table><tbody>'
         '<tr><th>継続G数</th><td>7G</td></tr>'
         '<tr><th>期待度</th><td>約50%</td></tr></tbody></table></div>')
    tb = tables(H)
    t("★表を1つずつ切り出す★", len(tb) == 2)
    t("★★表ごとに直前の見出しを持つ★★（別の表の名前が混ざらない）",
      tb[0]["title"] == "CZ「Aチャレンジ」" and tb[1]["title"] == "CZ「Bチャレンジ」")
    t("★★値は同じ表の中からしか採らない★★",
      value_of(tb[1]["pairs"], ("継続G数",)) == "7G"
      and value_of(tb[0]["pairs"], ("継続G数",)) == "4G+α")
    t("　全角の＋αも半角にそろえる",
      value_of(tb[0]["pairs"], ("継続G数",)) == "4G+α")
    t("　無い見出しは空を返す", value_of(tb[0]["pairs"], ("純増",)) == "")
    t("★★asideの見出しを表の題にしない★★（Codex61回目）",
      tables('<h3>AT「通常AT」</h3><table><tr><th>a</th><td>b</td></tr></table>'
             '<aside><h3>上位ATへの移行条件</h3></aside>'
             '<table><tr><th>c</th><td>d</td></tr></table>')[1]["title"] == "")
    t("　表が無ければ空", tables("<p>表はありません</p>") == [])
    t("　タグの中の文字は本文にしない",
      tables('<h3>見出し</h3><table><tr><th>a<span>b</span></th>'
             '<td>c</td></tr></table>')[0]["pairs"] == [("a b", "c")])
    HH = ("<h2>通常時</h2><h3>内部モードごとの特徴</h3>"
          "<table><tr><th>通常A</th><td>天井:749G+α</td></tr></table>"
          "<h2>AT中</h2><h3>内部モードごとの特徴</h3>"
          "<table><tr><th>通常A</th><td>天井:649G+α</td></tr></table>")
    hh = tables(HH)
    t("★★親の見出しを失わない★★（通常時の表とAT中の表を取り違えない）",
      hh[0]["headings"] == ["通常時", "内部モードごとの特徴"]
      and hh[1]["headings"] == ["AT中", "内部モードごとの特徴"])
    hc = tables("<h2>通常時</h2><table><caption>モード別天井</caption>"
                "<tr><th>a</th><td>b</td></tr></table>")[0]
    t("★★caption は題を上書きするが、見出しは消さない★★",
      hc["caption"] == "モード別天井" and hc["headings"] == ["通常時"])
    t("　深い見出しは新しい親で無効になる",
      tables("<h2>A</h2><h3>B</h3><h2>C</h2>"
             "<table><tr><th>x</th><td>y</td></tr></table>"
             )[0]["headings"] == ["C"])
    t("★caption があれば見出しより優先する★",
      tables('<h3>ちがう見出し</h3><table><caption>本当の名前</caption>'
             '<tr><th>a</th><td>b</td></tr></table>')[0]["title"] == "本当の名前")
    _HID = ('<h3>AT「本物」</h3><table><tr><th>継続G数</th><td>1セット100G</td></tr>'
            "<tr><th>純増</th><td>約2.8枚/G</td></tr></table>"
            '<div hidden><h3>上位AT「旧仕様」</h3><table>'
            "<tr><th>継続G数</th><td>1セット100G</td></tr>"
            "<tr><th>純増</th><td>約9.9枚/G</td></tr></table></div>")
    t("★★hidden祖先の中の表を返さない★★"
      "（読者に見えない旧値が2票になれた・Codex63回目）",
      len(tables(_HID)) == 1 and tables(_HID)[0]["title"] == "AT「本物」")
    t("　HTMLコメントの中の表を返さない",
      tables("<!-- <h3>廃止</h3><table><tr><th>a</th><td>b</td></tr></table> -->"
             "<p>本文</p>") == [])
    t("　template・display:none・aria-hiddenの中の表も返さない",
      tables("<template><table><tr><th>a</th><td>b</td></tr></table></template>"
             '<div style="display:none"><table><tr><th>c</th><td>d</td></tr>'
             "</table></div>"
             '<div aria-hidden="true"><table><tr><th>e</th><td>f</td></tr>'
             "</table></div>") == [])
    t("　asideの中の表も返さない",
      tables("<aside><table><tr><th>a</th><td>b</td></tr></table></aside>") == [])
    t("★★同じ文書の<style>によるクラス非表示の表を返さない★★（Codex64回目）",
      tables("<style>.old-spec { display:none }</style>"
             '<div class="old-spec"><h3>上位AT「旧仕様」</h3><table>'
             "<tr><th>純増</th><td>約9.9枚/G</td></tr></table></div>"
             "<h3>AT「本物」</h3><table>"
             "<tr><th>純増</th><td>約2.8枚/G</td></tr></table>")[0]["title"]
      == "AT「本物」"
      and len(tables("<style>.x{display:none}</style>"
                     '<table class="x"><tr><th>a</th><td>b</td></tr></table>')) == 0)
    t("★★入れ子の表は丸ごと不採用★★"
      "（内側の値が外側の見出しの値として混ざった・Codex64回目）",
      tables('<h3>CZ「Aチャレンジ」</h3><table><tr><td>'
             "<h3>ボーナス</h3><table>"
             "<tr><th>継続G数</th><td>7G</td></tr>"
             "<tr><th>期待度</th><td>約50%</td></tr></table>"
             "</td></tr></table>") == [])
    t("★★非表示の入れ子表の閉じで外の表を途中終了しない★★（Codex64回目）",
      value_of(tables("<h3>AT「本物」</h3><table>"
                      "<tr><th>継続G数</th><td>1セット100G</td></tr>"
                      "<tr><td><div hidden>"
                      "<table><tr><td>旧データ</td></tr></table>"
                      "</div></td></tr>"
                      "<tr><th>純増</th><td>約2.8枚/G</td></tr></table>"
                      )[0]["pairs"], ("純増",)) == "約2.8枚/G")
    t("　実体参照をほどく",
      tables("<h3>x</h3><table><tr><th>a</th><td>1&nbsp;G</td></tr></table>"
             )[0]["pairs"] == [("a", "1 G")])

    ng = [n for n, ok in results if not ok]
    print(f"{nl}{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


if __name__ == "__main__":
    import sys
    raise SystemExit(selftest() if "--selftest" in sys.argv else selftest())

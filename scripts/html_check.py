"""html_check.py — HTMLを実際に解析して確かめる共通部品。

★なぜ正規表現をやめたか（2026-07-31・Codex指摘を再現した）★
  検査を正規表現で書いていたため、書き方を少し変えるだけで通り抜けられた。

    <div hidden="">先行記事</div>          → 「見える文字」と誤判定した
    <meta name='robots' content='index'>   → 競合する2個目を数え落とした

  属性の引用符・順序・空白の揺れを正規表現で全部そろえるのは無理があるので、
  標準の HTMLParser で解析し、**属性を正規化してから**見る。

★この部品は「読む」だけ★ 何を許すかは呼び出し側が決める。
"""

from __future__ import annotations

import html
import sys
from html.parser import HTMLParser

# 中身を本文と見なさない要素
_NON_TEXT = {"script", "style", "template", "noscript", "head", "title"}


class _Doc(HTMLParser):
    """必要なものだけ拾う。★閉じ忘れがあっても落ちない★"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.metas = []          # [{name, content}]
        self.links = []          # [{rel, href}]
        self.bases = []          # [href]
        self.visible = []        # 読者に見える文字
        self._skip = 0           # 中身を読まない入れ子の深さ
        self._hidden = []        # 隠された要素のスタック
        self._in_body = False

    @staticmethod
    def _attrs(attrs) -> dict:
        out = {}
        for k, v in attrs:
            out[(k or "").strip().lower()] = (v or "").strip()
        return out

    @staticmethod
    def _is_hidden(a: dict) -> bool:
        """★隠されているか★（属性でもCSSでも）"""
        if "hidden" in a:                     # hidden / hidden="" / hidden="hidden"
            return True
        if a.get("aria-hidden", "").lower() == "true":
            return True
        style = a.get("style", "").replace(" ", "").lower()
        return "display:none" in style or "visibility:hidden" in style

    def handle_starttag(self, tag, attrs):
        a = self._attrs(attrs)
        if tag == "body":
            self._in_body = True
        if tag == "meta" and a.get("name"):
            self.metas.append({"name": a["name"].lower(), "content": a.get("content", "")})
        if tag == "link" and a.get("rel"):
            self.links.append({"rel": a["rel"].lower(), "href": a.get("href", "")})
        if tag == "base":
            self.bases.append(a.get("href", ""))
        if tag in _NON_TEXT:
            self._skip += 1
        elif self._is_hidden(a):
            self._hidden.append(tag)
        # 閉じない要素は入れ子に数えない
        if tag in ("meta", "link", "base", "br", "img", "hr", "input"):
            if tag in _NON_TEXT:
                self._skip -= 1
            elif self._hidden and self._hidden[-1] == tag:
                self._hidden.pop()

    def handle_endtag(self, tag):
        if tag in _NON_TEXT and self._skip > 0:
            self._skip -= 1
        elif self._hidden and self._hidden[-1] == tag:
            self._hidden.pop()

    def handle_data(self, data):
        if self._in_body and not self._skip and not self._hidden:
            t = data.strip()
            if t:
                self.visible.append(t)


def parse(source: str) -> _Doc:
    doc = _Doc()
    try:
        doc.feed(source or "")
        doc.close()
    except Exception:                       # noqa: BLE001
        pass                                # 壊れたHTMLでも、拾えた分で判断する
    return doc


def meta_values(doc: _Doc, name: str) -> list:
    """同じ name の meta の中身を、区切りでほどいて返す（★個数も分かる★）。"""
    out = []
    for m in doc.metas:
        if m["name"] == name.lower():
            out.append({x.strip().lower()
                        for x in m["content"].replace(";", ",").replace(" ", ",").split(",")
                        if x.strip()})
    return out


def link_hrefs(doc: _Doc, rel: str) -> list:
    return [x["href"] for x in doc.links if x["rel"] == rel.lower()]


def visible_text(source: str) -> str:
    """読者に見える文字だけ。★隠された要素とscriptは入らない★"""
    return " ".join(parse(source).visible)


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []
    nl = chr(10)

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    H = ('<html><head><base href="/">'
         '<meta name="robots" content="noindex,follow">'
         '<link rel="canonical" href="https://uchidokoro.com/machines/x/">'
         "</head><body><p>⚠ 先行記事（解析待ち）</p>"
         "<script>var s='先行記事';</script></body></html>")
    d = parse(H)
    t("★robots の中身をほどいて読める★", meta_values(d, "robots") == [{"noindex", "follow"}])
    t("★canonical を読める★",
      link_hrefs(d, "canonical") == ["https://uchidokoro.com/machines/x/"])
    t("　base を読める", d.bases == ["/"])
    t("★読者に見える文字だけを返す★",
      "先行記事" in visible_text(H) and "var s" not in visible_text(H))

    t("★★引用符が違っても数え落とさない★★（正規表現では見逃していた）",
      len(meta_values(parse(H.replace("</head>",
          "<meta name='robots' content='index'></head>")), "robots")) == 2)
    t("★★hidden=\"\" でも隠されていると分かる★★（正規表現では見逃していた）",
      "先行記事" not in visible_text('<body><div hidden="">先行記事</div>本文</body>'))
    for hide in ('hidden', 'aria-hidden="true"', 'style="display:none"',
                 'style="visibility: hidden"'):
        t(f"　{hide[:22]} で隠した文字は見えない扱い",
          "先行記事" not in visible_text(f"<body><div {hide}>先行記事</div>本文</body>"))
    t("　隠した要素の外の文字は見える",
      "本文" in visible_text('<body><div hidden>先行記事</div>本文</body>'))
    t("　属性の大文字・空白が揺れても読める",
      meta_values(parse('<meta NAME=" Robots "  content="NoIndex">'),
                  "robots") == [{"noindex"}])
    t("　壊れたHTMLでも落ちない", isinstance(visible_text("<body><p>あ"), str))
    t("　実体参照をほどく", "あ&い" not in visible_text("<body>あ&amp;い</body>")
      or "あ&い" in visible_text("<body>あ&amp;い</body>"))

    ng = [n for n, ok in results if not ok]
    print(f"{nl}{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


if __name__ == "__main__":
    raise SystemExit(selftest())

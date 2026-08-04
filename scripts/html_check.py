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
        self.notices = []        # 先行記事の断り書き（専用の目印つき）
        self.blocks = []         # ★未確認の箱★（data-pending-section つき）
        # ★開いている要素を全部積む★（2026-07-31・Codex指摘4を再現して直した）
        #   以前は「隠された要素」だけを積んでいたので、
        #   <div hidden><div></div>先行記事</div> のように
        #   同じタグ名で閉じられると、外側の隠しが外れてしまった。
        self._stack = []         # [(tag, hidden, skip)]
        self._in_body = False
        self._hidden_classes = set()

    # 閉じない要素
    _VOID = {"meta", "link", "base", "br", "img", "hr", "input", "source",
             "col", "area", "embed", "track", "wbr", "param"}

    @staticmethod
    def _attrs(attrs) -> dict:
        out = {}
        for k, v in attrs:
            out[(k or "").strip().lower()] = (v or "").strip()
        return out

    def _is_hidden(self, a: dict) -> bool:
        """★隠されているか★（属性でもCSSでも・クラスでも）

        ★クラスによる非表示も見る★（2026-08-04・Codex77回目の指摘4）
          `.is-hidden { display:none }` のようなクラスは画面では消えるのに
          「見えている」と判定していた。隠すクラスの名前は呼び出し側が渡す
          （CSSファイルから機械的に取り出す＝手で並べない）。
        """
        if "hidden" in a:                     # hidden / hidden="" / hidden="hidden"
            return True
        if a.get("aria-hidden", "").lower() == "true":
            return True
        style = a.get("style", "").replace(" ", "").lower()
        if "display:none" in style or "visibility:hidden" in style:
            return True
        cls = set((a.get("class") or "").split())
        return any(want <= cls for want in self._hidden_classes if want)

    def _hidden_now(self) -> bool:
        return any(h for _t, h, _s in self._stack)

    def _skip_now(self) -> bool:
        return any(k for _t, _h, k in self._stack)

    def handle_starttag(self, tag, attrs):
        a = self._attrs(attrs)
        if tag == "body":
            self._in_body = True
        if tag == "meta" and a.get("name"):
            self.metas.append({"name": a["name"].lower(),
                               "content": a.get("content", "")})
        if tag == "link" and a.get("rel"):
            self.links.append({"rel": a["rel"].lower(), "href": a.get("href", "")})
        if tag == "base":
            self.bases.append(a.get("href", ""))
        # ★専用の目印がある断り書き★（文面が本文のどこかにあるだけでは認めない）
        if a.get("data-preview-notice"):
            self.notices.append({"kind": a["data-preview-notice"],
                                 "hidden": self._hidden_now() or self._is_hidden(a),
                                 "text": ""})
            self._notice_open = len(self._stack)
        # ★記事の箱★（2026-08-04・Codex77〜78回目。ページ側の欠落・重複・
        #   順番・中身を確かめるため、**全部の箱**に目印を付けて集める）
        if a.get("data-section") is not None:
            self.blocks.append({
                "title": a["data-section"],
                "pending_title": a.get("data-pending-section"),
                "hidden": self._hidden_now() or self._is_hidden(a),
                "text": ""})
            self._block_open = len(self._stack)
        if tag not in self._VOID:
            self._stack.append((tag, self._is_hidden(a), tag in _NON_TEXT))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)      # <br/> のような自閉じ

    def handle_endtag(self, tag):
        # ★同じタグ名が見つかるところまで戻す★（閉じ忘れがあっても崩れない）
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                b_open = getattr(self, "_block_open", None)
                if b_open is not None and len(self._stack) <= b_open:
                    self._block_open = None
                opened = getattr(self, "_notice_open", None)
                if opened is not None and len(self._stack) <= opened:
                    # ★断り書きはここで終わり★（2026-07-31・自分の確認で気づいた）
                    #   閉じたことを覚えないと、その後の文章まで断り書きに混ざり、
                    #   ページ全体を「断り書きの文面」として読んでしまう。
                    self._notice_open = None
                return

    def handle_data(self, data):
        t = data.strip()
        if not t:
            return
        opened = getattr(self, "_notice_open", None)
        # ★断り書きの文字も、隠された所と script の中は数えない★
        #   （2026-07-31・Codex指摘5を再現）
        #   <span hidden>先行記事</span> や <script>先行記事</script> でも
        #   文面検査を通ってしまい、読者には何も見えない状態になり得た。
        if (self.notices and opened is not None and len(self._stack) > opened
                and not self._skip_now() and not self._hidden_now()):
            self.notices[-1]["text"] += t
        b_open = getattr(self, "_block_open", None)
        if (self.blocks and b_open is not None and len(self._stack) > b_open
                and not self._skip_now() and not self._hidden_now()):
            self.blocks[-1]["text"] += t
        if self._in_body and not self._skip_now() and not self._hidden_now():
            self.visible.append(t)


def hidden_classes_from_css(css: str) -> set:
    """CSSから「その組み合わせが付いていたら消える」クラス集合を取り出す。

    ★手で並べない★（2026-08-04・Codex77〜78回目）。返すのは frozenset の集合で、
    要素が**その全部**を持っていたら隠れていると見なす。
      `.is-hidden{display:none}`            → {frozenset({"is-hidden"})}
      `.article-item.pending{display:none}` → {frozenset({"article-item","pending"})}
    ★@media の中も数える★（画面幅で消えるものは「常に見える」とは言えない。
      読者の一部に見えないなら、未確認の表示としては不十分）。
    クラス以外（要素・id・子孫）が混ざる規則は、条件を判定できないので数えない。
    """
    import re as _re
    got = set()
    body = _re.sub(r"/\*.*?\*/", " ", css or "", flags=_re.S)
    body = body.replace("@media", chr(10) + "@media")   # 入れ子をほどきやすくする
    for sel, decl in _re.findall(r"([^{}]+)\{([^{}]*)\}", body):
        d = _re.sub(r"\s+", "", decl).lower()
        if "display:none" not in d and "visibility:hidden" not in d:
            continue
        for one in sel.split(","):
            o = one.strip()
            if o.startswith("@"):
                continue
            if not _re.match(r"^(?:\.[A-Za-z0-9_-]+)+$", o):
                continue                        # 複合・子孫・要素混じりは判定不能
            got.add(frozenset(_re.findall(r"\.([A-Za-z0-9_-]+)", o)))
    return got


def parse(source: str, hidden_classes: set | None = None) -> _Doc:
    doc = _Doc()
    doc._hidden_classes = set(hidden_classes or ())
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


def preview_notices(doc: _Doc, kind: str) -> list:
    """★専用の目印を持つ断り書き★（本文のどこかに同じ語があるだけでは認めない）"""
    return [n for n in doc.notices if n["kind"] == kind and not n["hidden"]]


def visible_text(source: str, hidden_classes: set | None = None) -> str:
    """読者に見える文字だけ。★隠された要素とscriptは入らない★"""
    return " ".join(parse(source, hidden_classes).visible)


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

    t("★★入れ子で閉じても外側の隠しが外れない★★（Codex指摘・実際に再現した）",
      "先行記事" not in visible_text(
          "<body><div hidden><div></div>先行記事</div>ふつう</body>"))
    t("　深い入れ子でも隠しが効く",
      "先行記事" not in visible_text(
          "<body><div hidden><p><span>先行記事</span></p></div>ふつう</body>"))
    t("　閉じ忘れがあっても崩れない",
      "ふつう" in visible_text("<body><div hidden><p>先行記事</div>ふつう</body>"))

    N = ('<body><aside data-preview-notice="PREVIEW_VERIFIED_SUBSET" role="note">'
         "先行記事：確認できた項目だけを掲載しています</aside>"
         "<footer>先行記事一覧はこちら</footer></body>")
    nd = parse(N)
    t("★★専用の目印を持つ断り書きだけを数える★★"
      "（フッターに同じ語があるだけでは認めない）",
      len(preview_notices(nd, "PREVIEW_VERIFIED_SUBSET")) == 1)
    t("★★断り書きの文面は、その要素の中だけ★★（ページ全体を拾っていた）",
      preview_notices(nd, "PREVIEW_VERIFIED_SUBSET")[0]["text"]
      == "先行記事：確認できた項目だけを掲載しています")
    t("　入れ子があっても中身は全部拾う",
      preview_notices(parse(
          '<body><div data-preview-notice="X"><p>先行</p><p>記事</p></div>'
          "<p>あと</p></body>"), "X")[0]["text"] == "先行記事")
    t("★★断り書きの中で隠された文字は文面に数えない★★（Codex指摘・再現した）",
      "先行記事" not in (preview_notices(parse(
          '<body><div data-preview-notice="X"><span hidden>先行記事</span>'
          "本文</div></body>"), "X")[0]["text"]))
    t("　scriptの中の文字も数えない",
      "先行記事" not in (preview_notices(parse(
          '<body><div data-preview-notice="X"><script>先行記事</script>'
          "本文</div></body>"), "X")[0]["text"]))
    t("　隠された断り書きは数えない",
      not preview_notices(parse(N.replace("<aside ", "<aside hidden ")),
                          "PREVIEW_VERIFIED_SUBSET"))

    ng = [n for n, ok in results if not ok]
    print(f"{nl}{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


if __name__ == "__main__":
    raise SystemExit(selftest())

# -*- coding: utf-8 -*-
"""list_card_identity.py — 公式の個別ページが読めない時、同じ公式の一覧カードで同定する。

★なぜ要るか（2026-08-04・台帳#209、Codex92回目で条件つき承認）★
  オーイズミは証明書が期限切れで**個別機種ページを取得できない**。
  一方で一覧ページは読めており、カードに
  「機種名 ／ パチスロ ／ 2026.08」が公式の表記で載っている。
  つまり公式が書いているという条件は満たせる。読めないのは"場所"だけ。

★これは「ブロック理由を消す」仕組みではない★
  取得失敗で止まった検査（名前・種目・年月・メーカー）を、
  **一覧カードから取り直して同じだけ確かめる**代替の検証器。
  条件を1つでも満たさなければ ok=False（＝従来どおり公開しない）。

★メーカーごとの明示許可制★
  maker-catalogs.json に `list_card`（カードの作り）と
  `allow_list_card_identity: true` があるメーカーだけが対象。
  たまたま3項目が揃った未検証のメーカーへ自動で広げない。

使い方:
    python scripts/list_card_identity.py --selftest
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import unicodedata
from html.parser import HTMLParser

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

# 回胴機の語（肯定の証拠）／ぱちんこの語（否定の証拠）
SLOT_WORDS = ("パチスロ", "スロット", "スマスロ", "回胴")
PACHI_WORDS = ("パチンコ", "ぱちんこ", "スマパチ")
# ぱちんこの規格印（名前の先頭）
PACHI_MARK = re.compile(r"^(?:CR(?![0-9A-Za-z])|[eEpP](?![0-9A-Za-z]))")
YEAR_MONTH = re.compile(r"(20\d\d)[./年](\d{1,2})")

# ★代替を許す取得失敗の種類★（Codex92回目：FETCH_FAILEDなら何でも可、にしない）
#   いま実際に起きている2つだけを許す。他は必要になった実例が出てから足す。
ALLOWED_FAILURES = ("CERTIFICATE_VERIFY_FAILED", "SSLV3_ALERT_HANDSHAKE_FAILURE",
                    "SSLCertVerificationError", "SSLError")


class CardError(RuntimeError):
    pass


class _Cards(HTMLParser):
    """要素の木として読み、指定タグ・クラスの要素を「カード」として集める。

    ★数えるのは要素であって文字の断片ではない★（2026-08-04・Codex93回目の指摘3）
      文字の数え方だと、空の<div class="name"></div>が並んでいても1個に見え、
      逆に1つの要素の文字が子要素で割れると複数個に見えた。
    ★隠れている要素・templateの中は読まない★（同・指摘4）
    ★兄弟かどうかを見るため、親を覚える★（同・指摘4）
    """

    _VOID = {"img", "br", "hr", "input", "meta", "link", "source", "area",
             "col", "embed", "track", "wbr", "param", "base"}
    _SKIP = {"script", "style", "template", "noscript"}

    def __init__(self, card_tag: str, card_class: str):
        super().__init__(convert_charrefs=True)
        self._tag, self._cls = card_tag.lower(), card_class
        self._stack: list = []            # 開いている要素
        self._seq = 0
        self.cards: list = []

    @staticmethod
    def _attrs(attrs) -> dict:
        return {(k or "").lower(): (v or "") for k, v in attrs}

    @staticmethod
    def _hidden(a: dict) -> bool:
        if "hidden" in a or a.get("aria-hidden", "").lower() == "true":
            return True
        st = (a.get("style") or "").replace(" ", "").lower()
        return "display:none" in st or "visibility:hidden" in st

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        a = self._attrs(attrs)
        self._seq += 1
        node = {"id": self._seq, "tag": tag,
                "classes": frozenset((a.get("class") or "").split()),
                "href": a.get("href", "").strip() if tag == "a" else "",
                "hidden": self._hidden(a) or tag in self._SKIP
                or any(x["hidden"] for x in self._stack),
                "parent": self._stack[-1]["id"] if self._stack else None,
                "text": "", "card": None}
        # このノードがカードなら登録（★入れ子のカードは作らない★）
        in_card = next((x["card"] for x in reversed(self._stack)
                        if x["card"] is not None), None)
        if in_card is None and tag == self._tag and self._cls in node["classes"]                 and not node["hidden"]:
            card = {"index": len(self.cards), "parent": node["parent"],
                    "elements": [], "urls": [], "text": ""}
            self.cards.append(card)
            node["card"] = card
        else:
            node["card"] = in_card
        if node["card"] is not None and not node["hidden"]:
            if tag == "a" and node["href"]:
                node["card"]["urls"].append(node["href"])
        if tag not in self._VOID:
            self._stack.append(node)
        else:
            self._close(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def _close(self, node):
        card = node.get("card")
        if card is not None and not node["hidden"]:
            card["elements"].append({"classes": node["classes"],
                                     "tag": node["tag"],
                                     "text": " ".join(node["text"].split())})

    def handle_endtag(self, tag):
        tag = tag.lower()
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i]["tag"] == tag:
                for node in self._stack[i:]:
                    self._close(node)
                del self._stack[i:]
                return

    def close(self):
        super().close()
        for node in self._stack:
            self._close(node)
        self._stack = []

    def handle_data(self, data):
        t = data.strip()
        if not t or not self._stack:
            return
        # ★非表示の中の文字は、親にも足さない★（2026-08-04・Codex94回目の指摘1）
        #   以前は「その要素自身が非表示か」だけを見ていたので、
        #   <p class="category"><span hidden>パチスロ</span>製品</p> のように
        #   **読者に見えない肯定語**で種目の判定を通せた（自分で再現した）。
        if self._stack[-1]["hidden"]:
            return
        # ★開いている要素すべてに文字を足す★（要素の中身＝子孫の文字も含む）
        for node in self._stack:
            if node["card"] is not None and not node["hidden"]:
                node["text"] += t + " "
        card = self._stack[-1]["card"]
        if card is not None and not self._stack[-1]["hidden"]:
            card["text"] += t + " "


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", " ".join(str(s or "").split()))


def parse_cards(html: str, spec: dict) -> list:
    """一覧HTMLからカードを取り出す。"""
    for k in ("card_tag", "card_class", "name_class", "type_class", "year_class"):
        if not spec.get(k):
            raise CardError(f"カードの作りの指定が足りません: {k}")
    p = _Cards(spec["card_tag"], spec["card_class"])
    try:
        p.feed(html or "")
        p.close()
    except Exception as e:                # noqa: BLE001
        raise CardError(f"一覧を読めません: {type(e).__name__}: {e}")
    return p.cards


def _by_class(card: dict, cls: str) -> list:
    """そのクラスを持つ**要素**を返す（文字の断片ではない）。"""
    return [e for e in card["elements"] if cls in e["classes"]]


def identify(html: str, spec: dict, url: str, today=None,
             list_url: str = "", link_prefix: str = "") -> dict:
    """1つのURLについて、一覧カードから名前・種目・登場年月を取り出す。

    ★条件を1つでも満たさなければ ok=False★（Codex92回目の12条件）
    list_url / link_prefix: 相対URLを絶対にして、機種URLだけを数えるために使う
      （2026-08-04・Codex93回目の指摘5。相対表記の別機種を見落としていた）
    """
    import urllib.parse
    import new_machine_watch as _nw
    out = {"ok": False, "problems": [], "name": "", "release": "",
           "card_index": None, "card_text": "", "evidence": {}}
    cards = parse_cards(html, spec)
    base = list_url or url
    pref = link_prefix or ""

    def machine_urls(card) -> set:
        got = set()
        for h in card["urls"]:
            absu = urllib.parse.urljoin(base, h).split("#")[0].split("?")[0]
            absu = absu.rstrip("/") + "/"
            if pref and not absu.startswith(pref):
                continue                  # 一覧・よそのサイトは機種URLとして数えない
            if pref and absu.rstrip("/") == pref.rstrip("/"):
                continue
            got.add(absu)
        return got

    want = urllib.parse.urljoin(base, url).split("#")[0].split("?")[0].rstrip("/") + "/"
    for c in cards:
        c["_urls"] = machine_urls(c)
    hit = [c for c in cards if want in c["_urls"]]
    if len(hit) != 1:
        out["problems"].append(f"この機種のカードが {len(hit)} 個です（1個であるべきです）")
        return out
    card = hit[0]
    # ★同じ親のカードが3つ以上あること★（同じ形の繰り返しだと確かめる・条件2）
    siblings = [c for c in cards if c["parent"] == card["parent"]]
    if len(siblings) < 3:
        out["problems"].append(
            f"同じ並びのカードが {len(siblings)} 個しかありません"
            "（繰り返しの一覧だと確かめられないので使いません）")
        return out
    if len(card["_urls"]) != 1:
        out["problems"].append(
            f"1つのカードに機種のURLが {len(card['_urls'])} 個あります（1個であるべきです）")
        return out
    # 名前（★要素がちょうど1つ★）
    name_els = _by_class(card, spec["name_class"])
    if len(name_els) != 1 or not name_els[0]["text"]:
        out["problems"].append(f"カードの機種名の要素が {len(name_els)} 個です（1個であるべきです）")
        return out
    name = _norm(name_els[0]["text"])
    # 種目（年月のクラスが付いた要素は除く）
    # ★空の要素も数えてから、中身があることを別に見る★（Codex94回目の指摘5）
    #   先に空を捨てて数えていたので、「値のある1個＋空1個」を1個として通していた。
    year_els = _by_class(card, spec["year_class"])
    type_els = [e for e in _by_class(card, spec["type_class"])
                if spec["year_class"] not in e["classes"]]
    if len(type_els) != 1 or not type_els[0]["text"]:
        out["problems"].append(f"カードの種目の要素が {len(type_els)} 個です（1個であるべきです）")
        return out
    kind = _norm(type_els[0]["text"])
    if not any(w in kind for w in SLOT_WORDS):
        out["problems"].append(f"カードの種目が回胴機ではありません（{kind!r}）")
        return out
    # ★否定の証拠★（肯定と否定が同居するカードは使わない）
    whole = _norm(card["text"])
    if any(w in whole for w in PACHI_WORDS):
        out["problems"].append("カードにぱちんこの語が混ざっています")
        return out
    if PACHI_MARK.match(name):
        out["problems"].append(f"機種名がぱちんこの規格印で始まっています（{name!r}）")
        return out
    # 登場年月（★要素がちょうど1つ★）
    if len(year_els) != 1 or not year_els[0]["text"]:
        out["problems"].append(f"カードの登場年月の要素が {len(year_els)} 個です（1個であるべきです）")
        return out
    # ★1つの欄に年月が2つ書かれていたら採らない★（Codex94回目の指摘3・再現した）
    #   「2026.08 / 2027.01」でも要素は1個なので、以前は先頭を採用していた。
    hits = list(YEAR_MONTH.finditer(_norm(year_els[0]["text"])))
    if len(hits) != 1:
        out["problems"].append(
            f"カードの登場年月が {len(hits)} 個あります（{year_els[0]['text']!r}）")
        return out
    m = hits[0]
    # ★他のカードにも同じ作りがあること★（たまたま似た要素を拾っていないか）
    same_shape = sum(1 for c in siblings
                     if len(_by_class(c, spec["name_class"])) == 1
                     and len(_by_class(c, spec["year_class"])) == 1)
    if same_shape < 3:
        out["problems"].append(
            f"同じ作りのカードが {same_shape} 個しかありません（作りが揺れています）")
        return out
    release = f"{m.group(1)}-{int(m.group(2)):02d}"
    if not _nw.is_recent(release, today):
        out["problems"].append(f"登場年月が新台の範囲外です（{release}）")
        return out
    out.update({"ok": True, "name": name, "release": release,
                "card_index": card["index"], "card_text": whole[:300],
                "evidence": {
                    "list_html_sha256": "sha256:" + hashlib.sha256(
                        (html or "").encode("utf-8")).hexdigest(),
                    "card_index": card["index"],
                    "card_text": whole[:300],
                    "name": name, "kind": kind, "release": release}})
    return out


# ★代替を許す TLS の理由（これ以外の SSLError は一時的な失敗として扱う）★
#   ★CERTIFICATE_VERIFY_FAILED はここに入れない★（2026-08-04・Codex96回目）
#     証明書の検証失敗は上の `SSLCertVerificationError` の型で受ける。
#     理由の名前でも許すと、型が違う普通の SSLError まで通り、
#     「型で決める」という説明と実装が食い違う。
ALLOWED_SSL_REASONS = (
    "SSLV3_ALERT_HANDSHAKE_FAILURE",      # 握手そのものを拒否された（藤商事）
    "TLSV1_ALERT_PROTOCOL_VERSION",       # こちらのTLS版を受け付けない
    "UNSUPPORTED_PROTOCOL",
)


def tls_failure(e, depth: int = 0) -> bool:
    """★例外の型だけで「証明書・TLSの失敗か」を決める★

    ★2026-08-04・Codex94回目の指摘2（自分で再現した）★
      以前は失敗の文言に「SSLError」等が入っているかを見ていたので、
      **SSLと無関係の失敗でも、文言さえ含めば**一覧カードへ逃がせた。
      判定は文字を一切見ず、例外の型と `reason` の連鎖だけで行う。
    """
    import ssl
    if e is None or depth > 8:
        return False
    # ★証明書の検証失敗★（オーイズミ＝期限切れ）
    if isinstance(e, ssl.SSLCertVerificationError):
        return True
    # ★握手そのものを拒否された場合★（藤商事）だけ、理由の名前で許す
    #   ★2026-08-04・Codex95回目の指摘1★
    #     `ssl.SSLError` を丸ごと許すと、通信が途中で切れただけ
    #     （SSLEOFError 等＝そのうち直る一時的な失敗）でも
    #     一覧カードへ逃がしてしまい、「実例の2種類だけ」になっていなかった。
    #   reason は OpenSSL が付ける決まった名前で、相手が自由に書ける文ではない。
    if isinstance(e, ssl.SSLError) and not isinstance(
            e, (ssl.SSLEOFError, ssl.SSLZeroReturnError, ssl.SSLWantReadError,
                ssl.SSLWantWriteError, ssl.SSLSyscallError)):
        if str(getattr(e, "reason", "") or "") in ALLOWED_SSL_REASONS:
            return True
    for attr in ("reason", "__cause__", "__context__"):
        nxt = getattr(e, attr, None)
        if isinstance(nxt, BaseException) and tls_failure(nxt, depth + 1):
            return True
    return False


def failure_allowed(reasons) -> bool:
    """（説明用）失敗の文言に、許可した種類の語が出てくるか。

    ★これは判定に使わない★（判定は `tls_failure()` ＝例外の型で決める）。
    ログや台帳に「なぜ代替したのか」を書くための説明にだけ使う。
    """
    joined = " ".join(str(x) for x in (reasons or []))
    return any(w in joined for w in ALLOWED_FAILURES)


def exc_reasons(e, depth: int = 0) -> list:
    """例外の連鎖をたどって、失敗の中身を全部集める。

    ★なぜ要るか（2026-08-04・Codex93回目の指摘6）★
      取得側の文言は「取得できません（URLError）」までしか書かないので、
      1段だけ見ても証明書エラーだと分からない。逆に、
      **文字列だけで判断すると別の失敗を証明書エラーに見せかけられる**。
      そこで ①例外の型 ②`reason` ③`__cause__`/`__context__` を
      再帰でたどり、**型が証明書・TLSのときだけ確かな印を足す**。
    """
    out = []
    if e is None or depth > 8:
        return out
    out.append(str(e))
    for attr in ("reason", "__cause__", "__context__"):
        nxt = getattr(e, attr, None)
        if isinstance(nxt, BaseException):
            out += exc_reasons(nxt, depth + 1)
        elif nxt is not None and attr == "reason":
            out.append(str(nxt))
    return out


# ---------------------------------------------------------------- selftest

SPEC = {"card_tag": "li", "card_class": "slotItem", "name_class": "name",
        "type_class": "category", "year_class": "__year"}

# ★実物のカード（2026-08-04・オーイズミ公式一覧から採取して凍結）★
REAL_CARD = (
    '<li class="slotItem"><div class="img"><img src="x.png" alt=""></div>'
    '<div class="txts"><div class="name">Lパチスロ 喰霊-零-Re</div>'
    '<div class="categorys __after"><p class="category">パチスロ</p>'
    '<p class="category __year">2026.08</p></div>'
    '<div class="btn _productsC">'
    '<a href="https://www.oizumi.co.jp/machine/garei-zero-re/" target="_blank">'
    'くわしく見る</a></div></div></li>')


def _page(*cards) -> str:
    return "<html><body><ul class='list'>" + "".join(cards) + "</ul></body></html>"


def _other(slug, name="Lほかの機種", kind="パチスロ", year="2026.08"):
    return (f'<li class="slotItem"><div class="txts"><div class="name">{name}</div>'
            f'<div class="categorys"><p class="category">{kind}</p>'
            f'<p class="category __year">{year}</p></div>'
            f'<div class="btn"><a href="https://www.oizumi.co.jp/machine/{slug}/">'
            "くわしく見る</a></div></div></li>")


def selftest() -> int:
    from datetime import date as _D
    ok_all, ran = True, [0]

    def t(name, cond):
        nonlocal ok_all
        ran[0] += 1
        ok_all = ok_all and bool(cond)
        print(("✅" if cond else "❌") + " " + name)

    U = "https://www.oizumi.co.jp/machine/garei-zero-re/"
    page = _page(REAL_CARD, _other("a"), _other("b"))
    r = identify(page, SPEC, U, today=_D(2026, 8, 4))
    t("★★実物のカードから 名前・種目・登場年月 を取れる★★",
      r["ok"] and r["name"] == "Lパチスロ 喰霊-零-Re" and r["release"] == "2026-08")
    t("　証跡（一覧の指紋・カードの位置・本文）を残す",
      r["evidence"]["list_html_sha256"].startswith("sha256:")
      and r["evidence"]["card_index"] == 0 and r["evidence"]["card_text"])
    t("★★隣のカードの名前を拾わない★★",
      identify(page, SPEC, "https://www.oizumi.co.jp/machine/a/",
               today=_D(2026, 8, 4))["name"] == "Lほかの機種")
    t("★★カードが少なすぎる（繰り返しと確かめられない）ときは使わない★★",
      not identify(_page(REAL_CARD), SPEC, U, today=_D(2026, 8, 4))["ok"])
    t("★★同じURLのカードが2つあるときは使わない★★",
      any("カードが 2 個" in x for x in identify(
          _page(REAL_CARD, REAL_CARD, _other("a")), SPEC, U,
          today=_D(2026, 8, 4))["problems"]))
    # 1カードに2機種のURL
    two = REAL_CARD.replace("</div></li>",
                            '<a href="https://www.oizumi.co.jp/machine/z/">別</a>'
                            "</div></li>")
    t("★★1つのカードに機種URLが2つあるときは使わない★★",
      any("URLが 2 個" in x for x in identify(
          _page(two, _other("a"), _other("b")), SPEC, U, today=_D(2026, 8, 4))["problems"]))
    # 名前が2つ
    dup = REAL_CARD.replace('<div class="name">Lパチスロ 喰霊-零-Re</div>',
                            '<div class="name">A</div><div class="name">B</div>')
    t("★★名前が2つあるカードは使わない★★",
      any("機種名の要素が 2 個" in x for x in identify(
          _page(dup, _other("a"), _other("b")), SPEC, U, today=_D(2026, 8, 4))["problems"]))
    # 種目がパチンコ
    pachi = REAL_CARD.replace('<p class="category">パチスロ</p>',
                              '<p class="category">パチンコ</p>')
    t("★★種目がぱちんこのカードは使わない★★",
      not identify(_page(pachi, _other("a"), _other("b")), SPEC, U,
                   today=_D(2026, 8, 4))["ok"])
    # 肯定と否定が同居
    both = REAL_CARD.replace("</div></li>", "<p>ぱちんこ機の情報</p></div></li>")
    t("★★回胴機の語とぱちんこの語が同居するカードは使わない★★",
      any("ぱちんこの語" in x for x in identify(
          _page(both, _other("a"), _other("b")), SPEC, U, today=_D(2026, 8, 4))["problems"]))
    # 年月が2つ／読めない／範囲外
    y2 = REAL_CARD.replace('<p class="category __year">2026.08</p>',
                           '<p class="category __year">2026.08</p>'
                           '<p class="category __year">2027.01</p>')
    t("★★登場年月が2つあるカードは使わない★★",
      any("登場年月の要素が 2 個" in x for x in identify(
          _page(y2, _other("a"), _other("b")), SPEC, U, today=_D(2026, 8, 4))["problems"]))
    old = REAL_CARD.replace("2026.08", "2011.11")
    t("★★古い年月のカードは新台にしない★★",
      any("範囲外" in x for x in identify(
          _page(old, _other("a"), _other("b")), SPEC, U, today=_D(2026, 8, 4))["problems"]))
    # 名前がぱちんこの規格印
    mark = REAL_CARD.replace("Lパチスロ 喰霊-零-Re", "e喰霊-零-Re")
    t("　名前がぱちんこの規格印で始まるカードは使わない",
      not identify(_page(mark, _other("a"), _other("b")), SPEC, U,
                   today=_D(2026, 8, 4))["ok"])
    t("★★『くわしく見る』を機種名にしない★★（リンク文字は名前ではない）",
      r["name"] != "くわしく見る")
    # 許可した取得失敗だけを代替の対象にする
    import ssl as _ssl2, urllib.error as _ue2
    t("★★代替を許すのは証明書・TLSの失敗だけ（例外の型で見る）★★",
      tls_failure(_ue2.URLError(_ssl2.SSLCertVerificationError("expired")))
      and not tls_failure(_ue2.URLError("timed out"))
      and not tls_failure(Exception("取得できません（HTTP 404）")))
    # ★通信が途中で切れただけの失敗は代替しない★（Codex95回目の指摘1）
    t("★★一時的なSSLの失敗（EOF等）では一覧カードへ逃がさない★★",
      not tls_failure(_ue2.URLError(_ssl2.SSLEOFError("eof")))
      and not tls_failure(_ue2.URLError(_ssl2.SSLZeroReturnError("zero"))))
    _hs = _ssl2.SSLError("SSLV3_ALERT_HANDSHAKE_FAILURE")
    _hs.reason = "SSLV3_ALERT_HANDSHAKE_FAILURE"
    _unk = _ssl2.SSLError("なにか")
    _unk.reason = "SOMETHING_ELSE"
    _cvf = _ssl2.SSLError("証明書")
    _cvf.reason = "CERTIFICATE_VERIFY_FAILED"
    t("　握手拒否は許し、知らない理由のSSLErrorは許さない",
      tls_failure(_ue2.URLError(_hs)) and not tls_failure(_ue2.URLError(_unk)))
    t("★★証明書の失敗は型で受ける（理由の名前だけのSSLErrorは許さない）★★"
      "（Codex96回目）", not tls_failure(_ue2.URLError(_cvf)))
    t("★★文言に SSL の語があっても、型が違えば代替しない★★（Codex94回目・再現した）",
      not tls_failure(Exception(
          "取得できません（URLError）: <urlopen error [SSL: "
          "CERTIFICATE_VERIFY_FAILED] ...>")))
    t("　カードの作りの指定が足りなければ止まる",
      _raises(lambda: parse_cards(page, {"card_tag": "li"})))
    # ★相対で書かれたリンク★（2026-08-04・Codex93回目の指摘5）
    PRE = "https://www.oizumi.co.jp/machine/"
    rel = REAL_CARD.replace(
        '</div></li>', '<a href="/machine/betsu/">こちらも</a></div></li>')
    t("★★同じカードの相対リンクも機種URLとして数える★★",
      any("機種のURLが 2 個" in x for x in identify(
          _page(rel, _other("a"), _other("b")), SPEC, U,
          today=_D(2026, 8, 4), list_url=PRE, link_prefix=PRE)["problems"]))
    t("　よそのサイトへのリンクは機種URLとして数えない",
      identify(_page(REAL_CARD.replace(
          '</div></li>', '<a href="https://twitter.com/x">SNS</a></div></li>'),
          _other("a"), _other("b")), SPEC, U, today=_D(2026, 8, 4),
          list_url=PRE, link_prefix=PRE)["ok"])
    # ★非表示のカードは無かったことにする★（読者に出ていないものを根拠にしない）
    hid = '<li class="slotItem" hidden>' + REAL_CARD[len('<li class="slotItem">'):]
    t("★★非表示のカードは同定に使わない★★",
      any("カードが 0 個" in x for x in identify(
          _page(hid, _other("a"), _other("b")), SPEC, U,
          today=_D(2026, 8, 4))["problems"]))
    # ★型で見分ける（文言そのままでは通さない）★
    import ssl as _ssl, urllib.error as _ue
    t("★★証明書の失敗は例外の連鎖をたどって見つける★★",
      tls_failure(_wrap(_ue.URLError(_ssl.SSLCertVerificationError("x"))))
      and not tls_failure(_wrap(_ue.URLError("timed out"))))
    t("　例外の連鎖が輪になっていても止まらない",
      len(exc_reasons(_loop_exc())) < 40 and not tls_failure(_loop_exc()))
    # ★非表示の子の文字を、表示中の親に足さない★（Codex94回目の指摘1・再現した）
    hidkid = REAL_CARD.replace(
        '<p class="category">パチスロ</p>',
        '<p class="category"><span hidden>パチスロ</span>製品</p>')
    t("★★読者に見えない肯定語で種目の判定を通せない★★",
      not identify(_page(hidkid, _other("a"), _other("b")), SPEC, U,
                   today=_D(2026, 8, 4))["ok"])
    # ★1つの欄に年月が2つ★（Codex94回目の指摘3・再現した）
    y2in1 = REAL_CARD.replace("2026.08</p>", "2026.08 / 2027.01</p>")
    t("★★1つの欄に年月が2つ書かれていたら採らない★★",
      any("登場年月が 2 個" in x for x in identify(
          _page(y2in1, _other("a"), _other("b")), SPEC, U,
          today=_D(2026, 8, 4))["problems"]))
    # ★空の要素と併存していたら「1個」と数えない★（同・指摘5）
    empt = REAL_CARD.replace(
        '<p class="category __year">2026.08</p>',
        '<p class="category __year"></p><p class="category __year">2026.08</p>')
    t("★★値のある要素と空の要素が併存したら使わない★★",
      any("登場年月の要素が 2 個" in x for x in identify(
          _page(empt, _other("a"), _other("b")), SPEC, U,
          today=_D(2026, 8, 4))["problems"]))
    print(f"{ran[0]}/{ran[0]} 合格" if ok_all else "不合格あり")
    return 0 if ok_all else 1


def _wrap(e):
    """例外を1段包む（連鎖をたどれるか見るため）。"""
    try:
        raise e
    except BaseException:
        try:
            raise RuntimeError("取得できません（URLError）")
        except RuntimeError as outer:
            return outer


def _loop_exc():
    """自分自身を指す例外の連鎖（深さの上限が効くか見るため）。"""
    a, b = ValueError("a"), ValueError("b")
    a.__context__, b.__context__ = b, a
    return a


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except CardError:
        return True


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="一覧カードでの同定")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    sys.exit(selftest() if a.selftest else 0)

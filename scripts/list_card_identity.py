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
    """指定したタグ・クラスの要素を「カード」として集める。"""

    def __init__(self, card_tag: str, card_class: str):
        super().__init__(convert_charrefs=True)
        self._tag, self._cls = card_tag.lower(), card_class
        self._stack: list = []
        self._open: int | None = None
        self.cards: list = []

    @staticmethod
    def _attrs(attrs) -> dict:
        return {(k or "").lower(): (v or "") for k, v in attrs}

    _VOID = {"img", "br", "hr", "input", "meta", "link", "source", "area"}

    def handle_starttag(self, tag, attrs):
        a = self._attrs(attrs)
        classes = set((a.get("class") or "").split())
        if self._open is None and tag.lower() == self._tag and self._cls in classes:
            self.cards.append({"urls": [], "by_class": {}, "text": "",
                               "index": len(self.cards)})
            self._open = len(self._stack)
        if self._open is not None and self.cards:
            c = self.cards[-1]
            if tag.lower() == "a" and a.get("href"):
                c["urls"].append(a["href"].strip())
            if classes:
                # クラスごとの本文を集める（あとで名前・種目・年月を取り出す）
                self._cur_classes = classes
                c.setdefault("_open_classes", []).append(
                    (len(self._stack), frozenset(classes)))
        if tag.lower() not in self._VOID:
            self._stack.append(tag.lower())

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i] == tag.lower():
                del self._stack[i:]
                break
        if self._open is not None and len(self._stack) <= self._open:
            self._open = None
        if self.cards and self.cards[-1].get("_open_classes"):
            self.cards[-1]["_open_classes"] = [
                x for x in self.cards[-1]["_open_classes"]
                if x[0] < len(self._stack)]

    def handle_data(self, data):
        t = data.strip()
        if not t or self._open is None or not self.cards:
            return
        c = self.cards[-1]
        c["text"] += t + " "
        for _depth, classes in (c.get("_open_classes") or []):
            for cl in classes:
                c["by_class"].setdefault(cl, []).append(t)


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


def identify(html: str, spec: dict, url: str, today=None) -> dict:
    """1つのURLについて、一覧カードから名前・種目・登場年月を取り出す。

    ★条件を1つでも満たさなければ ok=False★（Codex92回目の12条件）
    """
    import new_machine_watch as _nw
    out = {"ok": False, "problems": [], "name": "", "release": "",
           "card_index": None, "card_text": "", "evidence": {}}
    cards = parse_cards(html, spec)
    if len(cards) < 3:
        out["problems"].append(
            f"カードが {len(cards)} 個しかありません（同じ形の繰り返しとして"
            "確かめられないので使いません）")
        return out
    want = url.rstrip("/") + "/"
    hit = []
    for c in cards:
        urls = {u.split("#")[0].split("?")[0].rstrip("/") + "/" for u in c["urls"]}
        urls = {u for u in urls if u.startswith("http")}
        c["_urls"] = urls
        if want in urls:
            hit.append(c)
    if len(hit) != 1:
        out["problems"].append(f"この機種のカードが {len(hit)} 個です（1個であるべきです）")
        return out
    card = hit[0]
    if len(card["_urls"]) != 1:
        out["problems"].append(
            f"1つのカードに機種のURLが {len(card['_urls'])} 個あります（1個であるべきです）")
        return out
    # 名前
    names = [_norm(x) for x in card["by_class"].get(spec["name_class"], []) if _norm(x)]
    if len(names) != 1:
        out["problems"].append(f"カードの機種名が {len(names)} 個です（1個であるべきです）")
        return out
    name = names[0]
    # 種目（年月のクラスが付いている要素は除く）
    types = [_norm(x) for x in card["by_class"].get(spec["type_class"], []) if _norm(x)]
    years_raw = [_norm(x) for x in card["by_class"].get(spec["year_class"], []) if _norm(x)]
    types = [t for t in types if t not in years_raw]
    if len(types) != 1:
        out["problems"].append(f"カードの種目が {len(types)} 個です（1個であるべきです）")
        return out
    kind = types[0]
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
    # 登場年月
    if len(years_raw) != 1:
        out["problems"].append(f"カードの登場年月が {len(years_raw)} 個です（1個であるべきです）")
        return out
    m = YEAR_MONTH.search(years_raw[0])
    if not m:
        out["problems"].append(f"カードの登場年月を読めません（{years_raw[0]!r}）")
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


def failure_allowed(reasons) -> bool:
    """代替を許す取得失敗か（許可した種類だけ）。"""
    joined = " ".join(str(x) for x in (reasons or []))
    return any(w in joined for w in ALLOWED_FAILURES)


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
      any("機種名が 2 個" in x for x in identify(
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
      any("登場年月が 2 個" in x for x in identify(
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
    t("★★代替を許すのは証明書・TLSの失敗だけ★★",
      failure_allowed(["公式ページを取得できません: 取得できません（URLError）: "
                       "<urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] ...>"])
      and not failure_allowed(["公式ページを取得できません: 取得できません（HTTP 404）"])
      and not failure_allowed(["パチスロのページに見えません（回胴機の語が無い）"]))
    t("　カードの作りの指定が足りなければ止まる",
      _raises(lambda: parse_cards(page, {"card_tag": "li"})))
    print(f"{ran[0]}/{ran[0]} 合格" if ok_all else "不合格あり")
    return 0 if ok_all else 1


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

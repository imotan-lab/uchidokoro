# -*- coding: utf-8 -*-
"""dmm_calendar.py — DMMぱちタウンの新台カレンダーから、パチスロの新台を見つける。

★なぜDMMなのか（2026-08-16・台帳#376）★
  これまではP-WORLDの導入カレンダーを唯一の入口にしていましたが、
  **P-WORLDの利用規約がプログラムによるアクセスとデータ収集を禁止**しています
  （第8条）。一撃も同様に禁止していました。運営者の決定でDMMへ移しました。
  ★通信そのものは blocked_hosts.py が止めます★（最後の砦）。

★何を取るか★
  https://p-town.dmm.com/machines/new_calendar?year=YYYY&month=M

  素のHTMLに構造化されて入っています（JavaScriptで描いていません）。

    <div class="title"><a class="link" href="/machines/5038">Ｌすーぱぁびん娘</a></div>
    <div class="date">導入開始日：2026年08月03日</div>
    <div class="iconara"><span class="text-icon -slot -narrow">パチスロ</span></div>

  ★機種名・機種ID・導入日（日まで）・パチスロ/パチンコの別★がそろいます。

★守る線★
  ①**パチスロだけ**を返す（パチンコは扱わない）
  ②**1件でも形が読めなければ、その月は「読めなかった」とする**
    （欠けたまま「新台なし」と扱うと、見つけた新台を取りこぼす）
  ③要求した年月と、ページが示す年月が違えば止める
  ④同じIDが2回出てきたら止める（数え違いの元）
  ★「読めた範囲では新台なし」までしか言わない★

使い方:
    python scripts/dmm_calendar.py --year 2026 --month 9
    python scripts/dmm_calendar.py --selftest
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import blocked_hosts as _bh          # noqa: E402
import new_machine_watch as _w       # noqa: E402

URL = "https://p-town.dmm.com/machines/new_calendar?year=%d&month=%d"

# ★1件ぶんの形★（この形に合わないカードが1つでもあれば「読めなかった」）
_ITEM = re.compile(
    r'<li class="item">\s*'
    r'<div class="title"><a class="link" href="/machines/(?P<id>\d+)">'
    r'(?P<name>[^<]+)</a></div>\s*'
    r'<div class="date">導入開始日：(?P<y>\d{4})年(?P<m>\d{2})月(?P<d>\d{2})日</div>\s*'
    r'<div class="iconara"><span class="(?P<cls>[^"]*)">(?P<kind>[^<]+)</span>',
    re.S)
# ★カードの数を別の見方でも数える★（欠けに気づくため）
#   ★IDの形を要求しない★（2026-08-16・Codex依頼212の指摘7）
#   前は `href="/machines/\d+"` まで求めていたので、**リンクが壊れたカードは
#   そもそも「カード」として数えられず、数が合ってしまい黙って消えて**いました。
#   ここは「機種カードらしき箱の数」だけを数え、中身が読めるかは _ITEM に任せる。
#   （素の `<li class="item">` は店舗一覧にも使われるので、見出しの形まで見る）
_ITEM_HEAD = re.compile(r'<li class="item">\s*<div class="title">'
                        r'<a class="link" href="')
# ★ページ自身が名乗る月★（「2026年09月の導入機種」）
_MONTH_HEAD = re.compile(r"(\d{4})年(\d{2})月の導入機種")


class CalendarError(Exception):
    """カレンダーを読めない（★「新台なし」と扱わない★）。"""


def _slot(cls: str, kind: str) -> bool:
    """パチスロか。★印と文字が食い違えば止める★

    ★orで見てはいけない★（2026-08-16・Codex依頼212の指摘7／実測で確認）
      前は「印が -slot」または「文字に『スロ』」のどちらかで真としていました。
      これだと **印はパチンコなのに文字はパチスロ**（またはその逆）という
      矛盾した表示をそのまま通してしまいます。
      種別を取り違えるとパチンコの記事を作りかねないので、
      **食い違ったら『読めなかった』ことにして止めます**（迷ったら止める）。
    """
    by_mark = "-slot" in str(cls or "")
    by_text = "スロ" in str(kind or "")
    if by_mark != by_text:
        raise CalendarError(
            f"パチスロ／パチンコの印と文字が食い違います"
            f"（印=「{str(cls or '')[:30]}」 文字=「{str(kind or '')[:20]}」）"
            "／★どちらか分からないので使いません★")
    return by_mark


def parse(html: str, year: int, month: int) -> list:
    """1か月ぶんのカードを読む。★1件でも形が違えば例外★"""
    if not html:
        raise CalendarError("カレンダーが空です")
    # ③ 要求した年月のページか
    #   ★ページ自身が名乗っている月と突き合わせる★（2026-08-16）
    #   「2026年09月の導入機種」という見出しがあるので、それを正とする。
    #   URLだけを信じると、相手が別の月を返しても気づけない。
    m_head = _MONTH_HEAD.search(html)
    if not m_head:
        raise CalendarError(
            "ページに『◯年◯月の導入機種』の見出しがありません"
            "／★どの月を見ているか確かめられないので使いません★")
    got_y, got_m = int(m_head.group(1)), int(m_head.group(2))
    if (got_y, got_m) != (year, month):
        raise CalendarError(
            f"要求した年月と、ページが示す年月が違います"
            f"（要求 {year}年{month}月 ／ ページ {got_y}年{got_m}月）")
    heads = len(_ITEM_HEAD.findall(html))
    got = list(_ITEM.finditer(html))
    if heads != len(got):
        # ② 形の違うカードがある＝欠けている恐れ
        raise CalendarError(
            f"カードの形が読めません（見出し{heads}件に対し読めたのは{len(got)}件）"
            "／★欠けたまま『新台なし』と扱いません★")
    out, seen = [], set()
    for m in got:
        mid = m.group("id")
        if mid in seen:                    # ④ 同じIDが2回
            raise CalendarError(f"同じ機種IDが2回出ています: {mid}")
        seen.add(mid)
        if not _slot(m.group("cls"), m.group("kind")):
            continue                       # ① パチンコは扱わない
        # ★実在する日か確かめる★（2026-08-16・Codex依頼212の指摘3）
        #   形だけ見ていると 2026-02-31 のような日も通ります（実測で確認）。
        try:
            _dt.date(int(m.group("y")), int(m.group("m")), int(m.group("d")))
        except ValueError:
            raise CalendarError(
                f"導入日が実在しません: {m.group('y')}年{m.group('m')}月"
                f"{m.group('d')}日（{m.group('name')[:24]}）")
        out.append({
            "id": mid,
            "url": "https://p-town.dmm.com/machines/%s" % mid,
            "name": _w._norm_name(m.group("name")) if hasattr(_w, "_norm_name")
            else " ".join(m.group("name").split()),
            "release_date": "%s-%s-%s" % (m.group("y"), m.group("m"),
                                          m.group("d")),
            "kind": " ".join(m.group("kind").split()),
        })
    return out


def fetch(year: int, month: int, get=None) -> list:
    """その月の新台（パチスロだけ）。"""
    u = URL % (year, month)
    _bh.check(u)                           # ★禁止先なら通信しない★
    try:
        html = (get or _w._get)(u)
    except Exception as e:                 # noqa: BLE001
        raise CalendarError(f"カレンダーを取得できません（{u}）: {str(e)[:90]}")
    return parse(html, year, month)


def months_ahead(today, ahead: int = 3) -> list:
    """今月から先を何か月ぶん見るか（★先の月にしか無い新台を拾う★）。"""
    out = []
    y, m = today.year, today.month
    for _ in range(max(1, ahead) + 1):
        out.append((y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


# ---------------------------------------------------------------- selftest

def _fixture() -> str:
    p = os.path.join(BASE, "tests", "fixtures", "dmm_calendar_2026_09.html")
    if not os.path.isfile(p):
        return ""
    import io
    return io.open(p, encoding="utf-8").read()


def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅ " if cond else "❌ ") + name)

    html = _fixture()
    if not html:
        t("★試験用の保存ページがありません（tests/fixtures）★", False)
    else:
        got = parse(html, 2026, 9)
        t("★★保存したページからパチスロの新台を読める★★（ネットに出ない試験）",
          len(got) >= 5)
        names = {g["name"] for g in got}
        t("　実在の機種が入っている（Lパチスロ 彼女、お借りします）",
          any("彼女" in n for n in names))
        t("　機種IDと導入日がそろっている",
          all(g["id"].isdigit() and re.match(r"^\d{4}-\d{2}-\d{2}$",
                                             g["release_date"]) for g in got))
        t("★★パチンコは返さない★★",
          all("スロ" in g["kind"] or "-slot" in g["kind"] for g in got)
          or all("パチンコ" not in g["kind"] for g in got))
        t("　URLは機種ページの形",
          all(g["url"].startswith("https://p-town.dmm.com/machines/")
              for g in got))

        # ★読めないときに「新台なし」と言わないこと★
        def raises(fn):
            try:
                fn()
                return False
            except CalendarError:
                return True

        t("★★1件でも形が違えば読めなかったことにする★★"
          "（欠けたまま『新台なし』と扱わない）",
          raises(lambda: parse(
              html.replace('<div class="date">導入開始日：', '<div class="date">x',
                           1), 2026, 9)))
        t("★★別の月のページなら止める★★", raises(lambda: parse(html, 2026, 10)))
        t("　空なら止める", raises(lambda: parse("", 2026, 9)))
        # ★1件ぶんをまるごと複製して、同じIDが2回出る形を作る★
        #   ★`<li class="item">` は店舗リンク等にも使われている★ので、
        #   機種カードの先頭を正規表現で探す（見た目が同じでも中身が違う）。
        _s = _ITEM_HEAD.search(html).start()
        _e = html.index("</li>", _s) + len("</li>")
        _dup = html[:_e] + html[_s:_e] + html[_e:]
        t("★★同じ機種IDが2回出たら止める★★（数え違いの元）",
          raises(lambda: parse(_dup, 2026, 9)))
        # ★★リンクが壊れたカードが黙って消えないこと★★
        #   （2026-08-16・Codex依頼212の指摘7）
        #   前は数える側もIDの形を要求していたので、壊れたカードは
        #   「無かったこと」になり、数が合って素通りしていた。
        # ★試験はパチスロのカードを狙う★（パチンコのカードは読み飛ばすので、
        #   そこを壊しても止まらない＝試験が試験になっていないことになる）
        _slotm = next(m for m in _ITEM.finditer(html)
                      if "-slot" in m.group("cls"))
        _card = html[_slotm.start():_slotm.end()]

        def _swap(old, new):
            return html.replace(_card, _card.replace(old, new, 1), 1)

        t("★★機種リンクが壊れたカードは『読めなかった』にする★★"
          "（黙って1件消えないこと）",
          raises(lambda: parse(_swap('href="/machines/',
                                     'href="/machines/x'), 2026, 9)))
        t("★★種別の印と文字が食い違えば止める★★（取り違えるとパチンコの記事を作る）",
          raises(lambda: parse(_swap(">%s</span>" % _slotm.group("kind"),
                                     ">パチンコ</span>"), 2026, 9)))
        t("★★導入日が実在しない日なら止める★★（2026年02月31日）",
          raises(lambda: parse(_swap(
              "%s年%s月%s日" % (_slotm.group("y"), _slotm.group("m"),
                             _slotm.group("d")),
              "2026年02月31日"), 2026, 9)))

    t("★★規約で禁止された先には通信しない★★（P-WORLDのカレンダー）",
      _bh.is_blocked(
          "https://www.p-world.co.jp/database/machine/introduce_calendar.cgi"))
    import datetime
    ms = months_ahead(datetime.date(2026, 11, 15), 2)
    t("　年をまたいで先の月を見られる",
      ms == [(2026, 11), (2026, 12), (2027, 1)])

    ng = sum(1 for _, o in results if not o)
    print()
    print("%d/%d 合格" % (len(results) - ng, len(results)))
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="DMMの新台カレンダー")
    ap.add_argument("--year", type=int)
    ap.add_argument("--month", type=int)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if a.selftest:
        return selftest()
    if not (a.year and a.month):
        import datetime
        today = datetime.date.today()
        a.year, a.month = today.year, today.month
    try:
        got = fetch(a.year, a.month)
    except CalendarError as e:
        print("★" + str(e) + "★")
        return 1
    print("%d年%d月のパチスロ新台: %d件" % (a.year, a.month, len(got)))
    for g in got:
        print("  %-34s %s  %s" % (g["name"][:32], g["release_date"], g["url"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

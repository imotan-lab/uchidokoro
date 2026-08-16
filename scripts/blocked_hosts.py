# -*- coding: utf-8 -*-
"""blocked_hosts.py — ★規約で自動取得を禁じているサイトへ、機械が通信しない★

★なぜ要るか（2026-08-16・台帳#376）★
  P-WORLD と 一撃 は、利用規約で**プログラムによるアクセスとデータ収集を
  明確に禁止**しています。

    P-WORLD 総合利用規約 第8条
      「プログラムを用いたアクセスなど、通常のブラウザアクセス以外の方法で
        本サービスを利用する行為」
      「当社の許可なく、本サービスを通じて配信されるデータ等を収集する行為、
        またはデータ等の収集が疑われるアクセス」
    一撃 利用規約 第3条
      「自動化ツール・ボット・スクレイピング等により本サイトの情報を
        大量取得・再配布する行為（当社が明示的に許可した場合を除く）」

  ★robots.txt は許可していました★（P-WORLDは `User-Agent: * allow: /`）。
  機械の合図と文章の規約が食い違っていますが、**規約が優先**です。

★これは最後の砦です★
  巡回設定・出典レジストリからも外しますが、**設定を1か所消し忘れても
  通信だけは起きない**ようにするのがこの器の役目です。
  ★止めるのは機械の取得だけ★＝人がブラウザで見るのは自由です。

★外すときは運営者の判断で★
  許諾が取れたらここから外します。**コードの都合で勝手に外さないこと。**

使い方:
    import blocked_hosts as _bh
    _bh.check(url)          # 禁止先なら BlockedHostError
    _bh.is_blocked(url)     # 真偽だけ
    python scripts/blocked_hosts.py --selftest
"""
from __future__ import annotations

import argparse
import sys
import urllib.parse

# ★機械が取得してはいけないホスト★（サブドメインも含めて止める）
BLOCKED = {
    "p-world.co.jp": "P-WORLD 総合利用規約 第8条（プログラムを用いたアクセス・"
                     "データ収集の禁止）／2026-08-16・台帳#376",
    "1geki.jp": "一撃 利用規約 第3条（自動化ツール・ボット・スクレイピングによる"
                "大量取得・再配布の禁止）／2026-08-16・台帳#376",
}


class BlockedHostError(Exception):
    """規約で自動取得を禁じられている先（★取りに行かない★）。"""


def host_of(url: str) -> str:
    return (urllib.parse.urlsplit(str(url or "")).hostname or "").lower()


def reason(url: str) -> str:
    """止める理由（止めないなら空文字）。"""
    h = host_of(url)
    if not h:
        return ""
    for bad, why in BLOCKED.items():
        # ★サブドメインも止める★（www. だけ書いても www2. が通らないように）
        if h == bad or h.endswith("." + bad):
            return why
    return ""


def is_blocked(url: str) -> bool:
    return bool(reason(url))


def check(url: str) -> None:
    """★取りに行く直前に呼ぶ★（禁止先なら例外）。"""
    why = reason(url)
    if why:
        raise BlockedHostError(
            f"規約で自動取得が禁止されているサイトです（{host_of(url)}）: {why}"
            "／★機械では取りに行きません★（人がブラウザで見るのは自由です）")


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅ " if cond else "❌ ") + name)

    t("★★P-WORLDを止める★★",
      is_blocked("https://www.p-world.co.jp/machine/database/10510")
      and is_blocked("https://p-world.co.jp/info/ranks.htm"))
    t("★★一撃を止める★★", is_blocked("https://1geki.jp/slot/l_akame2/"))
    t("　サブドメインも止める", is_blocked("https://idn.p-world.co.jp/x"))
    t("★★使ってよい名鑑は止めない★★",
      not is_blocked("https://p-town.dmm.com/machines/4709")
      and not is_blocked("https://chonborista.com/slot/x/1/")
      and not is_blocked("https://nana-press.com/kaiseki/machine/1/"))
    t("　名前が似ているだけの別ホストは止めない",
      not is_blocked("https://p-world.co.jp.example.com/x")
      and not is_blocked("https://my1geki.jp/x"))
    t("　URLでないものは止めない", not is_blocked("") and not is_blocked(None))

    ok = False
    try:
        check("https://www.p-world.co.jp/x")
    except BlockedHostError as e:
        ok = "台帳#376" in str(e)
    t("★★止めるときは理由を言う★★（あとから外す判断ができるように）", ok)
    t("　通ってよい先では例外にならない",
      check("https://p-town.dmm.com/x") is None)

    ng = sum(1 for _, o in results if not o)
    print()
    print("%d/%d 合格" % (len(results) - ng, len(results)))
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="規約で禁止された取得先の見張り")
    ap.add_argument("--url")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if a.selftest:
        return selftest()
    if not a.url:
        print("止めている先:")
        for h, w in BLOCKED.items():
            print(f"  {h}\n    {w}")
        return 0
    why = reason(a.url)
    print(("★止めます★ " + why) if why else "通ってよい先です")
    return 3 if why else 0


if __name__ == "__main__":
    raise SystemExit(main())

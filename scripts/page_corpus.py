# -*- coding: utf-8 -*-
"""★1機種ぶんの「読んだページの集合」を作る★（2026-09-02・台帳#542）

★なぜ要るか★（実測・2026-09-01）
  なな徹は機種ページの下に**記事ページが分かれて**いる。
    スマスロ モンキーターンV（機種644）… 本体13412字 ／ ★下位49本★
    先頭12本だけで「モード」324回（本体は127回）
  材料集めは**本体しか取っていなかった**ので、
  ★モード・ゾーンのように下位ページに詳しく書かれるものは一度も読まれない★。

★この道具がやること★＝集めるだけ。**読み取らない・判定しない**。
  値や意味を決めるのは2AI（運営者の決まり）。

★守る線★
  1. ★同じ機種IDの下位ページだけ★（別機種へ広がらない）
  2. ★1本でも取れなければ「読めていない」★（`complete=False`）＝
     欠けたまま2AIに判断させない
  3. ★上限を超えたら「読めていない」★（部分的に読んで判断させない）
  4. ★票には数えない★＝同じ名鑑の下位ページは何本あっても1系列。
     数えるのは `source_lineage.independent()` の仕事で、ここは触らない
  5. ★集合が変われば指紋が変わる★＝以前の判定を無効にできる
     （「モードはありません」は、下位ページが1本増えただけで覆るため）
"""
from __future__ import annotations

import hashlib
import re
import sys

# ★自分の出力の文字の扱いを固定する★（2026-08-24・罠⑪）
#   Windowsの既定は cp932 なので、合格の記号で落ちる。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                            # noqa: BLE001
    pass

# ★1機種あたりの上限★（実測の最大は49本＝モンキーターンV）
#   ★超えたら「読めていない」にする★＝途中まで読んで判断させない。
DEFAULT_MAX_SUB = 80


def _norm(url: str) -> str:
    """★同じページを2度数えないための正規化★（末尾スラッシュだけ）。

    ★ここで大文字小文字やクエリを触らない★＝
      別のページを同じものと見なす危険のほうが大きい。
    """
    u = str(url or "").strip()
    return u[:-1] if u.endswith("/") else u


def machine_id(catalog: dict, url: str) -> str:
    """機種ページのURLから機種IDを取り出す（取れなければ空）。

    ★名鑑ごとの `machine_id_pattern`（丸括弧1つ）で決める★＝
      道具の中に名鑑ごとの分岐を書かない。
    """
    pat = str((catalog or {}).get("machine_id_pattern") or "")
    if not pat:
        return ""
    try:
        m = re.match(pat, str(url or ""))
    except re.error:
        return ""
    if not m or not m.groups():
        return ""
    return m.group(1)


def sub_urls(catalog: dict, machine_url: str, html: str,
             max_sub: int = DEFAULT_MAX_SUB):
    """★本体ページのHTMLから、同じ機種の下位ページURLを集める★

    返り: (URLの並び, 断る理由)
      理由が空でなければ、その機種は「読めていない」扱いにする。

    ★下位ページを持たない名鑑は空を返す★（理由も空＝正常）。
    """
    pat = str((catalog or {}).get("sub_page_pattern") or "")
    if not pat:
        return [], ""                      # ★下位ページの決まりが無い名鑑★
    mid = machine_id(catalog, machine_url)
    if not mid:
        return [], "機種ページのURLから機種IDを取り出せません"
    try:
        rx = re.compile(pat.replace("{id}", re.escape(mid)))
    except re.error as e:                                    # noqa: BLE001
        return [], f"下位ページの決まりが読めません（{e}）"

    out, seen = [], {_norm(machine_url)}
    for m in re.finditer(r'href=["\']([^"\']+)["\']', str(html or "")):
        u = m.group(1).strip()
        if not rx.match(u):
            continue
        k = _norm(u)
        if k in seen:
            continue
        seen.add(k)
        out.append(u)
        if len(out) > max_sub:
            # ★超えたら断る★＝途中まで読んで2AIに判断させない
            return [], (f"下位ページが上限（{max_sub}本）を超えました"
                        "＝全部は読めていません")
    return out, ""


def manifest(pages: dict, complete: bool) -> dict:
    """★読んだページの集合★を、あとで突き合わせられる形にする。

    pages … {URL: 見える文字}
    ★指紋はURLの集合と各本文から作る★＝
      URLが1本増えても、本文が書き換わっても、指紋が変わる。
    """
    items = sorted((_norm(u), _fp(t)) for u, t in (pages or {}).items())
    h = hashlib.sha256()
    for u, f in items:
        h.update(u.encode("utf-8"))
        h.update(b"\0")
        h.update(f.encode("utf-8"))
        h.update(b"\n")
    return {
        "urls": [u for u, _ in items],
        "page_fp": {u: f for u, f in items},
        "complete": bool(complete),
        "fp": "sha256:" + h.hexdigest(),
    }


def _fp(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def same_corpus(saved: dict, now: dict) -> bool:
    """★以前の判定を、いまも使ってよいか★

    ★指紋が違えば使わない★＝
      「モードはありません」は、下位ページが1本増えただけで覆る。
    ★どちらかが「読めていない」なら使わない★
    """
    if not isinstance(saved, dict) or not isinstance(now, dict):
        return False
    if not saved.get("complete") or not now.get("complete"):
        return False
    a, b = str(saved.get("fp") or ""), str(now.get("fp") or "")
    return bool(a) and a == b


def selftest() -> int:
    ok, cases = 0, []

    def t(name, cond):
        nonlocal ok
        cases.append(name)
        if cond:
            ok += 1
        print(("✅" if cond else "❌") + " " + name)

    NANA = {
        "machine_id_pattern": r"^https://nana-press\.com/kaiseki/machine/(\d+)/?$",
        "sub_page_pattern": r"^https://nana-press\.com/kaiseki/machine/{id}/\d+/?$",
    }
    ROOT = "https://nana-press.com/kaiseki/machine/644/"
    HTML = (
        '<a href="https://nana-press.com/kaiseki/machine/644/18017/">A</a>'
        '<a href="https://nana-press.com/kaiseki/machine/644/18039/">B</a>'
        '<a href="https://nana-press.com/kaiseki/machine/644/18017/">A again</a>'
        # ★別機種★
        '<a href="https://nana-press.com/kaiseki/machine/191/12345/">別</a>'
        # ★本体そのもの★
        '<a href="https://nana-press.com/kaiseki/machine/644/">本体</a>'
        # ★関係ないページ★
        '<a href="https://nana-press.com/kaiseki/index/machine/s/a/">索引</a>'
    )
    got, why = sub_urls(NANA, ROOT, HTML)
    t("下位ページを見つける", len(got) == 2 and why == "")
    t("★別機種の下位ページは拾わない★",
      all("/644/" in u for u in got))
    t("　同じURLを2度返さない", len(set(got)) == len(got))
    t("　本体そのものは下位に数えない", ROOT not in got)
    t("　索引は拾わない", not any("index" in u for u in got))

    t("★機種IDを取り出せなければ断る★",
      sub_urls(NANA, "https://nana-press.com/kaiseki/", HTML)[1] != "")
    t("　下位ページの決まりが無い名鑑は、空を返して黙る",
      sub_urls({"machine_id_pattern": NANA["machine_id_pattern"]},
               ROOT, HTML) == ([], ""))

    many = "".join(
        f'<a href="https://nana-press.com/kaiseki/machine/644/{i}/">x</a>'
        for i in range(1, 12))
    t("★上限を超えたら断る★（途中まで読んで判断させない）",
      sub_urls(NANA, ROOT, many, max_sub=5)[1] != "")
    t("　上限ちょうどは通す", sub_urls(NANA, ROOT, many, max_sub=11)[1] == "")

    m1 = manifest({ROOT: "あ", ROOT + "18017/": "い"}, True)
    m2 = manifest({ROOT: "あ", ROOT + "18017/": "い"}, True)
    m3 = manifest({ROOT: "あ", ROOT + "18017/": "★変わった★"}, True)
    m4 = manifest({ROOT: "あ", ROOT + "18017/": "い",
                   ROOT + "99999/": "う"}, True)
    t("　同じ集合なら同じ指紋", m1["fp"] == m2["fp"])
    t("★本文が変われば指紋も変わる★", m1["fp"] != m3["fp"])
    t("★ページが1本増えれば指紋も変わる★", m1["fp"] != m4["fp"])
    t("　末尾スラッシュの違いは同じ扱い",
      manifest({ROOT: "あ"}, True)["fp"]
      == manifest({ROOT.rstrip("/"): "あ"}, True)["fp"])

    t("　同じ集合なら以前の判定を使える", same_corpus(m1, m2) is True)
    t("★集合が変われば以前の判定を使わない★", same_corpus(m1, m4) is False)
    t("★読めていない側があれば使わない★",
      same_corpus(manifest({ROOT: "あ"}, False),
                  manifest({ROOT: "あ"}, True)) is False)
    t("　壊れた入力でも使わない側に倒れる",
      same_corpus(None, m1) is False and same_corpus(m1, "x") is False)

    print(f"\n{ok}/{len(cases)} 合格")
    return 0 if ok == len(cases) else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    print("使い方: python scripts/page_corpus.py --selftest")

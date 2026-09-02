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


def is_missing(exc) -> bool:
    """★そもそも相手のサイトに無いページか★（2026-09-02）

    ★文字の照合で見分けない★＝例外が持つ状態番号で決める
    （メッセージの書き方が変わると壊れるため）。
    """
    return getattr(exc, "status", None) == 404


def collect(catalog: dict, machine_url: str, fetch, text_of,
            max_sub: int = DEFAULT_MAX_SUB, missing=None) -> dict:
    """★1機種ぶんの証拠の集合を組み立てる★（2026-09-02・台帳#542）

    fetch(url)   … ページを取る（例外を投げてよい）
    text_of(pg)  … 取れたページから「見える文字」を出す

    返り: {"pages": {URL: 文字}, "manifest": {...}, "why": 断る理由}

    ★★本文は返すだけで、保存しない★★（CLAUDE.mdの決まり・台帳#378）
      このリポジトリは公開なので他サイトの本文は置けない。
      なな徹の規約にも複製の条項がある。
      ★残すのは指紋だけ★＝それで「集合が変わったか」は分かる。

    ★★1本でも取れなければ complete=False★★（Codexのレビュー34）
      ★既存の材料経路は「取れたページだけ残して続行」する★が、
      ここでは**流用しない**。欠けた証拠束で2AIに
      「モードはありません」と判断させるのは危険なため。
    """
    pages, why = {}, ""
    try:
        root = fetch(machine_url)
    except Exception as e:                                   # noqa: BLE001
        return {"pages": {}, "manifest": manifest({}, False),
                "why": f"機種ページを取れません（{str(e)[:80]}）"}
    pages[machine_url] = text_of(root)

    subs, why = sub_urls(catalog, machine_url, getattr(root, "cleaned_html", ""),
                         max_sub=max_sub)
    if why:
        # ★上限超過など★＝ここで止める（部分的な束を作らない）
        return {"pages": {}, "manifest": manifest({}, False), "why": why}

    _missing = missing or is_missing
    gone = []
    for u in subs:
        try:
            pg = fetch(u)
        except Exception as e:                               # noqa: BLE001
            if _missing(e):
                # ★★そもそも相手のサイトに無い★★（2026-09-02・本物で踏んだ）
                #   ★読まなくても証拠は欠けていない★＝
                #   リンクは在るがページが無い、は相手側の事情。
                #   ★黙って無かったことにしない★＝記録に残すので、
                #   相手が復活させたら指紋が変わって聞き直しになる。
                gone.append(u)
                continue
            # ★1本でも「読めなかった」なら、その機種は「読めていない」★
            return {"pages": {}, "manifest": manifest({}, False),
                    "why": f"下位ページを取れません（{u} … {str(e)[:60]}）"}
        pages[u] = text_of(pg)

    return {"pages": pages, "manifest": manifest(pages, True, gone), "why": ""}


def quick_check(catalog: dict, machine_url: str, fetch, text_of,
                saved: dict, max_sub: int = DEFAULT_MAX_SUB) -> tuple:
    """★1ページだけ見て、前の判断をそのまま使えるかを決める★

    （2026-09-02・運営者の判断「それでいいよ」）

    返り: (答え, 理由)
      "SAME"       … 前の判断をそのまま使ってよい
      "CHANGED"    … 集合が変わった＝集め直して聞き直す
      "UNREADABLE" … 本体すら読めない＝判断しない

    ★★正直な限界★★＝分かるのは
      ・下位ページが増えた／減った
      ・本体ページの中身が変わった
    ★既にある下位ページの中身が書き換えられても分からない★
      （全部読み直さないと分からない）。
      ★これを承知で採る★＝毎日6000ページ読み直すほうが害が大きい。

    ★取りに行くのは本体1ページだけ★
    """
    if not isinstance(saved, dict) or not saved.get("complete"):
        return "CHANGED", "前の記録がない（または読めていない記録）"
    try:
        root = fetch(machine_url)
    except Exception as e:                                   # noqa: BLE001
        return "UNREADABLE", f"機種ページを取れません（{str(e)[:80]}）"

    subs, why = sub_urls(catalog, machine_url,
                         getattr(root, "cleaned_html", ""), max_sub=max_sub)
    if why:
        return "UNREADABLE", why

    # ★見るのは「URLの顔ぶれ」と「本体の中身」だけ★
    now_urls = {_norm(machine_url)} | {_norm(u) for u in subs}
    was_urls = set(saved.get("urls") or []) | set(saved.get("gone") or [])
    if now_urls != was_urls:
        added = sorted(now_urls - was_urls)
        lost = sorted(was_urls - now_urls)
        return "CHANGED", (f"下位ページの顔ぶれが変わりました"
                           f"（増えた {len(added)} / 減った {len(lost)}）")

    was_fp = (saved.get("page_fp") or {}).get(_norm(machine_url))
    if was_fp and _fp(text_of(root)) != was_fp:
        return "CHANGED", "機種ページの中身が変わりました"
    return "SAME", ""


def publishers(urls) -> set:
    """★そのURLたちが、どの発行者のものか★（2026-09-02）

    ★数えない★＝票の数は `source_lineage.independent()` だけが決める（監査39）。
    ここは「かたまりの名前」を作るところまで。
    """
    import source_lineage as _sl
    out = set()
    for u in (urls or []):
        k = _sl.vote_key_of_url(str(u))
        if k:
            out.add(k)
    return out


def manifest(pages: dict, complete: bool, gone=None) -> dict:
    """★読んだページの集合★を、あとで突き合わせられる形にする。

    pages … {URL: 見える文字}
    gone  … ★相手のサイトに無かったURL（404）★（2026-09-02）

    ★指紋はURLの集合と各本文から作る★＝
      URLが1本増えても、本文が書き換わっても、指紋が変わる。
    ★無かったURLも指紋に入れる★＝相手が復活させたら聞き直しになる。
      ★黙って無かったことにしない★
    """
    items = sorted((_norm(u), _fp(t)) for u, t in (pages or {}).items())
    lost = sorted({_norm(u) for u in (gone or [])})
    h = hashlib.sha256()
    for u, f in items:
        h.update(u.encode("utf-8"))
        h.update(b"\0")
        h.update(f.encode("utf-8"))
        h.update(b"\n")
    h.update(b"--gone--\n")
    for u in lost:
        h.update(u.encode("utf-8"))
        h.update(b"\n")
    return {
        "urls": [u for u, _ in items],
        "page_fp": {u: f for u, f in items},
        "gone": lost,
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

    # ★★組み立て（collect）★★（2026-09-02・台帳#542の2段目）
    class _P:
        def __init__(self, html):
            self.cleaned_html = html

    _HTML2 = (
        '<a href="https://nana-press.com/kaiseki/machine/644/18017/">A</a>'
        '<a href="https://nana-press.com/kaiseki/machine/644/18039/">B</a>')

    def _ok_fetch(u):
        return _P(_HTML2 if u == ROOT else "")

    def _text(pg):
        return "本文:" + pg.cleaned_html[:20]

    got = collect(NANA, ROOT, _ok_fetch, _text)
    t("★集合を組み立てる（本体＋下位）★",
      len(got["pages"]) == 3 and got["why"] == ""
      and got["manifest"]["complete"] is True)

    def _bad_sub(u):
        if u.endswith("18039/"):
            raise RuntimeError("404")
        return _P(_HTML2 if u == ROOT else "")

    b = collect(NANA, ROOT, _bad_sub, _text)
    t("★下位が1本でも取れなければ「読めていない」★"
      "／★取れた分だけで判断させない★",
      b["pages"] == {} and b["manifest"]["complete"] is False
      and b["why"] != "")

    class _Gone(RuntimeError):
        status = 404

    def _one_gone(u):
        if u.endswith("18039/"):
            raise _Gone("404")
        return _P(_HTML2 if u == ROOT else "")

    g = collect(NANA, ROOT, _one_gone, _text)
    t("★そもそも無いページ（404）は、証拠の欠けにしない★"
      "／★これが無いと、リンク切れが1本あるだけで機種が永久に読めない★",
      g["manifest"]["complete"] is True and g["why"] == ""
      and len(g["pages"]) == 2)
    t("★無かったURLは記録に残す★（黙って無かったことにしない）",
      g["manifest"]["gone"] == ["https://nana-press.com/kaiseki/machine/644/18039"])
    t("★無かったページが復活したら指紋が変わる★（聞き直しになる）",
      g["manifest"]["fp"] != collect(NANA, ROOT, _ok_fetch, _text)["manifest"]["fp"])

    class _Busy(RuntimeError):
        status = 503

    def _one_busy(u):
        if u.endswith("18039/"):
            raise _Busy("503")
        return _P(_HTML2 if u == ROOT else "")

    t("★読めなかった（503）は、いままでどおり「読めていない」★",
      collect(NANA, ROOT, _one_busy, _text)["manifest"]["complete"] is False)
    t("　番号が分からない失敗も「読めていない」側に倒れる",
      collect(NANA, ROOT, _bad_sub, _text)["manifest"]["complete"] is False)

    def _bad_root(u):
        raise RuntimeError("503")

    r = collect(NANA, ROOT, _bad_root, _text)
    t("　機種ページが取れなければ「読めていない」",
      r["manifest"]["complete"] is False and r["why"] != "")

    _many_html = "".join(
        f'<a href="https://nana-press.com/kaiseki/machine/644/{i}/">x</a>'
        for i in range(1, 12))
    o = collect(NANA, ROOT, lambda u: _P(_many_html if u == ROOT else ""),
                _text, max_sub=5)
    t("★上限を超えたら、取りに行かずに断る★",
      o["pages"] == {} and o["manifest"]["complete"] is False)

    _calls = []

    def _count_fetch(u):
        _calls.append(u)
        return _P(_many_html if u == ROOT else "")

    collect(NANA, ROOT, _count_fetch, _text, max_sub=5)
    t("　上限超過のときは下位を1本も取りに行かない", _calls == [ROOT])

    t("　下位ページの決まりが無い名鑑は、本体だけで成立する",
      collect({"machine_id_pattern": NANA["machine_id_pattern"]},
              ROOT, _ok_fetch, _text)["manifest"]["complete"] is True)

    # ★★2回目以降は1ページだけ★★（2026-09-02・運営者の判断）
    _saved = collect(NANA, ROOT, _ok_fetch, _text)["manifest"]
    _hit = []

    def _one_page(u):
        _hit.append(u)
        return _P(_HTML2 if u == ROOT else "")

    t("★変わっていなければ、前の判断をそのまま使う★",
      quick_check(NANA, ROOT, _one_page, _text, _saved)[0] == "SAME")
    t("★★取りに行くのは本体1ページだけ★★"
      "／★これが無いと毎日6000ページ読み直すことになる★",
      _hit == [ROOT])

    _HTML3 = _HTML2 + \
        '<a href="https://nana-press.com/kaiseki/machine/644/99999/">C</a>'
    t("★下位ページが増えたら聞き直す★（Codexが挙げた危険）",
      quick_check(NANA, ROOT, lambda u: _P(_HTML3 if u == ROOT else ""),
                  _text, _saved)[0] == "CHANGED")
    t("　下位ページが減っても聞き直す",
      quick_check(NANA, ROOT,
                  lambda u: _P('<a href="https://nana-press.com/kaiseki/'
                               'machine/644/18017/">A</a>' if u == ROOT
                               else ""),
                  _text, _saved)[0] == "CHANGED")
    t("★本体の中身が変わったら聞き直す★",
      quick_check(NANA, ROOT, _one_page,
                  lambda pg: "★別の本文★", _saved)[0] == "CHANGED")
    t("　本体が読めなければ判断しない",
      quick_check(NANA, ROOT, _bad_root, _text, _saved)[0] == "UNREADABLE")
    t("　前の記録が無ければ集め直す",
      quick_check(NANA, ROOT, _one_page, _text, {})[0] == "CHANGED")
    t("　前の記録が「読めていない」なら集め直す",
      quick_check(NANA, ROOT, _one_page, _text,
                  manifest({}, False))[0] == "CHANGED")
    # ★404で読めなかったURLも「顔ぶれ」に数える★＝
    #   数えないと、毎回「減った」と見なして永久に聞き直しになる。
    t("　無かったページ（404）は顔ぶれに数える（毎回聞き直しにならない）",
      quick_check(NANA, ROOT, _one_page, _text,
                  collect(NANA, ROOT, _one_gone, _text)["manifest"])[0]
      == "SAME")

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

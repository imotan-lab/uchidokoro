"""スマホの幅で記事ページとポチポチくんの画面写しを撮る（2AI が表示崩れを目で見るため）

★なぜ要るか★（2026-11-25・運営者「こういう表示のおかしいものとかも更新のタイミングで見てね」）＝
東京喰種の記事ページが、設定1〜6の7列の表のせいでスマホの画面より横に広がっていた。
横幅のはみ出しは audit_render の R18 が機械で見る。文字の重なり・1文字ずつの折り返し・欠けた表のように
機械で決めにくい崩れは、この写しを Claude が読んで確かめる（朝の更新タスクの点検表 display）。

使い方:
  python -m http.server 8000            （リポジトリの直下で・別の窓で）
  python scripts/phone_shot.py --slug <slug> [--slug <slug> ...] [--base-url http://localhost:8000] [--width 390]
  python scripts/phone_shot.py --selftest

写しの置き場＝Documents/uchidokoro/ops/shots/<slug>_article_<幅>.png と <slug>_pochi_<幅>.png（リポジトリの外）。
終了コード：0＝撮れた／1＝撮れなかったものがある（理由を出す）／2＝引数の誤り。
"""
import argparse
import datetime as _dt
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import local_paths as _lp  # noqa: E402

OUT_DIR = os.path.join(_lp.OPS, "shots")
_SLUG = re.compile(r"^[a-z0-9_]+$")


_QUIET = False     # ★自己試験の間だけ True＝本番の記録に試験の行を書かない★（本番では必ず書く）


def _log(msg: str) -> None:
    if _QUIET:
        return
    try:
        os.makedirs(_lp.LOGS, exist_ok=True)
        with open(os.path.join(_lp.LOGS, f"phone_shot_{_dt.date.today().isoformat()}.log"), "a",
                  encoding="utf-8") as f:
            f.write(f"[{_dt.datetime.now().strftime('%Y/%m/%d %H:%M:%S')}] {msg}\n")
    except Exception:                       # noqa: BLE001
        pass


def targets(slug: str, base: str, width: int) -> list:
    """★純関数★ 撮るページの組 → [(名前, URL, 保存先)]"""
    if not _SLUG.match(str(slug or "")):
        raise ValueError(f"機種の名前の形が違います: {slug!r}")
    base = str(base).rstrip("/")
    return [("article", f"{base}/machines/{slug}/", os.path.join(OUT_DIR, f"{slug}_article_{width}.png")),
            ("pochi", f"{base}/setting.html?slug={slug}", os.path.join(OUT_DIR, f"{slug}_pochi_{width}.png"))]


SLICE_H = 1200        # 1枚の高さ（CSSの px）
MAX_SLICES = 15       # これより長いページは先頭から MAX_SLICES 枚まで（そのことを名前に残す）


def slices(total_h, out: str) -> list:
    """★純関数★ 縦の長さを SLICE_H ごとに分ける → [(y, 高さ, 保存先)]"""
    total_h = max(1, int(total_h or 1))
    stem, ext = os.path.splitext(out)
    got = []
    y = 0
    while y < total_h and len(got) < MAX_SLICES:
        h = min(SLICE_H, total_h - y)
        got.append((y, h, f"{stem}_{len(got) + 1:02d}{ext}"))
        y += h
    return got


def response_problem(status, want_url: str, got_url: str) -> str:
    """★純関数★ 開いたページが撮ってよいものか（よければ空文字）"""
    if not isinstance(status, int):
        return "ページの応答がありません"
    if not 200 <= status < 300:
        return f"ページがエラーです（HTTP {status}）"
    from urllib.parse import urlsplit
    w, g = urlsplit(str(want_url)), urlsplit(str(got_url))
    if (w.netloc, w.path, w.query) != (g.netloc, g.path, g.query):
        return f"別のページへ移りました（{got_url}）"
    return ""


def _squash(s: str) -> str:
    import unicodedata
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(s or "")))


def name_shown(kind: str, name: str, h1: str, title: str) -> bool:
    """★純関数★ そのページが、その機種のページか（全角半角・空白の違いは問わない）

    ★本文全体では見ない★（2026-11-26・Codexの2回目＝ポチポチくんは対応機種の一覧にほかの機種の名前も並ぶ）。
    記事ページ＝見出し（#machineTitle）が機種名そのもの／ポチポチくん＝ページの題が「機種名 小役カウンター…」で始まる
    （ポチポチくんの見出しは「小役カウンター ポチポチくん」で固定のため）。
    """
    n = _squash(name)
    if not n:
        return False
    if kind == "article":
        return _squash(h1) == n
    if kind == "pochi":
        return _squash(title).startswith(n + _squash("小役カウンター"))
    return False


HEAD_JS = "() => ({h1: (document.getElementById('machineTitle') || {}).innerText || '', title: document.title || ''})"


def shoot_one(page, slug: str, kind: str, url: str, out: str, width: int, mname: str) -> int:
    """1ページを確かめてから撮る → 失敗の数（0 か 1）。★page は本物でも試験用の偽物でもよい★"""
    try:
        resp = page.goto(url, wait_until="load", timeout=30000)
        # ★エラーのページ・別のページへの転送を「撮れた」にしない★（2026-11-26・Codexの指摘＝404でも終了コード0だった）
        why = response_problem(getattr(resp, "status", None) if resp is not None else None, url, page.url)
        if why:
            raise RuntimeError(why)
        page.wait_for_timeout(800)
        # ★待ったあとに、いまいるページをもう一度確かめる★（Codexの2回目＝待つ間に別のページへ移ると通っていた）
        why = response_problem(200, url, page.url)
        if why:
            raise RuntimeError(why)
        head = page.evaluate(HEAD_JS) or {}
        if not name_shown(kind, mname, head.get("h1"), head.get("title")):
            # ★その機種のページになっていなければ撮らない★（Codexの指摘＝目的の機種の表示を確かめる）
            raise RuntimeError(f"その機種（{mname}）のページになっていません")
        sw = page.evaluate("document.documentElement.scrollWidth")
        sh = page.evaluate("document.documentElement.scrollHeight")
        note = "" if sw <= width + 1 else f"（★横にはみ出しています：{sw}px★）"
        # ★縦に長い1枚は読めないので、画面ひとつ分ずつに分けて撮る★（縦18,000px の1枚では細かい崩れが見えない）
        parts = slices(sh, out)
        for i, (y, h, part) in enumerate(parts, 1):
            page.screenshot(path=part, full_page=True,
                            clip={"x": 0, "y": y, "width": max(width, sw), "height": h})
            print(f"撮りました: {part}{note if i == 1 else ''}")
        # ★撮り終えたあとにもう一度確かめる★（Codexの3回目＝測る・撮る間に別のページへ移ると、移動先を撮って成功にしていた）
        why = response_problem(200, url, page.url)
        if why:
            raise RuntimeError(why + "（撮っている間に移りました・この回の写しは使わない）")
        # ★今回の写しの番号を必ず出す★（前回の方が枚数が多いと、残った古い番号の写しを読み違えるため）
        print(f"  今回の写しは {len(parts)} 枚（_01〜_{len(parts):02d}）。★「撮りました」と出たものだけを読む★")
        _log(f"撮った {slug} {kind} {width}px 横幅{sw}px 縦{sh}px → {out}（{len(parts)}枚に分割）")
        cut = int(sh) - sum(h for _y, h, _p in parts)
        if cut > 0:
            # ★撮り切れなかった末尾を成功にしない★（Codexの指摘）
            print(f"★撮り切れていません★ {slug} {kind}: 末尾 {cut}px（{MAX_SLICES}枚まで）")
            _log(f"撮り切れない {slug} {kind}: 末尾{cut}px")
            return 1
        return 0
    except Exception as e:      # noqa: BLE001
        print(f"★撮れません★ {slug} {kind}: {type(e).__name__}: {str(e)[:120]}")
        _log(f"撮れない {slug} {kind}: {type(e).__name__}: {str(e)[:200]}")
        return 1


def shoot(slugs: list, base: str, width: int, names: dict = None) -> int:
    from playwright.sync_api import sync_playwright
    os.makedirs(OUT_DIR, exist_ok=True)
    bad = 0
    with sync_playwright() as pw:
        br = pw.chromium.launch(headless=True)
        ctx = br.new_context(viewport={"width": width, "height": 844}, device_scale_factor=2,
                             is_mobile=True, has_touch=True, service_workers="block")
        page = ctx.new_page()
        for slug in slugs:
            for kind, url, out in targets(slug, base, width):
                bad += shoot_one(page, slug, kind, url, out, width, (names or {}).get(slug, ""))
        br.close()
    return 1 if bad else 0


def selftest() -> int:
    global _QUIET
    _QUIET = True
    try:
        return _selftest()
    finally:
        _QUIET = False


def _selftest() -> int:
    res = []

    def t(name, cond):
        res.append(bool(cond))
        print(("✅ " if cond else "❌ ") + name)

    got = targets("tokyo_ghoul", "http://localhost:8000/", 390)
    t("記事ページとポチポチくんの2枚を、機種名と幅つきの名前でリポジトリの外へ",
      [g[0] for g in got] == ["article", "pochi"]
      and got[0][1] == "http://localhost:8000/machines/tokyo_ghoul/"
      and got[1][1] == "http://localhost:8000/setting.html?slug=tokyo_ghoul"
      and got[0][2].endswith(os.path.join("shots", "tokyo_ghoul_article_390.png"))
      and "うちどころ" not in os.path.dirname(got[0][2]).split(os.sep)[-3:-1])
    sl = slices(2500, os.path.join("d", "x.png"))
    t("縦に長いページは画面ひとつ分ずつに分ける（最後は残りの高さ）",
      [(y, h) for y, h, _p in sl] == [(0, 1200), (1200, 1200), (2400, 100)]
      and sl[0][2].endswith("x_01.png") and len(slices(10 ** 6, "x.png")) == MAX_SLICES)
    u = "http://localhost:8000/machines/x/"
    t("★エラーのページ・転送・応答なしは撮らない（404 で成功にしない）★",
      response_problem(200, u, u) == ""
      and response_problem(404, u, u) != "" and response_problem(500, u, u) != ""
      and response_problem(None, u, u) != ""
      and response_problem(200, u, "http://localhost:8000/index.html") != ""
      and response_problem(200, u, u + "#tocBlock") == "")
    t("★記事ページは見出しが機種名そのもの・ポチポチくんは題が「機種名 小役カウンター」で始まる★",
      name_shown("article", "L東京喰種", "L 東京喰種", "") and not name_shown("article", "L東京喰種", "L東京喰種2", "")
      and name_shown("pochi", "L東京喰種", "小役カウンター ポチポチくん", "L東京喰種 小役カウンター ポチポチくん | うちどころ。")
      and not name_shown("pochi", "L東京喰種", "小役カウンター ポチポチくん", "小役カウンター ポチポチくん | うちどころ。")
      and not name_shown("pochi", "東京喰種", "", "L東京喰種 小役カウンター ポチポチくん")
      and not name_shown("article", "", "", ""))

    # ★本物の撮る手順（shoot_one）を、偽のページで動かして確かめる★（Codexの2回目＝コードの文字ではなく結果を見る）
    class _Resp:
        def __init__(self, s):
            self.status = s

    class _Page:
        def __init__(self, status=200, move_to=None, h1="L東京喰種", title="", sh=2000, move_late=None):
            self.url, self._st, self._mv, self._h1, self._ti, self._sh = "", status, move_to, h1, title, sh
            self._late = move_late
            self.shots = []

        def goto(self, url, **_k):
            self.url = url
            return _Resp(self._st) if self._st is not None else None

        def wait_for_timeout(self, _ms):
            if self._mv:
                self.url = self._mv

        def evaluate(self, js):
            if js == HEAD_JS:
                return {"h1": self._h1, "title": self._ti}
            if self._late and "scrollHeight" in js:
                self.url = self._late          # 見出しを確かめたあと、測っている間に別のページへ移る
            return 390 if "scrollWidth" in js else self._sh

        def screenshot(self, path, **_k):
            self.shots.append(path)

    import io as _io
    import contextlib as _cl
    u = "http://localhost:8000/machines/x/"
    o = os.path.join("d", "x.png")

    def _run(pg, kind="article"):
        with _cl.redirect_stdout(_io.StringIO()):
            return shoot_one(pg, "x", kind, u, o, 390, "L東京喰種"), len(pg.shots)

    t("　正しいページは撮れて 0（縦2000px＝2枚）", _run(_Page()) == (0, 2))
    t("★404 のページは1枚も撮らず失敗★", _run(_Page(status=404)) == (1, 0))
    t("★待つ間に別のページへ移ったら撮らず失敗★", _run(_Page(move_to="http://localhost:8000/machines/y/")) == (1, 0))
    t("★見出しが別の機種なら撮らず失敗★", _run(_Page(h1="L東京喰種2")) == (1, 0))
    t("★測る・撮る間に別のページへ移ったら失敗（移動先の写しを成功にしない）★",
      _run(_Page(move_late="http://localhost:8000/machines/y/"))[0] == 1)
    t("★ポチポチくんは題が別の機種なら撮らず失敗★",
      _run(_Page(h1="", title="L東京喰種2 小役カウンター ポチポチくん | うちどころ。"), "pochi") == (1, 0)
      and _run(_Page(h1="", title="L東京喰種 小役カウンター ポチポチくん | うちどころ。"), "pochi") == (0, 2))
    t("★撮り切れない長さ（15枚を超える）は失敗に数える★",
      _run(_Page(sh=SLICE_H * MAX_SLICES + 50)) == (1, MAX_SLICES))
    try:
        targets("../x", "http://localhost:8000", 390)
        t("機種の名前に / や .. を入れさせない", False)
    except ValueError:
        t("機種の名前に / や .. を入れさせない", True)
    ok = sum(res)
    print(f"\n{ok}/{len(res)} 合格")
    return 0 if ok == len(res) else 1


def main() -> int:
    for _st in (sys.stdout, sys.stderr):
        try:
            _st.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                   # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description="スマホの幅の画面写しを撮る")
    ap.add_argument("--slug", action="append", default=[])
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--width", type=int, default=390)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.slug:
        print("--slug が要ります")
        return 2
    try:
        for s in a.slug:
            targets(s, a.base_url, a.width)
    except ValueError as e:
        print(f"★{e}★")
        return 2
    # ★機種一覧に無い名前は撮らない★（2026-11-26・ポチポチくんは知らない機種でもページが200で開き、撮れてしまった）
    try:
        import json as _json
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "assets", "data", "machines.json"), encoding="utf-8") as f:
            _ms = _json.load(f)
        _ms = _ms if isinstance(_ms, list) else _ms.get("machines", [])
        names = {m["slug"]: str(m.get("name") or "") for m in _ms if isinstance(m, dict) and m.get("slug")}
    except Exception as e:                  # noqa: BLE001
        print(f"★機種一覧を読めません★: {type(e).__name__}")
        return 2
    unknown = [s for s in a.slug if not names.get(s)]
    if unknown:
        print(f"★機種一覧に無い名前です★: {' '.join(unknown)}")
        return 2
    return shoot(a.slug, a.base_url, a.width, names)


if __name__ == "__main__":
    raise SystemExit(main())

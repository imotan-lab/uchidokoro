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


def _log(msg: str) -> None:
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


def shoot(slugs: list, base: str, width: int) -> int:
    from playwright.sync_api import sync_playwright
    os.makedirs(OUT_DIR, exist_ok=True)
    bad = 0
    with sync_playwright() as pw:
        br = pw.chromium.launch(headless=True)
        ctx = br.new_context(viewport={"width": width, "height": 844}, device_scale_factor=2,
                             is_mobile=True, has_touch=True, service_workers="block")
        page = ctx.new_page()
        for slug in slugs:
            for name, url, out in targets(slug, base, width):
                try:
                    page.goto(url, wait_until="load", timeout=30000)
                    page.wait_for_timeout(800)
                    sw = page.evaluate("document.documentElement.scrollWidth")
                    sh = page.evaluate("document.documentElement.scrollHeight")
                    note = "" if sw <= width + 1 else f"（★横にはみ出しています：{sw}px★）"
                    # ★縦に長い1枚は読めないので、画面ひとつ分ずつに分けて撮る★（縦18,000px の1枚では細かい崩れが見えない）
                    for i, (y, h, part) in enumerate(slices(sh, out), 1):
                        page.screenshot(path=part, full_page=True,
                                        clip={"x": 0, "y": y, "width": max(width, sw), "height": h})
                        print(f"撮りました: {part}{note if i == 1 else ''}")
                    _log(f"撮った {slug} {name} {width}px 横幅{sw}px 縦{sh}px → {out}（分割）")
                except Exception as e:      # noqa: BLE001
                    bad += 1
                    print(f"★撮れません★ {slug} {name}: {type(e).__name__}: {str(e)[:120]}")
                    _log(f"撮れない {slug} {name}: {type(e).__name__}: {str(e)[:200]}")
        br.close()
    return 1 if bad else 0


def selftest() -> int:
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
    return shoot(a.slug, a.base_url, a.width)


if __name__ == "__main__":
    raise SystemExit(main())

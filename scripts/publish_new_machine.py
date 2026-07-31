"""publish_new_machine.py — 新台1機種だけを公開する専用の経路。

★なぜ専用にするか（2026-07-31・Codexと相談して案Bに決めた）★
  既存119機種のページを直す `--legacy` に相乗りさせると、
  入力条件も品質も失敗時の扱いも違うものが同じ経路に混ざる。
  既存は `LEGACY_UNVERIFIED`（未裏取り）だが、新台の記事は
  **確認できた項目だけを載せた先行記事**で、意味がまるで違う。
  そこで状態名も別にする → `PREVIEW_VERIFIED_SUBSET`
  （載せた値は出典2件で確認済み・ただし記事は網羅的ではない）。

★この経路が触ってよいもの（これ以外は書かない）★
  1. `machines/{新しいslug}/index.html` を**新規に**作る
  2. `assets/data/machine-details/{新しいslug}.json` を新規に作る
  3. `machines.json` に1件足す
  ★sitemap は触らない★（preview は載せない決まり）
  ★既存ページは作り直さない・消さない・上書きしない★

★書く順番（Codexの指摘）★
  **ページを先に置き、最後に machines.json を足す。**
  トップページは machines.json を見てリンクを張るので、
  逆順だと「一覧に出るのにページが無い（404）」瞬間ができる。

使い方:
    python scripts/publish_new_machine.py --slug <slug>          # 確かめるだけ
    python scripts/publish_new_machine.py --slug <slug> --apply
    python scripts/publish_new_machine.py --selftest
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import build_machine_pages as _bmp      # noqa: E402
import safe_json as _sj                 # noqa: E402

MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
DETAILS = os.path.join(BASE, "assets", "data", "machine-details")
SITEMAP = os.path.join(BASE, "sitemap.xml")
STATE = "PREVIEW_VERIFIED_SUBSET"


class PublishError(RuntimeError):
    pass


def _page_path(slug: str) -> str:
    return os.path.join(BASE, "machines", slug, "index.html")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _existing_pages() -> dict:
    """いま公開中のページの指紋。★1枚も変えていないことを確かめるため★"""
    out = {}
    root = os.path.join(BASE, "machines")
    for slug in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        p = os.path.join(root, slug, "index.html")
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                out[slug] = _sha(f.read())
    return out


def check_before(slug: str, machine: dict, rows: list) -> list:
    """書く前に確かめること。★1つでも引っかかったら書かない★"""
    ng = []
    if not slug or slug != machine.get("slug"):
        ng.append("slug が機種データと合いません")
    if os.path.isfile(_page_path(slug)):
        ng.append(f"{slug} のページは既にあります（この経路は新規作成だけです）")
    if any(m.get("slug") == slug for m in rows):
        ng.append(f"{slug} は既に machines.json にあります（上書きしません）")
    if machine.get("status") != "preview":
        ng.append(f"status が preview ではありません（{machine.get('status')!r}）"
                  "。この経路は先行記事だけを公開します")
    if machine.get("publish_state") != STATE:
        ng.append(f"publish_state が {STATE} ではありません"
                  f"（{machine.get('publish_state')!r}）")
    return ng


def check_page(slug: str, html: str) -> list:
    """作ったページそのものを確かめる。★テンプレート任せにしない★"""
    ng = []
    if "noindex" not in html:
        ng.append("noindex が入っていません（先行記事は検索に出しません）")
    if '<base href="/">' not in html:
        ng.append('<base href="/"> がありません（ロゴ・ナビが404になります）')
    if f"https://uchidokoro.com/machines/{slug}/" not in html:
        ng.append("canonical がこの機種のURLになっていません")
    if "style=" in html:
        ng.append("インラインstyleが入っています")
    return ng


def check_after(slug: str, before_pages: dict, rows_before: list) -> list:
    """書いたあとに確かめること。★取り返しがつくうちに気づくため★"""
    ng = []
    now = _existing_pages()
    for s, h in before_pages.items():
        if s not in now:
            ng.append(f"既存ページが消えました: {s}")
        elif now[s] != h:
            ng.append(f"既存ページが書き換わりました: {s}")
    if slug not in now:
        ng.append(f"{slug} のページができていません")
    rows = _sj.read_rows(MACHINES)
    if len(rows) != len(rows_before) + 1:
        ng.append(f"machines.json の件数が {len(rows_before)} → {len(rows)} です（+1のはず）")
    for m in rows:
        if not os.path.isfile(_page_path(m.get("slug", ""))):
            ng.append(f"一覧に出るのにページがありません: {m.get('slug')}")
    with open(SITEMAP, encoding="utf-8") as f:
        if f"/machines/{slug}/" in f.read():
            ng.append("sitemap に先行記事が載っています（載せない決まりです）")
    return ng


def render(slug: str, machine: dict, detail: dict) -> str:
    """既存ページと同じ描き方で1枚だけ作る。"""
    with open(os.path.join(BASE, "machine.html"), encoding="utf-8") as f:
        template = _bmp.prepare_template(f.read())
    reasons = _bmp.extract_pochipochi_reasons(template)
    return _bmp.render_page(template, machine, detail, reasons)


def publish(slug: str, machine: dict, detail: dict, apply_it: bool = False) -> dict:
    """新台1件を公開する。★ページを先に置き、最後に一覧へ足す★"""
    out = {"slug": slug, "problems": [], "wrote": [], "html_bytes": 0}
    rows = _sj.read_rows(MACHINES)
    out["problems"] += check_before(slug, machine, rows)
    if out["problems"]:
        return out
    html = render(slug, machine, detail)
    out["html_bytes"] = len(html)
    out["problems"] += check_page(slug, html)
    if out["problems"] or not apply_it:
        return out

    before_pages = _existing_pages()
    page = _page_path(slug)
    dp = os.path.join(DETAILS, f"{slug}.json")
    made_dir = False
    try:
        # ① 記事データ（一覧からはまだ辿れない）
        if os.path.exists(dp):
            raise PublishError(f"{dp} が既にあります")
        with open(dp, "w", encoding="utf-8", newline=chr(10)) as f:
            json.dump(detail, f, ensure_ascii=False, indent=1)
            f.write(chr(10))
        # ② ページ（★一覧より先★＝404の瞬間を作らない）
        d = os.path.dirname(page)
        if not os.path.isdir(d):
            os.makedirs(d)
            made_dir = True
        with open(page, "w", encoding="utf-8", newline=chr(10)) as f:
            f.write(html)
        # ③ 最後に一覧へ足す
        rows.append(machine)
        tmp = MACHINES + ".new"
        with open(tmp, "w", encoding="utf-8", newline=chr(10)) as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
            f.write(chr(10))
        os.replace(tmp, MACHINES)
        out["wrote"] = [dp, page, MACHINES]
    except Exception as e:                # noqa: BLE001
        # ★どこで転んでも、置いたものを片付ける★
        for p in (dp, page):
            if os.path.exists(p):
                os.remove(p)
        if made_dir and os.path.isdir(os.path.dirname(page)):
            os.rmdir(os.path.dirname(page))
        raise PublishError(f"公開できませんでした（元に戻しました）: {e}")

    out["problems"] += check_after(slug, before_pages, rows[:-1])
    return out


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []
    nl = chr(10)

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    rows = _sj.read_rows(MACHINES)
    ok_machine = {"slug": "zzz_test", "name": "テスト機", "status": "preview",
                  "publish_state": STATE}
    t("★新しい機種なら前提を通る★", check_before("zzz_test", ok_machine, rows) == [])
    t("★★既にある機種は拒否する★★（上書きしない）",
      check_before(rows[0]["slug"],
                   {**ok_machine, "slug": rows[0]["slug"]}, rows))
    t("★★先行記事以外は公開しない★★",
      any("preview" in x for x in
          check_before("zzz_test", {**ok_machine, "status": "complete"}, rows)))
    t("★★状態名が違えば公開しない★★（既存の未裏取りページと混ぜない）",
      any("publish_state" in x for x in
          check_before("zzz_test",
                       {**ok_machine, "publish_state": "LEGACY_UNVERIFIED"}, rows)))
    t("　slugが食い違えば拒否", check_before("aaa", ok_machine, rows))

    good = ('<html><head><base href="/"><meta name="robots" content="noindex,follow">'
            '<link rel="canonical" href="https://uchidokoro.com/machines/zzz_test/">'
            "</head><body>x</body></html>")
    t("★作ったページの中身を必ず確かめる★", check_page("zzz_test", good) == [])
    t("★★noindex が無ければ公開しない★★（先行記事を検索に出さない）",
      any("noindex" in x for x in
          check_page("zzz_test", good.replace("noindex,follow", "index,follow"))))
    t("　base href が無ければ公開しない",
      any("base href" in x for x in check_page("zzz_test",
                                               good.replace('<base href="/">', ""))))
    t("　canonical が別機種なら公開しない",
      any("canonical" in x for x in
          check_page("zzz_test", good.replace("zzz_test/", "other/"))))
    t("　インラインstyleがあれば公開しない",
      any("style" in x for x in check_page("zzz_test",
                                           good.replace("<body>", '<body style="x">'))))

    pages = _existing_pages()
    t("★既存ページの指紋を取れる（1枚も変えていないことを確かめるため）★",
      len(pages) >= 100 and all(len(v) == 64 for v in pages.values()))

    ng = [n for n, ok in results if not ok]
    print(f"{nl}{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--slug")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.slug:
        ap.print_help()
        return 0
    print("★この経路は新台1機種だけを公開します（既存ページは触りません）★")
    print(f"  slug: {args.slug} / 書き込み: {'する' if args.apply else 'しない（確認だけ）'}")
    print("  機種データと記事データは、先に add_machine_run.py が作ったものを渡してください")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except (PublishError, _sj.SafeJsonError) as e:
        print(f"★{e}★")
        raise SystemExit(1)
    except Exception as e:                # noqa: BLE001
        print(f"★想定外の失敗 {type(e).__name__}: {e}★")
        raise SystemExit(1)

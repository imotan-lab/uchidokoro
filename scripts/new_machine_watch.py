"""new_machine_watch.py — メーカー公式の機種一覧を見て、新台を見つける。

★なぜこの向きなのか（2026-07-31・運営者判断＝完全自動化）★
  以前は「まとめサイトの機種名 → 公式ページを探す」向きだった。
  これだと名前の照合が必要で、人の判断なしには自動化できない。
  実際、まとめサイトの「ビンゴライブ・8月3日導入」は**名前も日付も誤り**で、
  公式は「Ｌすーぱぁびん娘・2026年8月登場」だった。

  そこで向きを逆にする。

    メーカー公式の機種一覧 → 新しいURLが現れた ＝ それが新台

  まとめサイトの名前を**そもそも読まない**ので、照合が発生しない。
  機種の正体は「公式一覧に載っている個別ページのURL」そのものになる。

★人が保守するのは assets/data/maker-catalogs.json だけ★
  メーカーの一覧ページURLを書くファイル。機種ごとの作業はゼロ。
  ここに無いメーカーの新台は見つからないが、それは「出さない」側の失敗。

★黙って0件にしない★
  一覧ページの作りが変わってリンクが取れなくなると、
  「新台なし」と誤認して静かに止まる。これが一番こわい。
  だからメーカーごとに「最低これだけは並んでいるはず」の数を持ち、
  下回ったら**異常として報告する**（新台なしとは言わない）。

使い方:
    python scripts/new_machine_watch.py --scan          # 全メーカーを見る
    python scripts/new_machine_watch.py --check bellco  # 1社だけ試す
    python scripts/new_machine_watch.py --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import safe_json as _sj               # noqa: E402

CATALOGS = os.path.join(BASE, "assets", "data", "maker-catalogs.json")
SEEN_PATH = r"C:/Users/imao_/Documents/uchidokoro/seen_machine_urls.json"
UA = "uchidokoro-new-machine-watch/1.0 (+https://uchidokoro.com)"
MAX_BYTES = 5 * 1024 * 1024

# 一覧ページに混ざる「機種ではないリンク」を落とす。
#   ★許可した形だけ通す★（禁止語を並べる方式は必ず抜ける）
_SLUGLIKE = re.compile(r"^[a-z0-9][a-z0-9_\-]{1,60}$")


class WatchError(RuntimeError):
    pass


def _get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                raise WatchError(f"HTTP {r.status}: {url}")
            body = r.read(MAX_BYTES + 1)
            charset = r.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as e:
        raise WatchError(f"取得できません（HTTP {e.code}）: {url}")
    except WatchError:
        raise
    except Exception as e:
        raise WatchError(f"取得できません（{type(e).__name__}）: {url}")
    if len(body) > MAX_BYTES:
        raise WatchError(f"ページが大きすぎます: {url}")
    return body.decode(charset, "replace")


def product_urls(html: str, base_url: str, link_prefix: str) -> list:
    """一覧ページから、個別機種ページのURLを取り出す。

    ★一覧ページ自身や親ページを機種と数えない★
      `/products/slot/` のような「末尾が接頭辞と同じ」ものは機種ではない。
    """
    out = set()
    for href in re.findall(r'href="([^"]+)"', html):
        absu = urllib.parse.urljoin(base_url, href.strip())
        absu = absu.split("#")[0].split("?")[0]
        if not absu.startswith(link_prefix):
            continue
        rest = absu[len(link_prefix):].strip("/")
        if not rest or "/" in rest:
            continue                      # 一覧そのもの／さらに下の階層は対象外
        if not _SLUGLIKE.match(rest):
            continue
        out.add(link_prefix.rstrip("/") + "/" + rest + "/")
    return sorted(out)


def page_title(html: str) -> str:
    m = re.search(r"(?is)<title[^>]*>(.*?)</title\s*>", html)
    if not m:
        m = re.search(r"(?is)<h1[^>]*>(.*?)</h1\s*>", html)
    if not m:
        return ""
    t = re.sub(r"(?s)<[^>]+>", "", m.group(1))
    return unicodedata.normalize("NFKC", t).strip()


def machine_name(html: str) -> str:
    """公式ページのタイトルから機種名だけを取る（サイト名などを落とす）。"""
    t = page_title(html)
    # 「機種名|機種情報|メーカー名...」の形が多い。最初の区切りまでを名前とする。
    for sep in ("|", "｜", "-", "‐", "―", "–"):
        if sep in t:
            t = t.split(sep)[0]
            break
    return t.strip()


def _load_seen() -> dict:
    if not os.path.isfile(SEEN_PATH):
        return {"schema": "seen-machine-urls/v1", "makers": {}}
    try:
        d = _sj.read_json(SEEN_PATH, expect=dict)
    except Exception as e:
        # ★読めないときは「全部新台」にしない★（初回と区別できず大量誤検出になる）
        raise WatchError(f"見たことのあるURLの記録が読めません: {e} → 今日は止めます")
    d.setdefault("makers", {})
    return d


def _save_seen(data: dict) -> None:
    import tempfile
    os.makedirs(os.path.dirname(SEEN_PATH), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(SEEN_PATH), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=chr(10)) as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SEEN_PATH)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def scan_maker(maker_id: str, conf: dict, seen: dict, record: bool = True) -> dict:
    """1社ぶん見る。★取れた数が少なすぎたら『新台なし』と言わない★"""
    out = {"maker": maker_id, "name": conf.get("name"), "new": [], "problem": None,
           "total": 0, "first_time": maker_id not in seen["makers"]}
    try:
        html = _get(conf["list_url"])
    except WatchError as e:
        out["problem"] = str(e)
        return out

    urls = product_urls(html, conf["list_url"], conf["link_prefix"])
    out["total"] = len(urls)
    least = int(conf.get("min_expected") or 1)
    if len(urls) < least:
        # ★ここが黙って0件になる事故を止める唯一の砦★
        out["problem"] = (f"一覧から {len(urls)} 件しか取れません（最低 {least} 件のはず）。"
                          f"ページの作りが変わった可能性があるので『新台なし』とは扱いません")
        return out

    known = set(seen["makers"].get(maker_id, {}).get("urls") or [])
    if out["first_time"]:
        # ★初回は全部を『既知』として覚えるだけ★
        #   いきなり100件を新台として扱わない。
        out["new"] = []
    else:
        out["new"] = [u for u in urls if u not in known]
    if record:
        seen["makers"][maker_id] = {"urls": urls, "count": len(urls)}
    return out


def describe(url: str) -> dict:
    """新台候補の個別ページから、公式が書いていることだけを取る。"""
    html = _get(url)
    text = re.sub(r"(?s)<[^>]+>", chr(10), re.sub(
        r"(?is)<(script|style)\b.*?</\1\s*>", " ", html))
    text = unicodedata.normalize("NFKC", text)
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    when = [x for x in lines if re.search(r"20\d\d年\s*\d{1,2}月", x)][:3]
    return {"url": url, "official_name": machine_name(html),
            "title": page_title(html), "release_lines": when,
            "chars": len(text)}


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    LIST = "https://m.example/products/slot/"
    html = ('<a href="/products/slot/aaa/">A</a>'
            '<a href="/products/slot/bbb/">B</a>'
            '<a href="/products/slot/">一覧</a>'
            '<a href="/products/pachinko/ccc/">パチンコ</a>'
            '<a href="/products/slot/aaa/spec/">下の階層</a>'
            '<a href="https://other.example/products/slot/ddd/">よそ</a>')
    got = product_urls(html, LIST, LIST)
    t("★個別機種ページだけを取る★",
      got == ["https://m.example/products/slot/aaa/",
              "https://m.example/products/slot/bbb/"])
    t("　一覧ページ自身を機種と数えない", LIST not in got)
    t("　パチンコ側・よそのサイト・下の階層は取らない",
      not any("pachinko" in u or "other.example" in u or "spec" in u for u in got))
    t("　#や?が付いていても同じURLとして1件にする",
      product_urls('<a href="/products/slot/aaa/?x=1">A</a>'
                   '<a href="/products/slot/aaa/#top">A</a>', LIST, LIST)
      == ["https://m.example/products/slot/aaa/"])

    t("★タイトルから機種名だけを取る★",
      machine_name("<title>Lすーぱぁびん娘|機種情報|BELLCO(ベルコ株式会社)</title>")
      == "Lすーぱぁびん娘")
    t("　全角の区切りでも取れる",
      machine_name("<title>テスト機　情報｜メーカー</title>") == "テスト機 情報")

    conf = {"name": "t", "list_url": LIST, "link_prefix": LIST, "min_expected": 5}
    seen = {"makers": {"t": {"urls": ["https://m.example/products/slot/aaa/"]}}}

    class _Stub:
        def __init__(self, h): self.h = h

    import builtins  # noqa: F401
    global _get
    real_get = _get
    try:
        _get = lambda u, timeout=20: html          # noqa: E731
        r = scan_maker("t", conf, seen, record=False)
        t("★★取れた数が少なすぎたら『新台なし』と言わない★★（黙って止まる事故を防ぐ）",
          r["problem"] is not None and r["new"] == [])
        conf2 = {**conf, "min_expected": 2}
        r2 = scan_maker("t", conf2, seen, record=False)
        t("　数が足りていれば、知らないURLだけを新台とする",
          r2["problem"] is None and r2["new"] == ["https://m.example/products/slot/bbb/"])
        r3 = scan_maker("zzz", conf2, {"makers": {}}, record=False)
        t("★★初回は全部を新台にしない（覚えるだけ）★★",
          r3["first_time"] is True and r3["new"] == [])
        _get = lambda u, timeout=20: (_ for _ in ()).throw(WatchError("落ちた"))  # noqa: E731
        r4 = scan_maker("t", conf2, seen, record=False)
        t("　取得に失敗したら理由を残して止まる（新台なしにしない）",
          r4["problem"] and r4["new"] == [])
    finally:
        _get = real_get

    t("★機種らしくない文字列は取らない★",
      not _SLUGLIKE.match("../etc") and not _SLUGLIKE.match("A B")
      and _SLUGLIKE.match("lbinko"))

    ng = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--scan", action="store_true", help="全メーカーを見る（記録を更新）")
    ap.add_argument("--check", help="1社だけ試す（記録を更新しない）")
    args = ap.parse_args()
    if args.selftest:
        return selftest()

    cats = _sj.read_json(CATALOGS, expect=dict)["catalogs"]
    if args.check:
        conf = cats.get(args.check)
        if not conf:
            print(f"★{args.check} は maker-catalogs.json にありません★")
            return 1
        seen = _load_seen()
        r = scan_maker(args.check, conf, seen, record=False)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 1 if r["problem"] else 0

    if args.scan:
        seen = _load_seen()
        problems, found = [], []
        for mid, conf in cats.items():
            if conf.get("status") != "ACTIVE":
                continue
            r = scan_maker(mid, conf, seen)
            if r["problem"]:
                problems.append(f"{mid}: {r['problem']}")
                continue
            if r["first_time"]:
                print(f"{mid}: 初回なので {r['total']} 件を記録しました（新台としては扱いません）")
                continue
            for u in r["new"]:
                found.append({"maker": mid, **describe(u)})
            print(f"{mid}: 一覧 {r['total']} 件 / 新台 {len(r['new'])} 件")
        _save_seen(seen)
        if found:
            print(chr(10) + "★新台候補★")
            print(json.dumps(found, ensure_ascii=False, indent=1))
        if problems:
            print(chr(10) + "★確認が要ります（新台なしとは扱いません）★")
            for p in problems:
                print("  ✗ " + p)
            return 1
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except WatchError as e:
        print(f"★{e}★")
        raise SystemExit(1)
    except _sj.SafeJsonError as e:
        print(f"★入力データが読めません: {e}★")
        raise SystemExit(1)
    except Exception as e:
        print(f"★想定外の失敗 {type(e).__name__}: {e}★")
        raise SystemExit(1)

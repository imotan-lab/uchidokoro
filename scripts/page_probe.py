"""page_probe.py — 出典ページが前回から変わったかを軽く確かめる。

★何のためか★（2026-08-13・台帳#346・運営者の採用）
  育てる処理は毎回すべての名鑑を取り直して本人性まで確かめ直すので、
  1機種8分かかる。導入が遠い機種を毎日そうすると空振りが確定するため、
  いまは「導入日からの距離」で間引いている（最大7日おき）。
  そのぶん、★解析が出ても最大7日気づかない★。

  そこで「前回から変わったか」だけを先に確かめる。数秒で済むので、
  変化が無い日は毎日見ても負担にならない。

★★守る線（ここを外すと誤情報になる）★★
  ①「変わっていないから**書かない**」にだけ使う。
    最悪でも新しい情報に気づくのが遅れるだけで、誤りは出ない。
  ②「変わっていないから**前の材料を使い回して書く**」は絶対にしない。
    これをやると、出典が消えたり書き換わったりしても古い値を出し続ける。
  ③**新しい出典ページを探す工程は省かない**（別の名鑑に載った機種を見逃すため）。
    このモジュールは「既に知っているURL」しか見ない。

★何を見るか★
  ETag / Last-Modified（サーバーが出していれば条件つき取得で数百バイト）
  → 出していなければ本文を取って指紋（ハッシュ）を比べる。
  ★指紋は「読める本文」から作る★＝広告や日時表示の揺れで毎回変わらないよう、
  スクリプト・スタイルを除いた可視テキストを使う。

使い方:
    python scripts/page_probe.py --url https://example.com/x
    python scripts/page_probe.py --selftest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import new_machine_watch as _w        # noqa: E402

import os as _os_lp                 # noqa: E402
import sys as _sys_lp               # noqa: E402
_sys_lp.path.insert(0, _os_lp.path.dirname(_os_lp.path.abspath(__file__)))
import local_paths as _lp           # noqa: E402
STORE = _lp.doc("page_probe.json")
SCHEMA = 1


def _load() -> dict:
    try:
        with open(STORE, encoding="utf-8") as f:
            got = json.load(f)
        if not isinstance(got, dict) or got.get("schema_version") != SCHEMA:
            return {"schema_version": SCHEMA, "pages": {}}
        got.setdefault("pages", {})
        return got
    except FileNotFoundError:
        return {"schema_version": SCHEMA, "pages": {}}
    except Exception:                     # noqa: BLE001
        # ★読めないときは「控えが無い」として扱う★
        #   （消さない・上書きしない。次に書けたときに作り直る）
        return {"schema_version": SCHEMA, "pages": {}, "_unreadable": True}


def _save(got: dict) -> bool:
    """★読めなかったときは書かない★（他の控えを消してしまわないため）。"""
    if got.get("_unreadable"):
        return False
    try:
        os.makedirs(os.path.dirname(STORE), exist_ok=True)
        tmp = f"{STORE}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(got, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STORE)
        return True
    except Exception as e:                # noqa: BLE001
        print(f"  ページの控えを書けません（続けます）: {e}")
        return False


def fingerprint(html: str) -> str:
    """読める本文から指紋を作る（広告や日時の揺れを拾わないため）。"""
    text = " ".join(str(_w._visible_text(html) or "").split())
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _conditional_get(url: str, etag: str, modified: str, timeout: int = 20):
    """条件つき取得。★変わっていなければ本文を受け取らない★

    返り値: ("same", "") / ("changed", 本文) / ("unknown", 本文)
      same    … サーバーが「変わっていない」と答えた（304）
      changed … 取れたので、呼ぶ側が指紋で比べる
      unknown … 取れなかった（★変化なしとは扱わない★）
    """
    req = urllib.request.Request(url, headers={"User-Agent": _w.UA})
    if etag:
        req.add_header("If-None-Match", etag)
    if modified:
        req.add_header("If-Modified-Since", modified)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(_w.MAX_BYTES + 1)
            if len(body) > _w.MAX_BYTES:
                return "unknown", "", {}
            charset = r.headers.get_content_charset() or "utf-8"
            head = {"etag": r.headers.get("ETag") or "",
                    "last_modified": r.headers.get("Last-Modified") or ""}
            try:
                return "changed", body.decode(charset, "replace"), head
            except LookupError:
                return "changed", body.decode("utf-8", "replace"), head
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return "same", "", {}
        return "unknown", "", {}
    except Exception:                     # noqa: BLE001
        return "unknown", "", {}


def check(url: str, store: dict = None) -> dict:
    """1ページを確かめる。

    返り値の `state`:
      SAME      … 前回から変わっていない（★今日は書かなくてよい★）
      CHANGED   … 変わった（フル確認へ）
      FIRST     … 初めて見た（フル確認へ）
      UNKNOWN   … 確かめられなかった（★変化なしとは扱わない★＝フル確認へ）
    """
    got = store if store is not None else _load()
    rec = (got.get("pages") or {}).get(url) or {}
    state, body, head = _conditional_get(
        url, str(rec.get("etag") or ""), str(rec.get("last_modified") or ""))
    out = {"url": url, "state": "UNKNOWN", "why": ""}
    if state == "same":
        out.update(state="SAME", why="サーバーが『変わっていない』と答えました")
        return out
    if state == "unknown":
        out.update(state="UNKNOWN", why="確かめられませんでした（取得できません）")
        return out
    fp = fingerprint(body)
    old = str(rec.get("fingerprint") or "")
    got.setdefault("pages", {})[url] = {
        "fingerprint": fp,
        "etag": head.get("etag") or "",
        "last_modified": head.get("last_modified") or "",
    }
    if not old:
        out.update(state="FIRST", why="はじめて見るページです")
    elif old == fp:
        out.update(state="SAME", why="本文の指紋が同じです")
    else:
        out.update(state="CHANGED", why="本文が変わっています")
    return out


def check_all(urls: list) -> dict:
    """まとめて確かめる。★1つでも『変わった・分からない』ならフル確認★

    返り値: {"skip": bool, "rows": [...]}
      skip=True は「全部が SAME だった」時だけ。
      ★迷ったらフル確認★＝取得できなかったページを「変化なし」に数えない。
    """
    got = _load()
    rows = [check(u, got) for u in urls]
    _save(got)
    skip = bool(rows) and all(r["state"] == "SAME" for r in rows)
    return {"skip": skip, "rows": rows}


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅ " if cond else "❌ ") + name)

    t("★★指紋は読める本文から作る（広告や日時の揺れで変わらない）★★",
      fingerprint("<html><body><p>天井999G</p><script>var t=1</script></body></html>")
      == fingerprint("<html><body><p>天井999G</p><script>var t=2</script></body></html>"))
    t("　本文が変われば指紋も変わる",
      fingerprint("<p>天井999G</p>") != fingerprint("<p>天井777G</p>"))

    _bak = globals()["_conditional_get"]
    try:
        globals()["_conditional_get"] = lambda u, e, m, timeout=20: ("same", "", {})
        t("★★サーバーが『変わっていない』と答えたら SAME★★",
          check("https://x.test/a", {"pages": {}})["state"] == "SAME")

        globals()["_conditional_get"] = lambda u, e, m, timeout=20: ("unknown", "", {})
        t("★★確かめられなかったら UNKNOWN★★"
          "（★変化なしと扱わない＝フル確認へ回す★）",
          check("https://x.test/a", {"pages": {}})["state"] == "UNKNOWN")

        globals()["_conditional_get"] = lambda u, e, m, timeout=20: (
            "changed", "<p>天井999G</p>", {})
        st = {"pages": {}}
        t("　はじめて見るページは FIRST",
          check("https://x.test/a", st)["state"] == "FIRST")
        t("★★2回目に同じ本文なら SAME★★",
          check("https://x.test/a", st)["state"] == "SAME")
        globals()["_conditional_get"] = lambda u, e, m, timeout=20: (
            "changed", "<p>天井777G</p>", {})
        t("★★本文が変われば CHANGED★★",
          check("https://x.test/a", st)["state"] == "CHANGED")

        # ★1つでも SAME でなければ、まとめては飛ばさない★
        globals()["_conditional_get"] = lambda u, e, m, timeout=20: (
            "same", "", {}) if u.endswith("a") else ("unknown", "", {})
        _bak_load, _bak_save = globals()["_load"], globals()["_save"]
        globals()["_load"] = lambda: {"schema_version": SCHEMA, "pages": {}}
        globals()["_save"] = lambda g: True
        try:
            t("★★1つでも確かめられなければ、飛ばさない★★",
              check_all(["https://x.test/a", "https://x.test/b"])["skip"] is False)
            globals()["_conditional_get"] = lambda u, e, m, timeout=20: (
                "same", "", {})
            t("　全部 SAME のときだけ飛ばす",
              check_all(["https://x.test/a", "https://x.test/b"])["skip"] is True)
            t("　1つも見ていないときは飛ばさない",
              check_all([])["skip"] is False)
        finally:
            globals()["_load"], globals()["_save"] = _bak_load, _bak_save
    finally:
        globals()["_conditional_get"] = _bak

    t("★★控えが読めないときは書かない★★（消してしまわないため）",
      _save({"schema_version": SCHEMA, "pages": {}, "_unreadable": True})
      is False)

    ng = sum(1 for _, o in results if not o)
    print()
    print("%d/%d 合格" % (len(results) - ng, len(results)))
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="出典ページが変わったかを軽く確かめる")
    ap.add_argument("--url", action="append", default=[])
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.url:
        ap.print_help()
        return 0
    got = check_all(a.url)
    for r in got["rows"]:
        print(f"  {r['state']:<8} {r['url'][:66]}  {r['why']}")
    print(f"→ 今日は{'飛ばせます' if got['skip'] else 'フル確認が要ります'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

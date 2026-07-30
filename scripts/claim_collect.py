"""claim_collect.py — 出典ページを取ってきて「証拠」として保存する。

★なぜ要るか（2026-07-30・Codex指摘4）★
  2つの自動タスクの手順書に「出典を集める」と書いたが、
  **公開ゲートが読む場所に証拠と台帳を作るコマンドが存在しなかった**。
  つまり「材料が貯まる」こと自体が成立していなかった。

★AIを証拠の経路に入れない★
  取得・保存・値の取り出しは、このスクリプトが実際のバイト列から行う。
  AI（自動タスク）が渡してよいのは **URLと、どの機種の何を探すか** だけ。
  AIが読んだ内容を書き写す形にすると、要約や記憶が混ざる余地が残る。

  保存するもの:
    assets/data/claim-evidence/<sha256>.json   … 取得結果（本文の指紋つき）
    assets/data/claim-evidence/raw/<sha256>.bin … 生の応答そのもの
  ★生を残す理由★：あとから別の取り出し方で検算できるようにするため。
  指紋だけだと「そのとき何を読んだか」を再現できない。

使い方:
    # 取ってきて証拠にする（何も判断しない・保存するだけ）
    python scripts/claim_collect.py fetch --url https://example.com/x --slug hokuto

    # 保存済みの証拠から、機種の同定に使える形を確かめる
    python scripts/claim_collect.py show --sha <sha256>

    python scripts/claim_collect.py --selftest
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import claim_evidence as ce           # noqa: E402
import claim_inventory as ci          # noqa: E402
import safe_json as _sj               # noqa: E402

RAW_DIR = os.path.join(ce.EVIDENCE_DIR, "raw")
UA = "uchidokoro-claim-collector/1.0 (+https://uchidokoro.com)"
FETCHER_VERSION = "claim_collect/1"

# 取ってよい相手（出典レジストリに載っているホストだけ）。
#   ★知らないホストからは取らない★ 誰の情報か分からないものを証拠にしない。
MAX_BYTES = 5 * 1024 * 1024


class CollectError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def allowed_hosts() -> set:
    """出典レジストリに登録されているホスト（ここに無い相手からは取らない）。"""
    try:
        reg = _sj.read_json(os.path.join(BASE, "assets", "data", "source-registry.json"),
                            expect=dict)
    except Exception as e:
        raise CollectError(f"出典レジストリが読めません: {e}")
    hosts = set()
    # ★ACTIVE な発行者の canonical_hosts だけ★
    #   停止中（status が ACTIVE 以外）の相手からは取らない。
    for _pid, pub in (reg.get("publishers") or {}).items():
        if not isinstance(pub, dict) or pub.get("status") != "ACTIVE":
            continue
        for h in (pub.get("canonical_hosts") or []):
            if isinstance(h, str) and h.strip():
                hosts.add(h.strip().lower())
    if not hosts:
        raise CollectError("出典レジストリに使えるホストがありません（default deny）")
    return hosts


def _strip_tags(html: str) -> str:
    """タグを外して本文だけにする（表の行が1行になるよう区切りを残す）。"""
    t = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1\s*>", " ", html)
    t = re.sub(r"(?i)</(tr|p|div|li|h[1-6]|table)\s*>", "\n", t)
    t = re.sub(r"(?i)</t[dh]\s*>", "\t", t)
    t = re.sub(r"(?s)<[^>]+>", "", t)
    t = (t.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    t = unicodedata.normalize("NFKC", t)
    t = re.sub(r"[ \t]+", " ", t)
    return "\n".join(ln.strip() for ln in t.splitlines() if ln.strip())


def _title(html: str) -> str:
    m = re.search(r"(?is)<title[^>]*>(.*?)</title\s*>", html)
    if not m:
        m = re.search(r"(?is)<h1[^>]*>(.*?)</h1\s*>", html)
    return unicodedata.normalize("NFKC", re.sub(r"(?s)<[^>]+>", "", m.group(1)).strip()) if m else ""



# 証拠として使える単位（claim_evidence.UNIT_TYPES に合わせる）
#   ★ページ全体は証拠にしない★ どの行の話か決まらないと、
#   別の行の数字を流用できてしまう。
#   ★正規表現は文字列を組み立てて作る★（2026-07-30）
#     バックスラッシュの並びをソースに直接書くと、編集の経路によっては
#     制御文字（0x08 / 0x01）に化けて**検査が何も見つけなくなる**。
#     今日3回起きたので、化けようのない書き方に統一する。
_B = "(?![A-Za-z0-9])"       # 単語境界の代わり（タグ名の直後が英数字でない）
_S = "[ \t\r\n]*"


def _tag_re(name, capture_level=False):
    head = "<" + name + "([1-6])" if capture_level else "<" + name
    close = "</" + name + "([1-6])?" if capture_level else "</" + name
    return re.compile("(?is)" + head + _B + "[^>]*>(.*?)" + close + _S + ">")


_UNIT_PATTERNS = (
    ("TABLE_ROW", _tag_re("tr")),
    ("HEADING", _tag_re("h", capture_level=True)),
    ("LIST_ITEM", _tag_re("li")),
    ("PARAGRAPH", _tag_re("p")),
)


def extract_units(html: str) -> list:
    """HTMLから証拠単位を切り出す。★保存したバイト列からやり直せる★

    戻り値: [{"index", "unit_type", "dom_path", "text"}, ...]
    同じ位置は1回だけ（表の行の中の段落を二重に数えない）。
    """
    out, seen = [], set()
    for unit_type, rx in _UNIT_PATTERNS:
        for i, m in enumerate(rx.finditer(html), 1):
            span = (m.start(), m.end())
            if any(span[0] >= a and span[1] <= b for a, b in seen):
                continue
            inner = m.group(2) if unit_type == "HEADING" else m.group(1)
            text = _strip_tags(inner).replace(chr(10), " ").strip()
            if not text or len(text) > 600:
                continue
            seen.add(span)
            out.append({"unit_type": unit_type,
                        "dom_path": f"{unit_type.lower()}[{i}]",
                        "text": text})
    for n, u in enumerate(out):
        u["index"] = n
    return out


def _raw_path(raw_sha: str) -> str:
    return os.path.join(RAW_DIR, f"{raw_sha}.bin")


def load_raw(raw_sha: str) -> str:
    """保存した生の応答を読み直す。★指紋が合わなければ使わない★"""
    fp = _raw_path(raw_sha)
    if not os.path.isfile(fp):
        raise CollectError(f"生の応答がありません: {raw_sha}")
    body = open(fp, "rb").read()
    got = hashlib.sha256(body).hexdigest()
    if got != raw_sha:
        raise CollectError(f"生の応答が書き換えられています: {raw_sha} → {got}")
    return body.decode("utf-8", "replace")


def make_unit_evidence(raw_sha: str, index: int, slug: str, meta: dict) -> dict:
    """★保存したバイト列から証拠単位を作る★（AIの読み取りを介さない）

    meta には fetch のときに記録した URL・取得時刻などを渡す。
    """
    html = load_raw(raw_sha)
    units = extract_units(html)
    hit = next((u for u in units if u["index"] == index), None)
    if hit is None:
        raise CollectError(f"その単位はありません: index={index}（全 {len(units)} 件）")

    rows = _sj.read_rows(os.path.join(BASE, "assets", "data", "machines.json"))
    machine = next((m for m in rows if m.get("slug") == slug), None)
    if machine is None:
        raise CollectError(f"machines.json に {slug} がありません")
    missing = ci.identity_missing(machine)
    if missing:
        raise CollectError(f"{slug} の型式が未登録です: {missing}")
    ident = machine.get("identity") or {}

    text = _strip_tags(html)
    ev = {
        "schema_version": ce.SCHEMA_VERSION,
        "fetch": {"requested_url": meta["requested_url"],
                  "final_url": meta["final_url"],
                  "fetched_at": meta["fetched_at"],
                  "http_status": 200,
                  "response_sha256": raw_sha},
        "page": {"title": _title(html),
                 "body_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()},
        "evidence_unit": {"unit_type": hit["unit_type"],
                          "dom_path": hit["dom_path"],
                          "text": hit["text"]},
        "machine_identity": {
            "manufacturer_id": ident.get("manufacturer_id"),
            "regulatory_model_code": ident.get("regulatory_model_code"),
            **({"market_release_date": ci.release_date_of(ident)}
               if ci.release_date_of(ident) else {}),
        },
        "fetcher_version": FETCHER_VERSION,
        "attestation_state": "UNATTESTED_METADATA",
    }
    ev["evidence_sha256"] = ce.content_sha256(ev)
    ce.validate_evidence(ev)
    return ev


def fetch(url: str, slug: str, timeout: int = 20) -> dict:
    """1ページ取ってきて証拠として保存する。★中身の判断はしない★"""
    if not url.lower().startswith("https://"):
        raise CollectError("https のページだけを出典にします")
    host = _host(url)
    allow = allowed_hosts()
    if host not in allow:
        raise CollectError(
            f"出典レジストリに無いホストです: {host}\n"
            f"  先に assets/data/source-registry.json へ運営元・系列を登録してください"
            f"（誰の情報か分からないものを証拠にしない）")

    machine = None
    try:
        rows = _sj.read_rows(os.path.join(BASE, "assets", "data", "machines.json"))
        machine = next((m for m in rows if m.get("slug") == slug), None)
    except Exception as e:
        raise CollectError(f"機種データが読めません: {e}")
    if machine is None:
        raise CollectError(f"machines.json に {slug} がありません")

    ident = machine.get("identity") or {}
    missing = ci.identity_missing(machine)
    if missing:
        # ★機種を特定できないうちは証拠にしない★
        #   どの台の話か決められない証拠は、あとで別機種に流用される。
        raise CollectError(f"{slug} の型式が未登録です: {missing}")

    allow = allowed_hosts()
    if host not in allow:
        raise CollectError(
            f"出典レジストリに無いホストです: {host}"
            f" — 先に assets/data/source-registry.json へ運営元・系列を登録してください"
            f"（誰の情報か分からないものを証拠にしない）")

    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(MAX_BYTES + 1)
            status = r.status
            final_url = r.geturl()
            charset = (r.headers.get_content_charset() or "utf-8")
    except urllib.error.HTTPError as e:
        raise CollectError(f"取得できません（HTTP {e.code}）: {url}")
    except Exception as e:
        raise CollectError(f"取得できません（{type(e).__name__}）: {url}")
    if len(body) > MAX_BYTES:
        raise CollectError("ページが大きすぎます（5MB超）")
    if status != 200:
        raise CollectError(f"HTTP {status} なので証拠にしません")
    if _host(final_url) not in allow:
        # ★転送先が知らないホストなら受け取らない★
        raise CollectError(f"転送先が出典レジストリにありません: {_host(final_url)}")

    raw_sha = hashlib.sha256(body).hexdigest()
    os.makedirs(RAW_DIR, exist_ok=True)
    with open(os.path.join(RAW_DIR, f"{raw_sha}.bin"), "wb") as f:
        f.write(body)

    html = body.decode(charset, "replace")
    # ★fetch は保存と切り出しだけ★（証拠として確定させるのは unit の役目）
    #   どの単位がその機種のどの値かを決めるのは判断なので、段を分ける。
    meta = {"requested_url": url, "final_url": final_url, "fetched_at": _now()}
    with open(os.path.join(RAW_DIR, f"{raw_sha}.meta.json"), "w",
              encoding="utf-8", newline=chr(10)) as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    units = extract_units(html)
    return {"raw_sha": raw_sha, "host": host, "title": _title(html),
            "units": len(units), "meta": meta,
            "hint": f"python scripts/claim_collect.py units --raw-sha {raw_sha} --contains <探す語>"}


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    def raises(fn, word=""):
        try:
            fn()
            return False
        except CollectError as e:
            return (word in str(e)) if word else True
        except Exception:
            return False

    t("★http は出典にしない（https だけ）★",
      raises(lambda: fetch("http://example.com/x", "hokuto"), "https"))
    t("★出典レジストリに無いホストからは取らない★",
      raises(lambda: fetch("https://evil.example/x", "hokuto"), "レジストリ"))
    t("★機種データに無い slug では取らない★",
      raises(lambda: fetch("https://www.p-world.co.jp/x", "zzz_none"), "ありません"))
    t("★★型式が未登録の機種では証拠にしない★★（別機種に流用されるため）",
      raises(lambda: fetch("https://www.p-world.co.jp/x", "hokuto"), "型式"))

    t("　タグを外して本文にできる",
      _strip_tags("<p>設定1</p><table><tr><td>97.2%</td></tr></table>")
      == "設定1\n97.2%")
    t("　scriptの中身は本文に混ぜない",
      "alert" not in _strip_tags("<script>alert('x')</script><p>本文</p>"))
    t("　タイトルを取れる", _title("<title>Sゴーゴージャグラー3KA</title>")
      == "Sゴーゴージャグラー3KA")
    t("　title が無ければ h1 を使う",
      _title("<h1>Sテスト機KA</h1>") == "Sテスト機KA")
    t("★出典レジストリのホスト一覧が空でない（空だと何も取れない）★",
      len(allowed_hosts()) > 0)
    t("★証拠の読み出しは (証拠, 理由) を返す（理由を捨てない）★",
      isinstance(ce.load_evidence("x" * 64), tuple)
      and ce.load_evidence("x" * 64)[0] is None
      and ce.load_evidence("x" * 64)[1] == "EVIDENCE_REF_NOT_SHA256"
      and ce.load_evidence("a" * 64)[1] == "EVIDENCE_NOT_FOUND")
    t("★★正規表現が制御文字に化けていない★★（今日3回起きた・単位が0件になる）",
      all(chr(8) not in rx.pattern and chr(1) not in rx.pattern
          for _n, rx in _UNIT_PATTERNS))
    t("　表の行を単位として切り出せる",
      [u["text"] for u in extract_units(
          "<table><tr><td>型式名</td><td>Sテス卜KA</td></tr></table>")]
      == ["型式名 Sテス卜KA"])
    t("★保存した生の応答が書き換えられていたら使わない★",
      raises(lambda: load_raw("f" * 64), "ありません"))

    ng = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("fetch", help="出典ページを取って証拠として保存する")
    p.add_argument("--url", required=True)
    p.add_argument("--slug", required=True)

    p = sub.add_parser("units", help="保存した応答から証拠単位の候補を並べる")
    p.add_argument("--raw-sha", dest="raw_sha", required=True)
    p.add_argument("--contains", default="", help="この語を含む単位だけ出す")

    p = sub.add_parser("unit", help="選んだ単位を証拠として確定させる")
    p.add_argument("--raw-sha", dest="raw_sha", required=True)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--slug", required=True)

    p = sub.add_parser("show", help="保存済みの証拠を見る")
    p.add_argument("--sha", required=True)

    p = sub.add_parser("hosts", help="取ってよいホストの一覧")

    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if args.cmd == "fetch":
        r = fetch(args.url, args.slug)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 0
    if args.cmd == "units":
        html = load_raw(args.raw_sha)
        us = extract_units(html)
        if args.contains:
            us = [u for u in us if args.contains in u["text"]]
        for u in us[:60]:
            print(f"[{u['index']:>4}] {u['unit_type']:<12} {u['text'][:100]}")
        print(chr(10) + f"該当 {len(us)} 件")
        return 0
    if args.cmd == "unit":
        meta_fp = os.path.join(RAW_DIR, f"{args.raw_sha}.meta.json")
        if not os.path.isfile(meta_fp):
            print(f"★取得時の記録がありません: {args.raw_sha}★")
            return 1
        meta = _sj.read_json(meta_fp, expect=dict)
        ev = make_unit_evidence(args.raw_sha, args.index, args.slug, meta)
        sha = ce.write_evidence(ev)
        print(json.dumps({"evidence_sha256": sha, "unit": ev["evidence_unit"],
                          "path": ce.evidence_path(sha)}, ensure_ascii=False, indent=1))
        return 0
    if args.cmd == "show":
        # ★load_evidence は (証拠, 理由) を返す★ 理由を捨てない
        ev, why = ce.load_evidence(args.sha)
        if not ev:
            print(f"証拠を使えません: {args.sha} — {why}")
            return 1
        print(json.dumps({k: v for k, v in ev.items() if k != "evidence_unit"},
                         ensure_ascii=False, indent=1))
        print("--- 本文の先頭 ---")
        print((ev.get("evidence_unit") or {}).get("text", "")[:800])
        return 0
    if args.cmd == "hosts":
        for h in sorted(allowed_hosts()):
            print(h)
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except CollectError as e:
        print(f"★{e}★")
        raise SystemExit(1)
    except _sj.SafeJsonError as e:
        print(f"★入力データが読めません: {e}★")
        raise SystemExit(1)
    except Exception as e:
        print(f"★想定外の失敗 {type(e).__name__}: {e}★")
        raise SystemExit(1)

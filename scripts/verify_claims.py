# -*- coding: utf-8 -*-
"""出典実在チェッカー（決定論・LLM非依存）＝幻覚値の無人公開を機械的に阻止する関所。

自動タスク（auto-add/new-machine）がpreview→complete昇格や新規完全記事で
「外部から新しい数字」を書こうとする時、その数字の出典を★コードが★再取得して
検証する。AIの「読んだ」という主張を信用せず、文字列一致で機械確認する。

★これが防ぐもの★
  2026-04のokidoki_gorgeous事故: 自動タスクが5サイト照合したと主張した上で
  「スロパチ=580G」という実在しない値を報告した（幻覚）。本スクリプトなら
  スロパチのページを実際に取得→「580」をgrep→存在しない→不合格で止まる。

検証内容（1クレームごと）:
  C1. 出典URLが実在しHTTP 200で取得できる
  C2. ページに機種の同定文字列（identity.must_contain 全て）が存在する
      ＝旧機種・別機種のページを出典にしていないか（新旧混同ガード）
  C3. 引用文（quote）が正規化一致でページ本文に実在する（逐語引用のみ合格）
  C4. 主張値（value）がページ本文に存在する
集計判定:
  critical=true のフィールドは「合格クレームの出典ドメインが min-domains 種類以上」必須。
  critical=false は1ドメイン合格でOK。1つでも不足があれば exit 1（昇格禁止）。

使い方:
  python scripts/verify_claims.py --file claims.json [--min-domains 2]
  exit 0 = 全関門通過（無人昇格を許可してよい）/ exit 1 = 不合格（preview維持＋台帳へ）

claims.json の形式:
{
  "slug": "yabachiba",
  "identity": { "must_contain": ["ヤバチバ", "2026"] },
  "claims": [
    { "field": "天井G数", "value": "999", "critical": true,
      "url": "https://...", "quote": "999G+α" },
    { "field": "狙い目等価", "value": "700", "critical": true,
      "url": "https://...", "quote": "狙い目(等価) 700G~" }
  ]
}
※quoteは出典ページの原文を一字一句そのまま書くこと（要約・言い換えは不合格になる）
※同一フィールドを複数URL（別ドメイン）で複数クレームにするとドメイン数が満たせる
"""
import argparse
import html
import json
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) uchidokoro-claim-verifier/1.0"
_page_cache = {}


def _now():
    return datetime.now().strftime("%H:%M:%S")


def fetch_text(url):
    """URLを取得しタグを除去したプレーンテキストを返す。失敗はNone（安全側）。"""
    if url in _page_cache:
        return _page_cache[url]
    text = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=25) as res:
                if res.status != 200:
                    continue
                raw = res.read()
            for enc in ("utf-8", "cp932", "euc-jp"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            if text is not None:
                break
        except Exception:
            continue
    if text is not None:
        text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = html.unescape(text)
    _page_cache[url] = text
    return text


_TRANS = str.maketrans({
    "〜": "~",  # 〜 WAVE DASH
    "～": "~",  # ～ FULLWIDTH TILDE
    "−": "-",  # − MINUS SIGN
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-",
})


def normalize(s):
    """NFKC正規化＋空白全除去＋チルダ/ダッシュ統一。表記ゆれで誤不合格にならない範囲の吸収のみ。"""
    s = unicodedata.normalize("NFKC", s or "")
    s = s.translate(_TRANS)
    return re.sub(r"\s+", "", s)


def domain_of(url):
    d = urllib.parse.urlparse(url).netloc.lower()
    return d[4:] if d.startswith("www.") else d


def run(path, min_domains):
    data = json.loads(open(path, encoding="utf-8").read())
    slug = data.get("slug", "?")
    identity = [normalize(s) for s in (data.get("identity") or {}).get("must_contain", [])]
    claims = data.get("claims") or []
    print(f"[{_now()}] === 出典実在チェック開始: {slug}（クレーム{len(claims)}件・critical必要ドメイン数{min_domains}） ===")
    if not identity:
        print(f"[{_now()}] ❌ identity.must_contain が空（同定ガード必須）→不合格")
        return 1
    if not claims:
        print(f"[{_now()}] ❌ クレーム0件→不合格")
        return 1

    field_pass_domains = {}
    field_critical = {}
    any_fail = False

    for i, c in enumerate(claims):
        field = c.get("field", f"claim{i}")
        url = c.get("url", "")
        quote = c.get("quote", "")
        value = str(c.get("value", ""))
        critical = bool(c.get("critical", True))
        field_critical[field] = field_critical.get(field, False) or critical
        tag = f"[{field}] {domain_of(url)}"

        if not url or not quote or not value:
            print(f"[{_now()}] ❌ {tag}: url/quote/value のいずれかが空→不合格")
            any_fail = True
            continue
        text = fetch_text(url)
        if text is None:
            print(f"[{_now()}] ❌ {tag}: C1 URL取得失敗（{url}）")
            any_fail = True
            continue
        ntext = normalize(text)
        missing_id = [s for s in identity if s not in ntext]
        if missing_id:
            print(f"[{_now()}] ❌ {tag}: C2 同定文字列がページに無い {missing_id}（別機種/旧機種ページの疑い）")
            any_fail = True
            continue
        if normalize(quote) not in ntext:
            print(f"[{_now()}] ❌ {tag}: C3 引用文がページに実在しない（幻覚/言い換えの疑い）: 「{quote}」")
            any_fail = True
            continue
        if normalize(value) not in ntext:
            print(f"[{_now()}] ❌ {tag}: C4 値 {value} がページに存在しない")
            any_fail = True
            continue
        field_pass_domains.setdefault(field, set()).add(domain_of(url))
        print(f"[{_now()}] ✅ {tag}: C1〜C4通過（quote実在確認）")

    verdict_fail = any_fail
    for field, critical in field_critical.items():
        got = len(field_pass_domains.get(field, set()))
        need = min_domains if critical else 1
        status = "✅" if got >= need else "❌"
        if got < need:
            verdict_fail = True
        print(f"[{_now()}] {status} 集計[{field}]: 合格ドメイン{got}/{need}必要{'（critical）' if critical else ''}")

    if verdict_fail:
        print(f"[{_now()}] === 判定: ❌ 不合格 → 無人昇格禁止（preview維持＋要確認台帳へ） ===")
        return 1
    print(f"[{_now()}] === 判定: ✅ 全関門通過 → 検証済みの値のみで昇格可 ===")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="claims.json のパス")
    ap.add_argument("--min-domains", type=int, default=2, help="criticalフィールドに必要な合格ドメイン数（既定2）")
    args = ap.parse_args()
    sys.exit(run(args.file, args.min_domains))


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""
shadow_gold.py — gold set（実Web正解セット）の機械検証つき凍結（決定論・LLM非依存）

ワークフローが収集した候補（機種×claim×逐語quote×URL）を、コードが再検証して
合格分だけを gold_set.json に凍結する。Codexの結果を見る前に期待値を固定するのが目的
（評価の循環禁止）。凍結後は追記・変更しない（変更が必要なら新版ファイル＋新epoch）。

検証:
  - asserted（値あり）claim: verify_claims.py --min-domains 1 で
    URL実在・機種同定（title+本文）・quote逐語一致・値in-quote を機械確認
  - asserted_none（天井非搭載の明示）claim: 値が無いためverify_claimsは使えず、
    本スクリプト内蔵の軽量チェック（fetch→正規化本文にquoteが逐語存在＋機種名が
    title/本文に存在）で確認する

使い方:
  python scripts/shadow_gold.py freeze --candidates <収集結果JSON> --out <gold_set.json>
  python scripts/shadow_gold.py stats --gold <gold_set.json>
  python scripts/shadow_gold.py --selftest
"""
from __future__ import annotations
import argparse
import datetime
import gzip
import hashlib
import io
import json
import re
import subprocess
import sys
import unicodedata
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent
SCRIPTS = BASE / "scripts"
DOC = Path(r"C:/Users/imao_/Documents/uchidokoro")
TMP_CLAIMS = DOC / "gpt_research" / "claims_check"
ALLOWED_DOMAINS = ("chonborista.com", "1geki.jp", "nana-press.com", "slopachi-quest.com")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) uchidokoro-gold-freeze"


def now_iso() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _norm(s: str) -> str:
    """verify_claimsと同思想の正規化（NFKC・空白除去・チルダ統一）で逐語照合"""
    s = unicodedata.normalize("NFKC", s or "")
    s = re.sub(r"\s+", "", s)
    return s.replace("〜", "~").replace("～", "~")


def _fetch(url: str, timeout=30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    text = raw.decode("utf-8", errors="replace")
    text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>|<!--[\s\S]*?-->",
                  " ", text)
    return text


_PREFIX_RE = re.compile(
    r"^(Lパチスロ|Lアニマルスロット|A-SLOT\+?|SB|スマスロ|パチスロ|スロット|L|S)\s*")

# ★同シリーズ・類似名機種の同定強化表（2026-07-17 チャッピー条件）★
# required: must_containに必ず加える識別トークン / forbidden: ページtitleに
# 含まれていたら旧作・別機種ページとして不合格にするトークン
IDENTITY_OVERRIDES = {
    "karakuri2":        {"required": ["からくりサーカス", "2"], "forbidden": []},
    "valvrave2":        {"required": ["ヴァルヴレイヴ", "2"], "forbidden": []},
    "enen2":            {"required": ["炎炎", "2"], "forbidden": []},
    "umineko2":         {"required": ["うみねこ", "2"], "forbidden": []},
    "hokuto_tensei2":   {"required": ["転生", "2"], "forbidden": []},
    "okidoki_gorgeous": {"required": ["沖ドキ", "ゴージャス"],
                         "forbidden": ["アンコール", "GOLD", "BLACK"]},
    "okidoki_encore":   {"required": ["沖ドキ", "アンコール"],
                         "forbidden": ["ゴージャス", "GOLD", "BLACK"]},
    "gundam_seed":      {"required": ["ガンダムSEED"], "forbidden": ["ユニコーン"]},
    "gundam_uc2":       {"required": ["ユニコーン"], "forbidden": ["SEED"]},
    "ultraman_final":   {"required": ["ULTRAMAN", "最終決戦"], "forbidden": []},
    "nangoku_special":  {"required": ["南国育ち", "SPECIAL"], "forbidden": []},
    "thunder_v":        {"required": ["サンダーV"], "forbidden": ["リボルト"]},
}

# claim_key末尾とceiling_type・unitの整合表（2026-07-17 チャッピー条件）
KEY_SUFFIX_BY_TYPE = {"game": "game", "point": "point", "cycle": "cycle",
                      "through": "through", "none": "none"}
UNIT_COMPAT = {"game": {"G"}, "point": {"pt", "Gpt"}, "cycle": {"cycle"},
               "through": {"through"}, "none": {None, ""}}


def migrate_claim_key(claim_key: str, ceiling_type: str) -> str:
    """キー末尾をceiling_typeに一致させる決定論移行（例: reset.game+point→reset.point）"""
    suffix = KEY_SUFFIX_BY_TYPE.get(ceiling_type)
    if not suffix:
        return claim_key
    parts = (claim_key or "").split(".")
    if len(parts) == 3 and parts[0] == "ceiling" and parts[2] != suffix:
        return f"{parts[0]}.{parts[1]}.{suffix}"
    return claim_key


def _title_of(url: str, _cache={}) -> str:
    if url in _cache:
        return _cache[url]
    try:
        html_text = _fetch(url)
        m = re.search(r"<title[^>]*>([\s\S]*?)</title>", html_text, re.I)
        _cache[url] = _norm(m.group(1)) if m else ""
    except Exception:
        _cache[url] = ""
    return _cache[url]


def forbidden_hit(slug: str, url: str) -> str | None:
    """禁止トークンがページtitleにあればそのトークンを返す（旧作・別機種ガード）"""
    ov = IDENTITY_OVERRIDES.get(slug)
    if not ov or not ov.get("forbidden"):
        return None
    title = _title_of(url)
    for tok in ov["forbidden"]:
        if _norm(tok) in title:
            return tok
    return None


def _identity_tokens(name: str) -> list[str]:
    """同定トークン候補（強い順）。①型式接頭辞を除いた本体 ②その第1セグメント。
    フル名は解析ページにサブタイトルまで書かれないことが多い（kabaneri等で実測）ため、
    決定論の段階的フォールバックにする（判別digitは本体・第1セグメント双方に残る）"""
    stripped = name
    for _ in range(3):
        new = _PREFIX_RE.sub("", stripped).strip()
        if new == stripped:
            break
        stripped = new
    tokens = []
    if len(_norm(stripped)) >= 3:
        tokens.append(stripped)
    first = stripped.split()[0] if stripped.split() else ""
    if len(_norm(first)) >= 3 and _norm(first) != _norm(stripped):
        tokens.append(first)
    return tokens or [name]


def verify_none_claim(name: str, url: str, quote: str,
                      slug_hint: str | None = None) -> tuple[bool, str]:
    """天井非搭載（値なし）claimの軽量検証: quote逐語＋機種同定（title∧本文）"""
    if not any(d in url for d in ALLOWED_DOMAINS):
        return False, "許可外ドメイン"
    if len(_norm(quote)) < 8:
        return False, "quoteが短すぎる"
    try:
        html_text = _fetch(url)
    except Exception as e:
        return False, f"取得失敗: {type(e).__name__}"
    body = _norm(re.sub(r"<[^>]+>", " ", html_text))
    if _norm(quote) not in body:
        return False, "quoteが本文に逐語一致しない"
    m = re.search(r"<title[^>]*>([\s\S]*?)</title>", html_text, re.I)
    title = _norm(m.group(1)) if m else ""
    required = [_norm(t) for t in (IDENTITY_OVERRIDES.get(slug_hint or "", {}) or {}).get("required", [])]
    for tok in _identity_tokens(name):
        t = _norm(tok)
        # verify_claims C2と同じ強度: title・本文の両方に同定トークン（＋必須トークン全部）
        if (t in title and t in body
                and all(rq in title and rq in body for rq in required)):
            return True, f"none_check:合格（同定={tok}）"
    return False, "機種同定不可（title+本文の両方に機種名が必要）"


def verify_asserted_claim(slug: str, name: str, claim_key: str, value,
                          url: str, quote: str, idx: int) -> tuple[bool, str]:
    """値ありclaimはverify_claims.pyで検証（既存の関所を正として流用）。
    同定トークンは強い順のカスケード（本体→第1セグメント）で試す"""
    TMP_CLAIMS.mkdir(parents=True, exist_ok=True)
    v = value
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    last = "検証未実行"
    required = (IDENTITY_OVERRIDES.get(slug, {}) or {}).get("required", [])
    for ti, tok in enumerate(_identity_tokens(name)):
        must = list(dict.fromkeys([tok] + required))  # 順序保持で重複除去
        cf = {"slug": slug, "identity": {"must_contain": must},
              "claims": [{"field": f"天井_{claim_key}", "value": str(v), "critical": False,
                          "url": url, "quote": quote}]}
        p = TMP_CLAIMS / f"gold_{slug}_{claim_key.replace('.', '_')}_{idx}_{ti}.json"
        p.write_text(json.dumps(cf, ensure_ascii=False), encoding="utf-8")
        try:
            r = subprocess.run([sys.executable, str(SCRIPTS / "verify_claims.py"),
                                "--file", str(p), "--min-domains", "1"],
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=120)
            if r.returncode == 0:
                return True, f"verify_claims:exit0（同定={'+'.join(must)}）"
            # 不合格理由のC分類を出力から抽出（内訳集計用・秘密値は含まない）
            m = re.search(r"(C[0-4])[ :：]", r.stdout or "")
            last = f"verify_claims:exit{r.returncode}" + (f":{m.group(1)}" if m else "")
        except Exception as e:
            last = f"検証実行失敗: {type(e).__name__}"
    return False, last


def load_candidates(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("result", data)
        data = data.get("collected", data)
    if not isinstance(data, list):
        raise ValueError("候補形式が不正（collected配列が見つからない）")
    return data


def freeze(candidates_path: Path, out_path: Path) -> int:
    if out_path.exists():
        print(f"❌ {out_path} は既に存在する（gold setは凍結後変更不可。新版は別名で）")
        return 1
    collected = load_candidates(candidates_path)
    names = {}
    mdata = json.loads((BASE / "assets" / "data" / "machines.json").read_text(encoding="utf-8"))
    for m in (mdata["machines"] if isinstance(mdata, dict) else mdata):
        names[m["slug"]] = m["name"]

    entries, rejects, seen = [], [], set()
    total_candidates = duplicates_skipped = 0
    for block in collected:
        slug = block.get("slug")
        name = names.get(slug, slug)
        for i, c in enumerate(block.get("candidates") or []):
            total_candidates += 1
            url = (c.get("url") or "").strip()
            quote = (c.get("quote") or "").strip()
            exp = c.get("expected") or {}
            # ★claim_key移行（末尾をceiling_typeに一致させる・2026-07-17チャッピー条件）★
            ckey = migrate_claim_key(c.get("claim_key") or "", exp.get("ceiling_type") or "")
            # ★重複判定はscope・valueまで含める（複合天井の別要素を捨てない）★
            key = (slug, ckey, url, exp.get("scope"), exp.get("value"))
            if key in seen:
                duplicates_skipped += 1
                continue
            seen.add(key)
            if not any(d in url for d in ALLOWED_DOMAINS):
                rejects.append((slug, ckey, "許可外ドメイン", url))
                continue
            # unit整合（キー末尾＝ceiling_type⇔unitの互換）
            ctype = exp.get("ceiling_type") or ""
            if exp.get("assertion_status") != "asserted_none" and \
                    exp.get("unit") not in UNIT_COMPAT.get(ctype, set()):
                rejects.append((slug, ckey, f"key_unit_mismatch:{ctype}/{exp.get('unit')}", url))
                continue
            # 禁止トークン（旧作・別機種ページのガード）
            fb = forbidden_hit(slug, url)
            if fb:
                rejects.append((slug, ckey, f"forbidden_token:{fb}", url))
                continue
            if exp.get("assertion_status") == "asserted_none":
                ok, rule = verify_none_claim(name, url, quote, slug_hint=slug)
            elif exp.get("value") is None:
                rejects.append((slug, ckey, "value欠落", url))
                continue
            else:
                ok, rule = verify_asserted_claim(slug, name, ckey,
                                                 exp["value"], url, quote, i)
            if not ok:
                rejects.append((slug, ckey, rule, url))
                continue
            entries.append({
                "gold_id": f"g{len(entries)+1:03d}",
                "slug": slug, "name": name,
                "claim_key": ckey,
                "expected": exp,
                "evidence": {"url": url, "quote": quote,
                             "identity_evidence": c.get("identity_evidence", ""),
                             "verified_at": now_iso(), "verifier": rule},
                "notes": c.get("notes", ""),
            })
            print(f"✅ {slug} {ckey} {exp.get('value')}{exp.get('unit') or ''} ({rule})")

    from collections import Counter
    by_type = Counter(e["expected"].get("ceiling_type") for e in entries)
    by_key = Counter(e["claim_key"] for e in entries)

    def _reason_class(rule: str) -> str:
        for pat, label in (("C2", "同定不能(C2)"), ("C3", "quote不一致(C3)"),
                           ("C4", "値がquote外(C4)"), ("C1", "URL取得不能(C1)"),
                           ("C0", "体裁不備(C0)"), ("forbidden_token", "禁止トークン"),
                           ("key_unit_mismatch", "key/unit不整合"),
                           ("許可外ドメイン", "許可外ドメイン"), ("value欠落", "value欠落"),
                           ("取得失敗", "URL取得不能"), ("同定不可", "同定不能"),
                           ("逐語一致しない", "quote不一致"), ("短すぎる", "体裁不備")):
            if pat in rule:
                return label
        return "その他"

    by_reason = Counter(_reason_class(r[2]) for r in rejects)
    gold = {
        "frozen_at": now_iso(),
        "purpose": "Codexシャドー運用 epoch 1 の実Web正解セット（凍結後変更不可）",
        "target_epoch": "epoch1",
        "counts": {"total": len(entries), "by_ceiling_type": dict(by_type),
                   "by_claim_key": dict(by_key),
                   "machines": len({e['slug'] for e in entries}),
                   "total_candidates": total_candidates,
                   "duplicates_skipped": duplicates_skipped,
                   "rejected_by_reason": dict(by_reason),
                   "gold_collection_coverage": round(len(entries) / total_candidates, 3)
                   if total_candidates else None},
        "rejected": len(rejects),
        "entries": entries,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(gold, ensure_ascii=False, indent=1), encoding="utf-8")
    digest = hashlib.sha256(out_path.read_bytes()).hexdigest()
    print(f"\n=== 凍結完了: 候補{total_candidates}件 → 合格{len(entries)} / "
          f"不合格{len(rejects)} / 重複除外{duplicates_skipped} ===")
    print(f"型分布: {dict(by_type)}")
    print(f"不合格の理由内訳: {dict(by_reason)}")
    print(f"gold_collection_coverage: {gold['counts']['gold_collection_coverage']}")
    print(f"機種数: {gold['counts']['machines']} / SHA256: {digest}")
    if rejects:
        print("--- 不合格一覧（値・quoteはログに出さない設計＝ルール名のみ）---")
        for slug, key, rule, url in rejects:
            print(f"  ✗ {slug} {key}: {rule} {url[:60]}")
    return 0


def stats(gold_path: Path) -> int:
    g = json.loads(gold_path.read_text(encoding="utf-8"))
    print(json.dumps(g["counts"], ensure_ascii=False, indent=1))
    print("frozen_at:", g["frozen_at"], "/ SHA256:",
          hashlib.sha256(gold_path.read_bytes()).hexdigest())
    return 0


def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, cond))
        print(("✅" if cond else "❌") + " " + name)

    t("正規化: 空白・全角・チルダ吸収",
      _norm("天井  １２６８Ｇ＋α～") == _norm("天井1268G+α~"))
    t("許可外ドメインは弾く（none検証）",
      verify_none_claim("テスト機", "https://evil.example.com/x", "天井非搭載です")[0] is False)
    t("短すぎるquoteは弾く",
      verify_none_claim("テスト機", "https://1geki.jp/x", "なし")[0] is False)
    # load_candidates: ワークフロー出力のラッパー形式を吸収
    import tempfile
    d = Path(tempfile.mkdtemp())
    p = d / "c.json"
    p.write_text(json.dumps({"result": {"collected": [{"slug": "x", "candidates": []}]}},
                            ensure_ascii=False), encoding="utf-8")
    t("load_candidates: ラッパー形式を吸収", load_candidates(p)[0]["slug"] == "x")
    # freeze: 既存ファイルへの上書き拒否（凍結不変性）
    gp = d / "gold.json"
    gp.write_text("{}", encoding="utf-8")
    t("freeze: 既存gold setへの上書きを拒否", freeze(p, gp) == 1)

    ok = all(c for _, c in results)
    print(f"\nselftest: {sum(1 for _, c in results if c)}/{len(results)} 合格")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", choices=["freeze", "stats"])
    ap.add_argument("--candidates")
    ap.add_argument("--out", default=str(DOC / "gpt_research" / "gold_set_v1.json"))
    ap.add_argument("--gold")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if args.command == "freeze":
        if not args.candidates:
            ap.error("freeze には --candidates が必要")
        return freeze(Path(args.candidates), Path(args.out))
    if args.command == "stats":
        return stats(Path(args.gold or args.out))
    ap.error("freeze / stats / --selftest を指定")
    return 2


if __name__ == "__main__":
    sys.exit(main())

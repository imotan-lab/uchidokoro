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
sys.path.insert(0, str(SCRIPTS))
import verify_claims  # noqa: E402（共有URL検証＝SSRF対策の単一実装を使う・2026-07-18）
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
    ok, why = verify_claims.is_public_fetchable_url(url)  # SSRF対策（取得前検査）
    if not ok:
        raise ValueError(f"unsafe_url: {why}")
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept-Encoding": "gzip"})
    with verify_claims._SAFE_OPENER.open(req, timeout=timeout) as resp:  # リダイレクトも検査
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


def _ident_fields_of_html(html_text: str) -> dict:
    """禁止トークン照合に使うページ同定フィールド（title・H1・canonical）。
    ★本文全体は使わない＝「前作○○と比べて」等の比較記述での誤検出を防ぐ
    （2026-07-17 チャッピー条件）★"""
    m = re.search(r"<title[^>]*>([\s\S]*?)</title>", html_text, re.I)
    title = m.group(1) if m else ""
    h1s = re.findall(r"<h1[^>]*>([\s\S]*?)</h1>", html_text, re.I)
    h1 = " ".join(re.sub(r"<[^>]+>", " ", x) for x in h1s)
    m = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]*href=["\']([^"\']+)', html_text, re.I) \
        or re.search(r'<link[^>]+href=["\']([^"\']+)["\'][^>]*rel=["\']canonical["\']', html_text, re.I)
    canonical = m.group(1) if m else ""
    # casefold＝canonical URL内の英字トークン（GOLD/BLACK等）を大文字小文字無視で照合
    return {"title": _norm(title).casefold(), "h1": _norm(h1).casefold(),
            "canonical": _norm(canonical).casefold()}


def _ident_fields_of(url: str, _cache={}) -> dict:
    if url in _cache:
        return _cache[url]
    try:
        _cache[url] = _ident_fields_of_html(_fetch(url))
    except Exception:
        _cache[url] = {"title": "", "h1": "", "canonical": ""}
    return _cache[url]


def forbidden_hit(slug: str, url: str, html_text: str | None = None) -> str | None:
    """禁止トークンがページのtitle・H1・canonicalのいずれかにあればそのトークンを返す
    （旧作・別機種ページのガード。html_text指定時は再取得しない＝テスト用）"""
    ov = IDENTITY_OVERRIDES.get(slug)
    if not ov or not ov.get("forbidden"):
        return None
    fields = _ident_fields_of_html(html_text) if html_text is not None else _ident_fields_of(url)
    for tok in ov["forbidden"]:
        needle = _norm(tok).casefold()
        if any(needle in v for v in fields.values()):
            return tok
    return None


def canonical_claim_id(slug: str, claim_key: str, exp: dict) -> tuple:
    """claimを特定する属性のみでID化（URL・valueは含めない・2026-07-17チャッピー条件）。
    valueを重複キーに含めると777/778のような競合値が両方goldに入ってしまうため。
    ★null scopeは「不明」であり「別scope」ではない（2026-07-18チャッピー指摘）→
    自動で別claim扱いにせず、null-scope分割は detect_null_scope_splits で検出し
    人間の証拠確認に回す（凍結時に自動統合すると誤マージのリスクがあるため）★"""
    scope, mode = exp.get("scope"), exp.get("mode")
    return (slug, claim_key,
            _norm(str(scope)) if scope not in (None, "") else None,
            _norm(str(mode)) if mode not in (None, "") else None)


def detect_null_scope_splits(entries: list[dict]) -> list[dict]:
    """★null scope と 具体scope に分かれた同一主張候補を検出（2026-07-18チャッピー）★
    同じ (slug, claim_key, mode, value, unit) で scope が「null」と「具体」に割れている組は、
    nullが『別scope』ではなく『不明』の可能性が高い＝独立claimとして分母に入れるべきでない。
    自動統合はせず（証拠再確認が必要）、検出結果を凍結出力に載せて人間判断に回す。"""
    from collections import defaultdict
    groups: dict = defaultdict(list)
    for e in entries:
        exp = e["expected"]
        k = (e["slug"], e["claim_key"],
             _norm(str(exp.get("mode"))) if exp.get("mode") not in (None, "") else None,
             exp.get("value"), exp.get("unit"))
        groups[k].append(e)
    splits = []
    for k, es in groups.items():
        scopes = {(_norm(str(x["expected"].get("scope")))
                   if x["expected"].get("scope") not in (None, "") else None) for x in es}
        if None in scopes and len(scopes) > 1:
            splits.append({"slug": k[0], "claim_key": k[1], "value": k[3], "unit": k[4],
                           "gold_ids": [x.get("gold_id") for x in es],
                           "scopes": sorted(str(s) for s in scopes)})
    return splits


def consolidate_entries(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """同一canonical claim IDのエントリを統合/隔離する（決定論・再取得なし）。
    - 同一ID・同一(値, unit, assertion_status) → 1件へ統合し出典をevidence[]へまとめる。
      出典間で表記が割れた副属性（operator/plus_alpha）はnullへ（採点で要求しない＝安全側）
    - 同一ID・異なる値/unit/assertion_status → 全メンバーをGOLD_CONFLICTとして隔離
    - scopeが異なるCZ間/AT間等はIDが異なるので別claimとして保持される
    戻り値: (統合済みentries（gold_idは呼び出し側で再採番）, conflicts)"""
    groups: dict[tuple, list[dict]] = {}
    order: list[tuple] = []
    for e in entries:
        cid = canonical_claim_id(e["slug"], e["claim_key"], e["expected"])
        if cid not in groups:
            groups[cid] = []
            order.append(cid)
        groups[cid].append(e)

    def _evs(x):
        raw = x["evidence"]
        return raw if isinstance(raw, list) else [raw]

    merged, conflicts = [], []
    for cid in order:
        es = groups[cid]
        sig = {(x["expected"].get("value"), x["expected"].get("unit"),
                x["expected"].get("assertion_status") or "asserted") for x in es}
        if len(sig) > 1:
            conflicts.append({
                "canonical_claim_id": [x if x is not None else None for x in cid],
                "reason": "GOLD_CONFLICT: 同一claimに異なる値/unit/assertion_status",
                "members": [{"gold_id": x.get("gold_id"),
                             "value": x["expected"].get("value"),
                             "unit": x["expected"].get("unit"),
                             "assertion_status": x["expected"].get("assertion_status"),
                             "url": _evs(x)[0].get("url")} for x in es]})
            continue
        base = es[0]
        exp = dict(base["expected"])
        relaxed = []
        for attr in ("operator", "plus_alpha"):
            if len({x["expected"].get(attr) for x in es}) > 1:
                exp[attr] = None
                relaxed.append(attr)
        evs, seen_urls = [], set()
        for x in es:
            for ev in _evs(x):
                if ev.get("url") not in seen_urls:
                    seen_urls.add(ev.get("url"))
                    evs.append(ev)
        notes = " ｜ ".join(dict.fromkeys(
            n for n in ((x.get("notes") or "").strip() for x in es) if n))
        if relaxed:
            notes = (notes + " ｜ " if notes else "") + \
                f"統合時に出典間で割れた属性をnull化: {','.join(relaxed)}"
        merged.append({"slug": base["slug"], "name": base["name"],
                       "claim_key": base["claim_key"],
                       "canonical_claim_id": "|".join(str(x) for x in cid),
                       "expected": exp, "evidence": evs, "notes": notes})
    return merged, conflicts


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
    ok, why = verify_claims.validate_source_url(url, allowed=ALLOWED_DOMAINS)
    if not ok:
        return False, f"URL不許可: {why}"
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
                                "--file", str(p), "--min-domains", "1",
                                "--allowed-domains", ",".join(ALLOWED_DOMAINS)],
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
            ok_url, why_url = verify_claims.validate_source_url(
                url, allowed=ALLOWED_DOMAINS, resolve=False)  # 凍結時はネット無しでも構文/許可検査
            if not ok_url:
                rejects.append((slug, ckey, f"URL不許可:{why_url}", url))
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
                "slug": slug, "name": name,
                "claim_key": ckey,
                "expected": exp,
                "evidence": [{"url": url, "quote": quote,
                              "identity_evidence": c.get("identity_evidence", ""),
                              "verified_at": now_iso(), "verifier": rule}],
                "notes": c.get("notes", ""),
            })
            print(f"✅ {slug} {ckey} {exp.get('value')}{exp.get('unit') or ''} ({rule})")

    # ★canonical claim ID統合（同一値マージ・GOLD_CONFLICT隔離・2026-07-17）★
    passed = len(entries)
    entries, conflicts = consolidate_entries(entries)
    for i, e in enumerate(entries):
        e["gold_id"] = f"g{i+1:03d}"

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
                   "verified_passed": passed,
                   "merged_away": passed - len(entries)
                   - sum(len(c["members"]) for c in conflicts),
                   "conflict_groups": len(conflicts),
                   "conflicts_quarantined": sum(len(c["members"]) for c in conflicts),
                   "rejected_by_reason": dict(by_reason),
                   # 名称分離（2026-07-17チャッピー条件）: 候補エントリ単位とcanonical claim単位は別指標
                   "candidate_evidence_pass_rate": round(passed / total_candidates, 3)
                   if total_candidates else None,
                   "canonical_gold_coverage": round(
                       len(entries) / (len(entries) + len(conflicts)), 3)
                   if (entries or conflicts) else None},
        "rejected": len(rejects),
        "conflicts": conflicts,
        "entries": entries,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(gold, ensure_ascii=False, indent=1), encoding="utf-8")
    digest = hashlib.sha256(out_path.read_bytes()).hexdigest()
    print(f"\n=== 凍結完了: 候補{total_candidates}件 → 検証合格{passed} → 統合後{len(entries)}"
          f"（統合{gold['counts']['merged_away']}・競合隔離{gold['counts']['conflicts_quarantined']}）"
          f" / 不合格{len(rejects)} / 重複除外{duplicates_skipped} ===")
    print(f"型分布: {dict(by_type)}")
    print(f"不合格の理由内訳: {dict(by_reason)}")
    print(f"candidate_evidence_pass_rate（候補エントリ単位）: "
          f"{gold['counts']['candidate_evidence_pass_rate']}")
    print(f"canonical_gold_coverage（統合後claimグループ単位）: "
          f"{gold['counts']['canonical_gold_coverage']}")
    print(f"機種数: {gold['counts']['machines']} / SHA256: {digest}")
    if conflicts:
        print("--- GOLD_CONFLICT（要人間判断・goldに入れない）---")
        for c in conflicts:
            print(f"  ⚠ {c['canonical_claim_id']}: " +
                  " vs ".join(f"{m['value']}{m['unit'] or ''}" for m in c["members"]))
    if rejects:
        print("--- 不合格一覧（値・quoteはログに出さない設計＝ルール名のみ）---")
        for slug, key, rule, url in rejects:
            print(f"  ✗ {slug} {key}: {rule} {url[:60]}")
    return 0


def consolidate(in_path: Path, out_path: Path) -> int:
    """既存gold setにcanonical claim ID統合を適用して新版を凍結（決定論・再取得なし。
    各エントリは元の凍結時に機械検証済み＝検証結果を引き継ぐ）"""
    if out_path.exists():
        print(f"❌ {out_path} は既に存在する（gold setは凍結後変更不可。新版は別名で）")
        return 1
    g = json.loads(in_path.read_text(encoding="utf-8"))
    entries, conflicts = consolidate_entries(g["entries"])
    for i, e in enumerate(entries):
        e["gold_id"] = f"g{i+1:03d}"
    from collections import Counter
    by_type = Counter(e["expected"].get("ceiling_type") for e in entries)
    by_key = Counter(e["claim_key"] for e in entries)
    src_total = len(g["entries"])
    quarantined = sum(len(c["members"]) for c in conflicts)
    out = {
        "frozen_at": now_iso(),
        "purpose": g.get("purpose", ""),
        "target_epoch": g.get("target_epoch", "epoch1"),
        "derived_from": {
            "path": in_path.name,
            "sha256": hashlib.sha256(in_path.read_bytes()).hexdigest(),
            "rule": "canonical_claim_id統合（同一値マージ・GOLD_CONFLICT隔離・再取得なし）"},
        "counts": {"total": len(entries), "by_ceiling_type": dict(by_type),
                   "by_claim_key": dict(by_key),
                   "machines": len({e["slug"] for e in entries}),
                   "source_entries": src_total,
                   "merged_away": src_total - len(entries) - quarantined,
                   "conflict_groups": len(conflicts),
                   "conflicts_quarantined": quarantined,
                   # ★名称明確化（2026-07-18チャッピー指摘）: これは「検証済みグループ内の
                   # 非競合解決率」であり一般的なcoverageではない（凍結時不合格候補を含まない）★
                   "nonconflict_resolution_rate": round(
                       len(entries) / (len(entries) + len(conflicts)), 3)
                   if (entries or conflicts) else None,
                   # null scope分割の検出（自動統合はしない・人間の証拠確認へ）
                   "null_scope_splits": detect_null_scope_splits(entries)},
        "conflicts": conflicts,
        "entries": entries,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    digest = hashlib.sha256(out_path.read_bytes()).hexdigest()
    print(f"=== 統合凍結完了: {in_path.name} {src_total}件 → {len(entries)}件"
          f"（統合{out['counts']['merged_away']}・競合隔離{quarantined}） ===")
    print(f"型分布: {dict(by_type)} / 機種数: {out['counts']['machines']}")
    for c in conflicts:
        print(f"  ⚠ GOLD_CONFLICT {c['canonical_claim_id']}: " +
              " vs ".join(f"{m['value']}{m['unit'] or ''}" for m in c["members"]))
    print(f"SHA256: {digest}")
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

    # ★canonical claim ID統合（2026-07-17 チャッピー条件）★
    def mke(slug, value, scope=None, url="https://1geki.jp/a", operator=None,
            status="asserted", unit="G"):
        return {"gold_id": "gx", "slug": slug, "name": slug,
                "claim_key": "ceiling.normal.game",
                "expected": {"ceiling_type": "game", "scope": scope, "operator": operator,
                             "value": value, "unit": unit, "plus_alpha": None,
                             "assertion_status": status, "mode": None},
                "evidence": {"url": url, "quote": "q", "identity_evidence": "i",
                             "verified_at": "t", "verifier": "v"}, "notes": ""}

    # 同一機種・同一scopeに777と778 → GOLD_CONFLICTとして隔離（チャッピー指定テスト）
    merged, conflicts = consolidate_entries([mke("m1", 777), mke("m1", 778,
                                            url="https://nana-press.com/b")])
    t("統合: 同一claimの777/778は競合検出され隔離（goldに入らない）",
      len(merged) == 0 and len(conflicts) == 1 and len(conflicts[0]["members"]) == 2)
    # 同一ID・同一値の別URL → 1件へ統合し出典をevidence[]にまとめる
    merged, conflicts = consolidate_entries([mke("m2", 800), mke("m2", 800,
                                            url="https://nana-press.com/b")])
    t("統合: 同一値の別出典は1件にマージされevidence[]が2本",
      len(merged) == 1 and not conflicts and len(merged[0]["evidence"]) == 2)
    # scopeが異なるCZ間/AT間は別claimとして保持
    merged, conflicts = consolidate_entries([mke("m3", 800, scope="CZ間"),
                                             mke("m3", 1500, scope="AT間")])
    t("統合: scope違い（CZ間/AT間）は競合にせず別claimとして保持",
      len(merged) == 2 and not conflicts)
    # 同一値だがoperatorが割れた → マージしつつ属性をnull化（採点で要求しない）
    merged, _ = consolidate_entries([mke("m4", 800, operator="max"),
                                     mke("m4", 800, operator="exact",
                                         url="https://nana-press.com/b")])
    t("統合: 出典間で割れたoperatorはnull化して安全側マージ",
      len(merged) == 1 and merged[0]["expected"]["operator"] is None
      and "null化" in merged[0]["notes"])

    # ★禁止トークンの照合対象＝title・H1・canonical（本文は使わない）★
    html_h1 = "<html><head><title>沖ドキ！天井</title></head><body><h1>沖ドキ！GOLD 天井解析</h1></body></html>"
    t("禁止トークン: H1で検出（okidoki_encore vs GOLD）",
      forbidden_hit("okidoki_encore", "https://x/", html_text=html_h1) == "GOLD")
    html_canon = ('<html><head><title>沖ドキ！天井</title>'
                  '<link rel="canonical" href="https://example.com/okidoki-gold-tenjou/">'
                  "</head><body><h1>沖ドキ！天井</h1></body></html>")
    t("禁止トークン: canonical URLで検出（大文字小文字無視）",
      forbidden_hit("okidoki_encore", "https://x/", html_text=html_canon) == "GOLD")
    html_body = ("<html><head><title>沖ドキ！アンコール 天井</title></head>"
                 "<body><h1>沖ドキ！アンコール 天井解析</h1>"
                 "<p>前作の沖ドキ！GOLDと比べて天井が浅い</p></body></html>")
    t("禁止トークン: 本文中の前作比較では誤検出しない",
      forbidden_hit("okidoki_encore", "https://x/", html_text=html_body) is None)

    ok = all(c for _, c in results)
    print(f"\nselftest: {sum(1 for _, c in results if c)}/{len(results)} 合格")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", choices=["freeze", "stats", "consolidate"])
    ap.add_argument("--candidates")
    ap.add_argument("--gold", help="consolidate/statsの入力gold set")
    ap.add_argument("--out", default=str(DOC / "gpt_research" / "gold_set_v1.json"))
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if args.command == "freeze":
        if not args.candidates:
            ap.error("freeze には --candidates が必要")
        return freeze(Path(args.candidates), Path(args.out))
    if args.command == "consolidate":
        if not args.gold:
            ap.error("consolidate には --gold（入力gold set）が必要")
        return consolidate(Path(args.gold), Path(args.out))
    if args.command == "stats":
        return stats(Path(args.gold or args.out))
    ap.error("freeze / consolidate / stats / --selftest を指定")
    return 2


if __name__ == "__main__":
    sys.exit(main())

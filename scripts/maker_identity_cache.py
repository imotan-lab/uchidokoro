"""maker_identity_cache.py — 「この機種について、この2つのメーカー表記は同じか」の控え。

★何のためか★（2026-08-14・運営者の指示／Codexの設計）
  名鑑によって同じ機種のメーカー欄が違う。
    L転生王女 … P-WORLD「オリンピアエステート」／他2社「平和」
    L聖闘士星矢 … P-WORLD「サンスリー」／なな徹「三洋物産」
  これまでは名簿（maker-catalogs.json）に**人が足すまで**その機種が止まっていた。

★やめたこと★
  「同じグループだから全機種で同じ会社」と扱うのをやめた。
  公式は「グループ会社」と書いているだけで、
  ★全機種でメーカー名を入れ替えてよいとは書いていない★。
  1回の判断ミスが、以後すべての機種で関門を無効にしてしまう。

★いまの形★
  ①名簿で一致／正式な別名 → 使う
  ②**この機種について**前に決めた結論があれば、それに従う（この器）
  ③解決できない・関係のありそうな会社 → その場で2AIへ回す
  ④それ以外 → 使わない

★答えが出ない状態は保存しない★
  `MATCH` と `MISMATCH` だけを控える。`UNKNOWN` は「まだ決めていない」＝
  レコードが無い状態として扱う（毎回もう一度考える）。

置き場: Documents/uchidokoro/maker_identity_cache.json（リポジトリ外・公開しない）

使い方:
    python scripts/maker_identity_cache.py --list
    python scripts/maker_identity_cache.py --record --official-url <P-WORLDのURL> \\
        --expected sanslay --seen 三洋物産 --verdict MATCH \\
        --why <理由> --by claude,codex \\
        --evidence "https://…|逐語引用|directory_observation" \\
        --evidence "https://…|逐語引用|official_relationship"
    python scripts/maker_identity_cache.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import claim_identity as _ci          # noqa: E402
import local_paths as _lp             # noqa: E402

STORE = _lp.doc("maker_identity_cache.json")
SCHEMA = "maker-identity-cache/v1"
VERDICTS = ("MATCH", "MISMATCH")
MIN_QUOTE = 8                          # 逐語引用の最低の長さ
KINDS = ("directory_observation", "official_relationship")


class CacheError(Exception):
    """控えに関する異常（★迷ったら記録しない★）。"""


def _empty() -> dict:
    return {"schema_version": SCHEMA, "machines": {}}


def load() -> dict:
    """控えを読む。★壊れていたら黙って「無い」ことにしない★"""
    if not os.path.exists(STORE):
        return _empty()
    try:
        with open(STORE, encoding="utf-8") as f:
            got = json.load(f)
    except Exception as e:              # noqa: BLE001
        raise CacheError(f"控えを読めません（直すまで使いません）: {e}")
    if not isinstance(got, dict) or got.get("schema_version") != SCHEMA:
        raise CacheError(
            f"控えの版が違います（{got.get('schema_version') if isinstance(got, dict) else '?'}）")
    got.setdefault("machines", {})
    return got


def save(got: dict) -> None:
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = f"{STORE}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(got, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STORE)


def key_of(seen: str) -> str:
    """メーカー欄の表記から、比べるための芯を作る。"""
    return _ci.normalize_core(str(seen or "")).replace("株式会社", "")


def verdict_for(slug: str, expected: str, seen: str, store=None):
    """この機種について前に決めた結論（無ければ None）。

    ★完全一致で引く★＝(機種・期待する社・名鑑の表記の芯) の3つ。
    """
    if not slug or not expected or not seen:
        return None
    got = store if store is not None else load()
    k = key_of(seen)
    for rec in (got.get("machines") or {}).get(slug) or []:
        if rec.get("expected") == expected and key_of(rec.get("seen")) == k:
            return rec.get("verdict")
    return None


def remember(slug: str, expected: str, seen: str, verdict: str,
             why: str, by: list, evidence: list, decided_at: str,
             store=None) -> dict:
    """結論を控える。★根拠が無ければ受け取らない★"""
    if verdict not in VERDICTS:
        raise CacheError(f"結論は {'/'.join(VERDICTS)} のどちらかです: {verdict!r}")
    for k, v in (("slug", slug), ("expected", expected), ("seen", seen),
                 ("why", why), ("decided_at", decided_at)):
        if not str(v or "").strip():
            raise CacheError(f"「{k}」が要ります")
    if not isinstance(by, list) or len(by) < 2:
        raise CacheError("判断した者を2つ以上書きます（例: claude, codex）")
    if not isinstance(evidence, list) or not evidence:
        raise CacheError("根拠（URLと逐語引用）が要ります")
    for e in evidence:
        if not isinstance(e, dict):
            raise CacheError("根拠は組（辞書）で書きます")
        if not str(e.get("url") or "").strip():
            raise CacheError("根拠にURLが要ります")
        q = " ".join(str(e.get("quote") or "").split())
        if len(q) < MIN_QUOTE:
            raise CacheError(f"逐語引用は{MIN_QUOTE}文字以上で書きます: {q!r}")
        if e.get("kind") not in KINDS:
            raise CacheError(f"根拠の種類は {'/'.join(KINDS)} のどれかです: "
                             f"{e.get('kind')!r}")
    got = store if store is not None else load()
    rows = got.setdefault("machines", {}).setdefault(slug, [])
    k = key_of(seen)
    rec = {"expected": expected, "seen": seen, "verdict": verdict,
           "why": why, "evidence": evidence, "agreed_by": by,
           "decided_at": decided_at}
    for i, old in enumerate(rows):
        if old.get("expected") == expected and key_of(old.get("seen")) == k:
            rows[i] = rec                # ★同じ組は上書き（増やさない）★
            break
    else:
        rows.append(rec)
    if store is None:
        save(got)
    return rec


def forget(slug: str, expected: str, seen: str, store=None) -> bool:
    """控えを消す（判断を取り消すとき）。"""
    got = store if store is not None else load()
    rows = (got.get("machines") or {}).get(slug) or []
    k = key_of(seen)
    left = [r for r in rows
            if not (r.get("expected") == expected and key_of(r.get("seen")) == k)]
    if len(left) == len(rows):
        return False
    if left:
        got["machines"][slug] = left
    else:
        got["machines"].pop(slug, None)
    if store is None:
        save(got)
    return True


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅ " if cond else "❌ ") + name)

    ev = [{"url": "https://x.test/a", "quote": "メーカー 三洋物産",
           "kind": "directory_observation"},
          {"url": "https://x.test/b", "quote": "株式会社サンスリー 遊技機の開発・製造",
           "kind": "official_relationship"}]
    st = _empty()

    def _ok(**kw):
        base = dict(slug="pw_1", expected="sanslay", seen="三洋物産",
                    verdict="MATCH", why="理由", by=["claude", "codex"],
                    evidence=ev, decided_at="2026-08-14", store=st)
        base.update(kw)
        try:
            remember(**base)
            return True
        except CacheError:
            return False

    t("★★根拠つきなら控えられる★★", _ok())
    t("　控えた結論を引ける",
      verdict_for("pw_1", "sanslay", "株式会社三洋物産", st) == "MATCH")
    t("★★機種が違えば効かない★★（全機種に一律で効かせない）",
      verdict_for("pw_2", "sanslay", "三洋物産", st) is None)
    t("　期待する社が違えば効かない",
      verdict_for("pw_1", "sammy", "三洋物産", st) is None)
    t("★★答えが出ない状態は控えない★★", not _ok(verdict="UNKNOWN"))
    t("★★根拠が無ければ受け取らない★★",
      not _ok(evidence=[]) and not _ok(
          evidence=[{"url": "https://x.test/a", "quote": "短い",
                     "kind": "directory_observation"}]))
    t("　根拠の種類を勝手に作れない",
      not _ok(evidence=[{"url": "https://x.test/a", "quote": "メーカー 三洋物産",
                         "kind": "でっちあげ"}]))
    t("★★判断した者が1人だけなら受け取らない★★（2AIで決める）",
      not _ok(by=["claude"]))
    t("　同じ組を2度控えても増えない",
      (_ok(why="別の理由") and len(st["machines"]["pw_1"]) == 1))
    t("　取り消せる",
      forget("pw_1", "sanslay", "三洋物産", st)
      and verdict_for("pw_1", "sanslay", "三洋物産", st) is None)

    ng = sum(1 for _, o in results if not o)
    print()
    print("%d/%d 合格" % (len(results) - ng, len(results)))
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="機種ごとのメーカー同一性の控え")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--forget", action="store_true")
    ap.add_argument("--official-url", dest="official_url",
                    help="P-WORLDの機種ページ（slugはここから決める）")
    ap.add_argument("--expected", help="期待している社（名簿のキー）")
    ap.add_argument("--seen", help="名鑑のメーカー欄に書かれていた表記")
    ap.add_argument("--verdict", choices=VERDICTS)
    ap.add_argument("--why")
    ap.add_argument("--by", help="判断した者（カンマ区切り・2つ以上）")
    ap.add_argument("--evidence", action="append", default=[],
                    help="URL|逐語引用|種類（種類: "
                         + "/".join(KINDS) + "）")
    ap.add_argument("--at", help="決めた日（省略時は今日）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    try:
        if a.list:
            got = load()
            for slug, rows in sorted((got.get("machines") or {}).items()):
                print(f"■ {slug}")
                for r in rows:
                    print(f"   {r['expected']} ⇔ {r['seen']}  {r['verdict']}")
                    print(f"      {r.get('why', '')[:80]}")
                    print(f"      {'/'.join(r.get('agreed_by') or [])}"
                          f"（{r.get('decided_at')}）")
            return 0
        if not (a.record or a.forget):
            ap.print_help()
            return 0
        # ★slugは自己申告させない★＝P-WORLDのURLから決める
        import build_new_article as _ba
        if not a.official_url:
            print("★--official-url が要ります（slugをそこから決めます）★")
            return 1
        slug = _ba.slug_from_url(a.official_url)
        if not slug:
            print(f"★そのURLからslugを決められません: {a.official_url}★")
            return 1
        if a.forget:
            ok = forget(slug, a.expected or "", a.seen or "")
            print("消しました" if ok else "その控えはありません")
            return 0 if ok else 1
        ev = []
        for spec in a.evidence:
            parts = [x.strip() for x in str(spec).split("|")]
            if len(parts) != 3:
                print("★--evidence は『URL|逐語引用|種類』の形で書きます★")
                return 1
            ev.append({"url": parts[0], "quote": parts[1], "kind": parts[2]})
        import datetime
        rec = remember(slug, a.expected or "", a.seen or "", a.verdict or "",
                       a.why or "", [x.strip() for x in
                                     str(a.by or "").split(",") if x.strip()],
                       ev, a.at or datetime.date.today().isoformat())
        print(json.dumps({"state": "RECORDED", "slug": slug, **rec},
                         ensure_ascii=False)[:300])
        return 0
    except CacheError as e:
        print("★" + str(e) + "★")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

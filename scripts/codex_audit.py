# -*- coding: utf-8 -*-
"""codex_audit — 既存記事の誤りを第二AI（Codex）に探させ、裏取りできたものだけ自動修正する。

★役割分担（ここを取り違えない）★
  Codex        = 誤りの「発見器」。精度は高くなくてよい。候補と出典を出すだけ。
  verify_claims= 「関所」。出典URLをコードが再取得し、引用の逐語一致・機種同定・
                 値が引用の中にあることを機械確認する。ここを通らない値は絶対に書かない。
  この分離により「Codexが間違えても誤情報は公開されない」。

★自動修正の条件（すべて満たした時だけ）★
  1. サイトの構造化値とCodexの主張値が食い違う（numeric_divergence / MISMATCH）
  2. Codexの主張が★独立2ドメイン★で verify_claims exit0（＝evidence_strength=verified_policy）
  3. Codexのscopeが限定条件付きでない（AT間/CZ間等の部分天井は自動修正しない）
  4. 矛盾スキャン: 裏取りに使ったページに★旧値も同じ単位で載っていない★
     （両方載っている＝どちらが正か機械で決まらない → 触らない）
  5. apply_external_fix が構造化値と本文の両方を曖昧さなく直せる
  上記を1つでも欠けば【現状維持】し、要確認台帳（open_issues）へ登録する。

使い方:
  python scripts/codex_audit.py --run                 # 本番（既定3機種・修正上限2件）
  python scripts/codex_audit.py --run --slugs a,b     # 対象指定
  python scripts/codex_audit.py --dry-run --slugs a   # Codexは呼ぶが書き込まない
  python scripts/codex_audit.py --selftest            # 判定ロジックの内蔵テスト（通信なし）
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
BASE = Path(__file__).resolve().parent.parent
SCRIPTS = BASE / "scripts"
sys.path.insert(0, str(SCRIPTS))

import apply_external_fix          # noqa: E402（書き戻し器）
import claim_identity              # noqa: E402
import shadow_claims               # noqa: E402（claim抽出・比較器）
import shadow_codex                # noqa: E402（Codex実行・出典再取得検証）
import shadow_gold                 # noqa: E402（許可ドメイン定義）
import verify_claims               # noqa: E402（ページ取得＝矛盾スキャン用）

DOC = Path(r"C:/Users/imao_/Documents/uchidokoro")
STATE_PATH = DOC / "state.json"
LOG_DIR = DOC / "logs"
AUDIT_DIR = DOC / "codex_audit"
SEND_NOTIFY = r"C:/Users/imao_/.claude/send_notify.py"
OPEN_ISSUES = SCRIPTS / "open_issues.py"
TASK_ID = "uchidokoro-codex-audit"

DEFAULT_MACHINES = 3          # 1日の点検機種数（約40日で全120機種を1巡）
MAX_FIXES_PER_RUN = 2         # 1日の自動修正上限（verify STEP2.9と同じ保守的な上限）
MACHINE_BUDGET_SEC = 180      # 1機種3分
TASK_BUDGET_SEC = 900         # 1タスク15分（★時間切れは必ず現状維持＋台帳。公開しない★）

# 自動修正してよいscope（限定条件のない主天井のみ）。
# 「AT間」「CZ間」等の部分天井は、サイト側の構造化値が同じものを指す保証がないので対象外。
PLAIN_SCOPES = (None, "", "通常時", "not_applicable", "液晶")
# 引用文にこれらが含まれる＝限定条件つきの天井なので、主天井の置き換えには使わない
# （2026-07-21 Codex5巡目 指摘7: scope=null と申告しても引用が「AT間天井は1000G」なら別物）
CONDITIONAL_WORDS = ("AT間", "at間", "CZ間", "cz間", "ボーナス間", "BB間", "bb間",
                     "RB間", "有利区間", "AT後", "ボーナス後", "前兆中", "引き戻し")

# 矛盾スキャンで使う単位表記（旧値がページに載っていないかを見る）
UNIT_WORDS = {
    "game": ("G", "ゲーム"), "point": ("pt", "ポイント"),
    "cycle": ("周期",), "through": ("スルー", "回"),
}

EXTRA_RULES = """

追加ルール（重要）:
A. 各claimには★異なる2ドメインの出典★を evidence に入れること（chonborista.com /
   1geki.jp / nana-press.com / slopachi-quest.com のうち2つ以上）。1つしか確認できない
   claimも返してよいが、その場合は1件だけ入れる（水増し・同一ドメインの別ページで
   2件にするのは禁止）。
B. 主天井（通常時のゲーム数天井など）と、条件付きの部分天井（AT間・CZ間・ボーナス間）を
   混同しない。条件付きの場合は必ず scope にその条件を書く。
"""


LOCK_PY = SCRIPTS / "task_lock.py"


def _lock(cmd: str, *args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(LOCK_PY), cmd, *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120, creationflags=_NO_WINDOW)


def lock_acquire() -> str | None:
    """4タスク共通ロックを取得。CTXパスを返す（取れなければNone）。"""
    r = _lock("acquire", "--task", TASK_ID)
    ctx = next((ln[4:] for ln in (r.stdout or "").splitlines() if ln.startswith("CTX=")), None)
    if not ctx:
        log(f"ロック取得できず: {(r.stdout or '').strip()[:200]}")
    return ctx


def heartbeat(ctx: str, note: str = "") -> None:
    if ctx:
        _lock("heartbeat", "--ctx", ctx)
        if note:
            log(f"  ・{note}")


def fencing_ok(ctx: str) -> bool:
    """★書き込み（commit/push/state）の直前に必ず呼ぶ。世代交代したゾンビ実行を止める★"""
    if not ctx:
        return False
    r = _lock("check", "--ctx", ctx)
    return "OWNER_OK" in (r.stdout or "")


def now() -> datetime.datetime:
    return datetime.datetime.now()


def iso(dt=None) -> str:
    return (dt or now()).strftime("%Y-%m-%dT%H:%M:%S")


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{now():%H:%M:%S}] {msg}"
    with open(LOG_DIR / f"codex_audit_{now():%Y-%m-%d}.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def load_json(p: Path, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json_atomic(p: Path, obj) -> None:
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


# ─────────────────────────────────────────────
# 対象選定（評価日が最も古い順＝日付ベース。位置番号方式は使わない）
# ─────────────────────────────────────────────

def select_slugs(machines: list[dict], state: dict, n: int) -> list[str]:
    seen = (state.get("codex_audit") or {}).get("last_audited") or {}
    cands = [m for m in machines if (m.get("status") or "complete") == "complete"]
    cands.sort(key=lambda m: (seen.get(m["slug"], ""), m["slug"]))
    return [m["slug"] for m in cands[:n]]


# ─────────────────────────────────────────────
# 判定（純関数・通信なし＝selftestで検証できる）
# ─────────────────────────────────────────────

def classify(site_claims: list[dict], codex_claims: list[dict],
             comparison: list[dict], auto_fix_allowed: bool = True) -> dict:
    """比較結果を「修正候補 / 要確認 / 変更なし」に仕分ける。書き込みはしない。

    auto_fix_allowed=False の機種（＝スマスロ機でない機種）は、同名の旧世代機と
    ページを区別する材料がタイトルに無いため【自動修正しない】（要確認へ回す）。
    2026-07-21 Codex3巡目 指摘7・8: 4号機/5号機/6号機や増台版は表記だけでは判別不能。
    """
    by_key = {}
    for c in codex_claims:
        key = shadow_gold.migrate_claim_key(c.get("claim_key") or "",
                                            c.get("ceiling_type") or "")
        by_key.setdefault(key, []).append(c)
    site_by_key = {s["claim_key"]: s for s in site_claims}

    out = {"fix_candidates": [], "reviews": [], "unchanged": []}
    for r in comparison:
        key = r["claim_key"]
        verdict, sub = r["verdict"], r.get("sub")
        site = site_by_key.get(key) or {}
        cands = by_key.get(key) or []
        codex = cands[0] if len(cands) == 1 else None

        diverged = (verdict == "MISMATCH") or (verdict == "UNKNOWN" and sub == "numeric_divergence")
        if not diverged:
            if verdict in ("MATCH",) or sub == "numeric_alignment":
                out["unchanged"].append({"claim_key": key, "detail": r["detail"]})
            elif verdict in ("MISSING_IN_CODEX", "MISSING_IN_SITE"):
                # 構造そのものの食い違い（例: サイトはG数天井・Codexは周期天井）は
                # 値の差し替えでは直らない＝人が構造を判断する必要がある
                out["reviews"].append({"claim_key": key, "kind": "structure",
                                       "detail": r["detail"], "auto_fixable": False})
            continue

        why = None
        ev_ok = [e for e in (codex or {}).get("evidence_results") or [] if e.get("verified")]
        ev_domains = sorted({shadow_codex._etld1(e.get("source_url") or "")
                             for e in ev_ok} - {""})
        quotes = " / ".join((e.get("raw_quote") or "")
                            for e in ((codex or {}).get("evidence") or []))
        if not auto_fix_allowed:
            why = ("スマスロ機ではないため自動修正の対象外"
                   "（同名の旧世代機とページを機械的に区別できない）")
        elif codex is None:
            why = "同じ項目にCodexの主張が複数あり一意に決まらない" if cands else "Codex主張が取れない"
        # ★申告を信用せず、実際の検証結果から数え直す（2026-07-21 Codex5巡目 指摘8）★
        elif codex.get("assertion_status") != "asserted":
            why = f"Codexの主張が確定でない（assertion_status={codex.get('assertion_status')}）"
        elif len(ev_domains) < 2:
            why = (f"裏取りが独立2ドメインに届かない"
                   f"（検証成功ドメイン={ev_domains} / 申告={codex.get('evidence_strength')}）")
        elif codex.get("evidence_strength") != "verified_policy":
            why = f"出典強度の申告が不足（{codex.get('evidence_strength')}）"
        elif codex.get("scope") not in PLAIN_SCOPES:
            why = f"条件付きの天井（scope={codex.get('scope')}）＝サイトの値と同じものか決まらない"
        # ★引用文に限定条件が書かれていれば主天井の置き換えに使わない（指摘7）★
        elif any(w in quotes for w in CONDITIONAL_WORDS):
            why = (f"出典の引用に限定条件（{[w for w in CONDITIONAL_WORDS if w in quotes][:2]}）"
                   f"が含まれる＝主天井と同じものか決まらない")
        elif site.get("value") is None or codex.get("value") is None:
            why = "値が欠けている"
        elif shadow_claims._norm_unit(site.get("unit")) != shadow_claims._norm_unit(codex.get("unit")):
            why = f"単位が違う（site={site.get('unit')} / codex={codex.get('unit')}）"
        # ★天井の種類が一致していること（指摘9: 単位すり替えでの誤修正を防ぐ）★
        elif (site.get("ceiling_type") or "") != (codex.get("ceiling_type") or ""):
            why = (f"天井の種類が違う（site={site.get('ceiling_type')} / "
                   f"codex={codex.get('ceiling_type')}）")
        # ★scopeについて★: サイト側の構造化データに scope は無い（machines.json の
        #   limit は定義上「主天井」）。したがって scope 未構造化そのものは修正を止める
        #   理由にしない。代わりに「Codexが限定条件を名乗っていないこと（PLAIN_SCOPES）」と
        #   「引用文に限定条件語が無いこと」で守る（上の2条件）。
        #   ただしCodexが★既知の語彙に無いscope★を出した場合は意味が確定しないので止める。
        elif "scope_unverified" in (r.get("attrs_unverified") or []):
            why = f"Codexのscopeが未知の語（{codex.get('scope')}）＝同じ項目と確定できない"

        rec = {"claim_key": key, "site_value": site.get("value"),
               "codex_value": (codex or {}).get("value"),
               "unit": (codex or {}).get("unit") or site.get("unit"),
               "ceiling_type": (codex or {}).get("ceiling_type") or site.get("ceiling_type"),
               "site_ceiling_type": site.get("ceiling_type"),
               "evidence": [e.get("source_url") for e in ((codex or {}).get("evidence") or [])],
               "verified_domains": (codex or {}).get("verified_domains") or [],
               "detail": r["detail"]}
        if why:
            rec.update(kind="value", auto_fixable=False, reason=why)
            out["reviews"].append(rec)
        else:
            rec.update(kind="value", auto_fixable=True)
            out["fix_candidates"].append(rec)
    return out


# ─────────────────────────────────────────────
# 矛盾スキャン（旧値も同じページに載っていないか）
# ─────────────────────────────────────────────

def contradiction_scan(cand: dict, allowed) -> str | None:
    """裏取りに使ったページに旧値も同単位で載っていれば理由文字列を返す（＝修正しない）。"""
    old = cand.get("site_value")
    if old is None:
        return "旧値が無い"
    # ★サイト側の天井種別で単位を決める（Codexの申告でのすり替えを防ぐ・指摘9）★
    units = UNIT_WORDS.get(cand.get("site_ceiling_type") or cand.get("ceiling_type") or "", ())
    if not units:
        return f"単位が特定できない（ceiling_type={cand.get('ceiling_type')}）"
    olds = apply_external_fix._num_variants(old)
    for url in cand.get("evidence") or []:
        page = verify_claims.fetch_page(url, allowed=allowed)
        if page is None:
            return f"矛盾スキャンでページを取得できない（{url}）"
        text = verify_claims.normalize(page.text)
        for o in olds:
            for u in units:
                if f"{o}{u}" in text:
                    return (f"裏取りページに旧値「{o}{u}」も載っている（{url}）"
                            f"＝どちらが正しいか機械で決められない")
    return None


# ─────────────────────────────────────────────
# 台帳・通知
# ─────────────────────────────────────────────

def add_issue(slug: str, kind: str, title: str, detail: str) -> None:
    """要確認台帳へ登録。★無人タスクはcloseしない（登録のみ）★"""
    try:
        r = subprocess.run([sys.executable, str(OPEN_ISSUES), "add",
                            "--source", "codex-audit", "--slug", slug,
                            "--kind", kind, "--title", title, "--detail", detail],
                           capture_output=True, text=True, timeout=60,
                           creationflags=_NO_WINDOW)
        if r.returncode != 0:
            log(f"台帳登録が失敗（処理は継続）: {(r.stderr or r.stdout or '')[:200]}")
    except Exception as e:
        log(f"台帳登録に失敗（処理は継続）: {e}")


def notify(subject: str, body: str) -> None:
    try:
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        bf = AUDIT_DIR / "notify_body.txt"
        bf.write_text(body, encoding="utf-8")
        subprocess.run([sys.executable, SEND_NOTIFY, "notify",
                        "--subject", subject, "--body-file", str(bf)],
                       capture_output=True, text=True, timeout=120,
                       creationflags=_NO_WINDOW)
    except Exception as e:
        log(f"メール送信失敗（処理は継続）: {e}")


# ─────────────────────────────────────────────
# 1機種の点検
# ─────────────────────────────────────────────

def audit_machine(machine: dict, machines: list[dict], run_id: str,
                  deadline: datetime.datetime) -> dict:
    slug = machine["slug"]
    res = {"slug": slug, "error": None, "classified": None, "fixes": [], "reviews": []}
    m_deadline = min(deadline, now() + datetime.timedelta(seconds=MACHINE_BUDGET_SEC))
    meta = shadow_codex.run_codex(machine, run_id, m_deadline,
                                  prompt=shadow_codex.build_prompt(machine, EXTRA_RULES))
    if meta.get("error"):
        res["error"] = meta["error"]
        return res
    claims = (meta["output"] or {}).get("claims") or []
    for c in claims:
        c["evidence_results"] = shadow_codex.verify_evidence(machine, c, run_id)
        oks = sorted({shadow_codex._etld1(er["source_url"])
                      for er in c["evidence_results"] if er.get("verified")} - {""})
        c["verified_domains"] = oks
        if c.get("assertion_status") == "asserted":
            c["evidence_strength"] = ("cannot_verify" if not oks else
                                      "verified_single" if len(oks) == 1 else "verified_policy")
            if not oks:
                c["assertion_status"] = "cannot_verify"
    site_claims = shadow_claims.extract_site_claims(machine)
    comparison = shadow_claims.compare_claims(
        site_claims, claims,
        codex_key=lambda c: shadow_gold.migrate_claim_key(
            c.get("claim_key") or "", c.get("ceiling_type") or ""))
    res["classified"] = classify(site_claims, claims, comparison,
                                 auto_fix_allowed=("smart" in claim_identity.machine_tags(machine)))
    res["site_claims"] = site_claims
    res["codex_claims"] = claims
    res["comparison"] = comparison
    return res


# ─────────────────────────────────────────────
# 修正の適用（1件ずつ・全部通ってから書く）
# ─────────────────────────────────────────────

def apply_one(slug: str, cand: dict, allowed, apply_mode: bool) -> dict:
    field = cand["claim_key"]
    why = contradiction_scan(cand, allowed)
    if why:
        return {"applied": False, "reason": why, "field": field, **_cand_view(cand)}
    r = apply_external_fix.run(BASE, slug, field, float(cand["site_value"]),
                               float(cand["codex_value"]), apply_mode)
    return {"applied": r["applied"], "reason": r["reason"], "field": field,
            "struct_path": r["struct_path"], "prose_edits": r["prose_edits"],
            **_cand_view(cand)}


def _sh(cmd: list[str], timeout=600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          creationflags=_NO_WINDOW)


def publish(fixes: list[dict], ctx: str) -> tuple[bool, str]:
    """修正後の再ビルド→検査→コミット→push。★検査が通らなければコミットしない★

    戻り値 (公開したか, 説明)。失敗時は作業ツリーを元に戻す（中途半端に公開しない）。
    """
    slugs = sorted({f["slug"] for f in fixes})
    steps = [
        (["python", "scripts/build_machine_pages.py"], "記事ページ再生成"),
        (["python", "scripts/build_hub_pages.py"], "ハブ4ページ再生成"),
        (["python", "scripts/validate_machine_data.py"], "数値整合チェック"),
        (["python", "scripts/audit_site.py"], "サイト構造監査"),
    ]
    for cmd, label in steps:
        r = _sh(cmd)
        if r.returncode != 0:
            tail = ((r.stdout or "") + (r.stderr or ""))[-800:]
            _sh(["git", "checkout", "--", "."])
            return False, f"{label}が不合格（rc={r.returncode}）→修正を取り消した:\n{tail}"
        log(f"  ・{label} OK")
        heartbeat(ctx)

    # コミット対象は明示パスのみ（add -A 禁止）
    paths = ["assets/data/machines.json", "sitemap.xml", "service-worker.js",
             "guide-tenjo-ranking.html", "guide-reset-ranking.html",
             "guide-suru-tenjo.html", "guide-ichiran.html"]
    paths += [f"assets/data/machine-details/{s}.json" for s in slugs]
    paths += [f"machines/{s}/index.html" for s in slugs]
    exist = [p for p in paths if (BASE / p).exists()]
    _sh(["git", "add", "--"] + exist)
    st = _sh(["git", "status", "--porcelain"])
    unstaged = [ln for ln in (st.stdout or "").splitlines()
                if ln[:2] not in ("M ", "A ", "D ") and ln.strip()]
    if unstaged:
        log(f"  ⚠ 対象外の変更を検知（台帳へ）: {unstaged[:5]}")
        add_issue("site", "other", "[codex-audit] コミット対象外の変更を検知",
                  "\n".join(unstaged)[:1000])

    lines = [f"- {f['slug']} {f['field']}: {f['old']} → {f['new']}"
             f"（出典 {', '.join(f.get('domains') or [])}）" for f in fixes]
    msg = ("fix(codex-audit): 裏取り済みの外部数値を自動修正\n\n" + "\n".join(lines) +
           "\n\n出典は verify_claims.py の関所を独立2ドメインで通過したもののみ。\n\n"
           "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>")
    if not fencing_ok(ctx):
        _sh(["git", "reset"])
        return False, "ロックの世代が変わっている（別実行に交代済み）→書き込みを中止した"
    r = _sh(["git", "commit", "-m", msg])
    if r.returncode != 0:
        return False, f"コミット失敗: {((r.stdout or '') + (r.stderr or ''))[-400:]}"
    r = _sh(["git", "push"], timeout=300)
    if r.returncode != 0:
        return False, f"push失敗（コミットは済み・次回再送）: {((r.stderr or ''))[-400:]}"
    return True, f"{len(fixes)}件を公開（{', '.join(slugs)}）"


def _cand_view(cand: dict) -> dict:
    return {"old": cand.get("site_value"), "new": cand.get("codex_value"),
            "unit": cand.get("unit"), "sources": cand.get("evidence"),
            "domains": cand.get("verified_domains")}


# ─────────────────────────────────────────────
# selftest（通信なし）
# ─────────────────────────────────────────────

def selftest() -> int:
    ok = fail = 0

    def eq(got, want, label):
        nonlocal ok, fail
        if got == want:
            ok += 1
        else:
            fail += 1
            print(f"  NG {label}: got={got!r} want={want!r}")

    def C(key, value, strength="verified_policy", scope=None, unit="G",
          ctype="game", ev=2, status="asserted", quote="天井は{v}Gで直撃"):
        doms = ["a.com", "b.com"][:ev]
        return {"claim_key": key, "ceiling_type": ctype, "value": value, "unit": unit,
                "scope": scope, "assertion_status": status,
                "evidence_strength": strength,
                "verified_domains": doms,
                "evidence": [{"source_url": f"https://{d}/x",
                              "raw_quote": quote.format(v=value)} for d in doms],
                "evidence_results": [{"source_url": f"https://{d}/x", "verified": True}
                                     for d in doms]}

    def S(key, value, unit="G", ctype="game"):
        return {"claim_key": key, "ceiling_type": ctype, "value": value, "unit": unit,
                "scope": None, "operator": None, "plus_alpha": None,
                "assertion_status": "asserted"}

    def R(key, verdict, sub=None, detail=""):
        return {"claim_key": key, "verdict": verdict, "sub": sub, "detail": detail,
                "attrs_unverified": []}

    K = "ceiling.normal.game"
    # 1. 2ドメイン裏取り＋値が違う → 修正候補
    c = classify([S(K, 900)], [C(K, 1000)], [R(K, "UNKNOWN", "numeric_divergence")])
    eq(len(c["fix_candidates"]), 1, "修正候補になる")
    eq(c["fix_candidates"][0]["old"] if False else c["fix_candidates"][0]["site_value"], 900, "旧値")
    eq(c["fix_candidates"][0]["codex_value"], 1000, "新値")
    # 2. 1ドメインしか裏取りできない → 要確認
    c = classify([S(K, 900)], [C(K, 1000, strength="verified_single", ev=1)],
                 [R(K, "UNKNOWN", "numeric_divergence")])
    eq(len(c["fix_candidates"]), 0, "1ドメインは修正しない")
    eq("2ドメイン" in c["reviews"][0]["reason"], True, "1ドメインの理由")
    # 3. 裏取りできていない → 要確認
    c = classify([S(K, 900)], [C(K, 1000, strength="cannot_verify", ev=0)],
                 [R(K, "UNKNOWN", "numeric_divergence")])
    eq(len(c["fix_candidates"]), 0, "未検証は修正しない")
    # 4. 条件付き天井（AT間）→ 要確認
    c = classify([S(K, 900)], [C(K, 1000, scope="AT間")],
                 [R(K, "UNKNOWN", "numeric_divergence")])
    eq(len(c["fix_candidates"]), 0, "条件付き天井は修正しない")
    eq("条件付き" in c["reviews"][0]["reason"], True, "条件付きの理由")
    # 5. 単位違い → 修正しない
    c = classify([S(K, 900)], [C(K, 1000, unit="pt")], [R(K, "MISMATCH")])
    eq(len(c["fix_candidates"]), 0, "単位違いは修正しない")
    # 6. 値一致 → 変更なし
    c = classify([S(K, 900)], [C(K, 900)], [R(K, "UNKNOWN", "numeric_alignment")])
    eq(len(c["fix_candidates"]), 0, "一致は修正しない")
    eq(len(c["unchanged"]), 1, "一致は変更なしに入る")
    # 7. 同じ項目にCodex主張が2つ → 一意に決まらないので修正しない
    c = classify([S(K, 900)], [C(K, 1000), C(K, 1200)],
                 [R(K, "UNKNOWN", "numeric_divergence")])
    eq(len(c["fix_candidates"]), 0, "複数主張は修正しない")
    eq("一意に決まらない" in c["reviews"][0]["reason"], True, "複数主張の理由")
    # 8. 構造の食い違い → 要確認（自動修正しない）
    c = classify([S(K, 900)], [], [R(K, "MISSING_IN_CODEX")])
    eq(c["reviews"][0]["kind"], "structure", "構造の食い違いは要確認")
    eq(c["reviews"][0]["auto_fixable"], False, "構造は自動修正しない")
    # 9. MISMATCH（属性まで検証済み）も修正候補になる
    c = classify([S(K, 900)], [C(K, 1000)], [R(K, "MISMATCH")])
    eq(len(c["fix_candidates"]), 1, "MISMATCHも修正候補")

    # 9.1 引用に限定条件（AT間）が入っていれば主天井の置き換えに使わない
    c = classify([S(K, 900)], [C(K, 1000, quote="AT間天井は{v}G")],
                 [R(K, "UNKNOWN", "numeric_divergence")])
    eq(len(c["fix_candidates"]), 0, "AT間の引用は修正しない")
    eq("限定条件" in c["reviews"][0]["reason"], True, "AT間の理由")
    # 9.2 検証済みドメインが実は1件（申告だけverified_policy）→ 修正しない
    fake = C(K, 1000)
    fake["evidence_results"] = [{"source_url": "https://a.com/x", "verified": True},
                                {"source_url": "https://b.com/x", "verified": False}]
    c = classify([S(K, 900)], [fake], [R(K, "UNKNOWN", "numeric_divergence")])
    eq(len(c["fix_candidates"]), 0, "申告だけの2ドメインは信用しない")
    # 9.3 assertion_status が asserted でない → 修正しない
    c = classify([S(K, 900)], [C(K, 1000, status="cannot_verify")],
                 [R(K, "UNKNOWN", "numeric_divergence")])
    eq(len(c["fix_candidates"]), 0, "cannot_verifyは修正しない")
    # 9.4 天井の種類が違う（G数天井 vs スルー天井）→ 修正しない
    c = classify([S(K, 900)], [C(K, 1000, ctype="through")],
                 [R(K, "UNKNOWN", "numeric_divergence")])
    eq(len(c["fix_candidates"]), 0, "天井の種類違いは修正しない")
    # 9.45 Codexのscopeが未知語→修正しない
    r_unknown = R(K, "UNKNOWN", "numeric_divergence")
    r_unknown["attrs_unverified"] = ["scope_unverified"]
    c = classify([S(K, 900)], [C(K, 1000, scope="謎の区間")], [r_unknown])
    eq(len(c["fix_candidates"]), 0, "未知のscopeは修正しない")

    # 9.5 スマスロ機でなければ自動修正しない（同名旧機種と区別できないため）
    c = classify([S(K, 900)], [C(K, 1000)], [R(K, "UNKNOWN", "numeric_divergence")],
                 auto_fix_allowed=False)
    eq(len(c["fix_candidates"]), 0, "非スマスロ機は自動修正しない")
    eq("スマスロ機ではない" in c["reviews"][0]["reason"], True, "非スマスロ機の理由")

    # 10. 選定は評価日が古い順・preview機種は対象外
    ms = [{"slug": "a"}, {"slug": "b"}, {"slug": "c", "status": "preview"}]
    st = {"codex_audit": {"last_audited": {"a": "2026-07-20", "b": "2026-07-01"}}}
    eq(select_slugs(ms, st, 3), ["b", "a"], "古い順・previewは除外")
    eq(select_slugs(ms, {}, 1), ["a"], "未点検が最優先")

    print(f"codex_audit selftest: {ok}/{ok + fail}")
    return 0 if fail == 0 else 1


# ─────────────────────────────────────────────
# 本体
# ─────────────────────────────────────────────

def run(slugs_override, n_machines: int, apply_mode: bool) -> int:
    started = now()
    deadline = started + datetime.timedelta(seconds=TASK_BUDGET_SEC)
    run_id = uuid.uuid4().hex[:8]
    log(f"=== codex_audit 開始 run_id={run_id} 期限={deadline:%H:%M} "
        f"モード={'本番' if apply_mode else 'dry-run'} ===")
    ctx = lock_acquire() if apply_mode else None
    if apply_mode and not ctx:
        log("=== codex_audit 中止（ロックが取れない＝他タスク実行中） STATUS=SKIPPED_LOCKED ===")
        write_marker("SKIPPED_LOCKED", run_id, started, now(), [], 0, 0)
        return 0
    machines = json.loads((BASE / "assets" / "data" / "machines.json").read_text(encoding="utf-8"))
    by = {m["slug"]: m for m in machines}
    state = load_json(STATE_PATH, {}) or {}
    slugs = slugs_override or select_slugs(machines, state, n_machines)
    log(f"対象: {slugs}")

    allowed = list(shadow_gold.ALLOWED_DOMAINS)
    fixes_done, all_reviews, all_fixes, errors = 0, [], [], []
    timeout_hit = False
    for slug in slugs:
        if now() >= deadline:
            timeout_hit = True
            log(f"⏰ 時間切れ: {slug} 以降は実施しない（現状維持）")
            break
        m = by.get(slug)
        if not m:
            log(f"⚠ {slug}: machines.json に無い")
            continue
        log(f"── {slug} 点検開始")
        heartbeat(ctx, f"{slug} 点検開始")
        r = audit_machine(m, machines, run_id, deadline)
        heartbeat(ctx, f"{slug} Codex応答受領")
        if r["error"]:
            errors.append((slug, r["error"]))
            log(f"── {slug} エラー: {r['error']}")
            continue
        cls = r["classified"]
        log(f"── {slug} 一致{len(cls['unchanged'])}件 / 修正候補{len(cls['fix_candidates'])}件 "
            f"/ 要確認{len(cls['reviews'])}件")

        for cand in cls["fix_candidates"]:
            if fixes_done >= MAX_FIXES_PER_RUN:
                cls["reviews"].append({**cand, "kind": "value", "auto_fixable": False,
                                       "reason": f"本日の自動修正上限（{MAX_FIXES_PER_RUN}件）に到達"})
                continue
            fr = apply_one(slug, cand, allowed, apply_mode)
            fr["slug"] = slug
            if fr["applied"]:
                fixes_done += 1
                all_fixes.append(fr)
                log(f"   ✅ 修正: {slug} {fr['field']} {fr['old']}→{fr['new']} "
                    f"（{fr['struct_path']} ＋本文{len(fr['prose_edits'])}箇所）")
            else:
                log(f"   ⏸ 修正見送り: {slug} {fr['field']} 理由={fr['reason']}")
                cls["reviews"].append({**cand, "kind": "value", "auto_fixable": False,
                                       "reason": fr["reason"]})

        for rv in cls["reviews"]:
            rv["slug"] = slug
            all_reviews.append(rv)

        # 状態更新（1機種終わるごと＝途中で落ちても進捗が残る）
        state.setdefault("codex_audit", {}).setdefault("last_audited", {})[slug] = \
            f"{now():%Y-%m-%d}"
        if apply_mode and fencing_ok(ctx):
            save_json_atomic(STATE_PATH, state)
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        save_json_atomic(AUDIT_DIR / f"{now():%Y%m%d%H%M%S}_{slug}_{run_id}.json", r)

    # 修正を公開（再ビルド→検査→コミット→push）。検査が通らなければ取り消す
    published, pub_note = False, "修正なし"
    if all_fixes and apply_mode:
        published, pub_note = publish(all_fixes, ctx)
        log(f"公開: {'✅' if published else '❌'} {pub_note}")
        if not published:
            add_issue(all_fixes[0]["slug"], "other",
                      "[codex-audit] 自動修正の公開に失敗", pub_note[:1500])

    # 要確認は台帳へ（自動修正できなかったもの全て）★無人タスクはcloseしない★
    if apply_mode:
        for rv in all_reviews:
            kind = "structural" if rv.get("kind") == "structure" else "external_value"
            title = (f"{rv.get('claim_key')} 要確認: "
                     f"サイト{rv.get('site_value')} / Codex{rv.get('codex_value')}"
                     if rv.get("kind") == "value" else
                     f"{rv.get('claim_key')} の構造が食い違う")
            add_issue(rv["slug"], kind, title[:120],
                      json.dumps(rv, ensure_ascii=False)[:1500])

    summary = (f"修正{len(all_fixes)}件 / 要確認{len(all_reviews)}件 / エラー{len(errors)}件"
               + (" / 時間切れあり" if timeout_hit else ""))
    log(f"=== codex_audit 完了 {summary} ===")

    if apply_mode:
        status = ("PARTIAL" if (errors or timeout_hit or (all_fixes and not published))
                  else ("COMPLETED" if slugs else "COMPLETED_NO_CHANGE"))
        write_marker(status, run_id, started, now(), slugs, len(slugs) - len(errors),
                     len(errors), note=pub_note)
        # ★「静かなのが正常」＝修正した/台帳に載せた/異常が出た時だけメール★
        if all_fixes or all_reviews or errors or timeout_hit:
            icon = "🔴" if errors or (all_fixes and not published) else (
                "🟡" if all_reviews or timeout_hit else "🟢")
            body = [f"対象: {', '.join(slugs)}", summary, f"公開: {pub_note}", ""]
            for f in all_fixes:
                body.append(f"✅ 修正 {f['slug']} {f['field']}: {f['old']} → {f['new']}"
                            f"（本文{len(f['prose_edits'])}箇所・出典 {', '.join(f.get('domains') or [])}）")
            for rv in all_reviews[:20]:
                body.append(f"⏸ 要確認 {rv['slug']} {rv.get('claim_key')}: "
                            f"{rv.get('reason') or rv.get('detail')}")
            for s, e in errors:
                body.append(f"❌ エラー {s}: {e}")
            notify(f"{icon} codex-audit: {summary}", "\n".join(body))
        if ctx:
            _lock("release", "--ctx", ctx)
    return 0 if not errors else 1


COMPLETION_MARKER = DOC / "codex_audit" / "codex_audit_last_run.json"


def write_marker(status: str, run_id: str, started, ended, selected,
                 success: int, errors: int, note: str = "") -> None:
    """完走マーカー（task-watchdogが「起動したが黙って死んだ」を検知するための足跡）"""
    save_json_atomic(COMPLETION_MARKER, {
        "status": status, "run_id": run_id, "started_at": iso(started),
        "ended_at": iso(ended), "target_count": len(selected),
        "success_count": success, "error_count": errors,
        "selected": list(selected), "note": note[:300]})


def main() -> int:
    ap = argparse.ArgumentParser(description="既存記事の誤りをCodex＋関所で自動修正")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--slugs")
    ap.add_argument("--max-machines", type=int, default=DEFAULT_MACHINES)
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    slugs = a.slugs.split(",") if a.slugs else None
    if a.dry_run:
        return run(slugs, a.max_machines, apply_mode=False)
    if a.run:
        return run(slugs, a.max_machines, apply_mode=True)
    ap.error("--run / --dry-run / --selftest のいずれかを指定")
    return 2


if __name__ == "__main__":
    sys.exit(main())

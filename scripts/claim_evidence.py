#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""claim_evidence.py — 出典の証拠を、台帳とは別の場所に置いて指紋で結ぶ

★なぜ要るか（Codex 8巡目・閉鎖条件③④）★
  これまで証拠（ページ見出し・表の行・型式）は**台帳の中**に書かれていた。
  台帳は claim を書く側が自由に編集できるので、
  「別ページのURLに、対象機種の見出しと表の行を書き込む」だけで通ってしまう。
  ＝**証拠の真正性が、証拠を書いた本人の申告に依存している**状態。

★どう変えるか★
  証拠を台帳から切り離し、**取得器だけが書く別ディレクトリ**へ置く。
  台帳は「その証拠の指紋（sha256）」しか持たない。

      台帳 claim.sources[i].evidence_ref = "<sha256>"
                       ↓ 指紋で引く
      assets/data/claim-evidence/<sha256>.json   ← 取得器が作る（人もAIも手で書かない）

  公開ゲートは①指紋でファイルを引き②中身から指紋を計算し直し③一致を確かめる。
  中身を1文字でも変えれば指紋が変わるので、**後から書き換えたら必ず落ちる**。

★Phase 1 の範囲★
  実際にページを取りに行くのは Phase 2。ここで用意するのは
  **スキーマと信頼境界と検証**（＝証拠が無い／指紋が合わない claim を公開させない）。

使い方:
    python scripts/claim_evidence.py --selftest
    python scripts/claim_evidence.py --list
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

DATA = os.path.join(BASE, "assets", "data")
EVIDENCE_DIR = os.path.join(DATA, "claim-evidence")

SCHEMA_VERSION = "claim-evidence/v1"
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# 証拠として受け取ってよい単位（claim_c5 と同じ定義を参照する）
UNIT_TYPES = ("TABLE_ROW", "TABLE_CELL", "LIST_ITEM", "PARAGRAPH",
              "HEADING", "DEFINITION_ITEM")

# ★★証拠がどこまで確かめられているかの段階★★（Codex 9巡目 (a)-3）
#   「証拠ファイルがある」＝「取ってきた本物」ではない。
#   今のPhase 1 は**取得を実行していない**ので、書けるのは1段目だけ。
#   票に数えてよいのは最終段だけにする（＝今は何も数えられない＝正しい状態）。
ATTESTATION_STATES = (
    "UNATTESTED_METADATA",   # 形は整っているが、取得も検算もしていない
    "FETCH_ATTESTED",        # 応答バイトを保存し、response_sha256 を検算済み
    "EXTRACTION_VERIFIED",   # 保存した応答から証拠単位を再抽出して一致を確認済み
    "CLAIM_VERIFIED",        # 型式・値まで出所に結び付いた（票に数えてよい）
)
# ★票に数えてよい段階★（これ未満は必ず落とす）
COUNTABLE_STATES = ("CLAIM_VERIFIED",)


class EvidenceError(Exception):
    pass


def _valid_ts(s) -> bool:
    """★形式だけでなく実在する日時か★（共通の日時検査）"""
    import datetime as _dt
    if not _TS_RE.match(str(s or "")):
        return False
    try:
        _dt.datetime.strptime(str(s), "%Y-%m-%dT%H:%M:%SZ")
        return True
    except ValueError:
        return False


def canonical_sha256(obj) -> str:
    """指紋の計算方法。★台帳側と同じ規則★（キー順を固定して空白を入れない）"""
    return hashlib.sha256(
        json.dumps(obj, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()


def content_sha256(ev: dict) -> str:
    """証拠の指紋。★自分の指紋の欄は計算に入れない★（自己参照になるため）"""
    body = {k: v for k, v in (ev or {}).items() if k != "evidence_sha256"}
    return canonical_sha256(body)


def evidence_violations(ev, where: str = "evidence") -> list:
    """証拠の形を検査し、**違反を全部**返す（Codex 9巡目 (b)-2）。

    1件目で止めると「status偽装＋DOM位置欠落＋型式欠落」が同時にあっても
    1つしか見えず、直すたびに次が出てくる。
    """
    v: list = []
    if not isinstance(ev, dict):
        return [f"{where}: 辞書でない"]
    if ev.get("schema_version") != SCHEMA_VERSION:
        v.append(f"{where}: schema_version が {SCHEMA_VERSION} でない")
    for k in ("fetch", "page", "evidence_unit", "machine_identity",
              "fetcher_version", "attestation_state", "evidence_sha256"):
        if k not in ev:
            v.append(f"{where}.{k}: 必須")

    # --- ★証拠がどこまで確かめられているか★
    st = ev.get("attestation_state")
    if st not in ATTESTATION_STATES:
        v.append(f"{where}.attestation_state: {ATTESTATION_STATES} のいずれか")
    if not isinstance(ev.get("fetcher_version"), str) \
            or not str(ev.get("fetcher_version") or "").strip():
        v.append(f"{where}.fetcher_version: 空にできない")

    # --- 取得の記録（★何を・いつ・どんな応答で取ったか★）
    f = ev.get("fetch")
    if not isinstance(f, dict):
        v.append(f"{where}.fetch: 辞書でない")
    else:
        for k in ("requested_url", "final_url", "fetched_at",
                  "http_status", "response_sha256"):
            if k not in f:
                v.append(f"{where}.fetch.{k}: 必須")
        for k in ("requested_url", "final_url"):
            if not str(f.get(k, "")).startswith("https://"):
                v.append(f"{where}.fetch.{k}: https のURLでない")
        # ★形だけでなく実在する日時か★（Codex 11巡目 (b)-3）
        #   2026-99-99T25:61:61Z は正規表現を通り、後段で ValueError になっていた
        if not _valid_ts(f.get("fetched_at")):
            v.append(f"{where}.fetch.fetched_at: 実在するUTC日時でない")
        if f.get("http_status") != 200:
            # ★200以外のページを証拠にしない★（404の案内文などを拾わせない）
            v.append(f"{where}.fetch.http_status: 200でない（{f.get('http_status')}）")
        if not _SHA_RE.match(str(f.get("response_sha256", ""))):
            v.append(f"{where}.fetch.response_sha256: sha256でない")

    # --- ページ（見出しと本文の指紋）
    p = ev.get("page")
    if not isinstance(p, dict) or not str(p.get("title") or "").strip():
        v.append(f"{where}.page.title: 空にできない")
    if not isinstance(p, dict) or not _SHA_RE.match(str(p.get("body_sha256", ""))):
        v.append(f"{where}.page.body_sha256: sha256でない")

    # --- 証拠単位（画面上の塊）
    u = ev.get("evidence_unit")
    if not isinstance(u, dict):
        v.append(f"{where}.evidence_unit: 辞書でない")
    else:
        if u.get("unit_type") not in UNIT_TYPES:
            v.append(f"{where}.evidence_unit.unit_type: {UNIT_TYPES} のいずれか")
        for k in ("dom_path", "text"):
            if not str(u.get(k) or "").strip():
                v.append(f"{where}.evidence_unit.{k}: 空にできない")

    # --- 機種の型式（同定の鍵）
    mi = ev.get("machine_identity")
    if not isinstance(mi, dict):
        v.append(f"{where}.machine_identity: 辞書でない")
    else:
        # ★発売日は必須にしない★（identity v2・2026-07-30）
        #   発売日は物理的な型式の不変の識別子ではない（先行導入・全国導入・
        #   再販で揺れる）。必須にすると、同じ型式でも出典が日付を書いていない
        #   だけで証拠が丸ごと無効になる。台の同定は2項目で足りる。
        #   発売日は「表示するための別の事実」として扱う。
        for k in ("manufacturer_id", "regulatory_model_code"):
            if not isinstance(mi.get(k), str) or not mi[k].strip():
                v.append(f"{where}.machine_identity.{k}: 空にできない")
        # 書いてあるなら空文字は認めない（書いた以上は値であること）
        for k in ("market_release_date", "release_date"):
            if k in mi and (not isinstance(mi[k], str) or not mi[k].strip()):
                v.append(f"{where}.machine_identity.{k}: 書くなら空にできない")

    # --- ★指紋が中身と一致すること★（後から中身を書き換えたら落ちる）
    if ev.get("evidence_sha256") != content_sha256(ev):
        v.append(f"{where}.evidence_sha256: 中身と指紋が一致しない"
                 f"（証拠が後から書き換えられている）")
    return v


def validate_evidence(ev: dict, where: str = "evidence") -> None:
    """後方互換：違反が1件でもあれば EvidenceError（全件を本文に載せる）。"""
    v = evidence_violations(ev, where)
    if v:
        raise EvidenceError(" / ".join(v))


def evidence_path(sha: str) -> str:
    return os.path.join(EVIDENCE_DIR, f"{sha}.json")


def load_evidence(sha: str):
    """指紋で証拠を引く。★ファイル名と中身の指紋の両方が一致して初めて返す★

    戻り値 (証拠, 理由)。読めない・合わない場合は (None, 理由)。
    """
    if not _SHA_RE.match(str(sha or "")):
        return None, "EVIDENCE_REF_NOT_SHA256"
    path = evidence_path(sha)
    if not os.path.isfile(path):
        return None, "EVIDENCE_NOT_FOUND"
    try:
        ev = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        return None, f"EVIDENCE_UNREADABLE:{type(e).__name__}"
    try:
        validate_evidence(ev, f"evidence[{sha[:8]}]")
    except EvidenceError as e:
        return None, f"EVIDENCE_INVALID:{e}"
    # ★ファイル名（＝台帳が指した指紋）と中身の指紋が同じであること★
    if content_sha256(ev) != sha:
        return None, "EVIDENCE_SHA_MISMATCH"
    return ev, "OK"


def as_identity_evidence(ev: dict) -> dict:
    """公開ゲート（claim_c5）が使う形へ変換する。

    ★台帳には無い情報をここで供給する★のが要点。
    claim 側が書いた見出し・証拠単位・型式は**一切使わない**。
    """
    return {"page_title": ev["page"]["title"],
            "evidence_unit": dict(ev["evidence_unit"]),
            "machine_identity": dict(ev["machine_identity"]),
            # ★出典の照合・重複排除に使う（台帳のURLは信用しない）★
            "fetch": dict(ev["fetch"]),
            "attestation_state": ev.get("attestation_state"),
            "evidence_sha256": ev.get("evidence_sha256")}


def is_countable(ev: dict) -> tuple[bool, str]:
    """★票に数えてよい段階か★（Codex 9巡目 (a)-3）

    今のPhase 1 は取得も検算もしていないので、正しく作った証拠でも
    `UNATTESTED_METADATA` にしかならず、**票にはならない**。
    これは不具合ではなく、「まだ裏取りできていない」を正直に表した状態。
    """
    st = (ev or {}).get("attestation_state")
    if st in COUNTABLE_STATES:
        return True, "OK"
    return False, f"NOT_ATTESTED:{st}"


def write_evidence(ev: dict, out_dir: str | None = None) -> str:
    """★取得器だけが使う書き込み口★（指紋を計算して指紋名で保存する）"""
    body = {k: v for k, v in ev.items() if k != "evidence_sha256"}
    sha = canonical_sha256(body)
    full = {**body, "evidence_sha256": sha}
    validate_evidence(full, "write")
    d = out_dir or EVIDENCE_DIR
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{sha}.json"), "w", encoding="utf-8") as f:
        json.dump(full, f, ensure_ascii=False, indent=1)
    return sha


# ---------------------------------------------------------------- selftest

def _mk(**over) -> dict:
    ev = {
        "schema_version": SCHEMA_VERSION,
        "fetch": {"requested_url": "https://chonborista.com/x",
                  "final_url": "https://chonborista.com/x",
                  "fetched_at": "2026-07-28T09:00:00Z",
                  "http_status": 200,
                  "response_sha256": "a" * 64},
        "page": {"title": "スマスロテスト機 天井・機械割", "body_sha256": "b" * 64},
        "evidence_unit": {"unit_type": "TABLE_ROW",
                          "dom_path": "table[1]/tbody/tr[2]",
                          "text": "スマスロテスト機｜機械割は設定1:97.2%"},
        "machine_identity": {"manufacturer_id": "test-maker",
                             "regulatory_model_code": "TEST-001",
                             "release_date": "2026-01-01"},
        "fetcher_version": "claim_evidence/1",
        "attestation_state": "UNATTESTED_METADATA",
    }
    ev.update(over)
    ev["evidence_sha256"] = content_sha256(ev)
    return ev


def selftest() -> int:
    import tempfile
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    # -------- identity v2（2026-07-30・Codex指摘 穴5）
    t("★発売日が無くても証拠として成立する（台の同定は2項目）★",
      validate_evidence(_mk(machine_identity={
          "manufacturer_id": "test-maker",
          "regulatory_model_code": "TEST-001"})) is None)
    def _bad_ident(**mi):
        """同定情報が不正なら例外で止まること（戻り値ではなく例外で表す）。"""
        try:
            validate_evidence(_mk(machine_identity=mi))
            return False
        except EvidenceError:
            return True
    t("★メーカーか型式が欠けたら証拠にしない★",
      _bad_ident(manufacturer_id="test-maker")
      and _bad_ident(regulatory_model_code="TEST-001"))
    t("★発売日を書くなら空にはできない（書いた以上は値であること）★",
      _bad_ident(manufacturer_id="test-maker",
                 regulatory_model_code="TEST-001",
                 market_release_date="   ")
      and _bad_ident(manufacturer_id="test-maker",
                     regulatory_model_code="TEST-001",
                     release_date=""))

    def raises(fn):
        try:
            fn()
            return False
        except EvidenceError:
            return True

    ok = _mk()
    t("正しい証拠は検査を通る", validate_evidence(ok) is None)
    t("★★中身を1文字でも書き換えたら落ちる（指紋が合わない）★★",
      raises(lambda: validate_evidence(
          {**ok, "page": {**ok["page"], "title": "別の見出し"}})))
    t("★指紋の欄だけ書き換えても落ちる",
      raises(lambda: validate_evidence({**ok, "evidence_sha256": "c" * 64})))
    t("★取得できていないページ（200以外）は証拠にしない",
      raises(lambda: validate_evidence(_mk(
          fetch={**ok["fetch"], "http_status": 404}))))
    t("★httpsでないURLは証拠にしない",
      raises(lambda: validate_evidence(_mk(
          fetch={**ok["fetch"], "final_url": "http://chonborista.com/x"}))))
    t("★応答の指紋が無ければ証拠にしない（取得の記録が要る）",
      raises(lambda: validate_evidence(_mk(
          fetch={**ok["fetch"], "response_sha256": "short"}))))
    t("★証拠単位の種類が決まっていなければ落ちる",
      raises(lambda: validate_evidence(_mk(
          evidence_unit={**ok["evidence_unit"], "unit_type": "FREE_TEXT"}))))
    t("★DOM上の位置が無ければ落ちる（どこから取ったか分からない）",
      raises(lambda: validate_evidence(_mk(
          evidence_unit={**ok["evidence_unit"], "dom_path": " "}))))
    t("★型式が欠けていれば落ちる",
      raises(lambda: validate_evidence(_mk(
          machine_identity={"manufacturer_id": "a"}))))
    t("★スキーマ版が違えば落ちる",
      raises(lambda: validate_evidence({**ok, "schema_version": "x/0"})))

    # --- 指紋で引く経路
    d = tempfile.mkdtemp()
    global EVIDENCE_DIR
    keep = EVIDENCE_DIR
    EVIDENCE_DIR = d
    try:
        sha = write_evidence({k: v for k, v in ok.items()
                              if k != "evidence_sha256"})
        got, why = load_evidence(sha)
        t("取得器が書いた証拠を、指紋で引ける", got is not None and why == "OK")
        t("★存在しない指紋は引けない",
          load_evidence("f" * 64)[1] == "EVIDENCE_NOT_FOUND")
        t("★指紋の形でない参照は受け付けない",
          load_evidence("not-a-sha")[1] == "EVIDENCE_REF_NOT_SHA256")
        # ★ファイル名はそのままに中身だけ差し替える（改ざん）
        tampered = json.load(open(evidence_path(sha), encoding="utf-8"))
        tampered["evidence_unit"]["text"] = "スマスロテスト機｜機械割は設定1:99.9%"
        json.dump(tampered, open(evidence_path(sha), "w", encoding="utf-8"),
                  ensure_ascii=False)
        t("★★保存後に中身を書き換えたら引けなくなる★★",
          load_evidence(sha)[0] is None)
        t("　その理由が指紋の不一致だと分かる",
          load_evidence(sha)[1].startswith("EVIDENCE_INVALID"))
    finally:
        EVIDENCE_DIR = keep

    t("公開ゲートが使う形へ変換できる",
      as_identity_evidence(ok)["page_title"] == ok["page"]["title"]
      and as_identity_evidence(ok)["evidence_unit"]["unit_type"] == "TABLE_ROW")

    ng = [n for n, c in results if not c]
    print(f"\n{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--list", action="store_true", help="保管されている証拠を一覧")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if args.list:
        if not os.path.isdir(EVIDENCE_DIR):
            print("証拠はまだ1件もありません:", EVIDENCE_DIR)
            return 0
        n = ng = 0
        for fn in sorted(os.listdir(EVIDENCE_DIR)):
            if not fn.endswith(".json"):
                continue
            n += 1
            ev, why = load_evidence(fn[:-5])
            mark = "OK " if ev else "NG "
            if not ev:
                ng += 1
            print(f"  [{mark}] {fn[:12]}… {why}")
        print(f"証拠 {n} 件 / 引けないもの {ng} 件")
        return 1 if ng else 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

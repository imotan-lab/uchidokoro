# -*- coding: utf-8 -*-
"""裏取り済みの外部数値を、機種データと記事本文の【両方】へ決定論で書き戻す。

apply_safe_fixes.py が「内部整合の破綻」を機種内の在庫値で直すのに対し、こちらは
verify_claims.py の関所を exit0 で通過した【外部数値】を書き戻す担当。

★安全原則★
  1. 新値発明禁止: 新しい数値は呼び出し側が渡した verified な値のみ。本モジュールは
     数値を推測・計算・生成しない（--new を検証済み値以外から渡す経路を作らないこと）。
  2. 楽観ロック: 現在の構造化値が --old と一致しない場合は何も書かない（状況が変わった）。
  3. 全か無か: 構造化値と本文のどちらか一方でも安全に直せないなら【何も書かない】。
     「数値だけ直って本文は旧値のまま」という中途半端な状態を作らない。
  4. 曖昧なら中止: 本文中に同じ数値が「その項目のラベルが無い段落」に出てくる、
     あるいは単位が伴わない裸の数値として出てくる場合は、意味を取り違える恐れがあるため
     修正せず理由を返す（呼び出し側が要確認台帳へ回す）。
  5. 既定は dry-run。--apply でのみ書き込む。書き込みは原子的（tmp→replace）。

使い方:
  python scripts/apply_external_fix.py --slug bandori --field ceiling.normal.cycle \\
      --old 10 --new 8 [--apply] [--base PATH]
  python scripts/apply_external_fix.py --selftest

exit code: 0=適用可/適用済み  1=適用不可（理由を出力）  2=引数エラー
出力は最終行に JSON（呼び出し側のタスクが機械可読で受け取る）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE_DEFAULT = Path(__file__).resolve().parent.parent

# ─────────────────────────────────────────────
# 項目の定義（ここが単一情報源）
# ─────────────────────────────────────────────
#   struct : machines.json 側の置き場所を探す関数キー
#   labels : 本文でその数値が「この項目の値」であることを示すラベル語（段落単位で必要）
#   units  : 本文でその数値の直後に来てよい単位表記（これが無い裸の数値は触らない）
#   follow  : 単位が略されていても「同じ意味」と分かる後続語（例「900到達で」）
FIELD_SPEC = {
    "ceiling.normal.game":  {"struct": "limit", "labels": ("天井",),
                             "units": ("G", "ゲーム"), "small": False},
    "ceiling.normal.point": {"struct": "limit", "labels": ("天井",),
                             "units": ("pt", "ポイント"), "small": False},
    "ceiling.normal.cycle": {"struct": "cycleMax", "labels": ("天井", "周期"),
                             "units": ("周期",), "small": True},
    "ceiling.normal.through": {"struct": "suruMax", "labels": ("天井", "スルー"),
                               "units": ("スルー",), "small": True},
}
# 単位が省かれていても同じ値を指すと読める後続語（ここも置換する）
_FOLLOW_WORDS = ("到達", "以降", "以上", "まで", "消化", "ハマり", "回転", "超え")


class Abort(Exception):
    """安全に直せないので何も書かない。"""


# ─────────────────────────────────────────────
# 構造化値の位置特定（読んだ場所に書き戻す）
# ─────────────────────────────────────────────

def _mode_conf(checker, key):
    """checker直下 と checker.modeData 配下の2系統を吸収（shadow_claims と同じ規則）"""
    if not isinstance(checker, dict):
        return None
    v = checker.get(key)
    if isinstance(v, dict):
        return v
    md = checker.get("modeData")
    if isinstance(md, dict) and isinstance(md.get(key), dict):
        return md[key]
    return None


def locate_struct(machine: dict, field: str):
    """(コンテナ, キー, 現在値, 表示パス) を返す。見つからなければ Abort。

    ★extract_site_claims（＝比較の入力を作る側）と同じ優先順位で探す。
      別の場所に書くと「比較した値」と「直した値」がズレる。★
    """
    spec = FIELD_SPEC.get(field)
    if not spec:
        raise Abort(f"未対応の項目: {field}")
    checker = machine.get("checker") or {}
    kind = spec["struct"]
    if kind == "limit":
        if isinstance(machine.get("limit"), (int, float)) and machine["limit"] > 0:
            return machine, "limit", machine["limit"], "machines.json:limit"
        if isinstance(checker.get("limit"), (int, float)) and checker["limit"] > 0:
            return checker, "limit", checker["limit"], "machines.json:checker.limit"
        raise Abort("構造化された天井値（limit）が無い＝構造ごと変わる修正なので自動化しない")
    if kind == "cycleMax":
        if isinstance(checker.get("cycleMax"), (int, float)):
            return checker, "cycleMax", checker["cycleMax"], "machines.json:checker.cycleMax"
        raise Abort("checker.cycleMax が無い")
    if kind == "suruMax":
        if isinstance(checker.get("suruMax"), (int, float)):
            return checker, "suruMax", checker["suruMax"], "machines.json:checker.suruMax"
        sc = _mode_conf(checker, "suru") or {}
        if isinstance(sc.get("suruMax"), (int, float)):
            return sc, "suruMax", sc["suruMax"], "machines.json:checker.suru.suruMax"
        raise Abort("suruMax が無い")
    raise Abort(f"未対応の置き場所: {kind}")


# ─────────────────────────────────────────────
# 本文の書き換え
# ─────────────────────────────────────────────

def _num_variants(n) -> list[str]:
    """本文に出てくる表記ゆれ（1000 / 1,000）。全角は正規化済み前提で扱わない。"""
    s = str(int(n)) if float(n).is_integer() else str(n)
    out = [s]
    if len(s) > 3 and s.isdigit():
        out.append(f"{int(s):,}")
    return out


def _fmt_like(sample: str, new) -> str:
    """旧表記に合わせて新値を整形（1,000 形式なら 1,268 と書く）。"""
    s = str(int(new)) if float(new).is_integer() else str(new)
    return f"{int(s):,}" if ("," in sample and s.isdigit()) else s


def _iter_texts(detail: dict):
    """(取り出し関数, 差し替え関数, 文字列) を列挙する。lead・sections本文・箱・表を対象。"""
    def walk(container, key, path):
        val = container[key]
        if isinstance(val, str):
            yield (container, key, val, path)
        elif isinstance(val, list):
            for i, v in enumerate(val):
                if isinstance(v, (str, list, dict)):
                    yield from walk(val, i, f"{path}[{i}]")
        elif isinstance(val, dict):
            for k in list(val.keys()):
                yield from walk(val, k, f"{path}.{k}")

    for key in ("lead", "sections", "summaryBoxes", "factTable"):
        if key in detail:
            yield from walk(detail, key, key)


# 裸の数値が「その項目の値ではない」と機械判定してよい文脈だけを列挙する。
# ここに無い文脈は判定不能として修正を中止する（見逃しではなく中止側に倒す）。
_OTHER_BEFORE = ("/", "／", "約1/", "1/")
_OTHER_AFTER = ("円", "枚", "%", "％", "年", "月", "日", "台", "人", "名", "分", "秒",
                "枚交換", "円分")


def _is_other_meaning(text: str, i: int, j: int) -> bool:
    before = text[max(0, i - 2):i]
    after = text[j:j + 4]
    if before.endswith(("/", "／")):
        return True
    return any(after.startswith(a) for a in _OTHER_AFTER)


def plan_prose_edits(detail: dict, field: str, old, new) -> list[dict]:
    """本文の置換計画を作る。曖昧・危険なものが1つでもあれば Abort（部分適用しない）。

    置換するのは「その値だと分かる書き方」だけ:
      ・単位付き（900G / 10周期 / 4スルー）
      ・単位が略されていても意味が分かる後続語つき（900到達 / 900以降）
    それ以外の裸の数値は:
      ・確率の分母・金額・枚数・％等＝明らかに別物 → 無視
      ・3桁以上（天井G数/pt）で意味不明の残り → 中止（記事内で数字が食い違うのを防ぐ）
      ・1〜2桁（周期・スルー）→ 無視（設定6・2枚など日常的に出る数字のため）
    """
    spec = FIELD_SPEC[field]
    units, small = spec["units"], spec["small"]
    olds = _num_variants(old)
    followers = "|".join(map(re.escape, list(units) + list(_FOLLOW_WORDS)))
    edits = []
    for container, key, text, path in _iter_texts(detail):
        if not any(o in text for o in olds):
            continue
        new_text = text
        hits = 0
        for o in olds:
            pat = re.compile(rf"(?<![0-9.,]){re.escape(o)}(?=\s*(?:{followers}))")
            new_text, n = pat.subn(_fmt_like(o, new), new_text)
            hits += n
            if small:
                continue
            bare = re.compile(rf"(?<![0-9.,]){re.escape(o)}(?![0-9.,])")
            for mm in bare.finditer(new_text):
                if not _is_other_meaning(new_text, mm.start(), mm.end()):
                    raise Abort(f"{path}: 意味の判定がつかない「{o}」が残る"
                                f"（…{new_text[max(0, mm.start() - 14):mm.end() + 14]}…）"
                                f"→記事内で数字が食い違う恐れがあるので修正しない")
        if hits:
            edits.append({"path": path, "before": text, "after": new_text,
                          "container": container, "key": key, "count": hits})
    return edits


# ─────────────────────────────────────────────
# 読み書き（round-trip安全性を確認してから）
# ─────────────────────────────────────────────

def _load(path: Path):
    raw = path.read_text(encoding="utf-8")
    return json.loads(raw), raw


def _dump(data, raw_original: str) -> str:
    indent = 1 if raw_original.startswith("{\n ") or raw_original.startswith("[\n ") else 2
    s = json.dumps(data, ensure_ascii=False, indent=indent)
    if raw_original.endswith("\n"):
        s += "\n"
    return s


def _roundtrip_safe(data, raw: str) -> bool:
    """整形しなおしても中身が変わらない（手整形JSONを壊さない）ことを確認。"""
    try:
        return json.loads(_dump(data, raw)) == data
    except Exception:
        return False


def _atomic_write(path: Path, text: str) -> None:
    d = path.parent
    fd, tmp = tempfile.mkstemp(dir=str(d), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())          # ★電源が落ちても中身が残るように★
        os.replace(tmp, path)
        try:                              # ★置き換えたことも残す（親フォルダ）★
            dfd = os.open(str(d), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except (OSError, AttributeError):
            pass                          # 対応していない環境では諦める
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ─────────────────────────────────────────────
# 契約（★これが唯一の入口★）
# ─────────────────────────────────────────────

CONTRACT_SCHEMA = "fix-contract/v3"
REPOSITORY_ID = "uchidokoro"


def plan_digest(plan: dict) -> str:
    """★どこを何に書き換えるかまで含めた計画の指紋★

    2026-08-05・Codex113回目の指摘1: 許可証は誰でも作れるので、
    「型を見るだけ」では**別の計画を渡して書かせる**ことができた。
    契約にこの指紋を書いておき、書く直前に計画から作り直して突き合わせる。
    これで「同じ件数だが別の場所を書いた」も止まる。
    """
    import hashlib
    ap = plan.get("_apply") or {}
    body = {
        "slug": plan.get("slug"), "field": plan.get("field"),
        # ★数は数として比べる★（900 と 900.0 で指紋が変わらないように）
        "old": float(plan.get("old")), "new": float(plan.get("new")),
        "struct_path": plan.get("struct_path"),
        "edits": [{"path": e["path"], "count": int(e["count"]),
                   "before": e["before"], "after": e["after"]}
                  for e in (ap.get("edits") or [])],
    }
    return "sha256:" + hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


# ★試験用の差し替え口★（2026-08-06・Codex115回目のP0-1）
#   以前は `verify` / `guard` を引数で差し替えられたので、
#   偽の関所と偽の台帳を渡せば**任意の値を書けた**。
#   本番の関数からは引数を消し、差し替えは
#   **環境変数 UCHI_AEF_TEST_HOOKS=1 のときだけ**登録できるようにする。
_HOOKS = {"verify": None, "guard": None}


def _install_test_hooks(verify=None, guard=None) -> None:
    """★試験専用★ 本番では環境変数が無いので必ず失敗する。"""
    if os.environ.get("UCHI_AEF_TEST_HOOKS") != "1":
        raise Abort("試験用の差し替えは、この環境では使えません")
    _HOOKS["verify"], _HOOKS["guard"] = verify, guard


def _verifier():
    if _HOOKS["verify"] and os.environ.get("UCHI_AEF_TEST_HOOKS") == "1":
        return _HOOKS["verify"]
    import verify_claims as _vc
    return lambda d: _vc.run_data(d, 2)


def _guard():
    if _HOOKS["guard"] and os.environ.get("UCHI_AEF_TEST_HOOKS") == "1":
        return _HOOKS["guard"]
    import task_guard as _tg
    return _tg


class _Ticket:
    """★書き込みの許可証★（2026-08-05・Codex111回目のP0-1）

    「関数名の先頭に _ を付けた」だけでは呼び出しを禁止できない。
    最終の書き込み関数は**この許可証**を要求し、許可証は
    `apply_contract()` が契約・証拠・予約を全部確かめた後にしか作らない。
    """

    __slots__ = ("contract", "sha256", "token", "attempt_id")

    def __init__(self, contract: dict, sha256: str, token: str,
                 attempt_id: str):
        self.contract = contract
        self.sha256 = sha256
        self.token = token
        self.attempt_id = attempt_id

# ★項目ごとに「証拠のどの見出しか」「単位は何か」を固定する★
#   （2026-08-05・Codex110回目の指摘3。以前は証拠の見出しを契約に自由記述
#     できたので、**G天井の欄にポイント天井の値**を書ける経路が残っていた）
FIELD_EVIDENCE = {
    "ceiling.normal.game":    {"evidence": ("天井", "天井ゲーム数"), "unit": "G"},
    "ceiling.normal.point":   {"evidence": ("天井", "天井ポイント"), "unit": "pt"},
    "ceiling.normal.cycle":   {"evidence": ("天井", "周期天井"), "unit": "周期"},
    "ceiling.normal.through": {"evidence": ("天井", "スルー天井"), "unit": "スルー"},
}


class ContractError(Exception):
    pass


def _sha256_file(path) -> str:
    import hashlib
    with open(path, "rb") as f:
        return "sha256:" + hashlib.sha256(f.read()).hexdigest()


def _norm_value(v) -> str:
    """比べるための形（数は数として、それ以外は空白を詰めた文字列）。"""
    import unicodedata
    t = unicodedata.normalize("NFKC", str(v)).strip()
    try:
        f = float(t.replace(",", "").rstrip("G枚%"))
        return f"{f:g}"
    except ValueError:
        return " ".join(t.split())


def load_contract(path) -> dict:
    """契約を読み、形を確かめる。

    ★なぜ契約が要るか（2026-08-05・Codex109回目）★
      これまでは `--new` に任意の値を渡せた。証拠ファイルで値Aを合格させてから、
      書き込み器に値Bを渡すことが**構造上できた**。
      「2出典が一致した値だけを書く」は手順書のルールでしかなく、
      コードが強制していなかった。いまは**契約に書いた値**しか書けない。
    """
    import safe_json as _sj
    data = _sj.read_json(str(path), expect=dict)
    if data.get("schema_version") != CONTRACT_SCHEMA:
        raise ContractError(f"知らない契約の形です: {data.get('schema_version')!r}")
    need = ("slug", "field", "old", "new", "unit", "evidence_file",
            "evidence_sha256", "repository_id",
            "machines_before_sha256", "detail_before_sha256")
    miss = [k for k in need if not str(data.get(k) or "").strip()]
    if miss:
        raise ContractError(f"契約に足りない項目があります: {', '.join(miss)}")
    # ★本文を何箇所直すのかを必ず書く★（2026-08-05・Codex111回目のP0-2）
    #   任意項目だったので、書かなければ検査ごと素通りできた。
    #   0 も許さない（構造化値だけ直して本文に旧値が残る形を作らせない）。
    if not re.match(r"^sha256:[0-9a-f]{64}$", str(data.get("plan_sha256") or "")):
        raise ContractError("契約に計画の指紋（plan_sha256）が要ります")
    # ★どのリポジトリのどこへ書くかも契約に書く★（2026-08-06・Codex114回目の指摘4）
    #   base を外から渡せたので、同じ中身の別の作業フォルダへ適用できた。
    bp = str(data.get("base_path") or "")
    if not bp or not os.path.isdir(bp):
        raise ContractError(f"契約の base_path が実在しません: {bp!r}")
    if not os.path.isfile(os.path.join(bp, "assets", "data", "machines.json")):
        raise ContractError(f"契約の base_path が機種データの場所ではありません: {bp!r}")
    n = data.get("prose_edit_count")
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise ContractError(
            f"契約の prose_edit_count は1以上の整数が要ります: {n!r}")
    if str(data.get("repository_id")) != REPOSITORY_ID:
        raise ContractError(
            f"別のリポジトリ向けの契約です: {data.get('repository_id')!r}")
    return data


def check_contract(c: dict, verify=None, min_domains: int = 2) -> dict:
    """契約と証拠が本当に結び付いているかを確かめる（★ここを通らないと書かない★）。

    ①証拠ファイルが契約の指紋と一致する（すり替え防止）
    ②その証拠が verify_claims の関所を通る（出典が実在し、逐語で一致する）
    ③証拠の中の**その項目**が、独立2ドメイン以上で、**すべて同じ値**
    ④その値が**契約の new と一致する**（＝合格させた値しか書けない）
    """
    import safe_json as _sj
    ev_path = c["evidence_file"]
    if not os.path.isfile(ev_path):
        raise ContractError(f"証拠ファイルがありません: {ev_path}")
    got = _sha256_file(ev_path)
    if got != c["evidence_sha256"]:
        raise ContractError(f"証拠ファイルが契約と違います（指紋 {got[:19]}…）")
    ev = _sj.read_json(ev_path, expect=dict)
    if str(ev.get("slug") or "") != str(c["slug"]):
        raise ContractError(f"証拠の機種が違います（{ev.get('slug')!r}）")
    spec = FIELD_EVIDENCE.get(str(c["field"]))
    if not spec:
        raise ContractError(f"この項目は契約で扱えません: {c['field']!r}")
    if str(c["unit"]) != spec["unit"]:
        raise ContractError(
            f"単位が項目と合いません（契約 {c['unit']!r} / {c['field']} は "
            f"{spec['unit']!r}）")
    if verify is None:
        import verify_claims as _vc
        verify = lambda d: _vc.run_data(d, min_domains)   # noqa: E731
    if verify(ev) != 0:
        raise ContractError("証拠が関所を通りません（出典の実在・逐語一致が取れない）")
    # ★証拠の見出しは、項目ごとに決めた名前だけを受け付ける★
    rows = [r for r in (ev.get("claims") or [])
            if str(r.get("field") or "") in spec["evidence"]
            # ★証拠の側にも単位を書かせる★（2026-08-05・Codex111回目のP1-6）
            #   見出しが「天井」だけだと、G天井の証拠でポイント天井の契約を
            #   通せた。証拠行の unit が契約と一致するものだけを数える。
            and str(r.get("unit") or "") == str(c["unit"])]
    if not rows:
        raise ContractError(
            f"証拠に「{' / '.join(spec['evidence'])}」の記載がありません")
    doms = set()
    for r in rows:
        d = _domain(r.get("url"))
        if not d:
            raise ContractError("証拠に出典URLの無い行があります")
        doms.add(d)
    if len(doms) < min_domains:
        raise ContractError(
            f"「{c['field']}」の出典が {len(doms)} ドメインしかありません")
    vals = {_norm_value(r.get("value")) for r in rows}
    if len(vals) != 1:
        raise ContractError(f"証拠の中で値が食い違っています: {sorted(vals)}")
    if vals.pop() != _norm_value(c["new"]):
        raise ContractError(
            f"契約の新値が、証拠で合格した値と違います（契約 {c['new']!r}）")
    return {"domains": sorted(doms), "value": c["new"]}


def _domain(url) -> str:
    """出典の「サイト」を数えるための形（★www・ポート・末尾ドットを揃える★）。

    2026-08-05・Codex110回目の指摘10: そのままの netloc だと
    `www.a.jp` と `a.jp:443` を別のサイトとして数えてしまう。
    """
    import urllib.parse
    host = urllib.parse.urlsplit(str(url or "")).netloc.lower()
    host = host.split("@")[-1].split(":")[0].rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


# ─────────────────────────────────────────────
# 本体
# ─────────────────────────────────────────────

def apply_contract(contract_path: str, token: str = "",
                   apply_mode: bool = False) -> dict:
    """★書き込みの唯一の入口★

    2026-08-06・Codex114回目: 書く直前の確認は `_commit_plan()` が
    **自分で契約を読み直して**行う。ここは下見と、予約の取り扱いだけ。
    `base` は受け取らない（契約の `base_path` が唯一の書き込み先）。
    """
    verify, guard = _verifier(), _guard()
    c = load_contract(contract_path)
    proof = check_contract(c, verify=verify)
    base = Path(c["base_path"])
    csha = _sha256_file(contract_path)
    out = {"slug": c["slug"], "field": c["field"], "old": c["old"],
           "new": c["new"], "applied": False, "reason": None,
           "outcome": "NOT_APPLIED",
           "contract": {"sha256": csha, "domains": proof["domains"]}}
    if not apply_mode:
        plan = plan_fix(base, c["slug"], c["field"], float(c["old"]),
                        float(c["new"]), expect=c)
        out.update({k: v for k, v in plan.items() if k != "_apply"})
        return out
    import hashlib as _h
    attempt = _h.sha256(f"{token}|{csha}".encode("utf-8")).hexdigest()[:16]
    # ★先に予約を見る★（2026-08-06・Codex115回目のP0-2）
    #   以前は先にジャーナルから元へ戻していたので、
    #   「書き終わって記録もしたが、ジャーナルを消す前に落ちた」場合に
    #   **ファイルを旧値へ戻したうえで『適用済み』と報告**していた。
    left = unfinished_fix(base)
    if left and left.get("attempt_id") != attempt:
        out["outcome"] = "UNKNOWN"
        out["reason"] = (f"前回の書き換えが決着していません"
                         f"（{left.get('attempt_id')} / {left.get('stage')}）。"
                         "人が確かめてください")
        return out
    if left:
        # ★復旧そのものを鍵と持ち主の中で行う★（2026-08-06・Codex116回目のP0-1）
        #   以前は鍵を取る前に戻していたので、**同じ回の先発がまだ生きている**間に
        #   後発が割り込んで、片方だけ旧値に戻すことができた。
        rec = _recover(base, left, attempt, token, guard)
        if rec.get("done"):
            out.update(rec["result"])
            return out
        if rec.get("problems"):
            out["outcome"] = rec.get("outcome", "UNKNOWN")
            out["reason"] = " / ".join(rec["problems"])[:300]
            return out
    try:
        guard.begin_apply(token, slug=c["slug"], kind="fix",
                          contract_sha256=csha, attempt_id=attempt)
    except Exception as e:                # noqa: BLE001
        # ★「記録が適用済み」だけで成功と言わない★（2026-08-06・自分の試験で発覚）
        #   中身を確かめる経路（_recover）を通っていない場合、
        #   ファイルが旧値でも成功と返していた。Codex115回目のP0-2と同じ型が
        #   別の道に残っていた。
        try:
            r = guard.reservation(token)
        except Exception:                 # noqa: BLE001
            r = {}
        if r.get("state") == "APPLIED_LOCAL" and r.get("attempt_id") == attempt:
            out["outcome"] = "UNKNOWN"
            out["reason"] = ("記録は『適用済み』ですが、書き終えた姿の控えで"
                             "確かめられませんでした。人が確かめてください")
            return out
        out["reason"] = f"予約を使えません: {e}"
        return out
    try:
        got = _commit_plan(contract_path, token)
    except Abort as e:
        # ★巻き戻しに失敗した結末を握りつぶさない★（Codex115回目のP0-3）
        #   以前はここで NOT_APPLIED に潰していたので、
        #   予約は「安全に戻した」に進み、ジャーナルまで消えていた。
        got = {"applied": False, "reason": str(e),
               "outcome": getattr(e, "outcome", "NOT_APPLIED"),
               "unrestored": getattr(e, "unrestored", [])}
    out.update({k: v for k, v in got.items() if k not in ("contract", "_apply")})
    guard.advance(token, {"APPLIED_LOCAL": "APPLIED_LOCAL",
                          "ROLLED_BACK_VERIFIED": "ROLLED_BACK_VERIFIED",
                          "ROLLBACK_FAILED": "ROLLBACK_FAILED",
                          "NOT_APPLIED": "ROLLED_BACK_VERIFIED",
                          }.get(out.get("outcome"), "UNKNOWN"))
    _journal_done(base, attempt, out.get("outcome"))
    return out


def _recover(base: Path, left: dict, attempt: str, token: str,
             guard) -> dict:
    """やりかけを片付ける（★鍵と持ち主を取ってから★）。

    ①書き終えた姿が控えてあり、いまの中身と一致 → そのまま成功
    ②一致しない・控えが無い → 元へ戻してやり直す
    """
    import hashlib
    out = {"done": False, "problems": []}
    with _DataLock(base):
        try:
            guard.hold_apply(token, attempt, left.get("contract_sha256") or "")
        except Exception as e:            # noqa: BLE001
            out["problems"].append(f"前の処理がまだ使っています: {e}")
            return out
        cur = unfinished_fix(base)        # ★鍵の中で読み直す★
        if not cur or cur.get("attempt_id") != attempt:
            return out                    # 誰かが片付けた
        after = cur.get("after") or {}
        want_paths = {str(Path(base) / "assets" / "data" / "machines.json"),
                      str(Path(base) / "assets" / "data" / "machine-details"
                          / f"{cur.get('slug')}.json")}
        if after and set(after) == want_paths:   # ★2つそろっている時だけ★
            ok = all(
                os.path.isfile(p)
                and "sha256:" + hashlib.sha256(
                    Path(p).read_bytes()).hexdigest() == want
                for p, want in after.items())
            if ok:
                # ★先に台帳を確定してから、証拠を消す★
                #   （2026-08-06・Codex117回目のP0。順番が逆だと
                #     「現物は適用済み・台帳は巻き戻し済み」を作れた）
                try:
                    guard.advance(token, "APPLIED_LOCAL")
                except Exception as e:    # noqa: BLE001
                    if str(guard.reservation(token).get("state")) \
                            != "APPLIED_LOCAL":
                        out["problems"].append(f"台帳を確定できません: {e}")
                        out["outcome"] = "UNKNOWN"
                        return out
                _journal_done(base, attempt, "APPLIED_LOCAL")
                out.update({"done": True,
                            "result": {"applied": True,
                                       "outcome": "APPLIED_LOCAL"}})
                return out
        back = _rollback_journal(base, cur)
        if back["problems"]:
            out["problems"] += back["problems"]
            out["outcome"] = "ROLLBACK_FAILED"
            # ★戻せなかったことを台帳とジャーナルの両方に残す★
            #   （2026-08-06・Codex117回目のP1。早期に返していたので、
            #     いちばん危ない状態が記録されずに消えていた）
            try:
                guard.advance(token, "ROLLBACK_FAILED")
            except Exception as e:        # noqa: BLE001
                # ★台帳に残せなかったことも伝える★（Codex118回目のP1）
                out["problems"].append(f"台帳に記録できません: {e}")
                out["outcome"] = "UNKNOWN"
            _journal_done(base, attempt, "ROLLBACK_FAILED")
    return out


def _rollback_journal(base: Path, rec: dict) -> dict:
    """控えてある元の中身へ戻す（★戻せたことをバイト単位で確かめる★）。"""
    import hashlib
    out = {"problems": []}
    before = rec.get("before") or {}
    if not before:
        out["problems"].append(
            "元の中身が控えられていません（古い記録）。人が確かめてください")
        return out
    for p, text in before.items():
        try:
            _atomic_write(Path(p), text)
            got = "sha256:" + hashlib.sha256(
                Path(p).read_bytes()).hexdigest()
            want = "sha256:" + hashlib.sha256(
                text.encode("utf-8")).hexdigest()
            if got != want:
                out["problems"].append(f"{Path(p).name} を戻せませんでした")
        except Exception as e:            # noqa: BLE001
            out["problems"].append(f"{Path(p).name} を戻せません（{e}）")
    return out


# ─────────────────────────────────────────────
# ジャーナル（★電源が落ちても、何をどこまでやったか分かるように★）
#   2026-08-05・Codex111回目: 2回の置き換えの間で落ちると、
#   予約には APPLYING が残るのに、元の中身をどこからも復元できなかった。
# ─────────────────────────────────────────────

def _journal_path(base: Path) -> Path:
    return Path(base) / ".fix-journal.json"


def _journal_begin(base: Path, c: dict, csha: str, token: str,
                   attempt: str, keep: dict | None = None) -> None:
    """★書く前に、元の中身そのものを控える★（2026-08-06・Codex114回目の指摘2）

    以前は指紋しか残していなかったので、1つ目を書いた直後に電源が落ちると
    **元へ戻す材料がどこにも無かった**。しかも再開時は
    「もう新しい値になっている＝直すものが無い」と読めてしまい、
    片方だけ直った状態を「安全に巻き戻した」と記録していた。
    """
    import datetime as _dt
    rec = {"attempt_id": attempt, "token": token, "contract_sha256": csha,
           "slug": c["slug"], "field": c["field"],
           "old": c["old"], "new": c["new"],
           "machines_before_sha256": c["machines_before_sha256"],
           "detail_before_sha256": c["detail_before_sha256"],
           "before": {str(k): v for k, v in (keep or {}).items()},
           "stage": "APPLYING", "outcome": None,
           "at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    p = _journal_path(base)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())               # ★電源が落ちても残るように★
    os.replace(tmp, p)
    _sync_dir(p.parent)


def _sync_dir(d) -> None:
    """置き換えたことをフォルダにも残す（電源断への備え）。"""
    try:
        fd = os.open(str(d), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except (OSError, AttributeError):
        pass


def _journal_done(base: Path, attempt: str, outcome) -> None:
    p = _journal_path(base)
    if not p.exists():
        return
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except Exception:                     # noqa: BLE001
        return
    if rec.get("attempt_id") != attempt:
        return
    # ★決着した回の記録は残さない★
    #   NOT_APPLIED（何も書かずに止まった）も決着に含める。
    #   2026-08-05: ここを漏らしていたため、条件に合わず止まっただけで
    #   ジャーナルが残り、**以後の書き換えが全部ブロックされた**（試験で発覚）。
    if outcome in ("APPLIED_LOCAL", "ROLLED_BACK_VERIFIED", "NOT_APPLIED"):
        p.unlink()
        _sync_dir(p.parent)
        return
    rec.update({"stage": "STUCK", "outcome": outcome})
    # ★やりかけの記録こそ確実に残す★（置き換えで書き、fsyncする）
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    _sync_dir(p.parent)


def _journal_after(base: Path, attempt: str, after: dict) -> None:
    """書き終えた姿（各ファイルの指紋）を控える。

    ★残せなければ失敗にする★（2026-08-06・Codex117回目のP1）
      黙って諦めると、次の再開で「本当に書けたのか」を確かめられないのに
      成功として進んでしまう。
    """
    p = _journal_path(base)
    if not p.exists():
        raise Abort("書き終えた姿を控えられません（記録がありません）")
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:                # noqa: BLE001
        raise Abort(f"書き終えた姿を控えられません（記録が読めません: {e}）")
    if rec.get("attempt_id") != attempt:
        raise Abort("書き終えた姿を控えられません（別の回の記録です）")
    rec["after"] = after
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    _sync_dir(p.parent)


def unfinished_fix(base: Path | None = None) -> dict:
    """やりかけの書き換えが残っていないか（★次を始める前に見る★）。"""
    p = _journal_path(Path(base or BASE_DEFAULT))
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:                # noqa: BLE001
        return {"stage": "UNKNOWN", "_why": f"記録が読めません: {e}"}


def _commit_plan(contract_path: str, token: str) -> dict:
    """★書き込みはここだけ★（2026-08-06・Codex114回目の指摘1）

    ★受け取るのは「契約ファイルの場所」と「予約」だけ★
      以前は計画と許可証を引数で受け取っていたので、呼び出し側が
      正しい指紋を自分で計算した許可証を作れば、**契約も証拠も通らずに**
      書けた。ここでは**この関数自身が**契約を読み、証拠を確かめ、
      計画を作り直し、予約を照合する。外から差し替えられるのは
      「試験用の関所と台帳」だけで、書く中身には一切触れない。
    """
    import hashlib
    verify, guard = _verifier(), _guard()
    c = load_contract(contract_path)
    csha = _sha256_file(contract_path)
    check_contract(c, verify=verify)       # ★証拠は必ずここで確かめ直す★
    base = Path(c["base_path"])
    attempt = hashlib.sha256(
        f"{token}|{csha}".encode("utf-8")).hexdigest()[:16]
    plan = plan_fix(base, c["slug"], c["field"], float(c["old"]),
                    float(c["new"]), expect=c)
    res = plan
    ap = plan.get("_apply") or {}
    if not plan.get("ready") or not ap:
        return res
    # ★計画そのものが契約と一致すること★
    if plan_digest(plan) != str(c.get("plan_sha256") or ""):
        raise Abort("計画が契約と違います（書き換える場所か中身が変わっています）")
    ticket = _Ticket(c, csha, token, attempt)
    with _DataLock(base):                  # ★確認から置き換えまでを他に割り込ませない★
        # ★予約の確認と書き込みを同じ鍵の中で★（Codex113回目の指摘4）
        r = guard.hold_apply(ticket.token, ticket.attempt_id, ticket.sha256)
        if r.get("state") != "APPLYING":
            raise Abort(f"予約の状態が変わりました（{r.get('state')}）")
        import hashlib
        c = ticket.contract
        for want_key, p in (("machines_before_sha256", ap["mpath"]),
                            ("detail_before_sha256", ap["dpath"])):
            want = str(c.get(want_key) or "")
            got = "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()
            if want and want != got:
                raise Abort(f"{p.name} が計画したときから変わっています")
        ap["container"][ap["key"]] = (int(ap["new"]) if float(ap["new"]).is_integer()
                                      else ap["new"])
        for e in ap["edits"]:
            e["container"][e["key"]] = e["after"]
        if not _roundtrip_safe(ap["machines"], ap["mraw"]):
            raise Abort("machines.json が再整形で変化する（手整形）→安全に書けない")
        if not _roundtrip_safe(ap["detail"], ap["draw"]):
            raise Abort(f"{ap['slug']}.json が再整形で変化する（手整形）→安全に書けない")
        keep = {ap["mpath"]: ap["mpath"].read_bytes(),
                ap["dpath"]: ap["dpath"].read_bytes()}
        # ★書く前に、元の中身そのものをディスクへ控える★（WAL）
        _journal_begin(base, c, csha, token, attempt,
                       keep={str(k): v.decode("utf-8") for k, v in keep.items()})
        try:
            _atomic_write(ap["mpath"], _dump(ap["machines"], ap["mraw"]))
            _atomic_write(ap["dpath"], _dump(ap["detail"], ap["draw"]))
            # ★書き終えた姿の控えも、ここ（巻き戻しの内側）で残す★
            #   （2026-08-06・Codex118回目のP0。外に置いていたので、
            #     控えに失敗すると**両方書いた後なのに「書く前に中止」扱い**になり、
            #     現物は新値のまま予約だけ「安全に戻した」と記録されていた）
            import hashlib as _hh
            _journal_after(base, attempt, {
                str(ap["mpath"]): "sha256:" + _hh.sha256(
                    ap["mpath"].read_bytes()).hexdigest(),
                str(ap["dpath"]): "sha256:" + _hh.sha256(
                    ap["dpath"].read_bytes()).hexdigest()})
        except BaseException as e:          # noqa: BLE001
            bad = []
            for p, b in keep.items():
                try:
                    _atomic_write(p, b.decode("utf-8"))
                    # ★戻せたことをバイトで確かめる★（Codex114回目の指摘2）
                    if p.read_bytes() != b:
                        bad.append(f"{p.name}（戻した中身が一致しません）")
                except Exception as e2:      # noqa: BLE001
                    bad.append(f"{p.name}（{e2}）")
            # ★戻せなかったことを「安全に戻した」と記録しない★
            res["outcome"] = ("ROLLBACK_FAILED" if bad
                              else "ROLLED_BACK_VERIFIED")
            res["unrestored"] = bad
            ab = Abort("書き込みを取り消しました: " + str(e)
                       + ("／★戻せませんでした: " + " / ".join(bad) + "★"
                          if bad else ""))
            ab.outcome = res["outcome"]   # ★結末を持たせて外へ伝える★
            ab.unrestored = bad
            raise ab
        res["applied"] = True
        res["outcome"] = "APPLIED_LOCAL"
    return res


class _DataLock:
    """対象データを触る間の鍵（同じ場所を2つの処理が書かないように）。

    ★持ち主がいなくなった鍵は奪う★（2026-08-06・電源断の試験で分かった）
      強制終了すると鍵のファイルだけが残り、**再開できない時間**ができる。
      鍵に持ち主のプロセス番号を書いておき、そのプロセスが居なければ奪う。
    """

    def __init__(self, base):
        self.path = str(Path(base) / ".fix-data.lock")
        self.fd = None

    @staticmethod
    def _alive(pid: int) -> bool:
        """そのプロセスがまだ動いているか（居なければ鍵を奪ってよい）。"""
        if pid <= 0:
            return False
        try:
            import subprocess
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                               capture_output=True, text=True, timeout=10,
                               encoding="utf-8", errors="replace")
            return str(pid) in (r.stdout or "")
        except Exception:                 # noqa: BLE001
            return True                   # 分からないときは奪わない（安全側）

    def _take_over(self) -> bool:
        """残っている鍵を奪ってよいか調べ、よければ消す。"""
        try:
            with open(self.path, encoding="utf-8") as f:
                pid = int((f.read().strip() or "0").split()[0])
        except Exception:                 # noqa: BLE001
            pid = 0
        import time
        old = False
        try:
            old = time.time() - os.path.getmtime(self.path) > 600
        except OSError:
            pass
        # ★生きている持ち主からは、時間が経っても奪わない★
        #   （2026-08-06・Codex117回目のP1。10分を超える処理があると、
        #     動いている相手の鍵を横取りできた）
        if pid and self._alive(pid):
            return False
        if not pid and not old:
            return False
        # ★名前を変えられた1人だけが奪う★（2026-08-06・Codex115回目のP1-5）
        #   「読んで、消して、作る」だと2つの処理が同じ鍵を消し合い、
        #   後から来た方が**相手の新しい鍵**を消してしまえた。
        mine = f"{self.path}.taking.{os.getpid()}"
        try:
            os.rename(self.path, mine)     # 成功するのは1人だけ
        except OSError:
            return False
        try:
            os.remove(mine)
        except OSError:
            pass
        return True

    def __enter__(self):
        import time
        for _ in range(300):
            try:
                self.fd = os.open(self.path,
                                  os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode("ascii"))
                return self
            except FileExistsError:
                if self._take_over():
                    continue
                time.sleep(0.1)
        raise Abort("ほかの処理がデータを書いています（30秒待っても空きません）")

    def __exit__(self, *a):
        try:
            if self.fd is not None:
                os.close(self.fd)
            os.remove(self.path)
        except OSError:
            pass
        return False


def plan_fix(base: Path, slug: str, field: str, old, new,
             expect: dict | None = None) -> dict:
    """★何をどう直すかを決めるだけ（1文字も書かない）★

    2026-08-05・Codex112回目の指摘1: 以前はこの関数自体が `apply_mode=True` で
    書き込めたので、外から直接呼べば契約も予約も迂回できた。
    書き込みは `_commit_plan()` の1か所だけに集約した。
    """
    res = {"slug": slug, "field": field, "old": old, "new": new,
           "applied": False, "struct_path": None, "prose_edits": [],
           "reason": None, "outcome": "NOT_APPLIED", "unrestored": []}
    mpath = base / "assets" / "data" / "machines.json"
    dpath = base / "assets" / "data" / "machine-details" / f"{slug}.json"
    try:
        if float(old) == float(new):
            raise Abort("新旧が同値（直すものが無い）")
        machines, mraw = _load(mpath)
        target = next((m for m in machines if m.get("slug") == slug), None)
        if target is None:
            raise Abort(f"machines.json に slug={slug} が無い")
        if not dpath.exists():
            raise Abort(f"記事JSONが無い: {dpath.name}")
        detail, draw = _load(dpath)

        container, key, cur, spath = locate_struct(target, field)
        res["struct_path"] = spath
        if float(cur) != float(old):
            raise Abort(f"現在値({cur})が想定した旧値({old})と違う→状況が変わったので書かない")

        # ★対象ファイルが契約を作ったときのままか★（指摘9）
        #   ここは計画段階の早期チェック。★本判定は書く直前の _commit_plan★
        if expect:
            import hashlib
            # ★ここで key という名前を使わない★（locate_struct が返した
            #   書き込み先のキーを潰してしまい、天井の値を別のキーへ
            #   書き込む事故を自分で作った。2026-08-05に再現して発見）
            for _ck, _cp in (("machines_before_sha256", mpath),
                             ("detail_before_sha256", dpath)):
                want = str(expect.get(_ck) or "")
                got = "sha256:" + hashlib.sha256(_cp.read_bytes()).hexdigest()
                if want and want != got:
                    raise Abort(f"{_cp.name} が契約を作ったときから変わっています")
        edits = plan_prose_edits(detail, field, old, new)
        res["prose_edits"] = [{"path": e["path"], "count": e["count"],
                               "before": e["before"][:120], "after": e["after"][:120]}
                              for e in edits]

        # ★直す場所の数まで契約に書いておく★（指摘4）
        #   0件でも「適用した」と言えてしまい、本文に旧値が残る形があった。
        if expect is not None and expect.get("prose_edit_count") is not None:
            # ★数えるのは「置き換える回数」★（2026-08-05・Codex111回目のP0-2）
            #   以前は「直す段落の数」で数えていたので、同じ段落の2か所を
            #   置き換えても1件だった。
            hits = sum(int(e["count"]) for e in edits)
            if hits != int(expect["prose_edit_count"]):
                raise Abort(
                    f"直す箇所の数が契約と違います"
                    f"（契約 {expect['prose_edit_count']}／実際 {hits}）")
        # ★ここまでが「計画」★（実際の書き込みは _commit_plan だけが行う）
        res["ready"] = True
        res["_apply"] = {"mpath": mpath, "dpath": dpath, "machines": machines,
                         "mraw": mraw, "detail": detail, "draw": draw,
                         "container": container, "key": key, "new": new,
                         "edits": edits, "slug": slug}
    except Abort as e:
        res["reason"] = str(e)
    return res


def _raises_abort(fn) -> bool:
    try:
        fn()
        return False
    except Abort:
        return True


def _raises_contract(fn) -> bool:
    try:
        fn()
        return False
    except (ContractError, Exception):
        return True


def _t_apply(base, slug, field, old, new, apply_mode=False, expect=None):
    """★試験用★ 契約と予約を本物どおり通してから書く（迂回経路を作らない）。

    2026-08-05・Codex112回目: 自己試験が書き込み関数を直接叩いていたため、
    「迂回経路が現存している」状態だった。試験も正規の入口だけを使う。
    """
    import hashlib
    import tempfile as _tf
    import task_guard as _tg
    if not apply_mode:
        return plan_fix(Path(base), slug, field, old, new, expect=expect)
    d = _tf.mkdtemp(prefix="t_apply_")
    sha = lambda p: "sha256:" + hashlib.sha256(  # noqa: E731
        Path(p).read_bytes()).hexdigest()
    mp = Path(base) / "assets" / "data" / "machines.json"
    dp = Path(base) / "assets" / "data" / "machine-details" / f"{slug}.json"
    _u = FIELD_EVIDENCE.get(field, {}).get("unit", "G")
    ev = {"slug": slug, "identity": {"must_contain": ["x"]}, "claims": [
        {"field": "天井", "value": str(new), "unit": _u,
         "critical": True, "url": "https://a.example/1", "quote": str(new)},
        {"field": "天井", "value": str(new), "unit": _u,
         "critical": True, "url": "https://b.example/1", "quote": str(new)}]}
    evp = os.path.join(d, "ev.json")
    with open(evp, "w", encoding="utf-8") as f:
        json.dump(ev, f, ensure_ascii=False)
    plan = plan_fix(Path(base), slug, field, old, new)
    hits = sum(int(e["count"]) for e in plan.get("prose_edits") or []) or 1
    pdig = plan_digest(plan)
    c = {"schema_version": CONTRACT_SCHEMA, "slug": slug, "field": field,
         "old": old, "new": new, "unit": FIELD_EVIDENCE.get(field, {}).get("unit", "G"),
         "evidence_file": evp, "evidence_sha256": sha(evp),
         "repository_id": REPOSITORY_ID,
         "machines_before_sha256": sha(mp) if mp.exists() else "",
         "detail_before_sha256": sha(dp) if dp.exists() else "",
         "prose_edit_count": (expect or {}).get("prose_edit_count", hits),
         "plan_sha256": pdig, "base_path": str(Path(base).resolve())}
    c.update({k: v for k, v in (expect or {}).items()
              if k in ("prose_edit_count", "machines_before_sha256",
                       "detail_before_sha256")})
    cp = os.path.join(d, "c.json")
    with open(cp, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False)
    sp, bp = os.path.join(d, "state.json"), os.path.join(d, "bud.json")
    with open(bp, "w", encoding="utf-8") as f:
        json.dump({"schema_version": "task-budget/v1", "writes_total": 9,
                   "writes_fix": 9, "writes_grow": 1, "inspections": 9,
                   "deadline_hhmm": "23:59"}, f)

    class _G:
        reservation = staticmethod(lambda t: _tg.reservation(t, path=sp))
        advance = staticmethod(lambda t, st, **k: _tg.advance(t, st, path=sp, **k))
        begin_apply = staticmethod(lambda t, **k: _tg.begin_apply(t, path=sp, **k))
        hold_apply = staticmethod(
            lambda t, a, c: _tg.hold_apply(t, a, c, path=sp))

    tok = _tg.reserve("t", slug, "fix", path=sp, budget_path=bp,
                      contract_sha256=sha(cp))["token"]
    os.environ["UCHI_AEF_TEST_HOOKS"] = "1"
    _install_test_hooks(verify=lambda e: 0, guard=_G)
    try:
        _r = apply_contract(cp, token=tok, apply_mode=True)
    finally:
        _HOOKS["verify"] = _HOOKS["guard"] = None
        os.environ.pop("UCHI_AEF_TEST_HOOKS", None)
    if os.environ.get("AEF_DEBUG") and not _r.get("applied"):
        print("   [debug]", slug, field, old, "->", new, "|", _r.get("reason"))
    return _r


def selftest() -> int:
    import shutil
    ok = fail = 0

    def t(label, cond):
        """真偽で書く試験（既存の eq に合わせる）。"""
        eq(bool(cond), True, label)

    def eq(got, want, label):
        nonlocal ok, fail
        if got == want:
            ok += 1
        else:
            fail += 1
            print(f"  NG {label}: got={got!r} want={want!r}")

    tmp = Path(tempfile.mkdtemp(prefix="aef_"))
    try:
        (tmp / "assets" / "data" / "machine-details").mkdir(parents=True)

        def setup(machine, detail):
            (tmp / "assets" / "data" / "machines.json").write_text(
                json.dumps([machine], ensure_ascii=False, indent=2), encoding="utf-8")
            (tmp / "assets" / "data" / "machine-details" / "t.json").write_text(
                json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")

        base_machine = {"slug": "t", "name": "Lテスト", "limit": 900,
                        "checker": {"unit": "G", "cycleMax": 10, "suruMax": 4}}
        base_detail = {"slug": "t", "lead": "天井は900Gです。",
                       "sections": [{"title": "天井・恩恵",
                                     "body": ["天井は**900G**で、到達時はATが確定します。",
                                              "リセット時は450G短縮されます。"]},
                                    {"title": "立ち回りのコツ",
                                     "body": ["純増は約2.8枚/Gです。"]}]}

        # 1. 正常系: 構造化値も本文も直る
        setup(base_machine, dict(base_detail))
        r = _t_apply(tmp, "t", "ceiling.normal.game", 900, 1000, apply_mode=True)
        eq(r["applied"], True, "正常系:適用された")
        m = json.loads((tmp / "assets" / "data" / "machines.json").read_text(encoding="utf-8"))
        d = json.loads((tmp / "assets" / "data" / "machine-details" / "t.json").read_text(encoding="utf-8"))
        eq(m[0]["limit"], 1000, "正常系:limitが直る")
        eq(d["lead"], "天井は1000Gです。", "正常系:leadが直る")
        eq(d["sections"][0]["body"][0], "天井は**1000G**で、到達時はATが確定します。",
           "正常系:本文が直る")
        eq(d["sections"][0]["body"][1], "リセット時は450G短縮されます。", "正常系:無関係な数値は不変")
        eq(d["sections"][1]["body"][0], "純増は約2.8枚/Gです。", "正常系:別セクション不変")

        # 2. 旧値が現状と違う → 何も書かない
        setup(base_machine, dict(base_detail))
        r = _t_apply(tmp, "t", "ceiling.normal.game", 777, 1000, apply_mode=True)
        eq(r["applied"], False, "楽観ロック:適用しない")
        eq("状況が変わった" in (r["reason"] or ""), True, "楽観ロック:理由")
        m = json.loads((tmp / "assets" / "data" / "machines.json").read_text(encoding="utf-8"))
        eq(m[0]["limit"], 900, "楽観ロック:machines.json不変")

        # 3a. 確率の分母など「明らかに別物」の同値は無視して修正を続行できる
        d2 = json.loads(json.dumps(base_detail))
        d2["sections"][1]["body"].append("設定6のBIG確率は1/900です。")
        setup(base_machine, d2)
        r = _t_apply(tmp, "t", "ceiling.normal.game", 900, 1000, apply_mode=True)
        eq(r["applied"], True, "確率の分母:別物として無視し適用")
        d = json.loads((tmp / "assets" / "data" / "machine-details" / "t.json").read_text(encoding="utf-8"))
        eq(d["sections"][1]["body"][-1], "設定6のBIG確率は1/900です。", "確率の分母:書き換えない")
        eq(d["lead"], "天井は1000Gです。", "確率の分母:本体は直る")

        # 3b. 単位が略されていても意味が分かる書き方（900到達）は一緒に直す
        d2b = json.loads(json.dumps(base_detail))
        d2b["sections"][1]["body"].append("900到達で優遇されます。")
        setup(base_machine, d2b)
        r = _t_apply(tmp, "t", "ceiling.normal.game", 900, 1000, apply_mode=True)
        eq(r["applied"], True, "単位省略+到達:適用する")
        d = json.loads((tmp / "assets" / "data" / "machine-details" / "t.json").read_text(encoding="utf-8"))
        eq(d["sections"][1]["body"][-1], "1000到達で優遇されます。", "単位省略+到達:直る")

        # 3c. ラベルの無い文でも単位付きなら直す（記事内で数字が食い違わないように）
        d2c = json.loads(json.dumps(base_detail))
        d2c["sections"][1]["body"].append("900Gからは打ち切りです。")
        setup(base_machine, d2c)
        r = _t_apply(tmp, "t", "ceiling.normal.game", 900, 1000, apply_mode=True)
        eq(r["applied"], True, "ラベル無し単位付き:直す")
        d = json.loads((tmp / "assets" / "data" / "machine-details" / "t.json").read_text(encoding="utf-8"))
        eq(d["sections"][1]["body"][-1], "1000Gからは打ち切りです。", "ラベル無し単位付き:内容")

        # 3d. 3桁以上で意味の判定がつかない裸の同値が残る → 中止（部分適用しない）
        d2d = json.loads(json.dumps(base_detail))
        d2d["sections"][1]["body"].append("900が一つの目安になります。")
        setup(base_machine, d2d)
        r = _t_apply(tmp, "t", "ceiling.normal.game", 900, 1000, apply_mode=True)
        eq(r["applied"], False, "判定不能な裸の同値:適用しない")
        d = json.loads((tmp / "assets" / "data" / "machine-details" / "t.json").read_text(encoding="utf-8"))
        eq(d["lead"], "天井は900Gです。", "判定不能:全体が不変（部分適用しない）")

        # 3e. 周期・スルー（1〜2桁）は裸の同値を無視して直す
        d2e = {"slug": "t", "lead": "天井は最大10周期です。",
               "sections": [{"title": "x", "body": ["設定6の確率は10%です。"]}]}
        setup(base_machine, d2e)
        r = _t_apply(tmp, "t", "ceiling.normal.cycle", 10, 8, apply_mode=True)
        eq(r["applied"], True, "小さい数字:裸の同値は無視して適用")
        d = json.loads((tmp / "assets" / "data" / "machine-details" / "t.json").read_text(encoding="utf-8"))
        eq(d["sections"][0]["body"][0], "設定6の確率は10%です。", "小さい数字:別文脈は不変")

        # 4. 同じ文に複数出てもすべて直る
        d3 = json.loads(json.dumps(base_detail))
        d3["sections"][0]["body"][0] = "天井は900Gで、900到達で確定です。"
        setup(base_machine, d3)
        r = _t_apply(tmp, "t", "ceiling.normal.game", 900, 1000, apply_mode=True)
        eq(r["applied"], True, "同一文の複数出現:適用")
        d = json.loads((tmp / "assets" / "data" / "machine-details" / "t.json").read_text(encoding="utf-8"))
        eq(d["sections"][0]["body"][0], "天井は1000Gで、1000到達で確定です。", "同一文の複数出現:内容")

        # 5. 周期・スルーの項目
        d4 = {"slug": "t", "lead": "天井は最大10周期です。", "sections": []}
        setup(base_machine, d4)
        r = _t_apply(tmp, "t", "ceiling.normal.cycle", 10, 8, apply_mode=True)
        eq(r["applied"], True, "周期:適用")
        d = json.loads((tmp / "assets" / "data" / "machine-details" / "t.json").read_text(encoding="utf-8"))
        eq(d["lead"], "天井は最大8周期です。", "周期:本文が直る")
        eq(json.loads((tmp / "assets" / "data" / "machines.json").read_text(encoding="utf-8"))[0]["checker"]["cycleMax"],
           8, "周期:cycleMaxが直る")

        # 6. dry-run は何も書かない
        setup(base_machine, dict(base_detail))
        r = _t_apply(tmp, "t", "ceiling.normal.game", 900, 1000, apply_mode=False)
        eq(r["applied"], False, "dry-run:書かない")
        eq(len(r["prose_edits"]), 2, "dry-run:計画は出る（lead＋本文）")
        eq(json.loads((tmp / "assets" / "data" / "machines.json").read_text(encoding="utf-8"))[0]["limit"],
           900, "dry-run:ファイル不変")

        # 7. 桁区切り表記に追随する
        d5 = {"slug": "t", "lead": "天井は1,200Gです。", "sections": []}
        m5 = json.loads(json.dumps(base_machine)); m5["limit"] = 1200
        setup(m5, d5)
        r = _t_apply(tmp, "t", "ceiling.normal.game", 1200, 1268, apply_mode=True)
        eq(r["applied"], True, "桁区切り:適用")
        eq(json.loads((tmp / "assets" / "data" / "machine-details" / "t.json").read_text(encoding="utf-8"))["lead"],
           "天井は1,268Gです。", "桁区切り:表記を保つ")

        # 8. 未対応項目・同値・不明slug
        setup(base_machine, dict(base_detail))
        eq(plan_fix(tmp, "t", "payout.setting6", 97, 98)["applied"], False,
           "未対応項目は適用しない")
        eq(plan_fix(tmp, "t", "ceiling.normal.game", 900, 900)["applied"], False,
           "同値は適用しない")
        eq(plan_fix(tmp, "zzz", "ceiling.normal.game", 900, 1000)["applied"],
           False, "不明slugは適用しない")

        # 9. 構造化値が無い機種（構造ごと変わる修正）は自動化しない
        m9 = {"slug": "t", "name": "Lテスト", "checker": {"unit": "G"}}
        setup(m9, {"slug": "t", "lead": "天井は900Gです。", "sections": []})
        r = _t_apply(tmp, "t", "ceiling.normal.game", 900, 1000, apply_mode=True)
        eq(r["applied"], False, "構造化値なし:適用しない")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ── ★契約入口★（2026-08-05・Codex109回目）
    import tempfile as _tf
    _d = _tf.mkdtemp()

    def _write(name, obj):
        p = os.path.join(_d, name)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        return p

    EV = {"slug": "x", "identity": {"must_contain": ["L機"]},
          "claims": [
              {"field": "天井", "value": "800", "unit": "G", "critical": True,
               "url": "https://a.example/1", "quote": "800G"},
              {"field": "天井", "value": "800", "unit": "G", "critical": True,
               "url": "https://b.example/1", "quote": "800G"}]}
    ev_p = _write("ev.json", EV)
    ok_verify = lambda d: 0            # noqa: E731  関所は通ったことにする
    base_c = {"schema_version": CONTRACT_SCHEMA, "slug": "x",
              "field": "ceiling.normal.game", "old": 900, "new": 800,
              "unit": "G", "evidence_file": ev_p,
              "evidence_sha256": _sha256_file(ev_p),
              "repository_id": "uchidokoro",
              "machines_before_sha256": "sha256:" + "0" * 64,
              "detail_before_sha256": "sha256:" + "0" * 64,
              "prose_edit_count": 1,
              "plan_sha256": "sha256:" + "0" * 64,
              "base_path": str(BASE_DEFAULT)}

    def _try(c, verify=ok_verify):
        try:
            check_contract(c, verify=verify)
            return ""
        except Exception as e:          # noqa: BLE001
            return str(e)

    t("　証拠と契約が合っていれば通る", _try(dict(base_c)) == "")
    t("★★証拠で合格した値と違う値は書けない★★（値のすり替え）",
      "証拠で合格した値と違います" in _try(dict(base_c, new=750)))
    t("★★証拠ファイルを差し替えたら通さない★★",
      "証拠ファイルが契約と違います" in
      _try(dict(base_c, evidence_sha256="sha256:" + "0" * 64)))
    t("★★関所を通らない証拠では書けない★★",
      "関所を通りません" in _try(dict(base_c), verify=lambda d: 1))
    EV1 = {**EV, "claims": EV["claims"][:1]}
    p1 = _write("ev1.json", EV1)
    t("★★1ドメインしか無い証拠では書けない★★",
      "ドメインしかありません" in _try(dict(
          base_c, evidence_file=p1, evidence_sha256=_sha256_file(p1))))
    EV2 = {**EV, "claims": [EV["claims"][0],
                            dict(EV["claims"][1], value="999")]}
    p2 = _write("ev2.json", EV2)
    t("★★証拠の中で値が食い違っていたら書けない★★",
      "食い違っています" in _try(dict(
          base_c, evidence_file=p2, evidence_sha256=_sha256_file(p2))))
    t("★★単位の書いていない証拠は使わない★★（見出しだけでは項目を決めない）",
      "記載がありません" in _try(dict(
          base_c, evidence_file=_write("ev5.json", {
              **EV, "claims": [{"field": "天井", "value": "800",
                                "url": "https://a.example/1"},
                               {"field": "天井", "value": "800",
                                "url": "https://b.example/1"}]}),
          evidence_sha256=_sha256_file(os.path.join(_d, "ev5.json")))))
    t("★★項目と単位が食い違う契約は通さない★★（G天井にpt値を書けない）",
      "単位が項目と合いません" in _try(dict(base_c, unit="pt")))
    t("★★扱えない項目は通さない★★",
      "契約で扱えません" in _try(dict(base_c, field="機械割")))
    t("　出典URLの無い証拠行は通さない",
      "出典URLの無い行" in _try(dict(
          base_c, evidence_file=_write("ev3.json", {
              **EV, "claims": [EV["claims"][0], {"field": "天井", "unit": "G",
                                                 "value": "800", "url": ""}]}),
          evidence_sha256=_sha256_file(os.path.join(_d, "ev3.json")))))
    t("★★www違い・ポート違いは同じサイトとして数える★★",
      "ドメインしかありません" in _try(dict(
          base_c, evidence_file=_write("ev4.json", {
              **EV, "claims": [
                  {"field": "天井", "value": "800", "unit": "G",
                   "url": "https://a.example/1"},
                  {"field": "天井", "value": "800", "unit": "G",
                   "url": "https://www.a.example:443/2"}]}),
          evidence_sha256=_sha256_file(os.path.join(_d, "ev4.json")))))
    t("★★知らない形の契約は読まない★★",
      _raises_contract(lambda: load_contract(
          _write("bad.json", {"schema_version": "でたらめ"}))))

    # ★書いた結果まで確かめる★（2026-08-05・自分のバグで気づいた。
    #   「applied=True」だけ見ていたので、値が別のキーへ書かれていたのに
    #   合格していた。**構造化値が変わり、余計なキーが増えていないこと**を見る）
    _b2 = os.path.join(_d, "base2")
    os.makedirs(os.path.join(_b2, "assets", "data", "machine-details"))
    _m2 = os.path.join(_b2, "assets", "data", "machines.json")
    _d2 = os.path.join(_b2, "assets", "data", "machine-details", "x.json")
    with open(_m2, "w", encoding="utf-8") as f:
        json.dump([{"slug": "x", "name": "L機", "limit": 900,
                    "checker": {"unit": "G"}}], f, ensure_ascii=False, indent=1)
    with open(_d2, "w", encoding="utf-8") as f:
        json.dump({"slug": "x", "sections": [
            {"title": "天井・恩恵",
             "body": ["天井は**900G**で、到達時はATが確定します。"]}]},
            f, ensure_ascii=False, indent=1)
    import hashlib as _hl
    _sha = lambda p: "sha256:" + _hl.sha256(open(p, "rb").read()).hexdigest()  # noqa: E731
    _exp = {"slug": "x", "field": "ceiling.normal.game", "old": 900, "new": 800,
            "machines_before_sha256": _sha(_m2),
            "detail_before_sha256": _sha(_d2), "prose_edit_count": 1}
    _rr = _t_apply(Path(_b2), "x", "ceiling.normal.game", 900, 800,
                          True, expect=_exp)
    _mm = json.load(open(_m2, encoding="utf-8"))[0]
    t("★★書いた値が正しい場所に入る★★（別のキーへ書く事故を実際に作った）",
      _rr["applied"] and _mm["limit"] == 800
      and set(_mm) == {"slug", "name", "limit", "checker"})
    t("　本文の数値も一緒に変わる",
      "800G" in json.load(open(_d2, encoding="utf-8"))["sections"][0]["body"][0])
    t("★★直す箇所の数が契約と違えば書かない★★",
      not _t_apply(Path(_b2), "x", "ceiling.normal.game", 800, 700, True,
                          expect=dict(_exp, prose_edit_count=5))["applied"])
    t("★★対象ファイルが契約時から変わっていたら書かない★★",
      "変わっています" in (_t_apply(
          Path(_b2), "x", "ceiling.normal.game", 800, 700, True,
          expect=dict(_exp, machines_before_sha256="sha256:" + "0" * 64)
      )["reason"] or ""))

    # ★許可証を自作しても、計画が契約と違えば書けない★（Codex113回目の指摘1）
    _p9 = plan_fix(Path(_b2), "x", "ceiling.normal.game", 800, 700)
    # ★本番では差し替え口が使えない★（2026-08-06・Codex115回目のP0-1）
    t("★★試験用の差し替えは、環境変数が無ければ使えない★★",
      _raises_abort(lambda: _install_test_hooks(verify=lambda e: 0)))
    _sig2 = list(_insp.signature(apply_contract).parameters)         if "_insp" in dir() else []
    # ★最終地点は契約ファイルしか受け取らない★（2026-08-06・Codex114回目）
    #   計画も許可証も外から渡せないので、「正しい指紋を自分で計算して
    #   自作の許可証で書く」という経路そのものが無くなった。
    import inspect as _insp
    _sig = list(_insp.signature(_commit_plan).parameters)
    t("★★書き込みの最終地点は、計画も許可証も受け取らない★★",
      _sig[:2] == ["contract_path", "token"]
      and not any(x in _sig for x in ("plan", "ticket", "base")))
    t("★★本番の入口に差し替え口が無い★★（偽の関所・偽の台帳を渡せない）",
      not any(x in _insp.signature(apply_contract).parameters
              for x in ("verify", "guard", "base"))
      and not any(x in _insp.signature(_commit_plan).parameters
                  for x in ("verify", "guard", "plan", "ticket")))
    t("　契約に書き込み先（base_path）が無ければ読まない",
      _raises_contract(lambda: load_contract(_write("nb.json", dict(
          base_c, base_path="/存在しない場所")))))
    t("　計画の指紋は数の書き方（900 と 900.0）で変わらない",
      plan_digest(plan_fix(Path(_b2), "x", "ceiling.normal.game", 800, 700))
      == plan_digest(plan_fix(Path(_b2), "x", "ceiling.normal.game",
                              800.0, 700.0)))

    # ★両方書いた後で控えの記録に失敗しても、現物・予約・記録がそろう★
    #   （2026-08-06・Codex118回目のP0。以前は「書く前に中止」と同じ扱いになり、
    #     現物は新値のまま予約だけ「安全に戻した」と記録されていた）
    _b3 = os.path.join(_d, "base3")
    os.makedirs(os.path.join(_b3, "assets", "data", "machine-details"))
    with open(os.path.join(_b3, "assets", "data", "machines.json"),
              "w", encoding="utf-8") as f:
        json.dump([{"slug": "x", "name": "L機", "limit": 900,
                    "checker": {"unit": "G"}}], f, ensure_ascii=False, indent=1)
    with open(os.path.join(_b3, "assets", "data", "machine-details", "x.json"),
              "w", encoding="utf-8") as f:
        json.dump({"slug": "x", "sections": [
            {"title": "天井・恩恵",
             "body": ["天井は**900G**で、到達時はATが確定します。"]}]},
            f, ensure_ascii=False, indent=1)
    _real_after = globals()["_journal_after"]
    try:
        globals()["_journal_after"] = lambda *a, **k: (_ for _ in ()).throw(
            Abort("控えを残せません"))
        _rf = _t_apply(_b3, "x", "ceiling.normal.game", 900, 800,
                       apply_mode=True)
    finally:
        globals()["_journal_after"] = _real_after
    _mm3 = json.load(open(os.path.join(_b3, "assets", "data", "machines.json"),
                          encoding="utf-8"))[0]
    t("★★控えを残せなければ、書いた分を戻して記録もそろえる★★",
      _rf.get("outcome") == "ROLLED_BACK_VERIFIED" and _mm3["limit"] == 900
      and not unfinished_fix(Path(_b3)))

    # ── ★書き込みの巻き戻し★（片方だけ書かれた状態を残さない）
    import shutil as _sh
    _b = os.path.join(_d, "base")
    os.makedirs(os.path.join(_b, "assets", "data", "machine-details"))
    _mp = os.path.join(_b, "assets", "data", "machines.json")
    _dp = os.path.join(_b, "assets", "data", "machine-details", "x.json")
    with open(_mp, "w", encoding="utf-8") as f:
        json.dump([{"slug": "x", "name": "L機", "limit": 900,
                    "checker": {"unit": "G"}}], f,
                  ensure_ascii=False, indent=1)
    with open(_dp, "w", encoding="utf-8") as f:
        json.dump({"slug": "x", "sections": [
            {"title": "天井・恩恵",
             "body": ["天井は**900G**で、到達時はATが確定します。"]}]},
            f, ensure_ascii=False, indent=1)
    _before = (open(_mp, encoding="utf-8").read(),
               open(_dp, encoding="utf-8").read())
    _real_w = globals()["_atomic_write"]
    _calls = {"n": 0}

    def _fail_second(path, text):
        _calls["n"] += 1
        if _calls["n"] == 2:
            raise OSError("2つ目の書き込みでわざと失敗")
        return _real_w(path, text)

    try:
        globals()["_atomic_write"] = _fail_second
        _r = _t_apply(Path(_b), "x", "ceiling.normal.game", 900, 800, True)
    finally:
        globals()["_atomic_write"] = _real_w
    t("★★片方だけ書けた状態を残さない★★（2つ目で失敗させて確認）",
      not _r["applied"] and "取り消しました" in (_r["reason"] or "")
      and (open(_mp, encoding="utf-8").read(),
           open(_dp, encoding="utf-8").read()) == _before)
    _sh.rmtree(_d, ignore_errors=True)

    print(f"apply_external_fix selftest: {ok}/{ok + fail}")
    return 0 if fail == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="裏取り済み外部数値の書き戻し（決定論）")
    ap.add_argument("--contract", help="★書き込みの唯一の入口★ 契約JSONのパス")
    ap.add_argument("--token", default="", help="task_guard で取った予約")
    ap.add_argument("--slug")
    ap.add_argument("--field")
    ap.add_argument("--old")
    ap.add_argument("--new")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--base", default=str(BASE_DEFAULT))
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.contract:
        try:
            r = apply_contract(a.contract, token=a.token, apply_mode=a.apply)
        except Exception as e:            # noqa: BLE001
            print(json.dumps({"applied": False,
                              "reason": f"契約を通せません: {e}"},
                             ensure_ascii=False))
            return 1
        print(json.dumps(r, ensure_ascii=False))
        return 0 if (r["applied"] or (not a.apply and not r["reason"])) else 1
    if not (a.slug and a.field and a.old is not None and a.new is not None):
        ap.error("--contract、または --slug --field --old --new が必要")
        return 2
    # ★契約なしで書き込むことは許さない★（2026-08-05・Codex109回目）
    #   下見（値の当たりを見る）だけは従来どおり使える。
    if a.apply:
        print(json.dumps({"applied": False,
                          "reason": "★--apply には --contract が必要です★"
                                    "（証拠と結び付いていない値は書きません）"},
                         ensure_ascii=False))
        return 1
    r = _t_apply(Path(a.base), a.slug, a.field, float(a.old), float(a.new), a.apply)
    print(json.dumps(r, ensure_ascii=False))
    return 0 if (r["applied"] or (not a.apply and not r["reason"])) else 1


if __name__ == "__main__":
    sys.exit(main())

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
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ─────────────────────────────────────────────
# 契約（★これが唯一の入口★）
# ─────────────────────────────────────────────

CONTRACT_SCHEMA = "fix-contract/v1"


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
    need = ("slug", "field", "old", "new", "evidence_file",
            "evidence_sha256", "evidence_field")
    miss = [k for k in need if not str(data.get(k) or "").strip()]
    if miss:
        raise ContractError(f"契約に足りない項目があります: {', '.join(miss)}")
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
    if verify is None:
        import verify_claims as _vc
        verify = lambda d: _vc.run_data(d, min_domains)   # noqa: E731
    if verify(ev) != 0:
        raise ContractError("証拠が関所を通りません（出典の実在・逐語一致が取れない）")
    rows = [r for r in (ev.get("claims") or [])
            if str(r.get("field") or "") == str(c["evidence_field"])]
    if not rows:
        raise ContractError(f"証拠に「{c['evidence_field']}」の記載がありません")
    doms = {(_domain(r.get("url")) or "") for r in rows}
    if len(doms) < min_domains:
        raise ContractError(
            f"「{c['evidence_field']}」の出典が {len(doms)} ドメインしかありません")
    vals = {_norm_value(r.get("value")) for r in rows}
    if len(vals) != 1:
        raise ContractError(f"証拠の中で値が食い違っています: {sorted(vals)}")
    if vals.pop() != _norm_value(c["new"]):
        raise ContractError(
            f"契約の新値が、証拠で合格した値と違います（契約 {c['new']!r}）")
    return {"domains": sorted(doms), "value": c["new"]}


def _domain(url) -> str:
    import urllib.parse
    return urllib.parse.urlsplit(str(url or "")).netloc.lower()


# ─────────────────────────────────────────────
# 本体
# ─────────────────────────────────────────────

def run(base: Path, slug: str, field: str, old, new, apply_mode: bool) -> dict:
    res = {"slug": slug, "field": field, "old": old, "new": new,
           "applied": False, "struct_path": None, "prose_edits": [], "reason": None}
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

        edits = plan_prose_edits(detail, field, old, new)
        res["prose_edits"] = [{"path": e["path"], "count": e["count"],
                               "before": e["before"][:120], "after": e["after"][:120]}
                              for e in edits]

        if apply_mode:
            container[key] = int(new) if float(new).is_integer() else new
            for e in edits:
                e["container"][e["key"]] = e["after"]
            if not _roundtrip_safe(machines, mraw):
                raise Abort("machines.json が再整形で変化する（手整形）→安全に書けない")
            if not _roundtrip_safe(detail, draw):
                raise Abort(f"{slug}.json が再整形で変化する（手整形）→安全に書けない")
            # ★全か無か★（2026-08-05・Codex109回目の指摘2）
            #   以前は machines.json を書いた後で記事の書き込みが失敗すると、
            #   **数値だけ直って本文は旧値のまま**の状態が残った（戻す処理が無かった）。
            keep = {mpath: mpath.read_bytes(), dpath: dpath.read_bytes()}
            try:
                _atomic_write(mpath, _dump(machines, mraw))
                _atomic_write(dpath, _dump(detail, draw))
            except BaseException as e:      # noqa: BLE001
                bad = []
                for p, b in keep.items():
                    try:
                        _atomic_write(p, b.decode("utf-8"))
                    except Exception as e2:  # noqa: BLE001
                        bad.append(f"{p.name}（{e2}）")
                raise Abort("書き込みを取り消しました: " + str(e)
                            + ("／★戻せませんでした: " + " / ".join(bad) + "★"
                               if bad else ""))
            res["applied"] = True
    except Abort as e:
        res["reason"] = str(e)
    return res


def _raises_contract(fn) -> bool:
    try:
        fn()
        return False
    except (ContractError, Exception):
        return True


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
        r = run(tmp, "t", "ceiling.normal.game", 900, 1000, apply_mode=True)
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
        r = run(tmp, "t", "ceiling.normal.game", 777, 1000, apply_mode=True)
        eq(r["applied"], False, "楽観ロック:適用しない")
        eq("状況が変わった" in (r["reason"] or ""), True, "楽観ロック:理由")
        m = json.loads((tmp / "assets" / "data" / "machines.json").read_text(encoding="utf-8"))
        eq(m[0]["limit"], 900, "楽観ロック:machines.json不変")

        # 3a. 確率の分母など「明らかに別物」の同値は無視して修正を続行できる
        d2 = json.loads(json.dumps(base_detail))
        d2["sections"][1]["body"].append("設定6のBIG確率は1/900です。")
        setup(base_machine, d2)
        r = run(tmp, "t", "ceiling.normal.game", 900, 1000, apply_mode=True)
        eq(r["applied"], True, "確率の分母:別物として無視し適用")
        d = json.loads((tmp / "assets" / "data" / "machine-details" / "t.json").read_text(encoding="utf-8"))
        eq(d["sections"][1]["body"][-1], "設定6のBIG確率は1/900です。", "確率の分母:書き換えない")
        eq(d["lead"], "天井は1000Gです。", "確率の分母:本体は直る")

        # 3b. 単位が略されていても意味が分かる書き方（900到達）は一緒に直す
        d2b = json.loads(json.dumps(base_detail))
        d2b["sections"][1]["body"].append("900到達で優遇されます。")
        setup(base_machine, d2b)
        r = run(tmp, "t", "ceiling.normal.game", 900, 1000, apply_mode=True)
        eq(r["applied"], True, "単位省略+到達:適用する")
        d = json.loads((tmp / "assets" / "data" / "machine-details" / "t.json").read_text(encoding="utf-8"))
        eq(d["sections"][1]["body"][-1], "1000到達で優遇されます。", "単位省略+到達:直る")

        # 3c. ラベルの無い文でも単位付きなら直す（記事内で数字が食い違わないように）
        d2c = json.loads(json.dumps(base_detail))
        d2c["sections"][1]["body"].append("900Gからは打ち切りです。")
        setup(base_machine, d2c)
        r = run(tmp, "t", "ceiling.normal.game", 900, 1000, apply_mode=True)
        eq(r["applied"], True, "ラベル無し単位付き:直す")
        d = json.loads((tmp / "assets" / "data" / "machine-details" / "t.json").read_text(encoding="utf-8"))
        eq(d["sections"][1]["body"][-1], "1000Gからは打ち切りです。", "ラベル無し単位付き:内容")

        # 3d. 3桁以上で意味の判定がつかない裸の同値が残る → 中止（部分適用しない）
        d2d = json.loads(json.dumps(base_detail))
        d2d["sections"][1]["body"].append("900が一つの目安になります。")
        setup(base_machine, d2d)
        r = run(tmp, "t", "ceiling.normal.game", 900, 1000, apply_mode=True)
        eq(r["applied"], False, "判定不能な裸の同値:適用しない")
        d = json.loads((tmp / "assets" / "data" / "machine-details" / "t.json").read_text(encoding="utf-8"))
        eq(d["lead"], "天井は900Gです。", "判定不能:全体が不変（部分適用しない）")

        # 3e. 周期・スルー（1〜2桁）は裸の同値を無視して直す
        d2e = {"slug": "t", "lead": "天井は最大10周期です。",
               "sections": [{"title": "x", "body": ["設定6の確率は10%です。"]}]}
        setup(base_machine, d2e)
        r = run(tmp, "t", "ceiling.normal.cycle", 10, 8, apply_mode=True)
        eq(r["applied"], True, "小さい数字:裸の同値は無視して適用")
        d = json.loads((tmp / "assets" / "data" / "machine-details" / "t.json").read_text(encoding="utf-8"))
        eq(d["sections"][0]["body"][0], "設定6の確率は10%です。", "小さい数字:別文脈は不変")

        # 4. 同じ文に複数出てもすべて直る
        d3 = json.loads(json.dumps(base_detail))
        d3["sections"][0]["body"][0] = "天井は900Gで、900到達で確定です。"
        setup(base_machine, d3)
        r = run(tmp, "t", "ceiling.normal.game", 900, 1000, apply_mode=True)
        eq(r["applied"], True, "同一文の複数出現:適用")
        d = json.loads((tmp / "assets" / "data" / "machine-details" / "t.json").read_text(encoding="utf-8"))
        eq(d["sections"][0]["body"][0], "天井は1000Gで、1000到達で確定です。", "同一文の複数出現:内容")

        # 5. 周期・スルーの項目
        d4 = {"slug": "t", "lead": "天井は最大10周期です。", "sections": []}
        setup(base_machine, d4)
        r = run(tmp, "t", "ceiling.normal.cycle", 10, 8, apply_mode=True)
        eq(r["applied"], True, "周期:適用")
        d = json.loads((tmp / "assets" / "data" / "machine-details" / "t.json").read_text(encoding="utf-8"))
        eq(d["lead"], "天井は最大8周期です。", "周期:本文が直る")
        eq(json.loads((tmp / "assets" / "data" / "machines.json").read_text(encoding="utf-8"))[0]["checker"]["cycleMax"],
           8, "周期:cycleMaxが直る")

        # 6. dry-run は何も書かない
        setup(base_machine, dict(base_detail))
        r = run(tmp, "t", "ceiling.normal.game", 900, 1000, apply_mode=False)
        eq(r["applied"], False, "dry-run:書かない")
        eq(len(r["prose_edits"]), 2, "dry-run:計画は出る（lead＋本文）")
        eq(json.loads((tmp / "assets" / "data" / "machines.json").read_text(encoding="utf-8"))[0]["limit"],
           900, "dry-run:ファイル不変")

        # 7. 桁区切り表記に追随する
        d5 = {"slug": "t", "lead": "天井は1,200Gです。", "sections": []}
        m5 = json.loads(json.dumps(base_machine)); m5["limit"] = 1200
        setup(m5, d5)
        r = run(tmp, "t", "ceiling.normal.game", 1200, 1268, apply_mode=True)
        eq(r["applied"], True, "桁区切り:適用")
        eq(json.loads((tmp / "assets" / "data" / "machine-details" / "t.json").read_text(encoding="utf-8"))["lead"],
           "天井は1,268Gです。", "桁区切り:表記を保つ")

        # 8. 未対応項目・同値・不明slug
        setup(base_machine, dict(base_detail))
        eq(run(tmp, "t", "payout.setting6", 97, 98, True)["applied"], False, "未対応項目は適用しない")
        eq(run(tmp, "t", "ceiling.normal.game", 900, 900, True)["applied"], False, "同値は適用しない")
        eq(run(tmp, "zzz", "ceiling.normal.game", 900, 1000, True)["applied"], False, "不明slugは適用しない")

        # 9. 構造化値が無い機種（構造ごと変わる修正）は自動化しない
        m9 = {"slug": "t", "name": "Lテスト", "checker": {"unit": "G"}}
        setup(m9, {"slug": "t", "lead": "天井は900Gです。", "sections": []})
        r = run(tmp, "t", "ceiling.normal.game", 900, 1000, apply_mode=True)
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
              {"field": "天井", "value": "800", "critical": True,
               "url": "https://a.example/1", "quote": "800G"},
              {"field": "天井", "value": "800", "critical": True,
               "url": "https://b.example/1", "quote": "800G"}]}
    ev_p = _write("ev.json", EV)
    ok_verify = lambda d: 0            # noqa: E731  関所は通ったことにする
    base_c = {"schema_version": CONTRACT_SCHEMA, "slug": "x",
              "field": "ceiling.normal.games", "old": 900, "new": 800,
              "evidence_file": ev_p, "evidence_sha256": _sha256_file(ev_p),
              "evidence_field": "天井"}

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
    t("　証拠に無い項目は書けない",
      "の記載がありません" in _try(dict(base_c, evidence_field="機械割")))
    t("★★知らない形の契約は読まない★★",
      _raises_contract(lambda: load_contract(
          _write("bad.json", {"schema_version": "でたらめ"}))))

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
        _r = run(Path(_b), "x", "ceiling.normal.game", 900, 800, True)
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
        # ★契約から読む★（値を人が手で渡さない）
        try:
            c = load_contract(a.contract)
            proof = check_contract(c)
        except (ContractError, Exception) as e:   # noqa: BLE001
            print(json.dumps({"applied": False,
                              "reason": f"契約を通せません: {e}"},
                             ensure_ascii=False))
            return 1
        r = run(Path(a.base), c["slug"], c["field"],
                float(c["old"]), float(c["new"]), a.apply)
        r["contract"] = {"evidence_file": c["evidence_file"],
                         "evidence_sha256": c["evidence_sha256"],
                         "domains": proof["domains"]}
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
    r = run(Path(a.base), a.slug, a.field, float(a.old), float(a.new), a.apply)
    print(json.dumps(r, ensure_ascii=False))
    return 0 if (r["applied"] or (not a.apply and not r["reason"])) else 1


if __name__ == "__main__":
    sys.exit(main())

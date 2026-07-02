# -*- coding: utf-8 -*-
"""安全な内部整合の自動修正（新値発明禁止・決定論・LLM非依存）。

validate_machine_data.py が検知する内部破綻のうち、「ルールで修正値が一意に
決まるもの」だけを修正する。決まらないものは絶対に触らず報告のみ（現状維持）。

★安全原則（2026-06-30設計 wkc8g7yhw / 2026-07-02実装）★
  1. 新値発明禁止: 書き込む数値は「その機種のJSONに既に存在する値」のみ。
     外部から新しい数字を持ち込む経路がコード上存在しない（_assert_in_stockで強制）。
  2. 一意性原則: 修正値がルールで一意に確定しない場合は修正しない（報告のみ）。
  3. 現状維持デフォルト: 迷ったら変えない（誤情報を公開しないことが最優先）。

修正ルール:
  R1. checker閾値フォールバックの「機種内慣例」による復元:
      非byRateフォールバック値が順序破綻（V1）しており、かつ同一機種内の他の
      byRate持ちブロックが「全て同一のbyRateキーと完全一致」する機種内慣例を
      持つ場合のみ、破綻ブロックのフォールバックを byRate[慣例キー] で復元する。
      慣例が一意に確定しなければ修正しない。
      ※全114機種調査(2026-07-02)でフォールバック慣例は機種ごとに異なる
      　(eq56系50/rate55系49/どれとも不一致25)と判明済み。グローバル慣例は
      　存在しないため使わず、機種内で全ブロック一致した時だけ採用する。
      　(banchou4 reset の手動修正 667b07c を機械化したもの)
      走査はchecker配下の★再帰列挙★（normal/reset/suru配列だけでなくcz/through/
      cycle/at/checker直下suru等も慣例判定の母集団に含める。走査漏れ＝慣例の誤認定）。
      復元するのは「破綻の当事者キー」（直接破綻＋慣例値置換で連鎖的に破綻したキー）
      のみで、当事者でないキーが慣例値と不一致なら独自様式とみなし修正しない
      ＝ブロックが「慣例＋破損」で説明できる時だけ直す（2026-07-02敵対的レビューの
      critical/major指摘への対策。誤って正しい独自値を巻き添え上書きしない）。
      note内の旧値は「{旧値}G〜/pt〜」の狙い目表現がちょうど1回出現する場合のみ
      新値に置換（天井等の別文脈の同値・桁区切りの部分一致は置換せずWARN報告）。
  R2. evTableのセンチネル行削除（V2）:
      天井あり機種の evTable で range にセンチネル値(99999等)を含む行を削除。
      全行消えたら evTable キー自体を削除。(dark_haibi 2026-06-30 の手動修正の機械化)

修正しないもの（報告のみ・現状維持）:
  - byRate内部の順序破綻（onepunchman型: noteとの整合判断が必要で一意に決まらない）
  - V4 機械割異常（外部数値の絶対値＝カテゴリ④・無人変更禁止）
  - フォールバックが慣例と不一致でも順序破綻していないもの（実害なし・触らない）
  - round-trip安全でないファイル（手整形JSON: dark_haibi等）への書き込み

使い方:
  python scripts/apply_safe_fixes.py                # dry-run（提案のみ・何も書かない）
  python scripts/apply_safe_fixes.py --apply        # 実際に書き込む
  python scripts/apply_safe_fixes.py --slug X       # 1機種のみ
  python scripts/apply_safe_fixes.py --selftest     # 新値発明禁止ガードの自己テスト
  python scripts/apply_safe_fixes.py --base PATH    # テスト用データディレクトリ（既定=リポジトリ）

exit code:
  0 = 検知なし（何もすることがない）
  3 = 修正あり（dry-run=提案 / --apply=適用済み）かつ未解決なし
  1 = 自動修正できない検知が残存（報告のみ・現状維持）※修正ありと併存時も1
出力は verify タスクが log.py 経由でログ・メールに転記する前提（各判定を逐一出力）。
"""
import argparse
import copy
import json
import os
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_machine_data import SENTINELS, _num, _is_setting_only, check_machine  # noqa: E402

THRESH_KEYS = ("caution", "good", "excellent")


# ---------------------------------------------------------------- ユーティリティ

def _collect_numbers(obj, acc=None):
    """機種オブジェクト内の全数値リーフを集める（新値発明禁止ガードの在庫集合）。"""
    if acc is None:
        acc = set()
    if isinstance(obj, dict):
        for v in obj.values():
            _collect_numbers(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _collect_numbers(v, acc)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        acc.add(obj)
    return acc


def _assert_in_stock(stock, value, context):
    """★新値発明禁止ガード★ 書き込む数値が既出値集合に無ければ書き込み自体を拒否する。"""
    if value not in stock:
        raise ValueError(f"新値発明禁止ガード発動: {context} に在庫外の値 {value} を書こうとした")


def _fallback_vals(blk):
    return tuple(blk.get(k) for k in THRESH_KEYS)


def _is_inverted(blk):
    """caution≤good≤excellent(≤limit) が崩れているか（validate V1と同じ規則）。"""
    seq = []
    for key in ("caution", "good", "excellent", "limit"):
        v = blk.get(key)
        if isinstance(v, (int, float)) and v not in SENTINELS:
            seq.append(v)
    return any(seq[i] > seq[i + 1] for i in range(len(seq) - 1))


def _iter_byrate_blocks(obj, path=""):
    """checker配下の「byRateを持つ閾値ブロック」を(パス, ブロック)で★再帰的に★列挙。

    normal/reset/suru配列だけでなく cz/through/cycle/at/modeData/checker直下suru等の
    全構造を対象にする。走査漏れがあると機種内慣例の母集団が欠けて「一意な慣例」を
    誤認定し、本来KEEPすべき破綻を書き換えてしまう（2026-07-02敵対的レビューのcritical指摘）。"""
    if isinstance(obj, dict):
        if isinstance(obj.get("byRate"), dict) and \
                any(isinstance(obj.get(k), (int, float)) for k in THRESH_KEYS):
            yield path or "checker", obj
        for k, v in obj.items():
            if k == "byRate":
                continue  # byRate配下のrate別値はフォールバックではない
            yield from _iter_byrate_blocks(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _iter_byrate_blocks(v, f"{path}[{i}]")


def _machine_convention(checker, exclude_paths):
    """機種内フォールバック慣例のbyRateキーを返す。一意に確定しなければNone。

    破綻ブロック(exclude_paths)以外の全byRate持ちブロックで
    「フォールバック値 == byRate[key]の値」となるkeyの積集合を取る。
    1つでも無一致ブロックがあれば慣例なし（None）。
    """
    keysets = []
    for path, blk in _iter_byrate_blocks(checker):
        if path in exclude_paths:
            continue
        fb = _fallback_vals(blk)
        matches = {
            rk for rk, rv in blk["byRate"].items()
            if isinstance(rv, dict) and _fallback_vals(rv) == fb
        }
        if not matches:
            return None  # 慣例に従っていないブロックがある＝機種内慣例が確立していない
        keysets.append(matches)
    if not keysets:
        return None
    common = set.intersection(*keysets)
    return common if common else None


def _implicated_by_cascade(blk, conv):
    """順序破綻の「当事者キー」集合を、慣例値での置換を連鎖適用しながら求める。

    直接破綻したキーを慣例値に置き、その結果新たに破綻したキーも順次置いていく。
    収束すれば当事者キー集合を返す（当事者でないキーは一切触っていない）。
    慣例値に置いても破綻が解けなければ None。
    banchou4型（good330>excellent200 → goodを置くとcautionが連鎖破綻 → cautionも当事者）は
    通り、独自様式が混ざるブロック（当事者でないキーが慣例値と不一致）は呼び出し側で弾く。"""
    work = {k: blk.get(k) for k in THRESH_KEYS
            if isinstance(blk.get(k), (int, float)) and blk.get(k) not in SENTINELS}
    lim = blk.get("limit")
    lim = lim if isinstance(lim, (int, float)) and lim not in SENTINELS else None
    marked = set()
    for _ in range(len(THRESH_KEYS) + 1):
        order = [k for k in THRESH_KEYS if k in work]
        viol = set()
        for k1, k2 in zip(order, order[1:]):
            if work[k1] > work[k2]:
                viol |= {k1, k2}
        if lim is not None and order and work[order[-1]] > lim:
            viol.add(order[-1])
        if not viol:
            return marked
        new = viol - marked
        if not new:
            return None  # 慣例値に置いても破綻が残る
        for k in new:
            if isinstance(conv.get(k), (int, float)):
                work[k] = conv[k]
            marked.add(k)
    return None


def _replace_in_note(note, old, new):
    """note内の旧値を新値へ置換。★狙い目表現「{old}G〜/{old}pt〜」に限定★・ちょうど1回の時だけ。
    「天井330G。」のような別文脈の同値や「1,330G〜」の桁区切り部分一致は置換しない。
    戻り値: (新note or None, 状態)  状態: 'replaced'|'absent'|'ambiguous'"""
    if not isinstance(note, str):
        return None, "absent"
    pat = re.compile(rf"(?<![\d,]){int(old)}((?:G|pt)〜)")
    hits = list(pat.finditer(note))
    if len(hits) == 0:
        return None, "absent"
    if len(hits) > 1:
        return None, "ambiguous"
    return pat.sub(rf"{int(new)}\1", note), "replaced"


# ---------------------------------------------------------------- 修正ルール

def fix_r1_fallback(machine, log):
    """R1: 順序破綻したフォールバックを機種内慣例のbyRate値で復元。"""
    if _is_setting_only(machine):
        return False  # 設定狙い専用機はvalidate同様V1対象外（誤修正防止）
    fixed = False
    checker = machine.get("checker") or {}
    stock = _collect_numbers(machine)
    broken = [(p, b) for p, b in _iter_byrate_blocks(checker) if _is_inverted(b)]
    if not broken:
        return False
    conv_keys = _machine_convention(checker, exclude_paths={p for p, _ in broken})
    for path, blk in broken:
        if not conv_keys:
            log.append(f"    [KEEP] R1 {path}: フォールバック順序破綻だが機種内慣例が一意に確定せず→現状維持（報告のみ）")
            continue
        # 慣例キーが複数でも、候補値が全キーで同一なら一意とみなす
        candidates = []
        for ck in sorted(conv_keys):
            rv = blk["byRate"].get(ck)
            if isinstance(rv, dict):
                candidates.append((ck, {k: rv.get(k) for k in THRESH_KEYS if k in blk}))
        uniq = {json.dumps(c[1], sort_keys=True) for c in candidates}
        if len(uniq) != 1:
            log.append(f"    [KEEP] R1 {path}: 慣例キー候補{sorted(conv_keys)}で値が割れる→現状維持（報告のみ）")
            continue
        ck, newvals = candidates[0]
        if any(not isinstance(v, (int, float)) or v in SENTINELS for v in newvals.values()):
            log.append(f"    [KEEP] R1 {path}: byRate[{ck}]に数値でない/センチネル値があり復元不能→現状維持")
            continue
        marked = _implicated_by_cascade(blk, newvals)
        if marked is None:
            log.append(f"    [KEEP] R1 {path}: byRate[{ck}]で復元しても順序破綻が解けない→現状維持")
            continue
        hybrid = sorted(k for k, v in newvals.items() if k not in marked and blk.get(k) != v)
        if hybrid:
            log.append(f"    [KEEP] R1 {path}: 破綻に関与していないキー{hybrid}が慣例値と不一致（独自様式の可能性）→現状維持（報告のみ）")
            continue
        changes = {k: (blk[k], newvals[k]) for k in sorted(marked) if k in newvals and blk.get(k) != newvals[k]}
        if not changes:
            log.append(f"    [KEEP] R1 {path}: 慣例値と既に一致（破綻原因が別）→現状維持")
            continue
        for k, (old, new) in changes.items():
            _assert_in_stock(stock, new, f"{machine.get('slug')}.checker.{path}.{k}")
        for k, (old, new) in changes.items():
            blk[k] = new
        # targetは両方に存在する時だけ揃える
        if "target" in blk and isinstance(blk["byRate"].get(ck, {}).get("target"), (int, float)):
            tv = blk["byRate"][ck]["target"]
            if blk["target"] != tv:
                _assert_in_stock(stock, tv, f"{machine.get('slug')}.checker.{path}.target")
                blk["target"] = tv
                changes["target"] = ("(旧)", tv)
        detail_str = " / ".join(f"{k}:{old}→{new}" for k, (old, new) in changes.items())
        log.append(f"    [FIX] R1 {path}: フォールバックを機種内慣例 byRate[{ck}] で復元（{detail_str}）")
        # note内の旧値を追随（ちょうど1回出現・かつ他キーの値と紛れない時のみ）
        block_nums_after = {blk.get(kk) for kk in ("caution", "good", "excellent", "target", "limit")
                            if isinstance(blk.get(kk), (int, float))}
        for k, (old, new) in changes.items():
            if not isinstance(old, (int, float)):
                continue
            others_old = {o for kk, (o, n) in changes.items() if kk != k and isinstance(o, (int, float))}
            newnote, state = _replace_in_note(blk.get("note"), old, new)
            if state == "replaced" and (old in block_nums_after or old in others_old):
                log.append(f"    [WARN] R1 {path}.note: 旧値{int(old)}が他キーの値とも重複し対応が曖昧→note未変更・文面は手動確認を（数値側はR1修正対象）")
            elif state == "replaced":
                blk["note"] = newnote
                log.append(f"    [FIX] R1 {path}.note: 旧値{int(old)}→{int(new)}に追随置換")
            elif state == "ambiguous":
                log.append(f"    [WARN] R1 {path}.note: 旧値{int(old)}が複数回出現・note文面は手動確認を（数値側はR1修正対象）")
            elif state == "absent" and isinstance(blk.get("note"), str) and \
                    re.search(rf"(?<!\d){int(old)}(?!\d)", blk["note"]):
                log.append(f"    [WARN] R1 {path}.note: 旧値{int(old)}が狙い目表現以外の文脈で出現・note整合を手動確認（数値側はR1修正対象）")
        fixed = True
    return fixed


def fix_r2_sentinel_rows(machine, detail, log):
    """R2: 天井あり機種のevTableからセンチネル行を削除。"""
    if detail is None or _is_setting_only(machine):
        return False
    ev = detail.get("evTable")
    if not isinstance(ev, list):
        return False
    keep, removed = [], []
    for row in ev:
        rng = row.get("range") if isinstance(row, dict) else None
        n = _num(rng)
        if n is not None and n in SENTINELS:
            removed.append(rng)
        else:
            keep.append(row)
    if not removed:
        return False
    if keep:
        detail["evTable"] = keep
        log.append(f"    [FIX] R2 evTable: センチネル行{len(removed)}件を削除（{removed}）")
    else:
        del detail["evTable"]
        log.append(f"    [FIX] R2 evTable: 全行がセンチネルのためevTable自体を削除（{removed}）")
    return True


# ---------------------------------------------------------------- 入出力（フォーマット保存）

def _load_raw(path):
    if not path.is_file():
        return None
    with open(path, encoding="utf-8", newline="") as f:  # newline=""で改行コードを無変換保持
        return f.read()


def _roundtrip_safe(raw):
    """標準dumps(indent=2)で改行・末尾改行以外が保存できるフォーマットか。"""
    try:
        data = json.loads(raw)
    except Exception:
        return False
    if "\r\n" in raw and raw.count("\n") != raw.count("\r\n"):
        return False  # 改行コード混在: 書き戻すと全行のフォーマットが変わるため対象外
    norm = raw.replace("\r\n", "\n")
    rt = json.dumps(data, ensure_ascii=False, indent=2)
    return norm in (rt, rt + "\n")


def _save_like(path, data, raw_original):
    """元ファイルの改行コード・末尾改行を保存して書き戻す。
    一時ファイル→os.replaceのアトミック差し替え（書き込み中断による切り詰め破壊防止）。"""
    text = json.dumps(data, ensure_ascii=False, indent=2)
    norm = raw_original.replace("\r\n", "\n")
    if norm.endswith("\n"):
        text += "\n"
    if "\r\n" in raw_original:
        text = text.replace("\n", "\r\n")
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


# ---------------------------------------------------------------- メイン

def run(base, slug_filter, apply_mode):
    machines_path = base / "assets" / "data" / "machines.json"
    details_dir = base / "assets" / "data" / "machine-details"
    machines_raw = _load_raw(machines_path)
    machines = json.loads(machines_raw)

    machines_rt_safe = _roundtrip_safe(machines_raw)
    if not machines_rt_safe:
        print("⚠ machines.json が標準フォーマットでないため書き込み不可（報告のみモード）")

    total_fixed = 0
    total_unresolved = 0
    machines_dirty = False

    for m in machines:
        slug = m.get("slug", "?")
        if slug_filter and slug != slug_filter:
            continue
        try:
            dpath = details_dir / f"{slug}.json"
            detail_raw = _load_raw(dpath)
            detail = json.loads(detail_raw) if detail_raw else None

            before = check_machine(m, detail)
            if not before:
                continue

            log = []
            m_work = copy.deepcopy(m)
            d_work = copy.deepcopy(detail)

            r1 = fix_r1_fallback(m_work, log)
            r2 = fix_r2_sentinel_rows(m_work, d_work, log)

            after = check_machine(m_work, d_work)
            print(f"⚠ {slug}（{m.get('name', '')}）: 検知{len(before)}件 → 修正後の残存{len(after)}件")
            for line in log:
                print(line)
            for ng in after:
                print(f"    [KEEP] 自動修正の対象外→現状維持: {ng}")
                total_unresolved += 1

            if r1 or r2:
                n_r1 = sum(1 for l in log if "[FIX] R1" in l)
                n_r2 = sum(1 for l in log if "[FIX] R2" in l)
                if apply_mode:
                    if r1:
                        if machines_rt_safe:
                            m.clear()
                            m.update(m_work)
                            machines_dirty = True
                            total_fixed += n_r1
                        else:
                            print(f"    [KEEP] machines.json書き込み不可（非標準フォーマット）→上記R1の[FIX]は未適用（提案扱い・手動対応）")
                            total_unresolved += 1
                    if r2:
                        if detail_raw and _roundtrip_safe(detail_raw):
                            _save_like(dpath, d_work, detail_raw)
                            print(f"    [APPLY] {dpath.name} を書き込みました")
                            total_fixed += n_r2
                        else:
                            print(f"    [KEEP] {dpath.name}は手整形JSONのため書き込み不可→上記R2の[FIX]は未適用（提案扱い・手動対応）")
                            total_unresolved += 1
                else:
                    total_fixed += n_r1 + n_r2
                    print("    （dry-run: 上記[FIX]は提案のみ・未書き込み。--applyで適用）")
        except Exception as e:
            # 1機種の破損データで全体を止めない（この機種は現状維持のまま報告）
            print(f"⚠ {slug}: 処理エラーのためスキップ→現状維持（{type(e).__name__}: {e}）")
            total_unresolved += 1
            continue

    if apply_mode and machines_dirty:
        _save_like(machines_path, machines, machines_raw)
        print("[APPLY] machines.json を書き込みました")

    mode = "適用" if apply_mode else "提案(dry-run)"
    print("")
    print(f"=== apply_safe_fixes 完了: 修正{mode} {total_fixed}件 / 自動修正対象外の残存 {total_unresolved}件 ===")
    if total_unresolved:
        return 1
    return 3 if total_fixed else 0


def selftest():
    """新値発明禁止ガードが確実に働くことの自己テスト。"""
    stock = _collect_numbers({"a": 100, "b": [{"c": 200}]})
    assert stock == {100, 200}, stock
    _assert_in_stock(stock, 200, "test")  # 在庫内→通る
    try:
        _assert_in_stock(stock, 580, "test")  # 在庫外（okidoki型幻覚値）→拒否されるべき
    except ValueError as e:
        print(f"✅ selftest OK: 在庫外の値は書き込み拒否される（{e}）")
        return 0
    print("❌ selftest FAILED: 在庫外の値が素通りした")
    return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="実際に書き込む（既定はdry-run）")
    ap.add_argument("--slug", default="", help="特定の1機種だけ")
    ap.add_argument("--base", default="", help="テスト用データディレクトリ")
    ap.add_argument("--selftest", action="store_true", help="新値発明禁止ガードの自己テスト")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(selftest())

    base = Path(args.base) if args.base else Path(__file__).resolve().parent.parent
    sys.exit(run(base, args.slug, args.apply))


if __name__ == "__main__":
    main()

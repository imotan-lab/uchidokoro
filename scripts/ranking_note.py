"""★ランキングの上位10機種に添える「恩恵の一言」を書く唯一の場所★（2026-11-07・運営者「すすめて」）

使い方:
  python scripts/ranking_note.py --apply <決定ファイル> [--dry-run]   … 2AIの決定を書く（WRITE / NO_NOTE）
  python scripts/ranking_note.py --check      … 一言と、その根拠の記録がそろっているか（番人・タスクが呼ぶ）
  python scripts/ranking_note.py --selftest

★なぜ要るか★＝朝一リセットランキング（検索から月400クリックほど）と天井ランキングは、
  狙い目のゲーム数しか並べておらず、「リセットされると何が良いのか」「天井で何が起きるか」が
  ページに無かった（運営者が週次レポートでランキングの検索語の多さに気づいた）。
★決めるのは2AI／機械が見るのは形と照合だけ★（鉄則0z）
  ・判断者の組（`confirmed_values.JUDGE_PAIRS`）と理由15字以上
  ・文の形（`text_problems`＝読者に出る前の検査。ハブの生成器も同じ関数を通す）
  ・★文の中の数値は、決定ファイルで名指しした確定値の記録に全部在ること★（数字を作らない）＝
    リセットは `reset*`、天井は `ceiling*` の記録だけ（`checker_ceiling` は恩恵を持たないので使わない）。
    ★自サイトの記事は根拠にしない★（記事と、その記事を作った値は独立した出典ではない）。
  ・★恩恵・条件の意味（通常時かリセット後か・CZ間かAT間か・+αの省略）は2AIが原文で確かめる★
    （機械は値の在処しか見られない＝Codex review_ranking_task の3）。
★置き場★
  一言 … assets/data/ranking-notes.json（リポジトリの中・読者に出る文だけ）
  根拠 … Documents/uchidokoro/ranking_basis.json（リポジトリの外・判断者・理由・名指しした記録の指紋）
  ★ハブの生成器は根拠を読まない★（CIに無い＝罠㊱）。根拠との照合は `--check`（このPCでだけ動く）。
"""
import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import local_paths as _lp  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOTES = os.path.join(BASE, "assets", "data", "ranking-notes.json")
MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
BASIS = _lp.doc("ranking_basis.json")
SCHEMA = "ranking-notes/v1"
RANKINGS = ("reset", "tenjo")
# ★どのランキングに、どの確定値の項目を名指しできるか★（`confirmed_values.base_field` で比べる）
RECORD_BASE = {"reset": "reset", "tenjo": "ceiling"}
DECISIONS = ("WRITE", "NO_NOTE")
MAX_LEN = 90
MIN_WHY = 15
_SLUG = re.compile(r"^[a-z0-9_]+$")
# ★です・ます で終わる文★（文末の括弧の補足は外して見る）
_ENDINGS = ("です", "ます", "ません", "でした", "ました")


class NoteError(Exception):
    pass


# ---------------------------------------------------------------- 文の形

def text_problems(text) -> list:
    """★読者に出る一言の形の検査（ハブの生成器も同じ関数を通す）★ → 理由の一覧（空なら合格）"""
    import html as _html
    import gates as _g
    out = []
    if not isinstance(text, str):
        return ["一言が文字列ではありません"]
    t = text
    if not t.strip():
        return ["一言が空です"]
    if t != t.strip():
        out.append("前後に空白があります")
    if len(t) > MAX_LEN:
        out.append(f"{MAX_LEN}字を超えています（{len(t)}字）")
    if any(c in t for c in "<>&\n\r\t"):
        out.append("タグ・記号の参照・改行は使えません")
    if "**" in t or "~~" in t or "★" in t:
        out.append("飾りの記号（** ~~ ★）は使えません")
    for why in (_g.invisible_unsafe(t), _g.invisible_unsafe(_html.unescape(t)), _g.html_unsafe(t)):
        if why:
            out.append(why)
    if not t.endswith("。"):
        out.append("文末が「。」ではありません")
    for s in [x for x in t.split("。") if x]:
        core = re.sub(r"[（(][^（）()]*[）)]$", "", s).rstrip()
        if not core.endswith(_ENDINGS):
            out.append(f"です・ますで終わっていない文があります（{s[-12:]}）")
    import audit_site as _a
    for name in _a.COMPETITOR_NAMES:
        if name in t:
            out.append("サイト名が入っています")
            break
    if _a._judge_54_wording(t):
        out.append("どこから採ったかの言い回しが入っています")
    import decide_now as _dn
    if any(m in t for m in _dn.ADVICE_MARKS):
        out.append("打ち方をすすめる言い方が入っています")
    # ★数値は算用数字で書く★（漢数字は「記録に無い数値」の検査を素通りする＝Codexの指摘）
    if _KANJI_NUM.search(t):
        out.append("数値は算用数字で書いてください（漢数字の数量があります）")
    return out


_KANJI_NUM = re.compile(r"[〇零一二三四五六七八九十百千万]+\s*(?:G|Ｇ|ゲーム|枚|回|周|pt|ｐｔ|%|％|スルー)")
_THOUSANDS = re.compile(r"(?<=\d)[,，](?=\d{3}(?!\d))")


def plain_numbers(s: str) -> list:
    """★桁区切り（1,000）を外してから数値を拾う★（記録は 1000・一言は 1,000 でも同じ数）"""
    import decide_now as _dn
    return _dn.signed_numbers(_THOUSANDS.sub("", str(s)))


# ---------------------------------------------------------------- 一言の置き場（リポジトリの中）

def _empty_notes() -> dict:
    return {"schema_version": SCHEMA, "notes": {}}


def read_notes(path: str = "") -> dict:
    """★無ければ空・形が違えば例外★（黙って0件と読まない）"""
    path = path or NOTES
    if not os.path.isfile(path):
        return _empty_notes()
    import safe_json as _sj
    d = _sj.read_json(path, expect=dict)
    if d.get("schema_version") != SCHEMA or not isinstance(d.get("notes"), dict):
        raise NoteError("ranking-notes.json の形が違います")
    for slug, per in d["notes"].items():
        if not _SLUG.match(str(slug)) or not isinstance(per, dict) or not per:
            raise NoteError(f"ranking-notes.json の {slug!r} の形が違います")
        for k, v in per.items():
            if k not in RANKINGS:
                raise NoteError(f"ranking-notes.json の {slug} に知らない欄があります（{k!r}）")
            bad = text_problems(v)
            if bad:
                raise NoteError(f"ranking-notes.json の {slug}/{k} の一言が決まりに合いません: {bad[0]}")
    return d


def _write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- 根拠の控え（リポジトリの外）

def read_basis(path: str = "") -> dict:
    path = path or BASIS
    if not os.path.isfile(path):
        return {"schema_version": "ranking-basis/v1", "entries": {}}
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if not isinstance(d, dict) or not isinstance(d.get("entries"), dict):
        raise NoteError("ranking_basis.json の形が違います")
    return d


def record_sha(rec) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(rec, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _record_numbers(rec) -> set:
    """★記録の値（value）に書かれた数値★（符号つき・単位なし・桁区切りは外す）"""
    val = (rec or {}).get("value") if isinstance(rec, dict) else None
    out = set()

    def walk(x):
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, bool) or x is None:
            return
        else:
            out.update(plain_numbers(str(x)))
    walk(val)
    return out


# ---------------------------------------------------------------- 決定の検査（純関数）

def decision_problems(dec, machines: list, records: dict) -> list:
    """★決定ファイルの検査★（`records`＝その機種の確定値の記録）→ 理由の一覧（空なら合格）"""
    import confirmed_values as _cv
    if not isinstance(dec, dict):
        return ["決定ファイルが辞書ではありません"]
    out = []
    slug = str(dec.get("slug") or "")
    if not _SLUG.match(slug):
        return ["slug の形が違います（英小文字・数字・下線だけ）"]
    # ★一言を取り下げる（NO_NOTE）のは、機種一覧から消えた機種でもできる★（Codexの指摘）
    #   ＝消えた機種の一言を --check が知らせても、取り下げる道が無かった
    if dec.get("decision") != "NO_NOTE" and \
            not any(isinstance(m, dict) and m.get("slug") == slug for m in machines):
        out.append(f"{slug} は機種一覧にありません")
    ranking = dec.get("ranking")
    if ranking not in RANKINGS:
        out.append(f"ranking は {' / '.join(RANKINGS)} のどれかです")
    decision = dec.get("decision")
    if decision not in DECISIONS:
        out.append(f"decision は {' / '.join(DECISIONS)} のどれかです")
    by = dec.get("by")
    judges = frozenset(x.strip() for x in str(by or "").split(",") if x.strip()) \
        if isinstance(by, str) else frozenset(str(x).strip() for x in (by or []))
    if judges not in _cv.JUDGE_PAIRS:
        out.append(f"判断者は {_cv.JUDGES_LABEL} です")
    if len(str(dec.get("why") or "").strip()) < MIN_WHY:
        out.append(f"理由（why）は{MIN_WHY}字以上です")
    if out or decision == "NO_NOTE":
        return out
    text = dec.get("text")
    out += text_problems(text)
    keys = dec.get("records")
    if not isinstance(keys, list) or not keys or len(set(map(str, keys))) != len(keys):
        return out + ["records（名指しする確定値の項目名）が要ります（重複なし）"]
    allowed = set()
    for k in keys:
        k = str(k)
        if _cv.base_field(k) != RECORD_BASE[ranking]:
            out.append(f"{ranking} の一言に {k} は使えません（{RECORD_BASE[ranking]} の記録だけ）")
            continue
        if k not in records:
            out.append(f"{slug} の確定値に {k} がありません（先に confirmed_values.py --record）")
            continue
        allowed |= _record_numbers(records[k])
    if out:
        return out
    made = [n for n in plain_numbers(text) if n not in allowed]
    if made:
        out.append("名指しした記録に無い数値があります: " + "・".join(dict.fromkeys(made)))
    return out


# ---------------------------------------------------------------- 書く

def apply(dec_path: str, dry_run: bool = False, notes_path: str = "", basis_path: str = "",
          machines_path: str = "") -> int:
    import confirmed_values as _cv
    import safe_json as _sj
    notes_path = notes_path or NOTES
    basis_path = basis_path or BASIS
    bad_code = unattended_code_problem()
    if bad_code:
        print(f"★書きません★ {bad_code}")
        return 1
    with open(dec_path, encoding="utf-8") as f:
        dec = json.load(f)
    machines = _sj.read_rows(machines_path or MACHINES)
    slug = str((dec or {}).get("slug") or "") if isinstance(dec, dict) else ""
    try:
        records = _cv.for_slug_checked(slug) if _SLUG.match(slug) else {}
    except Exception as e:                       # noqa: BLE001
        print(f"★確定値の控えを読めません（書きません）: {type(e).__name__}: {e}★")
        return 2
    bad = decision_problems(dec, machines, records)
    if bad:
        print("★書きません★")
        for b in bad:
            print("  - " + b)
        return 1
    ranking = dec["ranking"]
    notes = read_notes(notes_path)
    basis = read_basis(basis_path)
    key = f"{slug}::{ranking}"
    before = (notes["notes"].get(slug) or {}).get(ranking)
    if dec["decision"] == "WRITE":
        after = dec["text"]
        cited = {str(k): record_sha(records[str(k)]) for k in dec["records"]}
    else:
        after = None
        cited = {}
    if dry_run:
        print(f"{key}: {before!r} → {after!r}" + ("" if before != after else "（変更なし）"))
        return 0
    # ★根拠を先に書く★＝一言の書き込みが途中で落ちても、--check が「根拠だけある」と知らせる
    basis["entries"][key] = {
        "decision": dec["decision"], "text": after, "records": cited,
        "by": dec["by"] if isinstance(dec["by"], str) else ",".join(dec["by"]),
        "why": str(dec["why"]).strip()[:600],
        "at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    _write_json(basis_path, basis)
    per = dict(notes["notes"].get(slug) or {})
    if after is None:
        per.pop(ranking, None)
    else:
        per[ranking] = after
    if per:
        notes["notes"][slug] = per
    else:
        notes["notes"].pop(slug, None)
    _write_json(notes_path, notes)
    back = (read_notes(notes_path)["notes"].get(slug) or {}).get(ranking)
    if back != after:
        print("★書いた後の読み直しが合いません★")
        return 1
    # ★★終了コードで「公開するファイルが変わったか」を分ける★★（Codexの指摘）
    #   10＝一言が変わった（ページを作り直してコミットする）／0＝変わっていない（根拠の控えだけ新しくした）
    #   ＝確かめ直して同じ結論だった日に、変更の無いコミットで手順が止まらないようにする
    if before == after:
        print(f"{key}: 一言は変わっていません（根拠の控えだけ新しくしました・コミット不要）")
        return 0
    what = "消しました" if after is None else ("変えました" if before else "足しました")
    print(f"{key}: {what}（{before!r} → {after!r}）")
    return CHANGED


CHANGED = 10
UNREADABLE_MARK = "確定値の記録を読めません"


def unattended_code_problem() -> str:
    """★無人で動いている間は、コミットされていないスクリプトでは書かない★（Codexの指摘）
    一言だけの経路は担当の受け付け（task_guard.claim）を通らないので、同じ検査をここで呼ぶ。
    ★対話セッション（ロックが無い）では見ない★"""
    import task_guard as _tg
    if not _tg.lock_is_live():
        return ""
    dirty, gw = _tg.unattended_code_state(TASK)
    if dirty:
        return "コミットされていないスクリプトがあります: " + " / ".join(dirty[:3])
    if gw:
        return f"git に問い合わせできませんでした（{gw[:120]}）"
    return ""


TASK = "ranking-machine"


# ---------------------------------------------------------------- 照合

def check_problems(notes: dict, basis: dict, machines: list, records_of) -> list:
    """★純関数★ `records_of(slug)` は確定値の記録を返す（読めなければ例外）"""
    out = []
    alive = {m.get("slug") for m in machines if isinstance(m, dict)}
    seen = set()
    for slug, per in sorted(notes["notes"].items()):
        for ranking, text in sorted(per.items()):
            key = f"{slug}::{ranking}"
            seen.add(key)
            if slug not in alive:
                out.append(f"{key}: 機種一覧に無い機種の一言が残っています")
            b = basis["entries"].get(key)
            if not isinstance(b, dict) or b.get("decision") != "WRITE":
                out.append(f"{key}: 根拠の控えがありません")
                continue
            if b.get("text") != text:
                out.append(f"{key}: 一言が控えと違います（手で書き換えられた疑い）")
            cited = b.get("records")
            if not isinstance(cited, dict) or not cited:
                out.append(f"{key}: 名指しした記録がありません")
                continue
            # ★機種ごとに読む★（Codexの2回目）＝上位10に出ていない機種の記録が壊れているだけで、
            #   その日の照合ぜんたいが「読めない」になり、タスクが書き込みを全部やめていた
            try:
                recs = records_of(slug)
            except Exception as e:               # noqa: BLE001
                out.append(f"{key}: {UNREADABLE_MARK}（{type(e).__name__}: {str(e)[:120]}）")
                continue
            for k, sha in sorted(cited.items()):
                if k not in recs:
                    out.append(f"{key}: 名指しした記録 {k} が確定値から消えています")
                elif record_sha(recs[k]) != sha:
                    out.append(f"{key}: 名指しした記録 {k} が書いたあとに変わっています（2AIで確かめ直す）")
            # ★★書いたときと同じ検査を、いまの記録でかけ直す★★（Codexの指摘）
            #   ＝指紋が合うだけでは、判断者・理由・記録の種類・文の数値が決まりどおりかは分からない
            if slug in alive:
                again = decision_problems(
                    {"slug": slug, "ranking": ranking, "decision": "WRITE", "text": text,
                     "records": sorted(cited), "by": b.get("by"), "why": b.get("why")},
                    machines, recs)
                for x in again:
                    out.append(f"{key}: 書いたときの決まりに合いません（{x}）")
    for key, b in sorted(basis["entries"].items()):
        if key not in seen and isinstance(b, dict) and b.get("decision") == "WRITE":
            out.append(f"{key}: 控えでは書いたことになっているのに、一言がありません")
    return out


def check() -> int:
    import confirmed_values as _cv
    import safe_json as _sj
    try:
        notes = read_notes()
        basis = read_basis()
        machines = _sj.read_rows(MACHINES)
        bad = check_problems(notes, basis, machines, _cv.for_slug_checked)
        shown = shown_keys()
    except Exception as e:                       # noqa: BLE001
        print(f"★ランキングの一言を確かめられません★: {type(e).__name__}: {e}")
        return 2
    n = sum(len(v) for v in notes["notes"].values())
    now, later = split_shown(bad, shown)
    if later:
        # ★いま上位10に出ていない一言は、知らせるだけ★（Codexの指摘＝夕方の枠を使わせない）
        #   上位10へ戻ってきたら、そのときは上の「直す」側に入る
        print(f"（参考）いま上位10に出ていない一言のうち、根拠と合わないもの {len(later)}件")
        for b in later:
            print("  - " + b)
    if now:
        print(f"★ランキングの一言 {n}件のうち、いま上位10に出ていて根拠と合わないもの★")
        for b in now:
            print("  - " + b)
        # ★いま出ている一言の記録が読めないときだけ「読めない」（2）★
        return 2 if any(UNREADABLE_MARK in b for b in now) else 1
    print(f"ランキングの一言 {n}件：上位10に出ている分は根拠とそろっています")
    return 0


def shown_keys() -> set:
    """★いまページに出ている一言の鍵（slug::ranking）★＝上位10（`build_hub_pages.ranking_top`）"""
    import build_hub_pages as _bh
    rows = _bh.load_rows()
    return {f"{r['slug']}::{rk}" for rk in RANKINGS for r in _bh.ranking_top(rows, rk)}


def split_shown(bad: list, shown: set) -> tuple:
    """★純関数★ 指摘を「いま出ている一言」と「出ていない一言」に分ける（指摘は鍵で始まる）"""
    now, later = [], []
    for b in bad:
        key = str(b).split(": ", 1)[0]
        (now if key in shown else later).append(b)
    return now, later


# ---------------------------------------------------------------- 試験

def selftest() -> int:
    import tempfile
    results = []

    def t(name, cond):
        results.append(bool(cond))
        print(("✅" if cond else "❌") + " " + name)

    ok_text = "設定変更後は天井が600Gに短縮されます（電源のオンオフだけでは付きません）。"
    t("決まりどおりの一言は通る", text_problems(ok_text) == [])
    t("★です・ますで終わらない文は断る★", text_problems("天井が600Gに短縮。") != [])
    t("★タグは断る★", text_problems("天井が<b>600G</b>に短縮されます。") != [])
    t("★見えない字は断る★", text_problems("天井が600G‮に短縮されます。") != [])
    t("★長すぎる一言は断る★", text_problems("あ" * MAX_LEN + "です。") != [])
    t("★サイト名は断る★", text_problems("ちょんぼりすたによると短縮されます。") != [])
    t("★どこから採ったかの言い回しは断る★", text_problems("各解析では短縮されると言われています。") != [])
    t("★打ち方をすすめる言い方は断る★", text_problems("リセット台は積極的に狙えます。") != [])

    ms = [{"slug": "aaa"}, {"slug": "bbb"}]
    rec_reset = {"value": {"kind": "CEILING_SHORTENED", "games": "600"}, "sources": []}
    rec_ceil = {"value": {"kind": "GAME", "amount": "999", "unit": "G", "benefit": "AT"}, "sources": []}
    recs = {"reset": rec_reset, "ceiling#bonus": rec_ceil}
    good = {"slug": "aaa", "ranking": "reset", "decision": "WRITE", "text": ok_text,
            "records": ["reset"], "by": "claude,codex", "why": "2AIが出典2つの原文で短縮と条件を確かめた"}
    t("決まりどおりの決定は通る", decision_problems(good, ms, recs) == [])
    t("代役の組も通る", decision_problems(dict(good, by="claude-agent-a,claude-agent-b"), ms, recs) == [])
    t("★判断者が1人なら断る★", decision_problems(dict(good, by="claude"), ms, recs) != [])
    t("★組を混ぜたら断る★", decision_problems(dict(good, by="claude,claude-agent-b"), ms, recs) != [])
    t("★理由が短ければ断る★", decision_problems(dict(good, why="短い"), ms, recs) != [])
    t("★記録に無い数値は断る（数字を作らない）★",
      decision_problems(dict(good, text=ok_text.replace("600", "500")), ms, recs) != [])
    t("★リセットの一言に天井の記録を名指ししたら断る★",
      decision_problems(dict(good, records=["ceiling#bonus"],
                             text="設定変更後は天井が999Gになります。"), ms, recs) != [])
    t("★天井の一言にリセットの記録を名指ししたら断る★",
      decision_problems(dict(good, ranking="tenjo"), ms, recs) != [])
    t("天井の一言は ceiling の記録で通る",
      decision_problems(dict(good, ranking="tenjo", records=["ceiling#bonus"],
                             text="天井の999GでATに当選します。"), ms, recs) == [])
    t("★名指ししない記録の数値では通さない★（ほかの記録にあっても）",
      decision_problems(dict(good, records=["reset"], text="天井の999GでATに当選します。"), ms, recs) != [])
    t("★無い記録を名指ししたら断る★", decision_problems(dict(good, records=["reset#x"]), ms, recs) != [])
    t("★名指しが空なら断る★", decision_problems(dict(good, records=[]), ms, recs) != [])
    t("★機種一覧に無い機種は断る★", decision_problems(dict(good, slug="zzz"), ms, recs) != [])
    t("★slug の形が違えば断る★", decision_problems(dict(good, slug="../x"), ms, recs) != [])
    t("★桁区切りの数字（1,000G）は記録の 1000 と同じ数として通る★",
      decision_problems(dict(good, ranking="tenjo", records=["ceiling#k"],
                             text="天井の1,000GでATに当選します。"), ms,
                        dict(recs, **{"ceiling#k": {"value": {"kind": "GAME", "amount": "1000",
                                                              "unit": "G", "benefit": "AT"}}})) == [])
    t("　桁区切りでも、記録に無い数は断る",
      decision_problems(dict(good, ranking="tenjo", records=["ceiling#bonus"],
                             text="天井の1,000GでATに当選します。"), ms, recs) != [])
    t("★漢数字の数量は断る★（記録に無い数値の検査を素通りするため）",
      text_problems("設定変更後は天井が五百Gになります。") != [])
    t("　「一言」のような数量でない漢字は断らない",
      text_problems("一度リセットされると天井が600Gに短縮されます。") == [])
    t("★機種一覧から消えた機種でも NO_NOTE で取り下げられる★",
      decision_problems({"slug": "zzz", "ranking": "reset", "decision": "NO_NOTE",
                         "by": "claude,codex", "why": "機種一覧から消えたので一言を取り下げる"}, ms, {}) == [])
    t("NO_NOTE は文も記録も要らない",
      decision_problems({"slug": "aaa", "ranking": "reset", "decision": "NO_NOTE",
                         "by": "claude,codex", "why": "出典に恩恵の記載が無いと2AIで確かめた"}, ms, {}) == [])

    with tempfile.TemporaryDirectory() as td:
        np_, bp = os.path.join(td, "n.json"), os.path.join(td, "b.json")
        mp = os.path.join(td, "m.json")
        with open(mp, "w", encoding="utf-8") as f:
            json.dump(ms, f)
        dp = os.path.join(td, "d.json")
        import confirmed_values as _cv
        keep = _cv.for_slug_checked
        try:
            _cv.for_slug_checked = lambda s: recs
            with open(dp, "w", encoding="utf-8") as f:
                json.dump(good, f, ensure_ascii=False)
            t("書ける（一言が変わったので 10）", apply(dp, notes_path=np_, basis_path=bp, machines_path=mp) == CHANGED)
            t("★同じ決定をもう一度当てると 0（一言は変わらない＝コミットしない）★",
              apply(dp, notes_path=np_, basis_path=bp, machines_path=mp) == 0)
            n = read_notes(np_)
            t("一言が置き場に入る", n["notes"]["aaa"]["reset"] == ok_text)
            b = read_basis(bp)
            t("根拠に名指しした記録の指紋が入る",
              b["entries"]["aaa::reset"]["records"] == {"reset": record_sha(rec_reset)})
            t("照合が合う", check_problems(n, b, ms, lambda s: recs) == [])
            recs2 = dict(recs, reset={"value": {"kind": "CEILING_SHORTENED", "games": "500"}})
            t("★名指しした記録が変わったら照合が知らせる★",
              any("変わっています" in x for x in check_problems(n, b, ms, lambda s: recs2)))
            t("★名指しした記録が消えたら照合が知らせる★",
              any("消えています" in x for x in check_problems(n, b, ms, lambda s: {})))
            n2 = json.loads(json.dumps(n))
            n2["notes"]["aaa"]["reset"] = ok_text.replace("電源", "でんげん")
            t("★一言を手で書き換えたら照合が知らせる★",
              any("控えと違います" in x for x in check_problems(n2, b, ms, lambda s: recs)))
            n3 = {"schema_version": SCHEMA, "notes": {}}
            t("★控えにあるのに一言が無ければ知らせる★",
              any("一言がありません" in x for x in check_problems(n3, b, ms, lambda s: recs)))
            t("★機種一覧から消えた機種の一言を知らせる★",
              any("機種一覧に無い" in x for x in check_problems(n, b, [{"slug": "bbb"}], lambda s: recs)))
            # ★指紋だけ合っていても、書いたときの決まりに合わなければ知らせる★（Codexの指摘）
            n4 = {"schema_version": SCHEMA, "notes": {"aaa": {"reset": ok_text.replace("600", "500")}}}
            b4 = json.loads(json.dumps(b))
            b4["entries"]["aaa::reset"]["text"] = ok_text.replace("600", "500")
            t("★記録に無い数値の一言は、指紋が合っても照合が知らせる★",
              any("書いたときの決まり" in x for x in check_problems(n4, b4, ms, lambda s: recs)))
            b5 = json.loads(json.dumps(b))
            b5["entries"]["aaa::reset"]["by"] = "claude"
            t("★判断者が1人の控えは照合が知らせる★",
              any("書いたときの決まり" in x for x in check_problems(n, b5, ms, lambda s: recs)))
            def _boom(s):
                raise ValueError("壊れた記録")
            try:
                got_b = check_problems(n, b, ms, _boom)
            except Exception:                    # noqa: BLE001 例外で止まるのは「止まらず返す」の失敗
                got_b = []
            t("★機種の記録が読めなくても照合は止まらず、その機種の行として返す★（出ていない機種なら参考に回せる）",
              len(got_b) == 1 and got_b[0].startswith("aaa::reset: ") and UNREADABLE_MARK in got_b[0])
            t("★いま上位10に出ていない一言の指摘は、出ている分と分ける★",
              split_shown(["aaa::reset: x", "bbb::tenjo: y"], {"aaa::reset"})
              == (["aaa::reset: x"], ["bbb::tenjo: y"]))
            with open(dp, "w", encoding="utf-8") as f:
                json.dump({"slug": "aaa", "ranking": "reset", "decision": "NO_NOTE", "by": "claude,codex",
                           "why": "出典に恩恵の記載が無いと2AIで確かめた"}, f, ensure_ascii=False)
            t("NO_NOTE で一言を取り下げられる",
              apply(dp, notes_path=np_, basis_path=bp, machines_path=mp) == CHANGED
              and "aaa" not in read_notes(np_)["notes"])
            t("取り下げたあとも照合が合う",
              check_problems(read_notes(np_), read_basis(bp), ms, lambda s: recs) == [])
            with open(dp, "w", encoding="utf-8") as f:
                json.dump(dict(good, text=ok_text.replace("600", "700")), f, ensure_ascii=False)
            t("★断った決定は何も書かない★",
              apply(dp, notes_path=np_, basis_path=bp, machines_path=mp) == 1
              and "aaa" not in read_notes(np_)["notes"])
            # ★無人で動いている間は、コミットされていないスクリプトでは書かない★（Codexの指摘）
            import task_guard as _tg
            keep_live, keep_state = _tg.lock_is_live, _tg.unattended_code_state
            try:
                with open(dp, "w", encoding="utf-8") as f:
                    json.dump(good, f, ensure_ascii=False)
                _tg.lock_is_live = lambda *a, **k: True
                _tg.unattended_code_state = lambda task: (["scripts/x.py"], "")
                t("★無人の最中に未コミットのスクリプトがあれば書かない★",
                  apply(dp, notes_path=np_, basis_path=bp, machines_path=mp) == 1
                  and "aaa" not in read_notes(np_)["notes"])
                _tg.unattended_code_state = lambda task: ([], "")
                t("　未コミットが無ければ書ける",
                  apply(dp, notes_path=np_, basis_path=bp, machines_path=mp) == CHANGED)
                _tg.lock_is_live = lambda *a, **k: False
                _tg.unattended_code_state = lambda task: (["scripts/x.py"], "")
                t("　対話セッション（ロックなし）では見ない",
                  apply(dp, notes_path=np_, basis_path=bp, machines_path=mp) == 0)
            finally:
                _tg.lock_is_live, _tg.unattended_code_state = keep_live, keep_state
            with open(np_, "w", encoding="utf-8") as f:
                json.dump({"schema_version": SCHEMA, "notes": {"aaa": {"reset": "天井が短縮。"}}},
                          f, ensure_ascii=False)
            try:
                read_notes(np_)
                t("★置き場の一言が決まりに合わなければ読む側で止める★", False)
            except NoteError:
                t("★置き場の一言が決まりに合わなければ読む側で止める★", True)
        finally:
            _cv.for_slug_checked = keep
    ok = sum(results)
    print(f"{ok}/{len(results)} 合格")
    return 0 if ok == len(results) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.check:
        return check()
    if a.apply:
        return apply(a.apply, dry_run=a.dry_run)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())

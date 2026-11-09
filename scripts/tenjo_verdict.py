"""★天井ランキング・早見表の「天井」の欄を、2AIの決定どおりに直す唯一の場所★（2026-11-07）

使い方:
  python scripts/tenjo_verdict.py --apply <決定ファイル> [--dry-run]
  python scripts/tenjo_verdict.py --selftest

★なぜ要るか★＝機種一覧（machines.json）の `tenjo_games`（天井ランキングの並びと対象）と
  `tenjo_display`（表に出す天井の書き方）は、書く道具が無く、これまで対話セッションが手で直していた。
  ランキング専用タスク（毎日17:00）が誤りを見つけても、その回に直せなかった。
★2つを1つの決定で直す★（Codex review_ranking_task の1）＝並びに使う値と表の書き方を別々に直すと、
  順位と表示が食い違う。
★決めるのは2AI／機械が見るのは形と照合だけ★
  ・判断者の組と理由15字以上
  ・★直す前の値（before）がいまの機種一覧と一致すること★（読んだあとに別の書き手が変えていたら書かない）
  ・`tenjo_games` は 1〜9999 の整数か null（null＝G数で数えない天井＝天井ランキングの対象外）
  ・`tenjo_display` は40字以内・タグや見えない字・サイト名・どこから採ったかの言い回しなし
  ・`tenjo_games` が整数なら、その数が表の書き方の中に在ること（順位と表示を食い違わせない）
  ・★数値は、名指しした確定値の記録（ceiling* / checker_ceiling）か、いまの表の書き方に在ること★（数字を作らない）
  ・★機種一覧のほかの所は1文字も変えない★
決定ファイルの形:
  {"slug": "...", "tenjo_games": 600, "tenjo_display": "CZ間600G+α",
   "before": {"tenjo_games": 999, "tenjo_display": "999G"},   ← 無かった欄は "__absent__"
   "records": ["ceiling#cz"], "by": "claude,codex", "why": "..."}
★書いたら `python scripts/build_hub_pages.py --legacy` でランキングと早見表を作り直す★（この道具は作り直さない）。
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
ABSENT = "__absent__"
KEYS = ("tenjo_games", "tenjo_display")
MAX_DISPLAY = 40
MIN_WHY = 15
_SLUG = re.compile(r"^[a-z0-9_]+$")


def _cur(m: dict, k: str):
    return m[k] if k in m else ABSENT


def display_problems(text) -> list:
    import html as _html
    import gates as _g
    if not isinstance(text, str) or not text.strip():
        return ["tenjo_display が空です"]
    out = []
    if text != text.strip():
        out.append("前後に空白があります")
    if len(text) > MAX_DISPLAY:
        out.append(f"{MAX_DISPLAY}字を超えています")
    if any(c in text for c in "<>&\n\r\t") or "**" in text or "★" in text:
        out.append("タグ・記号・改行は使えません")
    for why in (_g.invisible_unsafe(text), _g.invisible_unsafe(_html.unescape(text))):
        if why:
            out.append(why)
    import ranking_note as _rn
    if _rn._KANJI_NUM.search(text):
        out.append("数値は算用数字で書いてください（漢数字の数量があります）")
    import audit_site as _a
    if any(n in text for n in _a.COMPETITOR_NAMES):
        out.append("サイト名が入っています")
    if _a._judge_54_wording(text):
        out.append("どこから採ったかの言い回しが入っています")
    return out


def decision_problems(dec, machines: list, records: dict) -> list:
    """★純関数★ `records`＝その機種の確定値の記録"""
    import confirmed_values as _cv
    import decide_now as _dn
    import ranking_note as _rn
    if not isinstance(dec, dict):
        return ["決定ファイルが辞書ではありません"]
    slug = str(dec.get("slug") or "")
    if not _SLUG.match(slug):
        return ["slug の形が違います"]
    m = next((x for x in machines if isinstance(x, dict) and x.get("slug") == slug), None)
    if m is None:
        return [f"{slug} は機種一覧にありません"]
    # ★新台経路の機種は断る★（2026-11-09）＝ランキングの天井はチェッカーの基準欄の天井（build_hub_pages.auto_rank）で、
    #   tenjo_games は使わない。しかも育成が機種行を材料から作り直すので書いても翌朝消える。直すのは checker_verdict。
    if "publication_policy" in m:
        return [f"{slug} は新台です。天井は checker_verdict でチェッカーの欄の天井を直してください"
                "（ランキングはその欄の天井を使います）"]
    out = []
    by = dec.get("by")
    judges = frozenset(x.strip() for x in str(by or "").split(",") if x.strip())
    if judges not in _cv.JUDGE_PAIRS:
        out.append(f"判断者は {_cv.JUDGES_LABEL} です")
    if len(str(dec.get("why") or "").strip()) < MIN_WHY:
        out.append(f"理由（why）は{MIN_WHY}字以上です")
    before = dec.get("before")
    if not isinstance(before, dict) or set(before) != set(KEYS):
        out.append("before に tenjo_games と tenjo_display の両方（無ければ \"__absent__\"）が要ります")
    else:
        for k in KEYS:
            if before[k] != _cur(m, k):
                out.append(f"{k} がいまの機種一覧と違います（いま {_cur(m, k)!r}）＝読んだあとに変わった")
    if not all(k in dec for k in KEYS):
        out.append("tenjo_games と tenjo_display の両方を書いてください")
        return out
    g, disp = dec["tenjo_games"], dec["tenjo_display"]
    if g is not None and (isinstance(g, bool) or not isinstance(g, int) or not 1 <= g <= 9999):
        out.append("tenjo_games は 1〜9999 の整数か null です")
    out += display_problems(disp)
    if out:
        return out
    nums = _rn.plain_numbers(disp) + ([str(g)] if g is not None else [])
    if g is not None and str(g) not in _rn.plain_numbers(disp):
        out.append(f"tenjo_games（{g}）が表の書き方に入っていません（順位と表示が食い違う）")
    allowed = set(_rn.plain_numbers(str(m.get("tenjo_display") or "")))
    if isinstance(m.get("tenjo_games"), int):
        allowed.add(str(m["tenjo_games"]))
    keys = dec.get("records") or []
    if not isinstance(keys, list):
        return out + ["records は項目名の一覧です"]
    for k in keys:
        k = str(k)
        if _cv.base_field(k) not in ("ceiling", "checker_ceiling"):
            out.append(f"{k} は天井の記録ではありません")
        elif k not in records:
            out.append(f"{slug} の確定値に {k} がありません（先に confirmed_values.py --record）")
        else:
            allowed |= _rn._record_numbers(records[k])
    # ★★G数の天井の記録を名指ししたのにランキングから外す（null）なら、外す理由を書かせる★★（Codexの指摘）
    #   ＝AT間だけのG数天井（godzilla）や「約633G相当」（magireco）のように、外すのが正しい機種もあるので
    #   機械は止めない。★外す判断は2AIで、理由が無いと通さない★（黙って順位から消えない）
    #   ★いまランキングに入っている機種を null に変えるときも同じ★（Codexの2回目＝記録を名指ししなければ
    #   理由なしで外せた）
    #   ★「いまの値」はランキングと同じ読み方で取る★（Codexの3回目）＝tenjo_games が無い機種は
    #   チェッカーの limit で並んでいる（`build_hub_pages` の rank_limit）
    if g is None and (any(_is_game_ceiling(records.get(str(k))) for k in keys)
                      or effective_games(m) is not None) \
            and len(str(dec.get("exclude_why") or "").strip()) < MIN_WHY:
        out.append("tenjo_games を null（天井ランキングの対象外）にするには、"
                   f"exclude_why（{MIN_WHY}字以上）に外す理由を書いてください"
                   "（G数の天井の記録を名指ししたとき・いまランキングに入っている機種を外すとき）")
    made = [n for n in nums if n not in allowed]
    if made:
        out.append("名指しした記録にも、いまの表の書き方にも無い数値があります: "
                   + "・".join(dict.fromkeys(made)))
    return out


def effective_games(m: dict):
    """★天井ランキングがいま使っている値★（`build_hub_pages` の rank_limit と同じ読み方）"""
    import build_hub_pages as _bh
    return m["tenjo_games"] if "tenjo_games" in m else _bh._scalar_limit(m.get("limit"))


def _is_game_ceiling(rec) -> bool:
    """★その記録がG数の天井か★（確定値の ceiling は unit=G・checker_ceiling は games を持つ）"""
    val = (rec or {}).get("value") if isinstance(rec, dict) else None
    if not isinstance(val, dict):
        return False
    return str(val.get("unit") or "").upper() == "G" or "games" in val


def revised(m: dict, dec: dict) -> dict:
    new = json.loads(json.dumps(m, ensure_ascii=False))
    new["tenjo_games"] = dec["tenjo_games"]
    new["tenjo_display"] = dec["tenjo_display"]
    return new


def apply(dec_path: str, dry_run: bool = False, machines_path: str = "") -> int:
    import confirmed_values as _cv
    import safe_json as _sj
    path = machines_path or MACHINES
    with open(dec_path, encoding="utf-8") as f:
        dec = json.load(f)
    ms = _sj.read_rows(path)
    slug = str((dec or {}).get("slug") or "") if isinstance(dec, dict) else ""
    try:
        records = _cv.for_slug_checked(slug) if _SLUG.match(slug) else {}
    except Exception as e:                       # noqa: BLE001
        print(f"★確定値の控えを読めません（書きません）: {type(e).__name__}: {e}★")
        return 2
    bad = decision_problems(dec, ms, records)
    if bad:
        print("★書きません★")
        for b in bad:
            print("  - " + b)
        return 1
    i = next(i for i, m in enumerate(ms) if m.get("slug") == slug)
    new = revised(ms[i], dec)
    if dry_run:
        print(f"{slug}: {[_cur(ms[i], k) for k in KEYS]} → {[new[k] for k in KEYS]}")
        return 0
    if new == ms[i]:
        print(f"{slug}: もう決定どおりです（書きません）")
        return 0
    ms2 = list(ms)
    ms2[i] = new
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(ms2, ensure_ascii=False, indent=1) + "\n")
    os.replace(tmp, path)
    back = _sj.read_rows(path)
    if back != ms2:
        print("★書いた後の読み直しが合いません★（手で確かめてください）")
        return 1
    print(f"{slug}: 天井の欄を直しました {[_cur(ms[i], k) for k in KEYS]} → {[new[k] for k in KEYS]}"
          "（このあと build_hub_pages.py --legacy）")
    return 0


def selftest() -> int:
    import tempfile
    results = []

    def t(name, cond):
        results.append(bool(cond))
        print(("✅" if cond else "❌") + " " + name)

    ms = [{"slug": "aaa", "name": "A", "tenjo_games": 999, "tenjo_display": "999G"},
          {"slug": "bbb", "name": "B"}]
    recs = {"ceiling#cz": {"value": {"kind": "GAME", "amount": "600", "unit": "G", "benefit": "CZ"}},
            "reset": {"value": {"kind": "CEILING_SHORTENED", "games": "300"}}}
    good = {"slug": "aaa", "tenjo_games": 600, "tenjo_display": "CZ間600G",
            "before": {"tenjo_games": 999, "tenjo_display": "999G"},
            "records": ["ceiling#cz"], "by": "claude,codex", "why": "出典2つの原文でCZ間600Gを確かめた"}
    t("決まりどおりの決定は通る", decision_problems(good, ms, recs) == [])
    t("★新台経路の機種は断る★（ランキングはチェッカーの欄の天井を使う）",
      decision_problems(good, [dict(ms[0], publication_policy="page-decision/v1"), ms[1]], recs) != [])
    t("★直す前の値がいまと違えば断る★",
      decision_problems(dict(good, before={"tenjo_games": 800, "tenjo_display": "999G"}), ms, recs) != [])
    t("★無かった欄は __absent__ で書く★",
      decision_problems(dict(good, slug="bbb", before={"tenjo_games": ABSENT, "tenjo_display": ABSENT}),
                        ms, recs) == [])
    t("★記録にも表にも無い数値は断る★",
      decision_problems(dict(good, tenjo_games=650, tenjo_display="CZ間650G"), ms, recs) != [])
    t("★順位の値が表の書き方に無ければ断る★",
      decision_problems(dict(good, tenjo_display="CZ間999G"), ms, recs) != [])
    t("★天井でない記録を名指ししたら断る★",
      decision_problems(dict(good, records=["reset"], tenjo_games=300, tenjo_display="300G"), ms, recs) != [])
    t("★無い記録を名指ししたら断る★", decision_problems(dict(good, records=["ceiling#x"]), ms, recs) != [])
    t("いまの表の数値だけなら記録なしでも書き方を整えられる",
      decision_problems(dict(good, tenjo_display="通常999G", records=[], tenjo_games=999), ms, recs) == [])
    t("★G数の天井の記録を名指しして null にするなら、外す理由が無いと断る★（黙って順位から消さない）",
      decision_problems(dict(good, tenjo_games=None, tenjo_display="CZ間600G"), ms, recs) != [])
    t("　外す理由（exclude_why）があれば通す（AT間だけの天井など）",
      decision_problems(dict(good, tenjo_games=None, tenjo_display="CZ間600G",
                             exclude_why="CZ間の天井で通常時のG数天井ではないので外す"), ms, recs) == [])
    recs_pt = dict(recs, **{"ceiling#pt": {"value": {"kind": "POINT", "amount": "1000", "unit": "pt"}}})
    t("　G数でない天井（pt）だけで、いまランキングに入っていない機種なら、理由なしで null にできる",
      decision_problems(dict(good, slug="bbb", before={"tenjo_games": ABSENT, "tenjo_display": ABSENT},
                             tenjo_games=None, tenjo_display="1000pt", records=["ceiling#pt"]),
                        ms, recs_pt) == [])
    t("★いまランキングに入っている機種を null にするなら、記録を名指ししなくても理由が要る★",
      decision_problems(dict(good, tenjo_games=None, tenjo_display="999G", records=[]), ms, recs) != []
      and decision_problems(dict(good, tenjo_games=None, tenjo_display="999G", records=[],
                                 exclude_why="AT間の天井で通常時のG数天井ではないので外す"), ms, recs) == [])
    ms_c = ms + [{"slug": "ccc", "name": "C", "limit": 700, "tenjo_display": "700G"}]
    t("★tenjo_games が無く limit で並んでいる機種も、null にするなら理由が要る★（ランキングと同じ読み方）",
      decision_problems(dict(good, slug="ccc", tenjo_games=None, tenjo_display="700G", records=[],
                             before={"tenjo_games": ABSENT, "tenjo_display": "700G"}), ms_c, recs) != [])
    t("★表示の漢数字の数量は断る★",
      decision_problems(dict(good, tenjo_display="CZ間600G（五十回で短縮）"), ms, recs) != [])
    recs_k = dict(recs, **{"ceiling#k": {"value": {"kind": "GAME", "amount": "1000", "unit": "G"}}})
    t("★桁区切りの数字（1,000G）も記録の 1000 と同じ数として通る★",
      decision_problems(dict(good, tenjo_games=1000, tenjo_display="通常1,000G", records=["ceiling#k"]),
                        ms, recs_k) == [])
    t("★整数でない天井は断る★", decision_problems(dict(good, tenjo_games="600"), ms, recs) != [])
    t("★タグは断る★", decision_problems(dict(good, tenjo_display="<b>600G</b>"), ms, recs) != [])
    t("★判断者が1人なら断る★", decision_problems(dict(good, by="codex"), ms, recs) != [])
    t("★理由が短ければ断る★", decision_problems(dict(good, why="短い"), ms, recs) != [])
    with tempfile.TemporaryDirectory() as td:
        mp, dp = os.path.join(td, "m.json"), os.path.join(td, "d.json")
        with open(mp, "w", encoding="utf-8") as f:
            json.dump(ms, f, ensure_ascii=False)
        import confirmed_values as _cv
        import safe_json as _sj
        keep = _cv.for_slug_checked
        try:
            _cv.for_slug_checked = lambda s: recs
            with open(dp, "w", encoding="utf-8") as f:
                json.dump(good, f, ensure_ascii=False)
            t("書ける", apply(dp, machines_path=mp) == 0)
            back = _sj.read_rows(mp)
            t("★直したのは天井の2つだけ★",
              back[0] == dict(ms[0], tenjo_games=600, tenjo_display="CZ間600G") and back[1] == ms[1])
            t("★同じ決定をもう一度流すと「直す前の値」が違うので断る★", apply(dp, machines_path=mp) == 1)
        finally:
            _cv.for_slug_checked = keep
    ok = sum(results)
    print(f"{ok}/{len(results)} 合格")
    return 0 if ok == len(results) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.apply:
        return apply(a.apply, dry_run=a.dry_run)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""★ポチポチくん（小役カウンター）の対応を、2AIの決定から増やす唯一の場所★（2026-10-19）

★なぜ要るか★
  対応機種は setting.html の MACHINE_CONFIGS に人が手で書いたものだけだった（35機種）。
  記事ページの無効表示も machine.html に手で書いた名簿（設定差なし30・未判明65）で決まり、
  新台は一律「解析データ判明後に対応」。★対応を増やす道がどの自動タスクにも無かった★
  ＝解析が出ても永久に無効のまま（2026-10-19に運営者が数の少なさに気づいた）。

★いまの形★
  ・2AIが決めたことを決定ファイルで受け取る（`--apply <決定ファイル>`）
      ADD     … 設定差のある小役の確率表を足す（カウンターが使えるようになる）
      NO_DIFF … 小役による設定差が無い（「小役による設定差なし」と出す）
      EXTEND  … ★自動で足した機種に、小役などの表を足す★（今ある表は残す・2026-11-10）
      KO_CHECK… ★小役の差を探した結果だけを控える★（HAS＝もう入っている／NONE＝見つからない）
  ・★★小役の確認（ko_checked）★★（2026-11-10・運営者「ほとんどがゲーム数と当たり数で意味がない」）
      初当たり回数だけでも1度入れると「使える」になり、STEP 1-P が二度と見直さなかった
      （自動で足した77機種のうち61機種が当たり回数だけだった）。
      ★自動の機種は、2AIが小役の差を探した記録が無ければ STEP 1-P の対象に戻す★。
      ★NONE は KO_RECHECK_DAYS 日たったらもう一度探す★（解析はあとから出る）。
      ★どれが小役かを機械に判定させない★（項目名の名簿を作らない＝鉄則0z）。決めるのは2AIの記録だけ。
  ・機械が見るのは形と照合だけ（★意味の判断はしない★＝鉄則0z）
      判断者の組／理由15字以上／設定の段階を明示し、全部の表がちょうどその段階を持つ
      （★存在しない設定の欄を埋めない★）／値は 1/x の形／
      ★表の値は、独立した2つの出典の逐語にそろって在る★（confirmed_values と同じ照合）／
      引用はページを取り直して実在を確かめる（言うだけでは通さない）
  ・控え＝`assets/data/pochipochi.json`（読者のページが読む・数値だけ）と
    `Documents/uchidokoro/pochipochi_basis.json`（出典の引用・判断者・理由＝リポジトリの外。
    ★どこから採ったかを読者に見せない★）
  ・setting.html の MACHINE_CONFIGS の中の印と印のあいだを、控えから機械が作る（`--build`）。
    ★手で書いた機種は ADD・EXTEND・NO_DIFF では触らない★（断る）。確かめ直して自動へ移すのは TAKE_OVER だけ（2026-11-10）。

★対応・非対応を決めるのは `status_override` の1か所★（Python は build_machine_pages が、
  ブラウザは machine.html が同じ控えを読む）。控えに無い機種は今までどおりの決まり。

使い方:
  python scripts/pochipochi.py --apply <決定ファイル> [--dry-run]
  python scripts/pochipochi.py --build        # setting.html の自動の区画を作り直す
  python scripts/pochipochi.py --check        # 控えと setting.html がそろっているか
  python scripts/pochipochi.py --list
  python scripts/pochipochi.py --selftest
"""
from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import local_paths as _lp                # noqa: E402
import safe_json as _sj                  # noqa: E402

STORE = os.path.join(BASE, "assets", "data", "pochipochi.json")
BASIS = _lp.doc("pochipochi_basis.json")
LOCK = _lp.doc("pochipochi_apply.lock")   # ★リポジトリの外★（公開される場所に印を置かない）
SETTING = os.path.join(BASE, "setting.html")
MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
SCHEMA = 1

BEGIN = "  // ★POCHI-AUTO-BEGIN★（scripts/pochipochi.py が控えから作る・手で書かない）"
END = "  // ★POCHI-AUTO-END★"
NO_DIFF_REASON = "小役による設定差なし"
KO_RESULTS = ("HAS", "NONE")
KO_RECHECK_DAYS = 60
KO_HAS_RECHECK_DAYS = 180   # ★小役の差が「ある」と決めた機種も、解析の訂正・追加に合わせて確かめ直す★（2026-11-10）
HAND_RECHECK_DAYS = 90   # 手書きの機種と「設定差なし」の機種を、2AIが確かめ直す間隔
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MIN_WHY = 15
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,31}$")
_SLUG = re.compile(r"^[a-z0-9_]{1,64}$")
_RATE = re.compile(r"^1/([0-9]{1,6}(?:\.[0-9]{1,3})?)$")


class PochiError(RuntimeError):
    pass


# ── 控え ────────────────────────────────────────────────────────────
def empty() -> dict:
    return {"schema_version": SCHEMA, "configs": {}, "no_diff": {}, "ko_checked": {},
            "hand_checked": {}, "diff_checked": {}}


def load(path: str = None) -> dict:
    """★読めないときは止める★（黙って空にすると、足した対応が消えて見える）

    ★読むたびに表の形を確かめ直す★（Codex review239）＝控えを手で書き換えて
    `--build` すれば、受け取りの検査を通らない表が setting.html に入った。
    """
    d = _sj.read_json(path or STORE, expect=dict)
    if d.get("schema_version") != SCHEMA:
        raise PochiError(f"控えの版が違います: {d.get('schema_version')!r}")
    for k in ("configs", "no_diff"):
        if not isinstance(d.get(k), dict):
            raise PochiError(f"控えの {k} が組（辞書）ではありません")
    both = set(d["configs"]) & set(d["no_diff"])
    if both:
        raise PochiError(f"対応と設定差なしの両方に入っています: {sorted(both)}")
    for slug, cfg in d["configs"].items():
        if not _SLUG.match(str(slug)) or not isinstance(cfg, dict):
            raise PochiError(f"控えの機種名か中身が壊れています: {slug!r}")
        extra = set(cfg) - {"settings", "fields", "rates", "note", "resultNote", "settingLabels"}
        pr = config_problems(cfg) + ([f"知らない項目 {sorted(extra)}"] if extra else [])
        if pr:
            raise PochiError(f"控えの {slug} の表が壊れています: " + " ／ ".join(pr[:3]))
    for slug in d["no_diff"]:
        if not _SLUG.match(str(slug)):
            raise PochiError(f"控えの機種名が壊れています: {slug!r}")
    # ★小役の確認の記録★（無い控えは古い形＝空として読む）
    kc = d.setdefault("ko_checked", {})
    if not isinstance(kc, dict):
        raise PochiError("控えの ko_checked が組（辞書）ではありません")
    extra_top = set(d) - {"schema_version", "configs", "no_diff", "ko_checked",
                          "hand_checked", "diff_checked"}
    if extra_top:
        raise PochiError(f"控えに知らない項目があります: {sorted(extra_top)}")
    # ★手書きの機種と「設定差なし」の機種を2AIが確かめ直した記録★（2026-11-10）
    for key in ("hand_checked", "diff_checked"):
        recs = d.setdefault(key, {})
        if not isinstance(recs, dict):
            raise PochiError(f"控えの {key} が組（辞書）ではありません")
        for slug, rec in recs.items():
            if (not _SLUG.match(str(slug)) or not isinstance(rec, dict) or set(rec) != {"checked"}
                    or not _DATE.match(str(rec.get("checked") or ""))):
                raise PochiError(f"控えの {key} の記録が壊れています: {slug!r}")
            try:
                datetime.date.fromisoformat(rec["checked"])
            except ValueError:
                raise PochiError(f"控えの {key} の日付が壊れています: {slug!r}")
    for slug in d["hand_checked"]:
        if slug in d["configs"]:
            raise PochiError(f"自動へ移した機種に手書きの確認の記録が残っています: {slug!r}")
    for slug, rec in kc.items():
        if slug not in d["configs"]:
            raise PochiError(f"表の無い機種に小役の確認の記録があります: {slug!r}")
        if (not isinstance(rec, dict) or set(rec) != {"result", "checked"}
                or rec.get("result") not in KO_RESULTS
                or not _DATE.match(str(rec.get("checked") or ""))):
            raise PochiError(f"控えの小役の確認の記録が壊れています: {slug!r}")
        try:
            datetime.date.fromisoformat(rec["checked"])
        except ValueError:
            raise PochiError(f"控えの小役の確認の日付が壊れています: {slug!r}")
    return d


def _json_bytes(obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_bytes(path: str, data: bytes) -> bytes:
    """一時ファイルに書いて置き換える。★書いたバイト列そのものを返す★（読み直さない）"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with io.open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return data


def _write_json(path: str, obj) -> bytes:
    return _write_bytes(path, _json_bytes(obj))


def status_override(slug: str, store: dict = None):
    """★控えが決める対応・非対応★（無ければ None＝今までどおりの決まり）"""
    try:
        d = store if store is not None else load()
    except Exception:                      # noqa: BLE001
        return None                        # ★読めないなら上書きしない（今までどおり）★
    if slug in d.get("configs", {}):
        return (True, "")
    if slug in d.get("no_diff", {}):
        return (False, NO_DIFF_REASON)
    return None


def ko_due(slug: str, store: dict, today: datetime.date = None, turn: bool = False):
    """★自動で足した機種の、小役の差をもう一度探す番か★（理由の文 か None）

    ★見るのは2AIの記録だけ★（どの項目が小役かを機械は判定しない）。
    手で書いた機種・控えに表の無い機種は None（ここでは扱わない）。
    """
    if slug not in store.get("configs", {}):
        return None
    rec = (store.get("ko_checked") or {}).get(slug)
    if not rec:
        return "小役の差を2AIでまだ探していない"
    if turn and rec["checked"] != (today or datetime.date.today()).isoformat():
        return "担当の回（間隔に関係なく、小役の差・表の訂正を確かめ直す：足すなら EXTEND・直すなら ADD・変わらなければ KO_CHECK）"
    if rec["result"] == "NONE":
        today = today or datetime.date.today()
        days = (today - datetime.date.fromisoformat(rec["checked"])).days
        if days >= KO_RECHECK_DAYS:
            return f"小役の差が見つからなかった日から{days}日たった（{KO_RECHECK_DAYS}日でもう一度探す）"
    # ★★HAS も確かめ直す★★（2026-11-10・洗い出し2回目）＝直す前は一度「ある」と決めると二度と見なかった
    #   （解析の訂正や小役の追加が反映されない）。2AIは EXTEND で足すか、KO_CHECK（ko=HAS）で日付だけ控える
    elif rec["result"] == "HAS":
        today = today or datetime.date.today()
        days = (today - datetime.date.fromisoformat(rec["checked"])).days
        if days >= KO_HAS_RECHECK_DAYS:
            return (f"小役の差を確かめた日から{days}日たった（{KO_HAS_RECHECK_DAYS}日で確かめ直す："
                    "小役の追加は EXTEND・値の訂正は ADD（表全体）・変わらなければ KO_CHECK で ko=HAS）")
    return None


# ── setting.html の自動の区画 ─────────────────────────────────────────
def _num(v: str) -> str:
    return "1/" + _RATE.match(v).group(1)


def render_block(store: dict) -> str:
    """控えの configs を MACHINE_CONFIGS の項目として書く（毎回同じ文字になる）"""
    out = [BEGIN]
    for slug in sorted(store["configs"]):
        c = store["configs"][slug]
        lines = [f"  {slug}: {{"]
        lines.append("    settings: " + json.dumps(c["settings"]) + ",")
        if c.get("settingLabels"):
            sl = c["settingLabels"]
            lines.append("    settingLabels: {" + ", ".join(
                f"{k}:" + json.dumps(sl[k], ensure_ascii=False)
                for k in sorted(sl, key=int)) + "},")
        if c.get("note"):
            lines.append("    note: " + json.dumps(c["note"], ensure_ascii=False) + ",")
        lines.append("    fields: [")
        for f in c["fields"]:
            item = {"id": f["id"], "label": f["label"], "sub": f.get("sub", ""),
                    "rateKey": f["rateKey"]}
            if f.get("ref"):
                item["ref"] = True
            lines.append("      " + json.dumps(item, ensure_ascii=False) + ",")
        lines.append("    ],")
        if c.get("resultNote"):
            lines.append("    resultNote: "
                         + json.dumps(c["resultNote"], ensure_ascii=False) + ",")
        lines.append("    rates: {")
        for rk in sorted(c["rates"]):
            tab = c["rates"][rk]
            body = ",".join(f"{s}:{_num(tab[str(s)])}" for s in c["settings"])
            lines.append(f"      {rk}: {{{body}}},")
        lines.append("    }")
        lines.append("  },")
        out += lines
    out.append(END)
    return "\n".join(out)


def _region(html: str):
    a, b = html.find(BEGIN), html.find(END)
    if a < 0 or b < 0 or b < a or html.count(BEGIN) != 1 or html.count(END) != 1:
        raise PochiError("setting.html に自動の区画の印がちょうど1組ありません")
    return a, b + len(END)


def build(html: str, store: dict) -> str:
    a, b = _region(html)
    return html[:a] + render_block(store) + html[b:]


def static_slugs(html: str) -> set:
    """★人が手で書いた MACHINE_CONFIGS の機種★（自動の区画は除く）"""
    import extract_setting_rates as _es
    a, b = _region(html)
    rest = html[:a] + html[b:]
    m = re.search(r"const MACHINE_CONFIGS\s*=\s*\{", rest)
    if not m:
        raise PochiError("setting.html に MACHINE_CONFIGS がありません")
    body, _ = _es._find_block(rest, m.end() - 1)
    return set(_es._MACHINE_BLOCK.findall(body))


def check_problems(html: str = None, store: dict = None) -> list:
    """★控えと setting.html がそろっているか★（そろっていなければ公開しない）"""
    try:
        html = html if html is not None else io.open(SETTING, encoding="utf-8").read()
        store = store if store is not None else load()
        out = []
        if build(html, store) != html:
            out.append("setting.html の自動の区画が控えと違います"
                       "（python scripts/pochipochi.py --build で作り直す）")
        st = static_slugs(html)
        dup = set(store["configs"]) & st
        if dup:
            out.append(f"手で書いた機種と自動の機種が重なっています: {sorted(dup)}")
        # ★手で書いた表がある機種を「設定差なし」にしていないか★（Codex review238）
        nd = set(store["no_diff"]) & st
        if nd:
            out.append(f"表がある機種が「設定差なし」になっています: {sorted(nd)}")
        return out
    except Exception as e:                 # noqa: BLE001
        return [f"ポチポチくんの控えを確かめられません: {type(e).__name__}: {e}"]


# ── 決定の検査 ───────────────────────────────────────────────────────
def _text_problems(name: str, s, limit: int) -> list:
    import gates as _g
    if not isinstance(s, str):
        return [f"{name} は文字で書きます"]
    if len(s) > limit:
        return [f"{name} は{limit}字までです"]
    # ★タグも記号も入れない★＝ページは innerHTML で描く（許可タグでも見た目が変わる）
    # ★引用符も入れない★（Codex review238＝ラベルは属性の中にも入る＝属性を足せた）
    if any(c in s for c in "<>&\x60\"'\\") or _g.html_unsafe(s) or _g.invisible_unsafe(s):
        return [f"{name} に使えない文字やタグがあります"]
    return []


def shape_problems(d: dict, rows: dict, static: set) -> list:
    """★形だけを見る★（通信しない＝試験しやすい）"""
    import confirmed_values as _cv
    p = []
    slug = str(d.get("slug") or "")
    if not _SLUG.match(slug) or slug not in rows:
        return [f"機種一覧に無い slug です: {slug!r}"]
    kind = d.get("decision")
    if kind not in ("ADD", "NO_DIFF", "EXTEND", "KO_CHECK", "TAKE_OVER", "HAND_KEEP", "DIFF_CHECK"):
        return ["decision は ADD / NO_DIFF / EXTEND / KO_CHECK / TAKE_OVER / HAND_KEEP / DIFF_CHECK のどれかです"]
    if _cv.judges_pair(d.get("by") or []) is None:
        p.append("判断者の組がそろっていません（claude と codex、または代役の2人）")
    if len(str(d.get("why") or "").strip()) < MIN_WHY:
        p.append(f"理由（why）は{MIN_WHY}字以上で書きます")
    # ★★手書きの機種は TAKE_OVER（確かめ直して自動へ移す）と HAND_KEEP（いまは移さない）だけ★★（2026-11-10）
    if kind in ("TAKE_OVER", "HAND_KEEP"):
        if slug not in static:
            p.append(f"{kind} は、手で書いた対応がある機種にだけ使えます")
    elif slug in static:
        p.append("手で書いた対応がある機種です（ここでは上書きしません。確かめ直すなら TAKE_OVER / HAND_KEEP）")
    if kind in ("HAND_KEEP", "DIFF_CHECK"):
        # ★表を変えないので出典は任意★。何を確かめたかは why に書く
        extra = set(d) & {"settings", "fields", "rates", "note", "resultNote", "settingLabels", "ko"}
        if extra:
            p.append(f"{kind} では表を書きません: {sorted(extra)}")
        if not isinstance(d.get("sources", []), list):
            p.append("出典（sources）は並びで書きます")
        return p
    if "ko" in d and d.get("ko") not in KO_RESULTS:
        p.append("ko は HAS か NONE です")
    if kind == "KO_CHECK":
        # ★表を変えないので出典は任意★（「見つからない」は引用できない）。何を探したかは why に書く
        if d.get("ko") not in KO_RESULTS:
            p.append("KO_CHECK には ko（HAS か NONE）が要ります")
        extra = set(d) & {"settings", "fields", "rates", "note", "resultNote", "settingLabels"}
        if extra:
            p.append(f"KO_CHECK では表を書きません: {sorted(extra)}")
        if not isinstance(d.get("sources", []), list):
            p.append("出典（sources）は並びで書きます")
        return p
    if kind in ("NO_DIFF", "EXTEND") and "ko" in d:
        p.append(f"{kind} には ko を書きません")
    srcs = d.get("sources") or []
    if not isinstance(srcs, list) or not srcs:
        p.append("出典（sources）が要ります")
    if kind == "NO_DIFF":
        return p
    if kind == "EXTEND" and "settingLabels" in d:
        p.append("EXTEND では settingLabels を変えません")
    return p + config_problems(d)


def status_line(slug: str, state: tuple, store: dict, static: set, today=None, turn: bool = False) -> str:
    """★STEP 1-P の対象かを1行で★（純関数＝試験はここを直接呼ぶ）

    対象は4通り＝①使えない（設定差なしと決まっていない）②自動の機種で小役をまだ探していない（ko_due）
    ③★手書きの機種で、2AIが確かめ直す番★（TAKE_OVER / HAND_KEEP）④★「設定差なし」で、確かめ直す番★（DIFF_CHECK / ADD）
    （③④は2026-11-10・運営者「更新されないページ他にもないの？」＝直す前は一度決まると二度と見直されなかった）
    """
    ok, why = state
    head = f"{slug}: {'使える' if ok else '使えない（' + why + '）'}"
    # ★★担当が回ってきた機種（turn）は、間隔に関係なく確かめ直す★★（2026-11-10・運営者「その機種を一つ一つ完璧に」）
    #   ＝今日もう確かめた記録があるときだけ対象外。間隔（60・90・180日）は担当が回ってこないときの保険
    if slug in static:
        if recheck_due(store, "hand_checked", slug, today, turn):
            return head + " → STEP 1-P 対象（手書き：2AIで確かめ直す・TAKE_OVER か HAND_KEEP）"
        return head + " → STEP 1-P 対象外"
    if ok:
        ko = ko_due(slug, store, today, turn)
        return head + (f" → STEP 1-P 対象（小役：{ko}）" if ko else " → STEP 1-P 対象外")
    if why == NO_DIFF_REASON:
        if recheck_due(store, "diff_checked", slug, today, turn):
            return head + " → STEP 1-P 対象（設定差なし：2AIで確かめ直す・ADD か DIFF_CHECK）"
        return head + " → STEP 1-P 対象外"
    return head + " → STEP 1-P 対象"


def remove_static_block(html: str, slug: str) -> str:
    """★手で書いた MACHINE_CONFIGS の1機種ぶんを取り除いた html★（TAKE_OVER 用・見つからなければ例外）

    探すのは MACHINE_CONFIGS の中の、自動の区画の外だけ（`static_slugs` と同じ見方）。
    取り除くのは「  slug: {」から対応する「}」とその後ろの「,」と改行まで。
    """
    import extract_setting_rates as _es
    m = re.search(r"const MACHINE_CONFIGS\s*=\s*\{", html)
    if not m:
        raise PochiError("setting.html に MACHINE_CONFIGS がありません")
    body, end = _es._find_block(html, m.end() - 1)
    lo, hi = m.end() - 1, end
    a, b = _region(html)
    pat = re.compile(r"(?m)^  " + re.escape(slug) + r"\s*:\s*\{")
    hits = [x for x in pat.finditer(html, lo, hi) if not (a <= x.start() < b)]
    if len(hits) != 1:
        raise PochiError(f"手で書いた {slug} がちょうど1つ見つかりません（{len(hits)}件）")
    start = hits[0].start()
    block, j = _es._find_block(html, hits[0].end() - 1)
    if not block:
        raise PochiError(f"手で書いた {slug} の終わりが見つかりません")
    if html[j:j + 1] == ",":
        j += 1
    if html[j:j + 2] == "\r\n":
        j += 2
    elif html[j:j + 1] == "\n":
        j += 1
    return html[:start] + html[j:]


def recheck_due(store: dict, key: str, slug: str, today: datetime.date = None, turn: bool = False) -> bool:
    """★手書き・設定差なしの機種を、2AIが確かめ直す番か★（記録が無いか HAND_RECHECK_DAYS 日たった／担当の回なら今日まだ見ていない）"""
    rec = (store.get(key) or {}).get(slug)
    if not rec:
        return True
    today = today or datetime.date.today()
    if turn:
        return rec["checked"] != today.isoformat()
    return (today - datetime.date.fromisoformat(rec["checked"])).days >= HAND_RECHECK_DAYS


def config_problems(d: dict) -> list:
    """★表そのものの形★（決定を受け取るときも、控えを読むときも同じ関数を通す）"""
    p = []
    st = d.get("settings")
    if (not isinstance(st, list) or len(st) < 2 or len(set(st)) != len(st)
            or any((not isinstance(x, int)) or isinstance(x, bool) or not 1 <= x <= 6
                   for x in st) or st != sorted(st)):
        p.append("settings は 1〜6 の段階を小さい順に2つ以上（実在する段階だけ）")
        return p
    fields = d.get("fields")
    if not isinstance(fields, list) or not fields or len(fields) > 12:
        return p + ["fields は1〜12項目で書きます"]
    ids, keys = set(), []
    for f in fields:
        if not isinstance(f, dict):
            p.append("fields の各項目は組で書きます")
            continue
        if not _ID.match(str(f.get("id") or "")) or f["id"] in ids:
            p.append(f"項目の id が使えないか重なっています: {f.get('id')!r}")
        ids.add(f.get("id"))
        if not _ID.match(str(f.get("rateKey") or "")) or f.get("rateKey") in keys:
            # ★重ねない★（Codex review238＝同じ表を2回掛けて期待度を誤計算する）
            p.append(f"rateKey が使えないか重なっています: {f.get('rateKey')!r}")
        keys.append(f.get("rateKey"))
        p += _text_problems("label", f.get("label"), 20)
        if not str(f.get("label") or "").strip():
            p.append("label が空です")
        p += _text_problems("sub", f.get("sub", ""), 30)
        if "ref" in f and not isinstance(f["ref"], bool):
            p.append("ref は真偽値です")
    rates = d.get("rates")
    if not isinstance(rates, dict) or set(rates) != set(keys):
        return p + ["rates の鍵は fields の rateKey とちょうど同じにします"]
    want = {str(x) for x in st}
    for rk, tab in rates.items():
        if not isinstance(tab, dict) or set(tab) != want:
            p.append(f"{rk}: 表はちょうど settings の段階を持ちます（足りない・余る段階は不可）")
            continue
        for s, v in tab.items():
            m = _RATE.match(str(v))
            if not m or float(m.group(1)) <= 1:
                p.append(f"{rk}: 設定{s}の値は 1/x の形で書きます: {v!r}")
    for k in ("note", "resultNote"):
        if k in d:
            p += _text_problems(k, d[k], 200)
    # ★数字でない設定名（設定V など）★（Codex review238）＝内部の段階 → 画面の名前
    sl = d.get("settingLabels")
    if sl is not None:
        if not isinstance(sl, dict) or not sl or not set(sl) <= want:
            p.append("settingLabels は settings の段階 → 名前の組で書きます")
        else:
            for k, v in sl.items():
                p += _text_problems("settingLabels", v, 8)
                if not str(v or "").strip():
                    p.append("settingLabels の名前が空です")
    return p


def support_problems(d: dict, sources: list) -> list:
    """★表ごとに、独立した2つの出典の逐語に「小役の名前」と全部の値が在るか★

    ★名前も求める★（Codex review238）＝値だけだと、複数の小役の表を含む引用で
    ベルとチェリーの表を取り違えても通った。名前は画面に出る label を逐語で探す
    （出典の呼び方に合わせて label を付ける）。どの設定の値かの対応は2AIが確かめる。
    """
    import confirmed_values as _cv
    p = []
    label = {f.get("rateKey"): str(f.get("label") or "") for f in (d.get("fields") or [])}
    # ★出典ごとの呼び名★（2026-10-20・一字一句の決まりを2AI判断へ）＝サイトによって
    #   「初当り」「初当たり」のように書き方が違うと、画面の名前が両方の引用にそのまま在る
    #   ことは満たせず、2AIが値まで一致させても通せなかった。sources[].labels に
    #   {rateKey: そのサイトでの呼び名} を書けば、その呼び名で探す。
    #   ★言うだけでは通さない★＝その呼び名は、その出典の引用にそのまま在ること。
    for s in sources:
        for rk, nm in (s.get("labels") or {}).items():
            if rk not in label:
                p.append(f"出典の呼び名に、表に無い項目があります: {rk!r}")
            p += _text_problems("出典の呼び名", nm, 20)

    def _name_in(s, rk):
        nm = _cv.page_form(str((s.get("labels") or {}).get(rk) or label.get(rk, "")))
        return bool(nm) and nm in _cv.page_form(s.get("quote") or "")
    for rk, tab in (d.get("rates") or {}).items():
        sup = [s for s in sources
               if _name_in(s, rk)
               and all(_cv.token_in_quote(str(v), s.get("quote") or "")
                       for v in tab.values())]
        try:
            _cv.check_sources(sup)
        except _cv.ConfirmedError as e:
            p.append(f"{rk}: 表の値を全部持つ逐語が、独立した2つの出典にありません（{e}）")
    return p


def _parse_sources(raw: list) -> list:
    import confirmed_values as _cv
    out = []
    for s in raw:
        if isinstance(s, str):
            out.append(_cv.parse_source(s))
        elif isinstance(s, dict):
            ps = _cv.parse_source(f"{s.get('url', '')}|{s.get('quote', '')}")
            # ★通称で載せるページの同定の根拠を落とさない★（Codex review238）
            for k in ("identity_why", "identity_proof"):
                if s.get(k):
                    ps[k] = s[k]
            if "labels" in s:
                if not isinstance(s["labels"], dict):
                    raise PochiError("出典の labels は {rateKey: 呼び名} の組で書きます")
                ps["labels"] = {str(k): v for k, v in s["labels"].items()}
            out.append(ps)
        else:
            raise PochiError("出典は 'URL|逐語' か {url, quote} で書きます")
    return out


def extend_problems(d: dict, cur: dict) -> list:
    """★EXTEND は今ある表に足すだけ★（今ある表を書き換えない・消さない）"""
    p = []
    if d.get("settings") != cur.get("settings"):
        p.append(f"EXTEND の settings は今の表と同じにします: {cur.get('settings')}")
    # ★今ある項目との重なりは、足したあとの表で見る★（id・rateKey の重なりを config_problems が断る
    #   ＝確かめた表を別の値で上書きできない）
    p += config_problems(merged_config(cur, d))
    return p


def merged_config(cur: dict, d: dict) -> dict:
    """今ある表＋足す表（note・resultNote は書いてあれば置き換える）"""
    m = json.loads(json.dumps(cur))
    m["fields"] = m["fields"] + json.loads(json.dumps(d.get("fields") or []))
    m["rates"].update(json.loads(json.dumps(d.get("rates") or {})))
    for k in ("note", "resultNote"):
        if d.get(k):
            m[k] = d[k]
    return m


def _rows() -> dict:
    return {m["slug"]: m for m in _sj.read_rows(MACHINES)}


def hand_no_diff() -> set:
    """★記事ページの手作りの名簿（machine.html の noSettingDiff）で「設定差なし」の機種★"""
    import build_machine_pages as _b
    reasons = _b.extract_pochipochi_reasons(io.open(
        os.path.join(BASE, "machine.html"), encoding="utf-8").read())
    return {s for s, r in reasons.items() if isinstance(s, str) and r == NO_DIFF_REASON}


def plan(d: dict, fetch=None, store: dict = None, html: str = None,
         rows: dict = None, no_diff_hand: set = None) -> dict:
    """★書く前に全部確かめる★（1つでも外れたら何も書かない）"""
    import confirmed_values as _cv
    rows = rows if rows is not None else _rows()
    html = html if html is not None else io.open(SETTING, encoding="utf-8").read()
    store = store if store is not None else load()
    if d.get("decision") == "DIFF_CHECK":
        nd = no_diff_hand if no_diff_hand is not None else hand_no_diff()
        if str(d.get("slug")) not in nd and str(d.get("slug")) not in store.get("no_diff", {}):
            return {"ok": False, "problems": ["DIFF_CHECK は「小役による設定差なし」の機種にだけ使えます"]}
    p = shape_problems(d, rows, static_slugs(html))
    if p:
        return {"ok": False, "problems": p}
    try:
        sources = _parse_sources(d.get("sources") or [])
    except Exception as e:                 # noqa: BLE001
        return {"ok": False, "problems": [f"出典を読めません: {e}"]}
    slug = d["slug"]
    cur = store["configs"].get(slug)
    if d["decision"] in ("EXTEND", "KO_CHECK") and cur is None:
        return {"ok": False, "problems": [f"{d['decision']} は、自動で足した表がある機種にだけ使えます"]}
    if d["decision"] == "EXTEND":
        p += extend_problems(d, cur)
        if p:
            return {"ok": False, "problems": p}
    if d["decision"] in ("ADD", "EXTEND", "TAKE_OVER"):
        p += support_problems(d, sources)
        if p:
            return {"ok": False, "problems": p}
    name = rows[d["slug"]].get("name") or d["slug"]
    try:
        verified = [_cv.verify_source(dict(s), name, fetch, judged_by=d["by"]) for s in sources]
    except _cv.ConfirmedError as e:
        return {"ok": False, "problems": [f"出典を確かめられません: {e}"]}
    new = json.loads(json.dumps(store))
    for _k in ("ko_checked", "hand_checked", "diff_checked"):
        new.setdefault(_k, {})
    today = datetime.date.today().isoformat()
    base_html = html
    if d["decision"] in ("ADD", "TAKE_OVER"):
        cfg = {"settings": d["settings"], "fields": d["fields"], "rates": d["rates"]}
        for k in ("note", "resultNote", "settingLabels"):
            if d.get(k):
                cfg[k] = d[k]
        new["configs"][slug] = cfg
        new["no_diff"].pop(slug, None)
        new["diff_checked"].pop(slug, None)
        new["hand_checked"].pop(slug, None)
        # ★小役の確認は、書いたときだけ記録★（書かなければ次に担当したときにまた探す）
        if d.get("ko") in KO_RESULTS:
            new["ko_checked"][slug] = {"result": d["ko"], "checked": today}
        else:
            new["ko_checked"].pop(slug, None)
        if d["decision"] == "TAKE_OVER":
            # ★★手で書いた表を、2AIが確かめ直した表で置き換えて自動の区画へ移す★★（2026-11-10）
            try:
                base_html = remove_static_block(html, slug)
            except PochiError as e:
                return {"ok": False, "problems": [str(e)]}
    elif d["decision"] == "HAND_KEEP":
        new["hand_checked"][slug] = {"checked": today}
    elif d["decision"] == "DIFF_CHECK":
        new["diff_checked"][slug] = {"checked": today}
    elif d["decision"] == "EXTEND":
        new["configs"][slug] = merged_config(cur, d)
        new["ko_checked"][slug] = {"result": "HAS", "checked": today}
    elif d["decision"] == "KO_CHECK":
        new["ko_checked"][slug] = {"result": d["ko"], "checked": today}
    else:
        if slug in new["configs"]:
            return {"ok": False, "problems": ["対応済みの機種を「設定差なし」にはしません"
                                              "（表を消す道具にしない）"]}
        new["no_diff"][slug] = {"reason": NO_DIFF_REASON}
        new["diff_checked"][slug] = {"checked": today}
    basis = {"decision": d["decision"], "by": sorted(_cv._judge_names(d["by"])),
             **({"ko": new["ko_checked"][slug]["result"]} if slug in new["ko_checked"]
                and d["decision"] != "NO_DIFF" else {}),
             "why": str(d["why"]).strip()[:300],
             "decided_at": datetime.date.today().isoformat(),
             "sources": verified}
    return {"ok": True, "problems": [], "store": new, "html": build(base_html, new),
            "basis": basis}


def _os_lock(fd: int) -> bool:
    """★OSのファイルロック★（持ち主の処理が終われば OS が外す＝残骸が残らない）"""
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _os_unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


class _ApplyLock:
    """★書き込みを1つずつにする★（Codex review240）＝2つの --apply が同時に走ると、
    どちらも同じ控えを読んでから書くので、片方の対応が黙って消える。
    ★OSのファイルロックを使う★（Codex review241）＝印のファイルの有無で決める形だと、
    古い印を2つが同時に取り直して両方とも書けた。OSのロックは持ち主の処理が終われば外れるので、
    「古い印」を見分ける必要そのものが無い（ファイルは消さずに残してよい）。"""

    def __init__(self):
        self.path = LOCK
        self.fd = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR)
        if not _os_lock(fd):
            os.close(fd)
            raise PochiError("ほかの書き込みが進行中です（印: " + self.path + "）。終わってからやり直します")
        self.fd = fd
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            _os_unlock(self.fd)
            os.close(self.fd)
            self.fd = None
        return False


def apply(path: str, dry: bool = False, fetch=None) -> dict:
    d = _sj.read_json(path, expect=dict)
    if dry:
        return plan(d, fetch=fetch)
    try:
        lock = _ApplyLock().__enter__()
    except PochiError as e:
        return {"ok": False, "problems": [str(e)], "written": False, "rebuilt": False}
    try:
        return _apply_locked(d, fetch)
    finally:
        lock.__exit__(None, None, None)


def _apply_locked(d: dict, fetch) -> dict:
    # ★控えと setting.html は印を取ってから読む★（取る前に読むと、待っている間の更新を消す）
    r = plan(d, fetch=fetch)
    if not r["ok"]:
        return r
    # ★★全部書けたときだけ残す★★（Codex review239）＝記事ページの作り直しが失敗したら、
    #   控え・setting.html・根拠の控えを書く前の姿へ戻す（中途半端な状態を残さない）。
    # ★★戻すのは「自分が書いたまま」のものだけ★★（Codex review240）＝書いたあとに
    #   別の処理（人の手・ほかのセッション）が書き換えていたら、戻さずに知らせる。
    #   ★記事ページは戻さない★＝作り直しは最後に1回だけ置き換える作りなので、失敗した回は
    #   ページに触れていない。変わっていたら、それは別の処理の更新（消さずに知らせる）。
    page = os.path.join(BASE, "machines", d["slug"], "index.html")

    def _snap(f):
        return io.open(f, "rb").read() if os.path.isfile(f) else None
    keep = {f: _snap(f) for f in (BASIS, STORE, SETTING)}
    page_before = _snap(page)
    wrote = {}
    try:
        b = _sj.read_json(BASIS, expect=dict, allow_missing=True, default={}) or {}
        b.setdefault(d["slug"], []).append(r["basis"])
        # ★書いたバイト列そのものを控える★（Codex review241）＝書いたあとに読み直すと、
        #   その一瞬に他人が書いた中身を「自分が書いたもの」と取り違えて、戻すときに消す。
        wrote[BASIS] = _write_json(BASIS, b)
        wrote[STORE] = _write_json(STORE, r["store"])
        wrote[SETTING] = _write_bytes(SETTING, r["html"].encode("utf-8"))
        # ★★その機種の記事ページも作り直す★★＝ページは案内の有効・無効を焼き込んでいるので、
        #   控えだけ書くと、次に作り直されるまで古い案内のまま（書き忘れを道具の側で防ぐ）。
        ok = rebuild_page(d["slug"])
    except Exception as e:                 # noqa: BLE001
        ok = False
        r["problems"] = [f"書く途中で失敗しました: {type(e).__name__}: {e}"]
    if not ok:
        left = _restore(keep, wrote)
        r["ok"] = False
        r.setdefault("problems", []).append(
            "記事ページを作り直せなかったので、書いたものを元に戻しました")
        for f in left:
            r["problems"].append(f"★別の処理が書き換えていたので戻していません: {f}★")
        if _snap(page) != page_before:
            r["problems"].append(f"★記事ページが途中で別の処理に書き換えられています（触っていません）: {page}★")
        r["written"] = r["rebuilt"] = False
        return r
    r["written"] = r["rebuilt"] = True
    return r


def _restore(keep: dict, wrote: dict) -> list:
    """★自分が書いたままのものだけを戻す★。戻さなかったファイルの一覧を返す"""
    left = []
    for f, data in keep.items():
        if f not in wrote:
            continue                       # ★書いていないものには触らない★
        now = io.open(f, "rb").read() if os.path.isfile(f) else None
        if now != wrote[f]:
            left.append(f)
            continue
        if data is None:
            os.remove(f)
        else:
            with io.open(f, "wb") as fh:
                fh.write(data)
    return left


def rebuild_page(slug: str) -> bool:
    """新台は --rebuild-auto、旧形式は --legacy --slug と同じ道で1枚だけ作り直す"""
    import build_machine_pages as _b
    import page_decision as _pd
    row = _rows().get(slug) or {}
    if _pd.is_auto(row):
        return (_b._rebuild_auto(slug) or 0) == 0
    return (_b.main(False, True, slug) or 0) == 0


# ── 自己試験 ─────────────────────────────────────────────────────────
def selftest() -> int:
    import confirmed_values as _cv
    import tempfile
    ok = [0, 0]

    def t(name, cond):
        ok[0 if cond else 1] += 1
        print(("✅ " if cond else "❌ ") + name)

    html = ("const MACHINE_CONFIGS = {\n" + BEGIN + "\n" + END + "\n"
            "  hokuto: {\n    rates: {\n      bb: {1:1/383.4,6:1/235.1}\n    }\n  },\n};\n")
    rows = {"zzz_a": {"slug": "zzz_a", "name": "試験機A"},
            "hokuto": {"slug": "hokuto", "name": "北斗"}}
    q1 = "試験機A 中段チェリー 設定1 1/210.1 設定6 1/190.5 の確率です。"
    q2 = "試験機Aの中段チェリーは設定1が1/210.1、設定6が1/190.5となっています。"
    page = "<html><head><title>試験機A</title></head><body><h1>試験機A</h1><p>%s</p><p>%s</p></body></html>" % (q1, q2)
    good = {"slug": "zzz_a", "decision": "ADD", "by": ["claude", "codex"],
            "why": "2つの出典の設定別の表を突き合わせて一致しました",
            "settings": [1, 6],
            "fields": [{"id": "cherry", "label": "中段チェリー", "sub": "通常時のみ",
                        "rateKey": "cherry"}],
            "rates": {"cherry": {"1": "1/210.1", "6": "1/190.5"}},
            "sources": [{"url": "https://chonborista.com/slot/zzz/", "quote": q1},
                        {"url": "https://nana-press.com/kaiseki/zzz/", "quote": q2}]}
    st = empty()
    r = plan(json.loads(json.dumps(good)), fetch=lambda u: page, store=st, html=html, rows=rows)
    t("★★2AIの決定が形と2出典の照合を通れば、自動の区画に書く★★",
      r["ok"] and "zzz_a: {" in r["html"] and "cherry: {1:1/210.1,6:1/190.5}" in r["html"])
    t("　書いた区画は、ページが読む形（extract_setting_rates）で読める",
      r["ok"] and __import__("extract_setting_rates").extract_rates(r["html"]).get(
          "zzz_a", {}).get("cherry") == {"1": "1/210.1", "6": "1/190.5"})
    t("　手で書いた機種はそのまま残る", r["ok"] and "hokuto: {" in r["html"])
    t("　同じ控えから作れば毎回同じ文字（2回目は何も変わらない）",
      r["ok"] and build(r["html"], r["store"]) == r["html"])
    t("　作った控えと区画はそろっている", r["ok"] and check_problems(r["html"], r["store"]) == [])
    t("　出典の引用と判断者は外の控えへ（読者のデータには入れない）",
      r["ok"] and "sources" not in json.dumps(r["store"]) and r["basis"]["sources"])

    def bad(mut, why):
        d = json.loads(json.dumps(good))
        mut(d)
        rr = plan(d, fetch=lambda u: page, store=empty(), html=html, rows=rows)
        t(why, not rr["ok"])

    bad(lambda d: d.update(by=["claude"]), "　判断者が1人なら断る")
    bad(lambda d: d.update(why="短い"), "　理由が短ければ断る")
    # ★形の守りは形の関数を直接呼ぶ★（plan を通すと、隣の守り＝逐語の照合や本人確認が
    #   先に断り、狙った守りを一度も試さない＝罠④）
    def shape_bad(mut, why, must):
        d = json.loads(json.dumps(good))
        mut(d)
        pr = shape_problems(d, rows, static_slugs(html))
        t(why, any(must in x for x in pr))

    shape_bad(lambda d: d["rates"]["cherry"].pop("6"), "★存在しない・欠けた段階の表は断る★",
              "ちょうど settings の段階")
    shape_bad(lambda d: d["rates"]["cherry"].update({"3": "1/200.0"}),
              "　settings に無い段階を持つ表は断る", "ちょうど settings の段階")
    shape_bad(lambda d: d.update(slug="hokuto"), "★手で書いた機種は上書きしない（形の段で断る）★",
              "手で書いた対応")
    bad(lambda d: d["rates"]["cherry"].update({"1": "0.0047"}), "　1/x の形でない値は断る")
    bad(lambda d: d.update(settings=[6, 1]), "　段階は小さい順に明示する")
    bad(lambda d: d["sources"].pop(), "★★出典が1つなら断る（独立2出典）★★")
    bad(lambda d: d["sources"].__setitem__(1, {"url": "https://chonborista.com/slot/zzz2/", "quote": q2}),
        "★同じ発行者の2ページは1票★")
    bad(lambda d: d["rates"]["cherry"].update({"6": "1/190.6"}),
        "★★表の値が逐語に無ければ断る（数字を作らない）★★")
    bad(lambda d: d["fields"][0].update(label="<b>x</b>"), "　ラベルにタグは使えない")
    shape_bad(lambda d: d["fields"][0].update(label='" hidden x="'),
              "★ラベルに引用符は使えない（属性を足せる）★", "使えない文字")
    shape_bad(lambda d: d["fields"].append({"id": "cherry2", "label": "中段チェリー",
                                            "sub": "", "rateKey": "cherry"}),
              "★同じ rateKey を2回使えない（同じ表を2回掛ける）★", "重なっています")
    bad(lambda d: d["fields"][0].update(label="弱スイカ"),
        "★★逐語にその小役の名前が無ければ断る（表の取り違え）★★")
    # ★★出典ごとの呼び名★★（2026-10-20・一字一句の決まりを2AI判断へ）
    q3 = "試験機Aの中段のチェリーは設定1が1/210.1、設定6が1/190.5です。"
    page3 = page.replace("</body>", "<p>%s</p></body>" % q3)

    def lab(srcs):
        d = json.loads(json.dumps(good))
        d["sources"] = srcs
        return plan(d, fetch=lambda u: page3, store=empty(), html=html, rows=rows)
    _s1 = {"url": "https://chonborista.com/slot/zzz/", "quote": q1}
    _s3 = {"url": "https://nana-press.com/kaiseki/zzz/", "quote": q3}
    t("　（対照）呼び名が違う出典は、呼び名を書かなければ通らない",
      not lab([_s1, _s3])["ok"])
    t("★★出典ごとの呼び名を書けば、書き方の違う2サイトでも通る★★",
      lab([_s1, dict(_s3, labels={"cherry": "中段のチェリー"})])["ok"])
    t("　その呼び名が、その出典の引用に無ければ通らない（言うだけでは通さない）",
      not lab([_s1, dict(_s3, labels={"cherry": "弱スイカ"})])["ok"])
    t("　表に無い項目の呼び名は断る",
      not lab([_s1, dict(_s3, labels={"cherry": "中段のチェリー", "bell": "ベル"})])["ok"])
    # ★記号の検査だけが断る形にする★（引用にも同じ記号を入れる＝呼び名は引用に在る・罠④）
    _q5 = '試験機Aの中段の"チェリーは設定1が1/210.1、設定6が1/190.5です。'
    t("　呼び名に記号は使えない（引用に在っても断る）",
      any("使えない文字" in x for x in support_problems(
          good, _parse_sources([_s1, {"url": _s3["url"], "quote": _q5,
                                      "labels": {"cherry": '中段の"チェリー'}}]))))
    # ★★正式名を書かないページは、判断者の組がそろえば2AIの判断として受け取る★★
    #   （confirmed_values.verify_source の judged_by。ここでは決定の by を渡す）
    page4 = ("<html><head><title>【試験A】解析まとめ</title></head><body><h1>【試験A】解析まとめ</h1>"
             "<p>%s</p><p>%s</p></body></html>" % (q1, q2))
    _d4 = json.loads(json.dumps(good))
    for s in _d4["sources"]:
        s.update(identity_proof="【試験A】解析まとめ",
                 identity_why="導入日とメーカーを2AIで突き合わせて同じ機種と判断した")
    _r4 = plan(_d4, fetch=lambda u: page4, store=empty(), html=html, rows=rows)
    t("★★正式名を書かないページでも、判断者の組がそろえば通る★★",
      _r4["ok"] and all((s.get("identity_override") or {}).get("core_in_proof") is False
                        for s in _r4["basis"]["sources"]))
    shape_bad(lambda d: d.update(settingLabels={"3": "設定3"}),
              "　settingLabels は settings の段階だけ", "settingLabels")
    _dl = json.loads(json.dumps(good))
    _dl["settingLabels"] = {"6": "設定V"}
    _rl = plan(_dl, fetch=lambda u: page, store=empty(), html=html, rows=rows)
    t("★設定V のような名前を書ける★", _rl["ok"] and 'settingLabels: {6:"設定V"},' in _rl["html"])
    _ps = _parse_sources([{"url": "https://chonborista.com/slot/zzz/", "quote": q1,
                           "identity_why": "理由", "identity_proof": "根拠"}])
    t("★通称のページの同定の根拠（identity_why / identity_proof）を落とさない★",
      _ps[0].get("identity_proof") == "根拠" and _ps[0].get("identity_why") == "理由")
    _st_nd = empty()
    _st_nd["no_diff"]["hokuto"] = {"reason": NO_DIFF_REASON}
    t("★表がある機種を「設定差なし」にしていたら止める★",
      bool(check_problems(build(html, _st_nd), _st_nd)))
    bad(lambda d: d.update(slug="hokuto"), "★手で書いた機種は上書きしない★")
    bad(lambda d: d.update(slug="zzz_none"), "　機種一覧に無い slug は断る")
    rr = plan(json.loads(json.dumps(good)), fetch=lambda u: "<html><body>別の文</body></html>",
              store=empty(), html=html, rows=rows)
    t("★★引用がページに実在しなければ断る（言うだけでは通さない）★★", not rr["ok"])

    nd = {"slug": "zzz_a", "decision": "NO_DIFF", "by": ["claude", "codex"],
          "why": "出典が小役による設定差は無いと明記していると確かめた",
          "sources": [{"url": "https://chonborista.com/slot/zzz/", "quote": q1}]}
    rn = plan(nd, fetch=lambda u: page, store=empty(), html=html, rows=rows)
    t("★NO_DIFF は「小役による設定差なし」として控える★",
      rn["ok"] and status_override("zzz_a", rn["store"]) == (False, NO_DIFF_REASON))
    rn2 = plan(nd, fetch=lambda u: page, store=r["store"], html=r["html"], rows=rows)
    t("★対応済みの機種を NO_DIFF にはしない（表を消す道具にしない）★", not rn2["ok"])
    t("★控えに表がある機種は使える／無い機種は今までどおり（None）★",
      status_override("zzz_a", r["store"]) == (True, "")
      and status_override("hokuto", r["store"]) is None)
    # ★記事ページ側の判定（build_machine_pages.pochipochi_state）も控えを先に見る★
    import build_machine_pages as _b
    _rs = {"zzz_a": "設定差のあるカウント対象小役が未判明のため非対応",
           _b.POCHI_DATA_KEY: frozenset({"zzz_n"}), "zzz_n": NO_DIFF_REASON}
    _auto = {"slug": "zzz_n", "publication_policy": "page-decision/v1"}
    def _st(row, rs):
        try:
            return _b.pochipochi_state(row, rs)
        except Exception as e:             # noqa: BLE001  ★落ちたら不合格として数える★
            return ("例外", type(e).__name__)
    t("★★控えで決めた機種は、新台でも控えの答えが先に出る★★",
      _st(_auto, _rs) == (False, NO_DIFF_REASON))
    _rs2 = dict(_rs)
    _rs2["zzz_n"] = ""
    t("　控えに表がある新台は「使える」", _st(_auto, _rs2) == (True, ""))
    t("　控えに表がある旧形式も「使える」（手作りの名簿より先）",
      _st({"slug": "zzz_n"}, _rs2) == (True, ""))
    # ★控えを読むときも表の形を確かめ直す★（手で書き換えた控えを --build で通さない）
    import tempfile as _tf
    _bad_store = json.loads(json.dumps(r["store"]))
    _bad_store["configs"]["zzz_a"]["fields"].append(
        {"id": "c2", "label": "x", "sub": "", "rateKey": "cherry"})
    _bp = os.path.join(_tf.mkdtemp(prefix="pochi_"), "p.json")
    _write_json(_bp, _bad_store)
    try:
        load(_bp)
        _ld = False
    except PochiError:
        _ld = True
    t("★★控えに壊れた表（rateKey の重なり）があれば、読む段で止める★★", _ld)
    _write_json(_bp, r["store"])
    t("　（対照）正しい控えは読める", load(_bp)["configs"].get("zzz_a") is not None)
    # ★★記事ページを作り直せなかったら、書いたものを全部戻す★★（本物の apply を写しの上で通す）
    g = globals()
    _keep = {k: g[k] for k in ("BASE", "STORE", "SETTING", "BASIS", "LOCK", "_rows", "_write_bytes",
                               "rebuild_page")}
    _tmp = _tf.mkdtemp(prefix="pochi_tx_")
    try:
        os.makedirs(os.path.join(_tmp, "machines", "zzz_a"))
        _pg = os.path.join(_tmp, "machines", "zzz_a", "index.html")
        io.open(_pg, "w", encoding="utf-8").write("元のページ")
        g["BASE"] = _tmp
        g["STORE"] = os.path.join(_tmp, "p.json")
        g["SETTING"] = os.path.join(_tmp, "setting.html")
        g["BASIS"] = os.path.join(_tmp, "basis.json")
        g["LOCK"] = os.path.join(_tmp, "apply.lock")
        _write_json(g["STORE"], empty())
        io.open(g["SETTING"], "w", encoding="utf-8", newline="").write(html)
        g["_rows"] = lambda: rows
        g["rebuild_page"] = lambda slug: False
        _df = os.path.join(_tmp, "d.json")
        _write_json(_df, good)
        _ra = apply(_df, fetch=lambda u: page)
        t("★★記事ページを作り直せなければ、控え・setting.html・根拠を全部元に戻す★★",
          not _ra["ok"] and io.open(g["SETTING"], encoding="utf-8").read() == html
          and load(g["STORE"]) == empty() and not os.path.isfile(g["BASIS"])
          and io.open(_pg, encoding="utf-8").read() == "元のページ")

        # ★★書いたあとに別の処理が書き換えたものは戻さない★★（Codex review240）
        def _other_writes(slug):
            io.open(g["SETTING"], "w", encoding="utf-8", newline="").write(html + "//他人の更新")
            io.open(_pg, "w", encoding="utf-8").write("他人のページ")
            return False
        g["rebuild_page"] = _other_writes
        _rc2 = apply(_df, fetch=lambda u: page)
        t("★★失敗しても、別の処理が書いた setting.html と記事ページは消さずに知らせる★★",
          not _rc2["ok"]
          and io.open(g["SETTING"], encoding="utf-8").read() == html + "//他人の更新"
          and io.open(_pg, encoding="utf-8").read() == "他人のページ"
          and any("戻していません" in x for x in _rc2["problems"])
          and any("記事ページが途中で" in x for x in _rc2["problems"]))
        t("　自分だけが書いた控えと根拠は戻っている",
          load(g["STORE"]) == empty() and not os.path.isfile(g["BASIS"]))
        io.open(g["SETTING"], "w", encoding="utf-8", newline="").write(html)
        io.open(_pg, "w", encoding="utf-8").write("元のページ")

        # ★★書いた直後に別の処理が書いても、その中身を「自分が書いたもの」と取り違えない★★
        #   （Codex review241＝書いたあとに読み直して控えると、他人の更新を戻して消した）
        _wb = g["_write_bytes"]

        def _wb_then_other(path, data):
            _wb(path, data)
            if path == g["SETTING"]:
                io.open(path, "w", encoding="utf-8", newline="").write(html + "//直後の他人")
            return data
        g["_write_bytes"] = _wb_then_other
        g["rebuild_page"] = lambda slug: False
        _rd = apply(_df, fetch=lambda u: page)
        g["_write_bytes"] = _wb
        t("★★書いた直後に他人が書いた setting.html は、自分の分と取り違えずに残す★★",
          not _rd["ok"]
          and io.open(g["SETTING"], encoding="utf-8").read() == html + "//直後の他人"
          and load(g["STORE"]) == empty())
        io.open(g["SETTING"], "w", encoding="utf-8", newline="").write(html)

        # ★★ほかの書き込みが進行中なら、読む前に断る★★（2つの --apply が同時に走らない）
        g["rebuild_page"] = lambda slug: True
        _held = _ApplyLock().__enter__()
        try:
            _rl = apply(_df, fetch=lambda u: page)
        finally:
            _held.__exit__(None, None, None)
        t("★★ほかの書き込みがロックを持っている間は、何も書かずに断る★★",
          not _rl["ok"] and load(g["STORE"]) == empty()
          and any("進行中" in x for x in _rl["problems"]))
        t("　ロックのファイルが残っていても、持ち主がいなければ止まらない（残骸で止めない）",
          os.path.isfile(g["LOCK"]))
        _rb = apply(_df, fetch=lambda u: page)
        t("　（対照）持ち主がいなくなれば書け、作り直せたら全部残る",
          _rb["ok"] and "zzz_a" in load(g["STORE"])["configs"] and os.path.isfile(g["BASIS"]))
    finally:
        g.update(_keep)
    # ★記事ページが控えを読んでいるかは、ページの実装で見る★（recheck の監査46）
    import recheck as _rc
    _mh = io.open(os.path.join(BASE, "machine.html"), encoding="utf-8").read()
    t("★本物の machine.html は控えの2つの分岐を持っている★",
      _rc.page_reads_pochi_data(_mh, "configs") and _rc.page_reads_pochi_data(_mh, "no_diff"))
    t("　分岐を消したページは「読んでいない」と分かる",
      not _rc.page_reads_pochi_data(_mh.replace("pochiData.no_diff, slug", "x"), "no_diff"))
    broken = html.replace(END, "")
    t("　区画の印が欠けていれば止める", bool(check_problems(broken, empty())))
    t("　区画を手で書き換えたら食い違いとして見つける",
      bool(check_problems(r["html"].replace("1/210.1", "1/200.0", 1), r["store"])))
    # ★本物の setting.html と控えが今そろっているか★（作り直し忘れを止める）
    t("★★本物の setting.html と控えがそろっている★★", check_problems() == [])

    # ★★小役の確認（ko_checked）と EXTEND / KO_CHECK★★（2026-11-10）
    _today = datetime.date(2026, 11, 10)
    st_a = r["store"]                      # zzz_a に cherry の表がある控え（記録なし）
    t("★★自動で足した機種は、小役の差を探した記録が無ければ探す番★★",
      ko_due("zzz_a", st_a, _today) is not None)
    t("　手で書いた機種・表の無い機種は扱わない",
      ko_due("hokuto", st_a, _today) is None and ko_due("zzz_none", st_a, _today) is None)
    _sh = json.loads(json.dumps(st_a))
    _sh["ko_checked"]["zzz_a"] = {"result": "HAS", "checked": (_today - datetime.timedelta(days=179)).isoformat()}
    t("　HAS と記録した機種は180日までは探し直さない", ko_due("zzz_a", _sh, _today) is None)
    _sh["ko_checked"]["zzz_a"]["checked"] = (_today - datetime.timedelta(days=180)).isoformat()
    t("★★HAS も180日たったら確かめ直す★★（直す前は一度「ある」と決めると二度と見なかった）",
      ko_due("zzz_a", _sh, _today) is not None)
    _sn = json.loads(json.dumps(st_a))
    _sn["ko_checked"]["zzz_a"] = {"result": "NONE", "checked": "2026-09-11"}
    t("★NONE は60日たったらもう一度探す★", ko_due("zzz_a", _sn, _today) is not None)
    _st = json.loads(json.dumps(_sn))
    _st["ko_checked"]["zzz_a"]["checked"] = "2026-11-09"
    t("★★担当の回（turn）は、間隔に関係なく確かめ直す（今日まだ見ていなければ）★★",
      ko_due("zzz_a", _st, _today) is None and ko_due("zzz_a", _st, _today, turn=True) is not None)
    _st["ko_checked"]["zzz_a"]["checked"] = "2026-11-10"
    t("　今日もう確かめた機種は、担当の回でも対象外", ko_due("zzz_a", _st, _today, turn=True) is None)
    _sth = {"hand_checked": {"zzz_h": {"result": "HAND_KEEP", "checked": "2026-11-01"}}}
    t("★担当の回は、手書き・設定差なしの機種も間隔に関係なく確かめ直す★",
      recheck_due(_sth, "hand_checked", "zzz_h", _today) is False
      and recheck_due(_sth, "hand_checked", "zzz_h", _today, turn=True) is True)
    _sn["ko_checked"]["zzz_a"]["checked"] = "2026-09-12"
    t("　（対照）59日なら探さない", ko_due("zzz_a", _sn, _today) is None)

    qk1 = "試験機A 弱スイカ 設定1 1/99.1 設定6 1/80.2 の確率です。"
    qk2 = "試験機Aの弱スイカは設定1が1/99.1、設定6が1/80.2となっています。"
    page_k = page.replace("</body>", "<p>%s</p><p>%s</p></body>" % (qk1, qk2))
    ext = {"slug": "zzz_a", "decision": "EXTEND", "by": ["claude", "codex"],
           "why": "2つの出典の弱スイカの設定別の表を突き合わせて一致しました",
           "settings": [1, 6],
           "fields": [{"id": "weakWm", "label": "弱スイカ", "sub": "通常時", "rateKey": "weakWm"}],
           "rates": {"weakWm": {"1": "1/99.1", "6": "1/80.2"}},
           "sources": [{"url": "https://chonborista.com/slot/zzz/", "quote": qk1},
                       {"url": "https://nana-press.com/kaiseki/zzz/", "quote": qk2}]}
    re_ = plan(json.loads(json.dumps(ext)), fetch=lambda u: page_k, store=st_a, html=r["html"], rows=rows)
    t("★★EXTEND は今ある表を残したまま小役の表を足す★★",
      re_["ok"] and [f["id"] for f in re_["store"]["configs"]["zzz_a"]["fields"]] == ["cherry", "weakWm"]
      and re_["store"]["configs"]["zzz_a"]["rates"]["cherry"] == {"1": "1/210.1", "6": "1/190.5"}
      and "weakWm: {1:1/99.1,6:1/80.2}" in re_["html"])
    t("　EXTEND したら小役の確認は HAS（もう探さない）",
      re_["ok"] and re_["store"]["ko_checked"]["zzz_a"]["result"] == "HAS"
      and ko_due("zzz_a", re_["store"]) is None)
    t("　EXTEND の控えは読む段の検査も通る（区画とそろう）",
      re_["ok"] and check_problems(re_["html"], re_["store"]) == [])

    def ext_bad(mut, why, must, store=None):
        d = json.loads(json.dumps(ext))
        mut(d)
        try:                               # ★落ちたら不合格として数える★
            rr = plan(d, fetch=lambda u: page_k, store=store or st_a, html=r["html"], rows=rows)
        except Exception as e:             # noqa: BLE001
            rr = {"ok": True, "problems": [f"例外 {type(e).__name__}"]}
        t(why, (not rr["ok"]) and any(must in x for x in rr["problems"]))
    ext_bad(lambda d: d["fields"][0].update(id="cherry", rateKey="cherry")
            or d.update(rates={"cherry": d["rates"]["weakWm"]}),
            "★★EXTEND で今ある項目を書き換えない★★", "重なっています")
    ext_bad(lambda d: d.update(settings=[1, 2, 6]) or d["rates"]["weakWm"].update({"2": "1/90.0"}),
            "★EXTEND の段階は今の表と同じ★", "今の表と同じ")
    ext_bad(lambda d: d.update(slug="hokuto"), "　EXTEND は手で書いた機種には使えない", "手で書いた")
    ext_bad(lambda d: None, "★EXTEND は表の無い機種には使えない★", "自動で足した表がある機種", store=empty())
    ext_bad(lambda d: d["rates"]["weakWm"].update({"6": "1/80.3"}),
            "★★EXTEND でも足す表の値は独立2出典の逐語に在ること★★", "独立した2つの出典")
    ext_bad(lambda d: d.update(ko="NONE"), "　EXTEND に ko は書かない", "ko を書きません")

    kchk = {"slug": "zzz_a", "decision": "KO_CHECK", "ko": "NONE", "by": ["claude", "codex"],
            "why": "2サイトとも初当たり確率しか設定差を載せていないと確かめた"}
    rk = plan(json.loads(json.dumps(kchk)), fetch=lambda u: page, store=st_a, html=r["html"], rows=rows)
    t("★★KO_CHECK は表を変えずに記録だけ残す（出典なしで通る）★★",
      rk["ok"] and rk["store"]["configs"] == st_a["configs"] and rk["html"] == r["html"]
      and rk["store"]["ko_checked"]["zzz_a"]["result"] == "NONE"
      and rk["basis"]["ko"] == "NONE")

    def kc_bad(mut, why, must, store=None):
        d = json.loads(json.dumps(kchk))
        mut(d)
        try:                               # ★落ちたら不合格として数える★
            rr = plan(d, fetch=lambda u: page, store=store or st_a, html=r["html"], rows=rows)
        except Exception as e:             # noqa: BLE001
            rr = {"ok": True, "problems": [f"例外 {type(e).__name__}"]}
        t(why, (not rr["ok"]) and any(must in x for x in rr["problems"]))
    kc_bad(lambda d: d.pop("ko"), "　KO_CHECK に ko が無ければ断る", "ko（HAS か NONE）")
    kc_bad(lambda d: d.update(ko="MAYBE"), "　ko は HAS か NONE だけ", "HAS か NONE")
    kc_bad(lambda d: d.update(by=["claude"]), "　KO_CHECK も判断者の組が要る", "判断者の組")
    kc_bad(lambda d: d.update(why="短い"), "　KO_CHECK も理由15字以上", "理由")
    kc_bad(lambda d: d.update(rates={"cherry": {"1": "1/2.0", "6": "1/2.0"}}),
           "★KO_CHECK で表を書き換えない★", "表を書きません")
    kc_bad(lambda d: None, "★KO_CHECK は表の無い機種には使えない★", "自動で足した表がある機種", store=empty())
    _add_ko = json.loads(json.dumps(good))
    _add_ko["ko"] = "HAS"
    _rak = plan(_add_ko, fetch=lambda u: page, store=empty(), html=html, rows=rows)
    t("★ADD に ko を書けば一緒に記録する★",
      _rak["ok"] and _rak["store"]["ko_checked"]["zzz_a"]["result"] == "HAS")
    _rak2 = plan(json.loads(json.dumps(good)), fetch=lambda u: page, store=_rak["store"], html=_rak["html"],
                 rows=rows)
    t("★ko の無い ADD で表を書き直したら、記録を消して探し直す★",
      _rak2["ok"] and "zzz_a" not in _rak2["store"]["ko_checked"])
    # ★読む段の検査★
    for _why, _mut in (("表の無い機種の記録", lambda s: s["ko_checked"].update(
                            zzz_q={"result": "HAS", "checked": "2026-11-10"})),
                       ("知らない結果", lambda s: s["ko_checked"].update(
                            zzz_a={"result": "MAYBE", "checked": "2026-11-10"})),
                       ("壊れた日付", lambda s: s["ko_checked"].update(
                            zzz_a={"result": "NONE", "checked": "2026-13-40"})),
                       ("知らない項目", lambda s: s.update(extra={}))):
        _s = json.loads(json.dumps(re_["store"]))
        _mut(_s)
        _write_json(_bp, _s)
        try:
            load(_bp)
            _ok = False
        except PochiError:
            _ok = True
        t(f"★控えに{_why}があれば読む段で止める★", _ok)
    _old = json.loads(json.dumps(r["store"]))
    _old.pop("ko_checked", None)
    _write_json(_bp, _old)
    t("　（対照）記録の無い古い形の控えも読める", load(_bp).get("ko_checked") == {})

    # ★★手書きの機種と「設定差なし」の機種も確かめ直す★★（2026-11-10＝直す前は一度決まると二度と見直されなかった）
    qh1 = "北斗 中段チェリー 設定1 1/210.1 設定6 1/190.5 の確率です。"
    qh2 = "北斗の中段チェリーは設定1が1/210.1、設定6が1/190.5となっています。"
    page_h = "<html><head><title>北斗</title></head><body><h1>北斗</h1><p>%s</p><p>%s</p></body></html>" % (qh1, qh2)
    take = dict(json.loads(json.dumps(good)), slug="hokuto", decision="TAKE_OVER",
                sources=[{"url": "https://chonborista.com/slot/hk/", "quote": qh1},
                         {"url": "https://nana-press.com/kaiseki/hk/", "quote": qh2}])
    rt = plan(json.loads(json.dumps(take)), fetch=lambda u: page_h, store=empty(), html=html, rows=rows)
    t("★★TAKE_OVER＝手で書いた表を、2AIが確かめ直した表で置き換えて自動の区画へ移す★★",
      rt["ok"] and "hokuto" in rt["store"]["configs"] and "hokuto" not in static_slugs(rt["html"])
      and "bb: {1:1/383.4" not in rt["html"] and "cherry: {1:1/210.1,6:1/190.5}" in rt["html"]
      and check_problems(rt["html"], rt["store"]) == [])
    t("　移したあとも、ページが読む形で読める（手書きの表は消え、自動の表だけが残る）",
      rt["ok"] and __import__("extract_setting_rates").extract_rates(rt["html"]).get("hokuto")
      == {"cherry": {"1": "1/210.1", "6": "1/190.5"}})
    t("★TAKE_OVER でも表は独立2出典の逐語と照合する★",
      not plan(dict(json.loads(json.dumps(take)), sources=take["sources"][:1]),
               fetch=lambda u: page_h, store=empty(), html=html, rows=rows)["ok"])
    t("　TAKE_OVER は手で書いた機種にだけ使える",
      any("手で書いた対応がある機種にだけ" in x
          for x in shape_problems(dict(take, slug="zzz_a"), rows, static_slugs(html))))
    keepd = {"slug": "hokuto", "decision": "HAND_KEEP", "by": ["claude", "codex"],
             "why": "手書きの表を2サイトで確かめたが、いまは移せる表がそろわない"}
    rk2 = plan(json.loads(json.dumps(keepd)), fetch=lambda u: page_h, store=empty(), html=html, rows=rows)
    t("★★HAND_KEEP＝表を変えず、確かめ直した日だけ控える★★",
      rk2["ok"] and rk2["html"] == html and rk2["store"]["hand_checked"]["hokuto"]["checked"]
      and not recheck_due(rk2["store"], "hand_checked", "hokuto"))
    t("　HAND_KEEP で表は書けない",
      any("表を書きません" in x for x in shape_problems(dict(keepd, rates={}), rows, static_slugs(html))))
    _st_h = json.loads(json.dumps(rk2["store"]))
    _st_h["hand_checked"]["hokuto"]["checked"] = "2026-08-01"
    t("★手書きの機種は90日たったらまた確かめ直す★",
      recheck_due(_st_h, "hand_checked", "hokuto", datetime.date(2026, 10, 30))
      and not recheck_due(_st_h, "hand_checked", "hokuto", datetime.date(2026, 10, 29)))
    dchk = {"slug": "zzz_a", "decision": "DIFF_CHECK", "by": ["claude", "codex"],
            "why": "2サイトとも小役の設定差を載せていないと確かめた"}
    rd2 = plan(json.loads(json.dumps(dchk)), fetch=lambda u: page, store=empty(), html=html, rows=rows,
               no_diff_hand={"zzz_a"})
    t("★★DIFF_CHECK＝「設定差なし」の機種を確かめ直した日だけ控える★★",
      rd2["ok"] and rd2["store"]["diff_checked"]["zzz_a"]["checked"] and rd2["html"] == html)
    t("　DIFF_CHECK は「設定差なし」の機種にだけ使える",
      not plan(json.loads(json.dumps(dchk)), fetch=lambda u: page, store=empty(), html=html, rows=rows,
               no_diff_hand=set())["ok"])
    _ra2 = plan(json.loads(json.dumps(good)), fetch=lambda u: page, store=rd2["store"], html=html, rows=rows)
    t("　「設定差なし」の機種に ADD したら、確かめ直しの記録は消える",
      _ra2["ok"] and "zzz_a" not in _ra2["store"]["diff_checked"])
    t("★★対象の出方：手書き・設定差なし・小役・使えない★★",
      "手書き" in status_line("hokuto", (True, ""), empty(), {"hokuto"})
      and "対象（設定差なし" in status_line("zzz_a", (False, NO_DIFF_REASON), empty(), set())
      and "対象外" in status_line("zzz_a", (False, NO_DIFF_REASON), rd2["store"], set())
      and "対象外" in status_line("hokuto", (True, ""), rk2["store"], {"hokuto"})
      and status_line("zzz_b", (False, "解析待ち"), empty(), set()).endswith("STEP 1-P 対象"))
    _bad_h = json.loads(json.dumps(rt["store"]))
    _bad_h["hand_checked"]["hokuto"] = {"checked": "2026-11-10"}
    _write_json(_bp, _bad_h)
    try:
        load(_bp)
        _hb = False
    except PochiError:
        _hb = True
    t("★自動へ移した機種に手書きの確認の記録が残っていたら、読む段で止める★", _hb)
    print(f"{ok[0]}/{sum(ok)} 合格")
    return 0 if ok[1] == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--status", nargs="+", metavar="SLUG",
                    help="その機種のポチポチくんが使えるか・STEP 1-P の対象かを出す")
    ap.add_argument("--turn", action="store_true",
                    help="担当が回ってきた機種として見る（間隔に関係なく、今日まだ見ていなければ対象）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.build:
        html = io.open(SETTING, encoding="utf-8").read()
        new = build(html, load())
        if new != html:
            with io.open(SETTING, "w", encoding="utf-8", newline="") as f:
                f.write(new)
            print("setting.html の自動の区画を作り直しました")
        else:
            print("変わりません")
        return 0
    if a.check:
        p = check_problems()
        for x in p:
            print("★" + x + "★")
        print("そろっています" if not p else "")
        return 1 if p else 0
    if a.status:
        # ★判定は記事ページと同じ関数★（build_machine_pages.pochipochi_state）
        import build_machine_pages as _b
        reasons = _b.extract_pochipochi_reasons(io.open(
            os.path.join(BASE, "machine.html"), encoding="utf-8").read())
        rows = _rows()
        store = load()
        static = static_slugs(io.open(SETTING, encoding="utf-8").read())
        for slug in a.status:
            if slug not in rows:
                print(f"{slug}: 機種一覧にありません")
                continue
            print(status_line(slug, _b.pochipochi_state(rows[slug], reasons), store, static, turn=a.turn))
        return 0
    if a.list:
        d = load()
        print(f"対応（自動）: {len(d['configs'])} 機種 {sorted(d['configs'])}")
        print(f"設定差なし（自動）: {len(d['no_diff'])} 機種 {sorted(d['no_diff'])}")
        due = sorted(s for s in d["configs"] if ko_due(s, d))
        print(f"小役の差を探す番（自動）: {len(due)} 機種 {due}")
        return 0
    if a.apply:
        r = apply(a.apply, dry=a.dry_run)
        if not r["ok"]:
            print("書きませんでした（書いたものは元に戻っています）:")
            for x in r["problems"]:
                print("  - " + x)
            return 1
        if a.dry_run:
            print("下見: 通ります（書いていません）")
            return 0
        print("書きました（控え・setting.html・記事ページ）")
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except (_sj.SafeJsonError, PochiError) as e:
        print(f"★{e}★")
        raise SystemExit(1)

"""★朝一リセットのランキングを「リセット時お得な順」に並べるための控え★（2026-11-09・運営者の決定）

使い方:
  python scripts/reset_ev.py --next 5                 … 今日確かめる機種（1日5件・同じ日は同じ割り当て）
  python scripts/reset_ev.py --apply <決定ファイル> [--dry-run]   … 2AIの決定を書く（EV / NO_EV）
  python scripts/reset_ev.py --tried <slug> --result UNREADABLE|UNDECIDED|ERROR   … 試しただけ（記録は変えない）
  python scripts/reset_ev.py --check                  … 並び順・古さ・進み具合（番人・タスク）
  python scripts/reset_ev.py --selftest

★なぜ要るか★＝朝一リセットのランキングは「通常時とリセット後の狙い目の差」で並べていたが、
  読者が知りたいのは「リセットされた台はどれがお得か」（運営者・2026-11-09）。
  外のサイトが出している「リセット後0Gから打ったときの期待値（5.6枚交換）」を2AIが読み、その順に並べる。
★★金額はどこにも出さない★★（運営者「金額とかは載せない・並び替えるだけ」）
  控え（金額・引用）… Documents/uchidokoro/reset_ev.json（リポジトリの外）
  並び順（slugの列だけ）… assets/data/reset-ev-order.json（リポジトリの中・ページが読む）
  ★ページの生成とCIは並び順だけを読む★（控えはCIに無い＝罠㊱）。控えとの一致は --check（このPCだけ）。
★マイナスの機種も並べる★（運営者の決定）。プラス・マイナスは読者に言わない。
★決めるのは2AI／機械が見るのは形と実在だけ★（鉄則0z）
  ・判断者の組と理由15字以上
  ・URLが通信してよい名簿の先（用途 reset_ev）で、取り直した本文に4つの逐語引用がそのまま在る
  ・表題の引用に「リセット」か「設定変更」、見出しの引用に「5.6」か「56」
  ・★0Gの行の引用に、符号つきの金額がそのまま在る★（-2186 なら「-2,186円」等。プラスなら直前にマイナス記号が無い）
  ・どの表・どの列を採るか（リセット後の表か・現金投資か・CZ間かAT間か）は2AIが原文で決めて why に書く
★サイトの優先順位は固定★＝なな徹 → スロパチクエスト → パチ7（上に値があれば下は見ない・大きい値を選ばない）。
★失敗は記録を書き換えない★（tries に残すだけ）。試した日と確かめた日（checked）は別。
"""
import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import local_paths as _lp  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORDER = os.path.join(BASE, "assets", "data", "reset-ev-order.json")
STORE = _lp.doc("reset_ev.json")
ORDER_SCHEMA = "reset-ev-order/v1"
STORE_SCHEMA = "reset-ev/v1"
PURPOSE = "reset_ev"
# ★サイトの優先順位（固定）★＝発行者の鍵 → ホスト
#   ★パチ7は機種ページに期待値を載せていない（2026-11-09確認）ので入れていない★＝通信の名簿でも
#   用途 reset_ev を許していない。載るようになったら、規約の記録に書いてから名簿とここの両方に足す。
SITES = {
    "nana-press": ("nana-press.com", "www.nana-press.com"),
    "slopachi-quest": ("slopachi-quest.com", "www.slopachi-quest.com"),
}
SITE_ORDER = ("nana-press", "slopachi-quest")
QUOTE_KEYS = ("title", "header", "row", "conditions")
COND_KEYS = ("rate", "money", "setting", "stop", "not_included")
TRY_RESULTS = ("UNREADABLE", "UNDECIDED", "ERROR")
MIN_WHY = 15
MIN_QUOTE, MAX_QUOTE = 8, 200
PER_DAY = 5
RETRY_DAYS = 2
STALE_DAYS = 60          # 確かめ直していない記録の古さの線（暫定版の表が更新されるため）
STALL_DAYS = 3           # これだけ1件も確かめ終えなければ知らせる
_SLUG = re.compile(r"^[a-z0-9_]+$")
_MINUS = "-−▲"
JST = _dt.timezone(_dt.timedelta(hours=9))


class EvError(Exception):
    pass


def _today() -> str:
    return _dt.datetime.now(JST).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 公開の並び順（リポジトリの中）

def read_order(path: str = "") -> list:
    """★無ければ空・形が違えば例外★（黙って0件と読まない）"""
    path = path or ORDER
    if not os.path.isfile(path):
        return []
    import safe_json as _sj
    d = _sj.read_json(path, expect=dict)
    if d.get("schema") != ORDER_SCHEMA or not isinstance(d.get("order"), list):
        raise EvError("reset-ev-order.json の形が違います")
    order = d["order"]
    if any(not isinstance(s, str) or not _SLUG.match(s) for s in order):
        raise EvError("reset-ev-order.json に slug の形でないものがあります")
    if len(set(order)) != len(order):
        raise EvError("reset-ev-order.json に同じ機種が2回あります")
    return list(order)


def order_from(store: dict) -> list:
    """★控えから並び順を作る唯一の場所★＝EV の記録を金額の大きい順・同額は slug 順"""
    recs = [(s, r) for s, r in (store.get("records") or {}).items()
            if isinstance(r, dict) and r.get("state") == "EV"]
    recs.sort(key=lambda x: (-int(x[1]["ev_yen"]), x[0]))
    return [s for s, _ in recs]


def _order_bytes(order: list) -> bytes:
    return (json.dumps({"schema": ORDER_SCHEMA, "order": order}, ensure_ascii=False, indent=1)
            + "\n").encode("utf-8")


# ---------------------------------------------------------------- 控え（リポジトリの外）

def empty_store() -> dict:
    return {"schema": STORE_SCHEMA, "records": {}, "tries": {}, "plan": {}}


def read_store(path: str = "", order_path: str = "") -> dict:
    """★無ければ空＝ただし公開の並び順が既にあるときは止める★（Codex impl の1）＝
    控えが消えたのを最初の日と取り違えると、公開中の並び順を空や1件で上書きしてしまう。
    ★戻し方はバックアップから控えを戻すこと★（作り直さない）"""
    path = path or STORE
    if not os.path.isfile(path):
        if read_order(order_path):
            raise EvError("控え（reset_ev.json）が無いのに、公開の並び順があります"
                          "／★控えをバックアップから戻してください（作り直しません）★")
        return empty_store()
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if not isinstance(d, dict) or d.get("schema") != STORE_SCHEMA:
        raise EvError("reset_ev.json の形が違います")
    for k in ("records", "tries", "plan"):
        if not isinstance(d.get(k), dict):
            raise EvError(f"reset_ev.json の {k} の形が違います")
    if "started" in d:
        try:
            _dt.date.fromisoformat(str(d["started"]))
        except ValueError:
            raise EvError("reset_ev.json の started（始めた日）が日付ではありません")
    for slug, rec in d["records"].items():
        bad = record_problems(slug, rec)
        if bad:
            raise EvError(f"reset_ev.json の {slug} の記録が決まりに合いません: {bad[0]}")
    return d


def _store_bytes(store: dict) -> bytes:
    return (json.dumps(store, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8")


def _replace_bytes(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _write_both(store: dict, store_path: str, order_path: str) -> None:
    """★控えと並び順を一緒に書く（全か無か）★＝片方が書けなければ、書いた方を元へ戻す"""
    new_s, new_o = _store_bytes(store), _order_bytes(order_from(store))
    old_s = open(store_path, "rb").read() if os.path.isfile(store_path) else None
    old_o = open(order_path, "rb").read() if os.path.isfile(order_path) else None
    _replace_bytes(store_path, new_s)
    try:
        if old_o != new_o:
            _replace_bytes(order_path, new_o)
    except Exception:
        if old_s is None:
            os.remove(store_path)
        else:
            _replace_bytes(store_path, old_s)
        raise


# ---------------------------------------------------------------- 記録・決定の検査（純関数）

def _judges(by) -> frozenset:
    if isinstance(by, str):
        return frozenset(x.strip() for x in by.split(",") if x.strip())
    return frozenset(str(x).strip() for x in (by or []))


def _site_of(url: str) -> str:
    import urllib.parse as _up
    try:
        p = _up.urlsplit(str(url or ""))
    except ValueError:
        return ""
    if p.scheme != "https":
        return ""
    host = (p.hostname or "").lower()
    for key, hosts in SITES.items():
        if host in hosts:
            return key
    return ""


def flat(s) -> str:
    """★比べる形★＝NFKC でそろえ、空白を全部詰める（表のセルの区切りに左右されない）"""
    return "".join(unicodedata.normalize("NFKC", str(s or "")).split())


def signed_amount_in(ev: int, quote: str) -> bool:
    """★符号つきの金額がそのまま引用にあるか★（桁区切りは外す・単位「円」まで）"""
    # ★空白は詰めずに1つへそろえる★＝表のセルの区切りを消すと「150 316円」が「150316円」になる
    t = " ".join(unicodedata.normalize("NFKC", str(quote or "")).split())
    t = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", t)
    n = str(abs(int(ev)))
    if ev < 0:
        return re.search(r"[%s] ?%s円" % (re.escape(_MINUS), n), t) is not None
    for m in re.finditer(r"(?<![\d.])\+?%s円" % n, t):
        before = t[:m.start()].rstrip()
        if not before or before[-1] not in _MINUS + "△":
            return True
    return False


def record_problems(slug: str, rec) -> list:
    """★控えに入っている記録の形★（読むたびに同じ検査をする）"""
    import confirmed_values as _cv
    if not _SLUG.match(str(slug)):
        return ["slug の形が違います"]
    if not isinstance(rec, dict):
        return ["記録が辞書ではありません"]
    out = []
    if _judges(rec.get("by")) not in _cv.JUDGE_PAIRS:
        out.append(f"判断者は {_cv.JUDGES_LABEL} です")
    if len(str(rec.get("why") or "").strip()) < MIN_WHY:
        out.append(f"理由（why）は{MIN_WHY}字以上です")
    try:
        _dt.date.fromisoformat(str(rec.get("checked") or ""))
    except ValueError:
        out.append("確かめた日（checked）が日付ではありません")
    st = rec.get("state")
    if st == "EV":
        ev = rec.get("ev_yen")
        if type(ev) is not int:
            out.append("金額（ev_yen）が整数ではありません")
        if rec.get("site") not in SITES or _site_of(rec.get("url")) != rec.get("site"):
            out.append("出典のURLとサイトが合いません")
        q = rec.get("quotes")
        if not isinstance(q, dict) or set(q) != set(QUOTE_KEYS):
            out.append("引用（quotes）は title・header・row・conditions の4つです")
        else:
            for k in QUOTE_KEYS:
                v = q.get(k)
                if not isinstance(v, str) or not (MIN_QUOTE <= len(v.strip()) <= MAX_QUOTE):
                    out.append(f"引用 {k} は{MIN_QUOTE}〜{MAX_QUOTE}字です")
            if not out:
                if not any(w in flat(q["title"]) for w in ("リセット", "設定変更")):
                    out.append("表題の引用に「リセット」か「設定変更」がありません")
                if not any(w in flat(q["header"]) for w in ("5.6", "56")):
                    out.append("見出しの引用に「5.6」か「56」がありません")
                if type(ev) is int and not signed_amount_in(ev, q["row"]):
                    out.append("0Gの行の引用に、符号つきの金額がそのままありません")
        c = rec.get("conditions")
        if not isinstance(c, dict) or set(c) != set(COND_KEYS) or \
                any(not isinstance(c[k], str) or not c[k].strip() for k in COND_KEYS):
            out.append("条件（conditions）は rate・money・setting・stop・not_included の5つ（空でない文）です")
        elif "5.6" not in flat(c["rate"]) and "56" not in flat(c["rate"]):
            out.append("条件の交換率が5.6枚ではありません")
    elif st == "NO_EV":
        s = rec.get("searched")
        if not isinstance(s, list) or not s or \
                any(not isinstance(x, str) or not _site_of(x) for x in s):
            out.append("NO_EV は見た先のURL（searched・名簿の3サイト）が1件以上要ります")
        # ★いちばん上のサイト（なな徹）を読まずに「値が無い」とは言えない★（Codex design2 の3）＝
        #   読めなかった・表の意味を決められなかったときは NO_EV ではなく --tried UNREADABLE / UNDECIDED
        elif not any(_site_of(x) == SITE_ORDER[0] for x in s):
            out.append("NO_EV は、いちばん上のサイト（なな徹）の機種ページを見た記録が要ります")
    else:
        out.append("state は EV / NO_EV のどちらかです")
    return out


def targets(rows=None) -> list:
    """★いまの朝一リセットの表の機種（slug・表の順＝狙える幅の大きい順）★"""
    import build_hub_pages as _bh
    if rows is None:
        rows = _bh.load_rows()
    return [r["slug"] for r in _bh.dataset_C(_bh.ranking_pool(rows))]


def build_record(dec: dict, fetch=None, today: str = "") -> dict:
    """★決定ファイルから記録を作る★（通らなければ EvError）。fetch は試験で差し替える"""
    rec = {k: dec.get(k) for k in ("state", "by", "why")}
    rec["by"] = ",".join(sorted(_judges(dec.get("by"))))
    rec["why"] = str(dec.get("why") or "").strip()[:600]
    rec["checked"] = today or _today()
    if dec.get("state") == "NO_EV":
        rec["searched"] = dec.get("searched")
        return rec
    rec.update({k: dec.get(k) for k in ("ev_yen", "site", "url", "quotes", "conditions")})
    bad = record_problems(str(dec.get("slug") or ""), rec)
    if bad:
        raise EvError(bad[0])
    if fetch is None:
        import fetched_page as _fp
        fetch = lambda u: _fp.fetch(u, PURPOSE).cleaned_html  # noqa: E731
    import confirmed_values as _cv
    try:
        html = fetch(rec["url"])
    except Exception as e:                       # noqa: BLE001
        raise EvError(f"出典を取り直せません: {type(e).__name__}: {str(e)[:120]}")
    body = flat(_cv.page_text(html, rec["url"]))
    for k in QUOTE_KEYS:
        if flat(rec["quotes"][k]) not in body:
            raise EvError(f"引用 {k} が、取り直した本文にそのままありません")
    rec["page_sha256"] = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    return rec


def decision_problems(dec, target_slugs: list, machine_slugs: set) -> list:
    if not isinstance(dec, dict):
        return ["決定ファイルが辞書ではありません"]
    slug = str(dec.get("slug") or "")
    if not _SLUG.match(slug):
        return ["slug の形が違います（英小文字・数字・下線だけ）"]
    if dec.get("state") == "EV" and slug not in target_slugs:
        return [f"{slug} はいまの朝一リセットの表にありません"]
    if dec.get("state") == "NO_EV" and slug not in machine_slugs:
        return [f"{slug} は機種一覧にありません"]
    rec = dict(dec)
    rec.setdefault("checked", _today())
    rec["by"] = ",".join(sorted(_judges(dec.get("by"))))
    return record_problems(slug, {k: v for k, v in rec.items() if k != "slug"})


# ---------------------------------------------------------------- 書く

def apply(dec_path: str, dry_run: bool = False, store_path: str = "", order_path: str = "",
          fetch=None, rows=None, today: str = "") -> int:
    """0＝並び順は変わらない／10＝並び順が変わった（ページを作り直してコミットする）／1＝断った／2＝控えが読めない"""
    import ranking_note as _rn
    import safe_json as _sj
    store_path, order_path = store_path or STORE, order_path or ORDER
    bad_code = _rn.unattended_code_problem()
    if bad_code:
        print(f"★書きません★ {bad_code}")
        return 1
    with open(dec_path, encoding="utf-8") as f:
        dec = json.load(f)
    try:
        store = read_store(store_path, order_path)
        before_order = read_order(order_path)
    except Exception as e:                       # noqa: BLE001
        print(f"★控えを読めません（書きません）: {type(e).__name__}: {e}★")
        return 2
    if rows is None:
        import build_hub_pages as _bh
        rows = _bh.load_rows()
    tg = targets(rows)
    ms = {m["slug"] for m in _sj.read_rows(os.path.join(BASE, "assets", "data", "machines.json"))}
    bad = decision_problems(dec, tg, ms)
    if bad:
        print("★書きません★")
        for b in bad:
            print("  - " + b)
        return 1
    slug = dec["slug"]
    try:
        rec = build_record(dec, fetch=fetch, today=today)
    except EvError as e:
        print(f"★書きません★ {e}")
        return 1
    new = json.loads(json.dumps(store))
    new["records"][slug] = rec
    new["tries"][slug] = {"date": today or _today(), "result": rec["state"]}
    new.setdefault("started", today or _today())
    after_order = order_from(new)
    if dry_run:
        print(f"{slug}: {rec['state']} を書けます（並び順は" +
              ("変わりません" if after_order == before_order else "変わります") + "）")
        return 0
    _write_both(new, store_path, order_path)
    if read_order(order_path) != after_order or read_store(store_path)["records"].get(slug) != rec:
        print("★書いた後の読み直しが合いません★")
        return 1
    # ★金額は出さない★（CIのログ・メールに載りうる）
    if after_order == before_order:
        print(f"{slug}: {rec['state']} を記録しました（並び順は変わりません・コミット不要）")
        return 0
    print(f"{slug}: {rec['state']} を記録し、並び順を作り直しました"
          f"（{before_order.index(slug) + 1 if slug in before_order else '-'}位 → "
          f"{after_order.index(slug) + 1 if slug in after_order else '-'}位）")
    return CHANGED


CHANGED = 10


def tried(slug: str, result: str, store_path: str = "", today: str = "", order_path: str = "") -> dict:
    """★試しただけ★（記録は変えない・並び順も変えない）"""
    if not _SLUG.match(str(slug)) or result not in TRY_RESULTS:
        raise EvError(f"slug と結果（{' / '.join(TRY_RESULTS)}）を確かめてください")
    store_path = store_path or STORE
    store = read_store(store_path, order_path)
    store["tries"][slug] = {"date": today or _today(), "result": result}
    # ★始めた日★（Codex impl の3）＝一度も成功しないまま止まっていても、ここから日数を数えて知らせる
    store.setdefault("started", today or _today())
    _replace_bytes(store_path, _store_bytes(store))
    return store["tries"][slug]


# ---------------------------------------------------------------- 割り当て（ranking_queue とは別の枠）

def pick(n: int, items: list, store: dict, today: str) -> list:
    """まだ試していない機種（表の順）→ 確かめた日が古い順。今日試した・失敗して2日たっていないものは外す"""
    t = _dt.date.fromisoformat(today)

    def usable(s):
        tr = (store.get("tries") or {}).get(s)
        if not tr:
            return True
        d = _dt.date.fromisoformat(tr["date"])
        if d >= t:
            return False
        if tr.get("result") in TRY_RESULTS and (t - d).days < RETRY_DAYS:
            return False
        return True
    tries = store.get("tries") or {}
    recs = store.get("records") or {}
    fresh = [s for s in items if s not in tries and usable(s)]
    # ★失敗のやり直しと、確かめ終えた機種の取り直しは1件ずつ交互★（Codex design2 の2）＝
    #   どちらかを先にすると、もう片方に枠が回らなくなる
    failed = [s for s in items if s in tries and usable(s) and tries[s].get("result") in TRY_RESULTS]
    failed.sort(key=lambda s: (tries[s]["date"], items.index(s)))
    redo = [s for s in items if s in tries and usable(s) and s not in failed]
    redo.sort(key=lambda s: ((recs.get(s) or {}).get("checked") or "0000-00-00", items.index(s)))
    mixed = []
    for i in range(max(len(failed), len(redo))):
        mixed += failed[i:i + 1] + redo[i:i + 1]
    return (fresh + mixed)[:max(0, n)]


def plan_today(n: int, items: list, store: dict, today: str) -> list:
    """★その日の割り当ては最初の --next で決めて控え、同じ日は使い回す★（今日試した数は差し引く）"""
    p = store.get("plan") or {}
    if p.get("date") == today and isinstance(p.get("slugs"), list):
        chosen = p["slugs"]
    else:
        chosen = pick(PER_DAY, items, store, today)
        store["plan"] = {"date": today, "slugs": chosen}
    done = {s for s, tr in (store.get("tries") or {}).items() if tr.get("date") == today}
    return [s for s in chosen if s not in done][:max(0, n)]


def next_items(n: int, store_path: str = "", today: str = "", items=None, order_path: str = "") -> list:
    store_path = store_path or STORE
    store = read_store(store_path, order_path)
    today = today or _today()
    out = plan_today(n, items if items is not None else targets(), store, today)
    _replace_bytes(store_path, _store_bytes(store))
    return out


# ---------------------------------------------------------------- 点検

def mismatches(rows, store: dict) -> list:
    """★外の値と、チェッカーのリセット後の線の食い違い★（止めない・2AIが点検する一覧）
    5.6枚か交換率を選ばない線の機種だけ見る。★食い違い＝誤りではない★（基準が同じとは限らない）"""
    import build_hub_pages as _bh
    out = []
    for r in _bh.dataset_C(_bh.ranking_pool(rows)):
        rec = (store.get("records") or {}).get(r["slug"])
        if not rec or rec.get("state") != "EV" or r["ratelabel"] not in ("5.6枚", "チェッカー基準"):
            continue
        if rec["ev_yen"] > 0 and r["rline"] != 0:
            out.append(f"{r['slug']}: 外の値はリセット後0Gからプラスなのに、リセット後の線が{r['rline']}G")
        if rec["ev_yen"] <= 0 and r["rline"] == 0:
            out.append(f"{r['slug']}: 外の値はリセット後0Gからプラスではないのに、リセット後の線が0G")
    return out


def unpublished_order() -> str:
    """★並び順のファイルに、コミットしていない変更があるか★（空＝無い）。git が読めなければ理由を返す"""
    import subprocess
    try:
        r = subprocess.run(["git", "status", "--porcelain", "--", "assets/data/reset-ev-order.json"],
                           cwd=BASE, capture_output=True, text=True, timeout=30)
    except Exception as e:                       # noqa: BLE001
        return f"git に問い合わせできません: {type(e).__name__}"
    if r.returncode != 0:
        return "git に問い合わせできません"
    return "コミットしていない変更があります" if r.stdout.strip() else ""


def check(store_path: str = "", order_path: str = "", today: str = "", rows=None) -> int:
    today = today or _today()
    try:
        store = read_store(store_path, order_path)
        order = read_order(order_path)
    except Exception as e:                       # noqa: BLE001
        print(f"★控えか並び順を読めません: {type(e).__name__}: {e}★")
        return 2
    rc = 0
    if order != order_from(store):
        print("★公開の並び順が控えから作ったものと違います★（--apply で作り直す）")
        rc = 1
    t = _dt.date.fromisoformat(today)
    if rows is None:
        try:
            import build_hub_pages as _bh
            rows = _bh.load_rows()
        except Exception as e:                   # noqa: BLE001
            print(f"★機種データを読めません: {e}★")
            return 2
    tg = targets(rows)
    # ★古さを見るのは、いまの表の機種だけ★（Codex design2 の6）＝表から外れた機種には割り当てが来ないので
    #   ずっと警告に残る。★60日は知らせる線で、載せる期限ではない★（古くても並びには残す・目安の並び順なので）
    old = [s for s, r in store["records"].items()
           if s in tg and (t - _dt.date.fromisoformat(r["checked"])).days > STALE_DAYS]
    if old:
        print(f"{STALE_DAYS}日より前に確かめたままの機種: {len(old)}件（{', '.join(sorted(old)[:5])}）")
        rc = 1
    # ★停滞＝最後に確かめ終えた日から／一度も無ければ始めた日から★（Codex impl の3）
    last = max([r["checked"] for r in store["records"].values()] or [""])
    base = last or store.get("started") or ""
    if base and (t - _dt.date.fromisoformat(base)).days >= STALL_DAYS:
        print(f"{STALL_DAYS}日以上、1件も確かめ終えていません（"
              + (f"最後 {last}" if last else f"始めた日 {base}・まだ0件") + "）")
        rc = 1
    # ★並び順を書いたのに、まだ公開していない★（Codex impl の2）＝ページを作る前に止まると
    #   控えと並び順は一致したまま残り、夜の新台公開の一致確認も止まる。本番の置き場のときだけ見る
    if (order_path or ORDER) == ORDER:
        pend = unpublished_order()
        if pend:
            print("★並び順がまだ公開されていません★（" + pend + "）"
                  "→ build_hub_pages.py --legacy・版上げ・コミット・push")
            rc = 1
    n_ev = sum(1 for s in tg if (store["records"].get(s) or {}).get("state") == "EV")
    n_no = sum(1 for s in tg if (store["records"].get(s) or {}).get("state") == "NO_EV")
    print(f"表の{len(tg)}機種のうち、お得さを確かめた機種 {n_ev}・値が見つからなかった機種 {n_no}")
    for m in mismatches(rows, store):
        print("食い違い（2AIで点検）: " + m)
    if rc == 0:
        print("問題ありません")
    return rc


# ---------------------------------------------------------------- 試験

def _raises(fn) -> bool:
    try:
        fn()
    except EvError:
        return True
    return False


def selftest() -> int:
    import tempfile
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅ " if cond else "❌ ") + name)

    page = ("<html><body><h2>リセット期待値【暫定版】</h2><table>"
            "<tr><th>打ち出し</th><th>等価交換</th><th>5.6枚交換</th></tr>"
            "<tr><td>0</td><td>-1195円</td><td>-2,186円</td></tr>"
            "<tr><td>50</td><td>-747円</td><td>-1745円</td></tr></table>"
            "<p>算出条件 設定1・AT終了後即ヤメ・閉店欠損は考慮しない</p>"
            "<h2>天井期待値（通常時）の表です</h2></body></html>")
    url = "https://nana-press.com/kaiseki/machine/1/2/"
    good = {"slug": "aaa", "state": "EV", "ev_yen": -2186, "site": "nana-press", "url": url,
            "quotes": {"title": "リセット期待値【暫定版】", "header": "打ち出し 等価交換 5.6枚交換",
                       "row": "0 -1195円 -2,186円", "conditions": "設定1・AT終了後即ヤメ・閉店欠損は考慮しない"},
            "conditions": {"rate": "5.6枚交換", "money": "記載なし", "setting": "設定1",
                           "stop": "AT終了後即ヤメ", "not_included": "閉店欠損"},
            "by": "claude,codex", "why": "なな徹のリセット期待値表の0G行・5.6枚の列を採った"}
    f = lambda u: page  # noqa: E731

    def rec_of(d):
        try:
            return build_record(d, fetch=f, today="2026-11-09")
        except EvError as e:
            return str(e)

    r = rec_of(json.loads(json.dumps(good)))
    t("正しい決定から記録を作れる", isinstance(r, dict) and r["ev_yen"] == -2186)

    def bad(mut, why):
        d = json.loads(json.dumps(good))
        mut(d)
        got = rec_of(d)
        t(f"断る: {why}", isinstance(got, str))
    bad(lambda d: d.update(ev_yen=2186), "符号の違う金額（-2186 をプラスで書く）")
    bad(lambda d: d.update(ev_yen=-218), "桁の違う金額")
    bad(lambda d: d["quotes"].update(row="0 -1195円 -2,186円 なし"), "本文に無い引用")
    # ★ページに実在する別の表題で試す★（無い引用だと、手前の「本文に無い」で止まり、この守りを試さない）
    bad(lambda d: d["quotes"].update(title="天井期待値（通常時）の表です"), "表題にリセットの語が無い")
    bad(lambda d: d["quotes"].update(header="打ち出し 等価交換 だけ"), "見出しに5.6が無い")
    bad(lambda d: d.update(url="https://example.com/x"), "名簿に無いサイト")
    bad(lambda d: d.update(url="https://pachiseven.jp/machines/1", site="pachiseven"),
        "通信の名簿で用途を許していないサイト（パチ7）")
    bad(lambda d: d.update(url="http://nana-press.com/kaiseki/machine/1/2/"), "https でないURL")
    bad(lambda d: d.update(by="claude"), "判断者が1人")
    bad(lambda d: d.update(why="短い"), "理由が短い")
    bad(lambda d: d["conditions"].update(rate="等価"), "交換率が5.6枚でない")
    bad(lambda d: d["conditions"].pop("stop"), "条件の欄が足りない")
    bad(lambda d: d.update(ev_yen="−2186"), "金額が整数でない")
    t("プラスの金額は直前にマイナスが無いときだけ", signed_amount_in(316, "150 316円 -701円")
      and not signed_amount_in(701, "150 316円 -701円") and signed_amount_in(-701, "150 316円 -701円"))
    t("全角のマイナスも符号として読む", signed_amount_in(-2186, "0 －2,186円"))
    t("桁区切りの有無は同じ金額", signed_amount_in(-2186, "0 -2186円") and signed_amount_in(-2186, "0 -2,186円"))
    t("先頭の数字の一部を金額と読まない", not signed_amount_in(186, "0 -2186円"))
    bad_fetch = rec_of_err = None
    try:
        build_record(json.loads(json.dumps(good)), fetch=lambda u: (_ for _ in ()).throw(OSError("x")),
                     today="2026-11-09")
        bad_fetch = False
    except EvError:
        bad_fetch = True
    t("取り直せなければ書かない", bad_fetch)

    # 並び順
    st = empty_store()
    st["records"] = {"b": dict(r, ev_yen=500), "a": dict(r, ev_yen=500), "c": dict(r, ev_yen=-3000),
                     "d": {"state": "NO_EV", "searched": [url], "by": "claude,codex",
                           "why": "なな徹にリセット期待値の表が無かった", "checked": "2026-11-09"}}
    t("並び順は金額の大きい順・同額は slug 順・NO_EV は入れない", order_from(st) == ["a", "b", "c"])
    t("マイナスの機種も並び順に入る", "c" in order_from(st))
    with tempfile.TemporaryDirectory() as td:
        sp, op = os.path.join(td, "s.json"), os.path.join(td, "o.json")
        dp = os.path.join(td, "d.json")
        with open(dp, "w", encoding="utf-8") as fh:
            json.dump(good, fh, ensure_ascii=False)
        rows = [{"slug": "aaa"}]
        import build_hub_pages as _bh
        keep_targets = globals()["targets"]
        globals()["targets"] = lambda rows=None: ["aaa", "bbb"]
        try:
            rc = apply(dp, store_path=sp, order_path=op, fetch=f, rows=rows, today="2026-11-09")
            t("書けたら並び順が変わったと返す（10）", rc == CHANGED and read_order(op) == ["aaa"])
            t("書いたあと --check の並び順は一致", read_order(op) == order_from(read_store(sp)))
            rc2 = apply(dp, store_path=sp, order_path=op, fetch=f, rows=rows, today="2026-11-09")
            t("同じ決定をもう一度書いても並び順は変わらない（0）", rc2 == 0)
            with open(dp, "w", encoding="utf-8") as fh:
                json.dump(dict(good, slug="zzz"), fh, ensure_ascii=False)
            t("表に無い機種の EV は断る", apply(dp, store_path=sp, order_path=op, fetch=f, rows=rows,
                                             today="2026-11-09") == 1)
            with open(dp, "w", encoding="utf-8") as fh:
                json.dump(dict(good, quotes=dict(good["quotes"], row="0 -1195円 -9,999円")),
                          fh, ensure_ascii=False)
            before = open(sp, "rb").read()
            t("引用が無ければ記録を書き換えない", apply(dp, store_path=sp, order_path=op, fetch=f,
                                                    rows=rows, today="2026-11-10") == 1
              and open(sp, "rb").read() == before)
            tried("bbb", "UNREADABLE", store_path=sp, today="2026-11-10", order_path=op)
            s2 = read_store(sp, op)
            t("試しただけでは記録も並び順も変わらない", "bbb" not in s2["records"]
              and read_order(op) == ["aaa"] and s2["tries"]["bbb"]["result"] == "UNREADABLE")
            # 並び順の書き込みが落ちたら控えも戻す
            keep_rep = globals()["_replace_bytes"]
            calls = {"n": 0}

            def boom(path, data):
                calls["n"] += 1
                if path == op:
                    raise OSError("書けない")
                keep_rep(path, data)
            globals()["_replace_bytes"] = boom
            # bbb を aaa より上に入れる決定＝並び順が変わる（-1195 は行の引用にある）
            with open(dp, "w", encoding="utf-8") as fh:
                json.dump(dict(good, slug="bbb", ev_yen=-1195), fh, ensure_ascii=False)
            before_s = open(sp, "rb").read()
            try:
                apply(dp, store_path=sp, order_path=op, fetch=f, rows=rows, today="2026-11-11")
                fell = False
            except OSError:
                fell = True
            globals()["_replace_bytes"] = keep_rep
            t("並び順を書けなければ控えも元へ戻す（全か無か）", fell and open(sp, "rb").read() == before_s)
            # ★控えが消えたのを最初の日と取り違えない★（Codex impl の1）
            os.rename(sp, sp + ".bak")
            try:
                read_store(sp, op)
                lost_ok = False
            except EvError:
                lost_ok = True
            t("★控えが無いのに公開の並び順があれば、読まずに止める★", lost_ok)
            t("★控えが無いときは書かない（並び順を1件で作り直さない）★",
              apply(dp, store_path=sp, order_path=op, fetch=f, rows=rows, today="2026-11-11") == 2
              and read_order(op) == ["aaa"] and not os.path.exists(sp))
            t("★控えが無いときは、試した記録・割り当ても作らない★",
              _raises(lambda: tried("bbb", "ERROR", store_path=sp, order_path=op, today="2026-11-11"))
              and _raises(lambda: next_items(5, store_path=sp, order_path=op, today="2026-11-11",
                                             items=["aaa"]))
              and not os.path.exists(sp))
            os.rename(sp + ".bak", sp)
            # ★一度も成功しないまま止まっても知らせる★（Codex impl の3）
            keep_mm = globals()["mismatches"]
            globals()["mismatches"] = lambda rows, store: []
            sp2 = os.path.join(td, "s2.json")
            op2 = os.path.join(td, "o2.json")
            tried("aaa", "UNREADABLE", store_path=sp2, order_path=op2, today="2026-11-01")
            tried("aaa", "UNREADABLE", store_path=sp2, order_path=op2, today="2026-11-08")
            t("★成功0件のまま始めた日から3日以上たてば知らせる（再試行で日付が進んでも）★",
              check(store_path=sp2, order_path=op2, today="2026-11-08", rows=rows) == 1)
            t("始めたばかりなら知らせない",
              check(store_path=sp2, order_path=op2, today="2026-11-02", rows=rows) == 0)
            globals()["mismatches"] = keep_mm
        finally:
            globals()["targets"] = keep_targets
        # 読む側の検査
        for bad_o, why in (({"schema": "x", "order": []}, "形の違う並び順"),
                           ({"schema": ORDER_SCHEMA, "order": ["a", "a"]}, "同じ機種が2回"),
                           ({"schema": ORDER_SCHEMA, "order": ["A-1"]}, "slug の形でない")):
            with open(op, "w", encoding="utf-8") as fh:
                json.dump(bad_o, fh)
            try:
                read_order(op)
                ok = False
            except EvError:
                ok = True
            t(f"並び順を読む側が断る: {why}", ok)
        t("並び順が無ければ空", read_order(os.path.join(td, "none.json")) == [])

    # 割り当て
    st = empty_store()
    items = ["a", "b", "c", "d", "e", "f", "g"]
    st["tries"] = {"a": {"date": "2026-11-08", "result": "EV"}, "b": {"date": "2026-11-09", "result": "ERROR"},
                   "c": {"date": "2026-11-05", "result": "UNREADABLE"}}
    st["records"] = {"a": {"checked": "2026-11-08"}}
    got = pick(5, items, st, "2026-11-10")
    t("まだ試していない機種が先・失敗して2日たっていない機種は外す",
      got == ["d", "e", "f", "g", "c"])
    st_m = empty_store()
    st_m["tries"] = {s: {"date": "2026-11-01", "result": ("ERROR" if s in "abc" else "EV")} for s in items}
    st_m["records"] = {s: {"checked": "2026-11-01"} for s in "defg"}
    t("★失敗のやり直しと、確かめた機種の取り直しは交互★",
      pick(4, items, st_m, "2026-11-10") == ["a", "d", "b", "e"])
    t("★NO_EV はなな徹を見た記録が要る★",
      record_problems("x", {"state": "NO_EV", "searched": ["https://www.slopachi-quest.com/article/x/"],
                            "by": "claude,codex", "why": "スロパチクエストに表が無かったので値なし",
                            "checked": "2026-11-09"}) != []
      and record_problems("x", {"state": "NO_EV", "searched": [url], "by": "claude,codex",
                                "why": "なな徹にリセット期待値の表が無かったので値なし",
                                "checked": "2026-11-09"}) == [])
    st2 = json.loads(json.dumps(st))
    p1 = plan_today(5, items, st2, "2026-11-10")
    st2["tries"]["d"] = {"date": "2026-11-10", "result": "EV"}
    p2 = plan_today(5, items, st2, "2026-11-10")
    t("同じ日は同じ割り当てを使い回し、試した分は差し引く", p1[1:] == p2 and len(p2) == 4)
    ok_n = sum(1 for _n, c in results if c)
    print(f"\n{ok_n}/{len(results)} 合格")
    return 0 if ok_n == len(results) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--next", type=int)
    ap.add_argument("--apply")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tried")
    ap.add_argument("--result")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--rebuild-order", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.check:
        return check()
    if a.rebuild_order:
        # ★途中で止まって並び順だけ古いときの戻し方★（Codex design2 の8）＝控えから作り直すだけ
        try:
            order = order_from(read_store())
        except EvError as e:
            print(f"★作り直しません★ {e}")
            return 2
        if read_order() == order:
            print("並び順は控えと一致しています（変更なし）")
            return 0
        _replace_bytes(ORDER, _order_bytes(order))
        print("並び順を控えから作り直しました（ページを作り直してコミットする）")
        return CHANGED
    if a.apply:
        return apply(a.apply, dry_run=a.dry_run)
    if a.tried:
        try:
            tried(a.tried, a.result or "")
        except EvError as e:
            print(f"★記録しません★ {e}")
            return 1
        print(f"{a.tried}: 試した記録を残しました（{a.result}）")
        return 0
    if a.next is not None:
        for s in next_items(a.next):
            print(s)
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())

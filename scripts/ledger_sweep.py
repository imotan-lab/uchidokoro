# -*- coding: utf-8 -*-
"""担当した機種の台帳を、その場で読み直して、直っていれば機械が確かめて閉じる。

★★運営者の指示（2026-08-30）★★
> 台帳って以前にあったタスクがどんどん詰んでいったものだよね
> 今ってそのタスクないんだけど2AIでそのへんのおかしいところを
> 見つけて更新する予定だったんだけど無理なの？
→ 「入れよう」

★何が起きていたか★＝毎朝のタスクは担当した機種の**記事だけ**を読み、
その機種について過去に書き留めたメモ（台帳）を**見ていなかった**。
運営者が「台帳を順番決めに使うな」と言ったのを受けて順番から外したとき、
★参照そのものもやめてしまった★。

  結果1＝直したのにメモが開いたまま残る
    （2026-08-30に実測。東京喰種の #284 は朝に直したのに開いたままだった）
  結果2＝記事を読むだけでは気づけない指摘が、メモにしか無いまま眠る
    （実測：58機種に68件が、いまも記事に残ったまま）

★★この道具がやること★★
  ①その機種の開いている案件を出し、「当てられそうな検査」を**参考として**添える
    → ★順番は決めない★（運営者の決めた 新台→人気→その他 は変えない）
  ②2AIが記事を読んで「この検査が全部通れば直っている」と決める
  ③機械がその検査を**全部**やり直し、通ったときだけ閉じる

★★語の名簿で自動的に閉じるのはやめた★★（2026-08-30・Codexの指摘1）
  はじめ「案件の題に出てくる語 → 当てる検査」を作り、当たったものを
  そのまま閉じていた。★これは例外リストで意味を判定する型★で、
  実際に誤って閉じる道が4つあった。

    ・「常体」→ plain_style_gone は、★検査自身が「これだけを根拠に
      文体混在を閉じてはいけない」と書いている★（19通りの文末しか見ない）
    ・「噂の箱が空」→ rumor_not_declared_empty は空箱を見ておらず、
      「噂はありません」という定型文を探す検査。★中身が違う★
    ・「ポチポチくん」を含む案件が全部「リンクが開けるか」になる
      （表示や設定値の誤りでも、リンクさえ開ければ閉じる）
    ・語が2つ当たっても最初の1つしか返さないので、
      「他サイト名＋型式名」の案件は片方だけ直せば閉じられる

  → 名簿は★参考の表示だけ★にした。閉じる検査を決めるのは2AI。

★★「問題の文が消えた＝直った」とはしない★★（2026-08-30に実測）
  東京喰種の #155 は「CZまたはAT当選」という文が消えていたが、
  それは直ったのではなく★恩恵が未確定で載せていない★からだった。
"""
from __future__ import annotations
import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_S = os.path.join(BASE, "scripts")
for _p in (BASE, _S):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import local_paths as _lp                                # noqa: E402
import recheck as _rc                                    # noqa: E402
import safe_json as _sj                                  # noqa: E402

LEDGER = _lp.doc("open_issues.json")

# ★題や詳細に出てくる言葉から「当てられそうな検査」を挙げる★
#   ★★これは参考の表示だけ。閉じる判断には使わない★★（2026-08-30・Codex指摘1）
#     語の一致は意味の一致ではない。決めるのは記事を読んだ2AI。
#   ★当たった検査は全部挙げる★（最初の1つで打ち切らない）
SUGGEST_CHECK = (
    (("他サイト名", "サイト名の露出", "競合サイト"), "competitor_names_gone"),
    (("型式名", "検定番号"), "model_code_gone"),
    (("常体", "文体混在", "だ・である"), "plain_style_gone"),
    (("設定示唆まとめが空", "設定示唆が空", "settei が空"), "settei_filled"),
    (("噂はありません", "噂・未確定情報はありません"),
     "rumor_not_declared_empty"),
    (("交換率別しきい値が逆転", "交換率が良いほど深い", "しきい値が逆転"),
     "rate_monotonic"),
    (("ポチポチくんへ行けない", "ポチポチくんのリンク"),
     "pochipochi_reachable"),
    (("同じ判断を2度", "重複行", "同一事実の重複"), "duplicate_prose_gone"),
)

# ★★これだけを根拠に閉じてはいけない検査★★
#   `recheck.check_plain_style_gone` の説明にそう書いてある＝
#   見ているのは19通りの文末だけで、「常体が無い」ことの証明ではない。
#   ★同じ規則を2か所に書かない★ので、ここでは「単独では通さない」だけを持つ。
NEED_COMPANION = ("plain_style_gone",)


def suggest_checks(issue: dict) -> list:
    """その案件に当てられそうな検査を**全部**挙げる（参考）。"""
    text = (str(issue.get("title") or "") + " "
            + str(issue.get("detail") or ""))
    out = []
    for words, check in SUGGEST_CHECK:
        if any(w in text for w in words) and check not in out:
            out.append(check)
    return out


def _head() -> str:
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=BASE,
                       capture_output=True, text=True)
    return (r.stdout or "").strip()


def _dirty() -> bool:
    """★未コミットの変更があるうちは閉じない★（既存の決まりと同じ）"""
    r = subprocess.run(["git", "status", "--porcelain"], cwd=BASE,
                       capture_output=True, text=True)
    return bool((r.stdout or "").strip())


def _rows() -> list:
    """台帳の案件を読む。

    ★台帳が無い場所でも落ちない★（2026-08-30・実際にCIを赤くした）
      台帳は書類フォルダ（リポジトリの外）にあり、★CIの機械には無い★。
      無いときは「案件0件」として読む。
    ★これは緩めではない★＝閉じる側は `find_issue` が None を返すので、
      案件が引けなければ**断る**（fail-closed のまま）。
    """
    data = _sj.read_json(LEDGER, expect=(dict, list),
                         allow_missing=True, default=[])
    if isinstance(data, list):
        return data
    return (data.get("issues") or []) if isinstance(data, dict) else []


def find_issue(issue_id: int):
    """番号で案件を引く（無ければ None）。★閉じる前に必ず引く★"""
    for r in _rows():
        try:
            if int(r.get("id")) == int(issue_id):
                return r
        except (TypeError, ValueError):
            continue
    return None


def for_slug(slug: str) -> dict:
    """その機種の開いている案件を出す。★書かない★

    ★閉じてよいかは決めない★＝当てられそうな検査と、その今の結果を
    添えるだけ。決めるのは記事を読んだ2AI。
    """
    mine = [r for r in _rows()
            if r.get("slug") == slug and r.get("status") != "closed"]
    out = {"slug": slug, "open": [], "checked": len(mine)}
    head = _head()
    for r in mine:
        row = {"id": r.get("id"),
               "title": str(r.get("title") or "")[:160],
               "detail": str(r.get("detail") or "")[:600],
               "suggest": []}
        for check in suggest_checks(r):
            meta = _rc.CHECKS.get(check) or {}
            ok, why, _got = _rc.closeable(
                {"check": check, "version": meta.get("version"),
                 "args": {"slug": slug}, "expected_commit": head})
            row["suggest"].append({"check": check, "pass": bool(ok),
                                   "why": str(why)[:120]})
        out["open"].append(row)
    return out


def _machine_slugs() -> set:
    """いま一覧にある機種（＝毎朝のタスクが担当しうる機種）。"""
    data = _sj.read_json(os.path.join(BASE, "assets", "data", "machines.json"),
                         expect=(dict, list))
    rows = data if isinstance(data, list) else (data.get("machines") or [])
    return {str(r.get("slug") or "") for r in rows if isinstance(r, dict)}


def for_site(limit: int = 2, today: str = "") -> list:
    """★機種に紐づかない案件を、古い順に少しだけ出す★（2026-08-30）

    ★なぜ要るか★＝毎朝のタスクは「担当した機種の案件」しか見ないので、
      `site` や `_global` の案件は**誰の目にも永久に触れない**
      （実測：開いている 144 件のうち 63 件・うち重要 26 件）。

    ★順番は「見せた日の古い順」★＝1周したらまた回ってくる。
    ★機種の担当順には割り込まない★（運営者が決めた順番は変えない）。
    ★ここでは閉じない★＝閉じるのは今までどおり `--close`（機械が確かめる）。
    """
    known = _machine_slugs()
    mine = [r for r in _rows()
            if r.get("status") != "closed"
            and str(r.get("slug") or "") not in known]
    seen = _seen_map()
    mine.sort(key=lambda r: (seen.get(str(r.get("id")), ""),
                             str(r.get("id"))))
    return mine[:max(0, int(limit))]


def _state_path() -> str:
    return _lp.doc("state.json")


def _seen_map() -> dict:
    """どの案件を、いつ材料として出したか。"""
    st = _sj.read_json(_state_path(), expect=dict,
                       allow_missing=True, default={})
    got = ((st or {}).get("ledger_site") or {}).get("last_shown") or {}
    return {str(k): str(v) for k, v in got.items()} if isinstance(got, dict) \
        else {}


def mark_shown(ids, today: str) -> None:
    """出した案件に日付を付ける（次は後ろへ回る）。"""
    p = _state_path()
    st = _sj.read_json(p, expect=dict, allow_missing=True, default={})
    st = st if isinstance(st, dict) else {}
    box = st.setdefault("ledger_site", {}).setdefault("last_shown", {})
    for i in ids:
        box[str(i)] = today
    tmp = p + ".sweep.tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(st, ensure_ascii=False, indent=1))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def _html_ready(slug: str) -> bool:
    """★公開HTMLが実在するか★（2026-08-30・Codexの指摘4）

    `text_gone` は公開HTMLが**無い**とき「HTMLにも無い」と読んでPASSする。
    ＝「記事データと公開HTMLを見た」という閉じ方の理由と食い違う。
    ★無いなら閉じない側へ倒す★
    """
    return os.path.exists(os.path.join(BASE, "machines", slug, "index.html"))


def run_checks(slug: str, checks, texts, head: str = "") -> tuple:
    """2AIが名指しした検査を**全部**やり直す → (ok, 一件ずつの記録)

    ★★1件でも通らなければ閉じない★★（罠⑮＝免除の条件をゆるくしない）
      1つの案件に問題が2つ書いてあることがある（実例 #284＝
      「狙い目の逆転」と「句点後の半角スペース」）。
      片方だけ確かめて閉じると、もう片方が直っていないまま消える。
    ★検査を1つも渡されなければ通さない★（空で閉じない）
    """
    checks = list(checks or [])
    texts = list(texts or [])
    if not checks and not texts:
        return False, ["確かめる検査が1件もありません"]

    # ★これだけでは閉じられない検査★は、逐語の確認と組でなければ通さない
    lone = [c for c in checks if c in NEED_COMPANION]
    if lone and not texts:
        return False, [f"{'/'.join(lone)} は単独では閉じられません"
                       "（消えた逐語も一緒に確かめてください）"]

    head = head or _head()
    whys = []
    for check in checks:
        meta = _rc.CHECKS.get(check)
        if not meta:
            return False, whys + [f"知らない検査です: {check}"]
        if not meta.get("closeable"):
            return False, whys + [f"観測どまりの検査です: {check}"]
        ok, why, _got = _rc.closeable(
            {"check": check, "version": meta.get("version"),
             "args": {"slug": slug}, "expected_commit": head})
        whys.append(f"{'○' if ok else '×'} {check} ／ {why}")
        if not ok:
            return False, whys
    if texts and not _html_ready(slug):
        return False, whys + ["公開HTMLがありません"
                              "（記事データだけでは閉じません）"]
    meta = _rc.CHECKS["text_gone"]
    for t in texts:
        ok, why, _got = _rc.closeable(
            {"check": "text_gone", "version": meta["version"],
             "args": {"slug": slug, "text": t}, "expected_commit": head})
        whys.append(f"{'○' if ok else '×'} text_gone[{t[:30]}] ／ {why}")
        if not ok:
            return False, whys
    return True, whys


def precheck_close(issue_id, slug: str) -> tuple:
    """★番号・状態・機種が結び付いているか★ → (ok, 理由)

    ★★close_issue から切り出してある★★（2026-08-30）
      理由＝ここを本体の中に埋めていたら、
      ★「木が汚れている」という別の守りに先に当たって★、
      壊し方の道具が4件とも「捕まえられない」になった（罠④）。
      切り出して直接呼べる形にすると、狙った1件だけを試せる。
    """
    row = find_issue(issue_id)
    if row is None:
        return False, f"#{issue_id} という案件がありません"
    if str(row.get("status") or "") == "closed":
        return False, f"#{issue_id} はすでに閉じています"
    if str(row.get("slug") or "") != slug:
        return False, (f"#{issue_id} の機種は {row.get('slug')!r} で、"
                       f"指定の {slug!r} と違います")
    return True, f"#{issue_id} は {slug} の開いている案件です"


def texts_from_issue(row, texts) -> tuple:
    """★逐語はその案件の本文から出ていること★ → (ok, 理由)

    （2026-08-30・Codexの指摘2の3点目）
    ★何が起きるか★＝機種が合っていても、案件と無関係な
    「記事に無い文字列」を渡せば text_gone は必ず通る。
    ＝東京喰種の #155 を、でたらめな文字列で閉じられた。
    ★見るのは案件の題と詳細だけ★（意味は判定しない＝そこに書いてあるか）。
    """
    texts = list(texts or [])
    if not texts:
        return True, "逐語の指定はありません"
    body = (str((row or {}).get("title") or "") + "\n"
            + str((row or {}).get("detail") or ""))
    bad = [t for t in texts if t not in body]
    if bad:
        return False, ("案件に書かれていない逐語です: "
                       + " / ".join(t[:40] for t in bad))
    return True, f"逐語 {len(texts)} 件はすべて案件の本文にあります"


# ★★逐語が消えただけでは閉じられない型★★（2026-08-30・実際にやらかした）
#   台帳の kind は「external_value = 外部数値の疑義（裏取り待ち）」を持つ。
#   ★この型は「値が確かめられていない」ことが中身★なので、
#   文が記事から消えたのは「直った」ではなく「載せるのをやめた」かもしれない。
#   ＝ text_gone は直った証拠にならない。ほかの検査を必ず組にする。
TEXT_GONE_NOT_ENOUGH = ("external_value",)


def kind_allows(row, checks, texts) -> tuple:
    """★その案件の型で、この検査の組み合わせで閉じてよいか★ → (ok, 理由)"""
    kind = str((row or {}).get("kind") or "")
    if kind in TEXT_GONE_NOT_ENOUGH and texts and not checks:
        return False, (f"{kind} は裏取り待ちの型です。"
                       "文が消えたのは「直った」ではなく"
                       "「載せるのをやめた」かもしれません"
                       "（ほかの検査と組にしてください）")
    return True, f"型 {kind or '(なし)'} でこの組み合わせは使えます"


def close_issue(issue_id: int, slug: str, checks, texts, why_extra="") -> int:
    """★案件を閉じる唯一の入口★ 0=閉じた / それ以外=閉じなかった

    ★★番号・機種・検査を結び付ける★★（2026-08-30・Codexの指摘2）
      直す前は番号を見ずに逐語だけ確かめていたので、
      ★別の機種で「存在しない文」を指定すれば、どの案件でも閉じられた★。
    ★★書き込む直前にもう一度確かめる★★（同・指摘3）
      検査と台帳の書き換えの間に別のコミットが入ると、
      「いまの記事で確かめた」と言えなくなる。
    """
    ok, why = precheck_close(issue_id, slug)
    print("  " + why)
    if not ok:
        print("★閉じません★")
        return 1
    row = find_issue(issue_id)
    ok, why = texts_from_issue(row, texts)
    print("  " + why)
    if not ok:
        print("★閉じません★")
        return 1
    ok, why = kind_allows(row, checks, texts)
    print("  " + why)
    if not ok:
        print("★閉じません★")
        return 1
    if _dirty():
        print("★閉じません★ 未コミットの変更があります"
              "（いまの記事で確かめたと言えないため）")
        return 1

    head0 = _head()
    ok, whys = run_checks(slug, checks, texts, head0)
    for w in whys:
        print("  " + w[:130])
    if not ok:
        print("★閉じません★（1件でも通らなければ閉じない）")
        return 1

    # ★書き込む直前に、検査したときと同じ状態のままかを見る★
    if _head() != head0 or _dirty():
        print("★閉じません★ 確かめている間にリポジトリが動きました")
        return 1

    ops = _lp.doc("ops")
    os.makedirs(ops, exist_ok=True)
    p = os.path.join(ops, f"close_{issue_id}.txt")
    lines = ["2AIがこの案件を読み、直っていれば通るはずの検査を決めました。",
             "機械がその検査を全部やり直し、通ったので閉じます。",
             f"機種: {slug} ／ コミット: {head0[:12]}"]
    lines += ["  " + w for w in whys]
    if why_extra:
        lines.append("2AIの理由: " + why_extra)
    lines.append("★AIの宣言ではなく、機械が確かめた結果です★")
    io.open(p, "w", encoding="utf-8", newline="\n").write(
        "\n".join(lines) + "\n")

    r = subprocess.run(
        [sys.executable, os.path.join(_S, "open_issues.py"), "close",
         "--id", str(issue_id), "--reason-file", p],
        cwd=BASE, capture_output=True, text=True, encoding="utf-8",
        errors="replace")
    out = (r.stdout or "").strip() or (r.stderr or "").strip()
    print(out[-200:])
    if r.returncode != 0:
        print(f"★閉じられませんでした★（終了コード {r.returncode}）")
        return r.returncode or 1
    print(f"★閉じました★ #{issue_id}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="担当した機種の台帳を読み直し、機械が確かめて閉じる")
    # ★必須にしない★＝--selftest が起動できなくなり、
    #   壊し方の道具が「壊す前から赤い」になって守りを一度も確かめられない。
    ap.add_argument("--slug", default="")
    ap.add_argument("--close", type=int, metavar="番号",
                    help="この案件を閉じる（--check / --text で検査を名指し）")
    ap.add_argument("--check", action="append", default=[],
                    help="やり直す検査の名前（2AIが決める・複数可）")
    ap.add_argument("--text", action="append", default=[],
                    help="消えているはずの逐語（1件につき text_gone を1回・複数可）")
    ap.add_argument("--why", default="",
                    help="2AIがそう決めた理由（記録に残す）")
    ap.add_argument("--site", action="store_true",
                    help="機種に紐づかない案件を、古い順に少しだけ出す")
    ap.add_argument("--limit", type=int, default=2,
                    help="--site で出す件数（既定 2）")
    ap.add_argument("--record", action="store_true",
                    help="--site で出したものに日付を付ける（次は後ろへ回る）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.site:
        today = datetime.now().strftime("%Y-%m-%d")
        got = for_site(a.limit, today)
        print(f"機種に紐づかない案件から {len(got)} 件（古い順）")
        for r in got:
            print(f"\n  #{r.get('id')} [{r.get('severity') or '-'}] "
                  f"{str(r.get('title'))[:110]}")
            print(f"    {str(r.get('detail') or '')[:400]}")
        if got:
            print("\n★これは「読む材料」です★"
                  "（機種の担当順には割り込みません）")
            print("★閉じるのは今までどおり★＝"
                  "python scripts/ledger_sweep.py --slug <機種> --close <番号> …")
        if a.record and got:
            mark_shown([r.get("id") for r in got], today)
            print(f"次は後ろへ回します: {[r.get('id') for r in got]}")
        return 0
    if not a.slug:
        print("--slug が要ります")
        return 1

    if a.close is not None:
        return close_issue(a.close, a.slug, a.check, a.text, a.why)

    got = for_slug(a.slug)
    print(f"{a.slug}: 開いている案件 {got['checked']} 件")
    for r in got["open"]:
        print(f"\n  #{r['id']} {r['title']}")
        print(f"    {r['detail'][:300]}")
        if r["suggest"]:
            print("    ★参考★ 当てられそうな検査（★これで閉じる判断はしない★）")
            for s in r["suggest"]:
                print(f"      {'○' if s['pass'] else '×'} {s['check']}"
                      f" ／ {s['why'][:70]}")
        else:
            print("    ★参考★ 当てられそうな検査はありません")
    if got["checked"]:
        print("\n★記事を読んで、直っているなら閉じる検査を名指ししてください★")
        print(f"  python scripts/ledger_sweep.py --slug {a.slug} "
              "--close <番号> --check <検査名> --text \"<消えた逐語>\"")
    return 0


def selftest() -> int:
    ng = []
    ran = [0]

    def t(name, cond):
        ran[0] += 1
        print(("✅ " if cond else "❌ ") + name)
        if not cond:
            ng.append(name)

    t("★★題の言葉から検査を挙げる（参考）★★",
      "competitor_names_gone"
      in suggest_checks({"title": "C評価: 他サイト名が本文に出ている"}))
    t("★★当たった検査は全部挙げる★★（最初の1つで打ち切らない）",
      set(suggest_checks({"title": "他サイト名と型式名が残っている"}))
      == {"competitor_names_gone", "model_code_gone"})
    t("★★当てられそうな検査が無ければ空★★",
      suggest_checks({"title": "天井の恩恵が未確定"}) == [])
    for _w, c in SUGGEST_CHECK:
        m = _rc.CHECKS.get(c) or {}
        if not m.get("closeable"):
            ng.append(f"観測どまりの検査を挙げています: {c}")
    t("★★挙げるのは「閉じられる検査」だけ★★",
      not [x for x in ng if "観測" in x])

    # --- ★検査のやり直し★ ------------------------------------------------
    real = ""
    _p = os.path.join(BASE, "assets", "data", "machine-details",
                      "tokyo_ghoul.json")
    if os.path.isfile(_p):
        _d = _sj.read_json(_p, expect=dict)
        for _s in (_d.get("sections") or []):
            for _b in (_s.get("body") or []):
                if isinstance(_b, str) and len(_b) > 30:
                    real = _b[:30]
                    break
            if real:
                break
    gone = "この文はうちどころのどの記事にも存在しません2026"

    # ★★断る理由まで見る★★（2026-08-30・壊し方の道具が4件見逃した）
    #   真偽だけを見ると、木が汚れているだけでも False になるので、
    #   ★狙った守りを一度も通らずに緑になる★（罠④）。
    def why1(*a):
        return (run_checks(*a)[1] or [""])[0]

    t("★★検査を1つも渡されなければ通さない★★",
      "検査が1件もありません" in why1("tokyo_ghoul", [], []))
    t("★★知らない検査の名前は通さない★★",
      "知らない検査です" in why1("tokyo_ghoul", ["そんな検査は無い"], []))
    t("★★観測どまりの検査では閉じられない★★",
      "観測どまりの検査です" in why1("tokyo_ghoul", ["strategy_vs_checker"], []))
    t("★★文体の検査は単独では通さない★★"
      "（recheck 自身が「これだけを根拠に閉じるな」と書いている）",
      "単独では閉じられません" in why1("tokyo_ghoul", ["plain_style_gone"], []))

    # --- ★閉じる入口の前さばき★（★試験用の台帳を自分で作る★） ----------
    #   ★本番の台帳を読まない★（2026-08-30・CIが赤くなった件の本直し）
    #     台帳は書類フォルダ（リポジトリの外）にあり、★CIの機械には無い★。
    #     「無ければ飛ばす」にしたら、CIでは守りを一度も通らず、
    #     壊し方の道具が「守られていません」と言って落ちた（罠④）。
    #   ★本番を読まない利点★＝どこでも同じに動く／台帳の中身が変わっても
    #     試験が落ちない／★本番の台帳を絶対に書き換えない★
    #     （今日、対照実験のつもりで本番の案件を1件消してしまった）。
    _keep_ledger = globals()["LEDGER"]
    _tmpdir = tempfile.mkdtemp(prefix="ledger_sweep_test_")
    try:
        _fake = os.path.join(_tmpdir, "open_issues.json")
        io.open(_fake, "w", encoding="utf-8", newline="\n").write(
            json.dumps({"issues": [
                {"id": 9001, "slug": "tokyo_ghoul", "status": "open",
                 "kind": "quality",
                 "title": "試験用: 記事に『試験用の逐語です』が残っている",
                 "detail": "『試験用の逐語です』という文が本文にあります"},
                {"id": 9002, "slug": "tokyo_ghoul", "status": "closed",
                 "kind": "quality", "title": "試験用: もう閉じた案件",
                 "detail": "閉じています"},
                {"id": 9003, "slug": "tokyo_ghoul", "status": "open",
                 "kind": "external_value", "title": "試験用: 裏取り待ち",
                 "detail": "『裏取り待ちの逐語』が未確定です"},
            ]}, ensure_ascii=False))
        globals()["LEDGER"] = _fake

        t("★★存在しない番号では閉じない★★",
          precheck_close(99999999, "tokyo_ghoul")
          == (False, "#99999999 という案件がありません"))
        t("★★すでに閉じている案件は閉じない★★",
          precheck_close(9002, "tokyo_ghoul")[0] is False)
        _ok, _w = precheck_close(9001, "yajikita_mairu")
        t("★★案件の機種と指定の機種が違えば閉じない★★"
          "（＝別機種の「存在しない文」でどの案件でも閉じられた穴）",
          _ok is False and "と違います" in _w)
        t("　★正しい機種なら前さばきは通る★",
          precheck_close(9001, "tokyo_ghoul")[0] is True)
        t("★★案件に書かれていない逐語では閉じない★★"
          "（＝機種が合っていても、でたらめな文字列で閉じられた穴）",
          texts_from_issue(find_issue(9001), [gone])[0] is False)
        t("　★案件の本文にある逐語なら通る★",
          texts_from_issue(find_issue(9001), ["試験用の逐語です"])[0] is True)
        t("★★台帳が無い場所でも同じように動く★★"
          "（CIの機械には書類フォルダがありません）",
          isinstance(_rows(), list))
        t("　★その機種の開いている案件だけを出す★",
          [r["id"] for r in for_slug("tokyo_ghoul")["open"]] == [9001, 9003])
    finally:
        globals()["LEDGER"] = _keep_ledger
        shutil.rmtree(_tmpdir, ignore_errors=True)

    t("★★裏取り待ちの案件は、逐語が消えただけでは閉じない★★"
      "（＝2026-08-30に #155 を誤って閉じた型）",
      kind_allows({"kind": "external_value"}, [], ["消えた文"])[0] is False)
    t("　★ほかの検査と組なら通る★",
      kind_allows({"kind": "external_value"}, ["text_gone"],
                  ["消えた文"])[0] is True)
    t("　★ほかの型なら逐語だけでも通る★",
      kind_allows({"kind": "quality"}, [], ["消えた文"])[0] is True)

    if _dirty():
        t("★★未コミットの木では、消えている逐語でも閉じない★★",
          run_checks("tokyo_ghoul", [], [gone])[0] is False)
        print("⏭ 木が汚れているので「逐語が消えたか」の4件は飛ばしました"
              "（CI・mutation_check の綺麗な写しで動きます）")
    else:
        t("★★消えている逐語なら通す★★",
          run_checks("tokyo_ghoul", [], [gone])[0] is True)
        t("★★まだ記事に残っている逐語なら通さない★★",
          bool(real) and run_checks("tokyo_ghoul", [], [real])[0] is False)
        t("★★2件のうち1件でも残っていたら通さない★★"
          "（＝片方だけ確かめて閉じる罠を塞ぐ）",
          bool(real)
          and run_checks("tokyo_ghoul", [], [gone, real])[0] is False)
        t("　★順番を入れ替えても同じ★",
          bool(real)
          and run_checks("tokyo_ghoul", [], [real, gone])[0] is False)

    print(f"\n{ran[0] - len(ng)}/{ran[0]} " + ("合格" if not ng else "不合格"))
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


if __name__ == "__main__":
    raise SystemExit(main())

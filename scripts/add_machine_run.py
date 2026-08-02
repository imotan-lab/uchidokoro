"""add_machine_run.py — 新台追加タスクの本体（部品を1本につなぐ）。

★これ1つで通る★
  メーカー公式の一覧を見る → 新台を見つける → 名鑑のURLを名前から探す
  → 型式名を確定 → 記事の材料を集める → 記事データを組み立てる

★止まる所は必ず理由を残す★
  「新台なし」で静かに終わるのが一番こわいので、
  取れなかった・決められなかったときは要確認台帳に残す。

★既定は dry-run★
  `--apply` を付けたときだけ書き込む。書き込む前に
  `task_guard`（1日1機種）と `task_lock`（ロック）を必ず通す。

使い方:
    python scripts/add_machine_run.py                 # 見るだけ
    python scripts/add_machine_run.py --apply --ctx <CTXパス>
    python scripts/add_machine_run.py --name "Lすーぱぁびん娘" \\
        --official-url https://... --maker bellco --release 2026-08   # 1機種だけ試す
    python scripts/add_machine_run.py --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import unicodedata

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

# ★出力の文字コードを固定する★（2026-08-01・実際にpushまで通して見つけた）
#   Windowsでパイプ越しに動かすと出力がcp932になり、
#   「✗」を1つ印字しただけで**タスク全体が途中で即死**していた。
#   無人実行の入口なので、ここで固定する（失敗理由が化けて消えるのも防ぐ）。
for _s in (sys.stdout, sys.stderr):
    if _s is not None and hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

import build_new_article as _ba       # noqa: E402
import check_duplicate as _cd        # noqa: E402
import at_spec_lookup as _at        # noqa: E402
import ceiling_lookup as _cl         # noqa: E402
import cz_lookup as _cz              # noqa: E402
import directory_index as _di         # noqa: E402
import lineage_check as _lc          # noqa: E402
import model_code_lookup as _mc       # noqa: E402
import new_machine_watch as _nw       # noqa: E402
import pending_machines as _pend      # noqa: E402
import prepush_gate as _pg            # noqa: E402
import publish_new_machine as _pub    # noqa: E402
import safe_json as _sj               # noqa: E402
import spec_lookup as _sl             # noqa: E402


def _hide(text: str) -> str:
    """★鍵を伏せる★（2026-07-31・Codex20回目）

    remote URL には利用者名と個人アクセストークンが埋め込んである。
    git の失敗メッセージにはそのURLが出ることがあり、
    そのまま画面・ログ・要確認台帳へ入っていた。
    """
    import re
    return re.sub(r"//[^@/\s]*@", "//***@", text or "")


def _log(msg: str) -> None:
    """★1行ずつファイルに残す★（プロジェクトの最優先ルール）

    無人で動くので、翌朝ログだけで「何を・いくつ・どこに・成否」を追えること。
    画面に出すだけでは、スケジュール実行では何も見えない。
    """
    from datetime import date
    line = f"[{_now()}] {msg}"
    print(line)
    try:
        subprocess.run(
            [sys.executable, r"C:/Users/imao_/.claude/log.py",
             f"add_machine_{date.today().isoformat()}", msg],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30)
    except Exception:                     # noqa: BLE001
        pass                              # ★ログが書けなくても処理は止めない★


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y/%m/%d %H:%M:%S")


def _ledger(slug, kind, severity, code, title, detail) -> bool:
    """要確認台帳に残す。★止まった理由を必ず残すため★

    ★残せたかどうかを返す★（2026-07-31・Codex19回目）
      以前は成否を見ていなかった。台帳に入らなかったのに待ち行列から外すと、
      **待ち行列にも台帳にも無い機種**になる。
      公式URLは既知なので、二度と出てこない＝黙って消える。
    """
    r = subprocess.run(
        [sys.executable, os.path.join(BASE, "scripts", "open_issues.py"), "add",
         "--source", "add-machine", "--slug", slug, "--kind", kind,
         "--severity", severity, "--reason-code", code,
         "--title", title, "--detail", detail],
        cwd=BASE, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    if r.returncode != 0:
        _log(f"  ★台帳に登録できませんでした: {(r.stderr or r.stdout)[:200]}★")
        return False
    return True


def _forget(seen: dict, maker_id: str, url: str) -> None:
    """★そのURLを「見たことがある」から外す★（2026-07-31・Codex20回目）

    待ち行列にも台帳にも残せなかったときだけ使う。
    既知にしてしまうと翌日から新台に出てこないので、機種が黙って消える。
    """
    ent = (seen.get("makers") or {}).get(maker_id) or {}
    urls = ent.get("urls")
    if isinstance(urls, list) and url in urls:
        urls.remove(url)
        ent["count"] = len(urls)


RECHECK_PER_MAKER = 3


def baseline_titles() -> int:
    """★既知URL全部の題を一度だけ控える★（2026-08-02・Codex29回目）

    ローテ（1社3件/晩）だけだと基準がそろうまで1か月以上かかり、
    その間に使い回されたURLは「新しい題」を基準として覚えてしまう。
    導入時に一度だけ全URLを読み、基準の題をそろえる。
    """
    import random
    import time
    seen = _nw._load_seen()
    titles = seen.setdefault("known_titles", {})
    checked = seen.setdefault("name_checked", {})
    todo = [u for m in (seen.get("makers") or {}).values()
            for u in (m.get("urls") or []) if u not in titles]
    print(f"題が未記録の既知URL: {len(todo)} 件")
    ok = ng = 0
    from datetime import date as _date
    for i, url in enumerate(todo, 1):
        try:
            html = _nw._get(url)
            t = unicodedata.normalize("NFKC", _nw.page_title(html)).strip()
            if t:
                titles[url] = t
                checked[url] = _date.today().isoformat()
                ok += 1
            else:
                ng += 1
                _log(f"  題が空でした: {url}")
        except Exception as e:            # noqa: BLE001
            ng += 1
            _log(f"  基準の題を取れませんでした（ローテが拾い直します）: {url} / {e}")
        if i % 20 == 0:
            _nw._save_seen(seen)          # ★途中で落ちても控えは残す★
            print(f"  {i}/{len(todo)} …")
        time.sleep(0.8 + random.random() * 0.8)
    _nw._save_seen(seen)
    _log(f"基準の題の一括取得: 成功{ok}件 / 失敗{ng}件 / 対象{len(todo)}件")
    print(f"完了: 成功{ok} / 失敗{ng}")
    return 0


def recheck_known(mid: str, r: dict, seen: dict, out: dict) -> None:
    """★既知URLの中身のすり替え検知★（2026-08-02・Codex28回目）

    見張りは「新しいURL」しか見ないので、メーカーが既存URLを
    別機種に使い回すと黙って見逃していた。
    毎晩、確認が最も古い既知URLを少数だけ読み直し、
    覚えている機種名と違えば台帳に残す（人が見る）。
    """
    ent = (seen.get("makers") or {}).get(mid) or {}
    known = [u for u in (ent.get("urls") or []) if u not in set(r.get("new") or [])]
    if not known:
        return
    titles = seen.setdefault("known_titles", {})
    checked = seen.setdefault("name_checked", {})
    # ★「最後に試した日」で回す★（2026-08-02・Codex29回目）
    #   成功した日で回すと、読めないURLが最古のまま先頭に居座り、
    #   同じ3件だけを毎晩試して残りが永久に確認されなかった。
    known.sort(key=lambda u: (checked.get(u, ""), u))
    from datetime import date as _date
    for url in known[:RECHECK_PER_MAKER]:
        checked[url] = _date.today().isoformat()   # ★試したら必ず末尾へ★
        try:
            html = _nw._get(url)
        except Exception as e:            # noqa: BLE001
            _log(f"  再確認できませんでした（次のローテで再試行）: {url} / {e}")
            continue
        # ★比べるのは切り詰めた機種名ではなく、題の全文★（Codex29回目）
        #   machine_name はハイフンで切るので「Lシリーズ - 初代」→「 - 新章」の
        #   変化を見分けられなかった。
        now_t = unicodedata.normalize("NFKC", _nw.page_title(html)).strip()
        old_t = titles.get(url) or ""
        if old_t and now_t and old_t != now_t:
            if _ledger("site", "structural", "MATERIAL",
                       "KNOWN_URL_CONTENT_CHANGED",
                       "既知の公式URLのページ題が変わりました（使い回しの疑い）",
                       f"{url} / {old_t[:80]} → {now_t[:80]}"):
                titles[url] = now_t       # ★台帳に残せた時だけ更新★
                out["problems"].append(
                    f"{url}: ページ題が変わりました（{old_t[:40]} → {now_t[:40]}）")
            continue
        if now_t and not old_t:
            titles[url] = now_t           # 初回は覚えるだけ


def discover(persist: bool = True) -> dict:
    """メーカー公式の一覧から新台候補を出す。

    ★persist=False（下見）は何も書かない★（2026-08-02・Codex30回目）
      下見はロックを持たないので、本番実行と重なると
      古い状態の保存が新しい記録を消す競合が起きえた。
      下見では待ち行列・台帳・seen・再確認のどれにも書かない。
    """
    cats = _sj.read_json(_nw.CATALOGS, expect=dict)["catalogs"]
    seen = _nw._load_seen()
    out = {"candidates": [], "problems": [], "first_time": [],
           # ★「新台なし」と言えるのは、正常に読めたメーカーの話だけ★
           #   （2026-07-31・Codexの指摘。読めなかった社と混ぜない）
           "watched": [], "not_watched": []}
    for mid, conf in cats.items():
        if not _nw.is_catalog(conf):
            continue                      # ★覚え書きはメーカーではない★
        if conf.get("status") != "ACTIVE":
            out["not_watched"].append(f"{mid}（{conf.get('status')}）")
            continue
        r = _nw.scan_maker(mid, conf, seen)
        _log(f"見張り {mid}: 状態={r['state']} 一覧={r['total']}件 "
             f"新しいURL={len(r['new'])}件 残存率={r.get('retention')}")
        if r["problem"]:
            out["problems"].append(f"{mid}: {r['problem']}")
            out["not_watched"].append(f"{mid}（{r['state']}）")
            _log(f"  ✗ {mid}: {r['problem'][:120]}")
            continue
        out["watched"].append(mid)
        # ★対応していない形のリンクが混ざっていたら台帳へ★（Codex36回目・社は止めない）
        for w_ in (r.get("shape_warnings") or [])[:5]:
            out["problems"].append(
                f"{mid}: 対応していない形の機種リンクがあります: {w_}"
                "（名簿の直しが要ります）")
        if r["first_time"]:
            out["first_time"].append(f"{mid}（{r['total']}件を記録）")
            # ★初回でも「これから出る新台」は拾う★（2026-08-02・Codex36回目）
            #   監視開始時に既に載っていた新台を既知に沈めない。
            #   登場年月が新台の範囲のものだけ、通常の分類を通して待ち行列へ。
            for url in (r.get("initial_urls") or []):
                c = _nw.classify(url, None,
                                 list_release=(r.get("hints") or {}).get(url))
                kept0 = True
                if c["ok"]:
                    out["candidates"].append({"maker": mid, **c})
                    if persist:
                        kept0 = _remember_url(
                            c.get("official_name") or "", url, mid,
                            (c.get("release") or {}).get("value") or "",
                            "初回の一覧から（新台の範囲内）")
                elif retry_later(c["reasons"]):
                    # ★初回の晩だけ読めなかった将来の新台を沈めない★（Codex37回目）
                    #   取得失敗・メンテ・年月未掲載は明日には変わりうる。
                    if persist:
                        kept0 = _remember_url(
                            c.get("official_name") or "", url, mid,
                            (c.get("release") or {}).get("value") or "",
                            "初回に読めなかった: " + " / ".join(c["reasons"])[:200])
                elif ((r.get("hints") or {}).get(url) or "") \
                        and _nw.is_recent((r.get("hints") or {}).get(url)):
                    # ★一覧カードの年月が新台の範囲なら、分類失敗の種類を問わず残す★
                    #   （2026-08-02・Codex39回目。先行公開直後の薄い個別ページが
                    #     「パチスロのページに見えません」＝永久理由になり、
                    #     初回記録で既知に沈んでいた）
                    if persist:
                        kept0 = _remember_url(
                            c.get("official_name") or "", url, mid,
                            (r.get("hints") or {}).get(url) or "",
                            "初回・個別ページが未完成の疑い: "
                            + " / ".join(c["reasons"])[:200])
                # 範囲外など「やり直しても変わらない」は初回の古い機種＝台帳に残さない
                if persist and not kept0:
                    # ★どこにも残せなかったURLは「見た」ことにしない★（Codex37回目）
                    _forget(seen, mid, url)
                    out["problems"].append(
                        f"{url}: 初回に残せなかったので『見た』ことにしません")
            continue
        for url in r["new"]:
            # ★公式一覧のカードの年月を控えとして渡す★（2026-08-02・Codex27回目）
            #   個別ページに年月が無いメーカー（サミー等）でも記事化できるように。
            c = _nw.classify(url, None,
                             list_release=(r.get("hints") or {}).get(url))
            kept = True
            if c["ok"]:
                out["candidates"].append({"maker": mid, **c})
                # ★seen を書く前に覚える★（2026-07-31・Codex17回目）
                #   あとで覚える形だと、その間に落ちたときに
                #   「既知のURLだが待ち行列にも無い」＝永久に消えた機種になる。
                kept = (_remember_url(c.get("official_name") or "", url, mid,
                                      (c.get("release") or {}).get("value") or "",
                                      "見つけたばかり")
                        if persist else True)
            else:
                out["problems"].append(f"{url}: " + " / ".join(c["reasons"]))
                # ★ここで取りこぼしていた★（2026-07-31・Codex16回目）
                #   このあと _save_seen で「見たことがあるURL」になるので、
                #   翌日はもう新台に出てこない。
                #   一晩だけページが取れなかっただけでも、その機種は永久に消えていた。
                #   あとで載る見込みがある理由なら、待ち行列に入れて毎日やり直す。
                if retry_later(c["reasons"]):
                    kept = (_remember_url(
                        c.get("official_name") or "", url, mid,
                        (c.get("release") or {}).get("value") or "",
                        " / ".join(c["reasons"])[:300]) if persist else True)
                else:
                    # ★やり直しても変わらない理由は、その場で1件ずつ台帳へ★
                    #   （2026-08-02・Codex26回目）まとめ登録は失敗を無視し
                    #   1500字で切るので、誤判定されたURLが台帳にも残らないまま
                    #   既知になり、**翌日から二度と出てこなかった**。
                    #   台帳に残せた時だけ既知にする（残せなければ明日また出てくる）。
                    kept = (_ledger(
                        "site", "structural", "MATERIAL", "URL_PERMANENT_REJECT",
                        "新URLを記事化の対象から外しました（やり直しても変わらない理由）",
                        f"{url} / " + " / ".join(c["reasons"])[:900])
                        if persist else True)
            if not kept:
                # ★どこにも残せなかったURLは「見た」ことにしない★
                #   （2026-07-31・Codex20回目）
                #   待ち行列にも台帳にも残らないまま既知にすると、
                #   翌日から新台に出てこない＝その機種は黙って消える。
                #   覚えないでおけば、明日もう一度あたらしいURLとして出てくる。
                _forget(seen, mid, url)
                out["problems"].append(
                    f"{url}: どこにも残せなかったので『見た』ことにしません")
            elif c.get("official_name"):
                # ★URLごとの機種名を覚える★（2026-08-02・Codex28回目）
                #   既知URLの中身が別機種にすり替わったことに気づくため。
                seen.setdefault("names", {})[url] = c["official_name"]
            # ★発見した時点で「基準の題」も控える★（2026-08-02・Codex30回目）
            #   最初の再確認までにURLが使い回されると、
            #   新しい機種の題を基準として覚えてしまう空白があった。
            if kept and c.get("page_title"):
                from datetime import date as _date30
                seen.setdefault("known_titles", {})[url] = c["page_title"]
                seen.setdefault("name_checked", {})[url] =                     _date30.today().isoformat()
        # ★既知URLの中身がすり替わっていないか、毎晩少しずつ見る★
        #   （2026-08-02・Codex28回目。全件毎晩は重いのでローテーション）
        if persist:
            recheck_known(mid, r, seen, out)
    if persist:
        _nw._save_seen(seen)
    else:
        _log("（下見）seen・待ち行列・台帳には何も書きません")
    _log(f"見張り終了: 正常{len(out['watched'])}社 / 見られず{len(out['not_watched'])}社 "
         f"/ 新台候補{len(out['candidates'])}件 / 確認が要る{len(out['problems'])}件")
    return out


def gather(name: str, maker: str = "") -> dict:
    """1機種ぶんの材料を集める。★止まった理由も返す★"""
    got = {"name": name, "urls": [], "model_code": None, "material": None,
           "problems": []}
    fr = _di.find(name)
    got["urls"] = _di.found_urls(fr)
    # ★使わない名鑑の問題は「票が成立した」と分かってから抑制する★
    #   （2026-08-02・Codex27〜28回目。URLが2件あるだけでは2票ではない＝
    #     型式の独立2票が成立して初めて、3件目の曖昧さを記録だけにしてよい）
    unused_msgs = [f"{did}: {v['state']} {v['why']}"[:160]
                   for did, v in fr["results"].items() if v["state"] != "FOUND"]
    _log(f"材料集め開始: {name} / 名鑑{len(got['urls'])}件 "
         + " ".join(f"{d}={v['state']}" for d, v in fr["results"].items()))
    if len(got["urls"]) < 2:
        got["problems"] += unused_msgs    # ★なぜ足りないかも残す★
        got["problems"].append(
            f"名鑑の個別ページが {len(got['urls'])} 件しか見つかりません（2件以上が要る）")
        return got
    # ★名鑑にも期待するメーカーを渡す★（2026-08-02・Codex40回目）
    looks = [_mc.lookup(u, name, expected_maker=maker) for u in got["urls"]]
    # ★メーカー違いと判明した名鑑は、材料・転載照合からも外す★
    #   （2026-08-02・Codex41回目。型式の票からしか外していなかったので、
    #     同名の別メーカー機のページが材料の2票に復活できた）
    _bad_maker = {r["url"] for r in looks
                  if str(r.get("reason") or "").startswith(
                      "DIRECTORY_MAKER_MISMATCH")}
    if _bad_maker:
        for u in sorted(_bad_maker):
            _log(f"  （別メーカーの名鑑・材料からも除外）{u}")
        got["urls"] = [u for u in got["urls"] if u not in _bad_maker]
        if len(got["urls"]) < 2:
            got["problems"] += unused_msgs
            got["problems"].append(
                f"名鑑の個別ページが {len(got['urls'])} 件しか見つかりません"
                "（2件以上が要る・別メーカーの名鑑を除いた結果）")
            return got
    # ★出典どうしが転載でないか確かめる★（2026-07-31・実際に見つけた）
    #   やんちゃプレスはちょんぼりすたと本文が17行そのまま同じだった。
    #   登録簿に無い転載を2票に数えると、独立2出典の意味が無くなる。
    lin = _lc.check(got["urls"])
    # ★照合できなかった＝独立を確かめられていない★（2026-08-02・Codex31回目）
    #   取得失敗を無視すると「独立か不明な2ページ」を2票にできた。
    for p_ in lin.get("problems") or []:
        got["problems"].append(f"転載照合を実施できません: {p_[:120]}")
    for sp in lin["suspects"]:
        got["problems"].append(
            f"転載の疑い: {sp['a']} と {sp['b']} の本文が {sp['ratio']:.0%} 一致"
            f"（登録簿に系列が書かれていません）")
    mv = _mc.agree(looks)
    got["model_code"] = mv.get("model_code")
    if not mv["adopted"]:
        got["problems"].append("型式名: " + str(mv.get("why", ""))[:160])
    # ★型式の2票が成立した時だけ、使わなかった名鑑の問題を記録に落とす★
    if mv.get("adopted"):
        for m_ in unused_msgs:
            _log(f"  （使わない名鑑）{m_}")
    else:
        got["problems"] += unused_msgs
    # ★採用した型式名の規格印も照合する★（2026-08-02・Codex24回目の助言で実装）
    #   規格の印が無い題（同名の旧機種のページ）が2名鑑でそろうと、
    #   旧機種の型式・スペックで新台の記事を作れてしまう。
    #   L/S世代の型式名は頭に同じ印を持つので、照合できるまで採用しない。
    #   新台の正しいページが名鑑に載れば自然に解ける＝待てば直る（打ち切りは60日）。
    _want_gen = _mc._gen_mark(name)
    if got["model_code"] and _want_gen \
            and _mc._gen_mark(got["model_code"]) != _want_gen:
        got["problems"].append(
            f"型式名の規格印が確認できません（機種は{_want_gen}版なのに、"
            f"型式名「{got['model_code']}」に{_want_gen}の印がありません。"
            "同名の旧機種のページを見ている可能性）")
        got["model_code"] = None
    elif got["model_code"] and not _want_gen:
        # ★公式名から規格（L/S）を読めない機種は照合できない★（2026-08-02・Codex39回目）
        #   照合を飛ばすと、同名の旧機種の型式・材料で新台記事を作れる。
        #   機械では区別を確定できないので、人が確認する（台帳へ）。
        got["problems"].append(
            f"型式名: 機種の規格（L/S）が公式名「{name[:30]}」から読めず、"
            f"型式名「{got['model_code']}」が同名の旧機種のものでないと"
            "確認できません（人が確認してください）")
        got["model_code"] = None
    def _read(mod, jp):
        """器ごとに全ページを読み、★使えなかったページの理由を必ず残す★

        （2026-07-31・自分で再現）以前はページ単位の不採用理由を捨てていたので、
        「本文にCZが6つあるのに3つしか採れなかった」ような取りこぼしが
        誰にも伝わらないまま、材料だけが減っていた。
        """
        pages = [mod.read_page(u, name) for u in got["urls"]]
        for pg in pages:
            if not pg.get("ok"):
                got["problems"].append(
                    f"{jp}: {pg['host']} を使えませんでした（{pg.get('reason', '')[:90]}）")
        return mod.compare(pages)

    got["material"] = _read(_sl, "基本スペック")
    # ★型式名の正本は mv（独立2票）★（2026-08-02・Codex29回目）
    #   基本スペック側は文字列の完全一致で拾うため、空白差があると採用されず、
    #   記事と identity に型式名が入らず、型式の重複検出からも漏れていた。
    if got["model_code"]:
        got["material"]["adopted"]["model_code"] = {
            "value": got["model_code"],
            "sources": list(mv.get("hosts") or [])}
    # ★天井は一式で採る★（値だけ先に載せない）
    got["material"]["ceilings"] = _read(_cl, "天井")
    for nt in got["material"]["ceilings"]["need_third"]:
        got["problems"].append(f"{nt['jp']}: {nt['why']}")
    # ★ATの仕様はモードごとに★（純増を混ぜたら誤情報）
    got["material"]["at_specs"] = _read(_at, "ATの仕様")
    for lb in (got["material"].get("setting_labels_unconfirmed") or []):
        got["problems"].append(
            f"設定{lb}: 出典に出てくるが値が確認できていません（設定の段数を誤る恐れ）")
    # ★CZは名前ごとに★（どのCZの期待度か分からないと誤情報）
    got["material"]["czs"] = _read(_cz, "CZ")
    for nt in got["material"]["czs"]["need_third"]:
        got["problems"].append(f"CZ「{nt['name']}」: {nt['why']}")
    # ★CZらしいのに採れなかった語は必ず報告する★（載せない判断には使わない）
    #   前兆ステージや文中の普通名詞も混じるため、機械では選り分けられない。
    un = got["material"]["czs"].get("unresolved") or []
    if un:
        got["problems"].append(
            "CZかもしれないが採れなかった語: " + "・".join(un[:6]))
    for c in got["material"]["czs"]["adopted"]:
        if c.get("rate_disputed"):
            got["problems"].append(f"CZ「{c['name']}」の期待度: 出典で書き方が異なります")
        if c.get("games_disputed"):
            got["problems"].append(f"CZ「{c['name']}」の継続G数: 出典が食い違っています")
    for nt in got["material"]["at_specs"]["need_third"]:
        jp = "メインAT" if nt["mode"] == "MAIN_AT" else "上位AT"
        got["problems"].append(f"{jp}の仕様: {nt['why']}")
    mat = got["material"]
    _log(f"材料集め終了: {name} / 型式={got.get('model_code')} "
         f"採用={len(mat.get('adopted') or {})}項目 "
         f"天井={len((mat.get('ceilings') or {}).get('adopted') or [])}件 "
         f"AT={len((mat.get('at_specs') or {}).get('adopted') or [])}件 "
         f"CZ={len((mat.get('czs') or {}).get('adopted') or [])}件 "
         f"／問題{len(got['problems'])}件")
    for p in got["problems"]:
        _log(f"  ・{p[:140]}")
    return got


# ★あとで載る見込みがある理由★（待ち行列に入れて毎日やり直す）
#   2026-07-31・実データで見つけた穴:
#   メーカー公式で先に見つけた新台は、名鑑にまだページが無くて止まる。
#   ところが公式URLは「既知」として記録されるので、翌日はもう新台に出ない。
#   ＝**早く見つけた機種ほど取りこぼす**（鮮度を上げる目的と正反対だった）。
RETRYABLE = ("名鑑の個別ページが", "HEALTHY_NO_MATCH", "CATALOG_UNHEALTHY",
             "取得できません", "を使えませんでした", "1つの出典にしかありません",
             "採用できた材料がありません",
             # ★公式がまだ書いていないだけ＝明日には書かれうる★（Codex16回目）
             # ★classify が出す文言そのまま★（似せて書いて一致していなかった）
             "登場年月を書いていません", "登場年月が書かれていません",
             "公式ページから機種名を取れません", "機種名を取れません",
             # ★型式名は導入前には無いのが普通★（2026-07-31・Codex21回目）
             #   「まだ載っていない」を「食い違う」と同じ扱いにしていたので、
             #   明日には載るかもしれない新台を初回で捨てていた。
             "型式名がまだどの名鑑にも載っていません",
             "型式名が1つの名鑑にしか載っていません",
             # ★旧機種のページしか無い間は保留＝新台のページが載れば解ける★
             "型式名の規格印が確認できません",
             # ★一時的な転送は戻れば解ける★（Codex34回目）
             "へ転送されました",
             # ★メンテ・拒否画面は待てば解ける★（Codex36〜37回目。36回目で
             #   足すと言って足し忘れ、永久理由のままだった）
             "読める状態ではありません")
# ★やり直しても意味がない理由★（待たずに台帳へ）
NOT_RETRYABLE = ("既に登録されている疑い", "公式ページと名前が一致しません",
                 "転載の疑い", "AMBIGUOUS_CANDIDATES",
                 # ★何度見ても変わらない／人が見るべきもの★（Codex16回目）
                 "すでに扱っている機種です", "パチスロのページに見えません",
                 "登場年月が新台の範囲外です", "同じURLの機種名が変わりました",
                 "メーカーが名簿にありません", "の場所ではありません",
                 "登場年月が公式と違います",
                 # ★食い違いは人が見るべきもの★（機械では決められない）
                 "名鑑ごとに型式名が食い違っています")


def retry_later(problems: list) -> bool:
    """あとでやり直す価値があるか。★意味の無い待ちはしない★"""
    if any(any(w in p for w in NOT_RETRYABLE) for p in problems):
        return False
    return any(any(w in p for w in RETRYABLE) for p in problems)


# ★書き込みを止める理由★（Codex指摘3・自分で再現を確認）
#   以前は problems を文字列で並べるだけで、**中身を見ずに書き込めた**。
#   機種の同定に関わる問題が1つでもあれば、材料が採れていても書かない。
BLOCKING = ("AMBIGUOUS_CANDIDATES", "CATALOG_UNHEALTHY", "型式名",
            "公式ページと名前が一致しません",
            # ★メーカー・登場年月が公式と食い違うなら書かない★（Codex16回目）
            #   別会社の機種として出す／打てる時期を誤って出す、どちらも読者への誤情報。
            "メーカーが名簿にありません", "の場所ではありません",
            "登場年月が公式と違います", "公式ページに登場年月が書かれていません",
            # ★公式ページを開けないなら、その機種だと確かめられていない★
            #   slug も公式URLから作るので、開けないURLのまま記事を作らない。
            "公式ページを取得できません", "既に登録されている疑い", "2件以上",
            # ★独立性を確かめられないまま2票にしない★（Codex31回目）
            "転載照合を実施できません",
            # ★別のページへ転送された中身で記事を作らない★（Codex34回目）
            "へ転送されました", "トップページへ転送されました",
            "転載の疑い",   # ★登録簿に無い転載があれば止める★
            # ★★ここに入れ忘れていた★★（2026-07-31・Codex18回目）
            #   直したつもりで、書き換える場所を1つ手前と間違えていた。
            #   「文言を返す」ことだけを試験していたので、
            #   **run_one が記事を作るのを拒む**ところまで確かめていなかった。
            # 名簿を読めない＝メーカーが合っているとは言えない
            "メーカー名簿を読めません", "メーカーが指定されていません",
            "はまだ見張れていません", "の公式の場所が名簿にありません",
            # 新台でない機種を新台として出さない
            "登場年月が新台の範囲外です",
            # ★パチンコ機を新台記事にしない★（2026-08-02・Codex28回目）
            #   --name 経路と、取得失敗→待ち行列→fill_missing の経路は
            #   classify() を通らないので、公開前の照合でも必ず見る。
            "パチスロのページに見えません")


def _blocking(problems: list) -> list:
    return [p for p in problems if any(w in p for w in BLOCKING)]


def verify_official(name: str, official_url: str,
                    maker: str = "", release: str = "",
                    release_is_cache: bool = False) -> dict:
    """★公式ページが本当にその機種か確かめる★（Codex指摘1・実際に再現した穴）

    以前は名前とURLを別々に受け取り、照合していなかった。
    そのため「機種Aの名前 ＋ 機種Bの公式URL」で、
    **中身が別機種の記事**を作れてしまった（実際に再現）。

    ★メーカーと登場年月も、渡された値を信じない★（2026-07-31・Codex16回目）
      この2つは `--maker` `--release` の入力のまま記事とページへ入っていた。
      メーカー名を間違えれば別会社の機種として、
      年月を間違えれば「いつ打てるか」を誤って読者に出せる。

    ★登場年月は「公式に書いてあるものを必ず取る」★（Codex17回目）
      渡された年月と照合するだけだと、**空で渡せば検査ごと飛ばせた**。
      さらに新台の範囲かも見る。見ないと、未登録の古い機種を
      「先行記事」として出せてしまう（`--name` の経路に穴があった）。

    返すのは {"problems": [...], "release": 公式に書いてある年月}。
    **記事に使うのは渡された値ではなく、この公式の値**。
    """
    out = {"problems": [], "release": ""}
    # ★転送された先も検査する★（2026-08-02・Codex26回目）
    #   渡されたURLだけ見ていたので、メーカーAのURLが別の場所へ転送されると、
    #   転送先の中身をメーカーAの公式として通せた。
    #   メーカー照合は「実際に読んだ場所（最終URL）」に対して行う。
    #   ★先にこちらでリセットする★（試験の偽取得は到達先を書かないため、
    #     前の呼び出しの残り値を拾わないように）
    _fin = getattr(_nw, "LAST_FINAL_URL", None)
    if isinstance(_fin, dict):
        _fin["url"] = None
    try:
        html = _nw._get(official_url)
    except Exception as e:
        out["problems"].append(f"公式ページを取得できません: {e}")
        return out
    final_url = str(((_fin or {}).get("url") if isinstance(_fin, dict) else None)
                    or official_url)
    if final_url != official_url:
        # ★同一メーカー内でも「別のページ」への転送は通さない★
        #   （2026-08-02・Codex34回目。機種Aが同社の機種Bへ一時転送されると、
        #     slugと公式URLはAのまま、中身はBの記事を作れた）
        #   https化・www・末尾スラッシュの違いだけは許す（redirect_problemの判定）。
        _why_rd = _nw.redirect_problem(official_url, final_url)
        if _why_rd:
            out["problems"].append(f"公式ページが{_why_rd}")
            return out
        _log(f"  公式ページが転送されました（同一ページ扱い）: "
             f"{official_url[:60]} → {final_url[:60]}")
    # ★尾部にはそのメーカーの社名・銘柄だけを追加で許す★（2026-08-02・Codex27回目）
    #   検査を丸ごと外していたら「Lすーぱぁびん娘（SP）|BELLCO」のような
    #   派生機の公式URLを本機として通せた。社名の飾り（|BELLCO / |Sammy）は
    #   名簿から作った許可で通し、派生の印（SP等）は従来どおり弾く。
    # ★読める状態のページか先に見る★（2026-08-02・Codex37回目）
    #   待ち行列の再処理中にHTTP200のメンテ画面が出ると、
    #   「名前が一致しません」（永久理由）として正しい新台を打ち切れた。
    _why_bad = _nw.bad_page(html, looks_like_list=True)
    if _why_bad:
        out["problems"].append(f"公式ページが読める状態ではありません（{_why_bad}）")
        return out
    # ★題がエラー文の soft 404 も待つ★（2026-08-02・Codex39回目。
    #   classify と同じ判定。無いと「名前が一致しません」＝永久理由になった）
    _t_low = unicodedata.normalize("NFKC", _nw.page_title(html)).lower()
    if any(w.lower() in _t_low for w in _nw._BAD_PAGE_WORDS):
        out["problems"].append(
            f"公式ページが読める状態ではありません（題がエラー文です: "
            f"{_nw.page_title(html)[:40]!r}）")
        return out
    # ★回胴機の判定は「ページ全体」ではなく機種固有の領域で★
    #   （2026-08-02・Codex28〜29回目。共通ナビの「パチスロ」の一語で
    #     パチンコ機のページが通ってしまう）
    #   見るのは題とH1だけ。名前の規格印（L/S）も回胴機の証拠に数える。
    _text = _nw._visible_text(html)
    _head_txt = _nw.page_title(html) + " " + " ".join(
        re.sub(r"<[^>]+>", " ", h)
        for h in re.findall(r"(?is)<h1[^>]*>(.*?)</h1>", html))
    _head_txt = unicodedata.normalize("NFKC", _head_txt)
    _slot_w = ("パチスロ", "スロット", "スマスロ", "回胴", "ぱちスロ")
    _slot_ev = (any(w in _head_txt for w in _slot_w)
                or _mc._gen_mark(name) in ("L", "S"))
    _pachi_ev = any(w in _head_txt for w in ("ぱちんこ", "パチンコ", "スマパチ"))         and not any(w in _head_txt for w in _slot_w)
    if _pachi_ev or not _slot_ev:
        out["problems"].append(
            "パチスロのページに見えません（題・見出しに回胴機の証拠が無い）")
    ok, why = _mc.page_is_machine(
        html, name,
        extra_tail_ok=_mc.maker_brand_cores(maker) if maker else None,
        strict_all_tail=True)
    if not ok:
        out["problems"].append(
            f"公式ページと名前が一致しません（{why}）: "
            f"公式のタイトル={_nw.page_title(html)[:40]!r} / 指定名={name!r}")
    if maker:
        out["problems"] += _verify_maker(final_url, maker)
    else:
        out["problems"].append("メーカーが指定されていません")
    got = _nw.release_month(_text)
    if not got and maker:
        # ★個別ページに年月が無ければ、公式一覧のカードから取り直す★
        #   （2026-08-02・Codex27回目。サミーは一覧に「2026.9」・個別には無し）
        #   渡された値は使わない＝いま公式の一覧を読み直して確かめる。
        lv = _release_from_official_list(maker, official_url)
        if lv:
            got = {"value": lv, "precision": "month",
                   "quote": "メーカー公式一覧のカードに記載"}
    if not got:
        out["problems"].append(
            "公式ページに登場年月が書かれていません（こちらで日付を補わない）")
        return out
    out["release"] = str(got.get("value") or "")
    if release and str(release) != out["release"]:
        if release_is_cache:
            # ★待ち行列の年月は「控え」＝公式が変えたら現在の公式値で続行★
            #   （2026-08-02・Codex31回目。以前は食い違いを永久理由として
            #     台帳送りにし、正しい新年月を取得済みなのに機種を失っていた）
            _log(f"  公式が登場年月を変えました（控え={release} → "
                 f"公式={out['release']}）。公式の値で続けます")
        else:
            # 手入力（--release）の食い違いは従来どおり止める
            out["problems"].append(
                f"登場年月が公式と違います（公式={out['release']} / "
                f"渡された値={release}）")
    if not _nw.is_recent(out["release"]):
        out["problems"].append(
            f"登場年月が新台の範囲外です（{out['release']}）")
    return out


# ★一覧の読み直しは一晩に1社1回★（描画つきの一覧もあるため）
_LIST_HINT_CACHE: dict = {}


def _release_from_official_list(maker: str, official_url: str) -> str:
    """★メーカー公式の一覧のカードに書かれた登場年月★（個別に無いときの控え）

    渡された値・待ち行列の記録は使わず、いま公式一覧を読み直して取る。
    取れなければ空文字（＝「書かれていません」として待つ・fail-closed）。
    """
    if maker not in _LIST_HINT_CACHE:
        hints = {}
        try:
            cats = _sj.read_json(_nw.CATALOGS, expect=dict)["catalogs"]
            conf = cats.get(maker)
            if conf and _nw.is_catalog(conf) \
                    and str(conf.get("status") or "") == "ACTIVE":
                if str(conf.get("fetch") or "static") == "render":
                    html, health = _nw._get_rendered(conf["list_url"],
                                                     conf["link_prefix"])
                    if health.get("problem"):
                        html = ""
                else:
                    html = _nw._get(conf["list_url"])
                if html:
                    # ★健全な一覧と確かめてから控えを使う★（2026-08-02・Codex31回目）
                    #   見張りと同じ検査（印・最低件数・拒否画面）を通す。
                    _marker = conf.get("list_marker")
                    _tn = unicodedata.normalize("NFKC", _nw.page_title(html))
                    _pu = _nw.product_urls(html, conf["list_url"],
                                           conf["link_prefix"])
                    _least = int(conf.get("min_expected") or 1)
                    if (len(_pu) >= _least
                            and (not _marker or _tn.startswith(
                                unicodedata.normalize("NFKC", _marker)))
                            and not _nw.bad_page(html, looks_like_list=True)):
                        hints = _nw.list_release_hints(
                            html, conf["list_url"], conf["link_prefix"])
                    else:
                        _log(f"  一覧が健全に読めないので年月の控えを使いません（{maker}）")
        except Exception as e:            # noqa: BLE001
            _log(f"  一覧の年月の控えを読めませんでした（{maker}）: {e}")
        _LIST_HINT_CACHE[maker] = hints
    return str(_LIST_HINT_CACHE[maker].get(
        official_url.rstrip("/") + "/", "") or "")


def _verify_maker(official_url: str, maker: str) -> list:
    """★そのURLは本当にそのメーカーの場所か★（名簿の link_prefix で見る）"""
    try:
        cats = _sj.read_json(_nw.CATALOGS, expect=dict)["catalogs"]
    except Exception as e:                # noqa: BLE001
        return [f"メーカー名簿を読めません: {e}"]
    conf = cats.get(maker)
    if not conf or not _nw.is_catalog(conf):
        return [f"メーカーが名簿にありません（{maker!r}）"]
    # ★見張っていない社は通さない★（2026-07-31・Codex19回目）
    #   名簿に名前だけあって場所（link_prefix）が空の社が実際に5つある。
    #   空だと照合が働かず、**どんなURLでもその社の公式として通っていた**。
    if str(conf.get("status") or "") != "ACTIVE":
        return [f"メーカー {maker} はまだ見張れていません"
                f"（状態={conf.get('status')}）"]
    pre = str(conf.get("link_prefix") or "")
    if not pre:
        return [f"メーカー {maker} の公式の場所が名簿にありません"
                "（照合できないので通しません）"]
    # ★www・httpsの違いは同じ場所として扱う★（2026-08-02・Codex35回目）
    #   redirect_problem はwww差を許すのに、ここが生の前方一致だったので、
    #   名簿がwww付き・転送先がwww無しの正しい新台を永久拒否できた。
    import urllib.parse as _up

    def _hp(u: str):
        q = _up.urlparse(u)
        return (q.netloc.lower().removeprefix("www."), q.path)

    ph, pp = _hp(pre)
    oh, op = _hp(official_url)
    if oh != ph or not op.startswith(pp):
        return [f"公式URLが {maker} の場所ではありません"
                f"（名簿は {pre[:48]}… / 渡されたURLは {official_url[:48]}…）"]
    return []


def _verify_release(html: str, release: str) -> list:
    """★公式ページに書いてある登場年月と同じか★（試験と再利用のために残す）

    ここで見るのは「メーカー自身が自社の機種について書いた年月」なので、
    独立2出典は求めない（自社の発表が一次情報）。
    ただし**こちらが入力した値をそのまま信じることはしない**。
    """
    got = _nw.release_month(_nw._visible_text(html))
    if not got:
        return ["公式ページに登場年月が書かれていません"
                "（こちらで日付を補わない）"]
    if str(got.get("value")) != str(release):
        return [f"登場年月が公式と違います（公式={got.get('value')} / "
                f"渡された値={release}）"]
    return []


# ★試験用の目印★（本番の待ち行列を汚さないため・2026-07-31に実際に混入した）
TEST_MARKS = ("zzz_", "確認機", "テスト機", "m.example", "x.example")


def _remember_url(name, url, maker, release, reason) -> bool:
    """★URLを待ち行列へ入れる（名前が無くてもよい）★

    覚えられなかったときに黙るのが一番危ない。
    seen には入るので、覚え損ねた機種は二度と出てこない。
    """
    if any(w in f"{name} {url}" for w in TEST_MARKS):
        return True
    try:
        pend = _pend.load()
        _pend.add(pend, name, url, maker, release, reason)
        _pend.save(pend)
        return True
    except Exception as e:                # noqa: BLE001
        _log(f"  ★待ち行列に入れられませんでした: {url} / {e}★")
        # ★台帳にも残せなければ「見た」ことにしない★（2026-07-31・Codex20回目）
        #   どちらにも残らないまま seen に入れると、その機種は二度と出てこない。
        return _ledger("site", "structural", "MATERIAL", "PENDING_WRITE_FAILED",
                       "新台を待ち行列に入れられませんでした",
                       f"{url} / {e}")


def _remember(name, official_url, maker, release, problems) -> None:
    """★あとで載る見込みがあるなら覚えておく★（翌日やり直すため）"""
    if not retry_later(problems):
        return
    _remember_url(name, official_url, maker, release,
                  " / ".join(problems)[:300])


def _claim_today(official_url: str) -> bool:
    """★1日1機種の上限をコードに守らせる★（人の判断に任せない）"""
    slug = _ba.slug_from_url(official_url)
    g = subprocess.run(
        [sys.executable, os.path.join(BASE, "scripts", "task_guard.py"),
         "claim", "--task", "add-machine", "--slug", slug],
        cwd=BASE, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    if g.returncode != 0:
        print((g.stdout or g.stderr or "").strip()[:200])
        return False
    return True


PUSH_PENDING = os.path.join(BASE, ".push-pending.json")


def _head() -> str:
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=BASE,
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return (r.stdout or "").strip()


def _mark_push_pending(slug: str, sha: str = "", stage: str = "COMMITTED",
                       parent: str = "") -> None:
    """★どのコミットを出そうとしているかまで残す★（Codex20回目）

    slug だけだと、やり直すときに「コミットからやり直す」ことになり、
    変更が無いので必ず失敗していた。

    ★必ず原子的に書く★（2026-08-01・Codex23回目）
      直接 "w" で開くと先に中身が消える。書いている途中で止まると
      壊れた目印が残り、翌日から**全部の公開が「目印が壊れています」で
      恒久停止**していた（人が直すまで出せない）。
      完成させてから置き換えれば、いつ止まっても目印は前か後の完全な形。
    """
    _pub.write_atomic(PUSH_PENDING, json.dumps(
        {"slug": slug, "sha": sha, "stage": stage,
         "parent": parent, "at": _now()}, ensure_ascii=False))


def _clear_push_pending() -> None:
    try:
        os.remove(PUSH_PENDING)
    except FileNotFoundError:
        pass


def _committed_on_top(parent: str, slug: str) -> bool:
    """★あの続きが、もうコミットされているか★

    先端の親が控えた先端と同じで、その説明に機種名が入っていれば、
    「コミットは通ったが目印を上げる前に止まった」と判断できる。
    """
    r = subprocess.run(["git", "log", "-1", "--format=%P%x1f%B"], cwd=BASE,
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return False
    got = (r.stdout or "").split(chr(31))
    if len(got) < 2:
        return False
    parents = got[0].split()
    return len(parents) == 1 and parents[0] == parent and slug in got[1]


def retry_push_first() -> list:
    """★前回コミットしたのに出せていないものを、先に出す★

    これを片付けないまま次の機種へ進むと、
    未pushのコミットが「許していないファイル」として後続を全部止める。
    """
    if not os.path.isfile(PUSH_PENDING):
        return []
    try:
        got = _sj.read_json(PUSH_PENDING, expect=dict)
    except Exception as e:                # noqa: BLE001
        return [f"出せていない公開の目印が壊れています: {e}"]
    slug = got.get("slug") or ""
    sha = got.get("sha") or ""
    stage = got.get("stage") or ("COMMITTED" if sha else "WRITTEN")
    if stage == "WRITTEN":
        # ★もうコミットされていないか先に見る★（2026-07-31・Codex22回目）
        #   git commit が通った直後に止まると、目印は WRITTEN のままなのに
        #   変更はコミット済みで、コミットからやり直すと必ず失敗していた。
        parent = got.get("parent") or ""
        if parent and _committed_on_top(parent, slug):
            _log(f"★{slug} はもうコミットされていました★ pushだけやります")
            ng = push_after_publish(slug, already_committed=True)
            if ng:
                return [f"{slug} をまだ出せません: " + " / ".join(ng)[:300]]
            _log(f"出せました: {slug}")
            return []
        # ★書いたがコミットまで行けなかった★ 続きからやる
        _log(f"★書いたのにコミットまで行けていません: {slug}★ 続きをやります")
        ng = push_after_publish(slug)
        if ng:
            return [f"{slug} をまだ出せません: " + " / ".join(ng)[:300]]
        _log(f"出せました: {slug}")
        return []
    now = _head()
    if sha and now != sha:
        # ★あのときのコミットが先端でない★
        #   あとから別のコミットが乗っている。機械では正否を決められない。
        return [f"出せていない公開（{slug}）のあとに別のコミットがあります"
                f"（記録={sha[:12]} / いま={now[:12]}）。人が確かめてください"]
    _log(f"★前回コミットしたのに出せていないものがあります: {slug}★ 先に出します")
    # ★コミットはやり直さない★（変更が無いので必ず失敗していた・Codex20回目）
    ng = push_after_publish(slug, already_committed=True)
    if ng:
        return [f"{slug} をまだ出せません: " + " / ".join(ng)[:300]]
    _log(f"出せました: {slug}")
    return []


def finish_publish(res: dict, pend: dict = None) -> list:
    """★公開したあとの後始末★（2026-07-31・Codex17回目）

    push が通って初めて「終わった」。
    通らなかったものを待ち行列から外すと、翌日やり直せなくなる。

    ★行列は呼び出し元のものを使う★（2026-08-01・give_up_now と同じ穴の予防）
      ここで読み直して外すと、呼び出し元が持つ古い行列に残ったままになり、
      あとから保存された瞬間に外したはずの機種が蘇る。
    """
    # ★目印は公開部が「途中」を消す前に作ってある★（Codex22回目）
    #   ここで作ると、公開部から戻る間に止まったときに目印が無くなる。
    ng = push_after_publish(res["slug"])
    if ng:
        return ng
    url = res.get("pending_url")
    if url:
        if pend is None:
            pend = _pend.load()
        if _pend.done(pend, url):
            _pend.save(pend)
            _log(f"待ち行列から外しました: {res.get('name')}")
    return []


# ★一晩に見る上限★（全部やると時間もアクセスも際限が無い）
MAX_TRY_PER_NIGHT = 5


def pick_work(pend: dict) -> list:
    """★今日やる機種を、古い順に並べて返す★（2026-07-31・Codex18回目）

    以前は最古の1件しか返していなかった。
    その1件が記事にできない状態だと、**翌日も同じ1件が選ばれ**、
    後ろに記事にできる新台があっても最大60日待たされていた。
    ＝早く見つけた機種ほど遅れる、という目的と正反対の動きだった。

    順に試して、**実際に公開できた1件**で止める。
    """
    # ★最後に試した日が古い順★（2026-07-31・Codex21回目）
    #   見つけた日だけで並べると、先頭の数件が詰まっているとき
    #   **6件目以降は一度も試されないまま60日で打ち切られていた**。
    #   最後に試した日で回せば、全部が順ぐりに当たる。
    items = _pend.due(pend)
    return sorted(items, key=lambda x: (x.get("last_try") or "",
                                        x.get("first_seen") or "",
                                        x.get("url")))[:MAX_TRY_PER_NIGHT]


def give_up_now(pend: dict, url: str, name: str, problems: list) -> None:
    """★何度やっても無理なものは、行列から出して台帳へ★

    行列に残すと、そのぶん後ろが詰まる。
    黙って消すのではなく、要確認台帳に残して人が見られるようにする。

    ★呼び出し元が持っている行列（pend）から外す★（2026-08-01・複数夜の通しで見つけた）
      以前はここでファイルを読み直して外していたので、
      ループが手元に持つ古い行列には残ったままだった。
      次の機種の「試した」を保存した瞬間に**古い行列ごと上書きされ、
      台帳へ移したはずの機種が毎晩蘇っていた**（台帳にも毎晩同じ件が積まれる）。
      行列の保存は「1回の実行につき1つの行列オブジェクト」に一本化する。
    """
    if not _ledger("site", "structural", "MATERIAL", "PENDING_PERMANENT_BLOCK",
                   "新台を記事にできません（やり直しても変わらない理由）",
                   f"{name} / {url} / " + " / ".join(problems)[:1200]):
        # ★台帳に残せなかったら行列からも外さない★（消えるより残るほうがまし）
        _log(f"  台帳に残せなかったので待ち行列に残します: {name or url}")
        return
    try:
        if _pend.done(pend, url):
            _pend.save(pend)
            _log(f"待ち行列から出して台帳へ移しました: {name or url}")
    except Exception as e:                # noqa: BLE001
        _log(f"  ✗ 待ち行列から出せませんでした: {e}")


def fill_missing(work: dict) -> dict:
    """★毎回、公式ページを見直して名前と年月を最新にする★

    ★空を埋めるだけにしない★（2026-08-02・Codex38回目）
      一時的なエラー画面の題（「ページが見つかりません」等）が名前として
      待ち行列に固定されると、復旧後も `or` のせいで直らず、
      「公式ページと名前が一致しません」（永久理由）で機種を失っていた。
      **読めた時は必ず公式の現在値で置き換える**（読めなければ従来値のまま）。
      こちらで作らないのは従来どおり（公式に無ければ空のまま）。
    """
    try:
        c = _nw.classify(work["url"], None)
    except Exception as e:                # noqa: BLE001
        _log(f"  公式ページを見直せませんでした: {e}")
        return work
    if c.get("official_name"):
        if work["name"] and work["name"] != c["official_name"]:
            # ★芯まで変わっていたら「直す」ではなく使い回しの疑い★
            #   （2026-08-02・Codex41回目。無条件の置き換えだと、
            #     URLが別機種に使い回されたとき新しい名前へ追随して
            #     別機種として公開し、元の機種が黙って消える）
            _old_core = _mc._ci.normalize_core(work["name"])
            _new_core = _mc._ci.normalize_core(c["official_name"])
            if _old_core and _new_core and _old_core != _new_core:
                work["_name_conflict"] = c["official_name"]
                _log(f"  ★機種名の芯が変わっています（使い回しの疑い）: "
                     f"{work['name'][:30]} → {c['official_name'][:30]}★")
                return work
            _log(f"  名前を公式の現在値に直します: "
                 f"{work['name'][:30]} → {c['official_name'][:30]}")
        work["name"] = c["official_name"]
    rel = (c.get("release") or {}).get("value") or ""
    if rel:
        work["release"] = rel
    return work


def push_after_publish(slug: str, already_committed: bool = False) -> list:
    """★公開したら関所を通してpushする★（2026-07-31・Codex16回目）

    手元に置いたままにすると、翌日の実行が「許していない変更がある」で止まる。
    **確かめる → コミット対象を選ぶ → コミット → もう一度確かめる → push**
    の順で、1つでも引っかかったら出さない。
    """
    gate = os.path.join(BASE, "scripts", "prepush_gate.py")

    def _run(*args):
        # ★PYTHONIOENCODING が必須★（2026-08-01・実際にpushまで通して見つけた）
        #   これが無いと、関所が「✗」を含む理由を印字しようとした瞬間に
        #   文字コードの失敗で落ち、**止まった本当の理由が化けて失われていた**。
        #   （同じ対策がこのファイルの他の subprocess には入っていた）
        return subprocess.run([sys.executable, gate, "--slug", slug, *args],
                              cwd=BASE, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"})

    # ★最初から --commit を呼ぶ★（2026-07-31・Codex18回目）
    #   引数なしの関所は「作業ツリーとコミットが一致しているか」まで見る。
    #   公開した直後は必ず一致していないので、**新台は1件もpushできなかった**。
    #   （公開の下見だけして、通しで動かしていなかったので気づけなかった）
    #   --commit の中でも、目印・許した範囲・サイト監査は同じように通る。
    # ★すでにコミット済みなら、コミットからやり直さない★（Codex20回目）
    #   出せなかったものをやり直すとき、変更はもう無い。
    #   それでもコミットしようとして「nothing to commit」で落ち、
    #   **push へ一度もたどり着けなかった**。
    if not already_committed:
        # ★コミットする前の先端を残す★（2026-07-31・Codex22回目）
        #   git commit が通った直後に止まると、目印は WRITTEN のままなのに
        #   変更はもうコミット済みで、次はコミットからやり直して
        #   「nothing to commit」で永久に止まっていた。
        _mark_push_pending(slug, "", "WRITTEN", parent=_head())
        r = _run("--commit")
        if r.returncode != 0:
            return ["関所で止まりました（コミット対象の選別）: "
                    + _hide((r.stdout or r.stderr or "").strip())[:300]]
        # ★コミットする前に「これから出す」と残す★（2026-07-31・Codex19回目）
        #   コミットしたあと push で落ちると、手元にだけ機種がある状態になる。
        #   翌日は machines.json にあるので「既に登録」と判定され、
        #   待ち行列から永久に外れ、さらに未pushコミットが後続のpushも塞ぐ。
        msg = (f"feat(machines): 新台 {slug} の先行記事を追加\n\n"
               "出典2件で一致した項目だけを載せています（status: preview・noindex）。\n\n"
               "Co-Authored-By: Claude <自動タスク> <noreply@anthropic.com>\n")
        c = subprocess.run(["git", "commit", "-m", msg], cwd=BASE,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if c.returncode != 0:
            return ["コミットできませんでした: "
                    + _hide((c.stdout or c.stderr or "").strip())[:300]]
        _mark_push_pending(slug, _head())
    # ★関所に入る前に「検査対象のコミット」を固定する★（2026-08-02・Codex35回目）
    #   関所の後で _head() を取り直すと、その隙間に増えた
    #   未検査のコミットを「検査済み」としてpushできた（34回目の直し漏れ）。
    checked_sha = _head()
    # ★コミットしたあと、もう一度関所★（確かめた中身がそのまま出るか）
    r = _run()
    if r.returncode != 0:
        return ["関所で止まりました（コミット後の確認・pushしていません）: "
                + _hide((r.stdout or r.stderr or "").strip())[:300]]
    # ★確かめた先へ、確かめた枝だけを出す★（2026-07-31・Codex17回目）
    #   裸の `git push` は remote.<名>.push の refspec や push.default に
    #   左右されるので、**確かめた場所と違う所へ出せた**。
    sc = _pg.push_scope()
    # ★差分の基準（手元の origin/main）が、実際のリモートの先端と同じか★
    #   （2026-08-02・Codex24回目）手元の基準が実リモートより進んでいると、
    #   関所は「基準〜HEAD」しか見ないのに、pushは**確かめていない範囲ごと**出せる。
    #   食い違っていたら fetch して止める（次の実行が新しい基準で確かめ直す）。
    lr = subprocess.run(
        ["git", "ls-remote", sc["remote"], f"refs/heads/{sc['dest']}"],
        cwd=BASE, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    remote_sha = (lr.stdout or "").split()[0] if (lr.stdout or "").split() else ""
    base_sha = subprocess.run(
        ["git", "rev-parse", sc["base"]], cwd=BASE, capture_output=True,
        text=True, encoding="utf-8", errors="replace").stdout.strip()
    if lr.returncode != 0 or not remote_sha:
        return ["push先の先端を確かめられませんでした（pushしていません）: "
                + _hide((lr.stderr or "").strip())[:200]]
    if remote_sha != base_sha:
        subprocess.run(["git", "fetch", sc["remote"]], cwd=BASE,
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
        return [f"push先の先端（{remote_sha[:12]}）が手元の基準（{base_sha[:12]}）と"
                "違います。fetchしたので、次の実行で確かめ直します（pushしていません）"]
    # ★基準が今のHEADの祖先であることも確かめる★（早送り以外は出さない）
    anc = subprocess.run(["git", "merge-base", "--is-ancestor", base_sha, "HEAD"],
                         cwd=BASE, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if anc.returncode != 0:
        return [f"手元の枝が基準（{base_sha[:12]}）の続きではありません"
                "（早送りで出せない形。人が確かめてください）"]
    # ★確かめた先端のまま、という条件つきでpushする★（2026-08-02・Codex25回目）
    #   ls-remote と push は別操作なので、その隙間に別PCがリモートを
    #   巻き戻すと、確かめていない範囲ごと再公開できた。
    #   「リモートが base_sha のままなら置き換える」を push 自体に持たせる。
    # ★pushするのは「関所を通したそのコミット」だけ★（2026-08-02・Codex34〜35回目）
    #   関所の間・後にコミットが増えていたら出さない。
    if _head() != checked_sha:
        return [f"関所の後にコミットが増えています（検査済み={checked_sha[:12]} / "
                f"いま={_head()[:12]}）。pushしていません（人が確かめてください）"]
    p = subprocess.run(
        ["git", "push",
         f"--force-with-lease=refs/heads/{sc['dest']}:{base_sha}",
         sc["remote"], f"{checked_sha}:refs/heads/{sc['dest']}"],
        cwd=BASE, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    if p.returncode == 0:
        _clear_push_pending()
    if p.returncode != 0:
        return ["pushできませんでした: "
                + _hide((p.stdout or p.stderr or "").strip())[:300]]
    _log(f"pushしました: {slug}")
    return []


def run_one(name, official_url, maker, release, apply_it=False,
            release_is_cache=False,
            before_write=None) -> dict:
    """1機種を最後まで進める。"""
    out = {"name": name, "slug": None, "wrote": [], "problems": [], "blocked": []}
    _log(f"=== 機種の処理開始: {name} / {maker} / {release} / {official_url} "
         f"/ 書き込み={'する' if apply_it else 'しない'} ===")
    # ★①まず公式ページと名前が同じ機種を指しているか★
    vo = verify_official(name, official_url, maker, release,
                         release_is_cache=release_is_cache)
    out["problems"] += vo["problems"]
    # ★記事に載せるのは公式に書いてある年月★（渡された値ではない）
    release = vo["release"] or release
    # ★②その機種が既に登録されていないか★（2026-07-31・実際に二重登録できた）
    #   手順書には書いてあったが、実行器が呼んでいなかった。
    # ★名前・公式URL・型式名のどれか1つでも一致したら疑う★（2026-07-31・Codex指摘）
    #   型式名は新台では無いことが多いので、無いこと自体は警告にしない。
    for slug, ename, why in _cd.find_duplicates(name, official_urls=[official_url]):
        out["problems"].append(
            f"既に登録されている疑い: slug={slug} name={ename}（{why}）"
            f"／新しいslugで作らず、更新タスクで直すこと")
    got = gather(name, maker)
    out["problems"] += got["problems"]
    # ★型式名でも重複を見る★（2026-07-31・Codex16回目）
    #   最初の重複検査は名前と公式URLしか渡していなかった。
    #   型式名は材料を集めて初めて分かるので、**分かった時点でもう一度見る**。
    #   「名前も公式URLも違うが、実は同じ型式」＝同じ機種を二重に作る経路だった。
    if got.get("model_code"):
        for slug, ename, why in _cd.find_duplicates(
                name, model_codes=[got["model_code"]]):
            out["problems"].append(
                f"既に登録されている疑い: slug={slug} name={ename}"
                f"（型式名が同じ: {got['model_code']} / {why}）"
                f"／新しいslugで作らず、更新タスクで直すこと")
    if not got["material"]:
        out["blocked"] = _blocking(out["problems"])
        if apply_it:
            _remember(name, official_url, maker, release, out["problems"])
        else:
            _log("（下見）待ち行列には触りません")
        return out
    out["slug"] = _ba.slug_from_url(official_url)
    mat = got["material"]
    out["adopted"] = sorted(_sl.FIELDS[k]["jp"] for k in mat["adopted"])
    out["held"] = sorted(_sl.FIELDS[k]["jp"] for k in mat["need_third"])
    out["thin"] = sorted(_sl.FIELDS[k]["jp"] for k in mat["thin"])
    # ★型式名だけでは「材料あり」と数えない★（2026-08-02・Codex29回目の副作用対策）
    #   型式名は identity の正本として adopted に入れるが、
    #   それしか無い記事（スペックも天井も無い）を作ってはいけない。
    usable_mat = {k: v for k, v in mat["adopted"].items() if k != "model_code"}
    if not usable_mat:
        out["problems"].append("採用できた材料がありません（記事を作りません）")
    # ★②同定に関わる問題があれば、材料が採れていても作らない★
    out["blocked"] = _blocking(out["problems"])
    if out["blocked"] or not usable_mat:
        for b in out["blocked"]:
            _log(f"  ★止めました: {b[:140]}")
        if apply_it:
            _remember(name, official_url, maker, release, out["problems"])
        else:
            _log("（下見）待ち行列には触りません")
        _log(f"=== 機種の処理終了（作らず）: {name} ===")
        return out
    machine = _ba.build_machine(out["slug"], name, maker, official_url, release, mat)
    detail = _ba.build_detail(out["slug"], name, release, mat)
    out["preview"] = {"machine": machine, "detail": detail}
    if apply_it:
        # ★公開は専用の経路だけ★（2026-07-31・Codexと相談した案B）
        #   ページを先に置き、最後に一覧へ足す。既存ページは1枚も触らない。
        # ★枠を使うのは公開部の中、最初の書き込みの直前★（Codex20回目）
        #   ここで使うと、途中公開・監査・早見表のずれで断られたときにも
        #   その日の枠が消えていた。
        res = _pub.publish_from_material(
            out["slug"], name, maker, official_url, release, mat,
            apply_it=True, before_write=before_write,
            # ★公開部が「途中」の目印を消す前に引き継ぐ★（Codex22回目）
            #   あとから作ると、その間に止まったときに目印がどこにも無くなる。
            on_written=lambda sl: _mark_push_pending(sl, "", "WRITTEN"))
        out["wrote"] = res["wrote"]
        out["problems"] += res["problems"]
        if res["problems"]:
            out["blocked"] = res["problems"]
            return out
        _log(f"公開しました: {out['slug']} / 書いたファイル{len(out['wrote'])}件 "
             + " ".join(os.path.relpath(w, BASE).replace(os.sep, "/")
                        for w in out["wrote"]))
        # ★待ち行列から外すのは push が通ってから★（2026-07-31・Codex17回目）
        #   ここで外すと、関所やpushで止まったとき
        #   「待ち行列にも無い・手元だけ変わっている」状態になり、
        #   翌日の実行が残骸で止まって、誰も気づかないまま進まなくなる。
        out["pending_url"] = official_url
    _log(f"=== 機種の処理終了: {name} / 止めた理由{len(out['blocked'])}件 "
         f"/ 問題{len(out['problems'])}件 ===")
    return out


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    import inspect
    results = []
    nl = chr(10)

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    real_find, real_read, real_lookup = _di.find, _sl.read_page, _mc.lookup
    # ★試験が本番の待ち行列を触らないようにする★（2026-07-31・実際に架空機種が入った）
    real_store = _pend.STORE
    _tmpdir = __import__("tempfile").mkdtemp(prefix="uchi_pend_")
    _pend.STORE = os.path.join(_tmpdir, "pending.json")
    # ★試験は本番の日次ログにも書かない★（2026-08-01・実際に混入した）
    #   混入すると完了マーカーが末尾から離れ、番兵（task-watchdog）が
    #   「起動したが完走していない」と誤検知しうる。画面出力だけにする。
    real_log = globals()["_log"]
    globals()["_log"] = lambda m: print(f"[selftest-log] {m}")
    try:
        _di.find = lambda n, c=None: {"results": {
            "a": {"state": "FOUND", "url": "https://a.example/1", "why": "",
                  "candidates": [], "surfaces": "1/1", "index_size": 9, "problems": []},
            "b": {"state": "HEALTHY_NO_MATCH", "url": None, "why": "載っていません",
                  "candidates": [], "surfaces": "1/1", "index_size": 9, "problems": []},
        }}
        g = gather("L試験機")
        t("★見つからない名鑑があっても、理由を残して進む★",
          len(g["urls"]) == 1 and any("HEALTHY_NO_MATCH" in p for p in g["problems"]))
        t("★★名鑑が1件だけなら材料を集めに行かない★★（2件以上が要る）",
          g["material"] is None
          and any("2件以上" in p for p in g["problems"]))

        _di.find = lambda n, c=None: {"results": {
            k: {"state": "FOUND", "url": f"https://{k}.example/1", "why": "",
                "candidates": [], "surfaces": "1/1", "index_size": 9, "problems": []}
            for k in ("a", "b")}}
        _mc.lookup = lambda u, n, **k: {"url": u, "model_code": "L1", "reason": "OK"}
        _sl.read_page = lambda u, n: {
            "url": u, "host": u.split("/")[2], "ok": True, "reason": "OK",
            "fields": {"payout_rate": {"1": "97.3%"}}}
        g2 = gather("L試験機")
        t("　2件そろえば型式名と材料を集める",
          g2["model_code"] == "L1" and g2["material"] is not None)

        # ★公式ページは本物を想定して差し替える★
        #   （開けなければ止まる作りなので、通る場合の試験には中身が要る）
        real_get = _nw._get
        _nw._get = lambda u, timeout=20: (
            "<title>L試験機</title><body>2026年9月 登場</body>")
        # ★メーカー名簿も試験用にする★（本番の名簿を書き換えない）
        real_cats = _nw.CATALOGS
        _nw.CATALOGS = os.path.join(_tmpdir, "cats.json")
        with open(_nw.CATALOGS, "w", encoding="utf-8") as _f:
            json.dump({"schema": "maker-catalogs/v1", "catalogs": {"m": {
                "name": "試験", "status": "ACTIVE",
                "list_url": "https://m.example/products/slot/",
                "link_prefix": "https://m.example/products/slot/"}}},
                _f, ensure_ascii=False)

        # -------- Codex16回目の反例（自分で再現してから直した）
        t("★★別会社のURLで記事を作れない★★"
          "（--maker は入力のままデータへ入っていた・Codex16回目）",
          any("場所ではありません" in x for x in _verify_maker(
              "https://other.example/x/", "m")))
        t("★★公式と違う登場年月では記事を作れない★★"
          "（いつ打てるかを誤って読者に出せた・Codex16回目）",
          any("公式と違います" in x for x in _verify_release(
              "<body>2026年9月 登場</body>", "2026-08")))
        t("　名簿に無いメーカーは通さない",
          any("名簿にありません" in x for x in _verify_maker("https://x/", "nosuch")))
        t("★★見張れていない社では、どんなURLでも通さない★★"
          "（公式の場所が名簿に無い社が5つあり、素通りしていた・Codex19回目）",
          _blocking(_verify_maker("https://evil.example/x/", "fujishoji")) != [])
        t("★★台帳に残せなかったら待ち行列から外さない★★"
          "（待ち行列にも台帳にも無い機種＝黙って消える・Codex19回目）",
          "台帳に残せなかったので" in inspect.getsource(give_up_now))
        t("★★コミットしたのに出せていないものを、次の実行で先に出す★★"
          "（未pushのコミットが後続を全部止める・Codex19回目）",
          "_mark_push_pending" in inspect.getsource(push_after_publish)
          and "retry_push_first" in inspect.getsource(main))
        t("★★公開が途中なら、書き込む日は進まない★★"
          "（進むと公開できる機種を待ち行列から捨てていた・Codex19回目）",
          "戻すまで進みません" in inspect.getsource(main))
        t("★★出せていないなら成功として返さない★★"
          "（手元に書いただけで成功にしていた・Codex19回目）",
          "push_ng" in inspect.getsource(main))
        t("★★やり直しはコミットからやらない★★"
          "（変更が無いので必ず失敗し、pushへ一度も届かなかった・Codex20回目）",
          "already_committed=True" in inspect.getsource(retry_push_first))
        t("★★出せていない公開の片付けを、どの経路より先にやる★★"
          "（直接指定の経路がその手前にあり、目印を上書きできた・Codex20回目）",
          inspect.getsource(main).index("retry_push_first")
          < inspect.getsource(main).index("if args.name:"))
        t("★★どこにも残せなかったURLは『見た』ことにしない★★"
          "（待ち行列にも台帳にも無いまま既知になると黙って消える・Codex20回目）",
          (lambda sn: (_forget(sn, "m", "https://x/1"),
                       sn["makers"]["m"]["urls"] == ["https://x/2"])[1])(
              {"makers": {"m": {"urls": ["https://x/1", "https://x/2"],
                                "count": 2}}}))
        t("★★gitの失敗メッセージから鍵を伏せる★★"
          "（push の失敗文にURLごと出て、画面・ログ・台帳へ入っていた）",
          "ghp_x" not in _hide("fatal: https://u:ghp_x@github.com/a/b.git")
          and "***@" in _hide("fatal: https://u:ghp_x@github.com/a/b.git"))
        t("★★1日の枠は公開部の中、最初の書き込み直前に使う★★"
          "（途中公開や監査で断られたときにも枠が消えていた・Codex20回目）",
          "before_write" in inspect.getsource(_pub._publish))
        t("　60日打ち切りも、台帳に残せたときだけ外す",
          "待ち行列に戻しました" in inspect.getsource(main))
        t("★★型式名が『まだ載っていない』と『食い違う』を分ける★★"
          "（同じ扱いで、明日には載る新台を初回で捨てていた・Codex21回目）",
          retry_later(["型式名: 型式名がまだどの名鑑にも載っていません"])
          and not retry_later(["型式名: 名鑑ごとに型式名が食い違っています: {}"]))
        t("★★待ち行列は最後に試した日が古い順に回す★★"
          "（見つけた日だけで並べると6件目以降が一度も試されない・Codex21回目）",
          [x["url"] for x in pick_work({"items": {
              "https://x/a": {"name": "a", "url": "https://x/a", "maker": "m",
                              "release": "2026-09", "first_seen": "2026-07-01",
                              "last_try": "2026-07-31", "tries": 1},
              "https://x/b": {"name": "b", "url": "https://x/b", "maker": "m",
                              "release": "2026-09", "first_seen": "2026-07-20",
                              "last_try": "2026-07-01", "tries": 1}}})]
          == ["https://x/b", "https://x/a"])
        t("★★書けた時点で必ず目印を残す★★"
          "（コミット前に止まると目印が無く、翌日なにも復旧できなかった・Codex21回目）",
          "WRITTEN" in inspect.getsource(run_one)
          and "WRITTEN" in inspect.getsource(retry_push_first))
        t("★★目印は公開部が『途中』を消す前に作る★★"
          "（あとから作ると、戻る間に止まったとき目印が無くなる・Codex22回目）",
          "on_written" in inspect.getsource(run_one)
          and "on_written" in inspect.getsource(_pub._publish))
        t("★★関所の呼び出しに文字コード指定がある★★"
          "（無いと関所が理由を印字した瞬間に落ち、本当の理由が失われた・2026-08-01実機）",
          "PYTHONIOENCODING" in inspect.getsource(push_after_publish))
        t("★★push目印は原子的に書く★★"
          "（途中で止まると壊れた目印が残り、全公開が恒久停止した・Codex23回目）",
          "write_atomic" in inspect.getsource(_mark_push_pending)
          and 'open(PUSH_PENDING, "w"' not in inspect.getsource(_mark_push_pending))
        t("★★push前に実リモートの先端と基準を突き合わせる★★"
          "（基準が古いと確かめていない範囲ごと出せた・Codex24回目）",
          "ls-remote" in inspect.getsource(push_after_publish))
        t("★★pushは「先端がそのままなら」の条件つき★★"
          "（ls-remoteとpushの隙間に巻き戻されると検査外まで出た・Codex25回目）",
          "--force-with-lease=refs/heads/" in inspect.getsource(push_after_publish)
          and "is-ancestor" in inspect.getsource(push_after_publish))
        t("★★下見は目印の片付け（コミット・push）をしない★★"
          "（見るだけの実行がロック無しで公開していた・Codex25回目）",
          "下見では触りません" in inspect.getsource(main)
          and inspect.getsource(main).index("if args.apply:")
          < inspect.getsource(main).index("retry_push_first()"))
        t("★★やり直しても変わらないURLは、台帳に残せた時だけ既知にする★★"
          "（まとめ登録は失敗無視＋1500字切りで、機種が黙って消えた・Codex26回目）",
          "URL_PERMANENT_REJECT" in inspect.getsource(discover))
        t("★★メーカー照合は転送先（最終URL）に対して行う★★"
          "（別の場所へ転送されても元のURLで照合していた・Codex26回目）",
          "LAST_FINAL_URL" in inspect.getsource(verify_official)
          and "_verify_maker(final_url" in inspect.getsource(verify_official))
        t("★★採用した型式名の規格印も照合する★★"
          "（旧機種のページが2名鑑でそろうと旧型式で新台を書けた・Codex24回目）",
          "規格印が確認できません" in inspect.getsource(gather)
          and retry_later(["型式名の規格印が確認できません（機種はL版なのに…）"])
          and "型式名の規格印が確認できません" not in str(NOT_RETRYABLE))
        # ★台帳へ移した機種が、次の保存で行列に蘇らない★
        #   （2026-08-01・複数夜の通しで見つけた。give_up_now が別読みして
        #     外していたので、ループ側の古い行列の保存が削除を打ち消していた）
        _real_store = _pend.STORE
        _real_lg = globals()["_ledger"]
        _pend.STORE = os.path.join(_tmpdir, "pend_resurrect.json")
        globals()["_ledger"] = lambda *a, **k: True
        try:
            _pd = {"schema": _pend.SCHEMA, "items": {}}
            _pend.add(_pd, "残る機種", "https://m.example/stay/", "m", "2026-09")
            _pend.add(_pd, "台帳行き", "https://m.example/dead/", "m", "2026-09")
            _pend.save(_pd)
            give_up_now(_pd, "https://m.example/dead/", "台帳行き", ["x"])
            _pend.mark_tried(_pd, "https://m.example/stay/")   # ループの次の周
            _pend.save(_pd)
            _after = _pend.load()["items"]
            t("★★台帳へ移した機種が次の保存で蘇らない★★"
              "（毎晩蘇って行列に居座り、台帳にも同じ件が積まれ続けた・2026-08-01実機）",
              "https://m.example/dead/" not in _after
              and "https://m.example/stay/" in _after)
            t("　行列の保存は呼び出し元の1オブジェクトに一本化",
              "pend" in inspect.signature(give_up_now).parameters
              and "pend" in inspect.signature(finish_publish).parameters)
        finally:
            _pend.STORE = _real_store
            globals()["_ledger"] = _real_lg
        t("★★コミットが通った直後に止まっても、次で分かる★★"
          "（WRITTEN のままコミット済みだと、やり直しが永久に失敗した・Codex22回目）",
          "_committed_on_top" in inspect.getsource(retry_push_first)
          and "parent=_head()" in inspect.getsource(push_after_publish))

        # ★★「文言が返る」ではなく「記事を作らない」ところまで見る★★
        #   （2026-07-31・Codex18回目。文言の試験しかしていなかったので、
        #     止める理由の一覧に入れ忘れていたことに気づけなかった）
        _nw._get = lambda u, timeout=20: (
            "<title>X</title><body>2019年4月 登場</body>")
        _old = run_one("L試験機", "https://m.example/products/slot/zzz/", "m", "")
        t("★★古い機種は記事そのものを作らない★★（通しで確かめる・Codex18回目）",
          "preview" not in _old and _old["wrote"] == []
          and any("範囲外" in x for x in _old["blocked"]))
        _nw._get = lambda u, timeout=20: (
            "<title>L試験機</title><body>2026年9月 登場</body>")
        _bad = run_one("L試験機", "https://m.example/products/slot/zzz/", "nosuch", "2026-09")
        t("★★名簿に無いメーカーでは記事そのものを作らない★★（通しで確かめる）",
          "preview" not in _bad and _bad["wrote"] == []
          and any("名簿" in x for x in _bad["blocked"]))
        _real_cats2 = _nw.CATALOGS
        _nw.CATALOGS = os.path.join(_tmpdir, "こわれている.json")
        with open(_nw.CATALOGS, "w", encoding="utf-8") as _f:
            _f.write("{ こわれた")
        _brk = run_one("L試験機", "https://m.example/products/slot/zzz/", "m", "2026-09")
        _nw.CATALOGS = _real_cats2
        t("★★名簿を読めないときも記事を作らない★★"
          "（読めない＝合っているとは言えない・Codex18回目）",
          "preview" not in _brk and _brk["wrote"] == []
          and any("名簿" in x for x in _brk["blocked"]))

        r = run_one("L試験機", "https://m.example/products/slot/zzz/", "m", "2026-09")
        t("★既定では書き込まない（dry-run）★", r["wrote"] == [])
        t("　組み立てた結果を返す（中身を見てから書ける）",
          r["preview"]["machine"]["status"] == "preview")
        t("　slugは公式URLから作る", r["slug"] == "zzz")

        _sl.read_page = lambda u, n: {"url": u, "host": u.split("/")[2], "ok": True,
                                      "reason": "OK", "fields": {}}
        r2 = run_one("L試験機", "https://m.example/products/slot/zzz/", "m", "2026-09")
        t("★★材料がゼロなら記事を作らない★★",
          "preview" not in r2 and any("採用できた材料" in p for p in r2["problems"]))

        # -------- Codexの反例（2026-07-31・自分で再現を確認してから修正）
        _sl.read_page = lambda u, n: {
            "url": u, "host": u.split("/")[2], "ok": True, "reason": "OK",
            "fields": {"payout_rate": {"1": "97.3%"}}}
        _mc.lookup = lambda u, n, **k: {"url": u, "model_code": None,
                                   "reason": "MODEL_CODE_NOT_FOUND"}
        r3 = run_one("L試験機", "https://m.example/products/slot/zzz/", "m", "2026-09")
        t("★★型式名が確定していなければ記事を作らない★★"
          "（材料が採れていても作れてしまう穴があった）",
          "preview" not in r3 and any("型式名" in x for x in r3["blocked"]))

        _mc.lookup = lambda u, n, **k: {"url": u, "model_code": "L1", "reason": "OK"}
        _di.find = lambda n, c=None: {"results": {
            "a": {"state": "FOUND", "url": "https://a.example/1", "why": "",
                  "candidates": [], "surfaces": "1/1", "index_size": 9, "problems": []},
            "b": {"state": "FOUND", "url": "https://b.example/1", "why": "",
                  "candidates": [], "surfaces": "1/1", "index_size": 9, "problems": []},
            "c": {"state": "AMBIGUOUS_CANDIDATES", "url": None, "why": "候補が3件",
                  "candidates": [1, 2, 3], "surfaces": "1/1", "index_size": 9,
                  "problems": []}}}
        r4 = run_one("L試験機", "https://m.example/products/slot/zzz/", "m", "2026-09")
        t("★★使わない3件目の名鑑が曖昧でも、成立した2票を捨てない★★"
          "（永久理由扱いで即・台帳送りになっていた・Codex27回目）",
          not any("AMBIGUOUS" in x for x in r4.get("problems") or []))
        # ★票が成立しなかった時は、3件目の曖昧さも残す★（Codex28回目）
        _real_lookup28 = _mc.lookup
        _mc.lookup = lambda u, n, **k: {"url": u, "model_code": None,
                                   "reason": "MODEL_CODE_NOT_FOUND"}
        r4c = run_one("L試験機", "https://m.example/products/slot/zzz/", "m", "2026-09")
        _mc.lookup = _real_lookup28
        t("★★票が成立しなければ、使わなかった名鑑の曖昧さも問題として残す★★"
          "（URL2件=2票ではない・Codex28回目）",
          any("AMBIGUOUS" in x for x in r4c.get("problems") or []))
        _di.find = lambda n, c=None: {"results": {
            "a": {"state": "FOUND", "url": "https://a.example/1", "why": "",
                  "candidates": [], "surfaces": "1/1", "index_size": 9, "problems": []},
            "c": {"state": "AMBIGUOUS_CANDIDATES", "url": None, "why": "候補が3件",
                  "candidates": [1, 2, 3], "surfaces": "1/1", "index_size": 9,
                  "problems": []}}}
        r4b = run_one("L試験機", "https://m.example/products/slot/zzz/", "m", "2026-09")
        t("　2票に満たないときの曖昧は、従来どおり問題として残す（人が解く）",
          "preview" not in r4b
          and any("AMBIGUOUS" in x for x in r4b["blocked"]))

        real_get, real_page = _nw._get, _mc.page_is_machine
        try:
            _nw._get = lambda u, timeout=20: "<title>ぜんぜん別の機種</title>"
            _mc.page_is_machine = real_page
            v = verify_official(
                "Lすーぱぁびん娘",
                "https://m.example/products/slot/other/")["problems"]
            t("★★公式ページが別機種なら止める★★"
              "（機種Aの名前＋機種BのURLで記事ができた穴・実際に再現した）",
              any("一致しません" in x for x in v))
            _nw._get = lambda u, timeout=20: (
                "<title>Lすーぱぁびん娘（SP） | EXAMPLE</title>"
                "<body>パチスロ 2026年9月 登場</body>")
            v2 = verify_official(
                "Lすーぱぁびん娘",
                "https://m.example/products/slot/lbinko/", "m")["problems"]
            t("★★派生機の公式URL（…（SP）|社名）を本機として通さない★★"
              "（公式だけ尾部検査を外していて通っていた・Codex27回目）",
              any("一致しません" in x for x in v2))
            # ★個別ページに年月が無くても、公式一覧の控えで通る★（Codex27回目）
            _nw._get = lambda u, timeout=20: (
                "<title>Lすーぱぁびん娘|EXAMPLE</title><body>パチスロの本文に年月なし</body>")
            _LIST_HINT_CACHE.clear()
            _LIST_HINT_CACHE["m"] = {
                "https://m.example/products/slot/lbinko/": "2026-09"}
            v3 = verify_official("Lすーぱぁびん娘",
                                 "https://m.example/products/slot/lbinko/", "m")
            _LIST_HINT_CACHE.clear()
            t("★★個別に年月が無くても、公式一覧のカードの年月で通せる★★"
              "（サミーは一覧に「2026.9」・個別には無し・Codex27回目）",
              v3["release"] == "2026-09"
              and not any("書かれていません" in x for x in v3["problems"]))
            # ★パチンコ機のページは公開前の照合で止まる★（Codex28〜29回目）
            #   共通ナビに「パチスロ」があっても、題・見出しに回胴機の証拠が
            #   無ければ通さない（ページ全体の語では判定しない）。
            _nw._get = lambda u, timeout=20: (
                "<title>コスモアタック|EXAMPLE</title>"
                "<nav>パチンコ・パチスロ製品情報</nav>"
                "<h1>ぱちんこコスモアタック</h1>"
                "<body>ぱちんこ新台のご案内 2026年9月 登場</body>")
            v4 = verify_official("コスモアタック",
                                 "https://m.example/products/slot/cosmo/", "m")
            t("★★本文ナビの「パチスロ」だけではパチンコ機を通さない★★"
              "（題・H1の機種固有領域で判定・Codex29回目）",
              any("パチスロのページに見えません" in x for x in v4["problems"])
              and _blocking(["パチスロのページに見えません（題・見出しに回胴機の証拠が無い）"]))
            _nw._get = lambda u, timeout=20: (
                "<title>スマスロ コスモアタック|EXAMPLE</title>"
                "<body>パチスロ 2026年9月 登場</body>")
            v4b = verify_official("スマスロ コスモアタック",
                                  "https://m.example/products/slot/cosmo/", "m")
            t("　題に回胴機の証拠（スマスロ等）があれば通る",
              not any("パチスロのページに見えません" in x for x in v4b["problems"]))
            # ★既知URLの中身のすり替えを見つける★（Codex28回目）
            _seen28 = {"schema": "seen-machine-urls/v1", "makers": {"m": {
                "urls": ["https://m.example/products/slot/aaa/"], "count": 1}},
                "known_titles": {"https://m.example/products/slot/aaa/":
                                 "前の機種|EXAMPLE"}}
            _out28 = {"problems": []}
            _nw._get = lambda u, timeout=20: "<title>新しい別機種|EXAMPLE</title>"
            _real_lg28 = globals()["_ledger"]
            globals()["_ledger"] = lambda *a, **k: True
            try:
                recheck_known("m", {"new": []}, _seen28, _out28)
            finally:
                globals()["_ledger"] = _real_lg28
            t("★★既知URLの題が変わったら台帳に残して知らせる★★"
              "（使い回しは差分0件で黙って見逃していた・Codex28〜29回目）",
              any("変わりました" in x for x in _out28["problems"])
              and _seen28["known_titles"]["https://m.example/products/slot/aaa/"]
              == "新しい別機種|EXAMPLE")
            t("　読めなかったURLも巡回の末尾へ送る（同じ3件で詰まらない）",
              "checked[url] = _date.today().isoformat()   # ★試したら必ず末尾へ★"
              in inspect.getsource(recheck_known))
            t("★★下見は seen・待ち行列・台帳のどれにも書かない★★"
              "（ロック無しの下見の保存が本番実行の記録を消せた・Codex30回目）",
              "persist" in inspect.signature(discover).parameters
              and "if persist:" in inspect.getsource(discover)
              and "d = discover(persist=apply_it)" in inspect.getsource(main))
            _src_pp = inspect.getsource(push_after_publish)
            t("★★pushするSHAは関所に入る前に固定し、後で増えたら出さない★★"
              "（関所の後のHEAD取り直しで未検査コミットを出せた・Codex35回目）",
              _src_pp.index("checked_sha = _head()")
              < _src_pp.index("コミットしたあと、もう一度関所")
              and "関所の後にコミットが増えています" in _src_pp)
            # ★www差のある名簿でも正しい場所として通す★（Codex35回目）
            _cats35 = _sj.read_json(_nw.CATALOGS, expect=dict)
            _cats35["catalogs"]["w"] = {
                "name": "ダブル", "status": "ACTIVE",
                "list_url": "https://www.w.example/products/slot/",
                "link_prefix": "https://www.w.example/products/slot/"}
            with open(_nw.CATALOGS, "w", encoding="utf-8") as _f35:
                json.dump(_cats35, _f35, ensure_ascii=False)
            t("★★www・https差だけなら「メーカーの場所」として通す★★"
              "（転送許可と食い違い、正しい新台を永久拒否できた・Codex35回目）",
              _verify_maker("https://w.example/products/slot/shin1/", "w") == []
              and _verify_maker("https://yoso.example/products/slot/x/", "w") != [])
            t("★★メンテ画面の理由はやり直す価値がある★★"
              "（36回目で足すと言って足し忘れていた・Codex37回目）",
              retry_later(["公式ページが読める状態ではありません（『メンテナンス中』）"]))
            _nw._get = lambda u, timeout=20: (
                "<title>Access Denied</title><p>ただいまメンテナンス中です</p>")
            v5 = verify_official("Lすーぱぁびん娘",
                                 "https://m.example/products/slot/lbinko/", "m")
            t("★★公開前の照合でもメンテ画面を永久理由にしない★★（Codex37回目）",
              any("読める状態ではありません" in x for x in v5["problems"])
              and not any("一致しません" in x for x in v5["problems"]))
            t("★★読み直せた時は待ち行列の名前を公式の現在値へ置き換える★★"
              "（エラー題の名前がorのせいで直らなかった・Codex38回目）",
              "名前を公式の現在値に直します" in inspect.getsource(fill_missing)
              and " or (c.get" not in inspect.getsource(fill_missing))
            t("★★規格を読めない公式名では型式を採用しない★★"
              "（照合を飛ばすと同名旧機種の型式・材料で新台を書けた・Codex39回目）",
              "規格（L/S）が公式名" in inspect.getsource(gather))
            t("★★公開前の照合でも soft 404（題がエラー文）を待つ★★（Codex39回目）",
              "題がエラー文です" in inspect.getsource(verify_official))
            t("★★初回・カード年月が新台範囲なら分類失敗でも残す★★"
              "（薄い先行ページが永久理由で既知に沈んだ・Codex39回目）",
              "初回・個別ページが未完成の疑い" in inspect.getsource(discover))
            t("★★メーカー違いの名鑑は材料・転載照合からも外す★★"
              "（型式の票からしか外れず材料に復活できた・Codex41回目）",
              "材料からも除外" in inspect.getsource(gather))
            t("★★機種名の芯が変わったURLは公開へ進めない★★"
              "（使い回し検知が公開を止めていなかった・Codex41回目）",
              "_name_conflict" in inspect.getsource(fill_missing)
              and "_name_conflict" in inspect.getsource(main))
            t("★★初回に読めなかった将来の新台を沈めない★★（Codex37回目）",
              "初回に読めなかった" in inspect.getsource(discover)
              and "初回に残せなかったので" in inspect.getsource(discover))
            t("★★発見した時点で基準の題を控える★★"
              "（最初の再確認までの使い回しを見逃した・Codex30回目）",
              "known_titles" in inspect.getsource(discover)
              and "page_title" in inspect.getsource(discover))
            t("★★下見は待ち行列・台帳を変えない★★"
              "（60日打ち切り・試行記録・台帳送りが下見でも進んでいた・Codex28回目）",
              "60日超えの待ち" in inspect.getsource(main)
              and "if apply_it:" in inspect.getsource(main)
              and "（下見）やり直しても変わらない理由です" in inspect.getsource(main))
            _nw._get = lambda u, timeout=20: "<title>Lすーぱぁびん娘|EXAMPLE</title>"
            t("★★既に登録されている機種は作らない★★（実際に二重登録できた・2026-07-31）",
              _blocking(["既に登録されている疑い: slug=super_binmusume"]))
            t("　実データでも既存機種を見つけられる",
              _cd.find_duplicates("Lすーぱぁびん娘"))
            # ★名前が違っても、公式URL・型式名で捕まえる★（Codex指摘・2026-07-31）
            import json as _json
            import tempfile as _tmp
            _real_m = _cd.MACHINES
            _dir = _tmp.mkdtemp(prefix="uchi_dup_")
            try:
                _f = os.path.join(_dir, "machines.json")
                with open(_f, "w", encoding="utf-8") as _fh:
                    _json.dump([{"slug": "aaa", "name": "ぜんぜん違う名前",
                                 "identity": {
                                     "official_product_url":
                                         "https://www.example.jp/products/slot/x/",
                                     "regulatory_model_code": "Lびん娘NY1"}}],
                               _fh, ensure_ascii=False)
                _cd.MACHINES = __import__("pathlib").Path(_f)
                t("★★名前が違っても公式URLが同じなら疑う★★"
                  "（追跡パラメータ・wwwの有無は無視する）",
                  _cd.find_duplicates("新しい名前", official_urls=[
                      "https://example.jp/products/slot/x?utm_source=z"]))
                t("★名前が違っても型式名が同じなら疑う★",
                  _cd.find_duplicates("新しい名前", model_codes=["Ｌびん娘 NY1"]))
                t("　手がかりが無ければ疑わない（型式が無いこと自体は警告にしない）",
                  not _cd.find_duplicates("新しい名前"))
            finally:
                _cd.MACHINES = _real_m
                __import__("shutil").rmtree(_dir, ignore_errors=True)
            t("　実在しない名前なら重複としない",
              not _cd.find_duplicates("そんな機種はありませんXYZ"))
            t("★★公式ページを開けないときは記事を作らない★★（機種を確かめられていない）",
              _blocking(["公式ページを取得できません: 取得できません（URLError）"]))
            _nw._get = lambda u, timeout=20: (
                _ for _ in ()).throw(RuntimeError("開けない"))
            r5 = run_one("L試験機", "https://m.example/products/slot/zzz/", "m", "2026-09")
            t("★★試したときの架空機種は待ち行列に入れない★★（実際に混入した）",
              _remember("通し確認機ZZZ",
                        "https://m.example/products/slot/zzz_x/", "m",
                        "2026-09", ["名鑑の個別ページが 1 件"]) is None)
            t("★★詰まっている機種が後ろを塞がない★★"
              "（最古の1件しか見ず、最大60日待たされていた・Codex18回目）",
              len(pick_work({"items": {
                  f"https://x/{i}": {
                      "name": f"n{i}", "url": f"https://x/{i}", "maker": "m",
                      "release": "2026-09", "first_seen": f"2026-07-0{i+1}",
                      "last_try": "", "tries": 1} for i in range(3)}})) == 3)
            t("　一晩に見る数には上限がある", MAX_TRY_PER_NIGHT <= 5)
            t("★★試験が本番の待ち行列を触らない★★（架空機種が入り込んだ）",
              _pend.STORE.startswith(_tmpdir))
            t("　実際に開けない公式URLでは組み立てまで進まない",
              "preview" not in r5
              and any("公式ページを取得できません" in x for x in r5["blocked"]))
            _nw._get = lambda u, timeout=20: (
                "<title>Lすーぱぁびん娘|EXAMPLE</title><body>パチスロ 2026年9月 登場</body>")
            t("　同じ機種なら通る",
              verify_official("Lすーぱぁびん娘", "https://m.example/products/slot/lbinko/",
                              "m")["problems"] == [])
            t("★★登場年月を渡さなくても、公式から必ず取って確かめる★★"
              "（空で渡せば検査ごと飛ばせた・Codex17回目）",
              verify_official("Lすーぱぁびん娘",
                              "https://m.example/products/slot/lbinko/",
                              "m")["release"] == "2026-09")
            _nw._get = lambda u, timeout=20: (
                "<title>Lすーぱぁびん娘|EXAMPLE</title><body>パチスロ 2019年4月 登場</body>")
            t("★★古い機種を新台として出せない★★"
              "（--name の経路は新台の範囲を見ていなかった・Codex17回目）",
              any("範囲外" in x for x in verify_official(
                  "Lすーぱぁびん娘", "https://m.example/products/slot/lbinko/",
                  "m")["problems"]))
        finally:
            _nw._get, _mc.page_is_machine = real_get, real_page
    finally:
        _di.find, _sl.read_page, _mc.lookup = real_find, real_read, real_lookup
        _pend.STORE = real_store
        globals()["_log"] = real_log
        __import__("shutil").rmtree(_tmpdir, ignore_errors=True)

    ng = [n for n, ok in results if not ok]
    print(f"{nl}{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--apply", action="store_true", help="実際に書き込む")
    ap.add_argument("--ctx", help="task_lock の CTX パス（--apply に必須）")
    ap.add_argument("--name", help="1機種だけ試す：正式名称")
    ap.add_argument("--baseline-titles", action="store_true",
                    help="既知URL全部の題を一度だけ控える（すり替え検知の基準）")
    ap.add_argument("--official-url", dest="official_url")
    ap.add_argument("--maker")
    ap.add_argument("--release", default="")
    args = ap.parse_args()
    if args.selftest:
        return selftest()

    if args.baseline_titles:
        return baseline_titles()

    if args.apply and not args.ctx:
        print("★--apply には --ctx（ロックのCTXパス）が必要です★")
        return 1
    if args.apply:
        r = subprocess.run(
            [sys.executable, os.path.join(BASE, "scripts", "task_lock.py"),
             "check", "--ctx", args.ctx], capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        if r.returncode != 0:
            print("★ロックを持っていません → 何も書かずに終了します★")
            return 1
        # ★task_guard も必ず通す★（Codex指摘4・通していなかった）
        # ★1日1機種の枠は「書く直前」に使う★（2026-07-31・Codex19回目）
        #   ここで先に使うと、--maker を書き忘れただけでその日の枠が消え、
        #   正しい別の機種を公開できなくなる。run_one の before_write に任せる。
        pass

    # ★出せていない公開を、どの経路より先に片付ける★（2026-07-31・Codex20回目）
    #   直接指定の経路がこの手前にあったので、前の機種を出せないまま
    #   次の機種を書いてコミットでき、目印まで上書きしていた。
    # ★片付け（コミット・push）は --apply の時だけ★（2026-08-02・Codex25回目）
    #   以前は下見でもここを通っていたので、目印が残っていると
    #   **見るだけの実行がロックも持たずに公開（push）していた**。
    #   下見では目印の存在を知らせるだけにする。
    if args.apply:
        for x in retry_push_first():
            print("  ✗ " + x[:200])
            _log("  ✗ " + x[:300])
            return 1                       # ★片付くまで次へ進まない★
    elif os.path.isfile(PUSH_PENDING):
        print("★出せていない公開の目印があります（下見では触りません）。"
              "--apply の実行が先に片付けます★")

    if args.name:
        if not (args.official_url and args.maker):
            print("★--name と一緒に --official-url --maker が必要です★")
            return 1
        res = run_one(args.name, args.official_url, args.maker, args.release,
                      args.apply,
                      before_write=lambda: _claim_today(args.official_url))
        # ★1機種だけ試す経路でも、公開したら最後まで通す★（Codex17回目）
        #   ここだけ push を呼んでいなかったので、手元に変更が残り、
        #   翌日の実行が「許していない変更がある」で止まっていた。
        push_ng = []
        if res.get("wrote"):
            push_ng = finish_publish(res)
            for x in push_ng:
                print("  ✗ " + x[:200])
                res.setdefault("problems", []).append(x)
        print(json.dumps({k: v for k, v in res.items() if k != "preview"},
                         ensure_ascii=False, indent=1))
        # ★出せていないなら成功にしない★（2026-07-31・Codex19回目）
        #   「手元には書いたが読者には届いていない」を成功として返していた。
        if push_ng:
            return 1
        return 0 if res.get("wrote") or not args.apply else 1

    apply_it = bool(args.apply)
    _log("★新台追加タスク 開始★" + ("（書き込みます）" if apply_it else "（下見）"))
    # ★前回が途中で終わっていないか、いちばん最初に知らせる★
    #   （2026-07-31・電源断→PC自動起動→翌日の実行、を再現して足した）
    #   見張りだけなら書き込まないので進んでよいが、
    #   **気づかないまま作業を続けてpushしてしまう**のが危ない。
    left = _pub.unfinished()
    if left:
        msg = (f"★前回の公開が途中で終わっています（{left.get('slug')} / "
               f"{left.get('started_at')}）★ "
               "`python scripts/publish_new_machine.py --recover --apply` で戻すまで、"
               "**公開もpushもしないでください**")
        _log(msg)
        print(msg)
        _ledger("site", "structural", "MATERIAL", "PUBLISH_UNFINISHED",
                "前回の公開が途中で終わっています",
                f"{left.get('slug')} / {left.get('started_at')} / "
                "--recover --apply で戻してください")
        # ★書き込む日はここで終わる★（2026-07-31・Codex19回目）
        #   そのまま進むと、公開部が途中状態を理由に断り、
        #   その理由は「やり直す価値なし」に当たるので、
        #   **公開できるはずの機種が待ち行列から捨てられていた**。
        if apply_it:
            _log("★戻すまで進みません（--recover --apply で戻してください）★")
            return 1
    d = discover(persist=apply_it)
    for x in d["first_time"]:
        print("初回として記録:", x)
    # ★見つけたが記事にできていない機種を、必ず待ち行列に入れる★
    # ★候補は discover() の中で、seen を書く前に待ち行列へ入れてある★
    pend = _pend.load()
    # ★下見は待ち行列・台帳を変えない★（2026-08-02・Codex28回目）
    #   見るだけの実行が60日打ち切り・台帳送り・試行記録を進めてしまうと、
    #   将来公開できる機種が自動経路から外れる。
    if not apply_it:
        would = [it for it in pend["items"].values()
                 if _pend.waited_days(it) >= _pend.GIVE_UP_DAYS
                 and int(it.get("runs", 0)) >= 1]
        if would:
            print(f"（下見）60日超えの待ち {len(would)} 件は --apply の実行が処理します")
    # ★待ちすぎた分は黙って消さず、台帳に残す★
    for it in (_pend.give_up(pend) if apply_it else []):
        if not _ledger("site", "structural", "MATERIAL", "PENDING_GAVE_UP",
                       f"新台を{_pend.GIVE_UP_DAYS}日待っても記事にできませんでした",
                       f"{it['name']} / {it['url']} / "
                       f"直近の理由: {it.get('last_reason', '')}"):
            # ★台帳に残せなかったら行列へ戻す★（2026-07-31・Codex20回目）
            #   give_up() は返す前に外してしまうので、そのまま保存すると
            #   **待ち行列にも台帳にも無い機種**になる。
            pend["items"][it["url"]] = it
            _log(f"  台帳に残せなかったので待ち行列に戻しました: {it['name']}")
            continue
        print(f"  ★{_pend.GIVE_UP_DAYS}日待っても記事にできませんでした: {it['name']}★")
    if apply_it:
        _pend.save(pend)                  # ★下見は古い姿を書き戻さない★（Codex30回目）
    print(f"新台候補: {len(d['candidates'])} 件 / 確認が要る: {len(d['problems'])} 件")
    # ★「新台なし」とは言わない★ 見られた社に限った話であることを必ず書く
    print(f"  正常に見られたメーカー: {len(d['watched'])} 社"
          + (f"（{', '.join(d['watched'])}）" if d["watched"] else ""))
    if d["not_watched"]:
        print(f"  ★見られていないメーカー: {', '.join(d['not_watched'])}★"
              "（この社の新台は検出できていません）")
    for c in d["candidates"]:
        print(f"  ★{c['official_name']}（{c['maker']}／{(c['release'] or {}).get('value')}）")
        print(f"    {c['url']}")
    for p in d["problems"]:
        print("  ✗ " + p[:150])
    waiting = _pend.due(pend)
    print(f"  記事にできず待っている新台: {len(waiting)} 件")
    for it in waiting[:10]:
        print(f"    {it['release']} {it['name'][:34]}"
              f"（{_pend.waited_days(it)}日待ち）{it.get('last_reason', '')[:40]}")

    # ★★ここから実際に機種を進める★★（2026-07-31・Codex16〜18回目）
    #   以前はここで終わっていたので、無人で動かしても永久に記事にならなかった。
    #   さらに1件しか見ていなかったので、その1件が詰まると後ろが全部止まっていた。
    for work in pick_work(pend):
        work = fill_missing(work)
        # ★使い回しの疑いは公開処理へ進めない★（2026-08-02・Codex41回目）
        #   検知（recheck）と公開の停止がつながっていなかった。
        if work.get("_name_conflict"):
            msg = (f"同じURLの機種名が変わりました（{work['name'][:30]} → "
                   f"{work['_name_conflict'][:30]}）")
            print("  ★止めました: " + msg)
            if apply_it:
                give_up_now(pend, work["url"], work["name"], [msg])
            else:
                print("（下見）--apply の実行が台帳へ移します")
            continue
        if not (work["name"] and work["maker"]):
            _log(f"  まだ記事にできません（名前かメーカーが取れない）: {work['url']}")
            # ★早く抜けるときも試した日を残す★（残さないと毎晩ここで詰まる）
            if apply_it:
                _pend.mark_tried(pend, work["url"])
                _pend.save(pend)
            continue
        _log(f"試す: {work['name']} / {work['maker']} / {work['release']}")
        # ★試したことを必ず残す★（残さないと同じものばかり選ばれる）
        # ★下見では残さない★（試行記録・巡回順を進めない・Codex28回目）
        if apply_it:
            _pend.mark_tried(pend, work["url"])
            _pend.save(pend)
        res = run_one(work["name"], work["url"], work["maker"],
                      work["release"], apply_it,
                      release_is_cache=True,       # ★待ち行列の年月は控え★
                      before_write=lambda u=work["url"]: _claim_today(u))
        for b in res.get("blocked") or []:
            print("  ★止めました: " + b[:150])
        if res.get("wrote"):
            ng = finish_publish(res, pend)
            for x in ng:
                print("  ✗ " + x[:200])
                _log("  ✗ " + x[:300])
            if ng:
                _ledger("site", "structural", "MATERIAL", "PUSH_BLOCKED",
                        "公開はしたがpushできませんでした",
                        f"{res['slug']} / " + " / ".join(ng)[:1200])
            d["problems"] += ng
            break                          # ★公開できたら今日はここまで★
        if any("今日の担当ではありません" in p for p in res.get("problems") or []):
            _log("  今日の担当ではありません（1日1機種）→ 今日はここまで")
            break
        # ★やり直しても変わらない理由なら、行列から出して後ろを通す★
        if res.get("blocked") and not retry_later(res["problems"]):
            if apply_it:
                give_up_now(pend, work["url"], work["name"], res["problems"])
            else:
                print("（下見）やり直しても変わらない理由です"
                      "（--apply の実行が台帳へ移します）: " + work["name"])
    if d["problems"]:
        _ledger("site", "structural", "MATERIAL", "WATCH_PROBLEM",
                "新台の見張りで確認が要る点が出ました",
                " / ".join(d["problems"])[:1500])
        _log(f"台帳に登録しました: 確認が要る{len(d['problems'])}件")
    _log(f"★新台追加タスク 終了★ 新台候補{len(d['candidates'])}件 "
         f"/ 待っている新台{len(waiting)}件 / 確認が要る{len(d['problems'])}件")
    return 1 if d["problems"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except _sj.SafeJsonError as e:
        print(f"★入力データが読めません: {e}★")
        raise SystemExit(1)
    except Exception as e:
        print(f"★想定外の失敗 {type(e).__name__}: {e}★")
        raise SystemExit(1)

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
import io
import json
import os
import re
import subprocess
import threading
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

import adoption_basis as _ab         # noqa: E402
import build_new_article as _ba       # noqa: E402
import check_duplicate as _cd        # noqa: E402
import at_spec_lookup as _at        # noqa: E402
import fetched_page as _fp           # noqa: E402
import os as _os_lp                 # noqa: E402
import sys as _sys_lp               # noqa: E402
_sys_lp.path.insert(0, _os_lp.path.dirname(_os_lp.path.abspath(__file__)))
import local_paths as _lp           # noqa: E402
import maker_identity_cache as _mic   # noqa: E402
import ceiling_lookup as _cl         # noqa: E402
import cz_lookup as _cz              # noqa: E402
import directory_index as _di         # noqa: E402
import lineage_check as _lc          # noqa: E402
import confirmed_values as _cv        # noqa: E402
import model_code_lookup as _mc       # noqa: E402
import new_machine_watch as _nw       # noqa: E402
import pending_machines as _pend      # noqa: E402
import page_decision as _pdz          # noqa: E402
import claim_identity as _ci          # noqa: E402  ★機種名の芯の照合★
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
            [sys.executable, _lp.LOG_PY,
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
    # ★★実行まで1か所に閉じ込める★★（2026-08-21・Codexの再指摘）
    #   ★オプション名を並べる場所も、別プロセスを起動する場所も1つ★
    #   ＝字面の監査に頼らず、書きようがない形にする。
    import open_issues as _oi
    ok, out = _oi.run_add(source="add-machine", slug=slug, kind=kind,
                          severity=severity, reason_code=code,
                          title=title, detail=detail)
    if not ok:
        _log(f"  ★台帳に登録できませんでした: {out[:200]}★")
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
            # ★用途を名乗ってから取りに行く★（2026-08-16・依頼218）
            with _nw.fetching("machine_identity"):
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
            # ★用途を名乗ってから取りに行く★（依頼218）
            with _nw.fetching("machine_identity"):
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


# ★入口はDMMぱちタウンだけ★（2026-08-16・台帳#376／2026-08-17に説明を更新）
#   ★「真にすると公式の見張りに戻る」という切り替えは**もうありません**★
#   仕組みごと削除済みです（2026-08-16・台帳#377）。この行が残っていたせいで
#   「まだ戻せる」と読める状態でした（Codex依頼229の指摘）。


# ★知らせ済みのメーカーを覚えておく場所★（同じ会社で毎晩鳴らさない）
#   ★ファイル名は旧入口の名残★＝中身は「名簿に無いメーカー」の控えで、
#   いまはDMMのカレンダーから来る（置き場を変えると控えが消えるので名前は据え置き）
UNKNOWN_MAKERS = _lp.doc("pworld_unknown_makers.json")


def _tell_unknown_makers(rows: list) -> None:
    """★名簿に無いメーカーをメールで知らせる★（2026-08-12・運営者の指示）

    名簿は「メーカーの表示名 → 内部の呼び名」の対応表。
    ここに無い会社の新台は、どのメーカーとして記録するか決まらないので
    記事を作れない。★推測で結ばない★（別会社の機種になる）。

    ★同じ会社では一度だけ★／★送れなくても新台の処理は止めない★
    """
    import json
    try:
        known = _sj.read_json(UNKNOWN_MAKERS, expect=dict, allow_missing=True,
                              default={"makers": {}})
    except Exception as e:                # noqa: BLE001
        _log(f"  知らせ済みの控えを読めません（全部知らせます）: {e}")
        known = {"makers": {}}
    fresh = [(m, n) for m, n in rows if m not in (known.get("makers") or {})]
    if not fresh:
        _log("  名簿に無いメーカーはすべて連絡済みです")
        return
    lines = ["DMMぱちタウンの導入カレンダーに、名簿に無いメーカーの新台が出ています。",
             "このままだと記事を作れません（どのメーカーとして記録するか決まらないため）。",
             "",
             "★名簿に足してください★",
             "  ファイル: assets/data/maker-catalogs.json",
             "  書き方  : \"<内部の呼び名>\": {\"name\": \"<表示名>\", "
             "\"status\": \"WATCH_OFF\"}",
             "  ★推測で既存の会社に結び付けないこと★（別会社の機種になります）",
             ""]
    for m, n in fresh:
        lines.append(f"  ・{m}    （例: {n}）")
    lines += ["", "足したあとは、翌晩の新台タスクが自動で拾います。"]
    ops = _lp.OPS
    try:
        os.makedirs(ops, exist_ok=True)
        sub = os.path.join(ops, "unknown_maker_subject.txt")
        body = os.path.join(ops, "unknown_maker_body.txt")
        with open(sub, "w", encoding="utf-8") as f:
            f.write("🟡 うちどころ: 名簿に無いメーカーの新台があります（%d社）"
                    % len(fresh))
        with open(body, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        # ★★時間制限を必ず付ける★★（2026-08-25・Codexの25回目）
        #   ★直す前は制限が無かった★＝メール送信が固まると
        #   **例外にも戻り値にもならず**、終了の記録も残らない。
        #   生存信号だけ動き続けてロックが延び、朝の更新タスクまで止まる。
        try:
            r = subprocess.run(
                [sys.executable, _lp.NOTIFY, "notify",
                 "--subject-file", sub, "--body-file", body],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=NET_TIMEOUT,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        except subprocess.TimeoutExpired:
            _log(f"  お知らせを送れませんでした（{NET_TIMEOUT}秒で打ち切り）")
            return
        if r.returncode != 0:
            # ★送れなくても止めない★（次の晩にまた知らせる＝控えを更新しない）
            _log(f"  ★メールを送れませんでした★: {(r.stderr or r.stdout)[:200]}")
            return
    except Exception as e:                # noqa: BLE001
        _log(f"  ★メールを送れませんでした★: {type(e).__name__}: {e}")
        return
    _log("  名簿に無いメーカーを知らせました: "
         + "／".join(m for m, _ in fresh))
    # ★送れてから控える★（送信前に控えると、失敗した会社を二度と知らせない）
    for m, n in fresh:
        import datetime
        known.setdefault("makers", {})[m] = {
            "first_seen": datetime.date.today().isoformat(), "example": n}
    try:
        tmp = UNKNOWN_MAKERS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(known, f, ensure_ascii=False, indent=2)
        os.replace(tmp, UNKNOWN_MAKERS)
    except Exception as e:                # noqa: BLE001
        _log(f"  知らせ済みの控えを書けません（次回また知らせます）: {e}")


def discover_calendar(persist: bool = True) -> dict:
    """★DMMぱちタウンの導入カレンダーから新台候補を出す★（2026-08-16・台帳#376）

    ★P-WORLDから移した★
      P-WORLDの利用規約がプログラムからのアクセスとデータ収集を禁じていた。
      通信そのものは blocked_hosts.py が止める（最後の砦）。

    ★discover() と同じ形を返す★（呼ぶ側を変えないため）。
    ★persist=False（下見）は何も書かない★＝待ち行列にも触らない。
    """
    import dmm_discover as _dd
    out = {"candidates": [], "problems": [], "first_time": [],
           "watched": [], "not_watched": []}
    try:
        got = _dd.run(apply_it=persist)
    except Exception as e:                # noqa: BLE001
        # ★読めなかったことを「新台なし」にしない★
        out["problems"].append(
            f"DMMのカレンダーを読めません: {type(e).__name__}: {e}")
        out["not_watched"].append("dmm-ptown")
        return out
    out["problems"] += got.get("problems") or []
    if got.get("problems"):
        out["not_watched"].append("dmm-ptown")
    else:
        out["watched"].append("dmm-ptown")
    for r in got.get("rebound") or []:
        # ★DMMに遅れて載った機種を、待っていた控えへ結び直した★
        _log(f"  待っていた控えを結び直しました: {r['queue_id']} {r['name']}")
    for q in got.get("queued") or []:
        # ★discover() と同じ形で入れる★（呼ぶ側が official_name / release を読む）
        out["candidates"].append({
            "maker": q["maker"], "url": q["url"],
            "official_name": q["name"],
            "release": {"value": q["release"]}})
        out["first_time"].append(f"{q['name']} / {q['url']}")
        _log(f"  カレンダーから: {q['name']}（{q['maker']}・{q['release']}）")
    unknown = []
    for h in got.get("held") or []:
        _log(f"  待たせます: {h['name']} ← {h['reason'][:120]}")
        # ★名簿に無いメーカーは、その都度知らせる★（2026-08-12・運営者の指示）
        if h.get("maker") and "名簿にありません" in (h.get("reason") or ""):
            unknown.append((h["maker"], h["name"]))
    if unknown and persist:
        _tell_unknown_makers(unknown)
    _log(f"DMMのカレンダー: 候補{got.get('looked', 0)}件 / "
         f"待ち行列へ{len(out['candidates'])}件 / "
         f"待たせた{len(got.get('held') or [])}件 / "
         f"結び直し{len(got.get('rebound') or [])}件")
    return out



def write_maker_relation_record(slug: str, name: str, checks: list,
                                base: str = "") -> str:
    """★控えで通した材料を、判断記録として残す★（2026-08-17・Codex依頼228の指摘7）

    ★なぜ要るか★＝いままで採否の事実は実行の戻り値とログにしか無く、
      「この記事のこの材料が、例外の許可で入った」という対応が後から追えなかった
      （ログは日ごとに流れる）。非公開の判断記録に残しておけば、あとで
      「なぜこの名鑑を使ったのか」をたどれる。

    ★公開物ではない★＝置き場は Documents/uchidokoro/decisions/（リポジトリ外）。
    ★書けなくても処理は止めない★（記録は大事だが、記事の公開を止める理由にはしない）
    """
    if not checks:
        return ""
    import datetime
    day = datetime.date.today().isoformat()
    root = base or os.path.join(_lp.DOCS, "decisions")
    path = os.path.join(root, f"{slug}_{day}_maker.md")
    lines = [
        f"# {name}（{slug}）メーカー欄の採否 — {day}",
        "",
        "★ここに書いてあるのは「会社が同じか」ではありません★",
        "決めたのは「この名鑑ページを、この機種の材料に使うか」だけです。",
        "会社どうしの関係は機械で確かめていません（relationship_verified: false）。",
        "記事のメーカー表記は DMM の値をそのまま使います。",
        "",
    ]
    for c in checks:
        lines += [
            f"## {c.get('url', '')}",
            f"- DMMのメーカー: {c.get('expected', '')}",
            f"- 名鑑のメーカー欄: {c.get('seen', '')}",
            f"- 名簿が指した社: {'/'.join(c.get('owners') or []) or '（なし）'}",
            f"- 票のかたまり: {c.get('vote_key') or '（取れませんでした）'}",
            f"- 突き合わせたDMMの機種: {c.get('machine_name', '')}"
            f"（導入日 {c.get('release_date', '')}）",
            f"- 結論: {c.get('verdict', '')}",
            f"- 根拠の範囲: {c.get('basis_scope', '')}",
            f"- 会社関係の機械確認: {c.get('relationship_verified')}",
            # ★名前を実態に合わせた★（2026-08-17・Codex依頼230）
            #   「材料に使った」ではなく「材料集めの最後まで残った」。
            #   そこから値が採用されたかは、また別のこと。
            f"- 材料集めの最後まで残った: "
            f"{c.get('eligible_at_collection_end')}",
            f"- 記事を作れた: {c.get('article_created')}",
            f"- 型式名の票に入れた: {c.get('model_code_vote_used')}",
            "",
        ]
    try:
        os.makedirs(root, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return path
    except Exception:                      # noqa: BLE001
        return ""


def _host_of_url(url: str) -> str:
    import urllib.parse
    return (urllib.parse.urlsplit(str(url or "")).hostname or "").lower()


def mc_expected(r: dict, maker: str) -> str:
    """そのページに期待している社（読めていなければ引数のメーカー）。"""
    return (r.get("maker_check") or {}).get("expected") or maker


def maker_material_decision(looks, slug, maker, cache=None, cache_ok=True,
                            verdict_of=None, machine_name="",
                            release_date="", pages=None) -> dict:
    """★メーカー欄を見て、どの名鑑ページを材料に使うか決める★

    ★ここを関数にした理由★（2026-08-17・Codex依頼228の指摘1と、その原因）
      判定は gather() の中に埋まっていて、試験は**本体の式を文字列で取り出して
      動かして**いた。すると「前段のループで `_unknown_ok` に何が入るか」を
      通らないので、★UNKNOWN が控えで救われる穴（5-A）を検知できなかった★。
      判定そのものをここへ出し、試験も本番もこの関数を通す。

    ★通信しない・記録しない・ログも出さない★＝決めるだけ。

    判定表（依頼225で決め、依頼228で UNKNOWN を締めたもの）:
      MATCH    名簿で一致          … ここへは来ない（そのまま使う）
      RELATED  関係のある社        … ★控えで ACCEPT_MATERIAL の時だけ使う★
      UNKNOWN  どの社か分からない  … ★控えでも救わない★
      MISMATCH 明らかに別の社      … 使わない

    返すもの:
      accepted   … 控えで「使う」と決まっているURL
      bad        … 材料からも票からも外すURL
      questions  … 2AIへの問い
      relation_checks … 控えで通したページの記録（判断記録に残す）
    """
    if verdict_of is None:
        def verdict_of(expected, seen, url, profile="maker_field"):
            if not (cache_ok and slug):
                return None
            # ★対象そのものを渡す★（2026-08-17・Codex依頼229の指摘1）
            #   いま決めようとしているページのURLと、DMMで確かめた
            #   機種名・導入日を渡さないと「使う」は返らない（fail-closed）。
            # ★求めている証明の型も渡す★（台帳#390）＝題の不一致で作った控えを
            #   メーカーの食い違いに流用させない。
            # ★確かめる本文＝あとで読取器が読む本文★（台帳#393）
            return _mic.verdict_for(slug, expected, seen, cache,
                                    material_url=url,
                                    machine_name=machine_name,
                                    release_date=release_date,
                                    want_profile=profile,
                                    runtime_page=(pages or {}).get(url))
    accepted, rejected, questions, notes = set(), set(), [], []
    # ★★控えを読めないなら、どのページも使わない★★
    #   （2026-08-17・Codex依頼232の指摘）
    #   ★穴だったところ★＝控えが読めないと `verdict_of` が常に「答えなし」を
    #   返すので、**「使わない」と決めてあるページかどうかも分からない**まま
    #   `MATCH` のページが材料にも型式名の票にも残っていた。
    #   コメントには「控えが読めないときは除外する（fail-closed）」と
    #   書いてありながら、実装は逆だった。
    #   ★読めない＝安全とは言えない★ので、その晩はこの機種を止める。
    if not cache_ok:
        return {"accepted": set(),
                "rejected_by_cache": {r["url"] for r in looks or []},
                "bad": {r["url"] for r in looks or []},
                "questions": [], "relation_checks": [],
                "cache_unreadable": True}
    for r in looks or []:
        mc = r.get("maker_check") or {}
        if not r.get("identity_ok"):
            continue
        st = mc.get("state")
        # ★★「使わない」と決めた控えは、状態によらず必ず効かせる★★
        #   （2026-08-17・Codex依頼230の指摘1）
        #   前は RELATED のときしか控えを見ていなかったので、
        #   **名簿を直して同じ表記が MATCH になった瞬間に、
        #   「使わない」と決めたページが材料へ戻れた**。
        #   ★束縛（URL・機種名・導入日）を渡さずに引く★＝
        #   「使う」は返らない（fail-closed）ので、通信もしない。
        if st != "RELATED":
            # ★「使わない」は対象ページで引く★（2026-08-17・台帳#390）
            #   v2までは表記だけで引いていたので、対象を渡さないと
            #   引けなかった。いまは鍵が (機種・対象ページ) なので必ず渡す。
            if verdict_of(mc.get("expected") or maker, mc.get("seen") or "",
                          r.get("url") or "") == "REJECT_MATERIAL":
                rejected.add(r["url"])
            continue
        # ★控えを「使う」側で引くのは RELATED だけ★（依頼228の指摘1）
        #   UNKNOWN は「メーカー欄を読めない」か「名簿に無い**任意の別会社**」。
        #   救うと、2つの名鑑が同じ表記をしただけで別会社の機種を材料に戻せる。
        #   ★同名で別メーカーの機種は実在する★
        #   （パチスロ犬夜叉＝2016年ロデオ／2022年クロスアルファ）。
        v = verdict_of(mc.get("expected") or maker, mc.get("seen") or "",
                       r.get("url") or "")
        if v == "ACCEPT_MATERIAL":
            accepted.add(r["url"])
            # ★どの発行者の票として入ったかも残す★（Codex依頼229）
            #   ★記録が取れなくても採否は変えない★（記録は監査のためのもの）
            try:
                import source_lineage as _sl2
                _vk = _sl2.vote_key_of_url(r["url"])
            except Exception:              # noqa: BLE001
                _vk = ""
            notes.append(
                {"url": r["url"], "expected": mc.get("expected", ""),
                 "seen": mc.get("seen", ""), "owners": mc.get("owners", []),
                 "vote_key": _vk,
                 "machine_name": machine_name, "release_date": release_date,
                 # ★決めたのは会社の同一性ではない★（依頼228の指摘5）
                 "verdict": "ACCEPT_MATERIAL",
                 "relationship_verified": False,
                 "basis_scope": _mic.BASIS_SCOPE,
                 # ★実際の結果は出口で入れ直す★（決めた時点では分からない）
                 "eligible_at_collection_end": None,
                 "article_created": None,
                 "model_code_vote_used": False})
        elif v == "REJECT_MATERIAL":
            # ★決めてある＝必ず除外★（2026-08-17・依頼225のCodex指摘1）
            #   前は何も記録せず素通りしていたので、採否を変えた瞬間に
            #   **控えで「使わない」と決めたページが材料に復活**した。
            rejected.add(r["url"])
        elif slug:
            questions.append({
                # ★v3は「1ページにつき1判断」★（Codex依頼234の指摘5）
                #   キーに対象ページを入れないと、同じ表記の2ページが
                #   台帳で同じ案件に合流し、片方の判断が見えなくなる。
                "key": (f"maker:{mc.get('expected')}:"
                        f"{_mic.key_of(mc.get('seen'))}:{r['url']}"),
                "text": (
                    f"★この名鑑ページを、この機種の材料に使ってよいですか★／"
                    f"名鑑 {r['url']} のメーカー欄が「{mc.get('seen')}」で、"
                    f"DMMは「{mc.get('expected')}」です。"
                    "★決めるのは会社が同じかではなく、このページを使うかです★／"
                    "そのページが本当にこの機種のページか（機種名・導入日）を"
                    "2AIで確かめ、決めたら "
                    "python scripts/maker_identity_cache.py --record "
                    "--machine-url https://p-town.dmm.com/machines/<機種ID> "
                    "--machine-name <カレンダーの機種名> "
                    f"--target-url {r['url']} "
                    "--proof-profile maker_field "
                    f"--expected {mc.get('expected')} "
                    f"--seen {mc.get('seen')} "
                    "--verdict ACCEPT_MATERIAL/REJECT_MATERIAL "
                    "--why-file <理由を書いたファイル> --by claude,codex "
                    "--evidence \"<名鑑①の機種ページURL>|<機種名とメーカー欄を"
                    "含む逐語引用>|directory_observation\" "
                    "--evidence \"<名鑑②の機種ページURL>|<同上>"
                    "|directory_observation\" "
                    "で控えてください（★この機種にだけ効きます／"
                    "使うと決めるには独立した名鑑が2つ要ります★）"),
            })
    # ★★題が略称で同定に落ちたページを、2AIへ回す★★
    #   （2026-08-17・台帳#390／Codex依頼233）
    #   実例＝なな徹は題が「【ガンゲイルオンライン(スマスロ)】…」で、本文には
    #   正式名がある。★機械が「本文に正式名があるから本人だ」と決めてはいけない★
    #   （それが二段目の意味判断）。機械は**候補として出す**までにする。
    #   ★足切り（Codexの③）★＝この4つを満たすものだけ2AIへ回す:
    #     ①その名鑑の機種ページの形に一致 ②投稿欄を落とせた
    #     ③落ちた理由が厳密に NAME_CORE_MISMATCH ④除去後の本文にDMMの
    #       正式名が完全一致で存在
    #   ★④が真でも identity_ok にはしない★（返せるのは候補だけ）
    # ★★救える落ち方は2つ★★（2026-08-26）
    #   ・NAME_CORE_MISMATCH … 題が略称
    #   ・TAIL_CONFLICT     … 題の後ろの飾りを分解できない
    #     （実測＝索引が正しく当てた14ページ中3件＝ちょんぼりすたの25%）
    #   ★落ち方ごとに控えの型を分ける★（どちらでも何でも救える、にしない）
    _RESCUE_PROFILE = {"NAME_CORE_MISMATCH": "title_name_core_mismatch",
                       "TAIL_CONFLICT": "title_tail_conflict"}
    for r in looks or []:
        _prof_want = _RESCUE_PROFILE.get(str(r.get("reason") or ""))
        if r.get("identity_ok") or not _prof_want:
            continue
        if not slug or not machine_name:
            continue
        # ★足切り★＝投稿欄を落とした本文にDMMの正式名が完全一致であること
        #   （lookup が印を付ける。無ければ2AIへ回さない）
        if not r.get("name_in_body"):
            continue
        # ★その名鑑の機種ページの形に一致していること★
        try:
            _conf = _mic.directory_of(_host_of_url(r.get("url") or ""))
            _pat = str(_conf.get("machine_page_pattern") or "")
            if not _pat or not __import__("re").match(_pat, r.get("url") or ""):
                continue
        except Exception:                  # noqa: BLE001
            continue                       # 名鑑を引けないものは回さない
        # ★題の不一致では maker_check が作られない★（同定で先に戻るため）。
        #   メーカー欄は「見えた事実」として lookup が返す observed_maker を使う。
        #   （2026-08-17・Codex依頼234の指摘2。ここが空だと控えを永久に引けない）
        _seen = str(r.get("observed_maker") or "")
        if not _seen:
            continue                       # メーカー欄が読めないものは回さない
        v = verdict_of(mc_expected(r, maker), _seen, r.get("url") or "",
                       _prof_want)
        if v == "ACCEPT_MATERIAL":
            accepted.add(r["url"])
            notes.append({"url": r["url"], "expected": mc_expected(r, maker),
                          # ★この経路に maker_check は無い★（依頼235の指摘2）
                          #   題の不一致では作られないので、観測した
                          #   メーカー欄をそのまま記録に残す（空にしない）。
                          "seen": _seen,
                          "owners": [], "vote_key": "",
                          "machine_name": machine_name,
                          "release_date": release_date,
                          "verdict": "ACCEPT_MATERIAL",
                          "proof_profile": _prof_want,
                          "relationship_verified": False,
                          "basis_scope": _mic.BASIS_SCOPE,
                          "eligible_at_collection_end": None,
                          "article_created": None,
                          "model_code_vote_used": False})
        elif v != "REJECT_MATERIAL":
            questions.append({
                "key": f"title:{slug}:{r['url']}",
                "text": (
                    f"★この名鑑ページを、この機種の材料に使ってよいですか★／"
                    f"{r['url']} は"
                    + ("題が略称で" if _prof_want == "title_name_core_mismatch"
                       else "題の後ろの飾り語を機械が分解できず")
                    + "、機械の同定に落ちました"
                    + f"（{r.get('reason')}）。DMMの正式名は「{machine_name}」"
                    f"（導入日 {release_date}）です。"
                    "★機械は『本文に正式名があるから本人だ』とは決めません★＝"
                    "そこは2AIが本文と欄の関係を読んで判断してください。"
                    "決めたら python scripts/maker_identity_cache.py --record "
                    "--machine-url https://p-town.dmm.com/machines/<機種ID> "
                    f"--machine-name \"{machine_name}\" "
                    f"--target-url {r['url']} "
                    f"--proof-profile {_prof_want} "
                    f"--expected {mc_expected(r, maker)} "
                    f"--seen \"{_seen}\" "
                    "--verdict ACCEPT_MATERIAL/REJECT_MATERIAL "
                    "--why-file <理由を書いたファイル> --by claude,codex "
                    "--evidence \"<そのページのURL>|<機種名とメーカー欄と導入日を"
                    "含む逐語引用>|directory_observation\" "
                    "で控えてください（★メーカー欄がDMMと一致していることが"
                    "必要です★）"),
            })
    # ★★材料から外すもの★★
    #   ①MISMATCH ②UNKNOWN（★控えでも救わない★）
    #   ③RELATED（控えで「使う」と決めた時だけ残す）
    #   ④控えで「使わない」と決めたページ ⑤同定に落ちたページ
    #   ★残せるのは accepted に入ったものだけ★
    #
    #   ★★判定は「状態」で見る。理由の文でなく★★
    #     （2026-08-17・Codex依頼229の厚みの指摘）
    #     前は reason の文字列の頭で見ていたので、
    #     `state` が UNKNOWN のままでも**理由文を書き換えるだけで除外を
    #     すり抜けられた**（隣り合う契約が静かにずれる形）。
    #     状態を正本にすれば、文言を直しても採否は変わらない。
    #   ★状態が読めないページも外す★（fail-closed・2026-08-17・依頼230）
    #     ★ただし、メーカーを期待していない呼び方のときは、この関門自体が無い★
    #     （`maker` が空＝照合する相手がいない。lookup も maker_check を作らない）
    _USE_STATES = ("MATCH",)          # そのまま材料に使ってよい状態
    bad = set()
    for r in looks or []:
        if r["url"] in accepted:
            continue
        st = (r.get("maker_check") or {}).get("state")
        if not r.get("identity_ok") or r["url"] in rejected:
            bad.add(r["url"])
        elif maker and st not in _USE_STATES:
            bad.add(r["url"])
    return {"accepted": accepted, "rejected_by_cache": rejected,
            "bad": bad, "questions": questions, "relation_checks": notes,
            "cache_unreadable": False}


def gather(*a, **k):
    """★何のために取りに行くかを名乗ってから中身を動かす★

    （2026-08-16・依頼219の指摘1）
    前は共有の値へ直接入れていたので、**抜けたあとも残って**いた。
    残ると、そのあとの「名乗っていない取得」が材料として通ってしまい、
    関所の意味が崩れる。★囲みにして必ず元へ戻す★
    """
    import new_machine_watch as _nwp
    with _nwp.fetching("claim_material"):
        return _gather(*a, **k)


def _gather(name: str, maker: str = "", slug: str = "",
            machine_name: str = "", release_date: str = "") -> dict:
    """1機種ぶんの材料を集める。★止まった理由も返す★

    ★machine_name / release_date は「DMMの機種ページで確かめた値」★
      （2026-08-17・Codex依頼229の指摘1）メーカー欄の控えを引くときに、
      控えが名乗る機種がこの機種と同じかを突き合わせるために使う。
      渡されなければ控えは効かない（fail-closed）。
    """
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
        # ★★DMM単独の例外★★（2026-08-23・運営者決定）
        #   「新台公開1週間前でもDMMしかない状態なら、DMMのだけを正として
        #     記事にしていいよ」
        #   ★ここを通さないと、採否の判定（adoption_basis）まで到達しない★
        #   ＝実測でこれに気づいた。抽出器へ配線しただけでは、
        #     材料集めの入口で早期returnしていて一度も呼ばれなかった。
        #   ★通してよいのは「1件だけ・それがDMM・導入が近い」ときだけ★。
        #   値ごとの採否はこのあと adoption_basis が6条件で判断する
        #   （ここは入口を開けるだけで、採用を決めてはいない）。
        #   ★控えに別の出典があるなら、それは「DMMしかない」ではない★
        #     （2026-08-23・Codexの敵対的レビューP0。索引は1ページしか
        #       読めない名鑑があるので、記事があるのに出ないことが実際にある）
        _other_here, _other_why_here = _ab.other_sources_known(slug, got["urls"])
        _solo_ok = (len(got["urls"]) == 1
                    and bool(_dmm_machine_id(got["urls"][0]))
                    and bool(machine_name) and bool(release_date)
                    and _ab.near_release(str(release_date))
                    and not _other_here)
        if not _solo_ok:
            got["problems"] += unused_msgs    # ★なぜ足りないかも残す★
            got["problems"].append(
                f"名鑑の個別ページが {len(got['urls'])} 件しか見つかりません（2件以上が要る）")
            if _other_here:
                # ★なぜ例外を使わなかったかを残す★（黙って落とさない）
                got["problems"].append(
                    "DMM単独の例外は使いませんでした: " + _other_why_here[:120])
            return got
        # ★1件で進む理由は必ず残す★（黙って例外を通さない）
        got["problems"] += unused_msgs
        got["single_source_exception"] = True
        _log("  ★DMM単独の例外で材料集めを続けます★"
             f"（導入{release_date}・7日前以降／運営者決定 2026-08-23）")
    # ★★DMMの機種ページは、DMM自身の決まりで確かめる★★（2026-08-22・台帳#453）
    #   ★なぜ分けるか（Codexの設計レビュー）★
    #     DMMの機種ページには**専用の同定経路がすでにある**
    #     （機種ID・canonical・転送先・機種名・種別・メーカー・導入年月）。
    #     そこを通ったページに、さらに汎用のSEO題検査を重ねると、
    #     ★DMMが題に何を書くかという、こちらに関係のない事情で落ちる★。
    #     実際 pw_10510（スマスロ タコスロ）は、題の後ろの「ボーナストリガー」を
    #     飾りとして分解できないだけで材料からも票からも外れ、5日間止まった。
    #
    #   ★「DMMのページなら無条件で通す」ではない★＝
    #     ここで束（機種ID・機種名・メーカーの表示名・導入日）を渡し、
    #     材料として取ってきた**その本文**に対して同じ束を確かめ直す。
    #
    #   ★確かめ済みの値が無いときは束を渡さない★（fail-closed）＝
    #     machine_name / release_date は「DMMの機種ページで確かめた値」で、
    #     渡されていなければ今までどおり汎用の題検査を通す。
    _maker_names = []
    if maker:
        try:
            import dmm_discover as _dd_names
            _c = (_sj.read_json(_dd_names.MAKER_CATALOG,
                                expect=dict)["catalogs"].get(maker) or {})
            _maker_names = [str(x) for x in
                            ([_c.get("name")] + list(_c.get("directory_names") or []))
                            if x]
        except Exception:                 # noqa: BLE001
            _maker_names = []             # 読めない＝束を弱めない（下で使わない）

    def _ident_for(u: str):
        mid = _dmm_machine_id(u)
        if not mid or not machine_name or not release_date:
            return None                   # ★確かめた値が無ければ渡さない★
        if not _maker_names:
            return None                   # ★メーカーを縛れないなら渡さない★
        return {"machine_id": mid, "name": machine_name,
                "maker_names": _maker_names, "release": release_date}

    # ★★DMM単独で採ってよいかの文脈★★（2026-08-23・運営者決定）
    #   「新台公開1週間前でもDMMしかない状態なら、DMMのだけを正として
    #     記事にしていいよ」
    #   ★渡せる条件は _ident_for と同じ★＝DMMの機種ページで
    #   機種名・メーカー・導入日を確かめられているときだけ。
    #   ★確かめていなければ空で渡す★＝空なら今までどおり独立2票のみ採用
    #   （adoption_basis 側が fail-closed で落とす）。
    #   ★導入日が月までしか分からないときも例外は効かない★
    #   （near_release が日精度を要求する）。
    _adopt_ctx = {}
    if machine_name and release_date and _maker_names:
        # ★★「DMM単独」と名乗る前に、控えを確かめる★★
        #   （2026-08-23・Codexの敵対的レビューP0）
        #   索引は1ページしか読めない名鑑があるので、
        #   ★記事があるのに索引に出ない★ことが実際に起きる（台帳#468）。
        #   控えに別の発行者の出典があるなら、それは「DMM単独」ではない。
        #   ★読めないときは例外を通さない★（fail-closed）
        _other, _other_why = _ab.other_sources_known(slug, got["urls"])
        if _other:
            _log(f"  ★DMM単独の例外は使いません★: {_other_why[:120]}")
        _adopt_ctx = {"release_date": str(release_date),
                      # ★この導入日はDMMの機種ページで確かめたもの★
                      "release_source": "dmm-ptown",
                      "identity_verified": True,
                      "other_sources_known": bool(_other),
                      "other_sources_why": _other_why}

    # ★名鑑にも期待するメーカーを渡す★（2026-08-02・Codex40回目）
    looks = [_mc.lookup(u, name, expected_maker=maker,
                        dmm_identity=_ident_for(u)) for u in got["urls"]]
    # ★★約束が守られているかを、その場で確かめる★★
    #   （2026-08-17・Codex依頼230の厚みの指摘）
    #   メーカーを期待して引いたなら、判定（state）が必ず返るのが約束。
    #   返っていないものを「判定なし＝素通り」にすると、隣の契約が変わった
    #   ときに**メーカーの関門を静かに抜ける**。読めなかった扱い（UNKNOWN）に
    #   倒しておく＝使わない側（fail-closed）。
    if maker:
        for r in looks:
            if not (r.get("maker_check") or {}).get("state"):
                r["maker_check"] = {"state": "UNKNOWN", "seen": "",
                                    "expected": maker, "owners": []}
    # ★メーカー違いと判明した名鑑は、材料・転載照合からも外す★
    #   （2026-08-02・Codex41回目。型式の票からしか外していなかったので、
    #     同名の別メーカー機のページが材料の2票に復活できた）
    # ★メーカー欄を名簿で解決できない名鑑も、票・材料とも不採用★
    #   （2026-08-02・Codex51回目。同名別会社機を異なる2名鑑が載せると
    #     誤った型式・スペックを2票一致として公開できてしまう。
    #     実在の別名は directory_names に足せば通る＝待ち行列側の失敗にとどまる）
    # ★同定に落ちたページも材料から外す★（2026-08-02・Codex56回目。
    #   他社名の題（GEN_MARK_CONFLICT）等で型式照合に落ちたページが、
    #   理由の文字列が DIRECTORY_MAKER_* でないため材料収集に復活していた。
    #   本人と確かめられていないページは票にも材料にもしない）
    # ★★関係のありそうなメーカー欄だけ、機種ごとの控えを見る★★
    #   （2026-08-14・依頼189／2026-08-17・依頼228で範囲を絞った）
    #   MATCH    … 名簿で一致。そのまま使う（ここへは来ない）
    #   RELATED  … 関係のありそうな社。★この控えを見る★
    #   UNKNOWN  … どの社か分からない。★控えでも救わない★
    #   MISMATCH … 明らかに別の社。使わない
    #   ★なぜ UNKNOWN を救ってはいけないか★（2026-08-17・Codex依頼228の指摘1）
    #     UNKNOWN は「メーカー欄を読めない」か「名簿に無い**任意の別会社**」。
    #     救うと、2つの名鑑が同じ表記をしただけで別会社の機種を材料に戻せる。
    #     ★同名で別メーカーの機種は実在する★
    #     （パチスロ犬夜叉＝2016年ロデオ／2022年クロスアルファ）。
    #     依頼225で決めた判定表もこれと同じ（UNKNOWN＝使わない）。
    #   ★控えが読めないときは、今までどおり除外する★（fail-closed）
    # ★★材料に使う候補を、ここで1回だけ取る★★（2026-08-17・台帳#393）
    #   ★以前は「材料集め」「控えの再確認」「4つの読取器」が
    #   それぞれ取り直していた★ので、確かめた本文と読む本文が同じである
    #   保証が無く、同じ型の穴が5回続いた。
    #   ここで取った器を、控えの再確認と読取器の両方へ渡す。
    #   ★取れなかったページは材料にしない★（fail-closed）
    _pages, _drop = {}, set()
    for _u in list(got["urls"]):
        try:
            _pg = _fp.fetch(_u, "claim_material")
        except _fp.PageError as e:        # noqa: BLE001
            _log(f"  （取れないので材料から外します）{_u} → {str(e)[:90]}")
            got["problems"].append(f"材料のページを取れません: {str(e)[:100]}")
            _drop.add(_u)
            continue
        # ★★取りに行った先と着いた先が違うページは使わない★★
        #   （2026-08-17・Codex依頼238のP1）
        #   ★控えで救う側だけ転送を見ていた★＝厳格な同定に通る「普通の
        #   ページ」は素通りしていたので、別ページへ転送されていても
        #   その本文が同じ機種に見えれば公開まで到達し得た。
        #   ★ここで一括して見る★＝例外側と通常側で扱いを分けない。
        if _mic.url_key(_pg.requested_url) != _mic.url_key(_pg.final_url):
            _log(f"  （別のページへ転送されるので材料から外します）"
                 f"{_u} → {_pg.final_url}")
            got["problems"].append(
                f"材料のページが転送されます（{_u} → {_pg.final_url}）")
            _drop.add(_u)
            continue
        _pages[_u] = _pg
    # ★★外すときは、材料の一覧と票の両方から外す★★
    #   （取れなかったURLを残すと、読取器が自分で取り直してしまう）
    if _drop:
        got["urls"] = [u for u in got["urls"] if u not in _drop]
        looks = [r for r in looks if r.get("url") not in _drop]
        if len(got["urls"]) < 2:
            got["problems"].append(
                f"名鑑の個別ページが {len(got['urls'])} 件しか残りません"
                "（取れない・転送されるページを除いた結果）")
            return got
    _cache_ok, _cache = True, None
    try:
        _cache = _mic.load()
    except Exception as e:                # noqa: BLE001
        _cache_ok = False
        _log(f"  ★メーカーの控えを読めません（この機種は今夜は止めます）★: {e}")
        got["problems"].append(
            f"メーカー照合の控えを読めません（{str(e)[:100]}）"
            "／★読めない＝「使わないと決めたページ」があるかも分からない"
            "ので、材料を使いません★")
    # ★判定は maker_material_decision に集めてある★（試験もそこを通す）
    _dec = maker_material_decision(looks, slug, maker, _cache, _cache_ok,
                                   machine_name=machine_name or name,
                                   release_date=release_date,
                                   pages=_pages)
    # ★控えで「使う」と決めたページの許可証★（2026-08-17・台帳#390）
    #   材料を読む部品も**それぞれ**同定をやり直すので、ここで通しただけでは
    #   値を読む段階でまた落ちる。同じ許可を全部の読取器へ渡す。
    # ★★許可証は「本文の指紋」で出す★★（2026-08-17・台帳#393）
    #   URLで出していたので、書き方の違い（末尾の / 等）や転送のたびに
    #   結び直しが必要になり、同じ型の穴が5回続いた。
    #   指紋なら「確かめた本文そのもの」以外は通らない。
    _grant = frozenset(
        _pages[u].sha256 for u in _dec["accepted"] if u in _pages)
    got["maker_questions"] = _dec["questions"]
    _bad_maker = _dec["bad"]
    for _n in _dec["relation_checks"]:
        # ★「同じ会社」とは言わない★（2026-08-17・Codex依頼228の指摘5）
        #   決めたのは会社の同一性ではなく、この名鑑ページを材料に使うこと。
        _log(f"  （メーカー欄はDMMと違うが、控えでこのページを材料に使うと"
             f"決めてある）{_n['url']} → {_n['seen']} ⇔ {_n['expected']}")
        got.setdefault("maker_relation_checks", []).append(_n)
    if _bad_maker:
        _bad_msgs = []
        for r in looks:
            if r["url"] in _bad_maker:
                _log(f"  （同定・メーカー欄の照合により票・材料からも除外）"
                     f"{r['url']} → {r['reason']}")
                _bad_msgs.append(str(r["reason"]))
        got["urls"] = [u for u in got["urls"] if u not in _bad_maker]
        # ★★票のほうからも同時に外す★★（2026-08-17・Codex依頼231の指摘1）
        #   ★消し漏らしていたところ★＝材料の一覧（urls）だけ削って、
        #   型式名と登場年月を数える `looks` を削っていなかった。そのため
        #   「控えで使わないと決めたページ」の型式名が独立票に残り、
        #   採用されると `regulatory_model_code` として**公開物に出る**経路が
        #   あった。★材料と票は必ず同時に外す★
        looks = [r for r in looks if r["url"] not in _bad_maker]
        if len(got["urls"]) < 2:
            got["problems"] += unused_msgs
            got["problems"] += _bad_msgs      # ★なぜ除外したかを行列・台帳に残す★
            got["problems"].append(
                f"名鑑の個別ページが {len(got['urls'])} 件しか見つかりません"
                "（2件以上が要る・メーカー欄の照合で除いた結果）")
            return got
    # ★出典どうしが転載でないか確かめる★（2026-07-31・実際に見つけた）
    #   やんちゃプレスはちょんぼりすたと本文が17行そのまま同じだった。
    #   登録簿に無い転載を2票に数えると、独立2出典の意味が無くなる。
    lin = _lc.check(got["urls"])
    # ★照合できなかったページは、そのページだけを票・材料から外す★
    #   （2026-08-02・Codex53回目。31回目は全体をBLOCKINGにしていたため、
    #     3件目の名鑑が一時的に落ちただけで、独立を確かめ終えた正常な
    #     2票まで公開不能になった。独立か不明なページを票に入れない、
    #     という31回目の目的は「外す」ことでそのまま守られる）
    _lin_failed = set(lin.get("failed") or [])
    if _lin_failed:
        for u in sorted(_lin_failed):
            _log(f"  （転載照合で取得できず・票と材料から除外）{u}")
        got["urls"] = [u for u in got["urls"] if u not in _lin_failed]
        looks = [r for r in looks if r["url"] not in _lin_failed]
        if len(got["urls"]) < 2:
            got["problems"] += unused_msgs
            got["problems"].append(
                f"名鑑の個別ページが {len(got['urls'])} 件しか見つかりません"
                "（2件以上が要る・転載照合で取得できないページを除いた結果）")
            return got
    # 取得失敗以外の照合不能（想定外）は従来どおり全体を止める
    for p_ in lin.get("problems") or []:
        if not any(p_.startswith(u) for u in _lin_failed):
            got["problems"].append(f"転載照合を実施できません: {p_[:120]}")
    for sp in lin["suspects"]:
        got["problems"].append(
            f"転載の疑い: {sp['a']} と {sp['b']} の本文が {sp['ratio']:.0%} 一致"
            f"（登録簿に系列が書かれていません）")
    mv = _mc.agree(looks)
    got["model_code"] = mv.get("model_code")
    # ★採用値と観測値を分ける★（2026-08-09・依頼130 P1-2）
    #   型式を載せているのは P-WORLD だけなので、独立2出典はそろわない。
    #   記事には出さない（＝採用しない）が、**取り違えを防ぐ検査には使う**。
    got["observed_model_code"] = (mv.get("model_code")
                                  or mv.get("observed_model_code"))
    got["observed_model_hosts"] = (mv.get("hosts")
                                   or mv.get("observed_hosts") or [])
    # ★名鑑2票一致の登場年月★（2026-08-02・Codex47回目に条件つきで承認）
    #   使ってよいのは「型式が一致した、同定検査を全部通った同じ2名鑑」の
    #   月が一致した時だけ。公式が年月を画像でしか出さない社（山佐）のため。
    if mv.get("adopted"):
        _hosts = set(mv.get("hosts") or [])
        _months = {r.get("release_hint") for r in looks
                   if r.get("release_hint")
                   and r["url"].split("/")[2].lower().removeprefix("www.")
                   in _hosts}
        if len(_months) == 1 and len([
                r for r in looks if r.get("release_hint")
                and r["url"].split("/")[2].lower().removeprefix("www.")
                in _hosts]) >= 2:
            got["directory_release"] = _months.pop()
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
    # ★型式名の印は専用の判定で読む★（2026-08-02・Codex54回目。
    #   題名用の _gen_mark だと実在のBT型式「LB/タコスロBD」が印なしになる）
    _want_gen = _mc._gen_mark(name)
    # ★検査は観測値で行う★（2026-08-09・依頼130 P1-2）
    #   採用値（独立2出典）だけを見ていたので、型式が1つしか無い新台では
    #   規格印の矛盾も重複も検査できていなかった＝取り違えを防ぐ入力が消えていた。
    _obs = got.get("observed_model_code")
    if _obs and _want_gen and _mc.model_gen_mark(_obs) != _want_gen:
        got["problems"].append(
            f"型式名の規格印が確認できません（機種は{_want_gen}版なのに、"
            f"型式名「{_obs}」に{_want_gen}の印がありません。"
            "同名の旧機種のページを見ている可能性）")
        got["model_code"] = None
        got["observed_model_code"] = None
    elif _obs and not _want_gen:
        # ★公式名にL/Sが無い社は現に在る★（2026-08-02・Codex43回目。
        #   北電子の公式名は「マイジャグラーVI」でP-WORLDの型式は
        #   「SマイジャグラーVI KK」＝一律の人送りだと実在の新台を出せない）
        #   型式名そのものに規格の印（L/S）があれば、独立2票の印を信じて通す。
        #   印の無い型式名だけ人の確認へ（規格を機械で確定できないため）。
        _code_gen = _mc.model_gen_mark(_obs)
        if _code_gen in ("L", "S"):
            _log(f"  公式名に規格印なし。型式名の印（{_code_gen}）で照合: {_obs}")
        else:
            got["problems"].append(
                f"型式名: 機種の規格（L/S）が公式名「{name[:30]}」からも"
                f"型式名「{_obs}」からも読めません"
                "（人が確認してください）")
            got["model_code"] = None
            got["observed_model_code"] = None
    def _read(mod, jp):
        """器ごとに全ページを読み、★使えなかったページの理由を必ず残す★

        （2026-07-31・自分で再現）以前はページ単位の不採用理由を捨てていたので、
        「本文にCZが6つあるのに3つしか採れなかった」ような取りこぼしが
        誰にも伝わらないまま、材料だけが減っていた。
        """
        pages = [mod.read_page(u, name, expected_maker=maker,
                              grant=_grant, page=_pages.get(u),
                              dmm_identity=_ident_for(u))
                 for u in got["urls"]]
        for pg in pages:
            if not pg.get("ok"):
                got["problems"].append(
                    f"{jp}: {pg['host']} を使えませんでした（{pg.get('reason', '')[:90]}）")
        # ★DMM単独の例外の文脈を渡す★（空なら今までどおり独立2票のみ）
        return mod.compare(pages, ctx=_adopt_ctx)

    got["material"] = _read(_sl, "基本スペック")
    # ★型式名の正本は mv（独立2票）★（2026-08-02・Codex29回目）
    #   基本スペック側は文字列の完全一致で拾うため、空白差があると採用されず、
    #   記事と identity に型式名が入らず、型式の重複検出からも漏れていた。
    if got["model_code"]:
        got["material"]["adopted"]["model_code"] = {
            "value": got["model_code"],
            "sources": list(mv.get("hosts") or [])}
    elif got.get("observed_model_code"):
        # ★1出典しか無い型式は「観測値」として残す★（2026-08-09・依頼130 P1-2）
        #   記事には出さない（採用しない）が、あとから
        #   「どの型式のページを見て作ったか」を追えるようにする。
        got["material"]["observed_model_code"] = {
            "value": got["observed_model_code"],
            "sources": list(got.get("observed_model_hosts") or []),
            "_note": "1出典のみ。記事には出さず同定にだけ使う",
        }
    # ★天井は一式で採る★（値だけ先に載せない）
    # ★天井はCZ名の突き合わせつきで採る★（2026-08-06）
    #   出典によって「CZ」と書く所と「関所チャレンジ」と書く所がある。
    #   ★独立2出典が『CZ＝その名前』と書いている時だけ★同じ物として扱う。
    _cl_pages = [_cl.read_page(u, name, expected_maker=maker,
                               grant=_grant, page=_pages.get(u),
                               dmm_identity=_ident_for(u))
                 for u in got["urls"]]
    for _pg in _cl_pages:
        if not _pg.get("ok"):
            got["problems"].append(
                f"天井: {_pg['host']} を使えませんでした（{_pg.get('reason','')[:90]}）")
    got["material"]["ceilings"] = _cl.compare(
        _cl_pages,
        # ★CZ名の確認にも同じ文脈を渡す★＝本体だけ通すと
        #   「天井は採れるがCZ名に寄せられない」半端な状態になる
        cz_names=_cl.verified_cz_names(_cl_pages, ctx=_adopt_ctx),
        ctx=_adopt_ctx)
    for nt in got["material"]["ceilings"]["need_third"]:
        got["problems"].append(f"{nt['jp']}: {nt['why']}")
    # ★ATの仕様はモードごとに★（純増を混ぜたら誤情報）
    got["material"]["at_specs"] = _read(_at, "ATの仕様")
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
             "読める状態ではありません",
             # ★更新の途中の食い違いも待てば解ける★（Codex47回目）
             "一覧で食い違っています",
             # ★名簿に別名を足せば解ける★（Codex51回目）
             "名鑑のメーカー欄を名簿で解決できません")
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


# ★「ページが読めない」失敗だけを障害として数える★（2026-08-04・Codex65〜66回目）
#   年月未掲載などページ自体は読めた理由と混ぜない（隔離の判定を歪めないため）。
#   ★classify が実際に返す形に合わせる★（Codex66回目の指摘1。
#     SSL・通信失敗は _get の WatchError がそのまま理由になる＝
#     「取得できません（URLError）: url」「取得できません（HTTP 503）: url」
#     「HTTP 404: url」「ページが大きすぎます: url」。
#     「公式ページが読める状態ではありません」は HTTP 200 の障害画面の形）
_OUTAGE_PREFIXES = ("公式ページが読める状態ではありません",
                    "取得できません（", "HTTP ", "ページが大きすぎます")

# ★復旧夜に個別ページを読みに行く上限（メーカーごと・一晩）★（Codex67回目）
#   行列投入の上限（moved）だけだと、部分復旧＝残りが障害中の夜に
#   143件へ全部取得しに行ってしまう（障害中サイトへの負荷と最大143×timeoutの
#   実行時間）。読みに行く件数そのものにも別のカウンタで上限を置く。
#   20件＝一晩の見張りが各社の一覧を読む規模と同程度で、144件の隔離でも
#   約1週間で排出できる折り合いの値。
RECLASSIFY_FETCH_PER_NIGHT = 20


def _is_outage(reasons: list) -> bool:
    return any(str(x).startswith(_OUTAGE_PREFIXES) for x in reasons)



# ★書き込みを止める理由★（Codex指摘3・自分で再現を確認）
#   以前は problems を文字列で並べるだけで、**中身を見ずに書き込めた**。
#   機種の同定に関わる問題が1つでもあれば、材料が採れていても書かない。
BLOCKING = ("CONFIRMED_VALUES_UNREADABLE",
            "AMBIGUOUS_CANDIDATES", "CATALOG_UNHEALTHY",
            # ★型式名は「別機種と取り違えない」ためだけに使う★（2026-08-09・運営者決定）
            #   実測: 型式名を載せているのは P-WORLD だけだった。
            #   DMMは描画して読んでも載せておらず、なな徹・ちょんぼりすた・
            #   メーカー公式にも無い。つまり「独立2出典が要る」という条件は
            #   **新台では原理的に満たせない**（4夜連続で1件も公開できなかった原因）。
            #   運営者の判断＝型式は記事に書かない。同定にだけ使う。
            #   よって「まだ載っていない／1つにしか載っていない」では止めない。
            #   ★取り違えを防ぐ検査は残す★＝下の3つは今までどおり止める。
            "名鑑ごとに型式名が食い違っています",   # 別機種の資料が混じっている
            "型式名の規格印が確認できません",       # 同名の旧機種のページの疑い
            "型式名: 機種の規格（L/S）が",          # 規格を機械で確定できない
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
            # ★公式を確認できていない状態で書かない★（2026-08-02・Codex45回目。
            #   RETRYABLEに足した時、公開を止める側に入れ忘れていた。
            #   メンテ画面でも名鑑2票がそろえば公開へ進めた）
            "読める状態ではありません",
            # ★個別と一覧の食い違いのまま書かない★（Codex47回目）
            "一覧で食い違っています",
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


# ★どの一覧を読むか★（自己テストが本番を書き換えないように差し替えられる）
#   （2026-08-11・依頼157のP1）以前は自己テストが本番の machines.json を
#   偽データや壊れたJSONで**直接上書き**していた。通常終了なら戻すが、
#   強制終了や電源断で偽データが残る。実行中に別処理が読めば壊れた状態も見える。
MACHINES_PATH = os.path.join(BASE, "assets/data/machines.json")


def _machine_class(slug: str) -> str:
    """コミット文に書くための区分（読めなければ「区分不明」）。"""
    try:
        ms = _sj.read_json(MACHINES_PATH, expect=(dict, list))
        ms = ms["machines"] if isinstance(ms, dict) else ms
        for m in ms:
            if m.get("slug") == slug:
                return _pdz.machine_class(m)
    except Exception as e:                # noqa: BLE001
        # ★止めないが、原因は残す★（2026-08-05・Codex99回目。
        #   握りつぶすと「区分不明」になった理由が誰にも分からなかった）
        _log(f"  コミット文の区分を読めません（{type(e).__name__}: {e}）")
    return "区分不明"


# ★同定できなかった、という印★（2026-08-12・依頼166のP0）
#   止める判定は「決まった文言が入っているか」で見ている。
#   新しい同定（P-WORLD）の文言は当然そこに無いので、
#   **同定が失敗しても公開が止まらなかった**（実際に確かめた）。
#   文言を足していく形は、経路が増えるたびに同じ穴が開く。
#   ★同定の失敗にはこの印を必ず付ける★＝文言に関係なく止まる。
IDENTITY_FAILED = "★本人性を確かめられませんでした★"


def blocking_problems(problems: list) -> list:
    """★書いてはいけない理由だけを取り出す★（新台追加も更新も同じ判定を使う）

    2026-08-05・Codex102回目: 更新側がこれを見ておらず、
    転載の疑いなど「材料は返るが公開してはいけない」場合を素通りしていた。
    """
    return [p for p in problems
            if IDENTITY_FAILED in p or any(w in p for w in BLOCKING)]


def _blocking(problems: list) -> list:
    return blocking_problems(problems)




# ★同定に使った一覧HTMLの保管場所★（リポジトリの外・上書きしない）
EVIDENCE_DIR = os.path.join(
    os.path.expanduser("~"), "Documents", "uchidokoro", "identity_evidence")


def _save_evidence(html: str, ev: dict) -> str:
    """同定の根拠にした一覧HTMLを、指紋の名前で確かに保管する。

    ★なぜ要るか（2026-08-04・Codex93回目の指摘8）★
      記事に残していたのは指紋（sha256）だけで、**元のHTMLはどこにも無かった**。
      あとから「本当にこのカードだったのか」を誰も確かめられない
      ＝証跡として成立していない。指紋を名前にして保管し、
      記事には **その場所とカード番号** を残す。

    ★保管できなければ空を返す＝その機種は公開しない★
      （2026-08-04・Codex94回目の指摘4。私は「指紋が残るから続けてよい」と
        考えたが、一覧が更新されれば同じHTMLは二度と手に入らず、
        指紋だけでは何も確かめられない＝証跡なしで公開したのと同じ。
        その晩は公開せず待ち行列に残し、翌日やり直す）

    ★書き途中の壊れたファイルを「保管済み」と思い込まない★
      いったん別名で書き、**中身から指紋を計算し直して一致した時だけ**置く。
      既にある場合も中身を読んで指紋を確かめる。

    ★必ずバイト列のまま扱う★（2026-08-04・Codex95回目の指摘2。実物で確かめた）
      文字として書くとWindowsでは改行が 
 に変換され、
      **ファイル名の指紋と、そのファイルの実バイトの指紋が食い違っていた**。
      あとから普通に sha256 を取った人が「別物だ」と判断してしまうので、
      書くのも読むのもバイナリにして、実バイトで照合する。
    """
    digest = str(ev.get("list_html_sha256") or "").split(":")[-1]
    if not digest:
        _log("  同定の根拠の指紋がありません")
        return ""
    fp = os.path.join(EVIDENCE_DIR, f"{digest}.html")
    ok_ref = f"identity_evidence/{digest}.html #card{ev.get('card_index')}"
    raw = (html or "").encode("utf-8")
    tmp = ""
    try:
        os.makedirs(EVIDENCE_DIR, exist_ok=True)
        if os.path.exists(fp):
            if _sha_bytes(io.open(fp, "rb").read()) == digest:
                ev["saved_path"] = fp
                return ok_ref
            _log("  保管済みの根拠が壊れています（書き直します）")
        tmp = fp + f".tmp{os.getpid()}"
        with io.open(tmp, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        if _sha_bytes(io.open(tmp, "rb").read()) != digest:
            _log("  根拠を書いた結果が元と一致しません（保管しません）")
            return ""
        os.replace(tmp, fp)
        tmp = ""
        ev["saved_path"] = fp
        return ok_ref
    except Exception as e:                # noqa: BLE001
        _log(f"  同定の根拠を保管できません（{e}）")
        return ""
    finally:
        # ★書き途中のファイルを残さない★（Codex95回目の指摘3。
        #   fsync や os.replace で失敗した場合も片付ける）
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _sha_bytes(b: bytes) -> str:
    import hashlib
    return hashlib.sha256(b or b"").hexdigest()


def _evidence_ref(vo: dict) -> str:
    """記事に残す証跡の指し先（一覧カード同定でない時は空）。"""
    ev = vo.get("identity_evidence") or {}
    if not ev:
        return ""
    # ★証拠は binding ごとに形が違う★（2026-08-12・依頼166のP1）
    #   一覧カード用の形で組み立てると、P-WORLDの証拠は
    #   存在しないカード番号を指す文字列になり、あとから確かめられない。
    if ev.get("kind") == "DMM_MACHINE_PAGE":
        # ★DMMには検定番号が無い★ので、型式・メーカー・導入日で指す。
        #   型式も未導入の新台には無いので「未掲載」と書く（空にしない）。
        return "dmm:%s 型式=%s メーカー=%s 導入=%s 確認日=%s" % (
            ev.get("dmm_machine_id", ""),
            ev.get("model_code") or "未掲載", ev.get("maker", ""),
            ev.get("release", ""), ev.get("checked_at", ""))
    return str(ev.get("evidence_ref")
               or (ev.get("list_html_sha256", "") + f" #card{ev.get('card_index')}"))



_PW_MACHINE_RE = re.compile(
    r"^https?://(?:www\.)?p-world\.co\.jp/machine/database/(\d{1,7})/?$")


def _slug_hint(url: str) -> str:
    """台帳のslug欄に入れる目印（機種IDが分かればそれ、無ければ site）。

    ★DMMのURLも見る★（2026-08-16・台帳#376）。移行前に公開した機種は
    slugが pw_ のままなので、増やせない対応表（slug_binding）で読み替える
    ＝台帳の目印が、実際に公開されているページのslugと一致する。
    """
    import slug_binding as _sb
    mid = _dmm_machine_id(url)
    if mid:
        want = "dmm_" + mid
        for old, new in _sb.LEGACY_BINDINGS.items():
            if new == want:
                return old              # 公開済みの機種は昔のslugのまま
        return want
    mid = _pw_machine_url(url)
    return ("pw_" + mid) if mid else "site"


def _pw_machine_url(url: str) -> str:
    """P-WORLDの機種ページなら機種IDを返す（違えば空）。"""
    m = _PW_MACHINE_RE.match(str(url or "").strip())
    return m.group(1) if m else ""


# ★止まった理由の符丁★（2026-08-22。★自由文を見張りに使わない★）
#   文言はいつでも書き換わるので、見張りが読むのは短い符丁だけにする。
#   ここに無い形は OTHER になる（＝符丁が増え続けない）。
_BLOCKER_CODES = (
    # ★★公開直前の関所で落ちた形を先に見る★★
    #   （2026-08-24の夜・台帳#474。★見張りが2晩、無害な理由を報告していた★）
    #   X-300は記事を作るところまで進み、公開直前の監査で止まったのに、
    #   材料集めの段階で出た「型式名がまだ載っていません」に先に当たって
    #   `MODEL_CODE_MISSING` と記録されていた。
    #   ＝★いちばん深くまで進んだ機種について、いちばん無害な理由を報告する★
    ("サイト監査", "BLOCKED_BY_SITE_AUDIT"),
    ("公開の関所", "BLOCKED_BY_PREPUSH_GATE"),
    ("名鑑の個別ページが", "NOT_ENOUGH_DIRECTORIES"),
    ("メーカー照合の控えを読めません", "MAKER_CACHE_UNREADABLE"),
    ("メーカー", "MAKER_UNRESOLVED"),
    ("採用できた材料", "NO_MATERIAL"),
    ("型式", "MODEL_CODE_MISSING"),
    ("取れません", "FETCH_FAILED"),
    ("担当", "NOT_TODAYS_TARGET"),
)


def _blocker_code(res: dict) -> str:
    """止まった理由を短い符丁にする。

    ★★「止めた理由」を先に見る★★（2026-08-24の夜・台帳#474）
      ★直す前は blocked と problems を混ぜて、先に当たった語を採っていた★。
      problems には材料集めの途中の注意書き（型式名がまだ無い等）が
      たくさん入るので、**公開直前で止まった機種ほど無害な符丁**になった。
      ＝見張りが「型式名が無いだけ」と報告し、本当の理由（監査で停止）が
        2晩埋もれた。
      → まず blocked（止めた理由そのもの）だけで探し、
        当たらなければ problems を見る。
    """
    def _pick(items) -> str:
        text = " ".join(str(x) for x in (items or []))
        if not text.strip():
            return ""
        for word, code in _BLOCKER_CODES:
            if word in text:
                return code
        return ""

    hit = _pick(res.get("blocked"))
    if hit:
        return hit
    hit = _pick(res.get("problems"))
    if hit:
        return hit
    text = " ".join(str(x) for x in
                    ((res.get("blocked") or []) + (res.get("problems") or [])))
    return "OTHER" if text.strip() else ""


def _verify_dmm(name: str, official_url: str, maker: str,
                release: str, expect_maker: str = "",
                release_is_cache: bool = False) -> dict:
    """★DMMの機種ページで身元を確かめる★（2026-08-16・台帳#376）

    返す形は verify_official と同じ（呼ぶ側を変えないため）。
    ★メーカーも突き合わせる★＝名簿の表示名と、ページの表示名が同じか。
      ここを見ないと、別会社の機種を渡されたまま進んでしまう。

    ★機種名は「見出しから作らない」★
      DMMの見出しはSEOの飾りつきなので、機種名の正はカレンダー側。
      ここでは**渡された名前がそのページの機種を指しているか**だけを見る。

    ★型式名は無いことがある★（未導入の新台）。同定の芯は
      **機種ID＋機種名＋メーカー＋導入日**で、型式名はあれば足す。
    """
    import dmm_discover as _dd
    import dmm_machine as _dm
    out = {"problems": [], "release": ""}
    mid = _dmm_machine_id(official_url)

    def _ng(why: str) -> dict:
        out["problems"].append(f"{IDENTITY_FAILED} {why}")
        return out

    try:
        cats = _sj.read_json(_dd.MAKER_CATALOG, expect=dict)["catalogs"]
    except Exception as e:                # noqa: BLE001
        return _ng(f"メーカー名簿を読めません: {e}")
    conf = cats.get(maker) or {}
    allow = [conf.get("name")] + list(conf.get("directory_names") or [])
    allow = [str(x) for x in allow if x]
    if maker and not allow:
        return _ng(f"メーカーが名簿にありません: {maker!r}")
    try:
        got = _dm.fetch(mid)
    except _dm.MachineError as e:
        return _ng(str(e)[:220])
    ok, why = _dm.name_matches(got["heading"], name)
    if not ok:
        return _ng(f"機種名が機種ページと合いません: {why[:180]}")
    page_maker = str(got.get("maker") or "")
    if allow and not page_maker:
        # ★読めなかったことを、確かめたことにしない★（依頼167のP0）
        return _ng("機種ページのメーカーを読めませんでした")
    # ★最初に確かめた表示名があれば、そちらと完全一致させる★（依頼167のP1）
    want = [expect_maker] if expect_maker else allow
    if want and _dd._norm(page_maker) not in {_dd._norm(x) for x in want}:
        return _ng(f"メーカーが食い違います（期待: {'／'.join(want)} / "
                   f"機種ページ: {page_maker}）")
    out["release"] = got.get("release_date") or ""
    # ★渡された年月と食い違わないか★（★控えは照合しない★）
    #   ★機種ページが月までのときは月で比べる★（日は勝手に決めない）
    if release and not release_is_cache and out["release"]:
        if str(release)[:7] != out["release"][:7]:
            return _ng(f"登場年月が機種ページと違います"
                       f"（機種ページ={out['release']} / 渡された値={release}）")
    if out["release"] and not _nw.is_recent(out["release"][:7]):
        return _ng(f"登場年月が新台の範囲外です（{out['release']}）")
    # ★機種名は渡された値（＝カレンダー側）を正とする★
    out["identity_name"] = name
    out["identity_binding"] = "DMM_MACHINE_PAGE"
    out["identity_evidence"] = {
        "kind": "DMM_MACHINE_PAGE",
        "dmm_machine_id": mid,
        "url": got.get("url") or official_url,
        "model_code": got.get("model_code", ""),
        # ★型式名が無いことは異常ではない★（未導入の新台には載らない）
        "has_model_code": bool(got.get("has_model_code")),
        "maker": page_maker,
        "release": out["release"],
        "release_precision": got.get("release_precision", ""),
        "checked_at": __import__("datetime").date.today().isoformat()}
    _log(f"  DMMの機種ページで同定しました: {out['identity_name']} "
         f"/ 機種ID {mid} / {out['release']}"
         + ("" if got.get("has_model_code") else "（型式名はまだ載っていません）"))
    return out



# ★自己試験の最中か★（架空のURLを使うので同定元の縛りだけ外す）
#   ★本番で立つことはない★＝selftest() の中でだけ真にする。
#   ★規約の関所は外れない★＝あちらは new_machine_watch 側で別に見る。
_IDENTITY_SELFTEST = {"on": False}


def identity_url_problem(official_url: str) -> str:
    """★同定に使ってよいURLか★（だめなら理由・よければ空）

    （2026-08-16・依頼217の指摘2）
    ★規約の関所とは別の縛り★＝あちらは「通信してよいか」を決める。
    ちょんぼりすた・なな徹は**材料としては通信を許している**ので、
    そのURLを同定に渡すと関所は通してしまう。
    「機種の正体を決めてよいのはDMMの機種ページだけ」は業務上の決まりなので、
    ここで別に見る。★通信の前に断る★＝止まる前に取りに行かない。

    ★呼ぶ場所は3つ★ verify_official / fill_missing / 手で渡す入口
    """
    u = str(official_url or "").strip()
    if not u:
        return "機種ページのURLがありません"
    if _dmm_machine_id(u):
        return ""
    if _IDENTITY_SELFTEST["on"]:
        return ""                      # ★試験は架空のURLを使う★
    if _pw_machine_url(u):
        return ("P-WORLDのURLは使えません"
                "（利用規約でプログラムからの取得が禁止・台帳#376）")
    return (f"同定に使えるのはDMMの機種ページだけです: {u[:70]}"
            "／★出典の材料と、機種の正体を決める根拠は別です★")


def verify_official(name: str, official_url: str,
                    maker: str = "", release: str = "",
                    release_is_cache: bool = False,
                    expect_maker: str = "") -> dict:
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
    # ★P-WORLDの機種ページはそちらで確かめる★（2026-08-12・入口をここ一本にした）
    #   以降の検査は「メーカー公式のドメインか」を見るので、
    #   P-WORLDのURLは必ず弾かれる（実際に試して確認した）。
    #   機種ページ側は機種IDでの同定・種目・転送・派生機まで見ている。
    # ★DMMの機種ページ以外は、通信する前に断る★（依頼217の指摘2）
    _ng = identity_url_problem(official_url)
    if _ng:
        return {"problems": [f"{IDENTITY_FAILED} {_ng}"], "release": ""}
    # ★同定の正はDMM★（2026-08-16・台帳#376）
    if _dmm_machine_id(official_url):
        # ★最初に確かめた表示名があれば、そちらと完全一致させる★（台帳#335の項目5）
        return _verify_dmm(name, official_url, maker, release,
                           expect_maker=expect_maker,
                           release_is_cache=release_is_cache)
    if _pw_machine_url(official_url):
        # ★入口で断る★（2026-08-16・依頼213）
        #   「通信で止まる」に頼ると、規約違反の一歩手前まで進んでしまう。
        #   手で --official-url を渡した時もここで止める。
        return {"problems": [f"{IDENTITY_FAILED} P-WORLDのURLは使えません"
                             "（利用規約でプログラムからの取得が禁止・台帳#376）"
                             "／★DMMの機種ページのURLを使ってください★"],
                "release": ""}
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
        # ★用途を名乗ってから取りに行く★（依頼218）
        #   ここは「そのページがこの機種か」を確かめる＝machine_identity。
        #   名乗りが無いと関所が例外を投げ、下の except が
        #   「取得できない」と読み替えて**静かに同定が落ちる**。
        with _nw.fetching("machine_identity"):
            html = _nw._get(official_url)
    except Exception as e:
        # ★取得できない時、同じ公式の一覧カードで確かめ直す★
        #   （2026-08-04・台帳#209、Codex92回目で条件つき承認）
        #   ★「取得できません」という理由を消すのではなく、
        #     名前・種目・登場年月を**一覧から取り直して同じだけ確かめる**★
        # ★失敗の中身まで見る★（2026-08-04。_get の文言は
        #   「取得できません（URLError）」までしか書かないので、
        #   例外の連鎖をたどらないと証明書エラーだと分からなかった）
        # ★一覧カードでの同定は廃止★（2026-08-16・運営者判断）
        #   出典は大手サイトへ寄せると決めたので、メーカー公式の一覧で
        #   同定する経路ごと消した（該当していた3機種はDMMへ移した）。
        card = None
        if card is not None:
            out["release"] = card["release"]
            # ★カードの公式名を正として後段へ渡す★（2026-08-04・Codex93回目の指摘2）
            #   表記ゆれを許した以上、記事名・名鑑検索・公開データが
            #   こちらの持っていた表記のままだと「公式で確かめた名前」ではなくなる。
            out["identity_name"] = card["name"]
            out["identity_binding"] = "MAKER_LIST_CARD"
            out["identity_evidence"] = card["evidence"]
            _log(f"  公式の個別ページを取得できないため、同じ公式の一覧カードで"
                 f"同定しました: {card['name']} / {card['release']}")
            return out
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
    # ★h1は可視のものだけをHTML解析で読む★（2026-08-02・Codex55回目。
    #   正規表現の全h1読みだと、隠しh1の「パチスロ」が回胴機の証拠になった）
    _head_txt = _nw.page_title(html) + " " + " ".join(_nw._visible_h1s(html))
    _head_txt = unicodedata.normalize("NFKC", _head_txt)
    # ★企業の定型句は種目の証拠から除く★（2026-08-02・Codex50回目）
    #   「…|パチンコ・パチスロメーカー」の定型句がパチスロの証拠になり、
    #   ぱちんこページの拒否も打ち消していた。
    _head_txt = _head_txt.replace("パチンコ・パチスロメーカー", "")
    _slot_w = ("パチスロ", "スロット", "スマスロ", "回胴", "ぱちスロ")
    _slot_ev = (any(w in _head_txt for w in _slot_w)
                or _mc._gen_mark(name) in ("L", "S"))
    # ★パチンコ語は、パチスロ語と同居していても打ち消さない★
    #   （2026-08-02・Codex55回目。「パチンコ ○○」のh1に隠しh1の
    #     「パチスロ」を添えるだけで拒否が解除できた。定型句は上で
    #     除いてあるので、残ったパチンコ語は種目の印として信じる）
    _pachi_ev = any(w in _head_txt
                    for w in ("ぱちんこ", "パチンコ", "スマパチ"))
    if _pachi_ev or not _slot_ev:
        out["problems"].append(
            "パチスロのページに見えません（題・見出しに回胴機の証拠が無い）")
    ok, why = _mc.page_is_machine(
        html, name,
        extra_tail_ok=_mc.maker_brand_cores(maker) if maker else None,
        strict_all_tail=True)
    if not ok:
        # ★かぎ括弧の公式題は、抜き出した機種名の完全一致で照合する★
        #   （2026-08-02・Codex42回目。実在＝山佐「「スマスロパリピ孔明」公式サイト」
        #     ・大都「大都技研「スロット ワールドダイスター」製品サイトはこちら!」。
        #     題の語検査では通らず、大都の8月導入機を出せない経路だった）
        #   条件は厳しいまま＝①「…」から抜いた名前の芯が指定名と完全一致
        #   ②規格の印（L/S）が食い違わない。派生機（…SP等）は芯が違うので通らない。
        _mn = _nw.machine_name(html)
        _mn_core = _mc._ci.normalize_core(_mn)
        _nm_core = _mc._ci.normalize_core(name)
        _g1, _g2 = _mc._gen_mark(_mn), _mc._gen_mark(name)
        # ★かぎ括弧の外も検査する★（2026-08-02・Codex46回目）
        #   「「機種名」SP公式サイト」のように、括弧の外の派生印を見ないと
        #   別版の公式ページを通せた。外に許すのは社名・飾り・定型句
        #   （公式サイト・製品サイトはこちら等）だけ。
        _ttl = _nw.page_title(html)
        _pre = _ttl.split("「", 1)[0]
        _suf = _ttl.split("」", 1)[1] if "」" in _ttl else ""
        _extra46 = _mc.maker_brand_cores(maker) if maker else None
        _outside_ok = (_mc._after_ok(_pre, _nm_core, name, _extra46)
                       and _mc._after_ok(_suf, _nm_core, name, _extra46))
        if "「" in _ttl and _outside_ok and _mn_core and _mn_core == _nm_core \
                and not (_g1 and _g2 and _g1 != _g2):
            ok = True
            _log(f"  公式題はかぎ括弧形。抜き出した機種名の完全一致で照合: {_mn[:30]}")
    if not ok:
        out["problems"].append(
            f"公式ページと名前が一致しません（{why}）: "
            f"公式のタイトル={_nw.page_title(html)[:40]!r} / 指定名={name!r}")
    if maker:
        out["problems"] += _verify_maker(final_url, maker)
    else:
        out["problems"].append("メーカーが指定されていません")
    got = _nw.release_month(_text)
    if maker:
        lv = _release_from_official_list(maker, official_url)
        if not got and lv:
            # ★個別ページに年月が無ければ、公式一覧のカードから取り直す★
            #   （2026-08-02・Codex27回目。サミーは一覧に「2026.9」・個別には無し）
            got = {"value": lv, "precision": "month",
                   "quote": "メーカー公式一覧のカードに記載"}
        elif got and lv and lv != got["value"]:
            # ★個別と一覧の食い違いは公開しない★（2026-08-02・Codex47回目）
            #   更新の途中かもしれない＝待てば解ける。
            out["problems"].append(
                f"登場年月が公式の個別ページと一覧で食い違っています"
                f"（個別={got['value']} / 一覧={lv}）")
            return out
    if not got:
        # ★人間確認済みの控え（release_overrides）★（2026-08-02・Codex46回目）
        #   山佐は導入年月を画像でしか載せない＝機械では読めない実在形。
        #   運営者が公式の画像を目視確認して書いた値だけを最後の控えに使う。
        #   無人タスクはこのファイルに書かない（読むだけ）。
        ov = _release_override(official_url)
        if ov:
            got = {"value": ov["value"], "precision": "month",
                   "quote": f"運営者確認: {ov.get('source', '')[:60]}"}
            _log(f"  登場年月は運営者確認の控えを使用: {ov['value']}")
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


RELEASE_OVERRIDES = r"（書類フォルダ）/uchidokoro/release_overrides.json"


def _release_override(url: str):
    """人間確認済みの登場年月（公式が画像でしか載せない機種用）。無ければNone。"""
    try:
        d = _sj.read_json(RELEASE_OVERRIDES, expect=dict)
    except Exception:                     # noqa: BLE001
        return None
    it = (d.get("items") or {}).get(url.rstrip("/") + "/")         or (d.get("items") or {}).get(url)
    if isinstance(it, dict) and re.match(r"^20\d\d-\d\d$", str(it.get("value") or "")):
        return it
    return None


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
                # ★メーカー公式の一覧を読む経路は削除済み★（台帳#377）
                #   `list_url` はもう名簿に無いので、ここへは来ない。
                html = ""
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
        # ★機種IDも渡す★（2026-08-16・台帳#376）
        #   URLは変わりうるが機種IDは機種ごとに1つ。同じ機種を
        #   二重に持たないための鍵になる。
        _pend.add(pend, name, url, maker, release, reason,
                  source_machine_id=_dmm_machine_id(url),
                  identity_source="dmm" if _dmm_machine_id(url) else "")
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


# ★ロックを失ったまま書かない★（2026-08-11・台帳#269）
#   生存信号を打てなくなった＝他のタスクに奪われた可能性がある。
#   書く直前（1日1機種の枠を使う所）で止める（fail-closed）。
_LOCK_LOST: list = []


def _claim_today(official_url: str) -> bool:
    """★1日1機種の上限をコードに守らせる★（人の判断に任せない）"""
    # ★★書く直前に、実際にロックを持っているか自分で確かめる★★
    #   （2026-08-11・依頼152の指摘②）
    #   印（_LOCK_LOST）を見るだけでは足りない。プロセスが長く止まって
    #   ロックを奪われた後にメインが先に動き出すと、見張りの糸が
    #   気づく前に枠を取れてしまう。ここで同期して確かめる。
    if _LOCK_LOST:
        print("★ロックの生存信号を打てなくなりました → 何も書きません★: "
              + str(_LOCK_LOST[0])[:120])
        return False
    ctx = os.environ.get("UCHIDOKORO_LOCK_CTX")
    if ctx:
        c = _run_capped(
            [sys.executable, os.path.join(BASE, "scripts", "task_lock.py"),
             "check", "--ctx", ctx], capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        if c.returncode != 0:
            print("★いまロックを持っていません → 何も書きません★: "
                  + (c.stderr or c.stdout or "").strip()[:150])
            _LOCK_LOST.append("check が非0")
            return False
    slug = _ba.slug_from_url(official_url)
    g = _run_capped(
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
    r = _run_capped(["git", "rev-parse", "HEAD"], cwd=BASE,
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return (r.stdout or "").strip()


# ★★外部プロセスには必ず打ち切り時間を付ける★★
#   （2026-08-25・Codexの26回目）
#   ★1か所ずつ書くと必ず漏れる★ので、入口を1つにして既定値を持たせる。
#   ローカルの処理でも、子プロセスやファイルシステムが固まれば
#   **上限なく待ち続ける**＝タスクが黙って止まり、ロックが延び、
#   翌朝の更新タスクまで巻き添えになる。
#   ★呼ぶ側が timeout を明示したときは、そちらを優先する★。
PROC_TIMEOUT = 300


def _run_capped(args, **kw):
    """打ち切り時間つきで外部プロセスを動かす（既定 PROC_TIMEOUT 秒）。"""
    kw.setdefault("timeout", PROC_TIMEOUT)
    return subprocess.run(args, **kw)   # ★ここだけ素の呼び出し★


def _dirty_before_from_mark():
    """★公開の目印から「始める前に変わっていた一覧」を取る★

    ★取れないときは None を返す★＝関所が「分からない」と答えて止まる。
    ここで空配列を返すと「綺麗だった」と嘘をつくことになる。
    """
    try:
        m = _sj.read_json(_pub.IN_PROGRESS, expect=dict,
                          allow_missing=True, default=None)
    except Exception:                                        # noqa: BLE001
        return None
    if not isinstance(m, dict):
        return None
    v = m.get("dirty_before")
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        return sorted(set(v))
    return None


def _dirty_before_kept(slug: str):
    """★いま控えてある「始める前の状態」を持ち越す★

    ★同じ機種のものだけ★（別の公開の控えを引き継がない）。
    読めない・形が違う・機種が違うなら None（＝関所が止める）。
    """
    try:
        m = _sj.read_json(PUSH_PENDING, expect=dict,
                          allow_missing=True, default=None)
    except Exception:                                        # noqa: BLE001
        return None
    if not isinstance(m, dict) or str(m.get("slug") or "") != slug:
        return None
    v = m.get("dirty_before")
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        return v
    return None


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
    # ★★「始める前に何が変わっていたか」をここへ引き継ぐ★★
    #   （2026-08-25・Codexの26回目。★私の直しは本番では効いていなかった★）
    #   ★公開の目印（.publish-in-progress.json）は、関所を呼ぶ**前**に
    #     mark_done() が消す★ので、関所は dirty_before を見られなかった。
    #   ＝便乗の遮断が、通常の経路では一度も働いていなかった。
    #   push をやり直す経路もこの目印しか読まないので、ここに載せる。
    # ★★一度控えた値を、あとから消さない★★（2026-08-25・本番で踏んだ）
    #   ★書く場所が3か所ある★＝
    #     ①公開のあと（on_written）… 公開の目印がまだ生きているので取れる
    #     ②③push の途中 … ★このときは目印がもう消えている★
    #   ②③で取り直すと None になり、**関所が「読めない形」で止める**。
    #   ＝せっかく引き継いだ値を、自分で上書きして壊していた。
    #   → 目印から取れなければ、いま控えてある値をそのまま持ち越す。
    _db = _dirty_before_from_mark()
    if _db is None:
        _db = _dirty_before_kept(slug)
    _pub.write_atomic(PUSH_PENDING, json.dumps(
        {"slug": slug, "sha": sha, "stage": stage,
         "parent": parent, "at": _now(), "dirty_before": _db},
        ensure_ascii=False))


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
    r = _run_capped(["git", "log", "-1", "--format=%P%x1f%B"], cwd=BASE,
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return False
    got = (r.stdout or "").split(chr(31))
    if len(got) < 2:
        return False
    parents = got[0].split()
    return len(parents) == 1 and parents[0] == parent and slug in got[1]


# ★出す作業の時間制限★（2026-08-11・依頼155の①）
#   ロックの期限は30分。始まってしまった git commit / git push は
#   途中でロックを失っても止まらないので、期限より十分短く切る。
LOCK_SAFE_TIMEOUT = 600


def lock_still_mine(where: str) -> list:
    """★書く・出す直前に、いまもロックを持っているか確かめる★

    （2026-08-11・依頼153の②）以前は1日1機種の枠を取るときに1回見るだけで、
    そのあとの公開・コミット・pushは確かめていなかった。
    30分以上止まってロックが別の実行へ移ったあと復帰すると、
    **旧い実行がそのまま出し続けられる**。
    """
    if _LOCK_LOST:
        return [f"{where}: ロックを失っています（{str(_LOCK_LOST[0])[:100]}）"]
    ctx = os.environ.get("UCHIDOKORO_LOCK_CTX")
    if not ctx:
        return []                              # 手動実行（ロック無し）は対象外
    # ★★確認だけでなく、その場で延長する★★（2026-08-11・依頼155の①）
    #   check は「持ち主が自分か」を見るだけで**期限を延ばさない**。
    #   そのため「期限切れ寸前に確認が通り、直後に別の実行が奪う」窓が残る。
    #   heartbeat は所有者の確認と延長を一度に行うので、通った時点から
    #   30分の猶予が付き、この窓が閉じる。
    c = _run_capped(
        [sys.executable, os.path.join(BASE, "scripts", "task_lock.py"),
         "heartbeat", "--ctx", ctx], capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    if c.returncode != 0:
        _LOCK_LOST.append(f"{where} で heartbeat が非0")
        return [f"{where}: いまロックを持っていません"]
    return []


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
    ng = lock_still_mine("公開の仕上げ（コミット・push）")
    if ng:
        return ng                      # ★出さない★
    # ★目印は公開部が「途中」を消す前に作ってある★（Codex22回目）
    #   ここで作ると、公開部から戻る間に止まったときに目印が無くなる。
    ng = push_after_publish(res["slug"])
    if ng:
        return ng
    qid = res.get("pending_id")
    if qid:
        if pend is None:
            pend = _pend.load()
        if _pend.done(pend, qid):
            _pend.save(pend)
            _log(f"待ち行列から外しました: {res.get('name')}")
    return []


# ★件数の上限は置かない★（2026-08-07・運営者決定）
#   新台は導入日が決まっていて待てない。待ち行列にあるものは全部やる。
#   ★代わりに時刻で区切る★
#     このタスクは23:30に始まり、5:05に更新タスクが動く。件数無制限のまま
#     朝までかかると、更新タスクがロック待ちで当日動けなくなる（60分待って
#     SKIPPED_LOCKED）。そこで**この時刻を過ぎたら新しい機種に着手しない**。
#     いま処理中の機種は最後まで通す（途中で放り出さない）。
#     書き換え系のタスクにも同じ仕組みがある（task-budget の deadline_hhmm）。
NEW_MACHINE_DEADLINE_HHMM = "04:30"


# ★無人実行が新しい機種に着手してよい時間帯★（2026-08-11・台帳#293）
#   このタスクは23:30に始まり、04:30で新規着手を止める。
NEW_MACHINE_START_HHMM = "23:00"


def past_deadline(now=None, scheduled: bool = False) -> bool:
    """新しい機種に着手してよい時刻を過ぎたか。

    ★手動か無人かを区別する★（2026-08-11・台帳#293）
      以前は時刻だけを見て「04:30〜08:00 なら止める」としていた。
      そのため**08:00より後に遅れて起動した無人実行**は、締切を過ぎているのに
      件数の上限も無いまま処理を続けられた（上限を撤廃したのは
      「時刻で区切るから」という前提だったので、前提が崩れていた）。
      無人実行は**決まった時間帯（23:00〜04:30）の外なら常に止める**。
      手動（対話セッション）は昼間に流すので締切を効かせない。
    """
    import datetime as _dt
    t = (now or _dt.datetime.now()).strftime("%H:%M")
    if not scheduled:
        return False                      # 手動は締切なし（人が見ている）
    return not (NEW_MACHINE_START_HHMM <= t or t < NEW_MACHINE_DEADLINE_HHMM)


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
                                        x.get("queue_id") or ""))


def give_up_now(pend: dict, queue_id: str, url: str, name: str,
                problems: list) -> None:
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
        if _pend.done(pend, queue_id):
            _pend.save(pend)
            _log(f"待ち行列から出して台帳へ移しました: {name or url}")
    except Exception as e:                # noqa: BLE001
        _log(f"  ✗ 待ち行列から出せませんでした: {e}")


def _expect_maker(work: dict) -> str:
    """★最初に確かめたメーカーの表示名★（出典に合わせて選ぶ）

    （2026-08-16・依頼213の指摘5）
    控えには出典ごとに別の欄で覚えている（`dmm_maker` / `pworld_maker`）。
    どちらか片方だけを見ると、
      ・DMMの控えにP-WORLD時代の表示名をぶつけて**合っているのに止まる**
      ・DMM内で表示名が変わっても**気づけない**
    のどちらかが起きる。
    """
    if str(work.get("identity_source") or "") == "dmm":
        return str(work.get("dmm_maker") or "")
    return str(work.get("pworld_maker") or "")


def _maker_conflicts(work: dict) -> list:
    """★覚えている表示名と食い違った値★（出典に合わせて選ぶ）"""
    key = ("dmm_maker" if str(work.get("identity_source") or "") == "dmm"
           else "pworld_maker")
    return list(work.get(key + "_conflict") or [])


def _dmm_machine_id(url: str) -> str:
    """DMMの機種ページのURLなら、その機種IDを返す（違えば空）。"""
    import re as _re
    m = _re.match(r"^https?://p-town\.dmm\.com/machines/(\d{1,7})/?$",
                  str(url or "").strip())
    return m.group(1) if m else ""


def _fill_missing_dmm(work: dict) -> dict:
    """★DMMの機種ページから名前と導入日を読み直す★（2026-08-16・台帳#376）

    ★見出しから機種名を作らない★（DMMの見出しはSEOの飾りつき）。
    ここは**待ち行列が覚えている名前が、まだそのページの機種を指しているか**
    を確かめるだけにする。指していなければ使い回しの疑いとして止める。
    ★取れるのは導入日（と、あれば型式名・メーカー）★
    """
    import dmm_machine as _dm
    mid = _dmm_machine_id(work.get("identity_url", ""))
    if not mid:
        return work
    try:
        got = _dm.fetch(mid)
    except _dm.MachineError as e:
        _log(f"  機種ページを見直せませんでした: {str(e)[:110]}")
        return work
    # ★名前が今もこのページの機種を指しているか★
    name = str(work.get("name") or "")
    if name:
        ok, why = _dm.name_matches(got["heading"], name)
        if not ok:
            # ★ここで名前を書き換えない★（見出しは飾りつきで機種名ではない）
            #   指していないなら、使い回しか別機種。翌晩やり直すのではなく
            #   止めて人・2AIに見てもらう（黙って別機種の記事を作らないため）。
            work["_name_conflict"] = got["heading"][:60]
            _log(f"  ★待ち行列の名前が機種ページと合いません: {why[:110]}★")
            return work
    if got.get("release_date"):
        work["release"] = got["release_date"][:7]   # 待ち行列は年月まで
    if got.get("maker") and not work.get("dmm_maker"):
        work["dmm_maker"] = got["maker"]
    # ★★名簿に足された社を、待ち行列にも効かせる★★（2026-08-17）
    #   ★穴だったところ★＝控えに入った時点で名簿に無かった社は
    #   `maker` が空のまま固定され、**あとで名簿へ足しても直らなかった**。
    #   毎晩「名前かメーカーが取れない」で飛ばされ、その機種は永久に公開
    #   できない（実例: LBトリプルクラウンX-300／清龍ジャパン。
    #   名簿には同じ日に足してあったのに、待ち行列が古いままだった）。
    #   ★新しい照合の規則は作らない★＝見つけたときと同じ索引を通す。
    if not work.get("maker") and got.get("maker"):
        try:
            import dmm_discover as _dd2
            _mid = _dd2.maker_index().get(_dd2._norm(got["maker"]))
        except Exception as e:            # noqa: BLE001
            _mid = ""
            _log(f"  メーカー名簿を読めません（結び直しません）: {e}")
        if _mid:
            work["maker"] = _mid
            _log(f"  名簿に載ったのでメーカーを結びました: "
                 f"{got['maker']} → {_mid}")
    if got.get("model_code") and not work.get("dmm_model_code"):
        work["dmm_model_code"] = got["model_code"]
    return work



def fill_missing(work: dict) -> dict:
    """★毎回、公式ページを見直して名前と年月を最新にする★

    ★空を埋めるだけにしない★（2026-08-02・Codex38回目）
      一時的なエラー画面の題（「ページが見つかりません」等）が名前として
      待ち行列に固定されると、復旧後も `or` のせいで直らず、
      「公式ページと名前が一致しません」（永久理由）で機種を失っていた。
      **読めた時は必ず公式の現在値で置き換える**（読めなければ従来値のまま）。
      こちらで作らないのは従来どおり（公式に無ければ空のまま）。
    """
    # ★P-WORLDはP-WORLDの読み方で★（2026-08-13・夜間タスクが検出）
    #   メーカー公式用の読み方は「ページの題＝機種名」とみなす。
    #   P-WORLDの題には宣伝用の語が並ぶので、名前が変わったように見え、
    #   **使い回しの疑いで全件が落ちた**（2026-08-12の夜に実際に発生）。
    # ★DMMの機種ページ以外は、通信する前に断る★（依頼217の指摘2）
    _ng = identity_url_problem(work.get("identity_url", ""))
    if _ng:
        _log(f"  同定に使えないURLなので見直しません: {_ng[:110]}")
        return work
    # ★同定の正はDMM★（2026-08-16・台帳#376）。P-WORLDへは通信できない。
    if _dmm_machine_id(work.get("identity_url", "")):
        return _fill_missing_dmm(work)
    try:
        c = _nw.classify(work["identity_url"], None)
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
            # ★規格の印（L/S）が入れ替わったら、芯が同じでも別機種★
            #   （2026-08-02・Codex42回目。芯はL/Sを落とすので、
            #     L北斗の拳→S北斗の拳の使い回しに追随して公開できた）
            _og = _mc._gen_mark(work["name"])
            _ng = _mc._gen_mark(c["official_name"])
            if _og and _ng and _og != _ng:
                work["_name_conflict"] = c["official_name"]
                _log(f"  ★規格の印が変わっています（使い回しの疑い）: "
                     f"{work['name'][:30]} → {c['official_name'][:30]}★")
                return work
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


# ★通信の打ち切り時間★（2026-08-25・Codexの25回目）
#   ★通信に時間制限が無いと、固まったときに例外も戻り値も出ず、
#     終了の記録すら残らない★＝ロックが延びて朝の更新タスクまで止まる。
NET_TIMEOUT = 120


def push_after_publish(slug: str, already_committed: bool = False) -> list:
    """★公開したら関所を通してpushする★（2026-07-31・Codex16回目）

    手元に置いたままにすると、翌日の実行が「許していない変更がある」で止まる。
    **確かめる → コミット対象を選ぶ → コミット → もう一度確かめる → push**
    の順で、1つでも引っかかったら出さない。

    ★★出す手前でロックを確かめる★★（2026-08-11・依頼154の②）
      以前は「枠を取るとき」と「公開の仕上げ」でしか見ていなかったので、
      **未完了公開の再開経路（retry_push_first）が素通り**していた。
      さらに、確かめた直後に長く止まると、所有権が移ったあとに
      コミットやpushができた。入口・commit直前・push直前の3か所で見る。
    """
    ng = lock_still_mine("公開のpush（入口）")
    if ng:
        return ng
    gate = os.path.join(BASE, "scripts", "prepush_gate.py")

    def _run(*args):
        # ★PYTHONIOENCODING が必須★（2026-08-01・実際にpushまで通して見つけた）
        #   これが無いと、関所が「✗」を含む理由を印字しようとした瞬間に
        #   文字コードの失敗で落ち、**止まった本当の理由が化けて失われていた**。
        #   （同じ対策がこのファイルの他の subprocess には入っていた）
        return _run_capped([sys.executable, gate, "--slug", slug, *args],
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
        # ★コミット文にも本当の区分を書く★（2026-08-05。
        #   「先行記事・status: preview」は新台経路では**もう使っていない**表現で、
        #   実際の成果物（判定書つき・statusなし）と食い違っていた）
        msg = (f"feat(machines): 新台 {slug} を追加（{_machine_class(slug)}）\n\n"
               "出典2件で一致した項目だけを載せています（DMM単独確認の値は記事に明記）。"
               "検索に載せるかは判定書（PageDecision v1）が決めます。\n\n"
               "Co-Authored-By: Claude <自動タスク> <noreply@anthropic.com>\n")
        ng = lock_still_mine("コミットの直前")
        if ng:
            return ng
        c = subprocess.run(["git", "commit", "-m", msg], cwd=BASE,
                           timeout=LOCK_SAFE_TIMEOUT,
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
    # ★★時間制限を必ず付ける★★（2026-08-25・Codexの25回目）
    #   `git push` には前から付いているのに、ここだけ無かった。
    #   認証補助が固まると**理由も残さずに止まり続ける**。
    try:
        lr = subprocess.run(
            ["git", "ls-remote", sc["remote"], f"refs/heads/{sc['dest']}"],
            cwd=BASE, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=NET_TIMEOUT)
    except subprocess.TimeoutExpired:
        return [f"push先の先端を確かめられませんでした"
                f"（{NET_TIMEOUT}秒で打ち切り・pushしていません）"]
    remote_sha = (lr.stdout or "").split()[0] if (lr.stdout or "").split() else ""
    base_sha = _run_capped(
        ["git", "rev-parse", sc["base"]], cwd=BASE, capture_output=True,
        text=True, encoding="utf-8", errors="replace").stdout.strip()
    if lr.returncode != 0 or not remote_sha:
        return ["push先の先端を確かめられませんでした（pushしていません）: "
                + _hide((lr.stderr or "").strip())[:200]]
    if remote_sha != base_sha:
        # ★★取り直せたかどうかを、そのまま伝える★★
        #   （2026-08-25・Codexの26回目）
        #   ★直す前は例外を捨てて、必ず「fetchした」と返していた★ので、
        #   時間切れでも認証失敗でも**取り直せたことになっていた**。
        #   ＝翌日「新しい基準で確かめ直す」前提が崩れ、同じ所で止まり続ける。
        _fetched, _why_f = True, ""
        try:
            _fr = subprocess.run(["git", "fetch", sc["remote"]], cwd=BASE,
                                 capture_output=True, text=True,
                                 encoding="utf-8", errors="replace",
                                 timeout=NET_TIMEOUT)
            if _fr.returncode != 0:
                _fetched = False
                _why_f = _hide((_fr.stderr or "").strip())[:160]
        except subprocess.TimeoutExpired:
            _fetched, _why_f = False, f"{NET_TIMEOUT}秒で打ち切り"
        _tail = ("fetchしたので、次の実行で確かめ直します（pushしていません）"
                 if _fetched
                 else f"★fetchできませんでした（{_why_f}）★"
                      "／次の実行も同じ所で止まります（pushしていません）")
        return [f"push先の先端（{remote_sha[:12]}）が手元の基準"
                f"（{base_sha[:12]}）と違います。" + _tail]
    # ★基準が今のHEADの祖先であることも確かめる★（早送り以外は出さない）
    anc = _run_capped(["git", "merge-base", "--is-ancestor", base_sha, "HEAD"],
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
    ng = lock_still_mine("pushの直前")
    if ng:
        return ng
    p = subprocess.run(
        ["git", "push",
         f"--force-with-lease=refs/heads/{sc['dest']}:{base_sha}",
         sc["remote"], f"{checked_sha}:refs/heads/{sc['dest']}"],
        cwd=BASE, timeout=LOCK_SAFE_TIMEOUT, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    if p.returncode == 0:
        _clear_push_pending()
    if p.returncode != 0:
        return ["pushできませんでした: "
                + _hide((p.stdout or p.stderr or "").strip())[:300]]
    _log(f"pushしました: {slug}")
    return []


# ★記事を作れるだけの材料があるか★（2026-08-12・依頼160のP1-6で関数にした）
#   ここは run_one の中に埋まっていて、試験は**本文に文字列があるか**しか
#   見られなかった。項目を1つ増やしても試験は通り、実際に数えているかは
#   確かめられない。★数える場所を関数にして、試験は実際に呼ぶ★
# ★ゲームの流れも「記事の中身」に数える★（2026-08-13・台帳#344）
#   2AIが2出典一致で決めた流れを材料に足すところまで通っていたのに、
#   ここで数えていなかったので「採用できた材料がありません」で止まっていた
#   （実際にモグモグ風林火山で発生）。導入前は流れが先に出るので、
#   数えないと**いちばん鮮度が価値になる時期に記事が作れない**。
MODULE_FIELDS = ("ceilings", "at_specs", "czs", "resets", "gameplays")


def usable_material(mat: dict) -> dict:
    """材料のうち、記事の中身になるものだけを返す。

    ★型式名だけでは「材料あり」と数えない★（2026-08-02・Codex29回目）
      型式名は identity の正本として adopted に入るが、
      それしか無い記事（スペックも天井も無い）を作ってはいけない。
    ★天井・AT・CZ・リセットの採用分も数える★（Codex57回目／依頼160のP1-6）
      基本スペック直下しか見ておらず、天井などが2媒体一致していても
      「材料なし」で記事を永久に作れなかった。

    ★★読者に出る項目だけを数える★★（2026-08-29・台帳#497／自分で再現した）
      ★直す前は「型式名」1つだけを名簿で除いていた★ので、
      同じく読者に出ない「型」（machine_profile）が数に入っていた。
      ＝他に何も採れていない機種で型だけを確定させると、
        機種名と登場時期だけ・7つの箱すべて「未確認です」という
        ★読者に届く事実がゼロのページ★が公開されていた。
      ★名簿に足すだけにしない★＝次に読者非表示の項目が増えたら同じことが起きる。
      「読者に出る項目か」の正本は `confirmed_values.topic_of` なので、そこを見る。
      ★まとめて除外してはいけない★＝`ceiling_state = NONE` は
      「この機種に天井はありません」という**読者向けの事実**。
      一括で外すと、天井なし機種が永久に作れなくなる。
    """
    out = {k: v for k, v in (mat.get("adopted") or {}).items()
           if _cv.topic_of(k)}
    for key in MODULE_FIELDS:
        for i, c in enumerate((mat.get(key) or {}).get("adopted") or []):
            out[f"{key}#{i}"] = c
    return out


def _ask_key(question: str) -> str:
    """★質問の見分け★（2026-08-12・依頼164）

    同じ機種でも別の質問なら別の案件にする。
    質問の文そのものは長いので、確定値の項目名を鍵にする。
    見つからなければ質問の先頭を短く切って使う。
    """
    m = re.search(r"--field\s+([A-Za-z_][A-Za-z0-9_]*)", question or "")
    if m:
        return m.group(1)
    head = re.sub(r"[^\wぁ-んァ-ヶ一-龠ー]+", "", str(question or ""))[:16]
    return head or "unknown"


def _ask_ledger(slug: str, name: str, question: str, key: str = "",
                code: str = "ASK_2AI") -> bool:
    """★2AIで決まらなかったことを台帳へ★（2026-08-12・運営者決定）

    「人が直す項目をなくす」ので、機械が決められないことは 2AI へ回す。
    それでも答えが出ないまま公開まで来たときだけ、ここで知らせる。
    ★メールを送るのは台帳のまとめ（翌朝）★＝公開処理はメールで止めない。
    """
    return _ledger(
        slug, "quality", "QUALITY", code,
        # ★質問ごとに別の案件にする★（2026-08-12・依頼164のP1）
        #   機種名だけだと、同じ機種の**別の質問**が同じ案件に合流し、
        #   片方の回数が満了しただけで新しい質問まで自動の輪から消える。
        f"{name}: 2AIで決まらなかった項目があります（{(key or _ask_key(question))}）",
        f"{question}\n\n"
        "★機械では決められない意味の判断です★\n"
        "★人が判断する案件ではありません★＝新台タスクが同じ晩のうちに、"
        "材料を変えながらやり直します。\n"
        # ★聞き方も記録先も違うので、案件ごとに書き分ける★（依頼192のP2）
        #   共通文に「confirmed_values へ記録」と書いていたため、
        #   メーカー表記の質問まで**違う置き場へ誘導**していた。
        + ("手順は新台SKILL.mdの STEP 3-B-M（メーカー表記の照合）。"
           "見るのは記事の原文ではなく、名鑑のその機種のページと"
           "当事会社の公式サイトです。決まれば "
           "maker_identity_cache.py --record へ控え、この行は閉じられます。\n"
           if code == "ASK_2AI_MAKER" else
           "（手元の出典→3つ目の出典→検索で別系統）。"
           "決まれば confirmed_values へ記録し、この行は閉じられます。\n")
        + "やり直しの上限に達したときだけ、人の出番になります"
        "（上限は open_issues.py の ASK_MAX_ATTEMPTS）。")



def field_label(k: str) -> str:
    """項目の表示名。★知らない項目でも止まらない・黙って消さない★

    （2026-08-24・Codexの4回目の指摘＝2AIだけが答える項目が
      `spec_lookup.FIELDS` に無く、**KeyError で新台追加が止まっていた**）
    ★2AIが「早見表に使う天井はどれか」に正しく答えた機種ほど
      公開できない★という状態だった（実際に再現した）。

    ★関数として外に出してある★＝試験が**本物を呼べる**ように。
    中に隠したままだと、試験が同じ式を書き写すことになり、
    ★写しを採点する試験★になってしまう（今日それを何度もやった）。
    """
    got = _sl.FIELDS.get(k)
    if got:
        return got["jp"]
    lab = _cv.AI_ONLY_LABELS.get(k)
    if lab:
        return lab
    return f"{k}（名前が未登録）"          # ★消さずに、分かる形で出す★


def run_one(name, official_url, maker, release, apply_it=False,
            release_is_cache=False,
            before_write=None, expect_maker: str = "",
            pending_id: str = "") -> dict:
    """1機種を最後まで進める。"""
    out = {"name": name, "slug": None, "wrote": [], "problems": [], "blocked": []}
    _log(f"=== 機種の処理開始: {name} / {maker} / {release} / {official_url} "
         f"/ 書き込み={'する' if apply_it else 'しない'} ===")
    # ★①まず公式ページと名前が同じ機種を指しているか★
    vo = verify_official(name, official_url, maker, release,
                         release_is_cache=release_is_cache,
                         expect_maker=expect_maker)
    out["problems"] += vo["problems"]
    # ★記事に載せるのは公式に書いてある年月★（渡された値ではない）
    release = vo["release"] or release
    # ★名前も公式（一覧カード）に書いてあるものを正とする★（Codex93回目の指摘2）
    if vo.get("identity_name") and vo["identity_name"] != name:
        _log(f"  機種名を公式の表記に合わせます: {name!r} → {vo['identity_name']!r}")
        name = vo["identity_name"]
        out["name"] = name
    # ★②その機種が既に登録されていないか★（2026-07-31・実際に二重登録できた）
    #   手順書には書いてあったが、実行器が呼んでいなかった。
    # ★名前・公式URL・型式名のどれか1つでも一致したら疑う★（2026-07-31・Codex指摘）
    #   型式名は新台では無いことが多いので、無いこと自体は警告にしない。
    # ★同定で読めた型式名も渡す★（2026-08-12・依頼166のP1）
    #   入口の切替直後は、同じ機種が旧URLとP-WORLDの2経路から入りうる。
    #   URLが違うのでURL一致では結べず、名前の書き方も揃わないことがある。
    #   ★これは「出典2件で採用した値」ではない★＝重複を見つけるためだけに使う。
    _ident_codes = []
    _ev = vo.get("identity_evidence") or {}
    for slug, ename, why in _cd.find_duplicates(
            name, official_urls=[official_url],
            model_codes=_ident_codes or None):
        out["problems"].append(
            f"既に登録されている疑い: slug={slug} name={ename}（{why}）"
            f"／新しいslugで作らず、更新タスクで直すこと")
    # ★slugを先に決めてから材料を集める★（2026-08-14・依頼189）
    #   メーカー欄が「分からない」ときに、**この機種の控え**を見るため。
    #   以前は材料集めのあとで slug を決めていたので、控えを引けなかった。
    out["slug"] = _ba.slug_from_url(official_url)
    # ★DMMで確かめた機種名・導入日を材料集めへ渡す★
    #   （2026-08-17・Codex依頼229の指摘1）メーカー欄の控えを、
    #   **この機種のもの**だと突き合わせてから使うため。
    got = gather(name, maker, slug=out["slug"],
                 machine_name=vo.get("identity_name") or name,
                 release_date=str(vo.get("release") or ""))
    out["problems"] += got["problems"]
    # ★メーカー欄で決められなかったものは、その場で2AIへ聞く★
    #   （人が名簿に足すまで待たない。運営者の方針）
    # ★メーカーの質問は別に持つ★（2026-08-14・依頼190のP1）
    #   以前は ask_2ai へ足していたが、後段の checker_questions が
    #   **同じ配列を丸ごと上書き**するため消えていた。
    #   さらに材料不足だと台帳処理の前に終わるので、★ここで台帳へ入れる★。
    out["maker_questions"] = list(got.get("maker_questions") or [])
    # ★例外的なメーカー関係を根拠に採否した事実を残す★（依頼226のCodex指摘）
    #   材料が足りずに早く終わるときも残す＝あとから由来を確かめられる。
    out["maker_relation_checks"] = list(
        got.get("maker_relation_checks") or [])
    # ★判断記録は「結果が出てから」書く★（2026-08-17・Codex依頼229の厚み）
    #   前はここで書いていたので、後段で転載照合に失敗しても・材料が採れなくても・
    #   公開が止まっても「材料に使った」と残った（記録が事実と違う）。
    #   実際に最後まで残ったURLと、記事を作れたかを入れて、出口で書く。
    def _write_relation_record(created: bool):
        rows = out.get("maker_relation_checks") or []
        if not (apply_it and rows):
            return
        alive = set(got.get("urls") or [])
        for _n in rows:
            # ★「材料に使った」ではなく「材料集めの最後まで残った」★
            #   （2026-08-17・Codex依頼230。そこから値が採用されたかは別）
            _n["eligible_at_collection_end"] = _n["url"] in alive
            _n["article_created"] = bool(created)
        p = write_maker_relation_record(out["slug"], name, rows)
        _log(f"  メーカー欄の採否を判断記録へ: {p or '★書けませんでした★'}")
    for _q in out["maker_questions"]:
        _log(f"  ★2AIに聞くこと（メーカー）: {_q['text'][:120]}")
        # ★メーカー表記の質問だけ見分けられるようにする★（依頼190）
        #   聞き方が違う（記事の原文ではなく名鑑と公式の会社情報を読む）ので、
        #   手順書の STEP 3-B-M へ確実に振り分けるため。
        if apply_it and not _ask_ledger(out["slug"], name, _q["text"],
                                        key=_q.get("key"),
                                        code="ASK_2AI_MAKER"):
            out["problems"].append(
                f"メーカーの質問を台帳に載せられません: {_q.get('key')}")
    # ★公式が年月を出さない機種は、名鑑2票一致の月で先行記事にする★
    #   （2026-08-02・Codex47回目に条件つきで承認。山佐は導入年月が画像のみ）
    #   条件＝型式が一致した同じ2名鑑の月が一致（gatherが判定済み）。
    #   公式に年月がある時はそちらが正（この控えは使わない）。
    if got.get("directory_release") and not release \
            and any("登場年月が書かれていません" in p for p in out["problems"]):
        release = got["directory_release"]
        out["problems"] = [p for p in out["problems"]
                           if "登場年月が書かれていません" not in p]
        if not _nw.is_recent(release):
            out["problems"].append(f"登場年月が新台の範囲外です（{release}）")
        else:
            _log(f"  登場年月は名鑑2票一致の月を使用（先行記事）: {release}")
    # ★型式名でも重複を見る★（2026-07-31・Codex16回目）
    #   最初の重複検査は名前と公式URLしか渡していなかった。
    #   型式名は材料を集めて初めて分かるので、**分かった時点でもう一度見る**。
    #   「名前も公式URLも違うが、実は同じ型式」＝同じ機種を二重に作る経路だった。
    # ★観測値で見る★（2026-08-09・依頼130 P1-2。1出典しか無い型式でも
    #   「同じ型式の機種がすでにある」なら二重に作ってはいけない）
    if got.get("observed_model_code"):
        for slug, ename, why in _cd.find_duplicates(
                name, model_codes=[got["observed_model_code"]]):
            out["problems"].append(
                f"既に登録されている疑い: slug={slug} name={ename}"
                f"（型式名が同じ: {got['observed_model_code']} / {why}）"
                f"／新しいslugで作らず、更新タスクで直すこと")
    if not got["material"]:
        out["blocked"] = _blocking(out["problems"])
        # ★材料が足りずに早く終わるときも記録を残す★
        #   （2026-08-17・Codex依頼231。ここだけ書き忘れていた）
        _write_relation_record(created=False)
        if apply_it:
            _remember(name, official_url, maker, release, out["problems"])
        else:
            _log("（下見）待ち行列には触りません")
        return out
    mat = got["material"]
    # ★2AIで突き合わせて確定した値を材料に足す★（2026-08-09・台帳#273）
    #   機械の抽出は「載っているのに読めない」が普通に起きる（実測: パリピ孔明は
    #   名鑑4件すべてに天井の記述があるのに4件とも採れなかった）。
    #   手順書には2AI突き合わせ（STEP 3-B）があるのに、**確定した値を
    #   受け取る場所が無かった**ので、読めない機種は永久に空のままだった。
    #   ★機械が採れている項目は上書きしない★／記録できるのは対話セッションだけ。
    try:
        _added = _cv.merge_into(mat, out["slug"])
        if _added:
            _log("  2AIで確定した値を材料に足しました: " + " / ".join(_added))
            # ★★公開直前に、その機種の控えだけ取り直して確かめる★★
            #   （2026-08-24・Codexの8回目）
            #   ★控えの読み直しは、保存されたURLと引用を信じている★ので、
            #   控えを手で書き換えられたら偽の引用でも通る。
            #   ★全件はやらない★＝いま書こうとしている機種だけ・出典は各1回。
            # ★確かめ済みの名前とURLを渡す★（2026-08-24・Codexの9回目）
            #   まだ一覧に無い新台は、控えだけでは機種名を引けない。
            _rv = _cv.reverify(out["slug"], name=name,
                               official_url=official_url)
            if _rv:
                out["problems"] += [
                    f"CONFIRMED_VALUES_UNREADABLE: 控えを確かめ直せません: {x}"
                    for x in _rv]
                _log("  ★控えの再確認で問題★: " + " / ".join(_rv[:3]))
    except Exception as e:                # noqa: BLE001
        # ★読めないことを黙って「無い」にしない★
        # ★★読めないときは止める★★（2026-08-24・Codexの6回目）
        #   ★直す前は「問題」に足すだけで、停止条件に入っていなかった★ので、
        #   控えが読めなくても**機械が採れた分だけで記事を作って公開**していた。
        #   ＝2AIが確定させた値が抜けた記事が、黙って世に出る。
        out["problems"].append(
            f"CONFIRMED_VALUES_UNREADABLE: 2AIの確定値を読めません:"
            f" {type(e).__name__}: {e}")
    # ★★「採れていない設定」を数え直す★★（2026-08-28・本番で誤記）
    #   ★2AIで確定した値は、ここまでで初めて材料に入る★ので、
    #   集めた時点の一覧のままでは「載せているのに載せていない」と
    #   報告してしまう（実際に読者向けの注記まで嘘になっていた）。
    #   ★try の外に置く★＝控えが読めなかった晩でも報告は出す。
    mat["setting_labels_unconfirmed"] = _sl.unconfirmed_labels(
        mat.get("setting_labels_seen"), mat.get("adopted"))
    for _lb in mat["setting_labels_unconfirmed"]:
        out["problems"].append(
            f"設定{_lb}: 出典に出てくるが値が確認できていません"
            "（設定の段数を誤る恐れ）")
    out["adopted"] = sorted(field_label(k) for k in mat["adopted"])
    out["held"] = sorted(field_label(k) for k in mat["need_third"])
    out["thin"] = sorted(field_label(k) for k in mat["thin"])
    # ★型式名だけでは「材料あり」と数えない★（2026-08-02・Codex29回目の副作用対策）
    #   型式名は identity の正本として adopted に入れるが、
    #   それしか無い記事（スペックも天井も無い）を作ってはいけない。
    # ★天井・AT・CZの採用分も材料に数える★（2026-08-03・Codex57回目。
    #   基本スペック直下しか見ておらず、天井などが2媒体一致していても
    #   「材料なし」で記事を永久に作れなかった）
    # ★機械が決められないことは、質問として持ち回る★（2026-08-12・運営者決定）
    #   黙って空にすると誰も気づかないまま、その欄は永久に埋まらない。
    #   ①ここで質問を出す ②2AIが答えて confirmed_values へ記録する
    #   ③公開まで答えが出なければ台帳へ＝翌朝のまとめメールで知らせる
    out["ask_2ai"] = _ba.checker_questions(mat)
    for q in out["ask_2ai"]:
        _log(f"  ★2AIに聞くこと: {q[:160]}")
    usable_mat = usable_material(mat)
    if not usable_mat:
        out["problems"].append("採用できた材料がありません（記事を作りません）")
    # ★②同定に関わる問題があれば、材料が採れていても作らない★
    out["blocked"] = _blocking(out["problems"])
    if out["blocked"] or not usable_mat:
        _write_relation_record(created=False)
        for b in out["blocked"]:
            _log(f"  ★止めました: {b[:140]}")
        if apply_it:
            _remember(name, official_url, maker, release, out["problems"])
        else:
            _log("（下見）待ち行列には触りません")
        _log(f"=== 機種の処理終了（作らず）: {name} ===")
        return out
    # ★2AIの答えが出ないまま公開まで来たらメールで知らせる★
    #   （2026-08-12・運営者決定「困ったら2AI、それでも無理ならメール」）
    #   ★公開より先に載せる★（2026-08-12・依頼163の2）＝公開の途中で落ちても
    #   質問が残る。台帳は同じ題を重ねないので、毎晩鳴ることはない。
    if apply_it:
        for q in out.get("ask_2ai") or []:
            if not _ask_ledger(out["slug"], name, q):
                # ★載せられなくても公開は止めない★が、黙って消さない
                out["problems"].append(f"2AIへの質問を台帳に載せられません: {q[:80]}")
    machine = _ba.build_machine(out["slug"], name, maker, official_url, release, mat)
    detail = _ba.build_detail(out["slug"], name, release, mat)
    out["preview"] = {"machine": machine, "detail": detail,
                      # ★下見と本番で出るものを揃える★（Codex93回目の指摘9）
                      "identity_binding": vo.get("identity_binding")
                      or "OFFICIAL_PRODUCT_PAGE",
                      "identity_evidence_ref": _evidence_ref(vo)}
    if apply_it:
        # ★公開は専用の経路だけ★（2026-07-31・Codexと相談した案B）
        #   ページを先に置き、最後に一覧へ足す。既存ページは1枚も触らない。
        # ★枠を使うのは公開部の中、最初の書き込みの直前★（Codex20回目）
        #   ここで使うと、途中公開・監査・早見表のずれで断られたときにも
        #   その日の枠が消えていた。
        res = _pub.publish_from_material(
            out["slug"], name, maker, official_url, release, mat,
            apply_it=True, before_write=before_write,
            # ★どの公式ページで本人性を確かめたかを残す★（台帳#209）
            identity_binding=vo.get("identity_binding")
            or "OFFICIAL_PRODUCT_PAGE",
            identity_evidence_ref=_evidence_ref(vo),
            # ★公開部が「途中」の目印を消す前に引き継ぐ★（Codex22回目）
            #   あとから作ると、その間に止まったときに目印がどこにも無くなる。
            on_written=lambda sl: _mark_push_pending(sl, "", "WRITTEN"))
        out["wrote"] = res["wrote"]
        out["problems"] += res["problems"]
        if res["problems"]:
            out["blocked"] = res["problems"]
            _write_relation_record(created=False)
            return out
        _log(f"公開しました: {out['slug']} / 書いたファイル{len(out['wrote'])}件 "
             + " ".join(os.path.relpath(w, BASE).replace(os.sep, "/")
                        for w in out["wrote"]))
        # ★待ち行列から外すのは push が通ってから★（2026-07-31・Codex17回目）
        #   ここで外すと、関所やpushで止まったとき
        #   「待ち行列にも無い・手元だけ変わっている」状態になり、
        #   翌日の実行が残骸で止まって、誰も気づかないまま進まなくなる。
        out["pending_id"] = pending_id
    _write_relation_record(created=bool(out.get("wrote")))
    _log(f"=== 機種の処理終了: {name} / 止めた理由{len(out['blocked'])}件 "
         f"/ 問題{len(out['problems'])}件 ===")
    return out


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    import inspect
    results = []
    nl = chr(10)

    # ★★試験は本番の控えを読まない★★（2026-08-22・実際に落ちて分かった）
    #   ★直す前に起きたこと★＝
    #     maker_identity_cache.load() は**控え全件**を検査する。
    #     試験の中ではメーカー名簿や出典まわりを偽物へ差し替えているので、
    #     ★本番に実在する控えがその偽物に引っかかって例外になり★、
    #     まったく関係のない試験（L試験機の下見）が KeyError で落ちた。
    #     実際の文言＝「期待 kyoraku ／名鑑「京楽」→ （不明）」
    #     ＝偽の名簿では京楽を引けないだけで、本番では通っている控え。
    #   ＝★試験の結果が本番のデータ次第で変わる★という重い欠陥。
    #     dmm_5073 の控えを1件足しただけで CI 再現が赤くなった。
    #   ★空の置き場を指す★＝読んでも書いても本番に触らない。
    import tempfile as _tf_mic
    _mic_keep_store = _mic.STORE
    _mic.STORE = os.path.join(_tf_mic.mkdtemp(prefix='uchi_mic_'),
                              'maker_identity_cache.json')

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    real_find, real_read, real_lookup = _di.find, _sl.read_page, _mc.lookup
    # ★★試験でも材料のページは「器」で取る★★（2026-08-17・台帳#393）
    #   本番は材料集めの前に1回だけ取りに行く。偽物がそこを満たさないと
    #   「取れない／転送される」と判定されて全部落ちる。
    #   ★到達先は要求元と同じにする★（転送なしのページを模す）
    real_fetch = _fp.fetch
    _fp.fetch = lambda u, purpose="claim_material", get=None: _fp.FetchedPage(
        u, u, "<title>L試験機 スロット 新台 解析 | ちょんぼりすた</title>"
           "<div>機種名 L試験機</div>")

    def _MKC(k):
        """★偽の名鑑ページにも、本番の約束を守らせる★（2026-08-17・依頼230）

        本番の `lookup()` は、メーカーを期待して呼ばれたら**必ず**
        メーカー欄の判定（maker_check）を返す。偽物がそれを返さないと、
        ★関門を通らない形の偽物で関門を試す★ことになる（同じ失敗を3回した）。
        """
        m = k.get("expected_maker")
        if not m:
            return {}
        return {"maker_check": {"state": "MATCH", "seen": m, "expected": m,
                                "owners": [m]}}
    # ★転載照合は試験では常に成功扱い★（2026-08-02・Codex53回目の変更で
    #   取得失敗が「そのページを票から外す」ようになり、架空URLの試験が
    #   全部外されてしまうため。lineage_check 自体の挙動は同スクリプトの
    #   自己テストで確かめている）
    real_lc = _lc.check
    _lc.check = lambda urls: {"suspects": [], "checked": [],
                              "problems": [], "failed": []}
    # ★試験が本番の待ち行列を触らないようにする★（2026-07-31・実際に架空機種が入った）
    real_store = _pend.STORE
    _tmpdir = __import__("tempfile").mkdtemp(prefix="uchi_pend_")
    _pend.STORE = os.path.join(_tmpdir, "pending.json")
    # ★試験は本番の未pushの目印にも書かない★（2026-08-11・実際に汚した）
    #   push_after_publish は本物の目印を書くので、試験の slug が残ると
    #   **次の --apply が未完了公開として処理し、後続を止める**。
    real_mark = globals()["PUSH_PENDING"]
    globals()["PUSH_PENDING"] = os.path.join(_tmpdir, ".push-pending.json")
    # ★試験は本番の台帳にも書かない★（2026-08-11・依頼157のP1）
    #   局所で偽物に差し替えていたが、分類の回帰や待ち行列の保存失敗など
    #   別の経路から本物の _ledger が呼ばれうる。全体で差し替える。
    real_ledger = globals()["_ledger"]
    _ledger_calls: list = []
    globals()["_ledger"] = (
        lambda *a, **k: _ledger_calls.append((a, k)) or True)
    # ★試験は本番の日次ログにも書かない★（2026-08-01・実際に混入した）
    #   混入すると完了マーカーが末尾から離れ、番兵（task-watchdog）が
    #   「起動したが完走していない」と誤検知しうる。画面出力だけにする。
    real_log = globals()["_log"]
    globals()["_log"] = lambda m: print(f"[selftest-log] {m}")
    try:
        _di.find = lambda n, c=None: {"results": {
            "a": {"state": "FOUND", "url": "https://chonborista.com/1", "why": "",
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

        # ★架空ホストは票に数えられない★（2026-08-09・登録されていない発行者は
        #   default deny にしたため、実在の発行者で試す）
        _di.find = lambda n, c=None: {"results": {
            k: {"state": "FOUND", "url": f"https://{h}/1", "why": "",
                "candidates": [], "surfaces": "1/1", "index_size": 9, "problems": []}
            for k, h in (("a", "chonborista.com"), ("b", "nana-press.com"))}}
        _mc.lookup = lambda u, n, **k: {"url": u, "identity_ok": True, "model_code": "L1", "reason": "OK", **_MKC(k)}
        _sl.read_page = lambda u, n, **k: {
            "url": u, "host": u.split("/")[2], "ok": True, "reason": "OK",
            "fields": {"payout_rate": {"1": "97.3%"}}}
        g2 = gather("L試験機")
        t("　2件そろえば型式名と材料を集める",
          g2["model_code"] == "L1" and g2["material"] is not None)

        # ★★★gather() を通して採否を確かめる★★★
        #   （2026-08-17・Codex依頼229。判定関数だけの試験では、
        #     「状態ではなく理由の文で決めている」という隣の契約の壊れ方を
        #     検知できなかった。★理由の文をわざと本番と違う言い回しにして★
        #     採否が状態で決まっていることを見る）
        _real_vf = _mic.verdict_for

        def _g_urls(state, cached=None, identity_ok=True):
            _mc.lookup = lambda u, n, **k: {
                "url": u, "identity_ok": identity_ok, "model_code": "L1",
                "reason": "（この文言は採否に関係しません）",
                "maker_check": {"state": state, "seen": "平和",
                                "expected": "olympia_estate",
                                "owners": ["heiwa"]}}
            _mic.verdict_for = lambda *a, **k: cached
            try:
                return len(gather("L試験機", "olympia_estate", slug="dmm_5086",
                                  machine_name="L試験機",
                                  release_date="2026-10-05")["urls"])
            finally:
                _mic.verdict_for = _real_vf

        t("★★名簿で一致した名鑑は、gatherを通しても残る★★",
          _g_urls("MATCH") == 2)
        t("★★★どの社か分からないものは、控えで『使う』と決めてあっても"
          "gatherを通して外れる★★★（理由の文に頼らず状態で決める）",
          _g_urls("UNKNOWN", cached="ACCEPT_MATERIAL") == 0)
        t("★★関係のある社は、控えで『使う』と決めてあれば残る★★",
          _g_urls("RELATED", cached="ACCEPT_MATERIAL") == 2)
        t("★★関係のある社でも、控えが無ければ外れる★★",
          _g_urls("RELATED", cached=None) == 0)
        t("★★控えで『使わない』と決めてあれば外れる★★",
          _g_urls("RELATED", cached="REJECT_MATERIAL") == 0)
        t("★★明らかに別の社は外れる★★", _g_urls("MISMATCH") == 0)
        t("　同定に落ちたページも外れる",
          _g_urls("MATCH", identity_ok=False) == 0)
        # ★★★2026-08-17・Codex依頼230の指摘1★★★
        #   控えを見るのが RELATED のときだけだったので、名簿を直して
        #   同じ表記が MATCH になった瞬間に、「使わない」と決めたページが
        #   材料へ戻れた。★決めてある＝必ず除外★という契約と逆だった。
        t("★★★控えで『使わない』と決めたページは、名簿で一致に変わっても"
          "戻らない★★★（前は MATCH になった瞬間に材料へ復活できた）",
          _g_urls("MATCH", cached="REJECT_MATERIAL") == 0)
        t("　どの社か分からない側でも、控えの『使わない』は効く",
          _g_urls("UNKNOWN", cached="REJECT_MATERIAL") == 0)

        # ★★★2026-08-17・Codex依頼231の指摘1★★★
        #   材料の一覧（urls）だけ削って、型式名と登場年月を数える looks を
        #   削っていなかった。★正常2件＋外した1件★を混ぜて、
        #   外したページの型式名が採用されないところまで見る。
        def _g_mixed():
            _di.find = lambda n, c=None: {"results": {
                k: {"state": "FOUND", "url": f"https://{h}/1", "why": "",
                    "candidates": [], "surfaces": "1/1", "index_size": 9,
                    "problems": []}
                for k, h in (("a", "chonborista.com"), ("b", "nana-press.com"),
                             ("c", "p-town.dmm.com"))}}
            # 3件目（DMM）だけ、メーカー欄の表記が違い、控えで
            # 「使わない」と決めてあることにする。
            # ★MATCH 側は対象URLを渡さない呼び方★（通信しないため）なので、
            #   控えは (機種・期待する社・表記) で引かれる。表記で分ける。
            # ★型式名を持つのは、外すページだけにする★
            #   （両方が持っていると、正常な2票で採用されてしまい、
            #     消し漏らしがあっても試験が通ってしまう）
            _mc.lookup = lambda u, n, **k: {
                "url": u, "identity_ok": True, "reason": "OK",
                "model_code": "L9" if "p-town" in u else None,
                "release_hint": "2029-12" if "p-town" in u else "2026-11",
                "maker_check": {
                    "state": "MATCH",
                    "seen": "べつ表記" if "p-town" in u else "平和",
                    "expected": "olympia_estate", "owners": ["heiwa"]}}
            _mic.verdict_for = (lambda slug, ex, se, st=None, fe=None, **k:
                                "REJECT_MATERIAL" if se == "べつ表記" else None)
            try:
                return gather("L試験機", "olympia_estate", slug="dmm_5086",
                              machine_name="L試験機",
                              release_date="2026-10-05")
            finally:
                _mic.verdict_for = _real_vf

        _gm = _g_mixed()
        t("★★★控えで『使わない』と決めたページは、型式名の票からも外れる★★★"
          "（前は材料からだけ外れ、型式名は独立票に残って公開物へ出得た）",
          _gm["model_code"] is None
          and not _gm.get("observed_model_code")
          and all("p-town.dmm.com" not in u
                  for u in (_gm.get("observed_model_hosts") or []))
          and all("p-town.dmm.com" not in u for u in _gm["urls"]))
        t("　（対照）正常な2件はそのまま残る", len(_gm["urls"]) == 2)

        # ★★★2026-08-17・Codex依頼232の指摘★★★
        #   控えを読めないと「使わないと決めたページ」があるかも分からない。
        #   ★前は MATCH のページがそのまま材料にも票にも残っていた★
        #   （コメントには fail-closed と書いてあった）。
        _dec_ng = maker_material_decision(
            [{"url": "https://chonborista.com/1", "identity_ok": True,
              "reason": "OK",
              "maker_check": {"state": "MATCH", "seen": "平和",
                              "expected": "olympia_estate",
                              "owners": ["heiwa"]}}],
            "dmm_5086", "olympia_estate", cache=None, cache_ok=False)
        t("★★★控えを読めないときは、どのページも材料に使わない★★★"
          "（読めない＝『使わないと決めた分』が分からない・fail-closed）",
          _dec_ng["bad"] == {"https://chonborista.com/1"}
          and _dec_ng["accepted"] == set()
          and _dec_ng.get("cache_unreadable") is True)

        # ★★★題が略称のときの経路★★★（2026-08-17・台帳#390）
        _TU = "https://chonborista.com/slot/orinpia-slot/264134/"

        def _title_dec(cached=None, name_in_body=True, url=_TU,
                       observed_maker="京楽"):
            # ★本番と同じ形の返り値にする★＝題の不一致では maker_check が
            #   作られず、メーカー欄は observed_maker として返る（依頼234）
            looks = [{"url": url, "identity_ok": False,
                      "reason": "NAME_CORE_MISMATCH",
                      "name_in_body": name_in_body,
                      "observed_maker": observed_maker}]
            return maker_material_decision(
                looks, "dmm_5073", "kyoraku",
                verdict_of=lambda e, s, u, prof="": cached,
                machine_name="L試験機", release_date="2026-11-02")

        t("★★題が略称で落ちたページは、控えがあれば材料に戻る★★",
          _TU in _title_dec(cached="ACCEPT_MATERIAL")["accepted"])
        t("★★控えが無ければ材料に使わず、2AIへ問いを出す★★"
          "（★機械が『本文に正式名があるから本人だ』とは決めない★）",
          _title_dec()["accepted"] == set()
          and len(_title_dec()["questions"]) == 1
          and _TU in _title_dec()["bad"])
        t("★★★足切り＝本文にDMMの正式名が無いものは2AIへ回さない★★★"
          "（毎晩ぜんぶ回すと、本当に別機種のページも掛かり続ける）",
          _title_dec(name_in_body=False)["questions"] == []
          and _TU in _title_dec(name_in_body=False)["bad"])
        t("★★その名鑑の機種ページの形でないURLは回さない★★",
          _title_dec(url="https://chonborista.com/slot/orinpia-slot/")
          ["questions"] == [])
        t("　控えで『使わない』と決めてあれば、問いも出さない",
          _title_dec(cached="REJECT_MATERIAL")["questions"] == []
          and _title_dec(cached="REJECT_MATERIAL")["accepted"] == set())
        t("★★メーカー欄を読めないページは2AIへ回さない★★"
          "（読めない＝この型の前提が確かめられない）",
          _title_dec(observed_maker="")["questions"] == []
          and _title_dec(observed_maker="")["accepted"] == set())

        # ★★★一続きで試す★★★（2026-08-17・Codex依頼234の恒久対応5）
        #   控えを作る → 採否で許可証になる → ★4つの読取器が通す★ まで。
        #   ★本物の略称の題を使う★（普通の題で試すと関門を通らない＝5回やった失敗）
        _MN2 = "L転生王女と天才令嬢の魔法革命"
        _NICK2 = ("<title>【ガンゲイル(スマスロ)】解析情報まとめ 天井</title>"
                  '<a class="rating-btn">みんなの評価 (平均0)</a>'
                  '<div id="hyouka">星</div>'
                  '<ul class="commentlist"><li>投稿</li></ul>'
                  f'<div id="entry"><div>機種名 {_MN2}</div>'
                  "<div>メーカー 京楽</div>"
                  "<div>導入日 2026年10月5日</div></div>")
        _TU2 = "https://chonborista.com/slot/orinpia-slot/264134/"

        def _f2(u):
            _nw.LAST_FINAL_URL["url"] = u
            return _NICK2

        _st2 = _mic._empty()
        _made = True
        try:
            _mic.remember(
                "dmm_e2e", "kyoraku", "京楽", "ACCEPT_MATERIAL", "理由",
                ["claude", "codex"],
                [{"url": _TU2,
                  "quote": f"機種名 {_MN2} メーカー 京楽 導入日 2026年10月5日",
                  "kind": "directory_observation"}],
                "2026-08-17", machine_name=_MN2, release_date="2026-10-05",
                target_url=_TU2, proof_profile="title_name_core_mismatch",
                store=_st2, fetch=_f2)
        except _mic.CacheError:
            _made = False
        _v2 = _mic.verdict_for("dmm_e2e", "kyoraku", "京楽", _st2, _f2,
                               material_url=_TU2, machine_name=_MN2,
                               release_date="2026-10-05",
                               want_profile="title_name_core_mismatch")
        # ★★4つの読取器を実際に呼ぶ★★（2026-08-17・Codex依頼235の厚みの指摘）
        #   前は共通の関所を1回呼んでいるだけで、read_page を1つも通して
        #   いなかった（range(1) で回していた＝試験の形だけ）。
        # ★取ってきた器を作り、許可証は★その本文の指紋★で出す★
        #   （2026-08-17・台帳#393。URLではなく本文で束縛する）
        _pg2 = _fp.FetchedPage(_TU2, _TU2, _NICK2)
        _grant2 = frozenset({_pg2.sha256})
        # ★偽の読取器に差し替わったままでは意味がない★＝本物に戻して呼ぶ
        _fake_sl_read = _sl.read_page
        _sl.read_page = real_read
        try:
            _readers, _readers_why = {}, {}
            for _mod, _nm in ((_sl, "基本スペック"), (_cl, "天井"),
                              (_cz, "CZ"), (_at, "AT仕様")):
                _ng_r = _mod.read_page(_TU2, _MN2, expected_maker="kyoraku",
                                       grant=None, page=_pg2)
                _readers[_nm] = (
                    _mod.read_page(_TU2, _MN2, expected_maker="kyoraku",
                                   grant=_grant2, page=_pg2).get("ok"),
                    _ng_r.get("ok"))
                _readers_why[_nm] = _ng_r.get("reason")
            # ★★本文が1文字でも違えば、許可証は効かない★★（不変条件の核）
            _pg_mod = _fp.FetchedPage(_TU2, _TU2, _NICK2 + "<p>あとから足した</p>")
            _other_ok = _sl.read_page(_TU2, _MN2, expected_maker="kyoraku",
                                      grant=_grant2, page=_pg_mod).get("ok")
        finally:
            _sl.read_page = _fake_sl_read
        _readers_ok = all(a for a, _ in _readers.values())
        _reader_ng = any(b for _, b in _readers.values())
        t("★★★控えを作る→採否→読取器の関所、まで一続きで通る★★★"
          "（本物の略称の題で。前は控えすら作れなかった）",
          _made and _v2 == "ACCEPT_MATERIAL" and _readers_ok
          and not _reader_ng)
        t("　許可証が無いときの断り方も見る（4つとも題の不一致で断る）",
          all(str(_r) == "NAME_CORE_MISMATCH" for _r in _readers_why.values()))
        t("★★★確かめた本文と1文字でも違えば、許可証は効かない★★★"
          "（台帳#393の不変条件＝URLではなく本文で束縛する）",
          not _other_ok)
        # ★★★2026-08-17・Codex依頼238のP1★★★
        #   控えで救う側だけ転送を見ていて、**厳格な同定に通る普通のページ**は
        #   素通りしていた（例外側は直したが通常側が隣で残っていた）。
        _HTML_OK = ("<title>L試験機 スロット 新台 解析 | ちょんぼりすた</title>"
                    "<div>機種名 L試験機</div><div>メーカー 京楽</div>")
        _R1 = "https://chonborista.com/slot/x/111/"
        _R2 = "https://chonborista.com/slot/x/999/"
        t("★★★普通のページでも、別ページへ転送されていたら使わない★★★",
          _mc.material_page_identity_ok(
              _fp.FetchedPage(_R1, _R2, _HTML_OK), "L試験機")
          == (False, "REDIRECTED"))
        t("　（対照）転送が無ければ通る",
          _mc.material_page_identity_ok(
              _fp.FetchedPage(_R1, _R1, _HTML_OK), "L試験機")[0])
        # ★器を後から書き換えても効かない★（指紋は関所で数え直す）
        _pg_mut = _fp.FetchedPage(_R1, _R1, _HTML_OK)
        _g_mut = frozenset({_pg_mut.sha256})
        _pg_mut.cleaned_html = _HTML_OK + "<p>あとから足した</p>"
        t("★★器の本文を後から書き換えても、許可証は効かない★★"
          "（作った時の指紋を信じない）",
          _mc.material_page_identity_ok(
              _pg_mut, "L別の機種", grant=_g_mut,
              expected_maker="kyoraku")[1] == "GRANT_CONTENT_MISMATCH")

        # ★★★非対称な転送★★★（2026-08-17・Codex依頼236の指摘）
        #   ★穴だったところ★＝控えに保存したURL（/ 付き）だけを取り直して
        #   いたので、「/ 付きは正常・実行時の / 無しだけ別機種へ転送」を
        #   一度も見ていなかった。許可証には実行時のURLが入り、
        #   読取器はそちらを取りに行くので、別機種の本文が材料に入り得た。
        _TU_RUN = _TU2.rstrip("/")          # 実行時に名鑑から見つかるURL
        _OTHER = "https://chonborista.com/slot/orinpia-slot/777777/"

        def _f_asym(u):
            # ★/ 付きは正しいページ・/ 無しだけ別機種へ転送★
            _nw.LAST_FINAL_URL["url"] = (_OTHER if u == _TU_RUN else u)
            return _NICK2

        _v_asym = _mic.verdict_for("dmm_e2e", "kyoraku", "京楽", _st2, _f_asym,
                                   material_url=_TU_RUN, machine_name=_MN2,
                                   release_date="2026-10-05",
                                   want_profile="title_name_core_mismatch")
        t("★★★実行時のURLだけが別機種へ転送されていたら、使わない★★★"
          "（控えのURLは正常なので、保存側だけ見ていた頃は素通りした）",
          _v_asym is None)
        t("　（対照）転送が無ければ、その形でも使える",
          _mic.verdict_for("dmm_e2e", "kyoraku", "京楽", _st2, _f2,
                           material_url=_TU_RUN, machine_name=_MN2,
                           release_date="2026-10-05",
                           want_profile="title_name_core_mismatch")
          == "ACCEPT_MATERIAL")
        _mc.lookup = lambda u, n, **k: {"url": u, "identity_ok": True, "model_code": "LB/タコスロBD",
                                        "reason": "OK"}
        t("★★BT型式（LB/…）を規格印ありとして採用する★★"
          "（実在の「スマスロ タコスロ」の型式・Codex54回目）",
          gather("スマスロ タコスロ")["model_code"] == "LB/タコスロBD")
        _mc.lookup = lambda u, n, **k: {"url": u, "identity_ok": True, "model_code": "SタコスロBD",
                                        "reason": "OK"}
        t("　S型式との取り違えは引き続き拒否",
          gather("スマスロ タコスロ")["model_code"] is None)
        _mc.lookup = lambda u, n, **k: {"url": u, "identity_ok": True, "model_code": "L1",
                                        "reason": "OK", **_MKC(k)}

        # ★公式ページは本物を想定して差し替える★
        #   （開けなければ止まる作りなので、通る場合の試験には中身が要る）
        real_get = _nw._get
        _true_get = _nw._get   # ★真の原本（real_getは後段で偽物に上書きされる）★
        _nw._get = lambda u, timeout=20: (
            "<title>L試験機</title><body>2026年9月 登場</body>")
        # ★メーカー名簿も試験用にする★（本番の名簿を書き換えない）
        real_cats = _nw.CATALOGS
        _nw.CATALOGS = os.path.join(_tmpdir, "cats.json")
        with open(_nw.CATALOGS, "w", encoding="utf-8") as _f:
            json.dump({"schema": "maker-catalogs/v1", "catalogs": {
                "m": {"name": "試験", "status": "ACTIVE",
                      "list_url": "https://m.example/products/slot/",
                      "link_prefix": "https://m.example/products/slot/"},
                # ★実在題の試験用（かぎ括弧の外の社名検査で使う）★
                "daito_test": {"name": "大都技研", "status": "ACTIVE",
                               "list_url": "https://d.example/slot/",
                               "link_prefix": "https://d.example/slot/"}}},
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
          [x["identity_url"] for x in pick_work({"items": {
              "q_1": {"queue_id": "q_1", "state": "READY", "name": "a",
                      "identity_url": "https://x/a", "maker": "m",
                      "release": "2026-09", "first_seen": "2026-07-01",
                      "last_try": "2026-07-31", "tries": 1},
              "q_2": {"queue_id": "q_2", "state": "READY", "name": "b",
                      "identity_url": "https://x/b", "maker": "m",
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
            _pd = _pend._empty()
            _stay = _pend.add(_pd, "残る機種", "https://m.example/stay/", "m",
                              "2026-09")
            _dead = _pend.add(_pd, "台帳行き", "https://m.example/dead/", "m",
                              "2026-09")
            _pend.save(_pd)
            give_up_now(_pd, _dead["queue_id"], "https://m.example/dead/",
                        "台帳行き", ["x"])
            _pend.mark_tried(_pd, _stay["queue_id"])           # ループの次の周
            _pend.save(_pd)
            _after = _pend.load()["items"]
            t("★★台帳へ移した機種が次の保存で蘇らない★★"
              "（毎晩蘇って行列に居座り、台帳にも同じ件が積まれ続けた・2026-08-01実機）",
              _dead["queue_id"] not in _after
              and _stay["queue_id"] in _after)
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
        # ★★いま発行してよい版そのものを要求する★★
        #   （2026-08-26・Codex29回目。★前は `in SCHEMAS` にしていた★＝
        #     「どちらの版でも合格」なので、**v2を発行しても試験が緑のまま**だった。
        #     凍結している間は、切り替えたら試験が落ちるのが正しい。）
        t("　組み立てた結果を返す（中身を見てから書ける）",
          r["preview"]["machine"]["publication_policy"]
          == _pdz.EMIT_SCHEMA
          and "status" not in r["preview"]["machine"])
        t("　slugは公式URLから作る", r["slug"] == "zzz")

        _sl.read_page = lambda u, n, **k: {"url": u, "host": u.split("/")[2], "ok": True,
                                      "reason": "OK", "fields": {}}
        r2 = run_one("L試験機", "https://m.example/products/slot/zzz/", "m", "2026-09")
        t("★★材料がゼロなら記事を作らない★★",
          "preview" not in r2 and any("採用できた材料" in p for p in r2["problems"]))

        # -------- Codexの反例（2026-07-31・自分で再現を確認してから修正）
        _sl.read_page = lambda u, n, **k: {
            "url": u, "host": u.split("/")[2], "ok": True, "reason": "OK",
            "fields": {"payout_rate": {"1": "97.3%"}}}
        _mc.lookup = lambda u, n, **k: {"url": u, "identity_ok": True, "model_code": None,
                                   "reason": "MODEL_CODE_NOT_FOUND", **_MKC(k)}
        r3 = run_one("L試験機", "https://m.example/products/slot/zzz/", "m", "2026-09")
        # ★2026-08-09・運営者決定で契約が変わった★
        #   以前は「型式名が確定しなければ記事を作らない」だった。
        #   ところが型式名を載せているのは P-WORLD だけ（実測）で、
        #   DMMは描画して読んでも載せていない＝独立2出典は原理的にそろわず、
        #   4夜連続で1件も公開できなかった。
        #   型式は記事に書かず同定にだけ使う方針へ変更。よって
        #   「まだ載っていない」では止めない（同定は名鑑2件＋公式名一致で担保）。
        t("★★型式名が無くても、同定が取れていれば記事を作る★★"
          "（載せているのがP-WORLDだけなので、待っても永久にそろわない）",
          "preview" in r3 and not any("型式名" in x for x in r3["blocked"]))

        # ★取り違えを防ぐ検査は残す★＝型式が名鑑ごとに食い違うなら作らない
        _mc.lookup = lambda u, n, **k: {
            "url": u, "identity_ok": True, "reason": "OK",
            "model_code": "L1" if "chonborista.com" in u else "L9", **_MKC(k)}
        r3b = run_one("L試験機", "https://m.example/products/slot/zzz/", "m", "2026-09")
        t("★★型式名が名鑑ごとに食い違うときは作らない★★（別機種の資料が混じっている）",
          "preview" not in r3b
          and any("食い違" in x for x in r3b["blocked"]))

        _mc.lookup = lambda u, n, **k: {"url": u, "identity_ok": True, "model_code": "L1", "reason": "OK", **_MKC(k)}
        _di.find = lambda n, c=None: {"results": {
            "a": {"state": "FOUND", "url": "https://chonborista.com/1", "why": "",
                  "candidates": [], "surfaces": "1/1", "index_size": 9, "problems": []},
            "b": {"state": "FOUND", "url": "https://nana-press.com/1", "why": "",
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
        _mc.lookup = lambda u, n, **k: {"url": u, "identity_ok": True, "model_code": None,
                                   "reason": "MODEL_CODE_NOT_FOUND", **_MKC(k)}
        r4c = run_one("L試験機", "https://m.example/products/slot/zzz/", "m", "2026-09")
        _mc.lookup = _real_lookup28
        t("★★票が成立しなければ、使わなかった名鑑の曖昧さも問題として残す★★"
          "（URL2件=2票ではない・Codex28回目）",
          any("AMBIGUOUS" in x for x in r4c.get("problems") or []))
        _di.find = lambda n, c=None: {"results": {
            "a": {"state": "FOUND", "url": "https://chonborista.com/1", "why": "",
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
            # ★★名簿に足された社を、待ち行列にも効かせる★★（2026-08-17）
            #   ★直す前はここが通らなかった★＝控えに入った時点で名簿に
            #   無かった社は maker が空のまま固定され、あとで名簿へ足しても
            #   毎晩「名前かメーカーが取れない」で飛ばされ続けた。
            import dmm_machine as _dm_fm
            _real_dm_fetch = _dm_fm.fetch
            _dm_fm.fetch = lambda mid, get=None: {
                "id": mid, "heading": "LB試験機X-300 （新台スマスロ）パチスロ｜天井",
                "maker": "清龍ジャパン", "release_date": "2026-08-03",
                "model_code": "", "has_model_code": False,
                "url": f"https://p-town.dmm.com/machines/{mid}"}
            try:
                _w_no_maker = fill_missing(
                    {"name": "LB試験機X-300", "maker": "",
                     "identity_url": "https://p-town.dmm.com/machines/5089",
                     "release": "2026-08"})
                _w_unknown = fill_missing(
                    {"name": "LB試験機X-300", "maker": "",
                     "identity_url": "https://p-town.dmm.com/machines/5089",
                     "release": "2026-08"})
            finally:
                _dm_fm.fetch = _real_dm_fetch
            t("★★名簿に載ったメーカーは、待ち行列でも結び直す★★"
              "（足しても直らず、その機種を永久に公開できなかった）",
              _w_no_maker.get("maker") == "seiryu_japan")
            t("　（対照）既に入っている値は上書きしない",
              fill_missing(dict(_w_unknown, maker="daitogiken")).get("maker")
              == "daitogiken")
            t("★★規格を読めない公式名では型式を採用しない★★"
              "（照合を飛ばすと同名旧機種の型式・材料で新台を書けた・Codex39回目）",
              "規格（L/S）が公式名" in inspect.getsource(gather))
            t("★★公開前の照合でも soft 404（題がエラー文）を待つ★★（Codex39回目）",
              "題がエラー文です" in inspect.getsource(verify_official))
            t("★★メーカー違いの名鑑は材料・転載照合からも外す★★"
              "（型式の票からしか外れず材料に復活できた・Codex41回目）",
              "材料からも除外" in inspect.getsource(gather))
            # ★★材料の採否を、実際の呼び出しで確かめる★★
            #   （2026-08-17・依頼225のCodex指摘4）
            #   前は「ソースにこの文字があるか」だけを見ていたので、
            #   **旧仕様の名前が残っていれば通って**しまった。
            # ★★本体の関数をそのまま呼ぶ★★（2026-08-17・Codex依頼228の指摘1）
            #   前は本体の式を正規表現で取り出して exec していたので、
            #   **前段のループ（どれが控えを引くか）を通っていなかった**。
            #   その結果 UNKNOWN が控えで救われる穴を検知できなかった。
            #   ★式を写さない・式を抜き出さない・本体を呼ぶ★
            _U = "https://a.example/1"

            def _pick(state, reason, identity_ok=True, cached=None):
                """★本体の判定関数を通して採否を見る★（通信しない）"""
                looks = [{"url": _U, "reason": reason,
                          "identity_ok": identity_ok,
                          "maker_check": {"state": state, "seen": "平和",
                                          "expected": "olympia_estate",
                                          "owners": ["heiwa"]}},
                         {"url": "https://b.example/2", "reason": "",
                          "identity_ok": True,
                          "maker_check": {"state": "MATCH", "seen": "平和",
                                          "expected": "heiwa"}}]
                dec = maker_material_decision(
                    looks, "dmm_5086", "olympia_estate",
                    verdict_of=lambda e, s, u, prof="maker_field": cached,
                    machine_name="L試験機", release_date="2026-10-05")
                return (_U not in dec["bad"], dec)

            t("★★明らかに別の社なら材料に使わない★★",
              not _pick("MISMATCH", "DIRECTORY_MAKER_MISMATCH（別の社）")[0])
            t("★★どの社か分からないものは材料に使わない★★"
              "（★同名で別メーカーの機種は実在する★"
              "＝パチスロ犬夜叉 2016年ロデオ／2022年クロスアルファ）",
              not _pick("UNKNOWN",
                        "DIRECTORY_MAKER_UNRESOLVED（名簿で解決できません）")[0])
            t("★★★どの社か分からないものは、控えで『使う』と決めてあっても"
              "救わない★★★（2026-08-17・Codex依頼228の指摘1／"
              "★前はここが通っていた＝UNKNOWNが控えで復活していた★）",
              not _pick("UNKNOWN",
                        "DIRECTORY_MAKER_UNRESOLVED（名簿で解決できません）",
                        cached="ACCEPT_MATERIAL")[0])
            t("★★関係のある社でも、控えで決めていなければ使わない★★"
              "（RELATEDは『2AIへ回す価値がある』印であって『同じ会社』ではない。"
              "DMMの機種IDは名鑑のURLと結び付いていない）",
              not _pick("RELATED", "DIRECTORY_MAKER_RELATED（関係のある社です）")[0])
            t("★★関係のある社で、控えで『材料に使う』と決めてあれば使う★★",
              _pick("RELATED", "DIRECTORY_MAKER_RELATED（関係のある社です）",
                    cached="ACCEPT_MATERIAL")[0])
            t("★★控えで『使わない』と決めてあれば、必ず材料から外す★★"
              "（2026-08-17に、ここが抜けて復活していた）",
              not _pick("RELATED", "DIRECTORY_MAKER_RELATED（関係のある社です）",
                        cached="REJECT_MATERIAL")[0])
            t("　機種名の照合に落ちたものは今までどおり外す",
              not _pick("RELATED", "", identity_ok=False)[0])
            # ★控えを引く対象そのものを、返り値で確かめる★
            t("★★控えを引くのは RELATED だけ★★"
              "（UNKNOWNでは2AIへの問いも出さない＝救う道を作らない）",
              len(_pick("RELATED", "DIRECTORY_MAKER_RELATED（…）")[1]
                  ["questions"]) == 1
              and len(_pick("UNKNOWN", "DIRECTORY_MAKER_UNRESOLVED（…）")[1]
                      ["questions"]) == 0
              and len(_pick("MISMATCH", "DIRECTORY_MAKER_MISMATCH（…）")[1]
                      ["questions"]) == 0)
            t("★★控えで通したページは、記録として残る★★"
              "（あとから『どの材料が例外で入ったか』を追えるように）",
              _pick("RELATED", "DIRECTORY_MAKER_RELATED（…）",
                    cached="ACCEPT_MATERIAL")[1]["relation_checks"][0]
              .get("relationship_verified") is False
              and _pick("RELATED", "DIRECTORY_MAKER_RELATED（…）",
                        cached="ACCEPT_MATERIAL")[1]["relation_checks"][0]
              .get("verdict") == "ACCEPT_MATERIAL")
            t("　名簿で一致した名鑑は、そもそも外れない",
              _pick("MATCH", "")[0])
            # ★採用の記録が残るか★（2026-08-17・Codex依頼228の指摘7）
            import tempfile as _tf
            with _tf.TemporaryDirectory() as _td:
                _p = write_maker_relation_record(
                    "dmm_5086", "L試験機",
                    _pick("RELATED", "DIRECTORY_MAKER_RELATED（…）",
                          cached="ACCEPT_MATERIAL")[1]["relation_checks"],
                    base=_td)
                _txt = (io.open(_p, encoding="utf-8").read() if _p else "")
            t("★★控えで通した材料は、判断記録に残る★★"
              "（前は実行の戻り値とログにしかなく、あとから由来を追えなかった）",
              bool(_p) and "ACCEPT_MATERIAL" in _txt
              and "https://a.example/1" in _txt
              and "会社が同じか" in _txt)
            t("　採用が無ければ記録も作らない（空のファイルを増やさない）",
              write_maker_relation_record("dmm_5086", "L試験機", []) == "")
            t("★★3件目の名鑑が落ちても、正常な2票を巻き込まない★★"
              "（取得失敗の名鑑だけを票・材料から外す・Codex53回目）",
              "転載照合で取得できず・票と材料から除外"
              in inspect.getsource(gather)
              and "failed" in inspect.getsource(gather))
            t("★★同定に落ちたページ（identity_ok=偽）は材料からも外す★★"
              "（他社名の題のページが材料の票に復活できた・Codex56回目）"
              "／★ソースの文字ではなく挙動で見る★",
              not _pick("MATCH", "", identity_ok=False)[0]
              and not _pick("RELATED", "", identity_ok=False,
                            cached="ACCEPT_MATERIAL")[0])
            # ★実際に呼んで数える★（2026-08-12・依頼160のP1-6）
            #   以前は本文に文字列があるかしか見ていなかったので、
            #   数える対象を増やし忘れても試験は通った。
            _only_model = {"adopted": {"model_code": "L試験機"}}
            _mods = {}
            # ★数える対象は定数から取る★（2026-08-13・台帳#344）
            #   ここに列挙を書き写していたので、MODULE_FIELDS を増やしても
            #   試験は素通りした（実際 gameplays を足したとき気づけなかった）。
            for _k in MODULE_FIELDS:
                _mods[_k] = usable_material(
                    {"adopted": {"model_code": "L試験機"},
                     _k: {"adopted": [{"x": 1}]}})
            # ★2AIで決まらなければメールで知らせる★（2026-08-12・運営者決定）
            #   台帳へ載せる＝翌朝のまとめメールで届く。
            #   ★載せられなくても公開は止めない★（ログには必ず残す）
            _asked = []
            _keep_ledger_fn = globals()["_ledger"]
            globals()["_ledger"] = lambda *a: (_asked.append(a), True)[1]
            try:
                _ask_ledger("zzz", "試験機", "天井はどれですか")
                _ok_call = (len(_asked) == 1 and _asked[0][0] == "zzz"
                            and _asked[0][3] == "ASK_2AI")
                globals()["_ledger"] = lambda *a: False    # 載せられない場合
                _ask_ledger("zzz", "試験機", "天井はどれですか")
                _no_raise = True
            except Exception:                        # noqa: BLE001
                _ok_call, _no_raise = False, False
            finally:
                globals()["_ledger"] = _keep_ledger_fn
            t("★★2AIで決まらなかった質問は台帳へ載せる★★（翌朝のメールで届く）",
              _ok_call)
            t("★★台帳に載せられなくても公開を止めない★★", _no_raise)
            # ★公開より先に載せる★（2026-08-12・依頼163の2）
            #   公開の途中で落ちると、質問がどこにも残らなくなる。
            _src = inspect.getsource(run_one)
            t("★★2AIへの質問は公開より先に台帳へ載せる★★",
              _src.index("_ask_ledger(out[\"slug\"], name, q)")
              < _src.index("machine = _ba.build_machine("))
            t("　台帳に載せられなかったら問題として残す（黙って消さない）",
              "2AIへの質問を台帳に載せられません" in _src)
            t("　質問は run_one が持ち回る（黙って捨てない）",
              'out["ask_2ai"] = _ba.checker_questions(mat)'
              in inspect.getsource(run_one))
            # ★同定の失敗は「文言」ではなく「印」で止める★（依頼166のP0）
            #   止める判定は決まった文言の一致で見ていたので、
            #   新しい同定（P-WORLD）の文言は一致せず、
            #   **同定が失敗しても公開が止まらなかった**（実際に確かめた）。
            _idf = [f"{IDENTITY_FAILED} メーカーが食い違います",
                    f"{IDENTITY_FAILED} 機種名が一致しません",
                    f"{IDENTITY_FAILED} パチスロのページではありません",
                    f"{IDENTITY_FAILED} 導入開始の日付が読めません",
                    f"{IDENTITY_FAILED} 機種ページを取得できません: timeout"]
            t("★★同定の失敗は必ず公開を止める★★（文言に頼らない）",
              all(_blocking([x]) for x in _idf))
            #   ★対照実験★＝印を外すと、どれも止まらない
            t("　（対照）印が無いと素通りする",
              not any(_blocking([x.replace(IDENTITY_FAILED, "").strip()])
                      for x in _idf))
            # ★メーカーが読めないページは同定成功にしない★（依頼167のP0）
            # ★下位が黙っていても、上位が止めることを見る★（依頼168のP2）
            #   偽の fetch に問題を持たせると、下位が止めた後の
            #   「印付け」しか見ないことになり、
            #   **上位自身の守りを壊しても試験が通って**しまう。
            import dmm_machine as _dm_mod
            _keep_fetch2 = _dm_mod.fetch
            _dm_mod.fetch = lambda mid, **k: {
                "id": str(mid), "url": "https://p-town.dmm.com/machines/5049",
                "heading": "スマスロ タコスロ （新台スマスロ）パチスロ｜天井",
                "model_code": "LB/タコスロBD", "has_model_code": True,
                "maker": "",                       # ★メーカーが読めなかった★
                "release_date": "2026-09-07", "release_precision": "day",
                "release_raw": "2026年09月07日（月）予定", "planned": True}
            try:
                _r = _verify_dmm(
                    "スマスロ タコスロ",
                    "https://p-town.dmm.com/machines/5049",
                    "universal", "2026-09")
            finally:
                _dm_mod.fetch = _keep_fetch2
            t("★★下位が黙っていても、上位がメーカー空を止める★★"
              "（公開まで止まる）",
              bool(_blocking(_r["problems"]))
              and any("メーカーを読めませんでした" in x for x in _r["problems"]))
            # ★名前の読み直しはDMMの読み方で★（2026-08-16・台帳#376）
            #   DMMの見出しはSEOの飾りつきなので、**見出しから機種名を作らない**。
            #   ここは「待ち行列が覚えている名前が、まだそのページの機種を
            #   指しているか」だけを見る（指していなければ使い回しの疑い）。
            _dmf = os.path.join(BASE, "tests", "fixtures",
                                "dmm_machine_5049.html")
            if os.path.isfile(_dmf):
                import dmm_machine as _dmm
                _dhtml = io.open(_dmf, encoding="utf-8").read()
                _keep_fetch = _dmm.fetch
                _dmm.fetch = lambda mid, **k: _dmm.parse(_dhtml, str(mid))
                try:
                    _w = fill_missing({
                        "name": "スマスロ タコスロ",
                        "identity_url": "https://p-town.dmm.com/machines/5049",
                        "maker": "universal", "release": "2026-08"})
                    _w2 = fill_missing({
                        "name": "スマスロ北斗の拳",
                        "identity_url": "https://p-town.dmm.com/machines/5049",
                        "maker": "universal", "release": "2026-08"})
                finally:
                    _dmm.fetch = _keep_fetch
                t("★★DMMの見出しから機種名を作らない★★"
                  "（飾りつきの見出しを名前にすると全件が使い回し扱いになる）",
                  _w["name"] == "スマスロ タコスロ"
                  and not _w.get("_name_conflict"))
                t("★★それでも使い回しの疑いは止める★★（別機種の名前なら印を付ける）",
                  bool(_w2.get("_name_conflict")))
                t("　導入年月も機種ページから直す", _w["release"] == "2026-09")
            else:
                t("★試験用の保存ページがありません（tests/fixtures）★", False)
            # ★手で渡すときの入口を、実際に main() で確かめる★
            #   （2026-08-13・依頼171のP3）引数の判定や受け渡しが将来外れても
            #   気づけるように、通し（main）で見る。
            def _main_rc(argv):
                _keep_argv, _keep_run = sys.argv, globals()["run_one"]
                _called = {}
                globals()["run_one"] = lambda *a, **k: (
                    _called.update(k), {"wrote": [], "problems": [],
                                        "blocked": [], "slug": None})[1]
                sys.argv = ["add_machine_run.py"] + argv
                try:
                    return main(), _called
                except SystemExit as e:          # noqa: PERF203
                    return (e.code or 0), _called
                finally:
                    sys.argv, globals()["run_one"] = _keep_argv, _keep_run

            _pwu = "https://www.p-world.co.jp/machine/database/10513"
            _rc1, _c1 = _main_rc(["--name", "試験機", "--official-url", _pwu,
                                  "--maker", "kitadenshi", "--release", "2026-10"])
            t("★★P-WORLDのURLを手で渡すとき、名乗りが無ければ止まる★★",
              _rc1 == 1 and not _c1)
            _rc2, _c2 = _main_rc(["--name", "試験機", "--official-url",
                                  _pwu + "?utm=1", "--maker", "kitadenshi",
                                  "--release", "2026-10", "--expect-maker", "北電子"])
            t("★★URLの形が違えば、直し方を示して止まる★★",
              _rc2 == 1 and not _c2)
            t("　一覧カードの証跡は今までどおり",
              "#card2" in _evidence_ref({"identity_evidence": {
                  "list_html_sha256": "abc", "card_index": 2}}))

            t("★★天井・AT・CZ・リセットだけ採れた機種も「材料あり」と数える★★"
              "（基本スペック直下しか見ず記事を永久に作れなかった・Codex57回目"
              "／リセットは依頼160のP1-6）",
              not usable_material(_only_model)
              and all(len(v) == 1 for v in _mods.values())
              and set(_mods["resets"]) == {"resets#0"})
            # ★★読者に届く事実がゼロのページを作らない★★
            #   （2026-08-29・台帳#497／自分で再現した）
            #   ★型（machine_profile）は読者に出ない★＝ページの品質ラインを
            #   選ぶだけの裏方。それだけで「材料あり」と数えていたので、
            #   機種名と登場時期だけ・7つの箱すべて「未確認です」という
            #   ページが黙って公開される入口になっていた。
            def _only(**kw):
                return {"adopted": dict(kw), "ceilings": {"adopted": []},
                        "at_specs": {"adopted": []}, "czs": {"adopted": []},
                        "resets": {"adopted": []}, "gameplays": {"adopted": []}}

            _prof = {"value": {"profile": "AT_CZ"}, "sources": ["a"]}
            t("★★型だけでは「材料あり」と数えない★★"
              "／★読者に出ない項目なので、中身ゼロのページが公開されていた★",
              not usable_material(_only(machine_profile=_prof)))
            t("　型式名と型だけでも数えない",
              not usable_material(_only(model_code={"value": "L/1"},
                                        machine_profile=_prof)))
            t("★★『天井はありません』は読者に出る事実なので数える★★"
              "／★裏方をまとめて外すと、天井なし機種が永久に作れなくなる★",
              bool(usable_material(_only(
                  ceiling_state={"value": {"state": "NONE"},
                                 "sources": ["a", "b"]}))))
            t("　読者に出る値（コイン持ち）は今までどおり数える",
              bool(usable_material(_only(
                  games_per_50={"value": {"games": 31.0},
                                "sources": ["a", "b"]}))))

            t("★★機種名の芯が変わったURLは公開へ進めない★★"
              "（使い回し検知が公開を止めていなかった・Codex41回目）",
              "_name_conflict" in inspect.getsource(fill_missing)
              and "_name_conflict" in inspect.getsource(main))
            # ★かぎ括弧の実在題（山佐・大都）を公開前照合が通す★（Codex42回目）
            _nw._get = lambda u, timeout=20: (
                "<title>「スマスロパリピ孔明」公式サイト</title>"
                "<body>パチスロ 2026年8月導入</body>")
            v6 = verify_official("スマスロパリピ孔明",
                                 "https://m.example/products/slot/prskkm/", "m")
            _nw._get = lambda u, timeout=20: (
                "<title>大都技研「スロット ワールドダイスター」製品サイトはこちら!"
                "</title><body>スロット 2026年8月導入</body>")
            # ★社名入りの題は「その社の照合」で通す★（2026-08-02・Codex53回目
            #   で他社名の題を拒むようにしたため、期待メーカーを大都に合わせる。
            #   本番でもこの題は大都の機種の照合でしか現れない）
            v7 = verify_official("スロット ワールドダイスター",
                                 "https://d.example/slot/wds/", "daito_test")
            t("★★かぎ括弧の実在題（山佐・大都）を公開前照合が通す★★"
              "（大都の8月導入機を出せない経路だった・Codex42回目）",
              not any("一致しません" in x for x in v6["problems"])
              and not any("一致しません" in x for x in v7["problems"]))
            t("★★他社名入りの題は、別メーカーの照合では通さない★★（Codex53回目）",
              any("一致しません" in x for x in verify_official(
                  "スロット ワールドダイスター",
                  "https://m.example/products/slot/wds/", "m")["problems"]))
            _nw._get = lambda u, timeout=20: (
                "<title>「スマスロパリピ孔明SP」公式サイト</title>"
                "<body>パチスロ 2026年8月導入</body>")
            v8 = verify_official("スマスロパリピ孔明",
                                 "https://m.example/products/slot/prskkm/", "m")
            t("　かぎ括弧でも派生機（…SP）は通さない",
              any("一致しません" in x for x in v8["problems"]))
            t("★★L/Sが入れ替わる使い回しに追随しない★★（芯はL/Sを落とす・Codex42回目）",
              "規格の印が変わっています" in inspect.getsource(fill_missing))
            t("★★公式名にL/Sが無くても、型式名の印があれば通す★★"
              "（北電子マイジャグラーVIを一律人送りにしていた・Codex43回目）",
              "型式名の印" in inspect.getsource(gather)
              and "からも読めません" in inspect.getsource(gather))
            # ★メンテ画面のまま公開へ進めない通し試験★（Codex45回目）
            _nw._get = lambda u, timeout=20: (
                "<title>Access Denied</title><p>ただいまメンテナンス中です</p>")
            r45 = run_one("L試験機", "https://m.example/products/slot/zzz/",
                          "m", "2026-09")
            t("★★公式がメンテ画面なら、材料がそろっても書かない★★"
              "（RETRYABLEに足した時BLOCKINGへ入れ忘れた・Codex45回目）",
              "preview" not in r45
              and any("読める状態ではありません" in x for x in r45["blocked"]))
            # ★かぎ括弧の外の派生印★（Codex46回目）
            _nw._get = lambda u, timeout=20: (
                "<title>「スマスロ試験機」SP公式サイト</title>"
                "<h1>スマスロ試験機 SP</h1><body>パチスロ 2026年9月導入</body>")
            v9 = verify_official("スマスロ試験機",
                                 "https://m.example/products/slot/shiken/", "m")
            t("★★かぎ括弧の外の派生印（SP）を通さない★★（Codex46回目）",
              any("一致しません" in x for x in v9["problems"]))
            _nw._get = lambda u, timeout=20: (
                "<title>大都技研「スロット ワールドダイスター」製品サイトはこちら!"
                "</title><body>スロット 2026年8月導入</body>")
            v10 = verify_official("スロット ワールドダイスター",
                                  "https://d.example/slot/wds/", "daito_test")
            t("　実在形（社名＋定型句）は引き続き通る",
              not any("一致しません" in x for x in v10["problems"]))
            t("★★人間確認済みの控え（release_overrides）だけ最後に使う★★"
              "（山佐は導入年月が画像のみ・Codex46回目）",
              "_release_override" in inspect.getsource(verify_official)
              and "read_json(RELEASE_OVERRIDES" in inspect.getsource(_release_override))
            t("★★名鑑2票一致の月は「公式が無言の時だけ」使う★★（Codex47回目）",
              "directory_release" in inspect.getsource(gather)
              and "名鑑2票一致の月を使用" in inspect.getsource(run_one)
              and "not release" in inspect.getsource(run_one))
            # ★定型句を種目の証拠にしない★（Codex50回目）
            _nw._get = lambda u, timeout=20: (
                "<title>北斗の拳|パチンコ・パチスロメーカー</title>"
                "<h1>ぱちんこ 北斗の拳</h1><body>2026年9月導入</body>")
            v11 = verify_official("北斗の拳",
                                  "https://m.example/products/slot/hokuto/", "m")
            t("★★企業定型句の「パチスロ」でぱちんこページを通さない★★（Codex50回目）",
              any("パチスロのページに見えません" in x for x in v11["problems"]))
            # ★隠しh1と共存語で種目を偽装できない★（Codex55回目）
            _nw._get = lambda u, timeout=20: (
                "<title>L対象機</title><h1>パチンコ L対象機</h1>"
                "<h1 hidden>パチスロ</h1><body>2026年9月導入</body>")
            v12 = verify_official("L対象機",
                                  "https://m.example/products/slot/tgt/", "m")
            t("★★隠しh1の「パチスロ」でパチンコページを通さない★★"
              "（可視h1限定＋パチンコ語は共存でも打ち消さない・Codex55回目）",
              any("パチスロのページに見えません" in x for x in v12["problems"]))
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
                  f"q_{i}": {
                      "queue_id": f"q_{i}", "state": "READY",
                      "name": f"n{i}", "identity_url": f"https://x/{i}",
                      "maker": "m", "release": "2026-09",
                      "first_seen": f"2026-07-0{i+1}",
                      "last_try": "", "tries": 1} for i in range(3)}})) == 3)
            t("★★一晩に見る件数に上限を置かない★★（2026-08-07・運営者決定）",
              len(pick_work({"items": {
                  f"q_{i}": {
                      "queue_id": f"q_{i}", "state": "READY",
                      "name": f"n{i}", "identity_url": f"https://x/{i}",
                      "maker": "m", "release": "2026-09",
                      "first_seen": "2026-07-01",
                      "last_try": "", "tries": 1} for i in range(30)}})) == 30)
            t("★★DMMに載るのを待っている控えは記事づくりの列に入れない★★"
              "（入れると毎晩『試した』ことになり、待っているだけで打ち切られる）",
              pick_work({"items": {"q_1": {"queue_id": "q_1",
                  "state": "AWAITING_DMM_ID", "name": "待ち",
                  "identity_url": "", "maker": "", "release": "2026-11",
                  "first_seen": "2026-07-01", "last_try": "",
                  "tries": 1}}}) == [])
            _dtm = __import__("datetime").datetime

            def _late(h, m, sch=True):
                return past_deadline(_dtm(2026, 8, 8, h, m), scheduled=sch)
            t("　代わりに時刻で区切る（更新タスクとぶつからないため）",
              _late(5, 30) and not _late(2, 0) and not _late(23, 45))
            t("★★遅れて起動した無人実行も止める★★（2026-08-11・台帳#293）"
              "＝以前は08:00より後だと締切が効かず、件数無制限で走れた",
              _late(8, 30) and _late(14, 0) and _late(22, 59))
            t("　手で流すときは締切を効かせない（人が見ている）",
              not _late(14, 0, sch=False) and not _late(5, 30, sch=False))
            # ★★ロックを失ったら、出す手前で全部止まる★★（依頼154の②）
            #   ここが無いと、30分以上止まってロックが別の実行へ移ったあと
            #   復帰した旧い実行が、そのままコミット・pushできてしまう。
            #   ★再開経路（retry_push_first）も push_after_publish を通る★
            _keep_lost = list(_LOCK_LOST)
            _keep_ctx = os.environ.get("UCHIDOKORO_LOCK_CTX")
            try:
                _LOCK_LOST.clear()
                os.environ.pop("UCHIDOKORO_LOCK_CTX", None)
                t("　ロックを持っていれば関所は通る（手動＝CTX無し）",
                  lock_still_mine("試験") == [])
                _LOCK_LOST.append("生存信号を打てません")
                t("★★生存信号を失ったら出さない★★",
                  bool(lock_still_mine("試験")))
                _LOCK_LOST.clear()
                os.environ["UCHIDOKORO_LOCK_CTX"] = os.path.join(
                    _tmpdir, "no_such_ctx.json")
                t("★★CTXはあるが今は持っていない場合も出さない★★",
                  bool(lock_still_mine("試験")))
                _LOCK_LOST.append("失った")
                t("★★失った状態では push_after_publish が入口で止まる★★"
                  "（未完了公開の再開経路もここを通る）",
                  bool(push_after_publish("dummy_slug")))

                # ★★3か所の配置を、順序つきで固定する★★（依頼155の②）
                #   入口だけを見ていると、commit直前・push直前の検査が
                #   消えても気づけない。「何回目の確認で落とすか」を変えて、
                #   git が呼ばれないことまで確かめる。
                _keep_lsm = globals()["lock_still_mine"]
                _keep_run = globals()["subprocess"].run
                try:
                    # (何回目の確認で落とすか, 名前, 呼ばれてはいけない, 呼ばれるべき)
                    for stop_at, jp, ban, must in (
                            (2, "コミットの直前", ("commit", "push"), ()),
                            (3, "pushの直前", ("push",), ("commit",))):
                        seen, gits = [0], []

                        def _lsm(where, _n=stop_at, _s=seen):
                            _s[0] += 1
                            return [] if _s[0] < _n else [f"{where}: 止めます"]

                        def _run(cmd, *a, **k):
                            if cmd and cmd[0] == "git":
                                gits.append(cmd[1] if len(cmd) > 1 else "")
                            class R:
                                returncode, stdout, stderr = 0, "", ""
                            return R()
                        globals()["lock_still_mine"] = _lsm
                        globals()["subprocess"].run = _run
                        out = push_after_publish("dummy_slug")
                        t(f"★★{jp}で失ったら、そこから先へ進まない★★",
                          bool(out) and not any(g in gits for g in ban)
                          and all(g in gits for g in must))
                finally:
                    globals()["lock_still_mine"] = _keep_lsm
                    globals()["subprocess"].run = _keep_run

                # ★再開経路の3分岐とも、出す経路を必ず通る★（依頼156のP2）
                #   以前は「何か返ってくれば合格」だったので、
                #   push_after_publish を呼ばずに別の理由で失敗しても通った。
                _keep_pap = globals()["push_after_publish"]
                try:
                    # ★3分岐とも、呼ばれ方（slugと already_committed）まで見る★
                    #   （依頼157のP2）以前は slug しか見ておらず、
                    #   誤った already_committed で呼んでも通った。
                    #   ★COMMITTED は実データと同じく sha を持つ★（依頼158のP2）
                    #     再開側は sha と現在のHEADの一致を見てから進むので、
                    #     空のままだとその判定を素通りしていた。
                    for stage, committed, on_top, sha in (
                            ("WRITTEN", False, False, ""),
                            ("WRITTEN", True, True, ""),
                            ("COMMITTED", True, False, "headcommit"),
                            # ★記録したコミットが先端でない＝人が確かめる★
                            #   （出さずに止まるので called は空のまま）
                            ("COMMITTED", None, False, "furuisha")):
                        called = []
                        globals()["push_after_publish"] = (
                            lambda slug, already_committed=False, _c=called:
                            _c.append((slug, already_committed))
                            or ["入口で止めました"])
                        io.open(PUSH_PENDING, "w", encoding="utf-8").write(
                            json.dumps({"slug": "t_resume", "sha": sha,
                                        "stage": stage,
                                        "parent": "oyacommit" if on_top else "",
                                        "at": "2026/08/11 00:00:00"}))
                        _keep_top = globals()["_committed_on_top"]
                        _keep_head = globals()["_head"]
                        try:
                            globals()["_committed_on_top"] = (
                                lambda parent, slug, _v=on_top: _v)
                            globals()["_head"] = (lambda _v=sha: "headcommit" if _v != "furuisha" else "atarasii")
                            out = retry_push_first()
                        finally:
                            globals()["_committed_on_top"] = _keep_top
                            globals()["_head"] = _keep_head
                        if committed is None:
                            t("★★記録したコミットが先端でなければ、出さずに止まる★★"
                              "（あとから別のコミットが乗っている＝人が確かめる）",
                              bool(out) and called == []
                              and any("別のコミット" in x for x in out))
                        else:
                            t(f"　未完了公開（{stage}/コミット済み={committed}）の再開も、"
                              "出す経路を必ず通る",
                              bool(out) and called == [("t_resume", committed)])
                finally:
                    globals()["push_after_publish"] = _keep_pap
                    try:
                        os.remove(PUSH_PENDING)
                    except OSError:
                        pass
            finally:
                _LOCK_LOST.clear()
                _LOCK_LOST.extend(_keep_lost)
                if _keep_ctx is None:
                    os.environ.pop("UCHIDOKORO_LOCK_CTX", None)
                else:
                    os.environ["UCHIDOKORO_LOCK_CTX"] = _keep_ctx
            # ★試験が本番のどこにも書かないことを、まとめて確かめる★
            #   （2026-08-11・実際に2つ汚した＝台帳と未pushの目印）
            t("★★試験が本番の未pushの目印を触らない★★"
              "（dummy_slug が残り、次の実行が未完了公開として処理しかけた）",
              PUSH_PENDING.startswith(_tmpdir))
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
            # ---- 一覧カードでの同定（2026-08-04・Codex93回目の直し）
            import io as _io
            import ssl as _ssl
            import urllib.error as _ue
            real_cats = _nw.CATALOGS
            _cat = os.path.join(_tmpdir, "cat.json")
            _cardspec = {"card_tag": "li", "card_class": "slotItem",
                         "name_class": "name", "type_class": "category",
                         "year_class": "__year"}
            _nw.CATALOGS = _cat



            # ★★同定に使えるのはDMMの機種ページだけ★★（依頼217の指摘2）
            #   規約の関所は「通信してよいか」を決める。ちょんぼりすた・
            #   なな徹は材料として通信を許しているので、そのURLを同定に
            #   渡すと関所は通してしまう。★別の縛りとして見る★
            _keep_idst = _IDENTITY_SELFTEST["on"]   # ★保存値へ戻す★
            _IDENTITY_SELFTEST["on"] = False   # ★ここだけ本番と同じ扱い★
            try:
                _calls = []
                _keep_g = _nw._get
                _nw._get = lambda u, timeout=20: (_calls.append(u), "")[1]
                try:
                    _r_ok = identity_url_problem(
                        "https://p-town.dmm.com/machines/5049")
                    _r_ch = identity_url_problem("https://chonborista.com/slot/x/")
                    _r_na = identity_url_problem("https://nana-press.com/kaiseki/x/")
                    _r_mk = identity_url_problem("https://m.example/products/slot/x/")
                    _r_pw = identity_url_problem(
                        "https://www.p-world.co.jp/machine/database/1")
                    _v = verify_official("試験機", "https://chonborista.com/slot/x/", "m")
                finally:
                    _nw._get = _keep_g
                t("★★同定に使えるのはDMMの機種ページだけ★★"
                  "（材料として通信を許した先でも、機種の正体は決めさせない）",
                  _r_ok == "" and _r_ch and _r_na and _r_mk and _r_pw)
                t("★★止まる前に取りに行かない★★（通信の前に断る）",
                  _calls == [] and bool(_blocking(_v["problems"])))
                t("　P-WORLDのURLは理由も規約だと分かる", "規約" in _r_pw)
            finally:
                _IDENTITY_SELFTEST["on"] = _keep_idst
            # ★コミット文に書く区分★（2026-08-05・Codex99回目）
            t("　コミット文の区分: 実データから決まった区分を返す",
              _machine_class(sorted(
                  m["slug"] for m in _sj.read_json(
                      os.path.join(BASE, "assets/data/machines.json"),
                      expect=(dict, list)))[0]) in (
                  "LEGACY_COMPLETE", "LEGACY_PREVIEW",
                  "AUTO_INDEXABLE", "AUTO_PENDING"))
            # ★本番の一覧は読むだけ★（書き換えない・依頼157のP1）
            import io as _io
            _real_ms = os.path.join(_tmpdir, "machines_for_test.json")
            _keep_mp = globals()["MACHINES_PATH"]
            globals()["MACHINES_PATH"] = _real_ms
            try:
                _io.open(_real_ms, "w", encoding="utf-8").write(json.dumps(
                    [{"slug": "t1", "publication_policy": "page-decision/v1",
                      "page_decision": {"schema_version": "page-decision/v1",
                                        "indexable": False}}],
                    ensure_ascii=False))
                t("　コミット文の区分: 知らないslugは区分不明",
                  _machine_class("nothing") == "区分不明")
                _io.open(_real_ms, "w", encoding="utf-8").write("{壊れた")
                t("★★コミット文の区分: 壊れていても止めず区分不明にする★★",
                  _machine_class("t1") == "区分不明")
            finally:
                globals()["MACHINES_PATH"] = _keep_mp
            _nw.CATALOGS = real_cats
        finally:
            _nw._get, _mc.page_is_machine = real_get, real_page
    finally:
        _di.find, _sl.read_page, _mc.lookup = real_find, real_read, real_lookup
        _fp.fetch = real_fetch
        _lc.check = real_lc
        _pend.STORE = real_store
        globals()["PUSH_PENDING"] = real_mark
        globals()["_ledger"] = real_ledger
        globals()["_log"] = real_log
        __import__("shutil").rmtree(_tmpdir, ignore_errors=True)

    # ★★回数の記録は、どの終わり方でもちょうど1回★★（2026-08-16・依頼223）
    #   包み（main）を消したり、finally を外したりする退行を捕まえる。
    #   ★本物のログにも通信にも触らない★＝中身とログを偽物へ差し替える。
    _keep_log = globals()["_log"]
    _keep_inner = globals()["_main"]
    _keep_argv = list(sys.argv)
    _lines = []
    try:
        globals()["_log"] = lambda m: _lines.append(str(m))

        def _count(argv, inner):
            _lines.clear()
            sys.argv = ["x"] + list(argv)
            globals()["_main"] = inner
            try:
                main()
            except Exception:                    # noqa: BLE001
                pass
            return len([x for x in _lines if "取りに行った回数" in x])

        _ok_n = _count([], lambda: 0)
        _ret_n = _count([], lambda: 3)

        class _Boom(Exception):
            pass

        _raised = {"got": None}

        def _boom():
            raise _Boom("わざと")

        _lines.clear()
        sys.argv = ["x"]
        globals()["_main"] = _boom
        try:
            main()
        except _Boom as e:
            _raised["got"] = str(e)
        _exc_n = len([x for x in _lines if "取りに行った回数" in x])
        _self_n = _count(["--selftest"], lambda: 0)
        t("★★回数の記録は、どの終わり方でもちょうど1回★★"
          "（正常・途中で返る・例外）",
          _ok_n == 1 and _ret_n == 1 and _exc_n == 1)
        t("★★例外はそのまま外へ出す★★（記録のために握りつぶさない）",
          _raised["got"] == "わざと")
        t("★★自己試験では本番の記録を書かない★★",
          _self_n == 0)
        # ★はじめに持ち分を0へ戻してから数える★（依頼223）
        #   戻さないと、前の実行のぶんが混ざって実数が分からない。
        import new_machine_watch as _nwt
        _nwt.FETCH_BUDGET["used"] = 99
        _count([], lambda: 0)
        t("★★実行のはじめに、取りに行った回数を0へ戻す★★"
          "（前の実行のぶんが混ざらない）",
          _nwt.FETCH_BUDGET["used"] == 0)
    finally:
        globals()["_log"] = _keep_log
        globals()["_main"] = _keep_inner
        sys.argv = _keep_argv

    # ★★2AIだけが答える項目でも、新台追加が止まらない★★
    #   （2026-08-24・Codexの4回目の指摘＝実際に KeyError を再現した）
    t("★★2AIだけが答える項目の表示名を作れる★★"
      "／★以前は KeyError で新台追加そのものが止まっていた★",
      field_label("checker_ceiling") == "早見表に使う天井")
    t("　いままでどおりの項目も同じ名前で出る",
      field_label("payout_range") == _sl.FIELDS["payout_range"]["jp"])
    t("　知らない項目でも止まらず、分かる形で出す（黙って消さない）",
      field_label("zzz_unknown") == "zzz_unknown（名前が未登録）")

    # ★★再検証が本番経路に繋がっている★★（2026-08-24・Codexの8回目）
    #   ★作っただけで呼ばれていない★を防ぐ（今日それを名簿でやった）。
    t("★★公開直前の再検証を、本番の経路から呼んでいる★★"
      "／★作っただけで繋がっていない、をやらない★",
      "_cv.reverify(" in inspect.getsource(run_one))

    # ★★控えが読めないときは新台を作らない★★（2026-08-24・Codexの6回目）
    #   ★直す前は「問題」に足すだけで停止条件に入っていなかった★ので、
    #   2AIが確定させた値が抜けた記事が、黙って世に出た。
    t("★★控えを読めないときは止まる★★"
      "／★2AIの値が抜けた記事を黙って公開しない★",
      blocking_problems(["CONFIRMED_VALUES_UNREADABLE: 2AIの確定値を読めません"]))
    t("　（対照）ふつうの問題では止めない",
      not blocking_problems(["ただのお知らせ"]))

    # ★★止めた理由の符丁が、いちばん無害な理由に落ちない★★
    #   （2026-08-24の夜・台帳#474。★見張りが2晩、本当の理由を隠していた★）
    t("★★公開直前の監査で止まったら、その符丁になる★★"
      "／★材料の注意書きに先に当たると、見張りが無害な理由を報告する★",
      _blocker_code({
          "blocked": ["公開できませんでした: サイト監査: "
                      "46_ポチポチくんの案内と飛び先: 1件 dmm_5089"],
          "problems": ["型式名がまだどの名鑑にも載っていません",
                       "天井: 取れません"]}) == "BLOCKED_BY_SITE_AUDIT")
    t("　材料だけで止まった時は、いままでどおりの符丁",
      _blocker_code({"blocked": [],
                     "problems": ["型式名がまだどの名鑑にも載っていません"]})
      == "MODEL_CODE_MISSING")
    t("　止まっていなければ符丁は空",
      _blocker_code({"blocked": [], "problems": []}) == "")
    t("　どれにも当たらなければ OTHER",
      _blocker_code({"blocked": ["よく分からない理由"]}) == "OTHER")

    # ★★通信には必ず時間制限★★（2026-08-25・Codexの25回目）
    #   ★制限が無いと、固まったときに例外も戻り値も出ず、
    #     終了の記録すら残らない★＝ロックが延びて朝の更新タスクまで止まる。
    #   ★字面ではなく、実際に呼ばれた引数を見る★
    import inspect as _insp25
    _src25 = _insp25.getsource(push_after_publish)
    t("★★push先の確認にも時間制限がある★★"
      "／★git push には前から付いていたのに、ここだけ無かった★",
      "timeout=NET_TIMEOUT" in _src25
      and "ls-remote" in _src25)
    t("　取り直し（fetch）にも時間制限がある",
      _src25.count("timeout=NET_TIMEOUT") >= 2)
    _spy25 = []
    _real_run25 = subprocess.run
    _real_lock25 = lock_still_mine

    # ★★止めるのは「通信するもの」だけ★★（2026-08-25・自分で踏んだ）
    #   ★はじめは subprocess.run を丸ごと止めた★ので、
    #   `git rev-parse HEAD` のようなローカルの処理まで例外になり、
    #   **通信まで一度も届かずに落ちていた**
    #   ＝「止まった」ように見えて、何も試していない。
    _NET25 = ("ls-remote", "fetch", "push")

    def _fake_run25(args, **kw):
        _a = [str(x) for x in args]
        # ★★関所は「通った」ことにする★★（2026-08-25・自分で踏んだ）
        #   ★直す前は本物の関所を通していた★ので、
        #   手元に未コミットの変更があると**そこで先に止まり**、
        #   通信まで一度も届かなかった。
        #   ＝★手元の汚れ具合で結果が変わる試験★（CIでは通り、手元では落ちる）。
        #   ここで見たいのは「通信が固まったとき」だけなので、関所は素通しにする。
        if any("prepush_gate" in x for x in _a):
            return subprocess.CompletedProcess(args, 0, "", "")
        if not any(x in _a for x in _NET25):
            return _real_run25(args, **kw)     # ローカルはそのまま通す
        _spy25.append((tuple(_a[:2]), kw.get("timeout")))
        raise subprocess.TimeoutExpired(args, kw.get("timeout") or 0)

    try:
        # ★ロックの確認で手前から返らないようにする★
        #   ここを通さないと、通信まで一度も届かず
        #   **何も試していないのに緑になる**（2026-08-25・自分で踏んだ）
        globals()["lock_still_mine"] = lambda *a, **k: []
        subprocess.run = _fake_run25
        _out25 = push_after_publish("zzz_timeout_test", already_committed=True)
    except Exception as _e25:                                # noqa: BLE001
        _out25 = [f"例外: {type(_e25).__name__}"]
    finally:
        subprocess.run = _real_run25
        globals()["lock_still_mine"] = _real_lock25
    t("★★通信が固まっても、理由を返して終わる★★"
      "／★黙って止まり続けると、朝の更新タスクまで巻き添えになる★",
      any("打ち切り" in str(x) or "確かめられません" in str(x)
          for x in (_out25 or [])))
    t("　実際に時間制限つきで呼んでいる",
      any(tm for _a, tm in _spy25))

    ng = [n for n, ok in results if not ok]
    # ★控えの置き場を元へ戻す★（試験のあとに本番へ影響を残さない）
    _mic.STORE = _mic_keep_store

    print(f"{nl}{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    """★取りに行った回数を、実行のどこで終わっても必ず残す★

    （2026-08-16・依頼221の指摘2）
    前は巡回の直後に記録していたので、**回数の大半を占める材料探し
    （1機種あたり213回）が入らず**、`--name` の経路には記録が無かった。
    """
    # ★自己試験は本番のログに混ぜない★（2026-08-16・依頼222の指摘1）
    #   selftest は自分でログを差し替えて、戻してから返る。そのあとに
    #   ここで書くと**本物のログへ書いてしまう**（既存の保護を私が壊した）。
    _is_selftest = "--selftest" in (sys.argv[1:] or [])
    _nw.budget_reset()
    try:
        return _main()
    finally:
        if not _is_selftest:
            _log(f"取りに行った回数: {_nw.FETCH_BUDGET['used']} 回"
                 f"（上限 {_nw.FETCH_BUDGET['limit']} 回 / 転送 "
                 f"{_nw.FETCH_COUNT.get('redirect', 0)} 回 / 控えで済んだ "
                 f"{_nw.FETCH_COUNT.get('cached', 0)} 回）")


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--apply", action="store_true", help="実際に書き込む")
    ap.add_argument("--ctx", help="task_lock の CTX パス（--apply に必須）")
    ap.add_argument("--scheduled", action="store_true",
                    help="無人タスクからの実行（決まった時間帯の外では"
                         "新しい機種に着手しない）")
    ap.add_argument("--name", help="1機種だけ試す：正式名称")
    ap.add_argument("--baseline-titles", action="store_true",
                    help="既知URL全部の題を一度だけ控える（すり替え検知の基準）")
    ap.add_argument("--official-url", dest="official_url")
    ap.add_argument("--maker")
    # ★P-WORLDのURLを手で渡すときは、メーカーの表示名も名乗る★
    #   （2026-08-13・依頼170のP2）待ち行列を通らないので覚えた値が無く、
    #   同じ内部IDにぶら下がる別名のどれでも通ってしまう。
    ap.add_argument("--expect-maker", dest="expect_maker", default="",
                    help="P-WORLDの機種ページに出るメーカー名（例: ミズホ）")
    ap.add_argument("--release", default="")
    args = ap.parse_args()
    if args.selftest:
        # ★印は必ず元へ戻す★（2026-08-16・依頼218の指摘3）
        #   例外で抜けたときに真のまま残ると、同じ手続きの中で
        #   **本番の同定が縛りを見ないまま通ってしまう**。
        _keep_id = _IDENTITY_SELFTEST["on"]
        _IDENTITY_SELFTEST["on"] = True   # ★試験は架空のURLを使う★
        try:
            return selftest()
        finally:
            _IDENTITY_SELFTEST["on"] = _keep_id

    if args.baseline_titles:
        return baseline_titles()

    if args.apply and not args.ctx:
        print("★--apply には --ctx（ロックのCTXパス）が必要です★")
        return 1
    if args.apply:
        r = _run_capped(
            [sys.executable, os.path.join(BASE, "scripts", "task_lock.py"),
             "check", "--ctx", args.ctx], capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        if r.returncode != 0:
            print("★ロックを持っていません → 何も書かずに終了します★")
            return 1
        # ★★処理中もロックの生存信号を打ち続ける★★（2026-08-11・台帳#269）
        #   起動時に1回 check するだけだったので、見張り（11社・実測10分超）や
        #   1機種の処理が長引くと**30分でロックを奪われ**、5:05の更新タスクと
        #   同時に同じファイルを触りうる状態だった。
        #   ★打てなくなったら書かない★＝失敗を覚えておき、書く直前に見る。
        _hb_stop = threading.Event()

        def _heartbeat():
            while not _hb_stop.wait(300):        # 5分ごと
              try:                               # noqa: E111
                h = _run_capped(
                    [sys.executable,
                     os.path.join(BASE, "scripts", "task_lock.py"),
                     "heartbeat", "--ctx", args.ctx],
                    capture_output=True, text=True,
                    encoding="utf-8", errors="replace")
                if h.returncode != 0:
                    _LOCK_LOST.append((h.stderr or h.stdout or "")[:200])
                    return                      # 失ったら打ち続けない
              except Exception as e:             # noqa: BLE001,E111
                # ★糸だけ黙って死なせない★（印が立たないと書けてしまう）
                _LOCK_LOST.append(f"生存信号を打てません: {type(e).__name__}: {e}")
                return

        os.environ["UCHIDOKORO_LOCK_CTX"] = args.ctx
        threading.Thread(target=_heartbeat, daemon=True).start()
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

    # ★★締切は**無人の**全部の入口に掛ける★★（2026-08-11・依頼152の指摘③）
    #   ★手で流すときは締切を効かせない★＝人が見ているので時間帯で縛らない
    #     （依頼153の指摘③。要件は「無人の全入口」であって「全入口」ではない）
    #   以前は待ち行列のループの中だけで見ていたので、
    #   **--name の直接指定は締切を一度も通らなかった**（時間帯の外でも1機種作れた）。
    #   また通常の経路も、締切の外に起動すると見張りまで済ませてから止まっていた。
    #   ★未完了の公開の片付けだけは、ここより手前で済ませる★（復旧は時間帯に関係ない）
    if past_deadline(scheduled=args.scheduled):
        _log(f"  {NEW_MACHINE_START_HHMM}〜{NEW_MACHINE_DEADLINE_HHMM} の外なので"
             "新しい機種には着手しません（片付けだけ済ませました）")
        print(f"  ★{NEW_MACHINE_START_HHMM}〜{NEW_MACHINE_DEADLINE_HHMM} の外です"
              "→新しい機種には着手しません★")
        return 0

    if args.name:
        if not (args.official_url and args.maker):
            print("★--name と一緒に --official-url --maker が必要です★")
            return 1
        # ★P-WORLDのURLなら表示名を必ず名乗ってもらう★（依頼170のP2）
        # ★P-WORLDのURLは形が違っても気づく★（依頼171のP3）
        #   クエリや#付きだと見分けられず、案内が出ないまま後段で止まっていた。
        from urllib.parse import urlsplit as _usp
        _u = _usp(str(args.official_url or ""))
        _is_pw_host = (_u.hostname or "") in ("www.p-world.co.jp", "p-world.co.jp")
        if _is_pw_host and not _pw_machine_url(args.official_url):
            print("★P-WORLDのURLは余計なものを外して渡してください★"
                  "（正しい形: https://www.p-world.co.jp/machine/database/<機種ID>）"
                  f"／渡された値: {args.official_url}")
            return 1
        if _pw_machine_url(args.official_url) and not args.expect_maker:
            print("★P-WORLDのURLを手で渡すときは --expect-maker が要ります★"
                  "（機種ページに出るメーカー名。例: --expect-maker ミズホ）"
                  "／同じ系列の別名を見分けるために使います")
            return 1
        res = run_one(args.name, args.official_url, args.maker, args.release,
                      args.apply,
                      before_write=lambda: _claim_today(args.official_url),
                      expect_maker=args.expect_maker)
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
    # ★入口はDMMの導入カレンダー一本★（2026-08-16・台帳#376）
    #   メーカー公式11社の一覧を見張る仕組み（discover）は
    #   2026-08-12に止め、2026-08-16に**削除**した。
    #   ★止めたまま残さない★＝残すと「まだ生きている」と誤読される
    #   （実際に、移行の作業中に誤って「メーカー公式の規約確認が要る」と
    #     報告してしまった）。戻したくなったらgitの履歴から取る。
    d = discover_calendar(persist=apply_it)
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
    # ★DMMに載らないまま導入日を過ぎた機種を、一度だけ台帳へ★
    #   （2026-08-16・依頼213／Codexの助言どおり「巡回のあと・記事づくりの前」）
    #   ここなら「その晩の巡回でも結び付かなかった」と確定している。
    #   ★控えは消さない★＝あとから載れば自動の経路へ戻せる。
    for it in (_pend.calendar_missing_due(pend) if apply_it else []):
        if _ledger("site", "structural", "MATERIAL", "DMM_CALENDAR_MISSING",
                   "導入日を過ぎてもDMMのカレンダーに載りません",
                   f"{it['name']} / 登場 {it.get('release', '')} / "
                   f"見つけた日 {it.get('first_seen', '')} / "
                   f"移行前のURL: {it.get('legacy_url', '')} / "
                   "★控えは残してあります（載れば自動で結び直します）★"):
            # ★台帳に残せた時だけ「知らせた」ことにする★
            #   残せないまま印を付けると、二度と知らせない機種になる。
            it["calendar_missing_reported_at"] = _pend._today()
            _pend.save(pend)
            _log(f"  ★DMMに載らないまま導入日を過ぎました: {it['name']}★")
        else:
            _log(f"  DMM未掲載を台帳に残せませんでした（明晩また知らせます）"
                 f": {it['name']}")
    # ★待ちすぎた分は黙って消さず、台帳に残す★
    for it in (_pend.give_up(pend) if apply_it else []):
        if not _ledger("site", "structural", "MATERIAL", "PENDING_GAVE_UP",
                       f"新台を{_pend.GIVE_UP_DAYS}日待っても記事にできませんでした",
                       f"{it['name']} / {it.get('identity_url', '')} / "
                       f"直近の理由: {it.get('last_reason', '')}"):
            # ★台帳に残せなかったら行列へ戻す★（2026-07-31・Codex20回目）
            #   give_up() は返す前に外してしまうので、そのまま保存すると
            #   **待ち行列にも台帳にも無い機種**になる。
            pend["items"][it["queue_id"]] = it
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
        # ★件数ではなく時刻で区切る★（2026-08-07・運営者決定）
        #   5:05の更新タスクをロック待ちにしないため。
        #   いま処理中の機種は最後まで通す（ここは着手の前）。
        if past_deadline(scheduled=args.scheduled):
            _log(f"  {NEW_MACHINE_DEADLINE_HHMM} を過ぎたので"
                 "新しい機種には着手しません（残りは明晩）")
            print(f"  ★{NEW_MACHINE_DEADLINE_HHMM} を過ぎました→残りは明晩★")
            break
        work = fill_missing(work)
        # ★使い回しの疑いは公開処理へ進めない★（2026-08-02・Codex41回目）
        #   検知（recheck）と公開の停止がつながっていなかった。
        if work.get("_name_conflict"):
            msg = (f"同じURLの機種名が変わりました（{work['name'][:30]} → "
                   f"{work['_name_conflict'][:30]}）")
            print("  ★止めました: " + msg)
            if apply_it:
                give_up_now(pend, work["queue_id"], work["identity_url"],
                            work["name"], [msg])
            else:
                print("（下見）--apply の実行が台帳へ移します")
            continue
        if not (work["name"] and work["maker"]):
            _log(f"  まだ記事にできません（名前かメーカーが取れない）: {work['identity_url']}")
            # ★早く抜けるときも試した日を残す★（残さないと毎晩ここで詰まる）
            if apply_it:
                _pend.mark_tried(pend, work["queue_id"])
                _pend.save(pend)
            continue
        _log(f"試す: {work['name']} / {work['maker']} / {work['release']}")
        # ★試したことを必ず残す★（残さないと同じものばかり選ばれる）
        # ★下見では残さない★（試行記録・巡回順を進めない・Codex28回目）
        if apply_it:
            _pend.mark_tried(pend, work["queue_id"])
            _pend.save(pend)
        # ★覚えた表示名と食い違ったことがあるなら、公開の前で止める★
        #   （2026-08-13・依頼171のP2）待ち行列に残すだけでは誰も見ない。
        #   ページが元の表示名へ戻っていると照合は通り、公開後に
        #   待ち行列ごと消えて**食い違いの記録が誰にも届かない**。
        # ★出典に合わせた欄を見る★（2026-08-16・依頼213の指摘5）
        #   DMMの控えなら dmm_maker_conflict を見る。片方だけ見ていると、
        #   DMM内で表示名が変わっても気づけない。
        _cfkey = ("dmm_maker"
                  if str(work.get("identity_source") or "") == "dmm"
                  else "pworld_maker")
        _cf = _maker_conflicts(work)
        if _cf:
            _t = (f"{work.get('name') or work['identity_url']}: "
                  "メーカーの表示名が途中で変わりました")
            _d = (f"覚えていた表示名: {_expect_maker(work)}\n"
                  f"あとから出てきた表示名: {'／'.join(_cf)}\n"
                  f"URL: {work['identity_url']}\n\n"
                  "★同じ機種か、別機種にURLが使い回されたのかを確かめてください★\n"
                  f"確かめたら、待ち行列の {_cfkey}_conflict を"
                  "消してください。")
            if apply_it and _ledger(_slug_hint(work["identity_url"]), "structural",
                                    "MATERIAL", "MAKER_NAME_CONFLICT", _t, _d):
                _log(f"  ★表示名の食い違いを台帳へ上げました: "
                     f"{work['identity_url']}")
            else:
                _log(f"  ★表示名の食い違いがあります（台帳へ上げられません）"
                     f": {work['identity_url']}")
            print("  ★止めました: 表示名が途中で変わりました"
                  f"（{work['identity_url']}）")
            continue
        res = run_one(work["name"], work["identity_url"], work["maker"],
                      work["release"], apply_it,
                      release_is_cache=True,       # ★待ち行列の年月は控え★
                      before_write=lambda u=work["identity_url"]: _claim_today(u),
                      # ★最初に確かめたメーカーの表示名★（台帳#335の項目5）
                      #   ★出典に合わせて選ぶ★（2026-08-16・依頼213の指摘5）
                      #   DMMの控えにP-WORLD時代の表示名をぶつけると、
                      #   合っているのに食い違い扱いで止まる。
                      expect_maker=_expect_maker(work),
                      # ★どの控えを外すかはIDで渡す★（URLは変わりうる）
                      pending_id=work.get("queue_id", ""))
        for b in res.get("blocked") or []:
            print("  ★止めました: " + b[:150])
        # ★★止まった理由を、機種ごとに符丁で残す★★（2026-08-22・Codexの指摘）
        #   ★これが無くて起きたこと★＝5日連続で公開0件だったのに、
        #   毎日エラーなく完走していたので誰も気づかなかった。
        #   ★同じ理由で2回続いたら知らせる★のが主監視（add_machine_health）。
        #   ★自由文は入れない★＝文言を変えるたびに見張りが壊れるため、
        #   最初に見つかった符丁だけを残す。
        if apply_it and not res.get("wrote"):
            _pend.mark_blocked(pend, work.get("queue_id", ""),
                               _blocker_code(res))
            _pend.save(pend)
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
            if ng:
                # ★pushできなかった夜は続けない★（手元に未pushを残したまま
                #   次の機種を作ると、翌晩に2機種ぶんまとめてpushしようとして
                #   関所で止まる。2026-08-04に実害・台帳#225）
                _log("  pushできなかったので今夜はここまで")
                break
            # ★公開できても続ける★（2026-08-07・運営者決定）
            #   新台は導入日が決まっていて待てない。分かり次第そのまま作る。
            _log(f"  公開しました → 次の機種へ（{res['slug']}）")
            if apply_it:
                # ★公開できたので、止まっていた連続を切る★（2026-08-22）
                _pend.mark_unblocked(pend, work.get("queue_id", ""))
                _pend.save(pend)
            continue
        if any("今日の担当ではありません" in p for p in res.get("problems") or []):
            _log("  今日の担当ではありません → 今日はここまで")
            break
        # ★やり直しても変わらない理由なら、行列から出して後ろを通す★
        if res.get("blocked") and not retry_later(res["problems"]):
            if apply_it:
                give_up_now(pend, work["queue_id"], work["identity_url"],
                            work["name"], res["problems"])
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


# ★中身を見に来たら元の関数を返す★（2026-08-16・依頼219）
#   囲みにしたので gather は薄い包みになった。試験や監査が中身を読むとき、
#   包みだけ見えると**守りが消えたように見える**（実際に試験が落ちた）。
gather.__wrapped__ = _gather


# ★中身を見に来たら元の関数を返す★（2026-08-16・依頼221）
#   回数の記録を実行全体で囲むため main を薄い包みにした。
#   試験や監査が中身を読むとき、包みだけ見えると守りが消えて見える。
main.__wrapped__ = _main


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

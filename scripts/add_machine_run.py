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
import subprocess
import sys

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


def discover() -> dict:
    """メーカー公式の一覧から新台候補を出す。"""
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
        if r["first_time"]:
            out["first_time"].append(f"{mid}（{r['total']}件を記録）")
            continue
        for url in r["new"]:
            c = _nw.classify(url, None)
            kept = True
            if c["ok"]:
                out["candidates"].append({"maker": mid, **c})
                # ★seen を書く前に覚える★（2026-07-31・Codex17回目）
                #   あとで覚える形だと、その間に落ちたときに
                #   「既知のURLだが待ち行列にも無い」＝永久に消えた機種になる。
                kept = _remember_url(c.get("official_name") or "", url, mid,
                                     (c.get("release") or {}).get("value") or "",
                                     "見つけたばかり")
            else:
                out["problems"].append(f"{url}: " + " / ".join(c["reasons"]))
                # ★ここで取りこぼしていた★（2026-07-31・Codex16回目）
                #   このあと _save_seen で「見たことがあるURL」になるので、
                #   翌日はもう新台に出てこない。
                #   一晩だけページが取れなかっただけでも、その機種は永久に消えていた。
                #   あとで載る見込みがある理由なら、待ち行列に入れて毎日やり直す。
                if retry_later(c["reasons"]):
                    kept = _remember_url(
                        c.get("official_name") or "", url, mid,
                        (c.get("release") or {}).get("value") or "",
                        " / ".join(c["reasons"])[:300])
                else:
                    # ★やり直しても変わらない理由は、その場で1件ずつ台帳へ★
                    #   （2026-08-02・Codex26回目）まとめ登録は失敗を無視し
                    #   1500字で切るので、誤判定されたURLが台帳にも残らないまま
                    #   既知になり、**翌日から二度と出てこなかった**。
                    #   台帳に残せた時だけ既知にする（残せなければ明日また出てくる）。
                    kept = _ledger(
                        "site", "structural", "MATERIAL", "URL_PERMANENT_REJECT",
                        "新URLを記事化の対象から外しました（やり直しても変わらない理由）",
                        f"{url} / " + " / ".join(c["reasons"])[:900])
            if not kept:
                # ★どこにも残せなかったURLは「見た」ことにしない★
                #   （2026-07-31・Codex20回目）
                #   待ち行列にも台帳にも残らないまま既知にすると、
                #   翌日から新台に出てこない＝その機種は黙って消える。
                #   覚えないでおけば、明日もう一度あたらしいURLとして出てくる。
                _forget(seen, mid, url)
                out["problems"].append(
                    f"{url}: どこにも残せなかったので『見た』ことにしません")
    _nw._save_seen(seen)
    _log(f"見張り終了: 正常{len(out['watched'])}社 / 見られず{len(out['not_watched'])}社 "
         f"/ 新台候補{len(out['candidates'])}件 / 確認が要る{len(out['problems'])}件")
    return out


def gather(name: str) -> dict:
    """1機種ぶんの材料を集める。★止まった理由も返す★"""
    got = {"name": name, "urls": [], "model_code": None, "material": None,
           "problems": []}
    fr = _di.find(name)
    for did, v in fr["results"].items():
        if v["state"] != "FOUND":
            got["problems"].append(f"{did}: {v['state']} {v['why']}"[:160])
    got["urls"] = _di.found_urls(fr)
    _log(f"材料集め開始: {name} / 名鑑{len(got['urls'])}件 "
         + " ".join(f"{d}={v['state']}" for d, v in fr["results"].items()))
    if len(got["urls"]) < 2:
        got["problems"].append(
            f"名鑑の個別ページが {len(got['urls'])} 件しか見つかりません（2件以上が要る）")
        return got
    # ★出典どうしが転載でないか確かめる★（2026-07-31・実際に見つけた）
    #   やんちゃプレスはちょんぼりすたと本文が17行そのまま同じだった。
    #   登録簿に無い転載を2票に数えると、独立2出典の意味が無くなる。
    lin = _lc.check(got["urls"])
    for sp in lin["suspects"]:
        got["problems"].append(
            f"転載の疑い: {sp['a']} と {sp['b']} の本文が {sp['ratio']:.0%} 一致"
            f"（登録簿に系列が書かれていません）")
    mv = _mc.agree([_mc.lookup(u, name) for u in got["urls"]])
    got["model_code"] = mv.get("model_code")
    if not mv["adopted"]:
        got["problems"].append("型式名: " + str(mv.get("why", ""))[:160])
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
             "型式名の規格印が確認できません")
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
            "転載の疑い",   # ★登録簿に無い転載があれば止める★
            # ★★ここに入れ忘れていた★★（2026-07-31・Codex18回目）
            #   直したつもりで、書き換える場所を1つ手前と間違えていた。
            #   「文言を返す」ことだけを試験していたので、
            #   **run_one が記事を作るのを拒む**ところまで確かめていなかった。
            # 名簿を読めない＝メーカーが合っているとは言えない
            "メーカー名簿を読めません", "メーカーが指定されていません",
            "はまだ見張れていません", "の公式の場所が名簿にありません",
            # 新台でない機種を新台として出さない
            "登場年月が新台の範囲外です")


def _blocking(problems: list) -> list:
    return [p for p in problems if any(w in p for w in BLOCKING)]


def verify_official(name: str, official_url: str,
                    maker: str = "", release: str = "") -> dict:
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
        # 同じメーカーの中の転送（https化・スラッシュ補正）は普通に起きるので
        # それ自体は問題にしない。★範囲の外なら下の照合が止める★
        _log(f"  公式ページが転送されました: {official_url[:60]} → {final_url[:60]}")
    ok, why = _mc.page_is_machine(html, name, strict_tail=False)
    if not ok:
        out["problems"].append(
            f"公式ページと名前が一致しません（{why}）: "
            f"公式のタイトル={_nw.page_title(html)[:40]!r} / 指定名={name!r}")
    if maker:
        out["problems"] += _verify_maker(final_url, maker)
    else:
        out["problems"].append("メーカーが指定されていません")
    got = _nw.release_month(_nw._visible_text(html))
    if not got:
        out["problems"].append(
            "公式ページに登場年月が書かれていません（こちらで日付を補わない）")
        return out
    out["release"] = str(got.get("value") or "")
    if release and str(release) != out["release"]:
        out["problems"].append(
            f"登場年月が公式と違います（公式={out['release']} / "
            f"渡された値={release}）")
    if not _nw.is_recent(out["release"]):
        out["problems"].append(
            f"登場年月が新台の範囲外です（{out['release']}）")
    return out


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
    if not official_url.startswith(pre):
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
    """★名前や年月が空なら、公式ページをもう一度見る★

    取りこぼし対策で入れたURLは、名前が取れていないことがある。
    その状態では記事にできないので、毎回もう一度公式を見て埋める。
    **こちらで作らない。公式に書いていなければ空のまま**。
    """
    if work["name"] and work["release"]:
        return work
    try:
        c = _nw.classify(work["url"], None)
    except Exception as e:                # noqa: BLE001
        _log(f"  公式ページを見直せませんでした: {e}")
        return work
    work["name"] = work["name"] or (c.get("official_name") or "")
    work["release"] = work["release"] or ((c.get("release") or {}).get("value") or "")
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
    p = subprocess.run(
        ["git", "push",
         f"--force-with-lease=refs/heads/{sc['dest']}:{base_sha}",
         sc["remote"], f"HEAD:refs/heads/{sc['dest']}"],
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
            before_write=None) -> dict:
    """1機種を最後まで進める。"""
    out = {"name": name, "slug": None, "wrote": [], "problems": [], "blocked": []}
    _log(f"=== 機種の処理開始: {name} / {maker} / {release} / {official_url} "
         f"/ 書き込み={'する' if apply_it else 'しない'} ===")
    # ★①まず公式ページと名前が同じ機種を指しているか★
    vo = verify_official(name, official_url, maker, release)
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
    got = gather(name)
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
        _remember(name, official_url, maker, release, out["problems"])
        return out
    out["slug"] = _ba.slug_from_url(official_url)
    mat = got["material"]
    out["adopted"] = sorted(_sl.FIELDS[k]["jp"] for k in mat["adopted"])
    out["held"] = sorted(_sl.FIELDS[k]["jp"] for k in mat["need_third"])
    out["thin"] = sorted(_sl.FIELDS[k]["jp"] for k in mat["thin"])
    if not mat["adopted"]:
        out["problems"].append("採用できた材料がありません（記事を作りません）")
    # ★②同定に関わる問題があれば、材料が採れていても作らない★
    out["blocked"] = _blocking(out["problems"])
    if out["blocked"] or not mat["adopted"]:
        for b in out["blocked"]:
            _log(f"  ★止めました: {b[:140]}")
        _remember(name, official_url, maker, release, out["problems"])
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
        g = gather("X")
        t("★見つからない名鑑があっても、理由を残して進む★",
          len(g["urls"]) == 1 and any("HEALTHY_NO_MATCH" in p for p in g["problems"]))
        t("★★名鑑が1件だけなら材料を集めに行かない★★（2件以上が要る）",
          g["material"] is None
          and any("2件以上" in p for p in g["problems"]))

        _di.find = lambda n, c=None: {"results": {
            k: {"state": "FOUND", "url": f"https://{k}.example/1", "why": "",
                "candidates": [], "surfaces": "1/1", "index_size": 9, "problems": []}
            for k in ("a", "b")}}
        _mc.lookup = lambda u, n: {"url": u, "model_code": "C1", "reason": "OK"}
        _sl.read_page = lambda u, n: {
            "url": u, "host": u.split("/")[2], "ok": True, "reason": "OK",
            "fields": {"payout_rate": {"1": "97.3%"}}}
        g2 = gather("X")
        t("　2件そろえば型式名と材料を集める",
          g2["model_code"] == "C1" and g2["material"] is not None)

        # ★公式ページは本物を想定して差し替える★
        #   （開けなければ止まる作りなので、通る場合の試験には中身が要る）
        real_get = _nw._get
        _nw._get = lambda u, timeout=20: (
            "<title>X</title><body>2026年9月 登場</body>")
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
        _old = run_one("X", "https://m.example/products/slot/zzz/", "m", "")
        t("★★古い機種は記事そのものを作らない★★（通しで確かめる・Codex18回目）",
          "preview" not in _old and _old["wrote"] == []
          and any("範囲外" in x for x in _old["blocked"]))
        _nw._get = lambda u, timeout=20: (
            "<title>X</title><body>2026年9月 登場</body>")
        _bad = run_one("X", "https://m.example/products/slot/zzz/", "nosuch", "2026-09")
        t("★★名簿に無いメーカーでは記事そのものを作らない★★（通しで確かめる）",
          "preview" not in _bad and _bad["wrote"] == []
          and any("名簿" in x for x in _bad["blocked"]))
        _real_cats2 = _nw.CATALOGS
        _nw.CATALOGS = os.path.join(_tmpdir, "こわれている.json")
        with open(_nw.CATALOGS, "w", encoding="utf-8") as _f:
            _f.write("{ こわれた")
        _brk = run_one("X", "https://m.example/products/slot/zzz/", "m", "2026-09")
        _nw.CATALOGS = _real_cats2
        t("★★名簿を読めないときも記事を作らない★★"
          "（読めない＝合っているとは言えない・Codex18回目）",
          "preview" not in _brk and _brk["wrote"] == []
          and any("名簿" in x for x in _brk["blocked"]))

        r = run_one("X", "https://m.example/products/slot/zzz/", "m", "2026-09")
        t("★既定では書き込まない（dry-run）★", r["wrote"] == [])
        t("　組み立てた結果を返す（中身を見てから書ける）",
          r["preview"]["machine"]["status"] == "preview")
        t("　slugは公式URLから作る", r["slug"] == "zzz")

        _sl.read_page = lambda u, n: {"url": u, "host": u.split("/")[2], "ok": True,
                                      "reason": "OK", "fields": {}}
        r2 = run_one("X", "https://m.example/products/slot/zzz/", "m", "2026-09")
        t("★★材料がゼロなら記事を作らない★★",
          "preview" not in r2 and any("採用できた材料" in p for p in r2["problems"]))

        # -------- Codexの反例（2026-07-31・自分で再現を確認してから修正）
        _sl.read_page = lambda u, n: {
            "url": u, "host": u.split("/")[2], "ok": True, "reason": "OK",
            "fields": {"payout_rate": {"1": "97.3%"}}}
        _mc.lookup = lambda u, n: {"url": u, "model_code": None,
                                   "reason": "MODEL_CODE_NOT_FOUND"}
        r3 = run_one("X", "https://m.example/products/slot/zzz/", "m", "2026-09")
        t("★★型式名が確定していなければ記事を作らない★★"
          "（材料が採れていても作れてしまう穴があった）",
          "preview" not in r3 and any("型式名" in x for x in r3["blocked"]))

        _mc.lookup = lambda u, n: {"url": u, "model_code": "C1", "reason": "OK"}
        _di.find = lambda n, c=None: {"results": {
            "a": {"state": "FOUND", "url": "https://a.example/1", "why": "",
                  "candidates": [], "surfaces": "1/1", "index_size": 9, "problems": []},
            "b": {"state": "FOUND", "url": "https://b.example/1", "why": "",
                  "candidates": [], "surfaces": "1/1", "index_size": 9, "problems": []},
            "c": {"state": "AMBIGUOUS_CANDIDATES", "url": None, "why": "候補が3件",
                  "candidates": [1, 2, 3], "surfaces": "1/1", "index_size": 9,
                  "problems": []}}}
        r4 = run_one("X", "https://m.example/products/slot/zzz/", "m", "2026-09")
        t("★★1つでも候補を絞れない名鑑があれば記事を作らない★★",
          "preview" not in r4
          and any("AMBIGUOUS" in x for x in r4["blocked"]))

        real_get, real_page = _nw._get, _mc.page_is_machine
        try:
            _nw._get = lambda u, timeout=20: "<title>ぜんぜん別の機種</title>"
            _mc.page_is_machine = real_page
            v = verify_official(
                "Lすーぱぁびん娘",
                "https://m.example/products/slot/other/")["problems"]
            t("★★公式ページが別機種なら止める★★"
              "（機種Aの名前＋機種BのURLで記事ができた穴・実際に再現した）",
              v and "一致しません" in v[0])
            _nw._get = lambda u, timeout=20: "<title>Lすーぱぁびん娘|BELLCO</title>"
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
            r5 = run_one("X", "https://m.example/products/slot/zzz/", "m", "2026-09")
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
                "<title>Lすーぱぁびん娘|BELLCO</title><body>2026年9月 登場</body>")
            t("　同じ機種なら通る",
              verify_official("Lすーぱぁびん娘", "https://m.example/products/slot/lbinko/",
                              "m")["problems"] == [])
            t("★★登場年月を渡さなくても、公式から必ず取って確かめる★★"
              "（空で渡せば検査ごと飛ばせた・Codex17回目）",
              verify_official("Lすーぱぁびん娘",
                              "https://m.example/products/slot/lbinko/",
                              "m")["release"] == "2026-09")
            _nw._get = lambda u, timeout=20: (
                "<title>Lすーぱぁびん娘|BELLCO</title><body>2019年4月 登場</body>")
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
    ap.add_argument("--official-url", dest="official_url")
    ap.add_argument("--maker")
    ap.add_argument("--release", default="")
    args = ap.parse_args()
    if args.selftest:
        return selftest()

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
    d = discover()
    for x in d["first_time"]:
        print("初回として記録:", x)
    # ★見つけたが記事にできていない機種を、必ず待ち行列に入れる★
    # ★候補は discover() の中で、seen を書く前に待ち行列へ入れてある★
    pend = _pend.load()
    # ★待ちすぎた分は黙って消さず、台帳に残す★
    for it in _pend.give_up(pend):
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
    _pend.save(pend)
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
        if not (work["name"] and work["maker"]):
            _log(f"  まだ記事にできません（名前かメーカーが取れない）: {work['url']}")
            # ★早く抜けるときも試した日を残す★（残さないと毎晩ここで詰まる）
            _pend.mark_tried(pend, work["url"])
            _pend.save(pend)
            continue
        _log(f"試す: {work['name']} / {work['maker']} / {work['release']}")
        # ★試したことを必ず残す★（残さないと同じものばかり選ばれる）
        _pend.mark_tried(pend, work["url"])
        _pend.save(pend)
        res = run_one(work["name"], work["url"], work["maker"],
                      work["release"], apply_it,
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
            give_up_now(pend, work["url"], work["name"], res["problems"])
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

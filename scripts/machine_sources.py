# -*- coding: utf-8 -*-
"""機種ごとの「この機種のページはここ」という控え。

★何のためにあるか★（2026-08-07・台帳#265）
  情報を1つ決めるには「大手2つが同じことを書いている」ことが要る。
  ところが名鑑（一覧ページ）から機種名で引く方法では、表記が違う機種を
  引き当てられない。実データで全121機種のうち38機種が2つに届かなかった。
    ・スマスロ防振り        ↔ 痛いのは嫌なので防御力に極振りしたいと思います
    ・SBニューキングハナハナV-30 ↔ ニューキングハナハナV-30
    ・LB不二子BT            ↔ 不二子BT
  これは「同じ機種か」という**意味の判断**なので、機械の照合では届かない。

★そこでAIに探させる。ただし守る線が1本ある★
  ┌────────────────────────────────────────────────┐
  │ AIが挙げたURLは、機械が実際に取ってきて          │
  │ 中身を確かめるまで採用しない                     │
  └────────────────────────────────────────────────┘
  過去にCodexが挙げたURLが404だった実例がある。この線さえ引いておけば、
  AIが間違ったURLを出しても無害＝存在しなければ落ち、別機種のページなら
  中身を読んだ時点で外れる。嘘のURLが記事に化ける経路が無い。

★探す先は登録済みの大手サイトだけ★（source-registry.json・default deny）
  「2つの出典が一致したら採用」の"2つ"を数えるには、その2つが本当に
  別系列かを知っている必要がある（P-WORLDと羽伏せは同じ系列で1票）。
  知らないサイトが出てくると独立性を数えられないので、票にできない。

★一度決めたら控えに残る★
  次からはAIを呼ばず機械が読むだけ。費用は機種あたり1回きり。

★使い方★
  # ①機械の検査（AIに渡す材料を出す。ここでは何も記録しない）
  python scripts/machine_sources.py --check --slug bofuri --url https://...
  # ②AIが「同じ機種だ」と判断したら記録する
  python scripts/machine_sources.py --record --slug bofuri --url https://... \
      --why "正式名称の略称。型式が一致" --by claude
  #   ★名前の形が合わないときは、判断した理由を書いて明示的に上書きする★
  #      --override-identity "略称なので題は一致しないが、型式番号が一致"
  # ③控えを見る / 手当てが要る機種を並べる
  python scripts/machine_sources.py --list [--slug bofuri]
  python scripts/machine_sources.py --missing
  python scripts/machine_sources.py --selftest
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import errno
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import claim_identity as _ci          # noqa: E402
import model_code_lookup as _mc       # noqa: E402
import new_machine_watch as _w        # noqa: E402
import safe_json as _sj               # noqa: E402
import source_lineage as _sl          # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY = os.path.join(BASE, "assets", "data", "source-registry.json")
MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
# ★控えはリポジトリの外★（release_overrides.json と同じ置き場）
#   公開物に他サイトのURL一覧を混ぜないため。控えはDropboxへ保全する。
import os as _os_lp                 # noqa: E402
import sys as _sys_lp               # noqa: E402
_sys_lp.path.insert(0, _os_lp.path.dirname(_os_lp.path.abspath(__file__)))
import local_paths as _lp           # noqa: E402
STORE = _lp.doc("machine_sources.json")

SCHEMA = "machine-sources/v1"


class SourceError(Exception):
    """控えに関する異常（★迷ったら記録しない★）。"""


# ---------------------------------------------------------------- 出典の台帳

def _publishers() -> dict:
    """ホスト名 → (発行者ID, 系列ID) の対応。★ACTIVE だけ★"""
    reg = _sj.read_json(REGISTRY, expect=dict)
    out = {}
    for pid, p in (reg.get("publishers") or {}).items():
        if p.get("status") != "ACTIVE":
            continue
        for h in p.get("canonical_hosts") or []:
            out[str(h).lower()] = (pid, p.get("content_lineage_id") or "")
    return out


def publisher_of(url: str, pubs: dict | None = None):
    """URLの発行者と系列を返す。★登録が無ければ (None, None)★"""
    host = urllib.parse.urlsplit(str(url or "")).hostname or ""
    return (pubs if pubs is not None else _publishers()).get(host.lower(),
                                                             (None, None))


# ---------------------------------------------------------------- 控えの読み書き

def _empty() -> dict:
    return {"schema_version": SCHEMA, "machines": {}}


def load() -> dict:
    if not os.path.exists(STORE):
        return _empty()
    got = _sj.read_json(STORE, expect=dict)
    if got.get("schema_version") != SCHEMA:
        raise SourceError(f"控えの形が違います: {got.get('schema_version')}")
    got.setdefault("machines", {})
    return got


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    # ★一時ファイルの名前を実行ごとに変える★（2026-08-10・依頼137のP1-1）
    #   同じ名前を使っていたので、2つの実行が同時に書くと
    #   **片方が書いた一時ファイルを、もう片方が控えとして公開する**形だった。
    tmp = "%s.tmp.%d" % (STORE, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        os.replace(tmp, STORE)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


_LOCK_WAIT = 15.0        # 鍵が空くのを待つ最大の秒数

# ★「いま他の実行が使っている」を表す番号だけ待つ★（2026-08-10・依頼139のP3）
#   どんな異常でも「使用中」と扱うと、壊れた記述子や使えない置き場のときに
#   15秒待たされたうえ、間違った案内が出る。原因の違う異常はすぐ知らせる。
_BUSY = {getattr(errno, n) for n in
         ("EACCES", "EAGAIN", "EWOULDBLOCK", "EDEADLOCK", "EDEADLK")
         if hasattr(errno, n)}


def _hold(fh, on: bool) -> None:
    """★OSに鍵を持たせる★（プロセスが死んだらOSが必ず外す）"""
    fh.seek(0)
    try:
        import msvcrt                        # Windows
        msvcrt.locking(fh.fileno(),
                       msvcrt.LK_NBLCK if on else msvcrt.LK_UNLCK, 1)
        return
    except ImportError:
        pass
    import fcntl                             # それ以外
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB if on else fcntl.LOCK_UN)


@contextlib.contextmanager
def _lock():
    """★控えを書くあいだの鍵★（2026-08-10・依頼137のP1-1 → 依頼138で作り直し）

    読み直して比べるだけでは、**比べてから置き換えるまでのごく短い隙**に
    別の実行が書いた分を消せる。消えるのが隔離や削除だと影響が大きい
    （誤同定を理由に外した出典が復活しうる）ので、鍵で囲む。
    ★ネットへ取りに行くのは鍵の外★（鍵を長く握らない）。

    ★「古い鍵を消して奪う」方式はやめた★（2026-08-10・依頼138のP1）
      持ち主が生きているか確かめずに消していたうえ、**持ち主のほうも
      自分の鍵か確かめずに消して**いた。そのため
        ①Aが止まって120秒を超える ②BがAの鍵を消して取る
        ③Aが再開してBの鍵を消す ④CがBの書き込み中に取る
      という二重取得が成立した。
      いまはOSに持たせる（プロセスが死ねばOSが外す）ので、時間で奪う必要が無い。
      ★鍵のファイルは消さない★＝消すと別の実行が握っている鍵を壊す。
    """
    path = STORE + ".lock"
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    started = time.time()
    with open(path, "a+b") as fh:
        while True:
            try:
                _hold(fh, True)
                break
            except OSError as e:
                if e.errno not in _BUSY:
                    # ★「使用中」とは別の異常★（待っても直らないので即やめる）
                    raise SourceError("控えの鍵を扱えません（%s）" % e)
                if time.time() - started > _LOCK_WAIT:
                    raise SourceError(
                        "控えが他の実行に使われています"
                        "（あとでやり直してください）")
                time.sleep(0.15)
        try:
            yield
        finally:
            try:
                _hold(fh, False)
            except OSError:
                pass                        # 閉じればOSが外す


# ★「書かずに結果だけ返す」印★（条件が合わなかったときに使う）
NO_WRITE = "_nowrite"


def _now() -> str:
    """★いつ書いたか（時刻まで）★＝同じ日の複数回を見分けるため（依頼136）"""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _raw() -> bytes | None:
    try:
        with open(STORE, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def _update(change, tries: int = 6):
    """★読んだときから変わっていなければ書く★（2026-08-10・依頼136のP1-3）

    控えは丸ごと読んで丸ごと書き戻す。`os.replace` は壊れた途中のファイルは
    防ぐが、**2つの実行が同じ古い中身を読んで順に書く**と、あとの1つが
    先の書き込みを消す。隔離（＝使わないという印）が消えると、
    次の実行では未隔離として本文が使われてしまう。
    そこで、書く直前にファイルが読んだときのままかを確かめ、
    変わっていたら読み直してやり直す。
    """
    with _lock():
        for _ in range(tries):
            base = _raw()
            data = load()
            out = change(data)
            if out is None:
                return None                 # 変えるものが無かった＝1文字も書かない
            if out.get(NO_WRITE):
                return out                  # 条件が合わない＝書かずに知らせる
            # ★鍵があるので普通はここで一致する★（鍵を無視する実行への備え）
            if _raw() == base:
                _save(data)
                return out
    raise SourceError("控えが同時に書き換えられています（やり直してください）")


def _update_one(slug: str, url: str, change):
    """控えの1件だけを、読み直してから書き換える。"""
    def _do(data):
        for rec in (data.get("machines") or {}).get(slug) or []:
            if rec.get("url") == url:
                # ★change は必ず結果を返すこと★（None は「書かない」の合図）
                return change(rec)
        return None                          # 見つからない＝1文字も書かない
    got = _update(_do)
    return got if got is not None else {"state": "NOT_FOUND"}


def urls_for(slug: str, data: dict | None = None) -> list:
    """控えに入っている、この機種の出典（機械が毎日読む側）。"""
    d = data if data is not None else load()
    return list((d.get("machines") or {}).get(slug) or [])


# ---------------------------------------------------------------- 機種の情報

def machine(slug: str) -> dict:
    """その機種の情報。★記事がまだ無い新台は待ち行列から引く★

    ★なぜ要るか（2026-08-13・台帳#347）★
      新台は「記事を作る材料を集める段階」で出典の同定が要ります。
      ところが控えは machines.json に載っている機種にしか書けなかったので、
      **2AIが一度出した結論を毎晩捨てて**同じ判断をやり直していました。

      実例: L ソードアート・オンライン オルタナティブ ガンゲイル・オンライン。
      なな徹の題が略称（ガンゲイルオンライン）で機械の照合を通らず、
      2AIが本文（正式名称とメーカーが完全一致）を読んで同じ機種と判断したのに、
      控えへ登録できず毎晩ナナプレスを除外したまま材料集めをやり直していた。

    ★名前を自己申告させない★
      待ち行列（add_machine_pending.json）に入っている名前とURLを使い、
      **slugはP-WORLDのURLから決めたもの**と突き合わせます。
    """
    ms = _sj.read_json(MACHINES, expect=(dict, list))
    ms = ms["machines"] if isinstance(ms, dict) else ms
    for m in ms:
        if m.get("slug") == slug:
            return m
    got = _pending_machine(slug)
    if got:
        return got
    raise SourceError(f"機種が見つかりません: {slug}"
                      "（記事にも、新台の待ち行列にもありません）")


class PendingUnreadable(SourceError):
    """新台の待ち行列を読めない（★「無い」と混ぜない★）。"""


def _pending_machine(slug: str) -> dict:
    """待ち行列の中の、まだ記事になっていない新台（無ければ空）。"""
    try:
        import build_new_article as _ba
        import pending_machines as _pm
        items = (_pm.load() or {}).get("items") or {}
        # ★待ち行列はURLをかぎにした組★（並びで来ても読めるようにしておく）
        items = list(items.values()) if isinstance(items, dict) else list(items)
    except FileNotFoundError:
        return {}                         # ★待ち行列がまだ無い＝素通り★
    except Exception as e:                # noqa: BLE001
        # ★「読めない」を「無い」にしない★（2026-08-14・依頼200のP2）
        #   壊れた待ち行列を「存在しない」と扱うと、
        #   生きている新台の控えまで置き去り（ORPHANED）と誤判定し、
        #   **見張りが黙って止まる**。読めないことは読めないと言う。
        raise PendingUnreadable(f"待ち行列を読めません: {str(e)[:80]}")
    for it in items:
        if not isinstance(it, dict):
            continue
        url = str(it.get("url") or "")
        if not url:
            continue
        # ★移行した公開済み機種も見つけられるようにする★
        #   （2026-08-16・台帳#376）URLがDMMへ変わった7機種は、
        #   URLから作り直すと `dmm_*` になり `pw_*` の控えと結びつかない。
        import slug_binding as _sb
        if not _sb.check(slug, url)[0]:
            continue
        name = str(it.get("name") or "").strip()
        if not name:
            # ★居るのに名前が空＝「無い」ではない★（2026-08-14・依頼201のP2）
            #   待ち行列は名前なしでも覚える作りなので、これは普通に起きる。
            #   {} を返すと「待ち行列にも居ない」＝置き去り扱いになり、
            #   生きている新台の控えが巡回から外れる。
            raise PendingUnreadable(
                f"待ち行列に居ますが名前がありません（{slug}）"
                "／同定できないので使いませんが、置き去りにもしません")
        return {"slug": slug, "name": name, "_pending": True,
                "identity": {"official_product_url": url}}
    return {}


def _text_of(html: str) -> str:
    return " ".join(_w._visible_text(html).split())


def url_key(u: str) -> str:
    """URLを比べるための形。★同じページを別物と数えない★（依頼136のP0-1）

    末尾の `/` や `www.` の有無が違うだけで「別のURL」と見なすと、
    名鑑側が先にそのページを読んでしまい、控えの同定を素通りする。
    ★問い合わせ文字列（?以降）は残す★＝別のページを指すことがある。
    ★そろえるのは「その方式で省略できるポート」だけ★（依頼138のP2）
      80を省略できるのは http、443を省略できるのは https のときだけ。
      `https://…:80/` や `:8443` は**別の窓口**なので、同じにしない。
    """
    p = urllib.parse.urlsplit(str(u or "").strip())
    host = (p.hostname or "").lower().removeprefix("www.")
    default = {"http": 80, "https": 443}.get((p.scheme or "https").lower())
    port = ":%d" % p.port if p.port and p.port != default else ""
    path = p.path.rstrip("/") or "/"
    return "%s%s%s?%s" % (host, port, path, p.query)


def _title_key(title: str) -> str:
    """題を比べるための形。★全半角と空白の違いだけを吸収する★"""
    return " ".join(
        unicodedata.normalize("NFKC", str(title or "")).split()).casefold()


def _title_fp(title: str) -> str:
    """題の指紋。★保存した題そのものを比べない★

    控えに残す題は人が読む用に120字で切ってある。切った文字どうしを
    比べると、長い題（P-WORLDは200字近い）が毎回食い違う。
    比べるのは**全文の指紋**、見せるのは切った題、と役割を分ける。
    """
    return hashlib.sha256(_title_key(title).encode("utf-8")).hexdigest()


def _model_code_of(html: str):
    """このページに書かれた型式を1つ返す（★値だけ★）。

    ★2026-08-10に見つけた取り違え★
      `extract_model_code()` は必ず **(値, 理由) の組** を返す。
      組は中身が None でも真になるので、`if got["model_code"]` は常に真、
      `str(...)` は `"(None, 'MODEL_CODE_NOT_FOUND')"` という文字列になる。
      手がかりとして保存する直前でこの形になっていた（実データはまだ無し）。
    """
    got = _mc.extract_model_code(html)
    val = got[0] if isinstance(got, tuple) else got
    val = str(val).strip() if val else ""
    return val or None


# ---------------------------------------------------------------- 機械の検査

def check(slug: str, url: str, html: str | None = None,
          pubs: dict | None = None) -> dict:
    """★AIに渡す材料を作る（記録はしない）★

    機械にできることだけを見る：
      ①登録済みの発行者か ②取れるか ③別のホストへ飛ばされないか
      ④中身から題・見出し・型式・機種名の出方を抜き出す
    「同じ機種か」は**ここでは決めない**（それがAIの仕事）。
    """
    m = machine(slug)
    name = str(m.get("name") or "")
    out = {"slug": slug, "name": name, "url": url, "final_url": url,
           "ok": False,
           "problems": [], "publisher": None, "lineage": None,
           "title": "", "headings": [], "model_code": None,
           "name_core": _ci.normalize_core(name),
           "identity_verdict": None, "identity_why": "",
           "excerpt": "", "text_sha256": "", "text_len": 0,
           "already_recorded": False, "same_lineage_already": []}

    pid, lin = publisher_of(url, pubs)
    if not pid:
        out["problems"].append(
            "登録されていないサイトです（票に数えられないので使いません）")
        return out
    out["publisher"], out["lineage"] = pid, lin

    if html is None:
        try:
            # ★用途を名乗ってから取りに行く★（2026-08-16・依頼218）
            #   控えが読むのは記事のページ＝材料なので `claim_material`。
            #   名乗り漏れで控えが全滅していた（2026-08-17に発覚）。
            with _w.fetching("claim_material"):
                html = _w._get(url)
        except Exception as e:              # noqa: BLE001
            out["problems"].append(f"取得できません（{e}）")
            return out
        final = _w.LAST_FINAL_URL.get("url") or url
        out["final_url"] = final
        bad = _w.redirect_problem(url, final)
        if bad:
            out["problems"].append(f"転送されました（{bad}）")
            return out
        if publisher_of(final, pubs)[0] != pid:
            out["problems"].append("別のサイトへ飛ばされました")
            return out
    why = _w.bad_page(html)
    if why:
        out["problems"].append(f"一覧・記事のページではありません（{why}）")
        return out

    text = _text_of(html)
    out["title"] = _w.page_title(html) or ""
    out["headings"] = [h[:80] for h in _w._visible_h1s(html)][:5]
    out["model_code"] = _model_code_of(html)
    out["text_len"] = len(text)
    out["text_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    out["excerpt"] = text[:400]
    ok, reason = _mc.page_is_machine(html, name)
    out["identity_verdict"], out["identity_why"] = bool(ok), str(reason or "")

    if not out["title"]:
        out["problems"].append("題がありません（機種のページか確かめられません）")
        return out
    if out["text_len"] < 200:
        out["problems"].append("本文がほとんどありません")
        return out

    data = load()
    # ★票が増えるかどうかは、保存してある系列名ではなく今のレジストリで数える★
    #   （2026-08-09・依頼125）保存値を信じると、レジストリで統合された後も
    #   古い名前のまま「別系列＝もう1票」に見えてしまう。
    try:
        my_key = _sl.vote_key(pid)
    except _sl.LineageError:
        my_key = None
    for rec in urls_for(slug, data):
        if rec.get("url") == url:
            out["already_recorded"] = True
            continue
        try:
            same = my_key is not None and _sl.vote_key(rec.get("publisher")) == my_key
        except _sl.LineageError:
            same = False
        if same:
            out["same_lineage_already"].append(rec.get("url"))
    out["ok"] = True
    return out


# ---------------------------------------------------------------- 記録

# ★判断してよいのはこの3者だけ★（2026-08-09・依頼125）
#   誰の判断かを後から追えないと、間違いを見つけたときに取り消す範囲を決められない。
JUDGES = ("claude", "codex", "運営者")
MIN_WHY = 8          # 「x」の一文字で通っていたので、文になる長さを求める


def _judgement(why: str, by: list) -> list:
    """★誰がなぜ判断したかを必ず書かせる★（記録も承認も同じ条件で通す）"""
    if len(str(why or "").strip()) < MIN_WHY:
        raise SourceError(
            "--why（なぜ同じ機種と判断したか）は%d文字以上で書きます" % MIN_WHY)
    who = [x for x in (by or []) if x]
    if not who:
        raise SourceError("--by（誰が判断したか）は必ず書きます")
    bad = [x for x in who if x not in JUDGES]
    if bad:
        raise SourceError("判断者に使えない名前です（%s のみ）: %s"
                          % ("/".join(JUDGES), ",".join(bad)))
    return who


def record(slug: str, url: str, why: str, by: list,
           override_identity: str = "", checked: dict | None = None) -> dict:
    """★検査を通ったものだけを控えに残す★（fail-closed）"""
    who = _judgement(why, by)
    got = checked if checked is not None else check(slug, url)
    # ★検査したものと記録するものが同じか確かめる★（2026-08-09・依頼125）
    #   ここが無いと、別の機種・別のURLを検査した結果を持ってきて
    #   「機械が取ってきた」という唯一の守る線を越えられる。
    if got.get("slug") != slug or got.get("url") != url:
        raise SourceError(
            "検査したものと記録するものが違います（検査=%s/%s 記録=%s/%s）"
            % (got.get("slug"), got.get("url"), slug, url))
    if not got["ok"]:
        raise SourceError("機械の検査を通りません: " + " / ".join(got["problems"]))
    if got["already_recorded"]:
        return {"state": "ALREADY", "url": url}
    # ★名前の形が合わないときは、判断した理由を明示的に書かせる★
    #   黙って通すと「近いから同じでいいや」が積み上がる。
    if not got["identity_verdict"] and not str(override_identity or "").strip():
        raise SourceError(
            "題が機種名と一致しません（" + got["identity_why"] + "）。"
            "同じ機種だと判断したなら --override-identity に理由を書きます")
    if not got["identity_verdict"]:
        # ★★題で分からないものは、2AIがそろって一致したときだけ登録する★★
        #   （2026-08-11・運営者のルール「2AIの一致でいいじゃん」・依頼153の①）
        #   片方だけの判断で通せると、**最初の誤登録は誰も検出できない**
        #   （以後の確認は「登録時と同じページか」しか見ないため）。
        if not ({"claude", "codex"} <= set(who)):
            raise SourceError(
                "題で分からない出典は、claude と codex の両方が一致したときだけ"
                "登録できます（--by claude,codex）。いまの判断者: "
                + ",".join(who))

    rec = {
        "url": url,
        # ★どこから来た機種か★（2026-08-14・台帳#350）
        #   記事がまだ無い新台の控えは、待ち行列が打ち切られると
        #   **誰も使わないのに巡回だけされ続ける**。印を残しておき、
        #   読むときに「いまも生きているか」を見る。
        "origin": ("pending" if machine(slug).get("_pending") else "machine"),
        "publisher": got["publisher"],
        "lineage": got["lineage"],
        "title": got["title"][:120],
        "decided_at": datetime.date.today().isoformat(),
        # ★時刻まで残す★（同じ日の複数回・書き込み順を後から追えるように）
        "recorded_at": _now(),
        "decided_by": who,
        "why": str(why).strip()[:300],
        # ★どの中身を見て決めたか★（後から同じものを見たか確かめられる）
        "text_sha256": got["text_sha256"],
        "identity_verdict": got["identity_verdict"],
        # ★このページが「その機種のページ」だと言える手がかり★
        #   （2026-08-09・依頼125）本文は日々変わるが、これが変わったときは
        #   別の機種のページに差し替わった疑いがある＝人がもう一度見る。
        "identity_marks": _marks_of(got),
    }
    if override_identity:
        rec["override_identity"] = str(override_identity).strip()[:300]

    def _change(data):
        rows = data.setdefault("machines", {}).setdefault(slug, [])
        if any(r.get("url") == url for r in rows):
            return {"state": "ALREADY", "url": url}   # 読み直したら先に入っていた
        rows.append(rec)
        return {"state": "RECORDED", "url": url, "lineage": got["lineage"],
                "sources_now": len(rows)}
    return _update(_change)


def forget(slug: str, url: str, why: str, by: list) -> dict:
    """判断が間違っていたときに控えから外す（★人の操作専用★）。

    ★「誰がなぜ」を書かないと外せない★（2026-08-10・依頼138のP2）
      外した記録は復元できない。残せるのは帰属だけなので、そこは必ず書かせる。
    """
    _judgement(why, by)

    def _change(data):
        rows = urls_for(slug, data)
        left = [r for r in rows if r.get("url") != url]
        if len(left) == len(rows):
            return None                     # 見つからない＝1文字も書かない
        gone = [r for r in rows if r.get("url") == url]
        data["machines"][slug] = left
        if not left:
            data["machines"].pop(slug, None)
        # ★外したことも残す★（2026-08-10・依頼137のP2）
        #   消した記録は復元できないので、いつ誰がなぜ外したかだけは残す。
        log = data.setdefault("removed", [])
        log.append({"slug": slug, "url": url, "at": _now(),
                    "by": list(by), "why": str(why).strip()[:300],
                    "title": str((gone[0] if gone else {}).get("title")
                                 or "")[:120]})
        del log[:-200]                      # 増えすぎないように直近だけ残す
        return {"state": "FORGOTTEN", "sources_now": len(left)}
    got = _update(_change)
    return got if got is not None else {"state": "NOT_FOUND"}


# ------------------------------------------------------- ★使う瞬間の同定★
#
# ★なぜ要るか（2026-08-10・台帳#292＝Codex依頼125のP0-2）★
#   控えは「このURLはこの機種のページだ」と一度人が確かめた記録だが、
#   **使うときは保存したURLをそのまま読むだけ**だった。保存したsha256は
#   記録用にしか使っていない。登録のあとにそのページが別機種へ差し替わっても
#   誰も気づかない＝別機種の天井値をこの機種の記事に書く経路が開いていた。
#
# ★どう守るか★
#   読む前に毎回「同じページのままか」を機械が確かめ、変わっていたら**使わない**。
#   ★今のページを黙って正としない★＝直すのは人の仕事（隔離して台帳へ）。
#
# ★何を手がかりにするか★
#   題（の指紋）と型式。本文は日々変わるので使わない。
#     ・題が同じ                     → 同じページ
#     ・題は違うが型式が一致          → 同じページ（飾りの文言が変わっただけ）
#     ・型式が食い違う                → ★題が同じでも別の機種★
#     ・手がかりが何も無い            → 使わない（fail-closed）

CHECK_OK = "OK"              # 同じページのまま。使ってよい
CHECK_CHANGED = "CHANGED"    # 別のページに変わった疑い。★使わない・人が見る★
CHECK_UNUSABLE = "UNUSABLE"  # 今日は読めない（取得失敗・記事でない）


def _marks_of(got: dict) -> dict:
    """このページが「その機種のページ」だと言える手がかり。"""
    return {
        "final_url": got.get("final_url") or got.get("url"),
        "title": str(got.get("title") or "")[:120],     # 人が読む用
        # ★比べるのは指紋★（保存する題は切ってあるので文字どうしは比べられない）
        "title_fp": _title_fp(got.get("title")),
        "headings": [str(h)[:80] for h in (got.get("headings") or [])][:3],
        "model_code": got.get("model_code") or None,
    }


def directory_page_ok(name: str, html: str):
    """★名鑑で見つけたページが、本当にその機種のページか★（2026-08-11・台帳#309）

    名鑑は**一覧のリンク文字だけ**で機種を決めていて、個別ページの中身を見て
    いなかった。リンク文字が古い機種名のまま中身が別機種に差し替わると、
    そのまま材料になる。

    ★機械がやるのは「題で確かめる」ところまで★（2026-08-11・運営者の指摘）
      ①題で同定できる → 通す
      ②できない → **通さない。候補として残す**（＝2AIが本文を読んで判断する）

    ★②で機械の判定を足さない★
      いちど「本文に正式名称があれば通す」「機種名ラベルと完全一致なら通す」を
      作りかけたが、どちらも**別機種を通す穴が残る**うえ、発行者ごとに
      書き方が違うので「この場合、この場合…」の場合分けが増えていく。
      ★機械で取れないものは2AIで取る★＝そのために2AIを組ませている。
      2AIが「同じ機種だ」と判断したら控え（machine_sources）へ登録すれば、
      以後は機械が手がかり（題の指紋・見出し・型式）で毎回確かめられる。

    ★実データ★ 8機種29ページのうち6ページが①で落ちる（題が通称のため）。
      6件とも正しいページだったので、2AIが見て控えへ登録する対象になる。

    返すもの: (通ってよいか, 理由)
    """
    ok, why = _mc.page_is_machine(html, name)
    if ok:
        return True, "題で確認"
    return False, why


def _headings_same(marks: dict, now: dict):
    """記録した見出しが、今もこのページに出ているか。

    返すもの: True=出ている / False=消えた / None=記録が無くて比べられない
    """
    old = [_title_key(h) for h in (marks.get("headings") or []) if str(h).strip()]
    if not old:
        return None
    new = {_title_key(h) for h in (now.get("headings") or [])}
    if not new:
        return False          # 見出しが1つも読めない＝別のページになった疑い
    # ★保存した見出しは全部そろっていること★（2026-08-10・依頼136のP0-2）
    #   先頭1本だけを見ていたので、「最初の見出しは同じで、あとが別機種に
    #   差し替わった」形が通っていた。「全部そろう」と書いた説明と実装がずれていた。
    return all(h in new for h in old)


def _same_page(rec: dict, now: dict):
    """保存した手がかりと今のページを比べる。★迷ったら使わない★

    ★手がかり1つでは足りない★（2026-08-10・依頼135のP0-1を再現して直した）
      題の指紋が合えばそれだけで通していたので、
        ①題を残したまま中身を別機種へ差し替える形
        ②保存時にあった型式が**読めなくなった**形（同定の根拠を失っている）
      が素通りしていた。いまは**保存してある手がかりが全部そろうこと**を求める。

    ★見抜けないもの（正直に書いておく）★
      題も見出しも同じまま本文だけ別機種になった場合は、ここでは分からない。
      それは意味の判断なので、2AIの突き合わせ側の仕事。
    """
    marks = rec.get("identity_marks") or {}
    if not marks.get("title_fp"):
        # ★手がかりが無い控えは使わない★（fail-closed）
        #   2026-08-10に全件取り直したので、ここへ来るのは手で書き足した控えだけ。
        #   ★120字で切った題との前方一致はやめた★（依頼135・回答2）
        #     121字目以降だけが違う別機種を通しうるうえ、もう不要になった。
        return (CHECK_CHANGED,
                "確かめる手がかりがありません（--accept-current で承認が要ります）")

    old_code = str(marks.get("model_code") or "").strip()
    new_code = str(now.get("model_code") or "").strip()
    if old_code and new_code and _title_key(old_code) != _title_key(new_code):
        # ★題を使い回して中身だけ差し替える形は、題では見抜けない★
        return CHECK_CHANGED, "型式が変わりました（%s → %s）" % (old_code, new_code)
    if old_code and not new_code:
        # ★あった手がかりが消えたのも「変わった」★（同定の根拠を失っている）
        return CHECK_CHANGED, "型式が読めなくなりました（控え: %s）" % old_code

    title_same = marks["title_fp"] == now.get("title_fp")
    heads = _headings_same(marks, now)
    if title_same:
        if heads is False:
            return (CHECK_CHANGED,
                    "題は同じですが見出しが変わりました（控え: %s）"
                    % str((marks.get("headings") or ["（なし）"])[0])[:60])
        return CHECK_OK, ""
    if old_code and new_code:
        # 型式が一致している＝機種は同じで、題の飾りが変わっただけ
        return CHECK_OK, "題は変わりましたが型式が一致します"
    return CHECK_CHANGED, "題が変わりました（控え: %s）" % (
        str(marks.get("title") or "")[:60])


def recheck(slug: str, rec: dict, html: str | None = None,
            pubs: dict | None = None) -> dict:
    """★控えのURLを読む直前に、同じページのままか確かめる★（書き込まない）"""
    url = rec.get("url")
    out = {"slug": slug, "url": url, "state": CHECK_UNUSABLE, "why": "",
           "final_url": url, "marks_now": None, "text_sha256": "", "html": None}
    if not url:
        out["why"] = "控えにURLがありません"
        return out
    pid, _lin = publisher_of(url, pubs)
    if not pid:
        out["why"] = "発行者が登録されていません（票に数えられません）"
        return out
    was = rec.get("publisher")
    if was and was != pid:
        # ★ホストの持ち主が変わった＝もう同じ発行者ではない★
        out["state"] = CHECK_CHANGED
        out["why"] = "発行者が変わりました（%s → %s）" % (was, pid)
        return out

    if html is None:
        try:
            # ★用途を名乗ってから取りに行く★（依頼218・上と同じ理由）
            with _w.fetching("claim_material"):
                html = _w._get(url)
        except Exception as e:              # noqa: BLE001
            msg = str(e)
            # ★無くなったページは「一時的に読めない」ではない★（依頼135・P2）
            #   404/410 を取得失敗と同じ扱いにすると、廃止された出典が
            #   いつまでも人の確認対象にならないまま控えに残る。
            if re.search(r"HTTP\s*(404|410)", msg):
                out["state"] = CHECK_CHANGED
                out["why"] = "ページが無くなりました（%s）" % msg[:80]
            else:
                out["why"] = "取得できません（%s）" % msg[:80]
            return out
        final = _w.LAST_FINAL_URL.get("url") or ""
        out["final_url"] = final or url
        if not final:
            out["why"] = "最終URLを確認できませんでした"
            return out
        if publisher_of(final, pubs)[0] != pid:
            out["state"] = CHECK_CHANGED
            out["why"] = "別のサイトへ飛ばされました（%s）" % final[:90]
            return out
        bad = _w.redirect_problem(url, final)
        if bad:
            # ★ページが移った＝このURLはもうあのページではない★
            #   取得できない（一時的）とは違うので、人がもう一度見る側に置く。
            out["state"] = CHECK_CHANGED
            out["why"] = "転送されました（%s）" % bad
            return out
    why = _w.bad_page(html)
    if why:
        out["why"] = "記事のページではありません（%s）" % why
        return out
    title = _w.page_title(html) or ""
    if not title:
        out["why"] = "題がありません（同じページか確かめられません）"
        return out
    text = _text_of(html)
    if len(text) < 200:
        out["why"] = "本文がほとんどありません"
        return out

    out["text_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    out["marks_now"] = _marks_of({
        "final_url": out["final_url"], "title": title,
        "headings": [h[:80] for h in _w._visible_h1s(html)][:5],
        "model_code": _model_code_of(html)})
    out["state"], out["why"] = _same_page(rec, out["marks_now"])
    if out["state"] == CHECK_OK:
        out["html"] = html          # ★取り直させない★（同じ相手を二度叩かない）
    return out


def quarantined(rec: dict) -> bool:
    """前回の確認で「別のページに変わった疑い」が出たまま直っていないか。"""
    return str((rec.get("last_check") or {}).get("state") or "") == CHECK_CHANGED


def issue_args(slug: str, rec: dict, got: dict) -> list:
    """隔離を台帳へ登録するときの引数（★組み立てだけ・実行はしない★）。"""
    title = ("控えの出典が別のページに変わった疑い: %s（%s）"
             % (slug, rec.get("publisher") or "?"))
    detail = "\n".join([
        "URL: " + str(rec.get("url")),
        "理由: " + str(got.get("why")),
        "控えの題: " + str(rec.get("title") or "（なし）"),
        "いまの題: " + str((got.get("marks_now") or {}).get("title") or ""),
        "控えの型式: " + str((rec.get("identity_marks") or {})
                             .get("model_code") or "（なし）"),
        "いまの型式: " + str((got.get("marks_now") or {})
                             .get("model_code") or "（なし）"),
        "★この出典は使っていません（隔離中）★",
        "対応: 人がページを開いて中身を見てから、どちらかを選ぶ。",
        "  ①まだ同じ機種のページだった（題や見出しが変わっただけ）",
        "    python scripts/machine_sources.py --accept-current --slug "
        + slug + " --url <URL> --why <理由> --by 運営者",
        "    ★--recheck --apply では戻りません★（手がかりを書けるのは"
        "「同じページだ」と言えたときだけなので堂々巡りになります）",
        "  ②別の機種のページになっていた",
        "    python scripts/machine_sources.py --forget --slug " + slug
        + " --url <URL> --why <理由> --by 運営者",
        "  ※ページが元どおりに戻った場合は、次の収集で自動的に解除されます。",
    ])
    return ["add", "--source", "machine-sources", "--slug", slug,
            "--kind", "external_value", "--severity", "CRITICAL",
            "--reason-code", "SOURCE_PAGE_CHANGED",
            "--title", title, "--detail", detail]


def report_changed(slug: str, rec: dict, got: dict) -> bool:
    """隔離を人に届ける。★届いたかどうかを返す★（例外は投げない）

    ここが無いと、無人タスクは出典を1つ静かに失うだけで、
    誰も「別機種に化けたページ」に気づけない。
    ★届かなかったことは呼び出し元が控えに残す★（依頼135・P1-5）
    """
    if got.get("state") != CHECK_CHANGED:
        return True
    try:
        import subprocess
        r = subprocess.run(
            [sys.executable, os.path.join(BASE, "scripts", "open_issues.py")]
            + issue_args(slug, rec, got),
            # ★シェルを通らない引数配列なので直接指定でよい★（台帳#295）
            env=dict(os.environ, UCHIDOKORO_ARGV_CALL="1"),
            capture_output=True, timeout=60, check=False)
        if r.returncode == 0:
            return True
        why = (r.stderr or b"").decode("utf-8", "replace")[:200]
    except Exception as e:                  # noqa: BLE001
        why = str(e)[:200]
    print("★台帳へ登録できませんでした（%s / %s）★: %s"
          % (slug, rec.get("url"), why), file=sys.stderr)
    return False


def remember_changed(slug: str, url: str, got: dict,
                     reported: bool = False) -> dict:
    """★「変わった」ことだけを控えに残す（手がかりは絶対に触らない）★

    ★なぜ無人タスクからでも書いてよいか★（2026-08-10・依頼135のP1-3）
      書くのは「使わない」という**制限**だけで、判断（手がかり）は書かない。
      これが無いと、読む側は毎回止めるのに `missing()` はずっと1票と数え続け、
      その機種が手当ての一覧から外れたまま誰も探しに行かない。
    """
    if got.get("state") != CHECK_CHANGED:
        return {"state": "SKIP"}

    def _change(rec):
        rec["last_check"] = {
            "at": _now(),
            "state": CHECK_CHANGED,
            "why": str(got.get("why") or "")[:200],
            # ★台帳へ届いたか★（届いていないものは次の収集で送り直す）
            "reported": bool(reported),
            # ★誰がいつ書いたか★（依頼136・回答2）
            "by": "collect_evidence",
        }
        return {"state": "QUARANTINED", "reported": bool(reported)}
    return _update_one(slug, url, _change)


def release_quarantine(slug: str, url: str, marks: dict) -> dict:
    """★ページが元どおりに戻ったら隔離を解く★（2026-08-10・依頼136）

    解くのは「保存してある手がかりに**完全に戻った**」ときだけ。
    手がかりは人が承認したものなので、そこへ戻ったなら同じページに戻っている。
    ★これが無いと、一時的な不具合で止まった出典が永久に戻らない★
      （題が1文字変わっただけでも止まるので、放っておくと出典が減り続ける）

    ★確かめてから解くまでに人が動かしていたら解かない★（依頼137のP1-2）
      `marks` に「確かめたときの手がかり」を**必ず**渡すこと（依頼138のP3）。
      控えが書き換わっていたら `MARKS_CHANGED` を返す
      （呼び出し元は本文を使ってはいけない）。
    """
    def _change(rec):
        if (rec.get("identity_marks") or {}) != (marks or {}):
            return {"state": "MARKS_CHANGED", NO_WRITE: True}
        if not quarantined(rec):
            return {"state": "ALREADY_OK", NO_WRITE: True}
        rec["last_check"] = {"at": _now(), "state": CHECK_OK,
                             "why": "保存してある手がかりに戻ったので解除しました",
                             "by": "collect_evidence"}
        return {"state": "RELEASED"}
    return _update_one(slug, url, _change)


def accept_current(slug: str, url: str, why: str, by: list) -> dict:
    """★人が「まだ同じ機種のページだ」と承認して手がかりを取り直す★

    ★なぜ要るか（2026-08-10・依頼135のP1-4）★
      題が正当に変わっただけの出典は、`--recheck --apply` では永久に戻らない
      （手がかりを書けるのは「同じページだ」と言えたときだけなので堂々巡り）。
      型式の無いページ（なな徹・ちょんぼりすた・一撃＝控えの大半）が該当する。
      戻す道が無いと、正しい出典を失ったまま機種の更新が止まる。
    """
    _judgement(why, by)
    here = [r for r in urls_for(slug) if r.get("url") == url]
    if not here:
        return {"state": "NOT_FOUND"}
    # ★取りに行くのは書き換えの外で1回だけ★（やり直しのたびに叩かない）
    got = recheck(slug, here[0])
    if not got.get("marks_now"):
        # ★読めないページは承認できない★（見ずに判を押させない）
        raise SourceError("いま読めないページは承認できません: " + got["why"])

    def _change(rec):
        before = dict(rec.get("identity_marks") or {})
        rec["identity_marks"] = got["marks_now"]
        rec["last_check"] = {"at": _now(), "state": CHECK_OK,
                             "why": "人が承認しました",
                             "by": ",".join(by)}
        rec.setdefault("accepted", []).append({
            "at": _now(),
            "by": list(by), "why": str(why).strip()[:300],
            "was_title": str(before.get("title") or "")[:120],
            "now_title": str(got["marks_now"].get("title") or "")[:120],
            "was_model_code": before.get("model_code"),
            "now_model_code": got["marks_now"].get("model_code"),
        })
        return {"state": "ACCEPTED", "url": url,
                "was": before.get("title"),
                "now": got["marks_now"].get("title")}
    return _update_one(slug, url, _change)


def remember_check(slug: str, url: str, got: dict) -> dict:
    """確認の結果を控えに書き戻す（★人が動かすときだけ★）。

    ★手がかりを上書きしてよいのは「同じページだ」と言えたときだけ★
      変わっていたのに今のページで上書きすると、
      **差し替わった別機種のページを正として覚え直す**ことになる。
    """
    def _change(rec):
        last = {"at": _now(), "state": got.get("state"),
                "why": str(got.get("why") or "")[:200], "by": "recheck"}
        if got.get("state") == CHECK_CHANGED:
            # ★台帳へ届いたかも一緒に残す★（届いていなければ次回また送る）
            last["reported"] = bool(got.get("reported"))
        rec["last_check"] = last
        if got.get("state") == CHECK_OK and got.get("marks_now"):
            rec["identity_marks"] = got["marks_now"]
            if got.get("text_sha256"):
                rec["text_sha256_last"] = got["text_sha256"]
        return {"state": "SAVED", "marks": got.get("state") == CHECK_OK}
    return _update_one(slug, url, _change)


def _no_name_is_not_absent() -> bool:
    """★待ち行列に居るのに名前が空＝置き去りにしない★（試験用）"""
    import pending_machines as _pm
    _bak = _pm.load
    try:
        _pm.load = lambda: {"items": {
            "https://www.p-world.co.jp/machine/database/99999": {
                "url": "https://www.p-world.co.jp/machine/database/99999",
                "name": ""}}}
        try:
            _pending_machine("pw_99999")
            return False                   # 例外にならなければ不合格
        except PendingUnreadable:
            pass
        return orphaned("pw_99999", {"origin": "pending"}) is False
    finally:
        _pm.load = _bak


def backfill_origin(apply: bool = False) -> list:
    """★印の無い古い控えに「どこから来たか」を補う★（2026-08-14・依頼200のP2）

    origin は 2026-08-14 から付け始めたので、それ以前の控えには無い。
    印が無いものは「記事のある機種」として扱われ、置き去りの判定が効かない。

    ★判断のしかた★＝そのslugが machines.json にあれば machine、
      無ければ pending（記事がまだ無い新台のために控えたもの）。
    ★一度きり★＝補ったあとは record() が付ける。
    """
    ms = _sj.read_json(MACHINES, expect=(dict, list))
    ms = ms["machines"] if isinstance(ms, dict) else ms
    known = {m.get("slug") for m in ms}
    out = []

    def _do(data):
        out.clear()
        for slug, recs in (data.get("machines") or {}).items():
            for rec in recs:
                if rec.get("origin"):
                    continue
                got = "machine" if slug in known else "pending"
                out.append({"slug": slug, "url": rec.get("url"),
                            "origin": got})
                rec["origin"] = got
        return data if out else None

    if apply:
        # ★書くときは他の実行と同じ鍵を通す★（丸ごと書き戻すため）
        _update(_do)
    else:
        _do(load())
    return out


def orphaned(slug: str, rec: dict) -> bool:
    """★もう誰も使わない控えか★（2026-08-14・台帳#350）

    記事がまだ無い新台のために控えたもので、
    ・記事にもなっていない（machines.json に無い）
    ・待ち行列にも居ない（60日で打ち切られた）
    のときだけ「置き去り」とみなす。

    ★消さない★＝同じ機種が再登場したら、そのまま復帰できるようにする。
      巡回（取り直し）の対象から外すだけ。
    """
    if str(rec.get("origin") or "") != "pending":
        return False
    try:
        machine(slug)
        return False                      # 記事になった or まだ待ち行列に居る
    except PendingUnreadable:
        # ★確かめられないなら置き去りにしない★（2026-08-14・依頼200のP2）
        #   「無い」と「確かめられない」は別。巡回を続ける側に倒す。
        return False
    except SourceError:
        return True


def recheck_all(slug: str = "", apply: bool = False) -> list:
    """控えを順に確かめる。★--apply で手がかりを保存し直す（取り直し）★

    ★置き去りの控えは見に行かない★（2026-08-14・台帳#350）
      待ち行列が打ち切られた新台の控えを毎回取りに行くと、
      要らない通信・ページ消滅による隔離・台帳のノイズが増える。
    """
    data = load()
    rows = []
    for s, recs in sorted((data.get("machines") or {}).items()):
        if slug and s != slug:
            continue
        for rec in recs:
            if orphaned(s, rec):
                rows.append({"slug": s, "url": rec.get("url"),
                             "publisher": rec.get("publisher"),
                             "state": "ORPHANED",
                             "why": "記事にも待ち行列にも無い新台の控えです"
                                    "（見に行きません・消しもしません）",
                             "had_marks": bool(rec.get("identity_marks")),
                             "title_now": None, "model_now": None})
                continue
            try:
                got = recheck(s, rec)
            except Exception as e:          # noqa: BLE001
                # ★1件つまずいても残りを見る★（途中で落ちると、
                #   その先の控えが確かめられないまま「無事」に見える）
                got = {"state": CHECK_UNUSABLE,
                       "why": "確かめられません: " + str(e)[:80],
                       "marks_now": None}
            if apply:
                if got.get("state") == CHECK_CHANGED:
                    # ★先に隔離を残してから台帳へ送る★（依頼135・回答5）
                    #   送信に失敗しても「使わない」は消えない。
                    remember_changed(s, rec.get("url"), got, reported=False)
                    got["reported"] = report_changed(s, rec, got)
                remember_check(s, rec.get("url"), got)
            rows.append({"slug": s, "url": rec.get("url"),
                         "publisher": rec.get("publisher"),
                         "state": got["state"], "why": got["why"],
                         "had_marks": bool(rec.get("identity_marks")),
                         "title_now": (got.get("marks_now") or {}).get("title"),
                         "model_now": (got.get("marks_now") or {}).get(
                             "model_code")})
    return rows


# ---------------------------------------------------------------- 手当てが要る機種

def missing(limit: int = 0) -> list:
    """2つの系列に届かない機種を並べる（★AIに探させる対象★）。

    ★票の単位は source_lineage が唯一の正本★（2026-08-09・依頼125）
      以前は名鑑側だけ `dir:<名鑑ID>` という仮の名前で数えていたため、
      **同じ発行者の名鑑と控えが2票に化けていた**（実データで再現済み）。
      名鑑も控えも「発行者ID → 票のかたまり」で数え直す。
      引けないものは票にしない（★仮の名前を作らない★）。
    """
    import directory_index as _di

    cats = _sj.read_json(_di.CATALOGS, expect=dict)["directories"]
    active = {k: c for k, c in cats.items() if c.get("status") == "ACTIVE"}
    # ★名鑑の発行者は起動時に必ず引けること★（引けなければ設定の誤り＝止める）
    dir_key = {}
    for dir_id, c in active.items():
        pid = c.get("publisher_id")
        if not pid:
            raise SourceError(
                "名鑑に publisher_id がありません: " + dir_id
                + "（票の単位を決められないので数えません）")
        dir_key[dir_id] = _sl.vote_key(pid)

    scans = {k: _di.scan_directory(k, c) for k, c in active.items()}
    ms = _sj.read_json(MACHINES, expect=(dict, list))
    ms = ms["machines"] if isinstance(ms, dict) else ms
    data = load()
    rows = []
    for m in ms:
        core = _ci.normalize_core(m.get("name") or "")
        have, seen, unknown = [], set(), []
        for dir_id, r in scans.items():
            # ★候補が2件以上ある名鑑は票にしない★（2026-08-09・実データで判明）
            #   実際に原文を読む側（directory_index.find）は候補が1件のときしか
            #   URLを返さない。ここだけ「1件でも当たれば手当て済み」と数えると、
            #   **読めない機種を「もう出典がある」と誤って外して**しまう。
            #   実例: Lハナビ → 「スマスロ ハナビ」と旧「ハナビ」の2件、
            #        Lパチスロ炎炎ノ消防隊 → L版と旧パチスロ版の2件。
            if len(_di.lookup_hits(r["index"], core)) != 1:
                continue
            if dir_key[dir_id] not in seen:
                seen.add(dir_key[dir_id])
                have.append(dir_id)
        held = []
        for rec in urls_for(m["slug"], data):
            # ★隔離中の控えは票にしない★（2026-08-10・台帳#292）
            #   別のページに変わった疑いが出たものは、読む側（collect_evidence）が
            #   使わない。ここだけ数え続けると「出典は2つある」ことにされ、
            #   **手当ての一覧から外れて誰も探しに行かなくなる**。
            if quarantined(rec):
                held.append(rec.get("publisher"))
                continue
            # ★保存済みの系列名は使わない★＝今のレジストリから引き直す
            try:
                key = _sl.vote_key(rec.get("publisher"))
            except _sl.LineageError:
                unknown.append(rec.get("publisher"))
                continue
            if key not in seen:
                seen.add(key)
                have.append(rec.get("publisher"))
        # ★共同で作ることがある組は1票にまとめる★（2026-08-14・依頼190のP1）
        seen = _sl.merge_joint(seen)
        if len(seen) < 2:
            rows.append({"slug": m["slug"], "name": m.get("name"),
                         "have": have, "votes": len(seen),
                         "unknown": unknown, "quarantined": held})
    return rows[:limit] if limit else rows


# ---------------------------------------------------------------- 表示

def _print_check(got: dict) -> None:
    print("■ %s（%s）" % (got["name"], got["slug"]))
    print("  URL     : " + got["url"])
    print("  発行者  : %s / 系列 %s" % (got["publisher"], got["lineage"]))
    if got["problems"]:
        for p in got["problems"]:
            print("  ★使えません★ " + p)
        return
    print("  題      : " + got["title"][:110])
    for h in got["headings"]:
        print("  見出し  : " + h)
    print("  型式    : " + (got["model_code"] or "（見つかりません）"))
    print("  本文    : %d文字 / sha256 %s" % (got["text_len"],
                                              got["text_sha256"][:12]))
    print("  名前の形: %s（%s）"
          % ("一致" if got["identity_verdict"] else "★一致しません★",
             got["identity_why"]))
    if got["already_recorded"]:
        print("  ※すでに控えにあります")
    if got["same_lineage_already"]:
        print("  ※同じ系列の出典がすでにあります（票は増えません）: "
              + ", ".join(str(u) for u in got["same_lineage_already"]))
    print("  抜粋    : " + got["excerpt"][:300])


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    import tempfile

    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    global STORE
    keep = STORE
    tmpdir = tempfile.mkdtemp()
    STORE = os.path.join(tmpdir, "machine_sources.json")
    pubs = {"chonborista.com": ("chonborista", "lin-chonborista"),
            "nana-press.com": ("nana-press", "lin-nana-press")}
    ms = _sj.read_json(MACHINES, expect=(dict, list))
    ms = ms["machines"] if isinstance(ms, dict) else ms
    slug = ms[0]["slug"]
    name = ms[0]["name"]
    body = ("<title>" + name + " スロット 新台 天井 | ちょんぼりすた</title>"
            "<body><h1>" + name + "</h1><p>" + ("天井は999Gです。" * 40)
            + "</p></body>")

    # ★★記事がまだ無い新台も引ける（2026-08-14・台帳#347）★★
    #   新台は「記事を作る材料を集める段階」で出典の同定が要る。
    #   控えが machines.json にある機種にしか書けないと、
    #   2AIが一度出した結論を毎晩捨てて同じ判断をやり直すことになる。
    _pm_bak = globals().get("_pending_machine")
    try:
        globals()["_pending_machine"] = lambda s: (
            {"slug": s, "name": "L試験の新台", "_pending": True}
            if s == "pw_test_new" else {})
        t("★★記事がまだ無い新台も、待ち行列から引ける★★（台帳#347）",
          machine("pw_test_new").get("name") == "L試験の新台")
        _ng = False
        try:
            machine("pw_shiranai")
        except SourceError:
            _ng = True
        t("　待ち行列にも記事にも無いslugは、今までどおり止まる", _ng)
        t("　（対照）名前を自己申告させない＝待ち行列の名前を使う",
          machine("pw_test_new").get("_pending") is True)
    finally:
        globals()["_pending_machine"] = _pm_bak

    # ★★置き去りの控えは見に行かない（2026-08-14・台帳#350）★★
    _pm_bak2 = globals().get("_pending_machine")
    # ★ここは本物の _pending_machine を使う★（待ち行列だけ差し替える）
    t("★★待ち行列に居るのに名前が空でも、置き去りにしない★★"
      "（2026-08-14・依頼201のP2）／待ち行列は名前なしでも覚える作りなので、"
      "「無い」と扱うと生きている新台の控えが巡回から外れる",
      _no_name_is_not_absent())
    try:
        globals()["_pending_machine"] = lambda s: {}
        t("★★記事にも待ち行列にも無い新台の控えは、巡回しない★★（台帳#350）"
          "／要らない通信・ページ消滅の隔離・台帳のノイズを増やさない",
          orphaned("pw_kieta", {"origin": "pending"}) is True)
        t("　（対照）記事になった機種の控えは今までどおり巡回する",
          orphaned(ms[0]["slug"], {"origin": "pending"}) is False)
        t("　印の無い（もともと記事があった）控えは、そのまま巡回する",
          orphaned("pw_kieta", {}) is False)
        globals()["_pending_machine"] = (
            lambda s: (_ for _ in ()).throw(PendingUnreadable("壊れています")))
        t("★★待ち行列を読めないときは置き去りにしない★★（依頼200のP2）"
          "／「無い」と「確かめられない」を混ぜると、生きている新台の控えまで"
          "巡回から外れ、見張りが黙って止まる",
          orphaned("pw_kieta", {"origin": "pending"}) is False)
    finally:
        globals()["_pending_machine"] = _pm_bak2

    try:
        t("★★登録されていないサイトは使わない★★（票に数えられない）",
          check(slug, "https://example.com/x", html=body, pubs=pubs)["problems"])

        got = check(slug, "https://chonborista.com/slot/a/1",
                    html=body, pubs=pubs)
        t("　登録済みなら発行者と系列が付く",
          got["publisher"] == "chonborista"
          and got["lineage"] == "lin-chonborista")
        t("　題・見出し・本文の指紋を材料として出す",
          got["title"] and got["headings"] and len(got["text_sha256"]) == 64)
        t("　本文が短すぎるページは使わない",
          not check(slug, "https://chonborista.com/slot/a/2",
                    html="<title>x</title><body>短い</body>",
                    pubs=pubs)["ok"])

        try:
            record(slug, got["url"], why="", by=["claude"], checked=got)
            ok = False
        except SourceError:
            ok = True
        t("★★なぜ同じ機種かを書かずには記録できない★★", ok)

        try:
            record(slug, got["url"], why="題が機種名と一致します", by=[], checked=got)
            ok = False
        except SourceError:
            ok = True
        t("　誰が判断したかを書かずには記録できない", ok)

        bad = dict(got, ok=False, problems=["取得できません"])
        try:
            record(slug, got["url"], why="題が機種名と一致します", by=["claude"],
                   checked=bad)
            ok = False
        except SourceError:
            ok = True
        t("★★機械の検査を通らないものは記録しない★★（AIの言い分だけでは残さない）",
          ok)

        ng = dict(got, identity_verdict=False, identity_why="TITLE_MISMATCH")
        try:
            record(slug, got["url"], why="題が機種名と一致します", by=["claude"],
                   checked=ng)
            ok = False
        except SourceError:
            ok = True
        t("★★題が合わないときは理由を明示しないと記録できない★★", ok)

        try:
            record(slug, got["url"], why="略称なので題は合いませんが型式が一致します",
                   by=["claude"], override_identity="略称だが型式が一致", checked=ng)
            _one = False
        except SourceError:
            _one = True
        t("★★題で分からない出典は、2AIがそろわないと登録できない★★"
          "（片方の誤判定だと、最初の誤登録を誰も検出できない）", _one)
        r = record(slug, got["url"], why="略称なので題は合いませんが型式が一致します",
                   by=["claude", "codex"],
                   override_identity="略称だが型式が一致", checked=ng)
        t("　2AIがそろって理由を書けば記録できる", r["state"] == "RECORDED")
        t("　控えから読み出せる（機械が毎日使う側）",
          [x["url"] for x in urls_for(slug)] == [got["url"]])
        t("　同じURLは二重に入らない",
          record(slug, got["url"], why="題が機種名と一致します", by=["claude"],
                 checked=dict(got, already_recorded=True))["state"] == "ALREADY")
        t("　控えの形が読み出せる", load()["schema_version"] == SCHEMA)

        got2 = check(slug, "https://chonborista.com/slot/a/9",
                     html=body, pubs=pubs)
        t("★★同じ系列がすでにあるときは知らせる★★（票は増えない）",
          got2["same_lineage_already"] == [got["url"]])

        try:
            forget(slug, got["url"], why="", by=["運営者"])
            okf = False
        except SourceError:
            okf = True
        t("★★外すときも「誰がなぜ」を書かせる★★（依頼138のP2）"
          "＝外した記録は復元できないので、残せる帰属だけは必ず残す", okf)
        t("　間違いは控えから外せる",
          forget(slug, got["url"], "別機種のページでした",
                 ["運営者"])["state"] == "FORGOTTEN"
          and urls_for(slug) == [])
        t("　無いものを外そうとしても壊れない",
          forget(slug, got["url"], "もう無いはずです",
                 ["運営者"])["state"] == "NOT_FOUND")

        # ------------------------------------------- ★使う瞬間の同定★（#292）
        title_now = _w.page_title(body)
        code_body = body.replace("<body>", "<body><p>型式名：LテストAB1</p>")
        code2_body = body.replace("<body>", "<body><p>型式名：LベツキシュCD2</p>")
        other = ("<title>ぜんぜん別の機種 スロット 天井 | ちょんぼりすた</title>"
                 "<body><h1>ぜんぜん別の機種</h1><p>"
                 + ("天井は777Gです。" * 40) + "</p></body>")

        def R(rec, html):
            return recheck(slug, rec, html=html, pubs=pubs)

        base = {"url": "https://chonborista.com/slot/a/1",
                "publisher": "chonborista"}
        marked = dict(base, identity_marks={
            "title_fp": _title_fp(title_now), "title": title_now[:120],
            "model_code": None})

        t("★★題が同じなら、そのまま使ってよい★★", R(marked, body)["state"] == CHECK_OK)
        t("　通ったときは取ってきた本文をそのまま渡す（二度取りに行かない）",
          R(marked, body)["html"] == body)
        g = R(marked, other)
        t("★★題が変わったら使わない★★（別機種に差し替わった疑い＝人が見る）",
          g["state"] == CHECK_CHANGED and "題が変わりました" in g["why"])

        m_code = dict(base, identity_marks={
            "title_fp": _title_fp(title_now), "title": title_now[:120],
            "model_code": "LテストAB1"})
        t("★★題の飾りが変わっても、型式が一致すれば使える★★",
          R(m_code, other.replace("<body>",
                                  "<body><p>型式名：LテストAB1</p>")
            )["state"] == CHECK_OK)
        g = R(m_code, code2_body)
        t("★★型式が食い違えば、題が同じでも使わない★★"
          "（題を使い回して中身だけ差し替える形は題では見抜けない）",
          g["state"] == CHECK_CHANGED and "型式が変わりました" in g["why"])

        # ★2026-08-10に見つけた取り違えを固定する★
        t("★★型式の手がかりは値だけを持つ★★"
          "（組のまま文字にすると \"(None, 'MODEL_CODE_NOT_FOUND')\" が入る）",
          _marks_of({"title": "x", "model_code": _model_code_of(body)}
                    )["model_code"] is None
          and _model_code_of(code_body) == "LテストAB1")

        # ★依頼135のP0-1：手がかり1つでは通さない★
        t("★★保存時にあった型式が読めなくなったら止める★★"
          "（同定の根拠を失っているのに、題だけで通していた）",
          R(m_code, body)["state"] == CHECK_CHANGED
          and "型式が読めなくなりました" in R(m_code, body)["why"])
        head_marked = dict(base, identity_marks={
            "title_fp": _title_fp(title_now), "title": title_now[:120],
            "headings": [name], "model_code": None})
        swap = ("<title>" + title_now + "</title><body><h1>ぜんぜん別の機種</h1>"
                "<p>" + ("天井は777Gです。" * 40) + "</p></body>")
        t("★★題を残したまま中身を差し替えた形を、見出しで捕まえる★★"
          "（型式の無い出典が控えの大半なので、題だけでは薄い）",
          R(head_marked, swap)["state"] == CHECK_CHANGED
          and "見出しが変わりました" in R(head_marked, swap)["why"])
        t("　題も見出しも同じなら通る（正しく通る側も確かめる）",
          R(head_marked, body)["state"] == CHECK_OK)
        t("　見出しを記録していない控えは題だけで判断する（記録が無いものは求めない）",
          R(marked, body)["state"] == CHECK_OK)
        two = dict(base, identity_marks={
            "title_fp": _title_fp(title_now), "title": title_now[:120],
            "headings": [name, "解析データ"], "model_code": None})
        two_body = body.replace("</h1>", "</h1><h1>解析データ</h1>")
        t("★★保存した見出しは全部そろっていること★★（依頼136のP0-2）"
          "＝先頭1本だけ見ていたので、あとが差し替わっても通っていた",
          R(two, two_body)["state"] == CHECK_OK
          and R(two, body)["state"] == CHECK_CHANGED)

        # ★鍵の異常は「使用中」と分ける★（依頼139のP3）
        def _broken(fh, on):
            raise OSError(errno.EBADF, "壊れた記述子")
        _keep_hold = globals()["_hold"]
        try:
            globals()["_hold"] = _broken
            t0 = time.time()
            try:
                _update(lambda d: {"state": "x"})
                okl = False
            except SourceError as e:
                okl = "扱えません" in str(e) and time.time() - t0 < 5
        finally:
            globals()["_hold"] = _keep_hold
        t("　鍵の異常は「他の実行が使用中」と分けて、待たずに知らせる", okl)

        # ★名鑑で見つけたページの同定（2026-08-11・台帳#309）★
        nick = ("<title>【ためし丸(スマスロ)】解析情報まとめ</title><body>"
                "<h1>【ためし丸(スマスロ)】解析情報まとめ</h1><p>機種名:" + name
                + " メーカー オリンピア</p></body>")
        other_m = ("<title>【べつの台(スマスロ)】解析情報まとめ</title><body>"
                   "<h1>【べつの台(スマスロ)】解析</h1><p>機種名:まったく別の機種"
                   " メーカー オリンピア</p></body>")
        t("　名鑑のページも題で同定できれば通す",
          directory_page_ok(name, body)[0])
        t("★★題で分からないものは通さず、2AIへ回す★★（運営者の指摘）"
          "＝ここで機械の判定を足すと『この場合、この場合…』が増える",
          directory_page_ok(name, nick)[0] is False)
        t("★★題も本文もその機種でなければ通さない★★"
          "（名鑑はリンク文字だけで決めていたので、中身が差し替わると素通りした）",
          not directory_page_ok(name, other_m)[0])

        # ★URLの表記ゆれ（依頼136のP0-1）★
        t("★★末尾の / や www の違いで、同じページを別物と数えない★★"
          "＝別物と見なすと名鑑側が先に読んで同定を素通りする",
          url_key("https://www.a.example/x/") == url_key("https://a.example/x")
          and url_key("https://a.example/x?q=1")
          != url_key("https://a.example/x?q=2"))
        t("　その方式で省略できるポートだけをそろえる（依頼138のP2）"
          "＝80を省けるのはhttp・443を省けるのはhttpsのときだけ",
          url_key("https://a.example:443/x") == url_key("https://a.example/x")
          and url_key("http://a.example:80/x") == url_key("http://a.example/x")
          and url_key("https://a.example:80/x") != url_key("https://a.example/x")
          and url_key("http://a.example:443/x") != url_key("http://a.example/x")
          and url_key("https://a.example:8443/x")
          != url_key("https://a.example/x"))

        # ★手がかりが無い控え（旧形式）は、もう通さない★（依頼135・回答2）
        t("★★手がかりが無い控えは使わない★★（fail-closed）"
          "＝120字の前方一致はやめた（121字目以降だけ違う別機種を通しうる）",
          R(dict(base, title=title_now[:120]), body)["state"] == CHECK_CHANGED)
        t("　戻し方を必ず案内する（黙って行き止まりにしない）",
          "--accept-current" in R(dict(base), body)["why"])

        t("★★発行者が変わったら使わない★★",
          R(dict(marked, publisher="nana-press"),
            body)["state"] == CHECK_CHANGED)
        t("　登録されていないサイトになっていたら使わない",
          recheck(slug, {"url": "https://example.com/x"}, html=body,
                  pubs=pubs)["state"] == CHECK_UNUSABLE)
        t("　記事ではない画面は「今日は読めない」（隔離とは分ける）",
          R(marked, "<title>x</title><body>ただいまメンテナンス中です</body>"
            )["state"] == CHECK_UNUSABLE)
        t("　本文がほとんど無いページも読めない扱い",
          R(marked, "<title>" + title_now + "</title><body>短い</body>"
            )["state"] == CHECK_UNUSABLE)

        # ★隔離を台帳へ届ける道を、本物の台帳スクリプトで通す★
        #   握りつぶす作りなので、引数が1つ違うだけで**誰にも届かなくなる**。
        led = os.path.join(tmpdir, "issues.json")
        import subprocess
        rr = subprocess.run(
            [sys.executable, os.path.join(BASE, "scripts", "open_issues.py"),
             "--file", led]
            + issue_args(slug, marked, R(marked, other)),
            env=dict(os.environ, UCHIDOKORO_ARGV_CALL="1"),
            capture_output=True, timeout=60, check=False)
        t("★★隔離は台帳が実際に受け取れる形で送る★★（届かなければ誰も気づけない）",
          rr.returncode == 0
          and "別のページに変わった疑い" in _sj.read_json(
              led, expect=dict)["issues"][0]["title"])

        t("　確認の結果は控えに書き戻せる（手がかりの取り直し）",
          (_save({"schema_version": SCHEMA,
                  "machines": {slug: [dict(marked)]}}),
           remember_check(slug, base["url"], R(marked, body))["state"] == "SAVED"
           and urls_for(slug)[0]["text_sha256_last"])[1])
        t("★★変わっていたら手がかりを上書きしない★★"
          "（差し替わった別機種のページを正として覚え直さない）",
          (_save({"schema_version": SCHEMA,
                  "machines": {slug: [dict(marked)]}}),
           remember_check(slug, base["url"], R(marked, other)),
           urls_for(slug)[0]["identity_marks"]["title_fp"]
           == _title_fp(title_now)
           and urls_for(slug)[0]["last_check"]["state"] == CHECK_CHANGED)[2])

        # ★依頼135のP1-3：隔離そのものを控えに残す★
        _save({"schema_version": SCHEMA, "machines": {slug: [dict(marked)]}})
        rq = remember_changed(slug, base["url"], R(marked, other),
                              reported=False)
        left = urls_for(slug)[0]
        t("★★『変わった』だけを控えに残せる（手がかりは触らない）★★"
          "＝これが無いと、読む側は毎回止めるのに手当ての一覧は出典ありと数え続ける",
          rq["state"] == "QUARANTINED" and quarantined(left)
          and left["identity_marks"]["title_fp"] == _title_fp(title_now)
          and left["last_check"]["reported"] is False)
        t("　変わっていないものを隔離として書き込まない",
          remember_changed(slug, base["url"], R(marked, body))["state"] == "SKIP")
        t("　元どおりに戻ったら隔離を解ける（自動解除）",
          release_quarantine(slug, base["url"],
                             marked["identity_marks"])["state"] == "RELEASED"
          and not quarantined(urls_for(slug)[0]))
        _save({"schema_version": SCHEMA, "machines": {slug: [
            dict(marked, last_check={"state": CHECK_CHANGED})]}})
        t("★★確かめてから解くまでに人が控えを変えていたら解かない★★"
          "（依頼137のP1-2）＝古い控えに合格した本文を使わせない",
          release_quarantine(slug, base["url"], {"title_fp": "ちがう"}
                             )["state"] == "MARKS_CHANGED"
          and quarantined(urls_for(slug)[0]))
        t("　外したことも控えに残る（いつ誰がなぜ）",
          forget(slug, base["url"], "別機種のページでした",
                 ["運営者"])["state"] == "FORGOTTEN"
          and load()["removed"][-1]["by"] == ["運営者"]
          and load()["removed"][-1]["at"])

        # ★同時に書いても、先の書き込みを消さない★（依頼136のP1-3）
        _save({"schema_version": SCHEMA, "machines": {slug: [
            dict(marked), dict(marked, url="https://chonborista.com/slot/a/2")]}})

        def _sneak(rec):
            """★書く直前に、別の実行が控えを書き換えた状況を作る★"""
            d2 = load()
            d2["machines"][slug][1]["last_check"] = {"state": CHECK_CHANGED}
            _save(d2)
            rec["last_check"] = {"state": CHECK_CHANGED, "why": "こちらの書き込み"}
            return {"state": "DONE"}
        calls = [0]
        _orig_sneak = _sneak

        def _once(rec):
            calls[0] += 1
            if calls[0] == 1:
                return _orig_sneak(rec)      # 1回目だけ横入りされる
            rec["last_check"] = {"state": CHECK_CHANGED, "why": "こちらの書き込み"}
            return {"state": "DONE"}
        _update_one(slug, base["url"], _once)
        after = urls_for(slug)
        t("★★同時に書いても、先の書き込みを消さない★★（読み直してやり直す）"
          "＝隔離が消えると、次の実行では未隔離として本文が使われてしまう",
          calls[0] == 2 and quarantined(after[0]) and quarantined(after[1]))

        # ★依頼135のP1-4：人が承認して戻せる★
        _save({"schema_version": SCHEMA, "machines": {slug: [dict(marked)]}})
        try:
            accept_current(slug, base["url"], why="短い", by=["運営者"])
            okx = False
        except SourceError:
            okx = True
        t("　承認にも「誰がなぜ」を書かせる（記録と同じ条件）", okx)
        _keep_recheck = globals()["recheck"]
        try:
            # ★承認は「いまのページ」を取りに行くので、そこだけ差し替える★
            globals()["recheck"] = (
                lambda s, r, html=None, pubs=None, _f=_keep_recheck:
                _f(s, r, html=other, pubs=pubs))
            acc = accept_current(slug, base["url"],
                                 why="題が変わっただけで同じ機種のページです",
                                 by=["運営者"])
            got_rec = urls_for(slug)[0]
            t("★★題が正当に変わったものを、人が承認して戻せる★★"
              "（型式の無い出典は --recheck --apply では永久に戻らない）",
              acc["state"] == "ACCEPTED" and not quarantined(got_rec)
              and got_rec["identity_marks"]["title_fp"]
              == _title_fp(_w.page_title(other))
              and got_rec["accepted"][0]["by"] == ["運営者"])
        finally:
            globals()["recheck"] = _keep_recheck

        # ★2026-08-09・依頼125で見つかった数え間違いを固定する★
        #   名鑑と控えに同じ発行者が出たとき、以前は2票と数えていた。
        import directory_index as _di
        real_scan = _di.scan_directory
        core = _ci.normalize_core(name)

        def fake_scan(dir_id, conf, _core=core):
            idx = ({_core: [("https://chonborista.com/slot/a/1", "題")]}
                   if dir_id == "chonborista" else {})
            return {"index": idx, "surfaces_ok": 1, "surfaces_total": 1,
                    "problems": []}

        def ambiguous_scan(dir_id, conf, _core=core):
            """★候補が2件ある名鑑★（Lハナビ ↔ スマスロハナビ/旧ハナビ）"""
            idx = ({_core: [("https://chonborista.com/slot/a/1", "新しい方"),
                            ("https://chonborista.com/slot/a/2", "古い方")]}
                   if dir_id == "chonborista" else {})
            return {"index": idx, "surfaces_ok": 1, "surfaces_total": 1,
                    "problems": []}
        _di.scan_directory = fake_scan
        try:
            _save({"schema_version": SCHEMA, "machines": {slug: [
                {"url": "https://chonborista.com/slot/a/9",
                 "publisher": "chonborista", "lineage": "lin-chonborista"}]}})
            still = [r for r in missing() if r["slug"] == slug]
            t("★★名鑑と控えが同じ発行者なら1票★★（1社を2票と数えない）",
              still and still[0]["votes"] == 1)

            _save({"schema_version": SCHEMA, "machines": {slug: [
                {"url": "https://nana-press.com/kaiseki/machine/1",
                 "publisher": "nana-press", "lineage": "lin-nana-press"}]}})
            t("　別の発行者なら2票になって一覧から外れる",
              not [r for r in missing() if r["slug"] == slug])

            _save({"schema_version": SCHEMA, "machines": {slug: [
                {"url": "https://example.com/x", "publisher": "shiranai-site",
                 "lineage": "lin-shiranai"}]}})
            left = [r for r in missing() if r["slug"] == slug]
            t("★★登録されていない発行者は票にしない★★（仮の名前を作らない）",
              left and left[0]["votes"] == 1
              and left[0]["unknown"] == ["shiranai-site"])

            _di.scan_directory = ambiguous_scan
            _save({"schema_version": SCHEMA, "machines": {slug: [
                {"url": "https://nana-press.com/kaiseki/machine/1",
                 "publisher": "nana-press", "lineage": "lin-nana-press"}]}})
            amb = [r for r in missing() if r["slug"] == slug]
            t("★★候補が割れている名鑑は票にしない★★（原文を読む側は1件のときしか使わない）",
              amb and amb[0]["votes"] == 1 and amb[0]["have"] == ["nana-press"])

            _di.scan_directory = fake_scan
            _save({"schema_version": SCHEMA, "machines": {slug: [
                {"url": "https://nana-press.com/kaiseki/machine/1",
                 "publisher": "nana-press", "lineage": "lin-nana-press",
                 "last_check": {"state": CHECK_CHANGED, "at": "2026-08-10",
                                "why": "題が変わりました"}}]}})
            q = [r for r in missing() if r["slug"] == slug]
            t("★★隔離中の控えは票にしない★★（2026-08-10・台帳#292）"
              "＝読む側が使わないものを数え続けると、手当ての一覧から外れる",
              q and q[0]["votes"] == 1 and q[0]["quarantined"] == ["nana-press"])
        finally:
            _di.scan_directory = real_scan
    finally:
        STORE = keep

    bad = sum(1 for _, ok in results if not ok)
    print()
    print("%d/%d 合格" % (len(results) - bad, len(results)))
    return 1 if bad else 0


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser(description="機種ごとの出典URLの控え")
    ap.add_argument("--check", action="store_true",
                    help="機械の検査だけ（AIに渡す材料を出す）")
    ap.add_argument("--record", action="store_true", help="控えに残す")
    ap.add_argument("--forget", action="store_true", help="控えから外す")
    ap.add_argument("--list", action="store_true", help="控えを見る")
    ap.add_argument("--missing", action="store_true",
                    help="2つの系列に届かない機種を並べる")
    ap.add_argument("--recheck", action="store_true",
                    help="控えのページが同じままか確かめる（既定は見るだけ）")
    ap.add_argument("--apply", action="store_true",
                    help="--recheck の結果を控えに保存する（★人の操作専用★）")
    ap.add_argument("--accept-current", dest="accept_current",
                    action="store_true",
                    help="いまのページを「まだ同じ機種」と人が承認して手がかりを"
                         "取り直す（--slug --url --why --by が要ります）")
    ap.add_argument("--slug")
    ap.add_argument("--url")
    ap.add_argument("--why", default="")
    ap.add_argument("--by", default="",
                    help="判断した人（claude / codex / 運営者。カンマ区切り）")
    ap.add_argument("--override-identity", default="",
                    help="題が機種名と一致しないときの理由")
    # ★自由文はファイルで渡せるようにする★（2026-08-14）
    #   長い理由をコマンドに書くと、中の記号がシェルに実行される
    #   （2026-08-08に実際に発生）。台帳やメールと同じく、
    #   ★文章はファイルに書いて、コマンドにはパスだけを渡す★形にそろえる。
    ap.add_argument("--why-file", dest="why_file", default="",
                    help="理由を書いたファイル（--why と同時には使えません）")
    ap.add_argument("--override-identity-file", dest="override_identity_file",
                    default="", help="同定の理由を書いたファイル")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()
    # ★ファイル渡しは台帳と同じ受け取り方を使う★（置き場も同じ制限）
    #   ＝ops / _design の下だけ・大きさとUTF-8を確かめる・制御文字を弾く。
    try:
        import open_issues as _oi
        a.why = _oi._read_text_arg(a.why, a.why_file, "why")
        a.override_identity = _oi._read_text_arg(
            a.override_identity, a.override_identity_file, "override-identity")
    except SystemExit as e:
        print(str(e))
        return 2
    try:
        if a.check:
            if not (a.slug and a.url):
                print("--slug と --url が要ります")
                return 2
            _print_check(check(a.slug, a.url))
            return 0
        if a.record:
            if not (a.slug and a.url):
                print("--slug と --url が要ります")
                return 2
            r = record(a.slug, a.url, a.why,
                       [x.strip() for x in a.by.split(",") if x.strip()],
                       a.override_identity)
            print(json.dumps(r, ensure_ascii=False))
            return 0
        if a.forget:
            print(json.dumps(
                forget(a.slug, a.url, a.why,
                       [x.strip() for x in a.by.split(",") if x.strip()]),
                ensure_ascii=False))
            return 0
        if a.accept_current:
            if not (a.slug and a.url):
                print("--slug と --url が要ります")
                return 2
            r = accept_current(a.slug, a.url, a.why,
                               [x.strip() for x in a.by.split(",") if x.strip()])
            print(json.dumps(r, ensure_ascii=False))
            return 0 if r["state"] == "ACCEPTED" else 1
        if a.recheck:
            rows = recheck_all(a.slug or "", apply=a.apply)
            mark = {CHECK_OK: "○", CHECK_CHANGED: "★変わっています★",
                    CHECK_UNUSABLE: "△読めません"}
            for r in rows:
                print("%-2s %-22s %-12s %s"
                      % (mark.get(r["state"], r["state"]), r["slug"],
                         r["publisher"], r["why"] or ""))
                print("     " + str(r["url"]))
            n = {s: sum(1 for r in rows if r["state"] == s)
                 for s in (CHECK_OK, CHECK_CHANGED, CHECK_UNUSABLE)}
            print()
            print("控え %d件： 同じ %d ／ ★変わった %d★ ／ 読めない %d"
                  % (len(rows), n[CHECK_OK], n[CHECK_CHANGED],
                     n[CHECK_UNUSABLE]))
            if a.apply:
                print("★保存しました★（同じページのものだけ手がかりを取り直し）")
                print("　まだ同じ機種だと人が判断したものは、"
                      "--accept-current --slug X --url Y --why … --by 運営者")
            else:
                print("※見るだけです。保存するには --apply を付けます")
            # ★読めないだけでも成功にしない★（依頼135・P2）
            #   廃止された出典が誰の目にも触れず残るのを防ぐ。
            if n[CHECK_CHANGED]:
                return 1
            return 2 if n[CHECK_UNUSABLE] else 0
        if a.missing:
            rows = missing()
            print("★2つの系列に届かない機種: %d件★" % len(rows))
            for r in rows:
                print("  %-22s %s  （いま: %s）%s"
                      % (r["slug"], r["name"], "/".join(
                          str(x) for x in r["have"]) or "なし",
                         ("  ★隔離中: %s★" % "/".join(
                             str(x) for x in r["quarantined"]))
                         if r.get("quarantined") else ""))
            return 0
        if a.list:
            data = load()
            for slug, rows in sorted((data.get("machines") or {}).items()):
                if a.slug and slug != a.slug:
                    continue
                print("■ " + slug)
                for r in rows:
                    print("   [%s] %s" % (r.get("lineage"), r.get("url")))
                    print("      %s ／ %s（%s）"
                          % (r.get("why"), ",".join(r.get("decided_by") or []),
                             r.get("decided_at")))
            return 0
    except SourceError as e:
        print("★" + str(e) + "★")
        return 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

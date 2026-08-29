# -*- coding: utf-8 -*-
"""popular_machines.py — ★人気機種の順位を、DMMの人気順から取る★

★なぜ要るか（2026-08-29・運営者の指示）★
  更新タスクは「人気機種を優先して直す」ことになっているが、
  その順番（`machines.json` の並び）は★手で並べ替えるしかなく、
  最後に並べ替えたのは2026-05-04＝約4か月前★だった。
  ＝もう打たれていない機種を優先し続け、いま人気の機種が後回しになる。

  ★運営者の言葉★＝「いやいや　自動化しないと意味ないじゃん」

★どこから取るか★
  https://p-town.dmm.com/machines/popularity/slot （DMMぱちタウンの人気順）
  ・並びは `sort=pv_desc`＝★DMM上で見られている順★
  ・★DMMは規約を確認済みの許可先★（automation-policy）
  ・各機種に `/machines/<機種ID>` のリンクがある
    ＝★機種IDで結び付けられる★ので、名前の文字合わせに頼らない

★★名前の辞書を作らない★★（プロジェクトの決まり）
  DMMとうちどころで機種名の書き方が違う（実測4件）＝
    Lパチスロ からくりサーカス2 ／ Lからくりサーカス2
    スロット ソードアート・オンラインⅡ ／ スマスロ ソードアート・オンラインⅡ
  ★接頭辞の辞書を作ると、出るたびに増える★ので作らない。
  ①機種IDの控えで引く（一度結び付ければ永続。DMMのIDは変わらない）
  ②控えに無ければ、機種名が★完全に一致★するときだけ結び付けて控える
  ③どちらでも決まらなければ★2AIへの質問として出す★（機械は決めない）

★取れなかったら並べ替えない★（fail-closed）
  順番が壊れるより、古いままのほうがまし。

使い方:
    python scripts/popular_machines.py              # 下見（書き込まない）
    python scripts/popular_machines.py --apply      # 控えと一覧を書く
    python scripts/popular_machines.py --selftest
"""
from __future__ import annotations
import argparse
import html.parser
import io
import contextlib
import errno
import json
import os
import re
import time
import sys
import unicodedata

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                        # noqa: BLE001
    pass

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import local_paths as _lp                                # noqa: E402
import safe_json as _sj                                  # noqa: E402

MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
# ★控えはリポジトリの外★（公開物ではないため）
STORE = _lp.doc("popular_machines.json")
SCHEMA = "popular-machines/v1"
URL = "https://p-town.dmm.com/machines/popularity/slot"
# ★人気枠に入れる数★（運営者の指示・2026-08-29）
TOP_N = 20


class PopularError(Exception):
    """人気順を読めないときの合図。★黙って古い順番を使わない★"""


class _Parser(html.parser.HTMLParser):
    """★HTML解析で読む★（正規表現で読まない＝プロジェクトの決まり）"""

    def __init__(self):
        super().__init__()
        self.rows: list = []
        self._mid = None
        self._buf: list = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "a":
            m = re.fullmatch(r"/machines/(\d+)", str(d.get("href") or ""))
            self._mid = m.group(1) if m else None
            self._buf = []
        elif tag == "img" and self._mid and d.get("alt"):
            self._buf.append(str(d["alt"]))

    def handle_data(self, data):
        if self._mid:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._mid:
            self.rows.append((self._mid, "".join(self._buf)))
            self._mid, self._buf = None, []


def _clean_name(raw: str) -> str:
    """★リンクの中の文字から機種名だけを取り出す★

    1〜10位は「機種名 ＋ N位 機種名 メーカー 機械割…」と説明が続き、
    11〜20位は「N位 機種名」だけ。★順位の印を目印に切る★。
    """
    t = re.sub(r"\s+", " ", str(raw or "")).strip()
    t = re.sub(r"^\s*\d+位\s*", "", t)          # 先頭の「N位」を落とす
    return re.split(r"\s*\d+位\s*", t)[0].strip()


def parse(html_text: str) -> list:
    """人気順のページから (順位, 機種ID, 機種名) を取り出す。

    ★★順位は自分で作らない★★（2026-08-29・Codexの指摘2）
      ★直す前は「出てきた順＝順位」としていた★ので、
      一覧ページへ転送された・おすすめ枠が足された場合でも、
      ★20件のリンクさえあれば誤った順位を保存★していた。
      いまは★ページが言う「N位」を読む★。
    """
    p = _Parser()
    p.feed(str(html_text or ""))
    out, seen = [], set()
    for mid, raw in p.rows:
        if mid in seen:
            continue
        t = re.sub(r"\s+", " ", str(raw or "")).strip()
        m = re.match(r"^\s*(\d+)位\s*", t)
        if not m:
            # ★1〜10位は「機種名 N位 機種名 …」の形★（先頭に画像の alt）
            m = re.search(r"(\d+)位", t)
        if not m:
            continue                            # ★順位が読めない行は使わない★
        name = _clean_name(t)
        if not name:
            continue
        seen.add(mid)
        out.append((int(m.group(1)), mid, name))
    out.sort(key=lambda r: r[0])
    return out


def check_ranks(top: list) -> None:
    """★1位から順に、抜けも重なりもないこと★（2026-08-29・Codexの指摘2）

    ★これが無いと、人気ページでなくても「20件あるから成功」になる★。
    """
    ranks = [r for r, _m, _n in top]
    want = list(range(1, TOP_N + 1))
    if ranks[:TOP_N] != want:
        raise PopularError(
            f"順位が1〜{TOP_N}位でそろっていません（読めた順位: "
            f"{ranks[:TOP_N]}）。★人気順のページではないかもしれません★")


def norm(s) -> str:
    """★そろえるのは空白だけ★（2026-08-29・Codexの指摘4）

    ★直す前は長音符・中黒・括弧・スラッシュまで落としていた★ので、
    ★別の機種の名前と偶然一致し得た★（しかも控えに永続する）。
    ★接頭辞の違い（スロット／スマスロ／Lパチスロ／L）は、
      どのみち完全一致では埋まらない★＝そこは2AIが判断する。
    ＝広く削る意味がなく、危ないだけだった。
    """
    s = unicodedata.normalize("NFKC", str(s or ""))
    return re.sub(r"\s+", "", s).lower()


def load_store() -> dict:
    """機種ID → うちどころのslug の控え。

    ★★「まだ無い」と「壊れている」を分ける★★（2026-08-29・Codexの指摘1）
      ★直す前は、読めない・形が違うを全部「初回の空」と同じ扱いにしていた★。
      そのあと `--apply` が同じファイルを上書きするので、
      ★一時的な破損や書き込み途中の停止で、
        2AIが決めた対応が丸ごと消える★（人が決めたものを壊す経路）。
      いまは★壊れていたら止まる★＝人が中身を見てから直す。
    """
    if not os.path.isfile(STORE):
        return {"schema_version": SCHEMA, "by_dmm_id": {}}
    try:
        got = _sj.read_json(STORE, expect=dict)
    except Exception as e:                               # noqa: BLE001
        raise PopularError(
            f"控えを読めません（{type(e).__name__}）。"
            f"★中身を確かめてから直してください★: {STORE}")
    if str(got.get("schema_version") or "") != SCHEMA:
        raise PopularError(
            f"控えの版が違います（{got.get('schema_version')!r}）: {STORE}")
    if not isinstance(got.get("by_dmm_id"), dict):
        raise PopularError(f"控えの形が違います（by_dmm_id）: {STORE}")
    return got


def resolve(top: list, machines: list, store: dict) -> dict:
    """人気順を、うちどころのslugへ結び付ける。

    ★返すもの★
      ranked    … 順位順のslug（うちどころに記事があるものだけ）
      learned   … 新しく結び付いた {機種ID: slug}（控えへ足す分）
      questions … 決められなかったもの（★2AIへ回す★）
    """
    by_id, by_name, slugs = {}, {}, set()
    for m in machines:
        if not isinstance(m, dict) or not m.get("slug"):
            continue
        slugs.add(m["slug"])
        u = str((m.get("identity") or {}).get("official_product_url") or "")
        g = re.search(r"/machines/(\d+)", u)
        if g:
            by_id[g.group(1)] = m["slug"]
        for nm in [m.get("name")] + list(m.get("aliases") or []):
            if not nm:
                continue
            # ★★同じ見た目が「別の機種」にあれば、名前では決めない★★
            #   ★同じ機種の中で名前と別名がぶつかるのは曖昧ではない★
            #   （実機で判明：真打 吉宗 は name「真打 吉宗」と
            #     alias「真打吉宗」が同じ形になり、
            #     正しく結び付くはずの機種が毎回2AIへの質問になっていた）
            k = norm(nm)
            if k in by_name and by_name[k] not in (None, m["slug"]):
                by_name[k] = None          # ★別の機種とぶつかった★
            elif k not in by_name:
                by_name[k] = m["slug"]

    kept = {k: v for k, v in (store.get("by_dmm_id") or {}).items()
            if v in slugs}                    # ★消えた機種の控えは使わない★
    ranked, learned, questions = [], {}, []
    for rank, mid, name in top:
        # ★★いまの機種ページのIDを、控えより優先する★★
        #   （2026-08-29・Codexの指摘4）
        #   ★直す前は控えが勝った★ので、machines.json を正しく直しても
        #   誤った対応が残り続けた。
        slug = by_id.get(mid) or kept.get(mid)
        if not slug:
            hit = by_name.get(norm(name))
            if hit:
                slug, learned[mid] = hit, hit
        if slug:
            if slug not in ranked:
                ranked.append(slug)
            continue
        questions.append({
            "dmm_id": mid, "rank": rank, "name": name,
            "text": ("★この機種が、うちどころのどの記事にあたるか判断してください★"
                     f"／DMMの人気{rank}位「{name}」"
                     f"（https://p-town.dmm.com/machines/{mid}）"
                     "／★記事が無ければ『無し』と答えてください★"
                     "／★機械は名前の書き方の違いを吸収しません★"
                     "（辞書を増やさないため）"),
        })
    return {"ranked": ranked, "learned": learned, "questions": questions}


def should_run(today, checked_at: str = "", pending: int = 0) -> tuple:
    """★今日、人気順を取り直すか★（2026-08-29・運営者の指示）

    ★返すもの★＝(取るか, 理由)

    ★決まり★＝月曜、または前回から7日以上経っていたら取る。
      ★月曜だけにしない★＝その日に通信が失敗すると
      ★丸1週間古いまま★になる。追いつけるようにしておく。
      ★同じ日に2回は取らない★（すでに今日取っていれば何もしない）。
    """
    import datetime as _dt
    if isinstance(today, str):
        today = _dt.date.fromisoformat(today)
    if int(pending or 0) > 0:
        # ★★決められない機種が残っている間は、必ず取り直す★★
        #   （2026-08-29・Codexの指摘3）
        #   ★直す前は確認日だけで決めていた★ので、
        #   「4日前に成功 → 昨日は決められず失敗」の翌日は
        #   「まだ7日たっていない」で取りに行かなかった＝
        #   ★「翌日も取り直す」が成立していなかった★。
        return True, f"決められない機種が {int(pending)} 件残っています"
    got = str(checked_at or "").strip()
    if not got:
        return True, "まだ一度も取っていません"
    try:
        last = _dt.date.fromisoformat(got)
    except ValueError:
        return True, f"前回の日付を読めません（{got!r}）"
    if last == today:
        return False, "今日はもう取っています"
    days = (today - last).days
    if days >= 7:
        return True, f"前回から{days}日たっています"
    if today.weekday() == 0:              # 0 = 月曜
        return True, "月曜です"
    return False, f"前回から{days}日（月曜でも7日でもありません）"


def fetch(fetcher=None) -> str:
    """★用途を名乗ってから取りに行く★（名簿を通る唯一の口）"""
    if fetcher is not None:
        return fetcher(URL)
    import new_machine_watch as _nw
    with _nw.fetching("popularity_rank"):
        return _nw._get(URL)


_LOCK_WAIT = 15.0        # 鍵が空くのを待つ最大の秒数
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
    """★控えを書くあいだの鍵★（2026-08-29・Codexの2周目の指摘1）

    ★読み直すだけでは足りない★＝読み直してから置き換えるまでの
    ごく短い隙に、別の実行が書いた分を消せる。
    消えるのは★2AIが決めた対応★なので、鍵で囲む。
    ★やり方は machine_sources と同じ★（OSに持たせる／鍵のファイルは消さない）。
    """
    path = STORE + ".lock"
    os.makedirs(os.path.dirname(STORE) or ".", exist_ok=True)
    started = time.time()
    with open(path, "a+b") as fh:
        while True:
            try:
                _hold(fh, True)
                break
            except OSError as e:
                if e.errno not in _BUSY:
                    raise PopularError(f"控えの鍵を扱えません（{e}）")
                if time.time() - started > _LOCK_WAIT:
                    raise PopularError(
                        "控えが他の実行に使われています"
                        "（あとでやり直してください）")
                time.sleep(0.15)
        try:
            yield
        finally:
            try:
                _hold(fh, False)
            except OSError:
                pass


def _write_store(mutate) -> dict:
    """★鍵を取り、読み直して、足す形で書く★（2026-08-29・Codexの指摘2）

    ★直す前は「全部読んで・全部置き換える」だった★ので、
    読んでから書くまでの間に別の処理が足した対応
    （2AIが決めた 機種ID→slug）を消しうる。
    ★控えは人が決めたものなので、消す方向には倒さない★。
    """
    with _lock():
        store = load_store()            # ★壊れていたら止まる（消さない）★
        mutate(store)
        # ★一時ファイルは自分専用の名前★（2026-08-29・Codexの2周目）
        #   共通の名前だと、2つ同時に書いたとき中身が混ざる。
        tmp = f"{STORE}.tmp{os.getpid()}"
        io.open(tmp, "w", encoding="utf-8", newline="\n").write(
            json.dumps(store, ensure_ascii=False, indent=1) + "\n")
        os.replace(tmp, STORE)          # ★途中で止まっても空にしない★
    return store


def _decide(top: list, rows: list, store: dict) -> dict:
    """いまの控えを見て「どれを2AIへ聞くか・どれを打ち切るか」を決める。

    ★★鍵の中でもう一度呼ぶ★★（2026-08-30・Codexの3周目の指摘2）
      ★直す前は、鍵を取る前の古い控えから作った `ranked` を
        そのまま保存していた★＝名前の一致で 1001→A と決めたあと、
        鍵を取るまでの間に2AIが 1001→B を記録すると、
        `by_dmm_id` は B（setdefault で守られる）なのに
        `ranked` は A のまま確定し、★サイトには A が出た★。
      ＝取ってくるのは鍵の外でよいが、**決めるのは鍵の中**。
    """
    got = resolve(top, rows, store)
    got["checked_at"] = None                  # 書くときに入れる
    got["report"] = []
    # ★★3回話しても決まらなかった機種は、報告して先へ進む★★
    #   （2026-08-29・運営者の指示「それでも無理な場合報告して
    #     その方向へ倒してもいいよ」）
    tries = store.get("tries") or {}
    seen = store.get("tried_names") or {}

    def _state(q) -> str:
        """ask＝2AIへ聞く／revive＝打ち切りを解く／giveup＝外したまま"""
        mid = str(q.get("dmm_id") or "")
        if int(tries.get(mid) or 0) < GIVE_UP_TRIES:
            return "ask"
        names = seen.get(mid) or []
        if not names:
            return "giveup"             # ★いま打ち切る回★（名前を控える）
        # ★★DMM側の名前が変わったら、また2AIへ聞く★★
        #   （2026-08-29・Codexの2周目の指摘2）
        #   ★打ち切りを永久にしない★＝名前が変わったのは新しい材料なので、
        #   同じ結論になるとは限らない。
        # ★★ただし、前に試した名前へ戻っただけなら聞き直さない★★
        #   （2026-08-30・Codexの3周目の指摘4）
        #   ★直す前は「直前の名前」としか比べていなかった★ので、
        #   A→B→A→B と往復すると**無期限に聞き続けた**。
        return "giveup" if norm(q.get("name") or "") in names else "revive"

    _st = {id(q): _state(q) for q in got["questions"]}
    got["give_up"] = [q for q in got["questions"] if _st[id(q)] == "giveup"]
    got["revived"] = [q for q in got["questions"] if _st[id(q)] == "revive"]
    got["ask_2ai"] = [q for q in got["questions"] if _st[id(q)] != "giveup"]
    got["kept_previous"] = False
    return got


def run(apply_it: bool = False, fetcher=None, machines=None) -> dict:
    """人気順を取って、うちどころのslugへ並べ直す。"""
    rows = machines
    if rows is None:
        rows = _sj.read_json(MACHINES, expect=list)
    try:
        page = fetch(fetcher)
    except Exception as e:                               # noqa: BLE001
        raise PopularError(f"人気順を取れません: {type(e).__name__}: {e}")
    top = parse(page)
    if len(top) < TOP_N:
        # ★少なければ並べ替えない★（ページの作りが変わった合図）
        raise PopularError(
            f"人気順が {len(top)} 件しか取れません（{TOP_N} 件を期待）")
    check_ranks(top)                    # ★1〜20位がそろっているか★
    top = top[:TOP_N]
    store = load_store()
    got = _decide(top, rows, store)
    if apply_it:
        import datetime as _dt

        def _mut(s):
            # ★★鍵の中で決め直す★★（2026-08-30・Codexの3周目の指摘2）
            got.clear()
            got.update(_decide(top, rows, s))
            # ★★新しく分かった対応は「空いている所」にだけ入れる★★
            #   （2026-08-29・Codexの2周目の指摘1）
            #   ★直す前は update() で無条件に上書きしていた★＝
            #   人気順を取っている間に2AIが同じ機種IDへ別の判断を
            #   記録すると、それを黙って消していた。
            #   ★learned は「名前が完全に一致した」だけの弱い根拠★で、
            #   2AIの判断のほうが強い。強いほうを消さない。
            _by = s.setdefault("by_dmm_id", {})
            for _k, _v in got["learned"].items():
                _by.setdefault(_k, _v)
            s["source"] = URL
            s["pending_questions"] = len(got["ask_2ai"])
            # ★★打ち切った機種の名前は、途中で終わる回にも控える★★
            #   （でないと「名前が変わったら解ける」条件が永久に来ない）
            # ★★試した名前は全部覚える★★（2026-08-30・Codexの3周目の指摘4）
            #   ★直前の1つだけだと、A→B→A の往復で無期限に聞き直す★
            _tn = s.setdefault("tried_names", {})
            for q in got["give_up"]:
                _k = str(q.get("dmm_id"))
                _lst = _tn.setdefault(_k, [])
                _nm = norm(q.get("name") or "")
                if _nm not in _lst:
                    _lst.append(_nm)
            # ★名前が変わったものは、回数と報告済みの印だけ0に戻す★
            #   （★試した名前は消さない★＝戻ってきたときに気づくため）
            for q in got["revived"]:
                _mid = str(q.get("dmm_id"))
                (s.get("tries") or {}).pop(_mid, None)
                _esc = s.get("escalated") or []
                if _mid in _esc:
                    _esc.remove(_mid)
            if got["ask_2ai"]:
                # ★★欠けた順位表を正式なものにしない★★
                #   （2026-08-29・Codexの指摘1）
                #   ★直す前は、決められない機種の分だけ短い一覧を保存し、
                #     `--plan` がそれを正しいものとして使っていた★＝
                #   20機種のうち4件が結び付かなかった週は、
                #   その4機種が誰にも触られないまま1週間過ぎる。
                #   ★前の週の完全な一覧をそのまま残す★（古いほうが安全）。
                got["kept_previous"] = bool(s.get("ranked"))
                return
            # ★ここへ来るのは「全部決まった」か
            #   「残りは3回話しても決まらなかった」か のどちらか★
            s["ranked"] = got["ranked"]
            s["checked_at"] = _dt.date.today().isoformat()
            # ★報告は1回だけ★（同じ機種で毎週鳴らさない）
            esc = s.setdefault("escalated", [])
            fresh = [q for q in got["give_up"]
                     if str(q.get("dmm_id")) not in esc]
            for q in fresh:
                esc.append(str(q.get("dmm_id")))
            got["report"] = fresh

        store = _write_store(_mut)
        got["checked_at"] = store.get("checked_at")
    return got


def popular_slugs(machines=None) -> list:
    """★いまの人気機種（順位順）★（控えが無ければ空＝人気枠を使わない）

    ★★いま在る機種だけを返す★★（2026-08-29・Codexの指摘5）
      ★直す前は文字かどうかしか見ていなかった★ので、
      週の途中で記事を消した・slugを変えた場合に、
      ★存在しない機種を毎日返し続けた★。
    """
    try:
        store = load_store()
    except PopularError:
        return []                       # ★壊れていたら人気枠を使わない★
    got = store.get("ranked")
    if not isinstance(got, list):
        return []
    rows = machines
    if rows is None:
        try:
            rows = _sj.read_json(MACHINES, expect=list)
        except Exception:                                # noqa: BLE001
            return []                   # ★機種一覧を読めなければ使わない★
    alive = {m.get("slug") for m in rows if isinstance(m, dict)}
    return [s for s in got if isinstance(s, str) and s in alive]


GIVE_UP_TRIES = 3        # ★これだけ話しても決まらなければ運営者へ報告する★


def record_decision(mid: str, slug: str, by: str, why: str,
                    machines=None) -> dict:
    """★2AIが決めた「機種ID → うちどころのslug」を控える★

    ★機械は名前の違いを埋めない★（辞書が増えるため）。
    決めるのは2AIで、ここは**決まったことを控えるだけの口**。
    """
    mid = str(mid or "").strip()
    slug = str(slug or "").strip()
    if not mid.isdigit():
        raise PopularError(f"機種IDは数字だけです: {mid!r}")
    who = [w.strip() for w in str(by or "").replace("、", ",").split(",")
           if w.strip()]
    if len(set(who)) < 2:
        raise PopularError(
            f"★判断者が2つ要ります★（例 --by claude,codex）: {by!r}")
    if len(str(why or "").strip()) < 15:
        raise PopularError("★なぜそう決めたかを15字以上で書いてください★")
    rows = machines
    if rows is None:
        rows = _sj.read_json(MACHINES, expect=list)
    alive = {m.get("slug") for m in rows if isinstance(m, dict)}
    if slug not in alive:
        raise PopularError(
            f"★うちどころに無い機種です★: {slug!r}"
            "（machines.json に実在する slug を指してください）")

    box = {}

    def _mut(s):
        cur = (s.get("by_dmm_id") or {}).get(mid)
        # ★★消えた機種を指す古い対応は、上書きしてよい★★
        #   （2026-08-29・Codexの2周目の指摘2）
        #   ★直す前は、記事を消した／slugを変えた機種の古い対応が残ると
        #     `--record` が「すでに別の機種」と断り、
        #     控えを手で直すしか戻せなかった★＝死んだ記録が復旧を塞ぐ。
        if cur and cur != slug and cur in alive:
            raise PopularError(
                f"★すでに別の機種に結び付いています★: {mid} → {cur}"
                f"（{slug} にするなら、先に控えを直してください）")
        s.setdefault("by_dmm_id", {})[mid] = slug
        s.setdefault("decisions", []).append(
            {"dmm_id": mid, "slug": slug, "by": who, "why": why.strip()})
        # ★決まったら回数は0に戻す★
        (s.get("tries") or {}).pop(mid, None)
        esc = s.get("escalated") or []
        if mid in esc:
            esc.remove(mid)          # ★決まったら報告済みの印も外す★
        box["slug"] = slug

    _write_store(_mut)
    return box


def note_try(mid: str) -> int:
    """★2AIで話しても決まらなかった回を1つ数える★（戻り値＝いま何回目か）"""
    mid = str(mid or "").strip()
    if not mid.isdigit():
        raise PopularError(f"機種IDは数字だけです: {mid!r}")
    box = {}

    def _mut(s):
        tr = s.setdefault("tries", {})
        tr[mid] = int(tr.get(mid) or 0) + 1
        box["n"] = tr[mid]

    _write_store(_mut)
    return box["n"]


def week_start(today) -> str:
    """★その週の月曜★（人気順を取り直す日）"""
    import datetime as _dt
    if isinstance(today, str):
        today = _dt.date.fromisoformat(today)
    return (today - _dt.timedelta(days=today.weekday())).isoformat()


def plan_today(today, budget: int, machines=None, store=None) -> dict:
    """★今日の枠を「人気機種」と「その他」へ割り振る★

    ★運営者が決めた並び（2026-08-29）★
      月 人気6／火 人気6／水 人気6／木 人気2＋その他4／金土日 その他6

    ★曜日では決めない★＝1日でも動かなかった週は舐め終わらない。
    ★今週まだ見ていない人気機種から先に埋める★＝
    全部動いた週は上の表と同じになり、止まった日があっても追いつく。
    """
    budget = max(0, int(budget))
    wk = week_start(today)
    if store is None:
        try:
            store = load_store()
        except PopularError:
            store = {}                  # ★壊れていたら全部「その他」★
    rot = store.get("rotation") or {}
    done = rot.get("done") or [] if rot.get("week") == wk else []
    order = popular_slugs(machines)
    todo = [s for s in order if s not in done]
    take = todo[:budget]
    got_at = str(store.get("checked_at") or "")
    return {"week": wk, "popular": take, "other": budget - len(take),
            "done_this_week": len(order) - len(todo), "ranked": len(order),
            # ★今週まだ取り直せていないなら、その日付を返す★
            "stale": got_at if (got_at and got_at < wk) else ""}


def mark_done(slug: str, today) -> dict:
    """★今週ぶんの人気機種を1件、見終わったと控える★

    ★書けたかどうかではなく「担当して見終わった」で数える★＝
    直すところが無い機種で枠が止まらないようにする。
    """
    wk = week_start(today)
    box = {}

    def _mut(s):
        rot = s.get("rotation") or {}
        if rot.get("week") != wk:
            rot = {"week": wk, "done": []}
        if slug and slug not in rot["done"]:
            rot["done"].append(slug)
        s["rotation"] = rot
        box["rot"] = rot

    _write_store(_mut)                  # ★書く直前に読み直す★（指摘2）
    return box["rot"]


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    import datetime as _dt
    import shutil
    import tempfile
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("OK   " if cond else "NG   ") + name)

    _PAGE = (
        '<a href="/machines/100"><img alt="L 東京喰種"> 1位 L 東京喰種 '
        'スパイキー 機械割: 97.5% 2位 別の機種</a>'
        '<a href="/machines/200">11位 Lからくりサーカス2 12位 次の機種</a>'
        '<a href="/machines/300">12位 知らない機種</a>'
        '<a href="/machines/100">重複するリンク</a>')
    got = parse(_PAGE)
    t("★★順位はページが言う「N位」を読む★★（自分で作らない）",
      [(r, m, n) for r, m, n in got]
      == [(1, "100", "L 東京喰種"), (11, "200", "Lからくりサーカス2"),
          (12, "300", "知らない機種")])
    t("　★同じ機種が2回出ても1回だけ数える★", len(got) == 3)
    t("　★順位の印が無いリンクは使わない★",
      parse('<a href="/machines/900">順位の無い機種</a>') == [])
    t("　★順位の順に並べ直す★（ページの並びに頼らない）",
      [r for r, _m, _n in parse(
          '<a href="/machines/1">9位 あ</a><a href="/machines/2">3位 い</a>'
      )] == [3, 9])

    # ★★指摘②：人気ページでなければ止まる★★
    _ok = [(i, str(i), f"機種{i}") for i in range(1, TOP_N + 1)]
    t("★★1〜20位がそろっていれば通る★★", check_ranks(_ok) is None)

    def _raises(fn):
        try:
            fn()
        except PopularError:
            return True
        return False

    t("　★順位が抜けていたら止まる★",
      _raises(lambda: check_ranks(
          [(i, str(i), "x") for i in list(range(1, TOP_N)) + [TOP_N + 1]])))
    t("　★1位から始まっていなければ止まる★",
      _raises(lambda: check_ranks(
          [(i, str(i), "x") for i in range(2, TOP_N + 2)])))
    _dupe = [(i, str(i), "x") for i in range(1, TOP_N)]
    _dupe.insert(0, (1, "zz", "x"))
    t("　★同じ順位が2つあれば止まる★", _raises(lambda: check_ranks(_dupe)))

    def _page_of(pairs):
        return "".join(f'<a href="/machines/{m}">{r}位 {n}</a>'
                       for r, m, n in pairs)

    def _fake20(prefix=""):
        return [(i, str(1000 + i), f"{prefix}機種{i}")
                for i in range(1, TOP_N + 1)]

    _ms20 = [{"slug": f"s{i}", "name": f"機種{i}"}
             for i in range(1, TOP_N + 1)]
    t("★★おすすめ枠が同じ順位を名乗ったら止まる★★",
      _raises(lambda: run(
          False,
          fetcher=lambda _u: _page_of(_fake20()) +
          '<a href="/machines/7777">おすすめ 3位 別の機種</a>',
          machines=_ms20)))
    t("　★20位より後ろの枠が増えても、上位20件は使える★"
      "（順位が抜けていない）",
      len(run(False,
              fetcher=lambda _u: _page_of(_fake20()) +
              '<a href="/machines/7777">99位 別の機種</a>',
              machines=_ms20)["ranked"]) == TOP_N)
    t("　★一覧ページへ転送されたら止まる★（順位の印が無い）",
      _raises(lambda: run(
          False,
          fetcher=lambda _u: "".join(
              f'<a href="/machines/{2000 + i}">機種{i}</a>'
              for i in range(1, 31)),
          machines=_ms20)))

    _MS = [{"slug": "tokyo_ghoul", "name": "L 東京喰種"},
           {"slug": "karakuri2", "name": "Lからくりサーカス2"},
           {"slug": "dmm_x", "name": "別名の機種",
            "identity": {"official_product_url":
                         "https://p-town.dmm.com/machines/300"}}]
    r = resolve(got, _MS, {"by_dmm_id": {}})
    t("★★人気順にslugが並ぶ★★",
      r["ranked"] == ["tokyo_ghoul", "karakuri2", "dmm_x"])
    t("　★機種IDで結び付いたものは、名前を見ない★",
      "300" not in r["learned"])
    t("　★名前が完全に一致したものは控えへ★",
      r["learned"] == {"100": "tokyo_ghoul", "200": "karakuri2"})

    r2 = resolve([(1, "999", "うちどころに無い機種")], _MS, {"by_dmm_id": {}})
    t("★★決められないものは2AIへの質問にする★★"
      "／★機械が名前の書き方の違いを埋めない（辞書を増やさない）★",
      r2["ranked"] == [] and len(r2["questions"]) == 1
      and "999" in r2["questions"][0]["text"])

    _DUP = [{"slug": "a", "name": "同じ名前"}, {"slug": "b", "name": "同じ名前"}]
    r3 = resolve([(1, "1", "同じ名前")], _DUP, {"by_dmm_id": {}})
    t("★★同じ見た目が2機種にあれば、名前では決めない★★",
      r3["ranked"] == [] and len(r3["questions"]) == 1)
    # ★★同じ機種の中で名前と別名がぶつかるのは、曖昧ではない★★
    #   （実機で判明：真打 吉宗 は name「真打 吉宗」と alias「真打吉宗」が
    #     同じ形になり、★正しく結び付く機種が毎回質問になっていた★）
    _SELF = [{"slug": "shinuchi", "name": "真打 吉宗",
              "aliases": ["真打吉宗", "しんうち"]}]
    r5 = resolve([(1, "1", "真打 吉宗")], _SELF, {"by_dmm_id": {}})
    t("★★同じ機種の中で名前と別名がぶつかっても、結び付く★★"
      "／★これが無いと、正しい機種が毎回2AIへの質問になる★",
      r5["ranked"] == ["shinuchi"] and r5["questions"] == [])

    r4 = resolve(got, _MS, {"by_dmm_id": {"100": "消えた機種"}})
    t("★★控えが指すslugが無くなっていたら、その控えは使わない★★",
      "tokyo_ghoul" in r4["ranked"])

    _raised = False
    try:
        run(fetcher=lambda u: '<a href="/machines/1">1位 ひとつだけ</a>',
            machines=_MS)
    except PopularError as e:
        _raised = "件しか取れません" in str(e)
    t("★★20件そろわなければ並べ替えない★★"
      "／★順番が壊れるより、古いままのほうがまし★", _raised)

    _raised2 = False
    try:
        def _boom(u):
            raise OSError("ためしの通信失敗")
        run(fetcher=_boom, machines=_MS)
    except PopularError:
        _raised2 = True
    t("★★取れなかったら並べ替えない★★", _raised2)

    # ★★週1回（月曜）だけ取る★★（2026-08-29・運営者の指示）
    t("　まだ一度も取っていなければ取る", should_run("2026-08-29", "")[0])
    t("★★月曜なら取る★★", should_run("2026-08-31", "2026-08-29")[0])
    t("　月曜でなく、前回から日が浅ければ取らない",
      should_run("2026-08-30", "2026-08-29")[0] is False)
    t("★★月曜に失敗しても、7日たてば追いつく★★"
      "／★月曜だけにすると、その日に失敗して丸1週間古いままになる★",
      should_run("2026-09-06", "2026-08-30")[0])
    t("　同じ日に2回は取らない",
      should_run("2026-09-01", "2026-09-01")[0] is False)
    t("　前回の日付が壊れていたら取る（古いまま使わない）",
      should_run("2026-08-29", "こわれています")[0])

    import inspect as _insp
    t("★★用途を名乗ってから取りに行く★★"
      "／★名乗らないと名簿の関所を通らない★",
      'fetching("popularity_rank")' in _insp.getsource(fetch))

    # ★★指摘③：決められない機種が残るなら確認日を進めない★★
    global STORE
    _keep = STORE
    _tmpdir = tempfile.mkdtemp(prefix="popular_test_")
    try:
        STORE = os.path.join(_tmpdir, "popular_machines.json")

        def _page20(names):
            return "".join(
                f'<a href="/machines/{1000 + i}">{i}位 {names(i)}</a>'
                for i in range(1, TOP_N + 1))

        # 1件だけ、うちどころに無い名前にする
        _ms = [{"slug": f"s{i}", "name": f"機種{i}"}
               for i in range(1, TOP_N)]
        r3 = run(True, fetcher=lambda _u: _page20(lambda i: f"機種{i}"),
                 machines=_ms)
        t("★★決められない機種が残る★★", len(r3["questions"]) == 1)
        t("　★そのとき確認日は進めない★", not r3.get("checked_at"))
        _saved = json.loads(io.open(STORE, encoding="utf-8").read())
        t("　★控えにも確認日を書かない★", not _saved.get("checked_at"))
        t("　★何件残っているかは控えに残す★",
          _saved.get("pending_questions") == 1)
        t("　★決まった分の対応は控える★（次回の質問を減らす）",
          len(_saved.get("by_dmm_id") or {}) == TOP_N - 1)

        # 全部そろえば確認日が入る
        _ms_all = [{"slug": f"s{i}", "name": f"機種{i}"}
                   for i in range(1, TOP_N + 1)]
        r4 = run(True, fetcher=lambda _u: _page20(lambda i: f"機種{i}"),
                 machines=_ms_all)
        t("★★全部決まれば確認日が入る★★", bool(r4.get("checked_at")))
        t("　★決まったら順位表を保存する★",
          len(json.loads(io.open(STORE, encoding="utf-8").read())
              .get("ranked") or []) == TOP_N)

        # ★★指摘1：欠けた順位表を正式なものにしない★★
        _full = [f"s{i}" for i in range(1, TOP_N + 1)]
        io.open(STORE, "w", encoding="utf-8", newline="\n").write(
            json.dumps({"schema_version": SCHEMA,
                        "by_dmm_id": {}, "ranked": _full,
                        "checked_at": "2026-08-24"},
                       ensure_ascii=False) + "\n")
        r5 = run(True, fetcher=lambda _u: _page20(lambda i: f"機種{i}"),
                 machines=_ms)                      # 1件足りない機種一覧
        _s5 = json.loads(io.open(STORE, encoding="utf-8").read())
        t("★★決められない日は、前の週の完全な順位表を残す★★"
          "（欠けた一覧を正式にしない）", _s5.get("ranked") == _full)
        t("　★そう報告する★", r5.get("kept_previous") is True)
        t("　★確認日も動かさない★", _s5.get("checked_at") == "2026-08-24")
        t("　★それでも分かった対応は足す★",
          len(_s5.get("by_dmm_id") or {}) == TOP_N - 1)
        t("　★その日の割り振りは、前の週の20件から出す★",
          plan_today("2026-08-31", 6, _ms_all)["popular"]
          == ["s1", "s2", "s3", "s4", "s5", "s6"])

        # 初回から決まらないときは、人気枠を使わない
        io.open(STORE, "w", encoding="utf-8", newline="\n").write(
            json.dumps({"schema_version": SCHEMA, "by_dmm_id": {}},
                       ensure_ascii=False) + "\n")
        r6 = run(True, fetcher=lambda _u: _page20(lambda i: f"機種{i}"),
                 machines=_ms)
        t("★★一度も決まっていないなら人気枠を使わない★★",
          r6.get("kept_previous") is False
          and not (json.loads(io.open(STORE, encoding="utf-8").read())
                   .get("ranked")))

        # ★★指摘2：書く直前に読み直す★★
        io.open(STORE, "w", encoding="utf-8", newline="\n").write(
            json.dumps({"schema_version": SCHEMA,
                        "by_dmm_id": {"1": "a"}}, ensure_ascii=False) + "\n")
        _stale = load_store()                       # ★古い読み取り★
        io.open(STORE, "w", encoding="utf-8", newline="\n").write(
            json.dumps({"schema_version": SCHEMA,
                        "by_dmm_id": {"1": "a", "2": "b"}},
                       ensure_ascii=False) + "\n")  # 別の処理が足した
        _write_store(lambda s: s.setdefault("by_dmm_id", {}).update(
            {"3": "c"}))
        _s7 = json.loads(io.open(STORE, encoding="utf-8").read())
        t("★★書く直前に読み直す★★"
          "（読んでから書くまでに足された対応を消さない）",
          _s7["by_dmm_id"] == {"1": "a", "2": "b", "3": "c"}
          and _stale["by_dmm_id"] == {"1": "a"})

        # ★★2AIで解決する経路★★（2026-08-29・運営者の指示）
        io.open(STORE, "w", encoding="utf-8", newline="\n").write(
            json.dumps({"schema_version": SCHEMA, "by_dmm_id": {}},
                       ensure_ascii=False) + "\n")
        _WHY = "DMMの表記ゆれ。機種ページの導入日と メーカー欄が一致した。"

        def _rec(**kw):
            args = {"mid": "1020", "slug": "s20", "by": "claude,codex",
                    "why": _WHY, "machines": _ms_all}
            args.update(kw)
            try:
                record_decision(**args)
                return None
            except PopularError as e:
                return str(e)

        t("★★2AIが決めた対応を控えられる★★", _rec() is None)
        t("　★控えに入る★",
          (json.loads(io.open(STORE, encoding="utf-8").read())
           .get("by_dmm_id") or {}).get("1020") == "s20")
        t("　★根拠も残す★",
          (json.loads(io.open(STORE, encoding="utf-8").read())
           .get("decisions") or [{}])[-1].get("why") == _WHY)
        t("　★判断者が1つなら断る★", bool(_rec(mid="1019", by="claude")))
        t("　★理由が短ければ断る★", bool(_rec(mid="1019", why="ゆれ")))
        t("　★うちどころに無い機種は断る★",
          bool(_rec(mid="1019", slug="no_such_machine")))
        t("　★機種IDが数字でなければ断る★", bool(_rec(mid="abc")))
        t("　★すでに別の機種に結び付いていれば断る★",
          bool(_rec(mid="1020", slug="s19")))

        # ★3回話しても決まらなければ報告して先へ進む★
        io.open(STORE, "w", encoding="utf-8", newline="\n").write(
            json.dumps({"schema_version": SCHEMA, "by_dmm_id": {}},
                       ensure_ascii=False) + "\n")
        _f = lambda _u: _page20(lambda i: f"機種{i}")     # noqa: E731
        r7 = run(True, fetcher=_f, machines=_ms)
        t("★★1回目は2AIへ回す（報告しない）★★",
          len(r7["ask_2ai"]) == 1 and not r7["report"])
        t("　★1回目は順位表を確定しない★", not r7.get("checked_at"))
        t("　★回数を数えられる★", note_try("1020") == 1)
        note_try("1020")
        t("　★3回目で打ち切りになる★", note_try("1020") == GIVE_UP_TRIES)
        r8 = run(True, fetcher=_f, machines=_ms)
        t("★★3回話しても決まらなければ報告して先へ進む★★",
          not r8["ask_2ai"] and len(r8["report"]) == 1)
        t("　★残りで順位表を確定する★",
          len(json.loads(io.open(STORE, encoding="utf-8").read())
              .get("ranked") or []) == TOP_N - 1
          and bool(r8.get("checked_at")))
        r9 = run(True, fetcher=_f, machines=_ms)
        t("　★報告は1回だけ★（毎週鳴らさない）", not r9["report"])
        t("★★あとから決まれば、また人気枠に戻る★★",
          _rec() is None
          and len(run(True, fetcher=_f,
                      machines=_ms_all)["ranked"]) == TOP_N)

        # ★★2周目1：取っている最中に2AIが記録した判断を消さない★★
        #   ★本当に競り合わせる★＝run() が最初に読んだときは控えが空で、
        #   書く直前に読み直したときには2AIの判断が入っている状態を作る。
        #   （そうしないと resolve() が先に結び付けてしまい、
        #     上書きする側の処理を一度も通らない＝罠④）
        io.open(STORE, "w", encoding="utf-8", newline="\n").write(
            json.dumps({"schema_version": SCHEMA,
                        "by_dmm_id": {"1001": "s20"}},
                       ensure_ascii=False) + "\n")
        _real_load, _seen = load_store, []

        def _racing_load():
            s = _real_load()
            _seen.append(1)
            if len(_seen) == 1:             # ★1回目＝まだ記録されていない★
                s = dict(s)
                s["by_dmm_id"] = {}
            return s

        globals()["load_store"] = _racing_load
        try:
            run(True, fetcher=_f, machines=_ms_all)
        finally:
            globals()["load_store"] = _real_load
        t("★★取っている最中に2AIが記録した判断を、名前の一致で消さない★★",
          (json.loads(io.open(STORE, encoding="utf-8").read())
           .get("by_dmm_id") or {}).get("1001") == "s20")

        # ★★2周目2a：消えた機種を指す古い対応は上書きできる★★
        io.open(STORE, "w", encoding="utf-8", newline="\n").write(
            json.dumps({"schema_version": SCHEMA,
                        "by_dmm_id": {"1020": "kieta_kishu"}},
                       ensure_ascii=False) + "\n")
        t("★★消えた機種を指す古い対応は、上書きして直せる★★"
          "（控えを手で直さずに戻せる）", _rec() is None)
        io.open(STORE, "w", encoding="utf-8", newline="\n").write(
            json.dumps({"schema_version": SCHEMA,
                        "by_dmm_id": {"1020": "s19"}},
                       ensure_ascii=False) + "\n")
        t("　★いま在る機種を指しているなら、やはり断る★", bool(_rec()))

        # ★★2周目2b：DMM側の名前が変わったら、また2AIへ聞く★★
        io.open(STORE, "w", encoding="utf-8", newline="\n").write(
            json.dumps({"schema_version": SCHEMA, "by_dmm_id": {},
                        "tries": {"1020": GIVE_UP_TRIES}},
                       ensure_ascii=False) + "\n")
        rA = run(True, fetcher=_f, machines=_ms)
        t("★★打ち切った機種は、試した名前を控える★★",
          (json.loads(io.open(STORE, encoding="utf-8").read())
           .get("tried_names") or {}).get("1020") == [norm("機種20")]
          and len(rA["give_up"]) == 1)
        rB = run(True, fetcher=_f, machines=_ms)
        t("　★名前が同じうちは、打ち切ったまま★",
          len(rB["give_up"]) == 1 and not rB["ask_2ai"])
        _f2 = (lambda _u: _page20(                     # noqa: E731
            lambda i: "機種20 改" if i == TOP_N else f"機種{i}"))
        rC = run(True, fetcher=_f2, machines=_ms)
        t("★★名前が変わったら、また2AIへ聞く★★（永久除外にしない）",
          len(rC["revived"]) == 1 and len(rC["ask_2ai"]) == 1
          and not rC["give_up"])
        t("　★回数も報告済みの印も0に戻す★",
          not (json.loads(io.open(STORE, encoding="utf-8").read())
               .get("tries") or {}).get("1020"))

        # ★★3周目2：決めるのは鍵の中★★
        #   ★競り合い★＝run() が最初に読んだときは控えが空で、
        #   書く直前に読み直したときには2AIの判断（1001→s20）が入っている。
        #   ★ranked も鍵の中で決め直していないと、名前の一致で決めた
        #     s1 のほうが保存され、サイトに出てしまう★
        io.open(STORE, "w", encoding="utf-8", newline="\n").write(
            json.dumps({"schema_version": SCHEMA,
                        "by_dmm_id": {"1001": "s20"}},
                       ensure_ascii=False) + "\n")
        _real2, _seen2 = load_store, []

        def _racing2():
            s = _real2()
            _seen2.append(1)
            if len(_seen2) == 1:
                s = dict(s)
                s["by_dmm_id"] = {}
            return s

        globals()["load_store"] = _racing2
        try:
            run(True, fetcher=_f, machines=_ms_all)
        finally:
            globals()["load_store"] = _real2
        _s8 = json.loads(io.open(STORE, encoding="utf-8").read())
        t("★★決めるのは鍵の中★★"
          "（取っている最中に2AIが決めた対応が、順位表にも効く）",
          (_s8.get("ranked") or [""])[0] == "s20")

        # ★★3周目4：前に試した名前へ戻っただけなら聞き直さない★★
        io.open(STORE, "w", encoding="utf-8", newline="\n").write(
            json.dumps({"schema_version": SCHEMA, "by_dmm_id": {},
                        "tries": {"1020": GIVE_UP_TRIES}},
                       ensure_ascii=False) + "\n")
        run(True, fetcher=_f, machines=_ms)          # 名前A で打ち切る
        _fB = (lambda _u: _page20(                     # noqa: E731
            lambda i: "機種20 改" if i == TOP_N else f"機種{i}"))
        rB1 = run(True, fetcher=_fB, machines=_ms)   # 名前B → 聞き直す
        t("★★名前が変わったら聞き直す★★", len(rB1["revived"]) == 1)
        t("　★聞き直すときも、前に試した名前は消さない★"
          "（消すと A→B→A の往復に気づけない）",
          norm("機種20") in
          ((json.loads(io.open(STORE, encoding="utf-8").read())
            .get("tried_names") or {}).get("1020") or []))
        io.open(STORE, "w", encoding="utf-8", newline="\n").write(
            json.dumps({"schema_version": SCHEMA, "by_dmm_id": {},
                        "tries": {"1020": GIVE_UP_TRIES},
                        "tried_names": {"1020": [norm("機種20"),
                                                 norm("機種20 改")]}},
                       ensure_ascii=False) + "\n")
        rB2 = run(True, fetcher=_f, machines=_ms)    # 名前A へ戻った
        t("★★前に試した名前へ戻っただけなら、聞き直さない★★"
          "（A→B→A の往復で無期限に聞き続けない）",
          not rB2["revived"] and len(rB2["give_up"]) == 1)
        t("　★試した名前は消さない★",
          len((json.loads(io.open(STORE, encoding="utf-8").read())
               .get("tried_names") or {}).get("1020") or []) == 2)

        # ★★2周目3：前の週の一覧を使っていることを隠さない★★
        io.open(STORE, "w", encoding="utf-8", newline="\n").write(
            json.dumps({"schema_version": SCHEMA, "by_dmm_id": {},
                        "ranked": [f"s{i}" for i in range(1, TOP_N + 1)],
                        "checked_at": "2026-08-24"},
                       ensure_ascii=False) + "\n")
        t("★★前の週の一覧なら、そう言う★★",
          plan_today("2026-08-31", 6, _ms_all)["stale"] == "2026-08-24")
        t("　★今週取り直していれば、言わない★",
          plan_today("2026-08-24", 6, _ms_all)["stale"] == "")

        # ★★指摘3：決められない機種が残る間は必ず取り直す★★
        t("★★決められない機種が残っていたら、翌日も取りに行く★★",
          should_run("2026-09-02", "2026-08-29", 1)[0] is True)
        t("　★残っていなければ、いままでどおり7日／月曜まで待つ★",
          should_run("2026-09-02", "2026-08-29", 0)[0] is False)
        t("　★同じ日でも、残っていれば取り直す★",
          should_run("2026-08-29", "2026-08-29", 2)[0] is True)
        t("　★次は今日もう取らない★",
          should_run(_dt.date.fromisoformat(r4["checked_at"]),
                     r4["checked_at"])[0] is False)
    finally:
        STORE = _keep
        shutil.rmtree(_tmpdir, ignore_errors=True)

    # ★★1週間の割り振り★★（2026-08-29・運営者の表のとおりになるか）
    _keep2 = STORE
    _tmp2 = tempfile.mkdtemp(prefix="popular_week_")
    try:
        STORE = os.path.join(_tmp2, "popular_machines.json")
        _pop = [f"p{i}" for i in range(1, 21)]
        _ms2 = [{"slug": s, "name": s} for s in _pop]
        io.open(STORE, "w", encoding="utf-8", newline="\n").write(
            json.dumps({"schema_version": SCHEMA, "by_dmm_id": {},
                        "ranked": _pop}, ensure_ascii=False) + "\n")
        t("★★月曜は今週の月曜を指す★★",
          week_start("2026-08-31") == "2026-08-31"
          and week_start("2026-09-06") == "2026-08-31")

        # 運営者の表を1週間分たどる
        _days = ["2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03",
                 "2026-09-04", "2026-09-05", "2026-09-06"]
        _got = []
        for _d in _days:
            _p = plan_today(_d, 6, _ms2)
            _got.append((len(_p["popular"]), _p["other"]))
            for _s in _p["popular"]:
                mark_done(_s, _d)
        t("★★運営者の表どおりに割り振る★★"
          "（月火水6／木2+4／金土日0+6）",
          _got == [(6, 0), (6, 0), (6, 0), (2, 4),
                   (0, 6), (0, 6), (0, 6)])
        t("　★翌週の月曜はまた人気機種から★",
          len(plan_today("2026-09-07", 6, _ms2)["popular"]) == 6)

        # 途中で1日動かなかった週
        io.open(STORE, "w", encoding="utf-8", newline="\n").write(
            json.dumps({"schema_version": SCHEMA, "by_dmm_id": {},
                        "ranked": _pop}, ensure_ascii=False) + "\n")
        for _d in ["2026-08-31", "2026-09-01"]:
            for _s in plan_today(_d, 6, _ms2)["popular"]:
                mark_done(_s, _d)
        t("★★1日止まっても、次の日が続きから拾う★★",
          plan_today("2026-09-03", 6, _ms2)["popular"]
          == ["p13", "p14", "p15", "p16", "p17", "p18"])

        t("★★人気の一覧が空なら全部その他★★",
          plan_today("2026-08-31", 6, [], {})["other"] == 6)
    finally:
        STORE = _keep2
        shutil.rmtree(_tmp2, ignore_errors=True)

    ng = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="人気機種の順位をDMMから取る")
    ap.add_argument("--record", metavar="機種ID=slug",
                    help="2AIが決めた対応を控える"
                         "（--by と --why-file が要ります）")
    ap.add_argument("--by", default="",
                    help="判断した2つ以上の名前（例 claude,codex）")
    ap.add_argument("--why-file", dest="why_file",
                    help="なぜそう決めたかを書いたファイル（15字以上）")
    ap.add_argument("--tried", metavar="機種ID",
                    help="2AIで話しても決まらなかった回を1つ数える")
    ap.add_argument("--plan", type=int, metavar="件数",
                    help="今日の枠を人気機種とその他へ割り振って表示する")
    ap.add_argument("--done", metavar="slug",
                    help="その人気機種を今週ぶんとして見終わったと控える")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--weekly", action="store_true",
                    help="月曜（または前回から7日以上）のときだけ取る")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.weekly:
        import datetime as _dt
        _st = load_store()
        ok, why = should_run(_dt.date.today(),
                             str(_st.get("checked_at") or ""),
                             int(_st.get("pending_questions") or 0))
        if not ok:
            print(f"今日は取りません（{why}）")
            return 0
        print(f"取ります（{why}）")
    if a.record:
        if "=" not in a.record:
            print("★書き方★ --record 機種ID=slug")
            return 1
        mid, _sep, slug = a.record.partition("=")
        if not a.why_file:
            print("★--why-file が要ります★（なぜそう決めたか）")
            return 1
        # ★自由文はシェルに書かせない★（プロジェクトの決まり）
        #   置き場の検査つきの共通の口を使う（認証情報の巻き込み防止）
        import open_issues as _oi
        why = _oi._read_text_arg("", a.why_file, "why")
        try:
            record_decision(mid, slug, a.by, why)
        except PopularError as e:
            print(f"★{e}★")
            return 1
        print(f"控えました: {mid} → {slug}")
        return 0
    if a.tried:
        try:
            n = note_try(a.tried)
        except PopularError as e:
            print(f"★{e}★")
            return 1
        left = GIVE_UP_TRIES - n
        print(f"{a.tried}: {n}回目"
              + (f"（あと{left}回で運営者へ報告します）" if left > 0
                 else "（★次から運営者へ報告して先へ進みます★）"))
        return 0
    if a.plan is not None:
        import datetime as _dt
        p = plan_today(_dt.date.today(), a.plan)
        print(f"今週（{p['week']}〜）の人気機種: "
              f"{p['done_this_week']}/{p['ranked']} 件は見終わりました")
        if p.get("stale"):
            # ★★前の週の一覧を使っていることを隠さない★★
            #   （2026-08-29・Codexの2周目の指摘3）
            #   ★一覧が完全でも「いまの順番」とは限らない★＝
            #   今週の新しい人気機種が抜け、圏外になった機種が残っている。
            print(f"★これは {p['stale']} に取った一覧です★"
                  "（今週まだ取り直せていません＝"
                  "決められない機種を2AIで片づけてください）")
        print(f"今日の人気機種 {len(p['popular'])} 件: "
              + ("、".join(p["popular"]) or "なし"))
        print(f"今日のその他 {p['other']} 件")
        return 0
    if a.done:
        import datetime as _dt
        rot = mark_done(a.done, _dt.date.today())
        print(f"今週ぶん {len(rot['done'])} 件目として控えました: {a.done}")
        return 0
    try:
        got = run(apply_it=a.apply)
    except PopularError as e:
        print(f"★{e}★（並べ替えません）")
        return 1
    print(f"人気機種: {len(got['ranked'])} / {TOP_N} 件が"
          "うちどころにあります")
    for n, s in enumerate(got["ranked"], 1):
        print(f"  {n:>2}  {s}")
    if got["learned"]:
        print(f"新しく結び付けました: {got['learned']}")
    for q in got["ask_2ai"]:
        print("  ★2AIに聞くこと: " + q["text"][:170])
    if not a.apply:
        print("★下見です★（--apply で控えに書きます）")
    if got.get("give_up"):
        # ★★打ち切った機種は毎回かならず出す★★
        #   （2026-08-29・Codexの2周目の指摘2）
        #   ★報告を1回だけにすると、取りこぼした後は
        #     「正常終了しながら黙って除外し続ける」★
        print(f"★いま人気枠から外している機種が {len(got['give_up'])} 件"
              "あります★（3回話しても決まらなかったもの）")
        for q in got["give_up"]:
            print(f"  ・{q.get('dmm_id')} {q.get('name')}"
                  "（--record で控えれば、また人気枠に戻ります）")
    if got.get("report"):
        # ★★3回話しても決まらなかった★★（2026-08-29・運営者の指示）
        print(f"★{len(got['report'])} 件は3回話しても決まりませんでした★"
              "（運営者へ報告して、この週は人気枠から外します）")
        for q in got["report"]:
            print("  ★報告: " + q["text"][:170])
        return 4
    if got["ask_2ai"]:
        # ★★終了コードで知らせる★★（2026-08-29・Codexの指摘3）
        #   ★表示だけでは見落とされる★＝「2AIへ回す」を機械の合図にする。
        print(f"★決められない機種が {len(got['ask_2ai'])} 件あります★"
              "（2AIで判断して控えに記録してください）"
              "／★確認日は進めていません＝翌日も取り直します★")
        if got.get("kept_previous"):
            print("★順位表は前の週のものを残しました★"
                  "（欠けた一覧を正式なものにしないため）")
        else:
            print("★順位表はまだありません★"
                  "（決まるまで人気枠は使わず、全部その他へ回します）")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

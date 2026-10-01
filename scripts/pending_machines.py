# -*- coding: utf-8 -*-
"""pending_machines.py — 見つけたが、まだ記事にできていない新台の控え。

★何のためにあるか★
  新台を見つけても、材料がそろわず記事にできない日がある。そこで
  「見つけた事実」だけを覚えておき、翌日以降にやり直す。
  ★数値や記事は持たない★（持つと古い値が生き残る）。

★v2：主キーをURLから採番したIDへ変えた★（2026-08-16・台帳#376／Codex依頼212）
  規約でP-WORLDからDMMへ移したとき、**同じ機種のURLが変わりました**。
  URLを主キーにしていると、URLが変わった瞬間に
  「別の機種」として二重に入るか、控えが迷子になります。

  そこで**一度だけ採番したID（queue_id）を主キー**にし、URLは中身の
  ひとつに格下げしました。移行前のURLは `legacy_url` に履歴として残しますが、
  ★取りに行く先には使いません★（そもそも blocked_hosts が通信を止めます）。

★状態★
  READY             … 機種ページが分かっている（DMMの機種ID付き）
  AWAITING_DMM_ID   … 機種は分かっているが、DMMのカレンダーにまだ載っていない
                      （P-WORLD時代に見つけたもの。★消さずに毎晩見に行く★）

使い方:
    python scripts/pending_machines.py list
    python scripts/pending_machines.py --selftest
    python scripts/pending_machines.py migrate --apply   # v1 → v2
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import safe_json as _sj                # noqa: E402

STORE = os.path.join(os.path.expanduser("~"), "Documents", "uchidokoro",
                     "add_machine_pending.json")
SCHEMA = "add-machine-pending/v2"
SCHEMA_V1 = "add-machine-pending/v1"

# ★これ以上待っても載らないなら、人に見てもらう★
GIVE_UP_DAYS = 60

# 状態（★増やすときはここだけを直す★）
READY = "READY"
AWAITING_DMM_ID = "AWAITING_DMM_ID"
STATES = (READY, AWAITING_DMM_ID)


class PendingError(RuntimeError):
    pass


def _today() -> str:
    return date.today().isoformat()


def _empty() -> dict:
    return {"schema": SCHEMA, "next_id": 1, "items": {}}


def load() -> dict:
    """待ち行列を読む。★壊れていたら止まる（黙って空にしない）★"""
    if not os.path.exists(STORE):
        return _empty()
    got = _sj.read_json(STORE, expect=dict)
    if got.get("schema") == SCHEMA_V1:
        raise PendingError(
            "待ち行列がまだ古い形（v1）です／"
            "★python scripts/pending_machines.py migrate --apply で移してください★")
    if got.get("schema") != SCHEMA:
        raise PendingError(f"待ち行列の形が違います: {got.get('schema')!r}")
    if not isinstance(got.get("items"), dict):
        raise PendingError("待ち行列の中身が壊れています")
    if not isinstance(got.get("next_id"), int) or got["next_id"] < 1:
        raise PendingError("待ち行列の採番が壊れています")
    for qid, it in got["items"].items():
        if not isinstance(it, dict):
            raise PendingError(f"待ち行列の項目が壊れています: {qid}")
        if it.get("queue_id") != qid:
            raise PendingError(f"待ち行列の鍵と中身が食い違います: {qid}")
        if it.get("state") not in STATES:
            raise PendingError(f"知らない状態です（{qid}）: {it.get('state')!r}")
    return got


def save(data: dict) -> None:
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".new"
    with open(tmp, "w", encoding="utf-8", newline=chr(10)) as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write(chr(10))
    os.replace(tmp, STORE)              # ★書き換え中に壊れないように★


def _next_qid(data: dict) -> str:
    """★一度だけ採番する★（使い回さない・空きを詰めない）"""
    n = int(data.get("next_id", 1))
    while ("q_%04d" % n) in data["items"]:
        n += 1                          # 念のため（採番が巻き戻っていた場合）
    data["next_id"] = n + 1
    return "q_%04d" % n


def find_by_machine_id(data: dict, source_machine_id: str) -> dict | None:
    """DMMの機種IDで探す（★同じ機種を二重に持たないため★）。"""
    mid = str(source_machine_id or "").strip()
    if not mid:
        return None
    for it in data["items"].values():
        if str(it.get("source_machine_id") or "") == mid:
            return it
    return None


def find_by_url(data: dict, url: str) -> dict | None:
    """★機種ページのURLで探す★（台帳#485）

    ★鍵ではなく identity_url を見る★＝待ち行列の鍵は採番したID（q_0001…）。
    ★空のURLは絶対に当てない★＝DMMのカレンダー待ちの機種は identity_url が
    空なので、空どうしが一致すると**別の機種の欄へ保存できてしまう**。
    ★末尾のスラッシュは落として比べる★（既存機種を引く枝と同じ物差し）。
    ★移行前のURL（legacy_url）は見ない★＝取りに行かない決まりと揃える。
    """
    want = str(url or "").rstrip("/")
    # ★★空のURLは絶対に当てない★★（ここ1か所で見る）
    #   DMMのカレンダー待ちの機種は identity_url が空なので、
    #   空どうしが一致すると**別の機種の欄へ保存できてしまう**。
    #   ★同じことを内側でも見ない★＝内側は外側に助けられて一度も効かず、
    #   壊しても試験が緑のままになる（2026-08-27・壊し方の道具が指摘）。
    if not want:
        return None
    for it in (data or {}).get("items", {}).values():
        if str((it or {}).get("identity_url") or "").rstrip("/") == want:
            return it
    return None


def find_by_core(data: dict, name: str):
    """★機種名の芯が完全一致するもの★（DMMにまだ載っていない控えとの結び付け用）

    ★前方一致や似ている判定はしない★（当サイトの鉄則）。
    完全一致で決まらないものは、二重に持ったまま人・2AIに見てもらう。
    取りこぼすより二重のほうが安全（取りこぼすと機種が消える）。
    """
    import claim_identity as _ci
    core = _ci.normalize_core(str(name or ""))
    if len(core) < 2:
        return []
    return [it for it in data["items"].values()
            if _ci.normalize_core(str(it.get("name") or "")) == core]


def add(data: dict, name: str, url: str, maker: str, release: str,
        reason: str = "", extra: dict | None = None,
        source_machine_id: str = "", identity_source: str = "",
        state: str = READY) -> dict:
    """待ち行列に入れる（既にあれば試した回数と理由を更新する）。

    ★同じ機種かどうかは「DMMの機種ID」で見る★（2026-08-16）
      URLは変わりうるが、機種IDは機種ごとに1つ。
      機種IDが分からないとき（移行前の控え）はURLで探す。

    ★extra＝あとで引き直せない手掛かり★（2026-08-13・台帳#335）
      カレンダーから消えると待ち行列だけでは二度と分からなくなるもの。
      ★空では上書きしない／違う値でも上書きしない★（食い違いは残す）。
    """
    # ★名前が無くても覚える★（2026-07-31・Codex17回目）
    #   ページを読めなかったURLは名前が取れない。そこで拒否すると、
    #   **そのURLは既知になったまま二度と出てこない**＝機種が消える。
    if state not in STATES:
        raise PendingError(f"知らない状態です: {state!r}")
    if state == READY and not url:
        raise PendingError("機種ページのURLは必ず要ります")
    if state == AWAITING_DMM_ID and not str(name or "").strip():
        raise PendingError("機種ページが無いときは、せめて名前が要ります")
    it = find_by_machine_id(data, source_machine_id)
    if it is None and url:
        it = next((x for x in data["items"].values()
                   if str(x.get("identity_url") or "") == url), None)
    if it:
        it["tries"] = int(it.get("tries", 0)) + 1
        it["last_try"] = _today()
        it["last_reason"] = reason[:300]
        # ★名前や登場年月が変わることがある（公式の書き換え）★
        # ★★空の値では上書きしない★★（2026-10-02・Codex review201）＝
        #   機種ページを読めなかった晩は名前以外が空で来るので、
        #   覚えていた導入日とメーカーが消え、導入3日前の🔴が出なくなっていた。
        if str(name or "").strip():
            it["name"] = name
        if str(maker or "").strip():
            it["maker"] = maker
        if str(release or "").strip():
            it["release"] = release
            # ★導入日が来た＝もう未定ではない★（2026-10-02・Codex review202）
            #   巡回で日付が分かっても印が残ると、記事づくりまで進めなかった晩、
            #   番兵がその機種を「未定」として黙って🔴を出さなかった。
            it.pop("release_tbd", None)
            # ★★日の欄も新しい導入日にそろえる★★（2026-10-02・Codex review203）
            #   release_of() は日の欄を優先するので、古い日が残ると
            #   早まった・延びた導入日が番兵に届かない。
            _r = str(release).strip()
            if len(_r) >= 10:
                it["release_day"] = _r[:10]
            elif not str(it.get("release_day") or "").startswith(_r[:7]):
                it.pop("release_day", None)
        if url:
            it["identity_url"] = url
        if source_machine_id:
            it["source_machine_id"] = str(source_machine_id)
        if identity_source:
            it["identity_source"] = identity_source
        # ★状態は勝手に進めない★（2026-08-16・依頼214の指摘3）
        #   以前はここで「機種ページのURLが来た＝待ちが解けた」として
        #   READY へ戻していた。しかし巡回は**確かめられなかった機種も**
        #   控えへ入れ直すので、確かめていないものが READY になり、
        #   その晩の記事づくりの列に入っていた。
        #   ★待ちを解くのは、機種ページを確かめられた時だけ★
        #   （dmm_discover.rebind_waiting が check_one の合格を見て決める）
        for k, v in (extra or {}).items():
            if not v:
                continue                  # ★空では上書きしない★
            old = it.get(k)
            if old and old != v:
                # ★一度覚えたものは変えない★（2026-08-13・依頼170のP1）
                #   ここを上書きにすると、翌日のカレンダーが違う表示名を
                #   返しただけで**公開直前の照合がその値に合わせて緩む**。
                it.setdefault(k + "_conflict", []).append(v)
                it[k + "_conflict"] = sorted(set(it[k + "_conflict"]))[:5]
                continue
            it[k] = v
        return it
    qid = _next_qid(data)
    data["items"][qid] = {
        "queue_id": qid, "state": state,
        "name": name, "identity_url": url, "maker": maker, "release": release,
        "identity_source": identity_source or ("dmm" if url else ""),
        "source_machine_id": str(source_machine_id or ""),
        "first_seen": _today(), "last_try": _today(), "tries": 1,
        "last_reason": reason[:300],
        **{k: v for k, v in (extra or {}).items() if v}}
    return data["items"][qid]


def fetch_url(item: dict) -> str:
    """★取りに行ってよいURL★（移行前のURLは絶対に返さない）

    `legacy_url` は履歴として残すが、再取得には使わない。
    （そもそも blocked_hosts.py が通信を止めるが、
      **そこへ持って行かない**のがここの役目）
    """
    return str((item or {}).get("identity_url") or "")


def done(data: dict, queue_id: str) -> bool:
    """記事にできたので外す。"""
    return data["items"].pop(str(queue_id or ""), None) is not None


def waited_days(item: dict, today: str = "") -> int:
    """見つけてから何日たったか。"""
    try:
        a = datetime.fromisoformat(item.get("first_seen") or "")
        b = datetime.fromisoformat(today or _today())
    except ValueError:
        return 0
    return (b - a).days


def release_of(item: dict) -> str:
    """★待ち行列の導入日★（2026-10-02）

    日まで分かっていれば release_day、無ければ年月の release。
    （release は待ち行列の決まりで年月までにしてあるので、
      日は別の欄に持つ＝今までの読み手を変えない）
    """
    return str(item.get("release_day") or item.get("release") or "").strip()


def release_last_day(item: dict) -> str:
    """導入日のいちばん遅い可能性（日ならその日・月ならその月末・分からなければ空）。

    ★DMMが「未定」と書いている印があれば空★（Codex review200）＝
    未定になる前の古い日付が欄に残っていても、それで数えない。
    """
    if item.get("release_tbd"):
        return ""
    r = release_of(item)
    try:
        if len(r) == 10:
            return date.fromisoformat(r).isoformat()
        if len(r) == 7:
            return month_end(r)
    except (ValueError, TypeError):
        return ""
    return ""


def would_give_up(it: dict, today: str = "") -> bool:
    """★確かめた日を除いて、打ち切りの条件を満たすか★（取り直す候補を選ぶため）"""
    today = today or _today()
    if it.get("state") != READY:
        return False
    if waited_days(it, today) < GIVE_UP_DAYS:
        return False
    if int(it.get("runs", 0)) < 1:
        return False
    last = release_last_day(it)
    if not last:
        return False
    try:
        return (date.fromisoformat(today[:10])
                - date.fromisoformat(last)).days >= GIVE_UP_DAYS
    except ValueError:
        return False


def give_up(data: dict, today: str = "") -> list:
    """★導入日から GIVE_UP_DAYS 日たっても記事にできないものを外して返す★

    ★★数える起点は導入日★★（2026-10-02・運営者の判断）
      直す前は「見つけた日」から数えていたので、8月に見つけた11月導入の
      機種が導入前に打ち切られるところだった（SAO・聖闘士星矢）。
      導入前は出典に情報が無いのが当たり前なので、日数だけで切らない。
      ★導入日が分からない（未定・読めない）ものは打ち切らない★。
      外したものは呼ぶ側がその晩のログに残す（台帳は2026-10-01に廃止）。

    ★一度も記事づくりを試していないものは打ち切らない★（Codex21回目）
      待ち行列の先頭が詰まっていると後ろは一度も試されない。
      試してもいないものを日数だけで捨てると、機種が黙って消える。
    """
    today = today or _today()
    out = []
    for qid, it in list(data["items"].items()):
        if it.get("state") != READY:
            # ★DMMに載るのを待っている控えは打ち切らない★
            #   （2026-08-16・依頼213の指摘4）
            #   8月に見つけた11月導入の機種は、載るのを待っているだけで
            #   60日たつ。ここで区別しないと、**待たせるために作った控えを
            #   待ち終わる前に捨てる**（聖闘士星矢がまさにその形）。
            #   期限を過ぎた分は calendar_missing_due() が台帳へ知らせ、
            #   ★控えは残したまま★載るのを待ち続ける。
            continue
        if waited_days(it, today) < GIVE_UP_DAYS:
            continue
        if int(it.get("runs", 0)) < 1:
            continue                      # ★まだ一度も試していない★
        # ★★導入日をその日にDMMで確かめたものだけ打ち切る★★
        #   （2026-10-02・Codex review201/202）＝古い日付のまま外すと、
        #   確かめたあとに延期・未定になった機種が消える。
        #   呼ぶ側（add_machine_run）は、would_give_up の候補を先に取り直してから呼ぶ。
        if str(it.get("release_checked") or "")[:10] != today[:10]:
            continue
        last = release_last_day(it)
        if not last:
            continue                      # ★導入日が分からないものは打ち切らない★
        try:
            gone = (date.fromisoformat(today[:10])
                    - date.fromisoformat(last)).days
        except ValueError:
            continue
        if gone < GIVE_UP_DAYS:
            continue                      # ★導入日から GIVE_UP_DAYS 日たっていない★
        out.append(data["items"].pop(qid))
    return out


def month_end(ym: str) -> str:
    """その月の最終日（★導入日が月までしか分からないときの期限★）。"""
    y, m = int(str(ym)[:4]), int(str(ym)[5:7])
    y2, m2 = (y + 1, 1) if m == 12 else (y, m + 1)
    return (date(y2, m2, 1) - __import__("datetime").timedelta(days=1)) \
        .isoformat()


def calendar_missing_due(data: dict, today: str = "") -> list:
    """★DMMに載らないまま導入日を過ぎた控え★（台帳へ一度だけ知らせる）

    （2026-08-16・依頼213／Codexの助言）
    ★控えは消さない★＝知らせたあとも載るのを待ち続ける
    （あとから載れば自動の経路へ戻せる）。
    日まで分かっていればその日、月までなら月末を期限とする。
    """
    today = today or _today()
    out = []
    for it in data["items"].values():
        if it.get("state") != AWAITING_DMM_ID:
            continue
        if it.get("calendar_missing_reported_at"):
            continue                      # ★知らせるのは一度だけ★
        rel = str(it.get("release") or "")
        if len(rel) == 7:
            limit = month_end(rel)
        elif len(rel) == 10:
            limit = rel
        else:
            continue                      # 期限を決められない＝知らせない
        if today >= limit:
            out.append(it)
    return out


def mark_tried(data: dict, queue_id: str, blocker: str = "") -> None:
    """★実際に記事づくりを試したことを残す★（2026-07-31・Codex21回目）

    これが無いと、詰まっている先頭の数件だけを毎晩見続けて、
    **6件目以降は一度も試されないまま60日で打ち切られる**。

    ★★なぜ止まったかも残す★★（2026-08-22・Codexの設計レビュー）
      ★これが無くて起きたこと★＝新台タスクは5日連続で公開0件だったのに、
      毎日エラーなく完走していたので誰も気づかなかった。
      「何回試したか」だけでは、**同じ理由で止まり続けているのか**
      **毎回違う理由なのか**が分からない。

      ★同じ理由で2回続いたら知らせる★のが主監視（add_machine_health）。
      全体の「公開0件」だけを見ていると、
      ★他の機種が毎日出ている裏で、1機種だけ永久に止まっていても
      見つからない★（Codexの指摘）。

      blocker は短い符丁（例: TAIL_CONFLICT / NOT_ENOUGH_DIRECTORIES /
      MAKER_UNKNOWN / NO_MATERIAL）。★自由文を入れない★＝
      文言を変えるたびに見張りが壊れるため。
    """
    it = data["items"].get(str(queue_id or ""))
    if not it:
        return
    it["last_try"] = _today()
    it["runs"] = int(it.get("runs", 0)) + 1
    # ★★ここでは連続の理由を触らない★★（2026-08-22・Codexの指摘で直した）
    #   ★直す前に何が起きていたか★＝
    #     本番は毎晩 mark_tried（試す前）→ mark_blocked（結果が出てから）の順。
    #     mark_tried が理由なしで呼ばれると連続を0へ戻していたので、
    #     ★同じ理由で何晩止まっても streak は毎回1に戻り、2に届かなかった★
    #     ＝「同じ理由で2回続いたら知らせる」が**一度も発火しない**。
    #   ★私の試験が見逃した理由★＝streak=2 の偽物を直接作って
    #     通知側だけを調べていた。**本番の順で2晩通していなかった**。
    #   ＝状態を変えるのは mark_blocked（＋1）と mark_unblocked（0）だけにする。
    if blocker:
        mark_blocked(data, queue_id, blocker)


def mark_blocked(data: dict, queue_id: str, blocker: str) -> None:
    """★試したあとで、止まった理由だけを足す★（2026-08-22）

    `mark_tried` は**試す前**に呼ぶ（途中で落ちても記録が残るように）ので、
    そこでは理由がまだ分からない。結果が出てから、ここで足す。

    ★同じ理由が続いた回数を数える★＝
      `add_machine_health` が「同じ理由で2回止まったら知らせる」に使う。
      全体の「公開0件」だけを見ていると、
      ★他の機種が毎日出ている裏で1機種だけ永久に止まっていても
      見つからない★（2026-08-22・Codexの指摘）。
    """
    it = data["items"].get(str(queue_id or ""))
    if not it:
        return
    b = str(blocker or "").strip()[:40]
    if not b:
        return
    if it.get("last_blocker") == b:
        it["blocker_streak"] = int(it.get("blocker_streak", 0)) + 1
    else:
        it["last_blocker"] = b
        it["blocker_streak"] = 1


def mark_unblocked(data: dict, queue_id: str) -> None:
    """★止まらずに進んだので、連続を切る★（2026-08-22）"""
    it = data["items"].get(str(queue_id or ""))
    if not it:
        return
    it["last_blocker"] = ""
    it["blocker_streak"] = 0


def due(data: dict, all_states: bool = False) -> list:
    """今日やり直すもの。★古いものから★（先に見つけたものを先に）

    ★機種ページが分かっているものだけ返す★（2026-08-16・依頼213の指摘4）
      DMMに載るのを待っている控えは、記事づくりの列に入れない。
      入れると毎晩「試した」ことになり、**待っているだけなのに
      60日で打ち切られる**（聖闘士星矢がその形）。
      待っている分を進めるのは巡回（dmm_discover）だけ。
      一覧を見たいときは all_states=True。
    """
    xs = [x for x in data["items"].values()
          if all_states or x.get("state") == READY]
    return sorted(xs, key=lambda x: (x.get("first_seen") or "",
                                     x.get("queue_id") or ""))


# ----------------------------------------------------------------- migrate

def migrate_v1(old: dict) -> dict:
    """★v1（URLが鍵）をv2（採番したIDが鍵）へ移す★

    移行前のURLは `legacy_url` に残す（履歴・取りに行く先には使わない）。
    P-WORLDの控えは機種ページを取りに行けないので AWAITING_DMM_ID。
    """
    import blocked_hosts as _bh
    out = _empty()
    for url, it in sorted((old.get("items") or {}).items()):
        it = dict(it)
        it.pop("url", None)
        blocked = _bh.is_blocked(url)
        qid = _next_qid(out)
        row = {"queue_id": qid,
               "state": AWAITING_DMM_ID if blocked else READY,
               "name": it.pop("name", ""), "maker": it.pop("maker", ""),
               "release": it.pop("release", ""),
               "identity_url": "" if blocked else url,
               "identity_source": "" if blocked else "dmm",
               "source_machine_id": "",
               "first_seen": it.pop("first_seen", _today()),
               "last_try": it.pop("last_try", _today()),
               "tries": int(it.pop("tries", 1)),
               "last_reason": it.pop("last_reason", "")}
        if blocked:
            # ★履歴として残すが、取りに行く先には使わない★
            row["legacy_url"] = url
            row["legacy_source"] = "p-world"
        row.update(it)                    # 覚えた手掛かり（pworld_maker 等）
        out["items"][qid] = row
    return out


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []
    nl = chr(10)

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    DMM = "https://p-town.dmm.com/machines/"
    d = _empty()
    a1 = add(d, "テスト機", DMM + "5049", "m", "2026-09", "名鑑にまだ無い",
             source_machine_id="5049")
    t("★見つけた機種を覚える★", len(d["items"]) == 1)
    t("　主キーは採番したID（URLではない）",
      a1["queue_id"] == "q_0001" and d["items"]["q_0001"] is a1)
    t("　覚えるのは事実だけ（数値や記事は持たない）",
      set(a1) == {"queue_id", "state", "name", "identity_url", "maker",
                  "release", "identity_source", "source_machine_id",
                  "first_seen", "last_try", "tries", "last_reason"})
    add(d, "テスト機", DMM + "5049", "m", "2026-09", "まだ無い",
        source_machine_id="5049")
    t("★★同じ機種を二重に持たない（試した回数が増える）★★",
      len(d["items"]) == 1 and d["items"]["q_0001"]["tries"] == 2)
    # ★★URLが変わっても同じ機種と分かる★★（v2の要）
    add(d, "テスト機", DMM + "5049?ref=x", "m", "2026-09", "URLが変わった",
        source_machine_id="5049")
    t("★★URLが変わっても同じ機種として扱う★★（機種IDで見るから）",
      len(d["items"]) == 1 and d["items"]["q_0001"]["tries"] == 3)
    add(d, "テスト機（改名）", DMM + "5049", "m", "2026-10",
        source_machine_id="5049")
    t("　公式が名前や登場月を書き換えたら追従する",
      d["items"]["q_0001"]["name"] == "テスト機（改名）"
      and d["items"]["q_0001"]["release"] == "2026-10")

    # ── 2026-08-27・台帳#485 URLで探す ──────────────────────────
    dU = _empty()
    add(dU, "URLのある機種", DMM + "5086", "m", "2026-10",
        source_machine_id="5086")
    add(dU, "DMM待ちの機種", "", "m", "2026-11", state=AWAITING_DMM_ID)
    t("★★機種ページのURLで引ける（鍵は採番IDなので鍵では引けない）★★"
      "／★これが無いと2AIが決めた値を新台へ記録できない★",
      (find_by_url(dU, DMM + "5086") or {}).get("name") == "URLのある機種")
    t("　末尾のスラッシュは無視する",
      (find_by_url(dU, DMM + "5086/") or {}).get("name") == "URLのある機種")
    t("★★空のURLは絶対に当てない（DMM待ちの機種へ誤って結び付く）★★",
      find_by_url(dU, "") is None)
    t("　知らないURLは何も返さない", find_by_url(dU, DMM + "9999") is None)
    t("　採番IDそのものでは引けない（鍵はURLではない）",
      find_by_url(dU, "q_0001") is None)

    t("★記事にできたら外す★", done(d, "q_0001") and not d["items"])
    t("　無いものを外そうとしても壊れない", done(d, "q_9999") is False)
    t("★★採番は使い回さない★★（消したIDを次の機種に当てない）",
      add(d, "次の機種", DMM + "5050", "m", "2026-09",
          source_machine_id="5050")["queue_id"] == "q_0002")

    d2 = _empty()
    add(d2, "古い機種", DMM + "1", "m", "2026-01", source_machine_id="1")
    d2["items"]["q_0001"]["first_seen"] = "2026-01-01"
    mark_tried(d2, "q_0001")                       # ★一度は試している★
    d2["items"]["q_0001"]["release_checked"] = "2026-07-31"
    add(d2, "新しい機種", DMM + "2", "m", "2026-09", source_machine_id="2")
    t("★★待ちすぎたものだけ取り出す★★（黙って消さない・その晩のログに残すため）",
      [x["name"] for x in give_up(d2, "2026-07-31")] == ["古い機種"]
      and len(d2["items"]) == 1)
    t("　まだ待てるものは残る", "q_0002" in d2["items"])

    d3 = _empty()
    add(d3, "あと", DMM + "8", "m", "2026-09", source_machine_id="8")
    d3["items"]["q_0001"]["first_seen"] = "2026-07-30"
    add(d3, "さき", DMM + "9", "m", "2026-09", source_machine_id="9")
    d3["items"]["q_0002"]["first_seen"] = "2026-07-01"
    t("★先に見つけたものから試す★", [x["name"] for x in due(d3)] == ["さき", "あと"])

    d4 = _empty()
    add(d4, "一度も試していない機種", DMM + "7", "m", "2026-01",
        source_machine_id="7")
    d4["items"]["q_0001"]["first_seen"] = "2026-01-01"
    t("★★一度も記事づくりを試していないものは打ち切らない★★"
      "（先頭が詰まると後ろは一度も試されない・Codex21回目）",
      give_up(d4) == [] and "q_0001" in d4["items"])
    mark_tried(d4, "q_0001")
    d4["items"]["q_0001"]["release_checked"] = _today()
    t("　一度でも試したものは、待ちすぎたら取り出す", len(give_up(d4)) == 1)

    # ★★導入前の機種は、見つけてからの日数で打ち切らない★★（2026-10-02）
    #   8/13に見つけた11月導入の機種が、10/12に導入前のまま消えるところだった。
    d5 = _empty()
    add(d5, "11月導入の機種", DMM + "5", "m", "2026-11", source_machine_id="5")
    d5["items"]["q_0001"]["first_seen"] = "2026-08-13"
    mark_tried(d5, "q_0001")
    t("★★導入前の機種は、見つけて60日たっても打ち切らない★★"
      "（数える起点は導入日・2026-10-02）",
      give_up(d5, "2026-10-13") == [] and "q_0001" in d5["items"])
    d5["items"]["q_0001"]["release_checked"] = "2027-01-28"
    _d5a = give_up(d5, "2027-01-28")
    if "q_0001" in d5["items"]:              # ★壊れた版では先に消えている★
        d5["items"]["q_0001"]["release_checked"] = "2027-01-29"
    t("　導入日（月なら月末）から60日たってから打ち切る",
      _d5a == [] and len(give_up(d5, "2027-01-29")) == 1)
    d6 = _empty()
    add(d6, "導入日が未定の機種", DMM + "6", "m", "", source_machine_id="6")
    d6["items"]["q_0001"]["first_seen"] = "2026-01-01"
    mark_tried(d6, "q_0001")
    t("★★導入日が分からないものは打ち切らない★★",
      give_up(d6, "2027-01-01") == [] and "q_0001" in d6["items"])
    d7 = _empty()
    add(d7, "日まで分かった機種", DMM + "7", "m", "2026-10", source_machine_id="7")
    d7["items"]["q_0001"]["first_seen"] = "2026-01-01"
    d7["items"]["q_0001"]["release_day"] = "2026-10-05"
    mark_tried(d7, "q_0001")
    d7["items"]["q_0001"]["release_checked"] = "2026-12-04"
    d9 = _empty()
    add(d9, "確かめていない機種", DMM + "9", "m", "2026-01", source_machine_id="9")
    d9["items"]["q_0001"]["first_seen"] = "2026-01-01"
    mark_tried(d9, "q_0001")
    d9["items"]["q_0001"]["release_checked"] = "2026-10-04"
    t("★★その日に導入日を確かめていなければ打ち切らない★★"
      "（前日の確認では、そのあとの延期・未定を見落とす・Codex review202）",
      give_up(d9, "2026-10-05") == [] and would_give_up(d9["items"]["q_0001"], "2026-10-05")
      and len(give_up(d9, "2026-10-04")) == 1)
    d11 = _empty()
    add(d11, "未定だった機種", DMM + "11", "m", "", source_machine_id="11")
    d11["items"]["q_0001"]["release_tbd"] = True
    add(d11, "未定だった機種", DMM + "11", "m", "2026-11-02", source_machine_id="11")
    t("★★巡回で導入日が来たら、未定の印を外す★★（Codex review202）",
      "release_tbd" not in d11["items"]["q_0001"])
    d12 = _empty()
    add(d12, "導入日が変わる機種", DMM + "12", "m", "2026-11-02", source_machine_id="12")
    d12["items"]["q_0001"]["release_day"] = "2026-10-25"   # ★同じ月の中で変わる★
    add(d12, "導入日が変わる機種", DMM + "12", "m", "2026-10-19", source_machine_id="12")
    _a = release_of(d12["items"]["q_0001"])
    add(d12, "導入日が変わる機種", DMM + "12", "m", "2026-12", source_machine_id="12")
    t("★★導入日が変わったら、日の欄も新しい日にそろえる★★（Codex review203）",
      _a == "2026-10-19")
    t("　月だけの導入日に変わり、古い日と月が違えば古い日を捨てる",
      release_of(d12["items"]["q_0001"]) == "2026-12")
    d10 = _empty()
    add(d10, "覚えている機種", DMM + "10", "maker_x", "2026-10-19",
        source_machine_id="10")
    add(d10, "覚えている機種", DMM + "10", "", "", source_machine_id="10")
    t("★★読めなかった晩の空の値で、覚えていた導入日とメーカーを消さない★★"
      "（Codex review201）",
      d10["items"]["q_0001"]["release"] == "2026-10-19"
      and d10["items"]["q_0001"]["maker"] == "maker_x")
    d8 = _empty()
    add(d8, "未定に戻った機種", DMM + "8", "m", "2026-01", source_machine_id="8")
    d8["items"]["q_0001"]["first_seen"] = "2026-01-01"
    d8["items"]["q_0001"]["release_tbd"] = True
    mark_tried(d8, "q_0001")
    d8["items"]["q_0001"]["release_checked"] = "2027-01-01"   # ★確かめた日の関門は通す★
    t("★★「未定」の印があれば、古い日付が残っていても打ち切らない★★"
      "（Codex review200）",
      give_up(d8, "2027-01-01") == [] and "q_0001" in d8["items"])
    t("★★日まで分かっていれば、月末ではなくその日から数える★★",
      release_of(d7["items"]["q_0001"]) == "2026-10-05"
      and len(give_up(d7, "2026-12-04")) == 1)

    t("★★名前が無くても覚える★★"
      "（読めなかったURLを拒否すると、既知のまま二度と出てこない・Codex17回目）",
      add(_empty(), "", DMM + "3", "m", "2026-09")["identity_url"] == DMM + "3")
    t("　機種ページのURLは必ず要る",
      _raises(lambda: add(_empty(), "X", "", "m", "2026-09")))
    t("★★形が違う待ち行列は読まずに止まる★★（黙って空にしない）",
      _raises(lambda: _check_schema({"schema": "べつのもの", "items": {}})))
    t("　中身が壊れていても止まる",
      _raises(lambda: _check_schema({"schema": SCHEMA, "items": []})))

    # ★あとで引き直せない手掛かりを覚える★（2026-08-13・台帳#335）
    _d = _empty()
    add(_d, "試験機", DMM + "4", "", "", reason="確かめられません",
        source_machine_id="4",
        extra={"pworld_maker": "ミズホ", "pworld_id": "10546"})
    t("★★覚えた手掛かりが残る★★（メーカーを引き直せる）",
      _d["items"]["q_0001"].get("pworld_maker") == "ミズホ"
      and _d["items"]["q_0001"].get("pworld_id") == "10546")
    add(_d, "試験機", DMM + "4", "universal", "2026-11", reason="2回目",
        source_machine_id="4", extra={"pworld_maker": "", "pworld_id": ""})
    t("★★空では上書きしない★★（一度覚えたものを消さない）",
      _d["items"]["q_0001"].get("pworld_maker") == "ミズホ")
    add(_d, "試験機", DMM + "4", "universal", "2026-11", reason="3回目",
        source_machine_id="4", extra={"pworld_maker": "メーシー"})
    t("★★違う値が来ても上書きしない★★（守りが緩まない）",
      _d["items"]["q_0001"].get("pworld_maker") == "ミズホ")
    t("　食い違いは残す（あとで人・2AIが見る）",
      _d["items"]["q_0001"].get("pworld_maker_conflict") == ["メーシー"])

    # ★★DMMにまだ載っていない機種を、消さずに待たせる★★（2026-08-16）
    _w = _empty()
    _it = add(_w, "L聖闘士星矢 黄金十二宮", "", "", "2026-11-02",
              reason="DMMのカレンダーに無い", state=AWAITING_DMM_ID)
    t("★★機種ページが無くても控えを持てる★★"
      "（DMMに載るのが遅い機種を落とさない）",
      _it["state"] == AWAITING_DMM_ID and _it["identity_url"] == "")
    t("　機種ページが無いときは、せめて名前が要る",
      _raises(lambda: add(_empty(), "", "", "", "2026-11",
                          state=AWAITING_DMM_ID)))
    t("★★取りに行ってよいURLに、移行前のURLは出てこない★★",
      fetch_url({"identity_url": "", "legacy_url": "https://www.p-world.co.jp/x"})
      == "")
    # DMMに現れたら、同じ控えを READY にできる
    _same = find_by_core(_w, "Ｌ聖闘士星矢　黄金十二宮")
    t("★機種名の芯が同じ控えを見つけられる★（全半角・記号の差を吸収）",
      len(_same) == 1 and _same[0]["queue_id"] == _it["queue_id"])
    # ★★待っている控えを、待ち終わる前に捨てない★★（2026-08-16・依頼213の指摘4）
    #   8月に見つけた11月導入の機種は、載るのを待っているだけで60日たつ。
    #   区別しないと「待たせるために作った控え」を待ち終わる前に捨てる。
    _w["items"][_it["queue_id"]]["first_seen"] = "2026-01-01"
    mark_tried(_w, _it["queue_id"])
    t("★★DMMに載るのを待っている控えは60日で打ち切らない★★"
      "（待たせるために作った控えを、待ち終わる前に捨てない）",
      give_up(_w, "2026-08-16") == [] and _it["queue_id"] in _w["items"])
    t("★★待っている控えは記事づくりの列に入れない★★"
      "（入れると毎晩『試した』ことになる）",
      due(_w) == [] and len(due(_w, all_states=True)) == 1)
    # ★★導入日を過ぎたら一度だけ知らせる★★（控えは消さない）
    t("　月末が期限になる（導入日が月までしか分からないとき）",
      month_end("2026-11") == "2026-11-30" and month_end("2026-12")
      == "2026-12-31" and month_end("2028-02") == "2028-02-29")
    _w["items"][_it["queue_id"]]["release"] = "2026-11"
    t("★★導入の月が終わるまでは知らせない★★",
      calendar_missing_due(_w, "2026-11-29") == [])
    _due = calendar_missing_due(_w, "2026-11-30")
    t("★★導入の月が終わってもDMMに無ければ知らせる★★（★控えは消さない★）",
      len(_due) == 1 and _it["queue_id"] in _w["items"])
    _w["items"][_it["queue_id"]]["calendar_missing_reported_at"] = "2026-11-30"
    t("　知らせるのは一度だけ（毎晩は鳴らさない）",
      calendar_missing_due(_w, "2026-12-25") == [])
    # ★★機種ページのURLが来ただけでは、待ちを解かない★★
    #   （2026-08-16・依頼214の指摘3）
    #   巡回は**確かめられなかった機種も**控えへ入れ直す。ここで
    #   READY へ戻していたので、確かめていないものが記事づくりの列に入った。
    _w2 = _empty()
    _wt = add(_w2, "待っている機種", "", "", "2026-11", state=AWAITING_DMM_ID)
    # 巡回が機種IDを結んだ状態（rebind_waiting がやること）
    _wt["source_machine_id"] = "5079"
    add(_w2, "待っている機種", DMM + "5079", "", "",
        reason="確かめられませんでした", source_machine_id="5079")
    t("★★機種ページのURLが来ただけでは待ちを解かない★★"
      "（確かめられていないものを記事づくりの列に入れない）",
      len(_w2["items"]) == 1
      and _w2["items"][_wt["queue_id"]]["state"] == AWAITING_DMM_ID
      and due(_w2) == [])
    t("　（対照）新しい機種は今までどおり READY で入る",
      add(_empty(), "新しい機種", DMM + "5080", "m", "2026-11",
          source_machine_id="5080")["state"] == READY)
    t("　似ているだけの名前は結び付けない（★前方一致で寄せない★）",
      find_by_core(_w, "L聖闘士星矢") == [])

    # ★★v1からの移行★★
    _v1 = {"schema": SCHEMA_V1, "items": {
        "https://www.p-world.co.jp/machine/database/10536": {
            "name": "L聖闘士星矢 黄金十二宮", "url": "https://www.p-world.co.jp/"
            "machine/database/10536", "maker": "", "release": "2026-11",
            "first_seen": "2026-08-01", "last_try": "2026-08-15", "tries": 3,
            "last_reason": "材料不足", "pworld_maker": "サミー"},
        "https://p-town.dmm.com/machines/5086": {
            "name": "L転生王女と天才令嬢の魔法革命",
            "url": "https://p-town.dmm.com/machines/5086", "maker": "",
            "release": "2026-10", "first_seen": "2026-08-10",
            "last_try": "2026-08-15", "tries": 1, "last_reason": ""}}}
    _v2 = migrate_v1(_v1)
    _pw = [x for x in _v2["items"].values() if "聖闘士" in x["name"]][0]
    _dm = [x for x in _v2["items"].values() if "転生王女" in x["name"]][0]
    t("★★v1から移せる（鍵が採番したIDになる）★★",
      _v2["schema"] == SCHEMA and len(_v2["items"]) == 2
      and all(k.startswith("q_") for k in _v2["items"]))
    t("★★取りに行けないURLの控えは待ち状態にする★★（消さない）",
      _pw["state"] == AWAITING_DMM_ID and _pw["identity_url"] == ""
      and _pw["legacy_url"].endswith("/10536"))
    t("　移行しても、覚えた手掛かりは失わない",
      _pw.get("pworld_maker") == "サミー" and _pw["tries"] == 3)
    t("　取りに行けるURLの控えはそのまま使える",
      _dm["state"] == READY and _dm["identity_url"].endswith("/5086"))
    t("★★移したものをそのまま読める★★（形の検査を通る）",
      _check_ok(_v2))

    # ★★本番と同じ順で2晩通す★★（2026-08-22・Codexの指摘で作り直した）
    #   ★直す前の試験の誤り★＝blocker_streak=2 の偽物を直接作って
    #   通知側だけを調べていた。だから
    #   ★mark_tried が毎晩0へ戻していた配線漏れを見逃した★
    #   （同じ理由で何晩止まっても streak は1のまま＝通知が一度も出ない）。
    #   ＝★最終状態を直接置かず、状態の移り変わりを実際に通す★
    _d2 = {"schema": SCHEMA, "next_id": 2,
           "items": {"q_1": {"queue_id": "q_1", "name": "試験機",
                             "state": "READY", "tries": 0, "runs": 0}}}
    for _ in range(2):
        mark_tried(_d2, "q_1")                       # ←本番はこの順
        mark_blocked(_d2, "q_1", "TAIL_CONFLICT")
    t("★★本番の順で2晩通すと、同じ理由の連続が2になる★★"
      "（mark_tried が毎晩0へ戻していた）",
      _d2["items"]["q_1"].get("blocker_streak") == 2
      and _d2["items"]["q_1"].get("last_blocker") == "TAIL_CONFLICT")

    mark_tried(_d2, "q_1")
    mark_blocked(_d2, "q_1", "NO_MATERIAL")
    t("　理由が変わったら1から数え直す",
      _d2["items"]["q_1"]["blocker_streak"] == 1
      and _d2["items"]["q_1"]["last_blocker"] == "NO_MATERIAL")

    mark_unblocked(_d2, "q_1")
    t("　公開できたら連続は消える",
      _d2["items"]["q_1"]["blocker_streak"] == 0
      and _d2["items"]["q_1"]["last_blocker"] == "")

    mark_tried(_d2, "q_1")
    t("★理由なしの mark_tried は連続を消さない★",
      "blocker_streak" in _d2["items"]["q_1"])

    # ★★数えるのは、全部の試験が終わったこの場所だけ★★（2026-08-22）
    #   ★実際にやらかしたこと★＝ここより**手前**で ng を計算していたので、
    #   あとから足した試験が❌でも
    #   ★「44/44 合格」と表示され、終了コードも0だった★。
    #   ＝**試験が落ちても緑に見える**＝いちばん危ない壊れ方。
    #   （新しい試験を末尾に足すのは自然な操作なので、また起きる）
    ng = [n for n, ok in results if not ok]
    print(f"{nl}{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def _check_schema(got: dict) -> None:
    if got.get("schema") != SCHEMA:
        raise PendingError("形が違います")
    if not isinstance(got.get("items"), dict):
        raise PendingError("中身が壊れています")


def _check_ok(data: dict) -> bool:
    """load() と同じ検査を、ファイルを介さずに掛ける。"""
    try:
        _check_schema(data)
        for qid, it in data["items"].items():
            if it.get("queue_id") != qid or it.get("state") not in STATES:
                return False
        return isinstance(data.get("next_id"), int) and data["next_id"] >= 1
    except PendingError:
        return False


def _raises(fn) -> bool:
    try:
        fn()
    except Exception:                    # noqa: BLE001
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?",
                    choices=["list", "add", "done", "migrate"])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--name", default="")
    ap.add_argument("--url", default="")
    ap.add_argument("--maker", default="")
    ap.add_argument("--release", default="")
    ap.add_argument("--reason", default="")
    ap.add_argument("--queue-id", default="")
    ap.add_argument("--machine-id", default="")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if args.selftest:
        return selftest()
    if args.cmd == "migrate":
        if not os.path.exists(STORE):
            print("待ち行列がまだありません")
            return 0
        got = _sj.read_json(STORE, expect=dict)
        if got.get("schema") == SCHEMA:
            print("すでに新しい形（v2）です")
            return 0
        if got.get("schema") != SCHEMA_V1:
            print(f"★知らない形です: {got.get('schema')!r}★")
            return 1
        new = migrate_v1(got)
        print(f"{len(got.get('items') or {})}件 → {len(new['items'])}件")
        for it in due(new):
            print("  %-8s %-11s %-34s %s" % (
                it["queue_id"], it["state"], it["name"][:32],
                it.get("identity_url") or ("（旧: " + str(
                    it.get("legacy_url") or "")[-24:] + "）")))
        if not args.apply:
            print(chr(10) + "★下見です（--apply で書き換えます）★")
            return 0
        save(new)
        print(chr(10) + "移しました: " + STORE)
        return 0
    data = load()
    if args.cmd == "add":
        it = add(data, args.name, args.url, args.maker, args.release,
                 args.reason, source_machine_id=args.machine_id)
        save(data)
        print(f"待ち行列に入れました: {it['queue_id']} {args.name}")
    elif args.cmd == "done":
        if done(data, args.queue_id):
            save(data)
            print("外しました")
        else:
            print("待ち行列にありません")
    else:
        items = due(data)
        print(f"待っている新台: {len(items)} 件")
        for it in items:
            print(f"  {it['queue_id']} [{it['state']}] {it['release']} "
                  f"{it['name'][:34]}"
                  f"（{waited_days(it)}日待ち・{it['tries']}回試した）")
            if it.get("last_reason"):
                print(f"    理由: {it.get('last_reason', '')[:90]}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except (PendingError, _sj.SafeJsonError) as e:
        print(f"★待ち行列を扱えません: {e}★")
        raise SystemExit(1)
    except Exception as e:                # noqa: BLE001
        print(f"★想定外の失敗 {type(e).__name__}: {e}★")
        raise SystemExit(1)

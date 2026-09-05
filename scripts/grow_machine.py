#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""grow_machine.py — 新台経路の機種を「育てて」検索に載せる専用の書き込み口。

★何のための道具か（2026-08-05）★
  新台は公開できるようになったが、材料が少ないうちは `AUTO_PENDING`＝
  **検索に載らない**（noindex・sitemap未掲載）。未確認の箱が埋まって
  品質ラインを越えたら `AUTO_INDEXABLE` へ上げる必要があるが、
  その経路がどのタスクにも繋がっていなかった。

★新規公開の経路を流用しない★（Codex100回目の助言）
  `publish_new_machine.py` は「新しく作る」専用で、既にあるファイルには触らない。
  上書きの経路を混ぜると、新規作成の安全策（既存を消さない）が緩む。
  ここは**上書き専用**として分け、条件を別に持つ。

★上げてよい条件（すべてAND・1つでも欠けたら何も書かない）★
  1. いまの区分がちょうど `AUTO_PENDING`
  2. 検索方針が `normal`（緊急スイッチが入っていない）
  3. 台帳に「止めるべき」案件が無い
  4. 公式（または同じ公式の一覧カード）で**本人性を確かめ直せる**
     ＝名前・メーカー・型式・登場年月が登録済みのものと**変わっていない**
  5. 材料は**増えるだけ**（既に確認済みの事実が消えたり変わったら中止）
  6. 作り直した記事・判定書・ページ・sitemap が**同時に**揃う
  7. 途中で1つでも失敗したら**全部元に戻す**

使い方:
    python scripts/grow_machine.py                 # 対象を探すだけ
    python scripts/grow_machine.py --slug xxx      # 下見（書き込まない）
    python scripts/grow_machine.py --slug xxx --apply
    python scripts/grow_machine.py --selftest
"""
from __future__ import annotations

import argparse
import datetime as _dt
import io
import inspect
import json
import re
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_new_article as _ba          # noqa: E402
import confirmed_values as _cv           # noqa: E402
import open_issues as _oi                # noqa: E402
import page_decision as _pdz
import page_probe as _pp             # noqa: E402
import publish_new_machine as _pub       # noqa: E402
import safe_json as _sj                  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
DETAILS = os.path.join(BASE, "assets", "data", "machine-details")
SITEMAP = os.path.join(BASE, "sitemap.xml")


class GrowError(Exception):
    pass


def _log(msg: str) -> None:
    print(msg)


# ★見に行く間隔★（2026-08-13・台帳#346）
#   毎朝すべての新台をフル確認すると1機種8分×機種数かかる。
#   実測: 2026-08-13の更新タスクは45分で、その大半が「変化なし」の確認だった。
#   11月導入機を80日間毎日見ても、解析サイトはまだ「準備中」で空振りが確定する。
#   ★コード側に既定値を持ち、設定ファイルで上書きする★
#     設定が壊れても全機種の確認が止まらないようにするため
#     （壊れていたら設定を丸ごと捨てて既定値で続け、必ず知らせる）。
FREQ_PATH = os.path.join(BASE, "assets", "data", "grow-machine-frequency.json")
FREQ_DEFAULT = {
    "schema_version": 1,
    "default_interval_days": 7,
    "month_interval_days": 3,
    "ranges": [
        {"from_days": -36500, "to_days": -31, "interval_days": 7},
        {"from_days": -30, "to_days": -8, "interval_days": 3},
        {"from_days": -7, "to_days": -1, "interval_days": 1},
        {"from_days": 0, "to_days": 30, "interval_days": 1},
        {"from_days": 31, "to_days": 60, "interval_days": 3},
        {"from_days": 61, "to_days": 36500, "interval_days": 7},
    ],
}
_FREQ_KEYS = {"schema_version", "default_interval_days", "month_interval_days",
              "ranges"}
_RANGE_KEYS = {"from_days", "to_days", "interval_days"}


def freq_problems(conf) -> list:
    """設定の壊れ方を並べる（★1つでもあれば設定ごと捨てる★）。"""
    ng = []
    if not isinstance(conf, dict):
        return ["設定が辞書ではありません"]
    if conf.get("schema_version") != 1:
        ng.append(f"schema_version が 1 ではありません: "
                  f"{conf.get('schema_version')!r}")
    for k in conf:
        if k.startswith("_"):
            continue                       # 覚え書きは自由
        if k not in _FREQ_KEYS:
            ng.append(f"知らない項目です: {k}")
    for k in ("default_interval_days", "month_interval_days"):
        v = conf.get(k)
        if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 7:
            ng.append(f"{k} は1〜7の整数で書きます: {v!r}")
    rs = conf.get("ranges")
    if not isinstance(rs, list) or not rs:
        return ng + ["ranges がありません"]
    seen = []
    for r in rs:
        if not isinstance(r, dict):
            ng.append("ranges の中身が辞書ではありません")
            continue
        for k in r:
            if not k.startswith("_") and k not in _RANGE_KEYS:
                ng.append(f"ranges に知らない項目があります: {k}")
        a, b = r.get("from_days"), r.get("to_days")
        iv = r.get("interval_days")
        if not all(isinstance(x, int) and not isinstance(x, bool)
                   for x in (a, b, iv)):
            ng.append(f"ranges の値が整数ではありません: {r}")
            continue
        if a > b:
            ng.append(f"範囲が逆です: {a}〜{b}")
        if not 1 <= iv <= 7:
            ng.append(f"interval_days は1〜7で書きます: {iv}")
        seen.append((a, b))
    seen.sort()
    for i in range(1, len(seen)):
        if seen[i][0] <= seen[i - 1][1]:
            ng.append(f"範囲が重なっています: {seen[i - 1]} と {seen[i]}")
        elif seen[i][0] != seen[i - 1][1] + 1:
            ng.append(f"範囲に隙間があります: {seen[i - 1]} と {seen[i]}")
    if not any(a <= 0 <= b for a, b in seen):
        ng.append("導入日当日（0日）がどの範囲にも入っていません")
    return ng


def load_freq() -> dict:
    """設定を読む。★壊れていたら丸ごと捨てて既定値で続ける★"""
    try:
        with open(FREQ_PATH, encoding="utf-8") as f:
            got = json.load(f)
    except FileNotFoundError:
        # ★消えたことに気づけるように知らせる★（依頼181のP2）
        print(f"★間隔の設定がありません（既定値で続けます）: {FREQ_PATH}★")
        return FREQ_DEFAULT
    except Exception as e:                # noqa: BLE001
        print(f"★間隔の設定を読めません（既定値で続けます）: {e}★")
        return FREQ_DEFAULT
    bad = freq_problems(got)
    if bad:
        print("★間隔の設定が壊れています（既定値で続けます）★")
        for b in bad:
            print("  -", b)
        return FREQ_DEFAULT
    return got


def parse_release(release: str):
    """導入日を読む → (年月日, 精度) か (None, "") 。

    ★カレンダーに無い日付で落ちない★（2026-08-13・依頼181のP1）
      「2026-02-30」は正規表現には通るが date() が例外を投げる。
      以前はここで**候補列挙そのものが止まって**いた。
      読めない値は「不明」として扱う（新台と推測しない・必ず見に行く）。
    """
    m = re.match(r"^(\d{4})-(\d{2})(?:-(\d{2}))?$", str(release or "").strip())
    if not m:
        return None, ""
    try:
        if m.group(3):
            return _dt.date(int(m.group(1)), int(m.group(2)),
                            int(m.group(3))), "day"
        return _dt.date(int(m.group(1)), int(m.group(2)), 1), "month"
    except ValueError:
        return None, ""


def interval_days(release: str, today, conf=None) -> int:
    """その機種を何日おきに見るか。

    ★月までしか分からない導入日は、日を勝手に補わない★
      月初を仮の日付にすると、最大30日早く「導入後」扱いになる。
      月精度は専用の間隔（既定3日）で見る。
    """
    conf = conf or load_freq()
    day, prec = parse_release(release)
    if not day:
        return int(conf.get("default_interval_days") or 7)
    if prec == "month":
        return int(conf.get("month_interval_days") or 3)
    off = (today - day).days
    for rg in conf.get("ranges") or []:
        if int(rg["from_days"]) <= off <= int(rg["to_days"]):
            return int(rg["interval_days"])
    return int(conf.get("default_interval_days") or 7)


NEW_MACHINE_DAYS = 30             # ★新台と呼ぶのは導入後この日数まで★


def is_new_machine(release: str, today, days: int = NEW_MACHINE_DAYS) -> bool:
    """いま「新台期間」か（導入日当日を0日目として days 日目まで）。

    ★これは「新台タスクが面倒を見る範囲」★（2026-08-13・運営者の方針）
      新台期間のあいだは新台タスクが育て、過ぎたら更新タスクの通常ローテへ回す。
      そうしないと新台が優先の上位に居座り続け、既存機種が永久に回らない。

    ★月までしか分からない導入日は、日を勝手に補わない★
      「2026-09」に月初を当てると最大30日早く終わってしまう。
      月全体を導入されうる期間として扱い、**月末＋30日**までを新台期間とする
      （月末を導入日として保存するわけではない。判定のときだけ使う）。

    ★導入前は新台期間に入れない★＝そちらは公開と育成の担当。
      ここは「導入後に人手で厚くする番が来るか」を決めるための線。
    """
    start, prec = parse_release(release)
    if not start:
        return False                       # 読めない＝新台と推測しない
    if prec == "day":
        end = start + _dt.timedelta(days=days)
    else:
        nxt = _dt.date(start.year + (start.month == 12),
                       (start.month % 12) + 1, 1)
        end = (nxt - _dt.timedelta(days=1)) + _dt.timedelta(days=days)
    return start <= today <= end


def due(slug: str, release: str, today, state: dict, conf=None) -> bool:
    """今日その機種を見るか（★前に見た日が分からなければ見る★）。"""
    seen, prec = parse_release(str((state or {}).get(slug) or ""))
    if not seen or prec != "day":
        return True                        # 記録が無い・壊れている＝見る
    return (today - seen).days >= interval_days(release, today, conf)


# ★見に行った日の控えは、共有の state.json とは別に持つ★
#   （2026-08-13・依頼181のP0）以前は state.json 全体を読んで書き戻していたので、
#   ★一時的に読めなかっただけで、他タスクの履歴をまとめて消す★恐れがあった
#   （読めない＝空扱い → そこへ自分の分だけ足して全体を上書き）。
#   この控えは消えても再確認が増えるだけなので、専用ファイルに分ける。
import os as _os_lp                 # noqa: E402
import sys as _sys_lp               # noqa: E402
_sys_lp.path.insert(0, _os_lp.path.dirname(_os_lp.path.abspath(__file__)))
import local_paths as _lp           # noqa: E402
STATE_PATH = _lp.doc("grow_check.json")


PROBE_STATE = _lp.doc("grow_sources.json")


def _probe_state() -> dict:
    """機種ごとに「前回見た出典URL」を持つ（軽い様子見に使う）。"""
    try:
        with open(PROBE_STATE, encoding="utf-8") as f:
            got = json.load(f)
        return got if isinstance(got, dict) else {}
    except Exception:                     # noqa: BLE001
        return {}


def remember_sources(slug: str, urls: list) -> bool:
    """見た出典URLを控える。★読めないときは書かない★"""
    if not urls:
        return False
    try:
        got = {}
        if os.path.exists(PROBE_STATE):
            try:
                with open(PROBE_STATE, encoding="utf-8") as f:
                    got = json.load(f)
                if not isinstance(got, dict):
                    got = {}
            except Exception as e:        # noqa: BLE001
                print(f"  出典の控えを読めません（書きません）: {e}")
                return False
        # ★★指紋はここでは触らない★★（2026-09-06・Codexの指摘1）
        #   ★ここは下見でも、あとで失敗しても通る場所★なので、
        #   ここで指紋を控えると★記事は古いままなのに「反映済み」★になる
        #   （実害＝下見しただけの prskkm に指紋が入っていた）。
        #   指紋を控えるのは `remember_confirmed()`＝書けたときだけ。
        #   ★前に控えた指紋は消さない★（消すと毎日やり直しになる）
        _was = got.get(slug) if isinstance(got.get(slug), dict) else {}
        got[slug] = {"urls": sorted(set(str(u) for u in urls))}
        if _was.get("cv"):
            got[slug]["cv"] = _was["cv"]
            got[slug]["rules"] = _was.get("rules") or ""
        tmp = f"{PROBE_STATE}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(got, f, ensure_ascii=False, indent=1)
        os.replace(tmp, PROBE_STATE)
        return True
    except Exception as e:                # noqa: BLE001
        print(f"  出典を控えられません（続けます）: {e}")
        return False


def remember_confirmed(slug: str, fp: str = "") -> bool:
    """★2AIの確定値を「記事に反映できた」と控える★（2026-09-06）

    ★呼んでよいのは、実際に書けた／読み比べて足すものが無いと
      分かったときだけ★。
    ★下見や失敗のあとに呼ぶと、記事が古いまま「反映済み」になる★。

    ★★`fp` は「記事に使った指紋」を渡す★★（2026-09-06・Codexの指摘2）
      ★直す前はここで最新を取り直していた★ので、
      書き込み検査のあとに確定値が更新されると
      ★記事には古い値・控えには新しい指紋★になり、
      その分が二度と反映されなかった。
      ★渡されなければ控えない★（取り直さない＝fail-closed）。
    """
    fp = str(fp or "")
    if not fp:
        # ★分からないときは控えない★＝次回も「変わった」として働く
        return False
    try:
        got = {}
        if os.path.exists(PROBE_STATE):
            with open(PROBE_STATE, encoding="utf-8") as f:
                got = json.load(f)
            if not isinstance(got, dict):
                got = {}
        _row = got.get(slug) if isinstance(got.get(slug), dict) else {}
        _row = dict(_row)
        _row["cv"] = fp
        # ★どの決まりで書いたかも残す★（決まりを変えたら調べ直すため）
        _row["rules"] = GROW_RULES_VERSION
        got[slug] = _row
        tmp = f"{PROBE_STATE}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(got, f, ensure_ascii=False, indent=1)
        os.replace(tmp, PROBE_STATE)
        return True
    except Exception as e:                # noqa: BLE001
        print(f"  確定値の控えを書けません（続けます）: {e}")
        return False


def confirm_and_remember(rows: list, slug: str) -> bool:
    """★読み比べたページの基準を進める★（2026-09-06）

    ★★確定値の指紋とは分けた★★（2026-09-06・Codexの指摘4）＝
    以前はここで指紋も控えていたが、この関数は `probe_rows` が要る。
    ★軽い様子見をしていない日は、記事を正しく書いても控えられず★、
    次回もう一度ぜんぶやり直していた（説明と実装が食い違っていた）。
    指紋は `remember_after_write()` が担当する。
    """
    return _pp.confirm(rows)


def remember_after_write(slug: str, cv_used: str) -> bool:
    """★書けたときに、確定値の指紋を控える★（2026-09-06）

    ★`probe_rows` の有無に関係なく控える★＝
    「実際に書けた」「読み比べて足すものが無い」と分かった時に呼ぶ。
    ★渡す指紋は、記事を組み立てるときに実際に読んだもの★（競合よけ）。
    """
    return remember_confirmed(slug, cv_used)


def last_checked() -> dict:
    """slug → 最後に見に行った日（YYYY-MM-DD）。"""
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            got = json.load(f)
        got = (got or {}).get("last_checked")
        return got if isinstance(got, dict) else {}
    except Exception:                     # noqa: BLE001
        return {}


def mark_checked(slug: str, today) -> bool:
    """今日見たことを控える。★書けなくても処理は止めない★

    控えられなければ、その機種は翌日また見に行くだけ（安全側）。
    ★一時ファイルの名前を重ねない★＝同時に走っても互いを壊さない。
    """
    try:
        got = {}
        if os.path.exists(STATE_PATH):
            try:
                with open(STATE_PATH, encoding="utf-8") as f:
                    got = json.load(f)
                if not isinstance(got, dict):
                    got = {}
            except Exception as e:        # noqa: BLE001
                # ★読めないときは書かない★（消してしまわないため）
                print(f"  見に行った日の控えを読めません（書きません）: {e}")
                return False
        got.setdefault("_why", "★育てる処理を見に行った日★（2026-08-13・台帳#346）"
                               "。導入日からの距離で間隔を変えるために使う。"
                               "消えても困らない（その機種を翌日また見るだけ）")
        got.setdefault("last_checked", {})[slug] = today.isoformat()
        tmp = f"{STATE_PATH}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(got, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_PATH)
        return True
    except Exception as e:                # noqa: BLE001
        print(f"  見に行った日を控えられません（続けます）: {e}")
        return False


def targets(rows: list, today=None, state: dict = None,
            conf=None, broken: list = None) -> list:
    """育てる対象（`AUTO_PENDING` の機種のうち、今日見る日のもの）。

    ★外したものを `broken` に入れて返す★（2026-08-27・Codexの指摘13）
      ★直す前は黙って飛ばしていた★ので、判定書が壊れた機種は
      **永久に候補から消え、全体は exit 0 のまま**だった。
      ＝誰も気づけない（★飛ばしたことを黙らない★）。
    """
    out = []
    for m in rows:
        try:
            if _pdz.machine_class(m) != "AUTO_PENDING":
                continue
        except _pdz.DecisionError as e:
            if broken is not None:
                broken.append(f"{m.get('slug')}: {str(e)[:70]}")
            continue                       # 壊れているものは別途 audit が拾う
        if today is not None and not due(m["slug"],
                                        str(m.get("release_date") or ""),
                                        today, state or {}, conf):
            continue
        out.append(m["slug"])
    return out


# ★育てても持ち越す控え★（2026-08-16・依頼213の指摘2／台帳#376）
#   ここに無いものは、育てるたびに identity を作り直す過程で落ちる。
_CARRY_PLAIN = ("_legacy_evidence_ref", "_legacy_official_product_url")
#   ★型式名は「今の材料と食い違わないこと」を確かめてから持ち越す★
_CARRY_CODE = (("regulatory_model_code", "_model_code_sources"),
               ("observed_model_code", "_observed_model_code_sources"))


def _carry_identity(old: dict, new: dict) -> list:
    """★確かめ済みの控えを、育てたあとの identity へ持ち越す★

    ★なぜ要るのか（2026-08-16・台帳#376）★
      規約でP-WORLDと一撃を出典から外したので、**当時2つの出典で
      確かめた型式名が、いま集め直すと1つ以下しか出てこない**
      （実データ: 5機種がこの形）。作り直しに任せると
      「型式名が取れなくなりました」で育てられなくなる。

    ★当時の確認は本物★（Codexも検定番号について同じ結論）なので値は残す。
    ★ただし黙って上書きしない★＝
      **いま集めた材料が別の値を出したら止める**（URLの使い回し・別機種の
      ページに変わった、という本当に危ない形はここで捕まえる）。
      材料が黙っているとき（出典が読めなくなった）だけ持ち越す。
    """
    ng = []
    if not isinstance(new, dict) or not isinstance(old, dict):
        return ng
    for k in _CARRY_PLAIN:
        if old.get(k):
            new[k] = old[k]

    def _code(d):
        """★型式名は採用値と観測値のどちらかに入る★（欄をまたいで見る）

        （2026-08-16・依頼214の指摘1）
        欄ごとに比べていたので、**昔の採用値Aと今の観測値B**が
        別々の欄に同居して素通りしていた（逆向きも同じ）。
        型式名は機種に1つなので、欄ではなく**値**で比べる。
        """
        for k, s in _CARRY_CODE:
            if d.get(k):
                return k, d[k], s
        return "", "", ""

    ok_key, want, want_src = _code(old)
    if not want:
        return ng
    new_key, got, _ = _code(new)
    import claim_identity as _ci
    if got and _ci.normalize_core(got) != _ci.normalize_core(want):
        # ★別の型式名が出てきた＝別機種のページに変わった疑い★
        ng.append(f"型式名が変わっています（{want!r} → {got!r}）"
                  "／★別機種のページに変わった疑い★")
        return ng
    if got:
        # ★同じ値（書き方の違いは吸収）★
        #   ★昔と同じ欄・同じ書き方に寄せる★（2026-08-16・依頼215の指摘1）
        #   ここで今の欄（観測値）に置き換えると、
        #   そのあとの identity_same が「採用値が取れなくなりました」で
        #   止める（実測で再現）。同じ値なのだから、昔の採用をそのまま保つ。
        for k, _s in _CARRY_CODE:
            new.pop(k, None)
            if k != ok_key:
                new.pop(_s, None)
        new[ok_key] = want
        if old.get(want_src):
            new[want_src] = old[want_src]
        if old.get("identity_tier"):
            new["identity_tier"] = old["identity_tier"]
        return ng
    # 材料が黙っている（出典が読めなくなった晩）＝昔の採用を維持する
    new[ok_key] = want
    if old.get(want_src):
        new[want_src] = old[want_src]
    for k, s in _CARRY_CODE:
        if k != ok_key:
            new.pop(k, None)
            new.pop(s, None)
    # ★当時の確からしさも維持する★（2026-08-16・依頼214の指摘2）
    #   いま集め直すと独立2出典がそろわないので、作り直した identity は
    #   必ず CATALOG_BOUND に落ちる。**型式名だけ戻して段だけ落とす**のは
    #   当時の確認を半分否定することになるので、段も一緒に戻す。
    #   ★下げないのではなく「当時の採用をそのまま維持する」★
    if old.get("identity_tier"):
        new["identity_tier"] = old["identity_tier"]
    return ng


def release_refined(old: str, new: str) -> bool:
    """★「月だけ」→「日まで」は食い違いではない★（2026-08-21・台帳#441）

    ★なぜ1か所にまとめたか★
      同じ判断が2か所に要るのに、片方（verify のあと）にしか入っていなかった。
      そのため identity の不変検査（identity_same）が
      「登場年月が変わっています（'2026-08' → '2026-08-17'）」で止め、
      ★garei_zero_re が育てられないままだった★（実データで再現）。
      ＝★同じ規則を2か所に書かない★。

    ★見るのは形だけ★＝新しい値が古い値の続きになっているか。
      意味の判断（本当に同じ導入日か）はしていない。
      月が変わっていれば続きにならないので通らない。

    ★片側だけの細分化しか認めない★＝「日まで」→「月だけ」は通さない
      （分かっていたことが分からなくなるのは、ただの後退）。
    """
    o, n = str(old or ""), str(new or "")
    return len(o) == 7 and n.startswith(o + "-") and len(n) == 10


def identity_same(old: dict, new: dict) -> list:
    """本人性が変わっていないか（★変わっていたら育てない★）。"""
    ng = []
    for k, jp in (("manufacturer_id", "メーカー"),
                  ("regulatory_model_code", "型式名"),
                  ("announced_name", "公式の機種名"),
                  ("official_product_url", "公式URL"),
                  # ★登場年月も不変★（2026-08-05・Codex102回目の指摘3。
                  #   仕様に書いていたのに比べていなかった＝公式の誤抽出や
                  #   一時的な表記変更で「打てる時期」を書き換えられた）
                  ("market_release_date", "登場年月"),
                  # ★1出典しか無い型式の観測値も不変★（2026-08-16・依頼213）
                  #   記事には出ないが同定の手がかり。育てるたびに
                  #   落ちると、確かめ直せない機種の唯一の材料が消える。
                  ("observed_model_code", "観測した型式名"),
                  # ★確からしさの段も不変★（2026-08-16・依頼214の指摘2）
                  #   型式名だけ戻して段が落ちると、当時の確認を半分否定する。
                  ("identity_tier", "本人性の段"),
                  # ★移行前に確かめた記録も不変★（台帳#376）
                  #   ★検定番号はDMMには無い★ので、これが唯一の記録。
                  ("_legacy_evidence_ref", "移行前に確かめた記録"),
                  ("_legacy_official_product_url", "移行前の機種ページURL")):
        a, b = (old or {}).get(k), (new or {}).get(k)
        if a and b and a != b:
            # ★月だけ→日まで、は食い違いにしない★（台帳#441）
            if k == "market_release_date" and release_refined(a, b):
                continue
            ng.append(f"{jp}が変わっています（{a!r} → {b!r}）")
        if a and not b:
            ng.append(f"{jp}が取れなくなりました（登録済み: {a!r}）")
    return ng


def claims_grew(old_decision: dict, material: dict) -> list:
    """材料が「増えるだけ」か（★減る・変わるのは中止★）。

    ★これだけでは足りない★（2026-08-05・Codex102回目）
      claim ID は「天井のゲーム数」までしか表さないので、
      **800G → 999G の書き換えは同じIDのまま通ってしまう**。
      値そのものは `text_kept()` で見る（前に載っていた文が消えないこと）。

    ★★比べる相手を変えた（2026-08-23・台帳#461）★★
      前は「昔の濃さ一覧」と「今夜の濃さ一覧」を比べていた。
      濃さ一覧は**2AIが確定した値を意図的に外す**ので、
      ★2AIで確定させるほど「事実が消えた」と判定される★状態だった
      （実測: 喰霊-零-Re は6件確定させた晩に「3件消えた」で止まった）。

      いまは **「昔に裏取りできていたもの」が「今夜知っていること」に
      残っているか** を見る。左は濃さ一覧（＝当時ちゃんと裏が取れた分だけ）、
      右は回帰用の射影（＝2AIの確定値も含む）。
      ★左右で意味が違うのは意図的★＝濃さ一覧 ⊆ 知っていること なので、
      「当時確かめられた事実が今夜どこにも無い」ときだけ止まる。
      ★今夜の判定書どうしを比べてはいけない★（Codexの指摘）。

    ★もう作らない claim は損失に数えない★（RETIRED_CLAIMS）＝
      外した直後に「型式名が消えた」で全機種が止まるのを防ぐ。
    """
    old = [c for c in ((old_decision or {}).get("claims") or [])
           if c not in _pdz.RETIRED_CLAIMS]
    new = _pdz.regression_claims_from_material(material)
    lost = [c for c in old if c not in new]
    # ★★書けるものは「消えた」ことにしない★★（2026-08-27・運営者の判断）
    #   ★ここには何も書かない★＝設定別の値から範囲を作れるかどうかは
    #   page_decision.derived_payout_range が決め、その結果は上の
    #   regression_claims_from_material に既に入っている。
    #   ★直す前は、記事の側だけで作っていたので、判定書が
    #     「基本スペックは未確認」と言うのに記事が書く食い違いになった★
    if lost:
        return [f"確認済みだった事実が消えます: {', '.join(sorted(lost)[:5])}"]
    return []


def confirmed_count(detail: dict) -> int:
    """確定して載っている中身の数（未確定の印は数えない）。"""
    def _n(x) -> int:
        # ★入れ子を最後までたどる★（表→行→欄→部分）
        if isinstance(x, tuple):
            body = x[1:] if (x and x[0] == CELL) else x
            return sum(_n(y) for y in body)
        return 0 if x == ANY else 1

    return sum(_n(u) for u in _units(detail))


_NAME_MARK = "\uE001"        # ★機種名の入る場所の目印★（本文に出ない字）


def confirmed_fingerprint(slug: str) -> str:
    """★2AIが確定させた値の「いまの姿」の指紋★（2026-09-05）

    ★何に使うか★＝出典が変わっていない日でも、
    **2AIが答えて記録した**なら、それは「変わった」。
    ★読めないときは空を返す★＝呼ぶ側は「変わった扱い」にする
    （fail-closed＝答えを取りこぼすより、余計に働くほうが安全）。
    """
    try:
        got = _cv.for_slug(slug)
    except Exception:                     # noqa: BLE001
        return ""
    if not isinstance(got, dict) or not got:
        return "empty"
    import hashlib as _hl
    # ★★控えの中身を丸ごと見る★★（2026-09-06・Codexの指摘3）
    #   ★直す前は 項目名・decided_at・value だけ★を見ていた。
    #   `merge_into()` は出典URLなども材料へ反映するのに、
    #   `decided_at` は日付までなので、★同じ日に値はそのままで
    #   出典URLだけ直すと指紋が変わらず、古い出典のまま見送られた★。
    #   ★並べ替えて書き出す★＝鍵の順番が違うだけで別物にしない。
    try:
        blob = json.dumps(got, ensure_ascii=False, sort_keys=True,
                          default=str)
    except Exception:                     # noqa: BLE001
        return ""                         # ★書き出せないなら「分からない」★
    return _hl.sha256(blob.encode("utf-8")).hexdigest()


# ★★育て方の決まりの版★★（2026-09-06・Codexの指摘）
#   ★決まりを変えたら、この日付を上げる★＝控えが一度無効になり、
#   すべての機種が次の回に調べ直される。
#   ★なぜ要るか★＝古い決まりで「書かずに指紋だけ控えた」機種は、
#   見送りの分岐で早期に戻るので `machine_row_drift()` まで届かず、
#   ★判定書が古いまま永久に見送られる★。
#   人が控えを消して回るのではなく、機械が気づくようにする。
GROW_RULES_VERSION = "2026-09-06"


def has_confirmed(slug: str) -> bool:
    """★その機種に2AIの確定値が1件でもあるか★（2026-09-06・Codexの指摘3）

    ★`confirmed_drift()` と混ぜない★＝あちらは「前に書いたときから
    変わったか」。控えがまだ一度も無い機種は**変わった扱い**になるので、
    ★答えが1件も無い機種にも「答えが反映できていません」と出ていた★。
    ★読めないときは False★＝無い方に倒す（余計な警告を出さない）。
    """
    try:
        got = _cv.for_slug(slug)
    except Exception:                     # noqa: BLE001
        return False
    return bool(isinstance(got, dict) and got)


def confirmed_drift(slug: str) -> bool:
    """★前に書いたときから、2AIの確定値が変わったか★（2026-09-05）

    ★なぜ要るか（Codexの指摘1・自分で確かめた）★＝
    見送りの分岐は質問を出した直後に return するが、
    確定値を材料へ足す `merge_into` は**その約120行後**にある。
    ＝2AIが答えて記録しても、翌日も同じ分岐で戻るので★永久に届かない★。
    「質問する→答える→反映する」の輪が閉じていなかった。

    ★控えが無い／読めないときは True★（fail-closed＝働くほうへ倒す）。
    """
    now = confirmed_fingerprint(slug)
    if not now:
        return True
    _row = _probe_state().get(slug) or {}
    # ★★育て方の決まりが変わっていたら、必ず調べ直す★★（2026-09-06）
    #   ★これが無いと、古い決まりで控えた機種を人が消して回るしかない★
    if str(_row.get("rules") or "") != GROW_RULES_VERSION:
        return True
    return str(_row.get("cv") or "") != now


def template_drift(machine: dict, old_detail: dict) -> list:
    """★いまのひな型なら別の文になる箇所★（2026-09-03・Codexの指摘3）

    ★見るのは導入文だけ★＝機種名と登場時期をはめ込んだ定型文で、
    **新しい事実を持たない**（だから `_units` の比較からも外してある）。
    ここがずれているのは「ひな型を変えたのに記事が追いついていない」状態。

    ★これを書く理由として数える★＝数えないと、
    ひな型を直した日に既存の記事が**永久に古いまま**残る
    （育成は材料が増えたときしか書かないため）。
    実測（2026-09-03）＝書き出しを直した日、10機種が取り残された。

    ★★機種名では比べない★★＝育成が使う機種名は2AIの確定値で
    上書きされることがあり（`vo["identity_name"]`）、
    こちらで組み立てると**別物と誤判定**する。
    ★ひな型の「名前より後ろ」だけを見る★＝
    ひな型そのものから切り出すので、次にひな型を変えても自動で追随する。

    ★登場時期は機種の一覧から取る★＝`release_date` と
    `identity.market_release_date` は同じ値（実測13機種すべて一致）。
    食い違うときは `identity_same` が別に止める。
    """
    if not isinstance(old_detail, dict) or not old_detail:
        return []
    got = str(old_detail.get("lead") or "")
    if not got:
        return []
    m = machine or {}
    rel = str(m.get("release_date")
              or (m.get("identity") or {}).get("market_release_date") or "")
    if rel:
        whole = _ba.LEAD_TEMPLATE.format(name=_NAME_MARK,
                                         release=_ba._fmt_release(rel))
    else:
        whole = _ba.LEAD_NO_DATE.format(name=_NAME_MARK)
    tail = whole.split(_NAME_MARK, 1)[-1]
    if tail and not got.endswith(tail):
        return ["書き出しの言い回しが、いまのひな型と違います"]
    return []


# ★毎回変わるので、差として数えない欄★（数えると毎日書き直しになる）
_VOLATILE_DECISION_KEYS = ("decided_at",)


def machine_row_drift(old_row: dict, new_machine: dict) -> list:
    """★機種の一覧の側に、書くべき変化があるか★（2026-09-06・Codexの指摘）

    ★なぜ要るか★＝`nothing_new()` は claim の件数と記事本文しか見ない。
    ★本文に出ない確定値★（`machine_profile` など）はどちらも増やさないので、
    「育てるものがありません」と判断され、書かずに指紋だけ控えて
    ★判定書が古いまま永久に検索へ載らない★。
    実例＝prskkm は claim 4件を持ち、止まっている理由は
    `MACHINE_PROFILE_UNKNOWN` だけ。型が確定してもここに落ちる。

    ★毎回変わる欄は外す★＝数えると毎日書き直しになる。
    ★新しい行が作れていないときは何も言わない★（判断材料が無い）。
    """
    if not isinstance(new_machine, dict) or not new_machine:
        return []
    out = []

    def _pd(d):
        pd = dict((d or {}).get("page_decision") or {})
        for k in _VOLATILE_DECISION_KEYS:
            pd.pop(k, None)
        return pd

    if _pd(old_row) != _pd(new_machine):
        out.append("判定書が変わります（検索に載せるかの判断が変わりました）")
    for _k, _why in (("checker", "早見表の材料"), ("identity", "機種の身元")):
        if (old_row or {}).get(_k) != new_machine.get(_k):
            out.append(f"{_why}が変わります")
    return out


def growth_reasons(nn: list, machine: dict, old_detail: dict,
                   new_machine: dict = None) -> list:
    """★今日この機種でやることがあるか★（2026-09-03・切り出した理由は下記）

    引数の `nn` は `nothing_new()` の答え
    （空＝材料が増えた／中身があれば「育てるものがありません」）。

    ★ひな型のずれも「やること」に数える★＝
    数えないと、ひな型を直した日に既存の記事が**永久に古いまま**残る
    （育成は材料が増えたときしか書かないため）。
    実測（2026-09-03）＝書き出しを直した日、13機種が取り残された。

    ★中身の守りは何も緩めない★＝
    「前に載っていた内容が消えていないか」（`text_kept`）は
    呼び出し側で既に通っている。ここは**書く理由**を決めるだけ。

    ★★関数に切り出した理由★★＝`plan_one` の中に埋めていたら、
    壊し方の道具が「この守りは試験で守られていません」と言った
    （そこへ届くには通信して記事を作るところまで行く必要がある）。
    """
    if nn and template_drift(machine, old_detail):
        return []
    # ★★機種の一覧の側の変化も「書く理由」に数える★★
    #   （2026-09-06・Codexの指摘）★数えないと、本文に出ない確定値
    #   （型など）は永久に反映されない★＝書かずに指紋だけ控えるため。
    if nn and machine_row_drift(machine, new_machine):
        return []
    return list(nn or [])


def nothing_new(old_dec: dict, new_dec: dict,
                old_detail: dict, new_detail: dict) -> list:
    """育てるものがあるか（★事実の数だけで見ない★）。

    ★2026-08-05・Codex107回目の指摘★
      claim ID は「CZ-Aがある」までしか表さないので、
      **そのCZの継続G数や期待度が新たに確定しても増えない**。
      claim の数だけを条件にしていたため、
      いちばんやりたい「未確認の欄を埋める更新」が永久に通らなかった。
      載っている確定内容が増えていれば、育てる価値がある。
    """
    grew_claims = len(list((new_dec or {}).get("claims") or [])) >         len(list((old_dec or {}).get("claims") or []))
    grew_text = confirmed_count(new_detail) > confirmed_count(old_detail)
    if grew_claims or grew_text:
        return []
    return ["育てるものがありません（確定した中身が増えていません）"]


ANY = "\uE000"          # ★この欄は何が来てもよい（まだ確定していない）★


CELL = ""        # ★これは「欄」だという印★（表の構造と混ぜない）


def _cell(text: str, pending) -> tuple:
    """1つの欄を、確定した部分と未確定の部分に分ける。

    ★2026-08-05・Codex105回目★
      CZの欄は「継続10 ／ 期待度は出典で書き方が異なります」のように
      **1つの欄に確定と未確定が同居する**。欄ごと「何が来てもよい」に
      していたので、確定していた「継続10」の書き換えを見逃していた。
      区切りで分け、未確定の部分だけを自由にする。
    """
    parts = [p.strip() for p in str(text).split("／")]
    return (CELL,) + tuple(ANY if pending(p) else p for p in parts)


def _same(a, b) -> bool:
    """未確定の印（ANY）は何にでも一致する、という比べ方。

    ★欄の中は「増えてよい」★（2026-08-05・Codex106回目）
      1つの欄には複数の事実が並ぶ（継続・期待度）。後から
      **もう1つ確定して増える**のが正しい更新なので、欄の中だけは
      「前にあった確定分が残っていること」を見る（増えるのは自由）。
      表の行や列の数は従来どおり厳密に比べる（緩めると構造が守れない）。
    """
    if a == ANY:
        return True
    a_cell = isinstance(a, tuple) and a and a[0] == CELL
    b_cell = isinstance(b, tuple) and b and b[0] == CELL
    if a_cell or b_cell:
        if not (a_cell and b_cell):
            return False
        from collections import Counter
        want = Counter(x for x in a[1:] if x != ANY)
        return not (want - Counter(b[1:]))
    if isinstance(a, tuple) and isinstance(b, tuple):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


def _match(old_u, new_units) -> bool:
    """未確定の欄を除いて、同じ単位が新しい側にあるか。"""
    return any(len(n) == len(old_u)
               and all(_same(x, y) for x, y in zip(old_u, n))
               for n in new_units)


# ★★言い換えた表のラベル★★（2026-08-23）
#   ★なぜ要るか★＝表のラベルは `_pending()` を通らず、比較の**鍵そのもの**。
#   ラベルを変えた瞬間、既存記事は「前に載っていた表が消えた」と判定され、
#   ★その機種は永久に更新できなくなる★（実測2件: garei_zero_re / prskkm）。
#   ★なぜ変えたか★＝DMM単独確認の値が1件でも混ざると
#   「出典2件で確認できた」が嘘になるため、表題を中立にした（台帳#443と同じ型）。
#   ★同じ表を指す言い換えだけを書く★（意味の違うラベルを混ぜない）
RENAMED_TABLE_LABELS = {
    "出典2件で確認できたCZ": "確認できたCZ",
}

# ★★もう出さないことにした決まり文句★★（2026-09-01・台帳#538）
#   ★1か所にまとめる理由★＝言い換えるたびに書き忘れて、
#   その文を持つ既存記事が**永久に育たなくなる**。
#   実害＝2026-08-26の言い換えで新しい方を足し忘れ、
#   prskkm と ssb1 が毎朝「内容が消えた」と判定されて止まっていた。
#   ★完全一致だけ★＝箱ごと免除しない（本物の本文は今までどおり守る）。
#   ★足すのは「いまの生成処理が二度と作らない文」だけ★
#   （読者に情報を与える文は入れない）。
RETIRED_BOILERPLATE = (
    # 2026-08-12・運営者決定で廃止
    "出典2件で一致した内容だけを載せています。",
    # 2026-08-26・「どこから採ったかを書かない」に伴う言い換えで廃止
    #   （言い換え先も、その後まもなく生成をやめた）
    "確認が取れた内容だけを載せています。",
)

# ★登場時期の行の頭★（2026-08-23・台帳#461）
#   `build_new_article` が書く形と揃える。★ズレたら試験が落ちる★
#   （_release_line_tests が実際に生成させて頭を確かめる）。
RELEASE_LINE_PREFIX = "**登場時期**："


def _pending_check(t: str) -> bool:
    """その文が「あとで埋まってよい欄」として扱われるか。

    ★試験から本物の判定を通すための入口★（手作りの写しで採点しない）。
    """
    d = {"sections": [{"title": "ゲーム性", "body": [t]}]}
    return not [x for x in _units(d) if x[2] == t]


def retired_boilerplate_problems(phrases=None) -> list:
    """★廃止した決まり文句が、いま本当に作られていないか★（台帳#538）

    ★これが要る理由★＝免除に足すのは「生成処理が二度と作らない文」だけ。
    まだ作っている文を免除すると、★本物の情報が消えても気づかない★。
    ★逆に、作らなくなった文を免除し忘れると、その文を持つ既存記事が
      永久に育たない★（実害2件: prskkm / ssb1）。
    """
    import glob as _g
    out, src = [], ""
    here = os.path.dirname(os.path.abspath(__file__))
    mine = os.path.basename(os.path.abspath(__file__))
    for p in sorted(_g.glob(os.path.join(here, "*.py"))):
        if os.path.basename(p) == mine:
            continue
        try:
            with open(p, encoding="utf-8") as f:
                src += f.read()
        except OSError:
            continue
    for t in (RETIRED_BOILERPLATE if phrases is None else phrases):
        if t in src:
            out.append(f"★まだ作っている文を免除しています★: {t}")
    return out


SPEC_TITLE = "基本スペック"
_SPEC_LINE = None


def spec_row(text: str):
    """基本スペックの本文 `**項目**：値` を (項目, 値) にする。形が違えば None。

    ★表にしても止まらないようにするため★（2026-09-01・Codexの指摘）＝
    本文と表を**同じ形**にしてから比べる。
    ★太字の形だけ★＝太字でない行は文章と区別できない
    （`tableize_spec` と同じ線。my_juggler_v で実際に踏んだ）。
    """
    global _SPEC_LINE
    if _SPEC_LINE is None:
        import re as _re5
        _SPEC_LINE = _re5.compile(r"\A\*\*([^*]+)\*\*：(.*)\Z", _re5.S)
    m = _SPEC_LINE.match(str(text or ""))
    if not m:
        return None
    lab, val = m.group(1), m.group(2)
    if not lab.strip() or not val.strip():
        return None
    return lab, val


def _cell_plain(c):
    """比べる用に包んだセルから、素の文字を取り出す（未確定を含めば None）。"""
    if isinstance(c, tuple) and c and c[0] == CELL:
        parts = c[1:]
        if any(p == ANY for p in parts):
            return None
        return "".join(str(p) for p in parts)
    if c == ANY:
        return None
    return str(c)


def _units(detail: dict) -> list:
    """読者に出ている中身を、比べられる単位に分ける。

    ★段落だけでは足りない★（2026-08-05・Codex103回目の指摘2）
      CZの継続G数や設定別の数値は `tables` にあるので、
      段落だけ比べると「表の値だけ書き換える」改ざんが素通りした。
      factTable（型式名・機械割）も同じ。

    ★「未確認」の行は数えない★（同・指摘3）
      箱ごとの決まり文句（PENDING_TEXT）だけでなく、
      項目ごとの「未確認（確認でき次第掲載します）」（PENDING_ITEM）も除く。
      ここを数えると、**未確認を埋める正しい更新まで拒否**してしまう。
    """
    out = []
    if not isinstance(detail, dict):
        return out

    def _pending(t: str) -> bool:
        """まだ確定していない書き方か（★あとで埋まってよい欄★）。

        ★決まり文句は1種類ではない★（2026-08-05・Codex104回目）
          CZの値が採れないときは「確認中」、値が採れていない設定があるときは
          注記に「値が確認できていないため掲載していません」と出る。
          これらを「確定した内容」として数えると、
          **後から埋まった正しい更新を「消えた」と誤判定**してしまう。
        """
        # ★もう出さないことにした決まり文句★（名簿は RETIRED_BOILERPLATE）
        #   既に公開した記事には入っているので、消えても「内容が減った」と
        #   判定しない（読者に情報を与える文ではない）。
        if t in RETIRED_BOILERPLATE:
            return True
        # ★同じ日に廃止したもう1つ＝空の噂の箱★（2026-08-23・台帳#461）
        #   「噂の箱は中身があるときだけ出す」と決めた（2026-08-12・運営者判断）
        #   ので、いまの生成器はこの文を**二度と出さない**。
        #   ★これが無いと8/12より前の3記事は永久に育たない★
        #   （garei_zero_re 08-07 / prskkm 08-10 / ssb1 08-11・実測）。
        #   ★完全一致だけ★＝箱ごと免除しない。実際の噂の本文が同居していても
        #   そちらは今までどおり守られる（Codexの指摘を狭い側で満たす）。
        if t == _ba.RUMOR_SECTION["body"][0]:
            return True
        return (t == _ba.PENDING_TEXT) or (_ba.PENDING_ITEM in t) \
            or (t == getattr(_ba, "PENDING_TEXT_OLD", "\0")) \
            or (t.strip() == "確認中") or ("確認できていない" in t) \
            or ("出典で食い違い" in t) or ("書き方が異なります" in t)

    for s in detail.get("sections") or []:
        title = str(s.get("title"))
        for b in (s.get("body") or []):
            t = str(b).strip()
            if t.startswith(RELEASE_LINE_PREFIX):
                # ★登場時期はここで比べない★（2026-08-23・台帳#461）
                #   ★同じ規則を2か所に書かない★＝登場年月は
                #   `identity_same()`（不変）と `release_refined()`
                #   （「月だけ」→「日まで」だけ許す）が既に守っている。
                #   ここは**その値を文にした写し**なので、書き方を変えた日に
                #   （2026-08-12「2026年8月（公式確認）」→
                #    「2026年8月17日 導入」）**再現できない文**になり、
                #   ★中身は前より正確なのに更新が永久に止まった★（実測）。
                continue
            # ★★基本スペックは、本文でも表でも同じ形で数える★★
            #   （2026-09-01・Codexの指摘）＝形式を変えただけで
            #   「内容が消えた」と判定されると、その機種は永久に育たない。
            _sr = spec_row(t) if title == SPEC_TITLE else None
            if _sr:
                if _sr[0].strip() == "登場時期":
                    continue          # ★登場時期はここで比べない★（上と同じ理由）
                if not _pending(_sr[1]):
                    out.append(("spec-row", _sr[0].strip(), _sr[1].strip()))
                continue
            if t and not _pending(t):
                out.append(("body", title, t))
        for tb in (s.get("tables") or []):
            label = str((tb or {}).get("label") or "")
            # ★言い換えたラベルは同じ表として比べる★（上の説明を参照）
            label = RENAMED_TABLE_LABELS.get(label, label)
            headers = tuple(str(x) for x in ((tb or {}).get("headers") or []))
            for row in ((tb or {}).get("rows") or []):
                # ★未確定の欄だけを「何が来てもよい」にする★
                #   （2026-08-05・Codex104回目の指摘3。行ごと捨てていたので、
                #     同じ行の**確定済みの欄**まで比べられなくなっていた）
                cells = tuple(_cell(x, _pending) for x in (row or []))
                # ★★基本スペックの2列表は、本文と同じ形にそろえる★★
                #   （2026-09-01・Codexの指摘）
                if title == SPEC_TITLE and len(cells) == 2 \
                        and headers == ("項目", "内容"):
                    _lab = _cell_plain(cells[0])
                    _val = _cell_plain(cells[1])
                    if _lab is None:
                        continue      # 項目名が未確定なら比べない
                    if _lab.strip() == "登場時期":
                        continue      # ★登場時期はここで比べない★
                    if _val is not None:
                        out.append(("spec-row", _lab.strip(), _val.strip()))
                    continue      # ★登場時期はここで比べない★
                    if cells[1] != ANY:
                        out.append(("spec-row", _lab,
                                    str(cells[1]).strip()))
                    continue
                out.append(("table", title, label, headers, cells))
            note = str((tb or {}).get("note") or "").strip()
            if note and not _pending(note):
                out.append(("note", title, label, note))
    for row in (detail.get("factTable") or []):
        cells = tuple(str(x) for x in (row or []))
        if not any(_pending(c) for c in cells):
            out.append(("fact",) + cells)
    for box in (detail.get("summaryBoxes") or []):
        lab = str((box or {}).get("label") or "")
        val = str((box or {}).get("value") or "")
        if val and not _pending(val):
            out.append(("box", lab, val))
    # ★導入文もここでは比べない★（2026-08-23・台帳#461）
    #   導入文は機種名と登場時期だけをはめ込んだ定型文
    #   （LEAD_TEMPLATE / LEAD_NO_DATE）で、独自の事実を持たない。
    #   機種名は `identity_same()` の announced_name が、
    #   登場時期は `release_refined()` が守っている。
    #   ★ここで比べると、上の登場時期と同じ理由で二重に止める★
    #   （実測: 「…登場時期は2026年8月（公式発表を確認済み）。」が
    #     いまの生成器では二度と出ない）。
    return out


def _ceiling_note_may_go(old_detail: dict, new_detail: dict) -> bool:
    """★天井の断り書きが消えてよい更新か★（2026-08-12・依頼161）

    断り書きは「ほかにも天井があるかもしれない」という**読者への断り**。
    これが消えるのは、天井が全部そろったときだけであるべき。
    ★消えたこと自体を見逃すのではなく、「天井の箱が実際に増えた」
      ことを条件にする★（無条件に外すと、材料が増えていないのに
      断り書きだけ消える更新を誰も止められない）。
    """
    note = _ba.CEILING_PARTIAL_NOTE

    def lines(d):
        for sec in (d.get("sections") or []):
            if not isinstance(sec, dict) or sec.get("title") != "天井・恩恵":
                continue
            return [str(x).strip() for x in (sec.get("body") or [])
                    if str(x).strip() and str(x).strip() != note]
        return []

    was, now = lines(old_detail), lines(new_detail)
    if len(now) > len(was) and all(x in now for x in was):
        return True
    # ★「一覧は同じまま、これで全部だと確認できた」も正しい更新★
    #   （2026-08-12・依頼162のP1。行数が増えたときしか許さないと、
    #     網羅性だけ確かめた更新が永久に公開できない）
    #   ★真偽値の真だけ★＝文字列の "true" では許さない。
    return (new_detail.get("ceilings_complete") is True
            and now == was)


def text_kept(old_detail: dict, new_detail: dict) -> list:
    """★前に載っていた文が、そのまま残っているか★（値の書き換えを止める）

    2026-08-05・Codex102回目の指摘2。claim ID の比較では
    「800G・AT当選」→「999G・CZ当選」を止められない。
    そこで**確認済みとして既に出ている文**を1つでも失う更新は拒否する
    （足すのは自由・消す/変えるのは不可＝本当の意味での単調追加）。
    """
    old, new = _units(old_detail), _units(new_detail)
    # ★天井の断り書きは「天井が実際に増えたとき」だけ消えてよい★
    #   （2026-08-12・依頼161。無条件に比較から外すと、
    #     網羅性が未確認のまま断り書きだけ消える更新を止められない）
    if _ceiling_note_may_go(old_detail, new_detail):
        note = _ba.CEILING_PARTIAL_NOTE
        old = [u for u in old if not (u[0] == "body" and u[-1] == note)]
    rest = list(new)
    gone = []
    for u in old:
        hit = next((n for n in rest if _match(u, [n])), None)
        if hit is None:
            gone.append(" ".join(str(x) for x in u).replace(ANY, "（未確定）")[:60])
        else:
            rest.remove(hit)               # ★同じ物を二重に使わない（数も見る）★
    if not gone:
        return []
    return [f"前に載っていた内容が消える/変わる更新です: {' / '.join(gone[:3])}"]


# ★★2AIで何回まで粘るか★★（2026-08-27・運営者の指示）
#   「2AIで結論出して。人に頼らないで。本当にどうしてもの場合だけメール」
STUCK_ASK_LIMIT = 3


def _stuck_state() -> dict:
    """控えを丸ごと読む（★読めないときは空★）。"""
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            got = json.load(f)
        return got if isinstance(got, dict) else {}
    except Exception:                     # noqa: BLE001
        return {}


def _stuck_save(got: dict) -> bool:
    """控えを書く（★書けなくても処理は止めない★）。"""
    try:
        tmp = f"{STATE_PATH}.{os.getpid()}.stuck.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(got, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_PATH)
        return True
    except Exception as e:                # noqa: BLE001
        print(f"  行き詰まりの回数を控えられません（続けます）: {e}")
        return False


def _stuck_count(slug: str, add: int = 0, today: str = "") -> int:
    """その機種で「決まらなかった」回数（★機種ごとに数える★）。

    ★別の機種の失敗を持ち越さない★／★うまく育った日は0に戻す★。
    置き場は「見に行った日」の控えと同じファイル。
    ★控えを書けなかったときは1回目扱い★＝いきなり人へ報告しない。
    """
    # ★★同じ日に何度動いても1回と数える★★
    #   （2026-08-27・Codexの5回目の指摘5）
    #   ★直す前は「行き詰まりを見つけた回数」だった★ので、
    #   下見や再起動で同じ晩に3回動くと、
    #   ★2AIが2回検討していなくても人へ回った★。
    #   数えたいのは「2AIが別の日に考え直しても決まらなかった」回数。
    got = _stuck_state()
    book = got.setdefault("grow_stuck", {})
    rec = book.get(slug)
    if not isinstance(rec, dict):         # ★古い形（数だけ）から移す★
        rec = {"n": int(rec or 0), "day": ""}
    today = today or _dt.date.today().isoformat()
    if add and rec.get("day") != today:
        rec = {"n": int(rec.get("n") or 0) + add, "day": today}
        book[slug] = rec
        if not _stuck_save(got):
            return min(rec["n"], 1)
    return int(rec.get("n") or 0)


def _stuck_clear(slug: str) -> None:
    """うまく育ったので、その機種の回数を0に戻す。"""
    got = _stuck_state()
    if (got.get("grow_stuck") or {}).pop(slug, None) is not None:
        _stuck_save(got)


# ★足りない理由を、読者に分かる言葉へ★（★機械が決めた符丁のまま渡さない★）
#   ★これは表示の言い換えであって、採否も可否も決めていない★
#   ★知らない符丁はそのまま出す★（黙って捨てない）
_LACK_WORDS = {
    "MACHINE_PROFILE_UNKNOWN": "機種の型（AT機かボーナス機か）",
    "CLAIMS_LT_3": "確認できた事実が3件に足りない",
    "CATEGORIES_LT_2": "確認できた話題が2種類に足りない",
    "NO_UNIQUE_GAMEPLAY": "その機種ならではのゲーム性（AT・CZ）",
    "NO_BONUS_PROB": "設定ごとのボーナス確率",
}

# ★★判定書に直に載っている「まだ決まっていない欄」★★
#   （2026-09-05・Codexの指摘3＝自分で確かめた）
#   ★なぜ理由コードで見ないか★＝`CEILING_STATE_UNKNOWN` は
#   page_decision が**一度も出さない**（実測0か所）。
#   理由コードだけを見ていると★天井の有無は永久に聞けない★。
#   実測（prskkm）＝`ceiling_state: UNKNOWN` なのに
#   理由コードは `MACHINE_PROFILE_UNKNOWN` だけだった。
#   ここは「未回答の欄を2AIへ渡す」だけで、意味は判定していない。
_UNKNOWN_FIELDS = (
    # (判定書の欄, 記録する項目名, 聞く文, 書き方の例)
    ("machine_profile", "machine_profile",
     "★この機種の型を判断してください★"
     "（AT_CZ＝ATまたはCZを持つ／BONUS＝完全告知などのボーナスタイプ）"
     "／★決まらないと検索に載せられません★。",
     '{"profile": "BONUS"}'),
    ("ceiling_state", "ceiling_state",
     "★この機種に天井があるかを判断してください★"
     "（PRESENT＝ある／NONE＝ない）"
     "／★「ボーナスタイプだから天井なし」と決めないでください★"
     "＝別々に確かめること。",
     '{"state": "PRESENT"}'),
)


def pending_questions(cur: dict, mat: dict = None, slug: str = "",
                      urls=None) -> list:
    """★まだ検索に載っていない機種は、2AIが原文を読んで埋める★

    ★運営者の指示（2026-09-05）★＝
    「機械的なのやめれば？ 2AIが天井の情報を取りに行けばいいじゃん」

    ★なぜ要るか★＝機械（抽出器）が「天井の記述はあるが採れませんでした」で
    止まると、そのまま「材料が増えていません」で終わり★誰にも回らなかった★。
    実測＝新台14機種のうち0件しか検索に載っておらず、
    12機種で天井=0件、7機種は型が UNKNOWN のままだった。

    ★決めるのは2AI／ここは「どこを読めばよいか」を渡すだけ★。
    ★もう載っている機種には聞かない★（答える意味がないので）。
    """
    pd = (cur or {}).get("page_decision") or {}
    if pd.get("indexable"):
        return []
    out = []
    if mat is not None:
        for q in _ba.checker_questions(mat):
            out.append({"text": str(q), "kind": "grow_pending", "slug": slug})
    # ★★判定書に「まだ決まっていない」と書いてある欄は、必ず聞く★★
    #   （2026-09-05）★材料が無い日（見送り）でも聞ける★のがここの値打ち。
    #   ★★同じ項目を二重に聞かない★★（実機で型・天井が2つずつ出た）＝
    #   記録するコマンドは必ず `--field <項目名>` を含むので、
    #   ★すでにその項目を聞いている質問があるなら足さない★
    #   （文字の意味は読まない。コマンドの形を見るだけ）。
    for _key, _field, _ask, _example in _UNKNOWN_FIELDS:
        if str(pd.get(_key) or "UNKNOWN") != "UNKNOWN":
            continue
        if any(f"--field {_field} " in str(q.get("text") or "") for q in out):
            continue
        out.append({
            "kind": "grow_pending", "slug": slug, "field": _field,
            "text": _ask + _ba._record_howto(_field, _example)})
    # ★★足りないものを名指しして、出典を読んでもらう★★
    lack = [_LACK_WORDS.get(r, r) for r in (pd.get("reason_codes") or [])]
    if lack:
        _u = [str(u) for u in (urls or []) if u]
        out.append({
            "kind": "grow_read_sources", "slug": slug, "urls": _u,
            "text": ("★この機種はまだ検索に載っていません★ " + str(slug)
                     + "／足りないもの: " + " ／ ".join(lack)
                     + "／★機械では取り出せなかったので、出典の原文を"
                       "自分で読んで決めてください★"
                     + ("／読む先: " + " ".join(_u[:4]) if _u else "")
                     + "／決めたら confirmed_values.py --record で記録"
                       "（逐語引用が要ります）")})
    # ★同じ文の質問はまとめる★（材料からの質問と、判定書からの質問が重なる）
    seen, uniq = set(), []
    for q in out:
        _t = str(q.get("text") or "")
        if _t in seen:
            continue
        seen.add(_t)
        uniq.append(q)
    return uniq


def grow_result(slug: str, ok: bool, why: str = "",
                today: str = "") -> dict:
    """★育てた結果を受けて、次にどうするかを決める★（2026-08-27）

    ★ここに切り出した理由★＝判断が処理の中に埋まっていると、
    通しで動かさないと試験できない＝★その守りを一度も確かめられない★。
    （CLAUDE.md「奥の層は、手前を通さずに直接呼んで試す」）

    ok=True  … うまく育った → 回数を0に戻す
    ok=False … 行き詰まった → 1〜2回目は2AIに聞く／3回目で人へ報告
    """
    if ok:
        _stuck_clear(slug)
        return {"do": "ok"}
    n = _stuck_count(slug, add=1, today=today)
    if n < STUCK_ASK_LIMIT:
        return {"do": "ask", "round": n,
                "text": ("★2AIで決めてください★ " + slug
                         + " で、前に載せた内容を今夜の材料で再現できません: "
                         + why
                         + "／出典を取り直すか、別の出典を当たるか、"
                         "その内容を落とすかを決めてください"
                         f"（{n}回目・{STUCK_ASK_LIMIT}回で報告します）")}
    return {"do": "ledger", "round": n,
            "detail": (why + f"／★2AIで{STUCK_ASK_LIMIT}回試しても"
                       "決まりませんでした★／出典の一時的な不調かもしれません。"
                       "旧い内容は公開されたままです。")}


def ledger_once(slug: str, title: str, detail: str,
                severity: str = "MATERIAL") -> None:
    """★黙って止まり続けない★（2026-08-05・Codex102回目）

    確認済みだった内容が再現できなくなった時、毎日同じ理由で止まるだけだと
    **誰も気づかないまま古い内容が公開され続ける**。
    台帳へ1件だけ上げる（同じ題なら重複せず last_seen が更新される）。
    ★無人タスクは close しない★＝人が判断する。
    """
    try:
        # ★CLIの引数の形に依存しない入口を使う★（2026-08-10・台帳#300）
        #   Namespace を手で組んでいたので、CLIに引数が増えるたびに
        #   ここが黙って壊れていた（安全網が黙って死んでいた）。
        _oi.add_issue(_oi.DEFAULT_FILE,
                      source="grow-machine", slug=slug, kind="external_value",
                      title=title, detail=detail, severity=severity,
                      reason_code="GROW_VALUE_LOST")
    except Exception as e:                # noqa: BLE001
        _log(f"  台帳に登録できませんでした: {type(e).__name__}: {e}")


def blocked_by_ledger(slug: str) -> list:
    """台帳に「止めるべき」案件があるか。"""
    try:
        got = _oi.blocking_slugs()
    except Exception as e:                # noqa: BLE001
        return [f"台帳を読めません: {e}"]   # ★読めない時は進めない★
    why = got.get(slug)
    return [f"台帳に止めるべき案件があります: {' / '.join(why)}"] if why else []



def _read_rows() -> list:
    return _sj.read_rows(MACHINES)


def _detail_path(slug: str) -> str:
    return os.path.join(DETAILS, f"{slug}.json")


def find_sources(machine: dict) -> list:
    """いまの出典の顔ぶれ（URL）を数え直す。★軽い工程★

    ★なぜ要るか★（2026-08-14・依頼185のP1）
      既知ページの中身だけを見て見送ると、**別の名鑑に新しく記事が出ても
      永久に気づかない**。顔ぶれが変わっていないことも確かめてから見送る。
      失敗したら空を返す＝呼ぶ側は「見送らない」（fail-closed）。
    """
    try:
        import directory_index as _di
        ident = machine.get("identity") or {}
        name = ident.get("announced_name") or machine.get("name") or ""
        if not name:
            return []
        return [str(u) for u in _di.found_urls(_di.find(name)) if u]
    except Exception:                     # noqa: BLE001
        return []


def plan_one(slug: str, gather=None, verify=None, probe=None,
             find=None) -> dict:
    """育てられるか調べて、新しい機種データ・記事を作る（★書き込まない★）。

    ★指紋は「読む前」に取る★（2026-08-05・Codex103回目の指摘1）
      以前は計画が終わってから指紋を取っていたので、
      **計画中に別の処理が書き換えると、その新しい姿を指紋にしてしまい**、
      古い計画で相手の追加を消せた。読む前に取れば必ず食い違って止まる。
    """
    out = {"slug": slug, "problems": [], "machine": None, "detail": None,
           "was": None, "now": None, "checked": False, "unchanged": False,
           # ★お知らせは「問題」に入れない★（2026-08-14・依頼190のP1）
           #   「出典の顔ぶれが変わりました／いつもどおり調べます」を
           #   problems に入れていたため、**その文自身が更新を止めていた**。
           "notes": [], "nothing_new_only": False, "cv_used": "",
           # ★2AIへ聞くこと★（2026-08-26）＝材料集めが出した質問を
           #   ここまで持ってくる。★読まないと誰も答えられない★
           "questions": [],
           # ★読む入力は全部指紋に入れる★（2026-08-11・依頼148の指摘2）
           #   2AIの確定値を読むようにしたのに、そこだけ監視の外だった。
           #   計画のあと・書き込みの前に確定値を取り消しても、
           #   古い計画のまま公開できてしまう。
           #   ★無い状態も指紋に入れる★（後から現れた場合も食い違わせる）
           "fingerprint": {p: (_file_sha(p) if os.path.isfile(p) else "")
                           for p in (_detail_path(slug), _pub._page_path(slug),
                                     MACHINES, SITEMAP, _cv.STORE)}}
    rows = _read_rows()
    cur = next((m for m in rows if m.get("slug") == slug), None)
    if cur is None:
        out["problems"].append("その機種は一覧にありません")
        return out
    try:
        out["was"] = _pdz.machine_class(cur)
    except _pdz.DecisionError as e:
        out["problems"].append(f"いまの判定書が壊れています: {e}")
        return out
    if out["was"] != "AUTO_PENDING":
        out["problems"].append(f"育てる対象ではありません（いまの区分: {out['was']}）")
        return out
    mode = (_pdz.load_policy() or {}).get("mode")
    if mode != "normal":
        out["problems"].append(f"検索方針が通常ではありません（{mode}）")
        return out
    out["problems"] += blocked_by_ledger(slug)
    left = _pub.unfinished()
    if left:
        out["problems"].append(
            f"前回の公開が途中で終わっています（{left.get('slug')}）")
    if out["problems"]:
        return out

    # ★★軽い様子見★★（2026-08-13・台帳#346・運営者の採用）
    #   前回見た出典が1つも変わっていなければ、その日は何もしない。
    #   ★これは「今日は書かなくてよいか」の判定にだけ使う★
    #   ＝「変わっていないから前の材料を使い回して書く」は絶対にしない
    #     （出典が消えたり書き換わったりしても古い値を出し続けるため）。
    #   ★確かめられなかったページは「変化なし」に数えない★（fail-closed）
    #   ★新しい出典を探す工程は省かない★＝ここは既知のURLしか見ない。
    # ★見送ってよいのは「出典の顔ぶれも中身も同じ」ときだけ★
    #   （2026-08-14・依頼185のP1）以前は既知URLだけを見て見送っていたので、
    #   ★別の名鑑に新しく記事が出ても永久に気づかなかった★
    #   （「新しい出典を探す工程は省かない」と書いておきながら省いていた）。
    #   いまは先に**いまの出典の顔ぶれ**を数え直し、前回と同じで、
    #   かつ既知ページが全部同じときだけ見送る。
    out["probe_rows"] = []
    if probe is not False:
        _known = sorted((_probe_state().get(slug) or {}).get("urls") or [])
        if _known:
            _now = sorted((find or find_sources)(cur) or [])
            if _now and _now == _known:
                _pr = (probe or _pp.check_all)(_known)
                out["probe_rows"] = _pr.get("rows") or []
                # ★★ひな型がずれていたら見送らない★★
                #   （2026-09-03・Codexの指摘3）＝出典が変わらなくても、
                #   こちらのひな型を変えた日は記事を追いつかせる必要がある。
                #   ★③と同じ関数・同じ材料で見る★＝食い違うと
                #   「毎日調べ直すが毎日書かない」を繰り返す。
                _dp0 = _detail_path(slug)
                _od0 = (_sj.read_json(_dp0, expect=dict)
                        if os.path.isfile(_dp0) else {})
                if (_pr.get("skip") and not template_drift(cur, _od0)
                        and not confirmed_drift(slug)):
                    out["problems"].append(
                        "出典の顔ぶれも中身も前回から変わっていません"
                        "／2AIの確定値も増えていません（今日は見送ります）")
                    out["unchanged"] = True
                    # ★★見送る日でも、載っていないなら聞く★★（2026-09-05）
                    #   ★直す前★＝ここで戻っていたので、質問に到達しなかった
                    #   （実測：パリピ孔明は材料が足りているのに永久に沈黙）。
                    #   ★出典が変わっていなくても、機械が読めていないだけで
                    #   原文には書いてある★のだから、2AIが読めば決まる。
                    out["questions"] += pending_questions(
                        cur, None, slug, urls=_known)
                    return out
            elif _now and _now != _known:
                # ★これは「調べる理由」であって「できない理由」ではない★
                #   （2026-08-14・依頼190のP1）problems に入れると
                #   後段の `if out["problems"]: return` で必ず止まり、
                #   それでも main は新しい顔ぶれを控えるので、
                #   ★新しい出典を見つけた日だけ書けず、翌日は見送る★
                #   という逆流ができていた。
                out["notes"].append(
                    f"出典の顔ぶれが変わりました（{len(_known)}件→{len(_now)}件）"
                    "／いつもどおり調べます")

    ident = cur.get("identity") or {}
    name = ident.get("announced_name") or cur.get("name")
    maker = ident.get("manufacturer_id") or ""
    url = ident.get("official_product_url") or ""
    # ① 本人性を確かめ直す（同定はDMMの機種ページで行う）
    #   ★以前ここで一覧カード用の下ごしらえをしていた★（`_ensure_list`）。
    #   メーカー公式の巡回は 2026-08-16（台帳#377）に仕組みごと消したので、
    #   呼び出しだけが残って **毎朝 NameError で落ちていた**（2026-08-17に発覚）。
    import add_machine_run as _amr
    verify = verify or _amr.verify_official
    # ★登録済みの登場年月を渡して照合させる★（空だと検査ごと素通りする）
    old_release = str(cur.get("release_date") or "")
    if old_release and str(ident.get("market_release_date") or "") != old_release:
        out["problems"].append(
            f"登録済みの登場年月が食い違っています（一覧 {old_release!r} / "
            f"identity {ident.get('market_release_date')!r}）")
        return out
    vo = verify(name, url, maker, old_release)
    if vo.get("problems"):
        out["problems"] += [f"本人性を確かめ直せません: {p}" for p in vo["problems"]]
        return out
    if old_release and vo.get("release") and vo["release"] != old_release:
        # ★★「月だけ」→「日まで」は食い違いではない★★（2026-08-21・台帳#383）
        #   2026-08-16 に公開済み機種の身元を DMM へ移した（台帳#377）。
        #   移す前は「2026-08」のように月だけだった機種があり、DMM は日まで持つ。
        #   ★同じ月をより細かく言っているだけ★なので、止める理由がない。
        #   直す前は、この形で**育てるレーンが全機種止まっていた**
        #   （2026-08-17に garei_zero_re / prskkm / ssb1 の3件が全部これ）。
        #
        #   ★見るのは形だけ★＝新しい値が古い値の続きになっているか
        #   （「2026-08」+「-」で始まるか）。意味の判断はしていない。
        #   ★release_date は勝手に書き換えない★＝ここでは通すだけ。
        #   細かくするかどうかは別の判断（値を作る話になるため）。
        if not release_refined(old_release, vo["release"]):
            out["problems"].append(
                f"登場年月が変わっています（{old_release} → {vo['release']}）"
                "／自動では直しません")
            return out
        # ★★日が分かったことを「予定」として返す（★ここでは書かない★）★★
        #   （2026-08-29・運営者の指示／Codexの指摘を受けて作り直した）
        #   ★ここで書いてはいけない理由★
        #     ①このあと指紋を取るので、書くと `apply_one` が
        #       「計画後に変わった」で必ず止まる
        #     ②下見（--apply なし）でも書いてしまう
        #     ③鍵の外なので、同時に走る処理の更新を消しうる
        #   ★書くのは呼び出し側（--apply のとき・鍵の中・計画の前）★
        out["release_refine"] = {"old": old_release, "new": vo["release"]}
    # ② 材料を集め直す
    gather = gather or _amr.gather
    # ★★機種を名乗って材料を集める★★（2026-08-20・Codex依頼239）
    #   ★穴だったところ★＝slug を渡していなかったので、
    #   `maker_material_decision` が**機種ごとの控えを引かず**、
    #   2AIが「このページは使わない」と決めた分が
    #   更新経路では効いていなかった。名簿の変更でそのページが
    #   MATCH になった瞬間、拒否を無視して材料に戻せた。
    #   （新台経路は最初から渡している。更新経路だけ抜けていた）
    got = gather(name, maker, slug=slug,
                 machine_name=vo.get("identity_name") or name,
                 release_date=str(vo.get("release") or ""))
    # ★次回の「軽い様子見」に使うため、見た出典URLを控える★
    #   （2026-08-13・台帳#346）ここで初めて確定するので、
    #   材料を集めたあとに控える。書けなくても処理は止めない。
    out["source_urls"] = list(got.get("urls") or [])
    # ★★材料が集まらなくても質問は受け取る★★（2026-08-26）
    #   ★止まる理由そのものが質問になっている★ことが多い
    #   （同定で外れたページを使ってよいか、等）。
    #   材料が無いときに捨てると、**止まった機種が永久に解けない**。
    out["questions"] += [q for q in (got.get("maker_questions") or [])]
    # ★材料が返っても「書いてはいけない理由」があれば止める★
    #   （2026-08-05・Codex102回目の指摘1。転載の疑いなどは
    #     material が作られても新台側では公開を止めている）
    blk = _amr.blocking_problems(got.get("problems") or [])
    if blk:
        out["problems"] += [f"止めました: {p}" for p in blk]
        return out
    mat = got.get("material")
    if not mat:
        # ★★材料が集まらなくても、2AIの確定値だけで進む★★
        #   （2026-09-06・Codexの指摘1。★私は「直せない」と判断したが
        #     間違いだった★＝`merge_into({})` は空の辞書を受け取り
        #     `setdefault()` で箱を作る（実測＝prskkm で3項目・
        #     at_specs / czs の箱ができた）。記事の骨組み
        #     （機種名・メーカー・公式URL・導入日）は `mat` ではなく
        #     既存行と本人性確認から渡している）。
        #   ★安全網はそのまま★＝あとの `claims_grew()` / `text_kept()` が
        #   「前に載っていた内容を再現できない」なら止める。
        if not has_confirmed(slug):
            out["problems"].append("材料を集められません: "
                                   + " / ".join(got.get("problems")
                                                or [])[:200])
            # ★★材料が集まらない日でも、決まっていないことは聞く★★
            #   （2026-09-05・Codexの指摘3）★ここで戻ると、
            #   出典が1つしか無い機種などは**永久に沈黙する**。
            out["questions"] += pending_questions(cur, None, slug,
                                                  urls=got.get("urls"))
            return out
        out["notes"].append(
            "材料を集められませんでしたが、2AIの確定値だけで進みます"
            "（" + " / ".join(got.get("problems") or [])[:160] + "）")
        mat = {}
        got["material"] = mat
    # ★ここまで来たら「実際に見に行けた」★（依頼181のP1）
    #   本人性の確認や材料集めに失敗した機種を「確認済み」にしない。
    out["checked"] = True
    # ★★まだ検索に載っていないなら、決められないことを必ず聞く★★
    #   （2026-09-05）★ここに置く理由★＝後段（記事の組み立て・
    #   消失の判定）がどう転んでも、載っていない機種には聞くべきだから。
    #   材料が増えなかった回ほど、この質問が要る。
    out["questions"] += pending_questions(cur, mat, slug,
                                          urls=got.get("urls"))
    # ★2AIで確定した値も材料に足す★（2026-08-11・台帳#316）
    #   足す場所が add_machine_run の中の1か所にしか無かったので、
    #   **確定値を載せた機種はここで「前に載っていた内容が再現できない」**
    #   と判定され、育てる処理が毎日止まっていた（パリピ孔明・ガレイゼロ）。
    #   ＝「読む側を1か所しか繋いでいなかった」型の穴。
    #   ★読めないことを黙って「無い」にしない★（例外は理由として残す）
    # ★★記事に使う指紋は、この時点のものを持ち回る★★
    #   （2026-09-06・Codexの指摘2）＝あとで取り直すと、
    #   書き込み検査のあとに確定値が更新されたとき
    #   ★記事には古い値・控えには新しい指紋★になり、その分が消える。
    out["cv_used"] = confirmed_fingerprint(slug)
    try:
        _added = _cv.merge_into(mat, slug)
        if _added:
            _log("  2AIで確定した値を材料に足しました: " + " / ".join(_added))
            # ★★育てる側も、出典を取り直して確かめる★★
            #   （2026-08-24・Codexの17回目）
            #   ★新台側だけ再確認を通していた★＝
            #   出典が変わっても、控えを手で書き換えられても、
            #   **育てる経路だけは値を公開できた**。
            #   ＝「record も load も grow も緑。繋ぐと再確認が抜ける」型。
            _rv = _cv.reverify(slug, name=vo.get("identity_name") or name,
                               official_url=url)
            if _rv:
                out["problems"] += [
                    f"控えを確かめ直せません: {x}" for x in _rv]
                return out
    except Exception as e:                    # noqa: BLE001
        out["problems"].append(
            f"2AIの確定値を読めません: {type(e).__name__}: {e}")
        return out
    release = vo.get("release") or (cur.get("release_date") or "")
    machine = _ba.build_machine(
        slug, vo.get("identity_name") or name, maker, url, release, mat,
        identity_binding=ident.get("identity_binding", ""),
        identity_evidence_ref=ident.get("identity_evidence_ref", ""))
    # ★育てても消えてはいけない控え★（2026-08-16・依頼213の指摘2）
    #   identity は毎回作り直すので、明示的に引き継がないと落ちる。
    #   ★検定番号はDMMには無い★＝移行前の記録が唯一の記録になる。
    out["problems"] += _carry_identity(ident, machine.get("identity"))
    detail = _ba.build_detail(slug, vo.get("identity_name") or name, release, mat)
    # ③ 本人性が変わっていないか
    out["problems"] += identity_same(ident, machine.get("identity") or {})
    # ④ 材料は増えるだけか（IDの増減と、載っている文の両方で見る）
    #   ★右は「今夜の判定書」ではなく「今夜の材料」★（2026-08-23・台帳#461）
    out["problems"] += claims_grew(cur.get("page_decision"), mat)
    dp = _detail_path(slug)
    old_detail = _sj.read_json(dp, expect=dict) if os.path.isfile(dp) else {}
    lost = text_kept(old_detail, detail)
    out["problems"] += lost
    # ★ここまで問題ゼロなら「最後まで読み比べられた」★（依頼190のP1④）
    #   その上で nothing_new だけが立つ＝「調べたが足すものが無い」。
    #   このときは基準を進めてよい（次回の様子見が効く）。
    _clean = not out["problems"]
    _nn = nothing_new(cur.get("page_decision"), machine.get("page_decision"),
                      old_detail, detail)
    # ★★やることがあるかの判断は `growth_reasons` の1か所★★
    #   （2026-09-03。★ここに埋めていたら、壊し方の道具が
    #     「この守りは試験で守られていません」と言った★）
    _nn = growth_reasons(_nn, cur, old_detail, machine)
    out["problems"] += _nn
    out["nothing_new_only"] = bool(_clean and _nn)
    # ★「前に載っていた内容が再現できない」は人へ回す★（黙って止め続けない）
    #
    # ★★ただし、まだ導入されていない機種では積まない★★
    #   （2026-08-21・台帳#452）
    #   ★なぜか★＝導入前の機種は、名鑑がまだ「調査中・準備中」の段階で、
    #   ★日によって載ったり載らなかったりする★。
    #   一度は載っていた機械割が今日は消えている、というのは
    #   出典側の都合で普通に起こる。
    #   ＝★人が出典を見に行っても「まだ載っていない」としか分からない★
    #     （CLAUDE.md の実例＝L転生王女は導入1か月半前で
    #       どの出典も「調査中・準備中」だった）。
    #
    #   実測（2026-08-21）＝新台経路10機種のうち★8機種が導入前★。
    #   このまま積むと、毎朝この案件が増えて台帳が埋まる。
    #
    #   ★導入日を過ぎても再現できないなら、それは本当に人が読む話★なので積む。
    #   ★止めること自体は変わらない★＝積まないだけで、
    #     「消える更新は書かない」という守りはそのまま働く。
    if lost or any("消えます" in p for p in out["problems"]):
        _rel = str((cur or {}).get("release_date") or "")
        _today_ymd = _dt.datetime.now().strftime("%Y-%m-%d")
        # ★「まだ導入されていない」と言い切れるときだけ見送る★
        #   月だけ（2026-09）なら、その月の末日までは導入前とみなす。
        _not_yet = False
        if len(_rel) == 10 and _rel > _today_ymd:
            _not_yet = True
        elif len(_rel) == 7 and _rel > _today_ymd[:7]:
            _not_yet = True
        if _not_yet:
            out.setdefault("notes", []).append(
                f"まだ導入されていないので台帳へは積みません（登場 {_rel}）")
        else:
            # ★★人ではなく2AIへ回す★★（2026-08-27・運営者の指示）
            #   ★直す前は、その場で台帳へ積んでいた★＝人が来るまで止まったまま。
            #   1〜2回目は2AIに聞く。3回目でどうしても決まらなければ報告する。
            _why = " / ".join(
                lost + [p for p in out["problems"] if "消えます" in p])[:900]
            _act = grow_result(slug, False, _why)
            if _act["do"] == "ask":
                out.setdefault("questions", []).append({
                    "text": _act["text"], "kind": "grow_stuck",
                    "slug": slug, "round": _act["round"]})
                out.setdefault("notes", []).append(
                    f"2AIに聞きます（{_act['round']}回目・台帳へは積みません）")
            else:
                ledger_once(
                    slug,
                    "確認済みだった内容を再現できません（育てる処理を止めています）",
                    _act["detail"])
    if out["problems"]:
        return out
    try:
        out["now"] = _pdz.machine_class(machine)
    except _pdz.DecisionError as e:
        out["problems"].append(f"新しい判定書が壊れています: {e}")
        return out
    # ★★うまく育ったので、行き詰まりの回数を0に戻す★★（2026-08-27）
    #   ★昔の失敗をいつまでも数えない★（数え続けると、次に1回詰まっただけで
    #   すぐ人への報告になってしまう）。
    grow_result(slug, True)
    out["machine"], out["detail"] = machine, detail
    return out


def _replace_row(rows: list, machine: dict) -> list:
    return [machine if m.get("slug") == machine["slug"] else m for m in rows]


def _row_fingerprint(slug: str) -> str:
    """いまの一覧に入っているその機種の姿（計画時と変わっていないか見る）。"""
    import hashlib
    cur = next((m for m in _read_rows() if m.get("slug") == slug), None)
    return hashlib.sha256(
        json.dumps(cur, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _file_sha(path: str) -> str:
    import hashlib
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def refine_release_date(slug: str, old: str, new: str,
                        machines_path: str = None) -> tuple:
    """★導入日が「月だけ」→「日まで」になったら、登録も直す★
       （2026-08-29・運営者の指示）

    ★返すもの★＝(直したか, 理由)

    ★値を作らない★＝DMMが持っている値をそのまま写すだけ。
    ★同じ月の中で細かくなるときだけ★（`release_refined` が形を見る）。
    ★逆（日まで→月だけ）は通さない★＝分かっていたことが分からなくなる。

    ★★呼ぶ側の約束★★（Codexの指摘・2026-08-29）
      ・`--apply` のときだけ呼ぶ（★下見では呼ばない★）
      ・`apply_one` と同じ鍵の中で呼ぶ（同時に走る処理の更新を消さない）
      ・★計画（plan_one）より前に呼ぶ★＝あとで呼ぶと、
        計画時の指紋と食い違って `apply_one` が必ず止まる

    ★★なぜ育成とは別にやるのか★★
      育成の中にも日を細かくする経路はあるが、
      ★育成が最後まで成功したときしか通らない★。
      実測（2026-08-29）＝ssb1 は「名鑑の個別ページが1件しか無い」で
      手前で止まるため、★何日経っても日付が入らない★。
      その間ずっと「3日おき」で、
      ★いちばん解析が出る時期に確認が3分の1になっていた★。
    """
    if not release_refined(old, new):
        return False, ""
    path = machines_path or MACHINES
    try:
        rows = _sj.read_json(path, expect=list)
    except Exception as e:                          # noqa: BLE001
        return False, f"機種一覧を読めません: {type(e).__name__}"
    hit = [m for m in rows
           if isinstance(m, dict) and m.get("slug") == slug]
    if len(hit) != 1:
        return False, f"機種一覧に {slug} が1件だけありません（{len(hit)}件）"
    if str(hit[0].get("release_date") or "") != old:
        # ★読んだあとに変わっていたら触らない★
        return False, "確かめたときと登録が違います"
    # ★★導入日は2か所ある。両方そろえる★★（2026-08-29・Codexの2回目の指摘）
    #   ★片方だけ書くと、次の計画が
    #     「登録済みの登場年月が食い違っています」で★必ず止まる★★
    #   （実際にそうしてしまい、ssb1 が止まる状態になった）。
    ident = hit[0].get("identity")
    if not isinstance(ident, dict):
        return False, "身元の記録がありません"
    if str(ident.get("market_release_date") or "") != old:
        return False, (f"身元の記録の登場年月が違います"
                       f"（{ident.get('market_release_date')!r}）")
    hit[0]["release_date"] = new
    ident["market_release_date"] = new
    try:
        _pub.write_atomic(path, json.dumps(rows, ensure_ascii=False,
                                           indent=1) + "\n")
    except Exception as e:                          # noqa: BLE001
        return False, f"機種一覧を書けません: {type(e).__name__}"
    return True, f"導入日を細かくしました（{old} → {new}）"


def apply_one(got: dict) -> dict:
    """育てた結果を書き込む（★全部そろうか、何も残さないか★）。

    ★書く直前にもう一度確かめる★（2026-08-05・Codex102回目の指摘4）
      計画してから書くまでの間に取得を挟むので、その間に別の処理が
      同じ機種を書き換えていることがある。古い計画で上書きしないよう、
      **一覧の中身・検索方針・台帳・途中の目印**を書く直前に見直す。

    ★同時に2つ走らせない★＝新規公開と同じ鍵（`publish_new_machine` のロック）
    ★途中で落ちても分かるように目印を残す★＝`.publish-in-progress.json`
    """
    slug = got["slug"]
    machine, detail = got["machine"], got["detail"]
    out = {"slug": slug, "problems": [], "wrote": [], "was": got["was"],
           "now": got["now"]}
    # ★指紋が無い計画では何もしない★（先に断る＝無駄な描画もしない）
    if not got.get("fingerprint"):
        out["problems"].append("計画時の指紋がありません（書きません）")
        return out
    # ★先に一度そろえて見る★（本番の判定は鍵の中でもう一度やる。
    #   ここで落とすのは「明らかに古い計画」を早く断るため）
    # ★「無い」も指紋のうち★（後から現れた／消えたのどちらも食い違わせる）
    stale = [p for p, want in got["fingerprint"].items()
             if (_file_sha(p) if os.path.isfile(p) else "") != want]
    if stale:
        out["problems"].append(
            "計画したときから中身が変わっています: "
            + ", ".join(os.path.relpath(x, BASE) for x in stale[:3]))
        return out
    indexable = got["now"] == "AUTO_INDEXABLE"
    html = _pub.render(slug, machine, detail)
    # ★書く前の検査（新規公開と同じものを使う）★
    out["problems"] += _pub.check_detail(slug, detail)
    out["problems"] += _pub.check_machine(slug, machine)
    out["problems"] += _pub.check_page(slug, html, expect_noindex=not indexable,
                                       detail=detail)
    out["problems"] += _pub.check_only_allowed_values(slug, machine, detail, html)
    out["problems"] += _pub.run_site_audit()
    if out["problems"]:
        return out

    page = _pub._page_path(slug)
    dp = _detail_path(slug)
    hubs_dst = [os.path.join(BASE, rel) for rel in _pub.HUB_FILES]
    watch = [page, dp, MACHINES, SITEMAP] + hubs_dst

    with _pub._OnlyOne():                  # ★同時に2つ走らせない★
        # ── 書く直前の再確認
        before_fp = got["fingerprint"]
        for p, want in before_fp.items():
            if (_file_sha(p) if os.path.isfile(p) else "") != want:
                out["problems"].append(
                    f"計画したときから中身が変わっています: "
                    f"{os.path.relpath(p, BASE)}")
        again = plan_one(slug, gather=lambda *a, **k: {"material": None,
                                                       "problems": []},
                         verify=lambda *a, **k: {"problems": [], "release": ""})
        # ↑ 取得はやり直さない。前提（区分・方針・台帳・目印）だけ見直す
        for p in again["problems"]:
            if "材料を集められません" in p or "本人性" in p:
                continue
            out["problems"].append(f"書く直前の確認で止めました: {p}")
        if out["problems"]:
            return out

        keep = {}
        for p in watch:
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    keep[p] = f.read()

        def _restore() -> list:
            bad = []
            for p, b in keep.items():
                try:
                    _pub.write_atomic(p, b.decode("utf-8"))
                except Exception as e:    # noqa: BLE001
                    bad.append(f"{os.path.relpath(p, BASE)}（{e}）")
            return bad

        # ★戻し方つきの目印★（電源が落ちても残る・新規公開と同じ場所）
        _pub.mark_start(slug, machine,
                        {p: b.decode("utf-8") for p, b in keep.items()})
        try:
            rows = _replace_row(_read_rows(), machine)
            _pub.write_atomic(dp, json.dumps(detail, ensure_ascii=False,
                                             indent=1) + "\n")
            _pub.write_atomic(page, html)
            # ★indent=1★ 他の書き手（publish_new_machine 等）と必ずそろえる。
            #   2にすると中身が同じでも全行が書き換わり、実際の差分が埋もれる
            _pub.write_atomic(MACHINES,
                              json.dumps(rows, ensure_ascii=False, indent=1) + "\n")
            sm = keep[SITEMAP].decode("utf-8")
            sm2 = _pub.add_to_sitemap(sm, slug) if indexable \
                else _pub.remove_from_sitemap(sm, slug)
            if sm2 != sm:
                _pub.write_atomic(SITEMAP, sm2)
            # ★早見表も実際に書き直す★（2026-08-05・Codex102回目の指摘5。
            #   `build_hubs()` は描くだけで書かないのに、返り値の problems を
            #   見て終わりにしていた＝一覧が古いまま残る）
            built = _pub.build_hubs()
            for rel, page_html in built.items():
                full = os.path.join(BASE, rel)
                if not os.path.isfile(full) or \
                        open(full, encoding="utf-8").read() != page_html:
                    _pub.write_atomic(full, page_html)
                    out["wrote"].append(full)
            # ★書いたあとにもう一度そろっているか見る★
            # ★自分が置いた「途中」の目印は、この監査では無視する★
            #   （目印はまさに今この処理が付けたもの。ここで拾うと必ず失敗する）
            after = (_pub.run_site_audit(ignore_in_progress=True)
                     + _pub.check_hubs_untouched())
            if indexable:
                after += _pub.check_sitemap_added(sm, slug)
            if after:
                raise GrowError(" / ".join(after)[:300])
            out["wrote"] = [dp, page, MACHINES] + \
                ([SITEMAP] if sm2 != sm else []) + out["wrote"]
        except BaseException as e:        # noqa: BLE001
            bad = _restore()
            out["problems"].append(f"書き込みを取り消しました: {e}")
            out["wrote"] = []
            if bad:
                out["problems"].append("★元に戻せなかったファイルがあります: "
                                       + " / ".join(bad)
                                       + "／目印は残します★")
                return out                # ★戻せていないので目印を消さない★
            _pub.mark_done()
            return out
        _pub.mark_done()                   # ★全部そろってから消す★
    return out


# ------------------------------------------------------------------ selftest

def selftest() -> int:
    ok, ran = True, [0]

    # ★★自己テストは本番の台帳に書かない★★（2026-08-11・台帳#310/#311/#320）
    #   ここは plan_one を偽の材料で呼ぶので、必ず「前に載っていた内容が消える」
    #   判定になり、そのたび ledger_once が**本番の台帳へ登録**していた
    #   （実際にごみが3件入った: s9 / s8 / garei_zero_re）。
    #   ★呼ぶ側が読んでいる実体を差し替える★＝このファイルを直接動かすと
    #   自分は __main__ になるため、名前で取り直す。
    import tempfile
    import open_issues as _oi_mod
    _keep_ledger, _tmp_dir = _oi_mod.DEFAULT_FILE, tempfile.mkdtemp()
    _oi_mod.DEFAULT_FILE = _oi_mod.Path(_tmp_dir) / "issues.json"

    # ★★自己試験は外へ出ない★★（2026-08-21・台帳#419／CIが2回落ちた原因）
    #   `plan_one` は `find` を渡さないと `find_sources()` を呼び、
    #   その先で `directory_index.find` → `new_machine_watch._get` と進んで
    #   **本物の名鑑を取りに行く**（取得間隔の待ちで止まる）。
    #   ★CI（通信できない）では必ず落ちる★／★手元では数分待たされて完走しない★。
    #   `plan_one` の呼び出しが9か所あり、毎回 `find=` を書くのは漏れるので、
    #   ★出どころの `find_sources` 自体を試験の間だけ差し替える★。
    #   ★本番の姿は変えていない★（この関数を抜けたら必ず元へ戻す）。
    _keep_find = globals()["find_sources"]
    globals()["find_sources"] = lambda machine: []

    def _ceil_box(lines):
        return {"slug": "zzz",
                "sections": [{"title": "天井・恩恵", "body": lines}]}

    def t(name, cond):
        nonlocal ok
        ran[0] += 1
        print(("✅ " if cond else "❌ ") + name)
        ok = ok and bool(cond)

    # ★右は「今夜の材料」★（2026-08-23・台帳#461で引数の意味を変えた）
    _RNG = {"value": {"low": 97, "high": 110}}
    _G50 = {"value": {"games": 36.1}}
    _MAT_A = {"adopted": {"payout_range": _RNG}}
    _MAT_AB = {"adopted": {"payout_range": _RNG, "games_per_50": _G50}}
    t("★★確認済みの事実が消える更新は拒否する★★",
      claims_grew({"claims": ["payout_range", "games_per_50"]}, _MAT_A)
      and "消えます" in claims_grew(
          {"claims": ["payout_range", "games_per_50"]}, _MAT_A)[0])
    t("　増えていれば通る",
      not claims_grew({"claims": ["payout_range"]}, _MAT_AB))
    # ★★型式名は消えても止めない★★（2026-08-23・台帳#461）
    #   ★これが無いと、型式名を濃さから外した翌朝に全機種が止まる★
    t("★★もう作らない claim（型式名）の消失だけでは止めない★★",
      not claims_grew({"claims": ["model_code", "payout_range"]}, _MAT_A))
    t("　型式名以外が消えていれば、やはり止める",
      claims_grew({"claims": ["model_code", "games_per_50"]}, _MAT_A))
    # ★★2AIで確定させた値は「消えた」にならない★★
    #   ★直す前の姿★＝濃さ一覧どうしを比べていたので、
    #   2AIが確定させるほど「事実が消えた」と判定された（喰霊-零-Reの実例）。
    _MAT_CV = {"adopted": {"payout_range": {**_RNG,
                                            "_from": "confirmed_values"}}}
    t("★★2AIの確定値しか無くても「消えた」とは言わない★★",
      not claims_grew({"claims": ["payout_range"]}, _MAT_CV))
    t("　★根拠が刻まれていなければ数えない★（白名簿・印だけでは通さない）",
      _pdz.index_claims_from_material(_MAT_CV) == [])
    # ★★書けるものは「消えた」ことにしない★★（2026-08-27・運営者の判断）
    #   確認済みの設定別の値から範囲を書けるなら、その事実は失われていない。
    _MAT_RATE = {"adopted": {"payout_rate": {"value": {"1": "97.0%",
                                                       "6": "109.4%"}}}}
    t("★★設定別の値から範囲を書けるなら「消えた」と言わない★★",
      not claims_grew({"claims": ["payout_range"]}, _MAT_RATE))
    t("　書けないとき（値が1つだけ）は、いままでどおり止める",
      claims_grew({"claims": ["payout_range"]},
                  {"adopted": {"payout_rate": {"value": {"1": "97.0%"}}}}))
    # ── 前に載っていた内容が消える/変わる更新を止める（Codex102/103回目）
    OLD = {"lead": "この機種の紹介です。",
           "factTable": [["型式名", "L機/1"], ["機械割", "97.0%〜110.0%"]],
           "summaryBoxes": [{"label": "天井", "value": "800G"}],
           "sections": [
               {"title": "基本スペック", "body": [
                   "**型式名**：L機/1",
                   f"**50枚あたりのゲーム数**：{_ba.PENDING_ITEM}"]},
               {"title": "確認できたCZ", "tables": [
                   {"label": "出典2件で確認できたCZ",
                    "headers": ["CZ", "継続", "契機"],
                    "rows": [["喰霊チャンス", "10G", "レア役"]]}]},
               {"title": "天井・恩恵", "body": [_ba.PENDING_TEXT]}]}

    def _mod(f):
        import copy
        d = copy.deepcopy(OLD)
        f(d)
        return d

    t("　同じ内容なら通る", not text_kept(OLD, _mod(lambda d: None)))
    t("　中身を足すのは通る",
      not text_kept(OLD, _mod(lambda d: d["sections"][0]["body"].append("**純増**：2.8枚"))))
    t("★★段落の値を書き換えたら止める★★",
      text_kept(OLD, _mod(lambda d: d["sections"][0]["body"].__setitem__(
          0, "**型式名**：L機/2"))))
    t("★★表の値だけ書き換えても止める★★（Codex103回目・段落だけ見ていた）",
      text_kept(OLD, _mod(lambda d: d["sections"][1]["tables"][0]["rows"]
                          .__setitem__(0, ["喰霊チャンス", "20G", "レア役"]))))
    t("★★factTableの値を書き換えても止める★★",
      text_kept(OLD, _mod(lambda d: d["factTable"].__setitem__(
          1, ["機械割", "99.0%〜115.0%"]))))
    t("★★まとめ箱の書き換えは止める★★",
      text_kept(OLD, _mod(lambda d: d["summaryBoxes"][0].__setitem__(
          "value", "999G"))))
    # ★★導入文はここでは比べない★★（2026-08-23・台帳#461で変更）
    #   ★守りを外したのではなく、守る場所を1つにした★＝
    #   導入文は「機種名」と「登場時期」をはめ込んだ定型文で、
    #   独自の事実を持たない。両方とも下の2つが止めるので、
    #   ここで比べると**書き方を変えた日に二重に止める**だけになる。
    t("　導入文の文言そのものは、ここでは止めない",
      not text_kept(OLD, _mod(lambda d: d.__setitem__(
          "lead", "別の紹介文。登場時期は2026年8月17日です。"))))
    t("★★その代わり、機種名が変わったら本人性の検査が止める★★",
      identity_same({"announced_name": "Lパチスロ A"},
                    {"announced_name": "Lパチスロ B"}))
    t("★★登場年月が変わったら止まる（細かくなるのだけ許す）★★",
      release_refined("2026-08", "2026-08-17")
      and not release_refined("2026-08", "2026-09-01")
      and not release_refined("2026-08-17", "2026-08"))
    # ★★導入日が「月だけ」→「日まで」になったら、登録も直す★★
    #   （2026-08-29・運営者の指示）
    #   ★これが無くて、いちばん解析が出る時期に確認が3分の1になっていた★
    #   （月精度は3日おき／日が入れば毎日）。
    import tempfile as _tf_rr
    _d_rr = _tf_rr.mkdtemp()
    _p_rr = os.path.join(_d_rr, "machines.json")

    def _mk_rr(rel, ident_rel=None):
        io.open(_p_rr, "w", encoding="utf-8").write(json.dumps(
            [{"slug": "a", "release_date": rel,
              "identity": {"market_release_date":
                           rel if ident_rel is None else ident_rel}},
             {"slug": "b", "release_date": "2026-07-01",
              "identity": {"market_release_date": "2026-07-01"}}],
            ensure_ascii=False))

    def _rel_rr(slug="a"):
        rows = json.load(io.open(_p_rr, encoding="utf-8"))
        return [r for r in rows if r["slug"] == slug][0].get("release_date")

    def _idt_rr(slug="a"):
        rows = json.load(io.open(_p_rr, encoding="utf-8"))
        r = [r for r in rows if r["slug"] == slug][0]
        return (r.get("identity") or {}).get("market_release_date")

    _mk_rr("2026-09")
    _ok_rr, _ = refine_release_date("a", "2026-09", "2026-09-07", _p_rr)
    t("★★日が分かったら、登録も日まで直す★★"
      "／★直らないと『新台期間は毎日確認』が効かない★",
      _ok_rr and _rel_rr() == "2026-09-07")
    t("★★導入日は2か所ある。両方そろえる★★"
      "／★片方だけ書くと、次の計画が食い違いで必ず止まる★"
      "（実際にそうしてしまった）",
      _idt_rr() == "2026-09-07")
    t("　★他の機種は触らない★",
      _rel_rr("b") == "2026-07-01" and _idt_rr("b") == "2026-07-01")
    _mk_rr("2026-09", ident_rel="2026-08")
    _o_mis, _w_mis = refine_release_date("a", "2026-09", "2026-09-07", _p_rr)
    t("★★2か所が食い違っていたら、1文字も書かない★★",
      _o_mis is False and "身元の記録の登場年月が違います" in _w_mis
      and _rel_rr() == "2026-09" and _idt_rr() == "2026-08")
    _mk_rr("2026-09-07")
    t("★★日まで→月だけ、は通さない★★（分かっていたことが分からなくなる）",
      refine_release_date("a", "2026-09-07", "2026-09", _p_rr)[0] is False
      and _rel_rr() == "2026-09-07")
    _mk_rr("2026-09")
    t("★★月が変わる書き換えは通さない★★（別の日付を作らない）",
      refine_release_date("a", "2026-09", "2026-10-07", _p_rr)[0] is False
      and _rel_rr() == "2026-09")
    _mk_rr("2026-09")
    _o6, _w6 = refine_release_date("a", "2026-08", "2026-08-17", _p_rr)
    t("★★確かめたときと登録が違えば、触らない★★"
      "（読んだあとに変わっていた場合）",
      _o6 is False and "登録が違います" in _w6 and _rel_rr() == "2026-09")
    _o7, _w7 = refine_release_date("zzz", "2026-09", "2026-09-07", _p_rr)
    t("　★一覧に無い機種は触らない★",
      _o7 is False and "1件だけありません" in _w7)

    # ★★Codexの指摘3件を塞いだことを、形で確かめる★★（2026-08-29）
    #   ★1回目の作りは、下見で本番データを書き換えた★（実際にやった）
    _src_plan = inspect.getsource(plan_one)
    _src_main = inspect.getsource(_main)
    _before = _src_main.split("refine_release_date")[0][-500:]
    t("★★計画は書かない（予定を返すだけ）★★"
      "／★書くと計画時の指紋と食い違い、育成が必ず止まる★",
      "release_refine" in _src_plan
      and "refine_release_date" not in _src_plan)
    t("★★下見では書かない★★／★1回目の作りは本番データを書き換えた★",
      "a.apply" in _before)
    t("★★書くのは鍵の中★★（同時に走る処理の更新を消さない）",
      "_OnlyOne" in _before)
    import shutil as _sh_rr
    _sh_rr.rmtree(_d_rr, ignore_errors=True)

    t("★★項目ごとの「未確認」を埋める更新は通す★★（Codex103回目・正しい更新を拒んでいた）",
      not text_kept(OLD, _mod(lambda d: d["sections"][0]["body"].__setitem__(
          1, "**50枚あたりのゲーム数**：約32G"))))
    t("　箱の「未確認です」を中身に差し替えるのも通る",
      not text_kept(OLD, _mod(lambda d: d["sections"][2]["body"].__setitem__(
          0, "通常時800Gで天井に到達します。"))))
    t("★★同じ表の行が減ったら止める★★",
      text_kept(OLD, _mod(lambda d: d["sections"][1]["tables"][0]
                          .__setitem__("rows", []))))
    # ★★2026-08-12に廃止した書き方（台帳#461）★★
    #   ★これが無いと8/12より前の3記事は永久に育たない★
    _RUMOR_LINE = _ba.RUMOR_SECTION["body"][0]
    _with_rumor = _mod(lambda d: d["sections"].append(
        {"title": "噂・未確定情報", "type": "rumor", "body": [_RUMOR_LINE]}))
    t("★★空の噂の箱の決まり文句は、消えてよい★★",
      not text_kept(_with_rumor, OLD))
    t("★★本物の噂の本文が消えるのは、今までどおり止める★★",
      text_kept(_mod(lambda d: d["sections"].append(
          {"title": "噂・未確定情報", "type": "rumor",
           "body": ["**噂・公式未確認**：設定6は毎ゲームBGM変化との噂があります。"]})),
          OLD))
    t("　似ているだけの文は免除しない（完全一致だけ）",
      text_kept(_mod(lambda d: d["sections"].append(
          {"title": "噂・未確定情報", "type": "rumor",
           "body": [_RUMOR_LINE.replace("掲載しません", "掲載しません。※追記")]})),
          OLD))
    # ★登場時期の書き方が変わっても止めない★（中身は前より正確になっている）
    _old_rel = _mod(lambda d: d["sections"][0]["body"].insert(
        0, "**登場時期**：2026年8月（公式確認）"))
    _new_rel = _mod(lambda d: d["sections"][0]["body"].insert(
        0, "**登場時期**：2026年8月17日"))
    t("★★登場時期の書き方が変わっただけでは止めない★★",
      not text_kept(_old_rel, _new_rel))
    # ★★頭がズレたら気づく★★（build_new_article が書く形と揃っているか）
    _spec = [s for s in _ba.build_detail(
        "zzz", "試験機", "2026-08-17",
        {"adopted": {}, "ceilings": {"adopted": []},
         "at_specs": {"adopted": []}, "czs": {"adopted": []},
         "setting_hints": {"adopted": []}}).get("sections", [])
        if s.get("title") == "基本スペック"]
    # ★2026-09-01に基本スペックを表へ変えた★ので、表の行で見る
    def _spec_rows_of(sec):
        out = []
        for tb in (sec.get("tables") or []):
            if [str(x) for x in (tb.get("headers") or [])] == ["項目", "内容"]:
                out += [r for r in (tb.get("rows") or [])
                        if isinstance(r, (list, tuple)) and len(r) == 2]
        return out

    t("★★登場時期の行が、実際の生成物にある★★",
      bool(_spec) and any(str(r[0]).strip() == "登場時期"
                          for r in _spec_rows_of(_spec[0])))
    # ★★比べない、と決めた前提そのものを固定する★★（2026-08-23・Codexの指摘）
    #   ★なぜ要るか★＝登場時期の行と導入文を比較から外した根拠は
    #   「どちらも機種名と登場時期しか入っていない定型文だから」。
    #   ★将来その行に別の事実が足されたら、根拠が崩れるのに誰も気づかない★
    #   （頭の一致だけを見る試験では通ってしまう）。
    #   ＝**中身が定型どおりであること**を毎回確かめる。
    def _built(rel):
        return _ba.build_detail(
            "zzz", "試験機", rel,
            {"adopted": {}, "ceilings": {"adopted": []},
             "at_specs": {"adopted": []}, "czs": {"adopted": []},
             "setting_hints": {"adopted": []}})

    def _rel_lines(d):
        """登場時期の行（★表になったので、本文と同じ形に直して返す★）。"""
        for s in (d.get("sections") or []):
            if s.get("title") != "基本スペック":
                continue
            out = [str(b) for b in (s.get("body") or [])
                   if str(b).startswith(RELEASE_LINE_PREFIX)]
            for r in _spec_rows_of(s):
                if str(r[0]).strip() == "登場時期":
                    out.append(f"{RELEASE_LINE_PREFIX}{r[1]}")
            return out
        return []

    # ★★期待する文は、生成する側の定数から作らない★★（2026-08-23）
    #   ★対照実験で判明した★＝はじめ `_ba.LEAD_TEMPLATE.format(...)` と
    #   比べていたので、**テンプレートに事実を足すと両辺が一緒に動いて**
    #   試験が絶対に落ちなかった（導入文に「天井は999Gです。」を足しても
    #   終了コード0・❌0件）。＝自分で自分を採点していた。
    #   ★実際の文字列をここに書く★＝文言を変えたらここも落ちるので、
    #   「比べないと決めた前提がまだ成り立つか」を人が見直すことになる。
    _WANT = {
        "2026-08-17": ("**登場時期**：2026年8月17日",
                       "試験機の機種情報ページです。登場時期は2026年8月17日です。"),
        "2026-08": ("**登場時期**：2026年8月頃",
                    "試験機の機種情報ページです。登場時期は2026年8月頃です。"),
    }
    _ok_rel = True
    _ok_lead = True
    for _rel, (_want_line, _want_lead) in _WANT.items():
        _d = _built(_rel)
        if _rel_lines(_d) != [_want_line]:     # ★1行だけ・中身もそのまま★
            _ok_rel = False
        if _d.get("lead") != _want_lead:
            _ok_lead = False
    t("★★登場時期の行は1行だけで、登場時期しか入らない★★", _ok_rel)
    t("★★導入文は機種名と登場時期だけ（別の事実が混ざっていない）★★", _ok_lead)
    t("　登場時期が無いときの導入文も定型どおり",
      _built("").get("lead")
      == "試験機のページです。登場時期は当サイトでは確認できていません。")
    # ── ★本物の build_detail で、暫定表現が埋まる更新を通す★（Codex104回目）
    def _stamp_basis(m):
        """★試験の材料に根拠を足す★（2026-08-24）

        ★ここの試験が見ているのは「育てる判断」であって根拠ではない★。
        本番の材料は抽出器が必ず根拠を入れる（adoption_basis の通し試験が
        本物の抽出器4つで確かめている）ので、ここでは形だけ合わせる。
        ★根拠そのものの守りは、そちらとミューテーション試験が見る★。
        """
        for box in ("ceilings", "at_specs", "czs"):
            for row in ((m.get(box) or {}).get("adopted") or []):
                if isinstance(row, dict):
                    row.setdefault("basis", "INDEPENDENT_MULTI")
                    for k in ("games", "rate"):
                        if row.get(k) not in (None, "", []):
                            row.setdefault(k + "_basis", "INDEPENDENT_MULTI")
        for k, row in ((m.get("adopted") or {}).items()):
            if isinstance(row, dict) and k not in _pdz.RETIRED_CLAIMS:
                row.setdefault("basis", "INDEPENDENT_MULTI")
        return m

    def _mat(**kw):
        m = {"adopted": {"model_code": {"value": "L機/1", "sources": ["a", "b"]}},
             "need_third": {}, "thin": {},
             "ceilings": {"adopted": [], "need_third": []},
             "at_specs": {"adopted": [], "need_third": []},
             "czs": {"adopted": [], "need_third": []},
             "setting_labels_seen": [], "setting_labels_unconfirmed": []}
        m.update(kw)
        return _stamp_basis(m)

    cz_thin = _mat(czs={"adopted": [{"name": "喰霊チャンス",
                                     "basis": "INDEPENDENT_MULTI",
                                     "sources": ["a", "b"]}], "need_third": []})
    cz_full = _mat(czs={"adopted": [{"name": "喰霊チャンス", "games": 10,
                                     "basis": "INDEPENDENT_MULTI",
                                     "games_basis": "INDEPENDENT_MULTI",
                                     "sources": ["a", "b"]}], "need_third": []})
    d_thin = _ba.build_detail("x", "L機", "2026-08", cz_thin)
    d_full = _ba.build_detail("x", "L機", "2026-08", cz_full)
    t("★★CZの「確認中」が中身に変わる更新を通す★★（Codex104回目・拒否していた）",
      not text_kept(d_thin, d_full))
    t("　逆に、確認できていた内容が「確認中」へ戻る更新は止める",
      bool(text_kept(d_full, d_thin)))
    # ★claimが増えなくても、欄の中身が確定すれば育てる★（Codex107回目・再現した）
    def _m2(**kw):
        base = {"adopted": {"model_code": {"value": "L/1", "sources": ["a", "b"]},
                            "payout_range": {"value": {"low": 97.0, "high": 110.0},
                                             "sources": ["a", "b"]}},
                "need_third": {}, "thin": {},
                "ceilings": {"adopted": [], "need_third": []},
                "at_specs": {"adopted": [{"mode": "MAIN_AT", "games": 30,
                                          "net": 2.8, "sources": ["a", "b"]}],
                             "need_third": []},
                "czs": {"adopted": [], "need_third": []},
                "setting_labels_seen": [], "setting_labels_unconfirmed": []}
        base.update(kw)
        return _stamp_basis(base)

    _thin = _m2(czs={"adopted": [{"name": "CZ-A", "sources": ["a", "b"]}],
                     "need_third": []})
    _rich = _m2(czs={"adopted": [{"name": "CZ-A", "games": 10, "rate": "50%",
                                  "sources": ["a", "b"]}], "need_third": []})
    _mt, _mr = (_ba.build_machine("x", "L機", "m", "https://e/x/", "2026-08", z)
                for z in (_thin, _rich))
    _dt, _dr = (_ba.build_detail("x", "L機", "2026-08", z)
                for z in (_thin, _rich))
    t("★★claimが増えなくても、欄の中身が確定すれば育てる★★（Codex107回目）",
      _mt["page_decision"]["claims"] == _mr["page_decision"]["claims"]
      and not nothing_new(_mt["page_decision"], _mr["page_decision"], _dt, _dr))
    t("　中身が何も増えていなければ育てない",
      bool(nothing_new(_mt["page_decision"], _mt["page_decision"], _dt, _dt)))
    t("　確定した中身が減る方向は（別の検査で）止まる", bool(text_kept(_dr, _dt)))
    # ★1つの欄に確定と未確定が同居する場合★（Codex105回目・再現した）
    cz_mix = _mat(czs={"adopted": [{"name": "CZ-A", "games": 10,
                                    "rate_disputed": True,
                                    "sources": ["a", "b"]}], "need_third": []})
    cz_changed = _mat(czs={"adopted": [{"name": "CZ-A", "games": 20,
                                        "rate": "50%",
                                        "sources": ["a", "b"]}], "need_third": []})
    cz_kept = _mat(czs={"adopted": [{"name": "CZ-A", "games": 10,
                                     "rate": "50%",
                                     "sources": ["a", "b"]}], "need_third": []})
    d_mix = _ba.build_detail("x", "L機", "2026-08", cz_mix)
    t("★★同じ欄の中でも、確定していた部分の書き換えは止める★★（継続10→20）",
      bool(text_kept(d_mix, _ba.build_detail("x", "L機", "2026-08", cz_changed))))
    t("★★同じ欄の未確定の部分だけが埋まる更新は通す★★（継続10のまま期待度が確定）",
      not text_kept(d_mix, _ba.build_detail("x", "L機", "2026-08", cz_kept)))
    def _cz(**kw):
        return _mat(czs={"adopted": [dict({"name": "CZ-A",
                                           "sources": ["a", "b"]}, **kw)],
                         "need_third": []})

    def _d(m):
        return _ba.build_detail("x", "L機", "2026-08", m)

    none_ = _cz()                                   # 確認中
    g10 = _cz(games=10)                             # 継続10
    r50 = _cz(rate="50%")                           # 期待度 50%
    both = _cz(games=10, rate="50%")                # 継続10 ／ 期待度 50%
    mix = _cz(games=10, rate_disputed=True)         # 継続10 ／ 未確定
    changed = _cz(games=20, rate="50%")             # 継続20 ／ 期待度 50%
    t("★★確認中 → 継続10 ／ 期待度50% は通る★★（Codex106回目・拒否していた）",
      not text_kept(_d(none_), _d(both)))
    t("★★継続10 → 継続10 ／ 期待度50% は通る★★（同じ欄に増えるのは正しい更新）",
      not text_kept(_d(g10), _d(both)))
    t("★★期待度50% → 継続10 ／ 期待度50% は通る★★",
      not text_kept(_d(r50), _d(both)))
    t("★★継続10 ／ 未確定 → 継続20 ／ 期待度50% は止める★★",
      bool(text_kept(_d(mix), _d(changed))))
    t("　欄の中身が減る更新は止める（継続10 ／ 期待度50% → 継続10）",
      bool(text_kept(_d(both), _d(g10))))
    set_thin = _mat(adopted={"model_code": {"value": "L機/1", "sources": ["a", "b"]},
                             "at_prob": {"value": {"1": "1/300"},
                                         "sources": ["a", "b"]}},
                    setting_labels_unconfirmed=[2])
    set_full = _mat(adopted={"model_code": {"value": "L機/1", "sources": ["a", "b"]},
                             "at_prob": {"value": {"1": "1/300", "2": "1/290"},
                                         "sources": ["a", "b"]}})
    t("★★未確認だった設定の値が載る更新を通す★★（注記が変わっても止めない）",
      not text_kept(_ba.build_detail("x", "L機", "2026-08", set_thin),
                    _ba.build_detail("x", "L機", "2026-08", set_full)))
    t("★★同じ設定の値が書き換わる更新は止める★★",
      bool(text_kept(
          _ba.build_detail("x", "L機", "2026-08", set_full),
          _ba.build_detail("x", "L機", "2026-08", _mat(
              adopted={"model_code": {"value": "L機/1", "sources": ["a", "b"]},
                       "at_prob": {"value": {"1": "1/999", "2": "1/290"},
                                   "sources": ["a", "b"]}})))))
    # ── ★指紋は一覧を読む前に取っているか★（順番そのものを見る）
    order = []
    real_sha, real_rows = _file_sha, _read_rows
    try:
        globals()["_file_sha"] = lambda p: (order.append("sha"), real_sha(p))[1]
        globals()["_read_rows"] = lambda: (order.append("rows"), real_rows())[1]
        # ★★試験はネットへ出ない★★（2026-08-20）
        #   出典探しだけ本物のままだったので、rate limit で待たされ
        #   **自己試験そのものがハングして誰も回せなかった**。
        plan_one("garei_zero_re",
                 gather=lambda *a, **k: {"material": None, "problems": []},
                 verify=lambda *a, **k: {"problems": [], "release": ""},
                 find=lambda *a, **k: [])
    finally:
        globals()["_file_sha"], globals()["_read_rows"] = real_sha, real_rows
    t("★★指紋を取り終えてから一覧を読んでいる★★（順番が戻ったら落ちる）",
      "rows" in order and "sha" in order
      and order.index("rows") > max(i for i, x in enumerate(order) if x == "sha"))
    # ── ★2AIで確定した値を、育てる処理も必ず読む★（2026-08-11・台帳#316）
    #    足す場所が新台側の1か所にしか無く、確定値を載せた機種は
    #    「前に載っていた内容が再現できない」と判定されて毎日止まっていた。
    #    ★呼んだかどうかを見る★（文字列を探すだけでは綴り違いも通る）
    seen = []
    the_mat = {"adopted": {}}
    real_merge = _cv.merge_into
    try:
        # ★どの材料に足したかまで見る★（依頼148の指摘4）
        #   slugだけ見ていると、空の辞書へ誤って繋いでも合格してしまう。
        def _spy(mat, slug):
            # ★「集めた材料そのもの」に足しているか★（別の辞書ではないこと）
            seen.append((slug, mat is the_mat))
            return []
        _cv.merge_into = _spy
        plan_one("garei_zero_re",
                 gather=lambda *a, **k: {"material": the_mat, "problems": []},
                 verify=lambda *a, **k: {"problems": [], "release": ""})
    finally:
        _cv.merge_into = real_merge
    t("★★育てる処理も2AIの確定値を読む★★"
      "（読まないと、確定値を載せた機種は毎日『再現できません』で止まる）",
      seen and seen[0] == ("garei_zero_re", True))
    # ★★出典の取り直しも、呼んだかどうかで見る★★（2026-08-24・Codexの18回目）
    #   ★直す前はソースに文字列があるかを見ていた★＝
    #   綴り違いでも、呼ばれない場所に書いてあっても通ってしまう。
    #   ★止まることまで確かめる★＝問題を返したら記事を作らない。
    _rvseen = []
    _real_rv = _cv.reverify
    try:
        def _rvspy(slug_, **kw):
            _rvseen.append((slug_, kw.get("name"), kw.get("official_url")))
            return []
        _cv.reverify = _rvspy
        _cv.merge_into = lambda mat, slug_: ["ceiling"]
        plan_one("garei_zero_re",
                 gather=lambda *a, **k: {"material": the_mat, "problems": []},
                 verify=lambda *a, **k: {"problems": [], "release": ""})
        # ★★「空でない」ではなく「その機種の正しい名前か」を見る★★
        #   （2026-08-24・Codexの19回目）
        #   ★直す前は name が真かどうかしか見ていなかった★ので、
        #   別の機種名を渡しても通った（＝別機種の控えで照合できてしまう）。
        _want_nm = ([r for r in _read_rows()
                     if r.get("slug") == "garei_zero_re"]
                    or [{}])[0].get("name") or ""
        t("★★育てる側も、出典を取り直して確かめる★★"
          "／★ここが抜けると、控えの手書きが記事に出る★",
          _rvseen and _rvseen[0][0] == "garei_zero_re")
        t("　その機種の名前を渡している（別機種の控えで照合させない）",
          bool(_want_nm) and _rvseen and _rvseen[0][1] == _want_nm)
        t("　公式URLも渡している（機種を引き直せるように）",
          _rvseen and _rvseen[0][2])
        # ★★問題を返したら「記事そのものが作られない」ことまで見る★★
        #   ★直す前は問題文があるかしか見ていなかった★ので、
        #   記事を作ったうえで問題も返す、という形でも通った。
        _built = []
        _real_bm, _real_bd = _ba.build_machine, _ba.build_detail
        try:
            _ba.build_machine = lambda *a, **k: _built.append("machine")
            _ba.build_detail = lambda *a, **k: _built.append("detail")
            _cv.reverify = lambda slug_, **kw: ["出典を確かめ直せません"]
            _stop = plan_one(
                "garei_zero_re",
                gather=lambda *a, **k: {"material": the_mat, "problems": []},
                verify=lambda *a, **k: {"problems": [], "release": ""})
        finally:
            _ba.build_machine, _ba.build_detail = _real_bm, _real_bd
        t("　確かめ直せなければ、その理由を返す",
          any("確かめ直せません" in str(x)
              for x in (_stop.get("problems") or [])))
        t("★★確かめ直せなければ、記事を1文字も作らない★★"
          "／★作ってから止めると、次の工程が拾える形で残る★",
          not _built and _stop.get("machine") is None
          and _stop.get("detail") is None)
    finally:
        _cv.reverify = _real_rv
        _cv.merge_into = real_merge

    t("　2AIの確定値も計画の指紋に入っている"
      "（計画のあとに取り消されたら食い違って止まる）",
      _cv.STORE in (plan_one(
          "garei_zero_re",
          gather=lambda *a, **k: {"material": None, "problems": []},
          verify=lambda *a, **k: {"problems": [], "release": ""}
      ).get("fingerprint") or {}))

    # ── ★計画後に中身が変われば、1文字も書かずに止まる★
    wrote = []
    real_write = _pub.write_atomic
    try:
        _pub.write_atomic = lambda *a, **k: wrote.append(a[0])
        bad = apply_one({"slug": "garei_zero_re", "machine": {"slug": "x"},
                         "detail": {}, "was": "AUTO_PENDING",
                         "now": "AUTO_PENDING",
                         "fingerprint": {MACHINES: "ちがう指紋"}})
    finally:
        _pub.write_atomic = real_write
    t("★★計画したときから変わっていたら、1つも書かない★★",
      not wrote and any("変わっています" in p or "計画" in p
                        for p in bad["problems"]))
    # 指紋は「計画で読む前」に取る
    got = plan_one("garei_zero_re", gather=lambda *a, **k: {"material": None,
                                                            "problems": []},
                   verify=lambda *a, **k: {"problems": [], "release": ""})
    t("★★計画結果に、読む前の指紋が入っている★★（同時更新を古い計画で消さない）",
      isinstance(got.get("fingerprint"), dict) and got["fingerprint"])
    t("★★指紋が無い計画では書かない★★",
      any("指紋がありません" in p for p in
          apply_one({"slug": "x", "machine": {}, "detail": {},
                     "was": "AUTO_PENDING", "now": "AUTO_PENDING",
                     "fingerprint": {}})["problems"]))
    t("★★型式が変わったら育てない★★",
      any("型式" in x for x in identity_same(
          {"regulatory_model_code": "A/1"}, {"regulatory_model_code": "B/2"})))
    t("★★登録済みの識別子が取れなくなったら育てない★★",
      any("取れなくなりました" in x for x in identity_same(
          {"manufacturer_id": "oizumi"}, {})))
    t("　同じなら通る",
      not identity_same({"manufacturer_id": "a", "announced_name": "L機"},
                        {"manufacturer_id": "a", "announced_name": "L機"}))
    # 区分が AUTO_PENDING でなければ育てない
    got = plan_one("hokuto")
    # ★★2026-08-13・台帳#346（見に行く間隔）★★
    import datetime as _dtm          # ★既存の試験が _dt を別用途で使っている★
    _T = _dtm.date(2026, 8, 13)

    def _rel(off):
        return (_T - _dtm.timedelta(days=off)).isoformat()

    # ★★2026-08-13・台帳#346（軽い様子見）★★
    def _probe_run(skip, known, now=None, drift=False, cv_drift=False):
        # ★出典を探す工程も差し替える★（試験で実サイトへ出ない）
        # ★★「ひな型のずれ」も渡せるようにする★★（2026-09-03・罠㉙）
        #   ★直す前は本物の記事の書き出しを読んでいた★ので、
        #   ひな型を変えた日に**この試験だけが赤くなった**
        #   （見ている守りが2つ混ざっていた＝罠㉚）。
        #   既定は「ずれ無し」＝出典の見送りだけを見る。
        _bk = globals()["_probe_state"]
        _bf = globals()["find_sources"]
        _bd = globals()["template_drift"]
        # ★★2AIの確定値が増えたかも渡せるようにする★★（2026-09-05）
        #   ★本物の控えを読ませない★（試験が機械の書類フォルダに依存しない）
        _bc = globals()["confirmed_fingerprint"]
        globals()["confirmed_fingerprint"] = lambda sl: "FP_IMA"
        _cvv = "FP_MUKASHI" if cv_drift else "FP_IMA"
        globals()["_probe_state"] = (
            lambda: ({"pw_10523": {"urls": known, "cv": _cvv,
                                   "rules": GROW_RULES_VERSION}}
                     if known else {}))
        globals()["find_sources"] = (
            lambda m: list(known if now is None else now))
        globals()["template_drift"] = (
            lambda m, d: (["書き出しの言い回しが、いまのひな型と違います"]
                          if drift else []))
        try:
            return plan_one("pw_10523",
                            probe=lambda u: {"skip": skip, "rows": []},
                            gather=lambda *a, **k: {"urls": [], "problems": [],
                                                    "material": None},
                            verify=lambda *a, **k: {"problems": [],
                                                    "release": "2026-09-07"})
        finally:
            globals()["_probe_state"] = _bk
            globals()["find_sources"] = _bf
            globals()["template_drift"] = _bd
            globals()["confirmed_fingerprint"] = _bc

    t("★★出典の顔ぶれが変わったら見送らない★★（2026-08-14・依頼185のP1）"
      "／別の名鑑に新しく記事が出ても永久に気づかない状態だった",
      _probe_run(True, ["https://x.test/a"],
                 now=["https://x.test/a", "https://x.test/b"])
      .get("unchanged") is False)
    _r = _probe_run(True, ["https://x.test/a"],
                    now=["https://x.test/a", "https://x.test/b"])
    t("★★顔ぶれが変わったこと自体では更新を止めない★★（2026-08-14・依頼190）"
      "／お知らせを problems に入れていたので、その文自身が止めていた",
      any("顔ぶれが変わりました" in n for n in _r.get("notes") or [])
      and not any("顔ぶれが変わりました" in p for p in _r["problems"]))
    t("　（対照）お知らせを problems に入れると必ず止まる"
      "＝新しい出典を見つけた日だけ書けず、翌日は見送りになる",
      bool([p for p in (_r["problems"] + ["顔ぶれが変わりました"])
            if "顔ぶれが変わりました" in p]))
    # ★自分の本文を読む試験は、探す文字列を割って書く★
    #   （そのまま書くと**この試験の文自身**が数に入って自己参照になる）
    _gsrc = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    _c1, _c2 = "_pp.con", "firm("
    _d1, _d2 = "def _absor", "bed()"
    _e1, _e2 = "def confirm_and_", "remember("
    _f1, _f2 = "return confirm_and_", 'remember(got[\"probe_rows\"]'
    # ★★基準を進める場所は、いまも1か所だけ★★
    #   ★2026-09-06に置き場所を変えた★＝`main()` の中の入れ子だと
    #   外から試せず「呼ばれているか」を誰も確かめられなかった（罠③）。
    #   いちばん外の高さへ出したので、その関数を直接試せる。
    t("★★見たページの基準を進めるのは「書けた」「足すものが無い」だけ★★"
      "（2026-08-14・依頼190のP1）／以前は下見でも失敗でも進んでいた",
      _gsrc.count(_c1 + _c2) == 1
      and (_d1 + _d2) in _gsrc
      # ★唯一の呼び出しは、切り出した関数の中にある★
      and _gsrc.index(_c1 + _c2) > _gsrc.index(_e1 + _e2)
      and _gsrc.index(_c1 + _c2) < _gsrc.index("def last_checked(")
      # ★そこへ行けるのは `_absorbed()` を通ったときだけ★
      #   （★文字列は割って書く★＝そのまま書くと**この試験の文自身**が
      #     数に入って、呼び出しを消しても緑のままになる）
      and (_f1 + _f2) in _gsrc
      and _gsrc.index(_d1 + _d2) > _gsrc.index("remember_sources(a.slug"))
    t("　（対照）checked だけを条件にすると、下見でも基準が進む"
      "＝いまは a.apply を必ず見ている",
      "a.apply and got.get(\"checked\") and got.get(\"probe_rows\")" in _gsrc)
    t("　顔ぶれを数え直せなかったときも見送らない（fail-closed）",
      _probe_run(True, ["https://x.test/a"], now=[]).get("unchanged") is False)
    t("★★出典が1つも変わっていなければ、その日は見送る★★（台帳#346）",
      _probe_run(True, ["https://x.test/a"]).get("unchanged") is True)
    t("★★1つでも変わった・確かめられないなら、いつもどおり調べる★★"
      "（★確かめられなかったページを『変化なし』に数えない★）",
      _probe_run(False, ["https://x.test/a"]).get("unchanged") is False)
    # ── ★「やることがあるか」の判断を直接たたく★（2026-09-03）
    #   ★`plan_one` の中に埋めていたときは、どの試験も通らなかった★
    #   （そこへ届くには通信して記事を作るところまで行く必要がある）。
    _old_lead = {"lead": "試験機の機種情報ページです。"
                         "登場時期は2026年8月17日 導入。"}
    _m_g = {"slug": "zzz", "name": "試験機", "release_date": "2026-08-17"}
    _now_lead = {"lead": _ba.LEAD_TEMPLATE.format(
        name="試験機", release=_ba._fmt_release("2026-08-17"))}
    t("★★ひな型がずれていれば「育てるものがありません」で止めない★★"
      "（2026-09-03・Codexの指摘3）",
      growth_reasons(["育てるものがありません（確定した中身が増えていません）"],
                     _m_g, _old_lead) == [])
    t("　（対照）ずれが無ければ、いつもどおり止まる"
      "＝止めなくするのは『ずれ』のときだけ",
      growth_reasons(["育てるものがありません（確定した中身が増えていません）"],
                     _m_g, _now_lead)
      == ["育てるものがありません（確定した中身が増えていません）"])
    t("　材料が増えていれば、ずれの有無に関わらず通す",
      growth_reasons([], _m_g, _old_lead) == []
      and growth_reasons([], _m_g, _now_lead) == [])
    t("★ずれを理由に、別の問題まで消さない★"
      "（消してよいのは『育てるものがありません』の判断だけ）",
      growth_reasons(["育てるものがありません（確定した中身が増えていません）"],
                     _m_g, _old_lead) == []
      # ★★探す文字列は割って書く★★（2026-09-06・Codexの指摘）
      #   ★直す前★＝そのまま書いていたので**この試験の文自身**が
      #   見つかり、本番の呼び出しを消しても緑のままだった。
      #   しかも本番はもう4つ目の引数（新しい機種の行）を渡しているのに、
      #   ★探していた文字列は3つ引数の古い形★で、どこにも無かった。
      and (lambda a, b: (a + b) in io.open(
          os.path.abspath(__file__), encoding="utf-8").read())(
              "growth_reasons(_nn, cur, ", "old_detail, machine)"))
    # ── ★ひな型のずれを見つける関数そのものを試す★（2026-09-03・罠⑤）
    #   ★上の様子見の試験は、この関数を丸ごと差し替える★ので、
    #   中の判定を空にしても緑のままだった（壊し方の道具が見つけた）。
    #   ★材料はその場で作る★＝生きているデータに貼り付けない（罠㉙）。
    _m_day = {"slug": "zzz", "name": "試験機", "release_date": "2026-08-17"}
    _m_mon = {"slug": "zzz", "name": "試験機", "release_date": "2026-08"}
    _m_non = {"slug": "zzz", "name": "試験機"}
    _now_day = _ba.LEAD_TEMPLATE.format(name="試験機",
                                        release=_ba._fmt_release("2026-08-17"))
    _now_non = _ba.LEAD_NO_DATE.format(name="試験機")
    t("★★古い書き方の書き出しは「ずれ」と分かる★★（体言止め）",
      bool(template_drift(_m_day,
                          {"lead": "試験機の機種情報ページです。"
                                   "登場時期は2026年8月17日 導入。"})))
    t("　（対照）いまのひな型で作った書き出しは、ずれない"
      "＝2回目に何も動かない（罠㉘）",
      not template_drift(_m_day, {"lead": _now_day}))
    t("　月までしか分からない機種でも見る",
      bool(template_drift(_m_mon,
                          {"lead": "試験機の機種情報ページです。"
                                   "登場時期は2026年8月頃。"}))
      and not template_drift(
          _m_mon,
          {"lead": _ba.LEAD_TEMPLATE.format(
              name="試験機", release=_ba._fmt_release("2026-08"))}))
    t("　登場時期が無い機種は、日付の無いひな型で見る",
      bool(template_drift(_m_non, {"lead": "試験機のページです。"}))
      and not template_drift(_m_non, {"lead": _now_non}))
    t("★機種名が違っても「ずれ」にしない★"
      "（2AIが機種名を確定すると別名になるため・名前より後ろだけを見る）",
      not template_drift(
          _m_day,
          {"lead": _ba.LEAD_TEMPLATE.format(
              name="ぜんぜん違う名前", release=_ba._fmt_release("2026-08-17"))}))
    t("　記事が無い・書き出しが空のときは、ずれと言わない",
      not template_drift(_m_day, {}) and not template_drift(_m_day, None)
      and not template_drift(_m_day, {"lead": ""}))
    t("　登場時期が食い違えば、ずれと分かる"
      "（ひな型の日付が本文に入るため）",
      bool(template_drift({"slug": "zzz", "name": "試験機",
                           "release_date": "2026-09-01"},
                          {"lead": _now_day})))
    t("★★ひな型がずれていたら見送らない★★（2026-09-03・Codexの指摘3）"
      "／ずれを数えないと、ひな型を直した日に既存の記事が永久に古いまま残る",
      _probe_run(True, ["https://x.test/a"], drift=True)
      .get("unchanged") is not True)
    t("　（対照）同じ材料でも、ずれが無ければ見送る"
      "＝止めているのは『ずれ』であって、他の検査ではない",
      _probe_run(True, ["https://x.test/a"], drift=False)
      .get("unchanged") is True)
    t("　前回の出典を控えていなければ、様子見せずに調べる",
      _probe_run(True, []).get("unchanged") is False)
    t("★★見送るときは材料を作らない★★"
      "（『変わっていないから前の材料を使い回す』をしないため）",
      _probe_run(True, ["https://x.test/a"]).get("machine") is None)
    t("★★カレンダーに無い日付で落ちない★★（依頼181のP1）"
      "／以前は候補を並べる処理ごと止まっていた",
      all(is_new_machine(_b, _T) is False and interval_days(_b, _T) == 7
          for _b in ("2026-02-30", "2026-13-01", "2026-11-31", "へんな値", "")))
    t("★★壊れた控えは「未確認」として必ず見に行く★★（依頼181のP1）",
      all(due("x", "2026-08-01", _T, {"x": _b}) is True
          for _b in ("2026-02-30", "こわれた", "")))
    t("★★見に行った日の控えは共有の state.json と別ファイル★★"
      "（依頼181のP0・一時的に読めないだけで他タスクの履歴を消さない）",
      "state.json" not in STATE_PATH and STATE_PATH.endswith("grow_check.json"))
    t("★★控えが読めないときは書かない★★（消してしまわないため）",
      (lambda: (lambda tmp: (
          io.open(tmp, "w", encoding="utf-8").write("{こわれたJSON"),
          globals().__setitem__("STATE_PATH", tmp),
          mark_checked("x", _T) is False
          and io.open(tmp, encoding="utf-8").read() == "{こわれたJSON",
      )[-1])(__import__("tempfile").mktemp(suffix=".json")))())
    t("★★新台期間は導入日当日から30日目まで★★（2026-08-13・運営者の方針）"
      "／31日目からは更新タスクの通常ローテへ回す",
      [is_new_machine(_rel(o), _T) for o in (-1, 0, 30, 31)]
      == [False, True, True, False])
    t("★★月までしか分からない機種は、月末＋30日まで新台期間★★"
      "（月初を仮の導入日にすると最大30日早く終わる）",
      is_new_machine("2026-07", _dtm.date(2026, 7, 1)) is True
      and is_new_machine("2026-07", _dtm.date(2026, 8, 30)) is True
      and is_new_machine("2026-07", _dtm.date(2026, 8, 31)) is False)
    t("　12月の機種でも月末を正しく出せる（年をまたぐ計算）",
      is_new_machine("2026-12", _dtm.date(2026, 12, 31)) is True
      and is_new_machine("2026-12", _dtm.date(2027, 1, 30)) is True
      and is_new_machine("2026-12", _dtm.date(2027, 1, 31)) is False)
    t("★★導入前と、読めない導入日は新台期間に入れない★★"
      "（導入前は公開と育成の担当／読めない値を新台と推測しない）",
      is_new_machine("2026-11", _T) is False
      and is_new_machine("", _T) is False
      and is_new_machine("へんな値", _T) is False)
    t("★★日が判明したら、その日からの数え方に切り替わる★★"
      "（控えを持たず毎回 release_date から出すため、訂正にも追従する）",
      is_new_machine("2026-07", _dtm.date(2026, 8, 15)) is True
      and is_new_machine("2026-07-01", _dtm.date(2026, 8, 15)) is False)
    t("★★境界値ちょうどで間隔が変わる★★（導入日当日は「導入後0日」＝毎日）",
      [interval_days(_rel(o), _T) for o in
       (-31, -30, -8, -7, -1, 0, 30, 31, 60, 61)]
      == [7, 3, 3, 1, 1, 1, 1, 3, 3, 7])
    t("★★月までしか分からない導入日は、日を補わず専用の間隔で見る★★"
      "（月初を仮の日にすると最大30日早く「導入後」になる）",
      interval_days("2026-09", _T) == 3
      and interval_days("2026-11", _T) == 3)
    t("　読めない導入日は既定の間隔",
      interval_days("", _T) == 7 and interval_days("へんな値", _T) == 7)
    t("★★前に見た日が分からなければ必ず見る★★（記録が消えても止まらない）",
      due("x", _rel(200), _T, {}) is True
      and due("x", _rel(200), _T, {"x": "こわれた日付"}) is True)
    t("　間隔ぶん経つまでは見ない／経ったら見る",
      due("x", _rel(200), _T, {"x": (_T - _dtm.timedelta(days=6)).isoformat()})
      is False
      and due("x", _rel(200), _T, {"x": (_T - _dtm.timedelta(days=7)).isoformat()})
      is True)
    t("★★設定が壊れていたら丸ごと捨てて既定値で続ける★★"
      "（全機種の確認が止まらないように）",
      all(freq_problems(_b) for _b in (
          None, {}, {"schema_version": 2},
          {"schema_version": 1, "default_interval_days": 7,
           "month_interval_days": 3, "ranges": []},
          {"schema_version": 1, "default_interval_days": 7,
           "month_interval_days": 3,
           "ranges": [{"from_days": 1, "to_days": 5, "interval_days": 1}]},
      )))
    t("　いま置いてある設定は壊れていない",
      not freq_problems(load_freq()))
    t("★★範囲の重なり・隙間・上限違反を見つける★★",
      any("重なって" in x for x in freq_problems(
          {"schema_version": 1, "default_interval_days": 7,
           "month_interval_days": 3,
           "ranges": [{"from_days": -36500, "to_days": 10, "interval_days": 1},
                      {"from_days": 5, "to_days": 36500, "interval_days": 7}]}))
      and any("隙間" in x for x in freq_problems(
          {"schema_version": 1, "default_interval_days": 7,
           "month_interval_days": 3,
           "ranges": [{"from_days": -36500, "to_days": 0, "interval_days": 1},
                      {"from_days": 5, "to_days": 36500, "interval_days": 7}]}))
      and any("interval_days" in x for x in freq_problems(
          {"schema_version": 1, "default_interval_days": 7,
           "month_interval_days": 3,
           "ranges": [{"from_days": -36500, "to_days": 36500,
                       "interval_days": 99}]})))
    t("★★既存機種（判定書なし）は育てる対象にしない★★",
      any("育てる対象ではありません" in p for p in got["problems"]))
    t("　知らないslugは対象にしない",
      any("一覧にありません" in p for p in plan_one("no_such_slug")["problems"]))
    # 台帳で止まっている機種は触らない
    real_blocking = _oi.blocking_slugs
    try:
        _oi.blocking_slugs = lambda: {"zz": ["#1 止める"]}
        t("★★台帳で止まっている機種は育てない★★",
          any("止めるべき案件" in x for x in blocked_by_ledger("zz"))
          and not blocked_by_ledger("other"))
        _oi.blocking_slugs = lambda: (_ for _ in ()).throw(RuntimeError("読めない"))
        t("★★台帳を読めない時は進めない★★",
          any("台帳を読めません" in x for x in blocked_by_ledger("zz")))
    finally:
        _oi.blocking_slugs = real_blocking
    # ★本番の台帳ではなく、使い捨ての台帳へ書いたことを確かめる★
    # ★その環境に本番の置き場が無くても落ちないこと★（2026-08-13）
    #   CIはLinuxなので C:/Users/... は存在しない。
    #   samefile() は存在しないパスに例外を投げるため、
    #   **自己テストがそこで落ちてCIが赤になっていた**。
    def _not_the_real_ledger() -> bool:
        if _oi_mod.DEFAULT_FILE == _keep_ledger:
            return False
        try:
            # 実在する時だけ「同じ場所ではない」ことも確かめる
            if _keep_ledger.exists() and _oi_mod.DEFAULT_FILE.exists():
                return not _keep_ledger.samefile(_oi_mod.DEFAULT_FILE)
        except OSError:
            pass                       # 触れない置き場なら、違う場所とみなす
        return True

    t("★★自己テストは本番の台帳に書かない★★（実際にごみが3件入ったので固定する）",
      _not_the_real_ledger())
    _oi_mod.DEFAULT_FILE = _keep_ledger
    # ★差し替えた出典探しを必ず戻す★（試験のあとに本番が空を返さないように）
    globals()["find_sources"] = _keep_find
    import shutil
    shutil.rmtree(_tmp_dir, ignore_errors=True)
    # ★断り書きだけ消す更新は止める★（2026-08-12・依頼161）
    #   無条件に比較の対象外にすると、材料が増えていないのに
    #   「ほかにも天井があるかも」の断りだけ消える更新を誰も止められない。
    _note = _ba.CEILING_PARTIAL_NOTE
    _was = _ceil_box(["**天井**：800G", _note])
    _sneak = _ceil_box(["**天井**：800G"])                     # 増えていない
    _grown = _ceil_box(["**天井**：800G", "**AT間天井**：1200G"])  # 増えた
    t("★★断り書きだけ消す更新は止める★★（網羅性は未確認のまま）",
      bool(text_kept(_was, _sneak)))
    t("★★天井が実際に増えたときは断り書きが消えてよい★★",
      not text_kept(_was, _grown))
    # ★一覧は同じまま「これで全部」と確認できた更新も通す★（依頼162のP1）
    _same = _ceil_box(["**天井**：800G"])
    _same_ok = dict(_same, ceilings_complete=True)
    _same_str = dict(_same, ceilings_complete="true")
    t("★★一覧が同じでも「これで全部」なら断り書きを消せる★★",
      not text_kept(_was, _same_ok))
    t("　文字列の \"true\" では消せない", bool(text_kept(_was, _same_str)))
    #   ★対照実験★＝昔の姿（断り書きを最初から数えない）では素通りする
    t("　（対照）断り書きを数えないと、消すだけの更新が通る",
      not text_kept(_ceil_box(["**天井**：800G"]), _sneak))

    # ★★移行した機種を育てても、確かめ済みの控えが消えないこと★★
    #   （2026-08-16・依頼213の指摘2／台帳#376）
    #   規約でP-WORLDと一撃を出典から外したので、当時2つの出典で確かめた
    #   型式名が、いま集め直すと1つ以下しか出てこない機種がある。
    #   ★実データ（移行した7機種）で確かめる★＝作り話の値では意味がない。
    import safe_json as _sj2
    _raw = _sj2.read_json(_ba.MACHINES, expect=(dict, list))
    _ms = _raw["machines"] if isinstance(_raw, dict) else _raw
    _moved = [m for m in _ms
              if "p-town" in str((m.get("identity") or {}).get(
                  "official_product_url") or "")]

    def _regrow(m):
        """育てたときと同じ道で identity を作り直す（材料は空＝出典が読めない晩）"""
        i = m.get("identity") or {}
        made = _ba.build_machine(
            m["slug"], m["name"], i.get("manufacturer_id", ""),
            i["official_product_url"], i.get("market_release_date", ""), {},
            identity_binding=i.get("identity_binding", ""),
            identity_evidence_ref=i.get("identity_evidence_ref", ""))
        ni = made.get("identity") or {}
        return i, ni, _carry_identity(i, ni)

    _lost = []
    for _m in _moved:
        _old, _new, _ng = _regrow(_m)
        if _ng or identity_same(_old, _new):
            _lost.append(_m["slug"])
    t("★★移行した機種を育てても、確かめ済みの控えが消えない★★"
      f"（実データ{len(_moved)}機種・型式名／移行前の記録／観測値）",
      len(_moved) >= 7 and not _lost)
    # ★対照実験★＝持ち越さない昔の姿では、実際に落ちることを見せる
    _would = []
    for _m in _moved:
        _i = _m.get("identity") or {}
        _made = _ba.build_machine(
            _m["slug"], _m["name"], _i.get("manufacturer_id", ""),
            _i["official_product_url"], _i.get("market_release_date", ""), {},
            identity_binding=_i.get("identity_binding", ""),
            identity_evidence_ref=_i.get("identity_evidence_ref", ""))
        if identity_same(_i, _made.get("identity") or {}):
            _would.append(_m["slug"])
    t("　（対照）持ち越さない昔の姿では、実際に型式名が落ちて育てられない",
      len(_would) >= 5)
    # ★★別の型式名が出てきたら、持ち越しではなく止める★★
    _o = {"regulatory_model_code": "L見える子ちゃんSC",
          "_model_code_sources": ["p-town.dmm.com", "p-world.co.jp"]}
    _n = {"regulatory_model_code": "L別機種XX"}
    t("★★いま集めた材料が別の型式名なら止める★★"
      "（URLが別機種に使い回された形をここで捕まえる）",
      bool(_carry_identity(dict(_o), _n))
      and _n["regulatory_model_code"] == "L別機種XX")
    _n2 = {}
    t("　材料が黙っているときだけ持ち越す（出典が読めなくなった晩）",
      not _carry_identity(dict(_o), _n2)
      and _n2["regulatory_model_code"] == "L見える子ちゃんSC"
      and _n2["_model_code_sources"] == _o["_model_code_sources"])

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

        # --- ★「月だけ」→「日まで」は食い違いにしない★（台帳#383・#441）
        # ★★本物の関数を通す★★（2026-08-21）
        #   直す前は、本体の式を試験のなかに**書き写して**いた。
        #   ＝本体を直しても試験は写しのままなので、
        #   ★同じ規則がもう1か所（identity_same）に要ることに気づけなかった★。
        #   実際、identity の不変検査は月→日の細分化で止め続けていて、
        #   garei_zero_re が育てられないままだった（実データで再現）。
        def _rel(old, new):
            return not release_refined(old, new)

        t("★★月だけ→日まで は止めない★★（DMM移行で全機種が止まっていた形）",
          _rel("2026-08", "2026-08-17") is False)
        t("　別の月なら、いままでどおり止める", _rel("2026-08", "2026-09-01"))
        t("　別の年なら止める", _rel("2026-08", "2027-08-17"))
        t("★日付が短くなる方向は止める★", _rel("2026-08-17", "2026-08"))
        t("　月の形でなければ止める（2026 のような粗い値）",
          _rel("2026", "2026-08"))
        t("　同じ値は「細分化」ではない", _rel("2026-08", "2026-08"))
        # --- ★★まだ導入されていない機種では台帳へ積まない★★
        #   （2026-08-21・台帳#452）
        #   ★導入前は名鑑が「調査中・準備中」なので、載ったり載らなかったりする★
        #   ＝人が見に行っても「まだ載っていない」としか分からない。
        #   実測（2026-08-21）＝新台10機種のうち8機種が導入前だった。

        def _not_yet(rel, today="2026-08-21"):
            rel = str(rel or "")
            if len(rel) == 10 and rel > today:
                return True
            if len(rel) == 7 and rel > today[:7]:
                return True
            return False

        t("★★導入前（来月・再来月）は台帳へ積まない★★",
          _not_yet("2026-09-07") and _not_yet("2026-10"))
        t("　今月・過ぎたものは積む", not _not_yet("2026-08")
          and not _not_yet("2026-08-03") and not _not_yet("2026-07"))
        t("　当日は積む（もう導入されている）", not _not_yet("2026-08-21"))
        # ── 2026-08-27・Codexのレビュー（指摘13）────────────────
        #   ★判定書が壊れている機種を黙って外さない★
        # ★本物の判定書を作らせる★（手書きだと形が合わず、
        #   ★壊れていない側まで壊れて見えて、対照にならない★）
        _ok_pd = _pdz.decide({"adopted": {}})
        _bad_rows = [{"slug": "zzz_broken", "publication_policy": None},
                     {"slug": "zzz_ok",
                      "publication_policy": _ok_pd["schema_version"],
                      "page_decision": _ok_pd}]
        _got_broken = []
        _tg = targets(_bad_rows, broken=_got_broken)
        t("★★判定書が壊れている機種は、黙って外さず名指しする★★"
          "／★直す前は永久に候補から消え、全体は成功に見えた★",
          _got_broken and "zzz_broken" in _got_broken[0])
        t("　壊れていない機種は今までどおり候補になる", _tg == ["zzz_ok"])

        # ── 2026-08-27・行き詰まったら人ではなく2AIへ回す ────────
        #   ★運営者の指示★「2AIで結論出して。人に頼らないで。
        #     本当にどうしてもの場合だけメールで報告」
        import tempfile as _tf_s
        _keep_sp = globals()["STATE_PATH"]
        _dir_s = _tf_s.mkdtemp()
        try:
            globals()["STATE_PATH"] = os.path.join(_dir_s, "s.json")
            # ★★まだ載っていない機種には、決められないことを必ず聞く★★
            #   （2026-09-05。★これが無かったので、型が UNKNOWN のまま
            #     作られた機種は二度と聞かれず、永久に検索へ載らなかった★
            #     ＝実測14機種のうち7機種がこれ）
            _q_no = pending_questions({"page_decision": {"indexable": False}},
                                      {"adopted": {}}, "zzz_q")
            t("★★検索に載っていない機種には、決められないことを聞く★★"
              "（型が決まらないと永久に載らない）",
              len(_q_no) >= 1
              and any("型" in x["text"] for x in _q_no)
              and all(x.get("kind") == "grow_pending" for x in _q_no))
            t("　もう載っている機種には聞かない（答える意味がないので）",
              pending_questions({"page_decision": {"indexable": True}},
                                {"adopted": {}}, "zzz_q") == [])
            t("　判定書がまだ無い機種にも聞く（載っていないので）",
              len(pending_questions({}, {"adopted": {}}, "zzz_q")) >= 1)

            # ★★足りないものを名指しして、出典を読んでもらう★★
            #   （2026-09-05・運営者の指示「2AIが天井の情報を取りに行けばいい」）
            _q_lack = pending_questions(
                {"page_decision": {"indexable": False,
                                   "reason_codes": ["CLAIMS_LT_3",
                                                    "NO_UNIQUE_GAMEPLAY"]}},
                None, "zzz_l", urls=["https://x.test/a"])
            _t_lack = " ".join(x["text"] for x in _q_lack)
            t("★★足りないものを、読める言葉で名指しする★★"
              "（機械の符丁のまま渡さない）",
              "確認できた事実が3件に足りない" in _t_lack
              and "ゲーム性" in _t_lack)
            t("　★読む先（出典URL）も渡す★（2AIが取りに行けるように）",
              "https://x.test/a" in _t_lack
              and any(x.get("urls") for x in _q_lack))
            t("　記録の仕方も伝える（逐語引用が要ること）",
              "confirmed_values" in _t_lack)

            # ★★見送る日でも、載っていないなら聞く★★（2026-09-05）
            #   ★直す前は、見送りの分岐が質問より手前で戻っていた★
            #   （実測：パリピ孔明は材料が足りているのに永久に沈黙）。
            _bk_ps2 = globals()["_probe_state"]
            _bk_fs2 = globals()["find_sources"]
            _bk_cv2 = globals()["confirmed_fingerprint"]
            globals()["confirmed_fingerprint"] = lambda sl: "FP_IMA"
            globals()["find_sources"] = lambda m: ["https://x.test/a"]

            def _skip_run(cv_now):
                globals()["_probe_state"] = (
                    lambda: {"pw_10523": {"urls": ["https://x.test/a"],
                                          "cv": cv_now,
                                          "rules": GROW_RULES_VERSION}})
                return plan_one(
                    "pw_10523", probe=lambda u: {"skip": True, "rows": []},
                    gather=lambda *a, **k: {"urls": [], "problems": [],
                                            "material": None},
                    verify=lambda *a, **k: {"problems": [],
                                            "release": "2026-09-07"})

            try:
                _got_skip = _skip_run("FP_IMA")
                _got_ans = _skip_run("FP_MUKASHI")
            finally:
                globals()["_probe_state"] = _bk_ps2
                globals()["find_sources"] = _bk_fs2
                globals()["confirmed_fingerprint"] = _bk_cv2
            t("★★見送る日でも、まだ載っていないなら2AIに聞く★★"
              "（ここで黙ると永久に載らない）",
              _got_skip.get("unchanged") is True
              and any(x.get("kind") == "grow_read_sources"
                      for x in (_got_skip.get("questions") or [])))
            # ★★答えを記録した日は見送らない★★（2026-09-05・Codexの指摘1）
            #   ★直す前★＝見送りの分岐は質問を出した直後に戻るが、
            #   確定値を材料へ足す処理は**その約120行後**にあった。
            #   ＝2AIが答えて記録しても、翌日も同じ分岐で戻るので
            #   ★答えが永久に届かない★（輪が閉じていなかった）。
            t("★★2AIが答えて記録した日は、出典が同じでも見送らない★★"
              "（ここで見送ると、答えが永久に反映されない）",
              _got_ans.get("unchanged") is not True)
            t("　（対照）確定値も同じなら、ちゃんと見送る"
              "＝止めているのは『確定値の変化』であって、他の検査ではない",
              _got_skip.get("unchanged") is True)
            # ★指紋そのものの試験★＝値が変われば指紋も変わる
            _bk_fs3 = globals()["_cv"].for_slug
            try:
                globals()["_cv"].for_slug = lambda sl: {
                    "machine_profile": {"value": {"profile": "AT_CZ"},
                                        "decided_at": "2026-09-05"}}
                _fp1 = confirmed_fingerprint("zzz_fp")
                globals()["_cv"].for_slug = lambda sl: {
                    "machine_profile": {"value": {"profile": "BONUS"},
                                        "decided_at": "2026-09-05"}}
                _fp2 = confirmed_fingerprint("zzz_fp")
                globals()["_cv"].for_slug = lambda sl: {}
                _fp3 = confirmed_fingerprint("zzz_fp")

                def _boom(sl):
                    raise RuntimeError("読めません")

                globals()["_cv"].for_slug = _boom
                _fp4 = confirmed_fingerprint("zzz_fp")
            finally:
                globals()["_cv"].for_slug = _bk_fs3
            t("　確定値が変われば指紋も変わる", _fp1 and _fp2 and _fp1 != _fp2)
            t("　1件も無いときは決まった印になる（毎回変わらない）",
              _fp3 == "empty")
            t("★★控えを読めないときは空を返す★★"
              "（呼ぶ側が『変わった扱い』にして働く＝答えを取りこぼさない）",
              _fp4 == "")
            # ★★「空を返す」だけでは足りない★★（2026-09-05・壊し方の検査が
            #   教えてくれた）＝呼ぶ側が**それを「変わった」と読む**ことまで
            #   見ないと、fail-open に書き換えても緑のまま通る（罠④）。
            _bk_cfp = globals()["confirmed_fingerprint"]
            _bk_pss = globals()["_probe_state"]
            try:
                globals()["_probe_state"] = (
                    lambda: {"zzz_d": {"urls": [], "cv": "FP_A",
                                       "rules": GROW_RULES_VERSION}})
                globals()["confirmed_fingerprint"] = lambda sl: ""
                _d_unread = confirmed_drift("zzz_d")
                globals()["confirmed_fingerprint"] = lambda sl: "FP_A"
                _d_same = confirmed_drift("zzz_d")
                globals()["confirmed_fingerprint"] = lambda sl: "FP_B"
                _d_new = confirmed_drift("zzz_d")
                globals()["_probe_state"] = lambda: {}
                _d_none = confirmed_drift("zzz_d")
                # ★★育て方の決まりが変わったら、必ず調べ直す★★
                #   （2026-09-06・Codexの指摘）＝古い決まりで
                #   「書かずに指紋だけ控えた」機種は、見送りの分岐で
                #   早期に戻るので★判定書が古いまま永久に見送られる★。
                #   人が控えを消して回るのではなく、機械が気づくようにする。
                globals()["_probe_state"] = (
                    lambda: {"zzz_d": {"urls": [], "cv": "FP_A",
                                       "rules": "2000-01-01"}})
                globals()["confirmed_fingerprint"] = lambda sl: "FP_A"
                _d_oldrule = confirmed_drift("zzz_d")
                globals()["_probe_state"] = (
                    lambda: {"zzz_d": {"urls": [], "cv": "FP_A"}})
                _d_norule = confirmed_drift("zzz_d")
            finally:
                globals()["confirmed_fingerprint"] = _bk_cfp
                globals()["_probe_state"] = _bk_pss
            t("★★控えを読めない日は『変わった』とみなす★★"
              "（ここを『変わっていない』にすると2AIの答えを取りこぼす）",
              _d_unread is True)
            t("　同じ指紋なら『変わっていない』", _d_same is False)
            t("　指紋が変われば『変わった』", _d_new is True)
            t("　控えが1件も無いときも『変わった』（初回は必ず働く）",
              _d_none is True)
            t("★★育て方の決まりが変わったら『変わった』★★"
              "（★これが無いと、古い決まりで控えた機種を"
              "人が消して回るしかない★）", _d_oldrule is True)
            t("　版の記録が無い古い控えも『変わった』（fail-closed）",
              _d_norule is True)
            # ★★「答えがあるか」は別の問い★★（2026-09-06・Codexの指摘3）
            #   ★直す前★＝`confirmed_drift()` で「答えがあるか」を見ていた。
            #   控えがまだ一度も無い機種は**変わった扱い**になるので、
            #   ★答えが1件も無い機種にも「答えが反映できていません」★
            #   と出ていた。
            _bk_fsx = globals()["_cv"].for_slug
            try:
                globals()["_cv"].for_slug = lambda sl: {}
                _h_none = has_confirmed("zzz_h")
                globals()["_cv"].for_slug = lambda sl: {"machine_profile": {}}
                _h_some = has_confirmed("zzz_h")

                def _boom_h(sl):
                    raise RuntimeError("読めません")

                globals()["_cv"].for_slug = _boom_h
                _h_bad = has_confirmed("zzz_h")
            finally:
                globals()["_cv"].for_slug = _bk_fsx
            t("★★答えが1件も無ければ『無い』と答える★★"
              "（★変わったか、とは別の問い★）", _h_none is False)
            t("　1件でもあれば『ある』", _h_some is True)
            t("　読めないときは『無い』に倒す（余計な警告を出さない）",
              _h_bad is False)

            # ★★控えに指紋が実際に書かれるか★★（2026-09-05）
            #   ★直す前は誰も控えの中身を見ていなかった★＝
            #   `remember_sources` から指紋を外しても緑のままだった。
            import tempfile as _tf_rs
            _bk_probe_path = globals()["PROBE_STATE"]
            _bk_cfp2 = globals()["confirmed_fingerprint"]
            _tmp_rs = _tf_rs.mkdtemp(prefix="grow_rs_")

            def _read_rs():
                with io.open(globals()["PROBE_STATE"],
                             encoding="utf-8") as _f_rs:
                    return json.load(_f_rs)

            try:
                globals()["PROBE_STATE"] = os.path.join(_tmp_rs, "s.json")
                globals()["confirmed_fingerprint"] = lambda sl: "FP_KIROKU"
                # ①下見（出典だけ控える）
                _ok_rs = remember_sources("zzz_rs", ["https://x.test/a"])
                _s1 = _read_rs()
                # ②書けたときだけ指紋を控える（★指紋は渡す★）
                _ok_cf = remember_after_write("zzz_rs", "FP_KIROKU")
                _s2 = _read_rs()
                # ③そのあと出典を控え直しても、指紋は消えない
                remember_sources("zzz_rs", ["https://x.test/a",
                                            "https://x.test/b"])
                _s3 = _read_rs()
                # ④指紋を渡されなければ控えない（★取り直さない★）
                globals()["confirmed_fingerprint"] = lambda sl: "FP_ATARASHII"
                _ok_ng = remember_after_write("zzz_rs2", "")
                _s4 = _read_rs()
            finally:
                globals()["PROBE_STATE"] = _bk_probe_path
                globals()["confirmed_fingerprint"] = _bk_cfp2
                import shutil as _sh_rs
                _sh_rs.rmtree(_tmp_rs, ignore_errors=True)
            # ★★下見では指紋を控えない★★（2026-09-06・Codexの指摘1）
            #   ★直す前★＝出典と一緒に控えていたので、
            #   **下見しただけ・あとで失敗した**ときも「反映済み」になり、
            #   ★記事は古いままなのに次から見送られた★
            #   （実害＝下見しただけの機種に指紋が入っていた）。
            t("★★出典を控えるだけでは、指紋を控えない★★"
              "（下見や失敗のあとに『反映済み』にしない）",
              _ok_rs and "cv" not in (_s1.get("zzz_rs") or {}))
            t("　出典URLは今までどおり控える",
              (_s1.get("zzz_rs") or {}).get("urls") == ["https://x.test/a"])
            t("★★書けたときは指紋を控える★★",
              _ok_cf and (_s2.get("zzz_rs") or {}).get("cv") == "FP_KIROKU")
            t("★★あとで出典を控え直しても、指紋は消えない★★"
              "（消すと毎日やり直しになる）",
              (_s3.get("zzz_rs") or {}).get("cv") == "FP_KIROKU"
              and len((_s3.get("zzz_rs") or {}).get("urls") or []) == 2)
            # ★★渡されなければ控えない（取り直さない）★★
            #   （2026-09-06・Codexの指摘2）＝呼ばれた時点の最新を
            #   取り直すと、書き込み検査のあとに確定値が更新されたとき
            #   ★記事には古い値・控えには新しい指紋★になり、
            #   その分が二度と反映されない。
            t("★★指紋を渡されなければ控えない★★"
              "（★その場で取り直さない★＝競合で答えを飛ばさない）",
              _ok_ng is False and "zzz_rs2" not in _s4)

            # ★★「読み比べ成立 → 指紋を控える」が繋がっているか★★
            #   （2026-09-06・罠③＝関数だけ試しても
            #     「呼ばれているか」は分からない。実際に
            #     壊し方の検査が「守られていません」と教えてくれた）
            _seen_cr = []
            _bk_rc = globals()["remember_confirmed"]
            try:
                globals()["remember_confirmed"] = (
                    lambda sl, fp="": _seen_cr.append((sl, fp)) or True)
                _r_w = remember_after_write("zzz_cr", "FP_TSUKATTA")
            finally:
                globals()["remember_confirmed"] = _bk_rc
            # ★★書けたときの控えは `probe_rows` に依らない★★
            #   （2026-09-06・Codexの指摘4）＝以前は読み比べの成立を
            #   条件にしていたので、★軽い様子見をしていない日は
            #   正しく書いても控えられず、次回ぜんぶやり直していた★。
            t("★★書けたら、記事に使った指紋をそのまま控える★★"
              "（★取り直さない★＝競合で答えを飛ばさない）",
              _r_w is True and _seen_cr == [("zzz_cr", "FP_TSUKATTA")])
            # ★★呼び出しが実在するか★★（関数の中身だけ試しても、
            #   呼び出しを消されたら気づかない＝罠③）
            #   ★文字列は割って書く★＝そのまま書くとこの試験の文自身が
            #   数に入って、呼び出しを消しても緑のままになる。
            _g1, _g2 = "remember_after_", 'write(a.slug, got.get("cv_used")'
            t("★★書けたあとに、指紋を控える呼び出しが2か所ある★★"
              "（『足すものが無かった』日と『書けた』日）",
              _gsrc.count(_g1 + _g2) == 2)

            # ★★材料が無い日でも、判定書の欄から聞ける★★（2026-09-05）
            #   ★ここが、この守りの効く唯一の場所★＝材料があるときは
            #   `checker_questions` が同じ項目を聞くので、
            #   この繰り返しを消しても気づかない（罠④）。
            _q_nomat = pending_questions(
                {"page_decision": {"indexable": False, "reason_codes": [],
                                   "machine_profile": "UNKNOWN",
                                   "ceiling_state": "UNKNOWN"}},
                None, "zzz_nm")
            _t_nomat = " ".join(x["text"] for x in _q_nomat)
            t("★★材料が無い日でも、型を聞ける★★（見送りの日はここだけ）",
              "--field machine_profile " in _t_nomat)
            t("★★材料が無い日でも、天井の有無を聞ける★★"
              "（★理由コードには出ないので、ここを消すと永久に聞けない★）",
              "--field ceiling_state " in _t_nomat)
            t("　決まっている欄は聞かない（答える意味がないので）",
              "--field ceiling_state " not in " ".join(
                  x["text"] for x in pending_questions(
                      {"page_decision": {"indexable": False,
                                         "reason_codes": [],
                                         "machine_profile": "AT_CZ",
                                         "ceiling_state": "PRESENT"}},
                      None, "zzz_nm2")))

            # ★★本番の入口（plan_one）でも、質問が出ることを確かめる★★
            #   （2026-09-05。★関数だけ試験しても「呼ばれているか」は
            #     分からない★＝壊し方の検査がそれを教えてくれた・罠③）
            _bk_ps = globals()["_probe_state"]
            _bk_fs = globals()["find_sources"]
            globals()["_probe_state"] = lambda: {}
            globals()["find_sources"] = lambda m: []
            try:
                _got_q = plan_one(
                    "pw_10523",
                    probe=lambda u: {"skip": True, "rows": []},
                    gather=lambda *a, **k: {"urls": [], "problems": [],
                                            "material": {"adopted": {}}},
                    verify=lambda *a, **k: {"problems": [],
                                            "release": "2026-09-07",
                                            "identity_name": "テスト機"})
            finally:
                globals()["_probe_state"] = _bk_ps
                globals()["find_sources"] = _bk_fs
            t("★★本番の入口でも、載っていない機種には質問が出る★★"
              "（関数を作っただけで繋がっていない、を防ぐ）",
              any(str(q.get("text", "")).find("型") >= 0
                  for q in (_got_q.get("questions") or [])))

            # ★★材料が無くても、2AIの確定値だけで進む★★
            #   （2026-09-06・Codexの指摘1。★私は「直せない」と判断したが
            #     間違いだった★＝`merge_into({})` は空の辞書に箱を作る。
            #     記事の骨組みは `mat` ではなく既存行から来ている）
            _bk_ps4 = globals()["_probe_state"]
            _bk_fs4 = globals()["find_sources"]
            _bk_hc4 = globals()["has_confirmed"]
            globals()["_probe_state"] = lambda: {}
            globals()["find_sources"] = lambda m: []

            def _nomat_run(has):
                globals()["has_confirmed"] = lambda sl: has
                return plan_one(
                    "pw_10523",
                    probe=lambda u: {"skip": True, "rows": []},
                    gather=lambda *a, **k: {"urls": [], "material": None,
                                            "problems": ["名鑑が1件だけです"]},
                    verify=lambda *a, **k: {"problems": [],
                                            "release": "2026-09-07",
                                            "identity_name": "テスト機"})

            try:
                _nm_yes = _nomat_run(True)
                _nm_no = _nomat_run(False)
            finally:
                globals()["_probe_state"] = _bk_ps4
                globals()["find_sources"] = _bk_fs4
                globals()["has_confirmed"] = _bk_hc4
            t("★★材料が無くても、答えがあるなら先へ進む★★"
              "（★ここで止めると、答えが永久に反映されない★）",
              not any("材料を集められません" in p
                      for p in (_nm_yes.get("problems") or []))
              and any("確定値だけで進みます" in n
                      for n in (_nm_yes.get("notes") or [])))
            t("　（対照）答えが1件も無ければ、今までどおり止まる"
              "＝止めているのは『答えの有無』であって、他の検査ではない",
              any("材料を集められません" in p
                  for p in (_nm_no.get("problems") or [])))
            t("★★答えが無い機種に「答えが反映できていません」と言わない★★"
              "（2026-09-06・Codexの指摘3）",
              not any("答えが記事に反映できていません" in p
                      for p in (_nm_no.get("problems") or [])))
            t("　答えが無い日でも、決まっていないことは聞く",
              any(x.get("kind") == "grow_read_sources"
                  for x in (_nm_no.get("questions") or [])))

            # ★★本文に出ない確定値も「書く理由」に数える★★
            #   （2026-09-06・Codexの指摘）★直す前★＝
            #   `nothing_new()` は claim の件数と記事本文しか見ないので、
            #   `machine_profile` のように本文に出ない値は
            #   「育てるものがありません」→ 書かずに指紋だけ控える
            #   → ★判定書が古いまま永久に検索へ載らない★。
            #   実例＝prskkm（claim 4件・止まる理由は型だけ）。
            _row_old = {"slug": "zzz_r",
                        "page_decision": {"indexable": False,
                                          "machine_profile": "UNKNOWN",
                                          "reason_codes":
                                              ["MACHINE_PROFILE_UNKNOWN"],
                                          "input_digest": "aaa",
                                          "decided_at": "2026-09-05"},
                        "checker": {"unit": "G"},
                        "identity": {"a": 1}}
            _row_same = json.loads(json.dumps(_row_old))
            _row_same["page_decision"]["decided_at"] = "2026-09-06"
            _row_type = json.loads(json.dumps(_row_same))
            _row_type["page_decision"]["machine_profile"] = "AT_CZ"
            _row_type["page_decision"]["indexable"] = True
            _row_type["page_decision"]["reason_codes"] = []
            _row_ck = json.loads(json.dumps(_row_same))
            _row_ck["checker"] = {"unit": "pt"}
            _row_id = json.loads(json.dumps(_row_same))
            _row_id["identity"] = {"a": 2}
            t("★★日付しか違わないときは、書く理由にしない★★"
              "（数えると毎日ぜんぶ書き直しになる）",
              machine_row_drift(_row_old, _row_same) == [])
            t("★★型が決まったら書く理由になる★★"
              "（★本文にもclaimにも出ないので、ここで数えないと"
              "永久に検索へ載らない★）",
              any("判定書" in x
                  for x in machine_row_drift(_row_old, _row_type)))
            t("　早見表の材料が変わっても書く理由になる",
              any("早見表" in x for x in machine_row_drift(_row_old, _row_ck)))
            t("　機種の身元が変わっても書く理由になる",
              any("身元" in x for x in machine_row_drift(_row_old, _row_id)))
            t("　新しい行が作れていないときは何も言わない",
              machine_row_drift(_row_old, None) == []
              and machine_row_drift(_row_old, {}) == [])
            # ★入口（growth_reasons）でも効いているか★（罠③）
            _nnx = ["育てるものがありません（確定した中身が増えていません）"]
            _bk_td = globals()["template_drift"]
            try:
                globals()["template_drift"] = lambda m, d: []
                _gr_same = growth_reasons(_nnx, _row_old, {}, _row_same)
                _gr_type = growth_reasons(_nnx, _row_old, {}, _row_type)
            finally:
                globals()["template_drift"] = _bk_td
            t("★★入口でも、型が決まったら止めない★★"
              "（関数を作っただけで繋がっていない、を防ぐ）", _gr_type == [])
            t("　（対照）何も変わっていなければ、今までどおり止まる"
              "＝止めているのは『一覧側の変化』であって、他の検査ではない",
              _gr_same == _nnx)

            # ★本番と同じ入口（grow_result）を通す★
            # ★日をまたいで数える★（同じ日に何度動いても1回）
            _a1 = grow_result("zzz_s", False, "理由", today="2026-08-01")
            t("　同じ日に何度動いても増えない",
              grow_result("zzz_s", False, "理由",
                          today="2026-08-01")["round"] == 1)
            _a2 = grow_result("zzz_s", False, "理由", today="2026-08-02")
            t("★★1〜2回目は2AIに聞く（人へ回さない）★★"
              "／★直す前は、その場で人へ回して止まったままだった★",
              _a1["do"] == "ask" and _a2["do"] == "ask"
              and "2AIで決めてください" in _a1["text"])
            _a3 = grow_result("zzz_s", False, "理由", today="2026-08-03")
            t("★★3回目でだけ人へ報告する★★",
              _a3["do"] == "ledger" and _a3["round"] == STUCK_ASK_LIMIT)
            t("　機種ごとに数える（よその失敗を持ち越さない）",
              grow_result("zzz_other", False, "理由",
                          today="2026-08-03")["round"] == 1)
            t("★うまく育ったら0に戻す（昔の失敗を数え続けない）★",
              grow_result("zzz_s", True)["do"] == "ok"
              and grow_result("zzz_s", False, "理由",
                              today="2026-08-04")["do"] == "ask")
        finally:
            globals()["STATE_PATH"] = _keep_sp
            import shutil as _sh_s
            _sh_s.rmtree(_dir_s, ignore_errors=True)

        t("★★登場時期が分からないときは積む（安全側）★★",
          not _not_yet(""))
        t("　日として短すぎる値は通さない", _rel("2026-08", "2026-08-1"))
        # ★★identity の不変検査も同じ規則で動く★★（台帳#441・対照実験）
        t("★★身元の検査でも、月だけ→日まで は止めない★★"
          "（ここが片側だけ残っていて garei_zero_re が育たなかった）",
          identity_same({"market_release_date": "2026-08"},
                        {"market_release_date": "2026-08-17"}) == [])
        t("　身元の検査でも、別の月なら止める",
          identity_same({"market_release_date": "2026-08"},
                        {"market_release_date": "2026-09-01"}) != [])
        t("　身元の検査でも、日付が短くなる方向は止める",
          identity_same({"market_release_date": "2026-08-17"},
                        {"market_release_date": "2026-08"}) != [])
        t("　登場年月いがいの項目は、いままでどおり食い違いで止める",
          identity_same({"manufacturer_id": "a"},
                        {"manufacturer_id": "b"}) != [])
    finally:
        globals()["_log"] = _keep_log
        globals()["_main"] = _keep_inner
        sys.argv = _keep_argv

    # ★★廃止した決まり文句の免除★★（2026-09-01・台帳#538）
    #   ★実害★＝2026-08-26に言い換えたとき新しい方を足し忘れ、
    #   その文を持つ既存記事（prskkm / ssb1）が**永久に育たなかった**。
    t("★★廃止した決まり文句は、比べる単位に数えない★★"
      "（数えると『内容が消えた』と判定され、永久に育たなくなる）",
      all(_pending_check(x) for x in RETIRED_BOILERPLATE))
    t("　ふつうの本文は今までどおり数える（箱ごと免除していない）",
      not _pending_check("天井は1200Gです"))
    # ★★基本スペックは、本文でも表でも同じに数える★★（2026-09-01・台帳）
    #   ★これが無いと★＝新台13機種を表へ移した瞬間に
    #   「前に載っていた内容が消えた」と判定され、**全部が永久に育たなくなる**
    #   （今日直した prskkm / ssb1 とまったく同じ事故）。
    _SB = {"sections": [{"title": "基本スペック", "body": [
        "**機種名**：テスト機", "**機械割**：97.9%〜112.1%",
        "**登場時期**：2026年8月3日",
        "**50枚あたりのゲーム数**：未確認（確認でき次第掲載します）"]}]}
    _ST = {"sections": [{"title": "基本スペック", "type": "table",
                         "tables": [{"headers": ["項目", "内容"], "rows": [
                             ["機種名", "テスト機"],
                             ["機械割", "97.9%〜112.1%"],
                             ["登場時期", "2026年8月3日"],
                             ["50枚あたりのゲーム数",
                              "未確認（確認でき次第掲載します）"]]}]}]}
    t("★★本文を表へ移しても、比べる単位は同じ★★"
      "（違うと13機種が永久に育たなくなる）",
      _units(_SB) == _units(_ST))
    t("　確定して載っている数も同じ",
      confirmed_count(_SB) == confirmed_count(_ST))
    t("　未確認の行は、本文でも表でも数えない",
      not any("未確認" in str(u) for u in _units(_ST)))
    t("　登場時期は、本文でも表でも比べない"
      "（identity_same と release_refined が守っている）",
      not any("登場時期" in str(u) for u in _units(_ST)))
    _ST2 = {"sections": [{"title": "基本スペック", "type": "table",
                          "tables": [{"headers": ["項目", "内容"], "rows": [
                              ["機種名", "テスト機"],
                              ["機械割", "99.9%〜112.1%"]]}]}]}
    t("★★値を1つ変えたら、今までどおり違うものとして扱う★★",
      _units(_SB) != _units(_ST2))
    t("　基本スペック以外の表は、いままでどおりの形で数える",
      any(u[0] == "table" for u in _units(
          {"sections": [{"title": "設定示唆まとめ", "type": "settei",
                         "tables": [{"label": "x", "headers": ["a", "b"],
                                     "rows": [["1", "2"]]}]}]})))
    t("　太字でない行は、基本スペックでも今までどおり本文として数える",
      any(u[0] == "body" for u in _units(
          {"sections": [{"title": "基本スペック",
                         "body": ["これは文章です。"]}]})))

    t("★まだ作っている文を免除していないか（機械が探す）★",
      retired_boilerplate_problems() == [])
    # ★★見つける側の道も通す★★（2026-09-01・壊し方の確認で判明）
    #   ★「1件も見つからない」だけを見ていると、探す処理を消しても緑★
    t("★★まだ使われている文を免除に入れたら、機械が見つける★★"
      "（見つからない側だけ試すと、探す処理を消しても緑だった）",
      retired_boilerplate_problems(["def build_detail("]) != [])
    t("　どこにも無い文なら何も言わない",
      retired_boilerplate_problems(["この文はどこにもありませんZZZ"]) == [])

    print(f"\n{ran[0]}/{ran[0]} 合格" if ok else "\n不合格あり")
    return 0 if ok else 1


def main() -> int:
    """★取りに行った回数を、実行のどこで終わっても必ず残す★

    （2026-08-16・依頼222の指摘2）
    育てる処理は page_probe からも外へ出る。ここで戻さないと、
    **この経路で増えた回数がプロセスの終わりに消える**。
    """
    _is_selftest = "--selftest" in (sys.argv[1:] or [])
    import new_machine_watch as _nwg
    _nwg.budget_reset()
    try:
        return _main()
    finally:
        if not _is_selftest:
            _log(f"取りに行った回数: {_nwg.FETCH_BUDGET['used']} 回"
                 f"（上限 {_nwg.FETCH_BUDGET['limit']} 回 / 転送 "
                 f"{_nwg.FETCH_COUNT.get('redirect', 0)} 回 / 控えで済んだ "
                 f"{_nwg.FETCH_COUNT.get('cached', 0)} 回）")


def _main() -> int:
    ap = argparse.ArgumentParser(description="新台経路の機種を育てる")
    ap.add_argument("--slug")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    rows = _read_rows()
    # ★今日見る分だけを選ぶ★（2026-08-13・台帳#346）
    #   毎朝すべてをフル確認すると1機種8分×機種数かかる。
    #   導入日からの距離で間隔を変える（設定＝grow-machine-frequency.json）。
    _today = _dt.date.today()
    _seen = last_checked()
    _broken = []
    tg = targets(rows, _today, _seen, broken=_broken)
    if not a.slug:
        _all = targets(rows)
        print(f"育てる対象: {len(tg)}機種 " + " ".join(tg[:10]))
        if len(_all) > len(tg):
            print(f"（{len(_all) - len(tg)}機種は今日は見ません＝"
                  f"導入日から遠いので間隔を空けています）")
        # ★★外した機種は必ず出す★★（2026-08-27・Codexの指摘13）
        for _b in _broken:
            print("  ★判定書が壊れていて候補から外しました★ " + _b)
        # ★★機種名なしの --apply をはっきり断る★★（Codexの指摘12）
        #   ★直す前は対象を並べて exit 0★＝
        #   「やったつもりで1機種も書いていない」が成功に見えた。
        if a.apply:
            print("★機種名がありません★ --apply は1機種ずつです"
                  "（python scripts/grow_machine.py --slug <機種> --apply）")
            return 2
        return 0
    got = plan_one(a.slug)
    # ★次回の様子見のための控えは「材料まで見に行けた」ときだけ★
    #   （2026-08-14・依頼185のP1）失敗した回の顔ぶれで上書きすると、
    #   一時的に一部しか見つからなかっただけで**出典の集合が縮む**。
    #   ★変化を見つけたページの基準も、ここまで来てから進める★
    #   （その場で進めると、確認に失敗しても翌日は「変化なし」になる）
    # ★★2AIに聞くことを必ず出す★★（2026-08-26）
    #   ★止まる理由そのものが質問になっている★ので、
    #   材料が集まらなかった回ほど大事。出さないと誰も答えられない。
    for _q in got.get("questions") or []:
        # ★★切らない★★（2026-09-05・Codexの指摘4）＝
        #   200字で切っていたので、★記録の書き方が丸ごと消えていた★
        #   （型の質問は335字あり `--official-url <公式URL` で切れる）。
        #   読む相手は2AIなので、長さより**欠けないこと**が大事。
        print("  ★2AIに聞くこと: " + str(_q.get("text") or _q))
        # ★読む先は行を分ける★（1行に並べると端が切れて見落とす）
        for _u in (_q.get("urls") or []):
            print("      読む先: " + str(_u))
    for _n in got.get("notes") or []:
        print("  （お知らせ）" + _n)
    # ★★導入日が「月だけ」→「日まで」分かったら、登録も直す★★
    #   （2026-08-29・運営者の指示／Codexの指摘を受けて作り直した）
    #   ★下見では書かない★／★鍵の中で書く★／
    #   ★書いた回はそこで終わる★＝計画の指紋と食い違うため。
    #   次の実行が新しい日付で計画し直し、そこから新しい間隔で回る。
    _rr = got.get("release_refine") or {}
    if _rr and a.apply:
        with _pub._OnlyOne():              # ★書き込みは同時に2つ走らせない★
            _ok_rr, _why_rr = refine_release_date(
                a.slug, _rr.get("old") or "", _rr.get("new") or "")
        if _ok_rr:
            print("  " + _why_rr + "／次の実行から新しい間隔で見ます")
            _log(f"{a.slug}: {_why_rr}")
            return 0
        if _why_rr:
            print("  ★導入日を細かくできません★: " + _why_rr)
            _log(f"{a.slug}: 導入日を細かくできません: {_why_rr}")
            return 1
    elif _rr:
        print(f"  （下見）導入日が細かく分かっています"
              f"（{_rr.get('old')} → {_rr.get('new')}）"
              "／--apply で登録を直します")
    if got.get("checked") and got.get("source_urls"):
        remember_sources(a.slug, got["source_urls"])

    def _absorbed():
        """★見たページの基準を進めてよいか★（2026-08-14・依頼190のP1④）

        以前は「材料まで見に行けた（checked）」だけで進めていたので、
        **その後で失敗しても・下見だけでも**基準が進み、
        次の日は「変化なし」で見送れてしまった＝変化を永久に取りこぼす。
        進めてよいのは次の2つだけ。
          ・実際に書けた
          ・最後まで読み比べて「足すものが無い」と分かった
        """
        if not (a.apply and got.get("checked") and got.get("probe_rows")):
            return False
        # ★★指紋を控えるのはここだけ★★（2026-09-06・Codexの指摘1）
        #   ＝「実際に書けた」「読み比べて足すものが無い」と
        #   分かった時にしか通らない場所。
        return confirm_and_remember(got["probe_rows"], a.slug)
    # ★控えるのは「書き込む実行」で「最後まで成立した」時だけ★
    #   （2026-08-13・依頼181のP1／依頼182のP1で条件を狭めた）
    #   ・下見で控えると、書いていないのに次の予定日まで候補から外れる
    #   ・材料が増えていた機種は、実際に書けたのを見てから控える
    #     （書込み前の監査で止まった／指紋が食い違った／書いたあと戻した、
    #      のどれでも「確認できた」ことにはならない）
    #   ★変化なしで正常に終わった時は控えてよい★＝見に行けているため。
    if got["problems"]:
        if a.apply and got.get("nothing_new_only"):
            # ★調べたが足すものが無かった＝正常に終わっている★
            #   （以前の条件 `was == now` は、problems があるときは
            #     now が None のままなので**一度も成立しなかった**）
            mark_checked(a.slug, _dt.date.today())
            _absorbed()
            # ★★指紋は probe_rows の有無に関係なく控える★★
            #   （2026-09-06・Codexの指摘4）＝軽い様子見をしていない日は
            #   `_absorbed()` が False で、正しく終わっても控えられず
            #   次回もう一度ぜんぶやり直していた。
            remember_after_write(a.slug, got.get("cv_used") or "")
        print("できません:")
        for p in got["problems"]:
            print("  -", p)
        return 1
    print(f"{a.slug}: {got['was']} → {got['now']} "
          f"/ 事実 {len((got['machine'].get('page_decision') or {}).get('claims') or [])}件")
    if not a.apply:
        print("（下見です。書き込むには --apply）")
        return 0
    r = apply_one(got)
    # ★書けたときだけ控える★（依頼182のP1）
    if not r["problems"]:
        mark_checked(a.slug, _dt.date.today())
        _absorbed()
        # ★★指紋は probe_rows の有無に関係なく控える★★（Codexの指摘4）
        remember_after_write(a.slug, got.get("cv_used") or "")
    for p in r["problems"]:
        print("  -", p)
    if r["wrote"]:
        print("書きました: " + " ".join(os.path.relpath(x, BASE).replace(os.sep, "/")
                                         for x in r["wrote"]))
    return 1 if r["problems"] else 0


# ★中身を見に来たら元の関数を返す★（2026-08-16・依頼222）
main.__wrapped__ = _main


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except _sj.SafeJsonError as e:
        print(f"★入力データが読めません: {e}★")
        raise SystemExit(1)

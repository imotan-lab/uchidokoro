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
        got[slug] = {"urls": sorted(set(str(u) for u in urls))}
        tmp = f"{PROBE_STATE}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(got, f, ensure_ascii=False, indent=1)
        os.replace(tmp, PROBE_STATE)
        return True
    except Exception as e:                # noqa: BLE001
        print(f"  出典を控えられません（続けます）: {e}")
        return False


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
            conf=None) -> list:
    """育てる対象（`AUTO_PENDING` の機種のうち、今日見る日のもの）。"""
    out = []
    for m in rows:
        try:
            if _pdz.machine_class(m) != "AUTO_PENDING":
                continue
        except _pdz.DecisionError:
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
            ng.append(f"{jp}が変わっています（{a!r} → {b!r}）")
        if a and not b:
            ng.append(f"{jp}が取れなくなりました（登録済み: {a!r}）")
    return ng


def claims_grew(old_decision: dict, new_decision: dict) -> list:
    """材料が「増えるだけ」か（★減る・変わるのは中止★）。

    ★これだけでは足りない★（2026-08-05・Codex102回目）
      claim ID は「天井のゲーム数」までしか表さないので、
      **800G → 999G の書き換えは同じIDのまま通ってしまう**。
      値そのものは `text_kept()` で見る（前に載っていた文が消えないこと）。
    """
    old = list((old_decision or {}).get("claims") or [])
    new = list((new_decision or {}).get("claims") or [])
    lost = [c for c in old if c not in new]
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
        # ★もう出さないことにした決まり文句★（2026-08-12・運営者決定）
        #   既に公開した記事には入っているので、消えても「内容が減った」と
        #   判定しない（読者に情報を与える文ではない）。
        if t == "出典2件で一致した内容だけを載せています。":
            return True
        return (t == _ba.PENDING_TEXT) or (_ba.PENDING_ITEM in t) \
            or (t == getattr(_ba, "PENDING_TEXT_OLD", "\0")) \
            or (t.strip() == "確認中") or ("確認できていない" in t) \
            or ("出典で食い違い" in t) or ("書き方が異なります" in t)

    for s in detail.get("sections") or []:
        title = str(s.get("title"))
        for b in (s.get("body") or []):
            t = str(b).strip()
            if t and not _pending(t):
                out.append(("body", title, t))
        for tb in (s.get("tables") or []):
            label = str((tb or {}).get("label") or "")
            headers = tuple(str(x) for x in ((tb or {}).get("headers") or []))
            for row in ((tb or {}).get("rows") or []):
                # ★未確定の欄だけを「何が来てもよい」にする★
                #   （2026-08-05・Codex104回目の指摘3。行ごと捨てていたので、
                #     同じ行の**確定済みの欄**まで比べられなくなっていた）
                cells = tuple(_cell(x, _pending) for x in (row or []))
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
    lead = str(detail.get("lead") or "").strip()
    if lead and not _pending(lead):
        out.append(("lead", lead))
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
           "notes": [], "nothing_new_only": False,
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
                if _pr.get("skip"):
                    out["problems"].append(
                        "出典の顔ぶれも中身も前回から変わっていません"
                        "（今日は見送ります）")
                    out["unchanged"] = True
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
        out["problems"].append(
            f"登場年月が変わっています（{old_release} → {vo['release']}）"
            "／自動では直しません")
        return out
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
    # ★材料が返っても「書いてはいけない理由」があれば止める★
    #   （2026-08-05・Codex102回目の指摘1。転載の疑いなどは
    #     material が作られても新台側では公開を止めている）
    blk = _amr.blocking_problems(got.get("problems") or [])
    if blk:
        out["problems"] += [f"止めました: {p}" for p in blk]
        return out
    mat = got.get("material")
    if not mat:
        out["problems"].append("材料を集められません: "
                               + " / ".join(got.get("problems") or [])[:200])
        return out
    # ★ここまで来たら「実際に見に行けた」★（依頼181のP1）
    #   本人性の確認や材料集めに失敗した機種を「確認済み」にしない。
    out["checked"] = True
    # ★2AIで確定した値も材料に足す★（2026-08-11・台帳#316）
    #   足す場所が add_machine_run の中の1か所にしか無かったので、
    #   **確定値を載せた機種はここで「前に載っていた内容が再現できない」**
    #   と判定され、育てる処理が毎日止まっていた（パリピ孔明・ガレイゼロ）。
    #   ＝「読む側を1か所しか繋いでいなかった」型の穴。
    #   ★読めないことを黙って「無い」にしない★（例外は理由として残す）
    try:
        _added = _cv.merge_into(mat, slug)
        if _added:
            _log("  2AIで確定した値を材料に足しました: " + " / ".join(_added))
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
    out["problems"] += claims_grew(cur.get("page_decision"),
                                   machine.get("page_decision"))
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
    out["problems"] += _nn
    out["nothing_new_only"] = bool(_clean and _nn)
    # ★「前に載っていた内容が再現できない」は人へ回す★（黙って止め続けない）
    if lost or any("消えます" in p for p in out["problems"]):
        ledger_once(
            slug, "確認済みだった内容を再現できません（育てる処理を止めています）",
            " / ".join(lost + [p for p in out["problems"] if "消えます" in p])[:900]
            + "／出典の一時的な不調かもしれません。旧い内容は公開されたままです。"
            "人が出典を見て、直すか消すかを決めてください。")
    if out["problems"]:
        return out
    try:
        out["now"] = _pdz.machine_class(machine)
    except _pdz.DecisionError as e:
        out["problems"].append(f"新しい判定書が壊れています: {e}")
        return out
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

    def _ceil_box(lines):
        return {"slug": "zzz",
                "sections": [{"title": "天井・恩恵", "body": lines}]}

    def t(name, cond):
        nonlocal ok
        ran[0] += 1
        print(("✅ " if cond else "❌ ") + name)
        ok = ok and bool(cond)

    t("★★確認済みの事実が消える更新は拒否する★★",
      claims_grew({"claims": ["a", "b"]}, {"claims": ["a"]})
      and "消えます" in claims_grew({"claims": ["a", "b"]},
                                    {"claims": ["a"]})[0])
    t("　増えていれば通る", not claims_grew({"claims": ["a"]},
                                            {"claims": ["a", "b"]}))
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
    t("★★まとめ箱・リード文の書き換えも止める★★",
      text_kept(OLD, _mod(lambda d: d["summaryBoxes"][0].__setitem__(
          "value", "999G")))
      and text_kept(OLD, _mod(lambda d: d.__setitem__("lead", "別の紹介文。"))))
    t("★★項目ごとの「未確認」を埋める更新は通す★★（Codex103回目・正しい更新を拒んでいた）",
      not text_kept(OLD, _mod(lambda d: d["sections"][0]["body"].__setitem__(
          1, "**50枚あたりのゲーム数**：約32G"))))
    t("　箱の「未確認です」を中身に差し替えるのも通る",
      not text_kept(OLD, _mod(lambda d: d["sections"][2]["body"].__setitem__(
          0, "通常時800Gで天井に到達します。"))))
    t("★★同じ表の行が減ったら止める★★",
      text_kept(OLD, _mod(lambda d: d["sections"][1]["tables"][0]
                          .__setitem__("rows", []))))
    # ── ★本物の build_detail で、暫定表現が埋まる更新を通す★（Codex104回目）
    def _mat(**kw):
        m = {"adopted": {"model_code": {"value": "L機/1", "sources": ["a", "b"]}},
             "need_third": {}, "thin": {},
             "ceilings": {"adopted": [], "need_third": []},
             "at_specs": {"adopted": [], "need_third": []},
             "czs": {"adopted": [], "need_third": []},
             "setting_labels_seen": [], "setting_labels_unconfirmed": []}
        m.update(kw)
        return m

    cz_thin = _mat(czs={"adopted": [{"name": "喰霊チャンス",
                                     "sources": ["a", "b"]}], "need_third": []})
    cz_full = _mat(czs={"adopted": [{"name": "喰霊チャンス", "games": 10,
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
        return base

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
    def _probe_run(skip, known, now=None):
        # ★出典を探す工程も差し替える★（試験で実サイトへ出ない）
        _bk = globals()["_probe_state"]
        _bf = globals()["find_sources"]
        globals()["_probe_state"] = (
            lambda: ({"pw_10523": {"urls": known}} if known else {}))
        globals()["find_sources"] = (
            lambda m: list(known if now is None else now))
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
    t("★★見たページの基準を進めるのは「書けた」「足すものが無い」だけ★★"
      "（2026-08-14・依頼190のP1）／以前は下見でも失敗でも進んでいた",
      _gsrc.count(_c1 + _c2) == 1
      and (_d1 + _d2) in _gsrc
      and _gsrc.index(_c1 + _c2) > _gsrc.index(_d1 + _d2)
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
    finally:
        globals()["_log"] = _keep_log
        globals()["_main"] = _keep_inner
        sys.argv = _keep_argv

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
    tg = targets(rows, _today, _seen)
    if not a.slug:
        _all = targets(rows)
        print(f"育てる対象: {len(tg)}機種 " + " ".join(tg[:10]))
        if len(_all) > len(tg):
            print(f"（{len(_all) - len(tg)}機種は今日は見ません＝"
                  f"導入日から遠いので間隔を空けています）")
        return 0
    got = plan_one(a.slug)
    # ★次回の様子見のための控えは「材料まで見に行けた」ときだけ★
    #   （2026-08-14・依頼185のP1）失敗した回の顔ぶれで上書きすると、
    #   一時的に一部しか見つからなかっただけで**出典の集合が縮む**。
    #   ★変化を見つけたページの基準も、ここまで来てから進める★
    #   （その場で進めると、確認に失敗しても翌日は「変化なし」になる）
    for _n in got.get("notes") or []:
        print("  （お知らせ）" + _n)
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
        return _pp.confirm(got["probe_rows"])
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

# -*- coding: utf-8 -*-
"""dmm_machine.py — DMMぱちタウンの機種ページから、機種を同定する。

★なぜDMMなのか（2026-08-16・台帳#376）★
  P-WORLDの利用規約がプログラムによるアクセスとデータ収集を禁止しているため、
  同定の正をDMMへ移しました。★通信は blocked_hosts.py が止めます★

★何を取るか★
  https://p-town.dmm.com/machines/<ID>

    型式名     LB/タコスロBD
    メーカー名  ユニバーサルブロス（メーカー公式サイト）
    導入開始日  2026年09月07日（月）予定

  ★表として読む★（正規表現で文を探さない）＝`html_tables` に任せる。

★検定番号は取れません★
  P-WORLDにしか無かったので、同定の芯は
  **DMMの機種ID＋型式名＋機種名＋メーカー＋導入日**になります。
  ★同じ機種IDで結ばれていること（カレンダー→個別ページ）が新しい芯★
  （Codexの助言・依頼211）

★守る線★
  ①URLのIDと、ページが名乗るID（正規URL）が一致すること
  ②型式名・メーカー・導入日のどれかが空なら「同定できない」
  ③同じ項目が2つ以上あって値が違えば止める
  ④パチスロだと確かめられること
  ★迷ったら同定しない★（別機種に情報を流用しないため）

使い方:
    python scripts/dmm_machine.py --id 5049
    python scripts/dmm_machine.py --selftest
"""
from __future__ import annotations

import argparse
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import blocked_hosts as _bh          # noqa: E402
import html_tables as _ht            # noqa: E402
import new_machine_watch as _w       # noqa: E402

URL = "https://p-town.dmm.com/machines/%s"
_ID_IN_URL = re.compile(r"/machines/(\d+)")
_DATE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
# ★先の機種は日が決まっていない★（2026-08-16・実データ）
#   「2026年11月予定」「2026年11月上旬予定」のように月までのことがある。
#   ★日を勝手に決めない★＝月の精度のまま返し、日はカレンダー側から取る。
_DATE_MONTH = re.compile(r"(\d{4})年(\d{1,2})月")
# ★メーカー欄にはリンクの文言が2つ入る★（実データで確認・2026-08-16）
#   「ユニバーサルブロス(メーカー公式サイト) ユニバーサルブロスの掲載機種一覧」
#   表を平らな文字にすると両方つながるので、**最初の印までを社名**とする。
_MAKER_CUT = re.compile(r"[（(]\s*メーカー公式サイト\s*[）)]")
# ★取り切れたか確かめる語★（残っていたら社名ではない＝止める）
_MAKER_LEFTOVER = ("掲載機種一覧", "公式サイト", "一覧")


class MachineError(Exception):
    """機種ページを同定に使えない（★迷ったら使わない★）。"""


def _one(pairs: list, label: str) -> str:
    """その表の中の1項目。★2つ以上あって値が違えば止める★"""
    got = [v for k, v in pairs if str(k).strip() == label]
    vals = {" ".join(str(v).split()) for v in got if str(v).strip()}
    if len(vals) > 1:
        raise MachineError(f"「{label}」が複数あって値が違います: {sorted(vals)}")
    return vals.pop() if vals else ""


def _maker_of(raw: str) -> str:
    """メーカー欄から社名だけを取る。★取り切れなければ止める★

    ★決め打ちで削らない★（2026-08-16）
      欄にはリンクの文言が2つ入る。最初の印までを社名として切り、
      **切ったあとに案内文が残っていたら社名として扱わない**。
      相手が文言を変えたら、黙って変な値を使うより止めるほうが安全
      （そこから先は2AIの出番＝当サイトの鉄則）。
    """
    s = " ".join(str(raw or "").split())
    if not s:
        return ""
    head = _MAKER_CUT.split(s, 1)[0].strip()
    if not head:
        raise MachineError(f"メーカー欄から社名を取れません: {s[:50]}")
    if any(w in head for w in _MAKER_LEFTOVER) or len(head) > 40:
        raise MachineError(
            f"メーカー欄の形が変わったようです: {head[:50]}"
            "／★社名として扱いません★（案内文が混ざっています）")
    return head


def parse(html: str, want_id: str = "") -> dict:
    """機種ページを読む。★足りなければ例外★"""
    if not html:
        raise MachineError("ページが空です")
    # ① ページが名乗るID（正規URL）
    m = re.search(r'<link[^>]+rel="canonical"[^>]+href="([^"]+)"', html)
    said = ""
    if m:
        mm = _ID_IN_URL.search(m.group(1))
        said = mm.group(1) if mm else ""
    if want_id and said and said != str(want_id):
        raise MachineError(
            f"URLのIDと、ページが名乗るIDが違います（URL {want_id} / ページ {said}）")
    # 表から取る（★1つ目の表が基本情報★）
    # ★型式名は「まだ無い」ことがある★（2026-08-16・実データで確認）
    #   未導入の新台は型式名がまだ載らない（L転生王女＝メーカー・導入日はあるが
    #   型式名なし）。必須にすると**新台を一切扱えなくなる**ので、
    #   基本情報の表かどうかは「メーカー名＋導入開始日」で見分け、
    #   ★型式名はあれば使う／無ければ空で返して、強さの判断は呼び出し側に任せる★
    spec = None
    for tb in _ht.tables(html):
        ks = {str(k).strip() for k, _ in tb["pairs"]}
        if {"メーカー名", "導入開始日"} <= ks:
            spec = tb
            break
    if spec is None:
        raise MachineError("基本情報の表（メーカー名・導入開始日）が"
                           "見つかりません")
    code = _one(spec["pairs"], "型式名")
    maker = _maker_of(_one(spec["pairs"], "メーカー名"))
    rel_raw = _one(spec["pairs"], "導入開始日")
    d = _DATE.search(rel_raw)
    # ② 足りなければ同定できない（★型式名は任意★）
    miss = [n for n, v in (("メーカー", maker),
                           ("導入開始日", rel_raw)) if not v]
    if miss:
        raise MachineError("同定に要る項目がありません: " + "・".join(miss))
    # ★日が決まっていなければ月の精度で返す★（日を勝手に決めない）
    if d:
        rel, prec = ("%s-%02d-%02d" % (d.group(1), int(d.group(2)),
                                       int(d.group(3))), "day")
    else:
        dm = _DATE_MONTH.search(rel_raw)
        if not dm:
            raise MachineError(f"導入開始日を読めません: {rel_raw[:40]}")
        rel, prec = ("%s-%02d" % (dm.group(1), int(dm.group(2))), "month")
    # ④ パチスロか
    title = _w.page_title(html) or ""
    body = html[:4000]
    if "パチスロ" not in title and "パチスロ" not in body:
        raise MachineError("パチスロのページだと確かめられません")
    return {
        "id": said or str(want_id),
        "url": URL % (said or want_id),
        "heading": _machine_name(html, title),
        "model_code": code,
        # ★型式名があるか★＝同定の強さは呼び出し側が決める
        #   （未導入の新台はまだ載らないので、無いこと自体は異常ではない）
        "has_model_code": bool(code),
        "maker": maker,
        "release_date": rel,
        # ★日まで分かっているか★（先の機種は月までのことがある）
        "release_precision": prec,
        "release_raw": rel_raw,
        "planned": "予定" in rel_raw,
    }


def _machine_name(html: str, title: str) -> str:
    """ページの見出し（★機種名そのものではない★）。

    ★DMMの見出しはSEO用の飾りが付く★（2026-08-16・実データ）
      「L 転生王女と天才令嬢の魔法革命 （新台スマスロ）パチスロ｜設定判別・天井…」
    ここから機種名を切り出そうとすると、切り方の場合分けが増える。
    ★機種名の正はカレンダー側★（そちらは飾りが無い）とし、
    ここは**照合のための見出し**として返すだけにする。
    """
    m = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.S)
    if m:
        t = " ".join(re.sub(r"<[^>]+>", " ", m.group(1)).split())
        if t:
            return t
    return " ".join(str(title).split("|")[0].split())


def name_matches(heading: str, calendar_name: str) -> bool:
    """★カレンダーの機種名が、機種ページの見出しと同じ機種を指すか★

    見出しは飾りが付くので、**カレンダーの名前の芯が見出しの芯の頭にある**
    ことを求める（前方一致）。芯は claim_identity の正規化を使う
    （「L」「スマスロ」等の飾り・全半角・記号の差を吸収する）。
    """
    import claim_identity as _ci
    a = _ci.normalize_core(str(calendar_name or ""))
    b = _ci.normalize_core(str(heading or ""))
    if len(a) < 2 or not b:
        return False
    return b.startswith(a)


def fetch(machine_id: str, get=None) -> dict:
    u = URL % machine_id
    _bh.check(u)
    try:
        html = (get or _w._get)(u)
    except Exception as e:                 # noqa: BLE001
        raise MachineError(f"取得できません（{u}）: {str(e)[:90]}")
    return parse(html, str(machine_id))


# ---------------------------------------------------------------- selftest

def _fixture(name: str) -> str:
    p = os.path.join(BASE, "tests", "fixtures", name)
    if not os.path.isfile(p):
        return ""
    import io
    return io.open(p, encoding="utf-8").read()


def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅ " if cond else "❌ ") + name)

    def raises(fn, word=""):
        try:
            fn()
            return False
        except MachineError as e:
            return (word in str(e)) if word else True

    h = _fixture("dmm_machine_5049.html")
    if not h:
        t("★試験用の保存ページがありません（tests/fixtures）★", False)
    else:
        g = parse(h, "5049")
        t("★★保存したページから同定に要る項目を読める★★（ネットに出ない試験）",
          g["model_code"] == "LB/タコスロBD"
          and g["release_date"] == "2026-09-07")
        t("　メーカー名から飾りを外す（（メーカー公式サイト）を落とす）",
          g["maker"] == "ユニバーサルブロス")
        t("　見出しを取れる（★機種名の正はカレンダー側★）",
          "タコスロ" in g["heading"])
        t("★★カレンダーの名前と機種ページの見出しを照合できる★★",
          name_matches(g["heading"], "スマスロ タコスロ")
          and not name_matches(g["heading"], "スマスロ北斗の拳"))
        t("　飾りの違い（L／スマスロ／全半角）は吸収する",
          name_matches("L 転生王女と天才令嬢の魔法革命 （新台スマスロ）パチスロ｜天井",
                       "L転生王女と天才令嬢の魔法革命"))
        t("　導入予定かどうかも分かる", g["planned"] is True)
        t("★★URLのIDとページが名乗るIDが違えば止める★★",
          raises(lambda: parse(h, "9999"), "違います"))
        # ★★型式名は「まだ無い」ことがある（2026-08-16）★★
        #   未導入の新台は型式名が載らない。必須にすると新台を扱えない。
        _nocode = parse(h.replace("型式名", "型式めい"), "5049")
        t("★★型式名がまだ無くても同定できる★★"
          "（未導入の新台は載らない・必須にすると新台を扱えない）",
          _nocode["has_model_code"] is False and _nocode["model_code"] == ""
          and _nocode["maker"] == "ユニバーサルブロス")
        t("　（対照）型式名があるときは必ず使う", g["has_model_code"] is True)
        t("★★メーカーが無ければ同定しない★★",
          raises(lambda: parse(h.replace("メーカー名", "めーかー名"), "5049"),
                 "見つかりません"))
        t("　空なら止める", raises(lambda: parse("", "5049")))

    h2 = _fixture("dmm_machine_4709.html")
    if h2:
        g2 = parse(h2, "4709")
        t("　別の機種でも読める（七つの魔剣が支配する）",
          g2["model_code"] == "L七つの魔剣が支配するPU"
          and g2["maker"].startswith("コナミ")
          and g2["release_date"] == "2025-01-20")
        t("　すでに導入済みなら「予定」ではない", g2["planned"] is False)

    # ★メーカー欄の形が変わったら止める★（黙って変な値を使わない）
    t("★★メーカー欄に案内文が残っていたら社名にしない★★"
      "／相手が文言を変えたら止めて2AIへ回す",
      raises(lambda: _maker_of("ユニバーサルブロスの掲載機種一覧"), "形が変わった")
      and raises(lambda: _maker_of("")) is False)
    t("　（対照）いまの形なら社名だけを取れる",
      _maker_of("ユニバーサルブロス(メーカー公式サイト) "
                "ユニバーサルブロスの掲載機種一覧") == "ユニバーサルブロス"
      and _maker_of("平和（メーカー公式サイト）") == "平和")
    t("★★規約で禁止された先には通信しない★★（P-WORLDの機種ページ）",
      _bh.is_blocked("https://www.p-world.co.jp/machine/database/10510"))
    t("　DMMは通ってよい",
      not _bh.is_blocked("https://p-town.dmm.com/machines/5049"))

    ng = sum(1 for _, o in results if not o)
    print()
    print("%d/%d 合格" % (len(results) - ng, len(results)))
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="DMMの機種ページから同定する")
    ap.add_argument("--id")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if a.selftest:
        return selftest()
    if not a.id:
        ap.print_help()
        return 0
    try:
        g = fetch(a.id)
    except MachineError as e:
        print("★" + str(e) + "★")
        return 1
    for k in ("id", "heading", "model_code", "has_model_code", "maker",
              "release_date", "planned", "url"):
        print("  %-13s %s" % (k, g[k]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

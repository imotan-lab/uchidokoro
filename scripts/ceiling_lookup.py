"""ceiling_lookup.py — 天井を「一式」で採る（値だけ先に載せない）。

★なぜ一式か（2026-07-31・Codexの助言／自分でも妥当と判断）★
  天井は数字だけでは使えない。同じ「1200G」でも、
  何を数えた1200Gなのか（通常時／AT間／液晶G数）で別の事実になる。
  実際、東京喰種で CZ間600G（液晶G数）と AT間1200G（メニュー画面）を
  取り違える事故が起きている。

  そこで **値・数える対象・恩恵** がそろって初めて1つの事実として採る。
  1つでも欠けたら**採らない**（値だけ先に載せない）。

★2つのサイトで書き方が違う（実データで確認）★
  P-WORLD       … 文章。「通常時を最大1200G消化するとゲーム数天井に到達し、ATに当選する。」
  ちょんぼりすた … 表。「天井G数 → 1200G」「恩恵 → AT当選」
  どちらの形からも同じ形に落とす。

★採用の条件★
  独立2出典で **値・種類・恩恵がすべて一致** したときだけ採用。
  1つでも違えば採らず、「第三の出典が要る」として返す。

使い方:
    python scripts/ceiling_lookup.py --name "Lすーぱぁびん娘" \\
        --url https://www.p-world.co.jp/machine/database/10496 \\
        --url https://chonborista.com/slot/belko-slot/260918/
    python scripts/ceiling_lookup.py --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import html_tables as _ht             # noqa: E402
import model_code_lookup as _mc       # noqa: E402
import new_machine_watch as _w        # noqa: E402
import spec_lookup as _sl             # noqa: E402

# 天井の種類。★数える対象が違えば別の事実★
KINDS = {
    "GAME": {"jp": "ゲーム数天井", "unit": "G"},
    "CYCLE": {"jp": "周期天井", "unit": "周期"},
    "POINT": {"jp": "ポイント天井", "unit": "pt"},
    # ★スルー天井★（2026-08-06。両名鑑が書いているのに拾えていなかった）
    #   ★数え方の取り違えに注意★＝「N回目で確定」は (N-1) スルー。
    #   採るのは**スルー回数**（うちどころの checker.suruMax と同じ数え方）。
    "THROUGH": {"jp": "スルー天井", "unit": "スルー"},
}

# 文章から採る形（★許可した言い回しだけ★・禁止語を並べる方式は必ず抜ける）
_SENT_GAME = re.compile(
    r"(?P<counted>[^。、]{0,14}?)を?最大\s*(?P<amount>\d{2,5})\s*G\s*消化すると"
    r"[^。]{0,20}?天井に到達し[、,]?\s*(?P<benefit>[^。]{1,24}?)に当選")
_SENT_CYCLE = re.compile(
    r"(?P<counted>[^。、]{0,20}?)が?最大\s*(?P<amount>\d{1,3})\s*周期に到達すると"
    r"[^。]{0,16}?天井となり[、,]?\s*(?P<benefit>[^。]{1,24}?)に当選")
# ★スルー天井の言い回し★（許可した形だけ・実データから起こした）
#   P-WORLD:「CZでAT非当選が最大6回続くと天井到達。到達時は次回(最大7回目)のCZでATに当選する。」
_SENT_THROUGH = re.compile(
    r"(?P<counted>[^。、]{0,16}?)で(?P<miss>[^。、]{0,10}?非当選)が?最大\s*"
    r"(?P<amount>\d{1,2})\s*回続くと[^。]{0,12}?天井")
#   「N回目で必ず当たる」形は (N-1) スルーとして採る
#   ★2026-08-06・Codex123回目★
#     以前は「当選」だけで一致していたので、
#       ・CZ7回目でもAT非当選（＝逆の意味）
#       ・7回目のCZでATに当選した（＝ある人の実績）
#       ・CZ7回目のスルーで天井（＝7スルー。N-1ではない）
#     まで6スルーとして採っていた（自分で再現した）。
#     いまは「必ず／濃厚／確定」の保証表現があり、
#     打ち消し（非当選・しなかった）と「N回目のスルー」を含まない形だけ。
_SENT_THROUGH_NTH = re.compile(
    r"(?P<counted>[^。、]{0,16}?)の?(?P<nth>\d{1,2})\s*回目"
    r"(?![^。]{0,6}?スルー)"
    r"(?:は天井|[^。]{0,14}?(?:必ず|確定|濃厚))")
#   「Nスルー後（に）M回目」形＝そのままNスルー
#     例:「関所チャレンジ6スルー後、7回目の関所チャレンジで勝率が100%となる。」
_SENT_THROUGH_AFTER = re.compile(
    r"(?P<counted>[^。、]{0,20}?)(?P<amount>\d{1,2})\s*スルー後[^。]{0,12}?"
    r"(?P<nth>\d{1,2})\s*回目")
# ★打ち消しが混じる文は採らない★
#   ★一致した部分だけでなく、その文の最後まで見る★（Codex124回目。
#     「CZ7回目は天井ではない」は一致が『天井』で終わるため素通りしていた）
#   ★「非当選」は打ち消しではない★（2026-08-06・自分の試験で気づいた）
#     「CZでAT非当選が最大6回続くと天井」は**正しい天井の説明**。
#     これを打ち消し扱いにしたら、正しい文まで捨てて出典が1つに減った。
_NEGATED = re.compile(r"しなかった|とは限らない|ではない|ではありません|"
                      r"天井では|外れ(?!値)")
# 「N回目で当たる」形にだけ効く打ち消し（当選の否定）
_NEGATED_NTH = re.compile(r"非当選|しなかった|とは限らない|ではない|ではありません")


def _sentence_at(text: str, pos: int) -> str:
    """その位置を含む1文を返す（否定語を文の最後まで見るため）。"""
    start = text.rfind("。", 0, pos) + 1
    end = text.find("。", pos)
    return text[start:(len(text) if end < 0 else end + 1)]

# 表から採る形（見出しと値が交互に並ぶ）
_TABLE_LABELS = {"GAME": ("天井G数", "天井ゲーム数"), "CYCLE": ("天井周期", "周期天井")}
_BENEFIT_LABELS = ("恩恵", "天井恩恵")

# 恩恵として認める形（★文にせず短い語だけ★）
_BENEFIT_OK = re.compile(r"^[ぁ-んァ-ヶ一-龥A-Za-z0-9「」・＋+/ 　]{2,24}$")


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", " ".join(str(s or "").split()))


# 恩恵の言い方をそろえる。★意味を変えない範囲だけ★
#   「AT」と「AT当選」は同じことを言っている（片方は動詞が省かれているだけ）。
#   ★「約50%」と「50%以上」のような意味の幅が違うものは揃えない★
#   （それは spec_lookup の phrasing_not_equal が担当）。
# 「当選」は動詞が省かれているだけなので落としてよい。
#   ★「濃厚」「確定」は落とさない★（確からしさが変わる・Codex指摘、実際に再現した）
_BENEFIT_PLAIN = ("に当選", "当選", "に突入", "突入")
# 確からしさ。★これが違えば別の事実として扱う★
_CERTAINTY = (("濃厚", "LIKELY"), ("確定", "GUARANTEED"))


# ★英語表記とカタカナ表記の差だけをそろえる★（2026-08-03・Codex59回目）
#   P-WORLD「夢娘 ドリムス CHANCE」↔ちょんぼりすた「夢娘チャンス」の
#   英語部分。CZ名の同値化（cz_lookup）と同じ考え方で、意味の同じ
#   固定語だけを対応させる。
_ENG_KANA = (("CHANCE", "チャンス"), ("CHALLENGE", "チャレンジ"),
             ("BONUS", "ボーナス"), ("TIME", "タイム"))


def split_benefit(text: str):
    """恩恵を「何が起きるか」と「どのくらい確かか」に分ける。

    ★『CZ当選濃厚』と『CZ当選』を同じにしない★
      濃厚は確定ではない。以前は両方 `CZ` にしていたため、
      **確からしさの違う出典どうしを一致とみなしていた**（実際に再現）。

    ★かっこ・空白・英語表記の差はそろえる★（2026-08-03・Codex59回目）
      P-WORLDは「AT+「 夢娘 ドリムス CHANCE」」のようにかっこと空白と
      ふりがなを挟む。かっこ・空白・英語↔カタカナは機械的にそろえる
      （ふりがな部分＝ドリムスは機械では判定できないので、
      benefit_aliases（機種名つきの検証済み対応表）が担当する）。
    """
    t = _norm(text).strip("。、,. ")
    for q in "「」『』“”\"'":
        t = t.replace(q, "")
    t = t.replace(" ", "").replace("　", "")
    for eng, kana in _ENG_KANA:
        t = re.sub(eng, kana, t, flags=re.I)
    cert = "PLAIN"
    for word, name in _CERTAINTY:
        if word in t:
            cert = name
            t = t.replace(word, "")
            break
    for suf in _BENEFIT_PLAIN:
        if t.endswith(suf) and len(t) > len(suf):
            t = t[: -len(suf)]
            break
    return t.strip("　 "), cert


def _benefit_alias(benefit: str, official_name: str) -> str:
    """★検証済みの恩恵名の対応表★（2026-08-03・Codex59回目）

    実ページで「同じ天井の恩恵」と確認できた組だけを
    collection-rules.json の benefit_aliases に機種名つきで登録し、
    片方の書き方（a）へ寄せる。登録が無ければそのまま返す。
    """
    try:
        rules = _sl.load_rules()
    except Exception:                     # noqa: BLE001
        return benefit
    for p in (rules.get("benefit_aliases") or {}).get("pairs") or []:
        if official_name not in (p.get("machines") or []):
            continue
        if benefit in (p.get("a"), p.get("b")):
            return p.get("a")
    return benefit


def normalize_benefit(text: str) -> str:
    """後方互換：恩恵の中身だけを返す（確からしさは split_benefit で取る）。"""
    return split_benefit(text)[0]


def normalize_counted(text):
    """数える対象をそろえる（見出しの飾りを落とす）。"""
    if not text:
        return None
    t = _norm(text)
    for mark in ("▼", "■", "●", "◆"):
        if mark in t:
            t = t.rsplit(mark, 1)[-1]
    # 「ゲーム数天井 通常時」のように見出しが残ることがあるので、天井の語を落とす
    t = re.sub(r"^(ゲーム数天井|周期天井|ポイント天井)\s*", "", t.strip())
    return t.strip() or None


# ★読者の書き込みは事実として採らない★（2026-08-06・自分の試験で気づいた）
#   P-WORLDの機種ページは下の方に掲示板があり、そこにも天井の話が出る。
#   書き込みは推測や間違いを含むので、ここから先は読まない。
_USER_AREA = ("掲示板", "口コミ", "みんなの感想", "コメント一覧")


def cut_user_area(text: str) -> str:
    """掲示板より前だけを返す（＝サイト側が書いた部分だけ）。"""
    # ★同じ語がページ上部の目次にも出る★（2026-08-06・実データで確認）
    #   いちばん最初で切ると本文ごと落ちる。**最後に出てきた場所**で切る。
    # ★語ごとの「最後」を集めて最小を取るのは誤り★（Codex123回目）
    #   目次に「掲示板 / 口コミ」が並ぶと、口コミの最後＝目次の位置になり、
    #   そこで切って**本文ごと落ちる**。
    #   いちばん後ろに現れた見出しを1つだけ選ぶ。
    last = -1
    for w in _USER_AREA:
        for m in re.finditer(rf"(?m)^\s*{re.escape(w)}\s*$", text):
            last = max(last, m.start())
    return text if last < 0 else text[:last]


# ★CZらしい名前★（「〇〇チャレンジ」「〇〇チャンス」など）
_CZ_NAME = re.compile(
    r"([一-龥ぁ-んァ-ヶA-Za-z0-9]{2,12}(?:チャレンジ|チャンス|バトル|ゾーン|RUSH))")


def through_counted(raw: str):
    """スルー天井の「何を数えるか」を返す（★決まらなければ None★）。

    ★書いてある名前をそのまま残す★（2026-08-06・Codex123回目）
      「CZ」と「関所チャレンジ」を勝手に同じ物にすると、別のCZと
      取り違える。名前は残しておき、**同じ物だと確かめられた時だけ**
      呼び出し側（compare）で寄せる。
    """
    t = _norm(raw)
    m = _CZ_NAME.search(t)
    if m:
        return m.group(1)
    if "CZ" in t or "ＣＺ" in t:
        return "CZ"
    if "ボーナス" in t:
        return "ボーナス"
    return None


def from_sentences(text: str) -> list:
    """文章から天井を採る（P-WORLD の形）。★掲示板は読まない★"""
    text = cut_user_area(text)
    out = []
    for rx, kind in ((_SENT_GAME, "GAME"), (_SENT_CYCLE, "CYCLE")):
        for m in rx.finditer(_norm(text)):
            benefit = m.group("benefit").strip()
            counted = m.group("counted").strip() or None
            if not _BENEFIT_OK.match(benefit):
                continue
            out.append({"kind": kind, "amount": int(m.group("amount")),
                        "unit": KINDS[kind]["unit"],
                        "counted": normalize_counted(counted),
                        "benefit": split_benefit(benefit)[0],
                        "certainty": split_benefit(benefit)[1],
                        "raw": m.group(0)[:120]})
    # ★スルー天井★（恩恵は同じ文に無いことが多いので、別に集める）
    #   ★「N回目で確定」は (N-1) スルー★（数え方を取り違えない）
    t = _norm(text)
    for m in _SENT_THROUGH.finditer(t):
        if _NEGATED.search(_sentence_at(t, m.start())):
            continue
        if through_counted(m.group(0)) is None:
            continue                      # 何を数えるか決まらない票は使わない
        out.append({"kind": "THROUGH", "amount": int(m.group("amount")),
                    "unit": KINDS["THROUGH"]["unit"],
                    "counted": through_counted(m.group(0)),
                    "benefit": "", "certainty": "",
                    "raw": m.group(0)[:120]})
    for m in _SENT_THROUGH_AFTER.finditer(t):
        amount, nth = int(m.group("amount")), int(m.group("nth"))
        # ★数え方が食い違う文は採らない★（Nスルー後は N+1 回目のはず）
        if nth != amount + 1 or amount < 1 or amount > 20:
            continue
        if _NEGATED.search(_sentence_at(t, m.start())):
            continue
        counted = through_counted(m.group(0))
        if counted is None:
            continue
        out.append({"kind": "THROUGH", "amount": amount,
                    "unit": KINDS["THROUGH"]["unit"], "counted": counted,
                    "benefit": "", "certainty": "", "raw": m.group(0)[:120]})
    for m in _SENT_THROUGH_NTH.finditer(t):
        nth = int(m.group("nth"))
        if nth < 2 or nth > 20:
            continue
        if _NEGATED_NTH.search(_sentence_at(t, m.start())):
            continue
        if through_counted(m.group(0)) is None:
            continue
        out.append({"kind": "THROUGH", "amount": nth - 1,
                    "unit": KINDS["THROUGH"]["unit"],
                    "counted": through_counted(m.group(0)),
                    "benefit": "", "certainty": "",
                    "raw": m.group(0)[:120]})
    return out


def from_table(html: str) -> list:
    """表から天井を採る（ちょんぼりすたの形・★表1区画ずつ★）。

    ★値と恩恵は同じ表の中だけで結びつける★（2026-08-03・Codex59回目）
      平らな行読み＋「後ろ6行の恩恵」は、隣の区画（フリーズ等）の
      恩恵を天井の値に結合できた（合成HTMLで成立）。
      実在形は1つの表に「天井G数|1200G」「恩恵|AT+夢娘チャンス」が
      並ぶ（ちょんぼりすた実ページで確認）ので、表単位で閉じて読む。
    """
    out = []
    for tb in _ht.tables(html):
        if tb.get("has_span"):
            continue          # ★多段見出し（rowspan/colspan）は列がずれる＝不採用★
        for kind, labels in _TABLE_LABELS.items():
            val = _norm(_ht.value_of(tb["pairs"], labels))
            m = re.match(r"^(\d{1,5})\s*" + KINDS[kind]["unit"] + r"$", val)
            if not m:
                continue
            benefit = _norm(_ht.value_of(tb["pairs"], _BENEFIT_LABELS))
            if not (benefit and _BENEFIT_OK.match(benefit)):
                continue        # ★恩恵が取れなければ採らない（値だけ載せない）★
            # ★表題に「何を数えるか」があれば捨てない★（2026-08-03・
            #   Codex61回目。常に counted=None だと「AT間天井」の表が
            #   条件なし票になり、別出典の「通常時」へ寄せられて
            #   条件の違う天井を一致にできた。
            #   「AT天井」のATは恩恵の意味なので、〜間・通常時だけ読む）
            _cm = re.search(r"(通常時|AT間|ボーナス間|CZ間|有利区間)",
                            _norm(tb["title"]))
            out.append({"kind": kind, "amount": int(m.group(1)),
                        "unit": KINDS[kind]["unit"],
                        "counted": _cm.group(1) if _cm else None,
                        "benefit": split_benefit(benefit)[0],
                        "certainty": split_benefit(benefit)[1],
                        "raw": f"{tb['title'][:20]}: {val} / 恩恵={benefit}"})
    return out


def read_page(url: str, official_name: str) -> dict:
    """1ページから天井の一式を採る。★機種が違えば何も採らない★"""
    out = {"url": url, "host": url.split("/")[2].lower().removeprefix("www."),
           "ok": False, "reason": "", "ceilings": [], "cz_names": set()}
    try:
        html = _w._get(url)
    except Exception as e:
        out["reason"] = f"取得できません: {e}"
        return out
    # ★材料の照合も厳格側で★（2026-08-02・Codex55回目。緩い側だと
    #   「機種名 新台 BLACK」のような未知の版名が装飾語の後ろで通り、
    #   別バージョンの値を2媒体一致で採用できた）
    ok, why = _mc.page_is_machine(html, official_name,
                                  strict_all_tail=True)
    if not ok:
        out["reason"] = why
        return out
    text = _w._visible_text(html)
    lines = [x.strip() for x in text.splitlines()]
    seen, got = set(), []
    for c in from_sentences(text) + from_table(html):
        # ★検証済みの恩恵名の対応表で書き方をそろえる★（Codex59回目）
        c["benefit"] = _benefit_alias(c["benefit"], official_name)
        # ★重複判定は事実の全部で★（Codex56〜57回目。
        #   (kind, amount)だけだと、同じG数の「通常時」と「AT間」の
        #   片方がページ内で消え、正しい2出典一致が成立しなかった。
        #   恩恵・確度を外すと、同じページ内の「1200G/AT」と「1200G/CZ」の
        #   後者＝反対情報が先着順で消え、比較の反対票規則が働かなかった）
        key = (c["kind"], c["amount"], c.get("counted"),
               c.get("benefit"), c.get("certainty"))
        if key in seen:
            continue
        seen.add(key)
        got.append(c)
    out["ceilings"] = got
    # ★このページが「CZ＝〇〇」と書いている名前を控える★（突き合わせに使う）
    out["cz_names"] = cz_names_in_page(_w._visible_text(html))
    # ★天井の話がありそうなのに1つも採れないなら OK と言わない★（Codex指摘・再現済み）
    #   採れなかったことを「天井が無い」と読まれると、
    #   別の出典だけで採用してしまう。
    # ★SEO用の題の行は数えない★（2026-08-03・青ブタ実データ）
    #   P-WORLDの未導入ページは本文がまだ無く、題の「…天井 初打ち…」だけで
    #   「記述はあるが採れない（要確認）」の誤警報になっていた。
    _title_line = _w.page_title(html).strip()
    _body = [ln for ln in text.splitlines()
             if ln.strip() and ln.strip() != _title_line]
    looks = any(w in ln for ln in _body
                for w in ("天井", "ゲーム数天井", "周期天井"))
    if looks and not got:
        out["ok"], out["reason"] = False, "天井の記述はあるが採れませんでした（要確認）"
        return out
    out["ok"] = True
    out["reason"] = "OK" if got else "天井の記述がありません"
    return out


def _key(c: dict) -> str:
    """一致を見るための鍵。★恩恵と「何を数えるか」まで含める★

    ★2026-07-31・実際に再現した値漏れ★
      以前は `counted`（何を数えるか）を鍵に入れていなかったため、
      **「通常時1200G」と「AT間1200G」が同じ天井として採用された**。
      この2つはまったく別物で、AT間天井を通常時天井として出すと
      読者は打てない台を打つことになる。
    """
    return json.dumps({k: (c.get(k) or "") for k in ("kind", "amount", "unit",
                                                     "counted", "benefit",
                                                     "certainty")},
                      ensure_ascii=False, sort_keys=True)


def _base_key(c: dict) -> str:
    """「何を数えるか」だけを外した鍵（片方が書いていない場合の突き合わせ用）。"""
    return json.dumps({k: (c.get(k) or "") for k in ("kind", "amount", "unit",
                                                     "benefit", "certainty")},
                      ensure_ascii=False, sort_keys=True)


def _merge_unqualified(votes: dict) -> dict:
    """片方が「何を数えるか」を書いていないだけなら、書いてある方に寄せる。

    ★条件を書いてある方を必ず残す★
      「通常時1200G」と「1200G（条件の記載なし）」は食い違いではない。
      ただし**条件なしの側を採用すると条件が消える**ので、
      条件つきの方にまとめる（消える方向には倒さない）。
      条件つきが2種類ある（通常時とAT間）ときはまとめない＝食い違いとして残す。
    """
    groups: dict = {}
    for k, v in votes.items():
        groups.setdefault(_base_key(v["sample"]), []).append((k, v))
    out: dict = {}
    for _, items in groups.items():
        qualified = [(k, v) for k, v in items if v["sample"].get("counted")]
        plain = [(k, v) for k, v in items if not v["sample"].get("counted")]
        # ★スルー天井はまとめない★（2026-08-06・Codex123回目）
        #   スルーは「何をスルーしたか」が本体（CZ / REG / ボーナス）。
        #   条件なしの票を寄せると、**別の対象どうしが2票に見える**。
        if any(v["sample"].get("kind") == "THROUGH" for _, v in items):
            for k, v in items:
                out[k] = v
            continue
        if len(qualified) == 1 and plain:
            k, v = qualified[0]
            for _, pv in plain:
                v["sources"] |= pv["sources"]
            out[k] = v
        else:
            for k, v in items:
                out[k] = v
    return out


# ★「CZ」と名前が並んで書かれている形★（これ自体が「同じ物だ」という証拠）
#   ★括弧を必須にする／述語まで見る★（2026-08-06・Codex124回目）
#     以前は `CZ中は小役チャンスアップ` から「中は小役チャンス」を名前として拾い、
#     `非CZ「関所チャレンジ」` や `CZ「関所チャレンジ」ではない` も通していた。
_CZ_IS_NAMED = re.compile(
    r"CZ[「『（(](?P<a>[一-龥ぁ-んァ-ヶA-Za-z0-9]{2,12}"
    r"(?:チャレンジ|チャンス|バトル))[」』）)]"
    r"|(?P<b>[一-龥ぁ-んァ-ヶA-Za-z0-9]{2,12}(?:チャレンジ|チャンス|バトル))"
    r"\s*(?:は|が)\s*CZ(?:です|。|、|$)")
# ★この語が同じ文にあれば、CZ名として採らない★
_NOT_CZ = ("非CZ", "ではない", "ではありません", "候補", "示唆", "かもしれ", "とは限")


def cz_names_in_page(text: str) -> set:
    """そのページが「CZ＝〇〇」と書いている名前を集める。

    ★推測しない★（2026-08-06）
      「関所チャレンジ」がCZかどうかは、**ページ自身がそう書いている**
      ことだけを根拠にする。書いていないページの名前は数えない。
    """
    out = set()
    for sent in re.split(r"(?<=。)|\n", _norm(cut_user_area(text))):
        if any(w in sent for w in _NOT_CZ):
            continue                      # 打ち消し・推測の文からは採らない
        for m in _CZ_IS_NAMED.finditer(sent):
            got = m.group("a") or m.group("b")
            if got:
                out.add(_norm(got))
    return out


def verified_cz_names(pages: list) -> list:
    """★独立2出典が同じ名前をCZだと書いている時だけ採る★"""
    from collections import Counter
    cnt = Counter()
    for p in pages:
        if not p.get("ok"):
            continue
        lin = _sl._lineage(p["host"])
        for nm in (p.get("cz_names") or set()):
            cnt[(nm, lin)] += 1
    by_name: dict = {}
    for (nm, lin) in cnt:
        by_name.setdefault(nm, set()).add(lin)
    return sorted(nm for nm, lins in by_name.items() if len(lins) >= 2)


def apply_cz_aliases(items: list, cz_names, page_names=None) -> list:
    """★確かめたCZ名を「CZ」に寄せる★（2026-08-06）

    ★寄せてよいのは「CZが1種類しか出てこない」時だけ★（Codex124回目）
      「〇〇はCZである」は**分類**の証拠でしかなく、天井文の「CZ」が
      その名前を指す証拠にはならない。CZが2種類ある機種では
        出典A: 関所チャレンジ6スルー ／ 出典B: 真剣チャレンジ6スルー
      が「CZの2票」に化ける。名前が1つに定まらない時は寄せない。
    """
    names = {_norm(str(n)) for n in (cz_names or []) if str(n).strip()}
    if len(names) != 1:
        return list(items)                # 1種類に定まらないなら寄せない
    if page_names is not None and len({_norm(str(x)) for x in page_names}) > 1:
        return list(items)                # そのページに複数のCZ名がある
    out = []
    for it in items:
        c = it.get("counted")
        if it.get("kind") == "THROUGH" and c and _norm(str(c)) in names:
            it = {**it, "counted": "CZ", "counted_as_written": c}
        out.append(it)
    return out


def compare(pages: list, cz_names=None) -> dict:
    """★値・種類・恩恵がすべて一致したものだけ採る★"""
    votes: dict = {}
    for p in pages:
        if not p.get("ok"):
            continue
        lin = _sl._lineage(p["host"])
        # ★確かめたCZ名だけを「CZ」に寄せてから数える★（2026-08-06）
        for c in apply_cz_aliases(p["ceilings"], cz_names,
                                  page_names=p.get("cz_names")):
            votes.setdefault(_key(c), {"sample": c, "sources": set()})
            votes[_key(c)]["sources"].add(lin)
    votes = _merge_unqualified(votes)
    adopted, need_third = [], []
    # 同じ種類で値が割れていないかも見る（1200Gと1500Gが両方2票、はありえない）
    # ★束ねる単位は (kind, counted)★（2026-08-02・Codex56回目。
    #   kindだけだと「通常時」「AT間」の2つのG数天井が両方2出典一致でも
    #   互いに食い違い扱いになり、正しい情報を全部落としていた）
    by_kind: dict = {}
    for k, v in votes.items():
        by_kind.setdefault(
            (v["sample"]["kind"], v["sample"].get("counted")),
            []).append((k, v))
    # ★「条件なし」と「条件つき」が同じ種類に併存したら、その種類は全部保留★
    #   （2026-08-02・Codex56回目の反対票規則の一部。寄せ先が一意なら
    #     _merge_unqualified が済ませている＝ここに残る条件なしは
    #     どの天井の票か決められない。反対票かもしれない声を無視しない）
    _has_counted = {k for (k, c) in by_kind if c is not None}
    _has_plain = {k for (k, c) in by_kind if c is None}
    _ambiguous = _has_counted & _has_plain
    for (kind, _cnt), items in by_kind.items():
        agreed = [(k, v) for k, v in items if len(v["sources"]) >= 2]
        # ★反対票が1票でもあれば採らない★（2026-08-02・Codex56回目）
        if len(agreed) == 1 and len(items) == 1 and kind not in _ambiguous:
            c = dict(agreed[0][1]["sample"])
            c["sources"] = sorted(agreed[0][1]["sources"])
            adopted.append(c)
        else:
            need_third.append({
                "kind": kind, "jp": KINDS[kind]["jp"],
                "why": ("出典が食い違っています" if len(agreed) != 1 and len(items) > 1
                        else "1つの出典にしかありません"),
                "candidates": [{"amount": v["sample"]["amount"],
                                "counted": v["sample"].get("counted"),
                                "benefit": v["sample"]["benefit"],
                                "sources": sorted(v["sources"])} for _, v in items]})
    return {"adopted": adopted, "need_third": need_third}


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []
    nl = chr(10)

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    S = ("天井は周期天井とゲーム数天井の2種類が存在する。"
         "各キャラの娘ポイントが最大10周期に到達すると周期天井となり、CZに当選する。"
         "通常時を最大1200G消化するとゲーム数天井に到達し、ATに当選する。")
    got = from_sentences(S)
    g = next((x for x in got if x["kind"] == "GAME"), None)
    c = next((x for x in got if x["kind"] == "CYCLE"), None)
    t("★★文章から 値・数える対象・恩恵 を一式で採る★★",
      g and g["amount"] == 1200 and g["counted"] == "通常時" and g["benefit"] == "AT")
    t("　周期天井も採れる", c and c["amount"] == 10 and c["benefit"] == "CZ")
    t("★恩恵が文になっていたら採らない（短い語だけ）★",
      from_sentences("通常時を最大1200G消化するとゲーム数天井に到達し、"
                     "状況によっては上位ATに直行する場合もあるとされているものに当選") == [])

    LH = ("<h3>AT天井</h3><table>"
          "<tr><th>天井G数</th><td>1200G</td></tr>"
          "<tr><th>恩恵</th><td>AT当選</td></tr></table>")
    tb = from_table(LH)
    t("★★表からも同じ形で採れる★★（実在形＝1つの表に値と恩恵）",
      tb and tb[0]["amount"] == 1200 and tb[0]["benefit"] == "AT")
    t("★★表題の「AT間」を捨てない★★"
      "（条件なし票として通常時へ寄せられた・Codex61回目）",
      from_table("<h3>AT間天井</h3><table>"
                 "<tr><th>天井G数</th><td>1200G</td></tr>"
                 "<tr><th>恩恵</th><td>AT当選</td></tr></table>"
                 )[0]["counted"] == "AT間"
      and from_table(LH)[0]["counted"] is None)
    t("★★非表示の天井表を採らない★★（Codex63回目）",
      from_table("<h3>AT天井</h3><table>"
                 "<tr><th>天井G数</th><td>1200G</td></tr>"
                 "<tr><th>恩恵</th><td>AT当選</td></tr></table>"
                 "<div hidden><h3>旧天井</h3><table>"
                 "<tr><th>天井G数</th><td>1500G</td></tr>"
                 "<tr><th>恩恵</th><td>AT当選</td></tr></table></div>"
                 )[0]["amount"] == 1200
      and len(from_table("<div hidden><table>"
                         "<tr><th>天井G数</th><td>1500G</td></tr>"
                         "<tr><th>恩恵</th><td>AT当選</td></tr></table></div>")) == 0)
    t("★★別区画の恩恵を天井に結合しない★★（Codex59回目・合成HTML）",
      from_table("<h3>AT天井</h3><table>"
                 "<tr><th>天井G数</th><td>1200G</td></tr></table>"
                 "<h3>フリーズ</h3><table>"
                 "<tr><th>恩恵</th><td>上位AT</td></tr></table>") == [])
    t("★★『AT』と『AT当選』を同じ恩恵として扱う★★（実データの差）",
      normalize_benefit("AT当選") == normalize_benefit("AT") == "AT"
      and normalize_benefit("CZ当選濃厚") == "CZ")
    t("　意味の違う語まで揃えない（『AT』と『上位AT』は別）",
      normalize_benefit("上位AT当選") != normalize_benefit("AT当選"))
    t("　見出しの飾りを落として数える対象にする",
      normalize_counted("▼ゲーム数天井 通常時") == "通常時")
    t("★★恩恵が無ければ採らない（値だけ載せない）★★",
      from_table("<table><tr><th>天井G数</th><td>1200G</td></tr>"
                 "<tr><th>解説</th><td>通常時の抽選</td></tr></table>") == [])
    t("　単位が合わなければ採らない",
      from_table("<table><tr><th>天井G数</th><td>1200pt</td></tr>"
                 "<tr><th>恩恵</th><td>AT当選</td></tr></table>") == [])

    A = {"url": "https://www.p-world.co.jp/x", "host": "p-world.co.jp", "ok": True,
         "ceilings": [{"kind": "GAME", "amount": 1200, "unit": "G",
                       "counted": "通常時", "benefit": "AT", "raw": ""}]}
    B = {"url": "https://chonborista.com/y", "host": "chonborista.com", "ok": True,
         "ceilings": [{"kind": "GAME", "amount": 1200, "unit": "G",
                       "counted": None, "benefit": "AT", "raw": ""}]}
    r = compare([A, B])
    t("★2出典で値も恩恵も一致すれば採用★",
      len(r["adopted"]) == 1 and r["adopted"][0]["amount"] == 1200)
    C = {**B, "ceilings": [{**B["ceilings"][0], "benefit": "CZ"}]}
    r2 = compare([A, C])
    t("★★値が同じでも恩恵が違えば採らない★★（値だけ合っても採用しない）",
      not r2["adopted"] and r2["need_third"])
    D = {**B, "ceilings": [{**B["ceilings"][0], "amount": 1500}]}
    r3 = compare([A, D])
    t("　値が違えば採らない", not r3["adopted"])
    t("　1出典だけなら採らない", not compare([A])["adopted"])
    E = {**B, "host": "p-world.co.jp"}
    t("★同じ運営元の2ページを2票と数えない★", not compare([A, E])["adopted"])
    t("　機種が違うページの内容は混ぜない",
      not compare([{**A, "ok": False}, B])["adopted"])


    mk = lambda h, cnt: {"url": "https://" + h + "/x", "host": h, "ok": True,
                         "ceilings": [{"kind": "GAME", "amount": 1200, "unit": "G",
                                       "counted": cnt, "benefit": "AT",
                                       "certainty": "PLAIN", "raw": ""}]}
    t("★★『通常時1200G』と『AT間1200G』を同じ天井にしない★★（実際に起きた値漏れ）",
      not compare([mk("p-world.co.jp", "通常時"), mk("chonborista.com", "AT間")])["adopted"])
    _r = compare([mk("chonborista.com", None), mk("p-world.co.jp", "通常時")])
    t("★★片方が条件を書いていないだけなら、条件つきの方を残す★★（条件を消さない）",
      len(_r["adopted"]) == 1 and _r["adopted"][0]["counted"] == "通常時")
    t("　その場合も2出典ぶんの票として数える",
      len(_r["adopted"][0]["sources"]) == 2)

    # ★★Codex56回目★★
    mk3 = lambda h: {"url": "https://" + h + "/x", "host": h, "ok": True,
                     "ceilings": [
                         {"kind": "GAME", "amount": 800, "unit": "G",
                          "counted": "通常時", "benefit": "AT",
                          "certainty": "PLAIN", "raw": ""},
                         {"kind": "GAME", "amount": 1200, "unit": "G",
                          "counted": "AT間", "benefit": "AT",
                          "certainty": "PLAIN", "raw": ""}]}
    r56 = compare([mk3("p-world.co.jp"), mk3("chonborista.com")])
    t("★★数える対象が違う2つのG数天井は、両方2出典一致なら両方採る★★"
      "（kindだけで束ねると互いを食い違い扱いにして全部落とした・Codex56回目）",
      len(r56["adopted"]) == 2
      and {c["counted"] for c in r56["adopted"]} == {"通常時", "AT間"})
    F = {**B, "url": "https://p-town.dmm.com/z", "host": "p-town.dmm.com",
         "ceilings": [{"kind": "GAME", "amount": 1500, "unit": "G",
                       "counted": None, "benefit": "AT", "raw": ""}]}
    t("★★2票一致でも反対票が1票あれば採らない★★（Codex56回目）",
      not compare([A, B, F])["adopted"])
    _g = globals()
    _real_fs, _real_ft = from_sentences, from_table
    _real_get, _real_pim = _w._get, _mc.page_is_machine
    try:
        _g["from_sentences"] = lambda text: [
            {"kind": "GAME", "amount": 1200, "unit": "G", "counted": "通常時",
             "benefit": "AT", "certainty": "PLAIN", "raw": ""},
            {"kind": "GAME", "amount": 1200, "unit": "G", "counted": "AT間",
             "benefit": "AT", "certainty": "PLAIN", "raw": ""}]
        _g["from_table"] = lambda lines: []
        _w._get = lambda u, timeout=20: "<title>x</title><body>天井</body>"
        _mc.page_is_machine = lambda *a, **k: (True, "OK")
        _p56 = read_page("https://x.example/1", "L試験機")
        t("★★同じG数で数える対象が違う天井を、ページ内で両方残す★★"
          "（(kind, amount)の重複判定で片方が消えていた・Codex56回目）",
          len(_p56["ceilings"]) == 2)
        # ★同じ(kind,amount,counted)で恩恵だけ違う併記＝ページ内の反対情報★
        _g["from_sentences"] = lambda text: [
            {"kind": "GAME", "amount": 1200, "unit": "G", "counted": "通常時",
             "benefit": "AT", "certainty": "PLAIN", "raw": ""},
            {"kind": "GAME", "amount": 1200, "unit": "G", "counted": "通常時",
             "benefit": "CZ", "certainty": "PLAIN", "raw": ""}]
        _p57 = read_page("https://x.example/1", "L試験機")
        t("★★恩恵違いの併記を先着順で消さない★★"
          "（片方が消えると反対票規則が働かず誤採用できた・Codex57回目）",
          len(_p57["ceilings"]) == 2)
        _pB = {"url": "https://chonborista.com/y", "host": "chonborista.com",
               "ok": True,
               "ceilings": [{"kind": "GAME", "amount": 1200, "unit": "G",
                             "counted": "通常時", "benefit": "AT",
                             "certainty": "PLAIN", "raw": ""}]}
        _p57["ok"] = True
        t("　その併記ページ＋AT側1票では採用しない（反対票として効く）",
          not compare([_p57, _pB])["adopted"])
    finally:
        _g["from_sentences"], _g["from_table"] = _real_fs, _real_ft
        _w._get, _mc.page_is_machine = _real_get, _real_pim
    # ★スルー天井★（2026-08-06。両名鑑が書いているのに拾えていなかった）
    TH = ("▼CZスルー回数天井 CZでAT非当選が最大6回続くと天井到達。"
          "到達時は次回(最大7回目)のCZでATに当選する。")
    got = [g for g in from_sentences(TH) if g["kind"] == "THROUGH"]
    t("★★スルー天井を採れる★★（CZ6回スルー）",
      got and all(g["amount"] == 6 for g in got))
    t("★★「N回目で確定」は (N-1) スルーとして採る★★（数え方の取り違え防止）",
      [g["amount"] for g in from_sentences("7回目のCZでAT当選確定")
       if g["kind"] == "THROUGH"] == [6])
    t("★★掲示板の書き込みは読まない★★（推測や誤りが混じる）",
      not [g for g in from_sentences(
          "本文です。" + chr(10) + "掲示板" + chr(10)
          + "7回目のCZでAT当選だと思います") if g["kind"] == "THROUGH"])
    t("★★目次と本当の掲示板が両方あっても、本文は落とさない★★"
      "（語ごとの最後を集めて最小を取ると本文ごと落ちた・Codex123回目）",
      [g["amount"] for g in from_sentences(
          "掲示板" + chr(10) + "口コミ" + chr(10) + TH + chr(10)
          + "掲示板" + chr(10) + "7回目だと思う")
       if g["kind"] == "THROUGH"] == [6])
    t("★★逆の意味・実績・N回目スルーは採らない★★（Codex123回目・再現した）",
      not from_sentences("CZ7回目でもAT非当選")
      and not from_sentences("7回目のCZでATに当選した")
      and not from_sentences("CZ7回目のスルーで天井"))
    t("　保証の言い方があるものは採る",
      [g["amount"] for g in from_sentences("CZ7回目は天井なので必ずAT当選")
       if g["kind"] == "THROUGH"] == [6])
    t("　1回目・21回目のような数え方は採らない",
      not [g for g in from_sentences("1回目で当選") if g["kind"] == "THROUGH"])

    # ★CZ名で突き合わせる★（2026-08-06・運営者の指摘から）
    t("★★『CZ＝〇〇』と書いてある名前だけを拾う★★",
      cz_names_in_page("CZ「関所チャレンジ」に突入") == {"関所チャレンジ"}
      and cz_names_in_page("あっぱれチャンスへ移行") == set())
    t("★★独立2出典が同じ名前をCZだと書いた時だけ採る★★",
      verified_cz_names([
          {"ok": True, "host": "a.example", "cz_names": {"関所チャレンジ"}},
          {"ok": True, "host": "b.example", "cz_names": {"関所チャレンジ"}}])
      == ["関所チャレンジ"]
      and verified_cz_names([
          {"ok": True, "host": "a.example", "cz_names": {"関所チャレンジ"}}]) == [])
    t("★★確かめていない名前は寄せない★★（別のCZと取り違えない）",
      apply_cz_aliases([{"kind": "THROUGH", "counted": "真剣チャレンジ"}],
                       ["関所チャレンジ"])[0]["counted"] == "真剣チャレンジ")
    t("　確かめた名前は CZ に寄せる（元の書き方も残す）",
      apply_cz_aliases([{"kind": "THROUGH", "counted": "関所チャレンジ"}],
                       ["関所チャレンジ"])[0]["counted"] == "CZ")
    t("★★『Nスルー後M回目』は数が合う時だけ採る★★",
      [g["amount"] for g in from_sentences("関所チャレンジ6スルー後、7回目の関所チャレンジで勝率が100%となる。")
       if g["kind"] == "THROUGH"] == [6]
      and not [g for g in from_sentences("関所チャレンジ6スルー後、9回目の関所チャレンジ")
               if g["kind"] == "THROUGH"])

    # ★Codex124回目の指摘★
    t("★★CZが2種類あるページからは寄せない★★（別のCZを2票にしない）",
      apply_cz_aliases([{"kind": "THROUGH", "counted": "関所チャレンジ"}],
                       ["関所チャレンジ", "真剣チャレンジ"])[0]["counted"]
      == "関所チャレンジ"
      and apply_cz_aliases([{"kind": "THROUGH", "counted": "関所チャレンジ"}],
                           ["関所チャレンジ"],
                           page_names={"関所チャレンジ", "真剣チャレンジ"}
                           )[0]["counted"] == "関所チャレンジ")
    t("★★CZ名の誤認を防ぐ★★（括弧必須・打ち消し・接尾辞）",
      cz_names_in_page("CZ中は小役チャンスアップ") == set()
      and cz_names_in_page("非CZ「関所チャレンジ」") == set()
      and cz_names_in_page("CZ「関所チャレンジ」ではない。") == set()
      and cz_names_in_page("CZ「関所チャレンジ」に突入。") == {"関所チャレンジ"})
    t("★★打ち消しは文の最後まで見る★★（『天井ではない』が素通りしていた）",
      not [g for g in from_sentences("CZ7回目は天井ではない。")
           if g["kind"] == "THROUGH"])
    t("★★『非当選が続くと天井』は打ち消しではない★★"
      "（打ち消し扱いにして正しい出典を1つ失っていた・自分の試験で発覚）",
      [g["amount"] for g in from_sentences("CZでAT非当選が最大6回続くと天井到達。")
       if g["kind"] == "THROUGH"] == [6])

    ng = [n for n, ok in results if not ok]
    print(f"{nl}{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--name")
    ap.add_argument("--url", action="append")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not (args.name and args.url):
        ap.print_help()
        return 0
    pages = [read_page(u, args.name) for u in args.url]
    for p in pages:
        print(f"{p['host']:20} {p['reason']:22} 天井 {len(p['ceilings'])} 件")
        for c in p["ceilings"]:
            print(f"     {KINDS[c['kind']]['jp']}: {c['amount']}{c['unit']}"
                  f" / 恩恵={c['benefit']} / 数える対象={c['counted']}")
    r = compare(pages)
    print(chr(10) + json.dumps(r, ensure_ascii=False, indent=1))
    return 0 if r["adopted"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(f"★想定外の失敗 {type(e).__name__}: {e}★")
        raise SystemExit(1)

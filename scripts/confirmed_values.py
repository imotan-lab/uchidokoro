# -*- coding: utf-8 -*-
"""2AIで突き合わせて確定した値を、記事の材料として受け取る口。

★なぜ要るか（2026-08-09・台帳#273）★
  機械の抽出は「載っているのに読めない」が普通に起きる。
  実測: パリピ孔明は名鑑4件すべてに天井の記述があるのに、
  4件とも「記述はあるが採れませんでした」で0件だった。
  そのため4夜連続で1件も公開できなかった。

  手順書には2AI突き合わせ（新台=STEP 3-B / 更新=STEP 2〜5）があるのに、
  **そこで確定した値を材料として受け取る場所が無かった**。
  だから機械が読めない機種は、何度回しても永久に空のまま公開され続ける。

★守る線（release_overrides と同じ形）★
  ┌────────────────────────────────────────────────┐
  │ ①2人（ClaudeとCodex）が同じ原文を読んで一致したこと│
  │ ②その根拠（出典URLと逐語の引用）が残っていること   │
  │ ③記録できるのは対話セッションだけ（無人は読むだけ）│
  └────────────────────────────────────────────────┘
  ★出典は独立2系列★＝同じ発行者の2ページは1票（source_lineage で数える）。
  ★値を発明しない★＝引用に現れない値は記録できない（機械が確かめる）。

置き場: C:/Users/imao_/Documents/uchidokoro/confirmed_values.json
        （リポジトリ外・Dropboxへ保全）

使い方:
  # 記録する（対話セッションのみ）
  python scripts/confirmed_values.py --record --slug prskkm --field ceiling \\
      --value-file <値のJSON> --source "p-world|https://…|天井は1000G+α" \\
      --source "nana-press|https://…|通常時1000G+αで天井" \\
      --by claude,codex --why "同じ原文を読んで一致"
  python scripts/confirmed_values.py --list [--slug prskkm]
  python scripts/confirmed_values.py --forget --slug prskkm --field ceiling
  python scripts/confirmed_values.py --selftest
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import safe_json as _sj              # noqa: E402
import source_lineage as _sl         # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE = r"C:/Users/imao_/Documents/uchidokoro/confirmed_values.json"
SCHEMA = "confirmed-values/v1"

# ★2人そろって初めて記録できる★（片方だけの読みは採らない）
REQUIRED_JUDGES = ("claude", "codex")
MIN_QUOTE = 6            # 逐語の引用がこれより短いものは根拠にしない
MIN_WHY = 8              # 「なぜ同じ機種か」は文になる長さを求める（控えと同じ）

# ★どの項目を、材料のどこへ入れるか★（2026-08-09・依頼130 P0-1）
#   最初の版は全部を material["adopted"] に入れていたが、
#   記事が天井を読むのは material["ceilings"]["adopted"] で、
#   しかも add_machine_run が spec_lookup.FIELDS を引くため
#   **記録した瞬間に KeyError で落ちた**（実機で確認）。
#   置き場を明示し、知らない項目は受け取らない。
FIELD_TARGETS = {
    "ceiling": "ceilings",      # 天井（1件ずつ）
    "at": "at_specs",           # ATの仕様
    "cz": "czs",                # CZ
    # ★朝一・リセット★（2026-08-12・運営者決定で箱を埋めることにした）
    #   原文を集める側には前から話題があったのに、値を受け取る器が無く、
    #   **情報が揃っても永久に空のまま**だった。
    "reset": "resets",
    # ★ゲームの流れ★（2026-08-13・台帳#344）
    #   導入前〜直後の新台は「名前と流れが先に出て、数値は後」。
    #   数値が要る器しか無かったので、いちばん鮮度が価値になる時期に
    #   2出典で一致しているものを書けなかった。
    "gameplay": "gameplays",
}
# 基本スペック側（spec_lookup.FIELDS の鍵）はそのまま adopted へ入る


# ★2AIだけが答えられる項目★（2026-08-12・運営者決定「人が直す項目をなくす」）
#   機械の側で決めようとすると場合分けが増えるだけなので、
#   「機械は質問を出す・2AIが答えて記録する」形にする。
AI_ONLY_FIELDS = {
    # 天井が複数ある機種（通常時／AT間／スルー）で、
    # 早見表の「天井まで残り」に使う値はどれか。
    "checker_ceiling": "adopted",
}


def allowed_fields() -> dict:
    """受け取ってよい項目 → 入れ先。"""
    import spec_lookup as _sp
    out = {k: "adopted" for k in _sp.FIELDS}
    out.update(FIELD_TARGETS)
    out.update(AI_ONLY_FIELDS)
    return out


# ★項目ごとに「値の形」を決める★（2026-08-09・依頼131 P0-3）
#   項目名しか見ていなかったので、benefit の無い天井を記録でき、
#   そのあと記事生成が c["benefit"] で落ち続ける状態になっていた。
#   ★引用と照合する表示値★も項目ごとに決める（内部の記号は照合しない）。
VALUE_SHAPES = {
    # ★早見表に使う通常時の天井★（2026-08-12）
    #   天井が複数ある機種で、2AIが「通常時の天井はこれ」と決めた値。
    #   なぜその値かを --why に必ず残す。
    "checker_ceiling": {"required": ("games",), "enums": {},
                        "quoted": ("games",)},
    "ceiling": {"required": ("kind", "amount", "unit", "benefit"),
                "enums": {"kind": ("GAME", "CYCLE", "POINT")},
                "quoted": ("amount", "unit")},
    # ★どれか1つでも確認できていればよい★（2026-08-09）
    #   継続率しか公表されていない機種が実在する（パリピ孔明）。
    #   3つとも必須にすると、確かに2出典で一致した継続率まで記録できない。
    "at": {"required": ("mode",), "any_of": ("games", "net", "loop_rate"),
           "enums": {"mode": ("MAIN_AT", "UPPER_AT")},
           "quoted": ("games", "net", "loop_rate")},
    # ★朝一・リセット★（2026-08-12）
    #   種類だけは必ず要る。中身はどれか1つでもあればよい
    #   （天井が短くなる機種／朝一の状態だけ分かる機種、どちらもある）。
    # ★種類ごとに、要る中身を決める★（2026-08-12・依頼160のP1-4）
    #   以前は「どれか1つあればよい」だったので、
    #   {kind: CEILING_SHORTENED, state: "高確スタート"} が検査を通り、
    #   記事側は games しか読まないので**1行も出ない**（確定値が消える）。
    "reset": {"required": ("kind",),
              "required_by_kind": {"CEILING_SHORTENED": ("games",),
                                   "MORNING_STATE": ("state",),
                                   "ADVANTAGE_RESET": ("state",)},
              "enums": {"kind": ("CEILING_SHORTENED", "MORNING_STATE",
                                 "ADVANTAGE_RESET")},
              "quoted": ("games", "state")},
    # ★CZは名前だけでなく、書いた項目は全部引用と照合する★（依頼134）
    #   記事は継続G数と期待度も出すので、書くなら根拠が要る。
    "cz": {"required": ("name",), "enums": {},
           "quoted": ("name", "games", "rate")},
    # ★ゲームの流れは構造にして受ける★（2026-08-13・台帳#344）
    #   ★自由文で受けない★＝逐語の照合だけでは
    #     「否定文か」「条件と結果の向き」「必ずと抽選の違い」
    #     「離れた二文をつないで新しい因果を作っていないか」を確かめられない。
    #   機械は引用の実在と固有語の在処だけを見て、
    #   関係の意味は2AIが決め、記事は定型文にして書く。
    #   ★1件＝独立して真偽を判定・更新できる最小命題★
    #     「通常時は周期抽選からCZへ突入し、ATを目指す」は2件（CZへの入り方／AT名）。
    #     1つの条件から結果が2つ出ても1件（配列の数でclaimを増やさない）。
    #   ★gains も引用と照合する★（2026-08-13・依頼177のP1）
    #     記事に「（上乗せ・武将参戦を獲得）」と断定して出すのに、
    #     照合の対象から外れていた＝根拠に無い獲得内容を書けた。
    "gameplay": {"required": ("trigger", "leads_to"), "enums": {},
                 "quoted": ("trigger", "leads_to", "when", "gains")},
    # ★AT名と対応の付かない純増★（2026-08-13・台帳#344）
    #   出典が「純増約3.1枚or約7.4枚/G」としか書かず、どちらがメインで
    #   どちらが上位か割り当てていないとき、**モードへ割り当てない**。
    #   ★順番に並べると読者が対応を推測する★ので、記事側は
    #   「AT名との対応は未確認」と明記して並べる。
    "at_net_unmapped": {"required": ("values",), "enums": {},
                        "quoted": ()},
}


def base_field(field: str) -> str:
    """「at#パーティータイム」→「at」。

    ★なぜ要るか（2026-08-09）★
      1機種にATもCZも複数ある（メインST・上位ST・究極ST…）。
      項目名を1つしか持てないと、2出典で一致した2つ目以降を捨てることになる。
      見出しを付けて複数を持てるようにし、入れ先は「#」の前で決める。
    """
    return str(field or "").split("#", 1)[0]


# ★項目ごとの「値の見た目」★（2026-08-10・依頼132 P0-2）
#   以前は「空でない文字列」しか見ていなかったので、次が通った:
#     loop_rate に「4.2枚/G」→ 記事に「継続率4.2枚/G」と出る
#     net に「73%」        → 記事に「純増約73%枚」と出る
#   単位は記事側だけが付けるので、値の側で単位の種類を必ず確かめる。
import re as _re                          # noqa: E402

# ★値には単位を書かせない★（2026-08-10・依頼134 P0-1）
#   記事側が「約」「枚」「G」を付けるので、値にも付いていると
#   「純増約約2.8枚枚」のような文が出る（実際に通る形だった）。
#   ★項目ごとに分ける★（依頼134 P1）＝ATの継続G数は「30」、
#   CZの継続G数は「4G+α」が正しい形で、同じ鍵名でも意味が違う。
VALUE_PATTERNS = {
    "at": {
        "games": (_re.compile(r"^\d{1,4}(\+α)?$"),
                  "ゲーム数（単位は書かない。例: 30 / 30+α）"),
        "net": (_re.compile(r"^\d{1,2}(\.\d)?$"),
                "純増の数だけ（単位も『約』も書かない。例: 2.8）"),
        "loop_rate": (_re.compile(r"^約?\d{1,3}(\.\d)?%$"),
                      "継続率（％を付ける。例: 約73%）"),
    },
    "cz": {
        "games": (_re.compile(r"^\d{1,4}\s*[GＧ](\+α)?$"),
                  "CZの継続G数（例: 4G+α）"),
        "rate": (_re.compile(r"^約?\d{1,3}(\.\d)?%$"), "期待度（例: 約85%）"),
    },
    "ceiling": {
        "amount": (_re.compile(r"^\d{1,5}$"), "数だけ（単位は unit に書く）"),
        "unit": (_re.compile(r"^[GＧ]|pt|周期|スルー|まいる$"), "単位"),
    },
    # ★早見表に使う通常時の天井★（2026-08-12）
    #   「天井まで残り」を引き算に使うので、+α のような幅は受け取らない。
    "checker_ceiling": {
        "games": (_re.compile(r"^\d{2,5}$"),
                  "数だけ（+αや単位は書かない。例: 1000）"),
    },
    # ★朝一・リセット★（2026-08-12）
    "reset": {
        "games": (_re.compile(r"^\d{1,4}(\+α)?$"),
                  "短縮後のゲーム数（単位は書かない。例: 600 / 600+α）"),
        "state": (_re.compile(r"^.{2,30}$"),
                  "朝一の状態（例: 高確からスタート）"),
    },
}


def _ci_mod():
    import claim_inventory as _ci
    return _ci


def check_spec_shape(field: str, value) -> list:
    """基本スペック側の形を spec_lookup の決まりで確かめる。

    ★以前は「形を持っている」とコメントに書いただけで、実際には
      呼んでいなかった★（2026-08-10・依頼132 P0-3）。
      文字列で記録できてしまい、記事側は辞書として読むので落ち続けた。
    """
    import spec_lookup as _sp
    spec = _sp.FIELDS.get(field) or {}
    kind = spec.get("kind")
    if kind == "range":
        # ★抽出器と同じ検査を通す★（2026-08-10・依頼134 P0-2）
        #   以前は鍵があるかしか見ておらず、low/high が空でも通った。
        #   空文字はどの引用にも含まれる扱いになるので照合もすり抜けた。
        if not (isinstance(value, dict)
                and all(k in value for k in ("low", "high", "unit"))):
            raise ConfirmedError(
                f"{field}: low / high / unit を持つ組で書きます（記事がこれを読みます）")
        norm = _sp.normalize_range("%s%% 〜 %s%%" % (value["low"], value["high"]))
        if not norm:
            raise ConfirmedError(
                f"{field}: 範囲として読めません（低い方→高い方・50〜200%%の間）: {value}")
        return [str(value["low"]), str(value["high"])]
    if kind == "games":
        if not (isinstance(value, dict) and "games" in value):
            raise ConfirmedError(f"{field}: games を持つ組で書きます")
        if not _sp.normalize_games("%sG" % value["games"]):
            raise ConfirmedError(
                f"{field}: G数として読めません（5〜100の数）: {value}")
        return [str(value["games"])]
    if kind == "per_setting":
        if not isinstance(value, dict) or not value:
            raise ConfirmedError(f"{field}: 設定ごとの組で書きます（例: 1 → 値）")
        unit = spec.get("unit") or ""
        for k, v in value.items():
            if not _re.match(r"^[1-6]$", str(k)):
                raise ConfirmedError(
                    f"{field}: 設定は1〜6で書きます（いま {k!r}）")
            if _ci_mod().normalize_value(str(v), unit) is None:
                raise ConfirmedError(
                    f"{field}: 設定{k}の値が単位（{unit}）に合いません: {v!r}")
        return [str(v) for v in value.values()]
    toks = _tokens(value)
    if not toks:
        raise ConfirmedError(f"{field}: 確かめられる値がありません")
    return toks


def check_shape(field: str, value) -> list:
    """値の形を確かめ、★引用と照合すべき表示値★を返す。"""
    shape = VALUE_SHAPES.get(base_field(field))
    if not shape:
        return check_spec_shape(base_field(field), value)
    if not isinstance(value, dict):
        raise ConfirmedError(f"{field}: 値は組（辞書）で書きます")
    for k in shape["required"]:
        if k not in value or str(value[k] or "").strip() == "":
            raise ConfirmedError(f"{field}: 「{k}」が要ります（記事がこれを読みます）")
    for k, ok in shape["enums"].items():
        if value.get(k) not in ok:
            raise ConfirmedError(
                f"{field}: 「{k}」は {'/'.join(ok)} のどれかです（いま {value.get(k)!r}）")
    any_of = shape.get("any_of") or ()
    if any_of and not any(str(value.get(k) or "").strip() for k in any_of):
        raise ConfirmedError(
            f"{field}: {'/'.join(any_of)} のどれか1つは要ります（中身の無い項目は作らない）")
    # ★種類ごとに要る中身を確かめる★（2026-08-12・依頼160のP1-4）
    #   記事は種類ごとに読む鍵が決まっているので、種類と中身が食い違うと
    #   検査は通るのに**記事には1行も出ない**（確定値が黙って消える）。
    by_kind = shape.get("required_by_kind") or {}
    need = by_kind.get(value.get("kind"))
    if need:
        miss = [k for k in need if not str(value.get(k) or "").strip()]
        if miss:
            raise ConfirmedError(
                f"{field}: {value.get('kind')} には "
                f"{'/'.join(need)} が要ります（記事がこれを読みます）")
        # ★その種類では読まない鍵を受け取らない★（2026-08-12・依頼161）
        #   記事は種類だけで書き分けるので、種類に関係ない鍵を入れると
        #   **2出典で確かめた中身が黙って消える**（記事に1行も出ない）。
        used = set(shape.get("required") or ()) | set(need)
        others = {k for keys in by_kind.values() for k in keys}
        extra = sorted(k for k in others - used
                       if str(value.get(k) or "").strip())
        if extra:
            raise ConfirmedError(
                f"{field}: {value.get('kind')} では "
                f"{'/'.join(extra)} は使いません（記事に出ないので受け取りません）")
    # ★単位の種類を確かめる★（依頼132 P0-2／依頼134で項目ごとに分けた）
    for k, (pat, jp) in (VALUE_PATTERNS.get(base_field(field)) or {}).items():
        v = str(value.get(k) or "").strip()
        if v and not pat.match(v):
            raise ConfirmedError(f"{field}: 「{k}」は{jp}の形で書きます（いま {v!r}）")
    # ★引用と照合するのは、実際に書いた項目だけ★
    # ★配列は要素ごとに照合する★（2026-08-13・依頼181のP1）
    #   以前は str() で丸ごと1つの語にしていたので、引用に
    #   「['上乗せ', '武将参戦']」というPythonの書き方が無い限り必ず落ちた
    #   ＝gains を含む正しい材料を1件も記録できなかった。
    out = []
    for k in shape["quoted"]:
        v = value.get(k)
        if isinstance(v, (list, tuple)):
            out += [str(x).strip() for x in v if str(x or "").strip()]
        elif str(v or "").strip():
            out.append(str(v).strip())
    return out


class ConfirmedError(Exception):
    """確定値に関する異常（★迷ったら記録しない★）。"""


def _empty() -> dict:
    return {"schema_version": SCHEMA, "machines": {}}


def load() -> dict:
    if not os.path.exists(STORE):
        return _empty()
    got = _sj.read_json(STORE, expect=dict)
    if got.get("schema_version") != SCHEMA:
        raise ConfirmedError(f"確定値の形が違います: {got.get('schema_version')}")
    got.setdefault("machines", {})
    return got


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, STORE)


def parse_source(spec: str) -> dict:
    """`URL|逐語の引用` を組に分ける。

    ★発行者は名乗らせない★（2026-08-09・依頼130 P0-2）
      以前は `発行者|URL|引用` と自己申告させていたので、
      登録済みの発行者名を**別ホストのURLに付けて**通せた。
      発行者はURLのホストから機械が引く。
    """
    parts = [x.strip() for x in str(spec or "").split("|", 1)]
    if len(parts) != 2 or not all(parts):
        raise ConfirmedError(
            "出典は URL|逐語の引用 の形で書きます: " + str(spec)[:60])
    url, quote = parts
    if len(quote) < MIN_QUOTE:
        raise ConfirmedError(f"引用が短すぎます（{MIN_QUOTE}文字以上）: {quote}")
    import urllib.parse
    host = urllib.parse.urlsplit(url).hostname or ""
    try:
        pub = _sl.publisher_of_host(host)
    except _sl.LineageError as e:
        raise ConfirmedError(str(e))
    return {"publisher": pub, "url": url, "quote": quote}


def check_sources(sources: list) -> list:
    """★独立2系列そろっているか★（同じ発行者の2ページは1票）"""
    if len(sources) < 2:
        raise ConfirmedError("出典が2つ要ります（独立した2系列）")
    keys = set()
    for s in sources:
        try:
            keys.add(_sl.vote_key(s["publisher"]))
        except _sl.LineageError as e:
            raise ConfirmedError(str(e))
    if len(keys) < 2:
        raise ConfirmedError(
            "同じ発行者の出典が2つあるだけです（独立した2系列が要ります）: "
            + " / ".join(s["publisher"] for s in sources))
    return sorted(keys)


def verify_source(src: dict, name: str, fetch=None) -> dict:
    """★出典のページを実際に取ってきて確かめる★（2026-08-09・依頼130 P0-2）

    以前は URL も引用も**言うだけ**で通った。そのため
    「機種Aについての本物の引用」を機種Bとして記録できた。
    ①そのページが本当にその機種のページか ②引用が本当にそこにあるか
    の2つを機械が確かめる。
    """
    if fetch is None:
        import new_machine_watch as _w

        def fetch(u):
            return _w._get(u)
    import hashlib

    import model_code_lookup as _mc
    import new_machine_watch as _w
    try:
        html = fetch(src["url"])
    except Exception as e:                 # noqa: BLE001
        raise ConfirmedError(f"出典を取得できません（{src['url']}）: {str(e)[:80]}")

    def text_of(h):
        return " ".join(_w._visible_text(h).split())
    ok, why = _mc.page_is_machine(html, name)
    if not ok:
        # ★機械が弾いたら、それはAIの出番の合図★（2026-08-11・運営者の指摘）
        #   大手には記事の題に**通称しか入れない**ところがある。
        #     なな徹の題「【青ブタ(スマスロ)】解析情報まとめ…」
        #     正式名称  「L青春ブタ野郎はバニーガール先輩の夢を見ない」
        #   ここで場合分け（通称の辞書・発行者ごとの例外）を足し始めると、
        #   「この場合、この場合…」が延々に増える。それはAIを使う意味がない。
        #   ★機械は取ってきて記録するところまで／同じ機種かの判断は2AI★
        #   ただし**言うだけでは通さない**＝本文は必ず機械が取ってきたもので、
        #   判断者と理由を必ず残す（あとで取り消す範囲を決められるように）。
        why_same = str(src.get("identity_why") or "").strip()
        proof = " ".join(str(src.get("identity_proof") or "").split())
        if not why_same or not proof:
            raise ConfirmedError(
                f"そのページは「{name}」のページだと確かめられません（{why}）: "
                f"{src['url']}／同じ機種だと2AIが判断したなら "
                "--source-identity に『URL|根拠の逐語引用|理由』を付けます")
        if len(proof) < MIN_QUOTE:
            raise ConfirmedError(
                f"同定の根拠は{MIN_QUOTE}文字以上で書きます: {proof}")
        # ★根拠は「この機種だ」と示すものでなければ意味がない★
        #   （2026-08-11・依頼150の指摘1）実在するだけの文を根拠にできたので、
        #   **別機種のページでも、そこにある文を写せば通っていた**。
        #   ＝「正式名称が載っている必要がある」という説明が実装と合っていなかった。
        import claim_identity as _cid
        core = _cid.normalize_core(name)
        if not core:
            # ★芯が取れないなら通さない★（2026-08-11・依頼151のP2）
            #   `if core and …` にしていたので、正本の名前が「L」「スマスロ」等
            #   飾りだけだと**検査ごと素通り**した（fail-open）。
            raise ConfirmedError(
                f"正式名称から機種の芯を取れません（{name!r}）＝同定を確かめられません")
        if core not in _cid.normalize_core(proof):
            raise ConfirmedError(
                f"同定の根拠に機種名が含まれていません（{src['url']}）: "
                f"{proof[:40]}／「{name}」だと分かる文を根拠にします")
        if len(why_same) < MIN_WHY:
            # ★受け取る側でも確かめる★（2026-08-11・依頼148の指摘3）
            #   CLIでしか見ていなかったので、別の呼び出し口を足すと素通りする。
            raise ConfirmedError(
                f"なぜ同じ機種かの理由は{MIN_WHY}文字以上で書きます: {why_same}")
        # ★理由だけでは通さない★（2026-08-11・依頼148の指摘1）
        #   「もっともらしい理由」は誰でも書ける。**そのページに実在する文**を
        #   根拠として出させ、機械が確かめる。これで、別機種のページを
        #   通すには「対象機種の正式名称がそのページに載っている」ことが要る。
        if proof not in text_of(html):
            raise ConfirmedError(
                f"同定の根拠がそのページに見当たりません（{src['url']}）: "
                f"{proof[:40]}")
        src["identity_override"] = {
            "why": why_same[:300],
            "proof": proof[:300],
            "machine_said": why,
            # ★どの本文を読んで判断したか★（あとで同じものを見たか確かめられる）
            "text_sha256": hashlib.sha256(
                text_of(html).encode("utf-8")).hexdigest(),
            "at": datetime.date.today().isoformat(),
        }
    text = " ".join(_w._visible_text(html).split())
    quote = " ".join(str(src["quote"]).split())
    if quote not in text:
        raise ConfirmedError(
            f"引用がそのページに見当たりません（{src['url']}）: {quote[:40]}")
    src["verified_at"] = datetime.date.today().isoformat()
    return src


def bind_machine(official_url: str) -> tuple:
    """公式URLから slug と正式名称を**正本から**引く。

    ★なぜ名前を名乗らせないか（2026-08-09・依頼131 P0-1）★
      `--slug` と `--name` を別々に受け取っていたので、
      **機種Aの本物のURL・引用を、機種Bのslugで記録できた**。
      三層の検査（発行者・ページの本人性・引用の実在）を全部通ってしまう。
      slugも名前も公式URLから導き、人に決めさせない。
    """
    import build_new_article as _ba
    slug = _ba.slug_from_url(official_url)
    if not slug:
        raise ConfirmedError(f"公式URLから機種の名前を作れません: {official_url}")
    # ①待ち行列（まだ登録されていない新台）
    try:
        pend = _sj.read_json(
            r"C:/Users/imao_/Documents/uchidokoro/add_machine_pending.json",
            expect=dict)
        for u, it in (pend.get("items") or {}).items():
            if u.rstrip("/") == str(official_url).rstrip("/"):
                return slug, str(it.get("name") or "")
    except Exception:                      # noqa: BLE001
        pass
    # ②すでに登録されている機種
    #   ★公式URLの完全一致で引く★（2026-08-10・依頼134 P0-3）
    #     URLの末尾だけでslugを作るので、待ち行列の機種のURL末尾が
    #     既存機種のslugと同じだと、別機種の欄へ保存できてしまった。
    try:
        ms = _sj.read_json(os.path.join(BASE, "assets", "data", "machines.json"),
                           expect=(dict, list))
        ms = ms["machines"] if isinstance(ms, dict) else ms
        for m in ms:
            ident = m.get("identity") or {}
            if str(ident.get("official_product_url") or "").rstrip("/") \
                    == str(official_url).rstrip("/"):
                return m.get("slug"), str(m.get("name") or "")
        for m in ms:
            if m.get("slug") == slug:
                # ★URLは違うのにslugだけ同じ＝取り違えの疑い★
                raise ConfirmedError(
                    "この公式URLから作ったslug（%s）は、別の機種がすでに使っています。"
                    "取り違えを防ぐため記録しません: 既存=%s"
                    % (slug, m.get("name")))
    except ConfirmedError:
        raise
    except Exception:                      # noqa: BLE001
        pass
    raise ConfirmedError(
        f"その公式URLの機種が見つかりません（待ち行列にも一覧にも無い）: {official_url}")


def record(slug: str, field: str, value, sources: list, by: list,
           why: str, name: str = "", fetch=None,
           official_url: str = "") -> dict:
    """★2AIが一致した値だけを残す★（fail-closed）"""
    if not field:
        raise ConfirmedError("--field が要ります")
    if base_field(field) not in allowed_fields():
        raise ConfirmedError(
            "受け取れない項目です: %s（使えるのは %s）"
            % (field, "/".join(sorted(allowed_fields()))))
    # ★公式URLを必ず要る★（2026-08-10・依頼132 P0-1）
    #   slug と name を別々に受け取れると、機種Aの本物の根拠を
    #   機種Bとして保存できた（三層の検査を全部通したうえで）。
    #   公式URLからしか slug と名前を決めない。
    if not official_url:
        raise ConfirmedError(
            "--official-url が要ります（slugと正式名称を正本から引くため。"
            "人が名乗った機種名は信用しません）")
    slug, name = bind_machine(official_url)
    if not str(name or "").strip():
        raise ConfirmedError(
            "正式名称を決められません。--official-url を使ってください"
            "（slugと名前を正本から引きます＝機種の取り違えを防ぐため）")
    who = sorted({x.strip() for x in (by or []) if x.strip()})
    for need in REQUIRED_JUDGES:
        if need not in who:
            raise ConfirmedError(
                "2人（%s）がそろって初めて記録できます: いまは %s"
                % ("/".join(REQUIRED_JUDGES), ",".join(who) or "なし"))
    if len(str(why or "").strip()) < 8:
        raise ConfirmedError("--why（どう突き合わせたか）は8文字以上で書きます")
    lineages = check_sources(sources)
    # ★出典ごとに、その値を支えていることを確かめる★（2026-08-09・依頼130 P1-1）
    #   以前は全出典の引用をつなげてから探していたので、
    #   **1つの出典にしか無い値でも「2出典一致」として通った**。
    # ★値の形を確かめ、引用と照合する表示値を決める★（依頼131 P0-3・P1）
    #   単位や恩恵まで照合しないと、引用が「1000pt」でも値を「1000G」にできた。
    toks = check_shape(field, value)
    for s in sources:
        q = " ".join(str(s["quote"]).split())
        for token in toks:
            if token not in q:
                raise ConfirmedError(
                    f"値『{token}』が {s['publisher']} の引用にありません"
                    "（★出典ごとに同じ値を支えている必要があります★）")
    # ★引用が本当にそのページにあるか・そのページがその機種かを確かめる★
    sources = [verify_source(dict(s), name, fetch) for s in sources]
    data = load()
    rec = {
        "value": value,
        "sources": sources,
        "lineages": lineages,
        "agreed_by": who,
        "why": str(why).strip()[:300],
        "decided_at": datetime.date.today().isoformat(),
    }
    data["machines"].setdefault(slug, {})[field] = rec
    _save(data)
    return {"state": "RECORDED", "slug": slug, "field": field,
            "lineages": lineages}


def _tokens(value) -> list:
    """値の中の「引用に現れるべき文字列」を取り出す。"""
    if isinstance(value, dict):
        out = []
        for k, v in value.items():
            if k.startswith("_") or k in ("unit", "note", "benefit", "counted",
                                          "phase", "role", "kind"):
                continue
            out += _tokens(v)
        return out
    if isinstance(value, list):
        return [t for v in value for t in _tokens(v)]
    if isinstance(value, bool) or value is None:
        return []
    return [str(value)]


def forget(slug: str, field: str) -> dict:
    data = load()
    fields = (data.get("machines") or {}).get(slug) or {}
    if field not in fields:
        return {"state": "NOT_FOUND"}
    fields.pop(field)
    if not fields:
        data["machines"].pop(slug, None)
    _save(data)
    return {"state": "FORGOTTEN"}


def for_slug(slug: str, data: dict | None = None) -> dict:
    """機械が毎回読む側（無人タスクはここだけ使う）。"""
    d = data if data is not None else load()
    return dict((d.get("machines") or {}).get(slug) or {})


def merge_into(material: dict, slug: str) -> list:
    """集めた材料に、2AIが確定した値を足す。★足したものの一覧を返す★

    ★機械が採れたものを上書きしない★（機械が採れているなら、それは
      すでに独立2出典で一致したもの。人の記録で塗り替えない）
    ★入れ先を間違えない★（2026-08-09・依頼130 P0-1）
      天井・AT・CZは基本スペックとは別の場所に入る。全部を adopted に
      入れていたので、記事に届かないうえ KeyError で落ちていた。
    """
    added = []
    if not isinstance(material, dict):
        return added
    targets = allowed_fields()
    for field, rec in for_slug(slug).items():
        where = targets.get(base_field(field))
        if not where:
            # ★知らない項目は黙って捨てない★
            raise ConfirmedError(f"知らない項目です: {field}")
        stamped = {
            "value": rec["value"],
            "sources": [s["url"] for s in rec.get("sources") or []],
            # ★どこから来た値かを残す★（あとで追える）
            "_from": "confirmed_values",
            "_agreed_by": rec.get("agreed_by"),
            "_decided_at": rec.get("decided_at"),
        }
        if where == "adopted":
            adopted = material.setdefault("adopted", {})
            if field in adopted:
                continue
            adopted[field] = stamped
        else:
            box = material.setdefault(where, {})
            rows = box.setdefault("adopted", [])
            # ★同じ中身が既にあるなら足さない★（機械が採れていれば上書きしない）
            def _core(d):
                # 出所や出典URLは比べない（機械が採った行と形が違うだけで
                # 「別物」と見なして重複して増えていた・依頼131 P1）
                return {k: v for k, v in (d or {}).items()
                        if not k.startswith("_") and k != "sources"}
            if any(_core(r) == _core(rec["value"]) for r in rows):
                continue
            row = dict(rec["value"]) if isinstance(rec["value"], dict) else {
                "value": rec["value"]}
            row["_from"] = "confirmed_values"
            row["sources"] = stamped["sources"]
            rows.append(row)
        added.append(field)
    return added


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    import tempfile

    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("OK " if cond else "NG ") + name)

    def stops(name, fn):
        try:
            fn()
            t(name, False)
        except ConfirmedError:
            t(name, True)

    NAME = "L試験機"
    Q1 = "天井は1000G+α"
    Q2 = "通常時1000G+αで天井"

    def fake_fetch(url):
        q = Q1 if "chonborista" in url else Q2
        return ("<title>" + NAME + " スロット 新台 天井 | 解析</title>"
                "<body><h1>" + NAME + "</h1><p>" + q + "。" + ("説明。" * 30)
                + "</p></body>")

    real_bind = globals()["bind_machine"]
    globals()["bind_machine"] = lambda u: ("x", NAME)

    def rec(**kw):
        base = dict(official_url="https://m.example/products/slot/x/",
                    slug="x", field="ceiling",
                    value={"kind": "GAME", "amount": "1000", "unit": "G",
                           "benefit": "AT"},
                    sources=None, by=["claude", "codex"],
                    why="同じ原文を読んで一致しました", name=NAME,
                    fetch=fake_fetch)
        base.update(kw)
        if base["sources"] is None:
            base["sources"] = [parse_source("https://chonborista.com/1|" + Q1),
                               parse_source("https://nana-press.com/1|" + Q2)]
        return record(**base)

    global STORE
    keep = STORE
    STORE = os.path.join(tempfile.mkdtemp(), "confirmed_values.json")
    try:
        t("★★発行者は名乗らせずURLから引く★★（別ホストに名前を付けて通せた）",
          parse_source("https://chonborista.com/1|" + Q1)["publisher"]
          == "chonborista")
        stops("　登録されていないサイトは使えない",
              lambda: parse_source("https://a.example/1|" + Q1))

        stops("★★2人そろわないと記録できない★★", lambda: rec(by=["claude"]))
        stops("　どう突き合わせたかを書かないと記録できない", lambda: rec(why="短い"))
        stops("★★出典が1つでは記録できない★★",
              lambda: rec(sources=[parse_source("https://chonborista.com/1|" + Q1)]))
        stops("★★同じ転載系列の2つは1票★★",
              lambda: rec(sources=[parse_source("https://chonborista.com/1|" + Q1),
                                   parse_source("https://yancha-press.com/1|" + Q1)]))
        stops("★★引用に無い値は記録できない★★（値を発明させない）",
              lambda: rec(value={"kind": "GAME", "amount": "1234", "unit": "G"}))
        stops("★★出典ごとに同じ値を支えていないと記録できない★★"
              "（つなげて探していたので1出典だけでも通った）",
              lambda: rec(sources=[parse_source("https://chonborista.com/1|" + Q1),
                                   parse_source("https://nana-press.com/1|天井なし")]))
        stops("★★受け取れない項目は断る★★（入れ先が決まっていないもの）",
              lambda: rec(field="なにか"))
        stops("★★公式URLが無ければ記録できない★★（機種の取り違えを断つ）",
              lambda: rec(official_url=""))
        stops("★★継続率の欄に枚数は書けない★★（記事が単位を付けるので嘘になる）",
              lambda: rec(field="at#x",
                          value={"mode": "MAIN_AT", "loop_rate": "4.2枚/G"}))
        stops("★★純増の欄に％は書けない★★",
              lambda: rec(field="at#y", value={"mode": "MAIN_AT", "net": "73%"}))

        def other_machine(url):
            return ("<title>別の機種 スロット 新台 | 解析</title>"
                    "<body><h1>別の機種</h1><p>" + Q1 + "。" + ("説明。" * 30)
                    + "</p></body>")
        stops("★★別機種のページの引用は記録できない★★"
              "（本物の引用でも、その機種のページでなければ採らない）",
              lambda: rec(fetch=other_machine))

        def no_quote(url):
            return ("<title>" + NAME + " スロット 新台 | 解析</title>"
                    "<body><h1>" + NAME + "</h1><p>" + ("説明。" * 40)
                    + "</p></body>")
        stops("★★引用がそのページに無ければ記録できない★★（言うだけでは通らない）",
              lambda: rec(fetch=no_quote))

        # ★題が通称のページを、2AIの判断で通す★（2026-08-11・運営者の指摘）
        #   機械が弾いたら場合分けを足すのではなく、2AIが本文を読んで決める。
        #   ★ただし理由を書かなければ通らない★（言うだけでは通さない）
        def nickname(url):
            """題は通称だけ・本文には正式名称がある（なな徹の実際の形）"""
            if "nana-press" not in url:
                return fake_fetch(url)
            # ★題には通称しか入っていない★（機種名の芯が1文字も出てこない）
            return ("<title>【ためし丸(スマスロ)】解析情報まとめ</title>"
                    "<body><h1>【ためし丸(スマスロ)】解析情報まとめ</h1>"
                    "<p>機種名:" + NAME + " メーカー オリンピア " + Q2 + "。"
                    + ("説明。" * 30) + "</p></body>")
        stops("★★題が機種名と合わないページは、理由なしでは通らない★★",
              lambda: rec(fetch=nickname))
        src_lie = [parse_source("https://chonborista.com/1|" + Q1),
                   dict(parse_source("https://nana-press.com/1|" + Q2),
                        identity_why="題は通称だが本文に正式名称とメーカーがある",
                        identity_proof="このページには無い文です")]
        stops("★★同定の根拠も、そのページに実在しなければ通らない★★"
              "（もっともらしい理由は誰でも書ける・依頼148の指摘1）",
              lambda: rec(fetch=nickname, sources=src_lie))
        # ★実在するだけでは足りない＝その機種だと分かる文でなければ意味がない★
        #   （依頼150の指摘1。別機種のページにある文を写せば通っていた）
        src_other = [parse_source("https://chonborista.com/1|" + Q1),
                     dict(parse_source("https://nana-press.com/1|" + Q2),
                          identity_why="題は通称だが同じ機種だと判断した",
                          identity_proof=Q2)]   # 実在するが機種名を含まない
        stops("★★根拠に機種名が入っていなければ通らない★★"
              "（別機種のページにある文を写しても越えられない）",
              lambda: rec(fetch=nickname, sources=src_other))
        src_short = [parse_source("https://chonborista.com/1|" + Q1),
                     dict(parse_source("https://nana-press.com/1|" + Q2),
                          identity_why="題は通称だが同じ機種だと判断した",
                          identity_proof="機種")]
        stops("　根拠が短すぎても通らない（受け取る関数側でも見る）",
              lambda: rec(fetch=nickname, sources=src_short))
        src_ok = [parse_source("https://chonborista.com/1|" + Q1),
                  dict(parse_source("https://nana-press.com/1|" + Q2),
                       identity_why="題は通称だが本文に正式名称とメーカーがある",
                       identity_proof="機種名:" + NAME)]
        r = rec(fetch=nickname, sources=src_ok)
        t("★★2AIが本文を読んで判断すれば、題が通称でも通せる★★"
          "（機械は取ってくるだけ／判断と理由は残す）",
          r["state"] == "RECORDED" and len(r["lineages"]) == 2)
        # ★正本の名前が飾りだけだと、検査ごと素通りしていた★（依頼151のP2）
        #   名前は公式URLから引くので、引き当てる側を差し替えて確かめる。
        _keep_bind = globals()["bind_machine"]
        try:
            globals()["bind_machine"] = lambda u: ("x", "スマスロ")
            stops("★★正式名称から芯を取れないときも通らない★★（依頼151のP2）",
                  lambda: rec(fetch=nickname, sources=src_ok, name="スマスロ"))
        finally:
            globals()["bind_machine"] = _keep_bind
        got = for_slug("x")["ceiling"]["sources"]
        ov = [s.get("identity_override") for s in got if s.get("identity_override")]
        t("　誰がなぜ通したか・何を読んで判断したかが残る",
          ov and ov[0]["why"] and ov[0]["proof"]
          and len(ov[0]["text_sha256"]) == 64)
        forget("x", "ceiling")

        r = rec()
        t("　2人が一致し、独立2系列の引用が実在すれば記録できる",
          r["state"] == "RECORDED" and len(r["lineages"]) == 2)

        mat = {}
        added = merge_into(mat, "x")
        t("★★天井は ceilings の中へ入る★★（依頼130 P0-1。adopted に入れて落ちていた）",
          added == ["ceiling"]
          and mat["ceilings"]["adopted"][0]["amount"] == "1000"
          and mat["ceilings"]["adopted"][0]["_from"] == "confirmed_values"
          and "ceiling" not in (mat.get("adopted") or {}))

        import spec_lookup as _sp
        # ★adopted に入るのは spec_lookup が知っている鍵か、
        #   2AIだけが答えられる鍵（AI_ONLY_FIELDS）★（2026-08-12）
        t("　基本スペック側の項目は spec_lookup か AI_ONLY_FIELDS の鍵だけ",
          all(k in _sp.FIELDS or k in AI_ONLY_FIELDS
              for k, v in allowed_fields().items() if v == "adopted"))
        t("　2AIだけが答える鍵にも値の形がある（何でも受け取らない）",
          all(k in VALUE_SHAPES for k in AI_ONLY_FIELDS))

        mat2 = {"ceilings": {"adopted": [{"kind": "GAME", "amount": "1000",
                                          "unit": "G", "benefit": "AT",
                                          "sources": ["x"]}]}}
        merge_into(mat2, "x")
        t("★★機械が採れている天井は増やさない★★",
          len(mat2["ceilings"]["adopted"]) == 1)

        stops("★★記事が読む項目（恩恵など）が無い値は記録できない★★"
              "（依頼131 P0-3。記録できてしまい、あとで記事生成が落ちていた）",
              lambda: rec(value={"kind": "GAME", "amount": "1000", "unit": "G"}))
        stops("　天井の種類が決まった語でないと記録できない",
              lambda: rec(value={"kind": "ナニカ", "amount": "1000",
                                 "unit": "G", "benefit": "AT"}))
        # ★依頼134で見つかった穴を固定する★
        stops("★★純増に単位を書かせない★★（記事が付けるので『約約2.8枚枚』になる）",
              lambda: rec(field="at#z",
                          value={"mode": "MAIN_AT", "net": "約2.8枚"}))
        t("　純増は数だけなら通る",
          check_shape("at#z", {"mode": "MAIN_AT", "net": "2.8"}) == ["2.8"])
        stops("★★機械割が空でも通っていた★★（引用照合もすり抜けた）",
              lambda: check_shape("payout_range",
                                  {"low": "", "high": "", "unit": "%"}))
        stops("　範囲として読めない機械割は受け取らない",
              lambda: check_shape("payout_range",
                                  {"low": "112.5", "high": "97.3", "unit": "%"}))
        stops("★★設定は1〜6以外を受け取らない★★（『設定memo』が表に出せた）",
              lambda: check_shape("payout_rate", {"memo": "97.8%"}))
        t("★CZの継続G数はGを付けた形★（ATの『30』とは別の決まり）",
          check_shape("cz#a", {"name": "石兵八陣", "games": "4G+α"})
          == ["石兵八陣", "4G+α"])
        stops("　ATの継続G数に単位は書かない",
              lambda: check_shape("at#w", {"mode": "MAIN_AT", "games": "30G"}))

        t("　間違いは取り消せる", forget("x", "ceiling")["state"] == "FORGOTTEN")
        t("　無いものを取り消しても壊れない",
          forget("x", "ceiling")["state"] == "NOT_FOUND")
        stops("　出典の書き方が違えば受け取らない", lambda: parse_source("URLだけ"))
    finally:
        STORE = keep
        globals()["bind_machine"] = real_bind

    # ★種類ごとに、要る鍵と使わない鍵を決める★（2026-08-12・依頼160/161）
    #   欠けていれば記事に1行も出ず、余分なら2出典で確かめた中身が黙って消える。
    stops("★★種類に要る鍵が欠けたら受け取らない★★（記事に1行も出ない）",
          lambda: check_shape("reset", {"kind": "CEILING_SHORTENED",
                                        "state": "高確スタート"}))
    stops("★★その種類で使わない鍵は受け取らない★★（黙って消えるのを防ぐ）",
          lambda: check_shape("reset", {"kind": "CEILING_SHORTENED",
                                        "games": "600",
                                        "state": "高確スタート"}))
    def _passes(field, value):
        """止まらずに通ること（返り値は逐語照合が要る欄の一覧）。"""
        try:
            check_shape(field, value)
            return True
        except ConfirmedError:
            return False

    t("　種類に合った形は通る",
      _passes("reset", {"kind": "CEILING_SHORTENED", "games": "600"})
      and _passes("reset", {"kind": "MORNING_STATE", "state": "高確スタート"}))

    t("★★配列（gains）は要素ごとに引用と照合する★★（依頼181のP1）"
      "／以前は丸ごと1語にしていたので、正しい材料も記録できなかった",
      (lambda got: "武将参戦" in got and "上乗せ" in got
       and not any("[" in x for x in got))(
          check_shape("gameplay", {"when": "AT中", "trigger": "参戦チャンス",
                                   "leads_to": "上乗せ",
                                   "gains": ["上乗せ", "武将参戦"]})))

    ng = sum(1 for _, o in results if not o)
    print()
    print("%d/%d 合格" % (len(results) - ng, len(results)))
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="2AIで確定した値の受け取り口")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--forget", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--slug", default="")
    ap.add_argument("--official-url", dest="official_url", default="",
                    help="★推奨★ 公式URL（slugと正式名称を正本から引く）")
    ap.add_argument("--name", default="",
                    help="正式名称（--official-url が使えないときだけ）")
    ap.add_argument("--field", default="")
    ap.add_argument("--value", default="", help="値（文字列）")
    ap.add_argument("--value-file", dest="value_file", default="",
                    help="値を書いたJSONファイル（構造のある値はこちら）")
    ap.add_argument("--source", action="append", default=[],
                    help="URL|逐語の引用（2つ以上・発行者はURLから引く）")
    ap.add_argument("--source-identity", dest="source_identity",
                    action="append", default=[],
                    help="URL|根拠の逐語引用|なぜ同じ機種と判断したか"
                         "（題が通称のサイト等。2AIが本文を読んで判断したとき。"
                         "根拠はそのページに実在する文＝機械が確かめる）")
    ap.add_argument("--by", default="", help="判断した人（claude,codex）")
    ap.add_argument("--why", default="")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()
    try:
        if a.record:
            if a.value_file:
                value = _sj.read_json(a.value_file, expect=(dict, list, str, int, float))
            elif a.value:
                value = a.value
            else:
                print("--value か --value-file が要ります")
                return 2
            # ★題が合わないページを2AIの判断で通す理由★（URLごとに結び付ける）
            why_by_url = {}
            for spec in a.source_identity:
                parts = [x.strip() for x in str(spec).split("|", 2)]
                if len(parts) != 3 or not parts[0] or len(parts[1]) < MIN_QUOTE \
                        or len(parts[2]) < MIN_WHY:
                    print("--source-identity は"
                          "『URL|根拠の逐語引用（%d文字以上）|理由（%d文字以上）』"
                          "の形です" % (MIN_QUOTE, MIN_WHY))
                    return 2
                why_by_url[parts[0]] = (parts[1], parts[2])
            srcs = []
            for s in a.source:
                one = parse_source(s)
                if one["url"] in why_by_url:
                    proof, w = why_by_url.pop(one["url"])
                    one["identity_proof"], one["identity_why"] = proof, w
                srcs.append(one)
            if why_by_url:
                # ★どの出典にも結び付かない理由は黙って捨てない★
                print("--source-identity のURLが --source にありません: "
                      + ", ".join(why_by_url))
                return 2
            r = record(a.slug, a.field, value, srcs,
                       [x for x in a.by.split(",") if x.strip()], a.why,
                       name=a.name, official_url=a.official_url)
            print(json.dumps(r, ensure_ascii=False))
            return 0
        if a.forget:
            print(json.dumps(forget(a.slug, a.field), ensure_ascii=False))
            return 0
        if a.list:
            data = load()
            for slug, fields in sorted((data.get("machines") or {}).items()):
                if a.slug and slug != a.slug:
                    continue
                print("■ " + slug)
                for f, rec in sorted(fields.items()):
                    print("   %-14s %s" % (f, json.dumps(rec["value"],
                                                         ensure_ascii=False)[:70]))
                    print("      %s ／ %s（%s）"
                          % (rec.get("why"), ",".join(rec.get("agreed_by") or []),
                             rec.get("decided_at")))
                    for s in rec.get("sources") or []:
                        print("      - %s %s" % (s["publisher"], s["url"][:70]))
            return 0
    except ConfirmedError as e:
        print("★" + str(e) + "★")
        return 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

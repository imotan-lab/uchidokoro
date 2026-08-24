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
  │ ③その逐語を機械が実ページから取り直して照合すること│
  └────────────────────────────────────────────────┘
  ★★③は「誰が実行したか」ではない★★（2026-08-23・台帳#462）
    以前ここには「記録できるのは対話セッションだけ（無人は読むだけ）」と
    書いてあったが、2026-08-12の運営者決定
    ★「人を中継役にしない＝新台タスクが自分で決める」★と食い違っていた
    （手順書は無人タスクに記録させている）。コード側に強制も無く、
    ★口約束だけの守り★になっていたので、決定のほうへ揃えた。
    ★本当の境界は「誰が」ではなく「何を満たしたか」★＝
    ①②③と下の2つは全部このコードが機械で強制している
    （2人そろわなければ拒否・引用に無い値は拒否・同じ発行者は1票）。
    同じ条件を満たすなら、対話でも無人でも安全さは変わらない。
  ★出典は独立2系列★＝同じ発行者の2ページは1票（source_lineage で数える）。
  ★値を発明しない★＝引用に現れない値は記録できない（機械が確かめる）。

置き場: （書類フォルダ）/uchidokoro/confirmed_values.json
        （リポジトリ外・Dropboxへ保全）

使い方:
  # 記録する（★2AIが一致し、逐語を機械が照合できたときだけ★）
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
import os as _os_lp                 # noqa: E402
import sys as _sys_lp               # noqa: E402
_sys_lp.path.insert(0, _os_lp.path.dirname(_os_lp.path.abspath(__file__)))
import local_paths as _lp           # noqa: E402
STORE = _lp.doc("confirmed_values.json")
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


# ★★確定値が「記事のどの箱」に出るかの正本★★（2026-08-25・Codexの20回目）
#   ★なぜ1か所に置くか★＝読む側（recheck）が自前の小さな表を持っていたため、
#   `reset` と `at_net_unmapped` が**どの話題にも結び付かず**、
#   ★2AIで正しく確定した行まで「根拠がない」と言われていた★（再現済み）。
#   ＝正しい記事を毎日「直せ」と言い続ける経路。
#   ★読者に出る項目は、ここに必ず話題を書く★（書き忘れは検査が知らせる）。
#   ★空文字は「記事に出さない項目」★（型式名など）。
FIELD_TOPICS = {
    "ceiling": "ceiling",           # 天井・恩恵
    "at": "gameplay",               # ゲーム性
    "cz": "cz",                     # 確認できたCZ
    "gameplay": "gameplay",         # ゲーム性
    "reset": "reset",               # 朝一・リセット情報
    "at_net_unmapped": "gameplay",  # ゲーム性（AT名との対応は未確認）
    "checker_ceiling": "ceiling",   # 早見表に使う天井（本文にも出る）
    "ceilings_complete": "ceiling",  # 「これで全部か」の断り書き
    "payout_range": "spec",         # 基本スペック（機械割）
    "games_per_50": "spec",         # 基本スペック（50枚あたり）
    # ★設定別の表に出る項目★（2026-08-25・Codexの21回目）
    #   ★記事の「設定示唆まとめ」の表に出るのに、対応表に無かった★
    "at_prob": "setting",           # AT初当たり確率（設定別）
    "payout_rate": "setting",       # 出玉率（設定別）
    # ★素の net_increase は受け取らない★（2026-08-25・Codexの22回目）
    #   ★受け取れるのに、記事を作る側が一度も読まない★状態だった＝
    #   2AIが正しく答えても**公開に届かない**（型式名・天井と同じ型）。
    #   しかも採取規則は「純増はどのモードか」を必須にしているのに、
    #   素の値は自由な文字として通っていた。
    #   ★構造化された `at` か `at_net_unmapped` を使う★（どちらも記事に出る）。
    "net_increase": "",             # ★受け口を閉じた（下の除外と対）★
    "model_code": "",               # ★読者には出さない★
}


def topic_of(field: str) -> str:
    """その項目が記事のどの話題に出るか（空＝読者に出さない）。

    ★知らない項目は例外にする★＝黙って spec に落とすと、
    足した項目が**どの話題でも根拠にならない**まま気づけない。
    """
    base = base_field(field)
    if base not in FIELD_TOPICS:
        raise ConfirmedError(
            f"確定値の項目 {base!r} が、記事のどの話題に出るか決まっていません"
            "（scripts/confirmed_values.py の FIELD_TOPICS に足してください）")
    return FIELD_TOPICS[base]


# ★2AIだけが答えられる項目★（2026-08-12・運営者決定「人が直す項目をなくす」）
#   機械の側で決めようとすると場合分けが増えるだけなので、
#   「機械は質問を出す・2AIが答えて記録する」形にする。
AI_ONLY_FIELDS = {
    # 天井が複数ある機種（通常時／AT間／スルー）で、
    # 早見表の「天井まで残り」に使う値はどれか。
    "checker_ceiling": "adopted",
    # ★AT名との対応が付かない純増★（2026-08-24・Codexの5回目）
    #   記事に出す形も、値の形も決まっているのに**名簿に無かった**ので、
    #   ★2AIが正しく答えても公式の記録経路から入れられなかった★。
    #   ＝正しい回答が公開に届かない停止経路（型式名・天井と同じ型）。
    "at_net_unmapped": "adopted",
    # ★「確認できた天井がこれで全部か」★（2026-08-24・Codexの5回目）
    #   ★直す前は材料の生の真偽値だけで断り書きが消えた★＝
    #   「ほかにも天井があるかもしれません」という**読者を守る一文**を、
    #   誰の証跡も無しに消せた。＝未確認の網羅性を断定していた。
    "ceilings_complete": "adopted",
}

# ★★人が読む名前★★（2026-08-24・Codexの4回目の指摘）
#   ★これが無いと新台追加が止まる★＝
#   `add_machine_run` は材料の adopted のキーを
#   `spec_lookup.FIELDS[k]["jp"]` で表示名にしていた。
#   2AIだけが答える項目は FIELDS に無いので **KeyError** になり、
#   ★2AIが正しく答えた機種ほど公開できない★状態だった（実際に再現）。
AI_ONLY_LABELS = {
    "checker_ceiling": "早見表に使う天井",
    "at_net_unmapped": "AT純増（AT名との対応は未確認）",
    "ceilings_complete": "天井はこれで全部か",
}


# ★2AIの受け口から外す項目★（2026-08-25・Codexの22回目）
#   受け取れても記事に届かないもの＝答えが迷子になるので、入口で断る。
CLOSED_FIELDS = ("net_increase",)


def allowed_fields() -> dict:
    """受け取ってよい項目 → 入れ先。

    ★★記事に届かない項目は受け取らない★★（2026-08-25・Codexの22回目）
      ★受け取れるのに、記事を作る側が一度も読まない項目があった★
      （素の `net_increase`）＝2AIが正しく答えても**公開に届かない**。
      入口で断れば、答えが迷子にならず「どの項目を使えばよいか」も分かる。
    """
    import spec_lookup as _sp
    out = {k: "adopted" for k in _sp.FIELDS}
    out.update(FIELD_TARGETS)
    out.update(AI_ONLY_FIELDS)
    for _k in CLOSED_FIELDS:
        out.pop(_k, None)
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
    # ★天井はこれで全部か★（2026-08-24）
    #   ★辞書で持つ★＝この仕組みは値を辞書で扱う契約なので、
    #   真偽値そのものだと公式の登録口を通れない（Codexの6回目で判明）。
    "ceilings_complete": {"required": ("complete",),
                          "enums": {"complete": ("YES",)},
                          "quoted": ()},
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
    # ★引用と照合する★（2026-08-24・Codexの7回目）
    #   ★直す前は照合対象が空だった★ので、出典に「999」が無くても
    #   `values=["999"]` を登録でき、記事に「約999枚/G」と出せた。
    #   「約3.1枚」を値にすれば「約約3.1枚枚/G」も通った。
    "at_net_unmapped": {"required": ("values", "mapping"),
                        "enums": {"mapping": ("UNCONFIRMED",)},
                        "quoted": ("values",)},
}


# ★配列で受け取る項目★（2026-08-13・依頼182のP1）
#   ここに無い項目に配列を渡したら拒否する（記事が壊れるため）。
LIST_FIELDS = ("gains", "values")


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


# ★数とみなす形★（前後に数字が続いていないかを見る対象）
_NUMBERISH = _re.compile(r"^[0-9]+(\.[0-9]+)?$")

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
    # ★AT名との対応が付かない純増★（2026-08-24・Codexの7回目）
    #   ★記事は「約{値}枚/G」と書く★ので、値は数だけ。
    #   単位や「約」を入れると「約約3.1枚枚/G」になる（実際に通っていた）。
    "at_net_unmapped": {
        "values": (_re.compile(r"^\d{1,2}(\.\d)?$"),
                   "純増の数だけ（単位も「約」も書かない。例: 3.1）"),
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
    # ★どの鍵も、決めた形でなければ受け取らない★（2026-08-14・依頼185のP1）
    #   以前は「引用と照合する鍵」だけ型を見ていたので、
    #   ceiling の benefit に配列を渡すと**記事にPythonの配列表記が出た**。
    #   ★配列を許すのは LIST_FIELDS だけ／それ以外は文字列か数★
    for k, v in value.items():
        if str(k).startswith("_"):
            continue                       # 覚え書きは自由
        if k in LIST_FIELDS:
            if not isinstance(v, (list, tuple)) or not v:
                raise ConfirmedError(
                    f"{field}: 「{k}」は中身のある文字列の配列で書きます")
            if any(not isinstance(x, str) or not x.strip() for x in v):
                raise ConfirmedError(
                    f"{field}: 「{k}」の中身は空でない文字列だけです")
            continue
        if isinstance(v, (list, tuple, dict)):
            raise ConfirmedError(f"{field}: 「{k}」は文字列で書きます")
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
        got = value.get(k)
        # ★配列は要素ごとに見る★（2026-08-24・Codexの7回目）
        #   丸ごと str() にすると「['999']」になり、どんな形も通らないか、
        #   逆に検査が素通りする。
        if isinstance(got, (list, tuple)):
            for x in got:
                xs = str(x or "").strip()
                if xs and not pat.match(xs):
                    raise ConfirmedError(
                        f"{field}: 「{k}」は{jp}の形で書きます（いま {xs!r}）")
            continue
        v = str(got or "").strip()
        if v and not pat.match(v):
            raise ConfirmedError(f"{field}: 「{k}」は{jp}の形で書きます（いま {v!r}）")
    # ★引用と照合するのは、実際に書いた項目だけ★
    # ★配列は要素ごとに照合する★（2026-08-13・依頼181のP1）
    #   以前は str() で丸ごと1つの語にしていたので、引用に
    #   「['上乗せ', '武将参戦']」というPythonの書き方が無い限り必ず落ちた
    #   ＝gains を含む正しい材料を1件も記録できなかった。
    # ★配列を許すのは gains だけ★（2026-08-13・依頼182のP1）
    #   全項目で配列を許すと、trigger に配列を渡しても引用照合を通り、
    #   記事に「**['参戦チャンス', '別契機']**から」とPythonの書き方が出る
    #   （実際に再現した）。項目ごとに受け取る形を決める。
    out = []
    for k in shape["quoted"]:
        v = value.get(k)
        if k in LIST_FIELDS:
            if v is None or (isinstance(v, str) and not v.strip()):
                continue
            if not isinstance(v, (list, tuple)):
                raise ConfirmedError(f"{field}: 「{k}」は文字列の配列で書きます")
            for x in v:
                if not isinstance(x, str) or not x.strip():
                    raise ConfirmedError(
                        f"{field}: 「{k}」の中身は空でない文字列だけです")
                out.append(x.strip())
            continue
        if isinstance(v, (list, tuple, dict)):
            raise ConfirmedError(f"{field}: 「{k}」は文字列で書きます")
        if str(v or "").strip():
            out.append(str(v).strip())
    return out


class ConfirmedError(Exception):
    """確定値に関する異常（★迷ったら記録しない★）。"""


def _empty() -> dict:
    return {"schema_version": SCHEMA, "machines": {}}


# ★1件の記録に必ずある鍵★（record が書く形）
RECORD_KEYS = ("value", "sources", "lineages", "agreed_by", "why",
               "decided_at")
# ★あってもよい鍵★（あとから足した任意のもの）
RECORD_OPTIONAL = ("verified_at", "identity_override", "official_url")


def validate_record(field: str, rec) -> list:
    """★1件の記録が、書き込みと同じ契約を満たしているか★

    ★★なぜ読み込み側でも見るか★★（2026-08-24・Codexの6回目）
      ★直す前は `load()` が版番号しか見ていなかった★ので、
      控えのファイルへ**形だけ正しい偽の記録**を置けば、
      出典0件・判断者0人でも根拠の関所を越えて記事に出せた。
      ＝「書き込み口を厳しくしても、読み込み口が同じ契約を検証していない」
        （あなたが挙げた3原因の2つ目そのもの）。

    ★戻り値は問題の一覧★（空なら合格）。
    """
    ng = []
    base = base_field(field)
    if base not in allowed_fields():
        ng.append(f"知らない項目です: {field}")
        return ng
    if not isinstance(rec, dict):
        ng.append(f"{field}: 記録が辞書ではありません")
        return ng
    for k in RECORD_KEYS:
        if k not in rec:
            ng.append(f"{field}: {k} がありません")
    if ng:
        return ng
    extra = [k for k in rec
             if k not in RECORD_KEYS and k not in RECORD_OPTIONAL]
    if extra:
        ng.append(f"{field}: 知らない鍵があります: {extra}")
    try:
        check_shape(base, rec["value"])
    except Exception as e:                                   # noqa: BLE001
        ng.append(f"{field}: 値の形が違います（{str(e)[:80]}）")
    src = rec.get("sources")
    if not isinstance(src, list) or not src:
        ng.append(f"{field}: 出典がありません")
    else:
        for i, x in enumerate(src):
            if not isinstance(x, dict) or not x.get("url") \
                    or not x.get("quote"):
                ng.append(f"{field}: 出典{i + 1}にURLか引用がありません")
    who = rec.get("agreed_by")
    # ★★書き込みと同じ顔ぶれを求める★★（2026-08-24・Codexの9回目）
    #   ★直す前は「違う文字列が2つ」で通した★ので、
    #   手書きの `["a", "b"]` が読み直しを素通りした。
    if not isinstance(who, list) or not (
            set(REQUIRED_JUDGES) <= {str(x).lower() for x in who}):
        ng.append(f"{field}: 判断者に {'/'.join(REQUIRED_JUDGES)} が"
                  f"そろっていません（{who!r}）")
    if not str(rec.get("why") or "").strip():
        ng.append(f"{field}: なぜその値かの記録がありません")
    # ★★以下は「通信のいらない再計算」★★（2026-08-24・Codexの7回目）
    #   ★直す前は、鍵がある/形が合うところまでしか見ていなかった★ので、
    #   出典1件・系列0件・理由1文字・実在しない日付・
    #   ★引用に無い値★でも読み込めた（＝そのまま記事へ届く）。
    if len(str(rec.get("why") or "").strip()) < MIN_WHY:
        ng.append(f"{field}: なぜその値かの記録が短すぎます（{MIN_WHY}文字以上）")
    d = str(rec.get("decided_at") or "")
    try:
        datetime.date.fromisoformat(d)
    except Exception:                                        # noqa: BLE001
        ng.append(f"{field}: 決めた日が実在しません（{d!r}）")
    if isinstance(src, list) and src:
        # ★★2AIで通した出典は、指紋の箱ごと必須★★（2026-08-24・Codexの10回目）
        #   ★直す前は「箱があるときだけ」見ていた★ので、
        #   箱ごと消すと比較が全部飛んだ。
        for i, x in enumerate(src):
            if not isinstance(x, dict):
                continue
            if not (x.get("identity_why") or x.get("identity_proof")):
                continue
            ov = x.get("identity_override") or {}
            sha = str(ov.get("text_sha256") or "")
            if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
                ng.append(f"{field}: 2AIで通した出典{i + 1}に本文の指紋が"
                          "ありません（判断し直してください）")
        # ★発行者はURLから引き直す★（申告された発行者名を信じない）
        pubs = []
        for i, x in enumerate(src):
            if not isinstance(x, dict):
                continue
            import urllib.parse as _up
            host = _up.urlsplit(str((x or {}).get("url") or "")).hostname or ""
            try:
                # ★当時の出典として引く★（いま巡回してよいかは別の話）
                pubs.append(_sl.publisher_of_host_any(host))
            except Exception as e:                           # noqa: BLE001
                ng.append(f"{field}: 出典{i + 1}の発行者を引けません"
                          f"（{str(e)[:60]}）")
        # ★独立した2系列を数え直す★（保存された系列を信じない）
        if len(pubs) == len(src):
            try:
                # ★共同制作の組もまとめる★（正本と同じ扱い）
                got = sorted(_sl.merge_joint({_sl.vote_key_any(p)
                                              for p in pubs}))
                if len(got) < 2:
                    ng.append(f"{field}: 独立した2系列になっていません（{got}）")
                keep = sorted(rec.get("lineages") or [])
                # ★空でも比べる★（2026-08-24・Codexの8回目）
                #   `keep and` を付けていたので、系列0件の記録は
                #   ★比較そのものが飛ばされて通っていた★。
                if keep != got:
                    ng.append(f"{field}: 保存された系列と数え直しが違います"
                              f"（{keep} ≠ {got}）")
            except Exception as e:                           # noqa: BLE001
                ng.append(f"{field}: 系列を数え直せません（{str(e)[:60]}）")
        # ★値が、保存された引用に実在するか★（記録した時と同じ照合）
        try:
            toks = check_shape(base, rec["value"])
        except Exception:                                    # noqa: BLE001
            toks = []
        for i, x in enumerate(src):
            q = " ".join(str((x or {}).get("quote") or "").split())
            for tk in toks:
                if not token_in_quote(tk, q):
                    ng.append(f"{field}: 値『{tk}』が出典{i + 1}の引用にありません")
    return ng


def load(strict: bool = True, require_exists: bool = False) -> dict:
    """控えを読む。★1件ずつ契約を確かめる★（2026-08-24・Codexの6回目）

    strict=False は、直すために中身を見たいときだけ使う。
    """
    if not os.path.exists(STORE):
        if require_exists:
            # ★★「消えた」を「0件」と読まない★★（2026-08-24・Codexの7回目）
            #   ★直す前は不存在を正常な0件として返していた★ので、
            #   控えが消えた日に**2AIの確定値が全部抜けた記事**を
            #   何事もなかったように作れた。
            raise ConfirmedError(f"確定値の控えがありません: {STORE}")
        return _empty()
    got = _sj.read_json(STORE, expect=dict)
    if got.get("schema_version") != SCHEMA:
        raise ConfirmedError(f"確定値の形が違います: {got.get('schema_version')}")
    if require_exists and not isinstance(got.get("machines"), dict):
        # ★中身の入れ物ごと無い／空でないものが入っている★
        raise ConfirmedError("確定値の控えに機種の並びがありません")
    got.setdefault("machines", {})
    if strict:
        bad = []
        for slug, rows in (got.get("machines") or {}).items():
            if not isinstance(rows, dict):
                bad.append(f"{slug}: 記録の並びが辞書ではありません")
                continue
            for field, rec in rows.items():
                bad += [f"{slug} / {x}" for x in validate_record(field, rec)]
        if bad:
            # ★止める★＝偽の記録を「読めた」ことにしない（fail-closed）
            raise ConfirmedError(
                "確定値の控えに、契約を満たさない記録があります: "
                + " ／ ".join(bad[:5])
                + (f" ほか{len(bad) - 5}件" if len(bad) > 5 else ""))
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


def token_in_quote(token: str, quote: str) -> bool:
    """★その値が、引用に**その値として**書かれているか★

    ★★2026-08-24・Codexの8回目★★
      ★直す前はただの部分一致だった★ので、

        値 3.1 ／ 引用「純増は13.1枚/G」 → 通る
        値 100 ／ 引用「天井は1000G」    → 通る

      ＝★出典に書かれていない数を、書かれていることにできた★。
      記事はその値をそのまま出すので、読者への誤情報になる。

    ★数のときだけ、前後に数字・小数点が続かないことを求める★
      （文字の値は今までどおり部分一致。機種名や恩恵は
        文の一部として書かれているのが普通なので）。
    ★3.1 と 3.10 は別の値として扱う★（末尾に数字が続くため）。
    """
    t = str(token or "").strip()
    q = " ".join(str(quote or "").split())
    if not t:
        return False
    if not _NUMBERISH.match(t):
        return t in q                      # 文字の値は今までどおり
    for m in _re.finditer(_re.escape(t), q):
        before = q[m.start() - 1] if m.start() > 0 else ""
        after = q[m.end()] if m.end() < len(q) else ""
        # ★★空文字は「どんな文字列にも含まれる」★★（2026-08-25・自分で踏んだ）
        #   ★直す前は `before in "0123456789."` と書いていた★ので、
        #   数字が**文頭または文末**にあると before/after が空文字になり、
        #   Python では `"" in "0123..."` が真になって**必ず弾いていた**。
        #   ＝引用に「600G」「天井は600」と書いてあっても照合できない
        #     ＝★2AIが正しく確定した値を記録できない★（正しい答えが入らない経路）。
        #   実測＝'600G' も '天井は600' も False だった。
        # ★★けた区切りのカンマも数の一部★★（2026-08-25・Codexの21回目）
        #   ★直す前はカンマを境界と見ていなかった★ので、
        #   値 600 が引用「天井は1,600G」に一致した。
        #   ＝出典に書かれていない数を、書かれていることにできる。
        if (before and before in "0123456789.,") \
                or (after and after in "0123456789.,"):
            continue                       # ★別の数の一部★
        return True
    return False

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
    # ★共同で作ることがある組は1票にまとめる★（2026-08-14・依頼190のP1）
    #   一撃とDMMぱちタウンには共同取材の企画が実在する（「双龍玉」）。
    #   ★値を控える場所がいちばん危ない★ので、ここは確かめるまで数えない。
    keys = _sl.merge_joint(keys)
    if len(keys) < 2:
        raise ConfirmedError(
            "同じ発行者の出典が2つあるだけです（独立した2系列が要ります）: "
            + " / ".join(s["publisher"] for s in sources))
    return sorted(keys)


def page_text(html: str, url: str) -> str:
    """★出典として読んでよい本文★（★ここだけが作る★）

    ★★2026-08-24・Codexの13回目★★
      ★保存するときの指紋は `text_of()`、公開前の再確認は `_visible_text()` と
      別々に作っていた★。投稿欄の決まりごとが無いサイトに見出しができると、
        ・保存時＝投稿欄より前の本文
        ・再確認時＝投稿欄を含む全文
      になり、**本文が変わっていないのに指紋が食い違って本番が止まる**。

    ★二重に掃除しない★＝取ってくる時点で箱は落ちている。
      足りないのは「決まりごとが無いサイトの行切り」だけ。
    ★行で切る処理なので、切る前に1行へ潰さない★（2026-08-24に踏んだ）。
    """
    import ceiling_lookup as _cl
    import new_machine_watch as _w2
    import user_area as _ua2
    raw = _w2._visible_text(html)
    ua = _ua2.conf_for_url(url or "")
    if [r for r in (ua.get("drop") or []) if isinstance(r, dict)]:
        return " ".join(raw.split())       # 箱で落とし済み（行では切らない）
    return " ".join(_cl.cut_user_area(raw).split())

def verify_source(src: dict, name: str, fetch=None) -> dict:
    """★出典のページを実際に取ってきて確かめる★（2026-08-09・依頼130 P0-2）

    以前は URL も引用も**言うだけ**で通った。そのため
    「機種Aについての本物の引用」を機種Bとして記録できた。
    ①そのページが本当にその機種のページか ②引用が本当にそこにあるか
    の2つを機械が確かめる。
    """
    if fetch is None:
        # ★取り直しの道は1本★（2026-08-24・Codexの9回目）
        #   ★直す前は生のHTMLをそのまま読んでいた★ので、
        #   **読者の書き込み欄に書かれた文**を逐語引用として
        #   記録・再検証できた（材料を読む側は落としているのに、
        #   確定値の経路だけ抜けていた）。
        fetch = _default_fetch
    import hashlib

    import model_code_lookup as _mc
    import new_machine_watch as _w
    import user_area as _ua
    try:
        html = fetch(src["url"])
    except Exception as e:                 # noqa: BLE001
        raise ConfirmedError(f"出典を取得できません（{src['url']}）: {str(e)[:80]}")

    def text_of(h):
        """★共通の本文づくりを呼ぶだけ★（作る場所は `page_text` 1か所）"""
        return page_text(h, src.get("url") or "")
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
    # ★同じ本文を使う★（2026-08-24・Codexの12回目＝ここが直っていなかった）
    text = text_of(html)
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
            _lp.doc("add_machine_pending.json"),
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
            if not token_in_quote(token, q):
                raise ConfirmedError(
                    f"値『{token}』が {s['publisher']} の引用にありません"
                    "（★出典ごとに同じ値を支えている必要があります★）")
    # ★引用が本当にそのページにあるか・そのページがその機種かを確かめる★
    sources = [verify_source(dict(s), name, fetch) for s in sources]
    # ★控えが無いときは作らない★（2026-08-24・Codexの9回目）
    #   初回は `--init`、消失は復旧。ここで黙って作ると両者を区別できない。
    data = load(strict=False, require_exists=True)
    rec = {
        "value": value,
        "sources": sources,
        "lineages": lineages,
        "agreed_by": who,
        "why": str(why).strip()[:300],
        "decided_at": datetime.date.today().isoformat(),
        # ★どの機種の正本から引いたか★（2026-08-24・Codexの8回目）
        #   ★これが無いと、あとで「slugと正式名称が今も同じか」を
        #   確かめ直せない★（記録だけ残って、由来が追えない）。
        "official_url": str(official_url or ""),
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


def init_store() -> str:
    """★初回だけ、空の正本を作る★（2026-08-24・Codexの9回目）

    ★★初回と「消えた」を区別する★★
      直す前は `record()` が不存在を正常な初回として空から作っていたので、
      ★消失事故のあとにも、何事もなかったように空の控えが再生した★。
      作るのはこの入口だけにし、ほかは「無ければ止まる」に統一する。

    ★復旧のときは使わない★＝控えのバックアップから戻すこと。
    """
    if os.path.exists(STORE):
        raise ConfirmedError(f"すでにあります（作り直しません）: {STORE}")
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".init"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(_empty(), fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, STORE)                 # ★途中の形を残さない★
    return STORE


def _default_fetch(url: str) -> str:
    """★出典を取り直す既定の道★（投稿欄を落とす唯一の入口を通る）

    ★★別のサイトへ転送されていたら使わない★★（2026-08-24・Codexの10回目）
      ★直す前は本文だけ受け取って、着いた先を捨てていた★。すると、
        ・投稿欄を落とす決まりは**頼んだURL**の側で選ばれる
        ・実際の本文は**着いた先**のもの
        ・票は頼んだURLの発行者として数える
      ＝★投稿欄が残る／同じページが2票になる★が同時に成立しうる。
      いまは着いた先が別のホストなら断る（そのURLで登録し直すこと）。
    """
    import urllib.parse as _up
    import fetched_page as _fp
    got = _fp.fetch(url, "claim_material")
    # ★同じ発行者なら通す★（2026-08-24・Codexの11回目）
    #   ★ホスト名の文字で比べていた★ので、www の有無をそろえるだけの
    #   転送でも止まった＝**正しい本番を止める型**。
    a = (_up.urlsplit(url).hostname or "").lower()
    b = (_up.urlsplit(got.final_url).hostname or "").lower()
    if a != b:
        try:
            same = (_sl.publisher_of_host_any(a)
                    == _sl.publisher_of_host_any(b))
        except Exception:                                    # noqa: BLE001
            same = False
        if not same:
            raise ConfirmedError(
                f"別のサイトへ転送されています（{url} → {got.final_url}）"
                "／★着いた先のURLで登録し直してください★")
    return got.cleaned_html


def reverify(slug: str, fetch=None, name: str = "",
             official_url: str = "") -> list:
    """★公開しようとしている機種の控えだけ、取り直して確かめる★

    ★★なぜ要るか★★（2026-08-24・Codexの8回目）
      控えの読み直しは、**保存されたURLと引用を信じて**いる。
      控えを手で書き換えられたら、偽の引用でも通ってしまう。
      ＝「書き込み口を厳しくしても、読み込み口が信じてしまう」型の残り。

    ★全件はやらない★＝いま書こうとしている機種だけ。
      出典は各1回だけ取りに行く（同じURLは1回）。

    ★戻り値は問題の一覧★（空なら合格）。
    """
    ng = []
    rows = for_slug(slug)
    if not rows:
        return ng                          # 確定値が無い機種は何もしない
    # ★機種の正本を引き直す★（slugと正式名称が今も同じか）
    #   ★呼ぶ側が確かめ済みの名前・URLを持っていればそれを使う★
    #   （2026-08-24・Codexの9回目＝まだ一覧に無い新台は自力で引けない）
    urls = {str((r or {}).get("official_url") or "") for r in rows.values()}
    urls.discard("")
    if official_url:
        urls.add(official_url)
    for u in urls:
        try:
            got_slug, got_name = bind_machine(u)
        except Exception as e:                               # noqa: BLE001
            ng.append(f"{slug}: 公式URLから機種を引き直せません（{str(e)[:60]}）")
            continue
        if got_slug != slug:
            ng.append(f"{slug}: 公式URLが別の機種を指しています（{got_slug}）")
        name = name or got_name
    if not name:
        # ★公式URLを持たない古い記録★（2026-08-24）
        #   ★slugをそのまま機種名として使わない★＝
        #   出典ページの同定に必ず失敗し、**正しい記録で公開が止まる**。
        #   一覧から正式名称を引く。
        try:
            rows_m = _sj.read_json(
                os.path.join(BASE, "assets", "data", "machines.json"),
                expect=(dict, list))
            rows_m = rows_m["machines"] if isinstance(rows_m, dict) else rows_m
            for m in rows_m:
                if str((m or {}).get("slug") or "") == slug:
                    name = str(m.get("name") or "")
                    break
        except Exception:                                    # noqa: BLE001
            pass
    if not name:
        ng.append(f"{slug}: 正式名称を引けないので出典を確かめ直せません")
        return ng
    # ★★取ってくるのは1回・照合は全部★★（2026-08-24・Codexの9回目）
    #   ★直す前は「確かめた結果」をURLごとに使い回していた★ので、
    #   同じURLを2つの項目で使うと、**2件目の引用は一度も照合されなかった**
    #   （例：天井の引用は残っているが、純増の引用は消えている → 通る）。
    pages = {}

    def _html_of(url):
        if url not in pages:
            try:
                pages[url] = fetch(url) if fetch else _default_fetch(url)
            except Exception as e:                           # noqa: BLE001
                pages[url] = e
        return pages[url]

    for field, r in rows.items():
        for src in (r.get("sources") or []):
            url = str((src or {}).get("url") or "")
            if not url:
                continue
            html = _html_of(url)
            if isinstance(html, Exception):
                ng.append(f"{slug} / {field}: 出典を取り直せません"
                          f"（{url}／{str(html)[:60]}）")
                continue
            try:
                # ★引用ごとに必ず照合する★（取得はしない＝上で1回だけ）
                got = verify_source(dict(src), name or slug,
                                    lambda _u, _h=html: _h)
            except Exception as e:                           # noqa: BLE001
                ng.append(f"{slug} / {field}: 出典を確かめ直せません"
                          f"（{url}／{str(e)[:60]}）")
                continue
            # ★★2AIで通した出典は、いまの本文の指紋と必ず比べる★★
            #   （2026-08-24・Codexの9回目）
            #   ★直す前は「新しく作られた指紋」と比べていた★ので、
            #   ①ページが機械で同定できるようになると指紋が作られず素通り
            #   ②控えから指紋だけ消すと比較そのものが飛んだ
            #   → **いまの本文から自分で計算して**比べる。
            old = (src or {}).get("identity_override") or {}
            if old:
                import hashlib as _hl
                import new_machine_watch as _w9
                # ★保存したときと同じ作り方で本文を出す★
                #   （2026-08-24・Codexの13回目＝別々に作っていた）
                now_sha = _hl.sha256(
                    page_text(html, url).encode("utf-8")).hexdigest()
                if not old.get("text_sha256"):
                    ng.append(f"{slug} / {field}: 2AIで通した出典に"
                              f"本文の指紋がありません（{url}）"
                              "／判断し直してください")
                elif old["text_sha256"] != now_sha:
                    ng.append(f"{slug} / {field}: 出典の本文が変わっています"
                              f"（{url}）／2AIで判断し直してください")
    return ng

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


def for_slug_checked(slug: str) -> dict:
    """★その機種の記録だけを、契約つきで読む★（2026-08-24・Codexの9回目）

    ★全件を厳しく見ると、無関係な古い1件で今夜の新台が止まる★。
    控えの存在と入れ物は必ず確かめ、**中身の契約は対象機種だけ**見る。
    """
    d = load(strict=False, require_exists=True)
    raw = (d.get("machines") or {})
    if slug in raw and not isinstance(raw[slug], dict):
        # ★★空の入れ物を「0件」と読まない★★（2026-08-24・Codexの10回目）
        #   `[]` や `""` や null だと、契約違反にならず「確定値なし」で進んだ。
        raise ConfirmedError(
            f"{slug} の確定値の入れ物が壊れています（{type(raw[slug]).__name__}）")
    rows = dict(raw.get(slug) or {})
    bad = []
    for field, rec in rows.items():
        bad += validate_record(field, rec)
    if bad:
        raise ConfirmedError(
            f"{slug} の確定値が契約を満たしていません: "
            + " ／ ".join(bad[:5])
            + (f" ほか{len(bad) - 5}件" if len(bad) > 5 else ""))
    return rows


def for_slug(slug: str, data: dict | None = None) -> dict:
    """機械が毎回読む側（無人タスクはここだけ使う）。

    ★控えが無い／壊れているときは止める★（2026-08-24・Codexの7回目）
      作るのは `record()` の仕事。読む側が黙って0件にしない。
    """
    if data is not None:
        return dict((data.get("machines") or {}).get(slug) or {})
    return for_slug_checked(slug)


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
            # ★★どの項目の控えかを刻む★★（2026-08-24・Codexの5回目）
            #   ★直す前は値だけを照合していた★ので、
            #   同じ形の値があれば**別項目の控えを証明に使えた**
            #   （例：出玉率の控えで AT初当たり確率を通す）。
            #   表示は項目名に従うので、値を**別の意味で**読者へ出せた。
            "_field": field,
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
            row["_field"] = field          # ★どの項目の控えか★（上と同じ理由）
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
    # ★★下ごしらえが落ちても、❌として数える★★（2026-08-24）
    #   ★直す前はここで例外が出ると、試験は1つも❌を出さずに終わった★＝
    #   壊し方の通し確認から見ると「ただ落ちただけ」になり、
    #   **その守りを見ている試験がある証拠にならなかった**。
    _init_ok, _init_why = True, ""
    try:
        init_store()      # ★初回は明示的に作る★（2026-08-24・Codexの9回目）
    except Exception as _e:                                  # noqa: BLE001
        _init_ok, _init_why = False, f"{type(_e).__name__}: {_e}"
        with open(STORE, "w", encoding="utf-8") as _fh:      # 続けるための土台
            json.dump({"schema_version": SCHEMA, "machines": {}}, _fh)
    try:
        t("★★控えが無い所では、初回の作成ができる★★"
          "／★『消えた』と『初回』を区別できないと、確定値が抜けた記事が出る★"
          + ("" if _init_ok else "／理由: " + _init_why), _init_ok)
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
        # ★★控えを読むときも契約を確かめる★★（2026-08-24・Codexの6回目）
        #   ★直す前は版番号しか見ていなかった★ので、控えへ形だけ正しい
        #   偽の記録を置けば、出典0件・判断者0人でも関所を越えられた。
        _keep_store = STORE
        try:
            import tempfile as _tf9
            STORE = os.path.join(_tf9.mkdtemp(prefix="cvbad_"),
                                 "confirmed_values.json")
            with open(STORE, "w", encoding="utf-8") as _fh9:
                json.dump({"schema_version": SCHEMA, "machines": {
                    "zzz": {"ceiling": {"value": {"kind": "GAME",
                                                  "amount": "999",
                                                  "unit": "G",
                                                  "benefit": "AT"}}}}},
                          _fh9, ensure_ascii=False)
            _bad_raised = False
            try:
                load()
            except ConfirmedError:
                _bad_raised = True
            t("★★出典も判断者も無い記録は、読む時点で断る★★"
              "／★書き込み口だけ厳しくしても、読み込み口が素通りなら意味がない★",
              _bad_raised)
            t("　（対照）中身を見ない読み方なら通ってしまう",
              isinstance(load(strict=False), dict))
        finally:
            STORE = _keep_store

        # ★★数は「別の数の一部」では通さない★★（2026-08-24・Codexの8回目）
        #   ★直す前はただの部分一致だった★ので、
        #   出典に書かれていない数を「書かれている」ことにできた。
        for _tk, _q, _want in (("3.1", "純増は13.1枚/G", False),
                               ("3.1", "純増は3.1枚/G", True),
                               ("100", "天井は1000G", False),
                               ("100", "天井は100G", True),
                               ("3.1", "純増は3.10枚/G", False),
                               ("AT当選", "恩恵はAT当選です", True)):
            t(f"★数の照合：『{_tk}』と『{_q}』→ {_want}★",
              token_in_quote(_tk, _q) is _want)
        # ★書き込みの側でも同じ★（引用に別の数しか無ければ記録できない）
        _num_bad = False
        try:
            check_shape("at_net_unmapped",
                        {"values": ["3.1"], "mapping": "UNCONFIRMED"})
            if token_in_quote("3.1", "純増は13.1枚/G"):
                _num_bad = False
            else:
                _num_bad = True
        except Exception:                                    # noqa: BLE001
            _num_bad = False
        t("　記録するときも、別の数の一部では通さない", _num_bad)

        # ★★純増は引用と照合する★★（2026-08-24・Codexの7回目）
        #   ★直す前は照合対象が空だった★ので、出典に無い数を載せられた。
        _np_toks = check_shape("at_net_unmapped",
                               {"values": ["3.1", "7.4"],
                                "mapping": "UNCONFIRMED"})
        t("★★純増の値は引用と照合する★★"
          "／★照合しないと、出典に無い数を記事に載せられる★",
          "3.1" in _np_toks and "7.4" in _np_toks)
        _np_bad = False
        try:
            check_shape("at_net_unmapped",
                        {"values": ["約3.1枚"], "mapping": "UNCONFIRMED"})
        except ConfirmedError:
            _np_bad = True
        t("　単位や「約」の付いた値は受け取らない（約約3.1枚枚/G になる）",
          _np_bad)

        # ★★控えが消えたら止まる★★（2026-08-24・Codexの7回目）
        #   ★直す前は不存在を正常な0件として返していた★ので、
        #   控えが消えた日に**確定値が全部抜けた記事**を作れた。
        _keep2 = STORE
        try:
            import tempfile as _tf8
            STORE = os.path.join(_tf8.mkdtemp(prefix="cvnone_"),
                                 "confirmed_values.json")
            _gone = False
            try:
                load(require_exists=True)
            except ConfirmedError:
                _gone = True
            t("★★控えが消えていたら、無人の読み口は止まる★★"
              "／★『0件』と読むと、確定値が全部抜けた記事が黙って出る★",
              _gone)
            t("　（対照）作る側の読み方なら、無くても0件で始められる",
              load(strict=False)["machines"] == {})
            # ★機種の並びが無い／別の形★も止める
            with open(STORE, "w", encoding="utf-8") as _fh8:
                json.dump({"schema_version": SCHEMA}, _fh8)
            _gone2 = False
            try:
                load(require_exists=True)
            except ConfirmedError:
                _gone2 = True
            t("　機種の並びが無い控えも止める", _gone2)
        finally:
            STORE = _keep2

        # ★★系列を数え直す★★（保存された系列を信じない）
        _one = {"value": {"kind": "GAME", "amount": "999", "unit": "G",
                          "benefit": "AT"},
                "sources": [{"url": "https://chonborista.com/slot/x",
                             "quote": "999 G AT"}],
                # ★保存された系列は「数え直した結果」と同じにする★
                #   （食い違いの検査に助けられると、系列の数の検査を
                #     外しても試験が緑のままになる＝実際にそうなった）
                "lineages": ["vote:chonborista"],
                "agreed_by": ["claude", "codex"],
                "why": "2AIで突き合わせました",
                "decided_at": "2026-08-24"}
        # ★★系列0件でも比べる★★（2026-08-24・Codexの8回目）
        _empty_lin = {"value": {"kind": "GAME", "amount": "999", "unit": "G",
                                "benefit": "AT当選"},
                      "sources": [
                          {"url": "https://chonborista.com/slot/x",
                           "quote": "999 G AT当選"},
                          {"url": "https://nana-press.com/kaiseki/x",
                           "quote": "999 G AT当選"}],
                      "lineages": [],
                      "agreed_by": ["claude", "codex"],
                      "why": "2AIで突き合わせました",
                      "decided_at": "2026-08-24"}
        t("★★系列が空の記録も、数え直しと突き合わせる★★"
          "／★空だと比較そのものを飛ばしていた★",
          any("系列" in x for x in validate_record("ceiling", _empty_lin)))
        # ★まとめ方は正本と同じ★（状態だけ無視する）
        import source_lineage as _sl9
        t("★★状態を無視した数え方でも、票のまとめ方は正本と同じ★★"
          "／★書き起こすと同じ運営を2票と数える★",
          set(_sl9.vote_groups_any().values())
          >= set(_sl9.vote_groups().values()))

        t("★★1つの出典しか無い記録は、控えを読む時点で断る★★"
          "／★保存された系列の申告を信じない★",
          any("系列" in x for x in validate_record("ceiling", _one)))

        # ★★公開直前の再検証★★（2026-08-24・Codexの8回目）
        #   ★控えの読み直しは、保存されたURLと引用を信じている★ので、
        #   手で書き換えられた偽の引用は見破れない。取り直して確かめる。
        _keep3 = STORE
        try:
            import tempfile as _tf7
            STORE = os.path.join(_tf7.mkdtemp(prefix="cvrv_"),
                                 "confirmed_values.json")
            # ★実在する機種で試す★（機種名は一覧から引かれる）
            _rows_m = _sj.read_json(
                os.path.join(BASE, "assets", "data", "machines.json"),
                expect=(dict, list))
            _rows_m = (_rows_m["machines"] if isinstance(_rows_m, dict)
                       else _rows_m)
            _rv_slug = str(_rows_m[0]["slug"])
            _rv_name = str(_rows_m[0]["name"])
            _quote = f"{_rv_name}の解析 999 G AT当選 と確認できました。"

            def _page(_q):
                return ("<html><head><title>" + _rv_name
                        + "</title></head><body><h1>" + _rv_name
                        + f"</h1><p>{_q}</p></body></html>")

            _hits = []

            def _fetch_ok(u):
                _hits.append(u)
                return _page(_quote)

            json.dump({"schema_version": SCHEMA, "machines": {_rv_slug: {
                "ceiling": {
                    "value": {"kind": "GAME", "amount": "999", "unit": "G",
                              "benefit": "AT当選"},
                    "sources": [
                        {"url": "https://chonborista.com/slot/x",
                         "quote": _quote},
                        {"url": "https://nana-press.com/kaiseki/x",
                         "quote": _quote}],
                    "lineages": ["vote:chonborista", "vote:nana-press"],
                    "agreed_by": ["claude", "codex"],
                    "why": "2AIで突き合わせました",
                    "decided_at": "2026-08-24",
                    "official_url": ""}}}},
                open(STORE, "w", encoding="utf-8"), ensure_ascii=False)
            t("★★取り直して引用が実在すれば通る★★",
              reverify(_rv_slug, fetch=_fetch_ok) == [])
            t("　同じURLは1回しか取りに行かない（出典2件で2回）",
              len(_hits) == 2)

            def _fetch_changed(u):
                return _page("いまは別のことが書いてあります。")
            t("★★引用が消えていたら公開前に気づく★★"
              "／★控えを手で書き換えても、取り直せば分かる★",
              reverify(_rv_slug, fetch=_fetch_changed) != [])

            # ★★2AIで本人性を通した出典は、本文が変わったら知らせる★★
            #   （2026-08-24・Codexの8回目）
            #   ★2AIが「この機種のページだ」と判断した前提は、
            #     そのときの本文★。本文が変わっていたら判断し直す。
            #   題が機種名でないページ＝機械では同定できないので、
            #   2AIの判断（理由と逐語）で通した形を作る。
            def _page2(_body):
                return ("<html><head><title>解析まとめ</title></head><body>"
                        f"<p>{_rv_name} の解析です。</p><p>{_body}</p>"
                        "</body></html>")

            _proof = f"{_rv_name} の解析です。"
            _src_ov = {"url": "https://chonborista.com/slot/y",
                       "quote": _quote,
                       "identity_why": "2AIで機種名と導入日を突き合わせました",
                       "identity_proof": _proof}
            _ov = verify_source(dict(_src_ov), _rv_name,
                                lambda u: _page2(_quote))
            json.dump({"schema_version": SCHEMA, "machines": {_rv_slug: {
                "ceiling": {
                    "value": {"kind": "GAME", "amount": "999", "unit": "G",
                              "benefit": "AT当選"},
                    "sources": [
                        _ov,
                        {"url": "https://nana-press.com/kaiseki/x",
                         "quote": _quote}],
                    "lineages": ["vote:chonborista", "vote:nana-press"],
                    "agreed_by": ["claude", "codex"],
                    "why": "2AIで突き合わせました",
                    "decided_at": "2026-08-24",
                    "official_url": ""}}}},
                open(STORE, "w", encoding="utf-8"), ensure_ascii=False)
            t("　（前提）本文が同じままなら通る",
              reverify(_rv_slug,
                       fetch=lambda u: (_page2(_quote)
                                        if "chonbo" in u else _page(_quote)))
              == [])
            t("★★2AIで通した出典の本文が変わったら、公開前に知らせる★★"
              "／★引用は残っていても、判断の前提は崩れている★",
              [x for x in reverify(
                  _rv_slug,
                  fetch=lambda u: (_page2(_quote + " なお内容を更新しました。")
                                   if "chonbo" in u else _page(_quote)))
               if "本文が変わって" in x])
        finally:
            STORE = _keep3

        # ★★出典の投稿欄は根拠にしない★★（2026-08-24・Codexの9回目）
        #   ★材料を読む側は落としているのに、確定値の経路だけ抜けていた★＝
        #   ちょんぼりすたの読者コメントに「天井999G」とあれば、
        #   それを逐語引用として記録・再検証できた。
        _ua_url = "https://chonborista.com/slot/orinpia-slot/264134/"
        _ua_html = ('<title>L試験機 スロット 新台 解析 | ちょんぼりすた</title>'
                    '<div id="hyouka">星の評価</div>'
                    '<ul class="commentlist"><li>読者の書き込み '
                    '天井は999G と確認できました。</li></ul>'
                    '<div id="entry"><div>機種名 L試験機</div>'
                    '<div>メーカー 京楽</div></div>')
        # ★★既定の道をそのまま通す★★（通信の手だてだけ差し替える）
        #   ★取得の関数を渡して試すと、既定の道を一度も通らない★＝
        #   既定を壊しても試験が緑のままだった（実際にそうなった）。
        import new_machine_watch as _w9b
        _real_get9 = _w9b._get

        def _g9(_x, timeout=20):
            _w9b.LAST_FINAL_URL["url"] = _x       # ★到達先を名乗る★
            return _ua_html
        try:
            _w9b._get = _g9
            _ua_bad = False
            try:
                verify_source(
                    {"url": _ua_url,
                     "quote": "読者の書き込み 天井は999G と確認できました。"},
                    "L試験機")
            except ConfirmedError:
                _ua_bad = True
            t("★★出典ページの投稿欄は根拠にできない★★"
              "／★読者の書き込みを『出典に書いてある』にできた★",
              _ua_bad)
            t("　（対照）本文に書いてあるものは通る",
              verify_source({"url": _ua_url, "quote": "機種名 L試験機"},
                            "L試験機").get("verified_at"))
        finally:
            _w9b._get = _real_get9

        # ★★読み直しの判断者は、書き込みと同じ顔ぶれ★★
        _judge = {"value": {"kind": "GAME", "amount": "999", "unit": "G",
                            "benefit": "AT当選"},
                  "sources": [
                      {"url": "https://chonborista.com/slot/x",
                       "quote": "999 G AT当選"},
                      {"url": "https://nana-press.com/kaiseki/x",
                       "quote": "999 G AT当選"}],
                  "lineages": ["vote:chonborista", "vote:nana-press"],
                  "agreed_by": ["a", "b"],
                  "why": "2AIで突き合わせました",
                  "decided_at": "2026-08-24"}
        t("★★読み直しでも判断者の顔ぶれを求める★★"
          "／★『違う文字列が2つ』では、手書きの記録が通る★",
          any("判断者" in x for x in validate_record("ceiling", _judge)))

        # ★★控えが無いときに黙って作らない★★（消失と初回を区別する）
        _keep4 = STORE
        try:
            import tempfile as _tf6
            _dinit = _tf6.mkdtemp(prefix="cvinit_")
            STORE = os.path.join(_dinit, "confirmed_values.json")
            # ★機種の正本も一時の場所に用意する★
            #   （記録は公式URLから機種名を引くので、無いと別の理由で落ちる）
            _lp_real6 = _lp.DOCS
            _lp.DOCS = _dinit
            _iurl = "https://m.example/products/slot/zzz_init/"
            with open(os.path.join(_dinit, "add_machine_pending.json"), "w",
                      encoding="utf-8") as _fh6:
                json.dump({"items": {_iurl: {"name": "L試験機"}}}, _fh6,
                          ensure_ascii=False)
            # ★本物の登録関数を、出典もそろえて呼ぶ★
            #   （そろっていないと別の理由で落ち、控えの話にならない）
            _iq = "L試験機の解析 999 G AT当選 と確認できました。"

            def _ifetch(u):
                return ("<html><head><title>L試験機</title></head><body>"
                        f"<h1>L試験機</h1><p>{_iq}</p></body></html>")
            _made = False
            try:
                record("", "ceiling",
                       {"kind": "GAME", "amount": "999",
                        "unit": "G", "benefit": "AT当選"},
                       [parse_source("https://chonborista.com/slot/x|" + _iq),
                        parse_source("https://nana-press.com/kaiseki/x|" + _iq)],
                       ["claude", "codex"], "2AIで突き合わせました",
                       official_url=_iurl, fetch=_ifetch)
            except ConfirmedError as e:
                _made = "控えがありません" in str(e)
            except Exception:                                # noqa: BLE001
                _made = False
            t("★★控えが無いときに、記録が勝手に作らない★★"
              "／★消失事故のあとに空の控えが黙って再生していた★",
              _made and not os.path.exists(STORE))
            # ★表明の中で例外が出ると、試験ごと落ちて❌が1つも出ない★
            #   （2026-08-24＝壊し方の通し確認から見ると「ただ落ちただけ」）
            try:
                _mk = init_store() == STORE and os.path.exists(STORE)
            except Exception as _e2:                         # noqa: BLE001
                _mk = False
                print("  （初回作成が例外: " + type(_e2).__name__ + "）")
            t("　初回は明示的に作る（--init）", _mk)
            _twice = False
            try:
                init_store()
            except ConfirmedError:
                _twice = True
            t("　すでにあるなら作り直さない", _twice)
        finally:
            STORE = _keep4
            _lp.DOCS = _lp_real6

        # ★★別のサイトへ転送されていたら使わない★★（2026-08-24・Codexの10回目）
        #   ★本文だけ受け取って着いた先を捨てていた★ので、
        #   投稿欄の決まりは頼んだURL側・本文は着いた先、という
        #   食い違いが起きた（同じページが2票にもなり得る）。
        import new_machine_watch as _w10
        _real_get10 = _w10._get

        def _g10(_x, timeout=20):
            _w10.LAST_FINAL_URL["url"] = "https://nana-press.com/kaiseki/x/"
            return "<html><head><title>x</title></head><body>y</body></html>"
        try:
            _w10._get = _g10
            _moved = False
            try:
                _default_fetch("https://chonborista.com/slot/x/")
            except ConfirmedError as e:
                _moved = "転送" in str(e)
            t("★★別のサイトへ転送された出典は使わない★★"
              "／★投稿欄の決まりと本文が食い違い、票も二重になる★",
              _moved)
        finally:
            _w10._get = _real_get10

        # ★★決まりごとが無いサイトでも、投稿文は引用にできない★★
        #   （2026-08-24・Codexの12回目）
        #   ★前回は同定の根拠の側だけ直して「引用を直した」と報告していた★。
        #   読者に出る値の引用は素通しのままだった＝また「片方だけ直した」。
        #   ★決まりごとがあるサイトで試すと、取ってくる時点で箱が落ちるので
        #     この抜けを検出できない★ので、**決まりごとが無いサイト**で試す。
        import new_machine_watch as _w12
        import user_area as _ua12
        _norule = "https://nana-press.com/kaiseki/machine/1/"
        t("　（前提）この先には投稿欄の決まりごとが無い",
          not [r for r in (_ua12.conf_for_url(_norule).get("drop") or [])
               if isinstance(r, dict)])
        _nr_html = ("<html><head><title>L試験機 解析</title></head><body>"
                    "<h1>L試験機</h1><p>天井は999Gです。</p>"
                    "<h2>口コミ</h2><p>読者A 天井は555Gだと思う</p>"
                    "</body></html>")
        _real_get12 = _w12._get

        def _g12(_x, timeout=20):
            _w12.LAST_FINAL_URL["url"] = _x
            return _nr_html
        try:
            _w12._get = _g12
            _nr_bad = False
            try:
                verify_source({"url": _norule,
                               "quote": "読者A 天井は555Gだと思う"},
                              "L試験機")
            except ConfirmedError:
                _nr_bad = True
            t("★★決まりごとが無いサイトに投稿欄があれば、そのページを使わない★★"
              "／★行切りは文章にしか効かない＝表を読む経路が守れない★"
              "（2026-08-24・Codexの13回目）",
              _nr_bad)
            # ★★どこで止まったかまで見る★★（取得の段で止まるのが正しい）
            #   ★「例外が出た」だけで満足しない★＝別の理由で落ちていても
            #   試験は緑になる（今日それを1度やった）。
            _why13 = ""
            try:
                verify_source({"url": _norule,
                               "quote": "読者A 天井は555Gだと思う"},
                              "L試験機")
            except ConfirmedError as _e13:
                _why13 = str(_e13)
            t("　止まる場所は「取ってくる段」（表も文章もまとめて守れる）",
              "掃除のあとにも投稿欄が残っています" in _why13)

            # ★対照★ 投稿欄が無いページなら、これまでどおり通る
            _nr_clean = ("<html><head><title>L試験機 解析</title></head><body>"
                         "<h1>L試験機</h1><p>天井は999Gです。</p></body></html>")

            def _g13(_x, timeout=20):
                _w12.LAST_FINAL_URL["url"] = _x
                return _nr_clean
            _w12._get = _g13
            t("　（対照）投稿欄が無いページは、これまでどおり通る",
              verify_source({"url": _norule, "quote": "天井は999Gです。"},
                            "L試験機").get("verified_at"))
        finally:
            _w12._get = _real_get12

        # ★★奥の層も直接試す★★（2026-08-24・Codexの13回目のあと）
        #   ★取ってくる段で止める守りを入れたら、その先の守りが
        #     試験で一度も通らなくなった★（壊し方3件が捕まらなくなった）。
        #   守りは重ねてある（取得で止める／行で切る／引用を照合する）ので、
        #   ★奥の層は取得を通さずに直接試す★。
        _deep_url = "https://nana-press.com/kaiseki/machine/2/"
        _deep_html = ("<html><head><title>L試験機 解析</title></head><body>"
                      "<h1>L試験機</h1><p>天井は999Gです。</p>"
                      "<p>口コミ</p><p>読者A 天井は555Gだと思う</p>"
                      "</body></html>")
        t("★★決まりごとが無いページは、投稿より前だけを本文にする★★"
          "／★行で切る前に1行へ潰すと、切れなくなる★",
          "999" in page_text(_deep_html, _deep_url)
          and "555" not in page_text(_deep_html, _deep_url))
        _deep_bad = False
        try:
            verify_source({"url": _deep_url,
                           "quote": "読者A 天井は555Gだと思う"},
                          "L試験機", lambda _u: _deep_html)
        except ConfirmedError:
            _deep_bad = True
        t("★★引用の照合も同じ本文の上でやる★★"
          "／★ここだけ素通しに戻すと、投稿文が根拠になる★",
          _deep_bad)
        t("　（対照）本文に書いてあるものは通る",
          verify_source({"url": _deep_url, "quote": "天井は999Gです。"},
                        "L試験機", lambda _u: _deep_html).get("verified_at"))

        # ★★指紋も同じ作り方で出す★★（保存時と再確認で食い違わせない）
        #   ★自分で計算して比べるだけでは、本体を通らない★
        #   （2026-08-24＝それをやって、壊し方が捕まえられなかった）。
        #   ★本体の reverify を通して確かめる★
        import hashlib as _hl13
        _keep13 = STORE
        try:
            import tempfile as _tf13
            STORE = os.path.join(_tf13.mkdtemp(prefix="cvsha_"),
                                 "confirmed_values.json")
            _rows_m13 = _sj.read_json(
                os.path.join(BASE, "assets", "data", "machines.json"),
                expect=(dict, list))
            _rows_m13 = (_rows_m13["machines"]
                         if isinstance(_rows_m13, dict) else _rows_m13)
            _slug13 = str(_rows_m13[0]["slug"])
            _name13 = str(_rows_m13[0]["name"])
            _h13 = ("<html><head><title>解析まとめ</title></head><body>"
                    f"<p>{_name13} の解析です。</p><p>天井は999Gです。</p>"
                    "<p>口コミ</p><p>読者A 天井は555Gだと思う</p></body></html>")
            # ★保存時と同じ作り方で指紋を作る★
            _sha13 = _hl13.sha256(
                page_text(_h13, _deep_url).encode("utf-8")).hexdigest()
            json.dump({"schema_version": SCHEMA, "machines": {_slug13: {
                "ceiling": {
                    "value": {"kind": "GAME", "amount": "999", "unit": "G",
                              "benefit": "AT当選"},
                    "sources": [
                        {"url": _deep_url, "quote": "天井は999Gです。",
                         "identity_why": "2AIで機種名と導入日を合わせました",
                         "identity_proof": f"{_name13} の解析です。",
                         "identity_override": {
                             "why": "2AIで機種名と導入日を合わせました",
                             "proof": f"{_name13} の解析です。",
                             "machine_said": "NAME_CORE_MISMATCH",
                             "text_sha256": _sha13,
                             "at": "2026-08-24"}},
                        {"url": "https://chonborista.com/slot/x",
                         "quote": "天井は999Gです。"}],
                    "lineages": ["vote:chonborista", "vote:nana-press"],
                    "agreed_by": ["claude", "codex"],
                    "why": "2AIで突き合わせました",
                    "decided_at": "2026-08-24",
                    "official_url": ""}}}},
                open(STORE, "w", encoding="utf-8"), ensure_ascii=False)
            _rv13 = reverify(_slug13, fetch=lambda _u: _h13)
            t("★★保存した指紋と同じ作り方なら、本文が同じ限り止まらない★★"
              "／★別の作り方だと、本文が変わっていないのに本番が止まる★",
              not [x for x in _rv13 if "本文が変わって" in x])
        finally:
            STORE = _keep13

        # ★★2AIで通した出典は、指紋の箱ごと必須★★
        #   ★直す前は「箱があるときだけ」見ていた★ので、箱ごと消すと素通り。
        _nobox = {"value": {"kind": "GAME", "amount": "999", "unit": "G",
                            "benefit": "AT当選"},
                  "sources": [
                      {"url": "https://chonborista.com/slot/x",
                       "quote": "999 G AT当選",
                       "identity_why": "2AIで機種名と導入日を突き合わせました",
                       "identity_proof": "L試験機 の解析です。"},
                      {"url": "https://nana-press.com/kaiseki/x",
                       "quote": "999 G AT当選"}],
                  "lineages": ["vote:chonborista", "vote:nana-press"],
                  "agreed_by": ["claude", "codex"],
                  "why": "2AIで突き合わせました",
                  "decided_at": "2026-08-24"}
        t("★★2AIで通した出典に指紋の箱が無ければ断る★★"
          "／★箱ごと消せば比較が全部飛んだ★",
          any("指紋" in x for x in validate_record("ceiling", _nobox)))

        # ★★対象機種の入れ物が壊れていたら「0件」と読まない★★
        _keep10 = STORE
        try:
            import tempfile as _tf10
            STORE = os.path.join(_tf10.mkdtemp(prefix="cvbox_"),
                                 "confirmed_values.json")
            with open(STORE, "w", encoding="utf-8") as _fh10:
                json.dump({"schema_version": SCHEMA,
                           "machines": {"zzz_box": []}}, _fh10)
            _box = False
            try:
                for_slug_checked("zzz_box")
            except ConfirmedError as e:
                _box = "入れ物" in str(e)
            t("★★入れ物が壊れていたら『確定値なし』にしない★★"
              "／★空の配列や null が『0件』として素通りしていた★",
              _box)
        finally:
            STORE = _keep10

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

    t("★★配列を許すのは gains だけ★★（依頼182のP1）"
      "／全項目で許すと記事にPythonの配列表記が出る（再現済み）",
      all(not _passes("gameplay", {"trigger": ["あ", "い"], "leads_to": "上乗せ"})
          for _ in (1,))
      and not _passes("gameplay", {"trigger": "参戦チャンス",
                                   "leads_to": "上乗せ", "gains": "文字列"})
      and not _passes("gameplay", {"trigger": "参戦チャンス",
                                   "leads_to": "上乗せ", "gains": ["ok", ""]})
      and _passes("gameplay", {"trigger": "参戦チャンス",
                               "leads_to": "上乗せ",
                               "gains": ["上乗せ", "武将参戦"]}))

    t("★★配列を許すのは gains だけ・全部の鍵で見る★★（依頼185のP1）"
      "／以前は照合する鍵しか見ておらず、ceiling.benefit に配列を渡すと"
      "記事にPythonの配列表記が出た",
      not _passes("ceiling", {"kind": "GAME", "amount": "999",
                             "unit": "G", "benefit": ["あ", "い"]})
      and not _passes("ceiling", {"kind": "GAME", "amount": "999",
                                 "unit": "G", "benefit": {"x": 1}})
      and _passes("ceiling", {"kind": "GAME", "amount": "999",
                             "unit": "G", "benefit": "AT当選"}))
    t("　gains は「中身のある文字列の配列」でなければ受け取らない",
      not _passes("gameplay", {"trigger": "x", "leads_to": "y",
                               "gains": []})
      and not _passes("gameplay", {"trigger": "x", "leads_to": "y",
                                   "gains": ["a", ""]})
      and _passes("gameplay", {"trigger": "x", "leads_to": "y",
                               "gains": ["上乗せ"]}))

    # ★★数字が文頭・文末にあっても照合できる★★（2026-08-25・自分で踏んだ）
    #   ★空文字はどんな文字列にも含まれる★ので、
    #   `before in "0123456789."` と書くと**文頭・文末で必ず弾いていた**。
    #   ＝引用に「600G」「天井は600」と書いてあっても記録できない
    #     ＝2AIが正しく確定した値が入らない（正しい答えが届かない経路）。
    for _q20 in ("600G", "600", "天井は600", "A 600 B", "天井は600です"):
        t("　引用「" + _q20 + "」に 600 が書いてあると分かる",
          token_in_quote("600", _q20))
    # ★対照★＝別の数の一部は今までどおり弾く
    for _t20, _q20 in (("3.1", "純増は13.1枚/G"), ("100", "天井は1000G"),
                       ("3.1", "3.10枚"), ("600", "1600G"),
                       # ★けた区切りのカンマも数の一部★（Codexの21回目）
                       ("600", "天井は1,600Gです"), ("600", "出玉600,000枚"),
                       ("1", "1,600G")):
        t("　（対照）" + _t20 + " は「" + _q20 + "」の中の別の数と混同しない",
          not token_in_quote(_t20, _q20))

    # ★★記事に届かない項目は受け取らない★★（2026-08-25・Codexの22回目）
    #   ★素の net_increase は受け取れるのに、記事が一度も読まなかった★
    #   ＝2AIが正しく答えても公開に届かない（答えが迷子になる）。
    t("★★記事が読まない項目は、入口で断る（net_increase）★★"
      "／★受け取れるのに届かないと、2AIの答えが迷子になる★",
      "net_increase" not in allowed_fields())
    t("　構造化された受け口は開いたまま（at / at_net_unmapped）",
      "at" in allowed_fields() and "at_net_unmapped" in allowed_fields())
    t("　閉じた項目は、記事の話題も空（読者に出さない印）",
      topic_of("net_increase") == "")

    ng = sum(1 for _, o in results if not o)
    print()
    print("%d/%d 合格" % (len(results) - ng, len(results)))
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="2AIで確定した値の受け取り口")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--forget", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--init", action="store_true",
                    help="★初回だけ★空の控えを作る（復旧には使わない）")
    ap.add_argument("--if-missing", dest="if_missing", action="store_true",
                    help="--init と一緒に使う＝すでにあるなら何もしない")
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
    if a.init:
        # ★初回と「消えた」を区別する★＝復旧はバックアップから戻すこと
        if a.if_missing and os.path.exists(STORE):
            # ★機械が何度も流す場所のための形★（CIは毎回まっさら）
            print("すでにあります（何もしません）: " + STORE)
            return 0
        print("作りました: " + init_store())
        return 0
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

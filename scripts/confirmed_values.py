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
      --value-file <値のJSON> --source "https://…|天井は1000G+α" \\
      --source "https://…|通常時1000G+αで天井" \\
  # ★出典は「URL|逐語の引用」の2つ★（発行者は名乗らせない＝
  #   URLのホストから機械が引く。名乗らせると別ホストへ付け替えられる）
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
import unicodedata

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
    # ★★ボーナス確率（設定×BIG/REG/合算）★★（2026-08-26）
    #   ★カテゴリ（bonusflow）と topic（setting）で答えが違うのは正常★
    #   ＝カテゴリは「証拠の種類」、topicは「記事のどの話題か」。別の軸。
    "bonus_prob": "setting",        # ボーナス確率（設定別）
    # ★素の net_increase は受け取らない★（2026-08-25・Codexの22回目）
    #   ★受け取れるのに、記事を作る側が一度も読まない★状態だった＝
    #   2AIが正しく答えても**公開に届かない**（型式名・天井と同じ型）。
    #   しかも採取規則は「純増はどのモードか」を必須にしているのに、
    #   素の値は自由な文字として通っていた。
    #   ★構造化された `at` か `at_net_unmapped` を使う★（どちらも記事に出る）。
    "net_increase": "",             # ★受け口を閉じた（下の除外と対）★
    # ★型は読者に出さない★（判定の線を選ぶためだけ）
    "machine_profile": "",
    # ★天井の有無は「天井・恩恵」の箱に出る★（「天井はありません」）
    "ceiling_state": "ceiling",
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
    # ★★機種の型★★（2026-08-25・Codexの27回目の設計助言）
    #   ★これが無いと、ノーマル機は永久に検索へ載せられない★＝
    #   掲載判定の3つ目が `at:`/`cz:` を必ず要求していたため。
    #   ★機械が「完全告知のボーナスタイプ」という文を読み取って決めない★
    #   ＝意味の判断は2AIの仕事。
    "machine_profile": "adopted",
    # ★★天井の有無★★（★型から推論してはいけない★・Codexの助言）
    #   実例＝X-300 は概要が「完全告知のボーナスタイプ」だが、
    #   天井欄は「調査中」＝型が分かっても天井の有無は分からない。
    "ceiling_state": "adopted",
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
    # ★型は3つのどれか★（表示には出さない。掲載判定の線を選ぶだけ）
    "machine_profile": {"required": ("profile",),
                        "enums": {"profile": ("AT_CZ", "BONUS")},
                        "quoted": ()},
    # ★天井の有無★（PRESENT / NONE のどちらかを確定したときだけ記録する）
    "ceiling_state": {"required": ("state",),
                      "enums": {"state": ("PRESENT", "NONE")},
                      "quoted": ()},
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
    if kind == "per_setting_matrix":
        # ★形の決まりは spec_lookup の1か所★（2026-08-26）
        #   ★同じ規則を2か所に書かない★＝収集器・受け口・判定書が
        #   同じ関数を通る（片方だけ緩いと、そこから壊れた値が入る）。
        try:
            _sp.validate_bonus_prob_value(value)
        except _sp.BonusShapeError as e:
            raise ConfirmedError(f"{field}: {e}")
        # ★引用の照合に使う語★＝表に出る数値そのもの
        out = []
        for st in sorted(value):
            for ck in sorted(value[st]):
                out.append(str(value[st][ck]))
        return out
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
    """★どんな中身でも問題の一覧を返す★（2026-09-09・Codexの指摘）

    ★直す前★＝必須の鍵がそろっていれば先へ進むので、
    出典が文字列・同定の上書きが文字列・URLが壊れている形で
    ★検査そのものが例外で落ちた★。
    ＝読む側も、直す道具も、そこで止まる。
    ★安全側に倒れる★＝落ちたことを問題として返すので、
    `load()` は今までどおり fail-closed で止まる。
    """
    try:
        return _validate_record(field, rec)
    except Exception as e:                                   # noqa: BLE001
        return [f"{field}: 検査できない形です（{type(e).__name__}）"]


def _validate_record(field: str, rec) -> list:
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
                _need = min_sources(base)
                if len(got) < _need:
                    ng.append(f"{field}: 独立した系列が{_need}つ"
                              f"ありません（{got}）")
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
        # ★書き込みと同じ関数を通す★（2026-09-16・台帳#691）＝
        #   ★別々に書くと、控えのファイルを手で書き換えたときに
        #     書き方の控えが一度も確かめられずに効く★。
        ng += wording_problems(field, toks, src)
    return ng


class StoreMissingError(ConfirmedError):
    """★控えのファイルそのものが無い★（2026-09-09・Codexの指摘）

    ★取り除く道具では直せない★＝バックアップから戻すしかない。
    `--init` は初回に作るためのもので、復旧には使わない（消失と初回を
    区別するための決まり）。
    """


class StoreBrokenError(ConfirmedError):
    """★控えの「外側」が壊れている★（2026-09-09・Codexの指摘）

    版番号・最上位の入れ物が壊れている形。1件ずつの記録の話ではないので、
    ★取り除く道具では直せない★＝JSONそのものが壊れているときと同じ案内が要る。
    """


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
            raise StoreMissingError(f"確定値の控えがありません: {STORE}")
        return _empty()
    got = _sj.read_json(STORE, expect=dict)
    if got.get("schema_version") != SCHEMA:
        raise StoreBrokenError(
            f"確定値の形が違います: {got.get('schema_version')}")
    if require_exists and not isinstance(got.get("machines"), dict):
        # ★中身の入れ物ごと無い／空でないものが入っている★
        raise StoreBrokenError("確定値の控えに機種の並びがありません")
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


def _num_edge(q: str, i: int, step: int) -> bool:
    """★位置 i の字が「数の続き」か★（step=-1 なら左へ、+1 なら右へ）

    ★全角も見る★（2026-09-16・Codexの指摘）＝数の字は `isnumeric` が
    全角も漢数字も拾うのに、区切りだけ半角を並べていたので
    「１，６００」の中の「６００」が通っていた。
    ★名簿を並べずに、同じ字へそろえてから比べる★（NFKC）。

    ★★区切りは「その向こうにも数がある」ときだけ数の続き★★
      （2026-09-16・Codexの指摘）
      ★直す前★＝隣が「、」「．」なら無条件に別の数の一部と見ていたので、
      ★句読点として使われただけの正常な引用まで断っていた★。実測＝
        値 ６００ ／ 引用「天井は６００，恩恵はAT当選」→ 断っていた
        値 600   ／ 引用「天井は600, 恩恵はAT当選」  → 断っていた
      ＝これは#691そのものと同じ型（2AIが正しく決めても記録できない）。
    """
    if i < 0 or i >= len(q):
        return False                       # ★文頭・文末は境目★
    c = q[i]
    if c.isnumeric():
        return True
    import unicodedata
    if unicodedata.normalize("NFKC", c) not in ".,":
        return False
    j = i + step
    return 0 <= j < len(q) and q[j].isnumeric()


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

    ★★2026-09-16・Codexの指摘（台帳#691）★★
      ★直す前は「まるごと数の形の字」にしか境目を見ていなかった★ので、
      **数で始まる／終わる言葉**が別の数の一部に一致した。実測＝

        値 1000G        ／ 引用「天井は11000Gです」          → 通っていた
        値 100ゲーム消化 ／ 引用「1100ゲーム消化からCZ」       → 通っていた
        値 千ゲーム消化  ／ 引用「二千ゲーム消化からCZ」       → 通っていた

      ＝★出典と違う数を、書かれていることにできた★（読者に出る数が変わる）。
      ★境目は「その字の端が数かどうか」で見る★＝
      先頭が数の字なら手前が、末尾が数の字なら後ろが、数でないこと。
      ★漢数字も数★（`isnumeric`。二千の「二」で止める）。
    """
    t = str(token or "").strip()
    q = " ".join(str(quote or "").split())
    if not t:
        return False
    for m in _re.finditer(_re.escape(t), q):
        # ★端が数の字のときだけ、その側の境目を見る★（ここが唯一の判定）
        #   ★★区切りは全角も見る★★（2026-09-16・Codexの指摘）
        #     ★直す前★＝数の字は `isnumeric` で全角も漢数字も拾うのに、
        #     区切りは半角の `.,` しか見ていなかった。実測＝
        #       値 ６００ ／ 引用「天井は１，６００G」→ 通っていた
        #       値 １     ／ 引用「天井は１．５G」   → 通っていた
        #     天井の形は `^\d{1,5}$` で全角も入口を通るので、実在しうる。
        #   ★★空文字は「どんな文字列にも含まれる」★★（2026-08-25・自分で踏んだ）
        #     ★直す前は `before in "0123456789."` と書いていた★ので、
        #     数字が**文頭または文末**にあると before/after が空文字になり、
        #     Python では `"" in "0123..."` が真になって**必ず弾いていた**。
        #     ＝引用に「600G」「天井は600」と書いてあっても照合できず、
        #     ★2AIが正しく確定した値を記録できない★（正しい答えが入らない経路）。
        #   ★★けた区切りのカンマも数の一部★★（2026-08-25・Codexの21回目）
        #     ★直す前はカンマを境界と見ていなかった★ので、
        #     値 600 が引用「天井は1,600G」に一致した。
        if t[:1].isnumeric() and _num_edge(q, m.start() - 1, -1):
            continue                       # ★別の数の一部★
        if t[-1:].isnumeric() and _num_edge(q, m.end(), 1):
            continue                       # ★別の数の一部★
        return True
    return False

MIN_WORDING_WHY = 15


def _has_number(s) -> bool:
    """★数を表す字を含むか★＝含むものは書き方の違いで通さない。

    ★★`isdigit()` では足りない★★（2026-09-16・Codexの指摘）
      ★直す前★＝`isdigit()` で見ていたので、漢数字とローマ数字が素通りした。
      実測＝千・百・万・一・二・三・〇・零・Ⅲ はどれも `isdigit()` が偽で
      `isnumeric()` が真。
      ＝「千ゲーム消化 → 百ゲーム消化」を★書き方の違いとして通せた★。
      これは表記ゆれではなく**別の数**なので、通してはいけない。
    ★ここで止められないもの（正直に）★＝かな書き・英単語の数詞
      （「せんゲーム」「one」）。★字の性質では判定できない★ので、
      そこは2AIの判断と、記録に残る理由が受け持つ。
      ★名簿（数詞の辞書）は作らない★＝意味の判断を機械にやらせる形になる。
    """
    return any(c.isnumeric() for c in str(s or ""))


def wording_problems(field: str, toks, sources) -> list:
    """★値が出典ごとに支えられているかを決める、唯一の場所★（台帳#691）

    ★★なぜ要るか★★（2026-09-16・運営者の指示）
      ＞ 2AIでは決まってるのにその後機械に渡すと弾かれてるみたいだけど
      ＞ これじゃ意味ないじゃん 修正して更新できるようにして
      2社が同じ事実を別の言葉で書いていると
      （なな徹「レア小役」／ちょんぼりすた「レア役」）、
      どちらを値にしてももう一方が落ち、
      ★独立2出典を永久に満たせなかった★（実例＝dmm_5086）。

    ★★この守り自体は正しいので外さない★★
      2026-08-09に入れたもので、引用が「1000pt」なのに値を「1000G」と
      書けた穴を塞いでいる。★開けるのは「数を含まない言葉」だけ★。

    ★機械が決めること★＝①控えた書き方がその引用に実在するか
      ②数が絡んでいないか ③値の字が、どれか1つの出典にそのまま在るか
      ④理由が書いてあるか。
    ★2AIが決めること★＝その2つが同じ意味かどうか（理由は機械が読まない）。

    ★★「数が絡む」の線は、値ぜんたいで見る★★（2026-09-16・自分で踏んだ）
      ★はじめ「その字が数を含むか」で見ていた★が、
      値は字ごとに分かれて照合される（天井なら 1000 と pt が別の字）ので、
      ★単位の字だけを pt → G と控えれば、数を含まないまま通った★
      ＝2026-08-09に塞いだ穴がそのまま開く。
      ★どれか1つの字が数を含めば、その値では書き方の違いを一切許さない★。

    ★書き込み側と読み込み側で同じ関数を通す★（同じ規則を2か所に書かない）。
    戻り値は問題の一覧（空なら合格）。
    """
    ng = []
    toks = [str(t) for t in (toks or [])]
    src = [s for s in (sources or []) if isinstance(s, dict)]
    # ★★数が絡む値は、書き方の違いを一切許さない★★（この判定は値ぜんたい）
    _measured = any(_has_number(t) for t in toks)
    for i, s in enumerate(src):
        who = str(s.get("publisher") or "") or f"出典{i + 1}"
        q = " ".join(str(s.get("quote") or "").split())
        wmap = s.get("wording")
        if wmap is None:
            wmap = {}
        if not isinstance(wmap, dict):
            ng.append(f"{field}: 出典{i + 1}の書き方の控えが組ではありません")
            wmap = {}
        # ★★控えは、使われるかどうかに関わらず全部確かめる★★
        #   （2026-09-16・Codexの指摘）
        #   ★直す前★＝値が引用にそのまま在ると、その字に付いた控えを
        #   ★一度も見ずに保存していた★。＝「控えは全部確かめてある」という
        #   約束になっていない。あとで値の書き方が変わった日に、
        #   確かめていない控えが黙って効き始める。
        _ok_wording = set()
        for k, v in wmap.items():
            alt = str(v or "").strip()
            if str(k) not in toks:
                # ★関係のない控えを残させない★
                ng.append(f"{field}: 出典{i + 1}の書き方の控え『{k}』は"
                          "この値の字ではありません")
                continue
            if not alt:
                ng.append(f"{field}: 出典{i + 1}の書き方の控え『{k}』が空です")
                continue
            if _measured or _has_number(alt):
                # ★ここが「単位や数の取り違え」と「表記ゆれ」の線★
                ng.append(f"{field}: 数が絡む値は書き方の違いで通しません"
                          f"（『{k}』↔『{alt}』・{who}）"
                          "／★単位や恩恵の取り違えを止めるため★")
                continue
            if alt == str(k):
                ng.append(f"{field}: 出典{i + 1}の書き方の控えが値と同じです")
                continue
            if not token_in_quote(alt, q):
                ng.append(f"{field}: {who} の引用に『{alt}』がありません"
                          "（★控えた書き方は、その引用に実在すること★）")
                continue
            if len(str(s.get("wording_why") or "").strip()) < MIN_WORDING_WHY:
                ng.append(f"{field}: {who} の書き方の違いに理由がありません"
                          f"（{MIN_WORDING_WHY}文字以上）")
                continue
            _ok_wording.add(str(k))        # ★全部通ったものだけ★
        # ★値が、この出典で支えられているか★
        for token in toks:
            if token_in_quote(token, q):
                continue
            if str(token) in _ok_wording:
                continue                   # 確かめた控えが支えている
            if str(token) in wmap:
                continue                   # 控えの問題は上で挙げている
            ng.append(f"{field}: 値『{token}』が {who} の引用にありません"
                      "（★出典ごとに同じ値を支えている必要があります★）")
    # ★★値の字は、どれか1つの出典がそのまま書いていること★★
    #   ★これが無いと★＝どこにも書かれていない言葉を値にして、
    #   出典を全部「書き方の違い」で埋められる＝★2AIが値を作れる★。
    for token in toks:
        if not any(str(token) in (s.get("wording") or {})
                   for s in src if isinstance(s.get("wording"), dict)):
            continue                       # 控えを使っていない値は上で見た
        if any(token_in_quote(token, " ".join(str(s.get("quote") or "").split()))
               for s in src):
            continue
        ng.append(f"{field}: 値『{token}』を、そのまま書いている出典が"
                  "1つもありません（★控えてよいのは書き方の違いだけ★）")
    return ng


# ★★出典1つでも確定してよい項目★★（2026-08-27・運営者の判断）
#   ★数値ではない分類だけ★＝「この機種はボーナスタイプか」のように、
#   見れば分かるのに1社しか明記していないことがある（実測で確認）。
#   数値と同じ「2出典」を課すと、★その機種は永久に検索に載せられない★。
#   ★機械割・確率などの数値は今までどおり2出典★（ここには足さない）。
#   ★緩めるのは出典の数だけ★＝公式URL・判断者2人・引用の実在確認は
#   今までどおり全部通す。
MIN_SOURCES = {"machine_profile": 1}
MIN_SOURCES_DEFAULT = 2


def min_sources(field: str) -> int:
    """その項目に要る独立系列の数（★既定は2★）"""
    return MIN_SOURCES.get(base_field(field), MIN_SOURCES_DEFAULT)


def check_sources(sources: list, field: str = "") -> list:
    """★独立した系列がそろっているか★（同じ発行者の2ページは1票）"""
    need = min_sources(field) if field else MIN_SOURCES_DEFAULT
    if len(sources) < need:
        raise ConfirmedError(f"出典が{need}つ要ります（独立した系列）")
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
    if len(keys) < need:
        raise ConfirmedError(
            f"独立した系列が{need}つ要ります（同じ発行者は1票）: "
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

# ★引用の前後を何文字みるか★（2026-09-08・台帳#585）
#   ★狭すぎると節の移動を見逃し、広すぎると全文の指紋と同じことになる★。
#   120字＝出典ページの1〜2文ぶん。実測で「設置店の宣伝文」までは届かない。
CTX_WINDOW = 120


def context_fingerprint(text: str, needle: str,
                        window: int = CTX_WINDOW) -> str:
    """★逐語の「すぐ周り」の指紋★（2026-09-08・台帳#585・運営者の承認）

    ★なぜ全文の指紋をやめたか★
      出典ページには設置店の一覧があり、店舗の宣伝文には日付が入る
      （実物に「7日!気合い入ります!」という文があった）。
      ＝記事と関係のない場所が毎日変わるので、全文の指紋は毎日食い違う。
      実測で11件中6件が、引用も同定の根拠も今もそのままなのに弾かれ、
      ★確かめた値が1日で使えなくなっていた★（新台が検索に載らない原因）。

    ★なぜ引用そのものだけでは足りないか★
      引用が残っていても、その前後が変われば意味が変わる
      （「AT間」の節にあった行が「CZ間」の節へ移る／
        「※旧スペック」の断りが足される）。＝**引用の周りを見る**。

    ★出現ごとに全部見る★＝同じ文が2か所にあるとき、
      片方だけ消えても・増えても指紋が変わる。
    ★見つからないときは空文字★（呼ぶ側が「引用が消えた」として扱う）。
    """
    import hashlib as _hl
    if not text or not needle:
        return ""
    out = []
    i = text.find(needle)
    while i >= 0:
        a = max(0, i - int(window))
        b = min(len(text), i + len(needle) + int(window))
        out.append(text[a:b])
        i = text.find(needle, i + 1)
    if not out:
        return ""
    # ★区切り文字は本文に出ない字★（連結の境目を偽装できないように）
    return _hl.sha256("\x1f".join(out).encode("utf-8")).hexdigest()


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
    # ★いま作ったものにだけ周りの指紋を足す★（2026-09-08・台帳#585）
    #   ★持ち越された控えに、今のページの指紋を書き足さない★＝
    #   それをやると「昔の判断」が「今のページ」で上書きされ、
    #   見張りが自己一致して永久に通る（罠㉕）。
    _built_override = False
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
        _built_override = True
    # ★同じ本文を使う★（2026-08-24・Codexの12回目＝ここが直っていなかった）
    text = text_of(html)
    quote = " ".join(str(src["quote"]).split())
    if quote not in text:
        raise ConfirmedError(
            f"引用がそのページに見当たりません（{src['url']}）: {quote[:40]}")
    if _built_override:
        # ★★引用と根拠の「周り」の指紋を控える★★（2026-09-08・台帳#585）
        #   ここが後の再確認の物差しになる。全文ではなくこちらを比べる。
        _ovp = " ".join(str(src["identity_override"].get("proof") or "").split())
        src["identity_override"]["context"] = {
            "window": CTX_WINDOW,
            "quote": context_fingerprint(text, quote),
            "proof": context_fingerprint(text, _ovp),
        }
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
    #   ★★探すのは pending_machines に任せる★★（2026-08-27・台帳#485）
    #   ★直す前は待ち行列の「鍵」を公式URLだと思って比べていた★＝
    #   待ち行列は採番したID（q_0001…）が鍵なので永久に一致せず、
    #   ★2AIが正しく答えても記録できない★状態が5晩続いていた
    #   （実測：8件一致したのに1件も保存できなかった）。
    #   ★形を知っている側に聞く★＝同じ規則を2か所に書かない。
    try:
        import pending_machines as _pm
        _hit = _pm.find_by_url(_pm.load(), official_url)
        if _hit:
            return slug, str(_hit.get("name") or "")
    except Exception:                      # noqa: BLE001
        pass                    # ★読めないときは②へ（最後は断るので安全側）★
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
    lineages = check_sources(sources, field)
    # ★出典ごとに、その値を支えていることを確かめる★（2026-08-09・依頼130 P1-1）
    #   以前は全出典の引用をつなげてから探していたので、
    #   **1つの出典にしか無い値でも「2出典一致」として通った**。
    # ★値の形を確かめ、引用と照合する表示値を決める★（依頼131 P0-3・P1）
    #   単位や恩恵まで照合しないと、引用が「1000pt」でも値を「1000G」にできた。
    toks = check_shape(field, value)
    # ★照合は1か所★（2026-09-16・台帳#691）＝読み込み側と同じ関数を通す。
    _wng = wording_problems(field, toks, sources)
    if _wng:
        raise ConfirmedError("／".join(_wng[:3]))
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
    # ★★読む側が断る記録は、書く側でも断る★★（2026-09-08）
    #   ★何が起きたか★＝同定の上書きが要らないページに --source-identity を
    #   渡すと、identity_why / identity_proof だけが出典に残り、
    #   identity_override が作られない。この形は validate_record が
    #   「本文の指紋がありません」で断る**契約違反**なのに、
    #   書き込みは素通りしていた。
    #   ★被害★＝契約の検査は load() の中で**控え全体**に対して走るので、
    #   1機種の1項目が壊れただけで★全機種の確定値が読めなくなる★。
    #   しかも --forget も load() を通るので**CLIから直せない**。
    #   夜の新台タスクは「控えが読めないなら作らない」（fail-closed）ので、
    #   ★その晩の新台追加が丸ごと静かに止まる★。
    #   2026-09-08に実際に2回踏んだ（pw_10523・pw_10521）。
    #   ★直し方は「保存しない」★＝読めない物を書かなければ、
    #   復旧の道が要らない。守りは1つも緩めていない。
    _bad = validate_record(field, rec)
    if _bad:
        raise ConfirmedError(
            "この記録は控えの契約を満たしません（保存しませんでした）: "
            + " ／ ".join(_bad[:5])
            + "／★同定の上書きが要らないページに --source-identity を"
              "渡すと、この形になります★")
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
             official_url: str = "", detail: bool = False):
    """★公開しようとしている機種の控えだけ、取り直して確かめる★

    ★★なぜ要るか★★（2026-08-24・Codexの8回目）
      控えの読み直しは、**保存されたURLと引用を信じて**いる。
      控えを手で書き換えられたら、偽の引用でも通ってしまう。
      ＝「書き込み口を厳しくしても、読み込み口が信じてしまう」型の残り。

    ★全件はやらない★＝いま書こうとしている機種だけ。
      出典は各1回だけ取りに行く（同じURLは1回）。

    ★戻り値は問題の一覧★（空なら合格）。
    ★`detail=True` なら `{"invalid": [...], "review": [...]}`★
      （2026-09-08・台帳#585）
      invalid … 引用や根拠がページから消えた＝**止める**
      review  … 引用も根拠もあるが、その周りが変わった＝**2AIが判断し直す**
      ★既定（detail=False）が返すのは invalid だけ★＝
      review で公開を止めない。止めると、出典ページの無関係な場所が
      動いただけで確定値が使えなくなる（それが台帳#585の中身）。
    """
    ng = []
    rv = []                                # ★2AIへ回すもの★

    def _done():
        return {"invalid": ng, "review": rv} if detail else ng

    rows = for_slug(slug)
    if not rows:
        return _done()                     # 確定値が無い機種は何もしない
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
        return _done()
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
                # ★保存したときと同じ作り方で本文を出す★
                #   （2026-08-24・Codexの13回目＝別々に作っていた）
                now_text = page_text(html, url)
                now_sha = _hl.sha256(now_text.encode("utf-8")).hexdigest()
                if not old.get("text_sha256"):
                    # ★指紋の箱ごと無いものは今までどおり断る★（fail-closed）
                    ng.append(f"{slug} / {field}: 2AIで通した出典に"
                              f"本文の指紋がありません（{url}）"
                              "／判断し直してください")
                else:
                    # ★★全文ではなく「引用と根拠の周り」を比べる★★
                    #   （2026-09-08・台帳#585・運営者の承認）
                    ctx = old.get("context") or {}
                    _q = " ".join(str((src or {}).get("quote") or "").split())
                    _p = " ".join(str(old.get("proof") or "").split())
                    _w = int(ctx.get("window") or CTX_WINDOW)
                    # ★★根拠が今もページに在るかを、ここでも必ず見る★★
                    #   （2026-09-08・Codexの指摘1・自分で再現して確かめた）
                    #   ★`verify_source` の根拠の検査は「機械が同定できな
                    #     かったとき」の枝の中にある★ので、相手のサイトが
                    #     題やh1に正式名を入れると（よくある改装）
                    #     ★その枝を通らず、根拠が消えていても気づかない★。
                    #   引用は残っているので、そこも通ってしまう。
                    #   ＝2AIが「この機種のページだ」と判断した前提が
                    #     消えているのに、公開が続く。
                    #   ★これは止める（2AIへ回すではない）★＝
                    #     判断の土台そのものが無くなっているため。
                    if not _p or _p not in now_text:
                        ng.append(
                            f"{slug} / {field}: 2AIが同じ機種だと判断した"
                            f"根拠が、そのページから消えています（{url}）"
                            "／判断し直してください")
                        continue
                    if ctx.get("quote") or ctx.get("proof"):
                        if (context_fingerprint(now_text, _q, _w)
                                != str(ctx.get("quote") or "")
                                or context_fingerprint(now_text, _p, _w)
                                != str(ctx.get("proof") or "")):
                            rv.append(
                                f"{slug} / {field}: 引用の周りが変わっています"
                                f"（{url}）／2AIで判断し直してください")
                    elif old["text_sha256"] != now_sha:
                        # ★周りの指紋を持たない古い記録★（移行の道）
                        #   ★全文が同じなら周りも同じ★なので通してよい。
                        #   違えば、どこが変わったか機械には分からない＝2AIへ。
                        rv.append(
                            f"{slug} / {field}: 出典の本文が変わっています"
                            f"（{url}・引用の周りの控えがまだありません）"
                            "／2AIで判断し直してください")
    return _done()

def forget(slug: str, field=None) -> dict:
    """★1件だけ取り除く★（2026-09-09・台帳#596）

    ★厳しい読みを通さない★＝壊れた記録があると、それを取り除く道具まで
    同じ例外で落ちて**直す手が無くなる**（実際に踏んだ）。
    ★危なくない理由★＝この関数は取り除くだけで、読んだ中身を
    どこにも渡さない。機械が material として使う側は今までどおり厳しい。
    """
    data = load(strict=False, require_exists=True)
    machines = data.get("machines")
    if not isinstance(machines, dict) or slug not in machines:
        return {"state": "NOT_FOUND"}
    # ★★機種の入れ物ごと壊れているときは、機種ごと取り除く★★
    #   （2026-09-09・Codexの指摘）
    #   ★直す前★＝辞書である前提だったので、
    #   文字列・空配列・null で壊れた機種は**取り除けなかった**
    #   （項目名が文字列の中にあると `str.pop()` で落ちさえした）。
    #   ＝控えが読めないまま、直す手が無い。
    # ★★機種ごと消すのは2つの場合だけ★★（2026-09-09・Codexの指摘）
    #   ①入れ物が壊れていて、項目を指せない
    #   ②項目を**書かなかった**（--field を渡していない）
    #   ★空文字を「書かなかった」と同じにしない★＝
    #   `--field ""` で、その機種の**正常な記録まで全部消えた**。
    if not isinstance(machines[slug], dict):
        machines.pop(slug, None)
        _save(data)
        return {"state": "FORGOTTEN", "whole": True}
    if field is None:
        machines.pop(slug, None)
        _save(data)
        return {"state": "FORGOTTEN", "whole": True}
    if not str(field).strip():
        return {"state": "NEED_FIELD",
                "why": "項目名が空です（機種ごと消すなら --field を書かない）"}
    fields = machines[slug]
    if field not in fields:
        return {"state": "NOT_FOUND"}
    fields.pop(field)
    if not fields:
        machines.pop(slug, None)
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


# ★★2AIの確定値は、全項目とも検索の濃さに数える★★
#   （2026-08-29・運営者の指示「全部やろう」）
#   ★直す前は ("bonus_prob",) の1項目だけだった★＝
#   2026-08-26に「ボーナス確率だけ載らない」を直したときの名残で、
#   そこだけ穴を開けた形になっていた。
#   実測（喰霊-零-Re・導入12日目）＝2AIで7項目を確定させ、
#   ★それぞれ独立2社の出典つき★なのに、検索の判定は0件だった。
#   ★確からしさは機械抽出と同等★＝記録の作法（独立2出典・判断者2名・
#   逐語引用の照合）は機械が強制しているので、根拠のない値は記録できない。
#   ★数えるかどうかは、項目ではなく「出典が何系列か」で決まる★
#   （`_independent_basis` が数え直す）。
INDEX_COUNTABLE_FIELDS = None       # None＝全項目


def _independent_basis(rec: dict) -> str:
    """★系列を数え直してから「独立2出典」を名乗る★（保存値を信じない）"""
    # ★check_sources とまったく同じ数え方★（票の鍵にしてから数える）
    #   ★URLをそのまま渡さない★＝independent() は**票の鍵**を受け取るので、
    #   URLを渡すと別々の文字というだけで2票に見えた（試験で判明）。
    keys = set()
    for s in (rec.get("sources") or []):
        try:
            keys.add(_sl.vote_key(s["publisher"]))
        except Exception:                                # noqa: BLE001
            return ""                                    # ★分からなければ名乗らない★
    try:
        n = _sl.independent(keys)
    except Exception:                                    # noqa: BLE001
        return ""
    return "INDEPENDENT_MULTI" if n >= 2 else ""


def _box_norm(x) -> str:
    """★箱の行を比べるときの値のそろえ方★（2026-09-25）

    表示側（`build_new_article._counted_norm`）は NFKC をかけ、空白の連なりを
    1つに詰めてから見出しを作るので、比べる側も同じ形にそろえないと
    「画面では同じ行なのに、比べると別物」になって重複が並ぶ。
    ★空白は消さずに詰めるだけ★（"A B" と "AB" は表示でも別の文字）。
    ★型もそろえる★（機械は 600、控えは "600" と持つ）。
    """
    if x is None or x is False:
        return ""
    s = unicodedata.normalize("NFKC", str(x))
    return " ".join(s.split())


# ★同じ天井かを決める鍵★（2026-09-25）＝`ceiling_lookup` の一致鍵のうち、
#   控えの行も持ちうる項目。role / phase は機械の行だけが持つ
#   （phase は counted と同じこと）。
#   ★恩恵は `ceiling_lookup.split_benefit` でそろえてから比べる★＝
#   「CZ」と「CZに当選」は同じ・「CZ」と「AT」、「当選」と「濃厚」は別。
_CEILING_BOX_KEY = ("kind", "amount", "unit", "mode", "after_event")
#   ★片方だけが書いていないなら同じ主張とみなす項目★
#   （`ceiling_lookup._merge_unqualified` と同じ考え方）。
_CEILING_BOX_DETAIL = ("counted", "count_note")


def _ceiling_benefit(d: dict) -> tuple:
    import ceiling_lookup as _cl      # noqa: E402（重い取り込みなので使う時だけ）
    ben, cert = _cl.split_benefit(str((d or {}).get("benefit") or ""))
    row_cert = _box_norm((d or {}).get("certainty"))
    if cert == "PLAIN" and row_cert:
        # ★行の確からしさも同じ物差しでそろえる★（「濃厚」＝LIKELY・「確定」＝GUARANTEED）
        _, c2 = _cl.split_benefit(row_cert)
        cert = c2 if c2 != "PLAIN" else row_cert.upper()
    return _box_norm(ben), cert


def _same_ceiling_box(a: dict, b: dict) -> bool:
    """★同じ天井か★（2026-09-25）

    鍵（`_CEILING_BOX_KEY`）と、そろえた恩恵・確からしさが同じで、
    数える区間と数え方の注記（`_CEILING_BOX_DETAIL`）がそれぞれ
    「同じ」か「片方だけが書いていない」とき。
    ★両方に書いてあって違えば別の天井★（AT間とCZ間・液晶G数と内部G数）。
    ★片方が書いていない場合の寄せ先が2つ以上あるときは寄せない★
    （呼ぶ側 `merge_into` が見る＝一意なときだけ）。
    """
    a, b = a or {}, b or {}
    if any(_box_norm(a.get(k)) != _box_norm(b.get(k)) for k in _CEILING_BOX_KEY):
        return False
    if _ceiling_benefit(a) != _ceiling_benefit(b):
        return False
    for k in _CEILING_BOX_DETAIL:
        na, nb = _box_norm(a.get(k)), _box_norm(b.get(k))
        if na and nb and na != nb:
            return False
    return True


def merge_into(material: dict, slug: str) -> list:
    """集めた材料に、2AIが確定した値を足す。★足したものの一覧を返す★

    ★機械が採れたものは、原則として上書きしない★（人の記録で塗り替えない）
    ★例外は1つだけ★（2026-09-24）＝機械の値が1社だけ（SINGLE_NEAR_RELEASE）で、
      確定値が独立2出典（INDEPENDENT_MULTI）のときは、確定値に置き換える。
      ★「機械が採れているなら独立2出典」という前提は、2026-09-20 に
      1社でも採る道を広げた日に崩れた★（1社の値が2社の確定値を押しのけていた）。
      ★箱（天井・AT・CZの行）にも同じ決まりを当てる★（2026-09-25）＝
      天井は構造の鍵（`_same_ceiling_box`）、それ以外の箱（AT・CZ・リセット・
      ゲーム性）は中身がそろえて同じとき。
      同じ事実の行が既にあれば控えは足さない（何度呼んでも増えない）。
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
        # ★★検索の濃さにも数える項目だけ、根拠を名乗る★★
        #   （2026-08-26・Codex35回目の穴2）
        #   ★直す前は `_from` しか付かず、`_skip_for_index` の白名簿
        #     （basis == INDEPENDENT_MULTI）に当たらなかった★＝
        #   第2の出典を見つけて正しく記録しても、
        #   **claim にならず `NO_BONUS_PROB` のまま検索へ載らない**。
        #   ★系列は数え直す★（保存された系列を信じない）。
        if (INDEX_COUNTABLE_FIELDS is None
                or base_field(field) in INDEX_COUNTABLE_FIELDS):
            _b = _independent_basis(rec)
            if _b:
                stamped["basis"] = _b
        if where == "adopted":
            adopted = material.setdefault("adopted", {})
            # ★★1社だけで採った機械の値は、2社で確定した値に道を譲る★★
            #   （2026-09-24）＝「機械が採れているなら独立2出典」の前提は、
            #   2026-09-20 に1社でも採る道（SINGLE_NEAR_RELEASE）を広げた日に
            #   崩れた。★強さが上のときだけ置き換える★（同じ強さなら今までどおり）。
            _mine = adopted.get(field)
            _weaker = (isinstance(_mine, dict)
                       and _mine.get("basis") == "SINGLE_NEAR_RELEASE"
                       and stamped.get("basis") == "INDEPENDENT_MULTI")
            if field in adopted and not _weaker:
                continue
            adopted[field] = stamped
        else:
            box = material.setdefault(where, {})
            rows = box.setdefault("adopted", [])
            # ★同じ中身が既にあるなら足さない★（機械が採れていれば上書きしない）
            #   出所・出典URL・根拠の名乗り（basis / *_basis）は比べない
            #   （機械が採った行と形が違うだけで「別物」と見なして重複して
            #    増えていた・依頼131 P1／2026-09-25に basis も外した）。
            #   ★値は表示側と同じようにそろえてから比べる★（全角・空白・型）
            #   ＝600 と "600"、"ＣＺ間" と "CZ間" は画面では同じ行になる。
            #   ★空の値は無いものとして比べる★（CZ抽出器は games_disputed=False
            #   などを必ず付けるが、控えは持たない＝有無の違いだけで別物に見えた）。
            def _core(d):
                out = {}
                for k, v in (d or {}).items():
                    if (k.startswith("_") or k in ("sources", "basis")
                            or k.endswith("_basis")):
                        continue
                    nv = _box_norm(v)
                    if nv:
                        out[k] = nv
                return out
            _v = rec["value"]
            if not isinstance(_v, dict):
                if any(_core(r) == _core(_v) for r in rows):
                    continue
            elif any(isinstance(r, dict) and r.get("_from") == "confirmed_values"
                     and r.get("_field") == field for r in rows):
                # ★同じ控えがもう入っているなら足さない★（2026-09-25・Codexの5回目）
                #   ＝曖昧で寄せなかった場合でも、2回目以降の呼び出しで増えない。
                continue
            else:
                # ★★同じ事実の行が既にあるとき（2026-09-25・更新タスクの自己修正）★★
                #   ★直す前★＝「中身が完全に同じ」ときだけ重ねずに済ませていた。
                #   機械の天井の行は恩恵を「CZ」、確定値は「CZに当選」と書くので
                #   別物に見え、★同じ CZ間600G が2行並んだ★。見出しの区別
                #   （AT間／CZ間）が重複で外れ、前に載っていた文を再現できずに
                #   育成が毎朝止まった（実例＝pw_10503）。
                #   ★天井は `_same_ceiling_box` で見る★＝種類・G数・単位・モード・
                #   発生条件と、`split_benefit` でそろえた恩恵・確からしさが同じ
                #   （「CZ」と「CZに当選」は同じ・「CZ」と「AT」は別）。
                #   数える区間・注記は片方だけ空なら同じ、両方にあって違えば別
                #   （ceiling_lookup と同じ考え）。
                #   ★AT・CZは中身がそろえて同じときだけ★＝名前の書き方の違いを
                #   同じとみなすのは意味の判断なので当てない。
                #   ★強さで決める★＝既にある行が全部「1社だけの機械の行」で、
                #   控えが独立2出典なら置き換える。それ以外（機械が2社で採った・
                #   既に控えの行がある・控えも1系列）なら控えを足さない
                #   ＝何度呼んでも行は増えない。
                #   ★機種ごとの恩恵の別名表（benefit_aliases）は通さない★＝
                #   登録は1組だけで、片方の表記は取得を止めたP-WORLD由来。
                #   外れたときは控えが足され、見出しが重なって育成の関所が止める
                #   （誤った内容は出ない側に倒れる）。
                if where == "ceilings":
                    def _same(r):
                        return isinstance(r, dict) and _same_ceiling_box(r, _v)
                else:
                    def _same(r, _c=_core(_v)):
                        return isinstance(r, dict) and _core(r) == _c
                _hits = [r for r in rows if _same(r)]
                # ★寄せ先が一意でないなら寄せない★（2026-09-25・Codexの3回目）
                #   注記なしの控えが「液晶G数」「内部G数」の2行に同時に当たると、
                #   別々の天井を1行に潰してしまう。数える区間・注記の組が
                #   2通り以上に分かれるときは、同じ事実とみなさない。
                #   ★寄せてよいのは、ほかの全部の行の条件を含む「1行」があるときだけ★
                #   （「注記なし」と「液晶G数」の組は一意に寄せられる／
                #    「CZ間だけ」と「液晶G数だけ」の2行を合成して、どちらにも無い
                #    「CZ間かつ液晶G数」を作らない＝Codexの6回目）。
                _cover = None
                if where == "ceilings" and _hits:
                    def _det(r):
                        return {k: _box_norm(r.get(k)) for k in _CEILING_BOX_DETAIL}
                    for _c in _hits:
                        _dc = _det(_c)
                        if all(all(not x or _dc[k] == x for k, x in _det(h).items())
                               for h in _hits):
                            _cover = _c
                            break
                    if _cover is None:
                        _hits = []
                #   ★天井・AT・CZ・リセット・ゲーム性の箱すべてに当てる★
                #   （同じ事実の行を2つ並べてよい箱は無い）。
                #   ★既にある行に強いものが混ざっていれば、弱い重複だけ外して
                #   控えは足さない★（★材料は毎回取り直して作り、merge_into は
                #   1回しか呼ばれないので、過去の実行の重複が持ち越されることは無い★。
                #   同じ控えが既に入っている場合は上の分岐で足さずに終わる）。
                if _hits:
                    _weak = [r for r in _hits
                             if r.get("_from") != "confirmed_values"
                             and r.get("basis") == "SINGLE_NEAR_RELEASE"]
                    _strong_here = len(_weak) < len(_hits)
                    # ★★控えより詳しい条件（区間・注記）を持つ行があるなら置き換えない★★
                    #   （2026-09-25・Codexの5・6回目）＝置き換えると条件が消える。
                    #   ★控えの行に条件を書き足すこともしない★＝公開の関所
                    #   （build_new_article.require_basis）は「控えと完全に一致」を
                    #   求めるので、書き足した行は根拠の無い値として止まる（実際に止まった）。
                    #   ★その詳しい1行だけを残し、ほかの弱い重複を外して、控えは足さない★
                    #   （詳しい側を残す＝ceiling_lookup._merge_unqualified と同じ）。
                    #   ★+α は比べも引き継ぎもしない★＝判定書・記事・育成のどこも読まない。
                    _richer = (where == "ceilings" and _cover is not None
                               and any(_box_norm(_cover.get(k))
                                       and not _box_norm(_v.get(k))
                                       for k in _CEILING_BOX_DETAIL))
                    if _richer:
                        rows[:] = [r for r in rows
                                   if r is _cover or not any(r is w for w in _weak)]
                        continue
                    if _strong_here or stamped.get("basis") == "INDEPENDENT_MULTI":
                        rows[:] = [r for r in rows
                                   if not any(r is w for w in _weak)]
                    if _strong_here or stamped.get("basis") != "INDEPENDENT_MULTI":
                        continue
            row = dict(rec["value"]) if isinstance(rec["value"], dict) else {
                "value": rec["value"]}
            row["_from"] = "confirmed_values"
            row["_field"] = field          # ★どの項目の控えか★（上と同じ理由）
            row["sources"] = stamped["sources"]
            # ★★天井・AT・CZにも根拠を名乗る★★（2026-08-29・運営者の指示）
            #   ★直す前は基本スペック側にしか付けていなかった★ので、
            #   天井やCZを2AIで確定させても検索の濃さに数えられなかった
            #   （実測：喰霊-零-Re は7項目のうち2項目しか数えられなかった）。
            if "basis" in stamped:
                row["basis"] = stamped["basis"]
            rows.append(row)
        added.append(field)
    return added


# ---------------------------------------------------------------- selftest

def _proof_gone_stops(slug, name, quote, src_ov) -> bool:
    """★同定の根拠が消えたら止まるか★（2026-09-08・Codexの指摘1）

    ★筋書き（実際に起こりうる）★
      記録時 … 出典ページの題が機種名でないので機械同定に失敗し、
               2AIが根拠の逐語と理由を付けて通した。
      再確認 … 相手のサイトが題とh1に正式名を入れた。
               機械同定が成功するので `verify_source` の根拠の検査を
               ★丸ごと通らない★。同時に根拠の文だけ消えている。
               引用は残っているので、そこも通る。
    ★期待★ invalid（止める）／review には出さない
    """
    far = "あ" * 400
    old_html = ("<html><head><title>解析まとめ</title></head><body>"
                f"<p>{name} の解析です。</p><p>{far}</p>"
                f"<p>{quote}</p></body></html>")
    # ★題とh1に正式名が入り、根拠の文だけ消えたページ★
    new_html = (f"<html><head><title>{name}</title></head><body><h1>{name}</h1>"
                f"<p>{far}</p><p>{quote}</p></body></html>")
    other = (f"<html><head><title>{name}</title></head><body><h1>{name}</h1>"
             f"<p>{quote}</p></body></html>")
    ov = verify_source(dict(src_ov), name, lambda u: old_html)
    json.dump({"schema_version": SCHEMA, "machines": {slug: {
        "ceiling": {
            "value": {"kind": "GAME", "amount": "999", "unit": "G",
                      "benefit": "AT当選"},
            "sources": [ov, {"url": "https://nana-press.com/kaiseki/x",
                             "quote": quote}],
            "lineages": ["vote:chonborista", "vote:nana-press"],
            "agreed_by": ["claude", "codex"],
            "why": "2AIで突き合わせました",
            "decided_at": "2026-08-24",
            "official_url": ""}}}},
        open(STORE, "w", encoding="utf-8"), ensure_ascii=False)
    got = reverify(slug, name=name, detail=True,
                   fetch=lambda u: (new_html if "chonbo" in u else other))
    return (got["review"] == []
            and [x for x in got["invalid"] if "根拠が、そのページから消えて" in x]
            != [])


def _fp_missing_review(slug, name, quote, src_ov) -> bool:
    """★周りの指紋を持たない古い記録は、全文で見て2AIへ回す★（台帳#585）

    ★なぜ関数に分けたか★＝控えを書き換えるので、本体の流れの中に置くと
      あとの試験がその書き換えを引き継ぐ（罠⑱＝共有の材料を置き換える）。
    ★これが移行の道★＝この直しより前に作られた控えには
      「引用の周りの指紋」が無い。全文が同じなら通し、違えば2AIへ回す。
      ★黙って通さない★（fail-open にしない）。
    """
    far = "あ" * 400

    def page(body, tail=""):
        return ("<html><head><title>解析まとめ</title></head><body>"
                f"<p>{name} の解析です。</p>"
                f"<p>{far}</p><p>{body}</p><p>{far}</p>"
                f"<p>{tail}</p></body></html>")

    ov = verify_source(dict(src_ov), name, lambda u: page(quote))
    # ★周りの指紋だけ落とす★（＝この直しより前に作られた控えの姿）
    ov["identity_override"].pop("context", None)
    json.dump({"schema_version": SCHEMA, "machines": {slug: {
        "ceiling": {
            "value": {"kind": "GAME", "amount": "999", "unit": "G",
                      "benefit": "AT当選"},
            "sources": [ov, {"url": "https://nana-press.com/kaiseki/x",
                             "quote": quote}],
            "lineages": ["vote:chonborista", "vote:nana-press"],
            "agreed_by": ["claude", "codex"],
            "why": "2AIで突き合わせました",
            "decided_at": "2026-08-24",
            "official_url": ""}}}},
        open(STORE, "w", encoding="utf-8"), ensure_ascii=False)

    def other(q):
        # ★もう1つの出典は、機械が同定できる普通のページ★
        #   （ここが同定に落ちると、狙った検査ではなく隣が先に断る＝罠④）
        return ("<html><head><title>" + name + "</title></head><body><h1>"
                + name + f"</h1><p>{q}</p></body></html>")

    def rv(tail=""):
        return reverify(slug, detail=True,
                        fetch=lambda u: (page(quote, tail) if "chonbo" in u
                                         else other(quote)))
    same = rv()
    moved = rv("新着情報を更新しました。")
    return (same["invalid"] == [] and same["review"] == []
            and moved["invalid"] == []
            and [x for x in moved["review"] if "本文が変わって" in x] != [])


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
        # ─── ★★2AIが決めた書き方の違いを受け取る★★（2026-09-16・台帳#691）
        #   ★運営者の指示★＝「2AIでは決まってるのにその後機械に渡すと
        #   弾かれてるみたいだけどこれじゃ意味ないじゃん 修正して更新できるように」
        #   ★実例★＝なな徹「レア小役」／ちょんぼりすた「レア役」。
        #   どちらを値にしてももう一方が落ち、独立2出典を満たせなかった。
        _QA = "通常時は基本的にレア役や規定G数消化からCZを目指す流れです"
        _QB = "通常時はレア小役や規定ゲーム数到達からCZを経由して当選します"

        def _fetch_w(url):
            q = _QA if "chonborista" in url else _QB
            return ("<title>" + NAME + " スロット 新台 天井 | 解析</title>"
                    "<body><h1>" + NAME + "</h1><p>" + q + "。"
                    + ("説明。" * 30) + "</p></body>")

        def _src_w(wording=None, why="レア小役はレア役の書き方の違いです"):
            a = parse_source("https://chonborista.com/1|" + _QA)
            b = parse_source("https://nana-press.com/1|" + _QB)
            if wording is not None:
                b["wording"] = wording
                b["wording_why"] = why
            return [a, b]

        def _rec_w(**kw):
            base = dict(field="gameplay#normal_cz",
                        value={"trigger": "レア役", "leads_to": "CZ"},
                        fetch=_fetch_w, sources=_src_w({"レア役": "レア小役"}))
            base.update(kw)
            return rec(**base)
        t("★★2AIが「同じ意味だ」と決めれば、書き方が違っても記録できる★★"
          "（★直す前は、どちらを値にしてももう一方が落ちて"
          "独立2出典を永久に満たせなかった★）",
          bool(_rec_w()))
        stops("　（対照）控えが無ければ、今までどおり断る",
              lambda: _rec_w(sources=_src_w()))
        stops("　（対照）控えた書き方が、その引用に無ければ断る",
              lambda: _rec_w(sources=_src_w({"レア役": "どこにも無い言葉"})))
        stops("　（対照）なぜ同じ意味かを書かなければ断る",
              lambda: _rec_w(sources=_src_w({"レア役": "レア小役"}, why="短い")))
        # ★★数が絡む値は、書き方の違いを一切許さない★★
        #   ★ここは「その字が数を含むか」では守れない★＝値は字ごとに
        #   照合されるので、天井は 1000 と pt に分かれ、
        #   ★単位の字だけを pt → G と控えれば数を含まないまま通る★。
        #   ＝2026-08-09に塞いだ「引用が1000ptなのに値を1000G」がそのまま開く。
        #   ★対照の作り★＝1つ目の出典は 1000pt をそのまま書いているので
        #   隣の守り（引用に無い／どこにも無い字）では止まらない。
        _QP = "この機種の天井は1000ptで、到達するとATに当選します"
        _QG = "この機種の天井は1000Gで、到達するとATに当選します"

        def _fetch_u(url):
            q = _QP if "chonborista" in url else _QG
            return ("<title>" + NAME + " スロット 新台 天井 | 解析</title>"
                    "<body><h1>" + NAME + "</h1><p>" + q + "。"
                    + ("説明。" * 30) + "</p></body>")
        stops("★★数が絡む値は、書き方の違いで通さない★★"
              "（★引用が1000ptなのに値を1000Gと書けた穴を残さない／"
              "単位の字だけを控えれば「数を含まない」ので、"
              "字ごとに見ていては守れない★）",
              lambda: rec(value={"kind": "GAME", "amount": "1000",
                                 "unit": "pt", "benefit": "AT"},
                          fetch=_fetch_u,
                          sources=[
                              parse_source("https://chonborista.com/1|" + _QP),
                              dict(parse_source(
                                  "https://nana-press.com/1|" + _QG),
                                  # ★理由は十分に長くする★（罠④＝短いと
                                  #   理由の検査のほうが先に断り、
                                  #   数の線を壊しても試験が赤くならない）
                                  wording={"pt": "G"},
                                  wording_why="どちらも同じ単位の書き方の"
                                              "違いだと判断しました（嘘）")]))
        # ★★漢数字・ローマ数字も「数」★★（2026-09-16・Codexの指摘）
        #   ★直す前は isdigit() で見ていた★ので、千・百・一・Ⅲ・〇 が
        #   どれも偽になり、★「千ゲーム消化 → 百ゲーム消化」を
        #   書き方の違いとして通せた★＝表記ゆれではなく別の数。
        _QK = "通常時は千ゲーム消化からCZに当選する仕組みです"
        _QH = "通常時は百ゲーム消化からCZに当選する仕組みです"

        def _fetch_k(url):
            q = _QK if "chonborista" in url else _QH
            return ("<title>" + NAME + " スロット 新台 天井 | 解析</title>"
                    "<body><h1>" + NAME + "</h1><p>" + q + "。"
                    + ("説明。" * 30) + "</p></body>")
        stops("★★漢数字も『数』として断る★★"
              "（★isdigit() では偽になるので、千 → 百 が"
              "書き方の違いとして通っていた★）",
              lambda: rec(
                  field="gameplay#normal_cz",
                  value={"trigger": "千ゲーム消化", "leads_to": "CZ"},
                  fetch=_fetch_k,
                  sources=[
                      parse_source("https://chonborista.com/1|" + _QK),
                      dict(parse_source("https://nana-press.com/1|" + _QH),
                           wording={"千ゲーム消化": "百ゲーム消化"},
                           wording_why="どちらも同じ消化数の書き方の違いだと"
                                       "判断しました（嘘）")]))
        # ★★控えは、使われるかどうかに関わらず全部確かめる★★（同・Codexの指摘）
        #   ★直す前★＝値が引用にそのまま在ると、その字に付いた控えを
        #   一度も見ずに保存していた。
        stops("★★使われていない控えも確かめる★★"
              "（★値が引用にそのまま在ると、その字の控えを一度も見ずに"
              "保存していた＝「控えは全部確かめてある」という約束が崩れる★）",
              lambda: _rec_w(sources=[
                  parse_source("https://chonborista.com/1|" + _QA),
                  dict(parse_source("https://nana-press.com/1|" + _QB),
                       wording={"レア役": "レア小役",
                                "CZ": "どこにも無い言葉"},
                       wording_why="レア小役はレア役の書き方の違いです")]))
        stops("★★どの出典もそのまま書いていない言葉は、値にできない★★"
              "（★全部を控えで埋めると、2AIが値そのものを作れる★）",
              lambda: _rec_w(
                  value={"trigger": "まったく別の言葉", "leads_to": "CZ"},
                  sources=[
                      dict(parse_source("https://chonborista.com/1|" + _QA),
                           wording={"まったく別の言葉": "レア役"},
                           wording_why="同じ意味だと判断しました（嘘）"),
                      dict(parse_source("https://nana-press.com/1|" + _QB),
                           wording={"まったく別の言葉": "レア小役"},
                           wording_why="同じ意味だと判断しました（嘘）")]))
        t("★★読み込み側も同じ照合をする★★"
          "（★控えのファイルを手で書き換えても、書き方の控えが効かない★）",
          any("引用に『" in x for x in validate_record(
              "gameplay#normal_cz",
              {"value": {"trigger": "レア役", "leads_to": "CZ"},
               "sources": [{"publisher": "chonborista",
                            "url": "https://chonborista.com/1",
                            "quote": _QA},
                           {"publisher": "nana-press",
                            "url": "https://nana-press.com/1",
                            "quote": _QB,
                            "wording": {"レア役": "どこにも無い言葉"},
                            "wording_why": "同じ意味だと判断しました（嘘）"}],
               "lineages": ["chonborista", "nana-press"],
               "agreed_by": ["claude", "codex"],
               "why": "同じ原文を読んで一致しました",
               "decided_at": "2026-09-16",
               "official_url": "https://m.example/products/slot/x/"})))
        # ★あとの試験の材料を増やさない★（罠⑱＝共有の材料は元へ戻す）
        forget("x", "gameplay#normal_cz")

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

        # ★★壊れた控えを、道具で直せること★★（2026-09-09・台帳#596）
        #   ★実際に踏んだ★＝1機種の1項目が契約違反で保存されると、
        #   控え全体が読めなくなり、★取り除く道具まで同じ例外で落ちた★。
        #   ＝人が手で直すまで、夜の新台追加が丸ごと止まる。
        #   ★偽物で確かめない★＝本物の `forget()` を、本物の置き場に対して動かす。
        import tempfile as _tf596
        _keep596 = STORE
        globals()["STORE"] = os.path.join(
            _tf596.mkdtemp(prefix="uchi_cv596_"), "confirmed_values.json")
        try:
            _save({"schema_version": SCHEMA,
                   "machines": {"zzz_ng": {"ceiling_state": {"value": None}}}})
            _blew = False
            try:
                load()
            except ConfirmedError:
                _blew = True
            t("　（前提）契約を満たさない記録があると、機械は控えを読まない", _blew)
            # ★例外で落ちるのを「止まった」と数えない★（罠⑤）
            #   守りを壊すと forget() が例外を投げるので、
            #   ここで受けて**試験の❌として出す**。
            try:
                _r596 = forget("zzz_ng", "ceiling_state")
            except ConfirmedError as _e596:
                _r596 = {"state": "直せません: " + str(_e596)[:40]}
            t("★★壊れた記録は、取り除く道具で直せる★★"
              "（★直せないと、人が手で直すまで新台追加が丸ごと止まる★）",
              _r596.get("state") == "FORGOTTEN")
            try:
                _now = load()      # ★取り除いたあとは、機械が読める★
            except ConfirmedError:
                _now = {"machines": {"まだ壊れています": {}}}
            t("　取り除いたあとは、機械が読めるようになる",
              (_now.get("machines") or {}) == {})
            # ★★機種の入れ物ごと壊れていても取り除ける★★
            #   （2026-09-09・Codexの指摘。★重大★）
            #   ★直す前★＝辞書である前提だったので、文字列・空配列・null で
            #   壊れた機種は取り除けず、控えが読めないまま直す手が無かった。
            for _shape596 in ("これは辞書ではありません", [], None, ""):
                _save({"schema_version": SCHEMA,
                       "machines": {"zzz_x": _shape596,
                                    "zzz_y": {"ceiling_state": {"value": 1}}}})
                try:
                    _fw = forget("zzz_x", "")
                except Exception as _efw:                    # noqa: BLE001
                    _fw = {"state": "落ちました: " + type(_efw).__name__}
                _after596 = (load(strict=False).get("machines") or {})
                t(f"　機種ごと取り除ける（{type(_shape596).__name__}）",
                  _fw.get("state") == "FORGOTTEN"
                  and "zzz_x" not in _after596
                  # ★★巻き添えにしない★★（2026-09-09・Codexの指摘）
                  #   ★全部消しても「消えた」だけなら合格していた★
                  and _after596.get("zzz_y") == {"ceiling_state":
                                                 {"value": 1}})

            # ★★どんな壊れ方でも、一覧は最後まで動く★★
            #   （2026-09-09・Codexの指摘）
            #   ★直す前★＝契約違反として名指しはするのに、そのあと
            #   通常表示で中身を読んでいたので、記録が文字列なら
            #   TypeError、value が無ければ KeyError で**落ちた**。
            #   ＝壊れたときに使う道具が、壊れていると使えない。
            #   ★本物の入口（main）を通す★＝表示の道を実際に歩かせる。
            import contextlib as _cl596
            import io as _io596
            _save({"schema_version": SCHEMA, "machines": {
                "zzz_a": "これは辞書ではありません",
                "zzz_b": {"ceiling_state": "文字列の記録"},
                "zzz_c": {"ceiling_state": {"why": "value がありません"}},
                "zzz_d": {"ceiling_state": {"value": {"state": "NONE"},
                                            "sources": ["壊れた出典"],
                                            "agreed_by": "配列ではない"}}}})
            _buf596 = _io596.StringIO()
            _rc596 = 1
            _argv596 = sys.argv
            sys.argv = ["confirmed_values.py", "--list"]
            try:
                with _cl596.redirect_stdout(_buf596):
                    _rc596 = main()
            except Exception as _e596b:                      # noqa: BLE001
                _rc596 = "落ちました: " + type(_e596b).__name__
            finally:
                sys.argv = _argv596
            _out596 = _buf596.getvalue()
            t("★★どんな壊れ方でも、一覧は最後まで動く★★"
              "（★壊れたときに使う道具が、壊れていると使えなかった★）",
              _rc596 == 0)
            t("　壊れている行は名指しで出す（どれを取り除けばよいか分かる）",
              "zzz_b" in _out596 and "zzz_c" in _out596
              and "壊れています" in _out596)

            # ★★必須の鍵がそろった「中身だけ壊れている」記録★★
            #   （2026-09-09・Codexの指摘）
            #   ★直す前★＝鍵がそろっていると検査が先へ進み、
            #   出典が文字列・同定の上書きが文字列・URLが壊れている形で
            #   ★検査そのものが例外で落ちた★。
            def _full596(**over):
                r = {"value": {"state": "NONE"},
                     "sources": [{"url": "https://chonborista.com/slot/x",
                                  "quote": "引用"}],
                     "lineages": ["vote:chonborista"],
                     "agreed_by": ["claude", "codex"],
                     "why": "2AIで突き合わせました",
                     "decided_at": "2026-09-09",
                     "official_url": "https://p-town.dmm.com/machines/1"}
                r.update(over)
                return r

            for _nm596, _rec596 in (
                    ("出典が文字列", _full596(sources=["これは文字列"])),
                    ("同定の上書きが文字列",
                     _full596(sources=[{"url": "https://chonborista.com/slot/x",
                                        "quote": "引用",
                                        "identity_override": "文字列"}])),
                    ("URLが壊れている",
                     _full596(sources=[{"url": "http://[", "quote": "引用"}])),
            ):
                # ★例外で落ちるのを「止まった」と数えない★（罠⑤）
                try:
                    _vr596 = validate_record("ceiling_state", _rec596)
                except Exception as _ev596:                  # noqa: BLE001
                    _vr596 = "落ちました: " + type(_ev596).__name__
                # ★空の一覧でも合格していた★（2026-09-09・Codexの指摘）
                #   ＝壊れた記録を正常扱いする退行を捕まえられない。
                t(f"　検査は落ちずに『問題あり』と答える: {_nm596}",
                  isinstance(_vr596, list) and bool(_vr596))
                _save({"schema_version": SCHEMA,
                       "machines": {"zzz_e": {"ceiling_state": _rec596}}})
                _b2 = _io596.StringIO()
                _argv2 = sys.argv
                sys.argv = ["confirmed_values.py", "--list"]
                try:
                    with _cl596.redirect_stdout(_b2):
                        _rc2 = main()
                except Exception as _e2:                     # noqa: BLE001
                    _rc2 = "落ちました: " + type(_e2).__name__
                finally:
                    sys.argv = _argv2
                t(f"　一覧も最後まで動く: {_nm596}", _rc2 == 0)
                t(f"　一覧に異常として出る: {_nm596}",
                  "契約を満たしていない記録があります" in _b2.getvalue())

            # ★★控えのファイルそのものが壊れている★★
            #   ★直す前は traceback で落ちた★＝何が起きたか伝わらない。
            open(STORE, "w", encoding="utf-8").write("{壊れたJSON")
            _b3 = _io596.StringIO()
            _argv3 = sys.argv
            sys.argv = ["confirmed_values.py", "--list"]
            try:
                with _cl596.redirect_stdout(_b3):
                    _rc3 = main()
            except Exception as _e3:                         # noqa: BLE001
                _rc3 = "落ちました: " + type(_e3).__name__
            finally:
                sys.argv = _argv3
            # ★★案内どおりに実行したら直る★★（2026-09-09・Codexの指摘）
            #   ★直す前は一律に項目つきの命令を出していた★ので、
            #   機種の入れ物ごと壊れている形では★その命令が効かなかった★。
            #   ★表示された命令をそのまま動かして確かめる★
            def _run_argv596(argv):
                _b = _io596.StringIO()
                _av = sys.argv
                sys.argv = ["confirmed_values.py"] + list(argv)
                try:
                    with _cl596.redirect_stdout(_b):
                        _rc = main()
                except Exception as _e:                      # noqa: BLE001
                    _rc = "落ちました: " + type(_e).__name__
                finally:
                    sys.argv = _av
                return _rc, _b.getvalue()

            # ★同居させる記録は「契約を満たしたもの」にする★
            #   （2026-09-09・Codexの指摘）★直す前はこれも壊れていた★ので、
            #   命令を実行しても機械は控えを読めないままで、
            #   ★「直った」を確かめずに合格していた（偽の合格）★。
            _ok596 = {"value": {"state": "NONE"},
                      "sources": [{"url": "https://chonborista.com/slot/x",
                                   "publisher": "chonborista",
                                   "quote": "天井なし"},
                                  {"url": "https://nana-press.com/kaiseki/x",
                                   "publisher": "nana-press",
                                   "quote": "天井なし"}],
                      "lineages": ["vote:chonborista", "vote:nana-press"],
                      "agreed_by": ["claude", "codex"],
                      "why": "2AIで突き合わせました",
                      "decided_at": "2026-09-09",
                      "official_url": "https://p-town.dmm.com/machines/1"}
            for _nm3, _shape3 in (("機種ごと壊れている", "文字列です"),
                                  ("1件だけ壊れている",
                                   {"ceiling_state": {"value": None}})):
                _save({"schema_version": SCHEMA,
                       "machines": {"zzz_p": _shape3,
                                    "zzz_q": {"ceiling_state": _ok596}}})
                _rc6, _out6 = _run_argv596(["--list"])
                # ★壊した機種ぶんの案内だけを見る★
                #   （同居させた機種も契約を満たしていないので、案内は複数出る）
                _cmds = [ln.strip() for ln in _out6.splitlines()
                         if ln.strip().startswith("python scripts/")
                         and "zzz_p" in ln]
                # ★壊れ方に合った形か★＝機種ごとなら --field を付けない
                _want_field = isinstance(_shape3, dict)
                t(f"　壊れ方に合った直し方が出る: {_nm3}",
                  _rc6 == 0 and len(_cmds) == 1
                  and (("--field" in _cmds[0]) is _want_field))
                # ★出た命令を、そのまま動かす★
                _argv6 = _cmds[0].split()[2:] if _cmds else []
                _rc7, _ = _run_argv596(_argv6)
                # ★★「直った」＝機械が厳しい読みで読めること★★
                #   （2026-09-09・Codexの指摘）★消えたことだけ見ると、
                #   読めないままでも合格する（偽の合格）★。
                try:
                    _strict596 = load()
                except ConfirmedError as _es596:
                    _strict596 = {"読めません": str(_es596)[:60]}
                _left = (_strict596.get("machines") or {})
                t(f"★★案内どおりに実行したら直る: {_nm3}★★"
                  "（★機械が控えを読めるようになるところまで見る★）",
                  _rc7 == 0 and "読めません" not in _strict596
                  and "zzz_p" not in _left
                  and _left.get("zzz_q") == {"ceiling_state": _ok596})

            # ★★空の項目名で、正常な記録まで消さない★★
            #   （2026-09-09・Codexの指摘）★人のデータを壊しうる★
            _save({"schema_version": SCHEMA,
                   "machines": {"zzz_q": {"ceiling_state": _ok596}}})
            _rc9 = forget("zzz_q", "")
            t("★★項目名が空のときは、機種ごと消さずに断る★★"
              "（★正常な記録まで失われる★）",
              _rc9.get("state") == "NEED_FIELD"
              and (load(strict=False).get("machines") or {}).get("zzz_q"))
            t("　（対照）項目名を書かなければ、機種ごと消せる",
              forget("zzz_q").get("state") == "FORGOTTEN"
              and "zzz_q" not in (load(strict=False).get("machines") or {}))

            # ★★控えが消えているときも、直し方を伝える★★
            os.remove(STORE)
            _rc8, _out8 = _run_argv596(["--list"])
            t("★★控えが消えていても、直し方を伝える★★"
              "（★案内が無いと、そこで手詰まりになる★）",
              _rc8 == 1 and "バックアップから戻して" in _out8
              and "--init は初回" in _out8)

            # ★★案内は「できること」だけ★★（2026-09-09・Codexの指摘）
            #   ★直す前は --forget を勧めていた★が、それも同じところで
            #   止まるので実行できない。★文言まで見ないと元に戻せる★。
            def _run_list596():
                _b = _io596.StringIO()
                _av = sys.argv
                sys.argv = ["confirmed_values.py", "--list"]
                try:
                    with _cl596.redirect_stdout(_b):
                        _rc = main()
                except Exception as _e:                      # noqa: BLE001
                    _rc = "落ちました: " + type(_e).__name__
                finally:
                    sys.argv = _av
                return _rc, _b.getvalue()

            # ★外側が壊れている形（JSONとしては読める）★
            for _nm2, _obj2 in (
                    ("機種の並びが null", '{"schema_version": "%s", '
                     '"machines": null}' % SCHEMA),
                    ("版番号が違う", '{"schema_version": "別物", '
                     '"machines": {}}'),
            ):
                open(STORE, "w", encoding="utf-8").write(_obj2)
                _rc4, _out4 = _run_list596()
                t(f"　外側が壊れていても、直し方まで伝える: {_nm2}",
                  _rc4 == 1 and "バックアップから戻す" in _out4
                  and "--forget も動きません" in _out4)

            # ★渡された値のファイルの壊れは、控えの壊れと区別する★
            _save({"schema_version": SCHEMA, "machines": {}})
            _vf596 = os.path.join(os.path.dirname(STORE), "壊れた値.json")
            open(_vf596, "w", encoding="utf-8").write("{これは壊れています")
            _b5 = _io596.StringIO()
            _av5 = sys.argv
            sys.argv = ["confirmed_values.py", "--record", "--slug", "zzz",
                        "--field", "ceiling_state", "--value-file", _vf596,
                        "--official-url", "https://p-town.dmm.com/machines/1",
                        "--by", "claude,codex", "--why", "試験のためです"]
            try:
                with _cl596.redirect_stdout(_b5):
                    _rc5 = main()
            except Exception as _e5:                         # noqa: BLE001
                _rc5 = "落ちました: " + type(_e5).__name__
            finally:
                sys.argv = _av5
            t("★★渡された値のファイルの壊れを、控えの壊れと言わない★★"
              "（★正常な控えを『壊れています』と案内していた★）",
              _rc5 == 2 and "渡された値のファイル" in _b5.getvalue()
              and "控えのファイルが壊れています" not in _b5.getvalue())

            t("★★控えのファイルが壊れていても、理由を出して終わる★★"
              "（★直す前は traceback で落ち、何が起きたか伝わらなかった★）",
              _rc3 == 1 and "壊れています" in _b3.getvalue())
        finally:
            globals()["STORE"] = _keep596
        # ★★数は「別の数の一部」では通さない★★（2026-08-24・Codexの8回目）
        #   ★直す前はただの部分一致だった★ので、
        #   出典に書かれていない数を「書かれている」ことにできた。
        for _tk, _q, _want in (("3.1", "純増は13.1枚/G", False),
                               ("3.1", "純増は3.1枚/G", True),
                               ("100", "天井は1000G", False),
                               ("100", "天井は100G", True),
                               ("3.1", "純増は3.10枚/G", False),
                               ("600", "天井は1,600G", False),
                               ("600", "天井は600", True),
                               # ★★数で始まる・終わる「言葉」も境目を見る★★
                               #   （2026-09-16・Codexの指摘・台帳#691）
                               #   ★直す前は「まるごと数の形」だけ見ていた★ので、
                               #   下の3つが全部 True になっていた
                               #   ＝出典と違う数を書けた（読者に出る数が変わる）。
                               ("1000G", "天井は11000Gです", False),
                               ("1000G", "天井は1000Gです", True),
                               ("100ゲーム消化",
                                "通常時は1100ゲーム消化からCZ", False),
                               # ★漢数字も数★（二千の「二」で止める）
                               ("千ゲーム消化",
                                "通常時は二千ゲーム消化からCZ", False),
                               ("千ゲーム消化",
                                "通常時は千ゲーム消化からCZ", True),
                               # ★★区切りは全角も見る★★
                               #   （2026-09-16・Codexの指摘・台帳#691）
                               #   ★数の字は isnumeric が全角も拾うのに、
                               #   区切りだけ半角を並べていた★＝
                               #   下の2つが True になっていた。
                               #   天井の形は全角も入口を通るので実在しうる。
                               ("６００", "天井は１，６００G", False),
                               ("１", "天井は１．５G", False),
                               ("６００", "天井は６００Gです", True),
                               # ★★区切りは「その向こうにも数がある」ときだけ★★
                               #   （2026-09-16・Codexの指摘・台帳#691）
                               #   ★直す前★＝隣が「，」なら無条件に別の数の
                               #   一部と見ていたので、★句読点として使われた
                               #   だけの正常な引用まで断っていた★
                               #   ＝#691そのものと同じ型
                               #   （2AIが正しく決めても記録できない）。
                               ("６００", "天井は６００，恩恵はAT当選", True),
                               ("600", "天井は600, 恩恵はAT当選", True),
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
            t("★★2AIで通した出典は、引用のすぐ横が変わったら知らせる★★"
              "／★引用は残っていても、判断の前提は崩れている★",
              [x for x in reverify(
                  _rv_slug, detail=True,
                  fetch=lambda u: (_page2(_quote + " なお内容を更新しました。")
                                   if "chonbo" in u else _page(_quote))
              )["review"] if "引用の周りが変わって" in x])

            # ★★2026-09-08・台帳#585・運営者の承認★★
            #   ★全文の指紋をやめ、引用と根拠の「周り」で見る★
            #   出典ページには設置店の一覧があり、店舗の宣伝文には日付が入る
            #   （実物に「7日!気合い入ります!」）。全文で見ていたので
            #   ★引用も根拠も今もそのままなのに、毎日弾かれていた★
            #   （実測11件中6件）＝確かめた値が1日で使えなくなり、
            #   新台が検索に載らない原因になっていた。
            _far = "あ" * 400            # ★引用から400字離す★

            def _page4(_body, _tail=""):
                return ("<html><head><title>解析まとめ</title></head><body>"
                        f"<p>{_rv_name} の解析です。</p>"
                        f"<p>{_far}</p><p>{_body}</p><p>{_far}</p>"
                        f"<p>{_tail}</p></body></html>")

            _ov4 = verify_source(dict(_src_ov), _rv_name,
                                 lambda u: _page4(_quote))
            t("　（前提）控えに「引用の周りの指紋」が入っている"
              "／★これが無いと全文で見る古い道に落ちる★",
              bool((_ov4.get("identity_override") or {})
                   .get("context", {}).get("quote"))
              and bool((_ov4.get("identity_override") or {})
                       .get("context", {}).get("proof")))
            json.dump({"schema_version": SCHEMA, "machines": {_rv_slug: {
                "ceiling": {
                    "value": {"kind": "GAME", "amount": "999", "unit": "G",
                              "benefit": "AT当選"},
                    "sources": [
                        _ov4,
                        {"url": "https://nana-press.com/kaiseki/x",
                         "quote": _quote}],
                    "lineages": ["vote:chonborista", "vote:nana-press"],
                    "agreed_by": ["claude", "codex"],
                    "why": "2AIで突き合わせました",
                    "decided_at": "2026-08-24",
                    "official_url": ""}}}},
                open(STORE, "w", encoding="utf-8"), ensure_ascii=False)

            def _rv4(tail=""):
                return reverify(
                    _rv_slug, detail=True,
                    fetch=lambda u: (_page4(_quote, tail)
                                     if "chonbo" in u else _page(_quote)))

            t("　（前提）本文が同じままなら通る",
              _rv4()["invalid"] == [] and _rv4()["review"] == [])
            # ★これが直した本体★
            _far_changed = _rv4("新着情報を更新しました。")
            t("★★引用から離れた場所が変わっても止まらない★★"
              "（★設置店の宣伝文の日付で、確かめた値が1日で消えていた★"
              "・台帳#585）",
              _far_changed["invalid"] == [] and _far_changed["review"] == [])
            # ★守りを弱めていないこと★＝全文はちゃんと変わっている
            import hashlib as _hl585
            t("　★対照：全文の指紋なら、この変更でも食い違う★"
              "（＝止まらなくなったのは、見る場所を変えたからで、"
              "検査を外したからではない）",
              _hl585.sha256(page_text(
                  _page4(_quote), "https://chonborista.com/slot/y")
                  .encode("utf-8")).hexdigest()
              != _hl585.sha256(page_text(
                  _page4(_quote, "新着情報を更新しました。"),
                  "https://chonborista.com/slot/y")
                  .encode("utf-8")).hexdigest())
            _near = _rv4.__call__("")      # 近くの変更は本文側を差し替える
            _near = reverify(
                _rv_slug, detail=True,
                fetch=lambda u: (_page4(_quote + " なお内容を更新しました。")
                                 if "chonbo" in u else _page(_quote)))
            t("★★引用のすぐ横が変わったら2AIへ回す★★"
              "（★止めるのではなく、判断し直させる★）",
              _near["invalid"] == []
              and [x for x in _near["review"] if "引用の周りが変わって" in x])
            t("★★引用そのものが消えたら止める★★"
              "（★2AIへ回すのではなく、公開させない★）",
              reverify(
                  _rv_slug, detail=True,
                  fetch=lambda u: (_page4("いまは別のことが書いてあります。")
                                   if "chonbo" in u else _page(_quote))
              )["invalid"] != [])
            t("　★既定（detail=False）が返すのは止める理由だけ★"
              "＝引用の周りが変わっただけでは公開を止めない",
              reverify(
                  _rv_slug,
                  fetch=lambda u: (_page4(_quote + " なお内容を更新しました。")
                                   if "chonbo" in u else _page(_quote))) == [])
            t("　★周りの指紋を持たない古い記録は、全文で見て2AIへ回す★"
              "（★移行の道＝黙って通さない★）",
              _fp_missing_review(_rv_slug, _rv_name, _quote, _src_ov))
            # ★★同定の根拠が消えたら止める★★（2026-09-08・Codexの指摘1）
            #   ★相手のサイトが題とh1に正式名を入れると（よくある改装）、
            #     機械同定が成功して `verify_source` の根拠の検査を
            #     丸ごと通らない★。引用は残っているので、そこも通る。
            #   ＝2AIの判断の土台が消えているのに公開が続いていた。
            t("★★同定の根拠がページから消えたら止める★★"
              "（★2AIへ回すのではなく公開させない＝判断の土台が無い★）",
              _proof_gone_stops(_rv_slug, _rv_name, _quote, _src_ov))
            # ★★控えは、あとから今のページで上書きされない★★
            #   （2026-09-08・Codexの「不変性の試験が無い」）
            #   ★上書きされると、見張りが毎回自己一致して永久に通る★（罠㉕）
            _keep_ctx = dict((_ov4.get("identity_override") or {})
                             .get("context") or {})
            # ★★機械が同定できるページで試す★★（2026-09-08）
            #   ★題が機種名でないページで試すと、壊す前も後も
            #     控えを作り直す枝を通ってしまい、差が出ない★（罠④）。
            #   ここでは題とh1に正式名を入れ、根拠も引用も残したうえで、
            #   ★周りの文字だけを変える★。
            _page5 = (f"<html><head><title>{_rv_name}</title></head><body>"
                      f"<h1>{_rv_name}</h1><p>{_rv_name} の解析です。</p>"
                      f"<p>ここは記録時と違う文です。</p><p>{_quote}</p>"
                      "</body></html>")
            _again = verify_source(dict(_ov4), _rv_name, lambda u: _page5)
            t("　★持ち越した控えに、今のページの指紋を書き足さない★"
              "（★書き足すと見張りが自己一致して永久に通る・罠㉕★）",
              (_again.get("identity_override") or {}).get("context")
              == _keep_ctx)
            t("　（前提）この筋書きでは機械が同定に成功している"
              "／★失敗する題だと、壊す前も後も控えを作り直して差が出ない★",
              __import__("model_code_lookup").page_is_machine(
                  _page5, _rv_name)[0])
        finally:
            STORE = _keep3

        # ★★出典の投稿欄は根拠にしない★★（2026-08-24・Codexの9回目）
        #   ★材料を読む側は落としているのに、確定値の経路だけ抜けていた★＝
        #   ちょんぼりすたの読者コメントに「天井999G」とあれば、
        #   それを逐語引用として記録・再検証できた。
        _ua_url = "https://chonborista.com/slot/orinpia-slot/264134/"
        _ua_html = ('<title>L試験機 スロット 新台 解析 | ちょんぼりすた</title>'
                    '<a class="rating-btn">みんなの評価 (平均0)</a>'
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

    # ─── ★ボーナス確率の受け口★（2026-08-26）────────────
    #   ★形の決まりは spec_lookup の1か所★（同じ規則を2か所に書かない）
    # ★合算は全設定にそろっているか、全設定に無いか★（2026-08-26・P1）
    _bp_ok = {"1": {"big": "1/273.1", "reg": "1/439.8", "total": "1/168.5"},
              "6": {"big": "1/240.1", "reg": "1/240.1", "total": "1/120.0"}}

    def _bp_ng(v):
        try:
            check_spec_shape("bonus_prob", v)
            return False
        except ConfirmedError:
            return True
        except Exception:                                # noqa: BLE001
            # ★きちんと断らずに落ちたのは合格に数えない★（2026-08-26）
            #   ★例外で死ぬと試験が❌を1行も出さず「ただ落ちただけ」になる★
            #   ＝構文エラーと区別がつかず、守りの証拠にならない。
            return False

    t("★★受け口：正しい形は通る★★", not _bp_ng(_bp_ok))
    t("★★受け口：昔の平たい形は断る★★"
      "／★受け口が形を見ないと、壊れた値が控えから記事まで届く★",
      _bp_ng({"1": "1/273.1"}))
    t("　受け口：必須の列が欠けていたら断る", _bp_ng({"1": {"big": "1/273"}}))
    t("　受け口：知らない列は断る",
      _bp_ng({"1": {"big": "1/273", "reg": "1/439", "zzz": "1/1"}}))
    t("　受け口：確率の形でない値は断る",
      _bp_ng({"1": {"big": "273", "reg": "1/439"}}))
    t("★★受け口：合算がある設定と無い設定が混ざったら断る★★"
      "／★記事は『1設定でもあれば列を出す』ので、混ざると食い違う★",
      _bp_ng({"1": {"big": "1/273.1", "reg": "1/439.8", "total": "1/168.5"},
              "6": {"big": "1/240.1", "reg": "1/240.1"}}))
    t("　合算が全設定に無いのは通る（列ごと出さない機種）",
      not _bp_ng({"1": {"big": "1/273.1", "reg": "1/439.8"},
                  "6": {"big": "1/240.1", "reg": "1/240.1"}}))
    t("　受け口：照合に使う語として、表の数値を全部返す",
      sorted(check_spec_shape("bonus_prob", _bp_ok))
      == sorted(["1/273.1", "1/439.8", "1/168.5",
                 "1/240.1", "1/240.1", "1/120.0"]))
    # ─── ★確定値が検索の濃さに届くか★（2026-08-26・Codex35回目の穴2）
    #   ★`merge_into` を通す★＝手で `basis` を付けた材料を採点しない。
    import page_decision as _pd_cv
    _bp_val = {"1": {"big": "1/273.1", "reg": "1/439.8"},
               "6": {"big": "1/240.1", "reg": "1/240.1"}}
    _rec2 = {"value": _bp_val,
             "sources": [{"url": "https://chonborista.com/slot/x",
                          "publisher": "chonborista",
                          "quote": "1/273.1 1/439.8"},
                         {"url": "https://nana-press.com/kaiseki/machine/1/",
                          "publisher": "nana-press",
                          "quote": "1/273.1 1/439.8"}],
             "agreed_by": ["claude", "codex"]}
    _rec1 = {**_rec2, "sources": _rec2["sources"][:1]}
    t("★★独立2系列なら『独立2出典』を名乗る★★"
      "／★名乗らないと、正しく記録しても claim にならず検索へ載らない★",
      _independent_basis(_rec2) == "INDEPENDENT_MULTI")
    t("★1系列だけなら名乗らない★", _independent_basis(_rec1) == "")
    t("　同じ発行者の2ページも1系列（名乗らない）",
      _independent_basis({**_rec2, "sources": [
          {"url": "https://chonborista.com/slot/a",
           "publisher": "chonborista", "quote": "x"},
          {"url": "https://chonborista.com/slot/b",
           "publisher": "chonborista", "quote": "x"}]}) == "")
    t("★★発行者の分からない出典が1つでも混ざれば名乗らない★★"
      "／★飛ばして残りで数えると、素性の知れない出典を混ぜられる★",
      _independent_basis({**_rec2, "sources": _rec2["sources"] + [
          {"url": "https://x.example/z", "quote": "x"}]}) == "")
    t("　発行者が分からなければ名乗らない（fail-closed）",
      _independent_basis({**_rec2, "sources": [
          {"url": "https://x.example/a", "quote": "x"},
          {"url": "https://y.example/b", "quote": "x"}]}) == "")
    # ★本物の入口（merge_into）を通して、claim になるところまで見る★
    _keep_for = globals()["for_slug"]
    try:
        globals()["for_slug"] = lambda s: {"bonus_prob": _rec2}
        _mat_cv = {"adopted": {}}
        merge_into(_mat_cv, "zzz_cv")
        t("★★merge_into が根拠を刻む★★",
          _mat_cv["adopted"]["bonus_prob"].get("basis") == "INDEPENDENT_MULTI")
        t("★★その材料が検索の濃さに数えられる★★"
          "／★これが無いと、第2の出典を見つけても NO_BONUS_PROB のまま★",
          "bonus_prob" in _pd_cv.index_claims_from_material(_mat_cv))
        # ★★箱（天井・AT・CZ）の行にも根拠を刻む★★（2026-08-29）
        #   ★刻まないと、2AIで確定させても検索の濃さに届かない★
        #   （実測：喰霊-零-Re は7項目のうち2項目しか数えられなかった）。
        globals()["for_slug"] = lambda s: {"ceiling": {
            "value": {"kind": "GAME", "amount": "999", "unit": "G",
                      "benefit": "AT"},
            "sources": _rec2["sources"],
            "agreed_by": ["claude", "codex"]}}
        _mat_box = {"adopted": {}, "ceilings": {"adopted": []}}
        merge_into(_mat_box, "zzz_cv")
        _row_box = (_mat_box["ceilings"]["adopted"] or [{}])[0]
        t("★★箱の行にも根拠を刻む★★"
          "／★刻まないと、天井・AT・CZは2AIで確定させても濃さに届かない★",
          _row_box.get("basis") == "INDEPENDENT_MULTI")
        t("　★その天井が検索の濃さに数えられる★",
          any(str(c).startswith("ceiling:")
              for c in _pd_cv.index_claims_from_material(_mat_box)))
        globals()["for_slug"] = lambda s: {"bonus_prob": _rec1}
        _mat_cv1 = {"adopted": {}}
        merge_into(_mat_cv1, "zzz_cv")
        t("★（対照）1系列だけなら濃さに数えない★",
          "bonus_prob" not in _pd_cv.index_claims_from_material(_mat_cv1))
        # ★★全項目に広げた★★（2026-08-29・運営者の指示「全部やろう」）
        #   ★数えるかどうかは項目ではなく「出典が何系列か」で決まる★。
        #   ★型（machine_profile）は claim として数えない★ので、
        #   根拠が刻まれても検索の判定には影響しない（_SPEC_CLAIMS に無い）。
        globals()["for_slug"] = lambda s: {
            "machine_profile": {"value": {"profile": "BONUS"},
                                "sources": _rec2["sources"],
                                "agreed_by": ["claude", "codex"]}}
        _mat_cv2 = {"adopted": {}}
        merge_into(_mat_cv2, "zzz_cv")
        t("★★2系列あれば、どの項目にも根拠を刻む★★"
          "（2026-08-29・運営者の指示）",
          _mat_cv2["adopted"]["machine_profile"].get("basis")
          == "INDEPENDENT_MULTI")
        t("　★型は claim として数えないので、検索の判定は変わらない★",
          "machine_profile" not in _pd_cv.index_claims_from_material(_mat_cv2))
        # ★★機械が1社だけで採った値は、2AIが2社で確定した値に道を譲る★★
        #   （2026-09-24・更新タスクの自己修正）
        #   ★直す前★＝「機械が採れたものは独立2出典で一致したもの」という
        #   前提で、同じ項目に機械の値があれば確定値を足さなかった。
        #   2026-09-20 に1社だけでも採る道（SINGLE_NEAR_RELEASE）を広げたので、
        #   ★1社の値が、2社で確定済みの値を押しのけて記事に出る★ようになった
        #   （実測＝タコスロの機械割がDMM単独の 98.7〜108.5% になり、
        #    2AIが技術介入の列を除いて決めた 98.7〜106.2% が消えた）。
        _rng2 = {"value": {"low": 98.7, "high": 106.2, "unit": "%"},
                 "sources": _rec2["sources"],
                 "agreed_by": ["claude", "codex"]}
        globals()["for_slug"] = lambda s: {"payout_range": _rng2}
        _mat_single = {"adopted": {"payout_range": {
            "value": {"low": 98.7, "high": 108.5, "unit": "%"},
            "sources": ["vote:dmm-ptown"], "basis": "SINGLE_NEAR_RELEASE"}}}
        _got_single = merge_into(_mat_single, "zzz_cv")
        t("★★1社だけの機械の値は、2社で確定した値に置き換わる★★"
          "／★置き換えないと、1社の値が確定値を押しのけて記事に出る★",
          _mat_single["adopted"]["payout_range"].get("_from")
          == "confirmed_values"
          and _mat_single["adopted"]["payout_range"]["value"]["high"] == 106.2
          and "payout_range" in _got_single)
        _mat_multi = {"adopted": {"payout_range": {
            "value": {"low": 98.7, "high": 108.5, "unit": "%"},
            "sources": ["vote:dmm-ptown", "vote:nana-press"],
            "basis": "INDEPENDENT_MULTI"}}}
        merge_into(_mat_multi, "zzz_cv")
        t("　（対照）機械が2社で採った値は、今までどおり上書きしない",
          _mat_multi["adopted"]["payout_range"]["value"]["high"] == 108.5)
        globals()["for_slug"] = lambda s: {"payout_range": {
            **_rng2, "sources": _rec2["sources"][:1]}}
        _mat_single1 = {"adopted": {"payout_range": {
            "value": {"low": 98.7, "high": 108.5, "unit": "%"},
            "sources": ["vote:dmm-ptown"], "basis": "SINGLE_NEAR_RELEASE"}}}
        merge_into(_mat_single1, "zzz_cv")
        t("　（対照）確定値も1系列なら置き換えない（強さが同じ）",
          _mat_single1["adopted"]["payout_range"]["value"]["high"] == 108.5)
        # ★★天井の箱でも、1社だけの機械の行は2社で確定した行に道を譲る★★
        #   （2026-09-25・更新タスクの自己修正）
        #   ★直す前★＝箱（天井）は「中身が完全に同じ」ときだけ重ねずに済ませていた。
        #   機械の行は恩恵を「CZ」、確定値は「CZに当選」と書くので別物に見え、
        #   ★同じ CZ間600G が2行並んだ★。見出しの区別（AT間／CZ間）が
        #   重複で外れ、前に載っていた「ゲーム数天井（CZ間）」の文が
        #   再現できなくなって育成が毎朝止まった（実例＝pw_10503）。
        #   ★同じ天井の目印は構造だけ★＝種類・G数・単位・数える区間。
        _c_cz = {"kind": "GAME", "amount": "600", "unit": "G",
                 "counted": "CZ間", "benefit": "CZに当選"}
        globals()["for_slug"] = lambda s: {"ceiling#cz": {
            "value": _c_cz, "sources": _rec2["sources"],
            "agreed_by": ["claude", "codex"]}}
        _mat_c1 = {"ceilings": {"adopted": [{
            "kind": "GAME", "amount": 600, "unit": "G", "counted": "CZ間",
            "benefit": "CZ", "sources": ["vote:chonborista"],
            "basis": "SINGLE_NEAR_RELEASE"}]}}
        merge_into(_mat_c1, "zzz_cv")
        _rows_c1 = _mat_c1["ceilings"]["adopted"]
        t("★★天井の箱でも、1社だけの機械の行は2社で確定した同じ天井に置き換わる★★"
          "／★置き換えないと同じ CZ間600G が2行並び、見出しの区別が外れる★",
          len(_rows_c1) == 1
          and _rows_c1[0].get("_from") == "confirmed_values"
          and _rows_c1[0].get("benefit") == "CZに当選")
        _mat_c2 = {"ceilings": {"adopted": [{
            "kind": "GAME", "amount": 800, "unit": "G", "counted": "CZ間",
            "benefit": "CZ", "sources": ["vote:chonborista"],
            "basis": "SINGLE_NEAR_RELEASE"}]}}
        merge_into(_mat_c2, "zzz_cv")
        t("　（対照）G数が違う天井は別の天井として残す（行は2つ）",
          len(_mat_c2["ceilings"]["adopted"]) == 2)
        _mat_c3 = {"ceilings": {"adopted": [{
            "kind": "GAME", "amount": 600, "unit": "G", "counted": "CZ間",
            "benefit": "CZ", "sources": ["vote:chonborista", "vote:nana-press"],
            "basis": "INDEPENDENT_MULTI"}]}}
        merge_into(_mat_c3, "zzz_cv")
        t("　（対照）機械が2社で採った天井は消さず、同じ天井の控えも足さない（1行だけ）",
          len(_mat_c3["ceilings"]["adopted"]) == 1
          and _mat_c3["ceilings"]["adopted"][0].get("benefit") == "CZ")
        # ★全角・空白・型の違いは、画面では同じ行＝同じ天井として扱う★
        globals()["for_slug"] = lambda s: {"ceiling#cz": {
            "value": dict(_c_cz, counted=" ＣＺ間 ", unit="Ｇ"),
            "sources": _rec2["sources"], "agreed_by": ["claude", "codex"]}}
        _mat_c4 = {"ceilings": {"adopted": [{
            "kind": "GAME", "amount": 600, "unit": "G", "counted": "CZ間",
            "benefit": "CZ", "sources": ["vote:chonborista"],
            "basis": "SINGLE_NEAR_RELEASE"}]}}
        merge_into(_mat_c4, "zzz_cv")
        t("★全角・空白の違う控えでも、同じ天井なら1行に置き換わる★"
          "（表示側はそろえてから見出しを作るので、比べる側もそろえる）",
          len(_mat_c4["ceilings"]["adopted"]) == 1
          and _mat_c4["ceilings"]["adopted"][0].get("_from") == "confirmed_values")
        globals()["for_slug"] = lambda s: {"ceiling#cz": {
            "value": _c_cz, "sources": _rec2["sources"],
            "agreed_by": ["claude", "codex"]}}
        _mat_c5 = {"ceilings": {"adopted": [{
            "kind": "GAME", "amount": 600, "unit": "G", "counted": "CZ間",
            "count_note": "液晶G数", "benefit": "CZ",
            "sources": ["vote:chonborista"], "basis": "SINGLE_NEAR_RELEASE"}]}}
        merge_into(_mat_c5, "zzz_cv")
        t("★注記を片方だけが書いている天井は同じ天井＝1行だけ★"
          "（ceiling_lookup と同じ考え・2行並ぶと見出しが外れて止まる）"
          "／★注記（液晶G数）は置き換えても残る★",
          len(_mat_c5["ceilings"]["adopted"]) == 1
          and _mat_c5["ceilings"]["adopted"][0].get("count_note") == "液晶G数")
        globals()["for_slug"] = lambda s: {"ceiling#cz": {
            "value": dict(_c_cz, count_note="内部G数"), "sources": _rec2["sources"],
            "agreed_by": ["claude", "codex"]}}
        _mat_c6 = {"ceilings": {"adopted": [{
            "kind": "GAME", "amount": 600, "unit": "G", "counted": "CZ間",
            "count_note": "液晶G数", "benefit": "CZ",
            "sources": ["vote:chonborista"], "basis": "SINGLE_NEAR_RELEASE"}]}}
        merge_into(_mat_c6, "zzz_cv")
        t("　（対照）注記が両方にあって違う天井（液晶G数と内部G数）は別の主張として残す",
          len(_mat_c6["ceilings"]["adopted"]) == 2)
        globals()["for_slug"] = lambda s: {"ceiling#cz": {
            "value": _c_cz, "sources": _rec2["sources"],
            "agreed_by": ["claude", "codex"]}}
        _mat_c7 = {"ceilings": {"adopted": [
            {"kind": "GAME", "amount": 600, "unit": "G", "counted": "CZ間",
             "benefit": "CZ", "sources": ["vote:chonborista"],
             "basis": "SINGLE_NEAR_RELEASE"},
            {"kind": "GAME", "amount": "600", "unit": "G", "counted": "CZ間",
             "benefit": "CZに当選", "_from": "confirmed_values",
             "sources": ["x"], "basis": "INDEPENDENT_MULTI"}]}}
        merge_into(_mat_c7, "zzz_cv")
        t("★強い行が既にあれば、同じ天井の弱い行は外れる★（1行だけ）",
          len(_mat_c7["ceilings"]["adopted"]) == 1
          and _mat_c7["ceilings"]["adopted"][0].get("_from") == "confirmed_values")
        def _weak_row(**kw):
            r = {"kind": "GAME", "amount": 600, "unit": "G", "benefit": "CZ",
                 "sources": ["vote:chonborista"], "basis": "SINGLE_NEAR_RELEASE"}
            r.update(kw)
            return r

        def _cv_with(**kw):
            v = {"kind": "GAME", "amount": "600", "unit": "G",
                 "benefit": "CZに当選"}
            v.update(kw)
            v = {k: x for k, x in v.items() if x is not None}
            globals()["for_slug"] = lambda s: {"ceiling#cz": {
                "value": v, "sources": _rec2["sources"],
                "agreed_by": ["claude", "codex"]}}
        _cv_with(counted=None)
        _mat_d1 = {"ceilings": {"adopted": [_weak_row(counted="CZ間")]}}
        merge_into(_mat_d1, "zzz_cv")
        t("★数える区間を控えだけが書いていない天井も同じ天井＝1行だけ★"
          "／★「CZ間」の条件を持つ行を残す★（詳しい側を残す・控えは書き換えない）",
          len(_mat_d1["ceilings"]["adopted"]) == 1
          and _mat_d1["ceilings"]["adopted"][0].get("counted") == "CZ間")
        _cv_with(counted="CZ間")
        _mat_d2 = {"ceilings": {"adopted": [_weak_row()]}}
        merge_into(_mat_d2, "zzz_cv")
        t("　（逆向き）機械の行だけが数える区間を書いていなくても1行だけ",
          len(_mat_d2["ceilings"]["adopted"]) == 1)
        _cv_with(counted="CZ間")
        _mat_d3 = {"ceilings": {"adopted": [_weak_row(counted="CZ間", benefit="AT")]}}
        merge_into(_mat_d3, "zzz_cv")
        t("★恩恵が本当に違う天井（CZ と AT）は同じとみなさない＝2行★",
          len(_mat_d3["ceilings"]["adopted"]) == 2)
        _mat_d4 = {"ceilings": {"adopted": [_weak_row(counted="CZ間",
                                                      benefit="CZ当選濃厚")]}}
        merge_into(_mat_d4, "zzz_cv")
        t("　（確からしさ）「当選」と「濃厚」は別の主張＝2行",
          len(_mat_d4["ceilings"]["adopted"]) == 2)
        _cv_with(counted="CZ間")
        _mat_d5 = {"ceilings": {"adopted": [
            _weak_row(counted="CZ間", count_note="液晶G数"),
            _weak_row(counted="CZ間", count_note="内部G数")]}}
        merge_into(_mat_d5, "zzz_cv")
        _cv_with(counted="CZ間")
        _mat_d6 = {"ceilings": {"adopted": [
            _weak_row(counted="CZ間"),
            _weak_row(counted="CZ間", count_note="液晶G数")]}}
        merge_into(_mat_d6, "zzz_cv")
        t("★「注記なし」と「液晶G数」の組は一意なので寄せる★（3行に増やさず1行）",
          len(_mat_d6["ceilings"]["adopted"]) == 1)
        _cv_with(counted="CZ間", benefit="CZ", certainty="LIKELY")
        _mat_d7 = {"ceilings": {"adopted": [
            _weak_row(counted="CZ間", certainty="濃厚")]}}
        merge_into(_mat_d7, "zzz_cv")
        t("★確からしさの日本語表記（濃厚）と LIKELY は同じ＝1行★",
          len(_mat_d7["ceilings"]["adopted"]) == 1)
        t("★注記なしの控えが2つの別の天井に当たるときは寄せない★（液晶・内部の2行は残る）",
          sum(1 for r in _mat_d5["ceilings"]["adopted"]
              if r.get("count_note") in ("液晶G数", "内部G数")) == 2)
        _cv_with(counted=None)
        _mat_d8 = {"ceilings": {"adopted": [
            _weak_row(counted="CZ間"), _weak_row(count_note="液晶G数")]}}
        merge_into(_mat_d8, "zzz_cv")
        t("★別々の行の条件を合成しない★（「CZ間だけ」と「液晶G数だけ」の2行は寄せずに残す）",
          sum(1 for r in _mat_d8["ceilings"]["adopted"]
              if r.get("_from") != "confirmed_values") == 2
          and not any(r.get("counted") == "CZ間" and r.get("count_note") == "液晶G数"
                      for r in _mat_d8["ceilings"]["adopted"]))
        _cv_with(counted="CZ間")
        _mat_d9 = {"ceilings": {"adopted": [_weak_row(counted="CZ間", plus_alpha=True)]}}
        merge_into(_mat_d9, "zzz_cv")
        t("★置き換えた行は控えと完全に同じ★（条件や +α を書き足さない＝公開の関所が断るため）",
          len(_mat_d9["ceilings"]["adopted"]) == 1
          and _mat_d9["ceilings"]["adopted"][0].get("_from") == "confirmed_values"
          and "plus_alpha" not in _mat_d9["ceilings"]["adopted"][0])
        _cv_with(counted="CZ間")
        _n_d5 = len(_mat_d5["ceilings"]["adopted"])
        merge_into(_mat_d5, "zzz_cv")
        t("★曖昧で寄せなかった控えも、もう一度足して増えない★",
          len(_mat_d5["ceilings"]["adopted"]) == _n_d5)
        globals()["for_slug"] = lambda s: {"ceiling#cz": {
            "value": _c_cz, "sources": _rec2["sources"],
            "agreed_by": ["claude", "codex"]}}
        merge_into(_mat_c1, "zzz_cv")
        t("★同じ材料へもう一度足しても行は増えない★（何度呼んでも1行）",
          len(_mat_c1["ceilings"]["adopted"]) == 1)
        # ★AT・CZでも、中身がそろえて同じ行は重ねない★（根拠の名乗りと型は比べない）
        globals()["for_slug"] = lambda s: {"at#main": {
            "value": {"mode": "MAIN_AT", "net": "2.8"},
            "sources": _rec2["sources"], "agreed_by": ["claude", "codex"]}}
        _mat_a1 = {"at_specs": {"adopted": [{
            "mode": "MAIN_AT", "net": 2.8, "sources": ["vote:dmm-ptown"],
            "basis": "SINGLE_NEAR_RELEASE"}]}}
        merge_into(_mat_a1, "zzz_cv")
        t("★AT・CZでも、中身が同じ1社だけの行は2社の控えに置き換わる★（1行だけ）",
          len(_mat_a1["at_specs"]["adopted"]) == 1
          and _mat_a1["at_specs"]["adopted"][0].get("_from") == "confirmed_values")
        # ★CZは抽出器の実物の形で試す★（games_disputed などを必ず付ける）
        globals()["for_slug"] = lambda s: {"cz#x": {
            "value": {"name": "○○チャレンジ", "games": "4G", "rate": "約40%"},
            "sources": _rec2["sources"], "agreed_by": ["claude", "codex"]}}
        _mat_z1 = {"czs": {"adopted": [{
            "name": "○○チャレンジ", "games": "4G", "rate": "約40%",
            "sources": ["vote:dmm-ptown"], "basis": "SINGLE_NEAR_RELEASE",
            "games_basis": "SINGLE_NEAR_RELEASE", "rate_basis": "SINGLE_NEAR_RELEASE",
            "games_disputed": False, "rate_disputed": False}]}}
        merge_into(_mat_z1, "zzz_cv")
        t("★CZ（抽出器の実物の形）でも、同じ1社だけの行は2社の控えに置き換わる★",
          len(_mat_z1["czs"]["adopted"]) == 1
          and _mat_z1["czs"]["adopted"][0].get("_from") == "confirmed_values")
        # ★リセットの箱にも同じ決まりが当たる★
        globals()["for_slug"] = lambda s: {"reset#at": {
            "value": {"kind": "CEILING_SHORTENED", "games": "600", "counted": "AT間"},
            "sources": _rec2["sources"], "agreed_by": ["claude", "codex"]}}
        _mat_r1 = {"resets": {"adopted": [{
            "kind": "CEILING_SHORTENED", "games": 600, "counted": "AT間",
            "sources": ["vote:dmm-ptown"], "basis": "SINGLE_NEAR_RELEASE"}]}}
        merge_into(_mat_r1, "zzz_cv")
        t("★リセットの箱でも、同じ1社だけの行は2社の控えに置き換わる★",
          len(_mat_r1["resets"]["adopted"]) == 1
          and _mat_r1["resets"]["adopted"][0].get("_from") == "confirmed_values")
    finally:
        globals()["for_slug"] = _keep_for


    # ─── ★型だけ出典1つでよい★（2026-08-27・運営者の判断）──────────
    #   ★緩めるのは「数値ではない分類」だけ★＝数値は今までどおり2出典。
    #   ★実測の背景★＝「マイジャグラーはボーナスタイプ」を明記しているのは
    #   3社中1社だけ。2出典を課すと、その機種は永久に検索に載せられない。
    t("★★型は出典1つでよい★★", min_sources("machine_profile") == 1)
    t("★★数値は今までどおり2出典★★"
      "／★ここが緩むと、1社の数値がそのまま記事に出る★",
      min_sources("payout_range") == 2 and min_sources("bonus_prob") == 2
      and min_sources("ceiling") == 2 and min_sources("at_prob") == 2)
    t("　知らない項目も2出典（既定は厳しい側）",
      min_sources("zzz_unknown") == 2)
    t("　天井の有無は緩めていない（型とは別に確かめる決まり）",
      min_sources("ceiling_state") == 2)

    _one = [{"publisher": "chonborista", "url": "https://chonborista.com/a",
             "quote": "仕様 ノーマルタイプ"}]

    def _src_ng(srcs, field):
        try:
            check_sources(srcs, field)
            return False
        except ConfirmedError:
            return True

    t("★型なら1系列で通る★", not _src_ng(_one, "machine_profile"))
    t("★★数値は1系列では通らない★★", _src_ng(_one, "payout_range"))
    t("　項目を言わなければ2系列を要求する（安全側）", _src_ng(_one, ""))
    t("　同じ発行者を2つ並べても、型以外は通らない",
      _src_ng(_one + [{"publisher": "chonborista",
                       "url": "https://chonborista.com/b",
                       "quote": "x"}], "bonus_prob"))

    # ── 2026-08-27・台帳#485 待ち行列の新台へ記録できるか ────────
    #   ★本番と同じ順で通す★＝待ち行列は本物の add()／save() で作る。
    #   ★直す前は5晩連続で「1件も記録できない」状態だった★
    #   （2AIが8件で一致したのに、入口で全部落ちていた）。
    import pending_machines as _pm485
    import shutil as _sh485
    _keep485 = _pm485.STORE
    _dir485 = tempfile.mkdtemp()
    try:
        _pm485.STORE = os.path.join(_dir485, "add_machine_pending.json")
        _q485 = _pm485._empty()
        _pm485.add(_q485, "通し試験の新台",
                   "https://p-town.dmm.com/machines/59901", "m", "2026-12",
                   source_machine_id="59901")
        _pm485.add(_q485, "DMM待ちの新台", "", "m", "2026-12",
                   state=_pm485.AWAITING_DMM_ID)
        _pm485.save(_q485)
        # ★例外を受け止めて❌として数える★（2026-08-27）
        #   ★直す前は、壊すと試験そのものが死んでいた★＝
        #   構文エラーと区別がつかず、守りの証拠にならない。
        try:
            _got485 = bind_machine("https://p-town.dmm.com/machines/59901")
        except Exception:                                    # noqa: BLE001
            _got485 = None
        t("★★★通し：待ち行列の新台を公式URLから引ける★★★"
          "／★これが壊れていて、2AIの結論が5晩ぶん捨てられていた★",
          _got485 == ("dmm_59901", "通し試験の新台"))
        _ok485 = False
        try:
            bind_machine("https://p-town.dmm.com/machines/59902")
        except ConfirmedError:
            _ok485 = True
        t("　待ち行列に無いURLは、いままでどおり断る", _ok485)
    finally:
        _pm485.STORE = _keep485
        _sh485.rmtree(_dir485, ignore_errors=True)

    ng = sum(1 for _, o in results if not o)
    print()
    print("%d/%d 合格" % (len(results) - ng, len(results)))
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="2AIで確定した値の受け取り口")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--forget", action="store_true",
                    help="1件を取り除く（--field を省くと機種ごと）")
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
    # ★書かなかったことと、空文字を区別する★（2026-09-09・Codexの指摘）
    #   --forget は「書かなければ機種ごと」なので、既定を空文字にすると
    #   ★--field "" でも機種ごと消えて、正常な記録まで失われる★。
    ap.add_argument("--field", default=None)
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
    ap.add_argument("--wording", action="append", default=[],
                    help="URL|値の字|その出典での書き方"
                         "（2AIが『同じ意味だ』と決めた表記ゆれ。"
                         "★数を含むものは通しません★／"
                         "控えた書き方がその引用に実在することは機械が確かめます）")
    ap.add_argument("--wording-why-file", dest="wording_why_file", default="",
                    help="なぜ同じ意味だと判断したか（%d文字以上・ファイルで渡す）"
                         % MIN_WORDING_WHY)
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
                # ★渡されたファイルの壊れは、控えの壊れと分けて言う★
                #   （2026-09-09・Codexの指摘）★直す前は同じ受け口に
                #   入り、正常な控えを「壊れています」と案内していた★。
                try:
                    value = _sj.read_json(
                        a.value_file, expect=(dict, list, str, int, float))
                except _sj.SafeJsonError as _ev:
                    print("★渡された値のファイルが壊れています★: "
                          + str(_ev)[:200])
                    return 2
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
            # ★2AIが「同じ意味だ」と決めた書き方の違い★（台帳#691）
            #   ★理由はファイルで渡す★（自由文をシェルに書かない・鉄則1c）
            _w_why = ""
            if a.wording_why_file:
                try:
                    _w_why = io.open(a.wording_why_file,
                                     encoding="utf-8").read().strip()
                except Exception as _ew:                     # noqa: BLE001
                    print("--wording-why-file を読めません: " + str(_ew)[:120])
                    return 2
            wording_by_url = {}
            for spec in a.wording:
                parts = [x.strip() for x in str(spec).split("|", 2)]
                if len(parts) != 3 or not all(parts):
                    print("--wording は『URL|値の字|その出典の書き方』の形です")
                    return 2
                wording_by_url.setdefault(parts[0], {})[parts[1]] = parts[2]
            srcs = []
            for s in a.source:
                one = parse_source(s)
                if one["url"] in why_by_url:
                    proof, w = why_by_url.pop(one["url"])
                    one["identity_proof"], one["identity_why"] = proof, w
                if one["url"] in wording_by_url:
                    one["wording"] = wording_by_url.pop(one["url"])
                    one["wording_why"] = _w_why
                srcs.append(one)
            if wording_by_url:
                # ★どの出典にも結び付かない控えは黙って捨てない★
                print("--wording のURLが --source にありません: "
                      + ", ".join(wording_by_url))
                return 2
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
            # ★壊れていても見られる★（2026-09-09・台帳#596）
            #   ★直す前は厳しい読みだったので、1件壊れると
            #   「何が壊れているか」すら見られなかった★。
            data = load(strict=False, require_exists=True)
            # ★★案内は、そのまま実行して直せる形にする★★
            #   （2026-09-09・Codexの指摘）
            #   ★直す前は一律に項目つきの命令を出していた★が、
            #   機種の入れ物ごと壊れている形には項目が存在しないので、
            #   ★その命令では直せなかった★。壊れ方ごとに分ける。
            _ng, _fix = [], []
            for _s, _rows in sorted((data.get("machines") or {}).items()):
                if not isinstance(_rows, dict):
                    _ng.append(f"{_s}: 記録の並びが辞書ではありません")
                    _fix.append("python scripts/confirmed_values.py "
                                f"--forget --slug {_s}")
                    continue
                for _f, _r in sorted(_rows.items()):
                    _bad_r = validate_record(_f, _r)
                    for _x in _bad_r:
                        _ng.append(f"{_s} / {_f}: {_x}")
                    if _bad_r:
                        _fix.append("python scripts/confirmed_values.py "
                                    f"--forget --slug {_s} --field {_f}")
            if _ng:
                print("★契約を満たしていない記録があります★"
                      "（この控えは機械からは読めません）")
                for _x in _ng:
                    print("   ✗ " + _x)
                print("   ★直し方★（そのまま実行できます）")
                for _c in dict.fromkeys(_fix):
                    print("     " + _c)
            for slug, fields in sorted((data.get("machines") or {}).items()):
                if not isinstance(fields, dict):
                    continue
                if a.slug and slug != a.slug:
                    continue
                print("■ " + slug)
                for f, rec in sorted(fields.items()):
                    # ★★壊れている行は、安全な要約だけ出して先へ進む★★
                    #   （2026-09-09・Codexの指摘）
                    #   ★直す前は名指ししたあとで中身を読んでいた★ので、
                    #   記録が文字列なら TypeError、`value` が無ければ
                    #   KeyError で**一覧そのものが落ちた**。
                    #   ＝壊れたときに使う道具が、壊れていると使えない。
                    if not isinstance(rec, dict) or "value" not in rec:
                        print("   %-14s ✗ 壊れています（%s）"
                              % (f, type(rec).__name__))
                        continue
                    print("   %-14s %s" % (f, json.dumps(rec["value"],
                                                         ensure_ascii=False,
                                                         default=str)[:70]))
                    print("      %s ／ %s（%s）"
                          % (rec.get("why"),
                             ",".join(str(x) for x in
                                      (rec.get("agreed_by") or [])
                                      if isinstance(rec.get("agreed_by"), list)),
                             rec.get("decided_at")))
                    _srcs = rec.get("sources")
                    for s in (_srcs if isinstance(_srcs, list) else []):
                        if not isinstance(s, dict):
                            print("      - ✗ 出典が壊れています（%s）"
                                  % type(s).__name__)
                            continue
                        print("      - %s %s"
                              % (s.get("publisher") or "（発行者なし）",
                                 str(s.get("url") or "")[:70]))
            return 0
    except StoreMissingError as e:
        print("★" + str(e) + "★")
        print("   ★直し方★ バックアップから戻してください"
              "（★--init は初回に作るためのもので、復旧には使いません★）")
        return 1
    except StoreBrokenError as e:
        # ★外側が壊れている＝取り除く道具では直せない★（同じ案内を出す）
        print("★確定値の控えが壊れています★: " + str(e)[:200])
        print("   ★直し方★ バックアップから戻すか、"
              "ファイルを手で直してください（★この状態では"
              "--forget も動きません★）")
        return 1
    except ConfirmedError as e:
        print("★" + str(e) + "★")
        return 1
    except _sj.SafeJsonError as e:
        # ★控えのJSONそのものが壊れている★（2026-09-09・Codexの指摘）
        #   ★直す前は traceback で落ちた★＝何が起きたか伝わらない。
        print("★確定値の控えのファイルが壊れています★: " + str(e)[:200])
        # ★できないことを案内しない★（2026-09-09・Codexの指摘）
        #   ★直す前は --forget を案内していた★が、それも最初に
        #   ファイル全体を読むので同じところで止まる＝実行できない。
        print("   ★直し方★ バックアップから戻すか、"
              "ファイルを手で直してください（★この状態では"
              "--forget も動きません★）")
        return 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

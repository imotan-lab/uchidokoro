#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""crosscheck_gates.py — ゲートと独立監査の突き合わせ（毎回同じコマンドで再実行できる停止ゲート）

★何を確かめるか★
  gates.py が「公開してよい」と判断した内容を、gates.py を使わない独立監査（audit_public.py）
  にかけ、食い違いがゼロであることを確かめる。共通原因故障（両者が同じ勘違いをする）の検出。

★最悪運用を想定する★
  分類台帳が未完成のため、未分類の原子を**すべて ALLOW と仮定**して射影する。
  これは「人が仕分けで全部OKにしてしまった場合」に相当し、最も危険な運用を模擬している。
  見出しが通ると配下が新たに検査対象になるため、増えなくなるまで繰り返す（不動点）。

★陰性対照★
  --negative-control を付けると、実データに危険な文を注入して
  「監査器がちゃんと鳴るか」を確認する。何も検出しないこと自体が異常でないかを確かめるため。

実行:
    python scripts/crosscheck_gates.py                 # 突き合わせ（違反があれば非0終了）
    python scripts/crosscheck_gates.py --negative-control
終了コード: 0=合格 / 1=違反あり・陰性対照失敗
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gates                     # noqa: E402
import audit_public              # noqa: E402
import build_ledger as bl        # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "assets", "data")
# 見出しが通ると配下が新たに現れるため、実データでは十数巡かかる機種がある
# （tokyo_ghoul は13巡で収束）。余裕を持たせつつ、無限ループは検出できるようにする。
MAX_ROUNDS = 40

# ★公開機種数の予算（外部固定値）★
#   gates の自己算出だと「1機種が黙って非公開になった」ことを検出できないため、
#   ここに人が意図した数を書く。機種を増減したら、意図した変更として必ずここを直す。
# ★編集待ちの機種★（原稿に「公開できない表現」が残っているもの）
#   これは異常ではなく Phase 2 の作業残量。集合で固定しておき、
#   黙って増えたら異常、減ったら進捗として表示する。
EXPECTED_NEEDS_EDIT = {
    "azurlane",
    "birdie_wing",
    "banchou4",
    "bandori",
    "bofuri",
    "burning_express",
    "code_geass",
    "dark_haibi",
    "eva_yakusoku",
    "fujiko_bt",
    "galfy",
    "gineiden_dnt",
    "godeater",
    "gundam_seed",
    "gundam_uc2",
    "hanabi",
    "hanma_baki",
    "isekai_quattro_bt",
    "iza_bancho",
    "kaguya",
    "kizumonogatari",
    "koukaku",
    "madomagi_forte",
    "monkeyv",
    "my_juggler_v",
    "neoplanet",
    "okidoki_black",
    "okidoki_encore",
    "okidoki_gorgeous",
    "railgun2",
    "rotis",
    "sao",
    "sengoku_otome5",
    "sf5",
    "shake_bt",
    "super_rio_ace2",
    "thunder_v",
    "triple_crown_7",
    "umineko2",
    "yorumungando",
    "zenigata5",
}

# ★表示整合の要修正★（要約とチェッカーの数字が食い違う／交換率を変えても
#   要約が連動しない）。どちらの数字が正しいかは決められないので人の作業。
EXPECTED_DISPLAY_FIX = {
    "chibaryo2",
    "goblin",
    "hokuto_tensei2",
    "prismnana",
}

EXPECTED_PUBLIC = 120 - len(EXPECTED_NEEDS_EDIT) - len(EXPECTED_DISPLAY_FIX - EXPECTED_NEEDS_EDIT)
# ★checker の想定値（2026-07-27 時点）★
#   200は「宣言」ではなく **実際に公開データへ入った mode** の数。
#   同日、チェッカーの注意書きから当サイトでは計算できない断定を47件削除した結果、
#   絶対禁止語で落ちていた mode が公開できるようになり 99機種162mode → 110機種200mode。
#   ただし kaguya は依然 checker 全モードが止まる（rate45 の excellent=1200 が
#   天井1100Gを超え、早見表が「1200G〜（天井1100G）」という到達できない行を作るため。
#   数値の作り直しは Phase 2 の出典検証の仕事なので、いまは止めたままにする）。
EXPECTED_CHECKER_MACHINES = 67
#   2026-07-27（25巡目）: 表示整合の要修正を止めたため 71機種131mode → 67機種123mode
#     （当初10機種→UIが交換率別の狙い目をチェッカーから組み立てるようにして5機種解消）
#   2026-07-27（24巡目）: 原稿に「公開できない表現」が残る41機種を編集待ちとして
#     公開対象から外したため 110機種197mode → 71機種131mode。
#     （編集が進めば EXPECTED_NEEDS_EDIT とともにこの数も戻る）
#   2026-07-27（22巡目）: 0スルーの行が無い suru mode を止めたため 200 → 197
#   （sao / bandori / hanma_baki。UIは0スルー入力を1スルーの閾値で判定していた）
EXPECTED_CHECKER_MODES = 123

# ★公開slugの固定集合★ 件数だけだと「1件消えて1件増える」相殺を見逃すため、
#   集合そのものを持つ。機種を増減したら意図した変更として更新すること。
EXPECTED_PUBLIC_SLUGS = {
    "akudama", "animal_dotch", "azurlane", "babel", "bakemonogatari", "baki", "banchou4",
    "bandori", "basilisk_tenzen", "bigdream_pusher", "biohazard", "biohazard_re3",
    "birdie_wing", "bofuri", "burning_express", "chibaryo2", "code_geass", "dark_haibi",
    "darlifra", "discup_ur", "dmc5_st", "dragon_hanahana_senko", "dumbbell", "enen", "enen2",
    "eva_yakusoku", "fujiko_bt", "funky_juggler2", "galfy", "gineiden_dnt", "goblin",
    "godeater", "godzilla", "gogo_juggler3", "goji_eva", "gundam_seed", "gundam_uc2", "hanabi",
    "hanma_baki", "happy_juggler_v3", "hihou", "hokuto", "hokuto_tensei2", "isekai_quattro_bt",
    "iza_bancho", "jashinchan", "kabaneri", "kaguya", "karakuri", "karakuri2", "kengan_ashura",
    "kerot5bt", "king_hanahana", "kizumonogatari", "koukaku", "kurea_bt", "kyokousuiri",
    "lupin_daikokaisha", "madomagi_forte", "magireco", "mhrise", "midoridon_viva",
    "milliongod_kiseki", "monkeyv", "mr_juggler", "mushoku", "my_juggler_v", "nanatsuma",
    "nangoku_special", "neo_aim_juggler", "neoplanet", "new_king_hanahana_v", "okidoki_black",
    "okidoki_encore", "okidoki_gold", "okidoki_gorgeous", "onepunchman", "onimusha3",
    "prismnana", "railgun2", "revengers", "revue_starlight", "rezero2", "rotis", "sao", "sao2",
    "sengoku_collection6", "sengoku_otome4", "sengoku_otome5", "sf5", "sf6", "shake_bt",
    "shaman_king", "shinuchi_yoshimune", "super_binmusume", "super_blackjack", "super_rio_ace2",
    "takt_opus", "tekken6", "tenken", "tensura", "thunder_v", "toaru_index2", "tokyo_ghoul",
    "tolove_darkness", "tonsuki", "triple_crown_7", "ultraman_final", "umineko2", "valvrave",
    "valvrave2", "world_dai_star", "yabachiba", "yajikita_mairu", "yorumungando", "yoshimune",
    "youjitsu", "zenigata5", "zettai_shougeki4", "zombieland_saga",
}

# ★軸契約の固定集合（件数ではなく (slug, mode) で持つ）★
#   件数だけだと「1件止まらなくなり、別の1件が止まる」入れ替わりを検出できない。
#   データを意図的に変えた時だけ、この集合を意図して更新すること。
EXPECTED_AXIS_STOPPED = {
    # Phase 0 で停止した20mode（構造だけで止まることを確認する対象）
    ("akudama", "suru"), ("azurlane", "suru"), ("basilisk_tenzen", "through"),
    ("biohazard_re3", "suru"), ("darlifra", "suru"), ("gundam_uc2", "suru"),
    ("karakuri", "through"), ("madomagi_forte", "through"), ("mhrise", "suru"),
    ("okidoki_black", "through"), ("okidoki_encore", "through"), ("okidoki_gold", "through"),
    ("onepunchman", "suru"), ("onimusha3", "cycle"), ("revengers", "through"),
    ("sengoku_otome4", "suru"), ("shaman_king", "suru"), ("super_blackjack", "suru"),
    ("super_rio_ace2", "suru"), ("valvrave", "suru"),
    # 2026-07-27 に軸契約で新たに発見した1件（判定材料が無い周期mode）
    ("sengoku_otome5", "cycle"),
}
EXPECTED_AXIS_PASSED = {
    ("tokyo_ghoul", "at"), ("valvrave2", "suru"), ("valvrave2", "cycle"),
    ("monkeyv", "cycle"), ("tekken6", "suru"), ("kaguya", "suru"), ("banchou4", "suru"),
    ("dumbbell", "suru"), ("baki", "suru"), ("sao", "suru"), ("bandori", "suru"),
    ("hanma_baki", "suru"), ("gundam_seed", "suru"), ("youjitsu", "suru"),
    ("kengan_ashura", "suru"), ("goji_eva", "suru"),
}


def _all_allow_ledger(sim: dict, detail: dict, g: dict, slug: str) -> dict:
    """未分類をすべて ALLOW と仮定した台帳（不動点まで反復）。"""
    ledger: dict = {}
    for _ in range(MAX_ROUNDS):
        ctx = bl._Collector(g["profile"], ledger, slug)
        gates._project_machine(sim, g, ctx)
        gates._project_detail(detail, g, ctx)
        new = {it["atom_id"]: {"verdict": "ALLOW"} for it in ctx.items
               if it["atom_id"] not in ledger}
        if not new:
            return ledger
        ledger.update(new)
    raise RuntimeError(f"{slug}: 台帳が収束しない（見出しの依存が深すぎる）")


def run() -> int:
    machines = json.load(open(os.path.join(DATA, "machines.json"), encoding="utf-8"))
    published = blocked = 0
    checker_machines = checker_modes = 0
    problems: list[str] = []
    seen_slugs: set = set()
    # ★件数予算は gates 自身に計算させない（自己申告では機種が消えても気づけない）★
    #   外部の固定値と突き合わせる。機種を増減した時は EXPECTED_PUBLIC を意図して直すこと。
    expected_public = EXPECTED_PUBLIC

    needs_edit: set = set()
    needs_display_fix: set = set()
    for m in machines:
        sim = bl.provisional(m)
        g = gates.compute_gates(sim)
        if not g["public"]:
            continue
        dp = os.path.join(DATA, "machine-details", f"{m['slug']}.json")
        detail = json.load(open(dp, encoding="utf-8")) if os.path.isfile(dp) else {}

        ledger = _all_allow_ledger(sim, detail, g, m["slug"])
        try:
            view = gates.publish_view(sim, detail, ledger)
        except gates.GateError as e:
            blocked += 1
            if "strategyByRate" in str(e):
                # ★表示面どうしの整合が取れていない（要約とチェッカーの食い違い／
                #   交換率を変えても要約が連動しない）★
                #   数字のどちらが正しいかは決められないので、直すのは人の作業。
                needs_display_fix.add(m["slug"])
            elif "公開できない表現" in str(e):
                # ★原稿に「公開できない表現」が残っている＝編集待ち★
                #   これは異常ではなく、Phase 2 でやる作業の残量そのもの。
                #   ただし黙って増減しないよう、集合として突き合わせる。
                needs_edit.add(m["slug"])
            else:
                problems.append(f"{m['slug']}: 公開が止まった（{e}）")
            continue
        published += 1
        # ★宣言ではなく「実際に公開データへ入った mode」を数える★
        #   （宣言だけ残って中身が無い状態を「監査済み」と誤認しないため）
        pub_ck = view["machine"].get("checker")
        if isinstance(pub_ck, dict):
            live = [k for k in view["gates"]["checker_modes"] if k in pub_ck]
            if live:
                checker_machines += 1
                checker_modes += len(live)
            if sorted(live) != sorted(view["gates"]["checker_modes"]):
                problems.append(f"{m['slug']}: 宣言modeと公開configが一致しない")
        elif view["gates"]["checker"]:
            problems.append(f"{m['slug']}: checkerゲートは開いているのに公開データにcheckerが無い")
        # slug重複も停止条件に含める（同じslugが2件あると上書き事故になる）
        problems.extend(audit_public.audit_machine(view["machine"], seen_slugs))
        # LEGACY（記事を出す状態）なのに記事が空なら、記事欠落として止める
        if view["gates"]["profile"] != "preview_basic" and not view["detail"]:
            problems.append(f"{m['slug']}: 記事を出す状態なのに公開記事が空")
        problems.extend(audit_public.audit_detail(
            m["slug"], view["detail"],
            has_disclaimer=isinstance(view["machine"].get("disclaimer"), str)
            and view["machine"]["disclaimer"] == audit_public.EXPECTED_DISCLAIMER,
            surfaces=(view["machine"].get("display_requirements") or {}).get("surfaces")))

    # ★表示整合の要修正も集合で固定する★
    for s_ in sorted(needs_display_fix - EXPECTED_DISPLAY_FIX):
        problems.append(f"表示整合の要修正が増えた: {s_}")
    _dfixed = sorted(EXPECTED_DISPLAY_FIX - needs_display_fix)
    if _dfixed:
        print(f"✅ 表示整合が直った機種: {len(_dfixed)} 件 {_dfixed}")

    # ★編集待ちの集合を突き合わせる★（黙って増えたら異常・減ったら進捗）
    for s_ in sorted(needs_edit - EXPECTED_NEEDS_EDIT):
        problems.append(f"編集待ちが増えた（原稿に公開できない表現が入った）: {s_}")
    _fixed = sorted(EXPECTED_NEEDS_EDIT - needs_edit)
    if _fixed:
        print(f"✅ 編集が済んだ機種: {len(_fixed)} 件 {_fixed[:8]}"
              f"{' …' if len(_fixed) > 8 else ''}")
        print("   → EXPECTED_NEEDS_EDIT から外してください")

    # 件数予算（黙って機種が消える事故を止める）
    if published != expected_public:
        problems.append(f"公開機種数が想定と違う: {published} != {expected_public}")
    # ★集合そのものも突き合わせる（1件消えて1件増える相殺を見逃さない）★
    for s in sorted(EXPECTED_PUBLIC_SLUGS - seen_slugs
                    - EXPECTED_NEEDS_EDIT - EXPECTED_DISPLAY_FIX):
        problems.append(f"公開されるはずの機種が公開されていない: {s}")
    for s in sorted(seen_slugs - EXPECTED_PUBLIC_SLUGS):
        problems.append(f"想定外の機種が公開されている: {s}")

    # ★checkerが実際に射影・監査されていることを停止条件にする★
    #   （以前は暫定状態のズレで全checkerが未検証のまま「違反0」と表示していた）
    if checker_machines != EXPECTED_CHECKER_MACHINES:
        problems.append(f"checkerを公開した機種数が想定と違う: "
                        f"{checker_machines} != {EXPECTED_CHECKER_MACHINES}")
    if checker_modes != EXPECTED_CHECKER_MODES:
        problems.append(f"公開したcheckerモード数が想定と違う: "
                        f"{checker_modes} != {EXPECTED_CHECKER_MODES}")
    if checker_machines == 0:
        problems.append("checkerが1件も監査されていない（検査が素通りしている）")

    problems.extend(axis_regression())    # 軸契約の回帰も停止条件に含める

    print(f"公開できた機種: {published} / 止まった機種: {blocked}（想定 {expected_public}）")
    print(f"独立監査の違反: {len(problems)} 件")
    for p in problems[:30]:
        print("  ✗", p)
    return 1 if problems else 0


def axis_regression() -> list[str]:
    """★実データ全件に対する軸契約の回帰検査★

    Phase 0 で停止した20modeは、停止マーカーを外しても構造だけで止まること。
    正常な回数系modeは通ること。人が付けた印に安全性を依存させないための検査。
    """
    machines = json.load(open(os.path.join(DATA, "machines.json"), encoding="utf-8"))
    ng: list[str] = []
    stopped: set = set()
    passed: set = set()
    for m in machines:
        c = m.get("checker") or {}
        decl = c.get("modes") if isinstance(c.get("modes"), list) else []
        for k, v in list(c.items()) + list((c.get("modeData") or {}).items()):
            if not isinstance(v, dict) or k in ("modeData", "byRate", "exchangeRates"):
                continue
            d = next((x for x in decl if isinstance(x, dict) and x.get("key") == k), {})
            is_target = ("_disabled" in v or k in ("suru", "through", "cycle")
                         or d.get("hasSuru") or d.get("hasCycle"))
            if not is_target:
                continue
            # ★停止マーカーをメモリ上で外して検査する（マーカー非依存を確かめる）★
            probe = {kk: vv for kk, vv in v.items() if kk != "_disabled"}
            (stopped if gates._axis_conflict(k, probe, c.get("unit"), d) else passed).add(
                (m["slug"], k))

    # ★件数ではなく固定集合と突き合わせる（入れ替わりも検出する）★
    for label, got, want in (("止まるべきmode", stopped, EXPECTED_AXIS_STOPPED),
                             ("通るべきmode", passed, EXPECTED_AXIS_PASSED)):
        for x in sorted(want - got):
            ng.append(f"軸回帰: {label}が{'止まらなくなった' if want is EXPECTED_AXIS_STOPPED else '止まるようになった'}: {x[0]}.{x[1]}")
        for x in sorted(got - want):
            ng.append(f"軸回帰: 想定外に{'止まった' if want is EXPECTED_AXIS_STOPPED else '通った'}: {x[0]}.{x[1]}")
    print(f"軸契約の回帰: 止まる {len(stopped)} 件（想定 {len(EXPECTED_AXIS_STOPPED)}）/ "
          f"通る {len(passed)} 件（想定 {len(EXPECTED_AXIS_PASSED)}）")
    return ng


def negative_control() -> int:
    """危険な文を注入して、監査器が確実に鳴ることを確かめる。"""
    cases = [
        ("機種データの計算断定", "計算断定",
         lambda: audit_public.audit_machine(
             {"slug": "x", "name": "t", "strategy": "580G〜から期待収支がプラスになります",
              "disclaimer": audit_public.EXPECTED_DISCLAIMER})),
        ("記事の分割断定", "計算断定",
         lambda: audit_public.audit_detail(
             "x", {"sections": [{"title": "期待値が", "body": ["プラス"]}]}, True)),
        ("設定の非存在断定", "設定段階",
         lambda: audit_public.audit_machine(
             {"slug": "x", "name": "t", "info": "設定3は非搭載",
              "disclaimer": audit_public.EXPECTED_DISCLAIMER})),
        ("秘密つきURL", "URL",
         lambda: audit_public.audit_machine(
             {"slug": "x", "name": "t", "sources": [{"url": "https://a.example/x?token=S"}],
              "disclaimer": audit_public.EXPECTED_DISCLAIMER})),
        ("checker: 閾値の順序破壊", "順序",
         lambda: audit_public.audit_machine(
             {"slug": "x", "name": "t", "disclaimer": audit_public.EXPECTED_DISCLAIMER,
              "display_requirements": {"disclaimer": audit_public.EXPECTED_DISCLAIMER,
                                       "surfaces": ["checker"]},
              "checker": {"unit": "G", "modes": [{"key": "normal", "label": "通常"}],
                          "normal": {"caution": 700, "good": 600, "excellent": 500}}})),
        ("checker: 宣言と実configの不一致", "判定データが無い",
         lambda: audit_public.audit_machine(
             {"slug": "x", "name": "t",
              "checker": {"unit": "G",
                          "modes": [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}],
                          "a": {"good": 600}},
              "disclaimer": audit_public.EXPECTED_DISCLAIMER,
              "display_requirements": {"disclaimer": audit_public.EXPECTED_DISCLAIMER,
                                       "surfaces": ["checker"]}})),
        ("目安ラベル無しの数値", "目安ラベル",
         lambda: audit_public.audit_machine({"slug": "x", "name": "t", "limit": 999})),
        # ===== Codex 21巡目で追加した反例 =====
        ("HTML: 属性つきタグ", "HTML",
         lambda: audit_public.audit_machine(
             {"slug": "x", "name": "t", "strategy": '<span class="a">600G〜</span>',
              "disclaimer": audit_public.EXPECTED_DISCLAIMER})),
        ("HTML: イベント属性", "HTML",
         lambda: audit_public.audit_machine(
             {"slug": "x", "name": "t", "strategy": '<img src=x onerror=alert(1)>',
              "disclaimer": audit_public.EXPECTED_DISCLAIMER})),
        ("不可視文字（U+2066 双方向制御）だけの名前", "表示すると空",
         lambda: audit_public.audit_machine(
             {"slug": "x", "name": "⁦⁩",
              "disclaimer": audit_public.EXPECTED_DISCLAIMER})),
        ("checker: 交換率ごとの good 欠落", "到達できない",
         lambda: audit_public.audit_machine(
             {"slug": "x", "name": "t", "disclaimer": audit_public.EXPECTED_DISCLAIMER,
              "display_requirements": {"disclaimer": audit_public.EXPECTED_DISCLAIMER,
                                       "surfaces": ["checker"]},
              "checker": {"unit": "G", "modes": [{"key": "normal", "label": "通常"}],
                          "exchangeRates": [{"key": "eq56", "label": "5.6枚"},
                                            {"key": "rate50", "label": "5.0枚"}],
                          "normal": {"byRate": {"eq56": {"good": 600, "excellent": 800},
                                                "rate50": {"excellent": 850}}}}})),
        ("checker: 入力上限を超える閾値", "入力上限",
         lambda: audit_public.audit_machine(
             {"slug": "x", "name": "t", "limit": 700,
              "disclaimer": audit_public.EXPECTED_DISCLAIMER,
              "display_requirements": {"disclaimer": audit_public.EXPECTED_DISCLAIMER,
                                       "surfaces": ["checker"]},
              "checker": {"unit": "G", "modes": [{"key": "normal", "label": "通常"}],
                          "normal": {"good": 600, "excellent": 760}}})),
        ("checker: 半端なセンチネル(99999)", "取り決めの形",
         lambda: audit_public.audit_machine(
             {"slug": "x", "name": "t",
              "disclaimer": audit_public.EXPECTED_DISCLAIMER,
              "display_requirements": {"disclaimer": audit_public.EXPECTED_DISCLAIMER,
                                       "surfaces": ["checker"]},
              "checker": {"unit": "G", "modes": [{"key": "normal", "label": "通常"}],
                          "normal": {"good": 600, "excellent": 99999}}})),
        ("checkerが無いのに strategyByRate が残る", "到達不能",
         lambda: audit_public.audit_machine(
             {"slug": "x", "name": "t", "strategyByRate": {"eq56": "600G〜"},
              "disclaimer": audit_public.EXPECTED_DISCLAIMER})),
        ("公開modeに無い limit キー", "到達不能",
         lambda: audit_public.audit_machine(
             {"slug": "x", "name": "t", "limit": {"nope": 700},
              "disclaimer": audit_public.EXPECTED_DISCLAIMER,
              "display_requirements": {"disclaimer": audit_public.EXPECTED_DISCLAIMER,
                                       "surfaces": ["checker"]},
              "checker": {"unit": "G", "modes": [{"key": "normal", "label": "通常"}],
                          "normal": {"good": 600, "excellent": 700}}})),
        # ===== Codex 22巡目で追加した反例 =====
        ("視覚順序を反転させる制御文字(U+202E)", "不可視",
         lambda: audit_public.audit_machine(
             {"slug": "x", "name": "t", "strategy": "‮スラプが値待期‬",
              "disclaimer": audit_public.EXPECTED_DISCLAIMER})),
        ("記事本文の危険HTML", "HTML",
         lambda: audit_public.audit_detail(
             "x", {"sections": [{"title": "本文",
                                 "body": ["<img src=x onerror=alert(1)>"]}]}, True)),
        ("記事本文の不可視文字", "不可視",
         lambda: audit_public.audit_detail(
             "x", {"lead": "‮あ"}, True)),
        ("許可タグは通す（過剰停止の回帰防止）", "",
         lambda: [] if audit_public.html_violation(
             "600G〜<br><strong>目安</strong>") is None else ["許可タグを弾いた"]),
        ("上限の優先順位が machine.limit 優先", "入力上限",
         lambda: audit_public.audit_machine(
             {"slug": "x", "name": "t", "limit": 700,
              "disclaimer": audit_public.EXPECTED_DISCLAIMER,
              "display_requirements": {"disclaimer": audit_public.EXPECTED_DISCLAIMER,
                                       "surfaces": ["checker"]},
              "checker": {"unit": "G", "limit": 1000,
                          "modes": [{"key": "normal", "label": "通常"}],
                          "normal": {"good": 600, "excellent": 800, "limit": 1000}}})),
        ("UIが参照しないフィールドが mode に残る", "参照しない",
         lambda: audit_public.audit_machine(
             {"slug": "x", "name": "t",
              "disclaimer": audit_public.EXPECTED_DISCLAIMER,
              "display_requirements": {"disclaimer": audit_public.EXPECTED_DISCLAIMER,
                                       "surfaces": ["checker"]},
              "checker": {"unit": "G", "modes": [{"key": "normal", "label": "通常"}],
                          "normal": {"good": 600, "excellent": 700, "count": 3}}})),
    ]
    # ===== 一次ゲート側の反例（gates.py が止めることを確かめる）=====
    _base = {"slug": "x", "name": "t", "lifecycle": "LEGACY_SEARCH", "checker_modes": {}}

    def _g(machine, detail=None):
        """audit_view の構造エラー＋内容除去を、監査結果と同じ「文字列の一覧」にする。"""
        a = gates.audit_view(machine, detail)
        return ([f"構造エラー: {e['reason']}" for e in a["errors"]]
                + [f"内容除去: {d['reason']}" for d in a["dropped"]])

    cases += [
        ("[ゲート] machine直下の未知フィールド", "未知フィールド",
         lambda: _g({**_base, "strategy_note": "誤記のため公開禁止"})),
        ("[ゲート] 0スルーの行が無い", "回数",
         lambda: _g({**_base, "checker_modes": {"suru": "STRUCT_OK"},
                     "checker": {"unit": "G",
                                 "modes": [{"key": "suru", "label": "スルー", "hasSuru": True}],
                                 "suru": {"suruMax": 2,
                                          "suru": [{"count": 1, "good": 600, "excellent": 800},
                                                   {"count": 2, "good": 500, "excellent": 700}]}}})),
        ("[ゲート] 交換率別の値が一部だけ", "交換率",
         lambda: _g({**_base, "checker_modes": {"normal": "STRUCT_OK"},
                     "checker": {"unit": "G",
                                 "exchangeRates": [{"key": "eq56", "label": "5.6枚"},
                                                   {"key": "rate45", "label": "4.5枚"}],
                                 "defaultRate": "eq56",
                                 "modes": [{"key": "normal", "label": "通常"}],
                                 "normal": {"good": 600, "excellent": 800,
                                            "byRate": {"eq56": {"good": 620,
                                                                "excellent": 820}}}}})),
        ("[ゲート] G数閾値に小数", "整数",
         lambda: _g({**_base, "checker_modes": {"normal": "STRUCT_OK"},
                     "checker": {"unit": "G",
                                 "modes": [{"key": "normal", "label": "通常"}],
                                 "normal": {"good": 600.5, "excellent": 700.5}}})),
        ("[ゲート] 表の行が3セル", "3セル",
         lambda: _g(_base, {"sections": [{"title": "仕様", "type": "settei",
                                          "rows": [["天井", "999G", "未確認"]]}]})),
        ("[ゲート] 視覚順序を反転させる制御文字", "不可視",
         lambda: _g({**_base, "strategy": "‮スラプが値待期‬"})),
        ("[ゲート] UIが読めない行形式（left/right）", "行形式",
         lambda: _g(_base, {"sections": [{"title": "仕様", "type": "settei",
                                          "rows": [{"left": "天井",
                                                    "right": "999G"}]}]})),
        ("[ゲート] UIが読めない行形式（title/badge/value）", "行形式",
         lambda: _g(_base, {"sections": [{"title": "仕様", "type": "settei",
                                          "rows": [{"title": "天井",
                                                    "badge": "weak",
                                                    "value": "999G"}]}]})),
        ("[ゲート] trigger/hint 形式は通す", "",
         lambda: [x for x in _g(_base, {"sections": [
             {"title": "仕様", "type": "settei",
              "rows": [{"trigger": "天井", "hint": "999G"}]}]})
             if "行形式" in x]),
        ("[ゲート] 漢数字も数値面として扱う", "",
         lambda: [] if "strategy" in (gates.publish_view(
             {**_base, "strategy": "天井九百九十九G"})["machine"]
             .get("display_requirements", {}).get("surfaces", []))
             else ["漢数字の面を拾えていない"]),
    ]

    ng = []
    for name, expect, fn in cases:
        found = fn()
        if expect == "":
            # ★正常系（過剰停止の回帰防止）★ 1件も出ないことを確かめる
            ok = not found
            print(("✅" if ok else "❌") + f" {name}: {len(found)} 件（0件が正）")
            if not ok:
                ng.append(name)
            continue
        # ★「何か検出した」ではなく「注入した違反そのものを検出したか」を見る★
        hit = [p for p in found if expect in p]
        print(("✅" if hit else "❌") + f" {name}: {len(hit)} 件検出（全{len(found)}件）")
        if not hit:
            ng.append(name)
    print(f"\n陰性対照 {len(cases) - len(ng)}/{len(cases)} 合格")
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--negative-control", action="store_true")
    args = ap.parse_args()
    if args.negative_control:
        return negative_control()
    return run()


if __name__ == "__main__":
    sys.exit(main())

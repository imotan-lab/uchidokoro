"""build_new_article.py — 集めた材料から新台の記事データを組み立てる。

★書くのは「裏取りできた材料」だけ★
  文章を作文しない。集まらなかった項目は**書かない**（埋めない・推測しない）。
  字数を満たすための加筆はしない（1500字ルールは廃止済み）。

★出せるのは preview（先行記事）まで★
  この段階で作るのは `status: "preview"` の最小記事。
  noindex になり、検索には出ない。complete への昇格は、
  裏取りが揃って `claim_pipeline` が READY を返してから。

★書き込む前に必ず確かめること（呼び出し側の責任）★
  - `task_guard.py claim` で今日の担当機種であること
  - `task_lock.py check` でロックを持っていること
  - 既にある機種を上書きしないこと（このスクリプトも二重に確かめる）

使い方:
    python scripts/build_new_article.py --slug binmusume --name "Lすーぱぁびん娘" \\
        --maker bellco --official-url https://www.s-bellco.co.jp/products/slot/lbinko/ \\
        --release 2026-08 --material material.json          # 既定 dry-run
    python scripts/build_new_article.py ... --apply
    python scripts/build_new_article.py --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import page_decision as _pd           # noqa: E402
import safe_json as _sj               # noqa: E402
import spec_lookup as _sl             # noqa: E402

MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
DETAILS = os.path.join(BASE, "assets", "data", "machine-details")

# slug に使ってよい形（★推測で作らない・URLの末尾から取る★）
_SLUG_OK = re.compile(r"^[a-z][a-z0-9_]{1,40}$")

# 固定文。★ここに数値を書かない★
# ★時間で嘘になる語（導入予定・登場予定・導入前）を書かない★
#   （2026-08-04・Codex70〜72回目。導入日を過ぎた瞬間に記事が古くなり、
#     8/3導入の7機種で実際に「導入予定」のまま公開が続いた。
#     いつ読んでも真になる文言だけを使う）
# ★確認した日付を読者向けに出さない★（2026-08-04・運営者判断）
#   日付は情報を新しくせず「古さの申告」にしかならない。時間が経つと
#   放置サイトに見え、更新のたびに日付だけ動けば別の不誠実になる。
#   鮮度は表示ではなく**再確認の仕組み**（台帳#220）で担保する。
#   ★代わりに「時間に言及しない書き方」にする★＝いつ読んでも真になる文。
#   （Codexの指摘は「読者の現在に依存する表現が時間で嘘になる」ことなので、
#     相対表現も日付も使わなければ、その懸念自体が消える）
# ★ページ全体への断り書きは書かない★（2026-08-04・運営者判断）
#   「出典で確認が取れた項目のみ掲載」を毎ページ繰り返しても読者の役に立たない。
#   代わりに**未確認の項目の場所に「未確認」と書く**（下の PENDING_TEXT）。
LEAD_TEMPLATE = "{name}の機種情報ページです。登場時期は{release}。"
LEAD_NO_DATE = "{name}のページです。登場時期は当サイトでは確認できていません。"
# ★生成物に混ぜてはいけない語★（検査でも使う）
STALE_WORDS = ("導入予定", "登場予定", "導入前")


# ★未確認の項目は「箱」を先に作り、そこに未確認と書く★（2026-08-04・運営者判断）
#   ページ全体への断り書きを繰り返すのではなく、**その項目の場所**に書く。
#   読者は何が分かっていて何が分かっていないかを一目で把握でき、
#   確認が取れたらこの1文を中身に差し替えるだけで済む。
# ★運営者が選んだ文言（2026-08-12）★／「解析待ち」は導入後に嘘になるので使わない
#   （当サイトが確認していない、はいつ見ても正しい）
PENDING_TEXT = "未確認（確認でき次第掲載します）"
# ★以前の文言★（既に公開した記事に入っている。未確認として扱い続ける）
PENDING_TEXT_OLD = "未確認です。確認でき次第、この欄に掲載します。"
# ★天井が全部そろったと言えないときの断り書き★（比較の対象外にする）
CEILING_PARTIAL_NOTE = ("ほかにも天井がある場合、確認が取れていないものは"
                        "掲載していません。")
# 箱の中の1項目だけが未確認のとき（例: 機械割）に、その欄へ入れる文言
PENDING_ITEM = "未確認（確認でき次第掲載します）"
# ★未確認と見なす文言★（新旧どちらも。ここが唯一の一覧）
PENDING_TEXTS = (PENDING_TEXT, PENDING_TEXT_OLD)


def is_pending_body(body) -> bool:
    """★この箱は「中身が無い（未確認）」か★（2026-08-12・依頼160のP2-7）

    プリレンダ・ブラウザ・最終DOM検査の3か所が別々に文言を持っていて、
    文言を変えたときに**片方だけ**が直り、目印が食い違って公開が止まった。
    """
    if not isinstance(body, list):
        return False
    got = [x.strip() for x in body if isinstance(x, str) and x.strip()]
    return len(got) == 1 and got[0] in PENDING_TEXTS

# 記事に必ず用意する「箱」（確認できたものから中身が入る）
#   ★並びは CLAUDE.md の IDEAL_ORDER に合わせる★
SECTION_ORDER = ("天井・恩恵", "基本スペック", "当サイトの狙い目",
                 "朝一・リセット情報", "ゲーム性", "確認できたCZ",
                 "設定示唆まとめ")


RUMOR_SECTION = {
    "title": "噂・未確定情報",
    "type": "rumor",
    # ★「噂はありません」と書かない★（Codex指摘5）
    #   噂を調べていないのに「無い」と書くのは、確認していないことの断定になる。
    "body": ["**噂・公式未確認**の情報は、当サイトで確認が取れるまで掲載しません。"],
}


# 表で中身を出す箱（本文が空でも表があればよい）
TABLE_SECTIONS = ("確認できたCZ", "設定示唆まとめ")


def expected_titles(detail) -> list:
    """★この記事に出るはずの箱の並び★（噂の箱は中身があるときだけ）

    （2026-08-12・依頼160のP0-5）
    公開の関所・最終DOM検査・記事データの検査の**3か所**が
    それぞれ並びを組み立てていたので、噂の箱を任意にしたときに
    記事データ側だけが直り、**公開できない**状態になった。
    ★数え方を変えるなら、数える場所は1つにする★
    """
    want = list(SECTION_ORDER)
    if not isinstance(detail, dict):
        return want
    titles = [x.get("title") for x in (detail.get("sections") or [])
              if isinstance(x, dict)]
    if titles and titles[-1] == RUMOR_SECTION["title"]:
        want = want + [RUMOR_SECTION["title"]]
    return want


def article_contract_problems(detail) -> list:
    """★記事データが「箱だけの骨組み」になっていないか★

    （2026-08-04・Codex82回目の指摘2。タイトルだけ残して本文を空にすると、
      描き直した期待値も同じく空になるので、突き合わせでは気づけなかった）
    ★中身が無い箱は「未確認」と書いてあること★を要求する。
    """
    if not isinstance(detail, dict):
        return ["記事データが辞書ではありません"]
    # ★噂の箱は中身があるときだけ★（2026-08-12・運営者決定）
    #   噂や小ネタは**無い機種のほうが多い**。空の箱を必ず置くと、
    #   読者には「何かあるのに載せていない」に見える。
    want = expected_titles(detail)
    if not isinstance(detail.get("slug"), str) or not detail["slug"]:
        return ["記事データに slug がありません"]
    secs = detail.get("sections")
    if not isinstance(secs, list) or not all(isinstance(x, dict) for x in secs):
        return ["記事データの sections が節の配列ではありません"]
    if [x.get("title") for x in secs] != want:
        return [f"記事の箱が契約と違います"
                f"（{[x.get('title') for x in secs]} / {want} のはず）"]
    ng = []
    for sec in secs:
        title = sec.get("title")
        body = [x for x in (sec.get("body") or []) if isinstance(x, str) and x.strip()]
        tables = sec.get("tables") or []
        if title == RUMOR_SECTION["title"]:
            # ★出すなら中身が要る★（決まり文句だけの箱は作らない）
            if sec.get("type") != "rumor" or not body:
                ng.append("噂の箱に中身がありません（無いなら箱ごと出さない）")
            continue
        if not body and not tables:
            ng.append(f"箱の中身がありません（未確認と書くこと）: {title}")
            continue
        # ★新旧どちらの文言も「未確認」★（2026-08-12・依頼161）
        #   ここだけ新しい文言を直接見ていたので、古い文言の記事
        #   （ssb1・prskkm の「確認できたCZ」「設定示唆まとめ」）が
        #   **公開の関所と最終監査で拒否**されていた（実データで再現）。
        pending_only = is_pending_body(body)
        if not tables and not pending_only and any(x in PENDING_TEXTS
                                                   for x in body):
            ng.append(f"未確認の文が中身に混ざっています: {title}")
        if title in TABLE_SECTIONS and not tables and not pending_only:
            ng.append(f"表の箱なのに表も未確認表示もありません: {title}")
    return ng


class BuildError(RuntimeError):
    pass


def slug_from_url(official_url: str) -> str:
    """公式ページのURLの末尾から slug を作る。★名前から作らない★

    名前から作ると表記ゆれで揺れる。URLは機種ごとに1つなので安定する。
    """
    tail = official_url.rstrip("/").rsplit("/", 1)[-1].lower()
    tail = re.sub(r"[^a-z0-9_]", "_", tail).strip("_")
    if not _SLUG_OK.match(tail):
        raise BuildError(f"URLから slug を作れません: {official_url}")
    return tail


def _fmt_day(ymd: str) -> str:
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", str(ymd or ""))
    return (f"{m.group(1)}年{int(m.group(2))}月{int(m.group(3))}日"
            if m else str(ymd or ""))


def _fmt_release(ymd: str) -> str:
    """登場時期の書き方（2026-08-12・運営者決定）。

      日まで分かる   → 2026年10月5日 導入
      月までしか無い → 2026年10月頃（曖昧なままでよい）

    ★「導入予定」とは書かない★＝導入後に嘘になる語なので検査で止まる。
    """
    t = str(ymd or "")
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", t)
    if m:
        return f"{m.group(1)}年{int(m.group(2))}月{int(m.group(3))}日 導入"
    m = re.match(r"^(\d{4})-(\d{2})$", t)
    return f"{m.group(1)}年{int(m.group(2))}月頃" if m else t


# 本人性の結び付け方（どの公式ページで確かめたか）
IDENTITY_BINDINGS = ("OFFICIAL_PRODUCT_PAGE", "MAKER_LIST_CARD")


def build_machine(slug, name, maker, official_url, release, material,
                  identity_binding: str = "", identity_evidence_ref: str = "") -> dict:
    """machines.json に足す1件を作る。★確認できた項目だけ★

    identity_binding: 個別ページで確かめたのか、同じ公式の一覧カードで
      確かめたのか（2026-08-04・台帳#209）。★どちらも公式だが粒度が違う★ので
      機械可読に残す。
    """
    ident = {"manufacturer_id": maker, "official_product_url": official_url,
             "announced_name": name, "identity_tier": "CATALOG_BOUND"}
    if identity_binding:
        if identity_binding not in IDENTITY_BINDINGS:
            raise BuildError(f"知らない本人性の結び付け方です: {identity_binding!r}")
        ident["identity_binding"] = identity_binding
        if identity_evidence_ref:
            ident["identity_evidence_ref"] = str(identity_evidence_ref)[:120]
    if release:
        ident["market_release_date"] = release
    code = (material.get("adopted") or {}).get("model_code")
    if code:
        ident["regulatory_model_code"] = code["value"]
        ident["identity_tier"] = "CATALOG_CODE_MATCHED"
        ident["_model_code_sources"] = code["sources"]
    elif (obs := material.get("observed_model_code")):
        # ★1出典しか無い型式も同定の手がかりとして残す★（2026-08-09・依頼130 P1-2）
        #   型式を載せているのは P-WORLD だけなので、これを捨てると
        #   「どの型式のページを見て作ったか」が後から分からなくなる。
        #   ★採用値ではないので regulatory_model_code には入れない★
        ident["observed_model_code"] = obs["value"]
        ident["_observed_model_code_sources"] = obs.get("sources") or []
    # ★判定書（PageDecision）を機種行に焼き込む★（2026-08-04・Codex71〜72回目）
    #   「先行/完成」の宣言をやめ、検索に載せるかは判定書が決める。
    #   status は書かない（旧契約との同居は machine_class が拒否する）。
    decision = _pd.decide(material)
    return {
        "slug": slug,
        "name": name,
        # ★未確認のことを固定値で書かない★（Codex指摘5・自分で確認）
        #   以前は全機種を「スマスロAT」とし、SEOタイトルも「天井・狙い目」と
        #   していた。AT機でない新台を処理した時点で明確な誤情報になるし、
        #   天井を1つも載せていないのに「天井」と名乗るのもおかしい。
        "seo": {"title": f"{name} スペック・基本情報"},
        "info": "",
        # ★狙い目は当サイトの判断なので、確認が取れるまで空にしない・書かない★
        "strategy": "",
        "aliases": [],
        "publication_policy": _pd.SCHEMA,
        "page_decision": decision,
        # ★既存の未裏取りページ（LEGACY_UNVERIFIED）と混ぜない★
        #   載せた値は出典2件で確認済み。ただし記事は網羅的ではない、という状態。
        #   （内部の検証状態＝読者向けラベルではない）
        "publish_state": "PREVIEW_VERIFIED_SUBSET",
        "release_date": release or "",
        "identity": ident,
        # ★早見表の材料（2026-08-12）★ 入るものが無ければ鍵ごと出ない
        **({"checker": ck} if (ck := build_checker(material)) else {}),
    }


def build_checker(material) -> dict | None:
    """早見表の材料（天井・50枚あたりG数）だけの checker を作る。

    ★入れられるものが1つも無ければ作らない★（空の器を置かない）
    ★天井は「G数の天井がちょうど1つ」のときだけ★
      通常時／AT間／スルーのように複数あるとき、どれを通常時の天井として
      扱うかは**意味の判断**なので機械は決めない（2AI・人が後で入れる）。
    """
    adopted = material.get("adopted") or {}
    out: dict = {}
    if (g50 := adopted.get("games_per_50")):
        games = (g50.get("value") or {}).get("games")
        if isinstance(games, (int, float)) and not isinstance(games, bool) \
                and 5 < games < 100:
            out["coinRate"] = games
    ceilings = [c for c in ((material.get("ceilings") or {}).get("adopted") or [])
                if (c or {}).get("kind") == "GAME"]
    mode = {}
    if len(ceilings) == 1:
        # 「1000」「1000+α」どちらも 1000 として読む（+αは前兆ぶんで幅がある）
        m = re.match(r"^(\d{2,5})", str(ceilings[0].get("amount") or "").strip())
        if m and 0 < int(m.group(1)) <= 20000:
            mode["ceiling"] = int(m.group(1))
    if not out and not mode:
        return None
    out["unit"] = "G"
    out["modes"] = [{"key": "normal", "label": "通常"}]
    out["normal"] = mode
    return out


def build_detail(slug, name, release, material) -> dict:
    """記事データを作る。★集まった材料だけを表に入れる★"""
    adopted = material.get("adopted") or {}
    facts = []
    # ★型式名は記事に書かない★（2026-08-09・運営者決定）
    #   型式は「別機種と取り違えないため」の同定に使うもので、読者が使う情報ではない。
    #   載せているのが P-WORLD だけ（実測）なので、記事に出すと
    #   「出典2件で一致した値だけ」という約束も守れない。
    #   同定に使う値は identity.regulatory_model_code に残す（読者には出ない）。
    if (rng := adopted.get("payout_range")):
        v = rng["value"]
        facts.append(["機械割", f"{v['low']}%〜{v['high']}%"])
    if (g50 := adopted.get("games_per_50")):
        facts.append(["50枚あたり", f"約{g50['value']['games']:g}G"])

    boxes = {}          # title -> section（確認できたものだけ中身が入る）
    # ★天井・恩恵★（一式で採れたものだけ。値だけでは載せない）
    ceil = (material.get("ceilings") or {}).get("adopted") or []
    if ceil:
        body = []
        for c in ceil:
            jp = {"GAME": "ゲーム数天井", "CYCLE": "周期天井",
                  "POINT": "ポイント天井"}.get(c["kind"], "天井")
            counted = f"（{c['counted']}を数えます）" if c.get("counted") else ""
            body.append(f"**{jp}**：{c['amount']}{c['unit']}{counted} "
                        f"／ 恩恵：{c['benefit']}")
        # ★断り書きは「1つしか確認できていないとき」だけ★
        #   （2026-08-12・運営者決定）
        #   天井は1機種に1つとは限らない（通常時／AT間／スルー）。
        #   1つしか載っていないと、読者は「この台の天井はこれだけ」と受け取り、
        #   **書いていないことが無いことに見える**。
        #   全部そろっている機種では不要な文なので出さない。
        # ★件数から「全部そろった」と決めない★（2026-08-12・依頼160のP0-2）
        #   通常時・AT間・スルーの3種類ある機種で2件だけ確認できたとき、
        #   件数で判断すると断り書きが消えて「これで全部」に見える。
        #   ★全部そろったと言えるのは、2AIがそう明示したときだけ★
        # ★真偽値の真だけを「全部そろった」と見る★（2026-08-12・依頼161）
        #   truthy で見ると、外から来た "false" という**文字列**でも
        #   断り書きが消える（網羅性が未確認なのに全部に見える）。
        #   ★いまこの旗を立てる処理は無い＝2AIが対話で入れたときだけ★
        if (material.get("ceilings") or {}).get("complete") is not True:
            body.append(CEILING_PARTIAL_NOTE)
        boxes["天井・恩恵"] = {"title": "天井・恩恵", "body": body}
        for c in ceil:
            jp = {"GAME": "ゲーム数天井", "CYCLE": "周期天井",
                  "POINT": "ポイント天井"}.get(c["kind"], "天井")
            facts.append([jp, f"{c['amount']}{c['unit']}"])

    # ★ATの仕様★（モードごとに分けて書く。混ぜたら誤情報）
    ats = (material.get("at_specs") or {}).get("adopted") or []
    if ats:
        body = []
        # ★確認できた項目だけを並べる★（2026-08-09）
        #   以前は「1セットG数」と「純増」が必ずある前提だった。
        #   ところが機種によっては**継続率しか公表されていない**
        #   （実例: スマスロパリピ孔明は継続率73%/81%/91%のみで、
        #   1セットG数も純増も独立2出典では取れない）。
        #   欠けた項目を空欄で書くと嘘になるので、あるものだけ書く。
        for c in sorted(ats, key=lambda x: x["mode"]):
            jp = "メインAT" if c["mode"] == "MAIN_AT" else "上位AT"
            parts = []
            if c.get("games"):
                parts.append(f"1セット{c['games']}G")
            if c.get("net"):
                parts.append(f"純増約{c['net']}枚")
            if c.get("loop_rate"):
                parts.append(f"継続率{c['loop_rate']}")
            if c.get("label"):
                jp = f"{jp}「{c['label']}」"
            body.append(f"**{jp}**：" + " ／ ".join(parts))
        boxes["ゲーム性"] = {"title": "ゲーム性", "body": body}
        for c in sorted(ats, key=lambda x: x["mode"]):
            if not c.get("net"):
                continue
            jp = "メインAT純増" if c["mode"] == "MAIN_AT" else "上位AT純増"
            facts.append([jp, f"約{c['net']}枚/G"])

    # ★朝一・リセット★（2026-08-12・運営者決定）
    #   原文を集める側には前から話題があったのに、書く処理が無かったため
    #   **情報が揃っても永久に空のまま**だった。
    #   ★2AIが確定した値だけを書く★（機械が本文から読み取ることはしない）
    resets = (material.get("resets") or {}).get("adopted") or []
    if resets:
        body = []
        for c in resets:
            kind = c.get("kind")
            if kind == "CEILING_SHORTENED":
                body.append(f"**設定変更後の天井**：{c['games']}G")
            elif kind == "MORNING_STATE":
                body.append(f"**朝一の状態**：{c['state']}")
            elif kind == "ADVANTAGE_RESET":
                body.append(f"**有利区間**：{c['state']}")
        if body:
            boxes["朝一・リセット情報"] = {"title": "朝一・リセット情報",
                                          "body": body}

    # ★CZ★（2026-07-31・Codexと相談した案D）
    #   ★「全種類」だと読まれない書き方にする★
    #     どの出典も「これで全部」とは書いていないため、一覧の完全性は判定できない。
    #     表題を「CZ一覧」にせず、注意書きを**表のすぐ上**に置く（離すと読まれない）。
    czs = (material.get("czs") or {}).get("adopted") or []
    if czs:
        rows = []
        for c in czs:
            parts = []
            if c.get("games"):
                parts.append(f"継続{c['games']}")
            elif c.get("games_disputed"):
                parts.append("継続G数は出典で食い違い")
            if c.get("rate"):
                parts.append(f"期待度 {c['rate']}")
            elif c.get("rate_disputed"):
                parts.append("期待度は出典で書き方が異なります")
            rows.append([c["name"], " ／ ".join(parts) if parts else "確認中"])
        boxes["確認できたCZ"] = {
            "title": "確認できたCZ", "type": "settei",
            "tables": [{"label": "出典2件で確認できたCZ",
                        "headers": ["CZ", "確認できた内容"], "rows": rows,
                        "note": "確認が取れたCZのみを載せています。"
                                "全種類をまとめたものではありません。"}]}

    # ★どの機種にも必ずある項目は、未確認でも欄ごと出す★
    #   （2026-08-04・Codex77回目の指摘2。一部だけ埋まった箱では、
    #     載っていない項目が「未確認なのか・非該当なのか・単に省いたのか」を
    #     読者が区別できなかった）
    #   ★存在するかどうか分からない項目（上位AT・CZ等）はここに並べない★
    #     （並べると「あるのに未確認」と読めてしまう）
    spec_body = [f"**機種名**：{name}"]
    spec_body.append(f"**登場時期**：{_fmt_release(release)}"
                     if release else f"**登場時期**：{PENDING_ITEM}")
    # ★型式名は書かない★（2026-08-09・運営者決定。同定専用にした）
    rng = adopted.get("payout_range")
    spec_body.append(
        f"**機械割**：{rng['value']['low']}%〜{rng['value']['high']}%" if rng
        else f"**機械割**：{PENDING_ITEM}")
    g50 = adopted.get("games_per_50")
    spec_body.append(
        f"**50枚あたりのゲーム数**：約{g50['value']['games']:g}G" if g50
        else f"**50枚あたりのゲーム数**：{PENDING_ITEM}")
    boxes["基本スペック"] = {"title": "基本スペック", "body": spec_body}

    # 設定別の表（★集まった設定だけ★＝1〜6の連番だと決めつけない）
    tables = []
    for key, label in (("at_prob", "AT初当たり確率"), ("payout_rate", "出玉率")):
        got = adopted.get(key)
        if not got:
            continue
        rows = [[f"設定{k}", got["value"][k]] for k in sorted(got["value"])]
        note = "出典で確認が取れた設定のみ掲載しています。"
        # ★値が採れていない設定があるなら、その名前を出す★
        #   （黙って省くと「これで全部」と読まれ、段数を誤って伝えることになる）
        un = material.get("setting_labels_unconfirmed") or []
        if un:
            note += ("この機種には" + "・".join(f"設定{x}" for x in un)
                     + "もありますが、値が確認できていないため掲載していません。")
        tables.append({"label": label, "headers": ["設定", label], "rows": rows,
                       "note": note})
    if tables:
        boxes["設定示唆まとめ"] = {"title": "設定示唆まとめ", "type": "settei",
                                  "tables": tables}

    # ★箱を必ず全部並べる★（確認できていない項目は「未確認」と書いておく）
    sections = []
    for title in SECTION_ORDER:
        sections.append(boxes.get(title)
                        or {"title": title, "body": [PENDING_TEXT]})
    # ★噂の箱は中身ができてから★（2026-08-12・運営者決定）
    #   新台の時点では噂も小ネタも無いので、箱ごと出さない。
    return {
        "slug": slug,
        "updated": date.today().isoformat(),
        # ★導入月が分からないなら「登場予定です」と書かない★（Codex指摘5）
        "lead": (LEAD_TEMPLATE.format(name=name,
                                      release=_fmt_release(release))
                 if release else LEAD_NO_DATE.format(name=name)),
        "summaryBoxes": [],
        "factTable": facts,
        "sections": sections,
    }


def can_publish_page(slug: str):
    """★ページを作れる見込みがあるか★（2026-07-31・実際の危険を確認して追加）

    `index.html` は machines.json の全機種に `/machines/{slug}/` へリンクを張る。
    データだけ書いてページを作らないと、**本番に404リンクができる**。

    いまの作りでは新台のページを作る経路が無い:
      - 通常経路は「公開用のHTMLはここからは作れません」と拒否
      - `--legacy` は既存ページの修理専用で、無い slug は拒否
    そこで**ページを作れないならデータも書かない**（片方だけ残さない）。
    """
    page = os.path.join(BASE, "machines", slug, "index.html")
    if os.path.isfile(page):
        return None
    return ("ページを作る経路がありません。データだけ書くと、"
            "トップページから404になるリンクができます"
            "（machines/{s}/index.html が要ります）".format(s=slug))


def apply(slug, machine, detail, allow_no_page: bool = False) -> list:
    """書き込む。★既にある機種は絶対に上書きしない★"""
    if not allow_no_page:
        why = can_publish_page(slug)
        if why:
            raise BuildError(why)
    rows = _sj.read_rows(MACHINES)
    if any(m.get("slug") == slug for m in rows):
        raise BuildError(f"{slug} はすでに machines.json にあります（上書きしません）")
    dp = os.path.join(DETAILS, f"{slug}.json")
    if os.path.isfile(dp):
        raise BuildError(f"{dp} がすでにあります（上書きしません）")
    # ★2つのファイルを「そろって」書く★（Codex指摘6・自分で確認）
    #   以前は machines.json を先に書き、そのあと記事を書いていた。
    #   後者で失敗すると**一覧にだけ機種がある**中途半端な状態が残る。
    #   いったん別名で書き、両方そろってから置き換える。
    #   片方の置き換えで失敗したら、もう片方を元に戻す。
    rows.append(machine)
    tmp_m, tmp_d = MACHINES + ".new", dp + ".new"
    backup = None
    try:
        for path, data in ((tmp_m, rows), (tmp_d, detail)):
            with open(path, "w", encoding="utf-8", newline=chr(10)) as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
                f.write(chr(10))
        backup = MACHINES + ".bak"
        os.replace(MACHINES, backup)
        try:
            os.replace(tmp_m, MACHINES)
            os.replace(tmp_d, dp)
        except Exception:
            os.replace(backup, MACHINES)      # ★元に戻す★
            backup = None
            raise
    finally:
        for t in (tmp_m, tmp_d):
            if os.path.exists(t):
                os.remove(t)
        if backup and os.path.exists(backup):
            os.remove(backup)
    return [MACHINES, dp]


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []
    nl = chr(10)

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    def _old_wording_article():
        """表の箱に古い文言だけが入っている記事（ssb1・prskkm と同じ形）。"""
        return {"slug": "zzz", "sections": [
            {"title": ti,
             "body": [PENDING_TEXT_OLD if ti in TABLE_SECTIONS else "本文です。"]}
            for ti in SECTION_ORDER]}

    def raises(fn, word=""):
        try:
            fn()
            return False
        except BuildError as e:
            return (word in str(e)) if word else True
        except Exception:
            return False

    t("★slugは公式URLの末尾から作る（名前から作らない）★",
      slug_from_url("https://www.s-bellco.co.jp/products/slot/lbinko/") == "lbinko")
    t("　slugにできない形は拒否する",
      raises(lambda: slug_from_url("https://x.example/products/slot/%%%/")))

    MAT = {"adopted": {
        "model_code": {"value": "Lびん娘NY1", "sources": ["a", "b"]},
        "payout_range": {"value": {"low": 97.3, "high": 112.5, "unit": "%"},
                         "sources": ["a", "b"]},
        "payout_rate": {"value": {"1": "97.3%", "6": "112.5%"}, "sources": ["a", "b"]},
    }}
    m = build_machine("lbinko", "Lすーぱぁびん娘", "bellco",
                      "https://www.s-bellco.co.jp/products/slot/lbinko/", "2026-08", MAT)
    t("★★新台は判定書つき（statusを書かない・旧契約と同居しない）★★"
      "（2026-08-04・Codex71〜72回目）",
      "status" not in m and m["publication_policy"] == _pd.SCHEMA
      and _pd.machine_class(m) in ("AUTO_INDEXABLE", "AUTO_PENDING"))
    t("★★固有ゲーム性が無い材料は indexable にならない★★"
      "（spec系claimだけでは検索に載せない）",
      _pd.machine_class(m) == "AUTO_PENDING"
      and "NO_UNIQUE_GAMEPLAY" in m["page_decision"]["reason_codes"])
    MAT_FULL = dict(MAT)
    MAT_FULL["at_specs"] = {"adopted": [
        {"mode": "MAIN_AT", "games": 30, "net": 2.8, "sources": ["a", "b"]}]}
    m_full = build_machine("lbinko", "Lすーぱぁびん娘", "bellco",
                           "https://www.s-bellco.co.jp/products/slot/lbinko/",
                           "2026-08", MAT_FULL)
    t("★★claim3件・2カテゴリ・固有ゲーム性あり → AUTO_INDEXABLE★★",
      _pd.machine_class(m_full) == "AUTO_INDEXABLE")
    # ★どの公式ページで本人性を確かめたかを残す★（2026-08-04・台帳#209）
    m_card = build_machine("lbinko", "Lすーぱぁびん娘", "bellco",
                           "https://www.s-bellco.co.jp/products/slot/lbinko/",
                           "2026-08", MAT, identity_binding="MAKER_LIST_CARD",
                           identity_evidence_ref="sha256:abc #card0")
    t("★★一覧カードで同定したことを機械可読に残す★★",
      m_card["identity"]["identity_binding"] == "MAKER_LIST_CARD"
      and m_card["identity"]["identity_evidence_ref"] == "sha256:abc #card0")
    t("　結び付け方を書かなければ identity に入らない（既存の形のまま）",
      "identity_binding" not in m["identity"])
    t("★★知らない結び付け方は止める★★",
      raises(lambda: build_machine("x", "X", "y",
                                   "https://a.example/products/slot/x/", "2026-09",
                                   {"adopted": {}}, identity_binding="でたらめ")))
    t("★狙い目は空のまま（当サイトの判断なので推測で埋めない）★", m["strategy"] == "")
    t("　型式が取れていれば identity に入り、段階が上がる",
      m["identity"]["regulatory_model_code"] == "Lびん娘NY1"
      and m["identity"]["identity_tier"] == "CATALOG_CODE_MATCHED")
    m2 = build_machine("x", "X", "y", "https://a.example/products/slot/x/", "2026-09",
                       {"adopted": {}})
    t("　型式が無ければ CATALOG_BOUND のまま",
      m2["identity"]["identity_tier"] == "CATALOG_BOUND"
      and "regulatory_model_code" not in m2["identity"])

    d = build_detail("lbinko", "Lすーぱぁびん娘", "2026-08", MAT)
    txt = json.dumps(d, ensure_ascii=False)
    # ★調べたいのは「値を作文していないか」★
    #   「後で追記します」という説明文に語が出るのは問題ない。
    #   値（数値つきの断定）が入っていないことを見る。
    t("★★集まっていない項目の値を作文しない★★"
      "（天井N G・純増N枚・狙い目N Gのような断定が無い）",
      not re.search(r"(天井|狙い目|純増)[^」』\"]{0,12}?\d", txt))
    t("★★確認できていない項目には『未確認』の箱が用意される★★"
      "（2026-08-04・運営者判断。ページ冒頭の断り書きの代わり）",
      all(any(sec["title"] == title for sec in d["sections"])
          for title in SECTION_ORDER)
      and any(PENDING_TEXT in " ".join(sec.get("body") or [])
              for sec in d["sections"]))
    t("★確認できた値だけが表に入る★",
      ["機械割", "97.3%〜112.5%"] in d["factTable"])
    t("★★型式名は記事に出さない★★（2026-08-09・運営者決定。同定にだけ使う）",
      not any(r[0] == "型式名" for r in d["factTable"])
      and not any("型式名" in " ".join(sec.get("body") or [])
                  for sec in d["sections"]))
    t("★★設定が1〜6の連番だと決めつけない★★（集まった設定だけ出す）",
      [r[0] for r in next(sec for sec in d["sections"]
                          if sec["title"] == "設定示唆まとめ")
       ["tables"][0]["rows"]] == ["設定1", "設定6"])
    t("★★噂の箱は中身ができてから出す★★（2026-08-12・運営者決定）"
      "＝噂や小ネタが無い機種のほうが多く、空の箱は「あるのに載せていない」と読める",
      not any(s.get("type") == "rumor" for s in d["sections"]))
    t("　本文に数値を作文しない（lead に数字が入らない）",
      not re.search(r"\d+\.\d+|\d+G|\d+枚", d["lead"]))
    t("★★確認した日付を読者向けに出さない★★"
      "（古さの申告にしかならず、放置サイトに見える・2026-08-04運営者判断）",
      not __import__("re").search(r"\d{4}年\d{1,2}月\d{1,2}日", txt))
    t("★★読者の『現在』に依存する表現も使わない★★"
      "（時間に言及しなければ、いつ読んでも真になる・Codex74回目の懸念に対応）",
      "現時点" not in txt and "現在" not in txt)
    t("　同じ断りをページ内で繰り返さない（バナーで言っている）",
      sum(1 for sec in d["sections"] for x in sec.get("body", [])
          if "確認が取れた項目だけを掲載" in x) == 0)
    t("★★時間で嘘になる語（導入予定・登場予定・導入前）を書かない★★"
      "（Codex70回目＝8/3導入7機種で「導入予定」のまま公開が続いた実害の再発防止）",
      not any(w in txt for w in STALE_WORDS))
    d2 = build_detail("x", "X", "2026-09", {"adopted": {}})
    t("　材料がゼロでも壊れない（表が空になるだけ）",
      d2["factTable"] == [] and len(d2["sections"]) >= 2)

    t("★★すでにある機種は上書きしない★★",
      raises(lambda: apply("hokuto", {}, {}), "すでに"))

    # ★実際に書き込む経路を確かめる★（2026-07-31）
    #   これまで「すでにある機種は上書きしない」しか試していなかった。
    #   本番のファイルは触らず、使い捨ての場所に向けて書かせる。
    import shutil
    import tempfile
    global MACHINES, DETAILS
    real_m, real_d = MACHINES, DETAILS
    tmpdir = tempfile.mkdtemp(prefix="uchi_apply_")
    try:
        MACHINES = os.path.join(tmpdir, "machines.json")
        DETAILS = os.path.join(tmpdir, "details")
        os.makedirs(DETAILS)
        with open(MACHINES, "w", encoding="utf-8") as f:
            json.dump([{"slug": "aaa", "name": "既存機"}], f, ensure_ascii=False)
        mch = {"slug": "zzz", "name": "テスト機", "status": "preview"}
        det = {"slug": "zzz", "sections": []}
        wrote = apply("zzz", mch, det, allow_no_page=True)
        rows = json.loads(open(MACHINES, encoding="utf-8").read())
        t("★★実際に2つのファイルへ書ける★★（本番では一度も通していなかった）",
          len(wrote) == 2 and len(rows) == 2 and rows[-1]["slug"] == "zzz"
          and os.path.isfile(os.path.join(DETAILS, "zzz.json")))
        t("　既存の機種は消さない", rows[0]["slug"] == "aaa")
        t("　書いたあとに一時ファイルを残さない",
          not [x for x in os.listdir(tmpdir) if x.endswith(".new") or x.endswith(".bak")])

        # ★片方の置き換えに失敗したら、もう片方を元に戻す★
        real_replace = os.replace
        calls = {"n": 0}

        def _flaky(src, dst):
            calls["n"] += 1
            if calls["n"] == 3:          # 3回目＝記事側の置き換え
                raise OSError("わざと失敗させる")
            return real_replace(src, dst)

        before = open(MACHINES, encoding="utf-8").read()
        os.replace = _flaky
        try:
            apply("yyy", {"slug": "yyy", "name": "テスト機2"}, {"slug": "yyy"},
                  allow_no_page=True)
            ok_rollback = False
        except Exception:                # noqa: BLE001
            ok_rollback = True
        finally:
            os.replace = real_replace
        t("★★途中で失敗したら一覧を元に戻す★★（一覧にだけ機種が残らない）",
          ok_rollback and open(MACHINES, encoding="utf-8").read() == before
          and not os.path.isfile(os.path.join(DETAILS, "yyy.json")))
    finally:
        MACHINES, DETAILS = real_m, real_d
        shutil.rmtree(tmpdir, ignore_errors=True)

    t("★★新台は既存の未裏取りページと別の状態名を持つ★★（意味が違うため）",
      build_machine("x", "テスト", "m", "https://m.example/products/slot/x/",
                    "2026-09", {"adopted": {}})["publish_state"]
      == "PREVIEW_VERIFIED_SUBSET")
    t("★★ページを作れないならデータも書かない★★"
      "（一覧に出るのにページが無いと本番が404になる・2026-07-31に確認）",
      can_publish_page("そんな機種はありません") is not None)
    t("　ページがある機種なら止めない", can_publish_page("hokuto") is None)

    # ★古い文言の記事も公開の関所を通る★（2026-08-12・依頼161）
    #   ここだけ新しい文言を直接比べていたので、公開済みの ssb1・prskkm が
    #   **公開の関所と最終監査で拒否**されていた（実データで再現した）。
    t("★★古い文言の記事も契約に合格する★★（公開済み記事が止まらない）",
      not article_contract_problems(_old_wording_article()))
    #   ★対照実験★＝新しい文言だけを見る昔の判定では落ちること
    t("　（対照）昔の判定では落ちる",
      any(x["title"] in TABLE_SECTIONS and x["body"] != [PENDING_TEXT]
          for x in _old_wording_article()["sections"]))
    # ★天井の断り書きは「本物の真」でしか消えない★（2026-08-12・依頼161）
    #   truthy で見ると、外から来た "false" という**文字列**でも消える。
    _mat_false = {"adopted": {}, "need_third": {}, "thin": {},
                  "ceilings": {"adopted": [{"kind": "GAMES", "amount": "800",
                                            "unit": "G", "benefit": "AT当選"}],
                               "complete": "false"}}
    _d_false = build_detail("zzz", "試験機", "2026-09", _mat_false)
    _mat_true = json.loads(json.dumps(_mat_false))
    _mat_true["ceilings"]["complete"] = True
    _d_true = build_detail("zzz", "試験機", "2026-09", _mat_true)

    def _ceil_body(d):
        return next(x["body"] for x in d["sections"] if x["title"] == "天井・恩恵")

    t("★★天井の網羅性は真偽値の真だけ★★"
      "（文字列の \"false\" で断り書きが消えない）",
      CEILING_PARTIAL_NOTE in _ceil_body(_d_false)
      and CEILING_PARTIAL_NOTE not in _ceil_body(_d_true))
    ng = [n for n, ok in results if not ok]
    print(f"{nl}{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--slug")
    ap.add_argument("--name")
    ap.add_argument("--maker")
    ap.add_argument("--official-url", dest="official_url")
    ap.add_argument("--release", default="")
    ap.add_argument("--material", help="spec_lookup の結果を書いたJSON")
    ap.add_argument("--apply", action="store_true", help="実際に書き込む（既定は dry-run）")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not (args.name and args.maker and args.official_url):
        ap.print_help()
        return 0
    slug = args.slug or slug_from_url(args.official_url)
    material = _sj.read_json(args.material, expect=dict) if args.material else {"adopted": {}}
    machine = build_machine(slug, args.name, args.maker, args.official_url,
                            args.release, material)
    detail = build_detail(slug, args.name, args.release, material)
    if not args.apply:
        print("（dry-run／--apply で書き込みます）")
        print(json.dumps({"machine": machine, "detail": detail},
                         ensure_ascii=False, indent=1)[:2600])
        return 0
    wrote = apply(slug, machine, detail)
    print("書き込みました:")
    for w in wrote:
        print("  " + w)
    print(chr(10) + "★このあと必ず★ python scripts/claim_pipeline.py --slug " + slug)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BuildError as e:
        print(f"★{e}★")
        raise SystemExit(1)
    except _sj.SafeJsonError as e:
        print(f"★入力データが読めません: {e}★")
        raise SystemExit(1)
    except Exception as e:
        print(f"★想定外の失敗 {type(e).__name__}: {e}★")
        raise SystemExit(1)

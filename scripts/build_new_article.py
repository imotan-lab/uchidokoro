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
LEAD_TEMPLATE = "{name}の機種情報ページです。登場時期は{release}（公式発表を確認済み）。"
LEAD_NO_DATE = "{name}のページです。登場時期は当サイトでは確認できていません。"
# ★生成物に混ぜてはいけない語★（検査でも使う）
STALE_WORDS = ("導入予定", "登場予定", "導入前")


# ★未確認の項目は「箱」を先に作り、そこに未確認と書く★（2026-08-04・運営者判断）
#   ページ全体への断り書きを繰り返すのではなく、**その項目の場所**に書く。
#   読者は何が分かっていて何が分かっていないかを一目で把握でき、
#   確認が取れたらこの1文を中身に差し替えるだけで済む。
PENDING_TEXT = "未確認です。確認でき次第、この欄に掲載します。"

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


def _fmt_release(ym: str) -> str:
    m = re.match(r"^(\d{4})-(\d{2})$", str(ym or ""))
    return f"{m.group(1)}年{int(m.group(2))}月" if m else str(ym or "")


def build_machine(slug, name, maker, official_url, release, material) -> dict:
    """machines.json に足す1件を作る。★確認できた項目だけ★"""
    ident = {"manufacturer_id": maker, "official_product_url": official_url,
             "announced_name": name, "identity_tier": "CATALOG_BOUND"}
    if release:
        ident["market_release_date"] = release
    code = (material.get("adopted") or {}).get("model_code")
    if code:
        ident["regulatory_model_code"] = code["value"]
        ident["identity_tier"] = "CATALOG_CODE_MATCHED"
        ident["_model_code_sources"] = code["sources"]
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
    }


def build_detail(slug, name, release, material) -> dict:
    """記事データを作る。★集まった材料だけを表に入れる★"""
    adopted = material.get("adopted") or {}
    facts = []
    if (code := adopted.get("model_code")):
        facts.append(["型式名", code["value"]])
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
        body.append("出典2件で一致した内容だけを載せています。"
                    "確認が取れていない天井は掲載していません。")
        boxes["天井・恩恵"] = {"title": "天井・恩恵", "body": body}
        for c in ceil:
            jp = {"GAME": "ゲーム数天井", "CYCLE": "周期天井",
                  "POINT": "ポイント天井"}.get(c["kind"], "天井")
            facts.append([jp, f"{c['amount']}{c['unit']}"])

    # ★ATの仕様★（モードごとに分けて書く。混ぜたら誤情報）
    ats = (material.get("at_specs") or {}).get("adopted") or []
    if ats:
        body = []
        for c in sorted(ats, key=lambda x: x["mode"]):
            jp = "メインAT" if c["mode"] == "MAIN_AT" else "上位AT"
            body.append(f"**{jp}**：1セット{c['games']}G ／ 純増約{c['net']}枚")
        body.append("モードごとに純増が異なります。出典2件で一致した内容だけを載せています。")
        boxes["ゲーム性"] = {"title": "ゲーム性", "body": body}
        for c in sorted(ats, key=lambda x: x["mode"]):
            jp = "メインAT純増" if c["mode"] == "MAIN_AT" else "上位AT純増"
            facts.append([jp, f"約{c['net']}枚/G"])

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

    spec_body = [f"**機種名**：{name}"]
    if release:
        spec_body.append(f"**登場時期**：{_fmt_release(release)}（公式確認）")
    if (code := adopted.get("model_code")):
        spec_body.append(f"**型式名**：{code['value']}")
    if (rng := adopted.get("payout_range")):
        v = rng["value"]
        spec_body.append(f"**機械割**：{v['low']}%〜{v['high']}%")
    if (g50 := adopted.get("games_per_50")):
        spec_body.append(f"**50枚あたりのゲーム数**：約{g50['value']['games']:g}G")
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
    sections.append(RUMOR_SECTION)
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
      ["型式名", "Lびん娘NY1"] in d["factTable"]
      and ["機械割", "97.3%〜112.5%"] in d["factTable"])
    t("★★設定が1〜6の連番だと決めつけない★★（集まった設定だけ出す）",
      [r[0] for r in next(sec for sec in d["sections"]
                          if sec["title"] == "設定示唆まとめ")
       ["tables"][0]["rows"]] == ["設定1", "設定6"])
    t("　噂セクションが必ず入る（新規追加の必須項目）",
      any(s.get("type") == "rumor" for s in d["sections"]))
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

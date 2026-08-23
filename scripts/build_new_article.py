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
# ★★根拠の名乗り（2026-08-23・運営者決定＋Codexの設計）★★
#   ★なぜ値ごとに書くのか★＝表ごとに「出典2件で確認」と一括で名乗ると、
#   DMM単独確認が1件でも混ざった瞬間に**表全体の名乗りが嘘**になる。
#   これは台帳#443（sf6・確かめていない「2件で一致」の名乗り）とまったく同じ型で、
#   ★誤情報より悪い「根拠の詐称」★になる。
#   ★表示文から根拠を逆算しない★＝根拠区分（basis）から表示文を作る。
BASIS_SUFFIX = {
    # 独立2出典は今までどおり何も書かない（それが当サイトの既定だから）
    "INDEPENDENT_MULTI": "",
    "DMM_SINGLE_NEAR_RELEASE": "（DMMぱちタウン単独確認）",
}
# ★単独確認が混ざったときだけ足す断り書き★
SINGLE_SOURCE_NOTE = (
    "「DMMぱちタウン単独確認」は、導入が近い機種について"
    "同サイトの掲載内容のみで確認したものです。"
    "ほかの解析サイトでまだ確認できていません。")


def _basis_tag(basis) -> str:
    """★値のうしろに付ける根拠の名乗り★

    ★★知らない区分は公開しない★★（2026-08-24・Codexの3回目の指摘1）
      ★直す前は空文字を返していた★。すると、

        ①抽出器が根拠を保存し忘れる
        ②検索の濃さには数えない（白名簿なので、ここは安全側に働く）
        ③★けれど記事には**断りなしの普通の値**として出る★

      という経路が通った。★検索に載せなくても読者はページを見られる★ので、
      「DMMだけで確認した値」が2出典で確かめた値と同じ顔で並ぶ。
      ＝当サイトが根拠を偽ったことになる（台帳#443と同じ型）。

      → ★空で流さず、公開そのものを断る★。
    """
    t = str(basis or "")
    if t not in BASIS_SUFFIX:
        raise BuildError(
            f"採用した値に根拠がありません（区分: {basis!r}）／"
            "★根拠の分からない値は記事にしません★"
            "／抽出器が basis を保存し忘れていないか確かめてください")
    return BASIS_SUFFIX[t]


# ★根拠を持たせる場所★＝★名簿そのものを読む★（2026-08-24・Codexの5回目）
#   ★手書きの表をやめた★＝以前は同じ内容を2か所に書いており、
#   「名簿を1か所にした」と報告しながら**関所は手書きの表を読んでいた**。
#   ここで組み立てれば、名簿に足した箱は必ず関所を通る。
_BASIS_REQUIRED = tuple(_pd.READER_BOXES.items())


def require_basis(material: dict, slug: str = "") -> None:
    """★採用した値すべてに、分かる根拠が付いているか★（公開の境界で見る）

    ★名乗りを出す場所だけを見ても足りない★（2026-08-24）
      表示のたびに `_basis_tag` を通せば大半は捕まるが、
      **表示の仕方を1つ足したときに通し忘れる**（今日それを4回やった）。
      材料の側で一度だけ全部を見ておけば、
      表示の書き方が変わっても抜けない。

    ★CZの G数・期待度は、値があるときだけ根拠を求める★
      （値が無いのに根拠だけ要求すると、正しい材料を弾いてしまう）
    """
    recs = _2ai_records(slug) if slug else {}
    for box, keys in _BASIS_REQUIRED:
        got = (material or {}).get(box) or {}
        rows = got.get("adopted") if isinstance(got, dict) else None
        if rows is None and isinstance(got, dict):
            items = list(got.items())           # adopted直下が辞書の形
        else:
            items = [("", x) for x in (rows or [])]
        for name, c in items:
            if not isinstance(c, dict):
                continue
            # ★読者に出さない値には名乗りを求めない★（2026-08-24）
            #   型式名は記事に書かない決まりで、監査47が消している。
            #   ★名簿は判定書と同じものを読む★＝同じ規則を2か所に書かない。
            if name in getattr(_pd, "RETIRED_CLAIMS", ()):
                continue
            if _confirmed_by_2ai(c, slug, recs):
                continue                 # ★2AIの確定値は別の道で根拠が残る★
            for k in keys:
                # ★G数・期待度は、値があるときだけ根拠を求める★
                #   （値が無いのに根拠だけ要求すると正しい材料まで弾く）
                if k != "basis" and c.get(k.replace("_basis", "")) in (
                        None, "", []):
                    continue
                _basis_tag(c.get(k))


def _2ai_records(slug: str) -> dict:
    """その機種について、控えに実在する2AIの確定値を読む（項目名→記録）。

    ★読めなければ「1件も無い」とする★＝印だけの行は通らなくなる。
    （fail-closed。控えが壊れた日に、根拠の無い値を公開しない）
    ★項目名のまま返す★（2026-08-24・Codexの5回目＝値だけで照合すると、
      別項目の控えを証明に使えてしまう）。
    """
    try:
        import confirmed_values as _cv
        return dict(_cv.for_slug(slug) or {})
    except Exception:                                        # noqa: BLE001
        return {}


def _core(d):
    """出所や出典URLを除いた「値そのもの」（控えとの照合に使う）。"""
    if not isinstance(d, dict):
        return d
    return {k: v for k, v in d.items()
            if not str(k).startswith("_") and k != "sources"}


def _confirmed_by_2ai(row, slug: str = "", records=None) -> bool:
    """★2AIが確定させた値か★（★自己申告では通さない★）

    ★★2026-08-24・Codexの4回目の指摘★★
      ★直す前は文字列を1つ見るだけだった★＝

          row.get("_from") == "confirmed_values"

      これは**材料の中の文字列**なので、誰でも付けられる。
      ＝任意のスペック行に印を付ければ、根拠の関所を素通りして
      公開まで到達できた（＝関所を自分で開ける鍵を配っていた）。

    ★直した後★＝印に加えて、**その機種の控えに同じ値が実在するか**を見る。
      控えには出典URL・逐語・判断者・決めた日が入っており、
      記録するときに機械が出典を取りに行って照合している。

    ★slug が渡されないときは通さない★（fail-closed）。
    """
    if not (isinstance(row, dict)
            and row.get("_from") == "confirmed_values"):
        return False
    if not slug:
        return False                     # ★どの機種の控えを見ればよいか分からない★
    field = row.get("_field")
    if not field:
        return False                     # ★どの項目の控えかが分からない★
    recs = records if records is not None else _2ai_records(slug)
    rec = (recs or {}).get(field)
    if not rec:
        return False                     # ★その項目の控えが実在しない★
    got = _core(row)
    val = (rec or {}).get("value")
    # 控えの値そのもの（辞書）／値を包んだ形（{"value": ...}）の両方に合わせる
    return _core(val) == got or {"value": val} == got or val == got.get("value")


def _tag(row, key: str = "basis", slug: str = "", records=None) -> str:
    """★その値に付ける名乗り★（行ごと渡す＝根拠の出どころを見分けるため）"""
    if _confirmed_by_2ai(row, slug, records):
        return ""
    return _basis_tag((row or {}).get(key))

def _has_single_source(items) -> bool:
    """その箱に「単独確認」の値が1つでも混ざっているか。"""
    for c in (items or []):
        if not isinstance(c, dict):
            continue
        for k in ("basis", "games_basis", "rate_basis"):
            if str(c.get(k) or "") == "DMM_SINGLE_NEAR_RELEASE":
                return True
    return False


def _cz_note(czs) -> str:
    """CZの表の注記。★単独確認が混ざったときだけ断りを足す★"""
    base = ("確認が取れたCZのみを載せています。"
            "全種類をまとめたものではありません。")
    return (base + SINGLE_SOURCE_NOTE) if _has_single_source(czs) else base


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


# ★P-WORLDの機種ページ★（2026-08-12・新台の見つけ方をここ一本にした）
_PWORLD_MACHINE = re.compile(
    r"^https?://(?:www\.)?p-world\.co\.jp/machine/database/(\d{1,7})/?$")
# ★DMMぱちタウンの機種ページ★（2026-08-16・台帳#376）
#   規約でP-WORLDへ通信できなくなったので、同定の正はDMMへ移した。
#   ★ホスト・道筋・IDを厳しく見る★（末尾だけ見ると別サイトのURLでも
#   同じslugを作れてしまう＝Codex依頼212の指摘5）
_DMM_MACHINE = re.compile(
    r"^https?://p-town\.dmm\.com/machines/(\d{1,7})/?$")


def slug_from_url(official_url: str) -> str:
    """ページのURLから slug を作る。★名前から作らない★

    名前から作ると表記ゆれで揺れるし、読み方を機械が決めることになる。
    URLは機種ごとに1つなので安定する。

    ★P-WORLDは機種IDが数字なので `pw_<機種ID>` にする★（2026-08-12）
      末尾をそのまま使うと数字だけの slug になり、
      英字で始まる決まりに合わずに止まっていた。
      機種IDは変わらないので、URLと同じくらい安定した名前になる。
    """
    u = str(official_url or "").strip()
    # ★DMMが今の正★（2026-08-16・台帳#376）。移行前の pw_ は
    #   scripts/slug_binding.py の対応表だけが結びつける（増やせない表）。
    m = _DMM_MACHINE.match(u)
    if m:
        return "dmm_" + m.group(1)
    m = _PWORLD_MACHINE.match(u)
    if m:
        return "pw_" + m.group(1)
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
#   ★PWORLD_MACHINE_PAGE★（2026-08-12）＝P-WORLDの機種ページ（機種IDが身元）
#   ★DMM_MACHINE_PAGE★（2026-08-16・台帳#376）＝DMMの機種ページ
#     規約でP-WORLDへ通信できなくなったので、同定の正はこちら。
#     ★ここに足さないと、新台は公開の関所で必ず止まる★
#     （移行した7機種を育てるときも同じ場所で止まる）
IDENTITY_BINDINGS = ("OFFICIAL_PRODUCT_PAGE", "MAKER_LIST_CARD",
                     "PWORLD_MACHINE_PAGE", "DMM_MACHINE_PAGE")


def build_machine(slug, name, maker, official_url, release, material,
                  identity_binding: str = "", identity_evidence_ref: str = "") -> dict:
    """machines.json に足す1件を作る。★確認できた項目だけ★

    identity_binding: 個別ページで確かめたのか、同じ公式の一覧カードで
      確かめたのか（2026-08-04・台帳#209）。★どちらも公式だが粒度が違う★ので
      機械可読に残す。
    """
    require_basis(material, slug)    # ★根拠の無い値は公開しない★
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


def checker_questions(material) -> list:
    """★機械が決められないことを、2AIへの質問として出す★（2026-08-12）

    運営者決定「人が直す項目をなくす。困ったら2AIで判断。
    それでも無理ならメールで知らせる」。
    黙って空にすると誰も気づかないので、必ず質問の形で外へ出す。
    """
    adopted = material.get("adopted") or {}
    ceilings = [c for c in ((material.get("ceilings") or {}).get("adopted") or [])
                if (c or {}).get("kind") == "GAME"]
    if len(ceilings) < 2:
        return []
    # ★答えがあっても、いまの候補に無ければ聞き直す★（2026-08-12・依頼163の1）
    #   出典が更新されて候補が変わったり、打ち間違いで候補に無い値を
    #   記録したりすると、**採用もされず質問も消える**（永久に空のまま）。
    picked = str(((adopted.get("checker_ceiling") or {}).get("value")
                  or {}).get("games") or "").strip()
    if picked and picked in {re.match(r"^(\d{2,5})", str(c.get("amount") or "")
                                      .strip()).group(1)
                             for c in ceilings
                             if re.match(r"^(\d{2,5})",
                                         str(c.get("amount") or "").strip())}:
        return []
    amounts = " / ".join(f"{c.get('amount')}{c.get('unit')}"
                         f"（{c.get('benefit')}）" for c in ceilings)
    return ["★通常時の天井はどれか判断してください★"
            f"（確認できたG数天井: {amounts}）"
            "／早見表の「天井まで残り」に使います。決めたら "
            "confirmed_values.py --record --field checker_ceiling "
            "--value-file <{\"games\": \"1000\"} を書いたファイル> "
            "--why <理由> --by 2AI で記録してください"]


def build_checker(material) -> dict | None:
    """早見表の材料（天井・50枚あたりG数）だけの checker を作る。

    ★入れられるものが1つも無ければ作らない★（空の器を置かない）
    ★天井は「G数の天井がちょうど1つ」のときだけ★
      通常時／AT間／スルーのように複数あるとき、どれを通常時の天井として
      扱うかは**意味の判断**なので機械は決めない。
      ★放置しない★＝決まっていなければ checker_questions() が質問を出し、
      2AIが決めて confirmed_values に記録する（運営者決定 2026-08-12）。
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

    def _as_int(x):
        m = re.match(r"^(\d{2,5})", str(x or "").strip())
        return int(m.group(1)) if m and 0 < int(m.group(1)) <= 20000 else None

    if len(ceilings) == 1:
        # 「1000」「1000+α」どちらも 1000 として読む（+αは前兆ぶんで幅がある）
        if (v := _as_int(ceilings[0].get("amount"))) is not None:
            mode["ceiling"] = v
    elif len(ceilings) >= 2:
        # ★2AIが決めた値があればそれを使う★（2026-08-12・運営者決定）
        #   どれが通常時の天井かは意味の判断なので機械は決めない。
        #   ただし**放置もしない**＝決まっていなければ質問として出す（下）。
        picked = _as_int(((adopted.get("checker_ceiling") or {})
                          .get("value") or {}).get("games"))
        if picked is not None and picked in {_as_int(c.get("amount"))
                                             for c in ceilings}:
            mode["ceiling"] = picked
    if not out and not mode:
        return None
    out["unit"] = "G"
    out["modes"] = [{"key": "normal", "label": "通常"}]
    out["normal"] = mode
    return out


def build_detail(slug, name, release, material) -> dict:
    """記事データを作る。★集まった材料だけを表に入れる★"""
    require_basis(material, slug)    # ★根拠の無い値は記事にしない★
    # ★控えを1回だけ読む★（行ごとに読むと機種の数だけ遅くなる）
    _recs = _2ai_records(slug)

    def _t(row, key: str = "basis") -> str:
        """★この機種の控えと突き合わせたうえで名乗る★"""
        return _tag(row, key, slug, _recs)
    adopted = material.get("adopted") or {}
    facts = []
    # ★型式名は記事に書かない★（2026-08-09・運営者決定）
    #   型式は「別機種と取り違えないため」の同定に使うもので、読者が使う情報ではない。
    #   載せているのが P-WORLD だけ（実測）なので、記事に出すと
    #   「出典2件で一致した値だけ」という約束も守れない。
    #   同定に使う値は identity.regulatory_model_code に残す（読者には出ない）。
    if (rng := adopted.get("payout_range")):
        v = rng["value"]
        facts.append(["機械割",
                      f"{v['low']}%〜{v['high']}%"
                      f"{_t(rng)}"])
    if (g50 := adopted.get("games_per_50")):
        facts.append(["50枚あたり",
                      f"約{g50['value']['games']:g}G"
                      f"{_t(g50)}"])

    boxes = {}          # title -> section（確認できたものだけ中身が入る）
    # ★天井・恩恵★（一式で採れたものだけ。値だけでは載せない）
    ceil = (material.get("ceilings") or {}).get("adopted") or []
    if ceil:
        body = []
        for c in ceil:
            jp = {"GAME": "ゲーム数天井", "CYCLE": "周期天井",
                  "POINT": "ポイント天井"}.get(c["kind"], "天井")
            counted = f"（{c['counted']}を数えます）" if c.get("counted") else ""
            # ★値ごとに根拠を名乗る★（2026-08-23・Codexの指摘4）
            #   ★CZの表だけ直して本文を忘れていた★＝単独確認の天井が
            #   断りなしで出る状態だった。
            body.append(f"**{jp}**：{c['amount']}{c['unit']}{counted} "
                        f"／ 恩恵：{c['benefit']}"
                        f"{_t(c)}")
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
        # ★★「全部そろった」と言えるのは、控えに裏付けがあるときだけ★★
        #   （2026-08-24・Codexの5回目）
        #   ★直す前は材料の生の真偽値だけで断り書きが消えた★。
        #   これは「ほかにも天井があるかもしれない」という
        #   **読者を守る一文**を、誰の証跡も無しに外せるということ。
        _cflag = (material.get("adopted") or {}).get("ceilings_complete")
        _complete = ((material.get("ceilings") or {}).get("complete") is True
                     and _confirmed_by_2ai(_cflag, slug, _recs)
                     and (_cflag or {}).get("value") is True)
        if not _complete:
            body.append(CEILING_PARTIAL_NOTE)
        boxes["天井・恩恵"] = {"title": "天井・恩恵", "body": body}
        for c in ceil:
            jp = {"GAME": "ゲーム数天井", "CYCLE": "周期天井",
                  "POINT": "ポイント天井"}.get(c["kind"], "天井")
            facts.append([jp, f"{c['amount']}{c['unit']}"
                              f"{_t(c)}"])

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
            # ★値ごとに根拠を名乗る★（2026-08-23・Codexの指摘4）
            body.append(f"**{jp}**：" + " ／ ".join(parts)
                        + _t(c))
        boxes["ゲーム性"] = {"title": "ゲーム性", "body": body}
        for c in sorted(ats, key=lambda x: x["mode"]):
            if not c.get("net"):
                continue
            jp = "メインAT純増" if c["mode"] == "MAIN_AT" else "上位AT純増"
            facts.append([jp,
                          f"約{c['net']}枚/G{_t(c)}"])

    # ★ゲームの流れ（数値でないもの）★（2026-08-13・台帳#344）
    #   導入前〜直後は「名前と流れが先に出て、数値は後」。数値が要る器しか
    #   無かったので、いちばん鮮度が価値になる時期に書けなかった。
    #   ★自由文は保存しない★＝2AIが構造にして記録したものから定型文を作る。
    #   ★claimには数えない★＝confirmed_values 由来は page_decision が除外する。
    flows = (material.get("gameplays") or {}).get("adopted") or []
    if flows:
        _lines = []
        for f in flows:
            _head = f"{f['when']}は" if f.get("when") else ""
            _line = f"{_head}**{f['trigger']}**から**{f['leads_to']}**へ進みます"
            # ★根拠の名乗り★（2026-08-24・Codexの5回目＝ここだけ抜けていた）
            #   関所は通るのに表示で名乗らないと、DMM単独の行が
            #   独立2出典の値と**同じ顔で**読者に出る。
            if f.get("gains"):
                _line += "（" + "・".join(str(g) for g in f["gains"]) + "を獲得）"
            _lines.append(_line + _t(f))
        _old = (boxes.get("ゲーム性") or {}).get("body") or []
        _old = [x for x in _old if x not in PENDING_TEXTS]
        boxes["ゲーム性"] = {"title": "ゲーム性", "body": _old + _lines}

    # ★AT名との対応が付かない純増★（2026-08-13・台帳#344）
    #   出典が「純増約3.1枚or約7.4枚/G」としか書かず、どちらがメインで
    #   どちらが上位か割り当てていないとき、**モードへ割り当てない**。
    #   ★順に並べると読者が対応を推測する★ので、必ず断りを添える。
    _unmapped = ((material.get("adopted") or {}).get("at_net_unmapped") or {})
    _uv = (_unmapped.get("value") or {}).get("values") or []
    if _uv:
        _txt = ("**AT純増（AT名との対応は未確認）**："
                + "、".join(f"約{v}枚/G" for v in _uv) + _t(_unmapped))
        _cur = (boxes.get("ゲーム性") or {}).get("body") or []
        _cur = [x for x in _cur if x not in PENDING_TEXTS]
        boxes["ゲーム性"] = {"title": "ゲーム性", "body": _cur + [_txt]}

    # ★朝一・リセット★（2026-08-12・運営者決定）
    #   原文を集める側には前から話題があったのに、書く処理が無かったため
    #   **情報が揃っても永久に空のまま**だった。
    #   ★2AIが確定した値だけを書く★（機械が本文から読み取ることはしない）
    resets = (material.get("resets") or {}).get("adopted") or []
    if resets:
        body = []
        for c in resets:
            kind = c.get("kind")
            # ★根拠の名乗り★（2026-08-24・Codexの5回目＝ここも抜けていた）
            _m = _t(c)
            if kind == "CEILING_SHORTENED":
                body.append(f"**設定変更後の天井**：{c['games']}G{_m}")
            elif kind == "MORNING_STATE":
                body.append(f"**朝一の状態**：{c['state']}{_m}")
            elif kind == "ADVANTAGE_RESET":
                body.append(f"**有利区間**：{c['state']}{_m}")
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
                # ★値ごとに根拠を添える★（2026-08-23・Codexの指摘）
                #   表ごとに一括で名乗ると、1つでも単独確認が混ざった瞬間に
                #   ★表全体の名乗りが嘘★になる（台帳#443と同じ型）。
                parts.append(f"継続{c['games']}{_t(c, "games_basis")}")
            elif c.get("games_disputed"):
                parts.append("継続G数は出典で食い違い")
            if c.get("rate"):
                parts.append(f"期待度 {c['rate']}{_t(c, "rate_basis")}")
            elif c.get("rate_disputed"):
                parts.append("期待度は出典で書き方が異なります")
            rows.append([c["name"] + _t(c),
                         " ／ ".join(parts) if parts else "確認中"])
        boxes["確認できたCZ"] = {
            "title": "確認できたCZ", "type": "settei",
            # ★表題は中立に★（2026-08-23）＝「出典2件で確認できた」と
            #   言い切ると、DMM単独確認が1件でも混ざったとき嘘になる。
            "tables": [{"label": "確認できたCZ",
                        "headers": ["CZ", "確認できた内容"], "rows": rows,
                        "note": _cz_note(czs)}]}

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
        f"**機械割**：{rng['value']['low']}%〜{rng['value']['high']}%"
        f"{_t(rng)}" if rng
        else f"**機械割**：{PENDING_ITEM}")
    g50 = adopted.get("games_per_50")
    spec_body.append(
        f"**50枚あたりのゲーム数**：約{g50['value']['games']:g}G"
        f"{_t(g50)}" if g50
        else f"**50枚あたりのゲーム数**：{PENDING_ITEM}")
    boxes["基本スペック"] = {"title": "基本スペック", "body": spec_body}

    # 設定別の表（★集まった設定だけ★＝1〜6の連番だと決めつけない）
    tables = []
    for key, label in (("at_prob", "AT初当たり確率"), ("payout_rate", "出玉率")):
        got = adopted.get(key)
        if not got:
            continue
        # ★値ごとに根拠を名乗る★（2026-08-23・Codexの再レビューP0-2）
        #   ★ここだけ名乗りが無かった★＝spec_lookup は at_prob/payout_rate にも
        #   DMM単独の例外を当てられるので、設定別の値が
        #   **断りなしで普通の値として**読者に出る状態だった。
        _mark = _t(got)
        rows = [[f"設定{k}", f"{got['value'][k]}{_mark}"]
                for k in sorted(got["value"])]
        note = "出典で確認が取れた設定のみ掲載しています。"
        if _mark:
            note += SINGLE_SOURCE_NOTE
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

    # ★実物の抽出器は採用値に必ず根拠を入れる★（2026-08-23）
    #   ★根拠の無い値は「検索の濃さに数えない」側に落ちる★ので、
    #   試験の材料も実物と同じ形にしておく（手で作った形で通さない）。
    IM = {"basis": "INDEPENDENT_MULTI"}
    MAT = {"adopted": {
        "model_code": {**IM, "value": "Lびん娘NY1", "sources": ["a", "b"]},
        "payout_range": {**IM,
                         "value": {"low": 97.3, "high": 112.5, "unit": "%"},
                         "sources": ["a", "b"]},
        "payout_rate": {**IM, "value": {"1": "97.3%", "6": "112.5%"},
                        "sources": ["a", "b"]},
    }}
    m = build_machine("lbinko", "Lすーぱぁびん娘", "bellco",
                      "https://www.s-bellco.co.jp/products/slot/lbinko/", "2026-08", MAT)
    t("★★新台は判定書つき（statusを書かない・旧契約と同居しない）★★"
      "（2026-08-04・Codex71〜72回目）",
      "status" not in m and m["publication_policy"] == _pd.SCHEMA
      and _pd.machine_class(m) in ("AUTO_INDEXABLE", "AUTO_PENDING"))
    def _ledger(slug, field_values, extra=None):
        """控えに実在させる（★機械が読むのと同じファイル★）。

        ★項目名は本物の名簿にあるものだけ使う★（2026-08-24・Codexの5回目）
          以前は `gameplay_0` のような**本番では登録できない名前**で
          控えを作っていた。＝本番の登録契約を満たさない試験だった。
        extra: {項目名: 値} ＝ adopted 側に入る確定値
        """
        data = {"schema_version": _cv_t.SCHEMA, "machines": {}}
        rows = {}

        def _rec(v):
            return {"value": v,
                    "sources": [{"url": "https://p-town.dmm.com/machines/dmm_1",
                                 "quote": "試験用"}],
                    "agreed_by": ["Claude", "Codex"],
                    "decided_at": "2026-08-24"}
        for _k, _v in (extra or {}).items():
            rows[_k] = _rec(_v)
        for i, v in enumerate(field_values):
            # ★名簿にある名前★（gameplay / gameplay#2 …）
            rows["gameplay" if i == 0 else f"gameplay#{i + 1}"] = _rec(v)
        data["machines"][slug] = rows
        os.makedirs(os.path.dirname(_cv_t.STORE), exist_ok=True)
        with open(_cv_t.STORE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)

    # ★★2026-08-13・台帳#344（数値でないゲーム性）★★
    def _gp_mat(flows=None, unmapped=None):
        m = {"adopted": {}, "ceilings": {"adopted": []},
             "at_specs": {"adopted": []}, "czs": {"adopted": []},
             "resets": {"adopted": []}}
        if flows:
            # ★★本物の取り込みに作らせる★★（2026-08-24・Codexの5回目）
            #   ★直す前は印を手で付けていた★＝本番では
            #   `confirmed_values.merge_into` が入れるので、
            #   **その処理が刻む印（項目名など）を試験が知らないまま**だった。
            #   ＝「都合のよいJSONを直接作る試験」。
            #   ここでは控えを先に用意し、本番と同じ関数に入れさせる。
            _ledger("pw_x", flows)
            _cv_t.merge_into(m, "pw_x")
        if unmapped:
            # ★★本物の取り込みに作らせる★★（2026-08-24・Codexの5回目）
            #   ★手で印を付けていた★＝本番が刻む項目名を知らないまま
            #   試験していたので、照合の抜けに気づけなかった。
            _ledger("pw_x", [], extra={"at_net_unmapped": {
                "values": unmapped, "mapping": "UNCONFIRMED"}})
            _cv_t.merge_into(m, "pw_x")
        return m

    # ★★控えを実際に作って、本番と同じ読み口で照合させる★★
    #   （2026-08-24・Codexの4回目の指摘＝2AIの印が自己申告だった）
    #   ★印だけの手作りで試験すると、照合を外しても気づけない★ので、
    #   本物の置き場（一時ディレクトリ）へ控えを書いてから記事を作る。
    import tempfile as _tf_t

    import confirmed_values as _cv_t
    _cv_t.STORE = os.path.join(_tf_t.mkdtemp(prefix="cv_"),
                               "confirmed_values.json")

    def _raises(fn) -> bool:
        """★その呼び出しが例外で止まるか★（止まらなければ守りが無い）"""
        try:
            fn()
        except Exception:                                    # noqa: BLE001
            return True
        return False


    def _gp_body(mat):
        d = build_detail("pw_x", "試験機", "2026-09-07", mat)
        got = next((x for x in d["sections"] if x["title"] == "ゲーム性"), None)
        return (got or {}).get("body") or []

    t("★★確定値が無ければ、ゲーム性は未確認のまま★★（台帳#344）",
      _gp_body(_gp_mat()) == [PENDING_TEXT])
    _f1 = [{"when": "通常時", "trigger": "周期抽選", "leads_to": "CZ"}]
    _ledger("pw_x", _f1)
    t("★★2AIが構造で記録した流れを、定型文にして書く★★（台帳#344）",
      _gp_body(_gp_mat(_f1))
      == ["通常時は**周期抽選**から**CZ**へ進みます"])
    # ★★対照実験★★＝本物の取り込みで作った材料でも、
    #   そのあと控えから消えていれば公開を断る（＝印だけでは通らない）
    _mat_signed = _gp_mat(_f1)          # 本物の取り込みが印を刻んだ材料
    _ledger("pw_x", [])                 # 控えを空にする
    t("★★控えに無い値は、2AIの印が付いていても公開を断る★★"
      "／★印は材料の中の文字列なので、誰でも付けられる★",
      _raises(lambda: build_detail("pw_x", "試験機", "2026-09-07",
                                   _mat_signed)))
    _ledger("pw_x", _f1)
    # ★★項目名まで見ている★★（別項目の控えを証明に使えない）
    _mat_wrong = json.loads(json.dumps(_mat_signed))
    for _r in _mat_wrong["gameplays"]["adopted"]:
        _r["_field"] = "cz"             # 実在するが、この値の項目ではない
    # ★★関所そのものを直接試す★★（2026-08-24）
    #   ★これが無いと、名簿から箱を外しても試験が緑のままだった★＝
    #   表示の道が別に名乗りを求めるので、**関所を外した影響が見えなかった**。
    #   ＝守りが二重にあるとき、片方を消しても気づけない。
    # ★★試験の側で「あるべき箱」を書き留める★★（2026-08-24）
    #   ★直す前は名簿そのものを回していた★＝名簿から箱を減らすと
    #   **試験のケースも一緒に減って、緑のまま**だった。
    #   ＝壊す対象からケースを作る試験は、その対象を壊しても気づけない。
    _MUST_GATE = ("adopted", "ceilings", "at_specs", "czs", "gameplays",
                  "resets")
    t("★★読者に出る箱が、名簿から漏れていない★★"
      "（箱を減らしたら、ここで落ちる）",
      set(_MUST_GATE) <= set(_pd.READER_BOXES))
    for _box in _MUST_GATE:
        if _box == "adopted":
            _bare = {"adopted": {"payout_range": {"value": {"low": 97,
                                                            "high": 110}}}}
        else:
            _bare = {"adopted": {}, _box: {"adopted": [{"name": "x",
                                                        "kind": "GAME",
                                                        "amount": 999,
                                                        "unit": "G",
                                                        "when": "通常時",
                                                        "trigger": "a",
                                                        "leads_to": "b",
                                                        "state": "高確",
                                                        "mode": "MAIN_AT",
                                                        "net": 2.0}]}}
        t(f"★★関所は {_box} の根拠なしを断る★★"
          "（名簿から外したら、ここが落ちる）",
          _raises(lambda m=_bare: require_basis(m, "zzz")))

    t("★★別の項目の控えでは通らない★★"
      "／★値だけを照合すると、出玉率の控えでAT確率を通せてしまう★",
      _raises(lambda: build_detail("pw_x", "試験機", "2026-09-07",
                                   _mat_wrong)))
    _f2 = [{"trigger": "全国制覇", "leads_to": "上位CZ",
            "gains": ["上乗せ", "武将参戦"]}]
    _ledger("pw_x", _f2)
    t("　条件が無い流れは「〜は」を付けずに書く／結果があれば添える",
      _gp_body(_gp_mat(_f2))
      == ["**全国制覇**から**上位CZ**へ進みます（上乗せ・武将参戦を獲得）"])
    t("★★AT名と対応の付かない純増は、必ず断りを添えて書く★★"
      "（順に並べると読者が対応を推測してしまう）",
      _gp_body(_gp_mat(unmapped=["3.1", "7.4"]))
      == ["**AT純増（AT名との対応は未確認）**：約3.1枚/G、約7.4枚/G"])
    t("★★ゲームの流れは公開ゲートのclaimに数えない★★"
      "（記事には出すが、検索に載せる判定は変えない）",
      (lambda: __import__("page_decision").claims_from_material(
          _gp_mat([{"trigger": "全国制覇", "leads_to": "上位CZ"}],
                  unmapped=["3.1"])) == [])())
    t("★★固有ゲーム性が無い材料は indexable にならない★★"
      "（spec系claimだけでは検索に載せない）",
      _pd.machine_class(m) == "AUTO_PENDING"
      and "NO_UNIQUE_GAMEPLAY" in m["page_decision"]["reason_codes"])
    # ★★単独確認の値には、どこに出ても必ず名乗りが付く★★
    #   （2026-08-23・Codexの敵対的レビュー指摘4）
    #   ★私はCZの表しか直しておらず★、機械割・50枚あたり・天井・AT・
    #   factTable には**断りなしで単独確認の値が出る**状態だった。
    #   ＝台帳#443（sf6・確かめていない「2件で一致」の名乗り）と同じ型。
    SS = {"basis": "DMM_SINGLE_NEAR_RELEASE"}
    MAT_SS = {"adopted": {
        "payout_range": {**SS, "value": {"low": 97.0, "high": 110.0,
                                         "unit": "%"}, "sources": ["a"]},
        "games_per_50": {**SS, "value": {"games": 36.1}, "sources": ["a"]},
        # ★設定別の表も入れる★（Codexの指摘＝前回の材料に無かったので
        #   名乗りの抜けを検出できなかった）
        "payout_rate": {**SS, "value": {"1": "97.0%", "6": "110.0%"},
                        "sources": ["a"]}},
        "ceilings": {"adopted": [{**SS, "kind": "GAME", "amount": 999,
                                  "unit": "G", "benefit": "AT当選",
                                  "sources": ["a"]}]},
        "at_specs": {"adopted": [{**SS, "mode": "MAIN_AT", "net": 1.0,
                                  "sources": ["a"]}]},
        "czs": {"adopted": [{**SS, "name": "試験CZ", "games": "8G",
                             "games_basis": SS["basis"], "sources": ["a"]}]}}
    _d_ss = build_detail("zzz", "試験機", "2026-08-17", MAT_SS)

    def _all_text(d):
        out = []
        for sec in (d.get("sections") or []):
            out += [str(x) for x in (sec.get("body") or [])]
            for tb in (sec.get("tables") or []):
                out.append(str(tb.get("note") or ""))
                for row in (tb.get("rows") or []):
                    out += [str(x) for x in (row or [])]
        for row in (d.get("factTable") or []):
            out += [str(x) for x in (row or [])]
        return out

    _texts = _all_text(_d_ss)
    _NUM_MARKS = ("97.0%", "36.1G", "999G", "約1.0枚", "8G", "110.0%")
    _naked = [t for t in _texts
              if any(m in t for m in _NUM_MARKS)
              and "DMMぱちタウン単独確認" not in t]
    t("★★単独確認の値は、どこに出ても必ず名乗りが付く★★"
      "／★付け忘れると「根拠の詐称」になる（台帳#443と同じ型）★",
      _texts and not _naked)
    if _naked:
        print("   ★名乗りが付いていない箇所★: " + " / ".join(_naked[:3]))
    t("　独立2出典のときは何も名乗らない（今までどおりの見た目）",
      not any("DMMぱちタウン単独確認" in x
              for x in _all_text(build_detail("zzz", "試験機", "2026-08-17",
                                              MAT))))

    MAT_FULL = dict(MAT)
    MAT_FULL["at_specs"] = {"adopted": [
        {**IM, "mode": "MAIN_AT", "games": 30, "net": 2.8,
         "sources": ["a", "b"]}]}
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
                  "ceilings": {"adopted": [{"basis": "INDEPENDENT_MULTI",
                                            "kind": "GAMES", "amount": "800",
                                            "unit": "G", "benefit": "AT当選"}],
                               "complete": "false"}}
    _d_false = build_detail("zzz", "試験機", "2026-09", _mat_false)
    _mat_true = json.loads(json.dumps(_mat_false))
    _mat_true["ceilings"]["complete"] = True
    # ★★「全部そろった」には控えの裏付けが要る★★（2026-08-24・Codexの5回目）
    #   ★申告だけでは断り書きは消えない★ので、本物の取り込みで裏付けを入れる。
    _ledger("zzz_complete", [], extra={"ceilings_complete": True})
    _cv_t.merge_into(_mat_true, "zzz_complete")
    _d_true = build_detail("zzz_complete", "試験機", "2026-09", _mat_true)
    # ★対照★ 控えが無ければ、申告があっても断り書きは残る
    _mat_claim = json.loads(json.dumps(_mat_false))
    _mat_claim["ceilings"]["complete"] = True
    _d_claim = build_detail("zzz", "試験機", "2026-09", _mat_claim)

    def _ceil_body(d):
        return next(x["body"] for x in d["sections"] if x["title"] == "天井・恩恵")

    t("★★控えの裏付けが無ければ、申告があっても断り書きは残る★★"
      "／★『ほかにも天井があるかも』は読者を守る一文★",
      CEILING_PARTIAL_NOTE in next(
          x["body"] for x in _d_claim["sections"]
          if x["title"] == "天井・恩恵"))
    t("★★天井の網羅性は真偽値の真だけ★★"
      "（文字列の \"false\" で断り書きが消えない）",
      CEILING_PARTIAL_NOTE in _ceil_body(_d_false)
      and CEILING_PARTIAL_NOTE not in _ceil_body(_d_true))
    # ★機械が決められないことは質問として出す★（2026-08-12・運営者決定）
    #   「人が直す項目をなくす。困ったら2AIで判断。それでも無理ならメール」。
    #   黙って空にすると誰も気づかず、その欄は永久に埋まらない。
    def _mat_ceil(*amounts, picked=None):
        mm = {"adopted": {}, "need_third": {}, "thin": {},
              "ceilings": {"adopted": [{"basis": "INDEPENDENT_MULTI",
                                        "kind": "GAME", "amount": a, "unit": "G",
                                        "benefit": "AT当選"} for a in amounts]}}
        if picked:
            mm["adopted"]["checker_ceiling"] = {"value": {"games": picked}}
        return mm

    t("★★天井が1つなら機械が決めてよい★★",
      (build_checker(_mat_ceil("1000")) or {}).get("normal") == {"ceiling": 1000}
      and not checker_questions(_mat_ceil("1000")))
    #   ★入れるものが何も無ければ checker ごと作らない★ので None になる。
    #   大事なのは「黙って終わらせず、質問が必ず1件出ること」。
    t("★★天井が2つ以上なら決めずに2AIへ質問する★★（黙って空にしない）",
      build_checker(_mat_ceil("800", "1200")) is None
      and len(checker_questions(_mat_ceil("800", "1200"))) == 1)
    t("★★2AIが決めたら、その値を使い質問は消える★★",
      (build_checker(_mat_ceil("800", "1200", picked="1200")) or {})
      .get("normal") == {"ceiling": 1200}
      and not checker_questions(_mat_ceil("800", "1200", picked="1200")))
    t("★★出典に無い値を答えても採らない★★（2AIでも値は発明できない）",
      build_checker(_mat_ceil("800", "1200", picked="999")) is None)
    # ★答えがあっても、いまの候補に無ければ聞き直す★（2026-08-12・依頼163の1）
    #   出典が更新されて候補が変わると、採用もされず質問も消え、
    #   その欄が永久に空のままになる（誰も気づけない）。
    t("★★候補に無い答えなら質問し続ける★★（採用も質問も消えるのを防ぐ）",
      len(checker_questions(_mat_ceil("800", "1200", picked="999"))) == 1)
    t("　候補に有る答えなら質問は消える",
      not checker_questions(_mat_ceil("800", "1200", picked="1200")))

    # ★P-WORLDの機種ページから記事の名前を作る★（2026-08-12）
    #   末尾が数字なので、そのままだと英字で始まる決まりに合わず止まっていた。
    t("★★DMMのURLからは dmm_<機種ID> を作る★★（2026-08-16・台帳#376）",
      slug_from_url("https://p-town.dmm.com/machines/5049") == "dmm_5049"
      and slug_from_url("https://p-town.dmm.com/machines/5049/") == "dmm_5049")
    t("　★別サイトの似たURLでは同じslugを作らない★（ホストと道筋まで見る）",
      raises(lambda: slug_from_url("https://example.com/machines/5049")))
    t("★★P-WORLDのURLからは pw_<機種ID> を作る★★",
      slug_from_url("https://www.p-world.co.jp/machine/database/10513") == "pw_10513"
      and slug_from_url("https://www.p-world.co.jp/machine/database/10513/")
      == "pw_10513")
    t("　メーカー公式のURLは今までどおり",
      slug_from_url("https://www.kitadenshi.co.jp/slot/myjuggler6/") == "myjuggler6")
    t("★★P-WORLDの身元の結び付け方を受け付ける★★",
      "PWORLD_MACHINE_PAGE" in IDENTITY_BINDINGS)
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

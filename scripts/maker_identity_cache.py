"""maker_identity_cache.py — 「この名鑑ページを、この機種の材料に使うか」の控え。

★何を決める器か（2026-08-17・依頼228でv2へ）★
  ★★これは「会社が同じか」を決める器ではない★★
  平和とオリンピアエステート、三洋物産とサンスリーは**別法人**である。
  それなのに v1 は結論を `MATCH`（＝同じ）と書き、ログにも
  「同じメーカーと決めてあります」と出していた。**事実と合っていない**
  （Codex依頼228の指摘5）。決めているのは会社の同一性ではなく、
  **その名鑑ページを、このDMM機種の材料として使うかどうか**でしかない。

  そこで結論を言い換えた:
    ACCEPT_MATERIAL … この名鑑ページを、この機種の材料に使う
    REJECT_MATERIAL … 使わない
  控えには `basis_scope`（何を根拠にしたか）と
  `relationship_verified`（会社の関係を機械で確かめたか＝いまは常に false）を残す。

★なぜ要るか★
  名鑑によって同じ機種のメーカー欄が違う。
    L転生王女 … DMM「オリンピアエステート」／ちょんぼりすた・なな徹「平和」
  これまでは名簿（maker-catalogs.json）に**人が足すまで**その機種が止まっていた。

★会社の関係はどこで見るか（2026-08-17・運営者判断）★
  ★メーカー公式へは通信しない★（運営者が2026-08-16に取りやめ）。
  グループ関係は `maker-catalogs.json` の `maker_relation_group` に入っている。
  **これはリポジトリの中にあり、変更に承認が要るファイル**で、
  根拠（日本遊技機工業組合のグループ会社一覧を人が読んだこと）は
  `_group_why` に書いてある。★機械はそこへも取りに行かない★＝
  名簿を読むだけ。だから新しい通信先が増えない。

★使ってよいのは RELATED のときだけ★（2026-08-17・Codex依頼228の指摘1）
  ①名簿で一致（MATCH）              … そのまま使う（この器は要らない）
  ②関係のありそうな社（RELATED）    … **この器を見る**
  ③どの社か分からない（UNKNOWN）    … ★救わない★＝常に除く
  ④明らかに別の社（MISMATCH）        … 常に除く
  ★UNKNOWN を控えで救ってはいけない★＝名簿に無いだけの**任意の別会社**まで
  同じ扱いになる。同名で別メーカーの機種は実在する
  （パチスロ犬夜叉＝2016年ロデオ／2022年クロスアルファ）。

★答えが出ない状態は保存しない★
  レコードが無い＝「まだ決めていない」（毎回もう一度考える）。

置き場: Documents/uchidokoro/maker_identity_cache.json（リポジトリ外・公開しない）

使い方:
    python scripts/maker_identity_cache.py --list
    python scripts/maker_identity_cache.py --record \\
        --machine-url https://p-town.dmm.com/machines/5086 \\
        --expected olympia_estate --seen 平和 --verdict ACCEPT_MATERIAL \\
        --why <理由> --by claude,codex \\
        --evidence "https://…|逐語引用|directory_observation" \\
        --evidence "https://…|逐語引用|directory_observation"
    python scripts/maker_identity_cache.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import claim_identity as _ci          # noqa: E402
import local_paths as _lp             # noqa: E402

STORE = _lp.doc("maker_identity_cache.json")
# ★v2＝意味を変えた（2026-08-17・依頼228）★
#   「会社が同じか」ではなく「この名鑑ページを材料に使うか」。
#   版を上げるので、v1の控えはそのままでは読めない（＝黙って混ざらない）。
SCHEMA = "maker-identity-cache/v2"
VERDICTS = ("ACCEPT_MATERIAL", "REJECT_MATERIAL")
# ★何を根拠にしたか★＝控えを読む人・監査が、守りの範囲を取り違えないための印。
BASIS_SCOPE = "directory_consensus_only"
# ★「2AIで決めます」を機械の約束にする★（2026-08-14・依頼193のP2）
#   以前は ["foo", "bar"] のような**架空のID2つ**でも「違う2者」だった。
#   ★これは本人確認ではない★＝手で ["claude","codex"] と書くことは防げない。
#   増えたらここだけ直す。
ALLOWED_AGREERS = frozenset({"claude", "codex"})
MIN_QUOTE = 8                          # 逐語引用の最低の長さ
# ★引用は「事実の欄の写し」までにとどめる★（2026-08-17・Codex依頼229の指摘3）
#   なな徹の規約（第7条1項(1)「入手したコンテンツの複製」）について、
#   運営者は2026-08-17に**「機種名・メーカー欄・導入日という事実の欄だけ・
#   1件50字前後・記事本文や表は保存しない」という前提で**「続ける」と判断した。
#   ★その前提をコードで守らせる★＝以前は下限しか無く、記事本文を丸ごと
#   引用として保存できた（判断の前提を実装が保証していなかった）。
MAX_QUOTE = 120                        # 逐語引用の最大の長さ
MAX_EVIDENCE = 4                       # 根拠の件数の上限
# ★根拠は名鑑の観測だけ★（2026-08-17・依頼228）
#   `official_relationship`（メーカー公式の会社関係ページ）は**削除した**。
#   運営者が「メーカー公式は使わない」と決めたため（止めずに消す）。
KINDS = ("directory_observation",)
# ★材料に使うと決めるのに要る、独立した名鑑の数★
MIN_DIRECTORIES = 2


class CacheError(Exception):
    """控えに関する異常（★迷ったら記録しない★）。"""


def _empty() -> dict:
    return {"schema_version": SCHEMA, "machines": {}}


def load() -> dict:
    """控えを読む。★壊れていたら黙って「無い」ことにしない★"""
    if not os.path.exists(STORE):
        return _empty()
    try:
        with open(STORE, encoding="utf-8") as f:
            got = json.load(f)
    except Exception as e:              # noqa: BLE001
        raise CacheError(f"控えを読めません（直すまで使いません）: {e}")
    if not isinstance(got, dict) or got.get("schema_version") != SCHEMA:
        raise CacheError(
            f"控えの版が違います（{got.get('schema_version') if isinstance(got, dict) else '?'}）")
    if not isinstance(got.get("machines"), dict):
        raise CacheError("控えの中身が壊れています（machines が組ではありません）")
    # ★読むときも中身を確かめる★（2026-08-14・依頼190のP1）
    #   書くときだけ検査していたので、手で書き足したレコードが
    #   **根拠も判断者も無いまま信用される**経路があった。
    # ★出どころの登録簿は1回だけ読む★（レコードごとに読み直さない）
    _reg = None
    if any(rows for rows in got["machines"].values()):
        import source_lineage as _sl
        try:
            _reg = _sl.load_registry()
        except Exception as e:              # noqa: BLE001
            raise CacheError(f"出典の登録簿を読めません（控えを使いません）: {e}")
    for slug, rows in got["machines"].items():
        if not isinstance(rows, list):
            raise CacheError(f"控えが壊れています（{slug} が並びではありません）")
        for rec in rows:
            _check_record(slug, rec, _reg)
    return got


_DATE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}$")


def _has_core(haystack: str, needle: str) -> bool:
    """★表記ゆれをならしてから、含まれているか見る★

    機種名は名鑑ごとに空白・記号の入れ方が違う（「L転生王女と天才令嬢の魔法革命」
    ／「L転生王女と天才令嬢の 魔法革命」）ので、同定に使っている芯の作り方
    （claim_identity）にそろえる。★ここで新しい正規表現を書かない★
    """
    n = _ci.normalize_core(str(needle or ""))
    if not n:
        return False
    return n in _ci.normalize_core(str(haystack or ""))


def _check_record(slug: str, rec, reg=None) -> None:
    """1件ぶんの控えを確かめる（★読むときも書くときも同じ物差し★）。"""
    if not isinstance(rec, dict):
        raise CacheError(f"控えが壊れています（{slug}）")
    if rec.get("verdict") not in VERDICTS:
        raise CacheError(f"控えの結論が不正です（{slug}）: {rec.get('verdict')!r}")
    for k in ("expected", "seen", "why", "decided_at"):
        if not str(rec.get(k) or "").strip():
            raise CacheError(f"控えに「{k}」がありません（{slug}）")
    by = rec.get("agreed_by")
    # ★表記ゆれを「違う2者」にしない★（2026-08-14・依頼192のP2）
    #   ["codex", " codex"] や ["codex", None] が2者として通っていた。
    if not isinstance(by, list) or not all(
            isinstance(x, str) and x.strip() for x in by):
        raise CacheError(f"控えの判断者が不正です（{slug}）: {by!r}")
    ids = {x.strip().casefold() for x in by}
    if not ids <= ALLOWED_AGREERS:
        raise CacheError(
            f"控えに知らない判断者がいます（{slug}）: "
            f"{sorted(ids - ALLOWED_AGREERS)}／★{sorted(ALLOWED_AGREERS)} だけです★")
    if len(ids) < 2:
        raise CacheError(f"控えの判断者が足りません（{slug}）: {by!r}"
                         "／★違う2者で決めます★")
    ev = rec.get("evidence")
    if not isinstance(ev, list) or not ev:
        raise CacheError(f"控えに根拠がありません（{slug}）")
    if len(ev) > MAX_EVIDENCE:
        raise CacheError(f"控えの根拠が多すぎます（{slug}）: {len(ev)}件"
                         f"／★{MAX_EVIDENCE}件までです（写しは最小限に）★")
    kinds = set()
    for e in ev:
        if not isinstance(e, dict):
            raise CacheError(f"控えの根拠が組ではありません（{slug}）")
        if not str(e.get("url") or "").startswith(("http://", "https://")):
            raise CacheError(f"控えの根拠のURLが不正です（{slug}）: {e.get('url')!r}")
        q1 = " ".join(str(e.get("quote") or "").split())
        if len(q1) < MIN_QUOTE:
            raise CacheError(f"控えの逐語引用が短すぎます（{slug}）")
        # ★長すぎる引用は受け取らない★（2026-08-17・Codex依頼229の指摘3）
        #   規約の判断は「事実の欄の写しにとどまる」という前提で出ている。
        if len(q1) > MAX_QUOTE:
            raise CacheError(
                f"控えの逐語引用が長すぎます（{slug}）: {len(q1)}字"
                f"／★{MAX_QUOTE}字までです＝機種名・メーカー欄・導入日の"
                "欄だけを写します（記事本文や表は写しません）★")
        if e.get("kind") not in KINDS:
            raise CacheError(f"控えの根拠の種類が不正です（{slug}）: {e.get('kind')!r}")
        kinds.add(e.get("kind"))
    if rec["verdict"] != "ACCEPT_MATERIAL":
        return
    # ---------------- ここから下は「材料に使う」と決めた控えだけの検査 ----------
    # ★①どの機種のページかを、控え自身が名乗る★（2026-08-17・依頼228の指摘2）
    #   v1は「登録済みの名鑑のホストである」ことしか見ていなかったので、
    #   **同じ名鑑の別機種ページ・関連記事・同名別メーカー機のページ**でも
    #   通った。機種名と導入日を控えに持たせ、根拠がその機種を指すか見る。
    for k in ("machine_name", "release_date"):
        if not str(rec.get(k) or "").strip():
            raise CacheError(f"控えに「{k}」がありません（{slug}）"
                             "／★どの機種のページかを名乗らせます★")
    if not _DATE.match(str(rec.get("release_date"))):
        raise CacheError(f"控えの導入日は YYYY-MM-DD で書きます（{slug}）: "
                         f"{rec.get('release_date')!r}")
    # ★②逐語引用そのものに、機種名とメーカー欄が入っていること★
    #   ページのどこかにあるだけでは足りない（別機種の欄でも通ってしまう）。
    for e in ev:
        q = str(e.get("quote") or "")
        if not _has_core(q, str(rec.get("machine_name"))):
            raise CacheError(
                f"根拠の逐語引用に機種名が入っていません（{slug}）: "
                f"{q[:40]}／★その機種のページだと示す引用にします★")
        if not _has_core(q, str(rec.get("seen"))):
            raise CacheError(
                f"根拠の逐語引用にメーカー欄の表記が入っていません（{slug}）: "
                f"{q[:40]}／★「{rec.get('seen')}」を含む引用にします★")
    # ★③独立した名鑑が2つ以上★（2026-08-17・依頼228）
    #   ★票の数は source_lineage.independent() だけで決める★
    #   （自前で len() すると共同制作の組をまとめ忘れる＝監査39が見張る）
    import source_lineage as _sl
    try:
        per = [_sl.vote_key_of_url(str(e.get("url")), reg) for e in ev]
    except Exception as e:                 # noqa: BLE001
        raise CacheError(f"根拠の出どころを数えられません（{slug}）: {e}")
    # ★同じ発行者から2つ以上写さない★（2026-08-17・Codex依頼229の指摘3）
    #   写しを最小限にするため。票の数え方（independent）とは別の目的。
    if len(set(per)) != len(per):
        raise CacheError(f"同じ名鑑から2件以上の引用を控えています（{slug}）"
                         "／★1つの名鑑につき1件までです★")
    keys = set(per)
    if _sl.independent(keys, reg) < MIN_DIRECTORIES:
        raise CacheError(
            f"材料に使うと決めるには独立した名鑑が{MIN_DIRECTORIES}つ要ります"
            f"（{slug}）: いまは {_sl.independent(keys, reg)}")
    # ★④守りの範囲を控え自身に書かせる★（2026-08-17・Codex依頼228の指摘5）
    #   これを読み落として「会社が同じと確かめた」と誤読されないようにする。
    if rec.get("basis_scope") != BASIS_SCOPE:
        raise CacheError(f"控えの basis_scope は {BASIS_SCOPE} です（{slug}）: "
                         f"{rec.get('basis_scope')!r}")
    if rec.get("relationship_verified") is not False:
        raise CacheError(
            f"控えの relationship_verified は false です（{slug}）"
            "／★会社の関係は機械で確かめていません★")


def save(got: dict) -> None:
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = f"{STORE}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(got, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STORE)


def key_of(seen: str) -> str:
    """メーカー欄の表記から、比べるための芯を作る。"""
    return _ci.normalize_core(str(seen or "")).replace("株式会社", "")


def verdict_for(slug: str, expected: str, seen: str, store=None,
                fetch=None, material_url: str = "",
                machine_name: str = "", release_date: str = ""):
    """この機種について前に決めた結論（無ければ None）。

    ★完全一致で引く★＝(機種・期待する社・名鑑の表記の芯) の3つ。

    ★★「使う」と答えるには、対象そのものと結び付いていること★★
      （2026-08-17・Codex依頼229の指摘1）
      前は鍵が (機種・期待する社・表記) の3つだけだったので、
        ①別機種の機種名・導入日・根拠を手で書いた控えでも、
          読むときにDMMと突き合わせずに「使う」を返せた
        ②いま採否を決めようとしているURLを渡していないので、
          **控えの根拠に入っていない別の名鑑ページまで**「使う」になった
      そこで `ACCEPT_MATERIAL` を返すのは、
        ・`material_url` が控えの根拠のURLに含まれる
        ・控えの `machine_name` / `release_date` が、DMMで確かめた値と一致する
      ときだけにした。★どれか1つでも渡されていなければ答えない★（fail-closed）。
      `REJECT_MATERIAL`（使わない側）は今までどおり、鍵が合えば返す。

    ★「材料に使う」として使う時だけ、根拠が実在するか確かめ直す★
      （2026-08-14・依頼192のP1）書くときに照合しても、
      **控えは手で書き足せるただのファイル**なので、
      形だけ整った偽の根拠で `ACCEPT_MATERIAL` を作れてしまう。
      使う直前に取り直せば、それが通らない。
      ★取れない・引用が見つからないなら「決めていない」と同じ扱い★
      （None を返す＝もう一度2AIへ回る。fail-closed）
      `REJECT_MATERIAL` は「使わない」側なので取り直さない（遅くする意味がない）。
    """
    if not slug or not expected or not seen:
        return None
    got = store if store is not None else load()
    k = key_of(seen)
    for rec in (got.get("machines") or {}).get(slug) or []:
        if rec.get("expected") != expected or key_of(rec.get("seen")) != k:
            continue
        v = rec.get("verdict")
        if v != "ACCEPT_MATERIAL":
            return v
        # ★①いま決めようとしているページが、控えの根拠そのものか★
        urls = {str(e.get("url") or "").rstrip("/")
                for e in (rec.get("evidence") or [])}
        if not material_url or str(material_url).rstrip("/") not in urls:
            return None
        # ★②控えが名乗る機種が、DMMで確かめた機種と同じか★
        if not machine_name or not release_date:
            return None
        if not _has_core(str(rec.get("machine_name") or ""), machine_name) \
                or str(rec.get("release_date") or "") != str(release_date):
            return None
        # ★③根拠が今もそのページに実在するか（毎回取り直す）★
        try:
            verify_evidence(rec.get("evidence") or [], fetch, expected, rec)
        except CacheError:
            return None
        return v
    return None


def _host_of(url: str) -> str:
    """URLからホストを取り出す（★文字列の前方一致で見ない★）。"""
    import urllib.parse
    return (urllib.parse.urlsplit(str(url or "")).hostname or "").lower()


# ★official_hosts() は削除しました★（2026-08-17・依頼228／運営者判断）
#   メーカー公式の会社関係ページを機械が取りに行く仕組みでした。
#   会社の関係は maker-catalogs.json の maker_relation_group（承認が要る
#   リポジトリ内のファイル）で見ます。根拠は同ファイルの _group_why に、
#   日本遊技機工業組合のグループ会社一覧を**人が読んだ記録**として残しています。
#   ★止めずに消す★＝残すと「まだ生きている」と誤読され、実際に誤報しました。


def directory_of(host: str) -> dict:
    """★そのホストの名鑑の設定★（ACTIVEのものだけ・無ければ例外）"""
    import directory_index as _di
    import safe_json as _sj
    import source_lineage as _sl
    h = str(host or "").lower()
    try:
        reg = _sl.load_registry()
        cats = _sj.read_json(_di.CATALOGS, expect=dict).get("directories") or {}
    except Exception as e:                 # noqa: BLE001
        raise CacheError(f"名鑑の登録簿を読めません（根拠を確かめられません）: {e}")
    pubs = {pid: p for pid, p in (reg.get("publishers") or {}).items()
            if p.get("status") == "ACTIVE"}
    for c in cats.values():
        if not isinstance(c, dict) or c.get("status") != "ACTIVE":
            continue
        p = pubs.get(str(c.get("publisher_id") or ""))
        for ch in (p or {}).get("canonical_hosts") or []:
            if str(ch).strip().lower() == h:
                return c
    raise CacheError(f"名鑑として登録されていないサイトです: {host}"
                     "／★観測の根拠は登録済みの名鑑から採ります★")


def directory_hosts() -> set:
    """★名鑑として登録されているホスト★（directory-catalogs.json の ACTIVE）

    ★新しい名簿は作らない★＝すでにある名鑑の登録簿から導く。
    """
    import directory_index as _di
    import safe_json as _sj
    import source_lineage as _sl
    # ★読めないときも CacheError にする★（2026-08-14・依頼194のP2）
    #   ここで別の例外が出ると、verdict_for が拾えず**新台の処理ごと落ちる**。
    #   守りたいのは「根拠を確かめられないなら使わない」であって、
    #   その晩の処理を全部止めることではない。
    try:
        reg = _sl.load_registry()
        pubs = {pid: p for pid, p in (reg.get("publishers") or {}).items()
                if p.get("status") == "ACTIVE"}
        cats = _sj.read_json(_di.CATALOGS, expect=dict).get("directories") or {}
    except CacheError:
        raise
    except Exception as e:                 # noqa: BLE001
        raise CacheError(f"名鑑の登録簿を読めません（根拠を確かめられません）: {e}")
    out = set()
    for c in cats.values():
        if not isinstance(c, dict) or c.get("status") != "ACTIVE":
            continue
        p = pubs.get(str(c.get("publisher_id") or ""))
        for h in (p or {}).get("canonical_hosts") or []:
            if str(h).strip():
                out.add(str(h).strip().lower())
    if not out:
        raise CacheError("名鑑の登録簿が空です（根拠を確かめられません）")
    return out


def check_evidence_source(e: dict, expected: str) -> None:
    """★根拠のURLが、その種類にふさわしい出どころか★（依頼192のP1）

    directory_observation … 登録済みの名鑑の、★機種ページ★

    ★ホストだけでは足りない★（2026-08-17・Codex依頼228の指摘4）
      v1は「登録済みの名鑑のホストか」しか見ていなかったので、
      同じ名鑑の**一覧ページ・特集記事・別機種のページ**でも通った。
      名鑑ごとに決めてある機種ページの形（machine_page_pattern）まで見る。
    """
    url = str(e.get("url") or "")
    if not url.startswith("https://"):
        raise CacheError(f"根拠は https のページだけです: {url}")
    host = _host_of(url)
    if not host:
        raise CacheError(f"根拠のURLからホストを取れません: {url}")
    kind = e.get("kind")
    if kind != "directory_observation":
        raise CacheError(f"根拠の種類が不正です: {kind!r}"
                         f"／★いまの種類は {'/'.join(KINDS)} だけです★")
    # ★「登録済みの発行者」ではなく「登録済みの名鑑」に限る★
    #   （2026-08-14・依頼193のP2）source-registry には解析サイトや
    #   メーカー公式も ACTIVE で載っているので、それだけで見ると
    #   **名鑑でないページを「名鑑での観測」として渡せた**＝役割の分離が崩れる。
    conf = directory_of(host)
    pat = str(conf.get("machine_page_pattern") or "")
    if not pat:
        # ★決めていない名鑑は使わない★（fail-closed）
        raise CacheError(
            f"この名鑑には機種ページの形が決めてありません: {host}"
            "／★directory-catalogs.json の machine_page_pattern に書きます★")
    import re as _re
    if not _re.match(pat, url):
        raise CacheError(
            f"その名鑑の機種ページではありません: {url}"
            f"／★形: {pat}★（一覧・特集・別機種のページは根拠にしません）")


def date_forms(iso: str) -> list:
    """★同じ1つの日付を、よくある書き方に直すだけ★（2026-08-17・依頼228）

    ★これは「サイトごとの場合分け」ではない★＝相手の作りを読むのではなく、
      **こちらが持っている1つの日付**を標準的な書式で並べるだけ。
      実データ: ちょんぼりすた「2026年10月5日」／なな徹「2026/10/5」
    """
    m = _DATE.match(str(iso or ""))
    if not m:
        return []
    y, mo, d = str(iso)[:4], int(str(iso)[5:7]), int(str(iso)[8:10])
    out = []
    for sep in ("/", ".", "-"):
        out.append(f"{y}{sep}{mo}{sep}{d}")
        out.append(f"{y}{sep}{mo:02d}{sep}{d:02d}")
    out.append(f"{y}年{mo}月{d}日")
    out.append(f"{y}年{mo:02d}月{d:02d}日")
    return out


def verify_evidence(evidence: list, fetch=None, expected: str = "",
                    rec=None) -> None:
    """★根拠の逐語引用が、本当にそのページにあるか確かめる★

    ★なぜ要るか（2026-08-14・依頼190のP1）★
      形（URLらしい文字列・8文字以上の引用）だけを見ていたので、
      **URLも引用も「言うだけ」で通った**。
      当サイトの原則は「言うだけでは通さない＝そのページに実在する逐語を
      根拠に出させ、機械が確かめる」なので、ここが抜けていると
      根拠の無い MATCH を控えられてしまう。

    ★取れなければ控えない★（fail-closed）＝取得できない・引用が見つからない
      ときは例外にする。「たぶん合っている」で通さない。
    """
    if fetch is None:
        import new_machine_watch as _w

        def fetch(u):
            # ★何のために取りに行くかを名乗る★（2026-08-17）
            #   8/16に「名乗らなければ通さない」形にしたとき、ここを
            #   直し忘れて**必ず例外**になっていた（実際に新台が止まった）。
            #   用途＝メーカーの同定（逐語引用が本当にそのページにあるか確かめる）
            with _w.fetching("maker_identity"):
                return _w._get(u)
    import new_machine_watch as _w
    for e in evidence:
        url = str(e.get("url") or "")
        if expected:
            check_evidence_source(e, expected)
        try:
            html = fetch(url)
        except Exception as ex:            # noqa: BLE001
            raise CacheError(f"根拠のページを取得できません（{url}）: "
                             f"{str(ex)[:80]}")
        # ★転送された先も同じ許可の中か見る★（依頼192のP1）
        #   許可したURLから許可外へ飛ばされたら、それは別の出どころ。
        # ★ホストが同じでも必ず見る★（2026-08-14・依頼193のP2）
        #   以前は「ホストが変わったときだけ」だったので、
        #   同じ社の https → http という降格が素通りした
        #   （＝そのあとの本文は通信経路で書き換えられうる）。
        fin = _w.LAST_FINAL_URL.get("url")
        if expected and fin:
            check_evidence_source(dict(e, url=fin), expected)
        body = " ".join(_w._visible_text(html or "").split())
        q = " ".join(str(e.get("quote") or "").split())
        if q not in body:
            raise CacheError(
                f"根拠の逐語引用がそのページに見つかりません（{url}）: "
                f"{q[:40]}／★写した文だけを根拠にします★")
        # ★★メーカー欄そのものを取り出して比べる★★
        #   （2026-08-17・Codex依頼229の指摘2）
        #   前は「seen という文字がページのどこかにあるか」しか見ていなかった。
        #   それだと、メーカー欄は別の社なのに本文のどこかに「平和」と
        #   書いてあるページでも「平和表記の2件目」に数えられた。
        #   ★新しい読み取りを書かない★＝名鑑のメーカー欄を読む役は
        #   model_code_lookup.extract_maker_name にあるので、そこを通す。
        seen = str((rec or {}).get("seen") or "")
        if seen:
            import model_code_lookup as _mcl
            mk = _mcl.extract_maker_name(html or "")
            if not mk:
                raise CacheError(
                    f"そのページのメーカー欄を読めません（{url}）"
                    "／★読めないものを「確かめた」ことにしません★")
            if key_of(mk) != key_of(seen):
                raise CacheError(
                    f"そのページのメーカー欄が控えと違います（{url}）: "
                    f"ページ「{mk[:20]}」／控え「{seen[:20]}」")
        # ★そのページが本当にこの機種のページか★（2026-08-17・運営者判断）
        #   機種名は引用の中で見ている（_check_record）。ここでは導入日を
        #   ページ本文で見る。同名で別メーカーの機種は導入年が違うので、
        #   ここが効く（パチスロ犬夜叉＝2016年／2022年）。
        #   ★弱い検査だと分かっていて残している★（Codex依頼229の指摘2）＝
        #   名鑑ごとの「導入日の欄」を読む役はまだ無く、それを書くと
        #   サイトごとの場合分けになる（当サイトが避けている形）。
        #   本人性の主たる担保は、上のメーカー欄の一致と、引用に機種名が
        #   入っていること、そして本体の同定（identity_ok）。
        want = date_forms(str((rec or {}).get("release_date") or ""))
        if want and not any(w in body for w in want):
            raise CacheError(
                f"そのページに導入日が見つかりません（{url}）: "
                f"{want[0]} など／★別の機種のページの可能性があります★")


def remember(slug: str, expected: str, seen: str, verdict: str,
             why: str, by: list, evidence: list, decided_at: str,
             machine_name: str = "", release_date: str = "",
             store=None, fetch=None) -> dict:
    """結論を控える。★根拠が無ければ受け取らない★

    ★逐語引用は実際にそのページから取ってきて照合する★（依頼190のP1）
    ★機種名と導入日はDMMの機種ページから取る★（呼ぶ側に名乗らせない）
    """
    if verdict not in VERDICTS:
        raise CacheError(f"結論は {'/'.join(VERDICTS)} のどちらかです: {verdict!r}")
    for k, v in (("slug", slug), ("expected", expected), ("seen", seen),
                 ("why", why), ("decided_at", decided_at)):
        if not str(v or "").strip():
            raise CacheError(f"「{k}」が要ります")
    if not isinstance(by, list) or len(by) < 2:
        raise CacheError("判断した者を2つ以上書きます（例: claude, codex）")
    if not isinstance(evidence, list) or not evidence:
        raise CacheError("根拠（URLと逐語引用）が要ります")
    for e in evidence:
        if not isinstance(e, dict):
            raise CacheError("根拠は組（辞書）で書きます")
        if not str(e.get("url") or "").strip():
            raise CacheError("根拠にURLが要ります")
        q = " ".join(str(e.get("quote") or "").split())
        if len(q) < MIN_QUOTE:
            raise CacheError(f"逐語引用は{MIN_QUOTE}文字以上で書きます: {q!r}")
        if e.get("kind") not in KINDS:
            raise CacheError(f"根拠の種類は {'/'.join(KINDS)} のどれかです: "
                             f"{e.get('kind')!r}")
    rec = {"expected": expected, "seen": seen, "verdict": verdict,
           "why": why, "evidence": evidence, "agreed_by": by,
           "decided_at": decided_at}
    if verdict == "ACCEPT_MATERIAL":
        rec.update({"machine_name": machine_name,
                    "release_date": release_date,
                    "basis_scope": BASIS_SCOPE,
                    "relationship_verified": False})
    # ★書く前に、読むときと同じ物差しを通す★（順番を変えない）
    #   先に形を確かめてから通信する＝形が違う控えのために外へ出ない。
    _check_record(slug, rec)
    verify_evidence(evidence, fetch, expected, rec)
    got = store if store is not None else load()
    rows = got.setdefault("machines", {}).setdefault(slug, [])
    k = key_of(seen)
    for i, old in enumerate(rows):
        if old.get("expected") == expected and key_of(old.get("seen")) == k:
            rows[i] = rec                # ★同じ組は上書き（増やさない）★
            break
    else:
        rows.append(rec)
    if store is None:
        save(got)
    return rec


def forget(slug: str, expected: str, seen: str, store=None) -> bool:
    """控えを消す（判断を取り消すとき）。"""
    got = store if store is not None else load()
    rows = (got.get("machines") or {}).get(slug) or []
    k = key_of(seen)
    left = [r for r in rows
            if not (r.get("expected") == expected and key_of(r.get("seen")) == k)]
    if len(left) == len(rows):
        return False
    if left:
        got["machines"][slug] = left
    else:
        got["machines"].pop(slug, None)
    if store is None:
        save(got)
    return True


# ---------------------------------------------------------------- selftest

# ★試験も本物の名鑑・本物の機種ページの形を使う★（2026-08-17・依頼228）
#   架空のホスト（x.test）では `directory_of` も `machine_page_pattern` も
#   通らない。**関所を素通りする偽物を使うと、関所の試験にならない**。
#   取ってくる役だけを差し替える（通信はしない）。
_MN = "L転生王女と天才令嬢の魔法革命"
_SEEN = "平和"
_EXPECTED = "olympia_estate"
_REL = "2026-10-05"
_C = "https://chonborista.com/slot/orinpia-slot/264134/"    # 名鑑①の機種ページ
_N = "https://nana-press.com/kaiseki/machine/1233/"         # 名鑑②の機種ページ
_LIST = "https://chonborista.com/slot/orinpia-slot/"        # 一覧（機種ページでない）
_KIT = "https://www.kitadenshi.co.jp/company/"              # 名鑑ではない登録先
_QC = f"機種名 {_MN} メーカー {_SEEN}"
_QN = f"機種名 {_MN} メーカー {_SEEN} 導入日 2026/10/5"


def _rec(**kw) -> dict:
    """試験用の、正しい形の控え1件。"""
    base = {"expected": _EXPECTED, "seen": _SEEN, "verdict": "ACCEPT_MATERIAL",
            "why": "理由", "agreed_by": ["claude", "codex"],
            "decided_at": "2026-08-17", "machine_name": _MN,
            "release_date": _REL, "basis_scope": BASIS_SCOPE,
            "relationship_verified": False,
            "evidence": [{"url": _C, "quote": _QC,
                          "kind": "directory_observation"},
                         {"url": _N, "quote": _QN,
                          "kind": "directory_observation"}]}
    base.update(kw)
    return base


def _bad_load() -> bool:
    """★手で書き足した控えを、読むときに弾けるか★（試験用）

    ★ここが v1 で足りなかったところ★＝書くときだけ検査していたので、
      手で書き足したレコードが根拠も判断者も無いまま信用される経路があった。
    """
    import copy
    bads = [
        {"verdict": "ACCEPT_MATERIAL", "expected": _EXPECTED, "seen": _SEEN},
        _rec(agreed_by=["claude"]),                    # 1人だけ
        _rec(agreed_by=["claude", "claude"]),          # 同じ人を2回
        _rec(evidence=[]),                             # 根拠なし
        _rec(evidence=[{"url": _C, "quote": _QC,
                        "kind": "directory_observation"}]),   # 名鑑1つだけ
        _rec(machine_name=""),                         # どの機種か名乗らない
        _rec(release_date=""),                         # 導入日を名乗らない
        _rec(release_date="2026/10/05"),               # 日付の形が違う
        _rec(basis_scope="whatever"),                  # 守りの範囲を偽る
        _rec(relationship_verified=True),              # 会社の関係を確かめた、と偽る
        # ★写しが長すぎる／多すぎる／同じ名鑑から2件★（依頼229の指摘3）
        _rec(evidence=[{"url": _C, "quote": _QC + "。" + "解析情報。" * 30,
                        "kind": "directory_observation"},
                       {"url": _N, "quote": _QN,
                        "kind": "directory_observation"}]),
        _rec(evidence=[{"url": _C, "quote": _QC,
                        "kind": "directory_observation"},
                       {"url": "https://chonborista.com/slot/x/2/",
                        "quote": _QC, "kind": "directory_observation"}]),
        _rec(evidence=[{"url": _C, "quote": _QC,
                        "kind": "directory_observation"}] * 5),
        # 引用に機種名が入っていない（別機種の欄でも通っていた）
        _rec(evidence=[{"url": _C, "quote": "メーカー 平和 の機種一覧です",
                        "kind": "directory_observation"},
                       {"url": _N, "quote": "メーカー 平和 の解析一覧です",
                        "kind": "directory_observation"}]),
        # 引用にメーカー欄の表記が入っていない
        _rec(evidence=[{"url": _C, "quote": f"機種名 {_MN} の解析",
                        "kind": "directory_observation"},
                       {"url": _N, "quote": f"機種名 {_MN} の天井",
                        "kind": "directory_observation"}]),
    ]
    reg = None
    try:
        import source_lineage as _sl
        reg = _sl.load_registry()
    except Exception:                      # noqa: BLE001
        return False
    for bad in bads:
        g = copy.deepcopy(bad)
        try:
            _check_record("dmm_5086", g, reg)
            return False                   # ★通ってしまった＝不合格★
        except CacheError:
            pass
    return True


def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅ " if cond else "❌ ") + name)

    st = _empty()

    # ★名鑑のページと同じ形で作る★（2026-08-17・Codex依頼229の指摘2）
    #   メーカー欄は「行の頭がメーカー」で読み取る（extract_maker_name）ので、
    #   1行にべた書きした偽ページでは**本番の読み取りを通らない**。
    #   ★関所を通る形の偽物でなければ、関所の試験にならない★
    def _page(maker=_SEEN, day="2026年10月5日", name=_MN):
        return (f"<div>機種名 {name}</div>"
                f"<div>メーカー {maker}</div>"
                f"<div>導入日 {day}</div>")

    _pages = {
        _C: _page(),
        _N: _page(day="2026/10/5"),
        _LIST: _page(),
        _KIT: _page(),
    }

    def _w_last(u):
        """取ってくる役が「最後に着いたURL」を控える（本物と同じ形）。"""
        import new_machine_watch as _w
        _w.LAST_FINAL_URL["url"] = u
        return u

    def _fetch(u):
        if u not in _pages:
            raise RuntimeError("404")
        _w_last(u)                         # ★転送なし＝最後のURLは自分自身★
        return _pages[u]

    ev = _rec()["evidence"]

    def _ask(slug="dmm_5086", expected=_EXPECTED, seen=_SEEN, store=None,
             fetch=None, url=_C, name=_MN, day=_REL):
        """★本番と同じ渡し方で引く★（対象URL・DMMで確かめた機種名と導入日）"""
        return verdict_for(slug, expected, seen,
                           st if store is None else store,
                           _fetch if fetch is None else fetch,
                           material_url=url, machine_name=name,
                           release_date=day)

    def _ok(**kw):
        base = dict(slug="dmm_5086", expected=_EXPECTED, seen=_SEEN,
                    verdict="ACCEPT_MATERIAL", why="理由",
                    by=["claude", "codex"], evidence=ev,
                    decided_at="2026-08-17", machine_name=_MN,
                    release_date=_REL, store=st, fetch=_fetch)
        base.update(kw)
        try:
            remember(**base)
            return True
        except CacheError:
            return False

    t("★★独立2名鑑の根拠つきなら控えられる★★", _ok())
    t("　控えた結論を引ける", _ask(seen="株式会社平和") == "ACCEPT_MATERIAL")

    # ★★★2026-08-17・Codex依頼229の指摘1★★★
    #   控えの鍵が (機種・期待する社・表記) の3つだけだったので、
    #   ①別機種の名前と日付を手で書いた控えでも「使う」を返せた
    #   ②いま決めようとしているURLを渡していないので、
    #     控えの根拠に入っていない別のページまで「使う」になった
    t("★★★控えの根拠に入っていないページには効かない★★★"
      "（前は採否対象のURLを渡していなかったので、同じ名鑑の別ページまで通った）",
      _ask(url="https://chonborista.com/slot/orinpia-slot/999999/") is None)
    t("　（対照）控えの根拠そのものなら通る",
      _ask(url=_C) == "ACCEPT_MATERIAL" and _ask(url=_N) == "ACCEPT_MATERIAL")
    t("★★★控えが名乗る機種がDMMと違えば効かない★★★"
      "（別機種の機種名・導入日を手で書いた控えを、読むときに落とす）",
      _ask(name="L別の機種") is None and _ask(day="2026-11-02") is None)
    t("★★対象を渡さなければ答えない★★（fail-closed）",
      _ask(url="") is None and _ask(name="") is None and _ask(day="") is None)

    # ★★使うときにも根拠を取り直す（2026-08-14・依頼192のP1）★★
    t("★★根拠のページが取れなくなったら材料に使わない★★"
      "／手で書き足した偽の根拠を、使う直前に落とす",
      _ask(fetch=lambda u: (_ for _ in ()).throw(RuntimeError("404"))) is None)
    t("　（対照）取り直せるうちは今までどおり使える",
      _ask() == "ACCEPT_MATERIAL")
    t("　引用が消えていたら使わない",
      _ask(fetch=lambda u: "<p>ページが作り替えられました</p>") is None)

    # ★★2026-08-17・依頼228で足した守り★★
    t("★★導入日がそのページに無ければ材料に使わない★★"
      "／同名で別メーカーの機種は導入年が違う（犬夜叉＝2016年／2022年）",
      _ask(fetch=lambda u: (_w_last(u), _page(day="未定"))[1]
           if u == _C else (_w_last(u), _pages[u])[1]) is None)
    # ★★★2026-08-17・Codex依頼229の指摘2★★★
    #   前は「seen という文字がページのどこかにあるか」しか見ていなかったので、
    #   メーカー欄が別の社でも、本文のどこかに「平和」とあれば通った。
    t("★★★メーカー欄が別の社なら、本文に同じ文字があっても使わない★★★"
      "（前はページのどこかに文字があれば「2件目の名鑑」に数えられた）",
      _ask(fetch=lambda u: (_w_last(u), _page(maker="サミー")
                            + "<div>関連: 平和 の機種はこちら</div>")[1]
           if u == _C else (_w_last(u), _pages[u])[1]) is None)
    t("　メーカー欄を読めないページも使わない（読めない＝確かめていない）",
      _ask(fetch=lambda u: (_w_last(u), f"<div>{_QC} 導入日 2026年10月5日</div>")[1]
           if u == _C else (_w_last(u), _pages[u])[1]) is None)
    t("★★名鑑1つだけでは控えられない★★（独立2名鑑が要る）",
      not _ok(evidence=[ev[0]], slug="dmm_1"))
    t("　（対照）2つあれば通る＝厳しすぎるのではない", _ok(slug="dmm_2"))
    t("★★同じ名鑑の2ページでは2つに数えない★★",
      not _ok(evidence=[ev[0], dict(ev[0], url=_LIST)], slug="dmm_same"))
    t("★★その名鑑の機種ページでないURLは根拠にできない★★"
      "／一覧・特集・別機種のページ",
      not _ok(evidence=[dict(ev[0], url=_LIST), ev[1]], slug="dmm_list"))
    t("★★引用に機種名が入っていなければ控えられない★★",
      not _ok(evidence=[dict(ev[0], quote="メーカー 平和 の一覧"),
                        dict(ev[1], quote="メーカー 平和 の解析")],
              slug="dmm_noname"))
    t("★★引用にメーカー欄の表記が入っていなければ控えられない★★",
      not _ok(evidence=[dict(ev[0], quote=f"機種名 {_MN} の解析"),
                        dict(ev[1], quote=f"機種名 {_MN} の天井")],
              slug="dmm_nomaker"))
    t("★★「公式の関係」という根拠はもう受け取らない★★"
      "／メーカー公式を見るのをやめた（運営者判断・2026-08-17）",
      not _ok(evidence=[ev[0], dict(ev[1], kind="official_relationship")],
              slug="dmm_off"))
    t("　根拠の種類を勝手に作れない",
      not _ok(evidence=[ev[0], dict(ev[1], kind="でっちあげ")],
              slug="dmm_kind"))

    # ★★★2026-08-17・Codex依頼229の指摘3★★★
    #   運営者の判断（なな徹の規約・「事実の欄だけ・短い抜粋なので続ける」）を
    #   コードで守らせる。前は下限しか無く、記事本文を丸ごと控えられた。
    _C2 = "https://chonborista.com/slot/orinpia-slot/264135/"
    _pages[_C2] = _page()
    _long = _QC + "。" + "この機種の解析情報をお届けします。" * 12
    _pages[_C] = _page() + f"<div>{_long}</div>"
    t("★★★長すぎる引用は控えられない★★★"
      "（記事本文を丸ごと写せた＝規約の判断の前提が守られていなかった）",
      len(_long) > MAX_QUOTE
      and not _ok(evidence=[dict(ev[0], quote=_long), ev[1]], slug="dmm_long"))
    _pages[_C] = _page()
    t("　（対照）欄の写しの長さなら通る＝厳しすぎるのではない",
      len(_QC) <= MAX_QUOTE and _ok(slug="dmm_short"))
    t("★★同じ名鑑から2件は控えない★★（写しは最小限に）",
      not _ok(evidence=[ev[0], dict(ev[0], url=_C2)], slug="dmm_two"))
    t("★★根拠の件数にも上限がある★★",
      not _ok(evidence=[ev[0], ev[1], dict(ev[0], url=_C2),
                        dict(ev[1], url=_N), dict(ev[0], url=_C)],
              slug="dmm_many"))

    # ★★以前からの守り（v1で入れたもの）が生きているか★★
    t("★★機種が違えば効かない★★（全機種に一律で効かせない）",
      verdict_for("dmm_9999", _EXPECTED, _SEEN, st) is None)
    t("　期待する社が違えば効かない",
      verdict_for("dmm_5086", "sammy", _SEEN, st) is None)
    t("★★答えが出ない状態は控えない★★", not _ok(verdict="UNKNOWN"))
    t("★★根拠が無ければ受け取らない★★",
      not _ok(evidence=[]) and not _ok(
          evidence=[dict(ev[0], quote="短い"), ev[1]]))
    t("★★判断した者が1人だけなら受け取らない★★（2AIで決める）",
      not _ok(by=["claude"]))
    t("★★判断者は決めた2つ以外を受け取らない★★"
      "／以前は架空のID2つでも「違う2者」だった",
      not _ok(by=["foo", "bar"], slug="dmm_by")
      and not _ok(by=["claude", "gemini"], slug="dmm_by2"))
    t("　（対照）決めた2つなら通る", _ok(by=["codex", "claude"], slug="dmm_by3"))
    t("　同じ組を2度控えても増えない",
      (_ok(why="別の理由") and len(st["machines"]["dmm_5086"]) == 1))
    t("★★逐語引用がそのページに無ければ受け取らない★★"
      "／以前はURLも引用も言うだけで通った",
      not _ok(evidence=[dict(ev[0], quote=f"機種名 {_MN} メーカー {_SEEN} 嘘"),
                        ev[1]], slug="dmm_lie"))
    t("　ページを取れなければ控えない（fail-closed）",
      not _ok(evidence=[dict(ev[0], url=_C.replace("264134", "999999")),
                        ev[1]], slug="dmm_nai"))
    t("★★名鑑でない登録済みサイトを「名鑑での観測」にできない★★"
      "／source-registry には解析サイトも載っているので、"
      "「登録済みの発行者」だけで見ると役割の分離が崩れる",
      "www.kitadenshi.co.jp" not in directory_hosts()
      and "nana-press.com" in directory_hosts()
      and not _ok(evidence=[dict(ev[0], url=_KIT), ev[1]], slug="dmm_kit"))
    t("★★同じ社でも https から http へ落とされたら受け取らない★★"
      "／ホストが変わったときだけ見ていたので素通りしていた",
      not _ok(slug="dmm_down",
              fetch=lambda u: (_w_last(u.replace("https://", "http://")),
                               _pages[u])[1]))
    t("　http（暗号化なし）の根拠は受け取らない",
      not _ok(evidence=[dict(ev[0], url=_C.replace("https://", "http://")),
                        ev[1]], slug="dmm_http"))
    t("★★許可したURLから許可外へ転送されたら受け取らない★★",
      not _ok(slug="dmm_redir",
              fetch=lambda u: (_w_last(_KIT), _pages[_C])[1]
              if u == _C else (_w_last(u), _pages[u])[1]))
    t("　（対照）「使わない」と決めるのは名鑑1つでもよい",
      _ok(verdict="REJECT_MATERIAL", evidence=[ev[0]], slug="dmm_rej"))
    t("★★読むときも同じ物差しで確かめる★★（手で書き足しても信用しない）",
      _bad_load())
    t("　取り消せる",
      forget("dmm_5086", _EXPECTED, _SEEN, st)
      and verdict_for("dmm_5086", _EXPECTED, _SEEN, st) is None)

    # ★日付の書き方をならすところ★（相手の作りを読むのではない）
    t("　同じ日付を、名鑑ごとの書き方に直せる"
      "（ちょんぼりすた「2026年10月5日」／なな徹「2026/10/5」）",
      "2026年10月5日" in date_forms(_REL) and "2026/10/5" in date_forms(_REL)
      and date_forms("") == [] and date_forms("2026/10/05") == [])

    ng = sum(1 for _, o in results if not o)
    print()
    print("%d/%d 合格" % (len(results) - ng, len(results)))
    return 1 if ng else 0

def main() -> int:
    ap = argparse.ArgumentParser(description="機種ごとのメーカー同一性の控え")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--forget", action="store_true")
    # ★DMMの機種ページだけ★（2026-08-17・依頼228／台帳#376でP-WORLDは停止）
    ap.add_argument("--machine-url", dest="machine_url",
                    help="DMMの機種ページ https://p-town.dmm.com/machines/<ID>"
                         "（slug・導入日はここから決める）")
    ap.add_argument("--machine-name", dest="machine_name", default="",
                    help="カレンダーに載っている機種名（DMMの見出しと照合する）")
    ap.add_argument("--expected", help="期待している社（名簿のキー）")
    ap.add_argument("--seen", help="名鑑のメーカー欄に書かれていた表記")
    ap.add_argument("--verdict", choices=VERDICTS)
    ap.add_argument("--why")
    # ★自由文はファイルでも渡せる★（2026-08-14）
    #   長い理由をコマンドに書くと、中の記号がシェルに実行される
    #   （2026-08-08に実際に発生）。台帳・メールと同じ受け取り方にそろえる。
    ap.add_argument("--why-file", dest="why_file", default="",
                    help="理由を書いたファイル（--why と同時には使えません）")
    ap.add_argument("--by", help="判断した者（カンマ区切り・2つ以上）")
    ap.add_argument("--evidence", action="append", default=[],
                    help="URL|逐語引用|種類（種類: "
                         + "/".join(KINDS) + "）")
    ap.add_argument("--at", help="決めた日（省略時は今日）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    # ★ファイル渡しは台帳と同じ受け取り方を使う★（置き場の制限つき）
    try:
        import open_issues as _oi
        a.why = _oi._read_text_arg(a.why or "", a.why_file, "why")
    except SystemExit as e:
        print(str(e))
        return 2
    try:
        if a.list:
            got = load()
            for slug, rows in sorted((got.get("machines") or {}).items()):
                print(f"■ {slug}")
                for r in rows:
                    print(f"   {r['expected']} ⇔ {r['seen']}  {r['verdict']}")
                    print(f"      {r.get('why', '')[:80]}")
                    print(f"      {'/'.join(r.get('agreed_by') or [])}"
                          f"（{r.get('decided_at')}）")
            return 0
        if not (a.record or a.forget):
            ap.print_help()
            return 0
        # ★slugは自己申告させない★＝DMMの機種URLから決める
        #   ★形を厳しく見る★（2026-08-17・Codex依頼228の指摘2）
        #   前は共通の slug_from_url に任せていたので、DMM/P-WORLD以外の
        #   URLは**末尾がそのままslug**になり、
        #   `https://example.com/dmm_5086/` のような外部URLでも
        #   狙ったslugの控えを作れた。
        import re as _re
        if not a.machine_url:
            print("★--machine-url が要ります（slugをそこから決めます）★")
            return 1
        m = _re.match(r"^https://p-town\.dmm\.com/machines/(\d+)/?$",
                      str(a.machine_url).strip())
        if not m:
            print(f"★DMMの機種ページのURLだけです: {a.machine_url}★"
                  "／形: https://p-town.dmm.com/machines/<機種ID>")
            return 1
        slug = "dmm_" + m.group(1)
        if a.forget:
            ok = forget(slug, a.expected or "", a.seen or "")
            print("消しました" if ok else "その控えはありません")
            return 0 if ok else 1
        ev = []
        for spec in a.evidence:
            parts = [x.strip() for x in str(spec).split("|")]
            if len(parts) != 3:
                print("★--evidence は『URL|逐語引用|種類』の形で書きます★")
                return 1
            ev.append({"url": parts[0], "quote": parts[1], "kind": parts[2]})
        import datetime
        # ★機種名と導入日はDMMの機種ページから取る★（2026-08-17・依頼228）
        #   ★呼ぶ側の自己申告で決めない★＝控えは手で書けるファイルなので、
        #   ここを言うだけで通すと「別機種の名前と日付」で根拠を作れてしまう。
        #   「使わない」と決めるだけなら機種の紐づけは要らない（取りに行かない）。
        machine_name, release_date = "", ""
        if a.verdict == "ACCEPT_MATERIAL":
            if not str(a.machine_name or "").strip():
                print("★--machine-name が要ります"
                      "（カレンダーに載っている機種名）★")
                return 1
            import dmm_machine as _dm
            try:
                got_m = _dm.fetch(m.group(1))
            except Exception as e:         # noqa: BLE001
                print(f"★DMMの機種ページを読めません: {str(e)[:120]}★")
                return 1
            ok_name, why_name = _dm.name_matches(got_m.get("heading") or "",
                                                 a.machine_name)
            if not ok_name:
                print(f"★その機種名はDMMの機種ページと一致しません: {why_name}★")
                return 1
            machine_name = str(a.machine_name).strip()
            release_date = str(got_m.get("release_date") or "")
            if not _DATE.match(release_date):
                print(f"★DMMから導入日を取れません（{release_date!r}）★"
                      "／日が確定していない機種は控えられません")
                return 1
        # ★CLIでは取ってくる役を差し替えない★＝本物のページで照合する
        rec = remember(slug, a.expected or "", a.seen or "", a.verdict or "",
                       a.why or "", [x.strip() for x in
                                     str(a.by or "").split(",") if x.strip()],
                       ev, a.at or datetime.date.today().isoformat(),
                       machine_name, release_date)
        print(json.dumps({"state": "RECORDED", "slug": slug, **rec},
                         ensure_ascii=False)[:300])
        return 0
    except CacheError as e:
        print("★" + str(e) + "★")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

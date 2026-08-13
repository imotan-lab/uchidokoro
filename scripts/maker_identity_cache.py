"""maker_identity_cache.py — 「この機種について、この2つのメーカー表記は同じか」の控え。

★何のためか★（2026-08-14・運営者の指示／Codexの設計）
  名鑑によって同じ機種のメーカー欄が違う。
    L転生王女 … P-WORLD「オリンピアエステート」／他2社「平和」
    L聖闘士星矢 … P-WORLD「サンスリー」／なな徹「三洋物産」
  これまでは名簿（maker-catalogs.json）に**人が足すまで**その機種が止まっていた。

★やめたこと★
  「同じグループだから全機種で同じ会社」と扱うのをやめた。
  公式は「グループ会社」と書いているだけで、
  ★全機種でメーカー名を入れ替えてよいとは書いていない★。
  1回の判断ミスが、以後すべての機種で関門を無効にしてしまう。

★いまの形★
  ①名簿で一致／正式な別名 → 使う
  ②**この機種について**前に決めた結論があれば、それに従う（この器）
  ③解決できない・関係のありそうな会社 → その場で2AIへ回す
  ④それ以外 → 使わない

★答えが出ない状態は保存しない★
  `MATCH` と `MISMATCH` だけを控える。`UNKNOWN` は「まだ決めていない」＝
  レコードが無い状態として扱う（毎回もう一度考える）。

置き場: Documents/uchidokoro/maker_identity_cache.json（リポジトリ外・公開しない）

使い方:
    python scripts/maker_identity_cache.py --list
    python scripts/maker_identity_cache.py --record --official-url <P-WORLDのURL> \\
        --expected sanslay --seen 三洋物産 --verdict MATCH \\
        --why <理由> --by claude,codex \\
        --evidence "https://…|逐語引用|directory_observation" \\
        --evidence "https://…|逐語引用|official_relationship"
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
SCHEMA = "maker-identity-cache/v1"
VERDICTS = ("MATCH", "MISMATCH")
MIN_QUOTE = 8                          # 逐語引用の最低の長さ
KINDS = ("directory_observation", "official_relationship")


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
    for slug, rows in got["machines"].items():
        if not isinstance(rows, list):
            raise CacheError(f"控えが壊れています（{slug} が並びではありません）")
        for rec in rows:
            _check_record(slug, rec)
    return got


def _check_record(slug: str, rec) -> None:
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
    if len({x.strip().casefold() for x in by}) < 2:
        raise CacheError(f"控えの判断者が足りません（{slug}）: {by!r}"
                         "／★違う2者で決めます★")
    ev = rec.get("evidence")
    if not isinstance(ev, list) or not ev:
        raise CacheError(f"控えに根拠がありません（{slug}）")
    kinds = set()
    for e in ev:
        if not isinstance(e, dict):
            raise CacheError(f"控えの根拠が組ではありません（{slug}）")
        if not str(e.get("url") or "").startswith(("http://", "https://")):
            raise CacheError(f"控えの根拠のURLが不正です（{slug}）: {e.get('url')!r}")
        if len(" ".join(str(e.get("quote") or "").split())) < MIN_QUOTE:
            raise CacheError(f"控えの逐語引用が短すぎます（{slug}）")
        if e.get("kind") not in KINDS:
            raise CacheError(f"控えの根拠の種類が不正です（{slug}）: {e.get('kind')!r}")
        kinds.add(e.get("kind"))
    # ★「同じ」と決めるには、名鑑の観測と公式の関係の両方が要る★
    #   片方だけでは「グループ会社と分かっただけ」で採用できてしまう。
    if rec["verdict"] == "MATCH" and kinds != set(KINDS):
        raise CacheError(f"「同じ」と決めるには{'と'.join(KINDS)}の両方が要ります"
                         f"（{slug}）: いまは {sorted(kinds)}")


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
                fetch=None):
    """この機種について前に決めた結論（無ければ None）。

    ★完全一致で引く★＝(機種・期待する社・名鑑の表記の芯) の3つ。

    ★「同じ」として使う時だけ、根拠が実在するか確かめ直す★
      （2026-08-14・依頼192のP1）書くときに照合しても、
      **控えは手で書き足せるただのファイル**なので、
      形だけ整った偽の根拠で `MATCH` を作れてしまう。
      使う直前に取り直せば、それが通らない。
      ★取れない・引用が見つからないなら「決めていない」と同じ扱い★
      （None を返す＝もう一度2AIへ回る。fail-closed）
      `MISMATCH` は「使わない」側なので取り直さない（遅くする意味がない）。
    """
    if not slug or not expected or not seen:
        return None
    got = store if store is not None else load()
    k = key_of(seen)
    for rec in (got.get("machines") or {}).get(slug) or []:
        if rec.get("expected") == expected and key_of(rec.get("seen")) == k:
            v = rec.get("verdict")
            if v == "MATCH":
                try:
                    verify_evidence(rec.get("evidence") or [], fetch, expected)
                except CacheError:
                    return None
            return v
    return None


def _host_of(url: str) -> str:
    """URLからホストを取り出す（★文字列の前方一致で見ない★）。"""
    import urllib.parse
    return (urllib.parse.urlsplit(str(url or "")).hostname or "").lower()


def official_hosts(expected: str) -> set:
    """★「公式の関係」を示してよいホスト★（名簿に人が登録したものだけ）

    ★なぜ名簿にするか（2026-08-14・依頼192のP1）★
      引用が実在するかを見るだけでは、**第三者の記事でも通ってしまう**。
      「HTTPSである」「公式を名乗っている」「会社名が書いてある」は
      公式であることの根拠にならない。中身から機械に推測させず、
      **人が開いて確かめたホストだけ**を許可する。

    ★関係する社のぶんも合わせる★＝関係は2社の間のことなので、
      親会社の公式が説明していることがある（平和⇔オリンピアエステート）。
    """
    if not expected:
        return set()
    import new_machine_watch as _w
    try:
        cats = (json.load(open(_w.CATALOGS, encoding="utf-8"))
                .get("catalogs") or {})
    except Exception as e:                 # noqa: BLE001
        raise CacheError(f"名簿を読めません（根拠を確かめられません）: {e}")
    me = cats.get(expected)
    if not isinstance(me, dict):
        raise CacheError(f"名簿に「{expected}」がありません")
    grp = str(me.get("maker_relation_group") or "")
    out = set()
    for mid, conf in cats.items():
        if not isinstance(conf, dict):
            continue
        same = (mid == expected) or (
            grp and str(conf.get("maker_relation_group") or "") == grp)
        if not same:
            continue
        for h in conf.get("official_relationship_hosts") or []:
            if str(h).strip():
                out.add(str(h).strip().lower())
    return out


def check_evidence_source(e: dict, expected: str) -> None:
    """★根拠のURLが、その種類にふさわしい出どころか★（依頼192のP1）

    directory_observation … 登録済みの名鑑（source-registry にある発行者）
    official_relationship … 名簿に登録した公式ホスト
    """
    url = str(e.get("url") or "")
    if not url.startswith("https://"):
        raise CacheError(f"根拠は https のページだけです: {url}")
    host = _host_of(url)
    if not host:
        raise CacheError(f"根拠のURLからホストを取れません: {url}")
    kind = e.get("kind")
    if kind == "directory_observation":
        import source_lineage as _sl
        try:
            _sl.publisher_of_host(host)
        except _sl.LineageError:
            raise CacheError(
                f"名鑑として登録されていないサイトです: {host}"
                "／★観測の根拠は登録済みの名鑑から採ります★")
    elif kind == "official_relationship":
        allow = official_hosts(expected)
        if host not in allow:
            raise CacheError(
                f"公式として登録されていないサイトです: {host}"
                f"／★許可: {sorted(allow) or '（未登録）'}★"
                "／第三者の記事・検索結果の要約は公式の根拠になりません。"
                "新しく足すときは名簿の official_relationship_hosts に登録します")
    else:
        raise CacheError(f"根拠の種類が不正です: {kind!r}")


def verify_evidence(evidence: list, fetch=None, expected: str = "") -> None:
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
        fin = _w.LAST_FINAL_URL.get("url")
        if expected and fin and _host_of(fin) != _host_of(url):
            check_evidence_source(dict(e, url=fin), expected)
        body = " ".join(_w._visible_text(html or "").split())
        q = " ".join(str(e.get("quote") or "").split())
        if q not in body:
            raise CacheError(
                f"根拠の逐語引用がそのページに見つかりません（{url}）: "
                f"{q[:40]}／★写した文だけを根拠にします★")


def remember(slug: str, expected: str, seen: str, verdict: str,
             why: str, by: list, evidence: list, decided_at: str,
             store=None, fetch=None) -> dict:
    """結論を控える。★根拠が無ければ受け取らない★

    ★逐語引用は実際にそのページから取ってきて照合する★（依頼190のP1）
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
    verify_evidence(evidence, fetch, expected)
    _check_record(slug, {"expected": expected, "seen": seen,
                         "verdict": verdict, "why": why, "agreed_by": by,
                         "evidence": evidence, "decided_at": decided_at})
    got = store if store is not None else load()
    rows = got.setdefault("machines", {}).setdefault(slug, [])
    k = key_of(seen)
    rec = {"expected": expected, "seen": seen, "verdict": verdict,
           "why": why, "evidence": evidence, "agreed_by": by,
           "decided_at": decided_at}
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

def _bad_load() -> bool:
    """★手で書き足した根拠なしのレコードを、読むときに弾けるか★（試験用）"""
    import copy
    good = {"schema_version": SCHEMA, "machines": {"pw_1": [
        {"expected": "sanslay", "seen": "三洋物産", "verdict": "MATCH",
         "why": "理由", "agreed_by": ["claude", "codex"],
         "decided_at": "2026-08-14",
         "evidence": [{"url": "https://x.test/a", "quote": "メーカー 三洋物産",
                       "kind": "directory_observation"},
                      {"url": "https://x.test/b", "quote": "株式会社サンスリー",
                       "kind": "official_relationship"}]}]}}
    for bad in ({"verdict": "MATCH", "expected": "sanslay", "seen": "三洋物産"},
                dict(good["machines"]["pw_1"][0], agreed_by=["claude"]),
                dict(good["machines"]["pw_1"][0], agreed_by=["claude", "claude"]),
                dict(good["machines"]["pw_1"][0], evidence=[]),
                dict(good["machines"]["pw_1"][0],
                     evidence=good["machines"]["pw_1"][0]["evidence"][:1])):
        g = copy.deepcopy(good)
        g["machines"]["pw_1"] = [bad]
        try:
            for slug, rows in g["machines"].items():
                for rec in rows:
                    _check_record(slug, rec)
            return False
        except CacheError:
            pass
    return True


def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅ " if cond else "❌ ") + name)

    # ★出どころも試験の一部★（登録済みの名鑑／登録済みの公式ホスト）
    _DIR = "https://nana-press.com/x"                 # 登録済みの名鑑
    _OFF = "https://www.sanyobussan.co.jp/corporate/"  # 名簿に登録した公式
    _3RD = "https://chonborista.com/kaisetsu"          # 名鑑だが公式ではない
    ev = [{"url": _DIR, "quote": "メーカー 三洋物産",
           "kind": "directory_observation"},
          {"url": _OFF, "quote": "株式会社サンスリー 遊技機の開発・製造",
           "kind": "official_relationship"}]
    st = _empty()

    _pages = {
        _DIR: "<p>メーカー 三洋物産 / 機種一覧</p>",
        _OFF: "<p>株式会社サンスリー 遊技機の開発・製造 を行います</p>",
        # ★第三者の記事に同じ文が実在しても、公式の根拠にはならない★
        _3RD: "<p>株式会社サンスリー 遊技機の開発・製造 を行います（解説）</p>",
    }

    def _fetch(u):
        if u not in _pages:
            raise RuntimeError("404")
        _w_last(u)                    # ★転送なし＝最後のURLは自分自身★
        return _pages[u]

    def _w_last(u):
        """取ってくる役が「最後に着いたURL」を控える（本物と同じ形）。"""
        import new_machine_watch as _w
        _w.LAST_FINAL_URL["url"] = u
        return u

    def _ok(**kw):
        base = dict(slug="pw_1", expected="sanslay", seen="三洋物産",
                    verdict="MATCH", why="理由", by=["claude", "codex"],
                    evidence=ev, decided_at="2026-08-14", store=st,
                    fetch=_fetch)
        base.update(kw)
        try:
            remember(**base)
            return True
        except CacheError:
            return False

    t("★★根拠つきなら控えられる★★", _ok())
    t("　控えた結論を引ける",
      verdict_for("pw_1", "sanslay", "株式会社三洋物産", st, _fetch) == "MATCH")
    # ★★使うときにも根拠を取り直す（2026-08-14・依頼192のP1）★★
    #   控えは手で書き足せるただのファイルなので、
    #   書くときだけ照合しても、形だけ整った偽の根拠で「同じ」を作れる。
    t("★★根拠のページが取れなくなったら「同じ」として使わない★★"
      "／手で書き足した偽の根拠を、使う直前に落とす",
      verdict_for("pw_1", "sanslay", "三洋物産", st,
                  lambda u: (_ for _ in ()).throw(RuntimeError("404"))) is None)
    t("　（対照）取り直せるうちは今までどおり使える",
      verdict_for("pw_1", "sanslay", "三洋物産", st, _fetch) == "MATCH")
    t("　引用が消えていたら使わない",
      verdict_for("pw_1", "sanslay", "三洋物産", st,
                  lambda u: "<p>ページが作り替えられました</p>") is None)
    t("★★機種が違えば効かない★★（全機種に一律で効かせない）",
      verdict_for("pw_2", "sanslay", "三洋物産", st) is None)
    t("　期待する社が違えば効かない",
      verdict_for("pw_1", "sammy", "三洋物産", st) is None)
    t("★★答えが出ない状態は控えない★★", not _ok(verdict="UNKNOWN"))
    t("★★根拠が無ければ受け取らない★★",
      not _ok(evidence=[]) and not _ok(
          evidence=[{"url": "https://x.test/a", "quote": "短い",
                     "kind": "directory_observation"}]))
    t("　根拠の種類を勝手に作れない",
      not _ok(evidence=[{"url": "https://x.test/a", "quote": "メーカー 三洋物産",
                         "kind": "でっちあげ"}]))
    t("★★判断した者が1人だけなら受け取らない★★（2AIで決める）",
      not _ok(by=["claude"]))
    t("　同じ組を2度控えても増えない",
      (_ok(why="別の理由") and len(st["machines"]["pw_1"]) == 1))
    # ★★言うだけでは通さない（2026-08-14・依頼190のP1）★★
    t("★★逐語引用がそのページに無ければ受け取らない★★"
      "／以前はURLも引用も言うだけで通った",
      not _ok(evidence=[dict(ev[0], quote="どこにも書いていない文字列"),
                        ev[1]]))
    t("　（対照）そのページにある文なら通る＝検査が厳しすぎるのではない",
      _ok(evidence=[dict(ev[0], quote="メーカー 三洋物産"), ev[1]],
          slug="pw_taisyo"))
    t("　ページを取れなければ控えない（fail-closed）",
      not _ok(evidence=[dict(ev[0], url="https://x.test/nai"), ev[1]]))
    t("★★「同じ」と決めるには名鑑の観測と公式の関係の両方が要る★★",
      not _ok(evidence=[ev[0]]) and not _ok(evidence=[ev[1]]))
    # ★★出どころも見る（2026-08-14・依頼192のP1）★★
    #   引用が実在するかを見るだけでは、第三者の記事でも通ってしまう。
    t("★★第三者の記事は「公式の関係」の根拠にならない★★"
      "／同じ文がそこに実在しても、公式であることの根拠にはならない",
      not _ok(evidence=[ev[0], dict(ev[1], url=_3RD)], slug="pw_3rd"))
    t("　（対照）名簿に登録した公式ホストなら通る", _ok(slug="pw_off"))
    t("　登録されていない名鑑からは観測を採らない",
      not _ok(evidence=[dict(ev[0], url="https://example.com/x"), ev[1]],
              slug="pw_dir"))
    t("　http（暗号化なし）の根拠は受け取らない",
      not _ok(evidence=[dict(ev[0], url=_DIR.replace("https://", "http://")),
                        ev[1]], slug="pw_http"))
    t("★★許可したURLから許可外へ転送されたら受け取らない★★",
      not _ok(slug="pw_redir",
              fetch=lambda u: (_w_last(_3RD), _pages[_OFF])[1]
              if u == _OFF else (_w_last(u), _pages[u])[1]))
    t("　公式ホストは関係する社のぶんも合わせて見る"
      "（平和⇔オリンピアエステート）",
      official_hosts("olympia_estate") == official_hosts("heiwa")
      and "www.heiwanet.co.jp" in official_hosts("olympia_estate"))
    t("　（対照）「違う」と決めるのは片方でもよい",
      _ok(verdict="MISMATCH", evidence=[ev[0]], slug="pw_mis"))
    t("★★読むときも同じ物差しで確かめる★★（手で書き足しても信用しない）",
      _bad_load())
    t("　取り消せる",
      forget("pw_1", "sanslay", "三洋物産", st)
      and verdict_for("pw_1", "sanslay", "三洋物産", st) is None)

    ng = sum(1 for _, o in results if not o)
    print()
    print("%d/%d 合格" % (len(results) - ng, len(results)))
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="機種ごとのメーカー同一性の控え")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--forget", action="store_true")
    ap.add_argument("--official-url", dest="official_url",
                    help="P-WORLDの機種ページ（slugはここから決める）")
    ap.add_argument("--expected", help="期待している社（名簿のキー）")
    ap.add_argument("--seen", help="名鑑のメーカー欄に書かれていた表記")
    ap.add_argument("--verdict", choices=VERDICTS)
    ap.add_argument("--why")
    ap.add_argument("--by", help="判断した者（カンマ区切り・2つ以上）")
    ap.add_argument("--evidence", action="append", default=[],
                    help="URL|逐語引用|種類（種類: "
                         + "/".join(KINDS) + "）")
    ap.add_argument("--at", help="決めた日（省略時は今日）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
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
        # ★slugは自己申告させない★＝P-WORLDのURLから決める
        import build_new_article as _ba
        if not a.official_url:
            print("★--official-url が要ります（slugをそこから決めます）★")
            return 1
        slug = _ba.slug_from_url(a.official_url)
        if not slug:
            print(f"★そのURLからslugを決められません: {a.official_url}★")
            return 1
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
        # ★CLIでは取ってくる役を差し替えない★＝本物のページで照合する
        rec = remember(slug, a.expected or "", a.seen or "", a.verdict or "",
                       a.why or "", [x.strip() for x in
                                     str(a.by or "").split(",") if x.strip()],
                       ev, a.at or datetime.date.today().isoformat())
        print(json.dumps({"state": "RECORDED", "slug": slug, **rec},
                         ensure_ascii=False)[:300])
        return 0
    except CacheError as e:
        print("★" + str(e) + "★")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""new_machine_watch.py — メーカー公式の機種一覧を見て、新台を見つける。

★なぜこの向きなのか（2026-07-31・運営者判断＝完全自動化）★
  以前は「まとめサイトの機種名 → 公式ページを探す」向きだった。
  これだと名前の照合が必要で、人の判断なしには自動化できない。
  実際、まとめサイトの「ビンゴライブ・8月3日導入」は**名前も日付も誤り**で、
  公式は「Ｌすーぱぁびん娘・2026年8月登場」だった。

  そこで向きを逆にする。

    メーカー公式の機種一覧 → 新しいURLが現れた ＝ それが新台

  まとめサイトの名前を**そもそも読まない**ので、照合が発生しない。
  機種の正体は「公式一覧に載っている個別ページのURL」そのものになる。

★人が保守するのは assets/data/maker-catalogs.json だけ★
  メーカーの一覧ページURLを書くファイル。機種ごとの作業はゼロ。
  ここに無いメーカーの新台は見つからないが、それは「出さない」側の失敗。

★黙って0件にしない★
  一覧ページの作りが変わってリンクが取れなくなると、
  「新台なし」と誤認して静かに止まる。これが一番こわい。
  だからメーカーごとに「最低これだけは並んでいるはず」の数を持ち、
  下回ったら**異常として報告する**（新台なしとは言わない）。

使い方:
    python scripts/new_machine_watch.py --scan          # 全メーカーを見る
    python scripts/new_machine_watch.py --check bellco  # 1社だけ試す
    python scripts/new_machine_watch.py --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import safe_json as _sj               # noqa: E402

CATALOGS = os.path.join(BASE, "assets", "data", "maker-catalogs.json")
SEEN_PATH = r"C:/Users/imao_/Documents/uchidokoro/seen_machine_urls.json"
UA = "uchidokoro-new-machine-watch/1.0 (+https://uchidokoro.com)"
MAX_BYTES = 5 * 1024 * 1024

# 一覧ページに混ざる「機種ではないリンク」を落とす。
#   ★許可した形だけ通す★（禁止語を並べる方式は必ず抜ける）
_SLUGLIKE = re.compile(r"^[a-z0-9][a-z0-9_\-]{1,60}$")


class WatchError(RuntimeError):
    pass


# ★最後にどのURLへ着いたか★（転送でトップや別サイトへ飛ばされた事故を見つける）
#   _get は文字列しか返さないので、直近の到達先をここに控える。
LAST_FINAL_URL = {"url": None}


def _get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    LAST_FINAL_URL["url"] = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                raise WatchError(f"HTTP {r.status}: {url}")
            LAST_FINAL_URL["url"] = r.geturl()
            body = r.read(MAX_BYTES + 1)
            charset = r.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as e:
        raise WatchError(f"取得できません（HTTP {e.code}）: {url}")
    except WatchError:
        raise
    except Exception as e:
        raise WatchError(f"取得できません（{type(e).__name__}）: {url}")
    if len(body) > MAX_BYTES:
        raise WatchError(f"ページが大きすぎます: {url}")
    return body.decode(charset, "replace")


# ★機種ではない「年別アーカイブ」を機種と数えない★（2026-07-31・平和で確認）
#   一覧の直下に 2009 / 2010 … が機種と同じ形で並ぶ社がある。
#   年だけの見た目は機種名になりえないので、機械的に外してよい。
_YEAR_ONLY = re.compile(r"^(19|20)\d\d$")


# ★一覧ページではない画面を「一覧」として読まないための語★
#   （2026-07-31・Codex優先度3）
#   最終URLが正しくても、アクセス拒否・メンテナンス・年齢確認・soft 404 が
#   返ることがある。件数の下限だけでは、そこそこリンクがある拒否画面を通す。
# ★これが出たら、それだけで一覧ではない★
_BAD_STRONG = (
    "アクセスが拒否", "アクセスできません", "ただいまメンテナンス", "メンテナンス中",
    "サービスを停止", "access denied", "forbidden", "service unavailable",
)
# ★これだけでは決められない語★（正常なページの注意書きにも出る）
#   例：パチスロメーカーのサイトには「18歳未満」の注意書きがあって当たり前。
#   そこで**一覧である証拠が無いとき**だけ、これらを異常の根拠にする。
_BAD_WEAK = (
    "ページが見つかりません", "お探しのページは", "not found",
    "年齢確認", "18歳未満", "あなたは18歳以上ですか", "18歳以上ですか",
)
_BAD_PAGE_WORDS = _BAD_STRONG + _BAD_WEAK      # 互換のため残す


def bad_page(html: str, looks_like_list: bool = False):
    """一覧ではない画面（拒否・メンテ・年齢確認・soft 404）なら理由を返す。

    ★語だけで決めない★（2026-07-31・Codex指摘を再現して二段構えにした）
      強い語（アクセス拒否・メンテナンス）は単独で止める。
      弱い語（18歳未満・ページが見つかりません）は、
      **一覧である証拠（印と機種リンク）が無いとき**だけ根拠にする。
      でないと、注意書きに「18歳未満」と書いてある正常な一覧まで止まる。
    """
    text = unicodedata.normalize("NFKC", _visible_text(html or "")).lower()
    for word in _BAD_STRONG:
        if word.lower() in text:
            return f"一覧ではない画面が返っています（『{word}』）"
    if looks_like_list:
        return None
    for word in _BAD_WEAK:
        if word.lower() in text:
            return f"一覧ではない画面が返っている可能性があります（『{word}』）"
    return None


def _host(u: str) -> str:
    """比べるためのホスト名。★www の有無は同じサイトとして扱う★"""
    return urllib.parse.urlparse(u or "").netloc.lower().removeprefix("www.")


def redirect_problem(asked: str, final: str):
    """転送された先がおかしくないか。★おかしければ理由を返す★

    ★2026-07-31・Codex優先度1を実装し、実際に設定ミスを見つけた★
      山佐ネクストは `www.yamasa-next.co.jp/machine/` を叩くと
      **トップページへ転送**されていた。一覧を読んでいるつもりで
      別のページを読んでいたことになる。
      なお www の有無だけの転送はよくあるので、それは異常としない。
    """
    if not final:
        # ★どこへ着いたか分からないなら、正常とは言えない★（Codex指摘）
        return "最終URLを確認できませんでした"
    if _host(final) != _host(asked):
        return f"別のドメインへ転送されました（{final[:90]}）"
    ap = urllib.parse.urlparse(asked).path.rstrip("/")
    fp = urllib.parse.urlparse(final).path.rstrip("/")
    if ap and not fp:
        return f"トップページへ転送されました（{final[:90]}）"
    if ap != fp:
        # ★同じサイトの中でも、別のページに飛ばされたら同じ一覧ではない★
        #   正当な転送がある社は、カタログに allow_redirect_to を書いて許可する。
        return f"別のページへ転送されました（{final[:90]}）"
    return None


def product_urls(html: str, base_url: str, link_prefix: str) -> list:
    """一覧ページから、個別機種ページのURLを取り出す。

    ★一覧ページ自身や親ページを機種と数えない★
      `/products/slot/` のような「末尾が接頭辞と同じ」ものは機種ではない。
    """
    out = set()
    for href in re.findall(r'href="([^"]+)"', html):
        absu = urllib.parse.urljoin(base_url, href.strip())
        absu = absu.split("#")[0].split("?")[0]
        if not absu.startswith(link_prefix):
            continue
        rest = absu[len(link_prefix):].strip("/")
        if not rest or "/" in rest:
            continue                      # 一覧そのもの／さらに下の階層は対象外
        if not _SLUGLIKE.match(rest):
            continue
        if _YEAR_ONLY.match(rest):
            continue                      # ★年別アーカイブは機種ではない★
        out.add(link_prefix.rstrip("/") + "/" + rest + "/")
    return sorted(out)


def page_title(html: str) -> str:
    m = re.search(r"(?is)<title[^>]*>(.*?)</title\s*>", html)
    if not m:
        m = re.search(r"(?is)<h1[^>]*>(.*?)</h1\s*>", html)
    if not m:
        return ""
    t = re.sub(r"(?s)<[^>]+>", "", m.group(1))
    return unicodedata.normalize("NFKC", t).strip()


def machine_name(html: str) -> str:
    """公式ページのタイトルから機種名だけを取る（サイト名などを落とす）。"""
    t = page_title(html)
    # 「機種名|機種情報|メーカー名...」の形が多い。最初の区切りまでを名前とする。
    for sep in ("|", "｜", "-", "‐", "―", "–"):
        if sep in t:
            t = t.split(sep)[0]
            break
    return t.strip()



# ★新台と認めるための条件★（2026-07-31・Codexの追加条件）
#   「未知のURL＝新台」だけでは足りない。次を全部満たしたものだけを候補にする。
#     1. パチスロのページであること
#     2. 公式が登場年月を書いていること（こちらで日を補わない）
#     3. すでに扱っている機種でないこと
#     4. 前に見たURLの中身が別機種にすり替わっていないこと
#   1つでも欠けたら候補にせず、理由を残す（黙って落とさない）。

_SLOT_WORDS = ("パチスロ", "スロット", "回胴", "スマスロ", "純増", "AT", "ART")
_RELEASE_RE = re.compile(r"(20\d\d)年\s*(\d{1,2})月")


def _visible_text(html: str) -> str:
    # ★scriptの中身を本文に混ぜない★
    #   タグ名は文字列から組み立てる（バックスラッシュを直接書くと
    #   編集の経路で制御文字に化ける事故が今日5回起きたため）
    for tag in ("script", "style", "noscript"):
        html = re.sub("(?is)<" + tag + "[^>]*>.*?</" + tag + "[ \t\r\n]*>", " ", html)
    t = re.sub("(?s)<[^>]+>", chr(10), html)
    # ★実体参照をほどく★（2026-07-31）
    #   `&nbsp;` が残ると「50枚あたりのゲーム数&nbsp;約31G」のように
    #   見出しと値がくっついたまま読めず、値を取りこぼす（実データで確認）。
    import html as _html
    t = _html.unescape(t)
    t = unicodedata.normalize("NFKC", t)
    return chr(10).join(x.strip() for x in t.splitlines() if x.strip())


def release_month(text: str):
    """公式が書いている登場年月。★日は補わない★（公式が月までなら月まで）"""
    m = _RELEASE_RE.search(text)
    if not m:
        return None
    return {"value": f"{m.group(1)}-{int(m.group(2)):02d}", "precision": "month",
            "quote": m.group(0)}


def looks_like_slot(text: str) -> bool:
    return any(w in text for w in _SLOT_WORDS)


def known_official_urls() -> set:
    """すでに扱っている機種の公式URL（重複を防ぐ）。"""
    try:
        rows = _sj.read_rows(os.path.join(BASE, "assets", "data", "machines.json"))
    except Exception:
        return set()
    out = set()
    for m in rows:
        u = (m.get("identity") or {}).get("official_product_url")
        if isinstance(u, str) and u:
            out.add(u.rstrip("/") + "/")
    return out


# 新台とみなす登場年月の幅（今月の1か月前 〜 6か月先）
#   前: 導入直後に気づいた場合も拾う  後: 事前告知を拾う
RECENT_BACK_MONTHS = 1
RECENT_AHEAD_MONTHS = 6


def is_recent(ym: str, today=None) -> bool:
    """登場年月が「新台」と呼べる範囲か。"""
    from datetime import date
    t = today or date.today()
    try:
        y, m = (int(x) for x in ym.split("-"))
    except Exception:
        return False
    # ★月が1〜12か確かめる★（2026-07-31・Codexの指摘を確かめる過程で見つけた）
    #   月を見ていなかったので `2026年13月` が新台として通っていた。
    #   99月は差が大きすぎて弾かれていたが、13月は範囲に入って通っていた。
    if not (1 <= m <= 12):
        return False
    months = (y - t.year) * 12 + (m - t.month)
    return -RECENT_BACK_MONTHS <= months <= RECENT_AHEAD_MONTHS


def classify(url: str, seen_entry: dict | None = None, today=None) -> dict:
    """新台候補として通してよいか判定する。★通らない理由を必ず残す★"""
    out = {"url": url, "ok": False, "reasons": [], "official_name": "",
           "release": None}
    try:
        html = _get(url)
    except WatchError as e:
        out["reasons"].append(str(e))
        return out
    text = _visible_text(html)
    out["official_name"] = machine_name(html)
    out["release"] = release_month(text)

    if not out["official_name"]:
        out["reasons"].append("公式ページから機種名を取れません")
    if not looks_like_slot(text):
        out["reasons"].append("パチスロのページに見えません（回胴機の語が無い）")
    if not out["release"]:
        out["reasons"].append("公式が登場年月を書いていません（こちらで日付を補わない）")
    elif not is_recent(out["release"]["value"], today):
        # ★古い機種のページを新台にしない★（Codexの「新しい登場年月」の条件）
        #   見たことのあるURLの記録が消えたときに、一覧の全機種が
        #   新台として押し寄せるのを止める最後の砦でもある。
        out["reasons"].append(
            f"登場年月が新台の範囲外です（{out['release']['value']}）")
    if url.rstrip("/") + "/" in known_official_urls():
        out["reasons"].append("すでに扱っている機種です")
    # ★前に見たURLの中身が別機種にすり替わっていないか★
    if seen_entry and seen_entry.get("name") and out["official_name"]             and seen_entry["name"] != out["official_name"]:
        out["reasons"].append(
            f"同じURLの機種名が変わりました（{seen_entry['name']} → {out['official_name']}）")
    out["ok"] = not out["reasons"]
    return out


def _load_seen() -> dict:
    if not os.path.isfile(SEEN_PATH):
        return {"schema": "seen-machine-urls/v1", "makers": {}}
    try:
        d = _sj.read_json(SEEN_PATH, expect=dict)
    except Exception as e:
        # ★読めないときは「全部新台」にしない★（初回と区別できず大量誤検出になる）
        raise WatchError(f"見たことのあるURLの記録が読めません: {e} → 今日は止めます")
    d.setdefault("makers", {})
    return d


def _save_seen(data: dict) -> None:
    import tempfile
    os.makedirs(os.path.dirname(SEEN_PATH), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(SEEN_PATH), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=chr(10)) as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SEEN_PATH)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _get_rendered(url: str, link_prefix: str = "") -> tuple:
    """★ブラウザで描画してから読む★（機種リンクがJavaScriptで作られる社向け）

    ★「ブラウザが起動できた」だけでは成功と見なさない★（Codex指摘・2026-07-31）
      JavaScriptエラー・通信遮断・Cookie画面・遅延読み込み未完了でも、
      リンク0件のまま正常終了しうる。そこで健全性を一緒に返し、
      呼び出し側が「読めなかった」と「読めたが新台なし」を区別できるようにする。

    返すもの: (html, health)
      health = {"status", "final_url", "js_errors", "problem"}
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:                       # noqa: BLE001
        raise WatchError(f"描画取得を使えません（Playwrightが要ります）: {e}")
    health = {"status": None, "final_url": None, "js_errors": [], "problem": None,
              "idle_timeout": False, "unstable": False, "counted": None}
    try:
        with sync_playwright() as pw:
            br = pw.chromium.launch()
            try:
                page = br.new_page()
                page.on("pageerror", lambda e: health["js_errors"].append(str(e)[:120]))
                resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
                health["status"] = resp.status if resp else None
                # ★通信が落ち着くまで待つ。落ち着かなくても記録して先へ進む★
                #   networkidle を必須にすると、広告や計測が鳴り続ける社で
                #   毎回タイムアウトして「読めない」になる（サミーで実際に発生）。
                #   代わりに「待ち切れなかった」ことを健全性として残し、
                #   件数の下限・残存率の検査で取りこぼしを見つける。
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:               # noqa: BLE001
                    health["idle_timeout"] = True
                page.wait_for_timeout(2000)
                # ★件数が続けて変わらないことを確かめる★（2026-07-31・Codex優先度4）
                #   遅延読み込みの途中で読むと、件数は正常なのに新台だけ落ちる。
                #   同じ数が3回続くまで待ち、続かなければ「まだ増えている」と記録する。
                if link_prefix:
                    same, last = 0, -1
                    for _ in range(8):
                        n = len(product_urls(page.content(), url, link_prefix))
                        same = same + 1 if n == last else 0
                        last = n
                        if same >= 2:
                            break
                        page.wait_for_timeout(1500)
                    health["unstable"] = same < 2
                    health["counted"] = last
                health["final_url"] = page.url
                html = page.content()
            finally:
                br.close()
    except Exception as e:                       # noqa: BLE001
        raise WatchError(f"描画できません: {type(e).__name__}: {e}")
    if health["status"] != 200:
        health["problem"] = f"HTTP {health['status']} が返りました"
    else:
        # ★静的取得と同じ判定を使う★（www の扱いが食い違っていた・Codex指摘）
        health["problem"] = redirect_problem(url, health["final_url"])
    return html, health


# ★一覧が丸ごと別物に差し替わったことを見抜くための条件★
#   （2026-07-31・Codexと相談し、自分で再現してから追加）
#   件数の下限だけでは、**同じ件数の別の一覧**を掴んだときに素通りする。
#   実際、既知60件が0件残りの55件に入れ替わっても「新台55件」として通った。
RETENTION_MIN = 0.8      # 前回の既知URLがこの割合は残っているはず
# ★1回のスキャンでこれ以上増えたら『新台』と扱わない★
#   以前は max(5, 全体の2割) にしていたので、97件の社では19件増えても通っていた
#   （名前は「絶対上限」なのに実際は割合で緩んでいた・Codex指摘を自分で確認）。
#   超えた日は記録を更新せず理由を残すので、人が見て判断する。
MAX_NEW_PER_SCAN = 5


def is_catalog(conf) -> bool:
    """メーカーの登録かどうか。★覚え書きをメーカーとして数えない★"""
    return isinstance(conf, dict) and "status" in conf


def scan_maker(maker_id: str, conf: dict, seen: dict, record: bool = True) -> dict:
    """1社ぶん見る。★取れた数が少なすぎたら『新台なし』と言わない★

    ★状態は3つ以上に分ける★（成功／失敗の2値では足りない）
      OK / FIRST_TIME / FETCH_FAILED / PARSE_SUSPECT
      「読めなかった」と「読めたが新台なし」を混ぜないため。
    """
    out = {"maker": maker_id, "name": conf.get("name"), "new": [], "problem": None,
           "total": 0, "first_time": maker_id not in seen["makers"], "state": "OK",
           "retention": None}
    render = str(conf.get("fetch") or "static") == "render"
    health = {}
    try:
        if render:
            html, health = _get_rendered(conf["list_url"], conf["link_prefix"])
            if health.get("problem"):
                out["problem"] = health["problem"]
                out["state"] = "FETCH_FAILED"
                return out
            if health.get("unstable"):
                # ★まだ増えている途中で読んだ★＝新台だけ落ちている恐れ
                out["problem"] = ("一覧の件数が落ち着きません（読み込みの途中の可能性）。"
                                  "『新台なし』とは扱いません")
                out["state"] = "PARSE_SUSPECT"
                return out
        else:
            html = _get(conf["list_url"])
            why = redirect_problem(conf["list_url"], LAST_FINAL_URL.get("url"))
            if why:
                out["problem"] = why
                out["state"] = "FETCH_FAILED"
                return out
    except WatchError as e:
        out["problem"] = str(e)
        out["state"] = "FETCH_FAILED"
        return out
    out["js_errors"] = len(health.get("js_errors") or [])
    out["idle_timeout"] = bool(health.get("idle_timeout"))

    # ★そのページである印を確かめる★（2026-07-31・Codex優先度2）
    #   最終URLが正しくても、別の画面が返ることがある。
    #   カタログに `list_marker` を書いておけば、その語が本文に無いとき止まる。
    # ★一覧である証拠がそろっているか★（印と機種リンクの両方）
    #   証拠があるなら、弱い語（18歳未満など）は異常の根拠にしない。
    # ★印は「ページの題が その語で始まること」で見る★（2026-07-31・Codex指摘）
    #   本文に含まれるかで見ると弱い。実際、ユニバーサル・ニューギン・北電子では
    #   機種ページの題が一覧の題を**末尾に含む**ため、本文照合では区別できなかった。
    #   例: 一覧「パチスロ|ユニバーサル…」／機種「アレックス ブライト|パチスロ|…」
    #   題の先頭で見れば、機種ページは機種名から始まるので区別できる。
    marker = conf.get("list_marker")
    title_n = unicodedata.normalize("NFKC", page_title(html))
    has_marker = bool(marker) and title_n.startswith(
        unicodedata.normalize("NFKC", marker))
    has_links = len(product_urls(html, conf["list_url"], conf["link_prefix"])) > 0
    why = bad_page(html, looks_like_list=has_marker and has_links)
    if why:
        out["problem"] = why
        out["state"] = "FETCH_FAILED"
        return out

    if marker and not has_marker:
        out["problem"] = (f"一覧ページの題が『{marker}』で始まりません"
                          f"（実際の題: {page_title(html)[:50]!r}）。"
                          f"別の画面を読んでいる可能性があるので『新台なし』とは扱いません")
        out["state"] = "PARSE_SUSPECT"
        return out

    urls = product_urls(html, conf["list_url"], conf["link_prefix"])
    out["total"] = len(urls)
    least = int(conf.get("min_expected") or 1)
    if len(urls) < least:
        # ★ここが黙って0件になる事故を止める唯一の砦★
        out["problem"] = (f"一覧から {len(urls)} 件しか取れません（最低 {least} 件のはず）。"
                          f"ページの作りが変わった可能性があるので『新台なし』とは扱いません"
                          + (f"／描画中にJSエラー {out['js_errors']} 件"
                             if out.get("js_errors") else ""))
        out["state"] = "PARSE_SUSPECT"
        return out

    known = set(seen["makers"].get(maker_id, {}).get("urls") or [])
    if out["first_time"]:
        # ★初回は全部を『既知』として覚えるだけ★
        #   いきなり100件を新台として扱わない。
        out["new"] = []
        out["state"] = "FIRST_TIME"
    else:
        kept = len(known & set(urls))
        # ★比べるのは丸める前の値★（丸めると 0.7996 が 0.8 になって通る・Codex指摘）
        ratio = (kept / len(known)) if known else None
        out["retention"] = round(ratio, 3) if ratio is not None else None
        if known and ratio < RETENTION_MIN:
            # ★前に見たURLが大量に消えた＝別の一覧を掴んだ疑い★
            out["problem"] = (
                f"前回の {len(known)} 件のうち {kept} 件しか残っていません"
                f"（{ratio:.1%}）。別の一覧を読んだ可能性があるので"
                f"『新台』とは扱いません")
            out["state"] = "PARSE_SUSPECT"
            return out          # ★記録も更新しない（誤った基準で上書きしない）★
        got = [u for u in urls if u not in known]
        limit = MAX_NEW_PER_SCAN
        if len(got) > limit:
            out["problem"] = (
                f"一度に {len(got)} 件も増えています（多くても {limit} 件のはず）。"
                f"一覧の作りが変わった可能性があるので『新台』とは扱いません")
            out["state"] = "PARSE_SUSPECT"
            return out
        out["new"] = got
    if record:
        seen["makers"][maker_id] = {"urls": urls, "count": len(urls)}
    return out


def describe(url: str) -> dict:
    """新台候補の個別ページから、公式が書いていることだけを取る。"""
    html = _get(url)
    text = re.sub(r"(?s)<[^>]+>", chr(10), re.sub(
        r"(?is)<(script|style)\b.*?</\1\s*>", " ", html))
    text = unicodedata.normalize("NFKC", text)
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    when = [x for x in lines if re.search(r"20\d\d年\s*\d{1,2}月", x)][:3]
    return {"url": url, "official_name": machine_name(html),
            "title": page_title(html), "release_lines": when,
            "chars": len(text)}


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    LIST = "https://m.example/products/slot/"
    html = ('<a href="/products/slot/aaa/">A</a>'
            '<a href="/products/slot/bbb/">B</a>'
            '<a href="/products/slot/">一覧</a>'
            '<a href="/products/pachinko/ccc/">パチンコ</a>'
            '<a href="/products/slot/aaa/spec/">下の階層</a>'
            '<a href="https://other.example/products/slot/ddd/">よそ</a>')
    got = product_urls(html, LIST, LIST)
    t("★個別機種ページだけを取る★",
      got == ["https://m.example/products/slot/aaa/",
              "https://m.example/products/slot/bbb/"])
    t("　一覧ページ自身を機種と数えない", LIST not in got)
    t("　パチンコ側・よそのサイト・下の階層は取らない",
      not any("pachinko" in u or "other.example" in u or "spec" in u for u in got))
    t("★★年別アーカイブ（2009・2010…）を機種と数えない★★（平和で確認）",
      product_urls('<a href="/products/slot/2009/">2009年</a>'
                   '<a href="/products/slot/sns3/">機種</a>', LIST, LIST)
      == ["https://m.example/products/slot/sns3/"])
    t("　#や?が付いていても同じURLとして1件にする",
      product_urls('<a href="/products/slot/aaa/?x=1">A</a>'
                   '<a href="/products/slot/aaa/#top">A</a>', LIST, LIST)
      == ["https://m.example/products/slot/aaa/"])

    t("★タイトルから機種名だけを取る★",
      machine_name("<title>Lすーぱぁびん娘|機種情報|BELLCO(ベルコ株式会社)</title>")
      == "Lすーぱぁびん娘")
    t("　全角の区切りでも取れる",
      machine_name("<title>テスト機　情報｜メーカー</title>") == "テスト機 情報")

    conf = {"name": "t", "list_url": LIST, "link_prefix": LIST, "min_expected": 5}
    seen = {"makers": {"t": {"urls": ["https://m.example/products/slot/aaa/"]}}}

    class _Stub:
        def __init__(self, h): self.h = h

    import builtins  # noqa: F401
    global _get
    real_get = _get
    try:
        def _fake(u, timeout=20, _h=None):
            LAST_FINAL_URL["url"] = u          # ★本物と同じく到達先を残す★
            return _h if _h is not None else html

        _get = _fake
        r = scan_maker("t", conf, seen, record=False)
        t("★★取れた数が少なすぎたら『新台なし』と言わない★★（黙って止まる事故を防ぐ）",
          r["problem"] is not None and r["new"] == [])
        conf2 = {**conf, "min_expected": 2}
        r2 = scan_maker("t", conf2, seen, record=False)
        t("　数が足りていれば、知らないURLだけを新台とする",
          r2["problem"] is None and r2["new"] == ["https://m.example/products/slot/bbb/"])
        r3 = scan_maker("zzz", conf2, {"makers": {}}, record=False)
        t("★★初回は全部を新台にしない（覚えるだけ）★★",
          r3["first_time"] is True and r3["new"] == [])
        # ★一覧ではない画面が返ったとき★（2026-07-31・Codex優先度3）
        _get = lambda u, timeout=20: _fake(u, _h="<p>ただいまメンテナンス中です</p>" + html)
        r_bad = scan_maker("t", {**conf, "min_expected": 2}, seen, record=False)
        t("★★メンテナンス・拒否・年齢確認の画面を一覧として読まない★★"
          "（そこそこリンクがあると件数の下限では通ってしまう）",
          r_bad["problem"] is not None and r_bad["state"] == "FETCH_FAILED")
        # ★一覧ページの印★（2026-07-31・Codex優先度2）
        _get = _fake
        titled = "<title>パチスロ機種一覧|テスト社</title>" + html
        _get = lambda u, timeout=20: _fake(u, _h=titled)   # noqa: E731
        r_mk = scan_maker("t", {**conf, "min_expected": 2,
                                "list_marker": "スロット機種"}, seen, record=False)
        t("★★一覧ページの題が印で始まらなければ『新台なし』と扱わない★★",
          r_mk["problem"] is not None and r_mk["state"] == "PARSE_SUSPECT")
        r_mk2 = scan_maker("t", {**conf, "min_expected": 2,
                                 "list_marker": "パチスロ機種一覧"}, seen, record=False)
        t("　題が印で始まれば通る", r_mk2["problem"] is None)
        machine_titled = "<title>スマスロ○○|パチスロ機種一覧|テスト社</title>" + html
        _get = lambda u, timeout=20: _fake(u, _h=machine_titled)   # noqa: E731
        r_mk3 = scan_maker("t", {**conf, "min_expected": 2,
                                 "list_marker": "パチスロ機種一覧"}, seen, record=False)
        t("★★機種ページの題（一覧の題を末尾に含む）を一覧と間違えない★★"
          "（本文で照合していた時は区別できなかった）",
          r_mk3["problem"] is not None)
        _get = _fake

        # ★別のドメインへ転送されたとき★（2026-07-31・Codex優先度1）
        _get = lambda u, timeout=20: (           # noqa: E731
            LAST_FINAL_URL.__setitem__("url", "https://よそ.example/top/") or html)
        r_red = scan_maker("t", {**conf, "min_expected": 2}, seen, record=False)
        t("★★別のドメインへ転送されたら『新台なし』と扱わない★★"
          "（正しいURLを叩いてもトップや別サイトが返ることがある）",
          r_red["problem"] is not None and r_red["state"] == "FETCH_FAILED")
        t("★★一覧を頼んだのにトップページへ飛ばされたら異常とする★★"
          "（山佐ネクストで実際に起きていた）",
          redirect_problem("https://www.x.example/machine/", "https://x.example/"))
        t("★★最終URLが分からないときは正常と言わない★★（Codex指摘・確認済み）",
          redirect_problem("https://x.example/machine/", None))
        t("★★同じサイトの中でも別のページへ飛ばされたら異常★★",
          redirect_problem("https://x.example/machine/", "https://x.example/products/"))
        t("★www の有無だけの転送は異常としない★",
          not redirect_problem("https://www.x.example/machine/",
                               "https://x.example/machine/"))
        t("　別のドメインへ飛んだら異常",
          redirect_problem("https://x.example/machine/",
                           "https://y.example/machine/"))
        _get = _fake
        r_ok = scan_maker("t", {**conf, "min_expected": 2}, seen, record=False)
        t("　同じドメインなら通る", r_ok["problem"] is None)

        # ★一覧が丸ごと別物に入れ替わったとき★（自分で再現した）
        many = "".join(f'<a href="/products/slot/new{i}/">x</a>' for i in range(55))
        old_seen = {"makers": {"t": {"urls": [f"{LIST}old{i}/" for i in range(60)]}}}
        _get = lambda u, timeout=20: _fake(u, _h=many)   # noqa: E731
        r5 = scan_maker("t", {**conf, "min_expected": 50}, old_seen, record=False)
        t("★★前に見たURLが大量に消えたら『新台』と扱わない★★"
          "（件数だけ見ていると55件が新台になった）",
          r5["problem"] is not None and r5["new"] == []
          and r5["state"] == "PARSE_SUSPECT")
        # ★一度に増えすぎたとき★
        base = [f"{LIST}a{i}/" for i in range(50)]
        grow = "".join(f'<a href="/products/slot/a{i}/">x</a>' for i in range(50)) +             "".join(f'<a href="/products/slot/z{i}/">x</a>' for i in range(20))
        _get = lambda u, timeout=20: _fake(u, _h=grow)   # noqa: E731
        r6 = scan_maker("t", {**conf, "min_expected": 50},
                        {"makers": {"t": {"urls": base}}}, record=False)
        t("★一度に増えすぎたときも『新台』と扱わない★",
          r6["problem"] is not None and r6["new"] == [])
        # ★普通に1件増えたときは通る★
        one = "".join(f'<a href="/products/slot/a{i}/">x</a>' for i in range(51))
        _get = lambda u, timeout=20: _fake(u, _h=one)    # noqa: E731
        r7 = scan_maker("t", {**conf, "min_expected": 50},
                        {"makers": {"t": {"urls": base}}}, record=False)
        t("　普通に1件増えたときはちゃんと新台として出る",
          r7["problem"] is None and r7["new"] == [f"{LIST}a50/"]
          and r7["state"] == "OK")
        _get = lambda u, timeout=20: (_ for _ in ()).throw(WatchError("落ちた"))  # noqa: E731
        r4 = scan_maker("t", conf2, seen, record=False)
        t("　取得に失敗したら理由を残して止まる（新台なしにしない）",
          r4["problem"] and r4["new"] == [])
    finally:
        _get = real_get

    from datetime import date
    TODAY = date(2026, 7, 31)
    t("★★古い機種のページを新台にしない★★（記録が消えても全機種が押し寄せない）",
      not is_recent("2024-12", TODAY) and not is_recent("2023-08", TODAY))
    t("　導入直後（先月）も拾う", is_recent("2026-06", TODAY))
    t("　事前告知（半年先まで）は拾う",
      is_recent("2026-08", TODAY) and is_recent("2027-01", TODAY))
    t("　それより先は拾わない（噂・別機種の混入を避ける）",
      not is_recent("2027-03", TODAY))
    t("★★ありえない月は通さない★★（13月が新台として通っていた・実際に再現）",
      not is_recent("2026-13", TODAY) and not is_recent("2026-00", TODAY)
      and not is_recent("2026-99", TODAY))
    t("　年月として読めない値は通さない",
      not is_recent("", TODAY) and not is_recent("2026", TODAY)
      and not is_recent("にせ-99", TODAY))
    t("★公式が書いた登場年月をそのまま持つ（日を補わない）★",
      release_month("2026年8月登場")["value"] == "2026-08"
      and release_month("2026年8月登場")["precision"] == "month")
    t("　scriptの中身を本文に混ぜない（偽の年月・数値を拾わない）",
      "パチスロ" not in _visible_text(
          '<script>var x="パチスロ純増99枚";</script><p>Lテスト機</p>'))
    t("★パチスロのページでなければ通さない★",
      not looks_like_slot("これは景品の紹介ページです"))

    t("★★『18歳未満』があっても、一覧の印と機種リンクがあれば止めない★★"
      "（パチスロメーカーのサイトには当たり前に書いてある・Codex指摘）",
      bad_page("<p>18歳未満の方は入場できません</p>", looks_like_list=True) is None)
    t("　一覧の証拠が無ければ、その語を根拠に止める",
      bad_page("<p>18歳未満の方は入場できません</p>", looks_like_list=False))
    t("★アクセス拒否・メンテナンスは、一覧の証拠があっても止める★",
      bad_page("<p>ただいまメンテナンス中です</p>", looks_like_list=True))
    t("★★残存率は丸める前の値で比べる★★（0.7996 が 0.8 になって通っていた）",
      (7996 / 10000) < RETENTION_MIN)
    t("★一度に増えてよいのは5件まで（割合で緩めない）★", MAX_NEW_PER_SCAN == 5)
    t("★覚え書きをメーカーとして数えない★",
      is_catalog({"status": "ACTIVE"}) and not is_catalog({"olympia": "平和に載る"}))
    t("★機種らしくない文字列は取らない★",
      not _SLUGLIKE.match("../etc") and not _SLUGLIKE.match("A B")
      and _SLUGLIKE.match("lbinko"))

    ng = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--scan", action="store_true", help="全メーカーを見る（記録を更新）")
    ap.add_argument("--check", help="1社だけ試す（記録を更新しない）")
    args = ap.parse_args()
    if args.selftest:
        return selftest()

    cats = _sj.read_json(CATALOGS, expect=dict)["catalogs"]
    if args.check:
        conf = cats.get(args.check)
        if not conf:
            print(f"★{args.check} は maker-catalogs.json にありません★")
            return 1
        seen = _load_seen()
        r = scan_maker(args.check, conf, seen, record=False)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 1 if r["problem"] else 0

    if args.scan:
        seen = _load_seen()
        problems, found = [], []
        for mid, conf in cats.items():
            if conf.get("status") != "ACTIVE":
                continue
            r = scan_maker(mid, conf, seen)
            if r["problem"]:
                problems.append(f"{mid}: {r['problem']}")
                continue
            if r["first_time"]:
                print(f"{mid}: 初回なので {r['total']} 件を記録しました（新台としては扱いません）")
                continue
            for u in r["new"]:
                found.append({"maker": mid, **describe(u)})
            print(f"{mid}: 一覧 {r['total']} 件 / 新台 {len(r['new'])} 件")
        _save_seen(seen)
        if found:
            print(chr(10) + "★新台候補★")
            print(json.dumps(found, ensure_ascii=False, indent=1))
        if problems:
            print(chr(10) + "★確認が要ります（新台なしとは扱いません）★")
            for p in problems:
                print("  ✗ " + p)
            return 1
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except WatchError as e:
        print(f"★{e}★")
        raise SystemExit(1)
    except _sj.SafeJsonError as e:
        print(f"★入力データが読めません: {e}★")
        raise SystemExit(1)
    except Exception as e:
        print(f"★想定外の失敗 {type(e).__name__}: {e}★")
        raise SystemExit(1)

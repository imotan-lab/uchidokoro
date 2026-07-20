# -*- coding: utf-8 -*-
"""出典実在チェッカー（決定論・LLM非依存）＝幻覚値の無人公開を機械的に阻止する関所。

自動タスク（auto-add/new-machine）がpreview→complete昇格や新規完全記事で
「外部から新しい数字」を書こうとする時、その数字の出典を★コードが★再取得して
検証する。AIの「読んだ」という主張を信用せず、文字列一致で機械確認する。

★これが防ぐもの★
  2026-04のokidoki_gorgeous事故: 自動タスクが5サイト照合したと主張した上で
  「スロパチ=580G」という実在しない値を報告した（幻覚）。本スクリプトなら
  スロパチのページを実際に取得→quoteの中に「580」が無い→不合格で止まる。

検証内容（1クレームごと）:
  C0. クレームの体裁（quoteがラベル語を含む8文字以上 / value・identityが退化していない /
      アーカイブ・キャッシュ・翻訳経由URLでない）＝退化クレームによる骨抜きを拒否
  C1. 出典URLが実在しHTTP 200で取得できる
  C2. ページ本文と<title>の★両方★に機種の同定文字列（identity.must_contain 全て）が存在する
      ＝旧機種・別機種・一覧ページを出典にしていないか（新旧混同ガード。
        旧機種ページの本文には後継機の告知が載ることがあるため、titleでの一致を必須にする）
  C3. 引用文（quote）が正規化一致でページ本文に実在する（逐語引用のみ合格）
  C4. 主張値（value）の数値が★quoteの中に★存在する（引用文がその値の証拠になっているか。
      ページのどこかに同じ数字がある、では合格しない）
集計判定:
  critical=true のフィールドは「合格クレームの出典ドメインが min-domains 種類以上」必須。
  ドメインはリダイレクト後の最終URLで数え、サブドメイン違い（m./www.等）は同一とみなす。
  ★フィールド名に 天井/機械割/恩恵/短縮 を含むもの（「狙い目」を含む場合を除く）は、
    claims側で critical:false と申告されていても critical=true に強制する（自己申告での緩和は不可）★
  critical=false は1ドメイン合格でOK。
  1つでも不合格クレームがあれば exit 1（昇格禁止）→ claimsは最小構成
  （フィールドごとに必要ドメイン数ちょうど）で書くこと。冗長クレームの失敗も全体不合格になる。

使い方:
  python scripts/verify_claims.py --file claims.json [--min-domains 2]
  python scripts/verify_claims.py --selftest   （ネット不要の内蔵テスト）
  exit 0 = 全関門通過（無人昇格を許可してよい）/ exit 1 = 不合格（preview維持＋台帳へ）

claims.json の形式:
{
  "slug": "yabachiba",
  "identity": { "must_contain": ["ヤバチバ"] },
  "claims": [
    { "field": "天井G数", "value": "999", "critical": true,
      "url": "https://...", "quote": "天井は999G+α" },
    { "field": "狙い目等価", "value": "700", "critical": false,
      "url": "https://...", "quote": "狙い目(等価) 700G~" }
  ]
}
★claims作成ルール（スクリプトが機械拒否する）★
  ・quoteは出典ページの原文を一字一句そのまま。かつ数値だけでなくラベル語を含めて
    正規化後8文字以上（例:「天井は999G+α」○ /「999G+α」×＝短すぎ不合格）
  ・identity.must_contain は「対象機種の解析ページの<title>にも現れる語」だけを選ぶ
    （機種名＋新旧判別トークン。例: ["からくりサーカス","2"]。年号・導入日は入れない）
  ・アーカイブ（web.archive.org等）・Googleキャッシュ・翻訳経由のURLは出典に使えない
  ・同一フィールドを複数URL（別ドメイン）で複数クレームにするとドメイン数が満たせる
"""
import argparse
import gzip
import hashlib
import html
import ipaddress
import json
import os
import re
import socket
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import namedtuple
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) uchidokoro-claim-verifier/1.0"
LOG_DIR = "C:/Users/imao_/Documents/uchidokoro/logs"
Page = namedtuple("Page", "text title final_url")
# ★D-1a構造保持取得用: 生HTML＋title＋最終URL＋本文hash（fetch_html が返す・fetch_pageは不変）
HtmlSnapshot = namedtuple("HtmlSnapshot", "html title final_url html_sha256")
_page_cache = {}
_html_cache = {}   # fetch_html の取得キャッシュ＋selftestモック差込口（_page_cache と対称）
_fetch_errors = {}
_MAX_FETCH_BYTES = 5_000_000        # 圧縮取得の上限（DoS/非HTML巨大対策・Codex E10/D-1a-2-1）
_MAX_DECODED_BYTES = 30_000_000     # gzip展開後の上限

_CJK = re.compile(r"[ぁ-ゟァ-ヿ一-鿿々]")
_FORCE_CRITICAL = ("天井", "機械割", "恩恵", "短縮")
_BLOCKED_HOSTS = (
    "web.archive.org", "archive.org", "archive.today", "archive.ph", "archive.is",
    "megalodon.jp", "webcache.googleusercontent.com", "translate.goog",
)
_JP_SLD = {"co.jp", "ne.jp", "or.jp", "go.jp", "ac.jp", "ed.jp", "lg.jp"}


def _now():
    return datetime.now().strftime("%H:%M:%S")


def log(msg):
    """コンソールとファイルの両方に出力（鉄則: 全スクリプトにファイルログ）。"""
    line = f"[{_now()}] {msg}"
    print(line)
    try:
        path = os.path.join(LOG_DIR, f"verify_claims_{datetime.now().strftime('%Y-%m-%d')}.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass  # ログ失敗で検証自体は止めない


def _decode(raw, header_charset):
    """charset宣言（HTTPヘッダ→meta）を優先し、最後は置換デコードで必ず文字列を返す。"""
    candidates = []
    if header_charset:
        candidates.append(header_charset)
    m = re.search(rb"charset=[\"\']?([A-Za-z0-9_\-]+)", raw[:4096])
    if m:
        candidates.append(m.group(1).decode("ascii", "ignore"))
    candidates += ["utf-8", "cp932", "euc-jp"]
    for enc in candidates:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")  # 文字化けはC2同定で安全側不合格になる


# 実在するUnicode符号位置は最大 0x10FFFF＝10進7桁/16進6桁。20桁超の数値文字参照は異常＝整数化DoS狙い。
_CHARREF_MAX_DIGITS = 20


def _has_overlong_numeric_charref(s):
    """★生HTMLに「桁数が異常に長い数値文字参照 &#…; / &#x…;」があるか（html.unescape/HTMLParser前に線形検査）★
    html.unescape と HTMLParser(convert_charrefs=True) は数値文字参照を int() で復号するため、数百万桁の
    &#999…; を通すと（Pythonの整数文字列変換桁数制限が無効化された環境で）整数化CPU DoSになる。
    ★取得直後・title/本文の unescape・平坦化・parser の前にこれで検出し取得拒否する（桁制限に依存しない安全弁）★。
    計算量は O(len(s))＝str.find は前回一致位置以降のみ走査／桁計上は上限で早期打ち切り。"""
    n = len(s)
    i = s.find("&#")
    while i != -1:
        j = i + 2
        if j < n and s[j] in ("x", "X"):     # 16進 &#x...
            j += 1
            k = j
            while k < n and s[k] in "0123456789abcdefABCDEF":
                k += 1
                if k - j > _CHARREF_MAX_DIGITS:
                    return True
        else:                                 # 10進 &#... （ASCII 0-9 のみ・html仕様の[0-9]+と同義）
            k = j
            while k < n and "0" <= s[k] <= "9":
                k += 1
                if k - j > _CHARREF_MAX_DIGITS:
                    return True
        i = s.find("&#", i + 2)               # 次の &# へ（前回位置以降のみ＝全体で線形）
    return False


_allowed_openers = {}


def _opener_for(allowed):
    """許可ドメインごとにリダイレクト検査opener をキャッシュ（allowed=Noneは公開IP検査のみ）。"""
    key = None if not allowed else tuple(sorted(str(d).lower() for d in allowed))
    if key not in _allowed_openers:
        _allowed_openers[key] = urllib.request.build_opener(_ValidatingRedirect(allowed))
    return _allowed_openers[key]


def _gunzip_limited(raw, limit):
    """gzip(連結メンバー対応)を limit バイトまでストリーミング展開。上限超過(gzip爆弾)・破損・途中切れ・空 は None。"""
    if not raw:
        return None  # 空入力（Content-Encoding: gzip なのに本文空＝途中切れ）は展開成功扱いしない
    out = bytearray()
    remaining = raw
    while remaining:
        d = zlib.decompressobj(31)   # 31 = gzip ヘッダ付き
        try:
            while True:
                chunk = d.decompress(remaining, 1 << 16)
                out.extend(chunk)
                if len(out) > limit:
                    return None
                remaining = d.unconsumed_tail
                if d.eof:
                    break
                if not chunk and not remaining:
                    return None
            remaining = d.unused_data
        except zlib.error:
            return None
    return bytes(out)


def _flatten_html(text):
    """HTMLタグ/コメント/script/styleを除去して本文テキストへ平坦化（fetch_page/selftest共通）。"""
    body = re.sub(r"(?s)<!--.*?-->", " ", text)
    body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", body)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    return html.unescape(body)


def page_from_html_snapshot(snapshot):
    """HtmlSnapshot(生HTML) を fetch_page 相当の Page(平坦本文) に変換（同一スナップショット再利用）。"""
    return Page(_flatten_html(snapshot.html), snapshot.title, snapshot.final_url)


def _fetch_decoded_html(url, allowed=None):
    """★fetch_page/fetch_html 共通の取得実装（取得処理を複製しない）★
    取得＋SSRF＋許可ドメイン＋取得/展開サイズ上限＋gzip＋charset＋最終URL許可再検査。
    戻り (decoded_html, title, final_url) または None（失敗・安全側）。
    ※同一スナップショットの共有は fetch_html→page_from_html_snapshot 経由の時のみ成立
      （fetch_page と fetch_html を別々に呼ぶと2回取得になる＝呼出側の契約）。"""
    safe, why = (validate_source_url(url, allowed=allowed) if allowed
                 else is_public_fetchable_url(url))
    if not safe:
        _fetch_errors[url] = f"取得拒否: {why}"
        return None
    last_err = ""
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
            with _opener_for(allowed).open(req, timeout=25) as res:
                if res.status != 200:
                    last_err = f"HTTP {res.status}"
                    continue
                raw = res.read(_MAX_FETCH_BYTES + 1)          # ★取得サイズ上限
                if len(raw) > _MAX_FETCH_BYTES:
                    _fetch_errors[url] = "取得サイズ上限超過"
                    return None
                final_url = res.geturl()
                enc_hdr = (res.headers.get("Content-Encoding") or "").strip().lower()
                header_charset = res.headers.get_content_charset()
            # ★Content-Encodingはヘッダー優先で判定（未対応encodingをマジック詮索より先に拒否）★
            #   gzip のみ展開。identity/空/未宣言はそのまま（HTTP準拠＝宣言なくgzip本文を送る鯖は想定外→
            #   デコード失敗しC2同定で安全側不合格になる）。br/deflate等は取得段階で拒否。
            if enc_hdr not in ("", "identity", "gzip"):
                last_err = f"未対応のContent-Encoding: {enc_hdr}"
                continue
            if enc_hdr == "gzip":
                raw = _gunzip_limited(raw, _MAX_DECODED_BYTES)  # ★gzip爆弾対策: 上限までストリーミング展開
                if raw is None:
                    last_err = "gzip展開失敗/上限超過/空"
                    continue
            if len(raw) > _MAX_DECODED_BYTES:                 # ★展開後サイズ上限（非圧縮も含む）
                _fetch_errors[url] = "展開後サイズ上限超過"
                return None
            text = _decode(raw, header_charset)
            # ★title/本文の html.unescape・平坦化・parser の前に巨大数値文字参照を止める（整数化DoS防止・
            #   Pythonの桁数制限に依存しない）。fetch_page(本番)・fetch_html(consensus) 双方をここで保護。★
            if _has_overlong_numeric_charref(text):
                _fetch_errors[url] = "巨大な数値文字参照（整数化DoS）→取得拒否"
                return None
            tm = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
            title = html.unescape(tm.group(1)) if tm else ""
            # 最終URLの許可ドメイン再確認（リダイレクトで許可外へ出ていないか・全ホップ強制の総仕上げ）
            if allowed and not host_matches_allowlist(final_url, allowed):
                _fetch_errors[url] = f"最終URLが許可ドメイン外: {final_url}"
                return None
            return (text, title, final_url)
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            continue
    _fetch_errors[url] = last_err or "不明"
    return None


def fetch_html(url, allowed=None):
    """★D-1a構造保持取得用: 生HTML(decoded)＋title＋最終URL＋hash を返す（SSRF等はfetch_pageと共通）。★
    失敗はNone。fetch_pageと同一検証内で別々に呼ばないこと（同一スナップショット要件）。
    キャッシュ/モックは _html_cache（fetch_page の _page_cache と対称・selftestの合成HTML差込口）。"""
    ck = (url, None if not allowed else tuple(sorted(str(d).lower() for d in allowed)))
    if ck in _html_cache:
        return _html_cache[ck]
    if url in _html_cache:  # 後方互換（selftestの合成HtmlSnapshot・素のurlキー）
        return _html_cache[url]
    r = _fetch_decoded_html(url, allowed)
    if r is None:
        _html_cache[ck] = None
        return None
    text, title, final_url = r
    snap = HtmlSnapshot(text, title, final_url,
                        hashlib.sha256(text.encode("utf-8", "replace")).hexdigest())
    _html_cache[ck] = snap
    return snap


def fetch_page(url, allowed=None):
    """URLを取得し (本文テキスト, title, 最終URL) を返す。失敗はNone（安全側）。★外部挙動は従来と不変★
    allowed指定時は取得前URL・全リダイレクト・最終URLの全ホップで許可ドメインを強制。
    取得処理は _fetch_decoded_html に集約（複製しない・2026-07-19 D-1a-2-1）。"""
    ck = (url, None if not allowed else tuple(sorted(str(d).lower() for d in allowed)))
    if ck in _page_cache:
        return _page_cache[ck]
    if url in _page_cache:  # 後方互換（selftestの合成ページ・素のurlキー）
        return _page_cache[url]
    r = _fetch_decoded_html(url, allowed)
    if r is None:
        _page_cache[ck] = None
        return None
    text, title, final_url = r
    page = Page(_flatten_html(text), title, final_url)  # コメント/script/style除去→タグ平坦化
    _page_cache[ck] = page
    return page


_TRANS = str.maketrans({
    "〜": "~",  # 〜 WAVE DASH
    "～": "~",  # ～ FULLWIDTH TILDE
    "−": "-",  # − MINUS SIGN
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-",
})


def normalize(s):
    """NFKC正規化＋空白全除去＋チルダ/ダッシュ統一。表記ゆれで誤不合格にならない範囲の吸収のみ。"""
    s = unicodedata.normalize("NFKC", s or "")
    s = s.translate(_TRANS)
    return re.sub(r"\s+", "", s)


def normalize_spaced(s):
    """C4の桁境界判定用：空白を除去せず1個に圧縮する正規化（2026-07-13追加）。
    空白全除去のnormalize()だと、表組みページで隣接セルの数字同士が連結し
    （例: 合算「1/163.9」の直後に出玉率「97.0%」→「163.997.0%」）、ページに実在する値まで
    桁境界NGになる偽陰性があった。値の同定(C4)はこちらで判定する。
    C3の逐語一致は従来どおりnormalize()＝引用の厳格さは緩めない。"""
    s = unicodedata.normalize("NFKC", s or "")
    s = s.translate(_TRANS)
    return re.sub(r"\s+", " ", s).strip()


def domain_of(url):
    """ドメインをeTLD+1相当に丸める（m./www.等のサブドメイン違いを同一ソース扱いにする）。"""
    d = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
    labels = [x for x in d.split(".") if x]
    if len(labels) >= 3 and ".".join(labels[-2:]) in _JP_SLD:
        return ".".join(labels[-3:])
    if len(labels) >= 2:
        return ".".join(labels[-2:])
    return d


def is_blocked_source(url):
    d = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
    return any(d == b or d.endswith("." + b) for b in _BLOCKED_HOSTS)


# ─────────────────────────────────────────────
# 共有URL検証（SSRF対策・gold/日次/verify_claimsで同一関数を使う・2026-07-18）
# チャッピー第2次シャドーレビュー指摘3への対応。`d in url` の部分文字列一致は
# 「許可ドメイン名をクエリ等に含む無関係URL」を通すためSSRFにならない。
# ─────────────────────────────────────────────

def _ip_is_public(host: str):
    """hostがIPリテラルならグローバルか判定。IPでなければNone（＝ホスト名）。"""
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return None


def is_public_fetchable_url(url: str, _resolve=True):
    """取得許可の安全判定。戻り値 (ok, reason)。
    https限定・userinfo禁止・localhost/IPリテラル(loopback/private)拒否・
    ホスト名はDNS解決先が全てグローバルIPであることを確認（SSRF防御）。"""
    try:
        u = urllib.parse.urlparse(url)
    except Exception as e:
        return False, f"URL解析不能: {type(e).__name__}"
    if u.scheme != "https":
        return False, f"httpsのみ許可（scheme={u.scheme or 'なし'}）"
    if u.username or u.password or "@" in (u.netloc or ""):
        return False, "userinfo付きURLは拒否"
    host = (u.hostname or "").lower().rstrip(".")
    if not host:
        return False, "hostnameなし"
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return False, "localhost系は拒否"
    lit = _ip_is_public(host)
    if lit is False:
        return False, "非グローバルIP(loopback/private/reserved)は拒否"
    if lit is None and _resolve:
        try:
            infos = socket.getaddrinfo(host, u.port or 443, proto=socket.IPPROTO_TCP)
        except Exception as e:
            return False, f"DNS解決失敗: {type(e).__name__}"
        for info in infos:
            ip = info[4][0]
            try:
                if not ipaddress.ip_address(ip).is_global:
                    return False, f"解決先が非グローバルIP({ip})"
            except ValueError:
                return False, "解決IP判定不能"
    return True, "ok"


def host_matches_allowlist(url: str, allowed) -> bool:
    """hostnameが許可ドメインと完全一致 or 正規サブドメインか（部分文字列一致は使わない）。"""
    host = (urllib.parse.urlparse(url).hostname or "").lower().rstrip(".")
    if not host:
        return False
    for d in allowed:
        d = str(d).lower().strip(".")
        if host == d or host.endswith("." + d):
            return True
    return False


def validate_source_url(url: str, allowed=None, resolve=True):
    """出典URLの総合検査（安全＋許可リスト）。戻り値 (ok, reason)。
    allowed指定時は許可ドメイン完全一致/正規サブドメインを追加要求する。"""
    ok, reason = is_public_fetchable_url(url, _resolve=resolve)
    if not ok:
        return False, reason
    if is_blocked_source(url):
        return False, "アーカイブ/キャッシュ/翻訳経由URLは拒否"
    if allowed is not None and not host_matches_allowlist(url, allowed):
        return False, "許可ドメイン外（完全一致/正規サブドメインのみ）"
    return True, "ok"


class _ValidatingRedirect(urllib.request.HTTPRedirectHandler):
    """リダイレクト先も同じ安全検査にかける（SSRFのリダイレクト回避を塞ぐ）。
    allowed指定時は各リダイレクトホップで許可ドメインも強制する。"""
    def __init__(self, allowed=None):
        self.allowed = allowed

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        ok, reason = is_public_fetchable_url(newurl)
        if ok and self.allowed and not host_matches_allowlist(newurl, self.allowed):
            ok, reason = False, "許可ドメイン外へのリダイレクト"
        if not ok:
            raise urllib.error.HTTPError(newurl, code,
                                         f"redirect blocked: {reason}", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_SAFE_OPENER = urllib.request.build_opener(_ValidatingRedirect())


def _value_tokens(nvalue):
    return re.findall(r"\d+(?:\.\d+)?", nvalue)


def _token_in(token, hay):
    """数字トークンを桁境界付きで探す（"700"が"1700"や"70023"に誤一致しない）。"""
    return re.search(rf"(?<![0-9.]){re.escape(token)}(?![0-9.])", hay) is not None


def _ident_in(s, hay):
    if s.isdigit():
        return _token_in(s, hay)  # "2"等の判別トークンは桁境界付きで（"2026"に誤一致しない）
    return s in hay


def run_data(data, min_domains, allowed_domains=None):
    slug = data.get("slug", "?")
    raw_identity = (data.get("identity") or {}).get("must_contain", [])
    claims = data.get("claims") or []
    # ★許可ドメイン: dataのfieldでも上書き可（呼び出し側がJSONに埋めても、CLIで渡してもよい）★
    allowed_domains = allowed_domains or data.get("allowed_domains")
    log(f"=== 出典実在チェック開始: {slug}（クレーム{len(claims)}件・critical必要ドメイン数{min_domains}"
        + (f"・許可ドメイン{list(allowed_domains)}" if allowed_domains else "") + "） ===")

    identity = []
    for s in raw_identity:
        ns = normalize(str(s))
        if not ns:
            log("❌ identity.must_contain に正規化後空になる要素（空文字/空白のみ）→不合格")
            return 1
        identity.append(ns)
    if not identity:
        log("❌ identity.must_contain が空（同定ガード必須）→不合格")
        return 1
    if not any(len(s) >= 3 for s in identity):
        log("❌ identity.must_contain に機種名に相当する3文字以上の要素が無い→不合格")
        return 1
    if not claims:
        log("❌ クレーム0件→不合格")
        return 1

    field_pass_domains = {}
    field_critical = {}
    any_fail = False

    for i, c in enumerate(claims):
        if not isinstance(c, dict):
            log(f"❌ claims[{i}] がオブジェクトでない→不合格")
            any_fail = True
            continue
        field = c.get("field", f"claim{i}")
        url = c.get("url", "")
        quote = c.get("quote", "")
        value = str(c.get("value", ""))

        # criticalの自己申告での緩和は不可（天井/機械割/恩恵/短縮はコード側で強制）
        declared = bool(c.get("critical", True))
        forced = any(p in field for p in _FORCE_CRITICAL) and ("狙い目" not in field)
        critical = declared or forced
        if forced and not declared:
            log(f"⚠ [{field}]: critical:false申告だがフィールド名により critical=true に強制（自己申告での緩和は不可）")
        field_critical[field] = field_critical.get(field, False) or critical
        tag = f"[{field}] {domain_of(url)}"

        # C0: クレームの体裁（退化クレーム拒否）
        nquote = normalize(quote)
        nvalue = normalize(value)
        if not url or not nquote or not nvalue:
            log(f"❌ {tag}: C0 url/quote/value のいずれかが空（正規化後空を含む）→不合格")
            any_fail = True
            continue
        if len(nquote) < 8 or not _CJK.search(nquote):
            log(f"❌ {tag}: C0 quoteが退化引用（8文字未満 or ラベル語なし）。数値だけでなく「天井」「狙い目」等のラベル語を含む原文を引用すること: 「{quote}」")
            any_fail = True
            continue
        if is_blocked_source(url):
            log(f"❌ {tag}: C0 アーカイブ/キャッシュ/翻訳経由のURLは出典に使えない（{url}）")
            any_fail = True
            continue
        # C0: 許可ドメイン強制（取得前・部分文字列でなく完全一致/正規サブドメイン）
        if allowed_domains and not host_matches_allowlist(url, allowed_domains):
            log(f"❌ {tag}: C0 出典URLが許可ドメイン外（{url}）→不合格")
            any_fail = True
            continue

        # C4: 値がquoteの中に実在するか（引用が値の証拠か）※C3より先に検査できる決定論チェック
        tokens = _value_tokens(nvalue)
        if tokens:
            # 桁境界は空白保持の正規化で判定（表出典で隣接セルと数字連結する偽陰性の回避・2026-07-13）
            nquote_sp = normalize_spaced(quote)
            missing_tok = [t for t in tokens if not _token_in(t, nquote_sp)]
            if missing_tok:
                log(f"❌ {tag}: C4 値の数値 {missing_tok} が引用文の中に無い（引用が値の証拠になっていない・取り違え/幻覚の疑い）")
                any_fail = True
                continue
        elif nvalue not in nquote:
            log(f"❌ {tag}: C4 値「{value}」が引用文の中に無い")
            any_fail = True
            continue

        # C1: 取得（allowed指定時は取得前・全リダイレクト・最終URLで許可ドメイン強制）
        page = fetch_page(url, allowed=allowed_domains)
        if page is None:
            log(f"❌ {tag}: C1 URL取得失敗（{url} / 理由: {_fetch_errors.get(url, '不明')}）")
            any_fail = True
            continue
        if is_blocked_source(page.final_url):
            log(f"❌ {tag}: C0 リダイレクト先がアーカイブ/キャッシュ（{page.final_url}）→出典に使えない")
            any_fail = True
            continue
        # ★リダイレクトで最終URLが変わった場合はSSRF＋許可ドメイン再検査（最終URL・指摘3/再指摘1）★
        #   各リダイレクトホップは_ValidatingRedirectが検査済み。ここは最終URLの明示再確認。
        if page.final_url != url:
            fu_ok, fu_why = is_public_fetchable_url(page.final_url)
            if fu_ok and allowed_domains and not host_matches_allowlist(page.final_url, allowed_domains):
                fu_ok, fu_why = False, "最終URLが許可ドメイン外"
            if not fu_ok:
                log(f"❌ {tag}: C1 リダイレクト最終URLが不合格（{page.final_url} / {fu_why}）")
                any_fail = True
                continue
        ntext = normalize(page.text)
        ntitle = normalize(page.title)

        # C2: 同定（本文＋titleの両方。旧機種ページ・一覧ページ対策）
        missing_id = [s for s in identity if not _ident_in(s, ntext)]
        if missing_id:
            log(f"❌ {tag}: C2 同定文字列がページ本文に無い {missing_id}（別機種/旧機種ページの疑い）")
            any_fail = True
            continue
        if not ntitle:
            log(f"❌ {tag}: C2 <title>が取得できない（一覧/異常ページの疑い）→不合格")
            any_fail = True
            continue
        missing_title = [s for s in identity if not _ident_in(s, ntitle)]
        if missing_title:
            log(f"❌ {tag}: C2 同定文字列がページタイトルに無い {missing_title}（旧機種ページに後継機告知が載っているだけ・一覧ページ等の疑い。title=「{page.title.strip()[:60]}」）")
            any_fail = True
            continue

        # C3: 逐語引用がページに実在
        if nquote not in ntext:
            log(f"❌ {tag}: C3 引用文がページに実在しない（幻覚/言い換えの疑い）: 「{quote}」")
            any_fail = True
            continue

        field_pass_domains.setdefault(field, set()).add(domain_of(page.final_url))
        log(f"✅ {tag}: C0〜C4通過（quote実在・値はquote内・title同定OK）")

    verdict_fail = any_fail
    for field, critical in field_critical.items():
        got = len(field_pass_domains.get(field, set()))
        need = min_domains if critical else 1
        status = "✅" if got >= need else "❌"
        if got < need:
            verdict_fail = True
        log(f"{status} 集計[{field}]: 合格ドメイン{got}/{need}必要{'（critical）' if critical else ''}")

    if verdict_fail:
        log("=== 判定: ❌ 不合格 → 無人昇格禁止（preview維持＋要確認台帳へ） ===")
        return 1
    log("=== 判定: ✅ 全関門通過 → 検証済みの値のみで昇格可 ===")
    return 0


def run(path, min_domains, allowed_domains=None):
    with open(path, encoding="utf-8") as f:
        data = json.loads(f.read())
    return run_data(data, min_domains, allowed_domains=allowed_domains)


# ------------------------------------------------------------------
# 内蔵セルフテスト（ネット不要・_page_cacheに合成ページを注入して検証）
# ------------------------------------------------------------------
def selftest():
    new_body = ("Lからくりサーカス2の解析ページです。天井は999G+αでAT直撃の恩恵。"
                "狙い目(等価) 700G~ が目安。機械割は97.7%～114.9%。設定6は114.9%。"
                "参考: 中古価格 15800円")
    new_title = "Lからくりサーカス2 天井・狙い目・やめどき解析"
    old_body = ("からくりサーカスの解析ページです。天井は777G+α。"
                "後継機『からくりサーカス2』が2026年に登場予定です。")
    old_title = "からくりサーカス 天井・狙い目解析"

    _page_cache.clear()
    _page_cache["https://sloquest.test/karakuri2"] = Page(new_body, new_title, "https://sloquest.test/karakuri2")
    _page_cache["https://m.sloquest.test/karakuri2"] = Page(new_body, new_title, "https://m.sloquest.test/karakuri2")
    _page_cache["https://chonborista.jp/karakuri2"] = Page(new_body, new_title, "https://chonborista.jp/karakuri2")
    _page_cache["https://sloquest.test/karakuri-old"] = Page(old_body, old_title, "https://sloquest.test/karakuri-old")
    _page_cache["https://notitle.test/karakuri2"] = Page(new_body, "", "https://notitle.test/karakuri2")

    IDENT = {"must_contain": ["からくりサーカス", "2"]}

    def case(name, data, expect, min_domains=2):
        got = run_data(data, min_domains)
        ok = got == expect
        log(f"{'✅' if ok else '❌'} selftest[{name}]: expect={expect} got={got}")
        return ok

    results = []
    # 1. 正常系: critical天井2ドメイン＋狙い目1ドメイン → 合格
    results.append(case("正常系合格", {
        "slug": "karakuri2", "identity": IDENT, "claims": [
            {"field": "天井G数", "value": "999", "critical": True,
             "url": "https://sloquest.test/karakuri2", "quote": "天井は999G+α"},
            {"field": "天井G数", "value": "999", "critical": True,
             "url": "https://chonborista.jp/karakuri2", "quote": "天井は999G+α"},
            {"field": "狙い目等価", "value": "700", "critical": False,
             "url": "https://sloquest.test/karakuri2", "quote": "狙い目(等価) 700G~"},
            {"field": "機械割", "value": "97.7~114.9", "critical": True,
             "url": "https://sloquest.test/karakuri2", "quote": "機械割は97.7%～114.9%"},
            {"field": "機械割", "value": "97.7~114.9", "critical": True,
             "url": "https://chonborista.jp/karakuri2", "quote": "機械割は97.7%～114.9%"},
        ]}, 0))
    # 2. 幻覚値: 実在quote＋捏造value(580はページの15800円に部分一致するがquoteに無い) → 不合格
    results.append(case("幻覚値はC4で不合格", {
        "slug": "karakuri2", "identity": IDENT, "claims": [
            {"field": "狙い目等価", "value": "580", "critical": False,
             "url": "https://sloquest.test/karakuri2", "quote": "狙い目(等価) 700G~"},
        ]}, 1))
    # 3. 退化quote（全角スペース） → 不合格
    results.append(case("退化quote不合格", {
        "slug": "karakuri2", "identity": IDENT, "claims": [
            {"field": "天井G数", "value": "999", "url": "https://sloquest.test/karakuri2", "quote": "　"},
        ]}, 1))
    # 4. 数値のみの短quote → 不合格
    results.append(case("数値のみquote不合格", {
        "slug": "karakuri2", "identity": IDENT, "claims": [
            {"field": "天井G数", "value": "999", "url": "https://sloquest.test/karakuri2", "quote": "999"},
        ]}, 1))
    # 5. 天井にcritical:false申告 → 強制criticalで1ドメインでは不合格
    results.append(case("critical自己申告の緩和は不可", {
        "slug": "karakuri2", "identity": IDENT, "claims": [
            {"field": "天井G数", "value": "999", "critical": False,
             "url": "https://sloquest.test/karakuri2", "quote": "天井は999G+α"},
        ]}, 1))
    # 6. 旧機種ページ（本文に後継機告知あり・titleに"2"なし） → C2で不合格
    results.append(case("旧機種ページはtitle同定で不合格", {
        "slug": "karakuri2", "identity": IDENT, "claims": [
            {"field": "天井G数", "value": "777", "critical": False,
             "url": "https://sloquest.test/karakuri-old", "quote": "天井は777G+α"},
        ]}, 1))
    # 7. モバイル版＋PC版の同一サイト2URL → 1ドメイン扱いでcritical不合格
    results.append(case("サブドメイン水増し無効", {
        "slug": "karakuri2", "identity": IDENT, "claims": [
            {"field": "天井G数", "value": "999", "critical": True,
             "url": "https://sloquest.test/karakuri2", "quote": "天井は999G+α"},
            {"field": "天井G数", "value": "999", "critical": True,
             "url": "https://m.sloquest.test/karakuri2", "quote": "天井は999G+α"},
        ]}, 1))
    # 8. アーカイブURL → 不合格
    results.append(case("アーカイブURL不合格", {
        "slug": "karakuri2", "identity": IDENT, "claims": [
            {"field": "天井G数", "value": "999",
             "url": "https://web.archive.org/web/2026/https://sloquest.test/karakuri2",
             "quote": "天井は999G+α"},
        ]}, 1))
    # 9. titleが取得できないページ → 不合格
    results.append(case("title無しページ不合格", {
        "slug": "karakuri2", "identity": IDENT, "claims": [
            {"field": "天井G数", "value": "999",
             "url": "https://notitle.test/karakuri2", "quote": "天井は999G+α"},
        ]}, 1))
    # 10. identityが空/退化 → 不合格
    results.append(case("identity退化不合格", {
        "slug": "karakuri2", "identity": {"must_contain": ["　"]}, "claims": [
            {"field": "天井G数", "value": "999",
             "url": "https://sloquest.test/karakuri2", "quote": "天井は999G+α"},
        ]}, 1))

    _page_cache.clear()

    # 11. ★共有URL検証（SSRF対策・2026-07-18）★
    def ucase(name, cond):
        results.append(cond)
        log(f"{'✅' if cond else '❌'} selftest[{name}]: {cond}")

    ucase("https以外を拒否", is_public_fetchable_url("http://1geki.jp/a", _resolve=False)[0] is False)
    ucase("userinfo付きを拒否",
          is_public_fetchable_url("https://user@1geki.jp/a", _resolve=False)[0] is False)
    ucase("localhostを拒否", is_public_fetchable_url("https://localhost/a", _resolve=False)[0] is False)
    ucase("プライベートIPを拒否",
          is_public_fetchable_url("https://192.168.0.1/a", _resolve=False)[0] is False)
    ucase("loopback IPを拒否",
          is_public_fetchable_url("https://127.0.0.1/a", _resolve=False)[0] is False)
    ucase("許可ドメイン部分文字列は不一致（SSRF芽）",
          host_matches_allowlist("https://evil.example.com/?x=1geki.jp",
                                 ["1geki.jp"]) is False)
    ucase("許可ドメイン完全一致",
          host_matches_allowlist("https://1geki.jp/a", ["1geki.jp"]) is True)
    ucase("正規サブドメインは一致",
          host_matches_allowlist("https://m.nana-press.com/a", ["nana-press.com"]) is True)
    ucase("別ドメインは不一致",
          host_matches_allowlist("https://1geki.jp.evil.com/a", ["1geki.jp"]) is False)
    ucase("validate_source_url: 許可外ドメイン拒否",
          validate_source_url("https://example.com/a", allowed=["1geki.jp"],
                              resolve=False)[0] is False)
    ucase("validate_source_url: アーカイブ拒否",
          validate_source_url("https://web.archive.org/1geki.jp", allowed=None,
                              resolve=False)[0] is False)

    # ★許可4ドメイン強制（数値claim経路・再指摘1）: 許可外ドメインのURLはC0で不合格★
    _page_cache.clear()
    good_body = ("Lからくりサーカス2の解析。天井は999G+αでAT直撃の恩恵。"
                 "設定6は114.9%。")
    good_title = "Lからくりサーカス2 天井解析"
    # 許可外ドメイン(evil.example.com)にページが実在してもallowed指定で弾く
    _page_cache[("https://evil.example.com/k", ("1geki.jp",))] = Page(
        good_body, good_title, "https://evil.example.com/k")
    rc_dis = run_data({"slug": "k", "identity": {"must_contain": ["からくりサーカス", "2"]},
                       "claims": [{"field": "天井G数", "value": "999", "critical": True,
                                   "url": "https://evil.example.com/k",
                                   "quote": "天井は999G+α"}]}, 1, allowed_domains=["1geki.jp"])
    ucase("許可4ドメイン強制: 許可外ドメインの数値claimはC0不合格", rc_dis == 1)
    # 許可ドメインなら同じ内容が通る（allowedキー付きキャッシュ）
    _page_cache[("https://1geki.jp/k", ("1geki.jp",))] = Page(
        good_body, good_title, "https://1geki.jp/k")
    rc_ok = run_data({"slug": "k", "identity": {"must_contain": ["からくりサーカス", "2"]},
                      "claims": [{"field": "天井G数", "value": "999", "critical": True,
                                  "url": "https://1geki.jp/k",
                                  "quote": "天井は999G+α"}]}, 1, allowed_domains=["1geki.jp"])
    ucase("許可4ドメイン強制: 許可ドメインの数値claimは通過", rc_ok == 0)
    _page_cache.clear()

    # ★gzip爆弾対策（_gunzip_limited・2026-07-19 D-1a-2-1）★
    ucase("gunzip: 正常gzipを展開",
          _gunzip_limited(gzip.compress(b"hello world"), 1000) == b"hello world")
    ucase("gunzip: gzip爆弾は上限超過でNone",
          _gunzip_limited(gzip.compress(b"x" * 10000), 100) is None)
    ucase("gunzip: 破損gzipはNone",
          _gunzip_limited(b"\x1f\x8b" + b"\x00" * 40, 1000) is None)
    ucase("gunzip: 空入力はNone（途中切れ扱い）",
          _gunzip_limited(b"", 1000) is None)
    ucase("gunzip: 連結メンバーを結合展開",
          _gunzip_limited(gzip.compress(b"aaa") + gzip.compress(b"bbb"), 1000) == b"aaabbb")
    ucase("gunzip: 展開後が上限ちょうどはOK",
          _gunzip_limited(gzip.compress(b"12345"), 5) == b"12345")
    ucase("gunzip: 上限+1バイトはNone",
          _gunzip_limited(gzip.compress(b"123456"), 5) is None)

    # ★fetch_html のキャッシュ/モック契約（_html_cache 経由・D-1a-2-2）★
    _html_cache.clear()
    _hs = HtmlSnapshot("<h1>x</h1>", "T", "https://x.test/a", "deadbeef")
    _html_cache["https://x.test/a"] = _hs
    ucase("fetch_html: _html_cacheの合成HtmlSnapshotを返す",
          fetch_html("https://x.test/a") is _hs)
    _html_cache.clear()

    # ★巨大数値文字参照の整数化DoS（Codex第5次）: 検出関数＋fetch層の関所（title unescape 前）★
    ucase("charref: 巨大10進 &#…; を検出", _has_overlong_numeric_charref("<td x='&#" + "9" * 100 + ";'>"))
    ucase("charref: 巨大16進 &#x…; を検出", _has_overlong_numeric_charref("a&#x" + "9" * 100 + ";b"))
    ucase("charref: <title>内の巨大charrefも検出", _has_overlong_numeric_charref("<title>&#" + "9" * 100 + ";</title>"))
    ucase("charref: 正常な短いcharrefは誤検知しない", not _has_overlong_numeric_charref("a&#12539;b&#x5929;c"))

    import time as _t_mod

    class _FakeHeaders:
        def get(self, k, default=None):
            return default
        def get_content_charset(self):
            return "utf-8"

    class _FakeResp:
        def __init__(self, data, url):
            self._d, self._u, self.status, self.headers = data, url, 200, _FakeHeaders()
        def read(self, n=-1):
            return self._d
        def geturl(self):
            return self._u
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class _FakeOpener:
        def __init__(self, d, u):
            self._d, self._u = d, u
        def open(self, req, timeout=None):
            return _FakeResp(self._d, self._u)

    _g = globals()
    _save_ipf, _save_open = _g["is_public_fetchable_url"], _g["_opener_for"]
    _save_limit = sys.get_int_max_str_digits() if hasattr(sys, "get_int_max_str_digits") else None
    try:
        if _save_limit is not None:
            sys.set_int_max_str_digits(0)   # 桁制限を無効化＝int() DoSが顕在化する条件（この状態で有界を実証）
        _url = "https://example.test/hugetitle"
        _big = ("<html><head><title>&#" + "9" * 3_000_000
                + ";</title></head><body>x</body></html>").encode("utf-8")
        _g["is_public_fetchable_url"] = lambda u, _resolve=True: (True, "ok")
        _g["_opener_for"] = lambda allowed: _FakeOpener(_big, _url)
        _t0 = _t_mod.time()
        _res = _fetch_decoded_html(_url)
        _dt = _t_mod.time() - _t0
        ucase("fetch層: <title>巨大charrefは unescape 前に取得拒否(None)＋短時間（桁制限無効でもDoSなし）",
              _res is None and _dt < 3.0)
    finally:
        _g["is_public_fetchable_url"] = _save_ipf
        _g["_opener_for"] = _save_open
        if _save_limit is not None:
            sys.set_int_max_str_digits(_save_limit)

    ok = all(results)
    log(f"=== selftest: {sum(results)}/{len(results)} 合格 → {'✅ 全テスト成功' if ok else '❌ 失敗あり'} ===")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="claims.json のパス")
    ap.add_argument("--min-domains", type=int, default=2, help="criticalフィールドに必要な合格ドメイン数（既定2）")
    ap.add_argument("--allowed-domains", help="カンマ区切りの許可ドメイン（全ホップで完全一致/正規サブドメインを強制）")
    ap.add_argument("--selftest", action="store_true", help="ネット不要の内蔵テストを実行")
    args = ap.parse_args()
    if args.selftest:
        sys.exit(selftest())
    if not args.file:
        ap.error("--file か --selftest のどちらかを指定")
    allowed = [d.strip() for d in args.allowed_domains.split(",")] if args.allowed_domains else None
    try:
        code = run(args.file, args.min_domains, allowed_domains=allowed)
    except Exception as e:
        log(f"❌ claims形式エラー/実行エラー: {type(e).__name__}: {e} → 不合格扱い（無人昇格禁止）")
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()

"""fetched_page.py — ★取ってきた本文を、1個のデータとして持ち回る★

★何のためか★（2026-08-17・台帳#393／Codex依頼237の診断）
  依頼231〜236で、同じ型の穴が**5回続けて**見つかった。
  どれも「直した箇所の**隣**が同じ理由でずれている」形だった。

  Codexの診断:
  > 共通原因は、「検証済みの対象」が1個のデータとして存在せず、
  > 複数の弱い表現へ分解されていること

  同じ1ページが、こう散らばっていた:
    控えの target_url ／ 根拠の evidence.url ／ 実行時の material_url ／
    正規化した url_key ／ observed_final_url ／ URLだけの許可証 ／
    実際に取得したHTML ／ 共有変数 LAST_FINAL_URL

  検証のあとに残るのが「**この本文を確かめた**」ではなく
  「**このURLはACCEPT**」という縮約された情報なので、
  境界を1つ越えるたびにURL・状態・到達先・本文を結び直す必要があった。

★この器が守る決まり（不変条件はこれ1つ）★
  ★★VerifiedMaterial と同じ本文を持たない値は、
    どの票・材料・公開物にも入らない★★

  そのために、
  ①取りに行くのは1回だけ（`fetch()`）
  ②投稿欄を落としたあとの本文と、その指紋を一緒に持つ
  ③許可証は**URLではなく指紋**で出す
  ④材料を読む部品は**取り直さず**、この本文をそのまま読む
"""
from __future__ import annotations

import hashlib
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))


class PageError(Exception):
    """取ってこられなかった／落としきれなかった（★迷ったら使わない★）。

    ★status★＝HTTPの状態番号（分かるときだけ・2026-09-02）
      ★404 は「そもそも無い」★＝読まなくても証拠は欠けていない。
      それ以外は「こちらが読めていない」＝欠けている。
    """

    def __init__(self, *a, status=None):
        super().__init__(*a)
        self.status = status


class FetchedPage:
    """★1回の取得で分かったことを、まとめて持つ★

    requested_url … 取りに行ったURL（実行時に名鑑から見つかったもの）
    final_url     … 実際に着いたURL（転送があればその先）
    cleaned_html  … 投稿欄・AI欄を箱ごと落としたあとのHTML
                    ★あとで読む部品には必ずこれを渡す★
    readable_text … そこから作った「2AIが読む文字」（逐語の照合もこの上で行う）
    text_sha256   … readable_text の指紋（★許可証・控えの照合はこれ★）
    html_sha256   … cleaned_html の指紋（★診断用★・採否には使わない）

    ★★`sha256` という名前は捨てた★★（2026-09-18・台帳#696）
      曖昧なまま置いておくと、次に増える照合箇所がまた全文を掴む。
      消したので、直し忘れた場所は**その場で落ちる**（黙ってずれない）。
    """

    __slots__ = ("requested_url", "final_url", "cleaned_html",
                 "readable_text", "text_sha256", "html_sha256")

    def __init__(self, requested_url: str, final_url: str, cleaned_html: str):
        import user_area as _ua
        self.requested_url = str(requested_url or "")
        self.final_url = str(final_url or "")
        self.cleaned_html = str(cleaned_html or "")
        self.readable_text = _ua.readable_text(self.cleaned_html)
        # ★★指紋は必ず共通関数に作らせる★★（2026-09-18）
        #   ★ここで自分で数えない★＝一度それをやったら、この器は「行の形」で、
        #   照合する側（`maker_identity_cache` / `model_code_lookup`）は
        #   「詰めた形」で数えていて、**同じページなのに永久に一致しなかった**
        #   （直している最中に実際に作ってしまった）。
        self.text_sha256 = _ua.readable_sha256(self.cleaned_html)
        self.html_sha256 = hashlib.sha256(
            self.cleaned_html.encode("utf-8")).hexdigest()

    def redirected(self) -> bool:
        """★取りに行った先と着いた先が違うか★（同じ物差しで比べる）"""
        import maker_identity_cache as _mic
        return _mic.url_key(self.requested_url) != _mic.url_key(self.final_url)

    def __repr__(self) -> str:                       # 目で見るときだけ
        return (f"FetchedPage({self.requested_url} → {self.final_url} / "
                f"{len(self.cleaned_html)}字 / 読む文字{len(self.readable_text)}字"
                f" / {self.text_sha256[:12]}…)")


def fetch(url: str, purpose: str = "claim_material", get=None) -> FetchedPage:
    """★取ってきて、投稿欄を落として、指紋まで作る★（ここが唯一の入口）

    ★用途を必ず名乗る★（通信の関所が用途を見るため）
    ★落としきれないページは使わない★（fail-closed）
    ★到達先が分からないページも使わない★＝あとで比べようがない
    """
    import new_machine_watch as _w
    import user_area as _ua
    try:
        with _w.fetching(purpose):
            raw = (get or _w._get)(url)
    except Exception as e:                 # noqa: BLE001
        raise PageError(f"取得できません（{url}）: {str(e)[:120]}",
                        status=getattr(e, "status", None))
    fin = str((getattr(_w, "LAST_FINAL_URL", {}) or {}).get("url") or "")
    if not fin:
        raise PageError(
            f"到達先が分かりません（{url}）"
            "／★分からない＝確かめていないので使いません★")
    try:
        cleaned = _ua.clean_html(raw or "", url)
    except Exception as e:                 # noqa: BLE001
        # ★★包み直しても、型のついた情報を落とさない★★
        #   （2026-09-10・CodexのP0）★直す前はここで文字列になっていた★ので、
        #   上位は「どの段で何が満たせなかったか」を知りようがなく、
        #   問いは文言の名簿頼りのままだった。
        _pe = PageError(f"投稿欄を落としきれません（{url}）: {str(e)[:120]}")
        for _k in ("stage", "failed_contract", "observations", "url", "raw"):
            if hasattr(e, _k):
                setattr(_pe, _k, getattr(e, _k))
        raise _pe
    # ★★掃除のあとの見張りは `clean_html` の中にある★★
    #   （2026-09-21に移した・台帳#669）
    #   ★直す前はここだけにあった★ので、`model_code_lookup` /
    #   `maker_identity_cache` / `collect_evidence` は自分で生HTMLを取って
    #   `clean_html` を直接呼び、★この見張りを一度も通らなかった★。
    #   ＝未知の箱にある読者の書き込みが材料に混ざり得た。
    #   ★ここに残すと守りが二重になる★＝片方を壊しても試験が緑のまま（罠③）。
    #   上の `except` が `UserAreaError` を `PageError` へ包み直すので、
    #   呼ぶ側から見た形（止まる・理由が残る）は同じ。
    return FetchedPage(url, fin, cleaned)


def _restructure(html: str) -> str:
    """★表をやめて、セルを1つずつ div にする★（CSSで組んだ表への作り替え）

    ★読む文字も指紋も1文字も変わらない★＝セルごとに行が分かれるのは同じ。
    ＝★指紋では原理的に気づけない構造の変更★（実在する作り替え方）。

    ★何のためか★＝この指紋は「読む文字」で取るので、
    **構造だけの変更は検出しない**（2026-09-18・Codexの指摘）。
    そこで「検出しないこと」を放置せず、
    ★構造が変わったときに読取器が別の値を採らないか★を機械で確かめる。

    ★1種類の作り替えを試しただけ★＝rowspan/colspan や別の表との競合など、
    見える文字の順を保つ構造変更の全部を保証するものではない（Codexの指摘）。
    """
    h = re.sub(r"<table[^>]*>|</table>|<tbody[^>]*>|</tbody>"
               r"|<tr[^>]*>|</tr>", "", html)
    h = re.sub(r"<t[hd][^>]*>", "<div>", h)
    return h.replace("</th>", "</div>").replace("</td>", "</div>")


def structure_only_change_problems() -> list:
    """★構造だけ変わっても、読取器が「別の値」を採らないこと★

    ★この指紋が保証しないもの★＝表の構造。
    文字が同じ順で残ったまま `<table>` が消えても指紋は変わらないので、
    ★2AIの「このページを材料に使う」という控えはそのまま効き続ける★。
    そのとき読取器が**黙って違う値を採る**なら、それは誤情報の経路になる。

    ★求める線は「同じ値か、何も採らないか」★（違う値を採ったら異常）。
    ★材料そのものが空だったら異常として出す★＝
    空どうしを比べても何も確かめたことにならない（罠㊴）。
    """
    import at_spec_lookup as _al
    import ceiling_lookup as _cl
    import cz_lookup as _zl
    import spec_lookup as _sl
    import user_area as _uap

    cases = [
        ("天井", _cl.from_table,
         "<h3>AT天井</h3><table>"
         "<tr><th>天井G数</th><td>1200G</td></tr>"
         "<tr><th>恩恵</th><td>AT当選</td></tr></table>"),
        ("ATの仕様", _al.from_tables,
         '<h3>AT「試験ライブ」</h3><table>'
         "<tr><th>継続G数</th><td>1セット100G</td></tr>"
         "<tr><th>純増</th><td>約2.8枚/G</td></tr></table>"),
        ("CZ", _zl.from_tables,
         '<h3><span>CZ「すぱ娘チャレンジ」</span></h3><table><tbody>'
         "<tr><th>タイプ</th><td>ST</td></tr>"
         "<tr><th>継続G数</th><td>4G＋α</td></tr>"
         "<tr><th>期待度</th><td>約40%</td></tr></tbody></table>"),
        ("ボーナス確率", lambda h: _sl.bonus_matrix_from_tables(h)[0],
         "<table>"
         "<tr><th>設定</th><th>BIG</th><th>REG</th><th>合算</th></tr>"
         "<tr><td>設定1</td><td>1/273.1</td><td>1/439.8</td>"
         "<td>1/168.5</td></tr>"
         "<tr><td>設定6</td><td>1/240.1</td><td>1/240.1</td>"
         "<td>1/120.0</td></tr></table>"),
    ]
    ng = []
    for name, read, table_html in cases:
        flat = _restructure(table_html)
        # ★前提＝指紋では区別できないこと★（2026-09-18・Codexの指摘で直した）
        #   ★逐語を探す形（compare_text）で見ていたのは誤り★＝
        #   あちらは改行も潰すので、指紋が区別できる違いまで「同じ」に見えた。
        if _uap.readable_sha256(table_html) != _uap.readable_sha256(flat):
            ng.append(f"{name}: 指紋が変わってしまっています"
                      "／★指紋で気づける変更は、この検査の対象ではありません★")
            continue
        got_a = read(table_html)
        got_b = read(flat)
        if not got_a:
            ng.append(f"{name}: 表の形でも何も採れていません"
                      "／★空どうしを比べても何も確かめたことになりません★")
            continue
        if got_b and got_b != got_a:
            ng.append(f"{name}: 構造だけ変えたら**別の値**を採りました"
                      f"（表 {got_a} → 崩した形 {got_b}）"
                      "／★指紋では気づけないので、ここで止めます★")
    return ng


def _wiring_problems() -> list:
    """★上の検査が、本当に自己試験から呼ばれているか★（2026-09-18）

    ★なぜ要るか★＝この検査は「問題が無ければ空の一覧」を返すので、
    ★呼び出しを `[]` に書き換えても緑のまま★になる（罠㊸）。
    検査を書いただけでは、気づかずに外されたことに誰も気づけない。
    ★これは「文字が在るか」の検査★＝動く証拠は検査そのものの結果のほう。
    """
    import inspect
    src = inspect.getsource(selftest)
    ng = []
    if "structure_only_change_problems()" not in src:
        ng.append("自己試験が、構造だけ変わったときの検査を呼んでいません")
    if "_restructure(" not in src:
        ng.append("自己試験に、崩した形の対照がありません")
    return ng


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅ " if cond else "❌ ") + name)

    import new_machine_watch as _w
    C = "https://chonborista.com/slot/orinpia-slot/264134/"
    HTML = ('<title>L試験機 スロット 新台 解析 | ちょんぼりすた</title>'
            '<a class="rating-btn">みんなの評価 (平均0)</a>'
            '<div id="hyouka">星の評価</div>'
            '<ul class="commentlist"><li>読者の書き込み メーカー サミー</li></ul>'
            '<div id="entry"><div>機種名 L試験機</div>'
            '<div>メーカー 京楽</div></div>')

    def _get_ok(u, timeout=20):
        _w.LAST_FINAL_URL["url"] = u
        return HTML

    p = fetch(C, get=_get_ok)
    t("★★取ってきた本文と指紋を一緒に持つ★★",
      p.requested_url == C and p.final_url == C and len(p.text_sha256) == 64)
    t("★★投稿欄は落ちている★★（読者の書き込みが本文に残らない）",
      "読者の書き込み" not in p.cleaned_html
      and "機種名 L試験機" in p.cleaned_html)
    t("　同じ本文なら指紋も同じ", fetch(C, get=_get_ok).text_sha256 == p.text_sha256)

    def _get_changed(u, timeout=20):
        _w.LAST_FINAL_URL["url"] = u
        return HTML.replace("京楽", "サミー")
    t("★★本文が変わったら指紋も変わる★★",
      fetch(C, get=_get_changed).text_sha256 != p.text_sha256)

    # ★★取り直すたびに変わる飾りでは、指紋を変えない★★
    #   （2026-09-18・台帳#696＝新台3件が11晩止まった原因）
    #   なな徹はCSSのURLに**そのときのunix秒**を、DMMは csrf-token と
    #   画像の `?t=` を毎回変える。実測した2つの形をそのまま試験にする。
    #   ★時計で作らない★＝Windowsの時刻は刻みが粗く、2回の取得が同じ値に
    #   なることがある。★そのとき飾りは一度も動いておらず、試験は
    #   何も確かめずに緑になる★（実際にそうなった）。数え上げで必ず動かす。
    _tick = [0]

    def _get_noisy(u, timeout=20):
        _w.LAST_FINAL_URL["url"] = u
        _tick[0] += 1
        n = 1789700770 + _tick[0]
        return (f'<link href="/css/g.css?{n}">'
                f'<meta name="csrf-token" content="tok{n}">'
                + HTML
                + f'<img src="/i.png?t={n}">')
    _n1 = fetch(C, get=_get_noisy)
    _n2 = fetch(C, get=_get_noisy)
    t("★★飾りが毎回変わっても「読む文字」の指紋は変わらない★★"
      "／★これが無いと2AIの控えが数秒で失効する★",
      _n1.text_sha256 == _n2.text_sha256)
    t("　★対照★＝そのときHTMLの指紋のほうは実際に変わっている"
      "（＝飾りが本当に動いていることの証拠）",
      _n1.html_sha256 != _n2.html_sha256)
    t("　読む文字には飾りが入らない",
      "csrf" not in _n1.readable_text and "g.css" not in _n1.readable_text
      and "機種名 L試験機" in _n1.readable_text)

    # ★★逐語の照合と同じ物差しであること★★
    #   ここがずれると「確かめた本文」と「引用を探す本文」が別物になる。
    import new_machine_watch as _wv
    import user_area as _ua_t
    import hashlib as _hs
    t("★★逐語照合の物差しは `readable_text` から作られている★★",
      _ua_t.compare_text(p.cleaned_html)
      == " ".join(_wv._visible_text(p.cleaned_html).split()))
    t("★★器の指紋と、照合する側が数え直す指紋が一致する★★"
      "／★割れると同じページなのに永久に一致しない★",
      p.text_sha256 == _ua_t.readable_sha256(p.cleaned_html)
      == _hs.sha256(_ua_t.fingerprint_text(p.cleaned_html)
                    .encode("utf-8")).hexdigest())
    # ★★2AIが読む形が違えば、指紋も違う★★（2026-09-18・Codexの指摘）
    #   ★直す前は成り立っていなかった★＝指紋を `compare_text`（改行も
    #   1個の空白に潰す形）で取っていたので、
    #   「天井G数／改行／1200G」と「天井G数 1200G」が同じ指紋になった。
    #   ＝★2AIが読んだ中身が変わっても気づけない★。実測で確認した。
    _L1 = "<div>天井G数</div><div>1200G</div>"
    _L2 = "<div>天井G数 1200G</div>"
    t("★★2AIが読む形が違えば指紋も違う★★"
      "（★行が変わるのは「読むものが変わった」ということ★）",
      _ua_t.readable_text(_L1) != _ua_t.readable_text(_L2)
      and _ua_t.readable_sha256(_L1) != _ua_t.readable_sha256(_L2))
    t("　★対照★＝逐語を探す形のほうは、行をまたいでも当たる"
      "（2AIが行をまたいで引用しても照合できる）",
      _ua_t.compare_text(_L1) == _ua_t.compare_text(_L2))
    t("　行の中の余分な空白だけは詰める（整形の揺れで失効させない）",
      _ua_t.fingerprint_text("<div>天井G数    1200G</div>")
      == _ua_t.fingerprint_text(_L2))

    t("★★曖昧な `sha256` は残っていない★★（次の照合箇所が掴まないように）",
      not hasattr(p, "sha256"))

    # ★★この指紋が「見ていないもの」を、放置しない★★（2026-09-18・Codexの指摘）
    _sp = structure_only_change_problems()
    t("★★構造だけ変わっても、読取器は別の値を採らない★★"
      "（指紋は構造を見ないので、ここが最後の歯止め）"
      + ("／" + " ／ ".join(_sp[:2]) if _sp else ""),
      not _sp)
    # ★★対照★★＝「構造だけ変える」が本当に構造を変えているか
    #   （何も変えていない材料で緑になっていたら、この検査は飾り）
    _TBL = ("<h3>AT天井</h3><table>"
            "<tr><th>天井G数</th><td>1200G</td></tr>"
            "<tr><th>恩恵</th><td>AT当選</td></tr></table>")
    import ceiling_lookup as _clc
    t("　★対照★＝崩した形では表として読めなくなっている"
      "（＝本当に構造が変わっている証拠）",
      bool(_clc.from_table(_TBL)) and not _clc.from_table(_restructure(_TBL)))
    _wp = _wiring_problems()
    t("★★その検査が、ここから本当に呼ばれている★★"
      "（★空の一覧を返す検査は、呼び出しを外しても緑のまま★）"
      + ("／" + " ／ ".join(_wp) if _wp else ""),
      not _wp)

    def _get_redir(u, timeout=20):
        _w.LAST_FINAL_URL["url"] = "https://chonborista.com/slot/x/999/"
        return HTML
    t("★★転送されたことが分かる★★", fetch(C, get=_get_redir).redirected())
    t("　転送が無ければ「転送された」とは言わない", not p.redirected())

    def _get_nofin(u, timeout=20):
        _w.LAST_FINAL_URL["url"] = None
        return HTML
    try:
        fetch(C, get=_get_nofin)
        t("★★到達先が分からないページは使わない★★（fail-closed）", False)
    except PageError as e:
        t("★★到達先が分からないページは使わない★★（fail-closed）",
          "到達先" in str(e))

    def _get_dirty(u, timeout=20):
        _w.LAST_FINAL_URL["url"] = u
        return "<title>L試験機</title><div>投稿欄の箱が無い</div>"
    try:
        fetch(C, get=_get_dirty)
        t("★★投稿欄を落としきれないページは使わない★★（fail-closed）", False)
    except PageError as e:
        t("★★投稿欄を落としきれないページは使わない★★（fail-closed）",
          "落としきれません" in str(e))

    def _get_ng(u, timeout=20):
        raise RuntimeError("404")
    try:
        fetch(C, get=_get_ng)
        t("　取得できないページは使わない", False)
    except PageError:
        t("　取得できないページは使わない", True)

    # ★★決まりごとがあるサイトでも、残存検査は効く★★
    #   （2026-08-24・Codexの14回目＝別の箱で投稿欄を足された場合）
    #   ★決まりごとがあるサイトだけ検査を飛ばすと、ここが素通りする★
    _second = ('<title>L試験機 スロット 新台 解析 | ちょんぼりすた</title>'
               '<a class="rating-btn">みんなの評価 (平均0)</a>'
               '<div id="hyouka">星の評価</div>'
               '<ul class="commentlist"><li>旧い書き込み</li></ul>'
               '<div id="entry"><div>機種名 L試験機</div>'
               '<div>メーカー 京楽</div></div>'
               '<div class="comment-list"><p>新しい投稿欄</p></div>')

    def _get_second(u, timeout=20):
        _w.LAST_FINAL_URL["url"] = u
        return _second
    _blocked2 = False
    try:
        fetch(C, get=_get_second)
    except PageError:
        _blocked2 = True
    t("★★決まりごとがあるサイトが別の箱で投稿欄を足しても止める★★"
      "／★古い箱があるので必須箱の検査は通ってしまう★",
      _blocked2)


    ng = sum(1 for _, o in results if not o)
    print()
    print("%d/%d 合格" % (len(results) - ng, len(results)))
    return 1 if ng else 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="取ってきた本文の器")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    raise SystemExit(selftest() if a.selftest else 0)

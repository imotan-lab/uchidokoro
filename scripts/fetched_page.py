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
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))


class PageError(Exception):
    """取ってこられなかった／落としきれなかった（★迷ったら使わない★）。"""


class FetchedPage:
    """★1回の取得で分かったことを、まとめて持つ★

    requested_url … 取りに行ったURL（実行時に名鑑から見つかったもの）
    final_url     … 実際に着いたURL（転送があればその先）
    cleaned_html  … 投稿欄・AI欄を箱ごと落としたあとのHTML
                    ★あとで読む部品には必ずこれを渡す★
    sha256        … cleaned_html の指紋（許可証はこれで出す）
    """

    __slots__ = ("requested_url", "final_url", "cleaned_html", "sha256")

    def __init__(self, requested_url: str, final_url: str, cleaned_html: str):
        self.requested_url = str(requested_url or "")
        self.final_url = str(final_url or "")
        self.cleaned_html = str(cleaned_html or "")
        self.sha256 = hashlib.sha256(
            self.cleaned_html.encode("utf-8")).hexdigest()

    def redirected(self) -> bool:
        """★取りに行った先と着いた先が違うか★（同じ物差しで比べる）"""
        import maker_identity_cache as _mic
        return _mic.url_key(self.requested_url) != _mic.url_key(self.final_url)

    def __repr__(self) -> str:                       # 目で見るときだけ
        return (f"FetchedPage({self.requested_url} → {self.final_url} / "
                f"{len(self.cleaned_html)}字 / {self.sha256[:12]}…)")


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
        raise PageError(f"取得できません（{url}）: {str(e)[:120]}")
    fin = str((getattr(_w, "LAST_FINAL_URL", {}) or {}).get("url") or "")
    if not fin:
        raise PageError(
            f"到達先が分かりません（{url}）"
            "／★分からない＝確かめていないので使いません★")
    try:
        cleaned = _ua.clean_html(raw or "", url)
    except Exception as e:                 # noqa: BLE001
        raise PageError(f"投稿欄を落としきれません（{url}）: {str(e)[:120]}")
    return FetchedPage(url, fin, cleaned)


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅ " if cond else "❌ ") + name)

    import new_machine_watch as _w
    C = "https://chonborista.com/slot/orinpia-slot/264134/"
    HTML = ('<title>L試験機 スロット 新台 解析 | ちょんぼりすた</title>'
            '<div id="hyouka">星の評価</div>'
            '<ul class="commentlist"><li>読者の書き込み メーカー サミー</li></ul>'
            '<div id="entry"><div>機種名 L試験機</div>'
            '<div>メーカー 京楽</div></div>')

    def _get_ok(u, timeout=20):
        _w.LAST_FINAL_URL["url"] = u
        return HTML

    p = fetch(C, get=_get_ok)
    t("★★取ってきた本文と指紋を一緒に持つ★★",
      p.requested_url == C and p.final_url == C and len(p.sha256) == 64)
    t("★★投稿欄は落ちている★★（読者の書き込みが本文に残らない）",
      "読者の書き込み" not in p.cleaned_html
      and "機種名 L試験機" in p.cleaned_html)
    t("　同じ本文なら指紋も同じ", fetch(C, get=_get_ok).sha256 == p.sha256)

    def _get_changed(u, timeout=20):
        _w.LAST_FINAL_URL["url"] = u
        return HTML.replace("京楽", "サミー")
    t("★★本文が変わったら指紋も変わる★★",
      fetch(C, get=_get_changed).sha256 != p.sha256)

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

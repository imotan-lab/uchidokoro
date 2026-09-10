# -*- coding: utf-8 -*-
"""★機械が読めなかったことを「型のついた出来事」として持ち回る★

（2026-09-10・運営者の基本方針
  「今後も機械的にやってだめな場合は2AI　これ基本方針」）

★なぜ要るか★
直す前は、読めなかったことは**例外の文章**として投げられ、
2AIへの問いは★その文章の中の決まった語句を探して★作られていた
（`build_new_article.UNRESOLVED_MARKS`）。
＝**新しい失敗の言い回しは、名簿に載るまで問いにならない**。
だから失敗が増えるたびに「この場合はこう」を足すことになっていた。
実例（2026-09-10の朝）＝
  ・出典ページの `<style>` が閉じておらず、本文を1文字も読めない
  ・「口コミ2件」と書いてあるのに一覧の箱がHTMLに無い
どちらも毎朝静かに止まり、★エラーメールも出なかった★。

★これで何が変わるか★＝失敗は必ずこの型で持ち回るので、
**言い回しを変えても問いになる**。場合分けを足さなくてよい。

★書くのは観測した事実だけ★＝解釈は書かない（解釈は2AIの仕事）。
"""
from __future__ import annotations

import hashlib
import json

# ★どの段で失敗したか★（免除は段ごと・またいで効かせない）
#   ★Codexの指摘（2026-09-10）★＝段を記録しないと、
#   投稿欄の失敗を「値を読む」の免除で迂回できてしまう。
STAGE_IDENTITY_FACTS = "IDENTITY_FACTS"      # 機種の基本情報（メーカー・導入日）
STAGE_USER_AREA = "USER_AREA"                # 投稿欄を落とす
STAGE_MATERIAL_READ = "MATERIAL_READ"        # 材料の読み取り
STAGES = (STAGE_IDENTITY_FACTS, STAGE_USER_AREA, STAGE_MATERIAL_READ)

# ★問いと答えの書き方の版★（控えの鍵に入れる）
ASK_SCHEMA = "read-failure/v1"


class ReadFailure(Exception):
    """★機械が読めなかった★（例外だが、中身は機械の観測）

    ★文章で持ち回らない★＝呼ぶ側は `stage` と `failed_contract` を見る。
    """

    def __init__(self, stage: str, failed_contract: str, *,
                 requested_url: str = "", final_url: str = "",
                 observations: dict | None = None, raw: str = "",
                 raw_sha256: str = "", why: str = ""):
        if stage not in STAGES:
            raise ValueError(f"知らない段です: {stage!r}")
        if not str(failed_contract or "").strip():
            raise ValueError("どの契約が満たせなかったかが要ります")
        self.stage = stage
        self.failed_contract = str(failed_contract)
        self.requested_url = str(requested_url or "")
        self.final_url = str(final_url or "")
        self.observations = dict(observations or {})
        self.raw_sha256 = str(raw_sha256 or (sha256(raw) if raw else ""))
        super().__init__(why or f"{stage}: {failed_contract}")

    def as_dict(self) -> dict:
        return {"schema": ASK_SCHEMA, "stage": self.stage,
                "failed_contract": self.failed_contract,
                "requested_url": self.requested_url,
                "final_url": self.final_url,
                "observations": self.observations,
                "raw_sha256": self.raw_sha256}


def sha256(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


# ★段ごとに、2AIに何を答えてほしいか★
#   ★「相手の作りが変わったのか」は聞かない★（2026-09-10・Codexの指摘）＝
#   いまのHTMLだけでは過去の作りは分からない。
#   聞くのは★このHTMLが何を述べているか★だけ。
_ASK = {
    STAGE_IDENTITY_FACTS: (
        "このページの生HTMLの中で、★メーカー名と導入開始日が同じひと続きの範囲★に"
        "書かれている場所を1つだけ挙げ、その範囲をそのまま（逐語で）返してください。"
        "／範囲の中に両方のラベルと両方の値が入っていること。"
        "／見つからなければ「使えません」と答えてください。"),
    STAGE_USER_AREA: (
        "このページの生HTMLに、★読者が書き込んだ本文★が載っているかを見てください。"
        "／載っていなければ、そう判断した手がかり（件数の表示・見出し・"
        "別ページへのリンクなど）を逐語で挙げてください。"
        "／少しでも分からなければ「使えません」と答えてください。"),
    STAGE_MATERIAL_READ: (
        "このページの生HTMLから、機械が読めなかった値を"
        "★同じひと続きの範囲★の逐語つきで挙げてください。"
        "／見つからなければ「使えません」と答えてください。"),
}


def of(exc) -> "ReadFailure | None":
    """★どんな例外からでも「型のついた失敗」を取り出す★（2026-09-10）

    ★これが無いと繋がらない★＝例外は途中で別の型に包み直されるので、
    受け取る側は `isinstance` では見つけられない。
    ★属性で見る★＝包み直した側が引き継いでいれば、そのまま取り出せる。
    ★段が無いものは None★＝型のついた失敗ではない（今までどおり扱う）。
    """
    if isinstance(exc, ReadFailure):
        return exc
    stage = getattr(exc, "stage", "")
    if stage not in STAGES:
        return None
    try:
        return ReadFailure(
            stage, getattr(exc, "failed_contract", "") or "（記録なし）",
            requested_url=getattr(exc, "url", "") or "",
            final_url=getattr(exc, "final_url", "")
            or getattr(exc, "url", "") or "",
            observations=getattr(exc, "observations", None) or {},
            raw=getattr(exc, "raw", "") or "")
    except Exception:                                        # noqa: BLE001
        return None


def question_for(exc) -> str:
    """★例外から直接、2AIへの問いを作る★（★文言の名簿を通さない★）

    ★これが要る理由★＝問いは今まで「問題の文章の中の決まった語句」から
    作られていたので、新しい失敗の言い回しは名簿に載るまで問いにならなかった。
    """
    rf = of(exc)
    return question(rf) if rf else ""


def question(rf: ReadFailure) -> str:
    """★2AIへの問いを作る★（観測した事実＋何を答えてほしいか）"""
    obs = ", ".join(f"{k}={v}" for k, v in sorted(rf.observations.items()))
    return (
        "★機械がこのページを読めませんでした★／"
        f"読めなかった段: {rf.stage}／"
        f"満たせなかった決まり: {rf.failed_contract}／"
        f"取りに行った先: {rf.requested_url}／着いた先: {rf.final_url}／"
        f"機械が数えたこと: {obs or '（なし）'}／"
        + _ASK[rf.stage]
        + "／★言うだけでは通しません★（挙げた逐語がそのページに"
          "そのまま在ることを機械が確かめます）")


def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    rf = ReadFailure(STAGE_IDENTITY_FACTS, "メーカー名と導入開始日が同じ表にある",
                     requested_url="https://example.invalid/a",
                     final_url="https://example.invalid/a",
                     observations={"style_open": 3, "style_close": 2},
                     raw="<html>x</html>")
    t("★観測した事実がそのまま持ち回れる★",
      rf.as_dict()["observations"] == {"style_open": 3, "style_close": 2})
    t("　生HTMLの指紋が入る", len(rf.raw_sha256) == 64)
    q = question(rf)
    t("★★問いに、どの段で何が満たせなかったかが出る★★"
      "（★これが無いと、別の段の免除で迂回できる★）",
      "IDENTITY_FACTS" in q and "メーカー名と導入開始日が同じ表にある" in q)
    t("　問いに機械が数えたことが出る", "style_open=3" in q)
    t("★★「作りが変わったのか」は聞かない★★"
      "（★いまのHTMLだけでは過去の作りは分からない・Codexの指摘★）",
      "変わ" not in q)
    t("　言うだけでは通さないと明記する", "言うだけでは通しません" in q)
    # ★知らない段は受け取らない★
    _bad = False
    try:
        ReadFailure("ZZZ", "x")
    except ValueError:
        _bad = True
    t("★知らない段は受け取らない★", _bad)
    _bad2 = False
    try:
        ReadFailure(STAGE_USER_AREA, "  ")
    except ValueError:
        _bad2 = True
    t("　満たせなかった決まりが空なら受け取らない", _bad2)
    t("　段ごとに聞くことが違う",
      len({_ASK[s] for s in STAGES}) == len(STAGES))
    ng = [n for n, ok in results if not ok]
    print(f"{len(results) - len(ng)}/{len(results)} 合格" if not ng
          else "失敗: " + json.dumps(ng, ensure_ascii=False))
    return 1 if ng else 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    print(__doc__)

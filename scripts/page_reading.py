# -*- coding: utf-8 -*-
"""★機械が読めなかったページについて、2AIが読んで出した答えを控える★

（2026-09-10・運営者の基本方針「機械的にやってだめな場合は2AI」）

★役割の分け方★
  機械 … 取ってくる／数える／★逐語がそのHTMLにそのまま在るか照合する★／指紋を取る
  2AI  … そのHTMLが何を述べているかを読む

★答えは3種類だけ★（★段をまたいで効かせない★・2026-09-10のCodexの指摘）
  READ_FACTS             … 機械が読めなかった値を、根拠の範囲つきで渡す
  WAIVE_MISSING_USER_BOX … 「必ずあるはず」の投稿欄の箱が無いことだけを免除する
  UNUSABLE               … このページは出典に使わない

★名前を「投稿欄が無い」にしない★（Codexの指摘）＝
  表しているのは「決まりごとが求める箱が無い」ことであって、
  「読者の書き込みが絶対に無い」という負の証明ではない。
  ★少しでも分からなければ UNUSABLE★

★根拠の範囲（カプセル）★＝READ_FACTS は値だけでは通さない。
  ひと続きのHTMLの範囲を返させ、機械が
    ①その範囲が生HTMLにそのまま在る
    ②範囲の中に両方のラベルと両方の値がある
    ③同じ範囲が2つ以上見つからない（一意）
  を確かめる。★2AIが別の似た表から拾ってきても通らないようにするため★

★どこを読んだのかを名乗る★＝`evidence_scope`
  RAW_RESPONSE      … 発行元が返した生HTMLの中の原文
  VISIBLE_DOCUMENT  … 画面に出ている文書
  ★閉じていない `<style>` の後ろは、標準的な読み方でも画面に出ない★ので、
  そこから採ったなら RAW_RESPONSE と名乗る（あとで説明を誤らないため）。

★指紋の細かさは答えの種類で変える★（Codexの指摘）
  WAIVE_MISSING_USER_BOX … ★指紋は見ていない★（2026-09-14に外した）
    いま見るのは3つ＝①必須の箱が今も欠けている ②欠けた箱が控えの範囲内
    ③そう判断した手がかりの逐語が、いまのページに在る。
    ★★この3つは、外した指紋の役目を果たしていない★★
    （2026-09-18・Codexの指摘／台帳#662・★未解決★）＝
    ★未知の名前の箱（例 `class="opinion-v2"`）に読者の書き込みが足されると、
    3つとも通ってしまう★（新しい箱は「欠けた必須の箱」に入らないため）。
    後段の `looks_like_user_area` は手がかり頼りで、しかも
    `fetched_page` の中にしか無い。★要るのは「揺れる値だけを外した構造の指紋」★。
  READ_FACTS             … 根拠の範囲の指紋（広告の日付替わりで失効させない）
  UNUSABLE               … ★「読む文字」の指紋★（2026-09-18・台帳#696）
    ★生HTML全体だった★＝なな徹はCSSのURLにそのときのunix秒を、
    DMMは csrf-token と画像の `?t=` を毎回変えるので、
    ★取ってくるたびに「使わない」の判断が失効★し、同じページを
    毎晩聞き直して3回の枠を食いつぶす。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import local_paths as _lp                # noqa: E402
import read_failure as _rf               # noqa: E402


def _text_sha(html: str) -> str:
    """★「2AIが読む文字」の指紋★（2026-09-18・台帳#696）

    ★同じ規則を2か所に書かない★＝作り方は `user_area` の1か所だけ。
    ★掃除前のHTMLを渡してよい★＝この層が扱うのは、そもそも機械が
    掃除・読み取りに失敗したページなので、掃除済みの本文が無い。
    見たいのは「読者に出る文字が変わったか」だけなので、これで足りる。
    """
    import user_area as _ua_pr
    return _ua_pr.readable_sha256(html or "")
import safe_json as _sj                  # noqa: E402

STORE = _lp.doc("page_reading.json")
SCHEMA = "page-reading/v1"

READ_FACTS = "READ_FACTS"
WAIVE_MISSING_USER_BOX = "WAIVE_MISSING_USER_BOX"
UNUSABLE = "UNUSABLE"
KINDS = (READ_FACTS, WAIVE_MISSING_USER_BOX, UNUSABLE)

# ★どの答えが、どの段だけを救えるか★（またいで効かせない）
KIND_STAGE = {
    READ_FACTS: (_rf.STAGE_IDENTITY_FACTS, _rf.STAGE_MATERIAL_READ),
    WAIVE_MISSING_USER_BOX: (_rf.STAGE_USER_AREA,),
    UNUSABLE: _rf.STAGES,
}

RAW_RESPONSE = "RAW_RESPONSE"
VISIBLE_DOCUMENT = "VISIBLE_DOCUMENT"
SCOPES = (RAW_RESPONSE, VISIBLE_DOCUMENT)

# ★項目とラベルの対応★（2026-09-10・CodexのP1）
#   ★自由な辞書にしない★＝どの項目がどのラベルの値かを決めておく。
FIELD_LABEL = {
    "maker": "メーカー名",
    "release_date": "導入開始日",
    "model_code": "型式名",
}

MIN_JUDGES = 2          # ★2AI★
MIN_WHY = 10            # なぜそう読んだか


class ReadingError(Exception):
    """控えの契約を満たさない"""


def _empty() -> dict:
    return {"schema_version": SCHEMA, "pages": {}}


def load(strict: bool = True) -> dict:
    """★控えを読む★（無ければ空・壊れていれば止める）"""
    if not os.path.exists(STORE):
        return _empty()
    got = _sj.read_json(STORE, expect=dict)
    if got.get("schema_version") != SCHEMA:
        raise ReadingError(f"控えの形が違います: {got.get('schema_version')}")
    got.setdefault("pages", {})
    if not isinstance(got["pages"], dict):
        raise ReadingError("控えの入れ物が壊れています")
    if strict:
        bad = []
        for k, rec in got["pages"].items():
            bad += [f"{k}: {x}" for x in validate(rec)]
        if bad:
            raise ReadingError("契約を満たさない記録があります: "
                               + " ／ ".join(bad[:5]))
    return got


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, STORE)


def contract_id(failed_contract: str) -> str:
    """★満たせなかった契約を、短い印にする★（2026-09-10・CodexのP1）

    ★これが無いと★＝同じ段の**別の読み取り失敗**に、
    前の答えが流用・上書きされる。
    """
    import hashlib
    t = " ".join(str(failed_contract or "").split())
    return hashlib.sha256(t.encode("utf-8")).hexdigest()[:16] if t else ""


def key_of(url: str, stage: str, contract: str = "") -> str:
    """★鍵は（段・URL）★＝段をまたいで効かせないため

    ★機種名は鍵に入れない★＝投稿欄が載っているか・その表に何が書いてあるかは
    **そのページの作りの話**で、どの機種のために読んでいるかとは関係がない。
    取ってくる唯一の入口（`fetched_page.fetch`）は機種名を受け取らないので、
    機種名を鍵にすると、この道に繋げられない。
    ★誰のために読んだかは記録として残す★（`slug`）。

    ★区切りに制御文字を使わない★＝安全な読み取りが弾く（作った直後に踏んだ）。
    段には空白が入らないので、空白1つで分けられる。
    """
    return f"{stage} {contract_id(contract)} {url}"


def _label_pairs(cap: str):
    """★根拠の範囲を表として読み直し、（ラベル, 値）の組を返す★

    （2026-09-10・CodexのP1④）
    ★縦持ちも横持ちも見る★＝
      縦持ち … 同じ行の左が見出し・右が値
      横持ち … 1行目が見出し・以降の行が値（同じ列で対応）
    ★読めなければ None★（呼ぶ側が「使わない」に倒す）。
    """
    try:
        import html_tables as _ht0
        tbs = _ht0.tables(str(cap or ""))
    except Exception:                                        # noqa: BLE001
        return None
    if not tbs:
        return None
    out = set()
    for tb in tbs:
        rows = [[str(c).strip() for c in r] for r in (tb.get("rows") or [])]
        if not rows:
            continue
        # 縦持ち（1行が「見出し・値」）
        for r in rows:
            if len(r) >= 2:
                out.add((r[0], r[1]))
        # 横持ち（1行目が見出し・以降が値）
        head = rows[0]
        for r in rows[1:]:
            for i, cell in enumerate(r):
                if i < len(head):
                    out.add((head[i], cell))
    return out


def validate(rec) -> list:
    """★どんな中身でも問題の一覧を返す★（落ちない）"""
    try:
        return _validate(rec)
    except Exception as e:                                   # noqa: BLE001
        return [f"検査できない形です（{type(e).__name__}）"]


def _validate(rec) -> list:
    ng = []
    if not isinstance(rec, dict):
        return ["記録が辞書ではありません"]
    kind = rec.get("kind")
    if kind not in KINDS:
        return [f"知らない答えです: {kind!r}"]
    stage = rec.get("stage")
    if stage not in KIND_STAGE[kind]:
        ng.append(f"この答えでは救えない段です（{kind} / {stage}）")
    by = rec.get("agreed_by")
    if not isinstance(by, list) or len({str(x) for x in by if x}) < MIN_JUDGES:
        ng.append(f"判断者が{MIN_JUDGES}人に足りません")
    if len(str(rec.get("why") or "").strip()) < MIN_WHY:
        ng.append(f"なぜそう読んだかが短すぎます（{MIN_WHY}文字以上）")
    if not str(rec.get("decided_at") or "").strip():
        ng.append("決めた日がありません")
    if not str(rec.get("raw_sha256") or "").strip():
        ng.append("読んだHTMLの指紋がありません")
    # ★問いの書き方が変わったら、古い答えは使わない★（2026-09-10・CodexのP1）
    #   ★直す前は保存するだけで照合していなかった★ので、
    #   聞き方の意味を変えても古い判断がそのまま通っていた。
    if str(rec.get("asked_schema") or "") != _rf.ASK_SCHEMA:
        ng.append(f"問いの書き方が変わっています（{rec.get('asked_schema')}）")
    if not str(rec.get("failed_contract") or "").strip():
        ng.append("どの契約が満たせなかったのかがありません")
    if kind == READ_FACTS:
        if rec.get("evidence_scope") not in SCOPES:
            ng.append("どこを読んだのかの名乗りがありません")
        cap = rec.get("evidence")
        if not isinstance(cap, str) or len(cap) < 20:
            ng.append("根拠の範囲がありません（20文字以上のひと続き）")
        fields = rec.get("fields")
        if not isinstance(fields, dict) or not fields:
            ng.append("読んだ値がありません")
        elif isinstance(cap, str):
            # ★★ラベルと値の対応を、表として読み直して確かめる★★
            #   （2026-09-10・CodexのP1④）
            #   ★順序だけでは足りない★＝横持ちの表では
            #     ラベル行: メーカー名 / 導入開始日
            #     値の行  : 北電子   / 2026年10月5日
            #   となり、★値を入れ替えても「ラベルが値より前」は成り立つ★。
            #   ★知らない項目は受け取らない★＝自由な辞書にしない。
            pairs = _label_pairs(cap)
            for k, v in fields.items():
                lab = FIELD_LABEL.get(k)
                sv = str(v)
                if not lab:
                    ng.append(f"知らない項目です（{k}）")
                    continue
                if sv not in cap:
                    ng.append(f"値が根拠の範囲の中にありません（{k}）")
                    continue
                if pairs is None:
                    ng.append("根拠の範囲を表として読めません（使いません）")
                    break
                if (lab, sv) not in pairs:
                    ng.append(f"「{lab}」の値として書かれていません（{k}）")
    if kind == WAIVE_MISSING_USER_BOX:
        miss = rec.get("waived_boxes")
        if not isinstance(miss, list) or not miss:
            ng.append("免除する箱の名前がありません")
        qs = rec.get("quotes")
        if not isinstance(qs, list) or not qs:
            ng.append("そう判断した手がかりの逐語がありません")
        # ★★作りの指紋を必ず持つ★★（2026-09-21・台帳#662/#669）
        #   ★無いものは受け取らない★＝箱が足されても失効しない控えになる。
        if not str(rec.get("waiver_structure_sha256") or "").strip():
            ng.append("ページの作りの指紋がありません"
                      "（未知の箱を足されても気づけません）")
        if not str(rec.get("waiver_text_sha256") or "").strip():
            ng.append("ページの読める文字の指紋がありません"
                      "（既存の箱の中へ読者の文字を足されても気づけません）")
        if not str(rec.get("url") or "").strip():
            ng.append("どのページを見て決めたかがありません"
                      "（掃除の決まりごとを引けず、同じ姿を作り直せません）")
    return ng


def verify(rec: dict, raw: str, *, stage: str, missing_boxes=None,
           labels=None) -> tuple:
    """★機械が確かめる★ → (使ってよいか, 理由)

    ★言うだけでは通さない★＝挙げられた逐語が、いま取ってきた
    そのHTMLにそのまま在ることを見る。
    """
    bad = validate(rec)
    if bad:
        return False, "控えが契約を満たしていません: " + " ／ ".join(bad[:3])
    if rec.get("stage") != stage:
        return False, f"別の段の答えです（控え {rec.get('stage')} / いま {stage}）"
    kind = rec["kind"]
    if kind == UNUSABLE:
        # ★ページが変わったら、この答えは効かせない★（2026-09-10・CodexのP2）
        # ★★比べるのは「読む文字」★★（2026-09-18・台帳#696）
        #   ★直す前は生HTML全文の指紋だった★＝なな徹はCSSのURLに
        #   そのときのunix秒を、DMMは csrf-token と画像の `?t=` を毎回変えるので、
        #   ★「このページは使わない」という2AIの判断が取得のたびに失効★し、
        #   同じページを毎晩聞き直して3回の枠を食いつぶす。
        #   ★安全な向き★＝安定させると「使わない」が**長く効く**。
        #   相手が中身を直せば読む文字が変わるので、ちゃんと聞き直しになる。
        #   ★`raw_sha256` では比べない★＝古い控えは `text_sha256` を持たないので
        #   自動的に失効する（fail-closed。「使わない」なので聞き直しは安全）。
        if not str(rec.get("text_sha256") or "").strip():
            return False, "古い形の控えです（読む文字の指紋がありません）"
        if _text_sha(raw) != str(rec.get("text_sha256")):
            return False, "ページが変わっているので、「使わない」は効かせません"
        return False, "2AIが「このページは使わない」と決めています"
    if kind == READ_FACTS:
        cap = str(rec.get("evidence") or "")
        n = str(raw or "").count(cap)
        if n == 0:
            return False, "根拠の範囲が、いまのページにありません"
        if n > 1:
            # ★一意でなければ通さない★（似た表から拾った可能性を排除できない）
            return False, f"根拠の範囲が {n} か所あります（1か所であること）"
        for lab in (labels or []):
            if str(lab) not in cap:
                return False, f"根拠の範囲に「{lab}」がありません"
        return True, "根拠の範囲がいまのページに1か所だけあります"
    if kind == WAIVE_MISSING_USER_BOX:
        # ★免除してよいのは「本当に無い箱」だけ★
        want = {str(x) for x in (rec.get("waived_boxes") or [])}
        now = {str(x) for x in (missing_boxes or [])}
        if not now:
            return False, "いまは箱が見つかっているので、免除は要りません"
        if not now <= want:
            return False, f"控えに無い箱まで免除しようとしています（{sorted(now - want)}）"
        # ★★全体の指紋では鍵にならない★★（2026-09-14・自分で測って確かめた）
        #   ★測ったこと★＝DMMの機種ページを、取得の控えを消して2回取ると
        #   大きさは同じ（136700字）なのに指紋は一致しない。
        #   違うのは `<meta name="csrf-token" content="...">` で、毎回変わる。
        #   ＝★2AIがどれだけ正しく判断しても、その答えは二度と使えない★
        #   （クチコミが1〜2件付いた新台が、出典ごと使えなくなっていた）。
        #   ★2026-09-08に出典の確かめ直しで同じ形を直したのに、ここに残っていた★（罠㊺）。
        #   ★★外した分は埋まっていない（台帳#662・未解決）★★
        #   （2026-09-18・Codexの指摘・自分で確かめた）
        #   ★直後にここへ「外しても弱くならない」と書いていたが、誤り★＝
        #   いま見ているのは3つ（①箱が戻れば `now` が空 ②`now <= want`
        #   ③手がかりの逐語が残っている）だが、
        #   ★未知の名前の箱（例 `class="opinion-v2"`）に読者の書き込みが
        #   足されると、3つとも通る★（新しい箱は「欠けた必須の箱」ではない）。
        #   外した全文の指紋なら確実に失効していた。
        # ★★2026-09-21に「作りの指紋」で埋めた★★（運営者の判断＝作り直す）
        #   ★見るのはタグ名・class・id だけ★＝属性の**値**を見ないので、
        #   毎回変わるもの（csrf-token・画像の `?t=`）は自然に入らない。
        #   ★実測★＝DMMの機種ページを20秒あけて2回取り、全文の指紋は
        #   違ったが、この指紋は一致した（箱1065個の並びが完全一致）。
        #   ＝★未知の箱を1つ足されれば必ず変わる★ので、
        #   「新しい投稿欄を足されても免除が通る」穴が塞がる。
        # ★★掃除したあとの姿で取る★★（2026-09-21・Codexの指摘・実測で確かめた）
        #   ★直す前は生HTML全体★だったので、DMMの設置店が1軒増えるだけで
        #   ★2AIの判断が失効していた★（店舗の一覧は日々変わる）。
        #   掃除したあとなら店舗は落ちているので入らない。
        # ★★読む文字の指紋も見る★★＝既存の汎用の `<p>` の中へ
        #   読者の文字だけを足されると、タグも class も深さも変わらないので
        #   作りだけでは気づけない（Codexが挙げた反例）。
        _want_sig = str(rec.get("waiver_structure_sha256") or "")
        _want_txt = str(rec.get("waiver_text_sha256") or "")
        _url = str(rec.get("url") or "")
        if not _url:
            # ★どのページを見て決めたか分からなければ通さない★＝
            #   掃除の決まりごとを引けないので、同じ姿を作り直せない。
            return False, "控えにページのURLがありません（同じ姿を作り直せません）"
        try:
            import user_area as _ua_sig
            _now = _ua_sig.waiver_fingerprints(str(raw or ""), _url)
        except Exception as e:                               # noqa: BLE001
            # ★数え直せないなら通さない★（同じだと言えない）
            return False, f"いまのページの作りを数えられません（{type(e).__name__}）"
        if not _want_sig or not _want_txt:
            return False, "控えにページの指紋がありません"
        if _want_sig != _now.get("structure_sha256"):
            return False, ("ページの作りが変わっています"
                           "（箱が足された・減った可能性があります）")
        if _want_txt != _now.get("text_sha256"):
            return False, ("ページの読める文字が変わっています"
                           "（読者の書き込みが足された可能性があります）")
        for q in (rec.get("quotes") or []):
            if str(q) not in str(raw or ""):
                return False, f"手がかりの逐語が、いまのページにありません（{str(q)[:30]}）"
        return True, "免除してよい箱だけで、作りも手がかりの逐語も変わっていません"
    return False, f"知らない答えです: {kind!r}"


def record(slug: str, url: str, stage: str, kind: str, *, raw: str,
           failed_contract: str = "",
           agreed_by: list, why: str, decided_at: str,
           evidence: str = "", evidence_scope: str = "",
           fields: dict | None = None, waived_boxes: list | None = None,
           quotes: list | None = None) -> dict:
    """★2AIが決めたことを控える★（★契約を満たさないものは保存しない★）"""
    rec = {"kind": kind, "stage": stage,
           # ★どの契約が満たせなかったときの答えか★（2026-09-10・CodexのP1）
           "failed_contract": str(failed_contract or ""),
           # ★誰のために読んだか★（鍵ではない・追えるように残す）
           "for_slug": str(slug or ""),
           "agreed_by": list(agreed_by or []),
           "why": str(why or "").strip(),
           "decided_at": str(decided_at or ""),
           # ★★どのページを見て決めたか★★（2026-09-21・Codexの指摘）
           #   ★直す前は控えに入っていなかった★ので、照合のときに
           #   空のURLで掃除の決まりごとを引き、★DMMでは `drop` が
           #   1つも当たらなかった★＝免除は保存の直前の自己照合で必ず落ち、
           #   **一度も使えなかった**（実際に再現した）。
           "url": str(url or ""),
           "raw_sha256": _rf.sha256(raw),
           # ★「読む文字」の指紋★（UNUSABLE の照合はこちらを使う）
           "text_sha256": _text_sha(raw),
           "asked_schema": _rf.ASK_SCHEMA}
    if kind == READ_FACTS:
        rec["evidence"] = str(evidence or "")
        rec["evidence_scope"] = str(evidence_scope or "")
        rec["fields"] = dict(fields or {})
    if kind == WAIVE_MISSING_USER_BOX:
        rec["waived_boxes"] = list(waived_boxes or [])
        rec["quotes"] = list(quotes or [])
        # ★★そのときのページの「作り」を控える★★（2026-09-21・台帳#662/#669）
        #   ★揺れる値は入らない★（タグ名・class・id しか見ない）ので、
        #   取り直しでは失効しない。★箱が足されれば必ず失効する★。
        import user_area as _ua_rec
        # ★★名前を分ける★★（2026-09-21）＝`text_sha256` は「読めない」の
        #   控えが**生の本文**の指紋として使っている鍵。同じ名前に
        #   掃除後の指紋を入れると、★同じ鍵が型によって別の意味★になる。
        _wf = _ua_rec.waiver_fingerprints(str(raw or ""), str(url or ""))
        rec["waiver_structure_sha256"] = _wf["structure_sha256"]
        rec["waiver_text_sha256"] = _wf["text_sha256"]
    bad = validate(rec)
    if bad:
        raise ReadingError("この記録は契約を満たしません（保存しませんでした）: "
                           + " ／ ".join(bad[:5]))
    # ★保存する前に、いまのHTMLで通ることを確かめる★
    ok, why_v = verify(rec, raw, stage=stage,
                       missing_boxes=waived_boxes or [])
    if kind != UNUSABLE and not ok:
        raise ReadingError("いまのページで確かめられませんでした: " + why_v)
    data = load(strict=False)
    data["pages"][key_of(url, stage, failed_contract)] = rec
    _save(data)
    return {"state": "RECORDED", "slug": slug, "url": url, "stage": stage,
            "kind": kind}


def find(url: str, stage: str, failed_contract: str = "") -> dict | None:
    """★控えを引く★（無ければ None）"""
    try:
        data = load(strict=False)
    except Exception:                                        # noqa: BLE001
        return None
    return (data.get("pages") or {}).get(key_of(url, stage, failed_contract))


def forget(url: str, stage: str, failed_contract: str = "") -> dict:
    data = load(strict=False)
    k = key_of(url, stage, failed_contract)
    if k not in (data.get("pages") or {}):
        return {"state": "NOT_FOUND"}
    data["pages"].pop(k)
    _save(data)
    return {"state": "FORGOTTEN"}


def selftest() -> int:                                       # noqa: C901
    import tempfile
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    keep = STORE
    globals()["STORE"] = os.path.join(
        tempfile.mkdtemp(prefix="uchi_pr_"), "page_reading.json")
    try:
        # ★毎回変わる値を、実ページと同じ形で入れておく★（2026-09-21）
        #   DMMは csrf-token の値と、画像のURLに付く時刻を取るたびに変える。
        #   ★試験も「値だけが変わる」形で書く★＝タグを足す形で真似ると、
        #   作りの指紋が変わるのは当たり前で、確かめたいことが確かめられない。
        RAW = ("<html><head><style>a{}</style>"
               '<meta name="csrf-token" content="y41mw51OQARB">'
               "</head><body>"
               "<table><tr><th>メーカー名</th><td>北電子</td></tr>"
               "<tr><th>導入開始日</th><td>2026年10月5日</td></tr></table>"
               "<p>ユーザー評価（2件）</p>"
               '<a href="/machines/5090/review">くちこみ</a>'
               "</body></html>")
        CAP = ("<table><tr><th>メーカー名</th><td>北電子</td></tr>"
               "<tr><th>導入開始日</th><td>2026年10月5日</td></tr></table>")
        _ok = record("dmm_5054", "https://example.invalid/5054",
                     _rf.STAGE_IDENTITY_FACTS, READ_FACTS, raw=RAW,
                     failed_contract="メーカー名と導入開始日が同じ表にある",
                     agreed_by=["claude", "codex"],
                     why="閉じていない飾りの後ろにある表を読みました",
                     decided_at="2026-09-10", evidence=CAP,
                     evidence_scope=RAW_RESPONSE,
                     fields={"maker": "北電子",
                             "release_date": "2026年10月5日"})
        t("★2AIが読んだ値を、根拠の範囲つきで控えられる★",
          _ok["state"] == "RECORDED")
        rec = find("https://example.invalid/5054",
                   _rf.STAGE_IDENTITY_FACTS, "メーカー名と導入開始日が同じ表にある")
        ok, _w = verify(rec, RAW, stage=_rf.STAGE_IDENTITY_FACTS,
                        labels=["メーカー名", "導入開始日"])
        t("　いまのページに範囲が在れば通る", ok)
        ok2, w2 = verify(rec, RAW.replace("北電子", "サミー"),
                         stage=_rf.STAGE_IDENTITY_FACTS)
        t("★★範囲が変わっていたら通さない★★"
          "（★広告の日替わりでは失効しないが、根拠が変われば失効する★）",
          not ok2 and "ありません" in w2)
        ok3, w3 = verify(rec, RAW + RAW, stage=_rf.STAGE_IDENTITY_FACTS)
        t("★★同じ範囲が2か所あれば通さない★★"
          "（★似た表から拾ってきた可能性を消せない・Codexの指摘★）",
          not ok3 and "2 か所" in w3)
        ok4, w4 = verify(rec, RAW, stage=_rf.STAGE_USER_AREA)
        t("★★別の段には効かない★★"
          "（★投稿欄の失敗を、値を読む免除で迂回できてしまう★）",
          not ok4 and "別の段" in w4)
        # ★値が範囲の外にあるものは保存しない★
        _blocked = False
        try:
            record("dmm_5054", "https://example.invalid/x",
                   _rf.STAGE_IDENTITY_FACTS, READ_FACTS, raw=RAW,
                   failed_contract="メーカー名と導入開始日が同じ表にある",
                   agreed_by=["claude", "codex"], why="ためしに書きます",
                   decided_at="2026-09-10", evidence=CAP,
                   evidence_scope=RAW_RESPONSE,
                   fields={"maker": "京楽"})
        except ReadingError:
            _blocked = True
        t("★値が根拠の範囲の外なら保存しない★", _blocked)
        _b2 = False
        try:
            record("dmm_5054", "https://example.invalid/y",
                   _rf.STAGE_IDENTITY_FACTS, READ_FACTS, raw=RAW,
                   failed_contract="メーカー名と導入開始日が同じ表にある",
                   agreed_by=["claude"], why="ひとりで決めました",
                   decided_at="2026-09-10", evidence=CAP,
                   evidence_scope=RAW_RESPONSE, fields={"maker": "北電子"})
        except ReadingError:
            _b2 = True
        t("　判断者が1人なら保存しない", _b2)
        _b3 = False
        try:
            record("dmm_5054", "https://example.invalid/z",
                   _rf.STAGE_IDENTITY_FACTS, READ_FACTS, raw=RAW,
                   failed_contract="メーカー名と導入開始日が同じ表にある",
                   agreed_by=["claude", "codex"], why="どこを読んだか言いません",
                   decided_at="2026-09-10", evidence=CAP,
                   fields={"maker": "北電子"})
        except ReadingError:
            _b3 = True
        t("★どこを読んだのかを名乗らないと保存しない★"
          "（★画面に出ない原文から採ったなら、そう名乗る★）", _b3)

        # ★★同じ段でも、別の契約の答えは流用されない★★
        #   （2026-09-10・CodexのP1）★鍵が段までだと、同じ段の別の失敗に
        #   前の答えが流用・上書きされる★
        t("★★同じ段でも、別の契約の答えは引けない★★"
          "（★流用されると、確かめていない読み方が通ってしまう★）",
          find("https://example.invalid/5054", _rf.STAGE_IDENTITY_FACTS,
               "まったく別の契約") is None)
        t("　同じ契約なら引ける",
          find("https://example.invalid/5054", _rf.STAGE_IDENTITY_FACTS,
               "メーカー名と導入開始日が同じ表にある") is not None)

        # ★★問いの書き方が変わったら、古い答えは通さない★★（CodexのP1）
        _old = dict(rec)
        _old["asked_schema"] = "read-failure/v0"
        _oko, _wo = verify(_old, RAW, stage=_rf.STAGE_IDENTITY_FACTS)
        t("★★問いの書き方が変わったら、古い答えは通さない★★"
          "（★聞き方の意味を変えても古い判断が通っていた★）",
          not _oko and "問いの書き方" in _wo)

        # ★★ラベルと値の対応まで見る★★（CodexのP1）
        _swapped = dict(rec)
        _swapped["fields"] = {"maker": "2026年10月5日",
                              "release_date": "北電子"}
        t("★★ラベルと値が入れ替わっていたら通さない（縦持ち）★★"
          "（★値が範囲のどこかにあれば通る、では取り違えを見つけられない★）",
          any("の値として書かれていません" in x for x in validate(_swapped)))
        # ★★横持ちの表でも見る★★（2026-09-10・CodexのP1④）
        #   ★順序だけの検査は、ここで破れる★＝
        #   ラベル行が先に並ぶので、値を入れ替えても「ラベルが前」は成り立つ。
        _WIDE = ("<table><tr><th>メーカー名</th><th>導入開始日</th></tr>"
                 "<tr><td>北電子</td><td>2026年10月5日</td></tr></table>")
        _w_ok = dict(rec)
        _w_ok["evidence"] = _WIDE
        _w_ok["fields"] = {"maker": "北電子",
                           "release_date": "2026年10月5日"}
        t("　横持ちの表で、正しい対応なら通る", validate(_w_ok) == [])
        _w_ng = dict(_w_ok)
        _w_ng["fields"] = {"maker": "2026年10月5日",
                           "release_date": "北電子"}
        t("★★横持ちの表で入れ替わっていたら通さない★★"
          "（★ここが順序だけの検査では破れる★）",
          any("の値として書かれていません" in x for x in validate(_w_ng)))
        _unknown = dict(rec)
        _unknown["fields"] = dict(rec.get("fields") or {})
        _unknown["fields"]["なにかの値"] = "北電子"
        t("　知らない項目は受け取らない（自由な辞書にしない）",
          any("知らない項目です" in x for x in validate(_unknown)))
        _notable = dict(rec)
        _notable["evidence"] = "メーカー名は北電子で、導入開始日は2026年10月5日です。"
        t("　表として読めない根拠は通さない（迷ったら使わない）",
          any("表として読めません" in x for x in validate(_notable)))

        # ── 投稿欄の免除 ──────────────────────────
        def _rec_ok(**kw):
            """★断られても死なない★＝例外は「試験が❌」として数える
            （罠⑤＝落ちると『ただ落ちただけ』に化けて守りの証拠にならない）。
            """
            try:
                return record(**kw)
            except Exception as e:                           # noqa: BLE001
                return {"state": "★断られました★ " + str(e)[:80]}

        _w = _rec_ok(slug="dmm_5090", url="https://example.invalid/5090",
                     stage=_rf.STAGE_USER_AREA, kind=WAIVE_MISSING_USER_BOX,
                     raw=RAW,
                     failed_contract="件数が1件以上なら投稿欄の一覧の箱がある",
                     agreed_by=["claude", "codex"],
                     why="件数の表示はあるが本文はこのHTMLに無い",
                     decided_at="2026-09-10",
                     waived_boxes=["list-machinesreviews"],
                    # ★逐語は「印つきの場所」も1件入れる★＝
                    #   作り（タグ・class・id・深さ）にも読む文字にも
                    #   出ない属性の値が変わった形を、逐語の層だけで捕まえる。
                     quotes=["ユーザー評価（2件）",
                             '<a href="/machines/5090/review">'])
        t("★無い箱の免除を控えられる★", _w["state"] == "RECORDED")
        wrec = find("https://example.invalid/5090",
                    _rf.STAGE_USER_AREA, "件数が1件以上なら投稿欄の一覧の箱がある")
        def _vw(rec, html, boxes=None):
            """★控えが無くても死なない★（上と同じ理由）。"""
            if not rec:
                return False, "★控えがありません★"
            return verify(rec, html, stage=_rf.STAGE_USER_AREA,
                          missing_boxes=(["list-machinesreviews"]
                                         if boxes is None else boxes))

        okw, _ = _vw(wrec, RAW)
        t("　その箱が無いときだけ通る", okw)
        okw2, ww2 = _vw(wrec, RAW, ["list-machinesreviews", "別の箱"])
        t("★★控えに無い箱まで免除しない★★"
          "（★1つ免除したら全部通る、にしない★）",
          not okw2 and "控えに無い箱" in ww2)
        # ★★ページの他の場所が変わっても、答えは効く★★（2026-09-14）
        #   ★測ったこと★＝DMMの機種ページは取るたびに `csrf-token` が変わり、
        #   全体の指紋は二度と一致しない（控えを消して2回取って確認）。
        #   ＝全体の指紋を鍵にすると、★2AIが正しく判断しても永久に使えない★。
        okw3, _ww3 = verify(
            wrec, RAW.replace("y41mw51OQARB", "ISiuxLab9lxK")
            if "y41mw51OQARB" in RAW
            else RAW + "<meta name='csrf-token' content='毎回変わる値'>",
            stage=_rf.STAGE_USER_AREA,
            missing_boxes=["list-machinesreviews"])
        t("★★関係のない所が変わっても、2AIの答えは効く★★"
          "（★全体の指紋を鍵にすると、毎回変わるページでは永久に使えない★）",
          okw3)
        okw3b, ww3b = verify(wrec, RAW.replace("ユーザー評価（2件）", "評価はまだありません"),
                             stage=_rf.STAGE_USER_AREA,
                             missing_boxes=["list-machinesreviews"])
        t("★★そう判断した手がかりが消えたら効かせない★★",
          not okw3b and ("読める文字が変わっています" in ww3b
                         or "手がかりの逐語" in ww3b))
        # ★★逐語の層だけを測る★★（2026-09-21）＝
        #   作りにも読む文字にも出ない所（リンクの飛び先）だけを変える。
        #   ★これが無いと、読む文字の指紋が手前で止めるので
        #   逐語の層が一度も試されない★（罠③と同じ形）。
        _mark = '<a href="/machines/5090/review">'
        okw3c, ww3c = verify(
            wrec, RAW.replace(_mark, '<a href="/machines/9999/review">'),
            stage=_rf.STAGE_USER_AREA,
            missing_boxes=["list-machinesreviews"])
        t("★飛び先だけ変えた形は、逐語の層が捕まえる★"
          "（作りも読む文字も変わらない）",
          not okw3c and "手がかりの逐語" in ww3c)
        # ★★読む文字の層だけを測る★★（2026-09-21・Codexの反例そのもの）
        #   既存のタグの中へ文字だけを足す＝タグも class も深さも
        #   文字の場所も変わらず、逐語も全部残る。
        #   ★これを捕まえられるのは読む文字の指紋だけ★
        okw3d, ww3d = verify(
            wrec, RAW.replace("<td>北電子</td>",
                              "<td>北電子 天井1200G引けました</td>"),
            stage=_rf.STAGE_USER_AREA,
            missing_boxes=["list-machinesreviews"])
        # ★★★掃除の決まりごとを持つホストで通す★★★（2026-09-21・Codexの指摘）
        #   ★直す前は `example.invalid`（決まりごと無し）だけ★だったので、
        #   ★照合でURLが失われ `drop` が1つも当たらない★配線切れを
        #   捕まえられなかった（実際にDMMでは免除が**一度も保存できなかった**）。
        _dmm = "https://p-town.dmm.com/machines/5090"
        _dmm_html = (
            "<html><body>"
            '<div class="machine-userreview"><p>ユーザー評価（2件）</p></div>'
            '<div class="machine-shop-by-prefecture"><p>A店</p></div>'
            '<div class="list-machineinformation">'
            "<table><tr><th>メーカー名</th><td>ユニバーサル</td></tr>"
            "<tr><th>導入開始日</th><td>2026年10月5日</td></tr></table></div>"
            '<div class="wysiwyg-box">天井は1200Gです。</div>'
            "</body></html>")
        _dw = _rec_ok(slug="dmm_5090", url=_dmm, stage=_rf.STAGE_USER_AREA,
                      kind=WAIVE_MISSING_USER_BOX,
                      raw=_dmm_html,
                      failed_contract="件数が1件以上なら投稿欄の一覧の箱がある",
                      agreed_by=["claude", "codex"],
                      why="件数の表示はあるが本文はこのHTMLに無い",
                      decided_at="2026-09-21",
                      waived_boxes=["list-machinesreviews"],
                      quotes=["ユーザー評価（2件）"])
        t("★★掃除の決まりごとを持つホストでも控えられる★★"
          "（★直す前は保存の直前の自己照合で必ず落ちた★）",
          _dw["state"] == "RECORDED")
        _drec = find(_dmm, _rf.STAGE_USER_AREA,
                     "件数が1件以上なら投稿欄の一覧の箱がある")
        t("　控えにページのURLが入っている（掃除の決まりごとを引くため）",
          str((_drec or {}).get("url") or "") == _dmm)

        def _v(rec, html):
            """★控えが無くても死なない★（上と同じ理由）。"""
            if not rec:
                return False, "★控えがありません★"
            return verify(rec, html, stage=_rf.STAGE_USER_AREA,
                          missing_boxes=["list-machinesreviews"])

        _dok, _ = _v(_drec, _dmm_html)
        t("　同じHTMLなら通る", _dok)
        _dok2, _ = _v(
            _drec, _dmm_html.replace("<p>A店</p>", "<p>A店</p><p>B店</p>"))
        t("★★設置店が1軒増えても、2AIの判断は効いたまま★★"
          "（★生HTMLで取ると、店が増えるたびに失効する★）",
          _dok2)
        _dok3, _dwhy3 = _v(
            _drec,
            _dmm_html.replace("</body>",
                              '<div class="opinion-v2">'
                              "<p>昨日は天井1200Gまでハマりました。"
                              "朝一はリセットっぽい挙動でした。</p>"
                              "</div></body>"))
        t("★★未知の箱に書き込みが足されたら失効する★★",
          not _dok3)
        _dok4, _dwhy4 = _v(
            _drec,
            _dmm_html.replace("<td>ユニバーサル</td>",
                              "<td>ユニバーサル 天井1200G引けました</td>"))
        t("★★残る箱の中へ文字だけ足されても失効する★★",
          not _dok4 and "読める文字が変わっています" in _dwhy4)
        _nourl = {k: v for k, v in (_drec or {}).items() if k != "url"}
        _dok5, _dwhy5 = _v(_nourl, _dmm_html)
        t("★URLの無い控えは通さない★（同じ姿を作り直せない）"
          "／★止めるのは契約の層★＝照合の側の同じ検査は受け皿"
          "（契約を通ったものしか来ないので、普段は発火しない）",
          not _dok5 and "どのページを見て決めたか" in _dwhy5)
        t("　契約もURLを求める",
          any("どのページを見て決めたか" in x
              for x in validate({**(_drec or {}), "url": ""})))

        t("★★既存の箱の中へ読者の文字だけ足された形を捕まえる★★"
          "（★作りも逐語も変わらないので、ここでしか気づけない★）",
          not okw3d and "読める文字が変わっています" in ww3d)
        # ★★未知の箱に読者の書き込みを足されたら効かせない★★
        #   （2026-09-21・台帳#662/#669。Codexが求めた回帰試験そのもの）
        #   ★直す前はここが通っていた★＝新しい箱は「欠けた必須の箱」に
        #   入らないので、3つの検査（箱・範囲・逐語）を全部すり抜けた。
        _added = RAW.replace(
            "</body>",
            '<div class="opinion-v2"><p>天井は1200Gでした</p></div></body>')
        okw3c, ww3c = verify(wrec, _added, stage=_rf.STAGE_USER_AREA,
                             missing_boxes=["list-machinesreviews"])
        t("★★未知の箱（例 opinion-v2）を足されたら、免除を効かせない★★"
          "（★逐語は残ったまま読者の数値が混ざる経路★）",
          not okw3c and "作りが変わっています" in ww3c)
        t("　控えに作りの指紋が無ければ受け取らない",
          any("作りの指紋がありません" in x
              for x in validate({**wrec, "waiver_structure_sha256": ""})))
        okw4, ww4 = _vw(wrec, RAW, [])
        t("　箱が見つかっているなら免除しない", not okw4)
        # ★使わないという答え★
        _u = record("dmm_9999", "https://example.invalid/9", _rf.STAGE_USER_AREA,
                    UNUSABLE, raw=RAW, failed_contract="件数が1件以上なら投稿欄の一覧の箱がある",
                    agreed_by=["claude", "codex"],
                    why="読者の書き込みがあるか判断できません",
                    decided_at="2026-09-10")
        oku, wu = verify(find("https://example.invalid/9",
                              _rf.STAGE_USER_AREA, "件数が1件以上なら投稿欄の一覧の箱がある"), RAW,
                         stage=_rf.STAGE_USER_AREA)
        t("★分からないときは「使わない」と控えられ、通らない★",
          _u["state"] == "RECORDED" and not oku and "使わない" in wu)
        _uold = find("https://example.invalid/9", _rf.STAGE_USER_AREA,
                     "件数が1件以上なら投稿欄の一覧の箱がある")
        _oku2, _wu2 = verify(_uold, RAW + "<p>直りました</p>",
                             stage=_rf.STAGE_USER_AREA)
        t("★★ページが変われば「使わない」も効かない★★"
          "（★相手が直して読めるようになっても、古い判断が残り続けた★）",
          not _oku2 and "効かせません" in _wu2)
        # ★★取り直すたびに変わる飾りでは失効させない★★（2026-09-18・台帳#696）
        #   ★直す前は生HTML全体の指紋だった★ので、
        #   なな徹のCSS（unix秒）やDMMの csrf-token だけで毎回失効し、
        #   同じページを毎晩2AIに聞き直して3回の枠を食いつぶしていた。
        _noisy = ('<link href="/css/g.css?1789700770">'
                  + RAW + '<img src="/i.png?t=1789700771">')
        _oku3, _wu3 = verify(_uold, _noisy, stage=_rf.STAGE_USER_AREA)
        #   ★断った理由の文まで見る★（罠㉚）＝「ページが変わっているので、
        #   「使わない」は効かせません」にも『使わない』が入っているので、
        #   その語を探すだけだと**失効していても緑**になる（実際になった）。
        t("★★飾りだけ変わっても「使わない」は効き続ける★★"
          "（★失効すると毎晩同じページを聞き直して枠を使い切る★）",
          not _oku3 and "効かせません" not in _wu3
          and _wu3 == "2AIが「このページは使わない」と決めています")
        t("　★対照★＝その飾りで生HTMLの指紋のほうは実際に変わっている",
          _rf.sha256(_noisy) != _rf.sha256(RAW))
        t("　取り除ける",
          forget("https://example.invalid/9",
                 _rf.STAGE_USER_AREA, "件数が1件以上なら投稿欄の一覧の箱がある")["state"] == "FORGOTTEN")
    finally:
        globals()["STORE"] = keep
    ng = [n for n, ok in results if not ok]
    print(f"{len(results) - len(ng)}/{len(results)} 合格" if not ng
          else "失敗: " + json.dumps(ng, ensure_ascii=False))
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="機械が読めなかったページの控え")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.list:
        data = load(strict=False)
        for k, rec in sorted((data.get("pages") or {}).items()):
            stage, _cid, url = k.split(" ", 2)
            print(f"■ {stage} ／ 読んだ機種: {rec.get(chr(34)+chr(34)+chr(34))}")
            print(f"   {url}")
            print(f"   {rec.get('kind')} ／ {rec.get('why')}"
                  f"（{rec.get('decided_at')}）")
            for x in validate(rec):
                print("   ✗ " + x)
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

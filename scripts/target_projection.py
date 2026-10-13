#!/usr/bin/env python3
"""★新台の狙い目を、チェッカーの線から4か所へそろえて出すための唯一の判定★（2026-10-04）

★何のためか★＝新台ページでは、2AIがチェッカーに入れた線（good など）と、
記事の「当サイトの狙い目」の欄・狙い目早見表・期待値早見表の判定が、
★同じ線から作られて同じ時に出る★必要がある（Codex review221）。
直す前は記事が「未確認」のまま判定だけ出て食い違った。

★ここが持つもの（ほかに同じ規則を書かない）★
  states(m)        読者が選べる状態を全部列挙する（画面の getConfig と同じ解決）
  renderable(m)    全状態に使える線がそろっているか
  projection_id(m) 全状態の指紋（記事と機種行に書き、ページは2つが等しいときだけ出す）
  box_text(m)      狙い目早見表の箱の文
  article_text(m)  記事の「当サイトの狙い目」の文
  sync_problems(m, detail)  関所用：書いてある指紋・文が、いまの線から作ったものと同じか

★範囲★＝交換率の切替を持たない新台だけ（新台経路は交換率を作らない＝いま0件）。
  交換率を持つ機種・modeData を持つ機種は「そろっていない」扱い（隠したまま）。
★新しい数字は作らない★（文は target_display.parts と同じ部品）。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import target_display as _td                                # noqa: E402

SECTION_TITLE = "当サイトの狙い目"
BOX_LABEL = "狙い目"
KEY = "targetProjection"
NO_TARGET = _td.NO_TARGET
_LINE_KEYS = ("caution", "good", "excellent", "target", "ceiling")


def _good_ok(v) -> bool:
    """★使える線か★＝正の整数・番兵未満・真偽値でない（400.0 は使わない＝Python と JS の差を作らない）"""
    return type(v) is int and 0 < v < NO_TARGET


def _rows(conf: dict):
    """画面の getConfig と同じ順（suru → cycle）で、回数の表を返す。無ければ None。"""
    for k in ("suru", "cycle"):
        if isinstance(conf.get(k), list):
            return k, conf.get(k)
    return None, None


def states(m: dict) -> list | None:
    """★読者が選べる状態を全部列挙する★（作れない形なら None）

    1状態＝{"mode","label","kind","count","caution","good","excellent","target","ceiling"}。
    ★画面と同じ解決★＝checker[モード]（modeData は読まない）／回数の表なら各行そのもの。
    """
    ck = (m or {}).get("checker")
    if not isinstance(ck, dict):
        return None
    if ck.get("exchangeRates") or ck.get("modeData"):
        return None                      # ★範囲外★（交換率・modeData）
    modes = ck.get("modes")
    if not isinstance(modes, list) or not modes:
        return None
    seen = set()
    out = []
    for md in modes:
        if not isinstance(md, dict):
            return None
        key = str(md.get("key") or "")
        label = str(md.get("label") or "").strip()
        if not key or not label or key in seen:
            return None                  # ★キー重複・空の呼び名は作れない★
        seen.add(key)
        conf = ck.get(key)
        if not isinstance(conf, dict) or not conf or _td.is_off(conf):
            return None
        kind, rows = _rows(conf)
        cands = rows if kind else [conf]
        if not cands:
            return None
        for r in cands:
            if not isinstance(r, dict) or _td.is_off(r):
                return None
            st = {"mode": key, "label": label, "kind": kind or "mode",
                  "count": r.get("count") if kind else None}
            for k in _LINE_KEYS:
                st[k] = r.get(k)
            out.append(st)
    return out


def renderable(m: dict) -> bool:
    """★全状態に使える線がそろっているか★（そろっていなければ4か所とも出さない）"""
    ck = (m or {}).get("checker") or {}
    if str(ck.get("unit") or "G") != "G":
        return False
    st = states(m)
    if not st:
        return False
    if not all(_good_ok(s["good"]) for s in st):
        return False
    # ★文が作れること★（parts が全欄を出すこと＝欠けた要約を出さない）
    return len(_td.parts(ck, "")) == len(ck.get("modes") or [])


def projection_id(m: dict) -> str:
    """★全状態の指紋★（そろっていなければ空文字）"""
    if not renderable(m):
        return ""
    ck = m["checker"]
    body = {"unit": str(ck.get("unit") or "G"), "states": states(m),
            "text": box_text(m)}
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def box_text(m: dict) -> str:
    """狙い目早見表の箱の文（例「通常400G〜」「BIG後700G〜 / REG後360G〜」）。"""
    ps = _td.parts((m or {}).get("checker") or {}, "")
    return " / ".join(p["text"] for p in ps)


def article_text(m: dict) -> str:
    """記事の「当サイトの狙い目」の文（です・ます）。"""
    t = box_text(m)
    return ("狙い目は、%sです。" % t) if t else ""


def detail_fields(machine_row: dict) -> dict:
    """★記事データに入れる中身★（build_detail が使う唯一の入口）

    → {"body": [...] or None, "summaryBoxes": [...], "projection": "..."}
    そろっていなければ body=None（未確認のまま）・箱なし・指紋なし。
    """
    pid = projection_id(machine_row or {})
    if not pid:
        return {"body": None, "summaryBoxes": [], "projection": ""}
    return {"body": [article_text(machine_row)],
            "summaryBoxes": [{"label": BOX_LABEL, "value": box_text(machine_row),
                              "role": "target"}],
            "projection": pid}


def sync_problems(m: dict, detail: dict | None) -> list:
    """★関所用★＝書いてある指紋と文が、いまの線から作ったものと同じか（違えば理由）

    ★指紋が両側とも無い機種は何も見ない★（そろっていない＝出ていない）。
    ★片側だけに指紋があるのは食い違い★（2026-10-13・Codex review228）＝記事にだけ残ると、
    チェッカー等は隠れても記事の狙い目の文（前の線から作ったもの）が読者に出る。
    """
    slug = str((m or {}).get("slug") or "")
    mid = str((m or {}).get(KEY) or "")
    did = str(((detail or {}).get(KEY)) or "")
    if not mid and not did:
        return []
    ng = []
    want = projection_id(m)
    # ★★片側だけに指紋があるのも食い違い★★（2026-10-13・Codex review228 のP1）
    #   ★直す前は、残った方の指紋がいまの線と合っていれば0件だった★＝
    #   記事にだけ指紋と生成した文が残ると、本文の照合もされずに古い文が読者に出る。
    if bool(mid) != bool(did):
        ng.append(f"{slug}: 指紋が" + ("機種行にだけ" if mid else "記事にだけ")
                  + "あります（片側だけでは4か所がそろっていません・記事の狙い目の欄を"
                  "未確認に戻すか、翌朝の作り直しで両方に付けること）")
    if mid and mid != want:
        ng.append(f"{slug}: 機種行の指紋がいまのチェッカーの線と合いません"
                  "（線を書いたら指紋を消すこと＝翌朝の作り直しで付け直す）")
    if did and did != want:
        ng.append(f"{slug}: 記事の指紋がいまのチェッカーの線と合いません")
    if mid and did and mid == did == want:
        secs = [s for s in ((detail or {}).get("sections") or [])
                if isinstance(s, dict) and s.get("title") == SECTION_TITLE]
        if len(secs) != 1 or secs[0].get("body") != [article_text(m)]:
            ng.append(f"{slug}: 記事の「{SECTION_TITLE}」の文が、チェッカーの線から作った文と違います")
        boxes = [b for b in ((detail or {}).get("summaryBoxes") or [])
                 if isinstance(b, dict) and b.get("role") == "target"]
        if boxes != [{"label": BOX_LABEL, "value": box_text(m), "role": "target"}]:
            ng.append(f"{slug}: 狙い目早見表の箱が、チェッカーの線から作ったものと違います")
    return ng


def unsync_detail(detail: dict) -> bool:
    """★記事から、指紋つきで作った狙い目の中身を外して「未確認」に戻す★（書いたら True）

    （2026-10-13・Codex review228 のP1）＝線を変えて機種行の指紋を外したのに、
    記事の「当サイトの狙い目」の文（前の線から作ったもの）が残ると、チェッカー等は
    隠れても記事にだけ古い値が出る。★指紋がある記事だけ触る★＝指紋は「この文は
    線から作った」印なので、人や2AIが書いた文は消さない。
    """
    if not isinstance(detail, dict) or not detail.get(KEY):
        return False
    from build_new_article import PENDING_TEXT
    for s in detail.get("sections") or []:
        if isinstance(s, dict) and s.get("title") == SECTION_TITLE:
            s["body"] = [PENDING_TEXT]
    boxes = detail.get("summaryBoxes")
    if isinstance(boxes, list):
        detail["summaryBoxes"] = [b for b in boxes
                                  if not (isinstance(b, dict) and b.get("role") == "target")]
    detail.pop(KEY, None)
    return True


def synced(m: dict, detail: dict | None) -> bool:
    """★4か所を出してよいか★（ページの判定と同じ答え＋中身の照合）"""
    mid = str((m or {}).get(KEY) or "")
    did = str(((detail or {}).get(KEY)) or "")
    return bool(mid) and mid == did and not sync_problems(m, detail)


# --- 自己試験 ---------------------------------------------------------------

def selftest() -> int:
    ok = [0, 0]

    def t(name, cond):
        ok[1] += 1
        if cond:
            ok[0] += 1
            print("✅", name)
        else:
            print("❌", name)

    def mm(**ck):
        base = {"unit": "G", "modes": [{"key": "normal", "label": "通常"}],
                "normal": {"ceiling": 900, "caution": 300, "good": 400, "excellent": 600}}
        base.update(ck)
        return {"slug": "zzz_auto", "publication_policy": "page-decision/v1", "checker": base}

    m1 = mm()
    t("★線がそろっている新台は出せる★", renderable(m1) and projection_id(m1))
    t("　文は既存機種と同じ部品から作る", box_text(m1) == "通常400G〜"
      and article_text(m1) == "狙い目は、通常400G〜です。")
    t("★★線の無い欄が1つでもあれば出さない★★",
      not renderable(mm(modes=[{"key": "normal", "label": "通常"}, {"key": "reset", "label": "リセット"}],
                        reset={"ceiling": 600})))
    t("★★回数の表の一部の行にしか線が無ければ出さない★★",
      not renderable(mm(modes=[{"key": "suru", "label": "スルー"}],
                        suru={"suru": [{"count": 0, "good": 3}, {"count": 1}]})))
    t("★数字でない good（400.0・文字・真偽値）は線に数えない★",
      not renderable(mm(normal={"good": 400.0})) and not renderable(mm(normal={"good": "400"}))
      and not renderable(mm(normal={"good": True})))
    t("★番兵（99999）・停止中の欄は出さない★",
      not renderable(mm(normal={"good": 99999}))
      and not renderable(mm(normal={"good": 400, "_disabled": True})))
    t("★範囲外（交換率・modeData・G以外）は出さない★",
      not renderable(mm(exchangeRates=[{"key": "eq56", "label": "5.6枚"}]))
      and not renderable(mm(modeData={"normal": {"good": 400}}))
      and not renderable(mm(unit="pt")))
    t("★キー重複・空の呼び名は出さない★",
      not renderable(mm(modes=[{"key": "normal", "label": "通常"}, {"key": "normal", "label": "通常"}]))
      and not renderable(mm(modes=[{"key": "normal", "label": ""}])))
    a = projection_id(m1)
    t("★★caution・excellent・天井だけ変えても指紋が変わる★★（古い記事のままなら出ない）",
      a != projection_id(mm(normal={"ceiling": 900, "caution": 310, "good": 400, "excellent": 600}))
      and a != projection_id(mm(normal={"ceiling": 900, "caution": 300, "good": 400, "excellent": 610}))
      and a != projection_id(mm(normal={"ceiling": 950, "caution": 300, "good": 400, "excellent": 600})))
    rows = mm(modes=[{"key": "suru", "label": "スルー"}],
              suru={"suru": [{"count": 0, "good": 500}, {"count": 1, "good": 300}, {"count": 2, "good": 100}]})
    rows2 = mm(modes=[{"key": "suru", "label": "スルー"}],
               suru={"suru": [{"count": 0, "good": 500}, {"count": 1, "good": 250}, {"count": 2, "good": 100}]})
    t("★★回数の途中の行だけ変えても指紋が変わる★★", projection_id(rows) != projection_id(rows2))
    f = detail_fields(m1)
    det = {"sections": [{"title": SECTION_TITLE, "body": f["body"]}],
           "summaryBoxes": f["summaryBoxes"], KEY: f["projection"]}
    m_sync = {**m1, KEY: f["projection"]}
    t("★記事と機種行の指紋・文がそろえば出す★", synced(m_sync, det))
    m_new = {**m_sync, "checker": {**m1["checker"],
                                   "normal": {"ceiling": 900, "caution": 300, "good": 450, "excellent": 600}}}
    t("★★線を書き換えたのに指紋が古いままなら止める★★（関所）",
      any("機種行の指紋" in x for x in sync_problems(m_new, det)) and not synced(m_new, det))
    t("★★機種行の指紋だけ消して記事に残すと、関所が止める★★"
      "（記事にだけ古い狙い目の文が出る・Codex review228 のP1）",
      any("記事にだけ" in x for x in sync_problems({**m_new, KEY: ""}, det))
      and not synced({**m_new, KEY: ""}, det))
    t("　（逆向き）記事の指紋だけ消して機種行に残しても止める",
      any("機種行にだけ" in x for x in sync_problems(m_sync, {**det, KEY: ""})))
    import copy as _cp
    det_u = _cp.deepcopy(det)
    from build_new_article import PENDING_TEXT as _PT
    t("★★記事を未確認に戻す★★（文・箱・指紋を外す＝両側とも指紋なし→関所は止めず、ページも出さない）",
      unsync_detail(det_u)
      and [s for s in det_u["sections"] if s["title"] == SECTION_TITLE][0]["body"] == [_PT]
      and not [b for b in det_u["summaryBoxes"] if b.get("role") == "target"]
      and KEY not in det_u
      and sync_problems({**m_new, KEY: ""}, det_u) == []
      and not synced({**m_new, KEY: ""}, det_u))
    det_h = {"sections": [{"title": SECTION_TITLE, "body": ["人が書いた文です。"]}], "summaryBoxes": []}
    t("　指紋の無い記事（人や2AIが書いた文）は触らない",
      not unsync_detail(det_h) and det_h["sections"][0]["body"] == ["人が書いた文です。"])
    bad = {**det, "sections": [{"title": SECTION_TITLE, "body": ["狙い目は、通常380G〜です。"]}]}
    t("★★記事の狙い目の文を手で変えたら止める★★", any("文が" in x for x in sync_problems(m_sync, bad)))
    bad2 = {**det, "summaryBoxes": [{"label": BOX_LABEL, "value": "通常380G〜", "role": "target"}]}
    t("★★狙い目早見表の箱を手で変えたら止める★★", any("箱が" in x for x in sync_problems(m_sync, bad2)))
    t("　そろっていない機種は何も入れない",
      detail_fields(mm(normal={"ceiling": 900})) == {"body": None, "summaryBoxes": [], "projection": ""})
    print(f"\n{ok[0]}/{ok[1]} 合格")
    return 0 if ok[0] == ok[1] else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    print(__doc__)

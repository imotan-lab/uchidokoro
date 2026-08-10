# -*- coding: utf-8 -*-
"""機種ごとの「この機種のページはここ」という控え。

★何のためにあるか★（2026-08-07・台帳#265）
  情報を1つ決めるには「大手2つが同じことを書いている」ことが要る。
  ところが名鑑（一覧ページ）から機種名で引く方法では、表記が違う機種を
  引き当てられない。実データで全121機種のうち38機種が2つに届かなかった。
    ・スマスロ防振り        ↔ 痛いのは嫌なので防御力に極振りしたいと思います
    ・SBニューキングハナハナV-30 ↔ ニューキングハナハナV-30
    ・LB不二子BT            ↔ 不二子BT
  これは「同じ機種か」という**意味の判断**なので、機械の照合では届かない。

★そこでAIに探させる。ただし守る線が1本ある★
  ┌────────────────────────────────────────────────┐
  │ AIが挙げたURLは、機械が実際に取ってきて          │
  │ 中身を確かめるまで採用しない                     │
  └────────────────────────────────────────────────┘
  過去にCodexが挙げたURLが404だった実例がある。この線さえ引いておけば、
  AIが間違ったURLを出しても無害＝存在しなければ落ち、別機種のページなら
  中身を読んだ時点で外れる。嘘のURLが記事に化ける経路が無い。

★探す先は登録済みの大手サイトだけ★（source-registry.json・default deny）
  「2つの出典が一致したら採用」の"2つ"を数えるには、その2つが本当に
  別系列かを知っている必要がある（P-WORLDと羽伏せは同じ系列で1票）。
  知らないサイトが出てくると独立性を数えられないので、票にできない。

★一度決めたら控えに残る★
  次からはAIを呼ばず機械が読むだけ。費用は機種あたり1回きり。

★使い方★
  # ①機械の検査（AIに渡す材料を出す。ここでは何も記録しない）
  python scripts/machine_sources.py --check --slug bofuri --url https://...
  # ②AIが「同じ機種だ」と判断したら記録する
  python scripts/machine_sources.py --record --slug bofuri --url https://... \
      --why "正式名称の略称。型式が一致" --by claude
  #   ★名前の形が合わないときは、判断した理由を書いて明示的に上書きする★
  #      --override-identity "略称なので題は一致しないが、型式番号が一致"
  # ③控えを見る / 手当てが要る機種を並べる
  python scripts/machine_sources.py --list [--slug bofuri]
  python scripts/machine_sources.py --missing
  python scripts/machine_sources.py --selftest
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys
import unicodedata
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import claim_identity as _ci          # noqa: E402
import model_code_lookup as _mc       # noqa: E402
import new_machine_watch as _w        # noqa: E402
import safe_json as _sj               # noqa: E402
import source_lineage as _sl          # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY = os.path.join(BASE, "assets", "data", "source-registry.json")
MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
# ★控えはリポジトリの外★（release_overrides.json と同じ置き場）
#   公開物に他サイトのURL一覧を混ぜないため。控えはDropboxへ保全する。
STORE = r"C:/Users/imao_/Documents/uchidokoro/machine_sources.json"

SCHEMA = "machine-sources/v1"


class SourceError(Exception):
    """控えに関する異常（★迷ったら記録しない★）。"""


# ---------------------------------------------------------------- 出典の台帳

def _publishers() -> dict:
    """ホスト名 → (発行者ID, 系列ID) の対応。★ACTIVE だけ★"""
    reg = _sj.read_json(REGISTRY, expect=dict)
    out = {}
    for pid, p in (reg.get("publishers") or {}).items():
        if p.get("status") != "ACTIVE":
            continue
        for h in p.get("canonical_hosts") or []:
            out[str(h).lower()] = (pid, p.get("content_lineage_id") or "")
    return out


def publisher_of(url: str, pubs: dict | None = None):
    """URLの発行者と系列を返す。★登録が無ければ (None, None)★"""
    host = urllib.parse.urlsplit(str(url or "")).hostname or ""
    return (pubs if pubs is not None else _publishers()).get(host.lower(),
                                                             (None, None))


# ---------------------------------------------------------------- 控えの読み書き

def _empty() -> dict:
    return {"schema_version": SCHEMA, "machines": {}}


def load() -> dict:
    if not os.path.exists(STORE):
        return _empty()
    got = _sj.read_json(STORE, expect=dict)
    if got.get("schema_version") != SCHEMA:
        raise SourceError(f"控えの形が違います: {got.get('schema_version')}")
    got.setdefault("machines", {})
    return got


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, STORE)


def urls_for(slug: str, data: dict | None = None) -> list:
    """控えに入っている、この機種の出典（機械が毎日読む側）。"""
    d = data if data is not None else load()
    return list((d.get("machines") or {}).get(slug) or [])


# ---------------------------------------------------------------- 機種の情報

def machine(slug: str) -> dict:
    ms = _sj.read_json(MACHINES, expect=(dict, list))
    ms = ms["machines"] if isinstance(ms, dict) else ms
    for m in ms:
        if m.get("slug") == slug:
            return m
    raise SourceError(f"機種が見つかりません: {slug}")


def _text_of(html: str) -> str:
    return " ".join(_w._visible_text(html).split())


def _title_key(title: str) -> str:
    """題を比べるための形。★全半角と空白の違いだけを吸収する★"""
    return " ".join(
        unicodedata.normalize("NFKC", str(title or "")).split()).casefold()


def _title_fp(title: str) -> str:
    """題の指紋。★保存した題そのものを比べない★

    控えに残す題は人が読む用に120字で切ってある。切った文字どうしを
    比べると、長い題（P-WORLDは200字近い）が毎回食い違う。
    比べるのは**全文の指紋**、見せるのは切った題、と役割を分ける。
    """
    return hashlib.sha256(_title_key(title).encode("utf-8")).hexdigest()


def _model_code_of(html: str):
    """このページに書かれた型式を1つ返す（★値だけ★）。

    ★2026-08-10に見つけた取り違え★
      `extract_model_code()` は必ず **(値, 理由) の組** を返す。
      組は中身が None でも真になるので、`if got["model_code"]` は常に真、
      `str(...)` は `"(None, 'MODEL_CODE_NOT_FOUND')"` という文字列になる。
      手がかりとして保存する直前でこの形になっていた（実データはまだ無し）。
    """
    got = _mc.extract_model_code(html)
    val = got[0] if isinstance(got, tuple) else got
    val = str(val).strip() if val else ""
    return val or None


# ---------------------------------------------------------------- 機械の検査

def check(slug: str, url: str, html: str | None = None,
          pubs: dict | None = None) -> dict:
    """★AIに渡す材料を作る（記録はしない）★

    機械にできることだけを見る：
      ①登録済みの発行者か ②取れるか ③別のホストへ飛ばされないか
      ④中身から題・見出し・型式・機種名の出方を抜き出す
    「同じ機種か」は**ここでは決めない**（それがAIの仕事）。
    """
    m = machine(slug)
    name = str(m.get("name") or "")
    out = {"slug": slug, "name": name, "url": url, "final_url": url,
           "ok": False,
           "problems": [], "publisher": None, "lineage": None,
           "title": "", "headings": [], "model_code": None,
           "name_core": _ci.normalize_core(name),
           "identity_verdict": None, "identity_why": "",
           "excerpt": "", "text_sha256": "", "text_len": 0,
           "already_recorded": False, "same_lineage_already": []}

    pid, lin = publisher_of(url, pubs)
    if not pid:
        out["problems"].append(
            "登録されていないサイトです（票に数えられないので使いません）")
        return out
    out["publisher"], out["lineage"] = pid, lin

    if html is None:
        try:
            html = _w._get(url)
        except Exception as e:              # noqa: BLE001
            out["problems"].append(f"取得できません（{e}）")
            return out
        final = _w.LAST_FINAL_URL.get("url") or url
        out["final_url"] = final
        bad = _w.redirect_problem(url, final)
        if bad:
            out["problems"].append(f"転送されました（{bad}）")
            return out
        if publisher_of(final, pubs)[0] != pid:
            out["problems"].append("別のサイトへ飛ばされました")
            return out
    why = _w.bad_page(html)
    if why:
        out["problems"].append(f"一覧・記事のページではありません（{why}）")
        return out

    text = _text_of(html)
    out["title"] = _w.page_title(html) or ""
    out["headings"] = [h[:80] for h in _w._visible_h1s(html)][:5]
    out["model_code"] = _model_code_of(html)
    out["text_len"] = len(text)
    out["text_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    out["excerpt"] = text[:400]
    ok, reason = _mc.page_is_machine(html, name)
    out["identity_verdict"], out["identity_why"] = bool(ok), str(reason or "")

    if not out["title"]:
        out["problems"].append("題がありません（機種のページか確かめられません）")
        return out
    if out["text_len"] < 200:
        out["problems"].append("本文がほとんどありません")
        return out

    data = load()
    # ★票が増えるかどうかは、保存してある系列名ではなく今のレジストリで数える★
    #   （2026-08-09・依頼125）保存値を信じると、レジストリで統合された後も
    #   古い名前のまま「別系列＝もう1票」に見えてしまう。
    try:
        my_key = _sl.vote_key(pid)
    except _sl.LineageError:
        my_key = None
    for rec in urls_for(slug, data):
        if rec.get("url") == url:
            out["already_recorded"] = True
            continue
        try:
            same = my_key is not None and _sl.vote_key(rec.get("publisher")) == my_key
        except _sl.LineageError:
            same = False
        if same:
            out["same_lineage_already"].append(rec.get("url"))
    out["ok"] = True
    return out


# ---------------------------------------------------------------- 記録

# ★判断してよいのはこの3者だけ★（2026-08-09・依頼125）
#   誰の判断かを後から追えないと、間違いを見つけたときに取り消す範囲を決められない。
JUDGES = ("claude", "codex", "運営者")
MIN_WHY = 8          # 「x」の一文字で通っていたので、文になる長さを求める


def record(slug: str, url: str, why: str, by: list,
           override_identity: str = "", checked: dict | None = None) -> dict:
    """★検査を通ったものだけを控えに残す★（fail-closed）"""
    if len(str(why or "").strip()) < MIN_WHY:
        raise SourceError(
            "--why（なぜ同じ機種と判断したか）は%d文字以上で書きます" % MIN_WHY)
    who = [x for x in (by or []) if x]
    if not who:
        raise SourceError("--by（誰が判断したか）は必ず書きます")
    bad = [x for x in who if x not in JUDGES]
    if bad:
        raise SourceError("判断者に使えない名前です（%s のみ）: %s"
                          % ("/".join(JUDGES), ",".join(bad)))
    got = checked if checked is not None else check(slug, url)
    # ★検査したものと記録するものが同じか確かめる★（2026-08-09・依頼125）
    #   ここが無いと、別の機種・別のURLを検査した結果を持ってきて
    #   「機械が取ってきた」という唯一の守る線を越えられる。
    if got.get("slug") != slug or got.get("url") != url:
        raise SourceError(
            "検査したものと記録するものが違います（検査=%s/%s 記録=%s/%s）"
            % (got.get("slug"), got.get("url"), slug, url))
    if not got["ok"]:
        raise SourceError("機械の検査を通りません: " + " / ".join(got["problems"]))
    if got["already_recorded"]:
        return {"state": "ALREADY", "url": url}
    # ★名前の形が合わないときは、判断した理由を明示的に書かせる★
    #   黙って通すと「近いから同じでいいや」が積み上がる。
    if not got["identity_verdict"] and not str(override_identity or "").strip():
        raise SourceError(
            "題が機種名と一致しません（" + got["identity_why"] + "）。"
            "同じ機種だと判断したなら --override-identity に理由を書きます")

    data = load()
    rec = {
        "url": url,
        "publisher": got["publisher"],
        "lineage": got["lineage"],
        "title": got["title"][:120],
        "decided_at": datetime.date.today().isoformat(),
        "decided_by": who,
        "why": str(why).strip()[:300],
        # ★どの中身を見て決めたか★（後から同じものを見たか確かめられる）
        "text_sha256": got["text_sha256"],
        "identity_verdict": got["identity_verdict"],
        # ★このページが「その機種のページ」だと言える手がかり★
        #   （2026-08-09・依頼125）本文は日々変わるが、これが変わったときは
        #   別の機種のページに差し替わった疑いがある＝人がもう一度見る。
        "identity_marks": _marks_of(got),
    }
    if override_identity:
        rec["override_identity"] = str(override_identity).strip()[:300]
    data["machines"].setdefault(slug, []).append(rec)
    _save(data)
    return {"state": "RECORDED", "url": url, "lineage": got["lineage"],
            "sources_now": len(data["machines"][slug])}


def forget(slug: str, url: str) -> dict:
    """判断が間違っていたときに控えから外す（★人の操作専用★）。"""
    data = load()
    rows = urls_for(slug, data)
    left = [r for r in rows if r.get("url") != url]
    if len(left) == len(rows):
        return {"state": "NOT_FOUND"}
    data["machines"][slug] = left
    if not left:
        data["machines"].pop(slug, None)
    _save(data)
    return {"state": "FORGOTTEN", "sources_now": len(left)}


# ------------------------------------------------------- ★使う瞬間の同定★
#
# ★なぜ要るか（2026-08-10・台帳#292＝Codex依頼125のP0-2）★
#   控えは「このURLはこの機種のページだ」と一度人が確かめた記録だが、
#   **使うときは保存したURLをそのまま読むだけ**だった。保存したsha256は
#   記録用にしか使っていない。登録のあとにそのページが別機種へ差し替わっても
#   誰も気づかない＝別機種の天井値をこの機種の記事に書く経路が開いていた。
#
# ★どう守るか★
#   読む前に毎回「同じページのままか」を機械が確かめ、変わっていたら**使わない**。
#   ★今のページを黙って正としない★＝直すのは人の仕事（隔離して台帳へ）。
#
# ★何を手がかりにするか★
#   題（の指紋）と型式。本文は日々変わるので使わない。
#     ・題が同じ                     → 同じページ
#     ・題は違うが型式が一致          → 同じページ（飾りの文言が変わっただけ）
#     ・型式が食い違う                → ★題が同じでも別の機種★
#     ・手がかりが何も無い            → 使わない（fail-closed）

CHECK_OK = "OK"              # 同じページのまま。使ってよい
CHECK_CHANGED = "CHANGED"    # 別のページに変わった疑い。★使わない・人が見る★
CHECK_UNUSABLE = "UNUSABLE"  # 今日は読めない（取得失敗・記事でない）


def _marks_of(got: dict) -> dict:
    """このページが「その機種のページ」だと言える手がかり。"""
    return {
        "final_url": got.get("final_url") or got.get("url"),
        "title": str(got.get("title") or "")[:120],     # 人が読む用
        # ★比べるのは指紋★（保存する題は切ってあるので文字どうしは比べられない）
        "title_fp": _title_fp(got.get("title")),
        "headings": [str(h)[:80] for h in (got.get("headings") or [])][:3],
        "model_code": got.get("model_code") or None,
    }


def _same_page(rec: dict, now: dict):
    """保存した手がかりと今のページを比べる。★迷ったら使わない★"""
    marks = rec.get("identity_marks") or {}
    old_code = str(marks.get("model_code") or "").strip()
    new_code = str(now.get("model_code") or "").strip()
    # ★型式が食い違ったら、題が同じでも別の機種★
    #   （題を使い回して中身だけ差し替える形は、題では見抜けない）
    if old_code and new_code and _title_key(old_code) != _title_key(new_code):
        return CHECK_CHANGED, "型式が変わりました（%s → %s）" % (old_code, new_code)
    if marks.get("title_fp"):
        if marks["title_fp"] == now.get("title_fp"):
            return CHECK_OK, ""
        if old_code and new_code:
            return CHECK_OK, "題は変わりましたが型式が一致します"
        return CHECK_CHANGED, "題が変わりました（控え: %s）" % (
            str(marks.get("title") or "")[:60])
    # ★手がかりを保存する前に入れた控え★（2026-08-07の35件）
    #   題だけが残っている。120字で切ってあるので**前方一致**で比べる。
    #   ここを通れるのは一度だけ＝`--recheck --apply` で手がかりを保存し直す。
    saved = str(rec.get("title") or "")
    if not saved:
        return CHECK_CHANGED, "確かめる手がかりがありません（取り直しが要ります）"
    a, b = _title_key(saved), _title_key(now.get("title"))
    if a == b or (len(saved) >= 120 and a and b.startswith(a)):
        return CHECK_OK, "旧い控え（保存時の題だけで確かめました）"
    return CHECK_CHANGED, "題が変わりました（控え: %s）" % saved[:60]


def recheck(slug: str, rec: dict, html: str | None = None,
            pubs: dict | None = None) -> dict:
    """★控えのURLを読む直前に、同じページのままか確かめる★（書き込まない）"""
    url = rec.get("url")
    out = {"slug": slug, "url": url, "state": CHECK_UNUSABLE, "why": "",
           "final_url": url, "marks_now": None, "text_sha256": "", "html": None}
    if not url:
        out["why"] = "控えにURLがありません"
        return out
    pid, _lin = publisher_of(url, pubs)
    if not pid:
        out["why"] = "発行者が登録されていません（票に数えられません）"
        return out
    was = rec.get("publisher")
    if was and was != pid:
        # ★ホストの持ち主が変わった＝もう同じ発行者ではない★
        out["state"] = CHECK_CHANGED
        out["why"] = "発行者が変わりました（%s → %s）" % (was, pid)
        return out

    if html is None:
        try:
            html = _w._get(url)
        except Exception as e:              # noqa: BLE001
            out["why"] = "取得できません（%s）" % str(e)[:80]
            return out
        final = _w.LAST_FINAL_URL.get("url") or ""
        out["final_url"] = final or url
        if not final:
            out["why"] = "最終URLを確認できませんでした"
            return out
        if publisher_of(final, pubs)[0] != pid:
            out["state"] = CHECK_CHANGED
            out["why"] = "別のサイトへ飛ばされました（%s）" % final[:90]
            return out
        bad = _w.redirect_problem(url, final)
        if bad:
            # ★ページが移った＝このURLはもうあのページではない★
            #   取得できない（一時的）とは違うので、人がもう一度見る側に置く。
            out["state"] = CHECK_CHANGED
            out["why"] = "転送されました（%s）" % bad
            return out
    why = _w.bad_page(html)
    if why:
        out["why"] = "記事のページではありません（%s）" % why
        return out
    title = _w.page_title(html) or ""
    if not title:
        out["why"] = "題がありません（同じページか確かめられません）"
        return out
    text = _text_of(html)
    if len(text) < 200:
        out["why"] = "本文がほとんどありません"
        return out

    out["text_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    out["marks_now"] = _marks_of({
        "final_url": out["final_url"], "title": title,
        "headings": [h[:80] for h in _w._visible_h1s(html)][:5],
        "model_code": _model_code_of(html)})
    out["state"], out["why"] = _same_page(rec, out["marks_now"])
    if out["state"] == CHECK_OK:
        out["html"] = html          # ★取り直させない★（同じ相手を二度叩かない）
    return out


def quarantined(rec: dict) -> bool:
    """前回の確認で「別のページに変わった疑い」が出たまま直っていないか。"""
    return str((rec.get("last_check") or {}).get("state") or "") == CHECK_CHANGED


def issue_args(slug: str, rec: dict, got: dict) -> list:
    """隔離を台帳へ登録するときの引数（★組み立てだけ・実行はしない★）。"""
    title = ("控えの出典が別のページに変わった疑い: %s（%s）"
             % (slug, rec.get("publisher") or "?"))
    detail = "\n".join([
        "URL: " + str(rec.get("url")),
        "理由: " + str(got.get("why")),
        "控えの題: " + str(rec.get("title") or "（なし）"),
        "いまの題: " + str((got.get("marks_now") or {}).get("title") or ""),
        "控えの型式: " + str((rec.get("identity_marks") or {})
                             .get("model_code") or "（なし）"),
        "いまの型式: " + str((got.get("marks_now") or {})
                             .get("model_code") or "（なし）"),
        "★この出典は使っていません（隔離中）★",
        "対応: 人が中身を見て、まだ同じ機種なら",
        "  python scripts/machine_sources.py --recheck --slug " + slug
        + " --apply",
        "  で手がかりを取り直す。別機種になっていたら --forget で外す。",
    ])
    return ["add", "--source", "machine-sources", "--slug", slug,
            "--kind", "external_value", "--severity", "CRITICAL",
            "--reason-code", "SOURCE_PAGE_CHANGED",
            "--title", title, "--detail", detail]


def report_changed(slug: str, rec: dict, got: dict) -> None:
    """隔離を人に届ける。★黙って落とさない★（失敗しても呼び出し元は止めない）

    ここが無いと、無人タスクは出典を1つ静かに失うだけで、
    誰も「別機種に化けたページ」に気づけない。
    """
    if got.get("state") != CHECK_CHANGED:
        return
    try:
        import subprocess
        r = subprocess.run(
            [sys.executable, os.path.join(BASE, "scripts", "open_issues.py")]
            + issue_args(slug, rec, got),
            # ★シェルを通らない引数配列なので直接指定でよい★（台帳#295）
            env=dict(os.environ, UCHIDOKORO_ARGV_CALL="1"),
            capture_output=True, timeout=60, check=False)
        if r.returncode != 0:
            # ★届かなかったことは必ず残す★（握りつぶすと隔離が誰にも見えない）
            print("★台帳へ登録できませんでした（%s / %s）★: %s"
                  % (slug, rec.get("url"),
                     (r.stderr or b"").decode("utf-8", "replace")[:200]),
                  file=sys.stderr)
    except Exception as e:                  # noqa: BLE001
        print("★台帳へ登録できませんでした（%s / %s）★: %s"
              % (slug, rec.get("url"), str(e)[:200]), file=sys.stderr)


def remember_check(slug: str, url: str, got: dict) -> dict:
    """確認の結果を控えに書き戻す（★人が動かすときだけ★）。

    ★手がかりを上書きしてよいのは「同じページだ」と言えたときだけ★
      変わっていたのに今のページで上書きすると、
      **差し替わった別機種のページを正として覚え直す**ことになる。
    """
    data = load()
    for rec in (data.get("machines") or {}).get(slug) or []:
        if rec.get("url") != url:
            continue
        rec["last_check"] = {
            "at": datetime.date.today().isoformat(),
            "state": got.get("state"),
            "why": str(got.get("why") or "")[:200],
        }
        if got.get("state") == CHECK_OK and got.get("marks_now"):
            rec["identity_marks"] = got["marks_now"]
            if got.get("text_sha256"):
                rec["text_sha256_last"] = got["text_sha256"]
        _save(data)
        return {"state": "SAVED", "marks": got.get("state") == CHECK_OK}
    return {"state": "NOT_FOUND"}


def recheck_all(slug: str = "", apply: bool = False) -> list:
    """控えを順に確かめる。★--apply で手がかりを保存し直す（取り直し）★"""
    data = load()
    rows = []
    for s, recs in sorted((data.get("machines") or {}).items()):
        if slug and s != slug:
            continue
        for rec in recs:
            got = recheck(s, rec)
            if apply:
                remember_check(s, rec.get("url"), got)
                report_changed(s, rec, got)
            rows.append({"slug": s, "url": rec.get("url"),
                         "publisher": rec.get("publisher"),
                         "state": got["state"], "why": got["why"],
                         "had_marks": bool(rec.get("identity_marks")),
                         "title_now": (got.get("marks_now") or {}).get("title"),
                         "model_now": (got.get("marks_now") or {}).get(
                             "model_code")})
    return rows


# ---------------------------------------------------------------- 手当てが要る機種

def missing(limit: int = 0) -> list:
    """2つの系列に届かない機種を並べる（★AIに探させる対象★）。

    ★票の単位は source_lineage が唯一の正本★（2026-08-09・依頼125）
      以前は名鑑側だけ `dir:<名鑑ID>` という仮の名前で数えていたため、
      **同じ発行者の名鑑と控えが2票に化けていた**（実データで再現済み）。
      名鑑も控えも「発行者ID → 票のかたまり」で数え直す。
      引けないものは票にしない（★仮の名前を作らない★）。
    """
    import directory_index as _di

    cats = _sj.read_json(_di.CATALOGS, expect=dict)["directories"]
    active = {k: c for k, c in cats.items() if c.get("status") == "ACTIVE"}
    # ★名鑑の発行者は起動時に必ず引けること★（引けなければ設定の誤り＝止める）
    dir_key = {}
    for dir_id, c in active.items():
        pid = c.get("publisher_id")
        if not pid:
            raise SourceError(
                "名鑑に publisher_id がありません: " + dir_id
                + "（票の単位を決められないので数えません）")
        dir_key[dir_id] = _sl.vote_key(pid)

    scans = {k: _di.scan_directory(k, c) for k, c in active.items()}
    ms = _sj.read_json(MACHINES, expect=(dict, list))
    ms = ms["machines"] if isinstance(ms, dict) else ms
    data = load()
    rows = []
    for m in ms:
        core = _ci.normalize_core(m.get("name") or "")
        have, seen, unknown = [], set(), []
        for dir_id, r in scans.items():
            # ★候補が2件以上ある名鑑は票にしない★（2026-08-09・実データで判明）
            #   実際に原文を読む側（directory_index.find）は候補が1件のときしか
            #   URLを返さない。ここだけ「1件でも当たれば手当て済み」と数えると、
            #   **読めない機種を「もう出典がある」と誤って外して**しまう。
            #   実例: Lハナビ → 「スマスロ ハナビ」と旧「ハナビ」の2件、
            #        Lパチスロ炎炎ノ消防隊 → L版と旧パチスロ版の2件。
            if len(_di.lookup_hits(r["index"], core)) != 1:
                continue
            if dir_key[dir_id] not in seen:
                seen.add(dir_key[dir_id])
                have.append(dir_id)
        held = []
        for rec in urls_for(m["slug"], data):
            # ★隔離中の控えは票にしない★（2026-08-10・台帳#292）
            #   別のページに変わった疑いが出たものは、読む側（collect_evidence）が
            #   使わない。ここだけ数え続けると「出典は2つある」ことにされ、
            #   **手当ての一覧から外れて誰も探しに行かなくなる**。
            if quarantined(rec):
                held.append(rec.get("publisher"))
                continue
            # ★保存済みの系列名は使わない★＝今のレジストリから引き直す
            try:
                key = _sl.vote_key(rec.get("publisher"))
            except _sl.LineageError:
                unknown.append(rec.get("publisher"))
                continue
            if key not in seen:
                seen.add(key)
                have.append(rec.get("publisher"))
        if len(seen) < 2:
            rows.append({"slug": m["slug"], "name": m.get("name"),
                         "have": have, "votes": len(seen),
                         "unknown": unknown, "quarantined": held})
    return rows[:limit] if limit else rows


# ---------------------------------------------------------------- 表示

def _print_check(got: dict) -> None:
    print("■ %s（%s）" % (got["name"], got["slug"]))
    print("  URL     : " + got["url"])
    print("  発行者  : %s / 系列 %s" % (got["publisher"], got["lineage"]))
    if got["problems"]:
        for p in got["problems"]:
            print("  ★使えません★ " + p)
        return
    print("  題      : " + got["title"][:110])
    for h in got["headings"]:
        print("  見出し  : " + h)
    print("  型式    : " + (got["model_code"] or "（見つかりません）"))
    print("  本文    : %d文字 / sha256 %s" % (got["text_len"],
                                              got["text_sha256"][:12]))
    print("  名前の形: %s（%s）"
          % ("一致" if got["identity_verdict"] else "★一致しません★",
             got["identity_why"]))
    if got["already_recorded"]:
        print("  ※すでに控えにあります")
    if got["same_lineage_already"]:
        print("  ※同じ系列の出典がすでにあります（票は増えません）: "
              + ", ".join(str(u) for u in got["same_lineage_already"]))
    print("  抜粋    : " + got["excerpt"][:300])


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    import tempfile

    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    global STORE
    keep = STORE
    tmpdir = tempfile.mkdtemp()
    STORE = os.path.join(tmpdir, "machine_sources.json")
    pubs = {"chonborista.com": ("chonborista", "lin-chonborista"),
            "nana-press.com": ("nana-press", "lin-nana-press")}
    ms = _sj.read_json(MACHINES, expect=(dict, list))
    ms = ms["machines"] if isinstance(ms, dict) else ms
    slug = ms[0]["slug"]
    name = ms[0]["name"]
    body = ("<title>" + name + " スロット 新台 天井 | ちょんぼりすた</title>"
            "<body><h1>" + name + "</h1><p>" + ("天井は999Gです。" * 40)
            + "</p></body>")

    try:
        t("★★登録されていないサイトは使わない★★（票に数えられない）",
          check(slug, "https://example.com/x", html=body, pubs=pubs)["problems"])

        got = check(slug, "https://chonborista.com/slot/a/1",
                    html=body, pubs=pubs)
        t("　登録済みなら発行者と系列が付く",
          got["publisher"] == "chonborista"
          and got["lineage"] == "lin-chonborista")
        t("　題・見出し・本文の指紋を材料として出す",
          got["title"] and got["headings"] and len(got["text_sha256"]) == 64)
        t("　本文が短すぎるページは使わない",
          not check(slug, "https://chonborista.com/slot/a/2",
                    html="<title>x</title><body>短い</body>",
                    pubs=pubs)["ok"])

        try:
            record(slug, got["url"], why="", by=["claude"], checked=got)
            ok = False
        except SourceError:
            ok = True
        t("★★なぜ同じ機種かを書かずには記録できない★★", ok)

        try:
            record(slug, got["url"], why="題が機種名と一致します", by=[], checked=got)
            ok = False
        except SourceError:
            ok = True
        t("　誰が判断したかを書かずには記録できない", ok)

        bad = dict(got, ok=False, problems=["取得できません"])
        try:
            record(slug, got["url"], why="題が機種名と一致します", by=["claude"],
                   checked=bad)
            ok = False
        except SourceError:
            ok = True
        t("★★機械の検査を通らないものは記録しない★★（AIの言い分だけでは残さない）",
          ok)

        ng = dict(got, identity_verdict=False, identity_why="TITLE_MISMATCH")
        try:
            record(slug, got["url"], why="題が機種名と一致します", by=["claude"],
                   checked=ng)
            ok = False
        except SourceError:
            ok = True
        t("★★題が合わないときは理由を明示しないと記録できない★★", ok)

        r = record(slug, got["url"], why="略称なので題は合いませんが型式が一致します",
                   by=["claude"],
                   override_identity="略称だが型式が一致", checked=ng)
        t("　理由を書けば記録できる", r["state"] == "RECORDED")
        t("　控えから読み出せる（機械が毎日使う側）",
          [x["url"] for x in urls_for(slug)] == [got["url"]])
        t("　同じURLは二重に入らない",
          record(slug, got["url"], why="題が機種名と一致します", by=["claude"],
                 checked=dict(got, already_recorded=True))["state"] == "ALREADY")
        t("　控えの形が読み出せる", load()["schema_version"] == SCHEMA)

        got2 = check(slug, "https://chonborista.com/slot/a/9",
                     html=body, pubs=pubs)
        t("★★同じ系列がすでにあるときは知らせる★★（票は増えない）",
          got2["same_lineage_already"] == [got["url"]])

        t("　間違いは控えから外せる",
          forget(slug, got["url"])["state"] == "FORGOTTEN"
          and urls_for(slug) == [])
        t("　無いものを外そうとしても壊れない",
          forget(slug, got["url"])["state"] == "NOT_FOUND")

        # ------------------------------------------- ★使う瞬間の同定★（#292）
        title_now = _w.page_title(body)
        code_body = body.replace("<body>", "<body><p>型式名：LテストAB1</p>")
        code2_body = body.replace("<body>", "<body><p>型式名：LベツキシュCD2</p>")
        other = ("<title>ぜんぜん別の機種 スロット 天井 | ちょんぼりすた</title>"
                 "<body><h1>ぜんぜん別の機種</h1><p>"
                 + ("天井は777Gです。" * 40) + "</p></body>")

        def R(rec, html):
            return recheck(slug, rec, html=html, pubs=pubs)

        base = {"url": "https://chonborista.com/slot/a/1",
                "publisher": "chonborista"}
        marked = dict(base, identity_marks={
            "title_fp": _title_fp(title_now), "title": title_now[:120],
            "model_code": None})

        t("★★題が同じなら、そのまま使ってよい★★", R(marked, body)["state"] == CHECK_OK)
        t("　通ったときは取ってきた本文をそのまま渡す（二度取りに行かない）",
          R(marked, body)["html"] == body)
        g = R(marked, other)
        t("★★題が変わったら使わない★★（別機種に差し替わった疑い＝人が見る）",
          g["state"] == CHECK_CHANGED and "題が変わりました" in g["why"])

        m_code = dict(base, identity_marks={
            "title_fp": _title_fp(title_now), "title": title_now[:120],
            "model_code": "LテストAB1"})
        t("★★題の飾りが変わっても、型式が一致すれば使える★★",
          R(m_code, other.replace("<body>",
                                  "<body><p>型式名：LテストAB1</p>")
            )["state"] == CHECK_OK)
        g = R(m_code, code2_body)
        t("★★型式が食い違えば、題が同じでも使わない★★"
          "（題を使い回して中身だけ差し替える形は題では見抜けない）",
          g["state"] == CHECK_CHANGED and "型式が変わりました" in g["why"])

        # ★2026-08-10に見つけた取り違えを固定する★
        t("★★型式の手がかりは値だけを持つ★★"
          "（組のまま文字にすると \"(None, 'MODEL_CODE_NOT_FOUND')\" が入る）",
          _marks_of({"title": "x", "model_code": _model_code_of(body)}
                    )["model_code"] is None
          and _model_code_of(code_body) == "LテストAB1")

        # ★手がかりを保存する前に入れた控え（2026-08-07の35件）★
        old = dict(base, title=title_now[:120])
        t("　旧い控えは、保存時の題だけで確かめる（取り直すまでの橋渡し）",
          R(old, body)["state"] == CHECK_OK)
        t("　旧い控えでも、題が変わっていれば使わない",
          R(old, other)["state"] == CHECK_CHANGED)
        t("★★手がかりが何も無い控えは使わない★★（fail-closed）",
          R(dict(base), body)["state"] == CHECK_CHANGED)
        long_title = ("<title>" + "あ" * 200 + "</title><body><h1>x</h1><p>"
                      + ("天井は999Gです。" * 40) + "</p></body>")
        t("　120字で切って保存した長い題は、前方一致で確かめる"
          "（P-WORLDの題は200字近い）",
          R(dict(base, title=_w.page_title(long_title)[:120]),
            long_title)["state"] == CHECK_OK)

        t("★★発行者が変わったら使わない★★",
          R(dict(marked, publisher="nana-press"),
            body)["state"] == CHECK_CHANGED)
        t("　登録されていないサイトになっていたら使わない",
          recheck(slug, {"url": "https://example.com/x"}, html=body,
                  pubs=pubs)["state"] == CHECK_UNUSABLE)
        t("　記事ではない画面は「今日は読めない」（隔離とは分ける）",
          R(marked, "<title>x</title><body>ただいまメンテナンス中です</body>"
            )["state"] == CHECK_UNUSABLE)
        t("　本文がほとんど無いページも読めない扱い",
          R(marked, "<title>" + title_now + "</title><body>短い</body>"
            )["state"] == CHECK_UNUSABLE)

        # ★隔離を台帳へ届ける道を、本物の台帳スクリプトで通す★
        #   握りつぶす作りなので、引数が1つ違うだけで**誰にも届かなくなる**。
        led = os.path.join(tmpdir, "issues.json")
        import subprocess
        rr = subprocess.run(
            [sys.executable, os.path.join(BASE, "scripts", "open_issues.py"),
             "--file", led]
            + issue_args(slug, marked, R(marked, other)),
            env=dict(os.environ, UCHIDOKORO_ARGV_CALL="1"),
            capture_output=True, timeout=60, check=False)
        t("★★隔離は台帳が実際に受け取れる形で送る★★（届かなければ誰も気づけない）",
          rr.returncode == 0
          and "別のページに変わった疑い" in _sj.read_json(
              led, expect=dict)["issues"][0]["title"])

        t("　確認の結果は控えに書き戻せる（手がかりの取り直し）",
          (_save({"schema_version": SCHEMA, "machines": {slug: [dict(old)]}}),
           remember_check(slug, base["url"], R(old, body))["state"] == "SAVED"
           and urls_for(slug)[0]["identity_marks"]["title_fp"]
           == _title_fp(title_now))[1])
        t("★★変わっていたら手がかりを上書きしない★★"
          "（差し替わった別機種のページを正として覚え直さない）",
          (_save({"schema_version": SCHEMA, "machines": {slug: [dict(old)]}}),
           remember_check(slug, base["url"], R(old, other)),
           "identity_marks" not in urls_for(slug)[0]
           and urls_for(slug)[0]["last_check"]["state"] == CHECK_CHANGED)[2])

        # ★2026-08-09・依頼125で見つかった数え間違いを固定する★
        #   名鑑と控えに同じ発行者が出たとき、以前は2票と数えていた。
        import directory_index as _di
        real_scan = _di.scan_directory
        core = _ci.normalize_core(name)

        def fake_scan(dir_id, conf, _core=core):
            idx = ({_core: [("https://chonborista.com/slot/a/1", "題")]}
                   if dir_id == "chonborista" else {})
            return {"index": idx, "surfaces_ok": 1, "surfaces_total": 1,
                    "problems": []}

        def ambiguous_scan(dir_id, conf, _core=core):
            """★候補が2件ある名鑑★（Lハナビ ↔ スマスロハナビ/旧ハナビ）"""
            idx = ({_core: [("https://chonborista.com/slot/a/1", "新しい方"),
                            ("https://chonborista.com/slot/a/2", "古い方")]}
                   if dir_id == "chonborista" else {})
            return {"index": idx, "surfaces_ok": 1, "surfaces_total": 1,
                    "problems": []}
        _di.scan_directory = fake_scan
        try:
            _save({"schema_version": SCHEMA, "machines": {slug: [
                {"url": "https://chonborista.com/slot/a/9",
                 "publisher": "chonborista", "lineage": "lin-chonborista"}]}})
            still = [r for r in missing() if r["slug"] == slug]
            t("★★名鑑と控えが同じ発行者なら1票★★（1社を2票と数えない）",
              still and still[0]["votes"] == 1)

            _save({"schema_version": SCHEMA, "machines": {slug: [
                {"url": "https://nana-press.com/kaiseki/machine/1",
                 "publisher": "nana-press", "lineage": "lin-nana-press"}]}})
            t("　別の発行者なら2票になって一覧から外れる",
              not [r for r in missing() if r["slug"] == slug])

            _save({"schema_version": SCHEMA, "machines": {slug: [
                {"url": "https://example.com/x", "publisher": "shiranai-site",
                 "lineage": "lin-shiranai"}]}})
            left = [r for r in missing() if r["slug"] == slug]
            t("★★登録されていない発行者は票にしない★★（仮の名前を作らない）",
              left and left[0]["votes"] == 1
              and left[0]["unknown"] == ["shiranai-site"])

            _di.scan_directory = ambiguous_scan
            _save({"schema_version": SCHEMA, "machines": {slug: [
                {"url": "https://nana-press.com/kaiseki/machine/1",
                 "publisher": "nana-press", "lineage": "lin-nana-press"}]}})
            amb = [r for r in missing() if r["slug"] == slug]
            t("★★候補が割れている名鑑は票にしない★★（原文を読む側は1件のときしか使わない）",
              amb and amb[0]["votes"] == 1 and amb[0]["have"] == ["nana-press"])

            _di.scan_directory = fake_scan
            _save({"schema_version": SCHEMA, "machines": {slug: [
                {"url": "https://nana-press.com/kaiseki/machine/1",
                 "publisher": "nana-press", "lineage": "lin-nana-press",
                 "last_check": {"state": CHECK_CHANGED, "at": "2026-08-10",
                                "why": "題が変わりました"}}]}})
            q = [r for r in missing() if r["slug"] == slug]
            t("★★隔離中の控えは票にしない★★（2026-08-10・台帳#292）"
              "＝読む側が使わないものを数え続けると、手当ての一覧から外れる",
              q and q[0]["votes"] == 1 and q[0]["quarantined"] == ["nana-press"])
        finally:
            _di.scan_directory = real_scan
    finally:
        STORE = keep

    bad = sum(1 for _, ok in results if not ok)
    print()
    print("%d/%d 合格" % (len(results) - bad, len(results)))
    return 1 if bad else 0


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser(description="機種ごとの出典URLの控え")
    ap.add_argument("--check", action="store_true",
                    help="機械の検査だけ（AIに渡す材料を出す）")
    ap.add_argument("--record", action="store_true", help="控えに残す")
    ap.add_argument("--forget", action="store_true", help="控えから外す")
    ap.add_argument("--list", action="store_true", help="控えを見る")
    ap.add_argument("--missing", action="store_true",
                    help="2つの系列に届かない機種を並べる")
    ap.add_argument("--recheck", action="store_true",
                    help="控えのページが同じままか確かめる（既定は見るだけ）")
    ap.add_argument("--apply", action="store_true",
                    help="--recheck の結果を控えに保存する（★人の操作専用★）")
    ap.add_argument("--slug")
    ap.add_argument("--url")
    ap.add_argument("--why", default="")
    ap.add_argument("--by", default="",
                    help="判断した人（claude / codex / 運営者。カンマ区切り）")
    ap.add_argument("--override-identity", default="",
                    help="題が機種名と一致しないときの理由")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()
    try:
        if a.check:
            if not (a.slug and a.url):
                print("--slug と --url が要ります")
                return 2
            _print_check(check(a.slug, a.url))
            return 0
        if a.record:
            if not (a.slug and a.url):
                print("--slug と --url が要ります")
                return 2
            r = record(a.slug, a.url, a.why,
                       [x.strip() for x in a.by.split(",") if x.strip()],
                       a.override_identity)
            print(json.dumps(r, ensure_ascii=False))
            return 0
        if a.forget:
            print(json.dumps(forget(a.slug, a.url), ensure_ascii=False))
            return 0
        if a.recheck:
            rows = recheck_all(a.slug or "", apply=a.apply)
            mark = {CHECK_OK: "○", CHECK_CHANGED: "★変わっています★",
                    CHECK_UNUSABLE: "△読めません"}
            for r in rows:
                print("%-2s %-22s %-12s %s"
                      % (mark.get(r["state"], r["state"]), r["slug"],
                         r["publisher"], r["why"] or ""))
                print("     " + str(r["url"]))
            n = {s: sum(1 for r in rows if r["state"] == s)
                 for s in (CHECK_OK, CHECK_CHANGED, CHECK_UNUSABLE)}
            print()
            print("控え %d件： 同じ %d ／ ★変わった %d★ ／ 読めない %d"
                  % (len(rows), n[CHECK_OK], n[CHECK_CHANGED],
                     n[CHECK_UNUSABLE]))
            if a.apply:
                print("★保存しました★（同じページのものだけ手がかりを取り直し）")
            else:
                print("※見るだけです。保存するには --apply を付けます")
            return 1 if n[CHECK_CHANGED] else 0
        if a.missing:
            rows = missing()
            print("★2つの系列に届かない機種: %d件★" % len(rows))
            for r in rows:
                print("  %-22s %s  （いま: %s）%s"
                      % (r["slug"], r["name"], "/".join(
                          str(x) for x in r["have"]) or "なし",
                         ("  ★隔離中: %s★" % "/".join(
                             str(x) for x in r["quarantined"]))
                         if r.get("quarantined") else ""))
            return 0
        if a.list:
            data = load()
            for slug, rows in sorted((data.get("machines") or {}).items()):
                if a.slug and slug != a.slug:
                    continue
                print("■ " + slug)
                for r in rows:
                    print("   [%s] %s" % (r.get("lineage"), r.get("url")))
                    print("      %s ／ %s（%s）"
                          % (r.get("why"), ",".join(r.get("decided_by") or []),
                             r.get("decided_at")))
            return 0
    except SourceError as e:
        print("★" + str(e) + "★")
        return 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

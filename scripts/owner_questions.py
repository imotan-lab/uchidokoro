# -*- coding: utf-8 -*-
"""★運営者への質問を、届くまで送り直すための控え★（2026-10-01・台帳の廃止で新設）

★これだけを持つ★＝運営者にしか決められないこと（方針・お金・規約・人が読むしかない外の事実）と、
2AIが3回で割れた記事の値。★技術的な直しや「あとでやる」は入れない★（それが台帳だった）。

★届くまで残る★＝タスクがメールを送れたときだけ `sent` を付ける。送れなければ印が残り、
毎朝の番人（8:03）が `pending` を見てメールに載せ直す。

使い方:
  python scripts/owner_questions.py add --title-file <題> --detail-file <本文>
      本文＝いまの記事の文／Claude＝値・根拠URL・その文／Codex＝同／3回で足した材料／返事の仕方
  python scripts/owner_questions.py pending        # まだ届いていない質問（無ければ「ありません」）
  python scripts/owner_questions.py sent --id 3    # メールを送れたときだけ
  python scripts/owner_questions.py answered --id 3 --note-file <運営者の答え>   # 対話セッションが記録する
  python scripts/owner_questions.py --selftest
置き場: （書類フォルダ）/uchidokoro/owner_questions.json
"""
import argparse
import datetime
import json
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import local_paths as _lp            # noqa: E402
import text_args as _ta              # noqa: E402

STORE = Path(_lp.doc("owner_questions.json"))


def _load(path: Path) -> dict:
    if path.is_file():
        d = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(d, dict) or not isinstance(d.get("items"), list):
            raise SystemExit(f"★控えの形が違います: {path}★（壊れたまま上書きしない）")
        return d
    return {"next_id": 1, "items": []}


def _save(path: Path, d: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def add(path: Path, title: str, detail: str) -> int:
    title, detail = str(title or "").strip(), str(detail or "").strip()
    if not title or not detail:
        raise SystemExit("★題と本文の両方が要ります★")
    d = _load(path)
    for it in d["items"]:
        # ★同じ題で答えが出ていないものは重ねない★（本文だけ新しくする＝届け直す）
        # ★取り下げた質問は使い回さない★（Codexの指摘＝取り下げの印が残ったまま本文だけ変わり、二度と届かなかった）
        # ★機械が「片づいた」と閉じた質問（resolved_at）も使い回さない★（2026-11-12）
        if it.get("title") == title and not _closed(it):
            # ★★本文が同じなら届け直さない★★（2026-11-12・運営者に同じ質問が 11/06・11/09・11/12 と3回届いた＝
            #   育成が詰まるたびに同じ題・同じ本文で出し直し、そのたび送った印を消していた）。
            #   ★答えの無いまま7日たった質問は unanswered が🟡で思い出させる★ので、同じ文を送り直す必要は無い。
            if it.get("detail") == detail and it.get("sent_at"):
                print(f"#{it['id']} は同じ本文で送り済みです（届け直しません）")
                return it["id"]
            it["detail"] = detail
            it.pop("sent_at", None)
            _save(path, d)
            print(f"#{it['id']} を新しい本文で届け直します")
            return it["id"]
    n = int(d.get("next_id") or 1)
    d["items"].append({"id": n, "title": title, "detail": detail, "created_at": _now()})
    d["next_id"] = n + 1
    _save(path, d)
    print(f"#{n} 運営者への質問を控えました（送れたら sent --id {n}）")
    return n


def _closed(it: dict) -> bool:
    """答えが出た・2AIが取り下げた・機械が片づいたと閉じた質問"""
    return bool(it.get("answered_at") or it.get("retracted_at") or it.get("resolved_at"))


def pending(path: Path) -> list:
    return [it for it in _load(path)["items"] if not it.get("sent_at") and not _closed(it)]


UNANSWERED_DAYS = 7


def unanswered(path: Path, now: str = None, days: int = UNANSWERED_DAYS) -> list:
    """★送ったのに答えが来ないまま days 日たった質問★（2026-11-10・洗い出し4回目）

    なぜ＝直す前は「送った」印が付くと二度と見ず、答えが無いまま残っても誰も知らなかった。
    見張り番が🟡で思い出させる（運営者がメールを見落としたときのため）。取り下げた質問は出さない。
    """
    import datetime as _dt
    # ★日時として比べる★（Codexの指摘＝区切りの空白と T を文字のまま比べ、最大1日早く出ていた）
    import re as _re

    def _dt_of(x):
        # ★決まった書式だけ読む★（末尾に余計な文字・時差つきなどは「読めない」＝答え待ちとして出す）
        t = str(x)
        if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}", t):
            raise ValueError(t)
        return _dt.datetime.fromisoformat(t.replace(" ", "T"))
    now_d = _dt_of(now or _now())
    out = []
    for it in _load(path)["items"]:
        if not it.get("sent_at") or _closed(it):
            continue
        try:
            if now_d - _dt_of(it["sent_at"]) >= _dt.timedelta(days=days):
                out.append(it)
        except (ValueError, TypeError):
            out.append(it)                 # ★読めない日時は「答え待ち」として出す（黙って外さない）★
    return out


def retract(path: Path, qid: int, why: str, by) -> int:
    """★2AIで片づいて、運営者に聞く必要が無くなった質問を取り下げる★（判断者の組・理由15字以上）

    例＝3回割れた値を、その後2AIが「落としてよい」と決めて片づけた（grow_loss_accepted）。
    ★答えたことにはしない★（answered と分ける）＝運営者の判断が要らなくなったという記録。
    """
    import confirmed_values as _cv
    why = str(why or "").strip()
    if len(why) < 15:
        print("★取り下げる理由を15字以上書いてください★")
        return 1
    if _cv.judges_pair([x.strip() for x in (by or []) if str(x).strip()]) is None:
        print("★取り下げには2AIの両方の判断が要ります（claude,codex または代役の2人）★")
        return 1
    d = _load(path)
    hit = next((it for it in d["items"] if it.get("id") == qid), None)
    if hit is None or hit.get("answered_at"):
        print(f"★#{qid} は無いか、もう答えが出ています★")
        return 1
    hit["retracted_at"] = _now()
    hit["retract_why"] = why
    hit["retracted_by"] = sorted(x.strip() for x in by if str(x).strip())
    _save(path, d)
    print(f"#{qid} を取り下げました（運営者の判断は要らなくなった）")
    return 0


def close_resolved(path: Path, title: str, why: str, by: str) -> list:
    """★★問いを出した道具が、問いの元になった詰まりが解けたのを見て、その質問を閉じる★★（2026-11-12）

    なぜ＝育成が「確認済みだった内容を再現できません」で運営者へ回した後、朝のタスクが2AIで片づけて
    育成が通るようになっても、質問は開いたままで、番人が古い質問をメールし続けた（運営者は答えようがない）。
    ★閉じてよいのは、その題を出した道具が「詰まりが解けた」と自分で確かめたときだけ★（題の完全一致）。
    答えたことにも、2AIの取り下げにもしない（resolved_at と resolve_why・resolved_by で分ける）。
    返すのは閉じた質問の番号。
    """
    why = str(why or "").strip()
    by = str(by or "").strip()
    if len(why) < 10 or not by:
        raise SystemExit("★閉じる理由（10字以上）と閉じた道具の名前が要ります★")
    if not path.is_file():
        return []
    d = _load(path)
    hits = [it for it in d["items"] if it.get("title") == title and not _closed(it)]
    if not hits:
        return []
    for it in hits:
        it["resolved_at"] = _now()
        it["resolve_why"] = why
        it["resolved_by"] = by
    _save(path, d)
    for it in hits:
        print(f"#{it['id']} を閉じました（{by}：{why}）")
    return [it["id"] for it in hits]


def mark(path: Path, qid: int, key: str, note: str = "") -> int:
    d = _load(path)
    hit = next((it for it in d["items"] if it.get("id") == qid), None)
    if hit is None:
        print(f"★#{qid} はありません★")
        return 1
    hit[key] = _now()
    if note:
        hit["answer"] = note
    _save(path, d)
    print(f"#{qid} に {key} を付けました")
    return 0


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except SystemExit:
        return True


def selftest() -> int:
    import tempfile
    ok = [0, 0]

    def t(name, cond):
        ok[1] += 1
        ok[0] += bool(cond)
        print(("✅ " if cond else "❌ ") + name)

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "q.json"
        a = add(p, "沖ドキの天井", "Claude=999G（A）／Codex=777G（B）")
        t("★質問を控えると、送るまで pending に出る★", [i["id"] for i in pending(p)] == [a])
        mark(p, a, "sent_at")
        t("★★送れた印が付いたら pending から外れる★★（毎朝同じメールを出さない）",
          pending(p) == [])
        b = add(p, "沖ドキの天井", "Claude=999G（A2）／Codex=777G（B2）")
        t("★★同じ題を出し直すと、本文が新しくなり、届け直しの対象に戻る★★",
          b == a and [i["id"] for i in pending(p)] == [a]
          and "A2" in pending(p)[0]["detail"])
        mark(p, a, "answered_at", "Claudeので")
        t("★答えが記録されたら pending から外れる★", pending(p) == [])
        c = add(p, "沖ドキの天井", "新しい食い違い")
        t("　答えが出た質問と同じ題でも、新しい質問として控える", c != a)
        e = add(p, "別の天井", "Claude=1/2／Codex=3/4")
        mark(p, e, "sent_at")
        _d = _load(p)
        for _it in _d["items"]:
            if _it["id"] == e:
                _it["sent_at"] = "2026-11-01T09:00:00"
        _save(p, _d)
        t("★★送ったのに7日答えが無い質問を挙げる★★（直す前は送ったら二度と見なかった）",
          [i["id"] for i in unanswered(p, "2026-11-09T09:00:00")] == [e]
          and unanswered(p, "2026-11-07T09:00:00") == [])
        t("　取り下げは判断者の組と理由が要る",
          retract(p, e, "短い", ["claude", "codex"]) == 1 and retract(p, e, "2AIで落としてよいと決めて片づいた", ["claude"]) == 1)
        t("★★2AIで片づいた質問は取り下げられ、答えの無い一覧から外れる★★",
          retract(p, e, "2AIで落としてよいと決めて片づいたので聞く必要が無い", ["claude", "codex"]) == 0
          and unanswered(p, "2026-11-09T09:00:00") == [])
        f = add(p, "同じ本文", "本文はこのまま")
        mark(p, f, "sent_at")
        t("★★同じ題・同じ本文で出し直しても、送った印を消さない（届け直さない）★★（同じ質問が3回届いた）",
          add(p, "同じ本文", "本文はこのまま") == f and f not in [i["id"] for i in pending(p)])
        t("　本文が変われば届け直す", add(p, "同じ本文", "本文が変わった") == f
          and f in [i["id"] for i in pending(p)])
        g = add(p, "zzz: 再現できません", "詰まった")
        g2 = add(p, "zzz: 再現できませんX", "題が少し違う")   # ★閉じる前から開いている、題の一部が同じ質問★
        mark(p, g, "sent_at")
        _dg = _load(p)
        for _it in _dg["items"]:
            if _it["id"] == g:
                _it["sent_at"] = "2026-11-01 09:00:00"
        _save(p, _dg)
        t("　（前提）送ってから7日たった質問は答え待ちに出る",
          g in [i["id"] for i in unanswered(p, "2026-11-12 09:00:00")])
        t("　閉じる理由と道具の名前が要る", _raises(lambda: close_resolved(p, "zzz: 再現できません", "短い", "x"))
          and _raises(lambda: close_resolved(p, "zzz: 再現できません", "育成が通るようになった", "")))
        t("★★問いを出した道具が詰まりの解けたのを確かめたら、その質問を閉じる（届け直し・答え待ちから外れる）★★",
          close_resolved(p, "zzz: 再現できません", "育成が通るようになった", "grow_machine") == [g]
          and g not in [i["id"] for i in pending(p)]
          and g not in [i["id"] for i in unanswered(p, "2026-11-12 09:00:00")])
        t("★題の一部が同じ別の質問は閉じない（題の完全一致）★（Codexの指摘＝閉じる前から開いている質問で確かめる）",
          g2 in [i["id"] for i in pending(p)])
        t("★★まだ送っていない質問も、閉じたら届け直しの一覧から外れる★★（Codexの指摘＝送り済みの質問では空振り）",
          close_resolved(p, "zzz: 再現できませんX", "育成が通るようになった", "grow_machine") == [g2]
          and g2 not in [i["id"] for i in pending(p)])
        t("　題が違う質問・閉じた質問は閉じない（題の完全一致）",
          close_resolved(p, "zzz: 再現できませ", "育成が通るようになった", "grow_machine") == []
          and close_resolved(p, "zzz: 再現できません", "育成が通るようになった", "grow_machine") == [])
        t("★閉じた質問と同じ題でまた詰まれば、新しい質問として控えて届ける★",
          (lambda h: h != g and h in [i["id"] for i in pending(p)])(add(p, "zzz: 再現できません", "また詰まった")))
        e2 = add(p, "別の天井", "新しく割れた")
        t("★★取り下げた質問と同じ題でも、新しい質問として控えて届ける★★（Codexの指摘）",
          e2 != e and e2 in [i["id"] for i in pending(p)] and e not in [i["id"] for i in pending(p)])
        mark(p, e2, "sent_at")
        _d2 = _load(p)
        for _it in _d2["items"]:
            if _it["id"] == e2:
                _it["sent_at"] = "2026-11-01 23:00:00"
        _save(p, _d2)
        t("　7日の判定は日時で比べる（11/1 23時に送った質問は 11/8 9時にはまだ出ない・11/9 0時には出る）",
          unanswered(p, "2026-11-08 09:00:00") == [] and [i["id"] for i in unanswered(p, "2026-11-09 00:00:00")] == [e2])
        _d3 = _load(p)
        for _it in _d3["items"]:
            if _it["id"] == e2:
                _it["sent_at"] = "2026-11-09 09:00:00broken"
        _save(p, _d3)
        t("　壊れた日時は「読めない」として答え待ちに出す（黙って外さない・Codexの指摘）",
          [i["id"] for i in unanswered(p, "2026-11-09 10:00:00")] == [e2])
        p.write_text("[]", encoding="utf-8")
        try:
            pending(p)
            t("★★壊れた控えは読まずに止める★★（空として扱って上書きしない）", False)
        except SystemExit:
            t("★★壊れた控えは読まずに止める★★（空として扱って上書きしない）", True)
    print(f"\n{ok[0]}/{ok[1]} 合格")
    return 0 if ok[0] == ok[1] else 1


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    ap = argparse.ArgumentParser(description="運営者への質問を届くまで送り直す控え")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add")
    p.add_argument("--title-file", required=True)
    p.add_argument("--detail-file", required=True)
    sub.add_parser("pending")
    sub.add_parser("unanswered")
    p = sub.add_parser("retract")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--why-file", required=True)
    p.add_argument("--by", required=True)
    for name in ("sent", "answered"):
        p = sub.add_parser(name)
        p.add_argument("--id", type=int, required=True)
        if name == "answered":
            p.add_argument("--note-file", default="")
    a = ap.parse_args()
    if a.cmd == "add":
        add(STORE, _ta.read_text_arg("", a.title_file, "title", allow_newline=False),
            _ta.read_text_arg("", a.detail_file, "detail"))
        return 0
    if a.cmd == "pending":
        items = pending(STORE)
        if not items:
            print("運営者への質問で、まだ届いていないものはありません")
        for it in items:
            print(f"#{it['id']} {it['title']}\n{it['detail']}\n")
        return 0
    if a.cmd == "unanswered":
        items = unanswered(STORE)
        for it in items:
            print(f"#{it['id']} {it['title']}（送った日 {str(it['sent_at'])[:10]}）")
        print((f"🟡 運営者への質問で、送ってから{UNANSWERED_DAYS}日以上答えが無いものが{len(items)}件あります"
               "（2AIで片づいたなら retract で取り下げる）") if items else "🟢 答えを待っている質問はありません")
        return 1 if items else 0
    if a.cmd == "retract":
        return retract(STORE, a.id, _ta.read_text_arg("", a.why_file, "why"),
                       [x for x in a.by.split(",") if x.strip()])
    if a.cmd == "sent":
        return mark(STORE, a.id, "sent_at")
    note = _ta.read_text_arg("", a.note_file, "note") if a.note_file else ""
    return mark(STORE, a.id, "answered_at", note)


if __name__ == "__main__":
    raise SystemExit(main())

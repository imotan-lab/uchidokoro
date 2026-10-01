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
        if it.get("title") == title and not it.get("answered_at"):
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


def pending(path: Path) -> list:
    return [it for it in _load(path)["items"]
            if not it.get("sent_at") and not it.get("answered_at")]


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
    if a.cmd == "sent":
        return mark(STORE, a.id, "sent_at")
    note = _ta.read_text_arg("", a.note_file, "note") if a.note_file else ""
    return mark(STORE, a.id, "answered_at", note)


if __name__ == "__main__":
    raise SystemExit(main())

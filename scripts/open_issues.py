# -*- coding: utf-8 -*-
"""要確認案件の恒久台帳（open_issues.json）操作ツール。

「要手動確認がメール1通に埋もれて放置される」問題の構造対策（2026-06-30設計 wkc8g7yhw）。
自動タスク（new-machine/auto-add/verify/quality-review）がエスカレーションを add で積み、
verify の毎朝メールに digest（未解決一覧・経過日数付き）を再掲し続ける。
解決したら close する（人間または対応したセッションのClaude）。
無人タスクのcloseは原則禁止。唯一の例外＝verify STEP 2.8のホワイトリスト型quality修正
（audit/validate機械確認済みの場合のみ。数値転記を含む案件は例外の対象外）。

★このスクリプトは機種データを一切触らない（台帳ファイルの読み書きのみ）★

使い方:
  python scripts/open_issues.py add --source verify --slug hokuto --kind external_value \
      --title "狙い目760G疑義" --detail "複数サイトは550G/650Gの報告あり・要裏取り"
      → 同一(slug+kind+title)が既にopenなら重複登録せず last_seen だけ更新
  python scripts/open_issues.py list            # open案件を一覧表示
  python scripts/open_issues.py list --all      # closed含め全件
  python scripts/open_issues.py digest          # メール転記用ブロックを出力（open 0件なら空出力・exit 0）
  python scripts/open_issues.py close --id 3 --reason "5サイト裏取りの上150Gに統一(コミットabc123)"

kind の目安:
  external_value    外部数値の疑義（無人修正禁止カテゴリ・裏取り待ち）
  structural        構造判断（重複統合・新規作成可否など）
  quality           品質指摘（quality-review C評価など）
  environment       環境問題（python3スタブ等）
  other             その他

保存先: C:/Users/imao_/Documents/uchidokoro/open_issues.json（--fileで上書き可・テスト用）
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

DEFAULT_FILE = Path("C:/Users/imao_/Documents/uchidokoro/open_issues.json")


def _load(path):
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"next_id": 1, "issues": []}


def _save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _today():
    return datetime.date.today().isoformat()


def _days_open(issue):
    try:
        first = datetime.date.fromisoformat(issue["first_seen"])
        return (datetime.date.today() - first).days
    except Exception:
        return -1


def cmd_add(path, args):
    data = _load(path)
    for it in data["issues"]:
        if it["status"] == "open" and it["slug"] == args.slug and \
           it["kind"] == args.kind and it["title"] == args.title:
            it["last_seen"] = _today()
            if args.detail and args.detail not in (it.get("detail") or ""):
                it["detail"] = (it.get("detail") or "") + f"\n[{_today()}追記] {args.detail}"
            _save(path, data)
            print(f"既存案件 #{it['id']} の last_seen を更新（重複登録なし・経過{_days_open(it)}日）")
            return 0
    issue = {
        "id": data["next_id"],
        "status": "open",
        "source": args.source,
        "slug": args.slug,
        "kind": args.kind,
        "title": args.title,
        "detail": args.detail or "",
        "first_seen": _today(),
        "last_seen": _today(),
        "resolution": None,
        "resolved_date": None,
    }
    data["issues"].append(issue)
    data["next_id"] += 1
    _save(path, data)
    print(f"新規案件 #{issue['id']} を登録: [{args.kind}] {args.slug}: {args.title}")
    return 0


def cmd_list(path, args):
    data = _load(path)
    items = data["issues"] if args.all else [i for i in data["issues"] if i["status"] == "open"]
    if not items:
        print("open案件なし" if not args.all else "案件なし")
        return 0
    for it in items:
        mark = "🔓" if it["status"] == "open" else "✅"
        days = f"・経過{_days_open(it)}日" if it["status"] == "open" else f"・解決{it.get('resolved_date')}"
        print(f"{mark} #{it['id']} [{it['kind']}] {it['slug']}: {it['title']}（{it['source']}・初出{it['first_seen']}{days}）")
        if it.get("detail"):
            for line in str(it["detail"]).splitlines():
                print(f"      {line}")
        if it["status"] != "open" and it.get("resolution"):
            print(f"      → 解決: {it['resolution']}")
    return 0


def cmd_digest(path, args):
    data = _load(path)
    items = [i for i in data["issues"] if i["status"] == "open"]
    if not items:
        return 0  # 空出力＝メールに何も足さない
    items.sort(key=_days_open, reverse=True)
    print("━━━ 未解決の要確認案件（解決するまで毎朝再掲されます） ━━━")
    for it in items:
        days = _days_open(it)
        urgency = "🔴" if days >= 7 else ("🟠" if days >= 3 else "🟡")
        print(f"{urgency} #{it['id']} [{it['kind']}] {it['slug']}: {it['title']}（経過{days}日・初出{it['first_seen']}・発見元{it['source']}）")
        if it.get("detail"):
            first_line = str(it["detail"]).splitlines()[0]
            print(f"    {first_line}")
    print(f"（計{len(items)}件。対応後は python scripts/open_issues.py close --id N --reason \"...\" でクローズ）")
    return 0


def cmd_close(path, args):
    data = _load(path)
    for it in data["issues"]:
        if it["id"] == args.id:
            if it["status"] != "open":
                print(f"#{args.id} は既にclosed（{it.get('resolved_date')}）")
                return 0
            it["status"] = "closed"
            it["resolution"] = args.reason
            it["resolved_date"] = _today()
            _save(path, data)
            print(f"案件 #{args.id} をクローズ: {args.reason}")
            return 0
    print(f"⚠ 案件 #{args.id} が見つかりません")
    return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="", help="台帳ファイルパス（既定: Documents/uchidokoro/open_issues.json）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add")
    p.add_argument("--source", required=True, help="発見元タスク（verify/new-machine/auto-add/quality-review/manual）")
    p.add_argument("--slug", required=True, help="対象機種slug（機種以外は site/env 等）")
    p.add_argument("--kind", required=True, choices=["external_value", "structural", "quality", "environment", "other"])
    p.add_argument("--title", required=True)
    p.add_argument("--detail", default="")

    p = sub.add_parser("list")
    p.add_argument("--all", action="store_true")

    sub.add_parser("digest")

    p = sub.add_parser("close")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--reason", required=True)

    args = ap.parse_args()
    path = Path(args.file) if args.file else DEFAULT_FILE
    fn = {"add": cmd_add, "list": cmd_list, "digest": cmd_digest, "close": cmd_close}[args.cmd]
    sys.exit(fn(path, args))


if __name__ == "__main__":
    main()

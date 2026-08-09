# -*- coding: utf-8 -*-
"""要確認案件の恒久台帳（open_issues.json）操作ツール。

「要手動確認がメール1通に埋もれて放置される」問題の構造対策（2026-06-30設計 wkc8g7yhw）。
自動タスク（new-machine/auto-add/verify/quality-review）がエスカレーションを add で積み、
verify の毎朝メールに digest（未解決一覧・経過日数付き）を再掲し続ける。
解決したら close する（人間または対応したセッションのClaude）。
無人タスクのcloseは原則禁止。例外は2つのみ＝
①verify STEP 2.8のホワイトリスト型quality修正（audit/validate機械確認済み・数値転記を含む案件は対象外）
②verify STEP 2.9の自動裏取り（verify_claims.py exit 0＝出典の機械検証合格が根拠の場合のみ）。

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

# ---------------------------------------------------------------- 自由文の受け取り
# ★なぜファイル渡しにするか（2026-08-09）★
#   2026-08-08、無人タスクが台帳に
#     「`python scripts/codex_reported.py` を実行する必要がある」
#   と**書こうとしただけ**で、その部分が本当に実行された。
#   バッククォートは文章としては飾りでも、シェルには
#   「ここを実行して結果を差し込め」という命令だから。
#   手順書は「ツールの出力をそのまま転記」「外部サイトの機種名を渡す」形なので、
#   同じことがいつでも起き得る。
#   ★文章はファイルに書き、コマンドにはパスだけを渡す★＝中身は読まれるだけで
#   実行されない。無人タスクが動いている間は、直接指定を受け付けない。

LOCK_PATH = Path("C:/Users/imao_/Documents/uchidokoro/task.lock")
LOCK_STALE_MIN = 30           # task_lock.py と同じ（これを超えたら残骸とみなす）
MAX_TEXT_BYTES = 64 * 1024

# ★文章ファイルはここから下だけ★（2026-08-09・依頼127 A-2 P1）
#   どこのファイルでも読めると、うっかり認証情報のファイルを指したときに
#   台帳やメールへその中身が写る。置き場を決めておけば起こらない。
TEXT_ROOTS = (
    Path("C:/Users/imao_/Documents/uchidokoro/ops"),
    Path("C:/Users/imao_/Desktop/個人用/うちどころ/_design"),
)


def _running_task() -> str:
    """無人タスクが動いている最中ならタスク名を返す（動いていなければ空）。"""
    try:
        d = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except Exception:                        # noqa: BLE001
        return ""
    ts = d.get("heartbeat") or d.get("started_at")
    try:
        t = datetime.datetime.fromisoformat(str(ts).replace("Z", ""))
    except Exception:                        # noqa: BLE001
        return ""
    if (datetime.datetime.now() - t).total_seconds() / 60.0 > LOCK_STALE_MIN:
        return ""                            # 残骸のロックは「動いていない」扱い
    return str(d.get("task") or "")


def _read_text_arg(inline: str, path: str, label: str,
                   allow_newline: bool = True) -> str:
    """自由文を受け取る。★直接指定とファイル指定は同時に使えない★"""
    if inline and path:
        raise SystemExit(f"★{label} は直接指定とファイル指定を同時に使えません★")
    if path:
        p = Path(path)
        if p.is_symlink() or not p.is_file():
            raise SystemExit(f"★{label}: 通常のファイルではありません: {path}★")
        real = p.resolve()
        if not any(str(real).lower().startswith(str(r.resolve()).lower())
                   for r in TEXT_ROOTS):
            raise SystemExit(
                f"★{label}: この置き場のファイルは使えません: {real}★ "
                + "／".join(str(r) for r in TEXT_ROOTS) + " の下に置いてください"
                "（うっかり認証情報のファイルを指しても台帳に写らないため）")
        size = real.stat().st_size          # ★読む前に大きさを見る★
        if size > MAX_TEXT_BYTES:
            raise SystemExit(
                f"★{label}: 大きすぎます（{size}バイト・上限{MAX_TEXT_BYTES}）★")
        raw = real.read_bytes()
        try:
            text = raw.decode("utf-8")       # ★strict＝壊れた文字は受け取らない★
        except UnicodeDecodeError as e:
            raise SystemExit(f"★{label}: UTF-8として読めません（{e}）★")
        # ★Windowsの改行（CRLF）で書かれたファイルも受け取る★
        #   以前はCRを制御文字として弾いていた。メモ帳等で書くと必ずCRLFになる。
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    else:
        text = inline or ""
        # ★シェルを通らない呼び出しだけは直接指定を許す★（2026-08-09）
        #   add_machine_run.py などは subprocess の引数配列で呼ぶので、
        #   文章の中の記号が実行されることはない（危ないのはシェル文字列だけ）。
        #   この印は「うっかり古い書き方に戻らないため」のものであって、
        #   安全の境界ではない（境界は PreToolUse の shell_guard.py）。
        if text and os.environ.get("UCHIDOKORO_ARGV_CALL") != "1" \
                and _running_task():
            raise SystemExit(
                f"★{label} は無人タスクの実行中は直接指定できません"
                f"（{_running_task()} が実行中）★ "
                f"文章をファイルに書いて --{label}-file でパスを渡してください"
                "（コマンドに文章を書くと、中の記号がシェルに実行されます）")
    bad = [c for c in text if c in "\x00" or (ord(c) < 32 and c not in "\n\t")]
    if bad:
        raise SystemExit(f"★{label}: 使えない制御文字が入っています★")
    if not allow_newline and ("\n" in text or "\r" in text):
        raise SystemExit(f"★{label}: 改行は入れられません★")
    return text.strip()


# ★どれだけ危ないか★（2026-07-30・Codex「これだけはやれ」⑧）
#   C評価が52件たまっていたが、「全部止める」も「全部出し続ける」も雑すぎる。
#   **公開を止めるべきものだけ**を機械が判別できるように段階を付ける。
#
#   CRITICAL … 機械の客観的な事実が誤っている疑い。★公開を止める★
#               別機種・別型式の混入／天井・恩恵・設定段階・機種タイプの誤り／
#               CZ間とAT間、実G と 液晶G、G と pt の取り違え。
#   MATERIAL … 当サイトの目安どうしが食い違っている等。読者は混乱するが、
#               機械について誤ったことを述べてはいない。公開は続けて順に直す。
#   QUALITY  … 文体・冗長・読みやすさ。公開に影響しない。
SEVERITIES = ("CRITICAL", "MATERIAL", "QUALITY")


def severity_of(issue: dict) -> str:
    """案件の危険度。★未設定は MATERIAL 扱いにしない★

    未設定＝まだ人が仕分けていない、という意味なので、
    公開を止める側（CRITICAL）に倒す（fail-closed）。
    仕分けが終わっていないものを黙って公開に通さない。
    """
    sev = issue.get("severity")
    return sev if sev in SEVERITIES else "CRITICAL"


def blocking_slugs(path=None) -> dict:
    """★公開を止めるべき機種★ {slug: [理由, ...]}（未解決の CRITICAL だけ）"""
    data = _load(Path(path) if path else DEFAULT_FILE)
    out: dict = {}
    for it in data.get("issues") or []:
        if it.get("status") != "open":
            continue
        if severity_of(it) != "CRITICAL":
            continue
        slug = it.get("slug")
        if not slug or slug in ("site", "env", "_site", "-"):
            continue        # サイト全体の課題は機種の公開停止にしない
        out.setdefault(slug, []).append(f"#{it['id']} {it.get('title','')}")
    return out


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
    args.title = _read_text_arg(args.title, args.title_file, "title",
                                allow_newline=False)
    args.detail = _read_text_arg(args.detail, args.detail_file, "detail")
    if not args.title:
        raise SystemExit("★--title または --title-file が要ります★")
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
        "severity": args.severity,
        "reason_code": args.reason_code or None,
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
    print("（対応方法: このメールをClaude Codeのセッションに貼り付けて「対応して」と伝えるだけでOK。裏取り→修正→closeまで処理されます）")
    return 0


def cmd_close(path, args):
    args.reason = _read_text_arg(args.reason, args.reason_file, "reason")
    if not args.reason:
        raise SystemExit("★--reason または --reason-file が要ります★")
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


def cmd_severity(path, args):
    data = _load(path)
    for it in data["issues"]:
        if it["id"] == args.id:
            old = it.get("severity") or "(未設定)"
            it["severity"] = args.level
            it["reason_code"] = args.reason_code
            _save(path, data)
            print(f"#{args.id}: {old} → {args.level} ({args.reason_code})")
            return 0
    print(f"案件 #{args.id} が見つかりません")
    return 1


def cmd_blocking(path, args):
    """公開を止めるべき機種の一覧（ビルドが読む形）。"""
    b = blocking_slugs(path)
    print(json.dumps(b, ensure_ascii=False, indent=1))
    print(f"# 公開を止めるべき機種: {len(b)}", file=sys.stderr)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="", help="台帳ファイルパス（既定: Documents/uchidokoro/open_issues.json）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add")
    p.add_argument("--source", required=True, help="発見元タスク（verify/new-machine/auto-add/quality-review/manual）")
    p.add_argument("--slug", required=True, help="対象機種slug（機種以外は site/env 等）")
    p.add_argument("--kind", required=True, choices=["external_value", "structural", "quality", "environment", "other"])
    # ★自由文はファイル渡しを使う★（無人タスクでは直接指定を拒否・2026-08-09）
    p.add_argument("--title", default="")
    p.add_argument("--title-file", dest="title_file", default="",
                   help="一行要約を書いたファイル（無人タスクはこちら）")
    p.add_argument("--detail", default="")
    p.add_argument("--detail-file", dest="detail_file", default="",
                   help="判断材料を書いたファイル（無人タスクはこちら）")
    p.add_argument("--severity", choices=SEVERITIES, default="CRITICAL",
                   help="どれだけ危ないか（既定は CRITICAL＝仕分け前は止める側に倒す）")
    p.add_argument("--reason-code", dest="reason_code", default="",
                   help="機械可読な理由コード（例: WRONG_CEILING / MEYASU_MISMATCH）")

    p = sub.add_parser("severity")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--level", required=True, choices=SEVERITIES)
    p.add_argument("--reason-code", dest="reason_code", required=True)

    p = sub.add_parser("blocking")

    p = sub.add_parser("list")
    p.add_argument("--all", action="store_true")

    sub.add_parser("digest")

    p = sub.add_parser("close")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--reason", default="")
    p.add_argument("--reason-file", dest="reason_file", default="",
                   help="クローズ理由を書いたファイル（無人タスクはこちら）")

    args = ap.parse_args()
    path = Path(args.file) if args.file else DEFAULT_FILE
    fn = {"add": cmd_add, "list": cmd_list, "digest": cmd_digest, "close": cmd_close,
          "severity": cmd_severity, "blocking": cmd_blocking}[args.cmd]
    sys.exit(fn(path, args))


if __name__ == "__main__":
    main()

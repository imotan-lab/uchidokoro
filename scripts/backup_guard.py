# -*- coding: utf-8 -*-
"""
backup_guard.py — Dropboxバックアップの秘密情報ガード（二層・決定論・LLM非依存）

2026-07-16 チャッピー第5次レビュー追撃指摘（A10条件）を受けて新設。
「認証情報がクラウドへ同期されてから検知しても遅い」ため、二層で守る:

  前段（copy）: バックアップのコピーを本スクリプト経由に一本化。
               ①許可リスト（バックアップしてよいファイル名の完全な一覧）に無いものはコピーしない
               ②許可リスト内でも秘密パターン（名前・JSONキー・値・Cookie構造）に該当したらコピーしない
  後段（scan）: 毎朝の番兵（task-watchdog）がDropbox側を再帰走査し、
               秘密パターンに該当するファイルの残存・混入を検知する

サブコマンド:
    copy <src> <dst> [--optional]   検査合格時のみコピー。
                                    exit 0: コピー成功（--optional時はsrc不存在も0）
                                    exit 1: 検査不合格＝コピー拒否（理由をログへ）
                                    exit 2: src不存在・IOエラー
    scan --dir <path>               再帰走査して秘密パターン該当を報告。
                                    exit 0: 検出なし / exit 1: 検出あり
    --selftest                      一時ファイルで全動作を自己検証（ネット不要）

★検知ログ・標準出力には秘密値そのものを一切出さない★
（ファイル名・JSONキーのパス・検知ルール名のみ。値や前後の文字列は転載禁止）

ログ: C:/Users/imao_/Documents/uchidokoro/logs/backup_guard.log
"""
from __future__ import annotations
import argparse
import datetime
import json
import os
import re
import shutil
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

LOG_PATH = r"C:/Users/imao_/Documents/uchidokoro/logs/backup_guard.log"

# ── 前段copy用: バックアップを許可するファイル名（完全一覧・basename照合）──
ALLOW_BASENAMES = {
    "SKILL.md",
    "send_notify.py",
    "refresh_x_cookies.py",
    "x_poster.py",
    "post_to_x.py",
    "post_update_to_x.py",
    "uchidokoro_state.json",
    "post_to_x_detached.log",
    "post_update_to_x_detached.log",
    "復旧手順.md",
    "consensus_design.md",   # コンセンサス設計書(gpt_research直下・ローカルのみ→Dropbox保全・秘密でない)
    # プロジェクトCLAUDE.md（.gitignore対象＝Dropboxが唯一の保全先・秘密は含めない運用）
    "CLAUDE_uchidokoro.md",
    "CLAUDE_history_uchidokoro.md",
}
# 日付つきタスクログ（例: new_machine_2026-07-16.log）
ALLOW_LOG_RE = re.compile(r"^[a-z0-9_]+_\d{4}-\d{2}-\d{2}\.log$")

# ── 秘密パターン: ファイル名（正規化後の部分一致）──
DENY_NAME_SUBSTR = [
    "secret", "credential", "cookie", "password", "passwd",
    "client_secret", "api_key", "apikey", "refresh_token", "access_token",
    "private_key", "gmail_config", "x_storage", "storage_state",
    "id_rsa", "id_ed25519", "keystore",
]
# 短く誤爆しやすい語はセグメント一致（アンダースコア区切りの単語単位）で判定
DENY_NAME_SEGMENT = {"auth", "oauth", "token", "session", "storage", "env"}
DENY_EXTENSIONS = {".pem", ".key", ".pfx", ".p12", ".jks"}
ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar", ".tar", ".gz", ".tgz"}

# ── 秘密パターン: JSONキー（大小文字・-/_ 無視・再帰）──
DENY_JSON_KEYS = {
    "app_password", "password", "passwd", "client_secret", "private_key",
    "access_token", "refresh_token", "id_token", "auth_token", "authorization",
    "bearer", "api_key", "apikey", "cookies", "storage_state",
    "sessionid", "session_id", "csrf", "xsrf", "smtp_password",
}
# 単独では一般的すぎる語（secret/token/cookie/session）はキー名の完全一致のみ
DENY_JSON_KEYS_EXACT = {"secret", "token", "cookie", "session"}

# ── 秘密パターン: 値（テキスト全文への正規表現・具体プレフィックスのみ）──
DENY_VALUE_PATTERNS = [
    ("github_token", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("github_pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}\.")),
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_\-]{30,}")),
    ("bearer_header", re.compile(r"Bearer [A-Za-z0-9_\-\.=]{25,}")),
]


def _log(msg: str) -> None:
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now().strftime('%Y/%m/%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _norm_name(name: str) -> str:
    return name.lower().replace("-", "_")


def _norm_key(key: str) -> str:
    return str(key).lower().replace("-", "_")


def name_findings(basename: str) -> list[str]:
    """ファイル名ベースの秘密パターン検知。ルール名のリストを返す。"""
    out = []
    n = _norm_name(basename)
    stem, ext = os.path.splitext(n)
    for s in DENY_NAME_SUBSTR:
        if s in n:
            out.append(f"name:{s}")
    segs = set(re.split(r"[^a-z0-9]+", stem))
    for s in DENY_NAME_SEGMENT & segs:
        out.append(f"name_seg:{s}")
    if n.startswith(".env"):
        out.append("name:.env")
    if ext in DENY_EXTENSIONS:
        out.append(f"ext:{ext}")
    return out


def _json_key_findings(obj, path="$") -> list[str]:
    out = []
    if isinstance(obj, dict):
        keys = list(obj.keys())
        for k in keys:
            nk = _norm_key(k)
            if nk in DENY_JSON_KEYS or nk in DENY_JSON_KEYS_EXACT:
                out.append(f"json_key:{path}.{k}")
            out.extend(_json_key_findings(obj[k], f"{path}.{k}"))
        # Cookie構造（name/value/domainを持つdictの配列）・storage_state構造
        if {"origins", "cookies"} & {_norm_key(k) for k in keys}:
            pass  # cookiesキー自体は上で検知済み
    elif isinstance(obj, list):
        dictitems = [x for x in obj if isinstance(x, dict)]
        if len(dictitems) >= 3 and all({"name", "value", "domain"} <= {_norm_key(k) for k in x.keys()} for x in dictitems[:3]):
            out.append(f"cookie_structure:{path}")
            return out  # 配列の中まで潜らない（値を触らない）
        for i, x in enumerate(obj[:50]):
            out.extend(_json_key_findings(x, f"{path}[{i}]"))
    return out


def content_findings(path: str) -> list[str]:
    """中身ベースの検知（JSONキー・Cookie構造・値パターン）。ルール名のみ返す。"""
    out = []
    try:
        size = os.path.getsize(path)
        if size > 20 * 1024 * 1024:  # 20MB超のテキスト走査はスキップ（名前検査は済み）
            return out
        with open(path, "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8", errors="ignore")
    except Exception:
        return out
    if path.lower().endswith(".json"):
        try:
            out.extend(_json_key_findings(json.loads(text)))
        except Exception:
            pass
    for rule, pat in DENY_VALUE_PATTERNS:
        if pat.search(text):
            out.append(f"value:{rule}")
    return out


def is_allowlisted(basename: str) -> bool:
    return basename in ALLOW_BASENAMES or bool(ALLOW_LOG_RE.match(basename))


def cmd_copy(src: str, dst: str, optional: bool) -> int:
    base = os.path.basename(src)
    dst_base = os.path.basename(dst)
    if not os.path.exists(src):
        if optional:
            _log(f"copy: src不存在（optional・スキップ）: {base}")
            print("SKIPPED_MISSING")
            return 0
        _log(f"copy: src不存在: {base}")
        print("SRC_MISSING")
        return 2
    findings = []
    # 許可リストは「バックアップ先に存在してよい名前」の一覧なのでdst名で照合する
    # （例: state.json → uchidokoro_state.json にリネームコピーする運用のため。
    #   ただし秘密パターンの名前検査はsrc/dst両方に掛ける＝リネームによるすり替えを防ぐ）
    if not is_allowlisted(dst_base):
        findings.append("allowlist:リスト外")
    for b in {base, dst_base}:
        if os.path.splitext(b.lower())[1] in ARCHIVE_EXTENSIONS:
            findings.append("archive:圧縮ファイルは原則バックアップ禁止")
        findings.extend(name_findings(b))
    findings.extend(content_findings(src))
    if findings:
        _log(f"copy: ❌拒否 {base} → 検知ルール: {', '.join(findings)}")
        print(f"BLOCKED {base} RULES={','.join(findings)}")
        return 1
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    _log(f"copy: ✅ {base} → {dst}")
    print("COPIED")
    return 0


# ── gpt_research のバックアップ対象定義（2026-07-18 チャッピー限定許可）──
#   触ってよいDropboxルート（ユーザー厳命: この階層より上へ出ない）
DROPBOX_ROOT_ALLOWED = r"C:/Users/imao_/今電 Dropbox/今電　今尾笙夢"
TREE_INCLUDE_DIRS = {"gold_eval", "results", "input_snapshot"}
TREE_INCLUDE_GLOBS = ["gold_set_v*.json", "codex_schema_*.json",
                      "gold_freeze_log*.txt", "shadow_state.json"]
TREE_EXCLUDE_DIRS = {"claims_check", "workdir", ".codex", ".claude", "notify_body.txt"}
TREE_EXCLUDE_EXT = {".tmp"}


def _under(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) \
            == os.path.abspath(root)
    except ValueError:
        return False


def _tree_included(rel_parts: tuple, basename: str) -> bool:
    import fnmatch
    top = rel_parts[0] if rel_parts else ""
    if top in TREE_INCLUDE_DIRS:
        return True
    if len(rel_parts) == 1:  # ルート直下のファイルはglob許可のみ
        return any(fnmatch.fnmatch(basename, g) for g in TREE_INCLUDE_GLOBS)
    return False


def cmd_backup_tree(src_root: str, dst_root: str) -> int:
    """gpt_research配下の許可サブセットをDropboxへ秘密検査つきでバックアップ。
    ★宛先は認可Dropboxルート配下に限定（それより上には出ない）★。
    許可リスト(basename)は使わずinclude/exclude規則＋秘密パターン検査で判定する。"""
    if not _under(dst_root, DROPBOX_ROOT_ALLOWED):
        print(f"REFUSED_DST_OUT_OF_ROOT dst={dst_root}")
        _log(f"backup-tree: ❌宛先が認可ルート外 → 中止: {dst_root}")
        return 2
    copied = blocked = skipped = 0
    blocks = []
    for dirpath, dirs, files in os.walk(src_root):
        dirs[:] = [d for d in dirs if d not in TREE_EXCLUDE_DIRS]
        for fn in files:
            src = os.path.join(dirpath, fn)
            rel = os.path.relpath(src, src_root).replace("\\", "/")
            rel_parts = tuple(rel.split("/"))
            if os.path.splitext(fn)[1].lower() in TREE_EXCLUDE_EXT \
                    or fn in TREE_EXCLUDE_DIRS or not _tree_included(rel_parts, fn):
                skipped += 1
                continue
            findings = name_findings(fn) + content_findings(src)
            if os.path.splitext(fn.lower())[1] in ARCHIVE_EXTENSIONS:
                findings.append("archive")
            if findings:
                blocked += 1
                blocks.append((rel, findings))
                _log(f"backup-tree: ❌拒否 {rel} → {', '.join(findings)}")
                continue
            dst = os.path.join(dst_root, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
    print(f"BACKUP_TREE copied={copied} blocked={blocked} skipped={skipped}")
    for rel, f in blocks:
        print(f"  BLOCKED {rel} RULES={','.join(f)}")
    _log(f"backup-tree: ✅ copied={copied} blocked={blocked} skipped={skipped} → {dst_root}")
    return 0 if blocked == 0 else 1


def cmd_scan(root: str) -> int:
    total = 0
    hits = []
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            total += 1
            p = os.path.join(dirpath, fn)
            # 許可リスト内の既知ファイル名は名前ルールを免除（refresh_x_cookies.py等の誤検知防止）。
            # 中身検査（JSONキー・Cookie構造・値パターン）は許可リスト内でも常に適用する。
            findings = ([] if is_allowlisted(fn) else name_findings(fn)) + content_findings(p)
            if findings:
                rel = os.path.relpath(p, root)
                hits.append((rel, findings))
    if hits:
        print(f"⚠ 秘密パターン検知: {len(hits)}件（走査 {total}ファイル）")
        for rel, findings in hits:
            line = f"  - {rel} → {', '.join(findings)}"
            print(line)
            _log(f"scan: ⚠ {rel} → {', '.join(findings)}")
        return 1
    print(f"✅ 検出なし（走査 {total}ファイル）")
    _log(f"scan: ✅ 検出なし（{root}・{total}ファイル）")
    return 0


def selftest() -> int:
    import tempfile
    d = tempfile.mkdtemp()
    dst_dir = os.path.join(d, "dst")
    results = []

    def t(name, cond):
        results.append((name, cond))
        print(("✅" if cond else "❌") + " " + name)

    def w(name, content):
        p = os.path.join(d, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return p

    import io, contextlib
    # 1. 許可リスト内の正常ファイルはコピーされる
    p = w("SKILL.md", "# 手順書\npython task_lock.py acquire")
    with contextlib.redirect_stdout(io.StringIO()):
        rc = cmd_copy(p, os.path.join(dst_dir, "SKILL.md"), False)
    t("許可リスト内の正常ファイルはコピー成功", rc == 0 and os.path.exists(os.path.join(dst_dir, "SKILL.md")))
    # 2. 許可リスト外はコピー拒否
    p = w("mystery_data.json", "{}")
    with contextlib.redirect_stdout(io.StringIO()):
        rc = cmd_copy(p, os.path.join(dst_dir, "mystery_data.json"), False)
    t("許可リスト外ファイルはコピー拒否", rc == 1 and not os.path.exists(os.path.join(dst_dir, "mystery_data.json")))
    # 3. 秘密ファイル名（gmail_config/x_storage）は拒否
    p1 = w("gmail_config.json", json.dumps({"gmail_address": "a@b", "app_password": "xxxx xxxx xxxx xxxx"}))
    p2 = w("x_storage_uchidokoro.json", json.dumps({"cookies": [{"name": "a", "value": "b", "domain": "x.com"}] * 3}))
    with contextlib.redirect_stdout(io.StringIO()):
        rc1 = cmd_copy(p1, os.path.join(dst_dir, "gmail_config.json"), False)
        rc2 = cmd_copy(p2, os.path.join(dst_dir, "x_storage_uchidokoro.json"), False)
    t("gmail_config/x_storageは名前で拒否", rc1 == 1 and rc2 == 1)
    # 4. 許可リスト名でも中身に秘密キーがあれば拒否（すり替え検知）
    p = w("uchidokoro_state.json", json.dumps({"pending": [], "app_password": "smuggled"}))
    with contextlib.redirect_stdout(io.StringIO()):
        rc = cmd_copy(p, os.path.join(dst_dir, "uchidokoro_state.json"), False)
    t("許可リスト名でも秘密キー入りJSONは拒否", rc == 1)
    # 4.5 リネームコピー: src=state.json → dst=uchidokoro_state.json は許可（実運用の形）
    p = w("state.json", json.dumps({"pending_recheck": [], "rotation_check": {}}))
    with contextlib.redirect_stdout(io.StringIO()):
        rc = cmd_copy(p, os.path.join(dst_dir, "uchidokoro_state.json"), False)
    t("state.json→uchidokoro_state.jsonのリネームコピーは成功", rc == 0)
    # 4.6 リネームすり替え: 秘密ファイルを許可された名前にリネームしても拒否
    with contextlib.redirect_stdout(io.StringIO()):
        rc = cmd_copy(p2, os.path.join(dst_dir, "uchidokoro_state.json"), False)
    t("x_storageを許可名にリネームしても拒否（src名で検知）", rc == 1)
    # 5. 許可リスト名でも値パターン（PAT）があれば拒否
    p = w("send_notify.py", "TOKEN = 'ghp_" + "a" * 30 + "'")
    with contextlib.redirect_stdout(io.StringIO()):
        rc = cmd_copy(p, os.path.join(dst_dir, "send_notify.py"), False)
    t("許可リスト名でも値パターン(ghp_)は拒否", rc == 1)
    # 6. Cookie構造の検知（cookiesキーが無い形でも）
    p = w("uchidokoro_state.json", json.dumps([{"name": "n", "value": "v", "domain": "d"}] * 4))
    with contextlib.redirect_stdout(io.StringIO()):
        rc = cmd_copy(p, os.path.join(dst_dir, "uchidokoro_state.json"), False)
    t("cookiesキー無しのCookie構造も拒否", rc == 1)
    # 7. 日付つきタスクログは許可
    p = w("new_machine_2026-07-16.log", "[00:00:00] STEP 0")
    with contextlib.redirect_stdout(io.StringIO()):
        rc = cmd_copy(p, os.path.join(dst_dir, "new_machine_2026-07-16.log"), False)
    t("日付つきタスクログはコピー成功", rc == 0)
    # 8. optional: src不存在はexit 0
    with contextlib.redirect_stdout(io.StringIO()):
        rc = cmd_copy(os.path.join(d, "nai.log"), os.path.join(dst_dir, "nai.log"), True)
    t("optional指定でsrc不存在はexit 0", rc == 0)
    # 9. scan: 秘密混入フォルダを検知（dstに正常ファイルのみ→0 / 混入→1）
    with contextlib.redirect_stdout(io.StringIO()):
        rc_clean = cmd_scan(dst_dir)
    shutil.copy2(p1, os.path.join(dst_dir, "gmail_config.json"))
    with contextlib.redirect_stdout(io.StringIO()):
        rc_dirty = cmd_scan(dst_dir)
    t("scanが清浄=0/混入=1を返す", rc_clean == 0 and rc_dirty == 1)
    # 10. ログ出力に秘密値そのものが含まれない
    logtxt = ""
    try:
        logtxt = open(LOG_PATH, encoding="utf-8").read()
    except Exception:
        pass
    t("ログに秘密値そのものが出ていない", "smuggled" not in logtxt and "xxxx xxxx" not in logtxt)

    # 11. ★backup-tree（gpt_research限定バックアップ・2026-07-18）★
    src_root = os.path.join(d, "gpt_research")
    os.makedirs(os.path.join(src_root, "gold_eval"), exist_ok=True)
    os.makedirs(os.path.join(src_root, "claims_check"), exist_ok=True)  # 除外対象
    os.makedirs(os.path.join(src_root, "workdir"), exist_ok=True)       # 除外対象
    open(os.path.join(src_root, "gold_set_v3.json"), "w").write('{"ok":1}')
    open(os.path.join(src_root, "gold_eval", "state.json"), "w").write('{"pending":[]}')
    open(os.path.join(src_root, "claims_check", "tmp.json"), "w").write('{"x":1}')
    open(os.path.join(src_root, "gold_eval", "leak.json"), "w").write(
        json.dumps({"app_password": "should_block"}))
    # 認可ルート外への宛先は拒否
    with contextlib.redirect_stdout(io.StringIO()) as b:
        rc_out = cmd_backup_tree(src_root, os.path.join(d, "outside_dropbox"))
    t("backup-tree: 認可ルート外の宛先を拒否", rc_out == 2)
    # 認可ルート配下を一時的に模してinclude/exclude/秘密検知を確認
    global DROPBOX_ROOT_ALLOWED
    orig_root = DROPBOX_ROOT_ALLOWED
    DROPBOX_ROOT_ALLOWED = d
    try:
        dst_root = os.path.join(d, "dropbox_dst")
        with contextlib.redirect_stdout(io.StringIO()) as b:
            rc_bt = cmd_backup_tree(src_root, dst_root)
        out = b.getvalue()
    finally:
        DROPBOX_ROOT_ALLOWED = orig_root
    copied_ok = os.path.exists(os.path.join(dst_root, "gold_set_v3.json")) \
        and os.path.exists(os.path.join(dst_root, "gold_eval", "state.json"))
    excluded_ok = not os.path.exists(os.path.join(dst_root, "claims_check", "tmp.json"))
    leak_blocked = not os.path.exists(os.path.join(dst_root, "gold_eval", "leak.json"))
    t("backup-tree: 許可対象コピー・除外dir無視・秘密混入は拒否",
      copied_ok and excluded_ok and leak_blocked and rc_bt == 1)

    ok = all(c for _, c in results)
    print(f"\nselftest: {sum(1 for _, c in results if c)}/{len(results)} 合格")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Dropboxバックアップの秘密情報ガード")
    parser.add_argument("command", nargs="?", choices=["copy", "scan", "backup-tree"])
    parser.add_argument("src", nargs="?")
    parser.add_argument("dst", nargs="?")
    parser.add_argument("--dir", help="scan: 走査対象ディレクトリ")
    parser.add_argument("--optional", action="store_true", help="copy: src不存在をエラーにしない")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()

    if args.selftest:
        return selftest()
    if args.command == "backup-tree":
        if not args.src or not args.dst:
            parser.error("backup-tree には src(gpt_researchルート) と dst(Dropbox宛先) が必要")
        return cmd_backup_tree(args.src, args.dst)
    if args.command == "copy":
        if not args.src or not args.dst:
            parser.error("copy には <src> <dst> が必要")
        return cmd_copy(args.src, args.dst, args.optional)
    if args.command == "scan":
        if not args.dir:
            parser.error("scan には --dir が必要")
        return cmd_scan(args.dir)
    parser.error("コマンドを指定（copy/scan か --selftest）")
    return 2


if __name__ == "__main__":
    sys.exit(main())

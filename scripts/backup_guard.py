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

ログ: （書類フォルダ）/uchidokoro/logs/backup_guard.log
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

import os as _os_lp                 # noqa: E402
import sys as _sys_lp               # noqa: E402
_sys_lp.path.insert(0, _os_lp.path.dirname(_os_lp.path.abspath(__file__)))
import local_paths as _lp           # noqa: E402
LOG_PATH = _lp.doc("logs/backup_guard.log")

# ── 前段copy用: バックアップを許可するファイル名（完全一覧・basename照合）──
ALLOW_BASENAMES = {
    # ★プロジェクトのルール（2026-07-31 追加）★
    #   中身はルール・現在の状態・パスだけで、認証情報を含まない。
    #   ★ここが失われると「毎回忘れる」ルールごと消える★ので必ず控えを取る。
    #   保存名は uchidokoro_ を付けて他プロジェクトと混ざらないようにする。
    "uchidokoro_CLAUDE.md",
    "uchidokoro_CLAUDE_history.md",
    # 要確認台帳（人間の判断待ち案件の唯一の恒久記録・台帳 #153）
    "open_issues.json",
    # ★台帳を越えた修正の記録★（2026-08-04・Codex84回目）
    #   運営者の承認で例外的に直した記録。失うと「なぜ直したか」が消える。
    "manual_overrides.json",
    # ★機種ごとの出典URLの控え★（2026-08-07・台帳#265）
    #   AIが探して機械が確かめた結果。失うと同じ調べ直しをやり直すことになる。
    #   中身は公開サイトのURLと判断理由だけで、認証情報を含まない。
    "uchidokoro_machine_sources.json",
    # ★★ここに confirmed_values を足さないこと★★（2026-08-23・台帳#464）
    #   ★2026-08-09から既に下（96行あたり）に載っている★。
    #   私は「名簿に無いから拒否された」と誤解して重複追加したが、
    #   ★集合なので何の効果も無かった★。
    #   ★本当の原因★＝無人タスクが保存先を `confirmed_values.json` にしていた。
    #   名簿は**保存名**で照合するので、`uchidokoro_` が無ければ当然落ちる。
    #   → 直したのは手順書（保存名に uchidokoro_ を付ける）と、
    #     拒否の文言（似た名前が許可されていればその名前を出す）。
    # ★見つけたが、まだ記事にできていない新台の控え★（2026-08-16・台帳#376）
    #   ★ここが失われると「見つけたのに記事にしていない機種」が丸ごと消える★
    #   （どの機種を見たかの記憶がなくなり、二度と出てこない）。
    #   中身は機種名・公開ページのURL・待った日数だけで、認証情報を含まない。
    "uchidokoro_add_machine_pending.json",
    # ★どのタスクが動いていて、どれを止めたかの契約★（2026-09-01追加）
    #   ★これが失われると監査37が「契約が無い」として黙って通る★
    #   ＝見張りが静かに消える。中身はタスク名とスキル名だけ。
    "tasks-contract.json",
    # 自動タスクの手順書（{taskId}_SKILL.md の形で保存する）
    "uchidokoro-fact-check_SKILL.md",
    "SKILL.md",
    "send_notify.py",
    # ★Codexの呼び出し口★（2026-08-07。リポジトリ外にあり、ここが壊れると
    #   2AIの突き合わせが丸ごと止まる。利用制限の検知もここに入っている）
    "codex_review.sh",
    # ★新台の待ち行列★（2026-08-11・台帳#271）
    #   「メーカー公式で見つけたが、まだ記事にできていない新台」の唯一の記録。
    #   公式URLは一度見たら既知として記録されるので、この控えを失うと
    #   **見つけた新台がもう二度と出てこない**（早く見つけた機種ほど消える）。
    #   中身は公式URLと機種名・理由だけで、認証情報を含まない。
    "add_machine_pending.json",
    # ★運営者が確認した登場年月の控え★（2026-08-10）
    #   公式が画像や「発売」表記でしか書かない機種の唯一の逃げ道。
    #   失うと、その機種は永久に公開できなくなる（機械では読めないため）。
    "release_overrides.json",
    # ★2AIで確定した値★（2026-08-09・台帳#273）
    #   ClaudeとCodexが同じ原文を読んで一致し、機械が出典を確かめた結果。
    #   失うと同じ突き合わせをやり直すことになる（Codexの回数も消費する）。
    #   中身は公開サイトのURLと逐語引用だけで、認証情報を含まない。
    "uchidokoro_confirmed_values.json",
    # ★機種ごとのメーカー同一性の控え★（2026-08-14・台帳#340）
    #   「この機種について、この2つのメーカー表記は同じか」を
    #   ClaudeとCodexが公式と名鑑を読んで決めた結果。
    #   失うと同じ突き合わせをやり直すことになる（Codexの回数も消費する）。
    #   中身は公開サイトのURLと逐語引用だけで、認証情報を含まない。
    "uchidokoro_maker_identity_cache.json",
    # ★全タスク共通のログ書き込み口★（2026-08-09）
    #   リポジトリ外にあり、うちどころ・わんさかんさい・番兵の全タスクが使う。
    #   2026-08-09に --message-file / --stdin を足した（標準入力を読まず
    #   「-」の1文字を書くだけで本文を捨てていたため）。
    "log.py",
    # ★シェル差し込みガード★（2026-08-09・依頼126）
    #   リポジトリ外（~/.claude）にあり、これが失われると
    #   「文章を書いただけでコマンドが実行される」経路が丸ごと開く。
    "shell_guard.py",
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
# ★出典の裏取り証拠（claims）★（2026-08-03・台帳#202）
#   命名は {slug}_{yyyymmdd}_{種別}.json（例: milliongod_kiseki_20260803_maker.json）。
#   verify_claims の全関門通過を記録した唯一の裏取り証拠で、失うと再検証が要る。
#   認証情報系（x_storage_*.json / gmail_config.json）は8桁日付の区切りが無いため
#   この形には一致しない（selftestで凍結）。
ALLOW_CLAIMS_RE = re.compile(
    r"^[a-z0-9_]+_(?:\d{8}_[a-z0-9_]+|\d{4}-\d{2}-\d{2})\.json$")
# 自動タスクの手順書（規約どおりの `{taskId}_SKILL.md`。2026-07-28に一般化）
#   個別に列挙する方式だと、新しいタスクの手順書が黙ってバックアップされない
#   （task-watchdog_SKILL.md が実際に拒否されていた）
ALLOW_TASK_SKILL_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{1,60}_SKILL\.md$")

# CLAUDE.md の日付つきスナップショット（圧縮など破壊的編集の前に退避する用途・2026-07-24追加）
# 例: CLAUDE_uchidokoro_2026-07-24.md / CLAUDE_history_uchidokoro_2026-07-24.md
# 通常のバックアップ名（CLAUDE_uchidokoro.md）を上書きせずに世代を残すために許可する。
# ★圧縮前の控えも同じ枠で許す★（2026-08-04。実際の運用では
#   CLAUDE_uchidokoro_precompress_2026-07-23.md のような名前を使ってきたが、
#   この形が許可の形から漏れていて、圧縮前のバックアップが取れなかった。
#   同じ日に2回取る場合の末尾1文字（…-04b.md）も許す）
ALLOW_CLAUDE_SNAPSHOT_RE = re.compile(
    r"^CLAUDE(_history)?_uchidokoro(_precompress)?_"
    r"\d{4}-\d{2}-\d{2}[a-z]?\.md$")

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

# JSONの配列を確かめる上限（超えたら「確かめられない」として拒否する）
JSON_SCAN_LIMIT = 5000

# ── 秘密パターン: JSONキー（大小文字・-/_ 無視・再帰）──
DENY_JSON_KEYS = {
    "app_password", "password", "passwd", "client_secret", "private_key",
    "access_token", "refresh_token", "id_token", "auth_token", "authorization",
    "bearer", "api_key", "apikey", "cookies", "storage_state",
    "sessionid", "session_id", "csrf", "xsrf", "smtp_password",
}
# 単独では一般的すぎる語（secret/token/cookie/session）はキー名の完全一致のみ
DENY_JSON_KEYS_EXACT = {"secret", "token", "cookie", "session"}

# 本文から「鍵の名前らしい書き方」を拾う（"app_password": / app_password= など）
# 本文から「鍵に値を入れている書き方」を拾う（2026-08-04・Codex89〜90回目）
#   ★形式（カッコ始まり）で判断すると [00:00:00] のタスクログが通らなくなり、
#     鍵の名前だけで判断すると設計メモのコード片（token: str）で誤検知する。
#     そこで「鍵に**それらしい値**を入れている書き方」に絞る。
#   ①引用符つき: 同じ引用符で閉じるまでを値とする（中のアポストロフィや
#     エスケープで検査から逃げられないように）
#   ②引用符なし: 記号と英数字だけの塊（日本語の説明文や `token: str` は当たらない）
_KEYLIKE_QUOTED = re.compile(
    r"[\"']?(?P<key>[A-Za-z_][A-Za-z0-9_-]{2,40})[\"']?"
    r"\s*[:=]\s*"
    r"(?P<q>[\"'])(?P<val>(?:\\.|(?!(?P=q)).){8,}?)(?P=q)")
_KEYLIKE_BARE = re.compile(
    r"(?<![A-Za-z0-9_])(?P<key>[A-Za-z_][A-Za-z0-9_-]{2,40})"
    r"\s*[:=]\s*"
    r"(?P<val>[A-Za-z0-9_+/=.\-]{8,})(?![A-Za-z0-9_+/=.\-]*[(\[])")

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
        # ★配列の途中で検査をやめない★（2026-08-04・Codex87回目）
        #   先頭50件だけ見ていたので、51件目に app_password を置けば
        #   20MB未満の正しいJSONでも素通りできた。
        #   数が多すぎる時は「確かめられない」として拒否する（fail-closed）。
        if len(obj) > JSON_SCAN_LIMIT:
            out.append(f"json:要素が多すぎて確かめられません（{len(obj)}件）")
            return out
        for i, x in enumerate(obj):
            out.extend(_json_key_findings(x, f"{path}[{i}]"))
    return out


def _decode_utf16(raw: bytes):
    """BOM付きUTF-16なら文字列にする（そうでなければ None）。"""
    for bom, enc in ((bytes([0xff, 0xfe]), "utf-16-le"),
                     (bytes([0xfe, 0xff]), "utf-16-be")):
        if raw.startswith(bom):
            try:
                return raw[2:].decode(enc)
            except UnicodeDecodeError:
                return None
    return None


def _is_archive(name: str, data: bytes) -> bool:
    """圧縮ファイルか（★名前・先頭の印・中身の3つで見る★）。"""
    import io as _io
    import zipfile as _zf
    if name.lower().endswith((".zip", ".gz", ".7z", ".rar", ".tgz", ".jar",
                              ".whl", ".xz", ".bz2")):
        return True
    for mg in (b"PK" + bytes([3, 4]), bytes([0x1f, 0x8b]),
               b"7z" + bytes([0xbc, 0xaf, 0x27, 0x1c]), b"Rar!"):
        if data.startswith(mg):
            return True
    try:                                  # 前置きデータ付きでも見つける
        return _zf.is_zipfile(_io.BytesIO(data))
    except Exception:                     # noqa: BLE001
        return False


def _looks_binary(name: str) -> bool:
    return name.lower().endswith(
        (".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
         ".zip", ".gz", ".7z", ".woff", ".woff2", ".ttf", ".mp4"))


def _zip_findings(path: str, raw: bytes, depth: int = 0):
    """ZIPなら中の文字ファイルを1件ずつ同じ検査に掛ける（ZIPでなければ None）。

    ★中を見ないまま拒否し続けない★（2026-08-06）
      拒否のままだと、Dropboxに置いた控えの中身を誰も確かめられず、
      毎朝の警告だけが積み上がる。
    """
    import tempfile
    import zipfile
    if not raw.startswith(b"PK" + bytes([3, 4])):
        return None
    if depth > 0:
        return ["content:ZIPの中にZIPがあるので確かめられません"]
    # ★見つけた秘密を、あとから来た「確かめられません」で捨てない★
    #   （2026-09-04・Codexの指摘）＝先に鍵を見つけていても、
    #   後続の入れ子ZIPや読めない要素で単独の理由を return していたので、
    #   ★何が見つかったかが運営者に届かなかった★。
    #   （どちらにせよ非0で止まるが、理由が消えると原因を追えない）
    out = []
    try:
        with zipfile.ZipFile(path) as z:
            # ★名前ではなく実体で回す★（2026-08-06・Codex122回目の指摘1）
            #   ZIPには同じ名前の中身を複数入れられる。名前で読むと
            #   **同名の1件しか読めず、残りを検査しないまま通していた**。
            infos = [zi for zi in z.infolist() if not zi.filename.endswith("/")]
            if len(infos) > 500:
                out.append(f"content:ZIPの中身が多すぎます（{len(infos)}件）")
                return out
            total = 0
            for zi in infos:
                nm = zi.filename
                total += zi.file_size
                if total > 20 * 1024 * 1024:
                    out.append("content:ZIPの中身が大きすぎて確かめられません")
                    return out
                data = z.read(zi)
                # ★中の圧縮ファイルは確かめられない★（見た目で飛ばさない）
                #   拡張子で「binaryだから無視」にすると、
                #   **ZIPの中にZIPを置けば中身を隠せた**（自分の試験で発覚）。
                #   ★先頭の印だけで見ない★（2026-08-06・Codex122回目の指摘2）
                #     ZIPは前に別のデータを付けても成立する。
                #     先頭一致だけだと「前置き＋ZIP」が素通りしていた。
                if _is_archive(nm, data):
                    out.append("content:ZIPの中にZIPがあるので確かめられません"
                               f"（{nm}）")
                    return out
                txt = None
                try:
                    txt = data.decode("utf-8")
                except UnicodeDecodeError:
                    txt = _decode_utf16(data)
                if txt is None:
                    if _looks_binary(nm):
                        continue
                    return [f"content:ZIP内 {nm} を読めないので確かめられません"]
                fd, tp = tempfile.mkstemp(suffix="_" + os.path.basename(nm))
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(txt)
                    out += [f"{nm} → {x}" for x in content_findings(tp)]
                finally:
                    os.remove(tp)
    except zipfile.BadZipFile:
        out.append("content:壊れたZIPなので確かめられません")
        return out
    return out


def content_findings(path: str) -> list[str]:
    """中身ベースの検知（JSONキー・Cookie構造・値パターン）。ルール名のみ返す。"""
    out = []
    # ★確かめられなかったものは通さない★（2026-08-04・Codex86回目）
    #   以前は「大きすぎる」「読めない」を空の結果で返していたので、
    #   20MB超のファイルに app_password を入れれば素通りできた。
    #   検査できない＝安全とは言えない、で統一する（fail-closed）。
    #   ※実運用の対象は最大2MB程度なので、拒否しても支障はない。
    try:
        size = os.path.getsize(path)
        if size > 20 * 1024 * 1024:
            return ["content:大きすぎて中身を確かめられません"
                    f"（{size // (1024 * 1024)}MB）"]
        with open(path, "rb") as f:
            raw = f.read()
        # ★読めない文字コードのまま素通りさせない★（2026-08-04・Codex87回目）
        #   errors="ignore" だと、UTF-16 で保存した .md/.py/.log は
        #   文字の間にNULが入って正規表現に当たらず、秘密が通っていた。
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # ★読める形なら、実際に読んで確かめる★（2026-08-06）
            #   Windowsのタスク定義は UTF-16、レビューの控えは ZIP。
            #   「読めないから拒否」だと毎朝の警告が積み上がるだけで、
            #   中身は一度も確かめられない。読めるようにするほうが安全。
            text = _decode_utf16(raw)
            _from_utf16 = text is not None
            if text is None:
                zf = _zip_findings(path, raw)
                if zf is not None:
                    return zf
                return ["content:UTF-8として読めないので中身を確かめられません"]
    except Exception as e:                # noqa: BLE001
        return [f"content:読めないので確かめられません（{type(e).__name__}）"]
    # ★中身がJSONなら、拡張子に関係なく鍵を確かめる★（2026-08-04・Codex88回目）
    #   .json の時しか見ていなかったので、許可対象の .md に
    #   {"app_password": "..."} と書けば素通りできた。
    #   ★NUL・圧縮ファイルの先頭印もここで拒否する★（テキストの皮をかぶった別物）
    # ★UTF-16で読めたものはNULがあって当たり前★（2026-08-06）
    #   文字の間にNULが入るので、生バイトで見ると必ず引っかかっていた。
    if bytes([0]) in raw and not locals().get("_from_utf16"):
        return ["content:テキストではありません（NULが入っています）"]
    for magic, name in (() if locals().get("_from_utf16") else
                        ((b"PK" + bytes([3, 4]), "zip"),
                        (bytes([0x1f, 0x8b]), "gzip"),
                        (b"7z" + bytes([0xbc, 0xaf, 0x27, 0x1c]), "7z"),
                        (b"Rar!", "rar"))):
        if raw.startswith(magic):
            return [f"content:圧縮ファイルの中身です（{name}）"]
    _txt = text.strip()
    _parsed = None
    try:
        _parsed = json.loads(_txt) if _txt[:1] in ("{", "[") else None
    except Exception:                     # noqa: BLE001
        _parsed = "BROKEN"
    if _parsed not in (None, "BROKEN"):
        out.extend(_json_key_findings(_parsed))
    if path.lower().endswith(".json") and _parsed in (None, "BROKEN"):
        # ★.json は必ず読めること★（読めない＝中身を確かめられない）
        out.append("json:壊れていて中身を確かめられません（JSONとして読めません）")
    # ★秘密の鍵の名前は、JSONとして読めなくても本文から探す★
    #   （2026-08-04・Codex89回目。壊れたJSONを .md に書いて隠せた。
    #     一方で「[00:00:00] のログはJSONではない」ので、
    #     カッコ始まりを一律に拒否すると本物のログが通らなくなる＝
    #     形式ではなく**鍵の名前**で見る）
    for _re9 in (_KEYLIKE_QUOTED, _KEYLIKE_BARE):
        for _m in _re9.finditer(text):
            _k = _norm_key(_m.group("key"))
            if _k in DENY_JSON_KEYS or _k in DENY_JSON_KEYS_EXACT:
                out.append(f"text_key:{_m.group('key')}")
    for rule, pat in DENY_VALUE_PATTERNS:
        if pat.search(text):
            out.append(f"value:{rule}")
    return out


# ── 設計メモ（_design/ 配下）のバックアップ許可（2026-07-28追加）──
#   `_design/` は .gitignore 対象なので、**Dropboxが唯一の保全先**。
#   PC故障で Phase の終了条件や設計判断が失われるのを防ぐ。
#   ★許可するのは「_design/ 配下の .md」だけ★（作業用JSONは巨大かつ再生成可能なので除く）
#   秘密パターンの検査は従来どおり全部に掛かる。
DESIGN_DIR_NAME = "_design"
ALLOW_DESIGN_EXT = {".md"}


def is_design_doc(src: str) -> bool:
    """コピー元が `_design/` 配下の設計メモ（.md）か。"""
    parent = os.path.basename(os.path.dirname(os.path.abspath(src)))
    return (parent == DESIGN_DIR_NAME
            and os.path.splitext(src.lower())[1] in ALLOW_DESIGN_EXT)


# ★claims の証拠は「置き場」も込みで許す★（2026-08-04・Codex83回目の指摘6）
#   名前の形だけで許していたので、別の場所にある同じ形のファイル
#   （anything_20260803_export.json など）も通ってしまった。
CLAIMS_DIR = os.path.join(os.path.expanduser("~"), "Documents", "uchidokoro",
                          "claims")


def _in_claims_dir(src: str | None) -> bool:
    if not src:
        return False
    try:
        d = os.path.normcase(os.path.abspath(os.path.dirname(src)))
        return d == os.path.normcase(os.path.abspath(CLAIMS_DIR))
    except Exception:                     # noqa: BLE001
        return False


def is_allowlisted(basename: str, src: str | None = None) -> bool:
    if (basename in ALLOW_BASENAMES
            or bool(ALLOW_LOG_RE.match(basename))
            or bool(ALLOW_CLAUDE_SNAPSHOT_RE.match(basename))
            or bool(ALLOW_TASK_SKILL_RE.match(basename))):
        return True
    # claims は「名前の形」と「claims置き場にあること」の両方が要る
    if ALLOW_CLAIMS_RE.match(basename) and _in_claims_dir(src):
        return True
    return bool(src) and is_design_doc(src)


def cmd_copy(src: str, dst: str, optional: bool) -> int:
    # ★★行き先は絶対パスで書く★★（2026-08-21・実際に間違えた）
    #   相対パスを渡すと**いま居るところ**の下に作られる。
    #   実際、Dropboxへ入れたつもりのSKILL.mdが
    #   ★リポジトリの中に Claude_backup/ として出来ていた★
    #   （気づかなければ、そのままコミットされて公開リポジトリに載る）。
    #   ★バックアップは「別の場所へ置く」のが目的★なので、
    #   行き先が曖昧なまま実行させない（fail-closed）。
    if not os.path.isabs(dst):
        _log(f"copy: 行き先が絶対パスではありません: {dst}")
        print("DST_NOT_ABSOLUTE")
        print("★行き先は絶対パスで書いてください★"
              "（相対パスだと、いま居るところの下に作られます。"
              "実際にリポジトリの中へバックアップが出来た事故があります）")
        return 2
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
    if not is_allowlisted(dst_base, src):
        # ★★似た名前が許可されているなら、それを言う★★（2026-08-23・台帳#464）
        #   ★なぜ要るか★＝実際に一晩無駄にした。
        #   無人タスクが保存先を `confirmed_values.json` にして拒否されたが、
        #   許可名簿にあるのは `uchidokoro_confirmed_values.json` だった。
        #   「リスト外」としか出ないので、**名簿に足りないのだと誤解して
        #   すでに載っている名前を重複追加する**という無意味な直しをした。
        #   ★機械が知っていることを、その場で言えば済む★
        hint = ""
        for allowed in ALLOW_BASENAMES:
            if allowed.endswith(dst_base) and allowed != dst_base:
                hint = f"／★保存名を「{allowed}」にしてください★"
                break
        findings.append("allowlist:リスト外" + hint)
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
DROPBOX_ROOT_ALLOWED = _lp.DROPBOX
# ★同定の根拠（一覧カードのHTML）も退避する★（2026-08-21・台帳#224）
#   置き場は Documents/uchidokoro/identity_evidence/ の1か所だけだった。
#   ★端末やディスクが壊れると、全部の証跡を失う★
#   （どの機種をどのページで同定したか、の唯一の記録）。
#   中身は公開サイトのHTMLで、秘密は含まない
#   （それでも下の秘密検査は全ファイルに掛かる）。
# ★直しの実行記録も退避する★（2026-08-22・台帳#458）
#   置き場は Documents/uchidokoro/repairs/（repair_journal.py）。
#   ★端末が壊れると、途中まで進んだ直しが全部消える★＝
#   どの段階まで行ったか（判定に封をした・Codexの返事を受けた・
#   直した・pushした・再検査した）が唯一ここにしか無い。
#   中身は機種名・逐語・指紋・コミットIDで、秘密は含まない
#   （それでも下の秘密検査は全ファイルに掛かる）。
TREE_INCLUDE_DIRS = {"gold_eval", "results", "input_snapshot",
                     "identity_evidence", "repairs"}
TREE_INCLUDE_GLOBS = ["gold_set_v*.json", "codex_schema_*.json",
                      "gold_freeze_log*.txt", "shadow_state.json",
                      # ★2AIの判断記録★（2026-08-11・台帳#317）
                      #   ClaudeとCodexが同じ原文を読んで何を採り、何を保留に
                      #   したかの唯一の記録。失うと同じ突き合わせをやり直す
                      #   ことになり、Codexの回数も消費する。
                      #   中身は公開サイトのURLと逐語引用だけで秘密を含まない。
                      "*_20??-??-??.md"]
TREE_EXCLUDE_DIRS = {"claims_check", "workdir", ".codex", ".claude", "notify_body.txt"}
TREE_EXCLUDE_EXT = {".tmp"}


def _under(path: str, root: str) -> bool:
    # ★上限が決まっていなければ、何も許さない★（2026-08-14・依頼185のP0）
    #   以前は root が空だと abspath("") ＝**いまの作業フォルダ**になり、
    #   その配下ならDropboxとして許してしまった。
    #   置き場が見つからないときは「断る」が正しい（fail-closed）。
    if not str(root or "").strip():
        return False
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


def cmd_backup_design(src_dir: str, dst_dir: str) -> int:
    """`_design/` の設計メモ（.md）を丸ごとDropboxへ保全する。

    `_design/` は .gitignore 対象なので、Dropboxが唯一の保全先。
    1件でも拒否されたら非0で終える（黙って一部だけ保全しない）。
    """
    if not os.path.isdir(src_dir):
        print("SRC_MISSING", src_dir)
        return 2
    if not _under(dst_dir, DROPBOX_ROOT_ALLOWED):
        print("DST_OUT_OF_ROOT", dst_dir)
        return 2
    ok = ng = 0
    for name in sorted(os.listdir(src_dir)):
        src = os.path.join(src_dir, name)
        if not os.path.isfile(src) or not is_design_doc(src):
            continue
        rc = cmd_copy(src, os.path.join(dst_dir, name), False)
        if rc == 0:
            ok += 1
        else:
            ng += 1
    print(f"DESIGN_BACKUP ok={ok} ng={ng}")
    _log(f"backup-design: ok={ok} ng={ng} → {dst_dir}")
    return 0 if ng == 0 else 1


# ★★運営者が「これは承知の上」と決めたもの★★（2026-08-22）
#   ★なぜ要るか★＝見張りの範囲を広げたら、うちどころ以外のプロジェクトの
#   バックアップから16件が出た。運営者の判断は
#   ★「Dropboxは安全とみなす。触らない」★（2026-08-22）。
#   ★このままだと毎朝🟠が出続ける★＝「静かなのが正常」が崩れ、
#   本物の警告が埋もれる。＝**承知しているものは基準値に置き、
#   新しく増えた分だけ知らせる**。
#   ★消すのではなく、記録して黙らせる★（検知そのものは続ける）。
BASELINE = os.path.join(os.path.expanduser("~"), "Documents", "uchidokoro",
                        "backup_scan_baseline.json")


def _load_baseline() -> dict:
    """承知済みの一覧を読む。★読めなければ「無い」ではなく止める★"""
    if not os.path.exists(BASELINE):
        return {"schema": "backup-scan-baseline/v2", "accepted": {}}
    import json as _j
    with open(BASELINE, encoding="utf-8") as f:
        got = _j.load(f)
    if not isinstance(got, dict) or "accepted" not in got:
        raise SystemExit("★基準値の形が違います★: " + BASELINE)
    return got


# ★「秘密を見つけた」ではなく「確かめられなかった」印★（2026-08-22）
#   中身を読めていないので、指紋が変わっても「秘密が変わった」とは言えない。
#   ★書き足されるログは毎回指紋が変わる★ので、ここを分けないと永久に鳴る。
# ★★「中身を見られなかった」検知の一覧★★（2026-09-04・Codexの指摘3）
#   ★直す前★＝「確かめられません」という**語**の部分一致で判定していた。
#   将来この語を含む**本物の検知**を足すと、静かにこちら側へ落ちる。
#   → 文言ではなく、決まった書き出し（符丁）で判定する。
#   ★新しい「読めない」検知を足すときは、ここにも足すこと★
#     （足し忘れると指紋を比べる側に回るだけで、止まらなくはならない＝安全側）。
_UNVERIFIABLE_STEMS = (
    "content:UTF-8として読めないので",
    "content:読めないので確かめられません",
    "content:大きすぎて中身を確かめられません",
    "content:ZIP内 ",
    "content:ZIPの中にZIPがあるので",
    "content:ZIPの中身が大きすぎて",
    "content:ZIPの中身が多すぎます",
    "content:壊れたZIPなので",
)


def _is_unverifiable(finding: str) -> bool:
    """★中身を見られなかった、という報告か★（秘密を見つけた報告ではない）。

    ★これは終了コードを変えない★＝どちらでも `fresh` に入って止まる。
    効くのは「指紋を比べるかどうか」だけ（中身を見られていないものは
    指紋が毎回変わるので、比べると永久に鳴り続ける）。
    """
    return str(finding or "").startswith(_UNVERIFIABLE_STEMS)


def _sha_file(path: str) -> str:
    """★中身が変わったかを見るための指紋★（中身そのものは残さない）"""
    import hashlib
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _walk(root: str, bad: list):
    """★入れなかったフォルダを黙って飛ばさない★（2026-08-22・Codexの指摘）"""
    def _oops(e):
        bad.append(f"読めないフォルダがあります: {getattr(e, 'filename', '?')}")
    return os.walk(root, onerror=_oops)


def _root_key(root: str) -> str:
    """走査ルートをそろえた形にする（別の場所の基準値を流用させない）。"""
    return os.path.normcase(os.path.abspath(root)).replace(os.sep, "/")


def cmd_scan(root: str) -> int:
    # ★走査先が本当にあるか★（2026-08-22・Codexの指摘）
    #   ★直す前★＝存在しない場所でも0ファイル走査で「検知なし」終了コード0。
    #   ＝**見ていないのに緑**という、いちばん危ない返し方。
    if not os.path.isdir(root):
        print(f"★走査先がありません: {root}★")
        _log(f"scan: ★走査先がありません: {root}★")
        return 1
    total = 0
    hits = []
    walk_ng = []
    for dirpath, _dirs, files in _walk(root, walk_ng):
        for fn in files:
            total += 1
            p = os.path.join(dirpath, fn)
            # 許可リスト内の既知ファイル名は名前ルールを免除（refresh_x_cookies.py等の誤検知防止）。
            # 中身検査（JSONキー・Cookie構造・値パターン）は許可リスト内でも常に適用する。
            findings = ([] if is_allowlisted(fn) else name_findings(fn)) + content_findings(p)
            if findings:
                rel = os.path.relpath(p, root)
                hits.append((rel, findings, _sha_file(p)))
    if walk_ng:
        # ★★調べられなかったフォルダがあるなら緑にしない★★（2026-09-04）
        #   ★直す前★＝印刷とログだけで先へ進み、他に検知が無ければ0を返した。
        #   「調べていない場所に秘密がある」経路をそのまま通していた。
        #   `cmd_accept` は同じ状況で基準値を作らないのに、こちらだけ素通り。
        for w in walk_ng:
            print("  ★" + w)
            _log("scan: ★" + w)
        print(f"★調べられなかったフォルダが {len(walk_ng)} 件あるので緑にしません★")
        _log(f"scan: ★読めないフォルダ {len(walk_ng)} 件のため非0★")
        return 1
    got = _load_baseline()
    # ★★別の場所の基準値を流用させない★★（2026-08-22・Codexの指摘）
    want_root = str(got.get("root") or "")
    if want_root and want_root != _root_key(root):
        # ★別の場所を調べるのは正当な操作★なので断らない。
        #   ★ただし基準値は使わない★＝そこで承知したものではないため。
        #   （断ってしまうと、別の場所を調べること自体ができなくなる）
        print("★基準値は別の場所のものなので使いません★")
        _log("scan: 基準値の走査ルートが違うので使わない")
        base = {}
    else:
        base = got.get("accepted") or {}
    # ★同じ場所でも、検知の中身が増えていたら新しい扱い★
    #   （承知したのは「そのとき見えていたもの」であって、
    #     あとから足された秘密まで承知したことにはならない）
    known, fresh = [], []
    for rel, findings, sha in hits:
        want = base.get(rel.replace(os.sep, "/"))
        # ★★中身が変わっていたら、検知の種類が同じでも新しい扱い★★
        #   （2026-08-22・Codexの指摘）
        #   ★直す前★＝種類の集合だけを見ていたので、
        #     ①古いトークンを新しいトークンへ差し替える
        #     ②同じファイルにトークンをもう1個足す
        #   のどちらも「承知済み」のまま素通りした（種類は同じだから）。
        # ★★「確かめられなかった」だけの検知は、指紋を見ない★★
        #   （2026-08-22・対照実験で分かった）
        #   ★何が起きたか★＝`delete_guard.log` のような**書き足されるログ**は
        #   毎回指紋が変わるので、永久に「新しい検知」になり続けた。
        #   ＝★消そうとしたノイズを、自分で作り直していた★。
        #   ★そもそも中身を見られていない★ので、指紋を比べる意味がない。
        #   （本当に秘密を見つけた検知だけ、中身が変わったら知らせる）
        unverifiable = all(_is_unverifiable(x) for x in findings)
        ok = (want is not None
              and set(findings) <= set(want.get("findings") or [])
              and (unverifiable
                   or str(want.get("sha256") or "") == sha))
        # ★★「確かめられなかった」ものを通さない★★（2026-09-04に戻した）
        #   ★一度「止めない」にして、自分で5通りの穴を作った★＝
        #   `report.pdf` の中身が不正UTF-8で始まり本物の鍵を含む／
        #   20MB超のテキストに鍵／ZIPで鍵を見つけた後に読めない要素／
        #   読めないフォルダ／未検査があっても緑表示。
        #   ★検査できない＝安全とは言えない★（fail-closed）を守る。
        #   ★騒がしさは番人の側で分類済み★＝「名前つき検知」と
        #   「読めない件数」を分けて報告している。こちらを緩める必要は無い。
        (known if ok else fresh).append((rel, findings))
    if fresh:
        print(f"⚠ 秘密パターン検知: {len(fresh)}件"
              f"（走査 {total}ファイル／承知済み {len(known)}件は除く）")
        for rel, findings in fresh:
            line = f"  - {rel} → {', '.join(findings)}"
            print(line)
            _log(f"scan: ⚠ {rel} → {', '.join(findings)}")
        return 1
    print(f"✅ 新しい検知なし（走査 {total}ファイル／承知済み {len(known)}件）")
    _log(f"scan: ✅ 新しい検知なし（{root}・{total}ファイル・承知済み{len(known)}件）")
    return 0


def cmd_accept(root: str) -> int:
    """★いま出ている検知を「承知済み」として記録する★（2026-08-22）

    ★運営者が判断したときだけ実行する★（無人タスクからは呼ばない）。
    記録するのは**場所と検知の種類だけ**＝★中身は書かない★。
    """
    import json as _j
    if not os.path.isdir(root):
        print(f"★走査先がありません: {root}★")
        return 1
    total = 0
    acc = {}
    walk_ng = []
    for dirpath, _dirs, files in _walk(root, walk_ng):
        for fn in files:
            total += 1
            p = os.path.join(dirpath, fn)
            findings = ([] if is_allowlisted(fn) else name_findings(fn)) + content_findings(p)
            if findings:
                rel = os.path.relpath(p, root).replace(os.sep, "/")
                acc[rel] = {"findings": sorted(set(findings)),
                            "sha256": _sha_file(p)}
    if walk_ng:
        # ★読めないフォルダがあるまま基準値を作らない★（見えていない分を承知できない）
        for w in walk_ng:
            print("  ★" + w)
        print("★読めないフォルダがあるので基準値を作りません★")
        return 1
    got = {"schema": "backup-scan-baseline/v2",
           "root": _root_key(root),
           # ★いつ承知したか★＝理由の文に日付を焼き込むと、
           #   やり直しても古い日付のまま残って確かめられない。
           "accepted_at": datetime.date.today().isoformat(),
           "why": "運営者の判断（2026-08-22）＝Dropboxは安全とみなす。"
                  "うちどころ以外のプロジェクトの控えに元からあったもので、"
                  "新しい漏れではない。★消さずに記録して黙らせる★",
           "decided_by": "運営者",
           "accepted": acc}
    os.makedirs(os.path.dirname(BASELINE), exist_ok=True)
    with open(BASELINE, "w", encoding="utf-8", newline="\n") as f:
        _j.dump(got, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    print(f"承知済みとして記録: {len(acc)}件（走査 {total}ファイル）→ {BASELINE}")
    _log(f"accept: {len(acc)}件を承知済みとして記録")
    return 0


def _baseline_tests(t) -> None:
    """★承知済みの仕組みの試験★（2026-08-22・Codexが挙げた穴を全部当てる）

    ★なぜ要るか★＝「承知済みで黙らせる」は、間違えると
    ★本当に危ないものを見逃す★方向に働く。実データで1回試すだけでは足りない。
    """
    import tempfile as _tf
    import shutil as _sh

    keep = globals()["BASELINE"]
    d = _tf.mkdtemp(prefix="uchi_bl_")
    root = os.path.join(d, "root")
    os.makedirs(root)
    globals()["BASELINE"] = os.path.join(d, "baseline.json")
    try:
        p1 = os.path.join(root, "a.txt")
        with open(p1, "w", encoding="utf-8") as f:
            f.write("github_token = ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        t("　基準値が無ければ検知する", cmd_scan(root) == 1)

        t("　承知済みにできる", cmd_accept(root) == 0)
        t("★承知したものでは鳴らない★", cmd_scan(root) == 0)

        # ★★中身を差し替えても、検知の種類は同じ★★
        with open(p1, "w", encoding="utf-8") as f:
            f.write("github_token = ghp_BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB")
        t("★★中身を別の値へ差し替えたら知らせる★★"
          "（検知の種類が同じでも見逃さない）", cmd_scan(root) == 1)

        # ★★同じファイルにもう1個足す★★
        cmd_accept(root)
        with open(p1, "a", encoding="utf-8") as f:
            f.write("\ngithub_token = ghp_CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC")
        t("★★同じ種類の秘密をもう1個足したら知らせる★★", cmd_scan(root) == 1)

        # ★★新しいファイルが増えたら★★
        cmd_accept(root)
        p2 = os.path.join(root, "b.txt")
        with open(p2, "w", encoding="utf-8") as f:
            f.write("github_token = ghp_DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD")
        t("★新しいファイルが増えたら知らせる★", cmd_scan(root) == 1)

        # ★★確かめられなかったものは緑にしない★★（2026-09-04・Codexの指摘）
        #   ★試験は終了コードで見る★＝`content_findings()` が空でないことだけを
        #   見ていたので、**`cmd_scan()` が0を返しても合格していた**。
        #   （2026-09-04に一度「止めない」にして、下の①③④を全部通した）
        import zipfile as _zf
        _tok = "ghp_" + "E" * 36
        cmd_accept(root)

        # ①★読めない中身に本物の秘密が入っている★
        #   （不正UTF-8で始まるので本文検査が打ち切られる）
        p3 = os.path.join(root, "report.pdf")
        with open(p3, "wb") as f:
            f.write(b"\xff" + f"github_token = {_tok}".encode("utf-8"))
        t("★★読めない中身に秘密があっても緑にしない★★"
          "（本文検査が打ち切られる形）", cmd_scan(root) == 1)
        os.remove(p3)
        cmd_accept(root)

        # ②★ただの読めないファイルでも緑にしない★（fail-closed）
        p3b = os.path.join(root, "scan.pdf")
        with open(p3b, "wb") as f:
            f.write(b"%PDF-1.4\n\xff\xfe binary")
        t("　確かめられないファイルは、それだけでも止める",
          cmd_scan(root) == 1)
        os.remove(p3b)
        cmd_accept(root)

        # ③★ZIPで秘密を見つけた後に読めない要素があっても、秘密を捨てない★
        p5 = os.path.join(root, "a.zip")
        with _zf.ZipFile(p5, "w") as z:
            z.writestr("secret.txt", f"github_token = {_tok}")
            z.writestr("inner.zip", b"PK\x03\x04broken")
        _f = content_findings(p5)
        t("★★ZIPで見つけた秘密を、あとの理由で捨てない★★",
          any("github_token" in x for x in _f)
          and any("ZIPの中にZIP" in x for x in _f))
        t("　その場合も止める", cmd_scan(root) == 1)
        os.remove(p5)
        cmd_accept(root)

        # ④★読めないフォルダがあったら緑にしない★
        _orig_walk = globals()["_walk"]

        def _walk_ng(r, out_ng, _o=_orig_walk):
            out_ng.append("読めないフォルダ: test")
            return _o(r, [])

        globals()["_walk"] = _walk_ng
        try:
            t("★★調べられなかったフォルダがあれば緑にしない★★"
              "（cmd_accept は断るのに cmd_scan は素通りしていた）",
              cmd_scan(root) == 1)
        finally:
            globals()["_walk"] = _orig_walk
        t("　読めないフォルダが無ければ、いつもどおり緑",
          cmd_scan(root) == 0)

        # ★★存在しない場所★★
        t("★★走査先が無いときは緑にしない★★"
          "（0件走査で「検知なし」になっていた）",
          cmd_scan(os.path.join(d, "no_such_place")) == 1)

        # ★★別の場所では基準値を使わない★★
        cmd_accept(root)
        other = os.path.join(d, "other")
        os.makedirs(other)
        with open(os.path.join(other, "a.txt"), "w", encoding="utf-8") as f:
            f.write("github_token = ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        t("★★別の場所では承知済みを流用しない★★"
          "（同じ相対パスでも別物）", cmd_scan(other) == 1)
    finally:
        globals()["BASELINE"] = keep
        _sh.rmtree(d, ignore_errors=True)


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
    # 1b. ★★行き先が相対パスなら断る★★（2026-08-21・実際に間違えた）
    #   Dropboxへ入れたつもりのSKILL.mdが、リポジトリの中へ作られていた。
    #   気づかなければ、そのままコミットされて公開リポジトリに載る。
    p = w("SKILL.md", "# tejunsho")
    _cwd = os.getcwd()
    os.chdir(d)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            rc = cmd_copy(p, os.path.join("relative_dst", "SKILL.md"), False)
        t("★★行き先が相対パスなら断る★★（いま居るところの下に作られる）",
          rc == 2 and not os.path.exists(os.path.join(d, "relative_dst")))
    finally:
        os.chdir(_cwd)
    with contextlib.redirect_stdout(io.StringIO()):
        rc = cmd_copy(p, os.path.join(dst_dir, "sub", "SKILL.md"), False)
    t("　絶対パスなら通る",
      rc == 0 and os.path.exists(os.path.join(dst_dir, "sub", "SKILL.md")))

    # 2. 許可リスト外はコピー拒否
    p = w("mystery_data.json", "{}")
    with contextlib.redirect_stdout(io.StringIO()):
        rc = cmd_copy(p, os.path.join(dst_dir, "mystery_data.json"), False)
    t("許可リスト外ファイルはコピー拒否", rc == 1 and not os.path.exists(os.path.join(dst_dir, "mystery_data.json")))
    # ★★接頭辞を付け忘れたときは、正しい保存名を教える★★（2026-08-23・台帳#464）
    #   ★なぜ要るか＝実際に一晩無駄にした★
    #   無人タスクが保存先を confirmed_values.json にして拒否されたが、
    #   「リスト外」としか出ないので、**名簿に足りないのだと誤解して
    #   すでに載っている名前を重複追加する**という無意味な直しをした。
    p_pre = w("confirmed_values.json", "{}")
    _buf = io.StringIO()
    with contextlib.redirect_stdout(_buf):
        rc_pre = cmd_copy(p_pre, os.path.join(dst_dir, "confirmed_values.json"),
                          False)
    _out = _buf.getvalue()
    t("★★接頭辞の付け忘れは、正しい保存名を教えてから断る★★"
      "／★「リスト外」だけだと名簿の不足と誤解する（実際にした）★",
      rc_pre == 1 and "uchidokoro_confirmed_values.json" in _out)
    with contextlib.redirect_stdout(io.StringIO()):
        rc_ok = cmd_copy(p_pre,
                         os.path.join(dst_dir,
                                      "uchidokoro_confirmed_values.json"), False)
    t("　（対照）正しい保存名なら通る", rc_ok == 0)
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

    # 12. ★backup-design（_design の設計メモ保全・2026-07-28）★
    des = os.path.join(d, "_design")
    os.makedirs(des, exist_ok=True)
    open(os.path.join(des, "phase1_closure_conditions.md"), "w",
         encoding="utf-8").write("# 閉鎖条件\n1. 公開値は typed slot へ")
    open(os.path.join(des, "ledger_todo.json"), "w").write('{"x":1}')   # .md でない
    open(os.path.join(des, "leak_design.md"), "w", encoding="utf-8").write(
        "token: ghp_" + "A" * 24)                                       # 秘密混入
    t("★_design配下の .md は許可される（許可リストに書かなくても）",
      is_allowlisted("phase1_closure_conditions.md",
                     os.path.join(des, "phase1_closure_conditions.md")))
    t("★_design配下でも .json は許可しない（巨大な作業データ）",
      not is_allowlisted("ledger_todo.json", os.path.join(des, "ledger_todo.json")))
    t("★タスク手順書は {taskId}_SKILL.md の形なら許可（列挙不要）",
      is_allowlisted("task-watchdog_SKILL.md")
      and is_allowlisted("uchidokoro-quality-review_SKILL.md")
      and not is_allowlisted("secret_SKILL.mdx"))
    # ★CLAUDE.mdの控えの名前★（2026-08-04〜。圧縮前の控えが取れなかったので追加）
    t("バックアップ名: CLAUDEの控えは日付形式だけ許す",
      is_allowlisted("CLAUDE_uchidokoro_precompress_2026-08-04b.md")
      and is_allowlisted("CLAUDE_history_uchidokoro_2026-08-04.md")
      and is_allowlisted("CLAUDE_history_uchidokoro_precompress_2026-08-04b.md")
      and not is_allowlisted("CLAUDE_uchidokoro_backup.md")
      and not is_allowlisted("CLAUDE_uchidokoro_2026-08-04.txt")
      and not is_allowlisted("CLAUDE_uchidokoro_2026-8-4.md")
      and not is_allowlisted("CLAUDE_uchidokoro_precompress_2026-08-04bb.md")
      and not is_allowlisted("evil_CLAUDE_uchidokoro_2026-08-04.md"))
    _cl = lambda n: os.path.join(CLAIMS_DIR, n)      # noqa: E731
    t("★claims証拠（{slug}_{8桁日付}_{種別}.json）は許可（台帳#202）",
      is_allowlisted("milliongod_kiseki_20260803_maker.json",
                     _cl("milliongod_kiseki_20260803_maker.json"))
      and is_allowlisted("hokuto_20260101_ceiling.json",
                        _cl("hokuto_20260101_ceiling.json"))
      and is_allowlisted("issue27_yorumungando_2026-07-17.json",
                        _cl("issue27_yorumungando_2026-07-17.json")))
    # ★壊れたJSONは「秘密が無い」と見なさない★（2026-08-04・Codex85回目）
    import tempfile as _tf9
    _d9 = _tf9.mkdtemp(prefix="uchi_bg_")
    _ok9 = os.path.join(_d9, "manual_overrides.json")
    _ng9 = os.path.join(_d9, "manual_overrides_broken.json")
    _sec9 = os.path.join(_d9, "secret_broken.json")
    open(_ok9, "w", encoding="utf-8").write('{"schema":"x","items":[]}')
    open(_ng9, "w", encoding="utf-8").write('{"a":1,}')
    open(_sec9, "w", encoding="utf-8").write(
        '{"app_password":"abcd efgh ijkl mnop",}')
    t("　正しいJSONはそのまま通る", content_findings(_ok9) == [])
    t("★★壊れたJSONは中身を確かめられないので通さない★★"
      "（例外を握り潰して素通りしていた＝いま動いているガードの穴）",
      any("壊れていて" in x for x in content_findings(_ng9)))
    t("★★壊れたJSONに秘密の鍵が入っていても通さない★★",
      bool(content_findings(_sec9)))
    # ★確かめられなかったものは通さない★（2026-08-04・Codex86回目）
    _big9 = os.path.join(_d9, "big.json")
    with open(_big9, "wb") as _fb9:
        _fb9.write(b'{"app_password":"abcd efgh ijkl mnop","pad":"'
                   + b"x" * (21 * 1024 * 1024) + b'"}')
    t("★★大きすぎて読めないファイルは通さない★★"
      "（20MB超は検査を飛ばして素通りしていた＝ガードのfail-open）",
      any("大きすぎて" in x for x in content_findings(_big9)))
    os.remove(_big9)
    # ★配列の途中で検査をやめない／読めない文字コードを通さない★（Codex87回目）
    import json as _js9
    _arr9 = os.path.join(_d9, "manual_overrides.json")
    open(_arr9, "w", encoding="utf-8").write(_js9.dumps(
        [{} for _ in range(50)] + [{"app_password": "abcd efgh ijkl mnop"}]))
    t("★★JSON配列の51件目に秘密があっても見つける★★"
      "（先頭50件しか見ていなかった＝ガードのfail-open）",
      any("app_password" in x for x in content_findings(_arr9)))
    # ★UTF-16は読んで確かめる★（2026-08-06・毎朝の警告を潰すために変更）
    #   以前は「読めないから拒否」だったが、Windowsのタスク定義など
    #   正当なUTF-16があり、拒否のままでは中身を一度も確かめられなかった。
    _u16 = os.path.join(_d9, "note.md")
    open(_u16, "wb").write("これはUTF-16で保存した文章です".encode("utf-16"))
    t("　UTF-16の普通の文章は通す（中身を読めるので）",
      not content_findings(_u16))
    _u16b = os.path.join(_d9, "note2.md")
    open(_u16b, "wb").write(
        '{"app_password": "abcd efgh ijkl mnop"}'.encode("utf-16"))
    t("★★UTF-16で保存しても秘密は見つける★★",
      any("app_password" in x for x in content_findings(_u16b)))
    # ★ZIPも中を読んで確かめる★
    import zipfile as _zf9
    _z9 = os.path.join(_d9, "bundle.zip")
    with _zf9.ZipFile(_z9, "w") as _z:
        _z.writestr("logs/ok.txt", "普通のログです")
    t("　中身が普通のZIPは通す", not content_findings(_z9))
    _z9b = os.path.join(_d9, "bundle2.zip")
    with _zf9.ZipFile(_z9b, "w") as _z:
        _z.writestr("conf/gmail.json", '{"app_password": "abcd efgh ijkl mnop"}')
    t("★★ZIPの中の秘密も見つける★★",
      any("app_password" in x for x in content_findings(_z9b)))
    _z9c = os.path.join(_d9, "bundle3.zip")
    with _zf9.ZipFile(_z9c, "w") as _z:
        _z.writestr("inner.zip", open(_z9, "rb").read())
    # ★同じ名前の中身が2つあっても、全部見る★（Codex122回目の指摘1）
    _z9d = os.path.join(_d9, "dup.zip")
    with _zf9.ZipFile(_z9d, "w") as _z:
        _z.writestr("conf.json", '{"app_password": "abcd efgh ijkl mnop"}')
        _z.writestr("conf.json", '{"note": "ふつうの中身"}')
    t("★★ZIP内に同名の中身が2つあっても両方見る★★"
      "（名前で読むと1件しか読めず、秘密を見逃していた）",
      any("app_password" in x for x in content_findings(_z9d)))
    # ★前に別のデータを付けたZIPも見逃さない★（同・指摘2）
    _z9e = os.path.join(_d9, "prefixed.zip")
    _inner = open(_z9, "rb").read()
    with _zf9.ZipFile(_z9e, "w") as _z:
        _z.writestr("inner.bin", b"MZ" + b"x" * 64 + _inner)
    t("★★前置きデータ付きのZIPも確かめられないので通さない★★",
      any("ZIPの中にZIP" in x for x in content_findings(_z9e)))
    t("★★ZIPの中のZIPは確かめられないので通さない★★",
      any("ZIPの中にZIP" in x for x in content_findings(_z9c)))
    # ★中身がJSONなら拡張子に関係なく確かめる★（2026-08-04・Codex88〜89回目）
    #   ★試験は「許可される場所・別々の名前」で行う★
    #     （前回は一時ディレクトリ直下に置いたため名前の許可で落ちており、
    #       さらに2本が同じパスを上書きしていて意図した経路を試せていなかった）
    _dd9 = os.path.join(_d9, "_design")
    os.makedirs(_dd9, exist_ok=True)

    def _mk9(name, text, binary=False):
        q = os.path.join(_dd9, name)
        if binary:
            open(q, "wb").write(text)
        else:
            open(q, "w", encoding="utf-8").write(text)
        return q

    _json_md9 = _mk9("fake_json.md", '{"app_password":"abcd efgh ijkl mnop"}')
    _broken_md9 = _mk9("broken_json.md", '{"app_password":"abcd efgh ijkl mnop",}')
    _u16_9 = _mk9("utf16_note.md",
                  '{"app_password": "abcd efgh ijkl mnop"}'.encode("utf-16"),
                  binary=True)
    _zip9 = _mk9("fake_zip.md", b"PK" + bytes([3, 4]) + b"rest", binary=True)
    _nul9 = _mk9("nul_note.md", b"abc" + bytes([0]) + b"def", binary=True)
    _plain9 = _mk9("plain_note.md", "# 設計メモ" + chr(10) + "ふつうの文章")
    t("★★.md に JSON を書いても鍵の検査から逃げられない★★",
      any("app_password" in x for x in content_findings(_json_md9)))
    # ★引用符の種類・エスケープ・引用符なしでも逃げられない★（Codex90回目）
    t("★値の中にアポストロフィがあっても検知する★",
      any("app_password" in x for x in content_findings(
          _mk9("apos.md", '{"app_password":"ab' + chr(39) + 'cdefghij",}'))))
    t("★エスケープした引用符が入っていても検知する★",
      any("app_password" in x for x in content_findings(
          _mk9("esc.md",
               '{"app_password":"ab' + chr(92) + chr(34) + 'cdefghij"}'))))
    t("★引用符なしの代入でも検知する★",
      any("app_password" in x for x in content_findings(
          _mk9("bare.md", "app_password = abcdefghijkl"))))
    t("　コード片（secret = os.environ.get(...)）は誤検知しない",
      content_findings(_mk9("code2.md",
                            'secret = os.environ.get("X")')) == [])
    t("　日本語の説明文は誤検知しない",
      content_findings(_mk9("ja.md", "token: 認証に使う値のことです。")) == [])
    t("★★壊れたJSONを .md に書いても通さない★★"
      "（鍵の名前を本文から探す＝形式に頼らない・Codex89回目）",
      any("app_password" in x for x in content_findings(_broken_md9)))
    t("★★設計メモのコード片（token: str）を誤って拒否しない★★"
      "（鍵の名前だけで見ると22件が拒否された・2026-08-04の実測）",
      content_findings(_mk9("code_note.md",
                            "def f(token: str) -> None:" + chr(10)
                            + "    pass")) == [])
    t("★★カッコで始まるタスクログを誤って拒否しない★★"
      "（形式で判断すると [00:00:00] のログが通らなくなる）",
      content_findings(_mk9("dummy_2026-07-16.log",
                            "[00:00:00] STEP 0" + chr(10)
                            + "[00:00:01] 見張り bellco: 状態=OK")) == [])
    t("★★テキストの皮をかぶった圧縮ファイルも通さない★★",
      any("圧縮ファイル" in x for x in content_findings(_zip9)))
    t("　NULが入っているファイルはテキストとして扱わない",
      any("NUL" in x for x in content_findings(_nul9)))
    t("　ふつうの設計メモはこれまでどおり通る", content_findings(_plain9) == [])
    t("★★UTF-16に隠した秘密も見つける★★（読めるようになったので中身で判断）",
      any("app_password" in x for x in content_findings(_u16_9)))
    # ★最後のコピー拒否まで、別々のファイルで確かめる★（Codex89回目）
    _out9 = os.path.join(_d9, "out")
    os.makedirs(_out9, exist_ok=True)
    for _src9, _why9 in ((_json_md9, ".mdに書いたJSON"),
                         (_broken_md9, ".mdに書いた壊れたJSON"),
                         (_u16_9, "UTF-16に隠した秘密"),
                         (_arr9, "配列51件目の秘密")):
        _dst9 = os.path.join(_out9, os.path.basename(_src9))
        _rc9 = cmd_copy(_src9, _dst9, False)
        t(f"★★{_why9} は実際にコピーされない★★",
          _rc9 != 0 and not os.path.exists(_dst9))
    t("　ふつうの設計メモは実際にコピーできる（拒否しすぎていない）",
      cmd_copy(_plain9, os.path.join(_out9, "plain_note.md"), False) == 0
      and os.path.exists(os.path.join(_out9, "plain_note.md")))
    t("★★読み取りに失敗したファイルも通さない★★",
      any("読めないので" in x
          for x in content_findings(os.path.join(_d9, "no_such_file.json"))))
    __import__("shutil").rmtree(_d9, ignore_errors=True)
    t("★台帳を越えた修正の記録（manual_overrides.json）は許可★"
      "（2026-08-04・Codex84回目。標準経路で保全されていなかった）",
      is_allowlisted("manual_overrides.json"))
    t("★★claimsの形でも、置き場が違えば許さない★★"
      "（名前だけで通っていた・2026-08-04 Codex83回目の指摘6）",
      not is_allowlisted("anything_20260803_export.json",
                         os.path.join(os.path.expanduser("~"), "Desktop",
                                      "anything_20260803_export.json"))
      and not is_allowlisted("milliongod_kiseki_20260803_maker.json"))
    t("　★認証情報系はclaimsの形に一致しない（8桁日付の区切りが無い）★",
      not is_allowlisted("x_storage_uchidokoro.json")
      and not is_allowlisted("gmail_config.json")
      and not is_allowlisted("state.json"))
    t("★_design 以外の場所の同名 .md は許可しない",
      not is_allowlisted("phase1_closure_conditions.md",
                         os.path.join(d, "phase1_closure_conditions.md")))
    DROPBOX_ROOT_ALLOWED = d
    try:
        ddst = os.path.join(d, "dropbox_design")
        with contextlib.redirect_stdout(io.StringIO()):
            rc_bd = cmd_backup_design(des, ddst)
    finally:
        DROPBOX_ROOT_ALLOWED = orig_root
    t("backup-design: .mdだけコピー・秘密混入は拒否して非0",
      os.path.exists(os.path.join(ddst, "phase1_closure_conditions.md"))
      and not os.path.exists(os.path.join(ddst, "ledger_todo.json"))
      and not os.path.exists(os.path.join(ddst, "leak_design.md"))
      and rc_bd == 1)
    with contextlib.redirect_stdout(io.StringIO()):
        rc_out2 = cmd_backup_design(des, os.path.join(d, "outside_dropbox2"))
    t("backup-design: 認可ルート外の宛先を拒否", rc_out2 == 2)

    # ★★承知済みの仕組みの試験★★（2026-08-22・Codexが挙げた穴を全部当てる）
    _baseline_tests(t)

    # ★数えるのは、全部の試験が終わったこの場所だけ★（監査51）
    ok = all(c for _, c in results)
    print(f"\nselftest: {sum(1 for _, c in results if c)}/{len(results)} 合格")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Dropboxバックアップの秘密情報ガード")
    parser.add_argument("command", nargs="?",
                        choices=["copy", "scan", "accept", "backup-tree", "backup-design"])
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
    if args.command == "backup-design":
        if not args.src or not args.dst:
            parser.error("backup-design には src(_designディレクトリ) と dst が必要")
        return cmd_backup_design(args.src, args.dst)
    if args.command == "copy":
        if not args.src or not args.dst:
            parser.error("copy には <src> <dst> が必要")
        return cmd_copy(args.src, args.dst, args.optional)
    if args.command == "scan":
        if not args.dir:
            parser.error("scan には --dir が必要")
        return cmd_scan(args.dir)
    if args.command == "accept":
        # ★運営者が判断したときだけ★（無人タスクからは呼ばない）
        if not args.dir:
            parser.error("accept には --dir が必要")
        return cmd_accept(args.dir)
    parser.error("コマンドを指定（copy/scan/accept か --selftest）")
    return 2


if __name__ == "__main__":
    sys.exit(main())

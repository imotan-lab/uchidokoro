#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""★案件が本当に直ったかを、機械が確かめ直す唯一の場所★（2026-08-20）

## なぜ要るのか

台帳の案件は「無人タスクは閉じない」決まりだった。機械が「直しました」と
自己申告して閉じたら、誤情報が野放しになるからである。
その結果 **人が手を動かすまで永久に減らない**状態になり、
未解決CRITICALのある54機種が更新タスクの対象から外れ続けていた。

★抜け道★＝「直ったか」をAIに**宣言させる**のではなく、
**同じ検査を機械にやり直させる**。再現であって判断ではない。

## ★ここで守る線★（Codex依頼242・243で指摘され、実データで確かめたもの）

1. **終了コード0を「合格」と読まない。** 既存スクリプトの exit 0 は
   「何も言わなかった」であって「案件が直った」ではない（2026-08-20に実測）:
     - `validate_machine_data.py --slug 存在しない機種` → exit 0
     - `risky_atoms.py --slug X`（下見）→ 危ない表現が残っていても exit 0
     - `claim_pipeline.py --slug X` → 台帳で止まっている間は BLOCKED_BY_LEDGER で exit 0
       （★案件があるせいで、その案件を確かめる検査に到達しない＝堂々巡り★）
   だから4値を返す: **PASS / FAIL / ERROR / NOT_APPLICABLE**

2. **★PASS だけでは閉じない★**（依頼243の指摘3）。`closeable()` は
   「どの検査の・どの版で・どの機種の・どの食い違いを・どのコミットで見たか」が
   **呼び出し側の期待と全部一致**したときだけ真を返す。
   PASS という字面だけの辞書では閉じられない。

3. **台帳の値をコマンドに渡さない。** `CHECKS` は固定の名簿、`args_spec` で型と
   列挙を検査、未知キーは拒否。★subprocess を使わない★（2026-08-08のシェル事故の型）

4. **意味の判断をしない。** 読むのは**形の決まったデータ**だけ。
   本文の日本語から数字を読み取ろうとしない（それは2AIの仕事）。
   形が想定と違ったら **NOT_APPLICABLE**（推測で埋めない）

5. **閉じる根拠にできるのは「読者に届いているもの」だけ。**（依頼243の指摘1）
   記事データの `evTable` は**公開ページで使われていない**（ページはチェッカーの
   数値からその場で表を作る／`gates.py` も公開用データから除外している）。
   そういう検査は `closeable=False` にして**観測どまり**にする。

## 使い方

    python scripts/recheck.py --list
    python scripts/recheck.py --check settei_filled --slug hokuto
    python scripts/recheck.py --check settei_filled --all --json
    python scripts/recheck.py --selftest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
DETAILS = os.path.join(BASE, "assets", "data", "machine-details")

PASS = "PASS"
FAIL = "FAIL"
ERROR = "ERROR"
NOT_APPLICABLE = "NOT_APPLICABLE"
RESULTS = (PASS, FAIL, ERROR, NOT_APPLICABLE)

SLUG_RE = re.compile(r"^[a-z0-9_]+$")     # ★形の決まったslugだけ★（パス移動を防ぐ）


# --- 土台 -----------------------------------------------------------------

def _sha(text: str) -> str:
    """★64桁のまま使う★（依頼243の防御3: 切り詰めると衝突耐性を語れない）"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _read_json(path):
    return json.loads(_read_text(path))


def head_commit() -> str:
    """いま見ているコミット。★subprocess を使わずに .git から読む★

    取れなければ空文字（＝閉じる根拠にはできない）。
    """
    try:
        head = _read_text(os.path.join(BASE, ".git", "HEAD")).strip()
        if head.startswith("ref: "):
            ref = head[5:].strip()
            p = os.path.join(BASE, ".git", *ref.split("/"))
            if os.path.exists(p):
                return _read_text(p).strip()
            packed = os.path.join(BASE, ".git", "packed-refs")
            if os.path.exists(packed):
                for line in _read_text(packed).splitlines():
                    if line.endswith(" " + ref):
                        return line.split(" ", 1)[0].strip()
            return ""
        return head if re.fullmatch(r"[0-9a-f]{40}", head) else ""
    except Exception:                                        # noqa: BLE001
        return ""


def repo_clean() -> bool:
    """作業ツリーに未コミットの変更が無いか。★分からなければ False★

    （依頼244の指摘2: `.git/HEAD` を読むだけでは「検査したファイルがその
     コミットの中身だ」と言えない。手元で書き換えたままPASSを取れてしまう）
    ★コマンドは固定の引数配列で呼ぶ★＝シェルを通さない・外から来た文字列を混ぜない。
    """
    try:
        p = subprocess.run(["git", "-C", BASE, "status", "--porcelain"],
                           capture_output=True, text=True, timeout=30)
    except Exception:                                        # noqa: BLE001
        return False
    if p.returncode != 0:
        return False
    return p.stdout.strip() == ""


def _machines():
    data = _read_json(MACHINES)
    if not isinstance(data, list):       # ★machines.json はリスト★
        raise ValueError("machines.json がリストではありません")
    return data


def _machine(slug: str):
    for m in _machines():
        if m.get("slug") == slug:
            return m
    return None


def valid_slug(slug) -> bool:
    """★slugは自己申告させない★ 形が正しく、machines.json に実在するものだけ。"""
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        return False
    return _machine(slug) is not None


def _detail_path(slug: str):
    """★記事ファイルは必ず DETAILS の直下★（`..` などで外へ出させない）"""
    path = os.path.abspath(os.path.join(DETAILS, f"{slug}.json"))
    root = os.path.abspath(DETAILS) + os.sep
    return path if path.startswith(root) else None


def _load_detail(slug: str):
    """記事データを読む。★中身のslugが違えば読まない★（依頼243の指摘6）

    戻り値: (detail, 生テキスト, 理由)。読めないときは (None, None, 理由)。
    """
    path = _detail_path(slug)
    if not path or not os.path.exists(path):
        return None, None, "記事データがありません"
    raw = _read_text(path)
    try:
        detail = json.loads(raw)
    except Exception as e:                                   # noqa: BLE001
        return None, None, f"記事データを読めません: {type(e).__name__}"
    if not isinstance(detail, dict):
        return None, None, "記事データの形が想定外です"
    inner = detail.get("slug")
    if inner != slug:
        # ★別機種の中身が入っているファイルを、その機種の記事として扱わない★
        return None, None, f"記事データの中の機種名が違います（{inner!r}）"
    return detail, raw, ""


def _is_int(v) -> bool:
    """★bool を整数として通さない★（Pythonでは True は 1 と等しい）"""
    return type(v) is int and v >= 0


# --- 検査①: 設定示唆まとめの箱が、中身なしで出ていないか -------------------
#   ★読者に見える★＝machine.html は type:"settei" のとき、
#     見出しとバッジ凡例（弱/中/強/確）を必ず描いてから表を並べる。
#     中身が無いと「見出しと凡例だけ」が残る（台帳#150 の型）。

def _cell_text(cell, md: bool = True) -> str:
    """★そのセルが読者の画面に出す文字★（machine.html と同じ取り出し方）

    描画側は `typeof c === "object" && c !== null` なら `c.text` を、
    そうでなければ値そのものを出す。
    """
    if isinstance(cell, dict):
        cell = cell.get("text")
    if cell is None or isinstance(cell, bool):
        return ""
    if isinstance(cell, (int, float)):
        return str(cell)
    if isinstance(cell, str):
        # ★★読者に文字が出るかで見る★★（2026-09-03・Codexの4回目の指摘2）
        #   ★直す前は元の文字列を strip するだけ★だったので、
        #   `"** **"` が「文字あり」と数えられた
        #   （描画すると `<strong> </strong>` ＝読者には何も見えない）。
        try:
            import build_new_article as _ba_vt
            # ★★強調の変換をするかは型で違う★★
            #   （2026-09-03・Codexの6回目の指摘4）＝
            #   `settei` は `md()` を通さないので `** **` はそのまま見える。
            return _ba_vt.visible_text(cell, markdown=md)
        except Exception:                 # noqa: BLE001
            return cell.strip()
    return ""


def _row_has_text(row, two_cells: bool, md: bool = True) -> bool:
    """その行に、実際に文字が出るセルが1つ以上あるか。

    （依頼245の指摘1: 行が1つあることと、中身が描けることは別。
     `rows: [{"trigger":"","hint":""}]` は行1つだが、画面には空のセルが2つ出るだけ）
    """
    if two_cells:
        # 「表が無いときの rows」＝描画側は row[0]/row[1]（または trigger/hint）だけを見る
        if isinstance(row, list):
            cells = [row[0] if len(row) > 0 else None,
                     row[1] if len(row) > 1 else None]
        elif isinstance(row, dict):
            cells = [row.get("trigger"), row.get("hint")]
        else:
            cells = [row]
    else:
        cells = row if isinstance(row, list) else [row]
    return any(_cell_text(c, md) != "" for c in cells)


def _settei_renderable_rows(section: dict):
    """★machine.html と同じ順序で「実際に描かれる行」を数える★

    （依頼244の指摘3・防御1: 描画は
       ① `section.tables` があればそちらだけを使う（**空配列でもそちらを使う**
          ＝JavaScript では `[]` は真なので `rows` 分岐へ入らない）
       ② `tables` が無いときだけ `rows` を使う
     という順で選ぶ。Python で「空配列なら偽」と書くと規則がずれる）

    戻り値: (描ける行数, 想定外があれば理由)
    """
    # ★★強調の変換をするかは型で違う★★（2026-09-03・Codexの6回目の指摘4）
    #   `settei` は `md()` を通さないので `** **` はそのまま読者に見える。
    _md = section.get("type") != "settei"
    tables = section.get("tables")
    if tables is not None:
        if not isinstance(tables, list):
            return 0, "表の形が想定外です"
        n = 0
        for t in tables:
            if not isinstance(t, dict):
                return 0, "表の要素が辞書ではありません"
            headers = t.get("headers")
            rows = t.get("rows")
            if not isinstance(headers, list) or not headers:
                return 0, "表に見出し行がありません"      # 描画側は headers.map で落ちる
            if not isinstance(rows, list):
                return 0, "表の行が配列ではありません"
            for row in rows:
                if not isinstance(row, (list, str, dict)):
                    return 0, "表の行の形が想定外です"
                # ★行の数ではなく「文字が出る行」を数える★（依頼245の指摘1）
                if _row_has_text(row, two_cells=False, md=_md):
                    n += 1
        return n, ""
    rows = section.get("rows")
    if rows is None:
        return 0, ""
    if not isinstance(rows, list):
        return 0, "行が配列ではありません"
    return sum(1 for row in rows
               if _row_has_text(row, two_cells=True, md=_md)), ""


def check_settei_filled(args: dict) -> dict:
    """★読者に届く形（公開射影）で、設定示唆の箱が空になっていないか★

    （依頼244の指摘3: authoring の machine-details を見ても、読者が受け取る
     公開データに空箱が無いことの証明にはならない。公開ページは
     `gates.publish_view` を通した射影から作られる）
    """
    slug = args.get("slug")
    if not valid_slug(slug):
        return _result(NOT_APPLICABLE, "machines.json にその機種がありません", args)

    detail, raw, why = _load_detail(slug)
    if detail is None:
        return _result(NOT_APPLICABLE, why, args)

    # ★読者が実際に読み込むのはこのファイルそのもの★（2026-08-20に確認）
    #   `machine.html` と `machines/{slug}/index.html` は
    #   `fetch("assets/data/machine-details/{slug}.json")` を直接呼んでいる。
    #   Codex依頼244の指摘3は「公開射影(gates.publish_view)を見よ」だったが、
    #   ★その射影は Phase 1 の仕組みで、本番にはまだ繋がっていない★
    #   （実データ130機種すべてが `lifecycle が未指定` で射影を通れない）。
    #   原則（読者に届くものを検査する）に従うなら、いまはこのファイルが正しい。
    #   ★Phase 1 を配線したら、射影の側も併せて見ること★＝下の追加検査で先に備える。
    pub = detail
    sections = pub.get("sections")
    if not isinstance(sections, list):
        return _result(NOT_APPLICABLE, "記事に本文の箱がありません", args)

    # ★Phase 1 の射影が通る機種では、そちらも空でないことを確かめる★
    #   （配線後に「authoring は埋まっているが公開側は空」を見逃さないため）
    projected_ok = None
    try:
        sys.path.insert(0, os.path.join(BASE, "scripts"))
        import gates as _gates
        view = _gates.publish_view(_machine(slug), detail)
        if (view.get("gates") or {}).get("public"):
            psec = (view.get("detail") or {}).get("sections")
            if isinstance(psec, list):
                pset = [s for s in psec
                        if isinstance(s, dict) and s.get("type") == "settei"]
                # ★箱が0個なら「射影側も空でない」とは言えない★（依頼245の防御1）
                #   all([]) は真になるので、bool(pset) を先に見る
                projected_ok = bool(pset) and all(
                    _settei_renderable_rows(s)[0] > 0 for s in pset)
    except Exception:                                        # noqa: BLE001
        projected_ok = None      # 射影が通らない＝Phase 1 未配線。いまは判断に使わない

    settei = [s for s in sections
              if isinstance(s, dict) and s.get("type") == "settei"]
    if not settei:
        return _result(NOT_APPLICABLE, "設定示唆まとめの箱がありません", args,
                       observed={"boxes": 0})

    empty, odd = [], []
    for i, s in enumerate(settei):
        n, why2 = _settei_renderable_rows(s)
        if why2:
            odd.append({"index": i, "title": s.get("title"), "why": why2})
        elif n == 0:
            empty.append({"index": i, "title": s.get("title")})

    observed = {"boxes": len(settei), "empty": empty, "odd": odd,
                "served_digest": _sha(raw),          # 読者が受け取るファイルそのもの
                "projected_ok": projected_ok}        # Phase 1 の射影側（未配線なら None）
    if odd:
        # ★形が想定外なら「合格」にしない★（描けるかどうかを判断しない）
        return _result(NOT_APPLICABLE,
                       f"設定示唆の箱の形が想定外です（{len(odd)}個）", args,
                       observed=observed)
    if empty:
        return _result(FAIL,
                       f"設定示唆まとめの箱が中身なしで公開されています（{len(empty)}個）",
                       args, observed=observed)
    if projected_ok is False:
        # ★配線後に「手元は埋まっているが公開側は空」を合格にしない★
        return _result(FAIL, "公開射影の側で設定示唆の箱が空になります",
                       args, observed=observed)
    return _result(PASS, "読者が受け取る記事データの設定示唆の箱には、すべて描ける行があります",
                   args, observed=observed)


# --- 検査②: 噂の箱が「噂はありません」と言いながら出ていないか ---------------
#   ★読者に見える★＝machine.html は type:"rumor" のとき
#     黄色い枠と「⚠ 噂・未確定情報」の見出しを必ず描いてから本文を並べる。
#     その中に「噂はありません」と書いてあれば、読者は
#     「わざわざ枠を作って、何も無いと言っている」ページを見ることになる。
#
#   ★運営者の決定（2026-08-12・CLAUDE.md）★
#     「rumor は★中身ができてから出す★。噂や小ネタが無い機種のほうが多く、
#       空の箱は『あるのに載せていない』と読める」
#
#   ★ここで見るのは「サイト自身が書いた定型文」だけ★
#     他所の日本語を読み解くのではなく、**うちの生成物が自分で
#     「無い」と宣言している**ことを見つける。だから意味の判断にはならない。
#     ★中身が有るか無いかの判断はしない★（それは2AIの仕事）＝
#     この検査が言えるのは「無いと書いてある箱が出ている」ことだけ。

NO_RUMOR_PHRASES = (
    "噂・未確定情報はありません",
    "噂はありません",
    "未確定情報はありません",
)


def check_rumor_not_declared_empty(args: dict) -> dict:
    slug = args.get("slug")
    if not valid_slug(slug):
        return _result(NOT_APPLICABLE, "machines.json にその機種がありません", args)

    detail, raw, why = _load_detail(slug)
    if detail is None:
        return _result(NOT_APPLICABLE, why, args)

    sections = detail.get("sections")
    if not isinstance(sections, list):
        return _result(NOT_APPLICABLE, "記事に本文の箱がありません", args)

    rumor = [s for s in sections
             if isinstance(s, dict) and s.get("type") == "rumor"]
    if not rumor:
        return _result(NOT_APPLICABLE, "噂の箱がありません", args, observed={"boxes": 0})

    declared = []
    for i, s in enumerate(rumor):
        body = s.get("body")
        if not isinstance(body, list):
            return _result(NOT_APPLICABLE, "噂の箱の本文の形が想定外です", args)
        for j, line in enumerate(body):
            if not isinstance(line, str):
                continue
            for ph in NO_RUMOR_PHRASES:
                if ph in line:
                    declared.append({"box": i, "line": j, "phrase": ph})
                    break

    observed = {"boxes": len(rumor), "declared_empty": declared,
                "served_digest": _sha(raw)}
    if declared:
        return _result(FAIL,
                       f"噂の箱に「噂はありません」と書いたまま出しています"
                       f"（{len(declared)}行）",
                       args, observed=observed)
    return _result(PASS, "噂の箱は「無い」と宣言していません", args, observed=observed)


# --- 検査③: 交換率の順で、狙い目ラインが逆転していないか ----------------------
#   ★読者に見える★＝チェッカーは既定で eq56（5.6枚）を表示する。
#     交換率が良いほど浅く狙えるはずなのに、良い交換率の方が深い値が出ると、
#     読者は「条件が良いのに、より回さないと狙えない」という誤った案内を受ける。
#   ★機械で判定できる★＝数値の並びを見るだけ。意味の判断は要らない。
#   台帳#165 の検出方法（交換率が良い→悪いの順に単調か）をそのまま使う。

RATE_ORDER = ("eq56", "rate55", "rate50", "rate45")   # 良い → 悪い


def check_rate_monotonic(args: dict) -> dict:
    slug = args.get("slug")
    if not valid_slug(slug):
        return _result(NOT_APPLICABLE, "machines.json にその機種がありません", args)

    checker = (_machine(slug) or {}).get("checker")
    if not isinstance(checker, dict):
        return _result(NOT_APPLICABLE, "狙い目チェッカーの設定がありません", args)

    checked = 0
    bad = []
    for mode, conf in checker.items():
        if not isinstance(conf, dict):
            continue
        by = conf.get("byRate")
        if not isinstance(by, dict):
            continue
        for key in ("caution", "good", "excellent"):
            vals = []
            for r in RATE_ORDER:
                v = (by.get(r) or {}).get(key) if isinstance(by.get(r), dict) else None
                if _is_int(v):
                    vals.append((r, v))
            if len(vals) < 2:
                continue
            checked += 1
            seq = [v for _, v in vals]
            if any(seq[i] > seq[i + 1] for i in range(len(seq) - 1)):
                bad.append({"mode": mode, "key": key,
                            "values": {r: v for r, v in vals}})

    if not checked:
        return _result(NOT_APPLICABLE, "交換率ごとの設定がありません", args)
    observed = {"checked": checked, "reversed": bad}
    if bad:
        where = " / ".join(f"{b['mode']}.{b['key']}" for b in bad[:3])
        return _result(FAIL,
                       f"交換率が良いほうが深い値になっています（{len(bad)}組: {where}）",
                       args, observed=observed)
    return _result(PASS, "交換率の順に、狙い目ラインが浅い→深いで並んでいます",
                   args, observed=observed)


# --- 検査④: ポチポチくんの案内が出るのに飛び先が準備中になっていないか --------
#   ★読者に見える★＝記事ページに「小役カウンター ポチポチくん →」が
#     有効なリンクとして出るのに、飛んだ先が「準備中」になる＝読者が空振りする。
#   ★新台が増えるたびに増える構造★＝新しく足した機種はどのリストにも入らない。
#     2026-08-07に15件だったものが、2026-08-21には24件になっていた（台帳#252）。

# ★版の書き方に依らない★（2026-08-26）
#   ★直す前は `=== "page-decision/v1"` の字面を探していた★ので、
#   ひな型を版に依らない書き方（startsWith("page-decision/")）へ直した瞬間、
#   ★中身は同じなのに検査が落ち、新台が全部止まった★（実際に鳴った）。
#   ＝見たいのは「preview と新台経路を、名簿より先に無効にしているか」。
_PAGE_DISABLE = re.compile(
    r'machine\.status\s*===\s*"preview"[^\n]*?'
    r'publication_policy[^\n]*?'
    r'available:\s*false')


def page_disables_pochipochi(mh: str, row: dict) -> bool:
    """★記事ページ自身が、その機種のポチポチくんを無効にしているか★

    ★なぜ要るか（2026-08-24の夜・台帳#469。新台が2晩公開できなかった）★
      `machine.html` は新台経路（page-decision/v1）と preview を
      **名簿より先に**「解析データ判明後に対応」で無効にしている。
      ★検査だけがそれを知らず、名簿に無いという理由で必ず落としていた★
      ＝新台は作った瞬間に必ず落ちる＝1件も公開できない。
    ★実装を読んで判断する★＝ページからその分岐が消えたら、また落ちる。
    ★関数にする理由★＝試験が**本番の machine.html を書き換えずに**
      「分岐が消えた姿」を試せるようにするため（承認対象なので触らない）。
    """
    if not _PAGE_DISABLE.search(str(mh or "")):
        return False
    # ★版は問わない★＝ひな型が新台経路をまとめて無効にしているので、
    #   こちらも `page_decision.is_auto` と同じ問いにそろえる。
    # ★鍵があるかで見る★（2026-08-26・Codex31回目のP0）
    #   page_decision.is_auto とまったく同じ問いにそろえる。
    return (row.get("status") == "preview"
            or "publication_policy" in row)


def check_pochipochi_reachable(args: dict) -> dict:
    slug = args.get("slug")
    if not valid_slug(slug):
        return _result(NOT_APPLICABLE, "machines.json にその機種がありません", args)
    try:
        with open(os.path.join(BASE, "setting.html"), encoding="utf-8") as f:
            setting = f.read()
        with open(os.path.join(BASE, "machine.html"), encoding="utf-8") as f:
            mh = f.read()
    except Exception as e:                                   # noqa: BLE001
        return _result(ERROR, f"ページを読めません: {type(e).__name__}", args)

    has_config = re.search(r"[\"']?" + re.escape(slug) + r"[\"']?\s*:\s*\{", setting)

    def _listed(name):
        m = re.search(name + r"\s*=\s*\[([^\]]*)\]", mh)
        if not m:
            return False
        return ("'" + slug + "'") in m.group(1) or ('"' + slug + '"') in m.group(1)

    # ★★記事ページ自身が「解析待ち」として無効にしている場合★★
    #   （2026-08-24の夜・台帳#469。★新台が2晩公開できなかった原因★）
    #   `machine.html` は新台経路（page-decision/v1）と preview を
    #   **名簿より先に**「解析データ判明後に対応」で無効にしている。
    #   ★検査だけがそれを知らず、名簿に無いという理由で必ず落としていた★
    #   ＝新台は作った瞬間に必ずこの検査に落ちる＝1件も公開できない。
    #   ★実装を読んで判断する★＝ページからその分岐が消えたら、また落ちる。
    _by_page = page_disables_pochipochi(mh, _machine(slug) or {})

    observed = {"has_config": bool(has_config),
                "no_setting_diff": _listed("noSettingDiff"),
                "no_analysis": _listed("noAnalysis"),
                "disabled_by_page": _by_page}
    if (has_config or observed["no_setting_diff"]
            or observed["no_analysis"] or _by_page):
        return _result(PASS, "ポチポチくんの案内と飛び先が食い違っていません",
                       args, observed=observed)
    return _result(FAIL,
                   "ポチポチくんの案内が出るのに、飛び先が準備中になります"
                   "（MACHINE_CONFIGS にも noSettingDiff/noAnalysis にも無い）",
                   args, observed=observed)


# --- 検査⑤（観測どまり）: 注記の「通常◯◯G」が実際の値と合っているか ----------
#   ★読者に見える★＝交換率を選ぶと注記が画面に出る。
#     そこに書いてある「通常450G」等が実際の狙い目と違うと、読者の判断が変わる。
#   ★観測どまりにする理由★＝注記が古いのか、値のほうが誤りかを機械が決められない。
#     どちらかへそろえるには出典が要る（2AIの仕事）。

_NOTE_NORMAL = re.compile(r"通常(\d{3,4})G")


_STRAT_G = __import__("re").compile(r"(\d{2,4})\s*G")


def check_strategy_vs_checker(args: dict) -> dict:
    """一覧の「狙い目」と、チェッカーが既定で出す値がそろっているか

    ★なぜ見るか（2026-08-21・台帳#152）★
      トップページの一覧に出る `strategy`（例「等価470G〜 / リセット180G〜」）は
      チェッカーの**既定の値**（`checker.normal.good`）と同じ数字で書かれている。
      ところが記事ページのチェッカーは、交換率を選ぶと
      **`byRate[その交換率]` の値で上書き**する（machine.html 493行）。
      最初に選ばれているのは `defaultRate`（多くは eq56＝5.6枚）。

      ＝★一覧で「等価470G〜」と読んだ人が、記事のチェッカーでは480Gを見る★
      実測（2026-08-21）＝通常時だけで17機種が食い違っていた。

    ★どちらが正しいかは、ここでは決めない★
      一覧の数字を直すのか、交換率ごとの値を直すのかは出典を見る話。
      さらに「等価」が5.6枚を指すのかも別に整理が要る（台帳#219・#186）。
      ＝★観測どまりの検査★（closeable にしない）。
    """
    slug = args.get("slug")
    if not valid_slug(slug):
        return _result(NOT_APPLICABLE, "machines.json にその機種がありません", args)
    machine = _machine(slug) or {}
    checker = machine.get("checker")
    if not isinstance(checker, dict):
        return _result(NOT_APPLICABLE, "狙い目チェッカーの設定がありません", args)
    strategy = str(machine.get("strategy") or "")
    if not strategy:
        return _result(NOT_APPLICABLE, "一覧の狙い目がありません", args)
    nums = {int(n) for n in _STRAT_G.findall(strategy)}
    if not nums:
        return _result(NOT_APPLICABLE, "一覧の狙い目にG数がありません", args)

    # ★★全部のモードを見る★★（2026-08-30・運営者「2AIの出番だよ」）
    #   ★直す前は「通常時」のモードしか見ていなかった★ので、
    #   CZ間・AT間・ST間だけの機種を丸ごと飛ばしていた（133機種中94機種）。
    #   ＝トップページと記事で数字が違っても、誰も気づかなかった（実測45機種）。
    import align_strategy as _al
    # ★交換率を選べない機種も見る★（2026-08-30）＝
    #   ★直す前は「既定の交換率が無い」で39機種を飛ばしていた★。
    #   選べないなら、チェッカーが出すのは基準の値そのものなので比べられる。
    dflt = _al.default_rate(checker)
    slots = _al.slots(checker)
    if not slots:
        return _result(NOT_APPLICABLE, "狙い目の枠が読めません", args)

    # 既定の交換率で読者が見る値／どの交換率でも出しうる値
    shown_default = {s["rate"] for s in slots}
    shown_any = set(shown_default) | {s["base"] for s in slots}
    for md2 in (checker.get("exchangeRates") or []):
        rk = (md2 or {}).get("key")
        if not rk:
            continue
        shown_any |= {v for v in
                      (_al._rate_value(_al.mode_conf(checker, m.get("key")), rk)
                       for m in (checker.get("modes") or [])
                       if isinstance(m, dict))
                      if v is not None}

    named = _al.rate_words(checker, strategy)
    observed = {"strategy": strategy[:70], "strategy_numbers": sorted(nums),
                "default_rate": dflt or "（交換率を選べない機種）",
                "checker_shows": sorted(shown_default)[:10],
                "names_rates": named}

    if named:
        # ★交換率ごとに書き分けている一覧★＝既定だけと比べても意味がない。
        #   ★どの交換率の値にも無い数字があるときだけ★知らせる。
        stray = sorted(n for n in nums if n not in shown_any)
        if not stray:
            return _result(PASS,
                           "一覧の数字は、どれかの交換率でチェッカーが出す値です",
                           args, observed=observed)
        observed["stray"] = stray
        return _result(
            FAIL,
            f"一覧の {stray} が、チェッカーのどの交換率にもありません"
            f"（一覧は {'/'.join(named)} で書き分けています・2AIが読んでください）",
            args, observed=observed)

    stray = sorted(n for n in nums if n not in shown_default)
    if not stray:
        return _result(PASS,
                       "一覧の狙い目は、チェッカーが既定で出す値と同じです",
                       args, observed=observed)
    observed["stray"] = stray
    return _result(
        FAIL,
        f"一覧の {stray} が、チェッカーが既定"
        f"（{dflt or '交換率なし'}）で出す値"
        f"{sorted(shown_default)[:6]} にありません"
        "（★どちらが正しいかは2AIが読んで決めてください★）",
        args, observed=observed)


# 記事の書き方 → チェッカーの交換率のキー
_RATE_LABELS = {"5.6枚": "eq56", "6.0枚": "rate55",
                "6.5枚": "rate50", "7.0枚": "rate45"}
_BODY_RATE = __import__("re").compile(
    r"【\s*(5\.6枚|6\.0枚|6\.5枚|7\.0枚)[^】]*】\s*\**\s*(\d{2,4})\s*G")


def check_body_vs_checker(args: dict) -> dict:
    """記事の「当サイトの狙い目」の交換率ごとのG数が、チェッカーと合っているか

    ★なぜ見るか（2026-08-21・台帳#234を確かめて分かった）★
      台帳#234 は「値自体は存在するので**誤情報ではなく**、行の欠落」と
      見立てていたが、★実際は記事とチェッカーが90〜110G食い違っていた★。

      実例（2026-08-21）
        darlifra  記事 6.0枚 420G ／ チェッカー 510G（+90）
        nanatsuma 記事 6.0枚 555G ／ チェッカー 660G（+105）
        dmc5_st   記事 5.6枚 690G ／ チェッカー 750G（+60）

      ★一覧で見た数字と、記事のチェッカーが100G以上ずれる★＝
      打つ／打たないが変わる。読者に見える食い違い。

    ★どちらが正しいかは、ここでは決めない★
      きれいな一定の差になっているので「片方だけまとめて直した跡」に見えるが、
      どちらを直すかは出典を見る話。＝★観測どまりの検査★。
    """
    slug = args.get("slug")
    if not valid_slug(slug):
        return _result(NOT_APPLICABLE, "machines.json にその機種がありません", args)
    machine = _machine(slug) or {}
    checker = machine.get("checker")
    if not isinstance(checker, dict):
        return _result(NOT_APPLICABLE, "狙い目チェッカーの設定がありません", args)
    md = checker.get("modeData") or {}
    base = md.get("normal") if isinstance(md.get("normal"), dict) \
        else checker.get("normal")
    if not isinstance(base, dict):
        return _result(NOT_APPLICABLE, "通常時の設定がありません", args)
    by = base.get("byRate") or {}
    if not by:
        return _result(NOT_APPLICABLE, "交換率ごとの設定がありません", args)

    detail, _raw, _why = _load_detail(slug)
    if not isinstance(detail, dict):
        return _result(NOT_APPLICABLE, _why or "記事データがありません", args)

    checked, bad = 0, []
    for sec in detail.get("sections") or []:
        if str(sec.get("title") or "") != "当サイトの狙い目":
            continue
        for line in sec.get("body") or []:
            if not isinstance(line, str):
                continue
            for label, num in _BODY_RATE.findall(line):
                key = _RATE_LABELS[label]
                got = (by.get(key) or {}).get("good")
                if not _is_int(got):
                    continue
                checked += 1
                if got != int(num):
                    bad.append({"rate": label, "body": int(num),
                                "checker": got})
    if not checked:
        return _result(NOT_APPLICABLE,
                       "記事に交換率ごとのG数の記載がありません", args)
    observed = {"checked": checked, "mismatch": bad}
    if bad:
        where = " / ".join(
            f"{b['rate']} 記事{b['body']}G↔チェッカー{b['checker']}G"
            for b in bad[:3])
        return _result(FAIL,
                       f"記事の狙い目がチェッカーと違います（{where}）",
                       args, observed=observed)
    return _result(PASS, "記事の交換率ごとのG数はチェッカーと一致しています",
                   args, observed=observed)


# 判定書の topic → 記事の箱の見出し
_TOPIC2TITLE = {
    # ★基本スペック★（2026-08-25・Codexの21回目）
    #   ★対応表には spec があるのに、ここに無かった★＝
    #   その話題の箱を検査が一度も見ていなかった。
    "spec": "基本スペック",
    # ★モード・ゾーン★（2026-09-02・台帳#523の②）
    "modezone": "モード・ゾーン",
    "gameplay": "ゲーム性",
    "cz": "確認できたCZ",
    "ceiling": "天井・恩恵",
    "setting": "設定示唆まとめ",
    "strategy": "当サイトの狙い目",
    "reset": "朝一・リセット情報",
}
_PENDING_TEXT = "未確認です。確認でき次第、この欄に掲載します。"


def _pending_texts() -> tuple:
    """★記事が使う「未確認」の言い方の一覧★（正本は記事を作る側）。"""
    try:
        import build_new_article as _ba_pt
        return tuple(x for x in (
            tuple(getattr(_ba_pt, "PENDING_TEXTS", ()) or ())
            + (getattr(_ba_pt, "PENDING_ITEM", ""),)) if x)
    except Exception:                                        # noqa: BLE001
        return ()


_PENDING_ALL = _pending_texts()


def check_decision_vs_body(args: dict) -> dict:
    """判定書が「未確認」と言っている箱に、記事が中身を書いていないか

    ★なぜ見るか（2026-08-21・台帳#358）★
      新台経路の記事は「判定書（PageDecision v1）」を持ち、
      どの話題が確認済みでどれが未確認かを記録している。
      ★判定書が pending と言っている箱に、記事が断定を書いていた★

      実例（2026-08-21）
        pw_10523  判定書 gameplay=pending ／ 記事「通常時は周期抽選からCZへ進みます」
        prskkm    判定書 claims=[] ／ 記事「継続率約73%（出典2件で一致と明記）」
        ssb1      判定書 claims=[model_code] ／ 記事「純増約8.0枚（同上）」

      ★判定書は claims から毎回計算し直す設計★なので、
      記事に書いてある事実が claims に入っていないのはおかしい。
      ＝記事が claims 由来でない値を持っているか、claims の保存が落ちている。

    ★どちらを直すかは、ここでは決めない★
      記事の行を落とすのか、claims を作り直すのかは出典を見る話。
      ＝★観測どまりの検査★。
    """
    slug = args.get("slug")
    if not valid_slug(slug):
        return _result(NOT_APPLICABLE, "machines.json にその機種がありません", args)
    machine = _machine(slug) or {}
    pd = machine.get("page_decision")
    if not isinstance(pd, dict):
        return _result(NOT_APPLICABLE, "判定書がありません（新台経路ではない）",
                       args)
    pend = set(pd.get("pending_topics") or [])
    if not pend:
        return _result(NOT_APPLICABLE, "未確認の話題がありません", args)
    # ★★行ごとに、その行の根拠を見る★★
    #   ★話題まるごと免除しない★（2026-08-24・Codexの18回目）＝
    #   純増だけ確定していても、「ゲーム性」の中の**無関係な断定**まで
    #   検査の外に出してはいけない。
    #   ★2026-08-29に方針が変わった★＝2AIの確定値もDMM単独の値も
    #   **検索の濃さに数える**ので、その話題は pending にならない。
    #   ＝「濃さに数えないから見かけの食い違いが起きる」という免除は要らない。
    #   ★★短い語の「どれか1つ」で免除しない★★（2026-08-24・Codexの19回目）
    #   ★直す前は、その機種の確定値から集めた語のどれか1つが行にあれば通した★＝
    #   ゲーム性の確定値に「CZ」が含まれていると、
    #   **根拠のない「CZ当選率は90%です」まで免除**された（再現済み）。
    #   → ①話題ごとに分ける（その箱の話題の確定値だけ見る）
    #     ②その値の語が**全部そろっている**行だけ根拠ありとする
    #       （＝その確定値から書かれた行だけが通る）
    _by_topic = {}
    _pairs = {}                            # 話題 → {設定: 値}
    try:
        sys.path.insert(0, os.path.join(BASE, "scripts"))
        import confirmed_values as _cv_rc
        import page_decision as _pd_rc
        for _f, _rec in (_cv_rc.for_slug(slug) or {}).items():
            _b = _cv_rc.base_field(_f)
            # ★★話題は正本から引く★★（2026-08-25・Codexの20回目）
            #   ★直す前はここに小さな表を自前で持っていた★ので、
            #   `reset` と `at_net_unmapped` がどの話題にも結び付かず、
            #   **2AIで正しく確定した行まで「根拠がない」**と言われていた。
            try:
                _tp = _cv_rc.topic_of(_b)
            except Exception as _e_tp:                       # noqa: BLE001
                # ★★握り潰さない★★（2026-08-25・Codexの21回目）
                #   ★直す前は continue で黙って飛ばしていた★ので、
                #   「知らない項目は例外にする」と書きながら、実処理では
                #   **その項目が根拠にならないまま静かに進んで**いた。
                #   ＝正しい記事を止める側に倒れる（fail-closed になっていない）。
                return _result(ERROR, f"確定値の項目の話題が決まっていません: "
                                      f"{_e_tp}", args)
            if not _tp:
                continue          # 読者に出さない項目（型式名など）
            try:
                _tk = [str(x) for x in _cv_rc.check_shape(
                    _b, (_rec or {}).get("value")) if str(x).strip()]
            except Exception as _e_sh:                       # noqa: BLE001
                # ★★形が判定できないときも止まる★★
                #   （2026-08-25・Codexの22回目。話題の例外と同じ扱い）
                #   ★直す前は continue で飛ばしていた★ので、
                #   その項目が根拠にならないまま静かに進み、
                #   正しい記事を止める側に倒れていた。
                return _result(ERROR, f"確定値の形を判定できません: {_e_sh}",
                               args)
            if _tk:
                _by_topic.setdefault(_tp, []).append(_tk)
            # ★★設定ごとの値は「設定→値」の組で持つ★★
            #   （2026-08-25・Codexの22回目）
            #   ★1つの確定値が表の複数行に対応する★ので、
            #   「1行に語が全部そろう」規則をそのまま当てると必ず落ちる。
            _val = (_rec or {}).get("value")
            if isinstance(_val, dict) and _tp:
                # ★★項目ごとに分ける★★（2026-08-25・Codexの23回目）
                #   ★直す前は「話題→設定→値」だった★ので、
                #   at_prob と payout_rate はどちらも話題が `setting` で、
                #   **同じ設定番号を後から来た項目が上書き**していた。
                #   ＝正しい2つの表を止めたり、別項目の値を根拠にしたりする。
                for _k, _v in _val.items():
                    if str(_k).strip() and str(_v or "").strip():
                        _pairs.setdefault(_tp, {}).setdefault(
                            _b, {})[str(_k).strip()] = str(_v)
    # ★★「控えが無いとき」の枝は置かない★★（2026-08-25・Codexの24回目）
    #   控えが本当に無いとき `confirmed_values` は ConfirmedError を出すので、
    #   FileNotFoundError の枝は**一度も通らない**。
    #   置いたままだと、将来まったく別の FileNotFoundError を
    #   「控え0件」に読み替えてしまう危険だけが残る。
    #   ★線引きの正体は「CIでは先に空の控えを明示的に作っている」こと★。
    except Exception as _e_cv:                               # noqa: BLE001
        # ★★控えが読めないときも止まる★★（2026-08-25・Codexの22回目）
        #   ★直す前は空にして先へ進んでいた★ので、
        #   控えが壊れていると**全部が「根拠なし」**になり、
        #   正しい記事を毎回「直せ」と言い続けた。
        return _result(ERROR, f"確定値を読めません: {_e_cv}", args)

    _row_rc = _machine(slug) or {}

    def _identity_line(line: str) -> bool:
        """★身元の行（機種名・登場時期）は、この検査の対象ではない★

        （2026-08-25・通し試験で判明）
        ★話題 `spec` に「基本スペック」の箱を結び付けた途端★、
        機種名や登場時期まで「根拠のない断定」に数えていた。
        これらは claims ではなく身元で、DMMの機種ページで別に確かめている。
        ★字面の名簿ではなく、機種一覧の値と突き合わせる★。
        """
        _nm = str(_row_rc.get("name") or "").strip()
        if _nm and _nm in line:
            return True
        _rel = str(_row_rc.get("release_date") or "").strip()
        if _rel:
            _y, _, _rest = _rel.partition("-")
            _m = (_rest.split("-")[0] if _rest else "").lstrip("0")
            if _y and _m and f"{_y}年{_m}月" in line:
                return True
        return False

    def _backed(line: str, topic: str) -> bool:
        """★その行が、確かめた値から書かれているか★

        ★★DMMの名乗りだけでは免除しない★★（2026-08-29・Codexの指摘）
          ★2026-08-29までは「DMM単独確認の名乗りが付いていれば免除」★
          だった（当時は濃さに数えなかったので、判定書が pending なのに
          記事に本文がある状態が正常だったため）。
          方針が変わり**数えるようになった**ので、その話題は pending に
          ならない。★残すと、本物の食い違いを名乗りだけで見逃す★
          （判定書は未確認なのに記事に断定がある、を通してしまう）。

        いま免除するのは1つだけ＝
          **この話題の**2AI確定値の語が、その行に**全部**出ていること。
        """
        # ★★数の境界を守る★★（2026-08-25・Codexの21回目）
        #   ★直す前はただの部分一致だった★ので、
        #   確定値 600 が記事の「1600G」を、3.1 が「13.1」を
        #   **根拠あり**にしていた（＝出典に無い数を根拠つきに見せる）。
        #   ★照合の規則は1か所★＝控えの登録と同じ `token_in_quote` を通す。
        return any(all(_cv_rc.token_in_quote(tk, line) for tk in _tk)
                   for _tk in _by_topic.get(topic) or [])
    detail, _raw, _why = _load_detail(slug)
    if not isinstance(detail, dict):
        return _result(NOT_APPLICABLE, _why or "記事データがありません", args)

    bad = []
    for sec in detail.get("sections") or []:
        title = str(sec.get("title") or "")
        topic = None
        for t, ti in _TOPIC2TITLE.items():
            if ti == title:
                topic = t
                break
        if topic is None or topic not in pend:
            continue
        body = [x for x in (sec.get("body") or []) if isinstance(x, str)]
        _tbl_bad = []                      # 表の行のうち、控えと合わないもの
        # ★★表の行も中身★★（2026-08-25・Codexの22回目）
        #   ★直す前は body だけ見て、空ならその箱を読み飛ばしていた★ので、
        #   設定別の値は `tables` に出るのに**一度も検査されていなかった**。
        #   ＝判定書が「未確認」と言っている設定の箱に、
        #     根拠のない値が並んでいても PASS した。
        #   ★行ごとに見る★＝設定と値の組を1行として扱う
        #   （表を丸ごと1つの文字列にすると、余分な行まで一緒に免除される）。
        def _cell(x) -> str:
            """表のセルの文字（★2列目はバッジ付きの辞書★）。"""
            if isinstance(x, dict):
                return str(x.get("text") or "")
            return str(x or "")

        # ★★基本スペックが表でも、本文と同じに見る★★（2026-09-01・Codexの指摘）
        #   ★表の判定は「AT初当たり確率」「出玉率」しか知らない★ので、
        #   基本スペックを表にすると、機種名・登場時期・未確認のセルまで
        #   「根拠がない」と誤検知する。
        #   ＝本文と同じ形の行に直してから、今までの本文の判定へ渡す。
        _SPEC_HEAD = ["項目", "内容"]
        _spec_tbls = set()
        if title == "基本スペック":
            for _i_tb, _tb0 in enumerate(sec.get("tables") or []):
                if [str(x) for x in (_tb0.get("headers") or [])] != _SPEC_HEAD:
                    continue
                _spec_tbls.add(_i_tb)
                for _r0 in (_tb0.get("rows") or []):
                    _c0 = _r0 if isinstance(_r0, (list, tuple)) else [_r0]
                    if len(_c0) == 2:
                        body.append(
                            f"**{_cell(_c0[0])}**：{_cell(_c0[1])}")

        # ★表の見出し → 控えの項目名★（記事を作る側と同じ言い方）
        _TBL_FIELD = {"AT初当たり確率": "at_prob", "出玉率": "payout_rate"}
        # ★決まった名乗りの一覧★（記事を作る側の正本から取る）
        try:
            import build_new_article as _ba_tb
            _BASIS_MARKS = tuple(x for x in _ba_tb.BASIS_SUFFIX.values() if x)
        except Exception:                                    # noqa: BLE001
            _BASIS_MARKS = ()

        def _row_backed(tbl: dict, label: str, val: str) -> bool:
            """★その行が「その表の項目・その設定」の控えどおりか★

            ★1つの確定値が表の複数行に対応する★ので、行ごとに見る。
            ★★表ごとに項目を特定する★★（2026-08-25・Codexの23回目）
              同じ「設定」話題に項目が2つ（AT初当たり確率・出玉率）あるので、
              項目を見ないと**別項目の値を根拠にできる**。
            ★値は完全一致★＝`1/300` と `1/3000` は別の値。
              部分一致だと桁違いの値が通る（実測で通っていた）。
            """
            _v = val.strip()
            # ★★名乗りだけでは免除しない★★（2026-08-29・Codexの指摘）
            #   ★2026-08-29までは「（確認1件のみ）で終わっていれば通す」★
            #   だった（当時は単独確認を濃さに数えなかったので、
            #   判定書が未確認なのに表に値がある状態が正常だったため）。
            #   方針が変わり**数えるようになった**ので、
            #   その話題は未確認にならない。
            #   ★残すと、控えと違う値が並んでいても名乗りだけで見逃す★。
            #   ★値そのものが控えと一致していれば、名乗り付きでも通る★
            #   （下の `want + _bs`）。
            _field = _TBL_FIELD.get(str(tbl.get("label") or "").strip())
            if not _field:
                return False               # 知らない表は免除しない
            _p = (_pairs.get(topic) or {}).get(_field) or {}
            _lab = label.replace("設定", "").strip()
            want = (_p.get(_lab) or _p.get(label.strip()) or "").strip()
            if not want:
                return False               # その設定の控えが無い
            # ★★許すのは「値＋決まった名乗り」だけ★★（Codexの24回目）
            #   ★直す前は丸括弧で始まる任意の追記を許していた★ので、
            #   「1/300（実際は1/3000）」のような**別の断定**も通った。
            if _v == want:
                return True
            return any(_v == want + _bs for _bs in _BASIS_MARKS)

        for _i_tb2, _tb in enumerate(sec.get("tables") or []):
            if _i_tb2 in _spec_tbls:
                continue          # ★上で本文の形に直して渡した★（二重に見ない）
            for _row in (_tb.get("rows") or []):
                if not isinstance(_row, (list, tuple)):
                    continue
                _cells = [_cell(x) for x in _row if _cell(x).strip()]
                if len(_cells) < 2:
                    continue
                if _row_backed(_tb, _cells[0], "：".join(_cells[1:])):
                    continue               # 控えどおりの行は数えない
                # ★★表の行は、表の規則だけで決める★★
                #   （2026-08-25・Codexの23回目。実測で素通りを確認）
                #   ★本文用の判定へ渡すと、別の設定の同じ値で免除される★＝
                #     控えが「設定1=1/300」だけの機種で、
                #     表に「設定3=1/300」を足しても通っていた。
                _tbl_bad.append("：".join(_cells))
        txt = "".join(body + _tbl_bad).strip()
        if not txt:
            continue
        # ★★未確認の断りは「それだけ」のときに正しい★★
        #   （2026-08-24・Codexの19回目）
        #   ★直す前は箱の先頭が「未確認」なら後続を一切見なかった★＝
        #     未確認（確認でき次第掲載します）
        #     AT中の純増は約99枚です      ← ★これが素通りした★（再現済み）
        _nonempty = [x for x in body if x.strip()]
        # ★★「未確認」の言い方は、記事を作る側の正本から取る★★
        #   （2026-08-25・通し試験で判明）
        #   ★検査は自分の文言だけを知っていた★ので、
        #   記事が使う「未確認（確認でき次第掲載します）」が
        #   **行の途中にある**形（「**機械割**：未確認（…）」）を
        #   中身のある断定として数えていた。
        #   ＝まだ何も書いていない箱を「書いている」と言う。
        _isnote = [x for x in _nonempty
                   if _PENDING_TEXT in x or x.strip().startswith("未確認")
                   or any(_pt and _pt in x for _pt in _PENDING_ALL)]
        # ★表に合わない行があるなら、本文が断り書きだけでも見る★
        #   （2026-08-25・Codexの24回目。いまの生成器は作らないが、
        #     形としては「本文＝未確認／表＝根拠なし」が作れてしまう）
        if _isnote and len(_isnote) == len(_nonempty) and not _tbl_bad:
            continue
        # ★★行ごとに見る★★（2026-08-24・Codexの18回目）
        #   ★この話題の2AI確定値の語が全部出ている行★は数えない。
        #   ★断り書きの行そのものは数えない★（中身ではないため）
        _left = [x for x in _nonempty
                 if x not in _isnote and not _identity_line(x)
                 and not _backed(x, topic)]
        # ★表の行は、表の規則で外れた時点で数える★（本文の免除を通さない）
        _left += _tbl_bad
        if not _left:
            continue
        bad.append({"title": title, "topic": topic, "lines": len(_left),
                    "first": _left[0][:60]})
    observed = {"pending_topics": sorted(pend),
                "claims": list(pd.get("claims") or []), "mismatch": bad}
    if bad:
        where = " / ".join(f"{b['title']}（{b['lines']}行）" for b in bad[:3])
        return _result(
            FAIL,
            f"判定書は未確認と言っているのに記事が書いています（{where}）",
            args, observed=observed)
    return _result(PASS, "判定書が未確認と言っている箱は、記事も未確認です",
                   args, observed=observed)


def check_note_vs_threshold(args: dict) -> dict:
    slug = args.get("slug")
    if not valid_slug(slug):
        return _result(NOT_APPLICABLE, "machines.json にその機種がありません", args)
    checker = (_machine(slug) or {}).get("checker")
    if not isinstance(checker, dict):
        return _result(NOT_APPLICABLE, "狙い目チェッカーの設定がありません", args)
    normal = checker.get("normal")
    if not isinstance(normal, dict):
        return _result(NOT_APPLICABLE, "通常時の設定がありません", args)

    checked = 0
    bad = []
    for mode, conf in checker.items():
        if not isinstance(conf, dict):
            continue
        for rate, rv in (conf.get("byRate") or {}).items():
            note = (rv or {}).get("note")
            if not isinstance(note, str):
                continue
            for num in _NOTE_NORMAL.findall(note):
                checked += 1
                nb = (normal.get("byRate") or {}).get(rate)
                src = nb if isinstance(nb, dict) else normal
                good = src.get("good")
                if _is_int(good) and good != int(num):
                    bad.append({"mode": mode, "rate": rate,
                                "note_says": int(num), "actual": good})
    if not checked:
        return _result(NOT_APPLICABLE, "注記に「通常◯◯G」の記述がありません", args)
    observed = {"checked": checked, "mismatch": bad}
    if bad:
        where = " / ".join(f"{b['mode']}.{b['rate']} 注記{b['note_says']}G↔実際{b['actual']}G"
                           for b in bad[:3])
        return _result(FAIL, f"注記の「通常◯◯G」が実際と違います（{where}）",
                       args, observed=observed)
    return _result(PASS, "注記の「通常◯◯G」は実際の狙い目と一致しています",
                   args, observed=observed)


# --- 検査⑤（観測どまり）: 記事の目安表とチェッカーの区切り -------------------
#   ★closeable=False★ 記事の evTable は公開ページで使われていないため、
#     ここで一致していても「読者に正しく届いた」ことにはならない（依頼243の指摘1）。
#     作業用データの手入れの目印として残す。

_RANGE_BOTH = re.compile(r"^(\d+)〜(\d+)G$")
_RANGE_OPEN = re.compile(r"^(\d+)G〜$")


def _evtable_bounds(ev_table):
    """目安表の区切りを取り出す。★形が少しでも崩れていれば None★

    ①各行が「a〜bG」（最後だけ「aG〜」）②最初が0から ③昇順
    ④前の段の終わりと次の段の始まりが連続している ⑤範囲が逆転していない
    （依頼243の指摘5: 開始値だけ見ると、重なった表や逆転した表でも通ってしまう）
    """
    if not isinstance(ev_table, list) or len(ev_table) < 2:
        return None
    starts, ends = [], []
    for i, row in enumerate(ev_table):
        if not isinstance(row, dict):
            return None
        rng = row.get("range")
        if not isinstance(rng, str):
            return None
        both = _RANGE_BOTH.match(rng)
        openx = _RANGE_OPEN.match(rng)
        if both:
            a, b = int(both.group(1)), int(both.group(2))
            if a >= b:
                return None                  # 逆転・つぶれ
            starts.append(a)
            ends.append(b)
        elif openx:
            if i != len(ev_table) - 1:
                return None                  # 「N G〜」は最後の段だけ
            starts.append(int(openx.group(1)))
            ends.append(None)
        else:
            return None
    if starts[0] != 0:
        return None
    for i in range(1, len(starts)):
        if starts[i] <= starts[i - 1]:
            return None                      # 昇順でない
        prev_end = ends[i - 1]
        if prev_end is None:
            return None
        # ★連続しているか★（「0〜300G」の次は「300〜」か「301〜」だけ許す）
        if starts[i] not in (prev_end, prev_end + 1):
            return None
    return starts[1:]


def _rate_thresholds(mode_conf, rate_key, rate_was_explicit):
    """その交換率の閾値 [caution, good, excellent]。取れなければ None。

    ★明示された交換率が byRate に無ければ、基準値へ落とさない★
    （依頼243の指摘7: 落とすと、たまたま一致したときに通ってしまう）
    """
    if not isinstance(mode_conf, dict):
        return None
    by_rate = mode_conf.get("byRate")
    if isinstance(by_rate, dict) and rate_key in by_rate:
        src = by_rate[rate_key]
    elif rate_was_explicit:
        return None
    else:
        src = mode_conf
    if not isinstance(src, dict):
        return None
    out = []
    for key in ("caution", "good", "excellent"):
        v = src.get(key)
        if not _is_int(v):
            return None
        out.append(v)
    if not (out[0] <= out[1] <= out[2]):     # ★順序が壊れていたら読まない★
        return None
    return out


def check_evtable_vs_checker(args: dict) -> dict:
    slug = args.get("slug")
    if not valid_slug(slug):
        return _result(NOT_APPLICABLE, "machines.json にその機種がありません", args)

    m = _machine(slug)
    checker = m.get("checker")
    if not isinstance(checker, dict):
        return _result(NOT_APPLICABLE, "狙い目チェッカーの設定がありません", args)
    if checker.get("unit") != "G":
        return _result(NOT_APPLICABLE, "G数以外の単位の機種は対象外です", args)

    detail, raw, why = _load_detail(slug)
    if detail is None:
        return _result(NOT_APPLICABLE, why, args)
    ev = detail.get("evTable")
    if ev is None:
        return _result(NOT_APPLICABLE, "記事に目安表がありません", args)

    bounds = _evtable_bounds(ev)
    if bounds is None:
        return _result(NOT_APPLICABLE, "目安表の書き方が想定外です（読みません）", args)

    mode_key = "normal" if isinstance(checker.get("normal"), dict) else None
    if mode_key is None:
        modes = checker.get("modes")
        if isinstance(modes, list) and modes and isinstance(modes[0], dict):
            mode_key = modes[0].get("key")
    if not mode_key or not isinstance(checker.get(mode_key), dict):
        return _result(NOT_APPLICABLE, "基準にするモードが決まりません", args)

    rate_was_explicit = "rate" in args
    rate_key = args.get("rate") or checker.get("defaultRate")
    if not isinstance(rate_key, str) or not rate_key:
        return _result(NOT_APPLICABLE, "既定の交換率が決まりません", args)

    th = _rate_thresholds(checker[mode_key], rate_key, rate_was_explicit)
    if th is None:
        return _result(NOT_APPLICABLE, "その交換率の閾値が取れません", args)

    observed = {"evtable": bounds, "checker": th, "mode": mode_key,
                "rate": rate_key, "detail_digest": _sha(raw)}
    if len(bounds) != len(th):
        # ★段数が違うのは「食い違い」ではなく「対応が決められない」★
        #   （依頼243の指摘2: okidoki_encore の追加段はゾーンで、閾値とは別物）
        return _result(NOT_APPLICABLE,
                       f"段の数が違うので対応を決められません"
                       f"（記事{len(bounds)}個 / チェッカー{len(th)}個）",
                       args, observed=observed)

    diff = [(i, b, t) for i, (b, t) in enumerate(zip(bounds, th)) if b != t]
    if diff:
        where = " / ".join(f"{i + 1}段目 記事{b}G↔チェッカー{t}G" for i, b, t in diff)
        return _result(FAIL, f"目安表とチェッカーの区切りが食い違います: {where}",
                       args, observed=observed)
    return _result(PASS, "目安表とチェッカーの区切りは一致しています",
                   args, observed=observed)


# --- 結果の作り方 ---------------------------------------------------------

def _result(result: str, detail: str, args: dict, observed=None) -> dict:
    """★あとから「同じものを見たか」を確かめられる形で返す★

    - finding_key: 何を見た検査かを表す安定した名前（結果が変わっても同じ）
    - observation_digest: いま読んだ中身の指紋（1文字でも変われば変わる）
    - commit_sha: どのコミットで見たか
    """
    if result not in RESULTS:
        raise ValueError(f"知らない結果です: {result}")
    obs = observed or {}
    key_src = json.dumps({"args": args}, ensure_ascii=False, sort_keys=True)
    obs_src = json.dumps({"args": args, "observed": obs, "result": result,
                          "detail": detail}, ensure_ascii=False, sort_keys=True)
    return {
        "result": result,
        "detail": detail,
        "observed": obs,
        "args": dict(args),
        "finding_key": _sha(key_src),
        "observation_digest": _sha(obs_src),
        "commit_sha": head_commit(),
    }


# --- ★その場で直せる型の「閉じられる検査」★ ---------------------------------
#   （2026-08-21・Codexの設計レビュー）
#   ★指摘★＝「再検査が未実装の問題型は自動修正の対象にしない」。
#   いままで閉じられる検査は4つしかなく、
#   ★文体・型式名・他サイト名・重複★＝品質レビューが毎朝いちばん多く挙げる型に
#   対応する検査が無かった。＝「直せても機械的に閉じられない」ままだった。
#
#   ★この4つが「閉じられる」と言える理由★
#     どれも**記事データだけを見れば白黒が付く**（出典を見に行かなくていい）。
#     どちらが正しいかを選ぶ判断が入らない。


def _competitor_hits(text: str) -> list:
    """★他サイト名がそのまま出ていないか★（★監査17と同じ見方をする★）

    ★★2026-08-24・Codexの17回目★★
      ★名簿も、名乗りの扱いも、監査17とずれていた★＝
        ・正当な「DMMぱちタウン単独確認」→ 監査17は通す／こちらはNG
        ・「なな徹」「1geki.jp」→ 監査17はNG／こちらは通す
      後者は★問題を誤って「直った」と閉じてしまう★経路。
      ★同じ規則を2か所に書かない★＝監査17の部品をそのまま呼ぶ。
    """
    sys.path.insert(0, os.path.join(BASE, "scripts"))
    import audit_site as _a
    # ★根拠の名乗りは外してから見る★（監査17と同じ）
    try:
        text = _a.strip_allowed_basis(text)
    except Exception:                                        # noqa: BLE001
        pass
    names = list(getattr(_a, "COMPETITOR_NAMES", None) or [])
    names += list(getattr(_a, "_COMPETITOR_ALIASES", None) or [])
    if not names:
        names = ["スロパチクエスト", "ちょんぼりすた", "ナナプレス", "DMM",
                 "ぱちタウン", "スロラボ", "もしもアフィリエイト",
                 "moshimo.com", "af.moshimo", "i.moshimo"]
    return [n for n in names if n in text]


def check_plain_style_gone(args: dict) -> dict:
    """★文体そろえが直せる文末が残っていないか★

    ★これは「常体が無い」ことの証明ではない★（2026-08-21・対照実験で判明）
      見ているのは fix_plain_style.ENDINGS の19通りだけ。
      壊した状態を作って試したら「…確定する。」「…となる。」は素通りした。
      ＝**この検査だけを根拠に「文体混在は直った」と閉じてはいけない**。
      文体の案件を閉じるときは text_gone で逐語が消えたことも確かめる。
    """
    slug = args.get("slug")
    if not valid_slug(slug):
        return _result(NOT_APPLICABLE, "machines.json にその機種がありません", args)
    detail, raw, why = _load_detail(slug)
    if detail is None:
        return _result(NOT_APPLICABLE, why, args)
    try:
        sys.path.insert(0, os.path.join(BASE, "scripts"))
        import fix_plain_style as _f
        plan = _f.plan_for(detail)
    except Exception as e:                                   # noqa: BLE001
        return _result(ERROR, f"検査できません: {type(e).__name__}", args)
    if plan:
        ex = [old for (_w, old, _new, _t) in plan[:3]]
        return _result(FAIL, f"常体の文末が {len(plan)} 箇所あります", args,
                       {"count": len(plan), "例": ex})
    return _result(PASS, "常体の文末はありません", args, {"count": 0})


def check_model_code_gone(args: dict) -> dict:
    """型式名・検定番号が読者に出ていないか（記事データと公開HTMLの両方）。"""
    slug = args.get("slug")
    if not valid_slug(slug):
        return _result(NOT_APPLICABLE, "machines.json にその機種がありません", args)
    detail, raw, why = _load_detail(slug)
    if detail is None:
        return _result(NOT_APPLICABLE, why, args)
    try:
        sys.path.insert(0, os.path.join(BASE, "scripts"))
        import strip_model_code as _s
        plan = _s.plan_for(detail)
    except Exception as e:                                   # noqa: BLE001
        return _result(ERROR, f"検査できません: {type(e).__name__}", args)
    n = sum(len(v) for v in plan.values())

    # ★記事データを直しても、HTMLを描き直さないと読者には届いたまま★
    #   （2026-08-21・台帳#434）
    html_hits = []
    hp = os.path.join(BASE, "machines", slug, "index.html")
    if os.path.exists(hp):
        htxt = _read_text(hp)
        for lab in getattr(_s, "LABELS", ()):
            if lab in htxt:
                html_hits.append(lab)

    if n or html_hits:
        return _result(
            FAIL,
            f"型式名が残っています（記事データ {n} 箇所 ／ 公開HTML {len(html_hits)} 語）",
            args, {"data": n, "html": html_hits})
    return _result(PASS, "型式名はありません", args,
                   {"data": 0, "html": [], "html_checked": os.path.exists(hp)})


def check_competitor_names_gone(args: dict) -> dict:
    """他サイト名がそのまま出ていないか（記事データと公開HTML）。"""
    slug = args.get("slug")
    if not valid_slug(slug):
        return _result(NOT_APPLICABLE, "machines.json にその機種がありません", args)
    detail, raw, why = _load_detail(slug)
    if detail is None:
        return _result(NOT_APPLICABLE, why, args)
    try:
        hits = _competitor_hits(raw)
    except Exception as e:                                   # noqa: BLE001
        return _result(ERROR, f"検査できません: {type(e).__name__}", args)

    html_hits = []
    hp = os.path.join(BASE, "machines", slug, "index.html")
    if os.path.exists(hp):
        html_hits = _competitor_hits(_read_text(hp))

    if hits or html_hits:
        return _result(
            FAIL,
            "他サイト名が出ています: " + " / ".join(sorted(set(hits + html_hits))),
            args, {"data": hits, "html": html_hits})
    return _result(PASS, "他サイト名はありません", args,
                   {"data": [], "html": [], "html_checked": os.path.exists(hp)})


def check_duplicate_prose_gone(args: dict) -> dict:
    """同じ判断を2度読ませている箇所が残っていないか。

    ★閉じられる理由★＝候補が0件になるのは、記事データだけで確かめられる。
    ★閉じられない場合★＝2AIが「どちらも残す」と決めた記事は、
      候補が残るので**永久にPASSしない**。それでよい（勝手に閉じない側に倒す）。
    """
    slug = args.get("slug")
    if not valid_slug(slug):
        return _result(NOT_APPLICABLE, "machines.json にその機種がありません", args)
    detail, raw, why = _load_detail(slug)
    if detail is None:
        return _result(NOT_APPLICABLE, why, args)
    try:
        sys.path.insert(0, os.path.join(BASE, "scripts"))
        import find_duplicate_prose as _d
        rows = _d.scan(slug=slug)
    except Exception as e:                                   # noqa: BLE001
        return _result(ERROR, f"検査できません: {type(e).__name__}", args)
    if rows:
        return _result(FAIL, f"よく似た文の組が {len(rows)} 件あります", args,
                       {"count": len(rows)})
    return _result(PASS, "よく似た文の組はありません", args, {"count": 0})


def check_text_gone(args: dict) -> dict:
    """★指摘された逐語が、記事データにも公開HTMLにも無いこと★

    （2026-08-21・Codexの設計レビュー「問題箇所の逐語と位置を固定する」）
    ★これが要る理由★＝型ごとの検査は、その型の**直せる範囲**しか見ていない。
      文体の検査は19通りの文末しか知らないので、
      「…となる。」を直しても直さなくてもPASSしてしまう（対照実験で確認）。
      指摘された文そのものが消えたことを見れば、型を問わず確かめられる。
    ★判断は要らない★＝あるか無いかだけ。
    """
    slug = args.get("slug")
    text = args.get("text")
    if not valid_slug(slug):
        return _result(NOT_APPLICABLE, "machines.json にその機種がありません", args)
    if not isinstance(text, str) or len(text.strip()) < 4:
        return _result(NOT_APPLICABLE, "確かめる逐語がありません（4字以上）", args)
    detail, raw, why = _load_detail(slug)
    if detail is None:
        return _result(NOT_APPLICABLE, why, args)

    in_data = text in raw
    hp = os.path.join(BASE, "machines", slug, "index.html")
    in_html = os.path.exists(hp) and text in _read_text(hp)
    where = [w for w, hit in (("記事データ", in_data), ("公開HTML", in_html)) if hit]
    if where:
        return _result(FAIL, "指摘された文がまだ残っています: " + " / ".join(where),
                       args, {"data": in_data, "html": in_html})
    return _result(PASS, "指摘された文はどこにもありません", args,
                   {"data": False, "html": False,
                    "html_checked": os.path.exists(hp)})


# --- 検査の名簿 -----------------------------------------------------------
# ★ここに無い名前は動かない★（台帳から来た文字列でコマンドを組み立てない）

CHECKS = {
    "text_gone": {
        "version": 1,
        "closeable": True,          # ★型を問わず使える★ あるか無いかだけ
        "title": "指摘された逐語が記事データ・公開HTMLから消えているか",
        "fn": check_text_gone,
        "args_spec": {"slug": (str, True, None), "text": (str, True, None)},
    },
    "plain_style_gone": {
        "version": 1,
        "closeable": True,          # ★記事データだけで白黒が付く★
        "title": "文体そろえが直せる文末（19通り）が残っていないか",
        "fn": check_plain_style_gone,
        "args_spec": {"slug": (str, True, None)},
    },
    "model_code_gone": {
        "version": 1,
        "closeable": True,          # ★記事データと公開HTMLの両方を見る★
        "title": "型式名・検定番号が読者に出ていないか",
        "fn": check_model_code_gone,
        "args_spec": {"slug": (str, True, None)},
    },
    "competitor_names_gone": {
        "version": 1,
        "closeable": True,          # ★名簿は監査17と同じ★
        "title": "他サイト名がそのまま出ていないか",
        "fn": check_competitor_names_gone,
        "args_spec": {"slug": (str, True, None)},
    },
    "duplicate_prose_gone": {
        "version": 1,
        "closeable": True,          # ★候補0件は記事データだけで確かめられる★
        "title": "同じ判断を2度読ませている箇所が残っていないか",
        "fn": check_duplicate_prose_gone,
        "args_spec": {"slug": (str, True, None)},
    },
    "settei_filled": {
        "version": 1,
        "closeable": True,          # ★読者に見える★ 見出しと凡例だけが残る型
        "title": "設定示唆まとめの箱が中身なしで出ていないか",
        "fn": check_settei_filled,
        "args_spec": {"slug": (str, True, None)},
    },
    "rumor_not_declared_empty": {
        "version": 1,
        "closeable": True,          # ★読者に見える★ 「無い」と書いた箱が出ている
        "title": "噂の箱に「噂はありません」と書いたまま出していないか",
        "fn": check_rumor_not_declared_empty,
        "args_spec": {"slug": (str, True, None)},
    },
    "rate_monotonic": {
        "version": 1,
        "closeable": True,          # ★読者に見える★ 既定表示の値が逆転する
        "title": "交換率が良いほうが深い狙い目になっていないか",
        "fn": check_rate_monotonic,
        "args_spec": {"slug": (str, True, None)},
    },
    "pochipochi_reachable": {
        "version": 1,
        "closeable": True,          # ★読者に見える★ 案内どおりに飛べるか
        "title": "ポチポチくんの案内が出るのに飛び先が準備中になっていないか",
        "fn": check_pochipochi_reachable,
        "args_spec": {"slug": (str, True, None)},
    },
    "strategy_vs_checker": {
        "version": 1,
        "closeable": False,         # ★観測どまり★ どちらを直すかは出典が要る
        "title": "一覧の狙い目と、チェッカーが既定で出す値がそろっているか",
        "fn": check_strategy_vs_checker,
        "args_spec": {"slug": (str, True, None)},
    },
    "body_vs_checker": {
        "version": 1,
        "closeable": False,         # ★観測どまり★ どちらを直すかは出典が要る
        "title": "記事の交換率ごとの狙い目が、チェッカーと合っているか",
        "fn": check_body_vs_checker,
        "args_spec": {"slug": (str, True, None)},
    },
    "decision_vs_body": {
        "version": 1,
        "closeable": False,         # ★観測どまり★ どちらを直すかは出典が要る
        "title": "判定書が未確認と言っている箱に、記事が書いていないか",
        "fn": check_decision_vs_body,
        "args_spec": {"slug": (str, True, None)},
    },
    "note_vs_threshold": {
        "version": 1,
        "closeable": False,         # ★観測どまり★ どちらが正しいかは出典が要る
        "title": "注記の「通常◯◯G」が実際の狙い目と合っているか",
        "fn": check_note_vs_threshold,
        "args_spec": {"slug": (str, True, None)},
    },
    "evtable_vs_checker": {
        "version": 2,
        "closeable": False,         # ★観測どまり★ evTable は公開ページで使われない
        "title": "記事の目安表とチェッカーの区切り（作業用データ・公開されない）",
        "fn": check_evtable_vs_checker,
        "args_spec": {
            "slug": (str, True, None),
            "rate": (str, False, ("eq56", "rate55", "rate50", "rate45")),
        },
    },
}


def validate_args(check: str, args) -> str:
    """★未知のキー・型違い・列挙外は拒否★ 問題なければ空文字。"""
    if check not in CHECKS:
        return f"知らない検査です: {check}"
    if not isinstance(args, dict):
        return "args が辞書ではありません"
    spec = CHECKS[check]["args_spec"]
    for key in args:
        if key not in spec:
            return f"知らない引数です: {key}"
    for key, (typ, required, allowed) in spec.items():
        if key not in args:
            if required:
                return f"引数が足りません: {key}"
            continue
        val = args[key]
        if type(val) is not typ:                 # ★bool を str/int と混同しない★
            return f"引数の型が違います: {key}"
        if allowed is not None and val not in allowed:
            return f"引数の値が許されていません: {key}={val}"
    return ""


def run(check: str, args: dict) -> dict:
    """検査を1つ動かす。★例外は ERROR にして返す（落ちない・閉じない）★"""
    why = validate_args(check, args)
    meta = CHECKS.get(check, {})
    if why:
        return {"check": check, "version": meta.get("version"),
                "closeable_check": bool(meta.get("closeable")),
                "result": ERROR, "detail": why, "observed": {}, "args": {},
                "finding_key": "", "observation_digest": "", "commit_sha": ""}
    try:
        out = meta["fn"](args)
    except Exception as e:                                   # noqa: BLE001
        return {"check": check, "version": meta["version"],
                "closeable_check": bool(meta.get("closeable")),
                "result": ERROR, "detail": f"{type(e).__name__}: {e}",
                "observed": {}, "args": dict(args),
                "finding_key": "", "observation_digest": "", "commit_sha": ""}
    out["check"] = check
    out["version"] = meta["version"]
    out["closeable_check"] = bool(meta.get("closeable"))
    return out


def closeable(condition):
    """★台帳を閉じてよいかを、この関数が自分で確かめる★（依頼244の指摘1）

    ★渡された「結果」を信じない★＝以前は呼び出し側が持ってきた結果の辞書を
    見ていたので、`result` を PASS に書き換えた偽の結果でも通った
    （観測どまりの検査を `closeable_check=True` に書き換えることもできた）。
    いまは**閉鎖条件だけを受け取り、検査をその場でやり直す**。

    condition に要るもの（これ以外は信じない）:
      check           … 名簿にある検査の名前
      version         … 期待している検査の版
      args            … 検査に渡す引数（型と列挙は validate_args が見る）
      expected_commit … その内容を確かめたコミット（40桁）

    戻り値: (閉じてよいか, 理由, いま取り直した結果)
    """
    if not isinstance(condition, dict):
        return False, "閉鎖条件がありません", None

    check = condition.get("check")
    meta = CHECKS.get(check) if isinstance(check, str) else None
    if meta is None:
        return False, f"知らない検査です: {check!r}", None
    if not meta.get("closeable"):
        return False, f"この検査は観測どまりです: {check}", None

    want_ver = condition.get("version")
    if want_ver != meta["version"]:
        # ★検査の中身が変わったら、古い条件では閉じない★
        return False, f"検査の版が違います（条件{want_ver} / いま{meta['version']}）", None

    args = condition.get("args")
    why = validate_args(check, args if isinstance(args, dict) else None)
    if why:
        return False, f"引数が不正です: {why}", None

    want_commit = str(condition.get("expected_commit") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", want_commit):
        return False, "どのコミットで確かめるかが示されていません", None

    now = head_commit()
    if now != want_commit:
        return False, f"いまのコミットが条件と違います（{now[:12] or '不明'}）", None
    if not repo_clean():
        # ★未コミットの変更があるうちは閉じない★（手元だけ直った状態を証拠にしない）
        return False, "作業ツリーに未コミットの変更があります", None

    res = run(check, dict(args))
    if res.get("result") != PASS:
        return False, f"再検査が {res.get('result')} でした: {res.get('detail')}", res

    # ★検査のあいだに手元が動いていないか、もう一度見る★（依頼245の防御2）
    #   前だけ見ていると、検査中に別のプロセスが書き換えた内容で合格し得る。
    if res.get("commit_sha") != want_commit or head_commit() != want_commit:
        return False, "検査中にコミットが変わりました", res
    if not repo_clean():
        return False, "検査中に未コミットの変更が入りました", res
    return True, "再検査が合格しました", res


# --- CLI ------------------------------------------------------------------
# ★終了コードは「運用が失敗したか」だけを表す★（判定の結果を終了コードで語らない）
#   0 = 検査を最後まで動かせた / 2 = 使い方が違う / 3 = 検査が ERROR を返した

def _cmd_list():
    for name, c in sorted(CHECKS.items()):
        mark = "閉じられる" if c["closeable"] else "★観測どまり（閉じない）★"
        print(f"{name}  v{c['version']}  [{mark}]  {c['title']}")
        for key, (typ, required, allowed) in c["args_spec"].items():
            extra = f" 値={allowed}" if allowed else ""
            print(f"    {key}: {typ.__name__} ({'必須' if required else '任意'}){extra}")
    return 0


def _cmd_run(check, slug, rate, run_all, as_json):
    # ★名簿そのものが壊れていても、約束した終了コードから外れない★（依頼244の指摘4）
    try:
        if run_all:
            # ★欠落を黙って除外しない★（依頼245の指摘2）
            #   以前は `if m.get("slug")` で slug の無い行を落としていたので、
            #   棚卸しに使うと対象が静かに減った。重複も見ていなかった。
            rows_in = _machines()
            slugs, seen, bad = [], set(), []
            for i, m in enumerate(rows_in):
                if not isinstance(m, dict):
                    bad.append(f"{i}番目が辞書ではありません")
                    continue
                s = m.get("slug")
                if not isinstance(s, str) or not SLUG_RE.match(s):
                    bad.append(f"{i}番目のslugが不正です: {s!r}")
                    continue
                if s in seen:
                    bad.append(f"slugが重複しています: {s}")
                    continue
                seen.add(s)
                slugs.append(s)
            if bad:
                raise ValueError("機種の名簿が使えません: " + " / ".join(bad[:5]))
        else:
            slugs = [slug]
    except Exception as e:                                   # noqa: BLE001
        msg = f"machines.json を読めません: {type(e).__name__}: {e}"
        if as_json:
            print(json.dumps({"check": check, "total": 0, "commit_sha": head_commit(),
                              "tally": {ERROR: 1}, "results": [],
                              "error": msg}, ensure_ascii=False, indent=1))
        else:
            print(f"[{ERROR}] {msg}")
        return 3
    rows = []
    for s in slugs:
        args = {"slug": s}
        if rate:
            args["rate"] = rate
        rows.append(run(check, args))

    tally = {r: 0 for r in RESULTS}
    for r in rows:
        tally[r["result"]] = tally.get(r["result"], 0) + 1

    if as_json:
        print(json.dumps({"check": check,
                          "total": len(rows),
                          "commit_sha": head_commit(),
                          "tally": tally,
                          "results": rows}, ensure_ascii=False, indent=1))
    else:
        for r in rows:
            if run_all and r["result"] == PASS:
                continue
            print(f"[{r['result']}] {r['args'].get('slug')}: {r['detail']}")
        print()
        print(f"total={len(rows)}  " + "  ".join(f"{k}={v}" for k, v in tally.items() if v))
    return 3 if tally.get(ERROR) else 0


def _selftest():
    ok = 0
    total = 0

    def t(name, cond):
        nonlocal ok, total
        total += 1
        if cond:
            ok += 1
        print(("OK   " if cond else "NG   ") + name)

    # --- 目安表の読み取り（形が崩れていれば読まない）
    good = [{"range": "0〜300G"}, {"range": "300〜449G"},
            {"range": "450〜999G"}, {"range": "1000G〜"}]
    t("ふつうの目安表から区切りを取れる", _evtable_bounds(good) == [300, 450, 1000])
    t("★書き方が想定外なら読まない★",
      _evtable_bounds([{"range": "0-300G"}, {"range": "300G以上"}]) is None)
    t("★0から始まらない表は読まない★",
      _evtable_bounds([{"range": "100〜300G"}, {"range": "300G〜"}]) is None)
    t("★開いた段が途中にあれば読まない★",
      _evtable_bounds([{"range": "0G〜"}, {"range": "300〜400G"}]) is None)
    t("★範囲が逆転していれば読まない★",
      _evtable_bounds([{"range": "0〜300G"}, {"range": "300〜10G"}, {"range": "450G〜"}]) is None)
    t("★段が重なっていれば読まない★",
      _evtable_bounds([{"range": "0〜999G"}, {"range": "300〜449G"}, {"range": "450G〜"}]) is None)
    t("★段が飛んでいれば読まない★",
      _evtable_bounds([{"range": "0〜300G"}, {"range": "400〜449G"}, {"range": "450G〜"}]) is None)
    t("表が短すぎれば読まない", _evtable_bounds([{"range": "0〜300G"}]) is None)

    # --- 引数の検査
    t("★知らない検査は拒否★", validate_args("nope", {"slug": "x"}) != "")
    t("★知らない引数は拒否★",
      validate_args("evtable_vs_checker", {"slug": "revengers", "z": 1}) != "")
    t("★列挙にない交換率は拒否★",
      validate_args("evtable_vs_checker", {"slug": "revengers", "rate": "zzz"}) != "")
    t("★型が違えば拒否★", validate_args("evtable_vs_checker", {"slug": 3}) != "")
    t("必須が無ければ拒否", validate_args("evtable_vs_checker", {}) != "")

    # --- slug の扱い（形とパス）
    t("★変な形のslugは通さない★", not valid_slug("../secrets"))
    t("★空のslugは通さない★", not valid_slug(""))
    t("★記事ファイルは置き場の外へ出ない★", _detail_path("../../x") is None)

    # --- 閾値の読み方
    t("★true/false を数値として通さない★",
      _rate_thresholds({"caution": True, "good": 2, "excellent": 3}, "eq56", False) is None)
    t("★順序が壊れていたら読まない★",
      _rate_thresholds({"caution": 500, "good": 200, "excellent": 900}, "eq56", False) is None)
    t("★明示した交換率が無ければ基準値へ落とさない★",
      _rate_thresholds({"caution": 1, "good": 2, "excellent": 3,
                        "byRate": {"rate55": {}}}, "eq56", True) is None)
    t("明示しなければ基準値でよい",
      _rate_thresholds({"caution": 1, "good": 2, "excellent": 3}, "eq56", False) == [1, 2, 3])

    # --- 描画規則との一致（★JavaScriptと同じ順で選ぶ★）
    t("表に行があれば描ける",
      _settei_renderable_rows({"tables": [{"headers": ["a", "b"], "rows": [["x", "y"]]}]})
      == (1, ""))
    t("★空の表の配列があるとき rows へ落ちない（JSと同じ）★",
      _settei_renderable_rows({"tables": [], "rows": [["x", "y"]]}) == (0, ""))
    t("表が無ければ rows を使う",
      _settei_renderable_rows({"rows": [["x", "y"], ["z", "w"]]}) == (2, ""))
    t("★見出しの無い表は想定外にする★",
      _settei_renderable_rows({"tables": [{"rows": [["x"]]}]})[1] != "")
    t("★行が配列でなければ想定外★",
      _settei_renderable_rows({"tables": [{"headers": ["a"], "rows": "x"}]})[1] != "")

    # --- ★「行はあるが、画面には何も出ない」を数えない★（依頼245の指摘1）
    t("★空の行（rows: []）は0行★", _settei_renderable_rows({"rows": []}) == (0, ""))
    t("★中身の無い行（rows: [[]]）は0行★",
      _settei_renderable_rows({"rows": [[]]}) == (0, ""))
    t("★空文字だけの行（trigger/hint）は0行★",
      _settei_renderable_rows({"rows": [{"trigger": "", "hint": ""}]}) == (0, ""))
    t("★空文字だけの行（配列）は0行★",
      _settei_renderable_rows({"rows": [["", "  "]]}) == (0, ""))
    t("★表の中の空文字だけの行も0行★",
      _settei_renderable_rows({"tables": [{"headers": ["a", "b"],
                                           "rows": [["", ""], [{"text": " "}]]}]})
      == (0, ""))
    t("片方だけ文字があれば1行",
      _settei_renderable_rows({"rows": [{"trigger": "宵越し", "hint": ""}]}) == (1, ""))
    t("バッジの中の文字も数える",
      _settei_renderable_rows({"tables": [{"headers": ["a"],
                                           "rows": [[{"text": "強", "badge": "strong"}]]}]})
      == (1, ""))
    t("★3列目に文字があっても、rows分岐は先頭2つしか見ない（JSと同じ）★",
      _settei_renderable_rows({"rows": [["", "", "見えない"]]}) == (0, ""))
    t("★真偽値は文字として数えない★",
      _settei_renderable_rows({"rows": [[True, False]]}) == (0, ""))
    t("数値は文字として数える", _settei_renderable_rows({"rows": [[0, ""]]}) == (1, ""))

    # --- ★負例★ わざと空箱を入れたら、必ず FAIL になること
    slug0 = None
    for m in _machines():
        r0 = run("settei_filled", {"slug": m.get("slug")})
        if r0["result"] == PASS:
            slug0 = m.get("slug")
            break
    t("合格する機種が実データにある", slug0 is not None)

    if slug0:
        real_loader = globals()["_load_detail"]

        def _make_loader(new_rows):
            def _loader(_slug):
                detail, raw, why = real_loader(_slug)
                if detail is None:
                    return detail, raw, why
                hacked = json.loads(json.dumps(detail))
                for s in hacked.get("sections") or []:
                    if s.get("type") == "settei":
                        s.pop("tables", None)
                        s["rows"] = json.loads(json.dumps(new_rows))
                return hacked, raw, ""
            return _loader

        # ★「空っぽに見える箱」を4通り作って、どれも必ず不合格になること★
        #   （依頼245の指摘1: 行が1つあるだけで合格していた）
        for label, rows in (
                ("行が無い", []),
                ("空の行が1つ", [[]]),
                ("空文字の2セル", [{"trigger": "", "hint": ""}]),
                ("空白だけの配列", [["", "  "]])):
            try:
                globals()["_load_detail"] = _make_loader(rows)
                bad = run("settei_filled", {"slug": slug0})
                t(f"★空箱（{label}）は必ず不合格★", bad["result"] == FAIL)
                ok2, _why2, res2 = closeable(
                    {"check": "settei_filled",
                     "version": CHECKS["settei_filled"]["version"],
                     "args": {"slug": slug0},
                     "expected_commit": head_commit()})
                # ★作業ツリーが汚れていると、再検査まで行かずに断られる★
                #   （それも正しい動作なので、そのときは「断られたこと」だけを見る。
                #     綺麗なときは「再検査が FAIL だったこと」まで確かめる）
                reached = repo_clean()
                t(f"★空箱（{label}）では閉じられない★",
                  (not ok2) and ((res2 or {}).get("result") == FAIL if reached else True))
            finally:
                globals()["_load_detail"] = real_loader

    # --- 閉鎖条件の縛り（★結果の辞書を一切受け取らない★）
    cond = {"check": "settei_filled", "version": CHECKS["settei_filled"]["version"],
            "args": {"slug": slug0 or "x"}, "expected_commit": head_commit()}
    t("★条件がそろえば閉じられる（作業ツリーが綺麗なとき）★",
      (not slug0) or (not repo_clean()) or closeable(cond)[0])
    t("★条件を渡さなければ閉じない★", not closeable(None)[0])
    t("★知らない検査では閉じない★",
      not closeable({**cond, "check": "nope"})[0])
    t("★版が違えば閉じない★", not closeable({**cond, "version": 99})[0])
    t("★別のコミットでは閉じない★",
      not closeable({**cond, "expected_commit": "0" * 40})[0])
    t("★コミットが示されていなければ閉じない★",
      not closeable({**cond, "expected_commit": ""})[0])
    t("★存在しない機種では閉じない★",
      not closeable({**cond, "args": {"slug": "zzz_not_exist"}})[0])
    t("★知らない引数では閉じない★",
      not closeable({**cond, "args": {"slug": slug0 or "x", "z": 1}})[0])

    # --- ★偽の結果では閉じられない（依頼244の指摘1）★
    forged = {"result": PASS, "closeable_check": True, "check": "settei_filled",
              "version": 1, "finding_key": "a" * 64, "commit_sha": "b" * 40,
              "observation_digest": "c" * 64}
    t("★偽の合格の辞書を渡しても、そもそも受け取らない★",
      not closeable(forged)[0])

    # --- ★一覧の狙い目とチェッカーの突き合わせ★（2026-08-21・台帳#152）
    _sv = check_strategy_vs_checker

    # ★★自前の材料で回す★★（2026-08-30・罠㉙）
    #   ★直す前は本物の機種を名指ししていた★ので、検査の見る範囲を広げたら
    #   その機種の判定が入れ替わり、★試験だけが落ちた★。
    _ck_fake = {
        "exchangeRates": [{"key": "eq56", "label": "5.6枚"},
                          {"key": "rate55", "label": "6.0枚"}],
        "defaultRate": "eq56",
        "modes": [{"key": "cz", "label": "CZ間"}],
        "cz": {"good": 250,
               "byRate": {"eq56": {"target": 300},
                          "rate55": {"target": 350}}},
    }

    def _sv_fake(strategy, checker=None):
        keep = globals()["_machine"]
        globals()["_machine"] = lambda s: {
            "slug": "zzz_fake", "strategy": strategy,
            "checker": checker if checker is not None else _ck_fake}
        keep_valid = globals()["valid_slug"]
        globals()["valid_slug"] = lambda s: True
        try:
            return _sv({"slug": "zzz_fake"})["result"]
        finally:
            globals()["_machine"] = keep
            globals()["valid_slug"] = keep_valid

    t("★一覧の数字が、チェッカーが既定で出す値と違えば不合格★",
      _sv_fake("CZ間250G〜") == FAIL)
    t("　一覧にチェッカーの値が入っていれば合格",
      _sv_fake("CZ間300G〜") == PASS)
    t("★★通常時が無い機種も見る★★"
      "（＝CZ間・AT間だけの機種を94件も飛ばしていた）",
      _sv_fake("CZ間250G〜") == FAIL)
    t("★★交換率を選べない機種も見る★★（39件を飛ばしていた）",
      _sv_fake("通常250G〜",
               {"modes": [{"key": "normal", "label": "通常"}],
                "normal": {"good": 300}}) == FAIL)
    t("★★交換率ごとに書き分けた一覧は、どの交換率にも無い数字だけを言う★★",
      _sv_fake("5.6枚300G〜 / 6.0枚350G〜") == PASS
      and _sv_fake("5.6枚300G〜 / 6.0枚999G〜") == FAIL)
    t("　機種が無ければ判定しない",
      _sv({"slug": "zzz_test"})["result"] == NOT_APPLICABLE)
    t("★★どちらが正しいかは決めない（観測どまり）★★",
      CHECKS["strategy_vs_checker"]["closeable"] is False)
    t("　観測どまりなので、PASSでも閉じられない",
      not closeable({"check": "strategy_vs_checker",
                     "version": CHECKS["strategy_vs_checker"]["version"],
                     "args": {"slug": "hokuto"},
                     "expected_commit": head_commit()})[0])

    # --- ★記事の交換率ごとの狙い目とチェッカー★（2026-08-21・台帳#234を確かめて）
    _bv = check_body_vs_checker
    t("★記事とチェッカーが違えば不合格★",
      _bv({"slug": "darlifra"})["result"] == FAIL)
    t("　合っていれば合格（または対象外）",
      _bv({"slug": "hokuto"})["result"] in (PASS, NOT_APPLICABLE))
    t("★★これも観測どまり（どちらを直すかは出典が要る）★★",
      CHECKS["body_vs_checker"]["closeable"] is False)

    # ★★監査17と同じ見方をしている★★（2026-08-24・Codexの17回目）
    #   ★名簿も名乗りの扱いもずれていた★＝
    #     正しい「DMMぱちタウン単独確認」を NG にし、
    #     「なな徹」「1geki.jp」は素通りしていた。
    #     後者は**問題を誤って「直った」と閉じる**経路。
    # ★★2026-08-26：記事にサイト名を出さないことにした★★（運営者の指示）
    #   「ほかサイトのコピーと思われたくないから両方消そう」＝
    #   単独確認の名乗りからサイト名を落とした。
    #   ★この試験の前提（サイト名入りの名乗りが記事に出る）は無くなった★ので、
    #   いまの取り決め（サイト名は例外なくNG）を確かめる形に変える。
    # ★★名乗りの文字を直に書かない★★（2026-08-26）
    #   ★正本＝build_new_article.BASIS_SUFFIX★。直に書くと、
    #   名乗りを変えるたびに試験を手で直すことになる（実際そうなりかけた）。
    import build_new_article as _ba_ms
    _mark_ss = _ba_ms.BASIS_SUFFIX["DMM_SINGLE_NEAR_RELEASE"]
    t("★★記事にサイト名が出たら、名乗りであっても数える★★"
      "／★サイト名は出さない取り決めになった（2026-08-26）★",
      _competitor_hits("天井は999G（DMMぱちタウン単独確認）です。"))
    t("　いまの名乗りは、他サイト名に数えない",
      not _competitor_hits(f"天井は999G{_mark_ss}です。"))
    t("　いまの名乗りにサイト名が入っていない（名乗り自体の検査）",
      not _competitor_hits(_mark_ss))
    t("　（対照）名乗り以外で出たら、ちゃんと数える",
      _competitor_hits("スロパチクエストによると999Gです。"))
    t("★★一意な別表記も数える★★（監査17と同じ名簿）",
      _competitor_hits("なな徹によると999Gです。")
      and _competitor_hits("1geki.jp によると999Gです。"))

    # --- ★判定書と記事の食い違い★（2026-08-21・台帳#358）
    _dv = check_decision_vs_body
    # ★★2026-08-29に方針が変わった★★＝2AIの確定値もDMM単独の値も
    #   **検索の濃さに数える**ので、その話題は pending にならない。
    #   免除するのは「この話題の確定値の語が、その行に全部出ている」場合だけ。
    # ★★控えがあるか無いかで結果を変えない試験にする★★
    #   （2026-08-24＝控えが無い機械では確定値も無いので、
    #     同じ機種の結果が変わる。**環境で結果が変わる試験は書かない**）
    t("　（対照）機種が無ければ判定しない",
      _dv({"slug": "zzz_no_such_machine"})["result"] == NOT_APPLICABLE)
    # ★★2AIの確定値で埋まっている話題は、不整合と呼ばない★★
    #   （2026-08-24・Codexの17回目）
    #   ★控えを実際に作って確かめる★＝本物の置き場には触らない
    import tempfile as _tf17
    sys.path.insert(0, os.path.join(BASE, "scripts"))
    import confirmed_values as _cv17
    _keep17 = _cv17.STORE
    try:
        _cv17.STORE = os.path.join(_tf17.mkdtemp(prefix="rc17_"),
                                   "confirmed_values.json")
        _cv17.init_store()
        _no = _dv({"slug": "pw_10523"})["result"]
        # ★★記事にある行の数だけ材料を置く★★（2026-08-24・Codexの19回目）
        #   ★1件しか置いていなかった★ので、行ごとに見る形へ直した途端に
        #   2行目が「根拠なし」になり、**正しい記事を止めた**。
        #   ＝守りを厳しくして本番を止める型（自分で踏んだ）。
        def _rec17(v):
            # ★引用文はその値から作る★＝読み込み側の契約
            #   （値の語が引用に無ければ弾かれる）を通すため。
            #   ★手で書いた引用にしていたので、2件目が黙って落ちていた★
            _q = " ".join(str(x) for x in v.values())
            return {"value": v,
                    "sources": [{"url": "https://chonborista.com/slot/x",
                                 "quote": _q},
                                {"url": "https://nana-press.com/kaiseki/x",
                                 "quote": _q}],
                    "lineages": ["vote:chonborista", "vote:nana-press"],
                    "agreed_by": ["claude", "codex"],
                    "why": "2AIで突き合わせました",
                    "decided_at": "2026-08-24", "official_url": ""}
        json.dump({"schema_version": _cv17.SCHEMA, "machines": {
            "pw_10523": {
                "gameplay": _rec17({"when": "通常時", "trigger": "周期抽選",
                                    "leads_to": "CZ"}),
                "gameplay#上位": _rec17({"when": "全国制覇",
                                        "trigger": "全国制覇",
                                        "leads_to": "上位CZ"})}}},
            open(_cv17.STORE, "w", encoding="utf-8"), ensure_ascii=False)
        _yes = _dv({"slug": "pw_10523"})["result"]
        t("★★確定値があれば、その話題は不整合と呼ばない★★"
          "／★呼ぶと、正しい記事を毎回『直せ』と言い続ける★",
          _yes in (PASS, NOT_APPLICABLE) and _no == FAIL)
    finally:
        _cv17.STORE = _keep17
    # ★★行ごとに見ているか★★（2026-08-24・Codexの18回目）
    #   ★話題まるごと免除だと、根拠のない断定が同じ箱に紛れても素通りする★
    #   ＝ここは「読者に誤情報が出る経路」なので、必ず行で見る。
    import build_new_article as _ba18
    _mark18 = _ba18.BASIS_SUFFIX["DMM_SINGLE_NEAR_RELEASE"]
    _keepm, _keepd = _machine, _load_detail
    try:
        globals()["_machine"] = lambda sl: {
            "slug": sl, "name": "試験機",
            "page_decision": {"pending_topics": ["gameplay"]}}

        def _det(rows):
            return lambda sl: ({"sections": [
                {"title": "ゲーム性", "body": rows}]}, "", "")

        globals()["_load_detail"] = _det(["通常時は周期抽選からCZへ進みます"
                                          + _mark18])
        t("★★DMM単独の名乗りが付いた行も、判定書が未確認なら食い違い★★"
          "／★2026-08-29に方針が変わり、単独確認も濃さに数えるので、"
          "その話題は未確認にならないはず★"
          "／★名乗りだけで免除すると、本物の食い違いを見逃す★",
          _dv({"slug": "zzz_t18"})["result"] == FAIL)
        globals()["_load_detail"] = _det([
            "通常時は周期抽選からCZへ進みます" + _mark18,
            "AT中の純増は約8.0枚です"])
        _mix = _dv({"slug": "zzz_t18"})
        t("★★同じ箱の行を、行ごとに見ている★★"
          "／★話題ごと免除すると、根拠のない断定が検査の外に出る★"
          "／★報告は先頭1行だけ出すので、語ではなく行数で確かめる★",
          _mix["result"] == FAIL
          and (_mix["observed"]["mismatch"][0]["lines"] == 2))
        # ★★確定値がある機種でも、免除は「その値から書かれた行」だけ★★
        #   （2026-08-24・Codexの19回目。3件とも再現してから直した）
        import tempfile as _tf19
        import confirmed_values as _cv19
        _keep19 = _cv19.STORE
        try:
            _cv19.STORE = os.path.join(_tf19.mkdtemp(prefix="rc19_"),
                                       "confirmed_values.json")
            _cv19.init_store()
            json.dump({"schema_version": _cv19.SCHEMA, "machines": {
                "zzz_t19": {"gameplay": {
                    "value": {"when": "通常時", "trigger": "周期抽選",
                              "leads_to": "CZ"},
                    "sources": [{"url": "https://chonborista.com/slot/x",
                                 "quote": "通常時 周期抽選 CZ"},
                                {"url": "https://nana-press.com/kaiseki/x",
                                 "quote": "通常時 周期抽選 CZ"}],
                    "lineages": ["vote:chonborista", "vote:nana-press"],
                    "agreed_by": ["claude", "codex"],
                    "why": "2AIで突き合わせました",
                    "decided_at": "2026-08-24", "official_url": ""}}}},
                open(_cv19.STORE, "w", encoding="utf-8"), ensure_ascii=False)
            globals()["_load_detail"] = _det(["CZ当選率は90%です"])
            t("★★短い語が1つ一致しただけでは免除しない★★"
              "／★免除すると、根拠のない断定が『CZ』の一致だけで通る★",
              _dv({"slug": "zzz_t19"})["result"] == FAIL)
            globals()["_load_detail"] = _det([
                "通常時は周期抽選からCZへ進みます"])
            t("　その値から書かれた行（語が全部そろう）は免除する",
              _dv({"slug": "zzz_t19"})["result"] in (PASS, NOT_APPLICABLE))
            globals()["_load_detail"] = _det([
                "未確認（確認でき次第掲載します）", "AT中の純増は約99枚です"])
            t("★★『未確認』で始まっても、後ろの断定は見る★★"
              "／★箱ごと免除すると、2行目が素通りする★",
              _dv({"slug": "zzz_t19"})["result"] == FAIL)
            globals()["_load_detail"] = _det([_PENDING_TEXT])
            t("　断り書きだけの箱は、いままでどおり正しい",
              _dv({"slug": "zzz_t19"})["result"] in (PASS, NOT_APPLICABLE))
            # ★★表に出る確定値は、どれも根拠として効く★★
            #   （2026-08-25・Codexの20回目。★reset と at_net_unmapped が
            #     どの話題にも結び付かず、正しい記事を毎日「直せ」と
            #     言っていた★＝再現してから直した）
            def _rec20(v):
                _q = " ".join(str(x) for x in
                              (v.values() if isinstance(v, dict) else [v]))
                return {"value": v,
                        "sources": [{"url": "https://chonborista.com/slot/x",
                                     "quote": _q},
                                    {"url": "https://nana-press.com/kaiseki/x",
                                     "quote": _q}],
                        "lineages": ["vote:chonborista", "vote:nana-press"],
                        "agreed_by": ["claude", "codex"],
                        "why": "2AIで突き合わせました",
                        "decided_at": "2026-08-25", "official_url": ""}

            # ★★前の試験の材料を消さない★★（2026-08-25・自分で踏んだ）
            #   ★丸ごと書き換えていた★ので、後ろにある
            #   「別の話題の確定値では免除しない」の材料（zzz_t19 の gameplay）が
            #   消え、★その守りを壊しても試験が緑★になっていた
            #   （壊し方の通し確認が push 前に検知）。
            _cur20 = json.load(open(_cv19.STORE, encoding="utf-8"))
            _cur20["machines"]["zzz_t20"] = {
                "reset": _rec20({"kind": "CEILING_SHORTENED", "games": 600}),
                "at_net_unmapped": _rec20({"values": ["3.1", "7.4"],
                                           "mapping": "UNCONFIRMED"})}
            json.dump(_cur20, open(_cv19.STORE, "w", encoding="utf-8"),
                      ensure_ascii=False)
            globals()["_machine"] = lambda sl: {
                "slug": sl, "name": "試験機",
                "page_decision": {"pending_topics": ["reset", "gameplay"]}}

            def _det20(title, rows):
                return lambda sl: ({"sections": [
                    {"title": title, "body": rows}]}, "", "")

            globals()["_load_detail"] = _det20(
                "朝一・リセット情報", ["**設定変更後の天井**：600G"])
            t("★★朝一・リセットの確定値も根拠として効く★★"
              "／★効かないと、正しい記事を毎日『直せ』と言い続ける★",
              _dv({"slug": "zzz_t20"})["result"] in (PASS, NOT_APPLICABLE))
            globals()["_load_detail"] = _det20(
                "朝一・リセット情報", ["**設定変更後の天井**：700G"])
            t("　値を変えた行は止める（確定値と違う数）",
              _dv({"slug": "zzz_t20"})["result"] == FAIL)
            # ★★数の境界★★（2026-08-25・Codexの21回目）
            #   ★直す前はただの部分一致だった★ので、
            #   確定値 600 が記事の「1600G」を根拠ありにしていた。
            for _bad21 in ("**設定変更後の天井**：1600G",
                           "**設定変更後の天井**：1,600G",
                           "**設定変更後の天井**：600000G"):
                globals()["_load_detail"] = _det20("朝一・リセット情報",
                                                   [_bad21])
                t("★★別の数（" + _bad21[-8:] + "）を 600 の根拠にしない★★",
                  _dv({"slug": "zzz_t20"})["result"] == FAIL)
            globals()["_load_detail"] = _det20(
                "ゲーム性",
                ["**AT純増（AT名との対応は未確認）**：約13.1枚/G、約7.4枚/G"])
            t("★★13.1 を 3.1 の根拠にしない★★",
              _dv({"slug": "zzz_t20"})["result"] == FAIL)
            globals()["_load_detail"] = _det20(
                "ゲーム性",
                ["**AT純増（AT名との対応は未確認）**：約3.1枚/G、約7.4枚/G"])
            t("　AT名との対応が未確認の純増も根拠として効く",
              _dv({"slug": "zzz_t20"})["result"] in (PASS, NOT_APPLICABLE))
            globals()["_load_detail"] = _det20(
                "朝一・リセット情報",
                ["**設定変更後の天井**：600G", "朝一は必ず有利区間が切れます"])
            t("　同じ箱に根拠の無い行が混ざれば止める",
              _dv({"slug": "zzz_t20"})["result"] == FAIL)
            # ★★表に出る項目で、話題が決まっていないものが無いこと★★
            #   ★これが今回の見落としを機械で捕まえる★
            # ★★受け取れる項目を全部回す★★（2026-08-25・Codexの21回目）
            #   ★直す前は2つの名簿だけ★だったので、
            #   `spec_lookup.FIELDS` の at_prob / payout_rate / net_increase が
            #   **記事の表に出るのに対応表に無い**まま素通りしていた。
            import spec_lookup as _sl21
            _all21 = (list(_cv19.FIELD_TARGETS) + list(_cv19.AI_ONLY_FIELDS)
                      + list(_sl21.FIELDS))
            _miss20, _notitle21 = [], []
            for _f in _all21:
                try:
                    _tp21 = _cv19.topic_of(_f)
                except Exception:                            # noqa: BLE001
                    _miss20.append(_f)
                    continue
                # ★話題が決まっていても、見出しの対応が無ければ検査が届かない★
                if _tp21 and _tp21 not in _TOPIC2TITLE:
                    _notitle21.append(f"{_f}→{_tp21}")
            t("★★話題に対応する見出しが決まっている★★"
              "／★無いと、その話題の箱を検査が一度も見ない★"
              + ("" if not _notitle21 else "／" + "／".join(_notitle21)),
              not _notitle21)
            t("★★確定値の項目に、記事の話題が決まっていないものが無い★★"
              "／★足し忘れると、その項目は根拠にならず正しい記事を止める★"
              + ("" if not _miss20 else "／未定義: " + "／".join(_miss20)),
              not _miss20)
            # ★★設定別の表の行も見る★★（2026-08-25・Codexの22回目）
            #   ★直す前は body だけ見て、空ならその箱を読み飛ばしていた★＝
            #   設定別の値は `tables` に出るので**一度も検査されていなかった**。
            _cur22 = json.load(open(_cv19.STORE, encoding="utf-8"))
            _cur22["machines"]["zzz_t22"] = {
                "at_prob": _rec20({"1": "1/300", "6": "1/200"})}
            json.dump(_cur22, open(_cv19.STORE, "w", encoding="utf-8"),
                      ensure_ascii=False)
            globals()["_machine"] = lambda sl: {
                "slug": sl, "name": "試験機",
                "page_decision": {"pending_topics": ["setting"]}}

            def _tbl22(*tables):
                """★記事と同じ形★＝表には見出し（label）が付く。"""
                return lambda sl: ({"sections": [
                    {"title": "設定示唆まとめ", "body": [], "type": "settei",
                     "tables": [
                         {"label": lb, "headers": ["設定", lb],
                          "rows": [[k, {"text": v, "badge": "hint"}]
                                   for k, v in rows]}
                         for lb, rows in tables]}]}, "", "")

            _AT = "AT初当たり確率"
            _PO = "出玉率"
            globals()["_load_detail"] = _tbl22(
                (_AT, [("設定1", "1/300"), ("設定6", "1/200")]))
            t("★★表の行も検査する（確定値どおりなら通す）★★",
              _dv({"slug": "zzz_t22"})["result"] in (PASS, NOT_APPLICABLE))
            globals()["_load_detail"] = _tbl22(
                (_AT, [("設定1", "1/300"), ("設定3", "1/250")]))
            t("★★表に根拠の無い行が混ざれば止める★★"
              "／★body だけ見ていた頃は、表の値を一度も検査していなかった★",
              _dv({"slug": "zzz_t22"})["result"] == FAIL)
            # ★★Codexの23回目：4つの反例★★
            globals()["_load_detail"] = _tbl22(
                (_AT, [("設定1", "1/3000")]))
            t("★★同じ設定で値だけ違う（1/300→1/3000）は止める★★"
              "／★部分一致だと桁違いの値が通る★",
              _dv({"slug": "zzz_t22"})["result"] == FAIL)
            globals()["_load_detail"] = _tbl22(
                (_AT, [("設定1", "1/300"), ("設定3", "1/300")]))
            t("★★控えに無い設定が、既知の値をコピーしていれば止める★★"
              "／★表で外れても本文の判定で免除されると素通りする★",
              _dv({"slug": "zzz_t22"})["result"] == FAIL)
            # ★★控えが1件だけの機種で、その値をコピーする形★★
            #   （2026-08-25・Codexの23回目。★実測で素通りしていた★）
            #   表の行を本文用の判定へ渡すと、
            #   「その値がどこかにある」だけで免除されてしまう。
            _cur23a = json.load(open(_cv19.STORE, encoding="utf-8"))
            _cur23a["machines"]["zzz_t23a"] = {
                "at_prob": _rec20({"1": "1/300"})}
            json.dump(_cur23a, open(_cv19.STORE, "w", encoding="utf-8"),
                      ensure_ascii=False)
            globals()["_load_detail"] = _tbl22((_AT, [("設定1", "1/300")]))
            t("　控えが1件だけでも、そのとおりなら通る",
              _dv({"slug": "zzz_t23a"})["result"] in (PASS, NOT_APPLICABLE))
            globals()["_load_detail"] = _tbl22(
                (_AT, [("設定1", "1/300"), ("設定3", "1/300")]))
            t("★★控えが1件だけの機種で値をコピーしても止める★★"
              "／★表の行を本文の判定へ渡すと、値がどこかにあるだけで免除される★",
              _dv({"slug": "zzz_t23a"})["result"] == FAIL)
            # ★2つの表（項目ちがい）が同時に正しいなら通る★
            _cur23 = json.load(open(_cv19.STORE, encoding="utf-8"))
            _cur23["machines"]["zzz_t23"] = {
                "at_prob": _rec20({"1": "1/300", "6": "1/200"}),
                "payout_rate": _rec20({"1": "97.3%", "6": "110.5%"})}
            json.dump(_cur23, open(_cv19.STORE, "w", encoding="utf-8"),
                      ensure_ascii=False)
            globals()["_load_detail"] = _tbl22(
                (_AT, [("設定1", "1/300"), ("設定6", "1/200")]),
                (_PO, [("設定1", "97.3%"), ("設定6", "110.5%")]))
            t("★★項目ちがいの2つの表が、どちらも正しければ通る★★"
              "／★話題だけで持つと、後から来た項目が上書きして正しい表を止める★",
              _dv({"slug": "zzz_t23"})["result"] in (PASS, NOT_APPLICABLE))
            globals()["_load_detail"] = _tbl22(
                (_AT, [("設定1", "97.3%")]),
                (_PO, [("設定1", "1/300")]))
            t("★★2つの表の値を入れ替えたら止める★★"
              "／★項目を見ないと、別項目の値を根拠にできる★",
              _dv({"slug": "zzz_t23"})["result"] == FAIL)
            # ★★形が判定できないときも止まる★★
            _keep_shape = _cv19.check_shape
            try:
                _cv19.check_shape = lambda f, v: (_ for _ in ()).throw(
                    _cv19.ConfirmedError("試験：形が判定できません"))
                t("　確定値の形が判定できなければ、判定せず止まる",
                  _dv({"slug": "zzz_t22"})["result"] == ERROR)
            finally:
                _cv19.check_shape = _keep_shape
            # ★★本文が断り書きだけでも、表に合わない行があれば止める★★
            #   （2026-08-25・Codexの24回目）
            #   ★直す前は先に飛ばしていた★ので、
            #   「本文＝未確認／表＝根拠なし」の形で表の不一致が消えた。
            #   ★いまの生成器は作らないが、形としては作れてしまう★。
            globals()["_machine"] = lambda sl: {
                "slug": sl, "name": "試験機",
                "page_decision": {"pending_topics": ["setting"]}}
            globals()["_load_detail"] = lambda sl: ({"sections": [
                {"title": "設定示唆まとめ", "type": "settei",
                 "body": [_PENDING_TEXT],
                 "tables": [{"label": "AT初当たり確率",
                             "headers": ["設定", "AT初当たり確率"],
                             "rows": [["設定3", {"text": "1/250",
                                                "badge": "hint"}]]}]}]},
                "", "")
            t("★★本文が断り書きだけでも、表の根拠なしは止める★★"
              "／★先に飛ばすと、表の不一致がまるごと消える★",
              _dv({"slug": "zzz_t22"})["result"] == FAIL)
            globals()["_load_detail"] = lambda sl: ({"sections": [
                {"title": "設定示唆まとめ", "type": "settei",
                 "body": [_PENDING_TEXT], "tables": []}]}, "", "")
            t("　（対照）本文が断り書きだけで表も無ければ、いままでどおり通す",
              _dv({"slug": "zzz_t22"})["result"] in (PASS, NOT_APPLICABLE))
            # ★★★材料から記事まで一本で通す試験★★★
            #   （2026-08-25・Codexの24回目の助言）
            #   ★私の直し方の癖＝反例で足りない次元を一段ずつ後付けする★
            #   と指摘された。個々の反例を足すだけでは、
            #   「本文なし・控えあり・きれいな値」しか試していない。
            #   → ★本物の生成器（build_detail）で記事を作り、それを検査する★。
            #     2AIの確定値と、DMM単独の両方で通す。
            import build_new_article as _ba24
            import page_decision as _pd_rc

            def _thru(mat, slug24, store=None):
                """材料 → 記事 → 検査（本物の生成器を通す）。"""
                if store is not None:
                    _c = json.load(open(_cv19.STORE, encoding="utf-8"))
                    _c["machines"][slug24] = store
                    json.dump(_c, open(_cv19.STORE, "w", encoding="utf-8"),
                              ensure_ascii=False)
                _detail = _ba24.build_detail(slug24, "通し試験機", "2026-09", mat)
                _dec = _pd_rc.decide(mat)
                globals()["_machine"] = lambda sl: {
                    "slug": sl, "name": "通し試験機",
                    "release_date": "2026-09", "page_decision": _dec}
                globals()["_load_detail"] = lambda sl: (_detail, "", "")
                return _dv({"slug": slug24})["result"]

            _SS24 = {"basis": "DMM_SINGLE_NEAR_RELEASE"}
            _mat_dmm = {"adopted": {
                "payout_rate": {**_SS24, "value": {"1": "97.0%", "6": "110.0%"},
                                "sources": ["a"]}}}
            # ★2026-08-29に理由が変わった★＝以前は「名乗りがあるから免除」
            #   だったが、いまは★単独確認も濃さに数えるので話題が未確認に
            #   ならない★。免除ではなく、そもそも食い違いが起きない。
            t("★★★通し：DMM単独の設定表は、話題が未確認にならないので通る★★★"
              "／★運営者が決めた書き方なので、止まると新台が作れない★",
              _thru(_mat_dmm, "zzz_e2e_dmm") in (PASS, NOT_APPLICABLE))
            _mat_2ai = {"adopted": {
                "payout_rate": {"_from": "confirmed_values",
                                "_field": "payout_rate",
                                "value": {"1": "97.3%", "6": "110.5%"},
                                "sources": ["a"]}}}
            # ★★名乗りだけの設定表は通さない★★（2026-08-29）
            #   ★控えにも無く、値も一致しないのに
            #     「（確認1件のみ）」が付いているだけの行★
            #   ＝控えと違う値が並んでいても見逃す経路だった。
            globals()["_machine"] = lambda sl: {
                "slug": sl, "name": "試験機",
                "page_decision": {"pending_topics": ["setting"]}}
            globals()["_load_detail"] = lambda sl: ({"sections": [
                {"title": "設定示唆まとめ", "body": [],
                 "tables": [{"label": "出玉率",
                             "rows": [["設定1", "97.0%" + _mark18]]}]}]},
                "", "")
            t("★★名乗りだけの設定表は、控えと照らさずには通さない★★"
              "／★控えと違う値が並んでいても見逃す経路だった★",
              _dv({"slug": "zzz_tbl29"})["result"] == FAIL)

            t("★★★通し：2AIの確定値から作った設定表も通る★★★",
              _thru(_mat_2ai, "zzz_e2e_2ai",
                    store={"payout_rate": _rec20({"1": "97.3%",
                                                  "6": "110.5%"})})
              in (PASS, NOT_APPLICABLE))
            # ★対照★＝記事を作ったあとで表の値を書き換えたら止まる
            _detail_bad = _ba24.build_detail("zzz_e2e_2ai", "通し試験機",
                                             "2026-09", _mat_2ai)
            for _sec24 in _detail_bad.get("sections") or []:
                for _tb24 in (_sec24.get("tables") or []):
                    for _row24 in (_tb24.get("rows") or []):
                        # ★セルは文字列のことも辞書のこともある★
                        #   （新台の生成は文字列／既存記事はバッジ付きの辞書）
                        #   ★片方しか書き換えないと、対照が空振りする★
                        if isinstance(_row24[1], dict):
                            _row24[1]["text"] = "99.9%"
                        else:
                            _row24[1] = "99.9%"
            globals()["_machine"] = lambda sl: {
                "slug": sl, "name": "通し試験機", "release_date": "2026-09",
                "page_decision": _pd_rc.decide(_mat_2ai)}
            globals()["_load_detail"] = lambda sl: (_detail_bad, "", "")
            t("　（対照）記事の表を書き換えたら止まる",
              _dv({"slug": "zzz_e2e_2ai"})["result"] == FAIL)
            # ★★話題が決まっていない項目があれば、黙って進まない★★
            #   （2026-08-25・Codexの21回目。★fail-closed になっていなかった★）
            _keep_topic = _cv19.topic_of
            try:
                _cv19.topic_of = lambda f: (_ for _ in ()).throw(
                    _cv19.ConfirmedError("試験：話題が決まっていません"))
                globals()["_load_detail"] = _det20(
                    "朝一・リセット情報", ["**設定変更後の天井**：600G"])
                t("★★話題が決まっていない項目があれば、判定せず止まる★★"
                  "／★黙って飛ばすと、正しい記事を止める側に倒れる★",
                  _dv({"slug": "zzz_t20"})["result"] == ERROR)
            finally:
                _cv19.topic_of = _keep_topic
            # ★話題ちがいの確定値では免除しない★
            globals()["_machine"] = lambda sl: {
                "slug": sl, "name": "試験機",
                "page_decision": {"pending_topics": ["ceiling"]}}
            globals()["_load_detail"] = _det(["天井は周期抽選で999Gです"])
            _o = _TOPIC2TITLE["ceiling"]
            globals()["_load_detail"] = lambda sl: ({"sections": [
                {"title": _o, "body": ["天井は通常時の周期抽選からCZで999G"]}]},
                "", "")
            t("★★別の話題の確定値では免除しない★★"
              "／★話題で分けないと、ゲーム性の値で天井の断定が通る★",
              _dv({"slug": "zzz_t19"})["result"] == FAIL)
        finally:
            _cv19.STORE = _keep19
    finally:
        globals()["_machine"], globals()["_load_detail"] = _keepm, _keepd

    t("　判定書が無い機種（旧方式）は判定しない",
      _dv({"slug": "hokuto"})["result"] == NOT_APPLICABLE)
    t("★★これも観測どまり（記事を消すか claims を作り直すかは出典が要る）★★",
      CHECKS["decision_vs_body"]["closeable"] is False)

    # ★★新台はポチポチくんの名簿に無くても、ページが無効にしている★★
    #   （2026-08-24の夜・台帳#469。★新台が2晩1件も公開できなかった★）
    #   machine.html は新台経路と preview を**名簿より先に**
    #   「解析データ判明後に対応」で無効にしている。
    #   ★検査だけがそれを知らず、必ず落としていた★
    _pr = check_pochipochi_reachable
    _keepm2 = _machine
    try:
        globals()["_machine"] = lambda sl: {
            "slug": sl, "name": "新台試験",
            "publication_policy": "page-decision/v1"}
        _keepv = globals()["valid_slug"]
        globals()["valid_slug"] = lambda sl: True
        t("★★新台（page-decision/v1）は落とさない★★"
          "／★落とすと新台は1件も公開できない★",
          _pr({"slug": "zzz_new_route"})["result"] == PASS)
        globals()["_machine"] = lambda sl: {"slug": sl, "name": "旧preview",
                                            "status": "preview"}
        t("　先行記事（preview）も落とさない",
          _pr({"slug": "zzz_preview"})["result"] == PASS)
        # ★対照★＝ページからその分岐が消えたら、無効とみなさない
        #   ★本番の machine.html は1文字も触らない★（承認対象）
        _mh_real = open(os.path.join(BASE, "machine.html"),
                        encoding="utf-8").read()
        _row_new = {"publication_policy": "page-decision/v1"}
        t("　いまのページは、新台を無効にしている",
          page_disables_pochipochi(_mh_real, _row_new) is True)
        # ★分岐そのものを行ごと落とす★（2026-08-26）
        #   ★直す前は判定式の**字面**を書いていた★ので、
        #   ひな型の書き方を変えた瞬間に、対照実験が「消せなかった」ことに
        #   気づかず落ちた（実際に落ちた）。
        #   ＝見たいのは「その分岐が無くなったら False になるか」であって、
        #     どう書かれているかではない。目印は読者向けの文言にする。
        _mh_gone = chr(10).join(
            ln for ln in _mh_real.split(chr(10))
            if "解析データ判明後に対応" not in ln)
        t("　対照実験が、実際に分岐を消せている",
          _mh_gone != _mh_real and "解析データ判明後に対応" not in _mh_gone)
        t("★★（対照）ページの無効化が消えたら、無効とみなさない★★"
          "／★これが無いと『いつでもPASS』の抜け穴になる★",
          page_disables_pochipochi(_mh_gone, _row_new) is False)
        t("　v2 の機種も、いまのページは無効にしている（版を問わない）",
          page_disables_pochipochi(
              _mh_real, {"publication_policy": "page-decision/v2"}) is True)
        t("　新台でも先行記事でもない機種は、この道では通さない",
          page_disables_pochipochi(_mh_real, {"slug": "hokuto"}) is False)
        globals()["valid_slug"] = _keepv
    finally:
        globals()["_machine"] = _keepm2

    # --- 観測どまりの検査では閉じない
    t("★観測どまりの検査は、PASSでも閉じられない★",
      not closeable({"check": "evtable_vs_checker",
                     "version": CHECKS["evtable_vs_checker"]["version"],
                     "args": {"slug": "hokuto"},
                     "expected_commit": head_commit()})[0])

    # --- 存在しない機種
    r = run("settei_filled", {"slug": "zzz_not_exist"})
    t("★存在しない機種は PASS にならない★", r["result"] == NOT_APPLICABLE)

    # --- 例外は ERROR（落ちない）＋その状態では閉じられない
    keep = CHECKS["settei_filled"]["fn"]
    try:
        def boom(_args):
            raise RuntimeError("わざと壊す")
        CHECKS["settei_filled"]["fn"] = boom
        r = run("settei_filled", {"slug": _machines()[0]["slug"]})
        t("★中で落ちても ERROR で返る★", r["result"] == ERROR)
        t("★検査が落ちる状態では閉じられない★", not closeable(cond)[0])
    finally:
        CHECKS["settei_filled"]["fn"] = keep

    # --- 指紋
    a = _result(FAIL, "x", {"slug": "a"}, observed={"n": 1})
    b = _result(FAIL, "x", {"slug": "a"}, observed={"n": 2})
    t("★見たものが違えば指紋も違う★",
      a["observation_digest"] != b["observation_digest"])
    t("同じ食い違いなら名前は同じ", a["finding_key"] == b["finding_key"])
    t("★指紋は切り詰めない（64桁）★", len(a["observation_digest"]) == 64)
    t("コミットが分かる", re.fullmatch(r"[0-9a-f]{40}", a["commit_sha"] or "") is not None)

    print()
    print(f"{ok}/{total} 合格")
    return 0 if ok == total else 1


def main():
    ap = argparse.ArgumentParser(description="案件が本当に直ったかを機械が確かめ直す")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--check")
    ap.add_argument("--slug")
    ap.add_argument("--rate")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return _selftest()
    if a.list or not a.check:
        return _cmd_list()
    if not a.slug and not a.all:
        # ★JSONを頼まれたらエラーもJSONで返す★（機械が読む口を平文で汚さない）
        msg = "--slug か --all が要ります"
        print(json.dumps({"check": a.check, "error": msg}, ensure_ascii=False)
              if a.json else msg)
        return 2
    return _cmd_run(a.check, a.slug, a.rate, a.all, a.json)


if __name__ == "__main__":
    sys.exit(main())

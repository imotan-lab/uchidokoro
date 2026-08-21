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

def _cell_text(cell) -> str:
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
        return cell.strip()
    return ""


def _row_has_text(row, two_cells: bool) -> bool:
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
    return any(_cell_text(c) != "" for c in cells)


def _settei_renderable_rows(section: dict):
    """★machine.html と同じ順序で「実際に描かれる行」を数える★

    （依頼244の指摘3・防御1: 描画は
       ① `section.tables` があればそちらだけを使う（**空配列でもそちらを使う**
          ＝JavaScript では `[]` は真なので `rows` 分岐へ入らない）
       ② `tables` が無いときだけ `rows` を使う
     という順で選ぶ。Python で「空配列なら偽」と書くと規則がずれる）

    戻り値: (描ける行数, 想定外があれば理由)
    """
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
                if _row_has_text(row, two_cells=False):
                    n += 1
        return n, ""
    rows = section.get("rows")
    if rows is None:
        return 0, ""
    if not isinstance(rows, list):
        return 0, "行が配列ではありません"
    return sum(1 for row in rows if _row_has_text(row, two_cells=True)), ""


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


# --- 検査④（観測どまり）: 注記の「通常◯◯G」が実際の値と合っているか ----------
#   ★読者に見える★＝交換率を選ぶと注記が画面に出る。
#     そこに書いてある「通常450G」等が実際の狙い目と違うと、読者の判断が変わる。
#   ★観測どまりにする理由★＝注記が古いのか、値のほうが誤りかを機械が決められない。
#     どちらかへそろえるには出典が要る（2AIの仕事）。

_NOTE_NORMAL = re.compile(r"通常(\d{3,4})G")


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


# --- 検査の名簿 -----------------------------------------------------------
# ★ここに無い名前は動かない★（台帳から来た文字列でコマンドを組み立てない）

CHECKS = {
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

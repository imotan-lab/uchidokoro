# -*- coding: utf-8 -*-
"""★記事の文体が「です・ます」でそろっているか★（2026-08-31・運営者の指示）

★運営者の言葉★
> 文体は統一したいね　今後も。
> タスクが走るたびに表記変わるのは避けたい

★★負の検査をやめる★★＝
いままで文体を見ていた `recheck.plain_style_gone` は
「常体の文末**19通り**」という名簿で、名簿に無い言い方は素通りした
（対照実験で「…となる。」が通ることを確認済み・CLAUDE.mdに記載）。
ここは**正の検査**＝「です・ます で終わっていること」を求め、
外れたものを全部挙げる。★名簿が増えない形★。

★★文でないものは数えない★★
  ・ラベルと値の行（「**天井**：1200G」「等価交換：520G〜から狙い目」）
    ＝これは表へ移すもの（運営者の要望③）。文体の対象ではない。
  ・「未確認（確認でき次第掲載します）」等の決まり文句

使い方:
  python scripts/style_check.py --all            # 全機種の件数と内訳
  python scripts/style_check.py --slug hokuto    # 1機種の場所
  python scripts/style_check.py --all --json     # 機械が読む
  python scripts/style_check.py --selftest

終了コード: 0=基準値以内 / 3=基準値より増えた / 1=読めなかった
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

# ★自分の出力の文字の扱いを固定する★（台帳#525・Windowsの既定では記号が書けない）
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                      # noqa: BLE001
        pass

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DETAILS = os.path.join(BASE, "assets", "data", "machine-details")
BASELINE = os.path.join(BASE, "assets", "data", "style-baseline.json")

# 認める文末（です・ます系）。閉じ括弧・強調記号が後ろに付いてもよい。
_OK_TAIL = re.compile(
    r"(?:です|ます|ません|でした|ました|ませんでした|でしょう|ましょう|"
    r"ください|でしょうか|ますか|ですか)"
    r"[）\)」』】\*＊\s]*$")

# ラベルと値の行（文ではない）
#   「**天井**：1200G」／「等価交換：520G〜から狙い目」
#   ★短い見出しのあとに「：」が来て、文の区切り（。）が無い★
_LABEL = re.compile(r"^\s*(?:\*\*)?([^：:。、\n]{1,14})(?:\*\*)?\s*[：:]")

# 記事が使う決まり文句（文体の対象にしない）
_STOCK = ("未確認（確認でき次第掲載します）",
          "未確認です。確認でき次第、この欄に掲載します。")


def is_label_line(line: str) -> bool:
    """★ラベルと値の行か★（文ではないので文体を求めない）。

    ★「。」があれば文とみなす★＝
    「天井は1200Gです。ただし…」のような文を取りこぼさないため。
    """
    t = str(line or "").strip()
    if not t or "。" in t:
        return False
    return bool(_LABEL.match(t))


# 括弧の対応（この中の「。」では切らない）
_OPEN = "（(「『【［〈"
_CLOSE = "）)」』】］〉"


def sentence_spans(text: str) -> list:
    """★文の切れ目を「元の文字列の範囲」で返す★（2026-09-01・Codexの助言）

    返すもの: [(始まり, 終わり), ...]（終わりは「。」や改行を**含む**）

    ★なぜ範囲で返すか★＝`sentences()` は「。」を落とし、改行を消し、
    前後を strip するので**元の文字列を復元できない**。
    「落とした以外を1文字も変えていない」を機械で示すには、
    ★元の文字列をそのまま切り出せる形★が要る。

    ★切れ目は「。」と改行だけ★＝「！」「？」は機種名や引用の中に
    実例があるので境界にしない（安全側）。
    ★括弧の中の「。」では切らない★（`sentences()` と同じ）。
    ★空白だけの範囲は返さない★
    """
    t = str(text or "")
    out, start, depth = [], 0, 0
    for i, ch in enumerate(t):
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth = max(0, depth - 1)
        if depth == 0 and (ch == "。" or ch == "\n"):
            if t[start:i + 1].strip():
                out.append((start, i + 1))
            start = i + 1
    if t[start:].strip():
        out.append((start, len(t)))
    return out


def sentences(line: str) -> list:
    """段落を文へ切る。★「。」が無い段落は、それ全体を1文として見る★
    （体言止めを見つけるため）。

    ★括弧の中の「。」では切らない★（2026-08-31・実データで誤検知が出た）
      「…**（基準。天井950G+α）」を切ると、
      「天井950G+α）」という**文でない断片**が「体言止め」に見える。
    ★丸ごと括弧だけの断片は数えない★＝前の文に付く注記なので、
      それ自体に文体を求めるのは筋が違う。
    """
    t = str(line or "").replace("\n", "").strip()
    if not t:
        return []
    out, buf, depth = [], [], 0
    for ch in t:
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth = max(0, depth - 1)
        if ch == "。" and depth == 0:
            if "".join(buf).strip():
                out.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    if "".join(buf).strip():
        out.append("".join(buf).strip())
    return [x for x in out if not _only_paren(x)]


def _only_paren(text: str) -> bool:
    """その断片が、まるごと括弧1つだけか（前の文に付く注記）。"""
    t = str(text or "").strip()
    if len(t) < 2 or t[0] not in _OPEN or t[-1] not in _CLOSE:
        return False
    depth = 0
    for i, ch in enumerate(t):
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth -= 1
            if depth == 0 and i != len(t) - 1:
                return False
    return depth == 0


# 文末に付く注記の括弧（「…見込めます（8000G想定）」）
_TAIL_PAREN = re.compile(r"(?:（[^（）]*）|\([^()]*\))\s*$")


# ★強調の書き方の違いだけを吸収する★（2026-08-31・Codexの11回目）
#   ★どのタグも外す作りは広すぎた★＝`<a href="A">公式</a>` と
#   `<a href="B">公式</a>` が同じ印になり、**違反の入れ替えを隠せた**。
_STRONG = re.compile(r"</?strong>")
_SPACES = re.compile(r"\s+")


def bare(text) -> str:
    """★印に使う形★＝強調の書き方の違いだけを吸収する。

    （2026-08-31）★これが要る理由★＝
    `<strong>即ヤメ</strong>` を `**即ヤメ**` へ直しても、
    **読者が読む文は1文字も変わらない**。
    それで印が変わると、書き方をそろえるたびに監査55が赤くなり、
    ★本当に増えた違反が埋もれる★。

    ★吸収するのはここだけ★（Codexの11回目）＝
      ・`<strong>` / `</strong>` を `**` と同じに見る
      ・空白の**連なり**を1つに詰める（★全部は消さない★＝
        消すと `100` と `1 00`、`AB` と `A B` が同じ印になる）
    ★ほかのタグ・リンク先・強調の有無は印に残す★
    （読者に見えるものが変わるので、別の違反として扱う）。
    """
    t = _STRONG.sub("**", str(text or ""))
    return _SPACES.sub(" ", t).strip()


def fingerprint_v2(row: dict) -> str:
    """★乗り換えの照合にだけ使う、古い印の作り方★（v2）。

    ★残す理由★＝「承認済みの1件1件が、対応する新しい印へ移った」ことを
    確かめるには、同じデータに対して**古い作り方でも**印を作る必要がある。
    ★新しく使わない★（`write_baseline(migrate=True)` からだけ呼ぶ）。
    """
    import hashlib
    key = "\n".join([str(row.get("slug") or ""),
                      str(row.get("section") or ""),
                      "".join(str(row.get("sentence") or "").split()),
                      str(row.get("nth") or 1)])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def ok_ending(sentence: str) -> bool:
    """その文が「です・ます」で終わっているか。

    ★文末の注記の括弧は外してから見る★＝
    「約850枚のプラスが見込めます（8000G想定）」は文体としては正しい。
    ★外しすぎない★＝括弧を外して何も残らないなら、外さずに見る。
    """
    t = str(sentence or "").strip()
    for _ in range(2):
        m = _TAIL_PAREN.search(t)
        if not m:
            break
        rest = t[:m.start()].strip()
        if not rest:
            break
        t = rest
    return bool(_OK_TAIL.search(t))


def _check_text(out: list, slug: str, where: str, line, li=0) -> None:
    """1行ぶん見て、外れている文を out へ足す。"""
    if not isinstance(line, str) or not line.strip():
        return
    if line.strip() in _STOCK or is_label_line(line):
        return
    for x in sentences(line):
        if ok_ending(x):
            continue
        out.append({"slug": slug, "section": where, "line": li,
                    "sentence": x})


def problems(slug: str, detail) -> list:
    """1機種ぶんの「です・ます で終わっていない文」を挙げる。

    ★読者の画面に文章として出るものを全部見る★
    （2026-08-31・Codexの6回目の指摘）＝
    直す前は `sections[].body` だけで、
    ★`lead`（ページ上部の説明）と 表の `note` を見ていなかった★。
    実データに常体の lead がある。
    """
    out = []
    if not isinstance(detail, dict):
        return out
    # ★ページ上部の説明（lead）★
    _check_text(out, slug, "lead", detail.get("lead"))
    for si, sec in enumerate(detail.get("sections") or []):
        if not isinstance(sec, dict):
            continue
        title = str(sec.get("title") or "")
        for li, line in enumerate(sec.get("body") or []):
            _check_text(out, slug, title, line, li)
        # ★表の注記も文章として画面に出る★
        for ti, tbl in enumerate(sec.get("tables") or []):
            if isinstance(tbl, dict):
                _check_text(out, slug, title + "（表の注記）",
                            tbl.get("note"), ti)
    # ★★同じ節に同じ文が2回あっても、別々に数える★★
    #   （2026-08-31・Codexの7回目のP2）
    #   ★直す前は印が同じ★だったので、
    #   2件目を新しく足しても、片方だけ直しても集合が変わらなかった。
    _seen = {}
    for r in out:
        k = (r["slug"], r["section"], bare(r["sentence"]))
        _seen[k] = _seen.get(k, 0) + 1
        r["nth"] = _seen[k]
    return out


def fingerprint(row: dict) -> str:
    """★その違反を一意に指す印★（機種・場所・文）。

    ★件数ではなく集合で持つため★（2026-08-31・Codexの6回目の指摘）＝
    件数だけだと、古い違反を1件直して別の文に1件入れれば
    同じ数のまま通る＝★入れ替えを許す★。
    """
    import hashlib
    key = "\n".join([str(row.get("slug") or ""),
                      str(row.get("section") or ""),
                      # ★強調の記号とタグは外す★（2026-08-31）＝
                      #   読者が読む文が同じなら、同じ違反として扱う。
                      bare(row.get("sentence")),
                      # ★同じ文が同じ節に何度も出るときの通し番号★
                      str(row.get("nth") or 1)])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def scan_all() -> list:
    """全機種を見る（★読むだけ★）。"""
    out = []
    for p in sorted(glob.glob(os.path.join(DETAILS, "*.json"))):
        slug = os.path.basename(p)[:-5]
        try:
            d = _load(p)
        except Exception:                  # noqa: BLE001
            continue
        out += problems(slug, d)
    return out


def compare(rows: list, base: dict) -> dict:
    """いまの違反と基準値を**集合で**比べる。

    ★件数では足りない★（2026-08-31・Codexの6回目の指摘）＝
    古い違反を1件直して別の文に1件入れると、件数は同じままで通る
    ＝★入れ替えを許す★。運営者の要望
    「タスクが走るたびに表記変わるのは避けたい」を満たせない。

    返すもの: {"new": [...], "gone": [...], "now": N, "base": M}
    """
    now = {fingerprint(r): r for r in rows}
    want = set(str(x) for x in (base or {}).get("items") or [])
    new = sorted(set(now) - want)
    gone = sorted(want - set(now))
    return {"new": [now[h] for h in new], "gone": gone,
            "now": len(now), "base": len(want)}


SCHEMA = "style-baseline/v3"
OLD_SCHEMA = "style-baseline/v2"


def write_baseline(rows: list, path: str = None, init: bool = False,
                   migrate: bool = False) -> dict:
    """基準値を書き直す。★減った時だけ★（増やす方向には書けない）。

    ★増やせないようにする理由★＝
    「直したら基準値を下げる」運用は、下げ忘れると後戻りが許される。
    逆に「増やせる」と、違反を足してから基準値を上げれば何でも通る。
    """
    path = path or BASELINE
    now = {fingerprint(r) for r in rows}
    old = None
    if os.path.isfile(path):
        if init:
            raise SystemExit("★基準値は既にあります（初回の作成はできません）★")
        old = _load(path)
        ver = old.get("schema_version")
        if migrate:
            # ★★印の作り方を変えるときだけの乗り換え★★（2026-08-31）
            #   ★中身が動いていないことを、件数で必ず確かめる★＝
            #   印の付け替えで違反が増えたり減ったりしてはいけない。
            #   （減るのが正しいときは、乗り換えではなく普通の書き直しを使う）
            if ver != OLD_SCHEMA:
                raise SystemExit(
                    f"★乗り換え元の版が違います（{ver!r}）★")
            # ★★1件1件が移ったことを確かめる★★（2026-08-31・Codexの11回目）
            #   ★件数一致だけでは足りない★＝1189件が丸ごと別の違反に
            #   入れ替わっても通り、誤った集合が正式な基準値になる。
            #   ★同じ材料に古い作り方で印を付け直し、
            #     それが「いまの基準値」と過不足なく一致することを求める★。
            # ★★旧基準値そのものの形を先に見る★★（2026-08-31・Codexの12回目）
            #   ★`set()` にすると重複が消える★＝
            #   旧が [A, B, B]、材料が [A, B] でも一致してしまう。
            items = old.get("items")
            if not isinstance(items, list):
                raise SystemExit("★旧基準値の items が配列ではありません★")
            if len(items) != len(rows):
                raise SystemExit(
                    "★旧基準値と材料の件数が違います"
                    f"（{len(items)} / {len(rows)}）★")
            was = set(items)
            if len(was) != len(items):
                raise SystemExit("★旧基準値に重複があります★")
            mine = {fingerprint_v2(r) for r in rows}
            if len(mine) != len(rows):
                raise SystemExit(
                    "★古い作り方で印が重なりました★"
                    "（同じ材料を渡していない可能性があります）")
            if mine != was:
                raise SystemExit(
                    "★★いまの基準値と一致しません★★\n"
                    f"  基準値にあって材料に無い: {len(was - mine)} 件\n"
                    f"  材料にあって基準値に無い: {len(mine - was)} 件\n"
                    "  ★乗り換えは「基準値を作ったときと同じ材料」で行います★"
                    "（記事を直す前の版を渡してください）")
            if len(now) != len(rows):
                raise SystemExit(
                    "★新しい作り方で印が重なりました★"
                    f"（{len(rows)} 件 → {len(now)} 件）")
            old = None                      # 集合の比較はしない（鍵が違うため）
        elif ver != SCHEMA:
            raise SystemExit(
                f"★基準値の版が違います（{ver!r}）★")
    if old is not None:
        want = set(str(x) for x in (old.get("items") or []))
        extra = now - want
        if extra:
            raise SystemExit(
                f"★増えているので書き直せません（新しい違反 {len(extra)} 件）★\n"
                "  先に直してください（python scripts/style_check.py --slug <機種>）")
        if now == want:
            raise SystemExit("★減っていないので書き直す必要がありません★")
    if not os.path.isfile(path) and not init:
        # ★★基準値が無いときは黙って作らない★★
        #   （2026-08-31・Codexの7回目のP2）
        #   ★直す前は、消してから実行すると
        #     「いまの違反」をそのまま基準値にできた★
        #   ＝新しい違反を丸ごと取り込んで監査55が緑になる。
        raise SystemExit(
            "★基準値がありません★（消してしまった場合は git から戻してください）\n"
            "  本当に初めて作るときだけ --init-baseline を使います")
    import datetime as _dt
    doc = {
        "schema_version": SCHEMA,
        "_why": ("記事の文章で「です・ます」から外れている文の**集合**。"
                 "★増やさない★（audit項目55）。件数ではなく集合で持つのは、"
                 "古いのを直して別に入れる**入れ替え**を許さないため。"),
        "_how": ("python scripts/style_check.py --all で見る／"
                 "直したら --update-baseline で書き直す（減った時だけ通る）。"),
        "count": len(now),
        "machines": len({r["slug"] for r in rows}),
        "measured_at": _dt.date.today().isoformat(),
        "items": sorted(now),
    }
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
        f.write("\n")
    return {"count": doc["count"],
            "before": (len(old.get("items") or []) if old else None)}


def selftest() -> int:
    ok = []

    def t(name, cond):
        ok.append(bool(cond))
        print(("✅" if cond else "❌") + " " + name)

    t("★です・ます は通す★", ok_ending("天井は1200Gです"))
    t("　「ません」も通す", ok_ending("設定差はありません"))
    t("　閉じ括弧が後ろにあっても通す", ok_ending("約850枚のプラスが見込めます（8000G想定）"))
    t("★★名簿に無い常体も捕まえる★★"
      "（いままでの19通りの名簿では素通りしていた型）",
      not ok_ending("この場合は天井狙いとなる")
      and not ok_ending("設定6であることが確定する"))
    t("★★体言止めも捕まえる★★", not ok_ending("リセット時は天井が600G+αに短縮"))
    t("★ラベルと値の行は文体を求めない★",
      is_label_line("**天井**：1200G+α")
      and is_label_line("等価交換：ボーナス間520G〜から狙い目"))
    t("★★「。」がある行はラベルにしない★★"
      "（「天井は1200Gです。ただし…」を取りこぼさないため）",
      not is_label_line("天井：1200Gです。ただし前兆があります。"))
    t("　長い前置きのある文はラベルにしない",
      not is_label_line("この機種の通常時における天井のゲーム数：1200G"))

    t("★★括弧の中の「。」では切らない★★（実データで誤検知が出た）",
      sentences("目安です（基準。天井950G+α）")
      == ["目安です（基準。天井950G+α）"])
    t("　まるごと括弧だけの断片は数えない",
      sentences("天井は1200Gです。（スマスロ北斗）") == ["天井は1200Gです"])
    t("　ふつうの文は今までどおり切る",
      sentences("天井は1200Gです。狙い目は600Gです。")
      == ["天井は1200Gです", "狙い目は600Gです"])

    det = {"sections": [{"title": "天井・恩恵",
                         "body": ["天井は1200Gです。",
                                  "**メーカー**：サミー",
                                  "リセット時は600G+αに短縮"]}]}
    got = problems("zzz", det)
    t("★★記事から、直すべき文だけを挙げる★★",
      len(got) == 1 and got[0]["sentence"].endswith("短縮"))
    # ★★読者に出る文章を全部見る★★（2026-08-31・Codexの6回目の指摘）
    det2 = {"lead": "新台のAT機。天井は1200G",
            "sections": [{"title": "設定示唆まとめ", "type": "settei",
                          "body": [],
                          "tables": [{"label": "x", "headers": [], "rows": [],
                                      "note": "設定6は出現率が低い"}]}]}
    got2 = problems("zzz", det2)
    t("★★ページ上部の説明（lead）も見る★★"
      "（実データに常体の lead がある）",
      any(r["section"] == "lead" for r in got2))
    t("★表の注記も見る★",
      any("表の注記" in r["section"] for r in got2))
    t("　決まり文句は挙げない",
      problems("zzz", {"sections": [{"title": "x",
                                     "body": [_STOCK[0]]}]}) == [])

    # ★★入れ替えを見つける★★（2026-08-31・Codexの6回目の指摘）
    r1 = {"slug": "a", "section": "x", "sentence": "天井は1200Gに短縮"}
    r2 = {"slug": "a", "section": "x", "sentence": "狙い目は600Gから"}
    base1 = {"items": [fingerprint(r1)]}
    t("★同じものなら差は無い★", compare([r1], base1)["new"] == [])
    got = compare([r2], base1)
    t("★★件数が同じでも、入れ替わったら見つける★★"
      "（古いのを直して別に入れる、が通っていた）",
      len(got["new"]) == 1 and len(got["gone"]) == 1
      and got["now"] == got["base"])
    t("　減ったことも分かる", compare([], base1)["gone"] == [fingerprint(r1)])

    # ★★強調の書き方を直しても印が動かない★★（2026-08-31）
    #   ★これが無いと★＝生の <strong> を ** へ直すたびに
    #   「新しい違反」が湧いて監査55が赤くなり、基準値も書き直せない。
    #   （実際に20件で起きた）
    _s1 = {"slug": "a", "section": "x",
           "sentence": "ボーナス後は<strong>即ヤメ</strong>が基本"}
    _s2 = {"slug": "a", "section": "x",
           "sentence": "ボーナス後は**即ヤメ**が基本"}
    # ★★吸収しすぎていないか★★（2026-08-31・Codexの11回目）
    t("★★リンク先が違えば別の印★★（どのタグも外す作りだと同じになった）",
      fingerprint({"slug": "a", "section": "x",
                   "sentence": '<a href="https://a/">公式</a>'})
      != fingerprint({"slug": "a", "section": "x",
                      "sentence": '<a href="https://b/">公式</a>'}))
    t("★★空白の有無で数字が変われば別の印★★（全部消すと 100 と 1 00 が同じ）",
      fingerprint({"slug": "a", "section": "x", "sentence": "天井は100Gです"})
      != fingerprint({"slug": "a", "section": "x",
                      "sentence": "天井は1 00Gです"}))
    t("　空白の連なりは1つに詰める（改行や連続空白では印が動かない）",
      fingerprint({"slug": "a", "section": "x", "sentence": "天井は  100G"})
      == fingerprint({"slug": "a", "section": "x", "sentence": "天井は 100G"}))
    t("★★強調の書き方を変えただけなら印は同じ★★"
      "（<strong> を ** へ直すたびに赤くなっていた）",
      fingerprint(_s1) == fingerprint(_s2))
    _s3 = {"slug": "a", "section": "x",
           "sentence": "ボーナス後は**即ヤメ**が基本だいたい"}
    t("　言葉が変われば印も変わる（緩めすぎていない）",
      fingerprint(_s3) != fingerprint(_s2))
    _s4 = {"slug": "a", "section": "y",
           "sentence": "ボーナス後は**即ヤメ**が基本"}
    t("　場所が違えば別の印", fingerprint(_s4) != fingerprint(_s2))
    # ★★文の範囲（sentence_spans）★★（2026-09-01・Codexの助言）
    #   ★落とした以外を1文字も変えていないことを示すために要る★
    _sp = "あああ。いいい。ううう"
    _got = [_sp[a:b] for a, b in sentence_spans(_sp)]
    t("★★文の範囲は、元の文字を丸ごと切り出せる★★"
      "（「。」を落とす sentences() では証明に使えない）",
      _got == ["あああ。", "いいい。", "ううう"]
      and "".join(_got) == _sp)
    _sp2 = "天井は950G（基準。浅い）です。次の文です。"
    t("　括弧の中の「。」では切らない",
      [_sp2[a:b] for a, b in sentence_spans(_sp2)]
      == ["天井は950G（基準。浅い）です。", "次の文です。"])
    _sp3 = "一行目\n二行目。"
    t("　改行でも切る（元の改行は残る）",
      [_sp3[a:b] for a, b in sentence_spans(_sp3)] == ["一行目\n", "二行目。"])
    t("　空白だけの範囲は返さない", sentence_spans("　\n  ") == [])
    t("　どの範囲も、つなげると元に戻る（1文字も足さない・落とさない）",
      all("".join(x[a:b] for a, b in sentence_spans(x)) == x
          for x in ("あ。い。", "あ", "あ。", "あ\nい")))

    t("　bare は strong を ** に置き換えるだけ",
      bare("天井は<strong>800G</strong>+α") == "天井は**800G**+α")
    t("　bare は他のタグを残す", "<span>" in bare("<span>x</span>"))

    # ★★乗り換えは件数が同じ時だけ★★
    import tempfile as _tf8
    _p8 = os.path.join(_tf8.mkdtemp(prefix="style_mig_"), "b.json")
    with open(_p8, "w", encoding="utf-8") as _f:
        json.dump({"schema_version": OLD_SCHEMA, "items": ["a", "b"]}, _f)
    _r8 = [{"slug": "a", "section": "x", "sentence": "天井は1200Gに短縮"}]
    _r8b = _r8 + [{"slug": "a", "section": "y", "sentence": "狙い目は600Gから"}]
    # ★基準値は「その材料を古い作り方で印にしたもの」でなければならない★
    with open(_p8, "w", encoding="utf-8") as _f:
        json.dump({"schema_version": OLD_SCHEMA,
                   "items": [fingerprint_v2(r) for r in _r8b]}, _f)
    try:
        write_baseline(_r8, _p8, migrate=True)
        _ng8 = False
    except SystemExit:
        _ng8 = True
    t("★★材料が基準値と一致しない乗り換えは断る★★"
      "（件数一致だけだと、丸ごと入れ替わっても通った）", _ng8)
    _bad8 = [{"slug": "a", "section": "x", "sentence": "別の文A"},
             {"slug": "a", "section": "y", "sentence": "別の文B"}]
    try:
        write_baseline(_bad8, _p8, migrate=True)
        _ng8b = False
    except SystemExit:
        _ng8b = True
    t("★★件数が同じでも中身が入れ替わっていたら断る★★"
      "（1189件が丸ごと別物でも通っていた）", _ng8b)
    # ★★旧基準値そのものの形を見る★★（2026-08-31・Codexの12回目）
    #   ★set() にすると重複が消える★＝[A,B,B] と [A,B] が一致し得た
    # ★★どの守りが働いたかまで見る★★（2026-08-31・壊し方の確認で判明）
    #   ★理由まで見ないと、隣の守りが断っているだけでも試験が通る★
    def _why8(rows, items):
        with open(_p8, "w", encoding="utf-8") as _f:
            json.dump({"schema_version": OLD_SCHEMA, "items": items}, _f)
        try:
            write_baseline(rows, _p8, migrate=True)
            return ""
        except SystemExit as e:
            return str(e)

    _dup = fingerprint_v2(_r8b[0])
    t("★★旧基準値に重複があれば断る★★（set にすると重複が消えて通った）",
      "重複があります" in _why8(_r8b, [_dup, _dup]))
    t("　旧基準値と材料の件数が違えば断る",
      "件数が違います" in _why8(_r8b, [_dup]))
    t("　旧基準値の items が配列でなければ断る",
      "配列ではありません" in _why8(_r8b, "配列でない"))
    # ★試験の材料を元に戻す★（後ろの試験が使うため）
    with open(_p8, "w", encoding="utf-8") as _f:
        json.dump({"schema_version": OLD_SCHEMA,
                   "items": [fingerprint_v2(r) for r in _r8b]}, _f)
    write_baseline(_r8b, _p8, migrate=True)
    with open(_p8, encoding="utf-8") as _f:
        _d8 = json.load(_f)
    t("　件数が同じなら乗り換えられる（版も上がる）",
      _d8["schema_version"] == SCHEMA and len(_d8["items"]) == 2)
    try:
        write_baseline(_r8b, _p8, migrate=True)
        _ng8c = False
    except SystemExit:
        _ng8c = True
    t("　乗り換えは2度できない（版が上がっているため）", _ng8c)
    t("　印は機種・場所・文で決まる",
      fingerprint(r1) != fingerprint({**r1, "slug": "b"})
      and fingerprint(r1) == fingerprint({**r1, "line": 99}))

    # ★★同じ節に同じ文が2回あっても、別々に数える★★
    dup = {"sections": [{"title": "x",
                         "body": ["天井は1200Gに短縮", "天井は1200Gに短縮"]}]}
    got_d = problems("a", dup)
    t("★★同じ文が2回あれば2件として数える★★"
      "（印が同じだと、片方を直しても集合が変わらない）",
      len(got_d) == 2
      and fingerprint(got_d[0]) != fingerprint(got_d[1]))

    # ★★基準値が無いときは黙って作らない★★
    import tempfile as _tf7
    _d7 = _tf7.mkdtemp(prefix="style_base_")
    _p7 = os.path.join(_d7, "b.json")
    _rows7 = [{"slug": "a", "section": "x", "sentence": "短縮", "nth": 1}]
    _made = True
    try:
        write_baseline(_rows7, _p7)
    except SystemExit:
        _made = False
    t("★★基準値が無いのに書き直そうとしたら断る★★"
      "（消してから実行すると、新しい違反を丸ごと取り込めた）", not _made)
    write_baseline(_rows7, _p7, init=True)
    t("　初回だけは作れる", os.path.isfile(_p7))
    _again = True
    try:
        write_baseline(_rows7, _p7, init=True)
    except SystemExit:
        _again = False
    t("　もうあるのに初回として作ろうとしたら断る", not _again)
    _same = True
    try:
        write_baseline(_rows7, _p7)
    except SystemExit:
        _same = False
    t("　減っていないなら書き直さない", not _same)

    ng = ok.count(False)
    print(f"{len(ok) - ng}/{len(ok)} 合格")
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="記事の文体（です・ます）を見る")
    ap.add_argument("--slug")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--update-baseline", action="store_true",
                    help="★直したあと★基準値を書き直す（減った時だけ通る）")
    ap.add_argument("--init-baseline", action="store_true",
                    help="★初めて作るときだけ★（既にあるときは断る）")
    ap.add_argument("--migrate-baseline", action="store_true",
                    help="★印の作り方を変えたときだけ★（件数が同じなら通る）")
    a = ap.parse_args()
    if a.selftest:
        return selftest()

    if a.update_baseline or a.init_baseline or a.migrate_baseline:
        got = write_baseline(scan_all(), init=a.init_baseline,
                             migrate=a.migrate_baseline)
        print(f"基準値を書き直しました: {got['before']} → {got['count']} 件")
        return 0

    if a.slug:
        p = os.path.join(DETAILS, a.slug + ".json")
        if not os.path.isfile(p):
            print(f"★{a.slug} の記事データがありません★")
            return 1
        rows = problems(a.slug, _load(p))
    else:
        rows = scan_all()

    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0

    if a.slug:
        print(f"{a.slug}: です・ます で終わっていない文 {len(rows)} 件")
        for r in rows:
            print(f"  [{r['section']}] {r['sentence'][-46:]}")
        return 0

    by_slug = {}
    for r in rows:
        by_slug[r["slug"]] = by_slug.get(r["slug"], 0) + 1
    print(f"です・ます で終わっていない文: {len(rows)} 件 / {len(by_slug)} 機種")
    for sl, n in sorted(by_slug.items(), key=lambda x: -x[1])[:12]:
        print(f"  {n:4d}  {sl}")
    try:
        base = _load(BASELINE)
    except Exception as e:                 # noqa: BLE001
        print(f"★基準値を読めません: {e}★")
        return 1
    d = compare(rows, base)
    if d["new"]:
        print(f"★新しい違反が {len(d['new'])} 件あります★"
              f"（いま {d['now']} 件／基準 {d['base']} 件）")
        for r in d["new"][:8]:
            print(f"  [{r['slug']}／{r['section']}] {r['sentence'][-40:]}")
        return 3
    if d["gone"]:
        print(f"直りました（{d['base']} → {d['now']}）。"
              "python scripts/style_check.py --update-baseline "
              "で基準値を書き直してください")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

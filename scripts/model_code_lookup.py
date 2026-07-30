"""model_code_lookup.py — 名鑑から機種の「型式名」を引く。

★なぜ要るか（2026-07-31）★
  メーカー公式ページには型式名が載っていないことが多い（登場年月だけ）。
  一方で名鑑（P-WORLD・DMMぱちタウン）には**導入前から**型式名が載る。

  以前は「型式は導入前には無い」と思い込んでいたが、それは
  **誤った機種名で検索していたため見つからなかっただけ**だった。
  実際、Lすーぱぁびん娘（2026-08-03導入）は導入前に
  P-WORLD・DMM・ゼンリンの3件に「Lびん娘NY1」として載っていた。

★引くときの名前はメーカー公式のものを使う★
  まとめサイトの名前で引くと取り違える（「ビンゴライブ」という
  実在しない名前で探して空振りした実例がある）。
  メーカー公式の一覧から取った正式名称だけを使う。

★同じ機種だと認めるための条件★
  名前が一致しただけでは足りない。続編・パチンコ版・L版と無印がある。
  そこで**名前の芯が完全に一致**することを求め、さらに
  **独立2つの名鑑で型式名が一致**して初めて採用する。

使い方:
    python scripts/model_code_lookup.py --url https://www.p-world.co.jp/machine/database/10496 \\
                                        --name "Lすーぱぁびん娘"
    python scripts/model_code_lookup.py --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import claim_identity as _ci          # noqa: E402
import new_machine_watch as _w        # noqa: E402

# 型式名が書かれている形（★見出しの次の行に値がある形もある★）
_LABELS = ("型式名", "型式")
# 型式名として認める形。★これ以外は採らない★（許可した形だけ通す）
#   英数字・記号・かな・漢字が混じる短い1行。文や説明を拾わない。
_CODE_OK = re.compile(r"^[0-9A-Za-zぁ-んァ-ヶ一-龥ー･・／/＋+\-−–—．.　 ]{2,40}$")
# 明らかに型式名ではない語（見出しの取り違え防止）
_CODE_NG = ("記載なし", "不明", "未定", "調査中")


class LookupError_(RuntimeError):
    pass


def extract_model_code(html: str):
    """名鑑ページの本文から型式名を1つ取り出す。決まらなければ None と理由。"""
    lines = _w._visible_text(html).splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        for lab in _LABELS:
            if not s.startswith(lab):
                continue
            # 「型式名：Lびん娘NY1」の形
            rest = s[len(lab):].lstrip("：: 　").strip()
            cand = rest
            if not cand and i + 1 < len(lines):
                # 「型式名 :」の次の行に値がある形（P-WORLD）
                cand = lines[i + 1].strip()
            if not cand:
                continue
            if cand in _CODE_NG:
                return None, "MODEL_CODE_NOT_STATED"
            if not _CODE_OK.match(cand):
                continue          # 説明文などを拾ってしまった。次の候補へ
            return unicodedata.normalize("NFKC", cand), "OK"
    return None, "MODEL_CODE_NOT_FOUND"


def page_is_machine(html: str, official_name: str):
    """★その名鑑ページが本当にその機種か★（名前の芯が完全一致すること）

    `claim_identity.normalize_core` で表記ゆれ（スマスロ/L/全角半角など）だけを
    落とした「芯」を作り、**名鑑のタイトルがその芯から始まる**ことを求める。
    タイトルには「(スマスロ) パチスロ新台 … | P-WORLD」のような
    サイト側の飾りが続くので、完全一致ではなく前方一致にする。

    ★前方一致でも続編は落ちる★
      「すーぱぁびん娘」の芯で「すーぱぁびん娘2…」は始まるので通ってしまう。
      そこで**芯の直後が数字や続編を表す文字でないこと**も確かめる。
    """
    title = _w.page_title(html)
    if not title:
        return False, "PAGE_TITLE_MISSING"
    core = _ci.normalize_core(official_name)
    if not core:
        return False, "OFFICIAL_NAME_HAS_NO_CORE"
    tcore = _ci.normalize_core(title)
    if not tcore.startswith(core):
        return False, "NAME_CORE_MISMATCH"
    rest = tcore[len(core):]
    # ★続編・改称を本人と誤認しない★（芯の直後に版を表す文字が続く）
    if rest[:1] in tuple("01234567892３４５６７８９ivxⅱⅲ") or             rest[:2] in ("ii", "iv", "vi"):
        return False, "SEQUEL_SUSPECTED"
    return True, "OK"


def lookup(url: str, official_name: str) -> dict:
    """1つの名鑑ページから型式名を引く。★機種が違えば採らない★"""
    out = {"url": url, "official_name": official_name,
           "model_code": None, "reason": ""}
    try:
        html = _w._get(url)
    except Exception as e:
        out["reason"] = f"取得できません: {e}"
        return out
    ok, why = page_is_machine(html, official_name)
    if not ok:
        out["reason"] = why
        return out
    code, why = extract_model_code(html)
    out["model_code"] = code
    out["reason"] = why
    return out


def agree(results: list) -> dict:
    """★独立2つ以上の名鑑で型式名が一致して初めて採用する★"""
    codes = {}
    for r in results:
        if r.get("model_code"):
            host = r["url"].split("/")[2].lower().removeprefix("www.")
            codes.setdefault(r["model_code"], set()).add(host)
    for code, hosts in codes.items():
        if len(hosts) >= 2:
            return {"model_code": code, "hosts": sorted(hosts), "adopted": True}
    return {"model_code": None, "adopted": False,
            "why": ("独立2つの名鑑で一致しません: "
                    + json.dumps({k: sorted(v) for k, v in codes.items()},
                                 ensure_ascii=False))}


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    nl = chr(10)
    t("★『型式名：値』の形から取れる★",
      extract_model_code("<p>型式名：Lびん娘NY1</p>")[0] == "Lびん娘NY1")
    t("★★見出しの次の行に値がある形からも取れる★★（P-WORLDがこの形）",
      extract_model_code("<p>型式名  :</p><p>Lびん娘NY1</p>")[0] == "Lびん娘NY1")
    t("　全角は揃える",
      extract_model_code("<p>型式名：Ｌびん娘ＮＹ１</p>")[0] == "Lびん娘NY1")
    t("★『記載なし』を型式名にしない★",
      extract_model_code("<p>型式名：記載なし</p>") == (None, "MODEL_CODE_NOT_STATED"))
    t("　型式の記載が無ければ理由を返す",
      extract_model_code("<p>導入日：2026年8月3日</p>")[1] == "MODEL_CODE_NOT_FOUND")
    t("★説明文を型式名として拾わない★",
      extract_model_code(
          "<p>型式名：この機種の型式については後日公表される予定となっています。"
          "なお導入は8月です。</p>")[0] is None)

    t("★★独立2つの名鑑で一致して初めて採用★★",
      agree([{"url": "https://www.p-world.co.jp/x", "model_code": "Lびん娘NY1"},
             {"url": "https://p-town.dmm.com/y", "model_code": "Lびん娘NY1"}])["adopted"]
      is True)
    t("　1つだけでは採用しない",
      agree([{"url": "https://www.p-world.co.jp/x",
              "model_code": "Lびん娘NY1"}])["adopted"] is False)
    t("★同じサイトの2ページを2票と数えない★",
      agree([{"url": "https://www.p-world.co.jp/x", "model_code": "A1"},
             {"url": "https://p-world.co.jp/y", "model_code": "A1"}])["adopted"] is False)
    t("　食い違ったら採用しない（理由を残す）",
      agree([{"url": "https://www.p-world.co.jp/x", "model_code": "A1"},
             {"url": "https://p-town.dmm.com/y", "model_code": "B2"}])["adopted"] is False)

    t("★★一致する場合はちゃんと通る★★（全部落ちていて気づかない事故を防ぐ）",
      page_is_machine("<title>Lすーぱぁびん娘(スマスロ) パチスロ新台 | P-WORLD</title>",
                      "Lすーぱぁびん娘") == (True, "OK"))
    t("　全角・サイト名つきでも通る",
      page_is_machine("<title>Ｌすーぱぁびん娘｜DMMぱちタウン</title>",
                      "Lすーぱぁびん娘")[0] is True)
    t("★★続編を本人と誤認しない★★（前方一致だけだと通ってしまう）",
      page_is_machine("<title>Lすーぱぁびん娘2 | P-WORLD</title>",
                      "Lすーぱぁびん娘") == (False, "SEQUEL_SUSPECTED"))
    t("★名前の芯が違うページからは採らない★",
      page_is_machine("<title>Lスーパービンゴネオ|P-WORLD</title>",
                      "Lすーぱぁびん娘")[0] is False)
    t("　タイトルが無ければ採らない",
      page_is_machine("<p>本文だけ</p>", "Lすーぱぁびん娘")[0] is False)

    ng = [n for n, ok in results if not ok]
    print(f"{nl}{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--url", action="append", help="名鑑ページのURL（複数指定可）")
    ap.add_argument("--name", help="メーカー公式の正式名称")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.url or not args.name:
        ap.print_help()
        return 0
    rs = [lookup(u, args.name) for u in args.url]
    for r in rs:
        print(f"{r['url']}{chr(10)}  型式名={r['model_code']!r} 理由={r['reason']}")
    v = agree(rs)
    print(chr(10) + json.dumps(v, ensure_ascii=False, indent=1))
    return 0 if v["adopted"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(f"★想定外の失敗 {type(e).__name__}: {e}★")
        raise SystemExit(1)

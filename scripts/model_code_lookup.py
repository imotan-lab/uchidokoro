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


# ★題を区切る記号★（サイト側の飾りを切り離すため）
#   ★「・」「-」は入れない★＝機種名そのものに使われる
#   （「すーぱぁびん娘・極」を「すーぱぁびん娘」と「極」に割ると別機種を本人にしてしまう）
_TITLE_SEPS = "|｜(（)）[［]］【】/／<＞>＜"


def title_parts(title: str) -> list:
    """題を区切って、機種名らしいかたまりに分ける。"""
    out, buf = [], []
    for ch in title or "":
        if ch in _TITLE_SEPS:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return [x.strip() for x in out if x.strip()]


# ★機種名のすぐ後ろに来てよい語★（題の飾り）
#   ここに無い語が名前の直後に来たら、**別の機種**とみなす。
#   「モンキーターン V」の "V"、「すーぱぁびん娘 SP」の "SP" を止めるため。
_DECOR = {
    "新台", "天井", "解析", "スペック", "設定", "判別", "設定判別", "設定差",
    "設定示唆", "やめどき", "ヤメ時", "やめ時", "狙い目", "初打ち", "打ち方",
    "機械割", "導入日", "設置店", "掲示板", "有利区間", "期待値", "評価",
    "感想", "演出", "攻略", "実践", "動画", "画像", "一覧", "情報", "恩恵",
    "ボーナス", "フリーズ", "ちょんぼりすた", "pworld", "ぱちタウン", "dmm",
    "パチンコ", "パチスロ解析", "解析情報", "スロット新台",
}
_DECOR_CORES = {_ci.normalize_core(w) or w for w in _DECOR}

# ★題を区切る記号★（サイト側の飾りを切り離す）
#   ★「・」「-」は入れない★＝機種名そのものに使われる
#   （「すーぱぁびん娘・極」を割ると、別機種を本人にしてしまう）
_TITLE_SEPS = "|｜(（)）[［]］【】/／<＞>＜、,"


def title_parts(title: str) -> list:
    """題を区切って、機種名らしいかたまりに分ける。"""
    out, buf = [], []
    for ch in title or "":
        if ch in _TITLE_SEPS:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return [x.strip() for x in out if x.strip()]


def page_is_machine(html: str, official_name: str):
    """★その名鑑ページが本当にその機種か★

    ★ただの前方一致をやめた★（2026-07-31・Codex22回目。実際に再現した）
      以前は「題の芯が指定名の芯で**始まる**こと」だけを見て、
      数字と続編記号しか弾いていなかった。そのため
        「すーぱぁびん娘新章」「すーぱぁびん娘SP」「すーぱぁびん娘・極」
      がどれも本人として通り、**別機種の公式URLと指定名を組み合わせて**
      記事を作れる穴になっていた。

    いまは題を「区切り記号」と「空白」で語に分け、
      ①続いた語をつないだものが、指定名の芯と**丸ごと同じ**
      ②その次の語が、飾り（新台・天井・解析…）か、区切りか、題の終わり
    の両方を求める。②が無いと「モンキーターン V」を
    「モンキーターン」として通してしまう。

    実データで通ることを確かめた形:
      「L青春ブタ野郎は…(スマスロ 青ブタ) パチスロ新台 … | P-WORLD」
      「スマスロ 甲鉄城のカバネリ 海門(うなと)決戦 パチスロ新台 …」← 名前に括弧
      「スマスロ 真打吉宗 スロット 新台 … | ちょんぼりすた …」
      「スマスロ東京喰種 スロット 新台 … 東京グール | ちょんぼりすた …」← 別名つき
    """
    title = _w.page_title(html)
    if not title:
        return False, "PAGE_TITLE_MISSING"
    core = _ci.normalize_core(official_name)
    if not core:
        return False, "OFFICIAL_NAME_HAS_NO_CORE"
    # ★題そのものも候補に入れる★（機種名の中に括弧が入ることがある）
    #   「甲鉄城のカバネリ 海門(うなと)決戦」は、区切ると名前が割れてしまう。
    for seg in [title] + title_parts(title):
        words = [_ci.normalize_core(w) for w in seg.split()]
        for i in range(len(words)):
            joined = ""
            for j in range(i, len(words)):
                joined += words[j]
                if joined != core:
                    continue
                # ★次の語を見る★（飾りか、そこで終わりならOK）
                k = j + 1
                while k < len(words) and words[k] == "":
                    k += 1               # 販売区分語などは芯が空になる
                if k >= len(words) or words[k] in _DECOR_CORES:
                    return True, "OK"
    return False, "NAME_CORE_MISMATCH"


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
    # ★食い違いを先に見る★（2026-07-31・Codex22回目。実際に再現した）
    #   以前は「2票そろった型式」を見つけた時点で採用していたので、
    #   A=2票・B=1票 のときAをそのまま採り、食い違いに気づかなかった。
    #   型式が食い違う＝別の機種の資料が混じっているので、材料ごと信用できない。
    if len(codes) >= 2:
        return {"model_code": None, "adopted": False, "state": "CONFLICT",
                "why": "名鑑ごとに型式名が食い違っています: "
                       + json.dumps({k: sorted(v) for k, v in codes.items()},
                                    ensure_ascii=False)}
    for code, hosts in codes.items():
        if len(hosts) >= 2:
            return {"model_code": code, "hosts": sorted(hosts), "adopted": True}
    # ★「まだ載っていない」と「食い違う」を分ける★（2026-07-31・Codex21回目）
    #   どちらも同じ文言だったので、
    #   **明日には載るかもしれない新台**まで「やり直しても無駄」と扱い、
    #   初回で待ち行列から外していた。
    detail = json.dumps({k: sorted(v) for k, v in codes.items()},
                        ensure_ascii=False)
    if len(codes) >= 2:
        return {"model_code": None, "adopted": False, "state": "CONFLICT",
                "why": f"名鑑ごとに型式名が食い違っています: {detail}"}
    if not codes:
        return {"model_code": None, "adopted": False, "state": "NOT_YET",
                "why": "型式名がまだどの名鑑にも載っていません"}
    return {"model_code": None, "adopted": False, "state": "NOT_YET",
            "why": f"型式名が1つの名鑑にしか載っていません: {detail}"}


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
                      "Lすーぱぁびん娘")[0] is False)
    # ★★ここから Codex22回目の反例★★（前方一致＋数字だけの検査を通っていた）
    for _bad in ("Lすーぱぁびん娘新章 | P-WORLD", "Lすーぱぁびん娘SP | P-WORLD",
                 "Lすーぱぁびん娘・極 | P-WORLD", "Lすーぱぁびん娘 SP | P-WORLD",
                 "Lすーぱぁびん娘 改 パチスロ新台 | P-WORLD"):
        t(f"★★別機種を本人にしない: {_bad[:22]}★★",
          page_is_machine(f"<title>{_bad}</title>", "Lすーぱぁびん娘")[0] is False)
    t("★★名前の中の括弧を割らない★★（実データ・甲鉄城のカバネリ）",
      page_is_machine("<title>スマスロ 甲鉄城のカバネリ 海門(うなと)決戦 "
                      "パチスロ新台 スロット 機械割</title>",
                      "スマスロ 甲鉄城のカバネリ 海門(うなと)決戦")[0] is True)
    t("★★別名が題に入っていても通る★★（実データ・東京喰種／東京グール）",
      page_is_machine("<title>スマスロ東京喰種 スロット 新台 天井 設定判別 解析 "
                      "東京グール | ちょんぼりすた パチスロ解析</title>",
                      "L 東京喰種")[0] is True)
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

"""directory_index.py — 名鑑の一覧から「機種名 → 個別ページURL」の索引を作る。

★なぜ要るか（2026-07-31）★
  名鑑のURLを人が手で探していたので、自動タスクが回らなかった。
  メーカー公式から取った正式名称で、この索引を引いて個別ページを見つける。

★検索エンジンは使わない★
  順位や要約に引きずられる。実際、検索結果から「ビンゴライブ」という
  **実在しない機種名**を拾って空振りした。名鑑の一覧を直接読む。

★入口は1つでは足りない（実データで確認）★
  ちょんぼりすたは、全機種一覧に `Lすーぱぁびん娘` が無く、
  メーカー別一覧（/slot/belko-slot/）にはある。
  P-WORLD は /database/machine.html と導入カレンダーの両方にある。
  だから **1つの名鑑につき複数の入口を和集合で見る**。

★「見つからない」と「まだ載っていない」を分ける★
  一覧が壊れて0件になったのを「新台なし」と読むのが一番こわい。
  返す状態を分ける:

    FOUND                個別ページを1つに決められた
    HEALTHY_NO_MATCH     一覧は正常に読めたが、その時点では載っていない
    AMBIGUOUS_CANDIDATES 候補が複数あって決められない → ★止める★
    CATALOG_UNHEALTHY    一覧を正常に読めていない → ★新台なしと扱わない★

使い方:
    python scripts/directory_index.py --name "Lすーぱぁびん娘"
    python scripts/directory_index.py --check chonborista
    python scripts/directory_index.py --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import claim_identity as _ci          # noqa: E402
import new_machine_watch as _w        # noqa: E402
import safe_json as _sj               # noqa: E402

CATALOGS = os.path.join(BASE, "assets", "data", "directory-catalogs.json")

# 一覧のリンク文字が「2026年5月22日 Lすーぱぁびん娘 スロット 新台 天井 …」の形なので、
# 先頭の日付と、後ろに続く記事タイトルの飾りを落として機種名だけにする。
_DATE_HEAD = re.compile(r"^\s*20\d\d年\s*\d{1,2}月\s*\d{1,2}日\s*")
_TITLE_TAIL = ("設定判別", "解析まとめ", "終了画面", "徹底解説", "打ち方", "狙い目",
               "やめどき", "ヤメ時", "ゾーン", "スペック", "期待値", "情報",
               "設置店", "掲示板", "初打ち", "機械割", "スロット", "パチスロ",
               "スマスロ", "新台", "天井", "解析", "まとめ", "攻略", "示唆",
               "評価", "考察")
# 飾り語どうしをつなぐ記号・助詞（これだけが残るなら「飾りだけの塊」とみなす）
_TAIL_JOINERS = set("・、/｜|()（） 　をとのー:：!！?？")


def _decor_only(token: str) -> bool:
    """その塊（空白なし）が記事タイトルの飾りだけでできているか。"""
    t = token
    for w in sorted(_TITLE_TAIL, key=len, reverse=True):
        t = t.replace(w, " ")
    return bool(token) and all(ch in _TAIL_JOINERS or ch == " " for ch in t)

STATES = ("FOUND", "HEALTHY_NO_MATCH", "AMBIGUOUS_CANDIDATES", "CATALOG_UNHEALTHY")


def anchor_core(text: str) -> str:
    """一覧のリンク文字から「機種名の芯」を作る。

    ★記事タイトルの飾りを落とす★
      落とさないと芯が「すーぱぁびん娘新台天井設定判別…」まで伸びて、
      正式名称の芯と一致しなくなる（実データで確認）。

    ★飾りは「右端から・語境界を保って」剥がす★（2026-08-02・Codex51回目）
      最初に現れた飾り語で切ると、機種名の中の「パチスロ」「スロット」で
      名前ごと消える。実在の「Lパチスロうる星やつら」は芯が空になり、
      「Lパチスロ からくりサーカス2」はちょんぼりすたの実一覧で
      既に取りこぼしていた（両方とも実ページで確認）。
    """
    t = _DATE_HEAD.sub("", " ".join(str(text or "").split()))
    # ｜は語の区切りとして扱う（「機種名｜天井・設定判別…」の形が実在）
    toks = [x for x in re.split(r"[ ｜|]+", t) if x]
    # 右端から「飾りだけの塊」を落とす。機種名に届いたら止まる
    while toks and _decor_only(toks[-1]):
        toks.pop()
    t = " ".join(toks)
    # ベタ付きの飾り（…びん娘新台天井）は、末尾に飾り語が2語以上
    # 連続する時だけ剥がす。1語では剥がさない（「アニマルスロット」等、
    # 機種名の一部かもしれないため）
    stripped, n = t, 0
    while True:
        hit = next((w for w in sorted(_TITLE_TAIL, key=len, reverse=True)
                    if stripped.endswith(w) and len(stripped) > len(w)), None)
        if not hit:
            break
        stripped = stripped[:-len(hit)].rstrip("".join(_TAIL_JOINERS))
        n += 1
    if n >= 2 and stripped:
        t = stripped
    return _ci.normalize_core(t)


def build_index(html: str, base_url: str, link_pattern: str) -> dict:
    """1つの入口から {機種名の芯: [(URL, 元の文字), ...]} を作る。

    ★リンクはHTML解析で読む★（2026-08-02・Codex52回目）
      href=\"...\" の正規表現だと単一引用符のリンクだけを黙って見落とし、
      既存リンクが十分あるページでは面が健全に見えたまま
      新台だけが HEALTHY_NO_MATCH で欠落する。
      解析できなければ0件＝最低件数の警報側に倒れる。
    """
    idx: dict = {}
    rx = re.compile(link_pattern)
    for href, text in (_w._visible_anchor_pairs(html) or []):
        if not rx.search(href):
            continue
        core = anchor_core(text)
        if not core:
            continue
        url = urllib.parse.urljoin(base_url, href).split("#")[0].split("?")[0]
        idx.setdefault(core, [])
        if url not in [u for u, _ in idx[core]]:
            idx[core].append((url, " ".join(text.split())[:60]))
    return idx


def scan_directory(dir_id: str, conf: dict) -> dict:
    """1つの名鑑の全入口を見て、索引と健全性を返す。"""
    out = {"directory": dir_id, "name": conf.get("name"), "index": {},
           "surfaces_ok": 0, "surfaces_total": 0, "problems": []}
    least = int(conf.get("min_expected") or 1)
    for sf in conf.get("surfaces") or []:
        out["surfaces_total"] += 1
        try:
            html = _w._get(sf["url"])
        except Exception as e:
            out["problems"].append(f"{sf['url']}: 取得できません（{e}）")
            continue
        idx = build_index(html, sf["url"], conf["link_pattern"])
        if len(idx) < least:
            # ★ここが黙って0件になる事故を止める砦★
            out["problems"].append(
                f"{sf['url']}: {len(idx)} 件しか取れません（最低 {least} 件のはず）")
            continue
        out["surfaces_ok"] += 1
        for core, items in idx.items():          # ★入口どうしは和集合★
            cur = out["index"].setdefault(core, [])
            for it in items:
                if it[0] not in [u for u, _ in cur]:
                    cur.append(it)
    return out


def find(official_name: str, catalogs: dict | None = None) -> dict:
    """正式名称から、各名鑑の個別ページURLを探す。"""
    cats = catalogs or _sj.read_json(CATALOGS, expect=dict)["directories"]
    core = _ci.normalize_core(official_name)
    out = {"official_name": official_name, "core": core, "results": {}}
    if not core:
        out["results"]["_"] = {"state": "CATALOG_UNHEALTHY",
                               "why": "正式名称から芯を作れません"}
        return out
    for dir_id, conf in cats.items():
        if conf.get("status") != "ACTIVE":
            continue
        r = scan_directory(dir_id, conf)
        hits = list(r["index"].get(core) or [])
        if not hits:
            # ★世代表記の同値化★（2026-08-02・Codex50回目。公式「…2」↔名鑑「…II」）
            ck = _ci.canon_num_tail(core)
            for k, v in r["index"].items():
                if k != core and _ci.canon_num_tail(k) == ck:
                    hits += v
        if r["surfaces_ok"] == 0:
            state, why = "CATALOG_UNHEALTHY", " / ".join(r["problems"])
        elif len(hits) == 1:
            state, why = "FOUND", ""
        elif len(hits) > 1:
            # ★順位や「新しい方」で選ばない★（Codexの指摘・自分でも妥当と判断）
            state, why = "AMBIGUOUS_CANDIDATES", f"候補が {len(hits)} 件あります"
        else:
            state, why = "HEALTHY_NO_MATCH", "正常に読めた一覧に載っていません"
        out["results"][dir_id] = {
            "state": state, "why": why,
            "url": hits[0][0] if state == "FOUND" else None,
            "candidates": [u for u, _ in hits],
            "surfaces": f"{r['surfaces_ok']}/{r['surfaces_total']}",
            "index_size": len(r["index"]),
            "problems": r["problems"],
        }
    return out


def found_urls(result: dict) -> list:
    return [v["url"] for v in result["results"].values()
            if v["state"] == "FOUND" and v["url"]]


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []
    nl = chr(10)

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    t("★★一覧の記事タイトルから機種名だけを取り出す★★（実データの形）",
      anchor_core("2026年5月22日 Lすーぱぁびん娘 スロット 新台 天井 設定判別 やめどき 解析まとめ")
      == _ci.normalize_core("Lすーぱぁびん娘"))
    t("　日付が無くても取れる",
      anchor_core("Lすーぱぁびん娘(スマスロ) パチスロ新台")
      == _ci.normalize_core("Lすーぱぁびん娘"))
    t("　飾りしか無ければ空を返す", anchor_core("スロット 新台 天井") == "")
    t("★★機種名の中の「パチスロ」で名前ごと消えない★★（実在・Codex51回目）",
      anchor_core("Lパチスロうる星やつら") == _ci.normalize_core("Lパチスロうる星やつら")
      and anchor_core("2026年7月20日 Lパチスロ からくりサーカス2｜天井 スペック 設定判別 解析 評価")
      == _ci.normalize_core("Lパチスロ からくりサーカス2"))
    t("　機種名の中の「スロット」でも切らない（Lアニマルスロット ドッチ）",
      anchor_core("Lアニマルスロット ドッチ 新台 天井 設定判別")
      == _ci.normalize_core("Lアニマルスロット ドッチ"))
    t("　｜区切りの飾り列も落とす",
      anchor_core("Lタクトオーパス デスティニー｜天井・設定判別・終了画面・ヤメ時を徹底解説")
      == _ci.normalize_core("Lタクトオーパス デスティニー"))
    t("　ベタ付きの飾りは2語以上の時だけ剥がす",
      anchor_core("Lすーぱぁびん娘新台天井設定判別") == _ci.normalize_core("Lすーぱぁびん娘")
      and anchor_core("Lアニマルスロット") == _ci.normalize_core("Lアニマルスロット"))

    HTML = ('<a href="/slot/belko-slot/260918/">2026年5月22日 Lすーぱぁびん娘 スロット 新台</a>'
            '<a href="/slot/belko-slot/111111/">Lスーパービンゴネオ スロット 解析</a>'
            '<a href="/about/">会社案内</a>'
            '<a href="/slot/belko-slot/260918/?utm=1">Lすーぱぁびん娘 まとめ</a>')
    idx = build_index(HTML, "https://d.example/slot/", r"/slot/[a-z\-]+/\d+")
    key = _ci.normalize_core("Lすーぱぁびん娘")
    t("★機種名から個別ページURLを引ける★",
      [u for u, _ in idx.get(key, [])] == ["https://d.example/slot/belko-slot/260918/"])
    t("　機種ページでないリンクは索引に入れない",
      all("about" not in u for v in idx.values() for u, _ in v))
    t("　同じURLは1件にまとめる（?や#が付いていても）",
      len(idx.get(key, [])) == 1)
    t("　別機種は別の項目になる",
      _ci.normalize_core("Lスーパービンゴネオ") in idx)
    idx_q = build_index(
        "<a href=\"/slot/belko-slot/1/\">L既存機 スロット 新台</a>"
        "<a href='/slot/belko-slot/2/'>L新台機 スロット 新台</a>",
        "https://d.example/slot/", r"/slot/[a-z\-]+/\d+")
    t("★★単一引用符のリンクも見落とさない★★"
      "（新台だけ欠落しても面が健全に見えた・Codex52回目）",
      _ci.normalize_core("L新台機") in idx_q
      and _ci.normalize_core("L既存機") in idx_q)
    t("　非表示のリンクは索引に入れない",
      _ci.normalize_core("L隠し機") not in build_index(
          '<div hidden><a href="/slot/belko-slot/3/">L隠し機 スロット</a></div>',
          "https://d.example/slot/", r"/slot/[a-z\-]+/\d+"))

    CONF = {"name": "t", "link_pattern": r"/slot/[a-z\-]+/\d+", "min_expected": 2,
            "status": "ACTIVE",
            "surfaces": [{"url": "https://d.example/slot/"},
                         {"url": "https://d.example/slot/belko-slot/"}]}
    real = _w._get
    try:
        _w._get = lambda u, timeout=20: HTML                       # noqa: E731
        r = find("Lすーぱぁびん娘", {"d": CONF})
        t("★★2つの入口を和集合で見る★★", r["results"]["d"]["surfaces"] == "2/2")
        t("　見つかれば FOUND と URL を返す",
          r["results"]["d"]["state"] == "FOUND"
          and r["results"]["d"]["url"].endswith("/260918/"))
        r2 = find("Lまったく別の機種", {"d": CONF})
        t("★★載っていないだけなら HEALTHY_NO_MATCH★★（異常と混ぜない）",
          r2["results"]["d"]["state"] == "HEALTHY_NO_MATCH")

        _w._get = lambda u, timeout=20: '<a href="/x">y</a>'       # noqa: E731
        r3 = find("Lすーぱぁびん娘", {"d": CONF})
        t("★★一覧が読めないときは『載っていない』と言わない★★",
          r3["results"]["d"]["state"] == "CATALOG_UNHEALTHY")
        t("　その理由が残る", bool(r3["results"]["d"]["problems"]))

        DUP = ('<a href="/slot/a/1/">Lすーぱぁびん娘 スロット</a>'
               '<a href="/slot/b/2/">Lすーぱぁびん娘 スロット</a>'
               '<a href="/slot/c/3/">Lべつの機種 スロット</a>')
        _w._get = lambda u, timeout=20: DUP                        # noqa: E731
        r4 = find("Lすーぱぁびん娘", {"d": CONF})
        t("★★候補が複数なら決めずに止める★★（順位や新しい方で選ばない）",
          r4["results"]["d"]["state"] == "AMBIGUOUS_CANDIDATES"
          and len(r4["results"]["d"]["candidates"]) == 2)
        t("　止めたときは URL を返さない", r4["results"]["d"]["url"] is None)
    finally:
        _w._get = real

    t("　状態の名前が想定どおり",
      set(STATES) == {"FOUND", "HEALTHY_NO_MATCH", "AMBIGUOUS_CANDIDATES",
                      "CATALOG_UNHEALTHY"})

    ng = [n for n, ok in results if not ok]
    print(f"{nl}{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--name", help="メーカー公式の正式名称")
    ap.add_argument("--check", help="1つの名鑑の索引だけ作って健全性を見る")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    cats = _sj.read_json(CATALOGS, expect=dict)["directories"]
    if args.check:
        conf = cats.get(args.check)
        if not conf:
            print(f"★{args.check} は directory-catalogs.json にありません★")
            return 1
        r = scan_directory(args.check, conf)
        print(f"{r['name']}: 入口 {r['surfaces_ok']}/{r['surfaces_total']} / "
              f"索引 {len(r['index'])} 件")
        for p in r["problems"]:
            print("  ✗ " + p)
        return 1 if r["surfaces_ok"] == 0 else 0
    if args.name:
        r = find(args.name)
        for did, v in r["results"].items():
            print(f"{did:14} {v['state']:22} 入口{v['surfaces']} 索引{v['index_size']:>4}件"
                  f"  {v['url'] or v['why']}")
        urls = found_urls(r)
        print(f"{chr(10)}見つかった個別ページ: {len(urls)} 件")
        for u in urls:
            print("  " + u)
        return 0 if urls else 1
    ap.print_help()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except _sj.SafeJsonError as e:
        print(f"★入力データが読めません: {e}★")
        raise SystemExit(1)
    except Exception as e:
        print(f"★想定外の失敗 {type(e).__name__}: {e}★")
        raise SystemExit(1)

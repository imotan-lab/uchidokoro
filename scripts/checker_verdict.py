# -*- coding: utf-8 -*-
"""★2AIが決めた狙い目の線を、チェッカーへ書く★（2026-09-18）

★なぜ要るか（実測）★
  新台経路の16機種は**1件も狙い目の線を持っていなかった**
  （`good` が在る機種 0／16・一覧の文も全部が空）。
  ＝★このサイトの看板である狙い目チェッカーが、自動で作った機種では
    まるごと動いていなかった★。
  材料を集める処理（`build_new_article.build_checker`）が作れるのは
  「50枚あたりG数」と「G数天井がちょうど1つのときの天井」だけで、
  ★狙い目の線（good / caution / excellent）を作る道がどこにも無かった★。

★運営者の指示（2026-09-18）★
  ＞ あと、チェッカーの編集も撮ってきた情報で修正あるならやってね

★機械と2AIの分け方★（鉄則0z＝既定は2AI）
  | | やること |
  |---|---|
  | 2AI | 狙い目を何Gにするか決める。理由を書く |
  | 機械 | 決まった形で書く／数の大小の筋が通っているか／
  |      | ★天井を発明していないか（記事に在る数字か）★／読み直して確かめる |

  ★機械は「いくつが妥当か」を決めない★＝それは意味の判断。
  ★機械が断るのは構造だけ★＝順序が逆・天井を超える・記事に無い天井、など。

★狙い目の文は書かない★＝一覧や記事に出る文字は `target_display.py` が
  この線から作る（★手書きしない★という決まりの唯一の出どころ）。

使い方
  python scripts/checker_verdict.py --slug <slug>          # 材料を見る
  python scripts/checker_verdict.py --apply <決定ファイル>   # 書く
  python scripts/checker_verdict.py --apply <決定ファイル> --dry-run
  python scripts/checker_verdict.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import safe_json as _sj                                      # noqa: E402

MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
DETAILS = os.path.join(BASE, "assets", "data", "machine-details")

# ★線の名前★＝浅いほうから。読者の画面もこの順で色が変わる。
LEVELS = ("caution", "good", "excellent")
MAX_VALUE = 20000
MIN_WHY = 15
# ★モードの鍵は英小文字と数字と下線だけ★（置き場の外を触られないため）
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")


def _judges_required() -> tuple:
    """★判断者の契約は `confirmed_values` から読む★（罠③＝同じ規則を2か所に書かない）"""
    import confirmed_values as _cv
    return tuple(str(x).lower() for x in _cv.REQUIRED_JUDGES)


def _machines() -> list:
    return _sj.read_json(MACHINES, expect=list)


def _find(ms: list, slug: str):
    for m in ms:
        if str(m.get("slug") or "") == slug:
            return m
    return None


def _lead_int(v):
    m = re.match(r"^(\d{2,5})", str(v or "").strip())
    n = int(m.group(1)) if m else None
    return n if n is not None and 0 < n <= MAX_VALUE else None


def known_ceilings(slug: str, m: dict | None = None) -> set:
    """★その機種で「天井」として確かめてある数字★（天井を発明させないため）

    ★★記事データの全文から数字を拾ってはいけない★★
      （2026-09-18・Codexの指摘・実データで確かめた）＝
      直す前は記事JSON全体を `\\d{2,5}` で拾っていたので、
      ★slugの機種番号（5100）・導入年（2026）・確率の分母・機械割★まで
      「記事に在る天井」として通っていた。＝歯止めになっていなかった。

    ★見るのは構造化された3か所だけ★
      ①2AIが記録した早見表の天井（`checker_ceiling.games`）
      ②2AIが記録した天井（`ceiling.value.amount` の先頭の数字・G数のみ）
      ③その機種のチェッカーに既に入っている天井（`<mode>.ceiling`）

    ★★②は「ceiling」という名前だけを見てはいけない★★
      （2026-09-18・Codexの指摘・実データで確かめた）＝
      1機種に天井は複数あるので、控えの項目名は
      `ceiling#bonus` `ceiling#cz` のように**見出し付き**になる。
      ★`got.get("ceiling")` だけを読むと、そういう機種は
      「天井が1つも確かめられていません」と誤って断る★
      （実測＝ssb1 は 899G と 560G を記録済みなのに 0件だった）。
      ★見出しの切り離しは `confirmed_values.base_field` に任せる★
      （同じ規則を2か所に書かない）。
      ★G数の天井だけを採る★＝`ssb1` の `ceiling#point` は
      1000pt なので混ぜない（狙い目の線はG数で引く）。
    """
    out = set()
    try:
        import confirmed_values as _cv
        got = _cv.for_slug_checked(slug) or {}
    except Exception:                                        # noqa: BLE001
        got = {}                          # ★読めなければ「無い」扱い（断る側へ倒す）★
        _cv = None
    v = ((got.get("checker_ceiling") or {}).get("value") or {}).get("games")
    if (n := _lead_int(v)) is not None:
        out.add(n)
    for _key in sorted(got):
        if _cv is None or _cv.base_field(_key) != "ceiling":
            continue
        c = (got.get(_key) or {}).get("value") or {}
        if str(c.get("kind") or "") != "GAME" or str(c.get("unit") or "") != "G":
            continue                       # ★pt・周期・スルーは混ぜない★
        if (n := _lead_int(c.get("amount"))) is not None:
            out.add(n)
    ck = (m or {}).get("checker") or {}
    if isinstance(ck, dict):
        for md in (ck.get("modes") or []):
            if not isinstance(md, dict):
                continue
            key = str(md.get("key") or "")
            conf = (ck.get("modeData") or {}).get(key) or ck.get(key) or {}
            if isinstance(conf, dict) and (n := _lead_int(conf.get("ceiling"))):
                out.add(n)
    return out


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def decision_problems(dec, ms=None) -> list:
    """★決定ファイルが受け取れる形か★（★1件でも駄目なら何も書かない★）"""
    ng = []
    if not isinstance(dec, dict):
        return ["決定ファイルが辞書ではありません"]
    slug = str(dec.get("slug") or "")
    if not KEY_RE.match(slug or " ") and not re.match(r"^[a-z0-9_]+$", slug):
        ng.append("slug が英小文字・数字・下線ではありません")
    ms = _machines() if ms is None else ms
    m = _find(ms, slug)
    if m is None:
        return ng + [f"{slug}: machines.json にありません"]
    if "publication_policy" not in m:
        # ★旧形式は触らない★＝すでに線があり、書き換える理由が無い。
        #   ★誤爆を構造で止める★（「気をつける」にしない）。
        ng.append(f"{slug}: 新台経路の機種ではありません（旧形式は触りません）")
    want = set(_judges_required())
    who = {str(x).lower() for x in (dec.get("judges") or [])}
    if not want <= who:
        ng.append("判断者に %s が要ります（いま: %s）"
                  % ("・".join(sorted(want)), "・".join(sorted(who)) or "なし"))
    if len(str(dec.get("why") or "").strip()) < MIN_WHY:
        ng.append(f"理由（why）が {MIN_WHY} 字以上ありません")
    ck = m.get("checker") or {}
    # ★★いまはG数の機種だけ★★（2026-09-18）＝天井の読み取りがG数だけなので、
    #   pt・周期の機種に同じ数字を書くと**単位の違う線**が入る。
    #   ★そういう機種は、確かめた天井が0件になって下で断られる★が、
    #   別の欄のG数天井がたまたま記録されていると通ってしまうので明示的に断る。
    #   ★要るようになったら、そのとき実データで作る★（罠㉖）。
    if str(ck.get("unit") or "G") != "G":
        ng.append(f"{slug}: G数以外の機種（{ck.get('unit')}）はまだ受け取れません")
    if ck.get("exchangeRates"):
        # ★交換率ごとの線は、いまは受け取らない★＝新台経路に該当が0件で、
        #   ★一度も本物で動かしていない道を作らない★（罠㉖）。
        #   要るようになったら、そのとき実データで作る。
        ng.append(f"{slug}: 交換率の切替を持つ機種はまだ受け取れません")
    modes = dec.get("modes")
    if not isinstance(modes, list) or not modes:
        return ng + ["modes が1件もありません"]
    # ★★天井が1つも確かめられていない機種では、線を決めない★★
    #   （2026-09-18・Codexの指摘）★直す前★＝`ceiling` は任意だったので、
    #   ★`ceiling` を書かずに `good: 20000` と書けば何の検査も走らなかった★。
    #   狙い目は「どこまで深ければ座ってよいか」なので、天井が分からない
    #   うちは決めようがない。★先に天井を確かめる★（同じ回に問いが出ている）。
    nums = known_ceilings(slug, m)
    if not nums:
        return ng + [f"{slug}: 天井が1つも確かめられていません"
                     "（先に天井を確かめてください。天井なしの機種は"
                     "設定狙い用のチェッカーを使います）"]
    cap = max(nums)
    seen = set()
    for i, md in enumerate(modes):
        tag = f"modes[{i}]"
        if not isinstance(md, dict):
            ng.append(f"{tag}: 辞書ではありません")
            continue
        key = str(md.get("key") or "")
        if not KEY_RE.match(key):
            ng.append(f"{tag}: key「{key}」が英小文字・数字・下線ではありません")
        if key in seen:
            ng.append(f"{tag}: key「{key}」が2回出てきます")
        seen.add(key)
        if not str(md.get("label") or "").strip():
            ng.append(f"{tag}: label（読者に出る呼び名）が空です")
        vals = {}
        for lv in LEVELS:
            if lv not in md:
                continue
            v = _int(md.get(lv))
            if v is None or not (0 < v <= MAX_VALUE):
                ng.append(f"{tag}: {lv} が 1〜{MAX_VALUE} の整数ではありません")
                continue
            vals[lv] = v
        if "good" not in vals:
            ng.append(f"{tag}: good（狙い目）がありません")
        # ★浅い→深いの順になっているか★（意味ではなく大小の筋）
        order = [vals[lv] for lv in LEVELS if lv in vals]
        if order != sorted(order):
            ng.append(f"{tag}: caution ≦ good ≦ excellent の順になっていません")
        # ★★そのモードの天井を超えない★★（2026-09-18・Codexの2回目の指摘）
        #   ★直す前は「全部の天井のいちばん深いところ」だけを見ていた★ので、
        #   `normal` の天井が600・`at` の天井が1000の機種で、
        #   `normal: {"good": 900}`（ceiling を書かない）が通り、
        #   ★書いたあとは good 900 > その欄の天井 600 のチェッカーになった★。
        #   ★そのモードの天井＝決定に書いてあればそれ／無ければ既にある値★
        eff = _int(md.get("ceiling")) if "ceiling" in md else mode_ceiling(m, key)
        if "ceiling" in md:
            ce = _int(md.get("ceiling"))
            if ce is None or not (0 < ce <= MAX_VALUE):
                ng.append(f"{tag}: ceiling が 1〜{MAX_VALUE} の整数ではありません")
                eff = None
            elif ce not in nums:
                # ★確かめていない天井は受け取らない★（2AIに数字を作らせない）
                ng.append(f"{tag}: 天井 {ce} は確かめた記録にありません"
                          f"（確かめてあるのは {sorted(nums)}）")
                eff = None
        # ★そのモードの天井が分からないときだけ、いちばん深いところで抑える★
        #   （リセット後のように、天井を別に記録していないモード用）
        limit, why = (eff, "その欄の天井") if eff else (cap, "確かめてある天井の"
                                                         "いちばん深いところ")
        for lv in LEVELS:
            if lv in vals and vals[lv] > limit:
                ng.append(f"{tag}: {lv}（{vals[lv]}）が、{why}（{limit}）を"
                          "超えています")
    if ng:
        return ng
    # ★★当てたあとの完成形でも、もう一度だけ見る★★（2026-09-18）
    #   ★これは受け皿★＝いまの `merged()` では上の欄ごとの検査と同じ答えになる。
    #   置いておく理由は、あとで当て方（欄の足し方・値の移し方）が増えたとき、
    #   書く前にもう一度だけ完成形で見られるようにするため。
    #   ★決定が触った欄だけを見る★＝元から入っていた値の問題で、
    #   関係のない直しまで断らないため（罠⓸＝直す道が無くなる）。
    #   ★同じ理由づけは `decide_now` の「書く直前の照合」にもある★
    touched = {str(x.get("key") or "") for x in (dec.get("modes") or [])
               if isinstance(x, dict)}
    return [p for p in merged_problems(merged(m, dec))
            if any(f"当てたあと: {k} の" in p for k in touched)]


def mode_ceiling(m: dict | None, key: str):
    """★その欄に既に入っている天井★（`modeData` の下も見る）"""
    ck = (m or {}).get("checker") or {}
    if not isinstance(ck, dict):
        return None
    conf = (ck.get("modeData") or {}).get(key) or ck.get(key) or {}
    return _lead_int(conf.get("ceiling")) if isinstance(conf, dict) else None


def merged_problems(m: dict) -> list:
    """★当てたあとの姿が、読者の道具として筋が通っているか★

    ★見るのは1つだけ★＝どの欄でも `good`（と前後の線）がその欄の天井を
    超えていないこと。★決定ファイルの検査だけでは覆えない★＝
    既に入っている天井と、新しく書く線の組み合わせで壊れるため。
    """
    ng = []
    ck = (m or {}).get("checker") or {}
    if not isinstance(ck, dict):
        return ng
    for md in (ck.get("modes") or []):
        if not isinstance(md, dict):
            continue
        key = str(md.get("key") or "")
        conf = (ck.get("modeData") or {}).get(key) or ck.get(key) or {}
        if not isinstance(conf, dict):
            continue
        ce = _lead_int(conf.get("ceiling"))
        if ce is None:
            continue
        for lv in LEVELS:
            v = _int(conf.get(lv))
            if v is not None and v > ce:
                ng.append(f"当てたあと: {key} の {lv}（{v}）が"
                          f"その欄の天井（{ce}）を超えます")
    return ng


def has_target_line(m: dict) -> bool:
    """★読者に狙い目が1つでも出る姿になっているか★

    ★見るのは `good`★＝`target_display.effective_good` と同じ鍵。
    交換率ごと（`byRate`）に入っている機種もあるので、そちらも見る。
    """
    ck = (m or {}).get("checker")
    if not isinstance(ck, dict):
        return False
    for md in (ck.get("modes") or []):
        if not isinstance(md, dict):
            continue
        key = str(md.get("key") or "")
        conf = (ck.get("modeData") or {}).get(key) or ck.get(key) or {}
        if not isinstance(conf, dict):
            continue
        if "good" in conf:
            return True
        br = conf.get("byRate")
        if isinstance(br, dict) and any(
                isinstance(v, dict) and "good" in v for v in br.values()):
            return True
        # ★回数系（スルー・周期）は行ごとに線を持つ★
        for kind in ("suru", "cycle"):
            arr = conf.get(kind)
            if isinstance(arr, list) and any(
                    isinstance(r, dict) and "good" in r for r in arr):
                return True
    return False


def target_line_questions(m: dict) -> list:
    """★狙い目の線が無い機種を、2AIへの問いにする★（2026-09-18）

    ★なぜ「載っているか」で分けないか★＝検索に載っていても、線が無ければ
    読者の画面には狙い目が1つも出ない。「まだ載っていない機種だけ聞く」枠に
    入れると、★載った瞬間に聞かれなくなり、永久に空のまま★になる。
    """
    if not isinstance(m, dict) or "publication_policy" not in m:
        return []                          # ★旧形式は触らない★
    if has_target_line(m):
        return []
    slug = str(m.get("slug") or "")
    return ["★この機種の狙い目（チェッカーの線）を決めてください★"
            "／★いまは線が1つも無いので、読者の画面に狙い目が出ていません★"
            f"／材料を見る: python scripts/checker_verdict.py --slug {slug}"
            "／決めたら決定ファイルをWriteツールで書いて "
            "python scripts/checker_verdict.py --apply <決定ファイル> "
            "で書き込んでください"
            "（★判断者は claude と codex の両方／理由は15字以上／"
            "天井は記事に書いてある数字と同じでないと受け取りません★）"
            "／★一覧や記事に出る文は手で書かないでください★"
            "＝`target_display.py` がこの線から作ります"]


def merged(m: dict, dec: dict) -> dict:
    """★決定を当てた後の姿★（★書かずに作るだけ★＝見るだけの回と同じ答えにする）"""
    out = json.loads(json.dumps(m, ensure_ascii=False))
    ck = dict(out.get("checker") or {})
    ck.setdefault("unit", "G")
    modes = list(ck.get("modes") or [])
    have = {str(x.get("key") or "") for x in modes if isinstance(x, dict)}
    for md in dec.get("modes") or []:
        key = str(md.get("key") or "")
        if key not in have:
            modes.append({"key": key, "label": str(md.get("label") or "")})
            have.add(key)
        else:
            for x in modes:
                if isinstance(x, dict) and str(x.get("key") or "") == key:
                    x["label"] = str(md.get("label") or x.get("label") or "")
        conf = dict(ck.get(key) or {})
        for lv in LEVELS:
            if lv in md:
                conf[lv] = md[lv]
        if "ceiling" in md:
            conf["ceiling"] = md["ceiling"]
        # ★target は good と同じ値★（読者の画面の「目安」表示に使う）
        if "good" in md:
            conf["target"] = md["good"]
        ck[key] = conf
    ck["modes"] = modes
    out["checker"] = ck
    return out


def apply_decision(path: str, dry_run: bool = False) -> int:
    dec = _sj.read_json(path, expect=dict)
    ms = _machines()
    ng = decision_problems(dec, ms)
    if ng:
        print("★書きません★（1件でも通らなければ何も書かない）")
        for x in ng:
            print("  ・" + x)
        return 1
    slug = str(dec.get("slug") or "")
    i = next(i for i, m in enumerate(ms) if str(m.get("slug") or "") == slug)
    new = merged(ms[i], dec)
    if new == ms[i]:
        print(f"{slug}: すでにその線になっています（書きません）")
        return 0
    if dry_run:
        print(f"{slug}: こう書きます →")
        print("  " + json.dumps(new.get("checker"), ensure_ascii=False)[:600])
        return 0
    ms[i] = new
    tmp = MACHINES + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ms, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, MACHINES)
    # ★書いたら読み直して確かめる★（書けたつもりで終わらない）
    back = _find(_machines(), slug)
    if back != new:
        print("★書いた後の読み直しが合いません★（手で確かめてください）")
        return 1
    print(f"{slug}: 狙い目の線を書きました "
          f"（{'・'.join(str(x.get('key')) for x in dec.get('modes') or [])}）")
    print("★次にやること★ python scripts/target_display.py --check"
          "（一覧の文は手で書かない）")
    return 0


def show(slug: str) -> int:
    """★2AIが読む材料を出す★（★決めない★＝いくつにするかは2AIの仕事）"""
    m = _find(_machines(), slug)
    if m is None:
        print(f"{slug}: machines.json にありません")
        return 1
    print(f"=== {slug} / {m.get('name')} ===")
    print("いまのチェッカー:",
          json.dumps(m.get("checker") or {}, ensure_ascii=False)[:700])
    print("いまの一覧の文:", repr(m.get("strategy") or ""))
    path = os.path.join(DETAILS, f"{slug}.json")
    if os.path.exists(path):
        d = _sj.read_json(path, expect=dict)
        for sec in (d.get("sections") or []):
            if str(sec.get("title") or "") in ("天井・恩恵", "基本スペック"):
                print("---", sec.get("title"), "---")
                print(str(sec.get("body") or "")[:700])
    try:
        import confirmed_values as _cv
        got = _cv.for_slug_checked(slug)
        print("--- 2AIで確定済みの値 ---")
        for k in sorted(got or {}):
            print("  ", k, "=",
                  json.dumps(got[k], ensure_ascii=False)[:160])
    except Exception as e:                                   # noqa: BLE001
        print("--- 2AIで確定済みの値 --- 読めません:", type(e).__name__, e)
    print("""
--- 決定ファイルの形（Writeツールで書いてください）---
{
  "slug": "%s",
  "judges": ["claude", "codex"],
  "why": "<なぜこの線にしたか・15字以上>",
  "modes": [
    {"key": "normal", "label": "通常", "ceiling": 1000,
     "excellent": 850, "good": 700, "caution": 600}
  ]
}
★ceiling は記事に書いてある天井と同じ数字でないと受け取りません★
★caution ≦ good ≦ excellent ≦ ceiling★
""" % slug)
    return 0


def selftest() -> int:
    ok = [0, 0]

    def t(name, cond):
        ok[1] += 1
        if cond:
            ok[0] += 1
            print("✅", name)
        else:
            print("❌", name)

    base_ms = [{"slug": "zzz_auto", "name": "試験機",
                "publication_policy": "page-decision/v1",
                "checker": {"unit": "G",
                            "modes": [{"key": "normal", "label": "通常"}],
                            "normal": {}}},
               {"slug": "zzz_legacy", "name": "旧形式",
                "checker": {"unit": "G",
                            "modes": [{"key": "normal", "label": "通常"}],
                            "normal": {"good": 500}}}]

    def dec(**kw):
        d = {"slug": "zzz_auto", "judges": ["claude", "codex"],
             "why": "天井1000Gで恩恵はAT確定のため",
             "modes": [{"key": "normal", "label": "通常",
                        "good": 700, "caution": 600}]}
        d.update(kw)
        return d

    # ★確かめてある天井は、本物の読み取り関数を差し替えて渡す★
    #   （罠①＝自作の材料を自分で採点しない。読み取りそのものは下で本物を動かす）
    real = globals()["known_ceilings"]
    globals()["known_ceilings"] = lambda s, m=None: {1000}
    try:
        t("ふつうの決定は通る", not decision_problems(dec(), base_ms))
        t("★判断者が1つだと通らない★",
          any("判断者" in x
              for x in decision_problems(dec(judges=["claude"]), base_ms)))
        t("　判断者を2回書いても通らない",
          any("判断者" in x
              for x in decision_problems(dec(judges=["claude", "claude"]),
                                         base_ms)))
        t("★理由が短いと通らない★",
          any("理由" in x for x in decision_problems(dec(why="短い"), base_ms)))
        t("★旧形式は触らない★",
          any("旧形式" in x
              for x in decision_problems(dec(slug="zzz_legacy"), base_ms)))
        t("★順序が逆だと通らない★",
          any("順になっていません" in x for x in decision_problems(
              dec(modes=[{"key": "normal", "label": "通常",
                          "good": 600, "caution": 700}]), base_ms)))
        t("★確かめていない天井は通らない★",
          any("確かめた記録にありません" in x for x in decision_problems(
              dec(modes=[{"key": "normal", "label": "通常", "ceiling": 1234,
                          "good": 700}]), base_ms)))
        t("　確かめてある天井なら通る",
          not decision_problems(
              dec(modes=[{"key": "normal", "label": "通常", "ceiling": 1000,
                          "good": 700}]), base_ms))
        t("★天井を超える線は通らない★",
          any("超えています" in x for x in decision_problems(
              dec(modes=[{"key": "normal", "label": "通常", "ceiling": 1000,
                          "good": 700, "excellent": 1500}]), base_ms)))
        # ★★`ceiling` を書かなければ検査が走らない、という抜け道★★
        #   （2026-09-18・Codexの指摘。★直す前はこれが通っていた★）
        t("★★天井を書かずに深い線を入れても通らない★★"
          "（★`ceiling` を省けば検査が走らない、という抜け道★）",
          any("いちばん深いところ" in x for x in decision_problems(
              dec(modes=[{"key": "normal", "label": "通常",
                          "good": 20000}]), base_ms)))
        t("　天井を書かなくても、天井より浅ければ通る",
          not decision_problems(
              dec(modes=[{"key": "reset", "label": "リセット後",
                          "good": 400}]), base_ms))
        # ★★欄ごとに天井が違う機種★★（2026-09-18・Codexの2回目の指摘）
        #   ★直す前★＝「全部の天井のいちばん深いところ」だけを見ていたので、
        #   浅い欄に深い線を入れても通り、★書いたあとは good > その欄の天井★。
        _two = [{"slug": "zzz_auto", "name": "試験機",
                 "publication_policy": "page-decision/v1",
                 "checker": {"unit": "G",
                             "modes": [{"key": "normal", "label": "通常"},
                                       {"key": "at", "label": "AT間"}],
                             "normal": {"ceiling": 600},
                             "at": {"ceiling": 1000}}}]
        globals()["known_ceilings"] = lambda s, m=None: {600, 1000}
        # ★「当てたあと」の受け皿ではなく、欄ごとの検査が捕まえること★
        #   （罠③＝守りが二重だと、片方を壊しても試験が緑のまま）
        t("★★浅い欄に、その欄の天井を超える線は入れられない★★"
          "（★直す前は全部の天井の最大値だけを見ていた★）",
          any("その欄の天井" in x and not x.startswith("当てたあと")
              for x in decision_problems(
                  dec(modes=[{"key": "normal", "label": "通常", "good": 900}]),
                  _two)))
        t("　同じ値でも、天井が深い欄なら通る",
          not decision_problems(
              dec(modes=[{"key": "at", "label": "AT間", "good": 900}]), _two))
        # ★当てたあとの姿でも見る（決定ファイルだけでは覆えない組み合わせ）★
        t("★当てたあとの姿でも、その欄の天井を超えていないか見る★",
          merged_problems(
              {"checker": {"modes": [{"key": "normal"}],
                           "normal": {"ceiling": 600, "good": 900}}})
          and not merged_problems(
              {"checker": {"modes": [{"key": "normal"}],
                           "normal": {"ceiling": 600, "good": 500}}}))
        t("　その欄に天井が無ければ、当てたあとの検査は何も言わない",
          not merged_problems(
              {"checker": {"modes": [{"key": "normal"}],
                           "normal": {"good": 900}}}))
        globals()["known_ceilings"] = lambda s, m=None: {1000}
        t("★good が無いと通らない★",
          any("good" in x for x in decision_problems(
              dec(modes=[{"key": "normal", "label": "通常",
                          "caution": 600}]), base_ms)))
        t("★呼び名が空だと通らない★",
          any("label" in x for x in decision_problems(
              dec(modes=[{"key": "normal", "label": "  ",
                          "good": 700}]), base_ms)))
        t("★置き場の外を指す key は通らない★",
          any("key" in x for x in decision_problems(
              dec(modes=[{"key": "../etc", "label": "通常",
                          "good": 700}]), base_ms)))
        t("★同じ key を2回書くと通らない★",
          any("2回" in x for x in decision_problems(
              dec(modes=[{"key": "normal", "label": "通常", "good": 700},
                         {"key": "normal", "label": "通常", "good": 500}]),
              base_ms)))
        t("★G数以外の機種（pt・周期）は、まだ受け取らない★"
          "（★単位の違う線が入る★）",
          any("G数以外" in x for x in decision_problems(
              dec(), [{"slug": "zzz_auto", "name": "試験機",
                       "publication_policy": "page-decision/v1",
                       "checker": {"unit": "pt",
                                   "modes": [{"key": "normal",
                                              "label": "通常"}]}}])))
        t("　単位が書いていなければG数として扱う（既定）",
          not decision_problems(
              dec(), [{"slug": "zzz_auto", "name": "試験機",
                       "publication_policy": "page-decision/v1",
                       "checker": {"modes": [{"key": "normal",
                                              "label": "通常"}]}}]))
        t("★交換率を持つ機種は、まだ受け取らない★",
          any("交換率" in x for x in decision_problems(
              dec(), [{"slug": "zzz_auto", "name": "試験機",
                       "publication_policy": "page-decision/v1",
                       "checker": {"unit": "G",
                                   "exchangeRates": [{"key": "eq56"}],
                                   "modes": [{"key": "normal",
                                              "label": "通常"}]}}])))
        # ── 当てた後の姿
        got = merged(base_ms[0], dec())
        conf = (got.get("checker") or {}).get("normal") or {}
        t("当てると good と caution が入る",
          conf.get("good") == 700 and conf.get("caution") == 600)
        t("　target は good と同じ値になる", conf.get("target") == 700)
        t("　元の姿は変わらない（写しの上で作る）",
          not (base_ms[0].get("checker") or {}).get("normal"))
        got2 = merged(base_ms[0],
                      dec(modes=[{"key": "reset", "label": "リセット後",
                                  "good": 300}]))
        keys = [x.get("key") for x in (got2.get("checker") or {}).get("modes")]
        t("★無いモードは足す。ある分は消さない★",
          keys == ["normal", "reset"])
        # ★2回当てても同じ姿★（罠㉘）
        t("★2回当てても同じ姿になる★",
          merged(merged(base_ms[0], dec()), dec()) == merged(base_ms[0], dec()))
        # ★天井が1つも確かめられていなければ、線を決めさせない★
        globals()["known_ceilings"] = lambda s, m=None: set()
        t("★★天井が1つも確かめられていない機種では、線を決めない★★",
          any("天井が1つも確かめられていません" in x
              for x in decision_problems(dec(), base_ms)))
        globals()["known_ceilings"] = lambda s, m=None: {1000}
    finally:
        globals()["known_ceilings"] = real

    # ── ★狙い目の線が無い機種を問いにする★
    t("★線が1つも無ければ聞く★", len(target_line_questions(base_ms[0])) == 1)
    t("　線があれば聞かない",
      not target_line_questions(merged(base_ms[0], dec())))
    t("　旧形式には聞かない", not target_line_questions(base_ms[1]))
    t("★検索に載っていても、線が無ければ聞く★"
      "（★載った瞬間に聞かれなくなると、永久に空のまま★）",
      len(target_line_questions(
          dict(base_ms[0], page_decision={"indexable": True}))) == 1)
    t("　交換率ごとに線があれば聞かない",
      not target_line_questions(
          {"slug": "zzz_auto", "publication_policy": "page-decision/v1",
           "checker": {"modes": [{"key": "normal"}],
                       "normal": {"byRate": {"eq56": {"good": 700}}}}}))
    t("　回数系（スルー）の行に線があれば聞かない",
      not target_line_questions(
          {"slug": "zzz_auto", "publication_policy": "page-decision/v1",
           "checker": {"modes": [{"key": "suru"}],
                       "suru": {"suru": [{"count": 3, "good": 0}]}}}))
    t("　modeData の下に線があっても聞かない",
      not target_line_questions(
          {"slug": "zzz_auto", "publication_policy": "page-decision/v1",
           "checker": {"modes": [{"key": "normal"}],
                       "modeData": {"normal": {"good": 700}}}}))
    # ★問いに、書き方（コマンド）がそのまま入っていること★
    t("　問いに、決定を書き込むコマンドが入っている",
      "--apply" in target_line_questions(base_ms[0])[0])

    # ★本物の読み取りも動かす★（差し替えたまま終わらない）
    t("　確かめてある天井を読める（本物）",
      isinstance(known_ceilings("zzz_does_not_exist"), set))
    # ★★見出し付きの天井（`ceiling#bonus`）も読めること★★
    #   （2026-09-18・Codexの指摘・実データで確かめた）＝
    #   ★直す前は `ceiling` という名前だけを読んでいた★ので、
    #   天井を2つ記録してある機種が「天井0件」で拒否されていた。
    #   ★G数以外（`ceiling#point` の 1000pt）は混ぜない★
    _ssb = known_ceilings("ssb1", _find(_machines(), "ssb1"))
    t("★★見出し付きの天井（ceiling#bonus / #cz）も読む★★"
      "（★直す前は0件で、その機種は永久に線を決められなかった★）",
      {560, 899} <= _ssb)
    t("　G数でない天井（pt）は混ぜない", 1000 not in _ssb)
    # ★★記事の全文から数字を拾っていないこと★★（Codexの指摘の対照実験）
    #   ★実データで確かめる★＝dmm_5100 の記事にはslugの番号（5100）も
    #   導入年（2026）も確率の分母もあるが、天井としては1つも出ない。
    _real_ceils = known_ceilings("dmm_5100", _find(_machines(), "dmm_5100"))
    t("★★記事の全文から数字を拾わない★★"
      "（★直す前は slugの番号・導入年・確率の分母まで天井として通った★）",
      not ({5100, 2026} & _real_ceils))
    # ★構造化された場所からは読める★（「何も読まない」で緑にならないように）
    _wired = known_ceilings(
        "zzz_no_such_slug",
        {"checker": {"modes": [{"key": "normal"}], "normal": {"ceiling": 888}}})
    t("　チェッカーに入っている天井は読める（対照）", _wired == {888})

    print(f"\n{ok[0]}/{ok[1]} 合格")
    return 0 if ok[0] == ok[1] else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="2AIが決めた狙い目の線をチェッカーへ書く")
    ap.add_argument("--slug", default="")
    ap.add_argument("--apply", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.apply:
        return apply_decision(a.apply, dry_run=a.dry_run)
    if a.slug:
        return show(a.slug)
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

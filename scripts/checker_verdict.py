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
import io
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


import local_paths as _lp                                   # noqa: E402

# ★★「線を引かない」と決めた控え★★（2026-09-21・台帳#704）
#   ★リポジトリの外★＝2AIの判断の記録であって、読者に出るものではない。
NO_LINE_STORE = _lp.doc("checker_no_line.json")
NO_LINE = "NO_LINE"


def _confirmed_state(slug: str):
    """★その機種について、いま確かめてある材料の姿★

    返すのは `(指紋, 項目名の一覧)` ／ ★読めないときは `None`★。

    ★★名前だけでは足りない★★（2026-09-21・Codexの指摘）＝
    同じ項目名のまま**中身だけ**直せる（`confirmed_values.record()` は
    同名を上書きできる）。名前の一覧だけを見ていると、
    ★値や根拠が良くなっても「線を引かない」が永久に効いたまま★になる。
    ＝**材料が揃ったのに、その機種だけ狙い目が空のまま**。
    そこで**中身そのものの指紋**で比べる。

    ★★「読めない」と「0件」を分ける★★（同・Codexの指摘）＝
    どちらも空で返していたので、★控えが消えた日に
    「0件で記録した免除」と一致して質問が止まった★。
    読めないときは `None` を返し、免除は必ず無効にする。
    """
    try:
        import confirmed_values as _cv_f
        # ★★控えが消えたことを「0件」と読まない★★（2026-09-23・Codexの指摘）
        #   ★直す前は `require_exists` を付けていなかった★ので、
        #   控えのファイルが消えると空の控えが返り、
        #   「0件で記録した免除」と一致して質問が止まったままになった。
        got = _cv_f.load(strict=False, require_exists=True)
    except Exception:                                        # noqa: BLE001
        return None                        # ★読めない・消えた★＝免除を効かせない
    if not isinstance(got, dict):
        return None
    rows = got.get("machines")
    if not isinstance(rows, dict):
        return None                        # ★入れ物が壊れている★
    per = rows.get(slug)
    if per is None:
        per = {}                           # ★その機種の記録が無い＝本当の0件★
    elif not isinstance(per, dict):
        # ★★その機種の記録だけ壊れているときも「0件」と読まない★★（同）
        return None
    import hashlib as _hl_cf
    blob = json.dumps(per, ensure_ascii=False, sort_keys=True)
    return (_hl_cf.sha256(blob.encode("utf-8")).hexdigest(),
            sorted(str(k) for k in per))


def _no_line_all() -> dict:
    try:
        if not os.path.exists(NO_LINE_STORE):
            return {}
        got = _sj.read_json(NO_LINE_STORE, expect=dict)
        rows = got.get("machines")
        return rows if isinstance(rows, dict) else {}
    except Exception:                                        # noqa: BLE001
        return {}                          # ★読めないときは「控え無し」＝また聞く★


def no_line_record(slug: str) -> dict | None:
    """★その機種は「線を引かない」と決めてあるか★（★材料が変わっていたら無効★）"""
    rec = _no_line_all().get(str(slug or ""))
    if not isinstance(rec, dict):
        return None
    now = _confirmed_state(slug)
    if now is None:
        return None                        # ★控えを読めない＝もう一度聞く★
    if str(rec.get("confirmed_sha256") or "") != now[0]:
        return None                        # ★材料が変わった＝もう一度聞く★
    return rec


def no_line_problems(dec, ms=None) -> list:
    """★「線を引かない」という決定を受け取れる形か★"""
    ng, m = _common_problems(dec, ms)
    if m is None:
        return ng
    slug = str(dec.get("slug") or "")
    if dec.get("modes"):
        ng.append("線を引かない決定に modes は書けません（どちらか一方）")
    # ★すでに線がある機種には書かせない★（消す道具にしない）
    if has_target_line(m):
        ng.append(f"{slug}: すでに狙い目の線があります"
                  "（この決定は線を消す道具ではありません）")
    return ng


def record_no_line(dec: dict) -> int:
    """★「線を引かない」を控える★（全か無か・書いたら読み直す）"""
    slug = str(dec.get("slug") or "")
    state = _confirmed_state(slug)
    if state is None:
        print("★書きません★: 確かめてある値の控えを読めません"
              "（読めないまま控えると、控えが壊れた日に質問が止まります）")
        return 1
    rows = _no_line_all()
    rows[slug] = {
        "decision": NO_LINE,
        "why": str(dec.get("why") or "").strip(),
        "judges": sorted({str(x).lower() for x in (dec.get("judges") or [])}),
        "decided_at": str(dec.get("decided_at") or ""),
        # ★待っているもの★＝2AIが読むための覚え書き（★判定には使わない★）
        "waiting_for": [str(x) for x in (dec.get("waiting_for") or [])],
        "confirmed_sha256": state[0],
        # ★項目名は読む人のための添え物★（判定は上の指紋だけで決める）
        "confirmed_fields": state[1],
    }
    tmp = NO_LINE_STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"schema_version": "checker-no-line/v1", "machines": rows},
                  f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, NO_LINE_STORE)
    if no_line_record(slug) is None:
        print("★書いた後の読み直しが合いません★（手で確かめてください）")
        return 1
    print(f"{slug}: ★線を引かない★と控えました（以後この機種は聞きません）")
    print("★材料の顔ぶれが増えたら、もう一度聞きます★")
    return 0


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


def _common_problems(dec, ms=None) -> tuple:
    """★どの決定にも共通の検査★（2026-09-21・罠③＝同じ規則を2か所に書かない）

    返すのは（問題の一覧, その機種の行）。★行が無ければ一覧だけ見る★。
    """
    ng = []
    if not isinstance(dec, dict):
        return ["決定ファイルが辞書ではありません"], None
    slug = str(dec.get("slug") or "")
    if not re.match(r"^[a-z0-9_]+$", slug or " "):
        ng.append("slug が英小文字・数字・下線ではありません")
    ms = _machines() if ms is None else ms
    m = _find(ms, slug)
    if m is None:
        return ng + [f"{slug}: machines.json にありません"], None
    # ★★既存の機種（旧形式）も受け取る★★（2026-09-25・鉄則0z＝既定は2AI）
    #   ★直す前は「旧形式は触らない」で断っていた★ので、2AIがカウンターと
    #   記事の食い違いを見つけても、カウンター側を直す道が無かった（炎炎ノ消防隊2）。
    want = set(_judges_required())
    who = {str(x).lower() for x in (dec.get("judges") or [])}
    if not want <= who:
        ng.append("判断者に %s が要ります（いま: %s）"
                  % ("・".join(sorted(want)), "・".join(sorted(who)) or "なし"))
    if len(str(dec.get("why") or "").strip()) < MIN_WHY:
        ng.append(f"理由（why）が {MIN_WHY} 字以上ありません")
    return ng, m


def decision_problems(dec, ms=None) -> list:
    """★決定ファイルが受け取れる形か★（★1件でも駄目なら何も書かない★）"""
    ng, m = _common_problems(dec, ms)
    if m is None:
        return ng
    slug = str(dec.get("slug") or "")
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
    # ★★天井が構造化されていなくても受け取る★★（2026-09-25・鉄則0z）
    #   ★直す前は「天井が1つも確かめられていなければ断る」だった★＝
    #   既存の機種は天井を構造化して持っていないことが多く、2AIが決めても書けなかった。
    #   ★上限は、天井が分かっている欄でだけ見る★（線を何Gにするかは2AIの判断）。
    nums = known_ceilings(slug, m)
    cap = max(nums) if nums else None
    # ★★一覧の文（strategy）も2AIが決めてよい★★（2026-09-25）
    #   ★交換率の無い既存の機種は、一覧の文が手書き★（`target_display` は
    #   交換率を持つ機種しか作らない）ので、線だけ変えると一覧が古いまま残る。
    #   ★機械が見るのは「数字を作っていないか」だけ★＝文の中の数字が、
    #   この決定の線・天井か、いまの一覧の文に在ること。
    if "strategy" in dec:
        st = dec.get("strategy")
        if not isinstance(st, str) or not st.strip():
            ng.append("strategy（一覧の文）が空です")
        elif m.get("exchangeRates") or (m.get("checker") or {}).get(
                "exchangeRates"):
            ng.append(f"{slug}: 交換率を持つ機種の一覧の文は target_display が作ります"
                      "（手で書きません）")
        else:
            # ★数値の読み方は記事を直す道具と同じ1か所★（符号も数値の一部＝
            #   線が 630 でも「-630G」は作った数字。2026-09-25・Codexの指摘）
            #   ★単位ごと比べる★（2026-09-25・Codexの2回目）＝線 700 に対して
            #   「700枚」は別の事実。決定の線と天井は G（または単位なし）で書く。
            import decide_now as _dn_num
            allowed = set(_dn_num.numbers_with_unit(str(m.get("strategy") or "")))
            _vals = [md[lv] for md in (dec.get("modes") or []) if isinstance(md, dict)
                     for lv in LEVELS + ("ceiling",) if _int(md.get(lv)) is not None]
            for v in list(_vals) + list(nums):
                allowed |= {(str(v), "G"), (str(v), "")}
            made = [n + u for n, u in _dn_num.numbers_with_unit(st)
                    if (n, u) not in allowed]
            if made:
                ng.append("一覧の文に、決定の線にも今の文にも無い数字があります: "
                          + " / ".join(made[:4]) + "（数字を作らない）")
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
        # ★既にある欄なら、呼び名は今のままでよい★（既存の機種は欄がそろっている）
        _have = {str(x.get("key") or "") for x in (ck.get("modes") or [])
                 if isinstance(x, dict)}
        if not str(md.get("label") or "").strip() and key not in _have:
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
        # ★★天井が2つ以上ある機種で、その欄の天井が分からないときは断る★★
        #   （2026-09-18・Codexの3回目）★直す前★＝いちばん深いところで
        #   抑えていたので、ssb1（899G と 560G）の**新しい欄**に
        #   `ceiling` を書かずに `good: 800` を入れると通っていた。
        #   ★その欄がCZ間（560G）なら、天井より深い狙い目になる★。
        #   天井が1つしか無い機種では迷いようがないので、いままでどおり。
        if "ceiling" not in md and eff is None and len(nums) >= 2:
            ng.append(f"{tag}: この機種は天井が {sorted(nums)} と複数あります。"
                      "どの天井の欄かを ceiling で書いてください")
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
            if limit is not None and lv in vals and vals[lv] > limit:
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
    # ★★2AIが「引かない」と決めてあれば聞かない★★（2026-09-21・台帳#704）
    #   ★直す前は控える道が無かった★ので、決着しているのに毎朝同じ問いが出て、
    #   「3回聞いても決まらなければ人へ」の数えに乗って運営者へメールが飛んだ。
    #   ★材料の顔ぶれが増えたら控えは効かなくなる★（`no_line_record` が見る）。
    if no_line_record(slug) is not None:
        return []
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
        # ★★欄の置き場は、読む側と同じ順で決める★★（2026-09-25）
        #   読む側は `modeData[key]` を先に見るので、既存の機種で
        #   そこに入っている欄を `checker[key]` へ書くと★読まれずに黙って無視される★。
        _md = ck.get("modeData") if isinstance(ck.get("modeData"), dict) else None
        _in_md = _md is not None and isinstance(_md.get(key), dict)
        conf = dict((_md.get(key) if _in_md else ck.get(key)) or {})
        for lv in LEVELS:
            if lv in md:
                conf[lv] = md[lv]
        if "ceiling" in md:
            conf["ceiling"] = md["ceiling"]
        # ★target は good と同じ値★（読者の画面の「目安」表示に使う）
        if "good" in md:
            conf["target"] = md["good"]
        if _in_md:
            _md = dict(_md)
            _md[key] = conf
            ck["modeData"] = _md
            # ★★直下にも同じ欄があれば、そちらも同じ値にする★★（2026-09-25・Codexの指摘）
            #   読む側は場所によって modeData か直下のどちらかを先に見るので、
            #   片方だけ書くと公開の関所が「同じ欄の食い違い」で止まる。
            #   ★直下は丸ごと置き換えない★（2026-09-25・Codexの2回目）＝
            #   直下にだけある欄（注記など）を消さないよう、線と天井と目安だけ合わせる。
            if isinstance(ck.get(key), dict):
                _dir = dict(ck[key])
                for _k in LEVELS + ("ceiling", "target"):
                    if _k in conf:
                        _dir[_k] = conf[_k]
                ck[key] = _dir
        else:
            ck[key] = conf
    ck["modes"] = modes
    out["checker"] = ck
    # ★一覧の文も2AIが決めたとき★（交換率の無い機種だけ・検査は decision_problems）
    if isinstance(dec.get("strategy"), str) and dec["strategy"].strip():
        out["strategy"] = dec["strategy"].strip()
    return out


def apply_decision(path: str, dry_run: bool = False) -> int:
    dec = _sj.read_json(path, expect=dict)
    ms = _machines()
    # ★★「線を引かない」という結論も受け取る★★（2026-09-21・台帳#704）
    #   ★天井が分かっても、座ってよいゲーム数を出せないことはある★
    #   （平均投資・当選時の期待枚数が確認できていない機種）。
    #   ★決めたことを控えられないと、毎朝おなじ問いが出続ける★。
    if str(dec.get("decision") or "") == NO_LINE:
        ng = no_line_problems(dec, ms)
        if ng:
            print("★書きません★（1件でも通らなければ何も書かない）")
            for x in ng:
                print("  ・" + x)
            return 1
        if dry_run:
            _st = _confirmed_state(str(dec.get("slug") or ""))
            if _st is None:
                print("★書きません★: 確かめてある値の控えを読めません")
                return 1
            print("%s: ★線を引かない★と控えます（材料の顔ぶれ: %s）"
                  % (dec.get("slug"), "・".join(_st[1]) or "なし"))
            return 0
        return record_no_line(dec)
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
    # ★字下げ1・LF★＝ほかの書き手（grow_machine / publish_new_machine 等）と同じ書式。
    #   （2026-09-25）直す前は字下げ2・Windowsでは CRLF で、線を1本入れただけで
    #   全行が変わる差分になり、本当の変更が埋もれた。
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(ms, ensure_ascii=False, indent=1) + "\n")
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


def _format_tests(t) -> None:
    """★書いた machines.json が、ほかの書き手と同じ書式か★（2026-09-25・更新タスクの自己修正）

    ★直す前★＝字下げ2・Windowsでは改行が CRLF で全体を書き直していた。
    ほかの書き手（grow_machine / publish_new_machine / reorder_machines）は
    字下げ1・LF なので、線を1本入れただけで★全行が変わる差分★になった
    （2026-09-25 に初めて本物の機種へ書いて気づいた）。
    ★本物の machines.json には触らない★＝一時の場所へ向け直して通しで書く。
    """
    import tempfile
    global MACHINES
    keep = MACHINES
    tmpd = tempfile.mkdtemp(prefix="ckfmt_")
    try:
        # ★2行目は触らない行★（キーをわざと五十音・ABC順でない並びにする＝
        #   並べ替えや別の行の書き換えも捕まえる）
        untouched = {"zeta": "後", "slug": "zzz_other", "alpha": "前"}
        ms = [{"slug": "zzz_auto", "name": "試験機",
               "publication_policy": "page-decision/v1",
               "checker": {"unit": "G",
                           "modes": [{"key": "normal", "label": "通常"}],
                           "normal": {"ceiling": 1000}}},
              dict(untouched)]
        MACHINES = os.path.join(tmpd, "machines.json")
        with open(MACHINES, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(ms, ensure_ascii=False, indent=1) + "\n")
        dp = os.path.join(tmpd, "dec.json")
        with open(dp, "w", encoding="utf-8") as f:
            json.dump({"slug": "zzz_auto", "judges": ["claude", "codex"],
                       "why": "天井1000Gで恩恵はAT確定のため",
                       "modes": [{"key": "normal", "label": "通常",
                                  "ceiling": 1000, "excellent": 900,
                                  "good": 800, "caution": 700}]},
                      f, ensure_ascii=False)
        import contextlib
        import io as _io
        with contextlib.redirect_stdout(_io.StringIO()):
            rc = apply_decision(dp)
        with open(MACHINES, "rb") as f:
            raw = f.read()
        got = json.loads(raw.decode("utf-8"))
        # ★期待値は書いた結果から作らない★（Codexの指摘＝両辺が一緒に動く）
        #   2行目は元のまま（並び順も）、1行目は線の値が入っていること、
        #   書式は「その中身を字下げ1で並べたもの＋LF」と完全一致。
        n = ((got[0].get("checker") or {}).get("normal") or {}) if got else {}
        _txt = raw.decode("utf-8")
        t("★★線を書いた machines.json は、ほかの書き手と同じ書式（字下げ1・LF）★★"
          "（★直す前は字下げ2・CRLFで全行が変わる差分になった★）",
          rc == 0 and b"\r" not in raw
          and _txt == json.dumps(got, ensure_ascii=False, indent=1) + "\n"
          and len(got) == 2 and got[1] == untouched
          and got[0] == {"slug": "zzz_auto", "name": "試験機",
                         "publication_policy": "page-decision/v1",
                         "checker": {"unit": "G",
                                     "modes": [{"key": "normal", "label": "通常"}],
                                     "normal": {"ceiling": 1000, "caution": 700,
                                                "good": 800, "excellent": 900,
                                                "target": 800}}}
          and list(got[1].keys()) == list(untouched.keys())
          and (n.get("good"), n.get("caution"), n.get("excellent"),
               n.get("ceiling")) == (800, 700, 900, 1000)
          and '\n  "zeta": "後",\n  "slug": "zzz_other",\n  "alpha": "前"\n' in _txt)
    finally:
        MACHINES = keep
        import shutil
        shutil.rmtree(tmpd, ignore_errors=True)


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
        t("★★既存の機種（旧形式）の線も、2AIの決定なら受け取る★★"
          "（★直す前は断っていたので、炎炎ノ消防隊2のカウンターを直す道が無かった★）",
          not decision_problems(dec(slug="zzz_legacy"), base_ms))
        # ★★一覧の文も2AIが決めてよい。機械は数字を作っていないかだけ見る★★
        t("★★一覧の文を決定の線どおりに直せる★★",
          not decision_problems(dec(slug="zzz_legacy",
                                    strategy="通常700G〜が狙い目"), base_ms))
        t("★★一覧の文に、線にも今の文にも無い数字は書かせない★★（数字を作らない）",
          any("数字を作らない" in x for x in decision_problems(
              dec(slug="zzz_legacy", strategy="通常650G〜が狙い目"), base_ms)))
        t("★★線と同じ数字でも、符号が付けば別の数字★★（-700G は作った数字）",
          any("数字を作らない" in x for x in decision_problems(
              dec(slug="zzz_legacy", strategy="通常 - 700G〜が狙い目"), base_ms)))
        t("　一覧の文が決まったら、その文になる",
          merged(base_ms[1], dec(slug="zzz_legacy",
                                 strategy="通常700G〜が狙い目")
                 ).get("strategy") == "通常700G〜が狙い目")
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
        t("★新しい欄で呼び名が空だと通らない★",
          any("label" in x for x in decision_problems(
              dec(modes=[{"key": "reset", "label": "  ",
                          "good": 300}]), base_ms)))
        t("　既にある欄なら、呼び名は今のままでよい",
          not any("label" in x for x in decision_problems(
              dec(modes=[{"key": "normal", "good": 700}]), base_ms)))
        # ★★既存の機種で modeData に入っている欄は、そこへ書く★★
        #   （読む側が modeData を先に見るので、ほかへ書くと黙って無視される）
        _mdm = {"slug": "zzz_md", "checker": {
            "unit": "G", "modes": [{"key": "normal", "label": "通常"}],
            "modeData": {"normal": {"good": 500}}}}
        _mdg = merged(_mdm, dec(slug="zzz_md",
                                modes=[{"key": "normal", "good": 450}]))
        t("★★modeData に入っている欄は modeData を書き換える★★",
          ((_mdg["checker"].get("modeData") or {}).get("normal") or {})
          .get("good") == 450 and "normal" not in _mdg["checker"])
        _both = {"slug": "zzz_both", "checker": {
            "unit": "G", "modes": [{"key": "normal", "label": "通常"}],
            "modeData": {"normal": {"good": 500}}, "normal": {"good": 500}}}
        _bg = merged(_both, dec(slug="zzz_both",
                                modes=[{"key": "normal", "good": 450}]))
        t("★★同じ欄が modeData と直下の両方にあれば、両方を同じ値にする★★"
          "（片方だけだと公開の関所が食い違いで止まる）",
          _bg["checker"]["modeData"]["normal"].get("good") == 450
          and _bg["checker"]["normal"].get("good") == 450)
        _both2 = {"slug": "zzz_both2", "checker": {
            "unit": "G", "modes": [{"key": "normal", "label": "通常"}],
            "modeData": {"normal": {"good": 500}},
            "normal": {"good": 500, "note": "直下にだけある注記"}}}
        _bg2 = merged(_both2, dec(slug="zzz_both2",
                                  modes=[{"key": "normal", "good": 450}]))
        t("★★直下にだけある欄（注記など）は消さない★★",
          _bg2["checker"]["normal"].get("note") == "直下にだけある注記"
          and _bg2["checker"]["normal"].get("good") == 450)
        t("★★線と同じ数字でも、単位が違えば別の数字★★（線700に対して「700枚」は作った事実）",
          any("数字を作らない" in x for x in decision_problems(
              dec(slug="zzz_legacy", strategy="通常700枚〜が狙い目"), base_ms)))
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
        # ★★天井が構造化されていなくても、2AIの線は受け取る★★（2026-09-25・鉄則0z）
        globals()["known_ceilings"] = lambda s, m=None: set()
        t("★★天井が構造化されていない機種でも、2AIが決めた線は受け取る★★"
          "（★直す前は断っていたので、既存の機種の線を直せなかった★）",
          not decision_problems(dec(), base_ms))
        # ★★天井が2つ以上あって、その欄の天井が分からないとき★★
        #   （2026-09-18・Codexの3回目。★実データ＝ssb1 は 899G と 560G★）
        globals()["known_ceilings"] = lambda s, m=None: {560, 899}
        _no_ce = [{"slug": "zzz_auto", "name": "試験機",
                   "publication_policy": "page-decision/v1",
                   "checker": {"unit": "G", "modes": [], }}]
        t("★★天井が複数ある機種で、欄の天井を書かない決定は通さない★★"
          "（★いちばん深いところで抑えると、浅い欄に深い線が入る★）",
          any("複数あります" in x for x in decision_problems(
              dec(modes=[{"key": "normal", "label": "通常", "good": 800}]),
              _no_ce)))
        t("　どの天井かを書けば通る",
          not decision_problems(
              dec(modes=[{"key": "normal", "label": "通常",
                          "ceiling": 560, "good": 500}]), _no_ce))
        globals()["known_ceilings"] = lambda s, m=None: {1000}
        t("　天井が1つだけの機種では、いままでどおり書かなくてよい",
          not decision_problems(
              dec(modes=[{"key": "normal", "label": "通常", "good": 800}]),
              base_ms))
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
    #   （2026-09-18・Codexの指摘・本番のデータ ssb1 で見つけた）＝
    #   ★直す前は `ceiling` という名前だけを読んでいた★ので、
    #   天井を2つ記録してある機種が「天井0件」で拒否されていた。
    #   ★G数以外（`ceiling#point` の 1000pt）は混ぜない★
    #
    # ★★本番の控えを読まない★★（2026-09-18・CIが赤くなって分かった）
    #   ★直す前は実データ（ssb1）をそのまま読んでいた★ので、
    #   控えがリポジトリの外にあるCIでは**必ず落ちた**（実測）。
    #   ★「無ければ飛ばす」で逃げない★＝本物の登録関数を通して、
    #   一時の置き場へ自分で材料を作る（罠①・CLAUDE.mdの決まり）。
    import tempfile as _tf
    _NAME = "L試験機ゼット"
    _Q = {"bonus": "ボーナス間の天井は899G", "cz": "CZ間の天井は560G",
          "point": "思春期ポイントの天井は1000pt"}

    def _ff(url):
        q = _Q["bonus"] if "zzbonus" in url else (
            _Q["cz"] if "zzcz" in url else _Q["point"])
        return ("<title>" + _NAME + " スロット 新台 天井 | 解析</title>"
                "<body><h1>" + _NAME + "</h1><p>" + q + "。"
                + ("説明。" * 30) + "</p></body>")

    import confirmed_values as _cvT
    _keep_store, _keep_bind = _cvT.STORE, _cvT.bind_machine
    try:
        _cvT.STORE = os.path.join(_tf.mkdtemp(prefix="ckv_cv_"),
                                  "confirmed_values.json")
        _cvT.bind_machine = lambda u: ("zz_ceil", _NAME)
        _cvT.init_store()
        for _sfx, _kind, _amt, _unit in (("bonus", "GAME", "899", "G"),
                                         ("cz", "GAME", "560", "G"),
                                         ("point", "POINT", "1000", "pt")):
            _cvT.record(
                slug="zz_ceil", field="ceiling#" + _sfx,
                official_url="https://m.example/products/slot/z/",
                value={"kind": _kind, "amount": _amt, "unit": _unit,
                       "benefit": "AT"},
                sources=[_cvT.parse_source(
                    "https://chonborista.com/zz" + _sfx + "|" + _Q[_sfx]),
                    _cvT.parse_source(
                        "https://nana-press.com/zz" + _sfx + "|" + _Q[_sfx])],
                by=["claude", "codex"], name=_NAME, fetch=_ff,
                why="同じ原文を読んで一致しました")
        _rec = known_ceilings("zz_ceil")
        t("★★見出し付きの天井（ceiling#bonus / #cz）も読む★★"
          "（★直す前は0件で、その機種は永久に線を決められなかった★）",
          {560, 899} <= _rec)
        t("　G数でない天井（pt）は混ぜない", 1000 not in _rec)
    finally:
        _cvT.STORE, _cvT.bind_machine = _keep_store, _keep_bind
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

    _no_line_tests(t)
    _format_tests(t)
    print(f"\n{ok[0]}/{ok[1]} 合格")
    return 0 if ok[0] == ok[1] else 1


def _no_line_tests(t) -> None:
    """★「線を引かない」の控え★（2026-09-21・台帳#704）

    ★本番の置き場へは書かない★＝一時の場所へ向けてから動かす
    （罠㊿＝試験が本番の記録を埋めると、本物が埋もれる）。
    """
    import tempfile
    global NO_LINE_STORE
    _bak_store = NO_LINE_STORE
    _bak_state = globals()["_confirmed_state"]
    _bak_ms = globals()["_machines"]
    _row = {"slug": "zz_no_line", "publication_policy": "page-decision/v1",
            "checker": {"unit": "G", "modes": [{"key": "normal"}]}}
    # ★「いま確かめてある材料」を丸ごと差し替えられる形にする★
    #   （★名前だけでなく中身も持つ★＝中身だけ直された形を試せる）
    _mat = {"now": {"at": {"v": 1}, "ceiling": {"v": 900}}}

    def _fake_state(slug):
        if _mat["now"] is None:
            return None                    # ★控えを読めない★
        import hashlib as _h
        blob = json.dumps(_mat["now"], ensure_ascii=False, sort_keys=True)
        return (_h.sha256(blob.encode("utf-8")).hexdigest(),
                sorted(_mat["now"]))

    try:
        NO_LINE_STORE = os.path.join(tempfile.mkdtemp(prefix="nl_"), "x.json")
        globals()["_confirmed_state"] = _fake_state
        globals()["_machines"] = lambda: [_row]
        dec = {"slug": "zz_no_line", "decision": NO_LINE,
               "judges": ["claude", "codex"], "decided_at": "2026-09-21",
               "why": "平均投資と当選時の期待枚数が未確認のため算定できない"}
        t("　控える前は、狙い目の線を聞く問いが出る",
          bool(target_line_questions(_row)))
        t("　判断者が1人なら受け取らない",
          any("判断者" in x for x in no_line_problems(
              {**dec, "judges": ["claude"]}, [_row])))
        t("　理由が短ければ受け取らない",
          any("理由" in x for x in no_line_problems(
              {**dec, "why": "無理"}, [_row])))
        t("　線を引く決定と混ぜて書けない",
          any("どちらか一方" in x for x in no_line_problems(
              {**dec, "modes": [{"key": "normal", "good": 500}]}, [_row])))
        t("　問題が無ければ受け取る", no_line_problems(dec, [_row]) == [])
        t("★★控えたら、その機種は聞かない★★"
          "（★直す前は控える道が無く、毎朝おなじ問いが出た★）",
          record_no_line(dec) == 0 and target_line_questions(_row) == [])
        _mat["now"] = {"at": {"v": 1}, "ceiling": {"v": 900},
                       "payout_rate": {"v": 97}}
        t("★★材料が増えたら、もう一度聞く★★"
          "（★控えたまま永久に黙ると、材料が揃っても線が入らない★）",
          no_line_record("zz_no_line") is None
          and bool(target_line_questions(_row)))
        _mat["now"] = {"at": {"v": 1}, "ceiling": {"v": 900}}
        t("　材料が戻れば、控えはまた効く",
          no_line_record("zz_no_line") is not None)
        # ★★項目名は同じまま、中身だけ直された形★★（2026-09-21・Codexの指摘）
        #   ★直す前は名前の一覧だけを比べていた★ので、値や根拠が良くなっても
        #   ★「線を引かない」が永久に効いたまま★だった。
        _mat["now"] = {"at": {"v": 1}, "ceiling": {"v": 1200}}
        t("★★項目名が同じでも、中身が変われば聞き直す★★"
          "（★名前だけを見ていると、値が直っても永久に黙る★）",
          no_line_record("zz_no_line") is None
          and bool(target_line_questions(_row)))
        _mat["now"] = {"at": {"v": 1}, "ceiling": {"v": 900}}
        t("　（対照）中身が戻れば、また効く",
          no_line_record("zz_no_line") is not None)
        # ★★控えを読めないときは、必ず聞き直す★★（同）
        _mat["now"] = None
        t("★★確かめてある値の控えを読めないときは、免除を効かせない★★"
          "（★直す前は「読めない」も「0件」も空で、"
          "控えが消えた日に質問が止まった★）",
          no_line_record("zz_no_line") is None
          and bool(target_line_questions(_row)))
        t("　読めないときは、新しく控えることもできない",
          record_no_line(dec) == 1)
        _mat["now"] = {"at": {"v": 1}, "ceiling": {"v": 900}}
        _row2 = {**_row, "checker": {"unit": "G", "modes": [{"key": "normal"}],
                                     "normal": {"good": 500}}}
        t("★すでに線がある機種には書けない★（線を消す道具にしない）",
          any("すでに狙い目の線" in x
              for x in no_line_problems(dec, [_row2])))
        with io.open(NO_LINE_STORE, "w", encoding="utf-8") as _f:
            _f.write("{")
        t("　控えが読めないときは、また聞く側へ倒れる",
          no_line_record("zz_no_line") is None)
        # ★★本物の `_confirmed_state` の「読めない」を通す★★
        #   （罠①＝差し替えた偽物だけで試すと、本物の分岐を一度も通らない）
        globals()["_confirmed_state"] = _bak_state
        import confirmed_values as _cv_t
        _bak_load = _cv_t.load
        try:
            _cv_t.load = lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("控えが壊れています"))
            t("★本物の読み取りでも、読めなければ None★"
              "（★0件と同じ空で返すと、控えが消えた日に質問が止まる★）",
              _confirmed_state("zz_no_line") is None)
            _cv_t.load = lambda *a, **k: {"machines": "こわれた"}
            t("　入れ物が壊れているときも None",
              _confirmed_state("zz_no_line") is None)
            _cv_t.load = lambda *a, **k: {"machines": {}}
            _st0 = _confirmed_state("zz_no_line")
            t("　0件のときは None ではなく（指紋, 空の一覧）",
              _st0 is not None and _st0[1] == [])
            # ★★控えのファイルが消えた★★（2026-09-23・Codexの指摘）
            #   ★本物の load に、存在しない置き場を読ませる★（罠①）
            _cv_t.load = _bak_load
            _bak_cv_store = _cv_t.STORE
            try:
                _cv_t.STORE = os.path.join(tempfile.mkdtemp(prefix="cvgone_"),
                                           "confirmed_values.json")
                t("★★控えのファイルが消えたら None★★"
                  "（★直す前は空の控えが返り「0件」と一致して質問が止まった★）",
                  _confirmed_state("zz_no_line") is None)
            finally:
                _cv_t.STORE = _bak_cv_store
            _cv_t.load = lambda *a, **k: {"machines": {"zz_no_line": ["壊"]}}
            t("★★その機種の記録だけ壊れていても None★★"
              "（★直す前は空の辞書に置き換えて「0件」と読んでいた★）",
              _confirmed_state("zz_no_line") is None)
        finally:
            _cv_t.load = _bak_load
    finally:
        NO_LINE_STORE = _bak_store
        globals()["_confirmed_state"] = _bak_state
        globals()["_machines"] = _bak_ms


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

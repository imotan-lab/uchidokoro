# -*- coding: utf-8 -*-
"""★「基本スペック」のラベル行を表へ移す★（2026-08-31・運営者の要望③）

★運営者の言葉★
> 文字じゃなくてできることは表にしてパット見でわかるようにしたい

★★値を1文字も変えない★★
  移した結果から**元の行を組み立て直して、元と完全に一致すること**を
  1行ずつ確かめる。1行でも一致しなければ、その機種は移さない。
  ＝「変えていない」を言葉ではなく機械で示す。

★★全行がラベルと値の機種だけ移す★★
  ★太字の「**見出し**：値」だけを自動で移す★（Codexの助言）＝
  太字でない行は、文章の途中と区別できない
  （「設定6はBIG：REG比率が…」は短く句読点も無いが文章）。
  ★説明文を無理に表へ入れない★（2AIが読んで判断する仕事）。

★★factTable との重複はここでは消さない★★
  形式だけの変更にしておかないと「値を変えていない」の証明が崩れる。

使い方:
  python scripts/tableize_spec.py --all              # 移せる機種を数える
  python scripts/tableize_spec.py --slug hokuto      # 1機種の下見
  python scripts/tableize_spec.py --all --apply      # 実際に移す
  python scripts/tableize_spec.py --selftest

終了コード: 0=正常 / 1=失敗
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                      # noqa: BLE001
        pass

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DETAILS = os.path.join(BASE, "assets", "data", "machine-details")
TITLE = "基本スペック"
HEADERS = ["項目", "内容"]

# ★★太字の「**項目**：値」だけを自動で移す★★（2026-08-31・Codexの10回目）
#   ★太字でない行は自動判定しない★＝
#   「設定6はBIG：REG比率が約1対1です」のような**文章の途中**は、
#   記号も字数も手がかりにならない（例外リストが増える型になる）。
#   ★件数を最大にしない★＝迷う機種は移さず、2AIが読む側へ回す。
#   区切りは全角の「：」だけ（半角だと組み立て直しが一意に決まらない）。
_BOLD = re.compile(r"^\*\*(?P<label>[^*]+)\*\*：(?P<value>.*)$")


def parse_line(line: str):
    """1行を (見出し, 値) にする。形が違えば None。

    ★空白を1文字も触らない★＝`strip()` もしない。
    行そのものが `**見出し**：値` の形でなければ移さない。
    """
    t = str(line or "")
    m = _BOLD.match(t)
    if not m:
        return None
    label, value = m.group("label"), m.group("value")
    if not label.strip() or not value.strip():
        return None
    # ★見出しに文の記号が入っていたら文章★（実データで発見・my_juggler_v）
    if any(c in label for c in "。、！？!?"):
        return None
    if len(label.strip()) > 20:
        return None
    return label, value


def rebuild(label: str, value: str) -> str:
    """移す前の行を組み立て直す（照合用）。★形は1つだけ★なので一意に決まる。"""
    return f"**{label}**：{value}"


# ★★移した節の「あるべき姿」は1か所で決める★★（2026-08-31・Codexの11回目）
#   ★書く側と照合する側が同じものを見ると、照合にならない★ので、
#   ここは**形だけ**を決める。照合は `verify()` が
#   「変換前 + この形」で全体を組み立て直して深く比べる。
_SECTION_KEYS_BEFORE = {"title", "body"}


def plan(detail) -> dict:
    """1機種ぶんの下見。

    返すもの:
      {"ok": 移せるか, "why": 理由, "rows": [[見出し, 値], ...],
       "index": 節の位置}
    """
    if not isinstance(detail, dict):
        return {"ok": False, "why": "記事データが辞書ではありません"}
    secs = detail.get("sections") or []
    hit = [(i, x) for i, x in enumerate(secs)
           if isinstance(x, dict) and x.get("title") == TITLE]
    if len(hit) != 1:
        return {"ok": False, "why": f"{TITLE} の箱が {len(hit)} 個です"}
    i, sec = hit[0]
    if sec.get("type"):
        return {"ok": False, "why": f"もう別の種別です（{sec['type']}）"}
    # ★★節に余計な項目があれば移さない★★（2026-08-31）
    #   移したあとの節は title / type / tables だけになるので、
    #   ★ほかの項目は黙って消える★。ここで「移せない」に分けて、
    #   その機種だけ2AIが読む側へ回す（ほかの機種は進める）。
    if set(sec.keys()) != _SECTION_KEYS_BEFORE:
        return {"ok": False,
                "why": f"節に余計な項目があります（{sorted(sec.keys())}）"}
    body = sec.get("body")
    if not isinstance(body, list) or not body:
        return {"ok": False, "why": "本文がありません"}
    # ★★文字列でない要素は黙って捨てない★★（2026-08-31・Codexの10回目）
    if not all(isinstance(x, str) for x in body):
        return {"ok": False, "why": "本文に文字列でない要素があります"}
    rows = []
    for line in body:
        got = parse_line(line)
        if got is None:
            return {"ok": False, "why": "太字のラベル行ではありません",
                    "line": str(line)[:50]}
        label, value = got
        # ★★組み立て直して元と一致することを確かめる★★
        #   ★前後の空白も含めて完全一致★（strip しない）
        if rebuild(label, value) != line:
            return {"ok": False, "why": "組み立て直すと元と違います",
                    "line": line[:50]}
        rows.append([label, value])
    return {"ok": True, "why": "", "rows": rows, "index": i}


def apply_to(detail: dict, got: dict) -> dict:
    """下見の結果どおりに、その機種の記事データを書き換えて返す。"""
    sec = detail["sections"][got["index"]]
    detail["sections"][got["index"]] = expected_section(sec, got)
    return detail


def expected_section(before_sec: dict, got: dict) -> dict:
    """移したあとの節（★これ以外の項目は残さない★）。"""
    return {"title": before_sec["title"],
            "type": "table",
            "tables": [{"headers": list(HEADERS),
                        "rows": [list(r) for r in got["rows"]]}]}


def verify(before: dict, after: dict, got: dict) -> str:
    """★変えてよい所しか変えていないか★（良ければ空文字）。

    （2026-08-31・Codexの10回目。★直す前は照合になっていなかった★＝
      正規表現で切った直後に同じ部品を連結していただけで、
      **変換後のデータを一度も見ていなかった**）

    ★★直し（2026-08-31・Codexの11回目）★★＝
    直す前は「対象の節を元に戻したら一致するか」だけだったので、
    ★対象の節の中は何をしても通った★（題を書き換える／表に余計な項目を足す）。
    いまは**期待する変換後の全体を独立に組み立てて、深い比較を1回**する。

    見ること:
      ①移す位置が整数で、範囲の中にあること
      ②変換前のその節が「題と本文だけ」の形だったこと
      ③表の中身を、変換前の本文から**もう一度読み直して**一致すること
        （下見の結果をそのまま信じない）
      ④変換前 + 期待する節 == 変換後（丸ごと深い比較）
    """
    import copy
    i = got.get("index")
    if not isinstance(i, int) or isinstance(i, bool):
        return "移す位置が整数ではありません"
    if not isinstance(before.get("sections"), list):
        return "変換前の sections が配列ではありません"
    if not (0 <= i < len(before["sections"])):
        return "移す位置が範囲の外です"
    src = before["sections"][i]
    if not isinstance(src, dict):
        return "変換前の節が辞書ではありません"
    if set(src.keys()) != _SECTION_KEYS_BEFORE:
        return f"変換前の節の形が違います（{sorted(src.keys())}）"
    # ★★空の本文を受け取らない★★（2026-08-31・Codexの12回目）
    #   ★`src.get("body") or []` だと None / "" / 0 / [] が
    #     すべて「空の並び」になり、**空の表への変換**が最終確認を通った★
    body = src["body"]
    if not isinstance(body, list) or not body:
        return "変換前の本文が、空でない配列ではありません"
    rows_in = got.get("rows")
    if not isinstance(rows_in, list) or not rows_in:
        return "表の中身が、空でない並びではありません"
    if not all(isinstance(r, (list, tuple)) and len(r) == 2
               and all(isinstance(c, str) for c in r) for r in rows_in):
        return "表の行が「見出しと値」の2つではありません"
    # ★★下見を信じず、変換前の本文からもう一度読み直す★★
    again = []
    for line in body:
        one = parse_line(line) if isinstance(line, str) else None
        if one is None or rebuild(one[0], one[1]) != line:
            return "変換前の本文を読み直せません"
        again.append([one[0], one[1]])
    if again != [list(r) for r in rows_in]:
        return "表の中身が、変換前の本文と違います"
    expected = copy.deepcopy(before)
    expected["sections"][i] = expected_section(src, got)
    if after != expected:
        return "期待した変換以外が行われました"
    return ""


def _load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _stage(p, d) -> str:
    """一時ファイルへ書くだけ（まだ本物は差し替えない）。置き場を返す。"""
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
        f.write("\n")
    return tmp


def _save(p, d):
    """★一時ファイルへ書いてから置き換える★（途中で止まっても壊れない）。"""
    os.replace(_stage(p, d), p)


def selftest() -> int:
    ok = []

    def t(name, cond):
        ok.append(bool(cond))
        print(("✅" if cond else "❌") + " " + name)

    def det(lines):
        return {"sections": [{"title": TITLE, "body": list(lines)}]}

    g = plan(det(["**機種名**：スマスロ北斗の拳", "**メーカー**：サミー"]))
    t("★太字のラベル行を移せる★",
      g["ok"] and g["rows"] == [["機種名", "スマスロ北斗の拳"],
                                ["メーカー", "サミー"]])
    t("★★太字でない行は自動で移さない★★（文章と区別できないため・Codexの助言）",
      not plan(det(["天井：1200G+α"]))["ok"])
    t("★★説明文が混ざっていたら、その機種は移さない★★",
      not plan(det(["**天井**：1200G", "この機種は初当りが軽いです"]))["ok"])
    t("★★値の中に「：」があっても、最初の1つで切る★★",
      plan(det(["**備考**：朝一：リセット恩恵あり"]))["rows"]
      == [["備考", "朝一：リセット恩恵あり"]])
    t("★本文に文字列でない要素があれば移さない★（黙って捨てない）",
      not plan({"sections": [{"title": TITLE, "body": ["**天井**：1200G", 5]}]})["ok"])
    t("　値が空なら移さない", not plan(det(["**天井**："])) ["ok"])
    t("　見出しが空なら移さない", not plan(det(["**　**：1200G"]))["ok"])
    t("　もう表になっているなら触らない",
      not plan({"sections": [{"title": TITLE, "type": "table",
                              "tables": []}]})["ok"])

    # ★★値を1文字も変えていないことを、組み立て直して確かめる★★
    src = ["**機種名**：スマスロ とある魔術の禁書目録2",
           "**コイン持ち**：約30.8G/50枚（設定1）"]
    g3 = plan(det(src))
    back = [rebuild(r[0], r[1]) for r in g3["rows"]]
    t("★★移したあとから元の行を組み立て直すと、元と一致する★★", back == src)

    # ★★2回かけても2回目は何も動かない★★
    d = det(["**天井**：1200G"])
    g4 = plan(d)
    apply_to(d, g4)
    t("★★2回目は「もう表なので触らない」になる★★", not plan(d)["ok"])

    # ★★文中の「：」で切って、文章を見出しにしない★★（実データで発見）
    t("★★文の途中の「：」で切らない★★（my_juggler_v で実際に起きた）",
      not plan(det(["ジャグラー屈指の設定差を持つ最新作。設定6はBIG：REG比率がほぼ1:1です"]))["ok"])
    t("　読点が入った見出しも文章とみなす",
      not plan(det(["天井、恩恵：1200G"]))["ok"])

    # ★機種名に「。」が入る機種でも壊れない★
    g5 = plan(det(["**機種名**：スマスロ 痛いのは嫌なので防御力に極振りしたいと思います。"]))
    t("★★機種名に「。」が入っていても移せる★★（bofuri）",
      g5["ok"] and g5["rows"][0][1].endswith("思います。"))

    # ★★照合そのものを試す★★（2026-08-31・Codexの10回目）
    #   ★直す前の照合は、切った直後に同じ部品を連結していただけ★＝
    #   変換後のデータを一度も見ていなかった。
    import copy as _cp
    _b = {"slug": "zzz", "lead": "説明です",
          "sections": [{"title": "天井・恩恵", "body": ["天井は1200Gです"]},
                       {"title": TITLE,
                        "body": ["**機種名**：テスト機", "**メーカー**：サミー"]}]}
    _g = plan(_b)
    _a = apply_to(_cp.deepcopy(_b), _g)
    t("★正しい移し方なら照合を通る★", verify(_b, _a, _g) == "")
    _a2 = _cp.deepcopy(_a); _a2["lead"] = "書き換えた"
    t("★★対象の節以外を触ったら止める★★", verify(_b, _a2, _g) != "")
    _a3 = _cp.deepcopy(_a)
    _a3["sections"][0]["body"] = ["天井は1200Gです。"]
    t("★★別の節の本文を変えたら止める★★", verify(_b, _a3, _g) != "")
    _a4 = _cp.deepcopy(_a)
    _a4["sections"][1]["tables"][0]["rows"][0][1] = "テスト機 "
    t("★★セルの文字が1つでも違えば止める★★（末尾の空白も）",
      verify(_b, _a4, _g) != "")
    _a5 = _cp.deepcopy(_a); _a5["sections"][1]["body"] = ["残した"]
    t("★★節に余計な項目が残っていたら止める★★", verify(_b, _a5, _g) != "")
    _a6 = _cp.deepcopy(_a); _a6["sections"].append({"title": "増やした"})
    t("　節の数が変わったら止める", verify(_b, _a6, _g) != "")

    # ★★対象の節の「中」も見る★★（2026-08-31・Codexの11回目）
    #   ★直す前は、対象の節を元に戻して比べるだけ★だったので、
    #   **その節の中は何をしても通った**。
    _a7 = _cp.deepcopy(_a); _a7["sections"][1]["title"] = "別の題"
    t("★★対象の節の題を書き換えたら止める★★（節の中は素通りだった）",
      verify(_b, _a7, _g) != "")
    _a8 = _cp.deepcopy(_a)
    _a8["sections"][1]["tables"][0]["label"] = "勝手な見出し"
    t("★★表に余計な項目を足したら止める★★", verify(_b, _a8, _g) != "")
    _a9 = _cp.deepcopy(_a)
    _a9["sections"][1]["tables"].append({"headers": list(HEADERS),
                                         "rows": [["x", "y"]]})
    t("　表が増えたら止める", verify(_b, _a9, _g) != "")
    _a10 = _cp.deepcopy(_a)
    _a10["sections"][1]["tables"][0]["headers"] = ["A", "B"]
    t("　見出しが違えば止める", verify(_b, _a10, _g) != "")
    # ★★下見の結果を信じない★★＝変換前の本文から読み直して照合する
    _gx = dict(_g); _gx["rows"] = [["機種名", "ウソの値"], ["メーカー", "サミー"]]
    _ax = apply_to(_cp.deepcopy(_b), _gx)
    t("★★下見の中身が変換前の本文と違えば止める★★"
      "（下見をそのまま信じると、偽の値を書けた）",
      verify(_b, _ax, _gx) != "")
    for _bad in (None, "1", True, -1, 99):
        t(f"　移す位置が {_bad!r} なら止める",
          verify(_b, _a, {**_g, "index": _bad}) != "")

    # ★★空の本文を受け取らない★★（2026-08-31・Codexの12回目）
    # ★★どの守りが働いたかまで見る★★（2026-08-31・壊し方の確認で判明）
    #   ★理由まで見ないと、隣の守りが断っているだけでも試験が通る★＝
    #   その守りを消しても赤くならない（＝一度も試していない）。
    for _empty in ([], "", None, 0):
        _be = _cp.deepcopy(_b)
        _be["sections"][1]["body"] = _empty
        _ae = _cp.deepcopy(_be)
        _ae["sections"][1] = {"title": TITLE, "type": "table",
                              "tables": [{"headers": list(HEADERS),
                                          "rows": []}]}
        t(f"　変換前の本文が {_empty!r} なら止める（空の表を作らせない）",
          "変換前の本文が、空でない配列ではありません"
          == verify(_be, _ae, {"index": 1, "rows": []}))
    t("　表の行が2つ組でなければ止める",
      "表の行が「見出しと値」の2つではありません"
      == verify(_b, _a, {**_g, "rows": [["機種名"]]}))
    # ★★変換前の節に余計な項目があったら止める★★（★本物の穴だった★）
    #   ★これが無いと、その項目が**黙って消える**★＝
    #   移したあとの節は title/type/tables だけになるので、
    #   note などを持っていた機種は中身を失う。
    #   ★下見は先に断るので、ここは照合を直接たたく★
    #     （手前の守りに助けられて、狙った守りを一度も試さないのを避ける）
    _bk = _cp.deepcopy(_b)
    _bk["sections"][1]["note"] = "消えては困る注記"
    _gk = {"ok": True, "why": "", "index": 1,
           "rows": [["機種名", "テスト機"], ["メーカー", "サミー"]]}
    _ak = apply_to(_cp.deepcopy(_bk), _gk)
    t("★★変換前の節に余計な項目があれば止める★★"
      "（そのまま移すと、その項目が黙って消える）",
      verify(_bk, _ak, _gk).startswith("変換前の節の形が違います"))
    t("　節に余計な項目があれば、その機種だけ移さない（全体は止めない）",
      plan({"sections": [{"title": TITLE, "body": ["**a**：b"],
                          "note": "x"}]})["why"].startswith(
          "節に余計な項目があります"))
    t("　本文に文字列でない要素があれば移さない",
      plan({"sections": [{"title": TITLE,
                          "body": ["**a**：b", 1]}]})["why"]
      == "本文に文字列でない要素があります")

    # ★★書き込みのまとまり★★（2026-08-31・Codexの12回目）
    #   ★1件でも失敗したら、置き換えた分を全部戻す★
    import shutil as _sh
    import tempfile as _tf
    _dir = _tf.mkdtemp(prefix="tableize_tx_")
    _paths, _befores, _ready = [], [], []
    for _i in range(3):
        _p = os.path.join(_dir, f"m{_i}.json")
        _d = {"slug": f"m{_i}",
              "sections": [{"title": TITLE,
                            "body": [f"**機種名**：機種{_i}"]}]}
        with open(_p, "w", encoding="utf-8", newline="\n") as _f:
            json.dump(_d, _f, ensure_ascii=False, indent=1)
            _f.write("\n")
        _befores.append(open(_p, encoding="utf-8").read())
        _paths.append(_p)
        _gg = plan(_d)
        _ready.append((_p, f"m{_i}", apply_to(_cp.deepcopy(_d), _gg), _gg))

    _done, _rc = write_all(_ready)
    _after_ok = [open(_p, encoding="utf-8").read() for _p in _paths]
    t("　ふつうに書ければ3件とも変わる",
      _rc == 0 and len(_done) == 3
      and all(a != b for a, b in zip(_after_ok, _befores)))
    t("　一時ファイルも退避も残らない",
      not [x for x in os.listdir(_dir) if x.endswith((".tmp", ".bak"))])

    # ★戻して、3件目の読み直しだけ失敗させる★
    for _p, _src in zip(_paths, _befores):
        with open(_p, "w", encoding="utf-8", newline="\n") as _f:
            _f.write(_src)
    _real_load = _load
    _seen2 = [0]

    def _flaky(p):
        got = _real_load(p)
        if p == _paths[2]:
            # ★write_all の中で _load が呼ばれるのは読み直しの1回だけ★
            #   （下見では本物を直に呼んでいる）
            _seen2[0] += 1
            return {"わざと": "違う中身"}
        return got

    globals()["_load"] = _flaky
    try:
        _ready2 = []
        for _p in _paths:
            _d = _real_load(_p)
            _gg = plan(_d)
            _ready2.append((_p, os.path.basename(_p)[:-5],
                            apply_to(_cp.deepcopy(_d), _gg), _gg))
        _done2, _rc2 = write_all(_ready2)
    finally:
        globals()["_load"] = _real_load
    _now = [open(_p, encoding="utf-8").read() for _p in _paths]
    t("★★1件でも失敗したら、置き換えた分を全部戻す★★"
      "（直す前は、そこまでのファイルだけが変わったまま残った）",
      _seen2[0] > 0                       # ★狂わせが実際に効いたこと★
      and _rc2 == 1 and _done2 == [] and _now == _befores)
    t("　失敗しても一時ファイル・退避を残さない",
      not [x for x in os.listdir(_dir) if x.endswith((".tmp", ".bak"))])

    # ★★置き換えの「途中」で失敗する場合★★（2026-08-31・Codexの13回目）
    #   ★前の試験は「全件置き換えたあとの読み直し」で失敗させていた★＝
    #   同じ受け止めと巻き戻しには入るが、
    #   **置き換え途中では残りの `.tmp` がまだ在る**状態を試していない。
    for _p, _src in zip(_paths, _befores):
        with open(_p, "w", encoding="utf-8", newline="\n") as _f:
            _f.write(_src)
    _ready3 = []
    for _p in _paths:
        _d = _load(_p)
        _gg = plan(_d)
        _ready3.append((_p, os.path.basename(_p)[:-5],
                        apply_to(_cp.deepcopy(_d), _gg), _gg))
    _real_replace = os.replace
    _count3 = [0]

    def _boom(src, dst):
        _count3[0] += 1
        if _count3[0] == 3:
            raise OSError("わざと置き換えを失敗させます")
        return _real_replace(src, dst)

    os.replace = _boom
    try:
        _done3, _rc3 = write_all(_ready3)
    finally:
        os.replace = _real_replace
    _now3 = [open(_p, encoding="utf-8").read() for _p in _paths]
    _left = [x for x in os.listdir(_dir) if x.endswith((".tmp", ".bak"))]
    t("★★置き換えの3件目で失敗しても、全件が元のまま★★"
      "（1・2件目は既に置き換わっている状態からの巻き戻し）",
      _count3[0] == 3 and _rc3 == 1 and _done3 == []
      and _now3 == _befores)
    t("　途中で失敗しても、一時ファイルも退避も残らない", _left == [])

    # ★★前の失敗が残した退避があれば断る★★
    with open(_paths[0] + ".bak", "w", encoding="utf-8") as _f:
        _f.write("前の残り")
    _ready4 = []
    for _p in _paths:
        _d = _load(_p)
        _gg = plan(_d)
        _ready4.append((_p, os.path.basename(_p)[:-5],
                        apply_to(_cp.deepcopy(_d), _gg), _gg))
    _done4, _rc4 = write_all(_ready4)
    _now4 = [open(_p, encoding="utf-8").read() for _p in _paths]
    t("★前の退避が残っていたら書かない（戻すための控えを自分で壊さない）★",
      _rc4 == 1 and _done4 == [] and _now4 == _befores)
    os.remove(_paths[0] + ".bak")
    for _x in os.listdir(_dir):
        if _x.endswith(".tmp"):
            os.remove(os.path.join(_dir, _x))

    _sh.rmtree(_dir, ignore_errors=True)

    # ★★対象の指定★★（2026-08-31・Codexの13回目）
    #   ★直す前は `--apply` だけで60機種すべてを書き換えた★
    t("★★--apply だけでは書かない★★（60機種すべてが書き換わっていた）",
      "--slug か --all" in target_problem(None, False, True))
    t("　--slug と --all の同時指定は断る",
      target_problem(["a"], True, True) != "")
    t("　--slug を並べれば通る", target_problem(["a", "b"], False, True) == "")
    t("　--all を明示すれば通る", target_problem(None, True, True) == "")
    t("　書かない実行（下見）は指定なしでも通る",
      target_problem(None, False, False) == "")
    t("　同じ機種を2回書いたら断る",
      target_problem(["a", "a"], False, True) != "")
    for _bad in ("../x", "A", "a b", "", "a/b"):
        t(f"　機種の書き方が {_bad!r} なら断る（置き場の外へ書かせない）",
          target_problem([_bad], False, True) != "")

    # ★★指定した機種以外は変わらない★★
    _dir2 = _tf.mkdtemp(prefix="tableize_pick_")
    _all_paths, _all_src = [], []
    for _i in range(4):
        _p = os.path.join(_dir2, f"n{_i}.json")
        _d = {"slug": f"n{_i}",
              "sections": [{"title": TITLE, "body": [f"**機種名**：機種{_i}"]}]}
        with open(_p, "w", encoding="utf-8", newline="\n") as _f:
            json.dump(_d, _f, ensure_ascii=False, indent=1)
            _f.write("\n")
        _all_paths.append(_p)
        _all_src.append(open(_p, encoding="utf-8").read())
    _pick = []
    for _p in (_all_paths[1], _all_paths[3]):
        _d = _load(_p)
        _gg = plan(_d)
        _pick.append((_p, os.path.basename(_p)[:-5],
                      apply_to(_cp.deepcopy(_d), _gg), _gg))
    _done5, _rc5 = write_all(_pick)
    _now5 = [open(_p, encoding="utf-8").read() for _p in _all_paths]
    t("★★指定した機種だけが変わる★★（ほかは1文字も動かない）",
      _rc5 == 0 and sorted(_done5) == ["n1", "n3"]
      and _now5[0] == _all_src[0] and _now5[2] == _all_src[2]
      and _now5[1] != _all_src[1] and _now5[3] != _all_src[3])
    _sh.rmtree(_dir2, ignore_errors=True)

    ng = ok.count(False)
    print(f"{len(ok) - ng}/{len(ok)} 合格")
    return 1 if ng else 0


def write_all(ready: list) -> tuple[list, int]:
    """★下書き→退避→置き換え→読み直し→（失敗なら全件戻す）★

    返すもの: (書けた機種の並び, 終了コード)

    ★関数に切り出した理由★（2026-08-31・Codexの12回目）＝
    処理の中に埋まっていると**巻き戻しの道を試験で通せない**ので、
    「守りがあるのに一度も確かめていない」状態になる。
    """
    done = []
    # ★★全部の一時ファイルを先に書き、そろってから置き換える★★
    #   （Codexの11回目）★直す前は1件ずつ本物を差し替えて★いたので、
    #   11件目で書き込みに失敗すると**部分適用**になった。
    #   置き換え（rename）は書き込みより失敗しにくいので、
    #   危ない工程を全部前に寄せる。
    staged = []
    tmps = []
    try:
        for p, slug, after, got in ready:
            # ★★書く前に置き場を控える★★（2026-08-31・Codexの13回目）
            #   ★直す前は `staged` に足す前に例外が出ると、
            #     いま書いていた1件の `.tmp` が残り得た★
            tmps.append(p + ".tmp")
            staged.append((p, slug, after, _stage(p, after)))
    except Exception as e:                          # noqa: BLE001
        for tmp in tmps:
            if os.path.exists(tmp):
                os.remove(tmp)
        print(f"★下書きの途中で失敗しました（{e}）。何も書いていません★")
        return done, 1

    # ★★元を全件退避してから置き換える★★（Codexの12回目）
    #   ★直す前は、置き換えの途中で失敗すると
    #     そこまでのファイルだけが変わったまま残った★
    #   （しかも例外を受け止めていないので、読み直しにも進まない）
    #   ★「git で戻せる」は回復手順であって、まとまりの代わりにならない★
    keep = []
    try:
        for p, slug, after, tmp in staged:
            bak = p + ".bak"
            # ★★前の失敗が残した退避があれば断る★★（Codexの13回目）
            #   ★上書きすると、戻すための控えを自分で壊す★
            if os.path.exists(bak):
                raise RuntimeError(
                    f"前回の退避が残っています: {bak}"
                    "（中身を確かめてから消してください）")
            shutil.copy2(p, bak)
            keep.append((p, bak))
    except Exception as e:                          # noqa: BLE001
        for _p, bak in keep:
            if os.path.exists(bak):
                os.remove(bak)
        for _p, _s, _a, tmp in staged:
            if os.path.exists(tmp):
                os.remove(tmp)
        print(f"★退避に失敗しました（{e}）。何も書いていません★")
        return done, 1

    try:
        for p, slug, after, tmp in staged:
            os.replace(tmp, p)
        # ★★書いたあと、全件を読み直して確かめる★★
        #   （書いた内容と、ファイルに残った内容は別物）
        bad = [slug for p, slug, after, tmp in staged if _load(p) != after]
        if bad:
            raise RuntimeError(f"保存した中身が違います: {bad}")
    except Exception as e:                          # noqa: BLE001
        # ★1件でも失敗したら、置き換えた分を全部戻す★
        failed = []
        for p, bak in keep:
            try:
                shutil.copy2(bak, p)
            except Exception:                       # noqa: BLE001
                failed.append(p)
        print(f"★書き込みに失敗しました（{e}）★")
        if failed:
            print("★★戻せなかったファイルがあります★★（手で直してください）:")
            for x in failed:
                print("  " + x)
        else:
            print("  ★全部、元に戻しました★")
        for _p, bak in keep:
            if os.path.exists(bak) and _p not in failed:
                os.remove(bak)
        for _p, _s, _a, tmp in staged:
            if os.path.exists(tmp):
                os.remove(tmp)
        return done, 1

    done = [slug for _p, slug, _a, _t in staged]
    for _p, bak in keep:
        if os.path.exists(bak):
            os.remove(bak)
    return done, 0


def target_problem(slugs, want_all: bool, apply: bool) -> str:
    """対象の指定が正しいか（良ければ空文字）。

    ★★書くときは、どこに書くかを必ず言わせる★★
    （2026-08-31・Codexの13回目。★直す前は `--apply` だけで
      60機種すべてを書き換えた★＝`--all` を受け取っていたのに見ていなかった）
    """
    if slugs and want_all:
        return "★--slug と --all は同時に指定できません★"
    if apply and not slugs and not want_all:
        return ("★--apply には --slug か --all の指定が要ります★\n"
                "  何機種か試すとき: --slug a --slug b --apply\n"
                "  全機種に当てるとき: --all --apply")
    if slugs:
        bad = [x for x in slugs
               if not x or not re.fullmatch(r"[a-z0-9_]+", x)]
        if bad:
            return f"★機種の書き方が違います: {bad}★"
        if len(set(slugs)) != len(slugs):
            return "★同じ機種を2回指定しています★"
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="基本スペックのラベル行を表へ移す")
    ap.add_argument("--slug", action="append",
                    help="対象の機種（何度でも書けます）")
    ap.add_argument("--all", action="store_true",
                    help="全機種を対象にする（--apply のときは明示が必要）")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()

    why = target_problem(a.slug, a.all, a.apply)
    if why:
        print(why)
        return 1

    import copy
    paths = ([os.path.join(DETAILS, x + ".json") for x in a.slug] if a.slug
             else sorted(glob.glob(os.path.join(DETAILS, "*.json"))))
    movable, stuck, done = [], [], []
    ready = []
    for p in paths:
        slug = os.path.basename(p)[:-5]
        if not os.path.isfile(p):
            print(f"★{slug} の記事データがありません★")
            return 1
        d = _load(p)
        got = plan(d)
        if not got["ok"]:
            stuck.append((slug, got["why"], got.get("line", "")))
            continue
        movable.append((slug, got))
        # ★★書く前に、全部の機種で照合を通す★★（2026-08-31・Codexの10回目）
        #   ★直す前はループの中で1ファイルずつ上書きしていた★ので、
        #   途中で止まると**部分適用**になった。
        after = apply_to(copy.deepcopy(d), got)
        why = verify(d, after, got)
        if why:
            print(f"★{slug}: 照合に通りませんでした（{why}）★")
            return 1
        ready.append((p, slug, after, got))

    if a.apply:
        done, rc = write_all(ready)
        if rc:
            return rc

    if a.slug and len(a.slug) == 1:
        slug = os.path.basename(paths[0])[:-5]
        if movable:
            print(f"{slug}: 移せます（{len(movable[0][1]['rows'])} 行）")
            for r in movable[0][1]["rows"]:
                print(f"  {r[0]}｜{r[1][:40]}")
        else:
            print(f"{slug}: 移せません（{stuck[0][1]}）")
            if stuck[0][2]:
                print(f"  例: {stuck[0][2]}")
        return 0

    print(f"移せる機種: {len(movable)} / 移せない: {len(stuck)}")
    if a.apply:
        print(f"★書き換えました: {len(done)} 機種★")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

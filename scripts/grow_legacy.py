#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""grow_legacy.py — 旧方式の先行記事に、裏取りできた材料を書き足す。

★何のための道具か（2026-08-06）★
  8月3日に導入された7機種の記事が「当サイトでは未確認です」のまま残っていた。
  名鑑の索引を直した結果、型式名・機械割・天井などが**2出典一致で採れる**ように
  なったので、その分だけを記事へ入れる。

★やること／やらないこと★
  やる   : 未確認の箱を、2出典で一致した事実に差し替える
           足した事実と食い違う「まだ分かりません」を落とす
  やらない: 値を作る（材料に無いものは書かない）
           迷ったら書く（★決められない時は止める★）

★止める（fail-closed）ところ★（2026-08-06・Codex125回目の指摘を反映）
  ・すでに書いてある同じ項目の値が**違う**（どちらが正しいか機械には決められない）
  ・落とそうとした文に、**まだ分からない別の話**が混じっている
  ・書く直前にファイルが変わっていた（誰かが同時に触った）
  ・記事の中身が別機種だった（slug・機種名が名簿と合わない）

使い方:
    python scripts/grow_legacy.py                 # 対象を見る
    python scripts/grow_legacy.py --slug xxx      # 1機種だけ（下見）
    python scripts/grow_legacy.py --slug xxx --apply
    python scripts/grow_legacy.py --next --apply  # ★無人運転★（1日1機種・最古優先）
    python scripts/grow_legacy.py --selftest

★無人運転（--next）の約束★（2026-08-06）
  ・1回に1機種だけ。**いちばん長く見ていない機種**から順に見る
  ・止まった（Halt）ら**台帳へ1件だけ上げて、その日は終わり**（例外で落ちない）
  ・止まっても「見た日」は進める＝同じ機種で毎日詰まらない
  ・台帳に「止めるべき」案件がある機種は触らない
  ・★1日1機種の枠（task_guard）は使わない★（育てるのは別レーン）
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import page_decision as _pd              # noqa: E402
import safe_json as _sj                  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DETAILS = os.path.join(BASE, "assets", "data", "machine-details")
PENDING = "当サイトでは未確認です。確認でき次第、この欄に掲載します。"
SOURCED = "（出典2件で一致）"

# 天井の種類ごとの見出し（★材料に無い言葉を足さない★）
_KIND_JP = {"GAME": "ゲーム数天井", "CYCLE": "周期天井",
            "POINT": "ポイント天井", "THROUGH": "スルー天井"}

# 項目キー → 「解析待ちの項目」の箇条書きで使われている言葉
#   ★言葉は厳しくする★（「・リセット時の天井短縮」を巻き込まないため）
_PENDING_WORDS = {"型式名": ("型式名",),
                  "天井GAME": ("天井ゲーム数",),
                  "天井CYCLE": ("周期天井",),
                  "天井POINT": ("ポイント天井",),
                  "天井THROUGH": ("スルー天井",)}
# 項目キー → 打ち消し文を探す時の言葉
#   ★天井は種類ごとに厳密に分ける★（2026-08-06・Codex126回目 #1）
#     以前はどの天井にも「天井」を入れていたので、スルー天井が分かっただけで
#     「天井ゲーム数」の未判明表示まで消えた（やじきたで実際に起きた）。
_SENT_WORDS = {"機械割": ("機械割", "出玉率"), "型式名": ("型式名",),
               "天井GAME": ("天井ゲーム数", "ゲーム数天井", "G数天井"),
               "天井CYCLE": ("周期天井", "天井周期"),
               "天井POINT": ("ポイント天井", "天井ポイント"),
               "天井THROUGH": ("スルー天井", "天井スルー", "スルー回数")}
# 天井を指す言葉の全体（★これが1つも無いのに「天井」だけある＝どの天井か決まらない★）
_CEILING_WORDS = tuple(w for k, ws in _SENT_WORDS.items()
                       if k.startswith("天井") for w in ws)

# ★「まだ分かりません」を表す言い方★
_UNKNOWN_MARK = ("判明していない", "判明していません", "判明しておらず",
                 "公開されていません", "解析判明後", "判明次第", "未解析",
                 "未判明", "調査中", "揃っていません", "確認できていません",
                 "分かっていません", "不明です", "解析待ち")
# 天井の付随項目 → 本文での言い方
_TOPIC_WORDS = {"恩恵": ("恩恵", "特典"),
                "何回": ("数える対象", "カウント対象", "何を数える")}
# ★まだ分からない別の話★（これが混じる文は勝手に落とさない）
_OTHER_TOPICS = ("狙い目", "リセット", "短縮", "ヤメ", "純増", "継続率",
                 "突入率", "設定示唆", "終了画面", "小役", "コイン単価",
                 "設定段階", "設定別", "有利区間", "引き戻し", "初当り")

_ENUM = re.compile(r"^\*\*(?P<items>[^*]+)\*\*\s*[：:]\s*解析判明次第追記します。?$")
_SEP = re.compile(r"[・／/、,]")
_LABELED = re.compile(r"^\*\*(?P<label>[^*]+)\*\*\s*[：:]\s*(?P<value>.+)$")


# ★状態は専用ファイル★（2026-08-06・Codex132回目）
#   state.json を共有すると、読めなかった時に **他タスクの履歴まで消して**
#   上書きしうる。この道具の記録だけを別ファイルに持つ。
import os as _os_lp                 # noqa: E402
import sys as _sys_lp               # noqa: E402
_sys_lp.path.insert(0, _os_lp.path.dirname(_os_lp.path.abspath(__file__)))
import local_paths as _lp           # noqa: E402
STATE = _lp.doc("legacy_grow_state.json")
# ★一時的な不調（時間が解決する）とそうでないものを分ける★
#   ★台帳を読めないのは入れない★（2026-08-06・Codex133回目 #2）
#     台帳全体の障害なのに機種ごとに数えると、週1運転では3回目まで
#     14週かかる＝その間ずっと「成功」に見えてしまう。すぐ知らせる。
_TRANSIENT = ("材料を集められません", "書く直前にファイルが変わっていました",
              "ほかの処理が同じ記事を書いています", "鍵が取れません")
_TRANSIENT_LIMIT = 3                      # 続けてこの回数で人に知らせる


class Halt(Exception):
    """決められないので書かずに止める。"""


def _sha(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _read_state() -> dict:
    """記録を読む。★無ければ空・壊れていれば止める★（空として上書きしない）"""
    if not os.path.exists(STATE):
        return {}
    try:
        return _sj.read_json(STATE, expect=dict)
    except Exception as e:                # noqa: BLE001
        raise Halt(f"記録ファイルが読めません（上書きしません）: {e}")


def _save_state(st: dict) -> None:
    """記録を書く（★失敗は握り潰さない★）。"""
    tmp = STATE + f".tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _peek_streak(slug: str) -> int:
    """続けて失敗した回数を見る（★記録する前に判断したい★）。"""
    return int((_read_state().get("transient_streak") or {}).get(slug, 0))


def _mark_checked(slug: str, today: str, outcome: str) -> int:
    """「見た日」と結果を記録し、続けて失敗した回数を返す。

    outcome: "wrote"（書けた）／"none"（足すものなし）／
             "halt"（人の判断が要る）／"transient"（時間が解決するかも）
    ★失敗したら例外★＝呼び出し側で失敗として扱う（Codex132回目）。
    """
    st = _read_state()
    st.setdefault("checked", {})[slug] = today
    st["last_run"] = today
    cnt = st.setdefault("counts", {}).setdefault(slug, {})
    cnt[outcome] = int(cnt.get(outcome, 0)) + 1
    fails = st.setdefault("transient_streak", {})
    fails[slug] = (int(fails.get(slug, 0)) + 1) if outcome == "transient" else 0
    _save_state(st)
    return int(fails[slug])


def is_transient(problems: list) -> bool:
    """時間が解決するかもしれない不調か（★人の判断が要るものと分ける★）。"""
    text = " / ".join(str(x) for x in problems)
    return any(w in text for w in _TRANSIENT)


def _to_ledger(slug: str, problems: list, transient: bool) -> None:
    """★黙って同じ所で止まり続けない★（無人運転のときだけ呼ぶ）。

    ★無人タスクは close しない★＝人が判断する。
    同じ (slug + kind + title) なら重複登録されず last_seen だけ伸びる。

    ★危険度は必ず決まった語で渡す★（2026-08-06・自分で確認して見つけた）
      `open_issues.severity_of()` は**知らない語を CRITICAL に倒す**（fail-closed）。
      当初 "normal" を渡していたが、これは有効な語ではないので CRITICAL になり、
      **一時的な取得失敗だけでその機種が公開停止扱い**になるところだった。
      人の判断が要る＝MATERIAL ／ 時間が解決するかも＝QUALITY。
    ★失敗したら例外★＝呼び出し側で失敗として扱う（Codex132回目）。
    """
    import open_issues as _oi
    if transient:
        title = (f"{slug}: 旧方式の先行記事の材料を"
                 f"{_TRANSIENT_LIMIT}回続けて集められません")
        kind, sev, code = "external_value", "QUALITY", "GROW_LEGACY_TRANSIENT"
    else:
        title = f"{slug}: 旧方式の先行記事を育てられません（人の判断が要ります）"
        kind, sev, code = "structural", "MATERIAL", "GROW_LEGACY_HALT"
    # ★CLIの引数の形に依存しない入口を使う★（2026-08-10・台帳#300）
    _oi.add_issue(_oi.DEFAULT_FILE,
                  source="update-machine", slug=slug, kind=kind, title=title,
                  detail="grow_legacy.py --next が止まりました: "
                         + " / ".join(str(x) for x in problems),
                  severity=sev, reason_code=code)


def _blocked(slug: str) -> list:
    """台帳に「止めるべき」案件があるか（★読めない時は進めない★）。"""
    try:
        import open_issues as _oi
        why = _oi.blocking_slugs().get(slug)
    except Exception as e:                # noqa: BLE001
        return [f"台帳を読めません: {e}"]
    return [f"台帳に止めるべき案件があります: {' / '.join(why)}"] if why else []


RUN_WEEKDAY = 0                           # 0=月曜（1周したあとは週1回だけ）
# 終了コード（★手順書と合わせる★・2026-08-06・Codex133回目）
# ★「書けた」だけを見分けられるようにする★（2026-08-06・Codex135回目 #2）
#   0 に全部まとめると、手順書が「書けた日だけ検証・コミットへ」を判断できない。
EXIT_OK = 0        # 何も変えていない（足すものなし／動かない日／作業中で飛ばした）
EXIT_WROTE = 10    # ★書いた★（この時だけ検証・コミット・push へ進む）
EXIT_WROTE_UNRECORDED = 11  # ★書いたが、見た日を記録できなかった★
#   （2026-08-06・Codex136回目。書いた後に記録で失敗して 1 を返すと、
#     書き換えたまま検証もコミットもされず、翌日は「作業中の変更あり」で
#     飛ばされて誰も気づかない。書いた事実のほうを優先して伝える）
EXIT_ATTENTION = 3 # このレーンだけ終わり（人の判断が要る・一時的な不調）
EXIT_FATAL = 1     # 予期しない失敗（記録が読めない・保存できない等）


def _valid_date(text: str) -> str:
    """日付の形を確かめる（★おかしな文字列を記録に残さない★・#6）。"""
    try:
        return datetime.date.fromisoformat(str(text)).isoformat()
    except (TypeError, ValueError):
        raise Halt(f"日付の形が違います: {text!r}")


def pick_next(today: str):
    """今日見る機種を1つ返す。見ない日は (None, 理由) を返す。

    ★動かす頻度★（2026-08-06・Codex132回目の助言）
      ・まだ一度も見ていない機種があるうちは**毎日1機種**（最初の1周）
      ・全機種を一度見たあとは**週1回（月曜）だけ**
        ＝7機種・普段は増えない・検索にも出ない、に対して毎日は割に合わない
    """
    today = _valid_date(today)            # ★巡回の途中でも必ず確かめる★
    st = _read_state()
    if st.get("last_run") == today:
        return None, "今日はもう見ました"
    rows = targets(strict=True)           # ★判定できない機種があれば止める★
    # ★対象0件は「もう全部仕上がった」場合もある★（2026-08-07）
    #   2026-08-06に旧preview7機種を全部完成記事へ上げた結果、対象が0になり
    #   毎朝「失敗」を報告する状態になっていた。
    #   ★危ないのは「名簿そのものが壊れて0件」★なので、そこだけ見る。
    all_rows = _sj.read_json(os.path.join(BASE, "assets", "data", "machines.json"),
                             expect=(dict, list))
    all_rows = all_rows["machines"] if isinstance(all_rows, dict) else all_rows
    if len(all_rows) < 100:
        return None, f"★機種の名簿が壊れています（{len(all_rows)}件）★"
    if not rows:
        return None, "育てる対象はありません（旧方式の先行記事は残っていません）"
    checked = st.get("checked") or {}
    first_round = any(m["slug"] not in checked for m in rows)
    if not first_round:
        wd = datetime.date.fromisoformat(today).weekday()
        if wd != RUN_WEEKDAY:
            return None, "今日は動かす日ではありません（1周したので週1回）"
    rows.sort(key=lambda m: (str(checked.get(m["slug"], "")), m["slug"]))
    return rows[0]["slug"], ""


def targets(slug: str | None = None, strict: bool = False) -> list:
    ms = _sj.read_json(os.path.join(BASE, "assets", "data", "machines.json"),
                       expect=(dict, list))
    ms = ms["machines"] if isinstance(ms, dict) else ms
    out, broken = [], 0
    for m in ms:
        if slug and m.get("slug") != slug:
            continue
        try:
            if _pd.machine_class(m) == "LEGACY_PREVIEW":
                out.append(m)
        except Exception:                 # noqa: BLE001
            broken += 1                   # ★黙って捨てない★（Codex132回目）
            continue
    if broken:
        # ★一部だけ判定できないのがいちばん危ない★（2026-08-06・Codex133回目 #1）
        #   その機種だけ対象から永久に外れ、残りで「1周した」と誤って判断する。
        if strict:
            raise Halt(f"区分を判定できない機種が {broken} 件あります")
        print(f"  ★区分を判定できない機種が {broken} 件あります★")
    return out


# --------------------------------------------------------------- 材料 → 行

def _num(x):
    """数値だけを通す（★NaN・無限大は数値として扱わない★・Codex126回目 #7）。"""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return x if math.isfinite(x) else None


def ceiling_items(material: dict) -> list:
    """天井の材料 → [(項目キー, 見出し, 値の文, 節)]（★採用ぶんだけ★）。"""
    out = []
    for c in ((material.get("ceilings") or {}).get("adopted") or []):
        kind = c.get("kind")
        jp = _KIND_JP.get(kind)
        amount, unit = _num(c.get("amount")), str(c.get("unit") or "").strip()
        if not jp or amount is None or amount <= 0 or not unit:
            continue                      # ★空の値からは行を作らない★
        counted = str(c.get("counted") or "").strip()
        value = f"{amount:g}{unit}" + (f"（{counted}）" if counted else "")
        ben = str(c.get("benefit") or "").strip()
        if ben:
            value += f" → {ben}"
        out.append((f"天井{kind}", jp, value, "天井・恩恵"))
    return out


def spec_items(material: dict) -> list:
    """基本スペックの材料 → [(項目キー, 見出し, 値の文, 節)]。"""
    ad = material.get("adopted") or {}
    out = []
    mc = (ad.get("model_code") or {}).get("value")
    if isinstance(mc, str) and mc.strip():
        out.append(("型式名", "型式名", mc.strip(), "基本スペック"))
    rng = (ad.get("payout_range") or {}).get("value") or {}
    low, high = _num(rng.get("low")), _num(rng.get("high"))
    if low is not None and high is not None and 50 <= low <= high <= 200:
        out.append(("機械割", "機械割", f"{low}%〜{high}%", "基本スペック"))
    g50 = ((ad.get("games_per_50") or {}).get("value") or {}).get("games")
    g50 = _num(g50)
    if g50 is not None and 10 <= g50 <= 120:
        out.append(("G数50", "50枚あたりのゲーム数", f"約{g50:g}G", "基本スペック"))
    return out


def _dedupe(cand: list) -> list:
    """★同じ項目が2つ以上あれば止める★（2026-08-06・Codex126回目 #2）

    同じ種類の天井が「6スルー」「8スルー」の2件来ると、本文には両方載り、
    早見表には片方だけが載る。どちらが正しいかは機械には決められない。
    """
    seen, out = {}, []
    for item in cand:
        key, label, value = item[0], item[1], item[2]
        if key in seen:
            if seen[key] != value:
                raise Halt(f"同じ項目に違う値が2つあります: {label}"
                           f"（「{seen[key]}」と「{value}」）")
            continue                      # まったく同じなら1件にまとめる
        seen[key] = value
        out.append(item)
    return out


# ★同じ事実を指す見出しの言い換え★（2026-08-07・台帳#262の実データ）
#   「設定変更後」と「設定変更時」が同じ意味なのに別の見出しとして扱われたため、
#   同じ事実が2行に増えていた（4機種で発生・読者に見えていた）。
#   ★突き合わせる時だけ同じものとみなす★＝書く文言は変えない。
#   ★言い換えを1つにまとめると、値が違うときに Halt できるようになる★
#   （いままでは見出しが違うので「別の事実」として黙って足していた）
_LABEL_SAME = {
    "設定変更時": "設定変更後",
    "リセット時": "設定変更後",
    "リセット後": "設定変更後",
    "朝一リセット": "設定変更後",
    "電源OFF/ON": "電源OFF→ON",
    "電源OFF・ON": "電源OFF→ON",
    "電源断": "電源OFF→ON",
}


def _canon_label(label: str) -> str:
    """見出しの言い換えを1つに寄せる（★突き合わせ専用★）。"""
    s = str(label or "").strip().strip("*＊ 　")
    return _LABEL_SAME.get(s, s)


def _value_of(line: str, label: str):
    """本文の1行が同じ見出しなら、その値を返す（違う見出しなら None）。"""
    m = _LABELED.match(str(line).strip())
    if not m or _canon_label(m.group("label")) != _canon_label(label):
        return None
    return m.group("value").replace(SOURCED, "").strip().rstrip("。")


# ------------------------------------------------------- 食い違いを落とす

def _vague_ceiling(text: str, keys: set) -> bool:
    """「天井」とだけ書かれていて、どの天井を指すか決まらないか。

    ★天井が1種類でも分かっている時だけ問題になる★（Codex126回目 #1）。
    種類が書いてあれば決められるので、ここでは False。
    """
    if not any(k.startswith("天井") for k in keys):
        return False
    return "天井" in text and not any(w in text for w in _CEILING_WORDS)


def _residual_ceiling(text: str, keys: set) -> bool:
    """分かった天井の言葉を取り除いても、まだ「天井」の話が残るか。

    ★2026-08-06・Codex127回目 #2★
      「ゲーム数天井は判明しましたが、ほかの天井は未判明です。」のように、
      分かった天井と分からない天井が同じ文にあると、消してはいけない。
    """
    if not any(k.startswith("天井") for k in keys):
        return False
    t = text
    for k in keys:
        for w in _SENT_WORDS.get(k, ()):
            t = t.replace(w, "")
    return "天井" in t


# ★2つ目の話が続く印★（前半だけ分かっても、後半まで消してはいけない）
_CLAUSE_MARK = ("ほか", "他の", "他は", "その他", "ものの", "一方",
                "ですが", "ますが", "以外")


# 文の終わり方（これだけを語尾として許す）
_TAIL_WORDS = ("しています", "しており", "されています", "されておらず",
               "ています", "ており", "ました", "でした", "です", "ます",
               "である", "ない")
# 助詞・記号（つなぎ）
_JOIN = r"[はがのをにでもとやへかねよ、。・\s「」『』（）()【】\-—…]"
# 「解析待ちの項目」の見出しに付く言葉（項目名の一部）
#   ★広い言葉は入れない★（2026-08-06・Codex130回目 #1。「内容」「詳細」
#     「条件」「情報」「本機」のような語を許すと、別の未判明事項まで消せる）
_ITEM_DESC = ("有無", "回数")


def _sentence_form(keys: set):
    """★消してよい文の形★（既知項目＋つなぎ＋未判明の決まり文句＋語尾）

    2026-08-06・Codex130回目 #1。言葉を1つずつ取り除いて「余りが空か」を
    見る方式（袋詰め）は、語の並び順を見ないため
    「スルー天井と本機の情報は未判明です」も空になってしまった。
    文の形そのものを頭からお尻まで照合する。
    """
    known = "|".join(re.escape(w) for k in sorted(keys)
                     for w in _SENT_WORDS.get(k, ()))
    if not known:
        return None
    unknown = "|".join(re.escape(w) for w in _UNKNOWN_MARK)
    tail = "|".join(re.escape(w) for w in _TAIL_WORDS)
    return re.compile(
        rf"^{_JOIN}*(?:(?:{known}){_JOIN}*)+(?:{unknown})(?:{tail})*{_JOIN}*$")


def _only_known(text: str, keys: set, extra=()) -> bool:
    """その文（または項目）が、知っている言葉だけで出来ているか。"""
    if extra:                             # 箇条書き＝項目名なので形が違う
        t = text
        for k in keys:
            for w in _SENT_WORDS.get(k, ()):
                t = t.replace(w, "")
        for w in tuple(extra):
            t = t.replace(w, "")
        return not re.sub(_JOIN, "", t).strip()
    form = _sentence_form(keys)
    return bool(form and form.match(text.strip()))


def guard_drop(text: str, keys: set, topics=(), extra=()) -> None:
    """★これを消してよいか★を1か所で決める（消してはいけなければ Halt）。

    2026-08-06・Codex128回目 #3〜#5。列挙・箇条書き・文でそれぞれ別の判定を
    していたため、片方だけ迂回できた。判断はここに集める。
    """
    for w in tuple(_OTHER_TOPICS) + tuple(topics):
        if w in text:
            raise Halt(f"まだ分からない別の話が混じっています（{w}）: {text[:40]}")
    if _residual_ceiling(text, keys):
        raise Halt(f"分かった天井と分からない天井が同じところにあります: {text[:40]}")
    for w in _CLAUSE_MARK:
        if w in text:
            raise Halt(f"2つ目の話が続いています（{w}）: {text[:40]}")
    # ★天井の材料に欠けがあるなら、天井の未判明文は触らない★
    if topics and any(w in text for w in _CEILING_WORDS):
        raise Halt(f"天井の材料に欠けがあります: {text[:40]}")
    # ★知らない言葉が残るなら、それは別の話★（白名簿・最後の砦）
    if not _only_known(text, keys, extra):
        raise Halt(f"知らない話が混じっています: {text[:40]}")


def _other_ceiling(text: str, keys: set) -> bool:
    """まだ分かっていない種類の天井の話が入っているか。

    ★2026-08-06★「恩恵」が分かっているのは**その天井**についてであって、
    別の種類の天井の恩恵ではない。「・天井ゲーム数と恩恵」（G数天井は未判明）を
    「恩恵は分かっている」と混同しないための見分け。
    """
    mine = {w for k in keys for w in _SENT_WORDS.get(k, ())}
    return any(w in text for w in _CEILING_WORDS if w not in mine)


def _removable(sent: str, keys: set) -> bool:
    """その文が『いま分かった事実』を「まだ分からない」と言っているか。"""
    if not any(w in sent for w in _UNKNOWN_MARK):
        return False
    return any(w in sent for k in keys for w in _SENT_WORDS.get(k, ()))


def _enum_rest(text: str, keys: set, topics=()):
    """「A・B：解析判明次第追記します」から、分かった項目を抜く。

    戻り値は (残した文 or "") ／ 形が違えば None。
    """
    m = _ENUM.match(text.strip())
    if not m:
        return None
    raw = m.group("items")
    sep = (_SEP.search(raw).group(0) if _SEP.search(raw) else "・")
    keep = []
    for x in _SEP.split(raw):
        if any(w in x for k in keys for w in _SENT_WORDS.get(k, ())):
            # 列挙も項目名なので、箇条書きと同じ形で見る
            guard_drop(x, keys, topics, _ITEM_DESC)
            continue                      # 分かった項目なので抜く
        if _vague_ceiling(x, keys):
            # ★どの天井を指すのか決まらない項目は、勝手に扱わない★
            raise Halt(f"どの天井を指すのか決まらない項目があります: {x[:24]}")
        keep.append(x)
    return f"**{sep.join(keep)}**：解析判明次第追記します。" if keep else ""


def resolve_contradictions(after: dict, keys: set, topics=(),
                           known_topics=()) -> list:
    """食い違う文を落とす計画を作る（★after を直接は書き換えない★）。

    戻り値は [(節の番号, 元の文, 直した文 or None)]。
    ★決められない時は Halt★（黙って消さない・黙って残さない）。
    """
    edits = []
    if not keys:
        return edits
    for i, sec in enumerate(after.get("sections") or []):
        if not isinstance(sec.get("body"), list):
            continue
        title = str(sec.get("title") or "")
        listing = ("解析待ち" in title) or ("未確認" in title)
        for b in sec["body"]:
            t = str(b)
            # ★分かっている項目を「まだ」と書いていないか★（構文より先に見る）
            #   2026-08-06・Codex130回目 #2。以前は通常文だけ見ていたので、
            #   箇条書き「・恩恵の有無」や列挙「**恩恵**：解析判明次第…」が
            #   判明済みの恩恵と同じページに並んで残った。
            implicit = ((listing and t.strip().startswith("・"))
                        or bool(_ENUM.match(t.strip())))
            if (implicit or any(u in t for u in _UNKNOWN_MARK))                     and not _other_ceiling(t, keys):
                for kt in known_topics:
                    if any(w in t for w in _TOPIC_WORDS.get(kt, ())):
                        raise Halt("分かっている項目を『まだ』と書いています: "
                                   + t[:40])
            if listing and t.strip().startswith("・"):
                hit = any(w in t for k in keys
                          for w in _PENDING_WORDS.get(k, ()))
                if hit:
                    # 箇条書きは項目名なので、見出しに付く言葉は許す
                    guard_drop(t, keys, topics, _ITEM_DESC)
                if not hit and _vague_ceiling(t, keys)                         and not any(w in t for w in _OTHER_TOPICS):
                    # ★「・リセット時の天井短縮」のような別の話は、そのまま残す★
                    raise Halt(f"どの天井を指すのか決まらない項目があります: {t[:28]}")
                if hit:
                    edits.append((i, t, None))
                continue
            rest = _enum_rest(t, keys, topics)
            if rest is not None:
                if rest.strip() != t.strip():
                    edits.append((i, t, rest or None))
                continue
            sents = [s for s in re.split(r"(?<=。)", t) if s.strip()]
            drop = [s for s in sents if _removable(s, keys)]
            for s2 in sents:
                if s2 in drop or not any(w in s2 for w in _UNKNOWN_MARK):
                    continue
                if _vague_ceiling(s2, keys):
                    raise Halt("どの天井を指すのか決まらない文があります: "
                               + s2[:44])
            if not drop:
                continue
            # ★別の「まだ分からない話」が混じる文は自分で決めない★
            for s in drop:
                guard_drop(s, keys, topics)
            new = "".join(s for s in sents if s not in drop).strip()
            edits.append((i, t, new or None))
    return edits


def _apply_edits(after: dict, edits: list, adds: dict, facts=()) -> dict:
    """計画どおりに本文を組み立てる。

    ★未確認の断りは「その節に事実があるか」で外す★（Codex127回目 #1）
      以前は「今回足したか」で判断していたので、前回すでに書いてある節では
      事実と「未確認です」が並んだままになった。
    """
    drop = {(i, b): a for i, b, a in edits}
    has_fact = set(adds or {}) | set(facts or ())
    for i, sec in enumerate(after.get("sections") or []):
        if not isinstance(sec.get("body"), list):
            continue
        body = []
        for b in sec["body"]:
            key = (i, str(b))
            if key in drop:
                if drop[key]:
                    body.append(drop[key])
                continue
            if str(b).strip() == PENDING and i in has_fact:
                continue                  # ★中身が入ったら断りは外す★
            body.append(b)
        sec["body"] = adds.get(i, []) + body
        if not sec["body"]:
            sec["body"] = [PENDING]       # ★節を空にしない★
    return after


# -------------------------------------------------------------- 早見表

# 早見表の「まだ分かりません」を表す値
_BOX_PENDING = ("解析待ち", "未確認", "調査中", "-", "－", "")
# 早見表に出す天井の優先順（★1つだけ出す欄なので順番を決めておく★）
_BOX_ORDER = ("天井GAME", "天井CYCLE", "天井POINT", "天井THROUGH")


def _apply_boxes(detail: dict, boxes: list) -> dict:
    """早見表の計画を当てる（★計画にある欄だけ★）。"""
    for i, before, after_v in boxes:
        box = (detail.get("summaryBoxes") or [])[i]
        if str(box.get("value") or "").strip() != str(before):
            raise Halt("早見表が計画と違います（先に誰かが書き換えた可能性）")
        box["value"] = after_v
    return detail


def _plan_summary(detail: dict, ceilings: list, present: set) -> list:
    """★早見表の「天井：解析待ち」を、記事に載せた天井にそろえる★

    2026-08-06・Codex125回目 #1。本文に「1200G」と書きながら、同じページの
    早見表が「解析待ち」のままだった。読者には両方見える。
    """
    got = {k: v for k, _lb, v, _s in ceilings if k in present}
    if not got:
        return []
    key = next((k for k in _BOX_ORDER if k in got), None)
    if key is None:
        return []
    value = got[key].split(" → ")[0]      # 恩恵は長いので欄には出さない
    if len(got) > 1:
        value += " ほか"
    out = []
    for i, box in enumerate(detail.get("summaryBoxes") or []):
        if str(box.get("label") or "").strip() != "天井":
            continue
        before = str(box.get("value") or "").strip()
        if before == value:
            continue
        if before not in _BOX_PENDING:
            # ★すでに別の天井が書いてある＝どちらが正しいか決められない★
            raise Halt(f"早見表にすでに別の天井が書かれています（{before}）")
        out.append((i, before, value))
    return out


# ------------------------------------------------------------------ 計画

def plan(machine: dict, material: dict, detail: dict) -> dict:
    """記事に足す内容を決める（★書き込まない★・決められなければ Halt★）。"""
    if str(detail.get("slug") or "") != str(machine.get("slug")) or \
            str(detail.get("name") or "") != str(machine.get("name")):
        raise Halt("記事の中身が名簿と合いません（slug・機種名の不一致）")
    after = json.loads(json.dumps(detail))
    secs = after.get("sections") or []
    idx = {}
    for i, sec in enumerate(secs):        # ★同名の節があれば決めない★（#6）
        idx.setdefault(str(sec.get("title")), []).append(i)
    cand = _dedupe(ceiling_items(material) + spec_items(material))

    present, adds, added_lines, facts = set(), {}, [], set()
    for key, label, value, want in cand:
        same = False
        for i, s in enumerate(secs):      # ★全部の節を見る★（同じ見出しの重複防止）
            for b in (s.get("body") or []):
                got = _value_of(b, label)
                if got is None:
                    continue
                if got == value:
                    same = True
                    facts.add(i)          # ★この節にはもう事実がある★
                else:
                    raise Halt(f"すでに違う値が書かれています: {label} "
                               f"（記事「{got}」／材料「{value}」）")
        if same:
            present.add(key)              # もう書いてある
            continue
        if want not in idx:
            # ★置く節が無い＝記事に載らない★
            #   載っていない事実で打ち消し文を消すと、値がどこにも無いまま
            #   「まだ分かりません」だけが消える（2026-08-06・Codex125回目 #6）
            continue
        if len(idx[want]) != 1:
            raise Halt(f"同じ名前の節が{len(idx[want])}つあり、どこへ書くか決められません"
                       f"（{want}）")
        present.add(key)
        line = f"**{label}**：{value}{SOURCED}"
        adds.setdefault(idx[want][0], []).append(line)
        added_lines.append(f"{want}: {line}")

    # ★恩恵が分かっていない天井があるなら「恩恵」の話は守る★（Codex127回目 #2）
    topics, known_topics = set(), set()
    for c in ((material.get("ceilings") or {}).get("adopted") or []):
        (topics if not str(c.get("benefit") or "").strip()
         else known_topics).add("恩恵")
        (topics if not str(c.get("counted") or "").strip()
         else known_topics).add("何回")
    known_topics -= topics                # 欠けがあるほうを優先する
    # ★食い違いを落としてよいのは「記事に載っている事実」だけ★
    edits = resolve_contradictions(after, present, topics, known_topics)
    after = _apply_edits(after, edits, adds, facts)
    boxes = _plan_summary(after, _dedupe(ceiling_items(material)), present)
    after = _apply_boxes(after, boxes)
    return {"slug": machine["slug"], "detail": after, "before": detail,
            "added": added_lines, "edits": edits, "boxes": boxes,
            "adds": adds, "facts": sorted(facts),
            "added_lines": [x.split(": ", 1)[1] for x in added_lines]}


def check(before: dict, after: dict, edits=(), added_lines=(), boxes=(),
          adds=None, facts=()) -> list:
    """★計画どおりに組み立て直して、完全に一致するか確かめる★

    2026-08-06・Codex126回目 #5。以前は「知らない文が増えていないか」しか
    見ていなかったので、**計画したのに実行されていない**（早見表を直し忘れた・
    足すはずの行が無い・別の節に入った）を通してしまった。
    ここでは before に計画を当て直し、**一字一句同じ**であることを求める。
    """
    ng = []
    # --- 計画そのものの妥当性 ---
    b_secs = before.get("sections") or []
    for i, orig, _new in (edits or []):
        if i >= len(b_secs) or str(orig) not in [str(x) for x in
                                                 (b_secs[i].get("body") or [])]:
            ng.append(f"消す予定の文が{i}番目の節にありません（{str(orig)[:28]}…）")
    for line in (added_lines or []):
        if not _LABELED.match(str(line)) or SOURCED not in str(line):
            ng.append(f"足す行の形が決まりと違います（{str(line)[:32]}…）")
    if ng:
        return ng
    # --- 組み立て直して完全一致 ---
    try:
        expect = _apply_edits(json.loads(json.dumps(before)),
                              list(edits or []), dict(adds or {}),
                              tuple(facts or ()))
        expect = _apply_boxes(expect, list(boxes or []))
    except Exception as e:                # noqa: BLE001
        return [f"計画を組み立て直せません: {e}"]
    if expect == after:
        return []
    e_secs, a_secs = expect.get("sections") or [], after.get("sections") or []
    if len(e_secs) != len(a_secs):
        return ["節の数が計画と違います"]
    for i, (es, as_) in enumerate(zip(e_secs, a_secs)):
        if str(es.get("title")) != str(as_.get("title")):
            ng.append(f"{i}番目の節の名前が計画と違います")
        eb = [str(x) for x in (es.get("body") or [])]
        ab = [str(x) for x in (as_.get("body") or [])]
        if eb != ab:
            miss = [x for x in eb if x not in ab]
            extra = [x for x in ab if x not in eb]
            if miss:
                ng.append(f"{es.get('title')}: 計画にある文がありません"
                          f"（{miss[0][:30]}…）")
            if extra:
                ng.append(f"{es.get('title')}: 計画にない文があります"
                          f"（{extra[0][:30]}…）")
            if not miss and not extra:
                ng.append(f"{es.get('title')}: 文の並び・個数が計画と違います")
    eb_box = expect.get("summaryBoxes") or []
    ab_box = after.get("summaryBoxes") or []
    if eb_box != ab_box:
        ng.append("早見表が計画と違います")
    return ng or ["計画と中身が違います（本文・早見表以外）"]


# ------------------------------------------------------------------ 実行

def _lock(path: str):
    """★同時に触らないための鍵★（2026-08-06・Codex135回目で作り替え）

    ★OSに任せる★＝ファイルの中身で持ち主を判断するのをやめた。
      自前で「置き去りかどうか」を判断する方式は、どう作っても
      「読んでから消すまでの間に別の処理が鍵を作る」競合が残る
      （Aが古い鍵を消して自分の鍵を作った直後に、Bが『古い鍵』のつもりで
        Aの鍵を消してしまう）。
      OSのファイルロックなら、**プロセスが終わった時点で必ず外れる**ので、
      置き去りも、奪い合いも、合言葉の照合も要らない。
    使い方: lk = _lock(path) … _unlock(lk)
    """
    fh = open(path + ".lock", "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise Halt("ほかの処理が動いています（鍵が取れません）")
    return fh


def _unlock(fh) -> None:
    """鍵を外す（★閉じれば OS が必ず外す★ので取りこぼしが無い）。"""
    if not fh:
        return
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        fh.close()
    except OSError:
        pass


def _write(path: str, detail: dict, sha_before: str) -> None:
    """★鍵の中で「もう一度確かめて→置き換える」★（間に何も挟まない）

    2026-08-06・Codex126回目 #4。以前は確かめたあとに import を挟んでいたので、
    その隙に誰かが書いた内容を上書きできた。
    """
    text = json.dumps(detail, ensure_ascii=False, indent=1) + "\n"
    lock = _lock(path)
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())          # ★中身が確実に書けてから★
        if _sha(path) != sha_before:      # ★置き換える直前に確かめる★
            raise Halt("書く直前にファイルが変わっていました（同時に触られた可能性）")
        os.replace(tmp, path)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
        _unlock(lock)                     # ★自分の鍵だけ外す★


def run(slug: str, apply_it: bool, gather=None) -> dict:
    ms = targets(slug)
    if not ms:
        return {"slug": slug, "problems": ["対象ではありません（旧方式の先行記事のみ）"]}
    m = ms[0]
    if gather is None:
        import add_machine_run as _amr
        gather = _amr.gather
    got = gather(m["name"])
    mat = got.get("material") or {}
    if not mat:
        return {"slug": slug, "problems": ["材料を集められません: "
                                           + " / ".join(got.get("problems") or [])[:160]]}
    path = os.path.join(DETAILS, f"{slug}.json")
    sha_before = _sha(path)
    detail = _sj.read_json(path, expect=dict)
    try:
        pl = plan(m, mat, detail)
    except Halt as e:
        return {"slug": slug, "problems": [f"★止めました★ {e}"]}
    ng = check(pl["before"], pl["detail"], pl["edits"], pl["added_lines"],
               pl["boxes"], pl["adds"], pl["facts"])
    if ng:
        return {"slug": slug, "problems": ng}
    res = {"slug": slug, "added": pl["added"],
           "boxes": [f"早見表 天井：{x[1]} → {x[2]}" for x in pl["boxes"]],
           "removed": [b for _i, b, a in pl["edits"] if a is None],
           "rewrote": [(b, a) for _i, b, a in pl["edits"] if a],
           "wrote": False, "problems": []}
    if apply_it and (pl["added"] or pl["edits"] or pl["boxes"]):
        try:
            _write(path, pl["detail"], sha_before)
        except Halt as e:
            return {"slug": slug, "problems": [f"★止めました★ {e}"]}
        res["wrote"] = True
    return res


# ------------------------------------------------------------------ selftest

def selftest() -> int:                    # noqa: C901
    ok, ran = True, [0]

    def t(name, cond):
        nonlocal ok
        ran[0] += 1
        print(("✅ " if cond else "❌ ") + name)
        ok = ok and bool(cond)

    MAT = {"adopted": {"model_code": {"value": "L機/1"},
                       "payout_range": {"value": {"low": 97.0, "high": 110.0}},
                       "games_per_50": {"value": {"games": 32}}},
           "ceilings": {"adopted": [
               {"kind": "THROUGH", "amount": 6, "unit": "スルー",
                "counted": "CZ", "benefit": ""}]}}
    t("★★採用された材料だけを行にする★★",
      ceiling_items(MAT) == [("天井THROUGH", "スルー天井", "6スルー（CZ）", "天井・恩恵")]
      and len(spec_items(MAT)) == 3)
    t("★★値が欠けた材料からは行を作らない★★",
      ceiling_items({"ceilings": {"adopted": [
          {"kind": "GAME", "amount": None, "unit": "G"},
          {"kind": "GAME", "amount": 0, "unit": "G"}]}}) == []
      and spec_items({"adopted": {"model_code": {"value": None},
                                  "payout_range": {"value": {"low": None,
                                                             "high": 110}}}}) == [])
    t("　あり得ない数字は採らない（機械割300%・50枚1G）",
      spec_items({"adopted": {"payout_range": {"value": {"low": 97, "high": 300}},
                              "games_per_50": {"value": {"games": 1}}}}) == [])

    MACH = {"slug": "x", "name": "機種X"}

    def D(sections):
        return {"slug": "x", "name": "機種X", "sections": sections}

    d = D([{"title": "天井・恩恵", "body": [PENDING]},
           {"title": "基本スペック", "body": ["**メーカー**：A社"]}])
    pl = plan(MACH, MAT, d)
    t("　足すだけなら通る",
      check(pl["before"], pl["detail"], pl["edits"], pl["added_lines"],
            pl["boxes"], pl["adds"]) == []
      and pl["detail"]["sections"][0]["body"]
      == ["**スルー天井**：6スルー（CZ）" + SOURCED])

    # --- ★同じ項目に違う値があったら止める★（Codex125 #3）
    d2 = D([{"title": "基本スペック",
             "body": ["**機械割**：97.0%〜105.0%"]}])
    halted = False
    try:
        plan(MACH, MAT, d2)
    except Halt as e:
        halted = "すでに違う値" in str(e)
    t("★★すでに違う値が書いてあったら書かずに止める★★", halted)
    d3 = D([{"title": "基本スペック",
             "body": [f"**機械割**：97.0%〜110.0%{SOURCED}"]}])
    pl3 = plan(MACH, MAT, d3)
    t("　同じ値なら二重に書かない",
      not any("機械割" in x for x in pl3["added"]))

    # --- ★別の話が混じる文は自分で決めない★（Codex125 #4）
    d4 = D([{"title": "天井・恩恵", "body": [PENDING]},
            {"title": "狙い目の根拠",
             "body": ["スルー天井は判明していませんので狙い目を出せません。"]}])
    halted4 = False
    try:
        plan(MACH, MAT, d4)
    except Halt as e:
        halted4 = "別の話" in str(e)
    t("★★落としてよいか決められない文があれば止める★★（狙い目の話が混じる）",
      halted4)
    # ★材料に欠けがない天井★（恩恵・数える対象がそろっている）
    MAT_FULL = {"adopted": MAT["adopted"],
                "ceilings": {"adopted": [
                    {"kind": "THROUGH", "amount": 6, "unit": "スルー",
                     "counted": "CZ", "benefit": "AT当選"}]}}
    d5 = D([{"title": "天井・恩恵", "body": [PENDING]},
            {"title": "ゲーム性",
             "body": ["スルー天井は判明していません。", "残す文。"]}])
    pl5 = plan(MACH, MAT_FULL, d5)
    t("　混ざり物が無ければ、その文だけ落とす",
      pl5["detail"]["sections"][1]["body"] == ["残す文。"]
      and check(pl5["before"], pl5["detail"], pl5["edits"],
                pl5["added_lines"], pl5["boxes"], pl5["adds"]) == [])

    d5b = D([{"title": "天井・恩恵", "body": [PENDING]},
             {"title": "ゲーム性", "body": ["天井は判明していません。"]}])
    halted5b = False
    try:
        plan(MACH, MAT, d5b)
    except Halt as e:
        halted5b = "どの天井" in str(e)
    t("★★どの天井を指すのか決まらない文は、消さずに止める★★"
      "（スルー天井だけ分かった時にG数天井の未判明を消さない）", halted5b)
    t("　種類が書いてあれば取り違えない",
      _enum_rest("**天井ゲーム数・スルー天井**：解析判明次第追記します。",
                 {"天井THROUGH"})
      == "**天井ゲーム数**：解析判明次第追記します。")

    # --- ★同じ項目に違う値が2つ★（Codex126 #2）
    dup = {"ceilings": {"adopted": [
        {"kind": "THROUGH", "amount": 6, "unit": "スルー", "counted": "CZ"},
        {"kind": "THROUGH", "amount": 8, "unit": "スルー", "counted": "CZ"}]}}
    halted_dup = False
    try:
        _dedupe(ceiling_items(dup))
    except Halt as e:
        halted_dup = "違う値が2つ" in str(e)
    t("★★同じ項目に違う値が2つ来たら止める★★", halted_dup)
    same = {"ceilings": {"adopted": [
        {"kind": "GAME", "amount": 1200, "unit": "G", "counted": "通常時"},
        {"kind": "GAME", "amount": 1200, "unit": "G", "counted": "通常時"}]}}
    t("　まったく同じなら1件にまとめる", len(_dedupe(ceiling_items(same))) == 1)

    # ★言い換えの見出しを同じ事実として扱う★（2026-08-07・台帳#262）
    #   「設定変更時」と「設定変更後」を別物として数えていたため、
    #   同じ事実が2行に増えて読者に見えていた（4機種で発生）。
    t("★★見出しの言い換えを同じ事実とみなす★★（設定変更時＝設定変更後・台帳#262）",
      _value_of("**設定変更時**：天井が650G+αに短縮", "設定変更後") == "天井が650G+αに短縮"
      and _value_of("**リセット時**：天井が650G+αに短縮", "設定変更後") is not None)
    t("　言い換えでない見出しまで混ぜない",
      _value_of("**電源OFF→ON**：引き継ぎ", "設定変更後") is None
      and _value_of("**通常B**：650G+α", "設定変更後") is None)
    t("★★NaN・無限大は数値として扱わない★★",
      ceiling_items({"ceilings": {"adopted": [
          {"kind": "GAME", "amount": float("nan"), "unit": "G"},
          {"kind": "GAME", "amount": float("inf"), "unit": "G"}]}}) == [])

    # --- ★同じ名前の節が2つあれば、どこへ書くか決めない★（Codex126 #6）
    d_two = D([{"title": "基本スペック", "body": ["A"]},
               {"title": "基本スペック", "body": ["B"]}])
    halted_two = False
    try:
        plan(MACH, {"adopted": MAT["adopted"]}, d_two)
    except Halt as e:
        halted_two = "どこへ書くか決められません" in str(e)
    t("★★同じ名前の節が2つあれば書かずに止める★★", halted_two)

    # --- ★計画したのに実行されていなければ止める★（Codex126 #5）
    d_np = D([{"title": "天井・恩恵", "body": [PENDING]},
              {"title": "基本スペック", "body": ["**メーカー**：A社"]}])
    pl_np = plan(MACH, MAT, d_np)
    t("★★計画どおりに組み立て直して一致する★★",
      check(pl_np["before"], pl_np["detail"], pl_np["edits"],
            pl_np["added_lines"], pl_np["boxes"], pl_np["adds"]) == [])
    t("★★計画したのに何も変えていなければ止める★★",
      check(pl_np["before"], pl_np["before"], pl_np["edits"],
            pl_np["added_lines"], pl_np["boxes"], pl_np["adds"]) != [])
    moved = json.loads(json.dumps(pl_np["detail"]))
    line = moved["sections"][0]["body"].pop(0)
    moved["sections"][1]["body"].insert(0, line)
    t("★★足す行を別の節に置いたら止める★★",
      check(pl_np["before"], moved, pl_np["edits"], pl_np["added_lines"],
            pl_np["boxes"], pl_np["adds"]) != [])

    # --- ★「解析判明次第」の並び★（Codex125 #5）
    t("★★区切りが「／」でも、分かった項目だけ抜く★★",
      _enum_rest("**機械割／コイン単価**：解析判明次第追記します。", {"機械割"})
      == "**コイン単価**：解析判明次第追記します。")
    t("　コロン前に空白があっても同じに扱う",
      _enum_rest("**機械割・コイン単価** ：解析判明次第追記します。", {"機械割"})
      == "**コイン単価**：解析判明次第追記します。")
    t("　全部分かったら行ごと落とす",
      _enum_rest("**機械割**：解析判明次第追記します。", {"機械割"}) == "")

    # --- ★足せなかった事実で打ち消し文を消さない★（Codex125 #6）
    d6 = D([{"title": "ゲーム性", "body": ["機械割は判明していません。"]}])
    pl6 = plan(MACH, {"adopted": MAT["adopted"]}, d6)
    t("★★置く節が無い時は、その項目の打ち消し文も消さない★★",
      pl6["detail"]["sections"][0]["body"] == ["機械割は判明していません。"])

    # --- ★「解析待ちの項目」の箇条書き★
    d7 = D([{"title": "天井・恩恵", "body": [PENDING]},
            {"title": "解析待ちの項目",
             "body": ["・天井ゲーム数と恩恵", "・スルー天井の有無と回数",
                      "・リセット時の天井短縮"]}])
    pl7 = plan(MACH, MAT_FULL, d7)
    t("★★分かった項目だけ『解析待ちの項目』から消す★★（似た言葉を巻き込まない）",
      pl7["detail"]["sections"][1]["body"]
      == ["・天井ゲーム数と恩恵", "・リセット時の天井短縮"])

    # --- ★勝手な削除・追加を止める柵★（Codex125 #4後段・#7）
    b8 = D([{"title": "A", "body": ["残す文。"]}, {"title": "A", "body": ["別の文。"]}])
    a8 = json.loads(json.dumps(b8))
    a8["sections"][0]["body"] = []
    t("★★同じ名前の節が2つあっても、消えたら気づく★★",
      any("計画にある文がありません" in x for x in check(b8, a8)))
    a9 = json.loads(json.dumps(b8))
    a9["sections"][0]["body"].append("勝手に足した文。")
    t("★★決めていない文が増えたら止める★★",
      any("計画にない文があります" in x for x in check(b8, a9)))

    # --- ★早見表と本文の食い違い★（Codex125 #1）
    d11 = {"slug": "x", "name": "機種X",
           "summaryBoxes": [{"label": "天井", "value": "解析待ち"},
                            {"label": "狙い目", "value": "解析待ち"}],
           "sections": [{"title": "天井・恩恵", "body": [PENDING]}]}
    pl11 = plan(MACH, MAT, d11)
    t("★★天井を載せたら早見表もそろえる★★（同じページで食い違わせない）",
      pl11["detail"]["summaryBoxes"][0]["value"] == "6スルー（CZ）"
      and pl11["detail"]["summaryBoxes"][1]["value"] == "解析待ち"
      and check(pl11["before"], pl11["detail"], pl11["edits"],
                pl11["added_lines"], pl11["boxes"], pl11["adds"]) == [])
    d12 = json.loads(json.dumps(d11))
    d12["summaryBoxes"][0]["value"] = "1200G"
    halted12 = False
    try:
        plan(MACH, MAT, d12)
    except Halt as e:
        halted12 = "早見表にすでに別の天井" in str(e)
    t("★★早見表に別の天井があれば書かずに止める★★", halted12)
    a13 = json.loads(json.dumps(pl11["detail"]))
    a13["summaryBoxes"][1]["value"] = "600G〜"
    t("★★決めていない欄が変わったら止める★★",
      any("早見表" in x for x in check(pl11["before"], a13, pl11["edits"],
                                       pl11["added_lines"], pl11["boxes"],
                                       pl11["adds"])))

    # --- ★Codex127回目に挙げられた迂回例★
    d14 = D([{"title": "天井・恩恵",
              "body": ["**スルー天井**：6スルー（CZ）" + SOURCED, PENDING]}])
    pl14 = plan(MACH, MAT, d14)
    t("★★すでに事実が書いてある節でも、未確認の断りは外す★★（#1の迂回例）",
      pl14["detail"]["sections"][0]["body"]
      == ["**スルー天井**：6スルー（CZ）" + SOURCED]
      and check(pl14["before"], pl14["detail"], pl14["edits"],
                pl14["added_lines"], pl14["boxes"], pl14["adds"],
                pl14["facts"]) == [])
    d15 = D([{"title": "天井・恩恵", "body": [PENDING]},
             {"title": "ゲーム性",
              "body": ["スルー天井の恩恵は判明していません。"]}])
    halted15 = False
    try:
        plan(MACH, MAT, d15)              # 材料の benefit は空
    except Halt as e:
        halted15 = "別の話" in str(e)
    t("★★回数だけ分かった天井の『恩恵は未判明』は消さない★★（#2の迂回例）",
      halted15)
    d16 = D([{"title": "天井・恩恵", "body": [PENDING]},
             {"title": "ゲーム性",
              "body": ["スルー天井は判明しましたが、ほかの天井は未判明です。"]}])
    halted16 = False
    try:
        plan(MACH, MAT_FULL, d16)
    except Halt as e:
        halted16 = "分からない天井が同じ" in str(e)
    t("★★分かった天井と分からない天井が同じ文にあれば止める★★（#2の迂回例）",
      halted16)
    d17 = D([{"title": "天井・恩恵", "body": [PENDING]},
             {"title": "解析待ちの項目", "body": ["・天井の有無と回数"]}])
    halted17 = False
    try:
        plan(MACH, MAT, d17)
    except Halt as e:
        halted17 = "どの天井" in str(e)
    t("★★『・天井の有無と回数』のような曖昧な項目でも止める★★（#2の迂回例）",
      halted17)

    # --- ★Codex128回目に挙げられた迂回例★
    d18 = D([{"title": "天井・恩恵", "body": [PENDING]},
             {"title": "基本スペック",
              "body": ["**スルー天井とほかの天井**：解析判明次第追記します。"]}])
    h18 = False
    try:
        plan(MACH, MAT_FULL, d18)
    except Halt as e:
        h18 = "2つ目の話" in str(e) or "分からない天井" in str(e)
    t("★★列挙の項目に2つ目の話が続いていれば止める★★（#3の迂回例）", h18)
    d19 = D([{"title": "天井・恩恵", "body": [PENDING]},
             {"title": "基本スペック",
              "body": ["**スルー天井の恩恵**：解析判明次第追記します。"]}])
    h19 = False
    try:
        plan(MACH, MAT, d19)              # 材料の恩恵は空
    except Halt as e:
        h19 = "別の話" in str(e) or "欠け" in str(e)
    t("★★恩恵が未判明なら、列挙の『恩恵』も消さない★★（#3の迂回例）", h19)
    d20 = D([{"title": "天井・恩恵", "body": [PENDING]},
             {"title": "解析待ちの項目",
              "body": ["・スルー天井の有無とリセット時の短縮"]}])
    h20 = False
    try:
        plan(MACH, MAT_FULL, d20)
    except Halt as e:
        h20 = "別の話" in str(e)
    t("★★箇条書きに別の話が同居していれば止める★★（#4の迂回例）", h20)
    d21 = D([{"title": "天井・恩恵", "body": [PENDING]},
             {"title": "ゲーム性",
              "body": ["スルー天井は判明しましたが、ほかは未判明です。"]}])
    h21 = False
    try:
        plan(MACH, MAT_FULL, d21)
    except Halt as e:
        h21 = "2つ目の話" in str(e)
    t("★★『ほかは未判明』のような言い方でも止める★★（#5の迂回例）", h21)
    d22 = D([{"title": "天井・恩恵", "body": [PENDING]},
             {"title": "ゲーム性",
              "body": ["スルー天井到達時の特典は未判明です。"]}])
    h22 = False
    try:
        plan(MACH, MAT, d22)              # 材料の恩恵が空
    except Halt as e:
        h22 = "欠け" in str(e)
    t("★★材料に欠けがある天井の未判明文は触らない★★（#5の言い換え迂回）", h22)

    # --- ★Codex129回目に挙げられた迂回例★
    d23 = D([{"title": "天井・恩恵", "body": [PENDING]},
             {"title": "ゲーム性", "body": ["恩恵は未判明です。"]}])
    h23 = False
    try:
        plan(MACH, MAT_FULL, d23)         # 恩恵は分かっている
    except Halt as e:
        h23 = "未判明" in str(e)
    t("★★分かっている項目を『未判明』と書いた文があれば止める★★（#1の迂回例）",
      h23)
    d24 = D([{"title": "天井・恩恵", "body": [PENDING]},
             {"title": "ゲーム性",
              "body": ["スルー天井とボーナス仕様は未判明です。"]}])
    h24 = False
    try:
        plan(MACH, MAT_FULL, d24)
    except Halt as e:
        h24 = "知らない話" in str(e)
    t("★★言葉の一覧に無い話題でも、余りが残れば止める★★（#2の迂回例・白名簿）",
      h24)
    t("　知っている言葉だけの文は消してよい",
      _only_known("スルー天井は判明していません。", {"天井THROUGH"})
      and not _only_known("スルー天井とボーナス仕様は未判明です。",
                          {"天井THROUGH"}))

    # --- ★Codex130回目に挙げられた迂回例★
    d25 = D([{"title": "天井・恩恵", "body": [PENDING]},
             {"title": "ゲーム性",
              "body": ["スルー天井と本機の情報は未判明です。"]}])
    h25 = False
    try:
        plan(MACH, MAT_FULL, d25)
    except Halt as e:
        h25 = "知らない話" in str(e)
    t("★★語を袋詰めで消さない★★（『本機の情報』まで消せた迂回・#1）", h25)
    t("　消してよい形だけを通す",
      _only_known("スルー天井は判明していません。", {"天井THROUGH"})
      and not _only_known("スルー天井と本機の情報は未判明です。",
                          {"天井THROUGH"})
      and not _only_known("スルー天井のデータは未判明です。", {"天井THROUGH"}))
    d26 = D([{"title": "天井・恩恵", "body": [PENDING]},
             {"title": "解析待ちの項目", "body": ["・恩恵の有無"]}])
    h26 = False
    try:
        plan(MACH, MAT_FULL, d26)         # 恩恵は分かっている
    except Halt as e:
        h26 = "『まだ』と書いています" in str(e)
    t("★★箇条書きでも『分かっている項目の解析待ち』を止める★★（#2）", h26)
    d27 = D([{"title": "天井・恩恵", "body": [PENDING]},
             {"title": "基本スペック",
              "body": ["**恩恵**：解析判明次第追記します。"]}])
    h27 = False
    try:
        plan(MACH, MAT_FULL, d27)
    except Halt as e:
        h27 = "『まだ』と書いています" in str(e)
    t("★★列挙でも『分かっている項目の解析待ち』を止める★★（#2）", h27)
    d28 = D([{"title": "天井・恩恵", "body": [PENDING]},
             {"title": "解析待ちの項目", "body": ["・天井ゲーム数と恩恵"]}])
    pl28 = plan(MACH, MAT_FULL, d28)      # G数天井は未判明なので触らない
    t("★★別の種類の天井の恩恵は「分かっている」に数えない★★（止めすぎ防止）",
      pl28["detail"]["sections"][1]["body"] == ["・天井ゲーム数と恩恵"])

    # --- ★無人運転（--next）★
    import tempfile
    keep_state, keep_targets = globals()["STATE"], globals()["targets"]
    tmpd = tempfile.mkdtemp()
    try:
        globals()["STATE"] = os.path.join(tmpd, "st.json")
        globals()["targets"] = lambda slug=None, strict=False: [
            {"slug": "a"}, {"slug": "b"}, {"slug": "c"}]
        with open(globals()["STATE"], "w", encoding="utf-8") as f:
            json.dump({"checked": {"a": "2026-08-05", "b": "2026-08-01"},
                       "last_run": "2026-08-05"}, f)
        t("★★いちばん長く見ていない機種を選ぶ★★（見たことが無い機種が最優先）",
          pick_next("2026-08-06")[0] == "c")
        t("　まだ全機種を見ていないうちは毎日動く（曜日を問わない）",
          pick_next("2026-08-08")[0] == "c")      # 2026-08-08は土曜
        streak = _mark_checked("c", "2026-08-06", "none")
        st = json.load(open(globals()["STATE"], encoding="utf-8"))
        t("　見た日と結果を記録する",
          st["checked"]["c"] == "2026-08-06"
          and st["counts"]["c"]["none"] == 1 and streak == 0)
        t("★★同じ日に2回は動かない★★", pick_next("2026-08-06")[0] is None)
        t("★★1周したら週1回（月曜）だけ★★",
          pick_next("2026-08-08")[0] is None          # 土曜
          and pick_next("2026-08-10")[0] == "b")      # 月曜
        t("★★一時的な不調は続いた回数を数える★★",
          _mark_checked("b", "2026-08-10", "transient") == 1
          and _mark_checked("b", "2026-08-17", "transient") == 2
          and _mark_checked("b", "2026-08-24", "none") == 0)
        with open(globals()["STATE"], "w", encoding="utf-8") as f:
            f.write("{壊れた")
        broke = False
        try:
            pick_next("2026-09-01")
        except Halt as e:
            broke = "上書きしません" in str(e)
        t("★★記録が壊れていたら、空として書き戻さない★★（他の記録を消さない）",
          broke)
        os.remove(globals()["STATE"])
        t("　記録がまだ無ければ、いちばん先頭から始める",
          pick_next("2026-09-01")[0] == "a")
        globals()["targets"] = lambda slug=None, strict=False: []
        got, why = pick_next("2026-09-01")
        t("★★対象0件は正常（全部仕上がった場合がある）★★"
          "（2026-08-07。7機種を完成記事へ上げたら毎朝失敗を報告していた）",
          got is None and "★" not in why and "残っていません" in why)
    finally:
        globals()["STATE"], globals()["targets"] = keep_state, keep_targets
    # --- ★鍵★（2026-08-06・Codex135回目でOSに任せる方式へ）
    lkd = tempfile.mkdtemp()
    lk_target = os.path.join(lkd, "x.json")
    fh = _lock(lk_target)
    t("　鍵が取れる", fh is not None)
    took = False
    try:
        _lock(lk_target)                  # 同じプロセスからでも二重には取れない
    except Halt as e:
        took = "鍵が取れません" in str(e)
    t("★★鍵を持っている間は、ほかは取れない★★（時間が経っても同じ）", took)
    old = os.stat(lk_target + ".lock").st_mtime - 99999
    os.utime(lk_target + ".lock", (old, old))
    took2 = False
    try:
        _lock(lk_target)
    except Halt:
        took2 = True
    t("★★どれだけ古くても、持っている間は奪えない★★（置き去り判定を持たない）",
      took2)
    _unlock(fh)
    fh2 = _lock(lk_target)
    t("　外したあとは取れる", fh2 is not None)
    _unlock(fh2)
    t("★★鍵のファイルは消さない★★（消す/作るの間の競合をそもそも作らない）",
      os.path.exists(lk_target + ".lock"))
    import subprocess as _sp
    here = os.path.dirname(os.path.abspath(__file__))
    prog = chr(10).join([
        f"import sys; sys.path.insert(0, r'{here}')",
        "import grow_legacy as g",
        "try:",
        f"    g._lock(r'{lk_target}')",
        "    print('取れた')",
        "except g.Halt:",
        "    print('取れない')",
    ])
    fh3 = _lock(lk_target)
    out = _sp.run([sys.executable, "-c", prog], capture_output=True, text=True,
                  timeout=120, encoding="utf-8", errors="replace")
    t("★★別のプロセスからも取れない★★（本当に排他できているか確かめる）",
      "取れない" in (out.stdout or ""))
    _unlock(fh3)
    out2 = _sp.run([sys.executable, "-c", prog], capture_output=True, text=True,
                   timeout=120, encoding="utf-8", errors="replace")
    t("★★持ち主のプロセスが終われば、OSが必ず外す★★（置き去りにならない）",
      "取れた" in (out2.stdout or ""))

    # --- ★無人運転を最後まで通す★（2026-08-06・Codex133回目の指摘）
    keep = {k: globals()[k] for k in ("STATE", "targets", "dirty_files",
                                      "run", "_to_ledger", "_blocked")}
    tmpd2 = tempfile.mkdtemp()
    calls = {"ledger": [], "marked": []}

    class _A:                             # 引数の代わり
        slug = None
        apply = True
        today = "2026-08-06"

    try:
        globals()["STATE"] = os.path.join(tmpd2, "st.json")
        globals()["targets"] = lambda slug=None, strict=False: [{"slug": "a"}]
        globals()["dirty_files"] = lambda: []
        globals()["_blocked"] = lambda slug: []
        globals()["_to_ledger"] = lambda slug, pr, tr: calls["ledger"].append(
            (slug, tr))
        globals()["run"] = lambda slug, ap: {"problems": [], "wrote": True,
                                             "added": ["**型式名**：X"]}
        t("★★書けた時だけ別の終了コード（10）にする★★"
          "（手順書が『書けた日だけ検証・コミット』を判断できるように）",
          run_next(_A()) == EXIT_WROTE)
        globals()["run"] = lambda slug, ap: {
            "problems": ["★止めました★ 知らない話が混じっています"]}
        globals()["run"] = lambda slug, ap: {"problems": [], "wrote": False,
                                             "added": []}
        _A.today = "2026-08-10"      # 月曜（1周後は週1）
        t("　足すものが無い日は 0（検証・コミットへ進まない）",
          run_next(_A()) == EXIT_OK)
        globals()["run"] = lambda slug, ap: {
            "problems": ["★止めました★ 知らない話が混じっています"]}
        _A.today = "2026-08-17"      # 月曜
        rc = run_next(_A())
        t("★★人の判断が要る時は 3 で終わり、台帳へ載せる★★",
          rc == EXIT_ATTENTION and calls["ledger"] == [("a", False)])
        globals()["run"] = lambda slug, ap: {
            "problems": ["材料を集められません: ..."]}
        _A.today = "2026-08-24"      # 月曜
        rc2 = run_next(_A())
        t("★★一時的な不調も 3 で終わる（最初は台帳に積まない）★★",
          rc2 == EXIT_ATTENTION and len(calls["ledger"]) == 1)
        globals()["dirty_files"] = lambda: [" M scripts/x.py"]
        _A.today = "2026-08-25"      # 火曜でも「作業中」判定が先
        t("★★作業中の変更があれば何もせず 0★★", run_next(_A()) == EXIT_OK)
        globals()["dirty_files"] = lambda: []
        def _boom(slug, pr, tr):
            raise RuntimeError("台帳が書けない")
        globals()["_to_ledger"] = _boom
        globals()["run"] = lambda slug, ap: {"problems": ["★止めました★ だめ"]}
        _A.today = "2026-08-31"      # 月曜
        rc3 = run_next(_A())
        st3 = json.load(open(globals()["STATE"], encoding="utf-8"))
        t("★★台帳に載せられなければ 1 で終わり、その日を消費しない★★"
          "（やり直せる）",
          rc3 == EXIT_FATAL and st3.get("last_run") != "2026-08-31")
        globals()["_blocked"] = lambda slug: ["台帳を読めません: こわれています"]
        _A.today = "2026-08-31"      # 月曜（上は日を消費していない）
        t("★★台帳が読めないのは、その機種の問題ではない（すぐ 1）★★",
          run_next(_A()) == EXIT_FATAL)
        globals()["_blocked"] = lambda slug: []      # 台帳は読める状態に戻す
        globals()["_to_ledger"] = lambda slug, pr, tr: None
        globals()["run"] = lambda slug, ap: {"problems": [], "wrote": True,
                                             "added": ["**型式名**：X"]}
        keep_mark = globals()["_mark_checked"]

        def _mark_boom(slug, today, outcome):
            raise RuntimeError("記録できない")
        globals()["_mark_checked"] = _mark_boom
        _A.today = "2026-09-07"          # 月曜
        rc4 = run_next(_A())
        globals()["_mark_checked"] = keep_mark
        t("★★書いた後に記録できなくても『失敗』にしない★★"
          "（記事は変わっているので検証・コミットへ進ませる）",
          rc4 == EXIT_WROTE_UNRECORDED)
        globals()["run"] = lambda slug, ap: {"problems": ["★止めました★ だめ"]}
        globals()["_mark_checked"] = _mark_boom
        _A.today = "2026-09-14"          # 月曜
        rc5 = run_next(_A())
        globals()["_mark_checked"] = keep_mark
        t("　書いていない時の記録失敗は、これまでどおり 1",
          rc5 == EXIT_FATAL)
        _A.apply = False
        t("★★--apply が無ければ何もしない★★", run_next(_A()) == EXIT_FATAL)
        _A.apply, _A.slug = True, "x"
        t("　--slug とは併用できない", run_next(_A()) == EXIT_FATAL)
        _A.slug, _A.today = None, "8月6日"
        t("★★日付の形がおかしければ動かない★★", run_next(_A()) == EXIT_FATAL)
    finally:
        for k, v in keep.items():
            globals()[k] = v
    t("★★一時的な不調と、人の判断が要るものを分ける★★",
      is_transient(["材料を集められません: ..."])
      and not is_transient(["★止めました★ 知らない話が混じっています: ..."]))

    # --- ★別機種の記事には書かない★（Codex125 #9）
    halted10 = False
    try:
        plan(MACH, MAT, {"slug": "y", "name": "機種Y", "sections": []})
    except Halt as e:
        halted10 = "名簿と合いません" in str(e)
    t("★★記事の中身が別機種なら書かない★★", halted10)

    print(f"\n{ran[0]}/{ran[0]} 合格" if ok else "\n不合格あり")
    return 0 if ok else 1


def dirty_files() -> list:
    """まだコミットしていない変更（★人の作業の上に書かない★）。

    2026-08-06・Codex132回目 #3。新台のレーンが同じ日に書いた直後だと、
    1回の実行で2機種ぶんの公開内容が変わり、切り分けが難しくなる。
    対話セッションの書きかけの上に重ねるのも避けたい。
    """
    import subprocess
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=BASE,
                             capture_output=True, text=True, timeout=60)
    except Exception as e:                # noqa: BLE001
        raise Halt(f"作業中の変更を確かめられません: {e}")
    if out.returncode != 0:
        raise Halt(f"作業中の変更を確かめられません（git が {out.returncode}）")
    # ★新しく作られたファイルも「作業中」に数える★（2026-08-06・Codex133回目 #4）
    #   ずっと置いておく生成物は .gitignore で管理する（個別に除外しない）
    return [x for x in out.stdout.splitlines() if x.strip()]


def run_next(a) -> int:
    """★無人運転★（1回1機種・詰まったら台帳・失敗は隠さない）

    終了コード（★手順書と揃える★）
      0 = 何も変えていない（足すものなし／動かない日／作業中で飛ばした）
     10 = ★書いた★（この時だけ検証・コミット・push へ進む）
     11 = 書いたが、見た日を記録できなかった（★10と同じく検証・コミットへ★）
      3 = このレーンだけ終わり（本編は続けてよい）
          ・人の判断が要る → **台帳に登録済み**
          ・一時的な不調   → 続いた回数が上限に届くまでは**登録しない**
      1 = 予期しない失敗（記録が読めない・保存できない・台帳が読めない）
    """
    if a.slug:
        print("★--next と --slug は同時に使えません★")
        return EXIT_FATAL
    if not a.apply:
        # ★下見のまま日を消費させない★（Codex132回目 #4）
        print("★--next には --apply が要ります★（下見は --slug で行います）")
        return EXIT_FATAL
    lock = None
    try:
        today = _valid_date(a.today or datetime.date.today().isoformat())
        # ★選ぶところから記録するところまでを1つの鍵で守る★
        #   （2026-08-06・Codex133回目 #5。2つ動くと同じ機種を選び、
        #     回数や連続失敗の記録がどちらが残るか分からなくなる）
        lock = _lock(STATE)
        dirty = dirty_files()
        if dirty:
            print("作業中の変更があるので今日は動きません（先にコミットしてください）")
            for x in dirty[:5]:
                print("  ", x)
            print("結果: skipped")
            return EXIT_OK                # ★異常ではない★
        slug, why = pick_next(today)
        if not slug:
            print(why)
            print("結果: fatal" if "★" in why else "結果: skipped")
            return EXIT_FATAL if "★" in why else EXIT_OK
        print(f"今日見る機種: {slug}")
        # ★台帳が読めないのは、その機種の問題ではない★（すぐ失敗にする）
        stop = _blocked(slug)
        if any("台帳を読めません" in str(x) for x in stop):
            print("★" + " / ".join(str(x) for x in stop) + "★")
            return EXIT_FATAL
        r = {"problems": stop} if stop else run(slug, True)
        problems = r.get("problems") or []
        transient = bool(problems) and is_transient(problems)
        outcome = ("transient" if transient else "halt") if problems else \
                  ("wrote" if r.get("wrote") else "none")
        for x in problems:
            print("  -", x)
        if problems:
            # ★台帳を先に、記録を後に★（2026-08-06・Codex133回目 #3）
            #   逆にすると、台帳登録に失敗した時「今日はもう見た」だけが残り、
            #   同じ日にやり直しても何もせず終わる。
            streak = _peek_streak(slug) + (1 if transient else 0)
            if not transient or streak >= _TRANSIENT_LIMIT:
                _to_ledger(slug, problems, transient)
            else:
                print(f"  （一時的な不調 {streak}/{_TRANSIENT_LIMIT} 回目・様子を見ます）")
            _mark_checked(slug, today, outcome)   # ★止まっても日は進める★
            print(f"結果: {outcome}")
            return EXIT_ATTENTION
        wrote = bool(r.get("wrote"))
        try:
            _mark_checked(slug, today, outcome)
        except Exception as e:            # noqa: BLE001
            if not wrote:
                raise
            # ★書いた後の記録失敗で「失敗」にしない★（Codex136回目）
            #   記事は変わっているので、検証とコミットまでは進めてもらう。
            print(f"★見た日を記録できませんでした: {type(e).__name__}: {e}★")
            for x in (r.get("added") or []) + (r.get("boxes") or []):
                print("  ＋", str(x)[:74])
            print("書きました（記録だけ失敗）")
            print("結果: wrote_unrecorded")
            return EXIT_WROTE_UNRECORDED
        for x in (r.get("added") or []) + (r.get("boxes") or []):
            print("  ＋", str(x)[:74])
        for x in (r.get("removed") or []):
            print("  －", str(x)[:70])
        print("書きました" if wrote else "足すものがありません")
        print("結果: wrote" if wrote else "結果: noop")
        return EXIT_WROTE if wrote else EXIT_OK
    except Halt as e:
        print(f"★止めました★ {e}")
        print("結果: fatal")
        return EXIT_FATAL
    except Exception as e:                # noqa: BLE001
        print(f"★思わぬ失敗★ {type(e).__name__}: {e}")
        print("結果: fatal")
        return EXIT_FATAL
    finally:
        _unlock(lock)                     # ★自分の鍵だけ外す★


def main() -> int:
    ap = argparse.ArgumentParser(description="旧方式の先行記事に材料を足す")
    ap.add_argument("--slug")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--next", action="store_true",
                    help="無人運転: いちばん長く見ていない1機種を見る")
    ap.add_argument("--today", help="無人運転で使う日付（既定は今日）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    slug = a.slug
    if a.next:
        return run_next(a)
    if not slug:
        print("対象:", " ".join(m["slug"] for m in targets()))
        return 0
    r = run(slug, a.apply)
    for p in r.get("problems") or []:
        print("  -", p)
    for x in r.get("added") or []:
        print("  ＋", x)
    for x in r.get("removed") or []:
        print("  －", str(x)[:70])
    for x in r.get("boxes") or []:
        print("  ◇", x)
    for b, x in r.get("rewrote") or []:
        print("  ＊", str(b)[:34], "→", str(x)[:34])
    if r.get("wrote"):
        print("書きました")
    elif not r.get("problems"):
        print("（下見です。--apply で書きます）"
              if (r.get("added") or r.get("removed") or r.get("rewrote")
                  or r.get("boxes"))
              else "足すものがありません")
    return 1 if r.get("problems") else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except _sj.SafeJsonError as e:
        print(f"★入力データが読めません: {e}★")
        raise SystemExit(1)

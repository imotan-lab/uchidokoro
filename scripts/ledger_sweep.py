# -*- coding: utf-8 -*-
"""担当した機種の台帳を、その場で読み直して、直っていれば機械が確かめて閉じる。

★★運営者の指示（2026-08-30）★★
> 台帳って以前にあったタスクがどんどん詰んでいったものだよね
> 今ってそのタスクないんだけど2AIでそのへんのおかしいところを
> 見つけて更新する予定だったんだけど無理なの？
→ 「入れよう」

★何が起きていたか★＝毎朝のタスクは担当した機種の**記事だけ**を読み、
その機種について過去に書き留めたメモ（台帳）を**見ていなかった**。
運営者が「台帳を順番決めに使うな」と言ったのを受けて順番から外したとき、
★参照そのものもやめてしまった★。

  結果1＝直したのにメモが開いたまま残る
    （2026-08-30に実測。東京喰種の #284 は朝に直したのに開いたままだった）
  結果2＝記事を読むだけでは気づけない指摘が、メモにしか無いまま眠る
    （実測：58機種に68件が、いまも記事に残ったまま）

★★この道具がやること★★
  ①その機種の開いている案件を出し、「当てられそうな検査」を**参考として**添える
    → ★順番は決めない★（運営者の決めた 新台→人気→その他 は変えない）
  ②2AIが記事を読んで「この検査が全部通れば直っている」と決める
  ③機械がその検査を**全部**やり直し、通ったときだけ閉じる

★★語の名簿で自動的に閉じるのはやめた★★（2026-08-30・Codexの指摘1）
  はじめ「案件の題に出てくる語 → 当てる検査」を作り、当たったものを
  そのまま閉じていた。★これは例外リストで意味を判定する型★で、
  実際に誤って閉じる道が4つあった。

    ・「常体」→ plain_style_gone は、★検査自身が「これだけを根拠に
      文体混在を閉じてはいけない」と書いている★（19通りの文末しか見ない）
    ・「噂の箱が空」→ rumor_not_declared_empty は空箱を見ておらず、
      「噂はありません」という定型文を探す検査。★中身が違う★
    ・「ポチポチくん」を含む案件が全部「リンクが開けるか」になる
      （表示や設定値の誤りでも、リンクさえ開ければ閉じる）
    ・語が2つ当たっても最初の1つしか返さないので、
      「他サイト名＋型式名」の案件は片方だけ直せば閉じられる

  → 名簿は★参考の表示だけ★にした。閉じる検査を決めるのは2AI。

★★「問題の文が消えた＝直った」とはしない★★（2026-08-30に実測）
  東京喰種の #155 は「CZまたはAT当選」という文が消えていたが、
  それは直ったのではなく★恩恵が未確定で載せていない★からだった。
"""
from __future__ import annotations
import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_S = os.path.join(BASE, "scripts")
for _p in (BASE, _S):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import local_paths as _lp                                # noqa: E402
import recheck as _rc                                    # noqa: E402
import safe_json as _sj                                  # noqa: E402
# ★受領証の版は台帳の側に1つだけ置く★（同じ規則を2か所に書かない・罠③）
#   ここで文字列を書き直すと、片方だけ上げたときに黙って食い違う。
import open_issues as _oi_mod                            # noqa: E402
from open_issues import RECEIPT_SCHEMA                    # noqa: E402

LEDGER = _lp.doc("open_issues.json")

# ★見せた日の控えの置き場★（2026-08-30・Codexの指摘4）
#   ★共有の state.json とは別に持つ★＝共有ファイルを丸ごと書き戻すと、
#   その間に別の処理が入れた更新を、古い内容で上書きしてしまう。
#   `grow_machine.py` が同じ理由で既に分けている。
SITE_STATE_NAME = "ledger_site_state.json"

# ★題や詳細に出てくる言葉から「当てられそうな検査」を挙げる★
#   ★★これは参考の表示だけ。閉じる判断には使わない★★（2026-08-30・Codex指摘1）
#     語の一致は意味の一致ではない。決めるのは記事を読んだ2AI。
#   ★当たった検査は全部挙げる★（最初の1つで打ち切らない）
SUGGEST_CHECK = (
    (("他サイト名", "サイト名の露出", "競合サイト"), "competitor_names_gone"),
    (("型式名", "検定番号"), "model_code_gone"),
    (("常体", "文体混在", "だ・である"), "plain_style_gone"),
    (("設定示唆まとめが空", "設定示唆が空", "settei が空"), "settei_filled"),
    (("噂はありません", "噂・未確定情報はありません"),
     "rumor_not_declared_empty"),
    (("交換率別しきい値が逆転", "交換率が良いほど深い", "しきい値が逆転"),
     "rate_monotonic"),
    (("ポチポチくんへ行けない", "ポチポチくんのリンク"),
     "pochipochi_reachable"),
    (("同じ判断を2度", "重複行", "同一事実の重複"), "duplicate_prose_gone"),
)

# ★★これだけを根拠に閉じてはいけない検査★★
#   `recheck.check_plain_style_gone` の説明にそう書いてある＝
#   見ているのは19通りの文末だけで、「常体が無い」ことの証明ではない。
#   ★同じ規則を2か所に書かない★ので、ここでは「単独では通さない」だけを持つ。
NEED_COMPANION = ("plain_style_gone",)


def suggest_checks(issue: dict) -> list:
    """その案件に当てられそうな検査を**全部**挙げる（参考）。"""
    text = (str(issue.get("title") or "") + " "
            + str(issue.get("detail") or ""))
    out = []
    for words, check in SUGGEST_CHECK:
        if any(w in text for w in words) and check not in out:
            out.append(check)
    return out


def _head() -> str:
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=BASE,
                       capture_output=True, text=True)
    return (r.stdout or "").strip()


def _dirty() -> bool:
    """★未コミットの変更があるうちは閉じない★（既存の決まりと同じ）"""
    r = subprocess.run(["git", "status", "--porcelain"], cwd=BASE,
                       capture_output=True, text=True)
    return bool((r.stdout or "").strip())


def _rows() -> list:
    """台帳の案件を読む。

    ★台帳が無い場所でも落ちない★（2026-08-30・実際にCIを赤くした）
      台帳は書類フォルダ（リポジトリの外）にあり、★CIの機械には無い★。
      無いときは「案件0件」として読む。
    ★これは緩めではない★＝閉じる側は `find_issue` が None を返すので、
      案件が引けなければ**断る**（fail-closed のまま）。
    """
    data = _sj.read_json(LEDGER, expect=(dict, list),
                         allow_missing=True, default=[])
    if isinstance(data, list):
        return data
    return (data.get("issues") or []) if isinstance(data, dict) else []


def find_issue(issue_id: int):
    """番号で案件を引く（無ければ None）。★閉じる前に必ず引く★"""
    for r in _rows():
        try:
            if int(r.get("id")) == int(issue_id):
                return r
        except (TypeError, ValueError):
            continue
    return None


def for_slug(slug: str) -> dict:
    """その機種の開いている案件を出す。★書かない★

    ★閉じてよいかは決めない★＝当てられそうな検査と、その今の結果を
    添えるだけ。決めるのは記事を読んだ2AI。
    """
    mine = [r for r in _rows()
            if r.get("slug") == slug and r.get("status") != "closed"]
    out = {"slug": slug, "open": [], "checked": len(mine)}
    head = _head()
    for r in mine:
        row = {"id": r.get("id"),
               "title": str(r.get("title") or "")[:160],
               "detail": str(r.get("detail") or "")[:600],
               "suggest": []}
        for check in suggest_checks(r):
            meta = _rc.CHECKS.get(check) or {}
            ok, why, _got = _rc.closeable(
                {"check": check, "version": meta.get("version"),
                 "args": {"slug": slug}, "expected_commit": head})
            row["suggest"].append({"check": check, "pass": bool(ok),
                                   "why": str(why)[:120]})
        out["open"].append(row)
    return out


# ★★機種に紐づかない案件だけを出す道（--site）は廃止した★★（2026-09-17）
#   ★理由★＝出すだけで**閉じる手順が繋がっていなかった**（読んで終わり）。
#   ＝運営者の指示「私の手を使わずとも閉じれるように」を満たさない。
#   `--due` が同じ回転（見せた日の古い順）で**全部**を対象に出し、
#   その場で閉じるところまで繋がっているので、こちらへ一本化した。
#   ★見せた日の控えはそのまま引き継ぐ★＝回転を最初へ戻さないため。
#   ★2つ残さない★（鉄則0b）＝同じ日に両方動くと、
#   後から動いたほうが「今日もう出した2件」に張り付いて先へ進めなかった
#   （実際にそうなった）。


def for_due(limit: int = 10, today: str = "") -> list:
    """★機種の段階によらず、開いている案件を順に出す★（2026-09-17）

    ★★なぜ要るか★★（運営者の指示）
      ＞ 私の手を使わずとも閉じれるような仕組みにしてくれないと
      ＞ いつまで経っても自動化出来ないじゃん
      ★直す前★＝閉じる工程に届くのは「朝のタスクが担当した機種」だけで、
      担当できるのは**公開済みの記事だけ**だった（早すぎる公開を止める線）。
      ＝★まだ公開していない機種の案件は、誰にも閉じられない★。
      しかもその案件がその機種を止めるので、★永久に解けない★。
      実例＝L転生王女。値は先月そろっていたのに1件残って止まっていた。
      ★記事を1文字も書かないので、公開を止める線はそのまま★
      （閉じることと、記事を書き換えることを切り離した）。

    ★★並び順★★（Codexと詰めた）
      ①まだ一度も出していないもの ②最後に出した日が古い順
      ③重要度は**同じ日のときの決着だけ**に使う ④初出が古い順 ⑤番号順
      ★重要度を先頭に置かない★＝軽いものが永久に回ってこない。
      ＝「入口は重いものを積み、出口は重いものしか拾わない」の再来になる。
    """
    seen = _seen_map()
    sev_rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}

    def _num(r):
        try:
            return int(r.get("id"))
        except (TypeError, ValueError):
            return 10 ** 9

    mine = [r for r in _rows() if r.get("status") != "closed"]
    mine.sort(key=lambda r: (
        seen.get(str(r.get("id")), ""),                 # 未提示が先（空文字）
        sev_rank.get(str(r.get("severity") or ""), 9),  # 同日なら重い順
        str(r.get("first_seen") or ""),
        _num(r)))
    limit = max(0, int(limit))
    if not today:
        return mine[:limit]
    # ★★今日もう出したものは、もう一度同じものを出す★★
    #   （番人が朝に再試行しても、読まれないまま飛ばされる案件を作らない）
    # ★★足りなければ、その先を足す★★（2026-09-17・実際に詰まった）
    #   ★直す前は「今日出した分」だけを返して打ち切っていた★ので、
    #   前の工程が2件出していると、10件見るつもりの回が
    #   ★その2件に張り付いて先へ進めなかった★。
    already = [r for r in mine if seen.get(str(r.get("id"))) == today]
    rest = [r for r in mine if seen.get(str(r.get("id"))) != today]
    return (already + rest)[:limit]


def _state_path() -> str:
    """★見せた日の控えの置き場★

    ★共有の state.json とは別に持つ★（2026-08-30・Codexの指摘4）＝
      共有ファイルを丸ごと読んで丸ごと書き戻すと、
      ★その間に別の処理が入れた更新を、古い内容で上書きしてしまう★。
      `grow_machine.py` が同じ理由で既に分けているので、それにそろえる。
    """
    return _lp.doc(SITE_STATE_NAME)


def _seen_map() -> dict:
    """どの案件を、いつ材料として出したか。"""
    st = _sj.read_json(_state_path(), expect=dict,
                       allow_missing=True, default={})
    got = ((st or {}).get("ledger_site") or {}).get("last_shown") or {}
    return {str(k): str(v) for k, v in got.items()} if isinstance(got, dict) \
        else {}


def mark_shown(ids, today: str) -> None:
    """出した案件に日付を付ける（次は後ろへ回る）。"""
    p = _state_path()
    st = _sj.read_json(p, expect=dict, allow_missing=True, default={})
    st = st if isinstance(st, dict) else {}
    box = st.setdefault("ledger_site", {}).setdefault("last_shown", {})
    for i in ids:
        box[str(i)] = today
    tmp = p + ".sweep.tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(st, ensure_ascii=False, indent=1))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def _html_ready(slug: str) -> bool:
    """★公開HTMLが実在するか★（2026-08-30・Codexの指摘4）

    `text_gone` は公開HTMLが**無い**とき「HTMLにも無い」と読んでPASSする。
    ＝「記事データと公開HTMLを見た」という閉じ方の理由と食い違う。
    ★無いなら閉じない側へ倒す★
    """
    return os.path.exists(os.path.join(BASE, "machines", slug, "index.html"))


def row_conditions(row) -> list:
    """★その案件に登録されている「閉じる条件」を全部★（読み方は台帳側に1つ）"""
    return _oi_mod.row_conditions(row or {})


def checks_bound_to_issue(row, checks, texts, guards) -> tuple:
    """★その案件に、閉じる条件が登録されているか★ → (ok, 理由)

    ★★何が起きていたか★★（2026-09-17・Codexの指摘・どちらも再現した）
      ①★同じ機種で通る、案件と無関係な検査を1つ挙げるだけで閉じられた★
        （ヤメ時の書き方の案件を、型式名の検査で閉じられた）。
      ②★案件の説明文そのものを「消えた逐語」にできた★
        （「ヤメ時の説明が読みづらい」という説明文は**もともと記事に無い**ので、
          `text_gone` が必ず通る＝1文字も直さずに閉じられた）。

    ★★だから結び付けは「登録」の1本だけにした★★
      ＝逐語も壊し方も、条件として**登録**してから使う。
      登録の時点で「いまはまだ通らない」ことを機械が確かめるので、
      あとで通ったことが**変化の証拠**になる（②はここで落ちる）。
    ★登録した条件は全部通す★（①はここで落ちる）。
    """
    if row_conditions(row):
        # ★封（これで案件の全部を覆ったという宣言）も要る★
        #   ＝条件が1件あるだけでは、案件に書かれた問題を
        #   全部登録したことにならない（2026-09-17・Codexの指摘）。
        #   ★中身の照合は台帳側に1つ★（`_condition_binds_row`）。
        _seal = (row or {}).get("conditions_sealed")
        if not isinstance(_seal, dict):
            return False, ("この案件には「これで全部を覆った」という封が"
                           "ありません。python scripts/open_issues.py seal "
                           "--id <番号> --why-file <理由> --by claude,codex")
        return True, "案件に登録した条件と封で結び付いています"
    return False, ("この案件には、閉じる条件が1件も登録されていません。"
                   "検査名や逐語だけでは、その案件が直った証拠になりません"
                   "（同じ機種で通る無関係な検査でも、"
                   "案件の説明文そのものを逐語にしても閉じられてしまうため）。"
                   "python scripts/open_issues.py condition --id <番号> "
                   "--check <検査名> … で先に登録してください")


def conditions_for_row(slug: str, row, head: str = "") -> list:
    """★案件に登録した条件を、そのまま動かせる形にする★（2026-09-17）

    ★機種は必ず案件の行から固定する★＝登録の側に別の機種が書いてあっても、
      その機種の記事や控えで通してしまわない。
    ★版も登録したものをそのまま入れる★＝検査が変わったら
      `recheck.closeable` が版の食い違いで断るので、必ず登録し直すことになる。
    ★切り出してある理由★＝木が汚れていても、
      「何をどう組み立てたか」だけを試験で直接見られるようにするため
      （合否は `closeable` が未コミットを見るので、手元では当てにできない）。
    """
    out = []
    for want in row_conditions(row):
        _c = str(want.get("check") or "")
        # ★機種は行から固定★（★ただし機種を取る検査だけ★＝規則は台帳側に1つ）
        a = _oi_mod.pin_slug(_c, want.get("args") or {}, slug)
        out.append({"check": _c, "version": want.get("version"), "args": a,
                    "expected_commit": head or _head()})
    return out


def due_condition_lines(row) -> list:
    """★閉じる回で、その案件について何を知らせるか★（2026-09-17）

    ★★切り出してある理由★★＝画面へ印字する処理の中に埋めていると、
      ★そこを壊しても試験が緑のまま★になる（罠③）。

    ★★壊れた一覧を「無い」「少ない」と見せない★★（Codexの指摘）＝
      `row_conditions` は辞書でない要素を落として読むので、
      ★一覧でなければ「未登録」、混ざっていれば「その分だけ」に見えた★。
      その案内どおり登録しても、壊れた要素が残るので結局閉じられない。
    ★詰まりを知らせたら、その場で直し方まで言う★
      （言わないと、案内どおりに登録し直して同じ輪に戻る）。
    """
    out = []
    ngb = _oi_mod.conditions_broken(row or {})
    if ngb:
        out.append("★" + ngb + "★")
    cs = row_conditions(row)
    if not cs and not ngb:
        out.append("★閉じる条件は未登録★（登録しないと閉じられません）")
    stale = False
    for cond in cs:
        out.append(f"★登録ずみの閉じる条件★ {cond.get('check')} "
                   f"{cond.get('args')}（{cond.get('why')}）")
        for w in condition_stale(cond):
            out.append("★" + w + "★")
            stale = True
    if stale:
        out.append("★この案件は、登録し直すだけでは直りません★")
    return out


def stale_conditions(row) -> list:
    """★登録した条件のうち、いま使えないもの★（理由の文の一覧）"""
    out = []
    for c in row_conditions(row):
        out += condition_stale(c)
    return out


def condition_stale(cond) -> list:
    """★登録した条件が、いまも使えるか★ → 使えない理由（無ければ空）

    ★★なぜ要るか★★（2026-09-17）＝登録した条件は**静かに古くなる**。
      検査の版が上がると `recheck.closeable` が版の食い違いで断るので、
      ★その案件だけが、理由の分からないまま閉じられなくなる★。
      名簿から検査が消えたときも同じ。
      ＝閉じる回で案件を出すときに、その場で言う。
    ★ここでは直さない★（勝手に版を上げると「確かめた」の中身が変わる）。
    """
    if not isinstance(cond, dict):
        return []
    name = str(cond.get("check") or "")
    meta = _rc.CHECKS.get(name)
    if meta is None:
        return [f"登録した検査（{name}）は、いまの名簿にありません。"
                + _oi_mod.REPAIR_STEPS]
    if not meta.get("closeable"):
        return [f"登録した検査（{name}）は、いまは観測どまりです。"
                + _oi_mod.REPAIR_STEPS]
    if cond.get("version") != meta.get("version"):
        return [f"登録した検査（{name}）の版が変わりました"
                f"（条件 {cond.get('version')} / いま {meta.get('version')}）。"
                "中身を読み直したうえで、" + _oi_mod.REPAIR_STEPS]
    return []


def run_checks(slug: str, checks, texts, head: str = "",
               guards=None, row=None) -> tuple:
    """2AIが名指しした検査を**全部**やり直す → (ok, 一件ずつの記録, 受領証の中身)

    ★★1件でも通らなければ閉じない★★（罠⑮＝免除の条件をゆるくしない）
      1つの案件に問題が2つ書いてあることがある（実例 #284＝
      「狙い目の逆転」と「句点後の半角スペース」）。
      片方だけ確かめて閉じると、もう片方が直っていないまま消える。
    ★検査を1つも渡されなければ通さない★（空で閉じない）

    ★3つ目に返すもの＝受領証に載せる「何を・どう確かめたか」★（2026-09-17）
      台帳を書き換える側（open_issues）が、これを**もう一度やり直して**から
      閉じる。＝ここを素通りして閉じる道を無くすため。
    """
    checks = list(checks or [])
    texts = list(texts or [])
    guards = list(guards or [])
    if not checks and not texts and not guards and not row_conditions(row):
        return False, ["確かめる検査が1件もありません"], []

    # ★これだけでは閉じられない検査★は、逐語の確認と組でなければ通さない
    lone = [c for c in checks if c in NEED_COMPANION]
    if lone and not texts:
        return False, [f"{'/'.join(lone)} は単独では閉じられません"
                       "（消えた逐語も一緒に確かめてください）"], []

    head = head or _head()
    whys = []
    done = []

    def _one(cond, label):
        ok, why, got = _rc.closeable(cond)
        whys.append(f"{'○' if ok else '×'} {label} ／ {why}")
        if ok:
            done.append({"condition": cond,
                         "observation_digest":
                             str((got or {}).get("observation_digest") or "")})
        return ok

    # ★★案件に登録した条件は、呼び出し側が何を渡しても必ず全部やり直す★★
    #   （2026-09-17・Codexの指摘①・再現済み）
    #   ★直す前は「渡された検査を全部やる」だけだった★ので、
    #   「必要な検査を全部渡したか」は誰も見ていなかった
    #   ＝1つの案件に問題が2つ書いてあるとき、
    #   ★片方を登録して片方の検査だけ渡せば閉じられた★。
    for want, cond in zip(row_conditions(row),
                          conditions_for_row(slug, row, head)):
        if not _one(cond, f"登録した条件[{want.get('check')}]"):
            return False, whys, done

    for check in checks:
        meta = _rc.CHECKS.get(check)
        if not meta:
            return False, whys + [f"知らない検査です: {check}"], done
        if not meta.get("closeable"):
            return False, whys + [f"観測どまりの検査です: {check}"], done
        if any(str(w.get("check") or "") == check for w in row_conditions(row)):
            continue                 # ★登録ぶんはもう上でやり直している★
        if not _one({"check": check, "version": meta.get("version"),
                     "args": {"slug": slug},
                     "expected_commit": head}, check):
            return False, whys, done
    if texts and not _html_ready(slug):
        return False, whys + ["公開HTMLがありません"
                              "（記事データだけでは閉じません）"], done
    meta = _rc.CHECKS["text_gone"]
    for t in texts:
        if not _one({"check": "text_gone", "version": meta["version"],
                     "args": {"slug": slug, "text": t},
                     "expected_commit": head}, f"text_gone[{t[:30]}]"):
            return False, whys, done
    # ★★機械の中身を直したときの道★★（2026-09-08・台帳#581）
    #   ★記事の文章を見る検査は当てはまらない★ので、
    #   「その直しを1行壊すと試験が赤くなるか」で確かめる。
    _gmeta = _rc.CHECKS["guard_proven"]
    for g in guards:
        if not _one({"check": "guard_proven", "version": _gmeta["version"],
                     "args": {"mutation_why": g},
                     "expected_commit": head}, f"guard_proven[{g[:40]}]"):
            return False, whys, done
    return True, whys, done


def precheck_close(issue_id, slug: str) -> tuple:
    """★番号・状態・機種が結び付いているか★ → (ok, 理由)

    ★★close_issue から切り出してある★★（2026-08-30）
      理由＝ここを本体の中に埋めていたら、
      ★「木が汚れている」という別の守りに先に当たって★、
      壊し方の道具が4件とも「捕まえられない」になった（罠④）。
      切り出して直接呼べる形にすると、狙った1件だけを試せる。
    """
    row = find_issue(issue_id)
    if row is None:
        return False, f"#{issue_id} という案件がありません"
    if str(row.get("status") or "") == "closed":
        return False, f"#{issue_id} はすでに閉じています"
    if str(row.get("slug") or "") != slug:
        return False, (f"#{issue_id} の機種は {row.get('slug')!r} で、"
                       f"指定の {slug!r} と違います")
    return True, f"#{issue_id} は {slug} の開いている案件です"


def texts_from_issue(row, texts) -> tuple:
    """★逐語はその案件の本文から出ていること★ → (ok, 理由)

    （2026-08-30・Codexの指摘2の3点目）
    ★何が起きるか★＝機種が合っていても、案件と無関係な
    「記事に無い文字列」を渡せば text_gone は必ず通る。
    ＝東京喰種の #155 を、でたらめな文字列で閉じられた。
    ★見るのは案件の題と詳細だけ★（意味は判定しない＝そこに書いてあるか）。
    """
    texts = list(texts or [])
    if not texts:
        return True, "逐語の指定はありません"
    body = (str((row or {}).get("title") or "") + "\n"
            + str((row or {}).get("detail") or ""))
    bad = [t for t in texts if t not in body]
    if bad:
        return False, ("案件に書かれていない逐語です: "
                       + " / ".join(t[:40] for t in bad))
    return True, f"逐語 {len(texts)} 件はすべて案件の本文にあります"


# ★★逐語が消えただけでは閉じられない型★★（2026-08-30・実際にやらかした）
#   台帳の kind は「external_value = 外部数値の疑義（裏取り待ち）」を持つ。
#   ★この型は「値が確かめられていない」ことが中身★なので、
#   文が記事から消えたのは「直った」ではなく「載せるのをやめた」かもしれない。
#   ＝ text_gone は直った証拠にならない。ほかの検査を必ず組にする。
TEXT_GONE_NOT_ENOUGH = ("external_value",)


def kind_allows(row, checks, texts, guards=None) -> tuple:
    """★その案件の型で、この検査の組み合わせで閉じてよいか★ → (ok, 理由)

    ★機械の中身の直し（guards）も検査のうち★（2026-09-08・台帳#581）＝
      数えないと「逐語だけで閉じようとしている」と誤って断られる。
    """
    kind = str((row or {}).get("kind") or "")
    # ★★裏取り待ちの型は、壊し方では通さない★★（2026-09-08・Codexの指摘2）
    #   ★直す前は `guards` を「検査のうち」に数えていた★ので、
    #   `external_value`（載せるのをやめただけかもしれない型）を
    #   ★機械の中身の壊し方1つで通せた★。
    #   機械の中身が直っていることは、その値の裏取りが済んだ証明にならない。
    if kind in TEXT_GONE_NOT_ENOUGH and guards and not checks:
        return False, (f"{kind} は裏取り待ちの型です。"
                       "機械の中身を直したこと（guard_proven）は、"
                       "その値の裏取りが済んだ証明になりません"
                       "（ほかの検査と組にしてください）")
    if kind in TEXT_GONE_NOT_ENOUGH and texts and not checks:
        return False, (f"{kind} は裏取り待ちの型です。"
                       "文が消えたのは「直った」ではなく"
                       "「載せるのをやめた」かもしれません"
                       "（ほかの検査と組にしてください）")
    return True, f"型 {kind or '(なし)'} でこの組み合わせは使えます"


def _guard_declares_issue(name: str, issue_id) -> bool:
    """★その守りが「この案件を証明する」と自分で名乗っているか★

    （2026-09-08・Codexの指摘で、場所での推測をやめた）
    ★なぜ要るか★＝守りは、案件を書いたあとで作られることがある。
    名前だけを求めると★守りより前の案件は永久に閉じられない★
    （実例＝#497。直し方まで合意して、その通りに直っているのに閉じられず、
      その機種だけが止まり続けていた）。

    ★なぜ「場所」ではなく「番号」か★＝場所（ファイル名＋壊す行）で
    推し量ると、**同じ行を壊す別の守り**でも通ってしまう（実在した）。
    ＝「その守りが効く」ことは示せても
      「その守りがこの案件の直しを証明する」ことにはならない。
    番号なら偶然当たることがない。

    ★守りの側の書き方★＝`MUTATIONS` の項目に `"issues": [497]` を書く。
    """
    # ★番号は正の整数だけ★（2026-09-08・Codexの指摘）
    #   `int()` に通すと 497.9 も True も番号として通ってしまう。
    if not isinstance(issue_id, int) or isinstance(issue_id, bool):
        if not (isinstance(issue_id, str) and issue_id.strip().isdigit()):
            return False
        issue_id = int(issue_id)
    n = int(issue_id)
    if n <= 0:
        return False
    try:
        import mutation_check as _mc0
    except Exception:                                        # noqa: BLE001
        return False
    hit = [m for m in getattr(_mc0, "MUTATIONS", [])
           if str(m.get("why") or "") == str(name)]
    if len(hit) != 1:
        return False                       # 名前で1つに決まらないなら通さない
    ids = hit[0].get("issues") or []
    # ★名乗る側も正の整数だけ★（書き間違いを機械で減らす）
    if not isinstance(ids, (list, tuple)) or not ids:
        return False
    for x in ids:
        if not isinstance(x, int) or isinstance(x, bool) or x <= 0:
            return False
    return n in list(ids)


def guards_from_issue(row, guards) -> tuple:
    """★壊し方の名前は、その案件の本文に書いてあること★ → (ok, 理由)

    （2026-09-08・Codexの指摘2）
    ★何が起きるか★＝直す前は `guards` が案件と何も結び付いていなかった。
    ＝★合格することが分かっている壊し方の名前を1つ渡すだけで、
    機械の中身と何の関係もない案件まで閉じられた★。
    しかも `external_value`（裏取り待ち＝逐語だけでは閉じない型）の
    関所まで、guard を1つ足すだけで通れた。
    ★見るのは案件の題と詳細だけ★（意味は判定しない＝そこに書いてあるか）。
    """
    guards = list(guards or [])
    if not guards:
        return True, "壊し方の指定はありません"
    body = (str((row or {}).get("title") or "") + "\n"
            + str((row or {}).get("detail") or ""))
    bad = [g for g in guards
           if not (g in body
                   or _guard_declares_issue(g, (row or {}).get("id")))]
    if bad:
        return False, ("案件に書かれていない壊し方です: "
                       + " / ".join(g[:40] for g in bad)
                       + "／★その案件の本文に壊し方の名前を書くか、"
                         "mutation_check の項目に issues: [番号] を"
                         "書いてください★")
    return True, f"壊し方 {len(guards)} 件はすべて案件の本文にあります"


def close_issue(issue_id: int, slug: str, checks, texts, why_extra="",
                guards=None) -> int:
    """★案件を閉じる唯一の入口★ 0=閉じた / それ以外=閉じなかった

    ★★番号・機種・検査を結び付ける★★（2026-08-30・Codexの指摘2）
      直す前は番号を見ずに逐語だけ確かめていたので、
      ★別の機種で「存在しない文」を指定すれば、どの案件でも閉じられた★。
    ★★書き込む直前にもう一度確かめる★★（同・指摘3）
      検査と台帳の書き換えの間に別のコミットが入ると、
      「いまの記事で確かめた」と言えなくなる。
    """
    ok, why = precheck_close(issue_id, slug)
    print("  " + why)
    if not ok:
        print("★閉じません★")
        return 1
    row = find_issue(issue_id)
    guards = list(guards or [])
    ok, why = texts_from_issue(row, texts)
    print("  " + why)
    if not ok:
        print("★閉じません★")
        return 1
    ok, why = guards_from_issue(row, guards)
    print("  " + why)
    if not ok:
        print("★閉じません★")
        return 1
    ok, why = kind_allows(row, checks, texts, guards)
    print("  " + why)
    if not ok:
        print("★閉じません★")
        return 1
    ok, why = checks_bound_to_issue(row, checks, texts, guards)
    print("  " + why)
    if not ok:
        print("★閉じません★")
        return 1
    # ★★古くなった条件では閉じない★★（2026-09-17・Codexの指摘②）
    #   ★直す前は、ここで知らせるだけだった★＝
    #   登録した版と違う版で確かめて、そのまま閉じていた。
    _st = stale_conditions(row)
    if _st:
        print("  " + _st[0])
        print("★閉じません★ " + _oi_mod.REPAIR_STEPS)
        return 1
    if _dirty():
        print("★閉じません★ 未コミットの変更があります"
              "（いまの記事で確かめたと言えないため）")
        return 1

    head0 = _head()
    ok, whys, done = run_checks(slug, checks, texts, head0, guards=guards,
                                row=row)
    for w in whys:
        print("  " + w[:130])
    if not ok:
        print("★閉じません★（1件でも通らなければ閉じない）")
        return 1

    # ★書き込む直前に、検査したときと同じ状態のままかを見る★
    if _head() != head0 or _dirty():
        print("★閉じません★ 確かめている間にリポジトリが動きました")
        return 1

    ops = _lp.doc("ops")
    os.makedirs(ops, exist_ok=True)
    p = os.path.join(ops, f"close_{issue_id}.txt")
    lines = ["2AIがこの案件を読み、直っていれば通るはずの検査を決めました。",
             "機械がその検査を全部やり直し、通ったので閉じます。",
             f"機種: {slug} ／ コミット: {head0[:12]}"]
    lines += ["  " + w for w in whys]
    if why_extra:
        lines.append("2AIの理由: " + why_extra)
    lines.append("★AIの宣言ではなく、機械が確かめた結果です★")
    io.open(p, "w", encoding="utf-8", newline="\n").write(
        "\n".join(lines) + "\n")

    # ★★受領証を書く★★（2026-09-17・閉じる入口を1本にする）
    #   ★これは「確かめた」という**申告**であって、通行証ではない★＝
    #   受け取った側は中の検査を**その場でもう一度やり直す**。
    #   ＝申告だけでは閉じられないので、偽物を書いても意味がない。
    rp = os.path.join(ops, f"close_receipt_{issue_id}.json")
    io.open(rp, "w", encoding="utf-8", newline="\n").write(
        json.dumps({"schema": RECEIPT_SCHEMA, "issue_id": int(issue_id),
                    "slug": slug, "commit": head0,
                    "issued_at": datetime.now().isoformat(timespec="seconds"),
                    "conditions": done}, ensure_ascii=False, indent=1))

    # ★どの台帳を書くかを明示して渡す★（2026-09-17）
    #   ★なぜ★＝確かめた台帳と、書き換える台帳が同じであることを
    #   両者の既定値がたまたま一致していることに頼らない。
    #   （これが無いと、通しの試験が本番の台帳を書き換えてしまう＝罠㉗）
    r = subprocess.run(
        [sys.executable, os.path.join(_S, "open_issues.py"),
         "--file", LEDGER, "close",
         "--id", str(issue_id), "--reason-file", p, "--receipt", rp],
        cwd=BASE, capture_output=True, text=True, encoding="utf-8",
        errors="replace")
    out = (r.stdout or "").strip() or (r.stderr or "").strip()
    print(out[-200:])
    if r.returncode != 0:
        print(f"★閉じられませんでした★（終了コード {r.returncode}）")
        return r.returncode or 1
    print(f"★閉じました★ #{issue_id}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="担当した機種の台帳を読み直し、機械が確かめて閉じる")
    # ★必須にしない★＝--selftest が起動できなくなり、
    #   壊し方の道具が「壊す前から赤い」になって守りを一度も確かめられない。
    ap.add_argument("--slug", default="")
    ap.add_argument("--close", type=int, metavar="番号",
                    help="この案件を閉じる（--check / --text で検査を名指し）")
    ap.add_argument("--check", action="append", default=[],
                    help="やり直す検査の名前（2AIが決める・複数可）")
    ap.add_argument("--text", action="append", default=[],
                    help="消えているはずの逐語（1件につき text_gone を1回・複数可）")
    ap.add_argument("--guard-mutation", action="append", default=[],
                    help="機械の中身を直したときに使う。"
                         "mutation_check に登録した壊し方の名前（逐語・複数可）。"
                         "★実際にコードを1行壊して、試験が赤くなるかを見る★")
    ap.add_argument("--guard-mutation-file", action="append", default=[],
                    help="同上。★名前に記号が入るときはこちら★"
                         "（自由文をシェルに書かない・鉄則1c）")
    ap.add_argument("--why", default="",
                    help="2AIがそう決めた理由（記録に残す）")
    ap.add_argument("--due", action="store_true",
                    help="★閉じる回で読む案件を出す★"
                         "（機種の段階によらず・未提示→最後に出した日の古い順）")
    ap.add_argument("--limit", type=int, default=2,
                    help="--due で出す件数（既定 2）")
    ap.add_argument("--record", action="store_true",
                    help="出したものに日付を付ける（次は後ろへ回る）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.due:
        today = datetime.now().strftime("%Y-%m-%d")
        got = for_due(a.limit, today)
        print(f"閉じる回で読む案件 {len(got)} 件"
              "（未提示→最後に出した日の古い順）")
        for r in got:
            print(f"\n  #{r.get('id')} [{r.get('severity') or '-'}] "
                  f"{r.get('slug')}: {str(r.get('title'))[:100]}")
            print(f"    {str(r.get('detail') or '')[:400]}")
            for _ln in due_condition_lines(r):
                print("    " + _ln)
        if got:
            print("\n★記事を読んで、直っているなら閉じてください★")
            print("★★①「これが通れば直っている」を案件に登録します★★"
                  "（★登録できるのは、いま実際に落ちている検査だけ★）")
            print("  python scripts/open_issues.py condition --id <番号> "
                  "--check <検査名> --arg <名前>=<値> "
                  "--why-file <理由を書いたファイル> --by claude,codex")
            print("★★②「これで案件の全部を覆った」と封をします★★"
                  "（問題が2つ書いてあるなら、2つとも登録してから）")
            print("  python scripts/open_issues.py seal --id <番号> "
                  "--why-file <理由を書いたファイル> --by claude,codex")
            print("★★③閉じます★★"
                  "（登録した条件は、渡さなくても機械が全部やり直します）")
            print("  python scripts/ledger_sweep.py --slug <機種> "
                  "--close <番号>")
            print("★直っていなければ、その回を数えます★")
            print("  python scripts/open_issues.py attempt --id <番号> "
                  "--round <この回の名前> --note \"試したこと\"")
            print("★★「条件が使えません」と言われたら、登録し直すのではなく"
                  "白紙に戻します★★")
            print("  " + _oi_mod.REPAIR_STEPS)
        if a.record and got:
            mark_shown([r.get("id") for r in got], today)
            print(f"次は後ろへ回します: {[r.get('id') for r in got]}")
        return 0
    if not a.slug:
        print("--slug が要ります")
        return 1

    if a.close is not None:
        # ★機械の中身の直しは、名前をファイルでも渡せる★（鉄則1c）
        _guards = list(a.guard_mutation or [])
        for _p in (a.guard_mutation_file or []):
            try:
                _guards.append(io.open(_p, encoding="utf-8").read().strip())
            except Exception as e:                           # noqa: BLE001
                print(f"★閉じません★ 壊し方の名前を読めません: {_p}（{e}）")
                return 1
        return close_issue(a.close, a.slug, a.check, a.text, a.why,
                           guards=_guards)

    got = for_slug(a.slug)
    print(f"{a.slug}: 開いている案件 {got['checked']} 件")
    for r in got["open"]:
        print(f"\n  #{r['id']} {r['title']}")
        print(f"    {r['detail'][:300]}")
        if r["suggest"]:
            print("    ★参考★ 当てられそうな検査（★これで閉じる判断はしない★）")
            for s in r["suggest"]:
                print(f"      {'○' if s['pass'] else '×'} {s['check']}"
                      f" ／ {s['why'][:70]}")
        else:
            print("    ★参考★ 当てられそうな検査はありません")
    if got["checked"]:
        print("\n★記事を読んで、直っているなら閉じる検査を名指ししてください★")
        print(f"  python scripts/ledger_sweep.py --slug {a.slug} "
              "--close <番号> --check <検査名> --text \"<消えた逐語>\"")
    return 0


def _guard_tests(t) -> None:
    """★壊し方の名前が案件と結び付いているか★（2026-09-08・Codexの指摘2）

    ★直す前★＝`guards` は案件と何も結び付いていなかったので、
    ★合格することが分かっている壊し方の名前を1つ渡すだけで、
    機械の中身と何の関係もない案件まで閉じられた★。
    """
    _row = {"title": "天井が2つある機種を公開できない",
            "detail": "壊し方の名前: ★見出しを区別しない★",
            "kind": "structural"}
    t("★★案件に書かれていない壊し方では閉じない★★"
      "（★合格する壊し方を1つ渡すだけで、無関係な案件を閉じられた★）",
      guards_from_issue(_row, ["★まったく別の壊し方★"])[0] is False)
    t("　案件の本文にある壊し方なら通る",
      guards_from_issue(_row, ["★見出しを区別しない★"])[0] is True)
    t("　指定が無ければ何も言わない", guards_from_issue(_row, [])[0] is True)
    # ★★守りより前に書かれた案件も閉じられること★★（2026-09-08）
    #   ★名前だけを求めると、守りができる前の案件は永久に閉じられない★
    #   （実例＝#497。直し方まで合意してその通りに直っているのに、
    #     案件に守りの名前が無いので、その機種だけ止まり続けていた）。
    #   ★場所（ファイル名と行）での推測はやめた★（Codexの指摘）＝
    #   同じ行を壊す別の守りでも通ってしまい、
    #   「その守りがこの案件を証明する」ことにならなかった。
    #   ★守りの側に、案件番号を名乗らせる★
    import mutation_check as _mc_t
    _g0 = next((m for m in _mc_t.MUTATIONS
                if m.get("issues")
                and sum(1 for x in _mc_t.MUTATIONS
                        if x.get("why") == m.get("why")) == 1), None)
    _nm = str((_g0 or {}).get("why") or "")
    _id = int(((_g0 or {}).get("issues") or [0])[0])
    _mine = {"id": _id, "title": "その案件", "detail": "本文",
             "kind": "structural"}
    t("★★守りが案件番号を名乗っていれば閉じられる★★"
      "（★名前だけを求めると、守りより前の案件は永久に閉じられない★）",
      bool(_g0) and guards_from_issue(_mine, [_nm])[0] is True)
    t("　（対照）別の案件番号では通さない",
      bool(_g0) and guards_from_issue(
          {"id": _id + 100000, "title": "x", "detail": "本文",
           "kind": "structural"}, [_nm])[0] is False)
    t("　（対照）番号が無い案件では通さない",
      bool(_g0) and guards_from_issue(
          {"title": "x", "detail": "本文", "kind": "structural"},
          [_nm])[0] is False)
    t("　（対照）登録されていない名前は通さない",
      guards_from_issue(_mine, ["★存在しない壊し方★"])[0] is False)
    t("　（対照）番号が小数や真偽値では通らない",
      bool(_g0) and all(
          guards_from_issue({"id": v, "title": "x", "detail": "本文",
                             "kind": "structural"}, [_nm])[0] is False
          for v in (float(_id), True, -1, 0)))
    # ★★#497 は動かない試験にする★★（2026-09-08・Codexの指摘）
    #   ★上の試験は「issues を持つ最初の守り」を拾うので、
    #     いつか別の守りに入れ替わって、この結び付けが消えても気づけない★
    _497 = [m for m in _mc_t.MUTATIONS if 497 in (m.get("issues") or [])]
    t("★読者に出ない項目まで数える守りは、#497を証明すると名乗っている★"
      "（★入れ替わっても気づけるように、番号を固定して見る★）",
      len(_497) == 1
      and "材料あり" in str(_497[0].get("why") or ""))
    t("　（対照）本文にその守りのファイル名と行を引用しても通らない",
      bool(_g0) and guards_from_issue(
          {"id": _id + 100000, "kind": "structural",
           "title": str(_g0.get("file") or ""),
           "detail": str(_g0.get("before") or "")}, [_nm])[0] is False)
    # ★裏取り待ちの型を、壊し方で通せないこと★
    _ev = {"title": "恩恵が確かめられない", "detail": "本文", "kind": "external_value"}
    t("★★裏取り待ちの案件を、機械の中身の壊し方だけで閉じない★★"
      "（★機械が直ったことは、その値の裏取りが済んだ証明にならない★）",
      kind_allows(_ev, [], [], ["★何かの壊し方★"])[0] is False)
    t("　ほかの検査と組なら、型の判定では止めない",
      kind_allows(_ev, ["text_gone"], [], ["★何かの壊し方★"])[0] is True)


def selftest() -> int:
    ng = []
    ran = [0]

    def t(name, cond):
        ran[0] += 1
        print(("✅ " if cond else "❌ ") + name)
        if not cond:
            ng.append(name)

    _guard_tests(t)
    t("★★題の言葉から検査を挙げる（参考）★★",
      "competitor_names_gone"
      in suggest_checks({"title": "C評価: 他サイト名が本文に出ている"}))
    t("★★当たった検査は全部挙げる★★（最初の1つで打ち切らない）",
      set(suggest_checks({"title": "他サイト名と型式名が残っている"}))
      == {"competitor_names_gone", "model_code_gone"})
    t("★★当てられそうな検査が無ければ空★★",
      suggest_checks({"title": "天井の恩恵が未確定"}) == [])
    for _w, c in SUGGEST_CHECK:
        m = _rc.CHECKS.get(c) or {}
        if not m.get("closeable"):
            ng.append(f"観測どまりの検査を挙げています: {c}")
    t("★★挙げるのは「閉じられる検査」だけ★★",
      not [x for x in ng if "観測" in x])

    # --- ★検査のやり直し★ ------------------------------------------------
    real = ""
    _p = os.path.join(BASE, "assets", "data", "machine-details",
                      "tokyo_ghoul.json")
    if os.path.isfile(_p):
        _d = _sj.read_json(_p, expect=dict)
        for _s in (_d.get("sections") or []):
            for _b in (_s.get("body") or []):
                if isinstance(_b, str) and len(_b) > 30:
                    real = _b[:30]
                    break
            if real:
                break
    gone = "この文はうちどころのどの記事にも存在しません2026"

    # ★★断る理由まで見る★★（2026-08-30・壊し方の道具が4件見逃した）
    #   真偽だけを見ると、木が汚れているだけでも False になるので、
    #   ★狙った守りを一度も通らずに緑になる★（罠④）。
    def why1(*a):
        return (run_checks(*a)[1] or [""])[0]

    t("★★検査を1つも渡されなければ通さない★★",
      "検査が1件もありません" in why1("tokyo_ghoul", [], []))
    t("★★知らない検査の名前は通さない★★",
      "知らない検査です" in why1("tokyo_ghoul", ["そんな検査は無い"], []))
    t("★★観測どまりの検査では閉じられない★★",
      "観測どまりの検査です" in why1("tokyo_ghoul", ["strategy_vs_checker"], []))
    t("★★文体の検査は単独では通さない★★"
      "（recheck 自身が「これだけを根拠に閉じるな」と書いている）",
      "単独では閉じられません" in why1("tokyo_ghoul", ["plain_style_gone"], []))

    # --- ★閉じる入口の前さばき★（★試験用の台帳を自分で作る★） ----------
    #   ★本番の台帳を読まない★（2026-08-30・CIが赤くなった件の本直し）
    #     台帳は書類フォルダ（リポジトリの外）にあり、★CIの機械には無い★。
    #     「無ければ飛ばす」にしたら、CIでは守りを一度も通らず、
    #     壊し方の道具が「守られていません」と言って落ちた（罠④）。
    #   ★本番を読まない利点★＝どこでも同じに動く／台帳の中身が変わっても
    #     試験が落ちない／★本番の台帳を絶対に書き換えない★
    #     （今日、対照実験のつもりで本番の案件を1件消してしまった）。
    _keep_ledger = globals()["LEDGER"]
    _tmpdir = tempfile.mkdtemp(prefix="ledger_sweep_test_")
    try:
        _fake = os.path.join(_tmpdir, "open_issues.json")
        io.open(_fake, "w", encoding="utf-8", newline="\n").write(
            json.dumps({"issues": [
                {"id": 9001, "slug": "tokyo_ghoul", "status": "open",
                 "kind": "quality",
                 "title": "試験用: 記事に『試験用の逐語です』が残っている",
                 "detail": "『試験用の逐語です』という文が本文にあります"},
                {"id": 9002, "slug": "tokyo_ghoul", "status": "closed",
                 "kind": "quality", "title": "試験用: もう閉じた案件",
                 "detail": "閉じています"},
                {"id": 9003, "slug": "tokyo_ghoul", "status": "open",
                 "kind": "external_value", "title": "試験用: 裏取り待ち",
                 "detail": "『裏取り待ちの逐語』が未確定です"},
                {"id": 9004, "slug": "tokyo_ghoul", "status": "open",
                 "kind": "quality", "title": "試験用: 古い条件つき",
                 "detail": "条件を古い版で登録したまま",
                 "resolution_conditions": [{
                     "check": "model_code_gone", "version": 999,
                     "args": {}, "set_at": "2026-08-01",
                     "set_by": ["claude", "codex"],
                     "why": "型式名が消えていれば直り"}],
                 "conditions_sealed": {"at": "2026-08-01"}},
            ]}, ensure_ascii=False))
        globals()["LEDGER"] = _fake

        t("★★存在しない番号では閉じない★★",
          precheck_close(99999999, "tokyo_ghoul")
          == (False, "#99999999 という案件がありません"))
        t("★★すでに閉じている案件は閉じない★★",
          precheck_close(9002, "tokyo_ghoul")[0] is False)
        _ok, _w = precheck_close(9001, "yajikita_mairu")
        t("★★案件の機種と指定の機種が違えば閉じない★★"
          "（＝別機種の「存在しない文」でどの案件でも閉じられた穴）",
          _ok is False and "と違います" in _w)
        t("　★正しい機種なら前さばきは通る★",
          precheck_close(9001, "tokyo_ghoul")[0] is True)
        t("★★案件に書かれていない逐語では閉じない★★"
          "（＝機種が合っていても、でたらめな文字列で閉じられた穴）",
          texts_from_issue(find_issue(9001), [gone])[0] is False)
        t("　★案件の本文にある逐語なら通る★",
          texts_from_issue(find_issue(9001), ["試験用の逐語です"])[0] is True)
        t("★★台帳が無い場所でも同じように動く★★"
          "（CIの機械には書類フォルダがありません）",
          isinstance(_rows(), list))
        t("　★その機種の開いている案件だけを出す★",
          [r["id"] for r in for_slug("tokyo_ghoul")["open"]]
          == [9001, 9003, 9004])

        # ★★閉じる本体を通す★★（罠③＝関数だけを試すと呼び出しを消せる）
        #   ★この2つの関門は、未コミットかどうかを見るより手前にある★ので、
        #   木が汚れていても本当に通せる。
        #   ★断った理由の文まで見る★（罠㉚＝奥にも守りがあるため）
        import contextlib as _ctx3
        import io as _io3

        def _close_says(issue_id, checks, texts=(), guards=()):
            _b = _io3.StringIO()
            with _ctx3.redirect_stdout(_b):
                _r = close_issue(issue_id, "tokyo_ghoul", list(checks),
                                 list(texts), "試験", guards=list(guards))
            return _r, _b.getvalue()

        _r1, _m1 = _close_says(9001, ["model_code_gone"])
        t("★★条件が無い案件を、検査名だけでは閉じない★★"
          "（同じ機種で通る無関係な検査を1つ挙げるだけで閉じられていた）",
          _r1 != 0 and "閉じる条件が1件も登録されていません" in _m1)
        _r1b, _m1b = _close_says(9001, [], ["試験用の逐語です"])
        t("★★案件の本文にある逐語だけでも閉じない★★"
          "（案件の説明文そのものを渡せば、記事に無いので必ず通ってしまう）",
          _r1b != 0 and "閉じる条件が1件も登録されていません" in _m1b)
        _r2, _m2 = _close_says(9004, ["model_code_gone"])
        t("★★古い版の条件のままでは閉じない★★"
          "（登録し直さなくても、いまの版で確かめて閉じられていた）",
          _r2 != 0 and "版が変わりました" in _m2)
    finally:
        globals()["LEDGER"] = _keep_ledger
        shutil.rmtree(_tmpdir, ignore_errors=True)

    # ★★見せた日の控えは、共有の state.json に置かない★★
    #   （2026-08-30・Codexの指摘4）＝共有ファイルを丸ごと書き戻すと、
    #   ★その間に別の処理が入れた更新を、古い内容で上書きする★。
    t("★★見せた日の控えを、共有の state.json に置かない★★"
      "（別の処理の更新を消してしまう）",
      os.path.basename(_state_path()) != "state.json")
    t("　★専用の置き場を使う★",
      os.path.basename(_state_path()) == "ledger_site_state.json")

    # ★★結び付けは「登録」の1本だけ★★
    #   （2026-09-17・Codexの指摘①②・どちらも実際に再現した）
    #   ①★ヤメ時の書き方の案件を、型式名の検査で閉じられた★
    #     ＝同じ機種で通る検査を1つ探してくるだけでよかった。
    #   ②★案件の説明文そのものを「消えた逐語」にできた★
    #     ＝もともと記事に無い文字なので、必ず「消えている」と出た。
    _plain = {"id": 7, "slug": "hokuto", "kind": "quality",
              "title": "ヤメ時の説明", "detail": "ヤメ時の説明が読みづらい"}
    t("★★条件が無い案件を、検査名だけで閉じない★★"
      "（同じ機種で通る無関係な検査で閉じられていた）",
      checks_bound_to_issue(_plain, ["model_code_gone"], [], [])[0] is False)
    t("★★案件の本文にある逐語も、それだけでは結び付きにしない★★"
      "（案件の説明文そのものを渡せば、記事に無いので必ず通る）",
      checks_bound_to_issue(_plain, [], ["ヤメ時の説明が読みづらい"],
                            [])[0] is False)
    t("　★壊し方だけでも結び付きにしない★",
      checks_bound_to_issue(_plain, [], [], ["壊し方の名前"])[0] is False)
    t("　★条件はあるが封が無ければ通さない★"
      "（条件1件だけで、案件の片方の問題を直さずに閉じられた）",
      checks_bound_to_issue(
          dict(_plain, resolution_conditions=[{"check": "model_code_gone"}]),
          [], [], [])[0] is False)
    t("　★条件と封がそろえば通る★",
      checks_bound_to_issue(
          dict(_plain, resolution_conditions=[{"check": "model_code_gone"}],
               conditions_sealed={"at": "2026-09-17"}),
          [], [], [])[0] is True)

    # ★★登録した条件は、呼び出し側が何を渡しても全部やり直す★★
    #   ★直す前は「渡された検査を全部やる」だけ★で、
    #   「必要な検査を全部渡したか」を誰も見ていなかった
    #   ＝問題が2つ書いてある案件を、片方の検査だけで閉じられた。
    #   ★木が汚れていても分かる形にする★＝「何を先にやり直したか」で見る
    #   （合否は `closeable` が未コミットを見るので、手元では当てにできない）
    _two = {"slug": "hokuto", "resolution_conditions": [
        {"check": "text_gone", "version": _rc.CHECKS["text_gone"]["version"],
         "args": {"text": "登録ぶんの逐語XYZ"}}]}
    _ok2, _w2, _d2r = run_checks("hokuto", ["model_code_gone"], [], _head(),
                                 row=_two)
    t("★★渡していない登録ぶんを、呼び出し側が省けない★★"
      "（問題が2つある案件を、片方の検査だけで閉じられた）",
      bool(_w2) and "登録した条件[text_gone]" in _w2[0])

    # ★★組み立てた条件そのものを見る★★（木が汚れていても分かる形）
    _mix = {"slug": "hokuto", "resolution_conditions": [
        {"check": "text_gone", "version": 7,
         "args": {"slug": "yajikita_mairu", "text": "ある文"}}]}
    _built = conditions_for_row("hokuto", _mix, "0" * 40)
    t("★★登録の側が別の機種を名乗っても、案件の機種で動かす★★"
      "（その機種の記事で通してしまう）",
      _built[0]["args"]["slug"] == "hokuto")
    t("★★登録した版をそのまま入れる★★"
      "（いまの版を入れると、登録し直さなくても閉じられる）",
      _built[0]["version"] == 7)
    t("　★引数はそのまま運ぶ★",
      _built[0]["args"]["text"] == "ある文")

    # ★★登録した版で確かめる★★（2026-09-17・Codexの指摘②・実際に再現した）
    #   ★直す前は、いつも「いまの版」を入れていた★ので、
    #   条件を古い版で登録したまま、新しい版で確かめて閉じられた。
    _oldv = {"slug": "hokuto", "resolution_conditions": [
        {"check": "model_code_gone", "version": 999, "args": {}}]}
    t("★★古い版の条件では、検査そのものが通らない★★"
      "（いまの版を入れると、登録し直さなくても閉じられた）",
      run_checks("hokuto", ["model_code_gone"], [], _head(),
                 row=_oldv)[0] is False)

    # ★★登録した条件は静かに古くなる★★（2026-09-17）
    #   版が上がると閉じられなくなるのに、理由がどこにも出なかった。
    _live = {"check": "confirmed_value_recorded",
             "version": _rc.CHECKS["confirmed_value_recorded"]["version"],
             "args": {"field": "ceiling"}}
    t("　★いまの版と同じ条件は、何も言わない★", condition_stale(_live) == [])
    # ★★詰まりを知らせる文は、必ず直し方まで言う★★
    #   （2026-09-17・Codexの指摘・罠⓸）＝
    #   ★直す前は「登録し直してください」で終わっていた★ので、
    #   案内どおりに動くと**同じ輪に戻るだけ**だった
    #   （版が上がった条件は、登録し直しても古いほうが残る）。
    #   ★無人タスクは案内どおりに動く★ので、これは実害になる。
    _RE = _oi_mod.REPAIR_STEPS
    t("★★版が変わった条件の知らせに、白紙に戻す道が書いてある★★",
      all(_RE in w for w in condition_stale(dict(_live, version=999))))
    t("　★名簿から消えた検査の知らせにも書いてある★",
      all(_RE in w for w in
          condition_stale(dict(_live, check="そんな検査はありませんXYZ"))))
    t("　★観測どまりに変わった検査の知らせにも書いてある★",
      all(_RE in w for w in condition_stale(
          {"check": "strategy_vs_checker",
           "version": _rc.CHECKS["strategy_vs_checker"]["version"],
           "args": {}})))
    t("　★壊れた条件の知らせにも書いてある★",
      _RE in _oi_mod.conditions_broken(
          {"resolution_conditions": [{"check": "x"}, "壊れた要素"]})
      and _RE in _oi_mod.conditions_broken(
          {"resolution_conditions": "ただの文字列"}))

    # ★★案件を出すときにも、壊れた一覧をそのまま知らせる★★
    #   （2026-09-17・Codexの指摘）＝★直す前は「未登録」「その分だけ」に見えた★。
    #   その案内どおり登録しても、壊れた要素が残るので結局閉じられない。
    t("★★一覧ですらない条件を「未登録」と見せない★★",
      any(_RE in x for x in
          due_condition_lines({"resolution_conditions": "ただの文字列"}))
      and not any("未登録" in x for x in
                  due_condition_lines(
                      {"resolution_conditions": "ただの文字列"})))
    t("★★壊れた要素が混ざった一覧を「その分だけ」と見せない★★",
      any(_RE in x for x in due_condition_lines(
          {"resolution_conditions": [{"check": "model_code_gone",
                                      "version": 1, "args": {}},
                                     "壊れた要素"]})))
    t("　★登録が無い案件は、今までどおり「未登録」と言う★",
      any("未登録" in x for x in due_condition_lines({})))
    t("　★古くなった条件は「登録し直すだけでは直りません」と言う★",
      any("登録し直すだけでは直りません" in x for x in due_condition_lines(
          {"resolution_conditions": [dict(_live, version=999)]})))
    t("★★検査の版が変わった条件は、その場で知らせる★★"
      "（その案件だけ、理由の分からないまま閉じられなくなる）",
      bool(condition_stale(dict(_live, version=999))))
    t("★★名簿から消えた検査の条件も知らせる★★",
      bool(condition_stale(dict(_live, check="そんな検査はありませんXYZ"))))
    t("★★観測どまりに変わった検査の条件も知らせる★★",
      bool(condition_stale({"check": "strategy_vs_checker", "version":
                            _rc.CHECKS["strategy_vs_checker"]["version"],
                            "args": {}})))

    # ------------------------------------------------ 公開の判定は台帳と別
    # ★★台帳を閉じても、公開してよいかの判定は動かない★★
    #   （2026-09-17・Codexの条件①「公開判定は従来どおり独立」）
    #   ★なぜ要るか★＝閉じる工程を「記事を直す」から切り離したので、
    #   ★まだ公開していない機種の案件も閉じられるようになった★。
    #   もし台帳が公開の判定に効いていたら、
    #   ★案件を閉じた瞬間に、中身の薄い新台が検索に載る★。
    import ast as _ast
    _pd_src = io.open(os.path.join(_S, "page_decision.py"),
                      encoding="utf-8").read()
    _pd_tree = _ast.parse(_pd_src)
    _imports = set()
    for _n in _ast.walk(_pd_tree):
        if isinstance(_n, _ast.Import):
            _imports |= {al.name.split(".")[0] for al in _n.names}
        elif isinstance(_n, _ast.ImportFrom):
            _imports.add(str(_n.module or "").split(".")[0])
    t("★★機種の区分を決める側は、台帳を一切読まない★★"
      "（読むようになったら、案件を閉じるだけで公開の線が動く）",
      "open_issues" not in _imports)
    import page_decision as _pd
    # ★判定書は本物の発行器に作らせる★（手で書いた偽物を採点しない・罠①）
    _pending = {"slug": "zz_pending", "name": "試験用",
                "publication_policy": _pd.SCHEMA,
                "page_decision": _pd.decide_for_schema(
                    {}, _pd.SCHEMA, {"mode": "normal"}, "2026-09-17")}
    _cls1 = _pd.machine_class(_pending, {"mode": "normal"})
    _keep_bs = _oi_mod.blocking_slugs
    try:
        _oi_mod.blocking_slugs = lambda *a, **k: {}      # ★案件を全部閉じた状態★
        _cls2 = _pd.machine_class(_pending, {"mode": "normal"})
    finally:
        _oi_mod.blocking_slugs = _keep_bs
    t("　★案件が1件も無くなっても、区分は AUTO_PENDING のまま★",
      _cls1 == "AUTO_PENDING" and _cls2 == "AUTO_PENDING")

    t("★★裏取り待ちの案件は、逐語が消えただけでは閉じない★★"
      "（＝2026-08-30に #155 を誤って閉じた型）",
      kind_allows({"kind": "external_value"}, [], ["消えた文"])[0] is False)
    t("　★ほかの検査と組なら通る★",
      kind_allows({"kind": "external_value"}, ["text_gone"],
                  ["消えた文"])[0] is True)
    t("　★ほかの型なら逐語だけでも通る★",
      kind_allows({"kind": "quality"}, [], ["消えた文"])[0] is True)

    if _dirty():
        t("★★未コミットの木では、消えている逐語でも閉じない★★",
          run_checks("tokyo_ghoul", [], [gone])[0] is False)
        print("⏭ 木が汚れているので「逐語が消えたか」の4件は飛ばしました"
              "（CI・mutation_check の綺麗な写しで動きます）")
    else:
        t("★★消えている逐語なら通す★★",
          run_checks("tokyo_ghoul", [], [gone])[0] is True)
        t("★★まだ記事に残っている逐語なら通さない★★",
          bool(real) and run_checks("tokyo_ghoul", [], [real])[0] is False)
        t("★★2件のうち1件でも残っていたら通さない★★"
          "（＝片方だけ確かめて閉じる罠を塞ぐ）",
          bool(real)
          and run_checks("tokyo_ghoul", [], [gone, real])[0] is False)
        t("　★順番を入れ替えても同じ★",
          bool(real)
          and run_checks("tokyo_ghoul", [], [real, gone])[0] is False)

    print(f"\n{ran[0] - len(ng)}/{ran[0]} " + ("合格" if not ng else "不合格"))
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


if __name__ == "__main__":
    raise SystemExit(main())

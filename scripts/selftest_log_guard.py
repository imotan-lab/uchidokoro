# -*- coding: utf-8 -*-
"""★試験の間だけ本番の記録を止める（決まりを1か所に置く）★（2026-09-13・台帳#655）

★なぜ要るか★＝守りを1行ずつ壊して確かめる道具が自己試験を何百回も動かすので、
本番のログが試験の書き込みで埋まる。実測＝
  ・新台タスクの当日ログ 5309行のうち、★本物の実行は1行も無かった★
  ・秘密の見張りの記録は 28万行 ／ ロックの記録は 2万4千行
＝★止まった晩に、本物の1行が試験の山に埋もれて原因を追えない★。

★同じ形を5本で見つけた★（担当の記録・新台タスク・裏取りの検査・ロック・下見）。
2026-09-09に担当の記録だけ直したとき、★隣を数え上げなかった★のが理由。
★1本ずつ書くと必ずどれかが食い違う★ので、決まりはここに1つだけ置く。

★守る線（4つとも試験にする）★
  ①試験の間は書かない
  ②★本番では必ず書く★（片方だけ確かめると、記録そのものを殺しても緑になる）
  ③★既定は「書く」側★（試験は自分で置き直すので、既定を壊されても気づけない）
  ④★試験が終わったら必ず戻す★（別のコードから呼んだあと、本番の記録が止まる）
"""

FLAG = "_IN_SELFTEST"
DEFAULT_FLAG = "_SELFTEST_DEFAULT_WAS_OFF"


def run_selftest(mod_globals: dict, body, flag: str = FLAG,
                 default_flag: str = DEFAULT_FLAG):
    """★試験の間だけ記録を止め、★必ず戻す★★

    `body` の戻り値をそのまま返す。
    ★始める前の既定値★を `default_flag` に控える
    （既定を「試験中」に壊されても、試験は自分で置き直すので気づけないため）。
    """
    keep = mod_globals.get(flag, False)
    mod_globals[default_flag] = (keep is False)
    mod_globals[flag] = True
    try:
        return body()
    finally:
        mod_globals[flag] = keep


def probe(mod_globals: dict, flag: str = FLAG,
          default_flag: str = DEFAULT_FLAG) -> tuple:
    """★戻すことを、その場で確かめる★（試験から呼ぶ）

    返すのは3つ＝(中では止まっていたか, 戻ったか, 既定を控えたか)。
    ★本番の状態（書く側）から始める★＝ここを試験中の状態から始めると、
    戻していなくても値が同じになり、★戻し忘れを見つけられない★。
    """
    orig_flag = mod_globals.get(flag, False)
    orig_default = mod_globals.get(default_flag, False)
    seen = {}
    try:
        mod_globals[flag] = False
        run_selftest(mod_globals,
                     lambda: seen.update(inside=mod_globals.get(flag)),
                     flag=flag, default_flag=default_flag)
        return (seen.get("inside") is True,
                mod_globals.get(flag) is False,
                mod_globals.get(default_flag) is True)
    finally:
        mod_globals[flag] = orig_flag
        mod_globals[default_flag] = orig_default


def run_selftest_path(mod_globals: dict, body, attr: str, new_value):
    """★記録の行き先を、試験の間だけ別の場所へ向け、★必ず戻す★★

    ★「書かない」ではなく「向け直す」形が要る場所がある★＝
    自分が書いたログの中身を見る試験（秘密値が出ていないか）は、
    書かなくすると★何も確かめずに合格する★ようになる。
    `body(もとの値)` として、本番の行き先も試験から見えるように渡す。
    """
    keep = mod_globals.get(attr)
    mod_globals[attr] = new_value
    try:
        return body(keep)
    finally:
        mod_globals[attr] = keep


def probe_path(mod_globals: dict, attr: str = "_PROBE_PATH") -> tuple:
    """★向け直しを戻すことを、その場で確かめる★（試験から呼ぶ）

    返すのは3つ＝(中で向いていたか, 戻ったか, もとの値を渡したか)。
    """
    orig = mod_globals.get(attr)
    seen = {}
    try:
        mod_globals[attr] = "本番の行き先"
        run_selftest_path(
            mod_globals,
            lambda real: seen.update(inside=mod_globals.get(attr), real=real),
            attr, "一時の行き先")
        return (seen.get("inside") == "一時の行き先",
                mod_globals.get(attr) == "本番の行き先",
                seen.get("real") == "本番の行き先")
    finally:
        mod_globals[attr] = orig


def under(path, root) -> bool:
    """★その場所が、この根の下にあるか★（2026-09-13・Codexの指摘）

    ★文字の前方一致では境目にならない★＝`<根>_outside` も通ってしまう。
    ★実体に直してから、共通の親で見る★（つなぎ・相対表記・大文字小文字も吸収）。
    """
    import os
    # ★空は「いまの場所」に化ける★（2026-09-13・Codexの指摘）＝
    #   `realpath("")` は現在位置を返すので、根が現在位置やその親なら
    #   ★何も渡していないのに「下にある」と答えていた★。
    if not str(path or "").strip() or not str(root or "").strip():
        return False
    try:
        p = os.path.realpath(str(path))
        r = os.path.realpath(str(root))
        return os.path.commonpath([p, r]) == r
    except (ValueError, OSError):
        return False                      # ★分からなければ通さない★


def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    g = {FLAG: False, DEFAULT_FLAG: False}
    inside = {}
    got = run_selftest(g, lambda: (inside.update(v=g[FLAG]), 7)[1])
    t("★試験の間は止める★", inside.get("v") is True)
    t("★終わったら戻す★", g[FLAG] is False)
    t("★始める前の既定が『書く』側だったと控える★", g[DEFAULT_FLAG] is True)
    t("　戻り値はそのまま返す", got == 7)

    g2 = {FLAG: True, DEFAULT_FLAG: False}
    run_selftest(g2, lambda: None)
    t("★既定が『試験中』だったら、そう控える★", g2[DEFAULT_FLAG] is False)
    t("　元が試験中なら、試験中のまま戻す", g2[FLAG] is True)

    def _boom():
        raise RuntimeError("試験の中で落ちた")

    g3 = {FLAG: False, DEFAULT_FLAG: False}
    try:
        run_selftest(g3, _boom)
    except RuntimeError:
        pass
    t("★試験が途中で落ちても戻す★", g3[FLAG] is False)

    g4 = {FLAG: False, DEFAULT_FLAG: False}
    t("★確かめ役（probe）は3つとも本当のことを言う★",
      probe(g4) == (True, True, True))
    t("　確かめ役は、呼んだあとの値を動かさない",
      g4[FLAG] is False and g4[DEFAULT_FLAG] is False)

    # ★行き先を向け直す側★（秘密の見張り・下見が使う）
    g5 = {"P": "本番"}
    inside5 = {}
    got5 = run_selftest_path(
        g5, lambda real: (inside5.update(v=g5["P"], real=real), 9)[1],
        "P", "一時")
    t("★行き先を向け直す★", inside5.get("v") == "一時")
    t("★終わったら戻す★（行き先）", g5["P"] == "本番")
    t("★本番の行き先も試験へ渡す★（向け直しただけで満足しないため）",
      inside5.get("real") == "本番")
    t("　戻り値はそのまま返す（行き先）", got5 == 9)

    g6 = {"P": "本番"}
    try:
        run_selftest_path(g6, lambda real: _boom(), "P", "一時")
    except RuntimeError:
        pass
    t("★途中で落ちても戻す★（行き先）", g6["P"] == "本番")

    g7 = {}
    t("★確かめ役（行き先）は3つとも本当のことを言う★",
      probe_path(g7) == (True, True, True))

    # ★根の下かどうかの判定★（文字の前方一致では境目にならない）
    import os as _os_t
    import tempfile as _tf_t
    _root = _tf_t.mkdtemp(prefix="uchi_under_")
    try:
        t("★根の下なら通す★",
          under(_os_t.path.join(_root, "logs", "a.log"), _root))
        t("★根そのものも下とみなす★", under(_root, _root))
        t("★★名前が続いているだけの隣は通さない★★（<根>_outside）",
          not under(_root + "_outside", _root))
        t("★別の場所は通さない★",
          not under(_tf_t.gettempdir(), _os_t.path.join(_root, "logs")))
        t("★分からないものは通さない★", not under("", _root))
        # ★★空は「いまの場所」に化ける★★（2026-09-13・Codexの指摘）＝
        #   根をいまの場所にすると、前の試験（根が一時の場所）では気づけない。
        t("★★空を、いまの場所の下だと答えない★★",
          not under("", _os_t.getcwd())
          and not under("   ", _os_t.getcwd())
          and not under(_os_t.getcwd(), ""))
    finally:
        import shutil as _sh_t
        _sh_t.rmtree(_root, ignore_errors=True)

    ok = all(c for _, c in results)
    print(f"selftest: {sum(1 for _, c in results if c)}/{len(results)} 合格")
    return 0 if ok else 1


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""★自由文（題・詳細・理由）をファイルから安全に受け取る部品★

（2026-10-01・台帳の廃止で open_issues.py から移した。中身は同じ）
★なぜファイル渡しにするか（2026-08-09）★
  2026-08-08、無人タスクが台帳に文章を書こうとしただけで、文中のバッククォートが
  シェルに実行された。★文章はファイルに書き、コマンドにはパスだけを渡す★
  ＝中身は読まれるだけで実行されない。無人タスクが動いている間は直接指定を受け付けない。

使い方（コードから）:
  import text_args; text_args.read_text_arg(直接の文, ファイル, "detail")
  python scripts/text_args.py --selftest
"""
import datetime
import json
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import os as _os_lp                 # noqa: E402
import sys as _sys_lp               # noqa: E402
_sys_lp.path.insert(0, _os_lp.path.dirname(_os_lp.path.abspath(__file__)))
import local_paths as _lp           # noqa: E402

# ★なぜファイル渡しにするか（2026-08-09）★
#   2026-08-08、無人タスクが台帳に
#     「`python scripts/codex_reported.py` を実行する必要がある」
#   と**書こうとしただけ**で、その部分が本当に実行された。
#   バッククォートは文章としては飾りでも、シェルには
#   「ここを実行して結果を差し込め」という命令だから。
#   手順書は「ツールの出力をそのまま転記」「外部サイトの機種名を渡す」形なので、
#   同じことがいつでも起き得る。
#   ★文章はファイルに書き、コマンドにはパスだけを渡す★＝中身は読まれるだけで
#   実行されない。無人タスクが動いている間は、直接指定を受け付けない。

LOCK_PATH = Path(_lp.doc("task.lock"))
LOCK_STALE_MIN = 30           # task_lock.py と同じ（これを超えたら残骸とみなす）
MAX_TEXT_BYTES = 64 * 1024

# ★文章ファイルはここから下だけ★（2026-08-09・依頼127 A-2 P1）
#   どこのファイルでも読めると、うっかり認証情報のファイルを指したときに
#   台帳やメールへその中身が写る。置き場を決めておけば起こらない。
TEXT_ROOTS = (
    Path(_lp.doc("ops")),
    Path(_lp.DESIGN),
)


def _running_task() -> str:
    """無人タスクが動いている最中ならタスク名を返す（動いていなければ空）。"""
    try:
        d = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except Exception:                        # noqa: BLE001
        return ""
    ts = d.get("heartbeat") or d.get("started_at")
    try:
        t = datetime.datetime.fromisoformat(str(ts).replace("Z", ""))
    except Exception:                        # noqa: BLE001
        return ""
    if (datetime.datetime.now() - t).total_seconds() / 60.0 > LOCK_STALE_MIN:
        return ""                            # 残骸のロックは「動いていない」扱い
    return str(d.get("task") or "")


def _read_text_arg(inline: str, path: str, label: str,
                   allow_newline: bool = True) -> str:
    """自由文を受け取る。★直接指定とファイル指定は同時に使えない★"""
    if inline and path:
        raise SystemExit(f"★{label} は直接指定とファイル指定を同時に使えません★")
    if path:
        p = Path(path)
        if p.is_symlink() or not p.is_file():
            raise SystemExit(f"★{label}: 通常のファイルではありません: {path}★")
        real = p.resolve()

        def _inside(child: Path, root: Path) -> bool:
            # ★フォルダの区切りで見る★（2026-08-09・依頼127→128で修正）
            #   文字の前方一致で見ていたため、隣の `ops-secret` まで
            #   許可されていた（実際に読めることを確認した）。
            try:
                child.relative_to(root)
                return True
            except ValueError:
                return False

        if not any(_inside(real, r.resolve()) for r in TEXT_ROOTS):
            raise SystemExit(
                f"★{label}: この置き場のファイルは使えません: {real}★ "
                + "／".join(str(r) for r in TEXT_ROOTS) + " の下に置いてください"
                "（うっかり認証情報のファイルを指しても台帳に写らないため）")
        size = real.stat().st_size          # ★読む前に大きさを見る★
        if size > MAX_TEXT_BYTES:
            raise SystemExit(
                f"★{label}: 大きすぎます（{size}バイト・上限{MAX_TEXT_BYTES}）★")
        raw = real.read_bytes()
        try:
            text = raw.decode("utf-8")       # ★strict＝壊れた文字は受け取らない★
        except UnicodeDecodeError as e:
            raise SystemExit(f"★{label}: UTF-8として読めません（{e}）★")
        # ★Windowsの改行（CRLF）で書かれたファイルも受け取る★
        #   以前はCRを制御文字として弾いていた。メモ帳等で書くと必ずCRLFになる。
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    else:
        text = inline or ""
        # ★シェルを通らない呼び出しだけは直接指定を許す★（2026-08-09）
        #   add_machine_run.py などは subprocess の引数配列で呼ぶので、
        #   文章の中の記号が実行されることはない（危ないのはシェル文字列だけ）。
        #   この印は「うっかり古い書き方に戻らないため」のものであって、
        #   安全の境界ではない（境界は PreToolUse の shell_guard.py）。
        if text and os.environ.get("UCHIDOKORO_ARGV_CALL") != "1" \
                and _running_task():
            raise SystemExit(
                f"★{label} は無人タスクの実行中は直接指定できません"
                f"（{_running_task()} が実行中）★ "
                f"文章をファイルに書いて --{label}-file でパスを渡してください"
                "（コマンドに文章を書くと、中の記号がシェルに実行されます）")
    bad = [c for c in text if c in "\x00" or (ord(c) < 32 and c not in "\n\t")]
    if bad:
        raise SystemExit(f"★{label}: 使えない制御文字が入っています★")
    if not allow_newline and ("\n" in text or "\r" in text):
        raise SystemExit(f"★{label}: 改行は入れられません★")
    return text.strip()


def selftest() -> int:
    """★自由文の受け取りかたの回帰テスト★（2026-08-09・依頼126〜128）

    ここを緩めると「文章を書いただけでコマンドが実行される」経路が戻る。
    """
    import tempfile

    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    def stops(name, fn):
        try:
            fn()
            t(name, False)
        except SystemExit:
            t(name, True)

    global LOCK_PATH
    keep = LOCK_PATH
    ops = TEXT_ROOTS[0]
    # ★自分が作ったものだけ片づける★（元からあった置き場は消さない）
    #   ★「前に見たら無かった」ではなく「自分が作れた」で決める★（依頼146）
    #     見てから作るまでの間に別の実行が作ることがあるので、
    #     exists() の結果を所有の根拠にしない。
    ops_created = False
    # ★後片付けは finally で必ず通すので、先に名前を用意しておく★
    #   （途中で落ちた回に、まだ作っていない名前を触って別の失敗にしない）
    tmp = None
    good = crlf = bad = nl = big = himitsu = sib = None
    stuck = []                 # ★消せなかったもの（黙って残さない）★
    try:
        # ★一時フォルダを作るのも try の中★（2026-08-11・依頼145）
        #   外で作ると、この直後の mkdir が失敗したときに finally へ入らず、
        #   フォルダが残ったままになる。
        tmp = Path(tempfile.mkdtemp())
        try:
            ops.mkdir(parents=True)        # ★作れたときだけ自分のもの★
            ops_created = True
        except FileExistsError:
            pass
        LOCK_PATH = tmp / "task.lock"
        LOCK_PATH.write_text(json.dumps(
            {"task": "uchidokoro-add-machine",
             "heartbeat": datetime.datetime.now().isoformat()}), encoding="utf-8")
        t("　無人タスクが動いていると分かる", _running_task())
        stops("★★無人タスク実行中はシェルからの直接指定を断る★★",
              lambda: _read_text_arg("直接書いた文章", "", "detail"))

        os.environ["UCHIDOKORO_ARGV_CALL"] = "1"
        t("　実行器（引数配列）からの直接指定は通す",
          _read_text_arg("実行器からの文章", "", "detail") == "実行器からの文章")
        del os.environ["UCHIDOKORO_ARGV_CALL"]

        mark = chr(96) + "記号" + chr(96) + " と " + chr(36) + "(canary)"
        good = ops / "_selftest_detail.txt"
        good.write_text(mark, encoding="utf-8", newline="\n")
        t("★★ファイル渡しは無人でも通り、記号はそのまま残る★★",
          _read_text_arg("", str(good), "detail") == mark)

        crlf = ops / "_selftest_crlf.txt"
        crlf.write_bytes("1行目\r\n2行目".encode("utf-8"))
        t("　Windowsの改行（CRLF）でも受け取れる",
          _read_text_arg("", str(crlf), "detail") == "1行目\n2行目")

        # ★隣のフォルダを許さない★（依頼128で実際に読めてしまった）
        #   ★名前は固定にしない★（2026-08-11・依頼146）
        #     固定名 `ops-secret` を丸ごと消す形にしてしまい、
        #     **人が置いた同名フォルダがあれば中身ごと消える**ところだった。
        #     頭が `ops-` で始まる使い捨ての名前にすれば、
        #     「隣を読まない」の検査はそのままで、自分の作ったものだけ消せる。
        sib = Path(tempfile.mkdtemp(prefix=ops.name + "-", dir=ops.parent))
        himitsu = sib / "_selftest.txt"
        himitsu.write_text("許可していない置き場", encoding="utf-8")
        stops("★★許可した置き場の『隣』は読まない（ops-secret 等）★★",
              lambda: _read_text_arg("", str(himitsu), "detail"))

        outside = tmp / "outside.txt"
        outside.write_text("よその文章", encoding="utf-8")
        stops("　決めた置き場の外は読まない",
              lambda: _read_text_arg("", str(outside), "detail"))
        stops("　直接指定とファイル指定の同時使用を断る",
              lambda: _read_text_arg("a", str(good), "detail"))

        bad = ops / "_selftest_bad.bin"
        bad.write_bytes(b"\xff\xfe not utf8")
        stops("　UTF-8として読めないファイルを断る",
              lambda: _read_text_arg("", str(bad), "detail"))

        nl = ops / "_selftest_nl.txt"
        nl.write_text("1行目\n2行目", encoding="utf-8", newline="\n")
        stops("　題に改行は入れられない",
              lambda: _read_text_arg("", str(nl), "title", allow_newline=False))

        big = ops / "_selftest_big.txt"
        big.write_text("あ" * 40000, encoding="utf-8", newline="\n")
        stops("　大きすぎるファイルを断る",
              lambda: _read_text_arg("", str(big), "detail"))

        LOCK_PATH.write_text(json.dumps(
            {"task": "x", "heartbeat":
             (datetime.datetime.now() - datetime.timedelta(hours=2)).isoformat()}),
            encoding="utf-8")
        t("　残骸のロックは実行中とみなさない", _running_task() == "")

    finally:
        LOCK_PATH = keep
        # ★後片付けは必ず通る場所へ★（依頼143の指摘3）
        #   途中で落ちた回ほど一時ファイルが残るので、finally に置く。
        # ★決めた置き場（ops）と、その隣に作ったものを消す★
        for f in (good, crlf, bad, nl, big, himitsu):
            if f is None:
                continue
            try:
                f.unlink()
            except FileNotFoundError:
                pass
            except Exception as e:         # noqa: BLE001
                stuck.append("%s（%s）" % (f.name, type(e).__name__))
        for d in (sib, tmp):
            # ★一時フォルダは丸ごと消す★（2026-08-11・依頼144）
            #   個々に並べると**足し忘れたものが黙って残る**ので、
            #   この中に作ったものはまとめて回収する。
            if d is None:
                continue
            try:
                import shutil
                shutil.rmtree(d)
            except FileNotFoundError:
                pass
            except Exception as e:         # noqa: BLE001
                stuck.append("%s（%s）" % (d.name, type(e).__name__))
        if ops_created:
            # 自分が作った置き場だけ戻す（元からあれば触らない）
            try:
                ops.rmdir()
            except FileNotFoundError:
                pass
            except OSError as e:
                # ★ここの失敗も黙って通さない★（依頼146）
                stuck.append("%s（%s）" % (ops.name, type(e).__name__))
        # ★消せなかったことを黙って通さない★（2026-08-11・依頼145）
        #   握りつぶしていたので、権限や掴まれで残っても合格に見えていた。
        t("　後片付けが実際にできた（残ったもの: %s）"
          % ("なし" if not stuck else "、".join(stuck)), not stuck)

    ng = sum(1 for _, o in results if not o)
    print()
    print("%d/%d 合格" % (len(results) - ng, len(results)))
    return 1 if ng else 0


read_text_arg = _read_text_arg


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    print(__doc__)

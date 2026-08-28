# -*- coding: utf-8 -*-
"""★表示の確認を、確かに自分のサーバーで行う★（2026-08-28・台帳#493）

★何が起きていたか★
  手順書は「サーバーを裏で起動してPIDを控え、終わったら kill」と書いていた。
  ところがこの環境では、控えたPID（シェルの仕事の番号）と、
  ★実際にポートを掴んでいる python の番号が別物★になるため、
  kill は成功したように見えて**サーバーは生き残る**。
  2026-08-28 の点検で、同じサーバーが7つ残っており、
  ★いちばん古いものは5日間動き続けていた★。
  そのため 2026-08-28 の1機種目の表示検査は、
  ★自分のサーバーではなく、過去の実行が残したサーバーが応答していた★
  （自分のサーバーの記録が1行も無いのに「問題なし」と出ていた）。

★この道具が守ること★
  (a) 使うポートが**空いている**ことを先に確かめる（塞がっていたら使わない）
  (b) 検査のあとに**自分のサーバーが実際に応答したか**を確かめる
      （記録が空なら、その検査結果は無効として失敗にする）
  (c) 終わったら**自分が起こした子プロセスを**必ず止める
      （番号を控えるのではなく、子プロセスそのものを持っている）

使い方:
    python scripts/render_check.py --slug <機種>
    python scripts/render_check.py --slug <機種> --port 8791
    python scripts/render_check.py --selftest
"""
import argparse
import os
import socket
import subprocess
import sys
import re
import time
import uuid

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                      # noqa: BLE001
        pass

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST = "127.0.0.1"
FIRST_PORT = 8765
TRIES = 40


def port_free(port: int, host: str = HOST) -> bool:
    """★そのポートが空いているか★（誰かが掴んでいたら False）

    ★「つながらない＝空いている」で見る★＝つながってしまうなら、
    そこには**別の誰か**が居る（過去の実行の残骸かもしれない）。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        try:
            s.connect((host, port))
        except OSError:
            return True
        return False


def pick_port(first: int = FIRST_PORT, tries: int = TRIES) -> int:
    """★空いているポートを探す★（見つからなければ例外＝黙って進まない）"""
    for p in range(first, first + tries):
        if port_free(p):
            return p
    raise RuntimeError(
        f"空いているポートがありません（{first}〜{first + tries - 1}）"
        "／★残骸が積み上がっている疑い★")


def served_count(log_path: str) -> int:
    """★自分のサーバーが実際に応答した回数★

    `http.server` は要求ごとに1行を標準エラーへ書く。
    ★0行なら、応答したのは自分ではない★＝検査結果を信じてはいけない。
    """
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            return sum(1 for line in fh if '"GET ' in line or '"HEAD ' in line)
    except OSError:
        return 0


def _start(port: int, log_path: str):
    """★そのポートで自分のサーバーを起こし、本当に自分のものか確かめる★

    ★空きを確かめてから bind するまでに隙間がある★
      （2026-08-28・Codexの10回目の指摘2）＝
      同時に2本動くと、両方が同じポートを選び、片方だけが bind に成功する。
      ★記録の名前がポート番号だけだと、失敗した側が成功した側の記録を見て
        「自分が応答した」と読む★＝直したかった事故と同じ型。

    ★自分だけが知っている合図で確かめる★＝
      合図つきの道を1回叩き、**自分の記録**にその合図が出れば自分のもの。
    戻り値: (子プロセス, 記録ファイル) ／ 自分のものでなければ (None, None)
    """
    log = open(log_path, "w", encoding="utf-8")
    srv = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", HOST],
        cwd=BASE, stdout=subprocess.DEVNULL, stderr=log)
    for _ in range(50):
        if srv.poll() is not None:         # ★子が死んでいる＝bind に失敗★
            break
        if not port_free(port):
            break
        time.sleep(0.1)
    if srv.poll() is not None:
        log.close()
        return None, None
    # ★合図つきの道を1回叩く★
    nonce = uuid.uuid4().hex
    try:
        with socket.create_connection((HOST, port), timeout=2) as s:
            s.sendall(("GET /__render_check_" + nonce
                       + " HTTP/1.0\r\nHost: x\r\n\r\n").encode())
            s.recv(64)
    except OSError:
        pass
    log.flush()
    for _ in range(30):
        if nonce in _read(log_path):
            return srv, log
        time.sleep(0.1)
    # ★自分の記録に合図が無い＝そのポートは自分のものではない★
    srv.terminate()
    try:
        srv.wait(timeout=10)
    except Exception:                      # noqa: BLE001
        srv.kill()
    log.close()
    return None, None


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def run(slug: str, port: int = 0, extra=None) -> int:
    # ★記録の名前は実行ごとに一意★（同時に動いても混ざらない）
    tag = f"{os.getpid()}_{uuid.uuid4().hex[:8]}"
    log_path = os.path.join(BASE, f".render_check_{tag}.log")
    fixed = bool(port)
    srv = log = None
    for cand in ([port] if fixed else range(FIRST_PORT, FIRST_PORT + TRIES)):
        if not port_free(cand):
            if fixed:
                print(f"★ポート {cand} は誰かが使っています★"
                      "（過去の実行の残骸かもしれません）。検査しません")
                return 2
            continue
        srv, log = _start(cand, log_path)
        if srv:
            port = cand
            break
        if fixed:
            print(f"★ポート {cand} で自分のサーバーを持てませんでした★")
            return 2
    if not srv:
        print("★空いているポートで自分のサーバーを起こせませんでした★")
        return 2
    print(f"自分のサーバー: ポート {port}（記録 {os.path.basename(log_path)}）")
    try:
        cmd = [sys.executable, os.path.join(BASE, "scripts", "audit_render.py"),
               "--base-url", f"http://{HOST}:{port}", "--slug", slug]
        cmd += list(extra or [])
        r = subprocess.run(cmd, cwd=BASE)
        rc = r.returncode
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except Exception:                  # noqa: BLE001
            srv.kill()
        log.close()
    # ★合図の1回は数えない★
    n = max(0, served_count(log_path) - 1)
    print(f"自分のサーバーが応答した回数: {n}")
    if n == 0:
        # ★★別のサーバーが応答した疑い★★＝結果を信じない
        print("★★自分のサーバーに記録がありません★★"
              "／★別のサーバーが応答した疑いがあるので、"
              "この検査結果は無効にします★")
        return 3
    try:
        os.remove(log_path)
    except OSError:
        pass
    return rc


def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅ " if cond else "❌ ") + name)

    # ★空いているかの判定を、実物の口で確かめる★
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind((HOST, 0))
    s.listen(1)
    taken = s.getsockname()[1]
    t("★★誰かが掴んでいるポートは「空いていない」と分かる★★"
      "／★これが分からないと、過去の残骸に検査させてしまう★",
      port_free(taken) is False)
    s.close()
    time.sleep(0.2)
    t("　閉じたポートは「空いている」と分かる", port_free(taken) is True)

    # ★空きを探す★
    p = pick_port()
    t("　空いているポートを選べる", isinstance(p, int) and port_free(p))

    # ★応答の記録の数え方★
    tmp = os.path.join(BASE, ".render_check_selftest.log")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write('127.0.0.1 - - [28/Aug/2026 05:00:00] "GET /x HTTP/1.1" 200 -\n')
        fh.write("何かの警告\n")
    t("★★自分のサーバーが応答した回数を数えられる★★", served_count(tmp) == 1)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("何も応答していません\n")
    t("★★記録が空なら0と数える★★"
      "／★0のときに検査結果を無効にできないと、"
      "「何を検査したか分からない合格」が出る★", served_count(tmp) == 0)
    os.remove(tmp)
    t("　記録が無いときも0（例外にしない）",
      served_count(os.path.join(BASE, ".render_check_no_such.log")) == 0)

    # ★★同時に動かしても、他人の応答を自分のものと数えない★★
    #   （2026-08-28・Codexの10回目の指摘2）
    #   ★空きを確かめてから bind するまでに隙間がある★＝
    #   同時に2本動くと両方が同じポートを選び、片方だけが bind に成功する。
    #   ★負けた側が勝った側の記録を見て「自分が応答した」と読む★のが、
    #   まさに直したかった事故と同じ型。
    _other = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _other.bind((HOST, 0))
    _other.listen(1)
    _taken = _other.getsockname()[1]
    _lg = os.path.join(BASE, ".render_check_ctrl.log")
    try:
        _srv, _log = _start(_taken, _lg)
        if _srv:
            _srv.terminate()
            try:
                _srv.wait(timeout=10)
            except Exception:              # noqa: BLE001
                _srv.kill()
        if _log:
            _log.close()
        t("★★他人が掴んでいるポートを、自分のものと言わない★★"
          "／★これを間違えると、他人が応答した検査を"
          "「合格」と読んでしまう★", _srv is None)
    finally:
        _other.close()
        try:
            os.remove(_lg)
        except OSError:
            pass

    ng = sum(1 for _, o in results if not o)
    print()
    print("%d/%d 合格" % (len(results) - ng, len(results)))
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="表示の確認を、確かに自分のサーバーで行う")
    ap.add_argument("--slug", default="")
    ap.add_argument("--port", type=int, default=0,
                    help="使うポート（省略すると空きを探す）")
    ap.add_argument("--selftest", action="store_true")
    a, rest = ap.parse_known_args()
    if a.selftest:
        return selftest()
    if not a.slug:
        print("★--slug を付けてください★（全機種の検査はしません）")
        return 2
    return run(a.slug, a.port, rest)


if __name__ == "__main__":
    sys.exit(main())

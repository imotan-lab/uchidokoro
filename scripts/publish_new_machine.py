"""publish_new_machine.py — 新台1機種だけを公開する専用の経路。

★なぜ専用にするか（2026-07-31・Codexと相談して案Bに決めた）★
  既存119機種のページを直す `--legacy` に相乗りさせると、
  入力条件も品質も失敗時の扱いも違うものが同じ経路に混ざる。
  既存は `LEGACY_UNVERIFIED`（未裏取り）だが、新台の記事は
  **確認できた項目だけを載せた先行記事**で、意味がまるで違う。
  そこで状態名も別にする → `PREVIEW_VERIFIED_SUBSET`
  （載せた値は出典2件で確認済み・ただし記事は網羅的ではない）。

★この経路が触ってよいもの（これ以外は書かない）★
  1. `machines/{新しいslug}/index.html` を**新規に**作る
  2. `assets/data/machine-details/{新しいslug}.json` を新規に作る
  3. `machines.json` に1件足す
  ★sitemap は判定書しだい★（2026-08-04〜。AUTO_INDEXABLE のときだけ1行足す。
    AUTO_PENDING では触らない＝載せてはいけない機種を載せた事故を検知するため）
  ★既存ページは作り直さない・消さない・上書きしない★

★書く順番（Codexの指摘）★
  **ページを先に置き、最後に machines.json を足す。**
  トップページは machines.json を見てリンクを張るので、
  逆順だと「一覧に出るのにページが無い（404）」瞬間ができる。

使い方:
    python scripts/publish_new_machine.py --slug <slug>          # 確かめるだけ
    python scripts/publish_new_machine.py --slug <slug> --apply
    python scripts/publish_new_machine.py --selftest
"""

from __future__ import annotations

import datetime
import time
import uuid
import argparse
import functools
import hashlib
import json
import re
import subprocess
import tempfile
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import build_machine_pages as _bmp      # noqa: E402
import build_new_article as _ba         # noqa: E402
import page_decision as _pdz            # noqa: E402  ★区分の唯一の判定箇所★
import new_machine_watch as _nwz       # noqa: E402  ★メーカー名簿★
import html_check as _hc                # noqa: E402
import task_lock as _tl                 # noqa: E402  ★見張りを借りる★
import safe_json as _sj                 # noqa: E402

MACHINES = os.path.join(BASE, "assets", "data", "machines.json")
DETAILS = os.path.join(BASE, "assets", "data", "machine-details")
SITEMAP = os.path.join(BASE, "sitemap.xml")
STATE = "PREVIEW_VERIFIED_SUBSET"


class PublishError(RuntimeError):
    pass


# ★slug に使ってよい形★（2026-07-31・自分で確かめて危険を確認）
#   `../` を入れると machines/ の外へ書けてしまう
#   （_page_path("../../evil") → ../evil/index.html）。
# ★断り書きは、決めた文言と丸ごと一致していること★
#   （2026-07-31・Codex指摘5を再現して変えた）
#   必須語と禁止語の組み合わせでは、
#   「先行記事です。解析の結果、全項目が正しいと判明しました。」が通ってしまった。
#   文言はこちらで作るものなので、**丸ごと突き合わせる**のが確実。
# ★時間で嘘になる語（先行・導入前）を使わない★（2026-08-04・Codex70〜72回目）
NOTICE_TEXT = ("⚠ このページは確認が取れた項目のみ掲載しています。"
               "未掲載の項目は確認でき次第更新します。")
NOTICE = chr(100)+chr(97)+chr(116)+chr(97)+chr(45)+"preview-notice="+chr(34)+STATE+chr(34)
_SLUG_OK = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
# 空白の並び（バックスラッシュを直接書かない：制御文字に化ける事故が続いたため）
_WS = "[ " + chr(9) + chr(13) + chr(10) + "]*"


LOCK = os.path.join(BASE, ".publish.lock")


# ★★残骸を時間で片付ける★★（2026-08-21・台帳#379の【4】を直しているときに発見）
#   直す前の _OnlyOne は「ファイルがあれば必ず断る」だけだった。
#   ＝★公開処理が途中で死ぬと、目印が永久に残って以後の新台公開が全部止まる★。
#   しかも止まり方が「いま別の処理が動いています」なので、
#   **動いていないのに動いていると言い続ける**（原因に辿り着けない）。
#   実際に PID 1692 の残骸が丸1日残っていて、この形で再現した。
#
#   ★時間で見る（生き死にを見に行かない）★
#     Windows で os.kill(pid, 0) は「問い合わせ」ではなく**終了させる**ので使えない。
#     task_lock.py と同じ「最後に触れてから30分」を残骸の目安にする。
#     公開1回は数分で終わるので、30分動いていれば異常。
#
#   ★★OSによって振る舞いが違う（寄りかからない）★★（2026-08-21）
#     Windows では、目印を掴んでいる間は OS がファイルを守るので
#     動いている処理からは奪えない（os.replace が WinError 32 で失敗する）。
#     ★Linux では開いたままでも rename できるので、そうならない★。
#     ＝★「OSが守ってくれる」を前提にしない★。
#     実際、そう書いた試験を入れたら CI（ubuntu）が落ち続けた
#     （ci_repro は手元＝Windows で走るので、この差は出ない）。
#     守りの本体は、下の「持ち主の印」と「見張りでの直列化」のほう。
#
#   ★★持ち主を印で確かめる★★（Codexの指摘・2026-08-21）
#     PIDだけでは足りない（PIDは使い回される）。
#     印が無いと、★奪われた側が終わるときに、いま動いている側の目印を
#     消してしまう★＝「同時に2つ公開しない」という肝心の守りが破れる。
#
#   ★★長い処理は touch() で「まだ動いている」と伝える★★
#     --recover を挟む経路が30分を超えても、正常なら奪われないように。
#
#   ★これは途中終了の防御を弱めない★
#     公開が途中で終わったことは **別の目印**（_MARK・1186行の検査）が持っている。
#     ロックの残骸を片付けても、その機種は
#     「前回の公開が途中で終わっています → --recover」で止まったまま。
#     ＝片付けるのは「入口の閂」だけで、「やりかけの後始末」は人と --recover の担当。
LOCK_STALE_MINUTES = 30
# ★監査の子プロセスに制限時間を置く★（2026-08-21・Codexの指摘2）
#   ★これが無いと、監査が固まったときに目印を持ったまま止まり続ける★
#   → 30分を超えて残骸とみなされ、別の処理に奪われる。
#   実測では数秒で終わる。5分は十分な余裕。
AUDIT_TIMEOUT_SEC = 300


class _OnlyOne:
    """★同時に2つ公開しない★（2026-07-31・Codex指摘4）

    2機種を同時に公開すると、どちらも同じ古い machines.json を読み、
    後から置き換えた方が先の追加を消してしまう。
    ロックファイルを「排他作成」で作れた側だけが進む。

    ★ただし残骸は片付ける★（2026-08-21）＝上の説明を参照。
    """

    def __init__(self, path=LOCK):
        self.path = path
        self.fd = None
        self.token = None         # ★自分の目印だと分かる印★
        self.evicted = None       # ★残骸を片付けたら、その事実を残す★
        self.lost = None          # ★途中で奪われていたら、その事実を残す★

    def touch(self) -> bool:
        """★まだ動いていることを目印に伝える★（2026-08-21・Codexの指摘）

        長くかかる経路（--recover など）が、正常に動いているのに
        「30分動いていない＝残骸」と見なされて奪われるのを防ぐ。
        ★自分の目印でなくなっていたら False★（呼び出し側が止まれる）。

        ★★見張りの中で確かめてから触る★★（2026-08-21・Codexの再指摘）
          ★直す前は「持ち主を確かめる → utime」が見張りの外だった★＝
          Windows では掴んでいる fd が実質守っていたが、
          ★Linux ではそうならない★ので、
          **Bの目印をAが touch して成功扱いにする**競合が残っていた。
        """
        try:
            with _tl._Guard(self.path):
                if not self._still_mine():
                    return False
                os.utime(self.path, None)
                return True
        except Exception:                 # noqa: BLE001
            # ★見張りを取れないときは「触れた」と言わない★（fail-closed）
            return False

    def _age_minutes(self):
        """目印に最後に触れてから何分たったか（読めなければ大きい値＝残骸扱い）"""
        try:
            return (time.time() - os.path.getmtime(self.path)) / 60.0
        except OSError:
            return 0.0            # 消えていた＝もう残骸ではない

    def _create(self):
        # ★★自分の目印だと分かる印を書く★★（2026-08-21・Codexの指摘）
        #   PIDだけでは足りない＝PIDは使い回される。
        #   ★これが無いと、奪われた側が終わるときに
        #     「いま動いている別の処理の目印」を消してしまう★。
        self.token = f"{os.getpid()}:{uuid.uuid4().hex}"
        self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(self.fd, self.token.encode())
        os.fsync(self.fd)

    def _holder_is_dead(self) -> bool:
        """★目印を作った処理が、確かに終わっているか★

        ★分からないときは False（＝奪わない）★＝安全側。
        生き死にの見方は `task_guard._Exclusive._alive` を借りる
        （★同じ規則を2か所に書かない★）。
        Windows の `os.kill(pid, 0)` は問い合わせではなく**終了させる**ので、
        向こうと同じく `tasklist` で見る。
        """
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = f.read().strip()
        except OSError:
            return False
        head = raw.split(":", 1)[0]
        if not head.isdigit():
            return False              # 印の形が違う＝分からない
        pid = int(head)
        if pid <= 0 or pid == os.getpid():
            return False
        try:
            import task_guard as _tg
            return not _tg._Exclusive._alive(pid)
        except Exception:             # noqa: BLE001
            return False              # 確かめられない＝奪わない

    def _still_mine(self) -> bool:
        """いまある目印が、自分が作ったものかどうか。"""
        try:
            with open(self.path, "rb") as f:
                return f.read().decode("utf-8", "replace").strip() == self.token
        except OSError:
            return False

    def __enter__(self):
        # ★★取得・退避・解放をひとつの見張りの中で直列化する★★
        #   （2026-08-21・Codexの指摘2）
        #   ★これが無いと何が起きるか★＝
        #     ①先行が「自分の目印だ」と確かめる
        #     ②その直後、後続が残骸とみなして退避し、自分の目印を作る
        #     ③先行が削除する → ★後続の目印が消える★
        #     ＝「同時に2つ公開しない」という肝心の守りが破れる。
        #   所有者を確かめてから消すまでの隙間を、見張りごと塞ぐ。
        #   ★見張りは task_lock._Guard を借りる★（同じ規則を2か所に書かない）
        with _tl._Guard(self.path):
            try:
                self._create()
            except FileExistsError:
                age = self._age_minutes()
                # ★★持ち主が死んでいると確かめられたら、30分待たない★★
                #   （2026-08-21・実際に起きた）
                #   ★何が起きたか★＝手元で試験を強制終了したら目印が残り、
                #   以後の実行が「2分前から動いています」と言い続けた。
                #   ＝**動いていないのに動いていると言う**（原因に辿り着けない）。
                #   夜の公開タスクが（セッション制限などで）落ちた晩にも同じ形になる。
                #
                #   ★安全側の作り★
                #     ・「生きている」と分かるとき／分からないときは奪わない
                #       （PIDは使い回されるので、生きて見えたら時間の規則に任せる）
                #     ・「死んでいる」と確かめられたときだけ早く片付ける
                #   ＝いままでより緩くはならない（待つ場面は今までどおり待つ）。
                if age < LOCK_STALE_MINUTES and not self._holder_is_dead():
                    raise PublishError(
                        "いま別の公開処理が動いています（同時に2つは公開しません）。"
                        f"{age:.0f}分前から動いています。"
                        f"止まったままなら {self.path} を消してください")
                # ★残骸だった★＝原子的に退避してから、もう一度だけ作る。
                #   退避に負けた（他が先に片付けた）ら、素直に断る。
                dst = self.path + ".stale." + datetime.datetime.now().strftime(
                    "%Y%m%d%H%M%S")
                try:
                    os.replace(self.path, dst)
                except OSError as e:
                    raise PublishError(
                        f"止まったままの目印を片付けられませんでした（{e}）。"
                        f"{self.path} を確かめてください")
                try:
                    self._create()
                except FileExistsError:
                    raise PublishError(
                        "いま別の公開処理が動いています（同時に2つは公開しません）")
                self.evicted = {"age_minutes": round(age, 1), "moved_to": dst}
                print(f"★{age:.0f}分ぶん止まっていた目印を片付けました★"
                      f"（{os.path.basename(dst)} へ退避）。"
                      "前回が途中で終わっていれば、この後の検査が止めます")
        return self

    def __exit__(self, *exc):
        # ★★自分の目印でなければ消さない★★（2026-08-21・Codexの指摘）
        #   ★直す前に起きえたこと★＝
        #     ①Aが30分を超えて動いている（正常だが遅い）
        #     ②Bが「残骸だ」と見なしてAの目印を退避し、自分の目印を作る
        #     ③Aが終わって os.remove する
        #     → ★Bの目印が消える★＝以後、Cが割り込めてしまう。
        #   ＝同時に2つ公開しない、という肝心の守りが破れる。
        # ★★見張りを先に取る → その中で閉じる・確かめる・消す★★
        #   （2026-08-21・Codexの再指摘）
        #   ★直す前の穴★＝見張りが取れなかったときに `_guard = None` にして
        #   **そのまま削除処理を続けていた**。すると次の順で事故が起きる:
        #     ①Aが見張りを取れずに（時間切れ等）自分の印を読む
        #     ②Bが見張りの中でAを残骸として退避し、Bの印を作る
        #     ③Aが os.remove() する → ★Bの目印が消える★
        #   ＝「同時に2つ公開しない」が破れる。
        #   ★見張りを取れなければ消さない★（fail-closed）。
        mine = False
        try:
            with _tl._Guard(self.path):
                if self.fd is not None:
                    try:
                        os.close(self.fd)
                    except OSError:
                        pass
                    self.fd = None
                mine = self._still_mine()
                if mine:
                    try:
                        os.remove(self.path)
                    except OSError:
                        pass
        except Exception as e:            # noqa: BLE001
            # ★見張りが取れないまま消さない★＝残骸として残るほうが安全。
            #   30分たてば次の処理が正しい手順で片付ける。
            if self.fd is not None:
                try:
                    os.close(self.fd)
                except OSError:
                    pass
                self.fd = None
            self.lost = "GUARD_FAILED"
            print(f"★公開の目印を片付けられませんでした（{type(e).__name__}）★"
                  f" {self.path} はそのまま残ります"
                  "（30分たてば次の処理が正しい手順で片付けます）")
            return False
        if not mine:
            self.lost = True
            print("★この処理の目印は、途中で別の処理に引き取られていました★"
                  "（自分のものではないので消しません）。"
                  "同じ時間帯に2つ動いていた可能性があるので、"
                  "公開結果を確かめてください")
        return False


# ★作業中の目印★（2026-07-31・Codex9回目・実際に再現した）
#   全部書き終えた直後に電源が落ちると、
#   ページも一覧もそろっているため「中断された処理」と
#   「正常に完成した新台」を区別できなかった。
#   書き始める前にこの目印を作り、全部終わってから消す。
#   目印が残っていれば、次の実行も push も止める。
def rmtree_hard(path) -> bool:
    """★読み取り専用でも消す★（2026-08-28・実測で15GB溜まっていた）

    Windows では `.git` の中に読み取り専用のファイルがあるため、
    ふつうの消し方は失敗する。`ignore_errors=True` だと
    ★黙って失敗して、写しが溜まり続ける★（実測: 500件超・15GB）。
    """
    import shutil as _sh_r
    import os as _os_r
    import stat as _st_r

    def _force(func, p, _exc):
        try:
            _os_r.chmod(p, _st_r.S_IWRITE)
            func(p)
        except Exception:                  # noqa: BLE001
            pass

    try:
        _sh_r.rmtree(path, onerror=_force)
    except Exception:                      # noqa: BLE001
        pass
    return not _os_r.path.exists(path)


def copy_tolerant(src, dst, *a, **k):
    """★写している間に消えたファイルは飛ばす★（2026-08-28）

    ★なぜ要るか（わざと再現して確かめた）★
      試験は本物のリポジトリを写してから使う。
      その最中に直下でファイルが増減すると、
      ★写しを作る処理が例外で落ちる★:

          Error: [('…/うちどころ/.probe_churn_27.tmp', …

      2026-08-28 の朝、強制終了まわりの試験10件が落ちたのはこれだと考えている
      （同じ時刻に、直下でファイルを作っては消す道具を動かしていた）。
    ★「途中で消えた」は写しの失敗ではない★ので、飛ばしてよい。
    """
    import shutil as _sh0
    try:
        return _sh0.copy2(src, dst, *a, **k)
    except FileNotFoundError:
        return dst


IN_PROGRESS = os.path.join(BASE, ".publish-in-progress.json")


def _dirty_now() -> list:
    """★いま変わっているファイルの一覧★（git から見る・見られなければ None）。

    ★見られないときは None★＝関所が「分からない」と答えて止まる
    （fail-closed。空の一覧を返すと「綺麗だった」と嘘をつくことになる）。
    """
    try:
        r = subprocess.run(["git", "status", "--porcelain"], cwd=BASE,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
    except Exception:                                        # noqa: BLE001
        return None
    if r.returncode != 0:
        return None
    out = []
    for line in (r.stdout or "").splitlines():
        name = line[3:].strip().strip('"')
        if not name:
            continue
        for x in (name.split(" -> ") if " -> " in name else [name]):
            x = x.strip().strip('"')
            if x:
                out.append(x)
    return sorted(set(out))


def mark_start(slug: str, machine: dict, backup: dict) -> None:
    """★書き始める前に目印を残す★（電源が落ちても残る）

    ★戻すのに必要な情報も一緒に残す★（2026-07-31・Codex10回目）
      目印だけ消して再実行すると、中途半端な状態のまま
      「正常」と見なして公開できてしまう。
      どのファイルを何に戻せばよいかを、目印の中に書いておく。
    """
    from datetime import datetime
    data = {
        "slug": slug, "name": machine.get("name", ""),
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "pid": os.getpid(),
        # ★★公開を始める前に変わっていたファイル★★（2026-08-25・Codexの25回目）
        #   ★許可一覧は「変わってよいファイル名」しか見ていない★ので、
        #   実行前から残っていた別の変更が、同じ名前というだけで
        #   **新台のコミットに便乗して公開**できた。
        #   ここで控えておき、公開前の関所が「この公開が作った変更だけか」を
        #   確かめられるようにする。★読めないときは分からないと答える★。
        "dirty_before": _dirty_now(),
        # ★戻し方★ 変える前の中身の指紋
        "restore": {os.path.relpath(k, BASE).replace(os.sep, "/"): _sha(v)
                    for k, v in backup.items() if v is not None},
        # ★作るものの指紋★（消してよいか判断する。人が直していたら消さない）
        # ★これから作るもの★（作る前に残す）
        #   2026-07-31・Codex13回目: 作ってから指紋を書く形だと、
        #   その隙間で落ちたとき「作ったのに目印に無い」残骸ができる。
        "planned": ([f"machines/{slug}/index.html",
                     f"assets/data/machine-details/{slug}.json",
                     f"machines.json#{slug}"]
                    # ★index対象は sitemap にも1行足す★（2026-08-04・Codex72回目）
                    + ([f"sitemap.xml#{slug}"]
                       if _pdz.is_auto(machine)
                       and _pdz.machine_class(machine) == "AUTO_INDEXABLE"
                       else [])),
        "created": {},
        "_why": "この目印がある間は、公開が途中で終わっています。"
                "★目印だけ消してはいけません★ "
                "scripts/publish_new_machine.py --recover で元に戻してください。",
    }
    # ★排他作成★（同時に2つ始まらない）
    tmp = f"{IN_PROGRESS}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline=chr(10)) as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=1) + chr(10))
        f.flush()
        os.fsync(f.fileno())
    try:
        os.link(tmp, IN_PROGRESS)     # 既にあれば失敗する（＝排他）
    except FileExistsError:
        os.remove(tmp)
        raise PublishError("いま別の公開処理が動いているか、前回が途中で終わっています")
    except (OSError, AttributeError):
        # リンクが使えない環境では、存在を確かめてから置く
        if os.path.exists(IN_PROGRESS):
            os.remove(tmp)
            raise PublishError("いま別の公開処理が動いているか、前回が途中で終わっています")
        os.replace(tmp, IN_PROGRESS)
        return
    os.remove(tmp)


def mark_created(created: dict) -> None:
    """★作ったものの指紋を目印に足す★（復旧のとき、消してよいか判断する）"""
    try:
        got = _sj.read_json(IN_PROGRESS, expect=dict)
    except Exception:                     # noqa: BLE001
        return
    got["created"] = {**(got.get("created") or {}), **created}
    write_atomic(IN_PROGRESS, json.dumps(got, ensure_ascii=False, indent=1) + chr(10))


def mark_done() -> None:
    """★全部終わってから消す★（ここまで来て初めて「終わった」）"""
    if os.path.exists(IN_PROGRESS):
        os.remove(IN_PROGRESS)


def unfinished() -> dict:
    """途中で終わった公開が残っていないか。★残っていれば中身を返す★"""
    if not os.path.exists(IN_PROGRESS):
        return {}
    try:
        return _sj.read_json(IN_PROGRESS, expect=dict)
    except Exception:                     # noqa: BLE001
        return {"slug": "(読めません)", "_why": "目印が壊れています"}


def write_atomic(path: str, text: str, new_only: bool = False) -> None:
    """★一時ファイルに完成させてから置き換える★（2026-07-31・Codex指摘2/3）

    最終名へ直接書いていたため、次の2つが起きた。
      ・書き込みの途中で失敗すると、**書きかけのファイル**が最終名に残る
        （この処理が作ったのに指紋が違うので、片付けの対象からも外れていた）
      ・復元も直接書いていたので、失敗すると**元は正常だった早見表が空になる**

    new_only=True は「新しく作る時だけ」。既にあれば作らない。
    """
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8", newline=chr(10)) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())      # ★中身が確実に書けてから置き換える★
        if new_only and os.path.exists(path):
            raise FileExistsError(path)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def check_slug(slug: str) -> list:
    """★書く場所を決める前に、slug そのものを確かめる★"""
    if not isinstance(slug, str) or not _SLUG_OK.match(slug):
        return [f"slug の形が許せません: {slug!r}"
                "（小文字英字で始まり、英数字と_のみ・2〜41文字）"]
    # ★形が合っていても、実際の書き先が machines/ の中か確かめる★（二重の守り）
    root = os.path.realpath(os.path.join(BASE, "machines"))
    for path in (os.path.realpath(os.path.join(BASE, "machines", slug, "index.html")),):
        if os.path.commonpath([root, path]) != root:
            return [f"書き先が machines/ の外を指しています: {slug!r}"]
    return []


def _page_path(slug: str) -> str:
    if check_slug(slug):
        raise PublishError(f"slug が不正です: {slug!r}")
    return os.path.join(BASE, "machines", slug, "index.html")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _existing_pages() -> dict:
    """いま公開中のページの指紋。★1枚も変えていないことを確かめるため★"""
    out = {}
    root = os.path.join(BASE, "machines")
    for slug in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        p = os.path.join(root, slug, "index.html")
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                out[slug] = _sha(f.read())
    return out


def check_before(slug: str, machine: dict, rows: list) -> list:
    """書く前に確かめること。★1つでも引っかかったら書かない★"""
    ng = check_slug(slug)
    if ng:
        return ng
    if not slug or slug != machine.get("slug"):
        ng.append("slug が機種データと合いません")
    if os.path.isfile(_page_path(slug)):
        ng.append(f"{slug} のページは既にあります（この経路は新規作成だけです）")
    if any(m.get("slug") == slug for m in rows):
        ng.append(f"{slug} は既に machines.json にあります（上書きしません）")
    try:
        cls = _pdz.machine_class(machine)
        if cls not in ("AUTO_INDEXABLE", "AUTO_PENDING"):
            ng.append(f"新台経路の区分ではありません（{cls}）"
                      "。この経路は判定書つきの新台だけを公開します")
    except _pdz.DecisionError as e:
        ng.append(f"判定書が壊れています: {e}")
    if machine.get("publish_state") != STATE:
        ng.append(f"publish_state が {STATE} ではありません"
                  f"（{machine.get('publish_state')!r}）")
    return ng


def check_page(slug: str, html: str, expect_noindex: bool = True,
               detail: dict | None = None) -> list:
    """作ったページそのものを確かめる。★テンプレート任せにしない★

    ★2026-07-31・Codexの指摘を再現して3回直した★
      1回目: 本文まるごとの文字列検索 → コメントの noindex で合格していた
      2回目: head の中は見るようにしたが、タグ全体に "noindex" があるかで
             見ていたため `content="index" data-note="noindex"` が合格した
      3回目: 正規表現をやめた。`<div hidden="">` を見逃し、
             `<meta name='robots' content='index'>` を数え落としていた。
             → **HTMLを実際に解析して属性を正規化してから**見る。
    """
    ng = []
    doc = _hc.parse(html)
    robots = _hc.meta_values(doc, "robots")
    if expect_noindex:
        if len(robots) != 1:
            ng.append(f"robots 指定が {len(robots)} 個です（1個であるべきです）")
        else:
            vals = robots[0]
            if "noindex" not in vals:
                ng.append(f"robots が noindex ではありません（{sorted(vals)}）")
            if "index" in vals:
                ng.append("robots に index と noindex が両方あります")
    else:
        # ★index対象（AUTO_INDEXABLE）: robots meta が1個も無いこと★
        #   逆に付いていたら止める（逆方向もfail-closed・Codex72回目）
        if len(robots) != 0:
            ng.append(f"index対象なのに robots 指定が {len(robots)} 個あります"
                      f"（{[sorted(v) for v in robots]!r}）")
    if doc.bases != ["/"]:
        ng.append(f'<base href="/"> が {doc.bases!r} です'
                  "（1個でないとロゴ・ナビが404になります）")
    canon = _hc.link_hrefs(doc, "canonical")
    want = f"https://uchidokoro.com/machines/{slug}/"
    if canon != [want]:
        ng.append(f"canonical が {canon!r} です（{want!r} が1個であるべきです）")
    if "style=" in html:
        ng.append("インラインstyleが入っています")
    # ★時間で嘘になる語の禁止★（2026-08-04・Codex70〜72回目の鮮度ゲート。
    #   導入日を過ぎた瞬間に記事が古くなる語は、日付を問わず最初から書かない）
    for w in _ba.STALE_WORDS:
        if w in html:
            ng.append(f"時間で嘘になる語がページに入っています: {w}")
    # ★先行記事だと読者に分かる表示があるか★（noindexは非公開化ではない）
    #   ★本文のどこかに語があるだけでは認めない★（Codex指摘3）
    #     ひな型のバナーは完成機種のページにも同じ形で入っており、
    #     JavaScriptで表示を切り替えているだけだった。
    #     専用の目印を持ち、隠されていない要素をちょうど1個求める。
    # ★箱ごとに、目印・見出し・本文を結びつけて確かめる★
    #   （2026-08-04・Codex77回目の指摘1/3。以前はページ全体で文言の**個数**を
    #     数えていたので、別の場所に同じ文言を置けば数がそろい、
    #     期待値も検査対象と同じ detail から出していたので自己参照だった）
    #   ★契約（作るべき箱の一覧）は build_new_article の定数＝独立した正本★
    if detail is not None:
        ng += check_pending_boxes(html, detail)
    ng += check_notice_text(html)
    return ng


# ★★ひな型の断り書きと NOTICE_TEXT を突き合わせる★★
#   （2026-08-26・Codex30回目。★前回の直しは半分だった★）
#   machine.html のバナーは既定で隠れている（JSで出す）ので、
#   生成物だけを見る検査は「見える断り書きが0個」で必ず素通りしていた。
#   ＝二重管理の食い違いを、いつまでも見つけられない。
#   ★隠れていても中身は読む★＝ひな型そのものを読んで比べる。
_BANNER_ID = 'id="previewBanner"'


def template_notice_text(template: str) -> str:
    """ひな型のバナーの中の文字（日付の欄は空なので入らない）"""
    i = template.find(_BANNER_ID)
    if i < 0:
        raise PublishError("ひな型に先行記事のバナーがありません")
    j = template.find("</div>", i)
    if j < 0:
        raise PublishError("ひな型のバナーが閉じていません")
    # ★バナーの開始タグの手前から閉じまで★
    start = template.rfind("<", 0, i)
    # ★隠れていても読む★（既定の visible_text は is-hidden を飛ばす＝空になる）
    #   ここで見たいのは「読者にどう見えるか」ではなく
    #   ★ひな型と定数が同じ文言か★なので、隠しの扱いを外す。
    frag = template[start:j + len("</div>")]
    # ★body で包む★（読み取りは body の中しか拾わないので、
    #   切り出した断片のままだと必ず空になる＝対照実験で分かった）
    return _hc.visible_text("<body>" + frag + "</body>", set())


def check_template_notice(template: str) -> list:
    """★ひな型と定数が食い違っていたら止める★（LEGACY_NOTE と同じ扱い）"""
    got = "".join(template_notice_text(template).split())
    want = "".join(NOTICE_TEXT.split())
    if got != want:
        return ["ひな型（machine.html）の断り書きが NOTICE_TEXT と違います: "
                f"ひな型={got[:50]!r} / 定数={want[:50]!r}"]
    return []


def check_notice_text(html: str) -> list:
    """★断り書きが、こちらで決めた文言そのものか★（2026-08-26・Codex29回目）

    ★なぜ要るか★
      `NOTICE_TEXT` はひな型（machine.html）と**二重管理**になっている。
      `LEGACY_NOTE` は食い違うと生成が止まるので気づけるが、
      こちらは**止まらずに黙って食い違う**。
      2026-08-26に実際、ひな型の断り書きから「出典」を落としたとき、
      この検査は何も言わなかった（定数を直したのは私の手作業）。

    ★見るのは「目印のある箱の中身」だけ★
      ページのどこかに同じ文字があればよい、にはしない
      （それは以前 Codex に指摘されて直した型）。
    """
    doc = _hc.parse(html)
    notices = _hc.preview_notices(doc, STATE)
    if not notices:
        return []                      # ★個数は上の検査の担当★
    # ★空白の入れ方は問わない★（2026-08-26）
    #   ★止めたいのは文言の食い違いで、字下げや折り返しではない★。
    #   ひな型で1行に書いてある文を折り返しただけで落ちていた（対照実験で判明）。
    got = ["".join((n.get("text") or "").split()) for n in notices]
    want = "".join(NOTICE_TEXT.split())
    bad = [g for g in got if g != want]
    if bad:
        return [f"断り書きの文言が決めたものと違います: {bad[0][:60]!r}"]
    return []


def check_pending_boxes(html: str, detail: dict) -> list:
    """記事の箱が、契約どおりページに出ているか（構造ごと・順番・中身）。

    ★平坦化した文字の比較をやめる★（2026-08-04・Codex79回目の指摘1）
      以前は「目印のある要素の見える文字」を突き合わせていたので、
      箱のクラスを外す・見出しを span に変える・表を段落に潰す、といった
      **構造の破壊が素通り**した。いまは記事データから描き直した
      セクション群のHTMLと、ページの該当部分を**そのまま**突き合わせる。

    ★ページ内 <style> を許さない★（Codex79回目の指摘2）
      `<style>.article-item{display:none}</style>` を1つ足すだけで
      全部の箱を消せた。ひな型に <style> は無いので、あれば止める。
    """
    ng = []
    # ★並びは build_new_article に聞く★（2026-08-12・依頼160のP0-5）
    want_titles = _ba.expected_titles(detail)
    # ★箱だけの骨組み（本文が空）も止める★（2026-08-04・Codex82回目の指摘2）
    bad = _ba.article_contract_problems(detail)
    if bad:
        return bad
    secs = detail.get("sections") or []
    if re.search("<[" + ' \t\n\r\x0c' + "]*style[" + ' \t\n\r\x0c' + ">/]",
                 html, re.I):
        ng.append("ページに <style> があります（箱ごと隠せるので許しません）")
    # ★描き直した結果と、そのまま突き合わせる★
    want_html = "".join(_bmp.render_section(sec) for sec in secs)
    want_block = f'<div id="articleSections">{want_html}</div>'
    if want_block not in html:
        # どこがどう違うかを短く示す（原文は出さない）
        got = re.search(r'<div id="articleSections">.*?(?=<div class="article-block">)',
                        html, re.S)
        got_titles2 = re.findall(r'data-section="([^"]*)"', got.group(0) if got else "")
        ng.append("記事の箱がページのものと一致しません"
                  f"（ページ側の並び: {got_titles2} / {want_titles} のはず）")
        return ng
    # ★念のため、目印つき要素が余分に無いか★（別の場所に置いた偽物）
    all_marks = re.findall(r'data-section="([^"]*)"', html)
    if all_marks != want_titles:
        ng.append(f"記事の箱の目印が余分にあります（{all_marks}）")
    # ★CSSクラスで隠されていないか★（practical.css から機械的に取り出す）
    try:
        with open(os.path.join(BASE, "assets", "css", "practical.css"),
                  encoding="utf-8") as f:
            hidden_cls = _hc.hidden_classes_from_css(f.read())
    except OSError as e:                  # noqa: BLE001
        return ng + [f"CSSを読めないので表示を確かめられません: {e}"]
    doc = _hc.parse(html, hidden_cls)
    shown = [b["title"] for b in doc.blocks if not b["hidden"]]
    if shown != want_titles:
        ng.append(f"読者に見えている箱がそろっていません（{shown} / "
                  f"{want_titles} のはず）")
    return ng


# 数値らしいかたまり（全角も半角にそろえてから見る）
_NUM = re.compile(r"[0-9][0-9,./]*%?")


def _numbers(text: str) -> set:
    import unicodedata
    t = unicodedata.normalize("NFKC", text or "")
    return {x.rstrip(",./") for x in _NUM.findall(t) if x.rstrip(",./")}


def check_only_allowed_values(slug: str, machine: dict, detail: dict,
                              html: str) -> list:
    """★載せてよい値だけが載っているか★（2026-07-31・Codexの必須条件）

    ひな型だけで描いた結果と見比べ、**この機種のせいで増えた数値**を取り出す。
    それが機種データ・記事データのどこにも無ければ、
    どこかで作られた値ということになるので止める。

    本文だけでなく `<head>`（title・説明・JSON-LD）も含めて丸ごと見る。
    """
    empty_machine = {"slug": slug, "name": machine.get("name", ""),
                     "seo": {"title": ""}, "info": "", "strategy": "",
                     "aliases": [], "release_date": ""}
    if _pdz.is_auto(machine):
        # バナー有無を実物とそろえる（判定書はそのまま使う＝素の描画でも
        # 区分が同じになる。中身の数字は allowed から除外済み）
        # ★版は直書きしない★（2026-08-26・Codex28回目のP0）
        #   ★直す前は v1 固定★＝v2 の機種だと
        #   `publication_policy`(v1) と判定書の版(v2) が食い違う写しになり、
        #   見比べ自体が別物になっていた。
        empty_machine["publication_policy"] = machine["publication_policy"]
        empty_machine["page_decision"] = machine["page_decision"]
    else:
        empty_machine["status"] = "preview"
    try:
        base = render(slug, empty_machine, {"slug": slug, "sections": []})
    except Exception as e:                # noqa: BLE001
        return [f"見比べ用のページを描けません: {type(e).__name__}: {e}"]
    added = _numbers(html) - _numbers(base)
    # ★判定書（decided_at・digest）の数字を「載せてよい数値」に混ぜない★
    #   （2026-08-04・Codex72回目の分析。掲載値の由来ではないため）
    m_for_allowed = {k: v for k, v in machine.items()
                     if k not in ("page_decision", "publication_policy")}
    allowed = _numbers(json.dumps(m_for_allowed, ensure_ascii=False)
                       + json.dumps(detail, ensure_ascii=False))
    stray = sorted(x for x in added if x not in allowed)
    if stray:
        return ["載せる材料に無い数値がページに出ています: "
                + "・".join(stray[:8])]
    return []


# 記事データに入ってよい鍵（★これ以外があれば止める★）
# ★実際の記事データを見て決めた★（2026-07-31・自分の検査が本物を弾いて気づいた）
#   新台: slug / lead / sections / factTable / summaryBoxes / updated
#   既存: それに name / evTable が加わる
_DETAIL_KEYS = {"slug", "name", "lead", "sections", "factTable",
                "summaryBoxes", "evTable", "updated"}
_SECTION_KEYS = {"title", "type", "body", "tables", "rows"}
# ★記事データへ入ってはいけない鍵★（採用しなかったものの置き場）
_FORBIDDEN = ("need_third", "unresolved", "candidates", "thin", "disputed")


_TABLE_KEYS = {"label", "headers", "rows", "note"}
# ★"table" は 2026-08-31 に足した（運営者の要望③の土台）★
_SECTION_TYPES = {"settei", "rumor", "table"}
# 機種データに入ってよい鍵（★新台が作るものだけ★）
_MACHINE_KEYS = {"slug", "name", "seo", "info", "strategy", "aliases",
                 "status", "release_date", "identity", "publish_state",
                 # ★新台経路の判定書★（2026-08-04・Codex71〜72回目）
                 "publication_policy", "page_decision",
                 # ★早見表の材料（2026-08-12）★天井・50枚あたりG数
                 "checker"}


def _is_text(x) -> bool:
    return isinstance(x, str)


def _rows_ok(rows) -> bool:
    """表の中身が「文字の並びの並び」か。"""
    return (isinstance(rows, list)
            and all(isinstance(r, list) and all(_is_text(c) for c in r)
                    for r in rows))


def check_detail(slug: str, detail: dict) -> list:
    """★受け取った記事データそのものを確かめる★（2026-07-31・Codex指摘）

    `build_detail` が正しくても、この関数は任意の記事データを受け取れる。
    直接呼び出し・試験用の呼び出し・将来のつなぎ間違いが別の入口になるので、
    **境界でもう一度、形と型を最後まで**確かめる。

    ★2026-07-31・自分で確かめて分かったこと★
      「配列である」までしか見ていない所が9箇所あり、
      その中に任意の辞書や文字列を入れられた。中まで見る。
    """
    ng = []
    if not isinstance(detail, dict):
        return ["記事データが辞書ではありません"]
    if detail.get("slug") != slug:
        ng.append(f"記事データの slug が {detail.get('slug')!r} です（{slug!r} のはず）")
    stray = sorted(set(detail) - _DETAIL_KEYS)
    if stray:
        ng.append(f"記事データに知らない項目があります: {stray}")
    for key in ("name", "lead", "updated"):
        if key in detail and not _is_text(detail[key]):
            ng.append(f"{key} が文字ではありません")
    if not isinstance(detail.get("sections"), list):
        ng.append("sections が配列ではありません")
    for sec in (detail.get("sections") or []):
        if not isinstance(sec, dict):
            ng.append("節が辞書ではありません")
            continue
        bad = sorted(set(sec) - _SECTION_KEYS)
        if bad:
            ng.append(f"節『{sec.get('title')}』に知らない項目があります: {bad}")
        if not _is_text(sec.get("title")):
            ng.append("節に題がありません")
        if "type" in sec and sec["type"] not in _SECTION_TYPES:
            ng.append(f"知らない節の種類です: {sec.get('type')!r}")
        if "body" in sec and not (isinstance(sec["body"], list)
                                  and all(_is_text(x) for x in sec["body"])):
            ng.append(f"節『{sec.get('title')}』の本文が文字の配列ではありません")
        if "rows" in sec and not _rows_ok(sec["rows"]):
            ng.append(f"節『{sec.get('title')}』の rows が文字の並びではありません")
        # ★★表の節の契約★★（2026-08-31・Codexの10回目）
        #   本文と表が同居すると**表のあとに本文が出る**が、
        #   その順番はどこにも書かれていない。表に添える文は note を使う。
        if sec.get("type") == "table":
            # ★存在で見る★（[] / "" / null も置かせない・Codexの11回目）
            if "body" in sec:
                ng.append(f"節『{sec.get('title')}』は表なので本文は置けません"
                          "（表の note を使います）")
            if "rows" in sec:
                ng.append(f"節『{sec.get('title')}』は表なので rows は使いません")
            if not sec.get("tables"):
                ng.append(f"節『{sec.get('title')}』は表なのに表がありません")
            # ★wide はここでは見ない★＝_TABLE_KEYS が「知らない項目」で
            #   既に断っている。2か所で見ると、片方を壊しても気づけない
        if "tables" in sec and not isinstance(sec["tables"], list):
            ng.append(f"節『{sec.get('title')}』の tables が配列ではありません")
        for tb in (sec.get("tables") or []):
            if not isinstance(tb, dict):
                ng.append("表が辞書ではありません")
                continue
            tbad = sorted(set(tb) - _TABLE_KEYS)
            if tbad:
                ng.append(f"表に知らない項目があります: {tbad}")
            for k in ("label", "note"):
                if k in tb and not _is_text(tb[k]):
                    ng.append(f"表の {k} が文字ではありません")
            if "headers" in tb and not (isinstance(tb["headers"], list)
                                        and all(_is_text(x) for x in tb["headers"])):
                ng.append("表の headers が文字の配列ではありません")
            if not _rows_ok(tb.get("rows")):
                ng.append("表の中身が文字の並びではありません")
            # ★見出しの数と行の列数をそろえる★（2026-07-31・Codex指摘3）
            #   ずれると、正しい値が別の見出しの下に表示される。
            elif isinstance(tb.get("headers"), list):
                w = len(tb["headers"])
                bad_rows = [i for i, r in enumerate(tb["rows"]) if len(r) != w]
                if bad_rows:
                    ng.append(f"表の見出しが {w} 列なのに、"
                              f"{len(bad_rows)} 行の列数が違います")
    # ★中まで見る★（配列であることだけでは、任意の辞書を入れられる）
    for key in ("factTable", "evTable"):
        val = detail.get(key)
        if val is None:
            continue
        if not _rows_ok(val):
            ng.append(f"{key} が文字の並びではありません")
    boxes = detail.get("summaryBoxes")
    if boxes is not None:
        if not isinstance(boxes, list):
            ng.append("summaryBoxes が配列ではありません")
        else:
            for b in boxes:
                if not isinstance(b, dict) or set(b) - {"title", "body", "type"}:
                    ng.append(f"summaryBoxes に知らない形があります: {b!r}"[:120])
                    continue
                # ★配列なら中身まで見る★（2026-07-31・Codex指摘3を再現）
                #   「文字か配列か」で止めていたので、配列の中に辞書を入れられた。
                for k, v in b.items():
                    if _is_text(v):
                        continue
                    if isinstance(v, list) and all(_is_text(x) for x in v):
                        continue
                    ng.append(f"summaryBoxes の {k} が文字でも文字の配列でもありません")
    blob = json.dumps(detail, ensure_ascii=False)
    for word in _FORBIDDEN:
        if chr(34) + word + chr(34) in blob:
            ng.append(f"採用しなかったものの置き場（{word}）が記事データに残っています")
    return ng


_IDENTITY_KEYS = {"manufacturer_id", "official_product_url", "announced_name",
                  "market_release_date", "identity_tier", "regulatory_model_code",
                  "_model_code_sources",
                  # ★1出典しか無い型式の観測値★（2026-08-09・依頼130 P1-2）
                  #   型式を載せているのは P-WORLD だけなので、独立2出典は
                  #   そろわない。記事には出さないが、同定の手がかりとして残す。
                  #   ★ここに足さないと、その機種は公開の関所で必ず止まる★
                  #   （依頼131 P0-2で実際に指摘された）
                  "observed_model_code", "_observed_model_code_sources",
                  # ★どの公式ページで本人性を確かめたか★（2026-08-04・台帳#209）
                  "identity_binding", "identity_evidence_ref",
                  # ★移行前に確かめた記録★（2026-08-16・台帳#376）
                  #   ★検定番号はDMMには無い★ので、ここが唯一の記録になる。
                  #   ★ここに足さないと、育てた瞬間に消える★
                  "_legacy_evidence_ref", "_legacy_official_product_url"}
_RELEASE_OK = re.compile(r"^(20[0-9]{2}-[0-9]{2}(-[0-9]{2})?)?$")


def release_ok(value: str) -> bool:
    """★暦として実在する年月（日）か★（2026-07-31・Codex指摘4を再現）

    形だけ見ていたので `2026-99` や `2026-02-30` が通っていた。
    """
    v = str(value or "")
    if v == "":
        return True
    if not _RELEASE_OK.match(v):
        return False
    from datetime import date
    try:
        if len(v) == 7:
            y, m = int(v[:4]), int(v[5:7])
            date(y, m, 1)               # 月が1〜12かは date が判断する
        else:
            date.fromisoformat(v)       # 日まであるなら暦どおりか
    except ValueError:
        return False
    return True


def check_machine(slug: str, machine: dict) -> list:
    """★機種データそのものを確かめる★（2026-07-31・Codex指摘2）

    知らない項目が混ざれば、そこに書いた文字がページへ出る道になる。
    ★配列・辞書は中まで見る★（「配列である」だけでは任意の辞書を入れられる）
    """
    ng = []
    if not isinstance(machine, dict):
        return ["機種データが辞書ではありません"]
    stray = sorted(set(machine) - _MACHINE_KEYS)
    if stray:
        ng.append(f"機種データに知らない項目があります: {stray}")
    for key in ("name", "info", "strategy", "status", "publish_state"):
        if key in machine and not _is_text(machine[key]):
            ng.append(f"{key} が文字ではありません")
    aliases = machine.get("aliases", [])
    if not (isinstance(aliases, list) and all(_is_text(x) for x in aliases)):
        ng.append("aliases が文字の配列ではありません")
    seo = machine.get("seo")
    if seo is not None:
        if not isinstance(seo, dict) or set(seo) - {"title", "description"}:
            ng.append("seo に知らない項目があります")
        elif not all(_is_text(v) for v in seo.values()):
            ng.append("seo の中身が文字ではありません")
    if not release_ok(machine.get("release_date", "")):
        ng.append(f"release_date が暦として実在しません: "
                  f"{machine.get('release_date')!r}（YYYY-MM か YYYY-MM-DD か空）")
    ident = machine.get("identity")
    if ident is not None:
        if not isinstance(ident, dict):
            ng.append("identity が辞書ではありません")
        else:
            ibad = sorted(set(ident) - _IDENTITY_KEYS)
            if ibad:
                ng.append(f"identity に知らない項目があります: {ibad}")
            for k, v in ident.items():
                if _is_text(v):
                    continue
                # ★配列なら中身まで見る★（Codex指摘3を再現）
                if isinstance(v, list) and all(_is_text(x) for x in v):
                    continue
                ng.append(f"identity.{k} が文字でも文字の配列でもありません")
    # ★狙い目は当サイトの判断なので、この経路では書かせない★
    if machine.get("strategy"):
        ng.append("この経路で狙い目は書けません（strategy は空のはず）")
    # ★本人性を公開の境界でも確かめ直す★（2026-08-04・Codex73回目の指摘5。
    #   上流の add_machine_run では確認しているが、この関数を通る経路は
    #   メーカーもURLも受け取れるので、最後の境界でも見る）
    # ★identity そのものの欠落で検証を素通りできないようにする★
    #   （Codex74回目の指摘4。「辞書なら見る」だけだと、identityを消せば
    #     どの検査も通らずに公開できた＝fail-open）
    if _pdz.is_auto(machine):
        if not isinstance(ident, dict):
            ng.append("新台経路には identity が要ります（本人性を確かめられません）")
        else:
            for k in ("manufacturer_id", "official_product_url",
                      "announced_name"):
                if not (ident.get(k) or "").strip():
                    ng.append(f"identity.{k} がありません（本人性を確かめられません）")
    if isinstance(ident, dict):
        url = ident.get("official_product_url") or ""
        if url and not url.startswith("https://"):
            ng.append(f"公式URLが https ではありません: {url[:60]}")
        # ★slugとURLの対応は slug_binding が唯一の判定箇所★
        #   （2026-08-16・台帳#376／Codex依頼212の指摘5）
        #   DMMへ移した公開済み7機種は `pw_*` のまま公開し続けるので、
        #   素の一致だけでは止まる。増やせない対応表で二択にしている。
        if url:
            import slug_binding as _sb
            _ok, _why = _sb.check(slug, url)
            if not _ok:
                ng.append(f"slug と公式URLが対応していません: {_why}")
        mid = ident.get("manufacturer_id") or ""
        if mid:
            try:
                cats = _sj.read_json(_nwz.CATALOGS, expect=dict)["catalogs"]
            except Exception as e:        # noqa: BLE001
                ng.append(f"メーカー名簿を読めません: {e}")
            else:
                if mid not in cats or not _nwz.is_catalog(cats[mid]):
                    ng.append(f"メーカーが名簿にありません: {mid}")
        ann = ident.get("announced_name")
        if ann and ann != machine.get("name"):
            ng.append(f"公式の発表名と機種名が違います（{ann!r} / "
                      f"{machine.get('name')!r}）")
    return ng


# ★機種数を書いている場所★（2026-07-31・公開後に監査して見つけた）
#   新台を足すと README・運営者情報の「全120機種」がずれる。
#   （一覧ページは機種の行そのものを持つので、数字だけ直すと嘘になる。下の作り直しで扱う）
# ★全体の機種数は表示しない方針になった（2026-07-31）★
#   増減のたびに数を合わせる必要があり、実際に何度もずれた。
#   数字が無いので直す処理も要らない。監査は「書いていないか」を見る。
COUNT_FILES = ()
# ★一覧・ランキングの4ページ★（機種の行を実際に持つ生成物）
HUB_FILES = ("guide-tenjo-ranking.html", "guide-reset-ranking.html",
             "guide-suru-tenjo.html", "guide-ichiran.html")


def count_updates(old_n: int, new_n: int) -> dict:
    """★もう何もしない★（全体の機種数を表示しない方針にしたため）"""
    return {}


def build_hubs() -> dict:
    """いまの machines.json から一覧・ランキング4ページを描く（書き込まない）。

    ★2026-07-31・公開後に自分で監査して見つけた★
      新台を足しても一覧ページは120機種のままだった。
      あのページは**機種の行を実際に持つ生成物**なので、
      件数の数字だけ直すと「121機種」と言いながら120行しかない嘘になる。
    """
    import build_hub_pages as _bhp
    import safe_json as _sj2
    rows = _bhp.load_rows()
    prose = _sj2.read_json(_bhp.PROSE, expect=dict)
    built, _data_html, _allowed = _bhp._build_pages(rows, prose)
    return built


def check_hubs_untouched() -> list:
    """★いまの4ページが、いまのデータから作った物と同じか★

    違えば、誰かの未反映の変更が残っているということ。
    その状態で作り直すと**既存の公開内容まで変えてしまう**ので、この経路は進まない。
    """
    try:
        built = build_hubs()
    except Exception as e:                # noqa: BLE001
        return [f"一覧・ランキングを描けません: {type(e).__name__}: {e}"]
    ng = []
    for rel, html in built.items():
        path = os.path.join(BASE, rel)
        if not os.path.isfile(path):
            ng.append(f"{rel} がありません")
            continue
        with open(path, encoding="utf-8") as f:
            if f.read() != html:
                ng.append(f"{rel} が、いまのデータから作った内容と違います"
                          "（未反映の変更が残っているので、この経路では触りません）")
    return ng


# ★サイト全体の掲載数を表す言い回し★（2026-07-31・全体件数の表示をやめた）
_TOTAL_COUNT_PAT = re.compile(
    r"(全|全部で|掲載|対象機種数[:：]?[ ]*)(<[^>]+>)?[ ]*[0-9]{2,3}[ ]*(</[^>]+>)?[ ]*機種")


def check_counts(new_n: int, slug: str = "") -> list:
    """★早見表が機種データと合っているか★（2026-07-31・Codex指摘2/3）

    以前は「一覧に新台の文字列があるか」しか見ていなかったので、
    既存機種の欠落・余分な行・重複・他3ページの未更新に気づけなかった。
    **載っている機種の集合**で突き合わせ、
    4ページとも「いまのデータから作った内容」と丸ごと一致することを確かめる。
    """
    ng = []
    rows = _sj.read_rows(MACHINES)
    want = [m.get("slug") for m in rows if m.get("slug")]
    path = os.path.join(BASE, "guide-ichiran.html")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            html_i = f.read()
        got = re.findall(r'href="/machines/([a-z0-9_]+)/"', html_i)
        missing = sorted(set(want) - set(got))
        extra = sorted(set(got) - set(want))
        dup = sorted({x for x in got if got.count(x) > 1})
        if missing:
            ng.append(f"一覧に無い機種: {missing[:5]}（全{len(missing)}件）")
        if extra:
            ng.append(f"機種データに無い行: {extra[:5]}（全{len(extra)}件）")
        if dup:
            ng.append(f"一覧に同じ機種の行が複数: {dup[:5]}")
    # ★4ページとも、いまのデータから作った内容と丸ごと同じか★
    ng += check_hubs_untouched()
    # ★全体件数が書き戻されていないか★（表示しない方針）
    for rel in ("README.md", "about.html", "guide-ichiran.html"):
        p2 = os.path.join(BASE, rel)
        if not os.path.isfile(p2):
            continue
        with open(p2, encoding="utf-8") as f:
            m = _TOTAL_COUNT_PAT.search(f.read())
        if m:
            ng.append(f"{rel} にサイト全体の機種数があります（{m.group(0)[:24]!r}）"
                      "。全体件数は表示しない方針です")
    return ng


def run_site_audit(ignore_in_progress: bool = False) -> list:
    """サイト全体の監査を回す。★公開の前後の二段構えにするため★

    （2026-07-31・Codexの助言）
      公開してから監査するだけだと、見つけたときには既に世に出ている。
      置き換える前にも同じ監査を通し、**駄目なら公開しない**。

    ★ignore_in_progress★（2026-07-31・実際に動かして見つけた）
      監査の項目33は「公開中の目印があるか」を見る。
      公開の最終確認は**自分がその目印を持っている最中**に回るので、
      そのままだと必ず引っかかり、**書けた記事を毎回取り消していた**。
      目印を正しく持っている側だけが、この項目を外してよい。
      ★push の関所では絶対に外さない★（そこは残骸を止める場所）。
    """
    # ★★必ず終わらせる★★（2026-08-21・Codexの指摘2）
    #   ★直す前は制限時間が無かった★＝監査が固まると、
    #   公開処理も --recover も**目印を持ったまま止まり続ける**。
    #   すると30分を超えて残骸とみなされ、別の処理に奪われる。
    #   監査は数秒で終わるので、5分あれば十分。
    try:
        r = subprocess.run([sys.executable,
                            os.path.join(BASE, "scripts", "audit_site.py"),
                            "--json"],
                           cwd=BASE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=AUDIT_TIMEOUT_SEC,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except subprocess.TimeoutExpired:
        # ★止まったことを「合格」にしない★
        return [f"監査が {AUDIT_TIMEOUT_SEC} 秒で終わりませんでした"
                "（監査そのものが固まっている可能性があります）"]
    # ★監査そのものが壊れて終わった場合を「合格」にしない★（2026-08-01〜02・Codex23〜24回目）
    #   1回目の直しは「❌の行が無い非0は異常」だったが、
    #   ❌を1行出した**あとに**落ちると素通りする穴が残っていた（Codex24回目）。
    #   JSONで受ければ「完全に出力し終えたか」を機械で判定できる：
    #   途中で落ちた出力はJSONとして読めないか、項目が欠ける。
    try:
        got = json.loads(r.stdout or "")
        if not isinstance(got, dict) or not got:
            raise ValueError("形が違います")
        # ★監査の全項目がそろっているか★（欠け＝監査が途中で終わっている）
        #   ★項目数は audit_site.CHECKS から取る★（2026-08-04・Codex72回目。
        #     1〜33の固定だと、項目を足したときに検査が追随しない）
        nums = {k.split("_", 1)[0] for k in got}
        import audit_site as _as_mod
        want_nums = {k.split("_", 1)[0] for k, _f in _as_mod.CHECKS}
        missing = want_nums - nums
        if missing:
            raise ValueError(f"項目が欠けています: {sorted(missing, key=int)[:5]}")
    except (ValueError, json.JSONDecodeError) as e:
        return ["サイト監査が異常終了しました（監査できていません）: "
                + str(e)[:100] + " / "
                + ((r.stderr or r.stdout or "").strip()[:150] or "出力なし")]
    out = []
    for key, items in got.items():
        if not items:
            continue
        # ★Codexへの報告漏れは公開の可否と関係ない★（開発の作法の話）
        if key.startswith("31_"):
            continue
        # ★説明書（CLAUDE.md）の大きさも公開の可否と関係ない★（2026-08-10）
        #   2026-08-09の夜、パリピ孔明は材料がそろって記事が組めたのに、
        #   **CLAUDE.mdが55.7KBだったという理由だけで公開が止まった**。
        #   これは記事が正しいかどうかとは何の関係もない。
        #   項目23は「私が読む説明書が膨らんでいる」という整理の話なので、
        #   監査には出し続けるが、公開は止めない（31_と同じ扱い）。
        if key.startswith("23_"):
            continue
        # ★お知らせだけの項目は公開を止めない★（2026-08-06）
        #   監査側で決めた「お知らせ」を、こちらでも同じに扱う
        #   （廃止したはずの1500字ルールが公開を止めていた）
        import audit_site as _as_info
        if key in getattr(_as_info, "INFO_ONLY", set()):
            continue
        if ignore_in_progress and key.startswith("33_"):
            continue
        out.append(f"サイト監査: {key}: {len(items)}件 " + str(items[0])[:120])
    return out


def allowed_paths(slug: str, with_sitemap: bool = False) -> set:
    """★この経路が変えてよいファイル★（これ以外が変わっていたら止める）

    with_sitemap は AUTO_INDEXABLE の公開だけ True（無条件に許すと、
    AUTO_PENDING で誤って sitemap を書いた事故を検知できない・Codex72回目）。
    """
    got = {
        f"machines/{slug}/index.html",
        f"assets/data/machine-details/{slug}.json",
        "assets/data/machines.json",
    } | set(COUNT_FILES) | set(HUB_FILES)   # ★件数と一覧の行も整える★
    if with_sitemap:
        got.add("sitemap.xml")
    return got


def changed_paths() -> list:
    """いまリポジトリで変わっているファイル（gitに聞く）。"""
    # ★-z で読む★（2026-07-31・Codexの助言）
    #   ふつうの porcelain は、空白や日本語を含むパスを引用符で囲み、
    #   rename を「旧 -> 新」の1行で出す。素朴に切ると読み違える。
    r = subprocess.run(["git", "status", "--porcelain", "-z"], cwd=BASE,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0:
        raise PublishError(f"git status が失敗しました: {r.stderr[:200]}")
    out = []
    for line in r.stdout.split(chr(0)):
        if len(line) <= 3:
            continue
        path = line[3:].strip()
        if path.endswith("/"):
            # ★gitは新しいフォルダを「フォルダごと1行」で報告する★
            #   （2026-07-31・自分の検査が正しい公開を止めて気づいた）
            #   そのままだと許可リスト（ファイル単位）と突き合わせられないので、
            #   中のファイルに開いてから比べる。
            root = os.path.join(BASE, path.rstrip("/"))
            for dirpath, _dirs, files in os.walk(root):
                for name in files:
                    rel = os.path.relpath(os.path.join(dirpath, name), BASE)
                    out.append(rel.replace(os.sep, "/"))
        else:
            out.append(path)
    return out


def snapshot(paths) -> dict:
    """指定したファイルの中身の指紋。★名前ではなく中身で見るため★"""
    out = {}
    for rel in paths:
        full = os.path.join(BASE, rel)
        if os.path.isfile(full):
            with open(full, "rb") as f:
                out[rel] = hashlib.sha256(f.read()).hexdigest()
        else:
            out[rel] = None
    return out


def check_no_stray_changes(slug: str, before_snap: dict,
                           with_sitemap: bool = False) -> list:
    """★許した3つ以外を書いていないか★（2026-07-31・Codexの条件）

    ★Codex指摘を再現して直した★
      以前は「実行前から変更中だったパス」を名前で除外していたので、
      **もともとdirtyだったCSSをさらに書き換えても見逃した**。
      実行前に取った中身の指紋と突き合わせる。
    """
    ng = []
    allowed = allowed_paths(slug, with_sitemap=with_sitemap)
    now = snapshot(list(before_snap))
    for rel, sha in before_snap.items():
        if rel in allowed:
            continue
        if now.get(rel) != sha:
            ng.append(f"許していないファイルが変わっています: {rel}")
    for rel in changed_paths():
        if rel not in allowed and rel not in before_snap:
            ng.append(f"許していないファイルが増えました: {rel}")
    return ng


def check_sitemap_kept(before_text: str) -> list:
    """★sitemap が1文字も変わっていないこと★（この経路は触らない決まり）

    件数だけ見ていると、同じ件数のまま別のURLへ差し替わっても通る（Codex指摘）。
    """
    with open(SITEMAP, encoding="utf-8") as f:
        now = f.read()
    if now != before_text:
        n0, n1 = before_text.count("<url>"), now.count("<url>")
        return [f"sitemap が変わりました（{n0} → {n1} 件）。この経路は触りません"]
    return []


SITE_ORIGIN = "https://uchidokoro.com"


def _sitemap_locs(text: str) -> list:
    import re as _re
    return _re.findall(r"<loc>([^<]+)</loc>", text)


def sitemap_line(slug: str) -> str:
    """追加する1行（1行形式・生成器 write_sitemap と同じ側に合わせる）。"""
    return f"  <url><loc>{SITE_ORIGIN}/machines/{slug}/</loc></url>"


def add_to_sitemap(before_text: str, slug: str) -> str:
    """★1行形式で </urlset> の直前に1件だけ足す★（復旧は同じ1行の完全一致除去）"""
    line = sitemap_line(slug)
    if line in before_text:
        raise PublishError(f"sitemap に {slug} の行が既にあります")
    marker = "</urlset>"
    if before_text.count(marker) != 1:
        raise PublishError("sitemap の形が想定と違います（</urlset> が1個でない）")
    return before_text.replace(marker, line + chr(10) + marker)


def remove_from_sitemap(text: str, slug: str) -> str:
    """add_to_sitemap が足した1行だけを外す（無ければそのまま返す）。"""
    line = sitemap_line(slug)
    return text.replace(line + chr(10), "", 1)


def check_sitemap_added(before_text: str, slug: str) -> list:
    """★期待した1件だけ増えたこと★（バイト一致でなく<loc>集合で見る・Codex72回目）"""
    with open(SITEMAP, encoding="utf-8") as f:
        now = f.read()
    b, n = _sitemap_locs(before_text), _sitemap_locs(now)
    want = f"{SITE_ORIGIN}/machines/{slug}/"
    ng = []
    if n.count(want) != 1:
        ng.append(f"sitemap に {slug} のURLが {n.count(want)} 件あります（1件のはず）")
    import collections as _c
    if _c.Counter([x for x in n if x != want]) != _c.Counter(b):
        ng.append("sitemap で追加した1件以外のURLが増減・変更されています")
    return ng


def _get_with_retry(url: str, opener=None, tries: int = 3):
    """★つながらなかっただけでは諦めない★（2026-08-22）

    ★なぜ切り出したか★＝直したことを**時間を測らずに**確かめるため。
      最初は「404なら1秒未満で返る」と書いたが、
      ★機械が混んでいると1秒を超えて落ちる★＝また「たまに落ちる検査」を
      作ってしまった。★時間で判定する試験を書かない★。
      いまは「何回呼んだか」を数えて確かめる（下の試験）。

    ★入れ直すのは「つながらない」ときだけ★
      サーバーが答えているもの（404 など＝HTTPError）は、
      何度引いても同じなので繰り返さない。
    """
    import urllib.error
    import urllib.request
    get = opener or urllib.request.urlopen
    last = None
    for i in range(tries):
        try:
            with get(url, timeout=20) as r:
                return r.status, r.read(400000).decode("utf-8", "replace")
        except urllib.error.HTTPError:
            raise                      # ★答えが返っている＝繰り返さない★
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last = e
            if i + 1 < tries:
                time.sleep(0.3 * (i + 1))
    raise last if last else RuntimeError("引けませんでした")


def check_served(slug: str, expect_noindex: bool = True) -> list:
    """★実際にHTTPで返るか確かめる★（ファイルがあるだけでは足りない）

    ローカルの簡易サーバで `/machines/{slug}/` を引き、200 と noindex を見る。
    ★必ずサーバを止める★
    """
    import http.server
    import socketserver
    import threading
    import urllib.error
    import urllib.request

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=BASE)
    try:
        srv = socketserver.TCPServer(("127.0.0.1", 0), handler)
    except OSError as e:
        return [f"確かめ用のサーバを立てられません: {e}"]
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    ng = []
    try:
        url = f"http://127.0.0.1:{port}/machines/{slug}/"
        # ★★つながらなかっただけで落とさない★★（2026-08-22）
        #   ★実際に起きたこと★＝この試験が CI で1回だけ落ち、
        #   同じコミットをやり直したら緑になった（コード側は無関係）。
        #   手元でも、他の処理と並行に走らせたときだけ落ちていた。
        #   ＝★たまに落ちる検査は、本物の赤と見分けが付かなくなる★ので直す。
        #
        #   ★見分ける★＝つながらない（接続の失敗・時間切れ）は入れ直す。
        #   ★中身の判定は1回でも通らなければ失敗のまま★
        #   （200でない・robots が違う、は何度やっても同じなので繰り返さない）。
        status, body = _get_with_retry(url)
        if status != 200:
            ng.append(f"公開したページが HTTP {status} を返します")
        vals = _hc.meta_values(_hc.parse(body), "robots")
        if expect_noindex:
            if len(vals) != 1 or "noindex" not in vals[0]:
                ng.append(f"配信されたHTMLの robots が {vals!r} です（noindex 1個のはず）")
        else:
            if len(vals) != 0:
                ng.append(f"index対象なのに配信HTMLに robots が {vals!r} あります")
    except Exception as e:                # noqa: BLE001
        ng.append(f"公開したページを引けません: {type(e).__name__}: {e}")
    finally:
        srv.shutdown()
        srv.server_close()
    return ng


def check_after(slug: str, before_pages: dict, rows_before: list,
                expect_in_sitemap: bool = False) -> list:
    """書いたあとに確かめること。★取り返しがつくうちに気づくため★"""
    ng = []
    now = _existing_pages()
    for s, h in before_pages.items():
        if s not in now:
            ng.append(f"既存ページが消えました: {s}")
        elif now[s] != h:
            ng.append(f"既存ページが書き換わりました: {s}")
    if slug not in now:
        ng.append(f"{slug} のページができていません")
    rows = _sj.read_rows(MACHINES)
    if not rows or rows[-1].get("slug") != slug:
        ng.append(f"一覧の最後が {slug} ではありません"
                  "（同時に別の書き込みがあった可能性があります）")
    if len(rows) != len(rows_before) + 1:
        ng.append(f"machines.json の件数が {len(rows_before)} → {len(rows)} です（+1のはず）")
    # ★件数だけでは、既存行の書き換えや入れ替わりを見つけられない★
    elif _sha(json.dumps(rows[:-1], ensure_ascii=False, sort_keys=True)) !=             _sha(json.dumps(rows_before, ensure_ascii=False, sort_keys=True)):
        ng.append("machines.json の既存の行が書き換わっています（足すだけのはずです）")
    for m in rows:
        if not os.path.isfile(_page_path(m.get("slug", ""))):
            ng.append(f"一覧に出るのにページがありません: {m.get('slug')}")
    with open(SITEMAP, encoding="utf-8") as f:
        sm_now = f.read()
    if expect_in_sitemap:
        if f"<loc>{SITE_ORIGIN}/machines/{slug}/</loc>" not in sm_now:
            ng.append("index対象なのに sitemap に載っていません")
    else:
        if f"/machines/{slug}/" in sm_now:
            ng.append("sitemap に noindex対象の機種が載っています（載せない決まりです）")
    return ng


# ひな型のバナー（JavaScriptで表示を切り替えている素の形）
_BANNER_HIDDEN = '<div id="previewBanner" class="preview-banner is-hidden">'
# 先行記事として出す形（★JavaScriptが動かなくても見える★＋機械で確かめられる目印）
_BANNER_SHOWN = ('<div id="previewBanner" class="preview-banner" role="note" '
                 + NOTICE + '>')


def render(slug: str, machine: dict, detail: dict) -> str:
    """既存ページと同じ描き方で1枚だけ作る。

    ★2026-07-31・Codex指摘3を確かめて分かったこと★
      先行記事のバナーは、**preview でも完成機種でもHTMLが全く同じ**で、
      JavaScript が `is-hidden` を外して初めて見える作りだった。
      つまり **JSが動かなければ、先行記事だという断りが一切出ない**。
      しかも検査側は「本文のどこかに『先行記事』の語があるか」しか見ていないので、
      完成機種のページでも合格してしまっていた。

      そこでこの経路で作るページだけ、
      **最初から見える形**にし、機械で数えられる目印を付ける。
      （ひな型と描画関数は既存119機種と共通のまま・ここでは差し替えない）
    """
    with open(os.path.join(BASE, "machine.html"), encoding="utf-8") as f:
        _raw_template = f.read()
    # ★★ひな型と NOTICE_TEXT の食い違いは、ここで止める★★
    #   （2026-08-26・Codex30回目。★生成物だけを見る検査は素通りしていた★＝
    #     バナーは既定で隠れているので「見える断り書きが0個」で合格になる）
    _tn = check_template_notice(_raw_template)
    if _tn:
        raise PublishError(_tn[0])
    template = _bmp.prepare_template(_raw_template)
    reasons = _bmp.extract_pochipochi_reasons(template)
    html = _bmp.render_page(template, machine, detail, reasons)
    # ★新台経路はページ全体の断り書きを出さない★（2026-08-04・運営者判断）
    #   未確認の項目は、その項目の場所に「未確認」と書いてある（build_new_article）。
    #   旧preview（既存7機種）はバナーを出す従来の作りのまま。
    if machine.get("status") == "preview":
        if _BANNER_HIDDEN not in html:
            raise PublishError("ひな型の断り書きバナーが見つかりません"
                               "（machine.html の作りが変わった可能性があります）")
        html = html.replace(_BANNER_HIDDEN, _BANNER_SHOWN, 1)
    return html


def publish_from_material(slug: str, name: str, maker: str, official_url: str,
                          release: str, material: dict,
                          apply_it: bool = False, before_write=None,
                          on_written=None, identity_binding: str = "",
                          identity_evidence_ref: str = "") -> dict:
    """★材料から公開まで一気に通す（これが正しい入口）★

    ★2026-07-31・Codex指摘1★
      以前は完成した `machine` / `detail` を受け取っていたので、
      **誰かが作った任意のデータをそのまま公開できた**。
      「出玉率の97.3%」を「CZ期待度97.3%」として渡しても、
      入力のどこかに同じ数値があるため検査を通ってしまう。

      公開の境界で組み立てれば、載る値は
      `build_new_article` が**採用済みの材料からしか作らない**ものに限られる。
    """
    machine = _ba.build_machine(slug, name, maker, official_url, release, material,
                                identity_binding=identity_binding,
                                identity_evidence_ref=identity_evidence_ref)
    detail = _ba.build_detail(slug, name, release, material)
    return _publish_prebuilt(slug, machine, detail, apply_it=apply_it,
                             before_write=before_write, on_written=on_written)


def _publish_prebuilt(slug: str, machine: dict, detail: dict,
                      apply_it: bool = False, before_write=None,
                      on_written=None) -> dict:
    """★内部専用★ 外からは `publish_from_material` を使うこと。

    こちらは完成データを受け取るので、境界の検査でしか守れない。
    コマンドからは呼べないようにしてある（2026-07-31・Codex指摘1）。
    """
    if not apply_it:
        return _publish(slug, machine, detail, apply_it=False)
    with _OnlyOne() as _one:
        # ★★長い工程の合間に「まだ動いている」と伝える★★
        #   （2026-08-21・Codexの指摘2＝touch が誰からも呼ばれていなかった）
        #   これが無いと、監査や検査で30分を超えたときに
        #   **正常に動いているのに残骸とみなされて奪われる**。
        return _publish(slug, machine, detail, apply_it=True,
                        before_write=before_write, on_written=on_written,
                        keepalive=_one.touch)


# ★外から使ってよいのは publish_from_material だけ★（2026-07-31・Codex指摘4）
#   完成データを受け取る経路は名前を _ で始めて、import * でも出さない。
__all__ = ["publish_from_material", "check_page", "check_detail", "check_machine",
           "check_counts", "check_hubs_untouched", "render", "STATE"]


def _publish(slug: str, machine: dict, detail: dict, apply_it: bool = False,
             before_write=None, on_written=None, keepalive=None) -> dict:
    """新台1件を公開する。★ページを先に置き、最後に一覧へ足す★

    ★keepalive★＝「まだ動いている」と目印に伝える呼び出し（省略可）。
      長い工程（監査など）の前後で呼ぶ。★戻り値が False なら
      目印が自分のものでなくなっている★＝そのまま進めない。
    """
    out = {"slug": slug, "problems": [], "wrote": [], "html_bytes": 0}

    def _alive(where: str):
        """★目印を持ったままか確かめる★（持っていなければ止める）"""
        if keepalive is None:
            return
        if not keepalive():
            raise PublishError(
                f"公開の目印が自分のものでなくなりました（{where}）。"
                "別の処理に引き取られた可能性があるので、ここで止めます")

    rows = _sj.read_rows(MACHINES)
    out["problems"] += check_before(slug, machine, rows)
    # ★区分（index対象かどうか）はここで一度だけ決めて全検査に配る★
    try:
        indexable = _pdz.machine_class(machine) == "AUTO_INDEXABLE"
    except _pdz.DecisionError:
        indexable = False      # check_before が既に問題として積んでいる
    out["problems"] += check_detail(slug, detail)
    out["problems"] += check_machine(slug, machine)
    # ★書き始める前にサイトが健全か確かめる★（2026-07-31・Codexの助言・二段構え）
    #   壊れた状態から公開すると、後で「どこまでが自分のせいか」分からなくなる。
    #   ★ページを置いた直後は設計上わざと不整合（一覧にまだ足していない）なので、
    #     その途中では監査しない★
    # ★前回の公開が途中で終わっていないか★（2026-07-31・Codex9回目）
    #   電源断だと、ページも一覧もそろってしまい、監査では区別できない。
    left = unfinished()
    if left:
        out["problems"].append(
            f"★前回の公開が途中で終わっています（{left.get('slug')} / "
            f"{left.get('started_at')}）★ "
            "★目印だけ消してはいけません★"
            "（中途半端な状態のまま『正常』として公開できてしまいます）。"
            "`python scripts/publish_new_machine.py --recover` で元に戻してください")
        return out
    _alive("公開前の監査")
    out["problems"] += run_site_audit()
    _alive("公開前の監査のあと")
    # ★一覧・ランキングが、いまのデータと一致しているか★
    #   ずれたまま作り直すと、既存の公開内容まで変えてしまう。
    for x in check_hubs_untouched():
        out["problems"].append(
            x + "／先に `python scripts/build_hub_pages.py` 相当の作り直しが要ります")
    if out["problems"]:
        return out
    html = render(slug, machine, detail)
    out["html_bytes"] = len(html.encode("utf-8"))   # ★文字数ではなくバイト数★
    out["problems"] += check_page(slug, html, expect_noindex=not indexable,
                                  detail=detail)
    out["problems"] += check_only_allowed_values(slug, machine, detail, html)
    if out["problems"] or not apply_it:
        return out

    # ★ここが最初の書き込みの直前★（2026-07-31・Codex20回目）
    #   「1日1機種」の枠は、前の検査を全部通ってから使う。
    #   手前で使うと、途中公開・監査・早見表のずれで断られたときにも
    #   その日の枠が消えて、**別の正しい機種を公開できなくなる**。
    if before_write and not before_write():
        out["problems"].append("今日の担当ではありません（1日1機種）")
        return out

    before_pages = _existing_pages()
    with open(MACHINES, "rb") as f:
        machines_before = f.read()          # ★戻すときの正本★
    # ★早見表の元の中身を控える★（作り直しに失敗したら戻すため）
    hub_backup = {}
    for rel in HUB_FILES:
        full = os.path.join(BASE, rel)
        if os.path.isfile(full):
            with open(full, encoding="utf-8") as f:
                hub_backup[full] = f.read()
    before_snap = snapshot(changed_paths()
                           + ["sitemap.xml", "index.html", "machine.html",
                              "assets/css/practical.css", "meta-auto.js"])
    with open(SITEMAP, encoding="utf-8") as f:
        before_sitemap = f.read()
    page = _page_path(slug)
    dp = os.path.join(DETAILS, f"{slug}.json")
    made = []          # ★この処理が実際に作ったものだけ★（既存を消さないため）
    # ★目印は、書き始める前に・戻し方つきで★
    backup_for_mark = {**hub_backup, MACHINES: machines_before.decode("utf-8")}
    if indexable:
        backup_for_mark[SITEMAP] = before_sitemap
    mark_start(slug, machine, backup_for_mark)
    machines_replaced = {}   # 一覧を置き換えたか（戻すため・置き換える前に立てる）
    sitemap_replaced = {}    # sitemap を置き換えたか（同上）
    restore_failed = []      # ★戻せなかったもの（あれば目印を消さない）★

    def _cleanup():
        """★自分が作ったものだけ片付ける★（2026-07-31・Codex指摘3を再現して直した）

        以前は「置くはずだった場所」を消していたので、
        **たまたま同名で既にあった記事データを消して**しまい、
        しかも「元に戻しました」と報告していた（実際に再現した）。

        ★片付け切れた時だけ「途中」の目印を消す★（2026-08-03・Codex57回目）
          先に目印を消すと、消せなかった残骸があるのに復旧の手がかりだけ
          失われる。残った時は目印を保持し、残ったパスを問題として残す。
        """
        left = []
        for kind, q, want in reversed(made):
            try:
                if kind == "file" and os.path.isfile(q):
                    # ★自分が書いた中身のままの時だけ消す★（Codex指摘5）
                    with open(q, encoding="utf-8") as fh:
                        if _sha(fh.read()) != want:
                            out["problems"].append(
                                f"作った後に中身が変わっていたので消しませんでした: {q}")
                            left.append(q)
                            continue
                    os.remove(q)
                elif kind == "dir" and os.path.isdir(q):
                    os.rmdir(q)
            except OSError as e:
                out["problems"].append(f"片付けに失敗しました: {q}（{e}）")
                left.append(q)
        if left:
            out["problems"].append(
                "★片付け切れていないため『途中』の目印は残します"
                "（--recover で確かめてください）★")
            return False
        # ★戻せなかったものが1つでもあれば目印を消さない★
        #   （2026-08-04・Codex73回目の指摘2。sitemapや一覧の復元に失敗しても
        #     「作ったファイルさえ消せば終わり」として目印を消していたので、
        #     自動では戻せない中途半端な状態が残った）
        if restore_failed:
            out["problems"].append(
                "★戻せなかったファイルがあるため『途中』の目印は残します"
                f"（{restore_failed[0]}／--recover で確かめてください）★")
            return False
        mark_done()                    # 片付け切れて初めて「途中」ではない
        return True

    try:
        # ① 記事データとページを置く（★この時点では一覧から辿れない★）
        #    "x" で開く＝既にあれば作らずに例外。存在確認との隙間も無くす。
        detail_text = json.dumps(detail, ensure_ascii=False, indent=1) + chr(10)
        # ★やる前に登録する★（2026-07-31・Codex指摘を再現して直した）
        #   以前は「置き換えが済んでから登録」だったので、
        #   os.replace が成功した直後に Ctrl+C が入ると、
        #   **できあがったファイルが片付けの対象にならず残った**（実際に再現）。
        #   write_atomic は一時ファイルを完成させてから置き換えるので、
        #   最終名に「書きかけ」は現れない。だから先に登録して安全。
        if os.path.exists(dp):
            raise FileExistsError(dp)
        made.append(("file", dp, _sha(detail_text)))
        write_atomic(dp, detail_text, new_only=True)
        d = os.path.dirname(page)
        if not os.path.isdir(d):
            made.append(("dir", d, None))
            os.makedirs(d)
        if os.path.exists(page):
            raise FileExistsError(page)
        made.append(("file", page, _sha(html)))
        write_atomic(page, html, new_only=True)
        # ★何を作ったかを目印にも残す★（復旧が「自分の作った物か」を見分ける）
        mark_created({f"machines/{slug}/index.html": _sha(html),
                      f"assets/data/machine-details/{slug}.json":
                          _sha(detail_text)})
    except FileExistsError as e:
        _cleanup()
        raise PublishError(f"同じ名前のファイルが既にあります（触っていません）: {e}")
    except BaseException as e:            # noqa: BLE001
        # ★Ctrl+C や強制終了でも巻き戻す★（2026-07-31・Codex指摘1）
        #   KeyboardInterrupt は Exception ではないので、
        #   以前は途中の状態を残したまま抜けていた。
        _cleanup()
        if isinstance(e, KeyboardInterrupt):
            raise
        raise PublishError(f"公開できませんでした（作ったものは消しました）: {e}")

    # ② ★一覧に足す前に全部確かめる★（2026-07-31）
    #   以前は machines.json まで書いてから確かめていたので、
    #   問題が見つかっても戻せなかった。ここで確かめれば、
    #   駄目なときは置いたファイルを消すだけで完全に元へ戻る。
    late = []
    # ★確かめている最中に例外が出ても片付ける★
    #   （2026-07-31・Codexが勧めた障害注入テストで見つけた）
    #   確認の関数が投げると、そのまま外へ抜けてページと記事データが残っていた。
    try:
        # ★書いたページと記事データが、そのままの中身か★（Codex指摘5）
        for path, want in ((page, _sha(html)), (dp, _sha(detail_text))):
            with open(path, encoding="utf-8") as f:
                if _sha(f.read()) != want:
                    late.append(f"書いたはずの中身と違います: {path}")
        late += check_served(slug, expect_noindex=not indexable)
        late += check_no_stray_changes(slug, before_snap,
                                       with_sitemap=indexable)
        # ★この時点では sitemap はまだ書いていない＝不変のはず★
        late += check_sitemap_kept(before_sitemap)
        now_pages = _existing_pages()
        for s_, h in before_pages.items():
            if s_ not in now_pages:
                late.append(f"既存ページが消えました: {s_}")
            elif now_pages[s_] != h:
                late.append(f"既存ページが書き換わりました: {s_}")
    except BaseException as e:            # noqa: BLE001
        _cleanup()
        if isinstance(e, KeyboardInterrupt):
            raise
        raise PublishError(f"確かめの最中に失敗しました（作ったものは消しました）: {e}")
    if late:
        # ★目印は _cleanup が「片付け切れた時だけ」消す★（2026-08-03・
        #   Codex60回目。ここで無条件に mark_done すると、ページ削除だけ
        #   失敗した時に残骸があるのに復旧の目印が消えた）
        if _cleanup():
            out["problems"] += late
            out["problems"].append(
                "★確かめで引っかかったので、置いたものを消して元に戻しました★")
        else:
            out["problems"] += late
        return out

    # ③ ここで初めて一覧へ足す（★これ以降トップページからリンクされる★）
    try:
        rows = _sj.read_rows(MACHINES)        # ★直前に読み直す★（競合対策）
        if any(m.get("slug") == slug for m in rows):
            _cleanup()
            out["problems"].append("書いている間に同じ機種が一覧へ入りました（やり直してください）")
            return out
        rows.append(machine)
        # ★一覧を置き換える前に「戻し方」を登録する★
        #   （2026-07-31・Codex指摘を再現：置き換え直後に中断すると戻らなかった）
        machines_replaced["yes"] = True
        write_atomic(MACHINES, json.dumps(rows, ensure_ascii=False, indent=1) + chr(10))
        # ★足した行の指紋も残す★（2026-07-31・Codex12回目）
        #   ページと同じで、人が直した行を巻き添えで消さないため。
        mark_created({f"machines.json#{slug}":
                      _sha(json.dumps(machine, ensure_ascii=False, sort_keys=True))})
        out["wrote"] = [dp, page, MACHINES]
        # ★index対象は sitemap にも1行足す★（2026-08-04・Codex72回目。
        #   1行形式・</urlset> 直前・復旧は同じ1行の完全一致除去）
        if indexable:
            # ★書く前に目印へ登録する★（2026-08-04・Codex73回目の指摘2。
            #   書いた直後に落ちると、復旧側は created に無いので外せなかった。
            #   足す行は決まった1行なので、書く前に指紋を出せる）
            mark_created({f"sitemap.xml#{slug}": _sha(sitemap_line(slug))})
            sitemap_replaced["yes"] = True
            write_atomic(SITEMAP, add_to_sitemap(before_sitemap, slug))
            out["wrote"].append(SITEMAP)
        # ★機種数の表記も同時に直す★（ここまで来たら一緒に整える）
        #   直せなくても公開は成立しているので、失敗は問題として残すだけにする。
        for rel, text in count_updates(len(rows) - 1, len(rows)).items():
            try:
                full = os.path.join(BASE, rel)
                with open(full, "w", encoding="utf-8", newline=chr(10)) as f:
                    f.write(text)
                out["wrote"].append(full)
            except OSError as e:
                out["problems"].append(f"機種数の表記を直せませんでした（{rel}）: {e}")
        # ★一覧・ランキングを作り直す★（行そのものを持つので数字だけでは足りない）
        #   ★全部そろってから一気に置き換える★（2026-07-31・Codex指摘1）
        #     1枚ずつ直接上書きしていたので、途中で失敗すると
        #     「1枚目だけ新台が載っている」ちぐはぐな状態が残った。
        #     書きかけのHTMLが配信される恐れもあった。
        tmps, swapped = [], []
        try:
            new_hubs = build_hubs()                      # ①全部メモリで作る
            # ★4ページそろっているか★（生成器が減らしても気づける・Codex指摘4）
            if set(new_hubs) != set(HUB_FILES):
                raise PublishError(
                    f"早見表が {sorted(new_hubs)} しか作られませんでした"
                    f"（{sorted(HUB_FILES)} のはず）")
            for rel, html2 in new_hubs.items():
                full = os.path.join(BASE, rel)
                tmp2 = full + f".new.{os.getpid()}"
                with open(tmp2, "w", encoding="utf-8", newline=chr(10)) as f:
                    f.write(html2)
                    f.flush()
                    os.fsync(f.fileno())
                tmps.append((tmp2, full))
            for tmp2, full in tmps:                      # ②一気に置き換える
                swapped.append(full)                     # ★やる前に登録★
                os.replace(tmp2, full)
                out["wrote"].append(full)
        except BaseException as e:        # noqa: BLE001  ★Ctrl+Cでも戻す★
            for tmp2, _f in tmps:
                if os.path.exists(tmp2):
                    os.remove(tmp2)
            # ★戻す途中で失敗しても、残りを戻し続ける★（Codexの助言）
            failed = []
            for full in swapped:
                text0 = hub_backup.get(full)
                if text0 is None:
                    continue
                try:
                    write_atomic(full, text0)
                except Exception as e2:       # noqa: BLE001
                    failed.append(f"{os.path.basename(full)}: {e2}")
            out["problems"].append(f"一覧・ランキングを作り直せませんでした（元に戻しました）: {e}")
            if failed:
                out["problems"].append(
                    "★戻せなかったファイルがあります（人が確かめてください）: "
                    + " / ".join(failed) + "★")
            if isinstance(e, KeyboardInterrupt):
                raise
    except BaseException as e:            # noqa: BLE001  ★Ctrl+Cでも戻す★
        if machines_replaced.get("yes"):
            try:
                write_atomic(MACHINES, machines_before.decode("utf-8"))
            except Exception:             # noqa: BLE001
                restore_failed.append("machines.json")
                out["problems"].append("★一覧を戻せませんでした（人が確かめてください）★")
        if sitemap_replaced.get("yes"):
            try:
                write_atomic(SITEMAP, before_sitemap)
            except Exception:             # noqa: BLE001
                restore_failed.append("sitemap.xml")
                out["problems"].append("★sitemapを戻せませんでした（人が確かめてください）★")
        _cleanup()
        if isinstance(e, KeyboardInterrupt):
            raise
        raise PublishError(f"一覧に足せませんでした（作ったものは消しました）: {e}")

    # ④ 一覧に足したあとの最終確認
    late2 = check_after(slug, before_pages, rows[:-1],
                        expect_in_sitemap=indexable)
    late2 += (check_sitemap_added(before_sitemap, slug) if indexable
              else check_sitemap_kept(before_sitemap))
    # ★終わったあとにもう一度★
    #   ここは自分が「公開中」の目印を持っている最中なので、項目33だけ外す。
    #   （外さないと、書けた記事を毎回自分で取り消していた・実機で判明）
    _alive("最後の監査")
    late2 += run_site_audit(ignore_in_progress=True)
    _alive("最後の監査のあと")
    late2 += check_counts(len(rows), slug)
    with open(page, encoding="utf-8") as f:          # ★最後にもう一度★
        if _sha(f.read()) != _sha(html):
            late2.append(f"一覧へ足した後にページの中身が変わっています: {page}")
    if late2:
        # ★戻せるときだけ戻す★（2026-07-31・Codexの助言）
        #   いま置いてある中身が「自分が書いたもの」と同じ時にだけ戻す。
        #   違っていれば誰かが触っているので、上書きせず知らせる。
        mine = _sha(json.dumps(rows, ensure_ascii=False, indent=1) + chr(10))
        with open(MACHINES, encoding="utf-8") as f:
            now_text = f.read()
        if _sha(now_text) == mine:
            write_atomic(MACHINES, machines_before.decode("utf-8"))
            if sitemap_replaced.get("yes"):
                # ★自分が書いた1行のままの時だけ戻す★（他人の変更を消さない）
                with open(SITEMAP, encoding="utf-8") as f:
                    sm_now2 = f.read()
                if _sha(sm_now2) == _sha(add_to_sitemap(before_sitemap, slug)):
                    write_atomic(SITEMAP, before_sitemap)
                else:
                    restore_failed.append("sitemap.xml")
                    late2.append("★sitemapに別の変更が入っているため自動では"
                                 "戻しませんでした（人が確かめてください）★")
            for full, text0 in hub_backup.items():       # ★早見表も戻す★
                if text0 is not None:
                    write_atomic(full, text0)
            # ★目印は _cleanup が「片付け切れた時だけ」消す★（Codex60回目）
            if _cleanup():
                out["wrote"] = []
                late2.append("★一覧から外し、置いたものを消して元に戻しました★")
            else:
                out["wrote"] = []
        else:
            late2.append("★別の書き込みが入っているため、自動では戻しませんでした★"
                         "（人が確かめてください）")
        out["problems"] += late2
        return out
    # ★「途中」の目印を消す前に、次の担当へ引き継ぐ★（2026-07-31・Codex22回目）
    #   ここで消してから呼び出し元が push待ちの目印を作っていたので、
    #   その間に止まると「公開ファイルはあるが目印はどこにも無い」状態になった。
    #   翌日は何も復旧できず、機種は『既に登録』と判定されて待ち行列から消え、
    #   残った変更が後続のpushも塞いでいた。
    if on_written:
        try:
            on_written(slug)
        except Exception as e:            # noqa: BLE001
            out["problems"].append(f"引き継ぎに失敗しました（pushしないでください）: {e}")
            return out
    mark_done()                        # ★ここまで来て初めて「終わった」★
    return out


def _find_stale_held(full: str, want: str):
    """★前回の復旧が残した退避物（*.recover.<旧PID>）を探す★（Codex62回目）

    返すもの: (指紋が一致した退避物のパス or None, 指紋が合わない退避物のリスト)
    一致しない退避物は「人が確かめる」対象（触らない）。
    """
    import glob as _glob
    hit, bad = None, []
    for o in sorted(_glob.glob(full + ".recover.*")):
        if not os.path.isfile(o):
            continue
        try:
            with open(o, encoding="utf-8") as f:
                if _sha(f.read()) == want:
                    if hit is None:
                        hit = o
                    else:
                        bad.append(o)     # 一致が2つ＝想定外。人へ
                    continue
        except OSError:
            pass
        bad.append(o)
    return hit, bad


RECOVER_LOCK = os.path.join(BASE, ".recover.lock")


def recover(apply_it: bool = False) -> dict:
    """★復旧も同時に2つ走らせない★（2026-07-31・Codex13回目）

    目印の存在は「新しい公開」を止めるが、
    同じ目印を読んで動く復旧処理どうしは止めない。
    2つが同時に「指紋が一致した」と判断して消しに行ける。
    """
    if not apply_it:
        return _recover(apply_it=False)
    # ★公開と復旧は同じロックでも排他する★（2026-08-03・Codex61回目）
    #   公開が mark_start() の直後（ファイル作成前）の隙間に復旧が走ると、
    #   「created が空＝何も作られていない」と判断して目印を消し、
    #   進行中の公開が目印なしになる。公開のロックを先に取れば、
    #   公開中の復旧・復旧中の公開はどちらも待たされて成立しない。
    with _OnlyOne():
        with _OnlyOne(RECOVER_LOCK):
            return _recover(apply_it=True)


def _recover(apply_it: bool = False) -> dict:
    """★途中で終わった公開を、処理前の状態に戻す★（2026-07-31・Codex10〜11回目）

    ★これは「厳密に元へ戻す」ではなく「今回の追加を打ち消す」処理★
      早見表は生成物なので、いまのデータから作り直せば整合します。
      ただし「処理前とバイト単位で同じ」ではありません（生成器が変われば変わる）。

    ★人が直したものは消さない★（Codex11回目）
      作ったときの指紋と違えば、誰かが手を入れたということなので、
      消さずに知らせて止まります。

    ★何度走らせても平気★ 既に片付いているものは「済んでいる」と扱います。
    """
    left = unfinished()
    out = {"slug": left.get("slug"), "problems": [], "restored": [],
           "todo": [], "kept": []}
    if not left:
        out["problems"].append("途中で終わった公開はありません")
        return out
    slug = left.get("slug") or ""
    created = left.get("created") or {}
    if not slug or not _SLUG_OK.match(slug):
        out["problems"].append(
            f"目印から機種名を読めません（{slug!r}）。手で確かめてください")
        return out
    planned = left.get("planned") or []
    if not created and planned:
        # ★作る前に落ちた場合★（目印に指紋が無い）
        #   planned に載っているものが実際にあるなら、
        #   それは「作ったが指紋を書く前に落ちた」もの。中身は分からないので人へ。
        stuck = [rel for rel in planned
                 if not rel.startswith("machines.json#")
                 and os.path.isfile(os.path.join(BASE, rel))]
        if stuck:
            out["problems"].append(
                "★作ったものの指紋が残る前に止まっています。"
                "中身が正しいか人が確かめてください★")
            for rel in stuck:
                out["problems"].append(f"  確かめる: {rel}")
            return out
        # 何も作られていないなら、目印を消すだけで元通り
        out["todo"].append("何も作られていないので、目印を消すだけです")
        if apply_it:
            mark_done()
            out["restored"].append("（目印を消しました）")
            _clear_stale_push_marker(slug, out)
        return out
    if not created:
        # ★目印が壊れていても、作られうる物は決まっている★
        #   指紋が無いので「自分が作った物か」は判断できない。
        #   その場合は消さずに、何を確かめるべきかだけ知らせる。
        out["problems"].append(
            "目印に『作ったものの指紋』がありません（壊れているか、"
            "作る前に止まった可能性）。下のファイルを人が確かめてください")
        for rel in (f"machines/{slug}/index.html",
                    f"assets/data/machine-details/{slug}.json"):
            if os.path.isfile(os.path.join(BASE, rel)):
                out["problems"].append(f"  確かめる: {rel}")
        rows0 = _sj.read_rows(MACHINES)
        if any(m.get("slug") == slug for m in rows0):
            out["problems"].append(f"  確かめる: 一覧に {slug} が入っています")
        return out

    # ★目印に書かれたパスをそのまま信用しない★（2026-07-31・Codex12回目）
    #   目印が書き換えられていたら、関係ないファイルを消しに行ける。
    allowed_created = {f"machines/{slug}/index.html",
                       f"assets/data/machine-details/{slug}.json",
                       f"machines.json#{slug}",
                       # ★index対象の公開は sitemap の1行も作る★（Codex72回目）
                       f"sitemap.xml#{slug}"}
    stray = sorted(set(created) - allowed_created)
    if stray:
        out["problems"].append(
            f"★目印に知らないファイルが入っています: {stray[:3]}。"
            "触らずに止めました。人が確かめてください★")
        return out

    # ★消す前に、一覧の行も先に確かめる★（2026-08-03・Codex57回目）
    #   ページ・詳細を消した後に一覧の行の食い違いで止まると、
    #   「記事は消えたのに一覧に行だけ残る」中途半端な状態を自分で作る。
    #   全部を確かめてから、初めて消し始める（全か無か）。
    rows_pre = _sj.read_rows(MACHINES)
    hit_pre = [i for i, m in enumerate(rows_pre) if m.get("slug") == slug]
    if len(hit_pre) > 1:
        out["problems"].append(
            f"★一覧に {slug} が {len(hit_pre)} 件あります。何も消さずに"
            "止めました。手で確かめてください★")
        return out
    if hit_pre:
        want_row_pre = (created or {}).get(f"machines.json#{slug}")
        now_row_pre = _sha(json.dumps(rows_pre[hit_pre[0]],
                                      ensure_ascii=False, sort_keys=True))
        if want_row_pre and now_row_pre != want_row_pre:
            out["kept"].append(f"machines.json#{slug}")
            out["problems"].append(
                f"★一覧の {slug} の行が、足したときと中身が違います"
                "（誰かが直した可能性）。ページ・詳細も含め何も消さずに"
                "止めました。人が確かめてください★")
            return out

    # ① 作ったものを消す（★自分が作った中身のままの時だけ★）
    #   ★確かめてから消すまでの隙間をなくす★（2026-07-31・Codex13回目）
    #     「読む→一致→消す」の間に人が直すと、その編集ごと消える。
    #     先に別名へ動かしてしまえば、以降の編集は別のファイルに向かうので、
    #     動かしたものを確かめて消せば取り違えない。
    # ★全部を確保・検証してから、初めて消す（全か無か）★（2026-08-03・
    #   Codex58回目。1件ずつ検証・削除すると、2件目の指紋違いで止まった時
    #   1件目だけが消えており、404や欠損記事を自分で作っていた）
    held_map, grab_fail = [], False
    for rel, want in created.items():
        if rel.startswith(("machines.json#", "sitemap.xml#")):
            continue                      # 一覧の行は上と②・sitemapの行は②bで扱う
        full = os.path.join(BASE, rel)
        # ★前回の復旧が残した退避物（旧PID名）を再接続する★
        #   （2026-08-03・Codex62回目。巻き戻しの復元に失敗すると
        #     *.recover.<旧PID> のまま残り、元パスが無いため以後の復旧が
        #     何度走っても見つけられず、新台公開が恒久停止した。
        #     指紋が一致した退避物だけを自分の held_map に引き取る）
        _old_hit, _old_bad = _find_stale_held(full, want)
        if _old_bad:
            out["problems"].append(
                f"★{rel} の退避物が残っていますが、作った時の指紋と合いません: "
                f"{_old_bad[0]}。触らずに止めました。人が確かめてください★")
            grab_fail = True
            break
        if not os.path.isfile(full):
            if _old_hit:
                out["todo"].append(f"消す: {rel}（前回の退避物を引き取り）")
                if apply_it:
                    held_map.append((rel, full, _old_hit, want))
            continue                      # 既に片付いている（何度走らせても平気）
        if _old_hit:
            # 元パスも退避物もある＝退避物は自分の複製（指紋一致）なので消してよい
            out["todo"].append(f"消す: {_old_hit}（自分の複製）")
            if apply_it:
                try:
                    os.remove(_old_hit)
                except OSError as e:      # noqa: BLE001
                    out["problems"].append(f"複製を消せませんでした: {e}")
                    grab_fail = True
                    break
        out["todo"].append(f"消す: {rel}")
        if not apply_it:
            continue
        held = f"{full}.recover.{os.getpid()}"
        try:
            os.replace(full, held)        # ★先に確保する（原子的）★
        except OSError as e:
            out["problems"].append(f"{rel} を確保できませんでした: {e}")
            grab_fail = True
            break
        held_map.append((rel, full, held, want))
    if apply_it:
        bad = []
        if not grab_fail:
            for rel, full, held, want in held_map:
                with open(held, encoding="utf-8") as f:
                    if _sha(f.read()) != want:
                        bad.append(rel)
    def _undo_held() -> bool:
        """退避したファイルを全部原位置へ戻す（何も消さなかったことにする）。

        ★成否を返す★（2026-08-03・Codex62回目）。戻せなかった退避物は
        旧PID名のまま残るが、次の復旧が指紋一致で引き取る（上の再接続）。
        """
        ok_all = True
        for rel_, full_, held_, _w2 in held_map:
            try:
                os.replace(held_, full_)
            except OSError as e:          # noqa: BLE001
                ok_all = False
                out["problems"].append(
                    f"★{rel_} を戻せませんでした（{held_} に退避したまま・"
                    f"次の --recover --apply が引き取ります）: {e}★")
        return ok_all

    if apply_it:
        if grab_fail or bad:
            _undo_held()
            for rel in bad:
                out["kept"].append(rel)
                out["problems"].append(
                    f"★{rel} は作ったときと中身が違います（誰かが直した可能性）。"
                    "何も消さずに止めました。人が確かめてください★")
            return out
        # ★退避物はまだ消さない★（2026-08-03・Codex59回目）
        #   一覧の行・早見表・監査まで成功した最後に消す。
        #   途中の失敗では _undo_held() で全部戻せるようにしておく
        #   （消してしまうと、目印に本文が無いので自動では戻せない）。
        for rel, full, held, _w_ in held_map:
            out["restored"].append(rel)
    if out["kept"]:
        return out                        # ★1つでも判断がつかなければ進まない★

    # ② 一覧から今回の1件だけを外す（★同じslugの行だけ・1件だけ★）
    rows = _sj.read_rows(MACHINES)
    with open(MACHINES, encoding="utf-8") as f:
        machines_text_before = f.read()   # ★失敗したら戻すための正本★
    hit = [i for i, m in enumerate(rows) if m.get("slug") == slug]
    if len(hit) > 1:
        _undo_held()
        out["problems"].append(
            f"★一覧に {slug} が {len(hit)} 件あります。何も消さずに戻しました。"
            "手で確かめてください★")
        return out
    if hit:
        # ★行の中身が作ったときと同じ時だけ外す★（2026-07-31・Codex12回目）
        #   同名が1件かどうかだけ見ていたので、
        #   あとから人が足した別名や狙い目ごと消していた（実際に再現）。
        want_row = (created or {}).get(f"machines.json#{slug}")
        now_row = _sha(json.dumps(rows[hit[0]], ensure_ascii=False, sort_keys=True))
        if want_row and now_row != want_row:
            _undo_held()
            out["kept"].append(f"machines.json#{slug}")
            out["problems"].append(
                f"★一覧の {slug} の行が、足したときと中身が違います"
                "（誰かが直した可能性）。何も消さずに戻しました。"
                "人が確かめてください★")
            return out
        out["todo"].append(f"一覧から外す: {slug}")
        if apply_it:
            del rows[hit[0]]
            write_atomic(MACHINES, json.dumps(rows, ensure_ascii=False,
                                              indent=1) + chr(10))
            out["restored"].append("assets/data/machines.json")

    # ②b sitemap から今回の1行だけを外す（★足した行そのもの・1行だけ★）
    with open(SITEMAP, encoding="utf-8") as f:
        sitemap_text_before = f.read()    # ★失敗したら戻すための正本★
    sm_replaced = {}
    smap_key = f"sitemap.xml#{slug}"
    if smap_key not in (created or {}) and sitemap_line(slug) in sitemap_text_before:
        # ★目印に無いのに sitemap に行がある＝説明のつかない状態★
        #   （黙って残すと noindex と矛盾したまま公開が続く）
        _undo_held()
        out["kept"].append("sitemap.xml")
        out["problems"].append(
            "★sitemap に この機種の行がありますが、目印に記録がありません。"
            "何も消さずに戻しました。人が確かめてください★")
        return out
    if smap_key in (created or {}):
        line = sitemap_line(slug)
        if _sha(line) != created[smap_key]:
            _undo_held()
            out["kept"].append(smap_key)
            out["problems"].append(
                "★sitemap の行の指紋が、足したときの記録と合いません。"
                "何も消さずに戻しました。人が確かめてください★")
            return out
        if line in sitemap_text_before:
            out["todo"].append(f"sitemap から外す: {slug}")
            if apply_it:
                sm_replaced["yes"] = True
                write_atomic(SITEMAP,
                             remove_from_sitemap(sitemap_text_before, slug))
                out["restored"].append("sitemap.xml")
        # 無ければ既に片付いている（何度走らせても平気）

    def _undo_all():
        """一覧の行・sitemap・退避物を元へ戻し、早見表も元データで作り直す。

        ★退避物を最初に戻す★（2026-08-03・Codex61回目）
          一覧の書き戻しが先だと、そこで失敗した時に退避物が
          退避名のまま残り、次の復旧が見つけられなかった。
        """
        _undo_held()
        write_atomic(MACHINES, machines_text_before)
        if sm_replaced.get("yes"):
            write_atomic(SITEMAP, sitemap_text_before)
        try:
            for rel_, html_ in build_hubs().items():
                full_ = os.path.join(BASE, rel_)
                with open(full_, encoding="utf-8") as f_:
                    if f_.read() != html_:
                        write_atomic(full_, html_)
        except Exception as e:            # noqa: BLE001
            out["problems"].append(f"★早見表を元に戻せませんでした: {e}★")

    # ③ 早見表を、いまのデータから作り直す
    if apply_it:
        try:
            for rel, html in build_hubs().items():
                full = os.path.join(BASE, rel)
                with open(full, encoding="utf-8") as f:
                    same = (f.read() == html)
                if not same:
                    write_atomic(full, html)
                    out["restored"].append(rel)
        except Exception as e:            # noqa: BLE001
            _undo_all()
            out["problems"].append(
                f"★早見表の作り直しに失敗したため、全部元に戻しました: {e}★")
            return out
    else:
        out["todo"].append("早見表4ページを作り直す")

    if apply_it:
        # ★★空になった機種ディレクトリは、監査より先に消す★★
        #   （2026-08-24・Codexの19回目の指摘4を直していて見つけた**本物の不具合**）
        #   ★直す前は監査のあとに消していた★ので、
        #     ①強制終了で公開が中断される
        #     ②`--recover` がファイルは消すが、空のディレクトリは残ったまま
        #     ③直後の監査が「孤児ディレクトリ」で落ちる
        #     ④復旧が**自分の後始末を全部取り消して**目印を残す
        #     ⑤何度やり直しても同じ＝★目印が永久に残る★
        #     ⑥目印がある間は新台の公開が全部止まる（誰にも通知されない）
        #   ★試験が空だったので、一度も見つからなかった★
        #     （「対象を作れなかったら合格」になっていた）
        _emptied = None
        _d0 = os.path.join(BASE, "machines", slug)
        if os.path.isdir(_d0) and not os.listdir(_d0):
            try:
                os.rmdir(_d0)
                _emptied = _d0
            except OSError:               # noqa: PERF203
                _emptied = None
        # ★★退避中のファイルは「まだある物」として数えない★★
        #   （2026-08-24・同じ不具合の2つ目の顔）
        #   ★退避先はその機種ディレクトリの中★（`index.html.recover.1234`）
        #   なので、監査から見ると**常に孤児ディレクトリが残っている**。
        #   ＝強制終了のあと、復旧は**何度やっても自分の監査に落ちる**。
        #   ★これは「例外を作る」のではなく、
        #     この処理自身の作業用ファイルを勘定に入れないという話★
        #     （項目33を外しているのと同じ理由）。
        #   ★パスの区切りをそろえてから比べる★＝目印の中は "machines/x/…"
        #     なので、Windowsでは "\\" と "/" が混ざって一致しない
        #     （直す前はここが常に空集合になり、この守りが効いていなかった）
        _held_names = {os.path.basename(h) for _r, _f, h, _w in held_map
                       if os.path.normpath(os.path.dirname(h))
                       == os.path.normpath(_d0)}
        _in_dir = set(os.listdir(_d0)) if os.path.isdir(_d0) else set()
        _only_held = bool(_in_dir) and _in_dir <= _held_names
        # ★戻し終わったか確かめてから目印を消す★
        #   監査の項目33は「目印がある＝途中」を見るので、
        #   消す前に回すと自分の目印を自分で見つけて永久に詰まる。
        # ★★この処理自身の作業用のものを、勘定に入れない★★
        #   ①項目33＝「途中の目印がある」（昔から除外している）
        #   ②項目52＝同じ目印を別の言い方で見つける（2026-08-24に新設した）
        #     ★除外を足し忘れたので、復旧が自分の目印を見て自分を止めた★
        #   ③項目6・52＝退避中のファイルがその機種ディレクトリに残っている
        #   ★これは例外リストではない★＝まだ後始末の途中で、
        #     どれもこの処理が最後に消すもの。人が置いた物は1つも外さない。
        _mine = [".publish-in-progress.json"]
        _dirmark = os.path.join("machines", slug)

        def _about_mine(line: str) -> bool:
            _l = line.replace("/", os.sep)
            if "33_" in line:
                return True
            if "52_" in line and slug in line and any(m in line for m in _mine):
                return True
            if _only_held and _dirmark in _l and ("6_" in line or "52_" in line):
                return True
            return False

        ng = [x for x in run_site_audit() if not _about_mine(x)]
        if ng:
            # ★消したディレクトリも戻してから巻き戻す★（何もしなかった形へ）
            if _emptied:
                try:
                    os.makedirs(_emptied, exist_ok=True)
                except OSError:           # noqa: PERF203
                    pass
            _undo_all()
            out["problems"] += ng
            out["problems"].append(
                "★戻したあとの監査に落ちたため、全部元に戻しました。"
                "目印は消しません★")
            return out
        # ★一覧・早見表・監査まで成功して、初めて退避物を消す★
        #   （2026-08-03・Codex59回目。先に消すと、後段の失敗で
        #     「先に消したファイルだけ自動復元できない」中途半端が残った）
        _del_fail = []
        for rel, full, held, _w_ in held_map:
            try:
                os.remove(held)
            except OSError:               # noqa: BLE001
                # ★消せなかった退避物は元パスへ戻す★（2026-08-03・Codex60回目）
                #   退避名（*.recover.<pid>）のまま残すと、目印に場所が
                #   書かれていないため、次の --recover では見つからず
                #   （元パスが無い＝held_mapが空）、目印だけ消えて
                #   未追跡ファイルが後続のpushを全部止めた。
                #   元パスへ戻せば、次の --recover が普通にやり直せる。
                try:
                    os.replace(held, full)
                    _del_fail.append(f"{rel}（元パスへ戻しました）")
                except OSError as e2:     # noqa: BLE001
                    _del_fail.append(f"{held}（戻せず退避名のまま: {e2}）")
        if _del_fail:
            out["problems"].append(
                "★退避物を消せませんでした（目印は残します・もう一度 "
                "--recover --apply でやり直せます）: "
                + " / ".join(_del_fail)[:200] + "★")
            return out
        # ★上で消せなかった時のための念のため★（普通はもう無い）
        d = os.path.join(BASE, "machines", slug)
        if os.path.isdir(d) and not os.listdir(d):
            os.rmdir(d)
        # ★退避物が1つでも残っている間は目印を消さない★（2026-08-03・
        #   Codex61回目。目印が消えると次の復旧が退避物を見つけられず、
        #   未追跡ファイルが後続のpushを止め続ける）
        import glob as _glob
        _strays = []
        for rel in allowed_created:
            if rel.startswith("machines.json#"):
                continue
            _strays += [x for x in _glob.glob(
                os.path.join(BASE, rel) + ".recover.*") if os.path.isfile(x)]
        if _strays:
            out["problems"].append(
                "★退避物が残っているため目印は消しません: "
                + " / ".join(sorted(set(_strays))[:3])[:200] + "★")
            return out
        mark_done()                       # ★最後の操作★（Codex11回目の助言）
        out["restored"].append("（目印を消しました）")
        _clear_stale_push_marker(slug, out)
    else:
        d = os.path.join(BASE, "machines", slug)
        if os.path.isdir(d):
            out["todo"].append(f"消す: machines/{slug}/（空になった時）")
        out["todo"].append("同じ機種のコミット前push待ちの目印があれば消す")
    return out


def _clear_stale_push_marker(slug: str, out: dict) -> None:
    """★戻した公開のpush待ちの目印も片付ける★（2026-08-02・Codex56回目）

    公開部は「途中」の目印を消す**前**にpush待ちの目印を作る（引き継ぎの
    隙間を無くすため・Codex22回目）。その間に止まると両方が残り、
    復旧で「途中」を戻しても push待ちだけが残った。翌晩からは
    変更なしのツリーをコミットしようとして毎晩失敗し、自動経路が
    恒久停止する（Codex56回目の指摘・コードで確認）。
    ★消すのは「同じ機種・コミット前（WRITTEN・sha無し）」の目印だけ★。
    コミット済み（sha入り）や別機種の目印は push側の仕事なので触らない。
    """
    p = os.path.join(BASE, ".push-pending.json")
    if not os.path.isfile(p):
        return
    try:
        got = _sj.read_json(p, expect=dict)
    except Exception:                     # noqa: BLE001
        out["problems"].append(
            "★push待ちの目印が壊れています。人が確かめてください★")
        return
    stage = got.get("stage") or ("COMMITTED" if got.get("sha") else "WRITTEN")
    if got.get("slug") == slug and stage == "WRITTEN" and not got.get("sha"):
        os.remove(p)
        out["restored"].append("（push待ちの目印も消しました＝コミット前だったため）")
    elif got.get("slug") == slug:
        out["problems"].append(
            f"★{slug} のpush待ちの目印がコミット済みの形で残っています。"
            "push側の再開処理に任せます（消していません）★")


# ---------------------------------------------------------------- selftest

def _raises(fn) -> bool:
    try:
        fn()
    except Exception:                        # noqa: BLE001
        return True
    return False


TEST_SLUG_PREFIX = "zzz_"


def _only_test_added(now: bytes, before: bytes, path: str) -> bool:
    """★増えたのが試験用の機種だけか★（2026-08-24・Codexの5回目）

    ★「印が入っているか」では足りない★＝試験の印が残っている最中に
    人が別の正当な編集をすると、印を理由に**その編集ごと書き戻す**。
    中身から試験用の機種を取り除いて、始めたときと一致するかで決める。
    """
    if TEST_SLUG_PREFIX.encode() not in now:
        return False
    try:
        cur = now.decode("utf-8")
        old = before.decode("utf-8")
    except Exception:                                        # noqa: BLE001
        return False
    if path.endswith(".json"):
        import json as _j
        try:
            a = _j.loads(cur)
            b = _j.loads(old)
        except Exception:                                    # noqa: BLE001
            return False
        if isinstance(a, list) and isinstance(b, list):
            kept = [x for x in a
                    if not str((x or {}).get("slug") or "").startswith(
                        TEST_SLUG_PREFIX)]
            return kept == b
        return False
    keep = [x for x in cur.splitlines() if TEST_SLUG_PREFIX not in x]
    return keep == old.splitlines()

def _same_as_head_without_test(rel: str, _git) -> bool:
    """★試験用の機種を取り除いたら、HEAD と同じ中身になるか★

    ★なぜ中身で見るか★（2026-08-24・Codexの4回目の指摘）
      行の字面（zzz_ を含むか）で判断すると、
      機種一覧のJSONのように**1機種が複数行の塊**になっている場合、
      塊の中の zzz_ を含まない行のせいで判断できない。
      ＝偽の機種が丸ごと残っていても掃除が働かない。

    ★人の作業を巻き添えにしない★は変わらない＝
      取り除いても HEAD と一致しないなら、別の変更が混ざっているので触らない。
    """
    head = _git("show", f"HEAD:{rel}").stdout
    if not head:
        return False
    cur_path = os.path.join(BASE, rel)
    try:
        with open(cur_path, encoding="utf-8") as fh:
            cur = fh.read()
    except OSError:
        return False
    if TEST_SLUG_PREFIX not in cur:
        return False                     # 試験用の残骸ではない
    if rel.endswith(".json"):
        import json as _j
        try:
            a = _j.loads(cur)
            b = _j.loads(head)
        except Exception:                                    # noqa: BLE001
            return False
        if isinstance(a, list) and isinstance(b, list):
            kept = [x for x in a
                    if not str((x or {}).get("slug") or "").startswith(
                        TEST_SLUG_PREFIX)]
            return kept == b
        return False
    # HTML（早見表）＝試験用の機種が出てくる行を落として比べる
    keep = [x for x in cur.splitlines() if TEST_SLUG_PREFIX not in x]
    return keep == [x for x in head.splitlines()
                    if TEST_SLUG_PREFIX not in x] and keep == head.splitlines()

def purge_test_residue(apply_it: bool = True) -> list:
    """★試験用の残骸（zzz_ で始まる機種）を消す★（2026-08-24・自分で踏んだ）

    ★なぜ要るか★
      障害注入の試験は**本番のファイルへ実際に書いてから元へ戻す**。
      ★途中で強制終了されると、その巻き戻しが走らない★ので
      「再開確認機ZZZ」のような偽の機種がリポジトリに残る。

      残ると公開前の関所が「許していないファイル」と見なして
      **夜の公開を丸ごと止める**。
      ＝★エラーも出ないまま公開0件が続く★（2026-08-22に直したのと同じ型）。

    ★消し方を2つに分ける★
      ・追跡されていない残骸（記事データ・ページ）＝そのまま消す
      ・追跡ファイル（機種一覧・早見表）＝**zzz_ の行しか変わっていない時だけ**
        HEAD へ戻す。他の変更が混ざっていたら**触らずに報告**する
        （★人の作業を巻き添えにしない★）。
    """
    import glob as _g
    import shutil as _sh
    import subprocess as _sp
    found = []

    def _git(*a):
        return _sp.run(["git"] + list(a), cwd=BASE, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")

    for d in _g.glob(os.path.join(BASE, "machines", TEST_SLUG_PREFIX + "*")):
        found.append(os.path.relpath(d, BASE).replace(chr(92), "/"))
        if apply_it:
            _sh.rmtree(d, ignore_errors=True)
    for f in _g.glob(os.path.join(BASE, "assets", "data", "machine-details",
                                  TEST_SLUG_PREFIX + "*.json")):
        found.append(os.path.relpath(f, BASE).replace(chr(92), "/"))
        if apply_it:
            os.remove(f)

    # ★★公開途中の目印も残骸になる★★（2026-08-24・Codexの3回目の指摘3）
    #   自己試験は**本物の目印**を書く（監査は別プロセスなので、
    #   偽物に差し替えると再現できない）。強制終了されると
    #   `.publish-in-progress.json` が残り、
    #   ★以後の新台追加が「前回の公開途中です」で永久に止まる★。
    #   `.push-pending.json` も同じで、偽の機種のpush待ちとして残る。
    #   ★試験用のslug（zzz_）が書いてある時だけ消す★
    #   ＝本物の公開途中には絶対に触らない。
    import json as _js
    for mk in (IN_PROGRESS, os.path.join(BASE, ".push-pending.json")):
        if not os.path.isfile(mk):
            continue
        try:
            _slug = str((_js.load(open(mk, encoding="utf-8")) or {}).get("slug")
                        or "")
        except Exception:                                    # noqa: BLE001
            continue                    # ★読めないものは触らない★
        if not _slug.startswith(TEST_SLUG_PREFIX):
            continue                    # ★本物の公開途中★
        found.append(os.path.relpath(mk, BASE).replace(chr(92), "/")
                     + f"（{_slug} の目印）")
        if apply_it:
            os.remove(mk)

    for rel in ("assets/data/machines.json",) + tuple(HUB_FILES):
        d = _git("diff", "-U0", "--", rel).stdout or ""
        if not d.strip():
            # ★中身は同じでも「変更あり」と出ることがある★（2026-08-24・実測）
            #   試験が書き直すと改行コードが LF になり、
            #   中身が1文字も違わないのに git は変更として数える。
            #   ＝夜の公開で「許していないファイル」に見えるのは同じ。
            #   中身が同じなら戻して困る人はいないので、そろえる。
            if _git("status", "--porcelain", "--", rel).stdout.strip():
                found.append(rel + "（改行コードだけ）")
                if apply_it:
                    _git("checkout", "--", rel)
            continue
        changed = [x for x in d.splitlines()
                   if (x.startswith("+") or x.startswith("-"))
                   and not x.startswith(("+++", "---"))]
        if not changed:
            continue
        # ★★行の字面だけでは足りない★★（2026-08-24・Codexの4回目の指摘）
        #   ★直す前は「動いた行が全部 zzz_ を含むとき」だけ戻していた★。
        #   機種一覧のJSONは1機種が十数行の塊なので、
        #   その中には `"info": "",` のような **zzz_ を含まない行**が必ずある。
        #   ＝★偽の機種が丸ごと残っていても、掃除は報告するだけだった★。
        #   → 中身で判断する＝「試験用の機種を取り除いたら HEAD と同じか」。
        if _same_as_head_without_test(rel, _git):
            found.append(rel)
            if apply_it:
                _git("checkout", "--", rel)
        elif any(TEST_SLUG_PREFIX in x for x in changed):
            found.append(rel + "（★他の変更と混ざっているので触りません★）")
    return found


def selftest() -> int:
    import inspect
    import inspect
    import tempfile as _tf
    # ★★始める前に、前回の残骸を掃除する★★（強制終了された時の受け皿）
    purge_test_residue(apply_it=True)
    results = []
    nl = chr(10)

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    rows = _sj.read_rows(MACHINES)
    _pd_ok = _pdz.decide_from_claims(["model_code"], "normal", "2026-08-04")
    _pd_index = _pdz.decide_from_claims(
        ["model_code", "payout_range", "at:MAIN_AT"], "normal", "2026-08-04")
    ok_machine = {"slug": "zzz_test", "name": "テスト機",
                  "publication_policy": _pdz.SCHEMA, "page_decision": _pd_ok,
                  "publish_state": STATE}
    t("★新しい機種なら前提を通る★", check_before("zzz_test", ok_machine, rows) == [])
    t("★★既にある機種は拒否する★★（上書きしない）",
      check_before(rows[0]["slug"],
                   {**ok_machine, "slug": rows[0]["slug"]}, rows))
    t("★★判定書と旧statusの同居は公開しない★★（fail-closed・Codex71回目）",
      any("判定書" in x or "区分" in x for x in
          check_before("zzz_test", {**ok_machine, "status": "preview"}, rows)))
    t("★★未知のpolicyは公開しない★★",
      any("判定書" in x or "区分" in x for x in
          check_before("zzz_test",
                       {**ok_machine, "publication_policy": "other/v9"}, rows)))
    t("★★判定書の欠落は公開しない★★",
      any("判定書" in x or "区分" in x for x in
          check_before("zzz_test",
                       {k: v for k, v in ok_machine.items()
                        if k != "page_decision"}, rows)))
    t("★★旧status契約（LEGACY_PREVIEW）はこの経路で公開しない★★",
      any("区分" in x for x in
          check_before("zzz_test", {"slug": "zzz_test", "name": "テスト機",
                                    "status": "preview",
                                    "publish_state": STATE}, rows)))
    t("★★状態名が違えば公開しない★★（既存の未裏取りページと混ぜない）",
      any("publish_state" in x for x in
          check_before("zzz_test",
                       {**ok_machine, "publish_state": "LEGACY_UNVERIFIED"}, rows)))
    t("　slugが食い違えば拒否", check_before("aaa", ok_machine, rows))

    good = ('<html><head><base href="/">'
            '<meta name="robots" content="noindex,follow">'
            '<link rel="canonical" href="https://uchidokoro.com/machines/zzz_test/">'
            "</head><body>"
            '<div class="preview-banner" role="note" ' + NOTICE + ">"
            + NOTICE_TEXT + "</div></body></html>")
    t("★作ったページの中身を必ず確かめる★", check_page("zzz_test", good) == [])
    # ★★ひな型（machine.html）そのものと突き合わせる★★
    #   （2026-08-26・Codex30回目。★前回の直しは半分だった★＝
    #     バナーは既定で隠れているので、生成物だけを見る検査は必ず素通りした）
    with open(os.path.join(BASE, "machine.html"), encoding="utf-8") as _tplf:
        _tpl_now = _tplf.read()
    t("★★いまのひな型と NOTICE_TEXT が一致している★★",
      check_template_notice(_tpl_now) == [])
    t("★★ひな型だけ文言を変えたら止める★★"
      "／★これが無いと、二重管理の食い違いを誰も見つけられない★",
      any("ひな型" in x for x in check_template_notice(
          _tpl_now.replace("確認が取れた項目のみ",
                           "出典で確認が取れた項目のみ"))))
    t("　隠れていても読む（既定の読み方だと必ず空になる）",
      "未掲載の項目" in template_notice_text(_tpl_now))
    # ★★呼び出し口（render）そのものを試す★★（2026-08-26）
    #   ★関数を直接たたく試験だけだと、render から外しても緑のまま★
    #   （壊し方の道具が「守られていない」と出して分かった）。
    import shutil as _sh54
    import tempfile as _tf54
    _tmpb = _tf54.mkdtemp(prefix="uchi_tplchk_")
    _keep_base = BASE
    try:
        _sh54.copy(os.path.join(BASE, "machine.html"),
                   os.path.join(_tmpb, "machine.html"))
        with open(os.path.join(_tmpb, "machine.html"), "r+",
                  encoding="utf-8") as _f54:
            _s54 = _f54.read().replace("確認が取れた項目のみ",
                                       "出典で確認が取れた項目のみ")
            _f54.seek(0)
            _f54.write(_s54)
            _f54.truncate()
        globals()["BASE"] = _tmpb
        try:
            render("zzz_test", {"slug": "zzz_test", "name": "試験"},
                   {"sections": []})
            _render_stopped = False
        except PublishError as _e54:
            _render_stopped = "ひな型" in str(_e54)
        except Exception:                                # noqa: BLE001
            _render_stopped = False
    finally:
        globals()["BASE"] = _keep_base
        _sh54.rmtree(_tmpb, ignore_errors=True)
    t("★★ページを描く所（render）が、ひな型の食い違いで止まる★★"
      "／★関数を直接たたく試験だけでは、外されても気づけない★",
      _render_stopped)
    # ★★断り書きの文言そのものを突き合わせる★★（2026-08-26・Codex29回目の指摘4）
    #   ★直す前は、ひな型側の文言を変えても検査が何も言わなかった★
    #   （`preview_notices()` は目印の**個数**を数えるだけ）。
    #   ＝`LEGACY_NOTE` と違い「止まらずに黙って食い違う」箇所だった。
    _bad_notice = good.replace(NOTICE_TEXT, "⚠ このページは出典で確認が取れた"
                               "項目のみ掲載しています。未掲載の項目は確認でき次第"
                               "更新します。")
    t("★★断り書きが決めた文言と違えば止める★★"
      "／★これが無いと、ひな型だけ書き換えても誰も気づかない★",
      any("断り書きの文言" in x for x in check_page("zzz_test", _bad_notice)))
    t("　1文字違うだけでも止める（似ていれば通す、にしない）",
      any("断り書きの文言" in x
          for x in check_page("zzz_test",
                              good.replace(NOTICE_TEXT, NOTICE_TEXT + "。"))))
    t("　空白の入れ方が違うだけなら通す（改行や字下げで落とさない）",
      check_page("zzz_test", good.replace(
          NOTICE_TEXT, "  " + NOTICE_TEXT.replace("。", "。\n  "))) == [])
    # ★index対象（AUTO_INDEXABLE）: robots meta が無いことを要求★（Codex72回目）
    good_indexable = good.replace(
        '<meta name="robots" content="noindex,follow">', "")
    t("★★index対象は robots 無しで通る★★",
      check_page("zzz_test", good_indexable, expect_noindex=False) == [])
    t("★★index対象に noindex が付いていたら止める★★（逆方向もfail-closed）",
      any("robots" in x for x in
          check_page("zzz_test", good, expect_noindex=False)))
    t("★★時間で嘘になる語（導入予定等）が入っていたら止める★★"
      "（鮮度ゲート・Codex70回目）",
      any("時間で嘘になる語" in x for x in
          check_page("zzz_test",
                     good.replace("</body>", "<p>2026年9月導入予定</p></body>"))))
    # ★sitemap の追加・除去（1行形式・1件だけ）★
    _sm0 = ('<?xml version="1.0" encoding="UTF-8"?>' + chr(10)
            + '<urlset>' + chr(10)
            + '  <url><loc>https://uchidokoro.com/machines/aaa/</loc></url>'
            + chr(10) + '</urlset>' + chr(10))
    _sm1 = add_to_sitemap(_sm0, "zzz_new")
    t("★★sitemapへ1行だけ足せる（</urlset>直前・1行形式）★★",
      _sitemap_locs(_sm1) == ["https://uchidokoro.com/machines/aaa/",
                              "https://uchidokoro.com/machines/zzz_new/"])
    def _pub_raises(fn):
        try:
            fn()
            return False
        except PublishError:
            return True
    t("　同じ行の二重追加は止める",
      _pub_raises(lambda: add_to_sitemap(_sm1, "zzz_new")))
    t("★★除去は自分が足した1行の完全一致だけ★★（元に戻る）",
      remove_from_sitemap(_sm1, "zzz_new") == _sm0)
    _real_smp = globals().get("SITEMAP")
    try:
        import tempfile as _tf65
        _smd = _tf65.mkdtemp(prefix="uchi_sm_")
        globals()["SITEMAP"] = os.path.join(_smd, "sitemap.xml")
        with open(SITEMAP, "w", encoding="utf-8") as _f65:
            _f65.write(_sm1)
        t("★★check_sitemap_added: 正しい1件追加は通る★★",
          check_sitemap_added(_sm0, "zzz_new") == [])
        with open(SITEMAP, "w", encoding="utf-8") as _f65:
            _f65.write(add_to_sitemap(_sm1, "zzz_two"))
        t("　2件増えていたら止める",
          check_sitemap_added(_sm0, "zzz_new"))
        with open(SITEMAP, "w", encoding="utf-8") as _f65:
            _f65.write(_sm1.replace("machines/aaa", "machines/bbb"))
        t("　1件追加＋別URLの書き換えは止める",
          check_sitemap_added(_sm0, "zzz_new"))
    finally:
        globals()["SITEMAP"] = _real_smp
        __import__("shutil").rmtree(_smd, ignore_errors=True)
    t("★★noindex をコメントに書いただけでは通さない★★（実際に通っていた）",
      check_page("zzz_test",
                 good.replace('content="noindex,follow"', 'content="index,follow"')
                 + "<!-- noindex -->"))
    t("★★robots が2つあれば止める★★（競合する指定を見逃さない）",
      any("robots" in x for x in check_page(
          "zzz_test", good.replace("</head>",
                                   '<meta name="robots" content="index"></head>'))))
    t("　base href が無ければ公開しない",
      any("base" in x for x in check_page("zzz_test",
                                          good.replace('<base href="/">', ""))))
    t("　canonical が別機種なら公開しない",
      any("canonical" in x for x in
          check_page("zzz_test", good.replace("zzz_test/", "other/"))))
    t("　インラインstyleがあれば公開しない",
      any("style" in x for x in check_page("zzz_test",
                                           good.replace("<body>", '<body style="x">'))))
    # ★★記事の箱をページ側で構造ごと確かめる★★（2026-08-04・Codex77〜79回目）
    #   平坦化した文字の比較では、クラス・見出し・表を壊しても通っていた。
    #   記事データから描き直したHTMLと、そのまま突き合わせる。
    # ★根拠は必ず持たせる★（2026-08-24＝本番の材料は必ず basis を持つ。
    #   持たない材料は build_detail が公開を断る）
    _IM = {"basis": "INDEPENDENT_MULTI"}
    _mat_ok = {"adopted": {"model_code": {**_IM, "value": "L1"},
                           "payout_range": {**_IM,
                                            "value": {"low": 97, "high": 110}},
                           "payout_rate": {**_IM,
                                           "value": {"1": "97%", "6": "110%"}}},
               "at_specs": {"adopted": [{**_IM, "mode": "MAIN_AT", "games": 30,
                                         "net": 2.8}]},
               "czs": {"adopted": [{**_IM, "name": "試験チャンス",
                                    "games": "20G",
                                    "games_basis": _IM["basis"]}]}}
    _det_ok = _ba.build_detail("zzz_test", "試験機", "2026-09", _mat_ok)
    _inner = "".join(_bmp.render_section(x) for x in _det_ok["sections"])
    _html_ok = ("<html><head></head><body>"
                + '<div id="articleSections">' + _inner + "</div>"
                + '<div class="article-block">next</div></body></html>')
    t("★★箱がそろっていれば通る★★", check_pending_boxes(_html_ok, _det_ok) == [])
    t("★★タイトルだけで本文が空の骨組みは公開しない★★（Codex82回目の指摘2）",
      any("中身がありません" in x for x in check_pending_boxes(
          _html_ok, {"slug": "zzz_test",
                     "sections": [{"title": x, "body": []}
                                  for x in _ba.SECTION_ORDER]
                     + [{"title": _ba.RUMOR_SECTION["title"], "body": []}]})))
    t("★★箱のクラスを外したら止める★★（構造ごと突き合わせる・Codex79回目）",
      any("ページのものと一致しません" in x for x in check_pending_boxes(
          _html_ok.replace('class="article-item" ', ""), _det_ok)))
    t("★★見出しを別のタグに変えたら止める★★",
      any("ページのものと一致しません" in x for x in check_pending_boxes(
          _html_ok.replace('<h3 class="article-title">', "<span>")
          .replace("</h3>", "</span>"), _det_ok)))
    t("★★表を段落に潰したら止める★★",
      any("ページのものと一致しません" in x for x in check_pending_boxes(
          _html_ok.replace("<table", "<p").replace("</table>", "</p>"),
          _det_ok)))
    t("★★★ページ内の <style> で箱ごと隠せない★★★（Codex79回目の指摘2）",
      any("<style>" in x for x in check_pending_boxes(
          _html_ok.replace("<body>",
                           "<body><style>.article-item{display:none}</style>"),
          _det_ok))
      and any("<style>" in x for x in check_pending_boxes(
          _html_ok.replace("<body>", "<body><STYLE >x{display:none}</STYLE>"),
          _det_ok)))
    t("★★別の場所に偽の箱を置いたら止める★★",
      any("目印が余分" in x for x in check_pending_boxes(
          _html_ok.replace("</body>",
                           '<p data-section="ゲーム性">にせもの</p></body>'),
          _det_ok)))
    t("★★CSSクラスで隠した箱は見えていないと判定する★★",
      any("見えている箱" in x or "ページのものと一致しません" in x
          for x in check_pending_boxes(
              _html_ok.replace('class="article-item" data-section="天井・恩恵"',
                               'class="article-item is-hidden" '
                               'data-section="天井・恩恵"'),
              _det_ok)))
    t("　記事データ側の箱が欠けていたら止める（契約と突き合わせる）",
      any("契約と違います" in x for x in check_pending_boxes(
          _html_ok, {**_det_ok,
                     "sections": [x for x in _det_ok["sections"]
                                  if x["title"] != "天井・恩恵"]})))

    t("　数値のかたまりを取り出せる（全角もそろえる）",
      _numbers("約97.3%と１２００Ｇ") == {"97.3%", "1200"})

    t("★★robots は content の中身で見る★★"
      "（data-note=\"noindex\" で合格していた・実際に再現）",
      any("robots" in x for x in check_page(
          "zzz_test",
          good.replace('content="noindex,follow"',
                       'content="index" data-note="noindex"'))))
    t("　暦にない日付は止める",
      any("暦" in x for x in check_machine(
          "zzz_test", {"slug": "zzz_test", "name": "x", "seo": {"title": "x"},
                       "info": "", "strategy": "", "aliases": [],
                       "status": "preview", "release_date": "2026-99",
                       "publish_state": STATE})))
    t("　2月30日も止める", not release_ok("2026-02-30"))
    t("　ふつうの年月は通る", release_ok("2026-09") and release_ok("2026-09-15")
      and release_ok(""))

    # ★中まで見る★（2026-07-31・自分で確かめて9箇所が素通りしていた）
    _b = {"slug": "zzz_test", "sections": []}
    for _why, _bad in (
            ("factTable の中に辞書", {**_b, "factTable": [{"x": "9999G"}]}),
            ("summaryBoxes に任意の形", {**_b, "summaryBoxes": [{"任意": "天井99999G"}]}),
            ("表の headers に辞書",
             {**_b, "sections": [{"title": "x",
                                  "tables": [{"headers": [{"a": 1}], "rows": []}]}]}),
            ("節の rows に辞書",
             {**_b, "sections": [{"title": "x", "rows": [{"a": 1}]}]}),
            ("lead が辞書", {**_b, "lead": {"a": "b"}})):
        t(f"★{_why}は止める★", check_detail("zzz_test", _bad))
    _m2 = {"slug": "zzz_test", "name": "x", "seo": {"title": "x"}, "info": "",
           "strategy": "", "aliases": [], "status": "preview",
           "release_date": "2026-09", "publish_state": STATE}
    for _why, _bad in (
            ("aliases に辞書", {**_m2, "aliases": [{"a": 1}]}),
            ("seo.title が辞書", {**_m2, "seo": {"title": {"a": 1}}}),
            ("identity に知らない項目", {**_m2, "identity": {"任意": "9999"}}),
            ("release_date が変な形", {**_m2, "release_date": "9999年天井"})):
        t(f"★{_why}は止める★", check_machine("zzz_test", _bad))
    t("★★identity の配列の中に辞書を入れられない★★（Codex指摘・再現した）",
      check_machine("zzz_test",
                    {**_m2, "identity": {"_model_code_sources": [{"任意": "にせ"}]}}))
    t("　まともな identity は通る",
      check_machine("zzz_test",
                    {**_m2, "identity": {"manufacturer_id": "bellco",
                                         "_model_code_sources": ["a", "b"]}}) == [])
    t("　本物の機種データは通る", check_machine("zzz_test", _m2) == [])

    # ★受け取った記事データそのものを確かめる★
    t("★まともな記事データなら通る★",
      check_detail("zzz_test", {"slug": "zzz_test", "sections": []}) == [])
    t("★★実際に作られる記事データが通る★★"
      "（許可リストを狭く書いて本物を弾いた・自分で気づいた）",
      check_detail("zzz_test", __import__("build_new_article").build_detail(
          "zzz_test", "テスト", "2026-09",
          {"adopted": {}, "need_third": {}, "thin": {}})) == [])
    t("★★別の機種の記事データなら止める★★",
      check_detail("zzz_test", {"slug": "other", "sections": []}))
    t("★★採用しなかったものの置き場が残っていたら止める★★",
      any("need_third" in x for x in
          check_detail("zzz_test", {"slug": "zzz_test", "sections": [],
                                    "need_third": {"at_prob": "1/999"}})))
    t("　知らない項目があれば止める",
      check_detail("zzz_test", {"slug": "zzz_test", "sections": [],
                                "こっそり": 1}))
    t("　節に知らない項目があれば止める",
      check_detail("zzz_test", {"slug": "zzz_test",
                                "sections": [{"title": "x", "候補": []}]}))

    t("★★summaryBoxes の配列の中に辞書を入れられない★★（Codex指摘・再現した）",
      check_detail("zzz_test", {"slug": "zzz_test", "sections": [],
                                "summaryBoxes": [{"title": "題",
                                                  "body": [{"任意": "天井99999G"}]}]}))
    t("　まともな summaryBoxes は通る",
      check_detail("zzz_test", {"slug": "zzz_test", "sections": [],
                                "summaryBoxes": [{"title": "題",
                                                  "body": ["ふつうの文"]}]}) == [])
    t("★★表の見出し数と行の列数がそろわなければ止める★★"
      "（正しい値が別の見出しの下に出る）",
      any("列数" in x for x in check_detail(
          "zzz_test", {"slug": "zzz_test",
                       "sections": [{"title": "x",
                                     "tables": [{"headers": ["A", "B", "C"],
                                                 "rows": [["1", "2"]]}]}]})))

    # ★★表の節の契約★★（2026-08-31・Codexの10回目）
    def _tsec(sec):
        return check_detail("zzz_test", {"slug": "zzz_test",
                                         "sections": [sec]})

    _tok = {"title": "基本スペック", "type": "table",
            "tables": [{"headers": ["項目", "内容"],
                        "rows": [["機種名", "テスト機"]]}]}
    t("　表だけの節は通る", _tsec(_tok) == [])
    t("★★表の節に本文があれば止める★★"
      "（表のあとに本文が出るのに、その順番の契約がどこにも無かった）",
      any("本文は置けません" in x
          for x in _tsec({**_tok, "body": ["補足です"]})))
    for _empty in ([], "", None, 0):
        t(f"　本文が {_empty!r} でも置かせない（存在で見る・Codexの11回目）",
          any("本文は置けません" in x
              for x in _tsec({**_tok, "body": _empty})))
    # ★wide を含むふつうの表は新台経路で必ず落ちる★（Codexの11回目・確認用）
    #   ★ここでは _TABLE_KEYS（知らない項目）が断る★＝
    #   同じことを2か所で見ないための確認
    t("★wide を含む表は新台経路で落ちる★（知らない項目として断られる）",
      any("知らない項目" in x for x in _tsec(
          {"title": "x", "type": "table",
           "tables": [{"headers": ["項目", "内容"], "wide": True,
                       "rows": [["a", "b"]]}]})))
    t("　表の節に rows は使わせない",
      any("rows は使いません" in x
          for x in _tsec({**_tok, "rows": [["a", "b"]]})))
    t("　表の節なのに表が無ければ止める",
      any("表がありません" in x
          for x in _tsec({"title": "x", "type": "table"})))

    # ★機種データそのものを確かめる★（Codex指摘2）
    _ok_machine = {"slug": "zzz_test", "name": "テスト", "seo": {"title": "x"},
                   "info": "", "strategy": "", "aliases": [],
                   "publication_policy": _pdz.SCHEMA,
                   "page_decision": _pd_ok,
                   "release_date": "2026-09",
                   "identity": {
                       "manufacturer_id": "bellco",
                       "identity_tier": "CATALOG_BOUND",
                       "official_product_url":
                           "https://www.s-bellco.co.jp/products/slot/zzz_test/",
                       "announced_name": "テスト"},
                   "publish_state": STATE}
    t("★まともな機種データなら通る★", check_machine("zzz_test", _ok_machine) == [])
    t("★★知らない項目が混ざっていたら止める★★（そこに書いた文字がページへ出る）",
      any("知らない項目" in x for x in
          check_machine("zzz_test", {**_ok_machine, "こっそり": "9999G天井"})))
    t("★★先行記事に狙い目は書かせない★★（当サイトの判断は裏取りの外）",
      any("狙い目" in x for x in
          check_machine("zzz_test", {**_ok_machine, "strategy": "等価600G〜"})))
    t("　aliases が配列でなければ止める",
      check_machine("zzz_test", {**_ok_machine, "aliases": "ほくと"}))
    # ★本人性を公開の境界でも確かめる★（Codex73回目の指摘5）
    _id_ok = {"manufacturer_id": "bellco", "identity_tier": "CATALOG_BOUND",
              "official_product_url":
                  "https://www.s-bellco.co.jp/products/slot/zzz_test/",
              "announced_name": "テスト"}
    t("★まともな identity なら通る★",
      check_machine("zzz_test", {**_ok_machine, "identity": _id_ok}) == [])
    t("★★identity ごと消して検証を素通りできない★★（Codex74回目の指摘4）",
      any("identity" in x for x in check_machine(
          "zzz_test", {k: v for k, v in _ok_machine.items()
                       if k != "identity"})))
    t("★★identity の必須項目が欠けていれば止める★★",
      any("official_product_url" in x for x in check_machine(
          "zzz_test", {**_ok_machine,
                       "identity": {"manufacturer_id": "bellco",
                                    "announced_name": "テスト"}})))
    t("★★名簿に無いメーカーは止める★★",
      any("メーカーが名簿" in x for x in check_machine(
          "zzz_test", {**_ok_machine,
                       "identity": {**_id_ok, "manufacturer_id": "zzzz"}})))
    t("★★公式URLが https でなければ止める★★",
      any("https" in x for x in check_machine(
          "zzz_test", {**_ok_machine,
                       "identity": {**_id_ok, "official_product_url":
                                    "http://www.s-bellco.co.jp/products/slot/zzz_test/"}})))
    t("★★slugが公式URLの末尾と違えば止める★★",
      any("slug" in x for x in check_machine(
          "zzz_test", {**_ok_machine,
                       "identity": {**_id_ok, "official_product_url":
                                    "https://www.s-bellco.co.jp/products/slot/other/"}})))
    t("★★公式の発表名と機種名が違えば止める★★",
      any("発表名" in x for x in check_machine(
          "zzz_test", {**_ok_machine,
                       "identity": {**_id_ok, "announced_name": "別の機種"}})))

    # ★記事データの中の形まで見る★
    t("　表の中身が文字の並びでなければ止める",
      any("文字の並び" in x for x in check_detail(
          "zzz_test", {"slug": "zzz_test",
                       "sections": [{"title": "x",
                                     "tables": [{"rows": "ただの文字列"}]}]})))
    t("　知らない節の種類なら止める",
      any("節の種類" in x for x in check_detail(
          "zzz_test", {"slug": "zzz_test",
                       "sections": [{"title": "x", "type": "なぞ"}]})))
    t("　本文が文字の配列でなければ止める",
      any("本文" in x for x in check_detail(
          "zzz_test", {"slug": "zzz_test",
                       "sections": [{"title": "x", "body": "ひとつの文字列"}]})))

    # ★見えない要素の判定★（Codex指摘5）
    t("★★引用符が違う robots も数える★★（正規表現では見逃していた）",
      any("2 個" in x for x in check_page("zzz_test", good.replace(
          "</head>", "<meta name='robots' content='index'></head>"))))
    t("★★旧preview（既存7機種）の断り書きは従来どおり検査する★★"
      "（隠された断り書きを認めない・DOM契約は残す）",
      len(_hc.preview_notices(_hc.parse(
          good.replace('role="note"', 'role="note" hidden')), STATE)) == 0)

    t("★★全体の機種数はもう扱わない★★（表示しない方針・監査が再導入を見張る）",
      count_updates(120, 121) == {} and COUNT_FILES == ())

    # ★形だけの試験をやめる★（2026-07-31・Codex指摘：常に合格していた）
    t("★いまは早見表がデータと一致している★", check_hubs_untouched() == [])
    _real_build = build_hubs
    try:
        globals()["build_hubs"] = lambda: {"guide-ichiran.html": "ちがう中身"}
        t("★★早見表がずれていたら見つける★★",
          any("違います" in x for x in check_hubs_untouched()))
        globals()["build_hubs"] = lambda: {"guide-ichiran.html": "x"}
        t("　4ページそろっていなければ気づける（生成器が減らした場合）",
          set(build_hubs()) != set(HUB_FILES))
    finally:
        globals()["build_hubs"] = _real_build
    import tempfile as _tf3
    _d3 = _tf3.mkdtemp(prefix="uchi_atomic_")
    try:
        _p3 = os.path.join(_d3, "a.txt")
        write_atomic(_p3, "ほんぶん")
        t("★一時ファイルに完成させてから置き換える★",
          open(_p3, encoding="utf-8").read() == "ほんぶん"
          and not [x for x in os.listdir(_d3) if ".tmp." in x])
        t("★★新しく作る時に既にあれば作らない★★",
          _raises(lambda: write_atomic(_p3, "うわがき", new_only=True))
          and open(_p3, encoding="utf-8").read() == "ほんぶん")
        t("　書きかけの一時ファイルを残さない",
          len(os.listdir(_d3)) == 1)
    finally:
        __import__("shutil").rmtree(_d3, ignore_errors=True)

    t("★いまは途中で終わった公開が残っていない★", unfinished() == {})
    _real_marker = IN_PROGRESS
    _md = _tf.mkdtemp(prefix="uchi_mark_")
    try:
        globals()["IN_PROGRESS"] = os.path.join(_md, "mark.json")
        t("★★目印を作れば「途中」と分かる★★"
          "（電源断ではページも一覧もそろってしまい、監査では区別できない）",
          (mark_start("zzz_mark", {"name": "試験"},
                      {os.path.join(BASE, "README.md"): "元の中身"})
           or unfinished().get("slug")) == "zzz_mark")
        t("★★戻し方を目印に持っている★★（目印だけ消すと中途半端なまま公開できる）",
          unfinished().get("restore"))
        t("★★同じ目印を二重に作れない★★（同時に2つ始まらない）",
          _raises(lambda: mark_start("zzz_two", {"name": "試験2"}, {})))
        mark_done()
        t("　消せば「途中」ではなくなる", unfinished() == {})
    finally:
        globals()["IN_PROGRESS"] = _real_marker
        __import__("shutil").rmtree(_md, ignore_errors=True)
    t("★★人が直したページは消さない★★（作ったときの指紋と違えば止まる・Codex11回目）",
      "created" in inspect.getsource(_recover)
      and "誰かが直した可能性" in inspect.getsource(_recover))
    t("★★復旧も同時に2つ走らせない★★（双方が指紋一致と判断して消しに行ける）",
      "RECOVER_LOCK" in inspect.getsource(recover))
    t("★★確かめてから消すまでの隙間をなくす★★"
      "（読む→一致→消すの間に人が直すと、その編集ごと消える）",
      "os.replace(full, held)" in inspect.getsource(_recover)
      and "os.replace(held_, full_)" in inspect.getsource(_recover))
    t("★★作る前に『これから作る』を目印へ残す★★"
      "（作ってから書く形だと、その隙間で落ちた残骸を特定できない）",
      "planned" in inspect.getsource(mark_start))
    t("★★一覧の行も、足したときと同じ時だけ外す★★"
      "（人が足した別名ごと消していた・実際に再現）",
      "足したときと中身が違います" in inspect.getsource(_recover))
    t("★★目印に書かれたパスをそのまま信用しない★★（書き換えられたら別のファイルを消せる）",
      "知らないファイルが入っています" in inspect.getsource(_recover))
    t("★★一覧から外すのは同じslugが1件のときだけ★★（複数あれば人へ）",
      "len(hit) > 1" in inspect.getsource(_recover))
    t("　目印が壊れていたら消さずに人へ知らせる",
      "作ったものの指紋』がありません" in inspect.getsource(_recover))
    t("★★消す前に一覧の行も先に確かめる（全か無か）★★"
      "（ページを消した後に行の食い違いで止まると中途半端が残る・Codex57回目）",
      "rows_pre" in inspect.getsource(_recover)
      and "何も消さずに" in inspect.getsource(_recover))
    t("★★ファイルの削除も全部を確保・検証してから（全か無か）★★"
      "（2件目の指紋違いで1件目だけ消え404を自作できた・Codex58回目）",
      "held_map" in inspect.getsource(_recover)
      and "_undo_held" in inspect.getsource(_recover))
    t("★★退避物を消すのは一覧・早見表・監査の成功後だけ★★"
      "（先に消すと後段の失敗で自動復元できない・Codex59回目）",
      "_undo_all" in inspect.getsource(_recover)
      and "初めて退避物を消す" in inspect.getsource(_recover)
      and "machines_text_before" in inspect.getsource(_recover))
    t("★★片付けに失敗したら目印を消さない（呼び出し側も）★★"
      "（_cleanup後の無条件mark_doneで残骸があるのに目印が消えた・Codex60回目）",
      "if _cleanup():" in inspect.getsource(_publish)
      and "return False" in inspect.getsource(_publish))
    t("★★消せなかった退避物は元パスへ戻す★★"
      "（退避名のままだと次のrecoverが見つけられず回収不能・Codex60回目）",
      "元パスへ戻しました" in inspect.getsource(_recover))
    t("★★公開と復旧は同じロックで排他★★"
      "（mark_startとファイル作成の隙間に復旧が目印を消せた・Codex61回目）",
      "with _OnlyOne():" in inspect.getsource(recover))
    # ★★Codex62回目：旧PIDの退避物の再接続★★
    _fs_dir = __import__("tempfile").mkdtemp(prefix="uchi_stale_")
    try:
        _fp = os.path.join(_fs_dir, "index.html")
        _want62 = _sha("中身A")
        with open(_fp + ".recover.100", "w", encoding="utf-8") as f:
            f.write("中身A")
        _hit, _bad = _find_stale_held(_fp, _want62)
        t("★★前回の退避物（旧PID名）を指紋一致で見つける★★"
          "（見失うと復旧が恒久に完走できなかった・Codex62回目）",
          _hit == _fp + ".recover.100" and _bad == [])
        with open(_fp + ".recover.200", "w", encoding="utf-8") as f:
            f.write("別の中身")
        _hit2, _bad2 = _find_stale_held(_fp, _want62)
        t("　指紋が合わない退避物は「人が確かめる」側に分ける",
          _hit2 == _fp + ".recover.100"
          and _bad2 == [_fp + ".recover.200"])
        t("　復旧の入口が退避物を引き取る配線",
          "_find_stale_held(full, want)" in inspect.getsource(_recover)
          and "前回の退避物を引き取り" in inspect.getsource(_recover))
    finally:
        __import__("shutil").rmtree(_fs_dir, ignore_errors=True)
    t("★★退避物が残っている間は目印を消さない＋巻き戻しは退避物から★★"
      "（Codex61回目）",
      "退避物が残っているため目印は消しません" in inspect.getsource(_recover)
      and inspect.getsource(_recover).index("_undo_held()")
      < inspect.getsource(_recover).index("write_atomic(MACHINES, machines_text_before)"))
    t("★★外部の材料JSONからは公開（--apply）できない★★"
      "（出典の再検証を通らない値を記事化できた・Codex58回目）",
      "外部の材料JSONからの公開" in inspect.getsource(main)
      and "apply_it=False" in inspect.getsource(main))
    t("★★失敗時の片付けは、片付け切れた時だけ目印を消す★★"
      "（残骸があるのに復旧の手がかりだけ失われた・Codex57回目）",
      "片付け切れて初めて" in inspect.getsource(_publish)
      and "片付け切れていないため" in inspect.getsource(_publish))
    # ★★Codex56回目：復旧はpush待ちの目印も片付ける★★
    _pp = os.path.join(BASE, ".push-pending.json")
    if os.path.isfile(_pp):
        # 本物のpush待ちがある時は触らない（試験は挙動の代わりに配線だけ見る）
        t("★★復旧がコミット前のpush待ちの目印も消す★★（配線のみ確認・Codex56回目）",
          "_clear_stale_push_marker" in inspect.getsource(_recover))
    else:
        try:
            import json as _js56
            write_atomic(_pp, _js56.dumps(
                {"slug": "zzz_test56", "stage": "WRITTEN", "sha": ""}))
            _o1 = {"problems": [], "restored": [], "todo": []}
            _clear_stale_push_marker("zzz_test56", _o1)
            _gone = not os.path.isfile(_pp)
            write_atomic(_pp, _js56.dumps(
                {"slug": "zzz_test56", "stage": "COMMITTED", "sha": "abc123"}))
            _o2 = {"problems": [], "restored": [], "todo": []}
            _clear_stale_push_marker("zzz_test56", _o2)
            _kept = os.path.isfile(_pp)
            t("★★復旧がコミット前（WRITTEN）のpush待ちの目印を消す★★"
              "（残ると毎晩の空コミット失敗で自動経路が恒久停止・Codex56回目）",
              _gone and _o1["restored"])
            t("　コミット済み（sha入り）の目印は消さない（push側の再開に任せる）",
              _kept and _o2["problems"])
        finally:
            try:
                os.remove(_pp)
            except OSError:
                pass
    t("★★公開の前にもサイト監査を通せる★★（後から気づいても世に出ている）",
      run_site_audit() == [])
    # ★★実機で見つけた壊れ方★★（2026-07-31・レビューでは出なかった）
    #   公開の最終確認は、自分が「公開中」の目印を持っている最中に回る。
    #   項目33（公開が途中で終わっている）を外していなかったので、
    #   **書けた記事を毎回自分で取り消していた**＝1機種も公開できなかった。
    t("★★最終確認は、自分が置いた目印を理由に取り消さない★★"
      "（1機種も公開できなくなっていた・実機で判明）",
      "ignore_in_progress=True" in inspect.getsource(_publish))
    # ★監査は別のプロセスで動く★ 本物の目印でないと再現できない。
    #   （モジュールの中で差し替えても、監査は本物のファイルを見る）
    if unfinished():
        t("　いま公開が途中なので、目印の試験は飛ばします", True)
    else:
        try:
            mark_start("zzz_audit33", {"name": "試験"}, {})
            _strict = run_site_audit()
            _loose = run_site_audit(ignore_in_progress=True)
        finally:
            mark_done()
        t("★★目印があるとき、外さなければちゃんと引っかかる★★"
          "（push の関所はここで残骸を止める）",
          any("33_" in x for x in _strict))
        t("★★外したときだけ、それを理由に止めない★★"
          "（公開の最終確認は目印を持っている最中に回る）",
          not any("33_" in x for x in _loose))
    # ★監査そのものが壊れたら「合格」にしない★（2026-08-01・Codex23回目を再現して直した）
    #   起動失敗・構文エラーは❌を出さずに非0で終わり、以前は空リスト＝合格だった。
    _real_run = subprocess.run

    def _crash_run(cmd, **k):
        if any("audit_site.py" in str(c) for c in cmd):
            class _R:
                returncode = 1
                stdout = "Traceback: ImportError"
                stderr = "boom"
            return _R()
        return _real_run(cmd, **k)

    def _fake_audit(stdout_text):
        def _fk(cmd, **k):
            if any("audit_site.py" in str(c) for c in cmd):
                class _R:
                    returncode = 1
                    stdout = stdout_text
                    stderr = "boom"
                return _R()
            return _real_run(cmd, **k)
        subprocess.run = _fk
        try:
            return run_site_audit()
        finally:
            subprocess.run = _real_run

    import audit_site as _as_mod2
    _full = {f"{k.split('_', 1)[0]}_試験": [] for k, _f in _as_mod2.CHECKS}
    t("★★監査が異常終了したら合格にしない★★"
      "（構文エラー等はJSONを出さずに終わり、素通りしていた・Codex23回目）",
      any("異常終了" in x for x in _fake_audit("Traceback: ImportError")))
    _cut = json.dumps({**_full, "31_Codexへの未報告": ["x"]},
                      ensure_ascii=False)[:80]
    t("★★途中まで出力して落ちた監査も合格にしない★★"
      "（❌を1行出した後に落ちると素通りしていた・Codex24回目）",
      any("異常終了" in x for x in _fake_audit(_cut)))
    _lack = {k: v for k, v in _full.items() if not k.startswith("32_")}
    t("　項目が欠けたJSONも合格にしない（途中終了の別の形）",
      any("異常終了" in x for x in _fake_audit(json.dumps(_lack, ensure_ascii=False))))
    t("　除外対象（Codex未報告）だけの非0は、いままでどおり通す",
      _fake_audit(json.dumps({**_full, "31_Codexへの未報告": ["x"]},
                             ensure_ascii=False)) == [])
    t("　普通のNGはちゃんと出る",
      any("22_" in x for x in _fake_audit(
          json.dumps({**_full, "22_機種重複検知": ["だぶり"]}, ensure_ascii=False))))
    t("★★同じ入力なら毎回同じ物ができる★★（2回目に差分が出ない・Codexの助言）",
      build_hubs() == build_hubs())
    t("★★一覧と機種データを集合で突き合わせる★★（欠け・余分・重複を見つける）",
      check_counts(len(rows)) == [])

    # ★★写している間に消えたファイルは飛ばす★★（2026-08-28）
    #   ★わざと再現して確かめた★＝直下でファイルを作っては消しながら
    #   走らせると、写しを作る処理が例外で落ちていた（10件が落ちた形と合う）。
    _cpdir = tempfile.mkdtemp(prefix="pnm_copy_")
    _gone = os.path.join(_cpdir, "きえた.txt")
    _dst = os.path.join(_cpdir, "out.txt")
    # ★例外で落ちるのを「守りの証拠」にしない★＝自分で受けて❌にする
    try:
        _cp_ok = copy_tolerant(_gone, _dst) == _dst
    except Exception:                      # noqa: BLE001
        _cp_ok = False
    t("★★写している間に消えたファイルで、写しごと失敗しない★★"
      "／★同時に別の作業をしていると、試験が丸ごと落ちていた★", _cp_ok)
    _src = os.path.join(_cpdir, "ある.txt")
    with open(_src, "w", encoding="utf-8") as _f:
        _f.write("中身")
    copy_tolerant(_src, _dst)
    t("　（対照）あるファイルは、ちゃんと写される",
      os.path.isfile(_dst)
      and open(_dst, encoding="utf-8").read() == "中身")

    # ★★試験の目印は、本物の作業ツリーに作らない★★
    #   （2026-08-28・Codexの助言）
    #   ★直す前は BASE に作っていた★ので、
    #   同じ試験を同時に走らせられず、CI再現と手元の作業がぶつかった。
    _lockdir = tempfile.mkdtemp(prefix="pnm_lock_")

    def _lk(name):
        return os.path.join(_lockdir, name)

    # ★同時に2つ公開しない★（Codex指摘4）
    with _OnlyOne(_lk(".publish.lock.test")) as _one:
        t("★★ロックを持っている間は、もう一方が入れない★★",
          _raises(lambda: _OnlyOne(
              _lk(".publish.lock.test")).__enter__()))
    t("　抜けたらロックは消える",
      not os.path.exists(_lk(".publish.lock.test")))

    # ★★持ち主が死んでいると分かったら、30分待たずに片付ける★★
    #   （2026-08-21・実際に起きた形。対照実験つき）
    #   手元で試験を強制終了したら目印が残り、以後の実行が
    #   「2分前から動いています」と言い続けた＝原因に辿り着けない。
    _dl = _lk(".publish.lock.dead_test")
    try:
        # ★★本当に終わったプロセスのPIDを使う★★（2026-08-21に直した）
        #   ★直す前は 999999 という決め打ちだった★＝
        #   Windows の tasklist では確かに見つからないが、
        #   Linux では実在しうるPIDなので「居ないこと」の保証にならない。
        #   ★実際に1つ動かして、終わるまで待った PID★なら、
        #   どちらのOSでも確実に「もう居ない」。
        import subprocess as _sp
        _proc = _sp.Popen([sys.executable, "-c", "pass"])
        _proc.wait()
        _dead_pid = _proc.pid
        with open(_dl, "w", encoding="utf-8") as _fd:
            _fd.write(f"{_dead_pid}:deadbeef")
        _od = _OnlyOne(_dl)
        try:
            with _od:
                t("★★死んだ持ち主の目印は、30分待たずに片付ける★★"
                  "（動いていないのに動いていると言い続けていた）",
                  _od.evicted is not None)
        except PublishError:
            t("★★死んだ持ち主の目印は、30分待たずに片付ける★★"
              "（動いていないのに動いていると言い続けていた）", False)

        # ★対照★ 生きている持ち主からは、時間内なら奪わない
        with open(_dl, "w", encoding="utf-8") as _fd:
            _fd.write(f"{os.getpid()}:alive")
        _oa = _OnlyOne(_dl)
        t("★★生きている持ち主からは奪わない★★（PIDは使い回されるため）",
          _raises(lambda: _oa.__enter__()))

        # ★印の形が読めないときも奪わない★（安全側）
        with open(_dl, "w", encoding="utf-8") as _fd:
            _fd.write("形が違う印")
        _ou = _OnlyOne(_dl)
        t("　印の形が分からないときは奪わない（安全側）",
          _raises(lambda: _ou.__enter__()))
    finally:
        # ★後片づけは、いま使っている場所を見る★（2026-08-28・Codexの12回目）
        #   ★一時の場所へ移したのに、BASE を掃除したままだった★
        for _n in os.listdir(_lockdir):
            if _n.startswith(".publish.lock.dead_test"):
                try:
                    os.remove(os.path.join(_lockdir, _n))
                except OSError:
                    pass

    # ★★止まったままの目印を、時間で片付ける★★（2026-08-21）
    #   ★直す前に実際に起きていたこと★＝PID 1692 の残骸が丸1日残り、
    #   「いま別の公開処理が動いています」と言い続けた。
    #   ＝★誰も動いていないのに、新台公開が永久に止まる★
    _lt = _lk(".publish.lock.stale_test")
    try:
        # ★★持ち主が生きている印を書く★★（2026-08-21に直した）
        #   ★直す前は居ないPID（9999）を書いていた★ので、
        #   「死んだ持ち主なら早く片付ける」を足した途端この試験が落ちた。
        #   ＝試験の書き方が、確かめたい中身（動いている間は奪わない）と
        #   合っていなかった。★動いているPID＝自分のPIDで書く★。
        with open(_lt, "w", encoding="utf-8") as _f3:
            _f3.write(f"{os.getpid()}:alive")
        t("★動いている間の目印は片付けない★（同時公開の防御は残す）",
          _raises(lambda: _OnlyOne(_lt).__enter__()))
        _old = time.time() - (LOCK_STALE_MINUTES + 1) * 60
        os.utime(_lt, (_old, _old))
        with _OnlyOne(_lt) as _one2:
            t("★★30分より古い目印は残骸として片付けて通る★★"
              "（直す前はここで永久に止まっていた）",
              _one2.evicted is not None
              and _one2.evicted["age_minutes"] > LOCK_STALE_MINUTES)
            _moved = _one2.evicted["moved_to"]
        t("　残骸は消さずに退避してある（後から中身を見られる）",
          os.path.exists(_moved))
        t("　抜けたら新しい目印も消える", not os.path.exists(_lt))
        os.remove(_moved)

        # ★★持ち主の印★★（2026-08-21・Codexの指摘）
        #   ★直す前に起きえたこと★＝
        #     ①Aが動いている ②Bが残骸とみなして奪う ③Aが終わる
        #     → ★os.remove が無条件だったのでBの目印が消えた★
        #     ＝以後Cが割り込める＝同時に2つ公開しない、が破れる
        _lt2 = _lk(".publish.lock.owner_test")
        _stale2 = []
        try:
            _a = _OnlyOne(_lt2)
            _a.__enter__()
            _old2 = time.time() - (LOCK_STALE_MINUTES + 1) * 60
            os.utime(_lt2, (_old2, _old2))
            # ★★OSの守りに寄りかからない★★（2026-08-21・CIが赤くなって分かった）
            #   ★最初はここに「動いている処理の目印は奪えない」と書いていた★。
            #   Windows では掴んでいる fd をOSが守るのでそのとおりだが、
            #   ★Linux では開いたままでも rename できるので成り立たない★。
            #   実際、この試験を入れた 8e117563 から CI が赤くなり続けた
            #   （ci_repro は手元＝Windows で走るので、この差は出ない）。
            #   ＝★試験は「どのOSでも成り立つこと」だけを見る★。
            #   守りの本体は、下の「持ち主の印」と「見張り」のほう。
            #
            # ★処理が死んだのと同じ状態にする★（OSが後始末する）
            os.close(_a.fd)
            _a.fd = None
            os.utime(_lt2, (_old2, _old2))
            _b = _OnlyOne(_lt2)
            _b.__enter__()
            _stale2.append(_b.evicted["moved_to"])
            t("　死んだ処理の目印だけが奪える", _b.evicted is not None)
            _a.__exit__()
            t("★★奪われた側が終わっても、いま動いている側の目印は消さない★★",
              os.path.exists(_lt2) and _a.lost is True)
            with open(_lt2, encoding="utf-8") as _f4:
                t("　残っている印は、奪った側のもの",
                  _f4.read().strip() == _b.token)
            t("　その状態で3つ目は割り込めない",
              _raises(lambda: _OnlyOne(_lt2).__enter__()))
            os.utime(_lt2, (_old2, _old2))
            # ★★時間で合否を決めない★★（2026-08-28・Codexの助言）
            #   ★直す前は「いまとの差が60秒未満」★を見ていたので、
            #   混んでいると落ちる検査だった。
            #   ★見るのは出来事★＝わざと古くした時刻より新しくなったか。
            t("★長い処理は touch() で『まだ動いている』と伝えられる★"
              "／★時計ではなく、古くした時刻より進んだかで見る★",
              _b.touch() and os.path.getmtime(_lt2) > _old2)
            _b.__exit__()
            t("　持ち主が終われば目印は消える", not os.path.exists(_lt2))
        finally:
            for _x in [_lt2] + _stale2:
                if os.path.exists(_x):
                    os.remove(_x)
    finally:
        for _leftover in (_lt,):
            if os.path.exists(_leftover):
                os.remove(_leftover)

    # ★★描き直しの経路（--rebuild-auto）に穴が無いか★★
    #   （2026-08-21・Codexの再指摘）
    #   ★この経路は「公開中の誤りを消す」ためのもの★なので、
    #   区分を動かす仕事はしない。動いていたら断る。
    import subprocess as _sp_rb
    _rb = os.path.join(BASE, "scripts", "build_machine_pages.py")
    with open(_rb, encoding="utf-8") as _f_rb:
        _rb_src = _f_rb.read()
    t("★★描き直しの経路が、いまのページの区分と判定書を突き合わせる★★"
      "（判定書だけ変えて noindex を外せた）",
      "was_noindex = (\"noindex\" in before)" in _rb_src
      and "was_noindex != want_noindex" in _rb_src)
    t("★★描き直しの経路が、記事データと機種データも検査する★★"
      "（check_page だけでは記事の変更がそのまま届いた）",
      "check_detail(slug, detail)" in _rb_src
      and "check_machine(slug, machine)" in _rb_src
      and "check_only_allowed_values(slug, machine, detail, html)" in _rb_src)
    t("　描き直しの経路が check_page に記事データを渡す",
      "check_page(slug, html, expect_noindex=want_noindex," in _rb_src
      and "detail=detail)" in _rb_src)
    t("　描き直しの経路が公開ロックを通る",
      "with _pub._OnlyOne():" in _rb_src)

    # ★sitemap は1文字も変えない★
    with open(SITEMAP, encoding="utf-8") as _f2:
        _sm2 = _f2.read()
    t("★★sitemapは件数が同じでも中身が変われば止める★★"
      "（同数の別URLに差し替えても通っていた）",
      check_sitemap_kept(_sm2.replace("/machines/", "/kikai/", 1)))

    # ★slug そのものを確かめる★（2026-07-31・machines/ の外へ書けた）
    t("★★slug に ../ が入っていたら受け付けない★★（machines/ の外へ書けた）",
      check_slug("../../evil"))
    t("　変な文字も受け付けない",
      check_slug("A B") and check_slug("") and check_slug("1abc"))
    t("　普通のslugは通る", check_slug("lbinko") == [])

    # ★machines.json の既存行が書き換わっていないか★
    _rows_before = [{"slug": "a", "name": "あ"}, {"slug": "b", "name": "い"}]
    _now = _rows_before + [{"slug": "c", "name": "う"}]
    t("　足すだけなら通る",
      _sha(json.dumps(_now[:-1], ensure_ascii=False, sort_keys=True))
      == _sha(json.dumps(_rows_before, ensure_ascii=False, sort_keys=True)))
    _tampered = [{"slug": "a", "name": "書き換え"}, {"slug": "b", "name": "い"},
                 {"slug": "c", "name": "う"}]
    t("★★件数が合っていても既存行が書き換わっていたら気づく★★",
      _sha(json.dumps(_tampered[:-1], ensure_ascii=False, sort_keys=True))
      != _sha(json.dumps(_rows_before, ensure_ascii=False, sort_keys=True)))

    pages = _existing_pages()
    t("★既存ページの指紋を取れる（1枚も変えていないことを確かめるため）★",
      len(pages) >= 100 and all(len(v) == 64 for v in pages.values()))

    t("★★新しいフォルダは中のファイルに開いてから比べる★★"
      "（gitはフォルダごと1行で報告するため、正しい公開を止めていた）",
      not any(x.endswith("/") for x in changed_paths()))
    t("★変えてよいのは決めたものだけ★",
      allowed_paths("zzz") == {"machines/zzz/index.html",
                               "assets/data/machine-details/zzz.json",
                               "assets/data/machines.json"}
      | set(COUNT_FILES) | set(HUB_FILES))
    _real_changed = changed_paths
    try:
        globals()["changed_paths"] = lambda: ["assets/css/practical.css"]
        _snap = snapshot(["assets/css/practical.css"])
        t("　何も変えていなければ通る（＝誤検知しない）",
          check_no_stray_changes("zzz", _snap) == [])
        t("★★もともと変更中だったファイルを、さらに書き換えたら気づく★★"
          "（名前で除外していたので見逃していた）",
          any("practical.css" in x for x in
              check_no_stray_changes("zzz", {"assets/css/practical.css": "ちがう指紋"})))
        globals()["changed_paths"] = lambda: ["assets/img/logo.png"]
        t("★許していないファイルが増えたら気づく★",
          any("増えました" in x for x in check_no_stray_changes("zzz", {})))
    finally:
        globals()["changed_paths"] = _real_changed

    with open(SITEMAP, encoding="utf-8") as _f3:
        _sm = _f3.read()
    t("　sitemapが変わっていなければ通る", check_sitemap_kept(_sm) == [])
    t("★★sitemapが1件でも増減したら止める★★",
      check_sitemap_kept(_sm + "<url>x</url>"))
    # ★対象が1機種も無い日がある★（2026-08-06。旧preview7機種を全部
    #   完成記事へ上げた日に、この試験が StopIteration で落ちた）
    import page_decision as _pd2
    _np = next((m["slug"] for m in rows
                if _pd2.machine_class(m) in ("LEGACY_PREVIEW", "AUTO_PENDING")),
               None)
    if _np:
        # ★落ちた理由をその場で出す★（2026-08-21）
        #   ＝理由を言わない赤は、CIで見ても手元で見ても直せない。
        _served = check_served(_np)
        if _served:
            print(f"   （{_np} を引けなかった理由: " + " / ".join(_served) + "）")
        t("★★実際にHTTPで引いて200とnoindexを確かめられる★★"
          "（ファイルがあるだけでは足りない）", _served == [])
    else:
        t("　検索に載せない機種が1つも無いので、この確認は行わない"
          "（★対象が無いこと自体は正常★）", True)
    t("　存在しない機種なら引けないと分かる",
      any("引けません" in x for x in check_served("zzz_nothing_here")))
    # ★★つながらなかっただけで落とさない★★（2026-08-22）
    #   ★実際に起きたこと★＝この試験が CI で1回だけ落ち、
    #   同じ内容をやり直したら緑になった（コード側は無関係）。
    #   ★たまに落ちる検査は、本物の赤と見分けが付かなくなる★。
    #   ★ただし「サーバーが答えている」ものは繰り返さない★＝
    #   404 は何度引いても404なので、待ち時間を無駄にしない。
    #   ★時間では測らない★＝混んでいると落ちる「たまに落ちる検査」になる。
    #   ★何回呼んだかを数える★
    import urllib.error as _ue_t

    def _counting(exc):
        n = {"n": 0}

        def _op(url, timeout=None):
            n["n"] += 1
            raise exc
        return _op, n

    _op, _n = _counting(_ue_t.HTTPError("u", 404, "nf", {}, None))
    t("★★サーバーが答えているとき（404）は入れ直さない★★"
      "（繰り返しても答えは変わらないので待つだけ無駄）",
      _raises(lambda: _get_with_retry("http://x/", opener=_op)) and _n["n"] == 1)

    _op2, _n2 = _counting(_ue_t.URLError("つながらない"))
    t("★つながらないときは入れ直す（3回まで）★",
      _raises(lambda: _get_with_retry("http://x/", opener=_op2)) and _n2["n"] == 3)

    # ★★書き込みのどこで失敗しても、中途半端な状態を残さない★★
    #   （2026-07-31・Codexが最も勧めた「障害注入」）
    #   各書き込み地点をわざと失敗させ、
    #   毎回「完全に元のまま」に戻ることを確かめる。
    import shutil as _sh
    import tempfile as _tf4
    _dir4 = _tf4.mkdtemp(prefix="uchi_fault_")
    # ★★写しの上でやる★★（2026-08-24・Codexが3回すすめた）
    #   ★直す前は本番のファイルへ実際に書いてから戻していた★ので、
    #     ①強制終了されると巻き戻しが走らず、偽の機種が残る
    #     ②戻す処理そのものが、同時に行われた人の作業を消し得る
    #     ③本番のファイルが一瞬だけ食い違い、監査やpushがその途中を見る
    #   ★掃除で受け止めるのをやめ、そもそも本番を触らない★
    _work4 = os.path.join(_dir4, "work")
    #   ★.git も写す★＝掃除は git に問い合わせて「元の中身」を決めるので、
    #   除くと**その守りを写しの上では一度も確かめられない**
    #   （2026-08-24に実際そうなった。CI再現の道具でも同じ罠を踏んでいる）。
    # ★★写している最中にリポジトリが動いても壊れないこと★★
    #   （2026-08-28・わざと再現して確かめた）
    #   直下でファイルを作っては消しながら走らせると、
    #   ★写しを作る処理が例外で落ちる★／紛れ込むと巻き戻しの検査が
    #   「許していないファイルが増えた」と見なして落ちる。
    #   ①消えたファイルは飛ばす（途中で消えたのは写しの失敗ではない）
    #   ②誰かの作業中のファイルは最初から写さない
    _sh.copytree(BASE, _work4, copy_function=copy_tolerant,
                 ignore=_sh.ignore_patterns(
                     "__pycache__", "node_modules", ".preview-site", "_site",
                     # ★自分が作る作業ファイルの形も入れる★
                     #   （2026-08-28・Codexの12回目）
                     #   `*.tmp` では `.tmp.<番号>` `.new.<番号>` に
                     #   ★当たらない★（書き込みの途中で必ず作る形）。
                     "*.tmp", "*.tmp.*", "*.new.*", "*.recover.*",
                     ".render_check_*", ".probe_churn_*",
                     ".publish.lock.*"))
    _real_base = BASE
    _real_ip = IN_PROGRESS
    globals()["BASE"] = _work4
    globals()["IN_PROGRESS"] = os.path.join(_work4,
                                            ".publish-in-progress.json")
    # ★写しの側の場所へ向け直す★（読み込み時に決まった定数を持っているもの）
    # ★★向け直し漏れが1つでもあると、本物のリポジトリへ書く★★
    #   （2026-08-24・実際に踏んだ）＝
    #   `BASE` `IN_PROGRESS` `MACHINES` だけ直していたので、
    #   記事データは `DETAILS`（読み込み時に決まる）を通って
    #   **本物のリポジトリへ書かれていた**。
    #   しかも後片づけの確認は**写しの側**を見るので、
    #   ★試験は「残っていません」と言いながら残していた★。
    #   ＝夜の公開が「許していないファイル」で丸ごと止まる経路。
    #   → ★BASE から作る定数は、機械的に全部たどって向け直す★
    #     （名前を並べると、次に足した定数でまた漏れる）。
    _bytes4 = {}
    _real = {"write_atomic": write_atomic, "build_hubs": build_hubs,
             "check_served": check_served, "run_site_audit": run_site_audit,
             "check_after": check_after}
    for _k4, _v4 in list(globals().items()):
        if (_k4.isupper() and isinstance(_v4, str)
                and _v4.startswith(_real_base + os.sep)):
            _real[_k4] = _v4
            globals()[_k4] = _work4 + _v4[len(_real_base):]
    # ★向け直せたことを、試験のはじめに確かめる★（黙って漏れない）
    _leak4 = [k for k, v in globals().items()
              if k.isupper() and isinstance(v, str)
              and v.startswith(_real_base + os.sep)]
    t("★★写しの上で試すとき、本物を指す場所が1つも残らない★★"
      "／★残ると本物のリポジトリを汚し、夜の公開が止まる★"
      + ("／残り: " + "／".join(_leak4) if _leak4 else ""),
      not _leak4)
    try:
        def _snapshot():
            """公開に関わるファイルの指紋（元のままか確かめる用）。"""
            out = {}
            for rel in list(HUB_FILES) + ["assets/data/machines.json"]:
                full = os.path.join(BASE, rel)
                with open(full, encoding="utf-8") as f:
                    # ★改行コードの違いは「戻っていない」と数えない★
                    #   巻き戻しは書き直すので改行がそろう。中身が同じなら戻っている。
                    out[rel] = _sha(f.read().replace(chr(13) + chr(10), chr(10)))
            return out

        _slug4 = "zzz_fault_test"
        _mat4 = {"adopted": {}, "need_third": {}, "thin": {}}
        _before4 = _snapshot()

        def _try_with(name, breaker, skip=0, need_fire=True):
            """★skip 回は本物を通し、その次の呼び出しで壊す★

            ★なぜ要るか（2026-08-24・Codexの19回目）★
              `build_hubs` と `run_site_audit` は**書き込みより前にも**呼ばれる
              （公開前の監査と、早見表がズレていないかの確認）。
              そのまま壊すと**書き込み前に止まる**ので、
              「元のまま」なのは当たり前＝★巻き戻しを一度も試していない★。
              ＝壊し方が守りではなく手前の関門を叩いていた（型⑧の変種）。
            """
            _seen = {"n": 0, "fired": False}
            _orig = _real[name]

            def _gate(*a, **k):
                if _seen["n"] < skip:
                    _seen["n"] += 1
                    return _orig(*a, **k)
                _seen["fired"] = True
                return breaker(*a, **k)

            globals()[name] = _gate
            try:
                publish_from_material(_slug4, "障害注入確認機", "bellco",
                                      f"https://m.example/products/slot/{_slug4}/",
                                      "2026-09", _mat4, apply_it=True)
            except BaseException:                       # noqa: BLE001
                pass
            finally:
                globals()[name] = _real[name]
            ok = (_snapshot() == _before4
                  and not os.path.isdir(os.path.join(BASE, "machines", _slug4))
                  and not os.path.isfile(os.path.join(
                      BASE, "assets", "data", "machine-details", f"{_slug4}.json")))
            # 後片付け（失敗しても残っていたら消す）
            _sh.rmtree(os.path.join(BASE, "machines", _slug4), ignore_errors=True)
            dp4 = os.path.join(BASE, "assets", "data", "machine-details",
                               f"{_slug4}.json")
            if os.path.isfile(dp4):
                os.remove(dp4)
            # ★★壊し方が実際に発火したことを要求する★★
            #   ★手前で止まって一度も呼ばれなくても「元のまま」で合格した★
            #   ★need_fire=False は「別の場所を壊す」使い方★＝
            #     置き換え直後の中断のように、この関数は素通しにして
            #     `os.replace` の側を壊す場合。発火は呼び出し側が確かめる。
            if need_fire and not _seen["fired"]:
                return False
            return ok

        def _boom(*_a, **_k):
            raise RuntimeError("わざと失敗させました")

        def _interrupt(*_a, **_k):
            raise KeyboardInterrupt()

        # ★★発火要求そのものを直接試す★★（2026-08-24）
        #   ★壊し方の通し確認で「壊しても試験が緑」と出た★＝
        #   いまの使い方ではどれも発火するので、この守りを見ている試験が無い。
        #   ＝将来、呼ばれる順番が変わって手前で止まるようになっても気づけない。
        t("★★壊し方が一度も発火しなければ、合格にしない★★"
          "／★これが無いと『元のまま』が当たり前の状態で緑になる★",
          _try_with("check_after", _boom, skip=99) is False)
        # ★書き込みより前で壊れる場合★（手前の関門が働くこと）
        t("　早見表の作成が公開前に壊れたら、そもそも書き始めない",
          _try_with("build_hubs", _boom))
        t("　公開前の監査が壊れたら、そもそも書き始めない",
          _try_with("run_site_audit", lambda *a, **k: ["わざとNG"]))
        # ★★書き込みのあとで壊れる場合★★（巻き戻しが働くこと）
        #   ★ここが本命★＝作ってしまったものを全部戻せるか。
        t("★★早見表を作り直す所（書き込み後）で失敗しても、完全に元のまま★★",
          _try_with("build_hubs", _boom, skip=1))
        t("★★配信の確認で失敗しても、完全に元のまま★★",
          _try_with("check_served", _boom))
        t("★★最後の監査（書き込み後）で失敗しても、完全に元のまま★★",
          _try_with("run_site_audit", lambda *a, **k: ["わざとNG"], skip=1))
        t("★★最終確認で失敗しても、完全に元のまま★★",
          _try_with("check_after", lambda *a, **k: ["わざとNG"]))
        t("★★Ctrl+C（中断・書き込み後）でも、完全に元のまま★★",
          _try_with("build_hubs", _interrupt, skip=1))
        # ★置き換えた直後に中断される狭い窓★（Codex指摘・実際に再現した）
        _real_replace = os.replace
        for _nth in (1, 2, 3, 4, 5):
            _cnt = {"i": 0}

            def _replace_then_stop(src, dst, _n=_nth, _c=_cnt):
                _real_replace(src, dst)
                _c["i"] += 1
                if _c["i"] == _n:
                    raise KeyboardInterrupt()

            os.replace = _replace_then_stop
            try:
                _ok = _try_with("check_served", _real["check_served"],
                                need_fire=False)
            finally:
                os.replace = _real_replace
            # ★★中断が実際に起きたことを確かめる★★（2026-08-24）
            #   ★置き換えが _nth 回に届かなければ、何も試していない★
            t(f"★★{_nth}回目の置き換え直後に中断されても元のまま★★",
              _ok and _cnt["i"] >= _nth)
        # ★復旧が途中で落ちても、もう一度走らせて完走できるか★
        #   （2026-07-31・Codex12回目「各中断点から再開できるか」）
        # ★★本物の強制終了が残す状態から始める★★（2026-08-24・Codexの19回目）
        #   ★直す前は失敗させるだけだった★ので、巻き戻しが全部片付けてしまい、
        #   **復旧の対象が一度もできていなかった**（＝何も試していなかった）。
        #   ★書いた中身を変えてから失敗させる手も採らない★＝
        #     復旧は「作ったときと中身が違う」ものを**わざと消さない**（正しい）。
        #     それでは復旧が完走しないので、試験の題材として誤り。
        #   → ★別のプロセスで公開を始め、書き終えた直後に強制終了させる★。
        #     巻き戻しも目印の後始末も走らない＝本番の電源断と同じ形。
        _s5 = "zzz_resume_test"
        _dp5 = os.path.join(BASE, "assets", "data", "machine-details",
                            f"{_s5}.json")
        _kill_py = os.path.join(BASE, "_zzz_kill_after_write.py")
        with open(_kill_py, "w", encoding="utf-8") as _fh:
            _fh.write(
                "import os, sys" + chr(10)
                + "sys.path.insert(0, os.path.join(os.path.dirname("
                + "os.path.abspath(__file__)), 'scripts'))" + chr(10)
                + "import publish_new_machine as P" + chr(10)
                + "P.check_served = lambda *a, **k: os._exit(9)" + chr(10)
                + "P.publish_from_material(" + repr(_s5)
                + ", '再開確認機ZZZ', 'bellco', "
                + repr(f"https://m.example/products/slot/{_s5}/")
                + ", '2026-09', {'adopted': {}, 'need_third': {}, "
                + "'thin': {}}, apply_it=True)" + chr(10))
        try:
            _kill_rc = subprocess.run([sys.executable, _kill_py], cwd=BASE,
                                      capture_output=True,
                                      timeout=600).returncode
        finally:
            if os.path.isfile(_kill_py):
                os.remove(_kill_py)
        t("　強制終了の試験が、実際に強制終了で終わっている", _kill_rc == 9)
        # ★★復旧が要る状態になっているか★★（ここが無いと何も試していない）
        _pg5 = os.path.join(BASE, "machines", _s5, "index.html")
        _made5 = (os.path.isfile(_dp5) and os.path.isfile(_pg5)
                  and os.path.exists(IN_PROGRESS))
        t("　強制終了は、ページ・記事データ・目印を残している", _made5)
        # ★★①強制終了そのままの形から復旧できるか★★
        #   （2026-08-24・ここで**本物の不具合**が出た）
        #   ★退避先はその機種ディレクトリの中★なので、監査から見ると
        #   **常に孤児ディレクトリが残っている**ように見え、
        #   復旧は自分の監査に落ちて**何度やっても目印が消えなかった**。
        #   ＝目印が残る＝以後の新台公開が**全部止まる**（誰にも通知されない）。
        _r5a = recover(apply_it=True)
        t("★★強制終了そのままの形から、復旧が完走する★★"
          "／★ここが詰まると新台の公開が永久に止まる★",
          not _r5a["problems"] and not os.path.exists(IN_PROGRESS)
          and not os.path.isdir(os.path.join(BASE, "machines", _s5))
          and not os.path.isfile(_dp5))
        # ★★②復旧が途中で落ちた形（ページだけ消えた）からも復旧できるか★★
        _kill2 = os.path.join(BASE, "_zzz_kill_after_write2.py")
        with open(_kill2, "w", encoding="utf-8") as _fh:
            _fh.write(
                "import os, sys" + chr(10)
                + "sys.path.insert(0, os.path.join(os.path.dirname("
                + "os.path.abspath(__file__)), 'scripts'))" + chr(10)
                + "import publish_new_machine as P" + chr(10)
                + "P.check_served = lambda *a, **k: os._exit(9)" + chr(10)
                + "P.publish_from_material(" + repr(_s5)
                + ", '再開確認機ZZZ', 'bellco', "
                + repr(f"https://m.example/products/slot/{_s5}/")
                + ", '2026-09', {'adopted': {}, 'need_third': {}, "
                + "'thin': {}}, apply_it=True)" + chr(10))
        try:
            subprocess.run([sys.executable, _kill2], cwd=BASE,
                           capture_output=True, timeout=600)
        finally:
            if os.path.isfile(_kill2):
                os.remove(_kill2)
        if os.path.isfile(_pg5):
            os.remove(_pg5)                  # ★復旧の途中で落ちた状態★
        _r5 = recover(apply_it=True)
        _left5 = (os.path.isdir(os.path.join(BASE, "machines", _s5))
                  or os.path.isfile(os.path.join(
                      BASE, "assets", "data", "machine-details", f"{_s5}.json"))
                  or any(m.get("slug") == _s5 for m in _sj.read_rows(MACHINES)))
        # ★★「作れなかったら合格」をやめる★★（2026-08-24・Codexの19回目）
        #   ★直す前は `not _made5 or …` だった★ので、
        #   **復旧の対象を作れなかった回も合格**していた（＝何も試していない）。
        t("★★復旧が途中で落ちても、もう一度走らせれば完走する★★"
          + ("" if not _r5["problems"]
             else "／理由: " + " / ".join(str(x) for x in _r5["problems"])[:200])
          + ("" if not _left5 else "／残骸あり"),
          not _left5 and not _r5["problems"])
        _sh.rmtree(os.path.join(BASE, "machines", _s5), ignore_errors=True)
        if os.path.exists(IN_PROGRESS):
            os.remove(IN_PROGRESS)
        t("　中途半端な一時ファイルを残さない",
          not [x for x in os.listdir(BASE) if ".tmp." in x or ".new." in x])
        # ★★偽の機種を1件も残さない★★（2026-08-24）
        #   ★強制終了で実際に残った★＝公開前の関所が
        #   「許していないファイル」と見なし、夜の公開を丸ごと止める。
        purge_test_residue(apply_it=True)
        t("★★試験用の偽の機種を1件も残さない★★"
          "（残すと夜の公開が丸ごと止まる）",
          purge_test_residue(apply_it=False) == [])
        # ★★掃除そのものを試す★★（2026-08-24・壊し方12が赤くならなかった）
        #   ★うまくいった回には残骸が出ない★ので、
        #   「最後に残っていない」だけでは掃除が働いた証拠にならない。
        #   （実際、掃除を止めても試験は緑のままだった）
        #   → わざと残骸を置いて、消えることを確かめる。
        _probe = "zzz_purge_probe"
        _pd_dir = os.path.join(BASE, "machines", _probe)
        _pd_json = os.path.join(BASE, "assets", "data", "machine-details",
                                f"{_probe}.json")
        os.makedirs(_pd_dir, exist_ok=True)
        with open(os.path.join(_pd_dir, "index.html"), "w",
                  encoding="utf-8") as _fh:
            _fh.write("<!-- 掃除の試験用 -->")
        with open(_pd_json, "w", encoding="utf-8") as _fh:
            _fh.write("{}")
        _saw = purge_test_residue(apply_it=True)
        t("★★わざと置いた残骸を、実際に消す★★"
          "（『残っていない』ではなく『消した』を確かめる）",
          any(_probe in x for x in _saw)
          and not os.path.isdir(_pd_dir)
          and not os.path.isfile(_pd_json))
        # ★目印の残骸も同じように確かめる★
        with open(IN_PROGRESS, "w", encoding="utf-8") as _fh:
            _fh.write('{"slug": "zzz_marker_probe", "name": "試験"}')
        _saw2 = purge_test_residue(apply_it=True)
        t("★★試験用の公開途中の目印も消す★★"
          "（残すと以後の新台追加が永久に止まる）",
          any("zzz_marker_probe" in x for x in _saw2)
          and not os.path.isfile(IN_PROGRESS))
        # ★対照★ 本物の公開途中には触らない
        with open(IN_PROGRESS, "w", encoding="utf-8") as _fh:
            _fh.write('{"slug": "hokuto", "name": "本物"}')
        purge_test_residue(apply_it=True)
        t("　（対照）本物の公開途中の目印には触らない",
          os.path.isfile(IN_PROGRESS))
        os.remove(IN_PROGRESS)
        # ★★機種一覧に入り込んだ偽の機種も戻せる★★
        #   （2026-08-24・Codexの4回目の指摘＝行の字面では戻せなかった）
        #   ★1機種は十数行の塊で、その中に zzz_ を含まない行が必ずある★ので、
        #   「動いた行が全部 zzz_ を含む時だけ戻す」では**一度も戻らなかった**。
        import json as _js9
        _rows9 = _sj.read_rows(MACHINES)
        _n9 = len(_rows9)
        _rows9.append({"slug": "zzz_purge_json", "name": "掃除試験ZZZ",
                       "seo": {"title": "x"}, "info": "", "strategy": "",
                       "aliases": []})
        with open(MACHINES, "w", encoding="utf-8", newline="\n") as _fh9:
            _fh9.write(_js9.dumps(_rows9, ensure_ascii=False, indent=1) + "\n")
        _saw9 = purge_test_residue(apply_it=True)
        _after9 = _sj.read_rows(MACHINES)
        t("★★機種一覧に入り込んだ偽の機種を戻せる★★"
          "（行の字面ではなく、中身で判断する）",
          any("machines.json" in x for x in _saw9)
          and len(_after9) == _n9
          and not [r for r in _after9
                   if str(r.get("slug") or "").startswith(TEST_SLUG_PREFIX)])
    finally:
        for k, v in _real.items():
            globals()[k] = v
        globals()["IN_PROGRESS"] = _real_ip
        globals()["BASE"] = _real_base          # ★本番へ戻す★
        # ★★戻すのは「試験が汚した時」だけ★★（2026-08-24・Codexの4回目の指摘）
        #   ★直す前は無条件に書き戻していた★＝
        #   試験の最中に人や別の処理が正当な変更をしていたら、
        #   ★それを黙って消していた★（追跡ファイルなので git には残るが、
        #   作業中の変更は失われる）。
        #   → いまの中身に試験用の印（zzz_）が無く、かつ元と違うなら、
        #     それは**誰かの正当な変更**なので触らずに知らせる。
        #   ★★「印があれば全部戻す」でもまだ足りない★★
        #   （2026-08-24・Codexの5回目）＝試験の印が残っている最中に
        #   人が正当な編集を加えると、**印があることを理由に丸ごと書き戻す**。
        #   → 中身で判断する＝「試験用の機種を取り除いたら、
        #     始めたときと同じ中身になるか」。ならなければ触らない。
        for _f4, _b4 in _bytes4.items():
            with open(_f4, "rb") as _fh4:
                _now4 = _fh4.read()
            if _now4 == _b4:
                continue
            if not _only_test_added(_now4, _b4, _f4):
                print(f"⚠ {os.path.relpath(_f4, BASE)} は試験の外でも"
                      "変わっているので戻しません（人の作業を消さないため）")
                continue
            with open(_f4, "wb") as _fh5:
                _fh5.write(_b4)
        rmtree_hard(_dir4)          # ★読み取り専用でも消す★

    # ★項目23（説明書の大きさ）だけが赤なら公開は止めない★
    #   （2026-08-10・依頼133 P1。8行の変更に回帰テストが無かった）
    _real_run = __import__("subprocess").run

    def _fake_audit(only):
        import json as _j
        import subprocess as _sp

        def _run(cmd, **kw):
            if any("audit_site.py" in str(c) for c in cmd):
                body = {k: ([] if k != only else ["わざと赤にした"])
                        for k, _f in _as_mod.CHECKS}
                return _sp.CompletedProcess(cmd, 0, _j.dumps(body), "")
            return _real_run(cmd, **kw)
        return _run

    import audit_site as _as_mod
    import subprocess as _sp_mod
    try:
        _sp_mod.run = _fake_audit("23_CLAUDE_md肥大検知")
        t("★★項目23だけが赤でも公開は止めない★★（記事の正しさと無関係）",
          run_site_audit() == [])
        _sp_mod.run = _fake_audit("24_noindex整合")
        t("★★記事に関わる項目が赤なら止める★★（23を外したせいで素通りしない）",
          any("24_" in x for x in run_site_audit()))
    finally:
        _sp_mod.run = _real_run


    # ★試験が作った一時の置き場を片づける★（2026-08-28・Codexの12回目）
    for _td0 in (_lockdir, _cpdir):
        rmtree_hard(_td0)          # ★読み取り専用でも消す★

    ng = [n for n, ok in results if not ok]
    print(f"{nl}{len(results) - len(ng)}/{len(results)} 合格")
    if ng:
        print("失敗:", ng)
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--slug")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--recover", action="store_true",
                    help="途中で終わった公開を処理前へ戻す")
    ap.add_argument("--material", help="採用済みの材料（JSONファイル）")
    ap.add_argument("--name", help="メーカー公式の正式名称")
    ap.add_argument("--maker", help="メーカーID")
    ap.add_argument("--official-url", dest="official_url", help="公式ページURL")
    ap.add_argument("--release", default="", help="登場年月 YYYY-MM")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if args.recover:
        r = recover(apply_it=args.apply)
        for x in r["todo"]:
            print("  " + x)
        for x in r["restored"]:
            print("  戻しました: " + x)
        for x in r["problems"]:
            print("  ✗ " + x[:160])
        if not args.apply and not r["problems"]:
            print("★確認だけです。実際に戻すには --recover --apply★")
        return 1 if r["problems"] else 0
    if not args.slug:
        ap.print_help()
        return 0
    # ★公開できるのは材料からだけ★（2026-07-31・Codex指摘1）
    #   以前は完成した機種データ・記事データを受け取って publish() を直接呼べた。
    #   それだと「数値を含まない誤った文章」や
    #   「別項目の数値を置いたデータ」をそのまま公開できてしまう。
    if not (args.material and args.name and args.maker and args.official_url):
        print("★材料と機種の情報が要ります★")
        print("  --material <材料JSON> --name <正式名称> "
              "--maker <メーカーID> --official-url <公式URL> [--release YYYY-MM]")
        print("  （ふだんは add_machine_run.py --apply が中で呼びます）")
        return 1
    # ★外部の材料JSONからは公開（--apply）できない★（2026-08-03・Codex58回目）
    #   ファイルの中身は「2出典で確認済み」の再検証を通らないので、
    #   誤ったJSONや手打ちの値をそのまま「出典2件で一致」として
    #   記事化できてしまう。下見（--applyなし）だけ許し、
    #   公開は材料収集から検証込みで行う add_machine_run.py --apply 経由に限る。
    if args.apply:
        print("★外部の材料JSONからの公開（--apply）はできません★")
        print("  出典の再検証を通らない値を記事化できてしまうため、"
              "このコマンドは下見（--applyなし）専用です。")
        print("  公開は python scripts/add_machine_run.py --apply を使ってください。")
        return 1
    material = _sj.read_json(args.material, expect=dict)
    res = publish_from_material(args.slug, args.name, args.maker,
                                args.official_url, args.release or "",
                                material, apply_it=False)
    if res["problems"]:
        print("★公開できません★")
        for p in res["problems"]:
            print("  ✗ " + p[:160])
        return 1
    if args.apply:
        print("公開しました:")
        for w in res["wrote"]:
            print("   " + os.path.relpath(w, BASE).replace(os.sep, "/"))
    else:
        print(f"確認だけ済みました（問題なし・{res['html_bytes']} バイトのページを作れます）")
        print("  実際に書くには --apply を付けてください")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except (PublishError, _sj.SafeJsonError) as e:
        print(f"★{e}★")
        raise SystemExit(1)
    except Exception as e:                # noqa: BLE001
        print(f"★想定外の失敗 {type(e).__name__}: {e}★")
        raise SystemExit(1)

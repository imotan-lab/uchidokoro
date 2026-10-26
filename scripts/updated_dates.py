"""★機種ごとの「最終更新日」を git の記録から作る★（2026-10-26・運営者「更新日は修正するようにして」）

使い方:
  python scripts/updated_dates.py --write assets/data/updated-dates.json   … 作って書く（公開の直前に GitHub が呼ぶ）
  python scripts/updated_dates.py --slug hokuto                            … 1機種だけ出す（確かめる用）
  python scripts/updated_dates.py --selftest

★なぜ git から作るか★＝記事データの updated 欄は、記事を直す道具が書き換えておらず、
  128機種中50機種ほどが半年前の日付のまま残っていた（北斗は10/24に直したのに古い日付）。
  道具ごとに書かせると、1つ書き忘れるだけで嘘の日付が出る。git の記録は書き忘れが起きない。
★日付の決め方★＝その機種の
  ①記事データ（assets/data/machine-details/<slug>.json）の中身が変わった最後のコミット
  ②機種データ（machines.json の、その機種の行）の中身が変わった最後のコミット
  の遅いほうの日（日本時間）。★並べ替えだけ・書式だけの変更は数えない★（JSON として比べる）。
★作れなかったら書かない★＝公開は止めず、ページは日付を出さない（間違った日付を見せない）。
★リポジトリには置かない★（.gitignore）＝公開の直前に作る生成物。置くとコミットのたびに古くなる。
"""
import argparse
import datetime as _dt
import json
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MACHINES = "assets/data/machines.json"
DETAILS = "assets/data/machine-details"
JST = _dt.timezone(_dt.timedelta(hours=9))


def _git(args, root=BASE) -> str:
    r = subprocess.run(["git", "-C", root] + args, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} が失敗しました: {r.stderr.strip()[:200]}")
    return r.stdout


def _day(iso: str) -> str:
    """コミットの日時（ISO）を日本時間の日付にする"""
    return _dt.datetime.fromisoformat(iso.strip()).astimezone(JST).date().isoformat()


def _canon(obj, drop=()) -> str:
    if isinstance(obj, dict):
        obj = {k: v for k, v in obj.items() if k not in drop}
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def _history(path: str, root=BASE) -> list:
    """そのファイルを変えたコミット（古い順）＝[(sha, 日付)]"""
    out = _git(["log", "--reverse", "--format=%H %cI", "--", path], root)
    return [(x.split(" ", 1)[0], _day(x.split(" ", 1)[1])) for x in out.splitlines() if x.strip()]


MISSING = "<そのコミットには無い>"
BROKEN = "<JSONとして読めない版>"


def _show(sha: str, path: str, root=BASE):
    """★「無い」と「読めない」を分ける★（review282 の3）＝git が失敗したのに「無い」と読むと、
    最新の変更を読み落として古い日付のまま正常に終わる。★git の失敗は例外★（作れなければ公開側は日付を出さない）"""
    # ★「無い」は、そのコミットの一覧に名前が無いと確かめられたときだけ★（review283）＝
    #   一覧に在るのに中身が読めないものを「無い」と読むと、古い日付のまま正常に終わる
    t = subprocess.run(["git", "-C", root, "ls-tree", sha, "--", path], capture_output=True)
    if t.returncode != 0:
        raise RuntimeError(f"コミット {sha[:8]} を読めません")
    if not t.stdout.strip():
        return MISSING                   # コミットは在るが、そのファイルが無い（消された・まだ無い）
    r = subprocess.run(["git", "-C", root, "show", f"{sha}:{path}"], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"{sha[:8]}:{path} を読めません: {r.stderr.decode('utf-8', 'replace')[:200]}")
    try:
        return json.loads(r.stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return BROKEN                    # 壊れた版も「中身の1つの姿」として比べる


def row_dates(root=BASE) -> dict:
    """★機種データの行の中身が最後に変わった日★（並べ替えだけは数えない）"""
    last: dict = {}
    prev: dict = {}
    for sha, day in _history(MACHINES, root):
        data = _show(sha, MACHINES, root)
        if not isinstance(data, list):
            data = []                    # 無い・壊れた版＝どの機種の行も無い姿（戻ったら変わったと数える）
        cur = {str(m.get("slug")): _canon(m) for m in data if isinstance(m, dict) and m.get("slug")}
        for s, c in cur.items():
            if prev.get(s) != c:
                last[s] = day
        prev = cur
    return last


def detail_dates(slugs, root=BASE, until: str = "") -> dict:
    """★記事データの中身が最後に変わった日★（updated 欄だけの変更は数えない＝道具が書いていない古い欄）"""
    out = {}
    for s in slugs:
        path = f"{DETAILS}/{s}.json"
        prev = None
        for sha, day in _history(path, root):
            if until and day > until:
                break
            d = _show(sha, path, root)
            # ★消えた版も1つの姿として比べる★（review282 の2）＝飛ばすと、消して同じ中身で戻したとき
            #   戻した日が数えられず、消す前の日付が残る
            c = d if d in (MISSING, BROKEN) else _canon(d, drop=("updated",))
            if c != prev and c not in (MISSING, BROKEN):
                out[s] = day
            prev = c
    return out


def build(root=BASE) -> dict:
    ms = json.loads(_git(["show", f"HEAD:{MACHINES}"], root))
    slugs = [str(m["slug"]) for m in ms if isinstance(m, dict) and m.get("slug")]
    rows = row_dates(root)
    dets = detail_dates(slugs, root)
    dates = {}
    for s in slugs:
        got = [x for x in (rows.get(s), dets.get(s)) if x]
        if got:
            dates[s] = max(got)
    return {"schema_version": "updated-dates/v1",
            "_why": "機種ごとの最終更新日（記事データか機種データの中身が最後に変わった日・日本時間）。公開の直前に git の記録から作る",
            "dates": dates}


def selftest() -> int:
    import shutil
    import tempfile
    results = []

    def t(name, cond):
        results.append(bool(cond))
        print(("✅" if cond else "❌") + " " + name)

    tmp = tempfile.mkdtemp(prefix="upd_")
    try:
        def g(*a, date=None):
            env = dict(os.environ)
            if date:
                env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
            subprocess.run(["git", "-C", tmp] + list(a), check=True, capture_output=True, env=env)

        def put(path, obj):
            p = os.path.join(tmp, path)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=1)

        g("init", "-q")
        g("config", "user.email", "t@example.invalid")
        g("config", "user.name", "t")
        A = {"slug": "a", "name": "A", "checker": {"normal": {"good": 400}}}
        B = {"slug": "b", "name": "B"}
        put(MACHINES, [A, B])
        put(f"{DETAILS}/a.json", {"slug": "a", "lead": "最初", "updated": "2026-01-01"})
        put(f"{DETAILS}/b.json", {"slug": "b", "lead": "最初"})
        g("add", "-A")
        g("commit", "-q", "-m", "1", date="2026-03-01T10:00:00+09:00")
        # 並べ替えだけ（数えない）
        put(MACHINES, [B, A])
        g("commit", "-q", "-am", "2", date="2026-04-01T10:00:00+09:00")
        # a の行の中身を変える
        put(MACHINES, [B, dict(A, checker={"normal": {"good": 450}})])
        g("commit", "-q", "-am", "3", date="2026-05-01T23:30:00+09:00")
        # b の記事の中身を変える（日本時間では 6/2）
        put(f"{DETAILS}/b.json", {"slug": "b", "lead": "直した"})
        g("commit", "-q", "-am", "4", date="2026-06-01T16:00:00+00:00")
        # a の記事の updated 欄だけを変える（数えない）
        put(f"{DETAILS}/a.json", {"slug": "a", "lead": "最初", "updated": "2026-07-01"})
        g("commit", "-q", "-am", "5", date="2026-07-01T10:00:00+09:00")
        # b の記事を消して、同じ中身で戻す（戻した日を採る）
        os.remove(os.path.join(tmp, DETAILS, "b.json"))
        g("commit", "-q", "-am", "6", date="2026-08-01T10:00:00+09:00")
        put(f"{DETAILS}/b.json", {"slug": "b", "lead": "直した"})
        g("add", "-A")
        g("commit", "-q", "-m", "7", date="2026-09-01T10:00:00+09:00")
        d = build(tmp)["dates"]
        t("★★記事を消して同じ中身で戻したら、戻した日を採る★★（消す前の日付を残さない・review282 の2）",
          d.get("b") == "2026-09-01")
        try:
            _show("0" * 40, MACHINES, tmp)
            t("★読めないコミットは例外にする★（「無い」と読んで古い日付で終わらない・review282 の3）", False)
        except RuntimeError:
            t("★読めないコミットは例外にする★（「無い」と読んで古い日付で終わらない・review282 の3）", True)
        t("★行の中身が変わった日を採る★（a は 5/1）", d.get("a") == "2026-05-01")
        t("★並べ替えだけは更新に数えない★（b は並べ替えの 4/1 ではなく記事の日）", d.get("b") != "2026-04-01")
        t("★記事の中身が変わった日を、日本時間で採る★（消す前は b が 6/2）",
          detail_dates(["b"], tmp, until="2026-07-31").get("b") == "2026-06-02")
        t("★記事の updated 欄だけの変更は数えない★（a は 7/1 にならない）", d.get("a") != "2026-07-01")
        # ★一覧に在るのに中身が読めない★（中身の置き場を壊して確かめる）
        blob = subprocess.run(["git", "-C", tmp, "rev-parse", f"HEAD:{DETAILS}/b.json"],
                              capture_output=True, text=True).stdout.strip()
        obj = os.path.join(tmp, ".git", "objects", blob[:2], blob[2:])
        try:
            if os.path.exists(obj):
                os.chmod(obj, 0o666)
                os.remove(obj)
            _show("HEAD", f"{DETAILS}/b.json", tmp)
            t("★★一覧に在るのに中身が読めないものは例外にする★★（「無い」と読まない・review283）", False)
        except RuntimeError:
            t("★★一覧に在るのに中身が読めないものは例外にする★★（「無い」と読まない・review283）", True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} 合格")
    return 0 if all(results) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="機種ごとの最終更新日を git の記録から作る")
    ap.add_argument("--write", default="")
    ap.add_argument("--slug", default="")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    data = build()
    if a.slug:
        print(a.slug, data["dates"].get(a.slug, "（日付なし）"))
        return 0
    if a.write:
        p = os.path.join(BASE, a.write) if not os.path.isabs(a.write) else a.write
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=1) + "\n")
        os.replace(tmp, p)
        print(f"最終更新日を書きました: {len(data['dates'])} 機種 → {a.write}")
        return 0
    print(json.dumps(data["dates"], ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

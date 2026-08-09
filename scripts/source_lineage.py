# -*- coding: utf-8 -*-
"""「独立した2つの出典」の"2"を数える唯一の場所。

★なぜ要るか（2026-08-09・依頼125）★
  出典が2つ一致したら採用する、という運営方針の"2"は
  **本当に別の会社・別の系列であること**が前提になっている。
  ところが数える側が2か所に分かれていて、同じ発行者を2票と数えていた。

    名鑑（一覧ページ）側 : "dir:chonborista"   ← 名鑑IDから作った仮の名前
    控え（出典URL）側    : "lin-chonborista"   ← source-registry の系列ID

  この2つは文字列として違うので、**ちょんぼりすた1社しか無い機種が
  「2系列そろった」と数えられて**いた。P-WORLDと羽伏せのように
  registry では同じ系列に統合してある組でも同じことが起きる。

  実際に再現した（2026-08-09）:
    milliongod_kiseki を「名鑑=ちょんぼりすた だけ」「控え=ちょんぼりすた だけ」に
    しても、machine_sources.missing() は「2系列に届いた」と判定した。

★数え方の正本は source-registry.json の _policy★
  ・ここに無いホストは票に数えない（default deny）
  ・同じ ownership_group_id の出典は、ホストが違っても1票
  ・同じ content_lineage_id（転載系列）の出典も1票
  この2種類の関係でつながる発行者を**ひとかたまり＝1票**として扱う。

★迷ったら数えない（fail-closed）★
  発行者が引けないときに仮の名前を作ってその場をしのぐと、
  「本当は1社なのに2票」が静かに戻ってくる。**例外にして止める**。

使い方:
    python scripts/source_lineage.py --list          # 票のかたまりを見る
    python scripts/source_lineage.py --selftest
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import safe_json as _sj          # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY = os.path.join(BASE, "assets", "data", "source-registry.json")


class LineageError(Exception):
    """票の単位を決められない（★数えずに止める★）。"""


def load_registry(path: str | None = None) -> dict:
    return _sj.read_json(path or REGISTRY, expect=dict)


def _active(reg: dict) -> dict:
    return {pid: p for pid, p in (reg.get("publishers") or {}).items()
            if p.get("status") == "ACTIVE"}


def vote_groups(reg: dict | None = None) -> dict:
    """発行者ID → 票のかたまりのID。

    ownership（運営元が同じ）と lineage（転載系列が同じ）の
    **どちらかでつながる発行者は1つのかたまり**にする。
    """
    reg = reg if reg is not None else load_registry()
    pubs = _active(reg)
    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for pid, p in pubs.items():
        find(pid)
        own = p.get("ownership_group_id")
        lin = p.get("content_lineage_id")
        if own:
            union(pid, "own:" + str(own))
        if lin:
            union(pid, "lin:" + str(lin))

    # かたまりの名前は「所属する発行者IDのいちばん若いもの」に固定する
    members: dict = {}
    for pid in pubs:
        members.setdefault(find(pid), []).append(pid)
    return {pid: "vote:" + min(ms) for root, ms in members.items() for pid in ms}


def vote_key(publisher_id: str, reg: dict | None = None) -> str:
    """発行者IDから票の単位を返す。★登録が無ければ例外★"""
    g = vote_groups(reg)
    key = g.get(str(publisher_id or ""))
    if not key:
        raise LineageError(
            "票に数えられない出典です（source-registry に ACTIVE で無い）: "
            + str(publisher_id))
    return key


def publisher_of_host(host: str, reg: dict | None = None) -> str:
    """ホスト名から発行者IDを引く。★登録が無ければ例外★"""
    reg = reg if reg is not None else load_registry()
    h = str(host or "").lower()
    for pid, p in _active(reg).items():
        for c in p.get("canonical_hosts") or []:
            if str(c).lower() == h:
                return pid
    raise LineageError("登録されていないサイトです: " + str(host))


def vote_key_of_url(url: str, reg: dict | None = None) -> str:
    reg = reg if reg is not None else load_registry()
    host = urllib.parse.urlsplit(str(url or "")).hostname or ""
    return vote_key(publisher_of_host(host, reg), reg)


def count_votes(publisher_ids, reg: dict | None = None) -> int:
    """★独立した出典が何票あるか★（同じかたまりは何本あっても1票）"""
    reg = reg if reg is not None else load_registry()
    return len({vote_key(p, reg) for p in publisher_ids})


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    reg = load_registry()
    g = vote_groups(reg)

    t("　登録済みの発行者はすべて票の単位を持つ",
      all(g.get(p) for p in _active(reg)))
    t("★★同じ転載系列は1票★★（P-WORLDと羽伏せ）",
      g.get("p-world") == g.get("hazuse") and g.get("p-world"))
    t("★★同じ転載系列は1票★★（ちょんぼりすたとやんちゃプレス）",
      g.get("chonborista") == g.get("yancha-press"))
    t("　別の会社どうしは別の票",
      len({g.get("chonborista"), g.get("nana-press"), g.get("dmm-ptown"),
           g.get("1geki")}) == 4)
    t("★★1社しか無ければ何本URLがあっても1票★★",
      count_votes(["p-world", "hazuse", "p-world"], reg) == 1)
    t("　別の2社なら2票", count_votes(["chonborista", "nana-press"], reg) == 2)

    ok = False
    try:
        vote_key("slobase", reg)
    except LineageError:
        ok = True
    t("★★登録されていない出典は数えずに止める★★（黙って1票にしない）", ok)

    ok = False
    try:
        publisher_of_host("example.com", reg)
    except LineageError:
        ok = True
    t("　知らないホストも止める", ok)

    t("　ホストから票の単位を引ける",
      vote_key_of_url("https://www.p-world.co.jp/x") == g.get("p-world"))
    t("　同じ会社の別ホストは同じ票",
      vote_key_of_url("https://hazuse.com/y")
      == vote_key_of_url("https://p-world.co.jp/z"))

    bad = sum(1 for _, o in results if not o)
    print()
    print("%d/%d 合格" % (len(results) - bad, len(results)))
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="出典の独立性（票の単位）")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    g = vote_groups()
    by: dict = {}
    for pid, key in sorted(g.items()):
        by.setdefault(key, []).append(pid)
    print("★票のかたまり: %d★" % len(by))
    for key, ms in sorted(by.items()):
        print("  %-24s %s" % (key, " / ".join(ms)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

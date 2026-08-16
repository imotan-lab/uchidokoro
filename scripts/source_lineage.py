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


def independent(keys, reg: dict | None = None) -> int:
    """★独立していると数えてよい票の数★（2026-08-14・依頼192のP1）

    ★ここを通さずに `len(集合)` で数えない★
      票のかたまり（vote:xxx）を作るところまでは各抽出器がやるが、
      **「何票あるか」を決めるのはこの関数だけ**にする。
      共同制作の組（joint_production）をまとめる処理は、
      数える場所が散らばっていると必ず繋ぎ忘れる
      （実際、依頼190で入れたときに材料を採用する本体を通していなかった）。
    """
    return len(merge_joint({k for k in (keys or ()) if k}, reg))


def joint_pairs(reg: dict | None = None) -> list:
    """★共同で作ることがある発行者の組★（source-registry の joint_production）

    ★なぜ要るか（2026-08-14・依頼190のP1）★
      一撃とDMMぱちタウンは別会社だが、**共同取材の企画が実在する**
      （「双龍玉」）。共同制作の記事は2社の名前が並んでいても取材は1つで、
      独立2票と数えると「2つの出典が一致した」の土台が崩れる。
      名鑑側の JSON に注意書きは書いてあったが、**読むコードが無かった**＝
      無人の処理では何の関門にもなっていなかった。

    ★迷ったら数えない★＝共同かどうかは記事を読まないと分からないので、
      この組が顔ぶれに揃ったときは**まとめて1票**として扱う。
      別の発行者をもう1つ足せば2票に届く（＝作業は止まらない）。
    """
    reg = reg if reg is not None else load_registry()
    got = reg.get("joint_production") or []
    if not isinstance(got, list):
        raise LineageError("joint_production は並びで書きます")
    pubs = _active(reg)
    out = []
    for row in got:
        if not isinstance(row, dict):
            raise LineageError("joint_production の要素は組（辞書）で書きます")
        ps = row.get("publishers")
        if not isinstance(ps, list) or len(ps) < 2:
            raise LineageError(f"joint_production の publishers が不正です: {ps!r}")
        for p in ps:
            if p not in pubs:
                raise LineageError(
                    f"joint_production に知らない（またはACTIVEでない）"
                    f"発行者があります: {p}")
        if not str(row.get("why") or "").strip():
            raise LineageError("joint_production には理由（why）が要ります")
        out.append(list(ps))
    return out


def merge_joint(keys, reg: dict | None = None) -> set:
    """★共同制作がありうる組が揃っていたら、1票にまとめる★

    票のかたまり（vote:xxx）の集合を受け取り、まとめ直した集合を返す。
    ★1社でも欠けていれば何もしない★＝関係ない機種の邪魔をしない。
    """
    reg = reg if reg is not None else load_registry()
    g = vote_groups(reg)
    out = set(keys)
    for pair in joint_pairs(reg):
        ks = sorted({g[p] for p in pair if p in g})
        if len(ks) > 1 and all(k in out for k in ks):
            out -= set(ks)
            out.add(ks[0])          # ★まとめて1票★
    return out


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
    """★独立した出典が何票あるか★（同じかたまりは何本あっても1票）

    ★共同制作がありうる組もまとめる★（2026-08-14・依頼190のP1）
    """
    reg = reg if reg is not None else load_registry()
    return len(merge_joint({vote_key(p, reg) for p in publisher_ids}, reg))


# ---------------------------------------------------------------- selftest

def _bad_joint(reg: dict) -> bool:
    """★壊れた joint_production は止まるか★（試験用）"""
    import copy
    for bad in ([{"publishers": ["dmm-ptown"], "why": "x"}],
                [{"publishers": ["dmm-ptown", "zzz-nai"], "why": "x"}],
                [{"publishers": ["dmm-ptown", "nana-press"], "why": ""}],
                {"publishers": []}):
        r = copy.deepcopy(reg)
        r["joint_production"] = bad
        try:
            joint_pairs(r)
            return False
        except LineageError:
            pass
    return True


def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅" if cond else "❌") + " " + name)

    reg = load_registry()
    g = vote_groups(reg)

    t("　登録済みの発行者はすべて票の単位を持つ",
      all(g.get(p) for p in _active(reg)))
    # ★2026-08-16・台帳#376★ P-WORLD・一撃・羽伏せは規約により票から外した。
    #   （P-WORLDと羽伏せが同じ転載系列だったことは registry に記録が残る）
    t("★★規約で外した出典は票に数えない★★（P-WORLD・一撃・羽伏せ）",
      all(p not in g for p in ("p-world", "hazuse", "1geki")))
    t("★★同じ転載系列は1票★★（ちょんぼりすたとやんちゃプレス）",
      g.get("chonborista") == g.get("yancha-press"))
    t("　別の会社どうしは別の票",
      len({g.get("chonborista"), g.get("nana-press"),
           g.get("dmm-ptown")}) == 3)
    t("★★同じ転載系列は1票★★（ちょんぼりすたとやんちゃプレス）",
      g.get("chonborista") == g.get("yancha-press") and g.get("chonborista"))
    t("★★1社しか無ければ何本URLがあっても1票★★",
      count_votes(["chonborista", "yancha-press", "chonborista"], reg) == 1)
    t("　別の2社なら2票", count_votes(["chonborista", "nana-press"], reg) == 2)

    # ★★共同制作の組（2026-08-14・依頼190のP1⑤）★★
    # ★共同制作の設定はいま空★（一撃を規約で外したため・履歴は registry に残る）
    #   仕組みは残す＝また共同制作の組が出てきたら登録するだけで効く。
    t("★★共同で作ることがある組は1票にまとめる★★（仕組みは残っている）"
      "／共同取材の記事を独立2票と数えると土台が崩れる",
      merge_joint({g["chonborista"], g["dmm-ptown"]},
                  {**reg, "joint_production": [
                      {"publishers": ["chonborista", "dmm-ptown"],
                       "why": "試験"}]}) == {min(g["chonborista"],
                                                g["dmm-ptown"])})
    t("　（対照）登録が無ければまとめない",
      len(merge_joint({g["chonborista"], g["dmm-ptown"]}, reg)) == 2)
    t("★★票を数えるのはこの関数だけ★★（各抽出器はここを通す）",
      independent({g["chonborista"], g["dmm-ptown"]}, reg) == 2
      and independent({g["chonborista"], "", None}, reg) == 1
      and independent(set(), reg) == 0)
    t("　いま共同制作の登録は空（一撃を規約で外したため）",
      joint_pairs(reg) == [])
    t("　登録の形が壊れていたら止める（黙って素通りしない）",
      _bad_joint(reg))

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
      vote_key_of_url("https://p-town.dmm.com/x") == g.get("dmm-ptown"))
    t("　同じ会社の別ホストは同じ票",
      vote_key_of_url("https://www.slopachi-quest.com/y")
      == vote_key_of_url("https://slopachi-quest.com/z"))
    # ★規約で外した先は、票にもホストからも引けない★（2026-08-16・台帳#376）
    _ng = 0
    for _u in ("https://www.p-world.co.jp/x", "https://1geki.jp/y",
               "https://hazuse.com/z"):
        try:
            vote_key_of_url(_u)
        except LineageError:
            _ng += 1
    t("★★規約で外した先はホストからも引けない★★"
      "（票にも材料にも混ざらない）", _ng == 3)

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

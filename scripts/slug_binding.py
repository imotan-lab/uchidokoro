# -*- coding: utf-8 -*-
"""slug_binding.py — ★slugと機種ページURLの対応を確かめる唯一の場所★

★なぜ要るのか（2026-08-16・台帳#376／Codex依頼212の指摘5）★
  当サイトは「slugは機種ページのURLから作る」という決まりで、
  公開の境界でも**identity URLから作り直したslugと一致すること**を
  求めています。これが「別機種の記事を、別機種のURLで公開する」事故を
  止めています。

  ところがP-WORLDの規約でDMMへ移すと、**公開済み7機種のURLだけが変わり**、
  `pw_10501` と `dmm_5042` が一致しなくなって全部止まります。
  かといってslugを付け替えると**公開済みのURLが変わって読者のリンクが切れます**。

★どう解いたか★
  「machines.json に書いてあるslugなら照合を省く」は**採りません**。
  machines.json 自体を書き換えれば、slugとURLを同時に変えて通せるからです。

  代わりに**移行した7件だけの対応表**を置き、境界の判定を二択にします。

      slug == URLから作ったslug          → 新しいDMM機種として合格
      対応表[slug] == URLから作ったslug   → 公開済み7件の移行として合格
      どちらでもない                      → 止める

★対応表はデータではなくコードに置く★
  JSONに置くと「新しい機種を1行足して逃がす」ことができてしまいます。
  ここは**7件で終わりの、二度と増えない表**なので、コードに固定して
  レビューを必ず通る形にしました（増やすにはコミットが要る）。

使い方:
    python scripts/slug_binding.py --check          # 表そのものの点検
    python scripts/slug_binding.py --selftest
"""
from __future__ import annotations

import argparse
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

# ★移行した公開済み7機種だけの対応表★（2026-08-16・実データで確認）
#   左＝公開済みのslug（読者のリンク・検索の登録がこの形で残っている）
#   右＝DMMの機種ページURLから作られるslug
#   ★増やさないこと★（新しい機種はここを通さず、素の一致で合格する）
LEGACY_BINDINGS = {
    "pw_10501": "dmm_5042",   # パチスロ見える子ちゃん
    "pw_10503": "dmm_5033",   # スマスロ リコリス・リコイル
    "pw_10510": "dmm_5049",   # スマスロ タコスロ
    "pw_10513": "dmm_5054",   # マイジャグラーVI
    "pw_10521": "dmm_5061",   # スマスロ 獣王
    "pw_10523": "dmm_5059",   # モグモグ風林火山 大海戦の巻
    "pw_10543": "dmm_5065",   # Lパチスロ 彼女、お借りします
    # ★メーカー公式で同定していた3件★（2026-08-16・運営者判断）
    #   出典は大手サイトへ寄せると決めたのに、ここだけメーカー公式が
    #   残っていた。残すとメーカー巡回の仕組みごと消せないので移した。
    #   slugはメーカー公式のURL末尾から作った形なので、機種IDから
    #   作り直すと一致しない＝この表が唯一の結び付け。
    "garei_zero_re": "dmm_5028",   # Lパチスロ 喰霊-零-Re
    "prskkm": "dmm_5057",          # スマスロパリピ孔明
    "ssb1": "dmm_5064",            # L青春ブタ野郎はバニーガール先輩の夢を見ない
}

# 移行前のslugの形（★P-WORLD由来と、メーカー公式のURL末尾由来の2種類★）
_LEGACY_KEY = re.compile(r"^(pw_\d+|[a-z][a-z0-9_]{1,40})$")
_LEGACY_VAL = re.compile(r"^dmm_\d+$")


class BindingError(Exception):
    """slugとURLの対応が確かめられない（★迷ったら止める★）。"""


def _derive(identity_url: str) -> str:
    """機種ページのURLからslugを作る（★作る場所は1つ★）。"""
    import build_new_article as _ba
    try:
        return _ba.slug_from_url(identity_url)
    except Exception as e:                       # noqa: BLE001
        raise BindingError(f"URLからslugを作れません（{identity_url}）: "
                           f"{str(e)[:80]}")


def audit_table() -> list:
    """★対応表そのものの点検★（壊れていたら使わせない）。"""
    bad = []
    if len(LEGACY_BINDINGS) != 10:
        bad.append(f"件数が10件ではありません（{len(LEGACY_BINDINGS)}件）"
                   "／★この表は移行した10件で終わりです★")
    vals = list(LEGACY_BINDINGS.values())
    if len(set(vals)) != len(vals):
        dup = sorted({v for v in vals if vals.count(v) > 1})
        bad.append(f"同じDMM機種に2つのslugが向いています: {dup}")
    for k, v in LEGACY_BINDINGS.items():
        if not _LEGACY_KEY.match(k):
            bad.append(f"移行前のslugの形ではありません: {k}")
        if not _LEGACY_VAL.match(v):
            bad.append(f"移行後のslugの形ではありません: {v}")
        if k == v:
            bad.append(f"同じ値です（対応表に載せる意味がありません）: {k}")
    return bad


def published_slugs() -> set:
    """いま公開されている機種のslug。"""
    import safe_json as _sj
    p = os.path.join(BASE, "assets", "data", "machines.json")
    d = _sj.read_json(p, expect=(dict, list))
    ms = d["machines"] if isinstance(d, dict) else d
    return {m.get("slug") for m in ms if m.get("slug")}


def audit_against_site() -> list:
    """★表の左側が本当に公開済みの機種か★（架空のslugを逃がさない）。"""
    bad = audit_table()
    try:
        known = published_slugs()
    except Exception as e:                       # noqa: BLE001
        return bad + [f"機種一覧を読めません: {str(e)[:80]}"]
    for k in LEGACY_BINDINGS:
        if k not in known:
            bad.append(f"対応表にあるのに公開済みの機種ではありません: {k}"
                       "／★移行が済んだら表から消すのではなく、"
                       "そのslugが消えた理由を確かめてください★")
    return bad


def check(slug: str, identity_url: str) -> tuple:
    """★このslugで、このURLの機種を公開してよいか★

    返すもの: (合格?, 理由)
    """
    bad = audit_table()
    if bad:
        # ★表が壊れているなら、通す判断そのものをしない★
        return False, "対応表が壊れています: " + "／".join(bad[:2])
    slug = str(slug or "").strip()
    if not slug:
        return False, "slugが空です"
    try:
        derived = _derive(identity_url)
    except BindingError as e:
        return False, str(e)
    if slug == derived:
        return True, "URLから作ったslugと一致（新しい機種）"
    got = LEGACY_BINDINGS.get(slug)
    if got is None:
        return False, (f"slugとURLが対応していません"
                       f"（slug={slug} / URLから作ると{derived}）"
                       "／★対応表にも載っていません★")
    if got != derived:
        return False, (f"対応表と食い違います"
                       f"（slug={slug} は {got} のはずが、URLからは{derived}）")
    return True, f"移行した公開済み機種（{slug} → {derived}）"


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅ " if cond else "❌ ") + name)

    DMM = "https://p-town.dmm.com/machines/%s"
    PW = "https://www.p-world.co.jp/machine/database/%s"

    t("★★対応表そのものが正しい★★（件数・形・重複）", not audit_table())
    t("　メーカー公式で同定していた3件も入っている（大手サイトへ寄せた）",
      LEGACY_BINDINGS.get("garei_zero_re") == "dmm_5028"
      and LEGACY_BINDINGS.get("prskkm") == "dmm_5057"
      and LEGACY_BINDINGS.get("ssb1") == "dmm_5064")
    t("★★表の左側は本当に公開中の機種★★（架空のslugが混ざっていない）",
      not audit_against_site())

    ok, why = check("dmm_5086", DMM % "5086")
    t("★★新しい機種はURLから作ったslugと一致すれば合格★★", ok)
    ok, why = check("pw_10501", DMM % "5042")
    t("★★移行した公開済み機種は対応表で合格★★（読者のリンクを切らない）", ok)

    ok, why = check("pw_10501", DMM % "5049")
    t("★★対応表と違うDMM機種のURLなら止める★★"
      "（見える子ちゃんのslugにタコスロのURL）", not ok)
    ok, why = check("pw_99999", DMM % "5086")
    t("★★表に無い pw_ のslugは通さない★★（新しい機種を逃がせない）", not ok)
    ok, why = check("dmm_5042", DMM % "5086")
    t("　DMM同士でも食い違えば止める", not ok)
    ok, why = check("", DMM % "5086")
    t("　slugが空なら止める", not ok)
    ok, why = check("dmm_5086", "https://example.com/foo/%%%")
    t("　URLからslugを作れなければ止める", not ok)

    # ★対照実験★＝守りを外した姿では実際に通ってしまうことを見せる
    saved = dict(LEGACY_BINDINGS)
    try:
        LEGACY_BINDINGS["pw_99999"] = "dmm_5086"
        ok2, _ = check("pw_99999", DMM % "5086")
        t("★★（対照）表に1行足しても、件数の点検で通らない★★"
          "／新しい機種を表へ足して逃がすことができない", not ok2)
    finally:
        LEGACY_BINDINGS.clear()
        LEGACY_BINDINGS.update(saved)
    t("　（後始末）対応表は元どおり", not audit_table())

    # ★移行前のURLはそもそも通信できない（規約）★
    import blocked_hosts as _bh
    t("★★移行前のP-WORLDのURLは通信そのものが止まる★★（台帳#376）",
      _bh.is_blocked(PW % "10501"))

    ng = sum(1 for _, o in results if not o)
    print()
    print("%d/%d 合格" % (len(results) - ng, len(results)))
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="slugと機種ページURLの対応")
    ap.add_argument("--check", action="store_true", help="対応表を点検する")
    ap.add_argument("--slug")
    ap.add_argument("--url")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if a.selftest:
        return selftest()
    if a.slug and a.url:
        ok, why = check(a.slug, a.url)
        print(("合格: " if ok else "★止まります★ ") + why)
        return 0 if ok else 1
    bad = audit_against_site()
    if bad:
        print("★対応表に問題があります★")
        for b in bad:
            print("  -", b)
        return 1
    print("対応表 %d件・問題なし" % len(LEGACY_BINDINGS))
    for k, v in sorted(LEGACY_BINDINGS.items()):
        print("  %-10s → %s" % (k, v))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

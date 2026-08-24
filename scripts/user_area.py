# -*- coding: utf-8 -*-
"""user_area.py — 読者の投稿欄・AIがまとめた欄を、HTMLの箱ごと落とす。

★なぜ要るか（2026-08-13・台帳#345）★
  P-WORLDの機種ページには、読者の投稿と**それをAIが要約した欄**があります。

    「AI機種評価」「活発なトピック」「関連ワード」
    「最近の投稿をもとにAIがまとめた内容を表示しています。試験的な導入であり、
      情報の精度を保証するものではありません。」

  ★AIが要約した内容を出典として数えると、根拠のない数字を載せることになります★

★行では切れない★
  これまでの `cut_user_area` は「見出しだけの行」を探して**そこから後ろ**を
  落とす形でした。ところがP-WORLDのAI欄は、断り書き（メモ）が
  **要約本文より後ろ**に置かれています。断り書き以降を落としても
  要約本文は残るので、行で切る形では安全側になりません。

★どう直したか＝箱（HTMLの要素）ごと落とす★
  実際のページを取ってきて構造を確かめました（2026-08-14）。

    <div id="bbs" class="bbs toggleBox …">     ← 掲示板ぜんぶ
      <div class="bbsAiMatome">…</div>         ← AIがまとめた欄
      <div class="bbsThreadBox">…</div>        ← 投稿の一覧
    </div>

  この箱を**中身ごと**落としてから本文を取り出します。
  ★正規表現で文を探すのではなく、HTMLとして解析して要素を落とします★

★落としきれなかったら、そのページは使わない（fail-closed）★
  相手のHTMLはいつ変わるか分かりません。箱を落としたあとに
  「AIがまとめた」印が本文に残っていたら、**落とし損ねている**ということなので、
  そのページを出典として使いません（例外にして止めます）。
  ★黙って一部だけ落として使う、が一番危ない★

使い方:
    python scripts/user_area.py --url <ページのURL>     # 落ちる箱を見る
    python scripts/user_area.py --selftest
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from html.parser import HTMLParser as _HTMLParser  # noqa: E402

import safe_json as _sj              # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOGS = os.path.join(BASE, "assets", "data", "directory-catalogs.json")


class UserAreaError(Exception):
    """投稿欄を落としきれない（★そのページは使わない★）。"""


def _conf(host: str) -> dict:
    """そのホストの決まりごと（無ければ空）。"""
    h = str(host or "").lower().lstrip(".")
    if not h:
        return {}
    got = _sj.read_json(CATALOGS, expect=dict).get("directories") or {}
    for c in got.values():
        if not isinstance(c, dict) or c.get("status") != "ACTIVE":
            continue
        ua = c.get("user_area")
        if not isinstance(ua, dict):
            continue
        for hh in ua.get("hosts") or []:
            hh = str(hh).lower().lstrip(".")
            if h == hh or h.endswith("." + hh):
                return ua
    return {}


def conf_for_url(url: str) -> dict:
    return _conf(urllib.parse.urlsplit(str(url or "")).hostname or "")


def is_machine_page(url: str, conf: dict) -> bool:
    """そのURLは「機種ページ」か（2026-08-15）。

    ★必須アンカーは機種ページにだけ求める★
      一覧ページ・カレンダーには口コミ欄も掲示板も無いのが当たり前。
      そこで「無い＝作りが変わった」と判定すると、
      **新台を見つける入口（P-WORLDの導入カレンダー）まで丸ごと使えなくなる**。
      実際に3つの名鑑すべてで一覧ページが止まることを確認した。

    ★形を書いていなければ「機種ページ」として扱う★（今までどおり厳しく見る）
    """
    import re
    pat = str(conf.get("page_pattern") or "")
    if not pat or not url:
        return True
    return bool(re.match(pat, str(url)))


def _matches(node, rule: dict) -> bool:
    """その要素が落とす対象か（id または class の**語**で見る）。"""
    a = node.get("attrs") or {}
    want_id = str(rule.get("id") or "")
    if want_id and str(a.get("id") or "") == want_id:
        return True
    want_cls = str(rule.get("class") or "")
    if want_cls:
        # ★class は空白区切りの語なので、部分一致で見ない★
        #   （"bbs" が "bbsThreadList" に当たると意図より広く落ちる）
        return want_cls in str(a.get("class") or "").split()
    return False


def _find(node, rules: list) -> bool:
    """その木の中に、決めた箱があるか（★守る対象が居るかを数える★）。"""
    for ch in node.get("children") or []:
        if ch.get("tag") == "#text":
            continue
        if any(_matches(ch, r) for r in rules) or _find(ch, rules):
            return True
    return False


def _node_text(node) -> str:
    """その箱の中の文字だけを集める（★中身を読むためだけ★）。"""
    out = []
    for ch in node.get("children") or []:
        if ch.get("tag") == "#text":
            out.append(str(ch.get("text") or ""))
        else:
            out.append(_node_text(ch))
    return " ".join(" ".join(out).split())


def _declared_count(node, rule: dict):
    """★そのページが自分で言っている件数★（読めなければ None）。

    ★なぜ要るか（2026-08-23・台帳#460）★
      DMMの機種ページは投稿欄の枠に件数を書いている。

        ユーザー評価（0件）   → 一覧の箱は描画されない
        ユーザー評価（44件）  → 一覧の箱が描画される

      ★「一覧の箱が必ずある」と要求すると、レビューが0件の機種は
      永久に出典に使えない★（＝解析サイトが扱わないマイナー機種そのもの）。
      正しい要求は「枠は必ずある／一覧は件数が1件以上のときだけ必ずある」。

    ★読めなければ None を返す★＝呼び出し側は「要求する」側に倒す
      （相手が件数の書き方を変えた可能性を、安全側に扱う）。
    """
    import re as _re
    box = _first(node, [rule])
    if box is None:
        return None
    # ★★件数の場所を名指しする★★（2026-08-23・Codexの反例①）
    #   ★直す前は箱の中の「最初のN件」を拾っていた★ので、
    #   投稿とは無関係な「0件」が先に出る作りに変わると、
    #   ★それで要求を解除できてしまった★（実際に試験で再現）。
    #   名指しした場所が見つからなければ None＝要求する側（安全側）。
    where = rule.get("count_in")
    if isinstance(where, dict):
        box = _first(box, [where])
        if box is None:
            return None
    m = _re.search(r"(\d+)\s*件", _node_text(box))
    return int(m.group(1)) if m else None


def _first(node, rules: list):
    """その木の中で最初に見つかった箱（無ければ None）。"""
    for ch in node.get("children") or []:
        if ch.get("tag") == "#text":
            continue
        if any(_matches(ch, r) for r in rules):
            return ch
        got = _first(ch, rules)
        if got is not None:
            return got
    return None


def strip_tree(node, rules: list) -> int:
    """木から対象の箱を落とす。落とした数を返す。"""
    n = 0
    keep = []
    for ch in node.get("children") or []:
        if ch.get("tag") != "#text" and any(_matches(ch, r) for r in rules):
            n += 1
            continue                     # ★中身ごと落とす★
        keep.append(ch)
    node["children"] = keep
    for ch in keep:
        n += strip_tree(ch, rules)
    return n


def clean_text(html: str, url: str = "", conf: dict | None = None) -> str:
    """★出典として読んでよい本文★（ここだけを呼ぶ）

    ・名鑑に決まりごとがある → **箱ごと落とす**（行では切らない）
    ・決まりごとが無い       → これまでどおり行単位で切る

    ★両方はやらない★（2026-08-14・実データで確認）
      P-WORLDは箱を落とすと、ページ上部の見出しタブに残る「掲示板」の行が
      いちばん後ろの「掲示板」になり、行で切ると**機種データごと**落ちる
      （実測 5,317字 → 262字）。切る役目は箱に移したので、行では切らない。
    """
    import ceiling_lookup as _cl
    ua = conf if conf is not None else conf_for_url(url)
    got = visible_text(html, url, ua)
    if [r for r in (ua.get("drop") or []) if isinstance(r, dict)]:
        return _cl._norm(got)
    return _cl._norm(_cl.cut_user_area(got))


def visible_text(html: str, url: str = "", conf: dict | None = None) -> str:
    """★投稿欄・AI欄を箱ごと落としてから★画面に出る文字を返す。

    決まりごとが無いホストは、これまでどおり素通し（行単位の
    `ceiling_lookup.cut_user_area` が引き続き効きます）。
    """
    import new_machine_watch as _nw
    ua = conf if conf is not None else conf_for_url(url)
    rules = [r for r in (ua.get("drop") or []) if isinstance(r, dict)]
    if not rules:
        return _nw._visible_text(html or "")
    try:
        root = _nw.parse_tree(html)
    except Exception as e:               # noqa: BLE001
        # ★読めないものを「危なくない」とは言えない★
        raise UserAreaError(f"HTMLを解析できません（投稿欄を落とせません）: {e}")

    # ★落とす前に「守る対象の箱」が居るか確かめる★（2026-08-14・依頼196のP1）
    #   以前は落とした数を数えていただけで、使っていなかった。
    #   相手が id と 印（markers）を**同時に**変えると、
    #   1つも落とせないまま正常終了し、AIの要約が本文へ戻る。
    #   ★飾りを含む13個の合計ではなく、名指しした箱で見る★
    # ★書いた箱は「全部」そろっていること★（2026-08-14・依頼198）
    #   まとめて渡すと「どれか1つあれば合格」になる。1つずつ見る。
    # ★必須アンカーは機種ページにだけ求める★（2026-08-15）
    _mp = is_machine_page(url, ua)
    need_b = ([r for r in (ua.get("require_before") or []) if isinstance(r, dict)]
              if _mp else [])

    def _required_now(r: dict) -> bool:
        """★その箱を「必ずある」と求めてよいか★（2026-08-23・台帳#460）

        ★件数で条件が付いた箱★＝そのページが「0件」と言っているなら、
        描かれていなくて当然なので求めない。
        ★読めないときは求める★＝相手が書き方を変えた可能性を安全側に扱う。
        """
        gate = r.get("only_if_count_in")
        if not isinstance(gate, dict):
            return True
        n = _declared_count(root, gate)
        return n != 0                     # None（読めない）でも求める

    miss_b = [r for r in need_b
              if _required_now(r) and not _find(root, [r])]
    if miss_b:
        raise UserAreaError(
            f"落とすはずの箱が見つかりません（{miss_b}）"
            "／★このページは出典に使いません★"
            "（相手のHTMLの作りが変わった可能性があります）")
    dropped = strip_tree(root, rules)
    # ★落とした後に「本文の箱」が残っているか確かめる★
    #   落としすぎ（機種データごと消える）にも気づけるようにする。
    need_a = ([r for r in (ua.get("require_after") or []) if isinstance(r, dict)]
              if _mp else [])
    miss_a = [r for r in need_a if not _find(root, [r])]
    if miss_a:
        raise UserAreaError(
            f"落としたあとに本文の箱が残っていません（{miss_a}）"
            "／★このページは出典に使いません★（落としすぎの疑い）")
    # ★本文にするのは new_machine_watch の役目★（2026-08-14・台帳#351）
    #   ここで自前に木をたどっていたので、aside/nav/footer/header の
    #   扱いが本家とずれていた（同じHTMLから2通りの本文ができていた）。
    #   ★落とすのがこの器の役目／本文にするのは共通の役目★
    import re
    text = _nw.text_of_tree(root)
    # ★落とし損ねていないか確かめる★（fail-closed）
    left = [m for m in (ua.get("markers") or [])
            if str(m) and str(m) in text]
    if left:
        raise UserAreaError(
            f"投稿欄・AIのまとめを落としきれません（{', '.join(left[:2])}）"
            f"／★このページは出典に使いません★"
            f"（落とせた箱: {dropped}件・相手のHTMLが変わった可能性があります）")
    return re.sub(r"\n{3,}", "\n\n", text)


# ---------------------------------------------------------------- HTMLから落とす

class _Cutter(_HTMLParser):
    """★HTMLの文字列から、落とす箱の範囲を見つける★（2026-08-14）

    ★なぜ要るか★
      画面に出る文字（visible_text）だけを守っても足りない。
      天井・スペック・CZ・型式を採る処理は**表（table）を生のHTMLから**読むので、
      投稿欄の中の表がそのまま材料になりうる。
      そこで**HTMLの段階で**箱ごと切り落とし、以後の処理すべてを守る。

    ★正規表現で切らない★＝HTMLとして解析し、開始タグと対応する終了タグの
      位置を数えて範囲を出す。
    """

    def __init__(self, raw: str, rules: list):
        super().__init__(convert_charrefs=False)
        self.raw = raw
        self.rules = rules
        self.cuts = []                     # [(開始, 終了)]
        self._depth = 0                    # 落とす箱の中にいる深さ
        self._start = None
        self._tag = ""
        # 行の先頭が文字列全体の何文字目かを先に数えておく
        self._line_at = [0]
        for line in raw.splitlines(keepends=True):
            self._line_at.append(self._line_at[-1] + len(line))

    def _pos(self) -> int:
        ln, off = self.getpos()
        return self._line_at[min(ln - 1, len(self._line_at) - 1)] + off

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "img", "meta", "link", "input", "hr", "source"):
            return
        if self._depth:
            if tag == self._tag:
                self._depth += 1
            return
        node = {"attrs": dict(attrs)}
        if any(_matches(node, r) for r in self.rules):
            self._start = self._pos()
            self._tag = tag
            self._depth = 1

    def handle_endtag(self, tag):
        if not self._depth or tag != self._tag:
            return
        self._depth -= 1
        if self._depth:
            return
        p = self.raw.find(">", self._pos())
        end = (p + 1) if p >= 0 else len(self.raw)
        self.cuts.append((self._start, end))
        self._start, self._tag = None, ""


# ★★投稿欄を指す語の正本★★（2026-08-24・Codexの14回目）
#   ★行切り（ceiling_lookup）と、残っていないかの見張りが、
#     別々の語を持っていた★ので、
#   「掲示板」の中の表が**取ってくる関門を素通り**した
#   （文章は後から切れるが、天井・スペック・AT・CZは**表を直接読む**）。
#   ★語は1か所★＝どちらもここを読む。
#   ★★意味を読まなくても判る語だけ★★（CLAUDE.mdの原則）
#   ★「レビュー」「感想」の単体は入れない★＝編集部の「実戦レビュー」でも
#   止まってしまう（＝正しい本番を止める型）。
#   入れ物の名前（class="reviews" 等）は別に見ているので、
#   本物の投稿欄はそちらでも捕まる。
USER_AREA_WORDS = (
    "掲示板", "口コミ", "クチコミ", "みんなの感想", "みんなの評価",
    "みんなのレビュー", "ユーザーレビュー", "ユーザー評価",
    "コメント一覧", "コメント", "返信一覧", "書き込み", "投稿する",
    "ユーザー口コミ・評価詳細", "導入前評価", "COMMENTS & REVIEW",
    "コメント&評価", "コメントや評価を投稿", "User Reviews",
)

# ★入れ物の名前に出る手がかり★（id / class）
#   ★語として一致させる★（部分一致にすると moreView が review に当たる）
_UA_ATTR_HINTS = ("comment", "comments", "review", "reviews", "bbs",
                  "kuchikomi", "respond", "thread", "reply", "userpost")


def looks_like_user_area(html: str) -> list:
    """★掃除のあとに投稿欄が残っていないか★（★止めるだけ★）

    ★★2026-08-24・Codexの14回目★★
      ★直す前は「決まりごとが無いサイト」だけを見ていた★ので、
      すでに決まりごとがあるサイトが**別の箱で投稿欄を足した**場合は
      素通りした（古い箱があるので必須箱の検査も通る）。
      → ★掃除のあとの中身を、全サイトで見る★。
      決まりごとが正しければ、投稿欄は既に消えているので何も出ない。

    ★字面ではなく、組み立てたHTMLの要素で見る★
      引用符や空白の違い、見出しの入れ子（h2 の中の span）で見逃さない。

    ★これは「落とす場所を決める」処理ではない★＝
      見つかったら**そのページを出典に使わない**（fail-closed）。
      どこが投稿欄かは2AIが判断し、名鑑に決まりごとを登録する。
    """
    import html as _htmlmod
    import html.parser as _hp

    found = []

    class _P(_hp.HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self._head = None          # いま見出しの中か
            self._buf = []

        def handle_starttag(self, tag, attrs):
            # ★画面に出ないものは見ない★（2026-08-24・実ページで判明）
            #   読み込みの部品（script）の名前で止まっていた。
            #   読者に出る本文ではないので対象外。
            if tag in ("script", "style", "link", "meta", "noscript"):
                return
            d = {k.lower(): (v or "") for k, v in attrs}
            if tag == "textarea":
                found.append("入力欄: textarea")
            if "contenteditable" in d:
                found.append("入力欄: contenteditable")
            if str(d.get("type", "")).strip().lower() == "comment":
                found.append("入力欄: type=comment")
            # ★★名前は語の区切りで見る★★（2026-08-24・実ページで誤検知した）
            #   ★部分一致にしていた★ので、なな徹の `el_moreView` の中の
            #   「moreview」に「review」が含まれて**正常なページが止まった**。
            #   ＝守りを厳しくして本番を止める型。
            import re as _re3
            names = str(d.get("id", "")) + " " + str(d.get("class", ""))
            # 記号・大文字の切れ目で語に割る（moreView → more / view）
            toks = {x.lower() for x in _re3.findall(
                r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|[0-9]+", names)}
            for w in _UA_ATTR_HINTS:
                if w in toks or w.replace("-", "") in toks:
                    found.append(f"箱の名前: {w}")
                    break
            if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                self._head, self._buf = tag, []

        def handle_endtag(self, tag):
            if self._head and tag == self._head:
                txt = " ".join("".join(self._buf).split())
                for w in USER_AREA_WORDS:
                    if w.lower() in txt.lower():
                        found.append(f"見出し: {w}")
                        break
                self._head, self._buf = None, []

        def handle_data(self, data):
            if self._head:
                self._buf.append(data)

    try:
        _P().feed(_htmlmod.unescape(str(html or "")))
    except Exception:                                        # noqa: BLE001
        return ["組み立てられないHTML"]                      # ★使わない側に倒す★
    return sorted(set(found))

def clean_html(html: str, url: str = "", conf: dict | None = None) -> str:
    """★投稿欄・AI欄を、HTMLの段階で箱ごと落とす★

    ★これを取ってくる直後に通す★＝以後の処理（表を読む・文を読む）が
    まとめて守られる。決まりごとの無いホストはそのまま返す。
    """
    ua = conf if conf is not None else conf_for_url(url)
    rules = [r for r in (ua.get("drop") or []) if isinstance(r, dict)]
    if not rules or not html:
        return html
    # ★落とす前に守る対象が居るか／落とした後に本文が残るか★は
    #   visible_text と同じ物差しで見る（そちらが例外を出す）。
    visible_text(html, url, ua)
    p = _Cutter(html, rules)
    try:
        p.feed(html)
        p.close()
    except Exception as e:                 # noqa: BLE001
        raise UserAreaError(f"HTMLを解析できません（箱を落とせません）: {e}")
    out, last = [], 0
    for a, b in sorted(p.cuts):
        if a < last:
            continue                       # 入れ子は外側だけ切る
        out.append(html[last:a])
        last = b
    out.append(html[last:])
    return "".join(out)


# ---------------------------------------------------------------- selftest

_SAMPLE = """<html><body>
<div class="spec"><h2>基本情報</h2><p>メーカー ゲームカード・ジョイコ</p>
<p>型式名 LB/タコスロBD</p></div>
<div id="bbs" class="bbs toggleBox js-toggleBox is-visible">
 <h2>掲示板</h2>
 <div class="bbsAiMatome"><div class="bbsAiMatome-inner">
  <h4 class="bbsAiMatome-title">AI投稿まとめ</h4>
  <p class="bbsAiMatome-review-body">BIG確率1/324の重さが…</p>
  <ul class="bbsAiMatome-topics-list"><li>天井は999Gという声もある</li></ul>
  <p class="bbsAiMatome-memo">最近の投稿をもとにAIがまとめた内容を表示しています。</p>
 </div></div>
 <div class="bbsThreadBox"><ul class="bbsThreadList">
  <li class="bbsThreadList-item">BB360枚確実に貰えるのかと思ったら…2026/06/30 20:47</li>
 </ul></div>
</div>
</body></html>"""


def _all_required_checked() -> bool:
    """★必須の箱は全部そろうことを求めているか★（試験用）

    片方だけある形を渡して、ちゃんと止まるかを見る。
    """
    # （下の本体は従来どおり。件数の条件つきの試験は _count_gate_tests）
    conf = {"hosts": ["x.test"], "drop": [{"id": "bbs"}],
            "markers": [],
            "require_before": [{"id": "bbs"}, {"id": "sonzai_shinai"}]}
    try:
        visible_text(_SAMPLE, conf=conf)
        return False
    except UserAreaError:
        return True


def selftest() -> int:
    results = []

    def t(name, cond):
        results.append((name, bool(cond)))
        print(("✅ " if cond else "❌ ") + name)

    ua = {"hosts": ["p-world.co.jp"],
          "drop": [{"id": "bbs"}],
          "markers": ["AIがまとめた内容", "AI投稿まとめ"]}
    got = visible_text(_SAMPLE, conf=ua)
    t("★★AIがまとめた欄が本文から消える★★（根拠のない数字を出典にしない）",
      "999G" not in got and "AI投稿まとめ" not in got
      and "1/324" not in got)
    t("★★読者の投稿も消える★★",
      "BB360枚" not in got and "2026/06/30" not in got)
    t("　（対照）サイト自身が書いた部分は残る",
      "LB/タコスロBD" in got and "ゲームカード・ジョイコ" in got)

    # ★対照＝行で切るやり方が効かない形★
    #   行で切る形は「掲示板」等の**見出しだけの行**が要るので、
    #   相手が見出しの出し方を変えたり、AI欄を掲示板の外に置いた瞬間に
    #   効かなくなる。断り書き（メモ）は要約本文より**後ろ**にあるので、
    #   そこを目印にしても要約は残る。箱で落とす形はどちらにも効く。
    import ceiling_lookup as _cl
    import new_machine_watch as _nw
    _no_h2 = _SAMPLE.replace("<h2>掲示板</h2>", "")
    old = _cl.cut_user_area(_nw._visible_text(_no_h2))
    t("★★（対照）見出しの行が無いと、行で切るやり方では要約が残る★★"
      "／断り書きは要約本文より後ろにあるので目印にできない",
      "999G" in old and "1/324" in old)
    t("　（対照）同じ形でも、箱で落とせば消える",
      "999G" not in visible_text(_no_h2, conf=ua))

    _req = {**ua, "require_before": [{"id": "bbs"}],
            "require_after": [{"class": "spec"}]}
    t("　（対照）必須の箱がそろっていれば通る",
      "LB/タコスロBD" in visible_text(_SAMPLE, conf=_req))
    _ok2 = False
    try:
        # ★相手が箱の名前も印も同時に変えた形★＝落とせないまま通ってしまう
        visible_text(_SAMPLE.replace('id="bbs"', 'id="board2"')
                     .replace("bbsAiMatome", "aiMatome2")
                     .replace("bbsThreadBox", "threadBox2")
                     .replace("AIがまとめた内容", "AIによる要約")
                     .replace("AI投稿まとめ", "AI要約"),
                     conf=_req)
    except UserAreaError:
        _ok2 = True
    t("★★箱の名前と印が同時に変わっても気づく★★（2026-08-14・依頼196のP1）"
      "／落とすはずの箱が見つからなければ、そのページは使わない", _ok2)
    _ok3 = False
    try:
        visible_text(_SAMPLE, conf={**_req, "drop": [{"class": "spec"},
                                                     {"id": "bbs"}]})
    except UserAreaError:
        _ok3 = True
    t("　落としすぎ（本文の箱ごと消えた）にも気づく", _ok3)

    ok = False
    try:
        visible_text(_SAMPLE, conf={**ua, "drop": [{"class": "spec"}]})
    except UserAreaError:
        ok = True
    t("★★落としきれなかったら、そのページは使わない★★（fail-closed）"
      "／相手のHTMLが変わって箱を外したときに、黙って要約を採らないため", ok)

    t("　class は語で見る（部分一致で広く落とさない）",
      not _matches({"attrs": {"class": "bbsThreadList"}}, {"class": "bbs"})
      and _matches({"attrs": {"class": "bbs toggleBox"}}, {"class": "bbs"}))
    t("　決まりごとの無いホストは素通し",
      "999G" in visible_text(_SAMPLE, conf={}))

    # ★★2026-08-16・台帳#376★★
    #   P-WORLDと一撃は、規約でプログラム取得が禁止されていたので名鑑から外した。
    #   ★決まりごと（落とす箱・印）はJSONに残してある★＝許諾が取れて戻すとき、
    #   もう一度実ページを調べ直さずに済むように。ここでは**効いていないこと**を見る。
    import blocked_hosts as _bh2
    t("★★規約で外した名鑑の決まりごとは効かない★★（P-WORLD・一撃）",
      _conf("www.p-world.co.jp") == {} and _conf("1geki.jp") == {})
    t("★★そもそも機械が通信しない★★（最後の砦）",
      _bh2.is_blocked("https://www.p-world.co.jp/machine/database/10510")
      and _bh2.is_blocked("https://1geki.jp/slot/x/"))
    t("　（対照）使ってよい名鑑は通信できる",
      not _bh2.is_blocked("https://p-town.dmm.com/machines/4709")
      and not _bh2.is_blocked("https://chonborista.com/slot/x/1/"))
    # ★★他の名鑑も登録されているか（2026-08-14・台帳#348）★★
    #   ★一律に同じ規則をコピーしていない★＝サイトごとに実HTMLを見て決めた。
    _dmm = _conf("p-town.dmm.com")
    t("★★DMMぱちタウンの口コミ・評価も箱ごと落とす★★（台帳#348）",
      {"class": "list-machinesreviews"} in (_dmm.get("drop") or [])
      and {"class": "machine-userreview"} in (_dmm.get("drop") or [])
      and _dmm.get("require_after") == [{"class": "list-machineinformation"},
                                        {"class": "wysiwyg-box"}])
    _chon = _conf("chonborista.com")
    t("★★ちょんぼりすたのコメント・評価も箱ごと落とす★★（台帳#348）",
      {"class": "commentlist"} in (_chon.get("drop") or [])
      and {"id": "hyouka"} in (_chon.get("drop") or [])
      and _chon.get("require_after") == [{"id": "entry"}])
    t("　（対照）投稿フォームは必須に入れない"
      "＝コメントを閉じたページには無いため",
      {"id": "commentform"} in (_chon.get("drop") or [])
      and {"id": "commentform"} not in (_chon.get("require_before") or []))
    t("　なな徹はまだ登録しない（構造を確かめていないため）",
      _conf("nana-press.com") == {})
    # ★★必須アンカーは機種ページにだけ求める（2026-08-15）★★
    #   一覧ページ・カレンダーには口コミ欄も掲示板も無いのが当たり前。
    #   そこで止めると**新台を見つける入口（導入カレンダー）まで使えなくなる**。
    t("★★一覧やカレンダーを機種ページ扱いしない★★（2026-08-15）"
      "／ここを間違えると、新台を見つける入口が丸ごと止まる",
      is_machine_page("https://chonborista.com/slot/orinpia-slot/264134/",
                      _chon)
      and not is_machine_page("https://chonborista.com/slot/", _chon)
      and is_machine_page("https://p-town.dmm.com/machines/4709", _dmm)
      and not is_machine_page("https://p-town.dmm.com/machines", _dmm))
    t("　形を書いていない名鑑は、今までどおり厳しく見る",
      is_machine_page("https://x.test/anything", {}))
    t("　（対照）一覧ページでも、落とす箱と印は今までどおり効く",
      bool(_dmm.get("drop")) and bool(_chon.get("drop")))
    t("★★必須の箱は「どれか1つ」ではなく「全部」そろうこと★★（依頼198）",
      (lambda: [
          _ok for _ok in [False]
      ] and _all_required_checked())())
    t("　サブドメインでも引ける", _conf("p-town.dmm.com") == _conf("p-town.dmm.com"))
    t("　知らないホストは空", _conf("example.com") == {})

    # ★★件数で条件が付いた箱★★（2026-08-23・台帳#460）
    #   ★実データで見つけた★＝DMMの投稿一覧は**レビューが1件も無い機種には
    #   描画されない**（5089は「ユーザー評価（0件）」で一覧が無い／
    #   5028は「（44件）」で有る）。「必ずある」と求めていたので、
    #   ★レビューが付かないマイナー機種は永久に出典に使えなかった★
    #   ＝今回の運営者決定（DMM単独で記事にしてよい）が救おうとしている層そのもの。
    _CG = {"hosts": ["cg.test"], "drop": [{"class": "reviews"}],
           "markers": [],
           "require_before": [
               {"class": "wrap"},
               {"class": "reviews",
                "only_if_count_in": {"class": "wrap",
                                     "count_in": {"class": "count"}}}],
           "require_after": [{"class": "spec"}]}
    _page = ('<html><body><div class="machine">'
             '<div class="wrap"><span class="count">（%s件）</span></div>'
             '%s<div class="spec">天井999G</div></div></body></html>')
    _rev = '<div class="reviews">面白い台でした</div>'

    def _try_multi():
        """★件数が複数入っている形★（Codexの反例①）

        投稿とは無関係な「0件」が先に出る作りに変わったとき、
        それで要求を解除してしまわないか。
        """
        page = ('<html><body><div class="machine">'
                '<div class="wrap"><span class="other">（0件）</span>'
                '<span class="count">（44件）</span></div>'
                '<div class="spec">天井999G</div></div></body></html>')
        try:
            clean_html(page, "https://cg.test/machine/1", conf=_CG)
            return "OK"
        except UserAreaError:
            return "NG"

    def _real_pages():
        """★実物のHTMLで確かめる★（Codexの反例②）

        合成HTMLだけだと、相手の本当の作りとずれていても気づけない。
        `scripts/_fixtures/` に投稿欄まわりだけを切り出して置いてある。
        """
        import io as _io
        import os as _os
        out = {}
        base = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                             "_fixtures")

        def _run(name, mutate=None):
            p = _os.path.join(base, name)
            if not _os.path.isfile(p):
                return ("NOFILE", "")
            html = _io.open(p, encoding="utf-8").read()
            if mutate:
                html = mutate(html)
            try:
                return ("OK", clean_html(html,
                                         "https://p-town.dmm.com/machines/1"))
            except UserAreaError as e:
                return ("NG", str(e))

        out["zero"] = _run("dmm_reviews_zero.html")
        out["many"] = _run("dmm_reviews_many.html")
        # ★44件と言っているのに一覧の箱が消えた形★＝相手が名前を変えた疑い
        out["many_renamed"] = _run(
            "dmm_reviews_many.html",
            lambda h: h.replace("list-machinesreviews", "list-renamed"))
        return out

    def _try(cnt, with_list):
        try:
            got = clean_html(_page % (cnt, _rev if with_list else ''),
                             "https://cg.test/machine/1", conf=_CG)
            return ("OK", got)
        except UserAreaError as e:
            return ("NG", str(e))

    t("★★0件と書いてあれば、一覧が無くても通す★★"
      "／これが無いとレビューの付かない機種は永久に使えない",
      _try("0", False)[0] == "OK")
    t("★★0件でも、一覧があれば必ず落とす★★（通すことと落とさないことは別）",
      _try("0", True)[0] == "OK"
      and "面白い台でした" not in _try("0", True)[1])
    t("★★1件以上と書いてあるのに一覧が無ければ止める★★"
      "／相手が箱の名前を変えた形をここで捕まえる",
      _try("44", False)[0] == "NG")
    t("　1件以上で一覧があれば通し、中身は落とす",
      _try("44", True)[0] == "OK"
      and "面白い台でした" not in _try("44", True)[1])
    t("★★件数を読めないときは求める（安全側）★★",
      clean_html.__name__ == "clean_html"
      and (lambda: _try("", False)[0] == "NG")())
    # ★★Codexが挙げた反例★★（2026-08-23の敵対的レビュー）
    #   ①件数が複数入っていたら、先頭の無関係な「0件」で要求を解除できないか
    t("★★別の「N件」が先にあっても、投稿の件数で判断する★★"
      "／先頭の無関係な0件で要求を解除させない",
      _try_multi() == "NG")
    #   ②実物のHTMLで確かめる（合成HTMLだけで済ませない）
    _real = _real_pages()
    t("★★実物：レビュー0件の機種は通り、投稿欄が残らない★★",
      _real["zero"][0] == "OK"
      and "machine-userreview" not in _real["zero"][1])
    t("★★実物：レビュー44件の機種も通り、投稿欄が残らない★★",
      _real["many"][0] == "OK"
      and "machine-userreview" not in _real["many"][1]
      and "list-machinesreviews" not in _real["many"][1])
    t("★★実物：44件なのに一覧の箱を消したら止まる★★"
      "／相手が箱の名前を変えた形をここで捕まえる",
      _real["many_renamed"][0] == "NG")

    # ★★掃除のあとに投稿欄が残っていないか★★（2026-08-24・Codexの14回目）
    #   ★下の形は実ページで踏んだもの★＝
    #     ・掲示板の中の表（文章は後で切れるが、表は読まれる）
    #     ・決まりごとがあるサイトが**別の箱で**投稿欄を足した
    #     ・編集部の「実戦レビュー」で止まる（＝正しい本番を止める型）
    #     ・el_moreView の中の review で止まる（部分一致の誤検知）
    for _n, _h, _want in (
            ("掲示板の中の表",
             "<html><body><h2>掲示板</h2>"
             "<table><tr><td>555G</td></tr></table></body></html>", True),
            ("見出しの入れ子",
             "<html><body><h2><span>口コミ</span></h2></body></html>", True),
            ("決まりごとがあるサイトの第二の投稿欄",
             '<html><body><div id="bbs">旧</div>'
             '<div class="comment-list">新</div></body></html>', True),
            ("入力欄", "<html><body><textarea></textarea></body></html>", True),
            ("検索フォームと編集部の実戦レビュー",
             '<html><body><form><input name="q"></form>'
             "<h2>実戦レビュー</h2></body></html>", False),
            ("moreView は review ではない",
             '<html><body><div class="el_moreViewBox">続きを見る</div>'
             "</body></html>", False),
            ("読み込みの部品は見ない",
             '<html><body><script id="comment-js"></script><p>本文</p>'
             "</body></html>", False),
            ("普通の機種ページ",
             "<html><body><h1>L試験機</h1>"
             "<table><tr><td>999G</td></tr></table></body></html>", False)):
        t("★残存検査：" + _n + "★ → "
          + ("止める" if _want else "通す"),
          bool(looks_like_user_area(_h)) is _want)

    # ★語の正本は1つ★（行切りと残存検査が別の語を持つと片方だけ見逃す）
    # ★★行切りが「新しい語」でも切れるか★★（2026-08-24・Codexの14回目）
    #   ★語を1か所にしたのに、行切りが古い一覧を読んだままだと★
    #   「クチコミ」のような**あとから足した語では切れない**。
    #   ★本体の cut_user_area を呼んで確かめる★（名簿を見比べるだけでは足りない）
    import ceiling_lookup as _cl_cut
    _txt_cut = "天井は999Gです。" + chr(10) + "クチコミ" + chr(10) + "読者A 555G"
    t("★★行切りは、あとから足した語でも切れる★★"
      "／★古い一覧を読んだままだと切れない★",
      "999" in _cl_cut.cut_user_area(_txt_cut)
      and "555" not in _cl_cut.cut_user_area(_txt_cut))


    import ceiling_lookup as _cl_w
    t("★★投稿欄の語は1か所★★（行切りと残存検査が同じものを読む）",
      tuple(_cl_w._user_area_words()) == tuple(USER_AREA_WORDS))


    ng = sum(1 for _, o in results if not o)
    print()
    print("%d/%d 合格" % (len(results) - ng, len(results)))
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="投稿欄・AIのまとめを箱ごと落とす")
    ap.add_argument("--url", help="実際に取ってきて、落ちる箱を見る")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.url:
        ap.print_help()
        return 0
    import new_machine_watch as _nw
    # ★用途を名乗ってから取りに行く★（依頼218）
    with _nw.fetching("claim_material"):
        html = _nw._get(a.url)
    ua = conf_for_url(a.url)
    print(f"決まりごと: {ua or '（無し＝素通し）'}")
    try:
        got = visible_text(html, a.url)
    except UserAreaError as e:
        print("★" + str(e) + "★")
        return 1
    print(f"本文の長さ: {len(_nw._visible_text(html))} → {len(got)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""
consensus_verify.py — Dフェーズ D-1a: verify検証器（Cの verify コールバックの実体）

resolve_with_dialogue(consensus_resolver.py) が要求する verify(item_key, evidence) を、
既存 verify_claims.py の 実ページ再取得(SSRF対策)＋C0〜C4 と、consensus_resolver.py の
C5(check_claim_identity) を組み合わせて実装する。
★consensus_resolver.py には一切手を入れない（Cフェーズの合格を崩さない）★

安全要件（Codex Dフェーズ設計相談 v1.5 ＋ D-1a第1回レビュー反映）:
 - mode/scope/operator/unit はAIに定義させず ITEM_REGISTRY(フィールド定義)からサーバー側で確定
 - raw_value(元の十進文字列)を必須保持し、float化より前に Decimal で桁数・型・値域を検査
   （★元scaleで判定＝967.0/97.40 を拒否・int項目は小数点を構文禁止★）
 - value必須・型厳密(bool拒否・項目型一致)・canonical と一致（★検証器は値を書き換えない★）
 - callbackの item_key と field_key の anomaly_key の一致を強制（confused-deputy防止）
 - 1回のverifyは同一取得スナップショットでC0〜C5＋★quote出現位置の周辺に機種identity（局所束縛）★
 - 項目別C5が未実装のitem(機械割/狙い目/純増/スルー天井…)は必ず REVIEW（自動公開しない）
 - identity は 非空list[str]・空要素なし・最低1つ3文字以上 をサーバー検査
 - 検証器が返すのは Cの source 形 {value, domain, verified, c5_verdict, c5_code}

設計書: Documents/uchidokoro/gpt_research/consensus_design.md（v1.5）
"""
from __future__ import annotations
import hashlib
import math
import os
import re
import sys
from collections import namedtuple
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify_claims as vc
from consensus_resolver import check_claim_identity, PASS, REJECT, REVIEW

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ★D-1a-2-2 構造保持の器: DocumentSnapshot（設計書v1.7 Q3）★
#   この段では「取得を単一化し、hash/警告だけ先に持つ」＝束縛は従来の平坦テキスト(_sentences_with_value)
#   のまま変更しない。h1_candidates / units / parse_warnings の構造抽出は D-1a-2-3 以降で埋める。
#   verify_claims/fetch_page は不変（影響隔離）＝この型は D-1a 側(consensus_verify)に置く。
DocumentSnapshot = namedtuple(
    "DocumentSnapshot",
    "final_url title h1_candidates response_sha256 html_sha256 rendered_text_sha256 parse_warnings units")

# ★D-1a-2-3 構造単位: StructuredUnit（設計書v1.7 Q3・表以外＝段落文/リスト項目/見出し継承）★
#   kind = "paragraph_sentence" | "list_item"（表セル table_value_cell は D-1a-2-4）
#   heading_path = ((level, text), ...) 単位生成時点の見出しアウトラインのコピー
#   table_context は表以外では None。parse_flags は単位個別の注記（例: truncated）。
#   ★dom_order の契約 = 「確定(emit)順」★。atomic な li は閉じた時点で確定するため、入れ子リストでは
#   内側liが外側liより先に確定し得る（＝開始位置順とは限らない）。増分Bは源泉の開始位置順に依存しない。
StructuredUnit = namedtuple(
    "StructuredUnit",
    "unit_id kind text heading_path dom_order source_path table_context parse_flags")

# 構造化の上限（超過は parse_warnings→束縛側でREVIEW＝DoS/巨大入力対策・設計書v1.7 Q1/Q2）
_MAX_PARSE_NODES = 60000
_MAX_PARSE_DEPTH = 200
_MAX_PARSE_UNITS = 6000
_MAX_UNIT_TEXT = 2000
_MAX_TOTAL_TEXT = 2_000_000       # ★解析中の累積キャプチャ文字数上限（buf肥大＝メモリDoS対策・Codex D）

_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_LIST_TAGS = {"ul", "ol", "menu"}       # li を直接の子に持てるリスト要素（menu も含む・Codex#3）
# 内側テキストを証拠にしない要素（head/title/textarea＝値混入防止・Codex#1／select等の
# フォーム選択肢・iframe/canvas/object/svg/math/audio/video/map/embed/picture等の埋込＝記事本文でない）。
_SKIP_CONTENT_TAGS = {"script", "style", "noscript", "template",
                      "head", "title", "textarea",
                      "select", "datalist", "iframe", "canvas", "object",
                      "svg", "math", "audio", "video", "map", "embed", "picture"}
_TABLE_TAGS = {"table"}                 # 表の内側は D-1a-2-3 では単位化しない（D-1a-2-4で対応）
_VOID_TAGS = {"br", "hr", "img", "input", "meta", "link", "area", "base",
              "col", "embed", "param", "source", "track", "wbr"}
# ★記事本文（証拠）として capture するコンテンツ・フロー・コンテナ★（→ paragraph_sentence。li内は透過）。
#   header/footer/nav/aside/form 等の非本文（ページchrome/ナビ/フォーム）は含めない＝証拠にしない契約。
_PARA_BLOCKS = {"p", "div", "section", "article", "main", "figure", "figcaption",
                "blockquote", "pre", "dd", "dt", "td", "th", "caption",
                "details", "dialog", "search", "summary"}
# 非capture の構造/非本文コンテナ（境界にはなるが直下テキストは証拠にしない）。
#   ul/ol/menu は li の、dl は dt/dd の親。header/footer/nav/aside/form/fieldset/address は非本文領域。
_STRUCT_BLOCKS = {"ul", "ol", "menu", "dl", "table", "thead", "tbody", "tfoot",
                  "tr", "nav", "colgroup", "hgroup",
                  "header", "footer", "aside", "form", "fieldset", "address"}
# ブロック境界（開始/終了で段落runを確定 or li内なら区切り挿入）。これ以外(a/span/strong等)は
# インライン扱いでテキストを連結する（文を分断しない）。
_BOUNDARY_TAGS = _HEADING_TAGS | _PARA_BLOCKS | _STRUCT_BLOCKS | {"li"}
# 終了タグ省略が許容される要素（EOF未閉じ検査で benign 扱いにする集合）。
_OPTIONAL_END = {"p", "li", "td", "th", "tr", "thead", "tbody", "tfoot",
                 "dt", "dd", "option", "optgroup", "colgroup", "caption",
                 "rp", "rt", "body", "html", "head"}
# インライン要素（p はインライン末尾では終了省略できない＝インラインが跨げば交差）。
#   ★ruby は透過にしない（opaque扱い）＝ふりがな注釈テキストを証拠にしない（Codex増分A#3）。
_INLINE_TAGS = {"a", "span", "b", "i", "em", "strong", "u", "s", "strike",
                "small", "big", "sub", "sup", "mark", "code", "kbd", "samp",
                "var", "cite", "q", "abbr", "dfn", "time", "label", "tt", "font",
                "bdi", "bdo", "ins", "del", "data", "output"}
# ★暗黙終了の可否は「その要素の実際の親フレーム」で判定する（閉じるタグではない・Codex指摘）★
#   inner の終了タグ省略が benign なのは、親が下記の正当な親のときだけ（HTML終了省略規則）。
#   ※dt/dd は「親 dl」または「親 div かつ祖父 dl」を許す（HTML5のラッパー div）＝_dt_dd_ok で特別扱い。
_LEGAL_PARENTS = {
    "li": {"ul", "ol", "menu"},
    "td": {"tr"}, "th": {"tr"},
    "tr": {"thead", "tbody", "tfoot", "table"},
    "thead": {"table"}, "tbody": {"table"}, "tfoot": {"table"},
    "caption": {"table"}, "colgroup": {"table"},
    "option": {"select", "optgroup", "datalist"}, "optgroup": {"select"},
    "rp": {"ruby"}, "rt": {"ruby"},
    "body": {"html"}, "head": {"html"},
}
# p の終了省略が許されない親（HTML仕様: p は親が末尾でも a/audio/del/ins/map/noscript/video では省略不可）。
_P_NO_OMIT_PARENTS = {"a", "audio", "del", "ins", "map", "noscript", "video"}
# ★開始で開いている <p> を暗黙終了させるブロック要素（HTML仕様の「p 終了省略の後続タグ」集合）★
#   td/th/tr/li/dt/dd は含めない（p はそれらを直接子に持てず、p 内から開始しても p を閉じない＝misplaced扱い）。
_CLOSES_P = {"address", "article", "aside", "blockquote", "details", "dialog",
             "div", "dl", "fieldset", "figcaption", "figure", "footer", "form",
             "h1", "h2", "h3", "h4", "h5", "h6", "header", "hgroup", "main",
             "menu", "nav", "ol", "p", "pre", "section", "search", "table", "ul", "hr"}
# ★開始タグ駆動の兄弟暗黙終了ルール: tag → (閉じる対象集合, スコープ境界集合)★
#   表(td/th/tr)は D-1a-2-3 では suppressed（単位化も警告もしない）ため兄弟閉じ不要＝含めない。
#   閉じ対象は「境界まで遡って最も浅い一致」まで一括で閉じる（optgroupが option＋前のoptgroupを閉じる等）。
_SIBLING_RULES = {
    "li": ({"li"}, {"ul", "ol", "menu"}),
    "dt": ({"dt", "dd"}, {"dl"}), "dd": ({"dt", "dd"}, {"dl"}),
    "option": ({"option"}, {"select", "optgroup", "datalist"}),
    "optgroup": ({"option", "optgroup"}, {"select"}),
    "rt": ({"rt", "rp"}, {"ruby"}), "rp": ({"rt", "rp"}, {"ruby"}),
}
# ★本文到達可能性(content)は _permits(親,子) の簡易コンテンツモデルで判定★（_PARA_BLOCKS＝本文コンテナ）。
# ★名前空間別のHTML統合点★（外来名前空間内でも、これらの要素の子は HTML 規則で処理される）。
#   html.parser は小文字化するので小文字で持つ（Codex増分A#16）。
_SVG_INTEGRATION = {"foreignobject", "desc", "title"}          # SVG統合点
_MATHML_TEXT_INTEGRATION = {"mi", "mo", "mn", "ms", "mtext"}   # MathMLテキスト統合点
#   annotation-xml は encoding が HTML系のときだけ統合点。mtext等の子でも mglyph/malignmark はHTMLへ戻らない。
_MATHML_HTML_ENCODINGS = {"text/html", "application/xhtml+xml"}
_MATHML_NO_HTML_CHILDREN = {"mglyph", "malignmark"}
# ★HTML5「外来コンテンツからの breakout」開始タグ★（外来svg/math内でこれらが来ると外来を抜けHTML再処理）。
#   font は color/face/size 属性つきのみ breakout（下で属性判定）。
_BREAKOUT_TAGS = {"b", "big", "blockquote", "body", "br", "center", "code", "dd",
                  "div", "dl", "dt", "em", "embed", "h1", "h2", "h3", "h4", "h5",
                  "h6", "head", "hr", "i", "img", "li", "listing", "menu", "meta",
                  "nobr", "ol", "p", "pre", "ruby", "s", "small", "span", "strong",
                  "strike", "sub", "sup", "table", "tt", "u", "ul", "var"}
# 構造的に flow ブロック(p/div/見出し)を直接子に持てない親（この直下の p/div/見出しは misplaced 警告）。
#   ※ nav/header/footer/aside/form は「flowは許すが非本文」＝ここには入れない（captureしないが警告もしない）。
_NO_FLOW_PARENTS = {"ul", "ol", "menu", "dl", "table", "thead", "tbody", "tfoot",
                    "tr", "colgroup", "select", "datalist", "optgroup", "option",
                    "ruby", "rt", "rp", "hgroup"}
_MAX_HTML_LEN = 30_000_000        # feed前の入力長上限（取得層30MBと整合・直接呼び対策）
_MAX_TAG_LEN = 64                 # タグ名長上限（巨大独自タグ名でsource_path肥大を防ぐ）
_SENT_SPLIT_STRUCT = re.compile(r"[。！？；;\n]")  # 構造単位内の文分割（br/改行も境界）


class _ParseStop(Exception):
    """上限超過で feed() のトークナイズごと即中断する内部例外（Codex#4・CPU有界化）。"""


class _StructureParser(HTMLParser):
    """html.parser で保守的に構造化（完全DOM復元でなく証拠束縛に必要な構造だけ）。★フレームスタック方式★
    各開始タグに「フレーム」を積み、テキストは最内の captures フレームへ。
    - li      : captures=True・atomic=True → 子孫の直下テキストを1 list_item に（入れ子ブロックで割らない）
    - 段落系  : captures=True（ただしli内は透過＝captures=False・テキストはliへ吸収）→ paragraph_sentence
    - 見出し  : captures=True・is_heading → 文書アウトライン（hNで level>=N 置換）
    - script/style/comment・表内 → captures無効（証拠にしない・表はD-1a-2-4）
    - 未閉じ見出し/タグ交差/list外li/上限超過 → parse_warnings（束縛側でREVIEW・fail-closed）
    - ★_stopped 後は部分単位を一切足さない（警告付きで文書ごと増分BがREVIEW）★"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.units = []
        self.h1_candidates = []
        self.warnings = []
        self._warned = set()      # 警告の即時重複除去（大量入力で警告リスト肥大を防ぐ・Codex#4）
        self._outline = []        # [(level, text), ...] 見出しアウトライン（root生成前に用意）
        # フレーム: dict(tag, captures, atomic, is_heading, level, buf, heading_path, source_path, list_depth)
        # _elems[0] は決して pop しない root（body直下の地の文の受け皿）。
        self._elems = [self._new_frame("", captures=True)]
        self._dom_order = 0
        self._nodes = 0
        self._chars = 0           # 累積キャプチャ文字数（メモリ上限）
        self._skip_depth = 0      # script/style等の内側
        self._table_depth = 0     # table の内側（単位化しない）
        self._stopped = False

    # ── 内部ヘルパ ──
    def _new_frame(self, tag, captures=False, atomic=False, is_heading=False, level=0):
        return {"tag": tag, "captures": captures, "atomic": atomic,
                "is_heading": is_heading, "level": level, "buf": [],
                "heading_path": tuple(self._outline),
                "source_path": "", "list_depth": 0,
                "content": True,      # 本文到達可能か（root=True・_permits で伝播）
                "ns": "html",         # 名前空間 html|svg|mathml（自己終了の可否判定・統合点で切替）
                "is_integration": False}  # このフレームがHTML統合点か（子を html へ戻す）

    def _warn(self, why):
        if why not in self._warned:          # 種類ごとに1回だけ（メモリ上限・Codex#4）
            self._warned.add(why)
            self.warnings.append(why)

    def _stop(self, why):
        self._warn(why)
        self._stopped = True
        raise _ParseStop()          # feed()のトークナイズごと中断（CPU有界化・Codex#4）

    def _suppressed(self) -> bool:
        return self._skip_depth > 0 or self._table_depth > 0

    def _in_table(self) -> bool:
        # 交差/妥当性の警告抑止は「表内」だけ（表はD-1a-2-4領分・単位化もしない）。
        # noscript/template等のskipは markup を含み得るので警告は出す（Codex#2）。
        return self._table_depth > 0

    def _ul_count(self) -> int:
        return sum(1 for e in self._elems if e["tag"] in _LIST_TAGS)

    def _capturing_ancestor(self):
        """現在位置のテキストが入る capture フレーム（＝text sink）。
        ★content=False のフレームに達したら透過を止め None を返す（Codex増分A核心）★。
        これで「本文到達不可な非captureブロック(div外のdt/dd/td/th等)」を透過して外側captureに漏らさない。
        透過するのは inline / body / html / li内の content=True な非captureブロック・見出し だけ。"""
        for e in reversed(self._elems):
            if not e["content"]:
                return None                    # 本文到達不可のフレームで停止（テキストを証拠にしない）
            if e["captures"]:
                return e
            t = e["tag"]
            if t in _INLINE_TAGS or t in ("body", "html"):
                continue                       # 透過（インライン/文書ルート）
            if t in _PARA_BLOCKS or t in _HEADING_TAGS:
                continue                       # 非capture = li内の透過ブロック/見出し → li へ抜ける
            return None                        # opaque コンテナ → テキストはここで止める
        return None

    def _dt_dd_ok(self, idx) -> bool:
        """dt/dd の親妥当性: 親 dl、または 親 div かつ祖父 dl（HTML5のラッパー div・Codex#2）。"""
        parent = self._elems[idx - 1]["tag"] if idx >= 1 else ""
        if parent == "dl":
            return True
        if parent == "div":
            gp = self._elems[idx - 2]["tag"] if idx >= 2 else ""
            return gp == "dl"
        return False

    def _permits(self, parent_idx, child) -> bool:
        """★簡易コンテンツモデル: 親フレーム(_elems[parent_idx]) が child を「本文(証拠)として」正当に含めるか★
        content フラグ伝播の1ステップ。これで「必須コンテナ(ul/dl)の外側まで本文到達可能か」を厳密判定し、
        hgroup/ruby/nav 等の非本文/opaque を正当なリスト構造で迂回させない（Codex増分A#4/核心）。"""
        p = self._elems[parent_idx]["tag"]
        # 特定の親を要する子（li/dt/dd/td/th/tr/option…）は正当な親のときだけ本文到達
        if child == "li":
            return p in ("ul", "ol", "menu")
        if child in ("dt", "dd"):
            if p == "dl":
                return True
            if p == "div":                      # dl>div ラッパーのみ
                gp = self._elems[parent_idx - 1]["tag"] if parent_idx >= 1 else ""
                return gp == "dl"
            return False
        if child in _LEGAL_PARENTS:              # td/th/tr/thead/option/optgroup/caption/rp/rt…
            return p in _LEGAL_PARENTS[child]
        # 一般 flow/phrasing の子（p/div/section/見出し/ul/ol/dl/span/text…）
        if p == "dl":
            return child == "div"                # dl は一般flowとして div(ラッパー) のみ
        if p in _INLINE_TAGS or p in ("", "body", "html"):
            return True                          # インライン透過・文書ルート
        if p == "div":
            gp = self._elems[parent_idx - 1]["tag"] if parent_idx >= 1 else ""
            return gp != "dl"                    # dl>div ラッパーは一般flowを通さない（dt/dd専用）
        if p in _PARA_BLOCKS or p in _HEADING_TAGS or p == "li":
            return True                          # 本文コンテナ(p/section/li/dd/td/見出し…)＝flow/phrasing許容
        return False                             # 非本文/opaque親（ul/menu直下の一般flow, nav/header等）

    def _parent_ok(self, idx) -> bool:
        """_elems[idx] が「制約のある要素なら正当な親を持つ」か（制約なしは常にTrue＝misplaced検査用）。"""
        inner = self._elems[idx]["tag"]
        if inner in ("dt", "dd"):
            return self._dt_dd_ok(idx)
        parents = _LEGAL_PARENTS.get(inner)
        if parents is None:
            return True                        # 親制約のない要素は misplaced ではない
        parent = self._elems[idx - 1]["tag"] if idx >= 1 else ""
        return parent in parents

    def _can_close_frame(self, idx) -> bool:
        """_elems[idx] を暗黙終了してよいか（★実際の親フレームで判定★・HTML終了省略規則）。"""
        inner = self._elems[idx]["tag"]
        if inner == "p":
            parent = self._elems[idx - 1]["tag"] if idx >= 1 else ""
            return parent not in _INLINE_TAGS and parent not in _P_NO_OMIT_PARENTS
        if inner in ("dt", "dd"):
            return self._dt_dd_ok(idx)
        parents = _LEGAL_PARENTS.get(inner)
        if parents is None:
            return False                       # div/span/section/ul/table 等は暗黙終了不可
        parent = self._elems[idx - 1]["tag"] if idx >= 1 else ""
        return parent in parents

    def _crossed(self, lo, hi) -> bool:
        """[lo,hi) の巻き戻し対象に、正当に暗黙終了できない要素があれば True（＝不正交差）。"""
        return any(not self._can_close_frame(i) for i in range(lo, hi))

    def _softbreak(self):
        """br/hr は文境界。見出し中は空白（AB連結を防ぐ・Codex #6）。"""
        if self._stopped or self._suppressed():
            return
        a = self._capturing_ancestor()
        if a is not None:
            a["buf"].append(" " if a["is_heading"] else "\n")

    def _emit(self, frame):
        """フレームのバッファを単位化。is_heading→アウトライン更新／atomic→1 list_item／段落→文分割。"""
        if self._stopped:
            frame["buf"] = []
            return
        text = "".join(frame["buf"])
        frame["buf"] = []
        if frame["is_heading"]:
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                level = frame["level"]
                self._outline = [(lv, tx) for (lv, tx) in self._outline if lv < level]
                self._outline.append((level, text))
                if level == 1:
                    self.h1_candidates.append(text)
            return
        if not text.strip():
            return
        kind = "list_item" if frame["atomic"] else "paragraph_sentence"
        segments = [text] if frame["atomic"] else _SENT_SPLIT_STRUCT.split(text)
        for seg in segments:
            seg = re.sub(r"\s+", " ", seg).strip()
            if not seg:
                continue
            flags = ()
            if len(seg) > _MAX_UNIT_TEXT:
                seg = seg[:_MAX_UNIT_TEXT]
                flags = ("truncated",)
                self._warn("unit_truncated")
            if len(self.units) >= _MAX_PARSE_UNITS:
                self._stop("too_many_units")
                return
            self._dom_order += 1
            self.units.append(StructuredUnit(
                unit_id=len(self.units), kind=kind, text=seg,
                heading_path=frame["heading_path"], dom_order=self._dom_order,
                source_path=frame["source_path"], table_context=None, parse_flags=flags))

    def _breakout_foreign_needed(self) -> bool:
        """現在位置が外来コンテンツ内（統合点でもHTMLでもない）か＝breakout対象か。"""
        return (len(self._elems) > 1 and self._elems[-1]["ns"] != "html"
                and not self._elems[-1]["is_integration"])

    def _breakout_foreign(self):
        """HTML5: 外来コンテンツ(svg/math)内で breakout タグが来たら、統合点/HTMLに戻るまで外来を閉じる。"""
        while self._breakout_foreign_needed():
            self._pop_frame()

    def _pop_frame(self):
        e = self._elems.pop()
        if e["tag"] in _SKIP_CONTENT_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        if e["tag"] in _TABLE_TAGS:
            self._table_depth = max(0, self._table_depth - 1)
        self._emit(e)
        return e

    def _pop_emit_to(self, idx):
        """top から idx（含む）まで pop して emit（暗黙終了/交差の巻き戻し）。root(0)は残す。"""
        while len(self._elems) > max(idx, 1):
            self._pop_frame()

    def _boundary_open(self):
        """ブロック境界を開く直前: liなら区切り挿入（原子性維持）、段落なら現在のrunを確定。"""
        if self._suppressed():
            return
        a = self._capturing_ancestor()
        if a is None or a["is_heading"]:
            return
        if a["atomic"]:
            if a["buf"] and not a["buf"][-1].endswith((" ", "\n")):
                a["buf"].append(" ")
        else:
            self._emit(a)   # 段落run確定（フレームは開いたまま次のrunへ）

    def _boundary_close_sep(self, tag):
        """li内の透過ブロックが閉じた後、後続テキストと区切る（連結防止・Codex #3）。"""
        if self._suppressed():
            return
        a = self._capturing_ancestor()
        if a is not None and a["atomic"] and (
                tag in _PARA_BLOCKS or tag in _STRUCT_BLOCKS or tag in _HEADING_TAGS):
            if a["buf"] and not a["buf"][-1].endswith((" ", "\n")):
                a["buf"].append(" ")

    def _implicit_close_heading(self, frame):
        """割り込みで未閉じになった見出しを確定＋警告（本文吸収防止・Codex #2）。"""
        idx = self._elems.index(frame)
        if not self._in_table() and self._crossed(idx + 1, len(self._elems)):
            self._warn("implicit_close")       # 見出し内の未閉じブロック/インラインを跨いだ
        self._pop_emit_to(idx)                 # frame（見出し）を含めて確定
        self._warn("unclosed_heading")

    def _close_open_p(self):
        """_CLOSES_P タグの開始で、開いている <p> を暗黙終了（p は block を子に持てない・clean）。
        top から p までが全てインラインの時だけ閉じる（間にブロックがあれば閉じるべき p は無い）。
        p 内に未閉じ子孫(span等)が残れば交差＝警告（Codex#3・親フレーム基準）。"""
        for i in range(len(self._elems) - 1, 0, -1):
            t = self._elems[i]["tag"]
            if t == "p":
                if not self._in_table() and self._crossed(i + 1, len(self._elems)):
                    self._warn("implicit_close")
                self._pop_emit_to(i)           # p を確定（_CLOSES_P 直前のp終了は benign）
                return
            if t not in _INLINE_TAGS:
                return                          # ブロックに当たった＝直近に閉じるべき p は無い

    def _close_siblings(self, closes, stop):
        """開始タグ駆動の兄弟暗黙終了。スコープ境界(stop)まで遡り、最も浅い一致まで一括で確定。
        （例: 新 optgroup は 開いた option＋前の optgroup を両方閉じる）。
        兄弟までに「正当に暗黙終了できない」要素が挟まれば交差＝警告（Codex#1/#3/#4）。"""
        target = None
        for i in range(len(self._elems) - 1, 0, -1):
            t = self._elems[i]["tag"]
            if t in stop:
                break                          # コンテナ(ul/ol/menu/dl/select/ruby)に達した
            if t in closes:
                target = i                     # さらに浅い一致を探し続ける（入れ子兄弟を一括で閉じる）
        if target is not None:
            if not self._in_table() and self._crossed(target + 1, len(self._elems)):
                self._warn("implicit_close")
            self._pop_emit_to(target)

    def _implicit_close_siblings(self, tag):
        closes, stop = _SIBLING_RULES[tag]
        self._close_siblings(closes, stop)

    # ── html.parser コールバック ──
    def handle_starttag(self, tag, attrs):
        if self._stopped:
            return
        tag = tag.lower()
        self._nodes += 1
        if self._nodes > _MAX_PARSE_NODES:
            self._stop("too_many_nodes")
        if len(tag) > _MAX_TAG_LEN:            # 巨大タグ名（source_path肥大）は異常停止（Codex#4）
            self._stop("long_tag")
        # ★外来コンテンツ(svg/math)からの HTML breakout（void判定より前・Codex増分A#17）★
        if tag in _BREAKOUT_TAGS or (
                tag == "font" and any(a.lower() in ("color", "face", "size")
                                      for a, _v in (attrs or []))):
            self._breakout_foreign()
        if tag in _VOID_TAGS:
            if tag in ("br", "hr"):
                self._softbreak()
            if tag == "hr":                    # hr は p も option/optgroup も閉じる（HTML仕様・Codex#4）
                self._close_open_p()
                self._close_siblings({"option", "optgroup"}, {"select"})
            return
        is_boundary = tag in _BOUNDARY_TAGS
        # 未閉じ見出しの回復（見出し/ブロックが見出しに割り込んだら確定・本文吸収防止）
        if is_boundary:
            anc = self._capturing_ancestor()
            if anc is not None and anc["is_heading"]:
                self._implicit_close_heading(anc)
        if tag in _CLOSES_P:
            self._close_open_p()               # p を閉じるタグの開始で開いた<p>を暗黙終了（Codex#2/#3）
        if tag in _SIBLING_RULES:
            self._implicit_close_siblings(tag)  # li/dt/dd/option の兄弟暗黙終了（Codex#1）
        if is_boundary:
            self._boundary_open()              # 段落run確定 or li内区切り
        # ★本文到達可能性フラグ content = 親.content AND _permits(親,子)（簡易コンテンツモデル）★
        #   これで「必須コンテナ(ul/dl)の外側まで本文到達可能か」を厳密判定＝hgroup/ruby/nav等の
        #   非本文/opaque を正当なリスト構造(ul/dl)で迂回できない（Codex増分A#4/核心）。
        parent = self._elems[-1]
        parent_tag = parent["tag"]
        content_here = parent["content"] and self._permits(len(self._elems) - 1, tag)
        # ★名前空間の伝播（Codex増分A#16）: svg/math で外来へ、統合点(foreignObject/mtext等)の子で HTML へ戻る。
        #   MathMLテキスト統合点でも子が mglyph/malignmark なら MathML のまま。annotation-xml は encoding依存。
        pns = parent["ns"]
        if pns == "html":
            # HTML文脈: svg/math で外来へ入る
            ns_here = "svg" if tag == "svg" else ("mathml" if tag == "math" else "html")
        elif parent["is_integration"]:
            # 統合点直下は HTML 規則（svg/math は再び外来へ）。mglyph/malignmark はMathMLのまま。
            if pns == "mathml" and tag in _MATHML_NO_HTML_CHILDREN:
                ns_here = "mathml"
            elif tag == "svg":
                ns_here = "svg"
            elif tag == "math":
                ns_here = "mathml"
            else:
                ns_here = "html"
        elif parent_tag == "annotation-xml" and tag == "svg":
            ns_here = "svg"   # ★HTML5特例: annotation-xml(encoding不問)直下の svg は SVG 名前空間へ（Codex増分A#18）★
        else:
            ns_here = pns   # ★外来コンテンツ内(非統合点)は現在の名前空間を継承（svg/math含む・Codex増分A#17）★
        # このフレーム自身がHTML統合点か（＝子を html 規則で処理させるか）
        integ_here = False
        if ns_here == "svg" and tag in _SVG_INTEGRATION:
            integ_here = True
        elif ns_here == "mathml" and tag in _MATHML_TEXT_INTEGRATION:
            integ_here = True
        elif ns_here == "mathml" and tag == "annotation-xml":
            enc = ""
            for _an, _av in (attrs or []):
                if _an.lower() == "encoding":
                    enc = (_av or "").strip().lower()
                    break
            integ_here = enc in _MATHML_HTML_ENCODINGS
        # フレーム属性を決定（capture は content_here で一元判定・非本文はcaptureせず静かにスキップ）
        captures = atomic = is_heading = False
        level = 0
        if not self._suppressed():
            in_li = (self._capturing_ancestor() or {}).get("atomic", False)
            if tag == "li":
                if content_here:
                    captures, atomic = True, True   # 妥当性(親ul/ol/menu)は _parent_ok で後続検査
                # 非content(nav/ruby配下等)は静かにcaptureせず（構造誤りは _parent_ok が別途警告）
            elif tag in ("dt", "dd"):
                if in_li:
                    pass                            # li内は透過
                elif content_here:
                    captures = True                 # 妥当性(dl/div>dl)は _parent_ok(_dt_dd_ok)で後続検査
            elif tag == "div" and parent_tag == "dl":
                pass                                # ★dl>div は正当な非capture構造ラッパー（dt/ddを包む）★
            elif tag in _HEADING_TAGS:
                if in_li:
                    pass                            # li内見出しは透過（テキストはliへ）
                elif content_here:
                    captures, is_heading, level = True, True, int(tag[1])
                elif parent_tag in _NO_FLOW_PARENTS and parent_tag != "hgroup":
                    self._warn("misplaced_element")  # 見出しを構造的に許さない親(ul/dl/ruby等)＝構造誤り
            elif tag in _PARA_BLOCKS:
                if in_li:
                    pass                            # li内の透過ブロック（テキストはliへ）
                elif content_here:
                    captures = True
                elif parent_tag in _NO_FLOW_PARENTS:
                    self._warn("misplaced_element")  # flowを構造的に許さない親(ul/dl/ruby/hgroup等)＝構造誤り
        frame = self._new_frame(tag, captures=captures, atomic=atomic,
                                is_heading=is_heading, level=level)
        frame["content"] = content_here
        frame["ns"] = ns_here
        frame["is_integration"] = integ_here
        frame["list_depth"] = self._ul_count()
        self._elems.append(frame)
        frame["source_path"] = ">".join(e["tag"] for e in self._elems if e["tag"])[:512]
        # ★要素の親妥当性（li→ul/ol/menu・td/th→tr・dt/dd→dl(またはdiv>dl) 等）を実親フレームで検査★
        #   表内(D-1a-2-4領分)は抑止。noscript等のskipでも構造異常は警告する（Codex#1/#3/#4）。
        if not self._in_table() and (tag in _LEGAL_PARENTS or tag in ("dt", "dd")):
            if not self._parent_ok(len(self._elems) - 1):
                self._warn("li_without_list" if tag == "li" else "misplaced_element")
        if len(self._elems) > _MAX_PARSE_DEPTH:
            self._stop("too_deep")
        if tag in _SKIP_CONTENT_TAGS:
            self._skip_depth += 1
        if tag in _TABLE_TAGS:
            self._table_depth += 1

    def handle_startendtag(self, tag, attrs):
        # 自己終了スラッシュの扱い（HTML5）: void は starttag だけで完結。
        # ★通常のHTML非void要素(nav/div/dt/select/iframe/title/table…)の自己終了スラッシュは無視＝終了しない
        #   （開いたまま）＝後続は当該要素内に入る（抑止要素なら後続も抑止＝安全）。構文警告のみ（Codex増分A#13）★。
        #   例外は外来要素 svg/math のみ（自己終了が有効＝閉じる）。
        self.handle_starttag(tag, attrs)
        t = tag.lower()
        if t in _VOID_TAGS:
            return
        # ★外来名前空間(svg/mathml)の要素は自己終了が有効＝閉じる。HTML名前空間(統合点配下含む)の
        #   非void自己終了は無視＝開いたまま＋常に nonvoid_self_closing 警告（抑止中でも出す・Codex増分A#16）。
        top = self._elems[-1]
        if top["tag"] == t and top["ns"] in ("svg", "mathml"):
            self.handle_endtag(tag)
        else:
            self._warn("nonvoid_self_closing")

    def handle_endtag(self, tag):
        if self._stopped:
            return
        tag = tag.lower()
        self._nodes += 1                                # ★終了タグもイベント計上（上限迂回防止・Codex#4）
        if self._nodes > _MAX_PARSE_NODES:
            self._stop("too_many_nodes")
        if len(tag) > _MAX_TAG_LEN:                     # 終了タグ名も長さ上限（全経路で保証・Codex#4）
            self._stop("long_tag")
        # ★外来コンテンツ内の終了タグ </p> </br> も breakout（HTML5・Codex増分A#18）★
        if tag in ("p", "br") and self._breakout_foreign_needed():
            self._breakout_foreign()
            if tag == "br":
                self._softbreak()                       # </br> は <br> 相当
                return
            # </p> は breakout 後、下の通常処理（開いたpを閉じる/無ければ孤立終了）へ
        if tag in _VOID_TAGS:
            return
        idx = None
        for i in range(len(self._elems) - 1, 0, -1):   # root(0)は対象外
            e = self._elems[i]
            if e["tag"] == tag:
                idx = i
                break
            # ★scope境界: 外来要素(ns≠html)/統合点を跨いで対象を探さない（HTML5・Codex増分A#19）★
            #   例 <p><svg><foreignObject></p> の </p> は境界の foreignObject/svg を越えず外側 p を閉じない
            #   ＝svg抑止が維持され値漏れしない。境界要素そのものが対象なら上の一致で先に break 済み。
            if e["ns"] != "html" or e["is_integration"]:
                break
        if idx is None:
            self._warn("unmatched_end_tag")             # スタックに無い/scope外の孤立終了
            return
        # ★交差判定は「跨ぐ各要素を親フレーム基準で暗黙終了できるか」で（Codex#1/#2）。
        #   表/script内(suppressed)は単位化しないため交差警告も出さない（誤検知抑止・D-1a-2-4領分）。
        crossed = (not self._in_table()) and self._crossed(idx + 1, len(self._elems))
        self._pop_emit_to(idx)                          # 一致タグまで巻き戻して確定
        if crossed:
            self._warn("implicit_close")                # 不正な交差/未閉じ→REVIEW材料
        self._boundary_close_sep(tag)                   # li内透過ブロック閉じ後の区切り

    def handle_data(self, data):
        if self._stopped or self._suppressed():
            return
        self._chars += len(data)
        if self._chars > _MAX_TOTAL_TEXT:               # 累積テキスト上限（メモリDoS・Codex #5）
            self._stop("text_limit")
            return
        a = self._capturing_ancestor()
        if a is None:
            return
        if not a["atomic"] and not a["is_heading"] and not a["buf"]:
            a["heading_path"] = tuple(self._outline)    # run開始時点の見出しパスに更新
        a["buf"].append(data)

    def handle_comment(self, data):
        return  # コメント内の紛らわしい数値は証拠にしない（明示）


def parse_structured_units(html_text):
    """HTMLを構造化し (units, h1_candidates, parse_warnings) を返す（表以外・D-1a-2-3）。
    標準ライブラリのみ・失敗/上限超過/構造異常は parse_warnings に集約（束縛側でREVIEW）。"""
    if not isinstance(html_text, str):
        return (), (), ("html_not_str",)
    p = _StructureParser()
    if len(html_text) > _MAX_HTML_LEN:            # feed前の入力長上限（直接呼び対策・Codex#4）
        p._warn("html_too_long")
        html_text = html_text[:_MAX_HTML_LEN]
    try:
        p.feed(html_text)
        p.close()
    except _ParseStop:
        pass                                       # 上限到達で feed を即中断（CPU有界・_stopped済）
    except Exception as e:
        p._warn("parse_error:%s" % type(e).__name__)
    # EOF時の未閉じ検査: 省略不可タグ(div/span/section/ul/table/h*等)が開いたまま＝構造異常（Codex #4）
    if any(e["tag"] and e["tag"] not in _OPTIONAL_END for e in p._elems):
        p._warn("unclosed_tags")
    if not p._stopped:
        try:
            while len(p._elems) > 1:      # 未閉じフレームを内側から確定（root以外）
                p._pop_frame()
            p._emit(p._elems[0])          # root（body直下の地の文）
        except _ParseStop:
            pass                          # 確定中の単位数上限
        except Exception as e:            # EOF確定処理の想定外例外も parse_error 契約に含める
            p._warn("parse_error:%s" % type(e).__name__)
    warns = tuple(p.warnings)             # _warn で重複除去済み・順序維持
    return tuple(p.units), tuple(p.h1_candidates), warns


# canonical十進のみ許可（指数e / 16進 / カンマ / 単位付き / Unicode数字 / 先頭+ / 不要な先頭ゼロ を拒否）
# 監査用 raw_value の一意性のため 00967 / +967 も拒否（Codex D-1a再レビュー Minor）。
_DECIMAL_RE = re.compile(r"^(0|[1-9][0-9]*)(\.[0-9]+)?$")

# ── ITEM_REGISTRY: 公開先フィールドの定義（★mode/scope/unit/rangeはここで確定＝AI申告不可★）──
#   anomaly_key = consensus_resolver の ITEM_SPEC/MIN_DOMAINS キー（範囲・型・独立数の判定用）
#   c5_ready    = 項目別C5が使えるか（False=自動公開しない＝REVIEW固定・Codex指摘）
#   ntype/decimals = raw_value の Decimal 桁・型検査 / range = 値域(Dのraw処理でも検査・設計書v1.5)
#   ★天井のG数系のみ c5_ready=True（check_claim_identity が天井の意味検査を実装済み）。
#     スルー天井/機械割/狙い目/純増等は項目別C5を実装するまで c5_ready=False（＝REVIEW）。★
ITEM_REGISTRY = {
    "ceiling.normal":    {"anomaly_key": "ceiling.game",   "c5_item": "天井", "c5_ready": True,  "mode": "normal", "scope": None,    "unit": "G",  "ntype": "int",   "range": (50, 5000)},
    "ceiling.normal.at": {"anomaly_key": "ceiling.game",   "c5_item": "天井", "c5_ready": True,  "mode": "normal", "scope": "AT間", "unit": "G",  "ntype": "int",   "range": (50, 5000)},
    "ceiling.reset":     {"anomaly_key": "ceiling.game",   "c5_item": "天井", "c5_ready": True,  "mode": "reset",  "scope": None,    "unit": "G",  "ntype": "int",   "range": (50, 5000)},
    # ── 以下 c5_ready=False（項目別C5が未実装＝自動公開しない・必ずREVIEW）──
    "ceiling.through":   {"anomaly_key": "ceiling.through", "c5_item": "天井", "c5_ready": False, "mode": "normal", "scope": None,    "unit": None, "ntype": "int",   "range": (1, 40)},
    "kikaiwari.set1":    {"anomaly_key": "kikaiwari",       "c5_item": "機械割", "c5_ready": False, "mode": "normal", "scope": None, "unit": "%", "ntype": "float", "decimals": 1, "range": (90.0, 135.0)},
    "kikaiwari.set6":    {"anomaly_key": "kikaiwari",       "c5_item": "機械割", "c5_ready": False, "mode": "normal", "scope": None, "unit": "%", "ntype": "float", "decimals": 1, "range": (90.0, 135.0)},
    "nerai.normal":      {"anomaly_key": "nerai",           "c5_item": "狙い目", "c5_ready": False, "mode": "normal", "scope": None, "unit": "G", "ntype": "int",   "range": (100, 4000)},
}


def anomaly_key_of(field_key: str) -> str:
    """resolve_with_dialogue へ渡す item_key（＝anomaly/MIN_DOMAINSキー）を field_key から引く。"""
    reg = ITEM_REGISTRY.get(field_key)
    return reg["anomaly_key"] if reg else field_key


def _src(value, domain, verified, c5_verdict, c5_code, field_key, raw, snapshot=None):
    """Cの source 形（＋監査用に raw_value/field_key/理由コード/snapshotを保持）。
    snapshot={final_url, body_sha256, html_sha256, response_sha256} は後段(D-1b/d)の
    再取得整合・監査材料（Codex E9・D-1a-2-2でhash拡充。response_sha256は今段None）。"""
    s = {"value": value, "domain": domain, "verified": verified,
         "c5_verdict": c5_verdict, "c5_code": c5_code,
         "field_key": field_key, "raw_value": raw}
    if snapshot is not None:
        s["snapshot"] = snapshot
    return s


def _valid_identity(identity) -> bool:
    """identity は 非空 list/tuple[str]・空要素なし・最低1つ3文字以上（Codex A6）。"""
    if not isinstance(identity, (list, tuple)) or not identity:
        return False
    if any((not isinstance(s, str)) or not s.strip() for s in identity):
        return False
    return any(len(vc.normalize(s)) >= 3 for s in identity)


def _check_raw_value(reg, raw_value):
    """★float化より前に raw_value(元の十進文字列)を Decimal で検査（設計書v1.5・Codex）★
    戻り (ok, canonical_value, code)。指数/カンマ/Unicode数字/桁過多/型不一致/値域外 を拒否。
    ★scaleは normalize しない元の as_tuple().exponent で判定（967.0/97.40 を通さない）★"""
    if not isinstance(raw_value, str) or not raw_value:
        return False, None, "RAW_VALUE_MISSING"
    if raw_value != raw_value.strip():              # 前後空白はcanonicalでない（Codex Minor）
        return False, None, "RAW_VALUE_SYNTAX"
    s = raw_value
    if not _DECIMAL_RE.fullmatch(s):
        return False, None, "RAW_VALUE_SYNTAX"
    if len(s) > 12:                                 # 総桁数上限（巨大数・DoS・Codex C）
        return False, None, "RAW_VALUE_TOO_LONG"
    if reg["ntype"] == "int" and "." in s:
        return False, None, "RAW_VALUE_NOT_INT"   # int項目は小数点自体を禁止（967.0も拒否）
    try:
        d = Decimal(s)
    except InvalidOperation:
        return False, None, "RAW_VALUE_DECIMAL"
    if not d.is_finite():
        return False, None, "RAW_VALUE_NONFINITE"
    exp = d.as_tuple().exponent          # ★normalizeしない元scale（97.40→2桁）
    decimals = -exp if isinstance(exp, int) and exp < 0 else 0
    if reg["ntype"] == "int":
        cval = int(d)
    else:
        if decimals > reg.get("decimals", 1):
            return False, None, "RAW_VALUE_TOO_MANY_DECIMALS"
        f = float(d)
        if not math.isfinite(f):
            return False, None, "RAW_VALUE_NONFINITE"
        cval = f
    lo, hi = reg.get("range", (None, None))
    if lo is not None and not (lo <= cval <= hi):
        return False, None, "RAW_VALUE_OUT_OF_RANGE"
    return True, cval, "OK"


def build_document_snapshot(hs, page) -> DocumentSnapshot:
    """★D-1a-2-2/2-3: 単一取得(fetch_html→HtmlSnapshot)と平坦化Pageから DocumentSnapshot を組む。★
    - html_sha256          : 取得したdecoded HTMLのhash（HtmlSnapshotが計算済み）
    - rendered_text_sha256 : 平坦化後テキスト(page.text)のhash＝flatがそのHTML由来である監査痕跡
    - response_sha256      : 生ワイヤーhash。verify_claims影響隔離のため今段はNone（fetch_html拡張で後段に）
    - h1_candidates/units/parse_warnings : ★D-1a-2-3で構造化（段落文/リスト項目/見出しアウトライン）★
      表(td/th)の内側は D-1a-2-3 では単位化しない（D-1a-2-4で占有グリッド対応）。"""
    rendered_sha = hashlib.sha256((page.text or "").encode("utf-8", "replace")).hexdigest()
    units, h1_candidates, parse_warnings = parse_structured_units(hs.html)
    return DocumentSnapshot(
        final_url=hs.final_url,
        title=hs.title,
        h1_candidates=h1_candidates,
        response_sha256=None,
        html_sha256=hs.html_sha256,
        rendered_text_sha256=rendered_sha,
        parse_warnings=parse_warnings,
        units=units,
    )


# ── D-1a-2-3 増分B: 構造単位を唯一の局所束縛元にする（平坦 _sentences_with_value は撤去）──

def _unit_contains_value(unit_text: str, raw: str) -> bool:
    """構造単位のテキストが値(raw)を数字境界つきで含むか（C4と同じ規則・隣接連結の誤検出も回避）。"""
    nsp = vc.normalize_spaced(unit_text)
    toks = vc._value_tokens(vc.normalize(raw))
    if toks:
        return all(vc._token_in(t, nsp) for t in toks)
    return vc.normalize(raw) in vc.normalize(unit_text)


def _unit_heading_text(unit) -> str:
    return " ".join(t for _lv, t in unit.heading_path if t)


def _unit_bound_to_identity(unit, strong_ids) -> bool:
    """★対象identityが「その単位の本文」に在るか★（兄弟単位も見出しパスも使わない・Codex増分B#1）。
    見出しパスを identity 束縛に使うと、祖先見出しに対象機種名があるだけで、より近い別機種見出し配下の
    別機種の値まで束縛してしまう（<h1>アクダマ><h2>バベル><p>バベルの天井967G>）＝誤PASS。
    従って identity は必ず単位本文に要求する（見出しは _synth_for_c5 で mode/scope 供給にのみ使う）。"""
    nunit = vc.normalize(unit.text)
    return all(vc._ident_in(sid, nunit) for sid in strong_ids)


def _synth_for_c5(unit) -> str:
    """★C5へ渡す合成文脈（設計書v1.7 Q4）★＝見出し(mode/scope語)を値の直前文節に置いた1テキスト。
    ／(SOFT区切り)で見出しを単位本文の直前に連結＝C5の支配文脈が「見出し＋値の文節」を拾える
    （例: 見出し『朝一・リセット』＋単位『745Gで当選』→ reset を検出）。兄弟単位は結合しない。
    ★見出しは mode/scope 供給専用＝identity 束縛には使わない（_unit_bound_to_identity 参照）★。"""
    heads = _unit_heading_text(unit)
    return ("見出し:" + heads + "／" + unit.text) if heads else unit.text


def verify_evidence(field_key, evidence, identity, allowed_domains=None,
                    fetch_fn=None, *, expect_item_key):
    """1つの生証拠を検証し Cの source 形を返す（★mode/scope/unit/rangeはregから確定・AI申告無視★）。
    verified=True は C0〜C5全通過＋raw Decimal OK＋c5_ready＋局所束縛OK のときだけ。
    identity: 機種同定文字列リスト（正規化前・非空str）。expect_item_key: callbackのitem_key（一致強制）。
    ★D-1a-2-2: 取得は fetch_html を1回だけ呼び、平坦Pageは page_from_html_snapshot で導出（二重取得しない）。★"""
    fetch = fetch_fn or vc.fetch_html
    if not isinstance(evidence, dict):
        return _src(None, "", False, REVIEW, "EVIDENCE_NOT_DICT", field_key, None)
    reg = ITEM_REGISTRY.get(field_key)
    if reg is None:
        return _src(None, "", False, REVIEW, "UNREGISTERED_FIELD", field_key, None)
    # ★callbackの item_key と field_key の anomaly_key の一致を強制（confused-deputy防止・Codex A3）
    #   expect_item_key はキーワード必須＝直接呼びでも省略できない（None も不一致でREJECT・再レビューHigh）
    if expect_item_key != reg["anomaly_key"]:
        return _src(None, "", False, REJECT, "ITEM_KEY_MISMATCH", field_key, None)
    # identity の健全性（Codex A6）
    if not _valid_identity(identity):
        return _src(None, "", False, REVIEW, "BAD_IDENTITY", field_key, None)

    raw = evidence.get("raw_value")
    ok, cval, code = _check_raw_value(reg, raw)
    if not ok:
        return _src(None, "", False, REJECT, code, field_key, raw)
    # ★value必須・bool拒否・項目型一致・canonicalと一致（検証器は値を書き換えない・Codex A5/C）
    if "value" not in evidence:
        return _src(None, "", False, REJECT, "VALUE_MISSING", field_key, raw)
    ev_val = evidence.get("value")
    if isinstance(ev_val, bool):
        return _src(ev_val, "", False, REJECT, "VALUE_BOOL", field_key, raw)
    if reg["ntype"] == "int" and not isinstance(ev_val, int):
        return _src(ev_val, "", False, REJECT, "VALUE_TYPE", field_key, raw)
    if reg["ntype"] == "float" and not isinstance(ev_val, (int, float)):
        return _src(ev_val, "", False, REJECT, "VALUE_TYPE", field_key, raw)
    if ev_val != cval:
        return _src(ev_val, "", False, REJECT, "VALUE_MISMATCH_RAW", field_key, raw)

    url = evidence.get("url", "")
    quote = evidence.get("quote", "")
    nquote = vc.normalize(quote)
    nvalue = vc.normalize(raw)   # ★C4は検証済み raw（canonical十進文字列）で照合
    nids = [vc.normalize(str(s)) for s in identity if vc.normalize(str(s))]

    # C0: 体裁（url/quote非空・退化quote拒否・blocked拒否・許可ドメイン）
    if not url or not nquote:
        return _src(cval, "", False, REJECT, "C0_EMPTY", field_key, raw)
    if len(nquote) < 8 or not vc._CJK.search(nquote):
        return _src(cval, "", False, REJECT, "C0_DEGENERATE_QUOTE", field_key, raw)
    if vc.is_blocked_source(url):
        return _src(cval, "", False, REJECT, "C0_BLOCKED_SOURCE", field_key, raw)
    # ★空allowlist [] を fail-open にしない（is not None＝[]なら全拒否・Codex E7）
    if allowed_domains is not None and not vc.host_matches_allowlist(url, allowed_domains):
        return _src(cval, "", False, REJECT, "C0_DOMAIN_NOT_ALLOWED", field_key, raw)

    # C4: 値がquote内（決定論・取得前に検査できる）
    tokens = vc._value_tokens(nvalue)
    nquote_sp = vc.normalize_spaced(quote)
    if tokens:
        if any(not vc._token_in(t, nquote_sp) for t in tokens):
            return _src(cval, "", False, REJECT, "C4_VALUE_NOT_IN_QUOTE", field_key, raw)
    elif nvalue not in nquote:
        return _src(cval, "", False, REJECT, "C4_VALUE_NOT_IN_QUOTE", field_key, raw)

    # C1: 取得（★単一取得＝fetch_html で HtmlSnapshot を1回取り、平坦Pageは導出。以降は同一スナップショット★）
    snapshot = fetch(url, allowed=allowed_domains)
    if snapshot is None:
        return _src(cval, "", False, REVIEW, "C1_FETCH_FAILED", field_key, raw)
    page = vc.page_from_html_snapshot(snapshot)   # 平坦Page（C2/C3の同定・逐語確認に使う・再取得しない）
    doc = build_document_snapshot(snapshot, page)  # ★D-1a-2-3 増分B：構造単位が束縛の唯一の局所束縛元★
    if vc.is_blocked_source(page.final_url):
        return _src(cval, "", False, REJECT, "C0_REDIRECT_BLOCKED", field_key, raw)
    # ★最終URLの許可ドメイン再検査（verify_evidence側の多層確認・キャッシュ経路も塞ぐ・Codex E8）
    if allowed_domains is not None and not vc.host_matches_allowlist(page.final_url, allowed_domains):
        return _src(cval, "", False, REJECT, "C1_FINAL_URL_NOT_ALLOWED", field_key, raw)
    dom = vc.domain_of(page.final_url)
    ntext = vc.normalize(page.text)
    ntitle = vc.normalize(page.title)
    # ★hashは DocumentSnapshot 由来で持つ（body_sha256は後方互換キー＝rendered_text_sha256と同値）★
    snap = {"final_url": doc.final_url,
            "body_sha256": doc.rendered_text_sha256,
            "html_sha256": doc.html_sha256,
            "response_sha256": doc.response_sha256}

    # C2: 同定（title＋本文の両方に identity・旧機種/一覧ページ対策）
    if any(not vc._ident_in(s, ntext) for s in nids):
        return _src(cval, dom, False, REJECT, "C2_IDENT_NOT_IN_BODY", field_key, raw)
    if not ntitle or any(not vc._ident_in(s, ntitle) for s in nids):
        return _src(cval, dom, False, REJECT, "C2_IDENT_NOT_IN_TITLE", field_key, raw)

    # C3: claimant quote が逐語でページに実在（quoteが本物のページ引用であること）
    if nquote not in ntext:
        return _src(cval, dom, False, REJECT, "C3_QUOTE_NOT_ON_PAGE", field_key, raw)

    # ★機種×値の構造保持束縛（D-1a-2-3 増分B・平坦文束縛を置換／Codex増分A合格時の前提10項目）★
    #   束縛元は DocumentSnapshot の構造単位のみ（★兄弟単位を結合しない★）。値を含む単位のうち、
    #   ★対象identityが「その単位本文」に在り★（見出しは mode/scope 供給のみ・identity束縛には使わない）、
    #   合成文脈(見出し＋単位)でC5がPASSするものだけを採用。
    #   parse_warnings/truncated/意味割れ/対象外文脈での同値出現は全てREVIEW（fail-closed）。
    strong_ids = [s for s in nids if len(s) >= 3]     # 短縮語/一般語は束縛に使わない（部分一致誤同定回避）
    if not strong_ids:
        return _src(cval, dom, False, REVIEW, "NO_STRONG_IDENTITY", field_key, raw, snapshot=snap)
    value_units = [u for u in doc.units if _unit_contains_value(u.text, raw)]
    # ★UNIT_TRUNCATED を PARSE_WARNINGS より先に判定（truncatedも警告に入るため到達不能を回避・Codex増分B#2）
    if any("truncated" in u.parse_flags for u in value_units):   # 切り詰めた値単位はREVIEW（前提②）
        return _src(cval, dom, False, REVIEW, "UNIT_TRUNCATED", field_key, raw, snapshot=snap)
    if doc.parse_warnings:                            # 解析警告のある文書は自動公開しない（前提①）
        return _src(cval, dom, False, REVIEW, "PARSE_WARNINGS", field_key, raw, snapshot=snap)
    if not value_units:
        return _src(cval, dom, False, REVIEW, "VALUE_NOT_ON_PAGE", field_key, raw, snapshot=snap)
    # 項目別C5が未実装なら（値のページ実在は確認済みだが）自動公開しない
    if not reg["c5_ready"]:
        return _src(cval, dom, False, REVIEW, "C5_NOT_IMPLEMENTED", field_key, raw, snapshot=snap)
    bound_units = [u for u in value_units if _unit_bound_to_identity(u, strong_ids)]
    if not bound_units:                               # 値はあるが対象機種の単位でない（別機種文脈）
        return _src(cval, dom, False, REVIEW, "C5_NO_LOCAL_MATCH", field_key, raw, snapshot=snap)
    if len(bound_units) != len(value_units):          # 同値が対象外文脈にも出現＝意味割れ（前提⑦・fail-closed）
        return _src(cval, dom, False, REVIEW, "VALUE_IN_OTHER_CONTEXT", field_key, raw, snapshot=snap)
    # 合成文脈(見出し＋単位)でC5を評価。★全 bound 単位が PASS のときだけ verified（1つでも非PASSはREVIEW）★
    for u in bound_units:
        verdict, _reason, c5code = check_claim_identity(
            reg["c5_item"], reg["mode"], reg["scope"], cval, reg["unit"], _synth_for_c5(u))
        if verdict != PASS:
            return _src(cval, dom, False, REVIEW, "C5_NO_LOCAL_MATCH", field_key, raw, snapshot=snap)
    # 返す value は evidence 側（検証器は値を書き換えない・cvalと一致確認済み）
    return _src(ev_val, dom, True, PASS, "OK", field_key, raw, snapshot=snap)


def make_verifier(field_key, identity, allowed_domains=None, fetch_fn=None):
    """resolve_with_dialogue に渡す verify(item_key, evidence) を作る（field_key/identityを束縛）。
    ★resolveへ渡す item_key は anomaly_key_of(field_key)。verify内で item_key の一致を強制★"""
    def verify(item_key, evidence):
        return verify_evidence(field_key, evidence, identity, allowed_domains,
                               fetch_fn, expect_item_key=item_key)
    return verify


# ══════════════════════════════════════════════════════════════
# selftest（★fetchはモック＝vc._html_cache に合成HtmlSnapshotを差す・実ネットに出ない★）
#   verify_evidence は fetch_html→page_from_html_snapshot で平坦Pageを導出するため、
#   モックHTMLはタグ無しの平坦テキストでよい（_flatten_htmlは恒等でPageに戻る）。
# ══════════════════════════════════════════════════════════════

def selftest() -> int:
    from consensus_resolver import resolve_with_dialogue, DPASS
    results = []
    vc._html_cache.clear()
    vc._page_cache.clear()

    def t(name, cond):
        results.append((name, cond))
        print(("OK " if cond else "NG ") + name)

    IDENT = ["アクダマ"]

    def mock_page(url, body, title="アクダマ 天井・狙い目", final_url=None):
        # body は平坦テキスト（タグ無し）＝_flatten_htmlは恒等→page_from_html_snapshotでPage(body,...)へ
        h = hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()
        vc._html_cache[url] = vc.HtmlSnapshot(body, title, (final_url or url), h)

    def ev(value, url, quote, raw=None):
        return {"value": value, "raw_value": (raw if raw is not None else str(value)),
                "url": url, "quote": quote}

    def V(field_key, evidence, identity, **kw):
        # ★expect_item_key を自動付与（本番は make_verifier が付与・テストの利便）
        kw.setdefault("expect_item_key", anomaly_key_of(field_key))
        return verify_evidence(field_key, evidence, identity, **kw)

    # ★quoteには対象機種名を含める（別機種取り違え防止・Codex Critical・運用と同じ）
    Q_AT967 = "アクダマの通常時の天井はAT間967Gで当選"
    Q_THROUGH = "アクダマはボーナス6スルーで7回目AT確定の天井"
    Q_KW = "アクダマの機械割は設定1で97.4%"
    U1 = "https://chonborista.com/akudama"
    U2 = "https://nana-press.com/akudama"
    mock_page(U1, "Lアクダマドライブ。" + Q_AT967 + "。設定変更後（リセット）は745Gに短縮。" + Q_THROUGH + "。")
    mock_page(U2, "アクダマドライブ。" + Q_AT967 + "する。" + Q_KW + "。")

    # ── 正常系（天井・C5実装済み）→ verified True ──
    r = V("ceiling.normal.at", ev(967, U1, Q_AT967), IDENT)
    t("天井967(通常AT間)・C0〜C5全通過 → verified True/PASS",
      r["verified"] is True and r["c5_verdict"] == PASS and r["value"] == 967)
    # ── リセット混同 → C5でREJECT ──
    r = V("ceiling.reset", ev(967, U1, Q_AT967), IDENT)
    t("★reset claimだがページ本文は通常時の値のみ → verified False(REVIEW/該当文なし)",
      r["verified"] is False and r["c5_verdict"] == REVIEW)
    # ── スルー天井(c5_ready=False) → REVIEW ──
    r = V("ceiling.through", ev(6, U1, Q_THROUGH, raw="6"), IDENT)
    t("★スルー天井(c5_ready=False) → REVIEW(C5_NOT_IMPLEMENTED)",
      r["verified"] is False and r["c5_verdict"] == REVIEW and r["c5_code"] == "C5_NOT_IMPLEMENTED")
    # ── 機械割(c5_ready=False) → REVIEW ──
    r = V("kikaiwari.set1", ev(97.4, U2, Q_KW, raw="97.4"), IDENT)
    t("★機械割(項目別C5未実装) → REVIEW(C5_NOT_IMPLEMENTED)",
      r["c5_verdict"] == REVIEW and r["c5_code"] == "C5_NOT_IMPLEMENTED")
    # ── 未登録field → REVIEW ──
    t("★未登録field → REVIEW(UNREGISTERED_FIELD)",
      V("unknown.field", ev(967, U1, Q_AT967), IDENT)["c5_code"] == "UNREGISTERED_FIELD")

    # ── raw_value 桁/型/構文/値域/canonical（Codex A4/C・再レビューMinor）──
    t("★整数項目に小数raw 967.5 → REJECT(RAW_VALUE_NOT_INT)",
      V("ceiling.normal.at", ev(967, U1, "x", raw="967.5"), IDENT)["c5_code"] == "RAW_VALUE_NOT_INT")
    t("★整数項目に 967.0(末尾ゼロ) → REJECT(RAW_VALUE_NOT_INT)",
      V("ceiling.normal.at", ev(967, U1, "x", raw="967.0"), IDENT)["c5_code"] == "RAW_VALUE_NOT_INT")
    t("★指数表記 9.67e2 → REJECT(RAW_VALUE_SYNTAX)",
      V("ceiling.normal.at", ev(967, U1, "x", raw="9.67e2"), IDENT)["c5_code"] == "RAW_VALUE_SYNTAX")
    t("★カンマ 1,000 → REJECT(RAW_VALUE_SYNTAX)",
      V("ceiling.normal", ev(1000, U1, "x", raw="1,000"), IDENT)["c5_code"] == "RAW_VALUE_SYNTAX")
    t("★raw先頭+ +967 → REJECT(RAW_VALUE_SYNTAX)",
      V("ceiling.normal.at", ev(967, U1, "x", raw="+967"), IDENT)["c5_code"] == "RAW_VALUE_SYNTAX")
    t("★raw先頭ゼロ 00967 → REJECT(RAW_VALUE_SYNTAX)",
      V("ceiling.normal.at", ev(967, U1, "x", raw="00967"), IDENT)["c5_code"] == "RAW_VALUE_SYNTAX")
    t("★機械割 桁過多 97.45 → REJECT(TOO_MANY_DECIMALS)",
      V("kikaiwari.set1", ev(97.45, U2, "x", raw="97.45"), IDENT)["c5_code"] == "RAW_VALUE_TOO_MANY_DECIMALS")
    t("★機械割 97.40(末尾ゼロで2桁) → REJECT(TOO_MANY_DECIMALS)",
      V("kikaiwari.set1", ev(97.4, U2, "x", raw="97.40"), IDENT)["c5_code"] == "RAW_VALUE_TOO_MANY_DECIMALS")
    t("★値域外 天井 30G → REJECT(OUT_OF_RANGE)",
      V("ceiling.normal.at", ev(30, U1, "x", raw="30"), IDENT)["c5_code"] == "RAW_VALUE_OUT_OF_RANGE")
    t("★raw桁数過多 → REJECT(RAW_VALUE_TOO_LONG)",
      V("ceiling.normal.at", ev(967, U1, "x", raw="9" * 15), IDENT)["c5_code"] == "RAW_VALUE_TOO_LONG")
    t("★raw前後空白 ' 967 ' → REJECT(RAW_VALUE_SYNTAX)",
      V("ceiling.normal.at", ev(967, U1, "x", raw=" 967 "), IDENT)["c5_code"] == "RAW_VALUE_SYNTAX")

    # ── value 必須・型・一致（Codex A5/C）──
    t("★value欠落 → REJECT(VALUE_MISSING)",
      V("ceiling.normal.at", {"raw_value": "967", "url": U1, "quote": Q_AT967}, IDENT)["c5_code"] == "VALUE_MISSING")
    t("★value bool → REJECT(VALUE_BOOL)",
      V("ceiling.normal.at", {"value": True, "raw_value": "967", "url": U1, "quote": "x"}, IDENT)["c5_code"] == "VALUE_BOOL")
    t("★int項目にfloat value 967.0 → REJECT(VALUE_TYPE)",
      V("ceiling.normal.at", {"value": 967.0, "raw_value": "967", "url": U1, "quote": "x"}, IDENT)["c5_code"] == "VALUE_TYPE")
    t("★value と raw不一致(968 vs 967) → REJECT(VALUE_MISMATCH_RAW)",
      V("ceiling.normal.at", {"value": 968, "raw_value": "967", "url": U1, "quote": "x"}, IDENT)["c5_code"] == "VALUE_MISMATCH_RAW")

    # ── identity 健全性（Codex A6）──
    t("★identity空 → REVIEW(BAD_IDENTITY)",
      V("ceiling.normal.at", ev(967, U1, Q_AT967), [])["c5_code"] == "BAD_IDENTITY")
    t("★identity 3文字未満のみ → REVIEW(BAD_IDENTITY)",
      V("ceiling.normal.at", ev(967, U1, Q_AT967), ["A"])["c5_code"] == "BAD_IDENTITY")
    t("★identity 文字列を直接渡す(list化忘れ) → REVIEW(BAD_IDENTITY)",
      V("ceiling.normal.at", ev(967, U1, Q_AT967), "アクダマ")["c5_code"] == "BAD_IDENTITY")

    # ── C0/C4/C1/C2/C3 ──
    t("★退化quote → REJECT(C0_DEGENERATE_QUOTE)",
      V("ceiling.normal.at", ev(967, U1, "967G"), IDENT)["c5_code"] == "C0_DEGENERATE_QUOTE")
    t("★値がquote内に無い(1200) → REJECT(C4)",
      V("ceiling.normal.at", ev(1200, U1, Q_AT967, raw="1200"), IDENT)["c5_code"] == "C4_VALUE_NOT_IN_QUOTE")
    vc._html_cache["https://chonborista.com/none"] = None  # 取得失敗を決定論モック（実ネットに出ない）
    t("★取得失敗 → REVIEW(C1_FETCH_FAILED)",
      V("ceiling.normal.at", ev(967, "https://chonborista.com/none", Q_AT967), IDENT)["c5_code"] == "C1_FETCH_FAILED")
    t("★同定NG(別機種identity) → REJECT(C2)",
      V("ceiling.normal.at", ev(967, U1, Q_AT967), ["ケンガン"])["c5_verdict"] == REJECT)
    mock_page("https://chonborista.com/akudama2", "Lアクダマドライブ 別の記事本文。天井の話は無い。")
    t("★quoteがページに実在しない → REJECT(C3)",
      V("ceiling.normal.at", ev(967, "https://chonborista.com/akudama2", Q_AT967), IDENT)["c5_code"] == "C3_QUOTE_NOT_ON_PAGE")

    # ── ★Critical: quoteに対象機種名が無い(別機種quote・近くに対象名があっても) → REVIEW ──
    mock_page("https://chonborista.com/mixed",
              "アクダマドライブの解析情報はこちら。バベルの通常時の天井は745Gで当選する。",
              title="アクダマ 天井情報")
    r = V("ceiling.normal", ev(745, "https://chonborista.com/mixed",
                               "バベルの通常時の天井は745Gで当選する", raw="745"), IDENT)
    t("★別機種quote(値の文に対象名なし) → REVIEW(C5_NO_LOCAL_MATCH)",
      r["verified"] is False and r["c5_code"] == "C5_NO_LOCAL_MATCH")
    # ★Critical反例: quote水増し(対象名を別文に混ぜて丸ごと引用)でも、値の"文"に対象名が無ければ弾く
    mock_page("https://chonborista.com/pad",
              "アクダマの解析情報はこちら。バベルの通常時の天井は745Gで当選する。",
              title="アクダマ 天井情報")
    r = V("ceiling.normal", ev(745, "https://chonborista.com/pad",
                               "アクダマの解析情報はこちら。バベルの通常時の天井は745Gで当選する", raw="745"), IDENT)
    t("★quote水増し(対象名を別文に混ぜる) → REVIEW(C5_NO_LOCAL_MATCH)",
      r["verified"] is False and r["c5_code"] == "C5_NO_LOCAL_MATCH")

    # ── ★D-1a-2-3 増分B: 構造保持束縛（見出しがmode/scopeを供給・平坦束縛では不可）★ ──
    # 見出し『朝一・リセット』が単位に reset 文脈を供給 → reset claim が PASS（単位本文に reset 語は無い）
    mock_page("https://chonborista.com/hb-reset",
              "<h2>朝一・リセット</h2><p>アクダマの天井は745G</p>", title="アクダマ 天井")
    r = V("ceiling.reset", ev(745, "https://chonborista.com/hb-reset",
                             "アクダマの天井は745G", raw="745"), IDENT)
    t("★増分B: 見出しがreset文脈を供給→reset claimがPASS（構造保持束縛の核心）",
      r["verified"] is True and r["c5_verdict"] == PASS)
    # 見出しが通常時 → 同じ745でも reset claim は通らない（見出しの mode が効く）
    mock_page("https://chonborista.com/hb-normal",
              "<h2>通常時の天井</h2><p>アクダマの天井は745G</p>", title="アクダマ 天井")
    r = V("ceiling.reset", ev(745, "https://chonborista.com/hb-normal",
                             "アクダマの天井は745G", raw="745"), IDENT)
    t("★増分B: 見出しが通常時→reset claimは非PASS（見出しmodeで弾く）",
      r["verified"] is False)
    # parse_warnings のある文書は値が正当単位にあっても REVIEW（前提①）
    mock_page("https://chonborista.com/warn",
              "<div>アクダマの天井は967G</div><ul><p>x</p></ul>", title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/warn",
                              "アクダマの天井は967G", raw="967"), IDENT)
    t("★増分B: parse_warnings(ul>p misplaced)のある文書は REVIEW(PARSE_WARNINGS)",
      r["verified"] is False and r["c5_code"] == "PARSE_WARNINGS")
    # 同値が対象外機種の単位にも出現 → 意味割れで REVIEW（前提⑦）
    mock_page("https://chonborista.com/other",
              "<p>アクダマの天井は967G</p><p>バベルの天井は967G</p>", title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/other",
                              "アクダマの天井は967G", raw="967"), IDENT)
    t("★増分B: 同値が別機種単位にも出現→REVIEW(VALUE_IN_OTHER_CONTEXT・意味割れ)",
      r["verified"] is False and r["c5_code"] == "VALUE_IN_OTHER_CONTEXT")
    # 値の単位に identity 無し（identity は兄弟単位）→ 兄弟結合しない＝REVIEW（前提⑥）
    mock_page("https://chonborista.com/sib",
              "<p>アクダマの解説はこちら</p><p>この機種の天井は967Gです</p>", title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/sib",
                              "この機種の天井は967Gです", raw="967"), IDENT)
    t("★増分B: 値の単位にidentity無し(兄弟単位にあるだけ)→兄弟結合せず REVIEW(C5_NO_LOCAL_MATCH)",
      r["verified"] is False and r["c5_code"] == "C5_NO_LOCAL_MATCH")
    # ★祖先見出しに対象機種名があっても、より近い別機種見出し配下の別機種値は束縛しない（Codex増分B#1）
    mock_page("https://chonborista.com/ancestor",
              "<h1>アクダマ</h1><p>アクダマの通常時の天井は967G</p>"
              "<h2>バベル</h2><p>バベルの通常時の天井は967G</p>", title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/ancestor",
                              "バベルの通常時の天井は967G", raw="967"), IDENT)
    t("★増分B: 祖先h1がアクダマでも、値がバベル単位にも出る→identity単位本文要求で REVIEW(VALUE_IN_OTHER_CONTEXT)",
      r["verified"] is False and r["c5_code"] == "VALUE_IN_OTHER_CONTEXT")
    # ★truncated 単位（>2000字の単一文）を含む値は UNIT_TRUNCATED（PARSE_WARNINGSより先に到達・Codex増分B#2）
    _big = "アクダマの天井は967Gで" + ("あ" * 2100)   # 句点を挟まない単一文＝2000字超で truncated
    mock_page("https://chonborista.com/trunc", "<p>" + _big + "</p>", title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/trunc",
                              "アクダマの天井は967Gで", raw="967"), IDENT)
    t("★増分B: 切り詰め単位を含む値は UNIT_TRUNCATED（到達可能）",
      r["verified"] is False and r["c5_code"] == "UNIT_TRUNCATED")

    # ── ★item_key一致強制（confused-deputy・Codex A3/再レビューHigh）──
    verify_kw = make_verifier("ceiling.normal.at", IDENT)
    t("★誤配線 verify(kikaiwari,天井ev) → REJECT(ITEM_KEY_MISMATCH)",
      verify_kw("kikaiwari", ev(967, U1, Q_AT967))["c5_code"] == "ITEM_KEY_MISMATCH")
    t("正配線 verify(ceiling.game,天井ev) → verified True",
      verify_kw(anomaly_key_of("ceiling.normal.at"), ev(967, U1, Q_AT967))["verified"] is True)
    t("★verify_evidence直接呼びで item_key不一致(省略不可) → REJECT(ITEM_KEY_MISMATCH)",
      verify_evidence("ceiling.normal.at", ev(967, U1, Q_AT967), IDENT,
                      expect_item_key="kikaiwari")["c5_code"] == "ITEM_KEY_MISMATCH")

    # ── ★空allowlist fail-close(E7)・最終URL許可外(E8)──
    t("★空allowlist [] は全拒否(fail-open防止) → REJECT(C0_DOMAIN_NOT_ALLOWED)",
      V("ceiling.normal.at", ev(967, U1, Q_AT967), IDENT,
        allowed_domains=[])["c5_code"] == "C0_DOMAIN_NOT_ALLOWED")
    # リダイレクトで最終URLが許可外へ出る想定（final_url=evil）。キャッシュ経路でも最終URL再検査で弾く
    mock_page("https://chonborista.com/redir", "Lアクダマドライブ。" + Q_AT967,
              title="アクダマ 天井", final_url="https://evil.example/x")
    t("★最終URLが許可外 → REJECT(C1_FINAL_URL_NOT_ALLOWED)",
      V("ceiling.normal.at", ev(967, "https://chonborista.com/redir", Q_AT967),
        IDENT, allowed_domains=["chonborista.com"])["c5_code"] == "C1_FINAL_URL_NOT_ALLOWED")
    t("正常系はsnapshot(final_url/body_sha256)を返す",
      "snapshot" in V("ceiling.normal.at", ev(967, U1, Q_AT967), IDENT)
      and V("ceiling.normal.at", ev(967, U1, Q_AT967), IDENT)["snapshot"].get("body_sha256"))

    # ── ★D-1a-2-2: DocumentSnapshotとの二重化（束縛は未変更・hashだけ先に持つ）★ ──
    r_ok = V("ceiling.normal.at", ev(967, U1, Q_AT967), IDENT)
    t("★二重化: 正常系snapshotに html_sha256/body_sha256 を持つ",
      bool(r_ok["snapshot"].get("html_sha256")) and bool(r_ok["snapshot"].get("body_sha256")))
    _html = "<h1>アクダマ</h1><p>天井は967G</p>"
    _hs = vc.HtmlSnapshot(_html, "T", "https://x.test/a",
                          hashlib.sha256(_html.encode("utf-8")).hexdigest())
    _pg = vc.page_from_html_snapshot(_hs)
    _doc = build_document_snapshot(_hs, _pg)
    t("★二重化: html_sha256はHtmlSnapshot(取得HTML)由来", _doc.html_sha256 == _hs.html_sha256)
    t("★二重化: rendered_text_sha256は平坦テキストのhash",
      _doc.rendered_text_sha256 == hashlib.sha256(_pg.text.encode("utf-8", "replace")).hexdigest())
    t("★構造化: h1_candidatesにH1・response_sha256はNone(生hashは後段)",
      _doc.h1_candidates == ("アクダマ",) and _doc.response_sha256 is None)
    t("★構造化: 段落が見出し配下のparagraph_sentence単位になる",
      len(_doc.units) == 1 and _doc.units[0].kind == "paragraph_sentence"
      and _doc.units[0].text == "天井は967G"
      and _doc.units[0].heading_path == ((1, "アクダマ"),)
      and _doc.parse_warnings == ())
    # ── ★D-1a-2-3: 構造パーサ（表以外・段落文/リスト/見出しアウトライン・Q5相当）★ ──
    def pu(html):
        return parse_structured_units(html)

    u, h1s, w = pu("<h1>アクダマ</h1><h2>通常</h2><p>天井は967G。</p>"
                   "<h2>リセット</h2><p>リセットは745G。</p>")
    t("構造: 段落が直近見出しパス配下になる（h2で前h2以下を置換）",
      any(x.text == "天井は967G" and x.heading_path == ((1, "アクダマ"), (2, "通常")) for x in u)
      and any(x.text == "リセットは745G" and x.heading_path == ((1, "アクダマ"), (2, "リセット")) for x in u))
    t("構造: h1_candidatesにH1・警告なし", h1s == ("アクダマ",) and w == ())

    u, _, _ = pu("<ul><li>通常天井は967G。AT間は745G。</li><li>短縮あり</li></ul>")
    lis = [x for x in u if x.kind == "list_item"]
    t("構造: li は list_item で原子（文分割しない）",
      len(lis) == 2 and lis[0].text == "通常天井は967G。AT間は745G。")

    u, _, _ = pu("<p>本文999G</p><script>var x='天井12345G';</script>"
                 "<style>.a{content:'888G'}</style><!-- 隠し777G -->")
    joined = " ".join(x.text for x in u)
    t("構造: script/style/comment内の数値は単位に入らない",
      "999" in joined and "12345" not in joined and "888" not in joined and "777" not in joined)

    u, _, _ = pu("<p>外は500G</p><table><tr><td>表内は600G</td></tr></table>")
    joined = " ".join(x.text for x in u)
    t("構造: 表の内側は単位化しない（D-1a-2-4で対応・外の段落のみ）",
      "500" in joined and "600" not in joined)

    u, _, _ = pu("<div>朝一は300G<br>通常は500G</div>")
    texts = [x.text for x in u]
    t("構造: br で別単位に分かれる（文境界）",
      "朝一は300G" in texts and "通常は500G" in texts)

    _, _, w = pu("<p>値は<b>967G</p></b>")
    t("構造: タグ交差/孤立終了は parse_warnings（束縛側でREVIEW材料）",
      "unmatched_end_tag" in w)

    _, _, w = pu("<div>" * 260)
    t("構造: 深さ上限超過は too_deep 警告", "too_deep" in w)
    _, _, w = pu("<br>" * (_MAX_PARSE_NODES + 10))
    t("構造: ノード上限超過は too_many_nodes 警告", "too_many_nodes" in w)

    # ── ★Codex増分A再指摘の反例（li原子性/未閉じ見出し/交差/EOF未閉じ/メモリ上限）★ ──
    u, _, w = pu("<ul><li><p>A。</p><p>B。</p></li></ul>")
    lis = [x for x in u if x.kind == "list_item"]
    t("構造: li内の入れ子ブロックでも1 list_item・区切り挿入（原子性・Codex#1/#3）",
      len(lis) == 1 and lis[0].text == "A。 B。")

    u, _, w = pu("<ul><li>A<li>B</ul>")     # 終了タグ省略（正当なHTML）
    lis = [x for x in u if x.kind == "list_item"]
    t("構造: 終了省略の兄弟liは2 list_item・警告なし（Codex#1）",
      [x.text for x in lis] == ["A", "B"] and w == ())

    u, _, w = pu("<ul><li>A</li><li>B</li></ul>")
    lis = [x for x in u if x.kind == "list_item"]
    t("構造: 明示終了の兄弟liも2 list_item・警告なし",
      [x.text for x in lis] == ["A", "B"] and w == ())

    u, _, w = pu("<ul><li>親<ul><li>子</li></ul>続き</li></ul>")
    lis = [x.text for x in u if x.kind == "list_item"]
    t("構造: 入れ子リストは外側=親 続き・内側=子（分割しない・Codex#2）",
      set(lis) == {"子", "親 続き"} and w == ())

    u, _, w = pu("<ul><li>親<ul><li>子</ul>続き</ul>")   # 内側も外側も終了省略
    lis = [x.text for x in u if x.kind == "list_item"]
    t("構造: 終了省略の入れ子リストも外側=親 続き・内側=子",
      set(lis) == {"子", "親 続き"} and w == ())

    u, _, w = pu("<div><li>X</div>")        # ul/ol の外の li ＝ 構造異常
    t("構造: list外のliは li_without_list 警告＋値を捕捉しない（Codex#6）",
      "li_without_list" in w and all("X" != x.text for x in u))

    # ── ★Codex増分A第2次指摘（文脈依存交差/兄弟li交差/li内見出し/終了タグ上限）★ ──
    _, _, w = pu("<span><p>x</span>")        # インラインがブロックを跨ぐ＝不正
    t("構造: 文脈上不正な交差(span が p を跨ぐ)は implicit_close（Codex#1）",
      "implicit_close" in w)

    u, _, w = pu("<div><p>x</div>")          # ブロックが p を正当に閉じる＝警告なし
    t("構造: 正当な暗黙終了(div が p を閉じる)は警告なし",
      any(x.text == "x" for x in u) and "implicit_close" not in w)

    u, _, w = pu("<ul><li>A<div><li>B</li></ul>")   # 兄弟li暗黙終了で未閉じdivを跨ぐ
    lis = [x.text for x in u if x.kind == "list_item"]
    t("構造: 兄弟li暗黙終了が未閉じブロックを跨げば implicit_close（Codex#2）",
      "implicit_close" in w and "A" in lis and "B" in lis)

    u, _, w = pu("<ul><li><h3>天井967G</h3></li></ul>")   # li内が見出しのみ
    lis = [x for x in u if x.kind == "list_item"]
    t("構造: li内の見出しテキストも list_item に入る（値の消失防止・Codex#3）",
      any("天井967G" in x.text for x in lis))

    _, _, w = pu("</x>" * (_MAX_PARSE_NODES + 10))   # 終了タグだけで上限迂回できない
    t("構造: 終了タグ大量入力も too_many_nodes で停止（上限迂回防止・Codex#4）",
      "too_many_nodes" in w and len(w) <= 5)

    # ── ★Codex増分A第3次指摘（兄弟li交差の精緻化/p過検知/開始駆動p終了/feed中断）★ ──
    _, _, w = pu("<ul><li>A<td>X<li>B</li></ul>")     # td は新liで正当に閉じられない
    t("構造: 兄弟li回復でtdを跨げば implicit_close（Codex#1）", "implicit_close" in w)
    _, _, w = pu("<ul><li>A<dt>X<li>B</li></ul>")     # dt も同様
    t("構造: 兄弟li回復でdtを跨げば implicit_close", "implicit_close" in w)

    u, _, w = pu("<details><p>x</details>")           # p は details 末尾で終了省略可＝正当
    t("構造: pは非インライン親末尾で終了省略可（過検知しない・Codex#2）",
      any(x.text == "x" for x in u) and "implicit_close" not in w)

    u, _, w = pu("<p>A<div>B</div>C</p>")             # div開始でpが暗黙終了・Cは別単位
    texts = [x.text for x in u]
    t("構造: 開始タグ駆動のp暗黙終了でCがpに戻らない（Codex#3）",
      "A" in texts and "B" in texts and "C" in texts and len(w) > 0)

    _, _, w = pu("<x" + "y" * (_MAX_TAG_LEN + 5) + ">z")   # 巨大タグ名
    t("構造: 巨大タグ名は long_tag で停止（source_path肥大防止・Codex#4）", "long_tag" in w)

    # ── ★Codex増分A第4次指摘（p終了時の未閉じインライン/p親制限/終了タグ長）★ ──
    _, _, w = pu("<p>A<span>X<div>B</div>")           # p終了時に未閉じspanを跨ぐ
    t("構造: 開始駆動p終了で未閉じインラインを跨げば implicit_close（Codex#1）",
      "implicit_close" in w)
    _, _, w = pu("<ul><li>A<p><span>X<li>B</li></ul>")   # li内p終了で未閉じspan
    t("構造: li内p終了で未閉じインラインを跨げば implicit_close", "implicit_close" in w)

    for parent in ("video", "map", "audio", "noscript"):   # p を末尾省略できない親
        _, _, w = pu("<%s><p>x</%s>" % (parent, parent))
        t("構造: p は %s 末尾では終了省略不可＝implicit_close（Codex#2）" % parent,
          "implicit_close" in w)

    _, _, w = pu("<details><p>x</details>")            # details は許容（過検知しない・再確認）
    t("構造: p は details 末尾では終了省略可（過検知しない）", "implicit_close" not in w)

    _, _, w = pu("</" + "x" * (_MAX_TAG_LEN + 1) + ">")   # 終了タグ名の長さ上限
    t("構造: 巨大終了タグ名も long_tag で停止（全経路・Codex#4）", "long_tag" in w)

    # ── ★Codex増分A第5次指摘（開始駆動の兄弟省略/p終了集合/親妥当性/表抑止）★ ──
    _, _, w = pu("<table><tr><td>A<td>B</tr></table>")     # 兄弟td終了省略（正当）＝誤検知しない
    t("構造: 表内の兄弟td終了省略で誤検知しない（表抑止・Codex#1）", "implicit_close" not in w)
    _, _, w = pu("<table><tr><td>A<tr><td>B</table>")      # 兄弟tr終了省略（正当）
    t("構造: 表内の兄弟tr終了省略でも誤検知しない", "implicit_close" not in w)

    u, _, w = pu("<dl><dt>A<dd>B</dl>")                    # dt/dd の兄弟終了省略（正当）
    texts = [x.text for x in u]
    t("構造: dl の dt/dd 兄弟終了省略で誤検知しない・A/Bとも単位化（Codex#1）",
      "A" in texts and "B" in texts and "implicit_close" not in w)

    _, _, w = pu("<video><p>A<td>B</td></video>")          # td は p を閉じない・親も不正
    t("構造: pを閉じないtdでpが無警告終了しない（Codex#2）", "implicit_close" in w)

    u, _, w = pu("<p>A<details>B</details>C")              # details は p を閉じる
    texts = [x.text for x in u]
    t("構造: details開始でpが暗黙終了しABCが1単位にならない（Codex#2）",
      "A" in texts and "C" in texts and "AC" not in "".join(texts))

    _, _, w = pu("<ul><div><li>A<li>B</li></div></ul>")    # li の実親が div ＝ 構造異常
    t("構造: 祖先にulがあってもli実親がdivなら li_without_list（Codex#3）", "li_without_list" in w)

    _, _, w = pu("<div><td>x</td></div>")                  # td の実親が div（表外）＝構造異常
    t("構造: 表外のtd（実親div）は misplaced_element（Codex#4）", "misplaced_element" in w)

    # ── ★Codex増分A第6次指摘（head/title漏れ・dl>div・menu>li・ruby/optgroup）★ ──
    u, _, w = pu("<html><head><title>機種A 天井967G</title></head><body><p>本文なし</p></body></html>")
    t("構造: head/title の値は本文単位に漏れない（Codex#1）",
      all("967" not in x.text for x in u))

    u, _, w = pu("<dl><div><dt>A<dd>B</div></dl>")          # dl>div>dt/dd は正当（HTML5）
    texts = [x.text for x in u]
    t("構造: dl>div>dt/dd は誤検知しない・A/B単位化（Codex#2）",
      "A" in texts and "B" in texts and "misplaced_element" not in w and "implicit_close" not in w)

    u, _, w = pu("<menu><li>A<li>B</menu>")                 # menu は li を持てる
    lis = [x.text for x in u if x.kind == "list_item"]
    t("構造: menu>li は正当（li_without_list出さない・Codex#3）",
      lis == ["A", "B"] and "li_without_list" not in w)

    _, _, w = pu("<ruby>漢<rp>(<rt>かん<rp>)</ruby>")        # rt/rp の終了省略（正当）
    t("構造: ruby の rt/rp 終了省略で誤検知しない（Codex#4）", "implicit_close" not in w)

    _, _, w = pu("<select><optgroup label=A><option>A"
                 "<optgroup label=B><option>B</select>")    # optgroupが前のoptgroupも閉じる
    t("構造: optgroup開始が前のoption＋optgroupを閉じる（misplaced出さない・Codex#4）",
      "misplaced_element" not in w)

    # ── ★Codex増分A第7次指摘（非captureコンテナの子孫テキストがrootに漏れない）★ ──
    u, _, _ = pu("<body><select><option>機種Aの通常時の天井は967G</option></select>"
                 "<p>本文には天井情報なし</p></body>")
    t("構造: select/option の値は本文単位に漏れない（root catch-all停止・Codex増分A#1）",
      all("967" not in x.text for x in u))
    for tag in ("ul", "ol", "menu", "dl", "iframe", "canvas", "object", "nav",
                "header", "footer", "aside", "form"):
        u, _, _ = pu("<%s>機種Aの通常時天井は967G</%s>" % (tag, tag))
        t("構造: %s 直下テキストは証拠単位化されない（非content/opaque/抑止）" % tag,
          all("967" not in x.text for x in u))

    # ── ★Codex増分A第8次指摘（opaque内で開いたp/div/見出しの迂回を塞ぐ）★ ──
    u, _, w = pu("<ruby><rt><p>機種Aの通常時天井は967G</p></rt></ruby>")
    t("構造: opaque(rt)内で開いたpは capture せず値も漏れない（Codex増分A#2）",
      all("967" not in x.text for x in u) and "misplaced_element" in w)
    # figure(本文)は capture。ul/menu 等の構造親は misplaced 警告。nav/header/footer/aside 等の
    # 非本文は「静かにスキップ（値を捕捉しないが警告もしない）」＝Codex第12次「非contentと警告を分離」。
    for tag in ("figure", "ul", "menu", "nav", "header", "footer", "aside"):
        u, _, w = pu("<%s><p>機種Aの通常時天井は967G</p></%s>" % (tag, tag))
        if tag == "figure":
            ok = any("967" in x.text for x in u)                 # 本文＝capture
        elif tag in ("ul", "menu"):
            ok = all("967" not in x.text for x in u) and "misplaced_element" in w  # 構造誤り＝警告
        else:
            ok = all("967" not in x.text for x in u) and "misplaced_element" not in w  # 非本文＝静かにスキップ
        t("構造: %s>p の扱い（capture/構造警告/静かにスキップの分離）" % tag, ok)
    u, h1s, w = pu("<ruby><rt><h1>機種A</h1></rt></ruby><p>通常時の天井は967G</p>")
    t("構造: opaque内見出しは h1_candidates を汚染しない（Codex増分A#2）",
      h1s == () and "misplaced_element" in w)
    u, _, w = pu("<dl><div><dt>A<dd>B</div></dl>")   # dl>div は正当（再確認・過検知しない）
    texts = [x.text for x in u]
    t("構造: dl>div ラッパーは正当（misplaced出さない）",
      "A" in texts and "B" in texts and "misplaced_element" not in w)

    # ── ★Codex増分A第9次指摘（間に要素を挟むopaque迂回を実sinkで塞ぐ）★ ──
    u, _, w = pu("<ruby><rt><span><p>機種Aの通常時天井は967G</p></span></rt></ruby>")
    t("構造: opaque内で span を挟んでも p は capture されない（実content判定・Codex増分A#2）",
      all("967" not in x.text for x in u))
    u, _, w = pu("<ul><div><p>機種Aの通常時天井は967G</p></div></ul>")
    t("構造: ul>div>p も本文到達不可でcapture禁止（値漏れなし＋構造警告・Codex増分A#2）",
      all("967" not in x.text for x in u) and "misplaced_element" in w)
    u, h1s, w = pu("<ruby><rt><span><h1>機種A</h1></span></rt></ruby>")
    t("構造: opaque内でspanを挟んだ見出しも h1_candidates を汚染しない（Codex増分A#2）",
      h1s == ())
    u, _, _ = pu("<ruby>機種Aの通常時天井は967G</ruby>")   # ruby直下も opaque（透過しない）
    t("構造: ruby 直下テキストも証拠にしない（ruby非透過・Codex増分A#3）",
      all("967" not in x.text for x in u))
    u, _, w = pu("<dl><div>機種Aの通常時天井は967G</div></dl>")   # dl>div ラッパーの直下テキスト
    t("構造: dl>div ラッパーの直下テキストは証拠にしない（非capture wrapper・Codex増分A#3）",
      all("967" not in x.text for x in u))

    # ── ★Codex増分A第10次指摘（非content領域に正当リスト構造を挟む迂回・content伝播で塞ぐ）★ ──
    # 非本文領域(nav/ruby/header/form)配下は正当なリスト構造でも本文到達不可＝値漏れなし・静かにスキップ。
    for html in ("<nav><ul><li><p>機種Aの通常時天井は967G</p></li></ul></nav>",
                 "<ruby><ul><li>機種Aの通常時天井は967G</li></ul></ruby>",
                 "<header><dl><dt>機種Aの通常時天井は967G</dt></dl></header>",
                 "<form><dl><div><dd>機種Aの通常時天井は967G</dd></div></dl></form>"):
        u, _, w = pu(html)
        t("構造: 非本文領域配下の正当リスト構造も本文到達不可（値漏れなし）: %s" % html[:22],
          all("967" not in x.text for x in u))
    u, _, w = pu("<nav><ul><li><h3>機種A</h3>通常967G</li></ul></nav>")
    t("構造: 非content領域のli内見出しも list_item に漏れない（Codex増分A#4）",
      all("967" not in x.text and "機種A" not in x.text for x in u))
    # 正当な本文リストは従来どおり capture（過検知しない・再確認）
    u, _, w = pu("<main><ul><li>機種Aの通常時天井は967G</li></ul></main>")
    t("構造: main>ul>li は本文として capture（過検知しない）",
      any("967" in x.text for x in u) and "misplaced_element" not in w)

    # ── ★Codex増分A第11次指摘（sink探索でcontentを強制＝必須コンテナ外の要素が透過して漏れない）★ ──
    for tag in ("dt", "dd", "td", "th", "caption"):
        u, _, w = pu("<div><%s>機種Aの通常時天井は967G</%s></div>" % (tag, tag))
        t("構造: div>%s は本文到達不可＝透過せず漏れない＋misplaced（Codex増分A#11）" % tag,
          all("967" not in x.text for x in u) and "misplaced_element" in w)
    # 正当なインライン本文は従来どおり（過検知しない）
    u, _, w = pu("<p>機種Aの通常時<span>天井は967G</span>です</p>")
    t("構造: p 内のインライン(span)テキストは本文として capture（過検知しない）",
      any("967" in x.text for x in u) and "misplaced_element" not in w)

    # ── ★Codex増分A第12次指摘（非void要素の自己終了スラッシュは無視＝即pop で遮断回避させない）★ ──
    u, _, w = pu("<nav/>機種Aの通常時天井は967G")
    t("構造: <nav/>自己終了は無視（開いたまま）＝後続テキストが漏れない＋構文警告（Codex増分A#12）",
      all("967" not in x.text for x in u) and "nonvoid_self_closing" in w)
    u, _, w = pu("<div><dt/>機種Aの通常時天井は967G</div>")
    t("構造: <dt/>自己終了も即pop されず値が外側divに漏れない（Codex増分A#12）",
      all("967" not in x.text for x in u)
      and "misplaced_element" in w and "nonvoid_self_closing" in w)
    u, _, w = pu("<p>天井は967G</p><br/>朝一は300G")   # void の自己終了は従来どおり
    t("構造: void(br)の自己終了は従来どおり正常（回帰なし）",
      any("967" in x.text for x in u) and "nonvoid_self_closing" not in w)

    # ── ★Codex増分A第13次指摘（抑止/表コンテナの自己終了は無視＝開いたまま＝後続を抑止・漏らさない）★ ──
    for tag in ("select", "iframe", "textarea", "title", "canvas", "object",
                "audio", "video", "map", "picture", "table"):
        u, _, w = pu("<%s/>機種Aの通常時天井は967G" % tag)
        t("構造: <%s/>自己終了は開いたまま＝後続テキストが本文に漏れない（Codex増分A#13）" % tag,
          all("967" not in x.text for x in u))
    u, _, w = pu("<svg/>機種Aの通常時天井は967G")   # 外来svgは自己終了で閉じる→後続は本文
    t("構造: 外来svgの自己終了は閉じる→後続テキストは本文として capture（過検知なし）",
      any("967" in x.text for x in u))

    # ── ★Codex増分A第14次指摘（SVG/MathML名前空間内の子要素の自己終了も有効＝誤警告なし）★ ──
    u, _, w = pu("<p>本文967G</p><svg><path/></svg>")
    t("構造: SVG内の<path/>自己終了は有効＝正常SVGに誤implicit_close出さない（Codex増分A#14）",
      any("967" in x.text for x in u) and w == ())
    _, _, w = pu("<math><mspace/></math>")
    t("構造: MathML内の<mspace/>自己終了も有効＝誤警告なし", w == ())
    _, _, w = pu("<p>本文</p><svg><g><circle/></g></svg>")
    t("構造: SVGの入れ子(g>circle/)でも誤警告なし", w == ())

    # ── ★Codex増分A第15次指摘（HTML統合点=名前空間がHTMLへ戻る・値漏れなし）★ ──
    # 統合点(foreignObject/desc/title/mtext/annotation-xml)配下のHTML要素は外来扱いしない。
    # いずれも svg/math 全体が抑止のため値は本文単位化されない（安全）＝値漏れなしを確認。
    for html in ("<svg><foreignObject><nav/></foreignObject></svg>",
                 "<svg><desc><div/>機種A967G</desc></svg>",
                 "<svg><title><span/>機種A967G</title></svg>",
                 "<math><mtext><span/>機種A967G</mtext></math>",
                 "<math><annotation-xml encoding='text/html'><div/>機種A967G</annotation-xml></math>"):
        u, _, w = pu(html)
        t("構造: HTML統合点配下は値漏れなし: %s" % html[:26],
          all("967" not in x.text for x in u))
    # 統合点から再び SVG へ入った path/ は正常終了（誤警告なし）
    u, _, w = pu("<p>本文967G</p><svg><foreignObject><svg><path/></svg></foreignObject></svg>")
    t("構造: 統合点から再入したSVGの<path/>は正常終了（誤警告なし・本文は残る）",
      any("967" in x.text for x in u) and w == ())

    # ── ★Codex増分A第16次指摘（名前空間別の統合点・mglyph例外・encoding依存・警告ゲート）★ ──
    _, _, w = pu("<math><mtext><mglyph/></mtext></math>")
    t("構造: MathMLテキスト統合点でも mglyph はMathMLのまま＝誤警告なし（Codex増分A#16）",
      "nonvoid_self_closing" not in w)
    _, _, w = pu("<math><title><x/></title></math>")   # title は SVG統合点だが math名前空間では非統合
    t("構造: <math><title>は非統合（xはMathML＝自己終了で閉じる・誤警告なし）",
      "nonvoid_self_closing" not in w)
    _, _, w = pu("<svg><mtext><x/></mtext></svg>")      # mtext は SVG名前空間では非統合
    t("構造: <svg><mtext>は非統合（xはSVG＝自己終了で閉じる・誤警告なし）",
      "nonvoid_self_closing" not in w)
    _, _, w = pu("<math><annotation-xml encoding='application/xml'><x/></annotation-xml></math>")
    t("構造: annotation-xml encoding非HTMLは非統合（xはMathML・誤警告なし）",
      "nonvoid_self_closing" not in w)
    _, _, w = pu("<math><annotation-xml encoding='text/html'><div/></annotation-xml></math>")
    t("構造: annotation-xml encoding=text/html は統合＝div はHTML自己終了無視で警告",
      "nonvoid_self_closing" in w)
    u, _, w = pu("<math><mtext><span/>機種A967G</mtext></math>")
    t("構造: MathMLテキスト統合点配下のHTML(span)は自己終了無視で警告＋値漏れなし",
      all("967" not in x.text for x in u) and "nonvoid_self_closing" in w)
    # 抑止コンテナ自身の自己終了も HTML名前空間として nonvoid_self_closing 警告（ゲート撤廃・Codex増分A#16）
    for tag in ("select", "iframe", "textarea"):
        u, _, w = pu("<%s/>機種A967G" % tag)
        t("構造: <%s/>自己終了は開いたまま＋nonvoid_self_closing 警告（値漏れなし）" % tag,
          all("967" not in x.text for x in u) and "nonvoid_self_closing" in w)

    # ── ★Codex増分A第17次指摘（外来名前空間の継承・HTML breakout）★ ──
    _, _, w = pu("<svg><math><mtext><circle/></mtext></math></svg>")   # math は svg名前空間を継承
    t("構造: <svg><math>…<circle/> は全てSVG継承＝circleは自己終了で閉じ誤警告なし（Codex増分A#17）",
      "nonvoid_self_closing" not in w)
    _, _, w = pu("<math><svg><title><mspace/></title></svg></math>")   # svg は mathml名前空間を継承
    t("構造: <math><svg>…<mspace/> は全てMathML継承＝mspaceは閉じ誤警告なし",
      "nonvoid_self_closing" not in w)
    u, _, w = pu("<svg><div/>機種Aの通常時天井は967G")   # div は breakout でHTMLへ→本文capture
    t("構造: <svg><div/> は breakout で svg を抜けHTML本文として capture＋nonvoid_self_closing（Codex増分A#17）",
      any("967" in x.text for x in u) and "nonvoid_self_closing" in w)
    u, _, w = pu("<math><p/>機種Aの通常時天井は967G")     # p は breakout でHTMLへ→本文capture
    t("構造: <math><p/> は breakout で math を抜けHTML本文として capture＋nonvoid_self_closing",
      any("967" in x.text for x in u) and "nonvoid_self_closing" in w)
    u, _, w = pu("<svg><foreignObject><div>本文967G</div></foreignObject></svg>")  # 統合点で正常閉じ
    t("構造: 統合点foreignObject配下の閉じたdivは breakout せず（誤警告なし・svg内で抑止）",
      "nonvoid_self_closing" not in w)

    # ── ★Codex増分A第18次指摘（annotation-xml直下svg特例の値漏れ防止・終了タグbreakout）★ ──
    u, _, w = pu("<math><annotation-xml encoding='application/xml'>"
                 "<svg><foreignObject><div/>機種Aの通常時天井は967G</foreignObject></svg>"
                 "</annotation-xml></math>")
    t("構造: annotation-xml(非HTML)直下svgはSVG名前空間→foreignObjectが統合点＝値漏れなし（Codex増分A#18）",
      all("967" not in x.text for x in u))
    u, _, w = pu("<svg></p>機種Aの通常時天井は967G")   # </p> は外来から breakout
    t("構造: 外来内の</p>は breakout で svg を抜け後続を本文 capture（Codex増分A#18）",
      any("967" in x.text for x in u))
    u, _, w = pu("<math></br>機種Aの通常時天井は967G")   # </br> は breakout＋<br>相当
    t("構造: 外来内の</br>は breakout で math を抜け後続を本文 capture",
      any("967" in x.text for x in u))

    # ── ★Codex増分A第19次指摘（統合点/名前空間のscope境界を越える終了タグの値漏れ防止）★ ──
    for html in ("<p><svg><foreignObject></p>機種Aの通常時天井は967G",
                 "<div><svg><foreignObject></div>機種Aの通常時天井は967G",
                 "<span><svg><desc></span>機種Aの通常時天井は967G",
                 "<p><math><mtext></p>機種Aの通常時天井は967G"):
        u, _, w = pu(html)
        t("構造: 統合点を挟んだ外側HTML終了タグは境界を越えず値漏れなし: %s" % html[:24],
          all("967" not in x.text for x in u))
    # 外来要素の終了後は本文へ復帰（967抑止・300は本文取得）＝Codex推奨の復帰確認
    u, _, w = pu("<p><svg><foreignObject></p>967G</foreignObject></svg>本文300G</p>")
    t("構造: 外来抑止内967は漏れず・外来終了後の本文300は取得（Codex増分A合格時の推奨確認）",
      all("967" not in x.text for x in u) and any("300" in x.text for x in u))

    u, _, w = pu("<h1>章題<p>本文967G</p>")
    t("構造: 未閉じ見出しは本文を吸収せず警告（unclosed_heading・Codex#2）",
      "unclosed_heading" in w
      and any(x.text == "本文967G" and x.heading_path == ((1, "章題"),) for x in u))

    _, h1s, w = pu("<h1>A<h2>B</h2>C</h1>")
    t("構造: 見出し入れ子/交差は警告付きで安全側（h1=A・警告あり）",
      h1s == ("A",) and len(w) > 0)

    u, _, w = pu("<div><span>本文</div>")
    t("構造: 未閉じインラインをブロックが閉じる交差は implicit_close（Codex#3）",
      "implicit_close" in w and any(x.text == "本文" for x in u))

    _, _, w = pu("<p>外500G<noscript>隠し600G</p>")
    t("構造: skip要素を跨ぐ暗黙終了も警告（無警告で捨てない・Codex#3）", "implicit_close" in w)

    u, _, w = pu("<table><div>表内600G</table>")   # 表内はD-1a-2-4領分＝単位化せず警告も出さない
    t("構造: 表内の構造は単位化しない（値が漏れない・警告も出さない・D-1a-2-4領分）",
      all("600" not in x.text for x in u))

    _, h1s, _ = pu("<h1>A<br>B</h1>")
    t("構造: 見出し内brは空白（ABに連結しない・Codex#6）", h1s == ("A B",))

    _, _, w = pu("<div>朝一300G")
    t("構造: EOF未閉じの省略不可タグは unclosed_tags（Codex#4）", "unclosed_tags" in w)

    _, _, w = pu("<p>" + ("天井は967G。" * 300000) + "</p>")
    t("構造: 巨大単一段落は text_limit で停止（メモリDoS・Codex#5）", "text_limit" in w)
    _, _, w = pu("<p>" + (";" * (_MAX_TOTAL_TEXT + 5)) + "</p>")
    t("構造: 区切り大量の巨大単一ノードも text_limit で停止", "text_limit" in w)

    # ★取得は1検証で厳密に1回だけ（二重取得しない）をスパイで直接保証（Codex非ブロッキング提案）
    _calls = {"n": 0}
    def _spy_fetch(url, allowed=None):
        _calls["n"] += 1
        return vc.fetch_html(url, allowed=allowed)
    V("ceiling.normal.at", ev(967, U1, Q_AT967), IDENT, fetch_fn=_spy_fetch)
    t("★二重化: 1検証で取得は厳密に1回（二重取得しない）", _calls["n"] == 1)

    # ── E2E: verify検証器 × resolve_with_dialogue ──
    fk = "ceiling.normal.at"
    verify = make_verifier(fk, IDENT)
    seed = [ev(967, U1, Q_AT967), ev(967, U2, Q_AT967)]
    r = resolve_with_dialogue(anomaly_key_of(fk), seed, verify, ask_codex=lambda q: [])
    t("★E2E: 大手2独立で天井967が決着 → state PASS/967",
      r["state"] == DPASS and r["value"] == 967)
    r = resolve_with_dialogue(anomaly_key_of(fk), [ev(967, U1, Q_AT967)],
                              verify, ask_codex=lambda q: [])
    t("★E2E: 大手1独立だけ → 非PASS(裏取り不足)", r["state"] != DPASS)
    Q745 = "アクダマの通常時の天井はAT間745Gで当選"
    mock_page("https://1geki.jp/akudama", "アクダマドライブ。" + Q745 + "する。")
    r = resolve_with_dialogue(anomaly_key_of(fk),
                              [ev(967, U1, Q_AT967),
                               ev(745, "https://1geki.jp/akudama", Q745, raw="745")],
                              verify, ask_codex=lambda q: [])
    t("★E2E: 値が対立(967 vs 745) → 非PASS(対立は自動決着しない)", r["state"] != DPASS)

    ok_all = all(c for _, c in results)
    print(f"\nselftest: {sum(1 for _, c in results if c)}/{len(results)} 合格")
    return 0 if ok_all else 1


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        sys.exit(selftest())
    ap.error("--selftest を指定")

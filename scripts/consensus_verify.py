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
    "final_url title h1_candidates response_sha256 html_sha256 rendered_text_sha256 "
    "parse_warnings units shadow_text")

# ★D-1a-2-3 構造単位: StructuredUnit（設計書v1.7 Q3・段落文/リスト項目/見出し継承＋表セル）★
#   kind = "paragraph_sentence" | "list_item" | "table_value_cell"（表セルは D-1a-2-4）
#   heading_path = ((level, text), ...) 単位生成時点の見出しアウトラインのコピー
#   table_context は表以外では None。parse_flags は単位個別の注記（例: truncated / table_malformed /
#   nested_table / header_cell）。
#   ★dom_order の契約 = 「確定(emit)順」★。atomic な li は閉じた時点で確定するため、入れ子リストでは
#   内側liが外側liより先に確定し得る（＝開始位置順とは限らない）。増分Bは源泉の開始位置順に依存しない。
StructuredUnit = namedtuple(
    "StructuredUnit",
    "unit_id kind text heading_path dom_order source_path table_context parse_flags")

# ★D-1a-2-4 表コンテキスト: TableContext（設計書v1.7 Q3）★
#   表ごとに独立。value_cell=その値セルの占有情報(dict: row/col/rowspan/colspan/text)。
#   row_headers/column_headers=その値セルに適用される行/列ヘッダーのテキスト列（複数段可）。
#   related_cells=C5合成/identityに使ったヘッダーセルのテキスト（監査用）。caption=表の<caption>。
#   header_resolution = explicit(headers属性) | scope(th scope=row/col) | positional(配置=左/上のth) |
#     header_cell(このセル自身がth) | unresolved(ヘッダー解決不能) | ambiguous(headers属性が不正id)。
TableContext = namedtuple(
    "TableContext",
    "table_id caption row_headers column_headers value_cell related_cells header_resolution")

# 構造化の上限（超過は parse_warnings→束縛側でREVIEW＝DoS/巨大入力対策・設計書v1.7 Q1/Q2）
_MAX_PARSE_NODES = 60000
_MAX_PARSE_DEPTH = 200
_MAX_PARSE_UNITS = 6000
_MAX_UNIT_TEXT = 2000
_MAX_TOTAL_TEXT = 2_000_000       # ★解析中の累積キャプチャ文字数上限（buf肥大＝メモリDoS対策・Codex D）
# 表（占有グリッド）の上限（設計書v1.7 Q1/Q2「セル数/rowspan/colspan にも上限→超過REVIEW」）
_MAX_TABLE_CELLS = 4000           # グリッド展開後の総占有セル数上限（rowspan/colspan爆発＝DoS）
_MAX_TABLE_ROWS = 500
_MAX_TABLE_COLS = 120
_MAX_SPAN = 100                   # 単一セルの rowspan/colspan 上限（超過は表 malformed）
_MAX_SCOPED_TH = 256              # scope属性付きth の上限（超過は limit_hit＝doc REVIEW・O(n²)抑止）
_MAX_HEADER_ATTR_LEN = 512        # headers属性の文字数上限（巨大 .split() を防ぐ）
_MAX_HEADER_IDS = 32              # headers属性のid個数上限（超過は ambiguous＋limit_hit）
_HEADER_OVERFLOW = "\x00hdr_overflow"   # 実在しないsentinel id（明示ヘッダー解決で ambiguous に倒す）
# 表内の構造タグ（占有グリッド構築で意味を持つ・最外表 depth==1 のときのみ処理）
_TABLE_STRUCT_TAGS = {"tr", "td", "th", "thead", "tbody", "tfoot", "caption",
                      "col", "colgroup"}
# ★明示的な非本文領域（記事本文でない＝表があっても証拠にしない・collectorを起こさない）★
#   これ以外の位置（ul>table・未知要素>table・div>table 等の構造誤り含む）にある最外表は content 判定に
#   関わらず可視化する（＝単位化されない表由来の値で迂回PASSされるのを防ぐ・Codex再レビューCritical1）。
_NONBODY_ANCESTORS = {"nav", "header", "footer", "aside", "form", "fieldset", "address"}


def _collapse_ws(s: str) -> str:
    """連続空白を1個に圧縮し前後を除去（セル/caption/ヘッダーのテキスト確定用）。"""
    return re.sub(r"\s+", " ", s or "").strip()


def _parse_span(raw):
    """rowspan/colspan 属性を整数化。未指定/空は1。★非整数/符号/巨大桁は -1（＝表 malformed 判定）。
    ★int() へ渡す前に「ASCII十進かつ3桁以内」を強制する（最大許容は _MAX_SPAN=100＝3桁）。
      数百万桁の属性文字列を int() に渡すCPU DoSを、Pythonの整数文字列変換桁数制限に依存せず防ぐ
      （PYTHONINTMAXSTRDIGITS=0 等で制限を無効化されても安全・Codex第3次レビューCritical）。★
    rowspan=0（HTML=残り全行に伸長）は 0 を返し、グリッド構築で malformed 扱いにする（設計書Q2）。"""
    if raw is None:
        return 1
    s = str(raw).strip()
    if s == "":
        return 1
    if len(s) > 3 or not s.isascii() or not s.isdigit():
        return -1              # 4桁以上/符号/全角・Unicode数字/非数字 → 整数化せず malformed 判定
    return int(s)              # ここに来るのは 0〜999 の ASCII 十進のみ（int() は安全・有界）


# 巨大な数値文字参照(&#…;)の整数化DoS検出は取得層(verify_claims)に単一実装を置き、ここは委譲する
# （同一ロジックを二重管理しない）。fetch層でも取得直後に同じ関所が働く（title unescape 前・二重防御）。
_has_overlong_numeric_charref = vc._has_overlong_numeric_charref

_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_LIST_TAGS = {"ul", "ol", "menu"}       # li を直接の子に持てるリスト要素（menu も含む・Codex#3）
# 内側テキストを証拠にしない要素（head/title/textarea＝値混入防止・Codex#1／select等の
# フォーム選択肢・iframe/canvas/object/svg/math/audio/video/map/embed/picture等の埋込＝記事本文でない）。
# ★map は除外＝透過コンテンツ（実際に表示される本文）なのでインラインとして捕捉する（Codex第10次C3）。★
_SKIP_CONTENT_TAGS = {"script", "style", "noscript", "template",
                      "head", "title", "textarea",
                      "select", "datalist", "iframe", "canvas", "object",
                      "svg", "math", "audio", "video", "embed", "picture"}
_TABLE_TAGS = {"table"}                 # ★表は _TableCollector が占有グリッドで単位化（D-1a-2-4）★
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
#   ★ruby/rb は透過＝基底テキストを捕捉する（表示される本文）。注釈 rt/rp/rtc は _annot_depth で別途ドロップ
#     ＝ふりがな注釈は証拠にしない（Codex第10/11次C2）。map は透過コンテンツ（Codex第10次C3）。
_INLINE_TAGS = {"a", "span", "b", "i", "em", "strong", "u", "s", "strike",
                "small", "big", "sub", "sup", "mark", "code", "kbd", "samp",
                "var", "cite", "q", "abbr", "dfn", "time", "label", "tt", "font",
                "bdi", "bdo", "ins", "del", "data", "output", "map", "ruby", "rb"}
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
_MAX_SINGLE_TAG_LEN = 1_000_000   # 単一タグ <…> の長さ上限（巨大属性列のメモリ増幅を feed前に止める・Codex第13次DoS）
_MAX_ATTRS = 2000                 # 1タグの属性数上限（HTMLParser後の辞書化増幅を止める・Codex第13次DoS）


def _is_ascii_alpha(c):
    return ("a" <= c <= "z") or ("A" <= c <= "Z")


def _quoted_gt(s, i, n):
    """引用符 '…' "…" 内の `>` を飛ばして本当の終端 `>` の次位置を返す（開始/終了タグ用）。"""
    quote = ""
    while i < n:
        ch = s[i]
        if quote:
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
        elif ch == ">":
            return i + 1
        i += 1
    return n


def _decl_end(s, i, n):
    """宣言 <!DOCTYPE …> の終端：引用符＋内部サブセット `[ … ]` 深さを尊重し、深さ0の `>` の次位置を返す。"""
    quote = ""
    depth = 0
    while i < n:
        ch = s[i]
        if quote:
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            if depth > 0:
                depth -= 1
        elif ch == ">" and depth == 0:
            return i + 1
        i += 1
    return n


def _tag_token_end(s, lt, n):
    """`<` の位置 lt から「そのmarkupトークンの終端(次の走査開始位置)」を返す（HTMLParser相当の終端規則）。
    ・<!-- … -->            : `-->`（コメント内の `>` を終端と誤認しない）
    ・<![CDATA[…]]> 等       : `]]>`（marked section・内部の `>` を誤認しない・Codex第15次C2）
    ・<!DOCTYPE … [ … ]>    : 引用符＋内部サブセット `[]` 深さ0の `>`（内部宣言の `>` を誤認しない・Codex第15次C2）
    ・<tag …> / </tag>      : 属性値の引用符内 `>` を飛ばした本当の `>`（開始はASCII英字・終了は/＋ASCII英字のみ）
    ・<? … >                : 最初の `>`（PI）
    ・地の文の "<"          : タグでない → lt+1（呼び出し側で continue）
    ★DoS事前検査用：終端を過小評価しない（巨大トークンを見逃さない）ことが目的。ASCII英字のみタグ扱い（第15次非critical）。"""
    c = s[lt + 1] if lt + 1 < n else ""
    if c == "!":
        if s[lt + 2:lt + 4] == "--":                 # コメント
            end = s.find("-->", lt + 4)
            return (end + 3) if end != -1 else n
        if s[lt + 2:lt + 3] == "[":                  # marked section <![CDATA[…]]> / <![IGNORE[…]]> 等
            end = s.find("]]>", lt + 3)
            return (end + 3) if end != -1 else n
        return _decl_end(s, lt + 2, n)               # <!DOCTYPE …>（内部サブセット[]深さ尊重）
    if c == "?":                                      # PI <? … >
        end = s.find(">", lt + 2)
        return (end + 1) if end != -1 else n
    if c == "/":                                      # ★終了タグ </…>：HTMLParserは妥当タグ名でなくても `>` まで
        #   処理する（引用符も終端保護にならない）。`</   div …>`/`</1…>` 等の巨大不正終了タグを見逃さない（Codex第16次C2）。
        end = s.find(">", lt + 2)
        return (end + 1) if end != -1 else n
    if _is_ascii_alpha(c):                            # 開始タグ：ASCII英字始まりのみ（Unicode地の文を誤タグ化しない）
        return _quoted_gt(s, lt + 1, n)
    return lt + 1                                    # 地の文の "<"


def _has_overlong_tag(s):
    """★feed前に「異常に長い単一markupトークン」を線形検出（巨大属性列/巨大コメントでHTMLParserがメモリ増幅
    する前に止める）★。引用符・コメント終端を尊重してトークン終端を求め、1MB超なら True（＝文書REVIEW）。O(len(s))。"""
    n = len(s)
    i = s.find("<")
    while i != -1:
        end = _tag_token_end(s, i, n)
        if end <= i + 1:                             # 地の文の "<" → 次の "<" へ
            i = s.find("<", i + 1)
            continue
        if (end - i) > _MAX_SINGLE_TAG_LEN:
            return True
        i = s.find("<", end)
    return False
_SENT_SPLIT_STRUCT = re.compile(r"[。！？；;\n]")  # 構造単位内の文分割（br/改行も境界）

# ★表セル/caption/stray 内テキストの「要素境界」分類（Codex第7次＝境界を跨ぐ数字連結の値漏れ対策）★
#   JOIN = インライン/句レベル（テキストは確実に連続＝区切らない。<span>9</span><span>67</span>=967 を保つ）。
#   ruby は基底テキストが連続（rt/rp の注釈内容は data() で別途ドロップ＝基底のみ証拠に・Codex第8次C2）。
_JOIN_TAGS = _INLINE_TAGS | {"ruby", "rb", "rt", "rp", "rtc", "nobr", "wbr"}
#   KNOWN = 「区切っても安全（確実にブロック境界 or 内容を捨てる）」と分かっている標準要素だけ。この集合に
#   無い要素（未知カスタム＋表示/置換/動的内容が曖昧な標準要素）は数字が隣接した境界で文書REVIEWに倒す。
#   ★slot(shadow DOM)・meter/progress/selectedcontent(置換/動的表示)は「分離して安全」と言えない＝KNOWNに入れない
#     ＝数字隣接なら doc REVIEW（無警告で値を分離させない・Codex第8/9次）★。JOIN(インライン)は上で確実に連結。
_KNOWN_TAGS = (_HEADING_TAGS | _LIST_TAGS | _SKIP_CONTENT_TAGS | _TABLE_TAGS | _VOID_TAGS
               | _PARA_BLOCKS | _STRUCT_BLOCKS | _INLINE_TAGS | _TABLE_STRUCT_TAGS | _JOIN_TAGS
               | {"li", "html", "body", "head", "button", "optgroup", "option", "center",
                  "dir", "marquee", "menu", "menuitem", "legend", "figcaption"})
# ruby注釈（rt=読み仮名／rp=非対応フォールバック／rtc=注釈コンテナ）＝基底テキストでない。内側dataは証拠にしない。
#   ★注釈判定は「実親が ruby(rt/rp/rtc) または rtc(rt/rp)」のフレームのみ＝孤立/誤配置は内容を表す（Codex第10次C2）。★
_ANNOTATION_TAGS = {"rt", "rp", "rtc"}
_ANNOT_PARENTS = {"rt": {"ruby", "rtc"}, "rp": {"ruby", "rtc"}, "rtc": {"ruby"}}
# ★内容が「条件付きfallback」で捨てて安全と断定できない要素（object/canvas/audio/video/noscript）★。
#   表セル内に現れたら、内側に隠れた値を無警告で消して本文側をPASSさせないため doc REVIEW に倒す（Codex第10次）。
_FALLBACK_DROP_TAGS = {"object", "canvas", "audio", "video", "noscript"}
# ★実際に描画されるが構造抽出しない skip 要素（svg/math のtext・object等のfallback）＝落とすと可視値が消える。
#   content領域内での内側テキストを shadow へ退避し、verify が「値が shadow に出たらREVIEW」で迂回を塞ぐ（Codex第11次）。
# ★textarea(既定値)/select・option/datalist は content領域では表示され得る＝可視扱いで shadow へ（Codex第13次C2）。
_SHADOW_SKIP = {"svg", "math", "object", "canvas", "audio", "video", "noscript",
                "textarea", "select", "datalist"}
# ★確実に非表示の skip（code/埋込/メタ）＝可視skipの内側でもここは shadow にしない（Codex第12次Major）。
_HIDDEN_SKIP = {"script", "style", "template", "head", "title", "iframe", "embed", "picture"}
_MAX_SHADOW_CHUNKS = 20000        # shadow断片数の上限（コメント分断での断片爆発＝メモリDoS対策・Codex第12次）
# ★決定論的に「表示されない」と判る指定＝hidden属性／インラインstyleの display:none・visibility:hidden。
#   その内容は「正の証拠(unit)」にせず shadow へ回す＝表示されていない値を自動公開しない（Codex第19次Critical）。
#   ★style属性は素の正規表現でなく「有界なCSS宣言パーサ」で解釈する（Codex第20次Critical）＝
#     コメント/文字列を区別・CSSエスケープを復号・!important を認識・同一プロパティは importance＋後勝ちで解決。
#   ※外部/埋込CSS・クラス由来の非表示は rendered DOM が必要で静的解析では判定不能（下記の本番配線ゲート参照）。
_MAX_STYLE_LEN = 4096             # style属性の長さ上限（超過は解釈不能→REVIEW）
_MAX_STYLE_DECLS = 128            # style属性の宣言数上限（同上）

# ★★rendered 監査証跡ゲート（Codex第20/21次指摘＝機械的強制）★★
#   静的HTML解析では「外部/埋込CSS・クラスセレクタ由来の非表示」「CSS var()/計算値」を原理的に判定できない。
#   ＝静的解析だけの verified=True は「HTMLソース上の存在」しか示さず「実表示上の存在」を保証しない。
#   ★従って verify_evidence は rendered 監査証跡（rendered_attestation）が無い/不一致なら verified=True を返さない。★
#   証跡は「同一ページへの束縛（final_url＋html_sha256）」＋「実表示テキストに 値と機種同定が在る」ことを要求する。
#   D-1b dry-run 以降で Playwright（audit_render 相当）から供給する。証跡なし＝常にREVIEW＝fail-closed。
REQUIRES_RENDERED_DOM_BEFORE_AUTOPUBLISH = True


_MAX_ATTEST_TEXT = 2_000_000      # 証跡の可視テキスト総長上限（外部入力のCPU/メモリDoS対策・Codex第22次M3）
_MAX_ATTEST_IDENTS = 32           # identity 個数上限
_MAX_ATTEST_IDENT_LEN = 200       # identity 1件の長さ上限


def _attestation_ok(att, final_url, html_sha256, raw, nids):
    """★rendered 監査証跡の検証（同一ページ束縛＋実表示テキストでの値/機種同定）★。戻り (ok, code)。
    att = {"final_url":…, "html_sha256":…, "visible_text":…}（Playwright等で実描画後に得た可視テキスト）。
    ★外部入力なので長さ・個数に上限を設ける（DoS対策）。上限超過は証跡不正＝REVIEW。"""
    if not isinstance(att, dict):
        return False, "RENDERED_ATTESTATION_REQUIRED"
    if not att.get("final_url") or att.get("final_url") != final_url:
        return False, "ATTESTATION_URL_MISMATCH"          # 別ページの証跡を流用させない
    if not att.get("html_sha256") or att.get("html_sha256") != html_sha256:
        return False, "ATTESTATION_HASH_MISMATCH"         # 取得スナップショットと同一である束縛
    vis = att.get("visible_text")
    if not isinstance(vis, str) or not vis.strip():
        return False, "ATTESTATION_NO_VISIBLE_TEXT"
    if len(vis) > _MAX_ATTEST_TEXT:
        return False, "ATTESTATION_TOO_LARGE"
    if len(nids) > _MAX_ATTEST_IDENTS or any(len(s) > _MAX_ATTEST_IDENT_LEN for s in nids):
        return False, "ATTESTATION_TOO_LARGE"
    if not _unit_contains_value(vis, raw):
        return False, "ATTESTATION_VALUE_NOT_VISIBLE"     # 実表示に値が無い＝CSS等で非表示だった
    nvis = vc.normalize(vis)
    if any(not vc._ident_in(s, nvis) for s in nids):
        return False, "ATTESTATION_IDENT_NOT_VISIBLE"     # 実表示に対象機種同定が無い
    return True, "OK"


def _css_unescape(s):
    """CSSエスケープを復号（有界・O(len(s))）。
    ・\\6f 形式（16進1〜6桁＋任意の空白1個）
    ・\\<改行>（CRLF/CR/LF/FF）＝line continuation は**除去**（`displ\\<改行>ay` → `display`・Codex第22次M2）
    ・\\x 形式＝そのままの文字"""
    if "\\" not in s:
        return s
    out = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = s[i + 1]
        if nxt in ("\n", "\f"):                 # エスケープされた改行＝行継続（何も出力しない）
            i += 2
            continue
        if nxt == "\r":
            i += 3 if (i + 2 < n and s[i + 2] == "\n") else 2
            continue
        j = i + 1
        hexd = ""
        while j < n and len(hexd) < 6 and s[j] in "0123456789abcdefABCDEF":
            hexd += s[j]
            j += 1
        if hexd:
            try:
                out.append(chr(int(hexd, 16)))
            except (ValueError, OverflowError):
                out.append("�")
            if j < n and s[j] in " \t\r\n\f":
                j += 1
            i = j
        else:
            out.append(nxt)
            i += 2
    return "".join(out)


_MAX_CSS_DEPTH = 32               # component value の括弧ネスト上限（有界化）


def _css_declarations(style):
    """style属性を宣言リストに分解（コメント除去／文字列・エスケープ・括弧深さを尊重して ; で分割・有界）。
    戻り (decls, ok)。ok=False＝上限超過/未閉じコメント/未閉じ文字列/括弧不整合＝解釈不能（呼び出し側でREVIEW）。"""
    n = len(style)
    if n > _MAX_STYLE_LEN:
        return [], False
    _PAIR = {")": "(", "]": "[", "}": "{"}
    decls, buf, stack = [], [], []
    i, quote = 0, ""
    while i < n:
        ch = style[i]
        if quote:
            if ch == "\\" and i + 1 < n:
                buf.append(ch)
                buf.append(style[i + 1])
                i += 2
                continue
            if ch in ("\n", "\f", "\r"):
                return decls, False           # ★文字列内の未エスケープ改行＝bad-string（第22次M2）
            buf.append(ch)
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch == "\\" and i + 1 < n:          # ★文字列外のCSSエスケープ（\; は区切りにしない）
            buf.append(ch)
            buf.append(style[i + 1])
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and style[i + 1] == "*":
            end = style.find("*/", i + 2)
            if end == -1:
                return decls, False           # 未閉じコメント＝解釈不能
            i = end + 2
            continue
        if ch in "([{":                       # ★component value の括弧内（url(x;y) 等）は区切らない
            stack.append(ch)
            if len(stack) > _MAX_CSS_DEPTH:
                return decls, False
            buf.append(ch)
            i += 1
            continue
        if ch in ")]}":                       # ★括弧の種類一致を要求（(] のような不整合は解釈不能・第22次M1）
            if not stack or stack[-1] != _PAIR[ch]:
                return decls, False
            stack.pop()
            buf.append(ch)
            i += 1
            continue
        if ch == ";" and not stack:
            decls.append("".join(buf))
            buf = []
            if len(decls) > _MAX_STYLE_DECLS:
                return decls, False
            i += 1
            continue
        buf.append(ch)
        i += 1
    if quote or stack:
        return decls, False                   # 未閉じ文字列/括弧＝解釈不能
    if buf:
        decls.append("".join(buf))
    return decls, (len(decls) <= _MAX_STYLE_DECLS)


def _css_top_colon(d):
    """宣言内で「文字列・エスケープ・括弧の外」にある最初の ':' の位置（無ければ -1）。"""
    i, n, quote, depth = 0, len(d), "", 0
    while i < n:
        ch = d[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch == "\\":
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch in "([{":
            depth += 1
            i += 1
            continue
        if ch in ")]}":
            if depth > 0:
                depth -= 1
            i += 1
            continue
        if ch == ":" and depth == 0:
            return i
        i += 1
    return -1


_CSS_IMPORTANT_RE = re.compile(r"!\s*important\s*$", re.I)
# 計算値が静的に確定しない値（カスケード/変数）＝解釈不能としてREVIEWへ倒す（Codex第21次C2）
_CSS_GLOBAL_VALUES = {"inherit", "initial", "unset", "revert", "revert-layer"}
_CSS_DYNAMIC_FUNCS = ("var(", "env(", "attr(")
# ★display は「トークン集合」でなく **classifyできる正規形の明示列挙**（CSS Display L3の代表形）。
#   ここに無い値（`block none`・`table-cell flex`・ベンダー拡張・不明）は分類不能＝安全側にREVIEW（Codex第22次C4）。
_CSS_DISPLAY_HIDDEN = {"none"}
_CSS_DISPLAY_VISIBLE = {
    "block", "inline", "inline-block", "flex", "inline-flex", "grid", "inline-grid",
    "table", "inline-table", "table-row", "table-cell", "table-caption", "table-column",
    "table-column-group", "table-footer-group", "table-header-group", "table-row-group",
    "list-item", "contents", "flow-root", "run-in", "ruby", "ruby-base", "ruby-text",
    "ruby-base-container", "ruby-text-container", "math",
    # CSS Display L3 の代表的な2値形（outside inside / list-item 併記）
    "block flow", "block flow-root", "inline flow", "inline flow-root",
    "block flex", "inline flex", "block grid", "inline grid",
    "block table", "inline table", "block ruby", "inline ruby",
    "block flow list-item", "inline flow list-item", "block list-item", "inline list-item",
    "list-item block flow", "list-item inline flow",
}
_CSS_VISIBILITY_HIDDEN = {"hidden", "collapse"}   # collapse も非表示相当（Codex第21次C2）
_CSS_VISIBILITY_VISIBLE = {"visible"}


def _css_value_state(prop, keyword):
    """宣言値の状態: 'hidden' / 'visible' / 'dynamic'（計算値が静的に確定しない）/ 'unknown'（分類不能）。
    ★'unknown' と 'dynamic' はどちらも呼び出し側で ok=False＝REVIEW（無効値を勝手に visible 扱いしない）★"""
    kw = " ".join(keyword.strip().lower().split())    # 連続空白を1個に正規化
    if not kw:
        return "unknown"
    if kw in _CSS_GLOBAL_VALUES or any(f in kw for f in _CSS_DYNAMIC_FUNCS):
        return "dynamic"
    if prop == "visibility":
        if kw in _CSS_VISIBILITY_HIDDEN:
            return "hidden"
        return "visible" if kw in _CSS_VISIBILITY_VISIBLE else "unknown"
    if kw in _CSS_DISPLAY_HIDDEN:
        return "hidden"
    return "visible" if kw in _CSS_DISPLAY_VISIBLE else "unknown"


def _style_hides(style):
    """★インラインstyleが「表示されない」を指定しているか（display:none / visibility:hidden|collapse）★。
    戻り (hidden, ok)。ok=False＝解釈不能/上限超過/計算値が静的に確定しない（呼び出し側でREVIEW）。
    CSS準拠: 無効な宣言は無視して先行宣言を上書きしない。同一プロパティは importance優先＋同importanceは後勝ち。"""
    decls, ok = _css_declarations(style or "")
    if not ok:
        return False, False
    win = {}
    for d in decls:
        pos = _css_top_colon(d)
        if pos < 0:
            continue
        prop = _css_unescape(d[:pos]).strip().lower()
        if prop not in ("display", "visibility"):
            continue
        uval = _css_unescape(d[pos + 1:])
        important = False
        m = _CSS_IMPORTANT_RE.search(uval)
        if m:
            important = True
            uval = uval[:m.start()]
        state = _css_value_state(prop, uval)   # ★引用符は剥がさない（display:"none" は分類不能）
        if state in ("dynamic", "unknown"):
            # ★var()/global＝計算値不定、分類不能値（display:block none 等）＝CSS妥当性を静的に断定できない。
            #   「無効だから無視」と決めつけると先行の display:none を誤って visible にし得る（第22次C4）→REVIEW。
            return False, False
        prev = win.get(prop)
        if prev is None or important or (not prev[0]):
            win[prop] = (important, state)
    return (win.get("display", (False, ""))[1] == "hidden"
            or win.get("visibility", (False, ""))[1] == "hidden"), True
# ★obsolete raw-text要素＝ブラウザは後続をテキスト表示するが html.parser の解釈が乖離（内側scriptを実行扱い等）。
#   構造抽出できないので content領域での出現は doc REVIEW（Codex第11次C4）。
_OBSOLETE_RAWTEXT = {"xmp", "plaintext", "listing"}


class _ParseStop(Exception):
    """上限超過で feed() のトークナイズごと即中断する内部例外（Codex#4・CPU有界化）。"""


class _TableCollector:
    """★1つの最外表を占有グリッドで構造化し table_value_cell 単位データを生成（設計書v1.7 Q2/Q3）★
    ・_StructureParser と並走。最外 <table> 開始で生成、閉じ（frame pop）で finalize。
    ・構造(tr/td/th/caption/thead…)は最外表 depth==1 のときだけグリッドに使う。
    ・ネスト表(depth>=2)は別処理せず、内側テキストは「現在の最外セル」に入れて親セルへ nested フラグ。
      親セルが無い（tr直下のネスト表等）ネスト表の内側テキストは stray へ回して可視化する（Codex Critical3）。
    ・不正グリッド/巨大/rowspan=0/重なり → table_malformed（値は矯正して可視のまま・表全体REVIEW）。
    ・★上限（行/列/占有セル数）超過で「セルを捨てる」ときは limit_hit を立て、_StructureParser が文書全体の
      parse_warnings に昇格（＝doc全体REVIEW）。捨てた値が本文側の同値で迂回PASSするのを防ぐ（Codex Critical2/4）。★
    ・caption と「セル外/親セルなしネスト表の非空テキスト(stray)」は flag 付きの可視化単位として emit
      （値がそこにしか無い経路の迂回を塞ぐ・Codex Critical1/3）。
    ・ヘッダー解決不能 → header_resolution=unresolved（束縛側REVIEW）。★値は可視＝意味割れ検知を効かせる★。
    ★html.parser は暗黙終了しないので、セル/行の閉じはコレクタが行う（新td/th/tr・行グループ・finalizeで確定）。"""

    def __init__(self, heading_path, source_path):
        self.heading_path = heading_path
        self.source_path = source_path
        self.depth = 0
        self.rows = []            # [[cell, ...], ...] 最外表(depth==1)の行
        self.cur_row = None
        self.cur_cell = None
        self.in_caption = False
        self.caption_buf = []
        self.stray_buf = []       # セル外/親セルなしネスト表のテキスト（可視化して迂回を塞ぐ）
        self.malformed = False
        self.limit_hit = False    # ★上限超過でセルを捨てた＝doc全体REVIEWへ昇格するフラグ★
        self._cells = 0
        self._pending_sep = False       # 次のdataでバッファへ区切りを入れる（ブロック/void/skip/構造境界）
        self._pending_unknown = False   # その境界が未知カスタム要素（数字隣接なら曖昧→doc REVIEW）
        # ★ruby注釈(rt/rp/rtc)の内側判定は parser側の _annot_depth（フレームpush/popに同期）が担う。
        #   collectorはカウンタを持たない（暗黙終了で desync しないため・Codex第9次C2）。★

    # ── 境界・テキスト（_StructureParser から転送）──
    def _active_buf(self):
        """現在テキストが入るバッファ（data() の振り分けと一致）＝caption/cur_cell/stray。"""
        if self.depth == 1 and self.in_caption:
            return self.caption_buf
        if self.cur_cell is not None:
            return self.cur_cell["buf"]
        return self.stray_buf

    def mark_boundary(self, tag):
        """★要素境界の分類（Codex第7次）★。JOIN(インライン/句)=何もしない（連結を保つ）。
        td/th(depth1)=セル切替で別単位＝区切り不要。それ以外(ブロック/void/skip/構造/未知)=次のdataで区切りを入れる。
        未知カスタム要素は _pending_unknown も立て、数字と数字の間なら doc REVIEW（連結/分離の両解釈で値を見失うため）。
        ★全開始/終了タグ（void/skip/未知含む）で呼ばれる。区切りは _flush_pending が「実際に入るsink」へ遅延挿入。★"""
        if tag in _FALLBACK_DROP_TAGS:
            self.limit_hit = True        # object/canvas/audio/video/noscript＝fallback内容を捨てる→隠れ値→doc REVIEW
        if tag in _JOIN_TAGS:
            return
        if self.depth == 1 and tag in ("td", "th"):
            return                       # セル切替＝新バッファ（別単位・区切り不要）
        self._pending_sep = True
        if tag not in _KNOWN_TAGS:
            self._pending_unknown = True

    def _flush_pending(self, text):
        """data() 直前に保留中の区切りを「実際に text が入る sink」へ入れる（caption/cell/stray のズレを解消）。
        未知境界が数字と数字の間なら doc REVIEW（limit_hit）。"""
        if not self._pending_sep:
            return
        buf = self._active_buf()
        last = str(buf[-1])[-1:] if buf else ""
        if self._pending_unknown and last.isdigit() and text[:1].isdigit():
            self.limit_hit = True        # 未知カスタム要素が数字間＝曖昧→doc REVIEW
        if buf and last not in (" ", "\n"):
            buf.append(" ")
        self._pending_sep = False
        self._pending_unknown = False

    def open_table(self):
        self._pending_sep = True         # ネスト表開始＝ブロック境界（次dataで親セル/strayを区切る）
        self.depth += 1
        if self.depth >= 2 and self.cur_cell is not None:
            self.cur_cell["nested"] = True   # ネスト表: 親セルへフラグ（親セルが無ければ内側は stray へ）

    def close_table(self):
        self.depth = max(0, self.depth - 1)
        self._pending_sep = True         # ネスト表終了＝ブロック境界

    def _close_cell(self):
        if self.cur_cell is not None:
            if self.cur_row is None:
                self.cur_row = []
            self.cur_row.append(self.cur_cell)
        self.cur_cell = None

    def _close_row(self):
        self._close_cell()
        if self.cur_row is not None:
            self.rows.append(self.cur_row)
        self.cur_row = None

    def starttag(self, tag, attrs):
        # ★構造(グリッド)のみ担当。境界の区切りは mark_boundary（全タグで別途呼ばれる）が保留し _flush_pending が挿入。★
        if tag in ("strong", "b") and self.cur_cell is not None:
            self.cur_cell["strong"] = True
        if self.depth != 1:
            return                # ネスト表内(>=2)/未開(0)は最外グリッドに使わない（テキストは親セル/strayへ）
        if tag == "caption":
            self._close_cell()
            self.in_caption = True
            return
        if tag in ("thead", "tbody", "tfoot"):
            self._close_row()     # 行グループ境界＝開いている行/セルを確定
            return
        if tag == "tr":
            self._close_row()
            self.cur_row = []
            return
        if tag in ("td", "th"):
            self.in_caption = False
            self._close_cell()
            if self._cells >= _MAX_TABLE_CELLS:   # ★論理セル数の上限＝以降はセルを作らない（DoS抑止）
                self.limit_hit = True             #   以降のテキストは stray へ（cur_cell=None）＝可視のままdoc昇格
                return
            if self.cur_row is None:      # tr の外の td/th は暗黙の行を作る（欠落tr）
                self.cur_row = []
            ad = {}
            for k, v in (attrs or []):
                ad[(k or "").lower()] = (v or "")
            # ★headers属性の個数・総長に上限（巨大 .split() とメモリ増幅を防ぐ・Codex再レビューCritical3）★
            #   超過は sentinel id（明示解決で ambiguous）＋ limit_hit（doc REVIEW）で二重に安全側へ。
            raw_hdr = ad.get("headers") or ""
            if len(raw_hdr) > _MAX_HEADER_ATTR_LEN:
                hids = [_HEADER_OVERFLOW]
                self.limit_hit = True
            else:
                hids = raw_hdr.split()
                if len(hids) > _MAX_HEADER_IDS:
                    hids = [_HEADER_OVERFLOW]
                    self.limit_hit = True
            self.cur_cell = {
                "kind": tag, "buf": [],
                "colspan": _parse_span(ad.get("colspan")),
                "rowspan": _parse_span(ad.get("rowspan")),
                "cid": (ad.get("id") or "").strip(),
                "hids": hids,
                "scope": (ad.get("scope") or "").strip().lower(),
                "nested": False, "strong": False,
            }
            self._cells += 1
            return
        # br/hr/strong/b やブロック要素の区切りは mark_boundary が担当（strong/b の has_strong は上で処理）。

    def endtag(self, tag):
        # ★構造(セル/行の確定)のみ担当。境界の区切りは mark_boundary（全タグで別途呼ばれる）が保留する。★
        if self.depth != 1:
            return
        if tag == "caption":
            self.in_caption = False
        elif tag in ("td", "th"):
            self._close_cell()
        elif tag in ("tr", "thead", "tbody", "tfoot"):
            self._close_row()

    def data(self, text):
        # ★ruby注釈(rt/rp/rtc)内のdataは parser が事前に除外（_annot_depth）。ここには基底テキストのみ届く。★
        self._flush_pending(text)               # 保留中の境界区切りを「実際に入るsink」へ（数字連結防止・Codex第7次）
        if self.depth == 1 and self.in_caption:
            self.caption_buf.append(text)
        elif self.cur_cell is not None:
            self.cur_cell["buf"].append(text)   # ネスト表内テキストもここ（親セルは nested フラグ）
        else:
            self.stray_buf.append(text)         # ★セル外/親セルなしネスト表の値も可視化（Codex Critical3）★

    # ── 占有グリッド構築＋ヘッダー解決＋単位データ生成 ──
    def _build_grid(self):
        """占有グリッドを構築。各セルに _r/_c/_rs/_cs を付与。重なり/不正spanは malformed。
        ★占有セル数/行/列の上限に達したら物理的に停止（CPU/メモリ有界化）＋ limit_hit（doc全体REVIEW）★。
        戻り: grid=dict[(r,c)->cell]。★cell参照は同一オブジェクトで共有（rowspan/colspan継承）★"""
        for row in self.rows:
            for cell in row:
                cell["text"] = _collapse_ws("".join(cell["buf"]))
        grid = {}
        occ = 0
        stop = False
        for r, row in enumerate(self.rows):
            if stop:
                break
            if r >= _MAX_TABLE_ROWS:              # 行上限＝以降の行のセルを捨てる→doc昇格
                self.malformed = True
                self.limit_hit = True
                break
            c = 0
            for cell in row:
                while (r, c) in grid:
                    c += 1
                cs, rs = cell["colspan"], cell["rowspan"]
                if cs < 1 or rs < 1 or cs > _MAX_SPAN or rs > _MAX_SPAN:
                    self.malformed = True
                    cs, rs = 1, 1        # 可視化のため1に矯正して継続（値は残す）
                if c + cs > _MAX_TABLE_COLS:      # 列上限＝以降のセルを捨てる→doc昇格
                    self.malformed = True
                    self.limit_hit = True
                    stop = True
                    break
                cell["_r"], cell["_c"], cell["_rs"], cell["_cs"] = r, c, rs, cs
                for dr in range(rs):
                    for dc in range(cs):
                        if occ >= _MAX_TABLE_CELLS:   # ★占有セル総数上限＝物理停止（挿入予算を超えない）
                            self.malformed = True
                            self.limit_hit = True
                            stop = True
                            break
                        key = (r + dr, c + dc)
                        if key in grid:
                            self.malformed = True     # 重なり（値は残す・矯正しない）
                        else:
                            grid[key] = cell
                            occ += 1
                    if stop:
                        break
                c += cs
                if stop:
                    break
        return grid

    @staticmethod
    def _overlap(a0, a1, b0, b1):
        return a0 < b1 and b0 < a1

    def _left_ths(self, cell, grid):
        """値セルの左（同一行帯）にある th を列順で（＝行ヘッダー候補・positional）。"""
        out, seen = [], set()
        for rr in range(cell["_r"], cell["_r"] + cell["_rs"]):
            for cc in range(0, cell["_c"]):
                g = grid.get((rr, cc))
                if g is not None and g["kind"] == "th" and id(g) not in seen:
                    seen.add(id(g))
                    out.append((g["_c"], g))
        out.sort(key=lambda x: x[0])
        return [g for _c, g in out]

    def _above_ths(self, cell, grid):
        """値セルの上（同一列帯）にある th を行順で（＝列ヘッダー候補・positional・複数段可）。"""
        out, seen = [], set()
        for cc in range(cell["_c"], cell["_c"] + cell["_cs"]):
            for rr in range(0, cell["_r"]):
                g = grid.get((rr, cc))
                if g is not None and g["kind"] == "th" and id(g) not in seen:
                    seen.add(id(g))
                    out.append((g["_r"], g))
        out.sort(key=lambda x: x[0])
        return [g for _r, g in out]

    def _resolve_headers(self, cell, grid, scoped, by_id):
        """値セル(td)に適用される (row_headers, column_headers, related_cells, resolution)。
        優先: ①headers属性→対応id ②th scope=row/col ③配置(左/上のth)。解決不能は unresolved。
        scoped は「scope属性付きth」の事前計算リスト（セルごと再構築しない・Codex#5=O(n²)回避）。"""
        # ① headers 属性（明示関連付け）
        if cell["hids"]:
            hs = []
            for hid in cell["hids"]:
                hc = by_id.get(hid)
                if hc is None or hc["kind"] != "th":
                    return (), (), (), "ambiguous"   # 不正/欠落id → 曖昧
                hs.append(hc)
            rowh = tuple(h["text"] for h in hs if h["scope"] in ("row", "rowgroup"))
            colh = tuple(h["text"] for h in hs if h["scope"] not in ("row", "rowgroup"))
            rel = tuple(h["text"] for h in hs)
            return rowh, colh, rel, "explicit"
        # ② th scope=row/col（明示スコープ・事前計算済み scoped）
        if scoped:
            rowh = [h for h in scoped if h["scope"] in ("row", "rowgroup")
                    and self._overlap(h["_r"], h["_r"] + h["_rs"], cell["_r"], cell["_r"] + cell["_rs"])
                    and h["_c"] < cell["_c"]]
            colh = [h for h in scoped if h["scope"] in ("col", "colgroup")
                    and self._overlap(h["_c"], h["_c"] + h["_cs"], cell["_c"], cell["_c"] + cell["_cs"])
                    and h["_r"] < cell["_r"]]
            rowh.sort(key=lambda h: h["_c"])
            colh.sort(key=lambda h: h["_r"])
            if rowh or colh:
                rel = tuple(h["text"] for h in rowh) + tuple(h["text"] for h in colh)
                return (tuple(h["text"] for h in rowh), tuple(h["text"] for h in colh),
                        rel, "scope")
            return (), (), (), "unresolved"    # scope表だがこのセルに適用ヘッダーなし→解決不能
        # ③ 配置（左のth=行ヘッダー・上のth=列ヘッダー）
        rowh = self._left_ths(cell, grid)
        colh = self._above_ths(cell, grid)
        if rowh or colh:
            rel = tuple(h["text"] for h in rowh) + tuple(h["text"] for h in colh)
            return (tuple(h["text"] for h in rowh), tuple(h["text"] for h in colh),
                    rel, "positional")
        return (), (), (), "unresolved"

    def _aux_ctx(self, table_id, caption, text, resolution):
        """caption/stray の可視化単位用の最小 TableContext（束縛不可＝header_resolutionで弾く）。"""
        return TableContext(
            table_id=table_id, caption=caption, row_headers=(), column_headers=(),
            value_cell={"row": -1, "col": -1, "rowspan": 0, "colspan": 0, "text": text},
            related_cells=(), header_resolution=resolution)

    def finalize(self, table_id):
        """★最外表を占有グリッド化しヘッダー解決 → 各セルの (text, table_context, flags) を返す★。
        全ての非空セル＋caption＋strayを可視化（値の意味割れ検知＋caption/セル外値の迂回防止）。
        th/ネスト/malformed/解決不能/caption/strayはflag or resolutionで束縛側REVIEW。"""
        self._close_row()          # 最後の行/セルを確定
        caption = _collapse_ws("".join(self.caption_buf))
        grid = self._build_grid()
        all_cells = [cell for row in self.rows for cell in row if "_r" in cell]
        by_id = {}
        for cell in all_cells:
            if cell["cid"] and cell["cid"] not in by_id:
                by_id[cell["cid"]] = cell
        scoped = [h for h in all_cells
                  if h["kind"] == "th" and h["scope"] in ("row", "col", "rowgroup", "colgroup")]
        # ★scope付きth が過多なら header解決の O(セル×scoped) が実用CPU上限を超える → doc REVIEWへ昇格
        #   （実表のscope-thは高々数十・数百超は異常＝Codex再レビューCritical2）★
        if len(scoped) > _MAX_SCOPED_TH:
            self.limit_hit = True
            scoped = scoped[:_MAX_SCOPED_TH]
        out = []
        # ★caption を可視化単位に（値がcaptionにしか無い経路の迂回を塞ぐ・Codex Critical1）★
        if caption:
            out.append((caption, self._aux_ctx(table_id, caption, caption, "caption"),
                        ("caption_cell",)))
        # ★セル外/親セルなしネスト表の非空テキストも可視化（Codex Critical3）★
        stray = _collapse_ws("".join(self.stray_buf))
        if stray:
            out.append((stray, self._aux_ctx(table_id, caption, stray, "stray"),
                        ("table_stray",)))
        for cell in all_cells:
            text = cell["text"]
            if not text:
                continue           # 空セルはグリッド占有のみ（単位化しない）
            flags = []
            if self.malformed:
                flags.append("table_malformed")
            if cell["nested"]:
                flags.append("nested_table")
            if cell["kind"] == "th":
                rowh, colh, rel, resolution = (), (), (), "header_cell"
            else:
                rowh, colh, rel, resolution = self._resolve_headers(cell, grid, scoped, by_id)
            tctx = TableContext(
                table_id=table_id, caption=caption,
                row_headers=rowh, column_headers=colh,
                value_cell={"row": cell["_r"], "col": cell["_c"],
                            "rowspan": cell["_rs"], "colspan": cell["_cs"], "text": text},
                related_cells=rel, header_resolution=resolution)
            out.append((text, tctx, tuple(flags)))
        return out


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
        self._table_depth = 0     # table の入れ子深さ（最外＝1・collectorが単位化）
        self._table_collector = None   # 最外表を構造化する _TableCollector（表内のみ非None）
        self._table_id_seq = 0
        self._annot_depth = 0     # ruby注釈(rt/rp/rtc)の入れ子（フレームpush/popに同期・暗黙終了もpopで減る）
        self._vskip_depth = 0     # 可視だが構造抽出しないskip(svg/math/object等・_SHADOW_SKIP)の内側（content内のみ）
        self._hidden_skip_depth = 0   # 確実に非表示のskip(script/style等)の内側（可視skipの内側でもshadowにしない）
        self._vis_hidden_depth = 0    # hidden属性/inline display:none 等で表示されない要素の内側（unitにせずshadowへ）
        self._chrome_depth = 0    # nav/header/footer/aside/form等の非本文領域（shadow対象外＝誤検知回避）
        # ★可視テキストストリームの直前状態（sink=cap/shadow・末尾が数字か）＝unit↔shadow双方向の値分割検知用★
        self._last_char_digit = False
        self._last_sink = None    # 'cap' | 'shadow' | None（ブロック境界でNoneにリセット）
        # ★直前のdata以降に「要素境界(タグ)」があったか＝要素境界を跨いで数字が構成される候補の検知用（Codex第18次C2）。
        #   タグはCSS display で連結/分断が反転し得る（rendered DOM無しには決定不能）→ 数字隣接なら fail-closed でREVIEW。
        #   コメント/宣言/PI は不可視＝境界にしない（連続扱い）。
        self._saw_elem_since_data = False
        self._shadow = []         # ★構造化しなかった可視テキスト（隠れ値の照合用・content領域限定）★
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

    def _in_nonbody_region(self) -> bool:
        """明示的な非本文領域(nav/header/footer/aside/form/fieldset/address)の内側か。
        ここにある最外表は記事本文でない＝証拠にしない（collectorを起こさない）。それ以外の位置は
        構造誤り(ul>table・未知要素>table)でも可視化する（値漏れ防止・Codex再レビューCritical1）。"""
        return any(e["tag"] in _NONBODY_ANCESTORS for e in self._elems)

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

    def _visible_boundary(self, data, sink):
        """★可視テキストの境界での数字隣接を検知（値分割/要素境界跨ぎの構成・Codex第13次C1/第18次C2）★。
        直前の可視文字が数字 かつ 今回が数字始まり で、間に「sink変化」または「要素境界(タグ)」があれば、
        値が境界を跨いで構成/分割された疑い→doc REVIEW（CSS displayで連結/分断が反転し得るため fail-closed）。
        間がコメント/宣言/PI（不可視）だけなら境界にしない＝連続扱い（`9<!--c-->67`=967 は検知不要）。"""
        if not data:
            return
        if (self._last_char_digit and self._last_sink is not None and data[0].isdigit()
                and (self._last_sink != sink or self._saw_elem_since_data)):
            self._warn("opaque_in_digit_run")
        self._last_char_digit = data[-1].isdigit()
        self._last_sink = sink
        self._saw_elem_since_data = False

    def _shadow_add(self, data, affect_base_run=True):
        """★可視だが構造化しなかったテキストを shadow へ退避（隠れ値の照合用）★。
        ・affect_base_run=True: 基底の可視ストリームの一部（opaque/vskip）＝sink境界の数字隣接を検知。
        ・affect_base_run=False: ruby注釈(ふりがな)＝基底とは別ストリーム。shadowには保存するが基底の _last_* を汚さない
          （注釈を挟んだ cap↔shadow の値分割検知が壊れないように・Codex第15次C1）。
        ・断片数は上限（コメント分断DoS対策）。"""
        if affect_base_run:
            self._visible_boundary(data, "shadow")
        if len(self._shadow) >= _MAX_SHADOW_CHUNKS:
            self._warn("shadow_overflow")
            return
        self._shadow.append(data)

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

    def _reset_visible_run(self):
        """ブロック境界＝可視の数字runの終端。sink状態をリセット（跨ぎ誤検知/検知漏れ防止・Codex第12/13次）。
        ★skip(hidden/visible)の内側 or ruby注釈(rt/rp/rtc)の内側の境界は「基底の可視ストリーム」に境界を作らない
          ＝resetしない（非表示/注釈の境界で cap↔shadow の値分割検知を回避されるのを防ぐ・Codex第14/15次Critical）★。"""
        if self._skip_depth > 0 or self._annot_depth > 0:
            return
        self._last_char_digit = False
        self._last_sink = None

    def _softbreak(self):
        """br/hr は文境界。見出し中は空白（AB連結を防ぐ・Codex #6）。"""
        self._reset_visible_run()           # 改行境界＝数字runの終端（値分割検知のリセット）
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
        if e.get("chrome"):
            self._chrome_depth = max(0, self._chrome_depth - 1)
        if e.get("vishidden"):
            self._vis_hidden_depth = max(0, self._vis_hidden_depth - 1)
        if e["tag"] in _SKIP_CONTENT_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            if e.get("vskip"):
                self._vskip_depth = max(0, self._vskip_depth - 1)
            if e.get("hskip"):
                self._hidden_skip_depth = max(0, self._hidden_skip_depth - 1)
        if e.get("is_annotation"):            # ★注釈フレームの終了はフラグで判定（明示/暗黙/兄弟暗黙とも pop 経由・sync）★
            self._annot_depth = max(0, self._annot_depth - 1)
        if e["tag"] in _TABLE_TAGS:
            self._table_depth = max(0, self._table_depth - 1)
            # ★表フレームの pop（明示 </table> も祖先閉じによる暗黙終了も同じ経路）で collector を進める。
            #   ★close は「open したフレーム(collector_opened)」だけ＝skip/annot抑止で未openの表popは無視（対称）★。
            if self._table_collector is not None and e.get("collector_opened"):
                self._table_collector.close_table()
                if self._table_collector.depth == 0:
                    self._finalize_table_collector()
        self._emit(e)
        return e

    def _finalize_table_collector(self):
        """最外表を単位化して self.units へ追加（unit_id/dom_order/上限/truncationを親が付与）。"""
        coll = self._table_collector
        self._table_collector = None
        if self._stopped:
            return
        self._table_id_seq += 1
        units_data = coll.finalize(self._table_id_seq)
        # ★上限超過でセルを捨てた表は doc 全体を REVIEW へ昇格（捨てた値が本文側同値で迂回PASSするのを防ぐ）★
        if coll.limit_hit:
            self._warn("table_limit")
        for text, tctx, flags in units_data:
            f = list(flags)
            if len(text) > _MAX_UNIT_TEXT:
                text = text[:_MAX_UNIT_TEXT]
                f.append("truncated")
                self._warn("unit_truncated")
                tctx = tctx._replace(
                    value_cell=dict(tctx.value_cell, text=text))
            if len(self.units) >= _MAX_PARSE_UNITS:
                self._stop("too_many_units")
                return
            self._dom_order += 1
            self.units.append(StructuredUnit(
                unit_id=len(self.units), kind="table_value_cell", text=text,
                heading_path=coll.heading_path, dom_order=self._dom_order,
                source_path=coll.source_path, table_context=tctx, parse_flags=tuple(f)))

    def _pop_emit_to(self, idx):
        """top から idx（含む）まで pop して emit（暗黙終了/交差の巻き戻し）。root(0)は残す。"""
        while len(self._elems) > max(idx, 1):
            self._pop_frame()

    def _boundary_open(self):
        """ブロック境界を開く直前: liなら区切り挿入（原子性維持）、段落なら現在のrunを確定。"""
        self._reset_visible_run()           # ブロック境界（開始）＝数字runの終端（値分割検知のリセット・Codex第12次C2）
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
        if tag in _BOUNDARY_TAGS:           # ★ブロック境界(終了)＝数字runの終端（終了側リセット漏れ・Codex第13次Major）★
            self._reset_visible_run()
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
        self._saw_elem_since_data = True       # 要素境界（タグ）＝CSSで連結/分断が反転し得る点（第18次C2）
        self._nodes += 1
        if self._nodes > _MAX_PARSE_NODES:
            self._stop("too_many_nodes")
        if len(tag) > _MAX_TAG_LEN:            # 巨大タグ名（source_path肥大）は異常停止（Codex#4）
            self._stop("long_tag")
        if attrs is not None and len(attrs) > _MAX_ATTRS:   # 巨大属性数の辞書化増幅を止める（Codex第13次DoS）
            self._stop("too_many_attrs")
        # ★表collectorへ開始タグの境界を通知（void/未知含む・数字連結の値漏れ防止・Codex第7次）★
        #   table自身は open_table 経由。skip要素の開始タグは skip_depth==0 時に届き境界化（内部子タグは除外）。
        #   ★ruby注釈内(annot_depth>0)の子タグ境界は collector に影響させない＝基底テキストを分断しない（Codex第10次C1）★。
        if (self._table_collector is not None and self._skip_depth == 0
                and self._annot_depth == 0 and tag != "table"):
            self._table_collector.mark_boundary(tag)
        # ★外来コンテンツ(svg/math)からの HTML breakout（void判定より前・Codex増分A#17）★
        if tag in _BREAKOUT_TAGS or (
                tag == "font" and any(a.lower() in ("color", "face", "size")
                                      for a, _v in (attrs or []))):
            self._breakout_foreign()
        if tag in _VOID_TAGS:
            if tag in ("br", "hr"):
                self._softbreak()              # 表内の br/hr 境界は上の mark_boundary が区切る
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
        if tag in _OBSOLETE_RAWTEXT and content_here:
            self._warn("obsolete_raw_text")   # xmp/plaintext等＝parser-browser乖離→doc REVIEW（Codex第11次C4）
        # ★hidden属性 / inline style の display:none・visibility:hidden ＝表示されない要素（Codex第19/20次Critical）★
        #   その内容は unit（正の証拠）にせず shadow へ回す＝表示されていない値を自動公開しない。
        #   style は有界CSS宣言パーサで解釈（コメント/文字列/エスケープ/!important/後勝ち）。解釈不能はREVIEW。
        if attrs:
            _vis_hidden = False
            for _an, _av in attrs:
                _n = (_an or "").lower()
                if _n == "hidden":
                    _vis_hidden = True
                    break
                if _n == "style" and _av:
                    _h, _ok = _style_hides(_av)
                    if not _ok:
                        self._warn("style_unparseable")   # 解釈不能なstyle＝表示状態不明→doc REVIEW
                    elif _h:
                        _vis_hidden = True
                        break
            if _vis_hidden:
                frame["vishidden"] = True
                self._vis_hidden_depth += 1
        if tag in _NONBODY_ANCESTORS:         # nav/header/footer/aside/form等＝chrome（shadow対象外）
            frame["chrome"] = True
            self._chrome_depth += 1
        if tag in _SKIP_CONTENT_TAGS:
            self._skip_depth += 1
            if tag in _HIDDEN_SKIP:           # 確実に非表示（script/style等）＝可視skipの内側でもshadowにしない
                frame["hskip"] = True
                self._hidden_skip_depth += 1
            # ★可視skip（svg/object/textarea等）は content領域だけでなく ruby注釈内でも可視＝shadowへ追跡
            #   （注釈内は _permits で content_here=False になるが、furigana注釈として表示され得る・Codex第17次C1）★
            elif tag in _SHADOW_SKIP and (content_here or self._annot_depth > 0):
                frame["vskip"] = True
                self._vskip_depth += 1
        # ★ruby注釈は「実親が正当（rt/rp→ruby|rtc, rtc→ruby）」なフレームだけ＝孤立/誤配置は内容を表す（Codex第10次C2）★
        if tag in _ANNOTATION_TAGS:
            parent_tag = self._elems[-2]["tag"] if len(self._elems) >= 2 else ""
            if parent_tag in _ANNOT_PARENTS[tag]:
                frame["is_annotation"] = True     # pop 時はこのフラグで減らす（tag名でなく＝暗黙終了もsync）
                self._annot_depth += 1
        if tag in _TABLE_TAGS:
            # ★最外表は「明示的な非本文領域(nav/header/footer/aside/form/fieldset/address)」または script等の
            #   抑止内・ruby注釈内でなければ collector を起こす（content_here=False の構造誤り位置も可視化）。
            if (self._table_depth == 0 and self._skip_depth == 0 and self._annot_depth == 0
                    and not self._in_nonbody_region()):
                self._table_collector = _TableCollector(
                    heading_path=tuple(self._outline),
                    source_path=frame["source_path"])
            self._table_depth += 1
            # ★open は skip/annot 抑止内では呼ばない。close は「open したフレーム」だけ（collector_opened）＝対称★
            opened = False
            if (self._table_collector is not None and self._skip_depth == 0
                    and self._annot_depth == 0):
                self._table_collector.open_table()
                opened = True
            frame["collector_opened"] = opened
        # ★表内の構造を collector へ転送（最外 <table> 自身は open_table 済＝除外・ruby注釈内は除外）★
        elif (self._table_collector is not None and self._skip_depth == 0
              and self._annot_depth == 0):
            self._table_collector.starttag(tag, attrs)

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
        self._saw_elem_since_data = True                # 要素境界（タグ）＝CSSで連結/分断が反転し得る点（第18次C2）
        self._nodes += 1                                # ★終了タグもイベント計上（上限迂回防止・Codex#4）
        if self._nodes > _MAX_PARSE_NODES:
            self._stop("too_many_nodes")
        if len(tag) > _MAX_TAG_LEN:                     # 終了タグ名も長さ上限（全経路で保証・Codex#4）
            self._stop("long_tag")
        # ★表collectorへ終了タグの境界を通知（skip内部/ruby注釈内の子タグは除外・Codex第7/8/10次）★
        if (self._table_collector is not None and self._skip_depth == 0
                and self._annot_depth == 0 and tag != "table"):
            self._table_collector.mark_boundary(tag)
        # ★外来コンテンツ内の終了タグ </p> </br> も breakout（HTML5・Codex増分A#18）★
        if tag in ("p", "br") and self._breakout_foreign_needed():
            self._breakout_foreign()
            if tag == "br":
                self._softbreak()                       # </br> は <br> 相当
                return
            # </p> は breakout 後、下の通常処理（開いたpを閉じる/無ければ孤立終了）へ
        if tag in _VOID_TAGS:
            return
        # ★表構造の終了を collector へ（セル/行の確定。</table>除外・ruby注釈内除外＝depth/finalizeは frame pop）★
        if (self._table_collector is not None and self._skip_depth == 0
                and self._annot_depth == 0 and tag != "table"):
            self._table_collector.endtag(tag)
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
        if self._stopped:
            return
        self._chars += len(data)
        if self._chars > _MAX_TOTAL_TEXT:               # 累積テキスト上限（メモリDoS・Codex #5）
            self._stop("text_limit")
            return
        if self._skip_depth > 0:                        # script/style等の内側は無視。ただし
            # ★可視skip(svg/object等)の内側だが、確実に非表示のskip(script/style)の内側でなければshadowへ（Codex第12次Major）★
            #   確実に非表示(hidden skip)は可視ストリームでないので _last_* を触らない（scriptを跨ぐ数字は連続扱い）。
            #   注釈(rt/rp/rtc)内の可視skipは基底runを汚さない（affect_base_run=False・Codex第17次C1）。
            if self._vskip_depth > 0 and self._hidden_skip_depth == 0 and self._chrome_depth == 0:
                self._shadow_add(data, affect_base_run=(self._annot_depth == 0))
            return
        # ★hidden属性/inline display:none 等で表示されない要素の内容は「正の証拠(unit)」にしない＝shadowへ★
        #   （表示されていない値を自動公開しない・値照合はされるのでVALUE_IN_SHADOWでREVIEW・Codex第19次Critical）
        if self._vis_hidden_depth > 0:
            if self._chrome_depth == 0:
                self._shadow_add(data, affect_base_run=False)   # 非表示＝可視ストリームに参加しない
            return
        # ★表内（collector活性）は cell/caption へ。非本文表(collector None)は下の capture が content=Falseで停止★
        #   ruby注釈(rt/rp/rtc)内のテキストは基底でない＝shadowへ退避（可視の読み仮名・値照合の対象・Codex第9/11次）。
        if self._table_collector is not None:
            if self._annot_depth == 0:
                # ★表セルの基底テキストも可視run追跡に参加（cap↔shadow=vskipの値分割を検知・Codex第16次C1）。
                #   セル/行/ブロック境界では _boundary_open/_boundary_close_sep が _reset_visible_run 済み。
                self._visible_boundary(data, "cap")
                self._table_collector.data(data)
            else:
                self._shadow_add(data, affect_base_run=False)   # ruby注釈は基底ストリームを汚さない
            return
        if self._annot_depth > 0:                       # 段落内の ruby注釈テキスト＝基底でない→shadowへ
            if self._chrome_depth == 0:                 # 非本文領域(nav等)の注釈は chrome＝shadowにしない
                self._shadow_add(data, affect_base_run=False)
            return
        a = self._capturing_ancestor()
        if a is None:
            # ★content領域(chrome外)で落ちた可視テキスト＝opaque(meter/未知/opaque子孫のcontent=False)→shadow★
            if self._chrome_depth == 0:
                self._shadow_add(data)
            return
        self._visible_boundary(data, "cap")             # unit↔shadow 跨ぎの数字分割を検知（sink=cap）
        if not a["atomic"] and not a["is_heading"] and not a["buf"]:
            a["heading_path"] = tuple(self._outline)    # run開始時点の見出しパスに更新
        a["buf"].append(data)

    def _count_event(self):
        # markup イベントを計上（コメント/宣言/PI の大量分断での node予算迂回を防ぐ・Codex第12/13次DoS）。
        self._nodes += 1
        if self._nodes > _MAX_PARSE_NODES:
            self._stop("too_many_nodes")

    def handle_comment(self, data):
        self._count_event()   # コメント内の数値は証拠にしない。イベントだけ計上。

    def handle_decl(self, decl):
        self._count_event()   # <!DOCTYPE …> 等（大量宣言での予算迂回を防ぐ）

    def handle_pi(self, data):
        self._count_event()   # <? … > 処理命令

    def unknown_decl(self, data):
        self._count_event()   # <![ … ]> 等


def parse_structured_units(html_text):
    """HTMLを構造化し (units, h1_candidates, parse_warnings, shadow_text) を返す（表以外・D-1a-2-3）。
    標準ライブラリのみ・失敗/上限超過/構造異常は parse_warnings に集約（束縛側でREVIEW）。
    shadow_text＝構造化しなかった可視テキスト（svg/math/object/meter/ruby注釈/未知要素…）＝隠れ値の照合用。"""
    if not isinstance(html_text, str):
        return (), (), ("html_not_str",), ""
    p = _StructureParser()
    if len(html_text) > _MAX_HTML_LEN:            # feed前の入力長上限（直接呼び対策・Codex#4）
        p._warn("html_too_long")
        html_text = html_text[:_MAX_HTML_LEN]
    # ★feed前に巨大な数値文字参照(&#…;)を検出＝HTMLParserの int() 復号による整数化DoSを防ぐ（Codex第4次）★
    #   検出したら feed せず文書ごとREVIEW（Pythonの整数文字列桁数制限に依存しない安全弁）。
    if _has_overlong_numeric_charref(html_text):
        p._warn("numeric_charref_too_long")
        return tuple(p.units), tuple(p.h1_candidates), tuple(p.warnings), ""
    # ★feed前に「異常に長い単一タグ」を検出＝巨大属性列でHTMLParserがメモリ増幅する前に止める（Codex第13次DoS）★
    if _has_overlong_tag(html_text):
        p._warn("overlong_tag")
        return tuple(p.units), tuple(p.h1_candidates), tuple(p.warnings), ""
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
    # ★shadow は無区切りで連結＝コメント/インライン分断で分かれた可視値(9<!--c-->67=967)も検出（Codex第12次C2）。
    #   断片を跨いだ過剰検出はあり得るが安全側（誤PASSでなく誤REVIEW＝許容）。
    shadow = "".join(p._shadow)
    return tuple(p.units), tuple(p.h1_candidates), warns, shadow


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
    units, h1_candidates, parse_warnings, shadow_text = parse_structured_units(hs.html)
    return DocumentSnapshot(
        final_url=hs.final_url,
        title=hs.title,
        h1_candidates=h1_candidates,
        response_sha256=None,
        html_sha256=hs.html_sha256,
        rendered_text_sha256=rendered_sha,
        parse_warnings=parse_warnings,
        units=units,
        shadow_text=shadow_text,
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
                    fetch_fn=None, *, expect_item_key, rendered_attestation=None,
                    attest_fn=None):
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
    # ★取得HTMLに巨大な数値文字参照(&#…;)があれば処理前にREVIEW（平坦化 html.unescape も構造化 html.parser も
    #   int() 復号で整数化DoSになるため・Codex第4次）。verify_claims本体は不変＝この関所で消費経路を塞ぐ。★
    if _has_overlong_numeric_charref(snapshot.html or ""):
        return _src(cval, "", False, REVIEW, "CHARREF_DOS", field_key, raw)
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
    # ★shadow（構造化しなかった可視テキスト）に値が出る＝隠れた別文脈の可能性→REVIEW（段落/表 共通・Codex第11次）★
    #   svg/math text・object等fallback・meter/未知カスタム要素・ruby注釈 の値を無警告で消して本文PASSさせない。
    #   content領域限定のshadowなので nav/footer の同値では発火しない（誤検知回避）。
    if doc.shadow_text and _unit_contains_value(doc.shadow_text, raw):
        return _src(cval, dom, False, REVIEW, "VALUE_IN_SHADOW", field_key, raw, snapshot=snap)
    if not value_units:
        return _src(cval, dom, False, REVIEW, "VALUE_NOT_ON_PAGE", field_key, raw, snapshot=snap)
    # ★D-1a-2-4 増分A: 表に触れる値は全てREVIEW（占有グリッド化＝値は可視だが、表束縛=合成文脈/ヘッダー継承は
    #   増分Bで実装）。値がテーブルセルにも出るなら意味割れの疑いを含め fail-closed に止める。★
    if any(u.kind == "table_value_cell" for u in value_units):
        return _src(cval, dom, False, REVIEW, "TABLE_BINDING_PENDING", field_key, raw, snapshot=snap)
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
    # ★★最終ゲート: rendered 監査証跡が無い/不一致なら verified=True にしない（Codex第21次・機械的強制）★★
    #   静的解析は外部/埋込CSS・var()等の非表示を判定できないため、実表示の確認を必須にする（fail-closed）。
    #   ★attest_fn は「C0のblocked/allowlist検査＋取得＋最終URL再検査を通過した後」にだけ呼ぶ（SSRF/DoS防止・
    #     Codex第22次C3）。検証済みの final_url を渡す（生の入力URLをブラウザに渡さない）。★
    att = rendered_attestation
    if att is None and attest_fn is not None:
        try:
            att = attest_fn(page.final_url)
        except Exception:
            att = None                     # 証跡取得失敗＝証跡なし扱い（REVIEW）
    ok_att, att_code = _attestation_ok(att, doc.final_url, doc.html_sha256, raw, nids)
    if not ok_att:
        return _src(cval, dom, False, REVIEW, att_code, field_key, raw, snapshot=snap)
    # 返す value は evidence 側（検証器は値を書き換えない・cvalと一致確認済み）
    return _src(ev_val, dom, True, PASS, "OK", field_key, raw, snapshot=snap)


def make_verifier(field_key, identity, allowed_domains=None, fetch_fn=None, attest_fn=None):
    """resolve_with_dialogue に渡す verify(item_key, evidence) を作る（field_key/identityを束縛）。
    ★resolveへ渡す item_key は anomaly_key_of(field_key)。verify内で item_key の一致を強制★
    attest_fn(final_url) → rendered監査証跡 dict（Playwright等で実描画後の可視テキスト）。★未指定なら証跡なし＝
    verified=True には到達しない（fail-closed・Codex第21次の機械的ゲート）★。
    ★attest_fn は verify_evidence 内で「URL安全検査＋取得＋最終URL再検査の後」に呼ばれる（SSRF防止・第22次C3）。"""
    def verify(item_key, evidence):
        return verify_evidence(field_key, evidence, identity, allowed_domains,
                               fetch_fn, expect_item_key=item_key, attest_fn=attest_fn)
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

    def mock_att(url):
        """テスト用の rendered 監査証跡（本番は Playwright の実描画テキスト）。
        モックでは平坦化テキストを『実表示テキスト』の代用にし、同一ページ束縛(final_url/html_sha256)を張る。"""
        snap = vc._html_cache.get(url)
        if snap is None:
            return None
        # 本番の取得層と同じ関所（巨大数値文字参照は unescape 前に拒否＝整数化DoS防止）
        if vc._has_overlong_numeric_charref(snap.html or ""):
            return None
        return {"final_url": snap.final_url, "html_sha256": snap.html_sha256,
                "visible_text": vc._flatten_html(snap.html)}

    def V(field_key, evidence, identity, **kw):
        # ★expect_item_key を自動付与（本番は make_verifier が付与・テストの利便）
        kw.setdefault("expect_item_key", anomaly_key_of(field_key))
        # ★rendered監査証跡を自動付与（本番は attest_fn から供給）。証跡ゲート自体は専用テストで検証する。
        if "rendered_attestation" not in kw:
            u = evidence.get("url") if isinstance(evidence, dict) else None
            kw["rendered_attestation"] = mock_att(u)
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
    verify_kw = make_verifier("ceiling.normal.at", IDENT, attest_fn=mock_att)
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
        return parse_structured_units(html)[:3]   # 既存テストは (units, h1, warnings) の3値で受ける

    def sh(html):
        return parse_structured_units(html)[3]    # shadow_text（構造化しなかった可視テキスト）

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
    tcells = [x for x in u if x.kind == "table_value_cell"]
    t("構造: 表セルは table_value_cell 単位になる（D-1a-2-4A・外段落＋表内セル両方）",
      "500" in joined and "600" in joined
      and len(tcells) == 1 and tcells[0].text == "表内は600G")

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
    # ★第11次C2: ruby は透過＝基底テキストを捕捉する（表示される本文・注釈rt/rp/rtcは別途ドロップ）
    u, _, _ = pu("<ruby>機種Aの通常時天井は967G</ruby>")   # ruby基底（rt無し）＝そのまま捕捉
    t("構造: ruby 基底テキストは捕捉する（透過・第11次C2）",
      any("967" in x.text for x in u))
    u, _, _ = pu("<ruby>機種Aの通常時天井は9<rt>きゅう</rt>67G</ruby>")
    t("構造: ruby 基底を連結抽出し rt注釈『きゅう』はドロップ（第11次C2）",
      any("967" in x.text for x in u) and all("きゅう" not in x.text for x in u))
    u, _, w = pu("<dl><div>機種Aの通常時天井は967G</div></dl>")   # dl>div ラッパーの直下テキスト
    t("構造: dl>div ラッパーの直下テキストは証拠にしない（非capture wrapper・Codex増分A#3）",
      all("967" not in x.text for x in u))

    # ── ★Codex増分A第10次指摘（非content領域に正当リスト構造を挟む迂回・content伝播で塞ぐ）★ ──
    # 非本文領域(nav/header/form)配下は正当なリスト構造でも本文到達不可＝値漏れなし・静かにスキップ。
    #   （ruby は第11次で透過＝本文なので除外＝基底は捕捉する）
    for html in ("<nav><ul><li><p>機種Aの通常時天井は967G</p></li></ul></nav>",
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
    #   ※map は第10次で skip から除外＝透過コンテンツとして捕捉するので、この抑止ループには含めない。
    for tag in ("select", "iframe", "textarea", "title", "canvas", "object",
                "audio", "video", "picture"):
        u, _, w = pu("<%s/>機種Aの通常時天井は967G" % tag)
        t("構造: <%s/>自己終了は開いたまま＝後続テキストが本文に漏れない（Codex増分A#13）" % tag,
          all("967" not in x.text for x in u))
    # ★<table/>自己終了後のテキストは stray 可視化単位に入る（本文には漏れない・flag付き＝束縛側REVIEW）★
    #   ＝Codex Critical3対応で silently drop しない（本文の paragraph/list には決して混ざらない）。
    u, _, w = pu("<table/>機種Aの通常時天井は967G")
    t("構造: <table/>後のテキストは stray 可視化単位（本文paragraph/listには漏れない・table_stray flag）",
      all(x.kind == "table_value_cell" for x in u if "967" in x.text)
      and any("967" in x.text and "table_stray" in x.parse_flags for x in u))
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

    # 表内のセル外テキスト（td/th の外）は stray 可視化単位に入れる（silently drop しない・Codex Critical3）。
    u, _, w = pu("<table><div>表内600G</table>")
    tcs = [x for x in u if x.kind == "table_value_cell"]
    t("構造: 表内セル外テキストは stray 可視化単位（本文には漏れない・table_stray flag）",
      any("600" in x.text and "table_stray" in x.parse_flags for x in tcs)
      and all(x.kind == "table_value_cell" for x in u if "600" in x.text))

    _, h1s, _ = pu("<h1>A<br>B</h1>")
    t("構造: 見出し内brは空白（ABに連結しない・Codex#6）", h1s == ("A B",))

    _, _, w = pu("<div>朝一300G")
    t("構造: EOF未閉じの省略不可タグは unclosed_tags（Codex#4）", "unclosed_tags" in w)

    _, _, w = pu("<p>" + ("天井は967G。" * 300000) + "</p>")
    t("構造: 巨大単一段落は text_limit で停止（メモリDoS・Codex#5）", "text_limit" in w)
    _, _, w = pu("<p>" + (";" * (_MAX_TOTAL_TEXT + 5)) + "</p>")
    t("構造: 区切り大量の巨大単一ノードも text_limit で停止", "text_limit" in w)

    # ══════════════════════════════════════════════════════════════
    # ★D-1a-2-4 増分A: 表の占有グリッド構造化（ヘッダー解決・rowspan/colspan・ネスト・malformed）★
    #   この段では table_value_cell を単位化して可視化するだけ（束縛=合成文脈/ヘッダー継承は増分B）。
    #   束縛側は「表に触れる値は全てREVIEW(TABLE_BINDING_PENDING)」＝安全。
    # ══════════════════════════════════════════════════════════════
    def tcells(html):
        u = pu(html)[0]
        return [x for x in u if x.kind == "table_value_cell"]

    def find_cell(html, needle):
        for x in tcells(html):
            if needle in x.text:
                return x
        return None

    # ── 占有グリッド: rowspan 継承（上の行の行ヘッダーが後続行の同列に継承）──
    H = ("<table><tr><th rowspan=2>通常</th><td>A967</td></tr>"
         "<tr><td>B745</td></tr></table>")
    ca, cb = find_cell(H, "A967"), find_cell(H, "B745")
    t("表: rowspan で行ヘッダー『通常』が後続行の値セルに継承される（占有グリッド）",
      ca is not None and cb is not None
      and ca.table_context.row_headers == ("通常",)
      and cb.table_context.row_headers == ("通常",)
      and ca.table_context.header_resolution == "positional")

    # ── colspan: 上の th が複数列を占有 → 各列の値セルの列ヘッダーになる ──
    H = ("<table><tr><th colspan=2>スペック</th></tr>"
         "<tr><td>天井</td><td>967</td></tr></table>")
    c = find_cell(H, "967")
    t("表: colspan の列ヘッダー『スペック』が下段の各列値セルに適用される",
      c is not None and "スペック" in c.table_context.column_headers
      and c.table_context.value_cell["col"] == 1)

    # ── 複数段の列ヘッダー（thead 2行）──
    H = ("<table><thead><tr><th colspan=2>天井</th></tr>"
         "<tr><th>通常</th><th>リセット</th></tr></thead>"
         "<tbody><tr><td>967</td><td>745</td></tr></tbody></table>")
    c967, c745 = find_cell(H, "967"), find_cell(H, "745")
    t("表: 複数段の列ヘッダー（天井→通常/リセット）が値セルに順に積まれる",
      c967 is not None and c967.table_context.column_headers == ("天井", "通常")
      and c745 is not None and c745.table_context.column_headers == ("天井", "リセット"))

    # ── th scope=row/col（明示スコープ）──
    H = ("<table><tr><td></td><th scope=col>通常</th><th scope=col>リセット</th></tr>"
         "<tr><th scope=row>天井</th><td>967</td><td>745</td></tr></table>")
    c = find_cell(H, "967")
    t("表: th scope=row/col の明示スコープでヘッダー解決（resolution=scope）",
      c is not None and c.table_context.header_resolution == "scope"
      and c.table_context.row_headers == ("天井",)
      and c.table_context.column_headers == ("通常",))

    # ── headers属性＋id（明示関連付け・優先度①）──
    H = '<table><tr><th id=h1>通常</th><td headers="h1">967</td></tr></table>'
    c = find_cell(H, "967")
    t("表: headers属性→対応id で明示解決（resolution=explicit）",
      c is not None and c.table_context.header_resolution == "explicit"
      and "通常" in c.table_context.related_cells)
    # 不正/欠落idを指す headers属性 → ambiguous（束縛側REVIEW）
    H = '<table><tr><th id=h1>通常</th><td headers="nope">967</td></tr></table>'
    c = find_cell(H, "967")
    t("表: headers属性が欠落idを指す → header_resolution=ambiguous",
      c is not None and c.table_context.header_resolution == "ambiguous")

    # ── td内 strong/b は自動ヘッダーにしない（設計④・th無しは unresolved）──
    H = "<table><tr><td><strong>通常</strong></td><td>967</td></tr></table>"
    c = find_cell(H, "967")
    t("表: th無し（td太字のみ）は header_resolution=unresolved（td太字を自動ヘッダーにしない）",
      c is not None and c.table_context.header_resolution == "unresolved"
      and c.table_context.row_headers == () and c.table_context.column_headers == ())

    # ── th セル自身に値 → header_cell（束縛対象外・可視化のみ）──
    H = "<table><tr><th>天井967</th></tr></table>"
    c = find_cell(H, "967")
    t("表: th セルの値は header_cell フラグ（可視化のみ・束縛側REVIEW）",
      c is not None and c.table_context.header_resolution == "header_cell")

    # ── caption 捕捉 ──
    H = "<table><caption>設定示唆まとめ</caption><tr><th>天井</th><td>967</td></tr></table>"
    c = find_cell(H, "967")
    t("表: <caption> が table_context.caption に入る",
      c is not None and c.table_context.caption == "設定示唆まとめ")

    # ── ネスト表: 親セルに nested_table フラグ・内側テキストは親に混ざるが flag でREVIEW ──
    H = "<table><tr><td>親967<table><tr><td>子745</td></tr></table></td></tr></table>"
    cs = tcells(H)
    parent = next((x for x in cs if "967" in x.text), None)
    t("表: ネスト表の親セルに nested_table フラグ（内側は別処理せず親をREVIEW）",
      parent is not None and "nested_table" in parent.parse_flags
      and "745" in parent.text)   # 内側テキストは可視（意味割れ検知のため）だが flag 付き

    # ── malformed: rowspan=0 / 非整数span / 巨大span → table_malformed（表全体REVIEW）──
    for H, why in (
        ("<table><tr><td rowspan=0>x967</td></tr></table>", "rowspan=0"),
        ("<table><tr><td colspan=abc>x967</td></tr></table>", "非整数colspan"),
        ("<table><tr><td colspan=9999>x967</td></tr></table>", "巨大colspan"),
    ):
        c = find_cell(H, "967")
        t("表: %s は table_malformed フラグ（表全体REVIEW・値は可視）" % why,
          c is not None and "table_malformed" in c.parse_flags)

    # ── 非本文表（nav配下）は collector を起こさない＝証拠にしない ──
    t("表: nav 配下の表は単位化されない（非本文＝値を証拠にしない）",
      tcells("<nav><table><tr><td>ナビ967</td></tr></table></nav>") == [])

    # ── 暗黙終了: 祖先ブロックが未閉じ表を閉じても finalize される ──
    cs = tcells("<div><table><tr><td>暗黙967</td></div><p>後続</p>")
    t("表: 祖先ブロックの終了で未閉じ表も finalize（暗黙 </table>）",
      any("967" in x.text for x in cs))
    # ── EOF未閉じ表も finalize ──
    cs = tcells("<table><tr><td>EOF967</td>")
    t("表: EOF未閉じ表も finalize される",
      any("967" in x.text for x in cs))

    # ── 表内 script/comment の値は捕捉しない ──
    cs = tcells("<table><tr><td>本文967<script>var x='隠し888'</script>"
                "<!-- 隠し777 --></td></tr></table>")
    t("表: 表セル内の script/comment の数値は混ざらない",
      any("967" in x.text for x in cs)
      and all("888" not in x.text and "777" not in x.text for x in cs))

    # ── ★束縛ゲート（増分A）: 表に触れる値は全てREVIEW(TABLE_BINDING_PENDING) ★ ──
    mock_page("https://chonborista.com/tbl",
              "<h1>アクダマ</h1><table><tr><th>通常</th><td>アクダマの天井967G</td></tr></table>",
              title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/tbl",
                             "アクダマの天井967G", raw="967"), IDENT)
    t("★増分A: 値が表セルにある → REVIEW(TABLE_BINDING_PENDING・束縛は増分B)",
      r["verified"] is False and r["c5_code"] == "TABLE_BINDING_PENDING")
    # 段落＋表の両方に同値 → 表ゲートで REVIEW（段落単独PASSにしない・fail-closed）
    mock_page("https://chonborista.com/tbl2",
              "<h1>アクダマ</h1><p>アクダマの通常時の天井は967G</p>"
              "<table><tr><th>参考</th><td>967G</td></tr></table>",
              title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/tbl2",
                             "アクダマの通常時の天井は967G", raw="967"), IDENT)
    t("★増分A: 段落＋表に同値 → 表ゲートでREVIEW（段落単独PASSにしない）",
      r["verified"] is False and r["c5_code"] == "TABLE_BINDING_PENDING")

    # ══════════════════════════════════════════════════════════════
    # ★Codex Critical指摘（増分A第1次レビュー）＝「単位化されない表由来の値」が本文側同値で迂回PASSする穴★
    #   全て「本文にPASS可能な967＋表由来の別文脈967」で REVIEW に倒ることを確認（誤PASSの迂回封鎖）。
    # ══════════════════════════════════════════════════════════════
    P967 = "アクダマの通常時の天井は967G"       # 単独ならPASSし得る本文段落
    # (1) caption 内の値 → caption 可視化単位で表ゲート発火（Critical1）
    mock_page("https://chonborista.com/byp-cap",
              "<h1>アクダマ</h1><p>" + P967 + "</p>"
              "<table><caption>バベル 天井967G</caption><tr><td>参考</td></tr></table>",
              title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/byp-cap", P967, raw="967"), IDENT)
    t("★Critical1: 本文967＋caption内別文脈967 → REVIEW（caption可視化で迂回封鎖）",
      r["verified"] is False and r["c5_code"] == "TABLE_BINDING_PENDING")
    # (2) 501行目の値（行上限で捨てる）→ table_limit を doc全体REVIEWへ昇格（Critical2）
    big_rows = "".join("<tr><td>r%d</td></tr>" % i for i in range(500)) + "<tr><td>バベル967</td></tr>"
    _, _, wlim = pu("<table>" + big_rows + "</table>")
    mock_page("https://chonborista.com/byp-row",
              "<h1>アクダマ</h1><p>" + P967 + "</p><table>" + big_rows + "</table>",
              title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/byp-row", P967, raw="967"), IDENT)
    t("★Critical2: 本文967＋501行目別文脈967 → REVIEW（table_limitをdoc全体へ昇格）",
      "table_limit" in wlim and r["verified"] is False and r["c5_code"] == "PARSE_WARNINGS")
    # (3) table 直下（セル外）テキストの値 → stray 可視化単位で表ゲート発火（Critical3）
    mock_page("https://chonborista.com/byp-stray",
              "<h1>アクダマ</h1><p>" + P967 + "</p>"
              "<table>バベルの天井967G<tr><td>x</td></tr></table>",
              title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/byp-stray", P967, raw="967"), IDENT)
    t("★Critical3: 本文967＋table直下テキスト967 → REVIEW（stray可視化で迂回封鎖）",
      r["verified"] is False and r["c5_code"] == "TABLE_BINDING_PENDING")
    # (4) 親セルの無いネスト表の値 → stray 可視化単位で表ゲート発火（Critical3）
    mock_page("https://chonborista.com/byp-nest",
              "<h1>アクダマ</h1><p>" + P967 + "</p>"
              "<table><tr><td>親</td></tr><table><tr><td>バベルの天井967G</td></tr></table></table>",
              title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/byp-nest", P967, raw="967"), IDENT)
    t("★Critical3: 本文967＋親セルなしネスト表967 → REVIEW（stray可視化で迂回封鎖）",
      r["verified"] is False and r["c5_code"] == "TABLE_BINDING_PENDING")
    # (5) 占有セル爆発（colspan/rowspan巨大）→ 上限内で停止＋table_limitでdoc REVIEW（Critical4・DoS有界）
    _, _, wboom = pu("<table><tr><td colspan=100 rowspan=100>アクダマ967</td></tr></table>")
    t("★Critical4: 占有セル爆発は上限内で停止し table_limit（DoS有界・doc REVIEW）",
      "table_limit" in wboom)

    # ══════════════════════════════════════════════════════════════
    # ★Codex再レビューCritical（増分A第2次）＝collector非起動表による値の可視化漏れ＋DoS上限★
    # ══════════════════════════════════════════════════════════════
    # (6) ul>table（content_here=False の構造誤り位置）の別機種値 → 可視化して迂回封鎖（Critical1）
    us = tcells("<ul><table><tr><td>バベルの天井967G</td></tr></table></ul>")
    t("★再C1: ul>table（構造誤り位置）の値も table_value_cell に可視化される（無警告ドロップしない）",
      any("967" in x.text for x in us))
    mock_page("https://chonborista.com/ul-tbl",
              "<h1>アクダマ</h1><p>" + P967 + "</p>"
              "<ul><table><tr><td>バベルの天井967G</td></tr></table></ul>",
              title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/ul-tbl", P967, raw="967"), IDENT)
    t("★再C1: 本文967＋ul>table別機種967 → REVIEW（無警告PASSを禁止）",
      r["verified"] is False and r["c5_code"] == "TABLE_BINDING_PENDING")
    # (7) 未知/カスタム要素内の表も可視化（Critical1）
    xs = tcells("<x-content><table><tr><td>バベル967G</td></tr></table></x-content>")
    t("★再C1: 未知要素>table の値も可視化される（無警告ドロップしない）",
      any("967" in x.text for x in xs))
    mock_page("https://chonborista.com/x-tbl",
              "<h1>アクダマ</h1><p>" + P967 + "</p>"
              "<x-content><table><tr><td>バベル967G</td></tr></table></x-content>",
              title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/x-tbl", P967, raw="967"), IDENT)
    t("★再C1: 本文967＋未知要素>table別機種967 → REVIEW（無警告PASSを禁止）",
      r["verified"] is False and r["c5_code"] == "TABLE_BINDING_PENDING")
    # (8) ★明示的な非本文領域(nav)の表は従来どおり起こさない＝証拠にしない（区別の回帰ガード）
    t("★再C1: nav>table は collector非起動＝値を証拠にしない（本文でない・区別維持）",
      tcells("<nav><table><tr><td>ナビ967</td></tr></table></nav>") == []
      and tcells("<form><table><tr><td>フォーム967</td></tr></table></form>") == [])
    # (9) 大量 headers 属性 → sentinelで ambiguous＋limit_hit（doc REVIEW・巨大split抑止・Critical3）
    huge = "<table><tr><th id=h>通常</th><td headers='" + ("h " * 5000) + "'>967</td></tr></table>"
    c = find_cell(huge, "967")
    _, _, whdr = pu(huge)
    t("★再C3: 大量headers属性は ambiguous＋table_limit（巨大split抑止・doc REVIEW）",
      c is not None and c.table_context.header_resolution == "ambiguous"
      and "table_limit" in whdr)
    # (10) scope付きth 過多 → limit_hit（O(n²)抑止・doc REVIEW）
    many_scope = "<table><tr>" + "".join("<th scope=col>h%d</th>" % i for i in range(_MAX_SCOPED_TH + 5)) \
                 + "</tr><tr><td>アクダマ967</td></tr></table>"
    _, _, wsc = pu(many_scope)
    t("★再C2: scope付きth過多は table_limit（ヘッダー解決のO(n²)抑止・doc REVIEW）",
      "table_limit" in wsc)

    # ══════════════════════════════════════════════════════════════
    # ★Codex第3次Critical＝rowspan/colspan の巨大桁を int() 前に桁拒否（整数化DoS防止）★
    # ══════════════════════════════════════════════════════════════
    import time as _time
    # ★数百万桁の span 属性＝巨大属性タグ → overlong_tag で feed前REVIEW（第13次DoS：メモリ増幅を feed前に止める）★
    _t0 = _time.time()
    _, _, wovt = pu("<table><tr><td rowspan='" + "9" * 2_000_000 + "'>アクダマ967</td></tr></table>")
    _dt = _time.time() - _t0
    t("★第3/13次C: 数百万桁rowspan(巨大属性)は overlong_tag で feed前REVIEW＋短時間（整数化/メモリDoS防止）",
      "overlong_tag" in wovt and _dt < 2.0)
    _t0 = _time.time()
    _, _, wovt2 = pu("<table><tr><td colspan='" + "9" * 2_000_000 + "'>アクダマ967</td></tr></table>")
    t("★第3/13次C: 数百万桁colspanも overlong_tag（DoS有界）",
      "overlong_tag" in wovt2 and (_time.time() - _t0) < 2.0)
    # 1MB未満の巨大桁span（overlong_tag閾値未満）は従来どおり _parse_span が桁拒否＝table_malformed
    c = find_cell("<table><tr><td rowspan='" + "9" * 10000 + "'>x967</td></tr></table>", "967")
    t("★第3次C: 1MB未満の巨大桁spanは _parse_span が桁拒否＝table_malformed（int化前・桁制限非依存）",
      c is not None and "table_malformed" in c.parse_flags)
    t("★第3次C: span '+100'/全角'１００'/'②' は int化前に拒否＝malformed",
      "table_malformed" in find_cell("<table><tr><td rowspan='+100'>x967</td></tr></table>", "967").parse_flags
      and "table_malformed" in find_cell("<table><tr><td rowspan='１００'>x967</td></tr></table>", "967").parse_flags
      and "table_malformed" in find_cell("<table><tr><td rowspan='②'>x967</td></tr></table>", "967").parse_flags)
    t("★第3次C: span 100は許容(malformedにしない)・101は範囲外malformed",
      "table_malformed" not in find_cell("<table><tr><td rowspan=100>x967</td></tr></table>", "967").parse_flags
      and "table_malformed" in find_cell("<table><tr><td rowspan=101>x967</td></tr></table>", "967").parse_flags)
    mock_page("https://chonborista.com/bigspan",
              "<h1>アクダマ</h1><p>" + P967 + "</p>"
              "<table><tr><td rowspan='" + "9" * 100000 + "'>アクダマ967</td></tr></table>",
              title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/bigspan", P967, raw="967"), IDENT)
    t("★第3次C: 本文967＋巨大桁span表967 → REVIEW（迂回封鎖）",
      r["verified"] is False and r["c5_code"] == "TABLE_BINDING_PENDING")

    # ══════════════════════════════════════════════════════════════
    # ★Codex第4次Critical＝巨大な数値文字参照(&#…;)の int() 復号DoS（HTMLParser/html.unescape 共通）★
    # ══════════════════════════════════════════════════════════════
    LREF_DEC = "&#" + "9" * 2_000_000 + ";"
    LREF_HEX = "&#x" + "9" * 2_000_000 + ";"
    for label, html in (
        ("rowspan属性の巨大10進charref",
         "<table><tr><td rowspan='" + LREF_DEC + "'>x967</td></tr></table>"),
        ("colspan属性の巨大16進charref",
         "<table><tr><td colspan='" + LREF_HEX + "'>x967</td></tr></table>"),
        ("無関係属性data-xの巨大charref", "<div data-x='" + LREF_DEC + "'>本文967</div>"),
        ("本文テキストの巨大charref", "<p>本文" + LREF_DEC + "967</p>"),
    ):
        _t0 = _time.time()
        _, _, w = pu(html)
        t("★第4次C: %s → feed前検出 numeric_charref_too_long＋短時間（整数化DoS防止）" % label,
          "numeric_charref_too_long" in w and (_time.time() - _t0) < 2.0)
    # 本文PASS可能967＋巨大charref → 取得後の関所で CHARREF_DOS（平坦化/構造化の前にREVIEW）
    mock_page("https://chonborista.com/charref",
              "<h1>アクダマ</h1><p>" + P967 + LREF_DEC + "</p>", title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/charref", P967, raw="967"), IDENT)
    t("★第4次C: 本文967＋巨大charref → REVIEW(CHARREF_DOS・平坦化unescapeの前で止める)",
      r["verified"] is False and r["c5_code"] == "CHARREF_DOS")
    # 正常な短い数値文字参照は誤検知しない（&#12539;=中点・&#x5929;=天）
    _, _, wok = pu("<p>アクダマ&#12539;天井&#x5929;967</p>")
    t("★第4次C: 正常な短い数値文字参照は誤検知しない（numeric_charref_too_long出さない）",
      "numeric_charref_too_long" not in wok)
    # ★整数文字列桁数制限を無効化した状態でも DoS にならず検出（桁制限非依存の実証・Codex要求）
    if hasattr(sys, "set_int_max_str_digits"):
        _old_limit = sys.get_int_max_str_digits()
        try:
            sys.set_int_max_str_digits(0)           # 桁制限を無効化（この状態でこそ int() DoSが顕在化）
            _t0 = _time.time()
            _, _, w = pu("<td rowspan='&#" + "9" * 3_000_000 + ";'>x967</td>")
            t("★第4次C: int桁数制限を無効化(set_int_max_str_digits(0))してもDoSにならず検出（桁制限非依存）",
              "numeric_charref_too_long" in w and (_time.time() - _t0) < 3.0)
        finally:
            sys.set_int_max_str_digits(_old_limit)

    # ══════════════════════════════════════════════════════════════
    # ★Codex第6次Critical＝表セル/caption/stray のブロック境界で数字が連結し表ゲートを迂回する穴 ★
    #   <div>…967</div><div>123</div> を『…967 123』に区切り 967 を表値として検出（誤PASS防止）。
    #   インライン分割 <span>9</span><span>67</span> は 967 に連結（過剰区切りしない）。
    # ══════════════════════════════════════════════════════════════
    for tag_key, html_frag in (
        ("セル", "<table><tr><td><div>バベルの天井967</div><div>123</div></td></tr></table>"),
        ("caption", "<table><caption><div>バベル967</div><div>123</div></caption><tr><td>x</td></tr></table>"),
        ("stray", "<table><div>バベル967</div><div>123</div><tr><td>y</td></tr></table>"),
    ):
        url = "https://chonborista.com/blk-" + tag_key
        mock_page(url, "<h1>アクダマ</h1><p>" + P967 + "</p>" + html_frag, title="アクダマ 天井")
        r = V("ceiling.normal", ev(967, url, P967, raw="967"), IDENT)
        t("★第6次C: 本文967＋%s内のブロック境界967 → REVIEW（境界で連結させず表値検出）" % tag_key,
          r["verified"] is False and r["c5_code"] == "TABLE_BINDING_PENDING")
    dc = find_cell("<table><tr><td><div>A967</div><div>123B</div></td></tr></table>", "967")
    t("★第6次C: div境界は空白区切り（『967 123』＝連結しない）で値検出可",
      dc is not None and "967 123" in dc.text)
    sc = find_cell("<table><tr><td><span>9</span><span>67</span></td></tr></table>", "967")
    t("★第6次C: <span>9</span><span>67</span> はインライン連結で 967 を表値として検出（過剰区切りしない）",
      sc is not None and sc.text == "967")
    nc = find_cell("<table><tr><td>親967<table><tr><td>子123</td></tr></table></td></tr></table>", "967")
    t("★第6次C: ネスト表開始も境界（親967 子123＝連結しない）＋nested_tableフラグ",
      nc is not None and "967" in nc.text and "967123" not in nc.text
      and "nested_table" in nc.parse_flags)

    # ══════════════════════════════════════════════════════════════
    # ★Codex第7次Critical＝void/skip/複数caption/構造非切替/未知要素の境界で数字が連結する残穴 ★
    #   保留区切り(pending-sep)モデル＝全境界を分類し「実際に入るsink」へ遅延挿入。未知要素×数字隣接はdoc REVIEW。
    # ══════════════════════════════════════════════════════════════
    def cell_has(html, needle):    # 全表セルテキストを連結して桁境界照合（迂回検査用）
        cs = tcells(html)
        return cv_unit_join_has(cs, needle)

    def cv_unit_join_has(cs, needle):
        return any(_unit_contains_value(x.text, needle) for x in cs)

    # (C1) void要素 img が境界（バベル967<img>123 → 967 検出）
    t("★第7次C1: セル内 void img の境界で967が連結せず検出される",
      cell_has("<table><tr><td>バベル967<img src=x>123</td></tr></table>", "967"))
    # (C2) skip要素 script の開始/終了境界（967<script>x</script>123 → 967 検出・script内は捨てる）
    cs = tcells("<table><tr><td>バベル967<script>隠し888</script>123</td></tr></table>")
    t("★第7次C2: セル内 script の境界で967検出＋script内888は混ざらない",
      cv_unit_join_has(cs, "967") and all("888" not in x.text for x in cs))
    # (C3) 複数caption/構造非切替(colgroup/col)/行グループ跨ぎstray の境界
    t("★第7次C3: 複数captionの境界で967検出（共有バッファでも連結しない）",
      cell_has("<table><caption>バベル967</caption><caption>123</caption><tr><td>x</td></tr></table>", "967"))
    t("★第7次C3: セル内 colgroup/col の境界で967検出",
      cell_has("<table><tr><td>バベル967<colgroup></colgroup>123</td></tr></table>", "967")
      and cell_has("<table><tr><td>バベル967<col>123</td></tr></table>", "967"))
    t("★第7次C3: 行グループ(thead)を跨ぐstrayの境界で967検出",
      cell_has("<table>バベル967<thead></thead>123<tr><td>x</td></tr></table>", "967"))
    # (C4) 未知カスタム要素×数字隣接は曖昧→doc REVIEW（連結/分離の両解釈で値を見失うため）
    _, _, wamb = pu("<table><tr><td>天井<x-digit>9</x-digit><x-digit>67</x-digit>G</td></tr></table>")
    t("★第7次C4: 未知要素が数字間 → table_limit（曖昧→doc REVIEW・両解釈で見失う）",
      "table_limit" in wamb)
    # 既知インライン(span/nobr)は連結を保つ（過剰区切りしない＝正当な値分割を壊さない）
    t("★第7次: <span>9</span><span>67</span>・<nobr>…</nobr> はインライン連結で967（過剰区切りしない）",
      find_cell("<table><tr><td><span>9</span><span>67</span></td></tr></table>", "967").text == "967"
      and find_cell("<table><tr><td><nobr>9</nobr><nobr>67</nobr></td></tr></table>", "967").text == "967")
    # E2E: 本文PASS可能967＋各迂回経路 → REVIEW（誤PASSしない）
    for key, frag, code in (
        # img/script は「数字|タグ|数字」＝要素境界跨ぎ構成の疑いで先に PARSE_WARNINGS（第18次C2・どちらもREVIEW）
        ("img", "<table><tr><td>バベル967<img src=x>123</td></tr></table>", "PARSE_WARNINGS"),
        ("script", "<table><tr><td>バベル967<script>x</script>123</td></tr></table>", "PARSE_WARNINGS"),
        ("multicap", "<table><caption>バベル967</caption><caption>123</caption><tr><td>x</td></tr></table>", "TABLE_BINDING_PENDING"),
        ("unknown", "<table><tr><td>バベル<x-d>9</x-d><x-d>67</x-d></td></tr></table>", "PARSE_WARNINGS"),
    ):
        url = "https://chonborista.com/v7-" + key
        mock_page(url, "<h1>アクダマ</h1><p>" + P967 + "</p>" + frag, title="アクダマ 天井")
        r = V("ceiling.normal", ev(967, url, P967, raw="967"), IDENT)
        t("★第7次E2E: 本文967＋%s迂回 → REVIEW(%s)" % (key, code),
          r["verified"] is False and r["c5_code"] == code)

    # ══════════════════════════════════════════════════════════════
    # ★Codex第8次Critical＝slot(shadow DOM曖昧)・ruby基底テキスト混入 の値漏れ ★
    # ══════════════════════════════════════════════════════════════
    # (C1) slot は shadow DOM で内容が差し替わり得て曖昧＝未知扱い。数字隣接なら doc REVIEW（分離で値漏れさせない）
    _, _, wslot = pu("<table><tr><td>バベルの天井9<slot></slot>67G</td></tr></table>")
    t("★第8次C1: slot が数字間 → table_limit（shadow DOM曖昧→doc REVIEW・値漏れ封鎖）",
      "table_limit" in wslot)
    mock_page("https://chonborista.com/v8-slot",
              "<h1>アクダマ</h1><p>" + P967 + "</p>"
              "<table><tr><td>バベルの天井9<slot></slot>67G</td></tr></table>", title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/v8-slot", P967, raw="967"), IDENT)
    t("★第8次C1: 本文967＋slot分離の別機種967 → REVIEW(PARSE_WARNINGS)",
      r["verified"] is False and r["c5_code"] == "PARSE_WARNINGS")
    # (C2) ruby 基底テキストは 967（rt/rp注釈はドロップ＝基底のみ証拠に）→ 表値として検出
    rc = find_cell("<table><tr><td>バベルの天井<ruby>9<rt>きゅう</rt>67</ruby>G</td></tr></table>", "967")
    t("★第8次C2: ruby基底テキスト967を抽出（rt注釈『きゅう』はドロップ・基底のみ）",
      rc is not None and "967" in rc.text and "きゅう" not in rc.text)
    rc2 = find_cell("<table><tr><td><ruby>9<rp>(</rp><rt>x</rt><rp>)</rp>67</ruby></td></tr></table>", "967")
    t("★第8次C2: ruby rp(フォールバック)/rt もドロップして基底967を抽出",
      rc2 is not None and rc2.text == "967")
    mock_page("https://chonborista.com/v8-ruby",
              "<h1>アクダマ</h1><p>" + P967 + "</p>"
              "<table><tr><td>バベルの天井<ruby>9<rt>きゅう</rt>67</ruby>G</td></tr></table>", title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/v8-ruby", P967, raw="967"), IDENT)
    # ruby基底は rt を挟んで「9|rt|67」＝要素境界跨ぎ構成の疑い → 第18次C2で先に PARSE_WARNINGS（どちらもREVIEW）
    t("★第8次C2: 本文967＋ruby基底967(別機種) → REVIEW(基底を可視化しつつ境界跨ぎでPARSE_WARNINGS)",
      r["verified"] is False and r["c5_code"] == "PARSE_WARNINGS")
    # (非ブロッキング) skip内部の未知タグ(svg>path)は余計なdoc REVIEWにしない（skip境界のみ区切る）
    _, _, wsvg = pu("<table><tr><td>9<svg><path/></svg>67</td></tr></table>")
    t("★第8次: skip内部の未知path は table_limit を誘発しない（skip境界のみ・余計なREVIEW回避）",
      "table_limit" not in wsvg)

    # ══════════════════════════════════════════════════════════════
    # ★Codex第9次Critical＝rtc未ドロップ/rt暗黙終了desync/meter等の無警告分離 ★
    # ══════════════════════════════════════════════════════════════
    # (C1) rtc 注釈もドロップして基底967を抽出
    t("★第9次C1: <ruby>9<rtc>x</rtc>67</ruby> は rtc注釈をドロップし基底967を抽出",
      find_cell("<table><tr><td><ruby>9<rtc>注釈</rtc>67</ruby></td></tr></table>", "967").text == "967")
    # (C2) rt/rp の暗黙終了（</ruby>で閉じる）でも annot_depth が正しく戻り、ruby後の基底値を過剰ドロップしない
    for label, frag, expect in (
        ("rt暗黙終了", "<td>バベル<ruby>A<rt>注釈</ruby>967G</td>", "バベルA967G"),
        ("rp暗黙終了", "<td>バベル<ruby>A<rp>(</ruby>967G</td>", "バベルA967G"),
        ("rt→rp兄弟暗黙", "<td><ruby>A<rt>x<rp>(</rp>967</ruby></td>", "A967"),
    ):
        c = find_cell("<table><tr>" + frag + "</tr></table>", "967")
        t("★第9次C2: %s でも annot_depth が戻り基底値を過剰ドロップしない（967検出）" % label,
          c is not None and "967" in c.text and c.text == expect)
    mock_page("https://chonborista.com/v9-ruby",
              "<h1>アクダマ</h1><p>" + P967 + "</p>"
              "<table><tr><td>バベル<ruby>A<rt>注釈</ruby>967G</td></tr></table>", title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/v9-ruby", P967, raw="967"), IDENT)
    t("★第9次C2: 本文967＋ruby暗黙終了後の別機種967 → REVIEW（過剰ドロップせず可視化・迂回封鎖）",
      r["verified"] is False and r["c5_code"] == "TABLE_BINDING_PENDING")
    # (C3) meter/progress/selectedcontent は数字隣接で doc REVIEW（無警告分離させない）
    for widget in ("meter", "progress", "selectedcontent"):
        html = "<table><tr><td>天井<%s>9</%s>67G</td></tr></table>" % (widget, widget)
        _, _, wr = pu(html)
        t("★第9次C3: %s が数字間 → table_limit（置換/動的要素を無警告で分離しない）" % widget,
          "table_limit" in wr)

    # ══════════════════════════════════════════════════════════════
    # ★Codex第10次Critical＝注釈内境界の基底分断／孤立rt誤ドロップ／map SKIP／fallback要素 ★
    # ══════════════════════════════════════════════════════════════
    # (C1) 注釈(rt)内部の境界(br/img/div)は基底テキストを分断しない（annot_depthで全転送を抑止）
    for label, frag in (("br", "<br>"), ("img", "<img src=x>"), ("div", "<div>x</div>")):
        c = find_cell("<table><tr><td><ruby>9<rt>%sきゅう</rt>67</ruby></td></tr></table>" % frag, "967")
        t("★第10次C1: 注釈rt内の %s は基底を分断しない（基底967を連結抽出）" % label,
          c is not None and c.text == "967")
    # (C2) 孤立/誤配置の rt/rp/rtc は注釈扱いせず内容を保持（実親がruby/rtcのみ注釈）
    t("★第10次C2: 孤立<rt>967</rt>（親がtd）は内容を保持し967を検出（誤ドロップしない）",
      find_cell("<table><tr><td><rt>バベルの天井967G</rt></td></tr></table>", "967") is not None)
    t("★第10次C2: <ruby>9<span><rt>6</rt></span>7</ruby>（rtの実親がspan）は6を保持し967",
      find_cell("<table><tr><td><ruby>9<span><rt>6</rt></span>7</ruby></td></tr></table>", "967").text == "967")
    mock_page("https://chonborista.com/v10-orphan",
              "<h1>アクダマ</h1><p>" + P967 + "</p>"
              "<table><tr><td><rt>バベルの天井967G</rt></td></tr></table>", title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/v10-orphan", P967, raw="967"), IDENT)
    t("★第10次C2: 本文967＋孤立rt内の別機種967 → REVIEW（誤ドロップせず可視化・迂回封鎖）",
      r["verified"] is False and r["c5_code"] == "TABLE_BINDING_PENDING")
    # (C3) map は透過コンテンツ＝捕捉（SKIPしない）→ 表値として可視化
    t("★第10次C3: <map>967</map> は透過コンテンツとして捕捉（SKIPしない）",
      find_cell("<table><tr><td><map name=x>バベルの天井967G</map></td></tr></table>", "967") is not None)
    mock_page("https://chonborista.com/v10-map",
              "<h1>アクダマ</h1><p>" + P967 + "</p>"
              "<table><tr><td><map name=x>バベルの天井967G</map></td></tr></table>", title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/v10-map", P967, raw="967"), IDENT)
    t("★第10次C3: 本文967＋map内別機種967 → REVIEW（map捕捉で迂回封鎖）",
      r["verified"] is False and r["c5_code"] == "TABLE_BINDING_PENDING")
    # (追加) object/canvas/audio/video/noscript の fallback は捨てて安全と言えない→表内出現でdoc REVIEW
    for fb in ("object", "canvas", "audio", "video", "noscript"):
        _, _, wf = pu("<table><tr><td>天井<%s>967</%s>あり</td></tr></table>" % (fb, fb))
        t("★第10次: fallback要素 %s が表セル内 → table_limit（隠れ値の可能性→doc REVIEW）" % fb,
          "table_limit" in wf)
    mock_page("https://chonborista.com/v10-obj",
              "<h1>アクダマ</h1><p>" + P967 + "</p>"
              "<table><tr><td><object data=x>バベル967</object></td></tr></table>", title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/v10-obj", P967, raw="967"), IDENT)
    t("★第10次: 本文967＋object fallback → REVIEW(PARSE_WARNINGS・隠れ値を無警告で消さない)",
      r["verified"] is False and r["c5_code"] == "PARSE_WARNINGS")

    # ══════════════════════════════════════════════════════════════
    # ★Codex第11次Critical＝段落パーサでも「可視値を無警告で消す→誤PASS」＝shadow機構で統一防御 ★
    #   構造化しなかった可視テキスト(shadow・content領域限定)に値が出たらREVIEW。段落/表 共通。
    # ══════════════════════════════════════════════════════════════
    # (C1) 段落内の meter/nobr/progress/slot/未知カスタム要素の値 → shadow → VALUE_IN_SHADOW
    for label, frag in (("meter", "<meter>967</meter>"), ("nobr", "<nobr>967</nobr>"),
                        ("slot", "<slot>967</slot>"), ("progress", "<progress>967</progress>"),
                        ("未知要素", "<x-custom>967</x-custom>")):
        sv = sh("<p>バベルの通常時の天井は%sG</p>" % frag)
        t("★第11次C1: 段落内 %s の値は shadow に退避される" % label,
          _unit_contains_value(sv, "967"))
        url = "https://chonborista.com/v11-c1-" + label
        mock_page(url, "<h1>アクダマ</h1><p>" + P967 + "</p><p>バベル%sG</p>" % frag, title="アクダマ 天井")
        r = V("ceiling.normal", ev(967, url, P967, raw="967"), IDENT)
        t("★第11次C1: 本文967＋段落%s隠れ967 → REVIEW(VALUE_IN_SHADOW)" % label,
          r["verified"] is False and r["c5_code"] == "VALUE_IN_SHADOW")
    # (C2) 段落内 ruby 基底は透過捕捉＝unitに入る（shadowでなく可視unit）→ 基底値の消失なし
    uu = pu("<p>バベルの天井は<ruby>9<rt>きゅう</rt>67</ruby>G</p>")[0]
    t("★第11次C2: 段落内 ruby 基底967はunitに捕捉（消失しない）・rt注釈はドロップ",
      any("967" in x.text for x in uu))
    # (C3) content領域の object/canvas/svg text/math → shadow → REVIEW
    for label, frag in (("object", "<object>バベル967</object>"),
                        ("svg text", "<svg><text>バベル967</text></svg>"),
                        ("math", "<math><mn>967</mn></math>")):
        sv = sh("<p>x</p><div>%s</div>" % frag)
        t("★第11次C3: content領域の %s の値は shadow に退避される" % label,
          _unit_contains_value(sv, "967"))
    mock_page("https://chonborista.com/v11-svg",
              "<h1>アクダマ</h1><p>" + P967 + "</p><div><svg><text>バベル967</text></svg></div>",
              title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/v11-svg", P967, raw="967"), IDENT)
    t("★第11次C3: 本文967＋svg text隠れ967 → REVIEW(VALUE_IN_SHADOW)",
      r["verified"] is False and r["c5_code"] == "VALUE_IN_SHADOW")
    # (C4) xmp/plaintext は content領域出現で obsolete_raw_text 警告 → doc REVIEW
    for raw in ("xmp", "plaintext", "listing"):
        _, _, wrt = pu("<div>x<%s>バベル967</%s></div>" % (raw, raw))
        t("★第11次C4: obsolete raw-text %s は doc REVIEW(obsolete_raw_text警告)" % raw,
          "obsolete_raw_text" in wrt)
    mock_page("https://chonborista.com/v11-xmp",
              "<h1>アクダマ</h1><p>" + P967 + "</p><div><xmp>バベル967</xmp></div>", title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/v11-xmp", P967, raw="967"), IDENT)
    t("★第11次C4: 本文967＋xmp → REVIEW(PARSE_WARNINGS)",
      r["verified"] is False and r["c5_code"] == "PARSE_WARNINGS")
    # (誤検知なし) nav/header/footer の値は shadow にしない（chrome＝content領域でない）
    t("★第11次: nav/footer の値は shadow にしない（誤検知回避）",
      not _unit_contains_value(sh("<nav>メニュー967</nav><p>本文</p>"), "967")
      and not _unit_contains_value(sh("<footer>コピーライト967</footer><p>本文</p>"), "967"))
    # 本文の正常値は shadow にならず PASS 可能（過剰REVIEWしない・回帰確認）
    mock_page("https://chonborista.com/v11-clean",
              "<h1>アクダマ</h1><p>" + P967 + "</p>", title="アクダマ 天井")
    r = V("ceiling.normal.at", ev(967, U1, Q_AT967), IDENT)
    t("★第11次: 通常の本文値は shadow 誤検知せず従来通り検証される（回帰なし）",
      r["verified"] is True)

    # ══════════════════════════════════════════════════════════════
    # ★Codex第12次Critical＝chrome/opaque判定の分離・unit/shadow跨ぎ値分割・skip内script混入・shadow断片DoS ★
    # ══════════════════════════════════════════════════════════════
    # (C1) opaque(未知/meter)の子孫(content=False)も shadow に退避（chrome とは区別）
    t("★第12次C1: opaque子孫(x-custom>span)の値も shadow に退避（content=Falseでも chrome外なら）",
      _unit_contains_value(sh("<p>本文</p><x-custom><span>バベル967</span></x-custom>"), "967")
      and _unit_contains_value(sh("<div><meter><span>967</span></meter></div>"), "967"))
    mock_page("https://chonborista.com/v12-c1",
              "<h1>アクダマ</h1><p>" + P967 + "</p><x-custom><span>バベル967</span></x-custom>",
              title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/v12-c1", P967, raw="967"), IDENT)
    t("★第12次C1: 本文967＋opaque子孫の隠れ967 → REVIEW(VALUE_IN_SHADOW)",
      r["verified"] is False and r["c5_code"] == "VALUE_IN_SHADOW")
    # (C2) unit/shadow跨ぎの値分割（9<opaque>6</opaque>7）→ opaque_in_digit_run 警告 → doc REVIEW
    _, _, wsp = pu("<p>バベルの天井は9<x-custom>6</x-custom>7G</p>")
    t("★第12次C2: unit/shadow跨ぎの値分割(9[6]7)→ opaque_in_digit_run（doc REVIEW）",
      "opaque_in_digit_run" in wsp)
    mock_page("https://chonborista.com/v12-c2",
              "<h1>アクダマ</h1><p>" + P967 + "</p><p>バベルの天井は9<x-custom>6</x-custom>7G</p>",
              title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/v12-c2", P967, raw="967"), IDENT)
    t("★第12次C2: 本文967＋分割967(9[6]7) → REVIEW(PARSE_WARNINGS)",
      r["verified"] is False and r["c5_code"] == "PARSE_WARNINGS")
    # (C2') shadow断片跨ぎ（コメント分断 9<!--c-->67）は無区切り連結で 967 検出
    t("★第12次C2': shadow内コメント分断(9<!--c-->67)も無区切り連結で967検出",
      _unit_contains_value(sh("<div><x-custom>9<!--c-->67</x-custom></div>"), "967"))
    # (Major) 可視skip(svg)の内側の script/style は shadow にしない（非表示コード）
    t("★第12次Major: svg内の script は shadow にしない（非表示コード・過剰REVIEW回避）",
      not _unit_contains_value(sh("<div><svg><script>const v=967;</script></svg></div>"), "967")
      and _unit_contains_value(sh("<div><svg><text>バベル967</text></svg></div>"), "967"))
    # (DoS) コメント大量分断は shadow_overflow/too_many_nodes で有界（メモリDoS防止）
    import time as _tm
    _t0 = _tm.time()
    _, _, wdos = pu("<x-custom>" + ("9<!--x-->" * 300000) + "</x-custom>")
    t("★第12次DoS: コメント大量分断は shadow_overflow/too_many_nodes で有界（短時間）",
      ("shadow_overflow" in wdos or "too_many_nodes" in wdos) and (_tm.time() - _t0) < 3.0)
    # (誤検知なし) nav>span の値は shadow にしない（chrome_depthで除外）
    t("★第12次: nav>span の値は shadow にしない（chrome＝誤検知回避）",
      not _unit_contains_value(sh("<nav><span>メニュー967</span></nav><p>x</p>"), "967"))

    # ══════════════════════════════════════════════════════════════
    # ★Codex第13次Critical＝shadow→unit方向の値分割・可視skip(textarea/select)・終了境界reset・属性/宣言DoS ★
    # ══════════════════════════════════════════════════════════════
    # (C1) shadow→unit 方向の値分割（<x-custom>96</x-custom>7）も双方向検知＝opaque_in_digit_run
    for label, frag in (("shadow→unit", "<x-custom>96</x-custom>7G"),
                        ("shadow(nest)→unit", "<x-custom><span>96</span></x-custom>7G"),
                        ("全角", "<x-custom>９６</x-custom>７G"),
                        ("svg text→unit", "<svg><text>96</text></svg>7G")):
        _, _, wd = pu("<p>バベルの天井は%s</p>" % frag)
        t("★第13次C1: %s の値分割 → opaque_in_digit_run（双方向・doc REVIEW）" % label,
          "opaque_in_digit_run" in wd)
    mock_page("https://chonborista.com/v13-c1",
              "<h1>アクダマ</h1><p>" + P967 + "</p><p>バベルの天井は<x-custom>96</x-custom>7G</p>",
              title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/v13-c1", P967, raw="967"), IDENT)
    t("★第13次C1: 本文967＋shadow→unit分割967 → REVIEW(PARSE_WARNINGS)",
      r["verified"] is False and r["c5_code"] == "PARSE_WARNINGS")
    # (C2) textarea(既定値)/select(option)/datalist は可視＝shadow に退避（hidden扱いしない）
    for vis in ("textarea", "select", "datalist"):
        inner = "<option>バベル967</option>" if vis in ("select", "datalist") else "バベル967"
        t("★第13次C2: %s の可視内容は shadow に退避（hidden扱いしない）" % vis,
          _unit_contains_value(sh("<div><%s>%s</%s></div>" % (vis, inner, vis)), "967"))
    mock_page("https://chonborista.com/v13-c2",
              "<h1>アクダマ</h1><p>" + P967 + "</p><textarea>バベル967</textarea>", title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/v13-c2", P967, raw="967"), IDENT)
    t("★第13次C2: 本文967＋textarea隠れ967 → REVIEW(VALUE_IN_SHADOW)",
      r["verified"] is False and r["c5_code"] == "VALUE_IN_SHADOW")
    # (Major) 終了ブロック境界でも run リセット＝独立した次のopaque値で誤発火しない
    for frag in ("<p>9</p><x-custom>6</x-custom>", "<div>9</div><meter>6</meter>",
                 "<p>9</p>\n<x-custom>6</x-custom>"):
        _, _, wm = pu(frag)
        t("★第13次Major: 終了境界後の独立opaque値は opaque_in_digit_run 誤発火しない: %s" % frag[:18],
          "opaque_in_digit_run" not in wm)
    # (DoS) 巨大属性数（<1MB）は too_many_attrs、宣言/PI大量は too_many_nodes で有界
    _, _, wa = pu("<td " + ("a " * 100000) + ">967</td>")
    _, _, wp = pu("<?x?>" * 300000)
    t("★第13次DoS: 巨大属性数→too_many_attrs / PI大量→too_many_nodes で有界",
      "too_many_attrs" in wa and "too_many_nodes" in wp)

    # ══════════════════════════════════════════════════════════════
    # ★Codex第14次Critical＝hidden skip内境界の値分割回避／DoS事前検査の引用符・コメント終端誤認 ★
    # ══════════════════════════════════════════════════════════════
    # (C) hidden skip(template等)内の境界(br/div)は可視runをresetしない＝値分割検知を回避されない（双方向）
    for label, frag in (
        ("template>br 順方向", "9<template><br></template><x-custom>67</x-custom>G"),
        ("template>br 逆方向", "<x-custom>96</x-custom><template><br></template>7G"),
        ("template>div", "9<template><div></div></template><x-custom>67</x-custom>G"),
    ):
        _, _, wh = pu("<p>バベルの天井は%s</p>" % frag)
        t("★第14次C: hidden skip内境界(%s)でも opaque_in_digit_run（reset回避されない）" % label,
          "opaque_in_digit_run" in wh)
    mock_page("https://chonborista.com/v14-c",
              "<h1>アクダマ</h1><p>" + P967 + "</p>"
              "<p>バベルの天井は9<template><br></template><x-custom>67</x-custom>G</p>", title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/v14-c", P967, raw="967"), IDENT)
    t("★第14次C: 本文967＋hidden skip跨ぎ分割967 → REVIEW(PARSE_WARNINGS)",
      r["verified"] is False and r["c5_code"] == "PARSE_WARNINGS")
    # (DoS) 引用符内の `>`・コメント終端 `-->` を尊重して巨大トークンを feed前に検出
    _, _, wq = pu("<td id='>" + ("A" * 2_000_000) + "'>967</td>")
    _, _, wc = pu("<!-- >" + ("A" * 2_000_000) + " -->")
    t("★第14次DoS: 引用符内`>`の巨大属性・巨大コメントも overlong_tag（終端誤認しない）",
      "overlong_tag" in wq and "overlong_tag" in wc)
    # 正常な短い引用符属性・短いコメントは overlong_tag にしない（誤検知なし）
    _, _, wok = pu("<p id='a>b'>本文</p><!-- ok -->")
    t("★第14次DoS: 正常な短い引用符/コメントは overlong_tag 誤検知しない",
      "overlong_tag" not in wok)

    # ══════════════════════════════════════════════════════════════
    # ★Codex第15次Critical＝ruby注釈が基底可視runを汚染／CDATA・DOCTYPE内部サブセットの終端過小評価 ★
    # ══════════════════════════════════════════════════════════════
    # (C1) ruby注釈(rt)は別ストリーム＝基底可視runを汚さない → ruby基底＋opaqueに分割された値も検知
    for label, frag in (
        ("ruby基底→opaque", "<ruby>9<rt>きゅう</rt></ruby><x-custom>67</x-custom>G"),
        ("ruby>rt>br→opaque", "<ruby>9<rt><br></rt></ruby><x-custom>67</x-custom>G"),
    ):
        _, _, wr = pu("<p>バベルの天井は%s</p>" % frag)
        t("★第15次C1: %s の値分割 → opaque_in_digit_run（注釈が基底runを汚さない）" % label,
          "opaque_in_digit_run" in wr)
    mock_page("https://chonborista.com/v15-c1",
              "<h1>アクダマ</h1><p>" + P967 + "</p>"
              "<p>バベルの天井は<ruby>9<rt>きゅう</rt></ruby><x-custom>67</x-custom>G</p>", title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/v15-c1", P967, raw="967"), IDENT)
    t("★第15次C1: 本文967＋ruby基底/opaque分割967 → REVIEW(PARSE_WARNINGS)",
      r["verified"] is False and r["c5_code"] == "PARSE_WARNINGS")
    # 正常なruby（読み仮名のみ・数字分割なし）は誤発火しない
    t("★第15次C1: 正常な ruby(北斗/ほくと) は opaque_in_digit_run 誤発火しない",
      "opaque_in_digit_run" not in pu("<p>アクダマ<ruby>北斗<rt>ほくと</rt></ruby>の天井は967G</p>")[2])
    # (C2) CDATA(]]>)・DOCTYPE内部サブセット([]深さ)の終端を尊重して巨大トークンを feed前検出
    _, _, wcd = pu("<![CDATA[>" + ("A" * 2_000_000) + "]]>")
    _, _, wdt = pu("<!DOCTYPE x [<!ELEMENT x ANY>" + (" " * 2_000_000) + "]>")
    t("★第15次C2: CDATA/DOCTYPE内部サブセットの巨大トークンも overlong_tag（終端過小評価しない）",
      "overlong_tag" in wcd and "overlong_tag" in wdt)
    # (非crit) Unicode地の文は overlong_tag 誤検知しない（ASCII英字のみタグ判定）
    t("★第15次: Unicode地の文(<天井…)は overlong_tag 誤検知しない（ASCII限定タグ判定）",
      "overlong_tag" not in pu("<天井" + ("あ" * 1_100_000))[2])

    # ══════════════════════════════════════════════════════════════
    # ★Codex第16次Critical＝表collector基底↔vskip shadow 跨ぎの値分割／巨大不正終了タグのDoS迂回 ★
    # ══════════════════════════════════════════════════════════════
    # (C1) 表セル基底テキストも可視run追跡に参加＝cell内 svg/math/textarea/select 跨ぎの値分割を双方向検知
    for label, frag in (
        ("td 9<svg>67", "<td>バベルの天井は9<svg><text>67</text></svg>G</td>"),
        ("td <svg>96</svg>7", "<td><svg><text>96</text></svg>7G</td>"),
        ("td 9<math>67", "<td>バベル9<math><mn>67</mn></math>G</td>"),
        ("td 9<textarea>67", "<td>バベル9<textarea>67</textarea>G</td>"),
        ("td select 96>7", "<td><select><option>96</option></select>7G</td>"),
    ):
        _, _, wt = pu("<table><tr>" + frag + "</tr></table>")
        t("★第16次C1: 表セル内 %s の値分割 → opaque_in_digit_run（表collector↔shadow双方向）" % label,
          "opaque_in_digit_run" in wt)
    t("★第16次C1: 別セルの数字は opaque_in_digit_run 誤発火しない（セル境界でreset）",
      "opaque_in_digit_run" not in pu(
          "<table><tr><td>9</td><td><svg><text>67</text></svg></td></tr></table>")[2])
    mock_page("https://chonborista.com/v16-c1",
              "<h1>アクダマ</h1><p>" + P967 + "</p>"
              "<table><tr><td>バベルの天井は9<svg><text>67</text></svg>G</td></tr></table>", title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/v16-c1", P967, raw="967"), IDENT)
    t("★第16次C1: 本文967＋表セル内svg分割967 → REVIEW(PARSE_WARNINGS)",
      r["verified"] is False and r["c5_code"] == "PARSE_WARNINGS")
    # (C2) 巨大な不正終了タグ </   div …>・</1…> も overlong_tag で feed前検出（HTMLParser準拠で `>` まで）
    _, _, we1 = pu("</   div " + ("A" * 2_000_000) + ">")
    _, _, we2 = pu("</1" + ("A" * 2_000_000) + ">")
    t("★第16次C2: 巨大な不正終了タグ(</ div…/</1…)も overlong_tag（事前検査を迂回させない）",
      "overlong_tag" in we1 and "overlong_tag" in we2)

    # ══════════════════════════════════════════════════════════════
    # ★Codex第17次Critical1＝ruby注釈内の可視skip(svg/math/textarea/select)がshadowから消失 ★
    # ══════════════════════════════════════════════════════════════
    for label, inner in (("svg", "<svg><text>967</text></svg>"), ("math", "<math><mn>967</mn></math>"),
                         ("textarea", "<textarea>967</textarea>"),
                         ("select", "<select><option>967</option></select>")):
        t("★第17次C1: 注釈内の可視skip %s の値も shadow に退避（消失しない）" % label,
          _unit_contains_value(sh("<p>x<ruby>A<rt>%s</rt></ruby></p>" % inner), "967"))
        t("★第17次C1: 表セル内の注釈内可視skip %s の値も shadow に退避" % label,
          _unit_contains_value(sh("<table><tr><td><ruby>注<rt>%s</rt></ruby></td></tr></table>" % inner), "967"))
    mock_page("https://chonborista.com/v17-c1",
              "<h1>アクダマ</h1><p>" + P967 + "</p>"
              "<p>x<ruby>A<rt><svg><text>967</text></svg></rt></ruby></p>", title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/v17-c1", P967, raw="967"), IDENT)
    t("★第17次C1: 本文967＋注釈内svg隠れ967 → REVIEW(VALUE_IN_SHADOW)",
      r["verified"] is False and r["c5_code"] == "VALUE_IN_SHADOW")
    # 注釈内可視skipは基底runを汚さない（正常な段落内svgは基底runに参加＝回帰確認）
    t("★第17次C1: 通常段落内svgは基底run参加（opaque_in_digit_run）・注釈内svgは非影響",
      "opaque_in_digit_run" in pu("<p>天井9<svg><text>67</text></svg></p>")[2])
    # ★第18次(a)追補: 注釈内skipを挟んでも基底テキストは unit 上で連結され、注釈は基底runを上書きしない
    uu2 = pu("<p>天井は<ruby>9<rt><svg><text>X</text></svg></rt>67</ruby>G</p>")[0]
    t("★第18次(a): 注釈内skipを挟んだ基底は unit 上で 967 のまま（注釈が基底を分断しない）",
      any("967" in x.text for x in uu2))
    t("★第18次(a): 注釈内skipの直後にopaque sinkが来ても基底runは保たれ opaque_in_digit_run が出る",
      "opaque_in_digit_run" in pu(
          "<p>天井は<ruby>9<rt><svg><text>X</text></svg></rt></ruby><x-custom>67</x-custom>G</p>")[2])
    tc2 = find_cell("<table><tr><td>天井は<ruby>9<rt><svg><text>X</text></svg></rt>67</ruby>G</td></tr></table>", "967")
    t("★第18次(a): 表セルでも注釈内skipを挟んだ基底は 967 のまま",
      tc2 is not None and "967" in tc2.text)
    # ★第18次(a): rt→rp兄弟暗黙終了・</ruby>・EOF pop 後に annot/vskip depth が戻る（後続本文が通常捕捉される）
    for label, html in (
        ("rt→rp兄弟暗黙", "<p><ruby>A<rt><svg><text>X</text></svg><rp>(</rp></ruby>後続967G</p>"),
        ("</ruby>暗黙", "<p><ruby>A<rt><svg><text>X</text></svg></ruby>後続967G</p>"),
        ("EOF", "<p><ruby>A<rt><svg><text>X</text></svg>"),
    ):
        uu3 = pu(html)[0]
        ok = True if label == "EOF" else any("967" in x.text for x in uu3)
        t("★第18次(a): %s 後に annot/vskip depth が戻り後続本文を通常捕捉" % label, ok)

    # ══════════════════════════════════════════════════════════════
    # ★Codex第18次C2＝CSS display で連結/分断が反転し得る＝要素境界を跨いで構成される数値は fail-closed REVIEW ★
    #   （html.parser には computed style が無いため、JOIN固定は「表示上存在しない値の捏造」になり得る）
    # ══════════════════════════════════════════════════════════════
    for label, frag in (
        ("span(display:block)分断", '<span style="display:block">9</span><span style="display:block">67</span>'),
        ("素のspan分割", "<span>9</span><span>67</span>"),
        ("img挟み", "9<img src=x>67"),
        ("b挟み", "9<b>6</b>7"),
    ):
        t("★第18次C2: %s は要素境界跨ぎ構成 → opaque_in_digit_run（CSS反転に対し fail-closed）" % label,
          "opaque_in_digit_run" in pu("<p>天井は%sG</p>" % frag)[2])
    # 不可視（コメント/宣言/PI）だけを挟む場合は境界にしない＝連続扱い（過剰REVIEWしない）
    t("★第18次C2: コメントのみを挟む数字は連続扱い（967検出・誤発火しない）",
      "opaque_in_digit_run" not in pu("<p>天井は9<!--c-->67G</p>")[2]
      and any("967" in x.text for x in pu("<p>天井は9<!--c-->67G</p>")[0]))
    # 通常の本文値・数値全体の強調は誤発火しない（自動化率を不必要に落とさない）
    t("★第18次C2: 通常の 967G / <strong>967</strong>G は誤発火しない",
      "opaque_in_digit_run" not in pu("<p>アクダマの通常時の天井は967G</p>")[2]
      and "opaque_in_digit_run" not in pu("<p>天井は<strong>967</strong>Gです</p>")[2])
    mock_page("https://chonborista.com/v18-c2",
              "<h1>アクダマ</h1><p>" + P967 + "</p>"
              '<p>バベルの天井は<span style="display:block">9</span><span style="display:block">67</span>G</p>',
              title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/v18-c2", P967, raw="967"), IDENT)
    t("★第18次C2: 本文967＋CSSで分断され得る境界跨ぎ967 → REVIEW(PARSE_WARNINGS)",
      r["verified"] is False and r["c5_code"] == "PARSE_WARNINGS")

    # ══════════════════════════════════════════════════════════════
    # ★Codex第19次Critical＝hidden属性/inline display:none の「表示されない値」を正の証拠にしない ★
    #   unitにせず shadow へ回す＝自動公開しない。値照合はされるので VALUE_IN_SHADOW でREVIEW。
    # ══════════════════════════════════════════════════════════════
    for label, frag in (
        ("hidden属性", '<p hidden>アクダマの通常時の天井は967Gで当選</p>'),
        ("display:none", '<p style="display:none">アクダマの通常時の天井は967Gで当選</p>'),
        ("visibility:hidden", '<p style="color:red;visibility:hidden">アクダマの天井は967G</p>'),
        ("表セルdisplay:none", '<table><tr><td style="display:none">アクダマの天井967G</td></tr></table>'),
    ):
        uu4, _, _ = pu(frag)
        t("★第19次: %s の値は unit にならず shadow へ（表示されない値を正の証拠にしない）" % label,
          all("967" not in x.text for x in uu4) and _unit_contains_value(sh(frag), "967"))
    # 非表示要素にしか値が無い場合、その値は自動PASSしない（正の証拠にならない）
    mock_page("https://chonborista.com/v19-hidden",
              "<h1>アクダマ</h1><p>アクダマの解析ページです。</p>"
              "<p hidden>アクダマの通常時の天井は967Gで当選</p>", title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/v19-hidden",
                             "アクダマの通常時の天井は967Gで当選", raw="967"), IDENT)
    t("★第19次: 非表示要素にしか無い値は verified にならない（表示されていない値を公開しない）",
      r["verified"] is False)
    # 本文の正常値＋非表示の同値 → REVIEW(VALUE_IN_SHADOW)
    mock_page("https://chonborista.com/v19-hidden2",
              "<h1>アクダマ</h1><p>" + P967 + "</p>"
              '<p style="display:none">バベルの天井は967G</p>', title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/v19-hidden2", P967, raw="967"), IDENT)
    t("★第19次: 本文967＋非表示の別文脈967 → REVIEW(VALUE_IN_SHADOW)",
      r["verified"] is False and r["c5_code"] == "VALUE_IN_SHADOW")
    # 通常要素・display:block 等は従来どおり unit（過剰に非表示扱いしない＝回帰なし）
    t("★第19次: 通常要素/display:block は従来どおり unit（非表示扱いしない）",
      any("967" in x.text for x in pu('<p style="display:block;color:red">天井967G</p>')[0])
      and any("967" in x.text for x in pu("<p>アクダマの通常時の天井は967G</p>")[0]))

    # ══════════════════════════════════════════════════════════════
    # ★Codex第20次Critical＝style属性は有界CSS宣言パーサで解釈（!important/コメント/文字列/エスケープ/後勝ち）★
    # ══════════════════════════════════════════════════════════════
    def _sty_hidden(html):
        """unitに入らず shadow に入る＝「表示されない」と判定されたか。"""
        uu, _, _ = pu(html)
        return all("967" not in x.text for x in uu) and _unit_contains_value(sh(html), "967")

    for label, styl, expect in (
        ("display:none!important", 'display:none!important', True),
        ("none!important;display:block（importantが勝つ）", 'display:none!important;display:block', True),
        ("display/**/:none（コメント）", 'display/**/:none', True),
        ("d\\69splay:none（プロパティのCSSエスケープ）", "d\\69splay:none", True),
        ("display:n\\6fne（値のCSSエスケープ）", "display:n\\6fne", True),
        ("visibility:hidden!important", 'visibility:hidden!important', True),
        ("DISPLAY : NONE（大小/空白）", 'DISPLAY : NONE', True),
        ("文字列内の;display:none;（誤検知しない）", 'content:";display:none;";display:block', False),
        ("display:none;display:block（後勝ち）", 'display:none;display:block', False),
        ("display:block;color:red", 'display:block;color:red', False),
        ("border:none（誤検知しない）", 'border:none', False),
    ):
        html = "<p style='%s'>天井967G</p>" % styl if '"' in styl else '<p style="%s">天井967G</p>' % styl
        t("★第20次: style %s → 非表示=%s（CSS宣言パーサで正しく解決）" % (label, expect),
          _sty_hidden(html) is expect)
    t("★第20次: data-hidden 属性は非表示扱いしない（属性名の完全一致）",
      not _sty_hidden('<p data-hidden="1">天井967G</p>'))
    # 解釈不能（未閉じコメント/未閉じ文字列）・上限超過は style_unparseable → doc REVIEW
    t("★第20次: 未閉じコメント/未閉じ文字列/巨大styleは style_unparseable（表示状態不明→doc REVIEW）",
      "style_unparseable" in pu('<p style="display:none/*x">967</p>')[2]
      and "style_unparseable" in pu("<p style=\"content:'x;display:block\">967</p>")[2]
      and "style_unparseable" in pu('<p style="' + ("a:b;" * 3000) + '">967</p>')[2])
    # E2E: !important 非表示の別文脈967 → REVIEW（正の証拠にしない）
    mock_page("https://chonborista.com/v20-imp",
              "<h1>アクダマ</h1><p>" + P967 + "</p>"
              '<p style="display:none!important">バベルの天井は967G</p>', title="アクダマ 天井")
    r = V("ceiling.normal", ev(967, "https://chonborista.com/v20-imp", P967, raw="967"), IDENT)
    t("★第20次: 本文967＋display:none!important の別文脈967 → REVIEW(VALUE_IN_SHADOW)",
      r["verified"] is False and r["c5_code"] == "VALUE_IN_SHADOW")
    # ══════════════════════════════════════════════════════════════
    # ★Codex第21次C1/C2/Major＝CSS宣言の妥当性・collapse・var()・括弧/エスケープ/引用符 ★
    # ══════════════════════════════════════════════════════════════
    _BS = chr(92)
    for styl, exp_hidden, exp_ok, why in (
        # ★第22次C4: 分類不能な値（bogus/引用符付き/不正な多値）は「無視」と断定せず安全側REVIEW（ok=False）
        ("display:none; display:bogus", False, False, "分類不能値＝断定せずREVIEW"),
        ("display:none!important; display:bogus!important", False, False, "同上（important でも）"),
        ('display:none; display:"block"', False, False, "引用符付き＝分類不能→REVIEW"),
        ("visibility:hidden; visibility:bogus", False, False, "visibility も分類不能→REVIEW"),
        ("display:none; display:block none", False, False, "不正な多値display＝分類不能→REVIEW"),
        ("visibility:collapse", True, True, "collapse も非表示相当"),
        ("--d:none; display:var(--d)", False, False, "var()＝静的に確定しない→REVIEW"),
        ("display:var(--d, none)", False, False, "var()フォールバックも→REVIEW"),
        ("display:inherit", False, False, "global値＝計算値不定→REVIEW"),
        ("display:revert-layer", False, False, "revert-layer も→REVIEW"),
        ("background:url(x;display:none;y)", False, True, "括弧内の ; で分割しない（誤検知なし）"),
        ("color:red" + _BS + ";display:none", False, True, "文字列外エスケープ \\; で分割しない"),
        ('display:"none"', False, False, "引用符付き＝分類不能→REVIEW"),
        ("display:block flow", False, True, "正規形の多値displayは有効・非表示でない"),
        ("display:block(]", False, False, "括弧の種類不一致＝解釈不能→REVIEW"),
        ("displ" + _BS + "\nay:none", True, True, "CSS行継続(\\改行)を除去して display:none と解釈"),
        ("display:block;displ" + _BS + "\nay:none", True, True, "行継続の後勝ちで非表示"),
    ):
        _h, _ok = _style_hides(styl)
        t("★第21次: CSS宣言 %s → hidden=%s ok=%s（%s）" % (styl[:34], exp_hidden, exp_ok, why),
          _h is exp_hidden and _ok is exp_ok)

    # ══════════════════════════════════════════════════════════════
    # ★Codex第21次＝rendered 監査証跡ゲートの「機械的強制」（定数の宣言でなく実際に verified を止める）★
    #   外部/埋込CSSによる非表示は静的解析で判定不能 → 実表示テキストでの確認を必須にする。
    # ══════════════════════════════════════════════════════════════
    _AURL = "https://chonborista.com/att"
    mock_page(_AURL, "Lアクダマドライブ。" + Q_AT967 + "。", title="アクダマ 天井")
    _good = mock_att(_AURL)
    t("★第21次ゲート: 証跡ありなら従来どおり verified=True（正常系の回帰なし）",
      V("ceiling.normal.at", ev(967, _AURL, Q_AT967), IDENT,
        rendered_attestation=_good)["verified"] is True)
    t("★第21次ゲート: 証跡なし → verified=False(RENDERED_ATTESTATION_REQUIRED)",
      V("ceiling.normal.at", ev(967, _AURL, Q_AT967), IDENT,
        rendered_attestation=None)["c5_code"] == "RENDERED_ATTESTATION_REQUIRED")
    t("★第21次ゲート: 別ページの証跡流用 → ATTESTATION_URL_MISMATCH",
      V("ceiling.normal.at", ev(967, _AURL, Q_AT967), IDENT,
        rendered_attestation=dict(_good, final_url="https://other.test/x"))["c5_code"]
      == "ATTESTATION_URL_MISMATCH")
    t("★第21次ゲート: 取得スナップショットと不一致のhash → ATTESTATION_HASH_MISMATCH",
      V("ceiling.normal.at", ev(967, _AURL, Q_AT967), IDENT,
        rendered_attestation=dict(_good, html_sha256="deadbeef"))["c5_code"]
      == "ATTESTATION_HASH_MISMATCH")
    # ★核心: HTMLソースには在るが「実表示テキスト」に無い（外部CSSで display:none 等）→ verified にしない
    t("★第21次ゲート: 実表示テキストに値が無い(外部CSSで非表示) → ATTESTATION_VALUE_NOT_VISIBLE",
      V("ceiling.normal.at", ev(967, _AURL, Q_AT967), IDENT,
        rendered_attestation=dict(_good, visible_text="アクダマの解析ページ（天井の記載なし）"))["c5_code"]
      == "ATTESTATION_VALUE_NOT_VISIBLE")
    t("★第21次ゲート: 実表示テキストに機種同定が無い → ATTESTATION_IDENT_NOT_VISIBLE",
      V("ceiling.normal.at", ev(967, _AURL, Q_AT967), IDENT,
        rendered_attestation=dict(_good, visible_text="通常時の天井はAT間967Gで当選"))["c5_code"]
      == "ATTESTATION_IDENT_NOT_VISIBLE")
    t("★第21次ゲート: make_verifier で attest_fn 未指定なら verified=True に到達しない（fail-closed）",
      make_verifier("ceiling.normal.at", IDENT)(
          anomaly_key_of("ceiling.normal.at"), ev(967, _AURL, Q_AT967))["verified"] is False)
    t("★第21次ゲート: 契約定数も明文化されている（本番自動公開は rendered DOM 必須）",
      REQUIRES_RENDERED_DOM_BEFORE_AUTOPUBLISH is True)

    # ══════════════════════════════════════════════════════════════
    # ★Codex第22次C3＝attest_fn は「URL安全検査＋取得＋最終URL再検査」の後にだけ呼ぶ（SSRF/DoS防止）★
    # ══════════════════════════════════════════════════════════════
    _att_calls = []

    def _spy_attest(u):
        _att_calls.append(u)
        return None

    del _att_calls[:]
    r = verify_evidence("ceiling.normal.at",
                        ev(967, "https://web.archive.org/x", Q_AT967), IDENT,
                        expect_item_key=anomaly_key_of("ceiling.normal.at"), attest_fn=_spy_attest)
    t("★第22次C3: blocked source は C0 で弾き attest_fn を呼ばない（ブラウザを踏ませない）",
      r["c5_code"] == "C0_BLOCKED_SOURCE" and _att_calls == [])
    del _att_calls[:]
    r = verify_evidence("ceiling.normal.at", ev(967, "https://evil.test/x", Q_AT967), IDENT,
                        allowed_domains=["chonborista.com"],
                        expect_item_key=anomaly_key_of("ceiling.normal.at"), attest_fn=_spy_attest)
    t("★第22次C3: 許可ドメイン外も C0 で弾き attest_fn を呼ばない",
      r["c5_code"] == "C0_DOMAIN_NOT_ALLOWED" and _att_calls == [])
    del _att_calls[:]
    r = verify_evidence("ceiling.normal.at", ev(967, _AURL, Q_AT967), IDENT,
                        expect_item_key=anomaly_key_of("ceiling.normal.at"), attest_fn=_spy_attest)
    t("★第22次C3: 検査通過後は「検証済みの最終URL」で attest_fn を1回だけ呼ぶ",
      _att_calls == [_AURL] and r["c5_code"] == "RENDERED_ATTESTATION_REQUIRED")

    # ★第22次M3: 証跡入力（外部入力）の上限＝巨大 visible_text / identity 過多は ATTESTATION_TOO_LARGE
    t("★第22次M3: 巨大な visible_text は ATTESTATION_TOO_LARGE（証跡入力のDoS上限）",
      V("ceiling.normal.at", ev(967, _AURL, Q_AT967), IDENT,
        rendered_attestation=dict(_good, visible_text="あ" * (_MAX_ATTEST_TEXT + 1)))["c5_code"]
      == "ATTESTATION_TOO_LARGE")
    t("★第22次M3: identity 過多も ATTESTATION_TOO_LARGE",
      V("ceiling.normal.at", ev(967, _AURL, Q_AT967),
        ["アクダマ"] * (_MAX_ATTEST_IDENTS + 1),
        rendered_attestation=_good)["c5_code"] == "ATTESTATION_TOO_LARGE")

    # ★取得は1検証で厳密に1回だけ（二重取得しない）をスパイで直接保証（Codex非ブロッキング提案）
    _calls = {"n": 0}
    def _spy_fetch(url, allowed=None):
        _calls["n"] += 1
        return vc.fetch_html(url, allowed=allowed)
    V("ceiling.normal.at", ev(967, U1, Q_AT967), IDENT, fetch_fn=_spy_fetch)
    t("★二重化: 1検証で取得は厳密に1回（二重取得しない）", _calls["n"] == 1)

    # ── E2E: verify検証器 × resolve_with_dialogue ──
    fk = "ceiling.normal.at"
    verify = make_verifier(fk, IDENT, attest_fn=mock_att)
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

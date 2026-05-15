"""全HTMLのインラインstyleをCSSクラスに置換する一括スクリプト"""
import re
from pathlib import Path
BASE = Path(__file__).resolve().parent.parent

# 置換マッピング：style値 → 追加するクラス
STYLE_TO_CLASS = {
    "margin-top:8px;": "spacing-sm",
    "margin-top: 8px;": "spacing-sm",
    "margin-top:12px;": "spacing-md",
    "margin-top: 12px;": "spacing-md",
    "margin-bottom:12px;": "spacing-md",
    "width:20%": "cell-20",
    "width: 20%": "cell-20",
    "display:none;": "is-hidden",
    "display: none;": "is-hidden",
    "color:var(--muted2);font-size:10px;": "note-small",
    "color: var(--muted2); font-size: 10px;": "note-small",
    "color:var(--gold);font-weight:800;": "note-strong",
    "color: var(--gold); font-weight: 800;": "note-strong",
}


def replace_in_tag(tag_match, style_val):
    """タグ内の style="..." をclass追加に置換"""
    if style_val not in STYLE_TO_CLASS:
        return None
    new_class = STYLE_TO_CLASS[style_val]
    full_tag = tag_match.group(0)
    # 既存class があれば追記、無ければ新規追加
    class_match = re.search(r'class="([^"]*)"', full_tag)
    if class_match:
        existing = class_match.group(1)
        if new_class in existing.split():
            new_full = re.sub(r'\s*style="[^"]+"', "", full_tag, count=1)
        else:
            new_full = re.sub(r'\s*style="[^"]+"', "", full_tag, count=1)
            new_full = new_full.replace(
                f'class="{existing}"',
                f'class="{existing} {new_class}"',
                1,
            )
    else:
        # styleをclass属性に置き換え
        new_full = re.sub(r'style="[^"]+"', f'class="{new_class}"', full_tag, count=1)
    return new_full


def process_file(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    count = 0

    def repl(m):
        nonlocal count
        full = m.group(0)
        style_val = m.group(1)
        new_tag = replace_in_tag(m, style_val)
        if new_tag is None:
            return full
        count += 1
        return new_tag

    # 全タグの style="..." を順に処理
    new_text = re.sub(r'<\w+[^>]*?style="([^"]+)"[^>]*>', repl, text)
    if count:
        path.write_text(new_text, encoding="utf-8")
    return count


def main():
    targets = list(BASE.glob("*.html"))
    total = 0
    for p in targets:
        c = process_file(p)
        if c:
            print(f"{p.name}: {c}箇所書き換え")
            total += c
    print(f"\n合計 {total}箇所")


if __name__ == "__main__":
    main()

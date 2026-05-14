"""記事本文・HTMLから他サイト名を一括削除"""
import re, json
from pathlib import Path
BASE = Path(__file__).resolve().parent.parent

# (regex, replacement) 順序重要：長いパターン→短いパターン
PATTERNS = [
    # スロパチクエスト系（カッコごと削除）
    (re.compile(r'（スロパチクエスト[暫定確認]*基準）'), ''),
    (re.compile(r'（スロパチクエスト基準）'), ''),
    (re.compile(r'（スロパチクエスト確認値）'), ''),
    (re.compile(r'（スロパチクエスト暫定基準）'), ''),
    (re.compile(r'・スロパチクエスト基準'), ''),
    (re.compile(r'【スロパチクエスト基準】'), ''),
    (re.compile(r'スロパチクエスト基準の数値を参考にしているため、'), '当サイト基準ですので、'),
    (re.compile(r'スロパチクエスト基準の狙い目は'), '狙い目は'),
    (re.compile(r'スロパチクエスト基準の'), ''),
    (re.compile(r'スロパチクエスト'), ''),
    # ちょんぼりすた系
    (re.compile(r'（ちょんぼりすた[^）]*）'), ''),
    (re.compile(r'ちょんぼりすた'), '解析サイト'),
    # 余計に発生する連続スペース・空カッコの掃除
    (re.compile(r'（\s*）'), ''),
    (re.compile(r'\s+'), ' '),  # ただし重要：JSON内では使えない
]

def clean_text(text: str) -> str:
    for pat, rep in PATTERNS[:-1]:  # 最後の余分なspace掃除は別途
        text = pat.sub(rep, text)
    return text

def main():
    # machine-details
    detail_dir = BASE / "assets" / "data" / "machine-details"
    changed_machines = 0
    for jf in sorted(detail_dir.glob('*.json')):
        d = json.loads(jf.read_text(encoding='utf-8'))
        changed = False
        # lead
        if d.get('lead'):
            new = clean_text(d['lead'])
            if new != d['lead']:
                d['lead'] = new
                changed = True
        # sections
        for s in d.get('sections', []):
            body = s.get('body')
            if isinstance(body, list):
                for i, item in enumerate(body):
                    if isinstance(item, str):
                        new = clean_text(item)
                        if new != item:
                            body[i] = new
                            changed = True
            elif isinstance(body, str):
                new = clean_text(body)
                if new != body:
                    s['body'] = new
                    changed = True
        if changed:
            jf.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding='utf-8')
            changed_machines += 1
            print(f'{jf.stem}: 書き換え')
    print(f'\n機種記事: {changed_machines}機種')

    # contact.html: スロラボ → 他社の
    cont = BASE / 'contact.html'
    text = cont.read_text(encoding='utf-8')
    new = text.replace('スロラボ等の有料情報源', '他社の有料情報源')
    if new != text:
        cont.write_text(new, encoding='utf-8')
        print('contact.html: 書き換え')

if __name__ == '__main__':
    main()

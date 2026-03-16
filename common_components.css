# 新機種追加手順

## いちばん簡単な流れ

1. `templates/article-template.html` を複製して `slug_article.html` に改名
2. `templates/checker-template.html` を複製して `slug_checker.html` に改名
3. 2つのHTMLの `{{機種名}}` と `{{slug}}` を置換
4. `assets/data/machines.json` に1件追加
5. GitHub に4ファイルをアップロード

---

## machines.json に追加する形

```json
{
  "slug": "example_machine",
  "name": "機種名サンプル",
  "mode": "separated",
  "sub": "既存構成",
  "article": "example_machine_article.html",
  "checker": "example_machine_checker.html"
}
```

---

## 統合ページの場合

```json
{
  "slug": "example_machine",
  "name": "機種名サンプル",
  "mode": "integrated",
  "sub": "新方式 / 記事＋チェッカー一体型",
  "url": "example_machine.html"
}
```

---

## checker を量産しやすくするコツ

- まずは `assets/js/checker-common.js` をそのまま使う
- 判定ラインだけ変えるならテンプレート内の `thresholds` を差し替える
- リセット時や特殊条件がある機種だけ `judge()` の分岐を追加する
- 複雑になったら `assets/data/checkers/` に機種別メモを置く

---

## GitHub に上げるとき

アップロードするのは ZIP ではなく、解凍した中身です。

- `index.html`
- `assets/`
- `templates/`
- 追加した `*_article.html`
- 追加した `*_checker.html`

ルート直下に配置されていれば GitHub Pages でそのまま動きます。

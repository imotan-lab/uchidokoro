# 狙い目手帖

GitHub Pages 用の静的サイトです。

## 現在の整理方針

- トップの掲載機種一覧は `assets/data/machines.json` で管理
- `index.html` は JSON を読み込んで一覧を自動描画
- 新機種追加は `templates/` のテンプレートを複製して対応
- checker は `assets/js/checker-common.js` を土台にして量産しやすくする

## 新機種追加の最短手順

1. `templates/article-template.html` を複製
2. `templates/checker-template.html` を複製
3. 機種名と slug を置換
4. `assets/data/machines.json` に追加
5. GitHub にアップロード

詳しくは `docs/ADD_MACHINE_GUIDE.md` を参照。

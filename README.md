# うちどころ。

GitHub Pages で運用する静的スロット攻略サイトです。  
公開URL: https://imotan-lab.github.io/uchidokoro/

## 技術構成

- HTML / CSS（assets/css/practical.css）/ JavaScript
- 機種一覧データ：assets/data/machines.json
- 機種詳細記事データ：assets/data/machine-details/機種名.json
- Google Fonts（Orbitron）

## ファイル構成

- `index.html` トップページ
- `machine.html` 記事ページ（slugで機種切り替え）
- `checker.html` チェッカーページ（slugで機種切り替え）
- `setting.html` 設定判別ページ「ポチポチくん」（slugで機種切り替え）
- `assets/css/practical.css` 全ページ共通CSS
- `assets/data/machines.json` 機種一覧
- `assets/data/machine-details/` 機種別記事データ（JSON）

## 新機種追加の手順

1. `assets/data/machines.json` に機種データを追加
2. `assets/data/machine-details/機種名.json` を作成
3. `machines/機種名/index.html`・`checker.html` を追加（リダイレクト用）
4. `sitemap.xml` にURLを追加
5. GitHubにプッシュ

## 自動更新

GitHub Actions により毎日UTC18:00（日本時間3:00）にYouTube APIで人気ランキングを自動更新。

## 運用ドキュメント

- 作業ルール: `CODEX_RULES.txt`
- 引き継ぎメモ: `HANDOFF.md`

日をまたいで作業するときや 別のAIへ引き継ぐときは 上の2つを先に確認すること。

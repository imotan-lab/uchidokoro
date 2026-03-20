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

- 作業ルール（正本）: `CODEX_RULES.md`
- 引き継ぎメモ: `HANDOFF.md`
- この2つに `README.md` を加えた3ファイル全体を 通称「ルール」として扱う

日をまたいで作業するときや 別のAIへ引き継ぐときは 上の2つを先に確認すること。

現在は 全機種分の記事ページ枠はそろっており 今後は既存記事の厚み追加とブラッシュアップをまとめて進める段階。

## 次スレ開始用メモ

- 次の再開地点は 交換率4パターン対応を全対象機種へ入れ終えた後の最終確認と微調整
- `sf5` は設定狙い専用のため 交換率セレクター対象外のまま
- 新スレッドでは最初に `CODEX_RULES.md` と `HANDOFF.md` を確認する

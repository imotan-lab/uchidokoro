# うちどころ。

GitHub Pages で運用する静的スロット狙い目サイトです。
公開URL: https://imotan-lab.github.io/uchidokoro/

## 技術構成

- HTML / CSS（assets/css/practical.css）/ JavaScript
- 機種一覧データ：assets/data/machines.json
- 機種詳細記事データ：assets/data/machine-details/機種名.json
- 設定判別データ：setting.html 内の MACHINE_CONFIGS（ベイズ推定）
- Google Fonts（Orbitron）
- PWA対応（service-worker.js / manifest.json）
- GitHub Actions による人気ランキング自動更新

## 主な機能

- 狙い目チェッカー（交換率4パターン対応・モード切り替え・スルー回数/周期カウンター）
- 期待値早見表（閾値から色分けテーブルを自動生成）
- 設定判別ツール「ポチポチくん」（16機種対応・ベイズ推定でリアルタイム期待度表示）
- 設定示唆まとめ（バッジ色分け＋凡例自動生成）
- 狙い目早見表（モード別入口ライン）と基本情報（スペック補足）の役割分離

## ファイル構成

- `index.html` トップページ（機種一覧・検索・人気TOP10）
- `machine.html` 記事ページ（全機種共通・slugで切り替え）
- `setting.html` ポチポチくん（全機種共通・slugで切り替え）
- `about.html` このサイトについて（運営者情報・免責事項）
- `contact.html` お問い合わせページ（Googleフォーム埋め込み）
- `privacy.html` プライバシーポリシーページ
- `404.html` 404ページ
- `meta-auto.js` title/meta descriptionを機種ごとに動的生成
- `manifest.json` PWA設定
- `service-worker.js` オフラインキャッシュ
- `sitemap.xml` サイトマップ
- `robots.txt` クローラー向け設定
- `assets/css/practical.css` 全ページ共通CSS（唯一のCSS）
- `assets/data/machines.json` 機種一覧（交換率別データ含む）
- `assets/data/machine-details/` 機種別記事データ（JSON・全18機種）
- `machines/機種名/index.html` 直アクセス用リダイレクト

## 対応機種（全18機種）

北斗の拳 / 北斗転生2 / SF5 / チバリヨ2 / 番長4 / カバネリ / モンキーターンV / ゴブリンスレイヤー / 鉄拳6 / 東京喰種 / 攻殻機動隊 / バイオハザードRE2 / ゴッドイーター / バキ / 転スラ / ダンベル何キロ持てる / かぐや様 / ヴァルヴレイヴ2

- SF5 は設定狙い専用（交換率セレクター対象外）
- かぐや様・ヴァルヴレイヴ2 はポチポチくん非対応（設定差が小さいため）

## 新機種追加の手順

1. `assets/data/machines.json` に機種データを追加（交換率別 byRate 含む）
2. `assets/data/machine-details/機種名.json` を作成
3. `machines/機種名/index.html` を追加（リダイレクト用）
4. `setting.html` の MACHINE_CONFIGS に確率テーブルを追加（対応可能な場合）
5. `sitemap.xml` にURLを追加
6. GitHubにプッシュ

## 自動更新

GitHub Actions により毎日UTC18:00（日本時間3:00）にYouTube APIで人気ランキングを自動更新。

## 運用ドキュメント

- 作業ルール（正本）: `CODEX_RULES.md`（ローカルにのみ存在・GitHubから除外済み）
- 引き継ぎメモ: `HANDOFF.md`（ローカルにのみ存在・GitHubから除外済み）
- この2つに `README.md` を加えた3ファイル全体を通称「ルール」として扱う

新スレッドや初回着手時は `CODEX_RULES.md` → `HANDOFF.md` の順で確認してから作業を開始すること。

## 現在の状態（2026-03-23時点）

- 全18機種の記事データJSON・チェッカー・ポチポチくん完成済み
- 交換率4パターン対応は sf5 を除く17機種で実装済み
- 設定示唆バッジ凡例・早見表と基本情報の役割整理も完了
- 補助ページ整備完了（about.html / contact.html / privacy.html / 404.html）
- お問い合わせページはGoogleフォーム埋め込み・ダークテーマ対応済み
- 今後は既存記事の精度と厚みを上げる段階

## 残タスク

- 記事内容の最終ブラッシュアップ（情報源との突き合わせ）
- SEO強化（OGPタグ整備など）
- AdSense申請（補助ページ整備済み・コンテンツ充実後に申請）

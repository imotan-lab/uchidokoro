# うちどころ。

GitHub Pages で運用する静的スロット狙い目サイトです。
公開URL: https://uchidokoro.com/

## 技術構成

- HTML / CSS（assets/css/practical.css）/ JavaScript
- 機種一覧データ：assets/data/machines.json
- 機種詳細記事データ：assets/data/machine-details/機種名.json
- 小役カウンター：setting.html 内の MACHINE_CONFIGS（ベイズ推定）
- Google Fonts（Orbitron）
- PWA対応（service-worker.js / manifest.json）

## 主な機能

- 狙い目チェッカー（交換率4パターン対応・モード切り替え・スルー回数/周期カウンター）
- 期待値早見表（閾値から色分けテーブルを自動生成）
- 小役カウンター「ポチポチくん」（36機種対応・ベイズ推定でリアルタイム期待度表示）
- 設定示唆まとめ（バッジ色分け＋凡例自動生成）
- 狙い目早見表（モード別入口ライン）と基本情報（スペック補足）の役割分離
- 先行記事モード（解析待ち機種のSEO先行公開）

## ファイル構成

- `index.html` トップページ（機種一覧・検索・人気ランキング）
- `machine.html` 記事ページ（全機種共通・slugで切り替え）
- `setting.html` 小役カウンター ポチポチくん（slugで切り替え）
- `about.html` このサイトについて（運営者情報・免責事項）
- `contact.html` お問い合わせページ（Googleフォーム埋め込み）
- `privacy.html` プライバシーポリシーページ
- `404.html` 404ページ（旧サブパスURLの自動リダイレクト処理含む）
- `meta-auto.js` title / meta description / canonical / JSON-LD を機種ごとに動的生成
- `manifest.json` PWA設定
- `service-worker.js` オフラインキャッシュ（変更時にキャッシュバージョンを上げる）
- `sitemap.xml` サイトマップ
- `robots.txt` クローラー向け設定
- `favicon.ico` ファビコン（「う。」ゴールド）
- `assets/css/practical.css` 全ページ共通CSS（唯一のCSS）
- `assets/data/machines.json` 機種一覧（交換率別データ含む）
- `assets/data/machine-details/` 機種別記事データ（JSON）
- `machines/機種名/index.html` 直アクセス用リダイレクト（canonical指定済）
- `scripts/post_to_x.py` X新台投稿スクリプト（@uchidokoro）
- `scripts/post_update_to_x.py` X更新告知スクリプト

## 対応機種（全104機種）

機種数の最新値は `assets/data/machines.json` のエントリ数で確認してください。
カテゴリ内訳（machines.json の `info` フィールド準拠）：

| カテゴリ | 機種数 |
|---|---|
| スマスロAT系 | 78 |
| Aタイプ（ジャグラー系含む） | 7 |
| スマスロBT | 7 |
| スマスロノーマル | 3 |
| 疑似ボーナス | 3 |
| スマスロA+RT / ART | 2 |
| スマスロST | 2 |
| A+BT | 1 |
| その他（スマスロ） | 1 |
| **合計** | **104** |

ポチポチくん対応：36機種（setting.html の MACHINE_CONFIGS に登録）

## 新機種追加の手順

1. `assets/data/machines.json` に機種データを追加（`aliases` 必須・交換率別 `byRate` 含む）
2. `assets/data/machine-details/機種名.json` を作成（`"type":"rumor"` の噂セクション必須）
3. `machines/機種名/index.html` を追加（リダイレクト用・canonical指定済テンプレ）
4. `setting.html` の MACHINE_CONFIGS に確率テーブルを追加（設定差がある機種のみ）
5. `sitemap.xml` に記事ページURL・ポチポチくんURLを追加
6. `service-worker.js` のキャッシュバージョンを上げる
7. `README.md` の機種数を更新（このファイル）
8. `index.html` のポチポチくん非対応機種リスト（該当する場合のみ）
9. `about.html` の対象機種数を更新
10. GitHubにプッシュ

## 自動化

新台検出・先行記事公開・解析データ追記・X投稿は会社PCの Claude Code スケジュールタスクで実行：

- `uchidokoro-auto-add`（毎日0時）：解析待ち再チェック＋全機種ローテーション
- `uchidokoro-new-machine`（毎日23:30）：3週間先までの新台検出・先行記事モード公開
- `uchidokoro-verify`（毎日5:05）：全機種ローテーションチェック

月曜稼働率ランキングも会社PCのスケジュールタスクで自動並べ替え。
X投稿（@uchidokoro）：新台追加・解析データ判明時に自動投稿（Playwright方式）。

## 運用ドキュメント

作業ルール・引き継ぎ情報は `CLAUDE.md`（GitHubから除外済み）に記載。

## 現在の状態（2026-05-06時点）

- 全104機種の記事データJSON完成済み（うち4機種は5/11導入の先行記事・解析待ち、LBスロットGALFYは5/25導入予定で解析データ追記中）
- 交換率4パターン対応済み（Aタイプ除く）
- カスタムドメイン uchidokoro.com 設定済み（HTTPS有効）
- AdSense申請済み（審査待ち）
- A8.netアフィリエイト設置済み（A-SLOT・パチスロバンク・わっしょい・もしも経由楽天）
- ファビコン設定済み（「う。」ゴールド・BIZ UDゴシックBold）
- Google Search Console登録済み・sitemap送信済み

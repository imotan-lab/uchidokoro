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
- `contact.html` お問い合わせページ
- `privacy.html` プライバシーポリシーページ
- `404.html` 404ページ
- `robots.txt` クローラー向け設定
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
初回着手時は これに加えて 全ファイル一覧を確認し 実ファイル構成とルール記載の構造がズレていないかを見ること。
スクショ確認専用の URLパラメータや状態指定が必要な場合は 常設せず その確認便だけ一時的に入れて 確認後に外すこと。

現在は 全機種分の記事ページ枠はそろっており 今後は既存記事の厚み追加とブラッシュアップをまとめて進める段階。

補助ページとして `contact.html` `privacy.html` `404.html` `robots.txt` も運用中です。構成確認時はこれらも実ファイル基準で扱います。

補助ページは旧サイト名の残りがないかも確認対象です。今回までで お問い合わせページ プライバシーポリシーページ 404ページ の名称不整合は解消済みで、3ページともロゴ表示やフッター導線を含めて共通トーンへ寄せています。

公開確認をしやすくするため service worker のキャッシュ名は区切りで更新し、補助ページも静的キャッシュ対象として扱います。

主要ページと補助ページの共通導線は できるだけ共通CSSと共通順序で管理する方針です。個別ページの直書き見た目指定は 減らせるものから回収します。

## 次スレ開始用メモ

- 交換率4パターン対応は `sf5` を除く17機種へ実装完了
- 補助ページ3枚（お問い合わせページ・プライバシーポリシーページ・404ページ）の整備・共通トーンへの統一完了
- モンキーV 周期カウンター対応完了（hasCycle + cycle データ追加・機種詳細JSON更新済み）
- checker.html 削除済み・service-worker.js を v3 へ更新済み
- machine.html に期待値早見表（色分けテーブル）追加済み
- 交換率トグルを早見表の上へ移動し チェッカーと共用する配置へ変更済み
- 次の残り作業
  - 記事内容ブラッシュアップ（全18機種 machine-details JSON の加筆・精度向上）
  - ポチポチくんの機種別データ拡張（setting.html の RATES/FIELDS を動的化）
  - 共通導線の最終固定（ナビ順・フッター順・導線文言の横断統一）
- `sf5` は設定狙い専用のため 交換率セレクター対象外のまま
- 新スレッドでは最初に `CODEX_RULES.md` と `HANDOFF.md` を確認する

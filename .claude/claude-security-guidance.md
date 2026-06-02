# うちどころ。セキュリティ重点チェック項目

このファイルは security-guidance プラグインおよび手動レビュー時に重点確認したい項目をまとめたもの。
うちどころ。は **バックエンド・DB・認証を持たない静的サイト（GitHub Pages）** であり、
攻撃面は「クライアントサイドJS」「フォーム/入力欄」「ビルド/自動化スクリプトの秘密情報」に限定される。

---

## 🔴 最重点：フォーム・入力値の扱い

### 1. ユーザー入力を `innerHTML` に入れない（XSS予防）
- **ヘッダー検索欄**（`#headerSearch`）の入力値は必ず `textContent` で扱う。`innerHTML` に渡さない
  - 現状：検索結果は `item.textContent = m.name` で安全。**この方式を崩さない**
- **狙い目チェッカーの数値入力**（`#gameInput` 等）は `parseInt` で数値化し、範囲（0〜天井G数）でクランプする
  - 現状：`applyInputLimit()` で max 制御済み。負数・非数値・上限超過を必ず弾く
- 新しい入力欄・フォームを足すときは「入力→表示」の経路で `innerHTML` を絶対に使わない

### 2. 記事データ（JSON）の `innerHTML` 描画
- `machine.html` の記事描画は `section.body` / `md()` を `innerHTML` で展開している
- これは**著者管理のJSON**（machine-details/*.json）なので現状リスクは低い
- ただしルール：**外部・ユーザー由来の文字列を machine-details JSON に混ぜない**。自動タスクがWeb検索結果を本文に書く際も、HTMLタグやscriptを混入させない（`**強調**` のMarkdown記法のみ許可）

### 3. Googleフォーム（contact.html）
- 問い合わせは Google Form の iframe 埋め込み。入力処理はGoogle側なので自前バリデーション不要
- iframe の `src` を信頼できるGoogleドメイン以外に変更しない

---

## 🟠 秘密情報・認証の漏洩防止

### 4. リポジトリに秘密情報をコミットしない
- X投稿Cookie（`x_storage_uchidokoro.json`）・Gmail設定（`gmail_config.json`）は
  `C:/Users/imao_/.claude/secrets/` にあり**リポジトリ外**。この配置を崩さない
- 公開してよいID：GA測定ID（G-MSXLEMX2VJ）・AdSense pub-id・Search Console確認ファイル → これらは公開前提なのでOK
- **NG**：APIキー・アクセストークン・PAT・パスワードをHTML/JS/JSONに直書きしない
- `scripts/` 内のPythonがトークンをログ出力しないこと（`log.py` に秘密情報を渡さない）

### 5. Git設定
- リモートURLにPATを埋め込む運用（個人用 imotan-lab）。**このURLを含むファイルをコミットしない**
- `.git/config` は当然コミット対象外。誤って control 下に入れない

---

## 🟡 リンク・外部参照

### 6. 外部リンクの安全性
- `target="_blank"` のリンクには必ず `rel="noopener"`（できれば `noreferrer` も）を付ける
  - 現状：A8.netアフィリエイト・X・楽天リンクは `rel="nofollow noopener"` 付与済み。**これを維持**
- アフィリエイトリンク先ドメインを勝手に変えない（A8.net / 楽天直リンクのみ。**もしもアフィリエイトは利用不可・再追加禁止**）

### 7. オープンリダイレクト
- `404.html` や `machines/{slug}/checker.html` の `location.href` リダイレクト先は
  **自サイト内の固定パス**のみ。クエリパラメータをそのまま `location.href` に渡さない

---

## 🟢 自動化スクリプト（Python）

### 8. 自動タスクのコマンド実行
- `scripts/post_to_x.py` 等が外部入力（Web検索結果）を `subprocess`/`os.system` に渡さない
- ファイルパスを動的生成する際、`slug` 等を使うなら英数字+アンダースコアのみ許可（パストラバーサル予防）
  - 現状：slug は `[a-z_0-9]+` 想定。この前提を崩さない

### 9. 依存ライブラリ
- Playwright・requests 等のバージョンを極端に古いまま放置しない（既知脆弱性予防）

---

## チェックの優先度まとめ

| 優先 | 項目 | 理由 |
|---|---|---|
| 🔴 最重点 | フォーム/入力値を innerHTML に入れない | 唯一の動的XSS経路 |
| 🔴 最重点 | 秘密情報をコミットしない | 漏洩は即被害 |
| 🟠 高 | 外部リンク rel="noopener" | タブナビング予防 |
| 🟡 中 | リダイレクト先固定 | オープンリダイレクト予防 |
| 🟢 低 | Pythonの入力サニタイズ | ローカル実行・影響限定的 |

---

## 備考
- **security-guidance プラグインはこの環境（Desktop版）では使用不可**（`/plugin` コマンド自体が非対応・2026-06実機確認）。
  そのため本ファイルは「プラグインが自動参照する設定」ではなく、**手動レビュー時の参照ドキュメント**として機能する。
  セキュリティチェックは組み込み `/security-review` スキル＋手動コード監査で実施する（プラグイン不要）。
- 静的サイトのためサーバーサイド脆弱性（SQLi・SSRF・認証バイパス等）は対象外。

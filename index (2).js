<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{{MACHINE_NAME}} チェッカー</title>
  <link rel="stylesheet" href="assets/css/site.css">
  <script src="assets/js/checker-common.js"></script>
</head>
<body onload="loadChecker('{{SLUG}}')">
  <header class="site-header">
    <div class="site-header-inner">
      <a class="brand" href="index.html">狙い目手帳</a>
      <nav class="header-nav">
        <a href="index.html">トップ</a>
        <a href="{{SLUG}}_article.html">記事へ戻る</a>
      </nav>
    </div>
  </header>

  <main class="page">
    <section class="hero">
      <div class="eyebrow">CHECKER</div>
      <h1 class="page-title">{{MACHINE_NAME}} チェッカー</h1>
      <p class="lead">{{LEAD_TEXT}}</p>
    </section>

    <section class="grid grid-2">
      <div class="card checker-box">
        <div class="form-row">
          <label for="gameInput">現在ゲーム数</label>
          <input id="gameInput" type="number" placeholder="例: {{TARGET_GAME}}">
        </div>

        <button id="checkBtn" class="btn">判定する</button>

        <div class="result-box">
          <p id="result" class="result-text">ここに判定結果が表示されます。</p>
        </div>
      </div>

      <aside class="card">
        <h3>補足</h3>
        <ul class="point-list">
          <li>{{SIDE_1}}</li>
          <li>{{SIDE_2}}</li>
          <li>{{SIDE_3}}</li>
        </ul>
      </aside>
    </section>
  </main>

  <footer class="site-footer">
    <div class="site-footer-inner">© 狙い目手帳</div>
  </footer>
</body>
</html>
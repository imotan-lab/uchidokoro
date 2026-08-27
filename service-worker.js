const CACHE_NAME = 'uchidokoro-v235';

// ★先読みするのは「中身が機種に依存しない」ファイルだけ★
//   （2026-07-28・Codex 11巡目 手順7）
//   machine.html は汎用の器で、公開を止めた機種の表示にも使われうるので先読みしない。
//   機種ページ（/machines/{slug}/）は network-first で毎回取り直す。
const STATIC_CACHE = [
  '/',
  '/index.html',
  '/about.html',
  '/contact.html',
  '/privacy.html',
  '/guide-haena.html',
  '/guide-rate.html',
  '/guide-pochipochi.html',
  '/guide-yamedoki.html',
  '/guide-reset.html',
  '/404.html',
  '/meta-auto.js',
  '/assets/css/practical.css',
  '/assets/img/logo.png',
  '/assets/img/ogp.png'
];

// インストール時：静的ファイルをキャッシュ
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_CACHE))
  );
  self.skipWaiting();
});

// アクティベート時：古いキャッシュを削除
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// フェッチ時の戦略
// - データJSON (/assets/data/) は network-first（古いキャッシュリスク回避）
// - それ以外は cache-first（オフライン対応・既存挙動）
self.addEventListener('fetch', event => {
  // GETリクエスト以外はスキップ
  if (event.request.method !== 'GET') return;

  const url = new URL(event.request.url);

  // ★★数値を含むもの（機種の情報）は一切キャッシュしない★★
  //   （2026-07-28・Codex 12巡目 (a)-6）
  //   network-first でも、通信できない時に古い内容を返してしまう。
  //   公開を止めた機種・失効した数値が手元に残るくらいなら、
  //   「いま見られません」と出す方が安全。オフライン閲覧より正確さを優先する。
  const isClaimDependent =
    url.pathname.startsWith('/assets/data/')
    || url.pathname.startsWith('/machines/')
    || url.pathname === '/setting.html'
    || url.pathname === '/guide-tenjo-ranking.html'
    || url.pathname === '/guide-reset-ranking.html'
    || url.pathname === '/guide-suru-tenjo.html'
    || url.pathname === '/guide-ichiran.html';
  if (isClaimDependent) {
    event.respondWith(
      fetch(event.request).catch(() =>
        new Response(
          '<!doctype html><meta charset="utf-8">'
          + '<meta name="robots" content="noindex">'
          + '<title>いま表示できません | うちどころ。</title>'
          + '<h1>いま表示できません</h1>'
          + '<p>通信できないため、最新の情報を確認できませんでした。'
          + '古い情報をお見せしないよう、この画面を出しています。</p>'
          + '<p><a href="/">トップページへ</a></p>',
          { headers: { 'Content-Type': 'text/html; charset=utf-8' }, status: 503 }))
    );
    return;
  }

  // ★★その他のHTMLも network-first★★（Codex 11巡目 (a)-4）
  const isPage = event.request.mode === 'navigate'
    || (event.request.headers.get('accept') || '').includes('text/html');
  if (isPage) {
    event.respondWith(
      fetch(event.request).then(response => {
        if (response && response.status === 200) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
        }
        return response;
      }).catch(() => caches.match(event.request))
    );
    return;
  }

  // その他リソース（CSS/JS/画像）は cache-first（オフライン対応）
  event.respondWith(
    caches.match(event.request).then(cached => {
      const fetchPromise = fetch(event.request).then(response => {
        if (response && response.status === 200) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
        }
        return response;
      }).catch(() => cached);
      return cached || fetchPromise;
    })
  );
});

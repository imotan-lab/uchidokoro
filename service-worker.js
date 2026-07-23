const CACHE_NAME = 'uchidokoro-v141';

const STATIC_CACHE = [
  '/',
  '/index.html',
  '/machine.html',
  '/setting.html',
  '/about.html',
  '/contact.html',
  '/privacy.html',
  '/guide-haena.html',
  '/guide-rate.html',
  '/guide-pochipochi.html',
  '/guide-yamedoki.html',
  '/guide-reset.html',
  '/guide-tenjo-ranking.html',
  '/guide-reset-ranking.html',
  '/guide-suru-tenjo.html',
  '/guide-ichiran.html',
  '/404.html',
  '/meta-auto.js',
  '/assets/css/practical.css',
  '/assets/img/logo.png',
  '/assets/img/ogp.png',
  '/assets/data/machines.json'
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

  // ★ データJSONは network-first：ネット優先・失敗時のみキャッシュにフォールバック
  // machines.json / machine-details/*.json は頻繁に更新されるため、
  // SWキャッシュ→cache-firstだと古いデータが残り続ける問題があった。
  if (url.pathname.startsWith('/assets/data/')) {
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

  // その他リソースは cache-first（オフライン対応）
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

const CACHE_NAME = 'uchidokoro-v51';

const STATIC_CACHE = [
  '/',
  '/index.html',
  '/machine.html',
  '/setting.html',
  '/about.html',
  '/contact.html',
  '/privacy.html',
  '/404.html',
  '/meta-auto.js',
  '/assets/css/practical.css',
  '/assets/img/logo.png',
  '/assets/img/ogp.png',
  '/assets/data/machines.json',
  '/assets/data/machine-details/hokuto.json'
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

// フェッチ時：キャッシュ優先 → なければネット取得してキャッシュ更新
self.addEventListener('fetch', event => {
  // GETリクエスト以外はスキップ
  if (event.request.method !== 'GET') return;

  event.respondWith(
    caches.match(event.request).then(cached => {
      const fetchPromise = fetch(event.request).then(response => {
        // 正常なレスポンスのみキャッシュ更新
        if (response && response.status === 200) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
        }
        return response;
      }).catch(() => cached); // オフライン時はキャッシュを返す

      // キャッシュがあれば即返す（バックグラウンドで更新）
      return cached || fetchPromise;
    })
  );
});

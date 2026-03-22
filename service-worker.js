const CACHE_NAME = 'uchidokoro-v5';

const STATIC_CACHE = [
  '/uchidokoro/',
  '/uchidokoro/index.html',
  '/uchidokoro/machine.html',
  '/uchidokoro/setting.html',
  '/uchidokoro/about.html',
  '/uchidokoro/contact.html',
  '/uchidokoro/privacy.html',
  '/uchidokoro/404.html',
  '/uchidokoro/meta-auto.js',
  '/uchidokoro/assets/css/practical.css',
  '/uchidokoro/assets/img/logo.png',
  '/uchidokoro/assets/img/ogp.png',
  '/uchidokoro/assets/data/machines.json',
  '/uchidokoro/assets/data/machine-details/hokuto.json'
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

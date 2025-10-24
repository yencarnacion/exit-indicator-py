const CACHE_VERSION = 'ei-ticksonic-sounds-v1';
const SOUND_URLS = [
  '/sounds/above_ask.wav',
  '/sounds/below_bid.wav',
  '/sounds/between_bid_ask.wav',
  '/sounds/buy.wav',
  '/sounds/sell.wav',
  '/sounds/letter_u.wav',
  '/sounds/letter_d.wav',
];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE_VERSION).then(cache => cache.addAll(SOUND_URLS)));
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.map(k => (k === CACHE_VERSION ? Promise.resolve() : caches.delete(k))))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  if (!url.pathname.startsWith('/sounds/')) return;

  event.respondWith(
    caches.match(req).then(cached => cached || fetch(req).then(res => {
      if (res.ok) {
        const clone = res.clone();
        caches.open(CACHE_VERSION).then(cache => cache.put(req, clone));
      }
      return res;
    }))
  );
});

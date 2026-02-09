const CACHE_NAME = 'synth-webui-v1';
const ASSETS_TO_CACHE = [
  '/',
  '/static/synth_icon_192.png',
  '/static/synth_icon_512.png',
  '/static/manifest.webmanifest'
];

self.addEventListener('install', (event) => {
  console.debug('[service-worker] install');
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS_TO_CACHE))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  console.debug('[service-worker] activate');
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.map((k) => { if (k !== CACHE_NAME) return caches.delete(k); })
    ))
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  // Only handle GET requests
  if (event.request.method !== 'GET') return;
  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;
      return fetch(event.request).then((response) => {
        // optionally cache new requests
        return caches.open(CACHE_NAME).then((cache) => {
          try { cache.put(event.request, response.clone()); } catch (e) { /* ignore */ }
          return response;
        });
      }).catch(() => {
        return caches.match('/');
      });
    })
  );
});

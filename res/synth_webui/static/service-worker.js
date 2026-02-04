const CACHE_NAME = 'synth-webui-v3';
const ASSETS_TO_CACHE = [
  '/static/synth_icon_192.png',
  '/static/synth_icon_512.png',
  '/static/manifest.webmanifest',
  '/static/synth_logo_bg.png'
];

self.addEventListener('install', (event) => {
  console.debug('[service-worker] install');
  // Pre-cache core assets only
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS_TO_CACHE))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  console.debug('[service-worker] activate');
  // Purge old caches
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

  const url = new URL(event.request.url);

  // Never cache API endpoints or dynamic resources that may return varying content
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/skins/') || url.pathname.startsWith('/js/')) {
    event.respondWith(
      fetch(event.request).catch(() => caches.match('/'))
    );
    return;
  }

  // For navigations (HTML), prefer network-first so layout updates are immediate.
  if (event.request.mode === 'navigate' || (event.request.headers.get('accept') || '').includes('text/html')) {
    event.respondWith(
      fetch(event.request).catch(() => caches.match(event.request))
    );
    return;
  }

  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;
      return fetch(event.request).then((response) => {
        // Cache only successful (OK) non-HTML responses to avoid stale pages
        const contentType = response && response.headers ? (response.headers.get('content-type') || '') : '';
        if (response && response.ok && !contentType.includes('text/html')) {
          caches.open(CACHE_NAME).then((cache) => {
            try { cache.put(event.request, response.clone()); } catch (e) { /* ignore */ }
          });
        }
        return response;
      }).catch(() => caches.match(event.request));
    })
  );
});

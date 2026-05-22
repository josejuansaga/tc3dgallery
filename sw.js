// Service worker de limpieza: elimina caches antiguas y deja pasar todo a red.
const CACHE_PREFIX = 'tc3d-';

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(key => key.startsWith(CACHE_PREFIX)).map(key => caches.delete(key)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', () => {
  // No cacheamos nada aqui para evitar servir una portada antigua.
});

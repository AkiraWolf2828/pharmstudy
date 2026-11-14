const CACHE = 'pharmstudy-offline-v1';
const home = new URL('index.html', self.registration.scope).href;
const synapse = new URL('adrenergic-synapse.html', self.registration.scope).href;
const assets = [home, synapse];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(assets)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key =>
    key.startsWith('pharmstudy-offline-') && key !== CACHE
  ).map(key => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if(event.request.method !== 'GET' || event.request.mode !== 'navigate') return;
  const shell = url.origin + url.pathname;
  const asset = shell === self.registration.scope ? home : shell;
  if(!assets.includes(asset)) return;
  event.respondWith((async () => {
    const cache = await caches.open(CACHE);
    try {
      const response = await fetch(event.request, {signal: AbortSignal.timeout(3500)});
      if(response.ok){
        await cache.put(asset, response.clone());
        return response;
      }
    } catch {}
    return await cache.match(asset) || Response.error();
  })());
});

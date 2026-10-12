/* muji service worker — v41 (network-first shell; cache-first static; self-updating) */
const CACHE = "muji-v120";
const SHELL = [
  "/",
  "/static/index.html",
  "/static/style.css?v=172",
  "/static/app.js?v=191",
  "/static/vendor/marked.min.js",
  "/static/vendor/purify.min.js",
  "/static/vendor/highlight.min.js",
  "/static/vendor/hljs-github.min.css",
  "/static/vendor/hljs-github-dark.min.css",
  "/static/vendor/katex.min.css",
  "/static/vendor/katex.min.js",
  "/static/favicon.png?v=23",
  "/static/manifest.json",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/static/apple-touch-icon.png?v=3",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);

  // Never intercept API calls — let them fail naturally
  if (url.pathname.startsWith("/api/")) return;

  // Navigation: network-first, fall back to cached shell.
  // Version check: if the served HTML references a different app.js version
  // than this SW's SHELL, a newer deploy is out — tell the SW to activate
  // and clients to reload. Kills the stale-shell trap where a long-lived
  // tab keeps running an old app.js from the pinned cache.
  if (req.mode === "navigate") {
    e.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put("/", copy));
          copy.text().then((html) => {
            // Check BOTH shell versions: a CSS-only deploy leaves app.js's
            // version untouched, so the old single-check never fired and a
            // long-lived PWA kept running a stale shell (Boss's 2-tap bug).
            let stale = false;
            for (const re of [/app\.js\?v=(\d+)/, /style\.css\?v=(\d+)/]) {
              const m = html.match(re);
              const mine = (SHELL.find((u) => u.match(re)) || "").match(re);
              if (m && mine && m[1] !== mine[1]) { stale = true; break; }
            }
            if (stale) {
              self.skipWaiting();
              self.clients.matchAll().then((cs) =>
                cs.forEach((c) => c.postMessage("muji-update")));
            }
          }).catch(() => {});
          return res;
        })
        .catch(() => caches.match("/"))
    );
    return;
  }

  // Same-origin static assets: network-first, cache fallback (offline)
  if (url.origin === self.location.origin) {
    e.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
          return res;
        })
        .catch(() => caches.match(req))
    );
  }
});

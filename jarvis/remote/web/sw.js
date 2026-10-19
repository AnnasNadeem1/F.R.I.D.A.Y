// Service worker: shows push notifications (reminders, Claude alerts) and makes the
// web app installable. No offline caching: the assistant lives on your PC anyway.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});  // network as normal

self.addEventListener("push", (event) => {
  let data = { title: "Friday", body: "" };
  try { data = Object.assign(data, event.data.json()); } catch (e) {}
  event.waitUntil(self.registration.showNotification(data.title, {
    body: data.body, tag: data.tag || "friday", icon: "/icon-192.png", badge: "/icon-192.png",
    renotify: true, vibrate: [80, 40, 80],
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const c of all) { if ("focus" in c) return c.focus(); }
    return self.clients.openWindow("/");
  })());
});

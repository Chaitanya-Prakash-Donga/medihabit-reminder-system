/*
MediHabit - Service Worker
Handles Caching for PWA and Background Push Notifications
*/

const CACHE_NAME = 'medihabit-v1';
const ASSETS_TO_CACHE = [
  '/',
  '/dashboard',
  '/static/manifest.json',
  // Add other CSS/JS files here if you want offline support
];

// 1. Install Event: Caches basic assets
self.addEventListener('install', (event) => {
  console.log('[Service Worker] Install');
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(ASSETS_TO_CACHE);
    })
  );
});

// 2. Push Event: This wakes up the device even if the tab is in the background
self.addEventListener('push', function(event) {
    console.log('[Service Worker] Push Received');
    
    let data = { title: 'MediHabit Reminder', body: 'Time to take your medication!' };
    
    // If your server sends JSON data, we parse it here
    if (event.data) {
        try {
            data = event.data.json();
        } catch (e) {
            data.body = event.data.text();
        }
    }

    const options = {
        body: data.body,
        icon: '/static/icons/icon-192x192.png', // Ensure this file exists
        badge: '/static/icons/icon-192x192.png',
        vibrate: [200, 100, 200, 100, 200], // Strong vibration pattern
        tag: 'medication-reminder',
        renotify: true,
        actions: [
            { action: 'open', title: 'Open App' }
        ]
    };

    event.waitUntil(
        self.registration.showNotification(data.title, options)
    );
});

// 3. Notification Click Event: Opens the app when the user taps the alert
self.addEventListener('notificationclick', function(event) {
    event.notification.close();
    event.waitUntil(
        clients.openWindow('/') // Redirects user to the dashboard
    );
});

// 4. Fetch Event: Serves assets from cache if offline
self.addEventListener('fetch', (event) => {
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});

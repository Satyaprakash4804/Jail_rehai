/**
 * firebase-messaging-sw.js
 * ========================
 * Place this file at:  YOUR_FLASK_PROJECT/static/firebase-messaging-sw.js
 *
 * Flask serves it at the ROOT path via the /firebase-messaging-sw.js route
 * already added in run.py. The service worker MUST be at root scope
 * or browser push will silently fail.
 *
 * Fill in YOUR actual Firebase config values below.
 * Get them from: Firebase Console → Project Settings → General → Web App
 */

// ── Import Firebase compat scripts (required inside service workers) ──────────
importScripts('https://www.gstatic.com/firebasejs/10.12.2/firebase-app-compat.js');
importScripts('https://www.gstatic.com/firebasejs/10.12.2/firebase-messaging-compat.js');

// ════════════════════════════════════════════════════════════════════════════
// TODO: PASTE YOUR FIREBASE CONFIG HERE
// Firebase Console → Project Settings → General → Your Web App → Config
// ════════════════════════════════════════════════════════════════════════════
const firebaseConfig = {
  apiKey: "AIzaSyA72fZMa0oMjvOGzo6Mg4sH11qTndiiD-Y",
    authDomain: "jail-rihai.firebaseapp.com",
    projectId: "jail-rihai",
    storageBucket: "jail-rihai.firebasestorage.app",
    messagingSenderId: "609483163338",
    appId: "1:609483163338:web:a9add35587ff3d4466d633",
  };
// ════════════════════════════════════════════════════════════════════════════

firebase.initializeApp(firebaseConfig);
const messaging = firebase.messaging();

// ── Background / Terminated state: show OS notification ──────────────────────
messaging.onBackgroundMessage(function(payload) {
  console.log('[SW] Background message received:', payload);

  const notification = payload.notification || {};
  const data         = payload.data         || {};

  const title   = notification.title || 'Jail Rehai';
  const options = {
    body:               notification.body || 'You have a new notification.',
    icon:               '/static/icons/icon-192.png',
    badge:              '/static/icons/badge-72.png',
    tag:                'jail-rehai-notif',   // replaces earlier un-dismissed notif
    renotify:            true,
    requireInteraction:  true,
    data: {
      // Pass click URL through — read in notificationclick handler
      clickUrl: data.click_url || '/'
    },
    actions: [
      { action: 'open',    title: '📋 View Details' },
      { action: 'dismiss', title: 'Dismiss' }
    ]
  };

  self.registration.showNotification(title, options);
});

// ── Notification click: open the correct page ─────────────────────────────────
self.addEventListener('notificationclick', function(event) {
  event.notification.close();

  if (event.action === 'dismiss') return;

  const clickUrl = (event.notification.data && event.notification.data.clickUrl)
    ? event.notification.data.clickUrl
    : '/';

  const fullUrl = self.location.origin + clickUrl;

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true })
      .then(function(windowClients) {
        // If app is already open in a tab, focus it and navigate
        for (var i = 0; i < windowClients.length; i++) {
          var client = windowClients[i];
          if (client.url.indexOf(self.location.origin) === 0 && 'focus' in client) {
            client.focus();
            client.navigate(fullUrl);
            return;
          }
        }
        // Otherwise open a new tab
        if (clients.openWindow) {
          return clients.openWindow(fullUrl);
        }
      })
  );
});

// ── Lifecycle ─────────────────────────────────────────────────────────────────
self.addEventListener('install',  function() { self.skipWaiting(); });
self.addEventListener('activate', function(e) { e.waitUntil(clients.claim()); });

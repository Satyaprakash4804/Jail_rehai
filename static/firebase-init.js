/**
 * static/firebase-init.js  — FIXED
 * ══════════════════════════════════
 * ROOT CAUSE OF PREVIOUS FAILURE:
 *   The file had TWO separate IIFEs. FIREBASE_CONFIG and VAPID_KEY were
 *   declared inside the first IIFE which ended on line 43 with `})`.
 *   The second IIFE (the actual logic) started on line 44 and could NOT
 *   access those variables — they were in a different scope. So
 *   FIREBASE_CONFIG was always `undefined`, firebase.initializeApp() threw
 *   silently, and no token was ever obtained or saved.
 *
 * FIX: Everything is inside ONE single IIFE.
 */
(function () {
  'use strict';

  /* ══════════════════════════════════════════════════════════════
     YOUR FIREBASE CONFIG  (already filled in — do not change)
     ══════════════════════════════════════════════════════════════ */
  var FIREBASE_CONFIG = {
    apiKey:            "AIzaSyA72fZMa0oMjvOGzo6Mg4sH11qTndiiD-Y",
    authDomain:        "jail-rihai.firebaseapp.com",
    projectId:         "jail-rihai",
    storageBucket:     "jail-rihai.firebasestorage.app",
    messagingSenderId: "609483163338",
    appId:             "1:609483163338:web:a9add35587ff3d4466d633"
  };

  /* ══════════════════════════════════════════════════════════════
     YOUR VAPID KEY  (already filled in — do not change)
     ══════════════════════════════════════════════════════════════ */
  var VAPID_KEY = "BJ__z8BQrC1dpCkG2_NTezYH9_qtL1Gk442YFzpU171FMYvIMfzKi61voFaY01tkl0gRAS-7MrnIPLb4BwXpOuw";

  /* ─────────────────────────────────────────────────────────────
     NOTHING BELOW NEEDS EDITING
     ───────────────────────────────────────────────────────────── */

  var FCM_STORAGE_KEY = 'jailrehai_fcm_token';

  /* ── Small console logger (no on-screen panel in production) ── */
  function log(msg, level) {
    var prefix = { ok: '✓', warn: '⚠', error: '✗', info: '→' }[level] || '→';
    console.log('[FCM] ' + prefix + ' ' + msg);
  }

  /* ── Only run on pages with the notification bell ─────────── */
  if (!document.getElementById('notifBtn')) return;

  /* ── Browser support ───────────────────────────────────────── */
  if (!('serviceWorker' in navigator) || !('Notification' in window)) {
    log('Browser does not support push notifications.', 'warn');
    return;
  }

  /* ── Wait for Firebase compat SDK loaded by base.html ──────── */
  function waitForFirebase(cb, n) {
    n = n || 0;
    if (typeof firebase !== 'undefined' && typeof firebase.messaging === 'function') {
      log('Firebase SDK ready ✓', 'ok');
      cb();
    } else if (n < 60) {
      setTimeout(function () { waitForFirebase(cb, n + 1); }, 100);
    } else {
      log('Firebase SDK not loaded after 6s — check base.html script tags.', 'error');
    }
  }

  /* ── Main initialisation ───────────────────────────────────── */
  function init() {
    log('Initialising Firebase…', 'info');

    /* 1. Init Firebase app */
    try {
      if (!firebase.apps.length) {
        firebase.initializeApp(FIREBASE_CONFIG);
        log('firebase.initializeApp() OK ✓', 'ok');
      } else {
        log('Firebase already initialised ✓', 'ok');
      }
    } catch (e) {
      log('initializeApp ERROR: ' + e.message, 'error');
      return;
    }

    /* 2. Get messaging instance */
    var msg;
    try {
      msg = firebase.messaging();
      log('firebase.messaging() OK ✓', 'ok');
    } catch (e) {
      log('firebase.messaging() ERROR: ' + e.message, 'error');
      return;
    }

    /* 3. Register service worker */
    log('Registering service worker…', 'info');
    navigator.serviceWorker
      .register('/firebase-messaging-sw.js', { scope: '/' })
      .then(function (reg) {
        log('Service worker registered ✓ scope=' + reg.scope, 'ok');

        /* 4. Request OS notification permission */
        return Notification.requestPermission().then(function (perm) {
          log('Notification permission: ' + perm, perm === 'granted' ? 'ok' : 'warn');
          if (perm !== 'granted') return;

          /* 5. Get FCM token */
          return msg.getToken({ vapidKey: VAPID_KEY, serviceWorkerRegistration: reg })
            .then(function (token) {
              if (!token) {
                log('getToken() returned empty — check VAPID key.', 'error');
                return;
              }
              log('Token obtained (' + token.length + ' chars) ✓', 'ok');
              saveToken(token);
            })
            .catch(function (err) {
              log('getToken() failed: ' + err.message, 'error');
            });
        });
      })
      .catch(function (err) {
        log('Service worker registration failed: ' + err.message, 'error');
      });

    /* 6. Handle foreground messages (tab is active) */
    msg.onMessage(function (payload) {
      log('Foreground push received: ' + (payload.notification || {}).title, 'ok');
      var n    = payload.notification || {};
      var data = payload.data         || {};
      showToast(n.title || 'Jail Rehai', n.body || '', data.click_url || '/');
      bumpBadge();
      if (typeof loadNotifPanel === 'function') loadNotifPanel();
    });
  }

  /* ── Save token to Flask /api/fcm/save-token ───────────────── */
  function saveToken(token) {
    if (localStorage.getItem(FCM_STORAGE_KEY) === token) {
      log('Token unchanged — skipping save.', 'info');
      return;
    }
    log('Saving token to server…', 'info');
    fetch('/api/fcm/save-token', {
      method:      'POST',
      credentials: 'same-origin',
      headers:     { 'Content-Type': 'application/json' },
      body:        JSON.stringify({ token: token, device_type: 'web' })
    })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (d.success) {
        localStorage.setItem(FCM_STORAGE_KEY, token);
        log('Token saved to server ✓ — push active!', 'ok');
      } else {
        log('Server rejected token: ' + (d.error || 'unknown'), 'error');
      }
    })
    .catch(function (e) { log('Save-token network error: ' + e.message, 'error'); });
  }

  /* ── Delete token on logout (called from base.html logout link) */
  window.deleteFcmToken = function () {
    var token = localStorage.getItem(FCM_STORAGE_KEY);
    var ps    = [];
    if (typeof firebase !== 'undefined' && firebase.apps.length) {
      try { ps.push(firebase.messaging().deleteToken().catch(function () {})); } catch (e) {}
    }
    if (token) {
      ps.push(
        fetch('/api/fcm/delete-token', {
          method:      'POST',
          credentials: 'same-origin',
          headers:     { 'Content-Type': 'application/json' },
          body:        JSON.stringify({ token: token })
        }).catch(function () {})
      );
      localStorage.removeItem(FCM_STORAGE_KEY);
    }
    return Promise.all(ps);
  };

  /* ── In-page toast for foreground pushes ────────────────────── */
  function showToast(title, body, clickUrl) {
    var c = document.getElementById('fcm-toast-container');
    if (!c) {
      c = document.createElement('div');
      c.id = 'fcm-toast-container';
      Object.assign(c.style, {
        position: 'fixed', top: '72px', right: '16px', zIndex: '99997',
        display: 'flex', flexDirection: 'column', gap: '10px',
        maxWidth: '360px', pointerEvents: 'none'
      });
      document.body.appendChild(c);
    }
    var t = document.createElement('div');
    Object.assign(t.style, {
      background:    'var(--surface,#fff)',
      border:        '1px solid var(--border,#dee2e6)',
      borderLeft:    '4px solid #1a73e8',
      borderRadius:  '8px',
      boxShadow:     '0 4px 24px rgba(0,0,0,.15)',
      padding:       '13px 16px',
      display:       'flex',
      gap:           '12px',
      alignItems:    'flex-start',
      pointerEvents: 'all',
      cursor:        'pointer',
      transition:    'opacity .35s, transform .35s',
      transform:     'translateX(110%)',
      opacity:       '0'
    });
    t.innerHTML =
      '<div style="width:34px;height:34px;border-radius:50%;background:rgba(26,115,232,.12);' +
      'display:flex;align-items:center;justify-content:center;flex-shrink:0">' +
      '<i class="fas fa-bell" style="color:#1a73e8;font-size:.88rem"></i></div>' +
      '<div style="flex:1;min-width:0">' +
      '<div style="font-weight:700;font-size:.84rem;margin-bottom:3px;white-space:nowrap;' +
      'overflow:hidden;text-overflow:ellipsis">' + esc(title) + '</div>' +
      '<div style="font-size:.77rem;color:var(--text-muted,#6c757d);line-height:1.45;' +
      'display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">' +
      esc(body) + '</div></div>' +
      '<button onclick="this.closest(\'[data-toast]\').remove()" ' +
      'style="background:none;border:none;cursor:pointer;color:#888;font-size:1.2rem;padding:0;flex-shrink:0">×</button>';
    t.setAttribute('data-toast', '1');
    t.addEventListener('click', function (e) {
      if (e.target.tagName === 'BUTTON') return;
      window.location.href = clickUrl;
    });
    c.appendChild(t);
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        t.style.transform = 'translateX(0)';
        t.style.opacity   = '1';
      });
    });
    setTimeout(function () {
      t.style.opacity = '0';
      t.style.transform = 'translateX(110%)';
      setTimeout(function () { t.remove(); }, 380);
    }, 8000);
  }

  /* ── Badge increment (real-time, no reload needed) ──────────── */
  function bumpBadge() {
    ['topbar-notif-badge', 'sidebar-notif-badge'].forEach(function (id) {
      var el = document.getElementById(id);
      if (!el) return;
      el.textContent = (parseInt(el.textContent, 10) || 0) + 1;
      el.classList.add('show');
      el.style.display = 'inline';
    });
  }

  function esc(s) {
    return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  /* ── Bootstrap: wait for Firebase then init after 1.5s ──────── */
  log('firebase-init.js loaded ✓', 'ok');
  waitForFirebase(function () { setTimeout(init, 1500); });

})();  /* ← ONE closing bracket. Only one IIFE. */

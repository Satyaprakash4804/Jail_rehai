"""
firebase_config.py
==================
Firebase Admin SDK initialization + FCM send helpers.

FIX vs previous version:
  • WebpushFCMOptions link now reads BASE_URL from config.py instead of
    a hardcoded ngrok URL (which breaks every time ngrok restarts).
  • init_firebase() is fully idempotent — checks firebase_admin.get_app()
    in addition to the local flag to prevent "already exists" crashes on
    Flask debug reloader double-start.
  • send_fcm_multicast now uses send_each_for_multicast (FCM v1 API).
"""

import os
import logging
import firebase_admin
from firebase_admin import credentials, messaging

logger = logging.getLogger(__name__)

_firebase_initialized = False


def _get_base_url():
    """Read BASE_URL from config.py, falling back to localhost."""
    try:
        from config import BASE_URL
        # Strip trailing slash
        return BASE_URL.rstrip('/')
    except Exception:
        return 'http://localhost:5000'


def init_firebase():
    """
    Initialize Firebase Admin SDK exactly once.
    Safe to call even if Flask debug reloader runs this twice.
    """
    global _firebase_initialized

    # Already initialized in this process
    if _firebase_initialized:
        return

    # Also check firebase_admin itself (protects against reloader double-init)
    try:
        firebase_admin.get_app()
        _firebase_initialized = True
        logger.info("[FCM] Firebase already initialized (reloader guard).")
        return
    except ValueError:
        pass  # No app yet — proceed

    key_path = os.environ.get(
        "FIREBASE_CREDENTIALS",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "serviceAccountKey.json")
    )

    if not os.path.exists(key_path):
        logger.error(
            f"[FCM] serviceAccountKey.json not found at {key_path}. "
            "FCM push disabled."
        )
        return

    try:
        cred = credentials.Certificate(key_path)
        firebase_admin.initialize_app(cred)
        _firebase_initialized = True
        logger.info("[FCM] Firebase Admin SDK initialized OK.")
    except Exception as e:
        logger.error(f"[FCM] Firebase init failed: {e}")


def is_firebase_ready():
    return _firebase_initialized


# ── Single-token send ────────────────────────────────────────────────────────

def send_fcm_message(token, title, body, data=None, image_url=None):
    """Send a push notification to one FCM token. Returns True/False."""
    if not is_firebase_ready():
        logger.warning("[FCM] Not initialized — skipping single push.")
        return False

    base_url = _get_base_url()

    try:
        msg = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body,
                image=image_url or None,
            ),
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    title=title,
                    body=body,
                    sound="default",
                    click_action="FLUTTER_NOTIFICATION_CLICK",
                    channel_id="bail_notifications",
                    image=image_url or None,
                ),
            ),
            webpush=messaging.WebpushConfig(
                notification=messaging.WebpushNotification(
                    title=title,
                    body=body,
                    icon="/static/icons/icon-192.png",
                    badge="/static/icons/badge-72.png",
                ),
                fcm_options=messaging.WebpushFCMOptions(
                    link=base_url + "/"
                ),
            ),
            data={k: str(v) for k, v in (data or {}).items()},
            token=token,
        )
        response = messaging.send(msg)
        logger.info(f"[FCM] Single push OK. message_id={response}")
        return True

    except messaging.UnregisteredError:
        logger.warning(f"[FCM] Token unregistered: {token[:25]}…")
        return False
    except messaging.SenderIdMismatchError:
        logger.error(f"[FCM] Sender ID mismatch: {token[:25]}…")
        return False
    except Exception as e:
        logger.error(f"[FCM] send_fcm_message error: {e}")
        return False


# ── Multicast send (up to 500 tokens per call) ───────────────────────────────

def send_fcm_multicast(tokens, title, body, data=None, image_url=None):
    """
    Send push notification to a list of FCM tokens.
    Automatically chunks into batches of 500.

    Returns:
        dict: { success_count, failure_count, failed_tokens }
    """
    if not is_firebase_ready():
        logger.warning("[FCM] Not initialized — skipping multicast.")
        return {"success_count": 0, "failure_count": 0, "failed_tokens": []}

    if not tokens:
        return {"success_count": 0, "failure_count": 0, "failed_tokens": []}

    base_url   = _get_base_url()
    CHUNK      = 500
    totals     = {"success_count": 0, "failure_count": 0, "failed_tokens": []}

    for i in range(0, len(tokens), CHUNK):
        chunk = tokens[i: i + CHUNK]
        try:
            msg = messaging.MulticastMessage(
                notification=messaging.Notification(title=title, body=body),
                android=messaging.AndroidConfig(
                    priority="high",
                    notification=messaging.AndroidNotification(
                        title=title,
                        body=body,
                        sound="default",
                        click_action="FLUTTER_NOTIFICATION_CLICK",
                        channel_id="bail_notifications",
                    ),
                ),
                webpush=messaging.WebpushConfig(
                    notification=messaging.WebpushNotification(
                        title=title,
                        body=body,
                        icon="/static/icons/icon-192.png",
                        badge="/static/icons/badge-72.png",
                    ),
                    fcm_options=messaging.WebpushFCMOptions(
                        link=base_url + "/"
                    ),
                ),
                data={k: str(v) for k, v in (data or {}).items()},
                tokens=chunk,
            )

            resp = messaging.send_each_for_multicast(msg)
            totals["success_count"] += resp.success_count
            totals["failure_count"] += resp.failure_count

            for idx, r in enumerate(resp.responses):
                if not r.success:
                    err = r.exception
                    logger.warning(
                        f"[FCM] Token {chunk[idx][:25]}… failed: {err}"
                    )
                    if isinstance(err, (
                        messaging.UnregisteredError,
                        messaging.SenderIdMismatchError
                    )):
                        totals["failed_tokens"].append(chunk[idx])

        except Exception as e:
            logger.error(f"[FCM] Multicast chunk error: {e}")
            totals["failure_count"] += len(chunk)

    logger.info(
        f"[FCM] Multicast done — "
        f"✓{totals['success_count']} ✗{totals['failure_count']} "
        f"stale={len(totals['failed_tokens'])}"
    )
    return totals


# ── Topic send ────────────────────────────────────────────────────────────────

def send_fcm_to_topic(topic, title, body, data=None):
    """Broadcast to all devices subscribed to an FCM topic."""
    if not is_firebase_ready():
        return False

    base_url = _get_base_url()

    try:
        msg = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    sound="default",
                    channel_id="bail_notifications",
                ),
            ),
            webpush=messaging.WebpushConfig(
                notification=messaging.WebpushNotification(
                    title=title, body=body,
                    icon="/static/icons/icon-192.png",
                ),
                fcm_options=messaging.WebpushFCMOptions(link=base_url + "/"),
            ),
            data={k: str(v) for k, v in (data or {}).items()},
            topic=topic,
        )
        response = messaging.send(msg)
        logger.info(f"[FCM] Topic '{topic}' push OK. message_id={response}")
        return True
    except Exception as e:
        logger.error(f"[FCM] send_fcm_to_topic error: {e}")
        return False

"""
fcm_service.py
==============
High-level FCM token management + push helpers.

FIX vs previous version:
  • Removed ALL debug print() statements (were leaking to production logs).
  • push_to_district() now uses proper logger calls instead of prints.
  • save_fcm_token() debug print block removed; uses logger.info instead.
  • All functions now log at appropriate levels (info / warning / error).
"""

import logging
from db import get_connection
from firebase_config import send_fcm_multicast, is_firebase_ready

logger = logging.getLogger(__name__)


# ── Token persistence ─────────────────────────────────────────────────────────

def save_fcm_token(user_id, token, device_type="web"):
    """
    Upsert an FCM token for a user.
    - Re-assigns token if it previously belonged to another user (device swap).
    - Uses ON DUPLICATE KEY UPDATE on (user_id, token) unique index.
    """
    if not token or not user_id:
        return False
    try:
        conn   = get_connection()
        cursor = conn.cursor()

        # Remove token from any other user first (device transfer case)
        cursor.execute(
            "DELETE FROM fcm_tokens WHERE token=%s AND user_id != %s",
            (token, user_id)
        )

        cursor.execute("""
            INSERT INTO fcm_tokens (user_id, token, device_type)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE
                device_type = VALUES(device_type),
                updated_at  = CURRENT_TIMESTAMP
        """, (user_id, token, device_type))

        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"[FCM] Token saved — user_id={user_id} device={device_type}")
        return True
    except Exception as e:
        logger.error(f"[FCM] save_fcm_token error: {e}")
        return False


def delete_fcm_token(token):
    """Remove one specific FCM token."""
    if not token:
        return False
    try:
        conn   = get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM fcm_tokens WHERE token=%s", (token,))
        conn.commit()
        deleted = cursor.rowcount
        cursor.close()
        conn.close()
        logger.info(f"[FCM] Deleted {deleted} token(s).")
        return True
    except Exception as e:
        logger.error(f"[FCM] delete_fcm_token error: {e}")
        return False


def delete_user_tokens(user_id):
    """Remove ALL FCM tokens for a user (called on logout)."""
    if not user_id:
        return False
    try:
        conn   = get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM fcm_tokens WHERE user_id=%s", (user_id,))
        conn.commit()
        deleted = cursor.rowcount
        cursor.close()
        conn.close()
        logger.info(f"[FCM] Deleted {deleted} token(s) for user_id={user_id}.")
        return True
    except Exception as e:
        logger.error(f"[FCM] delete_user_tokens error: {e}")
        return False


def purge_stale_tokens(failed_tokens):
    """
    Remove tokens that FCM reported as invalid.
    Called automatically after every multicast send.
    """
    if not failed_tokens:
        return
    try:
        conn   = get_connection()
        cursor = conn.cursor()
        for t in failed_tokens:
            cursor.execute("DELETE FROM fcm_tokens WHERE token=%s", (t,))
        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"[FCM] Purged {len(failed_tokens)} stale token(s).")
    except Exception as e:
        logger.error(f"[FCM] purge_stale_tokens error: {e}")


# ── Token queries ─────────────────────────────────────────────────────────────

def get_tokens_for_users(user_ids):
    """Return all FCM tokens for a list of user DB IDs."""
    if not user_ids:
        return []
    try:
        conn   = get_connection()
        cursor = conn.cursor(dictionary=True)
        ph     = ",".join(["%s"] * len(user_ids))
        cursor.execute(
            f"SELECT token FROM fcm_tokens WHERE user_id IN ({ph})",
            list(user_ids)
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return [r["token"] for r in rows]
    except Exception as e:
        logger.error(f"[FCM] get_tokens_for_users error: {e}")
        return []


def get_all_tokens(roles=None):
    """Return FCM tokens for all active users, optionally filtered by role."""
    try:
        conn   = get_connection()
        cursor = conn.cursor(dictionary=True)
        if roles:
            ph = ",".join(["%s"] * len(roles))
            cursor.execute(f"""
                SELECT ft.token FROM fcm_tokens ft
                JOIN users u ON u.id = ft.user_id
                WHERE u.role IN ({ph}) AND u.is_active=1
            """, list(roles))
        else:
            cursor.execute("""
                SELECT ft.token FROM fcm_tokens ft
                JOIN users u ON u.id = ft.user_id
                WHERE u.is_active=1
            """)
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return [r["token"] for r in rows]
    except Exception as e:
        logger.error(f"[FCM] get_all_tokens error: {e}")
        return []


# ── High-level push API ───────────────────────────────────────────────────────

def push_to_users(user_ids, title, body, data=None):
    """Push to specific user IDs."""
    if not is_firebase_ready():
        return {"success_count": 0, "failure_count": 0, "failed_tokens": []}

    tokens = get_tokens_for_users(user_ids)
    if not tokens:
        logger.info(f"[FCM] push_to_users: no tokens for {user_ids}")
        return {"success_count": 0, "failure_count": 0, "failed_tokens": []}

    result = send_fcm_multicast(tokens, title, body, data)
    purge_stale_tokens(result.get("failed_tokens", []))
    return result


def push_to_district(district, title, body, data=None, exclude_user_id=None):
    """
    Push to all active admin/super_admin users in a district.
    Optionally exclude one user (the sender).
    """
    if not is_firebase_ready():
        return {"success_count": 0, "failure_count": 0, "failed_tokens": []}

    try:
        conn   = get_connection()
        cursor = conn.cursor(dictionary=True)
        # Fetch tokens for:
        # 1. All admins/super_admins in the exact district
        # 2. All master/super_admins with district='All' (they oversee all districts)
        cursor.execute("""
            SELECT ft.token
            FROM fcm_tokens ft
            JOIN users u ON u.id = ft.user_id
            WHERE (
                (u.district = %s AND u.role IN ('admin', 'super_admin'))
                OR
                (u.district = 'All' AND u.role IN ('master', 'super_admin'))
            )
            AND u.is_active = 1
            AND (%s IS NULL OR u.id != %s)
        """, (district, exclude_user_id, exclude_user_id))
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        tokens = [r["token"] for r in rows]
    except Exception as e:
        logger.error(f"[FCM] push_to_district query error: {e}")
        return {"success_count": 0, "failure_count": 0, "failed_tokens": []}

    if not tokens:
        logger.info(f"[FCM] push_to_district: no tokens in district={district!r}")
        return {"success_count": 0, "failure_count": 0, "failed_tokens": []}

    result = send_fcm_multicast(tokens, title, body, data)
    purge_stale_tokens(result.get("failed_tokens", []))

    logger.info(
        f"[FCM] push_to_district district={district!r} "
        f"✓{result['success_count']} ✗{result['failure_count']}"
    )
    return result


def push_to_admins(title, body, data=None):
    """Push to all active admins across all districts."""
    if not is_firebase_ready():
        return {"success_count": 0, "failure_count": 0, "failed_tokens": []}
    tokens = get_all_tokens(roles=["admin", "super_admin"])
    if not tokens:
        return {"success_count": 0, "failure_count": 0, "failed_tokens": []}
    result = send_fcm_multicast(tokens, title, body, data)
    purge_stale_tokens(result.get("failed_tokens", []))
    return result


def push_to_all(title, body, data=None):
    """Broadcast to every active user with an FCM token."""
    if not is_firebase_ready():
        return {"success_count": 0, "failure_count": 0, "failed_tokens": []}
    tokens = get_all_tokens()
    if not tokens:
        return {"success_count": 0, "failure_count": 0, "failed_tokens": []}
    result = send_fcm_multicast(tokens, title, body, data)
    purge_stale_tokens(result.get("failed_tokens", []))
    return result
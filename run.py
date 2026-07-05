"""
run.py  (UPDATED)
=================
Changes from original:
  • Firebase Admin SDK initialized at startup via init_firebase()
  • /api/fcm/save-token   — save/update token on login
  • /api/fcm/delete-token — remove token on logout
  • /api/notifications-preview — powers the notification bell dropdown
  • /save_fcm_token stub replaced with proper implementation
  • /api/mobile/* — JSON API blueprint (mobile_api.py) for the Flutter app,
    using the SAME session-cookie auth, DB, and business logic as the web
    app. Nothing above this line changed; this is purely additive.

All existing routes, blueprints, and middleware are preserved.
"""

from datetime import timedelta
from flask import Flask, render_template, redirect, url_for, session, request, jsonify
from flask_mail import Mail
from config import SECRET_KEY, MAIL_CONFIG, DB_CONFIG, IS_PRODUCTION
from db import init_db, get_connection
from utils import init_cloudinary, log_activity, get_accused_bail_alerts
import logging
import os

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024  # 32 MB

# ── Session cookie config ───────────────────────────────────────────────────
# Both the web app AND the Flutter app talk to this SAME Flask origin
# (the mobile app calls /api/mobile/... on the same domain the web UI is
# served from), so this is a same-origin cookie, not a cross-site one.
# That means SameSite=Lax is correct for BOTH clients — no need for
# SameSite=None, which only matters for true cross-site requests and
# forces Secure=True (HTTPS-only), which is what broke local web login
# (login -> dashboard -> login redirect loop over plain HTTP).
#
#   - SESSION_COOKIE_SECURE is tied to APP_ENV (see config.py):
#       APP_ENV=production (stpepl.com, HTTPS) -> Secure=True
#       APP_ENV unset / development (local HTTP, LAN IP)  -> Secure=False
#
# This single config now works for: web browser (local + prod) and the
# Flutter app (local LAN + prod), with no manual switching required.
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = IS_PRODUCTION

# ── Mail ──────────────────────────────────────────────────────────────────────
for k, v in MAIL_CONFIG.items():
    app.config[k] = v
mail = Mail(app)
app.extensions['mail'] = mail

# ── Cloudinary ────────────────────────────────────────────────────────────────
init_cloudinary()

# ── Firebase Admin SDK (FCM) ──────────────────────────────────────────────────
from firebase_config import init_firebase
init_firebase()          # idempotent — safe to call multiple times

# ── Blueprints ────────────────────────────────────────────────────────────────
from master import master_bp
from super_admin import super_bp
from admin import admin_bp
from auth import auth_bp
from mobile_api import mobile_bp

app.register_blueprint(auth_bp,   url_prefix='/auth')
app.register_blueprint(master_bp, url_prefix='/master')
app.register_blueprint(super_bp,  url_prefix='/super')
app.register_blueprint(admin_bp,  url_prefix='/admin')
app.register_blueprint(mobile_bp, url_prefix='/api/mobile')

# ── Request logger middleware ─────────────────────────────────────────────────
@app.after_request
def log_request(response):
    if request.path.startswith('/static'):
        return response
    user_id   = session.get('user_id')
    user_role = session.get('role')
    if user_id:
        log_activity(
            user_id=user_id,
            user_role=user_role,
            action=f"{request.method} {request.path}",
            endpoint=request.endpoint,
            method=request.method,
            ip=request.remote_addr,
            status_code=response.status_code
        )
    return response


# ── Serve firebase-messaging-sw.js from root (REQUIRED by FCM) ───────────────
# The service worker MUST be served from the root of the domain, not /static/.
# FCM will not work if the SW is scoped under /static/.
@app.route('/firebase-messaging-sw.js')
def firebase_sw():
    from flask import send_from_directory
    return send_from_directory(
        os.path.join(app.root_path, 'static'),
        'firebase-messaging-sw.js',
        mimetype='application/javascript'
    )


# ── Index redirect ────────────────────────────────────────────────────────────
@app.route('/')
def index():
    if 'user_id' in session:
        role = session.get('role')
        if role == 'master':      return redirect(url_for('master.dashboard'))
        if role == 'super_admin': return redirect(url_for('super.dashboard'))
        if role == 'admin':       return redirect(url_for('admin.dashboard'))
    return redirect(url_for('auth.login'))


# ── Bail alerts API (Accused-based) ───────────────────────────────────────────
@app.route('/api/bail-alerts')
def bail_alerts_api():
    if 'user_id' not in session:
        return jsonify([])
    district = session.get('district')
    role     = session.get('role')
    alerts   = get_accused_bail_alerts(district if role != 'master' else None)
    return jsonify(alerts)


# ── Notification count API (existing) ─────────────────────────────────────────
@app.route("/api/notifications-count")
def notifications_count():
    if "user_id" not in session:
        return jsonify({"count": 0})
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT COUNT(*) AS c
        FROM notifications
        WHERE user_id=%s AND is_read=0
    """, (session["user_id"],))
    count = cursor.fetchone()["c"]
    cursor.close()
    conn.close()
    return jsonify({"count": count})


# ── Notification preview API (NEW — powers the bell dropdown) ─────────────────
@app.route("/api/notifications-preview")
def notifications_preview():
    """
    Returns the 8 most recent notifications for the logged-in user.
    Used by the top-bar bell dropdown in base.html via loadNotifPanel().
    Also returns unread count for the badge.
    """
    if "user_id" not in session:
        return jsonify({"notifications": [], "unread": 0})

    user_id = session["user_id"]
    conn    = get_connection()
    cursor  = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT id, type, title, message, is_read,
               DATE_FORMAT(created_at, '%%d %%b %%Y, %%H:%%i') AS created_at
        FROM notifications
        WHERE user_id = %s
        ORDER BY created_at DESC
        LIMIT 8
    """, (user_id,))
    notifications = cursor.fetchall()

    cursor.execute("""
        SELECT COUNT(*) AS c FROM notifications
        WHERE user_id=%s AND is_read=0
    """, (user_id,))
    unread = cursor.fetchone()["c"]

    # Convert tinyint to bool for JSON
    for n in notifications:
        n["is_read"] = bool(n["is_read"])

    cursor.close()
    conn.close()
    return jsonify({"notifications": notifications, "unread": unread})


# ── FCM Token APIs (NEW) ──────────────────────────────────────────────────────

@app.route('/api/fcm/save-token', methods=['POST'])
def fcm_save_token():
    """
    Save or update an FCM registration token for the current user.
    Called from:
      - Web: after Firebase.getToken() in firebase-init.js
      - Flutter: after FirebaseMessaging.getToken()

    Body JSON: { "token": "<fcm_token>", "device_type": "web"|"android"|"ios" }
    """
    if "user_id" not in session:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    data = request.get_json(silent=True) or {}
    token       = data.get("token", "").strip()
    device_type = data.get("device_type", "web").strip()

    if not token:
        return jsonify({"success": False, "error": "Token is required"}), 400

    if device_type not in ("web", "android", "ios"):
        device_type = "web"

    try:
        from fcm_service import save_fcm_token
        ok = save_fcm_token(session["user_id"], token, device_type)
        return jsonify({"success": ok})
    except Exception as e:
        logger.error(f"[FCM] /api/fcm/save-token error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/fcm/delete-token', methods=['POST'])
def fcm_delete_token():
    """
    Remove a specific FCM token (called on logout).
    Body JSON: { "token": "<fcm_token>" }

    If token is omitted, removes ALL tokens for the current user.
    """
    if "user_id" not in session:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    data  = request.get_json(silent=True) or {}
    token = data.get("token", "").strip()

    try:
        from fcm_service import delete_fcm_token, delete_user_tokens
        if token:
            ok = delete_fcm_token(token)
        else:
            ok = delete_user_tokens(session["user_id"])
        return jsonify({"success": ok})
    except Exception as e:
        logger.error(f"[FCM] /api/fcm/delete-token error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/fcm/send-test', methods=['POST'])
def fcm_send_test():
    """
    Dev-only endpoint to test push to the current user.
    Body JSON: { "title": "...", "body": "..." }
    Remove or protect this route in production.
    """
    if "user_id" not in session:
        return jsonify({"success": False}), 401

    data  = request.get_json(silent=True) or {}
    title = data.get("title", "Test Notification")
    body  = data.get("body",  "FCM is working correctly!")

    try:
        from fcm_service import push_to_users
        result = push_to_users(
            user_ids=[session["user_id"]],
            title=title,
            body=body,
            data={"type": "test", "route": "/notifications"}
        )
        return jsonify(result)
    except Exception as e:
        logger.error(f"[FCM] /api/fcm/send-test error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ── Legacy stub (keep for backward compat, now delegates to proper service) ───
@app.route('/save_fcm_token', methods=['POST'])
def save_fcm_token_legacy():
    """Backward-compatible shim — delegates to the new endpoint."""
    if "user_id" not in session:
        return jsonify({"success": False}), 401
    data  = request.get_json(silent=True) or {}
    token = data.get("token", "").strip()
    if not token:
        return jsonify({"success": False}), 400
    from fcm_service import save_fcm_token
    ok = save_fcm_token(session["user_id"], token, "web")
    return jsonify({"success": ok})


# ── Error handlers (existing) ─────────────────────────────────────────────────
@app.errorhandler(404)
def not_found(e):
    return render_template('shared/error.html', code=404,
                           message="Page not found"), 404

@app.errorhandler(403)
def forbidden(e):
    return render_template('shared/error.html', code=403,
                           message="Access denied"), 403

@app.errorhandler(500)
def server_error(e):
    return render_template('shared/error.html', code=500,
                           message="Internal server error"), 500


if __name__ == '__main__':
    init_db()
    app.run(debug=True, host='0.0.0.0', port=5000)
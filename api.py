"""
api.py
======
JSON REST API for the Jail Rehai mobile app (React Native / Expo).

Design goals
------------
- Runs inside the SAME Flask app, on the SAME database, with the SAME
  business rules as the existing HTML web app (admin.py / super_admin.py /
  master.py / auth.py). Nothing in the web app is changed or duplicated
  in behaviour — this module only exposes the data as JSON instead of
  rendered HTML, and authenticates via a JWT bearer token instead of a
  browser session cookie (mobile apps can't easily hold Flask session
  cookies across app restarts/background states the way a browser can).
- Every endpoint enforces the same role checks as the web routes
  (admin_required / super_required / master_required equivalents).
- Where the underlying helper functions in accused_common.py / bail_bulk.py
  are pure (no render_template/redirect), they are called directly so the
  logic is 100% shared. Where the helpers are tightly coupled to
  request/render_template (most list/detail screens), the same SQL is
  mirrored here so the returned data is identical in shape/meaning.

Auth
----
POST /api/auth/login          -> { token, user }
POST /api/auth/logout         -> clears FCM token (client discards JWT)
POST /api/auth/forgot-password
POST /api/auth/verify-otp
POST /api/auth/change-password   (authenticated)

All other endpoints require header:  Authorization: Bearer <token>
"""
import logging
from datetime import datetime, timedelta
from functools import wraps

import jwt
from flask import Blueprint, request, jsonify, current_app, g
from werkzeug.security import check_password_hash, generate_password_hash

from config import SECRET_KEY
from db import get_connection
from utils import (log_activity, upload_image, get_notifications,
                    mark_notifications_read, paginate_query,
                    get_accused_bail_alerts, auto_complete_expired_accused_bails,
                    generate_otp)
from bail_bulk import (stage_bail_excel, get_batch_review, resolve_ambiguous_row,
                        discard_batch, confirm_batch, list_batches,
                        list_pending_photo_bails, complete_bail_photo)

api_bp = Blueprint('api', __name__)
logger = logging.getLogger(__name__)

JWT_ALGO = 'HS256'
JWT_EXPIRY_HOURS = 24 * 14  # 14 days — mobile sessions stay signed in


# ══════════════════════════════════════════════════════════════════════════
# JWT helpers
# ══════════════════════════════════════════════════════════════════════════

def _make_token(user: dict) -> str:
    payload = {
        'uid': user['id'],
        'user_id': user['user_id'],
        'role': user['role'],
        'district': user['district'],
        'name': user['name'],
        'exp': datetime.utcnow() + timedelta(hours=JWT_EXPIRY_HOURS),
        'iat': datetime.utcnow(),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=JWT_ALGO)


def _decode_token(token: str):
    return jwt.decode(token, SECRET_KEY, algorithms=[JWT_ALGO])


def token_required(roles=None):
    """Decorator: validates Bearer token, populates flask.g.user, optionally
    restricts to a set of roles (mirrors admin_required/super_required/
    master_required from the web blueprints)."""
    def outer(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            auth_header = request.headers.get('Authorization', '')
            if not auth_header.startswith('Bearer '):
                return jsonify(error='missing_token', message='Authorization header required'), 401
            token = auth_header.split(' ', 1)[1].strip()
            try:
                payload = _decode_token(token)
            except jwt.ExpiredSignatureError:
                return jsonify(error='token_expired', message='Session expired, please log in again'), 401
            except jwt.InvalidTokenError:
                return jsonify(error='invalid_token', message='Invalid session token'), 401

            if roles and payload.get('role') not in roles:
                return jsonify(error='forbidden', message='पहुँच अस्वीकृत / Access denied'), 403

            g.user = payload
            return f(*args, **kwargs)
        return wrapped
    return outer


def _bp_role():
    """admin/super_admin share the 'admin'/'super' table-prefix split used
    throughout accused_common.py"""
    return 'admin' if g.user['role'] == 'admin' else 'super'


# ══════════════════════════════════════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════════════════════════════════════

@api_bp.route('/auth/login', methods=['POST'])
def api_login():
    data = request.get_json(silent=True) or request.form
    user_id = (data.get('user_id') or '').strip()
    password = (data.get('password') or '').strip()

    if not user_id or not password:
        return jsonify(error='missing_fields', message='User ID and password are required'), 400

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM users WHERE user_id=%s", (user_id,))
    user = cursor.fetchone()
    cursor.close(); conn.close()

    if not user:
        return jsonify(error='invalid_credentials', message='Invalid User ID or password'), 401
    if not user['is_active']:
        return jsonify(error='account_revoked', message='Your account has been revoked. Contact administrator.'), 403
    if not check_password_hash(user['password_hash'], password):
        return jsonify(error='invalid_credentials', message='Invalid User ID or password'), 401

    token = _make_token(user)
    log_activity(user['id'], user['role'], 'User logged in (mobile)', ip=request.remote_addr)

    return jsonify(
        token=token,
        user={
            'id': user['id'], 'user_id': user['user_id'], 'name': user['name'],
            'role': user['role'], 'email': user['email'], 'district': user['district'],
            'designation': user['designation'],
        }
    )


@api_bp.route('/auth/logout', methods=['POST'])
@token_required()
def api_logout():
    try:
        from fcm_service import delete_user_tokens
        delete_user_tokens(g.user['uid'])
    except Exception as e:
        logger.warning(f"[FCM] Could not delete tokens on logout: {e}")
    log_activity(g.user['uid'], g.user['role'], 'User logged out (mobile)', ip=request.remote_addr)
    return jsonify(message='logged_out')


@api_bp.route('/auth/forgot-password', methods=['POST'])
def api_forgot_password():
    data = request.get_json(silent=True) or request.form
    email = (data.get('email') or '').strip()
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM users WHERE email=%s", (email,))
    user = cursor.fetchone()
    if not user:
        cursor.close(); conn.close()
        return jsonify(error='not_found', message='No account found with this email'), 404

    otp = generate_otp()
    expiry = datetime.now() + timedelta(minutes=10)
    cursor.execute("UPDATE users SET otp_code=%s, otp_expiry=%s WHERE id=%s", (otp, expiry, user['id']))
    conn.commit(); cursor.close(); conn.close()

    try:
        from flask_mail import Message
        mail = current_app.extensions['mail']
        msg = Message('Password Reset OTP - Jail Rehai', recipients=[email], sender='noreply@jailrehai.gov.in')
        msg.body = f"Dear {user['name']},\n\nYour OTP for password reset is: {otp}\n\nValid for 10 minutes."
        mail.send(msg)
    except Exception as e:
        logger.error(f"Mail error: {e}")

    return jsonify(message='otp_sent', email=email)


@api_bp.route('/auth/verify-otp', methods=['POST'])
def api_verify_otp():
    data = request.get_json(silent=True) or request.form
    email = (data.get('email') or '').strip()
    otp = (data.get('otp') or '').strip()
    new_pass = (data.get('new_password') or '').strip()

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM users WHERE email=%s", (email,))
    user = cursor.fetchone()

    if not user or user['otp_code'] != otp or datetime.now() > user['otp_expiry']:
        cursor.close(); conn.close()
        return jsonify(error='invalid_otp', message='Invalid or expired OTP'), 400

    cursor.execute(
        "UPDATE users SET password_hash=%s, otp_code=NULL, otp_expiry=NULL WHERE id=%s",
        (generate_password_hash(new_pass), user['id'])
    )
    conn.commit(); cursor.close(); conn.close()
    return jsonify(message='password_reset')


@api_bp.route('/auth/change-password', methods=['POST'])
@token_required()
def api_change_password():
    data = request.get_json(silent=True) or request.form
    current = (data.get('current_password') or '').strip()
    new_pass = (data.get('new_password') or '').strip()

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM users WHERE id=%s", (g.user['uid'],))
    user = cursor.fetchone()
    if not check_password_hash(user['password_hash'], current):
        cursor.close(); conn.close()
        return jsonify(error='wrong_password', message='Current password is incorrect'), 400
    cursor.execute("UPDATE users SET password_hash=%s WHERE id=%s",
                   (generate_password_hash(new_pass), g.user['uid']))
    conn.commit(); cursor.close(); conn.close()
    return jsonify(message='password_changed')


@api_bp.route('/auth/me', methods=['GET'])
@token_required()
def api_me():
    return jsonify(user=g.user)


# ══════════════════════════════════════════════════════════════════════════
# DASHBOARD  (mirrors admin.py / super_admin.py / master.py dashboards)
# ══════════════════════════════════════════════════════════════════════════

@api_bp.route('/dashboard', methods=['GET'])
@token_required()
def api_dashboard():
    role = g.user['role']
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    if role == 'master':
        cursor.execute("SELECT COUNT(*) as c FROM users WHERE role='super_admin'")
        super_count = cursor.fetchone()['c']
        cursor.execute("SELECT COUNT(*) as c FROM users WHERE role='admin'")
        admin_count = cursor.fetchone()['c']
        cursor.execute("SELECT COUNT(*) as c FROM users WHERE is_active=0")
        revoked_count = cursor.fetchone()['c']
        cursor.execute("SELECT COUNT(*) as c FROM activity_logs")
        log_count = cursor.fetchone()['c']
        cursor.close(); conn.close()
        return jsonify(role=role, stats={
            'super_admins': super_count, 'admins': admin_count,
            'revoked': revoked_count, 'logs': log_count,
        })

    district = g.user['district']
    auto_complete_expired_accused_bails(district)

    if role == 'super_admin':
        cursor.execute("SELECT COUNT(*) as c FROM users WHERE created_by=%s AND role='admin' AND is_active=1",
                       (g.user['uid'],))
        admins_count = cursor.fetchone()['c']

    cursor.execute("""
        SELECT COUNT(DISTINCT a.id) as c FROM accused a
        JOIN accused_fir af ON af.accused_id=a.id
        JOIN fir_cases f ON f.id=af.fir_id WHERE f.district=%s
    """, (district,))
    accused_count = cursor.fetchone()['c']
    cursor.execute("""
        SELECT COUNT(DISTINCT a.id) as c FROM accused a
        JOIN accused_fir af ON af.accused_id=a.id
        JOIN fir_cases f ON f.id=af.fir_id
        WHERE f.district=%s AND a.profile_status='pending'
    """, (district,))
    pending_count = cursor.fetchone()['c']
    cursor.execute("""
        SELECT COUNT(DISTINCT a.id) as c FROM accused a
        JOIN accused_fir af ON af.accused_id=a.id
        JOIN fir_cases f ON f.id=af.fir_id
        WHERE f.district=%s AND a.bail_status!='none'
    """, (district,))
    bail_count = cursor.fetchone()['c']
    cursor.execute("SELECT COUNT(*) as c FROM fir_cases WHERE district=%s", (district,))
    fir_count = cursor.fetchone()['c']
    cursor.close(); conn.close()

    alerts = get_accused_bail_alerts(district)
    stats = {'accused': accused_count, 'pending': pending_count,
             'on_bail': bail_count, 'firs': fir_count}
    if role == 'super_admin':
        stats['admins'] = admins_count

    return jsonify(role=role, stats=stats, alerts=alerts)


# ══════════════════════════════════════════════════════════════════════════
# ACCUSED  (mirrors accused_common.get_accused_list / get_accused_detail)
# ══════════════════════════════════════════════════════════════════════════

@api_bp.route('/accused', methods=['GET'])
@token_required(roles=('admin', 'super_admin'))
def api_accused_list():
    district = g.user['district']
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 25))
    search = request.args.get('search', '').strip()
    thana_f = request.args.get('thana', '').strip()
    fir_f = request.args.get('fir', '').strip()
    status_f = request.args.get('status', '').strip()

    conditions = ["f.district=%s"]
    params = [district]
    if thana_f:
        conditions.append("f.thana LIKE %s"); params.append(f'%{thana_f}%')
    if fir_f:
        conditions.append("f.fir_number LIKE %s"); params.append(f'%{fir_f}%')
    if status_f in ('pending', 'complete'):
        conditions.append("a.profile_status=%s"); params.append(status_f)
    if search:
        conditions.append("(a.name LIKE %s OR a.fathers_name LIKE %s)")
        like = f'%{search}%'; params += [like, like]
    where = " AND ".join(conditions)

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    base_q = f"""
        SELECT DISTINCT
            a.id, a.name, a.fathers_name, a.photo_url, a.profile_status, a.bail_status,
            (SELECT COUNT(*) FROM accused_fir af2 WHERE af2.accused_id=a.id) AS fir_count,
            (SELECT GROUP_CONCAT(DISTINCT f2.thana ORDER BY f2.thana SEPARATOR ', ')
             FROM accused_fir af3 JOIN fir_cases f2 ON f2.id=af3.fir_id
             WHERE af3.accused_id=a.id AND f2.district=%s) AS thanas,
            MAX(af.in_arrested) AS ever_arrested
        FROM accused a
        JOIN accused_fir af ON af.accused_id = a.id
        JOIN fir_cases f ON f.id = af.fir_id
        WHERE {where}
        GROUP BY a.id
        ORDER BY a.name
    """
    params_full = [district] + params
    rows, total, total_pages = paginate_query(cursor, base_q, params_full, page, per_page)
    cursor.close(); conn.close()

    return jsonify(accused=rows, page=page, total=total, total_pages=total_pages, per_page=per_page)


@api_bp.route('/accused/<int:accused_id>', methods=['GET'])
@token_required(roles=('admin', 'super_admin'))
def api_accused_detail(accused_id):
    district = g.user['district']
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM accused WHERE id=%s", (accused_id,))
    accused = cursor.fetchone()
    if not accused:
        cursor.close(); conn.close()
        return jsonify(error='not_found', message='अभियुक्त नहीं मिला'), 404

    cursor.execute("""
        SELECT f.*, af.in_total_accused, af.in_fir_accused, af.in_arrested, af.in_cs_accused
        FROM accused_fir af JOIN fir_cases f ON f.id = af.fir_id
        WHERE af.accused_id = %s AND f.district = %s ORDER BY f.fir_number
    """, (accused_id, district))
    firs = cursor.fetchall()

    cursor.execute("SELECT * FROM accused_photos WHERE accused_id=%s ORDER BY uploaded_at DESC", (accused_id,))
    photos = cursor.fetchall()

    is_arrested = any(f.get('in_arrested') for f in firs)
    arrest_firs = [f for f in firs if f.get('in_arrested')]
    has_active_bail = bool(accused.get('bail_status') and accused['bail_status'] != 'none')

    cursor.execute("""
        SELECT abh.*, abh.status AS bail_history_status, f.fir_number, f.thana, f.district,
               GROUP_CONCAT(DISTINCT CONCAT(f2.fir_number,'/',f2.thana)
                            ORDER BY f2.fir_number SEPARATOR ', ') AS all_firs,
               COUNT(DISTINCT f2.id) AS fir_count,
               u1.name AS approved_by_name, u2.name AS revoked_by_name
        FROM accused_bail_history abh
        JOIN fir_cases f ON f.id = abh.fir_id
        LEFT JOIN accused_bail_fir abf ON abf.bail_id = abh.id
        LEFT JOIN fir_cases f2 ON f2.id = abf.fir_id
        LEFT JOIN users u1 ON u1.id = abh.approved_by
        LEFT JOIN users u2 ON u2.id = abh.revoked_by
        WHERE abh.accused_id = %s
        GROUP BY abh.id
        ORDER BY abh.approved_at DESC
    """, (accused_id,))
    bail_history = cursor.fetchall()
    for b in bail_history:
        if not b.get('all_firs'):
            b['all_firs'] = f"{b['fir_number']}/{b['thana']}"
            b['fir_count'] = 1
    cursor.close(); conn.close()

    return jsonify(accused=accused, firs=firs, photos=photos, is_arrested=is_arrested,
                    arrest_firs=arrest_firs, has_active_bail=has_active_bail,
                    bail_history=bail_history)


@api_bp.route('/accused/<int:accused_id>/upload-photo', methods=['POST'])
@token_required(roles=('admin', 'super_admin'))
def api_accused_upload_photo(accused_id):
    if 'photo' not in request.files:
        return jsonify(error='missing_file', message='photo file required'), 400
    file = request.files['photo']
    url, public_id = upload_image(file)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE accused SET photo_url=%s WHERE id=%s", (url, accused_id))
    cursor.execute(
        "INSERT INTO accused_photos (accused_id, photo_url, public_id, uploaded_by) VALUES (%s,%s,%s,%s)",
        (accused_id, url, public_id, g.user['uid'])
    )
    conn.commit(); cursor.close(); conn.close()
    log_activity(g.user['uid'], g.user['role'], f"Uploaded photo for accused ID:{accused_id}", ip=request.remote_addr)
    return jsonify(message='photo_uploaded', photo_url=url)


@api_bp.route('/accused/<int:accused_id>/approve-bail', methods=['POST'])
@token_required(roles=('admin', 'super_admin'))
def api_approve_bail(accused_id):
    data = request.get_json(silent=True) or request.form
    fir_ids = data.get('fir_ids') or []
    bail_type = data.get('bail_type', 'temporary')
    remark = (data.get('remark') or '').strip()
    start_date = data.get('bail_start_date') or datetime.now().strftime('%Y-%m-%d')
    end_date = data.get('bail_end_date')

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM accused WHERE id=%s", (accused_id,))
    accused = cursor.fetchone()
    if not accused:
        cursor.close(); conn.close()
        return jsonify(error='not_found', message='अभियुक्त नहीं मिला'), 404

    cursor.execute("""
        SELECT f.id, f.fir_number, f.thana FROM accused_fir af JOIN fir_cases f ON f.id=af.fir_id
        WHERE af.accused_id=%s AND af.in_arrested=1
    """, (accused_id,))
    arrest_firs = cursor.fetchall()
    if not arrest_firs:
        cursor.close(); conn.close()
        return jsonify(error='not_eligible', message='जमानत केवल गिरफ़्तार अभियुक्तों के लिए स्वीकृत की जा सकती है'), 400

    if accused.get('bail_status') and accused['bail_status'] != 'none':
        cursor.close(); conn.close()
        return jsonify(error='already_on_bail', message='इस अभियुक्त की जमानत पहले से सक्रिय है'), 400

    if not fir_ids:
        fir_ids = [f['id'] for f in arrest_firs]
    primary_fir = fir_ids[0]

    cursor.execute("""
        INSERT INTO accused_bail_history
            (accused_id, fir_id, bail_type, bail_start_date, bail_end_date,
             bail_remark, status, approved_by, approved_at)
        VALUES (%s,%s,%s,%s,%s,%s,'ACTIVE',%s,NOW())
    """, (accused_id, primary_fir, bail_type, start_date, end_date, remark, g.user['uid']))
    bail_id = cursor.lastrowid
    for fid in fir_ids:
        cursor.execute(
            "INSERT INTO accused_bail_fir (bail_id, fir_id) VALUES (%s,%s)", (bail_id, fid))
    cursor.execute("""
        UPDATE accused SET bail_status=%s, bail_start_date=%s, bail_end_date=%s,
        bail_remark=%s, updated_by=%s WHERE id=%s
    """, (bail_type, start_date, end_date, remark, g.user['uid'], accused_id))
    conn.commit(); cursor.close(); conn.close()

    log_activity(g.user['uid'], g.user['role'], f"Approved bail for accused ID:{accused_id}", ip=request.remote_addr)
    return jsonify(message='bail_approved', bail_id=bail_id)


@api_bp.route('/accused/<int:accused_id>/revoke-bail', methods=['POST'])
@token_required(roles=('admin', 'super_admin'))
def api_revoke_bail(accused_id):
    data = request.get_json(silent=True) or request.form
    revoke_reason = (data.get('revoke_reason') or '').strip()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE accused_bail_history
        SET status='REVOKED', revoked_by=%s, revoked_at=NOW(), revoke_reason=%s
        WHERE accused_id=%s AND status='ACTIVE'
        ORDER BY approved_at DESC LIMIT 1
    """, (g.user['uid'], revoke_reason or None, accused_id))
    cursor.execute("""
        UPDATE accused SET bail_status='none', bail_start_date=NULL, bail_end_date=NULL,
        bail_documents_url=NULL, bail_documents_public_id=NULL,
        bail_photo_url=NULL, bail_photo_public_id=NULL,
        bail_photo_lat=NULL, bail_photo_lng=NULL, bail_photo_captured_at=NULL,
        bail_remark=NULL, bail_rating=0, updated_by=%s WHERE id=%s
    """, (g.user['uid'], accused_id))
    conn.commit(); cursor.close(); conn.close()
    log_activity(g.user['uid'], g.user['role'], f"Revoked bail for accused ID:{accused_id}", ip=request.remote_addr)
    return jsonify(message='bail_revoked')


@api_bp.route('/bailed-accused', methods=['GET'])
@token_required(roles=('admin', 'super_admin'))
def api_bailed_accused():
    district = g.user['district']
    status_filter = request.args.get('status', 'ACTIVE')
    bail_type_filter = request.args.get('bail_type', '')
    search = request.args.get('search', '').strip()
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 25))

    conditions = ["f.district=%s"]
    params = [district]
    if status_filter in ('ACTIVE', 'REVOKED', 'COMPLETED'):
        conditions.append("abh.status=%s"); params.append(status_filter)
    if bail_type_filter in ('temporary', 'permanent'):
        conditions.append("abh.bail_type=%s"); params.append(bail_type_filter)
    if search:
        conditions.append("(a.name LIKE %s OR a.fathers_name LIKE %s)")
        like = f'%{search}%'; params += [like, like]
    where = " AND ".join(conditions)

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    base_q = f"""
        SELECT abh.id AS bail_id, abh.bail_type, abh.bail_start_date, abh.bail_end_date,
               abh.bail_remark, abh.status AS bail_history_status,
               abh.approved_at, abh.revoked_at, abh.completed_at, abh.revoke_reason,
               a.id AS accused_id, a.name, a.fathers_name, a.photo_url,
               f.fir_number, f.thana, f.district,
               g.name AS approved_by_name, r.name AS revoked_by_name
        FROM accused_bail_history abh
        JOIN accused a ON a.id = abh.accused_id
        JOIN fir_cases f ON f.id = abh.fir_id
        LEFT JOIN users g ON g.id = abh.approved_by
        LEFT JOIN users r ON r.id = abh.revoked_by
        WHERE {where}
        GROUP BY abh.id
        ORDER BY abh.approved_at DESC
    """
    rows, total, total_pages = paginate_query(cursor, base_q, params, page, per_page)
    cursor.close(); conn.close()
    return jsonify(bail_records=rows, page=page, total=total, total_pages=total_pages, per_page=per_page)


# ══════════════════════════════════════════════════════════════════════════
# FIR
# ══════════════════════════════════════════════════════════════════════════

@api_bp.route('/fir', methods=['GET'])
@token_required(roles=('admin', 'super_admin'))
def api_fir_list():
    district = g.user['district']
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 25))
    search = request.args.get('search', '').strip()
    thana_f = request.args.get('thana', '').strip()
    fir_f = request.args.get('fir', '').strip()

    conditions = ["f.district=%s"]
    params = [district]
    if thana_f:
        conditions.append("f.thana LIKE %s"); params.append(f'%{thana_f}%')
    if fir_f:
        conditions.append("f.fir_number LIKE %s"); params.append(f'%{fir_f}%')
    if search:
        conditions.append("(f.fir_number LIKE %s OR f.thana LIKE %s OR f.complainant LIKE %s OR f.acts LIKE %s)")
        like = f'%{search}%'; params += [like, like, like, like]
    where = " AND ".join(conditions)

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    base_q = f"""
        SELECT f.*, (SELECT COUNT(*) FROM accused_fir af WHERE af.fir_id=f.id) AS accused_count
        FROM fir_cases f WHERE {where} ORDER BY f.created_at DESC
    """
    rows, total, total_pages = paginate_query(cursor, base_q, params, page, per_page)
    cursor.close(); conn.close()
    return jsonify(firs=rows, page=page, total=total, total_pages=total_pages, per_page=per_page)


@api_bp.route('/fir/<int:fir_id>', methods=['GET'])
@token_required(roles=('admin', 'super_admin'))
def api_fir_detail(fir_id):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM fir_cases WHERE id=%s", (fir_id,))
    fir = cursor.fetchone()
    if not fir:
        cursor.close(); conn.close()
        return jsonify(error='not_found', message='FIR नहीं मिला'), 404
    cursor.execute("""
        SELECT a.*, af.in_total_accused, af.in_fir_accused, af.in_arrested, af.in_cs_accused
        FROM accused_fir af JOIN accused a ON a.id = af.accused_id
        WHERE af.fir_id=%s ORDER BY a.name
    """, (fir_id,))
    accused = cursor.fetchall()
    cursor.close(); conn.close()
    return jsonify(fir=fir, accused=accused)


@api_bp.route('/fir/add', methods=['POST'])
@token_required(roles=('admin', 'super_admin'))
def api_fir_add():
    data = request.get_json(silent=True) or request.form
    required = ['thana', 'fir_number']
    if not all((data.get(k) or '').strip() for k in required):
        return jsonify(error='missing_fields', message='Thana and FIR number are required'), 400

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO fir_cases (district, thana, fir_number, acts, complainant, status, created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, (g.user['district'], data.get('thana', '').strip(), data.get('fir_number', '').strip(),
              data.get('acts', '').strip(), data.get('complainant', '').strip(),
              data.get('status', '').strip(), g.user['uid']))
        conn.commit()
        fir_id = cursor.lastrowid
    except Exception as e:
        conn.rollback()
        cursor.close(); conn.close()
        return jsonify(error='duplicate_or_db_error', message=str(e)), 400
    cursor.close(); conn.close()
    log_activity(g.user['uid'], g.user['role'], f"Created FIR ID:{fir_id}", ip=request.remote_addr)
    return jsonify(message='fir_created', fir_id=fir_id)


# ══════════════════════════════════════════════════════════════════════════
# NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════════

@api_bp.route('/notifications', methods=['GET'])
@token_required()
def api_notifications():
    notifs = get_notifications(g.user['uid'], limit=50)
    mark_notifications_read(g.user['uid'])
    return jsonify(notifications=notifs)


@api_bp.route('/notifications-preview', methods=['GET'])
@token_required()
def api_notifications_preview():
    notifs = get_notifications(g.user['uid'], limit=8)
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT COUNT(*) as c FROM notifications WHERE user_id=%s AND is_read=0", (g.user['uid'],))
    unread = cursor.fetchone()['c']
    cursor.close(); conn.close()
    return jsonify(notifications=notifs, unread=unread)


# ══════════════════════════════════════════════════════════════════════════
# BAIL EXCEL BULK UPLOAD  (delegates to bail_bulk.py pure functions directly)
# ══════════════════════════════════════════════════════════════════════════

@api_bp.route('/bail-excel/upload', methods=['POST'])
@token_required(roles=('admin', 'super_admin'))
def api_bail_excel_upload():
    if 'file' not in request.files:
        return jsonify(error='missing_file', message='Excel file required'), 400
    file = request.files['file']
    batch_id = stage_bail_excel(file, file.filename, g.user['district'], g.user['uid'])
    return jsonify(message='batch_staged', batch_id=batch_id)


@api_bp.route('/bail-excel/batches', methods=['GET'])
@token_required(roles=('admin', 'super_admin'))
def api_bail_excel_batches():
    return jsonify(batches=list_batches(g.user['district']))


@api_bp.route('/bail-excel/batch/<int:batch_id>', methods=['GET'])
@token_required(roles=('admin', 'super_admin'))
def api_bail_excel_batch(batch_id):
    batch, rows = get_batch_review(batch_id, g.user['district'])
    if not batch:
        return jsonify(error='not_found', message='Batch not found'), 404
    return jsonify(batch=batch, rows=rows)


@api_bp.route('/bail-excel/batch/<int:batch_id>/row/<int:row_id>/resolve', methods=['POST'])
@token_required(roles=('admin', 'super_admin'))
def api_bail_excel_resolve(batch_id, row_id):
    data = request.get_json(silent=True) or request.form
    accused_id = int(data.get('accused_id'))
    resolve_ambiguous_row(batch_id, row_id, accused_id, g.user['district'])
    return jsonify(message='row_resolved')


@api_bp.route('/bail-excel/batch/<int:batch_id>/confirm', methods=['POST'])
@token_required(roles=('admin', 'super_admin'))
def api_bail_excel_confirm(batch_id):
    data = request.get_json(silent=True) or request.form
    row_ids = data.get('row_ids') or []
    confirm_batch(batch_id, g.user['district'], row_ids, g.user['uid'])
    return jsonify(message='batch_confirmed')


@api_bp.route('/bail-excel/batch/<int:batch_id>/discard', methods=['POST'])
@token_required(roles=('admin', 'super_admin'))
def api_bail_excel_discard(batch_id):
    discard_batch(batch_id, g.user['district'], g.user['uid'])
    return jsonify(message='batch_discarded')


@api_bp.route('/bail-pending-photos', methods=['GET'])
@token_required(roles=('admin', 'super_admin'))
def api_bail_pending_photos():
    return jsonify(pending=list_pending_photo_bails(g.user['district']))


@api_bp.route('/bail-pending-photos/<int:bail_id>/complete', methods=['POST'])
@token_required(roles=('admin', 'super_admin'))
def api_bail_pending_photo_complete(bail_id):
    """Accepts a base64 data-URL 'photo_data' and/or a multipart 'document'
    file — mirrors complete_bail_photo()'s signature exactly."""
    doc_file = request.files.get('document')
    if request.is_json:
        photo_data = (request.get_json(silent=True) or {}).get('photo_data')
    else:
        photo_data = request.form.get('photo_data')
    ok, message = complete_bail_photo(bail_id, g.user['district'], g.user['uid'], photo_data, doc_file)
    if not ok:
        return jsonify(error='failed', message=message), 400
    return jsonify(message='photo_completed', detail=message)


# ══════════════════════════════════════════════════════════════════════════
# SUPER ADMIN — manage district admins  (mirrors super_admin.py)
# ══════════════════════════════════════════════════════════════════════════

@api_bp.route('/admins', methods=['GET'])
@token_required(roles=('super_admin',))
def api_admins_list():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT * FROM users WHERE created_by=%s AND role='admin' ORDER BY created_at DESC
    """, (g.user['uid'],))
    rows = cursor.fetchall()
    cursor.close(); conn.close()
    return jsonify(admins=rows)


@api_bp.route('/admins/create', methods=['POST'])
@token_required(roles=('super_admin',))
def api_admin_create():
    data = request.get_json(silent=True) or request.form
    required = ['user_id', 'name', 'email', 'password']
    if not all((data.get(k) or '').strip() for k in required):
        return jsonify(error='missing_fields', message='All required fields must be filled'), 400
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id FROM users WHERE user_id=%s OR email=%s", (data['user_id'], data['email']))
    if cursor.fetchone():
        cursor.close(); conn.close()
        return jsonify(error='duplicate', message='User ID or Email already exists'), 400
    cursor.execute("""
        INSERT INTO users (user_id,name,designation,contact,email,district,address,password_hash,role,created_by,is_active)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'admin',%s,1)
    """, (data['user_id'].strip(), data['name'].strip(), data.get('designation', '').strip(),
          data.get('contact', '').strip(), data['email'].strip(), g.user['district'],
          data.get('address', '').strip(), generate_password_hash(data['password'].strip()), g.user['uid']))
    conn.commit(); cursor.close(); conn.close()
    log_activity(g.user['uid'], 'super_admin', f"Created admin: {data['user_id']}", ip=request.remote_addr)
    return jsonify(message='admin_created')


@api_bp.route('/admins/<int:uid>/toggle', methods=['POST'])
@token_required(roles=('super_admin',))
def api_admin_toggle(uid):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM users WHERE id=%s AND created_by=%s AND role='admin'", (uid, g.user['uid']))
    user = cursor.fetchone()
    if not user:
        cursor.close(); conn.close()
        return jsonify(error='not_found', message='User not found'), 404
    new_status = 0 if user['is_active'] else 1
    cursor.execute("UPDATE users SET is_active=%s WHERE id=%s", (new_status, uid))
    conn.commit(); cursor.close(); conn.close()
    action = 'Revoked' if new_status == 0 else 'Restored'
    log_activity(g.user['uid'], 'super_admin', f"{action} admin: {user['user_id']}", ip=request.remote_addr)
    return jsonify(message='toggled', is_active=bool(new_status))


# ══════════════════════════════════════════════════════════════════════════
# MASTER — manage super admins / all admins / audit logs  (mirrors master.py)
# ══════════════════════════════════════════════════════════════════════════

@api_bp.route('/super-admins', methods=['GET'])
@token_required(roles=('master',))
def api_super_admins_list():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT u.*, (SELECT COUNT(*) FROM users a WHERE a.created_by=u.id AND a.role='admin') as admin_count
        FROM users u WHERE u.role='super_admin' ORDER BY u.created_at DESC
    """)
    rows = cursor.fetchall()
    cursor.close(); conn.close()
    return jsonify(super_admins=rows)


@api_bp.route('/super-admins/create', methods=['POST'])
@token_required(roles=('master',))
def api_super_admin_create():
    data = request.get_json(silent=True) or request.form
    required = ['user_id', 'name', 'email', 'password']
    if not all((data.get(k) or '').strip() for k in required):
        return jsonify(error='missing_fields', message='All required fields must be filled'), 400
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id FROM users WHERE user_id=%s OR email=%s", (data['user_id'], data['email']))
    if cursor.fetchone():
        cursor.close(); conn.close()
        return jsonify(error='duplicate', message='User ID or Email already exists'), 400
    cursor.execute("""
        INSERT INTO users (user_id,name,designation,contact,email,district,address,password_hash,role,created_by,is_active)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'super_admin',%s,1)
    """, (data['user_id'].strip(), data['name'].strip(), data.get('designation', '').strip(),
          data.get('contact', '').strip(), data['email'].strip(), data.get('district', '').strip(),
          data.get('address', '').strip(), generate_password_hash(data['password'].strip()), g.user['uid']))
    conn.commit(); cursor.close(); conn.close()
    log_activity(g.user['uid'], 'master', f"Created super admin: {data['user_id']}", ip=request.remote_addr)
    return jsonify(message='super_admin_created')


@api_bp.route('/all-admins', methods=['GET'])
@token_required(roles=('master',))
def api_all_admins():
    search = request.args.get('search', '')
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    base_q = """
        SELECT a.*, s.name as super_name, s.user_id as super_uid, s.district as super_district
        FROM users a LEFT JOIN users s ON a.created_by = s.id WHERE a.role='admin'
    """
    params = []
    if search:
        base_q += " AND (a.name LIKE %s OR a.user_id LIKE %s OR a.district LIKE %s)"
        params += [f'%{search}%', f'%{search}%', f'%{search}%']
    base_q += " ORDER BY a.created_at DESC"
    rows, total, total_pages = paginate_query(cursor, base_q, params, page, per_page)
    cursor.close(); conn.close()
    return jsonify(admins=rows, page=page, total=total, total_pages=total_pages, per_page=per_page)


@api_bp.route('/users/<int:uid>/toggle', methods=['POST'])
@token_required(roles=('master',))
def api_master_toggle_user(uid):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM users WHERE id=%s AND role!='master'", (uid,))
    user = cursor.fetchone()
    if not user:
        cursor.close(); conn.close()
        return jsonify(error='not_found', message='User not found'), 404
    new_status = 0 if user['is_active'] else 1
    cursor.execute("UPDATE users SET is_active=%s WHERE id=%s", (new_status, uid))
    conn.commit(); cursor.close(); conn.close()
    action = 'Revoked' if new_status == 0 else 'Restored'
    log_activity(g.user['uid'], 'master', f"{action} user: {user['user_id']}", ip=request.remote_addr)
    return jsonify(message='toggled', is_active=bool(new_status))


@api_bp.route('/logs', methods=['GET'])
@token_required(roles=('master',))
def api_logs():
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 50))
    role_filter = request.args.get('role', '')
    search = request.args.get('search', '')
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    base_q = """
        SELECT l.*, u.name as user_name, u.user_id as uid
        FROM activity_logs l LEFT JOIN users u ON l.user_id = u.id WHERE 1=1
    """
    params = []
    if role_filter:
        base_q += " AND l.user_role=%s"; params.append(role_filter)
    if search:
        base_q += " AND (u.name LIKE %s OR u.user_id LIKE %s OR l.action LIKE %s)"
        like = f'%{search}%'; params += [like, like, like]
    base_q += " ORDER BY l.created_at DESC"
    rows, total, total_pages = paginate_query(cursor, base_q, params, page, per_page)
    cursor.close(); conn.close()
    return jsonify(logs=rows, page=page, total=total, total_pages=total_pages, per_page=per_page)


# ══════════════════════════════════════════════════════════════════════════
# FCM push-token registration (mobile) — reuses existing fcm_service
# ══════════════════════════════════════════════════════════════════════════

@api_bp.route('/fcm/save-token', methods=['POST'])
@token_required()
def api_fcm_save_token():
    data = request.get_json(silent=True) or request.form
    token = data.get('token')
    if not token:
        return jsonify(error='missing_token', message='FCM token required'), 400
    try:
        from fcm_service import save_fcm_token
        save_fcm_token(g.user['uid'], token, device_type=data.get('platform', 'mobile'))
    except Exception as e:
        logger.warning(f"[FCM] save token failed: {e}")
        return jsonify(error='fcm_error', message=str(e)), 500
    return jsonify(message='token_saved')

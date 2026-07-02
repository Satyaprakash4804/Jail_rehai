"""
admin.py
========
District Admin routes. This system is fully Accused-based (अभियुक्त-आधारित)
— there is no criminal-management module.

Routes:
  /admin/dashboard              — जिला सारांश (Accused / FIR / bail stats)
  /admin/accused                — अभियुक्त सूची
  /admin/accused/<id>           — अभियुक्त विवरण
  /admin/accused/<id>/upload-photo
  /admin/accused/<id>/approve-bail   — केवल गिरफ़्तार अभियुक्तों के लिए, केवल
                                        उसी FIR के आधार पर जिसमें गिरफ़्तारी हुई
  /admin/accused/<id>/revoke-bail
  /admin/bailed-accused          — जमानत इतिहास
  /admin/fir                     — FIR सूची
  /admin/fir/<id>                — FIR विवरण
  /admin/fir/add                 — FIR मैन्युअल दर्ज करें
  /admin/upload-accused          — Excel बल्क अपलोड (FIR + अभियुक्त)
  /admin/download-accused-sample — Sample Excel टेम्पलेट
  /admin/search-by-photo         — FRS: फ़ोटो से अभियुक्त खोजें
  /admin/notifications           — सूचनाएं
"""
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify
from functools import wraps
from db import get_connection
from utils import (log_activity, upload_image, upload_document, upload_id_card_file,
                   get_notifications, mark_notifications_read, paginate_query,
                   get_accused_bail_alerts, auto_complete_expired_accused_bails)
from accused_common import (get_fir_list, get_fir_detail, get_accused_list,
                             get_accused_detail, upload_accused_excel,
                             create_fir_manual, download_accused_sample_file,
                             approve_accused_bail, revoke_accused_bail,
                             get_bailed_accused_list)
import logging
from datetime import datetime

admin_bp = Blueprint('admin', __name__)
logger   = logging.getLogger(__name__)


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session or session.get('role') not in ('admin', 'super_admin'):
            flash('पहुँच अस्वीकृत।', 'danger')
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated


# ══════════════════════════════════════════════════════════════════════════════
# DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

@admin_bp.route('/dashboard')
@admin_required
def dashboard():
    district = session.get('district')
    auto_complete_expired_accused_bails(district)
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
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
        WHERE f.district=%s AND a.profile_status='complete'
    """, (district,))
    complete_count = cursor.fetchone()['c']
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
    stats  = {
        'accused': accused_count, 'pending': pending_count,
        'complete': complete_count, 'on_bail': bail_count,
        'firs': fir_count,
    }
    return render_template('admin/dashboard.html', stats=stats, alerts=alerts)


@admin_bp.route('/notifications')
@admin_required
def notifications():
    notifs = get_notifications(session['user_id'], limit=50)
    mark_notifications_read(session['user_id'])
    return render_template('admin/notifications.html', notifications=notifs)


# ══════════════════════════════════════════════════════════════════════════════
# FRS — फ़ोटो से अभियुक्त खोजें (Face Recognition, Accused only)
# ══════════════════════════════════════════════════════════════════════════════

@admin_bp.route('/search-by-photo', methods=['GET', 'POST'])
@admin_required
def search_by_photo():
    results = []
    if request.method == 'POST':
        photo = request.files.get('photo')
        if photo and photo.filename:
            results = _frs_search(photo)
    return render_template('admin/search_by_photo.html', results=results)


def _frs_search(photo_file):
    """Compare an uploaded photo against Accused photos in this district."""
    try:
        import insightface, numpy as np, cv2
        import requests as req_lib
        app_model = insightface.app.FaceAnalysis(allowed_modules=['detection', 'recognition'])
        app_model.prepare(ctx_id=-1, det_size=(640, 640))
        img_bytes = photo_file.read()
        np_arr    = np.frombuffer(img_bytes, np.uint8)
        img       = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        faces     = app_model.get(img)
        if not faces:
            return []
        query_emb = faces[0].embedding

        conn = get_connection(); cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT DISTINCT a.id, a.name, a.fathers_name, a.photo_url
            FROM accused a
            JOIN accused_fir af ON af.accused_id = a.id
            JOIN fir_cases f ON f.id = af.fir_id
            WHERE a.photo_url IS NOT NULL AND f.district=%s
        """, (session.get('district'),))
        accused_rows = cursor.fetchall()
        cursor.close(); conn.close()

        matches = []
        for acc in accused_rows:
            try:
                resp = req_lib.get(acc['photo_url'], timeout=5)
                np2  = np.frombuffer(resp.content, np.uint8)
                img2 = cv2.imdecode(np2, cv2.IMREAD_COLOR)
                f2   = app_model.get(img2)
                if not f2:
                    continue
                sim = float(np.dot(query_emb, f2[0].embedding) /
                            (np.linalg.norm(query_emb) * np.linalg.norm(f2[0].embedding)))
                if sim > 0.4:
                    acc['similarity'] = round(sim * 100, 2)
                    matches.append(acc)
            except Exception:
                continue
        matches.sort(key=lambda x: x['similarity'], reverse=True)
        return matches[:10]
    except ImportError:
        flash('FRS के लिए insightface install करें।', 'warning')
        return []
    except Exception as e:
        logger.error(f"FRS error: {e}")
        flash('FRS खोज असफल।', 'danger')
        return []


# ══════════════════════════════════════════════════════════════════════════════
# ACCUSED & FIR ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@admin_bp.route('/accused')
@admin_required
def accused_list():
    """अभियुक्त सूची — name/father/thana/FIR filter"""
    return get_accused_list(role='admin')


@admin_bp.route('/accused/<int:accused_id>')
@admin_required
def accused_detail(accused_id):
    """अभियुक्त विवरण — सभी FIR और धाराएँ"""
    return get_accused_detail(accused_id)


@admin_bp.route('/accused/<int:accused_id>/upload-photo', methods=['POST'])
@admin_required
def upload_accused_photo(accused_id):
    """अभियुक्त का फ़ोटो अपलोड"""
    photo = request.files.get('photo')
    if not photo or not photo.filename:
        flash('फ़ोटो चुनें।', 'danger')
        return redirect(url_for('admin.accused_detail', accused_id=accused_id))
    url, pub_id = upload_image(photo, folder='accused_photos')
    if url:
        conn = get_connection(); cursor = conn.cursor()
        cursor.execute("UPDATE accused_photos SET is_current=0 WHERE accused_id=%s", (accused_id,))
        cursor.execute("""
            INSERT INTO accused_photos (accused_id,photo_url,photo_public_id,is_current,uploaded_by)
            VALUES (%s,%s,%s,1,%s)
        """, (accused_id, url, pub_id, session['user_id']))
        cursor.execute("UPDATE accused SET photo_url=%s,photo_public_id=%s,profile_status='complete' WHERE id=%s",
                       (url, pub_id, accused_id))
        conn.commit(); cursor.close(); conn.close()
        flash('फ़ोटो अपलोड सफल।', 'success')
    else:
        flash('फ़ोटो अपलोड असफल।', 'danger')
    return redirect(url_for('admin.accused_detail', accused_id=accused_id))


@admin_bp.route('/accused/<int:accused_id>/approve-bail', methods=['GET', 'POST'])
@admin_required
def approve_bail(accused_id):
    """
    जमानत स्वीकृत करें (Approve Bail) — यह "grant" नहीं है, admin केवल
    स्वीकृति दर्ज करता है, और केवल उन अभियुक्तों के लिए जो किसी FIR में
    गिरफ़्तार दर्शाए गए हैं, वह भी केवल उसी FIR के आधार पर।
    """
    return approve_accused_bail(accused_id, role='admin')


@admin_bp.route('/accused/<int:accused_id>/revoke-bail', methods=['POST'])
@admin_required
def revoke_bail_accused(accused_id):
    """जमानत रद्द करें — इतिहास सुरक्षित रहता है"""
    return revoke_accused_bail(accused_id, role='admin')


@admin_bp.route('/bailed-accused')
@admin_required
def bailed_accused():
    """सभी जमानत-स्वीकृत अभियुक्तों की सूची"""
    return get_bailed_accused_list(role='admin')


@admin_bp.route('/fir')
@admin_required
def fir_list():
    """FIR मामलों की सूची"""
    return get_fir_list(role='admin')


@admin_bp.route('/fir/<int:fir_id>')
@admin_required
def fir_detail(fir_id):
    """FIR विवरण — सभी अभियुक्त"""
    return get_fir_detail(fir_id)


@admin_bp.route('/fir/add', methods=['GET', 'POST'])
@admin_required
def add_fir():
    """मैन्युअल FIR दर्ज"""
    return create_fir_manual(role='admin')


@admin_bp.route('/upload-accused', methods=['GET', 'POST'])
@admin_required
def upload_accused():
    """Excel अपलोड — FIR + अभियुक्त (bulk). अज्ञात/खाली नाम वाले अभियुक्त नहीं बनाए जाते।"""
    return upload_accused_excel(role='admin')


@admin_bp.route('/download-accused-sample')
@admin_required
def download_accused_sample():
    return download_accused_sample_file()
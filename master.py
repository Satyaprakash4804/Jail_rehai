from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash
from functools import wraps
from db import get_connection
from utils import log_activity, paginate_query
import logging

master_bp = Blueprint('master', __name__)
logger = logging.getLogger(__name__)

def master_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session or session.get('role') != 'master':
            flash('Access denied.', 'danger')
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated

@master_bp.route('/dashboard')
@master_required
def dashboard():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT COUNT(*) as c FROM users WHERE role='super_admin'")
    super_count = cursor.fetchone()['c']
    cursor.execute("SELECT COUNT(*) as c FROM users WHERE role='admin'")
    admin_count = cursor.fetchone()['c']
    cursor.execute("SELECT COUNT(*) as c FROM users WHERE is_active=0")
    revoked_count = cursor.fetchone()['c']
    cursor.execute("SELECT COUNT(*) as c FROM activity_logs")
    log_count = cursor.fetchone()['c']
    cursor.close(); conn.close()
    stats = {'super_admins': super_count, 'admins': admin_count, 'revoked': revoked_count, 'logs': log_count}
    return render_template('master/dashboard.html', stats=stats)

@master_bp.route('/super-admins')
@master_required
def super_admins():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT u.*, 
               (SELECT COUNT(*) FROM users a WHERE a.created_by=u.id AND a.role='admin') as admin_count
        FROM users u WHERE u.role='super_admin' ORDER BY u.created_at DESC
    """)
    supers = cursor.fetchall()
    cursor.close(); conn.close()
    return render_template('master/super_admins.html', supers=supers)

@master_bp.route('/create-super-admin', methods=['GET', 'POST'])
@master_required
def create_super_admin():
    if request.method == 'POST':
        data = {
            'user_id': request.form.get('user_id', '').strip(),
            'name': request.form.get('name', '').strip(),
            'designation': request.form.get('designation', '').strip(),
            'contact': request.form.get('contact', '').strip(),
            'email': request.form.get('email', '').strip(),
            'district': request.form.get('district', '').strip(),
            'address': request.form.get('address', '').strip(),
            'password': request.form.get('password', '').strip(),
        }
        if not all([data['user_id'], data['name'], data['email'], data['password']]):
            flash('All required fields must be filled.', 'danger')
            return render_template('master/create_super_admin.html', form=data)
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id FROM users WHERE user_id=%s OR email=%s", (data['user_id'], data['email']))
        if cursor.fetchone():
            flash('User ID or Email already exists.', 'danger')
            cursor.close(); conn.close()
            return render_template('master/create_super_admin.html', form=data)
        cursor.execute("""
            INSERT INTO users (user_id,name,designation,contact,email,district,address,password_hash,role,created_by,is_active)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'super_admin',%s,1)
        """, (data['user_id'], data['name'], data['designation'], data['contact'],
              data['email'], data['district'], data['address'],
              generate_password_hash(data['password']), session['user_id']))
        conn.commit()
        log_activity(session['user_id'], 'master', f"Created super admin: {data['user_id']}", ip=request.remote_addr)
        cursor.close(); conn.close()
        flash(f"Super Admin '{data['name']}' created successfully.", 'success')
        return redirect(url_for('master.super_admins'))
    return render_template('master/create_super_admin.html', form={})

@master_bp.route('/edit-user/<int:uid>', methods=['GET', 'POST'])
@master_required
def edit_user(uid):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM users WHERE id=%s AND role!='master'", (uid,))
    user = cursor.fetchone()
    if not user:
        flash('User not found.', 'danger')
        cursor.close(); conn.close()
        return redirect(url_for('master.super_admins'))
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        designation = request.form.get('designation', '').strip()
        contact = request.form.get('contact', '').strip()
        email = request.form.get('email', '').strip()
        district = request.form.get('district', '').strip()
        address = request.form.get('address', '').strip()
        cursor.execute("""
            UPDATE users SET name=%s, designation=%s, contact=%s, email=%s, district=%s, address=%s WHERE id=%s
        """, (name, designation, contact, email, district, address, uid))
        conn.commit()
        log_activity(session['user_id'], 'master', f"Edited user ID: {uid}", ip=request.remote_addr)
        flash('User updated successfully.', 'success')
        cursor.close(); conn.close()
        return redirect(url_for('master.super_admins'))
    cursor.close(); conn.close()
    return render_template('master/edit_user.html', user=user)

@master_bp.route('/revoke-user/<int:uid>', methods=['POST'])
@master_required
def revoke_user(uid):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM users WHERE id=%s AND role!='master'", (uid,))
    user = cursor.fetchone()
    if not user:
        flash('User not found.', 'danger')
        cursor.close(); conn.close()
        return redirect(request.referrer or url_for('master.super_admins'))
    new_status = 0 if user['is_active'] else 1
    cursor.execute("UPDATE users SET is_active=%s WHERE id=%s", (new_status, uid))
    conn.commit()
    action = 'Revoked' if new_status == 0 else 'Restored'
    log_activity(session['user_id'], 'master', f"{action} user: {user['user_id']}", ip=request.remote_addr)
    flash(f"User access {action.lower()} successfully.", 'success')
    cursor.close(); conn.close()
    return redirect(request.referrer or url_for('master.super_admins'))

@master_bp.route('/all-admins')
@master_required
def all_admins():
    search = request.args.get('search', '')
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    base_q = """
        SELECT a.*, s.name as super_name, s.user_id as super_uid, s.district as super_district
        FROM users a
        LEFT JOIN users s ON a.created_by = s.id
        WHERE a.role='admin'
    """
    params = []
    if search:
        base_q += " AND (a.name LIKE %s OR a.user_id LIKE %s OR a.district LIKE %s)"
        params += [f'%{search}%', f'%{search}%', f'%{search}%']
    base_q += " ORDER BY a.created_at DESC"
    rows, total, total_pages = paginate_query(cursor, base_q, params, page, per_page)
    cursor.close(); conn.close()
    return render_template('master/all_admins.html', admins=rows, page=page,
                           total=total, total_pages=total_pages, search=search, per_page=per_page)

@master_bp.route('/logs')
@master_required
def logs():
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 50))
    role_filter = request.args.get('role', '')
    search = request.args.get('search', '')
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    base_q = """
        SELECT l.*, u.name as user_name, u.user_id as uid
        FROM activity_logs l
        LEFT JOIN users u ON l.user_id = u.id
        WHERE 1=1
    """
    params = []
    if role_filter:
        base_q += " AND l.user_role=%s"
        params.append(role_filter)
    if search:
        base_q += " AND (l.action LIKE %s OR l.endpoint LIKE %s OR u.name LIKE %s)"
        params += [f'%{search}%', f'%{search}%', f'%{search}%']
    base_q += " ORDER BY l.created_at DESC"
    rows, total, total_pages = paginate_query(cursor, base_q, params, page, per_page)
    cursor.close(); conn.close()
    return render_template('master/logs.html', logs=rows, page=page,
                           total=total, total_pages=total_pages,
                           role_filter=role_filter, search=search, per_page=per_page)
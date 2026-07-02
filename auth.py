"""
auth.py  (UPDATED)
==================
Changes from original:
  • /logout now calls fcm_service.delete_user_tokens() before
    clearing the session, so push tokens are cleaned up on sign-out.
  • All other routes are identical to the original.
"""

from flask import (Blueprint, render_template, request, redirect,
                   url_for, session, flash, current_app)
from werkzeug.security import check_password_hash, generate_password_hash
from db import get_connection
from utils import generate_otp, log_activity
from datetime import datetime, timedelta
import logging

auth_bp = Blueprint('auth', __name__)
logger  = logging.getLogger(__name__)


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return _redirect_by_role(session.get('role'))

    if request.method == 'POST':
        user_id  = request.form.get('user_id', '').strip()
        password = request.form.get('password', '').strip()

        conn   = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM users WHERE user_id=%s", (user_id,))
        user = cursor.fetchone()
        cursor.close()
        conn.close()

        if not user:
            flash('Invalid User ID or password.', 'danger')
            return render_template('auth/login.html')
        if not user['is_active']:
            flash('Your account has been revoked. Contact administrator.', 'danger')
            return render_template('auth/login.html')
        if not check_password_hash(user['password_hash'], password):
            flash('Invalid User ID or password.', 'danger')
            return render_template('auth/login.html')

        session['user_id']     = user['id']
        session['user_uid']    = user['user_id']
        session['name']        = user['name']
        session['role']        = user['role']
        session['email']       = user['email']
        session['district']    = user['district']
        session['designation'] = user['designation']

        log_activity(user['id'], user['role'], 'User logged in',
                     ip=request.remote_addr)
        flash(f"Welcome, {user['name']}!", 'success')
        return _redirect_by_role(user['role'])

    return render_template('auth/login.html')


@auth_bp.route('/logout')
def logout():
    user_id = session.get('user_id')
    role    = session.get('role')

    if user_id:
        log_activity(user_id, role, 'User logged out', ip=request.remote_addr)

        # ── FCM: remove all tokens for this user on logout ────────────────────
        try:
            from fcm_service import delete_user_tokens
            delete_user_tokens(user_id)
        except Exception as e:
            logger.warning(f"[FCM] Could not delete tokens on logout: {e}")

    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('auth.login'))


@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email  = request.form.get('email', '').strip()
        conn   = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM users WHERE email=%s", (email,))
        user = cursor.fetchone()
        if not user:
            flash('No account found with this email.', 'danger')
            cursor.close()
            conn.close()
            return render_template('auth/forgot_password.html')

        otp    = generate_otp()
        expiry = datetime.now() + timedelta(minutes=10)
        cursor.execute(
            "UPDATE users SET otp_code=%s, otp_expiry=%s WHERE id=%s",
            (otp, expiry, user['id'])
        )
        conn.commit()
        cursor.close()
        conn.close()

        try:
            from flask_mail import Message
            mail = current_app.extensions['mail']
            msg  = Message(
                'Password Reset OTP - Jail Rehai',
                recipients=[email],
                sender='noreply@jailrehai.gov.in'
            )
            msg.body = (
                f"Dear {user['name']},\n\n"
                f"Your OTP for password reset is: {otp}\n\n"
                f"This OTP is valid for 10 minutes.\n\n"
                f"Do not share this OTP with anyone.\n\n"
                f"Regards,\nJail Rehai System"
            )
            mail.send(msg)
            flash('OTP sent to your email.', 'success')
        except Exception as e:
            logger.error(f"Mail error: {e}")
            flash(f'OTP generated (mail not configured): {otp}', 'warning')

        return redirect(url_for('auth.verify_otp', email=email))

    return render_template('auth/forgot_password.html')


@auth_bp.route('/verify-otp', methods=['GET', 'POST'])
def verify_otp():
    email = request.args.get('email') or request.form.get('email')

    if request.method == 'POST':
        otp          = request.form.get('otp', '').strip()
        new_pass     = request.form.get('new_password', '').strip()
        confirm_pass = request.form.get('confirm_password', '').strip()

        if new_pass != confirm_pass:
            flash('Passwords do not match.', 'danger')
            return render_template('auth/verify_otp.html', email=email)

        conn   = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM users WHERE email=%s", (email,))
        user = cursor.fetchone()

        if (not user or user['otp_code'] != otp
                or datetime.now() > user['otp_expiry']):
            flash('Invalid or expired OTP.', 'danger')
            cursor.close()
            conn.close()
            return render_template('auth/verify_otp.html', email=email)

        cursor.execute(
            "UPDATE users SET password_hash=%s, otp_code=NULL, "
            "otp_expiry=NULL WHERE id=%s",
            (generate_password_hash(new_pass), user['id'])
        )
        conn.commit()
        cursor.close()
        conn.close()
        flash('Password reset successfully. Please login.', 'success')
        return redirect(url_for('auth.login'))

    return render_template('auth/verify_otp.html', email=email)


@auth_bp.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    """Reset password from dashboard (logged-in user)."""
    if 'user_id' not in session:
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        current  = request.form.get('current_password', '').strip()
        new_pass = request.form.get('new_password', '').strip()
        confirm  = request.form.get('confirm_password', '').strip()

        if new_pass != confirm:
            flash('Passwords do not match.', 'danger')
            return render_template('auth/reset_password.html')

        conn   = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM users WHERE id=%s", (session['user_id'],))
        user = cursor.fetchone()

        if not check_password_hash(user['password_hash'], current):
            flash('Current password is incorrect.', 'danger')
            cursor.close()
            conn.close()
            return render_template('auth/reset_password.html')

        cursor.execute(
            "UPDATE users SET password_hash=%s WHERE id=%s",
            (generate_password_hash(new_pass), user['id'])
        )
        conn.commit()
        cursor.close()
        conn.close()
        flash('Password updated successfully.', 'success')
        return _redirect_by_role(session.get('role'))

    return render_template('auth/reset_password.html')


def _redirect_by_role(role):
    if role == 'master':      return redirect(url_for('master.dashboard'))
    if role == 'super_admin': return redirect(url_for('super.dashboard'))
    if role == 'admin':       return redirect(url_for('admin.dashboard'))
    return redirect(url_for('auth.login'))



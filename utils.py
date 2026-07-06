"""
utils.py
========
Shared helper functions for the Accused-based (अभियुक्त-आधारित) system.
There is no criminal-management module in this system — all record
helpers below operate on Accused / FIR / bail-approval data only.

Notes:
  • push_data includes a 'click_url' key (read by firebase-messaging-sw.js
    notificationclick handler) so the OS notification opens the right
    in-app page instead of '/'.
  • click_url is role-aware: admins → /admin/notifications,
    super_admin → /super/notifications.
"""

import cloudinary
import cloudinary.uploader
import cloudinary.api
from config import CLOUDINARY_CONFIG, MAIL_CONFIG
from db import get_connection
import logging
import random
import string
from datetime import datetime, timedelta
from flask import request, session
from flask_mail import Message

logger = logging.getLogger(__name__)


def init_cloudinary():
    cloudinary.config(
        cloud_name=CLOUDINARY_CONFIG["cloud_name"],
        api_key=CLOUDINARY_CONFIG["api_key"],
        api_secret=CLOUDINARY_CONFIG["api_secret"],
        secure=True
    )


def upload_image(file, folder="accused_photos"):
    try:
        result = cloudinary.uploader.upload(file, folder=folder, resource_type="image")
        return result.get("secure_url"), result.get("public_id")
    except Exception as e:
        logger.error(f"Cloudinary image upload error: {e}")
        return None, None


def delete_image(public_id):
    try:
        cloudinary.uploader.destroy(public_id)
    except Exception as e:
        logger.error(f"Cloudinary delete error: {e}")


def upload_document(file, folder="bail_docs"):
    try:
        result = cloudinary.uploader.upload(file, folder=folder, resource_type="auto")
        return result.get("secure_url"), result.get("public_id"), result.get("resource_type", "raw")
    except Exception as e:
        logger.error(f"Cloudinary document upload error: {e}")
        return None, None, None


def upload_id_card_file(file, folder="accused_ids"):
    try:
        result = cloudinary.uploader.upload(file, folder=folder, resource_type="auto")
        return result.get("secure_url"), result.get("public_id"), result.get("resource_type", "image")
    except Exception as e:
        logger.error(f"Cloudinary ID card upload error: {e}")
        return None, None, None


def log_activity(user_id, user_role, action, endpoint=None, method=None,
                 ip=None, status_code=None, details=None):
    try:
        conn   = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO activity_logs
                (user_id, user_role, action, endpoint, method,
                 ip_address, status_code, details)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """, (user_id, user_role, action, endpoint, method, ip, status_code, details))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        logger.error(f"Log activity error: {e}")


def generate_otp(length=6):
    return ''.join(random.choices(string.digits, k=length))


def get_accused_bail_alerts(district=None):
    """Get accused whose active temporary bail ends within 2 days."""
    try:
        conn   = get_connection()
        cursor = conn.cursor(dictionary=True)
        today      = datetime.today().date()
        alert_date = today + timedelta(days=2)
        if district:
            cursor.execute("""
                SELECT DISTINCT a.id, a.name, a.fathers_name, a.bail_end_date
                FROM accused a
                JOIN accused_fir af ON af.accused_id = a.id
                JOIN fir_cases f ON f.id = af.fir_id
                WHERE a.bail_status='temporary'
                AND a.bail_end_date IS NOT NULL
                AND a.bail_end_date BETWEEN %s AND %s
                AND f.district = %s
            """, (today, alert_date, district))
        else:
            cursor.execute("""
                SELECT id, name, fathers_name, bail_end_date FROM accused
                WHERE bail_status='temporary'
                AND bail_end_date IS NOT NULL
                AND bail_end_date BETWEEN %s AND %s
            """, (today, alert_date))
        alerts = cursor.fetchall()
        cursor.close()
        conn.close()
        return alerts
    except Exception as e:
        logger.error(f"Accused bail alerts error: {e}")
        return []


def auto_complete_expired_accused_bails(district=None):
    """
    Mark accused_bail_history rows COMPLETED when bail_end_date has passed.
    Clear the CURRENT bail fields on the accused table (history stays intact
    forever — it is never deleted, only marked COMPLETED).
    Called on dashboard load, same pattern as auto_complete_expired_bails().
    """
    try:
        today  = datetime.today().date()
        conn   = get_connection()
        cursor = conn.cursor(dictionary=True)
        q = """
            SELECT abh.id, abh.accused_id
            FROM accused_bail_history abh
            WHERE abh.status = 'ACTIVE'
            AND abh.bail_type = 'temporary'
            AND abh.bail_end_date IS NOT NULL
            AND abh.bail_end_date < %s
        """
        params = [today]
        if district:
            q += (" AND EXISTS "
                  "(SELECT 1 FROM accused_fir af JOIN fir_cases f ON f.id=af.fir_id "
                  "WHERE af.accused_id=abh.accused_id AND f.district=%s)")
            params.append(district)
        cursor.execute(q, params)
        expired = cursor.fetchall()
        for row in expired:
            cursor.execute("""
                UPDATE accused_bail_history
                SET status='COMPLETED', completed_at=NOW()
                WHERE id=%s
            """, (row['id'],))
            cursor.execute("""
                UPDATE accused
                SET bail_status='none', bail_start_date=NULL,
                    bail_end_date=NULL, bail_documents_url=NULL,
                    bail_documents_public_id=NULL,
                    bail_photo_url=NULL, bail_photo_public_id=NULL,
                    bail_photo_lat=NULL, bail_photo_lng=NULL, bail_photo_captured_at=NULL,
                    bail_remark=NULL, bail_rating=0
                WHERE id=%s AND bail_status='temporary'
            """, (row['accused_id'],))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        logger.error(f"auto_complete_expired_accused_bails error: {e}")


def paginate_query(cursor, base_query, params, page, per_page=20):
    count_query = f"SELECT COUNT(*) as total FROM ({base_query}) as sub"
    cursor.execute(count_query, params)
    total  = cursor.fetchone()['total']
    offset = (page - 1) * per_page
    cursor.execute(f"{base_query} LIMIT %s OFFSET %s", params + [per_page, offset])
    rows        = cursor.fetchall()
    total_pages = (total + per_page - 1) // per_page if total else 1
    return rows, total, total_pages


# ── Notification core ─────────────────────────────────────────────────────────

def send_bail_notification(district, accused_name, fir_label,
                           bail_type, bail_start, bail_end, bail_remark,
                           bail_rating, approved_by_name, approved_by_id,
                           mail_instance=None, thana=None, notify_thana=True):
    """
    Notify everyone who needs to know a bail was APPROVED (स्वीकृत) for an
    Accused (अभियुक्त) — never "granted" by an admin, since only a court
    grants bail; the system only records the district's approval action.

    Recipients: every active admin AND every active super_admin belonging
    to the district the FIR/arrest was recorded in (so both the district's
    admins and their super admins are notified in one call), PLUS — if
    `thana` (the accused's own थाना, from the FIR) is given and a matching
    row exists in thana_master for this district — that थाना's own
    WhatsApp/email contact, looked up from the super admin's uploaded थाना
    list.

    Channels: in-app notification (MySQL) + FCM push + optional email.
    The 'click_url' in push_data is role-aware so clicking the OS
    notification opens the correct Notifications page for each user.
    """
    try:
        conn   = get_connection()
        cursor = conn.cursor(dictionary=True)
        # Notify every admin AND super_admin of this district
        cursor.execute("""
            SELECT id, name, email, role FROM users
            WHERE (
                (district=%s AND role IN ('admin','super_admin'))
                OR
                (district='All' AND role IN ('master','super_admin'))
            )
            AND is_active=1
        """, (district,))
        recipients = cursor.fetchall()

        bail_end_display = str(bail_end) if bail_end else 'Permanent'
        rating_text = (
            ('★' * bail_rating + '☆' * (5 - bail_rating)) if bail_rating else '—'
        )
        message = (
            f"Bail approved for accused <strong>{accused_name}</strong> "
            f"({fir_label}) | Type: {bail_type.title()} | "
            f"Start: {bail_start} | End: {bail_end_display} | "
            f"Risk: {rating_text} | By: {approved_by_name}"
        )
        title = f"Bail Approved — {accused_name}"

        for user in recipients:
            if user['id'] == approved_by_id:
                continue
            cursor.execute("""
                INSERT INTO notifications
                    (user_id, district, type, title, message, is_read)
                VALUES (%s, %s, 'bail_approved', %s, %s, 0)
            """, (user['id'], district, title, message))

        conn.commit()
        cursor.close()
        conn.close()

        # ── FCM Push ──────────────────────────────────────────────────────────
        push_body = (
            f"{accused_name} ({fir_label}) | "
            f"Type: {bail_type.title()} | End: {bail_end_display} | "
            f"Risk: {bail_rating}/5 | By: {approved_by_name}"
        )

        # click_url is used by:
        #   • firebase-messaging-sw.js (background/terminated) → notificationclick
        #   • firebase-init.js (foreground) → toast click
        # Each user's role determines which page opens.
        # We send one multicast but include both URLs in data; the client-side
        # JS reads its own role from session to pick the right one.
        # For simplicity we default to /notifications and let the client redirect.
        push_data = {
            "type":         "bail_approved",
            "district":     str(district),
            "accused_name": str(accused_name),
            "fir":          str(fir_label),
            "click_url":    "/notifications",
            # Flutter: named route
            "route":        "/notifications",
            "click_action": "FLUTTER_NOTIFICATION_CLICK",
        }

        try:
            from fcm_service import push_to_district
            # Note: the approver's own devices receive the push too (confirms
            # the action). The in-app MySQL notification still skips the
            # approver (no duplicate row in their own bell dropdown).
            result = push_to_district(
                district=district,
                title=title,
                body=push_body,
                data=push_data,
            )
            logger.info(
                f"[FCM] Bail-approval push for {accused_name}: "
                f"✓{result.get('success_count', 0)} "
                f"✗{result.get('failure_count', 0)}"
            )
        except Exception as fcm_err:
            logger.error(f"[FCM] Bail-approval push error: {fcm_err}")

        # ── WhatsApp (Meta Cloud API) ────────────────────────────────────────
        try:
            from whatsapp_service import send_bail_whatsapp_notification
            wa_result = send_bail_whatsapp_notification(
                district=district,
                bails=[{
                    "accused_name": accused_name,
                    "fir_label": fir_label,
                    "bail_type": bail_type,
                    "bail_start": bail_start,
                    "bail_end": bail_end_display,
                    "bail_remark": bail_remark,
                    "bail_rating": bail_rating,
                }],
                approved_by_name=approved_by_name,
                approved_by_id=approved_by_id,
            )
            logger.info(
                f"[WhatsApp] Bail-approval notify for {accused_name}: "
                f"✓{wa_result.get('sent', 0)} ✗{wa_result.get('failed', 0)}"
            )
        except Exception as wa_err:
            logger.error(f"[WhatsApp] Bail-approval notify error: {wa_err}")

        # ── Optional Email ───────────────────────────────────────────────────
        if mail_instance:
            emails = [
                u['email'] for u in recipients
                if u['id'] != approved_by_id and u.get('email')
            ]
            if emails:
                try:
                    subj = f"[Jail Rehai] Bail Approved — {accused_name} ({district})"
                    body_txt = (
                        f"Dear Officer,\n\n"
                        f"Bail has been approved for an accused in {district}.\n\n"
                        f"Accused  : {accused_name}\n"
                        f"FIR      : {fir_label}\n"
                        f"Type     : {bail_type.title()}\n"
                        f"Start    : {bail_start}\n"
                        f"End      : {bail_end_display}\n"
                        f"Risk     : {rating_text}\n"
                        f"Remark   : {bail_remark or '—'}\n"
                        f"Approved : {approved_by_name} (ID: {approved_by_id})\n\n"
                        f"Log in to Jail Rehai for full details.\n"
                        f"— Jail Rehai System"
                    )
                    msg = Message(subject=subj, recipients=emails, body=body_txt)
                    mail_instance.send(msg)
                except Exception as mail_err:
                    logger.error(f"Bail-approval email error: {mail_err}")

        # ── थाना notification (WhatsApp + email) ─────────────────────────────
        # Looks up the accused's own थाना (from the FIR) in the super admin's
        # uploaded thana_master list for this district, and alerts that थाना
        # directly — separate from, and in addition to, the district admin
        # alert above. Silently no-ops if the super admin hasn't uploaded a
        # matching थाना entry yet.
        if thana and notify_thana:
            try:
                from thana_service import get_thana_contact
                thana_row = get_thana_contact(district, thana)
            except Exception as thana_lookup_err:
                thana_row = None
                logger.error(f"[Thana] lookup error: {thana_lookup_err}")

            if thana_row:
                if thana_row.get('contact'):
                    try:
                        from whatsapp_service import send_bail_whatsapp_to_recipients
                        thana_wa_result = send_bail_whatsapp_to_recipients(
                            recipients=[{'name': thana_row['thana_name'], 'contact': thana_row['contact']}],
                            district=district,
                            bails=[{
                                "accused_name": accused_name,
                                "fir_label": fir_label,
                                "bail_type": bail_type,
                                "bail_start": bail_start,
                                "bail_end": bail_end_display,
                                "bail_remark": bail_remark,
                                "bail_rating": bail_rating,
                            }],
                            approved_by_name=approved_by_name,
                        )
                        logger.info(
                            f"[WhatsApp] थाना '{thana_row['thana_name']}' notify for "
                            f"{accused_name}: ✓{thana_wa_result.get('sent', 0)} "
                            f"✗{thana_wa_result.get('failed', 0)}"
                        )
                    except Exception as thana_wa_err:
                        logger.error(f"[WhatsApp] थाना notify error: {thana_wa_err}")

                if mail_instance and thana_row.get('email'):
                    try:
                        subj = f"[Jail Rehai] Bail Approved — {accused_name} ({district})"
                        body_txt = (
                            f"थाना {thana_row['thana_name']},\n\n"
                            f"आपके क्षेत्र के एक अभियुक्त की जमानत स्वीकृत हुई है।\n\n"
                            f"Accused  : {accused_name}\n"
                            f"FIR      : {fir_label}\n"
                            f"Type     : {bail_type.title()}\n"
                            f"Start    : {bail_start}\n"
                            f"End      : {bail_end_display}\n"
                            f"Risk     : {rating_text}\n"
                            f"Remark   : {bail_remark or '—'}\n"
                            f"Approved : {approved_by_name}\n\n"
                            f"— Jail Rehai System"
                        )
                        msg = Message(subject=subj, recipients=[thana_row['email']], body=body_txt)
                        mail_instance.send(msg)
                    except Exception as thana_mail_err:
                        logger.error(f"[Thana] email error: {thana_mail_err}")

    except Exception as e:
        logger.error(f"send_bail_notification error: {e}")


def get_notifications(user_id, limit=20):
    try:
        conn   = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT * FROM notifications WHERE user_id=%s
            ORDER BY created_at DESC LIMIT %s
        """, (user_id, limit))
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"get_notifications error: {e}")
        return []


def mark_notifications_read(user_id):
    try:
        conn   = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE notifications SET is_read=1 WHERE user_id=%s", (user_id,)
        )
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        logger.error(f"mark_notifications_read error: {e}")
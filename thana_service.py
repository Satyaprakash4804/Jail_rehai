# -*- coding: utf-8 -*-
"""
thana_service.py
=================
Super Admin managed थाना (police-station) directory, one list per district.

Each row: thana_name + contact (WhatsApp/SMS number) + email. Super admin
uploads this once (Excel) for their district, or adds/edits rows manually.

This directory is what bail-approval notifications (WhatsApp + email) are
routed through: when a bail is approved for an accused, the accused's FIR
thana is looked up here (by district + normalized thana name) and the
message/email is sent to *that* thana's contact/email — not to a generic
admin list.

normalize_thana() is imported from bail_bulk.py so the exact same fuzzy
"थाना देहात" == "देहात" == "को0देहात" matching used for Excel bail imports
is used here too — one source of truth for थाना name normalization.
"""

import logging
from db import get_connection
from bail_bulk import normalize_thana

logger = logging.getLogger(__name__)


# ── CRUD ──────────────────────────────────────────────────────────────────────

def list_thanas(district, search=''):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    if search:
        cursor.execute("""
            SELECT * FROM thana_master
            WHERE district=%s AND (thana_name LIKE %s OR contact LIKE %s OR email LIKE %s)
            ORDER BY thana_name
        """, (district, f'%{search}%', f'%{search}%', f'%{search}%'))
    else:
        cursor.execute(
            "SELECT * FROM thana_master WHERE district=%s ORDER BY thana_name", (district,)
        )
    rows = cursor.fetchall()
    cursor.close(); conn.close()
    return rows


def add_thana(district, thana_name, contact, email, created_by):
    thana_name = (thana_name or '').strip()
    contact    = (contact or '').strip()
    email      = (email or '').strip()
    if not thana_name:
        return False, 'थाना नाम आवश्यक है।'
    if not contact and not email:
        return False, 'संपर्क नंबर या ईमेल में से कम से कम एक आवश्यक है।'
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            INSERT INTO thana_master (district, thana_name, contact, email, created_by, is_active)
            VALUES (%s,%s,%s,%s,%s,1)
            ON DUPLICATE KEY UPDATE
                contact=VALUES(contact), email=VALUES(email), is_active=1
        """, (district, thana_name, contact or None, email or None, created_by))
        conn.commit()
        return True, None
    except Exception as e:
        logger.error(f"add_thana error: {e}")
        return False, str(e)
    finally:
        cursor.close(); conn.close()


def update_thana(thana_id, district, thana_name, contact, email):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id FROM thana_master WHERE id=%s AND district=%s", (thana_id, district))
    if not cursor.fetchone():
        cursor.close(); conn.close()
        return False, 'थाना नहीं मिला।'
    cursor.execute("""
        UPDATE thana_master SET thana_name=%s, contact=%s, email=%s WHERE id=%s AND district=%s
    """, ((thana_name or '').strip(), (contact or '').strip() or None,
          (email or '').strip() or None, thana_id, district))
    conn.commit()
    cursor.close(); conn.close()
    return True, None


def toggle_thana(thana_id, district):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT is_active FROM thana_master WHERE id=%s AND district=%s", (thana_id, district))
    row = cursor.fetchone()
    if not row:
        cursor.close(); conn.close()
        return False
    new_status = 0 if row['is_active'] else 1
    cursor.execute("UPDATE thana_master SET is_active=%s WHERE id=%s", (new_status, thana_id))
    conn.commit()
    cursor.close(); conn.close()
    return True


def delete_thana(thana_id, district):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM thana_master WHERE id=%s AND district=%s", (thana_id, district))
    conn.commit()
    deleted = cursor.rowcount > 0
    cursor.close(); conn.close()
    return deleted


# ── Excel bulk upload ─────────────────────────────────────────────────────────

def bulk_upload_thanas_from_excel(district, file, created_by):
    """
    Expected columns (row 1 = header, skipped): thana_name, contact, email
    Same relaxed style as the accused-Excel importer — extra/missing trailing
    columns are tolerated.
    """
    try:
        import openpyxl
        wb = openpyxl.load_workbook(file, data_only=True, read_only=True)
        ws = wb.active
    except Exception as e:
        return {'success': 0, 'failed': [f'Excel फ़ाइल पढ़ने में त्रुटि: {e}']}

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    success, failed = 0, []

    for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if not row or not any(row):
            continue
        thana_name, contact, email = (list(row) + [None] * 3)[:3]
        thana_name = str(thana_name or '').strip()
        contact    = str(contact or '').strip()
        email      = str(email or '').strip()

        if not thana_name:
            failed.append(f'पंक्ति {row_idx}: थाना नाम खाली')
            continue
        if not contact and not email:
            failed.append(f'पंक्ति {row_idx} ({thana_name}): संपर्क या ईमेल में से एक आवश्यक')
            continue
        try:
            cursor.execute("""
                INSERT INTO thana_master (district, thana_name, contact, email, created_by, is_active)
                VALUES (%s,%s,%s,%s,%s,1)
                ON DUPLICATE KEY UPDATE
                    contact=VALUES(contact), email=VALUES(email), is_active=1
            """, (district, thana_name, contact or None, email or None, created_by))
            success += 1
        except Exception as e:
            failed.append(f'पंक्ति {row_idx} ({thana_name}): {str(e)[:80]}')

    conn.commit()
    cursor.close(); conn.close()
    return {'success': success, 'failed': failed}


# ── Lookup used by notification routing (utils.py / bail_bulk.py) ────────────

def get_thana_contact(district, thana_name):
    """
    Resolve a single active थाना record for `district` whose thana_name
    matches `thana_name` under the same loose normalization used for the
    Excel bail-import fuzzy matcher. Returns a dict {id,thana_name,contact,
    email} or None if nothing on file / not configured yet by the super admin.
    """
    if not district or not thana_name:
        return None
    target = normalize_thana(thana_name)
    if not target:
        return None
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT id, thana_name, contact, email FROM thana_master
        WHERE district=%s AND is_active=1
    """, (district,))
    rows = cursor.fetchall()
    cursor.close(); conn.close()
    for r in rows:
        if normalize_thana(r['thana_name']) == target:
            return r
    return None

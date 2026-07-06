"""
whatsapp_service.py
====================
Meta WhatsApp Business Cloud API integration for bail-approval alerts.

Structured to match the existing whatsapp.js Node service used elsewhere in
your stack (election duty management):
  • One generic send_whatsapp_template() — same shape as sendWhatsAppTemplate()
  • Same phone formatting rule: strip leading '+', else prepend '91'
  • Same env vars: WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_API_TOKEN
  • Named per-event wrapper functions (notify_bail_approved,
    notify_bail_bulk_approved) instead of a generic free-text sender —
    mirrors notifyDutyAssigned / notifySwapRequested / etc.
  • Same result shape: {"success": True/False, "data"/"error": ...}

Sending number: 9818255326 (see config.WHATSAPP_CONFIG for template setup).

Recipient lookup (district admins/super_admins) is Python-specific since it
reads from the `users` MySQL table — no equivalent in whatsapp.js.
"""

import logging
import requests

from db import get_connection
from config import WHATSAPP_CONFIG

logger = logging.getLogger(__name__)

WHATSAPP_URL = (
    f"https://graph.facebook.com/{WHATSAPP_CONFIG['API_VERSION']}/"
    f"{WHATSAPP_CONFIG['PHONE_NUMBER_ID']}/messages"
)


def is_whatsapp_ready():
    """True once PHONE_NUMBER_ID + API_TOKEN are configured."""
    return bool(WHATSAPP_CONFIG.get("PHONE_NUMBER_ID") and WHATSAPP_CONFIG.get("API_TOKEN"))


# ── Generic template sender (== sendWhatsAppTemplate in whatsapp.js) ─────────

def send_whatsapp_template(phone, template_name, components=None, language_code=None):
    """
    Send an approved Message Template. `components` follows the exact Meta
    Cloud API shape, e.g.:
        [{"type": "body", "parameters": [{"type": "text", "text": "..."}]}]
    """
    if not is_whatsapp_ready():
        logger.warning("[WhatsApp] Not configured — skipping send.")
        return {"success": False, "error": "not_configured"}

    formatted_phone = phone[1:] if phone.startswith('+') else f"91{phone}"
    language_code = language_code or WHATSAPP_CONFIG.get("TEMPLATE_LANG", "en")

    payload = {
        "messaging_product": "whatsapp",
        "to": formatted_phone,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
            "components": components or [],
        },
    }
    headers = {
        "Authorization": f"Bearer {WHATSAPP_CONFIG['API_TOKEN']}",
        "Content-Type": "application/json",
    }
    try:
        response = requests.post(WHATSAPP_URL, json=payload, headers=headers, timeout=15)
        response.raise_for_status()
        return {"success": True, "data": response.json()}
    except requests.exceptions.HTTPError:
        err_msg = None
        try:
            err_msg = response.json().get("error", {}).get("message")
        except Exception:
            pass
        err_msg = err_msg or str(response.text)
        logger.error(f"WhatsApp send error [{template_name}]: {err_msg}")
        return {"success": False, "error": err_msg}
    except Exception as error:
        logger.error(f"WhatsApp send error [{template_name}]: {error}")
        return {"success": False, "error": str(error)}


# ── Bail templates ────────────────────────────────────────────────────────────

def notify_bail_approved(phone, accused_name, fir_label, bail_type, start_date, end_date, approved_by):
    """
    bail_approved: {{1}} accused name, {{2}} case/FIR details, {{3}} bail type,
                   {{4}} start date, {{5}} end date, {{6}} approved by
    """
    return send_whatsapp_template(phone, WHATSAPP_CONFIG["TEMPLATE_BAIL_SINGLE"], [{
        "type": "body",
        "parameters": [
            {"type": "text", "text": accused_name},
            {"type": "text", "text": fir_label},
            {"type": "text", "text": (bail_type or "temporary").title()},
            {"type": "text", "text": str(start_date or "—")},
            {"type": "text", "text": str(end_date or "Permanent")},
            {"type": "text", "text": approved_by},
        ],
    }])


def notify_bail_bulk_approved(phone, district, total_count, approved_by, details_list):
    """
    bail_bulk_approved: {{1}} district, {{2}} total count approved,
                         {{3}} approved by, {{4}} full details list (one block)
    `details_list` is the pre-formatted numbered list of every accused
    approved in the batch (built by format_bulk_bail_details below).
    """
    return send_whatsapp_template(phone, WHATSAPP_CONFIG["TEMPLATE_BAIL_BULK"], [{
        "type": "body",
        "parameters": [
            {"type": "text", "text": district},
            {"type": "text", "text": str(total_count)},
            {"type": "text", "text": approved_by},
            {"type": "text", "text": details_list},
        ],
    }])


def format_bulk_bail_details(bails: list) -> str:
    """Numbered one-line-per-accused summary for the {{4}} bulk template param."""
    lines = []
    for i, b in enumerate(bails, start=1):
        end_display = b.get('bail_end') or 'Permanent'
        lines.append(
            f"{i}. {b.get('accused_name', '—')} — {b.get('fir_label', '—')} "
            f"({(b.get('bail_type') or 'temporary').title()}, till {end_display})"
        )
    return "\n".join(lines)


# ── Recipient lookup (district admins + super_admins) ─────────────────────────

def get_whatsapp_recipients_for_district(district, exclude_user_id=None):
    """
    Return [{'id', 'name', 'contact', 'role'}] for every active admin/
    super_admin in `district`, plus master/super_admin users with
    district='All', that have a non-empty `contact` number on file.
    """
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT id, name, contact, role
            FROM users
            WHERE (
                (district = %s AND role IN ('admin', 'super_admin'))
                OR
                (district = 'All' AND role IN ('master', 'super_admin'))
            )
            AND is_active = 1
            AND contact IS NOT NULL AND contact != ''
            AND (%s IS NULL OR id != %s)
        """, (district, exclude_user_id, exclude_user_id))
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"[WhatsApp] get_whatsapp_recipients_for_district error: {e}")
        return []


# ── थाना-routed sender (uses the SAME templates, an explicit recipient list) ──

def send_bail_whatsapp_to_recipients(recipients, district, bails, approved_by_name):
    """
    Same message logic/templates as send_bail_whatsapp_notification below
    (single -> bail_approved, multiple -> bail_bulk_approved), but sent to an
    explicit `recipients` list (e.g. the accused's own थाना contact from
    thana_master) instead of doing the district admin/super_admin DB lookup.
    Used to notify the accused's thana on bail approval, both single and
    bulk, separately from — and in addition to — the district admin alert.

    recipients: list of dicts with at least a 'contact' phone number, e.g.
      [{'name': 'थाना कोतवाली', 'contact': '98XXXXXXXX'}]
    """
    if not bails or not recipients:
        return {"sent": 0, "failed": 0}
    if not is_whatsapp_ready():
        logger.info("[WhatsApp] Skipping थाना notification — WhatsApp not configured.")
        return {"sent": 0, "failed": 0, "skipped": "not_configured"}

    sent, failed = 0, 0
    if len(bails) == 1:
        b = bails[0]
        for r in recipients:
            if not r.get('contact'):
                continue
            result = notify_bail_approved(
                r['contact'], b.get('accused_name'), b.get('fir_label'),
                b.get('bail_type'), b.get('bail_start'), b.get('bail_end'),
                approved_by_name,
            )
            if result.get("success"):
                sent += 1
            else:
                failed += 1
                logger.error(
                    f"[WhatsApp] थाना notify failed ({r.get('name')}, {r.get('contact')}): "
                    f"{result.get('error')}"
                )
    else:
        details_list = format_bulk_bail_details(bails)
        for r in recipients:
            if not r.get('contact'):
                continue
            result = notify_bail_bulk_approved(
                r['contact'], district, len(bails), approved_by_name, details_list,
            )
            if result.get("success"):
                sent += 1
            else:
                failed += 1
                logger.error(
                    f"[WhatsApp] थाना bulk notify failed ({r.get('name')}, {r.get('contact')}): "
                    f"{result.get('error')}"
                )

    logger.info(
        f"[WhatsApp] थाना notification district={district!r} bails={len(bails)} "
        f"✓{sent} ✗{failed}"
    )
    return {"sent": sent, "failed": failed}


# ── High-level API used by accused_common.py / bail_bulk.py ──────────────────

def send_bail_whatsapp_notification(district, bails, approved_by_name, approved_by_id=None):
    """
    Notify every district super admin/admin (+ master/super_admin overseeing
    'All') about bail approval(s).

    bails: list of dicts with keys accused_name, fir_label, bail_type,
    bail_start, bail_end, (bail_remark/bail_rating optional).
      • len(bails) == 1 -> sends the `bail_approved` template (full detail).
      • len(bails) > 1  -> sends ONE `bail_bulk_approved` template per
        recipient, with every accused folded into the {{4}} details block —
        so a 20-accused Excel batch produces one WhatsApp message, not 20.
    """
    if not bails:
        return {"sent": 0, "failed": 0}

    if not is_whatsapp_ready():
        logger.info("[WhatsApp] Skipping bail notification — WhatsApp not configured.")
        return {"sent": 0, "failed": 0, "skipped": "not_configured"}

    recipients = get_whatsapp_recipients_for_district(district, exclude_user_id=approved_by_id)
    if not recipients:
        logger.info(f"[WhatsApp] No WhatsApp-eligible recipients in district={district!r}")
        return {"sent": 0, "failed": 0}

    sent, failed = 0, 0

    if len(bails) == 1:
        b = bails[0]
        for user in recipients:
            result = notify_bail_approved(
                user['contact'], b.get('accused_name'), b.get('fir_label'),
                b.get('bail_type'), b.get('bail_start'), b.get('bail_end'),
                approved_by_name,
            )
            if result.get("success"):
                sent += 1
            else:
                failed += 1
                logger.error(
                    f"[WhatsApp] Failed to notify user_id={user['id']} "
                    f"({user.get('name')}, {user.get('contact')}): {result.get('error')}"
                )
    else:
        details_list = format_bulk_bail_details(bails)
        for user in recipients:
            result = notify_bail_bulk_approved(
                user['contact'], district, len(bails), approved_by_name, details_list,
            )
            if result.get("success"):
                sent += 1
            else:
                failed += 1
                logger.error(
                    f"[WhatsApp] Failed to notify user_id={user['id']} "
                    f"({user.get('name')}, {user.get('contact')}): {result.get('error')}"
                )

    logger.info(
        f"[WhatsApp] Bail notification district={district!r} bails={len(bails)} "
        f"✓{sent} ✗{failed}"
    )
    return {"sent": sent, "failed": failed}
"""
bail_bulk.py
============
जमानत (Bail) से जुड़े दो नए features:

  1) एक जमानत रिकॉर्ड में एक से अधिक FIR चुनने का समर्थन (multi-FIR bail),
     ताकि एक ही अभियुक्त की कई FIR एक साथ एक जमानत में कवर हो सकें।

  2) माननीय न्यायालय द्वारा जमानत/रिहाई की Excel सूची अपलोड कर, अनेक
     अभियुक्तों की जमानत एक साथ स्वीकृत करना — नाम व पिता के नाम पर
     fuzzy-matching करके, थाना/FIR संख्या से क्रॉस-चेक करके।

     प्रवाह (2-चरण, ताकि कोई कानूनी रिकॉर्ड गलत मैच से न बन जाए):
       a) upload  -> parse हर पंक्ति -> हर पंक्ति का सर्वश्रेष्ठ मैच खोजें
                     -> bail_excel_batch + bail_excel_row में "staged" सेव करें
       b) review  -> admin को matched / ambiguous / not_found / already_bailed
                     / fir_not_found — सभी श्रेणियाँ साफ़-साफ़ दिखाई जाती हैं
       c) confirm -> admin जिन पंक्तियों को स्वीकार करता है, केवल उन्हीं के
                     लिए वास्तविक accused_bail_history रिकॉर्ड बनते हैं;
                     फ़ोटो/दस्तावेज़ photo_status='pending' रहते हैं और बाद
                     में "लंबित फ़ोटो" सूची से अलग से अपलोड किए जा सकते हैं।

Not-found / already-approved accused for a row are NEVER silently dropped —
they always surface in the batch's error report (see get_batch_review()).
"""

import re
import json
import logging
from datetime import datetime, date
from difflib import SequenceMatcher

from flask import session

from db import get_connection
from utils import upload_image, upload_document

logger = logging.getLogger(__name__)


# ── session helpers (mirrors accused_common.py) ────────────────────────────

def _district(): return session.get('district')
def _uid():      return session.get('user_id')
def _role():     return session.get('role')
def _bp():       return 'admin' if _role() == 'admin' else 'super'


# ── Name / address parsing for the "अभियुक्त का नाम पता" Excel column ──────
#
# Real-world format seen in court/jail release sheets:
#   "करन सोनकर पुत्र राजेश निवासी पाण्डेयपुर थाना को0 शहर मीरजापुर"
#    <name>       <पुत्र> <father>  <निवासी> <address ... थाना THANA DISTRICT>

_FATHER_SPLIT_RE = re.compile(
    r'\s+(?:s/o|S/O|पुत्र|पुत्री|w/o|W/O|d/o|D/O|पति|पत्नी)\s+'
)
_ADDRESS_SPLIT_RE = re.compile(r'\s+निवासी\s+')
_THANA_IN_ADDRESS_RE = re.compile(r'थाना\s+(.+)$')


def parse_bail_name_address(raw: str) -> dict:
    """
    Parse one "अभियुक्त का नाम पता" cell into name / father's name / address.
    Tolerant of missing pieces — always returns a dict with all four keys,
    falling back to empty strings rather than raising.
    """
    raw = re.sub(r'\s+', ' ', (raw or '')).strip()
    if not raw:
        return {'name': '', 'fathers_name': '', 'address': '', 'thana_guess': ''}

    addr_parts = _ADDRESS_SPLIT_RE.split(raw, maxsplit=1)
    name_father_part = addr_parts[0].strip()
    address = addr_parts[1].strip() if len(addr_parts) == 2 else ''

    m = _FATHER_SPLIT_RE.split(name_father_part, maxsplit=1)
    if len(m) == 2:
        name, fathers_name = m[0].strip(), m[1].strip()
    else:
        name, fathers_name = name_father_part.strip(), ''

    thana_guess = ''
    if address:
        tm = _THANA_IN_ADDRESS_RE.search(address)
        if tm:
            thana_guess = tm.group(1).strip()

    return {
        'name': name,
        'fathers_name': fathers_name,
        'address': address,
        'thana_guess': thana_guess,
    }


def normalize_name(name: str) -> str:
    """Same normalization as accused_common.normalize_name (kept local to
    avoid a circular import — both modules must stay in sync)."""
    if not name:
        return ''
    n = name.strip()
    for prefix in ['श्री ', 'श्रीमती ', 'Mr. ', 'Mrs. ', 'Smt. ', 'Shri ']:
        if n.startswith(prefix):
            n = n[len(prefix):]
    return re.sub(r'\s+', ' ', n).strip().lower()


def normalize_thana(thana: str) -> str:
    """Normalize a थाना string for loose comparison: strip 'थाना' word,
    trailing '0' (common OCR/typing artifact for '।'/'०' in UP Police
    sheets, e.g. 'को0 देहात'), and collapse whitespace."""
    if not thana:
        return ''
    t = thana.strip()
    t = re.sub(r'^थाना\s+', '', t)
    t = re.sub(r'0\b', '', t)          # 'को0' -> 'को'
    t = re.sub(r'\s+', ' ', t).strip().lower()
    return t


def name_similarity(a: str, b: str) -> float:
    """0..1 similarity between two normalized strings."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def weighted_match_score(name_a, name_b, father_a, father_b) -> tuple:
    """
    Returns (combined_score, name_score, father_score).
    Name carries more weight (0.6) since it's the primary identifier;
    father's name (0.4) disambiguates common first names — very common
    in Hindi naming, e.g. many "राम कुमार" but few share both name AND
    father's name.
    """
    ns = name_similarity(name_a, name_b)
    fs = name_similarity(father_a, father_b)
    return (0.6 * ns + 0.4 * fs), ns, fs


MATCH_THRESHOLD        = 0.80   # combined score to count as a real candidate
MIN_NAME_THRESHOLD     = 0.72   # name alone must be at least this close
AMBIGUOUS_GAP          = 0.04   # if top-2 candidates are within this gap -> ambiguous


# ── Date parsing (Excel cells arrive as datetime, date, or DD.MM.YYYY text) ─

def parse_excel_date(val):
    if val is None or val == '':
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    s = str(val).strip()
    for fmt in ('%d.%m.%Y', '%d-%m-%Y', '%d/%m/%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# ── Candidate lookup ─────────────────────────────────────────────────────────

def find_accused_candidates(cursor, district: str, name: str, fathers_name: str,
                             thana_col: str = '', fir_number_col: str = '', limit: int = 5):
    """
    Find the best-matching *bail-eligible* accused (arrested in >=1 FIR of
    this district, and currently has no active bail) for a parsed
    name/father pair from an uploaded bail Excel row.

    Scoping strategy (most reliable first):
      1. If both थाना and FIR संख्या columns resolve to a known fir_cases
         row, restrict candidates to accused linked to that exact FIR.
      2. Else if थाना alone resolves, restrict to accused arrested in that
         थाना within the district.
      3. Else fall back to every bail-eligible accused in the district.

    Returns (candidates, resolved_fir_id, fir_lookup_status) where
    candidates is a list of dicts sorted by score desc, each with:
      accused_id, name, fathers_name, score, name_score, father_score,
      fir_ids (list of int — this accused's arrest FIRs in the district).
    fir_lookup_status is one of 'exact', 'thana_only', 'none'.
    """
    name_norm   = normalize_name(name)
    father_norm = normalize_name(fathers_name)
    thana_norm  = normalize_thana(thana_col)
    fir_number  = (fir_number_col or '').strip()

    resolved_fir_id  = None
    fir_lookup_status = 'none'

    if thana_norm and fir_number:
        cursor.execute("""
            SELECT id, thana FROM fir_cases
            WHERE district=%s AND fir_number=%s
        """, (district, fir_number))
        for row in cursor.fetchall():
            if normalize_thana(row['thana']) == thana_norm:
                resolved_fir_id = row['id']
                fir_lookup_status = 'exact'
                break

    # Base pool: bail-eligible accused (arrested, no active bail) in district
    base_sql = """
        SELECT DISTINCT a.id, a.name, a.fathers_name, a.name_normalized, a.fathers_normalized
        FROM accused a
        JOIN accused_fir af ON af.accused_id = a.id
        JOIN fir_cases f ON f.id = af.fir_id
        WHERE f.district = %s AND af.in_arrested = 1
        AND (a.bail_status IS NULL OR a.bail_status = 'none')
    """
    params = [district]
    if resolved_fir_id:
        base_sql += " AND af.fir_id = %s"
        params.append(resolved_fir_id)
    elif thana_norm:
        base_sql += " AND f.thana = %s"
        params.append(thana_col.strip())
        fir_lookup_status = 'thana_only'

    cursor.execute(base_sql, params)
    pool = cursor.fetchall()

    # If the thana-scoped pool is empty (e.g. thana text mismatch/typo),
    # fall back to the whole district rather than reporting a false negative.
    if not pool and (resolved_fir_id or thana_norm):
        cursor.execute("""
            SELECT DISTINCT a.id, a.name, a.fathers_name, a.name_normalized, a.fathers_normalized
            FROM accused a
            JOIN accused_fir af ON af.accused_id = a.id
            JOIN fir_cases f ON f.id = af.fir_id
            WHERE f.district = %s AND af.in_arrested = 1
            AND (a.bail_status IS NULL OR a.bail_status = 'none')
        """, (district,))
        pool = cursor.fetchall()
        fir_lookup_status = 'none'
        resolved_fir_id = None

    scored = []
    for cand in pool:
        score, ns, fs = weighted_match_score(
            name_norm, cand['name_normalized'], father_norm, cand['fathers_normalized']
        )
        if score >= MATCH_THRESHOLD and ns >= MIN_NAME_THRESHOLD:
            scored.append({
                'accused_id': cand['id'], 'name': cand['name'],
                'fathers_name': cand['fathers_name'],
                'score': round(score, 4), 'name_score': round(ns, 4),
                'father_score': round(fs, 4),
            })

    scored.sort(key=lambda c: c['score'], reverse=True)
    scored = scored[:limit]

    # Attach each candidate's arrest-FIRs in this district (for display +
    # so confirm() knows which FIR(s) to attach the new bail record to).
    for c in scored:
        cursor.execute("""
            SELECT DISTINCT f.id FROM accused_fir af
            JOIN fir_cases f ON f.id = af.fir_id
            WHERE af.accused_id=%s AND af.in_arrested=1 AND f.district=%s
        """, (c['accused_id'], district))
        c['fir_ids'] = [r['id'] for r in cursor.fetchall()]

    return scored, resolved_fir_id, fir_lookup_status


def classify_match(candidates, resolved_fir_id):
    """
    Decide match_status from a scored candidate list.
    Returns (status, matched_accused_id_or_None, resolved_fir_ids_list).
    """
    if not candidates:
        return 'not_found', None, []

    if len(candidates) >= 2 and (candidates[0]['score'] - candidates[1]['score']) < AMBIGUOUS_GAP:
        return 'ambiguous', None, []

    best = candidates[0]
    fir_ids = [resolved_fir_id] if resolved_fir_id else best['fir_ids']
    return 'matched', best['accused_id'], fir_ids


# ── Stage: parse the uploaded Excel + fuzzy-match every row ─────────────────

# Expected header (UP Police jail-release format), 0-indexed column order:
#  0 क्र0सं0 | 1 थाना | 2 मु0अ0सं0 | 3 एसटी नं0 | 4 भादवि0 की धारा |
#  5 बीएनएस की धारा | 6 अभियुक्त का नाम पता | 7 अभियुक्त का फोटो |
#  8 अपराधिक इतिहास | 9 मा0 न्यायालय का नाम | 10 जमानत दिये जाने का दिनांक |
#  11 जेल जाने का दिनांक | 12 रिहा किये जाने की तिथी | 13 चौकी/हल्का | 14 बीट सं0
BAIL_EXCEL_COLUMNS = 15


def stage_bail_excel(file, filename: str, district: str, uploaded_by: int) -> int:
    """
    Parse + fuzzy-match every accused row in the uploaded Excel, persist
    the batch + all rows (matched and unmatched alike) to the database,
    and return the new batch_id. Does NOT create any bail record yet —
    that only happens on confirm_batch().
    """
    import openpyxl
    wb = openpyxl.load_workbook(file, data_only=True)
    ws = wb.active

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        INSERT INTO bail_excel_batch (district, filename, uploaded_by, status)
        VALUES (%s, %s, %s, 'staged')
    """, (district, filename, uploaded_by))
    batch_id = cursor.lastrowid

    # Find the header row (first row containing 'अभियुक्त' anywhere), then
    # start reading data from the row right after it. Falls back to row 2.
    header_row_idx = 1
    for idx, row in enumerate(ws.iter_rows(min_row=1, max_row=min(5, ws.max_row), values_only=True), start=1):
        if row and any(cell and 'अभियुक्त' in str(cell) for cell in row):
            header_row_idx = idx
            break

    total_rows, matched_rows, error_rows = 0, 0, 0

    for row_idx, row in enumerate(
            ws.iter_rows(min_row=header_row_idx + 1, values_only=True), start=header_row_idx + 1):
        if not row or not any(row):
            continue
        cells = (list(row) + [None] * BAIL_EXCEL_COLUMNS)[:BAIL_EXCEL_COLUMNS]
        (_sr, thana_col, fir_number_col, _st_no, _ipc, _bns,
         accused_field, _photo, criminal_history, court_name,
         bail_date_raw, jail_date_raw, release_date_raw, _chowki, _beat) = cells

        accused_field = str(accused_field or '').strip()
        if not accused_field:
            continue  # blank accused cell — nothing to import for this row

        total_rows += 1
        parsed = parse_bail_name_address(accused_field)
        thana_col_s = str(thana_col or '').strip()
        fir_number_s = str(fir_number_col or '').strip()

        if not parsed['name']:
            cursor.execute("""
                INSERT INTO bail_excel_row
                    (batch_id, row_number, raw_name_field, parsed_name, parsed_fathers_name,
                     parsed_address, thana_col, fir_number_col, court_name, jail_date,
                     release_date, bail_date, criminal_history, match_status, include_in_confirm)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'not_found',0)
            """, (batch_id, row_idx, accused_field, '', '', parsed['address'],
                  thana_col_s, fir_number_s, str(court_name or '').strip(),
                  parse_excel_date(jail_date_raw), parse_excel_date(release_date_raw),
                  parse_excel_date(bail_date_raw), str(criminal_history or '').strip()))
            error_rows += 1
            continue

        candidates, resolved_fir_id, _lookup = find_accused_candidates(
            cursor, district, parsed['name'], parsed['fathers_name'],
            thana_col_s, fir_number_s
        )
        status, matched_id, fir_ids = classify_match(candidates, resolved_fir_id)

        # already_bailed check: only relevant if the *exact* normalized
        # name+father exists but is currently excluded from the eligible
        # pool because they already have an active bail.
        if status == 'not_found':
            name_norm = normalize_name(parsed['name'])
            father_norm = normalize_name(parsed['fathers_name'])
            cursor.execute("""
                SELECT a.id FROM accused a
                WHERE a.name_normalized = %s AND a.fathers_normalized = %s
                AND a.bail_status != 'none'
                LIMIT 1
            """, (name_norm, father_norm))
            if cursor.fetchone():
                status = 'already_bailed'

        if status == 'matched':
            matched_rows += 1
        else:
            error_rows += 1

        cursor.execute("""
            INSERT INTO bail_excel_row
                (batch_id, row_number, raw_name_field, parsed_name, parsed_fathers_name,
                 parsed_address, thana_col, fir_number_col, court_name, jail_date,
                 release_date, bail_date, criminal_history, match_status,
                 matched_accused_id, match_confidence, candidate_ids, resolved_fir_ids,
                 include_in_confirm)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            batch_id, row_idx, accused_field, parsed['name'], parsed['fathers_name'],
            parsed['address'], thana_col_s, fir_number_s, str(court_name or '').strip(),
            parse_excel_date(jail_date_raw), parse_excel_date(release_date_raw),
            parse_excel_date(bail_date_raw), str(criminal_history or '').strip(),
            status, matched_id,
            candidates[0]['score'] if candidates else None,
            ','.join(str(c['accused_id']) for c in candidates) if status == 'ambiguous' else None,
            ','.join(str(i) for i in fir_ids) if fir_ids else None,
            1 if status == 'matched' else 0,
        ))

    cursor.execute("""
        UPDATE bail_excel_batch SET total_rows=%s, matched_rows=%s, error_rows=%s WHERE id=%s
    """, (total_rows, matched_rows, error_rows, batch_id))
    conn.commit()
    cursor.close()
    conn.close()
    return batch_id


# ── Review: fetch a staged batch for admin confirmation ─────────────────────

MATCH_STATUS_LABELS = {
    'matched':        'मैच मिला',
    'ambiguous':      'एक से अधिक संभावित मैच — मैन्युअल जाँच आवश्यक',
    'not_found':      'सिस्टम में यह अभियुक्त नहीं मिला',
    'already_bailed': 'इस अभियुक्त की जमानत पहले से स्वीकृत है',
    'fir_not_found':  'FIR सिस्टम में नहीं मिली',
}


def get_batch_review(batch_id: int, district: str):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM bail_excel_batch WHERE id=%s AND district=%s", (batch_id, district))
    batch = cursor.fetchone()
    if not batch:
        cursor.close(); conn.close()
        return None, []

    cursor.execute("""
        SELECT r.*, a.name AS matched_name, a.fathers_name AS matched_fathers_name,
               a.photo_url AS matched_photo_url
        FROM bail_excel_row r
        LEFT JOIN accused a ON a.id = r.matched_accused_id
        WHERE r.batch_id=%s
        ORDER BY r.row_number
    """, (batch_id,))
    rows = cursor.fetchall()

    for r in rows:
        r['status_label'] = MATCH_STATUS_LABELS.get(r['match_status'], r['match_status'])
        if r['match_status'] == 'ambiguous' and r['candidate_ids']:
            ids = [int(x) for x in r['candidate_ids'].split(',') if x]
            if ids:
                fmt_ids = ','.join(['%s'] * len(ids))
                cursor.execute(f"SELECT id, name, fathers_name FROM accused WHERE id IN ({fmt_ids})", ids)
                r['candidates'] = cursor.fetchall()
            else:
                r['candidates'] = []
        else:
            r['candidates'] = []
        if r['resolved_fir_ids']:
            fids = [int(x) for x in r['resolved_fir_ids'].split(',') if x]
            if fids:
                fmt_ids = ','.join(['%s'] * len(fids))
                cursor.execute(
                    f"SELECT id, fir_number, thana FROM fir_cases WHERE id IN ({fmt_ids})", fids)
                r['resolved_firs'] = cursor.fetchall()
            else:
                r['resolved_firs'] = []
        else:
            r['resolved_firs'] = []

    cursor.close()
    conn.close()
    return batch, rows


# ── Confirm: create real bail records for the rows admin approved ───────────

def confirm_batch(batch_id: int, district: str, confirmed_row_ids: list, resolved_by_uid: int):
    """
    For every bail_excel_row in this batch whose id is in confirmed_row_ids
    AND whose match_status == 'matched' (ambiguous/not_found/already_bailed
    rows are never auto-confirmed, even if somehow included), create an
    accused_bail_history record with photo_status='pending' (the photo /
    supporting document is uploaded afterwards from the "लंबित फ़ोटो" list),
    attach every resolved FIR via accused_bail_fir, and update the accused's
    current bail_status.

    Returns dict: {created: [...], skipped: [...]}
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM bail_excel_batch WHERE id=%s AND district=%s", (batch_id, district))
    batch = cursor.fetchone()
    if not batch or batch['status'] != 'staged':
        cursor.close(); conn.close()
        return {'created': [], 'skipped': [], 'error': 'बैच पहले ही प्रोसेस हो चुका है या नहीं मिला।'}

    cursor.execute("SELECT * FROM bail_excel_row WHERE batch_id=%s", (batch_id,))
    all_rows = {r['id']: r for r in cursor.fetchall()}

    created, skipped = [], []

    for row_id in confirmed_row_ids:
        r = all_rows.get(row_id)
        if not r or r['match_status'] != 'matched' or not r['matched_accused_id']:
            if r:
                skipped.append({'row': r['row_number'], 'reason': 'मैच नहीं — auto-skip'})
            continue

        accused_id = r['matched_accused_id']
        cursor.execute("SELECT bail_status FROM accused WHERE id=%s", (accused_id,))
        acc = cursor.fetchone()
        if not acc or (acc['bail_status'] and acc['bail_status'] != 'none'):
            skipped.append({'row': r['row_number'], 'reason': 'अभियुक्त की जमानत पहले ही सक्रिय हो गई — दोबारा नहीं बनाई गई'})
            continue

        fir_ids = [int(x) for x in (r['resolved_fir_ids'] or '').split(',') if x]
        if not fir_ids:
            skipped.append({'row': r['row_number'], 'reason': 'कोई गिरफ़्तारी FIR नहीं मिली'})
            continue

        bail_type = 'temporary'
        bail_start = r['bail_date'] or date.today()
        bail_end = None  # court-order Excel imports don't carry an explicit end date

        cursor.execute("""
            INSERT INTO accused_bail_history
                (accused_id, fir_id, bail_type, bail_start_date, bail_end_date,
                 status, approved_by, court_name, jail_date, release_date,
                 criminal_history, photo_status, source, excel_batch_id, excel_row_raw)
            VALUES (%s,%s,%s,%s,%s,'ACTIVE',%s,%s,%s,%s,%s,'pending','excel_bulk',%s,%s)
        """, (
            accused_id, fir_ids[0], bail_type, bail_start, bail_end,
            resolved_by_uid, r['court_name'], r['jail_date'], r['release_date'],
            r['criminal_history'], batch_id,
            json.dumps({'raw_name_field': r['raw_name_field'], 'row_number': r['row_number']},
                       ensure_ascii=False),
        ))
        bail_id = cursor.lastrowid

        for fid in fir_ids:
            cursor.execute("""
                INSERT IGNORE INTO accused_bail_fir (bail_id, fir_id) VALUES (%s, %s)
            """, (bail_id, fid))

        cursor.execute("""
            UPDATE accused SET bail_status=%s, bail_start_date=%s, bail_end_date=%s,
            bail_remark=%s, updated_by=%s WHERE id=%s
        """, (bail_type, bail_start, bail_end,
              'Excel बल्क जमानत स्वीकृति — फ़ोटो/दस्तावेज़ लंबित', resolved_by_uid, accused_id))

        cursor.execute("UPDATE bail_excel_row SET created_bail_id=%s WHERE id=%s", (bail_id, r['id']))

        created.append({'row': r['row_number'], 'accused_id': accused_id,
                         'name': r['parsed_name'], 'bail_id': bail_id})

    cursor.execute("""
        UPDATE bail_excel_batch SET status='confirmed', confirmed_by=%s, confirmed_at=NOW() WHERE id=%s
    """, (resolved_by_uid, batch_id))
    conn.commit()
    cursor.close()
    conn.close()
    return {'created': created, 'skipped': skipped}


# ── Pending photo/document completion ────────────────────────────────────────

def list_pending_photo_bails(district: str):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT abh.id AS bail_id, abh.accused_id, abh.bail_start_date, abh.court_name,
               abh.jail_date, abh.release_date, abh.approved_at,
               a.name, a.fathers_name, a.photo_url,
               GROUP_CONCAT(DISTINCT f.fir_number SEPARATOR ', ') AS fir_numbers,
               GROUP_CONCAT(DISTINCT f.thana SEPARATOR ', ') AS thanas
        FROM accused_bail_history abh
        JOIN accused a ON a.id = abh.accused_id
        LEFT JOIN accused_bail_fir abf ON abf.bail_id = abh.id
        LEFT JOIN fir_cases f ON f.id = abf.fir_id
        WHERE abh.photo_status = 'pending' AND abh.status = 'ACTIVE'
        AND EXISTS (
            SELECT 1 FROM accused_bail_fir x JOIN fir_cases fx ON fx.id = x.fir_id
            WHERE x.bail_id = abh.id AND fx.district = %s
        )
        GROUP BY abh.id
        ORDER BY abh.approved_at DESC
    """, (district,))
    rows = cursor.fetchall()
    cursor.close(); conn.close()
    return rows


def complete_bail_photo(bail_id: int, district: str, uid: int,
                         photo_data: str = None, doc_file=None):
    """
    Upload the (optionally later-captured) geo-tagged photo and/or
    supporting document for a bail record that was created via Excel bulk
    import (photo_status='pending'), and mark it complete.
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT abh.*, a.name, a.photo_url AS accused_photo_url
        FROM accused_bail_history abh JOIN accused a ON a.id = abh.accused_id
        WHERE abh.id=%s
    """, (bail_id,))
    bail = cursor.fetchone()
    if not bail:
        cursor.close(); conn.close()
        return False, 'जमानत रिकॉर्ड नहीं मिला।'

    photo_url, photo_public_id = None, None
    if photo_data and photo_data.startswith('data:image'):
        photo_url, photo_public_id = upload_image(photo_data, folder='accused_bail_photos')

    doc_url, doc_public_id, doc_res_type = None, None, 'raw'
    if doc_file and getattr(doc_file, 'filename', None):
        doc_url, doc_public_id, doc_res_type = upload_document(doc_file, folder='accused_bail_docs')

    if not photo_url and not doc_url:
        cursor.close(); conn.close()
        return False, 'कृपया फ़ोटो या दस्तावेज़ में से कम से कम एक अपलोड करें।'

    set_clauses, params = [], []
    if photo_url:
        set_clauses += ["bail_photo_url=%s", "bail_photo_public_id=%s"]
        params += [photo_url, photo_public_id]
    if doc_url:
        set_clauses += ["bail_document_url=%s", "bail_document_public_id=%s", "bail_document_resource_type=%s"]
        params += [doc_url, doc_public_id, doc_res_type]
    set_clauses.append("photo_status='complete'")
    params.append(bail_id)
    cursor.execute(f"UPDATE accused_bail_history SET {', '.join(set_clauses)} WHERE id=%s", params)

    # Mirror onto accused's current-bail columns + profile photo if missing
    acc_clauses, acc_params = [], []
    if photo_url:
        acc_clauses += ["bail_photo_url=%s", "bail_photo_public_id=%s"]
        acc_params += [photo_url, photo_public_id]
        if not bail['accused_photo_url']:
            acc_clauses += ["photo_url=%s", "photo_public_id=%s", "profile_status='complete'"]
            acc_params += [photo_url, photo_public_id]
            cursor.execute("UPDATE accused_photos SET is_current=0 WHERE accused_id=%s", (bail['accused_id'],))
            cursor.execute("""
                INSERT INTO accused_photos (accused_id, photo_url, photo_public_id, is_current, uploaded_by)
                VALUES (%s,%s,%s,1,%s)
            """, (bail['accused_id'], photo_url, photo_public_id, uid))
    if doc_url:
        acc_clauses += ["bail_documents_url=%s", "bail_documents_public_id=%s"]
        acc_params += [doc_url, doc_public_id]
    if acc_clauses:
        acc_clauses.append("updated_by=%s")
        acc_params.append(uid)
        acc_params.append(bail['accused_id'])
        cursor.execute(f"UPDATE accused SET {', '.join(acc_clauses)} WHERE id=%s", acc_params)

    conn.commit()
    cursor.close()
    conn.close()
    return True, 'फ़ोटो/दस्तावेज़ सफलतापूर्वक जोड़ा गया — जमानत रिकॉर्ड पूर्ण।'
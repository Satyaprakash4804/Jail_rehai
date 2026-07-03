"""
db.py
=====
This system is fully Accused-based (अभियुक्त-आधारित). There is no
criminal-management module — legacy `criminals` / `crime_records` /
`criminal_*` tables have been removed from the schema entirely.

Tables:
  • users               — master / super_admin / admin accounts
  • activity_logs        — audit trail
  • fir_cases            — एक FIR / एक मामला (FIR No, Thana, District, Acts, Status)
  • accused              — अभियुक्त (deduplicated by name + father, normalised)
  • accused_fir          — M:N junction: किस FIR में अभियुक्त की क्या भूमिका
  • accused_photos       — अभियुक्त के फ़ोटो इतिहास
  • accused_bail_history — जमानत स्वीकृति/निरस्तीकरण का स्थायी इतिहास
  • notifications        — in-app notifications (bail approvals etc.)
  • fcm_tokens           — push-notification device tokens
"""

import mysql.connector
from mysql.connector import Error
from config import DB_CONFIG
import logging

logger = logging.getLogger(__name__)


def get_connection():
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        return conn
    except Error as e:
        logger.error(f"DB connection error: {e}")
        raise


def init_db():
    cfg = dict(DB_CONFIG)
    dbname = cfg.pop("database")
    try:
        conn = mysql.connector.connect(**cfg)
        cursor = conn.cursor()
        cursor.execute(
            f"CREATE DATABASE IF NOT EXISTS `{dbname}` "
            f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        conn.commit()
        cursor.close()
        conn.close()
    except Error as e:
        logger.error(f"Error creating database: {e}")
        raise

    conn = get_connection()
    cursor = conn.cursor()

    # ── Users ─────────────────────────────────────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INT AUTO_INCREMENT PRIMARY KEY,
        user_id VARCHAR(50) UNIQUE NOT NULL,
        name VARCHAR(150) NOT NULL,
        designation VARCHAR(100),
        contact VARCHAR(20),
        email VARCHAR(150) UNIQUE NOT NULL,
        district VARCHAR(100),
        address TEXT,
        password_hash VARCHAR(256) NOT NULL,
        role ENUM('master','super_admin','admin') NOT NULL,
        created_by INT,
        is_active TINYINT(1) DEFAULT 1,
        otp_code VARCHAR(10),
        otp_expiry DATETIME,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
    ) ENGINE=InnoDB CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """)

    # ── Activity logs ─────────────────────────────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS activity_logs (
        id INT AUTO_INCREMENT PRIMARY KEY,
        user_id INT,
        user_role VARCHAR(50),
        action VARCHAR(255) NOT NULL,
        endpoint VARCHAR(255),
        method VARCHAR(10),
        ip_address VARCHAR(50),
        status_code INT,
        details TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
    ) ENGINE=InnoDB CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """)

    # ══════════════════════════════════════════════════════════════════════════
    # NEW: FIR Cases Table
    # एक पंक्ति = एक FIR मामला
    # ══════════════════════════════════════════════════════════════════════════
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS fir_cases (
        id               INT AUTO_INCREMENT PRIMARY KEY,
        district         VARCHAR(150) NOT NULL COMMENT 'जनपद',
        thana            VARCHAR(150) NOT NULL COMMENT 'थाना',
        fir_number       VARCHAR(50)  NOT NULL COMMENT 'FIR संख्या',
        acts             TEXT         COMMENT 'धाराएँ / Acts',
        total_accused_raw    TEXT     COMMENT 'कुल अभियुक्त (raw from Excel)',
        fir_accused_raw      TEXT     COMMENT 'FIR में अभियुक्त (raw)',
        arrested_accused_raw TEXT     COMMENT 'गिरफ्तार अभियुक्त (raw)',
        cs_accused_raw       TEXT     COMMENT 'आरोप पत्र अभियुक्त (raw)',
        complainant      VARCHAR(300) COMMENT 'वादी',
        status           VARCHAR(100) COMMENT 'स्थिति',
        total_accused_count  INT DEFAULT 0,
        created_by       INT,
        created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at       DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uq_fir (district, thana, fir_number),
        FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
    ) ENGINE=InnoDB CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """)

    # ══════════════════════════════════════════════════════════════════════════
    # NEW: Accused (अभियुक्त) — deduplicated master record
    # एक अभियुक्त एक बार — भले ही कितनी FIR में हो
    # Dedup key: name_normalized + fathers_name_normalized
    # ══════════════════════════════════════════════════════════════════════════
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS accused (
        id               INT AUTO_INCREMENT PRIMARY KEY,
        name             VARCHAR(200) NOT NULL COMMENT 'अभियुक्त का नाम',
        name_normalized  VARCHAR(200) NOT NULL COMMENT 'searchable normalized',
        fathers_name     VARCHAR(200) NOT NULL COMMENT 'पिता का नाम (S/o)',
        fathers_normalized VARCHAR(200) NOT NULL,
        address          TEXT         COMMENT 'पता',
        dob              DATE         COMMENT 'जन्म तिथि',
        photo_url        VARCHAR(500),
        photo_public_id  VARCHAR(255),
        profile_status   ENUM('pending','complete') DEFAULT 'pending',
        created_by       INT,
        updated_by       INT,
        created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at       DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        KEY idx_name_norm (name_normalized(100)),
        KEY idx_father_norm (fathers_normalized(100)),
        FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL,
        FOREIGN KEY (updated_by) REFERENCES users(id) ON DELETE SET NULL
    ) ENGINE=InnoDB CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """)

    # ══════════════════════════════════════════════════════════════════════════
    # NEW: Accused ↔ FIR junction table
    # एक अभियुक्त की एक FIR में भूमिका
    # ══════════════════════════════════════════════════════════════════════════
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS accused_fir (
        id               INT AUTO_INCREMENT PRIMARY KEY,
        accused_id       INT NOT NULL,
        fir_id           INT NOT NULL,
        -- अभियुक्त की इस FIR में स्थिति
        in_total_accused     TINYINT(1) DEFAULT 0 COMMENT 'कुल अभियुक्त में है',
        in_fir_accused       TINYINT(1) DEFAULT 0 COMMENT 'FIR में नामित',
        in_arrested          TINYINT(1) DEFAULT 0 COMMENT 'गिरफ्तार',
        in_cs_accused        TINYINT(1) DEFAULT 0 COMMENT 'आरोप पत्र में',
        created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_accused_fir (accused_id, fir_id),
        FOREIGN KEY (accused_id) REFERENCES accused(id) ON DELETE CASCADE,
        FOREIGN KEY (fir_id)     REFERENCES fir_cases(id) ON DELETE CASCADE
    ) ENGINE=InnoDB CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """)

    # ── Accused photos ─────────────────────────────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS accused_photos (
        id               INT AUTO_INCREMENT PRIMARY KEY,
        accused_id       INT NOT NULL,
        photo_url        VARCHAR(500),
        photo_public_id  VARCHAR(255),
        is_current       TINYINT(1) DEFAULT 1,
        uploaded_by      INT,
        uploaded_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (accused_id) REFERENCES accused(id) ON DELETE CASCADE,
        FOREIGN KEY (uploaded_by) REFERENCES users(id) ON DELETE SET NULL
    ) ENGINE=InnoDB CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """)

    # ══════════════════════════════════════════════════════════════════════════
    # NEW: Accused Bail (जमानत) — ONLY valid for accused who are marked
    # in_arrested=1 in at least one FIR (accused_fir.in_arrested).
    # Admin/Super Admin action is "Approve Bail" (स्वीकृत करें), not "Grant".
    # accused.bail_* columns hold the CURRENT active bail (this is the single
    # table pattern). accused_bail_history keeps EVERY bail record forever —
    # so if the same accused is arrested again in a different/future FIR,
    # the full bail + FIR history is still visible on their detail page.
    # ══════════════════════════════════════════════════════════════════════════
    for sql in [
        "ALTER TABLE accused ADD COLUMN bail_status ENUM('none','temporary','permanent') DEFAULT 'none'",
        "ALTER TABLE accused ADD COLUMN bail_start_date DATE",
        "ALTER TABLE accused ADD COLUMN bail_end_date DATE",
        "ALTER TABLE accused ADD COLUMN bail_documents_url VARCHAR(500)",
        "ALTER TABLE accused ADD COLUMN bail_documents_public_id VARCHAR(255)",
        "ALTER TABLE accused ADD COLUMN bail_remark TEXT",
        "ALTER TABLE accused ADD COLUMN bail_rating INT DEFAULT 0",
        "ALTER TABLE accused ADD COLUMN bail_photo_url VARCHAR(500) COMMENT 'जमानत स्वीकृति के समय लिया गया geo-tagged लाइव फ़ोटो'",
        "ALTER TABLE accused ADD COLUMN bail_photo_public_id VARCHAR(255)",
        "ALTER TABLE accused ADD COLUMN bail_photo_lat DECIMAL(10,7)",
        "ALTER TABLE accused ADD COLUMN bail_photo_lng DECIMAL(10,7)",
        "ALTER TABLE accused ADD COLUMN bail_photo_captured_at DATETIME",
    ]:
        try:
            cursor.execute(sql)
            conn.commit()
        except Exception:
            pass

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS accused_bail_history (
        id                       INT AUTO_INCREMENT PRIMARY KEY,
        accused_id               INT NOT NULL COMMENT 'अभियुक्त',
        fir_id                   INT NOT NULL COMMENT 'जिस FIR में गिरफ़्तारी के आधार पर जमानत स्वीकृत हुई',
        bail_type                ENUM('temporary','permanent') NOT NULL,
        bail_start_date          DATE,
        bail_end_date            DATE,
        bail_document_url        VARCHAR(500),
        bail_document_public_id  VARCHAR(255),
        bail_document_resource_type VARCHAR(20) DEFAULT 'raw',
        bail_photo_url           VARCHAR(500) COMMENT 'जमानत स्वीकृति के समय लिया गया geo-tagged लाइव फ़ोटो',
        bail_photo_public_id     VARCHAR(255),
        bail_photo_lat           DECIMAL(10,7),
        bail_photo_lng           DECIMAL(10,7),
        bail_photo_captured_at   DATETIME,
        bail_remark              TEXT,
        bail_rating              INT DEFAULT 0,
        status                   ENUM('ACTIVE','REVOKED','COMPLETED') DEFAULT 'ACTIVE',
        approved_by              INT COMMENT 'जमानत स्वीकृत करने वाला admin/super_admin',
        approved_at              DATETIME DEFAULT CURRENT_TIMESTAMP,
        revoked_by                INT,
        revoked_at                DATETIME,
        revoke_reason              TEXT,
        completed_at              DATETIME,
        FOREIGN KEY (accused_id) REFERENCES accused(id) ON DELETE CASCADE,
        FOREIGN KEY (fir_id)     REFERENCES fir_cases(id) ON DELETE CASCADE,
        FOREIGN KEY (approved_by) REFERENCES users(id) ON DELETE SET NULL,
        FOREIGN KEY (revoked_by)  REFERENCES users(id) ON DELETE SET NULL
    ) ENGINE=InnoDB CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS notifications (
        id INT AUTO_INCREMENT PRIMARY KEY,
        user_id INT NOT NULL, district VARCHAR(100),
        type VARCHAR(50) DEFAULT 'bail_granted',
        title VARCHAR(255), message TEXT, is_read TINYINT(1) DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    ) ENGINE=InnoDB CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS fcm_tokens (
        id          INT AUTO_INCREMENT PRIMARY KEY,
        user_id     INT NOT NULL,
        token       TEXT NOT NULL,
        device_type ENUM('web','android','ios') NOT NULL DEFAULT 'web',
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    ) ENGINE=InnoDB CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """)

    # Safe migrations
    for sql in [
        "ALTER TABLE fcm_tokens ADD COLUMN device_type ENUM('web','android','ios') NOT NULL DEFAULT 'web'",
        "ALTER TABLE fcm_tokens ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
        "ALTER TABLE fcm_tokens ADD UNIQUE KEY uq_user_token (user_id, token(255))",
        "ALTER TABLE accused_bail_history ADD INDEX idx_accused_status (accused_id, status)",
        "ALTER TABLE accused_bail_history ADD INDEX idx_fir (fir_id)",
        "ALTER TABLE accused_bail_history ADD COLUMN bail_photo_url VARCHAR(500)",
        "ALTER TABLE accused_bail_history ADD COLUMN bail_photo_public_id VARCHAR(255)",
        "ALTER TABLE accused_bail_history ADD COLUMN bail_photo_lat DECIMAL(10,7)",
        "ALTER TABLE accused_bail_history ADD COLUMN bail_photo_lng DECIMAL(10,7)",
        "ALTER TABLE accused_bail_history ADD COLUMN bail_photo_captured_at DATETIME",
    ]:
        try:
            cursor.execute(sql)
            conn.commit()
        except Exception:
            pass

    conn.commit()
    cursor.close()
    conn.close()
    _drop_legacy_criminal_tables()
    _create_master_admin()
    logger.info("Database initialized successfully.")


def _drop_legacy_criminal_tables():
    """
    One-time cleanup migration: drop legacy criminal-management tables
    from any older installation of this system. This system now manages
    only Accused (अभियुक्त) records — there is no criminal module.
    Safe to run repeatedly; each DROP is a no-op if the table is absent.
    """
    conn = get_connection()
    cursor = conn.cursor()
    legacy_tables = [
        "bail_notifications",
        "criminal_id_cards",
        "criminal_bail_history",
        "criminal_photos",
        "crime_records",
        "criminals",
    ]
    for tbl in legacy_tables:
        try:
            cursor.execute(f"DROP TABLE IF EXISTS `{tbl}`")
            conn.commit()
        except Exception as e:
            logger.warning(f"Legacy table cleanup skipped for {tbl}: {e}")
    cursor.close()
    conn.close()


def _create_master_admin():
    from werkzeug.security import generate_password_hash
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id FROM users WHERE role='master' LIMIT 1")
    if not cursor.fetchone():
        cursor.execute("""
            INSERT INTO users
                (user_id,name,designation,contact,email,district,
                 address,password_hash,role,is_active)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'master',1)
        """, (
            'MASTER001', 'Master Admin', 'Developer', '0000000000',
            'master@jailrehai.gov.in', 'All', 'HQ',
            generate_password_hash('Master@123')
        ))
        conn.commit()
        logger.info("Default master admin created.")
    cursor.close()
    conn.close()

init_db()
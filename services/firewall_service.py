import re
import sqlite3
from datetime import datetime, timedelta

from flask import current_app, request, session

from services.abuse_guard import get_client_ip, get_user_agent


SUSPICIOUS_PATH_PATTERNS = [
    ("ENV_PROBE", re.compile(r"/(\.env|env|config|secrets?|backup|\.git|wp-admin|wp-login|phpmyadmin|adminer|cgi-bin)", re.I)),
    ("SOURCE_PROBE", re.compile(r"\.(php|asp|aspx|jsp|bak|old|sql|zip|tar|gz)(/|$|\?)", re.I)),
    ("TRAVERSAL", re.compile(r"(\.\./|\.\.\\|%2e%2e|%252e%252e)", re.I)),
]

SUSPICIOUS_QUERY_PATTERNS = [
    ("SQLI", re.compile(r"(union\s+select|select\s+.+\s+from|or\s+1\s*=\s*1|sleep\s*\(|benchmark\s*\(|information_schema)", re.I)),
    ("XSS", re.compile(r"(<script|javascript:|onerror\s*=|onload\s*=|%3cscript)", re.I)),
    ("COMMAND_INJECTION", re.compile(r"(;\s*(cat|curl|wget|bash|sh)\b|\|\s*(cat|curl|wget|bash|sh)\b|`.+`|\$\(.+\))", re.I)),
]

SUSPICIOUS_UA_PATTERNS = [
    ("BOT_SCANNER", re.compile(r"(sqlmap|nikto|nmap|masscan|acunetix|nessus|wpscan|python-requests|curl|wget)", re.I)),
]

ATTACK_GUIDANCE = {
    "ENV_PROBE": "Block the IP if repeated, keep secrets out of repo, and verify .env/.git paths are not publicly served.",
    "SOURCE_PROBE": "Block scanner IPs and keep framework routes from serving backup/source archive files.",
    "TRAVERSAL": "Block repeated IPs, keep upload path sanitization strict, and review file-serving routes.",
    "SQLI": "Block repeated IPs, keep parameterized SQL, and review the targeted route inputs.",
    "XSS": "Block repeated IPs, keep template escaping, and review any rich-text or URL input on targeted routes.",
    "COMMAND_INJECTION": "Block immediately if repeated and review any route that shells out or handles file paths.",
    "BOT_SCANNER": "Rate-limit or block repeated scanner traffic; require Turnstile on public auth/checkout flows.",
    "AUTH_ABUSE": "Keep login/register rate limits, Turnstile, invite code, and email suppression enabled.",
    "BLOCKED_IP": "IP is already blocked by Admin/firewall policy.",
    "UNKNOWN": "Review the request path, user-agent, and account role before deciding to block.",
}


def firewall_enabled():
    return bool(current_app.config.get("FIREWALL_MONITOR_ENABLED", True))


def firewall_auto_block_enabled():
    return bool(current_app.config.get("FIREWALL_AUTO_BLOCK_ENABLED", False))


def firewall_auto_block_threshold():
    try:
        return int(current_app.config.get("FIREWALL_AUTO_BLOCK_THRESHOLD", 12) or 12)
    except Exception:
        return 12


def firewall_window_minutes():
    try:
        return int(current_app.config.get("FIREWALL_AUTO_BLOCK_WINDOW_MINUTES", 30) or 30)
    except Exception:
        return 30


def ensure_firewall_schema(app):
    db_path = app.config.get("DATABASE_PATH")

    if not db_path:
        return

    conn = sqlite3.connect(db_path)

    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS firewall_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip_address TEXT,
                user_agent TEXT,
                method TEXT,
                path TEXT,
                query_string TEXT,
                account_role TEXT,
                account_id INTEGER,
                account_label TEXT,
                area_hint TEXT,
                attack_type TEXT,
                severity TEXT,
                action_taken TEXT,
                mitigation_hint TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS firewall_ip_blocks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip_address TEXT NOT NULL,
                reason TEXT,
                source TEXT,
                status TEXT DEFAULT 'ACTIVE',
                created_at TEXT,
                updated_at TEXT
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_firewall_events_ip_created ON firewall_events(ip_address, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_firewall_events_type_created ON firewall_events(attack_type, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_firewall_events_role_created ON firewall_events(account_role, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_firewall_events_area_created ON firewall_events(area_hint, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_firewall_ip_blocks_ip_status ON firewall_ip_blocks(ip_address, status)")
        conn.commit()
    finally:
        conn.close()


def init_firewall(app):
    @app.before_request
    def _firewall_monitor():
        if not firewall_enabled():
            return None

        if _skip_path(request.path):
            return None

        try:
            from db import get_db

            db = get_db()
            ip_address = get_client_ip()

            if ip_is_blocked(db, ip_address):
                log_firewall_event(
                    db,
                    attack_type="BLOCKED_IP",
                    severity="HIGH",
                    action_taken="BLOCKED",
                    commit=True,
                )
                return "Forbidden", 403

            attack_type, severity = detect_attack_type()

            if attack_type:
                action_taken = "LOGGED"

                if should_auto_block_ip(db, ip_address):
                    add_ip_block(
                        db,
                        ip_address,
                        reason=f"Auto-blocked after repeated suspicious activity: {attack_type}",
                        source="firewall_auto",
                        commit=False,
                    )
                    action_taken = "AUTO_BLOCKED"

                log_firewall_event(
                    db,
                    attack_type=attack_type,
                    severity=severity,
                    action_taken=action_taken,
                    commit=True,
                )
        except Exception as exc:
            print(f"[FIREWALL][ERROR] monitor failed: {exc}")

        return None


def _skip_path(path):
    path = path or ""
    return path.startswith("/static/") or path.startswith("/uploads/") or path in {"/health", "/health/db"}


def detect_attack_type():
    path = request.path or ""
    query = request.query_string.decode("utf-8", errors="ignore") if request.query_string else ""
    user_agent = get_user_agent()

    for attack_type, pattern in SUSPICIOUS_PATH_PATTERNS:
        if pattern.search(path):
            return attack_type, "HIGH"

    combined = f"{path}?{query}"

    for attack_type, pattern in SUSPICIOUS_QUERY_PATTERNS:
        if pattern.search(combined):
            return attack_type, "HIGH"

    for attack_type, pattern in SUSPICIOUS_UA_PATTERNS:
        if pattern.search(user_agent):
            return attack_type, "MEDIUM"

    if request.endpoint in {"auth.login_submit", "auth.register_submit", "auth.resend_email_verification"}:
        return "AUTH_ABUSE", "LOW"

    return "", ""


def current_account_context():
    role = (session.get("role") or "GUEST").strip().upper() or "GUEST"
    account_id = session.get("user_id")
    login_id = session.get("login_id") or ""
    display_name = session.get("display_name") or ""
    label = login_id or display_name or role

    if role not in {"CUSTOMER", "STORE", "DRIVER", "ADMIN_OPERATOR"}:
        role = "GUEST"

    return role, account_id, label


def request_area_hint():
    country = (request.headers.get("CF-IPCountry") or "").strip().upper()
    region = (request.headers.get("X-Geo-Region") or request.headers.get("X-Region") or "").strip()
    city = (request.headers.get("X-Geo-City") or request.headers.get("X-City") or "").strip()

    parts = [part for part in [country, region, city] if part]
    return ", ".join(parts) if parts else "UNKNOWN"


def mitigation_for(attack_type):
    return ATTACK_GUIDANCE.get(attack_type or "UNKNOWN", ATTACK_GUIDANCE["UNKNOWN"])


def log_firewall_event(db, *, attack_type, severity="LOW", action_taken="LOGGED", commit=True):
    role, account_id, account_label = current_account_context()

    if attack_type not in {"AUTH_ABUSE", "BLOCKED_IP"} and role == "GUEST":
        account_role = "HACKER"
    else:
        account_role = role

    db.execute(
        """
        INSERT INTO firewall_events (
            ip_address,
            user_agent,
            method,
            path,
            query_string,
            account_role,
            account_id,
            account_label,
            area_hint,
            attack_type,
            severity,
            action_taken,
            mitigation_hint,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', '+8 hours'))
        """,
        (
            get_client_ip(),
            get_user_agent(),
            request.method,
            request.path or "",
            request.query_string.decode("utf-8", errors="ignore") if request.query_string else "",
            account_role,
            account_id,
            account_label,
            request_area_hint(),
            attack_type or "UNKNOWN",
            severity or "LOW",
            action_taken or "LOGGED",
            mitigation_for(attack_type),
        ),
    )

    if commit:
        db.commit()


def ip_is_blocked(db, ip_address):
    ip_address = (ip_address or "").strip()

    if not ip_address:
        return False

    row = db.execute(
        """
        SELECT id
        FROM firewall_ip_blocks
        WHERE ip_address = ?
          AND status = 'ACTIVE'
        ORDER BY id DESC
        LIMIT 1
        """,
        (ip_address,),
    ).fetchone()

    return bool(row)


def add_ip_block(db, ip_address, *, reason="", source="admin_manual", commit=True):
    ip_address = (ip_address or "").strip()

    if not ip_address:
        return None

    if ip_is_blocked(db, ip_address):
        return ip_address

    db.execute(
        """
        INSERT INTO firewall_ip_blocks (
            ip_address,
            reason,
            source,
            status,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, 'ACTIVE', datetime('now', '+8 hours'), datetime('now', '+8 hours'))
        """,
        (ip_address, reason or "Blocked by firewall", source or "admin_manual"),
    )

    if commit:
        db.commit()

    return ip_address


def release_ip_block(db, ip_address, *, commit=True):
    ip_address = (ip_address or "").strip()

    if not ip_address:
        return 0

    cur = db.execute(
        """
        UPDATE firewall_ip_blocks
        SET status = 'RELEASED',
            updated_at = datetime('now', '+8 hours')
        WHERE ip_address = ?
          AND status = 'ACTIVE'
        """,
        (ip_address,),
    )

    if commit:
        db.commit()

    return int(cur.rowcount or 0)


def should_auto_block_ip(db, ip_address):
    if not firewall_auto_block_enabled():
        return False

    if not ip_address or ip_is_blocked(db, ip_address):
        return False

    since = (datetime.now() - timedelta(minutes=firewall_window_minutes())).isoformat(timespec="seconds")
    row = db.execute(
        """
        SELECT COUNT(*) AS c
        FROM firewall_events
        WHERE ip_address = ?
          AND severity IN ('MEDIUM', 'HIGH')
          AND created_at >= ?
        """,
        (ip_address, since),
    ).fetchone()

    count = int(row["c"] or 0) if row else 0
    return count + 1 >= firewall_auto_block_threshold()

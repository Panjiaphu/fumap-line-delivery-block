import imaplib
import re
import sqlite3
from email import message_from_bytes
from email.utils import parsedate_to_datetime

from flask import current_app

from services.abuse_guard import add_email_suppression
from services.email_service import normalize_email


EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
BOUNCE_KEYWORDS = [
    "delivery status notification",
    "mailer-daemon",
    "mail delivery subsystem",
    "undelivered mail",
    "delivery failure",
    "returned mail",
    "address not found",
    "recipient not found",
    "user unknown",
    "550",
    "5.1.1",
]


def ensure_bounce_schema(app):
    db_path = app.config.get("DATABASE_PATH")

    if not db_path:
        return

    conn = sqlite3.connect(db_path)

    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS email_bounce_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipient_email TEXT,
                message_subject TEXT,
                sender TEXT,
                message_date TEXT,
                bounce_reason TEXT,
                raw_excerpt TEXT,
                source TEXT,
                status TEXT DEFAULT 'PROCESSED',
                created_at TEXT
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_email_bounce_events_email ON email_bounce_events(recipient_email)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_email_bounce_events_created ON email_bounce_events(created_at)")
        conn.commit()
    finally:
        conn.close()


def bounce_imap_configured():
    return bool(
        current_app.config.get("BOUNCE_IMAP_ENABLED", False)
        and current_app.config.get("BOUNCE_IMAP_HOST")
        and current_app.config.get("BOUNCE_IMAP_USERNAME")
        and current_app.config.get("BOUNCE_IMAP_PASSWORD")
    )


def recent_bounces(db, limit=100):
    return db.execute(
        """
        SELECT *
        FROM email_bounce_events
        ORDER BY id DESC
        LIMIT ?
        """,
        (int(limit or 100),),
    ).fetchall()


def bounce_summary(db):
    row = db.execute(
        """
        SELECT COUNT(*) AS total_bounces,
               COUNT(DISTINCT lower(COALESCE(recipient_email, ''))) AS bounced_emails,
               MAX(created_at) AS latest_bounce_at
        FROM email_bounce_events
        """
    ).fetchone()

    return {
        "total_bounces": int(row["total_bounces"] or 0) if row else 0,
        "bounced_emails": int(row["bounced_emails"] or 0) if row else 0,
        "latest_bounce_at": row["latest_bounce_at"] if row else "",
    }


def sync_gmail_bounces(db):
    if not bounce_imap_configured():
        return {
            "ok": False,
            "checked": 0,
            "processed": 0,
            "suppressed": 0,
            "error": "BOUNCE_IMAP is not enabled or not configured",
        }

    host = current_app.config.get("BOUNCE_IMAP_HOST", "imap.gmail.com")
    port = int(current_app.config.get("BOUNCE_IMAP_PORT", 993) or 993)
    username = current_app.config.get("BOUNCE_IMAP_USERNAME", "")
    password = current_app.config.get("BOUNCE_IMAP_PASSWORD", "")
    mailbox = current_app.config.get("BOUNCE_IMAP_MAILBOX", "INBOX") or "INBOX"
    search_query = current_app.config.get("BOUNCE_IMAP_SEARCH", "UNSEEN") or "UNSEEN"
    max_messages = max(1, int(current_app.config.get("BOUNCE_IMAP_MAX_MESSAGES", 25) or 25))

    checked = 0
    processed = 0
    suppressed = 0

    try:
        with imaplib.IMAP4_SSL(host, port) as imap:
            imap.login(username, password)
            imap.select(mailbox)
            status, data = imap.search(None, search_query)

            if status != "OK" or not data:
                return {"ok": True, "checked": 0, "processed": 0, "suppressed": 0, "error": ""}

            ids = data[0].split()[-max_messages:]

            for msg_id in ids:
                checked += 1
                status, payload = imap.fetch(msg_id, "(RFC822)")

                if status != "OK" or not payload:
                    continue

                raw = payload[0][1]
                result = process_bounce_message(db, raw)

                if result.get("processed"):
                    processed += 1

                if result.get("suppressed"):
                    suppressed += 1

            db.commit()
            return {
                "ok": True,
                "checked": checked,
                "processed": processed,
                "suppressed": suppressed,
                "error": "",
            }
    except Exception as exc:
        db.rollback()
        return {
            "ok": False,
            "checked": checked,
            "processed": processed,
            "suppressed": suppressed,
            "error": str(exc),
        }


def process_bounce_message(db, raw_bytes):
    msg = message_from_bytes(raw_bytes)
    subject = str(msg.get("Subject", ""))[:300]
    sender = str(msg.get("From", ""))[:300]
    body = _message_text(msg)
    haystack = f"{subject}\n{sender}\n{body}".lower()

    if not _looks_like_bounce(haystack):
        return {"processed": False, "suppressed": False, "email": "", "reason": "not a bounce"}

    recipient = extract_bounced_recipient(msg, body)

    if not recipient:
        return {"processed": False, "suppressed": False, "email": "", "reason": "no recipient found"}

    reason = extract_bounce_reason(body)
    excerpt = body[:1200]
    message_date = _message_date(msg)

    db.execute(
        """
        INSERT INTO email_bounce_events (
            recipient_email,
            message_subject,
            sender,
            message_date,
            bounce_reason,
            raw_excerpt,
            source,
            status,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 'gmail_imap', 'PROCESSED', datetime('now', '+8 hours'))
        """,
        (
            recipient,
            subject,
            sender,
            message_date,
            reason,
            excerpt,
        ),
    )

    suppressed = False

    if current_app.config.get("BOUNCE_AUTO_SUPPRESS_ENABLED", True):
        add_email_suppression(
            db,
            recipient,
            reason=reason or "Gmail bounce detected",
            source="gmail_bounce",
            commit=False,
        )
        suppressed = True

    return {"processed": True, "suppressed": suppressed, "email": recipient, "reason": reason}


def extract_bounced_recipient(msg, body):
    candidates = []

    for header in ["Final-Recipient", "Original-Recipient", "X-Failed-Recipients"]:
        value = str(msg.get(header, ""))
        candidates.extend(EMAIL_RE.findall(value))

    for marker in ["Final-Recipient:", "Original-Recipient:", "X-Failed-Recipients:"]:
        for line in body.splitlines():
            if marker.lower() in line.lower():
                candidates.extend(EMAIL_RE.findall(line))

    if not candidates:
        candidates.extend(EMAIL_RE.findall(body[:3000]))

    for candidate in candidates:
        email = normalize_email(candidate)

        if email and not email.startswith("mailer-daemon@") and "@" in email:
            return email

    return ""


def extract_bounce_reason(body):
    lowered = body.lower()

    for keyword in [
        "address not found",
        "recipient not found",
        "user unknown",
        "mailbox unavailable",
        "mailbox disabled",
        "550",
        "5.1.1",
        "5.2.1",
    ]:
        if keyword in lowered:
            return f"Gmail bounce: {keyword}"

    return "Gmail bounce detected"


def _looks_like_bounce(text):
    return any(keyword in text for keyword in BOUNCE_KEYWORDS)


def _message_text(msg):
    parts = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue

            content_type = part.get_content_type()

            if content_type not in {"text/plain", "message/delivery-status"}:
                continue

            try:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                parts.append(payload.decode(charset, errors="replace"))
            except Exception:
                continue
    else:
        try:
            payload = msg.get_payload(decode=True) or b""
            charset = msg.get_content_charset() or "utf-8"
            parts.append(payload.decode(charset, errors="replace"))
        except Exception:
            pass

    return "\n".join(parts)


def _message_date(msg):
    raw = str(msg.get("Date", ""))

    if not raw:
        return ""

    try:
        return parsedate_to_datetime(raw).isoformat()
    except Exception:
        return raw[:80]

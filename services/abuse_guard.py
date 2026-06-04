import os
import sqlite3
from datetime import datetime
from urllib.parse import quote

from flask import current_app, flash, redirect, request, session

try:
    from flask_limiter import Limiter
    from flask_limiter.errors import RateLimitExceeded
    from flask_limiter.util import get_remote_address
except Exception:
    Limiter = None
    RateLimitExceeded = None

    def get_remote_address():
        return request.remote_addr or "unknown"


DISPOSABLE_EMAIL_DOMAINS = {
    "10minutemail.com",
    "guerrillamail.com",
    "mailinator.com",
    "tempmail.com",
    "temp-mail.org",
    "throwawaymail.com",
    "yopmail.com",
}

EMAIL_VERIFICATION_EVENT = "EMAIL_VERIFICATION_REQUESTED"
TURNSTILE_SESSION_TTL_SECONDS = 900


def _limiter_key():
    return get_client_ip() or get_remote_address() or "unknown"


def _limiter_storage_uri():
    return (
        os.getenv("RATELIMIT_STORAGE_URI", "").strip()
        or os.getenv("REDIS_URL", "").strip()
        or "memory://"
    )


limiter = Limiter(
    key_func=_limiter_key,
    default_limits=[],
    storage_uri=_limiter_storage_uri(),
) if Limiter else None


def rate_limit(limit_value):
    if limiter:
        return limiter.limit(limit_value)

    def decorator(func):
        return func

    return decorator


def apply_abuse_route_limits(app):
    if not limiter:
        return

    endpoint_limits = {
        "auth.register_submit": app.config.get("REGISTER_RATE_LIMIT", "3 per hour"),
        "auth.login_submit": app.config.get("LOGIN_RATE_LIMIT", "10 per 10 minutes"),
        "auth.resend_email_verification": app.config.get(
            "VERIFY_RESEND_RATE_LIMIT",
            "1 per 10 minutes",
        ),
    }

    for endpoint, limit_value in endpoint_limits.items():
        view = app.view_functions.get(endpoint)

        if view and limit_value:
            app.view_functions[endpoint] = limiter.limit(limit_value)(view)


def init_abuse_guards(app):
    if limiter:
        limiter.init_app(app)

    @app.before_request
    def _guard_public_auth_abuse():
        if request.method != "POST":
            return None

        if request.endpoint == "auth.register_submit":
            role = request.form.get("role", "CUSTOMER")
            email = (request.form.get("email") or "").strip().lower()

            if not current_app.config.get("REGISTER_ENABLED", True):
                flash("目前暫停公開註冊，請聯絡 FUMAP GO 團隊。", "warning")
                return redirect(f"/register?role={role}")

            if honeypot_triggered(request.form):
                flash("註冊失敗，請稍後再試。", "danger")
                return redirect(f"/register?role={role}")

            if not invite_code_valid(request.form.get("invite_code", "")):
                flash("註冊碼不正確或已停用，請聯絡 FUMAP GO 團隊。", "danger")
                return redirect(f"/register?role={role}")

            ok, _message = verify_turnstile_for_action("register")
            if not ok:
                flash("請完成安全驗證後再註冊。", "danger")
                return redirect(f"/register?role={role}")

            if email and is_disposable_email(email):
                flash("此 Email 網域目前不支援註冊，請使用常用 Email。", "danger")
                return redirect(f"/register?role={role}")

            if email:
                from db import get_db

                db = get_db()
                suppressed, _reason = should_suppress_verification_email(db, email)

                if suppressed:
                    flash("此 Email 目前無法接收系統信，請使用其他 Email。", "danger")
                    return redirect(f"/register?role={role}")

                cooldown_seconds = int(
                    current_app.config.get("VERIFY_EMAIL_COOLDOWN_SECONDS", 600)
                )
                on_cooldown, _remain = verification_email_on_cooldown(
                    db,
                    email,
                    cooldown_seconds,
                )

                if on_cooldown:
                    flash("確認信寄送太頻繁，請稍後再試。", "warning")
                    return redirect(f"/register?role={role}")

        if request.endpoint == "auth.login_submit":
            ok, _message = verify_turnstile_for_action("login")
            if not ok:
                flash("請完成安全驗證後再登入。", "danger")
                return redirect("/login")

        if request.endpoint == "auth.resend_email_verification":
            ok, _message = verify_turnstile_for_action("resend")
            if not ok:
                flash("請完成安全驗證後再重新寄送確認信。", "danger")
                return redirect(request.referrer or "/account/email/resend")

            try:
                from db import get_db
                from services.permission_service import current_user

                db = get_db()
                user = current_user()
                email = ""

                if user and "email" in user.keys():
                    email = (user["email"] or "").strip().lower()
            except Exception:
                db = None
                email = ""

            if db and email:
                suppressed, _reason = should_suppress_verification_email(db, email)

                if suppressed:
                    flash("此 Email 目前無法接收系統信，請使用其他 Email。", "warning")
                    return redirect(request.referrer or "/")

                cooldown_seconds = int(
                    current_app.config.get("VERIFY_EMAIL_COOLDOWN_SECONDS", 600)
                )
                on_cooldown, remain = verification_email_on_cooldown(
                    db,
                    email,
                    cooldown_seconds,
                )

                if on_cooldown:
                    flash(f"請稍候 {remain} 秒後再重新寄送。", "warning")
                    return redirect(request.referrer or "/")

        return None

    @app.before_request
    def _block_unverified_customer_checkout():
        if request.method != "POST":
            return None

        if request.endpoint != "customer.checkout":
            return None

        checkout_guard = require_turnstile_session_or_redirect("checkout")
        if checkout_guard:
            return checkout_guard

        if not current_app.config.get("REQUIRE_VERIFIED_EMAIL_FOR_CUSTOMER_ORDER", True):
            return None

        if session.get("role") != "CUSTOMER":
            return None

        try:
            from services.permission_service import current_user

            user = current_user()
        except Exception:
            user = None

        email_verified = False

        try:
            email_verified = bool(user and user["email_verified_at"])
        except Exception:
            email_verified = False

        if not email_verified:
            flash("請先完成 Email 驗證後，才能建立訂單。", "danger")
            return redirect(request.path)

        return None

    if RateLimitExceeded:
        @app.errorhandler(RateLimitExceeded)
        def _rate_limit_exceeded(exc):
            flash("操作太頻繁，請稍後再試。", "warning")
            return redirect(request.referrer or "/login")


def ensure_abuse_schema(app):
    db_path = app.config.get("DATABASE_PATH")

    if not db_path:
        return

    user_columns = {
        "register_ip": "register_ip TEXT",
        "user_agent": "user_agent TEXT",
        "risk_score": "risk_score INTEGER DEFAULT 0",
        "verify_send_count": "verify_send_count INTEGER DEFAULT 0",
        "last_verify_sent_at": "last_verify_sent_at TEXT",
    }

    conn = sqlite3.connect(db_path)

    try:
        existing = {
            row[1]
            for row in conn.execute("PRAGMA table_info(users)").fetchall()
        }

        for name, column_sql in user_columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE users ADD COLUMN {column_sql}")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS email_suppressions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                reason TEXT,
                source TEXT,
                status TEXT DEFAULT 'ACTIVE',
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_email_suppressions_email_status ON email_suppressions(email, status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_email_suppressions_created_at ON email_suppressions(created_at)"
        )

        conn.commit()
    finally:
        conn.close()


def get_client_ip():
    forwarded_for = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    real_ip = (request.headers.get("X-Real-IP") or "").strip()
    return forwarded_for or real_ip or request.remote_addr or ""


def get_user_agent():
    return (request.headers.get("User-Agent") or "")[:500]


def register_enabled():
    return bool(current_app.config.get("REGISTER_ENABLED", True))


def honeypot_triggered(form):
    for field in current_app.config.get("REGISTER_HONEYPOT_FIELDS", ["website", "company_website"]):
        if (form.get(field) or "").strip():
            return True

    return False


def invite_code_valid(raw_code):
    if not current_app.config.get("REGISTER_REQUIRE_INVITE_CODE", False):
        return True

    configured = current_app.config.get("REGISTER_INVITE_CODES", "")
    allowed = {
        item.strip()
        for item in str(configured or "").split(",")
        if item.strip()
    }

    if not allowed:
        return False

    return (raw_code or "").strip() in allowed


def verify_turnstile_for_action(action):
    try:
        from services.turnstile_service import verify_turnstile_request

        return verify_turnstile_request(action, remote_ip=get_client_ip())
    except Exception as exc:
        return False, str(exc)


def turnstile_session_is_valid(action):
    try:
        value = session.get(f"turnstile_{action}_verified_at", "")
        verified_at = _parse_db_time(value)

        if not verified_at:
            return False

        elapsed = (datetime.now() - verified_at).total_seconds()
        return 0 <= elapsed <= TURNSTILE_SESSION_TTL_SECONDS
    except Exception:
        return False


def require_turnstile_session_or_redirect(action):
    try:
        from services.turnstile_service import turnstile_enabled_for

        if not turnstile_enabled_for(action):
            return None
    except Exception:
        return None

    ok, _message = verify_turnstile_for_action(action)

    if ok:
        session[f"turnstile_{action}_verified_at"] = datetime.now().isoformat(timespec="seconds")
        return None

    if turnstile_session_is_valid(action):
        return None

    flash("請先完成安全驗證，再重新送出訂單。", "warning")
    next_url = quote(request.path or "/show", safe="/")
    return redirect(f"/security/turnstile/{action}?next={next_url}")


def blocked_email_domains():
    configured = current_app.config.get("BLOCKED_EMAIL_DOMAINS", "")
    blocked = set(DISPOSABLE_EMAIL_DOMAINS)

    for item in str(configured or "").split(","):
        domain = item.strip().lower()
        if domain:
            blocked.add(domain)

    return blocked


def email_domain(email):
    email = (email or "").strip().lower()

    if "@" not in email:
        return ""

    return email.rsplit("@", 1)[-1]


def is_disposable_email(email):
    domain = email_domain(email)
    return bool(domain and domain in blocked_email_domains())


def _parse_db_time(value):
    try:
        return datetime.fromisoformat((value or "").replace("Z", ""))
    except Exception:
        return None


def recent_verification_send_seconds(db, email):
    email = (email or "").strip().lower()

    if not email:
        return None

    row = db.execute(
        """
        SELECT created_at, last_attempt_at
        FROM email_logs
        WHERE lower(COALESCE(recipient_email, '')) = ?
          AND event_type = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (email, EMAIL_VERIFICATION_EVENT),
    ).fetchone()

    if not row:
        return None

    sent_at = _parse_db_time(row["last_attempt_at"] or row["created_at"])

    if not sent_at:
        return None

    return max(0, int((datetime.now() - sent_at).total_seconds()))


def verification_email_on_cooldown(db, email, cooldown_seconds):
    elapsed = recent_verification_send_seconds(db, email)

    if elapsed is None or elapsed >= int(cooldown_seconds or 0):
        return False, 0

    return True, int(cooldown_seconds) - elapsed


def email_is_suppressed(db, email):
    email = (email or "").strip().lower()

    if not email:
        return False, ""

    try:
        row = db.execute(
            """
            SELECT reason
            FROM email_suppressions
            WHERE lower(COALESCE(email, '')) = ?
              AND status = 'ACTIVE'
            ORDER BY id DESC
            LIMIT 1
            """,
            (email,),
        ).fetchone()
    except Exception:
        return False, ""

    if not row:
        return False, ""

    return True, row["reason"] or "Email is suppressed"


def add_email_suppression(db, email, *, reason="", source="manual", commit=True):
    email = (email or "").strip().lower()

    if not email:
        return None

    existing, _reason = email_is_suppressed(db, email)

    if existing:
        return None

    db.execute(
        """
        INSERT INTO email_suppressions (
            email,
            reason,
            source,
            status,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, 'ACTIVE', datetime('now', '+8 hours'), datetime('now', '+8 hours'))
        """,
        (email, reason or "Suppressed", source or "manual"),
    )

    if commit:
        db.commit()

    return email


def release_email_suppression(db, email, *, commit=True):
    email = (email or "").strip().lower()

    if not email:
        return 0

    cur = db.execute(
        """
        UPDATE email_suppressions
        SET status = 'RELEASED',
            updated_at = datetime('now', '+8 hours')
        WHERE lower(COALESCE(email, '')) = ?
          AND status = 'ACTIVE'
        """,
        (email,),
    )

    if commit:
        db.commit()

    return int(cur.rowcount or 0)


def maybe_auto_suppress_failed_email(db, email, *, source="email_failure", commit=True):
    email = (email or "").strip().lower()

    if not email:
        return False

    failed_limit = int(current_app.config.get("EMAIL_FAILED_SUPPRESSION_COUNT", 2))

    if failed_limit <= 0:
        return False

    row = db.execute(
        """
        SELECT COUNT(*) AS failed_count
        FROM email_logs
        WHERE lower(COALESCE(recipient_email, '')) = ?
          AND status = 'FAILED'
        """,
        (email,),
    ).fetchone()

    failed_count = int(row["failed_count"] or 0) if row else 0

    if failed_count < failed_limit:
        return False

    add_email_suppression(
        db,
        email,
        reason=f"Auto-suppressed after {failed_count} failed sends",
        source=source,
        commit=False,
    )

    if commit:
        db.commit()

    return True


def should_suppress_verification_email(db, email):
    email = (email or "").strip().lower()

    if not email:
        return True, "Missing recipient email"

    if is_disposable_email(email):
        return True, "Disposable email domain"

    suppressed, reason = email_is_suppressed(db, email)

    if suppressed:
        return True, reason

    failed_limit = int(current_app.config.get("EMAIL_FAILED_SUPPRESSION_COUNT", 2))

    row = db.execute(
        """
        SELECT COUNT(*) AS failed_count
        FROM email_logs
        WHERE lower(COALESCE(recipient_email, '')) = ?
          AND status = 'FAILED'
        """,
        (email,),
    ).fetchone()

    failed_count = int(row["failed_count"] or 0) if row else 0

    if failed_count >= failed_limit:
        add_email_suppression(
            db,
            email,
            reason=f"Auto-suppressed after {failed_count} failed sends",
            source="failed_email_count",
            commit=True,
        )
        return True, "Recipient has repeated failed email sends"

    return False, ""

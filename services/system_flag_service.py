from flask import current_app

from services.code_service import now_iso
from services.block_service import create_block


RAIN_SURCHARGE_KEY = "RAIN_SURCHARGE_ENABLED"


def normalize_flag_value(value) -> str:
    if value in {True, 1, "1", "true", "TRUE", "on", "ON", "yes", "YES"}:
        return "1"
    return "0"


def get_flag(db, key: str, default: str = "0") -> str:
    key = (key or "").strip().upper()

    if not key:
        return default

    row = db.execute(
        """
        SELECT flag_value
        FROM system_flags
        WHERE flag_key = ?
        LIMIT 1
        """,
        (key,),
    ).fetchone()

    if not row:
        return default

    return str(row["flag_value"] or default)


def set_flag(db, key: str, value, *, actor_role="SYSTEM", actor_code="SYSTEM", commit=True):
    key = (key or "").strip().upper()

    if not key:
        raise ValueError("flag_key required")

    flag_value = normalize_flag_value(value)
    now = now_iso()

    existing = db.execute(
        """
        SELECT *
        FROM system_flags
        WHERE flag_key = ?
        LIMIT 1
        """,
        (key,),
    ).fetchone()

    previous_value = existing["flag_value"] if existing else ""

    if existing:
        db.execute(
            """
            UPDATE system_flags
            SET flag_value = ?,
                updated_at = ?
            WHERE flag_key = ?
            """,
            (flag_value, now, key),
        )
    else:
        db.execute(
            """
            INSERT INTO system_flags (
                flag_key,
                flag_value,
                updated_at
            )
            VALUES (?, ?, ?)
            """,
            (key, flag_value, now),
        )

    create_block(
        db,
        event_type="SYSTEM_FLAG_UPDATED",
        actor_role=actor_role,
        actor_code=actor_code,
        previous_status=previous_value,
        new_status=flag_value,
        payload={
            "flag_key": key,
            "previous_value": previous_value,
            "new_value": flag_value,
        },
        commit=False,
    )

    if commit:
        db.commit()

    return flag_value


def is_flag_enabled(db, key: str) -> bool:
    return get_flag(db, key, "0") == "1"


def is_rain_surcharge_enabled(db) -> bool:
    return is_flag_enabled(db, RAIN_SURCHARGE_KEY)


def set_rain_surcharge_enabled(db, enabled, *, actor_role="ADMIN_OPERATOR", actor_code="ADMIN", commit=True):
    return set_flag(
        db,
        RAIN_SURCHARGE_KEY,
        "1" if enabled else "0",
        actor_role=actor_role,
        actor_code=actor_code,
        commit=commit,
    )


def get_platform_payment_info():
    """
    Read platform BANK_TRANSFER information from Render ENV via Flask config.

    Do not hardcode bank info in templates.
    """
    return {
        "line_admin_url": current_app.config.get("LINE_ADMIN_URL", ""),
        "bank_name": current_app.config.get("PLATFORM_BANK_NAME", ""),
        "bank_code": current_app.config.get("PLATFORM_BANK_CODE", ""),
        "bank_account": current_app.config.get("PLATFORM_BANK_ACCOUNT", ""),
        "bank_note": current_app.config.get("PLATFORM_BANK_NOTE", ""),
        "linepay_name": current_app.config.get("PLATFORM_LINEPAY_NAME", "平台銀行轉帳"),
        "linepay_qr_url": current_app.config.get("PLATFORM_LINEPAY_QR_URL", ""),
        "payment_account": current_app.config.get("PLATFORM_PAYMENT_ACCOUNT", ""),
    }


def has_platform_payment_info() -> bool:
    info = get_platform_payment_info()

    required = [
        info.get("bank_name"),
        info.get("bank_code"),
        info.get("bank_account"),
        info.get("line_admin_url"),
    ]

    return all(bool(str(x or "").strip()) for x in required)


def get_commercial_flags(db):
    return {
        "rain_surcharge_enabled": is_rain_surcharge_enabled(db),
    }

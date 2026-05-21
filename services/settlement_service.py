import json
import os
import uuid
from datetime import datetime

from services.accounting_service import calculate_order_settlement
from services.code_service import now_iso


VALID_ROLES = {"STORE", "DRIVER"}
VALID_DIRECTIONS = {"TARGET_OWES_ADMIN", "ADMIN_OWES_TARGET"}
VALID_SETTLEMENT_TYPES = {
    "STORE_PLATFORM_FEE",
    "DRIVER_PLATFORM_FEE",
    "ADMIN_PAYOUT_STORE",
    "ADMIN_PAYOUT_DRIVER",
}
VALID_STATUSES = {
    "DRAFT",
    "EMAIL_SENT",
    "PAID_CONFIRMED",
    "CANCELLED",
    "DISPUTED",
}
VALID_PAYMENT_METHODS = {
    "BANK_TRANSFER",
    "LINEPAY",
    "CASH",
    "OTHER",
}


def _row_get(row, key, default=None):
    try:
        if row is None:
            return default
        if hasattr(row, "keys") and key in row.keys():
            return row[key]
        if isinstance(row, dict):
            return row.get(key, default)
    except Exception:
        pass

    return default


def _row_to_dict(row):
    if row is None:
        return {}

    try:
        return {k: row[k] for k in row.keys()}
    except Exception:
        if isinstance(row, dict):
            return dict(row)

    return {}


def _text(value, default=""):
    try:
        value = str(value if value is not None else default).strip()
        return value if value else default
    except Exception:
        return default


def _int(value, default=0):
    try:
        return int(value or 0)
    except Exception:
        return int(default or 0)


def _money(value):
    return max(0, _int(value, 0))


def _json_dumps(data):
    return json.dumps(data or {}, ensure_ascii=False, sort_keys=True)


def _json_loads(value, default=None):
    default = default if default is not None else {}

    try:
        if not value:
            return default
        return json.loads(value)
    except Exception:
        return default


def _today_date():
    return now_iso()[:10]


def current_month_range():
    today = _today_date()
    year = int(today[:4])
    month = int(today[5:7])

    period_start = f"{year:04d}-{month:02d}-01"

    if month == 12:
        period_end = f"{year + 1:04d}-01-01"
    else:
        period_end = f"{year:04d}-{month + 1:02d}-01"

    return period_start, period_end


def _date_text(value):
    value = _text(value)

    if len(value) >= 10:
        return value[:10]

    return ""


def _date_in_range(value, period_start=None, period_end=None):
    date_value = _date_text(value)

    if not date_value:
        return False

    if period_start and date_value < period_start:
        return False

    if period_end and date_value >= period_end:
        return False

    return True


def _settlement_code():
    return "SET-" + uuid.uuid4().hex[:12].upper()


def _table_columns(db, table_name):
    try:
        rows = db.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {r["name"] for r in rows}
    except Exception:
        return set()


def _add_column_if_missing(db, table_name, column_name, column_sql):
    columns = _table_columns(db, table_name)

    if column_name not in columns:
        db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}")


def ensure_settlement_tables(db):
    """
    Safe runtime guard for Admin Settlement V1.

    db.py already creates these fields. This service also keeps a defensive
    ensure function so routes can call it safely before settlement operations.
    """
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS settlement_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT
        )
        """
    )

    columns = [
        ("settlement_code", "settlement_code TEXT"),
        ("role", "role TEXT"),
        ("target_code", "target_code TEXT"),
        ("target_user_id", "target_user_id INTEGER"),
        ("target_email", "target_email TEXT"),
        ("direction", "direction TEXT"),
        ("settlement_type", "settlement_type TEXT"),
        ("period_start", "period_start TEXT"),
        ("period_end", "period_end TEXT"),
        ("amount_twd", "amount_twd INTEGER DEFAULT 0"),
        ("status", "status TEXT DEFAULT 'DRAFT'"),
        ("email_sent_at", "email_sent_at TEXT"),
        ("paid_confirmed_at", "paid_confirmed_at TEXT"),
        ("paid_confirmed_by", "paid_confirmed_by INTEGER"),
        ("payment_method", "payment_method TEXT DEFAULT 'BANK_TRANSFER'"),
        ("admin_bank_snapshot_json", "admin_bank_snapshot_json TEXT"),
        ("target_payout_snapshot_json", "target_payout_snapshot_json TEXT"),
        ("related_order_codes_json", "related_order_codes_json TEXT"),
        ("note", "note TEXT"),
        ("created_at", "created_at TEXT"),
        ("updated_at", "updated_at TEXT"),
    ]

    for column_name, column_sql in columns:
        _add_column_if_missing(db, "settlement_batches", column_name, column_sql)

    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_settlement_batches_code "
        "ON settlement_batches(settlement_code)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_settlement_batches_role_target "
        "ON settlement_batches(role, target_code)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_settlement_batches_direction_type "
        "ON settlement_batches(direction, settlement_type)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_settlement_batches_status "
        "ON settlement_batches(status)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_settlement_batches_period "
        "ON settlement_batches(period_start, period_end)"
    )

    for table_name in ("stores", "drivers"):
        for column_name, column_sql in [
            ("payout_bank_name", "payout_bank_name TEXT"),
            ("payout_bank_code", "payout_bank_code TEXT"),
            ("payout_bank_account", "payout_bank_account TEXT"),
            ("payout_account_name", "payout_account_name TEXT"),
            ("payout_note", "payout_note TEXT"),
        ]:
            _add_column_if_missing(db, table_name, column_name, column_sql)


def snapshot_admin_payment_info():
    return {
        "bank_name": os.getenv("PLATFORM_BANK_NAME", "").strip(),
        "bank_code": os.getenv("PLATFORM_BANK_CODE", "").strip(),
        "bank_account": os.getenv("PLATFORM_BANK_ACCOUNT", "").strip(),
        "bank_note": os.getenv("PLATFORM_BANK_NOTE", "").strip(),
        "linepay_name": os.getenv("PLATFORM_LINEPAY_NAME", "").strip(),
        "linepay_qr_url": os.getenv("PLATFORM_LINEPAY_QR_URL", "").strip(),
    }


def snapshot_target_payout_account(target):
    return {
        "payout_account_name": _text(_row_get(target, "payout_account_name", "")),
        "payout_bank_name": _text(_row_get(target, "payout_bank_name", "")),
        "payout_bank_code": _text(_row_get(target, "payout_bank_code", "")),
        "payout_bank_account": _text(_row_get(target, "payout_bank_account", "")),
        "payout_note": _text(_row_get(target, "payout_note", "")),
    }


def payout_account_is_complete(target):
    snapshot = snapshot_target_payout_account(target)

    return bool(
        snapshot["payout_account_name"]
        and snapshot["payout_bank_name"]
        and snapshot["payout_bank_account"]
    )


def _paid_settlement_total(
    db,
    *,
    role,
    target_code,
    direction,
    settlement_type,
    period_start=None,
    period_end=None,
):
    ensure_settlement_tables(db)

    sql = """
        SELECT COALESCE(SUM(amount_twd), 0) AS total
        FROM settlement_batches
        WHERE role = ?
          AND target_code = ?
          AND direction = ?
          AND settlement_type = ?
          AND status = 'PAID_CONFIRMED'
    """
    params = [
        role,
        target_code,
        direction,
        settlement_type,
    ]

    if period_start:
        sql += " AND COALESCE(period_end, '') > ?"
        params.append(period_start)

    if period_end:
        sql += " AND COALESCE(period_start, '') < ?"
        params.append(period_end)

    row = db.execute(sql, params).fetchone()
    return _money(_row_get(row, "total", 0))


def _open_settlement_total(
    db,
    *,
    role,
    target_code,
    direction,
    settlement_type,
):
    ensure_settlement_tables(db)

    row = db.execute(
        """
        SELECT COALESCE(SUM(amount_twd), 0) AS total
        FROM settlement_batches
        WHERE role = ?
          AND target_code = ?
          AND direction = ?
          AND settlement_type = ?
          AND status IN ('DRAFT', 'EMAIL_SENT')
        """,
        (
            role,
            target_code,
            direction,
            settlement_type,
        ),
    ).fetchone()

    return _money(_row_get(row, "total", 0))


def _latest_open_settlement(
    db,
    *,
    role,
    target_code,
    direction,
    settlement_type,
):
    ensure_settlement_tables(db)

    return db.execute(
        """
        SELECT *
        FROM settlement_batches
        WHERE role = ?
          AND target_code = ?
          AND direction = ?
          AND settlement_type = ?
          AND status IN ('DRAFT', 'EMAIL_SENT')
        ORDER BY id DESC
        LIMIT 1
        """,
        (
            role,
            target_code,
            direction,
            settlement_type,
        ),
    ).fetchone()


def _store_orders(db, store_id):
    return db.execute(
        """
        SELECT *
        FROM orders
        WHERE store_id = ?
          AND status IN ('DELIVERED', 'COMPLETED')
        ORDER BY id DESC
        LIMIT 5000
        """,
        (_int(store_id),),
    ).fetchall()


def _driver_orders(db, driver_id):
    return db.execute(
        """
        SELECT *
        FROM orders
        WHERE driver_id = ?
          AND status IN ('DELIVERED', 'COMPLETED')
        ORDER BY id DESC
        LIMIT 5000
        """,
        (_int(driver_id),),
    ).fetchall()


def _payment_is_platform_held(order):
    payment_method = _text(_row_get(order, "payment_method", "")).upper()
    payment_status = _text(_row_get(order, "payment_status", "")).upper()

    if payment_method in {"BANK_TRANSFER", "PLATFORM"} and payment_status == "PAID":
        return True

    return False


def calculate_store_unpaid_admin_debt(
    db,
    store,
    period_start=None,
    period_end=None,
):
    """
    Store 欠 Admin:
    completed order store platform fee - PAID_CONFIRMED store settlements.
    """
    store_id = _int(_row_get(store, "id", 0))
    store_code = _text(_row_get(store, "store_code", ""))

    gross = 0
    related_order_codes = []

    for order in _store_orders(db, store_id):
        updated_at = _text(_row_get(order, "updated_at", _row_get(order, "created_at", "")))

        if period_start or period_end:
            if not _date_in_range(updated_at, period_start, period_end):
                continue

        settlement = calculate_order_settlement(order)
        gross += settlement["store_platform_fee_twd"]
        related_order_codes.append(_text(_row_get(order, "order_code", "")))

    paid = _paid_settlement_total(
        db,
        role="STORE",
        target_code=store_code,
        direction="TARGET_OWES_ADMIN",
        settlement_type="STORE_PLATFORM_FEE",
        period_start=period_start,
        period_end=period_end,
    )

    open_amount = _open_settlement_total(
        db,
        role="STORE",
        target_code=store_code,
        direction="TARGET_OWES_ADMIN",
        settlement_type="STORE_PLATFORM_FEE",
    )

    return {
        "gross_twd": gross,
        "paid_twd": paid,
        "open_twd": open_amount,
        "unpaid_twd": max(0, gross - paid),
        "available_to_settle_twd": max(0, gross - paid - open_amount),
        "related_order_codes": [c for c in related_order_codes if c],
    }


def calculate_driver_unpaid_admin_debt(
    db,
    driver,
    period_start=None,
    period_end=None,
):
    """
    Shiper 欠 Admin:
    completed order driver platform fee - PAID_CONFIRMED driver settlements.
    """
    driver_id = _int(_row_get(driver, "id", 0))
    driver_code = _text(_row_get(driver, "driver_code", ""))

    gross = 0
    related_order_codes = []

    for order in _driver_orders(db, driver_id):
        updated_at = _text(_row_get(order, "updated_at", _row_get(order, "created_at", "")))

        if period_start or period_end:
            if not _date_in_range(updated_at, period_start, period_end):
                continue

        settlement = calculate_order_settlement(order)
        gross += settlement["driver_platform_fee_twd"]
        related_order_codes.append(_text(_row_get(order, "order_code", "")))

    paid = _paid_settlement_total(
        db,
        role="DRIVER",
        target_code=driver_code,
        direction="TARGET_OWES_ADMIN",
        settlement_type="DRIVER_PLATFORM_FEE",
        period_start=period_start,
        period_end=period_end,
    )

    open_amount = _open_settlement_total(
        db,
        role="DRIVER",
        target_code=driver_code,
        direction="TARGET_OWES_ADMIN",
        settlement_type="DRIVER_PLATFORM_FEE",
    )

    return {
        "gross_twd": gross,
        "paid_twd": paid,
        "open_twd": open_amount,
        "unpaid_twd": max(0, gross - paid),
        "available_to_settle_twd": max(0, gross - paid - open_amount),
        "related_order_codes": [c for c in related_order_codes if c],
    }


def calculate_admin_owes_store(
    db,
    store,
    period_start=None,
    period_end=None,
):
    """
    Admin 欠 Store:
    platform-held completed orders payable to store - paid payout settlements.

    V1 only counts clearly platform-held payments:
    BANK_TRANSFER / PLATFORM with payment_status PAID.
    COD is excluded because Shiper pays store directly.
    """
    store_id = _int(_row_get(store, "id", 0))
    store_code = _text(_row_get(store, "store_code", ""))

    gross = 0
    related_order_codes = []

    for order in _store_orders(db, store_id):
        if not _payment_is_platform_held(order):
            continue

        updated_at = _text(_row_get(order, "updated_at", _row_get(order, "created_at", "")))

        if period_start or period_end:
            if not _date_in_range(updated_at, period_start, period_end):
                continue

        settlement = calculate_order_settlement(order)
        gross += settlement["store_net_after_settlement_twd"]
        related_order_codes.append(_text(_row_get(order, "order_code", "")))

    paid = _paid_settlement_total(
        db,
        role="STORE",
        target_code=store_code,
        direction="ADMIN_OWES_TARGET",
        settlement_type="ADMIN_PAYOUT_STORE",
        period_start=period_start,
        period_end=period_end,
    )

    open_amount = _open_settlement_total(
        db,
        role="STORE",
        target_code=store_code,
        direction="ADMIN_OWES_TARGET",
        settlement_type="ADMIN_PAYOUT_STORE",
    )

    return {
        "gross_twd": gross,
        "paid_twd": paid,
        "open_twd": open_amount,
        "unpaid_twd": max(0, gross - paid),
        "available_to_settle_twd": max(0, gross - paid - open_amount),
        "related_order_codes": [c for c in related_order_codes if c],
    }


def calculate_admin_owes_driver(
    db,
    driver,
    period_start=None,
    period_end=None,
):
    """
    Admin 欠 Shiper:
    platform-held completed orders payable to driver - paid payout settlements.

    V1 only counts clearly platform-held payments:
    BANK_TRANSFER / PLATFORM with payment_status PAID.
    COD is excluded because Shiper collects cash directly.
    """
    driver_id = _int(_row_get(driver, "id", 0))
    driver_code = _text(_row_get(driver, "driver_code", ""))

    gross = 0
    related_order_codes = []

    for order in _driver_orders(db, driver_id):
        if not _payment_is_platform_held(order):
            continue

        updated_at = _text(_row_get(order, "updated_at", _row_get(order, "created_at", "")))

        if period_start or period_end:
            if not _date_in_range(updated_at, period_start, period_end):
                continue

        settlement = calculate_order_settlement(order)
        gross += settlement["driver_net_income_twd"]
        related_order_codes.append(_text(_row_get(order, "order_code", "")))

    paid = _paid_settlement_total(
        db,
        role="DRIVER",
        target_code=driver_code,
        direction="ADMIN_OWES_TARGET",
        settlement_type="ADMIN_PAYOUT_DRIVER",
        period_start=period_start,
        period_end=period_end,
    )

    open_amount = _open_settlement_total(
        db,
        role="DRIVER",
        target_code=driver_code,
        direction="ADMIN_OWES_TARGET",
        settlement_type="ADMIN_PAYOUT_DRIVER",
    )

    return {
        "gross_twd": gross,
        "paid_twd": paid,
        "open_twd": open_amount,
        "unpaid_twd": max(0, gross - paid),
        "available_to_settle_twd": max(0, gross - paid - open_amount),
        "related_order_codes": [c for c in related_order_codes if c],
    }


def get_store_with_user(db, store_code):
    return db.execute(
        """
        SELECT s.*,
               u.id AS user_id,
               u.email AS email,
               u.email_verified_at AS email_verified_at,
               u.display_name AS user_display_name
        FROM stores s
        LEFT JOIN users u ON u.id = s.owner_user_id
        WHERE s.store_code = ?
        LIMIT 1
        """,
        (_text(store_code),),
    ).fetchone()


def get_driver_with_user(db, driver_code):
    return db.execute(
        """
        SELECT d.*,
               u.id AS user_id,
               u.email AS email,
               u.email_verified_at AS email_verified_at,
               u.display_name AS user_display_name
        FROM drivers d
        LEFT JOIN users u ON u.id = d.user_id
        WHERE d.driver_code = ?
        LIMIT 1
        """,
        (_text(driver_code),),
    ).fetchone()


def _get_target(db, role, target_code):
    role = _text(role).upper()
    target_code = _text(target_code)

    if role == "STORE":
        return get_store_with_user(db, target_code)

    if role == "DRIVER":
        return get_driver_with_user(db, target_code)

    return None


def _target_name(target, role):
    if role == "STORE":
        return _text(_row_get(target, "store_name", _row_get(target, "target_code", "")))

    if role == "DRIVER":
        return _text(_row_get(target, "driver_name", _row_get(target, "target_code", "")))

    return ""


def create_settlement_batch(
    db,
    *,
    role,
    target_code,
    direction,
    settlement_type,
    amount_twd,
    period_start=None,
    period_end=None,
    related_order_codes=None,
    payment_method="BANK_TRANSFER",
    note="",
    commit=False,
):
    ensure_settlement_tables(db)

    role = _text(role).upper()
    direction = _text(direction).upper()
    settlement_type = _text(settlement_type).upper()
    target_code = _text(target_code)
    payment_method = _text(payment_method, "BANK_TRANSFER").upper()

    if role not in VALID_ROLES:
        raise ValueError("Invalid settlement role.")

    if direction not in VALID_DIRECTIONS:
        raise ValueError("Invalid settlement direction.")

    if settlement_type not in VALID_SETTLEMENT_TYPES:
        raise ValueError("Invalid settlement type.")

    if payment_method not in VALID_PAYMENT_METHODS:
        payment_method = "OTHER"

    amount_twd = _money(amount_twd)

    if amount_twd <= 0:
        raise ValueError("Settlement amount must be greater than 0.")

    target = _get_target(db, role, target_code)

    if not target:
        raise ValueError("Settlement target not found.")

    target_user_id = _int(_row_get(target, "user_id", 0))
    target_email = _text(_row_get(target, "email", ""))
    period_start = _text(period_start, current_month_range()[0])
    period_end = _text(period_end, current_month_range()[1])
    now = now_iso()
    code = _settlement_code()

    admin_snapshot = snapshot_admin_payment_info()
    payout_snapshot = snapshot_target_payout_account(target)

    if related_order_codes is None:
        related_order_codes = []

    db.execute(
        """
        INSERT INTO settlement_batches (
            settlement_code,
            role,
            target_code,
            target_user_id,
            target_email,
            direction,
            settlement_type,
            period_start,
            period_end,
            amount_twd,
            status,
            payment_method,
            admin_bank_snapshot_json,
            target_payout_snapshot_json,
            related_order_codes_json,
            note,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'DRAFT', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            code,
            role,
            target_code,
            target_user_id,
            target_email,
            direction,
            settlement_type,
            period_start,
            period_end,
            amount_twd,
            payment_method,
            _json_dumps(admin_snapshot),
            _json_dumps(payout_snapshot),
            _json_dumps(related_order_codes),
            _text(note),
            now,
            now,
        ),
    )

    if commit:
        db.commit()

    return get_settlement_by_code(db, code)


def get_settlement_by_code(db, settlement_code):
    ensure_settlement_tables(db)

    row = db.execute(
        """
        SELECT *
        FROM settlement_batches
        WHERE settlement_code = ?
        LIMIT 1
        """,
        (_text(settlement_code).upper(),),
    ).fetchone()

    if not row:
        return None

    data = _row_to_dict(row)
    data["admin_bank_snapshot"] = _json_loads(
        data.get("admin_bank_snapshot_json"),
        {},
    )
    data["target_payout_snapshot"] = _json_loads(
        data.get("target_payout_snapshot_json"),
        {},
    )
    data["related_order_codes"] = _json_loads(
        data.get("related_order_codes_json"),
        [],
    )

    return data


def mark_settlement_email_sent(db, settlement_code, commit=False):
    ensure_settlement_tables(db)

    now = now_iso()

    cur = db.execute(
        """
        UPDATE settlement_batches
        SET status = 'EMAIL_SENT',
            email_sent_at = ?,
            updated_at = ?
        WHERE settlement_code = ?
          AND status IN ('DRAFT', 'EMAIL_SENT')
        """,
        (
            now,
            now,
            _text(settlement_code).upper(),
        ),
    )

    if cur.rowcount <= 0:
        raise ValueError("Settlement cannot be marked as EMAIL_SENT.")

    if commit:
        db.commit()

    return get_settlement_by_code(db, settlement_code)


def confirm_settlement_paid(
    db,
    settlement_code,
    *,
    admin_user_id,
    payment_method="BANK_TRANSFER",
    note="",
    commit=False,
):
    ensure_settlement_tables(db)

    payment_method = _text(payment_method, "BANK_TRANSFER").upper()

    if payment_method not in VALID_PAYMENT_METHODS:
        payment_method = "OTHER"

    now = now_iso()

    cur = db.execute(
        """
        UPDATE settlement_batches
        SET status = 'PAID_CONFIRMED',
            paid_confirmed_at = ?,
            paid_confirmed_by = ?,
            payment_method = ?,
            note = CASE
                WHEN ? != '' THEN ?
                ELSE note
            END,
            updated_at = ?
        WHERE settlement_code = ?
          AND status IN ('DRAFT', 'EMAIL_SENT')
        """,
        (
            now,
            _int(admin_user_id),
            payment_method,
            _text(note),
            _text(note),
            now,
            _text(settlement_code).upper(),
        ),
    )

    if cur.rowcount <= 0:
        raise ValueError("Settlement cannot be confirmed paid.")

    if commit:
        db.commit()

    return get_settlement_by_code(db, settlement_code)


def cancel_settlement(db, settlement_code, *, note="", commit=False):
    ensure_settlement_tables(db)

    now = now_iso()

    cur = db.execute(
        """
        UPDATE settlement_batches
        SET status = 'CANCELLED',
            note = CASE
                WHEN ? != '' THEN ?
                ELSE note
            END,
            updated_at = ?
        WHERE settlement_code = ?
          AND status IN ('DRAFT', 'EMAIL_SENT')
        """,
        (
            _text(note),
            _text(note),
            now,
            _text(settlement_code).upper(),
        ),
    )

    if cur.rowcount <= 0:
        raise ValueError("Settlement cannot be cancelled.")

    if commit:
        db.commit()

    return get_settlement_by_code(db, settlement_code)


def dispute_settlement(db, settlement_code, *, note="", commit=False):
    ensure_settlement_tables(db)

    now = now_iso()

    cur = db.execute(
        """
        UPDATE settlement_batches
        SET status = 'DISPUTED',
            note = CASE
                WHEN ? != '' THEN ?
                ELSE note
            END,
            updated_at = ?
        WHERE settlement_code = ?
          AND status IN ('DRAFT', 'EMAIL_SENT')
        """,
        (
            _text(note),
            _text(note),
            now,
            _text(settlement_code).upper(),
        ),
    )

    if cur.rowcount <= 0:
        raise ValueError("Settlement cannot be disputed.")

    if commit:
        db.commit()

    return get_settlement_by_code(db, settlement_code)


def _latest_open_settlement_dict(
    db,
    *,
    role,
    target_code,
    direction,
    settlement_type,
):
    row = _latest_open_settlement(
        db,
        role=role,
        target_code=target_code,
        direction=direction,
        settlement_type=settlement_type,
    )

    if not row:
        return None

    return get_settlement_by_code(db, row["settlement_code"])


def _store_dashboard_row(db, store, period_start, period_end):
    store = _row_to_dict(store)
    store_code = _text(store.get("store_code", ""))

    debt_all = calculate_store_unpaid_admin_debt(db, store)
    debt_month = calculate_store_unpaid_admin_debt(
        db,
        store,
        period_start,
        period_end,
    )
    admin_owes_all = calculate_admin_owes_store(db, store)
    admin_owes_month = calculate_admin_owes_store(
        db,
        store,
        period_start,
        period_end,
    )

    store["email"] = _text(store.get("email", ""))
    store["email_verified"] = bool(_text(store.get("email_verified_at", "")))
    store["payout_account_complete"] = payout_account_is_complete(store)

    store["store_owes_admin"] = debt_all
    store["store_owes_admin_month"] = debt_month
    store["admin_owes_store"] = admin_owes_all
    store["admin_owes_store_month"] = admin_owes_month

    store["store_owes_admin_open_settlement"] = _latest_open_settlement_dict(
        db,
        role="STORE",
        target_code=store_code,
        direction="TARGET_OWES_ADMIN",
        settlement_type="STORE_PLATFORM_FEE",
    )
    store["admin_owes_store_open_settlement"] = _latest_open_settlement_dict(
        db,
        role="STORE",
        target_code=store_code,
        direction="ADMIN_OWES_TARGET",
        settlement_type="ADMIN_PAYOUT_STORE",
    )

    return store


def _driver_dashboard_row(db, driver, period_start, period_end):
    driver = _row_to_dict(driver)
    driver_code = _text(driver.get("driver_code", ""))

    debt_all = calculate_driver_unpaid_admin_debt(db, driver)
    debt_month = calculate_driver_unpaid_admin_debt(
        db,
        driver,
        period_start,
        period_end,
    )
    admin_owes_all = calculate_admin_owes_driver(db, driver)
    admin_owes_month = calculate_admin_owes_driver(
        db,
        driver,
        period_start,
        period_end,
    )

    driver["email"] = _text(driver.get("email", ""))
    driver["email_verified"] = bool(_text(driver.get("email_verified_at", "")))
    driver["payout_account_complete"] = payout_account_is_complete(driver)

    driver["driver_owes_admin"] = debt_all
    driver["driver_owes_admin_month"] = debt_month
    driver["admin_owes_driver"] = admin_owes_all
    driver["admin_owes_driver_month"] = admin_owes_month

    driver["driver_owes_admin_open_settlement"] = _latest_open_settlement_dict(
        db,
        role="DRIVER",
        target_code=driver_code,
        direction="TARGET_OWES_ADMIN",
        settlement_type="DRIVER_PLATFORM_FEE",
    )
    driver["admin_owes_driver_open_settlement"] = _latest_open_settlement_dict(
        db,
        role="DRIVER",
        target_code=driver_code,
        direction="ADMIN_OWES_TARGET",
        settlement_type="ADMIN_PAYOUT_DRIVER",
    )

    return driver


def list_recent_settlements(db, limit=80):
    ensure_settlement_tables(db)

    rows = db.execute(
        """
        SELECT *
        FROM settlement_batches
        ORDER BY id DESC
        LIMIT ?
        """,
        (_int(limit, 80),),
    ).fetchall()

    result = []

    for row in rows:
        item = get_settlement_by_code(db, row["settlement_code"])
        if item:
            result.append(item)

    return result


def list_admin_settlement_dashboard(db):
    ensure_settlement_tables(db)

    period_start, period_end = current_month_range()

    store_rows = db.execute(
        """
        SELECT s.*,
               u.id AS user_id,
               u.email AS email,
               u.email_verified_at AS email_verified_at,
               u.display_name AS user_display_name
        FROM stores s
        LEFT JOIN users u ON u.id = s.owner_user_id
        WHERE COALESCE(s.store_code, '') != ''
        ORDER BY s.id DESC
        LIMIT 500
        """
    ).fetchall()

    driver_rows = db.execute(
        """
        SELECT d.*,
               u.id AS user_id,
               u.email AS email,
               u.email_verified_at AS email_verified_at,
               u.display_name AS user_display_name
        FROM drivers d
        LEFT JOIN users u ON u.id = d.user_id
        WHERE COALESCE(d.driver_code, '') != ''
        ORDER BY d.id DESC
        LIMIT 500
        """
    ).fetchall()

    stores = [
        _store_dashboard_row(db, store, period_start, period_end)
        for store in store_rows
    ]

    drivers = [
        _driver_dashboard_row(db, driver, period_start, period_end)
        for driver in driver_rows
    ]

    summary = {
        "store_owes_admin_twd": sum(
            s["store_owes_admin"]["unpaid_twd"] for s in stores
        ),
        "driver_owes_admin_twd": sum(
            d["driver_owes_admin"]["unpaid_twd"] for d in drivers
        ),
        "admin_owes_store_twd": sum(
            s["admin_owes_store"]["unpaid_twd"] for s in stores
        ),
        "admin_owes_driver_twd": sum(
            d["admin_owes_driver"]["unpaid_twd"] for d in drivers
        ),
        "store_owes_admin_month_twd": sum(
            s["store_owes_admin_month"]["unpaid_twd"] for s in stores
        ),
        "driver_owes_admin_month_twd": sum(
            d["driver_owes_admin_month"]["unpaid_twd"] for d in drivers
        ),
        "admin_owes_store_month_twd": sum(
            s["admin_owes_store_month"]["unpaid_twd"] for s in stores
        ),
        "admin_owes_driver_month_twd": sum(
            d["admin_owes_driver_month"]["unpaid_twd"] for d in drivers
        ),
    }

    return {
        "period_start": period_start,
        "period_end": period_end,
        "admin_payment_info": snapshot_admin_payment_info(),
        "stores": stores,
        "drivers": drivers,
        "summary": summary,
        "recent_settlements": list_recent_settlements(db, limit=100),
    }


def settlement_label(settlement):
    direction = _text(_row_get(settlement, "direction", ""))
    settlement_type = _text(_row_get(settlement, "settlement_type", ""))

    if direction == "TARGET_OWES_ADMIN" and settlement_type == "STORE_PLATFORM_FEE":
        return "店家欠 Admin"

    if direction == "TARGET_OWES_ADMIN" and settlement_type == "DRIVER_PLATFORM_FEE":
        return "Shiper 欠 Admin"

    if direction == "ADMIN_OWES_TARGET" and settlement_type == "ADMIN_PAYOUT_STORE":
        return "Admin 欠店家"

    if direction == "ADMIN_OWES_TARGET" and settlement_type == "ADMIN_PAYOUT_DRIVER":
        return "Admin 欠 Shiper"

    return "結算"


def settlement_requires_target_payout(settlement):
    return _text(_row_get(settlement, "direction", "")) == "ADMIN_OWES_TARGET"


def settlement_requires_admin_receiving_account(settlement):
    return _text(_row_get(settlement, "direction", "")) == "TARGET_OWES_ADMIN"

import math
import uuid

from services.code_service import now_iso


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


def _int(value, default=0):
    try:
        if value is None:
            return int(default or 0)
        return int(value or 0)
    except Exception:
        return int(default or 0)


def _money(value):
    return max(0, _int(value, 0))


def _text(value, default=""):
    try:
        value = str(value if value is not None else default).strip()
        return value if value else default
    except Exception:
        return default


def _ceil_percent(amount, percent):
    amount = _money(amount)
    try:
        return int(math.ceil(amount * float(percent) / 100.0))
    except Exception:
        return 0


def _entry_code():
    return "ACC-" + uuid.uuid4().hex[:12].upper()


def _date_text(value):
    value = _text(value, "")
    return value[:10] if len(value) >= 10 else ""


def _current_month_range():
    today = now_iso()[:10]
    year = int(today[:4])
    month = int(today[5:7])

    start = f"{year:04d}-{month:02d}-01"

    if month == 12:
        next_start = f"{year + 1:04d}-01-01"
    else:
        next_start = f"{year:04d}-{month + 1:02d}-01"

    return start, next_start


def _date_in_range(date_value, start, end):
    date_value = _date_text(date_value)
    return bool(date_value and start <= date_value < end)


def _table_columns(db, table_name):
    try:
        rows = db.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {r["name"] for r in rows}
    except Exception:
        return set()


def _table_exists(db, table_name):
    try:
        row = db.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name = ?
            LIMIT 1
            """,
            (_text(table_name),),
        ).fetchone()

        return bool(row)
    except Exception:
        return False


def _accounting_table_exists(db):
    return _table_exists(db, "accounting_entries")


def _settlement_batches_table_exists(db):
    return _table_exists(db, "settlement_batches")


def _settlement_total(
    db,
    *,
    role,
    target_code,
    direction,
    settlement_type,
    statuses,
):
    """
    Read settlement_batches safely.

    Phase 6 Lite rule:
    - Only PAID_CONFIRMED decreases debt.
    - DRAFT / EMAIL_SENT are open settlements.
    - target_marked_paid_at does not decrease debt.
    """
    if not _settlement_batches_table_exists(db):
        return 0

    required_columns = {
        "role",
        "target_code",
        "direction",
        "settlement_type",
        "status",
        "amount_twd",
    }

    if not required_columns.issubset(_table_columns(db, "settlement_batches")):
        return 0

    statuses = [
        _text(status).upper()
        for status in (statuses or [])
        if _text(status)
    ]

    if not statuses:
        return 0

    placeholders = ",".join(["?"] * len(statuses))

    try:
        row = db.execute(
            f"""
            SELECT COALESCE(SUM(amount_twd), 0) AS total
            FROM settlement_batches
            WHERE role = ?
              AND target_code = ?
              AND direction = ?
              AND settlement_type = ?
              AND status IN ({placeholders})
            """,
            (
                _text(role).upper(),
                _text(target_code),
                _text(direction).upper(),
                _text(settlement_type).upper(),
                *statuses,
            ),
        ).fetchone()

        return _money(_row_get(row, "total", 0))

    except Exception:
        return 0


def _paid_settlement_total(
    db,
    *,
    role,
    target_code,
    direction,
    settlement_type,
):
    return _settlement_total(
        db,
        role=role,
        target_code=target_code,
        direction=direction,
        settlement_type=settlement_type,
        statuses=["PAID_CONFIRMED"],
    )


def _open_settlement_total(
    db,
    *,
    role,
    target_code,
    direction,
    settlement_type,
):
    return _settlement_total(
        db,
        role=role,
        target_code=target_code,
        direction=direction,
        settlement_type=settlement_type,
        statuses=["DRAFT", "EMAIL_SENT"],
    )


def calculate_order_settlement(order):
    """
    COD V2:
    - Shiper does not collect store platform fee.
    - Store owes Admin store platform fee.
    - Shiper owes Admin driver platform fee.
    """
    subtotal = _money(_row_get(order, "subtotal_twd", 0))
    total = _money(_row_get(order, "total_twd", 0))

    delivery_fee = _money(_row_get(order, "delivery_fee_twd", 0))
    base_delivery_fee = _money(
        _row_get(order, "base_delivery_fee_twd", delivery_fee)
    )
    extra_fee = _money(_row_get(order, "extra_fee_twd", 0))
    rain_fee = _money(_row_get(order, "rain_fee_twd", 0))

    customer_delivery_share = _money(
        _row_get(order, "customer_delivery_share_twd", delivery_fee)
    )
    store_delivery_support = _money(
        _row_get(order, "store_delivery_support_twd", 0)
    )

    service_fee = _money(_row_get(order, "service_fee_twd", 0))
    if service_fee <= 0 and subtotal > 0:
        service_fee = _ceil_percent(subtotal, 5)

    payment_method = _text(_row_get(order, "payment_method", "COD"), "COD").upper()

    driver_gross = delivery_fee + extra_fee
    driver_platform_fee = _ceil_percent(driver_gross, 5)
    driver_net_income = max(0, driver_gross - driver_platform_fee)

    store_platform_fee = service_fee
    store_cash_from_driver = max(0, subtotal - store_delivery_support)
    store_net_after_settlement = max(0, store_cash_from_driver - store_platform_fee)
    store_receivable = store_net_after_settlement

    admin_store_receivable = store_platform_fee
    admin_driver_receivable = driver_platform_fee
    admin_total_receivable = admin_store_receivable + admin_driver_receivable

    driver_collect_from_customer = 0
    driver_pay_store = 0
    driver_pay_admin = 0
    driver_keep = 0
    platform_pay_driver = 0

    prepaid_driver_collect_customer = 0
    prepaid_store_pay_driver_support = 0
    prepaid_driver_pay_admin = 0

    manual_review = False
    settlement_note = "OK"

    if payment_method == "COD":
        driver_collect_from_customer = total
        driver_pay_store = store_cash_from_driver
        driver_pay_admin = driver_platform_fee
        driver_keep = max(0, driver_collect_from_customer - driver_pay_store)
        settlement_note = (
            "COD V2: Shiper pays store subtotal minus store delivery support. "
            "Store platform fee is settled by store with Admin separately."
        )

    elif payment_method in {"BANK_TRANSFER", "PLATFORM"}:
        driver_collect_from_customer = 0
        platform_pay_driver = driver_gross
        settlement_note = (
            "Transfer/platform payment: Shiper does not collect cash from customer."
        )

    elif payment_method == "PREPAID_TO_STORE":
        manual_review = True
        prepaid_driver_collect_customer = customer_delivery_share + extra_fee
        prepaid_store_pay_driver_support = store_delivery_support
        prepaid_driver_pay_admin = driver_platform_fee
        settlement_note = "Prepaid to store: Admin review required."

    else:
        manual_review = True
        settlement_note = "Unknown payment method; Admin review required."

    return {
        "total_twd": total,
        "subtotal_twd": subtotal,
        "delivery_fee_twd": delivery_fee,
        "base_delivery_fee_twd": base_delivery_fee,
        "extra_fee_twd": extra_fee,
        "rain_fee_twd": rain_fee,
        "customer_delivery_share_twd": customer_delivery_share,
        "store_delivery_support_twd": store_delivery_support,
        "service_fee_twd": service_fee,
        "payment_method": payment_method,

        "driver_gross_twd": driver_gross,
        "driver_platform_fee_twd": driver_platform_fee,
        "driver_net_income_twd": driver_net_income,
        "driver_collect_from_customer_twd": driver_collect_from_customer,
        "driver_pay_store_twd": driver_pay_store,
        "driver_pay_admin_twd": driver_pay_admin,
        "driver_keep_twd": driver_keep,

        "store_cash_from_driver_twd": store_cash_from_driver,
        "store_platform_fee_twd": store_platform_fee,
        "store_net_after_settlement_twd": store_net_after_settlement,
        "store_receivable_twd": store_receivable,

        "admin_store_receivable_twd": admin_store_receivable,
        "admin_driver_receivable_twd": admin_driver_receivable,
        "admin_total_receivable_twd": admin_total_receivable,

        "platform_pay_driver_twd": platform_pay_driver,
        "prepaid_driver_collect_customer_twd": prepaid_driver_collect_customer,
        "prepaid_store_pay_driver_support_twd": prepaid_store_pay_driver_support,
        "prepaid_driver_pay_admin_twd": prepaid_driver_pay_admin,

        "balance_ok": True,
        "manual_review": manual_review,
        "settlement_note": settlement_note,
    }


def _insert_accounting_entry(
    db,
    *,
    order,
    entry_type,
    role,
    target_code,
    amount_twd,
    direction,
    note,
    created_at,
):
    if not _accounting_table_exists(db):
        return None

    return db.execute(
        """
        INSERT INTO accounting_entries (
            entry_code,
            order_id,
            order_code,
            entry_type,
            role,
            target_code,
            amount_twd,
            direction,
            note,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _entry_code(),
            _row_get(order, "id", 0),
            _row_get(order, "order_code", ""),
            entry_type,
            role,
            target_code,
            _money(amount_twd),
            direction,
            note,
            created_at,
        ),
    )


def _delivery_entries_already_exist(db, order_code):
    if not _accounting_table_exists(db):
        return False

    row = db.execute(
        """
        SELECT COUNT(*) AS c
        FROM accounting_entries
        WHERE order_code = ?
          AND entry_type IN (
              'DELIVERY_COMPLETED',
              'DRIVER_INCOME',
              'DRIVER_PLATFORM_FEE',
              'STORE_RECEIVABLE',
              'STORE_PLATFORM_FEE',
              'PLATFORM_FEE'
          )
        """,
        (order_code,),
    ).fetchone()

    return bool(row and int(row["c"] or 0) > 0)


def create_delivery_accounting_entries(db, order, driver, commit=False):
    order_code = _text(_row_get(order, "order_code", ""))

    if not order_code:
        return []

    if not _accounting_table_exists(db):
        return []

    if _delivery_entries_already_exist(db, order_code):
        return list_accounting_entries_for_order(db, order_code)

    settlement = calculate_order_settlement(order)
    created_at = now_iso()

    driver_code = _text(_row_get(driver, "driver_code", ""))
    store_code = _text(_row_get(order, "store_code", ""))

    _insert_accounting_entry(
        db,
        order=order,
        entry_type="DELIVERY_COMPLETED",
        role="ORDER",
        target_code=order_code,
        amount_twd=settlement["total_twd"],
        direction="INFO",
        note="Delivery completed; accounting entries generated.",
        created_at=created_at,
    )

    if driver_code:
        _insert_accounting_entry(
            db,
            order=order,
            entry_type="DRIVER_INCOME",
            role="DRIVER",
            target_code=driver_code,
            amount_twd=settlement["driver_gross_twd"],
            direction="CREDIT",
            note="Driver gross income: delivery fee + extra fee.",
            created_at=created_at,
        )

        _insert_accounting_entry(
            db,
            order=order,
            entry_type="DRIVER_PLATFORM_FEE",
            role="DRIVER",
            target_code=driver_code,
            amount_twd=settlement["driver_platform_fee_twd"],
            direction="DEBIT",
            note="Driver platform fee 5% of driver gross income.",
            created_at=created_at,
        )

    if store_code:
        _insert_accounting_entry(
            db,
            order=order,
            entry_type="STORE_RECEIVABLE",
            role="STORE",
            target_code=store_code,
            amount_twd=settlement["store_net_after_settlement_twd"],
            direction="CREDIT",
            note="Store net after delivery support and store platform fee.",
            created_at=created_at,
        )

        _insert_accounting_entry(
            db,
            order=order,
            entry_type="STORE_PLATFORM_FEE",
            role="STORE",
            target_code=store_code,
            amount_twd=settlement["store_platform_fee_twd"],
            direction="DEBIT",
            note="Store platform service fee; settled by store with Admin.",
            created_at=created_at,
        )

    _insert_accounting_entry(
        db,
        order=order,
        entry_type="PLATFORM_FEE",
        role="ADMIN",
        target_code="ADMIN",
        amount_twd=settlement["admin_total_receivable_twd"],
        direction="CREDIT",
        note="Platform fees from store service fee and driver platform fee.",
        created_at=created_at,
    )

    if commit:
        db.commit()

    return list_accounting_entries_for_order(db, order_code)


def list_accounting_entries_for_order(db, order_code):
    if not _accounting_table_exists(db):
        return []

    return db.execute(
        """
        SELECT *
        FROM accounting_entries
        WHERE order_code = ?
        ORDER BY id DESC
        """,
        (_text(order_code).upper(),),
    ).fetchall()


def list_driver_accounting_entries(db, driver_code, limit=300):
    if not _accounting_table_exists(db):
        return []

    return db.execute(
        """
        SELECT *
        FROM accounting_entries
        WHERE target_code = ?
           OR (role = 'DRIVER' AND target_code = ?)
        ORDER BY id DESC
        LIMIT ?
        """,
        (
            _text(driver_code),
            _text(driver_code),
            _int(limit, 300),
        ),
    ).fetchall()


def list_store_accounting_entries(db, store_code, limit=300):
    if not _accounting_table_exists(db):
        return []

    return db.execute(
        """
        SELECT *
        FROM accounting_entries
        WHERE target_code = ?
           OR (role = 'STORE' AND target_code = ?)
        ORDER BY id DESC
        LIMIT ?
        """,
        (
            _text(store_code),
            _text(store_code),
            _int(limit, 300),
        ),
    ).fetchall()


def _completed_driver_orders(db, driver_id):
    try:
        return db.execute(
            """
            SELECT *
            FROM orders
            WHERE driver_id = ?
              AND status IN ('DELIVERED', 'COMPLETED')
            ORDER BY id DESC
            LIMIT 2000
            """,
            (driver_id,),
        ).fetchall()
    except Exception:
        return []


def _store_orders(db, store_id):
    try:
        return db.execute(
            """
            SELECT *
            FROM orders
            WHERE store_id = ?
            ORDER BY id DESC
            LIMIT 2000
            """,
            (store_id,),
        ).fetchall()
    except Exception:
        return []


def driver_accounting_summary(db, driver):
    driver_id = _int(_row_get(driver, "id", 0))
    driver_code = _text(_row_get(driver, "driver_code", ""))
    today = now_iso()[:10]
    month_start, next_month_start = _current_month_range()

    summary = {
        "today_income_twd": 0,
        "today_payable_admin_twd": 0,
        "today_payable_store_twd": 0,
        "today_cod_collected_twd": 0,
        "today_cash_keep_twd": 0,
        "today_net_hint_twd": 0,
        "today_delivered_count": 0,

        "month_income_twd": 0,
        "month_payable_admin_twd": 0,
        "month_payable_store_twd": 0,
        "month_cod_collected_twd": 0,
        "month_cash_keep_twd": 0,
        "month_net_income_twd": 0,
        "month_delivered_count": 0,

        "unpaid_driver_platform_fee_twd": 0,
        "unpaid_platform_fee_twd": 0,
        "paid_driver_platform_fee_twd": 0,
        "open_driver_platform_fee_twd": 0,
        "available_driver_platform_fee_twd": 0,

        "active_orders_count": 0,
        "available_orders_count": 0,
        "all_waiting_orders_count": 0,

        "total_income_twd": 0,
        "total_platform_fee_twd": 0,
        "total_net_income_twd": 0,
        "total_cod_collected_twd": 0,
        "total_payable_store_twd": 0,
        "total_cash_keep_twd": 0,

        # Backward-compatible aliases for old templates.
        "driver_gross_twd": 0,
        "driver_platform_fee_twd": 0,
        "driver_net_income_twd": 0,
        "entries_count": 0,
        "cod_collected_twd": 0,
        "pay_store_twd": 0,
        "pay_admin_twd": 0,
        "driver_clear_cash_after_payables_twd": 0,
        "platform_receivable_twd": 0,
        "customer_delivery_collected_twd": 0,
        "store_support_receivable_twd": 0,
        "platform_orders": 0,
    }

    if not driver_id:
        return summary

    try:
        active = db.execute(
            """
            SELECT COUNT(*) AS c
            FROM orders
            WHERE driver_id = ?
              AND status IN ('DRIVER_ACCEPTED', 'PICKED_UP', 'DELIVERY_ISSUE', 'RETURNING_TO_STORE')
            """,
            (driver_id,),
        ).fetchone()
        summary["active_orders_count"] = int(active["c"] or 0) if active else 0
    except Exception:
        pass

    rows = _completed_driver_orders(db, driver_id)

    for row in rows:
        settlement = calculate_order_settlement(row)
        payment_method = settlement["payment_method"]

        gross = settlement["driver_gross_twd"]
        platform_fee = settlement["driver_platform_fee_twd"]
        net = settlement["driver_net_income_twd"]

        updated_at = _text(_row_get(row, "updated_at", _row_get(row, "created_at", "")))
        is_today = updated_at.startswith(today)
        is_month = _date_in_range(updated_at, month_start, next_month_start)

        summary["total_income_twd"] += gross
        summary["total_platform_fee_twd"] += platform_fee
        summary["total_net_income_twd"] += net

        if payment_method == "COD":
            summary["total_cod_collected_twd"] += settlement["driver_collect_from_customer_twd"]
            summary["total_payable_store_twd"] += settlement["driver_pay_store_twd"]
            summary["total_cash_keep_twd"] += settlement["driver_keep_twd"]

        elif payment_method in {"BANK_TRANSFER", "PLATFORM"}:
            summary["platform_receivable_twd"] += settlement["platform_pay_driver_twd"]
            summary["platform_orders"] += 1

        elif payment_method == "PREPAID_TO_STORE":
            summary["customer_delivery_collected_twd"] += settlement["prepaid_driver_collect_customer_twd"]
            summary["store_support_receivable_twd"] += settlement["prepaid_store_pay_driver_support_twd"]

        if is_today:
            summary["today_delivered_count"] += 1
            summary["today_income_twd"] += gross
            summary["today_payable_admin_twd"] += platform_fee
            summary["today_net_hint_twd"] += net

            if payment_method == "COD":
                summary["today_cod_collected_twd"] += settlement["driver_collect_from_customer_twd"]
                summary["today_payable_store_twd"] += settlement["driver_pay_store_twd"]
                summary["today_cash_keep_twd"] += settlement["driver_keep_twd"]

        if is_month:
            summary["month_delivered_count"] += 1
            summary["month_income_twd"] += gross
            summary["month_payable_admin_twd"] += platform_fee
            summary["month_net_income_twd"] += net

            if payment_method == "COD":
                summary["month_cod_collected_twd"] += settlement["driver_collect_from_customer_twd"]
                summary["month_payable_store_twd"] += settlement["driver_pay_store_twd"]
                summary["month_cash_keep_twd"] += settlement["driver_keep_twd"]

    paid_platform_fee = _paid_settlement_total(
        db,
        role="DRIVER",
        target_code=driver_code,
        direction="TARGET_OWES_ADMIN",
        settlement_type="DRIVER_PLATFORM_FEE",
    )
    open_platform_fee = _open_settlement_total(
        db,
        role="DRIVER",
        target_code=driver_code,
        direction="TARGET_OWES_ADMIN",
        settlement_type="DRIVER_PLATFORM_FEE",
    )

    gross_platform_fee = summary["total_platform_fee_twd"]
    unpaid_platform_fee = max(0, gross_platform_fee - paid_platform_fee)
    available_platform_fee = max(0, gross_platform_fee - paid_platform_fee - open_platform_fee)

    summary["paid_driver_platform_fee_twd"] = paid_platform_fee
    summary["open_driver_platform_fee_twd"] = open_platform_fee
    summary["unpaid_driver_platform_fee_twd"] = unpaid_platform_fee
    summary["unpaid_platform_fee_twd"] = unpaid_platform_fee
    summary["available_driver_platform_fee_twd"] = available_platform_fee

    summary["driver_gross_twd"] = summary["total_income_twd"]
    summary["driver_platform_fee_twd"] = summary["total_platform_fee_twd"]
    summary["driver_net_income_twd"] = summary["total_net_income_twd"]
    summary["cod_collected_twd"] = summary["total_cod_collected_twd"]
    summary["pay_store_twd"] = summary["total_payable_store_twd"]
    summary["pay_admin_twd"] = unpaid_platform_fee
    summary["driver_clear_cash_after_payables_twd"] = summary["total_net_income_twd"]

    try:
        summary["entries_count"] = len(
            list_driver_accounting_entries(
                db,
                driver_code,
                limit=10000,
            )
        )
    except Exception:
        summary["entries_count"] = 0

    return summary


def store_accounting_summary(db, store):
    store_id = _int(_row_get(store, "id", 0))
    store_code = _text(_row_get(store, "store_code", ""))
    today = now_iso()[:10]
    month_start, next_month_start = _current_month_range()

    summary = {
        "today_sales_twd": 0,
        "today_platform_fee_twd": 0,
        "today_delivery_support_twd": 0,
        "today_cash_from_driver_twd": 0,
        "today_net_twd": 0,
        "today_completed_orders_count": 0,

        "month_sales_twd": 0,
        "month_platform_fee_twd": 0,
        "month_delivery_support_twd": 0,
        "month_cash_from_driver_twd": 0,
        "month_net_twd": 0,
        "month_completed_orders_count": 0,

        "unpaid_store_platform_fee_twd": 0,
        "unpaid_platform_fee_twd": 0,
        "paid_store_platform_fee_twd": 0,
        "open_store_platform_fee_twd": 0,
        "available_store_platform_fee_twd": 0,

        "pending_receivable_twd": 0,
        "pending_cash_from_driver_twd": 0,
        "pending_platform_fee_twd": 0,

        "completed_orders_count": 0,
        "active_orders_count": 0,

        "total_sales_twd": 0,
        "total_platform_fee_twd": 0,
        "total_delivery_support_twd": 0,
        "total_cash_from_driver_twd": 0,
        "total_net_twd": 0,
    }

    if not store_id:
        return summary

    try:
        active = db.execute(
            """
            SELECT COUNT(*) AS c
            FROM orders
            WHERE store_id = ?
              AND status IN (
                  'CREATED',
                  'STORE_ACCEPTED',
                  'WAITING_DRIVER',
                  'DRIVER_ACCEPTED',
                  'PICKED_UP'
              )
            """,
            (store_id,),
        ).fetchone()
        summary["active_orders_count"] = int(active["c"] or 0) if active else 0
    except Exception:
        pass

    rows = _store_orders(db, store_id)

    for row in rows:
        settlement = calculate_order_settlement(row)

        subtotal = settlement["subtotal_twd"]
        service_fee = settlement["store_platform_fee_twd"]
        support = settlement["store_delivery_support_twd"]
        cash_from_driver = settlement["store_cash_from_driver_twd"]
        net = settlement["store_net_after_settlement_twd"]

        status = _text(_row_get(row, "status", "")).upper()
        updated_at = _text(_row_get(row, "updated_at", _row_get(row, "created_at", "")))
        is_today = updated_at.startswith(today)
        is_month = _date_in_range(updated_at, month_start, next_month_start)

        if status in {"DELIVERED", "COMPLETED"}:
            summary["completed_orders_count"] += 1
            summary["total_sales_twd"] += subtotal
            summary["total_platform_fee_twd"] += service_fee
            summary["total_delivery_support_twd"] += support
            summary["total_cash_from_driver_twd"] += cash_from_driver
            summary["total_net_twd"] += net

            if is_today:
                summary["today_completed_orders_count"] += 1
                summary["today_sales_twd"] += subtotal
                summary["today_platform_fee_twd"] += service_fee
                summary["today_delivery_support_twd"] += support
                summary["today_cash_from_driver_twd"] += cash_from_driver
                summary["today_net_twd"] += net

            if is_month:
                summary["month_completed_orders_count"] += 1
                summary["month_sales_twd"] += subtotal
                summary["month_platform_fee_twd"] += service_fee
                summary["month_delivery_support_twd"] += support
                summary["month_cash_from_driver_twd"] += cash_from_driver
                summary["month_net_twd"] += net

        elif status in {
            "CREATED",
            "STORE_ACCEPTED",
            "WAITING_DRIVER",
            "DRIVER_ACCEPTED",
            "PICKED_UP",
        }:
            summary["pending_receivable_twd"] += net
            summary["pending_cash_from_driver_twd"] += cash_from_driver
            summary["pending_platform_fee_twd"] += service_fee

    paid_platform_fee = _paid_settlement_total(
        db,
        role="STORE",
        target_code=store_code,
        direction="TARGET_OWES_ADMIN",
        settlement_type="STORE_PLATFORM_FEE",
    )
    open_platform_fee = _open_settlement_total(
        db,
        role="STORE",
        target_code=store_code,
        direction="TARGET_OWES_ADMIN",
        settlement_type="STORE_PLATFORM_FEE",
    )

    gross_platform_fee = summary["total_platform_fee_twd"]
    unpaid_platform_fee = max(0, gross_platform_fee - paid_platform_fee)
    available_platform_fee = max(0, gross_platform_fee - paid_platform_fee - open_platform_fee)

    summary["paid_store_platform_fee_twd"] = paid_platform_fee
    summary["open_store_platform_fee_twd"] = open_platform_fee
    summary["unpaid_store_platform_fee_twd"] = unpaid_platform_fee
    summary["unpaid_platform_fee_twd"] = unpaid_platform_fee
    summary["available_store_platform_fee_twd"] = available_platform_fee

    return summary

def list_admin_accounting_entries(db, limit=500):
    """
    Admin accounting feed.

    Compatibility helper for routes/admin_routes.py:
    /admin/accounting imports this function directly.

    Safe rule:
    - If accounting_entries table is missing, return [].
    - Do not throw, so Admin dashboard does not crash.
    """
    if not _accounting_table_exists(db):
        return []

    return db.execute(
        """
        SELECT *
        FROM accounting_entries
        ORDER BY id DESC
        LIMIT ?
        """,
        (_int(limit, 500),),
    ).fetchall()


def admin_accounting_summary(db):
    """
    Admin accounting summary.

    Used by /admin/accounting.

    Business rules:
    - Admin revenue = Store platform fee + Driver platform fee.
    - COD:
      Customer pays Shiper in cash.
      Shiper pays Store at pickup.
      Store platform fee is owed by Store to Admin.
      Driver platform fee is owed by Driver to Admin.
    - BANK_TRANSFER / PLATFORM:
      Platform/Admin holds customer payment.
      Platform/Admin should later pay Store and Driver via settlement.
    - Debt should not be reset here.
      Phase 6 Lite settlement truth remains settlement_batches.status = PAID_CONFIRMED.
    """
    summary = {
        "admin_revenue_twd": 0,
        "entries_count": 0,

        "store_platform_fee_twd": 0,
        "driver_platform_fee_twd": 0,

        "cod_admin_receivable_from_driver_twd": 0,
        "manual_review_orders": 0,

        "platform_customer_collected_twd": 0,
        "platform_keep_revenue_twd": 0,
        "platform_pay_store_twd": 0,
        "platform_pay_driver_twd": 0,
        "platform_cash_after_payables_twd": 0,

        "completed_orders_count": 0,
        "total_order_twd": 0,
    }

    try:
        row = db.execute(
            """
            SELECT COUNT(*) AS c
            FROM accounting_entries
            """
        ).fetchone()

        summary["entries_count"] = int(row["c"] or 0) if row else 0
    except Exception:
        summary["entries_count"] = 0

    try:
        orders = db.execute(
            """
            SELECT *
            FROM orders
            WHERE status IN ('DELIVERED', 'COMPLETED')
            ORDER BY id DESC
            LIMIT 10000
            """
        ).fetchall()
    except Exception:
        return summary

    for order in orders:
        settlement = calculate_order_settlement(order)
        payment_method = _text(_row_get(order, "payment_method", "COD"), "COD").upper()
        payment_status = _text(_row_get(order, "payment_status", "")).upper()

        total_twd = settlement["total_twd"]
        store_platform_fee = settlement["store_platform_fee_twd"]
        driver_platform_fee = settlement["driver_platform_fee_twd"]
        admin_revenue = settlement["admin_total_receivable_twd"]

        summary["completed_orders_count"] += 1
        summary["total_order_twd"] += total_twd

        summary["store_platform_fee_twd"] += store_platform_fee
        summary["driver_platform_fee_twd"] += driver_platform_fee
        summary["admin_revenue_twd"] += admin_revenue

        if settlement.get("manual_review"):
            summary["manual_review_orders"] += 1

        if payment_method == "COD":
            # COD V2:
            # Store platform fee is owed by Store to Admin.
            # Driver platform fee is owed by Driver to Admin.
            # This field name is kept for old template compatibility.
            summary["cod_admin_receivable_from_driver_twd"] += driver_platform_fee

        elif payment_method in {"BANK_TRANSFER", "PLATFORM"} and payment_status == "PAID":
            # Platform/Admin already holds customer payment.
            platform_pay_store = settlement["store_net_after_settlement_twd"]
            platform_pay_driver = settlement["platform_pay_driver_twd"]

            summary["platform_customer_collected_twd"] += total_twd
            summary["platform_pay_store_twd"] += platform_pay_store
            summary["platform_pay_driver_twd"] += platform_pay_driver

            platform_keep = max(0, total_twd - platform_pay_store - platform_pay_driver)
            summary["platform_keep_revenue_twd"] += platform_keep

        elif payment_method in {"PREPAID_TO_STORE"}:
            summary["manual_review_orders"] += 1

    summary["platform_cash_after_payables_twd"] = max(
        0,
        summary["platform_customer_collected_twd"]
        - summary["platform_pay_store_twd"]
        - summary["platform_pay_driver_twd"],
    )

    return summary

from flask import Blueprint, render_template, request, redirect, flash, session

from db import get_db
from services.permission_service import admin_required
from services.code_service import now_iso
from services.block_service import create_block
from services.system_flag_service import (
    is_rain_surcharge_enabled,
    set_rain_surcharge_enabled,
)
from services.email_service import (
    normalize_email,
    send_customer_payment_verified_email,
    send_customer_payment_rejected_email,
    send_store_payment_request_email,
    send_driver_payment_request_email,
    send_store_payout_confirmed_email,
    send_driver_payout_confirmed_email,
)
from services.settlement_service import (
    current_month_range,
    create_settlement_batch,
    get_settlement_by_code,
    mark_settlement_email_sent,
    confirm_settlement_paid,
    list_admin_settlement_dashboard,
    calculate_store_unpaid_admin_debt,
    calculate_driver_unpaid_admin_debt,
    calculate_admin_owes_store,
    calculate_admin_owes_driver,
    get_store_with_user,
    get_driver_with_user,
    snapshot_admin_payment_info,
)


try:
    from services.line_notify_service import push_to_role_target
except Exception:
    def push_to_role_target(db, **kwargs):
        return {"ok": False, "skipped": True, "error": "line notify unavailable"}


admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def _int(value, default=0):
    try:
        return int(value or 0)
    except Exception:
        return int(default or 0)


def _safe_text(value, default=""):
    value = "" if value is None else str(value)
    value = value.strip()
    return value if value else default


def _row_get(row, key, default=""):
    if row is None:
        return default

    try:
        if key in row.keys():
            value = row[key]
            return default if value is None else value
    except Exception:
        pass

    try:
        value = row.get(key, default)
        return default if value is None else value
    except Exception:
        return default


def _table_count(db, table_name, where_sql="", params=None):
    params = params or []
    sql = f"SELECT COUNT(*) AS c FROM {table_name}"

    if where_sql:
        sql += f" WHERE {where_sql}"

    row = db.execute(sql, params).fetchone()
    return int(row["c"] or 0) if row else 0


def _sum(db, sql, params=None):
    params = params or []
    row = db.execute(sql, params).fetchone()

    if not row:
        return 0

    try:
        return int(row[0] or 0)
    except Exception:
        return 0


def _admin_actor():
    try:
        admin_user_id = int(session.get("user_id") or 0)
        admin_login_id = session.get("login_id") or "admin"
        return admin_user_id, admin_login_id
    except Exception:
        return 0, "admin"


def _approval_status(row):
    try:
        if row and "status" in row.keys():
            return str(row["status"] or "PENDING_APPROVAL").strip().upper()
    except Exception:
        pass

    return "PENDING_APPROVAL"


def _customer_order_url(order_code):
    return f"/orders?order_code={order_code}"


def _customer_line_target_code(order):
    customer_user_id = _row_get(order, "customer_user_id", "")
    return f"CUS-{customer_user_id}" if customer_user_id else ""


def _customer_email_for_order(order):
    return normalize_email(
        _row_get(order, "customer_email", "")
        or _row_get(order, "user_email", "")
        or _row_get(order, "email", "")
    )


def _notify_customer_payment_verified(db, *, order):
    order_code = _safe_text(_row_get(order, "order_code", ""))
    customer_email = _customer_email_for_order(order)

    try:
        if customer_email:
            send_customer_payment_verified_email(
                order,
                customer_email,
                order_url=_customer_order_url(order_code),
            )
    except Exception as exc:
        print(f"[PAYMENT][EMAIL][CUSTOMER_APPROVED][ERROR] {exc}")

    try:
        target_code = _customer_line_target_code(order)

        if target_code:
            push_to_role_target(
                db,
                role="CUSTOMER",
                target_code=target_code,
                event_type="PAYMENT_VERIFIED",
                order_code=order_code,
                message=(
                    "FUMAP GO 付款已確認\n"
                    f"訂單：{order_code}\n"
                    "您的付款已由 Admin 確認，店家將開始處理訂單。"
                ),
                commit=True,
            )
    except Exception as exc:
        print(f"[PAYMENT][LINE][CUSTOMER_APPROVED][ERROR] {exc}")


def _notify_customer_payment_rejected(db, *, order, reason=""):
    order_code = _safe_text(_row_get(order, "order_code", ""))
    customer_email = _customer_email_for_order(order)
    reason = _safe_text(reason or _row_get(order, "payment_reject_reason", ""))

    if not reason:
        reason = "轉帳付款證明需要重新確認"

    try:
        if customer_email:
            send_customer_payment_rejected_email(
                order,
                customer_email,
                reason=reason,
                order_url=_customer_order_url(order_code),
            )
    except Exception as exc:
        print(f"[PAYMENT][EMAIL][CUSTOMER_REJECTED][ERROR] {exc}")

    try:
        target_code = _customer_line_target_code(order)

        if target_code:
            push_to_role_target(
                db,
                role="CUSTOMER",
                target_code=target_code,
                event_type="PAYMENT_REJECTED",
                order_code=order_code,
                message=(
                    "FUMAP GO 轉帳證明需要重新確認\n"
                    f"訂單：{order_code}\n"
                    f"原因：{reason}\n"
                    "請查看訂單並重新上傳付款證明。"
                ),
                commit=True,
            )
    except Exception as exc:
        print(f"[PAYMENT][LINE][CUSTOMER_REJECTED][ERROR] {exc}")


def _recent_orders(db, limit=20):
    return db.execute(
        """
        SELECT o.*,
               s.store_code,
               s.store_name,
               d.driver_code,
               d.driver_name,
               u.email AS customer_email,
               u.display_name AS customer_display_name
        FROM orders o
        JOIN stores s ON s.id = o.store_id
        LEFT JOIN drivers d ON d.id = o.driver_id
        LEFT JOIN users u ON u.id = o.customer_user_id
        ORDER BY o.id DESC
        LIMIT ?
        """,
        (int(limit or 20),),
    ).fetchall()


def _order_status_counts(db):
    rows = db.execute(
        """
        SELECT status, COUNT(*) AS c
        FROM orders
        GROUP BY status
        ORDER BY status ASC
        """
    ).fetchall()

    result = {
        "CREATED": 0,
        "STORE_ACCEPTED": 0,
        "WAITING_DRIVER": 0,
        "DRIVER_ACCEPTED": 0,
        "PICKED_UP": 0,
        "DELIVERY_ISSUE": 0,
        "RETURNING_TO_STORE": 0,
        "RETURNED_TO_STORE": 0,
        "DELIVERED": 0,
        "COMPLETED": 0,
        "CANCELLED": 0,
        "DISPUTED": 0,
    }

    for row in rows:
        result[row["status"]] = int(row["c"] or 0)

    return result


def _get_order_for_admin(db, order_code):
    order_code = (order_code or "").strip().upper()

    return db.execute(
        """
        SELECT o.*,
               s.store_code,
               s.store_name,
               d.driver_code,
               d.driver_name,
               u.email AS customer_email,
               u.display_name AS customer_display_name
        FROM orders o
        JOIN stores s ON s.id = o.store_id
        LEFT JOIN drivers d ON d.id = o.driver_id
        LEFT JOIN users u ON u.id = o.customer_user_id
        WHERE o.order_code = ?
        LIMIT 1
        """,
        (order_code,),
    ).fetchone()


def _get_settlement_target(db, settlement):
    role = _safe_text(_row_get(settlement, "role", "")).upper()
    target_code = _safe_text(_row_get(settlement, "target_code", ""))

    if role == "STORE":
        return get_store_with_user(db, target_code)

    if role == "DRIVER":
        return get_driver_with_user(db, target_code)

    return None


def _settlement_line_message(settlement):
    code = _safe_text(_row_get(settlement, "settlement_code", ""))
    amount = _int(_row_get(settlement, "amount_twd", 0))
    direction = _safe_text(_row_get(settlement, "direction", ""))
    settlement_type = _safe_text(_row_get(settlement, "settlement_type", ""))

    if direction == "TARGET_OWES_ADMIN" and settlement_type == "STORE_PLATFORM_FEE":
        return (
            "FUMAP GO 店家平台費結算通知\n"
            f"結算單：{code}\n"
            f"金額：{amount} TWD\n"
            "請查看 Email 內的 Admin 收款帳戶完成付款。"
        )

    if direction == "TARGET_OWES_ADMIN" and settlement_type == "DRIVER_PLATFORM_FEE":
        return (
            "FUMAP GO Shiper 平台費結算通知\n"
            f"結算單：{code}\n"
            f"金額：{amount} TWD\n"
            "請查看 Email 內的 Admin 收款帳戶完成付款。"
        )

    if direction == "ADMIN_OWES_TARGET":
        return (
            "FUMAP GO 已轉帳通知\n"
            f"結算單：{code}\n"
            f"金額：{amount} TWD\n"
            "Admin 已處理此筆結算，請確認收款。"
        )

    return (
        "FUMAP GO 結算通知\n"
        f"結算單：{code}\n"
        f"金額：{amount} TWD"
    )


def _push_settlement_line(db, settlement, event_type):
    role = _safe_text(_row_get(settlement, "role", "")).upper()
    target_code = _safe_text(_row_get(settlement, "target_code", ""))

    if role not in {"STORE", "DRIVER"} or not target_code:
        return {"ok": False, "skipped": True, "error": "invalid target"}

    try:
        return push_to_role_target(
            db,
            role=role,
            target_code=target_code,
            event_type=event_type,
            order_code=_safe_text(_row_get(settlement, "settlement_code", "")),
            message=_settlement_line_message(settlement),
            commit=True,
        )
    except Exception as exc:
        print(f"[SETTLEMENT][LINE][ERROR] {exc}")
        return {"ok": False, "error": str(exc)}


def _calculate_settlement_amount_and_orders(
    db,
    *,
    role,
    target_code,
    direction,
    settlement_type,
    period_start,
    period_end,
):
    role = _safe_text(role).upper()
    target_code = _safe_text(target_code)
    direction = _safe_text(direction).upper()
    settlement_type = _safe_text(settlement_type).upper()

    target = None

    if role == "STORE":
        target = get_store_with_user(db, target_code)
    elif role == "DRIVER":
        target = get_driver_with_user(db, target_code)

    if not target:
        return 0, []

    if role == "STORE" and direction == "TARGET_OWES_ADMIN" and settlement_type == "STORE_PLATFORM_FEE":
        data = calculate_store_unpaid_admin_debt(
            db,
            target,
            period_start=period_start,
            period_end=period_end,
        )
        return _int(data.get("available_to_settle_twd", 0)), data.get("related_order_codes", [])

    if role == "DRIVER" and direction == "TARGET_OWES_ADMIN" and settlement_type == "DRIVER_PLATFORM_FEE":
        data = calculate_driver_unpaid_admin_debt(
            db,
            target,
            period_start=period_start,
            period_end=period_end,
        )
        return _int(data.get("available_to_settle_twd", 0)), data.get("related_order_codes", [])

    if role == "STORE" and direction == "ADMIN_OWES_TARGET" and settlement_type == "ADMIN_PAYOUT_STORE":
        data = calculate_admin_owes_store(
            db,
            target,
            period_start=period_start,
            period_end=period_end,
        )
        return _int(data.get("available_to_settle_twd", 0)), data.get("related_order_codes", [])

    if role == "DRIVER" and direction == "ADMIN_OWES_TARGET" and settlement_type == "ADMIN_PAYOUT_DRIVER":
        data = calculate_admin_owes_driver(
            db,
            target,
            period_start=period_start,
            period_end=period_end,
        )
        return _int(data.get("available_to_settle_twd", 0)), data.get("related_order_codes", [])

    return 0, []

@admin_bp.get("")
@admin_bp.get("/")
@admin_required
def dashboard():
    db = get_db()

    status_counts = _order_status_counts(db)
    rain_surcharge_enabled = is_rain_surcharge_enabled(db)

    summary = {
        "customers": _table_count(db, "users", "role = 'CUSTOMER'"),
        "stores": _table_count(db, "stores"),
        "pending_stores": _table_count(db, "stores", "status = 'PENDING_APPROVAL'"),
        "drivers": _table_count(db, "drivers"),
        "pending_drivers": _table_count(db, "drivers", "status = 'PENDING_APPROVAL'"),
        "products": _table_count(db, "products"),
        "orders": _table_count(db, "orders"),
        "held_orders": _table_count(db, "orders", "admin_hold = 1"),
        "pending_transfer_orders": _table_count(
            db,
            "orders",
            "payment_method = 'BANK_TRANSFER' AND payment_status IN ('PENDING', 'PENDING_REUPLOAD')",
        ),
        "line_bindings": _table_count(db, "line_contact_bindings", "status = 'ACTIVE'"),
        "blocks": _table_count(db, "blocks"),
        "accounting_entries": _table_count(db, "accounting_entries"),
        "total_order_twd": _sum(db, "SELECT SUM(total_twd) FROM orders"),
        "delivered_order_twd": _sum(
            db,
            """
            SELECT SUM(total_twd)
            FROM orders
            WHERE status IN ('DELIVERED', 'COMPLETED')
            """,
        ),
        "extra_fee_twd": _sum(db, "SELECT SUM(extra_fee_twd) FROM orders"),
        "rain_fee_twd": _sum(db, "SELECT SUM(rain_fee_twd) FROM orders"),
        "service_fee_twd": _sum(
            db,
            """
            SELECT SUM(service_fee_twd)
            FROM orders
            WHERE status IN ('DELIVERED', 'COMPLETED')
            """,
        ),
    }

    recent_orders = _recent_orders(db, limit=15)
    recent_blocks = db.execute(
        """
        SELECT *
        FROM blocks
        ORDER BY id DESC
        LIMIT 15
        """
    ).fetchall()

    held_orders = db.execute(
        """
        SELECT o.*,
               s.store_code,
               s.store_name,
               u.email AS customer_email
        FROM orders o
        JOIN stores s ON s.id = o.store_id
        LEFT JOIN users u ON u.id = o.customer_user_id
        WHERE o.admin_hold = 1
        ORDER BY o.id DESC
        LIMIT 10
        """
    ).fetchall()

    return render_template(
        "mobile/admin/dashboard.html",
        summary=summary,
        status_counts=status_counts,
        recent_orders=recent_orders,
        recent_blocks=recent_blocks,
        held_orders=held_orders,
        rain_surcharge_enabled=rain_surcharge_enabled,
    )


@admin_bp.get("/approvals")
@admin_required
def approvals():
    db = get_db()

    approval_stores = db.execute(
        """
        SELECT *
        FROM stores
        WHERE status IN ('PENDING_APPROVAL', 'REJECTED', 'SUSPENDED')
        ORDER BY
          CASE status
            WHEN 'PENDING_APPROVAL' THEN 1
            WHEN 'REJECTED' THEN 2
            WHEN 'SUSPENDED' THEN 3
            ELSE 9
          END,
          id DESC
        LIMIT 300
        """
    ).fetchall()

    approval_drivers = db.execute(
        """
        SELECT *
        FROM drivers
        WHERE status IN ('PENDING_APPROVAL', 'REJECTED', 'SUSPENDED')
        ORDER BY
          CASE status
            WHEN 'PENDING_APPROVAL' THEN 1
            WHEN 'REJECTED' THEN 2
            WHEN 'SUSPENDED' THEN 3
            ELSE 9
          END,
          id DESC
        LIMIT 300
        """
    ).fetchall()

    return render_template(
        "mobile/admin/approvals.html",
        approval_stores=approval_stores,
        approval_drivers=approval_drivers,
        pending_stores=approval_stores,
        pending_drivers=approval_drivers,
    )


@admin_bp.post("/flags/rain-surcharge")
@admin_required
def rain_surcharge_action():
    db = get_db()
    action = request.form.get("action", "").strip()

    try:
        if action == "enable":
            set_rain_surcharge_enabled(
                db,
                True,
                actor_role="ADMIN_OPERATOR",
                actor_code="ADMIN",
                commit=True,
            )
            flash("雨天加價 +20 TWD 已啟用。", "success")

        elif action == "disable":
            set_rain_surcharge_enabled(
                db,
                False,
                actor_role="ADMIN_OPERATOR",
                actor_code="ADMIN",
                commit=True,
            )
            flash("雨天加價已停用。", "warning")

        else:
            flash("未知操作。", "warning")

    except Exception as exc:
        db.rollback()
        flash(f"設定失敗：{exc}", "danger")

    return redirect("/admin")


@admin_bp.get("/stores")
@admin_required
def stores():
    db = get_db()

    rows = db.execute(
        """
        SELECT s.*,
               u.login_id,
               u.display_name AS owner_name,
               u.phone AS owner_phone,
               COUNT(p.id) AS product_count
        FROM stores s
        LEFT JOIN users u ON u.id = s.owner_user_id
        LEFT JOIN products p ON p.store_id = s.id
        GROUP BY s.id
        ORDER BY s.id DESC
        """
    ).fetchall()

    return render_template(
        "mobile/admin/stores.html",
        stores=rows,
    )


@admin_bp.post("/stores/<store_code>/action")
@admin_required
def store_action(store_code):
    db = get_db()
    action = request.form.get("action", "").strip()
    store_code = (store_code or "").strip().upper()

    store = db.execute(
        """
        SELECT *
        FROM stores
        WHERE store_code = ?
        LIMIT 1
        """,
        (store_code,),
    ).fetchone()

    if not store:
        flash("找不到店家。", "danger")
        return redirect("/admin/stores")

    try:
        now = now_iso()

        if action == "activate":
            db.execute(
                """
                UPDATE stores
                SET status = 'ACTIVE',
                    updated_at = ?
                WHERE store_code = ?
                """,
                (now, store_code),
            )
            new_status = "ACTIVE"
            flash("店家已啟用。", "success")

        elif action == "suspend":
            db.execute(
                """
                UPDATE stores
                SET status = 'SUSPENDED',
                    updated_at = ?
                WHERE store_code = ?
                """,
                (now, store_code),
            )
            new_status = "SUSPENDED"
            flash("店家已停用。", "warning")

        elif action == "open":
            db.execute(
                """
                UPDATE stores
                SET is_open = 1,
                    updated_at = ?
                WHERE store_code = ?
                """,
                (now, store_code),
            )
            new_status = "OPEN"
            flash("店家已設為營業中。", "success")

        elif action == "close":
            db.execute(
                """
                UPDATE stores
                SET is_open = 0,
                    updated_at = ?
                WHERE store_code = ?
                """,
                (now, store_code),
            )
            new_status = "CLOSED"
            flash("店家已設為休息中。", "warning")

        else:
            flash("未知操作。", "warning")
            return redirect("/admin/stores")

        create_block(
            db,
            event_type="ADMIN_STORE_ACTION",
            actor_role="ADMIN_OPERATOR",
            actor_code="ADMIN",
            previous_status=store["status"],
            new_status=new_status,
            payload={
                "store_code": store_code,
                "action": action,
            },
            commit=False,
        )

        db.commit()

    except Exception as exc:
        db.rollback()
        flash(f"操作失敗：{exc}", "danger")

    return redirect("/admin/stores")


@admin_bp.post("/stores/<store_code>/approval")
@admin_required
def store_approval_action(store_code):
    db = get_db()
    action = request.form.get("action", "").strip()
    reason = request.form.get("reason", "").strip()
    store_code = (store_code or "").strip().upper()

    store = db.execute(
        """
        SELECT *
        FROM stores
        WHERE store_code = ?
        LIMIT 1
        """,
        (store_code,),
    ).fetchone()

    if not store:
        flash("找不到店家。", "danger")
        return redirect("/admin/approvals")

    admin_user_id, admin_login_id = _admin_actor()
    previous_status = _approval_status(store)
    now = now_iso()

    try:
        if action == "approve":
            new_status = "ACTIVE"
            status_reason = ""
            approved_at = now
            approved_by_admin_id = admin_user_id
            event_type = "STORE_APPROVED"
            flash_message = "店家已通過審核。"
            flash_category = "success"

        elif action == "reactivate":
            new_status = "ACTIVE"
            status_reason = ""
            approved_at = now
            approved_by_admin_id = admin_user_id
            event_type = "STORE_REACTIVATED"
            flash_message = "店家已重新啟用。"
            flash_category = "success"

        elif action == "reject":
            new_status = "REJECTED"
            status_reason = reason or "Admin 未通過審核"
            approved_at = store["approved_at"] or ""
            approved_by_admin_id = int(store["approved_by_admin_id"] or 0)
            event_type = "STORE_REJECTED"
            flash_message = "店家審核未通過。"
            flash_category = "warning"

        elif action == "suspend":
            new_status = "SUSPENDED"
            status_reason = reason or "Admin 暫停店家帳號"
            approved_at = store["approved_at"] or ""
            approved_by_admin_id = int(store["approved_by_admin_id"] or 0)
            event_type = "STORE_SUSPENDED"
            flash_message = "店家帳號已暫停。"
            flash_category = "warning"

        else:
            flash("未知審核操作。", "warning")
            return redirect("/admin/approvals")

        db.execute(
            """
            UPDATE stores
            SET status = ?,
                status_reason = ?,
                approved_at = ?,
                approved_by_admin_id = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                new_status,
                status_reason,
                approved_at,
                approved_by_admin_id,
                now,
                store["id"],
            ),
        )

        create_block(
            db,
            event_type=event_type,
            actor_role="ADMIN_OPERATOR",
            actor_id=admin_user_id,
            actor_code=admin_login_id,
            previous_status=previous_status,
            new_status=new_status,
            payload={
                "store_code": store["store_code"],
                "store_name": store["store_name"],
                "previous_status": previous_status,
                "new_status": new_status,
                "reason": status_reason,
                "admin_user_id": admin_user_id,
                "admin_login_id": admin_login_id,
                "approved_at": approved_at,
            },
            commit=False,
        )

        db.commit()
        flash(flash_message, flash_category)

    except Exception as exc:
        db.rollback()
        flash(f"店家審核操作失敗：{exc}", "danger")

    return redirect("/admin/approvals")

@admin_bp.get("/drivers")
@admin_required
def drivers():
    db = get_db()

    rows = db.execute(
        """
        SELECT d.*,
               u.login_id,
               u.display_name AS user_name,
               u.phone AS user_phone,
               COUNT(o.id) AS order_count
        FROM drivers d
        LEFT JOIN users u ON u.id = d.user_id
        LEFT JOIN orders o ON o.driver_id = d.id
        GROUP BY d.id
        ORDER BY d.id DESC
        """
    ).fetchall()

    return render_template(
        "mobile/admin/drivers.html",
        drivers=rows,
    )


@admin_bp.post("/drivers/<driver_code>/action")
@admin_required
def driver_action(driver_code):
    db = get_db()
    action = request.form.get("action", "").strip()
    driver_code = (driver_code or "").strip().upper()

    driver = db.execute(
        """
        SELECT *
        FROM drivers
        WHERE driver_code = ?
        LIMIT 1
        """,
        (driver_code,),
    ).fetchone()

    if not driver:
        flash("找不到 shiper。", "danger")
        return redirect("/admin/drivers")

    try:
        now = now_iso()
        event_type = "ADMIN_DRIVER_ACTION"
        previous_status = driver["status"]
        new_status = driver["status"]
        payload = {
            "driver_code": driver_code,
            "action": action,
        }

        if action == "activate":
            db.execute(
                """
                UPDATE drivers
                SET status = 'ACTIVE',
                    updated_at = ?
                WHERE driver_code = ?
                """,
                (now, driver_code),
            )
            new_status = "ACTIVE"
            flash("Shiper 已啟用。", "success")

        elif action == "suspend":
            db.execute(
                """
                UPDATE drivers
                SET status = 'SUSPENDED',
                    is_online = 0,
                    updated_at = ?
                WHERE driver_code = ?
                """,
                (now, driver_code),
            )
            new_status = "SUSPENDED"
            flash("Shiper 已停用。", "warning")

        elif action == "online":
            db.execute(
                """
                UPDATE drivers
                SET is_online = 1,
                    updated_at = ?
                WHERE driver_code = ?
                """,
                (now, driver_code),
            )
            new_status = "ONLINE"
            flash("Shiper 已設為在線。", "success")

        elif action == "offline":
            db.execute(
                """
                UPDATE drivers
                SET is_online = 0,
                    updated_at = ?
                WHERE driver_code = ?
                """,
                (now, driver_code),
            )
            new_status = "OFFLINE"
            flash("Shiper 已設為離線。", "warning")

        elif action == "set_area_zhongli":
            city_block = "ZHONGLI"
            area_label = "中壢區"
            service_area = "中壢區"
            new_status = "AREA_ZHONGLI"
            event_type = "ADMIN_DRIVER_AREA_UPDATED"
            previous_status = driver["city_block"] or ""

            db.execute(
                """
                UPDATE drivers
                SET city_block = ?,
                    area_label = ?,
                    service_area = ?,
                    updated_at = ?
                WHERE driver_code = ?
                """,
                (
                    city_block,
                    area_label,
                    service_area,
                    now,
                    driver_code,
                ),
            )

            payload = {
                "driver_code": driver_code,
                "driver_name": driver["driver_name"],
                "old_city_block": driver["city_block"] or "",
                "old_area_label": driver["area_label"] or "",
                "new_city_block": city_block,
                "new_area_label": area_label,
                "new_service_area": service_area,
                "action": action,
            }

            flash("Shiper khu vực đã đổi thành 中壢區。", "success")

        elif action == "set_area_taoyuan":
            city_block = "TAOYUAN"
            area_label = "桃園區"
            service_area = "桃園區"
            new_status = "AREA_TAOYUAN"
            event_type = "ADMIN_DRIVER_AREA_UPDATED"
            previous_status = driver["city_block"] or ""

            db.execute(
                """
                UPDATE drivers
                SET city_block = ?,
                    area_label = ?,
                    service_area = ?,
                    updated_at = ?
                WHERE driver_code = ?
                """,
                (
                    city_block,
                    area_label,
                    service_area,
                    now,
                    driver_code,
                ),
            )

            payload = {
                "driver_code": driver_code,
                "driver_name": driver["driver_name"],
                "old_city_block": driver["city_block"] or "",
                "old_area_label": driver["area_label"] or "",
                "new_city_block": city_block,
                "new_area_label": area_label,
                "new_service_area": service_area,
                "action": action,
            }

            flash("Shiper khu vực đã đổi thành 桃園區。", "success")

        else:
            flash("未知操作。", "warning")
            return redirect("/admin/drivers")

        create_block(
            db,
            event_type=event_type,
            actor_role="ADMIN_OPERATOR",
            actor_code="ADMIN",
            previous_status=previous_status,
            new_status=new_status,
            payload=payload,
            commit=False,
        )

        db.commit()

    except Exception as exc:
        db.rollback()
        flash(f"操作失敗：{exc}", "danger")

    return redirect("/admin/drivers")


@admin_bp.post("/drivers/<driver_code>/approval")
@admin_required
def driver_approval_action(driver_code):
    db = get_db()
    action = request.form.get("action", "").strip()
    reason = request.form.get("reason", "").strip()
    driver_code = (driver_code or "").strip().upper()

    driver = db.execute(
        """
        SELECT *
        FROM drivers
        WHERE driver_code = ?
        LIMIT 1
        """,
        (driver_code,),
    ).fetchone()

    if not driver:
        flash("找不到 Shiper。", "danger")
        return redirect("/admin/approvals")

    admin_user_id, admin_login_id = _admin_actor()
    previous_status = _approval_status(driver)
    now = now_iso()

    try:
        if action == "approve":
            new_status = "ACTIVE"
            status_reason = ""
            approved_at = now
            approved_by_admin_id = admin_user_id
            event_type = "DRIVER_APPROVED"
            flash_message = "Shiper 已通過審核。"
            flash_category = "success"

        elif action == "reactivate":
            new_status = "ACTIVE"
            status_reason = ""
            approved_at = now
            approved_by_admin_id = admin_user_id
            event_type = "DRIVER_REACTIVATED"
            flash_message = "Shiper 已重新啟用。"
            flash_category = "success"

        elif action == "reject":
            new_status = "REJECTED"
            status_reason = reason or "Admin 未通過審核"
            approved_at = driver["approved_at"] or ""
            approved_by_admin_id = int(driver["approved_by_admin_id"] or 0)
            event_type = "DRIVER_REJECTED"
            flash_message = "Shiper 審核未通過。"
            flash_category = "warning"

        elif action == "suspend":
            new_status = "SUSPENDED"
            status_reason = reason or "Admin 暫停 Shiper 帳號"
            approved_at = driver["approved_at"] or ""
            approved_by_admin_id = int(driver["approved_by_admin_id"] or 0)
            event_type = "DRIVER_SUSPENDED"
            flash_message = "Shiper 帳號已暫停。"
            flash_category = "warning"

        else:
            flash("未知審核操作。", "warning")
            return redirect("/admin/approvals")

        db.execute(
            """
            UPDATE drivers
            SET status = ?,
                status_reason = ?,
                approved_at = ?,
                approved_by_admin_id = ?,
                is_online = 0,
                updated_at = ?
            WHERE id = ?
            """,
            (
                new_status,
                status_reason,
                approved_at,
                approved_by_admin_id,
                now,
                driver["id"],
            ),
        )

        create_block(
            db,
            event_type=event_type,
            actor_role="ADMIN_OPERATOR",
            actor_id=admin_user_id,
            actor_code=admin_login_id,
            previous_status=previous_status,
            new_status=new_status,
            payload={
                "driver_code": driver["driver_code"],
                "driver_name": driver["driver_name"],
                "previous_status": previous_status,
                "new_status": new_status,
                "reason": status_reason,
                "admin_user_id": admin_user_id,
                "admin_login_id": admin_login_id,
                "approved_at": approved_at,
                "is_online_after": 0,
            },
            commit=False,
        )

        db.commit()
        flash(flash_message, flash_category)

    except Exception as exc:
        db.rollback()
        flash(f"Shiper 審核操作失敗：{exc}", "danger")

    return redirect("/admin/approvals")


@admin_bp.get("/orders")
@admin_required
def orders():
    db = get_db()
    status = (request.args.get("status") or "").strip().upper()
    hold = (request.args.get("hold") or "").strip()
    order_code = (request.args.get("order_code") or "").strip().upper()

    params = []
    where = []

    if status:
        where.append("o.status = ?")
        params.append(status)

    if order_code:
        where.append("o.order_code = ?")
        params.append(order_code)

    if hold == "1":
        where.append("o.admin_hold = 1")

    if hold == "0":
        where.append("COALESCE(o.admin_hold, 0) = 0")

    where_sql = ""

    if where:
        where_sql = "WHERE " + " AND ".join(where)

    rows = db.execute(
        f"""
        SELECT o.*,
               s.store_code,
               s.store_name,
               d.driver_code,
               d.driver_name,
               u.email AS customer_email,
               u.display_name AS customer_display_name
        FROM orders o
        JOIN stores s ON s.id = o.store_id
        LEFT JOIN drivers d ON d.id = o.driver_id
        LEFT JOIN users u ON u.id = o.customer_user_id
        {where_sql}
        ORDER BY
          CASE WHEN o.admin_hold = 1 THEN 0 ELSE 1 END,
          o.id DESC
        LIMIT 300
        """,
        params,
    ).fetchall()

    status_counts = _order_status_counts(db)
    rain_surcharge_enabled = is_rain_surcharge_enabled(db)

    hold_count = _table_count(db, "orders", "admin_hold = 1")
    pending_transfer_count = _table_count(
        db,
        "orders",
        "payment_method = 'BANK_TRANSFER' AND payment_status IN ('PENDING', 'PENDING_REUPLOAD')",
    )

    return render_template(
        "mobile/admin/orders.html",
        orders=rows,
        status=status,
        hold=hold,
        order_code=order_code,
        status_counts=status_counts,
        rain_surcharge_enabled=rain_surcharge_enabled,
        hold_count=hold_count,
        pending_transfer_count=pending_transfer_count,
    )

@admin_bp.post("/orders/<order_code>/action")
@admin_required
def order_action(order_code):
    db = get_db()
    action = request.form.get("action", "").strip()
    hold_reason = request.form.get("hold_reason", "").strip()
    reject_reason = request.form.get("reject_reason", "").strip()
    order_code = (order_code or "").strip().upper()

    order = _get_order_for_admin(db, order_code)

    if not order:
        flash("找不到訂單。", "danger")
        return redirect("/admin/orders")

    notify_customer_verified = False
    notify_customer_rejected = False
    reject_notify_reason = ""

    try:
        now = now_iso()
        old_status = order["status"]
        new_status = old_status
        event_type = "ADMIN_ORDER_ACTION"
        admin_user_id, admin_login_id = _admin_actor()

        if action in {"approve_payment", "mark_paid"}:
            db.execute(
                """
                UPDATE orders
                SET payment_status = 'PAID',
                    payment_proof_status = CASE
                        WHEN payment_method = 'BANK_TRANSFER' THEN 'APPROVED'
                        ELSE COALESCE(NULLIF(payment_proof_status, ''), 'APPROVED')
                    END,
                    payment_proof_reviewed_at = ?,
                    payment_proof_reviewed_by_admin_id = ?,
                    payment_verified_at = ?,
                    payment_verified_by = ?,
                    payment_rejected_at = '',
                    payment_reject_reason = '',
                    admin_hold = CASE
                        WHEN payment_method = 'BANK_TRANSFER' THEN 0
                        ELSE admin_hold
                    END,
                    admin_hold_reason = CASE
                        WHEN payment_method = 'BANK_TRANSFER' THEN ''
                        ELSE COALESCE(admin_hold_reason, '')
                    END,
                    admin_hold_at = CASE
                        WHEN payment_method = 'BANK_TRANSFER' THEN ''
                        ELSE COALESCE(admin_hold_at, '')
                    END,
                    updated_at = ?
                WHERE order_code = ?
                """,
                (
                    now,
                    admin_user_id,
                    now,
                    admin_user_id,
                    now,
                    order_code,
                ),
            )

            event_type = "PAYMENT_VERIFIED" if action == "approve_payment" else "PAYMENT_UPDATED"
            new_status = "PAYMENT_VERIFIED"
            notify_customer_verified = bool(order["payment_method"] == "BANK_TRANSFER")
            flash("付款已確認，轉帳訂單已解除暫停。", "success")

        elif action == "reject_payment":
            if order["payment_method"] != "BANK_TRANSFER":
                flash("只有轉帳訂單可以退回付款證明。", "warning")
                return redirect(f"/admin/orders?order_code={order_code}#{order_code}")

            reject_notify_reason = reject_reason or hold_reason or "轉帳付款證明需要重新確認"

            db.execute(
                """
                UPDATE orders
                SET payment_status = 'PENDING_REUPLOAD',
                    payment_proof_status = 'REJECTED',
                    payment_rejected_at = ?,
                    payment_reject_reason = ?,
                    payment_verified_at = '',
                    payment_verified_by = NULL,
                    admin_hold = 1,
                    admin_hold_reason = '轉帳付款證明需重新上傳',
                    admin_hold_at = COALESCE(NULLIF(admin_hold_at, ''), ?),
                    updated_at = ?
                WHERE order_code = ?
                """,
                (
                    now,
                    reject_notify_reason,
                    now,
                    now,
                    order_code,
                ),
            )

            event_type = "PAYMENT_REJECTED"
            new_status = "PAYMENT_REJECTED"
            notify_customer_rejected = True
            flash("已要求客戶重新上傳轉帳證明。", "warning")

        elif action == "complete":
            if old_status not in {"DELIVERED", "COMPLETED"}:
                flash("只有 DELIVERED 訂單可以完成對帳。", "warning")
                return redirect("/admin/orders")

            db.execute(
                """
                UPDATE orders
                SET status = 'COMPLETED',
                    payment_status = CASE
                        WHEN payment_status IN ('UNPAID', 'PENDING', 'PENDING_REUPLOAD') THEN 'PAID'
                        ELSE payment_status
                    END,
                    admin_hold = 0,
                    admin_hold_reason = '',
                    admin_hold_at = '',
                    updated_at = ?
                WHERE order_code = ?
                """,
                (now, order_code),
            )
            new_status = "COMPLETED"
            event_type = "ORDER_COMPLETED"
            flash("訂單已完成。", "success")

        elif action == "dispute":
            db.execute(
                """
                UPDATE orders
                SET status = 'DISPUTED',
                    updated_at = ?
                WHERE order_code = ?
                """,
                (now, order_code),
            )
            new_status = "DISPUTED"
            event_type = "DISPUTE_OPENED"
            flash("訂單已標記為爭議。", "warning")

        elif action == "cancel":
            if old_status in {"DELIVERED", "COMPLETED"}:
                flash("已送達或已完成訂單不能直接取消。", "warning")
                return redirect("/admin/orders")

            db.execute(
                """
                UPDATE orders
                SET status = 'CANCELLED',
                    admin_hold = 0,
                    updated_at = ?
                WHERE order_code = ?
                """,
                (now, order_code),
            )
            new_status = "CANCELLED"
            event_type = "ORDER_CANCELLED"
            flash("訂單已取消。", "warning")

        elif action == "hold":
            reason = hold_reason or "付款或訂單資料需人工確認"

            db.execute(
                """
                UPDATE orders
                SET admin_hold = 1,
                    admin_hold_reason = ?,
                    admin_hold_at = ?,
                    updated_at = ?
                WHERE order_code = ?
                """,
                (reason, now, now, order_code),
            )
            new_status = "ADMIN_HOLD"
            event_type = "ORDER_ADMIN_HOLD"
            flash("訂單已暫停。店家會看到警示，不能繼續製作或呼叫 shiper。", "warning")

        elif action == "release_hold":
            db.execute(
                """
                UPDATE orders
                SET admin_hold = 0,
                    admin_hold_reason = '',
                    admin_hold_at = '',
                    updated_at = ?
                WHERE order_code = ?
                """,
                (now, order_code),
            )
            new_status = "ADMIN_RELEASED"
            event_type = "ORDER_ADMIN_RELEASED"
            flash("訂單已解除暫停。", "success")

        else:
            flash("未知操作。", "warning")
            return redirect("/admin/orders")

        create_block(
            db,
            event_type=event_type,
            actor_role="ADMIN_OPERATOR",
            actor_id=admin_user_id,
            actor_code=admin_login_id,
            order_id=order["id"],
            order_code=order["order_code"],
            previous_status=old_status,
            new_status=new_status,
            amount_twd=order["total_twd"],
            payload={
                "action": action,
                "order_code": order_code,
                "store_code": order["store_code"],
                "driver_code": order["driver_code"] or "",
                "customer_user_id": order["customer_user_id"],
                "customer_email": _customer_email_for_order(order),
                "payment_method": order["payment_method"],
                "payment_status_before": order["payment_status"],
                "admin_hold_before": int(order["admin_hold"] or 0),
                "hold_reason": hold_reason,
                "reject_reason": reject_notify_reason,
                "admin_user_id": admin_user_id,
                "admin_login_id": admin_login_id,
            },
            commit=False,
        )

        db.commit()

        updated_order = _get_order_for_admin(db, order_code)

        if notify_customer_verified and updated_order:
            _notify_customer_payment_verified(db, order=updated_order)

        if notify_customer_rejected and updated_order:
            _notify_customer_payment_rejected(
                db,
                order=updated_order,
                reason=reject_notify_reason,
            )

    except Exception as exc:
        db.rollback()
        flash(f"操作失敗：{exc}", "danger")

    return redirect(f"/admin/orders?order_code={order_code}#{order_code}")


@admin_bp.get("/settlements")
@admin_required
def settlements():
    db = get_db()

    dashboard = list_admin_settlement_dashboard(db)

    return render_template(
        "mobile/admin/settlements.html",
        dashboard=dashboard,
        summary=dashboard.get("summary", {}),
        stores=dashboard.get("stores", []),
        drivers=dashboard.get("drivers", []),
        recent_settlements=dashboard.get("recent_settlements", []),
        admin_payment_info=dashboard.get("admin_payment_info", {}),
        period_start=dashboard.get("period_start", ""),
        period_end=dashboard.get("period_end", ""),
    )


@admin_bp.post("/settlements/create")
@admin_required
def settlement_create():
    db = get_db()

    role = _safe_text(request.form.get("role", "")).upper()
    target_code = _safe_text(request.form.get("target_code", "")).upper()
    direction = _safe_text(request.form.get("direction", "")).upper()
    settlement_type = _safe_text(request.form.get("settlement_type", "")).upper()
    payment_method = _safe_text(request.form.get("payment_method", "BANK_TRANSFER")).upper()
    note = _safe_text(request.form.get("note", ""))
    period_start = _safe_text(request.form.get("period_start", ""))
    period_end = _safe_text(request.form.get("period_end", ""))
    form_amount = _int(request.form.get("amount_twd", 0))

    if not period_start or not period_end:
        period_start, period_end = current_month_range()

    try:
        calculated_amount, related_order_codes = _calculate_settlement_amount_and_orders(
            db,
            role=role,
            target_code=target_code,
            direction=direction,
            settlement_type=settlement_type,
            period_start=period_start,
            period_end=period_end,
        )

        amount_twd = form_amount if form_amount > 0 else calculated_amount

        if amount_twd <= 0:
            flash("結算金額為 0，不能建立結算單。", "warning")
            return redirect("/admin/settlements")

        admin_user_id, admin_login_id = _admin_actor()

        settlement = create_settlement_batch(
            db,
            role=role,
            target_code=target_code,
            direction=direction,
            settlement_type=settlement_type,
            amount_twd=amount_twd,
            period_start=period_start,
            period_end=period_end,
            related_order_codes=related_order_codes,
            payment_method=payment_method,
            note=note,
            commit=False,
        )

        create_block(
            db,
            event_type="SETTLEMENT_BATCH_CREATED",
            actor_role="ADMIN_OPERATOR",
            actor_id=admin_user_id,
            actor_code=admin_login_id,
            amount_twd=settlement["amount_twd"],
            payload={
                "settlement_code": settlement["settlement_code"],
                "role": role,
                "target_code": target_code,
                "direction": direction,
                "settlement_type": settlement_type,
                "amount_twd": settlement["amount_twd"],
                "period_start": period_start,
                "period_end": period_end,
                "status": settlement["status"],
                "related_order_codes": related_order_codes,
                "admin_user_id": admin_user_id,
                "admin_login_id": admin_login_id,
            },
            commit=False,
        )

        db.commit()
        flash(f"已建立結算單：{settlement['settlement_code']}", "success")

    except Exception as exc:
        db.rollback()
        flash(f"建立結算單失敗：{exc}", "danger")

    return redirect("/admin/settlements")


@admin_bp.post("/settlements/<settlement_code>/send-email")
@admin_required
def settlement_send_email(settlement_code):
    db = get_db()
    settlement_code = _safe_text(settlement_code).upper()

    try:
        settlement = get_settlement_by_code(db, settlement_code)

        if not settlement:
            flash("找不到結算單。", "danger")
            return redirect("/admin/settlements")

        if _int(settlement.get("amount_twd", 0)) <= 0:
            flash("結算金額為 0，不能發送通知。", "warning")
            return redirect("/admin/settlements")

        target = _get_settlement_target(db, settlement)

        if not target:
            flash("找不到結算對象。", "danger")
            return redirect("/admin/settlements")

        target_email = normalize_email(_row_get(target, "email", ""))

        if not target_email:
            flash("對象尚未設定 Email，不能發送通知。", "warning")
            return redirect("/admin/settlements")

        direction = _safe_text(settlement.get("direction", "")).upper()
        settlement_type = _safe_text(settlement.get("settlement_type", "")).upper()
        role = _safe_text(settlement.get("role", "")).upper()

        admin_payment_info = snapshot_admin_payment_info()
        email_ok = False

        if role == "STORE" and direction == "TARGET_OWES_ADMIN" and settlement_type == "STORE_PLATFORM_FEE":
            email_ok = send_store_payment_request_email(
                target,
                settlement,
                admin_payment_info,
            )

        elif role == "DRIVER" and direction == "TARGET_OWES_ADMIN" and settlement_type == "DRIVER_PLATFORM_FEE":
            email_ok = send_driver_payment_request_email(
                target,
                settlement,
                admin_payment_info,
            )

        elif role == "STORE" and direction == "ADMIN_OWES_TARGET" and settlement_type == "ADMIN_PAYOUT_STORE":
            email_ok = send_store_payout_confirmed_email(
                target,
                settlement,
            )

        elif role == "DRIVER" and direction == "ADMIN_OWES_TARGET" and settlement_type == "ADMIN_PAYOUT_DRIVER":
            email_ok = send_driver_payout_confirmed_email(
                target,
                settlement,
            )

        else:
            flash("結算類型不支援發送 Email。", "warning")
            return redirect("/admin/settlements")

        if not email_ok:
            flash("Email 發送失敗，結算單狀態未更新。", "danger")
            return redirect("/admin/settlements")

        admin_user_id, admin_login_id = _admin_actor()
        settlement = mark_settlement_email_sent(db, settlement_code, commit=False)

        create_block(
            db,
            event_type="SETTLEMENT_EMAIL_SENT",
            actor_role="ADMIN_OPERATOR",
            actor_id=admin_user_id,
            actor_code=admin_login_id,
            amount_twd=settlement["amount_twd"],
            payload={
                "settlement_code": settlement["settlement_code"],
                "role": settlement["role"],
                "target_code": settlement["target_code"],
                "target_email": target_email,
                "direction": settlement["direction"],
                "settlement_type": settlement["settlement_type"],
                "amount_twd": settlement["amount_twd"],
                "email_sent_at": settlement["email_sent_at"],
                "status": settlement["status"],
            },
            commit=False,
        )

        db.commit()

        _push_settlement_line(
            db,
            settlement,
            event_type="SETTLEMENT_EMAIL_SENT",
        )

        flash("Email 已發送；若對象已綁定 LINE，系統也會推送 LINE 通知。", "success")

    except Exception as exc:
        db.rollback()
        flash(f"發送結算通知失敗：{exc}", "danger")

    return redirect("/admin/settlements")

@admin_bp.post("/settlements/<settlement_code>/confirm-paid")
@admin_required
def settlement_confirm_paid(settlement_code):
    db = get_db()
    settlement_code = _safe_text(settlement_code).upper()
    payment_method = _safe_text(request.form.get("payment_method", "BANK_TRANSFER")).upper()
    note = _safe_text(request.form.get("note", ""))

    try:
        old_settlement = get_settlement_by_code(db, settlement_code)

        if not old_settlement:
            flash("找不到結算單。", "danger")
            return redirect("/admin/settlements")

        if _int(old_settlement.get("amount_twd", 0)) <= 0:
            flash("結算金額為 0，不能確認。", "warning")
            return redirect("/admin/settlements")

        admin_user_id, admin_login_id = _admin_actor()

        settlement = confirm_settlement_paid(
            db,
            settlement_code,
            admin_user_id=admin_user_id,
            payment_method=payment_method,
            note=note,
            commit=False,
        )

        create_block(
            db,
            event_type="SETTLEMENT_PAID_CONFIRMED",
            actor_role="ADMIN_OPERATOR",
            actor_id=admin_user_id,
            actor_code=admin_login_id,
            amount_twd=settlement["amount_twd"],
            payload={
                "settlement_code": settlement["settlement_code"],
                "role": settlement["role"],
                "target_code": settlement["target_code"],
                "target_email": settlement["target_email"],
                "direction": settlement["direction"],
                "settlement_type": settlement["settlement_type"],
                "amount_twd": settlement["amount_twd"],
                "payment_method": payment_method,
                "paid_confirmed_at": settlement["paid_confirmed_at"],
                "paid_confirmed_by": admin_user_id,
                "status": settlement["status"],
                "note": note,
            },
            commit=False,
        )

        db.commit()

        target = _get_settlement_target(db, settlement)
        direction = _safe_text(settlement.get("direction", "")).upper()
        role = _safe_text(settlement.get("role", "")).upper()
        settlement_type = _safe_text(settlement.get("settlement_type", "")).upper()

        if target and direction == "ADMIN_OWES_TARGET":
            try:
                if role == "STORE" and settlement_type == "ADMIN_PAYOUT_STORE":
                    send_store_payout_confirmed_email(target, settlement)
                elif role == "DRIVER" and settlement_type == "ADMIN_PAYOUT_DRIVER":
                    send_driver_payout_confirmed_email(target, settlement)
            except Exception as exc:
                print(f"[SETTLEMENT][CONFIRM_EMAIL][ERROR] {exc}")

        _push_settlement_line(
            db,
            settlement,
            event_type="SETTLEMENT_PAID_CONFIRMED",
        )

        flash("已確認結算完成。未結算金額會依 PAID_CONFIRMED 自動扣除。", "success")

    except Exception as exc:
        db.rollback()
        flash(f"確認結算失敗：{exc}", "danger")

    return redirect("/admin/settlements")


@admin_bp.get("/accounting")
@admin_required
def accounting():
    from services.accounting_service import (
        admin_accounting_summary,
        list_admin_accounting_entries,
    )

    db = get_db()

    summary = admin_accounting_summary(db)
    entries = list_admin_accounting_entries(db, limit=500)

    order_summary = db.execute(
        """
        SELECT payment_method,
               payment_status,
               COUNT(*) AS count_orders,
               SUM(total_twd) AS total_twd,
               SUM(subtotal_twd) AS subtotal_twd,
               SUM(base_delivery_fee_twd) AS base_delivery_fee_twd,
               SUM(customer_delivery_share_twd) AS customer_delivery_share_twd,
               SUM(store_delivery_support_twd) AS store_delivery_support_twd,
               SUM(extra_fee_twd) AS extra_fee_twd,
               SUM(rain_fee_twd) AS rain_fee_twd
        FROM orders
        GROUP BY payment_method, payment_status
        ORDER BY payment_method ASC, payment_status ASC
        """
    ).fetchall()

    store_rows = db.execute(
        """
        SELECT ae.target_code AS store_code,
               COALESCE(s.store_name, '') AS store_name,
               SUM(CASE WHEN ae.entry_type = 'STORE_GROSS_SALE' THEN ae.amount_twd ELSE 0 END) AS store_sales_twd,
               SUM(CASE WHEN ae.entry_type = 'STORE_DELIVERY_SUPPORT' THEN ae.amount_twd ELSE 0 END) AS store_delivery_support_twd,
               SUM(CASE WHEN ae.entry_type = 'STORE_PLATFORM_FEE' THEN ae.amount_twd ELSE 0 END) AS store_platform_fee_twd,
               SUM(CASE WHEN ae.entry_type = 'STORE_NET_RECEIVABLE' THEN ae.amount_twd ELSE 0 END) AS store_net_receivable_twd,
               SUM(CASE WHEN ae.entry_type = 'COD_DRIVER_PAY_STORE_NET' THEN ae.amount_twd ELSE 0 END) AS receivable_from_driver_twd,
               SUM(CASE WHEN ae.entry_type = 'PLATFORM_PAY_STORE_NET' THEN ae.amount_twd ELSE 0 END) AS receivable_from_platform_twd,
               SUM(CASE WHEN ae.entry_type = 'PREPAID_STORE_RECEIVED_SUBTOTAL' THEN ae.amount_twd ELSE 0 END) AS prepaid_received_twd,
               COUNT(*) AS entry_count
        FROM accounting_entries ae
        LEFT JOIN stores s ON s.store_code = ae.target_code
        WHERE ae.role = 'STORE'
        GROUP BY ae.target_code, s.store_name
        ORDER BY store_net_receivable_twd DESC, ae.target_code ASC
        LIMIT 200
        """
    ).fetchall()

    driver_rows = db.execute(
        """
        SELECT ae.target_code AS driver_code,
               COALESCE(d.driver_name, '') AS driver_name,
               SUM(CASE WHEN ae.entry_type = 'DRIVER_GROSS_EARNING' THEN ae.amount_twd ELSE 0 END) AS driver_gross_twd,
               SUM(CASE WHEN ae.entry_type = 'DRIVER_PLATFORM_FEE' THEN ae.amount_twd ELSE 0 END) AS driver_platform_fee_twd,
               SUM(CASE WHEN ae.entry_type = 'DRIVER_NET_EARNING' THEN ae.amount_twd ELSE 0 END) AS driver_net_income_twd,
               SUM(CASE WHEN ae.entry_type = 'COD_CUSTOMER_PAID_DRIVER' THEN ae.amount_twd ELSE 0 END) AS cod_collected_twd,
               SUM(CASE WHEN ae.entry_type = 'COD_DRIVER_PAY_STORE_NET' THEN ae.amount_twd ELSE 0 END) AS pay_store_twd,
               SUM(CASE WHEN ae.entry_type = 'COD_DRIVER_PAY_ADMIN' THEN ae.amount_twd ELSE 0 END) AS pay_admin_twd,
               SUM(CASE WHEN ae.entry_type = 'PLATFORM_PAY_DRIVER_NET' THEN ae.amount_twd ELSE 0 END) AS platform_pay_driver_twd,
               COUNT(*) AS entry_count
        FROM accounting_entries ae
        LEFT JOIN drivers d ON d.driver_code = ae.target_code
        WHERE ae.role = 'DRIVER'
        GROUP BY ae.target_code, d.driver_name
        ORDER BY pay_admin_twd DESC, ae.target_code ASC
        LIMIT 200
        """
    ).fetchall()

    manual_review_orders = db.execute(
        """
        SELECT o.*,
               s.store_code,
               s.store_name,
               d.driver_code,
               d.driver_name
        FROM orders o
        JOIN stores s ON s.id = o.store_id
        LEFT JOIN drivers d ON d.id = o.driver_id
        WHERE COALESCE(o.admin_hold, 0) = 1
           OR o.payment_status IN ('PENDING', 'PENDING_REUPLOAD')
           OR o.status = 'DISPUTED'
           OR o.payment_method = 'PREPAID_TO_STORE'
        ORDER BY o.id DESC
        LIMIT 100
        """
    ).fetchall()

    recent_entries = db.execute(
        """
        SELECT *
        FROM accounting_entries
        ORDER BY id DESC
        LIMIT 300
        """
    ).fetchall()

    return render_template(
        "mobile/admin/accounting.html",
        summary=summary,
        entries=entries,
        recent_entries=recent_entries,
        order_summary=order_summary,
        store_rows=store_rows,
        driver_rows=driver_rows,
        manual_review_orders=manual_review_orders,
    )


@admin_bp.get("/blocks")
@admin_required
def blocks():
    db = get_db()

    event_type = (request.args.get("event_type") or "").strip()
    order_code = (request.args.get("order_code") or "").strip().upper()

    where = []
    params = []

    if event_type:
        where.append("event_type = ?")
        params.append(event_type)

    if order_code:
        where.append("order_code = ?")
        params.append(order_code)

    where_sql = ""

    if where:
        where_sql = "WHERE " + " AND ".join(where)

    rows = db.execute(
        f"""
        SELECT *
        FROM blocks
        {where_sql}
        ORDER BY id DESC
        LIMIT 500
        """,
        params,
    ).fetchall()

    event_types = db.execute(
        """
        SELECT event_type, COUNT(*) AS c
        FROM blocks
        GROUP BY event_type
        ORDER BY c DESC, event_type ASC
        """
    ).fetchall()

    return render_template(
        "mobile/admin/blocks.html",
        blocks=rows,
        event_types=event_types,
        selected_event_type=event_type,
        selected_order_code=order_code,
    )

from flask import Blueprint, render_template, request, redirect, flash, jsonify

from db import get_db
from services.code_service import now_iso
from services.system_flag_service import is_rain_surcharge_enabled
from services.permission_service import (
    login_required,
    role_required,
    current_user,
    get_current_store,
)
from services.block_service import create_block
from services.image_service import ImageUploadError, maybe_save_compressed_upload
from services.store_hours_service import (
    normalize_hhmm,
    normalize_open_days_from_form,
    dump_open_days,
    annotate_store_hours,
)
from services.order_service import (
    OrderError,
    create_store_manual_delivery_order,
    list_store_orders,
    list_store_products,
    store_accept_order,
    store_call_driver,
    cancel_order,
    normalize_city_block,
    area_label_for_city_block,
    get_order_by_code,
    get_order_items,
)


store_bp = Blueprint("store", __name__, url_prefix="/store")

STORE_HOME_ORDER_LIMIT = 80
STORE_ORDERS_PAGE_LIMIT = 150


def _int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def _float(value, default=0.0):
    try:
        value = str(value or "").strip()
        if not value:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _require_store_or_redirect():
    store = get_current_store()

    if not store:
        flash("找不到店家資料，請重新登入店家帳號。", "danger")
        return None

    return store


def _approval_status(row):
    try:
        if row and "status" in row.keys():
            return str(row["status"] or "PENDING_APPROVAL").strip().upper()
    except Exception:
        pass

    return "PENDING_APPROVAL"


def _store_is_active(store):
    return bool(store and _approval_status(store) == "ACTIVE")


def _store_pending_response(store):
    return render_template(
        "mobile/store/pending.html",
        store=store,
        status=_approval_status(store),
    )


def _store_inactive_realtime_payload(store):
    status = _approval_status(store)

    return {
        "ok": True,
        "role": "STORE",
        "should_ring": False,
        "message": "",
        "target_url": "/store",
        "new_orders": 0,
        "accepted_orders": 0,
        "waiting_driver_orders": 0,
        "delivery_orders": 0,
        "held_orders": 0,
        "latest_order_code": "",
        "latest_customer_name": "",
        "latest_order_status": "",
        "latest_payment_method": "",
        "latest_payment_status": "",
        "latest_total_twd": 0,
        "approval_required": True,
        "store_status": status,
        "server_time": now_iso(),
    }


def _store_dashboard_summary(db, store):
    today_prefix = now_iso()[:10]

    row = db.execute(
        """
        SELECT
            SUM(CASE WHEN status = 'CREATED' THEN 1 ELSE 0 END) AS new_orders,
            SUM(CASE WHEN status = 'STORE_ACCEPTED' THEN 1 ELSE 0 END) AS accepted_orders,
            SUM(CASE WHEN status = 'WAITING_DRIVER' THEN 1 ELSE 0 END) AS waiting_driver,
            SUM(CASE WHEN status IN ('DELIVERED', 'COMPLETED') THEN 1 ELSE 0 END) AS delivered_orders,
            SUM(
                CASE
                    WHEN status IN ('DELIVERED', 'COMPLETED')
                     AND substr(COALESCE(updated_at, created_at), 1, 10) = ?
                    THEN COALESCE(subtotal_twd, 0)
                    ELSE 0
                END
            ) AS today_sales_twd,
            SUM(CASE WHEN payment_method = 'COD' THEN 1 ELSE 0 END) AS cod_orders,
            SUM(CASE WHEN payment_method IN ('BANK_TRANSFER', 'PLATFORM') THEN 1 ELSE 0 END) AS platform_orders,
            SUM(CASE WHEN payment_status = 'PENDING' THEN 1 ELSE 0 END) AS pending_payment_orders
        FROM orders
        WHERE store_id = ?
        """,
        (
            today_prefix,
            store["id"],
        ),
    ).fetchone()

    if not row:
        return {
            "new_orders": 0,
            "accepted_orders": 0,
            "waiting_driver": 0,
            "delivered_orders": 0,
            "today_sales_twd": 0,
            "cod_orders": 0,
            "platform_orders": 0,
            "pending_payment_orders": 0,
        }

    return {
        "new_orders": int(row["new_orders"] or 0),
        "accepted_orders": int(row["accepted_orders"] or 0),
        "waiting_driver": int(row["waiting_driver"] or 0),
        "delivered_orders": int(row["delivered_orders"] or 0),
        "today_sales_twd": int(row["today_sales_twd"] or 0),
        "cod_orders": int(row["cod_orders"] or 0),
        "platform_orders": int(row["platform_orders"] or 0),
        "pending_payment_orders": int(row["pending_payment_orders"] or 0),
    }


def _store_realtime_payload(db, store):
    row = db.execute(
        """
        SELECT
            SUM(CASE WHEN status = 'CREATED' AND COALESCE(admin_hold, 0) = 0 THEN 1 ELSE 0 END) AS new_orders,
            SUM(CASE WHEN status = 'STORE_ACCEPTED' AND COALESCE(admin_hold, 0) = 0 THEN 1 ELSE 0 END) AS accepted_orders,
            SUM(CASE WHEN status = 'WAITING_DRIVER' AND COALESCE(admin_hold, 0) = 0 THEN 1 ELSE 0 END) AS waiting_driver_orders,
            SUM(CASE WHEN status IN ('DRIVER_ACCEPTED', 'PICKED_UP') THEN 1 ELSE 0 END) AS delivery_orders,
            SUM(CASE WHEN COALESCE(admin_hold, 0) = 1 THEN 1 ELSE 0 END) AS held_orders
        FROM orders
        WHERE store_id = ?
        """,
        (store["id"],),
    ).fetchone()

    latest = db.execute(
        """
        SELECT
            order_code,
            status,
            customer_name,
            payment_method,
            payment_status,
            total_twd,
            created_at
        FROM orders
        WHERE store_id = ?
          AND status = 'CREATED'
          AND COALESCE(admin_hold, 0) = 0
        ORDER BY id DESC
        LIMIT 1
        """,
        (store["id"],),
    ).fetchone()

    new_orders = int(row["new_orders"] or 0) if row else 0
    accepted_orders = int(row["accepted_orders"] or 0) if row else 0
    waiting_driver_orders = int(row["waiting_driver_orders"] or 0) if row else 0
    delivery_orders = int(row["delivery_orders"] or 0) if row else 0
    held_orders = int(row["held_orders"] or 0) if row else 0

    latest_order_code = latest["order_code"] if latest else ""

    return {
        "ok": True,
        "role": "STORE",
        "should_ring": new_orders > 0,
        "message": "有新訂單" if new_orders > 0 else "",
        "target_url": f"/store#{latest_order_code}" if latest_order_code else "/store",
        "new_orders": new_orders,
        "accepted_orders": accepted_orders,
        "waiting_driver_orders": waiting_driver_orders,
        "delivery_orders": delivery_orders,
        "held_orders": held_orders,
        "latest_order_code": latest_order_code,
        "latest_customer_name": latest["customer_name"] if latest else "",
        "latest_order_status": latest["status"] if latest else "",
        "latest_payment_method": latest["payment_method"] if latest else "",
        "latest_payment_status": latest["payment_status"] if latest else "",
        "latest_total_twd": int(latest["total_twd"] or 0) if latest else 0,
        "server_time": now_iso(),
    }


def _attach_order_items(db, orders):
    order_list = [dict(o) for o in orders]

    if not order_list:
        return []

    order_ids = [
        int(o["id"])
        for o in order_list
        if o.get("id") is not None
    ]

    if not order_ids:
        for o in order_list:
            o["items"] = []
        return order_list

    placeholders = ",".join(["?"] * len(order_ids))

    try:
        item_rows = db.execute(
            f"""
            SELECT *
            FROM order_items
            WHERE order_id IN ({placeholders})
            ORDER BY order_id ASC, id ASC
            """,
            order_ids,
        ).fetchall()
    except Exception:
        item_rows = []

    grouped = {}

    for item in item_rows:
        order_id = int(item["order_id"] or 0)
        grouped.setdefault(order_id, []).append(item)

    for o in order_list:
        o["items"] = grouped.get(int(o["id"] or 0), [])

    return order_list


@store_bp.get("")
@store_bp.get("/")
@login_required
@role_required("STORE")
def home():
    db = get_db()
    store = _require_store_or_redirect()

    if not store:
        return redirect("/login")

    if not _store_is_active(store):
        return _store_pending_response(store)

    rows = list_store_orders(db, store["id"], limit=STORE_HOME_ORDER_LIMIT)
    orders = _attach_order_items(db, rows)
    products = list_store_products(db, store["id"], only_active=False)
    summary = _store_dashboard_summary(db, store)

    issue_statuses = {
        "CANCELLED",
        "DELIVERY_ISSUE",
        "RETURNING_TO_STORE",
        "RETURNED_TO_STORE",
    }

    clean_orders = [
        o for o in orders
        if int(o.get("admin_hold") or 0) == 0
        and o.get("status") not in issue_statuses
    ]

    issue_orders = [
        o for o in orders
        if int(o.get("admin_hold") or 0) == 1
        or o.get("status") in issue_statuses
    ]

    new_orders = [
        o for o in clean_orders
        if o.get("status") == "CREATED"
    ]

    accepted_orders = [
        o for o in clean_orders
        if o.get("status") == "STORE_ACCEPTED"
    ]

    waiting_driver_orders = [
        o for o in clean_orders
        if o.get("status") in {"WAITING_DRIVER", "DRIVER_ACCEPTED"}
    ]

    active_delivery_orders = [
        o for o in clean_orders
        if o.get("status") == "PICKED_UP"
    ]

    completed_orders = [
        o for o in clean_orders
        if o.get("status") in {"DELIVERED", "COMPLETED"}
    ]

    new_orders.sort(key=lambda o: int(o.get("id") or 0))
    accepted_orders.sort(key=lambda o: int(o.get("id") or 0))
    waiting_driver_orders.sort(
        key=lambda o: (
            0 if o.get("status") == "DRIVER_ACCEPTED" else 1,
            int(o.get("id") or 0),
        )
    )
    active_delivery_orders.sort(key=lambda o: int(o.get("id") or 0))
    completed_orders.sort(key=lambda o: int(o.get("id") or 0), reverse=True)
    issue_orders.sort(key=lambda o: int(o.get("id") or 0), reverse=True)

    summary.update(
        {
            "active_delivery_orders": len(active_delivery_orders),
            "completed_orders": len(completed_orders),
            "issue_orders": len(issue_orders),
            "total_fast_board_orders": len(
                new_orders
                + accepted_orders
                + waiting_driver_orders
                + active_delivery_orders
                + completed_orders
                + issue_orders
            ),
        }
    )

    return render_template(
        "mobile/store/home.html",
        store=store,
        orders=orders,
        products=products,
        summary=summary,
        new_orders=new_orders,
        accepted_orders=accepted_orders,
        waiting_driver_orders=waiting_driver_orders,
        active_delivery_orders=active_delivery_orders,
        completed_orders=completed_orders,
        issue_orders=issue_orders,
    )


@store_bp.get("/realtime/status")
@login_required
@role_required("STORE")
def realtime_status():
    db = get_db()
    store = _require_store_or_redirect()

    if not store:
        return jsonify({"ok": False, "error": "STORE_NOT_FOUND"}), 403

    if not _store_is_active(store):
        return jsonify(_store_inactive_realtime_payload(store))

    return jsonify(_store_realtime_payload(db, store))

@store_bp.route("/setup", methods=["GET", "POST"])
@login_required
@role_required("STORE")
def setup():
    db = get_db()
    user = current_user()
    store = _require_store_or_redirect()

    if not store:
        return redirect("/login")

    if request.method == "POST":
        try:
            now = now_iso()

            store_name = request.form.get("store_name", "").strip()
            phone = request.form.get("phone", "").strip()
            address = request.form.get("address", "").strip()
            store_lat = _float(request.form.get("store_lat"), 0)
            store_lng = _float(request.form.get("store_lng"), 0)
            category = request.form.get("category", "FOOD").strip()
            description = request.form.get("description", "").strip()
            banner_url_input = request.form.get("banner_url", "").strip()

            city_block = normalize_city_block(
                request.form.get("city_block", "ZHONGLI")
            )
            area_label = area_label_for_city_block(city_block)

            is_open = 1 if request.form.get("is_open", "1") == "1" else 0
            setup_completed = 1

            open_time = normalize_hhmm(
                request.form.get("open_time"),
                "10:00",
            )
            close_time = normalize_hhmm(
                request.form.get("close_time"),
                "21:00",
            )

            if open_time == close_time:
                raise ValueError("開店時間與關店時間不能相同。")

            if close_time < open_time:
                raise ValueError("V1 暫不支援跨日營業時間，請設定同一天內的時間。")

            last_order_minutes_before_close = _int(
                request.form.get("last_order_minutes_before_close"),
                30,
            )
            last_order_minutes_before_close = max(
                0,
                min(180, last_order_minutes_before_close),
            )

            is_temporarily_closed = (
                1 if request.form.get("is_temporarily_closed") == "1" else 0
            )
            temporary_close_reason = request.form.get(
                "temporary_close_reason",
                "",
            ).strip()

            open_days = normalize_open_days_from_form(request.form)
            open_days_json = dump_open_days(open_days)

            if not store_name:
                raise ValueError("請輸入店家名稱。")

            if not address:
                raise ValueError("請輸入店家地址。")

            uploaded_banner_url = ""

            banner_file = request.files.get("banner_file")
            if banner_file and banner_file.filename:
                uploaded_banner_url = maybe_save_compressed_upload(
                    banner_file,
                    kind="store_banner",
                    owner_code=store["store_code"],
                )

            final_banner_url = uploaded_banner_url or banner_url_input

            db.execute(
                """
                UPDATE stores
                SET store_name = ?,
                    phone = ?,
                    address = ?,
                    store_lat = ?,
                    store_lng = ?,
                    category = ?,
                    description = ?,
                    banner_url = ?,
                    city_block = ?,
                    area_label = ?,
                    is_open = ?,
                    open_time = ?,
                    close_time = ?,
                    open_days_json = ?,
                    last_order_minutes_before_close = ?,
                    is_temporarily_closed = ?,
                    temporary_close_reason = ?,
                    setup_completed = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    store_name,
                    phone,
                    address,
                    store_lat,
                    store_lng,
                    category,
                    description,
                    final_banner_url,
                    city_block,
                    area_label,
                    is_open,
                    open_time,
                    close_time,
                    open_days_json,
                    last_order_minutes_before_close,
                    is_temporarily_closed,
                    temporary_close_reason,
                    setup_completed,
                    now,
                    store["id"],
                ),
            )

            create_block(
                db,
                event_type="STORE_SETUP_UPDATED",
                actor_role="STORE",
                actor_id=user["id"],
                actor_code=store["store_code"],
                payload={
                    "store_code": store["store_code"],
                    "store_name": store_name,
                    "store_lat": store_lat,
                    "store_lng": store_lng,
                    "gps_saved": bool(store_lat and store_lng),
                    "is_open": is_open,
                    "open_time": open_time,
                    "close_time": close_time,
                    "open_days": open_days,
                    "open_days_json": open_days_json,
                    "last_order_minutes_before_close": last_order_minutes_before_close,
                    "is_temporarily_closed": is_temporarily_closed,
                    "temporary_close_reason": temporary_close_reason,
                    "setup_completed": setup_completed,
                    "city_block": city_block,
                    "area_label": area_label,
                    "banner_uploaded": bool(uploaded_banner_url),
                    "banner_url": final_banner_url,
                    "store_status": _approval_status(store),
                },
                commit=False,
            )

            db.commit()

            if uploaded_banner_url:
                flash("店家設定已儲存，Banner 圖片已壓縮上傳。", "success")
            else:
                flash("店家設定已儲存。", "success")

            return redirect("/store/setup")

        except ImageUploadError as exc:
            db.rollback()
            flash(str(exc), "danger")
            return redirect("/store/setup")

        except Exception as exc:
            db.rollback()
            flash(f"店家設定儲存失敗：{exc}", "danger")
            return redirect("/store/setup")

    store = get_current_store()
    store_status = annotate_store_hours(store)

    return render_template(
        "mobile/store/setup.html",
        store=store,
        store_status=store_status,
    )


@store_bp.route("/products", methods=["GET", "POST"])
@login_required
@role_required("STORE")
def products():
    db = get_db()
    user = current_user()
    store = _require_store_or_redirect()

    if not store:
        return redirect("/login")

    if request.method == "POST":
        action = request.form.get("action", "add").strip()
        now = now_iso()

        try:
            if action == "add":
                name = request.form.get("name", "").strip()
                price_twd = _int(request.form.get("price_twd"), 0)
                stock_qty = _int(request.form.get("stock_qty"), 999)
                prepare_minutes = _int(request.form.get("prepare_minutes"), 15)
                product_category = (
                    request.form.get("product_category", "主餐").strip() or "主餐"
                )
                description = request.form.get("description", "").strip()
                product_note = request.form.get("product_note", "").strip()
                image_url_input = request.form.get("image_url", "").strip()
                is_active = 1 if request.form.get("is_active", "1") == "1" else 0

                if not name:
                    raise ValueError("請輸入商品名稱。")

                if price_twd <= 0:
                    raise ValueError("商品價格必須大於 0。")

                if stock_qty < 0:
                    raise ValueError("庫存數量不能小於 0。")

                if prepare_minutes < 0:
                    raise ValueError("準備時間不能小於 0。")

                uploaded_image_url = ""
                image_file = request.files.get("image_file")

                if image_file and image_file.filename:
                    uploaded_image_url = maybe_save_compressed_upload(
                        image_file,
                        kind="product_image",
                        owner_code=store["store_code"],
                    )

                final_image_url = uploaded_image_url or image_url_input

                cur = db.execute(
                    """
                    INSERT INTO products (
                        store_id,
                        name,
                        price_twd,
                        description,
                        image_url,
                        product_category,
                        stock_qty,
                        prepare_minutes,
                        product_note,
                        is_active,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        store["id"],
                        name,
                        price_twd,
                        description,
                        final_image_url,
                        product_category,
                        stock_qty,
                        prepare_minutes,
                        product_note,
                        is_active,
                        now,
                        now,
                    ),
                )

                create_block(
                    db,
                    event_type="PRODUCT_CREATED",
                    actor_role="STORE",
                    actor_id=user["id"],
                    actor_code=store["store_code"],
                    payload={
                        "store_code": store["store_code"],
                        "product_id": cur.lastrowid,
                        "name": name,
                        "price_twd": price_twd,
                        "stock_qty": stock_qty,
                        "prepare_minutes": prepare_minutes,
                        "product_category": product_category,
                        "image_uploaded": bool(uploaded_image_url),
                        "image_url": final_image_url,
                    },
                    commit=False,
                )

                db.commit()

                if uploaded_image_url:
                    flash("商品已新增，圖片已壓縮上傳。", "success")
                else:
                    flash("商品已新增。", "success")

            elif action == "update":
                product_id = _int(request.form.get("product_id"), 0)

                product = db.execute(
                    """
                    SELECT *
                    FROM products
                    WHERE id = ?
                      AND store_id = ?
                    LIMIT 1
                    """,
                    (product_id, store["id"]),
                ).fetchone()

                if not product:
                    raise ValueError("找不到商品。")

                name = request.form.get("name", "").strip()
                price_twd = _int(request.form.get("price_twd"), 0)
                stock_qty = _int(request.form.get("stock_qty"), 999)
                prepare_minutes = _int(request.form.get("prepare_minutes"), 15)
                product_category = (
                    request.form.get("product_category", "主餐").strip() or "主餐"
                )
                description = request.form.get("description", "").strip()
                product_note = request.form.get("product_note", "").strip()
                image_url_input = request.form.get("image_url", "").strip()
                is_active = 1 if request.form.get("is_active", "1") == "1" else 0

                if not name:
                    raise ValueError("請輸入商品名稱。")

                if price_twd <= 0:
                    raise ValueError("商品價格必須大於 0。")

                if stock_qty < 0:
                    raise ValueError("庫存數量不能小於 0。")

                if prepare_minutes < 0:
                    raise ValueError("準備時間不能小於 0。")

                uploaded_image_url = ""
                image_file = request.files.get("image_file")

                if image_file and image_file.filename:
                    uploaded_image_url = maybe_save_compressed_upload(
                        image_file,
                        kind="product_image",
                        owner_code=store["store_code"],
                    )

                final_image_url = (
                    uploaded_image_url
                    or image_url_input
                    or (product["image_url"] or "")
                )

                db.execute(
                    """
                    UPDATE products
                    SET name = ?,
                        price_twd = ?,
                        description = ?,
                        image_url = ?,
                        product_category = ?,
                        stock_qty = ?,
                        prepare_minutes = ?,
                        product_note = ?,
                        is_active = ?,
                        updated_at = ?
                    WHERE id = ?
                      AND store_id = ?
                    """,
                    (
                        name,
                        price_twd,
                        description,
                        final_image_url,
                        product_category,
                        stock_qty,
                        prepare_minutes,
                        product_note,
                        is_active,
                        now,
                        product_id,
                        store["id"],
                    ),
                )

                create_block(
                    db,
                    event_type="PRODUCT_UPDATED",
                    actor_role="STORE",
                    actor_id=user["id"],
                    actor_code=store["store_code"],
                    payload={
                        "store_code": store["store_code"],
                        "product_id": product_id,
                        "name": name,
                        "price_twd": price_twd,
                        "stock_qty": stock_qty,
                        "prepare_minutes": prepare_minutes,
                        "product_category": product_category,
                        "image_uploaded": bool(uploaded_image_url),
                        "image_url": final_image_url,
                    },
                    commit=False,
                )

                db.commit()

                if uploaded_image_url:
                    flash("商品已更新，圖片已壓縮上傳。", "success")
                else:
                    flash("商品已更新。", "success")

            elif action == "toggle":
                product_id = _int(request.form.get("product_id"), 0)

                product = db.execute(
                    """
                    SELECT *
                    FROM products
                    WHERE id = ?
                      AND store_id = ?
                    LIMIT 1
                    """,
                    (product_id, store["id"]),
                ).fetchone()

                if not product:
                    raise ValueError("找不到商品。")

                new_active = 0 if int(product["is_active"] or 0) == 1 else 1

                db.execute(
                    """
                    UPDATE products
                    SET is_active = ?,
                        updated_at = ?
                    WHERE id = ?
                      AND store_id = ?
                    """,
                    (new_active, now, product_id, store["id"]),
                )

                create_block(
                    db,
                    event_type="PRODUCT_STATUS_UPDATED",
                    actor_role="STORE",
                    actor_id=user["id"],
                    actor_code=store["store_code"],
                    payload={
                        "store_code": store["store_code"],
                        "product_id": product_id,
                        "is_active": new_active,
                    },
                    commit=False,
                )

                db.commit()
                flash("商品狀態已更新。", "success")

            return redirect("/store/products")

        except ImageUploadError as exc:
            db.rollback()
            flash(str(exc), "danger")
            return redirect("/store/products")

        except Exception as exc:
            db.rollback()
            flash(f"商品操作失敗：{exc}", "danger")
            return redirect("/store/products")

    rows = list_store_products(db, store["id"], only_active=False)

    return render_template(
        "mobile/store/products.html",
        store=store,
        products=rows,
    )

@store_bp.route("/orders/new", methods=["GET", "POST"])
@login_required
@role_required("STORE")
def create_delivery():
    db = get_db()
    user = current_user()
    store = _require_store_or_redirect()

    if not store:
        return redirect("/login")

    if not _store_is_active(store):
        flash("店家帳號尚未通過審核，不能建立配送單。", "warning")
        return redirect("/store")

    rain_surcharge_enabled = is_rain_surcharge_enabled(db)

    if request.method == "POST":
        try:
            order = create_store_manual_delivery_order(
                db,
                store=store,
                actor_user=user,
                customer_name=request.form.get("customer_name", ""),
                customer_phone=request.form.get("customer_phone", ""),
                delivery_address=request.form.get("delivery_address", ""),
                floor_number=request.form.get("floor_number", ""),
                address_note=request.form.get("address_note", ""),
                manual_order_title=request.form.get(
                    "manual_order_title",
                    "店家配送單",
                ),
                subtotal_twd=request.form.get("subtotal_twd", 0),
                delivery_lat=request.form.get("delivery_lat", 0),
                delivery_lng=request.form.get("delivery_lng", 0),
                distance_band=request.form.get("distance_band", "0-2KM"),
                difficulty_flags=request.form.getlist("difficulty_flags"),
                manual_extra_reason=request.form.get("manual_extra_reason", ""),
                city_block=normalize_city_block(
                    request.form.get("city_block")
                    or store["city_block"]
                    or "ZHONGLI"
                ),
                payment_type=request.form.get("payment_type", "COD"),
                delivery_method=request.form.get("delivery_method", "FACE_TO_FACE"),
                note=request.form.get("note", ""),
            )

            flash("店家配送單已建立，已直接呼叫同區域 Shiper。", "success")
            return redirect(f"/store/orders#{order['order_code']}")

        except OrderError as exc:
            db.rollback()
            flash(str(exc), "danger")

        except Exception as exc:
            db.rollback()
            flash(f"建立配送單失敗：{exc}", "danger")

    return render_template(
        "mobile/store/create_delivery.html",
        store=store,
        rain_surcharge_enabled=rain_surcharge_enabled,
    )


@store_bp.get("/orders")
@login_required
@role_required("STORE")
def orders():
    db = get_db()
    store = _require_store_or_redirect()

    if not store:
        return redirect("/login")

    if not _store_is_active(store):
        flash("店家帳號尚未通過審核，不能處理訂單。", "warning")
        return redirect("/store")

    rows = list_store_orders(db, store["id"], limit=STORE_ORDERS_PAGE_LIMIT)
    rows = _attach_order_items(db, rows)

    new_orders = [o for o in rows if o["status"] == "CREATED"]
    accepted_orders = [o for o in rows if o["status"] == "STORE_ACCEPTED"]
    waiting_driver_orders = [o for o in rows if o["status"] == "WAITING_DRIVER"]
    delivery_orders = [
        o
        for o in rows
        if o["status"] in {"DRIVER_ACCEPTED", "PICKED_UP", "DELIVERED", "COMPLETED"}
    ]

    return render_template(
        "mobile/store/orders.html",
        store=store,
        orders=rows,
        new_orders=new_orders,
        accepted_orders=accepted_orders,
        waiting_driver_orders=waiting_driver_orders,
        delivery_orders=delivery_orders,
    )


@store_bp.get("/orders/<order_code>/print")
@login_required
@role_required("STORE")
def print_order(order_code):
    db = get_db()
    store = _require_store_or_redirect()

    if not store:
        return redirect("/login")

    if not _store_is_active(store):
        flash("店家帳號尚未通過審核，不能列印訂單。", "warning")
        return redirect("/store")

    order_code = (order_code or "").strip().upper()
    order = get_order_by_code(db, order_code)

    if not order:
        flash("找不到訂單。", "danger")
        return redirect("/store/orders")

    if int(order["store_id"] or 0) != int(store["id"] or 0):
        flash("此訂單不屬於目前店家。", "danger")
        return redirect("/store/orders")

    paper = (request.args.get("paper") or "80").strip()

    if paper not in {"58", "80"}:
        paper = "80"

    auto_print = (request.args.get("auto") or "").strip() == "1"
    items = get_order_items(db, order["id"])

    return render_template(
        "mobile/store/print_order.html",
        store=store,
        order=order,
        items=items,
        auto_print=auto_print,
        paper_width=paper,
        printed_at=now_iso(),
    )


@store_bp.post("/orders/<order_code>/action")
@login_required
@role_required("STORE")
def order_action(order_code):
    db = get_db()
    user = current_user()
    store = _require_store_or_redirect()

    if not store:
        return redirect("/login")

    if not _store_is_active(store):
        flash("店家帳號尚未通過審核，不能操作訂單。", "warning")
        return redirect("/store")

    action = request.form.get("action", "").strip()

    try:
        if action == "accept":
            store_accept_order(
                db,
                store=store,
                order_code=order_code,
                actor_user=user,
            )
            flash("已確認接單。", "success")

        elif action == "call_driver":
            store_call_driver(
                db,
                store=store,
                order_code=order_code,
                actor_user=user,
            )
            flash("已完成商品並呼叫 shiper。", "success")

        elif action == "cancel":
            cancel_order(
                db,
                order_code=order_code,
                actor_role="STORE",
                actor_id=user["id"],
                actor_code=store["store_code"],
                reason=request.form.get("reason", "店家取消"),
            )
            flash("訂單已取消。", "warning")

        else:
            flash("未知操作。", "warning")

    except OrderError as exc:
        flash(str(exc), "danger")

    except Exception as exc:
        db.rollback()
        flash(f"訂單操作失敗：{exc}", "danger")

    return redirect(f"/store/orders#{order_code}")


@store_bp.get("/accounting")
@login_required
@role_required("STORE")
def accounting():
    from services.accounting_service import (
        list_store_accounting_entries,
        store_accounting_summary,
    )

    db = get_db()
    store = _require_store_or_redirect()

    if not store:
        return redirect("/login")

    entries = list_store_accounting_entries(db, store["store_code"], limit=300)
    summary = store_accounting_summary(db, store)

    return render_template(
        "mobile/store/accounting.html",
        store=store,
        entries=entries,
        summary=summary,
    )


@store_bp.post("/payout-account")
@login_required
@role_required("STORE")
def payout_account_update():
    db = get_db()
    user = current_user()
    store = _require_store_or_redirect()

    if not store:
        return redirect("/login")

    try:
        now = now_iso()

        payout_account_name = request.form.get("payout_account_name", "").strip()
        payout_bank_name = request.form.get("payout_bank_name", "").strip()
        payout_bank_code = request.form.get("payout_bank_code", "").strip()
        payout_bank_account = request.form.get("payout_bank_account", "").strip()
        payout_note = request.form.get("payout_note", "").strip()

        if payout_bank_account and len(payout_bank_account) < 5:
            raise ValueError("銀行帳號格式過短，請重新確認。")

        db.execute(
            """
            UPDATE stores
            SET payout_account_name = ?,
                payout_bank_name = ?,
                payout_bank_code = ?,
                payout_bank_account = ?,
                payout_note = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                payout_account_name,
                payout_bank_name,
                payout_bank_code,
                payout_bank_account,
                payout_note,
                now,
                store["id"],
            ),
        )

        create_block(
            db,
            event_type="STORE_PAYOUT_ACCOUNT_UPDATED",
            actor_role="STORE",
            actor_id=user["id"],
            actor_code=store["store_code"],
            payload={
                "store_code": store["store_code"],
                "store_name": store["store_name"],
                "payout_account_name_set": bool(payout_account_name),
                "payout_bank_name_set": bool(payout_bank_name),
                "payout_bank_code_set": bool(payout_bank_code),
                "payout_bank_account_set": bool(payout_bank_account),
                "payout_note_set": bool(payout_note),
                "updated_at": now,
            },
            commit=False,
        )

        db.commit()
        flash("店家收款帳戶已更新。", "success")

    except Exception as exc:
        db.rollback()
        flash(f"更新收款帳戶失敗：{exc}", "danger")

    return redirect("/store/accounting")

@store_bp.route("/contract", methods=["GET", "POST"])
@login_required
@role_required("STORE")
def contract():
    import hashlib
    import json

    db = get_db()
    user = current_user()
    store = _require_store_or_redirect()

    if not store:
        return redirect("/login")

    contract_terms = [
        "店家同意使用 FUMAP GO 作為在地買貨與外送協作平台。平台提供商品展示、客戶下單、合作外送員配送、LINE 通知、對帳紀錄與 BlockFGO 流程留證服務。",
        "店家需自行維護店名、電話、地址、營業狀態、商品、價格、圖片與商品說明，並確保商品內容、價格與營業資訊正確。",
        "FUMAP GO 採低平台費模式，平台僅向店家收取商品成交金額 5% 作為平台服務費。相較於高抽成外送平台，FUMAP GO 讓店家保留更高商品收入，並用更透明的配送與對帳方式服務客戶。",
        "為降低客戶下單負擔、提高下單意願與回購率，店家同意依平台配送規則支援部分基礎配送費。此支援金額會清楚顯示於店家對帳頁。",
        "Commercial V2 基礎配送費分擔方式如下：0–2 公里：基礎配送費 40 TWD，客戶支付 30 TWD，店家支援 10 TWD；3–4 公里：基礎配送費 60 TWD，客戶支付 45 TWD，店家支援 15 TWD；5–6 公里：基礎配送費 80 TWD，客戶支付 60 TWD，店家支援 20 TWD；超過 6 公里：不自動派單，需由 Admin 手動處理或另行確認。",
        "困難配送加價為每筆訂單最多加收 20 TWD，原則上由客戶負擔。即使同一筆訂單同時包含上樓、重物、地址難找、商場中心、偏遠地點、雨天或其他特殊情況，系統仍最多只加收一次 20 TWD。",
        "客戶建立訂單後，訂單會進入店家工作台。店家應於合理時間內確認接單或取消；確認接單後，應依訂單內容製作商品。",
        "店家完成商品後，按下「完成商品 / 呼叫外送員」，即表示店家需要合作外送員協助將商品配送給該店家的客戶。",
        "店家理解並同意：此配送是店家委託合作外送員協助交付商品給店家客戶。客戶關係、商品內容、商品品質與商品售後責任，原則上仍屬店家與客戶之間的交易關係。",
        "COD 訂單中，合作外送員到店取貨時，應依系統顯示之「到店應付店家金額」支付給店家。COD V2 中，到店應付店家金額為商品金額扣除店家支援配送費；店家平台 5% 由店家與 Admin 另行結算，合作外送員不代收店家平台費。",
        "COD 訂單配送成功後，合作外送員會向客戶收取 COD 總額。店家已於取貨時向合作外送員收取系統顯示之到店應付金額，因此店家不得再向客戶重複收款；店家平台費則由店家於對帳週期內與 Admin 結算。",
        "若 COD 訂單因客戶拒收、地址錯誤、客戶失聯、不可抗力或其他非合作外送員可控制因素導致配送失敗，合作外送員應將商品退回店家。店家應配合接收退回商品，並依系統紀錄退還合作外送員已支付之到店取貨款。",
        "若訂單為客戶已付款、轉帳或其他預付方式，合作外送員到店取貨時原則上不需再支付商品款。若配送失敗，合作外送員應將商品退回店家，由店家、客戶與 Admin 依付款紀錄與平台紀錄協調後續處理。",
        "若 Admin 標記訂單為暫停、付款異常、距離超過自動配送範圍或爭議，店家應暫停製作、暫停交付商品或暫停呼叫外送員，直到 Admin 解除暫停或完成處理。",
        "店家不得因客戶拒收、地址錯誤、付款異常或其他配送外部因素，無合理理由拒絕合作外送員退回商品。若店家拒收退貨或拒絕退還合作外送員已支付之到店取貨款，平台可將該紀錄列入 BlockFGO 與爭議處理。",
        "店家應定期查看店家對帳頁，確認商品收入、平台 5% 費用、店家支援配送費、已收款、待收款、合作外送員付款與 Admin 撥款紀錄。",
        "BlockFGO 用於記錄下單、接單、取貨、配送、付款、退貨、退款與對帳流程，是交易留證與流程紀錄，不是投資、收益或現金承諾。",
        "TimeBlock 為平台貢獻紀錄，用於記錄店家參與平台營運、完成訂單或配合流程之貢獻。TimeBlock 不等於現金，不保證收益，不構成投資承諾。",
        "LINE 綁定只用於通知與聯絡，不作為登入、帳號建立或權限授予。店家不得未經授權外洩客戶電話、地址、訂單內容或合作外送員資料。",
        "店家如發生缺貨、商品錯誤、價格錯誤、延遲、客訴或其他異常，應即時透過系統或 LINE 聯絡 Admin 處理。",
        "合約簽署後，系統會保存簽署時間、合約內容、合約 hash 與 BlockFGO 紀錄。合約簽署後只可查看，不可刪除或修改；若需更新，需由 Admin 建立新版本並重新簽署。",
    ]

    if request.method == "POST":
        if store["contract_signed_at"]:
            flash("合約已簽署，只能查看，不能重複簽署。", "warning")
            return redirect("/store/contract")

        agree = request.form.get("agree", "").strip()

        if agree != "1":
            flash("請先勾選同意合約條款。", "danger")
            return redirect("/store/contract")

        signed_at = now_iso()

        payload = {
            "contract_type": "STORE_COMMERCIAL_V1",
            "store_code": store["store_code"],
            "store_name": store["store_name"],
            "owner_user_id": user["id"],
            "signed_at": signed_at,
            "terms": contract_terms,
        }

        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        contract_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()

        try:
            db.execute(
                """
                UPDATE stores
                SET contract_signed_at = ?,
                    contract_payload_json = ?,
                    contract_hash = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    signed_at,
                    payload_json,
                    contract_hash,
                    signed_at,
                    store["id"],
                ),
            )

            create_block(
                db,
                event_type="STORE_CONTRACT_SIGNED",
                actor_role="STORE",
                actor_id=user["id"],
                actor_code=store["store_code"],
                previous_status="UNSIGNED",
                new_status="SIGNED",
                payload={
                    "contract_type": "STORE_COMMERCIAL_V1",
                    "store_code": store["store_code"],
                    "contract_hash": contract_hash,
                    "signed_at": signed_at,
                },
                commit=False,
            )

            db.commit()
            flash("店家合約已簽署。簽署後只能查看，不能刪除。", "success")

        except Exception as exc:
            db.rollback()
            flash(f"合約簽署失敗：{exc}", "danger")

        return redirect("/store/contract")

    store = get_current_store()

    return render_template(
        "mobile/store/contract.html",
        store=store,
        contract_terms=contract_terms,
    )

from urllib.parse import quote_plus

from flask import Blueprint, render_template, request, redirect, flash, jsonify

from db import get_db
from services.permission_service import (
    login_required,
    role_required,
    current_user,
    get_current_driver,
)
from services.code_service import now_iso
from services.block_service import create_block
from services.order_service import get_order_items
from services.image_service import ImageUploadError, save_compressed_upload
from services.email_service import (
    get_admin_notify_emails,
    send_admin_returned_to_store_email,
    send_customer_delivery_proof_email,
    send_customer_returned_to_store_email,
    send_store_returned_to_store_email,
    send_admin_payout_requested_email,
    send_admin_settlement_target_marked_paid_email,
)
from services.accounting_service import (
    calculate_order_settlement,
    create_delivery_accounting_entries,
    list_accounting_entries_for_order,
    list_driver_accounting_entries,
    driver_accounting_summary,
)
from services.settlement_service import (
    get_target_payout_summary,
    get_target_admin_debt_summary,
    create_target_payout_request,
    mark_settlement_target_paid,
)

try:
    from services.line_notify_service import (
        push_to_role_target,
        push_admin_payout_requested,
        push_admin_settlement_target_marked_paid,
    )
except Exception:
    def push_to_role_target(db, **kwargs):
        return {"ok": False, "skipped": True}

    def push_admin_payout_requested(db, **kwargs):
        return {"ok": False, "skipped": True}

    def push_admin_settlement_target_marked_paid(db, **kwargs):
        return {"ok": False, "skipped": True}


driver_bp = Blueprint("driver", __name__, url_prefix="/driver")

MAX_ACTIVE_ORDERS = 5

DRIVER_WORKING_STATUSES = {
    "DRIVER_ACCEPTED",
    "PICKED_UP",
    "DELIVERY_ISSUE",
    "RETURNING_TO_STORE",
}

DRIVER_BOARD_STATUSES = {
    "DRIVER_ACCEPTED",
    "PICKED_UP",
    "DELIVERY_ISSUE",
    "RETURNING_TO_STORE",
    "RETURNED_TO_STORE",
}


def _int(value, default=0):
    try:
        return int(value or 0)
    except Exception:
        return int(default or 0)


def _float(value, default=0.0):
    try:
        value = str(value or "").strip()
        if not value:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _text(value, default=""):
    try:
        return str(value or default).strip()
    except Exception:
        return default


def _money(value):
    return max(0, _int(value, 0))


def _safe_row_get(row, key, default=None):
    try:
        if row and key in row.keys():
            return row[key]
    except Exception:
        pass

    try:
        if isinstance(row, dict):
            return row.get(key, default)
    except Exception:
        pass

    return default


def _has_valid_coordinates(lat, lng):
    try:
        lat = float(lat or 0)
        lng = float(lng or 0)
        return lat != 0 and lng != 0 and -90 <= lat <= 90 and -180 <= lng <= 180
    except Exception:
        return False


def _maps_url(address):
    address = _text(address)

    if not address:
        return ""

    return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(address)


def _maps_url_for_coordinates(lat, lng):
    if not _has_valid_coordinates(lat, lng):
        return ""

    return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(
        f"{float(lat):.7f},{float(lng):.7f}"
    )


def _order_pickup_map_url(order):
    gps_url = _maps_url_for_coordinates(
        _safe_row_get(order, "store_lat", 0),
        _safe_row_get(order, "store_lng", 0),
    )

    if gps_url:
        return gps_url

    return _maps_url(_safe_row_get(order, "store_address", ""))


def _order_delivery_map_url(order):
    gps_url = _maps_url_for_coordinates(
        _safe_row_get(order, "delivery_lat", 0),
        _safe_row_get(order, "delivery_lng", 0),
    )

    if gps_url:
        return gps_url

    return _maps_url(_safe_row_get(order, "delivery_address", ""))


def _driver_city_block(driver):
    city_block = _text(_safe_row_get(driver, "city_block", "")).upper()

    if city_block:
        return city_block

    service_area = _text(_safe_row_get(driver, "service_area", "")).upper()

    if "TAOYUAN" in service_area or "桃園" in service_area:
        return "TAOYUAN"

    return "ZHONGLI"


def _driver_area_label(driver):
    area_label = _text(_safe_row_get(driver, "area_label", ""))

    if area_label:
        return area_label

    city_block = _driver_city_block(driver)

    if city_block == "TAOYUAN":
        return "桃園區"

    return "中壢區"


def _driver_estimated_income(order):
    settlement = calculate_order_settlement(order)
    return settlement["driver_gross_twd"]


def _distance_band_rank(distance_band):
    band = _text(distance_band).upper()

    if band in {"0-2KM", "0_2KM", "0-2"}:
        return 1
    if band in {"3-4KM", "3_4KM", "3-4"}:
        return 2
    if band in {"5-6KM", "5_6KM", "5-6"}:
        return 3
    if band in {"OVER_6KM", "OVER6KM", "6KM+", "OVER_6"}:
        return 9

    return 5


def _order_distance_value(order):
    distance_km = _float(_safe_row_get(order, "distance_km", 0), 0)

    if distance_km > 0:
        return distance_km

    return float(_distance_band_rank(_safe_row_get(order, "distance_band", "")))


def _smartroad_score_value(order):
    return _int(_safe_row_get(order, "smartroad_score", 50), 50)


def _driver_order_rank(order):
    score = _smartroad_score_value(order)

    if _safe_row_get(order, "smartroad_same_road", 0):
        score += 30

    if _safe_row_get(order, "smartroad_same_side", 0):
        score += 20

    if _safe_row_get(order, "smartroad_uturn_risk", 0):
        score -= 30

    score -= int(_order_distance_value(order) * 5)

    if _money(_safe_row_get(order, "extra_fee_twd", 0)) <= 0:
        score += 5

    return score


def _driver_order_lane(order):
    rank = _driver_order_rank(order)
    same_road = bool(_safe_row_get(order, "smartroad_same_road", 0))
    same_side = bool(_safe_row_get(order, "smartroad_same_side", 0))
    uturn = bool(_safe_row_get(order, "smartroad_uturn_risk", 0))
    distance_rank = _distance_band_rank(_safe_row_get(order, "distance_band", ""))

    if uturn or distance_rank >= 9:
        return "CAUTIOUS"

    if same_road and same_side and rank >= 70:
        return "RECOMMENDED"

    if same_road or same_side or rank >= 60:
        return "ROUTE"

    return "NORMAL"


def _driver_order_lane_label(order):
    lane = _driver_order_lane(order)

    if lane == "RECOMMENDED":
        return "推薦接單"
    if lane == "ROUTE":
        return "順路單"
    if lane == "CAUTIOUS":
        return "謹慎接單"

    return "一般單"


def _driver_order_lane_badge_class(order):
    lane = _driver_order_lane(order)

    if lane in {"RECOMMENDED", "ROUTE"}:
        return "ok"

    if lane == "CAUTIOUS":
        return "warn"

    return ""


def _waiting_order_sort_key(order):
    lane_priority = {
        "RECOMMENDED": 1,
        "ROUTE": 2,
        "NORMAL": 3,
        "CAUTIOUS": 4,
    }.get(_driver_order_lane(order), 9)

    return (
        lane_priority,
        -_driver_order_rank(order),
        _distance_band_rank(_safe_row_get(order, "distance_band", "")),
        _int(_safe_row_get(order, "id", 0), 0),
    )


def _delivery_order_sort_key(order):
    return (
        _order_distance_value(order),
        _distance_band_rank(_safe_row_get(order, "distance_band", "")),
        -_smartroad_score_value(order),
        _int(_safe_row_get(order, "id", 0), 0),
    )

def _active_order_count(db, driver_id):
    row = db.execute(
        """
        SELECT COUNT(*) AS c
        FROM orders
        WHERE driver_id = ?
          AND status IN (
            'DRIVER_ACCEPTED',
            'PICKED_UP',
            'DELIVERY_ISSUE',
            'RETURNING_TO_STORE'
          )
        """,
        (driver_id,),
    ).fetchone()

    return int(row["c"] or 0) if row else 0


def _driver_capacity(db, driver_id):
    active_count = _active_order_count(db, driver_id)
    max_active = MAX_ACTIVE_ORDERS
    remaining = max(0, max_active - active_count)
    can_accept = active_count < max_active

    return {
        "can_accept": can_accept,
        "active_count": active_count,
        "max_active": max_active,
        "remaining": remaining,
        "message": (
            f"可繼續接單，剩餘 {remaining} 筆。"
            if can_accept
            else f"目前已達 {max_active} 筆配送上限，請先完成或退回部分訂單。"
        ),
    }


def _require_driver_or_redirect():
    driver = get_current_driver()

    if not driver:
        flash("找不到 shiper 資料，請重新登入 shiper 帳號。", "danger")
        return None

    return driver


def _approval_status(row):
    try:
        if row and "status" in row.keys():
            return str(row["status"] or "PENDING_APPROVAL").strip().upper()
    except Exception:
        pass

    return "PENDING_APPROVAL"


def _driver_is_active(driver):
    return bool(driver and _approval_status(driver) == "ACTIVE")


def _driver_pending_response(driver):
    return render_template(
        "mobile/driver/pending.html",
        driver=driver,
        status=_approval_status(driver),
    )


def _driver_inactive_realtime_payload(driver):
    status = _approval_status(driver)

    return {
        "ok": True,
        "role": "DRIVER",
        "is_online": False,
        "should_ring": False,
        "message": "",
        "target_url": "/driver",
        "available_orders": 0,
        "active_orders": 0,
        "max_active_orders": MAX_ACTIVE_ORDERS,
        "can_accept_more_orders": False,
        "remaining_capacity_orders": 0,
        "driver_capacity": {
            "can_accept": False,
            "active_count": 0,
            "max_active": MAX_ACTIVE_ORDERS,
            "remaining": 0,
            "message": "Shiper 帳號尚未通過審核，不能接單。",
        },
        "latest_order_code": "",
        "latest_store_name": "",
        "latest_store_address": "",
        "latest_delivery_address": "",
        "latest_distance_km": "",
        "latest_delivery_fee_twd": 0,
        "latest_total_twd": 0,
        "latest_smartroad_lane": "",
        "latest_payment_method": "",
        "latest_payment_status": "",
        "approval_required": True,
        "driver_status": status,
        "city_block": _driver_city_block(driver) if driver else "ZHONGLI",
        "area_label": _driver_area_label(driver) if driver else "中壢區",
        "server_time": now_iso(),
    }


def _order_store_join_sql(where_sql):
    return f"""
        SELECT o.*,
               s.store_code,
               s.store_name,
               s.phone AS store_phone,
               s.address AS store_address,
               s.store_lat AS store_lat,
               s.store_lng AS store_lng,
               s.city_block AS store_city_block,
               s.area_label AS store_area_label,
               d.driver_code,
               d.driver_name,
               d.phone AS driver_phone
        FROM orders o
        JOIN stores s ON s.id = o.store_id
        LEFT JOIN drivers d ON d.id = o.driver_id
        WHERE {where_sql}
    """


def _list_waiting_driver_orders(db, driver):
    city_block = _driver_city_block(driver)

    rows = db.execute(
        _order_store_join_sql(
            """
            o.status = 'WAITING_DRIVER'
            AND o.driver_id IS NULL
            AND COALESCE(o.admin_hold, 0) = 0
            AND (
                COALESCE(o.city_block, '') = ''
                OR UPPER(o.city_block) = ?
            )
            """
        )
        + """
        ORDER BY o.id ASC
        LIMIT 100
        """,
        (city_block,),
    ).fetchall()

    return sorted(rows, key=_waiting_order_sort_key)


def _count_all_waiting_orders(db):
    row = db.execute(
        """
        SELECT COUNT(*) AS c
        FROM orders
        WHERE status = 'WAITING_DRIVER'
          AND driver_id IS NULL
          AND COALESCE(admin_hold, 0) = 0
        """
    ).fetchone()

    return int(row["c"] or 0) if row else 0


def _waiting_orders_by_city_block(db):
    rows = db.execute(
        """
        SELECT
            UPPER(COALESCE(NULLIF(city_block, ''), 'ZHONGLI')) AS city_block,
            COUNT(*) AS c
        FROM orders
        WHERE status = 'WAITING_DRIVER'
          AND driver_id IS NULL
          AND COALESCE(admin_hold, 0) = 0
        GROUP BY UPPER(COALESCE(NULLIF(city_block, ''), 'ZHONGLI'))
        """
    ).fetchall()

    result = {
        "ZHONGLI": 0,
        "TAOYUAN": 0,
    }

    for row in rows:
        city_block = (row["city_block"] or "ZHONGLI").upper()

        if city_block in result:
            result[city_block] = int(row["c"] or 0)

    return result


def _list_driver_active_orders(db, driver_id):
    rows = db.execute(
        _order_store_join_sql(
            """
            o.driver_id = ?
            AND o.status IN (
                'DRIVER_ACCEPTED',
                'PICKED_UP',
                'DELIVERY_ISSUE',
                'RETURNING_TO_STORE',
                'RETURNED_TO_STORE'
            )
            """
        )
        + """
        ORDER BY
          CASE o.status
            WHEN 'DRIVER_ACCEPTED' THEN 1
            WHEN 'PICKED_UP' THEN 2
            WHEN 'DELIVERY_ISSUE' THEN 3
            WHEN 'RETURNING_TO_STORE' THEN 4
            WHEN 'RETURNED_TO_STORE' THEN 5
            ELSE 9
          END,
          o.id ASC
        LIMIT 100
        """,
        (driver_id,),
    ).fetchall()

    return sorted(
        rows,
        key=lambda o: (
            {
                "DRIVER_ACCEPTED": 1,
                "PICKED_UP": 2,
                "DELIVERY_ISSUE": 3,
                "RETURNING_TO_STORE": 4,
                "RETURNED_TO_STORE": 5,
            }.get(_safe_row_get(o, "status", ""), 9),
            _delivery_order_sort_key(o),
        ),
    )


def _list_driver_done_orders(db, driver_id, limit=80):
    return db.execute(
        _order_store_join_sql(
            """
            o.driver_id = ?
            AND o.status IN ('DELIVERED', 'COMPLETED')
            """
        )
        + """
        ORDER BY o.updated_at DESC, o.id DESC
        LIMIT ?
        """,
        (driver_id, int(limit or 80)),
    ).fetchall()


def _get_order_for_driver_page(db, order_code):
    return db.execute(
        _order_store_join_sql("o.order_code = ?")
        + """
        LIMIT 1
        """,
        ((order_code or "").strip().upper(),),
    ).fetchone()


def _driver_owns_order(order, driver):
    return bool(
        order
        and driver
        and order["driver_id"]
        and int(order["driver_id"]) == int(driver["id"])
    )


def _can_view_order(order, driver):
    if not order or not driver:
        return False

    if (
        order["status"] == "WAITING_DRIVER"
        and not order["driver_id"]
        and int(order["admin_hold"] or 0) == 0
    ):
        return True

    return _driver_owns_order(order, driver)

def _driver_fast_summary(db, driver, waiting_orders, active_orders):
    summary = driver_accounting_summary(db, driver)

    driver_id = int(driver["id"])
    today_prefix = now_iso()[:10]

    rows = db.execute(
        """
        SELECT *
        FROM orders
        WHERE driver_id = ?
          AND status IN ('DELIVERED', 'COMPLETED')
          AND substr(COALESCE(updated_at, created_at), 1, 10) = ?
        ORDER BY id DESC
        """,
        (driver_id, today_prefix),
    ).fetchall()

    today_income = 0
    cod_collected = 0
    payable_store = 0
    payable_admin = 0
    cash_keep = 0
    net_hint = 0
    delivered_count = 0

    for row in rows:
        delivered_count += 1

        settlement = calculate_order_settlement(row)
        payment_method = settlement["payment_method"]

        today_income += settlement["driver_gross_twd"]
        payable_admin += settlement["driver_platform_fee_twd"]
        net_hint += settlement["driver_net_income_twd"]

        if payment_method == "COD":
            cod_collected += settlement["driver_collect_from_customer_twd"]
            payable_store += settlement["driver_pay_store_twd"]
            cash_keep += settlement["driver_keep_twd"]

    working_count = len(
        [
            o for o in active_orders
            if _safe_row_get(o, "status", "") in DRIVER_WORKING_STATUSES
        ]
    )

    summary.update(
        {
            "available_orders_count": len(waiting_orders),
            "all_waiting_orders_count": _count_all_waiting_orders(db),
            "active_orders_count": working_count,
            "today_delivered_count": delivered_count,
            "today_income_twd": today_income,
            "today_cod_collected_twd": cod_collected,
            "today_payable_store_twd": payable_store,
            "today_payable_admin_twd": payable_admin,
            "today_cash_keep_twd": cash_keep,
            "today_net_hint_twd": net_hint,
            "area_label": _driver_area_label(driver),
            "city_block": _driver_city_block(driver),
        }
    )

    return summary


def _push_delivery_updates(db, order):
    store_code = order["store_code"]
    customer_user_id = order["customer_user_id"]

    push_to_role_target(
        db,
        role="STORE",
        target_code=store_code,
        event_type="ORDER_DELIVERED",
        order_code=order["order_code"],
        message=(
            "FUMAP GO 配送完成\n"
            f"訂單：{order['order_code']}\n"
            "狀態：已送達\n"
            f"金額：{order['total_twd']} TWD"
        ),
        commit=False,
    )

    if customer_user_id:
        push_to_role_target(
            db,
            role="CUSTOMER",
            target_code=f"CUS-{customer_user_id}",
            event_type="ORDER_DELIVERED",
            order_code=order["order_code"],
            message=(
                "FUMAP GO 訂單已送達\n"
                f"訂單：{order['order_code']}\n"
                "如有問題請聯絡客服。"
            ),
            commit=False,
        )


def _send_returned_to_store_notifications(db, order):
    order_code = order["order_code"]

    customer_email = ""
    store_email = ""

    try:
        if order["customer_user_id"]:
            customer = db.execute(
                """
                SELECT id, email, email_verified_at
                FROM users
                WHERE id = ?
                LIMIT 1
                """,
                (order["customer_user_id"],),
            ).fetchone()

            if customer and _safe_row_get(customer, "email", ""):
                customer_email = _text(_safe_row_get(customer, "email", ""))

    except Exception as exc:
        print(f"[RETURNED_TO_STORE][EMAIL][CUSTOMER_LOOKUP_ERROR] {exc}")

    try:
        store_owner = db.execute(
            """
            SELECT u.id, u.email, u.email_verified_at
            FROM stores s
            JOIN users u ON u.id = s.owner_user_id
            WHERE s.id = ?
            LIMIT 1
            """,
            (order["store_id"],),
        ).fetchone()

        if store_owner and _safe_row_get(store_owner, "email", ""):
            store_email = _text(_safe_row_get(store_owner, "email", ""))

    except Exception as exc:
        print(f"[RETURNED_TO_STORE][EMAIL][STORE_LOOKUP_ERROR] {exc}")

    if customer_email:
        try:
            send_customer_returned_to_store_email(
                order,
                customer_email,
                order_url=f"/orders?order_code={order_code}",
            )
        except Exception as exc:
            print(f"[RETURNED_TO_STORE][EMAIL][CUSTOMER_ERROR] {exc}")

    if store_email:
        try:
            send_store_returned_to_store_email(
                order,
                store_email,
                order_url=f"/store/orders?order_code={order_code}",
            )
        except Exception as exc:
            print(f"[RETURNED_TO_STORE][EMAIL][STORE_ERROR] {exc}")

    try:
        send_admin_returned_to_store_email(
            order,
            admin_emails=get_admin_notify_emails(),
            admin_order_url=f"/admin/orders?order_code={order_code}",
        )
    except Exception as exc:
        print(f"[RETURNED_TO_STORE][EMAIL][ADMIN_ERROR] {exc}")

    try:
        push_to_role_target(
            db,
            role="STORE",
            target_code=order["store_code"],
            event_type="RETURNED_TO_STORE",
            order_code=order_code,
            message=(
                "FUMAP GO 退回店家通知\n"
                f"訂單：{order_code}\n"
                f"店家：{order['store_name']}\n"
                "狀態：商品已由 Shiper 退回店家\n"
                "請登入店家工作台確認後續處理。"
            ),
            commit=True,
        )
    except Exception as exc:
        print(f"[RETURNED_TO_STORE][LINE][STORE_ERROR] {exc}")

    if order["customer_user_id"]:
        try:
            push_to_role_target(
                db,
                role="CUSTOMER",
                target_code=f"CUS-{order['customer_user_id']}",
                event_type="RETURNED_TO_STORE",
                order_code=order_code,
                message=(
                    "FUMAP GO 訂單退回店家通知\n"
                    f"訂單：{order_code}\n"
                    "狀態：商品已退回店家，後續由店家與 Admin 處理。"
                ),
                commit=True,
            )
        except Exception as exc:
            print(f"[RETURNED_TO_STORE][LINE][CUSTOMER_ERROR] {exc}")


def _notify_admin_driver_payout_requested(db, *, driver, settlement, payout_summary, note=""):
    try:
        send_admin_payout_requested_email(
            target=driver,
            settlement=settlement,
            payout_summary=payout_summary,
            role="DRIVER",
            target_code=driver["driver_code"],
            note=note,
        )
    except Exception as exc:
        print(f"[DRIVER][PAYOUT_REQUEST][EMAIL_ADMIN][ERROR] {exc}")

    try:
        push_admin_payout_requested(
            db,
            target=driver,
            settlement=settlement,
            role="DRIVER",
            target_code=driver["driver_code"],
            note=note,
            commit=True,
        )
    except Exception as exc:
        print(f"[DRIVER][PAYOUT_REQUEST][LINE_ADMIN][ERROR] {exc}")


def _notify_admin_driver_marked_paid(db, *, driver, settlement, note="", payment_method=""):
    try:
        send_admin_settlement_target_marked_paid_email(
            target=driver,
            settlement=settlement,
            role="DRIVER",
            target_code=driver["driver_code"],
            payment_method=payment_method,
            note=note,
        )
    except Exception as exc:
        print(f"[DRIVER][MARK_PAID][EMAIL_ADMIN][ERROR] {exc}")

    try:
        push_admin_settlement_target_marked_paid(
            db,
            target=driver,
            settlement=settlement,
            role="DRIVER",
            target_code=driver["driver_code"],
            payment_method=payment_method,
            note=note,
            commit=True,
        )
    except Exception as exc:
        print(f"[DRIVER][MARK_PAID][LINE_ADMIN][ERROR] {exc}")

@driver_bp.get("")
@driver_bp.get("/")
@login_required
@role_required("DRIVER")
def home():
    db = get_db()
    driver = _require_driver_or_redirect()

    if not driver:
        return redirect("/login")

    if not _driver_is_active(driver):
        return _driver_pending_response(driver)

    board = request.args.get("board", "accept").strip().lower()

    waiting_orders = _list_waiting_driver_orders(db, driver)
    active_orders = _list_driver_active_orders(db, driver["id"])
    done_orders = _list_driver_done_orders(db, driver["id"], limit=20)
    waiting_by_city_block = _waiting_orders_by_city_block(db)

    accepted_orders = [
        o for o in active_orders
        if _safe_row_get(o, "status", "") == "DRIVER_ACCEPTED"
    ]

    picked_up_orders = [
        o for o in active_orders
        if _safe_row_get(o, "status", "") == "PICKED_UP"
    ]

    issue_orders = [
        o for o in active_orders
        if _safe_row_get(o, "status", "") in {
            "DELIVERY_ISSUE",
            "RETURNING_TO_STORE",
            "RETURNED_TO_STORE",
        }
    ]

    delivery_orders = sorted(
        accepted_orders + picked_up_orders + issue_orders,
        key=lambda o: (
            {
                "DRIVER_ACCEPTED": 1,
                "PICKED_UP": 2,
                "DELIVERY_ISSUE": 3,
                "RETURNING_TO_STORE": 4,
                "RETURNED_TO_STORE": 5,
            }.get(_safe_row_get(o, "status", ""), 9),
            _delivery_order_sort_key(o),
        ),
    )

    driver_capacity = _driver_capacity(db, driver["id"])
    active_order_count = driver_capacity["active_count"]
    completed_orders = done_orders
    accounting = _driver_fast_summary(db, driver, waiting_orders, active_orders)

    return render_template(
        "mobile/driver/home.html",
        driver=driver,
        waiting_orders=waiting_orders,
        active_orders=active_orders,
        accepted_orders=accepted_orders,
        picked_up_orders=picked_up_orders,
        issue_orders=issue_orders,
        delivery_orders=delivery_orders,
        completed_orders=completed_orders,
        done_orders=done_orders,
        accounting=accounting,
        maps_url=_maps_url,
        pickup_map_url_for_order=_order_pickup_map_url,
        delivery_map_url_for_order=_order_delivery_map_url,
        driver_city_block=_driver_city_block(driver),
        driver_area_label=_driver_area_label(driver),
        waiting_by_city_block=waiting_by_city_block,
        max_active_orders=MAX_ACTIVE_ORDERS,
        active_order_count=active_order_count,
        driver_capacity=driver_capacity,
        board=board,
        order_lane_label=_driver_order_lane_label,
        order_lane_badge_class=_driver_order_lane_badge_class,
        order_rank=_driver_order_rank,
    )


@driver_bp.post("/online")
@login_required
@role_required("DRIVER")
def set_online_status():
    db = get_db()
    user = current_user()
    driver = _require_driver_or_redirect()

    if not driver:
        return redirect("/login")

    if not _driver_is_active(driver):
        flash("Shiper 帳號尚未通過審核，不能上線接單。", "warning")
        return redirect("/driver")

    action = request.form.get("action", "").strip()
    now = now_iso()

    if action == "online":
        is_online = 1
        new_status = "ONLINE"
        flash("已上線，現在可以接單。", "success")
    elif action == "offline":
        is_online = 0
        new_status = "OFFLINE"
        flash("已離線，不會接收新訂單。", "warning")
    else:
        flash("未知操作。", "warning")
        return redirect("/driver")

    try:
        db.execute(
            """
            UPDATE drivers
            SET is_online = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (is_online, now, driver["id"]),
        )

        create_block(
            db,
            event_type="DRIVER_ONLINE_STATUS_UPDATED",
            actor_role="DRIVER",
            actor_id=user["id"],
            actor_code=driver["driver_code"],
            previous_status="ONLINE" if int(driver["is_online"] or 0) else "OFFLINE",
            new_status=new_status,
            payload={
                "driver_code": driver["driver_code"],
                "driver_name": driver["driver_name"],
                "is_online": is_online,
                "city_block": _driver_city_block(driver),
                "area_label": _driver_area_label(driver),
            },
            commit=False,
        )

        db.commit()

    except Exception as exc:
        db.rollback()
        flash(f"更新狀態失敗：{exc}", "danger")

    return redirect("/driver")


@driver_bp.post("/area")
@login_required
@role_required("DRIVER")
def set_driver_area():
    db = get_db()
    user = current_user()
    driver = _require_driver_or_redirect()

    if not driver:
        return redirect("/login")

    action = request.form.get("action", "").strip()

    if action == "set_area_zhongli":
        city_block = "ZHONGLI"
        area_label = "中壢區"
        service_area = "中壢區"
    elif action == "set_area_taoyuan":
        city_block = "TAOYUAN"
        area_label = "桃園區"
        service_area = "桃園區"
    else:
        flash("未知接單區域。", "warning")
        return redirect("/driver")

    active_count = _active_order_count(db, driver["id"])

    if active_count > 0:
        flash("你目前有配送中的訂單，完成後才能切換接單區域。", "warning")
        return redirect("/driver")

    old_city_block = (driver["city_block"] or "").upper()

    if old_city_block == city_block:
        flash("目前已在此接單區域。", "warning")
        return redirect("/driver")

    now = now_iso()

    try:
        db.execute(
            """
            UPDATE drivers
            SET city_block = ?,
                area_label = ?,
                service_area = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                city_block,
                area_label,
                service_area,
                now,
                driver["id"],
            ),
        )

        create_block(
            db,
            event_type="DRIVER_AREA_UPDATED",
            actor_role="DRIVER",
            actor_id=user["id"],
            actor_code=driver["driver_code"],
            previous_status=driver["city_block"] or "",
            new_status=city_block,
            payload={
                "driver_code": driver["driver_code"],
                "driver_name": driver["driver_name"],
                "old_city_block": driver["city_block"] or "",
                "old_area_label": driver["area_label"] or "",
                "new_city_block": city_block,
                "new_area_label": area_label,
                "new_service_area": service_area,
                "action": action,
                "rule": "Driver can change area only when no active order.",
            },
            commit=False,
        )

        db.commit()

        if city_block == "ZHONGLI":
            flash("接單區域已切換為 中壢區。", "success")
        else:
            flash("接單區域已切換為 桃園區。", "success")

    except Exception as exc:
        db.rollback()
        flash(f"切換接單區域失敗：{exc}", "danger")

    return redirect("/driver")


@driver_bp.get("/orders")
@login_required
@role_required("DRIVER")
def orders_alias():
    return redirect("/driver")

@driver_bp.route("/order/<order_code>", methods=["GET", "POST"])
@login_required
@role_required("DRIVER")
def order_detail(order_code):
    db = get_db()
    user = current_user()
    driver = _require_driver_or_redirect()

    if not driver:
        return redirect("/login")

    if not _driver_is_active(driver):
        flash("Shiper 帳號尚未通過審核，不能操作訂單。", "warning")
        return redirect("/driver")

    order = _get_order_for_driver_page(db, order_code)

    if not order:
        flash("找不到訂單。", "danger")
        return redirect("/driver")

    if not _can_view_order(order, driver):
        flash("此訂單不屬於你，或已被其他 shiper 接走。", "danger")
        return redirect("/driver")

    if int(order["admin_hold"] or 0) == 1:
        flash("此訂單已被 Admin 暫停，暫時不能操作。", "warning")
        return redirect("/driver")

    if request.method == "POST":
        action = request.form.get("action", "").strip()

        try:
            if action == "accept":
                _accept_order(db, user=user, driver=driver, order=order)
                flash("已接單，請前往店家取餐。", "success")
                return redirect("/driver")

            if action == "cancel_accept":
                _cancel_accept_order(
                    db,
                    user=user,
                    driver=driver,
                    order=order,
                    reason=request.form.get("reason", "Shiper 取消接單"),
                )
                flash("已取消接單，訂單已退回可接狀態。", "warning")
                return redirect("/driver")

            if action == "pickup":
                _pickup_order(db, user=user, driver=driver, order=order)
                flash("已取餐，請前往客戶地址。", "success")
                return redirect("/driver?board=delivery")

            if action == "delivered":
                _deliver_order(db, user=user, driver=driver, order=order)
                flash("已完成配送，系統已建立配送紀錄。", "success")
                return redirect("/driver?board=delivery")

            if action == "report_issue":
                _report_delivery_issue(
                    db,
                    user=user,
                    driver=driver,
                    order=order,
                    issue_reason=request.form.get("issue_reason", ""),
                    issue_note=request.form.get("issue_note", ""),
                    contacted_customer=request.form.get("contacted_customer") == "1",
                    contacted_store=request.form.get("contacted_store") == "1",
                )
                flash("已回報配送異常，請優先退回店家處理商品與款項。", "warning")
                return redirect("/driver?board=delivery")

            if action == "return_to_store":
                _mark_returning_to_store(
                    db,
                    user=user,
                    driver=driver,
                    order=order,
                    return_reason=request.form.get("return_reason", "配送異常後退回店家"),
                )
                flash("已進入退回店家流程，請依導航返回店家。", "warning")
                return redirect("/driver?board=delivery")

            if action == "returned_to_store":
                _confirm_returned_to_store(
                    db,
                    user=user,
                    driver=driver,
                    order=order,
                    money_returned_by_store=request.form.get(
                        "money_returned_by_store",
                        "NOT_APPLICABLE",
                    ),
                    amount_returned_twd=request.form.get("amount_returned_twd", 0),
                    return_note=request.form.get("return_note", ""),
                    contacted_store=request.form.get("contacted_store") == "1",
                    contacted_admin=request.form.get("contacted_admin") == "1",
                    return_proof_file=request.files.get("return_proof"),
                )
                flash("已確認商品退回店家，請等待 Admin 處理後續對帳。", "success")
                return redirect("/driver?board=delivery")

            flash("未知操作。", "warning")
            return redirect(f"/driver/order/{order_code}")

        except Exception as exc:
            db.rollback()
            flash(str(exc), "danger")
            return redirect(f"/driver/order/{order_code}")

    items = get_order_items(db, order["id"])

    blocks = db.execute(
        """
        SELECT *
        FROM blocks
        WHERE order_code = ?
        ORDER BY id DESC
        LIMIT 100
        """,
        (order["order_code"],),
    ).fetchall()

    accounting_entries = list_accounting_entries_for_order(db, order["order_code"])
    settlement = calculate_order_settlement(order)

    return render_template(
        "mobile/driver/order_detail.html",
        driver=driver,
        order=order,
        items=items,
        blocks=blocks,
        accounting_entries=accounting_entries,
        settlement=settlement,
        pickup_map_url=_order_pickup_map_url(order),
        delivery_map_url=_order_delivery_map_url(order),
        estimated_income_twd=_driver_estimated_income(order),
        driver_city_block=_driver_city_block(driver),
        driver_area_label=_driver_area_label(driver),
    )


def _accept_order(db, *, user, driver, order):
    if not _driver_is_active(driver):
        raise ValueError("Shiper 帳號尚未通過審核，不能接單。")

    if int(driver["is_online"] or 0) != 1:
        raise ValueError("請先上線，才能接單。")

    if order["status"] != "WAITING_DRIVER" or order["driver_id"]:
        raise ValueError("此訂單已被其他 shiper 接走，或尚未開放接單。")

    if int(order["admin_hold"] or 0) == 1:
        raise ValueError("此訂單已被 Admin 暫停，不能接單。")

    driver_city_block = _driver_city_block(driver)
    order_city_block = _text(_safe_row_get(order, "city_block", "")).upper()

    if order_city_block and order_city_block != driver_city_block:
        raise ValueError("此訂單不在你的接單區域，不能接單。")

    capacity = _driver_capacity(db, driver["id"])
    active_count = capacity["active_count"]

    if not capacity["can_accept"]:
        raise ValueError(capacity["message"])

    now = now_iso()

    cur = db.execute(
        """
        UPDATE orders
        SET status = 'DRIVER_ACCEPTED',
            driver_id = ?,
            updated_at = ?
        WHERE id = ?
          AND status = 'WAITING_DRIVER'
          AND driver_id IS NULL
          AND COALESCE(admin_hold, 0) = 0
        """,
        (
            driver["id"],
            now,
            order["id"],
        ),
    )

    if cur.rowcount <= 0:
        raise ValueError("此訂單已被其他 shiper 接走或狀態已更新。")

    active_count_after = active_count + 1
    remaining_after = max(0, MAX_ACTIVE_ORDERS - active_count_after)

    create_block(
        db,
        event_type="DRIVER_ACCEPTED",
        actor_role="DRIVER",
        actor_id=user["id"],
        actor_code=driver["driver_code"],
        order_id=order["id"],
        order_code=order["order_code"],
        previous_status="WAITING_DRIVER",
        new_status="DRIVER_ACCEPTED",
        amount_twd=order["total_twd"],
        payload={
            "driver_code": driver["driver_code"],
            "driver_name": driver["driver_name"],
            "lock_rule": "status WAITING_DRIVER + driver_id IS NULL + admin_hold 0",
            "max_active_orders": MAX_ACTIVE_ORDERS,
            "active_count_before": active_count,
            "active_count_after": active_count_after,
            "remaining_after": remaining_after,
            "capacity_rule": "Driver can hold up to 5 active delivery orders.",
            "city_block": _safe_row_get(order, "city_block", ""),
            "area_label": _safe_row_get(order, "area_label", ""),
            "smartroad_lane": _safe_row_get(order, "smartroad_lane", ""),
            "smartroad_score": _safe_row_get(order, "smartroad_score", 50),
            "distance_km": _safe_row_get(order, "distance_km", 0),
            "distance_band": _safe_row_get(order, "distance_band", ""),
        },
        commit=False,
    )

    db.commit()

    refreshed = _get_order_for_driver_page(db, order["order_code"])

    push_to_role_target(
        db,
        role="STORE",
        target_code=refreshed["store_code"],
        event_type="DRIVER_ACCEPTED",
        order_code=refreshed["order_code"],
        message=(
            "FUMAP GO Shiper 已接單\n"
            f"訂單：{refreshed['order_code']}\n"
            f"Shiper：{driver['driver_name']}\n"
            f"電話：{driver['phone'] or '-'}"
        ),
        commit=True,
    )

def _cancel_accept_order(db, *, user, driver, order, reason=""):
    if not _driver_is_active(driver):
        raise ValueError("Shiper 帳號尚未通過審核，不能操作訂單。")

    if not _driver_owns_order(order, driver):
        raise ValueError("此訂單不屬於你，不能取消接單。")

    if order["status"] != "DRIVER_ACCEPTED":
        raise ValueError("只有尚未取餐的訂單可以取消接單。已取餐後請使用配送異常流程。")

    if int(order["admin_hold"] or 0) == 1:
        raise ValueError("此訂單已被 Admin 暫停，不能取消接單。")

    now = now_iso()
    reason = _text(reason, "Shiper 取消接單") or "Shiper 取消接單"

    cur = db.execute(
        """
        UPDATE orders
        SET status = 'WAITING_DRIVER',
            driver_id = NULL,
            updated_at = ?
        WHERE id = ?
          AND driver_id = ?
          AND status = 'DRIVER_ACCEPTED'
        """,
        (
            now,
            order["id"],
            driver["id"],
        ),
    )

    if cur.rowcount <= 0:
        raise ValueError("取消接單失敗，此訂單狀態可能已更新。")

    create_block(
        db,
        event_type="DRIVER_CANCELLED_ACCEPT",
        actor_role="DRIVER",
        actor_id=user["id"],
        actor_code=driver["driver_code"],
        order_id=order["id"],
        order_code=order["order_code"],
        previous_status="DRIVER_ACCEPTED",
        new_status="WAITING_DRIVER",
        amount_twd=order["total_twd"],
        payload={
            "driver_code": driver["driver_code"],
            "driver_name": driver["driver_name"],
            "order_code": order["order_code"],
            "reason": reason,
            "rule": "Only DRIVER_ACCEPTED orders can be released before pickup.",
        },
        commit=False,
    )

    db.commit()


def _pickup_order(db, *, user, driver, order):
    if not _driver_owns_order(order, driver):
        raise ValueError("此訂單不屬於你。")

    if order["status"] != "DRIVER_ACCEPTED":
        raise ValueError("此訂單目前不能取餐。")

    now = now_iso()
    settlement = calculate_order_settlement(order)

    db.execute(
        """
        UPDATE orders
        SET status = 'PICKED_UP',
            updated_at = ?
        WHERE id = ?
          AND driver_id = ?
          AND status = 'DRIVER_ACCEPTED'
        """,
        (now, order["id"], driver["id"]),
    )

    create_block(
        db,
        event_type="PICKUP_BLOCK",
        actor_role="DRIVER",
        actor_id=user["id"],
        actor_code=driver["driver_code"],
        order_id=order["id"],
        order_code=order["order_code"],
        previous_status="DRIVER_ACCEPTED",
        new_status="PICKED_UP",
        amount_twd=order["total_twd"],
        payload={
            "driver_code": driver["driver_code"],
            "store_code": order["store_code"],
            "store_phone": order["store_phone"],
            "store_address": order["store_address"],
            "store_lat": _safe_row_get(order, "store_lat", 0),
            "store_lng": _safe_row_get(order, "store_lng", 0),
            "pickup_map_url": _order_pickup_map_url(order),
            "cod_v2_rule": "Shiper pays store subtotal minus store delivery support. Store platform fee is settled by store with Admin separately.",
            "driver_collect_from_customer_twd": settlement["driver_collect_from_customer_twd"],
            "driver_pay_store_twd": settlement["driver_pay_store_twd"],
            "driver_pay_admin_twd": settlement["driver_pay_admin_twd"],
            "driver_gross_twd": settlement["driver_gross_twd"],
            "driver_net_income_twd": settlement["driver_net_income_twd"],
            "store_platform_fee_twd": settlement["store_platform_fee_twd"],
            "admin_total_receivable_twd": settlement["admin_total_receivable_twd"],
            "note": "Shiper 已到店取餐。COD V2：店家平台費由店家自行與 Admin 結算，Shiper 不代收。",
        },
        commit=False,
    )

    db.commit()

    if order["customer_user_id"]:
        push_to_role_target(
            db,
            role="CUSTOMER",
            target_code=f"CUS-{order['customer_user_id']}",
            event_type="PICKED_UP",
            order_code=order["order_code"],
            message=(
                "FUMAP GO 配送更新\n"
                f"訂單：{order['order_code']}\n"
                "Shiper 已取餐，正在前往你的地址。"
            ),
            commit=True,
        )


def _report_delivery_issue(
    db,
    *,
    user,
    driver,
    order,
    issue_reason="",
    issue_note="",
    contacted_customer=False,
    contacted_store=False,
):
    if not _driver_owns_order(order, driver):
        raise ValueError("此訂單不屬於你。")

    if order["status"] != "PICKED_UP":
        raise ValueError("只有已取餐且尚未完成配送的訂單可以回報配送異常。")

    if int(order["admin_hold"] or 0) == 1:
        raise ValueError("此訂單已被 Admin 暫停，不能回報配送異常。")

    issue_reason = _text(issue_reason, "其他") or "其他"
    issue_note = _text(issue_note, "")
    now = now_iso()
    settlement = calculate_order_settlement(order)

    cur = db.execute(
        """
        UPDATE orders
        SET status = 'DELIVERY_ISSUE',
            updated_at = ?
        WHERE id = ?
          AND driver_id = ?
          AND status = 'PICKED_UP'
        """,
        (
            now,
            order["id"],
            driver["id"],
        ),
    )

    if cur.rowcount <= 0:
        raise ValueError("回報配送異常失敗，此訂單狀態可能已更新。")

    create_block(
        db,
        event_type="DELIVERY_ISSUE_REPORTED",
        actor_role="DRIVER",
        actor_id=user["id"],
        actor_code=driver["driver_code"],
        order_id=order["id"],
        order_code=order["order_code"],
        previous_status="PICKED_UP",
        new_status="DELIVERY_ISSUE",
        amount_twd=order["total_twd"],
        payload={
            "driver_code": driver["driver_code"],
            "driver_name": driver["driver_name"],
            "order_code": order["order_code"],
            "issue_reason": issue_reason,
            "issue_note": issue_note,
            "contacted_customer": bool(contacted_customer),
            "contacted_store": bool(contacted_store),
            "customer_phone": order["customer_phone"],
            "store_code": order["store_code"],
            "store_name": order["store_name"],
            "store_phone": order["store_phone"],
            "payment_method": order["payment_method"],
            "driver_pay_store_twd": settlement["driver_pay_store_twd"],
            "driver_collect_from_customer_twd": settlement["driver_collect_from_customer_twd"],
            "note": "After PICKED_UP, driver cannot cancel freely. Delivery issue flow starts.",
        },
        commit=False,
    )

    db.commit()
    refreshed = _get_order_for_driver_page(db, order["order_code"])

    try:
        push_to_role_target(
            db,
            role="STORE",
            target_code=refreshed["store_code"],
            event_type="DELIVERY_ISSUE_REPORTED",
            order_code=refreshed["order_code"],
            message=(
                "FUMAP GO 配送異常通知\n"
                f"訂單：{refreshed['order_code']}\n"
                f"原因：{issue_reason}\n"
                "Shiper 已回報配送異常，請留意後續退回店家流程。"
            ),
            commit=True,
        )
    except Exception as exc:
        print(f"[DELIVERY_ISSUE][LINE][STORE_ERROR] {exc}")

    if refreshed["customer_user_id"]:
        try:
            push_to_role_target(
                db,
                role="CUSTOMER",
                target_code=f"CUS-{refreshed['customer_user_id']}",
                event_type="DELIVERY_ISSUE_REPORTED",
                order_code=refreshed["order_code"],
                message=(
                    "FUMAP GO 配送異常通知\n"
                    f"訂單：{refreshed['order_code']}\n"
                    f"原因：{issue_reason}\n"
                    "Shiper 已回報配送異常，系統將依流程處理。"
                ),
                commit=True,
            )
        except Exception as exc:
            print(f"[DELIVERY_ISSUE][LINE][CUSTOMER_ERROR] {exc}")

def _mark_returning_to_store(
    db,
    *,
    user,
    driver,
    order,
    return_reason="配送異常後退回店家",
):
    if not _driver_owns_order(order, driver):
        raise ValueError("此訂單不屬於你。")

    if order["status"] != "DELIVERY_ISSUE":
        raise ValueError("只有配送異常中的訂單可以進入退回店家流程。")

    if int(order["admin_hold"] or 0) == 1:
        raise ValueError("此訂單已被 Admin 暫停，不能操作退回店家。")

    return_reason = _text(return_reason, "配送異常後退回店家") or "配送異常後退回店家"
    now = now_iso()
    settlement = calculate_order_settlement(order)

    cur = db.execute(
        """
        UPDATE orders
        SET status = 'RETURNING_TO_STORE',
            updated_at = ?
        WHERE id = ?
          AND driver_id = ?
          AND status = 'DELIVERY_ISSUE'
        """,
        (
            now,
            order["id"],
            driver["id"],
        ),
    )

    if cur.rowcount <= 0:
        raise ValueError("進入退回店家流程失敗，此訂單狀態可能已更新。")

    create_block(
        db,
        event_type="RETURNING_TO_STORE",
        actor_role="DRIVER",
        actor_id=user["id"],
        actor_code=driver["driver_code"],
        order_id=order["id"],
        order_code=order["order_code"],
        previous_status="DELIVERY_ISSUE",
        new_status="RETURNING_TO_STORE",
        amount_twd=order["total_twd"],
        payload={
            "driver_code": driver["driver_code"],
            "driver_name": driver["driver_name"],
            "order_code": order["order_code"],
            "return_reason": return_reason,
            "store_code": order["store_code"],
            "store_name": order["store_name"],
            "store_phone": order["store_phone"],
            "store_address": order["store_address"],
            "pickup_map_url": _order_pickup_map_url(order),
            "payment_method": order["payment_method"],
            "driver_pay_store_twd": settlement["driver_pay_store_twd"],
            "note": "Driver is returning goods to store after delivery issue.",
        },
        commit=False,
    )

    db.commit()
    refreshed = _get_order_for_driver_page(db, order["order_code"])

    try:
        push_to_role_target(
            db,
            role="STORE",
            target_code=refreshed["store_code"],
            event_type="RETURNING_TO_STORE",
            order_code=refreshed["order_code"],
            message=(
                "FUMAP GO 商品退回中\n"
                f"訂單：{refreshed['order_code']}\n"
                "Shiper 正在將商品退回店家，請準備確認商品與款項。"
            ),
            commit=True,
        )
    except Exception as exc:
        print(f"[RETURNING_TO_STORE][LINE][STORE_ERROR] {exc}")


def _confirm_returned_to_store(
    db,
    *,
    user,
    driver,
    order,
    money_returned_by_store="NOT_APPLICABLE",
    amount_returned_twd=0,
    return_note="",
    contacted_store=False,
    contacted_admin=False,
    return_proof_file=None,
):
    if not _driver_owns_order(order, driver):
        raise ValueError("此訂單不屬於你。")

    if order["status"] != "RETURNING_TO_STORE":
        raise ValueError("只有退回店家中的訂單可以確認已退回店家。")

    if int(order["admin_hold"] or 0) == 1:
        raise ValueError("此訂單已被 Admin 暫停，不能確認退回。")

    if not return_proof_file or not getattr(return_proof_file, "filename", ""):
        raise ValueError("請先上傳退回店家證明圖片。")

    money_returned_by_store = _text(
        money_returned_by_store,
        "NOT_APPLICABLE",
    ).upper()

    if money_returned_by_store not in {"YES", "NO", "NOT_APPLICABLE"}:
        money_returned_by_store = "NOT_APPLICABLE"

    amount_returned_twd = _money(amount_returned_twd)
    return_note = _text(return_note, "")
    now = now_iso()
    settlement = calculate_order_settlement(order)

    return_proof_url = save_compressed_upload(
        return_proof_file,
        kind="proof_image",
        owner_code=f"return-{order['order_code']}",
    )
    return_proof_uploaded_at = now

    cur = db.execute(
        """
        UPDATE orders
        SET status = 'RETURNED_TO_STORE',
            return_proof_image_url = ?,
            return_proof_uploaded_at = ?,
            updated_at = ?
        WHERE id = ?
          AND driver_id = ?
          AND status = 'RETURNING_TO_STORE'
        """,
        (
            return_proof_url,
            return_proof_uploaded_at,
            now,
            order["id"],
            driver["id"],
        ),
    )

    if cur.rowcount <= 0:
        raise ValueError("確認退回失敗，此訂單狀態可能已更新。")

    create_block(
        db,
        event_type="RETURNED_TO_STORE",
        actor_role="DRIVER",
        actor_id=user["id"],
        actor_code=driver["driver_code"],
        order_id=order["id"],
        order_code=order["order_code"],
        previous_status="RETURNING_TO_STORE",
        new_status="RETURNED_TO_STORE",
        amount_twd=order["total_twd"],
        payload={
            "driver_code": driver["driver_code"],
            "driver_name": driver["driver_name"],
            "order_code": order["order_code"],
            "store_code": order["store_code"],
            "store_name": order["store_name"],
            "payment_method": order["payment_method"],
            "money_returned_by_store": money_returned_by_store,
            "amount_returned_twd": amount_returned_twd,
            "expected_return_amount_twd": settlement["driver_pay_store_twd"],
            "return_note": return_note,
            "contacted_store": bool(contacted_store),
            "contacted_admin": bool(contacted_admin),
            "return_proof_image_url": return_proof_url,
            "return_proof_uploaded_at": return_proof_uploaded_at,
            "next_step": "ADMIN_REVIEW",
            "note": "Goods returned to store with proof image. Admin should review settlement, COD, refund or dispute.",
        },
        commit=False,
    )

    db.commit()

    refreshed = _get_order_for_driver_page(db, order["order_code"])

    try:
        _send_returned_to_store_notifications(db, refreshed)
    except Exception as exc:
        print(f"[RETURNED_TO_STORE][NOTIFY][ERROR] {exc}")


def _deliver_order(db, *, user, driver, order):
    if not _driver_owns_order(order, driver):
        raise ValueError("此訂單不屬於你。")

    if order["status"] != "PICKED_UP":
        raise ValueError("請先按已取餐，才能完成配送。配送異常或退回中的訂單不能直接完成配送。")

    if int(order["admin_hold"] or 0) == 1:
        raise ValueError("此訂單已被 Admin 暫停，不能完成配送。")

    delivery_method = (order["delivery_method"] or "FACE_TO_FACE").upper()
    proof_required = delivery_method == "PHOTO_PROOF"

    now = now_iso()
    proof_url = ""
    delivery_proof_uploaded_at = ""

    file = request.files.get("delivery_proof")

    if file and getattr(file, "filename", ""):
        proof_url = save_compressed_upload(
            file,
            kind="proof_image",
            owner_code=f"delivery-{order['order_code']}",
        )
        delivery_proof_uploaded_at = now

    if proof_required and not proof_url:
        raise ValueError("此單選擇拍照完成，請先上傳配送證明圖片。")

    cur = db.execute(
        """
        UPDATE orders
        SET status = 'DELIVERED',
            proof_image_url = CASE
                WHEN ? != '' THEN ?
                ELSE COALESCE(proof_image_url, '')
            END,
            delivery_proof_image_url = CASE
                WHEN ? != '' THEN ?
                ELSE COALESCE(delivery_proof_image_url, '')
            END,
            delivery_proof_uploaded_at = CASE
                WHEN ? != '' THEN ?
                ELSE COALESCE(delivery_proof_uploaded_at, '')
            END,
            payment_status = CASE
                WHEN payment_method = 'COD' THEN 'COD_COLLECTED'
                ELSE payment_status
            END,
            updated_at = ?
        WHERE id = ?
          AND driver_id = ?
          AND status = 'PICKED_UP'
        """,
        (
            proof_url,
            proof_url,
            proof_url,
            proof_url,
            delivery_proof_uploaded_at,
            delivery_proof_uploaded_at,
            now,
            order["id"],
            driver["id"],
        ),
    )

    if cur.rowcount <= 0:
        raise ValueError("完成配送失敗，此訂單狀態可能已更新。")

    settlement = calculate_order_settlement(order)

    create_block(
        db,
        event_type="DELIVERY_BLOCK",
        actor_role="DRIVER",
        actor_id=user["id"],
        actor_code=driver["driver_code"],
        order_id=order["id"],
        order_code=order["order_code"],
        previous_status="PICKED_UP",
        new_status="DELIVERED",
        amount_twd=order["total_twd"],
        payload={
            "driver_code": driver["driver_code"],
            "delivery_method": delivery_method,
            "proof_required": proof_required,
            "proof_image_stored": bool(proof_url),
            "proof_image_url": proof_url,
            "delivery_proof_uploaded_at": delivery_proof_uploaded_at,
            "delivered_at": now,
            "payment_method": order["payment_method"],
            "payment_status_after": "COD_COLLECTED"
            if order["payment_method"] == "COD"
            else order["payment_status"],
            "customer_phone": order["customer_phone"],
            "delivery_address": order["delivery_address"],
            "delivery_lat": _safe_row_get(order, "delivery_lat", 0),
            "delivery_lng": _safe_row_get(order, "delivery_lng", 0),
            "delivery_map_url": _order_delivery_map_url(order),
            "driver_collect_from_customer_twd": settlement["driver_collect_from_customer_twd"],
            "driver_pay_store_twd": settlement["driver_pay_store_twd"],
            "driver_pay_admin_twd": settlement["driver_pay_admin_twd"],
            "driver_keep_twd": settlement["driver_keep_twd"],
            "driver_gross_twd": settlement["driver_gross_twd"],
            "driver_net_income_twd": settlement["driver_net_income_twd"],
            "store_platform_fee_twd": settlement["store_platform_fee_twd"],
            "admin_total_receivable_twd": settlement["admin_total_receivable_twd"],
            "note": "Shiper confirmed delivery. COD V2: store platform fee is settled by store with Admin separately.",
        },
        commit=False,
    )

    if proof_url:
        create_block(
            db,
            event_type="DELIVERY_PROOF_UPLOADED",
            actor_role="DRIVER",
            actor_id=user["id"],
            actor_code=driver["driver_code"],
            order_id=order["id"],
            order_code=order["order_code"],
            previous_status="",
            new_status="DELIVERY_PROOF_STORED",
            amount_twd=0,
            payload={
                "order_code": order["order_code"],
                "proof_type": "DELIVERY_PROOF_IMAGE",
                "proof_image_stored": True,
                "delivery_proof_image_url": proof_url,
                "delivery_proof_uploaded_at": delivery_proof_uploaded_at,
                "note": "Delivery proof image saved by driver. Email will not attach this image.",
            },
            commit=False,
        )

    refreshed = _get_order_for_driver_page(db, order["order_code"])

    create_delivery_accounting_entries(
        db,
        order=refreshed,
        driver=driver,
        commit=False,
    )

    _push_delivery_updates(db, refreshed)

    db.commit()

    try:
        customer = None

        if refreshed["customer_user_id"]:
            customer = db.execute(
                """
                SELECT id, email, email_verified_at
                FROM users
                WHERE id = ?
                LIMIT 1
                """,
                (refreshed["customer_user_id"],),
            ).fetchone()

        customer_email = ""
        email_verified_at = ""

        if customer:
            customer_email = _text(_safe_row_get(customer, "email", ""))
            email_verified_at = _text(_safe_row_get(customer, "email_verified_at", ""))

        if customer_email and email_verified_at:
            email_sent = send_customer_delivery_proof_email(
                refreshed,
                customer_email,
                order_url=f"/orders?order_code={refreshed['order_code']}",
            )

            if email_sent:
                db.execute(
                    """
                    UPDATE orders
                    SET delivery_proof_sent_email_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        now_iso(),
                        now_iso(),
                        refreshed["id"],
                    ),
                )
                db.commit()

    except Exception as email_exc:
        print(f"[DELIVERY_PROOF][EMAIL][ERROR] {email_exc}")


@driver_bp.get("/accounting")
@login_required
@role_required("DRIVER")
def accounting():
    db = get_db()
    driver = _require_driver_or_redirect()

    if not driver:
        return redirect("/login")

    entries = list_driver_accounting_entries(db, driver["driver_code"], limit=300)
    summary = driver_accounting_summary(db, driver)
    done_orders = _list_driver_done_orders(db, driver["id"], limit=200)

    payout_summary = None
    admin_debt_summary = None

    try:
        payout_summary = get_target_payout_summary(
            db,
            role="DRIVER",
            target_code=driver["driver_code"],
        )
    except Exception as exc:
        print(f"[DRIVER][ACCOUNTING][PAYOUT_SUMMARY][ERROR] {exc}")

    try:
        admin_debt_summary = get_target_admin_debt_summary(
            db,
            role="DRIVER",
            target_code=driver["driver_code"],
        )
    except Exception as exc:
        print(f"[DRIVER][ACCOUNTING][ADMIN_DEBT_SUMMARY][ERROR] {exc}")

    return render_template(
        "mobile/driver/accounting.html",
        driver=driver,
        entries=entries,
        summary=summary,
        done_orders=done_orders,
        payout_summary=payout_summary,
        admin_debt_summary=admin_debt_summary,
    )


@driver_bp.post("/payout-account")
@driver_bp.post("/payout/bank-account")
@login_required
@role_required("DRIVER")
def payout_account_update():
    db = get_db()
    user = current_user()
    driver = _require_driver_or_redirect()

    if not driver:
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
            UPDATE drivers
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
                driver["id"],
            ),
        )

        create_block(
            db,
            event_type="DRIVER_PAYOUT_ACCOUNT_UPDATED",
            actor_role="DRIVER",
            actor_id=user["id"],
            actor_code=driver["driver_code"],
            payload={
                "driver_code": driver["driver_code"],
                "driver_name": driver["driver_name"],
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
        flash("Shiper 收款帳戶已更新。", "success")

    except Exception as exc:
        db.rollback()
        flash(f"更新收款帳戶失敗：{exc}", "danger")

    return redirect("/driver/accounting")


@driver_bp.post("/payout/request")
@login_required
@role_required("DRIVER")
def payout_request():
    db = get_db()
    user = current_user()
    driver = _require_driver_or_redirect()

    if not driver:
        return redirect("/login")

    try:
        amount_twd = _int(request.form.get("amount_twd"), 0)
        note = request.form.get("note", "").strip()

        payout_summary = get_target_payout_summary(
            db,
            role="DRIVER",
            target_code=driver["driver_code"],
        )

        available = int(payout_summary.get("available_to_request_twd") or 0)

        if available <= 0:
            raise ValueError("目前沒有可申請付款的金額。")

        if amount_twd <= 0:
            amount_twd = available

        if amount_twd > available:
            raise ValueError("申請金額不能超過可申請付款金額。")

        settlement = create_target_payout_request(
            db,
            role="DRIVER",
            target_code=driver["driver_code"],
            amount_twd=amount_twd,
            note=note or "Requested by DRIVER from driver accounting page",
            commit=False,
        )

        create_block(
            db,
            event_type="SETTLEMENT_REQUESTED_BY_DRIVER",
            actor_role="DRIVER",
            actor_id=user["id"],
            actor_code=driver["driver_code"],
            amount_twd=amount_twd,
            payload={
                "driver_code": driver["driver_code"],
                "driver_name": driver["driver_name"],
                "settlement_code": settlement.get("settlement_code"),
                "direction": "ADMIN_OWES_TARGET",
                "settlement_type": "ADMIN_PAYOUT_DRIVER",
                "amount_twd": amount_twd,
                "available_to_request_twd": available,
                "note": note,
            },
            commit=False,
        )

        db.commit()

        _notify_admin_driver_payout_requested(
            db,
            driver=driver,
            settlement=settlement,
            payout_summary=payout_summary,
            note=note,
        )

        flash("已送出 Admin 付款申請。Admin 確認轉帳後，對帳金額會自動更新。", "success")

    except Exception as exc:
        db.rollback()
        flash(f"申請 Admin 付款失敗：{exc}", "danger")

    return redirect("/driver/accounting")


@driver_bp.post("/settlements/<settlement_code>/mark-paid")
@login_required
@role_required("DRIVER")
def settlement_mark_paid(settlement_code):
    db = get_db()
    user = current_user()
    driver = _require_driver_or_redirect()

    if not driver:
        return redirect("/login")

    try:
        payment_method = (
            request.form.get("payment_method", "BANK_TRANSFER").strip().upper()
            or "BANK_TRANSFER"
        )
        note = request.form.get("note", "").strip()

        settlement = mark_settlement_target_paid(
            db,
            settlement_code,
            role="DRIVER",
            target_code=driver["driver_code"],
            payment_method=payment_method,
            note=note,
            commit=False,
        )

        create_block(
            db,
            event_type="SETTLEMENT_TARGET_MARKED_PAID",
            actor_role="DRIVER",
            actor_id=user["id"],
            actor_code=driver["driver_code"],
            amount_twd=settlement.get("amount_twd", 0),
            payload={
                "driver_code": driver["driver_code"],
                "driver_name": driver["driver_name"],
                "settlement_code": settlement.get("settlement_code"),
                "direction": settlement.get("direction"),
                "settlement_type": settlement.get("settlement_type"),
                "amount_twd": settlement.get("amount_twd", 0),
                "payment_method": payment_method,
                "note": note,
                "target_marked_paid_at": settlement.get("target_marked_paid_at"),
                "important_rule": "This does not finalize settlement. Admin must confirm-paid.",
            },
            commit=False,
        )

        db.commit()

        _notify_admin_driver_marked_paid(
            db,
            driver=driver,
            settlement=settlement,
            note=note,
            payment_method=payment_method,
        )

        flash("已回報付款。請等待 Admin 核對入帳並確認收款。", "success")

    except Exception as exc:
        db.rollback()
        flash(f"回報付款失敗：{exc}", "danger")

    return redirect("/driver/accounting")

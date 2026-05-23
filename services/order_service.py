import re
import secrets

from services.delivery_fee_service import (
    DeliveryFeeError,
    build_delivery_fee_rule_json,
    calculate_distance_and_fee_for_order,
    calculate_single_extra_fee,
)
from services.smartroad_service import (
    calculate_smartroad_score,
    smartroad_db_payload,
)
from services.code_service import now_iso, unique_code, generate_order_code
from services.block_service import create_block
from services.store_hours_service import (
    is_store_accepting_orders,
    annotate_store_hours,
)

try:
    from services.line_bind_service import customer_can_photo_proof
except Exception:
    def customer_can_photo_proof(db, user):
        return False

try:
    from services.line_notify_service import push_to_role_target
except Exception:
    def push_to_role_target(db, **kwargs):
        return {"ok": False, "skipped": True, "error": "line notify unavailable"}

try:
    from services.email_service import (
        send_customer_order_created_email,
        send_admin_order_created_email,
    )
except Exception:
    def send_customer_order_created_email(*args, **kwargs):
        return False

    def send_admin_order_created_email(*args, **kwargs):
        return False


ORDER_STATUSES = {
    "CREATED",
    "STORE_ACCEPTED",
    "WAITING_DRIVER",
    "DRIVER_ACCEPTED",
    "PICKED_UP",
    "DELIVERY_ISSUE",
    "RETURNING_TO_STORE",
    "RETURNED_TO_STORE",
    "DELIVERED",
    "COMPLETED",
    "CANCELLED",
    "DISPUTED",
}

PAYMENT_METHODS = {"COD", "BANK_TRANSFER", "PREPAID_TO_STORE"}
DELIVERY_METHODS = {"FACE_TO_FACE", "PHOTO_PROOF"}
INVOICE_TYPES = {"NONE", "RECEIPT", "COMPANY_INVOICE"}


class OrderError(ValueError):
    pass


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


def _money(value):
    return max(0, _int(value, 0))


def _clean(value):
    return (value or "").strip()


def _row_get(row, key, default=None):
    try:
        if row is not None and key in row.keys():
            return row[key]
    except Exception:
        pass

    try:
        if isinstance(row, dict) and key in row:
            return row[key]
    except Exception:
        pass

    return default


def _valid_email(value):
    value = (value or "").strip().lower()

    if not value:
        return False

    if len(value) > 255:
        return False

    if "@" not in value:
        return False

    local, _, domain = value.partition("@")

    if not local or not domain:
        return False

    if "." not in domain:
        return False

    return True


def normalize_customer_email(value):
    value = (value or "").strip().lower()

    if not value:
        return ""

    if not _valid_email(value):
        raise OrderError("Email 格式不正確。")

    return value


def generate_guest_access_token(db):
    for _ in range(10):
        token = secrets.token_urlsafe(32)

        exists = db.execute(
            """
            SELECT id
            FROM orders
            WHERE guest_access_token = ?
            LIMIT 1
            """,
            (token,),
        ).fetchone()

        if not exists:
            return token

    raise OrderError("建立訪客訂單追蹤碼失敗，請再試一次。")


def normalize_payment_method(value):
    value = (value or "COD").strip().upper()

    if value not in PAYMENT_METHODS:
        value = "COD"

    return value


def normalize_delivery_method(value):
    value = (value or "FACE_TO_FACE").strip().upper()

    if value not in DELIVERY_METHODS:
        value = "FACE_TO_FACE"

    return value


def normalize_invoice_request(
    invoice_required=0,
    invoice_type="NONE",
    invoice_title="",
    invoice_tax_id="",
    invoice_note="",
):
    invoice_required = 1 if str(invoice_required) in {"1", "true", "True", "on"} else 0
    invoice_type = (invoice_type or "NONE").strip().upper()

    if invoice_type not in INVOICE_TYPES:
        invoice_type = "NONE"

    invoice_title = _clean(invoice_title)
    invoice_tax_id = re.sub(r"[^0-9]", "", invoice_tax_id or "")
    invoice_note = _clean(invoice_note)

    if not invoice_required or invoice_type == "NONE":
        return {
            "invoice_required": 0,
            "invoice_type": "NONE",
            "invoice_title": "",
            "invoice_tax_id": "",
            "invoice_note": "",
        }

    if invoice_tax_id and len(invoice_tax_id) != 8:
        raise OrderError("統一編號需為 8 碼數字。")

    return {
        "invoice_required": 1,
        "invoice_type": invoice_type,
        "invoice_title": invoice_title,
        "invoice_tax_id": invoice_tax_id,
        "invoice_note": invoice_note,
    }


def normalize_city_block(value):
    value = (value or "ZHONGLI").strip().upper()

    if value in {"TAOYUAN", "TAOYUAN_CITY", "桃園"}:
        return "TAOYUAN"

    return "ZHONGLI"


def area_label_for_city_block(city_block):
    city_block = normalize_city_block(city_block)

    if city_block == "TAOYUAN":
        return "桃園區"

    return "中壢區"


def parse_floor_number(floor_number):
    floor_number = _clean(floor_number)

    if not floor_number:
        return None

    nums = re.findall(r"\d+", floor_number)

    if not nums:
        return None

    try:
        return int(nums[0])
    except Exception:
        return None


def _default_smartroad_payload():
    return {
        "smartroad_score": 50,
        "smartroad_score_label": "UNKNOWN",
        "smartroad_reasons_json": "[]",
        "smartroad_same_road": 0,
        "smartroad_same_side": 0,
        "smartroad_uturn_risk": 0,
        "store_road_name": "",
        "customer_road_name": "",
        "store_house_number": "",
        "customer_house_number": "",
        "store_house_parity": "UNKNOWN",
        "customer_house_parity": "UNKNOWN",
        "smartroad_lane": "YELLOW",
    }


def _has_valid_gps(lat, lng):
    try:
        lat = float(lat or 0)
        lng = float(lng or 0)
        return lat != 0 and lng != 0 and -90 <= lat <= 90 and -180 <= lng <= 180
    except Exception:
        return False


def _calculate_order_smartroad_payload(
    *,
    store,
    delivery_address,
    city_block,
    distance_km,
    delivery_lat=0,
    delivery_lng=0,
    floor_number="",
    address_note="",
    difficulty_flags=None,
    rain_fee_twd=0,
    extra_fee_twd=0,
):
    try:
        result = calculate_smartroad_score(
            store_address=_row_get(store, "address", "") or "",
            delivery_address=delivery_address or "",
            store_city_block=_row_get(store, "city_block", "") or "",
            order_city_block=city_block or "",
            distance_km=distance_km or 0,
            has_valid_gps=_has_valid_gps(delivery_lat, delivery_lng),
            floor_number=floor_number or "",
            address_note=address_note or "",
            difficulty_flags=difficulty_flags or [],
            rain_fee_twd=rain_fee_twd or 0,
            extra_fee_twd=extra_fee_twd or 0,
        )
        return smartroad_db_payload(result)
    except Exception:
        return _default_smartroad_payload()


def _smartroad_block_payload(smartroad):
    smartroad = smartroad or _default_smartroad_payload()

    return {
        "smartroad_score": smartroad.get("smartroad_score", 50),
        "smartroad_score_label": smartroad.get("smartroad_score_label", "UNKNOWN"),
        "smartroad_lane": smartroad.get("smartroad_lane", "YELLOW"),
        "smartroad_same_road": smartroad.get("smartroad_same_road", 0),
        "smartroad_same_side": smartroad.get("smartroad_same_side", 0),
        "smartroad_uturn_risk": smartroad.get("smartroad_uturn_risk", 0),
    }


def _safe_push_to_role_target(db, **kwargs):
    try:
        return push_to_role_target(db, **kwargs)
    except Exception as exc:
        print(f"[LINE_PUSH][SKIPPED][ORDER_SERVICE] {exc}")
        return {
            "ok": False,
            "skipped": True,
            "error": str(exc),
        }


def _safe_send_customer_order_created_email(order, customer_email=None, order_url=None):
    try:
        return send_customer_order_created_email(
            order,
            customer_email=customer_email,
            order_url=order_url,
        )
    except Exception as exc:
        print(f"[EMAIL][CUSTOMER_ORDER_CREATED][SKIPPED] {exc}")
        return False


def _safe_send_admin_order_created_email(order, review_url=None):
    try:
        return send_admin_order_created_email(
            order,
            review_url=review_url,
        )
    except Exception as exc:
        print(f"[EMAIL][ADMIN_ORDER_CREATED][SKIPPED] {exc}")
        return False


def get_service_fee_twd():
    return 0


def get_store_by_code(db, store_code):
    store_code = (store_code or "").strip().upper()

    if not store_code:
        return None

    return db.execute(
        """
        SELECT *
        FROM stores
        WHERE store_code = ?
        LIMIT 1
        """,
        (store_code,),
    ).fetchone()


def get_store_by_id(db, store_id):
    return db.execute(
        """
        SELECT *
        FROM stores
        WHERE id = ?
        LIMIT 1
        """,
        (store_id,),
    ).fetchone()


def get_product(db, product_id, store_id=None):
    product_id = _int(product_id, 0)

    if not product_id:
        return None

    if store_id:
        return db.execute(
            """
            SELECT *
            FROM products
            WHERE id = ?
              AND store_id = ?
              AND is_active = 1
            LIMIT 1
            """,
            (product_id, store_id),
        ).fetchone()

    return db.execute(
        """
        SELECT *
        FROM products
        WHERE id = ?
          AND is_active = 1
        LIMIT 1
        """,
        (product_id,),
    ).fetchone()


def list_public_stores(db, city_block=None):
    params = []
    city_filter = ""

    if city_block:
        city_filter = "AND s.city_block = ?"
        params.append(normalize_city_block(city_block))

    rows = db.execute(
        f"""
        SELECT s.*,
               COUNT(p.id) AS product_count
        FROM stores s
        LEFT JOIN products p
          ON p.store_id = s.id
         AND p.is_active = 1
        WHERE s.status = 'ACTIVE'
          AND s.setup_completed = 1
          {city_filter}
        GROUP BY s.id
        ORDER BY
          CASE WHEN s.is_open = 1 THEN 0 ELSE 1 END,
          s.id DESC
        LIMIT 100
        """,
        params,
    ).fetchall()

    return [annotate_store_hours(row) for row in rows]


def list_store_products(db, store_id, only_active=True):
    if only_active:
        return db.execute(
            """
            SELECT *
            FROM products
            WHERE store_id = ?
              AND is_active = 1
            ORDER BY product_category ASC, sort_order ASC, id DESC
            """,
            (store_id,),
        ).fetchall()

    return db.execute(
        """
        SELECT *
        FROM products
        WHERE store_id = ?
        ORDER BY product_category ASC, sort_order ASC, id DESC
        """,
        (store_id,),
    ).fetchall()

def list_customer_orders(db, user_id, limit=100):
    return db.execute(
        """
        SELECT o.*,
               s.store_name,
               s.store_code,
               s.phone AS store_phone,
               d.driver_code,
               d.driver_name,
               d.phone AS driver_phone
        FROM orders o
        JOIN stores s ON s.id = o.store_id
        LEFT JOIN drivers d ON d.id = o.driver_id
        WHERE o.customer_user_id = ?
        ORDER BY o.id DESC
        LIMIT ?
        """,
        (user_id, int(limit or 100)),
    ).fetchall()


def list_store_orders(db, store_id, limit=150):
    return db.execute(
        """
        SELECT o.*,
               d.driver_code,
               d.driver_name,
               d.phone AS driver_phone
        FROM orders o
        LEFT JOIN drivers d ON d.id = o.driver_id
        WHERE o.store_id = ?
        ORDER BY
          CASE o.status
            WHEN 'CREATED' THEN 1
            WHEN 'STORE_ACCEPTED' THEN 2
            WHEN 'WAITING_DRIVER' THEN 3
            WHEN 'DRIVER_ACCEPTED' THEN 4
            WHEN 'PICKED_UP' THEN 5
            ELSE 9
          END,
          o.id DESC
        LIMIT ?
        """,
        (store_id, int(limit or 150)),
    ).fetchall()


def get_order_by_code(db, order_code):
    order_code = (order_code or "").strip().upper()

    return db.execute(
        """
        SELECT o.*,
               s.store_name,
               s.store_code,
               s.phone AS store_phone,
               s.address AS store_address,
               d.driver_code,
               d.driver_name,
               d.phone AS driver_phone
        FROM orders o
        JOIN stores s ON s.id = o.store_id
        LEFT JOIN drivers d ON d.id = o.driver_id
        WHERE o.order_code = ?
        LIMIT 1
        """,
        (order_code,),
    ).fetchone()


def get_guest_order_by_code_token(db, order_code, guest_access_token):
    order_code = (order_code or "").strip().upper()
    guest_access_token = (guest_access_token or "").strip()

    if not order_code or not guest_access_token:
        return None

    return db.execute(
        """
        SELECT o.*,
               s.store_name,
               s.store_code,
               s.phone AS store_phone,
               s.address AS store_address,
               d.driver_code,
               d.driver_name,
               d.phone AS driver_phone
        FROM orders o
        JOIN stores s ON s.id = o.store_id
        LEFT JOIN drivers d ON d.id = o.driver_id
        WHERE o.order_code = ?
          AND o.guest_access_token = ?
        LIMIT 1
        """,
        (order_code, guest_access_token),
    ).fetchone()


def get_order_items(db, order_id):
    return db.execute(
        """
        SELECT *
        FROM order_items
        WHERE order_id = ?
        ORDER BY id ASC
        """,
        (order_id,),
    ).fetchall()


def _order_customer_url(order):
    order_code = _row_get(order, "order_code", "")
    guest_token = _row_get(order, "guest_access_token", "")

    if guest_token:
        return f"/guest/orders/{order_code}?token={guest_token}"

    return f"/orders?order_code={order_code}"


def create_customer_order(
    db,
    *,
    customer_user=None,
    store_code,
    product_id,
    qty,
    customer_name,
    customer_phone,
    customer_email="",
    delivery_address,
    floor_number,
    address_note="",
    delivery_lat=0,
    delivery_lng=0,
    distance_band="0-2KM",
    city_block="ZHONGLI",
    difficulty_flags=None,
    payment_method="COD",
    delivery_method="FACE_TO_FACE",
    note="",
    invoice_required=0,
    invoice_type="NONE",
    invoice_title="",
    invoice_tax_id="",
    invoice_note="",
    order_source="CUSTOMER_MARKETPLACE",
):
    is_guest_order = customer_user is None

    if customer_user and customer_user["role"] != "CUSTOMER":
        raise OrderError("此角色不能建立客戶訂單。")

    store = get_store_by_code(db, store_code)

    if not store:
        raise OrderError("找不到店家。")

    hours_status = is_store_accepting_orders(store)

    if not hours_status["accepting"]:
        raise OrderError(
            f"店家目前無法接單：{hours_status['label']}。{hours_status['reason']}"
        )

    product = get_product(db, product_id, store["id"])

    if not product:
        raise OrderError("找不到商品或商品已下架。")

    qty = max(1, _int(qty, 1))

    user_display_name = _row_get(customer_user, "display_name", "") if customer_user else ""
    user_phone = _row_get(customer_user, "phone", "") if customer_user else ""
    user_email = _row_get(customer_user, "email", "") if customer_user else ""

    customer_name = _clean(customer_name) or _clean(user_display_name)
    customer_phone = _clean(customer_phone) or _clean(user_phone)
    customer_email = normalize_customer_email(customer_email or user_email)

    delivery_address = _clean(delivery_address)
    floor_number = _clean(floor_number)
    address_note = _clean(address_note)
    note = _clean(note)
    city_block = normalize_city_block(city_block)
    area_label = area_label_for_city_block(city_block)

    invoice_data = normalize_invoice_request(
        invoice_required=invoice_required,
        invoice_type=invoice_type,
        invoice_title=invoice_title,
        invoice_tax_id=invoice_tax_id,
        invoice_note=invoice_note,
    )

    delivery_lat = _float(delivery_lat, 0)
    delivery_lng = _float(delivery_lng, 0)
    distance_band = _clean(distance_band) or "0-2KM"
    difficulty_flags = difficulty_flags or []

    if not customer_name:
        raise OrderError("請輸入姓名。")

    if not customer_phone:
        raise OrderError("請輸入電話。")

    if not delivery_address:
        raise OrderError("請輸入收貨地址。")

    if not floor_number:
        raise OrderError("請輸入樓層，例如：1樓、5樓、無電梯。")

    has_email = bool(customer_email)
    has_line_bind = False

    if customer_user:
        try:
            has_line_bind = bool(customer_can_photo_proof(db, customer_user))
        except Exception:
            has_line_bind = False

    payment_method = normalize_payment_method(payment_method)
    delivery_method = normalize_delivery_method(delivery_method)

    if payment_method == "BANK_TRANSFER" and not has_email:
        raise OrderError("請輸入有效 Email 後，才能使用轉帳付款。")

    if delivery_method == "PHOTO_PROOF" and not has_email:
        raise OrderError("請輸入有效 Email 後，才能選擇拍照完成。")

    subtotal_twd = _money(product["price_twd"]) * qty

    try:
        delivery_fee = calculate_distance_and_fee_for_order(
            store_lat=_row_get(store, "store_lat", 0),
            store_lng=_row_get(store, "store_lng", 0),
            delivery_lat=delivery_lat,
            delivery_lng=delivery_lng,
            distance_band=distance_band,
            allow_admin_review=False,
        )
    except DeliveryFeeError as exc:
        raise OrderError(str(exc))

    extra = calculate_single_extra_fee(
        db,
        floor_number=floor_number,
        difficulty_flags=difficulty_flags,
        manual_extra_reason="",
        include_rain=True,
    )

    base_delivery_fee_twd = _money(delivery_fee["base_delivery_fee_twd"])
    customer_delivery_share_twd = _money(delivery_fee["customer_delivery_share_twd"])
    store_delivery_support_twd = _money(delivery_fee["store_delivery_support_twd"])

    delivery_fee_twd = base_delivery_fee_twd
    service_fee_twd = get_service_fee_twd()
    extra_fee_twd = _money(extra["extra_fee_twd"])
    rain_fee_twd = _money(extra["rain_fee_twd"])

    total_twd = subtotal_twd + customer_delivery_share_twd + extra_fee_twd

    delivery_fee_rule_json = build_delivery_fee_rule_json(delivery_fee, extra)
    distance_km = _float(delivery_fee.get("distance_km"), 0)
    normalized_distance_band = delivery_fee.get("distance_band", distance_band)

    payment_status = "UNPAID"

    if payment_method == "BANK_TRANSFER":
        payment_status = "PENDING"

    smartroad = _calculate_order_smartroad_payload(
        store=store,
        delivery_address=delivery_address,
        city_block=city_block,
        distance_km=distance_km,
        delivery_lat=delivery_lat,
        delivery_lng=delivery_lng,
        floor_number=floor_number,
        address_note=address_note,
        difficulty_flags=difficulty_flags,
        rain_fee_twd=rain_fee_twd,
        extra_fee_twd=extra_fee_twd,
    )

    now = now_iso()
    order_code = unique_code(db, "orders", "order_code", generate_order_code)

    if is_guest_order:
        normalized_order_source = "GUEST_CHECKOUT"
        guest_access_token = generate_guest_access_token(db)
        customer_user_id = None
        actor_role = "GUEST_CUSTOMER"
        actor_id = None
        actor_code = f"GUEST-{order_code}"
    else:
        normalized_order_source = _clean(order_source) or "CUSTOMER_MARKETPLACE"
        guest_access_token = ""
        customer_user_id = customer_user["id"]
        actor_role = "CUSTOMER"
        actor_id = customer_user["id"]
        actor_code = f"CUS-{customer_user['id']}"

    cur = db.execute(
        """
        INSERT INTO orders (
            order_code,
            customer_user_id,
            store_id,
            driver_id,
            status,
            payment_method,
            payment_status,
            delivery_method,
            subtotal_twd,
            delivery_fee_twd,
            base_delivery_fee_twd,
            customer_delivery_share_twd,
            store_delivery_support_twd,
            delivery_fee_rule_json,
            service_fee_twd,
            extra_fee_twd,
            rain_fee_twd,
            total_twd,
            delivery_address,
            delivery_lat,
            delivery_lng,
            distance_band,
            floor_number,
            address_note,
            extra_fee_reason,
            difficulty_flags_json,
            customer_name,
            customer_phone,
            customer_email,
            guest_access_token,
            note,
            proof_image_url,
            invoice_required,
            invoice_type,
            invoice_title,
            invoice_tax_id,
            invoice_note,
            city_block,
            area_label,
            smartroad_lane,
            distance_km,
            smartroad_score,
            smartroad_score_label,
            smartroad_reasons_json,
            smartroad_same_road,
            smartroad_same_side,
            smartroad_uturn_risk,
            store_road_name,
            customer_road_name,
            store_house_number,
            customer_house_number,
            store_house_parity,
            customer_house_parity,
            admin_hold,
            admin_hold_reason,
            admin_hold_at,
            order_source,
            created_at,
            updated_at
        )
        VALUES (
            :order_code,
            :customer_user_id,
            :store_id,
            NULL,
            'CREATED',
            :payment_method,
            :payment_status,
            :delivery_method,
            :subtotal_twd,
            :delivery_fee_twd,
            :base_delivery_fee_twd,
            :customer_delivery_share_twd,
            :store_delivery_support_twd,
            :delivery_fee_rule_json,
            :service_fee_twd,
            :extra_fee_twd,
            :rain_fee_twd,
            :total_twd,
            :delivery_address,
            :delivery_lat,
            :delivery_lng,
            :distance_band,
            :floor_number,
            :address_note,
            :extra_fee_reason,
            :difficulty_flags_json,
            :customer_name,
            :customer_phone,
            :customer_email,
            :guest_access_token,
            :note,
            '',
            :invoice_required,
            :invoice_type,
            :invoice_title,
            :invoice_tax_id,
            :invoice_note,
            :city_block,
            :area_label,
            :smartroad_lane,
            :distance_km,
            :smartroad_score,
            :smartroad_score_label,
            :smartroad_reasons_json,
            :smartroad_same_road,
            :smartroad_same_side,
            :smartroad_uturn_risk,
            :store_road_name,
            :customer_road_name,
            :store_house_number,
            :customer_house_number,
            :store_house_parity,
            :customer_house_parity,
            0,
            '',
            '',
            :order_source,
            :created_at,
            :updated_at
        )
        """,
        {
            "order_code": order_code,
            "customer_user_id": customer_user_id,
            "store_id": store["id"],
            "payment_method": payment_method,
            "payment_status": payment_status,
            "delivery_method": delivery_method,
            "subtotal_twd": subtotal_twd,
            "delivery_fee_twd": delivery_fee_twd,
            "base_delivery_fee_twd": base_delivery_fee_twd,
            "customer_delivery_share_twd": customer_delivery_share_twd,
            "store_delivery_support_twd": store_delivery_support_twd,
            "delivery_fee_rule_json": delivery_fee_rule_json,
            "service_fee_twd": service_fee_twd,
            "extra_fee_twd": extra_fee_twd,
            "rain_fee_twd": rain_fee_twd,
            "total_twd": total_twd,
            "delivery_address": delivery_address,
            "delivery_lat": delivery_lat,
            "delivery_lng": delivery_lng,
            "distance_band": normalized_distance_band,
            "floor_number": floor_number,
            "address_note": address_note,
            "extra_fee_reason": extra["extra_fee_reason"],
            "difficulty_flags_json": extra["difficulty_flags_json"],
            "customer_name": customer_name,
            "customer_phone": customer_phone,
            "customer_email": customer_email,
            "guest_access_token": guest_access_token,
            "note": note,
            "invoice_required": invoice_data["invoice_required"],
            "invoice_type": invoice_data["invoice_type"],
            "invoice_title": invoice_data["invoice_title"],
            "invoice_tax_id": invoice_data["invoice_tax_id"],
            "invoice_note": invoice_data["invoice_note"],
            "city_block": city_block,
            "area_label": area_label,
            "smartroad_lane": smartroad["smartroad_lane"],
            "distance_km": distance_km,
            "smartroad_score": smartroad["smartroad_score"],
            "smartroad_score_label": smartroad["smartroad_score_label"],
            "smartroad_reasons_json": smartroad["smartroad_reasons_json"],
            "smartroad_same_road": smartroad["smartroad_same_road"],
            "smartroad_same_side": smartroad["smartroad_same_side"],
            "smartroad_uturn_risk": smartroad["smartroad_uturn_risk"],
            "store_road_name": smartroad["store_road_name"],
            "customer_road_name": smartroad["customer_road_name"],
            "store_house_number": smartroad["store_house_number"],
            "customer_house_number": smartroad["customer_house_number"],
            "store_house_parity": smartroad["store_house_parity"],
            "customer_house_parity": smartroad["customer_house_parity"],
            "order_source": normalized_order_source,
            "created_at": now,
            "updated_at": now,
        },
    )

    order_id = cur.lastrowid

    db.execute(
        """
        INSERT INTO order_items (
            order_id,
            product_id,
            product_name,
            unit_price_twd,
            qty,
            line_total_twd,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            order_id,
            product["id"],
            product["name"],
            product["price_twd"],
            qty,
            subtotal_twd,
            now,
        ),
    )

    create_block(
        db,
        event_type="ORDER_CREATED",
        actor_role=actor_role,
        actor_id=actor_id,
        actor_code=actor_code,
        order_id=order_id,
        order_code=order_code,
        previous_status="",
        new_status="CREATED",
        amount_twd=total_twd,
        payload={
            "order_source": normalized_order_source,
            "is_guest_order": is_guest_order,
            "customer_user_id": customer_user_id,
            "customer_name": customer_name,
            "customer_phone": customer_phone,
            "customer_email_saved": bool(customer_email),
            "guest_tracking_enabled": bool(guest_access_token),
            "email_primary_notification": bool(customer_email),
            "line_optional_notification": bool(has_line_bind),
            "has_line_bind": bool(has_line_bind),
            "store_code": store["store_code"],
            "store_name": store["store_name"],
            "business_hours_status": hours_status,
            "product_id": product["id"],
            "product_name": product["name"],
            "qty": qty,
            "subtotal_twd": subtotal_twd,
            "delivery_fee_twd": delivery_fee_twd,
            "base_delivery_fee_twd": base_delivery_fee_twd,
            "customer_delivery_share_twd": customer_delivery_share_twd,
            "store_delivery_support_twd": store_delivery_support_twd,
            "delivery_fee_rule_json": delivery_fee_rule_json,
            "service_fee_twd": service_fee_twd,
            "extra_fee_twd": extra_fee_twd,
            "rain_fee_twd": rain_fee_twd,
            "extra_fee_reason": extra["extra_fee_reason"],
            "total_twd": total_twd,
            "payment_method": payment_method,
            "payment_status": payment_status,
            "delivery_method": delivery_method,
            "invoice_required": invoice_data["invoice_required"],
            "invoice_type": invoice_data["invoice_type"],
            "invoice_title": invoice_data["invoice_title"],
            "invoice_tax_id": invoice_data["invoice_tax_id"],
            "invoice_note": invoice_data["invoice_note"],
            "delivery_address": delivery_address,
            "delivery_lat": delivery_lat,
            "delivery_lng": delivery_lng,
            "distance_km": distance_km,
            "distance_band": normalized_distance_band,
            "floor_number": floor_number,
            "address_note": address_note,
            "city_block": city_block,
            "area_label": area_label,
            **_smartroad_block_payload(smartroad),
            "note": "客戶建立訂單，系統自動送入店家訂單中心。Email 為主要通知方式，LINE 綁定僅作為額外推播。",
        },
        commit=False,
    )

    db.commit()

    order = get_order_by_code(db, order_code)

    _safe_send_customer_order_created_email(
        order,
        customer_email=customer_email,
        order_url=_order_customer_url(order),
    )

    _safe_send_admin_order_created_email(
        order,
        review_url=f"/admin/orders?order_code={order_code}",
    )

    _safe_push_to_role_target(
        db,
        role="STORE",
        target_code=store["store_code"],
        event_type="ORDER_CREATED",
        order_code=order_code,
        message=(
            "FUMAP GO 新訂單通知\n"
            f"訂單：{order_code}\n"
            f"店家：{store['store_name']}\n"
            f"金額：{total_twd} TWD\n"
            f"付款：{payment_method} / {payment_status}\n"
            "請登入店家工作台確認接單。"
        ),
        commit=True,
    )

    if customer_user and has_line_bind:
        _safe_push_to_role_target(
            db,
            role="CUSTOMER",
            target_code=f"CUS-{customer_user['id']}",
            event_type="ORDER_CREATED",
            order_code=order_code,
            message=(
                "FUMAP GO 訂單已建立\n"
                f"訂單：{order_code}\n"
                f"店家：{store['store_name']}\n"
                f"金額：{total_twd} TWD\n"
                "您可到我的訂單查看狀態。"
            ),
            commit=True,
        )

    return order


def store_accept_order(db, *, store, order_code, actor_user):
    order = get_order_by_code(db, order_code)

    if not order:
        raise OrderError("找不到訂單。")

    if int(order["store_id"]) != int(store["id"]):
        raise OrderError("此訂單不屬於目前店家。")

    if int(order["admin_hold"] or 0) == 1:
        raise OrderError("此訂單已被 Admin 暫停，請先不要製作。")

    if order["status"] != "CREATED":
        raise OrderError("此訂單目前不能確認接單。")

    now = now_iso()

    db.execute(
        """
        UPDATE orders
        SET status = 'STORE_ACCEPTED',
            updated_at = ?
        WHERE id = ?
        """,
        (now, order["id"]),
    )

    create_block(
        db,
        event_type="STORE_ACCEPTED",
        actor_role="STORE",
        actor_id=actor_user["id"],
        actor_code=store["store_code"],
        order_id=order["id"],
        order_code=order["order_code"],
        previous_status="CREATED",
        new_status="STORE_ACCEPTED",
        amount_twd=order["total_twd"],
        payload={
            "store_code": store["store_code"],
            "note": "店家確認接單，開始製作。",
        },
        commit=False,
    )

    db.commit()

    return get_order_by_code(db, order_code)


def store_call_driver(db, *, store, order_code, actor_user):
    order = get_order_by_code(db, order_code)

    if not order:
        raise OrderError("找不到訂單。")

    if int(order["store_id"]) != int(store["id"]):
        raise OrderError("此訂單不屬於目前店家。")

    if int(order["admin_hold"] or 0) == 1:
        raise OrderError("此訂單已被 Admin 暫停，請先不要呼叫 Shiper。")

    if order["status"] != "STORE_ACCEPTED":
        raise OrderError("請先確認接單，完成商品後才能叫外送。")

    now = now_iso()

    db.execute(
        """
        UPDATE orders
        SET status = 'WAITING_DRIVER',
            updated_at = ?
        WHERE id = ?
        """,
        (now, order["id"]),
    )

    create_block(
        db,
        event_type="DRIVER_REQUESTED",
        actor_role="STORE",
        actor_id=actor_user["id"],
        actor_code=store["store_code"],
        order_id=order["id"],
        order_code=order["order_code"],
        previous_status="STORE_ACCEPTED",
        new_status="WAITING_DRIVER",
        amount_twd=order["total_twd"],
        payload={
            "store_code": store["store_code"],
            "city_block": order["city_block"],
            "area_label": order["area_label"],
            "smartroad_lane": order["smartroad_lane"],
            "smartroad_score": _row_get(order, "smartroad_score", 50),
            "smartroad_score_label": _row_get(order, "smartroad_score_label", "UNKNOWN"),
            "smartroad_same_road": _row_get(order, "smartroad_same_road", 0),
            "smartroad_same_side": _row_get(order, "smartroad_same_side", 0),
            "smartroad_uturn_risk": _row_get(order, "smartroad_uturn_risk", 0),
            "distance_km": _row_get(order, "distance_km", 0),
            "distance_band": _row_get(order, "distance_band", ""),
            "note": "店家完成商品，呼叫同區域 shiper。",
        },
        commit=False,
    )

    db.commit()

    _safe_push_to_role_target(
        db,
        role="STORE",
        target_code=store["store_code"],
        event_type="DRIVER_REQUESTED",
        order_code=order["order_code"],
        message=(
            "FUMAP GO 已呼叫 Shiper\n"
            f"訂單：{order['order_code']}\n"
            f"區域：{order['area_label'] or '-'}\n"
            "訂單已開放給同區域 shiper 接單。"
        ),
        commit=True,
    )

    return get_order_by_code(db, order_code)


def cancel_order(db, *, order_code, actor_role, actor_id=None, actor_code="", reason=""):
    order = get_order_by_code(db, order_code)

    if not order:
        raise OrderError("找不到訂單。")

    if order["status"] in {"DELIVERED", "COMPLETED", "CANCELLED"}:
        raise OrderError("此訂單已結束，不能取消。")

    now = now_iso()
    old_status = order["status"]

    db.execute(
        """
        UPDATE orders
        SET status = 'CANCELLED',
            note = COALESCE(note, '') || ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            f"\n取消原因：{reason or '未填寫'}",
            now,
            order["id"],
        ),
    )

    create_block(
        db,
        event_type="ORDER_CANCELLED",
        actor_role=actor_role,
        actor_id=actor_id,
        actor_code=actor_code,
        order_id=order["id"],
        order_code=order["order_code"],
        previous_status=old_status,
        new_status="CANCELLED",
        amount_twd=order["total_twd"],
        payload={"reason": reason or "未填寫"},
        commit=False,
    )

    db.commit()

    return get_order_by_code(db, order_code)


def normalize_store_manual_payment(value):
    value = (value or "COD").strip().upper()

    if value in {"COD", "CASH_ON_DELIVERY"}:
        return {
            "payment_method": "COD",
            "payment_status": "STORE_COLLECT_AT_PICKUP",
            "prepaid_to": "",
            "label": "貨到付款 COD",
            "note": "COD：Shiper 到店取貨時先向店家支付商品款，配送成功後向客戶收取 COD。",
        }

    if value in {"PREPAID_TO_STORE", "PAID_TO_STORE", "STORE_PREPAID"}:
        return {
            "payment_method": "PREPAID_TO_STORE",
            "payment_status": "PAID_TO_STORE",
            "prepaid_to": "STORE",
            "label": "客戶已付款給店家",
            "note": "客戶已付款給店家：Shiper 到店取貨時不需支付商品款，只負責配送。",
        }

    if value in {"BANK_TRANSFER_ADMIN", "BANK_TRANSFER", "ADMIN_TRANSFER"}:
        return {
            "payment_method": "BANK_TRANSFER",
            "payment_status": "PENDING",
            "prepaid_to": "ADMIN",
            "label": "客戶轉帳給 Admin",
            "note": "BANK_TRANSFER：付款狀態先為 PENDING，Admin 檢查付款截圖；若有問題才暫停訂單。",
        }

    return {
        "payment_method": "COD",
        "payment_status": "STORE_COLLECT_AT_PICKUP",
        "prepaid_to": "",
        "label": "貨到付款 COD",
        "note": "COD：Shiper 到店取貨時先向店家支付商品款，配送成功後向客戶收取 COD。",
    }

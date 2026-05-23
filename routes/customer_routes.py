from flask import Blueprint, render_template, request, redirect, flash, abort

from db import get_db
from services.permission_service import login_required, role_required, current_user
from services.order_service import (
    OrderError,
    create_customer_order,
    get_store_by_code,
    get_order_by_code,
    get_guest_order_by_code_token,
    get_order_items,
    list_customer_orders,
    list_public_stores,
    list_store_products,
    normalize_customer_email,
)
from services.block_service import get_order_blocks, create_block
from services.code_service import now_iso
from services.image_service import ImageUploadError, save_compressed_upload
from services.email_service import (
    normalize_email,
    send_admin_payment_proof_uploaded_email,
    send_customer_payment_proof_received_email,
)
from services.system_flag_service import (
    is_rain_surcharge_enabled,
    get_platform_payment_info,
)
from services.store_hours_service import annotate_store_hours

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


customer_bp = Blueprint("customer", __name__)


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


def _safe_text(value, default=""):
    value = "" if value is None else str(value)
    value = value.strip()
    return value if value else default


def _valid_email(value):
    value = normalize_email(value)

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


def _user_email(user):
    return normalize_email(_row_get(user, "email", ""))


def _user_email_verified(user):
    return bool(_safe_text(_row_get(user, "email_verified_at", "")))


def _is_logged_customer(user):
    return bool(user and _row_get(user, "role", "") == "CUSTOMER")


def _customer_line_target_code(user):
    user_id = _row_get(user, "id", "")

    return f"CUS-{user_id}" if user_id else ""


def _admin_line_target_code():
    return "ADMIN"


def _absolute_order_url(order_code, guest_access_token=""):
    order_code = _safe_text(order_code)

    if guest_access_token:
        return f"/guest/orders/{order_code}?token={guest_access_token}"

    return f"/orders?order_code={order_code}"


def _admin_order_url(order_code):
    return f"/admin/orders?order_code={order_code}"


def _payment_proof_url(order_code):
    return f"/proofs/orders/{order_code}/payment"


def _guest_order_url(order):
    order_code = _safe_text(_row_get(order, "order_code", ""))
    guest_access_token = _safe_text(_row_get(order, "guest_access_token", ""))

    if not order_code or not guest_access_token:
        return "/show"

    return f"/guest/orders/{order_code}?token={guest_access_token}"


def _safe_push_line(db, **kwargs):
    try:
        return push_to_role_target(db, **kwargs)
    except Exception as exc:
        print(f"[LINE_PUSH][CUSTOMER_ROUTES][SKIPPED] {exc}")
        return {"ok": False, "skipped": True, "error": str(exc)}


def _notify_customer_payment_proof_received(db, *, order, user=None, customer_email=""):
    """
    Notify customer after payment proof upload.

    Email is primary.
    LINE is optional if customer has LINE bind.
    Notification failure must not rollback proof/order.
    """
    order_code = _safe_text(_row_get(order, "order_code", ""))
    guest_access_token = _safe_text(_row_get(order, "guest_access_token", ""))

    customer_email = (
        normalize_email(customer_email)
        or normalize_email(_row_get(order, "customer_email", ""))
        or _user_email(user)
    )

    try:
        if customer_email:
            send_customer_payment_proof_received_email(
                order,
                customer_email,
                order_url=_absolute_order_url(order_code, guest_access_token),
            )
    except Exception as exc:
        print(f"[PAYMENT_PROOF][EMAIL][CUSTOMER][ERROR] {exc}")

    try:
        target_code = _customer_line_target_code(user)

        if target_code:
            _safe_push_line(
                db,
                role="CUSTOMER",
                target_code=target_code,
                event_type="PAYMENT_PROOF_RECEIVED",
                order_code=order_code,
                message=(
                    "FUMAP GO 已收到您的轉帳證明\n"
                    f"訂單：{order_code}\n"
                    "狀態：等待 Admin 確認\n"
                    "確認完成後系統會再通知您。"
                ),
                commit=True,
            )
    except Exception as exc:
        print(f"[PAYMENT_PROOF][LINE][CUSTOMER][ERROR] {exc}")


def _notify_admin_payment_proof_uploaded(db, *, order, proof_url=None):
    """
    Notify admin after customer uploads payment proof.

    Admin email is primary.
    Admin LINE is optional.
    Notification failure must not rollback proof/order.
    """
    order_code = _safe_text(_row_get(order, "order_code", ""))

    try:
        send_admin_payment_proof_uploaded_email(
            order,
            review_url=_admin_order_url(order_code),
            proof_url=proof_url or _payment_proof_url(order_code),
        )
    except Exception as exc:
        print(f"[PAYMENT_PROOF][EMAIL][ADMIN][ERROR] {exc}")

    try:
        _safe_push_line(
            db,
            role="ADMIN",
            target_code=_admin_line_target_code(),
            event_type="PAYMENT_PROOF_UPLOADED",
            order_code=order_code,
            message=(
                "FUMAP GO 有新的轉帳付款證明等待確認\n"
                f"訂單：{order_code}\n"
                f"客戶：{_safe_text(_row_get(order, 'customer_name', '-'), '-')}\n"
                f"金額：{int(_row_get(order, 'total_twd', 0) or 0)} TWD\n"
                "請到 Admin 後台確認。"
            ),
            commit=True,
        )
    except Exception as exc:
        print(f"[PAYMENT_PROOF][LINE][ADMIN][ERROR] {exc}")


def _apply_bank_transfer_pending(db, *, order, proof_url="", uploaded_at=""):
    """
    Mark BANK_TRANSFER order as pending Admin review.

    Store should not process this order until Admin confirms payment.
    """
    now = now_iso()

    payment_proof_status = "PENDING_REVIEW" if proof_url else "WAITING_UPLOAD"

    db.execute(
        """
        UPDATE orders
        SET payment_status = 'PENDING',
            payment_proof_image_url = CASE
                WHEN ? != '' THEN ?
                ELSE COALESCE(payment_proof_image_url, '')
            END,
            payment_proof_uploaded_at = CASE
                WHEN ? != '' THEN ?
                ELSE COALESCE(payment_proof_uploaded_at, '')
            END,
            payment_proof_status = ?,
            payment_verified_at = '',
            payment_verified_by = NULL,
            payment_rejected_at = '',
            payment_reject_reason = '',
            admin_hold = 1,
            admin_hold_reason = '等待 Admin 確認轉帳付款',
            admin_hold_at = COALESCE(NULLIF(admin_hold_at, ''), ?),
            updated_at = ?
        WHERE id = ?
        """,
        (
            proof_url,
            proof_url,
            uploaded_at,
            uploaded_at,
            payment_proof_status,
            now,
            now,
            order["id"],
        ),
    )


def _save_payment_proof_file(file, order_code):
    if not file or not getattr(file, "filename", ""):
        return ""

    return save_compressed_upload(
        file,
        kind="proof_image",
        owner_code=f"payment-{order_code}",
    )


def _annotate_store_or_none(store):
    if not store:
        return None

    return annotate_store_hours(store)


def _store_not_accepting_message(store):
    if not store:
        return "店家目前無法接單。"

    label = store.get("accepting_orders_label") or "目前不接單"
    reason = store.get("accepting_orders_reason") or ""

    if reason:
        return f"店家目前無法接單：{label}。{reason}"

    return f"店家目前無法接單：{label}。"


def _order_actor_context(user, order):
    if _is_logged_customer(user):
        user_id = _row_get(user, "id", "")
        return {
            "actor_role": "CUSTOMER",
            "actor_id": user_id,
            "actor_code": _row_get(user, "login_id", "") or f"CUS-{user_id}",
        }

    return {
        "actor_role": "GUEST_CUSTOMER",
        "actor_id": None,
        "actor_code": f"GUEST-{_row_get(order, 'order_code', '')}",
    }


@customer_bp.get("/customer")
@login_required
@role_required("CUSTOMER")
def customer_home():
    return redirect("/show")


@customer_bp.get("/show/store/<store_code>")
def store_detail(store_code):
    db = get_db()

    store = get_store_by_code(db, store_code)

    if not store:
        flash("找不到店家。", "danger")
        return redirect("/show")

    store = _annotate_store_or_none(store)
    products = list_store_products(db, store["id"], only_active=True)

    return render_template(
        "mobile/customer/store_detail.html",
        store=store,
        products=products,
        business_hours_status=store.get("business_hours_status"),
    )


@customer_bp.route("/show/checkout/<store_code>", methods=["GET", "POST"])
def checkout(store_code):
    db = get_db()
    user = current_user()
    is_logged_customer = _is_logged_customer(user)

    customer_user = user if is_logged_customer else None

    store = get_store_by_code(db, store_code)

    if not store:
        flash("找不到店家。", "danger")
        return redirect("/show")

    store = _annotate_store_or_none(store)
    products = list_store_products(db, store["id"], only_active=True)

    if not products:
        flash("此店家尚未有可販售商品。", "warning")
        return redirect(f"/show/store/{store_code}")

    default_customer_email = _user_email(user) if is_logged_customer else ""
    email_available = _valid_email(default_customer_email)

    has_cum_bind = False

    if is_logged_customer:
        try:
            has_cum_bind = bool(customer_can_photo_proof(db, user))
        except Exception:
            has_cum_bind = False

    can_photo_proof = email_available
    can_bank_transfer = email_available
    email_verified = _user_email_verified(user) if is_logged_customer else False

    rain_surcharge_enabled = is_rain_surcharge_enabled(db)
    platform_payment_info = get_platform_payment_info()

    if request.method == "POST":
        try:
            if not store.get("accepting_orders"):
                flash(_store_not_accepting_message(store), "danger")
                return redirect(f"/show/store/{store_code}")

            payment_method = request.form.get("payment_method", "COD").strip().upper()
            delivery_method = request.form.get("delivery_method", "FACE_TO_FACE").strip().upper()

            if payment_method not in {"COD", "BANK_TRANSFER"}:
                payment_method = "COD"

            if delivery_method not in {"FACE_TO_FACE", "PHOTO_PROOF"}:
                delivery_method = "FACE_TO_FACE"

            submitted_customer_email = normalize_email(
                request.form.get("customer_email", "")
                or default_customer_email
            )

            if submitted_customer_email:
                submitted_customer_email = normalize_customer_email(submitted_customer_email)

            submitted_email_valid = _valid_email(submitted_customer_email)

            if payment_method == "BANK_TRANSFER" and not submitted_email_valid:
                flash("請輸入有效 Email 後，才能使用轉帳付款。", "danger")
                return redirect(f"/show/checkout/{store_code}")

            if delivery_method == "PHOTO_PROOF" and not submitted_email_valid:
                flash("請輸入有效 Email 後，才能選擇拍照完成。", "danger")
                return redirect(f"/show/checkout/{store_code}")

            order_source = "CUSTOMER_MARKETPLACE" if is_logged_customer else "GUEST_CHECKOUT"

            order = create_customer_order(
                db,
                customer_user=customer_user,
                store_code=store_code,
                product_id=request.form.get("product_id"),
                qty=request.form.get("qty", 1),
                customer_name=request.form.get("customer_name", ""),
                customer_phone=request.form.get("customer_phone", ""),
                customer_email=submitted_customer_email,
                delivery_address=request.form.get("delivery_address", ""),
                floor_number=request.form.get("floor_number", ""),
                address_note=request.form.get("address_note", ""),
                delivery_lat=request.form.get("delivery_lat", 0),
                delivery_lng=request.form.get("delivery_lng", 0),
                distance_band=request.form.get("distance_band", "0-2KM"),
                city_block=request.form.get("city_block") or store["city_block"] or "ZHONGLI",
                difficulty_flags=request.form.getlist("difficulty_flags"),
                payment_method=payment_method,
                delivery_method=delivery_method,
                note=request.form.get("note", ""),
                invoice_required=request.form.get("invoice_required", "0"),
                invoice_type=request.form.get("invoice_type", "NONE"),
                invoice_title=request.form.get("invoice_title", ""),
                invoice_tax_id=request.form.get("invoice_tax_id", ""),
                invoice_note=request.form.get("invoice_note", ""),
                order_source=order_source,
            )

            if order["payment_method"] == "BANK_TRANSFER":
                now = now_iso()
                proof_url = ""

                payment_proof_file = request.files.get("payment_proof")

                if payment_proof_file and getattr(payment_proof_file, "filename", ""):
                    proof_url = _save_payment_proof_file(
                        payment_proof_file,
                        order["order_code"],
                    )

                _apply_bank_transfer_pending(
                    db,
                    order=order,
                    proof_url=proof_url,
                    uploaded_at=now if proof_url else "",
                )

                actor = _order_actor_context(user, order)

                create_block(
                    db,
                    event_type="BANK_TRANSFER_ORDER_CREATED",
                    actor_role=actor["actor_role"],
                    actor_id=actor["actor_id"],
                    actor_code=actor["actor_code"],
                    order_id=order["id"],
                    order_code=order["order_code"],
                    previous_status="",
                    new_status="PENDING_PAYMENT_REVIEW",
                    amount_twd=order["total_twd"],
                    payload={
                        "order_code": order["order_code"],
                        "order_source": order_source,
                        "is_guest_order": not is_logged_customer,
                        "customer_email_saved": bool(submitted_customer_email),
                        "payment_method": "BANK_TRANSFER",
                        "payment_status": "PENDING",
                        "payment_proof_uploaded": bool(proof_url),
                        "payment_proof_status": "PENDING_REVIEW" if proof_url else "WAITING_UPLOAD",
                        "admin_hold": 1,
                        "admin_hold_reason": "等待 Admin 確認轉帳付款",
                        "email_primary_notification": bool(submitted_customer_email),
                        "line_optional_notification": bool(has_cum_bind),
                        "has_line_bind": bool(has_cum_bind),
                        "invoice_required": int(order["invoice_required"] or 0),
                        "invoice_type": order["invoice_type"] or "NONE",
                        "invoice_title": order["invoice_title"] or "",
                        "invoice_tax_id": order["invoice_tax_id"] or "",
                        "invoice_note": order["invoice_note"] or "",
                    },
                    commit=False,
                )

                if proof_url:
                    create_block(
                        db,
                        event_type="PAYMENT_PROOF_UPLOADED",
                        actor_role=actor["actor_role"],
                        actor_id=actor["actor_id"],
                        actor_code=actor["actor_code"],
                        order_id=order["id"],
                        order_code=order["order_code"],
                        previous_status="",
                        new_status="PENDING_REVIEW",
                        amount_twd=order["total_twd"],
                        payload={
                            "order_code": order["order_code"],
                            "order_source": order_source,
                            "is_guest_order": not is_logged_customer,
                            "customer_email_saved": bool(submitted_customer_email),
                            "payment_method": "BANK_TRANSFER",
                            "payment_status": "PENDING",
                            "payment_proof_status": "PENDING_REVIEW",
                            "payment_proof_uploaded_at": now,
                            "payment_proof_image_url": proof_url,
                        },
                        commit=False,
                    )

                db.commit()

                updated_order = get_order_by_code(db, order["order_code"])

                if proof_url:
                    _notify_customer_payment_proof_received(
                        db,
                        order=updated_order,
                        user=user if is_logged_customer else None,
                        customer_email=submitted_customer_email,
                    )
                    _notify_admin_payment_proof_uploaded(
                        db,
                        order=updated_order,
                        proof_url=_payment_proof_url(order["order_code"]),
                    )

                    flash("訂單已建立，轉帳證明已上傳，等待 Admin 確認。", "success")
                else:
                    flash(
                        "訂單已建立。此訂單為轉帳付款，請保留訂單追蹤頁，並盡快補交轉帳證明給 Admin。",
                        "warning",
                    )

                if is_logged_customer:
                    return redirect(f"/orders?order_code={order['order_code']}")

                return redirect(_guest_order_url(updated_order))

            flash("訂單已建立，已直接送入店家工作台。", "success")

            if is_logged_customer:
                return redirect(f"/orders?order_code={order['order_code']}")

            return redirect(_guest_order_url(order))

        except ImageUploadError as exc:
            db.rollback()
            flash(str(exc), "danger")

        except OrderError as exc:
            db.rollback()
            flash(str(exc), "danger")

        except Exception as exc:
            db.rollback()
            flash(f"建立訂單失敗：{exc}", "danger")

    return render_template(
        "mobile/customer/checkout.html",
        store=store,
        products=products,
        user=user if is_logged_customer else None,
        can_photo_proof=can_photo_proof,
        can_bank_transfer=can_bank_transfer,
        has_cum_bind=has_cum_bind,
        email_verified=email_verified,
        customer_email=default_customer_email,
        is_guest_checkout=not is_logged_customer,
        rain_surcharge_enabled=rain_surcharge_enabled,
        platform_payment_info=platform_payment_info,
        business_hours_status=store.get("business_hours_status"),
    )


@customer_bp.get("/guest/orders/<order_code>")
def guest_order_detail(order_code):
    db = get_db()

    token = request.args.get("token", "").strip()

    order = get_guest_order_by_code_token(db, order_code, token)

    if not order:
        abort(404)

    items = get_order_items(db, order["id"])
    blocks = get_order_blocks(db, order["order_code"])

    return render_template(
        "mobile/customer/guest_order_detail.html",
        order=order,
        selected_order=order,
        selected_items=items,
        selected_blocks=blocks,
        platform_payment_info=get_platform_payment_info(),
    )


@customer_bp.get("/orders")
@login_required
@role_required("CUSTOMER")
def customer_orders():
    db = get_db()
    user = current_user()

    orders = list_customer_orders(db, user["id"], limit=100)

    selected_order = None
    selected_items = []
    selected_blocks = []

    order_code = request.args.get("order_code", "").strip().upper()

    if order_code:
        selected_order = get_order_by_code(db, order_code)

        selected_customer_user_id = _row_get(selected_order, "customer_user_id", 0)

        if (
            selected_order
            and selected_customer_user_id
            and int(selected_customer_user_id) == int(user["id"])
        ):
            selected_items = get_order_items(db, selected_order["id"])
            selected_blocks = get_order_blocks(db, selected_order["order_code"])
        else:
            selected_order = None
            flash("找不到你的訂單。", "warning")

    return render_template(
        "mobile/customer/orders.html",
        orders=orders,
        selected_order=selected_order,
        selected_items=selected_items,
        selected_blocks=selected_blocks,
        platform_payment_info=get_platform_payment_info(),
        user=user,
        email_verified=_user_email_verified(user),
    )

@customer_bp.post("/customer/orders/<order_code>/payment-proof")
@login_required
@role_required("CUSTOMER")
def upload_payment_proof(order_code):
    db = get_db()
    user = current_user()
    order_code = (order_code or "").strip().upper()

    order = get_order_by_code(db, order_code)

    if not order:
        flash("找不到訂單。", "danger")
        return redirect("/orders")

    if int(order["customer_user_id"] or 0) != int(user["id"]):
        flash("你不能上傳其他客戶的訂單付款證明。", "danger")
        return redirect("/orders")

    if order["payment_method"] != "BANK_TRANSFER":
        flash("只有轉帳付款訂單可以上傳轉帳證明。", "warning")
        return redirect(f"/orders?order_code={order_code}")

    customer_email = normalize_email(
        _row_get(order, "customer_email", "")
        or _user_email(user)
    )

    if not _valid_email(customer_email):
        flash("請先提供有效 Email 後，才能上傳轉帳付款證明。", "danger")
        return redirect(f"/orders?order_code={order_code}")

    file = request.files.get("payment_proof")

    if not file or not getattr(file, "filename", ""):
        flash("請選擇轉帳證明圖片。", "warning")
        return redirect(f"/orders?order_code={order_code}")

    try:
        now = now_iso()

        proof_url = _save_payment_proof_file(file, order_code)

        previous_proof_status = ""

        try:
            previous_proof_status = order["payment_proof_status"] or ""
        except Exception:
            previous_proof_status = ""

        db.execute(
            """
            UPDATE orders
            SET payment_status = 'PENDING',
                payment_proof_image_url = ?,
                payment_proof_uploaded_at = ?,
                payment_proof_status = 'PENDING_REVIEW',
                payment_verified_at = '',
                payment_verified_by = NULL,
                payment_rejected_at = '',
                payment_reject_reason = '',
                admin_hold = 1,
                admin_hold_reason = '等待 Admin 確認轉帳付款',
                admin_hold_at = COALESCE(NULLIF(admin_hold_at, ''), ?),
                updated_at = ?
            WHERE id = ?
            """,
            (
                proof_url,
                now,
                now,
                now,
                order["id"],
            ),
        )

        create_block(
            db,
            event_type="PAYMENT_PROOF_UPLOADED",
            actor_role="CUSTOMER",
            actor_id=user["id"],
            actor_code=user["login_id"],
            order_id=order["id"],
            order_code=order["order_code"],
            previous_status=previous_proof_status,
            new_status="PENDING_REVIEW",
            amount_twd=order["total_twd"],
            payload={
                "order_code": order["order_code"],
                "payment_method": order["payment_method"],
                "payment_status": "PENDING",
                "payment_proof_status": "PENDING_REVIEW",
                "payment_proof_uploaded_at": now,
                "payment_proof_image_url": proof_url,
                "admin_hold": 1,
                "admin_hold_reason": "等待 Admin 確認轉帳付款",
                "customer_email_saved": bool(customer_email),
                "email_primary_notification": True,
                "line_optional_notification": bool(customer_can_photo_proof(db, user)),
            },
            commit=False,
        )

        db.commit()

        updated_order = get_order_by_code(db, order_code)

        _notify_customer_payment_proof_received(
            db,
            order=updated_order,
            user=user,
            customer_email=customer_email,
        )

        _notify_admin_payment_proof_uploaded(
            db,
            order=updated_order,
            proof_url=_payment_proof_url(order_code),
        )

        flash("轉帳證明已上傳，等待 Admin 確認。", "success")
        return redirect(f"/orders?order_code={order_code}")

    except ImageUploadError as exc:
        flash(str(exc), "danger")
        return redirect(f"/orders?order_code={order_code}")

    except Exception as exc:
        db.rollback()
        flash(f"上傳轉帳證明失敗：{exc}", "danger")
        return redirect(f"/orders?order_code={order_code}")
